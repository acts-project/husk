"""The `Backend` seam — the abstraction that makes failures injectable.

The single most important contract here is `list_slots()`:

* it **raises** `ListSlotsError` on ANY failure (auth expiry, 5xx, network). It
  must never return ``[]`` to mean "couldn't tell".
* returning ``[]`` is a trusted observation: "zero managed slots".

The controller relies on that distinction for its hard fail-safe: a raise aborts
the whole tick before any mutation, while ``[]`` may legitimately trigger creates.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from husk.slot import Capacity, Slot


class BackendError(Exception):
    """Base for backend failures."""


class ListSlotsError(BackendError):
    """Raised when the backend cannot enumerate its slots.

    Catching this in the reconcile loop is what guarantees a listing failure
    aborts the tick rather than being misread as "no slots ⇒ destroy/create".
    """


@runtime_checkable
class Backend(Protocol):
    """Infrastructure backend that owns a pool of recyclable slots."""

    def list_slots(self) -> list[Slot]:
        """Return all slots this backend manages (filtered to managed-by=husk).

        Raises:
            ListSlotsError: on any failure to enumerate. Never returns a sentinel.
        """
        ...

    def create_slot(self, *, user_data: bytes, name: str, cycle: int) -> Slot:
        """Provision a fresh slot booting the given cloud-init user-data."""
        ...

    def rebuild_slot(self, slot: Slot, *, user_data: bytes, cycle: int) -> None:
        """Re-image a slot in place with fresh user-data (disk wiped, JIT refreshed).

        Non-blocking: issues the action and returns. Nova preserves power state,
        so a SHUTOFF slot stays SHUTOFF — the controller issues `start_slot` once
        the rebuild settles.
        """
        ...

    def start_slot(self, slot: Slot) -> None:
        """os-start a slot (used after a rebuild settles to SHUTOFF)."""
        ...

    def mark_active(self, slot: Slot) -> None:
        """Persist the startup-grace origin once a slot has reached ACTIVE.

        Resets the durable `husk-provisioned-at` so stateless observers
        (`huskctl status`) and a restarted controller anchor the grace to the
        boot, not the (long, on CERN) create — otherwise a freshly-built slot
        reads UNHEALTHY before cloud-init can register its runner."""
        ...

    def stop_slot(self, slot: Slot) -> None:
        """os-stop a slot → SHUTOFF. The timeout action (NOT a destroy)."""
        ...

    def destroy_slot(self, slot: Slot, *, reason: str) -> None:
        """Delete a slot. Reserved for ERROR (unrecoverable) and decommission
        (hysteresis-guarded ramp-down); never used as a timeout action."""
        ...

    def capacity(self) -> Capacity:
        """Best-effort free capacity. The controller also clamps to max_total."""
        ...

    def console_output(self, slot: Slot, *, lines: int | None = None) -> str | None:
        """Best-effort serial-console log of the slot — for the boot report.

        `lines` tails the last N lines; None (default) returns the whole captured
        log. Full-log is the default deliberately: the husk-bootreport block is
        emitted late in boot but cloud-init keeps printing after it, so a small
        tail can miss it — the parser picks the last complete block out of the full
        text cheaply.

        Returns None when unavailable (backend can't read it, transient error, or
        not yet supported). Unlike `list_slots`, this NEVER raises: boot-timing is
        an observability nicety and must not abort a reconcile tick.
        """
        ...

    def image_ready(self, slot: Slot) -> bool:
        """Whether this slot can be (re)imaged now — i.e. the configured golden has
        finished staging for it. `capacity()` already gates *grows* on this; the
        controller also consults it before a *rebuild* so a recycle doesn't drive
        into a no-image error while the golden is still in flight. Backends with no
        async image delivery (fake / manual local-file) are always ready."""
        ...
