"""Persistence of the event-time counters/histograms across a huskd restart.

The behaviours worth pinning are the failure ones: huskd restarts on every config
change, so this path runs often, and a bookkeeping file must never be able to take
down a runner fleet or silently corrupt a long-horizon query."""

from __future__ import annotations

import json
import os

from husk.metrics import Metrics
from husk.metrics_store import SCHEMA_VERSION, MetricsStore


def _populated() -> Metrics:
    m = Metrics()
    m.action_failures.inc("pool-a", "rebuild")
    m.action_failures.inc("pool-a", "rebuild")
    m.action_failures.inc("pool-b", "create")
    m.slots_created.inc("pool-a")
    m.recycle_duration.observe(72.0, "pool-a")
    m.recycle_duration.observe(95.0, "pool-a")
    return m


def _roundtrip(tmp_path, source: Metrics) -> Metrics:
    path = str(tmp_path / "metrics.json")
    assert MetricsStore(path, source).save()
    restored = Metrics()
    MetricsStore(path, restored).load()
    return restored


# ------------------------------------------------------------------ happy path
def test_counters_and_histograms_survive_a_restart(tmp_path):
    restored = _roundtrip(tmp_path, _populated())

    assert restored.action_failures.value("pool-a", "rebuild") == 2.0
    assert restored.action_failures.value("pool-b", "create") == 1.0
    assert restored.slots_created.value("pool-a") == 1.0
    assert restored.recycle_duration.count("pool-a") == 2.0
    assert restored.recycle_duration.sum("pool-a") == 167.0


def test_restored_histogram_keeps_its_bucket_distribution(tmp_path):
    """Not just the sum/count: a restored p95 has to be the same p95."""
    source = Metrics()
    for value in (20.0, 20.0, 300.0):
        source.recycle_duration.observe(value, "pool-a")

    restored = _roundtrip(tmp_path, source)

    before = list(source.recycle_duration.collect())[0].samples
    after = list(restored.recycle_duration.collect())[0].samples
    assert [(s.name, s.labels, s.value) for s in before] == [
        (s.name, s.labels, s.value) for s in after
    ]


def test_loading_is_additive_so_a_restart_never_loses_the_first_tick(tmp_path):
    """`load` folds saved totals into whatever is already there rather than
    replacing it, so a counter incremented before the load still counts."""
    path = str(tmp_path / "metrics.json")
    MetricsStore(path, _populated()).save()

    live = Metrics()
    live.slots_created.inc("pool-a")  # happened before the restore
    MetricsStore(path, live).load()

    assert live.slots_created.value("pool-a") == 2.0


def test_a_missing_file_is_the_normal_first_run(tmp_path):
    m = Metrics()
    assert MetricsStore(str(tmp_path / "nope.json"), m).load() is False
    assert m.slots_created.value("pool-a") == 0.0


# --------------------------------------------------------------- failure modes
def test_corrupt_json_starts_from_zero_instead_of_raising(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text("{not json at all")

    assert MetricsStore(str(path), Metrics()).load() is False


def test_a_truncated_write_cannot_be_observed(tmp_path):
    """Writes go to a temp file in the same directory and are moved into place, so
    a pod killed mid-save leaves the previous file intact rather than a partial
    one that fails to parse on the next boot."""
    path = str(tmp_path / "metrics.json")
    MetricsStore(path, _populated()).save()
    MetricsStore(path, _populated()).save()

    # Same directory, so os.replace is atomic (it is not across filesystems), and
    # no temp files are left behind to fill a small PVC.
    assert os.listdir(tmp_path) == ["metrics.json"]
    json.loads(open(path).read())


def test_a_schema_bump_discards_rather_than_migrates(tmp_path):
    """huskd has no back-compat obligations, and half-restored data is worse than
    a clean reset: a counter that silently loses part of its history is
    indistinguishable from one that is simply low."""
    path = tmp_path / "metrics.json"
    MetricsStore(str(path), _populated()).save()
    doc = json.loads(path.read_text())
    doc["version"] = SCHEMA_VERSION + 1
    path.write_text(json.dumps(doc))

    restored = Metrics()
    assert MetricsStore(str(path), restored).load() is False
    assert restored.action_failures.value("pool-a", "rebuild") == 0.0


def test_changed_bucket_bounds_discard_that_metric_only(tmp_path):
    """Folding old counts into differently-bounded buckets would produce a
    distribution that never existed. The rest of the file is still usable."""
    path = tmp_path / "metrics.json"
    MetricsStore(str(path), _populated()).save()
    doc = json.loads(path.read_text())
    doc["metrics"]["husk_recycle_duration_seconds"]["buckets"] = [1, 2, 3]
    doc["metrics"]["husk_recycle_duration_seconds"]["values"] = [
        {"labels": ["pool-a"], "counts": [1, 1, 0, 0], "sum": 5.0}
    ]
    path.write_text(json.dumps(doc))

    restored = Metrics()
    MetricsStore(str(path), restored).load()

    assert restored.recycle_duration.count("pool-a") == 0.0  # dropped
    assert restored.action_failures.value("pool-a", "rebuild") == 2.0  # kept


def test_a_removed_metric_in_the_file_is_ignored(tmp_path):
    path = tmp_path / "metrics.json"
    MetricsStore(str(path), _populated()).save()
    doc = json.loads(path.read_text())
    doc["metrics"]["husk_metric_we_deleted"] = {
        "type": "counter",
        "labels": ["backend"],
        "values": [{"labels": ["pool-a"], "value": 9.0}],
    }
    path.write_text(json.dumps(doc))

    restored = Metrics()
    assert MetricsStore(str(path), restored).load() is True
    assert restored.slots_created.value("pool-a") == 1.0


def test_an_unwritable_path_is_reported_not_raised(tmp_path):
    """A full or read-only PVC must not take the daemon down."""
    blocker = tmp_path / "afile"
    blocker.write_text("x")

    store = MetricsStore(str(blocker / "metrics.json"), _populated())

    assert store.save() is False


# ------------------------------------------------------------- the save loop
def test_the_daemon_flushes_once_more_on_shutdown(tmp_path):
    """What makes an ordinary restart lossless: the periodic flush only bounds the
    loss from an *ungraceful* exit, so a clean shutdown has to write again."""
    import asyncio

    from husk.cli import _save_metrics

    path = str(tmp_path / "metrics.json")
    m = Metrics()
    store = MetricsStore(path, m)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(_save_metrics(store, stop))
        await asyncio.sleep(0)
        m.slots_created.inc("pool-a")  # happens long before the 60s interval
        stop.set()
        await task

    asyncio.run(go())

    restored = Metrics()
    MetricsStore(path, restored).load()
    assert restored.slots_created.value("pool-a") == 1.0


def test_the_save_loop_survives_an_unwritable_path(tmp_path):
    """A full or read-only PVC must not kill the task and take the shutdown path
    down with it."""
    import asyncio

    from husk.cli import _save_metrics

    blocker = tmp_path / "afile"
    blocker.write_text("x")
    store = MetricsStore(str(blocker / "metrics.json"), Metrics())

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(_save_metrics(store, stop))
        await asyncio.sleep(0)
        stop.set()
        await task  # must not raise

    asyncio.run(go())


def test_the_state_file_stays_small(tmp_path):
    """No event-time instrument carries a per-slot label, so the file is bounded
    by config rather than by how many slots have ever passed through."""
    m = Metrics()
    for i in range(500):  # 500 recycles across two pools
        m.slot_recycles.inc("pool-a" if i % 2 else "pool-b")
        m.recycle_duration.observe(60.0 + i % 90, "pool-a" if i % 2 else "pool-b")
    path = tmp_path / "metrics.json"
    MetricsStore(str(path), m).save()

    assert path.stat().st_size < 8192
