"""Tests for the safety gate — the decision between an indent and a dispense.

Entirely offline. The FHIR side runs against an in-memory server built from the project's
own `seed.build_dataset`, and the RxNorm side replays **recorded real responses**
(`tests/data/rxnav_responses.json`). So the graded behaviour — code lookup rather than
string matching — is exercised for real, with no network.

The tests that carry the most weight are the failure paths. A gate that only ever passes is
not a safety check, and the brief's whole fifth requirement is that a rejected order
produces *no* dispense and a *recorded* reason. Three of them are pinned here specifically
because they are the ones that are tempting to get wrong:

* ``test_a_strength_mismatch_holds`` — the clinically dangerous case: right drug, wrong
  strength. It must not pass just because the ingredient and dose form agree.
* ``test_unreadable_strength_holds_rather_than_crashing`` — the message is *parseable*; the
  gate must turn it into an auditable hold rather than an exception, because an exception
  leaves no trail.
* ``test_incomparable_units_hold_rather_than_matching`` — "100 MG/ML" vs "100 UNT/ML" has
  equal values and must not be reported as a matched strength.
"""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from app import demo
from app.fhir import build
from app.hl7 import fixtures
from app.hl7.parse import parse_indent
from app.models import ALERT_FIELDS, HoldCode
from app.safety import validate


def _order(name: str, **spec_overrides):
    """Parse a named fixture, optionally with its spec altered."""
    spec = fixtures.FIXTURES[name]
    er7 = fixtures.render(replace(spec, **spec_overrides)) if spec_overrides else fixtures.er7(name)
    return parse_indent(er7)


def _order_with_broken_strength(value: str = "abc"):
    """An indent whose RXO-18 is not a number.

    Built by editing the rendered ER7 rather than through `IndentSpec`, because hl7apy
    *refuses to render* it: RXO-18 is typed NM, so assigning a non-numeric value raises at
    build time. That asymmetry is the point — a sender can emit this message even though a
    conformant builder cannot, which is exactly the case TOLERANT parsing exists to handle.
    The same technique is used in `test_hl7_parse.py`.
    """
    er7 = fixtures.er7("clean").replace("|100|UNT/ML|", f"|{value}|UNT/ML|")
    assert value in er7, "the strength substitution did not apply — fixture layout changed"
    return parse_indent(er7)


# -- valid signs of life -----------------------------------------------------------------


def test_the_clean_fixture_passes(fhir, rxnav) -> None:
    verdict = validate.evaluate(_order("clean"), fhir=fhir, rxnav=rxnav)

    assert verdict.ok, verdict.outcome.detail
    assert verdict.outcome.code == "ok"
    assert verdict.prescription is not None
    assert verdict.prescription.drug.ingredient_name == "insulin glargine"


def test_a_pass_hands_back_everything_a_dispense_needs(fhir, rxnav, dataset) -> None:
    """The pipeline gets its ids from here, not by re-deriving them.

    A dispense that referenced a request the gate never validated would be exactly the
    silent failure the gate exists to prevent, so the linkage is asserted.
    """
    verdict = validate.evaluate(_order("clean"), fhir=fhir, rxnav=rxnav)

    assert verdict.prescription is not None
    assert verdict.prescription.request_id == dataset.request["id"]
    assert verdict.prescription.patient_id == dataset.patient["id"]
    assert verdict.prescription.medication == dataset.request["medicationCodeableConcept"]


# -- the shape checks --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("not_new", HoldCode.ORDER_NOT_NEW),
        ("multi_item", HoldCode.MULTI_ITEM_ORDER),
        ("compound", HoldCode.COMPOUND_UNSUPPORTED),
    ],
)
def test_order_shapes_that_are_out_of_scope_hold(fhir, rxnav, fixture, expected) -> None:
    verdict = validate.evaluate(_order(fixture), fhir=fhir, rxnav=rxnav)
    assert verdict.outcome.code == expected
    assert not verdict.ok


def test_shape_is_checked_before_anything_else(fhir, rxnav) -> None:
    """A message that is wrong in two ways reports the reason that must be fixed first.

    The multi-item fixture also matches a valid prescription, so if the shape check ran
    after the comparison this would report a pass.
    """
    verdict = validate.evaluate(_order("multi_item"), fhir=fhir, rxnav=rxnav)
    assert verdict.outcome.code == HoldCode.MULTI_ITEM_ORDER


def test_shape_checks_need_no_network() -> None:
    """They are pure functions of the message, so they are asserted without clients."""
    assert validate.check_order_shape(_order("clean")) is None
    for fixture, code in (
        ("not_new", HoldCode.ORDER_NOT_NEW),
        ("multi_item", HoldCode.MULTI_ITEM_ORDER),
        ("compound", HoldCode.COMPOUND_UNSUPPORTED),
    ):
        outcome = validate.check_order_shape(_order(fixture))
        assert outcome is not None and outcome.code == code


# -- correlation -------------------------------------------------------------------------


def test_an_order_with_no_matching_prescription_holds(fhir_for, rxnav, dataset) -> None:
    """No correlation is a hold, never a guessed match."""
    order = _order("clean", placer_order_number="INDENT-NOT-SEEDED", filler_order_number="RX-NOPE")
    verdict = validate.evaluate(order, fhir=fhir_for(dataset.resources()), rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.ORDER_CORRELATION_ABSENT
    assert verdict.prescription is None


def test_two_prescriptions_on_one_order_number_hold(fhir_for, rxnav, dataset) -> None:
    """Ambiguity is refused rather than resolved by picking the first.

    This is also what a namespace collision on the shared sandbox looks like, which is why
    `seed.verify` treats a second hit as a failure too.
    """
    duplicate = copy.deepcopy(dataset.request)
    duplicate["id"] = f"{dataset.request['id']}-other"
    resources = [*dataset.resources(), duplicate]

    verdict = validate.evaluate(_order("clean"), fhir=fhir_for(resources), rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.ORDER_CORRELATION_AMBIGUOUS
    assert verdict.prescription is None


def test_filler_order_number_is_used_when_the_placer_is_absent(fhir, rxnav) -> None:
    """Either populated order number may be the correlation key on a real interface."""
    order = _order("clean", placer_order_number="")
    verdict = validate.evaluate(order, fhir=fhir, rxnav=rxnav)

    assert verdict.ok, verdict.outcome.detail


def test_contradictory_order_identifiers_hold_before_drug_validation(fhir, rxnav) -> None:
    """A matching placer identifier cannot override a contradictory filler identifier."""
    order = _order("clean", filler_order_number="RX-NOT-THE-PRESCRIPTION")
    verdict = validate.evaluate(order, fhir=fhir, rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.ORDER_CORRELATION_INCONSISTENT
    assert verdict.prescription is None


def test_correlation_hold_detail_contains_no_order_identifier(fhir_for, rxnav, dataset) -> None:
    """The reason is copied into an AuditEvent, so it must be safe without later redaction."""
    placer = "PLACER-PRIVATE-001"
    filler = "FILLER-PRIVATE-001"
    order = _order("clean", placer_order_number=placer, filler_order_number=filler)
    verdict = validate.evaluate(order, fhir=fhir_for(dataset.resources()), rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.ORDER_CORRELATION_ABSENT
    assert placer not in verdict.outcome.detail
    assert filler not in verdict.outcome.detail


def test_an_indent_for_a_different_patient_holds(fhir, rxnav) -> None:
    """The prescription exists and matches the drug, but it is someone else's."""
    order = _order("clean", patient_mrn="MRN-DIFFERENT-PATIENT")
    verdict = validate.evaluate(order, fhir=fhir, rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.PATIENT_MISMATCH
    assert verdict.prescription is None


def test_a_prescription_pointing_at_a_missing_patient_holds(fhir_for, rxnav, dataset) -> None:
    """"We could not check" must not be promoted to "we checked and it was fine"."""
    orphan = copy.deepcopy(dataset.request)
    orphan["subject"] = {"reference": "Patient/does-not-exist"}
    resources = [r for r in dataset.resources() if r["resourceType"] != "MedicationRequest"]
    resources.append(orphan)

    verdict = validate.evaluate(_order("clean"), fhir=fhir_for(resources), rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.PATIENT_MISMATCH
    assert verdict.prescription is None


def test_a_prescription_with_no_patient_subject_holds(fhir_for, rxnav, dataset) -> None:
    subjectless = copy.deepcopy(dataset.request)
    del subjectless["subject"]
    resources = [r for r in dataset.resources() if r["resourceType"] != "MedicationRequest"]
    resources.append(subjectless)

    verdict = validate.evaluate(_order("clean"), fhir=fhir_for(resources), rxnav=rxnav)
    assert verdict.outcome.code == HoldCode.PATIENT_MISMATCH


# -- the clinically dangerous mismatch ---------------------------------------------------


def test_a_strength_mismatch_holds(fhir, rxnav) -> None:
    """Right ingredient, right form, wrong strength — the case that must never pass.

    Everything except the strength agrees here, so a comparison that stopped after the
    ingredient check would dispense a tenfold overdose.
    """
    verdict = validate.evaluate(_order("strength_mismatch"), fhir=fhir, rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.STRENGTH_MISMATCH
    assert verdict.prescription is None
    assert "300" in verdict.outcome.detail and "100" in verdict.outcome.detail


def test_a_dose_form_mismatch_holds(fhir, rxnav) -> None:
    """Same ingredient and strength, different formulation."""
    verdict = validate.evaluate(_order("dose_form_mismatch"), fhir=fhir, rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.DOSE_FORM_MISMATCH
    assert verdict.prescription is None


def test_unresolvable_terminology_holds(fhir, rxnav) -> None:
    """A nonsense product name must be reported as unresolvable, not as a mismatch.

    Fuzzy matching always returns *something*; if the token-coverage filter were removed
    this would resolve to an unrelated product and be reported as a wrong drug, which is a
    different and misleading claim.
    """
    verdict = validate.evaluate(_order("unresolvable"), fhir=fhir, rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.TERMINOLOGY_UNRESOLVED
    assert verdict.prescription is None


def test_unreadable_strength_holds_rather_than_crashing(fhir, rxnav) -> None:
    """The message parses (TOLERANT) but the strength is not a number.

    This is the payoff for choosing TOLERANT parsing: the bad value survives far enough to
    become a recorded hold with an audit trail, instead of a transport-level rejection that
    leaves no trail at all.
    """
    verdict = validate.evaluate(_order_with_broken_strength(), fhir=fhir, rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.ORDER_DETAIL_UNREADABLE
    assert verdict.prescription is None


def test_a_missing_strength_holds(fhir, rxnav) -> None:
    order = _order("clean", strength_value="", strength_units="")
    verdict = validate.evaluate(order, fhir=fhir, rxnav=rxnav)
    assert verdict.outcome.code == HoldCode.ORDER_DETAIL_UNREADABLE


def test_incomparable_units_hold_rather_than_matching(fhir, rxnav) -> None:
    """Equal numbers on different units is not a match.

    Reporting this as a strength mismatch would be a clinical claim we have not earned: the
    values agree, and we simply cannot compare the units.
    """
    order = _order("clean", strength_units="FURLONGS")
    verdict = validate.evaluate(order, fhir=fhir, rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.UNIT_INCOMPATIBLE
    assert verdict.prescription is None


# -- the prescribed side's own failure modes ---------------------------------------------


def test_a_prescription_not_coded_in_rxnorm_holds(fhir_for, rxnav, dataset) -> None:
    """Falling back to the display text would be the string matching this design avoids."""
    uncoded = copy.deepcopy(dataset.request)
    uncoded["medicationCodeableConcept"] = {"text": demo.DRUG_DISPLAY}
    resources = [r for r in dataset.resources() if r["resourceType"] != "MedicationRequest"]
    resources.append(uncoded)

    verdict = validate.evaluate(_order("clean"), fhir=fhir_for(resources), rxnav=rxnav)

    assert verdict.outcome.code == HoldCode.TERMINOLOGY_UNRESOLVED
    assert verdict.prescription is None


def test_a_prescription_coded_in_another_system_holds(fhir_for, rxnav, dataset) -> None:
    """A SNOMED-coded or local-code prescription is not comparable here."""
    other = copy.deepcopy(dataset.request)
    other["medicationCodeableConcept"] = {
        "coding": [{"system": "http://snomed.info/sct", "code": "347100000"}]
    }
    resources = [r for r in dataset.resources() if r["resourceType"] != "MedicationRequest"]
    resources.append(other)

    verdict = validate.evaluate(_order("clean"), fhir=fhir_for(resources), rxnav=rxnav)
    assert verdict.outcome.code == HoldCode.TERMINOLOGY_UNRESOLVED


def test_medication_reference_is_resolved(fhir_for, rxnav, dataset) -> None:
    """R4 allows either medicationCodeableConcept or medicationReference.

    A request written by another system routinely uses the reference form, so assuming the
    shape our own seeder happens to write would fail against a real interface.
    """
    medication = {
        "resourceType": "Medication",
        "id": "ns-medication-1",
        "code": dataset.request["medicationCodeableConcept"],
    }
    by_reference = copy.deepcopy(dataset.request)
    del by_reference["medicationCodeableConcept"]
    by_reference["medicationReference"] = {"reference": f"Medication/{medication['id']}"}
    resources = [r for r in dataset.resources() if r["resourceType"] != "MedicationRequest"]
    resources += [by_reference, medication]

    verdict = validate.evaluate(_order("clean"), fhir=fhir_for(resources), rxnav=rxnav)
    assert verdict.ok, verdict.outcome.detail


def test_a_medication_reference_to_a_missing_resource_holds(fhir_for, rxnav, dataset) -> None:
    dangling = copy.deepcopy(dataset.request)
    del dangling["medicationCodeableConcept"]
    dangling["medicationReference"] = {"reference": "Medication/does-not-exist"}
    resources = [r for r in dataset.resources() if r["resourceType"] != "MedicationRequest"]
    resources.append(dangling)

    verdict = validate.evaluate(_order("clean"), fhir=fhir_for(resources), rxnav=rxnav)
    assert verdict.outcome.code == HoldCode.TERMINOLOGY_UNRESOLVED


# -- the detail string is a public surface ----------------------------------------------


def test_hold_details_carry_no_patient_identifiers(fhir, rxnav) -> None:
    """``detail`` is returned over the API and written into the audit trail.

    The audit record is readable by anyone with sandbox access, so a "helpful" message
    naming the patient is the leak. Checked across every hold path that a fixture can
    reach, not just one.
    """
    for fixture in ("clean", "strength_mismatch", "dose_form_mismatch", "unresolvable"):
        verdict = validate.evaluate(_order(fixture), fhir=fhir, rxnav=rxnav)
        if verdict.ok:
            continue
        for phi in (demo.MRN, demo.PATIENT_FAMILY, demo.PATIENT_GIVEN, "1974-03-02"):
            assert phi not in verdict.outcome.detail, f"{phi!r} leaked into a hold detail"


def test_a_pass_detail_names_no_patient_either(fhir, rxnav) -> None:
    verdict = validate.evaluate(_order("clean"), fhir=fhir, rxnav=rxnav)
    assert demo.MRN not in verdict.outcome.detail
    assert demo.PATIENT_FAMILY not in verdict.outcome.detail


def test_every_hold_code_is_in_the_declared_set(fhir, rxnav, fhir_for, dataset) -> None:
    """The set is closed so the API and the audit trail can be asserted against it.

    A future edit that invents a code inline, rather than adding it to `HoldCode`, would
    quietly break anything keyed on the set — so the codes reachable from the fixtures are
    collected and checked.
    """
    declared = {v for k, v in vars(HoldCode).items() if not k.startswith("_")}
    reached = set()

    for fixture in fixtures.FIXTURES:
        verdict = validate.evaluate(_order(fixture), fhir=fhir, rxnav=rxnav)
        if not verdict.ok:
            reached.add(verdict.outcome.code)

    reached.add(
        validate.evaluate(_order_with_broken_strength(), fhir=fhir, rxnav=rxnav).outcome.code
    )
    order = _order("clean", strength_units="FURLONGS")
    reached.add(validate.evaluate(order, fhir=fhir, rxnav=rxnav).outcome.code)
    order = _order("clean", patient_mrn="OTHER")
    reached.add(validate.evaluate(order, fhir=fhir, rxnav=rxnav).outcome.code)

    assert reached, "no hold codes reached — the fixtures are not exercising the gate"
    assert reached <= declared, f"undeclared hold codes: {sorted(reached - declared)}"


def test_the_alert_allowlist_is_not_widened_by_validation() -> None:
    """A cheap tripwire: the gate must not grow a path that carries PHI outward.

    `ALERT_FIELDS` is the allowlist the notification is built from. If validation ever
    starts producing something nurse-facing, that something has to be a field of
    `DispatchAlert` — so the alert shape is asserted here next to the code that decides
    what a nurse is told.
    """
    assert set(ALERT_FIELDS) == {
        "item_description",
        "quantity",
        "courier_display_name",
        "eta",
    }


def test_rxnorm_system_is_the_standard_one() -> None:
    """Codes are only comparable if both sides agree on the system URI."""
    assert build.RXNORM_SYSTEM == "http://www.nlm.nih.gov/research/umls/rxnorm"
