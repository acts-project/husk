"""LibvirtBackend image delivery: sync_images stages the golden by digest and
pushes idempotently; _gc_goldens removes only unreferenced goldens.

The libvirt connection is never opened — pool_dir is pre-seeded and every host
command (`_ssh`/`_push_file`) is stubbed, so these exercise the delivery logic
without a hypervisor (the live I/O is covered by scripts/smoke_libvirt.py)."""

from __future__ import annotations

import pytest

from husk.config import BackendConfig, HostConfig
from husk.image_sync import ResolvedImage

libvirt = pytest.importorskip("libvirt")
from husk.libvirt_backend import LibvirtBackend  # noqa: E402

CURR = "sha256:" + "c" * 64  # "current" image digest → short cccccccccccc


class FakeSync:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = 0

    def resolve(self, ref: str) -> ResolvedImage:
        self.calls += 1
        return ResolvedImage(ref=ref, digest=self.digest, local_path="/cache/img.qcow2")


def _backend(**host_overrides):
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
    )
    b = LibvirtBackend(cfg)
    b._hosts["h1"]._pool_dir = "/pool"  # avoid opening a connection
    b._list_raw = lambda: []  # GC sees no live slots unless a test overrides
    # Stage synchronously so a single sync_images() adopts (prod uses a thread).
    b._preparer._spawn = lambda fn: fn()
    return b


def test_sync_pulls_pushes_and_stamps_digest():
    b = _backend()
    b._sync = FakeSync(CURR)
    ssh, pushed = [], []
    b._ssh = lambda host, cmd, data=None: (
        ssh.append(cmd) or (b"n\n" if cmd.startswith("test -s") else b"")
    )
    b._push_file = lambda host, local, remote: pushed.append((local, remote))

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
    b._push_file = lambda host, local, remote: pushed.append(remote)

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
    def __init__(self, uuid: str, name: str) -> None:
        self._uuid, self._name = uuid, name

    def state(self):
        return (1, 0)  # VIR_DOMAIN_RUNNING → ACTIVE

    def UUIDString(self):
        return self._uuid

    def name(self):
        return self._name


def test_list_slots_filters_by_pool():
    b = _backend()  # backend name "lv" → self._pool == "lv"
    b._list_raw = lambda: [
        ("h1", _FakeDom("u1", "husk-lv-1"), {"pool": "lv", "unit": "cpu0"}),
        ("h1", _FakeDom("u2", "other-1"), {"pool": "other-pool", "unit": "cpu1"}),
        ("h1", _FakeDom("u3", "legacy"), {"pool": None, "unit": "cpu2"}),
    ]
    slots = b.list_slots()
    assert [s.name for s in slots] == ["husk-lv-1"]  # only this pool's domain


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
