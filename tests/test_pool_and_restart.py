"""Pool sizing under load and controller-restart durability."""

from __future__ import annotations

import time

from conftest import FakeClock, make_config, make_controller, make_runner, make_slot
from husk.fake_backend import FakeBackend, FakeGitHub


def test_busy_slot_triggers_warm_spare(clock):
    # busy=1, min_ready=1, max_total=2 → desired=2 → create one warm spare.
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(runners=[make_runner(name="husk-1-c0", busy=True)])
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=2), clock
    )

    ctrl.tick()

    assert backend.ops().count("create") == 1


def test_restart_does_not_rebuild_healthy_slots():
    # A fresh controller (lost in-memory state) seeing an ACTIVE+online slot with
    # no durable provisioned_at must NOT immediately rebuild it: first sight
    # grants a fresh startup grace.
    clock = FakeClock(t=5000.0)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(runners=[make_runner(name="husk-1-c0", busy=False)])
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=1, startup_grace=10), clock
    )

    ctrl.tick()
    assert backend.calls == []  # idle, left alone — no rebuild/destroy


def test_restart_uses_durable_provisioned_at():
    # An ACTIVE slot with no runner and an OLD durable provisioned_at should be
    # judged past grace on first sight (restart) and rebuilt, not given a free pass.
    clock = FakeClock(t=5000.0)
    old = time.time() - 9999  # provisioned long ago (wall-clock metadata)
    backend = FakeBackend(
        slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE", provisioned_at=old)]
    )
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=1, startup_grace=10), clock
    )

    ctrl.tick()

    assert "rebuild" in backend.ops()


def test_long_build_gets_fresh_grace_at_active(clock):
    # Repro: a fresh create spends longer in BUILD than startup_grace (CERN's
    # ~5min Neutron phase). When it finally reaches ACTIVE with no runner yet, it
    # must be STARTING (grace anchored to ACTIVE), NOT UNHEALTHY → rebuilt.
    backend = FakeBackend(slots=[])
    github = FakeGitHub()
    cfg = make_config(min_ready=1, max_total=1, startup_grace=300)
    ctrl = make_controller(backend, github, cfg, clock)

    ctrl.tick()  # creates one slot (BUILD)
    sid = backend.slots[0].id

    # Long build, well past the grace window — still BUILD → STARTING, no rebuild.
    backend.set_status(sid, status="BUILD", task_state=None)
    clock.advance(600)
    ctrl.tick()
    assert "rebuild" not in backend.ops()

    # Now it boots: BUILD → ACTIVE, runner not registered yet.
    backend.set_status(sid, status="ACTIVE", task_state=None)
    clock.advance(10)
    ctrl.tick()  # grace restarts at ACTIVE → STARTING, NOT unhealthy
    assert "rebuild" not in backend.ops()

    # Only once the grace elapses *after* ACTIVE (cloud-init genuinely failed)
    # does it become UNHEALTHY and get rebuilt.
    clock.advance(400)
    ctrl.tick()
    assert "rebuild" in backend.ops()


def test_active_transition_persists_grace_origin(clock):
    # After huskd sees a slot reach ACTIVE, a *separate* stateless observer (what
    # `huskctl status` is) must NOT independently classify it UNHEALTHY — huskd
    # persists the grace origin to durable metadata so the observer agrees.
    import time as _t

    old = _t.time() - 600  # created long ago (wall clock), still installing runner
    backend = FakeBackend(
        slots=[make_slot(id="vm-1", name="husk-1", status="BUILD", provisioned_at=old)]
    )
    github = FakeGitHub()
    cfg = make_config(min_ready=1, max_total=1, startup_grace=300)

    huskd = make_controller(backend, github, cfg, clock)
    huskd.tick()  # BUILD -> STARTING, prev=BUILD
    backend.set_status("vm-1", status="ACTIVE", task_state=None)
    huskd.tick()  # ACTIVE transition -> mark_active persists a fresh origin

    assert ("mark_active", "vm-1") in backend.calls
    assert backend.slots[0].provisioned_at > old  # durable origin refreshed

    # A fresh, stateless controller observing the same backend now reads STARTING.
    snap = make_controller(backend, github, cfg, FakeClock()).observe()
    assert snap.counts["unhealthy"] == 0
    assert snap.counts["starting"] == 1


def test_durable_cycle_seeds_runner_name():
    # husk-cycle metadata=4 → next recycle mints cycle 5 (unique JIT name across
    # restarts without relying on the 409-retry).
    clock = FakeClock()
    backend = FakeBackend(
        slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF", cycle=4)]
    )
    github = FakeGitHub()
    ctrl = make_controller(backend, github, make_config(), clock)

    ctrl.tick()

    assert ("mint", "husk-1-c5") in github.calls
