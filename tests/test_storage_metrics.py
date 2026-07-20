"""qcow2 storage accounting: the controller cache scan, the libvirt per-host
scan, cross-pool dedupe, and the daemon-wide Prometheus block."""

from __future__ import annotations

import os

from husk.image_sync import ImageSync
from husk.storage import CACHE, GOLDEN, OVERLAY, DiskUsage, collect
from husk.web import render_storage_prometheus


def _qcow2(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


# ------------------------------------------------------------ controller cache
def test_cache_usage_counts_and_sums_qcow2(tmp_path):
    cache = str(tmp_path / "cache")
    _qcow2(os.path.join(cache, "sha256-aaa", "base.qcow2"), 100)
    _qcow2(os.path.join(cache, "sha256-bbb", "base.qcow2"), 250)

    usage = ImageSync(cache).cache_usage()

    assert usage == DiskUsage(kind=CACHE, host="", images=2, total_bytes=350)


def test_cache_usage_ignores_in_flight_pulls(tmp_path):
    """A `.pull-*` temp dir is a partial download, not cache content — counting
    it would make the gauge sawtooth during every pull."""
    cache = str(tmp_path / "cache")
    _qcow2(os.path.join(cache, "sha256-aaa", "base.qcow2"), 100)
    _qcow2(os.path.join(cache, ".pull-xyz", "base.qcow2"), 900)

    assert ImageSync(cache).cache_usage().total_bytes == 100


def test_cache_usage_on_missing_dir_is_zero_not_an_error(tmp_path):
    usage = ImageSync(str(tmp_path / "never-pulled")).cache_usage()

    assert (usage.images, usage.total_bytes) == (0, 0)


def test_cache_usage_is_memoized(tmp_path):
    """/metrics reads this, so a scrape storm must not re-scan the disk."""
    cache = str(tmp_path / "cache")
    _qcow2(os.path.join(cache, "sha256-aaa", "base.qcow2"), 100)
    sync = ImageSync(cache)
    assert sync.cache_usage().total_bytes == 100

    _qcow2(os.path.join(cache, "sha256-bbb", "base.qcow2"), 250)

    assert sync.cache_usage().total_bytes == 100  # still the memoized figure


# --------------------------------------------------------------------- collect
class _Backend:
    def __init__(self, rows, boom=False):
        self._rows = rows
        self._boom = boom

    def disk_usage(self):
        if self._boom:
            raise RuntimeError("host unreachable")
        return self._rows


class _Sync:
    def __init__(self, usage=None, boom=False):
        self._usage = usage
        self._boom = boom

    def cache_usage(self):
        if self._boom:
            raise RuntimeError("cache gone")
        return self._usage


def test_collect_dedupes_a_host_shared_by_two_pools():
    """Two libvirt pools on one hypervisor measure the SAME pool dir. Counting
    both would double the bytes for a disk that only filled once."""
    shared = [
        DiskUsage(kind=GOLDEN, host="hv1", images=2, total_bytes=200),
        DiskUsage(kind=OVERLAY, host="hv1", images=4, total_bytes=40),
    ]
    rows = collect(
        _Sync(DiskUsage(CACHE, "", 1, 10)), [_Backend(shared), _Backend(shared)]
    )

    assert rows == [DiskUsage(CACHE, "", 1, 10)] + shared


def test_collect_keeps_distinct_hosts():
    a = [DiskUsage(kind=GOLDEN, host="hv1", images=1, total_bytes=100)]
    b = [DiskUsage(kind=GOLDEN, host="hv2", images=3, total_bytes=300)]

    rows = collect(None, [_Backend(a), _Backend(b)])

    assert [(r.host, r.total_bytes) for r in rows] == [("hv1", 100), ("hv2", 300)]


def test_collect_survives_a_failing_backend_and_cache():
    """This feeds /metrics: one broken source must not fail the whole scrape."""
    ok = [DiskUsage(kind=GOLDEN, host="hv2", images=1, total_bytes=7)]

    rows = collect(_Sync(boom=True), [_Backend(None, boom=True), _Backend(ok)])

    assert rows == ok


# ------------------------------------------------------------------- rendering
def test_render_storage_exposition():
    body = render_storage_prometheus(
        [
            DiskUsage(kind=CACHE, host="", images=2, total_bytes=350),
            DiskUsage(kind=GOLDEN, host="hv1", images=1, total_bytes=100),
        ]
    )

    assert 'husk_images{kind="cache",host=""} 2' in body
    assert 'husk_image_bytes{kind="cache",host=""} 350' in body
    assert 'husk_images{kind="golden",host="hv1"} 1' in body
    assert 'husk_image_bytes{kind="golden",host="hv1"} 100' in body
    # No backend label: these are daemon-wide, not per-pool.
    assert "backend=" not in body
    assert body.count("# TYPE") == 2


def test_render_storage_with_nothing_measured_still_emits_headers():
    body = render_storage_prometheus([])

    assert "# TYPE husk_images gauge" in body
    assert "# TYPE husk_image_bytes gauge" in body
