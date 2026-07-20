"""Multi-pool orchestration — one huskd process driving N backend pools.

A thin facade over N validated `Controller` instances (one per pool). The
`Controller` is unchanged in spirit: each owns its backend + GitHub client +
per-slot bookkeeping (keyed by globally-unique `slot.id`), so the pools share
nothing implicitly. The facade only owns what is genuinely process-wide: the
reconcile tasks and the config reload. Snapshots stay in memory on each Controller
(`snapshots()` gathers them); the HTTP layer reads that provider directly.

The reconcile unit is `(target, pool)`, and the target set is **dynamic**: a
discovery task polls installations ∩ allowlist and spawns a reconcile task when a
target appears, drains and stops it when one goes away. Two safety rules keep a
GitHub blip from tearing down live runners:

* discovery failing at all leaves the target set exactly as it was, and
* a *partial* sweep may only **add** targets — absence from an incomplete result
  is not evidence of removal (see `husk.discovery`).

Removal drains rather than destroys: a busy slot is left alone and retried on the
next round, so a de-allowlisted target loses its runners only once their jobs
finish.

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
from typing import Awaitable, Callable

from husk.aio import sleep_or_stop
from husk.config import Config
from husk.controller import Controller
from husk.discovery import Discovery
from husk.snapshot import ControllerState
from husk.target import Target

log = logging.getLogger("husk.multipool")

# How often the reload task re-checks the config file for hot-reloadable knobs.
# Cheap (an mtime stat), so a small fixed cadence keeps edits picked up promptly
# without coupling to any pool's poll interval.
_RELOAD_INTERVAL_S = 5.0

# How often to re-run target discovery. Installs change on human timescales, and
# each sweep costs an API call per installation, so this is deliberately slow
# relative to the reconcile cadence.
_DISCOVERY_INTERVAL_S = 60.0


class MultiPoolController:
    def __init__(
        self,
        controllers: list[Controller],
        *,
        reload_configs: Callable[[], list[Config] | None] | None = None,
        discover: Callable[[], Awaitable[Discovery]] | None = None,
        build: Callable[[Target], list[Controller]] | None = None,
        attach: Callable[[Controller], None] | None = None,
        detach: Callable[[Target], None] | None = None,
        discovery_interval: float = _DISCOVERY_INTERVAL_S,
    ) -> None:
        if not controllers and discover is None:
            raise ValueError("MultiPoolController needs at least one pool")
        if discover is not None and build is None:
            raise ValueError("discovery needs a `build` to create a target's pools")
        self._controllers = controllers
        self._reload = reload_configs
        self._discover = discover
        self._build = build
        self._attach = attach
        self._detach = detach
        self._discovery_interval = discovery_interval
        self._stop: asyncio.Event | None = None
        # Live reconcile tasks, keyed "<target.key>/<pool>", and targets that have
        # been removed but still own slots (drained over subsequent rounds).
        self._tasks: dict[str, asyncio.Task] = {}
        self._draining: dict[str, list[Controller]] = {}

    @staticmethod
    def _name(ctrl: Controller) -> str:
        return ctrl.cfg.backend.name

    @staticmethod
    def _unit(ctrl: Controller) -> str:
        return f"{ctrl.target.key}/{ctrl.cfg.backend.name}"

    @staticmethod
    def _pool_key(name: str) -> str:
        """The *configured* pool name behind a possibly target-folded one.

        With >1 target a pool is named `gpu@acts-project` so snapshots and logs can
        tell the units apart, but the config file only ever knows `gpu` — so
        reload has to match on the base. `@` is rejected in configured pool names
        (see `load_configs`) to keep this unambiguous."""
        return name.split("@", 1)[0]

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
            "huskd starting: %d (target, pool) unit(s): %s",
            len(self._controllers),
            ", ".join(self._unit(c) for c in self._controllers)
            or "(awaiting discovery)",
        )
        for c in self._controllers:
            self._spawn(c)
        own = [asyncio.create_task(self._reload_loop(), name="husk-reload")]
        if self._discover is not None:
            own.append(
                asyncio.create_task(self._discovery_loop(), name="husk-discovery")
            )
        try:
            # The pool tasks come and go under discovery, so wait on the loop's own
            # long-lived tasks and let `stop` end the pool tasks alongside them.
            await asyncio.gather(*own)
        finally:
            for t in [*own, *self._tasks.values()]:
                t.cancel()
            await asyncio.gather(*own, *self._tasks.values(), return_exceptions=True)

    def _spawn(self, ctrl: Controller) -> None:
        """Start one `(target, pool)` reconcile task.

        A no-op before `run()`: the `--once` path discovers targets without a
        reconcile loop to attach them to, and `run()` spawns whatever it finds
        already registered."""
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
        """Keep the live `(target, pool)` set in step with installations ∩ allowlist."""
        assert self._stop is not None
        try:
            while not self._stop.is_set():
                await self.discover_once()
                await sleep_or_stop(self._stop, self._discovery_interval)
        except asyncio.CancelledError:
            pass
        log.info("discovery task stopped")

    async def discover_once(self) -> None:
        """One sweep: add targets that appeared, drain targets that went away.

        Never raises — a failed sweep leaves the current target set untouched,
        which is the whole point: huskd must not tear down live runners because
        GitHub returned a 500."""
        assert self._discover is not None
        try:
            result = await self._discover()
        except Exception:
            log.warning(
                "target discovery failed; keeping the current %d target(s)",
                len({c.target.key for c in self._controllers}),
                exc_info=True,
            )
            await self._drain_step()
            return

        wanted = {t.key: t for t in result.targets}
        have = {c.target.key for c in self._controllers}

        for key, target in wanted.items():
            if key not in have:
                self._add_target(target)

        if result.complete:
            for key in have - set(wanted):
                self._begin_drain(key)
        elif have - set(wanted):
            # Absence in a partial sweep proves nothing; say so once per sweep so a
            # persistently-degraded discovery is visible rather than silently
            # holding targets open.
            log.info(
                "partial discovery: not removing %s (absence is not evidence)",
                sorted(have - set(wanted)),
            )
        await self._drain_step()

    def _add_target(self, target: Target) -> None:
        """Build and start every pool for a newly discovered target."""
        # A target that reappears mid-drain is simply revived: its slots are still
        # there, so re-adopting them beats destroying and rebuilding.
        revived = self._draining.pop(target.key, None)
        if revived is not None:
            log.info("target %s came back during drain; resuming it", target)
            ctrls = revived
        else:
            assert self._build is not None
            try:
                ctrls = self._build(target)
            except Exception:
                # Retried on the next sweep — a target that can't be built (bad
                # backend, unreachable cloud) must not stop the others.
                log.error(
                    "could not build pools for %s; will retry", target, exc_info=True
                )
                return
            if not ctrls:
                return
        for c in ctrls:
            self._controllers.append(c)
            if self._attach is not None:
                self._attach(c)
            self._spawn(c)
        log.info(
            "target %s added: %d pool(s): %s",
            target,
            len(ctrls),
            ", ".join(self._name(c) for c in ctrls),
        )

    def _begin_drain(self, key: str) -> None:
        """Stop reconciling a target and move its pools into the drain set."""
        going = [c for c in self._controllers if c.target.key == key]
        if not going:
            return
        self._controllers[:] = [c for c in self._controllers if c.target.key != key]
        for c in going:
            task = self._tasks.pop(self._unit(c), None)
            if task is not None:
                task.cancel()
        if self._detach is not None:
            self._detach(going[0].target)
        self._draining[key] = going
        log.warning(
            "target %s is no longer allowed/installed: stopped reconciling, "
            "draining %d pool(s)",
            going[0].target,
            len(going),
        )

    async def _drain_step(self) -> None:
        """Tear down one round of a removed target's slots.

        Busy slots are deliberately left running: losing the target should not
        kill someone's in-flight job. They are retried each sweep, so the drain
        completes once the work does."""
        for key, ctrls in list(self._draining.items()):
            remaining = 0
            for c in ctrls:
                remaining += await self._drain_one(c)
            if remaining == 0:
                self._draining.pop(key, None)
                for c in ctrls:
                    await self._safe_close(c)
                log.info("target %s fully drained", ctrls[0].target)
            else:
                log.info(
                    "target %s: %d slot(s) still draining (busy)",
                    ctrls[0].target,
                    remaining,
                )

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
        # One config pool can back several live units (one per target), so this is
        # a name -> list, and every unit of that pool gets the new knobs.
        by_name: dict[str, list[Controller]] = {}
        for c in self._controllers:
            by_name.setdefault(self._pool_key(self._name(c)), []).append(c)
        new_names = {cfg.backend.name for cfg in new}
        if new_names != set(by_name):
            log.warning(
                "pool set changed (%s -> %s); restart huskd to add/remove pools",
                sorted(by_name),
                sorted(new_names),
            )
        for cfg in new:
            ctrls = by_name.get(cfg.backend.name)
            if not ctrls:
                continue  # a new pool — restart-only (warned above)
            for ctrl in ctrls:
                # Normalize the shared sections to what the sub-controller was
                # built with: the facade owns http_addr (blanked on the
                # sub-controller) and the per-target identity (name/vm_prefix are
                # folded per target), so feeding the file's raw values would
                # spuriously trip apply_reloaded_config's structural-change warning
                # every reload. Keep the *new* hot knobs (min_ready/max_total,
                # controller.shrink_ticks) by normalizing only the facade-owned
                # fields, and reuse the shared github object as-is.
                norm = dataclasses.replace(
                    cfg,
                    controller=dataclasses.replace(cfg.controller, http_addr=""),
                    backend=dataclasses.replace(
                        cfg.backend,
                        name=ctrl.cfg.backend.name,
                        vm_prefix=ctrl.cfg.backend.vm_prefix,
                    ),
                    github=ctrl.cfg.github,
                    access=ctrl.cfg.access,
                )
                ctrl.apply_reloaded_config(norm)
