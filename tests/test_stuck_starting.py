"""The STARTING liveness backstop, and the pending-start trap it closes.

Regression suite for an observed incident: slots the controller had rebuilt sat
in "starting" across a whole weekend. Two independent defects kept them there —
STARTING had no deadline of any kind, and `pending_start` short-circuited the
remediation loop *before* the ERROR branch, so a slot that broke mid-rebuild was
skipped on every subsequent tick."""

from __future__ import annotations

from conftest import (
    make_config,
    make_controller,
    make_runner,
    make_slot,
    tick,
)
from husk.fake_backend import FakeBackend, FakeGitHub


def _reasons(backend: FakeBackend) -> list[str]:
    return [c[2] for c in backend.calls if c[0] == "destroy"]


def test_wedged_task_state_is_destroyed_after_backstop(clock):
    # The incident shape: Nova sets a task_state that never clears. classify()
    # tests task_state before it ever consults startup_grace, so the slot reads
    # STARTING forever — no rebuild, no destroy, no timeout. It sat for days.
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-1", name="husk-1", status="ACTIVE", task_state="spawning")
        ]
    )
    github = FakeGitHub()
    cfg = make_config(
        min_ready=1, max_total=1, startup_grace=300, starting_timeout=1800
    )
    ctrl = make_controller(backend, github, cfg, clock)

    tick(ctrl)
    assert backend.ops().count("destroy") == 0  # settling; leave it alone

    clock.advance(1700)  # past the grace, still inside the backstop
    tick(ctrl)
    assert backend.ops().count("destroy") == 0

    clock.advance(200)  # 1900s in STARTING — past the backstop
    tick(ctrl)
    assert _reasons(backend) == ["stuck_starting"]


def test_build_that_never_settles_is_destroyed(clock):
    # Same backstop, other route in: a create that never leaves BUILD.
    backend = FakeBackend(slots=[])
    github = FakeGitHub()
    cfg = make_config(min_ready=1, max_total=1, starting_timeout=1800)
    ctrl = make_controller(backend, github, cfg, clock)

    tick(ctrl)  # creates one slot, in BUILD
    sid = backend.slots[0].id
    backend.set_status(sid, status="BUILD", task_state=None)

    # The backstop clock starts when the slot is first *classified*, i.e. the tick
    # after the one that created it — deliberately conservative.
    tick(ctrl)
    clock.advance(2000)
    tick(ctrl)

    assert _reasons(backend) == ["stuck_starting"]


def test_backstop_spares_a_slot_running_a_job(clock):
    # classify() checks task_state BEFORE the runner, so a healthy busy slot whose
    # server picks up a task (snapshot, live migration) reads as STARTING. The
    # backstop must never destroy that — it would kill a running job.
    backend = FakeBackend(
        slots=[
            make_slot(
                id="vm-1", name="husk-1", status="ACTIVE", task_state="image_snapshot"
            )
        ]
    )
    github = FakeGitHub(runners=[make_runner(name="husk-1-c0", busy=True)])
    cfg = make_config(min_ready=1, max_total=1, starting_timeout=1800)
    ctrl = make_controller(backend, github, cfg, clock)

    clock.advance(9999)
    tick(ctrl)

    assert "destroy" not in backend.ops()


def test_pending_start_slot_in_error_is_destroyed(clock):
    # The trap: a rebuilt slot lands in ERROR while it sits in pending_start.
    # _drain_pending_start matches neither SHUTOFF nor ACTIVE, so before the fix
    # it returned without discarding and the loop's short-circuit hid the slot
    # from the ERROR branch on every tick thereafter — permanently.
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=1), clock
    )

    tick(ctrl)  # NEEDS_RECYCLE → rebuild; slot enters pending_start
    assert "rebuild" in backend.ops()
    assert "vm-1" in ctrl.pending_start

    backend.set_status("vm-1", status="ERROR", task_state=None)
    clock.advance(30)
    tick(ctrl)

    assert _reasons(backend) == ["error"]
    assert "vm-1" not in ctrl.pending_start


def test_pending_start_unexpected_status_is_released(clock):
    # A pending-start slot in a status the drain cannot act on must be let go
    # rather than held, so normal remediation sees it again next tick.
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=1), clock
    )

    tick(ctrl)
    assert "vm-1" in ctrl.pending_start

    backend.set_status("vm-1", status="PAUSED", task_state=None)
    clock.advance(30)
    tick(ctrl)

    assert "vm-1" not in ctrl.pending_start


def test_normal_rebuild_start_path_is_unaffected(clock):
    # Guard rail for the reordering: the happy path still os-starts a settled
    # rebuild rather than treating it as a fresh NEEDS_RECYCLE.
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=1), clock
    )

    tick(ctrl)  # rebuild issued; fake sets task_state="rebuilding"
    clock.advance(30)
    backend.set_status("vm-1", status="SHUTOFF", task_state=None)  # rebuild settled
    tick(ctrl)

    assert backend.ops().count("rebuild") == 1
    assert backend.ops().count("start") == 1
    assert "destroy" not in backend.ops()
    assert "vm-1" not in ctrl.pending_start
