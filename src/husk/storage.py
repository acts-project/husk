"""Where husk's qcow2 images sit on disk, and how much room they take.

Three populations of qcow2 exist, on two different machines:

  * the **controller cache** (`image_sync.ImageSync`) — one dir per pulled layer
    digest under `~/.cache/husk/images`. `ImageSync.gc` evicts a digest no pool
    pins once it has gone unused for the retention window, so this should track
    the goldens in service; a figure that only ever climbs means pins are not
    being released.
  * per-host **goldens** (`husk-golden-<digest>.qcow2`) — the backing files the
    libvirt backend scp's into each hypervisor's storage pool. `_gc_goldens`
    prunes the unreferenced ones.
  * per-host **overlays** (`husk-<slot>-c<cycle>.qcow2`) — the COW disks live
    slots write to. These grow with runner churn and are the number that
    actually predicts a full hypervisor disk.

Usage is reported as a flat list of `DiskUsage` rather than a nested structure
because that is what the Prometheus renderer wants: one sample per row.

Everything here is *global*, not per-pool. The controller cache is shared by
every pool (huskd builds one `ImageSync`), and two libvirt pools can share a
host's storage pool dir — so both would report the same bytes. `collect()`
therefore dedupes by (host, kind), which keeps a `sum()` over the metric honest
instead of double-counting a shared dir once per pool that touches it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("husk.storage")

# Where a DiskUsage row was measured.
CACHE = "cache"  # controller-local OCI pull cache
GOLDEN = "golden"  # backing files staged on a VM host
OVERLAY = "overlay"  # per-slot COW disks on a VM host


@dataclass(frozen=True)
class DiskUsage:
    """Count + total size of one population of qcow2 files, and — where we can
    measure it — the filesystem those files sit on.

    The two halves answer different questions. `images`/`total_bytes` say how much
    husk is holding; `fs_size_bytes`/`fs_avail_bytes` say how much room is left,
    which is the number that actually predicts an outage. They are not
    interchangeable: the cache PVC also holds nothing else, but a hypervisor's
    storage pool shares a disk with everything else on that box, so husk's own
    footprint can be flat while the volume fills anyway.

    Filesystem figures are None where nobody has measured them (the backends do
    not report them yet — only the controller-local cache does), and the renderer
    omits the series rather than emitting a misleading 0."""

    kind: str  # CACHE | GOLDEN | OVERLAY
    host: str  # VM host name; "" for the controller-local cache
    images: int
    total_bytes: int
    fs_size_bytes: int | None = None  # capacity of the filesystem holding them
    fs_avail_bytes: int | None = None  # free space on it (as seen by this user)


def collect(image_sync, backends) -> list[DiskUsage]:
    """Merge every backend's last-known on-host usage with the controller cache.

    Best-effort by construction: this feeds `/metrics`, so a backend that can't
    answer is dropped rather than allowed to fail the scrape. Backends report
    from a per-tick cached scan (no I/O here); the cache figure is memoized with
    a short TTL inside `ImageSync`, so a scrape storm can't hammer the disk.

    Deduped by (host, kind) — see the module docstring on why two pools sharing
    a hypervisor's storage pool must not have their bytes counted twice.
    """
    out: list[DiskUsage] = []
    if image_sync is not None:
        try:
            out.append(image_sync.cache_usage())
        except Exception:
            log.debug("controller cache usage unavailable", exc_info=True)
    seen: set[tuple[str, str]] = set()
    for backend in backends:
        try:
            rows = backend.disk_usage()
        except Exception:
            log.debug("backend disk usage unavailable", exc_info=True)
            continue
        for row in rows:
            key = (row.host, row.kind)
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    return out
