"""ImageSync: read the qcow2 layer digest from the manifest, pull once, cache
content-addressed. Uses a fake oras client so no registry is needed."""

from __future__ import annotations

import os
import threading
import time

import pytest

from husk.image_sync import ImageSync, ImageSyncError

QCOW2_MT = "application/vnd.husk.qcow2"
LAYER_DIGEST = "sha256:" + "a" * 64


class FakeOras:
    """Stands in for oras.client.OrasClient. `pull` drops a qcow2 into the given
    outdir so the cache logic has something to find; calls are recorded."""

    def __init__(self, manifest: dict | None = None) -> None:
        self.manifest = manifest or {
            "layers": [
                {
                    "mediaType": QCOW2_MT,
                    "digest": LAYER_DIGEST,
                    "annotations": {
                        "org.opencontainers.image.title": "husk-base.qcow2"
                    },
                }
            ]
        }
        self.manifests = 0
        self.pulls = 0

    def get_manifest(self, ref, **kw):
        self.manifests += 1
        return self.manifest

    def pull(self, *, target, outdir, allowed_media_type=None):
        self.pulls += 1
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, "husk-base.qcow2")
        with open(path, "wb") as f:
            f.write(b"qcow2-bytes")
        return [path]


def _sync(tmp_path, oras):
    return ImageSync(str(tmp_path), client_factory=lambda: oras)


def test_resolve_pulls_and_caches(tmp_path):
    oras = FakeOras()
    r = _sync(tmp_path, oras).resolve("ghcr.io/acts-project/husk-base:v1")

    assert r.digest == LAYER_DIGEST  # content-addressed by the qcow2 layer
    assert r.short == "a" * 12
    assert os.path.isfile(r.local_path) and r.local_path.endswith(".qcow2")
    assert LAYER_DIGEST.replace(":", "-") in r.local_path  # cache keyed by digest
    assert oras.pulls == 1


def test_second_resolve_is_a_cache_hit(tmp_path):
    oras = FakeOras()
    sync = _sync(tmp_path, oras)
    sync.resolve("ghcr.io/acts-project/husk-base:v1")
    sync.resolve("ghcr.io/acts-project/husk-base:v1")
    assert oras.pulls == 1  # already cached at that digest → no second pull


def test_concurrent_resolve_same_ref_pulls_once(tmp_path):
    # One shared ImageSync across pools: two threads resolving the same new ref on
    # a cold cache must single-flight the pull (the per-digest lock + re-check),
    # not both download it.
    started = threading.Event()

    class BlockingOras(FakeOras):
        def pull(self, *, target, outdir, allowed_media_type=None):
            started.set()
            time.sleep(0.2)  # hold the digest lock so the sibling has to wait
            return super().pull(target=target, outdir=outdir)

    oras = BlockingOras()
    sync = _sync(tmp_path, oras)
    results: list = []

    def go():
        results.append(sync.resolve("ghcr.io/acts-project/husk-base:v1"))

    t1 = threading.Thread(target=go)
    t2 = threading.Thread(target=go)
    t1.start()
    started.wait(timeout=1)  # ensure t1 is mid-pull before t2 reaches the lock
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert oras.pulls == 1  # the sibling reused the first pull, didn't re-download
    assert len(results) == 2
    assert results[0].local_path == results[1].local_path


def test_distinct_digests_not_serialized(tmp_path):
    # The lock is per content digest, not global: two refs that resolve to
    # different digests each pull (a shared lock would wrongly gate the second).
    digest_b = "sha256:" + "b" * 64

    class TwoRefOras(FakeOras):
        def get_manifest(self, ref, **kw):
            self.manifests += 1
            digest = digest_b if "gpu" in ref else LAYER_DIGEST
            return {"layers": [{"mediaType": QCOW2_MT, "digest": digest}]}

    oras = TwoRefOras()
    sync = _sync(tmp_path, oras)
    a = sync.resolve("ghcr.io/org/husk-base:v1")
    b = sync.resolve("ghcr.io/org/husk-gpu:v1")
    assert oras.pulls == 2
    assert a.digest == LAYER_DIGEST and b.digest == digest_b


def test_digest_comes_from_qcow2_layer(tmp_path):
    # A manifest with the empty-config layer present: the qcow2 layer is selected,
    # not the config.
    oras = FakeOras(
        manifest={
            "layers": [
                {
                    "mediaType": "application/vnd.oci.empty.v1+json",
                    "digest": "sha256:0",
                },
                {"mediaType": QCOW2_MT, "digest": LAYER_DIGEST},
            ]
        }
    )
    r = _sync(tmp_path, oras).resolve("ghcr.io/org/husk-gpu:v1")
    assert r.digest == LAYER_DIGEST


def test_artifact_without_qcow2_layer_raises(tmp_path):
    oras = FakeOras(manifest={"layers": [{"mediaType": "application/json"}]})
    with pytest.raises(ImageSyncError, match="no application/vnd.husk.qcow2 layer"):
        _sync(tmp_path, oras).resolve("ghcr.io/org/x:v1")


def test_qcow2_layer_carries_size(tmp_path):
    # The manifest layer's size is captured (drives pull-progress percentage).
    oras = FakeOras(
        manifest={
            "layers": [{"mediaType": QCOW2_MT, "digest": LAYER_DIGEST, "size": 4096}]
        }
    )
    assert _sync(tmp_path, oras)._qcow2_layer("ghcr.io/org/x:v1") == (
        LAYER_DIGEST,
        4096,
    )


def test_qcow2_layer_size_defaults_to_zero(tmp_path):
    oras = FakeOras()  # manifest omits "size"
    assert _sync(tmp_path, oras)._qcow2_layer("ghcr.io/org/x:v1")[1] == 0


def _run_pull_progress(tmp_path, monkeypatch, *, total: int) -> list[str]:
    """Drive `_log_pull_progress` once against a temp dir holding 2 MiB, returning
    the progress lines pushed to the report sink."""
    monkeypatch.setattr("husk.image_sync._PULL_PROGRESS_INTERVAL_S", 0.0)
    with open(tmp_path / "part.qcow2", "wb") as f:
        f.write(b"\0" * (2 << 20))
    reports: list[str] = []
    stop = threading.Event()
    sync = ImageSync(str(tmp_path))
    t = threading.Thread(
        target=sync._log_pull_progress,
        args=("ghcr.io/org/x:v1", str(tmp_path), stop, total, reports.append),
        daemon=True,
    )
    t.start()
    deadline = time.time() + 2.0
    while not reports and time.time() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(timeout=1)
    return reports


def test_pull_progress_reports_percent_when_total_known(tmp_path, monkeypatch):
    reports = _run_pull_progress(tmp_path, monkeypatch, total=4 << 20)  # 2/4 MiB
    assert reports and "2/4 MiB (50%)" in reports[0]


def test_pull_progress_reports_bytes_when_total_unknown(tmp_path, monkeypatch):
    reports = _run_pull_progress(tmp_path, monkeypatch, total=0)
    assert reports and "2 MiB so far" in reports[0]


def test_manifest_read_failure_is_wrapped_after_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "husk.image_sync._MANIFEST_BACKOFF_S", 0
    )  # don't sleep in tests
    attempts = {"n": 0}

    class Boom:
        def get_manifest(self, ref, **kw):
            attempts["n"] += 1
            raise RuntimeError("404 not found")

    with pytest.raises(ImageSyncError, match="could not read manifest"):
        ImageSync(str(tmp_path), client_factory=lambda: Boom()).resolve("ghcr.io/x:v1")
    assert attempts["n"] == 3  # retried before giving up


# ------------------------------------------------------------------------ gc
# The cache holds multi-GB goldens, so it must not grow without bound: a digest
# no pool pins ages out, and a pull that died mid-download leaves no debris.


def _aged(path: str, seconds: float) -> str:
    """Backdate an entry so the age-based GC rules can be exercised without sleeping."""
    old = time.time() - seconds
    os.utime(path, (old, old))
    return path


def _digest_dir(cache, name: str, age: float = 0.0) -> str:
    path = os.path.join(str(cache), name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "husk-base.qcow2"), "wb") as f:
        f.write(b"x")
    return _aged(path, age)


def test_gc_evicts_unpinned_digest_past_retention(tmp_path):
    sync = _sync(tmp_path, FakeOras())
    stale = _digest_dir(tmp_path, "sha256-" + "b" * 64, age=48 * 3600)
    fresh = _digest_dir(tmp_path, "sha256-" + "c" * 64, age=60)

    sync.gc(force=True)

    assert not os.path.exists(stale)
    assert os.path.exists(fresh)  # inside the retention window → a rollback re-uses it


def test_gc_keeps_pinned_digest_however_old(tmp_path):
    sync = _sync(tmp_path, FakeOras())
    pinned = _digest_dir(tmp_path, "sha256-" + "b" * 64, age=90 * 24 * 3600)
    sync.pin("pool-a", {"sha256:" + "b" * 64})

    sync.gc(force=True)

    assert os.path.exists(pinned)  # in service — age is irrelevant


def test_gc_keeps_a_siblings_pin(tmp_path):
    # Several pools share one cache: the keep set is the union of their pins, so a
    # pool rolling forward must not collect the digest another pool still serves.
    sync = _sync(tmp_path, FakeOras())
    a = _digest_dir(tmp_path, "sha256-" + "a" * 64, age=48 * 3600)
    b = _digest_dir(tmp_path, "sha256-" + "b" * 64, age=48 * 3600)
    sync.pin("pool-a", {"sha256:" + "a" * 64})
    sync.pin("pool-b", {"sha256:" + "b" * 64})
    sync.pin("pool-a", {"sha256:" + "a" * 64})  # re-pin: same digest, still needed

    sync.gc(force=True)

    assert os.path.exists(a) and os.path.exists(b)


def test_gc_releases_a_rolled_off_digest(tmp_path):
    sync = _sync(tmp_path, FakeOras())
    old = _digest_dir(tmp_path, "sha256-" + "a" * 64, age=48 * 3600)
    sync.pin("pool-a", {"sha256:" + "a" * 64})
    sync.pin("pool-a", {"sha256:" + "d" * 64})  # rolled onto a new golden

    sync.gc(force=True)

    assert not os.path.exists(old)


def test_gc_sweeps_abandoned_pull_dirs_but_not_live_ones(tmp_path):
    # A huskd killed mid-pull leaves a .pull-* dir the in-process cleanup never ran
    # on; only age distinguishes it from a pull another process has in flight.
    sync = _sync(tmp_path, FakeOras())
    dead = os.path.join(str(tmp_path), ".pull-dead")
    live = os.path.join(str(tmp_path), ".pull-live")
    os.makedirs(dead)
    os.makedirs(live)
    _aged(dead, 12 * 3600)
    _aged(live, 12 * 3600)
    sync._active_tmp.add(live)  # this process owns it

    sync.gc(force=True)

    assert not os.path.exists(dead)
    assert os.path.exists(live)


def test_gc_leaves_foreign_entries_alone(tmp_path):
    sync = _sync(tmp_path, FakeOras())
    junk = os.path.join(str(tmp_path), "notes.txt")
    with open(junk, "wb") as f:
        f.write(b"hi")
    _aged(junk, 365 * 24 * 3600)

    sync.gc(force=True)

    assert os.path.exists(junk)  # only digest dirs and .pull-* are ours to delete


def test_gc_is_throttled_between_sweeps(tmp_path):
    sync = _sync(tmp_path, FakeOras())
    sync.gc()  # first call sweeps and arms the throttle
    stale = _digest_dir(tmp_path, "sha256-" + "b" * 64, age=48 * 3600)

    sync.gc()  # too soon — a per-tick caller must not pay for a sweep every tick
    assert os.path.exists(stale)

    sync.gc(force=True)
    assert not os.path.exists(stale)


def test_resolve_sweeps_and_survives_it(tmp_path):
    # The pull that just landed is unpinned until the backend adopts it, so the
    # retention window (not a pin) is what keeps GC off it.
    oras = FakeOras()
    sync = _sync(tmp_path, oras)
    stale = _digest_dir(tmp_path, "sha256-" + "e" * 64, age=48 * 3600)

    r = sync.resolve("ghcr.io/acts-project/husk-base:v1")

    assert os.path.isfile(r.local_path)
    assert not os.path.exists(stale)


def test_cache_hit_refreshes_last_use(tmp_path):
    oras = FakeOras()
    sync = _sync(tmp_path, oras)
    r = sync.resolve("ghcr.io/acts-project/husk-base:v1")
    dest = os.path.dirname(r.local_path)
    _aged(dest, 48 * 3600)  # pretend it went unused for two days

    sync.resolve("ghcr.io/acts-project/husk-base:v1")  # ... then it's used again
    sync.gc(force=True)

    assert os.path.exists(dest)
    assert oras.pulls == 1  # and it was a cache hit, not a re-pull
