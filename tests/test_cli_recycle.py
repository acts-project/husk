"""`huskctl recycle` target selection + stop semantics (the _recycle helper),
plus the CLI's pool fan-out (--all across every pool)."""

from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from conftest import make_runner, make_slot
from husk.cli import _recycle as _arecycle
from husk.cli import huskctl_app
from husk.fake_backend import FakeBackend, FakeGitHub


def _recycle(backend, github, **kwargs):
    """Drive the (now async) helper to completion. It takes a LIST of clients now
    — one per target the pool serves — so wrap the single fake."""
    return asyncio.run(_arecycle(backend, [github], **kwargs))


class _NullTokens:
    """Recycle only needs a client per target; no real credential is involved.

    `installations` is here because recycle now *discovers* its targets rather
    than reading them from config — the allowlist alone can't tell it which orgs
    the App is actually installed on."""

    async def installations(self, *, refresh: bool = False) -> list[dict]:
        return [
            {"id": 11, "account": {"login": "acts-project", "type": "Organization"}}
        ]

    async def aclose(self) -> None: ...


def _names(slots):
    return sorted(s.name for s in slots)


_cli = CliRunner()

_TWO_POOLS = """
[github]
app_id = 123456

[[pool]]
name = "openstack-cpu"
target = { org = "acts-project", group = "husk" }
[pool.runner]
version = "2.334.0"
labels = ["self-hosted", "cpu"]
[pool.backend]
type = "openstack"
cloud = "cern"
image_name = "ALMA10 - x86_64"
flavor_name = "m2.small"
network_name = "CERN_NETWORK"
[[pool]]
name = "libvirt-gpu"
target = { org = "acts-project", group = "husk" }
[pool.runner]
version = "2.334.0"
labels = ["self-hosted", "gpu"]
[pool.backend]
type = "libvirt"
image_ref = "ghcr.io/acts-project/husk-gpu:v1"
[[pool.backend.hosts]]
name = "gpubox"
libvirt_uri = "qemu+ssh://paul@GpuBox/system"
gpu_pci_addresses = ["0000:01:00.0"]
"""


def _two_pool_cli(tmp_path, monkeypatch):
    """Write a 2-pool config and stub _build so each pool gets its own fake
    backend (one ACTIVE slot each). Returns (config_path, {pool: backend})."""
    monkeypatch.setenv(
        "HUSK_GITHUB__PRIVATE_KEY",
        "-----BEGIN RSA PRIVATE KEY-----\nnotreal\n-----END RSA PRIVATE KEY-----\n",
    )
    cfg = tmp_path / "config.toml"
    cfg.write_text(_TWO_POOLS)
    backends = {
        "openstack-cpu": FakeBackend(
            slots=[make_slot(id="o-1", name="husk-openstack-cpu-1", status="ACTIVE")]
        ),
        "libvirt-gpu": FakeBackend(
            slots=[make_slot(id="l-1", name="husk-libvirt-gpu-1", status="ACTIVE")]
        ),
    }
    monkeypatch.setattr(
        "husk.cli._backend_for", lambda c, image_sync=None: backends[c.backend.name]
    )
    monkeypatch.setattr("husk.cli._tokens", lambda c: _NullTokens())
    monkeypatch.setattr("husk.github.GitHubClient", lambda **kw: FakeGitHub())
    return str(cfg), backends


def test_all_with_no_pool_recycles_every_pool(tmp_path, monkeypatch):
    cfg, backends = _two_pool_cli(tmp_path, monkeypatch)
    result = _cli.invoke(huskctl_app, ["recycle", "--all", "-c", cfg])
    assert result.exit_code == 0, result.output
    # every pool's slot was stopped → SHUTOFF (the NEEDS_RECYCLE trigger)
    assert backends["openstack-cpu"].ops() == ["stop"]
    assert backends["libvirt-gpu"].ops() == ["stop"]
    assert "stopped 2 slot(s)" in result.output


def test_all_with_pool_scopes_to_one(tmp_path, monkeypatch):
    cfg, backends = _two_pool_cli(tmp_path, monkeypatch)
    result = _cli.invoke(
        huskctl_app, ["recycle", "--all", "-c", cfg, "--pool", "libvirt-gpu"]
    )
    assert result.exit_code == 0, result.output
    assert backends["libvirt-gpu"].ops() == ["stop"]
    assert backends["openstack-cpu"].ops() == []  # untouched


def test_one_pool_failure_does_not_abort_the_others(tmp_path, monkeypatch):
    cfg, backends = _two_pool_cli(tmp_path, monkeypatch)
    backends["openstack-cpu"].raise_on_list = True  # wedged pool
    result = _cli.invoke(huskctl_app, ["recycle", "--all", "-c", cfg])
    assert result.exit_code == 1  # surfaced as failure...
    assert "recycle failed for openstack-cpu" in result.output
    assert backends["libvirt-gpu"].ops() == ["stop"]  # ...but the healthy pool ran


def test_named_slot_still_needs_pool_when_multipool(tmp_path, monkeypatch):
    cfg, _ = _two_pool_cli(tmp_path, monkeypatch)
    result = _cli.invoke(huskctl_app, ["recycle", "husk-openstack-cpu-1", "-c", cfg])
    assert result.exit_code == 2
    assert "pass --pool" in result.output


def test_all_stops_idle_active_slots():
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-1", name="husk-1", status="ACTIVE"),
            make_slot(id="vm-2", name="husk-2", status="ACTIVE"),
        ]
    )
    acted, skipped, unknown = _recycle(
        backend, FakeGitHub(), names=[], all_slots=True, force=False, dry_run=False
    )
    assert _names(acted) == ["husk-1", "husk-2"] and not skipped and not unknown
    # a stop drives each to SHUTOFF — the NEEDS_RECYCLE trigger huskd reconciles
    assert backend.ops() == ["stop", "stop"]
    assert all(s.status == "SHUTOFF" for s in backend.slots)


def test_busy_slot_skipped_without_force():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(
        runners=[make_runner(name="husk-1-c0", status="online", busy=True)]
    )
    acted, skipped, _ = _recycle(
        backend, github, names=[], all_slots=True, force=False, dry_run=False
    )
    assert not acted and backend.calls == []
    assert len(skipped) == 1 and "busy" in skipped[0][1]


def test_busy_slot_recycled_with_force():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub(
        runners=[make_runner(name="husk-1-c0", status="online", busy=True)]
    )
    acted, skipped, _ = _recycle(
        backend, github, names=["husk-1"], all_slots=False, force=True, dry_run=False
    )
    assert _names(acted) == ["husk-1"] and not skipped
    assert backend.ops() == ["stop"]


def test_non_active_slot_skipped():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="SHUTOFF")])
    acted, skipped, _ = _recycle(
        backend, FakeGitHub(), names=[], all_slots=True, force=False, dry_run=False
    )
    assert not acted and backend.calls == []
    assert len(skipped) == 1 and "not ACTIVE" in skipped[0][1]


def test_dry_run_changes_nothing():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    acted, _, _ = _recycle(
        backend, FakeGitHub(), names=[], all_slots=True, force=False, dry_run=True
    )
    assert _names(acted) == ["husk-1"]  # reported as a target...
    assert backend.calls == []  # ...but no stop issued
    assert backend.slots[0].status == "ACTIVE"


def test_select_by_id_and_name_unknown_reported():
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-1", name="husk-1", status="ACTIVE"),
            make_slot(id="vm-2", name="husk-2", status="ACTIVE"),
        ]
    )
    acted, skipped, unknown = _recycle(
        backend,
        FakeGitHub(),
        names=["vm-1", "husk-2", "ghost"],
        all_slots=False,
        force=False,
        dry_run=False,
    )
    assert _names(acted) == ["husk-1", "husk-2"]
    assert unknown == ["ghost"] and not skipped


def test_duplicate_tokens_stop_once():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    acted, _, _ = _recycle(
        backend,
        FakeGitHub(),
        names=["vm-1", "husk-1"],  # same slot via id and name
        all_slots=False,
        force=False,
        dry_run=False,
    )
    assert _names(acted) == ["husk-1"]
    assert backend.ops() == ["stop"]


def test_github_list_failure_does_not_block_recycle():
    backend = FakeBackend(slots=[make_slot(id="vm-1", name="husk-1", status="ACTIVE")])
    github = FakeGitHub()
    github.raise_on_list = True  # can't tell busy → best-effort, still recycle
    acted, skipped, _ = _recycle(
        backend, github, names=[], all_slots=True, force=False, dry_run=False
    )
    assert _names(acted) == ["husk-1"] and not skipped
