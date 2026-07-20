"""Pool lifecycle: reconcile while the target is servable, drain when it isn't.

Each pool names one target. This covers what happens as that target's
availability changes. The dangerous direction is losing it — a bug there
destroys live VMs and kills running jobs — so most of this file is about what
must NOT happen: no teardown on a failed check, none on a partial one, and never
on a busy slot."""

from __future__ import annotations

import asyncio
import dataclasses

from conftest import make_config, make_runner, make_slot
from husk.controller import Controller
from husk.discovery import Discovery
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.multipool import MultiPoolController
from husk.poller import SnapshotRegistry
from husk.target import Target

ORG = Target.org("acts-project")
REPO = Target.repo("paulgessinger/husk-test")


def _run(coro):
    return asyncio.run(coro)


def _pool(target, *, name="gpu", slots=None, runners=None):
    cfg = make_config(min_ready=0, max_total=2)
    cfg = dataclasses.replace(
        cfg, target=target, backend=dataclasses.replace(cfg.backend, name=name)
    )
    return Controller(
        FakeBackend(slots=slots),
        FakeGitHub(runners=runners or []),
        cfg,
        target=target,
        registry=SnapshotRegistry(),
    )


class Checker:
    """A scriptable availability source: each call returns the next scripted sweep."""

    def __init__(self, *sweeps) -> None:
        self._sweeps = list(sweeps)
        self.calls = 0

    async def __call__(self) -> Discovery:
        self.calls += 1
        sweep = self._sweeps[min(self.calls - 1, len(self._sweeps) - 1)]
        if isinstance(sweep, Exception):
            raise sweep
        return sweep


def _facade(checker, pools, **kw):
    attached, detached = [], []
    facade = MultiPoolController(
        list(pools),
        discover=checker,
        attach=attached.append,
        detach=detached.append,
        **kw,
    )
    return facade, attached, detached


def _sweep(*targets, complete=True):
    return Discovery(targets=tuple(targets), complete=complete)


# ------------------------------------------------------------------ enabling
def test_a_pool_starts_when_its_target_is_available():
    p = _pool(ORG)
    facade, attached, _ = _facade(Checker(_sweep(ORG)), [p])
    assert facade.controllers == []  # nothing runs before the first check
    _run(facade.discover_once())
    assert facade.controllers == [p]
    # The poller has to learn about it too, or its snapshot is never refreshed.
    assert attached == [p]


def test_a_pool_whose_target_is_unavailable_never_starts():
    p = _pool(ORG)
    facade, attached, _ = _facade(Checker(_sweep()), [p])
    _run(facade.discover_once())
    assert facade.controllers == []
    assert attached == []


def test_a_pool_is_not_re_enabled_on_every_sweep():
    p = _pool(ORG)
    facade, attached, _ = _facade(Checker(_sweep(ORG)), [p])
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == [p]
    assert attached == [p]


def test_only_the_pools_whose_target_is_available_run():
    org, repo = _pool(ORG, name="gpu"), _pool(REPO, name="cpu")
    facade, _, _ = _facade(Checker(_sweep(ORG)), [org, repo])
    _run(facade.discover_once())
    assert facade.controllers == [org]


def test_a_target_becoming_available_later_starts_its_pool():
    org, repo = _pool(ORG, name="gpu"), _pool(REPO, name="cpu")
    facade, _, _ = _facade(Checker(_sweep(ORG), _sweep(ORG, REPO)), [org, repo])
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert {c.target for c in facade.controllers} == {ORG, REPO}


def test_two_pools_on_one_target_share_a_single_poller_registration():
    """The runner API is target-wide, so the listing is per target, not per pool."""
    a, b = _pool(ORG, name="gpu"), _pool(ORG, name="cpu")
    facade, _, detached = _facade(Checker(_sweep(ORG), _sweep()), [a, b])
    _run(facade.discover_once())
    assert len(facade.controllers) == 2
    _run(facade.discover_once())
    # Detach exactly once, and only after the LAST pool on that target is gone.
    assert detached == [ORG]


# ------------------------------------------------------------------ draining
def test_losing_a_target_stops_reconciling_and_destroys_idle_slots():
    p = _pool(ORG, slots=[make_slot(id="s-1", name="husk-1")])
    facade, _, detached = _facade(Checker(_sweep(ORG), _sweep()), [p])
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == []
    assert detached == [ORG]
    assert "destroy" in p.backend.ops()


def test_drain_leaves_a_busy_slot_alone_and_retries():
    """Losing a target must not kill someone's in-flight job."""
    busy = make_runner(id=1, name="husk-1-c0", status="online", busy=True)
    p = _pool(ORG, slots=[make_slot(id="s-1", name="husk-1")], runners=[busy])
    facade, _, _ = _facade(Checker(_sweep(ORG), _sweep()), [p])
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert "destroy" not in p.backend.ops()
    assert facade._draining  # still pending, retried next sweep

    p.github.runners = []  # job finishes → runner goes idle
    _run(facade.discover_once())
    assert "destroy" in p.backend.ops()
    assert facade._draining == {}


def test_drain_deregisters_the_runner_before_destroying_the_slot():
    idle = make_runner(id=7, name="husk-1-c0", status="online", busy=False)
    p = _pool(ORG, slots=[make_slot(id="s-1", name="husk-1")], runners=[idle])
    facade, _, _ = _facade(Checker(_sweep(ORG), _sweep()), [p])
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert ("delete_runner", 7) in [(c[0], c[1]) for c in p.github.calls]
    assert "destroy" in p.backend.ops()


def test_a_backend_that_cannot_list_is_retried_rather_than_assumed_empty():
    """ "Can't tell" must never be read as "nothing to clean up"."""
    p = _pool(ORG)
    p.backend.raise_on_list = True
    facade, _, _ = _facade(Checker(_sweep(ORG), _sweep()), [p])
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade._draining  # held open, not silently completed


def test_a_target_returning_mid_drain_revives_the_same_pool():
    """Its slots are still there, so re-adopting beats destroy-and-rebuild."""
    busy = make_runner(id=1, name="husk-1-c0", status="online", busy=True)
    p = _pool(ORG, slots=[make_slot(id="s-1", name="husk-1")], runners=[busy])
    facade, _, _ = _facade(Checker(_sweep(ORG), _sweep(), _sweep(ORG)), [p])
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == []
    _run(facade.discover_once())
    assert facade.controllers == [p]  # the SAME controller, slot intact
    assert facade._draining == {}
    assert "destroy" not in p.backend.ops()


# --------------------------------------------------------------- not-removal
def test_a_failed_availability_check_never_drains():
    """A GitHub 500 must not be read as "the App was uninstalled"."""
    p = _pool(ORG, slots=[make_slot(id="s-1", name="husk-1")])
    facade, _, _ = _facade(Checker(_sweep(ORG), RuntimeError("github is down")), [p])
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == [p]
    assert "destroy" not in p.backend.ops()


def test_a_partial_sweep_never_drains():
    """Absence from an incomplete sweep is not evidence of an uninstall."""
    p = _pool(REPO, slots=[make_slot(id="s-1", name="husk-1")])
    facade, _, _ = _facade(Checker(_sweep(REPO), _sweep(complete=False)), [p])
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == [p]
    assert "destroy" not in p.backend.ops()


def test_a_partial_sweep_may_still_enable():
    org, repo = _pool(ORG, name="gpu"), _pool(REPO, name="cpu")
    facade, _, _ = _facade(
        Checker(_sweep(ORG), _sweep(ORG, REPO, complete=False)), [org, repo]
    )
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert {c.target for c in facade.controllers} == {ORG, REPO}
