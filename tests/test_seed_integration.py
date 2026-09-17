"""Integration test for the seeder — writes to the live public HAPI sandbox.

Deselected by default (see `addopts` in pyproject.toml); run with `make integration`.
It exists because the seeder's whole job is to prove the *write* path works, and a mocked
client would prove nothing about that.

A per-run token is applied to the dataset, so this cannot collide with a seeded demo, with
a previous run's leftovers, or with another candidate on the shared sandbox.

Confirmed here rather than assumed: HAPI rejects a write that duplicates an existing
resource (``HAPI-2840``), and it decides "duplicate" from content. Patient MRN, Practitioner
staff id, MedicationRequest order number, and even Location *name* all trigger it — which
is why the token has to cover the ward name too.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import Settings
from app.fhir import build
from app.fhir.client import FhirClient
from app.fhir.seed import (
    build_dataset,
    order_identifiers,
    purge,
    verify,
    write_all,
)

pytestmark = pytest.mark.integration

#: Namespaced separately from the demo so a test run cannot clobber `make seed` output.
SETTINGS = Settings(demo_namespace="pytest-coldchain")


@pytest.fixture
def client() -> FhirClient:
    with FhirClient(SETTINGS.fhir_root, timeout_s=SETTINGS.fhir_timeout_s) as c:
        yield c


@pytest.fixture
def dataset():
    data = build_dataset(SETTINGS, token=uuid.uuid4().hex[:8])
    yield data
    # Teardown runs even if the test failed, so a failure leaves no records behind.
    with FhirClient(SETTINGS.fhir_root, timeout_s=SETTINGS.fhir_timeout_s) as c:
        purge(c, data, raise_on_error=False)


def test_put_creates_then_replaces(client: FhirClient, dataset) -> None:
    """PUT with a client-assigned id is create-or-replace — the basis of idempotency."""
    first = client.update(dataset.patient)
    assert first["id"] == dataset.patient["id"]

    second = client.update(dataset.patient)
    assert second["id"] == dataset.patient["id"]

    # Same id, and the resource is still findable exactly once by its MRN.
    mrn = dataset.patient["identifier"][0]["value"]
    hits = client.search_by_identifier("Patient", build.SYSTEM_MRN, mrn)
    assert [h["id"] for h in hits] == [dataset.patient["id"]]


def test_seeded_medication_request_is_findable_by_order_number(
    client: FhirClient, dataset
) -> None:
    write_all(client, dataset)
    assert verify(client, dataset) == [], "seeder verification reported problems"


def test_reference_from_request_to_patient_resolves(client: FhirClient, dataset) -> None:
    """The linkage the safety gate depends on: a real Patient behind the reference."""
    write_all(client, dataset)
    request = client.read("MedicationRequest", dataset.request["id"])
    assert request is not None
    reference = request["subject"]["reference"]
    assert client.read(*reference.split("/")) is not None


def test_order_identifiers_are_searchable_after_a_tokenized_seed(
    client: FhirClient, dataset
) -> None:
    """Guards the token path: identifiers must be read off the resource, not the demo."""
    write_all(client, dataset)
    placer, filler = order_identifiers(dataset)
    assert placer not in ("INDENT-1001",), "token was not applied to the order numbers"
    for system, value in (
        (build.SYSTEM_PLACER_ORDER, placer),
        (build.SYSTEM_FILLER_ORDER, filler),
    ):
        hits = client.search_by_identifier("MedicationRequest", system, value)
        assert [h["id"] for h in hits] == [dataset.request["id"]]
