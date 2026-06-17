"""The read-only HTTP status/metrics surface."""

from __future__ import annotations

import json
import urllib.request

import pytest

from conftest import make_runner, make_slot
from husk.http_server import StatusServer, parse_addr, render_prometheus
from husk.slot import SlotState
from husk.snapshot import ControllerState


def _snap():
    classified = [
        (
            make_slot(id="vm-1", name="husk-1", status="ACTIVE", cycle=2),
            make_runner(name="husk-1-c2", status="online", busy=True),
            SlotState.BUSY,
        ),
        (
            make_slot(id="vm-2", name="husk-2", status="SHUTOFF"),
            None,
            SlotState.NEEDS_RECYCLE,
        ),
    ]
    return ControllerState.from_classified(
        generation=3,
        backend="openstack-cern",
        min_ready=1,
        max_total=4,
        desired_total=2,
        classified=classified,
    )


@pytest.mark.parametrize(
    "addr, expected",
    [
        ("127.0.0.1:9100", ("127.0.0.1", 9100)),
        (":9100", ("0.0.0.0", 9100)),
        ("9100", ("0.0.0.0", 9100)),
    ],
)
def test_parse_addr(addr, expected):
    assert parse_addr(addr) == expected


def test_render_prometheus():
    text = render_prometheus(_snap())
    assert 'husk_slots{backend="openstack-cern",state="busy"} 1' in text
    assert 'husk_slots{backend="openstack-cern",state="needs_recycle"} 1' in text
    assert 'husk_slots_max_total{backend="openstack-cern"} 4' in text
    assert "husk_last_reconcile_timestamp_seconds" in text


def _serve(provider):
    server = StatusServer(provider, "127.0.0.1", 0)
    server.start()
    host, port = server.address
    return server, f"http://{host}:{port}"


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def test_http_status_metrics_healthz():
    # Multi-pool: the provider yields a list of per-pool snapshots.
    snaps = [_snap()]
    server, base = _serve(lambda: snaps)
    try:
        code, body = _get(base + "/status")
        assert code == 200
        listed = json.loads(body)
        assert isinstance(listed, list) and len(listed) == 1
        assert ControllerState.from_dict(listed[0]).to_dict() == snaps[0].to_dict()

        code, body = _get(base + "/metrics")
        assert code == 200 and b"husk_slots{" in body

        code, _ = _get(base + "/healthz")  # fresh snapshot → healthy
        assert code == 200
    finally:
        server.stop()


def test_http_metrics_concats_pools():
    a = ControllerState.from_classified(
        generation=1,
        backend="pool-a",
        min_ready=1,
        max_total=2,
        desired_total=1,
        classified=[],
    )
    b = ControllerState.from_classified(
        generation=1,
        backend="pool-b",
        min_ready=1,
        max_total=2,
        desired_total=1,
        classified=[],
    )
    server, base = _serve(lambda: [a, b])
    try:
        _, body = _get(base + "/metrics")
        assert b'husk_slots_desired{backend="pool-a"}' in body
        assert b'husk_slots_desired{backend="pool-b"}' in body
    finally:
        server.stop()


def test_http_healthz_503_when_no_pools():
    server, base = _serve(lambda: [])
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(base + "/healthz")
        assert ei.value.code == 503
        code, body = _get(base + "/status")  # empty list, but a valid 200
        assert code == 200 and json.loads(body) == []
    finally:
        server.stop()


def test_cli_http_getter_reads_server():
    # The huskctl status HTTP getter fetches and parses huskd's served snapshot
    # list (one entry per pool).
    from dataclasses import replace

    from conftest import make_config
    from husk.cli import _snapshot_getter

    snaps = [_snap()]
    server, base = _serve(lambda: snaps)
    try:
        cfg = make_config()
        host_port = base.removeprefix("http://")
        cfg = replace(cfg, controller=replace(cfg.controller, http_addr=host_port))
        getter = _snapshot_getter([cfg], live=False)
        got = getter()
        assert [s.to_dict() for s in got] == [s.to_dict() for s in snaps]
    finally:
        server.stop()
