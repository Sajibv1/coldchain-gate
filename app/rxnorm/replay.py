"""Replay recorded RxNav responses — the offline stand-in for the NIH API.

This lives in ``app/`` rather than in ``tests/`` because two callers need it and only one
of them is a test. The offline suite uses it so the drug-matching logic is exercised
against RxNav's *real* response shapes rather than a hand-written fake's, which is the
difference between testing the normalizer and testing my assumptions about it. The
deployed demo uses it for a different reason: a judge pressing "Correct drug" should not
get a different answer because NLM had a slow minute.

It stays honest about being a replay. ``RecordedRxNavTransport.description`` replaces the
upstream URL in ``GET /health``, so a process serving recordings cannot report a live
lookup it never made.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx


def replay_key(url: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Identify a request by path plus query *as a set of pairs*.

    Sorting the query into a string is not enough: the recorder stores the URL httpx
    actually built, so a hand-sorted comparison disagrees on parameter order alone
    (``term=…&maxEntries=…`` vs ``maxEntries=…&term=…``) and every replay misses. Parsing
    both sides into sorted pairs makes the key independent of who ordered the query.
    """
    parts = urlsplit(url)
    return parts.path, tuple(sorted(parse_qsl(parts.query)))


class RecordedRxNavTransport(httpx.BaseTransport):
    """Serve recorded RxNav responses; fail loudly on anything not recorded.

    An unrecorded request raises rather than falling through to the network. In the tests
    that is the point: a test that silently reached the live API would still pass while
    proving nothing about running offline, and would start failing the moment the sandbox
    is unavailable.

    The same raise is correct in the deployed demo, which is worth spelling out because it
    looks like a hazard and is not. The gate calls RxNav inside ``process()``'s broad
    handler, so a missing recording becomes ``status="error"`` and an HTTP 503 — "the check
    did not run" — rather than a wrong clinical answer or a stack trace. A gap in the
    recording is therefore visible and safe, which is the only thing a demo needs it to be.
    """

    #: Reported by ``GET /health`` in place of the upstream URL. Says what this process
    #: really did, not what it was configured to be able to do.
    description = "recorded (replayed from tests/data/rxnav_responses.json)"

    def __init__(self, recorded: dict[str, Any]) -> None:
        self._by_key = {replay_key(url): entry for url, entry in recorded.items()}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = replay_key(str(request.url))
        entry = self._by_key.get(key)
        if entry is None:
            readable = "\n".join(
                f"  {path}?{urlencode(params)}" for path, params in self._by_key
            )
            raise AssertionError(
                f"RxNav request was not recorded: {key[0]}?{urlencode(key[1])}\n"
                f"Recorded:\n{readable}\n"
                "Re-record by running the capture script that writes "
                "tests/data/rxnav_responses.json."
            )
        return httpx.Response(entry["status"], json=entry["json"])
