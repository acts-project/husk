"""vm_prefix drives created VM/runner names — the cross-pool isolation invariant
(GitHub runner APIs are repo-wide, so pools must not mint colliding names)."""

from __future__ import annotations

import dataclasses

from conftest import TEST_TARGET, FakeClock, make_config, tick
from husk.controller import Controller
from husk.fake_backend import FakeBackend, FakeGitHub


def _config_with_prefix(prefix: str):
    cfg = make_config(min_ready=1, max_total=1)
    return dataclasses.replace(
        cfg, backend=dataclasses.replace(cfg.backend, vm_prefix=prefix)
    )


def test_default_prefix_is_husk():
    backend, github = FakeBackend(), FakeGitHub()
    ctrl = Controller(
        backend, github, make_config(min_ready=1), clock=FakeClock(), target=TEST_TARGET
    )
    tick(ctrl)
    created = [c[1] for c in backend.calls if c[0] == "create"]
    assert created and all(n.startswith("husk-") for n in created)


def test_pool_prefix_partitions_names():
    backend, github = FakeBackend(), FakeGitHub()
    ctrl = Controller(
        backend,
        github,
        _config_with_prefix("husk-gpu"),
        clock=FakeClock(),
        target=TEST_TARGET,
    )
    tick(ctrl)
    created = [c[1] for c in backend.calls if c[0] == "create"]
    minted = [c[1] for c in github.calls if c[0] == "mint"]
    assert created and all(n.startswith("husk-gpu-") for n in created)
    # The runner name is f"{vm}-c{cycle}", so it inherits the pool prefix too.
    assert minted and all(n.startswith("husk-gpu-") for n in minted)
