"""Per-slot timing: cloud-init/recycle durations and the busy fraction."""

from __future__ import annotations

from conftest import make_config, make_controller, make_runner, make_slot
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.slot import SlotState
from husk.timing import SlotTiming


def test_busy_fraction_accumulates(clock):
    runner = make_runner(id=1, name="husk-1-c0", status="online", busy=True)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(runners=[runner])
    ctrl = make_controller(backend, github, make_config(max_total=1), clock)

    ctrl.tick()  # establishes BUSY at t0, no interval yet
    clock.advance(100)
    ctrl.tick()  # 100s attributed to BUSY
    clock.advance(100)
    github.runners = [make_runner(id=1, name="husk-1-c0", status="online", busy=False)]
    ctrl.tick()  # 100s attributed to BUSY (state during the interval), now IDLE

    t = ctrl.timing["vm-1"]
    assert t.state_seconds["busy"] == 200.0
    # 200s busy of 200s tracked so far -> 1.0; will fall as idle time accrues.
    assert t.busy_fraction == 1.0


def test_cloudinit_and_recycle_measured_on_bringup(clock):
    # Drive a full recycle on one slot and check the ACTIVE->online (cloud-init)
    # and issue->online (recycle) durations are captured.
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    github = FakeGitHub()
    ctrl = make_controller(backend, github, make_config(max_total=1), clock)

    ctrl.tick()  # SHUTOFF -> rebuild issued (issued_at=t), task=rebuilding
    issued = ctrl.timing["vm-1"].issued_at
    assert issued is not None

    clock.advance(8)  # rebuild settles
    backend.set_status("vm-1", status="SHUTOFF", task_state=None)
    ctrl.tick()  # pending_start drain -> os-start (fake sets ACTIVE this tick)

    clock.advance(2)
    # Next tick observes the SHUTOFF->ACTIVE transition (on_active recorded).
    ctrl.tick()
    assert ctrl.timing["vm-1"].active_at is not None

    clock.advance(60)  # cloud-init installs the runner
    github.runners = [make_runner(id=1, name="husk-1-c1", status="online", busy=False)]
    ctrl.tick()  # runner online -> cloud-init / recycle durations recorded

    t = ctrl.timing["vm-1"]
    assert t.last_cloudinit_seconds == 60.0  # ACTIVE -> online
    assert t.last_recycle_seconds is not None and t.last_recycle_seconds >= 60.0


def test_timing_surfaces_in_snapshot(clock):
    runner = make_runner(id=1, name="husk-1-c0", status="online", busy=True)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(runners=[runner])
    ctrl = make_controller(backend, github, make_config(max_total=1), clock)

    ctrl.tick()
    clock.advance(50)
    snap = ctrl.tick()

    v = snap.slots[0]
    assert v.busy_fraction == 1.0
    assert "busy_fraction" in v.__dict__ and "cloudinit_seconds" in v.__dict__


def test_slottiming_unit():
    t = SlotTiming(first_seen=0.0)
    assert t.busy_fraction is None  # no time tracked yet
    t.accumulate(SlotState.BUSY, 30)
    t.accumulate(SlotState.IDLE, 10)
    assert t.busy_fraction == 0.75
    t.on_issued(100)
    t.on_active(160)  # boot = 60
    t.on_runner_online(220)  # cloud-init = 60, recycle = 120
    assert t.last_boot_seconds == 60
    assert t.last_cloudinit_seconds == 60
    assert t.last_recycle_seconds == 120
