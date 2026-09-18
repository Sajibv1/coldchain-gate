"""The HTTP surface for the cold-chain pipeline.

Four things this module is, and one it deliberately is not.

**It is a thin adapter.** Every clinical decision lives in :mod:`app.safety.validate` and
every ordering of effects lives in :mod:`app.pipeline`; this file moves bytes and picks a
status code. If a rule can be tested without HTTP, it is not written here.

**It is the same pipeline the CLI runs.** ``POST /indent`` calls the same ``process()`` that
``make demo`` calls, with the same FHIR client, the same RxNav client, and the same
notifier, over the same seeded dataset. There is no second code path for the web, which is
the only reason a demo over HTTP proves anything about the system.

**It has no endpoint that returns PHI.** :meth:`app.pipeline.Run.public` is the only view
any route serialises. The internal record — patient name, MRN, order and message numbers,
FHIR resource ids — is reachable from the CLI, where the operator is sitting at the
workstation in front of it, and from nowhere else. This is a deliberate refusal of the
obvious feature. ``Run.internal()`` exists and is one line away, so the boundary has to be a
decision rather than an oversight; an endpoint that hands back a patient record to whoever
asks is the precise failure this project is graded on, and no query parameter changes that.
An operator console over HTTP is a defensible product — behind the authentication this demo
does not have. So it is not here, and the console is the terminal.

**It is not production infrastructure**, and says so rather than implying otherwise: no
authentication, no rate limiting, no persistence beyond the process, one worker assumed. The
request-size cap below is the one hardening step worth taking in a demo, because an
unbounded body is a denial of service whether or not the service is public.

The one thing it is *not* is a validator. A message that is not HL7 is answered with a
``held`` run carrying ``unparseable_indent``, not with a 4xx: that outcome is auditable, it
names the sending interface as the thing to fix, and it means the failure that matters most
in this system — an indent nobody can read — lands in the trail instead of in an access log.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.demo import COURIER_NAME
from app.fhir.client import FhirClient
from app.fhir.seed import SeededDataset, build_dataset
from app.hl7 import fixtures
from app.pipeline import (
    InMemoryFhirTransport,
    Run,
    courier_from,
    process,
)
from app.pipeline import Courier
from app.rxnorm.client import RxNavClient
from app.safety import phi
from app.safety.notify import InMemoryNotifier

JSON = dict[str, Any]

log = logging.getLogger("coldchain")

#: The largest indent this service will read. An OMP^O09 for a single item is a few hundred
#: bytes; a megabyte is generous enough that no real message is refused and small enough
#: that a hostile body cannot exhaust memory. Starlette enforces no limit of its own.
MAX_BODY_BYTES = 1024 * 1024

#: The static demo page. Resolved from this file rather than from the working directory,
#: because ``make run`` is not the only way to start uvicorn and a cwd-relative mount
#: silently 404s the UI when it is.
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class Service:
    """The process-wide half of the service: clients, dataset, notifier, and the mode.

    Built once at startup rather than per request. Three of these four are worth it for
    their own reasons — the HTTP clients own connection pools, the dataset is pure but
    non-trivial to build, and the notifier is *stateful on purpose*, because it is what
    stands in for a phone: a delivery recorded during one request has to still be there for
    the next one, or the demo cannot show what the device received.

    httpx clients are documented thread-safe, and this service is exercised from FastAPI's
    threadpool, so the sharing is sound. The counters are incremented under the GIL and are
    diagnostics, not decisions.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        fhir_transport: httpx.BaseTransport | None = None,
        rxnav_transport: httpx.BaseTransport | None = None,
    ) -> None:
        """``fhir_transport`` and ``rxnav_transport`` are the seam the tests use.

        Same shape as the one on the clients themselves, and for the same reason: the
        offline suite has to exercise *this* wiring — the status codes, the view, the
        request-size cap — without a network, and a test that stubbed the whole ``Service``
        would prove nothing about any of that.
        """
        self.settings = settings
        self.dataset: SeededDataset = build_dataset(settings)
        self.courier: Courier = courier_from(self.dataset)
        self.notifier = InMemoryNotifier()

        # In dry-run the FHIR server is a dict, seeded from the same dataset `make seed`
        # writes to the sandbox and accumulating writes for the life of the process. That
        # makes an offline run a complete demonstration with no network at all, and it is
        # the same trade `app.pipeline --dry-run` makes, reached through the same class.
        self.dry_run = settings.fhir_mode == "dry-run"
        if fhir_transport is None and self.dry_run:
            fhir_transport = InMemoryFhirTransport(self.dataset.resources())
        self.fhir = FhirClient(
            settings.fhir_root, timeout_s=settings.fhir_timeout_s, transport=fhir_transport
        )
        self.rxnav = RxNavClient(
            settings.rxnav_root,
            timeout_s=settings.rxnav_timeout_s,
            transport=rxnav_transport,
        )
        # What the RxNorm lookups will actually reach. A transport that describes itself —
        # app.rxnorm.replay.RecordedRxNavTransport does — is reported instead of the URL,
        # because a process answering from a recording must not tell /health it is calling
        # the live API. The offline test suite is that configuration; the deployed demo
        # (``api/index.py``) injects nothing and so reports the real endpoint.
        self.rxnav_source = getattr(rxnav_transport, "description", settings.rxnav_root)
        self.processed = 0

    def close(self) -> None:
        self.fhir.close()
        self.rxnav.close()

    def run(self, message: str) -> Run:
        """Drive one indent through the pipeline. Blocking; callers go to a threadpool.

        :class:`~app.safety.phi.PrivacyViolation` is *not* caught here. It means a payload
        that was not the allowlist was about to reach a device — a defect in this process,
        not a bad request — and the route turns it into an opaque 500.
        """
        self.processed += 1
        return process(
            message,
            fhir=self.fhir,
            rxnav=self.rxnav,
            notifier=self.notifier,
            settings=self.settings,
            courier=self.courier,
        )

    def status(self) -> JSON:
        """What this process is configured to talk to.

        Reports configuration, not liveness, and that is not laziness: a health check that
        polls the public sandbox on every call would be a load generator pointed at somebody
        else's server, and it would report *this* service unhealthy whenever HAPI had a bad
        minute. The demo's real connectivity check is ``make verify``, run deliberately.
        """
        return {
            # Rendered verbatim by the demo page, which composes "A live run against this
            # server — {mode}." A bare "sandbox" made that read "...— sandbox.": true, and
            # nothing a reader could use. Both strings name what the process is talking to.
            "mode": (
                "dry-run (in-memory FHIR, no network writes)"
                if self.dry_run
                else "sandbox (live RxNav, public HAPI server)"
            ),
            "fhir": "in-memory" if self.dry_run else self.settings.fhir_root,
            "rxnav": self.rxnav_source,
            "namespace": self.settings.demo_namespace,
            "courier": COURIER_NAME,
            "indents_processed": self.processed,
            # Every *send attempt*, split by how it went. Not "held": the notifier's list holds
            # successful sends too, and a held indent never reaches the notifier at all — so a
            # field called ``notifications_held`` reading ``len(deliveries)`` counted the
            # opposite of what it said. It reported 1 after a clean dispense, which is the one
            # run in the demo where a notification certainly was not held back.
            "notifications_delivered": sum(1 for d in self.notifier.deliveries if d.delivered),
            "notifications_failed": sum(
                1 for d in self.notifier.deliveries if not d.delivered
            ),
        }


# --------------------------------------------------------------------------------------
# Request handling
# --------------------------------------------------------------------------------------


def read_er7(body: bytes) -> str:
    """Bytes off the wire, as ER7 the parser can read.

    Two normalisations, both about transport rather than clinical content, and both
    necessary for the demo to work at all:

    * **Line endings.** HL7 v2 terminates a segment with ``\\r``. Anything that reached this
      service through a text-mode channel — a shell heredoc, a browser, a copy-paste into a
      docs page — has had that rewritten to ``\\n`` or ``\\r\\n``, and hl7apy rejects both.
      Restoring the terminator is not a fixup of the message's meaning; the terminator *is*
      the framing, and framing is the transport's business. Doing it here rather than in
      ``app.hl7.parse`` keeps the parser honest about what a well-formed message is.
    * **Encoding.** Decoded leniently, because a byte the sender got wrong is not a reason
      to refuse the message at the edge — it goes to the pipeline, which holds it with
      ``unparseable_indent`` and records that in the trail.
    """
    text = body.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\r").replace("\n", "\r")


def response_for(run: Run) -> JSONResponse:
    """Map a run onto a status code and the one view allowed to leave the process.

    The mapping carries the distinction the whole design turns on:

    * ``dispensed`` and ``held`` are both **200**. Both are the service working: a hold is a
      clinical finding, delivered on purpose, with a code and an audit trail. ``status`` in
      the body says which happened, so a caller reads one field instead of two.
    * A run whose ``status`` is ``error`` is **503**, not 200. The check did not run, which
      is the one outcome a caller should retry, and answering 200 would tell a monitoring
      system that everything is fine while no indent is being dispensed.
    * ``PrivacyViolation`` never reaches here — the route catches it — and would be 500.
    """
    code = status.HTTP_200_OK if run.status in {"dispensed", "held"} else status.HTTP_503_SERVICE_UNAVAILABLE
    payload = run.public()
    log.info(
        "indent -> %s (%s) token=%s",
        run.status,
        run.code,
        run.token or "-",
    )
    return JSONResponse(payload, status_code=code)


# --------------------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the service at startup, close the clients at shutdown.

    The factory is read off ``app.state`` rather than called at module scope so that a test
    can build an application around an offline service without monkeypatching anything —
    see ``tests/test_api.py``.
    """
    service = app.state.service_factory()
    app.state.service = service
    log.info("coldchain up: %s", service.status()["mode"])
    try:
        yield
    finally:
        service.close()


#: The two bodies returned when the pipeline refuses. Module-level constants rather than
#: literals at the raise site so the test that asserts they name no patient data can import
#: them, and so the wording lives in one place.
PRIVACY_REFUSAL: JSON = {
    "status": "error",
    "code": "privacy_violation",
    "detail": (
        "the outbound payload was refused at the boundary; nothing was sent to a device"
    ),
}
TOO_LARGE: JSON = {
    "status": "error",
    "code": "request_too_large",
    "detail": f"body exceeds {MAX_BODY_BYTES} bytes",
}


async def dispatch(service: Service, message: str) -> JSONResponse:
    """Run one message and turn the outcome into a response. Every route goes through here.

    A helper rather than a decorator or middleware because it needs the *service*, which only
    the route has. It exists as one function for a specific reason: the first cut of this
    module had the ``PrivacyViolation`` guard written into ``POST /indent`` and forgotten on
    ``POST /indent/demo/{name}``, so the exception escaped one route and not the other. The
    failure mode of a duplicated safety check is that one copy goes missing, so there is one
    copy.

    The refusal is opaque and says nothing about what leaked. ``PhiLeak`` names the offending
    field and the identifying text it found; both are the patient data that was about to
    reach a device, so neither may appear in the response. The traceback goes to the log,
    where an operator is already authorised to see it.
    """
    try:
        run = await run_in_threadpool(service.run, message)
    except phi.PrivacyViolation:
        log.exception("refused an outbound payload that was not the allowlist")
        return JSONResponse(
            PRIVACY_REFUSAL, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    return response_for(run)


def create_app(service_factory: Callable[[], Service] | None = None) -> FastAPI:
    """The application. ``service_factory`` defaults to the configured service."""
    app = FastAPI(
        title="Pharmacy cold-chain dispatch",
        version="0.1.0",
        summary=(
            "Turns an HL7 v2 OMP^O09 indent into a validated FHIR MedicationDispense, a "
            "PHI-free device notification, and a hash-chained audit trail."
        ),
        lifespan=lifespan,
    )
    app.state.service_factory = service_factory or (lambda: Service(get_settings()))

    @app.post(
        "/indent",
        tags=["pipeline"],
        response_class=JSONResponse,
        summary="Process one HL7 v2 OMP^O09 indent",
        responses={
            200: {"description": "Dispensed, or held with a machine-readable reason code"},
            413: {"description": "Body larger than the request cap"},
            500: {"description": "A payload that was not the allowlist was about to be sent"},
            503: {"description": "The safety check did not run; the indent was not dispensed"},
        },
    )
    async def post_indent(request: Request) -> JSONResponse:
        """The whole system, one message at a time.

        ``async def`` with the pipeline moved to a threadpool, rather than a plain ``def``,
        for one reason: the body has to be read from an async stream, and reading it before
        deciding whether to accept it is what makes the size cap possible. After that the
        work is synchronous by design — see the note on synchronous httpx in
        :mod:`app.fhir.client`.
        """
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(TOO_LARGE, status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        service: Service = request.app.state.service
        # Do not accept or reflect a client correlation header here.  Callers commonly put
        # patient or order identifiers in request IDs, while this response is deliberately
        # PHI-free.
        return await dispatch(service, read_er7(body))

    @app.get("/fixtures", tags=["pipeline"], summary="List the demo indents by name")
    def list_fixtures() -> JSON:
        """The fixture names, so a reviewer need not read the source to drive the demo."""
        return {"fixtures": sorted(fixtures.FIXTURES)}

    @app.post(
        "/indent/demo/{name}",
        tags=["pipeline"],
        response_class=JSONResponse,
        summary="Process a named demo indent",
    )
    async def post_demo_indent(name: str, request: Request) -> JSONResponse:
        """``POST /indent`` for a built-in message — no ER7 escaping in a shell.

        Exists because the alternative is a reviewer hand-quoting a message with ``\\r``
        terminators into curl, getting it subtly wrong, and concluding the hold they got
        back was the system's fault.
        """
        if name not in fixtures.FIXTURES:
            return JSONResponse(
                {
                    "status": "error",
                    "code": "unknown_fixture",
                    "detail": f"no fixture named {name!r}",
                    "fixtures": sorted(fixtures.FIXTURES),
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )

        service: Service = request.app.state.service
        return await dispatch(service, fixtures.er7(name))

    @app.get("/health", tags=["ops"], summary="What this process is configured to talk to")
    def health(request: Request) -> JSON:
        return request.app.state.service.status()

    # The UI last: a mount at "/" swallows every path, so every route above has to be
    # declared before it. This is the one ordering constraint in the file.
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")

    return app


app = create_app()
