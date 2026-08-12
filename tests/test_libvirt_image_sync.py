"""LibvirtBackend image delivery: sync_images stages the golden by digest and
pushes idempotently; _gc_goldens removes only unreferenced goldens.

The libvirt connection is never opened — pool_dir is pre-seeded and every host
command (`_ssh`/`_push_file`) is stubbed, so these exercise the delivery logic
without a hypervisor (the live I/O is covered by scripts/smoke_libvirt.py)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from husk.backend import BackendError
from husk.config import BackendConfig, HostConfig
from husk.image_sync import ResolvedImage
from husk.slot import Slot

libvirt = pytest.importorskip("libvirt")
from husk import libvirt_xml as lx  # noqa: E402
from husk.libvirt_backend import LibvirtBackend  # noqa: E402

CURR = "sha256:" + "c" * 64  # "current" image digest → short cccccccccccc


class FakeSync:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = 0
        self.pins: dict[str, set[str]] = {}

    def resolve(self, ref: str, report=None) -> ResolvedImage:
        self.calls += 1
        return ResolvedImage(ref=ref, digest=self.digest, local_path="/cache/img.qcow2")

    def pin(self, owner: str, digests) -> None:
        self.pins[owner] = set(digests)

    def gc(self, *, force: bool = False) -> None:
        pass


def _backend(*, adopt_from=(), **host_overrides):
    host = HostConfig(
        name="h1",
        libvirt_uri="qemu+ssh://u@host/system",
        ssh_target="u@host",
        **host_overrides,
    )
    cfg = BackendConfig(
        name="lv",
        type="libvirt",
        min_ready=1,
        max_total=1,
        image_ref="ghcr.io/acts-project/husk-gpu:v1",
        hosts=(host,),
        adopt_from=adopt_from,
    )
    b = LibvirtBackend(cfg)
    b._hosts["h1"]._pool_dir = "/pool"  # avoid opening a connection
    b._list_raw = lambda: []  # GC sees no live slots unless a test overrides
    # Stage synchronously so a single sync_images() adopts (prod uses a thread).
    b._ops._spawn = lambda fn: fn()
    return b


def test_uses_injected_image_sync():
    # huskd passes one shared ImageSync so the registry pull is single-flighted and
    # the cache is shared across pools; the backend must use exactly that instance.
    shared = FakeSync(CURR)
    host = HostConfig(
        name="h1",
        libvirt_uri="qemu+ssh://u@host/system",
        ssh_target="u@host",
    )
    cfg = BackendConfig(
        name="lv",
        type="libvirt",
        min_ready=1,
        max_total=1,
        image_ref="ghcr.io/acts-project/husk-gpu:v1",
        hosts=(host,),
    )
    b = LibvirtBackend(cfg, image_sync=shared)
    assert b._sync is shared


def test_sync_pulls_pushes_and_stamps_digest():
    b = _backend()
    b._sync = FakeSync(CURR)
    ssh, pushed = [], []
    b._ssh = lambda host, cmd, data=None: (
        ssh.append(cmd) or (b"n\n" if cmd.startswith("test -s") else b"")
    )
    b._push_file = lambda host, local, remote, report=None: pushed.append(
        (local, remote)
    )

    b.sync_images(b.cfg)

    host = b._hosts["h1"]
    assert host.image == "husk-golden-cccccccccccc.qcow2"  # digest-named golden
    assert host.image_digest == CURR
    assert pushed and pushed[0][0] == "/cache/img.qcow2"
    assert any(c.startswith("mv ") for c in ssh)  # atomic temp→final swap
    # current-golden marker written at stage time (cross-pool GC protection)
    assert any(c.startswith("printf ") and ".husk-current-" in c for c in ssh)


def test_sync_is_noop_once_synced():
    b = _backend()
    b._sync = FakeSync(CURR)
    b._ssh = lambda host, cmd, data=None: b"n\n" if cmd.startswith("test -s") else b""
    b._push_file = lambda *a: None

    b.sync_images(b.cfg)
    b.sync_images(b.cfg)
    assert b._sync.calls == 1  # same ref already synced → no second resolve/pull


def test_sync_skips_push_when_host_already_has_digest():
    b = _backend()
    b._sync = FakeSync(CURR)
    pushed = []
    # test -s reports the golden already present → no push, just adopt it.
    b._ssh = lambda host, cmd, data=None: b"y\n" if cmd.startswith("test -s") else b""
    b._push_file = lambda host, local, remote, report=None: pushed.append(remote)

    b.sync_images(b.cfg)
    assert pushed == []
    assert b._hosts["h1"].image_digest == CURR


def test_ref_change_resyncs():
    b = _backend()
    b._sync = FakeSync(CURR)
    b._ssh = lambda host, cmd, data=None: b"n\n" if cmd.startswith("test -s") else b""
    b._push_file = lambda *a: None

    b.sync_images(b.cfg)
    assert b._sync.calls == 1
    b.sync_images(b.cfg.__class__(**{**b.cfg.__dict__, "image_ref": "ghcr.io/o/x:v2"}))
    assert b._sync.calls == 2  # new ref → re-resolved


def test_make_overlay_omits_size_by_default():
    # No disk_size_gb → qemu-img inherits the golden's own virtual size, same
    # as before this knob existed.
    b = _backend()
    b._hosts["h1"].image = "husk-golden-cccccccccccc.qcow2"
    cmds = []
    b._ssh = lambda host, cmd, data=None: cmds.append(cmd) or b""

    b._make_overlay(b._hosts["h1"], "husk-lv-1")

    create = next(c for c in cmds if c.startswith("qemu-img create"))
    assert not create.rstrip().endswith("G")


def test_make_overlay_passes_disk_size_gb_as_the_size_argument():
    b = _backend(disk_size_gb=80)
    b._hosts["h1"].image = "husk-golden-cccccccccccc.qcow2"
    cmds = []
    b._ssh = lambda host, cmd, data=None: cmds.append(cmd) or b""

    b._make_overlay(b._hosts["h1"], "husk-lv-1")

    create = next(c for c in cmds if c.startswith("qemu-img create"))
    assert create.endswith(" 80G")


def test_gc_removes_only_unreferenced_goldens():
    b = _backend()
    host = b._hosts["h1"]
    host.image_digest = CURR  # keep current
    live = "sha256:" + "d" * 64  # a live slot still on this digest → keep
    b._list_raw = lambda: [("h1", object(), {"image_digest": live})]

    listing = (
        "/pool/husk-golden-cccccccccccc.qcow2\n"  # current  → keep
        "/pool/husk-golden-dddddddddddd.qcow2\n"  # live ref → keep
        "/pool/husk-golden-eeeeeeeeeeee.qcow2\n"  # orphan   → rm
    )
    removed = []

    def fake_ssh(host, cmd, data=None):
        if cmd.startswith("ls "):
            return listing.encode()
        if cmd.startswith("rm -f"):
            removed.append(cmd)
        return b""

    b._ssh = fake_ssh
    b._gc_goldens()

    assert len(removed) == 1
    assert "eeeeeeeeeeee" in removed[0]


def test_gc_keeps_marker_protected_golden():
    # A golden that's another pool's CURRENT image (protected by its on-host
    # marker) is kept even with no live slot of ours referencing it — so two
    # libvirt pools sharing a host's pool dir never GC each other's backing files.
    b = _backend()
    b._hosts["h1"].image_digest = CURR  # our current → cccccccccccc
    b._list_raw = lambda: []  # no live slots at all

    listing = (
        "/pool/husk-golden-cccccccccccc.qcow2\n"  # our current      → keep
        "/pool/husk-golden-ffffffffffff.qcow2\n"  # other pool marker → keep
        "/pool/husk-golden-eeeeeeeeeeee.qcow2\n"  # orphan            → rm
    )
    removed = []

    def fake_ssh(host, cmd, data=None):
        if cmd.startswith("cat "):  # the .husk-current-* markers
            return b"husk-golden-ffffffffffff.qcow2\n"
        if cmd.startswith("ls "):
            return listing.encode()
        if cmd.startswith("rm -f"):
            removed.append(cmd)
        return b""

    b._ssh = fake_ssh
    b._gc_goldens()

    assert len(removed) == 1 and "eeeeeeeeeeee" in removed[0]
    assert "ffffffffffff" not in removed[0]  # marker-protected, survived
    assert "cccccccccccc" not in removed[0]


class _FakeDom:
    def __init__(self, uuid: str, name: str, *, ip_raises: bool = False) -> None:
        self._uuid, self._name, self._ip_raises = uuid, name, ip_raises

    def state(self):
        return (1, 0)  # VIR_DOMAIN_RUNNING → ACTIVE

    def UUIDString(self):
        return self._uuid

    def name(self):
        return self._name

    def isActive(self):  # noqa: N802 (libvirt API)
        return True

    def interfaceAddresses(self, src, flags):  # noqa: N802 (libvirt API)
        if self._ip_raises:
            raise RuntimeError("libvirt hiccup")
        return {}


def _real_shell(_host, cmd, data=None):
    """Runs the constructed probe command through an ACTUAL shell. The bug this
    guards against lives in shell short-circuit semantics, not in Python, so a
    mocked _ssh that just returns a canned value would never have caught it."""
    return subprocess.run(["bash", "-c", cmd], capture_output=True, timeout=5).stdout


def test_resolve_emulator_picks_only_the_first_existing_candidate(monkeypatch):
    """Regression test: `A && B || C && D` (no grouping) does not short-circuit
    the way it looks. Once `test -x A && echo A` succeeds, `|| test -x C` skips
    C without updating the exit status — so the stale success from `echo A`
    still satisfies the `&&` before `echo D`, and D fires too even though C was
    never checked. On a host where BOTH candidates genuinely exist, that fed a
    two-line, newline-containing garbage value straight into <emulator>, which
    broke defineXML outright instead of just picking the first candidate."""
    import husk.libvirt_backend as lb

    monkeypatch.setattr(lb, "_EMULATOR_CANDIDATES", (sys.executable, "/bin/ls"))
    b = _backend()
    host = b._hosts["h1"]
    b._ssh = _real_shell

    assert b._resolve_emulator(host) == sys.executable


def test_resolve_emulator_falls_through_to_the_second_candidate(monkeypatch):
    import husk.libvirt_backend as lb

    monkeypatch.setattr(
        lb, "_EMULATOR_CANDIDATES", ("/no/such/binary-xyz", sys.executable)
    )
    b = _backend()
    host = b._hosts["h1"]
    b._ssh = _real_shell

    assert b._resolve_emulator(host) == sys.executable


def test_resolve_emulator_falls_back_when_no_candidate_exists(monkeypatch):
    import husk.libvirt_backend as lb

    monkeypatch.setattr(
        lb, "_EMULATOR_CANDIDATES", ("/no/such/binary-a", "/no/such/binary-b")
    )
    b = _backend()
    host = b._hosts["h1"]
    b._ssh = _real_shell

    assert b._resolve_emulator(host) == lb.lx.DEFAULT_EMULATOR


def test_list_slots_filters_by_pool():
    b = _backend()  # backend name "lv" → self._pool == "lv"
    b._list_raw = lambda: [
        ("h1", _FakeDom("u1", "husk-lv-1"), {"pool": "lv", "unit": "cpu0"}),
        ("h1", _FakeDom("u2", "other-1"), {"pool": "other-pool", "unit": "cpu1"}),
        ("h1", _FakeDom("u3", "legacy"), {"pool": None, "unit": "cpu2"}),
    ]
    slots = b.list_slots()
    assert [s.name for s in slots] == ["husk-lv-1"]  # only this pool's domain


def test_list_slots_adopts_a_retired_pool_name():
    # Renamed lv-old -> lv with adopt_from = ["lv-old"] must still see domains
    # still tagged under the old name, so a rename doesn't orphan them.
    b = _backend(adopt_from=("lv-old",))
    b._list_raw = lambda: [
        ("h1", _FakeDom("u1", "husk-lv-1"), {"pool": "lv", "unit": "cpu0"}),
        ("h1", _FakeDom("u2", "husk-lv-old-1"), {"pool": "lv-old", "unit": "cpu1"}),
        ("h1", _FakeDom("u3", "other-1"), {"pool": "other-pool", "unit": "cpu2"}),
    ]
    slots = b.list_slots()
    assert {s.name for s in slots} == {"husk-lv-1", "husk-lv-old-1"}


class _RedefiningFakeDom:
    """A domain that tracks destroy() and answers metadata()/isActive()/name()
    off fixed values — enough surface for rebuild_slot, not a full fake libvirt."""

    def __init__(self, uuid: str, name: str, meta_xml: str) -> None:
        self._uuid, self._name, self._meta_xml = uuid, name, meta_xml
        self.active = True
        self.destroyed = False

    def isActive(self):  # noqa: N802 (libvirt API)
        return self.active

    def destroy(self):
        self.active = False
        self.destroyed = True

    def UUIDString(self):  # noqa: N802 (libvirt API)
        return self._uuid

    def name(self):
        return self._name

    def metadata(self, *_a):
        return self._meta_xml


class _RedefiningFakeConn:
    """Records every defineXML call so a test can inspect what was redefined."""

    def __init__(self, dom: _RedefiningFakeDom) -> None:
        self._dom = dom
        self.defined: list[str] = []

    def defineXML(self, xml):  # noqa: N802 (libvirt API)
        self.defined.append(xml)
        return self._dom

    def lookupByUUIDString(self, uuid):  # noqa: N802 (libvirt API)
        return self._dom

    def isAlive(self):  # noqa: N802 (libvirt API) — host.conn() liveness probe
        return True


def _rebuild_slot(slot_id: str = "h1:u1"):
    host_name, _, dom_uuid = slot_id.partition(":")
    return Slot(
        id=slot_id,
        name="husk-lv-1",
        status="SHUTOFF",
        task_state=None,
        created_at=1.0,
        flavor_id="",
        image_id="",
        host=host_name,
    )


def test_rebuild_redefines_the_domain_from_current_host_config():
    """memory_mb/vcpus are only ever written into the domain XML at create_slot
    time — rebuild_slot must redefine them too, or a config edit (e.g. the
    hardware bump behind a pool rename) never reaches VMs already running under
    that pool, no matter how many times they're recycled."""
    b = _backend(memory_mb=8192, vcpus=6)
    host = b._hosts["h1"]
    host.emulator = "/usr/bin/qemu-system-x86_64"  # skip the SSH emulator probe
    host.image_digest = "sha256:" + "d" * 64
    b._make_overlay = lambda h, name: f"{h.pool_dir()}/{name}.qcow2"
    b._make_seed = lambda h, name, cycle, user_data: f"{h.pool_dir()}/{name}-seed.iso"

    old_meta = lx.metadata_xml(
        cycle=0,
        provisioned_at=1.0,
        created_at=1.0,
        unit="cpu0",
        image_digest="sha256:" + "o" * 64,
        pool=b._pool,
    )
    dom = _RedefiningFakeDom("u1", "husk-lv-1", old_meta)
    host._conn = _RedefiningFakeConn(dom)

    b.rebuild_slot(_rebuild_slot(), user_data=b"#cloud-config\n", cycle=1)

    assert dom.destroyed  # powered off before redefining, per the docstring
    assert len(host._conn.defined) == 1
    xml = host._conn.defined[0]
    assert "<memory unit='MiB'>8192</memory>" in xml
    assert "<vcpu>6</vcpu>" in xml
    assert "<uuid>u1</uuid>" in xml
    assert "<name>husk-lv-1</name>" in xml


def test_rebuild_preserves_the_original_placement_unit():
    """rebuild_slot must NOT re-run placement — it reuses the unit recorded in
    metadata, so a GPU slot keeps its PCI passthrough and a redefine can never
    collide with a concurrent create() picking a fresh unit."""
    b = _backend(
        gpu_pci_addresses=("0000:21:00.0",), max_slots=None, memory_mb=16384, vcpus=8
    )
    host = b._hosts["h1"]
    host.emulator = "/usr/bin/qemu-system-x86_64"
    host.image_digest = "sha256:" + "d" * 64
    b._make_overlay = lambda h, name: f"{h.pool_dir()}/{name}.qcow2"
    b._make_seed = lambda h, name, cycle, user_data: f"{h.pool_dir()}/{name}-seed.iso"

    old_meta = lx.metadata_xml(
        cycle=0,
        provisioned_at=1.0,
        created_at=1.0,
        unit="0000:21:00.0",
        image_digest="sha256:" + "o" * 64,
        pool=b._pool,
    )
    dom = _RedefiningFakeDom("u1", "husk-lv-gpu-1", old_meta)
    host._conn = _RedefiningFakeConn(dom)

    b.rebuild_slot(_rebuild_slot("h1:u1"), user_data=b"#cloud-config\n", cycle=1)

    xml = host._conn.defined[0]
    assert "0000:21:00.0" in xml  # the hostdev, still attached
    assert "<memory unit='MiB'>16384</memory>" in xml


def test_guest_ip_failure_never_breaks_list_slots():
    # The guest-IP lookup is a metrics nicety that runs inside list_slots. If it
    # could raise, a libvirt hiccup would abort the reconcile tick — i.e. husk would
    # stop managing runners because it couldn't collect metrics. It must degrade to
    # "no ip" (slot simply isn't scraped), never take the tick down.
    b = _backend()
    b._list_raw = lambda: [
        (
            "h1",
            _FakeDom("u1", "husk-lv-1", ip_raises=True),
            {"pool": "lv", "unit": "cpu0"},
        ),
    ]
    slots = b.list_slots()
    assert [s.name for s in slots] == ["husk-lv-1"]  # tick survives
    assert slots[0].ip is None  # just no metrics target this tick


def test_push_rejects_truncated_transfer(tmp_path):
    # A transfer whose landed size != the source size must NOT be published as a
    # usable golden — it raises (the preparer retries/resumes), and a complete
    # transfer proceeds to the atomic mv.
    b = _backend()
    host = b._hosts["h1"]
    src = tmp_path / "img.qcow2"
    src.write_bytes(b"x" * 2048)
    b._push_file = lambda host, local, remote, report=None: (
        None
    )  # pretend the push happened

    def ssh_truncated(host, cmd, data=None):
        if cmd.startswith("test -s"):
            return b"n\n"
        if cmd.startswith("stat -c%s"):
            return b"1024\n"  # only half arrived
        return b""

    b._ssh = ssh_truncated
    with pytest.raises(BackendError, match="incomplete"):
        b._ensure_on_host(host, str(src), "husk-golden-x.qcow2")

    moved = []

    def ssh_complete(host, cmd, data=None):
        if cmd.startswith("test -s"):
            return b"n\n"
        if cmd.startswith("stat -c%s"):
            return b"2048\n"  # full file landed
        if cmd.startswith("mv "):
            moved.append(cmd)
        return b""

    b._ssh = ssh_complete
    b._ensure_on_host(host, str(src), "husk-golden-x.qcow2")
    assert moved  # published only after the size check passed


def test_capacity_is_zero_until_image_staged():
    # OCI mode: a host whose golden hasn't staged yet contributes no capacity, so
    # the controller never attempts a create (nor mints a JIT) before it's ready.
    b = _backend()
    b._occupied = lambda: set()
    assert b.capacity().free_instances == 0
    assert not b.capacity().can_create

    b._hosts["h1"].image_digest = CURR  # golden staged
    cap = b.capacity()
    assert cap.can_create and cap.free_instances > 0


def test_occupied_ignores_untagged_legacy_domains():
    # A pre-pool-tag legacy domain (no "pool" in its metadata) is invisible to
    # list_slots and GC'd by nothing, so if _occupied counted it the unit would be
    # pinned forever and the pool could never grow onto it. Tagged domains (any
    # pool, for cross-pool unit safety) still count.
    b = _backend(max_slots=2)  # host units → cpu0, cpu1
    b._hosts["h1"].image_digest = CURR  # host ready → units contribute capacity
    b._list_raw = lambda: [
        ("h1", object(), {"pool": "lv", "unit": "cpu0"}),  # our live slot → counts
        ("h1", object(), {"unit": "cpu1"}),  # legacy orphan (no pool) → ignored
    ]

    assert b._occupied() == {("h1", "cpu0")}  # cpu1 orphan does not occupy
    cap = b.capacity()
    assert cap.can_create and cap.free_instances == 1  # cpu1 is free to grow onto


def test_no_image_source_is_rejected():
    cfg = BackendConfig(
        name="lv",
        type="libvirt",
        min_ready=1,
        max_total=1,  # no image_ref, no image_name anywhere
        hosts=(
            HostConfig(name="h", libvirt_uri="qemu+ssh://u@h/system", ssh_target="u@h"),
        ),
    )
    with pytest.raises(RuntimeError, match="no image source"):
        LibvirtBackend(cfg)


def test_sync_pins_the_staged_digest_in_the_controller_cache():
    # The cache GC keeps the union of every pool's pins, so a pool must declare the
    # golden it stages from each tick — otherwise a sibling's sweep could evict it.
    b = _backend()
    b._sync = FakeSync(CURR)
    b._ssh = lambda host, cmd, data=None: b"n\n" if cmd.startswith("test -s") else b""
    b._push_file = lambda host, local, remote, report=None: None

    b.sync_images(b.cfg)

    assert b._sync.pins == {"lv": {CURR}}


# ------------------------------------------------------- per-host disk usage
def _scan_ssh(listing: str):
    """An _ssh stub that answers the disk scan with `listing` and nothing else."""

    def _ssh(host, cmd, data=None):
        if cmd.startswith("stat -c '%s %n'"):
            return listing.encode()
        return b"n\n" if cmd.startswith("test -s") else b""

    return _ssh


def test_scan_disk_splits_goldens_from_overlays():
    b = _backend()
    b._ssh = _scan_ssh(
        "100 /pool/husk-golden-cccccccccccc.qcow2\n"
        "200 /pool/husk-golden-dddddddddddd.qcow2\n"
        "7 /pool/husk-lv-1-c3.qcow2\n"
        "9 /pool/husk-lv-2-c1.qcow2\n"
    )

    b._scan_disk()

    usage = {u.kind: u for u in b.disk_usage()}
    assert (usage["golden"].images, usage["golden"].total_bytes) == (2, 300)
    assert (usage["overlay"].images, usage["overlay"].total_bytes) == (2, 16)
    assert {u.host for u in b.disk_usage()} == {"h1"}


def test_scan_disk_on_empty_pool_reports_zero():
    b = _backend()
    b._ssh = _scan_ssh("")

    b._scan_disk()

    assert [(u.kind, u.images, u.total_bytes) for u in b.disk_usage()] == [
        ("golden", 0, 0),
        ("overlay", 0, 0),
    ]


def test_scan_disk_keeps_last_known_when_a_host_is_unreachable():
    """A transient SSH failure must not look like a disk that emptied itself."""
    b = _backend()
    b._ssh = _scan_ssh("100 /pool/husk-golden-cccccccccccc.qcow2\n")
    b._scan_disk()

    def _boom(host, cmd, data=None):
        raise BackendError("host down")

    b._ssh = _boom
    b._scan_disk()

    assert [(u.kind, u.total_bytes) for u in b.disk_usage()] == [
        ("golden", 100),
        ("overlay", 0),
    ]


def test_disk_usage_is_empty_before_the_first_scan():
    assert _backend().disk_usage() == []


def test_sync_images_scans_disk():
    """The scan rides the existing per-tick sync so /metrics never waits on SSH."""
    b = _backend()
    b._sync = FakeSync(CURR)
    b._push_file = lambda *a, **kw: None
    b._ssh = _scan_ssh("42 /pool/husk-golden-cccccccccccc.qcow2\n")

    b.sync_images(b.cfg)

    assert any(u.total_bytes == 42 for u in b.disk_usage())
