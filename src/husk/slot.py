"""Slot value objects and the pure state classifier.

This module has no I/O and no third-party imports — it is the testable heart of
the controller. Backends build `Slot`s from their native resources; the GitHub
client builds `Runner`s; `classify()` maps a (Slot, Runner) snapshot plus a
provision age onto a `SlotState`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class SlotState(enum.Enum):
    """The lifecycle state the controller acts on each tick."""

    IDLE = "idle"  # warm, runner online, waiting for a job
    BUSY = "busy"  # runner online and running a job
    STARTING = "starting"  # provisioning / cloud-init still installing runner
    NEEDS_RECYCLE = (
        "needs_recycle"  # SHUTOFF — job done, poweroff fired, ready to rebuild
    )
    UNHEALTHY = "unhealthy"  # ACTIVE but no runner past the startup grace
    ERROR = "error"  # Nova ERROR — the only state that earns a destroy


@dataclass(frozen=True)
class Slot:
    """A normalized view of one backend VM ("slot")."""

    id: str  # backend id (Nova server id) — stable across rebuilds
    name: str  # VM name (timestamped; stable across rebuilds)
    status: str  # ACTIVE | SHUTOFF | REBUILD | BUILD | ERROR
    task_state: (
        str | None
    )  # in-flight provisioning task (rebuilding/spawning/...) or None
    created_at: float  # backend creation epoch (wall-clock; NOT used for age timers)
    flavor_id: str  # running flavor — seam for future flavor migration
    image_id: str  # booted image — seam for future image migration
    cycle: int = 0  # from durable metadata husk-cycle (see controller restart)
    provisioned_at: float | None = None  # durable metadata husk-provisioned-at (epoch)
    fault: str | None = None
    # True when the slot was built from an image whose digest no longer matches
    # the backend's current image (a config image_ref change). The controller
    # drains such a slot onto the new image. Always False for backends with no
    # image-versioning concept (OpenStack/fake), so the drain rule is inert there.
    image_stale: bool = False


@dataclass(frozen=True)
class Capacity:
    """How much room a backend has to create more slots."""

    can_create: bool
    free_instances: int


@dataclass(frozen=True)
class Runner:
    """A normalized view of one GitHub Actions runner registration."""

    id: int
    name: str
    status: str  # "online" | "offline"
    busy: bool

    @property
    def online(self) -> bool:
        return self.status == "online"


def _cycle_of(runner: Runner) -> int:
    """Extract the trailing -c<N> cycle number from a runner name (-1 if absent)."""
    try:
        return int(runner.name.rsplit("-c", 1)[1])
    except (IndexError, ValueError):
        return -1


def match_runner(runners: list[Runner], slot: Slot) -> Runner | None:
    """Find the GitHub runner belonging to `slot`.

    Runner names are ``f"{vm}-c{cycle}"`` (unique per recycle cycle), so a slot
    matches any runner whose name starts with ``slot.name + "-c"``. Stale runners
    from prior cycles linger offline until reaped; prefer an online match, then
    the highest cycle number.
    """
    candidates = [r for r in runners if r.name.startswith(slot.name + "-c")]
    if not candidates:
        return None
    online = [r for r in candidates if r.online]
    return max(online or candidates, key=_cycle_of)


def classify(
    slot: Slot,
    runner: Runner | None,
    *,
    provision_age: float | None,
    startup_grace: float,
) -> SlotState:
    """Map a snapshot onto a `SlotState`. Pure; precedence is first-match-wins.

    `provision_age` is seconds since the controller last issued create/rebuild/
    start on this slot (or None if unknown — treated as freshly provisioned).
    Keeping the age as a scalar makes this clock-agnostic and trivially testable.
    """
    # 1. ERROR — the only state that earns a destroy.
    if slot.status == "ERROR":
        return SlotState.ERROR

    # 2. Any in-flight provisioning task (or REBUILD status) → still settling.
    #    Checked BEFORE SHUTOFF: mid-rebuild Nova can show SHUTOFF with a task
    #    set (rebuild preserves the pre-rebuild power state while spawning runs).
    if slot.status == "REBUILD" or slot.task_state is not None:
        return SlotState.STARTING

    # 3. SHUTOFF with task cleared — job done, poweroff fired, ready to rebuild.
    if slot.status == "SHUTOFF":
        return SlotState.NEEDS_RECYCLE

    # 4. Fresh create still provisioning (the slow Neutron phase).
    if slot.status == "BUILD":
        return SlotState.STARTING

    # 5/6. ACTIVE with a live runner.
    if runner is not None and runner.online:
        return SlotState.BUSY if runner.busy else SlotState.IDLE

    # 7. ACTIVE, no live runner, still within the grace window → cloud-init is
    #    likely still installing the runner.
    if provision_age is None or provision_age <= startup_grace:
        return SlotState.STARTING

    # 8. ACTIVE, no runner, past grace → something went wrong.
    return SlotState.UNHEALTHY
