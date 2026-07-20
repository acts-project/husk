"""The centralized runner poller and its snapshot registry.

The load-bearing behaviour is the failure policy: a failed poll keeps the last
good snapshot (a blip must not stall reconciliation) while the *controller*
refuses one that has gone stale (see test_failsafe)."""

from __future__ import annotations

import asyncio
import time

from conftest import make_runner
from husk.poller import RunnerPoller, SnapshotRegistry
from husk.target import Target

T = Target.repo("acts-project/husk-test")
OTHER = Target.org("acts-project")


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- registry
def test_unpolled_target_is_unknown_not_empty():
    reg = SnapshotRegistry()
    assert reg.runners(T) is None  # "unknown", never "no runners"
    assert reg.age(T) is None


def test_publish_then_read():
    reg = SnapshotRegistry()
    reg.publish_runners(T, [make_runner(id=1, name="r1")])
    assert [r.id for r in reg.runners(T)] == [1]
    assert reg.age(T) is not None and reg.age(T) < 5


def test_targets_are_isolated():
    reg = SnapshotRegistry()
    reg.publish_runners(T, [make_runner(id=1)])
    assert reg.runners(OTHER) is None


def test_reader_cannot_mutate_stored_snapshot():
    reg = SnapshotRegistry()
    reg.publish_runners(T, [make_runner(id=1)])
    reg.runners(T).clear()  # a caller mangling its copy must not affect the store
    assert len(reg.runners(T)) == 1


def test_age_uses_supplied_epoch():
    reg = SnapshotRegistry()
    reg.publish_runners(T, [], epoch=time.time() - 100)
    assert 99 <= reg.age(T) <= 105


# --------------------------------------------------------------------- poller
def test_poll_once_publishes_every_target():
    reg = SnapshotRegistry()

    async def a():
        return [make_runner(id=1)]

    async def b():
        return [make_runner(id=2), make_runner(id=3)]

    _run(RunnerPoller(reg, {T: a, OTHER: b}, interval=1).poll_once())

    assert [r.id for r in reg.runners(T)] == [1]
    assert [r.id for r in reg.runners(OTHER)] == [2, 3]


def test_failed_poll_keeps_last_good_snapshot():
    reg = SnapshotRegistry()
    state = {"fail": False}

    async def lister():
        if state["fail"]:
            raise RuntimeError("GitHub 502")
        return [make_runner(id=1)]

    poller = RunnerPoller(reg, {T: lister}, interval=1)
    _run(poller.poll_once())
    state["fail"] = True
    _run(poller.poll_once())  # must not raise, must not clear

    assert [r.id for r in reg.runners(T)] == [1]


def test_one_failing_target_does_not_stop_the_others():
    reg = SnapshotRegistry()

    async def bad():
        raise RuntimeError("boom")

    async def good():
        return [make_runner(id=9)]

    _run(RunnerPoller(reg, {T: bad, OTHER: good}, interval=1).poll_once())

    assert reg.runners(T) is None
    assert [r.id for r in reg.runners(OTHER)] == [9]


def test_run_polls_repeatedly_until_stopped():
    reg = SnapshotRegistry()
    polls = {"n": 0}

    async def lister():
        polls["n"] += 1
        return [make_runner(id=polls["n"])]

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(
            RunnerPoller(reg, {T: lister}, interval=0.01).run(stop)
        )
        deadline = time.monotonic() + 3
        while polls["n"] < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=5)  # stop must end the loop promptly

    _run(go())
    assert polls["n"] >= 3
