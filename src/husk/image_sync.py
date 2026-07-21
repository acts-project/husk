"""Pull golden VM images from an OCI registry to a controller-local cache.

The controller (huskd) is the one place that talks to the registry: it pulls a
config-pinned image ref **once** into a local cache keyed by content digest, and
the libvirt backend then fans the qcow2 out to each VM-host over its existing SSH
channel (`libvirt_backend.py`). Hosts need no registry client or credentials.

This module is the registry half only — pure of any libvirt/SSH knowledge. It
uses the pure-Python `oras` client (the `husk[libvirt]` extra), so there is no
`oras` CLI to install on the controller. The images are public on ghcr.io, so
there is no login/credential handling here.

The build pipeline (`.github/workflows/build-images.yml`, `image-pipeline.md`)
publishes each qcow2 as an OCI artifact (`application/vnd.husk.vmimage`) whose
single layer is the `*.qcow2` (`application/vnd.husk.qcow2`). We content-address
by that **layer** digest — the hash of the qcow2 blob itself — so the cache dir
and the on-host filename are stable and a tag moving to new content is a new
digest.

The cache is bounded (`gc`): each pool `pin`s the digests it still needs, and a
digest nobody pins is evicted once it has gone unused for the retention window,
as is the debris of a pull that died mid-download. Downstream copies have their
own GC — `LibvirtBackend._gc_goldens` on the hosts, `OpenStackBackend._gc_glance`
in Glance.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass

from husk.storage import CACHE, DiskUsage

log = logging.getLogger("husk.image")

# How often to log registry-pull progress (MiB pulled so far) during a slow pull.
_PULL_PROGRESS_INTERVAL_S = 30.0

# The layer mediaType the build pipeline stamps on the qcow2 (build-images.yml).
_QCOW2_MEDIA_TYPE = "application/vnd.husk.qcow2"

# ghcr's anonymous-token endpoint occasionally 401/404s under burst; a couple of
# quick retries absorb that on cold-start (steady state also retries each tick).
_MANIFEST_ATTEMPTS = 3
_MANIFEST_BACKOFF_S = 2.0

_DEFAULT_CACHE = os.path.expanduser("~/.cache/husk/images")

# Cache GC (see ImageSync.gc). A cached golden is multi-GB, so an unbounded cache
# eats the controller's disk one image rollout at a time.
#   * A digest nobody pins is kept this long after its last use, so a rollback or
#     a second huskd/CLI process using the same cache re-uses it instead of
#     re-pulling — and so a pull that just landed is never reaped before the
#     backend gets a chance to pin it.
_UNPINNED_RETENTION_S = 24 * 3600
#   * A `.pull-*` temp dir this process doesn't own is debris from a huskd that
#     died mid-pull; nothing will ever finish it. The age floor keeps us off the
#     in-flight pulls of a *concurrent* process sharing the cache.
_STALE_PULL_AGE_S = 6 * 3600
#   * Sweeps are cheap (a listdir + a stat each) but not free; once a quarter
#     hour is plenty to bound the footprint.
_GC_INTERVAL_S = 900

# `<algo>-<hex>` — the on-disk spelling of a content digest (":" isn't portable).
# GC only ever deletes entries matching this or the `.pull-` prefix, so anything
# else a human parked in the cache dir is left alone.
_DIGEST_DIR = re.compile(r"^sha\d+-[0-9a-f]{32,}$")

# How long a cache-usage measurement is reused. The scan is a local scandir over
# a handful of digest dirs (microseconds), but it is reached from the /metrics
# handler, so a memo keeps a scrape storm off the disk entirely.
_USAGE_TTL_S = 30.0


def _default_cache_dir() -> str:
    return os.environ.get("HUSK_IMAGE_CACHE", _DEFAULT_CACHE)


def _dir_for(digest: str) -> str:
    """The cache subdir holding a digest's qcow2 (":" isn't a portable filename)."""
    return digest.replace(":", "-")


def _fs_space(path: str) -> tuple[int | None, int | None]:
    """(capacity, free) in bytes for the filesystem holding `path`, or (None, None).

    Walks up to the nearest existing ancestor, because on a cold start the cache
    directory has not been created yet while the volume under it very much exists —
    and an ancestor on the same mount reports the same filesystem, which is the
    thing we actually want to know about.

    `f_bavail`, not `f_bfree`: the latter counts blocks reserved for root, which
    huskd (running unprivileged) can never use. Reporting them as free would make
    the volume look emptier than it is right when that matters most.

    Best-effort — this feeds /metrics, so anything unmeasurable is None and the
    series is simply omitted."""
    while True:
        try:
            st = os.statvfs(path)
            return st.f_frsize * st.f_blocks, st.f_frsize * st.f_bavail
        except FileNotFoundError:
            parent = os.path.dirname(path.rstrip(os.sep))
            if not parent or parent == path:
                return None, None
            path = parent
        except OSError:
            log.debug("could not statvfs %s", path, exc_info=True)
            return None, None


class ImageSyncError(RuntimeError):
    """The registry pull failed, or the artifact had no qcow2 layer."""


@dataclass(frozen=True)
class ResolvedImage:
    """A registry ref resolved to concrete, content-addressed local state."""

    ref: str  # the configured ref as given (tag or digest)
    digest: str  # the qcow2 layer's content digest, e.g. "sha256:abcd…"
    local_path: str  # absolute path to the pulled qcow2 in the controller cache

    @property
    def short(self) -> str:
        """First 12 hex chars of the digest — used to name the on-host golden
        (`husk-golden-<short>.qcow2`) so files are content-addressed and a ref
        moving to new content never overwrites an in-use backing file."""
        return self.digest.split(":", 1)[-1][:12]


def _new_client():  # pragma: no cover - thin import shim, faked in tests
    try:
        import oras.client
    except ImportError as e:
        raise ImageSyncError(
            "the `oras` Python package is required to sync OCI images; install the "
            "extra: pip install 'husk[libvirt]' (or unset [backend].image_ref)"
        ) from e
    return oras.client.OrasClient()


class ImageSync:
    """Resolves + caches OCI image artifacts on the controller.

    `client_factory` returns an object with `get_manifest(ref) -> dict` and
    `pull(*, target, outdir, allowed_media_type) -> list[str]` (the oras-py
    `OrasClient` API); it is injectable so tests need no registry.
    """

    def __init__(
        self,
        cache_dir: str | None = None,
        *,
        client_factory=_new_client,
        retention_s: float = _UNPINNED_RETENTION_S,
    ) -> None:
        self.cache_dir = cache_dir or _default_cache_dir()
        self._client_factory = client_factory
        self._client = None
        self._retention_s = retention_s
        # One instance is shared across all pools (huskd builds it once), so its
        # concurrency has teeth: guard the lazy client build and single-flight the
        # pull per content digest so two pools resolving the same new ref don't
        # both download it.
        self._locks_guard = threading.Lock()
        self._digest_locks: dict[str, threading.Lock] = {}
        # GC bookkeeping, all under _locks_guard: who still needs which digest
        # (pool name -> digests), the temp dirs of our own in-flight pulls, and
        # when we last swept.
        self._pins: dict[str, set[str]] = {}
        self._active_tmp: set[str] = set()
        self._last_gc = 0.0
        # Memoized cache_usage(): (measured_at, usage). Guarded by _locks_guard.
        self._usage: tuple[float, "DiskUsage"] | None = None

    def _client_(self):
        # Two pool worker threads can race the first resolve; build the shared
        # oras client at most once.
        with self._locks_guard:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def _digest_lock(self, digest: str) -> threading.Lock:
        """A lock unique to a content digest — held around the pull so concurrent
        resolves of the same digest (across pools) serialize into one download.
        Keyed by digest, not ref: two tags can resolve to the same content, and
        the digest is what names the cache dest."""
        with self._locks_guard:
            lock = self._digest_locks.get(digest)
            if lock is None:
                lock = self._digest_locks[digest] = threading.Lock()
            return lock

    def resolve(self, ref: str, report=None) -> ResolvedImage:
        """Ensure `ref` is present in the controller cache; return its concrete
        qcow2 digest + local path.

        Idempotent: a ref already cached at its current digest is reused without a
        re-pull. The manifest is read first to learn the qcow2 layer digest, so a
        moved tag re-pulls (new digest ⇒ new cache dir) while a stable tag/digest
        is a no-op. `report` (if given) receives a live "N/M MiB (P%)" line during
        a slow pull, for the status board."""
        digest, size = self._qcow2_layer(ref)
        dest = os.path.join(self.cache_dir, _dir_for(digest))
        cached = self._qcow2_in(dest)
        if cached is not None:
            log.debug("image %s already cached at %s", ref, cached)
            self._touch(dest)  # keep GC's "last used" honest
            self.gc()
            return ResolvedImage(ref=ref, digest=digest, local_path=cached)

        # Serialize the pull per digest so two pools resolving the same new ref on
        # a cold cache don't both download it. A pool that blocks here re-checks
        # the cache on the far side of the lock and reuses the sibling's pull.
        with self._digest_lock(digest):
            cached = self._qcow2_in(dest)
            if cached is not None:
                log.debug("image %s cached by a sibling pull at %s", ref, cached)
                self._touch(dest)
                return ResolvedImage(ref=ref, digest=digest, local_path=cached)
            os.makedirs(self.cache_dir, exist_ok=True)
            # Pull into a temp dir then atomically swap into place, so an interrupted
            # pull never leaves a half-written qcow2 that a later run treats as cached.
            # The dir is registered as ours for the duration so a concurrent GC sweep
            # doesn't mistake a live pull for a dead one's debris.
            staged = tmp = tempfile.mkdtemp(prefix=".pull-", dir=self.cache_dir)
            with self._locks_guard:
                self._active_tmp.add(staged)
            stop = threading.Event()
            threading.Thread(
                target=self._log_pull_progress,
                args=(ref, tmp, stop, size, report),
                name="husk-pull-progress",
                daemon=True,
            ).start()
            try:
                log.info("pulling image %s (%s) to controller cache", ref, digest[:19])
                # NB: don't pass allowed_media_type here — in oras-py that filters the
                # *manifest* Accept header (not the layers), and restricting it to the
                # qcow2 type makes the registry 404 the manifest. The default accepts
                # the OCI manifest; the artifact's only layer is the qcow2.
                self._client_().pull(target=ref, outdir=tmp)
                qcow2 = self._qcow2_in(tmp)
                if qcow2 is None:
                    raise ImageSyncError(
                        f"artifact {ref} contained no *.qcow2 layer (got: {os.listdir(tmp)})"
                    )
                shutil.rmtree(dest, ignore_errors=True)
                os.replace(tmp, dest)
                tmp = None  # consumed by the rename; don't clean it up below
            finally:
                stop.set()
                with self._locks_guard:
                    self._active_tmp.discard(staged)
                if tmp is not None:
                    shutil.rmtree(tmp, ignore_errors=True)
        local = self._qcow2_in(dest)
        assert local is not None  # we just placed it
        log.info("image %s ready at %s", ref, local)
        self.gc()
        return ResolvedImage(ref=ref, digest=digest, local_path=local)

    # ------------------------------------------------------------------- gc
    def pin(self, owner: str, digests) -> None:
        """Declare the digests `owner` (a pool name) still needs cached, replacing
        that owner's previous declaration.

        This is what makes GC safe with several pools on one cache: the keep set is
        the *union* of every pool's pins, so a pool rolling forward releases its old
        digest without touching a sibling's. A pool that never pins keeps nothing —
        its images age out on the retention window like any other."""
        with self._locks_guard:
            self._pins[owner] = {d for d in digests if d}

    def gc(self, *, force: bool = False) -> None:
        """Bound the cache's disk footprint: drop superseded goldens and the debris
        of pulls that died mid-flight.

        Deleted: a `<algo>-<hex>` dir no pool pins whose last use is older than the
        retention window, and a `.pull-*` temp dir this process doesn't own that is
        older than `_STALE_PULL_AGE_S` (a crashed huskd's half-download — the
        in-process `finally` in `resolve` can't clean those up). Everything else in
        the cache dir is left alone.

        Self-throttled to `_GC_INTERVAL_S` so callers can call it every tick; pass
        `force=True` to sweep now. Best-effort throughout: a cache we can't read or
        an entry we can't remove is skipped, never fatal — worst case the next sweep
        gets it, and a wrongly-deleted golden only costs a re-pull."""
        now = time.time()
        with self._locks_guard:
            if not force and now - self._last_gc < _GC_INTERVAL_S:
                return
            self._last_gc = now
            keep = {_dir_for(d) for pins in self._pins.values() for d in pins}
            active = set(self._active_tmp)
        try:
            entries = os.listdir(self.cache_dir)
        except OSError:
            return  # no cache dir yet (or unreadable) — nothing to collect
        for name in entries:
            path = os.path.join(self.cache_dir, name)
            if name.startswith(".pull-"):
                if path in active:
                    continue  # our own live pull
                limit, what = _STALE_PULL_AGE_S, "abandoned pull"
            elif _DIGEST_DIR.match(name):
                if name in keep:
                    continue  # a pool is still serving this digest
                limit, what = self._retention_s, "unused golden"
            else:
                continue  # not ours
            try:
                if now - os.path.getmtime(path) < limit:
                    continue
            except OSError:
                continue
            log.info("GC %s %s from the image cache", what, name)
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _touch(path: str) -> None:
        """Mark a cache entry used now, so GC's retention window measures time since
        last use rather than since the pull."""
        try:
            os.utime(path, None)
        except OSError:
            pass

    def _log_pull_progress(
        self, ref: str, tmp: str, stop, total: int = 0, report=None
    ) -> None:
        """Log how much has been pulled into the temp dir every
        `_PULL_PROGRESS_INTERVAL_S`, so a slow multi-GB registry pull isn't a silent
        gap; if given a `report` sink, push the same line onto the op. When the
        manifest gave a layer `total`, report a percentage, else bytes so far.
        Best-effort: it stops the moment the pull completes."""
        while not stop.wait(_PULL_PROGRESS_INTERVAL_S):
            try:
                got = sum(
                    os.path.getsize(os.path.join(dp, f))
                    for dp, _dirs, files in os.walk(tmp)
                    for f in files
                )
            except OSError:
                continue
            if got > 0:
                line = (
                    f"pulling golden: {got >> 20}/{total >> 20} MiB "
                    f"({100 * got // total}%)"
                    if total > 0
                    else f"pulling golden: {got >> 20} MiB so far"
                )
                log.info("%s (%s)", line, ref)
                if report is not None:
                    report(line)

    def _qcow2_layer(self, ref: str) -> tuple[str, int]:
        """The (digest, size-in-bytes) of the artifact's qcow2 layer. `size` is 0
        if the manifest omits it (then pull progress falls back to bytes-so-far)."""
        last: Exception | None = None
        for attempt in range(_MANIFEST_ATTEMPTS):
            try:
                manifest = self._client_().get_manifest(ref)
                break
            except Exception as e:  # registry/network/parse — retry then surface
                last = e
                if attempt + 1 < _MANIFEST_ATTEMPTS:
                    log.debug("manifest read for %s failed (%s); retrying", ref, e)
                    time.sleep(_MANIFEST_BACKOFF_S)
        else:
            raise ImageSyncError(
                f"could not read manifest for {ref!r}: {last}"
            ) from last
        layers = manifest.get("layers") or []
        # Prefer the husk qcow2 mediaType; fall back to the only layer / a .qcow2 title.
        for layer in layers:
            if layer.get("mediaType") == _QCOW2_MEDIA_TYPE and layer.get("digest"):
                return layer["digest"], int(layer.get("size") or 0)
        for layer in layers:
            title = (layer.get("annotations") or {}).get(
                "org.opencontainers.image.title", ""
            )
            if title.endswith(".qcow2") and layer.get("digest"):
                return layer["digest"], int(layer.get("size") or 0)
        raise ImageSyncError(
            f"artifact {ref!r} has no {_QCOW2_MEDIA_TYPE} layer (layers: "
            f"{[layer.get('mediaType') for layer in layers]})"
        )

    def cache_usage(self) -> DiskUsage:
        """How many qcow2 images the controller cache holds, their total size, and
        how full the filesystem underneath them is.

        `gc` bounds this cache, so the image counts should sit near "the goldens
        in service" and spike for a day around a rollout. A count that keeps
        climbing means pins are not being released (or a pool stopped pinning) —
        which is exactly the kind of drift worth a graph.

        The filesystem figures answer the different question GC does not: how much
        room is left. Those come apart, because the cache is not the only thing on
        the volume and GC only reclaims what husk itself put there. On k8s this
        directory is its own PVC, so this is the number that says whether the next
        golden pull will fail.

        Memoized for `_USAGE_TTL_S` because `/metrics` reads it. In-flight pulls
        (`.pull-*` temp dirs) are excluded: they are not cache content yet, and
        counting them would make the gauge sawtooth during a pull. A cache dir
        that doesn't exist yet is 0/0, not an error."""
        now = time.time()
        with self._locks_guard:
            if self._usage is not None and now - self._usage[0] < _USAGE_TTL_S:
                return self._usage[1]
        images = total = 0
        try:
            for entry in os.scandir(self.cache_dir):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                for name in os.listdir(entry.path):
                    if not name.endswith(".qcow2"):
                        continue
                    try:
                        total += os.path.getsize(os.path.join(entry.path, name))
                    except OSError:
                        continue  # vanished mid-scan (a concurrent re-pull swap)
                    images += 1
        except FileNotFoundError:
            pass  # nothing pulled yet
        except OSError:
            log.debug("could not scan image cache %s", self.cache_dir, exc_info=True)
        size, avail = _fs_space(self.cache_dir)
        usage = DiskUsage(
            kind=CACHE,
            host="",
            images=images,
            total_bytes=total,
            fs_size_bytes=size,
            fs_avail_bytes=avail,
        )
        with self._locks_guard:
            self._usage = (now, usage)
        return usage

    @staticmethod
    def _qcow2_in(directory: str) -> str | None:
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return None
        for name in sorted(names):
            if name.endswith(".qcow2"):
                return os.path.join(directory, name)
        return None
