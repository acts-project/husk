"""Optional web dashboard (Quart + Jinja, live over SSE). Imports fail with a
clear ImportError unless the `web` extra is installed (`pip install 'husk[web]'`)
— the CLI guards on that and runs without the dashboard otherwise."""

from husk.web.app import WebServer, make_app

__all__ = ["WebServer", "make_app"]
