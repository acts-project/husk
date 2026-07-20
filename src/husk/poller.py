"""Centralized GitHub runner polling — the single producer of runner snapshots.

Reconcile no longer calls `list_runners()` inline. One `RunnerPoller` task polls
each target's runner listing on its own cadence and publishes it into the
`SnapshotRegistry`; every `(target, pool)` reconcile task reads that same
in-memory snapshot. The win is that N pools sharing a target cost *one* GitHub
listing per interval instead of one per pool per tick, and a slow GitHub can no
longer stretch a reconcile tick.

Failure policy is deliberately asymmetric:

* A failed poll **keeps the last good snapshot** rather than clearing it, so a
  transient blip doesn't stall reconciliation.
* But a snapshot is only *usable* while it is fresh — the controller rejects one
  older than its `runner_snapshot_max_age` and fail-safes the tick. That preserves
  today's "GitHub is down ⇒ take no action" safety without making every hiccup a
  stall.

Everything here is confined to the single event loop (poller task writes,
reconcile tasks read), so no locking is required.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from husk.aio import sleep_or_stop
from husk.slot import Runner
from husk.target import Target

log = logging.getLogger("husk.poller")

RunnerLister = Callable[[], Awaitable[list[Runner]]]


class SnapshotRegistry:
    """In-memory GitHub runner snapshots, keyed by target.

    Holds the *GitHub-side* view (what runners exist and whether they're busy).
    Distinct from `ControllerState`, which is husk's own published per-pool
    snapshot of classified slots."""

    def __init__(self) -> None:
        self._by_key: dict[str, tuple[list[Runner], float]] = {}

    def publish_runners(
        self, target: Target, runners: list[Runner], *, epoch: float | None = None
    ) -> None:
        self._by_key[target.key] = (
            list(runners),
            time.time() if epoch is None else epoch,
        )

    def runners(self, target: Target) -> list[Runner] | None:
        """The last published runner listing, or None if this target has never
        been polled successfully. None is "unknown", never "no runners"."""
        entry = self._by_key.get(target.key)
        return list(entry[0]) if entry is not None else None

    def age(self, target: Target, *, now: float | None = None) -> float | None:
        """Seconds since this target's snapshot was published (None if never)."""
        entry = self._by_key.get(target.key)
        if entry is None:
            return None
        return max(0.0, (time.time() if now is None else now) - entry[1])

    def forget(self, target: Target) -> None:
        """Drop a target's snapshot when it stops being served, so a re-added
        target starts from "never polled" rather than a stale listing."""
        self._by_key.pop(target.key, None)


class RunnerPoller:
    """Polls every target's runner listing into the registry on one cadence."""

    def __init__(
        self,
        registry: SnapshotRegistry,
        listers: dict[Target, RunnerLister],
        *,
        interval: float,
    ) -> None:
        self._registry = registry
        self._listers = dict(listers)
        self._interval = interval

    @property
    def targets(self) -> list[Target]:
        return list(self._listers)

    def add_target(self, target: Target, lister: RunnerLister) -> None:
        """Start polling a newly discovered target (no-op if already polled — N
        pools share one target's listing)."""
        if target not in self._listers:
            self._listers[target] = lister
            log.info("runner poller now tracking %s", target)

    def remove_target(self, target: Target) -> None:
        """Stop polling a target that is no longer served."""
        if self._listers.pop(target, None) is not None:
            log.info("runner poller dropped %s", target)

    async def poll_once(self) -> None:
        """One pass over every target. Never raises: a target that fails is logged
        and skipped, leaving its previous snapshot (and its age) in place."""
        # Snapshot the mapping: discovery can add/remove targets while this pass
        # awaits, and mutating a dict mid-iteration would raise.
        for target, lister in list(self._listers.items()):
            try:
                runners = await lister()
            except Exception:
                log.warning(
                    "runner poll failed for %s; keeping last snapshot",
                    target,
                    exc_info=True,
                )
                continue
            self._registry.publish_runners(target, runners)
            log.debug("polled %s -> %d runner(s)", target, len(runners))

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set. One task for all targets."""
        log.info(
            "runner poller up: %d target(s) every %.0fs: %s",
            len(self._listers),
            self._interval,
            ", ".join(str(t) for t in self._listers),
        )
        while not stop.is_set():
            await self.poll_once()
            await sleep_or_stop(stop, self._interval)
        log.info("runner poller stopped")
