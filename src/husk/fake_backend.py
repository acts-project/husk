"""In-memory `Backend` and GitHub fakes for the unit-test matrix.

These live under `src/` (not `tests/`) so they are importable as `husk.*` and can
double as a reference implementation of the seams. Every mutation is recorded in
`.calls` so tests can assert *exactly* which actions a tick took, and helper
mutators (`set_status`, `set_runner`) let a test drive multi-tick narratives.
"""

from __future__ import annotations

import itertools
import time

from husk.backend import ListSlotsError
from husk.slot import Capacity, Runner, Slot


class FakeBackend:
    """Scriptable `Backend`. Inject failures via `raise_on_list`; constrain
    growth via `cap`. Mutations update `slots` so multi-tick tests are realistic."""

    def __init__(
        self,
        slots: list[Slot] | None = None,
        capacity: Capacity = Capacity(can_create=True, free_instances=99),
        image_ready: bool = True,
    ) -> None:
        self.slots: list[Slot] = list(slots or [])
        self.cap = capacity
        self._image_ready = image_ready
        self.raise_on_list = False
        self.calls: list[tuple] = []
        self._ids = itertools.count(1)
        self.console_text: str | None = None  # scripted serial console (bootreport)
        self.console_reads: list[str] = []  # slot ids read (console_output is no-op)

    # --- Backend protocol -------------------------------------------------
    def list_slots(self) -> list[Slot]:
        if self.raise_on_list:
            raise ListSlotsError("injected list failure")
        return list(self.slots)

    def create_slot(self, *, user_data: bytes, name: str, cycle: int) -> Slot:
        self.calls.append(("create", name))
        slot = Slot(
            id=f"vm-{next(self._ids)}",
            name=name,
            status="BUILD",
            task_state="spawning",
            created_at=0.0,
            flavor_id="flavor-current",
            image_id="image-current",
            cycle=cycle,
        )
        self.slots.append(slot)
        return slot

    def rebuild_slot(self, slot: Slot, *, user_data: bytes, cycle: int) -> None:
        self.calls.append(("rebuild", slot.id))
        # Emulate Nova: rebuild starts an in-flight task, power state preserved.
        self.set_status(slot.id, task_state="rebuilding", cycle=cycle)

    def start_slot(self, slot: Slot) -> None:
        self.calls.append(("start", slot.id))
        self.set_status(slot.id, status="ACTIVE", task_state=None)

    def mark_active(self, slot: Slot) -> None:
        self.calls.append(("mark_active", slot.id))
        self.set_status(slot.id, provisioned_at=time.time())

    def stop_slot(self, slot: Slot) -> None:
        self.calls.append(("stop", slot.id))
        self.set_status(slot.id, status="SHUTOFF", task_state=None)

    def destroy_slot(self, slot: Slot, *, reason: str) -> None:
        self.calls.append(("destroy", slot.id, reason))
        self.slots = [s for s in self.slots if s.id != slot.id]

    def capacity(self) -> Capacity:
        return self.cap

    def image_ready(self, slot: Slot) -> bool:
        return self._image_ready

    def console_output(self, slot: Slot, *, lines: int | None = None) -> str | None:
        self.console_reads.append(slot.id)  # read-only: kept out of .calls (actions)
        return self.console_text

    # --- test helpers -----------------------------------------------------
    def set_status(self, slot_id: str, **changes) -> None:
        """Replace a stored slot with a copy carrying the given field changes."""
        from dataclasses import replace

        for i, s in enumerate(self.slots):
            if s.id == slot_id:
                self.slots[i] = replace(s, **changes)
                return
        raise KeyError(slot_id)

    def ops(self) -> list[str]:
        """Just the operation names, for terse assertions."""
        return [c[0] for c in self.calls]


class FakeGitHub:
    """Scriptable GitHub client. `raise_on_list` exercises the GitHub-side abort."""

    def __init__(self, runners: list[Runner] | None = None) -> None:
        self.runners: list[Runner] = list(runners or [])
        self.raise_on_list = False
        self.calls: list[tuple] = []
        self._jit = itertools.count(1)

    def list_runners(self) -> list[Runner]:
        if self.raise_on_list:
            raise RuntimeError("injected GitHub list failure")
        return list(self.runners)

    def generate_jitconfig(self, name: str) -> str:
        self.calls.append(("mint", name))
        return f"jit-{name}-{next(self._jit)}"

    def delete_runner(self, runner_id: int) -> None:
        self.calls.append(("delete_runner", runner_id))
        self.runners = [r for r in self.runners if r.id != runner_id]

    def ops(self) -> list[str]:
        return [c[0] for c in self.calls]
