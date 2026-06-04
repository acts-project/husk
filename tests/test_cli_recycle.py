"""`huskctl recycle` target selection + stop semantics (the _recycle helper)."""

from __future__ import annotations

from conftest import make_runner, make_slot
from husk.cli import _recycle
from husk.fake_backend import FakeBackend, FakeGitHub


def _names(slots):
    return sorted(s.name for s in slots)


def test_all_stops_idle_active_slots():
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-1", name="husk-1", status="ACTIVE"),
            make_slot(id="vm-2", name="husk-2", status="ACTIVE"),
        ]
    )
    acted, skipped, unknown = _recycle(
        backend, FakeGitHub(), names=[], all_slots=True, force=False, dry_run=False
    )
    assert _names(acted) == ["husk-1", "husk-2"] and not skipped and not unknown
    # a stop drives each to SHUTOFF — the NEEDS_RECYCLE trigger huskd reconciles
    assert backend.ops() == ["stop", "stop"]
    assert all(s.status == "SHUTOFF" for s in backend.slots)


def test_busy_slot_skipped_without_force():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(
        runners=[make_runner(name="husk-1-c0", status="online", busy=True)]
    )
    acted, skipped, _ = _recycle(
        backend, github, names=[], all_slots=True, force=False, dry_run=False
    )
    assert not acted and backend.calls == []
    assert len(skipped) == 1 and "busy" in skipped[0][1]


def test_busy_slot_recycled_with_force():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(
        runners=[make_runner(name="husk-1-c0", status="online", busy=True)]
    )
    acted, skipped, _ = _recycle(
        backend, github, names=["husk-1"], all_slots=False, force=True, dry_run=False
    )
    assert _names(acted) == ["husk-1"] and not skipped
    assert backend.ops() == ["stop"]


def test_non_active_slot_skipped():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    acted, skipped, _ = _recycle(
        backend, FakeGitHub(), names=[], all_slots=True, force=False, dry_run=False
    )
    assert not acted and backend.calls == []
    assert len(skipped) == 1 and "not ACTIVE" in skipped[0][1]


def test_dry_run_changes_nothing():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    acted, _, _ = _recycle(
        backend, FakeGitHub(), names=[], all_slots=True, force=False, dry_run=True
    )
    assert _names(acted) == ["husk-1"]  # reported as a target...
    assert backend.calls == []  # ...but no stop issued
    assert backend.slots[0].status == "ACTIVE"


def test_select_by_id_and_name_unknown_reported():
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-1", name="husk-1", status="ACTIVE"),
            make_slot(id="vm-2", name="husk-2", status="ACTIVE"),
        ]
    )
    acted, skipped, unknown = _recycle(
        backend,
        FakeGitHub(),
        names=["vm-1", "husk-2", "ghost"],
        all_slots=False,
        force=False,
        dry_run=False,
    )
    assert _names(acted) == ["husk-1", "husk-2"]
    assert unknown == ["ghost"] and not skipped


def test_duplicate_tokens_stop_once():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    acted, _, _ = _recycle(
        backend,
        FakeGitHub(),
        names=["vm-1", "husk-1"],  # same slot via id and name
        all_slots=False,
        force=False,
        dry_run=False,
    )
    assert _names(acted) == ["husk-1"]
    assert backend.ops() == ["stop"]


def test_github_list_failure_does_not_block_recycle():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub()
    github.raise_on_list = True  # can't tell busy → best-effort, still recycle
    acted, skipped, _ = _recycle(
        backend, github, names=[], all_slots=True, force=False, dry_run=False
    )
    assert _names(acted) == ["husk-1"] and not skipped
