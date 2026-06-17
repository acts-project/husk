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
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("husk.image")

# A failed staging attempt is retried on the next tick that asks, but not before
# this backoff — so a registry/Glance outage doesn't spin a tight retry loop.
_PREPARE_RETRY_BACKOFF_S = 30.0

# The layer mediaType the build pipeline stamps on the qcow2 (build-images.yml).
_QCOW2_MEDIA_TYPE = "application/vnd.husk.qcow2"

# ghcr's anonymous-token endpoint occasionally 401/404s under burst; a couple of
# quick retries absorb that on cold-start (steady state also retries each tick).
_MANIFEST_ATTEMPTS = 3
_MANIFEST_BACKOFF_S = 2.0

_DEFAULT_CACHE = os.path.expanduser("~/.cache/husk/images")


def _default_cache_dir() -> str:
    return os.environ.get("HUSK_IMAGE_CACHE", _DEFAULT_CACHE)


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
    ) -> None:
        self.cache_dir = cache_dir or _default_cache_dir()
        self._client_factory = client_factory
        self._client = None

    def _client_(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def resolve(self, ref: str) -> ResolvedImage:
        """Ensure `ref` is present in the controller cache; return its concrete
        qcow2 digest + local path.

        Idempotent: a ref already cached at its current digest is reused without a
        re-pull. The manifest is read first to learn the qcow2 layer digest, so a
        moved tag re-pulls (new digest ⇒ new cache dir) while a stable tag/digest
        is a no-op."""
        digest = self._qcow2_digest(ref)
        dest = os.path.join(self.cache_dir, digest.replace(":", "-"))
        cached = self._qcow2_in(dest)
        if cached is not None:
            log.debug("image %s already cached at %s", ref, cached)
            return ResolvedImage(ref=ref, digest=digest, local_path=cached)

        os.makedirs(self.cache_dir, exist_ok=True)
        # Pull into a temp dir then atomically swap into place, so an interrupted
        # pull never leaves a half-written qcow2 that a later run treats as cached.
        tmp = tempfile.mkdtemp(prefix=".pull-", dir=self.cache_dir)
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
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        local = self._qcow2_in(dest)
        assert local is not None  # we just placed it
        log.info("image %s ready at %s", ref, local)
        return ResolvedImage(ref=ref, digest=digest, local_path=local)

    def _qcow2_digest(self, ref: str) -> str:
        """The digest of the artifact's qcow2 layer (its content hash)."""
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
                return layer["digest"]
        for layer in layers:
            title = (layer.get("annotations") or {}).get(
                "org.opencontainers.image.title", ""
            )
            if title.endswith(".qcow2") and layer.get("digest"):
                return layer["digest"]
        raise ImageSyncError(
            f"artifact {ref!r} has no {_QCOW2_MEDIA_TYPE} layer (layers: "
            f"{[layer.get('mediaType') for layer in layers]})"
        )

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


class BackgroundImagePreparer:
    """Runs slow image staging (registry pull + delivery to a backend's targets)
    OFF the reconcile thread, so a multi-GB transfer never blocks a tick.

    The controller calls `ready(ref)` once per tick: it returns the prepared
    result as soon as staging for that ref has finished, or `None` while it's
    still in flight (the first ask kicks off a background prepare). The backend
    then adopts a non-None result instantly on the tick thread — so the live image
    fields are only ever written by the single reconcile thread, never racing the
    worker. Single-flight per ref; a failed prepare is retried on the next ask
    after a backoff. `prepare(ref)` does the heavy, idempotent work and returns an
    opaque result the backend knows how to adopt.

    `spawn` runs the worker (default: a daemon thread); tests inject a synchronous
    runner so a single `ready()` returns the result deterministically."""

    def __init__(self, prepare, *, spawn=None) -> None:
        self._prepare = prepare
        self._spawn = spawn or self._thread_spawn
        self._lock = threading.Lock()
        self._ready: dict[str, object] = {}
        self._inflight: set[str] = set()
        self._failed_at: dict[str, float] = {}

    @staticmethod
    def _thread_spawn(fn) -> None:
        threading.Thread(target=fn, name="husk-image-prepare", daemon=True).start()

    def ready(self, ref: str):
        """Return the staged result for `ref`, or None while staging is in flight
        (kicking off a background prepare on the first ask / after a backoff)."""
        with self._lock:
            if ref in self._ready:
                return self._ready[ref]
            if ref in self._inflight:
                return None
            failed = self._failed_at.get(ref)
            if (
                failed is not None
                and time.monotonic() - failed < _PREPARE_RETRY_BACKOFF_S
            ):
                return None  # recent failure — hold off before retrying
            self._inflight.add(ref)
        log.info("staging image %s in the background", ref)
        self._spawn(lambda: self._run(ref))
        with self._lock:
            # Populated already iff `spawn` ran the worker inline (tests); a real
            # daemon thread hasn't finished yet, so this returns None this tick.
            return self._ready.get(ref)

    def _run(self, ref: str) -> None:
        try:
            result = self._prepare(ref)
        except BaseException as e:  # noqa: BLE001 - a worker must never escape
            with self._lock:
                self._inflight.discard(ref)
                self._failed_at[ref] = time.monotonic()
            log.warning("image staging for %s failed: %s", ref, e, exc_info=True)
            return
        with self._lock:
            self._ready[ref] = result
            self._inflight.discard(ref)
            self._failed_at.pop(ref, None)
        log.info("image %s staged and ready", ref)
