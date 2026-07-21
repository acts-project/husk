"""What the reconcile loop and the poller actually record.

These are the numbers no snapshot can produce: a rebuild that failed and was
retried, a tick that fail-safed, a poll that GitHub refused. All of them leave the
observable state either unchanged or looking healthy, which is exactly why they
have to be counted as they happen."""

from __future__ import annotations

import asyncio
import time

import dataclasses

from conftest import TEST_TARGET, make_config, make_runner, make_slot, tick
from husk.controller import Controller
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.metrics import Metrics
from husk.poller import RunnerPoller, SnapshotRegistry


def _ctrl(backend, github, clock, *, config=None, metrics=None) -> Controller:
    return Controller(
        backend,
        github,
        config or make_config(),
        clock=clock,
        target=TEST_TARGET,
        registry=SnapshotRegistry(),
        metrics=metrics,
    )


# --------------------------------------------------------------- fail-safes
def test_a_listing_failure_is_counted_with_its_reason(clock):
    """The most important alert in the set: huskd is up, scraping fine, and has
    silently stopped acting on reality. Nothing else in the exposition changes —
    the last good snapshot keeps being served."""
    m = Metrics()
    backend = FakeBackend(slots=[make_slot(status="SHUTOFF")])
    backend.raise_on_list = True
    ctrl = _ctrl(backend, FakeGitHub(), clock, metrics=m)

    tick(ctrl)

    assert m.reconcile_aborts.value("fake", "list_slots") == 1.0
    assert m.reconcile_ticks.value("fake") == 0.0  # the tick did not complete


def test_a_missing_runner_snapshot_is_a_distinct_abort_reason(clock):
    """ "GitHub has never answered" and "the backend won't list" have completely
    different fixes, so they must not collapse into one counter."""
    m = Metrics()
    github = FakeGitHub()
    github.raise_on_list = True
    ctrl = _ctrl(
        FakeBackend(slots=[make_slot(status="SHUTOFF")]), github, clock, metrics=m
    )

    tick(ctrl)

    assert m.reconcile_aborts.value("fake", "no_runner_snapshot") == 1.0
    assert m.reconcile_aborts.value("fake", "list_slots") == 0.0


def test_a_stale_runner_snapshot_is_counted(clock):
    m = Metrics()
    ctrl = _ctrl(
        FakeBackend(slots=[make_slot(status="SHUTOFF")]), FakeGitHub(), clock, metrics=m
    )
    ctrl.registry.publish_runners(ctrl.target, [], epoch=time.time() - 10_000)

    asyncio.run(ctrl.tick())

    assert m.reconcile_aborts.value("fake", "stale_runner_snapshot") == 1.0


def test_a_completed_tick_is_counted_and_timed(clock):
    m = Metrics()
    ctrl = _ctrl(FakeBackend(slots=[make_slot()]), FakeGitHub(), clock, metrics=m)

    tick(ctrl)

    assert m.reconcile_ticks.value("fake") == 1.0
    assert m.reconcile_duration.count("fake") == 1.0


def test_an_aborted_tick_is_still_timed(clock):
    """Duration is observed in a `finally`, so a tick that fail-safes early still
    contributes — otherwise the histogram would quietly only describe healthy
    ticks and hide a backend that takes 30s to refuse a listing."""
    m = Metrics()
    backend = FakeBackend(slots=[make_slot()])
    backend.raise_on_list = True
    ctrl = _ctrl(backend, FakeGitHub(), clock, metrics=m)

    tick(ctrl)

    assert m.reconcile_duration.count("fake") == 1.0


# ------------------------------------------------------------ action failures
def test_a_failed_rebuild_is_counted_by_action(clock):
    m = Metrics()
    backend = FakeBackend(slots=[make_slot(status="SHUTOFF")])
    backend.raise_on_rebuild = True
    ctrl = _ctrl(backend, FakeGitHub(), clock, metrics=m)

    tick(ctrl)

    assert m.action_failures.value("fake", "rebuild") == 1.0
    assert m.slot_recycles.value("fake") == 0.0


def test_a_successful_rebuild_counts_a_recycle_and_no_failure(clock):
    m = Metrics()
    ctrl = _ctrl(
        FakeBackend(slots=[make_slot(status="SHUTOFF")]), FakeGitHub(), clock, metrics=m
    )

    tick(ctrl)

    assert m.slot_recycles.value("fake") == 1.0
    assert m.action_failures.value("fake", "rebuild") == 0.0


def test_action_labels_never_carry_a_slot_id(clock):
    """`_safe` descriptions embed the slot id for the log line ("destroy vm-1").
    Reaching a label, that would mint a new series per slot per action and leave
    it behind forever."""
    from husk.controller import _action

    assert _action("destroy vm-abc123") == "destroy"
    assert _action("mark_active vm-abc123") == "mark_active"
    assert _action("delete_runner") == "delete_runner"


def test_a_destroy_is_counted_with_its_reason(clock):
    """Slots retired for surplus and slots destroyed because they broke are very
    different signals, and `reason` is a fixed vocabulary so it is safe as a
    label."""
    m = Metrics()
    backend = FakeBackend(slots=[make_slot(status="ERROR")])
    ctrl = _ctrl(backend, FakeGitHub(), clock, metrics=m)

    tick(ctrl)

    assert m.slots_destroyed.value("fake", "error") == 1.0


def test_a_created_slot_is_counted(clock):
    m = Metrics()
    ctrl = _ctrl(FakeBackend(slots=[]), FakeGitHub(), clock, metrics=m)

    tick(ctrl)

    assert m.slots_created.value("fake") == 1.0


# ----------------------------------------------------------- bring-up timings
def test_a_completed_bringup_observes_the_recycle_histogram(clock):
    """The distribution the per-slot "last value" gauges cannot give you. Observed
    when the runner comes online, because a scrape-time renderer has no idea how
    many bring-ups happened since the last scrape."""
    m = Metrics()
    backend = FakeBackend(slots=[make_slot(id="vm-1", status="SHUTOFF")])
    github = FakeGitHub()
    ctrl = _ctrl(backend, github, clock, metrics=m)

    tick(ctrl)  # rebuild issued -> timing.issued_at set
    backend.set_status("vm-1", status="ACTIVE")
    clock.advance(20)
    tick(ctrl)  # observed ACTIVE -> timing.active_at set
    github.runners = [make_runner(name="husk-1-c1", status="online")]
    clock.advance(50)
    tick(ctrl)  # runner online -> bring-up complete

    assert m.recycle_duration.count("fake") == 1.0
    assert m.recycle_duration.sum("fake") == 70.0  # 20s spawn + 50s cloud-init
    assert m.cloudinit_duration.sum("fake") == 50.0


def test_two_recycles_accumulate_into_one_distribution(clock):
    """Which is the whole point: the gauge only ever shows the last one."""
    m = Metrics()
    m.recycle_duration.observe(72.0, "fake")
    m.recycle_duration.observe(95.0, "fake")

    assert m.recycle_duration.count("fake") == 2.0


# -------------------------------------------------------------------- poller
def test_a_failed_poll_is_counted(clock):
    """A failed poll leaves the last good snapshot published — deliberately, so a
    blip doesn't stall reconcile. That makes it invisible everywhere else: this
    counter is the only way to see GitHub degrading before a snapshot ages out
    and ticks start aborting."""
    m = Metrics()
    github = FakeGitHub()
    github.raise_on_list = True
    poller = RunnerPoller(
        SnapshotRegistry(), {TEST_TARGET: github.list_runners}, interval=1, metrics=m
    )

    asyncio.run(poller.poll_once())

    assert m.github_polls.value(TEST_TARGET.key) == 1.0
    assert m.github_poll_failures.value(TEST_TARGET.key) == 1.0


def test_a_successful_poll_counts_an_attempt_but_no_failure():
    m = Metrics()
    poller = RunnerPoller(
        SnapshotRegistry(),
        {TEST_TARGET: FakeGitHub().list_runners},
        interval=1,
        metrics=m,
    )

    asyncio.run(poller.poll_once())

    assert m.github_polls.value(TEST_TARGET.key) == 1.0
    assert m.github_poll_failures.value(TEST_TARGET.key) == 0.0


# ------------------------------------------------------------------- sharing
def test_pools_keep_separate_series_on_a_shared_instrument_set(clock):
    """huskd builds one Metrics and hands it to every Controller; the `backend`
    label is what keeps the pools separable."""
    m = Metrics()
    base = make_config()
    other = dataclasses.replace(
        base, backend=dataclasses.replace(base.backend, name="other")
    )
    a = _ctrl(FakeBackend(slots=[]), FakeGitHub(), clock, metrics=m)
    b = _ctrl(FakeBackend(slots=[]), FakeGitHub(), clock, config=other, metrics=m)

    tick(a)
    tick(b)

    assert m.slots_created.value("fake") == 1.0
    assert m.slots_created.value("other") == 1.0


def test_a_controller_without_metrics_still_works(clock):
    """Constructing Metrics is cheap and side-effect-free, so Controller defaults
    to a private instance: every instrumented path stays exercised in tests and in
    huskctl, the numbers are just discarded."""
    ctrl = _ctrl(FakeBackend(slots=[]), FakeGitHub(), clock)

    tick(ctrl)

    assert ctrl.metrics.slots_created.value("fake") == 1.0
