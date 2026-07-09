"""The huskd HTTP surface: a single async Quart app (Jinja dashboard + JSON
status / Prometheus metrics / healthz / SSE), served on the main event loop while
the reconcile loop runs in a background thread (see `husk.cli._serve`)."""

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
