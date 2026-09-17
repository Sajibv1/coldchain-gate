"""The pipeline, end to end and offline.

These tests do not re-check the gate — ``test_validate.py`` owns that. What is tested here is
the *composition*: that the order of operations makes the safety properties structural
rather than conventional, that an outage is never recorded as a clinical finding, and that
the claim the whole submission rests on holds on every path — no patient data reaches the
device, and every outcome leaves a trail a third party can verify.

The FHIR transport is the pipeline's own ``--dry-run`` server, so the offline demo path is
exercised by every test here rather than only by hand.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import httpx

from app import demo
from app.audit.chain import verify_chain
from app.fhir import build, seed
from app.fhir.client import FhirClient
from app.hl7 import fixtures
from app.hl7.parse import IndentParseError, parse_indent
from app.main import response_for
from app.models import ALERT_FIELDS, HoldCode, ValidationOutcome
from app.pipeline import (
    SYSTEM_AGENT_LOCAL_ID,
    Courier,
    InMemoryFhirTransport,
    Run,
    courier_from,
    process,
)
from app.safety import phi
from app.safety.notify import InMemoryNotifier
from app.safety.validate import Verdict


#: A fixed instant, so the ETA is predictable and no assertion depends on the wall clock.
NOW = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)

#: Everything about the seeded patient that must never reach an outbound payload. Drawn from
#: ``app.demo`` rather than retyped, so changing the demo data cannot leave these assertions
#: passing while looking for a string the fixtures no longer contain.
PATIENT_VALUES = (
    demo.MRN,
    demo.PATIENT_FAMILY,
    demo.PATIENT_GIVEN,
    "DOE, JOHN",
    demo.PLACER_ORDER_NUMBER,
    demo.FILLER_ORDER_NUMBER,
)


def outbound_forbidden(name: str) -> tuple[str, ...]:
    """Values that must not appear in the public view of fixture ``name``.

    Per-fixture because each message has its own control id, and the message control id
    belongs on this list even though it is not a patient identifier — it *resolves* like one.
    AuditEvent ids are ``{namespace}-audit-{message_control_id}-{seq}``, ``GET /health``
    publishes the namespace, and the demo server has no access control, so publishing the
    control id would let anyone the API already answered walk from the response to the
    MedicationRequest to the Patient without ever being authorized. What is linkable is
    treated as PHI here, not only what is identifying.
    """
    return (*PATIENT_VALUES, fixtures.FIXTURES[name].message_control_id)

#: Every clinical hold code. Kept as an explicit set so a code invented at a call site —
#: rather than added to ``HoldCode`` — fails the test that checks membership.
HOLD_CODES = {
    value
    for name, value in vars(HoldCode).items()
    if not name.startswith("_") and isinstance(value, str)
}


class RecordingTransport(InMemoryFhirTransport):
    """The dry-run server, plus a log of everything written to it."""

    def __init__(self, resources) -> None:
        super().__init__(resources)
        self.put: list[tuple[str, dict]] = []

    def handle_request(self, request):
        response = super().handle_request(request)
        if request.method == "PUT":
            self.put.append((request.url.path, json.loads(request.content)))
        return response


class AuditRejectingTransport(RecordingTransport):
    """Accept the clinical write but simulate an unavailable audit repository."""

    def handle_request(self, request):
        if request.method == "PUT" and "/AuditEvent/" in request.url.path:
            return httpx.Response(503, json={"resourceType": "OperationOutcome"})
        return super().handle_request(request)


class BrokenFhir:
    """A FHIR client that fails every call, for the outage path.

    Raises with a message that quotes a PID segment on purpose: an exception raised while
    handling an inbound message very often does, and the pipeline must not republish it.
    """

    MESSAGE = f"failed while handling PID|||{demo.MRN}||{demo.PATIENT_FAMILY}^JOHN"

    def read(self, *a, **k):
        raise RuntimeError(self.MESSAGE)

    def search_by_identifier(self, *a, **k):
        raise RuntimeError(self.MESSAGE)

    def update(self, *a, **k):
        raise RuntimeError(self.MESSAGE)


def _drive(er7: str, *, rxnav, settings, fhir=None, notifier=None, courier=None) -> Run:
    """Run one message through the pipeline against fake servers."""
    dataset = seed.build_dataset(settings)
    transport = None if fhir is not None else RecordingTransport(dataset.resources())
    client = fhir if fhir is not None else FhirClient(
        "https://hapi.fhir.org/baseR4", transport=transport
    )
    try:
        run = process(
            er7,
            fhir=client,
            rxnav=rxnav,
            notifier=notifier or InMemoryNotifier(),
            settings=settings,
            courier=courier or courier_from(dataset),
            now=NOW,
        )
    finally:
        if fhir is None:
            client.close()
    return run


@pytest.fixture
def go(rxnav, settings):
    """Run a named fixture. Keeps each test to one line."""

    def _go(name: str, *, notifier=None) -> Run:
        return _drive(fixtures.er7(name), rxnav=rxnav, settings=settings, notifier=notifier)

    return _go


# -- the happy path ---------------------------------------------------------------------


def test_a_matching_indent_is_dispensed(go):
    run = go("clean")
    assert run.status == "dispensed"
    assert run.dispense["resourceType"] == "MedicationDispense"


def test_the_dispense_records_packing_courier_and_eta(go):
    """The three things the brief asks the write-back to carry."""
    dispense = go("clean").dispense
    assert dispense["whenPrepared"].startswith("2026-09-16T08:00")
    assert dispense["performer"][0]["actor"]["display"] == demo.COURIER_NAME
    assert dispense["extension"][0]["valueDateTime"].startswith("2026-09-16T08:30")


def test_the_eta_the_nurse_sees_is_the_instant_the_record_stores(go):
    """Two renderings of one instant. A lock screen wants ``08:30``; the resource wants a
    datetime. Deriving them separately is how they come to disagree."""
    run = go("clean")
    assert run.eta == "08:30"
    assert run.dispense["extension"][0]["valueDateTime"].startswith("2026-09-16T08:30")
    assert run.alert.to_payload()["eta"] == run.eta


def test_the_dispense_authorises_against_the_request_that_was_validated(go, settings):
    """A dispense pointing at a request the gate never read is exactly the silent failure
    the gate exists to prevent, so the reference is asserted rather than assumed."""
    dispense = go("clean").dispense
    reference = dispense["authorizingPrescription"][0]["reference"]
    assert reference == f"MedicationRequest/{settings.ns_id('medreq-1001')}"


def test_the_dispense_carries_the_prescriptions_own_medication_concept(go):
    """Copied, not rebuilt: two CodeableConcepts meant to be the same, built twice, will
    eventually differ."""
    dispense = go("clean").dispense
    coding = dispense["medicationCodeableConcept"]["coding"]
    assert any(c["code"] == demo.DRUG_RXCUI for c in coding)


def test_the_dispense_references_the_patient_rather_than_copying_them(go):
    """A name copied onto a dispense "for the courier's convenience" is a second PHI record
    to keep access-controlled, and a second place to get it wrong."""
    subject = go("clean").dispense["subject"]
    assert subject["reference"].startswith("Patient/")
    assert demo.PATIENT_FAMILY not in json.dumps(subject)


def test_the_happy_path_leaves_four_audit_events(go):
    assert [e.subtype for e in go("clean").events] == [
        "order-received",
        "formulation-verified",
        "dispense-written",
        "notification-sent",
    ]


def test_the_notification_is_delivered_and_recorded(go):
    notifier = InMemoryNotifier()
    run = go("clean", notifier=notifier)
    assert run.delivery.delivered is True
    assert len(notifier.deliveries) == 1
    assert notifier.deliveries[0].token == run.token


# -- structural safety properties -------------------------------------------------------


def test_a_hold_writes_no_dispense_at_all(go):
    """Not "a dispense with the wrong status" — no dispense.

    The gate returns ``prescription=None`` on a hold, so the pipeline has no value from which
    to build one. This asserts the consequence rather than the mechanism.
    """
    run = go("strength_mismatch")
    assert run.status == "held"
    assert run.dispense is None
    assert run.alert is None


@pytest.mark.parametrize("name", ["strength_mismatch", "dose_form_mismatch", "unresolvable"])
def test_a_clinical_hold_is_never_reported_as_an_error(go, name):
    """A hold is a finding; an error is an outage. An API consumer that cannot tell them
    apart sends an operator to investigate the wrong thing."""
    run = go(name)
    assert run.status == "held"
    assert run.error is None
    assert run.code in HOLD_CODES


def test_a_held_indent_leaves_a_two_event_trail(go):
    run = go("strength_mismatch")
    assert [e.subtype for e in run.events] == ["order-received", "formulation-held"]
    assert run.events[1].outcome == build.OUTCOME_HOLD


def test_nothing_is_notified_when_the_gate_holds(go):
    notifier = InMemoryNotifier()
    go("strength_mismatch", notifier=notifier)
    assert notifier.deliveries == []


def test_an_infrastructure_failure_is_an_error_not_a_clinical_hold(rxnav, settings):
    """A FHIR outage must not become ``order_correlation_absent`` in a permanent record.

    The trail still carries what was observed before the failure — which is why the indent
    is recorded as received before any check runs.
    """
    run = _drive(fixtures.er7("clean"), rxnav=rxnav, settings=settings, fhir=BrokenFhir())
    assert run.status == "error"
    assert run.dispense is None
    assert run.code == "processing_error"
    assert "not a clinical rejection" in run.detail
    assert [e.subtype for e in run.events] == ["order-received", "formulation-held"]


def test_an_outage_detail_does_not_republish_the_exception_message(rxnav, settings):
    """The exception's message quotes a PID segment. The pipeline publishes the *type* — the
    part that makes a failure diagnosable and carries no payload — and nothing else."""
    run = _drive(fixtures.er7("clean"), rxnav=rxnav, settings=settings, fhir=BrokenFhir())
    blob = json.dumps(run.public())
    assert demo.MRN not in blob
    assert demo.PATIENT_FAMILY not in blob
    assert "PID" not in blob
    assert BrokenFhir.MESSAGE not in blob
    # The type is deliberately published: it is the diagnosis, not the data.
    assert "RuntimeError" in blob


def test_a_failed_notification_does_not_undo_the_dispense(go):
    """The medicine is on its way whether or not the phone buzzed. Unwinding the dispense
    would make the record disagree with the ward."""
    notifier = InMemoryNotifier(fail_next=1)
    run = go("clean", notifier=notifier)
    assert run.status == "dispensed"
    assert run.dispense is not None
    assert run.delivery.delivered is False
    assert run.events[-1].subtype == "notification-sent"
    assert run.events[-1].outcome == build.OUTCOME_HOLD


def test_an_audit_write_failure_is_visible_to_the_caller(rxnav, settings):
    """A completed dispense must not imply its AuditEvents reached the record."""
    dataset = seed.build_dataset(settings)
    transport = AuditRejectingTransport(dataset.resources())
    with FhirClient("https://hapi.fhir.org/baseR4", transport=transport) as fhir:
        run = process(
            fixtures.er7("clean"),
            fhir=fhir,
            rxnav=rxnav,
            notifier=InMemoryNotifier(),
            settings=settings,
            courier=courier_from(dataset),
            now=NOW,
        )

    assert run.status == "dispensed"  # the handoff already happened
    assert len(run.audit_problems) == len(run.events)
    assert run.public()["audit_recorded"] is False
    assert "audit_problems" not in run.public()


def test_a_payload_that_is_not_the_allowlist_is_refused_at_the_boundary():
    """The one failure that must be loud. A payload carrying an extra key means PHI was about
    to reach a phone, which is a defect in this program rather than a condition to report —
    so it raises instead of returning a tidy error status.
    """

    class Polluted:
        """Stands in for a future edit that widens ``DispatchAlert`` without widening the
        tests. Nothing else can produce this shape."""

        def to_payload(self):
            return {
                **phi.alert_from(
                    item_description="insulin glargine",
                    quantity="1 pen",
                    courier_display_name=demo.COURIER_NAME,
                    eta="08:30",
                ).to_payload(),
                "patient_name": f"{demo.PATIENT_FAMILY}, {demo.PATIENT_GIVEN}",
            }

    with pytest.raises(phi.PrivacyViolation):
        InMemoryNotifier().send(Polluted(), token="t")


# -- the privacy property, over every path ----------------------------------------------


@pytest.mark.parametrize("name", sorted(fixtures.FIXTURES))
def test_no_fixture_leaks_a_patient_value_into_the_public_view(go, name):
    """The central claim, asserted against every fixture rather than only the one that
    succeeds. ``test_the_internal_view_would_have_leaked`` is its anti-vacuity control."""
    blob = json.dumps(go(name).public())
    for value in outbound_forbidden(name):
        assert value not in blob, f"{name}: {value!r} reached the outbound view"


@pytest.mark.parametrize("name", sorted(fixtures.FIXTURES))
def test_the_internal_view_would_have_leaked(go, name):
    """Anti-vacuity: the same fixture, read internally, *does* carry the patient. Without
    this, a pipeline that carried nothing anywhere would pass the test above."""
    blob = json.dumps(go(name).internal())
    assert demo.MRN in blob
    assert demo.PATIENT_FAMILY in blob
    # And the linkable value specifically, so `outbound_forbidden` cannot quietly grow an
    # entry for something that was never present to begin with.
    assert fixtures.FIXTURES[name].message_control_id in blob


def test_the_alert_carries_exactly_the_allowlist(go):
    assert set(go("clean").alert.to_payload()) == set(ALERT_FIELDS)


def test_the_alert_keys_are_the_dispense_fields_that_may_cross_and_no_others(go):
    """Named individually, so that widening the allowlist is a deliberate edit to this list
    rather than a diff nobody reads."""
    assert set(go("clean").alert.to_payload()) == {
        "item_description",
        "quantity",
        "courier_display_name",
        "eta",
    }


def test_the_internal_view_and_the_alert_disagree_about_the_patient(go):
    """The contrast the UI renders, asserted as a property of the two view models rather
    than only of the rendered page."""
    run = go("clean")
    internal = run.internal()
    assert internal["patient"]["mrn"] == demo.MRN
    assert demo.MRN not in json.dumps(run.public())


def test_the_token_is_a_handle_and_not_an_identifier(go):
    """The phone must be able to name the delivery without holding anything linkable to the
    patient — a plain digest of a sequential order number would be reversible."""
    run = go("clean")
    assert run.token
    assert run.token != demo.PLACER_ORDER_NUMBER
    for value in PATIENT_VALUES:
        assert value not in run.token


def test_the_same_order_yields_the_same_token(go):
    """Stability matters: a re-sent alert must be recognisable as the same delivery, which a
    random per-send value could not achieve."""
    assert go("clean").token == go("clean").token


# -- the audit trail --------------------------------------------------------------------


def test_the_chain_verifies(go):
    run = go("clean")
    assert run.chain_intact is True
    assert verify_chain([e.entry for e in run.events]) == []


def test_editing_an_event_breaks_its_own_link(go):
    """Tamper-**evident**: the test has to actually alter something, or it asserts the happy
    path twice and proves nothing."""
    entries = [e.entry for e in go("clean").events]
    tampered = replace(entries[1], payload={**entries[1].payload, "outcomeDesc": "all fine"})
    assert verify_chain([entries[0], tampered, *entries[2:]])


def test_removing_an_event_breaks_the_link_that_followed_it(go):
    entries = [e.entry for e in go("clean").events]
    assert verify_chain([entries[0], *entries[2:]])


def test_the_trail_reaches_the_server_and_reads_back_verifiable(rxnav, settings):
    """The property that makes this evidence rather than a private log: verification needs
    nothing but the server. Rebuild the chain from the written resources alone and check it.
    """
    dataset = seed.build_dataset(settings)
    transport = RecordingTransport(dataset.resources())
    with FhirClient("https://hapi.fhir.org/baseR4", transport=transport) as fhir:
        run = process(
            fixtures.er7("clean"),
            fhir=fhir,
            rxnav=rxnav,
            notifier=InMemoryNotifier(),
            settings=settings,
            courier=courier_from(dataset),
            now=NOW,
        )

    assert run.status == "dispensed"
    written = [body for path, body in transport.put if "/AuditEvent/" in path]
    assert len(written) == len(run.events) == 4

    rebuilt = [build.chain_entry(resource) for resource in written]
    assert all(entry is not None for entry in rebuilt)
    assert verify_chain(rebuilt) == []


def test_the_server_copy_carries_the_same_hashes_as_the_run(rxnav, settings):
    """A trail that verifies in memory but differs on the server is worse than no trail."""
    dataset = seed.build_dataset(settings)
    transport = RecordingTransport(dataset.resources())
    with FhirClient("https://hapi.fhir.org/baseR4", transport=transport) as fhir:
        run = process(
            fixtures.er7("clean"),
            fhir=fhir,
            rxnav=rxnav,
            notifier=InMemoryNotifier(),
            settings=settings,
            courier=courier_from(dataset),
            now=NOW,
        )
    written = [body for path, body in transport.put if "/AuditEvent/" in path]
    stored = [build.chain_entry(r).hash for r in written]
    assert stored == [e.entry.hash for e in run.events]


def test_audit_ids_are_stable_across_runs(go):
    """Ids derive from the message control id and the sequence, so re-running a message
    replaces its events instead of growing the trail with duplicates."""
    assert [e.entry.sequence for e in go("clean").events] == [1, 2, 3, 4]
    assert [e.entry.sequence for e in go("clean").events] == [1, 2, 3, 4]
    assert go("clean").events[0].entry.prev_hash == "0" * 64


def test_the_trail_names_the_prescriber_once_the_request_has_been_read(go, settings):
    """``agent.who`` answers "who is accountable". The first event precedes that knowledge,
    so it is attributed to the service rather than to a person it has not met yet.

    ``Organization`` and not ``Practitioner``: a clinician reference here would be inventing
    a clinician. It also has to resolve — HAPI rejects an AuditEvent naming a resource it
    does not hold, so a made-up reference means the event is never written at all.
    """
    agents = [
        e.entry.payload["agent"][0]["who"]["reference"] for e in go("clean").events
    ]
    assert agents[0] == f"Organization/{settings.ns_id(SYSTEM_AGENT_LOCAL_ID)}"
    assert agents[1] == f"Practitioner/{settings.ns_id('practitioner-prescriber')}"
    assert len(set(agents)) == 2


def test_every_agent_reference_resolves_on_the_seeded_server(go, dataset):
    """The regression pin for the ``Practitioner/coldchain-pipeline`` defect.

    That reference did not exist, so HAPI refused every event that used it and a hold left
    no server-side trail — while this suite passed, because nothing checked references. The
    seeded dataset is now the authority: an agent the trail names must be a resource the
    seeder actually writes.
    """
    held = {"Patient", "Practitioner", "Organization", "Location"}
    seeded = {f"{r['resourceType']}/{r['id']}" for r in dataset.resources() if r["resourceType"] in held}

    for name in sorted(fixtures.FIXTURES):
        for event in go(name).events:
            ref = event.entry.payload["agent"][0]["who"]["reference"]
            assert ref in seeded, f"{name}: {ref} is not a seeded resource"


def test_the_hold_detail_reaches_the_audit_trail(go):
    run = go("strength_mismatch")
    assert run.events[-1].entry.payload["outcomeDesc"] == run.detail
    assert run.code == "strength_mismatch"


def test_a_hold_is_recorded_as_a_serious_failure(go):
    """Every hold means a medication was not dispensed and a human must follow up.
    Downplaying a strength mismatch as a minor failure is not a judgement this system
    should make."""
    assert go("strength_mismatch").events[-1].outcome == build.OUTCOME_HOLD


# -- malformed input --------------------------------------------------------------------


def test_an_unparseable_message_holds_rather_than_raising(rxnav, settings):
    run = _drive("this is not an HL7 message", rxnav=rxnav, settings=settings)
    assert run.status == "held"
    assert run.code == HoldCode.UNPARSEABLE_INDENT
    assert run.dispense is None
    # The parser's own message quotes the segment it choked on, so it is not carried.
    assert "this is not an HL7 message" not in json.dumps(run.public())


def test_a_message_that_fails_mid_parse_still_leaves_evidence_it_arrived(rxnav, settings):
    run = _drive("MSH|^~\\&|WARD", rxnav=rxnav, settings=settings)
    assert run.status == "held"
    assert [e.subtype for e in run.events] == ["order-received"]
    assert run.chain_intact is True


def test_an_unparseable_message_is_filed_on_the_server_too(rxnav, settings):
    """The one outcome that used to reach the response but never the server.

    A message that will not parse is exactly what an auditor goes looking for, and it was
    the single branch that returned before filing its events — so the trail existed only in
    memory and vanished with the process. This test is the pin: it asserts the AuditEvent is
    on the (fake) server, by the id the pipeline derived for it.

    There is no message control id to build that id from, so the stem is a digest of the raw
    bytes. ``test_the_unparseable_stem_is_a_digest_and_not_the_message`` covers the reason
    that is not a disclosure.
    """
    er7 = "this is not an HL7 message"
    transport = RecordingTransport(seed.build_dataset(settings).resources())
    with FhirClient("https://hapi.fhir.org/baseR4", transport=transport) as client:
        run = _drive(er7, rxnav=rxnav, settings=settings, fhir=client)

    assert run.status == "held"
    assert run.audit_problems == [], "the event did not reach the server"

    stem = f"unparsed-{hashlib.sha256(er7.encode()).hexdigest()[:12]}"
    filed = [r for path, r in transport.put if settings.ns_id(f"audit-{stem}-1") in path]
    assert len(filed) == 1, f"expected exactly one AuditEvent, wrote: {[p for p, _ in transport.put]}"
    assert filed[0]["outcome"] == build.OUTCOME_HOLD
    # And it carries no patient data and does not quote the message it could not read.
    assert er7 not in json.dumps(filed[0])
    for value in PATIENT_VALUES:
        assert value not in json.dumps(filed[0])


def test_the_unparseable_stem_is_a_digest_and_not_the_message():
    """Anti-vacuity for the id the test above pins.

    The stem must not be the message, and must not be something that can be read back into
    it — the ER7 is the PHI-bearing artifact, and a stem that carried it would put patient
    data in a server-side resource id where anyone with sandbox access could read it.
    """
    from app.pipeline import _audit_key

    er7 = f"MSH|^~\\&|WARD|||PID|||{demo.MRN}||{demo.PATIENT_FAMILY}^{demo.PATIENT_GIVEN}"
    stem = _audit_key(None, er7)
    assert stem.startswith("unparsed-")
    assert er7 not in stem
    assert demo.MRN not in stem
    assert demo.PATIENT_FAMILY not in stem
    # Deterministic, so re-processing the same bad message replaces its event rather than
    # accumulating duplicates — the same idempotency the control-id path gets.
    assert stem == _audit_key(None, er7)
    assert stem != _audit_key(None, er7 + " ")
    # When there *is* an order, the control id is still used: that path is unchanged.
    order = parse_indent(fixtures.er7("clean"))
    assert _audit_key(order, fixtures.er7("clean")) == order.message_control_id


def test_the_parser_rejects_the_input_this_test_relies_on():
    """Anti-vacuity for the two tests above: they only mean something if the input really
    does fail to parse."""
    with pytest.raises(IndentParseError):
        parse_indent("this is not an HL7 message")


# -- the public/internal split ----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(fixtures.FIXTURES))
def test_the_public_view_is_json_serialisable(go, name):
    json.dumps(go(name).public())


def test_the_public_view_reports_the_delivery_outcome(go):
    view = go("clean").public()
    assert view["status"] == "dispensed"
    assert view["notified"] is True
    assert view["token"]


def test_the_public_view_withholds_the_fhir_ids_that_would_resolve_the_patient(go):
    """A dispense id derived from the filler order number is a linkable identifier, so it
    belongs on the server and in the operator's view — not in what leaves the process."""
    run = go("clean")
    assert run.dispense["id"]  # it exists...
    assert "dispense_id" not in run.public()  # ...and is not published
    assert run.internal()["dispense_id"] == run.dispense["id"]


def test_the_public_audit_summary_omits_the_resource_references(go):
    """Same reason: `MedicationDispense/…-RX-1001` carries the order number."""
    run = go("clean")
    assert "entities" not in run.public()["audit"][-1]
    assert run.internal()["chain"][-1]["entities"]


def test_a_held_public_view_carries_no_dispense_and_no_alert(go):
    view = go("strength_mismatch").public()
    assert view["status"] == "held"
    assert "dispense_id" not in view
    assert "alert" not in view
    assert "token" not in view  # nothing was dispatched, so there is nothing to correlate


def test_public_hold_and_audit_summary_redact_order_identifiers(rxnav, settings):
    """A correlation failure must not place its inbound keys in a response or AuditEvent."""
    placer = "PLACER-PRIVATE-001"
    filler = "FILLER-PRIVATE-001"
    er7 = fixtures.render(
        replace(
            fixtures.FIXTURES["clean"],
            placer_order_number=placer,
            filler_order_number=filler,
        )
    )
    run = _drive(er7, rxnav=rxnav, settings=settings)

    assert run.status == "held"
    for serialised in (
        json.dumps(run.public()),
        json.dumps(run.events[-1].entry.payload),
    ):
        assert placer not in serialised
        assert filler not in serialised


def test_response_body_has_no_request_identifier(go):
    """The HTTP adapter cannot add a correlation identifier back into the public view."""
    response = response_for(go("clean"))
    assert "request_id" not in response.body.decode()


def test_the_public_view_publishes_digests_but_not_hashed_payloads(go):
    """The audit summary is safe to publish — digests and dimensions. The hashed payload is
    not, because it carries entity references and the validator's own detail string."""
    event = go("clean").public()["audit"][0]
    assert event["hash"]
    assert "payload" not in event


def test_an_error_response_never_echoes_the_message_by_default():
    view = phi.error_view(RuntimeError(f"choked on PID|||{demo.MRN}"), known=(demo.MRN,))
    assert view["error"] == "RuntimeError"
    assert demo.MRN not in json.dumps(view)


def test_the_notifier_protocol_covers_the_token():
    """``token_for`` is part of what a notifier *is*, not an extra on the demo adapter: a
    transport that could not mint a handle could not satisfy the privacy boundary."""
    assert hasattr(InMemoryNotifier(), "token_for")


# -- defaults that fail closed ----------------------------------------------------------


def test_a_run_that_never_ran_does_not_present_itself_as_a_success():
    run = Run()
    assert run.status == "error"
    assert run.code == "processing_error"
    assert run.chain_intact is True  # an empty chain is trivially intact


def test_only_a_pass_produces_a_prescription():
    """The invariant the fail-closed argument rests on, asserted where it is defined."""
    held = Verdict(outcome=ValidationOutcome.hold(HoldCode.STRENGTH_MISMATCH, "x"))
    assert held.ok is False
    assert held.prescription is None


def test_the_courier_comes_from_the_caller_not_the_message(settings):
    """A nurse's indent says what to send, not who carries it. Deriving a courier from the
    message would put a name in the record that nobody assigned."""
    dataset = seed.build_dataset(settings)
    courier = courier_from(dataset)
    assert isinstance(courier, Courier)
    assert courier.practitioner_id == dataset.courier["id"]
    assert courier.destination_id == dataset.ward["id"]
