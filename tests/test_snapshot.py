"""ControllerState snapshot enrichment + status table rendering."""

from __future__ import annotations

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


def test_table_alignment():
    rendered = _table(["A", "BB"], [["x", "yy"], ["longer", "z"]])
    lines = rendered.splitlines()
    assert lines[0].startswith("A")
    # every row padded to the widest cell in the column ("longer" = 6 wide)
    assert all(line.startswith(line[:6]) for line in lines[2:])
    assert "longer" in lines[-1]
