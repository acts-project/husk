"""Read-only HTTP surface exposing huskd's published snapshot.

This is the single source of truth served three ways:
  GET /status   — ControllerState as JSON (huskctl status, dashboards)
  GET /metrics  — Prometheus text exposition (gauges derived from the snapshot)
  GET /healthz  — 200 if a recent reconcile is published, else 503

Runs in a daemon thread alongside the reconcile loop, reading the controller's
latest `snapshot` (an immutable dataclass swapped atomically each tick, so a
cross-thread read is safe). No auth: it exposes slot ids / runner names (not
secrets) — bind to localhost unless it sits behind network controls.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from husk.snapshot import ControllerState

log = logging.getLogger("husk.http")

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
    return "\n".join(out) + "\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        snap: ControllerState | None = self.server.snapshot_provider()  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/status", "/state.json"):
            if snap is None:
                self._send(503, "application/json", b'{"error":"no snapshot yet"}\n')
            else:
                self._send(
                    200,
                    "application/json",
                    (json.dumps(snap.to_dict()) + "\n").encode(),
                )
        elif path == "/metrics":
            body = b"" if snap is None else render_prometheus(snap).encode()
            self._send(200, "text/plain; version=0.0.4; charset=utf-8", body)
        elif path == "/healthz":
            ok = (
                snap is not None
                and (time.time() - snap.last_reconcile_epoch) <= STALE_AFTER_SEC
            )
            self._send(200 if ok else 503, "text/plain", b"ok\n" if ok else b"stale\n")
        else:
            self._send(404, "text/plain", b"not found\n")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # silence default stderr spam
        log.debug("http %s", fmt % args)


class StatusServer:
    def __init__(self, snapshot_provider, host: str, port: int) -> None:
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.snapshot_provider = snapshot_provider  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address  # type: ignore[return-value]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="husk-http", daemon=True
        )
        self._thread.start()
        log.info("status HTTP server listening on http://%s:%d", *self.address)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
