"""PAT resolution + env-override precedence + the [[pool]] schema in load_config(s)."""

from __future__ import annotations

import pytest

from husk.config import load_config, load_configs

_TOML = """
[github]
repo = "acts-project/husk-test"
{extra}
[[pool]]
name = "openstack-cern"
[pool.runner]
version = "2.334.0"
labels = ["self-hosted"]
runner_group_id = 1
[pool.backend]
cloud = "cern"
image_name = "ALMA10 - x86_64"
flavor_name = "m2.small"
network_name = "CERN_NETWORK"
keypair = "acts-gha"
"""


def _write(tmp_path, extra: str = "") -> str:
    p = tmp_path / "config.toml"
    p.write_text(_TOML.format(extra=extra))
    return str(p)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("GH_TOKEN", "HUSK_GITHUB__PAT", "MY_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_pat_from_gh_token_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_local")
    assert load_config(_write(tmp_path)).github.token == "ghp_local"


def test_husk_env_overrides_gh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_local")
    monkeypatch.setenv("HUSK_GITHUB__PAT", "ghp_explicit")
    assert load_config(_write(tmp_path)).github.token == "ghp_explicit"


def test_custom_pat_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "ghp_custom")
    cfg = load_config(_write(tmp_path, extra='pat_env = "MY_TOKEN"'))
    assert cfg.github.token == "ghp_custom"


def test_pat_path_file_fallback(tmp_path):
    secret = tmp_path / "pat"
    secret.write_text("ghp_fromfile\n")
    cfg = load_config(_write(tmp_path, extra=f'pat_path = "{secret}"'))
    assert cfg.github.token == "ghp_fromfile"


def test_fail_closed_when_no_pat(tmp_path):
    with pytest.raises(RuntimeError, match="GitHub PAT not configured"):
        load_config(_write(tmp_path))


def test_pool_name_drives_backend_name_and_vm_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    cfg = load_config(_write(tmp_path))
    assert cfg.backend.name == "openstack-cern"  # from pool name
    assert cfg.backend.vm_prefix == "husk-openstack-cern"  # derived default


# ------------------------------------------------------------------- multi-pool
_MULTI_TOML = """
[github]
repo = "acts-project/husk-test"
[controller]
http_addr = "127.0.0.1:9100"

[[pool]]
name = "openstack-cpu"
[pool.runner]
version = "2.334.0"
labels = ["self-hosted", "husk-cpu"]
[pool.backend]
type = "openstack"
cloud = "cern"
image_name = "ALMA10 - x86_64"
flavor_name = "m2.small"
network_name = "CERN_NETWORK"
min_ready = 2
max_total = 4

[[pool]]
name = "libvirt-gpu"
[pool.runner]
version = "2.334.0"
labels = ["self-hosted", "gpu"]
gpu = true
prebaked = true
[pool.backend]
type = "libvirt"
image_ref = "ghcr.io/acts-project/husk-gpu:v1"
min_ready = 1
max_total = 1
[[pool.backend.hosts]]
name = "lenovo-gpu"
libvirt_uri = "qemu+ssh://paul@GpuBox/system"
gpu_pci_addresses = ["0000:01:00.0"]
"""


def test_load_configs_two_pools_share_github_and_controller(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    p = tmp_path / "multi.toml"
    p.write_text(_MULTI_TOML)
    cfgs = load_configs(str(p))

    assert [c.backend.name for c in cfgs] == ["openstack-cpu", "libvirt-gpu"]
    assert [c.backend.vm_prefix for c in cfgs] == [
        "husk-openstack-cpu",
        "husk-libvirt-gpu",
    ]
    # Shared sections are identical across pools.
    assert cfgs[0].github == cfgs[1].github
    assert cfgs[0].controller == cfgs[1].controller
    # Per-pool knobs differ.
    cpu, gpu = cfgs
    assert cpu.backend.type == "openstack" and cpu.backend.min_ready == 2
    assert gpu.backend.type == "libvirt" and gpu.runner.gpu and gpu.runner.prebaked
    assert gpu.backend.hosts[0].gpu_pci_addresses == ("0000:01:00.0",)


def test_no_pool_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    p = tmp_path / "empty.toml"
    p.write_text('[github]\nrepo = "acts-project/husk-test"\n')
    with pytest.raises(RuntimeError, match="no \\[\\[pool\\]\\] defined"):
        load_configs(str(p))


def test_duplicate_pool_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    p = tmp_path / "dup.toml"
    p.write_text(
        '[github]\nrepo = "r"\n'
        '[[pool]]\nname = "dup"\n[pool.runner]\nversion="1"\nlabels=["a"]\n'
        '[[pool]]\nname = "dup"\n[pool.runner]\nversion="1"\nlabels=["b"]\n'
    )
    with pytest.raises(RuntimeError, match="duplicate pool name"):
        load_configs(str(p))


# ------------------------------------------------------------------- libvirt
def test_ssh_target_from_uri_preserves_case_and_strips_port():
    from husk.config import _ssh_target_from_uri

    assert _ssh_target_from_uri("qemu+ssh://paul@GpuBox/system") == "paul@GpuBox"
    assert (
        _ssh_target_from_uri("qemu+ssh://paul@host.cern.ch:2222/system")
        == "paul@host.cern.ch"
    )
    assert _ssh_target_from_uri("qemu:///system") == ""


def test_libvirt_config_parses_host_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    p = tmp_path / "multi.toml"
    p.write_text(_MULTI_TOML)
    cfg = load_configs(str(p))[1]  # the libvirt-gpu pool
    assert cfg.backend.type == "libvirt"
    assert len(cfg.backend.hosts) == 1
    h = cfg.backend.hosts[0]
    assert h.ssh_target == "paul@GpuBox"  # derived, case preserved
    assert h.gpu_pci_addresses == ("0000:01:00.0",)
    assert h.max_slots is None
    assert h.pool == "husk" and h.network == "default"  # defaults applied
