"""Payload contract tests for `app.fhir.build`.

Offline and pure: `build_dataset` and the builders take no clock and do no I/O, so these
run in the default suite with no network.

The tests that carry weight here are the last two. The brief's fifth requirement is that
nothing leaving the system carries PHI, and the easiest place to break that is not the
alert — it is the dispense write-back, where copying a patient name "for convenience" is
the obvious thing to do. ``test_dispense_carries_no_copied_phi`` is the guard.
"""

from __future__ import annotations

import json

from app import demo
from app.config import Settings
from app.fhir import build, seed

SETTINGS = Settings(demo_namespace="test-ns")


def _medication() -> dict:
    return build.medication_codeable(rxcui=demo.DRUG_RXCUI, display=demo.DRUG_DISPLAY)


# -- MedicationRequest -----------------------------------------------------------------


def test_medication_request_carries_both_order_identifiers() -> None:
    request = seed.build_dataset(SETTINGS).request
    by_system = {i["system"]: i["value"] for i in request["identifier"]}
    assert by_system[build.SYSTEM_PLACER_ORDER] == demo.PLACER_ORDER_NUMBER
    assert by_system[build.SYSTEM_FILLER_ORDER] == demo.FILLER_ORDER_NUMBER


def test_medication_request_has_r4_required_elements() -> None:
    request = seed.build_dataset(SETTINGS).request
    # R4 MedicationRequest requires exactly these four, plus medication[x].
    for field in ("status", "intent", "subject", "medicationCodeableConcept"):
        assert request.get(field), f"missing required element {field}"
    assert request["status"] == "active"
    assert request["intent"] == "order"


def test_medication_is_coded_in_rxnorm_not_just_named() -> None:
    """The rubric penalises string matching; the payload must not force it."""
    coding = _medication()["coding"]
    rxnorm = [c for c in coding if c["system"] == build.RXNORM_SYSTEM]
    assert rxnorm, "medication carried no RxNorm coding"
    assert rxnorm[0]["code"] == demo.DRUG_RXCUI


def test_ids_are_namespaced() -> None:
    dataset = seed.build_dataset(SETTINGS)
    for resource in dataset.resources():
        assert resource["id"].startswith("test-ns-"), resource["id"]


def test_subject_references_the_seeded_patient_rather_than_a_bare_id() -> None:
    """A bare id in `subject` is invalid R4; it must be a Reference."""
    dataset = seed.build_dataset(SETTINGS)
    assert dataset.request["subject"] == {"reference": f"Patient/{dataset.patient['id']}"}


# -- MedicationDispense ----------------------------------------------------------------


def _dispense() -> dict:
    dataset = seed.build_dataset(SETTINGS)
    return build.medication_dispense(
        resource_id="test-ns-dispense-1",
        medication=_medication(),
        subject_id=dataset.patient["id"],
        authorizing_request_id=dataset.request["id"],
        packed_at="2026-09-16T09:00:00Z",
        handed_over_at="2026-09-16T09:10:00Z",
        eta="2026-09-16T09:45:00Z",
        courier_id="test-ns-practitioner-courier",
        courier_display_name="M. OKAFOR",
        destination_id=dataset.ward["id"],
        destination_display="3 West",
        quantity_value=1,
        quantity_unit="pen",
    )


def test_dispense_answers_the_three_things_the_brief_asks_for() -> None:
    """packed / who is carrying it / ETA."""
    dispense = _dispense()

    assert dispense["status"] == build.STATUS_IN_PROGRESS  # dispatched, not delivered
    assert dispense["whenPrepared"] == "2026-09-16T09:00:00Z"  # packed
    assert dispense["whenHandedOver"] == "2026-09-16T09:10:00Z"

    assert dispense["performer"][0]["actor"]["display"] == "M. OKAFOR"  # carrier
    assert dispense["performer"][0]["function"]["coding"][0]["code"] == "courier"

    eta = [e for e in dispense["extension"] if e["url"] == build.ETA_EXTENSION_URL]
    assert len(eta) == 1, "ETA extension missing"
    assert eta[0]["valueDateTime"] == "2026-09-16T09:45:00Z"


def test_status_is_in_progress_not_preparation() -> None:
    """``preparation`` would contradict ``whenHandedOver`` being set.

    R4 defines ``preparation`` as staging-that-has-not-started, and ``in-progress`` as the
    product being ready. A courier holding the item means the handover happened.
    """
    assert build.STATUS_IN_PROGRESS == "in-progress"
    assert _dispense()["status"] != "preparation"


def test_dispense_names_the_destination_ward() -> None:
    dataset = seed.build_dataset(SETTINGS)
    assert _dispense()["destination"] == {
        "reference": f"Location/{dataset.ward['id']}",
        "display": "3 West",
    }


def test_dispense_links_back_to_the_prescription() -> None:
    """Without this the audit trail cannot be reconstructed from the dispense."""
    dataset = seed.build_dataset(SETTINGS)
    dispense = _dispense()
    assert dispense["authorizingPrescription"] == [
        {"reference": f"MedicationRequest/{dataset.request['id']}"}
    ]


def test_eta_extension_is_declared_local_not_dressed_up_as_official() -> None:
    """R4 has no native delivery-time field and no official extension for one.

    Asserting the URN form keeps anyone from later "tidying" this into an
    hl7.org URL, which would claim conformance to an extension that does not exist.
    """
    assert build.ETA_EXTENSION_URL.startswith("urn:dna:coldchain:")
    assert "hl7.org" not in build.ETA_EXTENSION_URL


def test_dispense_carries_no_copied_phi() -> None:
    """The write-back must reference the patient, never restate them.

    Serialising the whole resource is the point: this catches PHI added anywhere in the
    payload, including in a field a future edit invents.
    """
    serialized = json.dumps(_dispense())
    for phi in (demo.MRN, demo.PATIENT_FAMILY, demo.PATIENT_GIVEN, "1974-03-02"):
        assert phi not in serialized, f"{phi!r} leaked into the MedicationDispense"
