"""Multi-pool orchestration — one huskd process driving N backend pools.

A thin facade over N validated `Controller` instances (one per pool). The
`Controller` is unchanged in spirit: each owns its backend + GitHub client +
per-slot bookkeeping (keyed by globally-unique `slot.id`), so the pools share
nothing implicitly. The facade only owns what is genuinely process-wide: the
reconcile tasks and the config reload. Snapshots stay in memory on each Controller
(`snapshots()` gathers them); the HTTP layer reads that provider directly.

Each pool runs its **own asyncio task** on its own `poll_interval_sec` cadence,
all on the single event loop that also serves HTTP and runs the centralized
`RunnerPoller`. A pool that stalls can neither delay another pool's ticks nor
freeze the snapshot the dashboard renders for it — provided its blocking work
stays off the loop, which is why every backend call inside `Controller.tick()`
goes through `asyncio.to_thread`. Each pool's `tick()` is wrapped so an unexpected
raise can't kill its task. The config reload runs as one more task (reading the
file once and dispatching hot knobs to every pool) rather than N re-parses, and
does its file I/O in a thread. A `stop()` event makes every loop's sleep
interruptible so shutdown is prompt.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Callable

from husk.aio import sleep_or_stop
from husk.config import Config
from husk.controller import Controller
from husk.snapshot import ControllerState

log = logging.getLogger("husk.multipool")

# How often the reload task re-checks the config file for hot-reloadable knobs.
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
        self._stop: asyncio.Event | None = None

    @staticmethod
    def _name(ctrl: Controller) -> str:
        return ctrl.cfg.backend.name

    @property
    def controllers(self) -> list[Controller]:
        return self._controllers

    def snapshots(self) -> list[ControllerState]:
        """The current per-pool snapshots (one per pool; the in-memory HTTP source).
        Each `c.snapshot` is an immutable frozen dataclass swapped atomically per
        tick, so a reader never sees a half-built state."""
        return [c.snapshot for c in self._controllers if c.snapshot is not None]

    # ------------------------------------------------------------------- run
    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Drive every pool until `stop` is set. Runs on the caller's event loop.

        Spawns one reconcile task per pool plus the reload task, then awaits them.
        The caller holds the single process lock for this coroutine's lifetime."""
        self._stop = stop or asyncio.Event()
        log.info(
            "huskd starting: %d pool(s): %s",
            len(self._controllers),
            ", ".join(self._name(c) for c in self._controllers),
        )
        tasks = [
            asyncio.create_task(self._pool_loop(c), name=f"husk-pool-{self._name(c)}")
            for c in self._controllers
        ]
        tasks.append(asyncio.create_task(self._reload_loop(), name="husk-reload"))
        try:
            await asyncio.gather(*tasks)
        finally:
            # Stop is normally already set; cancel covers an early raise so no task
            # is orphaned on the loop.
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _pool_loop(self, ctrl: Controller) -> None:
        """One pool's reconcile loop: tick, then sleep its own `poll_interval_sec`
        (interruptibly). Isolated on its own task so a stall here can't delay any
        other pool or freeze their snapshots."""
        assert self._stop is not None
        name = self._name(ctrl)
        log.info("pool %s: reconcile task up", name)
        try:
            while not self._stop.is_set():
                await self._tick_one(ctrl)
                await sleep_or_stop(self._stop, ctrl.cfg.timeouts.poll_interval_sec)
        except asyncio.CancelledError:
            pass
        log.info("pool %s: reconcile task stopped", name)

    async def _reload_loop(self) -> None:
        """Re-read the config file on a fixed cadence, off the event loop."""
        assert self._stop is not None
        try:
            while not self._stop.is_set():
                await asyncio.to_thread(self._maybe_reload)
                await sleep_or_stop(self._stop, _RELOAD_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        """Signal `run()` and every pool loop to exit (wakes their sleeps)."""
        if self._stop is not None:
            self._stop.set()

    async def tick_all(self) -> None:
        """Tick every pool once (the `--once` path / tests)."""
        for c in self._controllers:
            await self._tick_one(c)

    async def _tick_one(self, ctrl: Controller) -> None:
        try:
            await ctrl.tick()
        except asyncio.CancelledError:
            raise
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
