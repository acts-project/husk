"""Per-tick config hot reload: safe knobs swap live; structural changes warn."""

from __future__ import annotations

import dataclasses

import pytest
from conftest import make_config, make_controller, tick
from husk.fake_backend import FakeBackend, FakeGitHub


def _ctrl(clock, **cfg):
    return make_controller(
        FakeBackend(slots=[]), FakeGitHub(), make_config(**cfg), clock
    )


def test_hot_knobs_apply_live(clock):
    ctrl = _ctrl(clock, min_ready=1, max_total=2, shrink_ticks=3)
    new = make_config(min_ready=3, max_total=5, shrink_ticks=7)
    new = dataclasses.replace(
        new,
        timeouts=dataclasses.replace(
            new.timeouts, poll_interval_sec=10, idle_timeout_sec=99
        ),
    )

    ctrl.apply_reloaded_config(new)

    assert ctrl.cfg.backend.min_ready == 3
    assert ctrl.cfg.backend.max_total == 5
    assert ctrl.cfg.controller.shrink_ticks == 7
    assert ctrl.cfg.timeouts.poll_interval_sec == 10
    assert ctrl.cfg.timeouts.idle_timeout_sec == 99


def test_hot_knobs_take_effect_on_next_tick(clock):
    # min_ready drives desired growth — bumping it live must create more slots.
    backend = FakeBackend(slots=[])
    ctrl = make_controller(
        backend, FakeGitHub(), make_config(min_ready=1, max_total=5), clock
    )

    tick(ctrl)
    assert backend.ops().count("create") == 1

    ctrl.apply_reloaded_config(make_config(min_ready=3, max_total=5))
    clock.advance(5)
    tick(ctrl)
    # pool was 1, desired now 3 → two more creates this tick
    assert backend.ops().count("create") == 3


def test_structural_change_is_ignored_and_warns(clock, caplog):
    ctrl = _ctrl(clock)
    new = dataclasses.replace(
        ctrl.cfg,
        backend=dataclasses.replace(ctrl.cfg.backend, image_name="OTHER-IMAGE"),
    )

    with caplog.at_level("WARNING", logger="husk.controller"):
        ctrl.apply_reloaded_config(new)

    assert ctrl.cfg.backend.image_name != "OTHER-IMAGE"  # not applied
    assert any("structural changes ignored" in r.message for r in caplog.records)


def test_no_changes_is_a_noop(clock, caplog):
    ctrl = _ctrl(clock)
    same = make_config()  # value-identical to the controller's config
    with caplog.at_level("INFO", logger="husk.controller"):
        ctrl.apply_reloaded_config(same)
    assert not any("config reload applied" in r.message for r in caplog.records)
    assert not any("structural changes ignored" in r.message for r in caplog.records)


def test_maybe_reload_applies_when_loader_yields(clock):
    ctrl = _ctrl(clock, min_ready=1)
    ctrl._reload_config = lambda: make_config(min_ready=4, max_total=9)
    ctrl._maybe_reload()
    assert ctrl.cfg.backend.min_ready == 4


def test_maybe_reload_noop_when_loader_returns_none(clock):
    ctrl = _ctrl(clock, min_ready=1)
    ctrl._reload_config = lambda: None
    ctrl._maybe_reload()
    assert ctrl.cfg.backend.min_ready == 1


def test_maybe_reload_survives_loader_exception(clock):
    ctrl = _ctrl(clock, min_ready=1)

    def boom():
        raise RuntimeError("bad config")

    ctrl._reload_config = boom
    ctrl._maybe_reload()  # must not raise
    assert ctrl.cfg.backend.min_ready == 1


def test_no_reloader_is_inert(clock):
    ctrl = _ctrl(clock, min_ready=1)
    assert ctrl._reload_config is None
    ctrl._maybe_reload()  # no-op, no error
    assert ctrl.cfg.backend.min_ready == 1


@pytest.mark.parametrize("field", ["min_ready", "max_total"])
def test_each_backend_knob_hot(clock, field):
    ctrl = _ctrl(clock)
    new = dataclasses.replace(
        ctrl.cfg, backend=dataclasses.replace(ctrl.cfg.backend, **{field: 42})
    )
    ctrl.apply_reloaded_config(new)
    assert getattr(ctrl.cfg.backend, field) == 42
