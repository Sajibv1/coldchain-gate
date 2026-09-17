"""Hash-chained, tamper-**evident** audit trail.

The brief asks for a "tamper-proof" audit trail. On a shared public sandbox nothing is
tamper-proof: the server is writable by anyone who can reach it, and we do not control it.
Claiming otherwise in a submission would be the wrong thing to hand a reviewer. What this
module actually provides is tamper-*evident*, and the difference is worth being precise
about.

Each event's hash covers the previous event's hash plus a canonical serialisation of the
event's own content, plus its sequence number. So:

* editing an event's content breaks its own hash;
* deleting an event breaks the link from its successor;
* reordering two events breaks both links (the sequence is inside the hash, so events with
  identical content still cannot be swapped);
* truncating the tail is invisible *from the chain alone* — only an externally anchored head
  hash catches that, which is exactly why this is not called tamper-proof.

The property that makes the exercise worthwhile: verification needs **nothing but the
server**. Read the AuditEvents back, order by sequence, recompute. A third party can do that
without trusting this code or this process, because the hashed content is embedded verbatim
in the resource. See `app/fhir/build.py`.

Deliberately free of FHIR knowledge — it hashes bytes and knows nothing about resources, so
it can be tested exhaustively without a server. The FHIR-side read/write lives in
`app/fhir/build.py`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

JSON = dict[str, Any]

#: The predecessor hash of the first event. A run of zeros, so a chain that fails to start
#: at genesis is detectable rather than blending into the first link.
GENESIS_HASH = "0" * 64

#: Byte between hashed components. Without a separator, a crafted predecessor hash could
#: bleed into the payload and two different events could hash identically.
_SEPARATOR = b"\x1f"


def canonical(payload: Mapping[str, Any]) -> bytes:
    """Deterministic bytes for a payload.

    ``sort_keys`` and the compact separators matter: a hash over ``json.dumps`` defaults
    would depend on dict insertion order and whitespace, so the same event could verify or
    not depending on how it was built. Non-ASCII is left escaped-normalised by
    ``ensure_ascii=False`` plus UTF-8 encoding, so a name in any script hashes the same way
    the server stored it.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def event_hash(*, sequence: int, prev_hash: str, payload: Mapping[str, Any]) -> str:
    """SHA-256 over ``prev_hash | sequence | canonical(payload)``."""
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(_SEPARATOR)
    digest.update(str(sequence).encode("ascii"))
    digest.update(_SEPARATOR)
    digest.update(canonical(payload))
    return digest.hexdigest()


@dataclass(frozen=True)
class ChainEntry:
    """One link: what it hashed, what came before, and the resulting hash."""

    sequence: int
    prev_hash: str
    hash: str
    payload: JSON


def link(prev: ChainEntry | None, payload: Mapping[str, Any]) -> ChainEntry:
    """Append ``payload`` to the chain. ``prev=None`` starts a new chain at genesis."""
    sequence = 1 if prev is None else prev.sequence + 1
    prev_hash = GENESIS_HASH if prev is None else prev.hash
    return ChainEntry(
        sequence=sequence,
        prev_hash=prev_hash,
        hash=event_hash(sequence=sequence, prev_hash=prev_hash, payload=payload),
        payload=dict(payload),
    )


def build_chain(payloads: Iterable[Mapping[str, Any]]) -> list[ChainEntry]:
    """Link a sequence of payloads from genesis."""
    entries: list[ChainEntry] = []
    for payload in payloads:
        entries.append(link(entries[-1] if entries else None, payload))
    return entries


@dataclass(frozen=True)
class ChainProblem:
    """One reason a chain does not verify.

    ``index`` is the position in the supplied sequence (not the recorded sequence number),
    because that is what a caller holding a list can act on.
    """

    index: int
    sequence: int | None
    reason: str

    def __str__(self) -> str:
        return f"[{self.index}] seq={self.sequence}: {self.reason}"


def verify_chain(entries: Sequence[ChainEntry]) -> list[ChainProblem]:
    """Recompute the chain. Returns every problem found; empty means intact.

    Reports *all* problems rather than stopping at the first, so a tampered trail can be
    triaged: a single broken hash with intact links is an edit, a broken link with intact
    hashes is a deletion.

    An empty chain verifies — vacuously, and deliberately: "no audit events" is a legitimate
    state (nothing was attempted), not a failure.
    """
    problems: list[ChainProblem] = []

    for index, entry in enumerate(entries):
        expected_prev = GENESIS_HASH if index == 0 else entries[index - 1].hash
        expected_sequence = index + 1

        if entry.sequence != expected_sequence:
            problems.append(
                ChainProblem(
                    index,
                    entry.sequence,
                    f"sequence is {entry.sequence}, expected {expected_sequence} "
                    "(gap, reorder, or duplicate)",
                )
            )

        if entry.prev_hash != expected_prev:
            problems.append(
                ChainProblem(
                    index,
                    entry.sequence,
                    f"prev_hash {entry.prev_hash[:12]}… does not match "
                    f"{'genesis' if index == 0 else 'the previous entry'} "
                    f"{expected_prev[:12]}… (an event was removed or inserted)",
                )
            )

        recomputed = event_hash(
            sequence=entry.sequence, prev_hash=entry.prev_hash, payload=entry.payload
        )
        if recomputed != entry.hash:
            problems.append(
                ChainProblem(
                    index,
                    entry.sequence,
                    f"content hash mismatch: recorded {entry.hash[:12]}…, "
                    f"recomputed {recomputed[:12]}… (the event was altered)",
                )
            )

    return problems
