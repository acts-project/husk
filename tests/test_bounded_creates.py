"""Bounded growth and hysteresis-guarded ramp-down."""

from __future__ import annotations

from conftest import make_config, make_controller, make_runner, make_slot, tick
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.slot import Capacity


def test_empty_bounded_create(clock):
    backend = FakeBackend(slots=[])
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=2), clock
    )

    tick(ctrl)

    assert backend.ops().count("create") == 1


def test_full_capacity_no_create(clock):
    backend = FakeBackend(
        slots=[], capacity=Capacity(can_create=False, free_instances=0)
    )
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=2), clock
    )

    tick(ctrl)

    assert "create" not in backend.ops()


def test_capacity_clamps_partial(clock):
    # desired=2 (min_ready), pool empty → need 2, but only 1 instance free.
    backend = FakeBackend(
        slots=[], capacity=Capacity(can_create=True, free_instances=1)
    )
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=2, max_total=2), clock
    )

    tick(ctrl)

    assert backend.ops().count("create") == 1


def test_rampdown_hysteresis(clock):
    # Two idle slots, desired=1 → sustained surplus must persist shrink_ticks
    # before one idle slot is decommissioned (oldest by created_at).
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-a", name="husk-a", status="ACTIVE", created_at=1.0),
            make_slot(id="vm-b", name="husk-b", status="ACTIVE", created_at=2.0),
        ]
    )
    github = FakeGitHub(
        runners=[
            make_runner(id=1, name="husk-a-c0"),
            make_runner(id=2, name="husk-b-c0"),
        ]
    )
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=2, shrink_ticks=3), clock
    )

    for _ in range(2):
        clock.advance(5)
        tick(ctrl)
    assert "destroy" not in backend.ops()  # hysteresis not yet satisfied

    clock.advance(5)
    tick(ctrl)  # third surplus tick → ramp down

    destroys = [c for c in backend.calls if c[0] == "destroy"]
    assert len(destroys) == 1
    assert destroys[0][1] == "vm-a"  # oldest idle
    assert destroys[0][2] == "decommission"  # distinct from the ERROR-only destroy
    assert ("delete_runner", 1) in github.calls


def test_downscale_retires_powered_off_excess(clock):
    # One IDLE slot + one SHUTOFF (excess) slot, desired=1. The powered-off excess
    # slot must be held off (never rebuilt) and, once surplus is sustained, retired
    # — so a downscale drains even when the pool is never fully idle. The working
    # idle slot is the one that survives.
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-a", name="husk-a", status="ACTIVE", created_at=1.0),
            make_slot(id="vm-b", name="husk-b", status="SHUTOFF", created_at=2.0),
        ]
    )
    github = FakeGitHub(runners=[make_runner(id=1, name="husk-a-c0")])
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=2, shrink_ticks=3), clock
    )

    for _ in range(2):
        clock.advance(5)
        tick(ctrl)
    # excess slot held off — neither rebuilt nor (yet) destroyed (hysteresis unmet)
    assert "rebuild" not in backend.ops()
    assert "destroy" not in backend.ops()

    clock.advance(5)
    tick(ctrl)  # third surplus tick → retire the powered-off excess slot

    destroys = [c for c in backend.calls if c[0] == "destroy"]
    assert destroys == [("destroy", "vm-b", "decommission")]  # the off one, kept vm-a
    assert "rebuild" not in backend.ops()  # excess slot was never rebuilt


def test_rampdown_resets_on_balance(clock):
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-a", name="husk-a", status="ACTIVE", created_at=1.0),
            make_slot(id="vm-b", name="husk-b", status="ACTIVE", created_at=2.0),
        ]
    )
    github = FakeGitHub(
        runners=[
            make_runner(id=1, name="husk-a-c0"),
            make_runner(id=2, name="husk-b-c0"),
        ]
    )
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=2, shrink_ticks=3), clock
    )

    clock.advance(5)
    tick(ctrl)  # surplus 1
    clock.advance(5)
    tick(ctrl)  # surplus 2

    # Load arrives: one runner goes busy → desired=2 → pool is balanced again.
    github.runners = [
        make_runner(id=1, name="husk-a-c0", busy=True),
        make_runner(id=2, name="husk-b-c0"),
    ]

    for _ in range(3):
        clock.advance(5)
        tick(ctrl)

    assert "destroy" not in backend.ops()  # hysteresis reset, nothing decommissioned
