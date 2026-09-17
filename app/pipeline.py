"""The pipeline — one indent, from ER7 bytes to a nurse's phone and a dispense on the server.

This module is the composition root. Everything it calls is already tested on its own
(:mod:`app.hl7.parse`, :mod:`app.safety.validate`, :mod:`app.fhir.build`,
:mod:`app.audit.chain`, :mod:`app.safety.phi`), so what is left to get right here is the
*order* of the calls and what happens when one of them fails.

Four rules govern that order.

**Nothing is dispatched that the gate did not pass.** The dispense is built from
``verdict.prescription``, which the gate sets only on success. There is no code path here
that has both a dispense and a hold, because there is no value to build one from — the
fail-closed property is structural rather than a branch someone has to remember.

**Every outcome is audited, including the ones that stop.** A hold that leaves no trace is
worse than a hold: the drug was not dispensed and nobody can show why. The indent is
recorded as received *before* the gate runs, so a failure mid-gate still leaves evidence
that the message arrived.

**An outage is not a hold.** :func:`app.safety.validate.evaluate` documents this for the
gate; it holds here too. A clinical hold means a human must look at *this drug*; an outage
means the check did not run. Writing ``strength_mismatch`` into an audit trail because
RxNav was down would put a false clinical statement in a permanent record, so an
infrastructure failure produces ``status="error"``. It *returns* rather than raises, so the
audit events already written are not discarded with the exception.

**Nothing composes an outbound string by hand.** The alert comes from
:func:`app.safety.phi.alert_from`, which is the allowlist; the hold detail goes through
:func:`app.safety.phi.hold_view`, which scrubs it. Where this module has to describe a
failure it names the exception *type* and never its message — a parser's message quotes the
segment it choked on, and a PID segment begins with the patient's MRN.

The one exception that propagates is :class:`app.safety.phi.PrivacyViolation`: a payload
that is not the allowlist means patient data was about to reach a phone. That is a defect in
this program, not a condition to report, and it must be loud.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app import demo
from app.audit.chain import ChainEntry, link, verify_chain
from app.config import Settings, get_settings
from app.fhir import build
from app.fhir.client import FhirClient
from app.hl7.fixtures import all_er7
from app.hl7.parse import IndentParseError, parse_indent
from app.models import DispatchAlert, HoldCode, IndentOrder, ValidationOutcome
from app.rxnorm.client import RxNavClient
from app.safety import phi
from app.safety.notify import Delivery, InMemoryNotifier, Notifier
from app.safety.validate import Verdict, evaluate

JSON = dict[str, Any]

#: How long after packing the courier is expected on the ward. A constant rather than a
#: setting: it is a property of the demo's story ("the medicine is on its way"), and a real
#: deployment takes this from the pharmacy's own dispatch system, not from a config file.
ETA_LEAD = timedelta(minutes=30)

#: Audit subtypes, in a local code system. The outer ``type``/``action``/``outcome`` fields
#: of the AuditEvent use the standard vocabularies, so a generic FHIR audit consumer can read
#: the record; only the subtype is ours, because it names an event in this workflow.
SUBTYPE_ORDER_RECEIVED = ("order-received", "Indent received")
SUBTYPE_FORMULATION_HELD = ("formulation-held", "Formulation held")
SUBTYPE_FORMULATION_VERIFIED = ("formulation-verified", "Formulation verified")
SUBTYPE_DISPENSE_WRITTEN = ("dispense-written", "MedicationDispense written")
SUBTYPE_NOTIFICATION_SENT = ("notification-sent", "Dispatch notification sent")

#: The agent named on events recorded before the request has been read, and therefore before
#: any clinician is known. The alternative — leaving ``agent`` empty — would produce a record
#: that does not say who acted, which is the one thing an audit record is for.
#: The resource this pipeline names as the accountable party for events recorded *before* it
#: has correlated a prescriber — a message that would not parse, or an order refused on shape
#: alone. A real seeded ``Organization``, because ``AuditEvent.agent.who`` is validated on
#: write: naming a resource that does not exist gets the whole event rejected by HAPI, so a
#: hold would leave no trail at all. See :func:`app.fhir.build.organization`.
SYSTEM_AGENT_LOCAL_ID = "organization-dispensing-service"


# --------------------------------------------------------------------------------------
# Operational context — the facts the workflow needs that no message carries
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Courier:
    """Who is carrying it, and where to.

    Not read from the inbound message: a nurse's indent says what to send, not who will
    carry it, and inventing a courier from the message would put a name in the record that
    nobody assigned. In the demo these come from the seeded dataset; in a deployment they
    come from the pharmacy's dispatch system.
    """

    practitioner_id: str
    display_name: str
    destination_id: str
    destination_display: str


@dataclass(frozen=True)
class Event:
    """One audit event: what happened, its place in the chain, and the bytes that were hashed.

    ``payload`` is kept rather than reconstructed when the resource is written. Rebuilding it
    from the friendly fields would mean two definitions of the same event that have to stay
    in step, and a drift between them would make the chain verify as broken the first time
    anybody checked it — a false tamper alarm, which is the worst possible failure for a
    tamper-evident log.
    """

    subtype: str
    display: str
    outcome: str
    detail: str
    entities: tuple[str, ...]
    entry: ChainEntry
    payload: JSON

    def to_json(self) -> JSON:
        """The event as it may be published: digests, dimensions, and no patient data.

        ``entities`` is deliberately absent. A FHIR reference is a *linkable identifier* —
        ``MedicationDispense/…-RX-1001`` carries the filler order number — and the whole
        point of the boundary is that what leaves this process can be correlated but not
        resolved back to a person. The references are on the server, in the AuditEvent's
        ``entity.what``, where access control applies; a caller that needs to address the
        resource reads it from there. :meth:`to_record` is the version that keeps them.
        """
        return {
            "sequence": self.entry.sequence,
            "subtype": self.subtype,
            "display": self.display,
            "outcome": self.outcome,
            "detail": self.detail,
            "hash": self.entry.hash,
            "prev_hash": self.entry.prev_hash,
        }

    def to_record(self) -> JSON:
        """The event with its entity references — the operator's view, not the wire's."""
        return {**self.to_json(), "entities": list(self.entities)}


class Trail:
    """Accumulates audit events, chaining each to the one before it.

    A class rather than a list plus a helper because the chain is stateful in a way that is
    easy to get wrong by hand: every event must hash the *previous* event, and one built
    before the event it follows would verify as a break. Here :meth:`record` is the only way
    to make an :class:`Event`, so a mis-ordered chain cannot be expressed.
    """

    def __init__(self, *, agent_ref: str) -> None:
        #: A full reference (``Organization/x``, ``Practitioner/y``), not a bare id: the two
        #: are different resource types and only the caller knows which applies.
        self.agent_ref = agent_ref
        self.events: list[Event] = []

    def record(
        self,
        subtype: tuple[str, str],
        *,
        detail: str,
        outcome: str,
        entities: Iterable[str] = (),
        agent_ref: str | None = None,
    ) -> Event:
        code, display = subtype
        refs = list(entities)
        payload = build.audit_payload(
            subtype_code=code,
            subtype_display=display,
            # When *we* observed the fact. Not the message's timestamp: an inbound
            # timestamp is a claim by the sender, and the chain can only attest to what
            # this process saw.
            recorded=datetime.now(UTC).isoformat(timespec="seconds"),
            outcome_code=outcome,
            outcome_desc=detail,
            agent_ref=agent_ref or self.agent_ref,
            entity_refs=refs,
        )
        # ``link(prev=None)`` starts a fresh chain at genesis, so the first event and the
        # rest go through one code path rather than two.
        entry = link(self.events[-1].entry if self.events else None, payload)
        event = Event(
            subtype=code,
            display=display,
            outcome=outcome,
            detail=detail,
            entities=tuple(refs),
            entry=entry,
            payload=payload,
        )
        self.events.append(event)
        return event


@dataclass
class Run:
    """Everything one indent produced — the unit the CLI prints and the API returns.

    Holds the internal record as well as the outbound one, because the *contrast* between
    them is what this project demonstrates. :meth:`public` is the half that may leave the
    process; :meth:`internal` is what an operator sees. They are separate methods rather
    than one dict behind a flag so that adding a field to the internal record cannot
    accidentally add it to the response.
    """

    status: str = "error"  # "dispensed" | "held" | "error"
    order: IndentOrder | None = None
    verdict: Verdict | None = None
    events: list[Event] = field(default_factory=list)
    dispense: JSON | None = None
    alert: DispatchAlert | None = None
    delivery: Delivery | None = None
    token: str | None = None
    eta: str | None = None
    error: str | None = None
    #: Audit writes that failed. The dispense stands and the courier is already carrying the
    #: medicine, so this is reported rather than escalated; but it is reported.
    audit_problems: list[str] = field(default_factory=list)

    @property
    def code(self) -> str:
        if self.status == "error":
            return "processing_error"
        if self.verdict is None:
            return "not_processed"
        return self.verdict.outcome.code

    @property
    def detail(self) -> str:
        if self.status == "error":
            return self.error or "the request could not be processed"
        if self.verdict is None:
            return "the indent was not processed"
        return self.verdict.outcome.detail

    @property
    def chain_intact(self) -> bool:
        return not verify_chain([e.entry for e in self.events])

    # -- the two views -----------------------------------------------------------------

    def public(self) -> JSON:
        """Safe to serialise, log, or hand to a caller.

        Assembled from named fields — never a source dict with keys removed, because
        subtraction is the failure mode an allowlist exists to avoid.
        """
        known = _known_identifiers(self.order) if self.order is not None else ()
        if self.status == "error":
            view: JSON = {"status": "error", "code": self.code, "detail": self.detail}
        else:
            view = phi.hold_view(self.code, self.detail, known=known)
            view["status"] = self.status

        if self.alert is not None:
            view["alert"] = self.alert.to_payload()
        if self.token is not None:
            view["token"] = self.token
        if self.eta is not None:
            view["eta"] = self.eta
        if self.delivery is not None:
            view["notified"] = self.delivery.delivered
        # Note what is *not* here: the dispense id, and the audit entities. Both are FHIR
        # references derived from the order, and both are therefore linkable identifiers —
        # `…-dispense-RX-1001` names the filler order number in its last five characters.
        # The token is the correlation handle this view offers instead, and it is the only
        # one, which is why it is not optional on a dispatched run.
        # Event details have to cross the same boundary as the primary hold detail.  The
        # durable AuditEvent records a deliberately PHI-free detail, but this second pass
        # keeps a future event author from reintroducing an identifier through the summary.
        public_events: list[JSON] = []
        for event in self.events:
            event_view = event.to_json()
            event_view["detail"] = phi.scrub(event.detail, known=known)
            public_events.append(event_view)
        view["audit"] = public_events
        # A dispense cannot be unwound merely because the record system failed after the
        # handoff, but callers must never be told an audit trail exists when it did not
        # persist.  Production deployments should back this with a durable outbox.
        view["audit_recorded"] = not self.audit_problems
        return view

    def internal(self) -> JSON:
        """The operator's record — what the system knows, PHI included.

        Named ``internal`` rather than reached by ``public(include_phi=True)`` so that no
        caller can get here by passing a flag, and so that grepping for it finds only this
        module, the CLI, and the tests.
        """
        order = self.order
        return {
            "status": self.status,
            "code": self.code,
            # PHI-free by contract — see models.ValidationOutcome.
            "detail": self.detail,
            "patient": {
                "mrn": order.patient_mrn if order else None,
                "name": order.patient_name if order else None,
            },
            "order": {
                "placer": order.placer_order_number if order else None,
                "filler": order.filler_order_number if order else None,
                "control": order.order_control if order else None,
                "message_control_id": order.message_control_id if order else None,
            },
            "requested_display": order.requested_display if order else None,
            "prescribed": self._prescribed_summary(),
            "dispense_id": self.dispense.get("id") if self.dispense else None,
            "chain": [e.to_record() for e in self.events],
            "chain_intact": self.chain_intact,
            "audit_problems": list(self.audit_problems),
            "error": self.error,
        }

    def _prescribed_summary(self) -> JSON | None:
        """The normalized formulation, once the gate has got far enough to produce one.

        The drug, never the patient — this is the internal view, and a drug name is not an
        identifier.
        """
        if self.verdict is None:
            return None
        drug = self.verdict.prescription.drug if self.verdict.prescription else self.verdict.requested
        if drug is None:
            return None
        strength = (
            None
            if drug.strength_value is None
            else f"{drug.strength_value:g} {drug.strength_unit or ''}".strip()
        )
        return {
            "ingredient": drug.ingredient_name,
            "strength": strength,
            "dose_form": drug.dose_form,
            # Provenance, for the operator: which RxNorm concept the triple came from, and
            # why it is empty when it is. The alert never sees this.
            "source_rxcui": drug.source_rxcui,
            "unresolved_reason": drug.unresolved_reason,
        }


# --------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------


def process(
    er7: str,
    *,
    fhir: FhirClient,
    rxnav: RxNavClient,
    notifier: Notifier,
    settings: Settings,
    courier: Courier,
    now: datetime | None = None,
) -> Run:
    """Take one indent from ER7 to a dispense, a notification, and an audit trail.

    Returns a :class:`Run` for every outcome a reviewer would want recorded — dispatched,
    held, or an infrastructure error. Only :class:`~app.safety.phi.PrivacyViolation` escapes,
    and it escapes on purpose.
    """
    now = now or datetime.now(UTC)
    # The default agent is the service itself. Events recorded before a prescriber has been
    # correlated — a message that would not parse, an order refused on shape — have no
    # clinician to name, and `record(agent_ref=...)` overrides this once one is known.
    trail = Trail(agent_ref=system_agent_ref(settings))
    run = Run()

    # -- 1. parse ----------------------------------------------------------------------
    # A message hl7apy cannot read is a hold, not a crash: the sending interface is at
    # fault, and the operator needs a code rather than a stack trace. The exception's own
    # message is deliberately *not* carried into the detail — an HL7 parser reports the
    # segment it choked on, and the first segment carrying patient data is the PID.
    try:
        order = parse_indent(er7)
    except IndentParseError as exc:
        trail.record(
            SUBTYPE_ORDER_RECEIVED,
            detail=f"indent could not be parsed: {type(exc).__name__}",
            outcome=build.OUTCOME_HOLD,
        )
        run.status = "held"
        run.events = trail.events
        run.verdict = Verdict(
            outcome=ValidationOutcome.hold(
                HoldCode.UNPARSEABLE_INDENT,
                "the message is not a readable OMP^O09 order, so nothing was dispensed",
            )
        )
        # Filed like every other outcome. This branch used to return before reaching the
        # write, which made an unreadable message the one thing the gate refused without
        # leaving a trace on the server — the exact event an auditor would go looking for.
        run.audit_problems = _write_audit_events(
            fhir, settings, _audit_key(None, er7), trail.events
        )
        return run

    run.order = order
    trail.record(
        SUBTYPE_ORDER_RECEIVED,
        # The drug, and no identifier. The message control id was here in the first draft,
        # and it is the same defect as the dispense id further down: `MSG00001` is not a
        # patient identifier on its own, but it resolves like one. AuditEvent ids are
        # `{namespace}-audit-{message_control_id}-{seq}`, `/health` publishes the namespace,
        # and the public sandbox has no access control — so a value published here would
        # walk a reader from this response to the MedicationRequest to the Patient, using
        # nothing but what this API hands out. An event is identified by its sequence number
        # and its place in the chain; the message it came from is named by the resource id on
        # the server, where reading it requires access.
        detail=f"indent received for {order.requested_display}",
        outcome=build.OUTCOME_SUCCESS,
    )

    # -- 2. the gate -------------------------------------------------------------------
    # The only step that can raise on infrastructure. The trail is attached to the run
    # before the failure is described, so what was already recorded survives.
    try:
        verdict = evaluate(order, fhir=fhir, rxnav=rxnav)
    except Exception as exc:  # noqa: BLE001 — deliberately broad; see the module docstring
        trail.record(
            SUBTYPE_FORMULATION_HELD,
            detail=f"the check did not run: {type(exc).__name__}",
            outcome=build.OUTCOME_HOLD,
        )
        run.status = "error"
        run.events = trail.events
        run.error = (
            "the safety check could not be completed — this is not a clinical rejection, "
            "and nothing was dispensed"
        )
        return run

    run.verdict = verdict
    agent = agent_ref(verdict, settings)

    if not verdict.ok:
        trail.record(
            SUBTYPE_FORMULATION_HELD,
            detail=verdict.outcome.detail,
            outcome=build.OUTCOME_HOLD,
            entities=_request_entities(verdict),
            agent_ref=agent,
        )
        run.status = "held"
        run.events = trail.events
        run.audit_problems = _write_audit_events(
            fhir, settings, _audit_key(order, er7), trail.events
        )
        return run

    # -- 3. the write-back -------------------------------------------------------------
    assert verdict.prescription is not None  # guaranteed by Verdict on success
    prescription = verdict.prescription

    iso_eta, clock_eta = _eta(now)
    run.eta = clock_eta

    dispense = build.medication_dispense(
        resource_id=settings.ns_id(f"dispense-{order.filler_order_number}"),
        # The request's own CodeableConcept, copied rather than rebuilt, so the dispense
        # and the prescription cannot come to disagree about what was dispensed.
        medication=prescription.medication,
        subject_id=prescription.patient_id,
        authorizing_request_id=prescription.request_id,
        packed_at=now.isoformat(timespec="seconds"),
        handed_over_at=now.isoformat(timespec="seconds"),
        eta=iso_eta,
        courier_id=courier.practitioner_id,
        courier_display_name=courier.display_name,
        destination_id=courier.destination_id,
        destination_display=courier.destination_display,
        quantity_value=1,
        quantity_unit="pen",
    )

    trail.record(
        SUBTYPE_FORMULATION_VERIFIED,
        detail=verdict.outcome.detail,
        outcome=build.OUTCOME_SUCCESS,
        entities=_request_entities(verdict),
        agent_ref=agent,
    )

    try:
        written = fhir.update(dispense)
    except Exception as exc:  # noqa: BLE001
        trail.record(
            SUBTYPE_DISPENSE_WRITTEN,
            detail=f"the dispense could not be written: {type(exc).__name__}",
            outcome=build.OUTCOME_HOLD,
            entities=_request_entities(verdict),
            agent_ref=agent,
        )
        run.status = "error"
        run.events = trail.events
        run.error = "the dispense could not be written to the record"
        return run

    run.dispense = written
    trail.record(
        SUBTYPE_DISPENSE_WRITTEN,
        # The drug, not the resource id. The link to the resource is the event's
        # `entity.what` reference, which is the correct FHIR modelling anyway — prose that
        # names an id duplicates the reference and leaks it into every published summary.
        detail=f"dispense written for {order.requested_display}",
        outcome=build.OUTCOME_SUCCESS,
        entities=[f"MedicationDispense/{written.get('id')}", *_request_entities(verdict)],
        agent_ref=agent,
    )

    # -- 4. the notification -----------------------------------------------------------
    # Built through `phi.alert_from`, and the *form* of this call is the privacy control:
    # four keyword arguments, every one of them either an operational fact or workforce
    # identity. There is no argument here that could carry the patient, which is the whole
    # argument for an allowlist over a filter.
    alert = phi.alert_from(
        item_description=order.requested_display,
        quantity="1 pen",
        courier_display_name=courier.display_name,
        eta=clock_eta,
        known=_known_identifiers(order),
    )
    token = notifier.token_for(order.placer_order_number, order.filler_order_number)
    delivery = notifier.send(alert, token=token)

    run.alert = alert
    run.token = token
    run.delivery = delivery
    run.status = "dispensed"

    trail.record(
        SUBTYPE_NOTIFICATION_SENT,
        detail=(
            "dispatch notification delivered"
            if delivery.delivered
            else "dispatch notification not delivered"
        ),
        # A failed send is a hold in the *trail*, not in the run: the medicine is already
        # on its way, so the delivery status must not be recorded as a clinical rejection.
        # It carries the transport's error string, which is ours, not upstream text.
        outcome=build.OUTCOME_SUCCESS if delivery.delivered else build.OUTCOME_HOLD,
        entities=[f"MedicationDispense/{written.get('id')}"],
        agent_ref=agent,
    )
    run.events = trail.events
    run.audit_problems = _write_audit_events(fhir, settings, _audit_key(order, er7), trail.events)
    return run


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def system_agent_ref(settings: Settings) -> str:
    """The service itself, as a reference the FHIR server can resolve.

    Namespaced like every other client-assigned id, because the ``Organization`` it points at
    is seeded alongside the demo dataset — see :func:`app.fhir.seed.build_dataset`.
    """
    return f"Organization/{settings.ns_id(SYSTEM_AGENT_LOCAL_ID)}"


def agent_ref(verdict: Verdict, settings: Settings) -> str:
    """Who is accountable for this event: the prescriber if known, else the service.

    The distinction is not cosmetic. A hold that got as far as correlating a request is a
    clinical judgement *about that prescriber's order*, and the trail should say so. A hold
    that stopped earlier — an unreadable message, an order number matching nothing — is a
    judgement this service made on its own, and naming a clinician for it would put a
    person's name on a decision they had no part in.
    """
    if verdict.prescription is not None:
        return f"Practitioner/{verdict.prescription.requester_id}"
    return system_agent_ref(settings)


def _eta(now: datetime) -> tuple[str, str]:
    """``(ISO 8601 for the FHIR extension, wall clock for the phone)``.

    Two renderings because the two consumers want different things: the resource stores an
    instant, and a nurse reading a lock screen wants ``14:30``.
    """
    expected = now + ETA_LEAD
    return expected.isoformat(timespec="seconds"), expected.strftime("%H:%M")


def _request_entities(verdict: Verdict) -> list[str]:
    """The FHIR resources an event is about — references, never inline data."""
    if verdict.prescription is None:
        return []
    return [f"MedicationRequest/{verdict.prescription.request_id}"]


def _known_identifiers(order: IndentOrder) -> tuple[str, ...]:
    """Values that must be refused if they turn up inside an allowed field.

    Passed to :func:`app.safety.phi.alert_from` so a patient name arriving in
    ``item_description`` is caught. A person's name has no distinguishing syntax — there is
    no pattern that means "this is a human" — so it can only be caught by being named. The
    error message is included for the same reason: it is the field most likely to quote back
    whatever the sender wrote.
    """
    parts = order.patient_name.replace(",", " ").split() if order.patient_name else []
    return tuple(
        v
        for v in (
            order.patient_name,
            order.patient_mrn,
            order.placer_order_number,
            order.filler_order_number,
            *parts,
        )
        if v
    )


def _audit_key(order: IndentOrder | None, er7: str) -> str:
    """The id stem for this run's AuditEvents.

    Normally the message control id, which is what makes re-running a message *replace* its
    events rather than accumulate duplicate ones — the same idempotency ``make seed`` relies
    on.

    A message that would not parse has no control id to read, but it still has to be filed:
    a malformed interface message is exactly the kind of event an auditor asks about, and
    until this existed an unparseable indent was the one outcome that reached the response
    but never the server. The stem is a digest of the raw bytes instead.

    Digesting the message is not a disclosure. The ER7 *is* the PHI-bearing artifact, so
    anyone holding it already holds everything the digest could lead back to, and the digest
    does not invert. It is truncated to keep the id legible, and prefixed so an operator
    reading server ids can tell the two cases apart.
    """
    if order is not None:
        return order.message_control_id
    return f"unparsed-{hashlib.sha256(er7.encode()).hexdigest()[:12]}"


def _write_audit_events(
    fhir: FhirClient, settings: Settings, audit_key: str, events: Iterable[Event]
) -> list[str]:
    """Put the chain on the server, one AuditEvent per link.

    Ids derive from ``audit_key`` (see :func:`_audit_key`) and the sequence number, so
    re-running a message replaces its events rather than accumulating duplicates.

    Failures are collected, not raised. The dispense is already written and the courier is
    already carrying the medicine; failing to file the paperwork must not unwind either. But
    a trail that silently did not reach the server is exactly the gap this feature exists to
    close, so the caller is told.
    """
    problems: list[str] = []
    for event in events:
        resource = build.audit_event(
            resource_id=settings.ns_id(f"audit-{audit_key}-{event.entry.sequence}"),
            payload=event.payload,
            entry=event.entry,
        )
        try:
            fhir.update(resource)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{event.subtype}: {type(exc).__name__}")
    return problems


# --------------------------------------------------------------------------------------
# An in-memory FHIR server, so `--dry-run` needs no network at all
# --------------------------------------------------------------------------------------


class InMemoryFhirTransport(httpx.BaseTransport):
    """Serves a resource list and accepts writes, entirely in memory.

    Exists so the pipeline can be demonstrated end to end with no network: a reviewer on a
    locked-down machine, or a recording made when the public sandbox happens to be down,
    still gets a complete run. It implements only what this pipeline asks for — identifier
    search, instance read, and PUT — so it cannot quietly stand in for a real server in any
    other respect.

    It is, however, deliberately **stricter than the real server in one place**: a write
    whose references do not resolve to a held resource is refused, the way HAPI refuses it
    (``HAPI-1094``). That check is here because its absence was a real defect. The pipeline
    once wrote ``AuditEvent.agent.who = Practitioner/coldchain-pipeline``; HAPI rejected
    every one of those writes, so a hold left no server-side trail at all — and the entire
    offline suite passed, because nothing here looked at references. A fake that is more
    permissive than production tests a system that does not exist.
    """

    def __init__(self, resources: Iterable[JSON]) -> None:
        self.resources: dict[tuple[str, str], JSON] = {
            (r["resourceType"], r["id"]): dict(r) for r in resources
        }
        self.writes: list[tuple[str, str]] = []

    #: Where a reference can hide, per resource type. Not a general FHIR walker: it covers
    #: the resources this pipeline writes, and a new one has to be added deliberately, which
    #: is the point — an entry here is a claim that this reference is validated in production.
    _REFERENCE_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
        "MedicationDispense": (
            ("subject", "reference"),
            ("authorizingPrescription", 0, "reference"),
            ("performer", 0, "actor", "reference"),
            ("destination", "reference"),
        ),
        "AuditEvent": (
            ("agent", 0, "who", "reference"),
            ("entity", 0, "what", "reference"),
        ),
        "MedicationRequest": (
            ("subject", "reference"),
            ("requester", "reference"),
        ),
    }

    @staticmethod
    def _at(resource: JSON, path: tuple[Any, ...]) -> str | None:
        node: Any = resource
        for step in path:
            if isinstance(step, int):
                if not isinstance(node, list) or len(node) <= step:
                    return None
                node = node[step]
            else:
                if not isinstance(node, dict) or step not in node:
                    return None
                node = node[step]
        return node if isinstance(node, str) else None

    def _unresolved(self, resource: JSON) -> list[str]:
        """References on ``resource`` that name something the server does not hold."""
        missing: list[str] = []
        for path in self._REFERENCE_PATHS.get(resource["resourceType"], ()):
            ref = self._at(resource, path)
            if not ref or "/" not in ref:
                continue
            # A relative reference and nothing else — this transport holds one server's
            # worth of resources, so a URN or an absolute URL is out of scope rather than
            # silently accepted.
            kind, _, rid = ref.partition("/")
            if (kind, rid) not in self.resources:
                missing.append(ref)
        return missing

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        parts = [p for p in request.url.path.split("/") if p]
        known = {rt for rt, _ in self.resources} | {
            "Patient",
            "Practitioner",
            "Organization",
            "Location",
            "MedicationRequest",
            "MedicationDispense",
            "AuditEvent",
        }
        for index, segment in enumerate(parts):
            if segment in known:
                parts = parts[index:]
                break
        else:
            return httpx.Response(404, json={"resourceType": "OperationOutcome"})

        resource_type, *rest = parts

        if request.method == "PUT" and rest:
            body = json.loads(request.content)
            if missing := self._unresolved(body):
                # The shape of HAPI's own refusal, so a caller that handles the real server's
                # error handles this one too.
                return httpx.Response(
                    400,
                    json={
                        "resourceType": "OperationOutcome",
                        "issue": [
                            {
                                "severity": "error",
                                "code": "processing",
                                "diagnostics": (
                                    f"HAPI-1094: Resource {missing[0]} not found, "
                                    f"specified in path: references on "
                                    f"{resource_type}/{rest[0]}"
                                ),
                            }
                        ],
                    },
                )
            self.resources[(resource_type, rest[0])] = body
            self.writes.append((resource_type, rest[0]))
            return httpx.Response(200, json=body)

        if request.method != "GET":
            return httpx.Response(405, json={"resourceType": "OperationOutcome"})

        if not rest:
            identifier = request.url.params.get("identifier")
            if not identifier:
                return httpx.Response(400, json={"resourceType": "OperationOutcome"})
            system, _, value = identifier.partition("|")
            matches = [
                r
                for (rt, _), r in self.resources.items()
                if rt == resource_type
                and any(
                    i.get("system") == system and i.get("value") == value
                    for i in (r.get("identifier") or [])
                )
            ]
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": len(matches),
                    "entry": [{"resource": r} for r in matches],
                },
            )

        found = self.resources.get((resource_type, rest[0]))
        if found is None:
            return httpx.Response(404, json={"resourceType": "OperationOutcome"})
        return httpx.Response(200, json=found)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def courier_from(dataset) -> Courier:
    """The dispatch context for a seeded dataset."""
    return Courier(
        practitioner_id=dataset.courier["id"],
        display_name=demo.COURIER_NAME,
        destination_id=dataset.ward["id"],
        destination_display=dataset.ward.get("name", demo.WARD_NAME),
    )


def _report(run: Run, label: str) -> str:
    """One fixture's output: the internal record beside what the phone received.

    Printed from :meth:`Run.internal` and :meth:`Run.public` rather than from ad-hoc dicts,
    so what the CLI shows is exactly what those two views contain.
    """
    width = max(0, 66 - len(label))
    lines = [f"── {label} " + "─" * width]
    lines.append(f"   status    {run.status}  ({run.code})")
    lines.append(f"   detail    {run.detail}")

    internal = run.internal()
    patient = internal["patient"]
    if patient["mrn"]:
        lines.append(
            f"   internal  {patient['name']}  mrn={patient['mrn']}  "
            f"order={internal['order']['placer']}"
        )
    if run.verdict and internal["prescribed"]:
        p = internal["prescribed"]
        lines.append(f"   compare   {p['ingredient']} | {p['strength']} | {p['dose_form']}")

    if run.alert:
        lines.append(f"   device    {json.dumps(run.alert.to_payload())}")
        lines.append(f"   token     {run.token}")
    if run.delivery and not run.delivery.delivered:
        lines.append(f"   !         not delivered: {run.delivery.error}")

    if not run.events:
        lines.append("   audit     (nothing recorded)")
    for event in run.events:
        lines.append(f"   audit {event.entry.sequence}   {event.subtype:<22} {event.detail[:56]}")
    if run.audit_problems:
        lines.append(f"   !         audit writes failed: {', '.join(run.audit_problems)}")
    if run.dispense:
        lines.append(f"   wrote     MedicationDispense/{run.dispense.get('id')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="app.pipeline", description="Walk an OMP^O09 indent through the cold-chain gate."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", action="store_true", help="walk every fixture in one run")
    group.add_argument("--fixture", help="run one named fixture")
    group.add_argument("--file", help="run an ER7 message read from a file ('-' for stdin)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="serve FHIR from memory instead of the sandbox: no network, no writes",
    )
    parser.add_argument("--json", action="store_true", help="emit the public view as JSON")
    args = parser.parse_args(argv)

    settings = get_settings()

    from app.fhir.seed import build_dataset

    dataset = build_dataset(settings)

    if args.file:
        text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
        messages = {"<stdin>" if args.file == "-" else args.file: text}
    else:
        messages = all_er7()
        if args.fixture:
            if args.fixture not in messages:
                print(
                    f"unknown fixture {args.fixture!r}; known: {', '.join(sorted(messages))}",
                    file=sys.stderr,
                )
                return 2
            messages = {args.fixture: messages[args.fixture]}

    if args.dry_run:
        # The dataset is served from memory, so this path never touches the sandbox. It is
        # also the only path that works with no network at all.
        transport = InMemoryFhirTransport(dataset.resources())
        fhir = FhirClient(settings.fhir_root, transport=transport)
    else:
        transport = None
        fhir = FhirClient(settings.fhir_root, timeout_s=settings.fhir_timeout_s)

    courier = courier_from(dataset)
    notifier = InMemoryNotifier()
    runs: list[Run] = []

    with fhir, RxNavClient(settings.rxnav_root, timeout_s=settings.rxnav_timeout_s) as rxnav:
        for label, er7 in messages.items():
            run = process(
                er7,
                fhir=fhir,
                rxnav=rxnav,
                notifier=notifier,
                settings=settings,
                courier=courier,
            )
            runs.append(run)
            if args.json:
                print(json.dumps(run.public(), indent=2))
            else:
                print(_report(run, label))
                print()

    if args.json:
        return 0

    dispensed = sum(1 for r in runs if r.status == "dispensed")
    held = sum(1 for r in runs if r.status == "held")
    errored = sum(1 for r in runs if r.status == "error")
    delivered = sum(1 for d in notifier.deliveries if d.delivered)
    print(
        f"{len(runs)} indent(s): {dispensed} dispensed, {held} held, {errored} errored; "
        f"{delivered} notification(s) delivered"
    )
    if transport is not None:
        print(f"in-memory server: {len(transport.writes)} resource(s) written, nothing sent")
    if any(r.audit_problems for r in runs):
        print("warning: some audit events could not be written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
