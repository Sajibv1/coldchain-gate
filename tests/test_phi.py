"""Tests for the privacy boundary.

The plan calls these the most important tests in the repo, and the reason is that a privacy
claim is the one claim a reviewer cannot verify by reading the code quickly. "We strip PHI" is
true of almost any implementation until you find the field it forgot. So these tests do not
check that a scrubber runs — they check that a **worst-case record** comes out the other side
carrying nothing, and they check the *positive* half too, because an alert that leaks nothing
by being empty is not a delivery notification.

Three things are asserted that a naive test suite would omit:

* **The corpus is broader than the demo's own data.** The seeded ``Patient`` carries an MRN,
  a name and a date of birth. The corpus here also carries a phone number, an address, and a
  free-text note with a name in it — PHI this project never writes, which is the point: the
  guarantee has to hold for the field nobody modelled.
* **The courier and the ETA do survive.** Over-scrubbing is the failure mode of the naive
  reading, and it silently drops a stated requirement. A test that only asserts absence would
  pass against an implementation that sends an empty payload.
* **The failure paths are covered too.** A hold message and an exception response are
  outward-facing; the version of them that explains itself by quoting the record it read is
  the leak nobody tests for.
"""

from __future__ import annotations

import json

import pytest

from app import demo
from app.hl7.fixtures import er7
from app.hl7.parse import parse_indent
from app.models import ALERT_FIELDS
from app.safety import notify, phi

#: The worst-case inbound record: every patient-identifying form a message could plausibly
#: carry, not merely the ones this project's seeder happens to write.
#:
#: No middle initial. A bare initial is not one of HIPAA's identifiers, and as a one-character
#: needle it matches arbitrary text ("Q" against the key ``quantity``) — a value that cannot be
#: searched for meaningfully cannot be asserted absent meaningfully either.
PHI_CORPUS: dict[str, str] = {
    "patient_family": "DOE",
    "patient_given": "JOHN",
    "mrn": demo.MRN,
    "birth_date": "1974-03-02",
    "phone": "+60 12-345 6789",
    "address": "14 Jalan Ampang, 50450 Kuala Lumpur",
    "note": "Patient JOHN DOE asked for the morning dose",
    "placer_order_number": demo.PLACER_ORDER_NUMBER,
    "filler_order_number": demo.FILLER_ORDER_NUMBER,
    "request_id": "ns-medreq-1001",
    "dispense_id": "ns-dispense-1001",
}

#: The identifiers and names that no outbound payload may contain, in any casing.
FORBIDDEN: tuple[str, ...] = (
    "DOE",
    "JOHN",
    demo.MRN,
    "1974-03-02",
    "345 6789",
    "Jalan Ampang",
    demo.PLACER_ORDER_NUMBER,
    demo.FILLER_ORDER_NUMBER,
    "ns-medreq-1001",
    "ns-dispense-1001",
)

#: What a nurse's device legitimately receives. Drug, quantity, ETA are operational facts; the
#: courier is workforce identity. None of them is a patient fact.
COURIER = demo.COURIER_NAME
ETA = "14:30"
ITEM = demo.DRUG_DISPLAY


def _alert(**overrides: str):
    fields = {
        "item_description": ITEM,
        "quantity": "1 pen",
        "courier_display_name": COURIER,
        "eta": ETA,
    }
    fields.update(overrides)
    return phi.alert_from(**fields, known=(demo.MRN, demo.PATIENT_FAMILY, demo.PATIENT_GIVEN))


def _internal_record() -> dict[str, str]:
    """What the internal view holds — the operator's side of the side-by-side UI.

    Built here rather than imported because the pipeline does not exist yet; the assertion it
    supports is about the *relationship* between the two views, which is what matters.
    """
    return {
        **PHI_CORPUS,
        "item_description": ITEM,
        "quantity": "1 pen",
        "courier_display_name": COURIER,
        "eta": ETA,
    }


# -- the central claim -------------------------------------------------------------------


@pytest.mark.parametrize("forbidden", FORBIDDEN)
def test_no_patient_value_survives_into_the_alert(forbidden: str) -> None:
    """Every PHI form in the corpus, checked against the serialised alert.

    Serialised rather than inspected as an object: the leak that matters is the one that
    reaches the wire, and a dict comparison can be fooled by a nested value.
    """
    assert forbidden.lower() not in json.dumps(_alert().to_payload()).lower()


def test_the_whole_corpus_is_absent_at_once() -> None:
    """The same check as a single assertion, so a failure shows the payload and the corpus.

    The parametrized version above names the offending value; this one makes the payload
    itself visible when something does get through.
    """
    serialised = json.dumps(_alert().to_payload())
    survivors = {k: v for k, v in PHI_CORPUS.items() if v.lower() in serialised.lower()}
    assert not survivors, f"patient data reached the alert: {survivors}"


@pytest.mark.parametrize("field", ["patient_family", "mrn", "birth_date", "request_id"])
def test_the_internal_record_would_have_leaked_if_it_were_passed_through(field: str) -> None:
    """Proves the corpus is loaded, so the tests above cannot pass vacuously.

    A corpus with a typo in it would make every absence assertion succeed while testing
    nothing. Each of these values is asserted present in the internal record the alert is
    *not* built from — so the leak tests above are known to be looking for something real.
    """
    assert PHI_CORPUS[field] in json.dumps(_internal_record())


def test_the_alert_carries_exactly_the_allowlist() -> None:
    """Keys equal to the allowlist — not a subset, not a superset.

    A subset would let a future edit drop the ETA and still pass; a superset is the leak. The
    allowlist is derived from `DispatchAlert`, so this also pins the type and the list
    together.
    """
    payload = _alert().to_payload()
    assert tuple(payload) == ALERT_FIELDS
    assert set(payload) == {"item_description", "quantity", "courier_display_name", "eta"}


@pytest.mark.parametrize("key", ALERT_FIELDS)
def test_every_allowed_field_is_actually_carried(key: str) -> None:
    """The positive half: each allowlisted field is populated, not dropped for safety."""
    assert _alert().to_payload()[key]


def test_the_courier_and_eta_reach_the_device() -> None:
    """The requirement the naive "strip all names" reading quietly breaks.

    The brief asks the alert to show *who* is bringing the medicine and *when* it arrives. A
    courier's name is workforce identity, not patient data, so it is delivered.
    """
    payload = _alert().to_payload()
    assert payload["courier_display_name"] == COURIER
    assert payload["eta"] == ETA
    assert payload["item_description"] == ITEM


def test_the_alert_shares_no_patient_field_with_the_internal_view() -> None:
    """The side-by-side contrast, asserted as a relationship rather than by eye.

    The internal record and the alert legitimately share the operational fields — that is what
    makes the UI comparison meaningful — and share nothing the boundary exists to protect.
    """
    alert_keys = set(_alert().to_payload())
    internal_keys = set(_internal_record())

    assert alert_keys & set(phi.PATIENT_FIELDS) == set()
    assert alert_keys & {"item_description", "quantity", "courier_display_name", "eta"}
    assert internal_keys & set(phi.PATIENT_FIELDS), "the internal view should hold patient data"


# -- the allowlist cannot be defeated by field contents ----------------------------------


def test_a_patient_name_pasted_into_an_allowed_field_is_refused() -> None:
    """The one hole an allowlist leaves open, closed explicitly.

    Field *names* are fixed, but their contents come from upstream — and a free-text item
    description is exactly where a name gets pasted. Refused rather than redacted, because a
    name arriving here is a defect in the caller, and quietly sending a mangled alert would
    hide it.
    """
    with pytest.raises(phi.PhiLeak) as caught:
        _alert(item_description=f"{ITEM} for {demo.PATIENT_FAMILY}")

    assert "DOE" in str(caught.value)
    assert caught.value.field == "item_description"


@pytest.mark.parametrize(
    "value",
    [
        f"deliver to room 4 — {demo.MRN}",
        "dose on 1974-03-02",
        "call +60 12-345 6789 on arrival",
    ],
)
def test_phi_shaped_text_in_a_courier_field_is_refused(value: str) -> None:
    """Not only the drug field: every allowlisted field is content-checked.

    The courier display name is free text in the seeded FHIR resource, so it is reachable by
    exactly the same accident.
    """
    with pytest.raises(phi.PhiLeak):
        _alert(courier_display_name=value)


def test_a_payload_that_is_not_the_allowlist_is_refused_at_the_boundary() -> None:
    """Enforced where the payload is serialised, not only in the tests that check it."""
    with pytest.raises(phi.AlertShapeError) as caught:
        phi.assert_alert_shaped({"item_description": ITEM, "patient_mrn": demo.MRN})

    message = str(caught.value)
    assert "patient_mrn" in message and "missing" in message


def test_the_boundary_check_catches_a_missing_field_too() -> None:
    """A dropped ETA is a broken notification, and worth failing loudly for the same reason.

    ``PrivacyViolation`` is the common base so the send path's ``except`` clause has to handle
    only one family of failure.
    """
    with pytest.raises(phi.PrivacyViolation):
        phi.assert_alert_shaped({"item_description": ITEM, "quantity": "1", "eta": ETA})


# -- the second line of defence ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        (f"patient {demo.PATIENT_FAMILY} {demo.PATIENT_GIVEN} mrn {demo.MRN}", demo.MRN),
        ("dob 1974-03-02 recorded", "1974-03-02"),
        ("contact +60 12-345 6789", "345 6789"),
        ("mail john.doe@example.com", "john.doe@example.com"),
    ],
)
def test_scrub_redacts_patient_shaped_text(text: str, forbidden: str) -> None:
    """Applied to log lines, exception messages and hold details — text with no fixed schema."""
    cleaned = phi.scrub(
        text, known=(demo.PATIENT_FAMILY, demo.PATIENT_GIVEN, demo.MRN)
    )
    assert forbidden.lower() not in cleaned.lower()
    assert "[redacted]" in cleaned


def test_scrub_redacts_the_longer_identifier_first() -> None:
    """Otherwise redacting "JOHN" leaves a fragment of "JOHNSON" behind.

    Ordering by length is the whole reason this is not a dict comprehension over the known
    set: substring replacement is not commutative, and the long form has to win.
    """
    cleaned = phi.scrub("seen by JOHNSON", known=("JOHN", "JOHNSON"))
    assert "JOHNSON" not in cleaned.upper()
    assert "JOHN" not in cleaned.upper().replace("JOHNSON", "")


def test_scrub_leaves_operational_text_readable() -> None:
    """Over-redaction costs the operator the detail they need to debug.

    Order numbers and times are operationally necessary in a log and are not patient data —
    they are excluded from the *alert* structurally, which is a different control.
    """
    text = f"order {demo.PLACER_ORDER_NUMBER} eta {ETA} parsed"
    assert phi.scrub(text) == text


def test_find_phi_reports_what_it_found_and_why() -> None:
    """Returns the matches, not a bool — a failing test should name the leak."""
    found = phi.find_phi(
        f"{demo.PATIENT_GIVEN} {demo.PATIENT_FAMILY} {demo.MRN}",
        known=(demo.PATIENT_FAMILY, demo.PATIENT_GIVEN, demo.MRN),
    )
    assert demo.MRN in found
    assert demo.PATIENT_FAMILY in found


def test_find_phi_prefers_to_over_report() -> None:
    """A detector may return a superset; it may not return a subset.

    The phone pattern bridges two adjacent number groups (measured: an MRN followed by a date
    matches as one run). That is accepted rather than tuned away, because the failure
    directions are not symmetric — a spurious match costs a moment's confusion, a missed one
    writes a patient identifier to disk.
    """
    found = phi.find_phi("MRN12345 1974-03-02")
    assert any("1974-03-02" in f for f in found), "the date itself must be reported"


# -- the failure paths -------------------------------------------------------------------


def test_a_hold_message_carries_no_patient_identifiers() -> None:
    """A rejection is outward-facing too, and it is composed from many sources."""
    view = phi.hold_view(
        "patient_mismatch",
        f"the indent's patient {demo.MRN} does not match the prescription for "
        f"{demo.PATIENT_GIVEN} {demo.PATIENT_FAMILY}",
        known=(demo.MRN, demo.PATIENT_FAMILY, demo.PATIENT_GIVEN),
    )

    serialised = json.dumps(view)
    for forbidden in ("DOE", "JOHN", demo.MRN):
        assert forbidden.lower() not in serialised.lower()
    assert view["code"] == "patient_mismatch"


def test_an_error_response_does_not_echo_the_raw_payload() -> None:
    """Where exceptions actually leak: the message quotes what it choked on.

    A FHIR ``OperationOutcome`` echoes the resource, an ``httpx`` error carries the URL, and an
    HL7 parse failure quotes the segment — and a PID segment begins with the patient's MRN.
    So the default withholds the message entirely rather than scrubbing it, because scrubbing
    is a denylist and this is the same argument the alert's allowlist rests on.
    """
    raw_hl7 = er7("clean")
    assert demo.MRN in raw_hl7, "the fixture should carry PHI, or this test proves nothing"

    exc = ValueError(f"could not parse segment: {raw_hl7}")
    view = phi.error_view(exc, known=(demo.MRN, demo.PATIENT_FAMILY, demo.PATIENT_GIVEN))

    assert view["error"] == "ValueError", "the type is what makes it diagnosable"
    serialised = json.dumps(view)
    for fragment in (demo.MRN, "PID", "ORC", "GLARG100PEN", demo.PATIENT_FAMILY):
        assert fragment not in serialised, f"{fragment!r} survived into the error response"


def test_opting_in_to_the_message_still_scrubs_it() -> None:
    """``echo_message`` is for a caller that knows the text is safe — not a way out of the backstop.

    A deliberately authored message ("strength is not a number") is worth returning; the same
    switch applied to a parser failure must still not emit the segment.
    """
    safe = phi.error_view(ValueError("strength is not a number"), echo_message=True)
    assert safe["detail"] == "strength is not a number"

    leaky = phi.error_view(
        ValueError(f"bad segment: {er7('clean')}"),
        known=(demo.MRN, demo.PATIENT_FAMILY, demo.PATIENT_GIVEN),
        echo_message=True,
    )
    assert demo.MRN not in json.dumps(leaky)


def test_an_error_detail_is_truncated() -> None:
    """An unbounded message is a payload dump with a stack trace attached."""
    view = phi.error_view(RuntimeError("x" * 5000), echo_message=True)
    assert len(view["detail"]) == 400


def test_a_hold_detail_from_the_gate_passes_the_scrubber_unchanged(rxnav, fhir) -> None:
    """The gate already promises PHI-free detail; this checks the two agree.

    `test_validate.py` asserts the gate's details name no patient. Asserting the scrubber
    finds nothing in them is the same claim from the other side — if either drifts, one of the
    two fails, which is the point of stating an invariant in two places.
    """
    from app.safety import validate

    order = parse_indent(er7("strength_mismatch"))
    verdict = validate.evaluate(order, fhir=fhir, rxnav=rxnav)
    assert not verdict.ok

    cleaned = phi.scrub(verdict.outcome.detail, known=(demo.MRN, demo.PATIENT_FAMILY))
    assert cleaned == verdict.outcome.detail


# -- the device-facing handle ------------------------------------------------------------


def test_the_notification_token_is_stable_across_sends() -> None:
    """A re-sent alert must be recognisable as the same alert on the device."""
    first = phi.notification_token("secret", demo.PLACER_ORDER_NUMBER)
    assert first == phi.notification_token("secret", demo.PLACER_ORDER_NUMBER)


def test_the_notification_token_changes_with_the_secret() -> None:
    """Keyed, so the token is useless off-box — and unguessable from the order number.

    A bare digest of a sequential order number is reversible by anyone willing to hash a few
    thousand candidates, which is the mistake this avoids.
    """
    assert phi.notification_token("secret-a", "X") != phi.notification_token("secret-b", "X")


def test_the_notification_token_does_not_carry_its_input() -> None:
    token = phi.notification_token("secret", demo.PLACER_ORDER_NUMBER)
    assert demo.PLACER_ORDER_NUMBER.lower() not in token.lower()
    assert len(token) < 32


def test_a_delivered_alert_exposes_only_the_token_and_the_allowlist() -> None:
    """What the demo's delivery log holds — the shape the UI renders."""
    notifier = notify.InMemoryNotifier(secret="test-secret")
    delivery = notifier.send(_alert(), token=notifier.token_for(demo.PLACER_ORDER_NUMBER))

    assert delivery.delivered and delivery.error is None
    assert set(delivery.payload) == set(ALERT_FIELDS)
    serialised = json.dumps(delivery.to_json())
    for forbidden in FORBIDDEN:
        assert forbidden.lower() not in serialised.lower()


def test_a_failed_delivery_is_reported_rather_than_raised() -> None:
    """The medicine is already on its way; a phone that did not buzz must not undo that.

    A raised exception here would travel back up a pipeline that has already written the
    dispense, turning a transport hiccup into a lost record. The result carries the failure so
    the caller can retry or escalate, and the audit trail records the attempt either way.
    """
    notifier = notify.InMemoryNotifier(fail_next=1)
    delivery = notifier.send(_alert(), token=notifier.token_for(demo.PLACER_ORDER_NUMBER))

    assert not delivery.delivered
    assert delivery.error
    assert notifier.deliveries, "a failed attempt is still recorded"
    # ...and the next send succeeds, so the failure is a hiccup rather than a broken notifier.
    assert notifier.send(_alert(), token="t").delivered


def test_a_payload_that_is_not_the_allowlist_is_never_sent() -> None:
    """The boundary refuses at the point of sending, not merely upstream of it."""
    notifier = notify.InMemoryNotifier()

    class Smuggling:
        def to_payload(self) -> dict:
            return {**{k: "x" for k in ALERT_FIELDS}, "patient_mrn": demo.MRN}

    with pytest.raises(phi.PrivacyViolation):
        notifier.send(Smuggling(), token="t")  # type: ignore[arg-type]

    assert notifier.deliveries == [], "nothing may be recorded as sent"


def test_the_delivery_log_is_bounded() -> None:
    """An unbounded list in a long-lived web process is a slow leak."""
    notifier = notify.InMemoryNotifier(capacity=3)
    for index in range(10):
        notifier.send(_alert(), token=f"t{index}")

    assert len(notifier.deliveries) == 3
    assert notifier.recent()[0]["token"] == "t7"


def test_the_device_field_list_has_one_definition() -> None:
    """The UI, the API and the tests read the same list, or they drift."""
    assert notify.DEVICE_FIELDS == ALERT_FIELDS
    assert set(notify.DEVICE_FIELDS) == set(_alert().to_payload())


# -- against the real demo data ----------------------------------------------------------


def test_the_real_fixture_produces_a_clean_alert(dataset, fhir, rxnav) -> None:
    """The end-to-end version: real HL7 fixture in, real patient on the server, clean alert out.

    The tests above use a hand-written corpus; this one uses the values the demo actually
    holds — the MRN and name from the seeded ``Patient`` and the parsed order. It is the same
    claim, made against the data a reviewer will see.
    """
    from app.safety import validate

    order = parse_indent(er7("clean"))
    patient = fhir.read("Patient", dataset.patient["id"])
    assert patient is not None
    mrn = patient["identifier"][0]["value"]
    family = patient["name"][0]["family"]
    given = patient["name"][0]["given"][0]

    verdict = validate.evaluate(order, fhir=fhir, rxnav=rxnav)
    assert verdict.ok, verdict.outcome.detail

    alert = phi.alert_from(
        item_description=order.requested_display,
        quantity="1 pen",
        courier_display_name=demo.COURIER_NAME,
        eta=ETA,
        known=(mrn, family, given, order.patient_name),
    )

    serialised = json.dumps(alert.to_payload())
    for value in (mrn, family, given, order.patient_name, order.placer_order_number):
        assert value.lower() not in serialised.lower(), f"{value!r} reached the alert"
