"""FhirClient behaviour tests, driven by an httpx MockTransport.

Offline. The client is the boundary between our code and the server, so its status-code
mapping is worth pinning down: the safety gate reacts to "no such resource" by holding,
and it must not be able to tell a deleted record from a crashed server.

The 410 case is not hypothetical — it is what the live HAPI sandbox actually returned
after a `--purge`, and treating only 404 as absent would have turned a purged sandbox into
an exception instead of an orderly hold.
"""

from __future__ import annotations

import httpx
import pytest

from app.fhir.client import FhirClient, FhirError


def _client(handler) -> FhirClient:
    return FhirClient(
        "https://example.test/baseR4", timeout_s=1.0, transport=httpx.MockTransport(handler)
    )


@pytest.mark.parametrize("status", [404, 410])
def test_read_treats_missing_and_deleted_as_absent(status: int) -> None:
    client = _client(lambda request: httpx.Response(status))
    assert client.read("Patient", "gone") is None


def test_read_returns_the_resource_on_success() -> None:
    payload = {"resourceType": "Patient", "id": "p1"}
    client = _client(lambda request: httpx.Response(200, json=payload))
    assert client.read("Patient", "p1") == payload


def test_read_raises_on_server_error() -> None:
    """A 500 is not "absent" — collapsing it to None would silently skip validation."""
    client = _client(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(FhirError) as excinfo:
        client.read("Patient", "p1")
    assert excinfo.value.status == 500


def test_error_message_does_not_echo_the_response_body_by_default(monkeypatch) -> None:
    """HAPI echoes the offending resource in errors, and that resource carries PHI."""
    monkeypatch.delenv("FHIR_DEBUG", raising=False)
    phi = '{"resourceType":"OperationOutcome","issue":[{"diagnostics":"JOHN DOE MRN12345"}]}'
    client = _client(lambda request: httpx.Response(400, text=phi))

    with pytest.raises(FhirError) as excinfo:
        client.read("Patient", "p1")

    assert "JOHN DOE" not in str(excinfo.value)
    assert "MRN12345" not in str(excinfo.value)


@pytest.mark.parametrize("status", [404, 410])
def test_delete_is_idempotent(status: int) -> None:
    client = _client(lambda request: httpx.Response(status))
    client.delete("Patient", "gone")  # must not raise


def test_search_unwraps_the_bundle() -> None:
    bundle = {
        "resourceType": "Bundle",
        "entry": [{"resource": {"resourceType": "Patient", "id": "a"}}],
    }
    client = _client(lambda request: httpx.Response(200, json=bundle))
    assert client.search_by_identifier("Patient", "sys", "val") == [
        {"resourceType": "Patient", "id": "a"}
    ]


def test_search_sends_the_token_identifier_parameter() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"resourceType": "Bundle"})

    _client(handler).search_by_identifier("MedicationRequest", "urn:x", "INDENT-1")
    assert seen["identifier"] == "urn:x|INDENT-1"


def test_update_refuses_a_resource_without_an_id() -> None:
    """update() is PUT-with-client-id; silently POSTing instead would break idempotency."""
    client = _client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        client.update({"resourceType": "Patient"})
