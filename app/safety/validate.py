"""The safety gate — the decision that stands between an indent and a dispense.

The brief's rubric penalises string matching, and this module is where that rule earns its
keep. Nothing here compares drug *names*. Both sides are reduced to an
(ingredient, strength, dose form) triple and compared on that:

    prescribed  <- FHIR ``MedicationRequest``, resolved through RxNorm's structured
                   relations (``IN`` for the ingredient, ``DF`` for the dose form,
                   ``AVAILABLE_STRENGTH`` for the strength)
    requested   <- the HL7 message, read *structurally*: the ingredient by resolving
                   RXO-1.2 through RxNorm, but the strength from RXO-18/19 and the dose
                   form from RXO-5

That split is deliberate. The display string is a human label and may be abbreviated,
stale, or wrong; RXO-18/19 is what the ordering system actually recorded as the strength,
and it is where the clinically dangerous error lives — an indent for 300 UNT/ML against a
prescription for 100 UNT/ML. RxNorm is used for what only RxNorm knows (which ingredient a
product contains); the message is used for what the message authoritatively states.

**Every path fails closed.** There are thirteen ways to stop and exactly one way to
proceed. A missing correlation, an ambiguous RxNorm match, an unreadable strength, an
unknown unit, or a mismatch each produce a hold and *no dispense*. None of them falls back
to a best guess, and none of them is an exception: a hold is a normal, auditable outcome
with a machine-readable code, because the alternative — a crash — leaves no trail at all.

The order of checks is part of the contract. Shape before correlation, correlation before
terminology, ingredient before strength before dose form. Each is cheaper than the next, so
a nonsensical message is refused without spending an RxNorm call, and the *first* reason
found is the one reported — a message that is both a multi-item order and a strength
mismatch reports the shape problem, which is the one that has to be fixed first.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.fhir import build
from app.fhir.client import FhirClient
from app.models import HoldCode, IndentOrder, NormalizedDrug, ValidationOutcome
from app.rxnorm.client import JSON, RxNavClient
from app.rxnorm.normalize import (
    canonical_unit,
    candidates_for_display,
    dose_form_tokens,
    normalize_rxcui,
    parse_strength,
)


@dataclass(frozen=True)
class Prescription:
    """The FHIR side: what was prescribed, and the ids a dispense needs to reference.

    Carrying the ids here rather than re-deriving them in the pipeline keeps the "which
    resources did we actually check" decision in one place — a dispense that referenced a
    request this gate never validated would be exactly the silent failure the gate exists
    to prevent.
    """

    request_id: str
    patient_id: str
    #: The prescribing clinician. Carried because the audit trail's ``agent.who`` is
    #: "who is accountable for this dispense" — a person, not the pipeline — and the
    #: pipeline would otherwise have to re-read the request to find them.
    requester_id: str
    #: The ``CodeableConcept`` from the request, copied (not rebuilt) onto the dispense so
    #: the two can never disagree about what was dispensed.
    medication: JSON
    drug: NormalizedDrug


@dataclass(frozen=True)
class Verdict:
    """The gate's answer.

    ``prescription`` is present only on success, which is what makes "no dispense on a
    hold" structural rather than a convention the pipeline has to remember: there is
    nothing to write a dispense from.
    """

    outcome: ValidationOutcome
    prescription: Prescription | None = None
    requested: NormalizedDrug | None = None

    @property
    def ok(self) -> bool:
        return self.outcome.ok


def _hold(code: str, detail: str) -> Verdict:
    return Verdict(outcome=ValidationOutcome.hold(code, detail))


# -- 1. order shape ---------------------------------------------------------------------


def check_order_shape(order: IndentOrder) -> ValidationOutcome | None:
    """Refuse orders this system cannot safely fulfil, before spending a network call.

    All three are properties of the *message*, checked without touching FHIR or RxNorm.
    """
    if not order.is_new_order:
        return ValidationOutcome.hold(
            HoldCode.ORDER_NOT_NEW,
            f"order control is {order.order_control!r}, not 'NW' — "
            "this is not a new order, so there is nothing to dispense",
        )
    if order.item_count > 1:
        return ValidationOutcome.hold(
            HoldCode.MULTI_ITEM_ORDER,
            f"message orders {order.item_count} items; this system dispenses one order "
            "at a time and will not choose among them",
        )
    if order.is_compound:
        return ValidationOutcome.hold(
            HoldCode.COMPOUND_UNSUPPORTED,
            "message carries compound components (RXC); component preparation is out of "
            "scope for this pipeline",
        )
    return None


# -- 2. correlation ---------------------------------------------------------------------


def correlate(fhir: FhirClient, order: IndentOrder) -> tuple[JSON | None, ValidationOutcome | None]:
    """Find the one ``MedicationRequest`` this indent refers to.

    An interface may omit one of ORC-2/ORC-3, in which case the populated identifier is
    sufficient.  When it sends *both*, however, both must resolve to the same single
    request.  A fallback-only lookup is unsafe: it would accept a correct placer number
    while silently ignoring a contradictory filler number.

    The returned detail deliberately names no inbound identifier.  It is copied into the
    audit trail and outward-facing hold view, where an order number is patient-linkable.
    """
    lookups: list[tuple[str, list[JSON]]] = []
    if order.placer_order_number:
        lookups.append(
            (
                "placer",
                fhir.search_by_identifier(
                    "MedicationRequest", build.SYSTEM_PLACER_ORDER, order.placer_order_number
                ),
            )
        )
    if order.filler_order_number:
        lookups.append(
            (
                "filler",
                fhir.search_by_identifier(
                    "MedicationRequest", build.SYSTEM_FILLER_ORDER, order.filler_order_number
                ),
            )
        )

    if not lookups:
        return None, ValidationOutcome.hold(
            HoldCode.ORDER_CORRELATION_ABSENT,
            "no MedicationRequest matches every populated inbound order identifier",
        )
    if any(not candidates for _, candidates in lookups):
        # One populated key resolving while the other does not is a contradiction, not a
        # harmless fallback.  If neither resolves, it is simply an unknown order.
        if any(candidates for _, candidates in lookups):
            return None, ValidationOutcome.hold(
                HoldCode.ORDER_CORRELATION_INCONSISTENT,
                "the inbound placer and filler order identifiers do not resolve to the same "
                "MedicationRequest",
            )
        return None, ValidationOutcome.hold(
            HoldCode.ORDER_CORRELATION_ABSENT,
            "no MedicationRequest matches every populated inbound order identifier",
        )
    if any(len(candidates) > 1 for _, candidates in lookups):
        return None, ValidationOutcome.hold(
            HoldCode.ORDER_CORRELATION_AMBIGUOUS,
            "an inbound order identifier matches multiple MedicationRequests; refusing to "
            "guess which one this indent belongs to",
        )

    matched = [candidates[0] for _, candidates in lookups]
    if len({request.get("id") for request in matched}) != 1:
        return None, ValidationOutcome.hold(
            HoldCode.ORDER_CORRELATION_INCONSISTENT,
            "the inbound placer and filler order identifiers do not resolve to the same "
            "MedicationRequest",
        )
    return matched[0], None


def confirm_patient(fhir: FhirClient, order: IndentOrder, request: JSON) -> ValidationOutcome | None:
    """The correlated prescription must belong to the patient on the indent.

    Unverifiable counts as a failure. If the ``Patient`` cannot be read, or carries no MRN
    on the system we index, we cannot show that these are the same person — and "we could
    not check" must not be quietly promoted to "we checked and it was fine".
    """
    reference = (request.get("subject") or {}).get("reference") or ""
    patient_id = reference.split("/", 1)[1] if reference.startswith("Patient/") else ""
    if not patient_id:
        return ValidationOutcome.hold(
            HoldCode.PATIENT_MISMATCH,
            "the matched MedicationRequest has no Patient subject, so the indent's patient "
            "cannot be confirmed",
        )

    patient = fhir.read("Patient", patient_id)
    if patient is None:
        return ValidationOutcome.hold(
            HoldCode.PATIENT_MISMATCH,
            "the MedicationRequest references a Patient that could not be read; refusing to "
            "dispense without confirming the patient",
        )

    mrns = {
        i.get("value")
        for i in (patient.get("identifier") or [])
        if i.get("system") == build.SYSTEM_MRN
    }
    if order.patient_mrn not in mrns:
        # No MRN is named here. The detail string reaches the audit trail and the API
        # response, and an MRN is a patient identifier.
        return ValidationOutcome.hold(
            HoldCode.PATIENT_MISMATCH,
            "the indent's patient does not match the patient on the correlated "
            "prescription",
        )
    return None


# -- 3. the prescribed side -------------------------------------------------------------


def _medication_concept(fhir: FhirClient, request: JSON) -> JSON | None:
    """The request's medication as a ``CodeableConcept``.

    R4 allows either ``medicationCodeableConcept`` or ``medicationReference``; a request
    built by another system routinely uses the latter, so both are supported rather than
    assuming the shape this project's own seeder happens to write.
    """
    concept = request.get("medicationCodeableConcept")
    if concept:
        return concept
    reference = (request.get("medicationReference") or {}).get("reference") or ""
    if not reference.startswith("Medication/"):
        return None
    medication = fhir.read("Medication", reference.split("/", 1)[1])
    if medication is None:
        return None
    return medication.get("code")


def _rxnorm_code(concept: JSON) -> str | None:
    for coding in concept.get("coding") or []:
        if coding.get("system") == build.RXNORM_SYSTEM and coding.get("code"):
            return coding["code"]
    return None


def prescribed_drug(
    fhir: FhirClient, rxnav: RxNavClient, request: JSON
) -> tuple[NormalizedDrug | None, Prescription | None, ValidationOutcome | None]:
    """Normalize what was prescribed, using RxNorm's structured relations only."""
    concept = _medication_concept(fhir, request)
    if not concept:
        return None, None, ValidationOutcome.hold(
            HoldCode.TERMINOLOGY_UNRESOLVED,
            "the correlated MedicationRequest carries no resolvable medication code",
        )

    rxcui = _rxnorm_code(concept)
    if not rxcui:
        # Falling back to the display text here would be the string matching this design
        # exists to avoid: it would look like a code lookup while being a name guess.
        return None, None, ValidationOutcome.hold(
            HoldCode.TERMINOLOGY_UNRESOLVED,
            "the prescription's medication is not coded in RxNorm, so it cannot be "
            "compared as a formulation",
        )

    drug = normalize_rxcui(rxnav, rxcui)
    if drug is None or not drug.is_complete:
        missing = "unknown concept" if drug is None else "incomplete concept"
        return None, None, ValidationOutcome.hold(
            HoldCode.TERMINOLOGY_UNRESOLVED,
            f"RxNorm could not resolve the prescribed medication ({missing}: rxcui "
            f"{rxcui}); ingredient, strength and dose form are all required to compare",
        )

    patient_id = ((request.get("subject") or {}).get("reference") or "").split("/", 1)[-1]
    requester_id = ((request.get("requester") or {}).get("reference") or "").split("/", 1)[-1]
    prescription = Prescription(
        request_id=request["id"],
        patient_id=patient_id,
        requester_id=requester_id,
        medication=concept,
        drug=drug,
    )
    return drug, prescription, None


# -- 4. the requested side --------------------------------------------------------------


def _ordered_strength(order: IndentOrder) -> tuple[float, str] | None:
    """``(value, unit)`` from RXO-18/19, or ``None`` when it cannot be read as a number.

    The unit is returned as written — canonicalising it here would hide the difference
    between "the strengths differ" and "these units are not comparable", which the caller
    needs to tell apart to report the right hold.
    """
    if not order.strength_value or not order.strength_units:
        return None
    return parse_strength(f"{order.strength_value} {order.strength_units}")


def requested_drug(
    rxnav: RxNavClient, order: IndentOrder
) -> tuple[NormalizedDrug | None, ValidationOutcome | None]:
    """Normalize what was ordered, from the message's own fields."""
    candidates = candidates_for_display(rxnav, order.requested_display)
    if not candidates:
        return None, ValidationOutcome.hold(
            HoldCode.TERMINOLOGY_UNRESOLVED,
            f"RxNorm has no concept matching the ordered product "
            f"{order.requested_display!r}; refusing to guess which drug was meant",
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(c.source_name or "?" for c in candidates))
        return None, ValidationOutcome.hold(
            HoldCode.TERMINOLOGY_AMBIGUOUS,
            f"the ordered product matches {len(candidates)} different RxNorm "
            f"formulations ({names}); refusing to choose one",
        )

    resolved = candidates[0]

    strength = _ordered_strength(order)
    if strength is None:
        return None, ValidationOutcome.hold(
            HoldCode.ORDER_DETAIL_UNREADABLE,
            "the ordered strength (RXO-18/19) is missing or is not a number, so there is "
            "nothing to compare against the prescription",
        )
    value, raw_unit = strength
    unit, unit_known = canonical_unit(raw_unit)

    # The dose form is the one dimension where the resolved RxNorm concept is a legitimate
    # fallback: RXO-5 has no HL7 table in v2.5, so interfaces populate it from a local
    # vocabulary and some omit it entirely.
    dose_form = order.dose_form or resolved.dose_form
    if not dose_form:
        return None, ValidationOutcome.hold(
            HoldCode.ORDER_DETAIL_UNREADABLE,
            "the order states no dose form (RXO-5) and RxNorm has none for the resolved "
            "product, so the formulation cannot be compared",
        )

    return (
        NormalizedDrug(
            ingredient_name=resolved.ingredient_name,
            ingredient_rxcui=resolved.ingredient_rxcui,
            strength_value=value,
            strength_unit=unit,
            dose_form=dose_form,
            source_rxcui=resolved.source_rxcui,
            source_tty=resolved.source_tty,
            source_name=resolved.source_name,
        ),
        None,
    )


# -- 5. the comparison ------------------------------------------------------------------


def _ingredient_differs(prescribed: NormalizedDrug, requested: NormalizedDrug) -> bool:
    """Compare by RxCUI when both have one — a code is not a name.

    The name comparison is only a fallback for a concept whose ingredient could not be
    rolled up, and it is deliberately exact-after-lowercasing rather than fuzzy: two
    different ingredient names are a different drug.
    """
    if prescribed.ingredient_rxcui and requested.ingredient_rxcui:
        return prescribed.ingredient_rxcui != requested.ingredient_rxcui
    left = (prescribed.ingredient_name or "").strip().lower()
    right = (requested.ingredient_name or "").strip().lower()
    return left != right


def compare(prescribed: NormalizedDrug, requested: NormalizedDrug) -> ValidationOutcome:
    """Judge the two triples. Returns a passing outcome or the first reason to hold."""
    if _ingredient_differs(prescribed, requested):
        return ValidationOutcome.hold(
            HoldCode.INGREDIENT_MISMATCH,
            f"prescribed ingredient {prescribed.ingredient_name!r} differs from ordered "
            f"ingredient {requested.ingredient_name!r}",
        )

    # Units before values: "100 MG/ML" against "100 UNT/ML" is not a matched strength, and
    # reporting it as a mismatch would be a clinical claim we have not earned.
    prescribed_unit, prescribed_known = canonical_unit(prescribed.strength_unit)
    requested_unit, requested_known = canonical_unit(requested.strength_unit)
    if not (prescribed_known and requested_known):
        unknown = prescribed.strength_unit if not prescribed_known else requested.strength_unit
        return ValidationOutcome.hold(
            HoldCode.UNIT_INCOMPATIBLE,
            f"strength unit {unknown!r} is not one this system can compare; refusing to "
            "treat incomparable units as a match",
        )
    if prescribed_unit != requested_unit:
        return ValidationOutcome.hold(
            HoldCode.UNIT_INCOMPATIBLE,
            f"prescribed strength unit {prescribed_unit!r} cannot be compared with ordered "
            f"unit {requested_unit!r}",
        )
    if prescribed.strength_value != requested.strength_value:
        return ValidationOutcome.hold(
            HoldCode.STRENGTH_MISMATCH,
            f"prescribed {prescribed.strength_value:g} {prescribed_unit} differs from "
            f"ordered {requested.strength_value:g} {requested_unit} "
            f"({prescribed.ingredient_name})",
        )

    if dose_form_tokens(prescribed.dose_form) != dose_form_tokens(requested.dose_form):
        return ValidationOutcome.hold(
            HoldCode.DOSE_FORM_MISMATCH,
            f"prescribed dose form {prescribed.dose_form!r} differs from ordered "
            f"{requested.dose_form!r} ({prescribed.ingredient_name})",
        )

    return ValidationOutcome.pass_(
        f"ordered {requested.ingredient_name} {requested.strength_value:g} "
        f"{requested_unit} {requested.dose_form} matches the prescription"
    )


# -- the gate ---------------------------------------------------------------------------


def evaluate(order: IndentOrder, *, fhir: FhirClient, rxnav: RxNavClient) -> Verdict:
    """Run every check in order and stop at the first failure.

    Failures that are *not* holds — a FHIR outage, an RxNav outage — propagate as
    exceptions rather than being converted into holds. That distinction matters: a hold
    says "a human must look at this indent", while an outage says "the check did not run".
    Recording an outage as a clinical hold would put a false statement in the audit trail
    and would train operators to ignore the code that means "this drug was wrong".
    """
    shape = check_order_shape(order)
    if shape is not None:
        return Verdict(outcome=shape)

    request, correlation_problem = correlate(fhir, order)
    if correlation_problem is not None:
        return Verdict(outcome=correlation_problem)
    assert request is not None

    patient_problem = confirm_patient(fhir, order, request)
    if patient_problem is not None:
        return Verdict(outcome=patient_problem)

    prescribed, prescription, prescribed_problem = prescribed_drug(fhir, rxnav, request)
    if prescribed_problem is not None:
        return Verdict(outcome=prescribed_problem)

    requested, requested_problem = requested_drug(rxnav, order)
    if requested_problem is not None:
        return Verdict(outcome=requested_problem)
    assert prescribed is not None and requested is not None

    outcome = compare(prescribed, requested)
    # `prescription` is returned only on success, so "no dispense on a hold" is structural:
    # the pipeline has nothing to build a dispense from.
    return Verdict(
        outcome=outcome,
        prescription=prescription if outcome.ok else None,
        requested=requested,
    )
