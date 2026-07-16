"""The Phase 5 fail-safe matrix — the testable core of Phase 5.

Every test asserts *exactly* which actions a tick took against the in-memory
fakes. The hard invariant (a listing failure aborts the tick) leads.
"""

from __future__ import annotations

from conftest import make_config, make_controller, make_runner, make_slot
from husk.fake_backend import FakeBackend, FakeGitHub


def _run(backend, github, config, clock):
    return make_controller(backend, github, config, clock)


def test_list_raises_no_destroy(clock):
    # A SHUTOFF slot would normally be rebuilt — but if listing fails, do nothing.
    backend = FakeBackend(slots=[make_slot(status="SHUTOFF")])
    backend.raise_on_list = True
    github = FakeGitHub()
    ctrl = _run(backend, github, make_config(), clock)

    ctrl.tick()

    assert backend.calls == []  # no destroy, no rebuild, no create
    assert github.calls == []  # no mint, no delete_runner


def test_github_list_raises_no_mutation(clock):
    backend = FakeBackend(slots=[make_slot(status="SHUTOFF")])
    github = FakeGitHub()
    github.raise_on_list = True
    ctrl = _run(backend, github, make_config(), clock)

    ctrl.tick()

    assert backend.calls == []
    assert github.calls == []


def test_error_destroy_then_recreate_next_tick(clock):
    backend = FakeBackend(slots=[make_slot(id="vm-err", status="ERROR")])
    github = FakeGitHub()
    ctrl = _run(backend, github, make_config(min_ready=1, max_total=2), clock)

    ctrl.tick()  # destroys the ERROR slot; does NOT create same tick
    assert backend.calls[0][:2] == ("destroy", "vm-err")
    assert backend.calls[0][2] == "error"
    assert "create" not in backend.ops()

    backend.calls.clear()
    ctrl.tick()  # pool now empty → grow to min_ready
    assert backend.ops() == ["create"]


def test_shutoff_rebuild_not_destroy(clock):
    backend = FakeBackend(slots=[make_slot(id="vm-1", status="SHUTOFF")])
    github = FakeGitHub()
    ctrl = _run(backend, github, make_config(), clock)

    ctrl.tick()

    assert "rebuild" in backend.ops()
    assert "destroy" not in backend.ops()
    assert "mint" in github.ops()  # fresh JIT minted for the recycle


def test_rebuild_failure_surfaces_on_the_slot_then_clears(clock):
    # A rejected rebuild (e.g. CERN Nova 500) must be visible per slot on the
    # dashboard, not just logged — and clear once a later rebuild succeeds.
    backend = FakeBackend(slots=[make_slot(id="vm-1", status="SHUTOFF")])
    backend.raise_on_rebuild = True
    ctrl = _run(backend, FakeGitHub(), make_config(), clock)

    ctrl.tick()  # rebuild attempt raises
    v = next(s for s in ctrl.snapshot.slots if s.id == "vm-1")
    assert v.error and "rebuild failed" in v.error
    assert v.error_epoch is not None

    backend.raise_on_rebuild = False  # backend recovers
    ctrl.tick()  # rebuild now succeeds
    v = next(s for s in ctrl.snapshot.slots if s.id == "vm-1")
    assert v.error is None  # cleared


def test_backend_nonfatal_warning_surfaces_on_the_slot(clock):
    # A non-fatal backend warning (e.g. a swallowed metadata-write 500) is folded
    # into the same per-slot error column, so it's visible without the log.
    backend = FakeBackend(slots=[make_slot(id="vm-1", status="ACTIVE")])
    backend.warnings = {"vm-1": (1000.0, "metadata write failed: HTTP 500 CernLanDB")}
    ctrl = _run(
        backend,
        FakeGitHub(runners=[make_runner(name="husk-1-c0")]),
        make_config(),
        clock,
    )

    ctrl.tick()
    v = next(s for s in ctrl.snapshot.slots if s.id == "vm-1")
    assert v.error == "metadata write failed: HTTP 500 CernLanDB"


def test_fatal_error_wins_over_a_backend_warning(clock):
    # If a slot has both, the fatal action error takes the column (more actionable).
    backend = FakeBackend(slots=[make_slot(id="vm-1", status="SHUTOFF")])
    backend.raise_on_rebuild = True
    backend.warnings = {"vm-1": (1000.0, "metadata write failed")}
    ctrl = _run(backend, FakeGitHub(), make_config(), clock)

    ctrl.tick()
    v = next(s for s in ctrl.snapshot.slots if s.id == "vm-1")
    assert "rebuild failed" in v.error  # fatal overrides the warning


def test_busy_over_timeout_stop_not_destroy(clock):
    runner = make_runner(name="husk-1-c0", busy=True)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(runners=[runner])
    # max_total=1 so the busy slot doesn't trigger a warm-spare create; isolates timeout.
    ctrl = _run(backend, github, make_config(max_job_duration=10, max_total=1), clock)

    ctrl.tick()  # BUSY, age 0
    assert backend.calls == []
    clock.advance(20)
    ctrl.tick()  # BUSY past timeout

    assert "stop" in backend.ops()
    assert "destroy" not in backend.ops()


def test_busy_under_timeout_noop(clock):
    runner = make_runner(name="husk-1-c0", busy=True)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(runners=[runner])
    ctrl = _run(
        backend, github, make_config(max_job_duration=10_000, max_total=1), clock
    )

    ctrl.tick()
    clock.advance(20)
    ctrl.tick()

    assert backend.calls == []  # no stop/destroy/rebuild


def test_idle_over_timeout_deregisters_runner(clock):
    runner = make_runner(id=5, name="husk-1-c0", busy=False)
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(runners=[runner])
    ctrl = _run(backend, github, make_config(idle_timeout=10), clock)

    ctrl.tick()  # IDLE, age 0
    clock.advance(20)
    ctrl.tick()  # IDLE past timeout

    assert ("delete_runner", 5) in github.calls
    assert backend.calls == []  # reaper is GitHub-side; no Nova mutation


def test_starting_within_grace_noop(clock):
    # ACTIVE, no runner yet, freshly provisioned → STARTING, leave it alone.
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub()
    ctrl = _run(backend, github, make_config(startup_grace=300), clock)

    ctrl.tick()

    assert backend.calls == []


def test_unhealthy_past_grace_rebuild(clock):
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub()
    ctrl = _run(backend, github, make_config(startup_grace=10), clock)

    ctrl.tick()  # within grace → STARTING
    assert backend.calls == []
    clock.advance(30)
    ctrl.tick()  # no runner past grace → UNHEALTHY → rebuild

    assert "rebuild" in backend.ops()
    assert "destroy" not in backend.ops()


def test_rebuild_no_double_issue(clock):
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    github = FakeGitHub()
    ctrl = _run(backend, github, make_config(), clock)

    ctrl.tick()  # SHUTOFF → rebuild (fake sets task=rebuilding), enters pending_start
    assert backend.ops() == ["rebuild"]

    clock.advance(5)
    ctrl.tick()  # still settling (task set) → no action
    assert backend.ops() == ["rebuild"]

    backend.set_status("vm-1", status="SHUTOFF", task_state=None)  # rebuild settled
    clock.advance(5)
    ctrl.tick()  # pending_start drain → os-start (not a second rebuild)

    assert backend.ops() == ["rebuild", "start"]


def test_only_mutates_listed_slots(clock):
    # Mixed pool driven across several ticks; every mutated id must have been a
    # slot the backend actually listed.
    listed_ids: set[str] = set()
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-err", name="husk-err", status="ERROR"),
            make_slot(id="vm-shut", name="husk-shut", status="SHUTOFF"),
        ]
    )
    github = FakeGitHub()
    ctrl = _run(backend, github, make_config(min_ready=1, max_total=3), clock)

    real_list = backend.list_slots

    def tracking_list():
        out = real_list()
        listed_ids.update(s.id for s in out)
        return out

    backend.list_slots = tracking_list  # type: ignore[method-assign]

    for _ in range(4):
        clock.advance(5)
        ctrl.tick()

    mutated = {c[1] for c in backend.calls if c[0] != "create"}
    assert mutated, "expected some mutations in this scenario"
    assert mutated <= listed_ids
