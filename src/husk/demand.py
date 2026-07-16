"""Demand-signal seam — the in-memory registry reconcile reads `desired` from.

The reconcile loop no longer folds an inline GitHub call straight into its sizing
math; it *publishes* the demand it observes for a `(target, pool)` and reads
`desired(target, pool)` back through this registry. The indirection is the seam
that lets the producer move without the consumer changing:

- Phase 0 (now): a single inline producer — the same poll, behind the interface —
  so publish and read happen in one tick and the value is identical to before.
- Phase 1: one centralized async poller fills the registry; reconcile only reads.
- Phase 4: a webhook handler becomes a *second* producer nudging the same map.

Keyed by ``(target.key, pool)`` so it is producer-agnostic and safe to share
across pools/threads once the centralized poller lands.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from husk.target import Target


@dataclass(frozen=True)
class Demand:
    """One `(target, pool)`'s demand signal as last observed."""

    busy: int  # runners currently running a job — the external load signal
    desired: int  # slots we want: min(max_total, busy + min_ready)
    epoch: float  # wall-clock of the observation


class DemandRegistry:
    """Thread-safe map of `(target, pool)` → `Demand`.

    Reconcile is the only reader. Today it is also the only writer (inline, from
    the slots it just classified); once the centralized poller lands, production
    moves off the reconcile path and this class is the hand-off point. The lock is
    cheap and makes it correct to share the instance the moment that happens."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_key: dict[tuple[str, str], Demand] = {}

    def publish(
        self,
        target: Target,
        pool: str,
        *,
        busy: int,
        desired: int,
        epoch: float | None = None,
    ) -> Demand:
        d = Demand(
            busy=busy,
            desired=desired,
            epoch=time.time() if epoch is None else epoch,
        )
        with self._lock:
            self._by_key[(target.key, pool)] = d
        return d

    def get(self, target: Target, pool: str) -> Demand | None:
        with self._lock:
            return self._by_key.get((target.key, pool))

    def desired(self, target: Target, pool: str) -> int | None:
        """The wanted slot count for `(target, pool)`, or None if never published."""
        d = self.get(target, pool)
        return d.desired if d is not None else None
