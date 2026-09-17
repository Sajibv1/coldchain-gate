"""Tests for the OMP^O09 parser and the fixtures it reads.

Two of these carry more weight than the rest.

``test_fixture_places_strength_at_rxo_18`` verifies the *builder* put fields where the
parser thinks they are. The parser reads by name and the fixture builds by name, so if both
were wrong in the same way every other test here would still pass while the pipeline
validated the wrong strength. Splitting on ``|`` is normally the mistake this project is
built to avoid; here it is the only way to check the wire layout itself, which is inherently
positional.

``test_tolerant_parsing_accepts_a_message_strict_would_reject`` pins the deliberate choice
of TOLERANT validation. Without it, someone "tightening" the parser to STRICT would break
real interfaces and no test would object.
"""

from __future__ import annotations

import pytest
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.parser import parse_message

from app import demo
from app.hl7 import fixtures
from app.hl7.parse import IndentParseError, parse_indent

ALL_FIXTURES = sorted(fixtures.FIXTURES)


def _without_segment(er7: str, name: str) -> str:
    """Drop a whole segment — a stand-in for an interface that omits it."""
    kept = [s for s in er7.split("\r") if not s.startswith(f"{name}|")]
    return "\r".join(kept)


# -- the happy paths ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_parses(name: str) -> None:
    order = parse_indent(fixtures.er7(name))
    assert order.message_control_id == fixtures.FIXTURES[name].message_control_id
    assert order.requested_display, "no drug description extracted"


def test_clean_fixture_extracts_the_full_picture() -> None:
    order = parse_indent(fixtures.er7("clean"))

    assert order.patient_mrn == demo.MRN
    assert order.patient_name == "JOHN DOE"  # PID-5 is an XPN; components joined
    assert order.placer_order_number == demo.PLACER_ORDER_NUMBER
    assert order.filler_order_number == demo.FILLER_ORDER_NUMBER
    assert order.order_control == "NW"
    assert order.is_new_order

    assert order.requested_display == demo.DRUG_DISPLAY
    assert order.give_amount == "1"
    assert order.give_units == "PEN"
    assert order.dose_form == "Pen Injector"
    assert order.strength_value == "100"
    assert order.strength_units == "UNT/ML"
    assert order.route == "Subcutaneous"
    assert order.deliver_to == "3W"
    assert order.indication == "T2DM"
    assert order.sending_application == fixtures.SENDING_APP


def test_fixture_places_strength_at_rxo_18() -> None:
    """The builder's positional correctness — the assumption the parser rests on."""
    rxo = next(s for s in fixtures.er7("clean").split("\r") if s.startswith("RXO|"))
    fields = rxo.split("|")  # fields[N] is RXO-N

    assert fields[1] == "GLARG100PEN^insulin glargine 100 UNT/ML Pen Injector^L"
    assert fields[2] == "1"  # RXO-2  give amount
    assert fields[4] == "PEN"  # RXO-4  give units
    assert fields[5] == "PEN INJ^Pen Injector^L"  # RXO-5  dose form
    assert fields[8] == "3W"  # RXO-8  deliver-to
    assert fields[18] == "100"  # RXO-18 strength  <- nineteen fields in
    assert fields[19] == "UNT/ML"  # RXO-19 strength units
    assert fields[20] == "T2DM"  # RXO-20 indication


def test_ce_fields_prefer_display_over_identifier() -> None:
    """``RXO-5`` is ``PEN INJ^Pen Injector^L``: the model wants "Pen Injector"."""
    order = parse_indent(fixtures.er7("clean"))
    assert order.dose_form == "Pen Injector"
    assert order.dose_form != "PEN INJ"


# -- variants ----------------------------------------------------------------------------


def test_varying_drugs_are_read_from_rxo_1_2() -> None:
    assert parse_indent(fixtures.er7("strength_mismatch")).requested_display == (
        demo.DRUG_WRONG_STRENGTH_DISPLAY
    )
    assert parse_indent(fixtures.er7("dose_form_mismatch")).requested_display == (
        demo.DRUG_WRONG_FORM_DISPLAY
    )
    assert parse_indent(fixtures.er7("unresolvable")).requested_display == (
        demo.DRUG_UNRESOLVABLE_DISPLAY
    )


def test_order_control_other_than_nw_is_not_a_new_order() -> None:
    order = parse_indent(fixtures.er7("not_new"))
    assert order.order_control == "XO"
    assert not order.is_new_order


def test_multi_item_is_counted() -> None:
    """Two RXO segments across two ORDER groups. The gate refuses these."""
    assert parse_indent(fixtures.er7("multi_item")).item_count == 2
    assert parse_indent(fixtures.er7("clean")).item_count == 1


def test_compound_is_detected_through_nested_groups() -> None:
    """RXC sits two groups deep (ORDER > COMPONENT > RXC).

    This is also the traversal test: it fails if the parser stops descending into groups,
    which would silently turn every compound order into a normal one.
    """
    assert parse_indent(fixtures.er7("compound")).is_compound
    assert not parse_indent(fixtures.er7("clean")).is_compound


def test_absent_rxr_yields_no_route_rather_than_failing() -> None:
    order = parse_indent(_without_segment(fixtures.er7("clean"), "RXR"))
    assert order.route is None
    assert order.requested_display == demo.DRUG_DISPLAY


# -- validation-level choice -------------------------------------------------------------


def test_strict_and_tolerant_agree_on_missing_segments() -> None:
    """Neither level rejects a sparse message — measured, not assumed.

    The intuitive claim ("STRICT rejects missing required segments") is false at *parse*
    time. It holds at *build* time, where assigning MSH-10 or PID-5 is enforced — but a
    message that arrives without them parses at both levels. Recorded here because the
    design rationale for TOLERANT depends on it.
    """
    sparse = _without_segment(fixtures.er7("clean"), "PID")
    for level in (VALIDATION_LEVEL.STRICT, VALIDATION_LEVEL.TOLERANT):
        parse_message(sparse, validation_level=level, find_groups=True)  # must not raise


def test_strict_rejects_a_datatype_error_that_tolerant_accepts() -> None:
    """This is the whole of what STRICT adds at parse time, and the reason we skip it."""
    garbage = fixtures.er7("clean").replace("|100|UNT/ML|", "|abc|UNT/ML|")

    with pytest.raises(ValueError):
        parse_message(garbage, validation_level=VALIDATION_LEVEL.STRICT, find_groups=True)

    parse_message(garbage, validation_level=VALIDATION_LEVEL.TOLERANT, find_groups=True)


def test_unreadable_strength_becomes_a_hold_not_a_rejection() -> None:
    """Why TOLERANT is safe: the bad value survives parsing so the gate can hold on it.

    A transport-level rejection would leave no audit trail. Instead the unreadable strength
    reaches the normalizer, which refuses to read it, and the gate fails closed.
    """
    from app.rxnorm.normalize import parse_strength

    order = parse_indent(fixtures.er7("clean").replace("|100|UNT/ML|", "|abc|UNT/ML|"))

    assert order.strength_value == "abc"
    assert parse_strength(order.strength_value) is None, "unreadable strength must not resolve"


def test_datatype_error_is_reported_as_a_parse_error_not_a_crash() -> None:
    """hl7apy raises bare ValueError here; uncaught it would surface as a 500."""
    # MSH-12 is the version; an unparseable one fails inside the library, not in our checks.
    malformed = fixtures.er7("clean").replace("|P|2.5", "|P|9.9")
    with pytest.raises(IndentParseError):
        parse_indent(malformed)


# -- rejections --------------------------------------------------------------------------


def test_empty_message_is_rejected() -> None:
    for blank in ("", "   "):
        with pytest.raises(IndentParseError):
            parse_indent(blank)


def test_wrong_message_type_is_rejected() -> None:
    """RDS^O13 is *dispense*, not order — the brief conflates the two, so pin it."""
    er7 = fixtures.er7("clean").replace("OMP^O09^OMP_O09", "RDS^O13^RDS_O13")
    with pytest.raises(IndentParseError, match=r"RDS\^O13"):
        parse_indent(er7)


def test_message_without_orc_is_rejected() -> None:
    with pytest.raises(IndentParseError, match="ORC"):
        parse_indent(_without_segment(fixtures.er7("clean"), "ORC"))


def test_message_without_rxo_is_rejected() -> None:
    with pytest.raises(IndentParseError, match="RXO"):
        parse_indent(_without_segment(fixtures.er7("clean"), "RXO"))


def test_rxo_without_a_drug_description_is_rejected() -> None:
    """A code alone cannot be resolved — RxNorm needs the description string."""
    er7 = fixtures.er7("clean").replace(
        "GLARG100PEN^insulin glargine 100 UNT/ML Pen Injector^L", "GLARG100PEN"
    )
    with pytest.raises(IndentParseError, match="RXO-1"):
        parse_indent(er7)


def test_message_without_a_patient_identifier_is_rejected() -> None:
    er7 = fixtures.er7("clean").replace("MRN12345^^^CITYHOSP^MR", "")
    with pytest.raises(IndentParseError, match="PID-3"):
        parse_indent(er7)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: e.replace("OMP^O09^OMP_O09", "RDS^O13^RDS_O13"), id="wrong-type"),
        pytest.param(lambda e: _without_segment(e, "ORC"), id="no-orc"),
        pytest.param(lambda e: _without_segment(e, "RXO"), id="no-rxo"),
        pytest.param(lambda e: e.replace("MRN12345^^^CITYHOSP^MR", ""), id="no-mrn"),
    ],
)
def test_parse_errors_never_echo_message_content(mutate) -> None:
    """These errors are returned to the caller and logged. This message is PHI-bearing.

    The drug name is checked too: it is not patient-identifying, but an error string built
    from message content is how a leak starts.
    """
    with pytest.raises(IndentParseError) as excinfo:
        parse_indent(mutate(fixtures.er7("clean")))

    message = str(excinfo.value)
    for secret in (demo.MRN, "DOE", "JOHN", demo.DRUG_DISPLAY, demo.DRUG_RXCUI):
        assert secret not in message, f"{secret!r} leaked into a parse error"


# -- the inbound legitimately carries PHI ------------------------------------------------


def test_indent_order_carries_phi_by_design() -> None:
    """Not a bug: the scrubber tests are only meaningful if there is PHI to strip."""
    order = parse_indent(fixtures.er7("clean"))
    assert order.patient_name == "JOHN DOE"
    assert order.patient_mrn == demo.MRN
