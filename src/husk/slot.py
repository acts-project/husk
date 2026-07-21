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
    # Identity of the image this slot is ACTUALLY running — distinct from image_id,
    # which for libvirt is the host's *current* golden (not what a stale slot booted
    # from). libvirt: the baked content digest (husk-image-digest metadata);
    # OpenStack: the booted Glance image id. Surfaced on the dashboard so a rollout
    # is visible per slot. None when the backend can't report it.
    active_image: str | None = None
    # Metrics-discovery hints (observability http_sd). OpenStack: the guest fixed
    # IP (directly scrapeable). libvirt: no guest IP (never contacted) — carries the
    # host name instead, and scraping routes through that host's metrics proxy.
    ip: str | None = None
    host: str | None = None


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


def orphaned_runners(
    runners: list[Runner], slots: list[Slot], prefix: str
) -> list[Runner]:
    """Offline runner registrations owned by this pool that no live slot needs.

    Two ways a registration becomes garbage (see the module docstring on naming:
    runners are ``f"{vm}-c{cycle}"``):

      * its `vm` names no slot that exists any more — the slot was destroyed, or
        ownership moved (pool renamed, `vm_prefix` changed, project switched);
      * its `vm` is live but the cycle is STRICTLY OLDER than that slot's current
        cycle — a prior-cycle registration the slot has already moved past.

    Deliberately conservative in three ways, because the cost of a wrong delete
    (a slot that can never register, then rebuilds after its grace) far exceeds
    the cost of a missed one (a stale row in the runners list):

      * `prefix` scoping means only this pool's own names are ever candidates;
      * an offline runner at cycle >= the slot's current cycle is KEPT — that is
        the mid-boot case, where the JIT config is minted and reads `offline`
        until the runner process connects;
      * an unparseable name (no trailing -c<N>) is kept, never guessed at.
    """
    live = {s.name: s.cycle for s in slots}
    out: list[Runner] = []
    for r in runners:
        if r.online or not r.name.startswith(prefix):
            continue
        vm, sep, tail = r.name.rpartition("-c")
        if not sep or not tail.isdigit():
            continue  # not a husk cycle name — leave it alone
        current = live.get(vm)
        if current is None or int(tail) < current:
            out.append(r)
    return out


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
