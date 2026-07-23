"""The /metrics exposition: the accumulator primitives, the snapshot-derived
collector, and the invariants that make the whole thing safe to scrape.

Assertions run against the *serialized* exposition wherever it matters, because
that is the actual contract with Prometheus — and because the failures worth
guarding against here (a label that breaks parsing, a series that never expires)
only exist after serialization."""

from __future__ import annotations

import pytest
from prometheus_client.parser import text_string_to_metric_families

from conftest import make_runner, make_slot, render_metrics
from husk.metrics import Counter, Histogram, Metrics
from husk.slot import SlotState
from husk.snapshot import ControllerState
from husk.timing import SlotTiming


def _snap(*classified, backend="pool-a", timing=None, generation=1, image_ref=""):
    return ControllerState.from_classified(
        generation=generation,
        backend=backend,
        min_ready=1,
        max_total=4,
        desired_total=1,
        classified=list(classified),
        timing=timing,
        image_ref=image_ref,
    )


def _one(name="husk-a-1", *, runner="husk-a-1-c2", cycle=2, ip=None, host=None):
    return (
        make_slot(id="vm-1", name=name, cycle=cycle, ip=ip, host=host),
        make_runner(name=runner, status="online"),
        SlotState.IDLE,
    )


def _samples(text: str, metric: str) -> dict[tuple, float]:
    """Parse the exposition and pull one metric's samples out by name, keyed by
    its sorted labels. Parsing rather than substring-matching is the point: a
    malformed line makes this raise instead of silently not matching."""
    out = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            if s.name == metric:
                out[tuple(sorted(s.labels.items()))] = s.value
    return out


# ------------------------------------------------------------------ primitives
def test_counter_accumulates_per_labelset():
    c = Counter("husk_test", "doc", ["pool", "action"])
    c.inc("a", "rebuild")
    c.inc("a", "rebuild")
    c.inc("b", "rebuild")

    assert c.value("a", "rebuild") == 2.0
    assert c.value("b", "rebuild") == 1.0
    assert c.value("a", "create") == 0.0  # never touched


def test_counter_rejects_a_wrong_width_labelset():
    """Loudly, at the call site. A silently-accepted short labelset would blow up
    much later inside collect(), during a scrape, far from the cause."""
    c = Counter("husk_test", "doc", ["pool", "action"])

    with pytest.raises(ValueError, match="takes 2 label"):
        c.inc("a")


def test_histogram_buckets_are_le_not_lt():
    """A value exactly on a boundary belongs to that bucket — Prometheus buckets
    are `le`. Getting this backwards shifts every on-the-nose observation one
    bucket high, which is invisible until someone reads a quantile."""
    h = Histogram("husk_test_seconds", "doc", ["pool"], (10, 20))
    h.observe(10.0, "a")  # exactly on the first bound -> le=10
    h.observe(10.1, "a")  # just over -> le=20
    h.observe(999.0, "a")  # over everything -> +Inf only

    text = _render_only(h)
    buckets = _samples(text, "husk_test_seconds_bucket")
    assert buckets[(("le", "10.0"), ("pool", "a"))] == 1
    assert buckets[(("le", "20.0"), ("pool", "a"))] == 2  # cumulative
    assert buckets[(("le", "+Inf"), ("pool", "a"))] == 3


def test_histogram_exposes_sum_and_count():
    h = Histogram("husk_test_seconds", "doc", ["pool"], (10, 20))
    h.observe(5.0, "a")
    h.observe(15.0, "a")

    text = _render_only(h)
    assert _samples(text, "husk_test_seconds_sum")[(("pool", "a"),)] == 20.0
    assert _samples(text, "husk_test_seconds_count")[(("pool", "a"),)] == 2.0


def test_histogram_rejects_unsorted_buckets():
    with pytest.raises(ValueError, match="ascending"):
        Histogram("husk_test_seconds", "doc", ["pool"], (10, 5))


def _render_only(instrument) -> str:
    from prometheus_client import CollectorRegistry, generate_latest

    class _C:
        def collect(self):
            return list(instrument.collect())

    registry = CollectorRegistry()
    registry.register(_C())
    return generate_latest(registry).decode()


# --------------------------------------------------------------- escaping
def test_hostile_label_values_do_not_break_the_scrape():
    """A runner name is chosen by whoever configures the runner, and GitHub allows
    quotes and backslashes in it. Interpolated raw, one such name produced a
    malformed line that made Prometheus reject the ENTIRE scrape — every husk
    metric would go dark because of one badly-named runner."""
    text = render_metrics(
        [_snap(_one(name='slot"quote', runner="back\\slash\nnewline"))]
    )

    # The strong assertion: the whole document still parses.
    info = _samples(text, "husk_slot_info")
    assert (
        info[
            (
                ("backend", "pool-a"),
                ("host", ""),
                ("image", ""),
                ("image_stale", "false"),
                ("ip", ""),
                ("runner", "back\\slash\nnewline"),
                ("slot", 'slot"quote'),
            )
        ]
        == 1.0
    )


# ------------------------------------------------------------------- naming
def test_reconcile_generation_is_a_counter_named_total():
    """It was typed `counter` while named without the `_total` suffix, which is a
    convention violation some tooling keys off."""
    text = render_metrics([_snap(_one(), generation=7)])

    assert "# TYPE husk_reconcile_generation_total counter" in text
    assert (
        _samples(text, "husk_reconcile_generation_total")[(("backend", "pool-a"),)]
        == 7.0
    )


# -------------------------------------------------------------- cardinality
def test_cycle_is_a_value_not_a_label():
    """`cycle` increments on every recycle. As a label on the join table it minted
    a brand-new series per recycle, which then went stale — unbounded churn
    proportional to recycles-over-time."""
    text = render_metrics([_snap(_one(cycle=5))])

    assert "cycle=" not in text
    assert (
        _samples(text, "husk_slot_cycle")[(("backend", "pool-a"), ("slot", "husk-a-1"))]
        == 5.0
    )


def test_no_event_time_instrument_carries_a_per_slot_label():
    """The guard that keeps the persisted state file bounded and the series count
    proportional to config rather than to fleet churn. Per-slot detail belongs in
    the snapshot half, where it expires when the slot does."""
    forbidden = {"slot", "runner", "vm", "id", "ip"}
    for name, instrument in Metrics().instruments.items():
        assert not forbidden & set(instrument.labels), name


def test_pool_info_carries_the_configured_image_ref():
    """The pool's target image is exposed as a label on an always-1 gauge, so a
    ref bump is a visible time boundary to correlate recycle timings against."""
    snap = _snap(_one(), image_ref="ghcr.io/acts-project/husk-base:v7")
    info = _samples(render_metrics([snap]), "husk_pool_info")
    # The FULL ref, verbatim — the pool-level metric names the configured target,
    # not the abbreviated per-slot form.
    assert info == {
        (
            ("backend", "pool-a"),
            ("image_ref", "ghcr.io/acts-project/husk-base:v7"),
        ): 1.0
    }


def test_slot_info_exposes_active_image_and_staleness():
    """A rollout is observable per slot: the current slot names the pool's tag and
    is not stale; a slot on a prior image is flagged and falls back to its digest.
    `count by (image)` over these is the drain curve."""
    current = (
        make_slot(id="vm-1", name="husk-a-1", cycle=3),
        make_runner(name="r1", status="online"),
        SlotState.IDLE,
    )
    lagging = (
        make_slot(
            id="vm-2",
            name="husk-a-2",
            cycle=2,
            image_stale=True,
            active_image="sha256:deadbeefcafe0000",
        ),
        make_runner(name="r2", status="online"),
        SlotState.IDLE,
    )
    snap = _snap(current, lagging, image_ref="ghcr.io/acts-project/husk-base:v7")
    info = _samples(render_metrics([snap]), "husk_slot_info")

    current_labels = next(k for k in info if dict(k)["slot"] == "husk-a-1")
    assert dict(current_labels)["image"] == "v7"
    assert dict(current_labels)["image_stale"] == "false"

    stale_labels = next(k for k in info if dict(k)["slot"] == "husk-a-2")
    assert dict(stale_labels)["image"] == "deadbeefcafe"  # short digest, no tag
    assert dict(stale_labels)["image_stale"] == "true"


def test_a_destroyed_slot_stops_being_reported():
    """The reason the snapshot half is a collector and not a library Gauge: a
    Gauge's labelsets never expire, so a destroyed slot would keep reporting its
    final value forever."""
    snaps = [_snap(_one())]
    assert _samples(render_metrics(snaps), "husk_slot_info")

    snaps = [_snap()]  # slot gone from the next tick's snapshot
    assert _samples(render_metrics(snaps), "husk_slot_info") == {}


# ------------------------------------------------------- time-in-state counter
def test_state_seconds_replaces_the_precomputed_live_fraction():
    """A ratio computed inside husk fixes the window to "since huskd started" and
    resets silently on restart. Two counters let the query pick its own window."""
    t = SlotTiming(first_seen=0.0)
    t.accumulate(SlotState.BUSY, 30.0)
    t.accumulate(SlotState.IDLE, 10.0)
    t.accumulate(SlotState.STARTING, 60.0)

    text = render_metrics([_snap(_one(), timing={"vm-1": t})])

    seconds = _samples(text, "husk_slot_state_seconds_total")

    def key(state):
        return (("backend", "pool-a"), ("slot", "husk-a-1"), ("state", state))

    assert seconds[key("busy")] == 30.0
    assert seconds[key("idle")] == 10.0
    assert seconds[key("starting")] == 60.0
    # ...and the ratio itself is no longer exposed as a gauge.
    assert "husk_slot_live_fraction" not in text


def test_live_fraction_is_still_published_for_the_dashboard():
    """It left /metrics, not /status — the dashboard wants one precomputed number
    and has no PromQL to divide with."""
    t = SlotTiming(first_seen=0.0)
    t.accumulate(SlotState.BUSY, 30.0)
    t.accumulate(SlotState.STARTING, 10.0)

    snap = _snap(_one(), timing={"vm-1": t})

    assert snap.slots[0].live_fraction == 0.75


# -------------------------------------------------------------- event metrics
def test_event_metrics_are_absent_until_something_happens():
    """`Metrics` is optional on make_app: huskctl and most tests want only the
    snapshot half, and an unregistered instrument set must not appear at all."""
    assert "husk_action_failures_total" not in render_metrics([_snap(_one())])


def test_event_metrics_render_alongside_the_snapshot():
    m = Metrics()
    m.action_failures.inc("pool-a", "rebuild")
    m.action_failures.inc("pool-a", "rebuild")
    m.reconcile_aborts.inc("pool-a", "list_slots")
    m.recycle_duration.observe(72.0, "pool-a")

    text = render_metrics([_snap(_one())], metrics=m)

    assert (
        _samples(text, "husk_action_failures_total")[
            (("action", "rebuild"), ("backend", "pool-a"))
        ]
        == 2.0
    )
    assert (
        _samples(text, "husk_reconcile_aborts_total")[
            (("backend", "pool-a"), ("reason", "list_slots"))
        ]
        == 1.0
    )
    assert (
        _samples(text, "husk_recycle_duration_seconds_sum")[(("backend", "pool-a"),)]
        == 72.0
    )
    # The two halves coexist in one exposition.
    assert _samples(text, "husk_slot_info")
