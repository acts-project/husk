"""LibvirtBackend host connect is hard-bounded: a down host fails fast instead of
wedging the reconcile thread on an unbounded `libvirt.open()` (the keepalive only
covers RPCs on an already-open connection, not this initial connect)."""

from __future__ import annotations

import threading
import time

import pytest

from husk.backend import BackendError
from husk.config import HostConfig

libvirt = pytest.importorskip("libvirt")
from husk import libvirt_backend as lb  # noqa: E402


def _host() -> "lb._HostConn":
    cfg = HostConfig(
        name="h1",
        libvirt_uri="qemu+ssh://u@host/system",
        ssh_target="u@host",
    )
    return lb._HostConn(cfg, "golden.qcow2")


class _FakeConn:
    def __init__(self) -> None:
        self.keepalive: tuple[int, int] | None = None

    def setKeepAlive(self, interval: int, count: int) -> None:
        self.keepalive = (interval, count)

    def isAlive(self) -> bool:
        return True


def test_open_times_out_on_a_hung_connect(monkeypatch):
    monkeypatch.setattr(lb, "_CONNECT_TIMEOUT_S", 0.2)
    blocked = threading.Event()

    def _hang(_uri):
        blocked.wait(5)  # simulate a down host: connect never returns in time

    monkeypatch.setattr(lb.libvirt, "open", _hang)

    host = _host()
    start = time.monotonic()
    with pytest.raises(BackendError, match="timed out"):
        host.conn()
    # Fails fast (near the bound), not after the full underlying handshake.
    assert time.monotonic() - start < 2.0
    blocked.set()  # let the abandoned daemon thread unwind


def test_open_wraps_immediate_connect_error(monkeypatch):
    def _boom(_uri):
        raise lb.libvirt.libvirtError("Cannot recv data: Connection refused")

    monkeypatch.setattr(lb.libvirt, "open", _boom)

    with pytest.raises(BackendError, match="failed"):
        _host().conn()


def test_open_returns_and_sets_keepalive_on_success(monkeypatch):
    fake = _FakeConn()
    monkeypatch.setattr(lb.libvirt, "open", lambda _uri: fake)

    host = _host()
    assert host.conn() is fake
    assert fake.keepalive == (lb._KEEPALIVE_INTERVAL_S, lb._KEEPALIVE_COUNT)
