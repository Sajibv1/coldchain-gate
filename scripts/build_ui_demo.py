"""Record the demo runs the UI falls back to when no server is answering.

`ui/index.html` is an interactive demo: press a fixture button and the page shows what the
pipeline did with that indent. When the page is served by `make run-offline` it gets those
answers live over HTTP. But a reviewer who opens the file directly — double-clicking it, or
reading the repo on a machine with nothing running — has no server to ask, and a demo page
that does nothing when opened is worse than no demo page.

So the same seven runs are embedded in the page, and this script is how they are produced.

    .venv/bin/python scripts/build_ui_demo.py

**It records by driving the real code.** The runs are captured from a real `Service` over an
in-process ASGI client, offline: `fhir_mode="dry-run"` puts FHIR in memory, and RxNav replays the
recordings `scripts/record_rxnav.py` captured from the live API. Nothing here is hand-written,
so the page cannot claim an outcome the pipeline does not produce. `tests/test_ui.py` asserts
the embedded block still matches a fresh run, which is what keeps that true.

Only `Run.public()` is recorded — the allowlist view, which is what may leave the process at
all. The internal record stays out of the page for the same reason it stays out of the API
response: it carries the patient. The UI shows it from a hand-written panel, labelled as the
seeded demo dataset, rather than by serialising it into a file anyone can open.

Two fields in a recorded run do not survive a re-run, and the test knows it: `eta` (and the
copy inside `alert`) is wall-clock at minute resolution, and each audit event's `hash` covers
a `recorded` timestamp taken at second resolution. Everything else is stable, including the
notification token, which is an HMAC over the order numbers under a fixed demo secret.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Run as a plain script, so the repo root is not on the path by default.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.hl7 import fixtures  # noqa: E402
from app.main import Service, create_app  # noqa: E402
from app.testing import ASGIClient  # noqa: E402
from tests.conftest import (  # noqa: E402
    RECORDED_RXNAV,
    TEST_SETTINGS,
    RecordedRxNavTransport,
)

UI_PATH = Path(__file__).resolve().parents[1] / "ui" / "index.html"

#: The one block this script owns. Everything between these two tags is replaced wholesale;
#: everything outside them is hand-written and untouched.
PATTERN = re.compile(
    r'(<script type="application/json" id="recorded-runs">)(.*?)(</script>)',
    re.DOTALL,
)


def collect() -> dict[str, Any]:
    """One real run per fixture, offline. Pure with respect to everything but the clock."""
    settings = Settings(**{**TEST_SETTINGS.model_dump(), "fhir_mode": "dry-run"})
    recorded = json.loads(RECORDED_RXNAV.read_text())

    def factory() -> Service:
        return Service(settings, rxnav_transport=RecordedRxNavTransport(recorded))

    runs: dict[str, Any] = {}
    service = factory()
    with ASGIClient(create_app(factory), service) as client:
        for name in sorted(fixtures.FIXTURES):
            response = client.post(f"/indent/demo/{name}")
            # A hold is a 200 and a finding; anything else here is a broken fixture or a
            # broken service, and recording it into the page would ship the breakage.
            if response.status_code != 200:
                raise SystemExit(
                    f"fixture {name!r} answered {response.status_code}, not 200:\n"
                    f"{response.text}"
                )
            runs[name] = response.json()
        mode = client.get("/health").json()["mode"]

    return {"mode": mode, "runs": runs}


def main() -> int:
    payload = collect()
    # `</script>` cannot appear in the payload, but escaping every `<` costs nothing and
    # removes the question — a drug name is not going to contain one either.
    encoded = json.dumps(payload, indent=2, sort_keys=True).replace("<", "\\u003c")

    html = UI_PATH.read_text()
    if not PATTERN.search(html):
        raise SystemExit(
            f"no <script type=\"application/json\" id=\"recorded-runs\"> block in {UI_PATH}\n"
            "The page must declare the block before this script can fill it."
        )
    UI_PATH.write_text(PATTERN.sub(lambda m: f"{m.group(1)}\n{encoded}\n{m.group(3)}", html, count=1))

    dispensed = sum(1 for run in payload["runs"].values() if run["status"] == "dispensed")
    held = len(payload["runs"]) - dispensed
    print(f"{len(payload['runs'])} runs recorded — {dispensed} dispensed, {held} held")
    for name, run in payload["runs"].items():
        print(f"  {name:<20} {run['status']:<10} {run['code']}")
    print(f"\nwrote {UI_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
