"""Multi-pool orchestration — one huskd process driving N backend pools.

A thin facade over N validated `Controller` instances (one per pool). The
`Controller` is unchanged in spirit: each owns its backend + GitHub client +
per-slot bookkeeping (keyed by globally-unique `slot.id`), so the pools share
nothing implicitly. The facade only owns what is genuinely process-wide: the
reconcile tasks. Snapshots stay in memory on each Controller (`snapshots()`
gathers them); the HTTP layer reads that provider directly.

Every pool is known at startup — each `[[pool]]` names the one target it serves —
so the only thing that moves at runtime is whether that target is *servable*: is
the App installed on it, and (for a repo target) did that install grant the repo?
A background task checks this and starts a pool's reconcile task when its target
becomes available, drains and stops it when it goes away.

Two safety rules keep a GitHub blip from tearing down live runners:

* an availability check that fails at all leaves the live set exactly as it was,
* and a *partial* sweep may only **enable** pools — absence from an incomplete
  result is not evidence of an uninstall (see `husk.discovery`).

Draining destroys only *idle* slots: a busy one is left running and retried next
sweep, so losing a target never kills an in-flight job. A target that comes back
mid-drain revives the same Controller with its slots intact.

Each pool runs its **own asyncio task** on its own `poll_interval_sec` cadence,
all on the single event loop that also serves HTTP and runs the centralized
`RunnerPoller`. A pool that stalls can neither delay another pool's ticks nor
freeze the snapshot the dashboard renders for it — provided its blocking work
stays off the loop, which is why every backend call inside `Controller.tick()`
goes through `asyncio.to_thread`. Each pool's `tick()` is wrapped so an unexpected
raise can't kill its task. A `stop()` event makes every loop's sleep interruptible
so shutdown is prompt.

The config file is read exactly once, at startup. There is no hot reload: changing
anything means restarting huskd, which is cheap (no state file — slots are
re-adopted from backend metadata on the first tick) and means the running config
is always exactly what the file says, with no half-applied hybrid.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from husk.aio import sleep_or_stop
from husk.controller import Controller
from husk.discovery import Discovery
from husk.snapshot import ControllerState
from husk.target import Target

log = logging.getLogger("husk.multipool")

# How often to re-run target discovery. Installs change on human timescales, and
# each sweep costs an API call per installation, so this is deliberately slow
# relative to the reconcile cadence.
_DISCOVERY_INTERVAL_S = 60.0


class MultiPoolController:
    def __init__(
        self,
        controllers: list[Controller],
        *,
        discover: Callable[[], Awaitable[Discovery]] | None = None,
        attach: Callable[[Controller], None] | None = None,
        detach: Callable[[Target], None] | None = None,
        discovery_interval: float = _DISCOVERY_INTERVAL_S,
    ) -> None:
        if not controllers:
            raise ValueError("MultiPoolController needs at least one pool")
        # Every pool is known at startup (each names its target in config); the
        # only thing that moves at runtime is whether its target is *servable*.
        self._all = list(controllers)
        self._controllers: list[Controller] = [] if discover else list(controllers)
        self._discover = discover
        self._attach = attach
        self._detach = detach
        self._discovery_interval = discovery_interval
        self._stop: asyncio.Event | None = None
        # Live reconcile tasks, keyed "<target.key>/<pool>", and pools whose target
        # went away but which still own slots (drained over subsequent rounds).
        self._tasks: dict[str, asyncio.Task] = {}
        self._draining: dict[str, Controller] = {}

    @staticmethod
    def _name(ctrl: Controller) -> str:
        return ctrl.cfg.backend.name

    @staticmethod
    def _unit(ctrl: Controller) -> str:
        return f"{ctrl.target.key}/{ctrl.cfg.backend.name}"

    @property
    def controllers(self) -> list[Controller]:
        return self._controllers

    def console_output(self, backend: str, slot_id: str) -> str | None:
        """A slot's serial console, by pool name and slot id — the read behind
        `/slot/<backend>/<slot>/console`. Returns None when the pool is unknown or
        the backend has no console for it; the seam is best-effort by contract
        (husk.backend.Backend.console_output) and never raises."""
        ctrl = next(
            (c for c in self._controllers if c.cfg.backend.name == backend), None
        )
        if ctrl is None:
            return None
        # Re-list rather than reuse the reconcile tick's slots: this is an
        # on-demand debug read, so one extra API call is cheaper than holding
        # backend Slot objects alive between ticks just for it.
        slot = next((s for s in ctrl.backend.list_slots() if s.id == slot_id), None)
        if slot is None:
            return None
        return ctrl.backend.console_output(slot)

    def snapshots(self) -> list[ControllerState]:
        """The current per-pool snapshots (one per pool; the in-memory HTTP source).
        Each `c.snapshot` is an immutable frozen dataclass swapped atomically per
        tick, so a reader never sees a half-built state."""
        return [c.snapshot for c in self._controllers if c.snapshot is not None]

    # ------------------------------------------------------------------- run
    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Drive every pool until `stop` is set. Runs on the caller's event loop.

        Spawns one reconcile task per pool (plus the discovery loop), then awaits
        them. The caller holds the single process lock for this coroutine's
        lifetime."""
        self._stop = stop or asyncio.Event()
        log.info(
            "huskd starting: %d pool(s) configured: %s",
            len(self._all),
            ", ".join(self._unit(c) for c in self._all),
        )
        for c in self._controllers:
            self._spawn(c)
        # Waiting on `stop` is what holds run() open: the pool tasks come and go
        # under discovery, and discovery itself is optional, so neither can be the
        # thing we await.
        own: list[asyncio.Task] = [
            asyncio.create_task(self._stop.wait(), name="husk-stop")
        ]
        if self._discover is not None:
            own.append(
                asyncio.create_task(self._discovery_loop(), name="husk-discovery")
            )
        try:
            # Wait on the loop's own long-lived tasks and let `stop` end the pool
            # tasks alongside them.
            await asyncio.gather(*own)
        finally:
            for t in [*own, *self._tasks.values()]:
                t.cancel()
            await asyncio.gather(*own, *self._tasks.values(), return_exceptions=True)

    def _spawn(self, ctrl: Controller) -> None:
        """Start one pool's reconcile task.

        A no-op before `run()`: the `--once` path checks availability with no
        reconcile loop to attach pools to, and `run()` spawns whatever it finds
        already enabled."""
        if self._stop is None:
            return
        unit = self._unit(ctrl)
        self._tasks[unit] = asyncio.create_task(
            self._pool_loop(ctrl), name=f"husk-pool-{unit}"
        )

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

    # ------------------------------------------------------------- discovery
    async def _discovery_loop(self) -> None:
        """Keep the live pool set in step with which targets are servable."""
        assert self._stop is not None
        try:
            while not self._stop.is_set():
                await self.discover_once()
                await sleep_or_stop(self._stop, self._discovery_interval)
        except asyncio.CancelledError:
            pass
        log.info("discovery task stopped")

    async def discover_once(self) -> None:
        """One sweep: enable pools whose target became servable, drain the rest.

        Never raises — a failed sweep leaves the live set untouched, which is the
        whole point: huskd must not drain live runners because GitHub 500'd."""
        assert self._discover is not None
        try:
            result = await self._discover()
        except Exception:
            log.warning(
                "target availability check failed; keeping the current %d pool(s)",
                len(self._controllers),
                exc_info=True,
            )
            await self._drain_step()
            return

        available = {t.key for t in result.targets}
        for ctrl in self._all:
            live = ctrl in self._controllers
            if ctrl.target.key in available:
                if not live:
                    self._enable(ctrl)
            elif live and result.complete:
                self._begin_drain(ctrl)
            elif live:
                # Absence in a partial sweep proves nothing; say so once per sweep
                # so a persistently-degraded check stays visible.
                log.info(
                    "partial sweep: not draining %s (absence is not evidence)",
                    self._unit(ctrl),
                )
        await self._drain_step()

    def _enable(self, ctrl: Controller) -> None:
        """Start reconciling a pool whose target is servable."""
        # A pool that comes back mid-drain keeps its slots — re-adopting beats
        # destroying and rebuilding them.
        self._draining.pop(self._unit(ctrl), None)
        self._controllers.append(ctrl)
        if self._attach is not None:
            self._attach(ctrl)
        self._spawn(ctrl)
        log.info(
            "pool %s: target %s available; reconciling", self._name(ctrl), ctrl.target
        )

    def _begin_drain(self, ctrl: Controller) -> None:
        """Stop reconciling a pool and move it into the drain set."""
        self._controllers[:] = [c for c in self._controllers if c is not ctrl]
        task = self._tasks.pop(self._unit(ctrl), None)
        if task is not None:
            task.cancel()
        # Only detach the poller once no live pool still serves this target.
        if self._detach is not None and not any(
            c.target == ctrl.target for c in self._controllers
        ):
            self._detach(ctrl.target)
        self._draining[self._unit(ctrl)] = ctrl
        log.warning(
            "pool %s: target %s no longer servable; stopped reconciling, draining",
            self._name(ctrl),
            ctrl.target,
        )

    async def _drain_step(self) -> None:
        """Tear down one round of each draining pool's slots.

        Busy slots are deliberately left running: losing a target should not kill
        someone's in-flight job. They are retried each sweep, so the drain
        completes once the work does."""
        for unit, ctrl in list(self._draining.items()):
            remaining = await self._drain_one(ctrl)
            if remaining == 0:
                self._draining.pop(unit, None)
                await self._safe_close(ctrl)
                log.info("pool %s fully drained", unit)
            else:
                log.info("pool %s: %d slot(s) still draining (busy)", unit, remaining)

    async def _drain_one(self, ctrl: Controller) -> int:
        """Destroy this pool's idle slots; return how many are still left."""
        from husk.slot import match_runner

        try:
            slots = await asyncio.to_thread(ctrl.backend.list_slots)
        except Exception:
            log.warning(
                "drain %s: cannot list slots; retrying next sweep",
                self._unit(ctrl),
                exc_info=True,
            )
            return 1  # unknown ⇒ assume work remains, so the drain is retried
        if not slots:
            return 0
        # Busy detection is best-effort. If the App was uninstalled the listing
        # fails — and so does the runner's own connection to GitHub, so treating
        # "can't tell" as "not busy" here does not kill a job that is still alive.
        try:
            runners = await ctrl.github.list_runners()
        except Exception:
            log.warning(
                "drain %s: no runner listing; treating all slots as idle",
                self._unit(ctrl),
                exc_info=True,
            )
            runners = []

        remaining = 0
        for s in slots:
            r = match_runner(runners, s)
            if r is not None and r.online and r.busy:
                remaining += 1
                continue
            if r is not None:
                try:
                    await ctrl.github.delete_runner(r.id)
                except Exception:
                    log.warning(
                        "drain %s: could not deregister runner %s",
                        self._unit(ctrl),
                        r.name,
                        exc_info=True,
                    )
            try:
                await asyncio.to_thread(
                    ctrl.backend.destroy_slot, s, reason="target no longer served"
                )
            except Exception:
                log.warning(
                    "drain %s: could not destroy slot %s; retrying next sweep",
                    self._unit(ctrl),
                    s.name,
                    exc_info=True,
                )
                remaining += 1
        return remaining

    async def _safe_close(self, ctrl: Controller) -> None:
        try:
            await ctrl.github.aclose()
        except Exception:
            log.debug(
                "closing github client for %s failed", self._unit(ctrl), exc_info=True
            )

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
