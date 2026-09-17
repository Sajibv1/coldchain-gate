"""The HTTP surface, exercised offline.

Everything here runs against an in-memory FHIR server and the recorded RxNav payloads, so
the suite stays in the default (non-``integration``) selection and needs no network. The
route is only worth testing because it is thin: what these tests are actually pinning is
the *mapping* — which run status becomes which HTTP status, and which of the two views is
the one that leaves the process.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.hl7 import fixtures
from app.main import MAX_BODY_BYTES, Service, create_app
from app.safety import phi
from tests.conftest import RecordedRxNavTransport, TEST_SETTINGS

#: Everything a response must never contain, from `app/demo.py` and `app/hl7/fixtures.py`.
#: Listed here rather than imported so that a change to the fixture values cannot quietly
#: shrink what is checked. The message control id is included for the reason spelled out in
#: `tests/test_pipeline.py`: it is not a patient identifier, but it resolves like one.
PHI = ["JOHN", "DOE", "MRN12345", "INDENT-1001", "RX-1001", "MSG00001"]

#: The demo page is the one route allowed to carry patient values, and this is the entire
#: allowance. Its internal-record panel and its HL7 excerpt are hand-written markup showing the
#: *seeded* demo dataset — the page says so above them — because the argument the page makes is a
#: contrast: these four fields crossed to the phone, and everything in that panel did not. The
#: values have to be visible for the contrast to land. They are not fetched, so no value a
#: running server holds can reach them. ``MSG00001`` is deliberately *not* here: the control id
#: is the one identifier the page keeps off on purpose, since ``/health`` publishes the namespace
#: and ``namespace + control id`` reconstructs the audit-event id.
SEEDED_DEMO_VALUES = {"DOE", "JOHN", "MRN12345", "INDENT-1001", "RX-1001"}

#: The route that serves ``ui/index.html``.
PAGE_ROUTE = "/"


@pytest.fixture
def client(rxnav_responses: dict) -> Iterator[TestClient]:
    """A TestClient over a service wired to the offline transports.

    ``dry_run`` is forced rather than inherited so the suite cannot be flipped onto the
    public sandbox by whatever happens to be in ``.env``.
    """
    settings = Settings(**{**TEST_SETTINGS.model_dump(), "fhir_mode": "dry-run"})

    def factory() -> Service:
        return Service(
            settings,
            rxnav_transport=RecordedRxNavTransport(rxnav_responses),
        )

    with TestClient(create_app(factory)) as client:
        yield client


def assert_no_phi(payload: object) -> None:
    """Fail naming the value that leaked, not merely that something did."""
    blob = json.dumps(payload) if not isinstance(payload, str) else payload
    for value in PHI:
        assert value not in blob, f"{value} reached the HTTP response"


# -- the happy path ---------------------------------------------------------------------


def test_a_clean_indent_is_dispatched(client: TestClient) -> None:
    response = client.post("/indent/demo/clean")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "dispensed"
    assert body["code"] == "ok"
    assert body["notified"] is True


def test_the_response_carries_the_alert_the_device_would_receive(client: TestClient) -> None:
    body = client.post("/indent/demo/clean").json()

    assert set(body["alert"]) == {
        "item_description",
        "quantity",
        "courier_display_name",
        "eta",
    }
    assert body["alert"]["quantity"] == "1 pen"
    assert body["alert"]["courier_display_name"] == "M. OKAFOR"
    # An ETA is a wall-clock time, which is what a lock screen can render.
    assert len(body["alert"]["eta"]) == 5 and ":" in body["alert"]["eta"]


def test_the_response_carries_a_token_instead_of_an_order_number(client: TestClient) -> None:
    """The token is the correlation handle, and it is the only one on offer.

    Asserted as a pair with the PHI check below: it would be easy to satisfy "no order
    number in the response" by publishing nothing a caller could correlate on, which would
    make the boundary useless rather than correct.
    """
    body = client.post("/indent/demo/clean").json()

    assert body["token"]
    assert body["token"] not in PHI


def test_a_client_request_id_is_never_reflected(client: TestClient) -> None:
    """Trace headers are untrusted and commonly contain patient-linkable values."""
    response = client.post(
        "/indent",
        content=fixtures.er7("clean").encode(),
        headers={"x-request-id": "MRN12345"},
    )

    assert response.status_code == 200
    assert "request_id" not in response.json()
    assert_no_phi(response.text)


def test_the_audit_trail_travels_with_the_response_and_verifies(client: TestClient) -> None:
    body = client.post("/indent/demo/clean").json()

    assert [event["subtype"] for event in body["audit"]] == [
        "order-received",
        "formulation-verified",
        "dispense-written",
        "notification-sent",
    ]
    assert [event["sequence"] for event in body["audit"]] == [1, 2, 3, 4]


# -- the boundary ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    [
        "/indent/demo/clean",
        "/indent/demo/strength_mismatch",
        "/indent/demo/unresolvable",
        "/indent/demo/multi_item",
    ],
)
def test_no_response_contains_a_patient_value(client: TestClient, route: str) -> None:
    response = client.post(route)

    assert response.status_code == 200
    assert_no_phi(response.text)


def test_every_route_is_scanned_for_patient_values(client: TestClient) -> None:
    """The claim is "no route returns PHI", so the check has to *be* every route.

    The earlier scan covers the indent routes, which are the ones that obviously carry patient
    data. The ones worth pinning are the ones that look inert: ``/fixtures``, which had no test
    at all before this, and ``/health``, which reports the namespace and the courier —
    deliberately, the courier's name being workforce identity rather than patient data.

    ``/`` is a documented exception rather than a hole. It is asserted on its own terms at the
    end — see :data:`SEEDED_DEMO_VALUES` — instead of being skipped.

    The set of routes is read off the app rather than hand-listed, so a route added later fails
    here until it is scanned.
    """
    exercised = {
        "/": client.get("/"),
        "/health": client.get("/health"),
        "/fixtures": client.get("/fixtures"),
        "/indent": client.post("/indent", content=fixtures.er7("clean").encode()),
        "/indent/demo/{name}": client.post("/indent/demo/clean"),
    }

    # FastAPI's own documentation routes are excluded by name; they render this app's schema,
    # which is already a public description of the interface. The static mount carries an
    # empty path rather than "/", which is the one wart in reading the routes off the app.
    documented = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
    declared = set()
    for route in client.app.routes:
        path = getattr(route, "path", None)
        if path is None or path in documented:
            continue
        declared.add(path or "/")

    assert declared == set(exercised), (
        "a route was added or removed without being scanned for patient data: "
        f"{declared ^ set(exercised)}"
    )
    for path, response in exercised.items():
        assert response.status_code == 200, f"{path} answered {response.status_code}"
        if path != PAGE_ROUTE:
            assert_no_phi(response.text)

    # The page, handled as the narrow exception it is. Values in the allowance must still *be*
    # there — otherwise the panel could quietly empty out and nothing would notice — and every
    # other PHI value must be absent, the control id above all.
    page = exercised[PAGE_ROUTE].text
    for value in PHI:
        if value in SEEDED_DEMO_VALUES:
            assert value in page, f"the demo page's seeded panel lost {value!r}"
        else:
            assert value not in page, f"{value!r} reached the demo page"


def test_a_held_indent_notifies_nobody(client: TestClient) -> None:
    """No alert key at all on a hold, rather than an empty one.
    """
    body = client.post("/indent/demo/strength_mismatch").json()

    assert body["status"] == "held"
    assert body["code"] == "strength_mismatch"
    assert "alert" not in body
    assert "token" not in body
    assert "notified" not in body


def test_a_hold_still_records_a_trail(client: TestClient) -> None:
    """Stopping is an outcome, and outcomes are audited — the first two events only."""
    body = client.post("/indent/demo/strength_mismatch").json()

    assert [event["subtype"] for event in body["audit"]] == [
        "order-received",
        "formulation-held",
    ]


def test_the_hold_reason_is_scrubbed_of_anything_patient_shaped(client: TestClient) -> None:
    body = client.post("/indent/demo/strength_mismatch").json()

    # The detail names the drugs and the strengths, because that is the clinical finding a
    # pharmacist has to act on. It names no patient, which is the line `phi.hold_view` holds.
    assert "insulin glargine" in body["detail"]
    assert_no_phi(body)


# -- transport handling ------------------------------------------------------------------


def test_a_message_with_lf_terminators_is_accepted(client: TestClient) -> None:
    """What curl, a heredoc, or a copy-paste actually sends.

    Without the normalisation in ``read_er7`` this is a hold for every reviewer who tries
    the documented example, and the reason would be invisible.
    """
    message = fixtures.er7("clean").replace("\r", "\n")
    response = client.post("/indent", content=message.encode())

    assert response.status_code == 200
    assert response.json()["status"] == "dispensed"


def test_a_crlf_message_is_accepted(client: TestClient) -> None:
    message = fixtures.er7("clean").replace("\r", "\r\n")
    response = client.post("/indent", content=message.encode())

    assert response.json()["status"] == "dispensed"


def test_an_unreadable_body_is_a_hold_not_a_400(client: TestClient) -> None:
    """The deliberate choice: garbage is a clinical-style outcome with a code.

    A 4xx would put the failure in an access log nobody reads and leave no trail. A hold
    names the sending interface as the thing to fix and writes an AuditEvent saying so.
    """
    response = client.post("/indent", content=b"this is not HL7")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "held"
    assert body["code"] == "unparseable_indent"


def test_an_empty_body_is_also_a_hold(client: TestClient) -> None:
    response = client.post("/indent", content=b"")

    assert response.status_code == 200
    assert response.json()["status"] == "held"


def test_a_body_over_the_cap_is_refused_without_being_processed(client: TestClient) -> None:
    response = client.post("/indent", content=b"x" * (MAX_BODY_BYTES + 1))

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"


def test_an_unknown_fixture_names_the_ones_that_exist(client: TestClient) -> None:
    response = client.post("/indent/demo/nope")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "unknown_fixture"
    assert "clean" in body["fixtures"]


# -- process state -----------------------------------------------------------------------


def test_the_notifier_remembers_deliveries_across_requests(client: TestClient) -> None:
    """The point of a stateful notifier: it stands in for the phone, so it must persist.

    Per-request notifiers would pass every other test in this file and make the demo
    incoherent — the second indent would show a device that had forgotten the first.
    """
    client.post("/indent/demo/clean")
    client.post("/indent/demo/clean")

    service: Service = client.app.state.service
    assert len(service.notifier.deliveries) == 2


def test_a_failed_delivery_is_reported_and_does_not_undo_the_dispense(
    client: TestClient,
) -> None:
    service: Service = client.app.state.service
    service.notifier.fail_next = 1

    body = client.post("/indent/demo/clean").json()

    # The medicine is on its way whether or not the phone buzzed: the dispense stands and
    # the run is still `dispensed`, while the trail records that the send did not land.
    # `8` is the FHIR AuditEvent outcome code for a serious failure (`0` is success), which
    # is what makes this visible to anything reading the trail off the server.
    assert body["status"] == "dispensed"
    assert body["notified"] is False
    assert body["audit"][-1]["outcome"] == "8"


def test_the_health_view_reports_configuration_not_liveness(client: TestClient) -> None:
    body = client.get("/health").json()

    assert body["mode"].startswith("dry-run")
    assert body["fhir"] == "in-memory"
    assert body["indents_processed"] == 0
    # The injected transport describes itself, and the view repeats that rather than the
    # URL it was configured with. The *deployed* demo needs no such thing — ``api/index.py``
    # injects nothing and calls the live API — so this asserts the mechanism rather than the
    # deployment: whenever a transport can describe what it really reaches, that description
    # is what crosses, and a ``/health`` naming the upstream host would then be the one place
    # in the system claiming a live call that never happened.
    assert body["rxnav"] == RecordedRxNavTransport.description


def test_the_health_view_names_the_live_api_when_nothing_is_replayed() -> None:
    """The same field, with no transport injected, still reports the real endpoint.

    Without this the assertion above would pass on a hard-coded string, and the demo's
    honesty about replaying would be indistinguishable from never configuring RxNav at all.
    """
    settings = Settings(**{**TEST_SETTINGS.model_dump(), "fhir_mode": "dry-run"})

    with TestClient(create_app(lambda: Service(settings))) as client:
        assert client.get("/health").json()["rxnav"] == settings.rxnav_root


def test_the_mode_string_says_what_the_process_talks_to() -> None:
    """``mode`` is rendered verbatim by the demo page, so a bare word is not enough.

    The page composes "A live run against this server — {mode}.", which on the deployed demo
    read "...— sandbox.": true, and nothing a reader could use. A judge should not have to
    cross-reference ``fhir`` and ``rxnav`` to work out which configuration they are looking
    at, and the page has no other way to say it.

    Both services are constructed without a request leaving the process — ``Service.__init__``
    builds clients, it does not call them — so this stays offline.
    """
    base = TEST_SETTINGS.model_dump()

    offline = Service(Settings(**{**base, "fhir_mode": "dry-run"})).status()["mode"]
    live = Service(Settings(**{**base, "fhir_mode": "sandbox"})).status()["mode"]

    assert offline != live, "both configurations describe themselves identically"
    assert "in-memory" in offline, f"the offline mode does not say so: {offline!r}"
    assert "RxNav" in live and "HAPI" in live, f"the live mode does not say so: {live!r}"


def test_a_dispense_is_counted_as_delivered_and_never_as_held(client: TestClient) -> None:
    """The notification counters have to mean what their names say.

    This is the test that was missing when the field was ``notifications_held`` and computed
    from ``len(notifier.deliveries)`` — every send attempt, the successful ones included. It
    read ``1`` after a clean dispense: the one run in the demo where a notification is
    certainly *not* held back, reported under a name saying it was.

    A hold is the opposite case and moves neither number, because a held indent never reaches
    the notifier at all. Asserting that is what separates these counters from
    ``indents_processed``, which counts both.
    """
    before = client.get("/health").json()
    assert before["notifications_delivered"] == 0
    assert before["notifications_failed"] == 0

    assert client.post("/indent/demo/clean").json()["status"] == "dispensed"
    dispensed = client.get("/health").json()
    assert dispensed["notifications_delivered"] == 1
    assert dispensed["notifications_failed"] == 0

    assert client.post("/indent/demo/strength_mismatch").json()["status"] == "held"
    held = client.get("/health").json()
    assert held["notifications_delivered"] == 1, "a hold was counted as a delivery"
    assert held["notifications_failed"] == 0, "a hold was counted as a failed send"
    assert held["indents_processed"] == 2, "the hold did not reach the pipeline at all"


def test_the_ui_is_served_at_the_root(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# -- the seam itself ---------------------------------------------------------------------


def test_an_infrastructure_outage_is_a_503(client: TestClient) -> None:
    """Both dependency failures map to 503, which is the status a caller should retry.

    Driven by replacing the FHIR transport under the live service, because the interesting
    thing is the *mapping* — the pipeline's own tests already cover what an outage does to
    a run.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated outage", request=request)

    service: Service = client.app.state.service
    service.fhir._client._transport = httpx.MockTransport(refuse)

    response = client.post("/indent/demo/clean")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    # The outage detail names the exception type, which is the diagnosis, and nothing else.
    assert "ConnectError" in body["audit"][-1]["detail"]
    assert_no_phi(body)


@pytest.mark.parametrize("route", ["/indent", "/indent/demo/clean"])
def test_a_payload_that_is_not_the_allowlist_is_a_500_and_says_nothing(
    client: TestClient, route: str
) -> None:
    """The one route where the response body matters more than the status code.

    A ``PrivacyViolation`` escaping ``process()`` means a payload that was not the allowlist
    was about to reach a device. The route must refuse it *and* tell the caller nothing: the
    exception names the offending field and the leaked text, and both are the PHI that was
    about to leak. This is asserted against the raw response text, not the parsed body, so a
    detail added anywhere in the envelope is caught.

    Parametrized over *both* routes because the first cut of ``app/main.py`` guarded
    ``POST /indent`` and forgot ``POST /indent/demo/{name}`` — the exception escaped one and
    not the other. Both now share one ``dispatch`` helper; this is the test that would have
    caught the split.

    The trigger is planted on the notifier because that is where the exception genuinely
    comes from — ``process()`` mints the token immediately before sending — so this exercises
    the real propagation path out of ``run_in_threadpool`` rather than a stub for the route.
    """
    service: Service = client.app.state.service

    class Polluted:
        fail_next = 0
        deliveries: list = []

        def token_for(self, *parts: str) -> str:
            raise phi.PhiLeak("item_description", ["JOHN DOE", "MRN12345"])

        def send(self, alert: object, *, token: str) -> object:
            raise AssertionError("unreachable: the token is minted before the send")

    service.notifier = Polluted()  # type: ignore[assignment]

    content = fixtures.er7("clean").encode() if route == "/indent" else None
    response = client.post(route, content=content)

    assert response.status_code == 500
    assert response.json()["code"] == "privacy_violation"
    assert_no_phi(response.text)
    assert "PhiLeak" not in response.text
