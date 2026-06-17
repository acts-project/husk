"""Published controller state — the metrics / status-board data seam.

The reconcile loop publishes one `ControllerState` per tick. `huskctl status`
renders it now; a future `/metrics` (Prometheus) or `/status` (web board) are
thin renderers of this same object — no controller change required.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field

from husk.slot import SlotState


@dataclass(frozen=True)
class SlotView:
    """A flat, serializable summary of one classified slot."""

    id: str
    name: str
    state: str  # SlotState.value (husk's classification)
    status: str  # backend/Nova status (ACTIVE/SHUTOFF/...)
    task_state: str | None  # in-flight provisioning task, if any
    runner: str | None  # matched GitHub runner name, if any
    runner_status: str | None  # "online" | "offline" | None
    busy: bool  # runner currently running a job
    cycle: int  # recycle cycle (durable husk-cycle)
    cloudinit_seconds: float | None = (
        None  # last ACTIVE→runner-online (cloud-init step)
    )
    recycle_seconds: float | None = None  # last issue→runner-online (whole bring-up)
    live_fraction: float | None = (
        None  # (busy+idle) / total tracked ("available to serve")
    )


@dataclass(frozen=True)
class ControllerState:
    """Immutable snapshot the loop swaps in atomically each tick."""

    generation: int
    last_reconcile_epoch: float
    backend: str
    min_ready: int
    max_total: int
    desired_total: int
    counts: dict[str, int]  # SlotState.value -> count
    slots: list[SlotView] = field(default_factory=list)

    @classmethod
    def from_classified(
        cls,
        *,
        generation: int,
        backend: str,
        min_ready: int,
        max_total: int,
        desired_total: int,
        classified: list[tuple],  # list of (Slot, Runner|None, SlotState)
        timing: dict | None = None,  # slot_id -> SlotTiming (optional)
    ) -> "ControllerState":
        timing = timing or {}
        counts = {st.value: 0 for st in SlotState}
        views: list[SlotView] = []
        for slot, runner, state in classified:
            counts[state.value] += 1
            t = timing.get(slot.id)
            lf = t.live_fraction if t is not None else None
            views.append(
                SlotView(
                    id=slot.id,
                    name=slot.name,
                    state=state.value,
                    status=slot.status,
                    task_state=slot.task_state,
                    runner=runner.name if runner else None,
                    runner_status=runner.status if runner else None,
                    busy=runner.busy if runner else False,
                    cycle=slot.cycle,
                    cloudinit_seconds=(
                        round(t.last_cloudinit_seconds, 1)
                        if t is not None and t.last_cloudinit_seconds is not None
                        else None
                    ),
                    recycle_seconds=(
                        round(t.last_recycle_seconds, 1)
                        if t is not None and t.last_recycle_seconds is not None
                        else None
                    ),
                    live_fraction=round(lf, 3) if lf is not None else None,
                )
            )
        return cls(
            generation=generation,
            last_reconcile_epoch=time.time(),
            backend=backend,
            min_ready=min_ready,
            max_total=max_total,
            desired_total=desired_total,
            counts=counts,
            slots=views,
        )

    def to_dict(self) -> dict:
        """Plain dict for JSON rendering (huskctl status / future /status)."""
        return {
            "generation": self.generation,
            "last_reconcile_epoch": self.last_reconcile_epoch,
            "backend": self.backend,
            "min_ready": self.min_ready,
            "max_total": self.max_total,
            "desired_total": self.desired_total,
            "counts": dict(self.counts),
            "slots": [vars(v) for v in self.slots],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ControllerState":
        return cls(
            generation=d["generation"],
            last_reconcile_epoch=d["last_reconcile_epoch"],
            backend=d["backend"],
            min_ready=d["min_ready"],
            max_total=d["max_total"],
            desired_total=d["desired_total"],
            counts=dict(d["counts"]),
            slots=[SlotView(**sv) for sv in d["slots"]],
        )


def _atomic_write(path: str, data: str) -> None:
    """Write `data` to `path` via tmp file + rename (never a torn read)."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".huskd-state-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_state(path: str, state: ControllerState) -> None:
    """Atomically publish one snapshot to `path` (single-pool / direct Controller)."""
    _atomic_write(path, json.dumps(state.to_dict()))


def read_state(path: str) -> ControllerState | None:
    """Read a single published snapshot; None if missing or unparseable."""
    try:
        with open(path) as f:
            return ControllerState.from_dict(json.load(f))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_states(path: str, states: list[ControllerState]) -> None:
    """Atomically publish the per-pool snapshots as a JSON list (multi-pool huskd).

    The container is always a list (one element per pool); element format is the
    same `ControllerState.to_dict()` a single-pool publish used. huskd and huskctl
    ship together, so there is no bare-object back-compat to carry."""
    _atomic_write(path, json.dumps([s.to_dict() for s in states]))


def read_states(path: str) -> list[ControllerState]:
    """Read the published per-pool snapshots; [] if missing or unparseable."""
    try:
        with open(path) as f:
            return [ControllerState.from_dict(d) for d in json.load(f)]
    except (OSError, ValueError, KeyError, TypeError):
        return []
