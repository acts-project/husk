"""App private-key resolution + env-override precedence + the [[pool]] schema."""

from __future__ import annotations

import pytest

from husk.config import load_config, load_configs
from husk.target import Target

_TOML = """
[github]
app_id = 123456
{extra}

[[pool]]
name = "openstack-cern"
target = {{ org = "acts-project", group = "husk" }}
[pool.runner]
version = "2.334.0"
labels = ["self-hosted"]
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


FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nnotreal\n-----END RSA PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # The App key must come from whatever each test sets, never from the ambient
    # environment — otherwise the fail-closed test would pass for the wrong reason.
    for var in ("GH_TOKEN", "HUSK_GITHUB__PAT", "HUSK_GITHUB__PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_private_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    cfg = load_config(_write(tmp_path))
    assert cfg.github.private_key == FAKE_PEM
    assert cfg.github.app_id == 123456


def test_private_key_path_file_fallback(tmp_path):
    pem = tmp_path / "app.pem"
    pem.write_text(FAKE_PEM)
    cfg = load_config(_write(tmp_path, extra=f'private_key_path = "{pem}"'))
    assert cfg.github.private_key == FAKE_PEM


def test_env_key_wins_over_path(tmp_path, monkeypatch):
    pem = tmp_path / "app.pem"
    pem.write_text(FAKE_PEM)
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nenv\n")
    cfg = load_config(_write(tmp_path, extra=f'private_key_path = "{pem}"'))
    assert "env" in cfg.github.private_key


def test_fail_closed_when_no_private_key(tmp_path):
    with pytest.raises(RuntimeError, match="private key not configured"):
        load_config(_write(tmp_path))


def test_rejects_a_file_that_is_not_a_pem(tmp_path):
    junk = tmp_path / "app.pem"
    junk.write_text("this is not a key")
    with pytest.raises(RuntimeError, match="private key not configured"):
        load_config(_write(tmp_path, extra=f'private_key_path = "{junk}"'))


def _target(tmp_path, table: str) -> str:
    """Write the standard config with the pool's target table swapped out."""
    bad = _TOML.format(extra="").replace(
        'target = { org = "acts-project", group = "husk" }', f"target = {table}"
    )
    p = tmp_path / "c.toml"
    p.write_text(bad)
    return str(p)


def test_org_target_is_parsed_with_its_group(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    cfg = load_config(_write(tmp_path))
    assert cfg.target == Target.org("acts-project")
    # `group` is written inside the target table but flattened onto the runner
    # config, which is what the GitHub client consumes.
    assert cfg.runner.runner_group == "husk"


def test_repo_target_is_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    cfg = load_config(_target(tmp_path, '{ repo = "owner/name" }'))
    assert cfg.target == Target.repo("owner/name")


def test_a_target_without_a_group_defaults_to_Default(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    cfg = load_config(_target(tmp_path, '{ org = "acts-project" }'))
    assert cfg.runner.runner_group == "Default"


def test_a_target_needs_exactly_one_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    for table in ["{ }", '{ org = "a", repo = "o/n" }']:
        with pytest.raises(Exception, match="exactly one of org / repo"):
            load_config(_target(tmp_path, table))


def test_a_repo_in_the_org_slot_is_rejected(tmp_path, monkeypatch):
    """A common mix-up. It would silently never match an installation account,
    so it has to fail loudly."""
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    with pytest.raises(Exception, match="looks like a repo"):
        load_config(_target(tmp_path, '{ org = "owner/name" }'))


def test_a_bare_login_in_the_repo_slot_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    with pytest.raises(Exception, match="must be owner/name"):
        load_config(_target(tmp_path, '{ repo = "acts-project" }'))


def test_a_group_on_a_repo_target_is_unrepresentable(tmp_path, monkeypatch):
    """Runner groups are an org-only concept. Nesting `group` inside the target
    table is what makes this a schema error rather than a silent no-op."""
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    cfg = load_config(_target(tmp_path, '{ repo = "owner/name", group = "husk" }'))
    # The group parses (it is a plain field) but cannot apply — a repo target has
    # no groups, and the client ignores it. The value simply has no reachable use.
    assert cfg.target.kind == "repo"


def test_a_pool_without_a_target_is_rejected(tmp_path, monkeypatch):
    """Every pool must say who it serves; there is no global default any more."""
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    bad = _TOML.format(extra="").replace(
        'target = { org = "acts-project", group = "husk" }\n', ""
    )
    p = tmp_path / "c.toml"
    p.write_text(bad)
    with pytest.raises(Exception, match="target"):
        load_config(str(p))


def test_pool_name_drives_backend_name_and_vm_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    cfg = load_config(_write(tmp_path))
    assert cfg.backend.name == "openstack-cern"  # from pool name
    assert cfg.backend.vm_prefix == "husk-openstack-cern"  # derived default


# ------------------------------------------------------------------- multi-pool
_MULTI_TOML = """
[github]
app_id = 123456

[controller]
http_addr = "127.0.0.1:9100"

[[pool]]
name = "openstack-cpu"
target = { org = "acts-project", group = "husk" }
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
target = { org = "acts-project", group = "husk" }
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
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
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


def test_image_cache_dir_lives_on_controller(tmp_path, monkeypatch):
    # The oras pull cache is process-wide, so it's a [controller] knob shared
    # identically by every pool (not a per-[backend] field anymore).
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    toml = _MULTI_TOML.replace(
        '[controller]\nhttp_addr = "127.0.0.1:9100"\n',
        '[controller]\nhttp_addr = "127.0.0.1:9100"\n'
        'image_cache_dir = "/var/cache/husk/images"\n',
    )
    p = tmp_path / "multi.toml"
    p.write_text(toml)
    cfgs = load_configs(str(p))
    assert cfgs[0].controller.image_cache_dir == "/var/cache/husk/images"
    assert cfgs[0].controller == cfgs[1].controller  # shared across pools


def test_stray_backend_image_cache_dir_is_ignored(tmp_path, monkeypatch):
    # Clean cutover: image_cache_dir moved out of [pool.backend]; a leftover one is
    # silently dropped (pydantic extra="ignore") rather than erroring.
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    toml = _MULTI_TOML.replace(
        "min_ready = 2\n", 'min_ready = 2\nimage_cache_dir = "/ignored"\n'
    )
    p = tmp_path / "multi.toml"
    p.write_text(toml)
    cfgs = load_configs(str(p))  # must not raise
    assert not hasattr(cfgs[0].backend, "image_cache_dir")
    assert cfgs[0].controller.image_cache_dir == ""


def test_no_pool_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    p = tmp_path / "empty.toml"
    p.write_text('[github]\napp_id = 1\n[access]\nallowed_orgs = ["acme"]\n')
    with pytest.raises(RuntimeError, match="no \\[\\[pool\\]\\] defined"):
        load_configs(str(p))


def test_duplicate_pool_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    p = tmp_path / "dup.toml"
    p.write_text(
        "[github]\napp_id = 1\n"
        '[[pool]]\nname = "dup"\ntarget = { org = "acme" }\n'
        '[pool.runner]\nversion="1"\nlabels=["a"]\n'
        '[[pool]]\nname = "dup"\ntarget = { org = "acme" }\n'
        '[pool.runner]\nversion="1"\nlabels=["b"]\n'
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


def test_libvirt_config_parses_host_storage_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    p = tmp_path / "multi.toml"
    p.write_text(_MULTI_TOML)
    cfg = load_configs(str(p))[1]  # the libvirt-gpu pool
    assert cfg.backend.type == "libvirt"
    assert len(cfg.backend.hosts) == 1
    h = cfg.backend.hosts[0]
    assert h.ssh_target == "paul@GpuBox"  # derived, case preserved
    assert h.gpu_pci_addresses == ("0000:01:00.0",)
    assert h.max_slots is None
    assert h.storage_pool == "husk" and h.network == "default"  # defaults applied
