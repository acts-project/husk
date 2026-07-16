"""libvirt/QEMU backend — a pool of VM-hosts behind the single `Backend` seam.

One `LibvirtBackend` owns N host connections (`qemu+ssh://…`). Slots are placed on
the first free *slot-unit* (a GPU PCI address on a GPU host, or `cpuN` on a CPU
host); every method routes by the host name encoded in `Slot.id` (`host:uuid`).
Domain lifecycle/state/metadata go over the libvirt API; per-slot disk + cloud-init
seed are prepared on the host over SSH (`qemu-img`/`genisoimage`|`mkisofs`) — the guest is
never SSHed. Pure XML/state/placement helpers live in `libvirt_xml.py`.

Mutation methods are non-blocking in the controller's sense: libvirt's local disk
ops are fast and synchronous, so `rebuild_slot` returns with the domain SHUTOFF and
the controller's `pending_start` drain issues `start_slot` on the next tick — there
is no Nova-style async task to wait out (`task_state` is always None).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from husk import libvirt_xml as lx
from husk.backend import BackendError, ListSlotsError
from husk.config import BackendConfig, HostConfig
from husk.image_sync import ImageSync
from husk.ops import DONE, OpStore, OpView
from husk.slot import Capacity, Slot


@dataclass(frozen=True)
class _GoldenPrepared:
    """An OCI golden pulled + pushed to every host that uses its ref, ready for
    the tick thread to adopt (point each host's current image at it)."""

    digest: str
    golden: str  # the digest-named filename now present in each host pool


log = logging.getLogger("husk.libvirt")

# Timeouts — a control plane must never be wedged by one unresponsive host.
_SSH_TIMEOUT_S = 60  # cap each host-side qemu-img/genisoimage/rm command
_SSH_CONNECT_TIMEOUT_S = 15  # cap the ssh TCP/auth handshake
# Hard wall-clock bound on the *initial* libvirt.open() connect. libvirt-python's
# open() takes no timeout and the qemu+ssh transport doesn't bound its own
# handshake, so a down host would otherwise block the caller (the reconcile
# thread) for the full kernel SYN-retry window. A bit above the ssh connect bound
# to leave headroom for the libvirtd RPC negotiation on a healthy host.
_CONNECT_TIMEOUT_S = _SSH_CONNECT_TIMEOUT_S + 5
_PUSH_TIMEOUT_S = 3600  # a golden qcow2 is multi-GB; scp to a host can be slow
_PROGRESS_INTERVAL_S = 30  # how often to log golden-transfer progress (MiB/%)
# libvirt RPC keepalive: probe every N seconds, drop the connection after C
# misses (~N*C s). This is what stops an in-flight call to a frozen host from
# blocking the reconcile loop forever; it needs the event loop (below) running.
_KEEPALIVE_INTERVAL_S = 5
_KEEPALIVE_COUNT = 3

try:  # optional extra: pip install husk[libvirt]
    import libvirt
except ImportError:  # pragma: no cover - exercised only without the extra
    libvirt = None


def _run_libvirt_event_loop() -> None:  # pragma: no cover - needs the extra
    while True:
        libvirt.virEventRunDefaultImpl()


if libvirt is not None:  # pragma: no cover - needs the extra + a live host
    # libvirt echoes every raised error to stderr via its default callback —
    # including the *expected* "metadata not found" probe `_read_meta` does on
    # each non-husk domain. Route them to our debug logger so the controller's
    # logs stay clean; genuine failures still surface as raised exceptions.
    libvirt.registerErrorHandler(
        lambda _ctx, err: log.debug("libvirt: %s", err[2]), None
    )
    # Drive libvirt's event loop in a daemon thread so connection keepalive timers
    # fire — without it, an in-flight RPC to a wedged host would block forever.
    libvirt.virEventRegisterDefaultImpl()
    threading.Thread(
        target=_run_libvirt_event_loop, name="husk-libvirt-events", daemon=True
    ).start()


class _HostConn:
    """A single VM-host: its config, lazy libvirt connection, and slot-units."""

    def __init__(self, cfg: HostConfig, golden_image: str) -> None:
        self.cfg = cfg
        # On-disk golden filename in the host pool. In OCI mode this is overwritten
        # by sync_images with a digest-named file; in the manual path it's the
        # literal image_name. image_digest is the content digest once synced (None
        # in the manual path → no drain/GC for this host).
        self.image = cfg.image_name or golden_image
        self.image_digest: str | None = None
        self.units = lx.host_units(list(cfg.gpu_pci_addresses), cfg.max_slots or 1)
        self._conn = None
        self._pool_dir: str | None = None

    def conn(self):
        """Open (or reopen a dropped) libvirt connection."""
        if self._conn is None or not self._alive():
            log.debug("opening libvirt connection %s", self.cfg.libvirt_uri)
            self._conn = self._open()
            try:  # bound in-flight RPCs to a wedged host (needs the event loop)
                self._conn.setKeepAlive(_KEEPALIVE_INTERVAL_S, _KEEPALIVE_COUNT)
            except libvirt.libvirtError:
                log.debug("keepalive unsupported on %s", self.cfg.libvirt_uri)
        return self._conn

    def _open(self):
        """`libvirt.open` under a hard wall-clock bound (see `_CONNECT_TIMEOUT_S`).

        The keepalive set in `conn()` only bounds RPCs on an *already-open*
        connection — it can't rescue this initial connect, which for a down host
        would otherwise hang for minutes and (since it runs on the reconcile
        thread via list_slots/sync_images) wedge the whole loop. So run the
        connect on a throwaway daemon thread and give up on it after the bound,
        raising BackendError so list_slots fails fast and the tick's fail-safe
        aborts cleanly. The abandoned thread is a daemon and unwinds on its own
        once ssh finally errors; a connection it opens after we gave up is closed
        by GC."""
        box: dict[str, object] = {}

        def _connect() -> None:
            try:
                box["conn"] = libvirt.open(self.cfg.libvirt_uri)
            except BaseException as e:  # surfaced to the caller below
                box["err"] = e

        t = threading.Thread(
            target=_connect, name=f"husk-connect-{self.cfg.name}", daemon=True
        )
        t.start()
        t.join(_CONNECT_TIMEOUT_S)
        if t.is_alive():
            raise BackendError(
                f"libvirt connect to host {self.cfg.name} timed out after "
                f"{_CONNECT_TIMEOUT_S}s (host down/unreachable)"
            )
        if "err" in box:
            raise BackendError(
                f"libvirt connect to host {self.cfg.name} failed: {box['err']}"
            ) from box["err"]  # type: ignore[arg-type]
        conn = box.get("conn")
        if conn is None:
            raise BackendError(
                f"libvirt connect to host {self.cfg.name} returned no connection"
            )
        return conn

    def _alive(self) -> bool:
        try:
            return bool(self._conn.isAlive())
        except Exception:
            return False

    def pool_dir(self) -> str:
        """Absolute host path of the storage pool's target dir (where overlays +
        seeds live, and where the golden qcow2 is expected)."""
        if self._pool_dir is None:
            pool = self.conn().storagePoolLookupByName(self.cfg.storage_pool)
            path = ET.fromstring(pool.XMLDesc()).findtext("target/path")
            if not path:
                raise BackendError(
                    f"storage pool {self.cfg.storage_pool!r} has no target path"
                )
            self._pool_dir = path
        return self._pool_dir


class LibvirtBackend:
    def __init__(
        self, cfg: BackendConfig, *, image_sync: ImageSync | None = None
    ) -> None:
        if libvirt is None:
            raise RuntimeError(
                "libvirt-python not installed; install the extra: pip install 'husk[libvirt]'"
            )
        if not cfg.hosts:
            raise RuntimeError(
                "libvirt backend requires at least one [[backend.hosts]]"
            )
        self.cfg = cfg
        self._pool = cfg.name  # stamped into domain metadata; scopes list_slots
        # huskd passes one shared ImageSync so the registry pull is single-flighted
        # and the cache is shared across pools; a fresh default keeps one-shot
        # callers and tests self-contained.
        self._sync = image_sync or ImageSync()
        self._backend_ref = cfg.image_ref or ""
        # Per-host ref last successfully synced, so sync_images is a cheap no-op
        # each tick until the configured ref actually changes.
        self._synced_ref: dict[str, str] = {}
        # Heavy staging (oras pull + scp to each host) runs off the reconcile
        # thread as a keyed op; the tick only adopts a ready result (points each
        # host's current image at the staged golden).
        self._ops = OpStore()
        self._hosts: dict[str, _HostConn] = {}
        for h in cfg.hosts:
            if h.gpu_pci_addresses and h.max_slots is not None:
                raise RuntimeError(
                    f"host {h.name!r}: set either gpu_pci_addresses (GPU) or "
                    "max_slots (CPU), not both"
                )
            if h.name in self._hosts:
                raise RuntimeError(f"duplicate host name {h.name!r}")
            # Fail closed: every host needs an image source — an OCI ref (synced by
            # the controller) or a literal qcow2 filename already in its pool.
            if not (h.image_ref or self._backend_ref or h.image_name or cfg.image_name):
                raise RuntimeError(
                    f"host {h.name!r}: no image source — set [backend].image_ref "
                    "(OCI) or image_name (a qcow2 already in the host pool)"
                )
            self._hosts[h.name] = _HostConn(h, cfg.image_name)

    # --------------------------------------------------------------- internals
    def _ssh(
        self, host: _HostConn, remote_cmd: str, data: bytes | None = None
    ) -> bytes:
        """Run a command on the host (over SSH, or locally if no ssh_target)."""
        if host.cfg.ssh_target:
            argv = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_S}",
                host.cfg.ssh_target,
                remote_cmd,
            ]
        else:
            argv = ["bash", "-c", remote_cmd]
        log.debug("host[%s] exec: %s", host.cfg.name, remote_cmd)
        try:
            r = subprocess.run(
                argv, input=data, capture_output=True, timeout=_SSH_TIMEOUT_S
            )
        except subprocess.TimeoutExpired as e:
            # A frozen/overloaded host must NOT hang the reconcile loop forever.
            # Raising here means list_slots → ListSlotsError aborts the tick
            # (fail-safe), and mutations fail-and-retry next tick instead of
            # blocking indefinitely on a wedged host.
            raise BackendError(
                f"host {host.cfg.name} cmd timed out after {_SSH_TIMEOUT_S}s"
            ) from e
        if r.returncode != 0:
            raise BackendError(
                f"host {host.cfg.name} cmd failed ({r.returncode}): "
                f"{r.stderr.decode(errors='replace')[:300]}"
            )
        return r.stdout

    def _make_overlay(self, host: _HostConn, name: str) -> str:
        overlay = f"{host.pool_dir()}/{name}.qcow2"
        golden = f"{host.pool_dir()}/{host.image}"
        self._ssh(host, f"rm -f {shlex.quote(overlay)}")
        self._ssh(
            host,
            f"qemu-img create -f qcow2 -b {shlex.quote(golden)} -F qcow2 "
            f"{shlex.quote(overlay)}",
        )
        return overlay

    def _make_seed(
        self, host: _HostConn, name: str, cycle: int, user_data: bytes
    ) -> str:
        """Build the NoCloud cidata ISO. meta-data carries the rotating
        instance-id (cloud-init re-runs only when it changes)."""
        pool = host.pool_dir()
        seed = f"{pool}/{name}-seed.iso"
        tmp = f"{pool}/.seed-{name}"
        # Remove any prior seed first (mirrors _make_overlay): on rebuild the file
        # exists and libvirt has relabeled it to the qemu user while the slot ran,
        # so xorriso/genisoimage can't reopen it for writing. rm works regardless
        # (the SSH user owns the pool dir, so it can unlink files it no longer owns).
        self._ssh(host, f"rm -f {shlex.quote(seed)}; mkdir -p {shlex.quote(tmp)}")
        self._ssh(
            host,
            f"cat > {shlex.quote(tmp)}/meta-data",
            data=lx.meta_data(name, cycle).encode(),
        )
        self._ssh(host, f"cat > {shlex.quote(tmp)}/user-data", data=user_data)
        # genisoimage (cdrkit) and mkisofs (xorriso/cdrtools) take identical args
        # for our use; pick whichever the host has.
        self._ssh(
            host,
            'mkiso="$(command -v genisoimage || command -v mkisofs)"; '
            f'"$mkiso" -quiet -output {shlex.quote(seed)} -volid cidata '
            f"-joliet -rock {shlex.quote(tmp)}/user-data {shlex.quote(tmp)}/meta-data",
        )
        self._ssh(host, f"rm -rf {shlex.quote(tmp)}")
        return seed

    def _read_meta(self, dom) -> dict | None:
        try:
            xml = dom.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, lx.HUSK_NS)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
                return None  # legitimately not a husk slot (e.g. fedora-gpu)
            # Any OTHER error (broken/timed-out connection, host wedged) must NOT
            # be read as "not managed" — that would drop a real slot from
            # list_slots, making the controller spawn duplicates and never settle.
            # Propagate so list_slots → ListSlotsError aborts the tick (fail-safe).
            raise
        return lx.parse_metadata(xml)

    def _write_meta(
        self,
        dom,
        *,
        cycle: int,
        provisioned_at: float,
        created_at: float,
        unit: str,
        image_digest: str | None = None,
    ) -> None:
        meta = lx.metadata_xml(
            cycle=cycle,
            provisioned_at=provisioned_at,
            created_at=created_at,
            unit=unit,
            image_digest=image_digest,
            pool=self._pool,
        )
        dom.setMetadata(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT, meta, "husk", lx.HUSK_NS, 0
        )

    def _list_raw(self) -> list[tuple[str, object, dict]]:
        """(host_name, domain, husk-metadata) for every managed domain, all hosts."""
        out: list[tuple[str, object, dict]] = []
        for host_name, host in self._hosts.items():
            for dom in host.conn().listAllDomains():
                meta = self._read_meta(dom)
                if meta is None:
                    continue
                out.append((host_name, dom, meta))
        return out

    def _guest_ip(self, dom) -> str | None:
        """The guest's IPv4 on the private libvirt net, read from the network's DHCP
        leases. Lease source (not the guest agent): the golden image runs no
        qemu-guest-agent, and the lease is authoritative for the NAT network anyway.

        Only huskd resolves this — it already holds a connection to every host, which
        is what lets each host's metrics proxy stay dumb (it forwards to an IP it is
        handed, and never talks to libvirt). The mapping is stable across a recycle:
        `rebuild_slot` wipes the disk but does NOT redefine the domain, so the MAC —
        and therefore the lease — survives.

        None when the domain is off or the lease hasn't appeared yet; the slot is
        then simply not published as a metrics target.

        NOTHING here may raise. This runs inside `list_slots`, so an exception would
        surface as `ListSlotsError` and abort the whole reconcile tick — i.e. an
        observability nicety could stop husk from managing runners. Metrics are
        strictly best-effort: on any failure we return None and the slot just isn't
        scraped this tick."""
        try:
            if not dom.isActive():
                return None
            src = libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE
            ifaces = dom.interfaceAddresses(src, 0) or {}
            for iface in ifaces.values():
                for addr in (iface or {}).get("addrs") or []:
                    if addr.get("type") == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                        return addr.get("addr")
        except Exception as e:  # no lease yet, no DHCP on the net, libvirt hiccup...
            log.debug("no guest ip for %s: %s", getattr(dom, "name", lambda: "?")(), e)
        return None

    def _slot(self, host_name: str, dom, meta: dict) -> Slot:
        state, _reason = dom.state()
        status, task = lx.domain_status(state)
        host = self._hosts[host_name]
        slot_digest = meta.get("image_digest")
        # Stale only when both digests are known and differ — i.e. the host has
        # synced a current image (OCI mode) and this slot was built from an older
        # one. In the manual/local path host.image_digest is None → never stale.
        stale = bool(
            host.image_digest and slot_digest and slot_digest != host.image_digest
        )
        return Slot(
            id=f"{host_name}:{dom.UUIDString()}",
            name=dom.name(),
            status=status,
            task_state=task,
            created_at=meta.get("created_at") or 0.0,
            flavor_id=host_name,
            image_id=host.image,
            cycle=meta.get("cycle") or 0,
            provisioned_at=meta.get("provisioned_at"),
            fault=None,
            image_stale=stale,
            active_image=slot_digest,  # what THIS slot booted from (may lag host.image)
            # Metrics routing: the guest is only reachable from the host, so scrapes
            # go through that host's proxy — but huskd resolves the guest IP here so
            # the proxy needs no libvirt access of its own (see _guest_ip).
            host=host_name,
            ip=self._guest_ip(dom),
        )

    def _resolve(self, slot: Slot):
        host_name, _, dom_uuid = slot.id.partition(":")
        host = self._hosts.get(host_name)
        if host is None:
            raise BackendError(f"slot {slot.id} references unknown host {host_name!r}")
        return host_name, host, host.conn().lookupByUUIDString(dom_uuid)

    def _occupied(self) -> set[tuple[str, str]]:
        # Units held by live domains, pool-blind ON PURPOSE: two pools can share a
        # host, so a sibling pool's units (e.g. a GPU PCI address) must be seen here
        # or placement/capacity would double-book them. But require a `pool` tag:
        # current huskd always stamps one at create, so an UNtagged domain is a
        # pre-upgrade legacy leftover (invisible to list_slots, GC'd by nothing) and
        # must not silently consume a unit forever.
        return {
            (host_name, meta["unit"])
            for host_name, _dom, meta in self._list_raw()
            if meta.get("unit") and meta.get("pool")
        }

    def _host_ready(self, host: _HostConn) -> bool:
        """Can this host back a NEW slot yet? In OCI mode the golden is staged
        asynchronously (off the reconcile thread), so a host is ready only once its
        image has landed (`image_digest` known); the manual/local-file path (a
        literal `image_name`) is always ready. Gating capacity + placement on this
        means the controller simply sees zero capacity while staging — it never
        attempts a create (nor mints a wasted JIT runner) before the image is up."""
        if host.cfg.image_ref or self._backend_ref:
            return host.image_digest is not None
        return True

    def image_ready(self, slot: Slot) -> bool:
        """Whether the host backing `slot` has its golden staged (same gate as
        capacity uses for grows), so the controller can defer a rebuild instead of
        erroring while staging. An unknown host ⇒ ready, so the rebuild proceeds
        and surfaces the real unknown-host error rather than being silently held."""
        host = self._hosts.get(slot.id.partition(":")[0])
        return host is None or self._host_ready(host)

    def _host_units(self) -> list[lx.HostUnits]:
        # Only hosts whose golden has finished staging contribute slot-units, so a
        # not-yet-staged host reports no capacity rather than erroring on create.
        return [
            lx.HostUnits(name, h.units)
            for name, h in self._hosts.items()
            if self._host_ready(h)
        ]

    # ----------------------------------------------------------- image sync
    def sync_images(self, cfg: BackendConfig | None = None) -> None:
        """Adopt the configured golden on each host once it's staged, then GC
        orphans.

        Non-blocking: the controller's only coupling to image delivery, called
        once per tick. The slow work (oras pull + scp to each host) runs on a
        background thread (`_prepare_image`); a tick just adopts a ready result
        and otherwise keeps serving the current golden, so a multi-GB transfer
        never stalls the reconcile loop. Cheap when nothing changed: it
        short-circuits a host whose effective ref is already adopted.

        Hosts on the manual/local-file path (no ref) are skipped entirely. The
        backend-level ref can be hot-reloaded; per-host `image_ref` overrides are
        read once at construction (changing one needs a restart)."""
        if cfg is not None and (cfg.image_ref or "") != self._backend_ref:
            log.info(
                "image ref changed: %r -> %r (staging in the background)",
                self._backend_ref,
                cfg.image_ref,
            )
            self._backend_ref = cfg.image_ref or ""
        for name, host in self._hosts.items():
            ref = host.cfg.image_ref or self._backend_ref
            if not ref:
                continue  # manual/local-file host — nothing to pull
            if self._synced_ref.get(name) == ref and host.image_digest:
                continue  # already current
            key = f"stage:{ref}"
            view = self._ops.submit(
                key, "host-stage", lambda report, r=ref: self._prepare_image(r, report)
            )
            if view.state != DONE:
                continue  # still staging (or failed + backing off) — keep current
            prepared = self._ops.result(key)
            host.image = prepared.golden
            host.image_digest = prepared.digest
            self._synced_ref[name] = ref
            log.info("host %s now serving %s (%s)", name, ref, prepared.golden)
        self._gc_goldens()

    def staging_ops(self) -> list[OpView]:
        """In-flight / recent image-staging ops, for the status board."""
        return self._ops.views()

    def _prepare_image(self, ref: str, report) -> _GoldenPrepared:
        """Heavy staging (op worker): pull the OCI golden to the controller cache
        and scp it into the pool of every host that uses this ref. Returns what the
        tick adopts; it does NOT touch live host fields (only the tick thread does,
        in sync_images)."""
        report("pulling golden from registry")
        resolved = self._sync.resolve(ref, report=report)
        golden = f"husk-golden-{resolved.short}.qcow2"
        for host in self._hosts.values():
            if (host.cfg.image_ref or self._backend_ref) == ref:
                report(f"staging to host {host.cfg.name}")
                self._ensure_on_host(host, resolved.local_path, golden, report)
                # Mark this pool's current golden the moment it lands, BEFORE the
                # tick adopts it — so another pool sharing this host's pool dir
                # can't GC a freshly-staged golden in the pre-adopt window.
                self._mark_current(host, golden)
        return _GoldenPrepared(digest=resolved.digest, golden=golden)

    def _ensure_on_host(
        self, host: _HostConn, local_path: str, golden: str, report=None
    ) -> None:
        """Place `local_path` into the host pool as `golden`, if not already there.

        Idempotent (skip a present, non-empty file) and atomic (push to a temp
        name then `mv`) so a partial transfer is never seen as a usable backing
        file, and an in-use golden of the same digest is never re-pushed."""
        pool = host.pool_dir()
        remote = f"{pool}/{golden}"
        present = self._ssh(
            host, f"test -s {shlex.quote(remote)} && echo y || echo n"
        ).strip()
        if present == b"y":
            log.debug("host %s already has %s", host.cfg.name, golden)
            return
        # Stable tmp name within this process so a retried/resumed push reuses the
        # same partial file rather than starting a fresh one each attempt.
        tmp = f"{remote}.tmp.{os.getpid()}"
        try:
            size: int | None = os.path.getsize(local_path)
        except OSError:
            size = None  # can't read the source → skip the size check below
        log.info(
            "staging %s%s to host %s",
            golden,
            f" ({size >> 20} MiB)" if size else "",
            host.cfg.name,
        )
        if host.cfg.ssh_target:
            self._push_file(host, local_path, tmp, report)
        else:  # local host: a plain copy within the same filesystem
            self._ssh(host, f"cp {shlex.quote(local_path)} {shlex.quote(tmp)}")
        # Verify the whole file landed before publishing it — never `mv` a truncated
        # transfer into place (an interrupted push retries/resumes next tick). A
        # size mismatch raises, so the preparer records a failure and retries.
        if size is not None:
            got = int(
                self._ssh(
                    host, f"stat -c%s {shlex.quote(tmp)} 2>/dev/null || echo 0"
                ).strip()
                or b"0"
            )
            if got != size:
                raise BackendError(
                    f"golden {golden} transfer to {host.cfg.name} incomplete "
                    f"({got}/{size} bytes); will resume"
                )
        self._ssh(host, f"mv {shlex.quote(tmp)} {shlex.quote(remote)}")
        log.info("staged %s on host %s", golden, host.cfg.name)

    def _push_file(
        self, host: _HostConn, local_path: str, remote_path: str, report=None
    ) -> None:
        argv = [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_S}",
            local_path,
            f"{host.cfg.ssh_target}:{remote_path}",
        ]
        # Log transfer progress so a multi-GB push isn't a silent multi-minute gap:
        # a daemon thread polls the growing destination size against the source.
        try:
            total = os.path.getsize(local_path)
        except OSError:
            total = 0
        stop = threading.Event()
        if total:
            threading.Thread(
                target=self._log_push_progress,
                args=(host, remote_path, total, stop, report),
                name="husk-stage-progress",
                daemon=True,
            ).start()
        try:
            r = subprocess.run(argv, capture_output=True, timeout=_PUSH_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            raise BackendError(
                f"scp to host {host.cfg.name} timed out after {_PUSH_TIMEOUT_S}s"
            ) from e
        finally:
            stop.set()
        if r.returncode != 0:
            raise BackendError(
                f"scp to host {host.cfg.name} failed ({r.returncode}): "
                f"{r.stderr.decode(errors='replace')[:300]}"
            )

    def _log_push_progress(
        self,
        host: _HostConn,
        remote_path: str,
        total: int,
        stop: threading.Event,
        report=None,
    ) -> None:
        """Poll the destination size every `_PROGRESS_INTERVAL_S` and log MiB/% so a
        slow golden transfer is visible in the log; if given a `report` sink, push
        the same line onto the op so the dashboard shows it live. Best-effort: a
        failed probe is skipped, and it stops the moment the push finishes."""
        while not stop.wait(_PROGRESS_INTERVAL_S):
            try:
                got = int(
                    self._ssh(
                        host,
                        f"stat -c%s {shlex.quote(remote_path)} 2>/dev/null || echo 0",
                    ).strip()
                    or b"0"
                )
            except Exception:
                continue
            if got > 0:
                line = (
                    f"host {host.cfg.name}: {got >> 20}/{total >> 20} MiB "
                    f"({100 * got // total}%)"
                )
                log.info("staging to %s", line)
                if report is not None:
                    report(line)

    def _marker_path(self, host: _HostConn) -> str:
        """On-host file recording THIS pool's current golden, so a pool sharing the
        host's pool dir never GCs another pool's current image (`.husk-current-<pool>`)."""
        tag = re.sub(r"[^A-Za-z0-9_.-]", "_", self._pool)
        return f"{host.pool_dir()}/.husk-current-{tag}"

    def _mark_current(self, host: _HostConn, golden: str) -> None:
        try:
            self._ssh(
                host,
                f"printf '%s\\n' {shlex.quote(golden)} > {shlex.quote(self._marker_path(host))}",
            )
        except Exception:
            log.debug(
                "could not write golden marker on %s", host.cfg.name, exc_info=True
            )

    def _marked_goldens(self, host: _HostConn) -> set[str]:
        """Golden filenames every pool on this host has marked current (the union
        of all `.husk-current-*` markers in the shared pool dir)."""
        try:
            out = self._ssh(
                host,
                f"cat {shlex.quote(host.pool_dir())}/.husk-current-* 2>/dev/null || true",
            )
        except Exception:
            return set()
        return set(out.decode(errors="replace").split())

    def _gc_goldens(self) -> None:
        """Remove golden images on each host that no live slot references and that
        no pool has marked current. Best-effort and conservative: it only deletes
        `husk-golden-<digest>.qcow2` files, and keeps any that (a) back a live
        overlay (across ALL pools on the host), (b) are this pool's current image,
        or (c) are marked current by ANY pool sharing the host's pool dir — so two
        libvirt pools on one host never GC each other's backing files. A GC failure
        disturbs nothing."""
        try:
            raw = self._list_raw()
        except Exception:
            return
        live: dict[str, set[str]] = {}
        for host_name, _dom, meta in raw:
            d = meta.get("image_digest")
            if d:
                fn = f"husk-golden-{d.split(':', 1)[-1][:12]}.qcow2"
                live.setdefault(host_name, set()).add(fn)
        for name, host in self._hosts.items():
            keep = set(live.get(name, set()))
            if host.image_digest:
                keep.add(
                    f"husk-golden-{host.image_digest.split(':', 1)[-1][:12]}.qcow2"
                )
            keep |= self._marked_goldens(host)  # every pool's current, durably
            if not keep:
                continue  # don't GC a host we know nothing current about
            try:
                out = self._ssh(
                    host,
                    f"ls {shlex.quote(host.pool_dir())}/husk-golden-*.qcow2 "
                    "2>/dev/null || true",
                )
            except Exception:
                continue
            for path in out.decode(errors="replace").split():
                base = path.rsplit("/", 1)[-1]
                if base and base not in keep:
                    log.info("GC stale golden %s on host %s", base, name)
                    self._ssh(host, f"rm -f {shlex.quote(path)}")

    # --------------------------------------------------------------- backend
    def list_slots(self) -> list[Slot]:
        try:
            raw = self._list_raw()
        except Exception as e:  # libvirt/SSH/network — MUST raise, never []
            raise ListSlotsError(f"list domains failed: {e}") from e
        # Only this pool's domains (two pools can share a host). Placement/capacity
        # still consider ALL husk domains on the host (see _occupied) so a GPU unit
        # is never double-assigned across pools.
        slots = [
            self._slot(hn, dom, meta)
            for hn, dom, meta in raw
            if meta.get("pool") == self._pool
        ]
        log.debug(
            "listed %d managed slot(s) for pool %s across %d host(s)",
            len(slots),
            self._pool,
            len(self._hosts),
        )
        return slots

    def create_slot(self, *, user_data: bytes, name: str, cycle: int) -> Slot:
        placed = lx.place(self._host_units(), self._occupied())
        if placed is None:
            raise BackendError("no free slot-units across host pool")
        host_name, unit = placed
        # Placement only returns a host whose golden has staged (see _host_units /
        # _host_ready), so the overlay always has a backing image here.
        host = self._hosts[host_name]
        conn = host.conn()

        overlay = self._make_overlay(host, name)
        seed = self._make_seed(host, name, cycle, user_data)
        now = time.time()
        dom_uuid = str(uuid.uuid4())
        meta = lx.metadata_xml(
            cycle=cycle,
            provisioned_at=now,
            created_at=now,
            unit=unit,
            image_digest=host.image_digest,
            pool=self._pool,
        )
        xml = lx.domain_xml(
            name=name,
            uuid=dom_uuid,
            memory_mb=host.cfg.memory_mb,
            vcpus=host.cfg.vcpus,
            overlay_path=overlay,
            seed_path=seed,
            network=host.cfg.network,
            metadata=meta,
            gpu_pci_address=unit if lx.is_gpu_unit(unit) else None,
            # console_log_path intentionally unset → interactive pty console.
            # A file-backed serial log (domain_xml supports it) needs the qemu
            # user to own/relabel the file in the pool dir; under SELinux + the
            # SSH-user-owned pool that fails without weakening host perms, so it's
            # deferred to host setup. Debug via `virsh console` meanwhile.
        )
        dom = conn.defineXML(xml)
        dom.create()  # boot
        log.info(
            "created slot %s:%s on host %s unit %s",
            host_name,
            dom_uuid,
            host_name,
            unit,
        )
        return self._slot(host_name, dom, lx.parse_metadata(meta) or {})

    def rebuild_slot(self, slot: Slot, *, user_data: bytes, cycle: int) -> None:
        host_name, host, dom = self._resolve(slot)
        name = dom.name()
        if dom.isActive():
            dom.destroy()  # ensure off before wiping the disk
        meta = self._read_meta(dom) or {}
        unit = meta.get("unit") or "cpu0"
        created = meta.get("created_at") or time.time()

        self._make_overlay(host, name)  # wipe: fresh COW overlay off the golden image
        self._make_seed(host, name, cycle, user_data)  # NEW instance-id → re-runs
        # Rebuild adopts the host's CURRENT golden (sync_images may have advanced
        # it), so stamp the current digest — this is what clears a slot's stale
        # flag once it has drained onto the new image.
        self._write_meta(
            dom,
            cycle=cycle,
            provisioned_at=time.time(),
            created_at=created,
            unit=unit,
            image_digest=host.image_digest,
        )
        log.info(
            "rebuilt slot %s as cycle %d (left SHUTOFF for drain→start)", slot.id, cycle
        )

    def start_slot(self, slot: Slot) -> None:
        _hn, _host, dom = self._resolve(slot)
        if not dom.isActive():
            dom.create()

    def stop_slot(self, slot: Slot) -> None:
        _hn, _host, dom = self._resolve(slot)
        if dom.isActive():
            dom.shutdown()  # graceful ACPI → SHUTOFF (the timeout action, not destroy)

    def mark_active(self, slot: Slot) -> None:
        _hn, _host, dom = self._resolve(slot)
        meta = self._read_meta(dom) or {}
        self._write_meta(
            dom,
            cycle=meta.get("cycle") or 0,
            provisioned_at=time.time(),
            created_at=meta.get("created_at") or time.time(),
            unit=meta.get("unit") or "cpu0",
            image_digest=meta.get("image_digest"),  # preserve; don't re-stamp here
        )

    def destroy_slot(self, slot: Slot, *, reason: str) -> None:
        log.info("destroying slot %s (reason=%s)", slot.id, reason)
        host_name, host, dom = self._resolve(slot)
        name = dom.name()
        if dom.isActive():
            dom.destroy()
        dom.undefine()
        pool = host.pool_dir()
        self._ssh(
            host,
            f"rm -f {shlex.quote(pool + '/' + name + '.qcow2')} "
            f"{shlex.quote(pool + '/' + name + '-seed.iso')} "
            f"{shlex.quote(pool + '/' + name + '-console.log')}",
        )

    def console_output(self, slot: Slot, *, lines: int | None = None) -> str | None:
        # Deferred (observability Phase O1 is OpenStack-only): the domain runs an
        # interactive pty console with no captured serial-log file. Enabling this
        # needs console_log_path set in the domain XML plus the host-side serial-log
        # ownership/relabel fix (see the console_log_path note in _define / build).
        return None

    def capacity(self) -> Capacity:
        try:
            free = lx.free_unit_count(self._host_units(), self._occupied())
            return Capacity(can_create=free > 0, free_instances=free)
        except Exception:
            log.warning(
                "could not compute libvirt capacity; deferring to max_total",
                exc_info=True,
            )
            return Capacity(can_create=True, free_instances=10**6)
