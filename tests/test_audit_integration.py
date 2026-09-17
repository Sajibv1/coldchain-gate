"""Integration test for the audit trail against the live public HAPI sandbox.

Deselected by default; run with `make integration`.

This file exists because of a specific failure mode. The chain hashes the payload *as we
build it*, and `verify_chain` later recomputes it from the payload *as the server stored
it*. Those are the same bytes only if the server round-trips the resource without
normalising it. If it does not — if it reformats an instant, reorders an array, or drops an
element it does not like — then every honest trail verifies as tampered, and the whole
feature is worse than useless.

That is not hypothetical. Two measured findings are pinned here:

* ``AuditEvent.outcome`` is a primitive ``code`` in R4. Built as a ``Coding`` (the shape the
  same field has on most resources) HAPI answers **201** and silently discards it, so the
  trail records no outcome while looking like it worked.
* ``AuditEvent.entity.what`` is resolved server-side. A reference to a resource that does
  not exist is rejected with **400 HAPI-1094** — which is why the hold path omits ``entity``
  entirely when no MedicationRequest was found, rather than citing a placeholder.

Both were invisible offline. Hence the read-back assertions.
"""

from __future__ import annotations

import uuid

import pytest

from app.audit.chain import link
from app.config import Settings
from app.fhir import build
from app.fhir.client import FhirClient, FhirError
from app.fhir.seed import build_dataset, purge, write_all

pytestmark = pytest.mark.integration

SETTINGS = Settings(demo_namespace="pytest-coldchain")


@pytest.fixture
def client() -> FhirClient:
    with FhirClient(SETTINGS.fhir_root, timeout_s=SETTINGS.fhir_timeout_s) as c:
        yield c


@pytest.fixture
def dataset():
    data = build_dataset(SETTINGS, token=uuid.uuid4().hex[:8])
    yield data
    with FhirClient(SETTINGS.fhir_root, timeout_s=SETTINGS.fhir_timeout_s) as c:
        purge(c, data, raise_on_error=False)


@pytest.fixture
def written(client: FhirClient, dataset) -> list[str]:
    """Ids of audit events written by a test, deleted afterwards.

    Each test gets a fresh one-shot cleanup so a mid-test assertion failure still leaves the
    shared sandbox clean.
    """
    ids: list[str] = []
    yield ids
    for resource_id in ids:
        client.delete("AuditEvent", resource_id)


def _write(
    client: FhirClient,
    dataset,
    written: list[str],
    *,
    recorded: str | None = None,
    outcome_code: str = build.OUTCOME_SUCCESS,
    entity_refs: list[str] | None = None,
) -> tuple[dict, object]:
    """Write one audit event against ``dataset``'s prescription, then read it back.

    ``recorded`` defaults to a value varying per call: HAPI rejects identical content as a
    duplicate (412, HAPI-2840). The pipeline is safe by construction because a real run's
    clock always differs; tests have to arrange it explicitly.
    """
    tag = uuid.uuid4().hex[:8]
    if recorded is None:
        recorded = f"2026-09-16T10:{int(tag[:2], 16) % 60:02d}:{int(tag[2:4], 16) % 60:02d}Z"
    if entity_refs is None:
        entity_refs = [f"MedicationRequest/{dataset.request['id']}"]

    payload = build.audit_payload(
        subtype_code="indent-validated",
        subtype_display="Cold-chain indent validated",
        recorded=recorded,
        outcome_code=outcome_code,
        outcome_desc="prescription and indent agree",
        agent_ref=f"Practitioner/{dataset.prescriber['id']}",
        entity_refs=entity_refs,
    )
    entry = link(None, payload)
    resource = build.audit_event(
        resource_id=SETTINGS.ns_id(f"auditevent-{tag}"), payload=payload, entry=entry
    )

    client.update(resource)
    written.append(resource["id"])
    back = client.read("AuditEvent", resource["id"])
    assert back is not None, "the AuditEvent did not read back"
    return back, entry


def test_the_server_stores_the_hashed_fields_verbatim(client: FhirClient, dataset, written) -> None:
    """The assumption the whole design rests on: what we hash is what the server keeps.

    Asserted field by field rather than as one blob, so a failure names the element that got
    normalised instead of just saying the round trip broke.
    """
    write_all(client, dataset)
    back, entry = _write(client, dataset, written)

    for field in build.HASHED_AUDIT_FIELDS:
        assert back.get(field) == entry.payload.get(field), (
            f"HAPI did not round-trip {field!r} unchanged: "
            f"wrote {entry.payload.get(field)!r}, read {back.get(field)!r}"
        )


def test_chain_entry_inverts_audit_event_on_real_server_data(
    client: FhirClient, dataset, written
) -> None:
    """End to end: write a link, fetch it, recover the exact entry.

    This is what lets a third party verify the trail with nothing but the server.
    """
    write_all(client, dataset)
    back, entry = _write(client, dataset, written)
    assert build.chain_entry(back) == entry


def test_outcome_survives_as_a_primitive_code(client: FhirClient, dataset, written) -> None:
    """The regression pin for the ``Coding`` mistake.

    Offline we can only assert the type we send. Only the server can tell us it keeps it.
    """
    write_all(client, dataset)
    back, _ = _write(client, dataset, written, outcome_code=build.OUTCOME_HOLD)
    assert back.get("outcome") == build.OUTCOME_HOLD, (
        "outcome was dropped on write — R4 types it as a primitive code, and a Coding is "
        "discarded silently with a 201"
    )


def test_a_reference_to_a_missing_resource_is_rejected(client: FhirClient) -> None:
    """Why the hold path omits ``entity`` instead of citing a placeholder.

    HAPI resolves audit entity references. If it did not, an invented reference would sit in
    the trail looking authoritative; because it does, a placeholder would make the write
    fail — and it would fail on exactly the path (no correlated prescription) where recording
    an audit event matters most.
    """
    payload = build.audit_payload(
        subtype_code="indent-validated",
        subtype_display="Cold-chain indent validated",
        recorded="2026-09-16T11:00:00Z",
        outcome_code=build.OUTCOME_HOLD,
        outcome_desc="no matching prescription",
        agent_ref=f"Practitioner/{SETTINGS.ns_id('practitioner-prescriber')}",
        entity_refs=[f"MedicationRequest/{SETTINGS.ns_id('does-not-exist')}"],
    )
    entry = link(None, payload)
    resource = build.audit_event(
        resource_id=SETTINGS.ns_id(f"auditevent-{uuid.uuid4().hex[:8]}"),
        payload=payload,
        entry=entry,
    )

    with pytest.raises(FhirError) as excinfo:
        client.update(resource)
    assert excinfo.value.status == 400


def test_an_audit_event_without_an_entity_is_accepted(
    client: FhirClient, dataset, written
) -> None:
    """The counterpart: omitting ``entity`` is valid, so the hold path can always record."""
    write_all(client, dataset)
    back, entry = _write(client, dataset, written, entity_refs=[])
    assert "entity" not in back
    assert build.chain_entry(back) == entry


def test_a_fetched_chain_verifies_from_server_data_alone(client: FhirClient, dataset) -> None:
    """Read a multi-link trail back by search and verify it, trusting nothing local.

    The point of the exercise: verification runs off the server's copy, not ours.
    """
    from app.audit.chain import verify_chain

    write_all(client, dataset)
    written: list[str] = []
    entries = []
    # Timestamps are offset by a per-run nonce. HAPI's duplicate check (412, HAPI-2840) reads
    # *content*, and a deleted event is only soft-deleted — it still counts. Three events with
    # hardcoded times therefore collide with the previous run's leftovers and the second run
    # of this test fails. A real run's clock never repeats, so the pipeline cannot hit this;
    # a test with fixed literals hits it every time.
    nonce = int(uuid.uuid4().hex[:4], 16) % 40
    try:
        for index in range(3):
            payload = build.audit_payload(
                subtype_code="indent-validated",
                subtype_display="Cold-chain indent validated",
                recorded=f"2026-09-16T12:{nonce + index:02d}:00Z",
                outcome_code=build.OUTCOME_SUCCESS,
                outcome_desc="prescription and indent agree",
                agent_ref=f"Practitioner/{dataset.prescriber['id']}",
                entity_refs=[f"MedicationRequest/{dataset.request['id']}"],
            )
            # Chain the links the way the pipeline does: each covers its predecessor.
            entry = link(entries[-1] if entries else None, payload)
            entries.append(entry)
            resource_id = SETTINGS.ns_id(f"auditevent-chain-{index}-{uuid.uuid4().hex[:6]}")
            client.update(
                build.audit_event(resource_id=resource_id, payload=payload, entry=entry)
            )
            written.append(resource_id)

        # Rebuild the chain purely from what the server returns.
        fetched = [client.read("AuditEvent", rid) for rid in written]
        recovered = [build.chain_entry(r) for r in fetched]
        assert all(r is not None for r in recovered)

        assert verify_chain(recovered) == [], "an honest trail fetched from the server must verify"
    finally:
        for resource_id in written:
            client.delete("AuditEvent", resource_id)
        purge(client, dataset, raise_on_error=False)
