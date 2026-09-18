"""Synchronous test driver for an ASGI application.

``fastapi.testclient.TestClient`` starts an AnyIO blocking portal in a background thread.
That is a useful default for browser-like tests, but it makes otherwise hermetic tests depend
on thread/portal support in the runner.  This adapter uses httpx's ASGI transport directly:
the request still crosses the real ASGI boundary, but each synchronous test drives it through
``asyncio.run`` instead of a long-lived portal.

It is deliberately small and is used only by the offline test suite and the script that
generates the checked-in UI fallback data.  Applications still run through Uvicorn.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import FastAPI


class ASGIClient:
    """A context-managed synchronous client over an in-process ASGI app.

    The caller supplies the already-built service because ASGI transports do not drive an
    application's lifespan automatically.  Assigning it here mirrors ``lifespan`` exactly
    for the routes under test; ``close`` mirrors its shutdown half.
    """

    def __init__(self, app: FastAPI, service: Any) -> None:
        self.app = app
        self._service = service
        self.app.state.service = service

    def __enter__(self) -> ASGIClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._service.close()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=True)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(send())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)
