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

import dataclasses
import itertools
import logging
import time
from typing import Callable

from husk.backend import ListSlotsError
from husk.cloudinit import render_cloud_init
from husk.config import Config
from husk.slot import Runner, Slot, SlotState, classify, match_runner
from husk.snapshot import ControllerState, write_state
from husk.timing import SlotTiming

log = logging.getLogger("husk.controller")

# Config knobs that are safe to change while the loop runs: the controller reads
# each of these fresh from self.cfg on every tick, so swapping them between ticks
# takes effect immediately with no rebuild. Everything else (backend type/hosts,
# github creds, runner labels, lock/http/state paths) is captured at build time by
# the backend/github/server objects and needs a restart — apply_reloaded_config
# warns about and ignores changes to those rather than half-applying them.
_HOT_RELOAD: dict[str, tuple[str, ...]] = {
    # image_ref is hot: a new ref is picked up by sync_images on the next tick,
    # which stages the new golden and drains slots onto it (see image-pipeline.md
    # Phase C). Per-host image_ref overrides are still restart-only.
    "backend": ("min_ready", "max_total", "image_ref"),
    "controller": ("shrink_ticks",),
    "timeouts": (
        "poll_interval_sec",
        "idle_timeout_sec",
        "startup_grace_sec",
        "max_job_duration_sec",
    ),
}


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


class Controller:
    def __init__(
        self,
        backend,
        github,
        config: Config,
        *,
        clock=time.monotonic,
        reload_config: Callable[[], Config | None] | None = None,
    ) -> None:
        self.backend = backend
        self.github = github
        self.cfg = config
        self._clock = clock
        # Optional hot-reload hook: called once per tick from run(); returns a
        # freshly loaded Config when the file changed (else None). cli.py wires an
        # mtime-guarded loader; tests leave it None (tick() stays reload-free).
        self._reload_config = reload_config

        self.first_seen_state: dict[str, tuple[SlotState, float]] = {}
        self.last_provision_action: dict[str, float] = {}
        self.prev_status: dict[str, str] = {}
        self.runner_present: set[str] = set()
        self.pending_start: set[str] = set()
        self.cycle_counter: dict[str, int] = {}
        self.timing: dict[str, SlotTiming] = {}

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

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        """Blocking reconcile loop. Caller is responsible for the lock."""
        log.info(
            "huskd starting: backend=%s min_ready=%d max_total=%d poll=%.0fs",
            self.cfg.backend.name,
            self.cfg.backend.min_ready,
            self.cfg.backend.max_total,
            self.cfg.timeouts.poll_interval_sec,
        )
        while True:
            self._maybe_reload()
            try:
                self.tick()
            except Exception:  # never let a single tick kill the loop
                log.exception("unhandled error in tick")
            time.sleep(self.cfg.timeouts.poll_interval_sec)

    # -------------------------------------------------------------- hot reload
    def _maybe_reload(self) -> None:
        """Pick up an edited config file between ticks (no restart needed).

        The loader is mtime-guarded and swallows its own parse errors, so a bad
        edit leaves the running config in place rather than killing the loop."""
        if self._reload_config is None:
            return
        try:
            new = self._reload_config()
        except Exception:
            log.warning("config reload failed; keeping current config", exc_info=True)
            return
        if new is not None:
            self.apply_reloaded_config(new)

    def apply_reloaded_config(self, new: Config) -> None:
        """Adopt the hot-reloadable knobs from a freshly loaded config; warn about
        (and ignore) any structural change that needs a restart to take effect."""
        cur = self.cfg
        changes: list[str] = []
        replacements: dict[str, object] = {}
        for section, fields in _HOT_RELOAD.items():
            cur_sec, new_sec = getattr(cur, section), getattr(new, section)
            updates = {
                f: getattr(new_sec, f)
                for f in fields
                if getattr(cur_sec, f) != getattr(new_sec, f)
            }
            if updates:
                changes += [
                    f"{section}.{f}: {getattr(cur_sec, f)} -> {v}"
                    for f, v in updates.items()
                ]
                replacements[section] = dataclasses.replace(cur_sec, **updates)

        # Structural diff: normalize `new` by forcing the hot fields back to their
        # current values, then compare whole-config. Any remaining difference is a
        # field we can't safely swap live (backend/hosts, github, runner, paths).
        normalized = new
        for section, fields in _HOT_RELOAD.items():
            cur_sec = getattr(cur, section)
            normalized = dataclasses.replace(
                normalized,
                **{
                    section: dataclasses.replace(
                        getattr(normalized, section),
                        **{f: getattr(cur_sec, f) for f in fields},
                    )
                },
            )
        if normalized != cur:
            log.warning(
                "config reload: structural changes ignored (restart huskd to apply: "
                "backend/hosts, github, runner, or controller paths/ports)"
            )

        if replacements:
            self.cfg = dataclasses.replace(self.cfg, **replacements)
            log.info("config reload applied: %s", "; ".join(changes))

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

    # ----------------------------------------------------------------- tick
    def tick(self) -> ControllerState | None:
        now = self._clock()

        # 0. IMAGE SYNC — ensure each host holds the configured golden image
        #    before any create/rebuild this tick (a no-op once synced; on a ref
        #    change it stages the new image and slots drain onto it below).
        self._sync_images()

        # 1. FAIL-SAFE SNAPSHOT — a raise aborts the whole tick (no mutations).
        try:
            slots = self.backend.list_slots()
        except ListSlotsError:
            log.error("list_slots failed; aborting tick (no mutations)", exc_info=True)
            return self.snapshot
        try:
            runners: list[Runner] = self.github.list_runners()
        except Exception:
            log.error(
                "list_runners failed; aborting tick (no mutations)", exc_info=True
            )
            return self.snapshot

        log.debug("tick: %d managed slot(s), %d runner(s)", len(slots), len(runners))

        self._gc_bookkeeping({s.id for s in slots})
        for s in slots:
            self._first_sight(s, now)
            self._note_active_transition(s, now)

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
        self._track_runner_presence(classified, now)

        # 3-prep. POOL SIZING — computed BEFORE remediation so an excess slot can
        # be retired at its natural poweroff point (NEEDS_RECYCLE) instead of being
        # rebuilt. Under constant job load slots rarely sit IDLE, so an idle-only
        # ramp-down can never drain a downscale; retiring at poweroff can. Gated by
        # the same hysteresis as the idle ramp-down (one retirement per sustained-
        # surplus window) so it doesn't thrash when `desired` oscillates.
        desired = min(self.cfg.backend.max_total, busy + self.cfg.backend.min_ready)
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

        # 3. PER-SLOT REMEDIATION (one action max per slot)
        for s, runner, state in classified:
            if s.id in self.pending_start:
                self._drain_pending_start(s, now)
                continue
            if state is SlotState.ERROR:
                self._destroy(s, "error")
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
                        self._destroy(s, "decommission")
                        did_retire = True
                else:
                    self._rebuild_then_start(s, now)
            elif state is SlotState.BUSY:
                if self._state_age(s.id, now) > self.cfg.timeouts.max_job_duration_sec:
                    log.warning("slot %s busy past max_job_duration; stopping", s.id)
                    self._safe(lambda: self.backend.stop_slot(s), f"stop {s.id}")
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
                    self._safe(
                        lambda: self.github.delete_runner(runner.id), "delete_runner"
                    )
            elif state is SlotState.UNHEALTHY:
                log.warning(
                    "slot %s unhealthy (no runner past grace); rebuilding", s.id
                )
                self._rebuild_then_start(s, now)
            # STARTING: nothing — re-check next tick.

        # 4/5. GROW or RAMP DOWN (mutually exclusive — never thrash within a tick).
        # Sizing + surplus hysteresis were computed in 3-prep above; an excess slot
        # that was already off got retired in step 3.
        if desired - total > 0:
            self._grow(desired - total, now)
        elif shrink_now and not did_retire:
            # No excess slot was powered off to retire this tick → decommission an
            # idle slot (the no-load downscale path; a no-op if none are idle).
            self._ramp_down(classified)
            did_retire = True
        if did_retire:
            self._surplus_ticks = 0

        # 6. PUBLISH SNAPSHOT
        self._generation += 1
        self.snapshot = ControllerState.from_classified(
            generation=self._generation,
            backend=self.cfg.backend.name,
            min_ready=self.cfg.backend.min_ready,
            max_total=self.cfg.backend.max_total,
            desired_total=desired,
            classified=classified,
            timing=self.timing,
        )
        log.debug(
            "tick %d done: %s",
            self._generation,
            {k: v for k, v in self.snapshot.counts.items() if v},
        )
        self._publish()
        return self.snapshot

    def _publish(self) -> None:
        """Write the snapshot so `huskctl status` renders huskd's exact view
        rather than independently (and divergently) recomputing it."""
        path = self.cfg.controller.state_path
        if not path or self.snapshot is None:
            return
        try:
            write_state(path, self.snapshot)
        except Exception:
            log.warning("could not publish state to %s", path, exc_info=True)

    def observe(self) -> ControllerState:
        """Read-only classification snapshot for `huskctl status` — no mutations.

        Raises through any listing failure (the CLI surfaces it); unlike `tick`,
        there is nothing to fail safe *about* here since we never mutate."""
        now = self._clock()
        slots = self.backend.list_slots()
        runners = self.github.list_runners()
        for s in slots:
            self._first_sight(s, now)
        classified = self._classify_all(slots, runners, now)
        busy = sum(1 for _, _, st in classified if st is SlotState.BUSY)
        desired = min(self.cfg.backend.max_total, busy + self.cfg.backend.min_ready)
        self._generation += 1
        self.snapshot = ControllerState.from_classified(
            generation=self._generation,
            backend=self.cfg.backend.name,
            min_ready=self.cfg.backend.min_ready,
            max_total=self.cfg.backend.max_total,
            desired_total=desired,
            classified=classified,
            timing=self.timing,
        )
        return self.snapshot

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

    # ----------------------------------------------------------- remediation
    def _rebuild_then_start(self, slot: Slot, now: float) -> None:
        cycle = self.cycle_counter.get(slot.id, slot.cycle) + 1
        name = runner_name(slot.name, cycle)
        try:
            jit = self.github.generate_jitconfig(name)
            user_data = render_cloud_init(
                jit, self.cfg.runner.url, gpu=self.cfg.runner.gpu
            )
            self.backend.rebuild_slot(slot, user_data=user_data, cycle=cycle)
        except Exception:
            log.exception("rebuild of slot %s failed", slot.id)
            return
        self.cycle_counter[slot.id] = cycle
        self.last_provision_action[slot.id] = now
        self.pending_start.add(slot.id)
        t = self.timing.get(slot.id)
        if t is not None:
            t.on_issued(now)
        log.info("rebuilt slot %s as runner %s (cycle %d)", slot.id, name, cycle)

    def _drain_pending_start(self, slot: Slot, now: float) -> None:
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
            self._safe(lambda: self.backend.start_slot(slot), f"start {slot.id}")
            self.last_provision_action[slot.id] = now  # reset grace for runner-online
            self.pending_start.discard(slot.id)
        elif slot.status == "ACTIVE":
            log.debug("slot %s rebuilt while ACTIVE; no os-start needed", slot.id)
            self.pending_start.discard(slot.id)  # rebuilt-while-ACTIVE: no start needed

    def _grow(self, want: int, now: float) -> None:
        cap = self.backend.capacity()
        budget = min(want, cap.free_instances) if cap.can_create else 0
        log.debug(
            "grow: want=%d capacity(can_create=%s free=%d) -> budget=%d",
            want,
            cap.can_create,
            cap.free_instances,
            budget,
        )
        for _ in range(max(0, budget)):
            self._create_one(now)

    def _create_one(self, now: float) -> None:
        vm = vm_name("husk", next(self._namer))
        name = runner_name(vm, 0)
        log.debug("creating slot %s (runner %s)", vm, name)
        try:
            jit = self.github.generate_jitconfig(name)
            user_data = render_cloud_init(
                jit, self.cfg.runner.url, gpu=self.cfg.runner.gpu
            )
            slot = self.backend.create_slot(user_data=user_data, name=vm, cycle=0)
        except Exception:
            log.exception("create of slot %s failed", vm)
            return  # one attempt; no retry storm, no orphaned ghost tracked
        self.cycle_counter[slot.id] = 0
        self.last_provision_action[slot.id] = now
        self._known.add(slot.id)
        self.timing[slot.id] = SlotTiming(first_seen=now, issued_at=now)
        log.info("created slot %s (%s)", slot.id, vm)

    def _ramp_down(self, classified) -> None:
        idle = [(s, r) for s, r, st in classified if st is SlotState.IDLE]
        if not idle:
            return
        slot, runner = min(idle, key=lambda sr: (sr[0].created_at, sr[0].name))
        log.info("ramping down idle slot %s (sustained surplus)", slot.id)
        if runner is not None:
            self._safe(lambda: self.github.delete_runner(runner.id), "delete_runner")
        self._destroy(slot, "decommission")

    def _destroy(self, slot: Slot, reason: str) -> None:
        self._safe(
            lambda: self.backend.destroy_slot(slot, reason=reason), f"destroy {slot.id}"
        )
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

    def _track_runner_presence(self, classified, now: float) -> None:
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
                    self._safe(
                        lambda: self.backend.mark_active(s), f"mark_active {s.id}"
                    )
                    log.debug(
                        "slot %s runner gone while ACTIVE; draining (grace reset)", s.id
                    )

    def _note_active_transition(self, slot: Slot, now: float) -> None:
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
            self._safe(lambda: self.backend.mark_active(slot), f"mark_active {slot.id}")
            log.debug("slot %s reached ACTIVE; (re)starting startup grace", slot.id)
        self.prev_status[slot.id] = slot.status

    def _gc_bookkeeping(self, live: set[str]) -> None:
        for d in (
            self.first_seen_state,
            self.last_provision_action,
            self.prev_status,
            self.cycle_counter,
            self.timing,
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

    @staticmethod
    def _safe(fn, what: str) -> None:
        try:
            fn()
        except Exception:
            log.exception("%s failed", what)
