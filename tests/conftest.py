"""Shared offline test harness: replay recorded RxNav responses, and a fake FHIR server.

Both seams exist so the *unit* suite can exercise the real client code without a network.
That matters more here than usual: the brief grades code lookup over string matching, and
the drug-matching logic lives in the interaction between the RxNav client and the
normalizer. Stubbing the client with a hand-written fake would test my assumptions about
RxNav's response shapes rather than RxNav's actual ones — so the RxNav side replays
**recorded real payloads** (see ``tests/data/rxnav_responses.json``).

The FHIR side needs no recording: the requests are ours and the payloads come from the
project's own builders, so a small router over ``seed.build_dataset`` is both faithful and
impossible to drift from the seeded demo.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from app.config import Settings
from app.fhir import seed
from app.fhir.client import FhirClient
from app.rxnorm.client import RxNavClient
from app.rxnorm.replay import RecordedRxNavTransport, replay_key

DATA = Path(__file__).parent / "data"
RECORDED_RXNAV = DATA / "rxnav_responses.json"

#: A fixed namespace: these tests never touch a server, so the ids only need to be stable.
TEST_SETTINGS = Settings(demo_namespace="test-ns")


#: The replay transport and its key function live in ``app.rxnorm.replay``. They moved
#: there when the deployed demo (``api/index.py``) needed them too, and a production
#: entry point importing test code is a dependency in the wrong direction. Re-exported
#: under their original names so nothing below this line, and no test importing from
#: this module, has to change.
_replay_key = replay_key


@pytest.fixture(scope="session")
def rxnav_responses() -> dict[str, Any]:
    return json.loads(RECORDED_RXNAV.read_text())


@pytest.fixture
def rxnav(rxnav_responses: dict[str, Any]) -> RxNavClient:
    """An RxNavClient backed by recorded responses. No network."""
    with RxNavClient(
        "https://rxnav.nlm.nih.gov/REST",
        transport=RecordedRxNavTransport(rxnav_responses),
    ) as client:
        yield client


@pytest.fixture
def rxnav_aliased(rxnav_responses: dict[str, Any]) -> Callable[..., RxNavClient]:
    """Build an RxNavClient that serves one concept's payloads under a second rxcui.

    Exists so the (ingredient, strength, dose form) dedup in ``candidates_for_display`` can
    be tested at all. The recorded data cannot exercise it: RxNorm's several concepts for one
    formulation are consistently *unexpandable* over REST (``properties`` answers ``200 {}``),
    so they are dropped as unusable before dedup ever sees them. The payloads replayed here
    are the real ones — only the rxcui identity is invented.

    The synthetic search lists both rxcuis within the score band, and both names are written
    so that they cover every token of ``display``; that is the state dedup is there to
    collapse.
    """
    clients: list[RxNavClient] = []

    def make(*, alias: str, original: str, display: str) -> RxNavClient:
        by_key = {_replay_key(url): entry for url, entry in rxnav_responses.items()}
        names = [
            {"rxcui": original, "score": "20.0", "name": f"3 ML {display}"},
            {"rxcui": alias, "score": "19.5", "name": f"3 ML {display}"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/approximateTerm.json"):
                return httpx.Response(200, json={"approximateGroup": {"candidate": names}})
            if path.endswith("/rxcui.json"):
                return httpx.Response(200, json={"idGroup": {}})
            if f"/rxcui/{alias}/" in path:
                # Serve the alias from the real concept's payloads.
                path = path.replace(f"/rxcui/{alias}/", f"/rxcui/{original}/")
                key = (path, tuple(sorted(parse_qsl(request.url.query.decode()))))
            else:
                key = _replay_key(str(request.url))
            entry = by_key.get(key)
            if entry is None:
                raise AssertionError(f"aliased fixture has no response for {path}")
            return httpx.Response(entry["status"], json=entry["json"])

        client = RxNavClient(
            "https://rxnav.nlm.nih.gov/REST", transport=httpx.MockTransport(handler)
        )
        clients.append(client)
        return client

    yield make

    for client in clients:
        client.close()


def fhir_transport(resources: Sequence[JSON]) -> httpx.MockTransport:
    """A read-only in-memory FHIR server over ``resources``.

    Implements exactly what the gate asks for — identifier search and instance read — and
    404s anything else, so a test cannot accidentally depend on an endpoint the real server
    would answer differently.

    Takes an arbitrary resource list rather than a dataset so a test can pose scenarios the
    seeder does not produce: two prescriptions sharing an order number, a request pointing
    at a missing patient, a request coded by ``medicationReference``.
    """

    known = {r["resourceType"] for r in resources}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            return httpx.Response(405, json={"resourceType": "OperationOutcome"})

        parts = [p for p in request.url.path.split("/") if p]

        # The client's base_url carries a path (`/baseR4` on the public server), so the
        # resource type is not necessarily the first segment. Locate it by name instead of
        # by position, which also keeps this working if the base path changes.
        for index, segment in enumerate(parts):
            if segment in known:
                parts = parts[index:]
                break
        else:
            return httpx.Response(404, json={"resourceType": "OperationOutcome"})

        resource_type, *rest = parts

        if not rest or rest[0].startswith("_"):
            identifier = request.url.params.get("identifier")
            if not identifier:
                return httpx.Response(400, json={"resourceType": "OperationOutcome"})
            system, _, value = identifier.partition("|")
            matches = [
                r
                for r in resources
                if r["resourceType"] == resource_type
                and any(
                    i.get("system") == system and i.get("value") == value
                    for i in (r.get("identifier") or [])
                )
            ]
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": len(matches),
                    "entry": [{"resource": r} for r in matches],
                },
            )

        resource_id = rest[0]
        for resource in resources:
            if resource["resourceType"] == resource_type and resource["id"] == resource_id:
                return httpx.Response(200, json=resource)
        return httpx.Response(404, json={"resourceType": "OperationOutcome"})

    return httpx.MockTransport(handler)


@pytest.fixture
def settings() -> Settings:
    """The test settings, as a fixture so a test module need not import this one.

    ``tests/`` is not a package, so ``from .conftest import ...`` is not available; anything
    a test needs from here has to be a fixture.
    """
    return TEST_SETTINGS


@pytest.fixture
def dataset() -> seed.SeededDataset:
    return seed.build_dataset(TEST_SETTINGS)


@pytest.fixture
def fhir(dataset: seed.SeededDataset) -> FhirClient:
    """A FhirClient backed by an in-memory server over the demo dataset. No network."""
    with FhirClient(
        "https://hapi.fhir.org/baseR4", transport=fhir_transport(dataset.resources())
    ) as client:
        yield client


@pytest.fixture
def fhir_for() -> Callable[[Sequence[JSON]], FhirClient]:
    """Build a FhirClient over an arbitrary resource list, for negative scenarios."""
    clients: list[FhirClient] = []

    def make(resources: Sequence[JSON]) -> FhirClient:
        client = FhirClient("https://hapi.fhir.org/baseR4", transport=fhir_transport(resources))
        clients.append(client)
        return client

    yield make

    for client in clients:
        client.close()


@pytest.fixture
def writable_fhir(dataset: seed.SeededDataset) -> Iterator[FhirClient]:
    """A FhirClient over the demo dataset that also accepts writes.

    The read-only ``fhir`` fixture above is right for the gate, which only reads. The
    pipeline writes a dispense and an AuditEvent per step, so it needs a server that keeps
    them — and reusing the pipeline's own ``--dry-run`` transport means that the offline
    demo path is exercised by every pipeline test rather than only by hand.
    """
    from app.pipeline import InMemoryFhirTransport

    with FhirClient(
        "https://hapi.fhir.org/baseR4",
        transport=InMemoryFhirTransport(dataset.resources()),
    ) as client:
        yield client
