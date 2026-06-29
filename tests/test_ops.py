"""The in-process async-op registry (husk.ops.OpStore): keyed single-flight,
progress, failure + cooldown restart, and DONE pruning. A synchronous `spawn`
runs the worker inline so a single `submit()` settles deterministically."""

from __future__ import annotations

import pytest

from husk.ops import DONE, FAILED, PENDING, OpAbort, OpStore
from husk.snapshot import ControllerState
from husk.ops import OpView


def _inline(**kw) -> OpStore:
    # Run the worker inline; default to no in-worker retry (giveup at 0) so a
    # failing op fails fast without real backoff sleeps.
    kw.setdefault("retry_giveup_s", 0)
    return OpStore(spawn=lambda fn: fn(), **kw)


def test_submit_runs_and_completes():
    store = _inline()
    view = store.submit("k", "demo", lambda report: 42)
    assert view.state == DONE
    assert store.result("k") == 42


def test_single_flight_runs_once():
    store = _inline()
    calls = []
    fn = lambda report: calls.append(1) or "ok"  # noqa: E731
    store.submit("k", "demo", fn)
    store.submit("k", "demo", fn)  # already DONE → not re-run
    assert calls == [1]


def test_progress_is_recorded():
    store = _inline()

    def fn(report):
        report("halfway")
        return "done"

    store.submit("k", "demo", fn)
    # progress is cleared on DONE; capture it mid-flight via a non-completing fn.
    seen = {}

    def fn2(report):
        report("step 1")
        seen["view"] = store.view("k2")
        return "x"

    store.submit("k2", "demo", fn2)
    assert seen["view"].progress == "step 1"


def test_failure_marks_failed_and_hides_result():
    store = _inline()

    def boom(report):
        raise RuntimeError("nope")

    view = store.submit("k", "demo", boom)
    assert view.state == FAILED
    assert "nope" in view.error
    with pytest.raises(KeyError):
        store.result("k")


def test_failed_op_restarts_only_after_cooldown():
    now = [1000.0]
    store = _inline(retry_after_s=30, monotonic=lambda: now[0])
    calls = []

    def boom(report):
        calls.append(1)
        raise RuntimeError("nope")

    store.submit("k", "demo", boom)
    store.submit("k", "demo", boom)  # within cooldown → not restarted
    assert calls == [1]
    now[0] += 31  # past cooldown
    store.submit("k", "demo", boom)
    assert calls == [1, 1]


def test_opabort_is_not_retried():
    # Even with a generous giveup window, an OpAbort fails immediately (no retry).
    store = OpStore(spawn=lambda fn: fn(), retry_giveup_s=600)
    calls = []

    def fn(report):
        calls.append(1)
        raise OpAbort("permanent")

    view = store.submit("k", "demo", fn)
    assert view.state == FAILED and calls == [1]


def test_views_prune_done_past_ttl():
    clock = [1000.0]
    store = _inline(done_ttl_s=60, clock=lambda: clock[0])
    store.submit("k", "demo", lambda report: "ok")
    assert [v.key for v in store.views()] == ["k"]
    clock[0] += 61  # DONE older than TTL → pruned from the board
    assert store.views() == []


def test_pending_op_is_visible():
    # A worker that never returns (here: not spawned) leaves the op PENDING/visible.
    store = OpStore(spawn=lambda fn: None)  # don't run the worker
    view = store.submit("k", "demo", lambda report: "x")
    assert view.state == PENDING
    assert [v.key for v in store.views()] == ["k"]


def test_snapshot_round_trips_ops():
    ops = [
        OpView(
            key="glance:ref",
            kind="glance-upload",
            state=PENDING,
            progress="uploading",
            error=None,
            started_at=1.0,
            updated_at=2.0,
            attempts=1,
        )
    ]
    state = ControllerState.from_classified(
        generation=1,
        backend="os",
        min_ready=1,
        max_total=2,
        desired_total=1,
        classified=[],
        ops=ops,
    )
    again = ControllerState.from_dict(state.to_dict())
    assert again.ops == ops
