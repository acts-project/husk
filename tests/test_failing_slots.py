"""A slot whose actions keep failing must be visible as such — for how long.

From an incident: a slot whose Nova instance no longer existed 404'd on every
rebuild for a weekend. The dashboard showed the error, but `slot_errors` is
overwritten each tick, so its timestamp always read "just now" and a multi-hour
outage was indistinguishable from a blip. Prometheus saw nothing at all: the
state metric said `needs_recycle`, exactly like a healthy slot mid-recycle.

The failure streak — when it started, how many times — is what separates the
two, and it is deliberately published as an elapsed DURATION rather than a
`broken` boolean, so the "how much patience does this deserve" threshold lives
in the query instead of in huskd's config."""

from __future__ import annotations

from conftest import (
    make_config,
    make_controller,
    make_slot,
    render_metrics,
    tick,
)
from husk.backend import SlotActionError
from husk.fake_backend import FakeBackend, FakeGitHub


def _ctrl(clock, *, fails: bool):
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    if fails:

        def boom(*a, **kw):
            raise SlotActionError("rebuild rejected: HTTP 404: could not be found")

        backend.rebuild_slot = boom
    ctrl = make_controller(
        backend, FakeGitHub(), make_config(min_ready=1, max_total=1), clock
    )
    return backend, ctrl


def _view(ctrl, slot_id="vm-1"):
    return next(v for v in ctrl.snapshot.slots if v.id == slot_id)


def test_the_streak_measures_the_run_not_the_last_failure(clock):
    _, ctrl = _ctrl(clock, fails=True)

    tick(ctrl)
    assert _view(ctrl).failure_count == 1
    assert _view(ctrl).failing_seconds == 0.0

    for _ in range(3):
        clock.advance(600)
        tick(ctrl)

    v = _view(ctrl)
    assert v.failure_count == 4
    assert v.failing_seconds == 1800.0  # since the FIRST failure, not the last


def test_a_success_ends_the_streak(clock):
    backend, ctrl = _ctrl(clock, fails=True)

    clock.advance(600)
    tick(ctrl)
    assert _view(ctrl).failing_seconds is not None

    del backend.rebuild_slot  # backend recovers → next rebuild succeeds
    clock.advance(600)
    tick(ctrl)

    v = _view(ctrl)
    assert v.failing_seconds is None and v.failure_count == 0


def test_a_healthy_slot_reports_no_failing_series(clock):
    _, ctrl = _ctrl(clock, fails=False)
    tick(ctrl)

    text = render_metrics([ctrl.snapshot])
    # Absent, not zero: recovery is then handled by Prometheus staleness, and a
    # `> 900` alert can never be tripped by a slot that is simply healthy.
    assert "husk_slot_failing_seconds{" not in text
    assert 'failing="false"' in text


def test_a_failing_slot_is_visible_in_the_exposition(clock):
    _, ctrl = _ctrl(clock, fails=True)
    tick(ctrl)
    clock.advance(3600)
    tick(ctrl)

    text = render_metrics([ctrl.snapshot])
    assert 'husk_slot_failing_seconds{backend="fake",slot="husk-1"} 3600.0' in text
    assert 'husk_slot_failure_streak{backend="fake",slot="husk-1"} 2.0' in text
    # ...and annotated on the info metric the state panels already join against.
    assert 'failing="true"' in text


def test_the_state_metric_still_says_what_the_slot_was_doing(clock):
    """The whole point of keeping brokenness off SlotState: a broken slot is still
    a needs_recycle slot, and the state metric must not start lying about that."""
    _, ctrl = _ctrl(clock, fails=True)
    tick(ctrl)
    clock.advance(600)
    tick(ctrl)

    assert _view(ctrl).state == "needs_recycle"
    text = render_metrics([ctrl.snapshot])
    assert 'state="needs_recycle"' in text
    assert 'state="broken"' not in text


def test_errors_carry_a_wall_clock_epoch(clock):
    """The dashboard renders this as `Date.now()/1000 - epoch`. The controller's
    own clock is monotonic, so stamping it with that produced an 'ago' of roughly
    the Unix epoch."""
    import time

    _, ctrl = _ctrl(clock, fails=True)
    tick(ctrl)

    epoch = _view(ctrl).error_epoch
    assert epoch is not None
    assert abs(epoch - time.time()) < 60  # wall-clock, not the FakeClock's t=1000
