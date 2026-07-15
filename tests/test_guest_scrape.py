"""GuestScraper: huskd's bridge to a libvirt guest's node_exporter (Phase O4).

These exercise the REAL subprocess path (no mocked asyncio), using a local host
entry (ssh_target="") so the command runs here instead of over SSH — the same code
path minus the ssh hop.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from husk.guest_scrape import GuestScraper, GuestScrapeError

pytestmark = pytest.mark.skipif(
    shutil.which("curl") is None, reason="curl not available"
)

_BODY = b'node_cpu_seconds_total{cpu="0"} 123.4\n'


class _Exporter(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(_BODY)))
        self.end_headers()
        self.wfile.write(_BODY)

    def log_message(self, *a):
        pass


@pytest.fixture
def exporter():
    """A stand-in node_exporter on localhost."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Exporter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _scraper(port, **kw):
    # ssh_target "" → runs locally, so we exercise the subprocess path for real.
    return GuestScraper({("pool-gpu", "gpu-1"): ""}, **kw)


def test_fetches_the_guest_exposition(exporter, monkeypatch):
    monkeypatch.setattr("husk.guest_scrape.EXPORTER_PORT", exporter)
    sc = _scraper(exporter)
    try:
        body = asyncio.run(sc.fetch("pool-gpu", "gpu-1", "127.0.0.1"))
        assert body == _BODY
    finally:
        sc.close()


def test_unreachable_guest_raises(monkeypatch):
    # Nothing is listening — a rebooting slot, or an exporter that isn't up. Must
    # raise (→ 502 → up==0), not hang and not return empty.
    monkeypatch.setattr("husk.guest_scrape.EXPORTER_PORT", 1)  # nothing here
    sc = _scraper(1, timeout=3)
    try:
        with pytest.raises(GuestScrapeError):
            asyncio.run(sc.fetch("pool-gpu", "gpu-1", "127.0.0.1"))
    finally:
        sc.close()


def test_unknown_host_raises():
    sc = GuestScraper({})
    try:
        with pytest.raises(GuestScrapeError, match="no ssh route"):
            asyncio.run(sc.fetch("pool-gpu", "nope", "127.0.0.1"))
    finally:
        sc.close()


def test_close_removes_the_control_socket_dir():
    sc = GuestScraper({("p", "h"): "user@host"})
    d = sc.control_dir
    assert d.is_dir()
    sc.close()
    assert not d.exists()


def test_control_socket_path_fits_sun_path_limit():
    # Regression: on macOS $TMPDIR (/var/folders/.../T/) is long enough that
    # <controldir>/%C overran the ~104-byte unix-socket limit and every scrape
    # failed with "ControlPath too long". The control dir must leave room for
    # ssh's 40-char %C hash under that limit — and multiplexing must be ON when it
    # fits (i.e. we didn't "fix" it by silently dropping connection reuse).
    from husk.guest_scrape import _CONTROL_HASH_LEN, _SUN_PATH_MAX

    sc = GuestScraper({("p", "h"): "user@host"})
    try:
        socket_len = len(str(sc.control_dir)) + 1 + _CONTROL_HASH_LEN
        assert socket_len <= _SUN_PATH_MAX, f"{socket_len} bytes: {sc.control_dir}"
        assert sc._multiplex  # short path → multiplexing stays enabled
    finally:
        sc.close()


def test_degrades_gracefully_if_multiplex_impossible(monkeypatch):
    # If the control path somehow can't fit (a pathologically long temp base), we
    # scrape WITHOUT connection reuse rather than fail every scrape.
    monkeypatch.setattr("husk.guest_scrape._SUN_PATH_MAX", 1)
    sc = GuestScraper({("p", "h"): "user@host"})
    try:
        assert sc._multiplex is False
    finally:
        sc.close()
