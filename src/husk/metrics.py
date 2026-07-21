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

from husk.snapshot import ControllerState
from husk.storage import DiskUsage

log = logging.getLogger("husk.metrics")

# Bucket boundaries (seconds), in upper-bound order and WITHOUT the implicit
# +Inf. The library defaults top out at 10s, which is useless for everything
# husk measures — a slot bring-up is a minute or more.
#
# Changing any of these invalidates previously persisted data for that metric;
# `husk.metrics_store` detects that by comparing bounds and drops the stale
# series rather than silently mixing two bucket layouts.
BRINGUP_BUCKETS = (15, 30, 45, 60, 75, 90, 120, 150, 180, 240, 300, 600)
BOOT_BUCKETS = (1, 2, 5, 10, 15, 20, 30, 45, 60, 120)
TICK_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

Labels = tuple[str, ...]


class Counter:
    """A monotonic count, keyed by labelset.

    Deliberately minimal: `inc` is the only mutation, so a counter can never go
    backwards except by a process restart (which Prometheus already models as a
    reset) or by a `load_dict` at startup."""

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
        self.boot_duration = Histogram(
            "husk_boot_phase_seconds",
            "Guest systemd-analyze boot phase durations (husk-bootreport)",
            ["backend", "phase"],
            BOOT_BUCKETS,
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
            self.boot_duration,
            self.github_polls,
            self.github_poll_failures,
            self.guest_scrape_failures,
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

    def __init__(self, snapshots, storage=None) -> None:
        self._snapshots = snapshots
        self._storage = storage

    def collect(self) -> Iterator:
        snaps = self._snapshots() or []
        yield from self._storage_families()
        yield from self._pool_families(snaps)
        yield from self._slot_families(snaps)

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
        for s in snaps:
            b = s.backend
            for state, n in s.counts.items():
                slots.add_metric([b, state], n)
            desired.add_metric([b], s.desired_total)
            min_ready.add_metric([b], s.min_ready)
            max_total.add_metric([b], s.max_total)
            last.add_metric([b], s.last_reconcile_epoch)
            generation.add_metric([b], s.generation)
        yield from (slots, desired, min_ready, max_total, last, generation)

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
        boot = GaugeMetricFamily(
            "husk_slot_boot_seconds",
            "systemd-analyze boot phase durations (husk-bootreport)",
            labels=[*labels, "phase"],
        )
        # `cycle` used to be a *label* on husk_slot_info. It increments on every
        # recycle, so each recycle minted a brand-new series that then went stale
        # — unbounded churn proportional to recycles-over-time, and the exact
        # opposite of the low-cardinality join table the info metric is meant to
        # be. It is a value, so it is exposed as one.
        cycle = GaugeMetricFamily(
            "husk_slot_cycle", "Current recycle cycle of the slot", labels=labels
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
        info = GaugeMetricFamily(
            "husk_slot_info",
            "Slot identity for joining in-guest metrics (always 1)",
            labels=[*labels, "ip", "host", "runner"],
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
                for phase, val in (
                    ("kernel", v.boot_kernel_seconds),
                    ("initrd", v.boot_initrd_seconds),
                    ("userspace", v.boot_userspace_seconds),
                    ("total", v.boot_total_seconds),
                ):
                    if val is not None:
                        boot.add_metric([*key, phase], val)
                cycle.add_metric(key, v.cycle)
                for state, secs in v.state_seconds.items():
                    state_seconds.add_metric([*key, state], secs)
                info.add_metric([*key, v.ip or "", v.host or "", v.runner or ""], 1)
        yield from (cloudinit, recycle, boot, cycle, state_seconds, info)

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
        usage: Iterable[DiskUsage] = (self._storage() if self._storage else None) or []
        for u in usage:
            images.add_metric([u.kind, u.host], u.images)
            image_bytes.add_metric([u.kind, u.host], u.total_bytes)
        yield from (images, image_bytes)
