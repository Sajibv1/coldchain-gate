"""Parse an HL7 v2.5 ``OMP^O09`` Pharmacy/Treatment Order into `IndentOrder`.

Parsing goes through hl7apy's object model. Nothing here splits on ``|`` or applies a
regex, and that is not fussiness: ``RXO-18`` sits nineteen fields into the segment, so a
positional read is one interface revision away from silently returning the wrong value —
and a pharmacy order that validates against the wrong strength is the exact failure this
system exists to prevent. Reading by field name means a shifted position fails loudly
instead.

Validation level is TOLERANT rather than STRICT, for a narrower reason than it first
appears. Measured against the library (`tests/test_hl7_parse.py` holds the table):
``parse_message`` accepts missing segments and missing fields at *both* levels — neither
rejects a sparse message. What STRICT adds at parse time is **datatype** validation: it
rejects ``RXO-18 = "abc"`` where TOLERANT accepts it.

TOLERANT is the right choice because that error belongs one layer down. A strength that
cannot be read as a number is a *clinical* problem, and the correct response is a recorded
hold with an audit trail, not a transport-level rejection that leaves nothing behind.
``parse_strength`` returns ``None`` for ``"abc"`` and the gate fails closed on it. The
datatype check still happens; it happens in the layer that knows what the value means.

One consequence worth knowing: hl7apy raises a bare ``ValueError`` for datatype failures,
*not* its own ``HL7apyException``. Both are caught below — otherwise a malformed message
would escape as a 500 instead of a 400.

Two failure modes are kept distinct:

* :class:`IndentParseError` — the message is not a usable OMP^O09 at all: wrong message
  type, or missing a field without which there is no order to speak of. This is a client
  error, surfaced as a 4xx.
* An `IndentOrder` with an empty ``placer_order_number`` — the order is well-formed but
  cannot be tied to a prescription. That is a *clinical* outcome, and the gate turns it into
  an ``ORDER_CORRELATION_ABSENT`` hold rather than a rejection.
"""

from __future__ import annotations

from collections.abc import Iterator

from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Element
from hl7apy.exceptions import HL7apyException
from hl7apy.parser import parse_message

from app.models import IndentOrder

#: Message type this parser accepts (MSH-9.1 / MSH-9.2).
EXPECTED_MESSAGE_CODE = "OMP"
EXPECTED_TRIGGER = "O09"


class IndentParseError(ValueError):
    """The message could not be read as an OMP^O09 indent.

    ``detail`` is safe to return to the caller and to log: it names the structural problem
    and never echoes field values, which in this message are PHI.
    """


# -- traversal ---------------------------------------------------------------------------


def _iter_segments(node: Element) -> Iterator[Element]:
    """Yield every Segment under ``node``, descending through groups.

    Done by walking ``children`` rather than by naming groups. The nesting is not obvious —
    PV1 sits in an ``OMP_O09_PATIENT_VISIT`` group and RXC in an ``OMP_O09_COMPONENT``
    group, both of which surprised us — and reaching into groups by name would couple this
    parser to hl7apy's internal naming. Walking finds them wherever they are.
    """
    for child in node.children:
        if child.classname == "Segment":
            yield child
        else:
            yield from _iter_segments(child)


def _segments_by_name(message: Element) -> dict[str, list[Element]]:
    found: dict[str, list[Element]] = {}
    for segment in _iter_segments(message):
        found.setdefault(segment.name, []).append(segment)
    return found


def _text(parent: Element, name: str, sub: str | None = None) -> str:
    """Trimmed value of a field (or one of its components); ``""`` when absent.

    hl7apy returns ``''`` for a field that is present-but-empty and for one that is absent
    but defined by the segment grammar — verified, not assumed. The exception path covers
    only a name the segment does not define, which would be a bug in this module rather
    than anything about the message.
    """
    try:
        child = getattr(parent, name)
        if sub is not None:
            child = getattr(child, sub)
        return (child.value or "").strip()
    except (AttributeError, HL7apyException):
        return ""


def _codeable(segment: Element, name: str) -> str:
    """An HL7 CE/CNE field as human-readable text.

    Prefers the display (component 2) and falls back to the identifier (component 1). The
    domain model wants the descriptive form for ``dose_form``/``route``/``indication`` —
    those exist to be read by a human in the audit trail, and RxNorm resolution works off
    ``requested_display`` instead.
    """
    return _text(segment, name, f"{name}_2") or _text(segment, name, f"{name}_1")


# -- public API --------------------------------------------------------------------------


def parse_indent(er7: str) -> IndentOrder:
    """Parse an OMP^O09 ER7 message.

    Raises :class:`IndentParseError` when the message is not a usable indent.
    """
    if not er7 or not er7.strip():
        raise IndentParseError("empty message")

    try:
        message = parse_message(
            er7.strip(),
            validation_level=VALIDATION_LEVEL.TOLERANT,
            find_groups=True,
        )
    except (HL7apyException, ValueError) as exc:
        # hl7apy signals datatype failures with a bare ValueError, not HL7apyException —
        # catching only the latter would let a malformed message escape as a 500.
        # The underlying message can quote the offending content, which is PHI, so only
        # the exception type is carried forward.
        raise IndentParseError(f"not parseable as HL7 v2 ER7 ({type(exc).__name__})") from None

    msh = message.msh
    message_code = _text(msh, "msh_9", "msh_9_1")
    trigger = _text(msh, "msh_9", "msh_9_2")
    if (message_code, trigger) != (EXPECTED_MESSAGE_CODE, EXPECTED_TRIGGER):
        raise IndentParseError(
            f"expected {EXPECTED_MESSAGE_CODE}^{EXPECTED_TRIGGER}, "
            f"got {message_code or '?'}^{trigger or '?'}"
        )

    segments = _segments_by_name(message)
    orc_segments = segments.get("ORC", [])
    rxo_segments = segments.get("RXO", [])

    if not orc_segments:
        raise IndentParseError("no ORC segment: the message carries no order control")
    if not rxo_segments:
        raise IndentParseError("no RXO segment: the message carries no drug to validate")

    # The first ORDER group is the one we validate. A second RXO is recorded via item_count
    # and refused by the gate rather than silently ignored.
    orc = orc_segments[0]
    rxo = rxo_segments[0]

    requested_display = _text(rxo, "rxo_1", "rxo_1_2")
    if not requested_display:
        raise IndentParseError("RXO-1 carries no drug description to validate")

    pid = segments.get("PID", [None])[0]
    patient_mrn = _text(pid, "pid_3", "pid_3_1") if pid is not None else ""
    if not patient_mrn:
        raise IndentParseError("PID-3 carries no patient identifier")

    rxr = segments.get("RXR", [None])[0]

    return IndentOrder(
        patient_mrn=patient_mrn,
        patient_name=_patient_name(pid),
        # Left empty rather than rejected when absent — see the module docstring.
        placer_order_number=_text(orc, "orc_2", "orc_2_1"),
        filler_order_number=_text(orc, "orc_3", "orc_3_1"),
        order_control=_text(orc, "orc_1"),
        requested_code=_text(rxo, "rxo_1", "rxo_1_1"),
        requested_display=requested_display,
        give_amount=_text(rxo, "rxo_2") or None,
        give_units=_codeable(rxo, "rxo_4") or None,
        dose_form=_codeable(rxo, "rxo_5") or None,
        strength_value=_text(rxo, "rxo_18") or None,
        strength_units=_codeable(rxo, "rxo_19") or None,
        deliver_to=_text(rxo, "rxo_8") or None,
        indication=_codeable(rxo, "rxo_20") or None,
        route=_codeable(rxr, "rxr_1") if rxr is not None else None,
        message_control_id=_text(msh, "msh_10"),
        sending_application=_text(msh, "msh_3", "msh_3_1") or None,
        item_count=len(rxo_segments),
        is_compound=bool(segments.get("RXC")),
    )


def _patient_name(pid: Element | None) -> str:
    """PID-5 as a display name, e.g. ``"JOHN DOE"``.

    PID-5 is an XPN, so the surname and given name are separate components and neither is
    the whole name. Joining them here means downstream code has one string to treat as
    PHI, instead of three components that each have to be remembered.
    """
    if pid is None:
        return ""
    family = _text(pid, "pid_5", "pid_5_1")
    given = _text(pid, "pid_5", "pid_5_2")
    return " ".join(part for part in (given, family) if part)
