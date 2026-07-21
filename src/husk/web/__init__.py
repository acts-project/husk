"""The huskd HTTP surface: a single async Quart app (Jinja dashboard + JSON
status / Prometheus metrics / healthz / SSE), served on the same event loop that
runs the centralized runner poller and every pool's reconcile task (see
`husk.cli._serve`)."""

from husk.web.app import (
    STALE_AFTER_SEC,
    build_registry,
    make_app,
    parse_addr,
    serve_app,
)

__all__ = [
    "make_app",
    "serve_app",
    "parse_addr",
    "build_registry",
    "STALE_AFTER_SEC",
]
