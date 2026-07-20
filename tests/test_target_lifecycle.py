"""Target lifecycle: spawn on discovery, drain on removal.

The dangerous direction is removal — a bug here destroys live VMs and kills
running jobs. So most of this file is about what must NOT happen: no teardown on
a failed sweep, none on a partial one, and never on a busy slot."""

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


def _controller(target, *, slots=None, runners=None, name="gpu"):
    cfg = make_config(min_ready=0, max_total=2)
    cfg = dataclasses.replace(cfg, backend=dataclasses.replace(cfg.backend, name=name))
    gh = FakeGitHub(runners=runners or [])
    return Controller(
        FakeBackend(slots=slots),
        gh,
        cfg,
        target=target,
        registry=SnapshotRegistry(),
    )


class Discoverer:
    """A scriptable discovery source: each call returns the next scripted sweep."""

    def __init__(self, *sweeps) -> None:
        self._sweeps = list(sweeps)
        self.calls = 0

    async def __call__(self) -> Discovery:
        self.calls += 1
        sweep = self._sweeps[min(self.calls - 1, len(self._sweeps) - 1)]
        if isinstance(sweep, Exception):
            raise sweep
        return sweep


def _facade(discoverer, built, **kw):
    """A facade whose `build` hands back pre-made controllers per target."""
    attached, detached = [], []
    facade = MultiPoolController(
        [],
        discover=discoverer,
        build=lambda t: list(built.get(t.key, [])),
        attach=attached.append,
        detach=detached.append,
        **kw,
    )
    return facade, attached, detached


def _sweep(*targets, complete=True):
    return Discovery(targets=tuple(targets), complete=complete)


# ----------------------------------------------------------------- spawn side
def test_discovered_target_creates_its_pools():
    ctrl = _controller(ORG)
    facade, attached, _ = _facade(Discoverer(_sweep(ORG)), {ORG.key: [ctrl]})
    _run(facade.discover_once())
    assert facade.controllers == [ctrl]
    # The poller has to learn about it too, or its snapshot is never refreshed.
    assert attached == [ctrl]


def test_a_target_is_not_rebuilt_on_every_sweep():
    ctrl = _controller(ORG)
    d = Discoverer(_sweep(ORG))
    facade, _, _ = _facade(d, {ORG.key: [ctrl]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == [ctrl]  # still exactly one unit, not two


def test_a_target_appearing_later_is_added():
    org, repo = _controller(ORG), _controller(REPO)
    d = Discoverer(_sweep(ORG), _sweep(ORG, REPO))
    facade, _, _ = _facade(d, {ORG.key: [org], REPO.key: [repo]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert {c.target for c in facade.controllers} == {ORG, REPO}


def test_a_target_that_cannot_be_built_is_retried_not_dropped():
    """A pool whose backend is unreachable at first must not be lost forever."""
    ctrl = _controller(ORG)
    calls = {"n": 0}

    def build(t):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("cloud unreachable")
        return [ctrl]

    facade = MultiPoolController([], discover=Discoverer(_sweep(ORG)), build=build)
    _run(facade.discover_once())
    assert facade.controllers == []
    _run(facade.discover_once())
    assert facade.controllers == [ctrl]


# ------------------------------------------------------------------ drain side
def test_removed_target_stops_reconciling_and_destroys_idle_slots():
    slot = make_slot(id="s-1", name="husk-1")
    ctrl = _controller(ORG, slots=[slot])
    d = Discoverer(_sweep(ORG), _sweep())
    facade, _, detached = _facade(d, {ORG.key: [ctrl]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == []
    assert detached == [ORG]
    assert "destroy" in ctrl.backend.ops()


def test_drain_leaves_a_busy_slot_alone_and_retries():
    """Losing a target must not kill someone's in-flight job."""
    slot = make_slot(id="s-1", name="husk-1")
    busy = make_runner(id=1, name="husk-1-c0", status="online", busy=True)
    ctrl = _controller(ORG, slots=[slot], runners=[busy])
    d = Discoverer(_sweep(ORG), _sweep())
    facade, _, _ = _facade(d, {ORG.key: [ctrl]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert "destroy" not in ctrl.backend.ops()
    assert ORG.key in facade._draining  # still pending, retried next sweep

    # Job finishes → the runner goes idle → the next sweep completes the drain.
    ctrl.github.runners = []
    _run(facade.discover_once())
    assert "destroy" in ctrl.backend.ops()
    assert ORG.key not in facade._draining


def test_failed_discovery_never_removes_a_target():
    """A GitHub 500 must not be read as "every install vanished"."""
    slot = make_slot(id="s-1", name="husk-1")
    ctrl = _controller(ORG, slots=[slot])
    d = Discoverer(_sweep(ORG), RuntimeError("github is down"))
    facade, _, _ = _facade(d, {ORG.key: [ctrl]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == [ctrl]
    assert "destroy" not in ctrl.backend.ops()


def test_partial_sweep_never_removes_a_target():
    """Absence from an incomplete sweep is not evidence of removal."""
    slot = make_slot(id="s-1", name="husk-1")
    ctrl = _controller(REPO, slots=[slot])
    d = Discoverer(_sweep(REPO), _sweep(complete=False))
    facade, _, _ = _facade(d, {REPO.key: [ctrl]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == [ctrl]
    assert "destroy" not in ctrl.backend.ops()


def test_partial_sweep_may_still_add():
    org, repo = _controller(ORG), _controller(REPO)
    d = Discoverer(_sweep(ORG), _sweep(ORG, REPO, complete=False))
    facade, _, _ = _facade(d, {ORG.key: [org], REPO.key: [repo]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert {c.target for c in facade.controllers} == {ORG, REPO}


def test_target_returning_mid_drain_is_revived_not_rebuilt():
    """Its slots are still there, so re-adopting beats destroy-and-rebuild."""
    slot = make_slot(id="s-1", name="husk-1")
    busy = make_runner(id=1, name="husk-1-c0", status="online", busy=True)
    ctrl = _controller(ORG, slots=[slot], runners=[busy])
    d = Discoverer(_sweep(ORG), _sweep(), _sweep(ORG))
    facade, _, _ = _facade(d, {ORG.key: [ctrl]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert facade.controllers == []
    _run(facade.discover_once())
    assert facade.controllers == [ctrl]  # the SAME controller, slot intact
    assert facade._draining == {}
    assert "destroy" not in ctrl.backend.ops()


def test_drain_deregisters_the_runner_before_destroying_the_slot():
    slot = make_slot(id="s-1", name="husk-1")
    idle = make_runner(id=7, name="husk-1-c0", status="online", busy=False)
    ctrl = _controller(ORG, slots=[slot], runners=[idle])
    d = Discoverer(_sweep(ORG), _sweep())
    facade, _, _ = _facade(d, {ORG.key: [ctrl]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert ("delete_runner", 7) in [(c[0], c[1]) for c in ctrl.github.calls]
    assert "destroy" in ctrl.backend.ops()


def test_a_backend_that_cannot_list_is_retried_rather_than_assumed_empty():
    """ "Can't tell" must never be read as "nothing to clean up"."""
    ctrl = _controller(ORG)
    ctrl.backend.raise_on_list = True
    d = Discoverer(_sweep(ORG), _sweep())
    facade, _, _ = _facade(d, {ORG.key: [ctrl]})
    _run(facade.discover_once())
    _run(facade.discover_once())
    assert ORG.key in facade._draining  # held open, not silently completed
