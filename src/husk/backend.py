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
