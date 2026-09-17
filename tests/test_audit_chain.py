"""Tests for the audit trail: the hash chain, and the AuditEvent that carries it.

Two halves, and they fail for different reasons.

The chain half (`app/audit/chain.py`) is pure hashing, so it is tested exhaustively here
without a server: every way of tampering with a stored trail must be detected, and the
detection must *say which kind* of tampering it was.

The FHIR half is the round trip, and it is the load-bearing one. The chain is only worth
anything if a verifier holding nothing but the resources fetched from the server can
recompute the hashes. That requires `chain_entry` to be a true inverse of `audit_event`:
whatever `audit_event` embedded is exactly what `chain_entry` reads back. If those two ever
drift, `verify_chain` reports tampering on an untouched trail — the failure mode that makes
a tamper-evident log useless, because a warning that always fires is a warning nobody reads.

The `outcome` tests are a regression pin for a mistake that was invisible from the outside.
R4 types ``AuditEvent.outcome`` as a primitive ``code``. Building it as a ``Coding`` (the
shape the same field has on most other resources) is *accepted* by HAPI with a 201 and
then silently discarded, so every audit record would have claimed no outcome at all while
looking like it worked. Nothing but a read-back catches that, which is why the test asserts
on the round-tripped resource rather than on the dict we built.
"""

from __future__ import annotations

import copy

from app.audit.chain import (
    GENESIS_HASH,
    ChainEntry,
    build_chain,
    canonical,
    event_hash,
    link,
    verify_chain,
)
from app.fhir import build

# -- fixtures ----------------------------------------------------------------------------


def _payload(
    *,
    recorded: str = "2026-09-16T08:00:00Z",
    outcome_code: str = build.OUTCOME_SUCCESS,
    entity_refs: tuple[str, ...] = ("MedicationRequest/ns-medreq-1001",),
    **field_overrides,
) -> dict:
    """Build an audit payload.

    Parameters of `audit_payload` are named explicitly; anything left goes in as a *field
    override*. The distinction matters: passing ``outcome_code`` through to ``update()``
    would add a stray ``outcome_code`` key and leave ``outcome`` untouched, so a test that
    meant to forge a hold into a pass would tamper with nothing and pass vacuously.
    """
    payload = build.audit_payload(
        subtype_code="indent-validated",
        subtype_display="Cold-chain indent validated",
        recorded=recorded,
        outcome_code=outcome_code,
        outcome_desc="prescription and indent agree",
        agent_ref="Practitioner/ns-practitioner-prescriber",
        entity_refs=list(entity_refs),
    )
    payload.update(field_overrides)
    return payload


def _three_entries() -> list[ChainEntry]:
    return build_chain(
        [
            _payload(recorded="2026-09-16T08:00:00Z"),
            _payload(recorded="2026-09-16T08:01:00Z", outcome_code=build.OUTCOME_HOLD),
            _payload(recorded="2026-09-16T08:02:00Z"),
        ]
    )


# -- the round trip: the reason the chain is verifiable by a third party -----------------


def test_audit_round_trip() -> None:
    """`chain_entry` must invert `audit_event` exactly.

    This is the contract `HASHED_AUDIT_FIELDS` exists to hold. Break it and verification
    fails on honest data, which is worse than having no verification at all.
    """
    payload = _payload()
    entry = link(None, payload)
    resource = build.audit_event(resource_id="ns-auditevent-1", payload=payload, entry=entry)

    assert build.chain_entry(resource) == entry


def test_round_trip_survives_a_mid_chain_entry() -> None:
    """Not just the genesis case: the predecessor link has to come back too."""
    entry = _three_entries()[2]
    resource = build.audit_event(resource_id="ns-auditevent-3", payload=entry.payload, entry=entry)
    recovered = build.chain_entry(resource)

    assert recovered == entry
    assert recovered is not None
    assert recovered.prev_hash == _three_entries()[1].hash
    assert recovered.sequence == 3


def test_the_hashed_payload_is_embedded_verbatim() -> None:
    """A verifier recomputes from the resource alone, so the content must be present.

    If `audit_event` summarised or re-derived the payload, a verifier would have to trust
    our code to reproduce it — which is the thing a tamper-evident log exists to avoid.
    """
    entry = _three_entries()[1]
    resource = build.audit_event(resource_id="ns-auditevent-2", payload=entry.payload, entry=entry)

    for field in build.HASHED_AUDIT_FIELDS:
        assert resource.get(field) == entry.payload.get(field), f"{field} not embedded verbatim"


def test_reordering_two_entries_breaks_the_chain() -> None:
    """The point of hashing the sequence, not just the content."""
    entries = _three_entries()
    swapped = [entries[0], entries[2], entries[1]]

    problems = verify_chain(swapped)
    assert problems, "a reordered trail verified"
    assert any("sequence" in p.reason for p in problems)


def test_an_untouched_trail_verifies() -> None:
    assert verify_chain(_three_entries()) == []


def test_an_empty_trail_verifies_vacuously() -> None:
    """"Nothing was attempted" is a legitimate state, not a failure."""
    assert verify_chain([]) == []


# -- tampering ---------------------------------------------------------------------------


def test_editing_an_events_content_is_detected() -> None:
    """The case the brief's "tamper-proof" asks about: flipping a hold to a success."""
    entries = _three_entries()
    tampered = list(entries)
    forged_payload = dict(entries[1].payload)
    forged_payload["outcome"] = build.OUTCOME_SUCCESS  # a hold laundered into a pass
    tampered[1] = ChainEntry(
        sequence=entries[1].sequence,
        prev_hash=entries[1].prev_hash,
        hash=entries[1].hash,  # the recorded hash is left alone — that is the attack
        payload=forged_payload,
    )

    problems = verify_chain(tampered)
    assert any("altered" in p.reason for p in problems), problems
    assert any(p.index == 1 for p in problems)


def test_deleting_an_event_is_detected() -> None:
    """Removing the middle event breaks the *link*, not the hashes."""
    entries = _three_entries()
    without_middle = [entries[0], entries[2]]

    problems = verify_chain(without_middle)
    assert any("removed or inserted" in p.reason for p in problems), problems


def test_truncating_the_tail_is_not_detectable_from_the_chain_alone() -> None:
    """A documented limitation, pinned so nobody claims more than this delivers.

    Dropping the newest events leaves a shorter but internally consistent chain. Catching
    it needs the head hash recorded somewhere the attacker does not control — which is
    exactly why the module docstring says tamper-*evident*. If this test ever starts
    failing, the chain has gained a property genuinely worth advertising; until then, the
    write-up must not claim it.
    """
    entries = _three_entries()
    assert verify_chain(entries[:2]) == [], "truncation became detectable — update the docs"


def test_all_problems_are_reported_not_just_the_first() -> None:
    """Triage needs the shape of the damage: one broken hash reads differently from three."""
    entries = _three_entries()
    mangled = list(entries)
    for index in (0, 2):
        payload = dict(entries[index].payload)
        payload["outcomeDesc"] = "edited"
        mangled[index] = ChainEntry(
            entries[index].sequence,
            entries[index].prev_hash,
            entries[index].hash,
            payload,
        )

    problems = verify_chain(mangled)
    assert {p.index for p in problems} >= {0, 2}, problems


# -- hashing mechanics -------------------------------------------------------------------


def test_hash_depends_on_the_predecessor() -> None:
    """Otherwise the chain would be a list of independent hashes, trivially rebuildable."""
    payload = _payload()
    first = event_hash(sequence=2, prev_hash=GENESIS_HASH, payload=payload)
    second = event_hash(sequence=2, prev_hash="a" * 64, payload=payload)
    assert first != second


def test_hash_depends_on_the_sequence() -> None:
    """Identical content at two positions must not hash the same, or entries could be
    swapped without detection."""
    payload = _payload()
    assert event_hash(sequence=1, prev_hash=GENESIS_HASH, payload=payload) != event_hash(
        sequence=2, prev_hash=GENESIS_HASH, payload=payload
    )


def test_canonical_is_independent_of_key_order() -> None:
    """The server does not promise to preserve key order, and a hash must not care.

    Without `sort_keys`, a resource that came back with its keys reordered would verify as
    tampered. That would be a false alarm indistinguishable from a real one.
    """
    reordered = dict(reversed(list(_payload().items())))
    assert canonical(_payload()) == canonical(reordered)


def test_genesis_entry_links_to_a_run_of_zeros() -> None:
    entry = link(None, _payload())
    assert entry.sequence == 1
    assert entry.prev_hash == GENESIS_HASH


def test_build_chain_numbers_from_one_without_gaps() -> None:
    assert [e.sequence for e in _three_entries()] == [1, 2, 3]


# -- the AuditEvent payload itself -------------------------------------------------------


def test_outcome_is_a_primitive_code_not_a_coding() -> None:
    """R4 types ``AuditEvent.outcome`` as a ``code``. A ``Coding`` is silently dropped.

    Measured on the sandbox: HAPI answers 201 and the field is absent on read-back, so the
    trail would record no outcome while appearing to work. Asserting the *type* is the only
    offline way to keep that from coming back.
    """
    outcome = _payload()["outcome"]
    assert isinstance(outcome, str), f"outcome must be a code string, got {type(outcome).__name__}"
    assert outcome == build.OUTCOME_SUCCESS


def test_a_hold_is_recorded_as_a_serious_failure() -> None:
    """Every hold means a medication was not dispensed and a human must follow up."""
    assert build.OUTCOME_HOLD == "8"
    assert build.OUTCOME_HOLD != build.OUTCOME_SUCCESS


def test_action_is_execute_not_create() -> None:
    """The event records that the gate *ran a validation*; the write happens afterwards."""
    assert _payload()["action"] == "E"


def test_the_prescriber_is_the_accountable_agent() -> None:
    """The audit question is "who is accountable", and that is a person, not the pipeline."""
    agent = _payload()["agent"][0]
    assert agent["who"]["reference"] == "Practitioner/ns-practitioner-prescriber"
    assert agent["requestor"] is True


def test_the_observer_identifies_this_system() -> None:
    observer = _payload()["source"]["observer"]
    assert observer["identifier"]["system"] == build.SYSTEM_OBSERVER
    assert observer["identifier"]["value"] == build.SYSTEM_OBSERVER_VALUE


def test_source_type_is_omitted_rather_than_guessed() -> None:
    """R4 binds ``source.type`` to a DICOM code list we cannot verify offline.

    Same rule as the ETA extension: declare nothing rather than invent a plausible-looking
    code. The observer is named explicitly instead, so nothing is lost.
    """
    assert "type" not in _payload()["source"]


def test_entity_references_the_prescription_it_judged() -> None:
    assert _payload()["entity"] == [{"what": {"reference": "MedicationRequest/ns-medreq-1001"}}]


def test_an_uncorrelated_order_carries_no_entity() -> None:
    """When no MedicationRequest was found there is nothing to cite.

    Emitting an ``entity`` with an empty or invented reference would be worse than omitting
    it: HAPI resolves these references and rejects the write outright (400, HAPI-1094), so
    the hold path would fail to record an audit trail at exactly the moment it matters.
    """
    payload = _payload(entity_refs=())
    assert "entity" not in payload

    # And the omission must survive the round trip: `chain_entry` rebuilds the payload from
    # whichever hashed fields are present, so an absent key has to stay absent rather than
    # coming back as None and hashing differently.
    entry = link(None, payload)
    resource = build.audit_event(resource_id="ns-auditevent-1", payload=payload, entry=entry)
    assert build.chain_entry(resource) == entry


def test_audit_payload_carries_no_patient_identifiers() -> None:
    """The audit record is readable by anyone with sandbox access.

    ``outcomeDesc`` is the sharp edge: it is the validator's free-text ``detail``, and the
    obvious way to write a helpful message ("no order for JOHN DOE") is the one that leaks.
    """
    import json

    from app import demo

    serialized = json.dumps(_payload())
    for phi in (demo.MRN, demo.PATIENT_FAMILY, demo.PATIENT_GIVEN, "1974-03-02"):
        assert phi not in serialized, f"{phi!r} leaked into the AuditEvent"


# -- the reader is defensive -------------------------------------------------------------


def test_chain_entry_returns_none_without_the_extension() -> None:
    """An AuditEvent written by some other system is not an error — it is just not ours."""
    assert build.chain_entry({"resourceType": "AuditEvent", "id": "x"}) is None


def test_chain_entry_returns_none_when_the_extension_is_incomplete() -> None:
    """A half-written link must not be mistaken for a valid one with empty values."""
    resource = {
        "resourceType": "AuditEvent",
        "id": "x",
        "extension": [
            {
                "url": build.AUDIT_CHAIN_EXTENSION_URL,
                "extension": [{"url": "sequence", "valuePositiveInt": 1}],
            }
        ],
    }
    assert build.chain_entry(resource) is None


def test_unknown_sub_extensions_are_ignored() -> None:
    """Forward compatibility: adding a field to the extension must not break old readers."""
    payload = _payload()
    entry = link(None, payload)
    resource = build.audit_event(resource_id="ns-auditevent-1", payload=payload, entry=entry)
    resource["extension"][0]["extension"].append({"url": "futureField", "valueString": "x"})

    assert build.chain_entry(resource) == entry


def test_the_chain_extension_survives_a_copy_of_the_resource() -> None:
    """Cheap guard against the reader accidentally mutating what it reads."""
    payload = _payload()
    entry = link(None, payload)
    resource = build.audit_event(resource_id="ns-auditevent-1", payload=payload, entry=entry)
    before = copy.deepcopy(resource)

    build.chain_entry(resource)
    assert resource == before
