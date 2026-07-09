"""The single HTTP surface: an async Quart app serving every endpoint, plus a
coroutine the CLI awaits to run it on the main event loop.

  GET /         — live dashboard (Jinja + SSE, no polling)
  GET /status   — JSON list of ControllerState, one per pool (huskctl, dashboards)
  GET /metrics  — Prometheus text exposition (per-pool gauges, backend="..." label)
  GET /healthz  — 200 if every pool has a recent reconcile, else 503
  GET /events   — Server-Sent Events stream of the per-pool snapshots

All endpoints read the SAME in-memory snapshot provider (a 0-arg callable
returning `list[ControllerState]`, swapped atomically per tick), so they never
touch the backends and a cross-thread read is safe. The reconcile loop runs in a
background thread; this app owns the event loop on the main thread (see
`husk.cli._serve`). No auth: it exposes slot ids / runner names (not secrets) —
bind to localhost unless it sits behind network controls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from quart import Quart, Response, render_template

from husk.snapshot import ControllerState

log = logging.getLogger("husk.web")

# How often the SSE stream pushes a fresh snapshot to each connected dashboard.
_PUSH_INTERVAL_S = 2.0

# /healthz reports 503 once a pool's last reconcile is older than this.
STALE_AFTER_SEC = 60


def parse_addr(addr: str) -> tuple[str, int]:
    """Split a "host:port" bind address. Bare ":9100" / "9100" bind all interfaces."""
    addr = addr.strip()
    if ":" in addr:
        host, _, port = addr.rpartition(":")
        return (host or "0.0.0.0", int(port))
    return ("0.0.0.0", int(addr))


def render_prometheus(s: ControllerState) -> str:
    b = s.backend
    out = [
        "# HELP husk_slots Slots by classified state",
        "# TYPE husk_slots gauge",
    ]
    out += [
        f'husk_slots{{backend="{b}",state="{state}"}} {n}'
        for state, n in s.counts.items()
    ]
    out += [
        "# HELP husk_slots_desired Desired total slots",
        "# TYPE husk_slots_desired gauge",
        f'husk_slots_desired{{backend="{b}"}} {s.desired_total}',
        "# HELP husk_slots_min_ready Configured min_ready",
        "# TYPE husk_slots_min_ready gauge",
        f'husk_slots_min_ready{{backend="{b}"}} {s.min_ready}',
        "# HELP husk_slots_max_total Configured max_total",
        "# TYPE husk_slots_max_total gauge",
        f'husk_slots_max_total{{backend="{b}"}} {s.max_total}',
        "# HELP husk_last_reconcile_timestamp_seconds Unix time of the last reconcile",
        "# TYPE husk_last_reconcile_timestamp_seconds gauge",
        f'husk_last_reconcile_timestamp_seconds{{backend="{b}"}} {s.last_reconcile_epoch}',
        "# HELP husk_reconcile_generation Monotonic reconcile counter",
        "# TYPE husk_reconcile_generation counter",
        f'husk_reconcile_generation{{backend="{b}"}} {s.generation}',
    ]
    # Per-slot timing (low cardinality — slots are long-lived). Emit only when
    # a value exists so a never-recycled slot doesn't report a bogus 0.
    out += [
        "# HELP husk_slot_last_cloudinit_seconds Last ACTIVE->runner-online duration",
        "# TYPE husk_slot_last_cloudinit_seconds gauge",
    ]
    out += [
        f'husk_slot_last_cloudinit_seconds{{backend="{b}",slot="{v.name}"}} {v.cloudinit_seconds}'
        for v in s.slots
        if v.cloudinit_seconds is not None
    ]
    out += [
        "# HELP husk_slot_last_recycle_seconds Last issue->runner-online duration",
        "# TYPE husk_slot_last_recycle_seconds gauge",
    ]
    out += [
        f'husk_slot_last_recycle_seconds{{backend="{b}",slot="{v.name}"}} {v.recycle_seconds}'
        for v in s.slots
        if v.recycle_seconds is not None
    ]
    out += [
        "# HELP husk_slot_live_fraction Fraction of tracked time the slot was available to serve (busy or idle)",
        "# TYPE husk_slot_live_fraction gauge",
    ]
    out += [
        f'husk_slot_live_fraction{{backend="{b}",slot="{v.name}"}} {v.live_fraction}'
        for v in s.slots
        if v.live_fraction is not None
    ]
    return "\n".join(out) + "\n"


def make_app(
    snapshot_provider: Callable[[], list[ControllerState]],
    *,
    shutdown: asyncio.Event | None = None,
) -> Quart:
    """Build the app over a per-pool snapshot provider (the same one every
    endpoint reads). Templates resolve relative to this package.

    `shutdown`, if given, is the server's shutdown event: the long-lived `/events`
    SSE stream watches it and returns promptly when it fires, so a connected
    dashboard doesn't hold graceful shutdown open until hypercorn's much longer
    `shutdown_timeout` (this is what made Ctrl-C appear to hang)."""
    app = Quart(__name__)

    def _snaps() -> list[ControllerState]:
        return snapshot_provider() or []

    def _payload() -> str:
        return json.dumps([s.to_dict() for s in _snaps()])

    @app.get("/")
    async def index():
        return await render_template("dashboard.html", push_interval=_PUSH_INTERVAL_S)

    @app.get("/status")
    async def status():
        return Response(_payload(), content_type="application/json")

    @app.get("/metrics")
    async def metrics():
        # Per-pool series are distinguished by the backend="..." label already on
        # every metric, so concatenation across pools is a valid exposition.
        body = "".join(render_prometheus(s) for s in _snaps())
        return Response(body, content_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/healthz")
    async def healthz():
        # Healthy only when every pool has published a recent reconcile.
        snaps = _snaps()
        ok = bool(snaps) and all(
            (time.time() - s.last_reconcile_epoch) <= STALE_AFTER_SEC for s in snaps
        )
        return Response(
            "ok\n" if ok else "stale\n",
            status=200 if ok else 503,
            content_type="text/plain",
        )

    @app.get("/events")
    async def events():
        async def stream():
            # Send immediately, then on every push interval. EventSource on the
            # client auto-reconnects if the stream drops. The inter-push wait races
            # the server shutdown event, so on Ctrl-C the stream returns at once
            # instead of holding the connection open through graceful shutdown.
            try:
                while shutdown is None or not shutdown.is_set():
                    yield f"data: {_payload()}\n\n".encode()
                    if shutdown is None:
                        await asyncio.sleep(_PUSH_INTERVAL_S)
                    else:
                        try:
                            await asyncio.wait_for(
                                shutdown.wait(), timeout=_PUSH_INTERVAL_S
                            )
                        except asyncio.TimeoutError:
                            pass  # normal push cadence; loop and send again
            except asyncio.CancelledError:  # client disconnected
                return

        return Response(
            stream(),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


async def serve_app(app: Quart, host: str, port: int, *, shutdown_trigger) -> None:
    """Serve `app` on the current event loop until `shutdown_trigger` fires.

    Drives hypercorn directly (not `app.run`) so it installs NO signal handlers —
    the caller owns SIGINT/SIGTERM and trips `shutdown_trigger`. Works on any
    thread, so tests can run it on a background loop."""
    cfg = HypercornConfig()
    cfg.bind = [f"{host}:{port}"]
    cfg.accesslog = None
    cfg.errorlog = None
    # Bound a stuck response at shutdown (default is 60s): the SSE stream already
    # exits cooperatively, this just caps any other slow handler on Ctrl-C.
    cfg.shutdown_timeout = 3.0
    await serve(app, cfg, shutdown_trigger=shutdown_trigger)
