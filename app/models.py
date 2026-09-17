"""Domain types.

Deliberately independent of both wire formats: the HL7 parser produces these, the FHIR
builder consumes them, and the validators reason only over these. Nothing here knows about
ER7 pipes or FHIR JSON.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


# --------------------------------------------------------------------------------------
# Inbound: HL7 v2 OMP^O09 (the floor nurse's indent)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IndentOrder:
    """A floor nurse's indent, parsed from an OMP^O09 Pharmacy/Treatment Order.

    Note this carries patient-identifying fields (``patient_name``, ``patient_mrn``) on
    purpose. The inbound message legitimately contains PHI; carrying it here is what lets
    the PHI tests prove those fields never reach the outbound alert.
    """

    # --- correlation: which patient, which order -------------------------------------
    patient_mrn: str  # PID-3.1
    patient_name: str  # PID-5 — PHI, must never reach the alert
    placer_order_number: str  # ORC-2.1
    filler_order_number: str  # ORC-3.1
    order_control: str  # ORC-1 (NW / XO / CA / DC …)

    # --- what was requested -----------------------------------------------------------
    requested_code: str  # RXO-1.1
    requested_display: str  # RXO-1.2 — the human string we resolve against RxNorm
    give_amount: str | None  # RXO-2
    give_units: str | None  # RXO-4
    dose_form: str | None  # RXO-5  (no HL7 table in v2.5 — external vocabulary)
    strength_value: str | None  # RXO-18
    strength_units: str | None  # RXO-19
    deliver_to: str | None  # RXO-8
    indication: str | None  # RXO-20
    route: str | None  # RXR-1

    # --- provenance -------------------------------------------------------------------
    message_control_id: str  # MSH-10
    sending_application: str | None  # MSH-3

    # --- order shape: what the gate refuses outright ----------------------------------
    # These are parsed rather than inferred, because they are properties of the *message*
    # (how many items it orders, whether it carries compound components) and cannot be
    # recovered from a single RXO.
    item_count: int = 1  # number of RXO segments in the message
    is_compound: bool = False  # an RXC (component) segment is present

    @property
    def is_new_order(self) -> bool:
        return self.order_control.upper() == "NW"


# --------------------------------------------------------------------------------------
# Drug normalization — the three values we actually compare
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedDrug:
    """A drug reduced to ingredient + strength + dose form.

    This triple is the comparison surface. Anything RxNorm cannot resolve into all three
    normalizes to a value with ``None`` fields and is treated as a hold downstream — we
    never guess a missing dimension.
    """

    ingredient_name: str | None
    ingredient_rxcui: str | None
    strength_value: float | None
    strength_unit: str | None
    dose_form: str | None

    # provenance, kept for the audit trail (never for the alert)
    source_rxcui: str | None = None
    source_tty: str | None = None
    source_name: str | None = None
    unresolved_reason: str | None = None

    @property
    def is_complete(self) -> bool:
        """True only when all three comparable dimensions are present."""
        return (
            self.ingredient_name is not None
            and self.strength_value is not None
            and self.strength_unit is not None
            and self.dose_form is not None
        )

    def comparable(self) -> tuple[str | None, float | None, str | None, str | None]:
        return (
            self.ingredient_name.lower() if self.ingredient_name else None,
            self.strength_value,
            self.strength_unit,
            self.dose_form.lower() if self.dose_form else None,
        )


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


class HoldCode:
    """Machine-readable reasons a run stops short of writing a dispense.

    Every one of these is a *hold*, never a guess. The set is deliberately closed so the
    audit trail and the API response can be asserted against it.
    """

    #: The message did not parse as HL7 at all. Distinct from every code below, which are
    #: all *clinical* findings about a message that was read successfully: this one says the
    #: sending interface is broken, and the fix is on that side, not the prescriber's.
    UNPARSEABLE_INDENT = "unparseable_indent"
    ORDER_NOT_NEW = "order_control_not_new"
    MULTI_ITEM_ORDER = "multi_item_order_unsupported"
    COMPOUND_UNSUPPORTED = "compound_unsupported"
    ORDER_CORRELATION_ABSENT = "order_correlation_absent"
    ORDER_CORRELATION_AMBIGUOUS = "order_correlation_ambiguous"
    #: Both inbound order identifiers were populated but did not point at the same
    #: MedicationRequest.  Treating either one as sufficient would let a malformed
    #: message dispense against a prescription it contradicts.
    ORDER_CORRELATION_INCONSISTENT = "order_correlation_inconsistent"
    PATIENT_MISMATCH = "patient_mismatch"
    TERMINOLOGY_UNRESOLVED = "terminology_unresolved"
    TERMINOLOGY_AMBIGUOUS = "terminology_ambiguous"
    #: The inbound did not carry a value we can compare on one of the three dimensions —
    #: an unreadable strength ("abc UNT/ML"), or a missing strength or dose form.
    #:
    #: Distinct from the mismatch codes on purpose, and distinct from an unknown *unit*
    #: (``UNIT_INCOMPATIBLE``). Reporting "the strengths differ" when the truth is "we could
    #: not read the strength" would put a false clinical claim in the audit trail, and the
    #: two need different follow-up: a mismatch needs the prescriber, an unreadable field
    #: needs the sending interface fixed.
    ORDER_DETAIL_UNREADABLE = "order_detail_unreadable"
    DOSE_FORM_MISMATCH = "dose_form_mismatch"
    STRENGTH_MISMATCH = "strength_mismatch"
    INGREDIENT_MISMATCH = "ingredient_mismatch"
    UNIT_INCOMPATIBLE = "unit_incompatible"


@dataclass(frozen=True)
class ValidationOutcome:
    """Result of the safety gate.

    ``detail`` is free text that may be written to the audit trail and returned over the
    API, so it must never contain patient identifiers. It names the drug and the
    mismatched dimension only.
    """

    ok: bool
    code: str
    detail: str

    @staticmethod
    def pass_(detail: str = "prescription and indent agree") -> ValidationOutcome:
        return ValidationOutcome(ok=True, code="ok", detail=detail)

    @staticmethod
    def hold(code: str, detail: str) -> ValidationOutcome:
        return ValidationOutcome(ok=False, code=code, detail=detail)


# --------------------------------------------------------------------------------------
# Outbound: what may reach the nurse's device
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchAlert:
    """The ONLY payload shape permitted to reach a nurse's phone.

    The allowlist is structural rather than conventional: it is this class's field list.
    There is no constructor that accepts a source object, so a new PHI field appearing
    upstream cannot leak — there is no code path that could carry it. Compare
    ``ALERT_FIELDS`` below, which the PHI tests assert against.
    """

    item_description: str
    quantity: str
    courier_display_name: str
    eta: str

    def to_payload(self) -> dict[str, str]:
        return dataclasses.asdict(self)


#: The allowlist, derived from the type so the two can never drift apart.
ALERT_FIELDS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(DispatchAlert))
