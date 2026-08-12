"""huskd's own metrics — the `/metrics` exposition, in two halves.

There are two fundamentally different kinds of number here, and the split runs
through this whole module:

* **Snapshot-derived** (`SnapshotCollector`) — how many slots are idle, what the
  last recycle took, how many bytes of qcow2 are on disk. These describe the
  *present*, and huskd already holds a complete, immutable description of the
  present: the per-pool `ControllerState` the reconcile loop swaps in each tick.
  They are rendered at scrape time straight from that snapshot and are never
  stored here.
* **Event-time** (`Metrics`) — how many rebuilds failed, how long recycles take,
  how often a tick fail-safed. These describe *what happened between scrapes*,
  which no snapshot can express: a rebuild that failed and was retried leaves no
  trace in the current state. They must be recorded as they occur and accumulated
  across ticks, which is what the instruments below do.

Both halves are `prometheus_client` *collectors* registered on one
`CollectorRegistry`, rather than library `Gauge`/`Counter` objects. That is
deliberate, and for two different reasons:

* For the snapshot half, a library `Gauge` would be actively wrong. Its labelsets
  never expire: `Gauge.labels(slot="husk-a-7").set(...)` keeps reporting that slot
  forever after the slot is destroyed, and we would have to hand-roll
  clear-and-repopulate every tick to avoid it. A collector reads the current
  snapshot, so a slot that is gone simply produces no sample and Prometheus's own
  staleness handling does the rest — which is exactly the behaviour we want.
* For the event-time half, we need the accumulated values to survive a huskd
  restart (see `husk.metrics_store`), and reading/writing them through
  `prometheus_client`'s internal `Value` objects would mean depending on its
  private API. Backing them with the plain dicts below makes persistence a
  straight `to_dict()`/`load_dict()`.

What the library still does for us is the part that is genuinely fiddly and easy
to get subtly wrong by hand: label escaping (a runner named `foo"bar` used to
produce a malformed line that failed the *entire* scrape, not just its own
series), metric-name and type validation, `le` bucket labels with `+Inf` and the
matching `_sum`/`_count`, and the `_total` suffix convention.

Cardinality rule, enforced by construction: **no event-time instrument carries a
per-slot label.** Every label value below comes from config (pool names) or a
fixed vocabulary (action, reason, phase), so the series count is bounded and the
persisted state file stays small. Per-slot detail lives only in the snapshot half,
where it expires on its own.
"""

from __future__ import annotations

import bisect
import logging
from typing import Iterable, Iterator, Sequence

from prometheus_client.metrics_core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)

from husk.slot import SlotState
from husk.snapshot import ControllerState
from husk.storage import DiskUsage

log = logging.getLogger("husk.metrics")

# The classified state as a small integer, for `husk_slot_state_code`. A
# state-timeline panel colours a lane by a numeric field value, so the state has
# to reach Grafana as a number; declaration order in `SlotState` is the encoding
# and the dashboard's value mappings mirror it 1:1. Derived rather than written
# out so a new state cannot be added to the enum and forgotten here — it gets a
# code automatically, and only the dashboard mapping needs the new name. Codes
# start at 1 so that 0 stays free as "no such state" if anything ever needs it.
_STATE_CODE = {s.value: i for i, s in enumerate(SlotState, start=1)}

# Bucket boundaries (seconds), in upper-bound order and WITHOUT the implicit
# +Inf. The library defaults top out at 10s, which is useless for everything
# husk measures — a slot bring-up is a minute or more.
#
# Changing any of these invalidates previously persisted data for that metric;
# `husk.metrics_store` detects that by comparing bounds and drops the stale
# series rather than silently mixing two bucket layouts.
BRINGUP_BUCKETS = (15, 30, 45, 60, 75, 90, 120, 150, 180, 240, 300, 600)
TICK_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

Labels = tuple[str, ...]


class Counter:
    """A monotonic count, keyed by labelset.

    Deliberately minimal: `inc` and the additive `load_dict` are the only
    mutations, and neither can lower a value — so within one process a counter is
    monotonic by construction. The only decrease Prometheus can ever observe is a
    restart that finds no (or an older) saved state, which is exactly the counter
    reset `rate()` already handles."""

    def __init__(self, name: str, doc: str, labels: Sequence[str]) -> None:
        self.name = name
        self.doc = doc
        self.labels = tuple(labels)
        self._values: dict[Labels, float] = {}

    def inc(self, *labelvalues: str, amount: float = 1.0) -> None:
        key = self._key(labelvalues)
        self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, *labelvalues: str) -> float:
        """The current total for one labelset (0.0 if never incremented). For
        tests and for `metrics_store`; the exposition goes through `collect`."""
        return self._values.get(self._key(labelvalues), 0.0)

    def _key(self, labelvalues: Sequence[str]) -> Labels:
        if len(labelvalues) != len(self.labels):
            raise ValueError(
                f"{self.name} takes {len(self.labels)} label(s) {self.labels}, "
                f"got {len(labelvalues)}"
            )
        return tuple(str(v) for v in labelvalues)

    def collect(self) -> Iterator[CounterMetricFamily]:
        fam = CounterMetricFamily(self.name, self.doc, labels=self.labels)
        for key, value in sorted(self._values.items()):
            fam.add_metric(list(key), value)
        yield fam

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        return {
            "type": "counter",
            "labels": list(self.labels),
            "values": [
                {"labels": list(k), "value": v} for k, v in self._values.items()
            ],
        }

    def load_dict(self, d: dict) -> None:
        """Restore saved totals. A labelset whose arity no longer matches the
        current definition is dropped — the metric was redefined between runs,
        and a wrong-width labelset would raise at collect time."""
        if tuple(d.get("labels", ())) != self.labels:
            log.warning(
                "%s: labels changed since save; discarding stored data", self.name
            )
            return
        for row in d.get("values", []):
            key = tuple(str(v) for v in row["labels"])
            if len(key) == len(self.labels):
                self._values[key] = self._values.get(key, 0.0) + float(row["value"])


class Histogram:
    """An observation distribution, keyed by labelset.

    Stores per-labelset *non-cumulative* bucket counts plus a running sum; the
    cumulative `le` series Prometheus wants are built at collect time. Buckets
    are `self.buckets` upper bounds plus one overflow slot for +Inf, so the
    counts list is always `len(buckets) + 1` wide."""

    def __init__(
        self, name: str, doc: str, labels: Sequence[str], buckets: Sequence[float]
    ) -> None:
        self.name = name
        self.doc = doc
        self.labels = tuple(labels)
        self.buckets = tuple(float(b) for b in buckets)
        if list(self.buckets) != sorted(self.buckets):
            raise ValueError(f"{name}: buckets must be in ascending order")
        self._counts: dict[Labels, list[float]] = {}
        self._sums: dict[Labels, float] = {}

    def observe(self, value: float, *labelvalues: str) -> None:
        key = self._key(labelvalues)
        counts = self._counts.setdefault(key, [0.0] * (len(self.buckets) + 1))
        # bisect_left, not bisect_right: Prometheus buckets are `le` (less than
        # or *equal*), so an observation exactly on a boundary belongs to that
        # bucket rather than the next one up.
        counts[bisect.bisect_left(self.buckets, value)] += 1.0
        self._sums[key] = self._sums.get(key, 0.0) + value

    def count(self, *labelvalues: str) -> float:
        """Total observations for one labelset — for tests and `metrics_store`."""
        return sum(self._counts.get(self._key(labelvalues), ()))

    def sum(self, *labelvalues: str) -> float:
        return self._sums.get(self._key(labelvalues), 0.0)

    def _key(self, labelvalues: Sequence[str]) -> Labels:
        if len(labelvalues) != len(self.labels):
            raise ValueError(
                f"{self.name} takes {len(self.labels)} label(s) {self.labels}, "
                f"got {len(labelvalues)}"
            )
        return tuple(str(v) for v in labelvalues)

    def collect(self) -> Iterator[HistogramMetricFamily]:
        fam = HistogramMetricFamily(self.name, self.doc, labels=self.labels)
        bounds = [*(str(float(b)) for b in self.buckets), "+Inf"]
        for key, counts in sorted(self._counts.items()):
            cumulative, running = [], 0.0
            for bound, n in zip(bounds, counts):
                running += n
                cumulative.append((bound, running))
            fam.add_metric(list(key), cumulative, sum_value=self._sums.get(key, 0.0))
        yield fam

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        return {
            "type": "histogram",
            "labels": list(self.labels),
            "buckets": list(self.buckets),
            "values": [
                {"labels": list(k), "counts": list(c), "sum": self._sums.get(k, 0.0)}
                for k, c in self._counts.items()
            ],
        }

    def load_dict(self, d: dict) -> None:
        """Restore saved observations. Stored data is discarded outright if either
        the labels or the *bucket boundaries* changed since it was written —
        folding counts into differently-bounded buckets would silently produce a
        distribution that never existed."""
        if tuple(d.get("labels", ())) != self.labels:
            log.warning(
                "%s: labels changed since save; discarding stored data", self.name
            )
            return
        if tuple(float(b) for b in d.get("buckets", ())) != self.buckets:
            log.warning(
                "%s: buckets changed since save; discarding stored data", self.name
            )
            return
        for row in d.get("values", []):
            key = tuple(str(v) for v in row["labels"])
            counts = [float(c) for c in row["counts"]]
            if len(key) != len(self.labels) or len(counts) != len(self.buckets) + 1:
                continue
            into = self._counts.setdefault(key, [0.0] * (len(self.buckets) + 1))
            for i, n in enumerate(counts):
                into[i] += n
            self._sums[key] = self._sums.get(key, 0.0) + float(row["sum"])


class Metrics:
    """The event-time instruments, and a collector over them.

    One instance per daemon, handed to every `Controller` and to the poller. Each
    instrument is labelled by `backend` (the husk pool name) where a value is
    per-pool, so a multi-pool huskd keeps its pools separable.

    Constructing this is cheap and side-effect-free, which is what lets
    `Controller` default to a private instance: a test (or `huskctl`) that builds
    a controller without caring about metrics still exercises every instrumented
    code path, it just throws the numbers away."""

    def __init__(self) -> None:
        self.reconcile_ticks = Counter(
            "husk_reconcile_ticks",
            "Reconcile ticks that ran to completion",
            ["backend"],
        )
        # A tick that fail-safes is the single most important thing to alert on:
        # huskd is up and scraping fine, but it has stopped acting on reality.
        # `reason` distinguishes a backend listing failure from a stale/absent
        # GitHub runner snapshot, which have completely different fixes.
        self.reconcile_aborts = Counter(
            "husk_reconcile_aborts",
            "Ticks aborted before any mutation (fail-safe), by reason",
            ["backend", "reason"],
        )
        self.reconcile_duration = Histogram(
            "husk_reconcile_duration_seconds",
            "Wall-clock duration of one reconcile tick",
            ["backend"],
            TICK_BUCKETS,
        )
        # Every non-fatal action failure the controller records, counted rather
        # than only pinned to a slot for the dashboard. `action` is the verb only
        # (rebuild/create/destroy/start/stop/delete_runner/…) — never the slot id.
        self.action_failures = Counter(
            "husk_action_failures",
            "Backend/GitHub actions that failed, by action",
            ["backend", "action"],
        )
        self.slots_created = Counter("husk_slots_created", "Slots created", ["backend"])
        self.slots_destroyed = Counter(
            "husk_slots_destroyed", "Slots destroyed, by reason", ["backend", "reason"]
        )
        self.slot_recycles = Counter(
            "husk_slot_recycles", "Slot rebuilds issued", ["backend"]
        )
        # The distributions the per-slot "last value" gauges cannot give you:
        # a gauge answers "how slow is this slot right now", these answer "what
        # is the p95 over the last day, and did it move when we bumped the image".
        self.recycle_duration = Histogram(
            "husk_recycle_duration_seconds",
            "Rebuild issued to runner online (whole bring-up)",
            ["backend"],
            BRINGUP_BUCKETS,
        )
        self.cloudinit_duration = Histogram(
            "husk_cloudinit_duration_seconds",
            "Slot ACTIVE to runner online (the cloud-init step)",
            ["backend"],
            BRINGUP_BUCKETS,
        )
        self.github_polls = Counter(
            "husk_github_polls", "Runner-listing polls attempted", ["target"]
        )
        self.github_poll_failures = Counter(
            "husk_github_poll_failures",
            "Runner-listing polls that failed (last snapshot kept)",
            ["target"],
        )
        self.guest_scrape_failures = Counter(
            "husk_guest_scrape_failures",
            "Proxied libvirt guest metric scrapes that failed",
            ["backend"],
        )
        # Webhook deliveries, by outcome. `result` is a CLOSED vocabulary —
        # accepted / rejected / ignored / malformed — deliberately carrying no
        # repo, job, workflow or sender label: this endpoint is internet-facing,
        # so anything derived from the request body is attacker-chosen and would
        # let a stranger mint unbounded series in a counter huskd PERSISTS to
        # disk. `rejected` (bad signature) is the one to alert on: a nonzero rate
        # means either a rotated secret huskd has not been restarted for, or
        # someone probing the endpoint.
        self.webhook_deliveries = Counter(
            "husk_webhook_deliveries",
            "workflow_job webhook deliveries, by result",
            ["result"],
        )
        self._instruments: tuple[Counter | Histogram, ...] = (
            self.reconcile_ticks,
            self.reconcile_aborts,
            self.reconcile_duration,
            self.action_failures,
            self.slots_created,
            self.slots_destroyed,
            self.slot_recycles,
            self.recycle_duration,
            self.cloudinit_duration,
            self.github_polls,
            self.github_poll_failures,
            self.guest_scrape_failures,
            self.webhook_deliveries,
        )

    @property
    def instruments(self) -> dict[str, Counter | Histogram]:
        """Instruments by metric name — the seam `husk.metrics_store` persists."""
        return {i.name: i for i in self._instruments}

    def collect(self) -> Iterator:
        for instrument in self._instruments:
            yield from instrument.collect()


class SnapshotCollector:
    """The snapshot-derived half: everything renderable from the current state.

    Reads the same in-memory providers every other endpoint reads — a 0-arg
    callable returning the per-pool `ControllerState` list, and one returning
    daemon-wide qcow2 usage — so a scrape never touches a backend and always sees
    a complete, immutable state.

    Per-pool series are distinguished by the `backend` label, so emitting every
    pool from one collector is a valid exposition. Storage is emitted *once*, not
    per pool: the controller cache is shared by every pool and two libvirt pools
    can share a hypervisor's storage dir, so a `backend` label there would make
    `sum(husk_image_bytes)` double-count. `storage.collect` has already deduped by
    (host, kind)."""

    def __init__(self, snapshots, storage=None, jobs=None) -> None:
        self._snapshots = snapshots
        self._storage = storage
        # Optional `JobRegistry`. Queue depth belongs in THIS half rather than
        # among the event-time counters for the usual reason: a `runs-on` labelset
        # that stops being used must stop producing samples, and a collector that
        # reads the live registry gets that for free where a library Gauge would
        # report a drained queue forever.
        self._jobs = jobs

    def collect(self) -> Iterator:
        snaps = self._snapshots() or []
        yield from self._storage_families()
        yield from self._pool_families(snaps)
        yield from self._slot_families(snaps)
        yield from self._queue_families()

    # ------------------------------------------------------------------ queue
    def _queue_families(self) -> Iterator:
        """Jobs waiting for a runner, per (target, runs-on labelset).

        Observability only — nothing in huskd sizes off this (see husk.webhook on
        why). It answers "is the queue draining", which `husk_slots{state="busy"}`
        cannot: a saturated fleet and an idle one both read 100% busy when the
        queue behind them is 0 or 200 deep.

        Empty whenever no webhook is configured, which is a truthful zero-series
        rather than a zero: with no deliveries huskd genuinely does not know the
        queue depth, and reporting 0 would look like an empty queue.
        """
        if self._jobs is None:
            return
        depth = GaugeMetricFamily(
            "husk_jobs_queued",
            "Actions jobs queued and not yet assigned a runner",
            labels=["target", "labels"],
        )
        for (target, labelset), n in sorted(self._jobs.queued_depth().items()):
            # The labelset is joined into ONE label value rather than exploded
            # into columns: `runs-on` is a set of arbitrary length, so a column
            # per label is not expressible in a fixed schema, and the joined form
            # is what a `=~` query matches against anyway.
            depth.add_metric([target, ",".join(labelset)], n)
        yield depth

    # ------------------------------------------------------------------ pools
    def _pool_families(self, snaps: list[ControllerState]) -> Iterator:
        slots = GaugeMetricFamily(
            "husk_slots", "Slots by classified state", labels=["backend", "state"]
        )
        desired = GaugeMetricFamily(
            "husk_slots_desired", "Desired total slots", labels=["backend"]
        )
        min_ready = GaugeMetricFamily(
            "husk_slots_min_ready", "Configured min_ready", labels=["backend"]
        )
        max_total = GaugeMetricFamily(
            "husk_slots_max_total", "Configured max_total", labels=["backend"]
        )
        last = GaugeMetricFamily(
            "husk_last_reconcile_timestamp_seconds",
            "Unix time of the last reconcile",
            labels=["backend"],
        )
        # Typed as a counter (and so exposed as `..._total`) because that is what
        # it is — a monotonic tick count. It was previously named without the
        # suffix while declaring TYPE counter, which is a convention violation
        # some tooling keys off.
        generation = CounterMetricFamily(
            "husk_reconcile_generation",
            "Monotonic reconcile counter",
            labels=["backend"],
        )
        # The pool's CONFIGURED target image, carried as a label on an always-1
        # gauge (the Prometheus info-metric idiom). It is what makes "did p95
        # recycle move when we bumped the image" answerable: bumping the ref and
        # restarting retires this series and starts a new one, so the image change
        # is a visible time boundary to correlate the recycle histogram against —
        # which the histogram's own doc cites as the reason it is a histogram.
        # One series per pool, so no cardinality concern. `image_ref` is the full
        # ref; the per-slot ACTIVE image (which may lag during a rollout) is a
        # separate label on husk_slot_info.
        pool_info = GaugeMetricFamily(
            "husk_pool_info",
            "Pool identity: its configured target image (always 1)",
            labels=["backend", "image_ref"],
        )
        for s in snaps:
            b = s.backend
            for state, n in s.counts.items():
                slots.add_metric([b, state], n)
            desired.add_metric([b], s.desired_total)
            min_ready.add_metric([b], s.min_ready)
            max_total.add_metric([b], s.max_total)
            last.add_metric([b], s.last_reconcile_epoch)
            generation.add_metric([b], s.generation)
            pool_info.add_metric([b, s.image_ref or ""], 1)
        yield from (slots, desired, min_ready, max_total, last, generation, pool_info)

    # ------------------------------------------------------------------ slots
    def _slot_families(self, snaps: list[ControllerState]) -> Iterator:
        labels = ["backend", "slot"]
        cloudinit = GaugeMetricFamily(
            "husk_slot_last_cloudinit_seconds",
            "Last ACTIVE->runner-online duration",
            labels=labels,
        )
        recycle = GaugeMetricFamily(
            "husk_slot_last_recycle_seconds",
            "Last issue->runner-online duration",
            labels=labels,
        )
        # `cycle` used to be a *label* on husk_slot_info. It increments on every
        # recycle, so each recycle minted a brand-new series that then went stale
        # — unbounded churn proportional to recycles-over-time, and the exact
        # opposite of the low-cardinality join table the info metric is meant to
        # be. It is a value, so it is exposed as one.
        cycle = GaugeMetricFamily(
            "husk_slot_cycle", "Current recycle cycle of the slot", labels=labels
        )
        # The slot's CURRENT state, encoded as _STATE_CODE. This is deliberately
        # NOT derivable from husk_slot_state_seconds below, and the dashboard used
        # to try: it thresholded each state's rate at >0.5 and summed the ordinals
        # of whichever fired, on the assumption that only one state can hold more
        # than half an interval. rate() extrapolates (a 4x-scrape window samples
        # only 3/4 of itself, so the effective threshold is 37.5%, not 50%), so a
        # slot that spent one window half BUSY and half NEEDS_RECYCLE fired BOTH —
        # and 2+4 is the code for ERROR. Every ordinary recycle painted itself as
        # a state the slot was never in. A time-share counter cannot answer "what
        # is it now", so this gauge answers it instead: one series per slot,
        # exact, expiring with the slot like everything else here.
        state_code = GaugeMetricFamily(
            "husk_slot_state_code",
            "Classified state of the slot, encoded as an integer (see _STATE_CODE)",
            labels=labels,
        )
        # Time-in-state as a counter, replacing the precomputed
        # husk_slot_live_fraction gauge. A ratio baked inside husk fixes the
        # window to "since huskd started" and resets silently on restart; two
        # counters let the *consumer* pick the window:
        #
        #   sum by (backend, slot) (rate(husk_slot_state_seconds_total{
        #       state=~"busy|idle"}[1h]))
        #   / sum by (backend, slot) (rate(husk_slot_state_seconds_total[1h]))
        #
        # This lives here rather than in `Metrics` despite being a counter: it is
        # per-slot, and its accumulator (`SlotTiming.state_seconds`) is owned by
        # the slot, so it must expire when the slot does.
        state_seconds = CounterMetricFamily(
            "husk_slot_state_seconds",
            "Cumulative seconds the slot has spent in each classified state",
            labels=[*labels, "state"],
        )
        # How long the slot's CURRENT run of failed actions has lasted, as measured
        # by the controller at its last tick. Emitted only while failing, so a
        # healthy slot produces no sample and recovery is handled by Prometheus
        # staleness — the same contract as the gauges above.
        #
        # A duration rather than a `broken` boolean on purpose: "broken" is a
        # judgement about how much patience the situation deserves, and huskd has
        # no business fixing that number for every pool. Exporting the elapsed time
        # leaves the threshold in the query, where one alert can say `> 900` while a
        # dashboard panel says `> 60`, with no config knob to keep in sync.
        failing = GaugeMetricFamily(
            "husk_slot_failing_seconds",
            "Duration of the slot's current run of consecutive failed actions",
            labels=labels,
        )
        # Companion count for the same run. Not a Counter: it RESETS to absent on
        # the first success, which is the semantics of a gauge, and rate() over it
        # would be meaningless anyway.
        failures = GaugeMetricFamily(
            "husk_slot_failure_streak",
            "Consecutive failed actions on the slot since the last success",
            labels=labels,
        )
        # `image` is the slot's ACTIVE image (short digest, or the tag when the
        # slot is on the pool's current target), and `image_stale` flags a slot
        # still running a prior image. Together they turn a rollout into something
        # observable per slot: `count by (image) (husk_slot_info)` is the drain
        # curve, and `husk_slot_info{image_stale="true"}` is exactly the slots not
        # yet cycled. The pool's *configured* target is husk_pool_info.
        #
        # `flavor_stale` is the OpenStack analog for a running server's flavor,
        # but — unlike image_stale — it is visibility ONLY: Nova's rebuild action
        # (what recycle issues) cannot change a server's flavor, only its image.
        # `husk_slot_info{flavor_stale="true"}` names slots an operator needs to
        # destroy+recreate by hand; nothing here drains them automatically.
        #
        # `failing` joins that family: it is a FACT (the last action on this slot
        # failed), not the judgement "broken" — which stays a threshold on
        # husk_slot_failing_seconds. It earns a label rather than only a series
        # because the state panels are already built on this metric, so a broken
        # slot can be annotated there with a join instead of a redesign.
        info = GaugeMetricFamily(
            "husk_slot_info",
            "Slot identity for joining in-guest metrics (always 1)",
            labels=[
                *labels,
                "ip",
                "host",
                "runner",
                "image",
                "image_stale",
                "flavor_stale",
                "failing",
            ],
        )
        for s in snaps:
            b = s.backend
            for v in s.slots:
                key = [b, v.name]
                # Emit only when a value exists, so a never-recycled slot does not
                # report a bogus 0.
                if v.cloudinit_seconds is not None:
                    cloudinit.add_metric(key, v.cloudinit_seconds)
                if v.recycle_seconds is not None:
                    recycle.add_metric(key, v.recycle_seconds)
                cycle.add_metric(key, v.cycle)
                code = _STATE_CODE.get(v.state)
                if code is not None:
                    state_code.add_metric(key, code)
                for state, secs in v.state_seconds.items():
                    state_seconds.add_metric([*key, state], secs)
                if v.failing_seconds is not None:
                    failing.add_metric(key, v.failing_seconds)
                    failures.add_metric(key, v.failure_count)
                info.add_metric(
                    [
                        *key,
                        v.ip or "",
                        v.host or "",
                        v.runner or "",
                        v.image or "",
                        "true" if v.image_stale else "false",
                        "true" if v.flavor_stale else "false",
                        "true" if v.failing_seconds is not None else "false",
                    ],
                    1,
                )
        yield from (
            cloudinit,
            recycle,
            cycle,
            state_code,
            state_seconds,
            failing,
            failures,
            info,
        )

    # ---------------------------------------------------------------- storage
    def _storage_families(self) -> Iterator:
        images = GaugeMetricFamily(
            "husk_images", "Stored qcow2 images by location", labels=["kind", "host"]
        )
        image_bytes = GaugeMetricFamily(
            "husk_image_bytes",
            "Total size of stored qcow2 images by location",
            labels=["kind", "host"],
        )
        # How much room is LEFT, which is the number that predicts an outage —
        # husk's own footprint can be flat while the volume fills for other
        # reasons, and on k8s the cache is its own PVC whose capacity husk cannot
        # otherwise see. Named after node_exporter's equivalents so the alerting
        # idiom carries over.
        #
        # These describe the *filesystem* behind a location, so two `kind`s sharing
        # one disk would report identical numbers: aggregate with max()/avg(),
        # never sum(). (Today only kind="cache" reports them at all — the backends
        # do not measure host filesystems yet.)
        fs_size = GaugeMetricFamily(
            "husk_filesystem_size_bytes",
            "Capacity of the filesystem holding these images",
            labels=["kind", "host"],
        )
        fs_avail = GaugeMetricFamily(
            "husk_filesystem_avail_bytes",
            "Space available to huskd on that filesystem",
            labels=["kind", "host"],
        )
        usage: Iterable[DiskUsage] = (self._storage() if self._storage else None) or []
        for u in usage:
            images.add_metric([u.kind, u.host], u.images)
            image_bytes.add_metric([u.kind, u.host], u.total_bytes)
            # Omitted rather than emitted as 0 where unmeasured: a 0-capacity
            # filesystem would read as "completely full" to any sane alert.
            if u.fs_size_bytes is not None:
                fs_size.add_metric([u.kind, u.host], u.fs_size_bytes)
            if u.fs_avail_bytes is not None:
                fs_avail.add_metric([u.kind, u.host], u.fs_avail_bytes)
        yield from (images, image_bytes, fs_size, fs_avail)
