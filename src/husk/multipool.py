"""Multi-pool orchestration — one huskd process driving N backend pools.

A thin facade over N validated `Controller` instances (one per pool). The
`Controller` is unchanged: each owns its backend + GitHub client + per-slot
bookkeeping (keyed by globally-unique `slot.id`), so the pools share nothing
implicitly. The facade only owns what is genuinely process-wide: the reconcile
threads and the config reload. Snapshots stay in memory on each Controller
(`snapshots()` gathers them); the HTTP layer reads that provider directly.

Each pool runs its **own** reconcile thread on its own `poll_interval_sec`
cadence, so a pool that stalls (a wedged libvirt host, a slow cloud) can neither
delay another pool's ticks nor freeze the snapshot the dashboard renders for it.
Each pool's `tick()` is wrapped so an unexpected raise can't kill its thread. A
single config-reload runs on the caller's thread (`run()`) — reading the file
once and dispatching hot knobs to every pool — so N threads don't each re-parse
it. `run()` is driven on a background thread by the CLI (the event loop owns the
main thread); a `stop()` event makes every loop's sleep interruptible so shutdown
is prompt."""

from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Callable

from husk.config import Config
from husk.controller import Controller
from husk.snapshot import ControllerState

log = logging.getLogger("husk.multipool")

# How often the caller thread re-checks the config file for hot-reloadable knobs.
# Cheap (an mtime stat), so a small fixed cadence keeps edits picked up promptly
# without coupling to any pool's poll interval.
_RELOAD_INTERVAL_S = 5.0


class MultiPoolController:
    def __init__(
        self,
        controllers: list[Controller],
        *,
        reload_configs: Callable[[], list[Config] | None] | None = None,
    ) -> None:
        if not controllers:
            raise ValueError("MultiPoolController needs at least one pool")
        self._controllers = controllers
        self._reload = reload_configs
        self._stop = threading.Event()

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
        """Blocking multi-pool driver, run on a background thread by the CLI. Spawns
        one reconcile thread per pool, then runs the config-reload loop on this
        thread until `stop()` is set; on stop, signals the pool threads and joins
        them (best-effort — they are daemons, so a pool stuck mid-tick never blocks
        process exit). Caller holds the single process lock."""
        log.info(
            "huskd starting: %d pool(s): %s",
            len(self._controllers),
            ", ".join(self._name(c) for c in self._controllers),
        )
        threads = [
            threading.Thread(
                target=self._pool_loop,
                args=(c,),
                name=f"husk-pool-{self._name(c)}",
                daemon=True,
            )
            for c in self._controllers
        ]
        for t in threads:
            t.start()
        # Reload runs here (one reader for all pools); it also keeps run() blocking
        # until stop, holding the process lock for the daemon's lifetime.
        while not self._stop.is_set():
            self._maybe_reload()
            self._stop.wait(timeout=_RELOAD_INTERVAL_S)
        for t in threads:
            t.join(timeout=_RELOAD_INTERVAL_S)

    def _pool_loop(self, ctrl: Controller) -> None:
        """One pool's reconcile loop: tick, then sleep its own `poll_interval_sec`
        (interruptibly). Isolated on its own thread so a stall here can't delay any
        other pool or freeze their snapshots."""
        name = self._name(ctrl)
        log.info("pool %s: reconcile thread up", name)
        while not self._stop.is_set():
            self._tick_one(ctrl)
            self._stop.wait(timeout=ctrl.cfg.timeouts.poll_interval_sec)
        log.info("pool %s: reconcile thread stopped", name)

    def stop(self) -> None:
        """Signal `run()` and every pool loop to exit (wakes their sleeps)."""
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
