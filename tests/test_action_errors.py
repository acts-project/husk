"""A rejected slot action reports the cloud's complaint, not a stack trace.

From an incident: a slot whose Nova instance no longer existed 404'd on every
rebuild, and because the reconcile loop retries each tick, the log filled with
identical `to_thread` → `run_in_executor` tracebacks — plumbing frames that name
neither the slot, the action, nor the 404. Same bargain as `CreateSlotError`."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from conftest import make_config, make_controller, make_slot, tick
from husk.backend import SlotActionError
from husk.config import BackendConfig
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.openstack_backend import OpenStackBackend
from husk.slot import Slot

GONE = (
    '{"itemNotFound": {"code": 404, "message": '
    '"Instance 283d6866-87c8-434e-947c-dd0e69e2a256 could not be found."}}'
)


def _slot(sid="vm-1"):
    return Slot(
        id=sid,
        name=sid,
        status="ACTIVE",
        task_state=None,
        created_at=0.0,
        flavor_id="f",
        image_id="img",
    )


def _backend(*, status: int, body: str):
    b = OpenStackBackend.__new__(OpenStackBackend)
    b.cfg = BackendConfig(name="os", type="openstack", min_ready=1, max_total=1)
    b._warnings = {}
    b._pool = b.cfg.name
    b.image_id = "img-1"

    def post(path, **kw):
        return SimpleNamespace(status_code=status, text=body)

    b.conn = SimpleNamespace(compute=SimpleNamespace(post=post))
    return b


def test_a_404_rebuild_raises_slot_action_error_carrying_novas_words():
    b = _backend(status=404, body=GONE)
    with pytest.raises(SlotActionError) as e:
        b.rebuild_slot(_slot(), user_data=b"x", cycle=1)
    msg = str(e.value)
    assert "HTTP 404" in msg  # what the cloud said
    assert "could not be found" in msg  # ...in its own words


def test_a_rejected_power_action_raises_slot_action_error():
    b = _backend(status=409, body='{"conflictingRequest": {}}')
    with pytest.raises(SlotActionError) as e:
        b.start_slot(_slot())
    assert "os-start" in str(e.value) and "HTTP 409" in str(e.value)


def test_a_rejected_rebuild_is_logged_without_a_traceback(clock, caplog):
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    backend.rebuild_slot = _raiser(SlotActionError(f"rebuild of vm-1 rejected: {GONE}"))
    ctrl = make_controller(
        backend, FakeGitHub(), make_config(min_ready=1, max_total=1), clock
    )

    with caplog.at_level(logging.ERROR, logger="husk.controller"):
        tick(ctrl)

    rec = next(r for r in caplog.records if "rebuild of slot" in r.message)
    assert rec.exc_info is None  # the point: no stack trace
    assert "could not be found" in rec.getMessage()  # ...but the cause survives
    # Still a first-class failure: recorded for the dashboard, counted for metrics.
    assert "rebuild failed" in ctrl.slot_errors["vm-1"][1]


def test_an_unexpected_rebuild_failure_still_gets_its_traceback(clock, caplog):
    """The narrowing must not swallow the failures where the frames ARE the
    information — a TypeError in our own code, say."""
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    backend.rebuild_slot = _raiser(TypeError("boom"))
    ctrl = make_controller(
        backend, FakeGitHub(), make_config(min_ready=1, max_total=1), clock
    )

    with caplog.at_level(logging.ERROR, logger="husk.controller"):
        tick(ctrl)

    rec = next(r for r in caplog.records if "rebuild of slot" in r.message)
    assert rec.exc_info is not None


def _raiser(exc):
    def boom(*a, **kw):
        raise exc

    return boom
