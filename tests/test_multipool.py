"""MultiPoolController: pools reconcile independently, never cross-delete each
other's runners, and one pool's failure can't suppress the others' snapshots."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
import time

from conftest import TEST_TARGET, FakeClock, make_config, make_slot, pump, tick_all
from husk.controller import Controller
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.multipool import MultiPoolController
from husk.slot import Runner


def _pool(name, prefix, *, github=None, slots=None, runners=None, **cfg_kw):
    backend = FakeBackend(slots=slots)
    gh = github or FakeGitHub(runners=runners)
    cfg = make_config(**cfg_kw)
    cfg = dataclasses.replace(
        cfg,
        backend=dataclasses.replace(cfg.backend, name=name, vm_prefix=prefix),
        controller=dataclasses.replace(cfg.controller, http_addr=""),
    )
    ctrl = Controller(backend, gh, cfg, clock=FakeClock(), target=TEST_TARGET)
    return ctrl, backend, gh


def test_pools_size_independently():
    a, backend_a, _ = _pool("pool-a", "husk-a", min_ready=1, max_total=2)
    b, backend_b, _ = _pool("pool-b", "husk-b", min_ready=2, max_total=3)
    tick_all(MultiPoolController([a, b]))

    assert len(backend_a.slots) == 1 and len(backend_b.slots) == 2
    assert all(s.name.startswith("husk-a-") for s in backend_a.slots)
    assert all(s.name.startswith("husk-b-") for s in backend_b.slots)


def test_no_cross_pool_runner_deletion():
    # One shared GitHub repo: both pools' clients see ALL runners (repo-wide API).
    shared = FakeGitHub(
        runners=[
            Runner(id=1, name="husk-a-1-c0", status="online", busy=False),
            Runner(id=2, name="husk-b-1-c0", status="online", busy=False),
        ]
    )
    # pool-a's idle slot is stale → it deregisters ITS runner this tick.
    a, _, _ = _pool(
        "pool-a",
        "husk-a",
        github=shared,
        slots=[make_slot(id="a1", name="husk-a-1", status="ACTIVE", image_stale=True)],
    )
    # pool-b's idle slot is fresh → no action.
    b, _, _ = _pool(
        "pool-b",
        "husk-b",
        github=shared,
        slots=[make_slot(id="b1", name="husk-b-1", status="ACTIVE")],
    )
    tick_all(MultiPoolController([a, b]))

    deleted = [c for c in shared.calls if c[0] == "delete_runner"]
    assert deleted == [("delete_runner", 1)]  # only pool-a's own runner, by prefix
    assert {r.id for r in shared.runners} == {2}


def test_one_pool_raise_does_not_block_others(monkeypatch):
    a, _, _ = _pool("pool-a", "husk-a", min_ready=1)
    b, _, _ = _pool("pool-b", "husk-b", min_ready=1)
    monkeypatch.setattr(a, "tick", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    facade = MultiPoolController([a, b])
    tick_all(facade)

    # snapshots() reads each pool's in-memory state (no file).
    snaps = {s.backend: s for s in facade.snapshots()}
    assert set(snaps) == {"pool-a", "pool-b"}
    # pool-b ticked (generation advanced); pool-a stayed at its seeded snapshot.
    assert snaps["pool-b"].generation >= 1
    assert snaps["pool-a"].generation == 0


def _fast_poll(ctrl, seconds=0.01):
    ctrl.cfg = dataclasses.replace(
        ctrl.cfg,
        timeouts=dataclasses.replace(ctrl.cfg.timeouts, poll_interval_sec=seconds),
    )


def test_stalled_pool_does_not_block_another():
    # The point of per-pool reconcile TASKS plus to_thread'd backend calls: a pool
    # wedged in a blocking backend call (a frozen libvirt host) must not stall the
    # event loop, another pool's ticks, or its snapshot. The wedge below blocks a
    # worker thread exactly the way a hung Nova/libvirt call does.
    a, _, _ = _pool("pool-a", "husk-a", min_ready=1)
    b, _, _ = _pool("pool-b", "husk-b", min_ready=1)
    _fast_poll(a)
    _fast_poll(b)

    release = threading.Event()
    orig_tick = a.tick

    async def wedged_tick():
        await asyncio.to_thread(release.wait, 5)
        return await orig_tick()

    a.tick = wedged_tick
    facade = MultiPoolController([a, b])

    async def go():
        stop = asyncio.Event()
        for c in (a, b):
            await pump(c)  # warm both registries as the real poller would
        task = asyncio.create_task(facade.run(stop))
        deadline = time.monotonic() + 3
        while b.snapshot.generation < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert b.snapshot.generation >= 1  # pool-b reconciled independently
        assert a.snapshot.generation == 0  # pool-a still stuck in its own tick
        release.set()
        stop.set()
        await asyncio.wait_for(task, timeout=5)  # stop() drains every pool task

    asyncio.run(go())


def test_reload_matches_by_name_without_structural_warning(caplog):
    a, _, _ = _pool("pool-a", "husk-a", min_ready=1, max_total=2)
    # The reload yields the file's REAL controller http_addr; the facade must
    # normalize it (facade owns http) so no spurious "structural changes" warning.
    new = dataclasses.replace(
        make_config(min_ready=3, max_total=5),
        backend=dataclasses.replace(a.cfg.backend, min_ready=3, max_total=5),
        controller=dataclasses.replace(a.cfg.controller, http_addr="127.0.0.1:9100"),
    )
    facade = MultiPoolController([a], reload_configs=lambda: [new])

    with caplog.at_level(logging.WARNING):
        facade._maybe_reload()

    assert a.cfg.backend.min_ready == 3 and a.cfg.backend.max_total == 5
    assert "structural changes ignored" not in caplog.text
