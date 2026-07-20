"""The huskd HTTP surface: a single async Quart app (Jinja dashboard + JSON
status / Prometheus metrics / healthz / SSE), served on the same event loop that
runs the centralized runner poller and every pool's reconcile task (see
`husk.cli._serve`)."""

from husk.web.app import (
    STALE_AFTER_SEC,
    make_app,
    parse_addr,
    render_prometheus,
    serve_app,
)

__all__ = [
    "make_app",
    "serve_app",
    "parse_addr",
    "render_prometheus",
    "STALE_AFTER_SEC",
]
