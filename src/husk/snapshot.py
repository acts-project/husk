"""Published controller state — the metrics / status-board data seam.

The reconcile loop publishes one `ControllerState` per tick. `huskctl status`
renders it now; a future `/metrics` (Prometheus) or `/status` (web board) are
thin renderers of this same object — no controller change required.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from husk.slot import SlotState


@dataclass(frozen=True)
class SlotView:
    """A flat, serializable summary of one classified slot."""

    id: str
    name: str
    state: str  # SlotState.value
    status: str  # backend status (ACTIVE/SHUTOFF/...)


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
    ) -> "ControllerState":
        counts = {st.value: 0 for st in SlotState}
        views: list[SlotView] = []
        for slot, _runner, state in classified:
            counts[state.value] += 1
            views.append(
                SlotView(
                    id=slot.id, name=slot.name, state=state.value, status=slot.status
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
