"""OpenStack Glance leg: sync_images uploads the OCI golden to Glance once,
rotates self.image_id, drains via Slot.image_stale, and GCs superseded goldens.

The OpenStack SDK is never touched — the backend is built via __new__ with a
fake connection injected, so these exercise the delivery/rollout logic without a
cloud (the live I/O is validated against CERN, like the rest of OpenStackBackend
which has no other unit tests)."""

from __future__ import annotations

import dataclasses

from husk.config import BackendConfig
from husk.image_sync import ResolvedImage
from husk.openstack_backend import GLANCE_PREFIX, OpenStackBackend
from husk.ops import OpStore

CURR = "sha256:" + "c" * 64  # short cccccccccccc → husk-golden-cccccccccccc
REF = "ghcr.io/acts-project/husk-base:v1"


class FakeSync:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = 0

    def resolve(self, ref: str, report=None) -> ResolvedImage:
        self.calls += 1
        return ResolvedImage(ref=ref, digest=self.digest, local_path="/cache/img.qcow2")


class FakeImage:
    def __init__(self, id: str, name: str) -> None:
        self.id = id
        self.name = name


class FakeImageProxy:
    def __init__(self) -> None:
        self.images_list: list[FakeImage] = []
        self.deleted: list[str] = []

    def find_image(self, name):
        return next((im for im in self.images_list if im.name == name), None)

    def images(self):
        return list(self.images_list)

    def delete_image(self, image_id, ignore_missing=True):
        self.deleted.append(image_id)
        self.images_list = [im for im in self.images_list if im.id != image_id]


class FakeServer:
    def __init__(self, id: str, image_id: str) -> None:
        self.id = id
        self.name = id
        self.status = "ACTIVE"
        self.task_state = None
        self.image = {"id": image_id}
        self.flavor = {"id": "flavor-1"}
        self.created_at = None
        self.fault = None
        self.metadata = {"managed-by": "husk", "husk-pool": "os"}

    def to_dict(self):
        return {}


class FakeCompute:
    def __init__(self, servers) -> None:
        self._servers = servers

    def servers(self, details=True):
        return list(self._servers)


class FakeConn:
    def __init__(self, servers=None) -> None:
        self.image = FakeImageProxy()
        self.compute = FakeCompute(servers or [])
        self.created: list[dict] = []
        self._ids = iter(f"img-new-{i}" for i in range(99))

    def create_image(self, **kw):
        im = FakeImage(next(self._ids), kw["name"])
        self.image.images_list.append(im)
        self.created.append(kw)
        return im


def _backend(ref: str = REF, servers=None) -> OpenStackBackend:
    b = OpenStackBackend.__new__(OpenStackBackend)
    b.cfg = BackendConfig(
        name="os", type="openstack", min_ready=1, max_total=1, image_ref=ref
    )
    b.conn = FakeConn(servers or [])
    b._warnings = {}
    b._pool = b.cfg.name
    b._sync = FakeSync(CURR)
    b._backend_ref = ref
    b._synced_ref = ""
    b._image_digest = None
    b.image_id = None
    b._image_conn = b.conn  # upload via the same fake conn (no second connect)
    # Stage synchronously so a single sync_images() adopts (prod uses a thread).
    b._ops = OpStore(spawn=lambda fn: fn())
    return b


def test_sync_uploads_and_adopts_golden():
    b = _backend()
    b.sync_images(b.cfg)

    assert b.image_id == "img-new-0"
    assert b._image_digest == CURR
    up = b.conn.created[0]
    assert up["name"] == f"{GLANCE_PREFIX}cccccccccccc"
    assert up["disk_format"] == "qcow2" and up["container_format"] == "bare"
    assert up["filename"] == "/cache/img.qcow2"


def test_sync_reuses_present_image_without_upload():
    b = _backend()
    b.conn.image.images_list.append(
        FakeImage("existing", f"{GLANCE_PREFIX}cccccccccccc")
    )
    b.sync_images(b.cfg)

    assert b.image_id == "existing"
    assert b.conn.created == []  # content-addressed name already present → no upload


def test_sync_is_noop_once_synced():
    b = _backend()
    b.sync_images(b.cfg)
    b.sync_images(b.cfg)
    assert b._sync.calls == 1  # same ref already current → no second resolve/upload


def test_ref_change_resyncs():
    b = _backend()
    b.sync_images(b.cfg)
    assert b._sync.calls == 1
    b.sync_images(dataclasses.replace(b.cfg, image_ref="ghcr.io/o/x:v2"))
    assert b._sync.calls == 2  # new ref → re-resolved/uploaded


def test_gc_removes_only_superseded_goldens():
    b = _backend(servers=[FakeServer("s1", "img-live")])
    b.image_id = "img-cur"
    b.conn.image.images_list = [
        FakeImage("img-cur", f"{GLANCE_PREFIX}cccccccccccc"),  # current  → keep
        FakeImage("img-live", f"{GLANCE_PREFIX}dddddddddddd"),  # live ref → keep
        FakeImage("img-orphan", f"{GLANCE_PREFIX}eeeeeeeeeeee"),  # orphan → rm
        FakeImage("stock", "ALMA10 - x86_64"),  # not ours → never touched
    ]
    b._gc_glance()
    assert b.conn.image.deleted == ["img-orphan"]


def test_capacity_zero_while_staging():
    # OCI mode, image not uploaded to Glance yet → zero capacity, so no create is
    # attempted (and no JIT runner minted) until the golden lands.
    b = _backend()  # image_ref set, image_id None
    cap = b.capacity()
    assert cap.free_instances == 0 and not cap.can_create


def test_slot_image_stale_only_in_oci_mode():
    b = _backend()
    b.image_id = "img-cur"

    assert b._slot(FakeServer("s", "img-old")).image_stale is True
    assert b._slot(FakeServer("s", "img-cur")).image_stale is False

    b._backend_ref = ""  # legacy image_name mode → nothing to roll onto
    assert b._slot(FakeServer("s", "img-old")).image_stale is False
