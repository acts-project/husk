"""Pure builders for the libvirt backend — no I/O, no `libvirt` import.

Everything here is a pure function of its arguments so it can be unit-tested
without a hypervisor (mirroring how `slot.py` is the testable heart of the
controller). The `LibvirtBackend` in `libvirt_backend.py` does the actual I/O and
leans on these for XML/string construction, state mapping, and slot placement.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape

# libvirt virDomainState codes, redeclared here so this module needs no `libvirt`
# import (keeps it pure + importable in CI without libvirt headers). Values are
# part of libvirt's stable ABI.
DOM_NOSTATE = 0
DOM_RUNNING = 1
DOM_BLOCKED = 2
DOM_PAUSED = 3
DOM_SHUTDOWN = 4  # in the process of shutting down
DOM_SHUTOFF = 5
DOM_CRASHED = 6
DOM_PMSUSPENDED = 7

# husk metadata namespace (the libvirt analog of Nova server metadata).
HUSK_NS = "https://husk.cern/xmlns/slot/1"
MANAGED_BY = "husk"

# Default emulator path on EL/Fedora hosts. Overridable per host later if needed.
DEFAULT_EMULATOR = "/usr/bin/qemu-system-x86_64"


def domain_status(state: int) -> tuple[str, None]:
    """Map a libvirt domain state to the Nova-style status the classifier reads.

    `task_state` is always None: libvirt has no async provisioning task (rebuild
    is a synchronous local operation), so there is nothing to "wait out" the way
    the OpenStack backend does. See the mapping table in the plan.
    """
    if state == DOM_RUNNING:
        return "ACTIVE", None
    if state in (DOM_PAUSED, DOM_PMSUSPENDED, DOM_BLOCKED):
        # Rare/transient; treat as running so a live runner still classifies it.
        return "ACTIVE", None
    if state == DOM_CRASHED:
        return "ERROR", None  # the only status that earns a destroy+recreate
    # SHUTOFF, SHUTDOWN (settling), NOSTATE → powered off / job-done → recycle.
    return "SHUTOFF", None


# --------------------------------------------------------------- slot placement
@dataclass(frozen=True)
class HostUnits:
    """A host and its orderered slot-units (GPU PCI addresses, or `cpuN` ids)."""

    name: str
    units: list[str]


def host_units(gpu_pci_addresses: list[str], max_slots: int) -> list[str]:
    """Expand a host's capacity into slot-unit ids.

    GPU host (any addresses given) → one unit per GPU (the PCI address itself, so
    it doubles as the `<hostdev>` source). CPU host → `cpu0..cpu{max_slots-1}`.
    """
    if gpu_pci_addresses:
        return list(gpu_pci_addresses)
    return [f"cpu{i}" for i in range(max(0, max_slots))]


def is_gpu_unit(unit: str) -> bool:
    """A GPU unit is a PCI address (`DDDD:BB:SS.F`); a CPU unit is `cpuN`."""
    return not unit.startswith("cpu")


def place(
    hosts: list[HostUnits], occupied: set[tuple[str, str]]
) -> tuple[str, str] | None:
    """First free `(host_name, unit)` in host/unit order, or None if the pool is full.

    `occupied` is the set of `(host_name, unit)` currently assigned to live slots
    (read from each domain's `husk-unit` metadata). Deterministic ordering keeps
    placement stable and a GPU is never double-assigned.
    """
    for h in hosts:
        for unit in h.units:
            if (h.name, unit) not in occupied:
                return h.name, unit
    return None


def free_unit_count(hosts: list[HostUnits], occupied: set[tuple[str, str]]) -> int:
    """Total free slot-units across the pool (drives `capacity()`)."""
    return sum(1 for h in hosts for u in h.units if (h.name, u) not in occupied)


# --------------------------------------------------------------- cloud-init seed
def instance_id(name: str, cycle: int) -> str:
    """NoCloud instance-id. MUST change every recycle or cloud-init won't re-run
    (libvirt, unlike Nova, doesn't rotate it for us). `name` is stable across a
    slot's life; `cycle` increments each rebuild, so this rotates exactly once
    per recycle."""
    return f"husk-{name}-c{cycle}"


def meta_data(name: str, cycle: int) -> str:
    """NoCloud `meta-data` file contents (carries the rotating instance-id)."""
    return f"instance-id: {instance_id(name, cycle)}\nlocal-hostname: {name}\n"


# ------------------------------------------------------------------- metadata
def metadata_xml(
    *,
    cycle: int,
    provisioned_at: float,
    created_at: float,
    unit: str,
    image_digest: str | None = None,
    pool: str | None = None,
) -> str:
    """The `<husk:slot>` element stored under the domain `<metadata>` (durable
    state: the libvirt analog of Nova metadata). `unit` is the assigned slot-unit
    (a GPU PCI address, or `cpuN`); `image_digest` is the content digest of the
    golden image this slot was (re)built from — the controller drains a slot whose
    stamped digest no longer matches the host's current image (`image-pipeline.md`
    Phase C). Omitted (empty) in the manual/local-file path, where there is no
    digest to track. `pool` is the owning backend name, so two pools can share a
    host without `list_slots` adopting each other's domains."""
    digest_el = (
        f"<image-digest>{escape(image_digest)}</image-digest>" if image_digest else ""
    )
    pool_el = f"<pool>{escape(pool)}</pool>" if pool else ""
    return (
        f'<slot xmlns="{HUSK_NS}">'
        f"<managed-by>{MANAGED_BY}</managed-by>"
        f"<cycle>{int(cycle)}</cycle>"
        f"<provisioned-at>{provisioned_at:.0f}</provisioned-at>"
        f"<created-at>{created_at:.0f}</created-at>"
        f"<unit>{escape(unit)}</unit>"
        f"{digest_el}"
        f"{pool_el}"
        f"</slot>"
    )


def parse_metadata(xml: str | None) -> dict | None:
    """Parse a `<husk:slot>` metadata element back to a dict, or None if absent/
    unparseable. Tolerant: a malformed/foreign element reads as "not managed"."""
    if not xml:
        return None
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    def _text(tag: str) -> str | None:
        # libvirt's virDomainGetMetadata strips the namespace from the returned
        # element — children come back un-namespaced (`<managed-by>`), while our
        # own serialization and the domain XMLDesc keep it (`{HUSK_NS}managed-by`).
        # The dom.metadata(ELEMENT, HUSK_NS) query already filters by namespace, so
        # matching children by local name here handles both forms safely.
        for el in root:
            if el.tag.rpartition("}")[2] == tag:
                return el.text
        return None

    if _text("managed-by") != MANAGED_BY:
        return None
    out: dict = {
        "managed_by": MANAGED_BY,
        "unit": _text("unit"),
        "image_digest": _text("image-digest"),
        "pool": _text("pool"),
    }
    for key, tag, cast in (
        ("cycle", "cycle", int),
        ("provisioned_at", "provisioned-at", float),
        ("created_at", "created-at", float),
    ):
        raw = _text(tag)
        try:
            out[key] = cast(raw) if raw is not None else None
        except (TypeError, ValueError):
            out[key] = None
    return out


# ------------------------------------------------------------------- domain XML
def _pci_hostdev(addr: str) -> str:
    """`<hostdev>` for full PCI passthrough of a GPU at `DDDD:BB:SS.F`
    (managed='yes' → libvirt binds/unbinds vfio-pci around domain start/stop).
    Matches the validated XML in gpu-passthrough-poc-findings.md."""
    dom_bus, slotfunc = addr.rsplit(":", 1)
    domain, bus = dom_bus.split(":")
    slot, func = slotfunc.split(".")
    return (
        "<hostdev mode='subsystem' type='pci' managed='yes'>"
        "<driver name='vfio'/>"
        "<source><address "
        f"domain='0x{int(domain, 16):04x}' bus='0x{int(bus, 16):02x}' "
        f"slot='0x{int(slot, 16):02x}' function='0x{int(func, 16):x}'/>"
        "</source></hostdev>"
    )


def domain_xml(
    *,
    name: str,
    uuid: str,
    memory_mb: int,
    vcpus: int,
    overlay_path: str,
    seed_path: str,
    network: str,
    metadata: str,
    gpu_pci_address: str | None = None,
    console_log_path: str | None = None,
    emulator: str = DEFAULT_EMULATOR,
) -> str:
    """Full libvirt domain XML for one slot.

    A virtio system disk (the COW overlay), a raw cdrom carrying the NoCloud seed,
    a NAT-network NIC, the durable husk `<metadata>`, and — only when
    `gpu_pci_address` is set — the GPU `<hostdev>`. CPU slots omit the hostdev and
    are otherwise byte-for-byte the same shape.

    When `console_log_path` is given the serial console is captured to that host
    file (the guest is never SSHed, so this file is how we observe cloud-init);
    otherwise it falls back to an interactive `pty`.
    """
    hostdev = _pci_hostdev(gpu_pci_address) if gpu_pci_address else ""
    if console_log_path:
        # A single file-backed serial on port 0 (NOT also a <console> on the same
        # path — two file chardevs on one path makes qemu trip over cleanup). Alma
        # cloud images log kernel + cloud-init to ttyS0, so this captures the whole
        # boot. libvirt implicitly exposes it as the console.
        console = (
            f"<serial type='file'><source path='{escape(console_log_path)}' append='on'/>"
            "<target port='0'/></serial>"
        )
    else:
        console = "<console type='pty'/>"
    return (
        "<domain type='kvm'>"
        f"<name>{escape(name)}</name>"
        f"<uuid>{escape(uuid)}</uuid>"
        f"<metadata>{metadata}</metadata>"
        f"<memory unit='MiB'>{int(memory_mb)}</memory>"
        f"<currentMemory unit='MiB'>{int(memory_mb)}</currentMemory>"
        f"<vcpu>{int(vcpus)}</vcpu>"
        "<os><type arch='x86_64' machine='q35'>hvm</type><boot dev='hd'/></os>"
        "<features><acpi/><apic/></features>"
        "<cpu mode='host-passthrough' check='none'/>"
        "<clock offset='utc'/>"
        "<on_poweroff>destroy</on_poweroff>"
        "<on_reboot>restart</on_reboot>"
        "<on_crash>destroy</on_crash>"
        "<devices>"
        f"<emulator>{escape(emulator)}</emulator>"
        "<disk type='file' device='disk'>"
        "<driver name='qemu' type='qcow2'/>"
        f"<source file='{escape(overlay_path)}'/>"
        "<target dev='vda' bus='virtio'/>"
        "</disk>"
        "<disk type='file' device='cdrom'>"
        "<driver name='qemu' type='raw'/>"
        f"<source file='{escape(seed_path)}'/>"
        "<target dev='sda' bus='sata'/><readonly/>"
        "</disk>"
        f"<interface type='network'><source network='{escape(network)}'/>"
        "<model type='virtio'/></interface>"
        f"{console}"
        "<channel type='unix'>"
        "<target type='virtio' name='org.qemu.guest_agent.0'/></channel>"
        f"{hostdev}"
        "<video><model type='virtio'/></video>"
        "<memballoon model='virtio'/>"
        "</devices>"
        "</domain>"
    )
