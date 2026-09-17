"""Minimal FHIR R4 REST client.

Four operations, and no clinical knowledge: read, identifier-search, update-with-client-id
(the idempotent upsert that `make seed` and retry-safety depend on), and delete. Resource
payload construction lives in `build.py`.

Two deliberate choices:

* **Synchronous httpx.** FastAPI runs plain `def` endpoints in a threadpool, so this costs
  nothing in the demo and removes an entire class of async bugs from the safety path.
* **Errors never echo the request body.** HAPI's error responses echo the offending
  resource, which in this system contains patient identifiers. A validation failure must
  not become a PHI leak through a stack trace, so the body is only attached when
  ``FHIR_DEBUG=1`` is set explicitly. `tests/test_phi.py` asserts this.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

JSON = dict[str, Any]


class FhirError(RuntimeError):
    """A FHIR interaction failed.

    ``safe_message`` is what may be logged or returned; it never contains a resource body.
    """

    def __init__(self, status: int | None, method: str, path: str, safe_message: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {safe_message}")
        self.status = status
        self.method = method
        self.path = path
        self.safe_message = safe_message


@dataclass
class FhirClient:
    """Thin wrapper over the FHIR RESTful API."""

    base_url: str
    timeout_s: float = 30.0
    #: Injection seam for tests: an httpx transport to use instead of the network.
    transport: httpx.BaseTransport | None = None
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_s,
            transport=self.transport,
            headers={
                "Accept": "application/fhir+json",
                "Content-Type": "application/fhir+json",
            },
        )

    # -- lifecycle ---------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FhirClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------------------

    def _debug(self) -> bool:
        return os.environ.get("FHIR_DEBUG") == "1"

    def _raise(self, resp: httpx.Response, method: str, path: str) -> None:
        detail = f"HTTP {resp.status_code}"
        if self._debug():
            # Opt-in only: this can contain PHI from an echoed resource.
            detail = f"{detail} | {resp.text[:500]}"
        raise FhirError(resp.status_code, method, path, detail)

    def _json_or_raise(self, resp: httpx.Response, method: str, path: str) -> JSON:
        if resp.status_code >= 400:
            self._raise(resp, method, path)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise FhirError(resp.status_code, method, path, "response was not JSON") from None

    # -- operations --------------------------------------------------------------------

    def read(self, resource_type: str, resource_id: str) -> JSON | None:
        """GET a resource by id. Returns ``None`` when it is not there.

        Both 404 and 410 mean "no such resource": HAPI soft-deletes, so reading an
        instance that was deleted answers **410 Gone** rather than 404. Treating only 404
        as absent would turn a deleted record into a crash instead of a hold — which is
        exactly the case the safety gate must handle, since a purged sandbox and a
        withdrawn prescription look identical from here.
        """
        path = f"/{resource_type}/{resource_id}"
        resp = self._client.get(path)
        if resp.status_code in (404, 410):
            return None
        return self._json_or_raise(resp, "GET", path)

    def search(self, resource_type: str, params: dict[str, str]) -> list[JSON]:
        """GET a search. Returns the list of matching resources (may be empty)."""
        path = f"/{resource_type}"
        resp = self._client.get(path, params=params)
        bundle = self._json_or_raise(resp, "GET", path)
        return [e["resource"] for e in bundle.get("entry", []) if "resource" in e]

    def search_by_identifier(
        self, resource_type: str, system: str, value: str
    ) -> list[JSON]:
        """Search on a token identifier, e.g. ``identifier=<system>|<value>``."""
        return self.search(resource_type, {"identifier": f"{system}|{value}"})

    def update(self, resource: JSON) -> JSON:
        """PUT a resource with a client-assigned id — create-or-replace, idempotent.

        This is the operation `make seed` is built on: running it twice leaves the server
        in the same state, so a sandbox purge is recoverable with one command.
        """
        resource_type = resource["resourceType"]
        resource_id = resource.get("id")
        if not resource_id:
            raise ValueError("update() requires a client-assigned id; use create() instead")
        path = f"/{resource_type}/{resource_id}"
        resp = self._client.put(path, json=resource)
        return self._json_or_raise(resp, "PUT", path)

    def create(self, resource: JSON) -> JSON:
        """POST a resource and let the server assign the id."""
        resource_type = resource["resourceType"]
        path = f"/{resource_type}"
        resp = self._client.post(path, json=resource)
        return self._json_or_raise(resp, "POST", path)

    def delete(self, resource_type: str, resource_id: str) -> None:
        """DELETE a resource. Already-gone is success, not an error.

        404 and 410 both mean there is nothing left to delete, which is the state the
        caller asked for. Which one comes back depends on whether the id ever existed —
        HAPI answers 410 for an instance it soft-deleted and 404 for one it never had.
        """
        path = f"/{resource_type}/{resource_id}"
        resp = self._client.delete(path)
        if resp.status_code in (404, 410):
            return
        if resp.status_code >= 400:
            self._raise(resp, "DELETE", path)
