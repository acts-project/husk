"""The reconcile loop — tick-based and non-blocking.

Each `tick()` takes one snapshot of reality, classifies every slot, and issues at
most one mutating action per slot (fire-and-return, no waiting). In-memory
bookkeeping (`pending_start`, `last_provision_action`, `cycle_counter`,
`first_seen_state`) ensures a mid-action slot is skipped on subsequent ticks so
nothing is double-issued. Nova is the source of truth for *existence*, so a
controller restart never orphans or duplicates slots — see the restart handling
in `tick()`.

The hard fail-safe: if `list_slots()` (or the GitHub listing) raises, the tick
aborts before any classification or mutation. A *raise* must never be read as
"no slots ⇒ destroy/create".
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import replace

from husk.backend import CreateSlotError, ListSlotsError
from husk.cloudinit import render_cloud_init
from husk.config import Config
from husk.demand import DemandRegistry
from husk.metrics import Metrics
from husk.poller import SnapshotRegistry
from husk.slot import (
    Runner,
    Slot,
    SlotState,
    classify,
    match_runner,
    orphaned_runners,
)
from husk.snapshot import ControllerState
from husk.target import Target
from husk.timing import SlotTiming

log = logging.getLogger("husk.controller")

# How stale the centralized poller's runner snapshot may be before a tick refuses
# to act on it. A failed poll keeps the last good snapshot (a blip must not stall
# reconciliation), so this is what still guarantees today's "GitHub is down ⇒ take
# no action" safety. Generous by design: at the default 30s poll cadence this is
# several missed polls, so only a sustained outage trips it.
RUNNER_SNAPSHOT_MAX_AGE_S = 180.0

# How often the opt-in runner reaper runs. Orphans appear at recycle speed
# (minutes at best), so reaping every tick would burn API budget listing nothing.
_REAP_INTERVAL_S = 300.0

# How often a long image sync republishes its staging ops onto the snapshot, so
# the dashboard shows the upload progressing instead of an unexplained wait.
_OPS_PUBLISH_INTERVAL_S = 2.0


def vm_name(prefix: str, n: int) -> str:
    """Unique VM name. CERN registers VM names in DNS (LANDB) and rejects dupes,
    so suffix with a timestamp + per-process counter (two creates can land in the
    same second). Stable across rebuilds — only create mints a new one."""
    return f"{prefix}-{int(time.time())}-{n}"


def runner_name(vm: str, cycle: int) -> str:
    """GitHub runner name — unique per recycle cycle (GitHub-side only)."""
    return f"{vm}-c{cycle}"


def _fmt(v: float | None) -> str:
    return f"{v:.0f}" if v is not None else "?"


def _action(what: str) -> str:
    """The metric label for a `_safe()` action description.

    Descriptions carry the slot id for the log line (`"destroy vm-abc123"`), which
    must never reach a label — that would mint a new series per slot per action and
    leave it behind forever. Only the leading verb is kept, which comes from a
    fixed vocabulary in this file."""
    return what.split(" ", 1)[0]


class Controller:
    def __init__(
        self,
        backend,
        github,
        config: Config,
        *,
        clock=time.monotonic,
        target: Target,
        demand: DemandRegistry | None = None,
        registry: SnapshotRegistry | None = None,
        runner_snapshot_max_age: float = RUNNER_SNAPSHOT_MAX_AGE_S,
        metrics: Metrics | None = None,
    ) -> None:
        self.backend = backend
        self.github = github
        self.cfg = config
        self._clock = clock
        # Reconcile is keyed (target, pool) — one Controller per pair. The target
        # is injected by the caller, which gets it from discovery (installations ∩
        # allowlist); a Controller is created when its target appears and torn down
        # when it goes away, so this loop never sees the set change. The demand
        # registry is the seam reconcile reads `desired` from — a webhook becomes a
        # second producer in Phase 4 without this loop changing either.
        self.target = target
        self.pool = config.backend.name
        self.demand = demand or DemandRegistry()
        # Runner listings come from the centralized poller via this registry, never
        # from an inline GitHub call. GitHub *writes* (JIT mint, deregister) are
        # still issued straight from reconcile — they are per-slot and can't be
        # batched behind a poll.
        self.registry = registry or SnapshotRegistry()
        self._runner_max_age = runner_snapshot_max_age
        # Event-time instruments, shared across every pool in the daemon (huskd
        # builds one and hands it to each Controller). Defaulting to a private
        # instance keeps every instrumented path exercised in tests and on the
        # `huskctl` side, where the numbers are simply discarded.
        self.metrics = metrics or Metrics()
        # `self.cfg` is fixed for the life of the process: huskd does not reload the
        # config file. Changing anything means a restart, which is cheap and safe —
        # every slot is re-adopted from backend metadata (husk-pool/husk-cycle/
        # husk-provisioned-at) on the first tick, and a BUSY slot is classified from
        # GitHub's runner listing, so a running job is never disturbed.

        self.first_seen_state: dict[str, tuple[SlotState, float]] = {}
        self.last_provision_action: dict[str, float] = {}
        self.prev_status: dict[str, str] = {}
        self.runner_present: set[str] = set()
        self.pending_start: set[str] = set()
        self.cycle_counter: dict[str, int] = {}
        # Rate-limits the opt-in runner reaper. -inf so the first eligible tick
        # reaps immediately rather than waiting out one interval after a restart.
        self._last_reap: float = float("-inf")
        self.timing: dict[str, SlotTiming] = {}
        # Last failed backend action per slot (rebuild/start/stop/…), surfaced on the
        # dashboard so a stuck slot's cause is visible without the logs. slot_id ->
        # (epoch, message); cleared when the same slot's next action succeeds.
        self.slot_errors: dict[str, tuple[float, str]] = {}

        self._known: set[str] = set()
        self._last_tick: float | None = None
        self._surplus_ticks = 0
        self._generation = 0
        self._namer = itertools.count(1)
        # Seed an empty snapshot so the status endpoint serves a valid (empty) 200
        # from startup instead of 503 until the first reconcile publishes. The
        # epoch-0 timestamp keeps /healthz honestly "stale" until that happens.
        self.snapshot: ControllerState | None = ControllerState(
            generation=0,
            last_reconcile_epoch=0.0,
            backend=self.cfg.backend.name,
            min_ready=self.cfg.backend.min_ready,
            max_total=self.cfg.backend.max_total,
            desired_total=self.cfg.backend.min_ready,
            counts={st.value: 0 for st in SlotState},
            slots=[],
        )

    def _sync_images(self) -> None:
        """Hand the backend the current image config so it can stage the golden
        image to every host (and GC orphans). Optional: backends with no image
        delivery (OpenStack/fake) don't implement it. A failure here must not kill
        the tick — we log and proceed with whatever image the hosts already hold."""
        fn = getattr(self.backend, "sync_images", None)
        if fn is None:
            return
        try:
            fn(self.cfg.backend)
        except Exception:
            log.warning(
                "image sync failed; continuing with current host image",
                exc_info=True,
            )
            self.metrics.action_failures.inc(self.pool, "sync_images")

    # ----------------------------------------------------------------- tick
    async def tick(self) -> ControllerState | None:
        """One reconcile pass. Wraps `_tick` only to time it and to count the
        completions, so the fail-safe early returns inside stay easy to read."""
        started = self._clock()
        try:
            return await self._tick(started)
        finally:
            self.metrics.reconcile_duration.observe(self._clock() - started, self.pool)

    async def _tick(self, now: float) -> ControllerState | None:

        # 0. IMAGE SYNC — ensure each host holds the configured golden image
        #    before any create/rebuild this tick (a no-op once synced; on a ref
        #    change it stages the new image and slots drain onto it below).
        await self._sync_images_publishing_ops()

        # 1. FAIL-SAFE SNAPSHOT — a raise aborts the whole tick (no mutations).
        #    Slot data is always read fresh here (never cached): the backend is the
        #    source of truth for existence, and acting on a stale slot list could
        #    duplicate or orphan VMs.
        try:
            slots = await asyncio.to_thread(self.backend.list_slots)
        except ListSlotsError:
            log.error("list_slots failed; aborting tick (no mutations)", exc_info=True)
            self.metrics.reconcile_aborts.inc(self.pool, "list_slots")
            return self.snapshot

        # Runner data comes from the centralized poller. Missing (never polled) or
        # stale (sustained GitHub outage) both abort the tick — the same fail-safe
        # the old inline `list_runners()` raise gave us. A merely *late* poll is
        # fine: the snapshot stays usable until it ages out.
        runners: list[Runner] | None = self.registry.runners(self.target)
        age = self.registry.age(self.target)
        if runners is None:
            log.error(
                "no runner snapshot for %s yet; aborting tick (no mutations)",
                self.target,
            )
            self.metrics.reconcile_aborts.inc(self.pool, "no_runner_snapshot")
            return self.snapshot
        if age is not None and age > self._runner_max_age:
            log.error(
                "runner snapshot for %s is %.0fs stale (max %.0fs); "
                "aborting tick (no mutations)",
                self.target,
                age,
                self._runner_max_age,
            )
            self.metrics.reconcile_aborts.inc(self.pool, "stale_runner_snapshot")
            return self.snapshot

        log.debug(
            "tick: %d managed slot(s), %d runner(s) (snapshot %.0fs old)",
            len(slots),
            len(runners),
            age or 0.0,
        )

        self._gc_bookkeeping({s.id for s in slots})
        for s in slots:
            self._first_sight(s, now)
            await self._note_active_transition(s, now)

        # Account the just-elapsed interval to each slot's prior state (read from
        # first_seen_state before this tick's classify overwrites it).
        dt = (now - self._last_tick) if self._last_tick is not None else 0.0
        self._last_tick = now
        if dt > 0:
            for s in slots:
                prev = self.first_seen_state.get(s.id)
                t = self.timing.get(s.id)
                if prev is not None and t is not None:
                    t.accumulate(prev[0], dt)

        # 2. CLASSIFY
        classified = self._classify_all(slots, runners, now)
        busy = sum(1 for _, _, st in classified if st is SlotState.BUSY)
        await self._track_runner_presence(classified, now)

        # 3-prep. POOL SIZING — computed BEFORE remediation so an excess slot can
        # be retired at its natural poweroff point (NEEDS_RECYCLE) instead of being
        # rebuilt. Under constant job load slots rarely sit IDLE, so an idle-only
        # ramp-down can never drain a downscale; retiring at poweroff can. Gated by
        # the same hysteresis as the idle ramp-down (one retirement per sustained-
        # surplus window) so it doesn't thrash when `desired` oscillates.
        desired = self._publish_demand(busy)
        total = len(slots)
        over = max(0, total - desired)
        if over > 0:
            self._surplus_ticks += 1
        else:
            self._surplus_ticks = 0
        shrink_now = (
            over > 0 and self._surplus_ticks >= self.cfg.controller.shrink_ticks
        )
        log.debug(
            "pool: busy=%d total=%d desired=%d (min_ready=%d max_total=%d) "
            "surplus_ticks=%d shrink_now=%s",
            busy,
            total,
            desired,
            self.cfg.backend.min_ready,
            self.cfg.backend.max_total,
            self._surplus_ticks,
            shrink_now,
        )
        surplus_remaining = over  # how many powered-off excess slots to shed/hold
        did_retire = False

        # 2b. REAP dead runner registrations (opt-in; see controller.reap_runners).
        #     Placed here, on the same fresh slots+runners snapshot the tick has
        #     already fail-safed, so it can never act on a stale slot list.
        await self._reap_runners(slots, runners, now)

        # 3. PER-SLOT REMEDIATION (one action max per slot)
        for s, runner, state in classified:
            if s.id in self.pending_start:
                await self._drain_pending_start(s, now)
                continue
            if state is SlotState.ERROR:
                await self._destroy(s, "error")
            elif state is SlotState.NEEDS_RECYCLE:
                if surplus_remaining > 0:
                    # This powered-off slot is surplus. Don't rebuild it — either
                    # retire it (sustained surplus) or hold it off so a returning
                    # load can reclaim it via rebuild once we're no longer over.
                    surplus_remaining -= 1
                    if shrink_now and not did_retire:
                        log.info(
                            "retiring excess slot %s at poweroff (sustained surplus)",
                            s.id,
                        )
                        await self._destroy(s, "decommission")
                        did_retire = True
                else:
                    await self._rebuild_then_start(s, now)
            elif state is SlotState.BUSY:
                if self._state_age(s.id, now) > self.cfg.timeouts.max_job_duration_sec:
                    log.warning("slot %s busy past max_job_duration; stopping", s.id)
                    await self._safe(
                        asyncio.to_thread(self.backend.stop_slot, s),
                        f"stop {s.id}",
                        slot_id=s.id,
                    )
            elif state is SlotState.IDLE:
                stale = s.image_stale
                idle_timed_out = (
                    self._state_age(s.id, now) > self.cfg.timeouts.idle_timeout_sec
                )
                if runner and (stale or idle_timed_out):
                    # Deregister the (idle, no-job) runner so GitHub stops
                    # dispatching to it; the slot then recycles — rebuilding onto
                    # the current golden, which clears a stale image. Same drain
                    # path as the idle-timeout reap, just also triggered by a
                    # config image_ref change.
                    log.info(
                        "slot %s idle; deregistering runner (%s)",
                        s.id,
                        "stale image" if stale else "idle_timeout",
                    )
                    await self._safe(
                        self.github.delete_runner(runner.id), "delete_runner"
                    )
            elif state is SlotState.UNHEALTHY:
                log.warning(
                    "slot %s unhealthy (no runner past grace); rebuilding", s.id
                )
                await self._rebuild_then_start(s, now)
            # STARTING: nothing — re-check next tick.

        # 4/5. GROW or RAMP DOWN (mutually exclusive — never thrash within a tick).
        # Sizing + surplus hysteresis were computed in 3-prep above; an excess slot
        # that was already off got retired in step 3.
        if desired - total > 0:
            await self._grow(desired - total, now)
        elif shrink_now and not did_retire:
            # No excess slot was powered off to retire this tick → decommission an
            # idle slot (the no-load downscale path; a no-op if none are idle).
            await self._ramp_down(classified)
            did_retire = True
        if did_retire:
            self._surplus_ticks = 0

        # 6. PUBLISH SNAPSHOT
        self.metrics.reconcile_ticks.inc(self.pool)
        self._generation += 1
        self.snapshot = ControllerState.from_classified(
            generation=self._generation,
            backend=self.cfg.backend.name,
            min_ready=self.cfg.backend.min_ready,
            max_total=self.cfg.backend.max_total,
            desired_total=desired,
            classified=classified,
            timing=self.timing,
            ops=self._backend_ops(),
            image_ref=self.cfg.backend.image_ref,
            errors=self._all_errors(),
        )
        log.debug(
            "tick %d done: %s",
            self._generation,
            {k: v for k, v in self.snapshot.counts.items() if v},
        )
        return self.snapshot

    async def observe(self) -> ControllerState:
        """Read-only classification snapshot — no mutations.

        Raises through a slot-listing failure (the caller surfaces it); unlike
        `tick`, there is nothing to fail safe *about* here since we never mutate.
        Runners come from the poller's registry, treating "never polled" as an
        empty listing rather than an error — an observer should still render the
        slots it can see."""
        now = self._clock()
        slots = await asyncio.to_thread(self.backend.list_slots)
        runners = self.registry.runners(self.target) or []
        for s in slots:
            self._first_sight(s, now)
        classified = self._classify_all(slots, runners, now)
        busy = sum(1 for _, _, st in classified if st is SlotState.BUSY)
        desired = self._publish_demand(busy)
        self._generation += 1
        self.snapshot = ControllerState.from_classified(
            generation=self._generation,
            backend=self.cfg.backend.name,
            min_ready=self.cfg.backend.min_ready,
            max_total=self.cfg.backend.max_total,
            desired_total=desired,
            classified=classified,
            timing=self.timing,
            ops=self._backend_ops(),
            image_ref=self.cfg.backend.image_ref,
            errors=self._all_errors(),
        )
        return self.snapshot

    def _publish_demand(self, busy: int) -> int:
        """Size this target through the demand seam: compute `desired` from the
        observed load, publish it to the registry, and read it back.

        In Phase 0 the producer and consumer are the same tick, so the read-back
        is identical to the old inline `min(max_total, busy + min_ready)` — the
        point is only to move the sizing behind the registry so a centralized
        poller (Phase 1) or a webhook (Phase 4) can become the producer without
        touching this loop. The read-back falls back to the just-computed value
        defensively; it is never actually None here."""
        desired = min(self.cfg.backend.max_total, busy + self.cfg.backend.min_ready)
        self.demand.publish(self.target, self.pool, busy=busy, desired=desired)
        got = self.demand.desired(self.target, self.pool)
        return got if got is not None else desired

    def _classify_all(
        self, slots, runners, now
    ) -> list[tuple[Slot, Runner | None, SlotState]]:
        classified: list[tuple[Slot, Runner | None, SlotState]] = []
        for s in slots:
            runner = match_runner(runners, s)
            prov_age = self._provision_age(s.id, now)
            state = classify(
                s,
                runner,
                provision_age=prov_age,
                startup_grace=self.cfg.timeouts.startup_grace_sec,
            )
            self._age_state(s.id, state, now)
            log.debug(
                "classify %s (%s): status=%s task=%s runner=%s prov_age=%s -> %s",
                s.id,
                s.name,
                s.status,
                s.task_state,
                runner.status if runner else "none",
                f"{prov_age:.0f}s" if prov_age is not None else "n/a",
                state.value,
            )
            classified.append((s, runner, state))
        return classified

    async def _sync_images_publishing_ops(self) -> None:
        """`_sync_images` in a thread, republishing the backend's staging ops onto
        the current snapshot while it runs.

        A first sync that pulls a golden from the registry and uploads it to Glance
        takes minutes, and it happens before this tick can publish any slot data —
        so without this the dashboard sits on the seeded empty snapshot with nothing
        to explain the wait. The ops list is exactly that explanation, and it only
        exists once the upload is underway, hence polling it rather than publishing
        once up front. `last_reconcile_epoch` is deliberately untouched: no reconcile
        has completed, and /healthz must keep saying so."""
        task = asyncio.create_task(asyncio.to_thread(self._sync_images))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=_OPS_PUBLISH_INTERVAL_S)
                if self.snapshot is not None:
                    self.snapshot = replace(self.snapshot, ops=self._backend_ops())
                if done:
                    break
        finally:
            await task  # propagate a raise (and never leave the task orphaned)

    # ----------------------------------------------------------- remediation
    def _backend_ops(self) -> list:
        """The backend's in-flight/recent async ops (image staging) for the status
        board. Optional — backends with no async delivery (fake) don't expose it."""
        fn = getattr(self.backend, "staging_ops", None)
        return fn() if fn else []

    async def _image_ready(self, slot: Slot) -> bool:
        """Whether the backend can re-image this slot yet. Grows are already gated
        by `capacity()` reporting zero while the golden stages; rebuilds need the
        same gate so a NEEDS_RECYCLE/UNHEALTHY slot doesn't drive `rebuild_slot`
        into a no-image error (and burn a JIT token) every tick during staging.
        Optional on the backend — absent ⇒ ready (fake/manual paths)."""
        fn = getattr(self.backend, "image_ready", None)
        return await asyncio.to_thread(fn, slot) if fn else True

    async def _rebuild_then_start(self, slot: Slot, now: float) -> None:
        if not await self._image_ready(slot):
            log.info(
                "slot %s needs recycle but golden image still staging; deferring",
                slot.id,
            )
            return
        cycle = self.cycle_counter.get(slot.id, slot.cycle) + 1
        name = runner_name(slot.name, cycle)
        try:
            jit = await self.github.generate_jitconfig(name)
            user_data = render_cloud_init(
                jit,
                gpu=self.cfg.runner.gpu,
                scrape_cidr=self.cfg.runner.scrape_cidr,
                cvmfs_repos=self.cfg.cvmfs.repositories if self.cfg.cvmfs else (),
                cvmfs_proxy=self.cfg.cvmfs.http_proxy if self.cfg.cvmfs else "",
                cvmfs_quota_mb=(
                    self.cfg.cvmfs.quota_limit_mb if self.cfg.cvmfs else 4000
                ),
                egress_allow_hosts=(
                    self.cfg.egress.allow_hosts if self.cfg.egress else ()
                ),
                container_env=(self.cfg.container.env if self.cfg.container else ()),
            )
            await asyncio.to_thread(
                self.backend.rebuild_slot, slot, user_data=user_data, cycle=cycle
            )
            self.slot_errors.pop(slot.id, None)  # cleared on a successful rebuild
        except Exception as e:
            log.exception("rebuild of slot %s failed", slot.id)
            self.slot_errors[slot.id] = (now, f"rebuild failed: {e}")
            self.metrics.action_failures.inc(self.pool, "rebuild")
            return
        self.metrics.slot_recycles.inc(self.pool)
        self.cycle_counter[slot.id] = cycle
        self.last_provision_action[slot.id] = now
        self.pending_start.add(slot.id)
        t = self.timing.get(slot.id)
        if t is not None:
            t.on_issued(now)
        log.info("rebuilt slot %s as runner %s (cycle %d)", slot.id, name, cycle)

    async def _drain_pending_start(self, slot: Slot, now: float) -> None:
        """Issue os-start once a rebuild has settled. Nova preserves power state,
        so a slot that was SHUTOFF before rebuild is SHUTOFF again — which would
        re-trigger NEEDS_RECYCLE if we didn't intercept it here first."""
        if slot.task_state is not None:
            log.debug(
                "slot %s pending-start: still settling (task=%s)",
                slot.id,
                slot.task_state,
            )
            return  # still settling
        if slot.status == "SHUTOFF":
            log.info("slot %s rebuild settled to SHUTOFF; os-starting", slot.id)
            await self._safe(
                asyncio.to_thread(self.backend.start_slot, slot),
                f"start {slot.id}",
                slot_id=slot.id,
            )
            self.last_provision_action[slot.id] = now  # reset grace for runner-online
            self.pending_start.discard(slot.id)
        elif slot.status == "ACTIVE":
            log.debug("slot %s rebuilt while ACTIVE; no os-start needed", slot.id)
            self.pending_start.discard(slot.id)  # rebuilt-while-ACTIVE: no start needed

    async def _grow(self, want: int, now: float) -> None:
        cap = await asyncio.to_thread(self.backend.capacity)
        budget = min(want, cap.free_instances) if cap.can_create else 0
        log.debug(
            "grow: want=%d capacity(can_create=%s free=%d) -> budget=%d",
            want,
            cap.can_create,
            cap.free_instances,
            budget,
        )
        for _ in range(max(0, budget)):
            await self._create_one(now)

    async def _create_one(self, now: float) -> None:
        vm = vm_name(self.cfg.backend.vm_prefix, next(self._namer))
        name = runner_name(vm, 0)
        log.debug("creating slot %s (runner %s)", vm, name)
        try:
            jit = await self.github.generate_jitconfig(name)
            user_data = render_cloud_init(
                jit,
                gpu=self.cfg.runner.gpu,
                scrape_cidr=self.cfg.runner.scrape_cidr,
                cvmfs_repos=self.cfg.cvmfs.repositories if self.cfg.cvmfs else (),
                cvmfs_proxy=self.cfg.cvmfs.http_proxy if self.cfg.cvmfs else "",
                cvmfs_quota_mb=(
                    self.cfg.cvmfs.quota_limit_mb if self.cfg.cvmfs else 4000
                ),
                egress_allow_hosts=(
                    self.cfg.egress.allow_hosts if self.cfg.egress else ()
                ),
                container_env=(self.cfg.container.env if self.cfg.container else ()),
            )
            slot = await asyncio.to_thread(
                self.backend.create_slot, user_data=user_data, name=vm, cycle=0
            )
        except CreateSlotError as e:
            # The backend already diagnosed this one, so the message IS the
            # report. No traceback: creates are retried every tick while the pool
            # is below min_ready, and a persistent cause (a dead image, exhausted
            # quota) would otherwise emit a 30-line trace per slot per tick —
            # burying the one line that says what to fix.
            log.error("create of slot %s failed: %s", vm, e)
            self.metrics.action_failures.inc(self.pool, "create")
            return
        except Exception:
            log.exception("create of slot %s failed", vm)
            self.metrics.action_failures.inc(self.pool, "create")
            return  # one attempt; no retry storm, no orphaned ghost tracked
        self.metrics.slots_created.inc(self.pool)
        self.cycle_counter[slot.id] = 0
        self.last_provision_action[slot.id] = now
        self._known.add(slot.id)
        self.timing[slot.id] = SlotTiming(first_seen=now, issued_at=now)
        log.info("created slot %s (%s)", slot.id, vm)

    async def _ramp_down(self, classified) -> None:
        idle = [(s, r) for s, r, st in classified if st is SlotState.IDLE]
        if not idle:
            return
        slot, runner = min(idle, key=lambda sr: (sr[0].created_at, sr[0].name))
        log.info("ramping down idle slot %s (sustained surplus)", slot.id)
        if runner is not None:
            await self._safe(self.github.delete_runner(runner.id), "delete_runner")
        await self._destroy(slot, "decommission")

    async def _reap_runners(
        self, slots: list[Slot], runners: list[Runner], now: float
    ) -> None:
        """Delete this pool's dead runner registrations. Opt-in, prefix-scoped.

        Runs at most every `_REAP_INTERVAL_S` rather than every tick: orphans
        accrue at recycle speed (minutes), so a 5s cadence would spend API budget
        listing for nothing. Failures are swallowed per runner — cleanup is
        housekeeping and must never abort a tick or block provisioning.
        """
        mode = self.cfg.controller.reap_runners
        if mode == "off":
            return
        if now - self._last_reap < _REAP_INTERVAL_S:
            return
        self._last_reap = now

        doomed = orphaned_runners(runners, slots, self.cfg.backend.vm_prefix)
        if not doomed:
            return
        names = [r.name for r in doomed]
        if mode == "dry-run":
            log.info(
                "reap (dry-run): would delete %d offline runner(s) for %s: %s",
                len(names),
                self.cfg.backend.vm_prefix,
                names,
            )
            return
        log.info("reap: deleting %d dead runner registration(s): %s", len(names), names)
        for r in doomed:
            await self._safe(self.github.delete_runner(r.id), f"reap runner {r.name}")

    async def _destroy(self, slot: Slot, reason: str) -> None:
        # `reason` is a fixed vocabulary here (error / decommission / …), so it is
        # safe as a label and is the thing you actually want to break down by:
        # slots retired for surplus and slots destroyed because they broke are
        # very different signals.
        await self._safe(
            asyncio.to_thread(self.backend.destroy_slot, slot, reason=reason),
            f"destroy {slot.id}",
        )
        self.metrics.slots_destroyed.inc(self.pool, reason)
        self._forget(slot.id)

    # ------------------------------------------------------------ bookkeeping
    def _first_sight(self, slot: Slot, now: float) -> None:
        """Seed durable/conservative state the first time we see a slot.

        On restart the in-memory clocks are gone. Prefer the slot's durable Nova
        metadata (husk-cycle / husk-provisioned-at); otherwise grant a fresh
        startup grace so a restart never instantly declares a healthy-but-
        installing slot UNHEALTHY."""
        if slot.id in self._known:
            return
        self._known.add(slot.id)
        self.timing.setdefault(slot.id, SlotTiming(first_seen=now))
        self.cycle_counter.setdefault(slot.id, slot.cycle)
        if slot.id not in self.last_provision_action:
            if slot.provisioned_at is not None:
                age = max(0.0, time.time() - slot.provisioned_at)
                self.last_provision_action[slot.id] = now - age
                log.debug(
                    "first sight of %s: seeded provision age %.0fs from metadata (cycle %d)",
                    slot.id,
                    age,
                    slot.cycle,
                )
            else:
                self.last_provision_action[slot.id] = now  # fresh grace
                log.debug("first sight of %s: granted fresh startup grace", slot.id)

    async def _track_runner_presence(self, classified, now: float) -> None:
        """Keep the no-runner grace origin anchored to 'last had a runner'.

        A JIT runner deregisters at job end (and huskd deletes it on idle-reap),
        then the VM powers off a few seconds later — so a long-lived slot spends a
        brief ACTIVE-without-runner window before reaching SHUTOFF. Anchoring grace
        to boot would flag that window UNHEALTHY (and rebuild the slot) every
        recycle. Instead: while a runner is attached, refresh the origin each tick;
        when it disappears while still ACTIVE, anchor grace to the loss and persist
        it (so a stateless `huskctl status` reading metadata agrees) — the slot is
        draining, not unhealthy. A slot whose runner NEVER appeared is never in
        runner_present, so genuine cloud-init failures still go UNHEALTHY on time."""
        for s, runner, _state in classified:
            if runner is not None and runner.online:
                if s.id not in self.runner_present:  # runner just came online
                    t = self.timing.get(s.id)
                    if t is not None:
                        t.on_runner_online(now)
                        # The distribution behind the per-slot "last value"
                        # gauges. Recorded here, at the moment the bring-up
                        # completes, because a scrape-time renderer cannot know
                        # how many bring-ups happened since the last scrape.
                        if t.last_cloudinit_seconds is not None:
                            self.metrics.cloudinit_duration.observe(
                                t.last_cloudinit_seconds, self.pool
                            )
                        if t.last_recycle_seconds is not None:
                            self.metrics.recycle_duration.observe(
                                t.last_recycle_seconds, self.pool
                            )
                        log.debug(
                            "slot %s runner online: cloud-init=%ss recycle=%ss",
                            s.id,
                            _fmt(t.last_cloudinit_seconds),
                            _fmt(t.last_recycle_seconds),
                        )
                self.last_provision_action[s.id] = now
                self.runner_present.add(s.id)
            elif s.id in self.runner_present:
                self.runner_present.discard(s.id)
                if s.status == "ACTIVE":  # draining toward SHUTOFF, not unhealthy
                    self.last_provision_action[s.id] = now
                    await self._safe(
                        asyncio.to_thread(self.backend.mark_active, s),
                        f"mark_active {s.id}",
                        slot_id=s.id,
                    )
                    log.debug(
                        "slot %s runner gone while ACTIVE; draining (grace reset)", s.id
                    )

    async def _note_active_transition(self, slot: Slot, now: float) -> None:
        """Restart the startup-grace clock when a slot first reaches ACTIVE.

        The clock is otherwise stamped at create/rebuild *issue* time, but a fresh
        CERN create spends minutes in BUILD (Neutron) before the VM is ACTIVE —
        which can exceed startup_grace on its own, so the slot would be judged
        UNHEALTHY the instant it boots, before cloud-init can register the runner.
        Anchoring grace to the ACTIVE transition makes it cover only the
        cloud-init / runner-registration phase, independent of build time. We only
        reset on an observed non-ACTIVE→ACTIVE edge (prev seen this process), so a
        controller restart that first sees an already-ACTIVE slot keeps its
        first-sight grace instead of resetting on every restart."""
        prev = self.prev_status.get(slot.id)
        if slot.status == "ACTIVE" and prev is not None and prev != "ACTIVE":
            self.last_provision_action[slot.id] = now
            t = self.timing.get(slot.id)
            if t is not None:
                t.on_active(now)
            # Persist the grace origin so stateless observers (huskctl status) and
            # a restarted controller agree with us instead of re-deriving grace
            # from the create-time metadata (which would read UNHEALTHY).
            await self._safe(
                asyncio.to_thread(self.backend.mark_active, slot),
                f"mark_active {slot.id}",
                slot_id=slot.id,
            )
            log.debug("slot %s reached ACTIVE; (re)starting startup grace", slot.id)
        self.prev_status[slot.id] = slot.status

    def _gc_bookkeeping(self, live: set[str]) -> None:
        for d in (
            self.first_seen_state,
            self.last_provision_action,
            self.prev_status,
            self.cycle_counter,
            self.timing,
            self.slot_errors,
        ):
            for k in list(d):
                if k not in live:
                    del d[k]
        self.pending_start &= live
        self.runner_present &= live
        self._known &= live

    def _forget(self, slot_id: str) -> None:
        self.first_seen_state.pop(slot_id, None)
        self.last_provision_action.pop(slot_id, None)
        self.prev_status.pop(slot_id, None)
        self.cycle_counter.pop(slot_id, None)
        self.timing.pop(slot_id, None)
        self.pending_start.discard(slot_id)
        self.runner_present.discard(slot_id)
        self._known.discard(slot_id)

    def _provision_age(self, slot_id: str, now: float) -> float | None:
        t = self.last_provision_action.get(slot_id)
        return None if t is None else now - t

    def _age_state(self, slot_id: str, state: SlotState, now: float) -> None:
        prev = self.first_seen_state.get(slot_id)
        if prev is None or prev[0] is not state:
            self.first_seen_state[slot_id] = (state, now)

    def _state_age(self, slot_id: str, now: float) -> float:
        entry = self.first_seen_state.get(slot_id)
        return now - entry[1] if entry else 0.0

    def _all_errors(self) -> dict[str, tuple[float, str]]:
        """Per-slot errors for the dashboard: the backend's non-fatal warnings
        (e.g. swallowed metadata-write 500s) overlaid by our fatal action failures,
        so a fatal error wins when a slot has both."""
        combined = dict(self.backend.slot_warnings())
        combined.update(self.slot_errors)
        return combined

    async def _safe(self, awaitable, what: str, *, slot_id: str | None = None) -> None:
        """Await one fallible action, recording (not raising) its failure.

        Callers hand in the awaitable directly — `asyncio.to_thread(backend.op, …)`
        for a blocking backend call, or an async GitHub coroutine. Both defer their
        work until awaited here, so this stays the single place an action's failure
        is caught and pinned to a slot."""
        try:
            await awaitable
            if slot_id is not None:
                self.slot_errors.pop(slot_id, None)  # cleared on success
        except Exception as e:
            log.exception("%s failed", what)
            self.metrics.action_failures.inc(self.pool, _action(what))
            if slot_id is not None:
                self.slot_errors[slot_id] = (self._clock(), f"{what} failed: {e}")
