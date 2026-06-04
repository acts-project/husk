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
