"""ControllerState snapshot enrichment + status table rendering."""

from __future__ import annotations

import json

from conftest import make_runner, make_slot
from husk.cli import _print_status, _table
from husk.slot import SlotState
from husk.snapshot import ControllerState


def _snap(classified):
    return ControllerState.from_classified(
        generation=1,
        backend="fake",
        min_ready=1,
        max_total=2,
        desired_total=2,
        classified=classified,
    )


def test_slotview_captures_detail():
    slot = make_slot(
        id="vm-1", name="husk-1", status="ACTIVE", task_state=None, cycle=3
    )
    runner = make_runner(id=7, name="husk-1-c3", status="online", busy=True)
    snap = _snap([(slot, runner, SlotState.BUSY)])

    v = snap.slots[0]
    assert (v.id, v.name, v.state, v.status) == ("vm-1", "husk-1", "busy", "ACTIVE")
    assert v.runner == "husk-1-c3" and v.runner_status == "online"
    assert v.busy is True and v.cycle == 3
    assert snap.counts["busy"] == 1


def test_slotview_no_runner():
    slot = make_slot(id="vm-2", name="husk-2", status="SHUTOFF")
    snap = _snap([(slot, None, SlotState.NEEDS_RECYCLE)])

    v = snap.slots[0]
    assert v.runner is None and v.runner_status is None and v.busy is False


def test_slotview_surfaces_active_image_shortened():
    # With no configured ref (tag unknown), fall back to the 12-char digest.
    slot = make_slot(active_image="sha256:" + "c" * 64, image_stale=False)
    v = _snap([(slot, None, SlotState.IDLE)]).slots[0]
    assert v.image == "cccccccccccc"
    assert v.image_stale is False


def _snap_ref(classified, image_ref):
    return ControllerState.from_classified(
        generation=1,
        backend="libvirt-gpu",
        min_ready=1,
        max_total=2,
        desired_total=1,
        classified=classified,
        image_ref=image_ref,
    )


def test_current_slot_shows_the_configured_tag():
    # A non-stale slot is on the pool's target, so name its tag — the rollout signal.
    slot = make_slot(active_image="sha256:" + "e" * 64, image_stale=False)
    snap = _snap_ref([(slot, None, SlotState.IDLE)], "ghcr.io/acts/husk-gpu:v4")
    assert snap.image_ref == "ghcr.io/acts/husk-gpu:v4"
    assert snap.slots[0].image == "v4"


def test_stale_slot_shows_digest_not_tag():
    # A stale slot is NOT on the target tag, and its own tag isn't recorded — so it
    # must not be mislabeled "v4"; show the digest, flagged stale.
    slot = make_slot(active_image="sha256:" + "a" * 64, image_stale=True)
    snap = _snap_ref([(slot, None, SlotState.IDLE)], "ghcr.io/acts/husk-gpu:v4")
    assert snap.slots[0].image == "aaaaaaaaaaaa"
    assert snap.slots[0].image_stale is True


def test_ref_tag_parsing():
    from husk.snapshot import _ref_tag

    assert _ref_tag("ghcr.io/acts-project/husk-gpu:v4") == "v4"
    assert _ref_tag("registry:5000/o/husk-base:v2") == "v2"  # not fooled by host:port
    assert _ref_tag("ghcr.io/o/husk-gpu@sha256:" + "d" * 64) == ""  # digest-pinned
    assert _ref_tag("ghcr.io/o/husk-gpu") == ""  # tagless
    assert _ref_tag("husk-gpu-golden.qcow2") == ""  # manual/local name
    assert _ref_tag("") == ""


def test_slotview_flags_stale_image():
    slot = make_slot(active_image="sha256:" + "d" * 64, image_stale=True)
    v = _snap([(slot, None, SlotState.IDLE)]).slots[0]
    assert v.image == "dddddddddddd" and v.image_stale is True


def test_slotview_image_none_when_backend_cant_report():
    v = _snap([(make_slot(), None, SlotState.IDLE)]).slots[0]
    assert v.image is None  # active_image unset → no image shown, not a crash


def test_image_fields_round_trip():
    slot = make_slot(active_image="sha256:" + "a" * 64, image_stale=True)
    snap = _snap([(slot, None, SlotState.IDLE)])
    back = ControllerState.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert back.slots[0].image == "aaaaaaaaaaaa"
    assert back.slots[0].image_stale is True


def test_to_dict_includes_detail():
    slot = make_slot(id="vm-1", name="husk-1", status="ACTIVE", cycle=2)
    runner = make_runner(name="husk-1-c2")
    d = _snap([(slot, runner, SlotState.IDLE)]).to_dict()
    assert d["slots"][0]["cycle"] == 2
    assert d["slots"][0]["runner"] == "husk-1-c2"
    assert set(d) >= {"backend", "counts", "desired_total", "slots"}


def test_print_status_renders_table(capsys):
    slot = make_slot(id="vm-1", name="husk-1", status="ACTIVE", cycle=1)
    runner = make_runner(name="husk-1-c1", busy=True)
    _print_status(_snap([(slot, runner, SlotState.BUSY)]))

    out = capsys.readouterr().out
    assert "backend : fake" in out
    assert "states  :" in out and "busy=1" in out
    # table header + the slot row present
    assert "STATE" in out and "CYCLE" in out
    assert "vm-1" in out and "husk-1-c1" in out


def test_status_table_sorted_by_name():
    from husk.cli import _status_table

    classified = [
        (
            make_slot(id="vm-z", name="husk-9", status="SHUTOFF"),
            None,
            SlotState.NEEDS_RECYCLE,
        ),
        (
            make_slot(id="vm-a", name="husk-1", status="ACTIVE", cycle=1),
            make_runner(name="husk-1-c1", busy=True),
            SlotState.BUSY,
        ),
    ]
    table = _status_table(_snap(classified))
    assert list(table.columns[0]._cells) == ["vm-a", "vm-z"]


def test_rich_status_table_row_count():
    from husk.cli import _status_table

    classified = [
        (
            make_slot(id="vm-1", name="husk-1", status="ACTIVE", cycle=1),
            make_runner(name="husk-1-c1", busy=True),
            SlotState.BUSY,
        ),
        (
            make_slot(id="vm-2", name="husk-2", status="SHUTOFF"),
            None,
            SlotState.NEEDS_RECYCLE,
        ),
    ]
    assert _status_table(_snap(classified)).row_count == 2


def test_watch_table_width_stable_when_cells_empty():
    # The live --watch table must not jitter as a slot's runner/task/timing flip
    # between values and "-": min_width floors keep every column padded.
    import io

    from rich.console import Console

    from husk.cli import _status_table

    def widths(classified):
        console = Console(file=io.StringIO(), width=200, color_system=None)
        console.print(_status_table(_snap(classified)))
        return [len(line) for line in console.file.getvalue().splitlines()]

    populated = [
        (
            make_slot(
                id="vm-1",
                name="husk-1",
                status="ACTIVE",
                task_state="rebuilding",
                cycle=2,
            ),
            make_runner(name="husk-1-c2", busy=True),
            SlotState.STARTING,
        )
    ]
    empty = [  # same slot draining: runner "-", task "-", no timing
        (
            make_slot(id="vm-1", name="husk-1", status="ACTIVE", cycle=2),
            None,
            SlotState.STARTING,
        )
    ]
    assert widths(populated) == widths(empty)


def test_rich_status_renderable_renders_text():
    import io

    from rich.console import Console

    from husk.cli import _status_renderable

    snap = _snap(
        [
            (
                make_slot(id="vm-1", name="husk-1", status="ACTIVE", cycle=4),
                make_runner(name="husk-1-c4", status="online", busy=True),
                SlotState.BUSY,
            ),
        ]
    )
    console = Console(file=io.StringIO(), width=140, color_system=None)
    console.print(_status_renderable(snap))
    out = console.file.getvalue()
    assert "backend" in out and "husk-1" in out and "busy" in out and "CYCLE" in out


def test_watch_status_stops_on_interrupt(monkeypatch):
    from husk import cli

    frames = []

    def fake_sleep(_):
        frames.append(1)
        if len(frames) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    snap = _snap([(make_slot(), make_runner(), SlotState.IDLE)])
    cli._watch_status(lambda: snap, interval=0.0)  # exits cleanly, no exception
    assert len(frames) >= 2


def test_watch_status_survives_observe_error(monkeypatch):
    from husk import cli

    def fake_sleep(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    def boom():
        raise RuntimeError("list failed")

    cli._watch_status(boom, interval=0.0)  # error rendered, not raised


def test_state_roundtrip_to_from_dict():
    classified = [
        (
            make_slot(
                id="vm-1", name="husk-1", status="ACTIVE", task_state=None, cycle=2
            ),
            make_runner(name="husk-1-c2", status="online", busy=True),
            SlotState.BUSY,
        ),
        (
            make_slot(
                id="vm-2", name="husk-2", status="REBUILD", task_state="rebuilding"
            ),
            None,
            SlotState.STARTING,
        ),
    ]
    snap = _snap(classified)
    back = ControllerState.from_dict(snap.to_dict())
    assert back.to_dict() == snap.to_dict()
    assert back.slots[0].runner == "husk-1-c2" and back.slots[0].busy is True


def test_table_alignment():
    rendered = _table(["A", "BB"], [["x", "yy"], ["longer", "z"]])
    lines = rendered.splitlines()
    assert lines[0].startswith("A")
    # every row padded to the widest cell in the column ("longer" = 6 wide)
    assert all(line.startswith(line[:6]) for line in lines[2:])
    assert "longer" in lines[-1]
