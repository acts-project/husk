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

import itertools
import logging
import time

from husk.backend import ListSlotsError
from husk.cloudinit import render_cloud_init
from husk.config import Config
from husk.slot import Runner, Slot, SlotState, classify, match_runner
from husk.snapshot import ControllerState

log = logging.getLogger("husk.controller")


def vm_name(prefix: str, n: int) -> str:
    """Unique VM name. CERN registers VM names in DNS (LANDB) and rejects dupes,
    so suffix with a timestamp + per-process counter (two creates can land in the
    same second). Stable across rebuilds — only create mints a new one."""
    return f"{prefix}-{int(time.time())}-{n}"


def runner_name(vm: str, cycle: int) -> str:
    """GitHub runner name — unique per recycle cycle (GitHub-side only)."""
    return f"{vm}-c{cycle}"


class Controller:
    def __init__(
        self, backend, github, config: Config, *, clock=time.monotonic
    ) -> None:
        self.backend = backend
        self.github = github
        self.cfg = config
        self._clock = clock

        self.first_seen_state: dict[str, tuple[SlotState, float]] = {}
        self.last_provision_action: dict[str, float] = {}
        self.pending_start: set[str] = set()
        self.cycle_counter: dict[str, int] = {}

        self._known: set[str] = set()
        self._surplus_ticks = 0
        self._generation = 0
        self._namer = itertools.count(1)
        self.snapshot: ControllerState | None = None

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
            try:
                self.tick()
            except Exception:  # never let a single tick kill the loop
                log.exception("unhandled error in tick")
            time.sleep(self.cfg.timeouts.poll_interval_sec)

    # ----------------------------------------------------------------- tick
    def tick(self) -> ControllerState | None:
        now = self._clock()

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

        self._gc_bookkeeping({s.id for s in slots})
        for s in slots:
            self._first_sight(s, now)

        # 2. CLASSIFY
        classified = self._classify_all(slots, runners, now)
        busy = sum(1 for _, _, st in classified if st is SlotState.BUSY)

        # 3. PER-SLOT REMEDIATION (one action max per slot)
        for s, runner, state in classified:
            if s.id in self.pending_start:
                self._drain_pending_start(s, now)
                continue
            if state is SlotState.ERROR:
                self._destroy(s, "error")
            elif state is SlotState.NEEDS_RECYCLE:
                self._rebuild_then_start(s, now)
            elif state is SlotState.BUSY:
                if self._state_age(s.id, now) > self.cfg.timeouts.max_job_duration_sec:
                    log.warning("slot %s busy past max_job_duration; stopping", s.id)
                    self._safe(lambda: self.backend.stop_slot(s), f"stop {s.id}")
            elif state is SlotState.IDLE:
                if (
                    runner
                    and self._state_age(s.id, now) > self.cfg.timeouts.idle_timeout_sec
                ):
                    log.info(
                        "slot %s idle past idle_timeout; deregistering runner", s.id
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

        # 4/5. GROW or RAMP DOWN (mutually exclusive — never thrash within a tick)
        desired = min(self.cfg.backend.max_total, busy + self.cfg.backend.min_ready)
        total = len(slots)
        if desired - total > 0:
            self._surplus_ticks = 0
            self._grow(desired - total, now)
        elif total > desired:
            self._surplus_ticks += 1
            if self._surplus_ticks >= self.cfg.controller.shrink_ticks:
                self._ramp_down(classified)
                self._surplus_ticks = 0
        else:
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
        )
        return self.snapshot

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
        )
        return self.snapshot

    def _classify_all(
        self, slots, runners, now
    ) -> list[tuple[Slot, Runner | None, SlotState]]:
        classified: list[tuple[Slot, Runner | None, SlotState]] = []
        for s in slots:
            runner = match_runner(runners, s)
            state = classify(
                s,
                runner,
                provision_age=self._provision_age(s.id, now),
                startup_grace=self.cfg.timeouts.startup_grace_sec,
            )
            self._age_state(s.id, state, now)
            classified.append((s, runner, state))
        return classified

    # ----------------------------------------------------------- remediation
    def _rebuild_then_start(self, slot: Slot, now: float) -> None:
        cycle = self.cycle_counter.get(slot.id, slot.cycle) + 1
        name = runner_name(slot.name, cycle)
        try:
            jit = self.github.generate_jitconfig(name)
            user_data = render_cloud_init(jit, self.cfg.runner.url)
            self.backend.rebuild_slot(slot, user_data=user_data, cycle=cycle)
        except Exception:
            log.exception("rebuild of slot %s failed", slot.id)
            return
        self.cycle_counter[slot.id] = cycle
        self.last_provision_action[slot.id] = now
        self.pending_start.add(slot.id)

    def _drain_pending_start(self, slot: Slot, now: float) -> None:
        """Issue os-start once a rebuild has settled. Nova preserves power state,
        so a slot that was SHUTOFF before rebuild is SHUTOFF again — which would
        re-trigger NEEDS_RECYCLE if we didn't intercept it here first."""
        if slot.task_state is not None:
            return  # still settling
        if slot.status == "SHUTOFF":
            self._safe(lambda: self.backend.start_slot(slot), f"start {slot.id}")
            self.last_provision_action[slot.id] = now  # reset grace for runner-online
            self.pending_start.discard(slot.id)
        elif slot.status == "ACTIVE":
            self.pending_start.discard(slot.id)  # rebuilt-while-ACTIVE: no start needed

    def _grow(self, want: int, now: float) -> None:
        cap = self.backend.capacity()
        budget = min(want, cap.free_instances) if cap.can_create else 0
        for _ in range(max(0, budget)):
            self._create_one(now)

    def _create_one(self, now: float) -> None:
        vm = vm_name("husk", next(self._namer))
        name = runner_name(vm, 0)
        try:
            jit = self.github.generate_jitconfig(name)
            user_data = render_cloud_init(jit, self.cfg.runner.url)
            slot = self.backend.create_slot(user_data=user_data, name=vm, cycle=0)
        except Exception:
            log.exception("create of slot %s failed", vm)
            return  # one attempt; no retry storm, no orphaned ghost tracked
        self.cycle_counter[slot.id] = 0
        self.last_provision_action[slot.id] = now
        self._known.add(slot.id)

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
        self.cycle_counter.setdefault(slot.id, slot.cycle)
        if slot.id not in self.last_provision_action:
            if slot.provisioned_at is not None:
                age = max(0.0, time.time() - slot.provisioned_at)
                self.last_provision_action[slot.id] = now - age
            else:
                self.last_provision_action[slot.id] = now  # fresh grace

    def _gc_bookkeeping(self, live: set[str]) -> None:
        for d in (
            self.first_seen_state,
            self.last_provision_action,
            self.cycle_counter,
        ):
            for k in list(d):
                if k not in live:
                    del d[k]
        self.pending_start &= live
        self._known &= live

    def _forget(self, slot_id: str) -> None:
        self.first_seen_state.pop(slot_id, None)
        self.last_provision_action.pop(slot_id, None)
        self.cycle_counter.pop(slot_id, None)
        self.pending_start.discard(slot_id)
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
