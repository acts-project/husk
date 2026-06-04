"""Table-driven tests for the pure classifier and runner matching."""

from __future__ import annotations

import pytest

from conftest import make_runner, make_slot
from husk.slot import Runner, SlotState, classify, match_runner

GRACE = 300.0


@pytest.mark.parametrize(
    "status, task, runner, prov_age, expected",
    [
        # 1. ERROR wins over everything.
        ("ERROR", None, None, 0, SlotState.ERROR),
        ("ERROR", "rebuilding", make_runner(), 0, SlotState.ERROR),
        # 2. In-flight provisioning → STARTING (REBUILD status or any task_state).
        ("REBUILD", None, None, 0, SlotState.STARTING),
        ("ACTIVE", "rebuilding", None, 9999, SlotState.STARTING),
        ("ACTIVE", "spawning", make_runner(), 9999, SlotState.STARTING),
        # Precedence: SHUTOFF *with* a task set is still STARTING (row 2 before 3).
        ("SHUTOFF", "spawning", None, 0, SlotState.STARTING),
        # 3. SHUTOFF + task clear → NEEDS_RECYCLE.
        ("SHUTOFF", None, None, 0, SlotState.NEEDS_RECYCLE),
        # 4. Fresh create.
        ("BUILD", None, None, 0, SlotState.STARTING),
        # 5/6. ACTIVE with a live runner.
        ("ACTIVE", None, make_runner(busy=True), 9999, SlotState.BUSY),
        ("ACTIVE", None, make_runner(busy=False), 9999, SlotState.IDLE),
        # 7. ACTIVE, no live runner, within grace (and unknown age) → STARTING.
        ("ACTIVE", None, None, 10, SlotState.STARTING),
        ("ACTIVE", None, None, None, SlotState.STARTING),
        ("ACTIVE", None, make_runner(status="offline"), 10, SlotState.STARTING),
        # 8. ACTIVE, no live runner, past grace → UNHEALTHY.
        ("ACTIVE", None, None, 400, SlotState.UNHEALTHY),
        ("ACTIVE", None, make_runner(status="offline"), 400, SlotState.UNHEALTHY),
    ],
)
def test_classify(status, task, runner, prov_age, expected):
    slot = make_slot(status=status, task_state=task)
    assert classify(slot, runner, provision_age=prov_age, startup_grace=GRACE) is expected


def test_match_runner_prefix_and_prefers_online():
    slot = make_slot(name="husk-7")
    runners = [
        Runner(id=1, name="husk-7-c0", status="offline", busy=False),
        Runner(id=2, name="husk-7-c1", status="online", busy=False),
        Runner(id=3, name="other-c0", status="online", busy=False),
    ]
    m = match_runner(runners, slot)
    assert m is not None and m.id == 2  # online match, ignores the unrelated runner


def test_match_runner_highest_cycle_when_all_offline():
    slot = make_slot(name="husk-7")
    runners = [
        Runner(id=1, name="husk-7-c0", status="offline", busy=False),
        Runner(id=2, name="husk-7-c3", status="offline", busy=False),
    ]
    m = match_runner(runners, slot)
    assert m is not None and m.id == 2


def test_match_runner_none_when_no_prefix():
    assert match_runner([make_runner(name="nope-c0")], make_slot(name="husk-7")) is None
