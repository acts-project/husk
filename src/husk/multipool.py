"""Multi-pool orchestration — one huskd process driving N backend pools.

A thin facade over N validated `Controller` instances (one per pool). The
`Controller` is unchanged: each owns its backend + GitHub client + per-slot
bookkeeping (keyed by globally-unique `slot.id`), so the pools share nothing
implicitly. The facade only owns what is genuinely process-wide: the reconcile
loop and the per-tick config reload. Snapshots stay in memory on each Controller
(`snapshots()` gathers them); the HTTP layer reads that provider directly.

Pools tick **sequentially** on their own `poll_interval_sec` cadence (one thread,
no cross-pool shared state to reason about). Each pool's `tick()` is wrapped so an
unexpected raise in one pool can neither skip the others — the same per-tick
safety `Controller.run()` gives a single pool today. `run()` is driven on a
background thread by the CLI (the event loop owns the main thread); a `stop()`
event makes the loop's sleep interruptible so shutdown is prompt.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Callable

from husk.config import Config
from husk.controller import Controller
from husk.snapshot import ControllerState

log = logging.getLogger("husk.multipool")


class MultiPoolController:
    def __init__(
        self,
        controllers: list[Controller],
        *,
        reload_configs: Callable[[], list[Config] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not controllers:
            raise ValueError("MultiPoolController needs at least one pool")
        self._controllers = controllers
        self._reload = reload_configs
        self._clock = clock
        self._stop = threading.Event()
        # Per-pool next-due timestamp (monotonic) so each pool keeps its own
        # cadence within the single loop.
        self._next_due: dict[str, float] = {self._name(c): 0.0 for c in controllers}

    @staticmethod
    def _name(ctrl: Controller) -> str:
        return ctrl.cfg.backend.name

    @property
    def controllers(self) -> list[Controller]:
        return self._controllers

    def snapshots(self) -> list[ControllerState]:
        """The current per-pool snapshots (one per pool; the in-memory HTTP source).
        Read cross-thread by the HTTP layer — each `c.snapshot` is an immutable
        frozen dataclass swapped atomically per tick, so this is safe."""
        return [c.snapshot for c in self._controllers if c.snapshot is not None]

    # ------------------------------------------------------------------- run
    def run(self) -> None:
        """Blocking multi-pool reconcile loop, run on a background thread by the
        CLI. Loops until `stop()` is set; the sleep is interruptible so shutdown
        is prompt. Caller holds the single process lock."""
        log.info(
            "huskd starting: %d pool(s): %s",
            len(self._controllers),
            ", ".join(self._name(c) for c in self._controllers),
        )
        while not self._stop.is_set():
            self._maybe_reload()
            now = self._clock()
            for c in self._controllers:
                name = self._name(c)
                if now >= self._next_due[name]:
                    self._tick_one(c)
                    self._next_due[name] = (
                        self._clock() + c.cfg.timeouts.poll_interval_sec
                    )
            soonest = min(self._next_due.values())
            self._stop.wait(timeout=max(0.0, soonest - self._clock()))

    def stop(self) -> None:
        """Signal `run()` to exit at the next loop boundary (wakes the sleep)."""
        self._stop.set()

    def tick_all(self) -> None:
        """Tick every pool once (the `--once` path / tests)."""
        for c in self._controllers:
            self._tick_one(c)

    def _tick_one(self, ctrl: Controller) -> None:
        try:
            ctrl.tick()
        except Exception:
            # A pool's tick should be self-contained (list failures fail safe
            # inside tick), but never let an unexpected raise stop the other pools.
            log.exception(
                "pool %s: tick raised; other pools continue", self._name(ctrl)
            )

    # ---------------------------------------------------------------- reload
    def _maybe_reload(self) -> None:
        """Hot-reload each pool's knobs from the config file (mtime-guarded by the
        loader). Pools are matched by `backend.name`; adding/removing a pool needs
        a restart (warned, ignored)."""
        if self._reload is None:
            return
        try:
            new = self._reload()
        except Exception:
            log.warning("config reload failed; keeping current config", exc_info=True)
            return
        if not new:
            return
        by_name = {self._name(c): c for c in self._controllers}
        new_names = {cfg.backend.name for cfg in new}
        if new_names != set(by_name):
            log.warning(
                "pool set changed (%s -> %s); restart huskd to add/remove pools",
                sorted(by_name),
                sorted(new_names),
            )
        for cfg in new:
            ctrl = by_name.get(cfg.backend.name)
            if ctrl is None:
                continue  # a new pool — restart-only (warned above)
            # Normalize the shared sections to what the sub-controller was built
            # with: the facade owns http_addr (blanked on the sub-controller), so
            # feeding the file's real value would spuriously trip
            # apply_reloaded_config's structural-change warning every reload. Keep
            # the *new* hot knobs (controller.shrink_ticks) by blanking only the
            # facade-owned field, and reuse the shared github object as-is.
            norm = dataclasses.replace(
                cfg,
                controller=dataclasses.replace(cfg.controller, http_addr=""),
                github=ctrl.cfg.github,
            )
            ctrl.apply_reloaded_config(norm)
