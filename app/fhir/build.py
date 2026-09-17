"""FHIR R4 payload builders.

Plain dicts rather than the ``fhir.resources`` package: modern releases of that library
ship R4B and STU3 models but no R4 sub-package (R4 models last existed in the flat 4.0.0
layout, and v7 is where R4B was *added*), while the brief links the R4 specification.
Field names below are read off ``hl7.org/fhir/R4``.

Every builder is a pure function of its arguments — no clock, no globals — so payload
contracts can be asserted in tests without a server. Callers pass timestamps in.

Coding systems are declared rather than invented. RxNorm is the standard system for the
drug; the order-identifier and courier-function systems are this system's own and are
namespaced under ``urn:dna:coldchain:``. Where no standard code applies, the builder
carries ``text`` and omits ``coding`` rather than synthesising a plausible-looking code —
see the dose-strength Quantity and the route below.
"""

from __future__ import annotations

from typing import Any

from app.audit.chain import ChainEntry

JSON = dict[str, Any]

#: Standard FHIR system URI for RxNorm concepts.
RXNORM_SYSTEM = "http://www.nlm.nih.gov/research/umls/rxnorm"

#: This system's identifier namespaces. ``urn:`` form so they are unambiguously local.
SYSTEM_MRN = "urn:dna:coldchain:mrn"
SYSTEM_PLACER_ORDER = "urn:dna:coldchain:placer-order-number"
SYSTEM_FILLER_ORDER = "urn:dna:coldchain:filler-order-number"
SYSTEM_STAFF_ID = "urn:dna:coldchain:staff-id"

#: R4 has no native "expected delivery time" field on MedicationDispense, and the R4
#: extension registry contains nothing suitable (checked: the only "delivery" entries are
#: ISO 21090 address parts). So the ETA is carried in a locally-defined extension. The
#: standards-native alternative would be a separate SupplyDelivery resource, whose
#: ``occurrence[x]`` means exactly this; see WRITEUP.md for why we did not take it.
ETA_EXTENSION_URL = "urn:dna:coldchain:StructureDefinition:expected-delivery-time"

#: Local code system for the role a performer plays in a dispense.
COURIER_FUNCTION_SYSTEM = "urn:dna:coldchain:CodeSystem:dispense-performer-function"

#: Where the audit-chain fields live on an AuditEvent. R4 has no extension for a hash chain
#: (checked: the registry's only related entry is the separate `Provenance` resource, which
#: records how a resource came to be rather than chaining a log), so this is declared local.
AUDIT_CHAIN_EXTENSION_URL = "urn:dna:coldchain:StructureDefinition:audit-chain"

#: This system's own identity, as recorded in AuditEvent.source.
SYSTEM_OBSERVER = "urn:dna:coldchain:system-id"
SYSTEM_OBSERVER_VALUE = "coldchain-dispense-pipeline"

#: R4 bindings we can cite. `audit-event-type` and `audit-event-outcome` are the standard
#: terminologies for these two fields; the subtype is ours, because no standard code
#: describes "this gate validated a cold-chain indent".
#:
#: There is no ``AUDIT_OUTCOME_SYSTEM`` constant, and that is not an omission. R4 declares
#: ``AuditEvent.outcome`` as a primitive ``code`` (0..1) with a *required* binding to the
#: AuditEventOutcome value set — not a ``Coding``, so there is nowhere to put a system URI;
#: the binding supplies it. Measured the hard way: sending a ``Coding`` here is silently
#: discarded by HAPI (201, field absent on read-back), which would have left every audit
#: record claiming no outcome at all while looking successful.
AUDIT_EVENT_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/audit-event-type"
AUDIT_SUBTYPE_SYSTEM = "urn:dna:coldchain:CodeSystem:audit-subtype"
PARTICIPATION_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ParticipationType"

#: AuditEvent.action. "E" (Execute) — the gate executed a validation, which is what the
#: event records. Not "C": creating a resource is what a successful run does next.
AUDIT_ACTION_EXECUTE = "E"

#: DICOM outcome codes, for ``AuditEvent.outcome``. A primitive ``code`` — see the note on
#: ``AUDIT_EVENT_TYPE_SYSTEM`` above. Any hold is "8" (Serious failure) rather than "4"
#: (Minor): every hold means a medication was *not* dispensed and a human must follow up.
#: Downplaying a strength mismatch as a minor failure is not a judgement this system should
#: make.
OUTCOME_SUCCESS = "0"
OUTCOME_HOLD = "8"

#: AuditEvent fields that are covered by the chain hash.
#:
#: Symmetry with `chain_entry()` is the load-bearing part: the payload hashed at write time
#: is embedded verbatim in the resource, so a verifier can recompute it from the server
#: alone without trusting anything this process did. `test_audit_round_trip` pins that.
HASHED_AUDIT_FIELDS: tuple[str, ...] = (
    "type",
    "subtype",
    "action",
    "recorded",
    "outcome",
    "outcomeDesc",
    "agent",
    "source",
    "entity",
)

#: MedicationDispense.status for "packed, handed to a courier, not yet delivered".
#:
#: R4's definitions: ``preparation`` means staging has begun but the core event has not
#: started; ``in-progress`` means the dispensed product is ready for pickup. Once a courier
#: is carrying it the handover *has* happened, so the core event has started and
#: ``in-progress`` is the honest value. ``preparation`` would contradict setting
#: ``whenHandedOver``. Neither value means "delivered" — that would be ``completed``, and
#: this system never observes delivery.
STATUS_IN_PROGRESS = "in-progress"


# -- parties ---------------------------------------------------------------------------


def patient(
    *,
    resource_id: str,
    mrn: str,
    family: str,
    given: str,
    gender: str | None = None,
    birth_date: str | None = None,
) -> JSON:
    """A Patient carrying the MRN the HL7 PID-3 segment will be matched on."""
    resource: JSON = {
        "resourceType": "Patient",
        "id": resource_id,
        "identifier": [{"system": SYSTEM_MRN, "value": mrn}],
        "name": [{"family": family, "given": [given]}],
    }
    if gender:
        resource["gender"] = gender
    if birth_date:
        resource["birthDate"] = birth_date
    return resource


def practitioner(
    *,
    resource_id: str,
    family: str,
    given: str,
    staff_id: str | None = None,
) -> JSON:
    """A Practitioner.

    Used for both the prescriber and the courier. Workforce identity is deliberately *not*
    treated as PHI by the alert scrubber — see ``app/safety/phi.py`` — so the courier has a
    real display name here.
    """
    resource: JSON = {
        "resourceType": "Practitioner",
        "id": resource_id,
        "name": [{"family": family, "given": [given]}],
    }
    if staff_id:
        resource["identifier"] = [{"system": SYSTEM_STAFF_ID, "value": staff_id}]
    return resource


def location(*, resource_id: str, name: str) -> JSON:
    """A ward/floor Location — the cold chain's destination."""
    return {
        "resourceType": "Location",
        "id": resource_id,
        "name": name,
        "operationalStatus": {
            "system": "http://terminology.hl7.org/CodeSystem/v2-0116",
            "code": "O",
            "display": "Occupied",
        },
        "mode": "instance",
    }


def organization(*, resource_id: str, name: str) -> JSON:
    """The dispensing service itself, as an auditable party.

    AuditEvent.agent.who is "who is accountable for this event", and for the events this
    pipeline records before it has correlated a prescriber — a message that would not parse,
    an order it refused on shape alone — the accountable party is the service, not a person.
    R4 types that element as ``Reference(PractitionerRole | Practitioner | Organization |
    Device | ...)``, so the service belongs there as an Organization.

    It has to be a real, seeded resource rather than a bare reference, because HAPI
    validates references on write: an ``AuditEvent`` naming a Practitioner that does not
    exist is rejected with ``HAPI-1094``. The first version of this referenced
    ``Practitioner/coldchain-pipeline`` and every hold wrote no audit trail at all against
    the sandbox — see the reference check in ``app.pipeline.InMemoryFhirTransport``, which
    now makes the offline suite catch that class of defect.
    """
    return {
        "resourceType": "Organization",
        "id": resource_id,
        "active": True,
        "name": name,
    }


# -- medication ------------------------------------------------------------------------


def medication_codeable(*, rxcui: str, display: str, ingredient_rxcui: str | None = None) -> JSON:
    """A medication as a CodeableConcept, coded in RxNorm.

    The brief's rubric penalises string matching, so the product concept is carried as a
    code. When the ingredient is known it is carried too, which is what makes a
    formulation-level comparison possible without re-parsing the display name.
    """
    coding: list[JSON] = [{"system": RXNORM_SYSTEM, "code": rxcui, "display": display}]
    if ingredient_rxcui and ingredient_rxcui != rxcui:
        coding.append({"system": RXNORM_SYSTEM, "code": ingredient_rxcui})
    return {"coding": coding, "text": display}


def medication_request(
    *,
    resource_id: str,
    placer_order_number: str,
    filler_order_number: str,
    medication: JSON,
    subject_id: str,
    requester_id: str,
    authored_on: str,
    dose_value: float | None = None,
    dose_unit: str | None = None,
    route_text: str | None = None,
    quantity_value: float | None = None,
    quantity_unit: str | None = None,
) -> JSON:
    """The prescription the indent is validated against.

    Both order numbers are carried as identifiers because the HL7 fixture supplies both
    (ORC-2 placer, ORC-3 filler) and either may be the correlation key on a real interface.
    """
    resource: JSON = {
        "resourceType": "MedicationRequest",
        "id": resource_id,
        "identifier": [
            {"system": SYSTEM_PLACER_ORDER, "value": placer_order_number},
            {"system": SYSTEM_FILLER_ORDER, "value": filler_order_number},
        ],
        "status": "active",
        "intent": "order",
        "medicationCodeableConcept": medication,
        "subject": {"reference": f"Patient/{subject_id}"},
        "authoredOn": authored_on,
        "requester": {"reference": f"Practitioner/{requester_id}"},
    }

    dosage: JSON = {}
    if route_text:
        # Text only. The R4 route binding is to SNOMED CT, and we have no verified SNOMED
        # code for "Subcutaneous" available offline, so we do not assert one.
        dosage["route"] = {"text": route_text}
    if dose_value is not None:
        dose_quantity: JSON = {"value": dose_value}
        if dose_unit:
            dose_quantity["unit"] = dose_unit
        dosage["doseAndRate"] = [{"doseQuantity": dose_quantity}]
    if dosage:
        resource["dosageInstruction"] = [dosage]

    if quantity_value is not None:
        dispense_quantity: JSON = {"value": quantity_value}
        if quantity_unit:
            dispense_quantity["unit"] = quantity_unit
        resource["dispenseRequest"] = {"quantity": dispense_quantity}

    return resource


def medication_dispense(
    *,
    resource_id: str,
    medication: JSON,
    subject_id: str,
    authorizing_request_id: str,
    packed_at: str,
    handed_over_at: str,
    eta: str,
    courier_id: str | None = None,
    courier_display_name: str | None = None,
    destination_id: str | None = None,
    destination_display: str | None = None,
    quantity_value: float | None = None,
    quantity_unit: str | None = None,
) -> JSON:
    """The write-back the brief asks for: packed, who is carrying it, and the ETA.

    Mapping onto R4, one field per thing the brief names:

    ``whenPrepared``          when it was packed.
    ``performer``             who is carrying it. ``function`` comes from a local code
                              system; R4 binds performer function to a terminology we
                              cannot cite offline, so it is declared rather than invented.
    ``extension``             the ETA, under :data:`ETA_EXTENSION_URL`.
    ``whenHandedOver``        when the courier took it.
    ``destination``           the ward it is going to.
    ``authorizingPrescription`` ties the dispense back to the MedicationRequest, which is
                              what makes the audit trail reconstructable.
    ``status``                ``in-progress`` — see :data:`STATUS_IN_PROGRESS`.

    ``subject`` is a reference to the Patient, never a copied name or MRN: the dispense is
    as much a PHI-bearing record as the request, and a reference is both smaller and already
    access-controlled by the server. ``tests/test_fhir_build.py`` asserts on the serialized
    resource, because the tempting mistake here is copying a name in "for the courier's
    convenience".
    """
    resource: JSON = {
        "resourceType": "MedicationDispense",
        "id": resource_id,
        "status": STATUS_IN_PROGRESS,
        "medicationCodeableConcept": medication,
        "subject": {"reference": f"Patient/{subject_id}"},
        "authorizingPrescription": [{"reference": f"MedicationRequest/{authorizing_request_id}"}],
        "whenPrepared": packed_at,
        "whenHandedOver": handed_over_at,
        "extension": [
            {"url": ETA_EXTENSION_URL, "valueDateTime": eta},
        ],
    }

    if destination_id:
        destination: JSON = {"reference": f"Location/{destination_id}"}
        if destination_display:
            destination["display"] = destination_display
        resource["destination"] = destination

    if courier_id:
        performer: JSON = {"actor": {"reference": f"Practitioner/{courier_id}"}}
        if courier_display_name:
            performer["actor"]["display"] = courier_display_name
        performer["function"] = {
            "coding": [
                {
                    "system": COURIER_FUNCTION_SYSTEM,
                    "code": "courier",
                    "display": "Courier",
                }
            ],
            "text": "Courier",
        }
        resource["performer"] = [performer]

    if quantity_value is not None:
        quantity: JSON = {"value": quantity_value}
        if quantity_unit:
            quantity["unit"] = quantity_unit
        resource["quantity"] = quantity

    return resource


# -- AuditEvent -------------------------------------------------------------------------


def audit_payload(
    *,
    subtype_code: str,
    subtype_display: str,
    recorded: str,
    outcome_code: str,
    outcome_desc: str,
    agent_ref: str,
    entity_refs: list[str],
) -> JSON:
    """The AuditEvent fields that the hash chain covers.

    Split out from :func:`audit_event` so the same dict is (a) hashed at write time and
    (b) reconstructable at verify time by reading these fields back off the resource. The
    two must stay in step; `chain_entry()` below is the read side, and a round-trip test
    asserts they agree.

    Two deliberate omissions:

    * ``source.type`` is not set. R4 binds it to the DICOM security-source-type code list and
      we could not verify a code offline, so we omit the field rather than assert one. The
      observer is identified explicitly instead.
    * ``agent[].type`` cites ``v3-ParticipationType`` ``AUT`` (author).

    ``agent_ref`` is a *full reference* — ``Practitioner/{id}`` for a clinician,
    ``Organization/{id}`` for the service — rather than a bare id, because the two are
    different resource types and only the caller knows which applies. When the pipeline has
    correlated a prescriber, ``who`` is that person: the audit question is "who is
    accountable for this dispense", and that is a clinician. When it has not — a message
    that would not parse, an order refused on shape before any lookup — the accountable
    party is the service, and naming a clinician there would be inventing one.

    ``outcome_desc`` is the validator's ``detail`` string. That is PHI-free by contract
    (see `app/models.ValidationOutcome`), which is what makes it safe to put in an audit
    record that is readable by anyone with sandbox access.
    """
    agent: JSON = {
        "type": {
            "coding": [
                {
                    "system": PARTICIPATION_TYPE_SYSTEM,
                    "code": "AUT",
                    "display": "author",
                }
            ]
        },
        "who": {"reference": agent_ref},
        "requestor": True,
    }

    entity: list[JSON] = [{"what": {"reference": ref}} for ref in entity_refs]

    payload: JSON = {
        "type": {
            "system": AUDIT_EVENT_TYPE_SYSTEM,
            "code": "rest",
            "display": "Restful Operation",
        },
        "subtype": [
            {
                "system": AUDIT_SUBTYPE_SYSTEM,
                "code": subtype_code,
                "display": subtype_display,
            }
        ],
        "action": AUDIT_ACTION_EXECUTE,
        "recorded": recorded,
        # A bare code string, not a Coding. R4 types this element as ``code``.
        "outcome": outcome_code,
        "outcomeDesc": outcome_desc,
        "agent": [agent],
        "source": {
            "observer": {
                "identifier": {"system": SYSTEM_OBSERVER, "value": SYSTEM_OBSERVER_VALUE},
                "display": SYSTEM_OBSERVER_VALUE,
            },
        },
    }
    if entity:
        payload["entity"] = entity
    return payload


def audit_event(*, resource_id: str, payload: JSON, entry: ChainEntry) -> JSON:
    """An AuditEvent carrying ``payload`` plus its chain link.

    The payload is embedded **verbatim** — not summarised, not re-derived — so a verifier
    holding only this resource can recompute the hash. That is what makes the trail
    checkable by a third party rather than only by us.
    """
    resource: JSON = {"resourceType": "AuditEvent", "id": resource_id}
    resource.update(payload)
    resource["extension"] = [
        {
            "url": AUDIT_CHAIN_EXTENSION_URL,
            "extension": [
                {"url": "sequence", "valuePositiveInt": entry.sequence},
                {"url": "previousHash", "valueString": entry.prev_hash},
                {"url": "hash", "valueString": entry.hash},
            ],
        }
    ]
    return resource


def _chain_sub_extension(resource: JSON) -> JSON | None:
    for extension in resource.get("extension") or []:
        if extension.get("url") == AUDIT_CHAIN_EXTENSION_URL:
            return extension
    return None


def chain_entry(resource: JSON) -> ChainEntry | None:
    """Read a chain link back out of an AuditEvent. ``None`` if it carries none.

    The inverse of :func:`audit_event`, and the reason verification can run against
    resources fetched from the server: the hashed payload is rebuilt from the very fields
    that were hashed, so a mismatch means the content changed rather than that the reader
    guessed the wrong shape.
    """
    extension = _chain_sub_extension(resource)
    if extension is None:
        return None

    parts: dict[str, Any] = {}
    for sub in extension.get("extension") or []:
        name = sub.get("url")
        if name not in ("sequence", "previousHash", "hash"):
            continue
        # FHIR puts the value under a type-named key (``valuePositiveInt``, ``valueString``),
        # not under a plain ``value``. Reading ``sub[name]`` finds nothing and yields a chain
        # entry of all-None, which would look like tampering rather than a reader bug.
        for key, value in sub.items():
            if key.startswith("value"):
                parts[name] = value
                break
    if not {"sequence", "previousHash", "hash"} <= parts.keys():
        return None

    payload = {name: resource[name] for name in HASHED_AUDIT_FIELDS if name in resource}
    return ChainEntry(
        sequence=int(parts["sequence"]),
        prev_hash=str(parts["previousHash"]),
        hash=str(parts["hash"]),
        payload=payload,
    )
