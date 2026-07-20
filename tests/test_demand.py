"""Demand-signal seam: the registry keying, and that reconcile now sizes through
it (the published `desired` matches the snapshot, and the value is unchanged from
the old inline math)."""

from __future__ import annotations

from conftest import (
    FakeClock,
    make_config,
    make_controller,
    make_runner,
    make_slot,
    tick,
)
from husk.demand import DemandRegistry
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.target import Target


def test_registry_keys_by_target_and_pool():
    reg = DemandRegistry()
    t = Target.repo("acts-project/husk-test")
    assert reg.get(t, "gpu") is None
    assert reg.desired(t, "gpu") is None

    reg.publish(t, "gpu", busy=2, desired=3)
    d = reg.get(t, "gpu")
    assert d is not None and d.busy == 2 and d.desired == 3
    assert reg.desired(t, "gpu") == 3

    # Same target, different pool is a distinct entry (and different target too).
    assert reg.desired(t, "cpu") is None
    assert reg.desired(Target.org("acts-project"), "gpu") is None


def test_tick_publishes_desired_matching_snapshot():
    # 1 busy slot + min_ready 1 → desired 2 (capped at max_total 3).
    backend = FakeBackend(slots=[make_slot(id="s1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(runners=[make_runner(name="husk-1-c0", busy=True)])
    cfg = make_config(min_ready=1, max_total=3)
    ctrl = make_controller(backend, github, cfg, FakeClock())

    snap = tick(ctrl)

    assert snap.desired_total == 2  # busy(1) + min_ready(1)
    published = ctrl.demand.get(ctrl.target, ctrl.pool)
    assert published is not None
    assert published.busy == 1
    assert published.desired == snap.desired_total


def test_target_defaults_to_configured_repo():
    ctrl = make_controller(FakeBackend(), FakeGitHub(), make_config(), FakeClock())
    assert ctrl.target == Target.repo("acts-project/husk-test")
    assert ctrl.pool == "fake"
