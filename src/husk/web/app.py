"""The web dashboard: a Quart (ASGI) app with Jinja templates that streams the
per-pool status to the browser over Server-Sent Events (no polling).

Kept entirely separate from the stdlib status server (`http_server.py`), which
still serves `/status` / `/metrics` / `/healthz` zero-dep for huskctl, Prometheus,
and k8s probes. The dashboard reads the SAME in-memory snapshot provider (a 0-arg
callable returning `list[ControllerState]`), so it never touches the backends.

Importing this module requires the `web` extra (quart + hypercorn); the CLI guards
the import so huskd runs without the dashboard when it isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Callable

from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from quart import Quart, Response, render_template

from husk.snapshot import ControllerState

log = logging.getLogger("husk.web")

# How often the SSE stream pushes a fresh snapshot to each connected dashboard.
_PUSH_INTERVAL_S = 2.0


def make_app(snapshot_provider: Callable[[], list[ControllerState]]) -> Quart:
    """Build the dashboard app over a per-pool snapshot provider."""
    app = Quart(__name__)  # templates/ resolved relative to this package

    def _payload() -> str:
        return json.dumps([s.to_dict() for s in (snapshot_provider() or [])])

    @app.get("/")
    async def index():
        return await render_template("dashboard.html", push_interval=_PUSH_INTERVAL_S)

    @app.get("/status")
    async def status():
        # Same shape as the stdlib /status, so the page can fall back to a fetch.
        return Response(_payload(), content_type="application/json")

    @app.get("/events")
    async def events():
        async def stream():
            # Send immediately, then on every push interval. EventSource on the
            # client auto-reconnects if the stream drops.
            try:
                while True:
                    yield f"data: {_payload()}\n\n".encode()
                    await asyncio.sleep(_PUSH_INTERVAL_S)
            except asyncio.CancelledError:  # client disconnected
                return

        return Response(
            stream(),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


class WebServer:
    """Runs a Quart app on its own asyncio loop in a daemon thread (mirrors
    `http_server.StatusServer`'s lifecycle so the CLI manages both the same way)."""

    def __init__(self, app: Quart, host: str, port: int) -> None:
        self._app = app
        self._host = host
        self._port = port
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown: asyncio.Event | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="husk-web", daemon=True)
        self._thread.start()
        log.info("web dashboard listening on http://%s:%d", self._host, self._port)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._shutdown = asyncio.Event()
        cfg = HypercornConfig()
        cfg.bind = [f"{self._host}:{self._port}"]
        cfg.accesslog = None
        cfg.errorlog = None
        try:
            # shutdown_trigger avoids hypercorn's signal handlers (which only work
            # on the main thread); stop() fires the event to unblock serve().
            loop.run_until_complete(
                serve(self._app, cfg, shutdown_trigger=self._shutdown.wait)
            )
        finally:
            loop.close()

    def stop(self) -> None:
        if self._loop is not None and self._shutdown is not None:
            self._loop.call_soon_threadsafe(self._shutdown.set)
        if self._thread is not None:
            self._thread.join(timeout=3)
