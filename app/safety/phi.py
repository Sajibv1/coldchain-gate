"""The privacy boundary — what may leave this system, and what may not.

Three arguments carry this module. Each is a place where the naive reading of "strip PHI"
gets it wrong, so each is stated rather than assumed.

**The outbound alert is built from an allowlist, never filtered by a denylist.** The novice
move is to regex out ``name``, ``dob``, ``mrn`` — which fails on the field nobody thought
of. So the alert is *constructed* from a fixed set of fields (``ALERT_FIELDS``, derived from
``DispatchAlert`` so the type and the allowlist cannot drift apart) and nothing else. The
guarantee is structural rather than procedural: a new patient field appearing upstream
cannot leak, because there is no code path that could carry it. There is deliberately no
function here that accepts a source object and returns "the source minus PHI" —
subtraction is the failure mode.

**A courier's name is not PHI.** It is workforce identity. HIPAA protects patient data, and
the courier and the nurse are both staff of the covered entity. Stripping the courier's name
buys no compliance and drops a stated requirement: the brief asks the alert to show *who* is
bringing the medicine. The defensible line is narrower, and easier to defend — *strip
everything patient-linkable; allow the workforce and operational fields the workflow
requires.*

**An identifier is PHI.** A FHIR, order, or request id names nobody, but it is linkable back
to the patient, so it is treated as patient-linkable and excluded from the alert. Where the
device needs something to correlate on, :func:`notification_token` produces a keyed,
non-reversible handle; the mapping from that handle back to the underlying records never
leaves this process.

**``scrub`` is a second line of defence, not the control.** An allowlist cannot protect
free-form text — a log line, an exception message, a hold's ``detail`` — because there is
nothing to allowlist against. So that text is redacted before it is written anywhere an
operator or reviewer will read it. It is explicitly *not* why the alert is safe, and no test
here rests on it doing that job.

**Residual risk, stated rather than hidden.** Drug, quantity, ETA and courier identify no
patient, but that is sufficient only *because* no patient identifier is present. On a shared
or shoulder-surfable device the drug itself can be sensitive — an oncology agent discloses a
diagnosis to whoever glances at the phone. This module does not solve that; a real
deployment needs device binding and per-user authorisation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from typing import Any

from app.models import ALERT_FIELDS, DispatchAlert

JSON = dict[str, Any]

#: Truncation length of a notification token. 96 bits is far beyond collision concerns for a
#: demo's worth of live alerts, and short enough to read out over a phone.
TOKEN_BYTES = 12

#: The fields that are patient-linkable, named so tests can assert their *absence* from the
#: alert and so a reviewer can see the intended boundary in one place.
#:
#: This is documentation, not a filter. Nothing here is used to remove anything — the alert
#: is safe because it is built from ``ALERT_FIELDS``, and a denylist is the technique this
#: module rejects. An entry missing from this tuple would not cause a leak.
PATIENT_FIELDS: tuple[str, ...] = (
    "patient_name",  # PID-5
    "patient_mrn",  # PID-3
    "birth_date",  # PID-7
    "address",  # PID-11
    "phone",  # PID-13/14
    "subject",  # FHIR MedicationRequest.subject
    "identifier",  # any FHIR identifier, incl. the order numbers
    "placer_order_number",  # ORC-2 — linkable to the request
    "filler_order_number",  # ORC-3
    "request_id",  # the FHIR resource id a dispense references
    "dispense_id",
)

#: Shape-based redaction for free text. Deliberately conservative: over-redacting an operator's
#: log costs a debugging detail, while under-redacting writes a patient identifier to disk.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    # A leading letter run plus five or more digits: "MRN12345", "PAT0042". Five, not four, so
    # that order numbers shaped like "RX-1001" survive in logs where they are operationally
    # useful — and they are the values the alert excludes structurally, not textually.
    ("mrn", re.compile(r"\b[A-Z]{2,5}[-]?\d{5,}\b")),
    ("phone", re.compile(r"(?<!\d)\+?\d[\d\s().-]{7,}\d(?!\d)")),
)


class PrivacyViolation(RuntimeError):
    """Base for "this payload must not leave the process".

    The send boundary catches this, so the schema check below and a content leak are refused
    the same way — both mean the same thing operationally: do not deliver, investigate.
    """


class PhiLeak(PrivacyViolation):
    """Raised when a value bound for an allowlisted field is itself patient-identifying.

    The allowlist is structural and cannot be defeated from upstream — but it constrains the
    *field names*, not what gets written into them. A patient name pasted into a free-text
    item description arrives through an allowed field and would be published. That single hole
    is what this exception closes: it is a bug in the caller, so it raises instead of silently
    redacting, and the alert is not sent.
    """

    def __init__(self, field: str, leaks: Iterable[str]) -> None:
        self.field = field
        self.leaks = sorted(set(leaks))
        super().__init__(f"{field} contains patient-identifying text: {', '.join(self.leaks)}")


class AlertShapeError(PrivacyViolation):
    """Raised when a payload is not exactly the allowlist.

    Separate from :class:`PhiLeak` because the cause is different: nothing identifying is
    present, the shape is simply not the one this system is allowed to emit.
    """


def notification_token(secret: str | bytes, *parts: str) -> str:
    """A keyed, non-reversible handle for correlating an alert on the device.

    An order number cannot go to the phone — it is linkable to the patient. But a nurse
    holding a phone and a pharmacist looking at a screen need to agree they mean the same
    delivery, and a random per-send value cannot do that when the same alert is re-sent.

    HMAC rather than a plain digest: a bare ``sha256("INDENT-1001")`` is trivially reversed by
    anyone who can guess the input space, which for a sequential order number is everyone. The
    secret stays server-side, so the token is stable across sends and useless off-box.
    """
    key = secret.encode() if isinstance(secret, str) else secret
    message = "\x1f".join(parts).encode()
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:TOKEN_BYTES]).decode().rstrip("=")


def find_phi(text: str, *, known: Iterable[str] = ()) -> list[str]:
    """Tokens in ``text`` that look patient-identifying, or match a known identifier.

    Two sources, because neither is sufficient alone. A pattern catches an identifier shape
    nobody enumerated, and the ``known`` list catches the forms no pattern describes — most
    obviously a patient's *name*, which has no distinguishing syntax at all.

    Returns what it found rather than a bool so a failing test names the leak instead of
    reporting that one exists somewhere.
    """
    found: list[str] = []
    for _, pattern in _PATTERNS:
        found.extend(m.group(0) for m in pattern.finditer(text))
    lowered = text.lower()
    found.extend(term for term in known if term and term.lower() in lowered)
    return found


def scrub(text: str, *, known: Iterable[str] = (), placeholder: str = "[redacted]") -> str:
    """Redact patient-identifying text from free-form text before it is logged or returned.

    Applied to exception messages, hold details, and log lines — never to the alert, which
    does not need scrubbing because it never contained the material in the first place. See
    the module docstring on why this is a backstop and not the control.
    """
    cleaned = text
    for _, pattern in _PATTERNS:
        cleaned = pattern.sub(placeholder, cleaned)
    # Longest first, so redacting "JOHN" cannot leave a fragment of "JOHNSON" behind.
    for term in sorted((t for t in known if t), key=len, reverse=True):
        cleaned = re.sub(re.escape(term), placeholder, cleaned, flags=re.IGNORECASE)
    return cleaned


def alert_from(
    *,
    item_description: str,
    quantity: str,
    courier_display_name: str,
    eta: str,
    known: Iterable[str] = (),
) -> DispatchAlert:
    """Build the only payload shape permitted to reach a nurse's device.

    The parameters are the allowlist, spelled out as keyword-only arguments so a caller cannot
    pass a domain object and hope. Every one is either an operational fact (drug, quantity,
    ETA) or workforce identity (the courier), never a patient fact.

    Each value is additionally checked for patient-identifying text. That is the one hole an
    allowlist leaves open — the field names are fixed, but their contents come from upstream —
    and it raises rather than redacting, because a patient name arriving in
    ``item_description`` is a defect in the caller, not a formatting problem.
    """
    values: Mapping[str, str] = {
        "item_description": item_description,
        "quantity": quantity,
        "courier_display_name": courier_display_name,
        "eta": eta,
    }
    for field, value in values.items():
        if leaks := find_phi(value, known=known):
            raise PhiLeak(field, leaks)
    return DispatchAlert(**values)


def hold_view(code: str, detail: str, *, known: Iterable[str] = ()) -> JSON:
    """The outbound shape for a rejection — the alert's counterpart on the failure path.

    A hold is *also* an outward-facing message, and the tempting version of it explains itself
    by quoting what it read: the patient, the order, the raw segment. The rejection path is
    therefore built from an allowlist too, and its free-text reason is scrubbed, because it is
    a string composed from many sources rather than a fixed set of fields.
    """
    return {
        "status": "held",
        "code": code,
        "detail": scrub(detail, known=known),
    }


def error_view(exc: BaseException, *, known: Iterable[str] = (), echo_message: bool = False) -> JSON:
    """An error response safe to return to a caller and to write to a log.

    ``echo_message`` defaults to False, and the default *is* the design. An exception raised
    while handling an inbound message very often quotes that message: an HL7 parser reports
    the segment it choked on, a FHIR ``OperationOutcome`` echoes the resource, an ``httpx``
    error carries the URL. A PID segment begins with the patient's MRN, so echoing the message
    hands the caller precisely what this module exists to withhold.

    Scrubbing it instead would be a denylist, and the argument this project makes about the
    alert applies verbatim here — a denylist fails on the field nobody enumerated. So the
    default withholds: the exception *type* is what makes a failure diagnosable, and it
    carries no payload. A caller that knows its message is safe — a deliberate
    ``ValueError("strength is not a number")`` — opts in, and even then the text is scrubbed
    and capped, because opting in should not mean opting out of the backstop.

    Contrast :func:`hold_view`, which does echo its text. The difference is authorship: a hold
    detail is composed by this system out of drug names and dimension names, and is asserted
    PHI-free by the gate's own tests. An exception message is composed by whatever failed.
    """
    view: JSON = {
        "status": "error",
        "error": type(exc).__name__,
        "detail": "the request could not be processed",
    }
    if echo_message:
        view["detail"] = scrub(str(exc), known=known)[:400]
    return view


def assert_alert_shaped(payload: Mapping[str, Any]) -> None:
    """Fail loudly if ``payload`` is not exactly the allowlist.

    Called on the boundary itself, so the invariant is enforced where the payload is
    serialised rather than only in the tests that check it. A key added here without being
    added to ``DispatchAlert`` — the likely shape of a future mistake — stops the send.
    """
    extra = set(payload) - set(ALERT_FIELDS)
    missing = set(ALERT_FIELDS) - set(payload)
    if extra or missing:
        raise AlertShapeError(
            "alert payload is not the allowlist: "
            + "; ".join(
                [f"unexpected {k!r}" for k in sorted(extra)]
                + [f"missing {k!r}" for k in sorted(missing)]
            )
        )
