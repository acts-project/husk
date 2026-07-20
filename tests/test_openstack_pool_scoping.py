"""OpenStack slot ownership is scoped to ONE pool.

Several pools — and, under the App migration, several targets — share one
OpenStack project. Before the `husk-pool` tag, `list_slots()` filtered only on
`managed-by=husk`, so every controller saw every other controller's servers,
found no runner matching their prefix, classified them unhealthy and rebuilt
them. Two units tore each other down."""

from __future__ import annotations

import dataclasses
import types

import pytest

from conftest import make_config
from husk.openstack_backend import MANAGED_BY, POOL_KEY, OpenStackBackend


class _Server(types.SimpleNamespace):
    """Stands in for an openstacksdk Server (which exposes `to_dict()` for the
    OS-EXT-STS extension fields)."""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _server(id, name, metadata):
    return _Server(
        id=id,
        name=name,
        metadata=metadata,
        status="ACTIVE",
        task_state=None,
        flavor={"id": "flavor-current"},
        image={"id": "image-current"},
        created_at="2026-07-20T00:00:00Z",
        addresses={},
        fault=None,
    )


def _backend(pool: str, prefix: str, servers: list) -> OpenStackBackend:
    """An OpenStackBackend with its __init__ bypassed — this exercises ownership
    and metadata, not cloud connection setup."""
    cfg = make_config().backend
    cfg = dataclasses.replace(cfg, name=pool, vm_prefix=prefix)
    b = OpenStackBackend.__new__(OpenStackBackend)
    b.cfg = cfg
    b._pool = pool
    b._prefix = prefix
    b._warnings = {}
    b._untagged = set()
    b._backend_ref = ""
    b.image_id = "image-current"
    b.conn = types.SimpleNamespace(
        compute=types.SimpleNamespace(
            servers=lambda details=True: list(servers),
            set_server_metadata=lambda sid, **kw: b._writes.append((sid, kw)),
        )
    )
    b._writes = []
    return b


TAGGED_A = _server("a-1", "husk-gpu-a-1", {"managed-by": MANAGED_BY, POOL_KEY: "gpu-a"})
TAGGED_B = _server("b-1", "husk-gpu-b-1", {"managed-by": MANAGED_BY, POOL_KEY: "gpu-b"})
FOREIGN = _server("x-1", "someone-else-1", {})


def test_a_pool_sees_only_its_own_tagged_servers():
    b = _backend("gpu-a", "husk-gpu-a", [TAGGED_A, TAGGED_B, FOREIGN])
    assert [s.id for s in b.list_slots()] == ["a-1"]


def test_a_sibling_pools_servers_are_invisible():
    """The regression that mattered: gpu-b's server must not reach gpu-a's
    controller, which would rebuild it as 'unhealthy'."""
    b = _backend("gpu-b", "husk-gpu-b", [TAGGED_A, TAGGED_B])
    assert [s.id for s in b.list_slots()] == ["b-1"]


def test_unmanaged_servers_are_still_ignored():
    b = _backend("gpu-a", "husk-gpu-a", [FOREIGN])
    assert b.list_slots() == []


# ------------------------------------------------------------------- legacy
LEGACY = _server("old-1", "husk-gpu-a-7", {"managed-by": MANAGED_BY})


def test_legacy_untagged_server_is_adopted_by_prefix():
    """It must NOT become invisible — nothing would reconcile or delete it, and
    it would bill forever."""
    b = _backend("gpu-a", "husk-gpu-a", [LEGACY])
    assert [s.id for s in b.list_slots()] == ["old-1"]
    assert b._untagged == {"old-1"}


def test_a_legacy_server_is_adopted_by_exactly_one_pool():
    """vm_prefix is unique per pool (enforced by load_configs), so the fallback
    can't hand the same server to two owners."""
    a = _backend("gpu-a", "husk-gpu-a", [LEGACY])
    other = _backend("gpu-b", "husk-gpu-b", [LEGACY])
    assert [s.id for s in a.list_slots()] == ["old-1"]
    assert other.list_slots() == []


def test_mark_active_backfills_the_pool_tag():
    b = _backend("gpu-a", "husk-gpu-a", [LEGACY])
    slot = b.list_slots()[0]
    b.mark_active(slot)
    (sid, kw) = b._writes[-1]
    assert sid == "old-1"
    assert kw[POOL_KEY] == "gpu-a"  # ownership no longer rests on the prefix
    assert "husk-provisioned-at" in kw  # and the original write still happens
    assert b._untagged == set()


def test_mark_active_does_not_rewrite_an_already_tagged_server():
    b = _backend("gpu-a", "husk-gpu-a", [TAGGED_A])
    slot = b.list_slots()[0]
    b.mark_active(slot)
    (_, kw) = b._writes[-1]
    assert POOL_KEY not in kw  # no needless metadata write every bring-up


def test_list_slots_still_raises_rather_than_returning_empty():
    """The fail-safe contract: a listing failure must never look like 'no slots'."""
    from husk.backend import ListSlotsError

    b = _backend("gpu-a", "husk-gpu-a", [])

    def boom(details=True):
        raise RuntimeError("nova 503")

    b.conn.compute.servers = boom
    with pytest.raises(ListSlotsError):
        b.list_slots()
