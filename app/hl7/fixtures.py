"""HL7 v2.5 ``OMP^O09`` fixture generation.

The fixtures are built with hl7apy's object model rather than hand-written as ER7. That is
a deliberate call: ``RXO-18`` (Requested Give Strength) sits nineteen fields into the
segment, and a hand-counted pipe is one miscount away from a fixture that tests the wrong
thing while still parsing cleanly. Building it means the positions are right by
construction — verified by reading the emitted ER7 back (see ``tests/test_hl7_parse.py``,
which asserts RXO-18 lands where the parser expects it).

The message is always the same shape; only the drug, the order control code, and the order
shape vary. ``IndentSpec`` is that variation.

Run ``python -m app.hl7.fixtures`` to print every fixture as ER7 — that is the reviewable
form, and it is the same string the tests parse.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Message

from app import demo

#: Everything in the fixture is fixed except these.
DEFAULT_SPEC = {
    "order_control": "NW",
    "requested_code": "GLARG100PEN",
    "requested_display": demo.DRUG_DISPLAY,
    "give_amount": "1",
    "give_units": "PEN",
    "dose_form": "PEN INJ^Pen Injector^L",
    "strength_value": "100",
    "strength_units": "UNT/ML",
    "route": "SC^Subcutaneous^HL70162",
    "deliver_to": "3W",
    "indication": "T2DM",
}

#: Sending application / facility. Invented, and stable so fixtures are diffable.
SENDING_APP = "COLDCHAIN"
SENDING_FACILITY = "DNAHEALTH"

#: Fixed, so two renderings of the same spec produce byte-identical ER7. hl7apy otherwise
#: stamps MSH-7 with the current time.
MESSAGE_DATETIME = "20260916080000"


@dataclass(frozen=True)
class IndentSpec:
    """The varying part of an indent fixture."""

    message_control_id: str
    requested_code: str = DEFAULT_SPEC["requested_code"]  # type: ignore[assignment]
    requested_display: str = DEFAULT_SPEC["requested_display"]  # type: ignore[assignment]
    order_control: str = "NW"
    give_amount: str = "1"
    give_units: str = "PEN"
    dose_form: str = "PEN INJ^Pen Injector^L"
    strength_value: str = "100"
    strength_units: str = "UNT/ML"
    route: str = "SC^Subcutaneous^HL70162"
    deliver_to: str = "3W"
    indication: str = "T2DM"
    patient_mrn: str = demo.MRN
    patient_family: str = demo.PATIENT_FAMILY
    patient_given: str = demo.PATIENT_GIVEN
    placer_order_number: str = demo.PLACER_ORDER_NUMBER
    filler_order_number: str = demo.FILLER_ORDER_NUMBER
    #: Extra ORDER groups beyond the first. 1 extra => a two-item indent.
    extra_items: int = 0
    #: Add RXC component segments, marking the order as a compound.
    compound: bool = False


def _set(field: object, value: str) -> None:
    """Assign a value to an hl7apy field, ignoring empty optionals."""
    if value:
        setattr(field, "value", value)  # type: ignore[attr-defined]


def render(spec: IndentSpec) -> str:
    """Build the ER7 string for ``spec``. Pure; no clock, no I/O."""
    message = Message("OMP_O09", version="2.5", validation_level=VALIDATION_LEVEL.STRICT)

    msh = message.msh
    _set(msh.msh_3, SENDING_APP)
    _set(msh.msh_4, SENDING_FACILITY)
    _set(msh.msh_7, MESSAGE_DATETIME)
    _set(msh.msh_9, "OMP^O09^OMP_O09")
    _set(msh.msh_10, spec.message_control_id)
    _set(msh.msh_11, "P")
    _set(msh.msh_12, "2.5")

    patient_group = message.add_group("OMP_O09_PATIENT")
    pid = patient_group.add_segment("PID")
    _set(pid.pid_3, f"{spec.patient_mrn}^^^CITYHOSP^MR")
    _set(pid.pid_5, f"{spec.patient_family}^{spec.patient_given}^Q")
    # PV1 is not a direct child of the PATIENT group — hl7apy nests it in a VISIT group,
    # matching the v2.5 OMP_O09 structure. Nothing in IndentOrder consumes PV1; it is here
    # because an inpatient pharmacy order that omits the visit looks nothing like the real
    # interface, and a reviewer should be able to see that we know where it goes.
    visit_group = patient_group.add_group("OMP_O09_PATIENT_VISIT")
    pv1 = visit_group.add_segment("PV1")
    _set(pv1.pv1_1, "1")  # Set ID — numeric; "I" here is a datatype error
    _set(pv1.pv1_2, "I")  # Inpatient

    def add_order(index: int) -> None:
        """One ORDER group: ORC + RXO (+ RXC when compounding) + RXR."""
        group = message.add_group("OMP_O09_ORDER")
        orc = group.add_segment("ORC")
        _set(orc.orc_1, spec.order_control)
        # The second item gets its own order numbers, as a real multi-item indent would.
        suffix = "" if index == 0 else f"-{index + 1}"
        _set(orc.orc_2, f"{spec.placer_order_number}{suffix}")
        _set(orc.orc_3, f"{spec.filler_order_number}{suffix}")

        rxo = group.add_segment("RXO")
        _set(rxo.rxo_1, f"{spec.requested_code}^{spec.requested_display}^L")
        _set(rxo.rxo_2, spec.give_amount)
        _set(rxo.rxo_4, spec.give_units)
        _set(rxo.rxo_5, spec.dose_form)
        _set(rxo.rxo_8, spec.deliver_to)
        _set(rxo.rxo_18, spec.strength_value)
        _set(rxo.rxo_19, spec.strength_units)
        _set(rxo.rxo_20, spec.indication)

        if spec.compound:
            # RXC is not a direct child of ORDER either — v2.5 nests it in a COMPONENT
            # group, which is exactly the shape a compound order takes on the wire.
            component_group = group.add_group("OMP_O09_COMPONENT")
            rxc = component_group.add_segment("RXC")
            _set(rxc.rxc_1, "B")  # base component
            _set(rxc.rxc_2, "ING1^insulin glargine^L")
            _set(rxc.rxc_3, "100")
            _set(rxc.rxc_4, "UNT/ML")

        rxr = group.add_segment("RXR")
        _set(rxr.rxr_1, spec.route)

    add_order(0)
    for extra in range(spec.extra_items):
        add_order(extra + 1)

    return message.to_er7()


#: Every fixture the demo and the tests use. Names are referenced by the pipeline's
#: `--demo` walk, so they are part of the interface.
FIXTURES: dict[str, IndentSpec] = {
    # The happy path: indent matches the seeded prescription.
    "clean": IndentSpec(message_control_id="MSG00001"),
    # Same ingredient, wrong strength — the clinically dangerous case.
    "strength_mismatch": IndentSpec(
        message_control_id="MSG00002",
        requested_code="GLARG300PEN",
        requested_display=demo.DRUG_WRONG_STRENGTH_DISPLAY,
        strength_value="300",
    ),
    # Same ingredient and strength, wrong dose form.
    "dose_form_mismatch": IndentSpec(
        message_control_id="MSG00003",
        requested_code="GLARG100SOL",
        requested_display=demo.DRUG_WRONG_FORM_DISPLAY,
        dose_form="SOLN^Injectable Solution^L",
    ),
    # Terminology that resolves to nothing — must hold, not guess.
    "unresolvable": IndentSpec(
        message_control_id="MSG00004",
        requested_code="UNKNOWN1",
        requested_display=demo.DRUG_UNRESOLVABLE_DISPLAY,
        strength_value="",
        strength_units="",
        dose_form="",
    ),
    # Not a new order (XO = change order) — nothing to dispense.
    "not_new": IndentSpec(message_control_id="MSG00005", order_control="XO"),
    # Two items in one message — out of scope, hold rather than dispense one of them.
    "multi_item": IndentSpec(message_control_id="MSG00006", extra_items=1),
    # A compound (RXC present) — out of scope.
    "compound": IndentSpec(message_control_id="MSG00007", compound=True),
}


def er7(name: str) -> str:
    """The ER7 string for a named fixture."""
    return render(FIXTURES[name])


def all_er7() -> dict[str, str]:
    return {name: render(spec) for name, spec in FIXTURES.items()}


def with_identifiers(name: str, *, token: str) -> str:
    """A fixture rendered with tokenised patient/order identifiers.

    For integration tests: the identifiers must not collide with a seeded demo, for the
    same reason described in ``app.fhir.seed.build_dataset``.
    """
    spec = FIXTURES[name]
    return render(
        replace(
            spec,
            patient_mrn=f"{spec.patient_mrn}-{token}",
            placer_order_number=f"{spec.placer_order_number}-{token}",
            filler_order_number=f"{spec.filler_order_number}-{token}",
        )
    )


def main() -> int:
    for name, message in all_er7().items():
        print(f"--- {name} ---")
        print(message.replace("\r", "\n"))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
