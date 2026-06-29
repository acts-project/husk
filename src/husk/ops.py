"""In-process async-operation registry — keyed single-flight over background work.

The reconcile loop is level-triggered and must never block on the slow, one-shot
work that image delivery needs (oras pull, multi-GB Glance upload / host scp).
This is the seam that holds that work: `submit(key, …)` runs a function off the
tick thread exactly once per key (single-flight), and `view(key)`/`views()` let
the tick — and the dashboard — read its PENDING/DONE/FAILED state plus a free-text
progress line, instead of re-deriving it from flags scattered across the backend.

Retry is delegated to `tenacity`: a transient failure is retried in-worker with
exponential backoff + jitter (the op stays PENDING, so the controller keeps
deferring gracefully); a `tenacity` give-up — or a non-retryable `OpAbort` — marks
the op FAILED, and a later `submit` of the same key restarts it after a cooldown.

Deliberately in-process and non-durable: husk's image staging is idempotent and
content-addressed, so a restart safely re-derives state and re-submits. When
cross-restart durability or cron scheduling is actually needed, an APScheduler /
Huey jobstore would slot in *here* — not in the control loop. Workers are daemon
threads so a SIGTERM never blocks shutdown on an in-flight multi-GB transfer
(abandoning it is safe: the next run resumes from the content-addressed cache).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from tenacity import (
    Retrying,
    retry_if_not_exception_type,
    stop_after_delay,
    wait_random_exponential,
)

log = logging.getLogger("husk.ops")

# Op lifecycle states (also the wire values the dashboard renders).
PENDING = "pending"
DONE = "done"
FAILED = "failed"

# A FAILED op is only restarted by a later submit() once this cooldown has passed,
# so a persistent outage doesn't spin a tight retry loop across ticks.
_RETRY_AFTER_S = 30.0
# How long a DONE op lingers in views() (for the dashboard) before it is pruned.
_DONE_TTL_S = 180.0
# tenacity in-worker retry: cap on a single backoff wait, and on total retry time
# before the op gives up and surfaces FAILED.
_RETRY_MAX_WAIT_S = 60.0
_RETRY_GIVEUP_S = 600.0

# A function submitted to the store. It receives a `report` callable it can use to
# publish a human-readable progress line, and returns the result the caller adopts.
OpFn = Callable[[Callable[[str], None]], Any]


class OpAbort(Exception):
    """Raised by an op's work function to fail immediately without retrying — for a
    permanent error (e.g. a malformed artifact) where retrying is pointless."""


@dataclass(frozen=True)
class OpView:
    """A flat, JSON-serializable snapshot of one op — what the dashboard renders.
    Carries no result payload (use `OpStore.result` to adopt a DONE op)."""

    key: str
    kind: str
    state: str  # PENDING | DONE | FAILED
    progress: str | None
    error: str | None
    started_at: float  # epoch of the current attempt's submit
    updated_at: float  # epoch of the last state/progress change
    attempts: int


class _Op:
    """Mutable internal record for one keyed operation."""

    __slots__ = (
        "key",
        "kind",
        "state",
        "progress",
        "error",
        "result",
        "started_at",
        "updated_at",
        "failed_mono",
        "attempts",
    )

    def __init__(self, key: str, kind: str, now: float) -> None:
        self.key = key
        self.kind = kind
        self.state = PENDING
        self.progress: str | None = None
        self.error: str | None = None
        self.result: Any = None
        self.started_at = now
        self.updated_at = now
        self.failed_mono: float | None = None
        self.attempts = 0


class OpStore:
    """Keyed single-flight registry of background operations. Thread-safe; the tick
    thread submits/reads, daemon workers run the functions."""

    def __init__(
        self,
        *,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        retry_after_s: float = _RETRY_AFTER_S,
        done_ttl_s: float = _DONE_TTL_S,
        retry_giveup_s: float = _RETRY_GIVEUP_S,
        retry_max_wait_s: float = _RETRY_MAX_WAIT_S,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ops: dict[str, _Op] = {}
        self._lock = threading.Lock()
        self._spawn = spawn or self._spawn_daemon
        self._retry_after = retry_after_s
        self._done_ttl = done_ttl_s
        self._retry_giveup = retry_giveup_s
        self._retry_max_wait = retry_max_wait_s
        self._now = clock
        self._mono = monotonic

    @staticmethod
    def _spawn_daemon(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="husk-op", daemon=True).start()

    def submit(self, key: str, kind: str, fn: OpFn) -> OpView:
        """Ensure the op for `key` is running, returning its current view. A PENDING
        or DONE op is left as-is (single-flight); a FAILED op is restarted only once
        its cooldown has elapsed. The first submit of a key kicks off the worker."""
        with self._lock:
            op = self._ops.get(key)
            if op is not None and (
                op.state in (PENDING, DONE)
                or (
                    op.failed_mono is not None
                    and self._mono() - op.failed_mono < self._retry_after
                )
            ):
                return self._view(op)
            op = _Op(key, kind, self._now())
            self._ops[key] = op
        log.info("op %s (%s) starting", key, kind)
        self._spawn(lambda: self._run(op, fn))
        with self._lock:
            return self._view(op)

    def result(self, key: str) -> Any:
        """The result of a DONE op; raises KeyError if absent or not DONE."""
        with self._lock:
            op = self._ops.get(key)
            if op is None or op.state != DONE:
                raise KeyError(key)
            return op.result

    def view(self, key: str) -> OpView | None:
        with self._lock:
            op = self._ops.get(key)
            return self._view(op) if op is not None else None

    def views(self) -> list[OpView]:
        """All current op views (in-flight, failed, and recently-done), pruning DONE
        ops past their TTL so the list reflects activity rather than all history."""
        cutoff = self._now() - self._done_ttl
        out: list[OpView] = []
        with self._lock:
            for key in list(self._ops):
                op = self._ops[key]
                if op.state == DONE and op.updated_at < cutoff:
                    del self._ops[key]
                    continue
                out.append(self._view(op))
        return out

    # ----------------------------------------------------------------- worker
    def _run(self, op: _Op, fn: OpFn) -> None:
        def report(msg: str) -> None:
            with self._lock:
                op.progress = msg
                op.updated_at = self._now()
            log.info("op %s: %s", op.key, msg)

        try:
            result = self._with_retry(op, fn, report)
        except BaseException as e:  # noqa: BLE001 — a worker must never escape
            with self._lock:
                op.state = FAILED
                op.error = f"{type(e).__name__}: {e}"
                op.progress = None
                op.updated_at = self._now()
                op.failed_mono = self._mono()
            log.warning("op %s failed: %s", op.key, e, exc_info=True)
            return
        with self._lock:
            op.state = DONE
            op.result = result
            op.error = None
            op.progress = None
            op.updated_at = self._now()
        log.info("op %s done", op.key)

    def _with_retry(self, op: _Op, fn: OpFn, report: Callable[[str], None]) -> Any:
        """Run `fn` under tenacity: transient errors back off and retry in-worker
        (op stays PENDING); an `OpAbort` or the give-up deadline propagates → FAILED."""
        for attempt in Retrying(
            retry=retry_if_not_exception_type(OpAbort),
            wait=wait_random_exponential(multiplier=1, max=self._retry_max_wait),
            stop=stop_after_delay(self._retry_giveup),
            reraise=True,
        ):
            with attempt:
                with self._lock:
                    op.attempts += 1
                    op.updated_at = self._now()
                    n = op.attempts
                if n > 1:
                    report(f"retrying (attempt {n}) after error")
                return fn(report)

    def _view(self, op: _Op) -> OpView:
        return OpView(
            key=op.key,
            kind=op.kind,
            state=op.state,
            progress=op.progress,
            error=op.error,
            started_at=op.started_at,
            updated_at=op.updated_at,
            attempts=op.attempts,
        )
