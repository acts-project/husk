"""The single HTTP surface: an async Quart app serving every endpoint, plus a
coroutine the CLI awaits to run it on the main event loop.

  GET /         — live dashboard (Jinja + SSE, no polling)
  GET /status   — JSON list of ControllerState, one per pool (huskctl, dashboards)
  GET /metrics  — Prometheus text exposition, rendered from a CollectorRegistry
                  (see husk.metrics for what is in it and why it is split into a
                  snapshot-derived half and an event-time half)
  GET /sd/targets — Prometheus http_sd: live per-slot node_exporter scrape targets
  GET /slot/<pool>/<slot>/metrics — a libvirt guest's node_exporter, bridged over
                  huskd's SSH channel to the hypervisor (the guest is on a private
                  net; only its host can reach it). OpenStack guests are routable
                  and are scraped directly, so they never come through here.
  GET /healthz  — 200 if every pool has a recent reconcile, else 503
  GET /events   — Server-Sent Events stream of the per-pool snapshots

All endpoints read the SAME in-memory snapshot provider (a 0-arg callable
returning `list[ControllerState]`, swapped atomically per tick), so they never
touch the backends and a read is always of a complete, immutable state. This app
shares one event loop with the centralized runner poller and every pool's
reconcile task (see `husk.cli._serve`); reconcile keeps its blocking backend work
off that loop via `asyncio.to_thread`, so a wedged hypervisor can't stall these
handlers. No auth: it exposes slot ids / runner names (not secrets) — bind to
localhost unless it sits behind network controls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from quart import Quart, Response, render_template

from husk.guest_scrape import GuestScraper, GuestScrapeError

from husk.metrics import Metrics, SnapshotCollector
from husk.snapshot import ControllerState
from husk.storage import DiskUsage

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


def build_registry(
    snapshot_provider: Callable[[], list[ControllerState]],
    *,
    storage_provider: Callable[[], list[DiskUsage]] | None = None,
    metrics: Metrics | None = None,
) -> CollectorRegistry:
    """The registry `/metrics` renders.

    A fresh `CollectorRegistry` rather than `prometheus_client.REGISTRY`, because
    the default one is process-global and already carries the Process/Platform/GC
    collectors from import time. Sharing it would mean every `make_app` in a test
    process accumulating collectors on the same registry — each one still gets
    collected, so the exposition would grow a duplicate copy of every husk series
    per app ever built — and it would fold in platform metrics husk never chose to
    publish. An explicit registry makes the exposition exactly what husk put in
    it."""
    registry = CollectorRegistry()
    registry.register(SnapshotCollector(snapshot_provider, storage_provider))
    if metrics is not None:
        registry.register(metrics)
    return registry


def make_app(
    snapshot_provider: Callable[[], list[ControllerState]],
    *,
    shutdown: asyncio.Event | None = None,
    scraper: GuestScraper | None = None,
    advertise_addr: str = "",
    storage_provider: Callable[[], list[DiskUsage]] | None = None,
    metrics: Metrics | None = None,
) -> Quart:
    """Build the app over a per-pool snapshot provider (the same one every
    endpoint reads). Templates resolve relative to this package.

    `shutdown`, if given, is the server's shutdown event: the long-lived `/events`
    SSE stream watches it and returns promptly when it fires, so a connected
    dashboard doesn't hold graceful shutdown open until hypercorn's much longer
    `shutdown_timeout` (this is what made Ctrl-C appear to hang).

    `scraper` bridges to libvirt guests, which sit on a private net no one but
    their hypervisor can reach: `/slot/<pool>/<slot>/metrics` fetches the guest's
    node_exporter over the SSH channel huskd already holds to the host. Without it,
    libvirt slots are simply not published as metrics targets. (OpenStack guests are
    routable and are scraped directly — huskd is never in *their* data path.)

    `advertise_addr` is where central Prometheus reaches THIS huskd — the address
    `/sd/targets` hands out for the proxied libvirt targets. Defaults to the
    controller's `http_addr`, which is wrong only if huskd is behind a NAT/ingress,
    hence the override.

    `metrics` is the daemon's event-time instrument set (`husk.metrics.Metrics`),
    the same object the controllers and the poller record into. Omitted, `/metrics`
    still serves everything derivable from the snapshot — which is what `huskctl`
    and most tests want."""
    app = Quart(__name__)
    registry = build_registry(
        snapshot_provider, storage_provider=storage_provider, metrics=metrics
    )

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
    async def metrics_endpoint():
        # The collectors read the snapshot/storage providers themselves, so this
        # is the whole handler: everything about which series exist, how they are
        # labelled and how they are escaped lives in husk.metrics.
        return Response(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)

    @app.get("/sd/targets")
    async def sd_targets():
        # Prometheus http_sd: one group per running slot. Per-target routing lets a
        # single feed serve both backends. Target labels are kept minimal (backend,
        # slot) for the join to husk_slot_info; only runner-online slots are published
        # (node_exporter is up by then, which avoids scrape `down` noise while a slot
        # boots or drains).
        #
        # `host` is what distinguishes the two. A libvirt guest is on a private net,
        # reachable only from its hypervisor, so Prometheus is pointed back at THIS
        # huskd, which bridges the last hop over its existing SSH channel. An
        # OpenStack guest has a routable fixed IP and is scraped directly — huskd
        # stays out of that data path entirely.
        groups = []
        for s in _snaps():
            for v in s.slots:
                if v.runner_status != "online" or not v.ip:
                    continue
                if v.host:  # libvirt: private guest → proxied through huskd
                    if scraper is None or not advertise_addr:
                        continue  # can't route to it → don't advertise a dead target
                    address = advertise_addr
                    path = f"/slot/{s.backend}/{v.name}/metrics"
                else:  # OpenStack: routable guest → direct
                    address, path = f"{v.ip}:9100", "/metrics"
                groups.append(
                    {
                        "targets": [address],
                        "labels": {
                            "__metrics_path__": path,
                            "backend": s.backend,
                            "slot": v.name,
                        },
                    }
                )
        return Response(json.dumps(groups), content_type="application/json")

    @app.get("/slot/<backend>/<slot>/metrics")
    async def slot_metrics(backend: str, slot: str):
        # The libvirt bridge: fetch this guest's node_exporter over huskd's existing
        # SSH channel to the hypervisor. Errors are 5xx on purpose — Prometheus turns
        # that into `up == 0` for the target, which is the honest signal.
        #
        # The slot is looked up in the CURRENT snapshot rather than trusted from the
        # URL, so this can only ever reach a live slot huskd itself manages — it is
        # not a general-purpose relay, and there is no way to point it at an
        # arbitrary address.
        if scraper is None:
            return Response("no guest scraper configured\n", status=503)
        view = next(
            (
                v
                for s in _snaps()
                if s.backend == backend
                for v in s.slots
                if v.name == slot
            ),
            None,
        )
        if view is None:
            return Response(f"no such slot {slot!r} in pool {backend!r}\n", status=404)
        if not view.host or not view.ip:
            # Not a libvirt slot, or its DHCP lease hasn't appeared yet.
            return Response(f"slot {slot!r} has no guest route\n", status=503)
        try:
            body = await scraper.fetch(backend, view.host, view.ip)
        except GuestScrapeError as e:
            log.warning("guest scrape failed for %s/%s: %s", backend, slot, e)
            if metrics is not None:
                metrics.guest_scrape_failures.inc(backend)
            return Response(f"{e}\n", status=502)
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
