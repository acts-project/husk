"""Per-slot timing: cloud-init/recycle durations and the busy fraction."""

from __future__ import annotations

from conftest import make_config, make_controller, make_runner, make_slot
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.slot import SlotState
from husk.timing import SlotTiming


def test_live_fraction_accumulates(clock):
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
    # busy + idle = available to serve; 200s of 200s tracked so far -> 1.0.
    assert t.live_fraction == 1.0


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
    assert v.live_fraction == 1.0
    assert "live_fraction" in v.__dict__ and "cloudinit_seconds" in v.__dict__


def test_slottiming_unit():
    t = SlotTiming(first_seen=0.0)
    assert t.live_fraction is None  # no time tracked yet
    # 30s busy + 10s idle = 40s available; +20s starting = 60s total -> 40/60.
    t.accumulate(SlotState.BUSY, 30)
    t.accumulate(SlotState.IDLE, 10)
    t.accumulate(SlotState.STARTING, 20)
    assert t.live_fraction == 40 / 60
    t.on_issued(100)
    t.on_active(160)  # boot = 60
    t.on_runner_online(220)  # cloud-init = 60, recycle = 120
    assert t.last_boot_seconds == 60
    assert t.last_cloudinit_seconds == 60
    assert t.last_recycle_seconds == 120


def test_slottiming_on_bootreport():
    t = SlotTiming(first_seen=0.0)
    t.on_bootreport(kernel=2.1, initrd=None, userspace=8.9, total=11.0)
    assert t.last_boot_kernel_seconds == 2.1
    assert t.last_boot_initrd_seconds is None
    assert t.last_boot_userspace_seconds == 8.9
    assert t.last_boot_total_seconds == 11.0


# A minimal husk-bootreport console block (systemd-analyze time only).
def _block(ts: str, total: float) -> str:
    return (
        f"===== husk-bootreport {ts} =====\n"
        f"Startup finished in 2.1s (kernel) + 8.9s (userspace) = {total}s\n"
        "===== husk-bootreport end =====\n"
    )


# A block that also carries the two blame sections (as journald prefixes them).
def _block_with_blame(ts: str) -> str:
    return (
        f"[   1.0] sh[1]: ===== husk-bootreport {ts} =====\n"
        "[   1.1] systemd-analyze[2]: Startup finished in 2.1s (kernel) = 11.0s\n"
        "[   1.2] sh[3]: 2.923s cloud-init-local.service\n"
        "[   1.3] cloud-init[4]:      02.26400s (init-local/search-OpenStackLocal)\n"
        "[   1.4] sh[5]: ===== husk-bootreport end =====\n"
    )


def _console_reads(backend) -> int:
    return len(backend.console_reads)


def test_bootreport_captured_from_console(clock):
    runner = make_runner(id=1, name="husk-1-c0", status="online", busy=False)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    backend.console_text = _block("2026-07-10T12:00:00Z", 11.0)
    github = FakeGitHub(runners=[runner])
    ctrl = make_controller(backend, github, make_config(max_total=1), clock)

    snap = ctrl.tick()

    t = ctrl.timing["vm-1"]
    assert t.last_boot_kernel_seconds == 2.1
    assert t.last_boot_userspace_seconds == 8.9
    assert t.last_boot_total_seconds == 11.0
    assert snap.slots[0].boot_total_seconds == 11.0

    # Captured once: a subsequent tick must not re-read the console.
    reads = _console_reads(backend)
    ctrl.tick()
    assert _console_reads(backend) == reads


def test_bootreport_blame_surfaces_in_snapshot(clock):
    runner = make_runner(id=1, name="husk-1-c0", status="online", busy=False)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    backend.console_text = _block_with_blame("2026-07-10T12:00:00Z")
    github = FakeGitHub(runners=[runner])
    ctrl = make_controller(backend, github, make_config(max_total=1), clock)

    snap = ctrl.tick()

    v = snap.slots[0]
    assert v.boot_units[0] == ("cloud-init-local.service", 2.9)
    assert v.boot_cloudinit_stages[0] == ("init-local/search-OpenStackLocal", 2.3)


def test_bootreport_stale_block_rejected_until_new_ts(clock):
    runner = make_runner(id=1, name="husk-1-c0", status="online", busy=False)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    backend.console_text = _block("2026-07-10T12:00:00Z", 11.0)
    github = FakeGitHub(runners=[runner])
    ctrl = make_controller(backend, github, make_config(max_total=1), clock)

    ctrl.tick()
    assert ctrl.timing["vm-1"].last_boot_total_seconds == 11.0

    # Re-arm capture exactly as the next bring-up (_rebuild_then_start) does: clear
    # captured/attempts but KEEP bootreport_last_ts, so the previous cycle's block —
    # still in the console ring buffer — is rejected.
    ctrl.bootreport_captured.discard("vm-1")
    ctrl.bootreport_attempts.pop("vm-1", None)

    ctrl.tick()  # console still shows the old ts -> rejected, timing unchanged
    assert "vm-1" not in ctrl.bootreport_captured
    assert ctrl.timing["vm-1"].last_boot_total_seconds == 11.0

    backend.console_text = _block("2026-07-10T12:05:00Z", 22.0)  # newer cycle flushed
    ctrl.tick()
    assert "vm-1" in ctrl.bootreport_captured
    assert ctrl.timing["vm-1"].last_boot_total_seconds == 22.0


def test_bootreport_attempts_bounded(clock):
    # No block on the console (block never flushes): reads stop at the cap.
    from husk.controller import BOOTREPORT_MAX_ATTEMPTS

    runner = make_runner(id=1, name="husk-1-c0", status="online", busy=False)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    backend.console_text = "no bootreport here\n"
    github = FakeGitHub(runners=[runner])
    ctrl = make_controller(backend, github, make_config(max_total=1), clock)

    for _ in range(BOOTREPORT_MAX_ATTEMPTS + 3):
        ctrl.tick()

    assert _console_reads(backend) == BOOTREPORT_MAX_ATTEMPTS
    assert ctrl.timing["vm-1"].last_boot_total_seconds is None
