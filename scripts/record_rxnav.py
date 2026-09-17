"""Re-record the RxNav responses the offline suite replays.

`tests/conftest.py` refuses to serve a request that was not recorded, and the offline tests
assert against real RxNav payloads rather than hand-written fakes — so the recordings are
part of the evidence that the drug matching works against the actual API. This script is how
they are produced, and how they are refreshed if RxNorm's data moves.

    .venv/bin/python scripts/record_rxnav.py            # add anything missing
    .venv/bin/python scripts/record_rxnav.py --force    # re-record everything

**It records by driving the real code.** Rather than a hand-maintained URL list, each term is
pushed through `candidates_for_display`, so whatever requests that path makes — the exact-name
lookup, the approximate search, and the per-concept properties/related/allProperties
expansion — are captured transitively. A URL list maintained by hand would drift from the
code it is supposed to serve, and the failure would show up as a missing recording in an
unrelated test.

The default is to *merge*: existing entries are kept so the committed tests stay pinned to
the payloads they were written against, and only new URLs are added. RxNav scores are
relative to each search, so a blanket re-record can shift a score enough to change which
candidate wins — a real change in the API's behavior, worth seeing deliberately via
``--force`` rather than by accident.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

# Run as a plain script, so the repo root is not on the path by default.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import demo  # noqa: E402
from app.rxnorm.client import RxNavClient  # noqa: E402
from app.rxnorm.normalize import candidates_for_display  # noqa: E402

BASE_URL = "https://rxnav.nlm.nih.gov/REST"
OUT_PATH = Path(__file__).resolve().parents[1] / "tests" / "data" / "rxnav_responses.json"

#: Every display string the offline suite replays. The first four are the HL7 fixtures'
#: displays; the last two are the matcher probes documented in `app/demo.py`.
TERMS: tuple[str, ...] = (
    demo.DRUG_DISPLAY,
    demo.DRUG_WRONG_STRENGTH_DISPLAY,
    demo.DRUG_WRONG_FORM_DISPLAY,
    demo.DRUG_UNRESOLVABLE_DISPLAY,
    demo.DRUG_NONEXISTENT_STRENGTH_DISPLAY,
    demo.DRUG_VIAL_DISPLAY,
)


class RecordingTransport(httpx.BaseTransport):
    """Pass through to the live API and keep a copy of every response.

    Keyed by the URL httpx actually built. `conftest._replay_key` normalises both sides to
    ``(path, sorted query pairs)`` before comparing, so the parameter order httpx happens to
    choose here does not matter to the replayer.
    """

    def __init__(self) -> None:
        self._inner = httpx.HTTPTransport()
        self.seen: dict[str, dict[str, Any]] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        # The underlying transport hands back a streaming response; reading it here is what
        # makes ``.json()`` legal, and the client downstream is unaffected because the body
        # is then already buffered.
        response.read()
        if response.status_code < 400:
            try:
                payload = response.json()
            except ValueError:
                # A 200 that is not JSON is worth seeing, not worth crashing on.
                print(f"  ! non-JSON response from {request.url}", file=sys.stderr)
            else:
                self.seen[str(request.url)] = {
                    "status": response.status_code,
                    "json": payload,
                }
        return response

    def close(self) -> None:
        self._inner.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-record URLs already in the file instead of keeping them",
    )
    args = parser.parse_args()

    existing: dict[str, Any] = (
        json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    )

    transport = RecordingTransport()
    try:
        with RxNavClient(BASE_URL, transport=transport) as client:
            for term in TERMS:
                drugs = candidates_for_display(client, term)
                resolved = ", ".join(
                    f"{d.source_rxcui} {d.ingredient_name} {d.strength_value:g} "
                    f"{d.strength_unit} {d.dose_form}"
                    for d in drugs
                )
                print(f"  {term!r}\n      -> {resolved or 'unresolved'}")
    finally:
        transport.close()

    added = {
        url: entry
        for url, entry in transport.seen.items()
        if args.force or url not in existing
    }
    merged = {**existing, **added}
    OUT_PATH.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")

    print(
        f"\n{len(added)} recorded, {len(transport.seen) - len(added)} already present, "
        f"{len(merged)} total\nwrote {OUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
