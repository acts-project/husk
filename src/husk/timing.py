"""Per-slot timing accounting, derived purely from the controller's own
observations (no SSH into the VM).

Two things are tracked:

* **Phase durations** of the most recent bring-up, from state transitions:
    - boot       = create/rebuild issued → ACTIVE      (Neutron/spawn)
    - cloud-init = ACTIVE → runner online              (the cloud-init step)
    - recycle    = create/rebuild issued → runner online (the whole bring-up)
* **Time-in-state** seconds (cumulative since the controller first saw the slot),
  which yields a "live-time" busy fraction = busy_seconds / total_seconds.

All times are the controller's monotonic clock; figures reset if huskd restarts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from husk.slot import SlotState


def _zero_states() -> dict[str, float]:
    return {s.value: 0.0 for s in SlotState}


@dataclass
class SlotTiming:
    first_seen: float
    state_seconds: dict[str, float] = field(default_factory=_zero_states)
    issued_at: float | None = None  # last create/rebuild issued (monotonic)
    active_at: float | None = None  # last ACTIVE transition
    last_boot_seconds: float | None = None
    last_cloudinit_seconds: float | None = None
    last_recycle_seconds: float | None = None
    # systemd-analyze phase durations from the last husk-bootreport block (distinct
    # from last_boot_seconds above, which is the controller-observed create→ACTIVE
    # spawn time). Any may stay None if the report is absent/unparseable.
    last_boot_kernel_seconds: float | None = None
    last_boot_initrd_seconds: float | None = None
    last_boot_userspace_seconds: float | None = None
    last_boot_total_seconds: float | None = None
    # Slowest entries from the two blame sections: (name, seconds), slowest first.
    last_boot_units: list[tuple[str, float]] = field(default_factory=list)
    last_boot_stages: list[tuple[str, float]] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return sum(self.state_seconds.values())

    @property
    def live_fraction(self) -> float | None:
        """Fraction of tracked time the slot was *available to serve* — running a
        job (BUSY) or warm and waiting for one (IDLE) — vs. overhead time spent
        starting/rebuilding/recycling/broken."""
        total = self.total_seconds
        if total <= 0:
            return None
        live = (
            self.state_seconds[SlotState.BUSY.value]
            + self.state_seconds[SlotState.IDLE.value]
        )
        return live / total

    def accumulate(self, state: SlotState, dt: float) -> None:
        if dt > 0:
            self.state_seconds[state.value] += dt

    def on_issued(self, now: float) -> None:
        """A fresh create/rebuild was issued — start a new bring-up timing."""
        self.issued_at = now
        self.active_at = None

    def on_active(self, now: float) -> None:
        self.active_at = now
        if self.issued_at is not None:
            self.last_boot_seconds = now - self.issued_at

    def on_runner_online(self, now: float) -> None:
        if self.active_at is not None:
            self.last_cloudinit_seconds = now - self.active_at
        if self.issued_at is not None:
            self.last_recycle_seconds = now - self.issued_at

    def on_bootreport(
        self,
        *,
        kernel: float | None,
        initrd: float | None,
        userspace: float | None,
        total: float | None,
        units: list[tuple[str, float]] | None = None,
        stages: list[tuple[str, float]] | None = None,
    ) -> None:
        """Record the systemd-analyze phase durations and slowest-unit/stage blame
        entries parsed from the slot's husk-bootreport console block (see
        husk.bootreport)."""
        self.last_boot_kernel_seconds = kernel
        self.last_boot_initrd_seconds = initrd
        self.last_boot_userspace_seconds = userspace
        self.last_boot_total_seconds = total
        self.last_boot_units = list(units or [])
        self.last_boot_stages = list(stages or [])
