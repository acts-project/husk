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
import shlex
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from husk import libvirt_xml as lx
from husk.backend import BackendError, ListSlotsError
from husk.config import BackendConfig, HostConfig
from husk.slot import Capacity, Slot

log = logging.getLogger("husk.libvirt")

# Timeouts — a control plane must never be wedged by one unresponsive host.
_SSH_TIMEOUT_S = 60  # cap each host-side qemu-img/genisoimage/rm command
_SSH_CONNECT_TIMEOUT_S = 15  # cap the ssh TCP/auth handshake
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
        self.image = cfg.image_name or golden_image
        self.units = lx.host_units(list(cfg.gpu_pci_addresses), cfg.max_slots or 1)
        self._conn = None
        self._pool_dir: str | None = None

    def conn(self):
        """Open (or reopen a dropped) libvirt connection."""
        if self._conn is None or not self._alive():
            log.debug("opening libvirt connection %s", self.cfg.libvirt_uri)
            self._conn = libvirt.open(self.cfg.libvirt_uri)
            try:  # bound in-flight RPCs to a wedged host (needs the event loop)
                self._conn.setKeepAlive(_KEEPALIVE_INTERVAL_S, _KEEPALIVE_COUNT)
            except libvirt.libvirtError:
                log.debug("keepalive unsupported on %s", self.cfg.libvirt_uri)
        return self._conn

    def _alive(self) -> bool:
        try:
            return bool(self._conn.isAlive())
        except Exception:
            return False

    def pool_dir(self) -> str:
        """Absolute host path of the storage pool's target dir (where overlays +
        seeds live, and where the golden qcow2 is expected)."""
        if self._pool_dir is None:
            pool = self.conn().storagePoolLookupByName(self.cfg.pool)
            path = ET.fromstring(pool.XMLDesc()).findtext("target/path")
            if not path:
                raise BackendError(f"pool {self.cfg.pool!r} has no target path")
            self._pool_dir = path
        return self._pool_dir


class LibvirtBackend:
    def __init__(self, cfg: BackendConfig) -> None:
        if libvirt is None:
            raise RuntimeError(
                "libvirt-python not installed; install the extra: pip install 'husk[libvirt]'"
            )
        if not cfg.hosts:
            raise RuntimeError(
                "libvirt backend requires at least one [[backend.hosts]]"
            )
        self.cfg = cfg
        self._hosts: dict[str, _HostConn] = {}
        for h in cfg.hosts:
            if h.gpu_pci_addresses and h.max_slots is not None:
                raise RuntimeError(
                    f"host {h.name!r}: set either gpu_pci_addresses (GPU) or "
                    "max_slots (CPU), not both"
                )
            if h.name in self._hosts:
                raise RuntimeError(f"duplicate host name {h.name!r}")
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
        self, dom, *, cycle: int, provisioned_at: float, created_at: float, unit: str
    ) -> None:
        meta = lx.metadata_xml(
            cycle=cycle, provisioned_at=provisioned_at, created_at=created_at, unit=unit
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

    def _slot(self, host_name: str, dom, meta: dict) -> Slot:
        state, _reason = dom.state()
        status, task = lx.domain_status(state)
        return Slot(
            id=f"{host_name}:{dom.UUIDString()}",
            name=dom.name(),
            status=status,
            task_state=task,
            created_at=meta.get("created_at") or 0.0,
            flavor_id=host_name,
            image_id=self._hosts[host_name].image,
            cycle=meta.get("cycle") or 0,
            provisioned_at=meta.get("provisioned_at"),
            fault=None,
        )

    def _resolve(self, slot: Slot):
        host_name, _, dom_uuid = slot.id.partition(":")
        host = self._hosts.get(host_name)
        if host is None:
            raise BackendError(f"slot {slot.id} references unknown host {host_name!r}")
        return host_name, host, host.conn().lookupByUUIDString(dom_uuid)

    def _occupied(self) -> set[tuple[str, str]]:
        return {
            (host_name, meta["unit"])
            for host_name, _dom, meta in self._list_raw()
            if meta.get("unit")
        }

    def _host_units(self) -> list[lx.HostUnits]:
        return [lx.HostUnits(name, h.units) for name, h in self._hosts.items()]

    # --------------------------------------------------------------- backend
    def list_slots(self) -> list[Slot]:
        try:
            raw = self._list_raw()
        except Exception as e:  # libvirt/SSH/network — MUST raise, never []
            raise ListSlotsError(f"list domains failed: {e}") from e
        slots = [self._slot(hn, dom, meta) for hn, dom, meta in raw]
        log.debug(
            "listed %d managed slot(s) across %d host(s)", len(slots), len(self._hosts)
        )
        return slots

    def create_slot(self, *, user_data: bytes, name: str, cycle: int) -> Slot:
        placed = lx.place(self._host_units(), self._occupied())
        if placed is None:
            raise BackendError("no free slot-units across host pool")
        host_name, unit = placed
        host = self._hosts[host_name]
        conn = host.conn()

        overlay = self._make_overlay(host, name)
        seed = self._make_seed(host, name, cycle, user_data)
        now = time.time()
        dom_uuid = str(uuid.uuid4())
        meta = lx.metadata_xml(
            cycle=cycle, provisioned_at=now, created_at=now, unit=unit
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
        self._write_meta(
            dom, cycle=cycle, provisioned_at=time.time(), created_at=created, unit=unit
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
