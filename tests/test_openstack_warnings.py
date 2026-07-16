"""A swallowed metadata-write failure (e.g. CERN's CernLanDB 500) must NOT fail the
operation, but it must be recorded as a per-slot warning so the dashboard shows it
instead of it being log-only. mark_active / rebuild's metadata persist are the
non-fatal path; the rebuild ACTION failing is the fatal path (surfaced elsewhere)."""

from __future__ import annotations

from types import SimpleNamespace

from husk.config import BackendConfig
from husk.openstack_backend import OpenStackBackend
from husk.slot import Slot


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


def _backend(*, meta_raises: bool):
    b = OpenStackBackend.__new__(OpenStackBackend)
    b.cfg = BackendConfig(name="os", type="openstack", min_ready=1, max_total=1)
    b._warnings = {}

    def set_server_metadata(server_id, **kw):
        if meta_raises:
            raise RuntimeError("HTTP 500: CernLanDB")

    b.conn = SimpleNamespace(
        compute=SimpleNamespace(set_server_metadata=set_server_metadata)
    )
    return b


def test_mark_active_records_a_warning_but_does_not_raise():
    b = _backend(meta_raises=True)
    b.mark_active(_slot())  # must NOT raise — the op is non-fatal
    warns = b.slot_warnings()
    assert "vm-1" in warns
    ts, msg = warns["vm-1"]
    assert "metadata write failed" in msg and ts > 0


def test_successful_mark_active_clears_a_prior_warning():
    b = _backend(meta_raises=True)
    b.mark_active(_slot())
    assert "vm-1" in b.slot_warnings()
    # backend recovers → next write succeeds → warning cleared
    b._backend_ok = True
    b.conn.compute.set_server_metadata = lambda server_id, **kw: None
    b.mark_active(_slot())
    assert "vm-1" not in b.slot_warnings()


def test_no_warning_when_metadata_write_succeeds():
    b = _backend(meta_raises=False)
    b.mark_active(_slot())
    assert b.slot_warnings() == {}
