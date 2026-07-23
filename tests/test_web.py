"""huskd's single HTTP surface (Quart): dashboard, /status, /metrics, /healthz,
and the huskctl HTTP status getter against a live server."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from conftest import (
    make_config,
    make_runner,
    make_slot,
    render_metrics,
    serve_in_thread,
)
from husk.slot import SlotState
from husk.snapshot import ControllerState
from husk.storage import DiskUsage
from husk.web import make_app, parse_addr


def _snap(backend="pool-a", *, busy=False):
    classified = [
        (
            make_slot(id="vm-1", name="husk-a-1", status="ACTIVE", cycle=2),
            make_runner(name="husk-a-1-c2", status="online", busy=busy),
            SlotState.BUSY if busy else SlotState.IDLE,
        )
    ]
    return ControllerState.from_classified(
        generation=2,
        backend=backend,
        min_ready=1,
        max_total=4,
        desired_total=1,
        classified=classified,
    )


# --------------------------------------------------------------- pure helpers
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
    text = render_metrics([_snap(backend="openstack-cern", busy=True)])
    assert 'husk_slots{backend="openstack-cern",state="busy"} 1.0' in text
    assert 'husk_slots_max_total{backend="openstack-cern"} 4.0' in text
    assert "husk_last_reconcile_timestamp_seconds" in text


def _client_get(app, path):
    async def go():
        r = await app.test_client().get(path)
        return r.status_code, (await r.get_data())

    return asyncio.run(go())


def test_dashboard_index_renders():
    code, body = _client_get(make_app(lambda: [_snap()]), "/")
    assert code == 200
    text = body.decode()
    assert "husk" in text and "/events" in text  # page subscribes to SSE


def test_status_returns_pool_list():
    code, body = _client_get(make_app(lambda: [_snap()]), "/status")
    assert code == 200
    data = json.loads(body)
    assert [p["backend"] for p in data] == ["pool-a"]
    assert data[0]["slots"][0]["name"] == "husk-a-1"


def test_metrics_endpoint():
    code, body = _client_get(make_app(lambda: [_snap()]), "/metrics")
    assert code == 200
    assert b'husk_slots_desired{backend="pool-a"}' in body


def _snap_of(*classified, backend="pool-a"):
    return ControllerState.from_classified(
        generation=1,
        backend=backend,
        min_ready=1,
        max_total=4,
        desired_total=1,
        classified=list(classified),
    )


def test_render_prometheus_slot_info():
    snap = _snap_of(
        (
            make_slot(id="vm-1", name="husk-a-1", cycle=2, ip="10.1.2.3"),
            make_runner(name="run-x", status="online"),
            SlotState.IDLE,
        )
    )
    text = render_metrics([snap])
    assert (
        'husk_slot_info{backend="pool-a",host="",ip="10.1.2.3",'
        'runner="run-x",slot="husk-a-1"} 1.0' in text
    )
    # `cycle` is a value, not an identity: as a label it minted a fresh series on
    # every recycle, which is unbounded churn for a join table.
    assert "cycle=" not in text
    assert 'husk_slot_cycle{backend="pool-a",slot="husk-a-1"} 2.0' in text


def _sd_get(app):
    code, body = _client_get(app, "/sd/targets")
    assert code == 200
    return json.loads(body)


def test_sd_targets_openstack_direct():
    snap = _snap_of(
        (
            make_slot(name="husk-a-1", ip="10.1.2.3"),
            make_runner(status="online"),
            SlotState.IDLE,
        )
    )
    groups = _sd_get(make_app(lambda: [snap]))
    assert groups == [
        {
            "targets": ["10.1.2.3:9100"],
            "labels": {
                "__metrics_path__": "/metrics",
                "backend": "pool-a",
                "slot": "husk-a-1",
            },
        }
    ]


class _FakeScraper:
    """Stands in for the SSH-backed GuestScraper."""

    def __init__(self, body=b"node_cpu_seconds_total 1\n", exc=None):
        self.body, self.exc, self.calls = body, exc, []

    async def fetch(self, backend, host, ip):
        self.calls.append((backend, host, ip))
        if self.exc:
            raise self.exc
        return self.body


def _libvirt_snap(**kw):
    return _snap_of(
        (
            make_slot(
                name=kw.get("name", "husk-g-1"),
                host=kw.get("host", "gpu-1"),
                ip=kw.get("ip", "192.168.122.57"),
            ),
            make_runner(status=kw.get("status", "online")),
            SlotState.IDLE,
        ),
        backend="pool-gpu",
    )


def test_sd_targets_libvirt_points_back_at_huskd():
    # The guest is private to its hypervisor, so Prometheus is sent back to huskd,
    # which bridges the last hop over SSH.
    app = make_app(
        lambda: [_libvirt_snap()],
        scraper=_FakeScraper(),
        advertise_addr="huskd.internal:9100",
    )
    assert _sd_get(app) == [
        {
            "targets": ["huskd.internal:9100"],
            "labels": {
                "__metrics_path__": "/slot/pool-gpu/husk-g-1/metrics",
                "backend": "pool-gpu",
                "slot": "husk-g-1",
            },
        }
    ]


def test_sd_targets_libvirt_never_scraped_directly():
    # A libvirt slot HAS an ip, but it is not routable from Prometheus. With no way
    # to bridge it, it must be dropped — never emitted as a direct 192.168.x target.
    app = make_app(lambda: [_libvirt_snap()])  # no scraper, no advertise_addr
    assert _sd_get(app) == []


def test_sd_targets_skips_offline_and_ipless():
    snap = _snap_of(
        (  # offline runner — not scrapeable yet
            make_slot(name="offline", ip="10.0.0.1"),
            make_runner(status="offline"),
            SlotState.STARTING,
        ),
        (  # online but no DHCP lease yet — nothing to route to
            make_slot(name="noip", host="gpu-1"),
            make_runner(status="online"),
            SlotState.IDLE,
        ),
    )
    app = make_app(lambda: [snap], scraper=_FakeScraper(), advertise_addr="h:9100")
    assert _sd_get(app) == []


def test_slot_metrics_bridges_to_the_guest():
    sc = _FakeScraper(body=b"node_memory_MemFree_bytes 42\n")
    app = make_app(lambda: [_libvirt_snap()], scraper=sc, advertise_addr="h:9100")
    code, body = _client_get(app, "/slot/pool-gpu/husk-g-1/metrics")
    assert code == 200
    assert b"node_memory_MemFree_bytes 42" in body
    # It routed to the right hypervisor + guest IP.
    assert sc.calls == [("pool-gpu", "gpu-1", "192.168.122.57")]


def test_slot_metrics_is_not_an_open_relay():
    # The slot must be resolved from huskd's OWN snapshot, never trusted from the
    # URL — so an unknown slot is a 404, not an attempt to reach something.
    sc = _FakeScraper()
    app = make_app(lambda: [_libvirt_snap()], scraper=sc, advertise_addr="h:9100")
    assert _client_get(app, "/slot/pool-gpu/not-a-slot/metrics")[0] == 404
    assert _client_get(app, "/slot/other-pool/husk-g-1/metrics")[0] == 404
    assert sc.calls == []  # never even attempted a fetch


def test_slot_metrics_reports_a_failed_scrape_as_5xx():
    # Prometheus turns a 5xx into up==0, which is the honest signal for "this guest
    # is unreachable" — better than serving stale or empty metrics as a 200.
    from husk.guest_scrape import GuestScrapeError

    sc = _FakeScraper(exc=GuestScrapeError("host unreachable"))
    app = make_app(lambda: [_libvirt_snap()], scraper=sc, advertise_addr="h:9100")
    assert _client_get(app, "/slot/pool-gpu/husk-g-1/metrics")[0] == 502


def test_slot_metrics_503_when_lease_missing():
    snap = _libvirt_snap(ip=None)
    app = make_app(lambda: [snap], scraper=_FakeScraper(), advertise_addr="h:9100")
    assert _client_get(app, "/slot/pool-gpu/husk-g-1/metrics")[0] == 503


# ── on-demand serial console ──────────────────────────────────────────────────
# Boot timing comes from the guest itself now (node_exporter textfile), and
# nothing polls the console. It survives for the case metrics structurally
# cannot cover: a slot that died before the exporter ever started.


def test_slot_console_returns_the_text():
    calls = []

    def provider(backend, slot_id):
        calls.append((backend, slot_id))
        return "===== husk-bootreport =====\nrunner_started 18.6\n"

    app = make_app(lambda: [_libvirt_snap()], console_provider=provider)
    code, body = _client_get(app, "/slot/pool-gpu/husk-g-1/console")
    assert code == 200
    assert b"runner_started 18.6" in body
    # Resolved to the slot's ID from the snapshot, not the name in the URL.
    assert calls == [("pool-gpu", "vm-1")]


def test_slot_console_is_not_an_open_relay():
    calls = []
    app = make_app(
        lambda: [_libvirt_snap()],
        console_provider=lambda b, s: calls.append((b, s)) or "x",
    )
    assert _client_get(app, "/slot/pool-gpu/not-a-slot/console")[0] == 404
    assert _client_get(app, "/slot/other-pool/husk-g-1/console")[0] == 404
    assert calls == []


def test_slot_console_503_without_a_provider():
    app = make_app(lambda: [_libvirt_snap()])
    assert _client_get(app, "/slot/pool-gpu/husk-g-1/console")[0] == 503


def test_slot_console_503_when_the_backend_has_none():
    # libvirt has no captured serial log yet, so its console_output returns None.
    app = make_app(lambda: [_libvirt_snap()], console_provider=lambda b, s: None)
    assert _client_get(app, "/slot/pool-gpu/husk-g-1/console")[0] == 503


def test_slot_console_reports_a_raising_provider_as_502():
    def boom(backend, slot_id):
        raise RuntimeError("cloud said no")

    app = make_app(lambda: [_libvirt_snap()], console_provider=boom)
    assert _client_get(app, "/slot/pool-gpu/husk-g-1/console")[0] == 502


def test_metrics_concats_pools():
    snaps = [_snap("pool-a"), _snap("pool-b")]
    code, body = _client_get(make_app(lambda: snaps), "/metrics")
    assert code == 200
    assert b'husk_slots_desired{backend="pool-a"}' in body
    assert b'husk_slots_desired{backend="pool-b"}' in body


def test_metrics_emits_storage_once_across_pools():
    """The storage block is daemon-wide: repeating it per pool would be a
    duplicate-series exposition error."""
    usage = [
        DiskUsage(kind="cache", host="", images=2, total_bytes=350),
        DiskUsage(kind="golden", host="hv1", images=1, total_bytes=100),
    ]
    app = make_app(
        lambda: [_snap("pool-a"), _snap("pool-b")], storage_provider=lambda: usage
    )
    code, body = _client_get(app, "/metrics")

    assert code == 200
    assert body.count(b"# TYPE husk_image_bytes gauge") == 1
    assert body.count(b'husk_image_bytes{host="",kind="cache"} 350.0') == 1
    assert b'husk_slots_desired{backend="pool-b"}' in body  # pool metrics intact


def test_metrics_without_a_storage_provider_emits_no_storage_samples():
    """The family is still declared (HELP/TYPE with no samples is valid, and keeps
    the metric's existence discoverable) — but nothing is measured, so no series."""
    code, body = _client_get(make_app(lambda: [_snap()]), "/metrics")

    assert code == 200
    assert b"# TYPE husk_image_bytes gauge" in body
    assert not [
        ln for ln in body.decode().splitlines() if ln.startswith("husk_image_bytes{")
    ]


def test_healthz_ok_when_fresh():
    code, body = _client_get(make_app(lambda: [_snap()]), "/healthz")
    assert code == 200 and body == b"ok\n"


def test_healthz_503_when_no_pools():
    code, body = _client_get(make_app(lambda: []), "/healthz")
    assert code == 503 and body == b"stale\n"


def test_livez_ok_even_with_no_pools():
    # Liveness is process-level on purpose: it is what the k8s probes (readiness
    # included) dial, so a pool that has never reconciled — a cold start uploading
    # a golden — must not cost us the dashboard/metrics endpoint or a restart.
    code, body = _client_get(make_app(lambda: []), "/livez")
    assert code == 200 and body == b"ok\n"


def test_livez_ok_when_every_pool_is_stale():
    stale = replace(_snap(), last_reconcile_epoch=time.time() - 3600)
    code, body = _client_get(make_app(lambda: [stale]), "/livez")
    assert code == 200 and body == b"ok\n"


def test_healthz_503_when_stale():
    stale = replace(_snap(), last_reconcile_epoch=time.time() - 120)
    code, body = _client_get(make_app(lambda: [stale]), "/healthz")
    assert code == 503 and body == b"stale\n"


# ------------------------------------------------------- live server + getter
def test_live_server_serves_endpoints():
    snaps = [_snap()]  # fresh snapshot → /healthz is healthy
    with serve_in_thread(lambda: snaps) as base:
        with urllib.request.urlopen(base + "/status", timeout=5) as r:
            assert r.status == 200
            assert json.loads(r.read())[0]["backend"] == "pool-a"
        with urllib.request.urlopen(base + "/metrics", timeout=5) as r:
            assert b"husk_slots{" in r.read()
        with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
            assert r.status == 200


def test_cli_http_getter_reads_live_server():
    from husk.cli import _snapshot_getter

    snaps = [_snap()]
    with serve_in_thread(lambda: snaps) as base:
        cfg = make_config()
        cfg = replace(
            cfg,
            controller=replace(cfg.controller, http_addr=base.removeprefix("http://")),
        )
        got = _snapshot_getter([cfg])()
        assert [s.to_dict() for s in got] == [s.to_dict() for s in snaps]
