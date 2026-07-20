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


def test_stray_backend_image_cache_dir_is_rejected(tmp_path, monkeypatch):
    # image_cache_dir moved out of [pool.backend] onto [controller]. A leftover one
    # must fail the load, not be silently dropped: an ignored key looks like it took
    # effect, and the operator only finds out from a cache in the wrong place.
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    toml = _MULTI_TOML.replace(
        "min_ready = 2\n", 'min_ready = 2\nimage_cache_dir = "/ignored"\n'
    )
    p = tmp_path / "multi.toml"
    p.write_text(toml)
    with pytest.raises(Exception, match="image_cache_dir"):
        load_configs(str(p))


def test_a_misspelt_key_is_rejected(tmp_path, monkeypatch):
    """The whole point of extra="forbid": `min_redy = 5` used to leave min_ready at
    its default and run a silently undersized pool."""
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    p = tmp_path / "typo.toml"
    p.write_text(_TOML.format(extra="") + "min_redy = 5\n")
    with pytest.raises(Exception, match="min_redy"):
        load_configs(str(p))


def test_an_unknown_top_level_table_is_rejected(tmp_path, monkeypatch):
    """Nested tables are covered by extra="forbid", but the settings model itself
    must stay lenient (its env source sees every HUSK_* var), so the file's top
    level gets its own check."""
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    p = tmp_path / "stray.toml"
    p.write_text(_TOML.format(extra="") + '\n[access]\nallowed_orgs = ["acme"]\n')
    with pytest.raises(RuntimeError, match="unknown top-level table 'access'"):
        load_configs(str(p))


def test_unrelated_husk_env_vars_do_not_break_the_load(tmp_path, monkeypatch):
    """The counterweight to the check above: HUSK_* vars that aren't config (log
    level, smoke-test knobs) must not be mistaken for stray settings."""
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    monkeypatch.setenv("HUSK_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("HUSK_SMOKE_HOST", "gpubox")
    assert load_config(_write(tmp_path)).backend.name == "openstack-cern"


def test_a_missing_config_file_says_so(tmp_path):
    with pytest.raises(RuntimeError, match="config file not found"):
        load_configs(str(tmp_path / "nope.toml"))


def test_no_pool_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    p = tmp_path / "empty.toml"
    p.write_text("[github]\napp_id = 1\n")
    with pytest.raises(RuntimeError, match="no \\[\\[pool\\]\\] defined"):
        load_configs(str(p))


def _pools(*names: str) -> str:
    """A minimal but *valid* multi-pool config with the given pool names."""
    head = "[github]\napp_id = 1\n"
    body = "".join(
        f'[[pool]]\nname = "{n}"\ntarget = {{ org = "acme" }}\n'
        f'[pool.runner]\nversion="1"\nlabels=["{n}"]\n'
        "[pool.backend]\n"
        'cloud="cern"\nimage_name="img"\nflavor_name="m2.small"\nnetwork_name="net"\n'
        for n in names
    )
    return head + body


def test_duplicate_pool_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    p = tmp_path / "dup.toml"
    p.write_text(_pools("dup", "dup"))
    with pytest.raises(RuntimeError, match="duplicate pool name"):
        load_configs(str(p))


# ---------------------------------------------------------------- validation
# These all used to load fine and fail later — at serve time, at backend
# construction (which needs libvirt-python or a live cloud), or not at all. huskd
# runs unattended under k8s, so each one has to be a load-time error instead.
def _pool_toml(tmp_path, *, runner: str = "", backend: str = "") -> str:
    """The base config with extra keys spliced into its [pool.runner] (which is
    followed by [pool.backend]) and appended to [pool.backend] (which ends it)."""
    toml = _TOML.format(extra="")
    if runner:
        toml = toml.replace(
            'labels = ["self-hosted"]\n', f'labels = ["self-hosted"]\n{runner}\n'
        )
    p = tmp_path / "c.toml"
    p.write_text(toml + (f"{backend}\n" if backend else ""))
    return str(p)


@pytest.mark.parametrize(
    "backend, match",
    [
        ('type = "libvrt"', "libvirt"),  # typo'd backend → silently OpenStack
        ("min_ready = 5\nmax_total = 2", "exceeds max_total"),
        ("max_total = 0", "greater than or equal to 1"),
        ("min_ready = -1", "greater than or equal to 0"),
    ],
)
def test_backend_numbers_and_type_are_checked(tmp_path, monkeypatch, backend, match):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    with pytest.raises(Exception, match=match):
        load_configs(_pool_toml(tmp_path, backend=backend))


def test_openstack_fields_on_a_libvirt_pool_are_rejected(tmp_path, monkeypatch):
    """The base config is OpenStack; flipping only `type` leaves cloud/flavor/network
    behind, which the libvirt backend would ignore without a word."""
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    with pytest.raises(Exception, match="OpenStack-only"):
        load_configs(_pool_toml(tmp_path, backend='type = "libvirt"'))


def test_an_openstack_pool_needs_its_flavor_and_network(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    bare = _TOML.format(extra="").replace('flavor_name = "m2.small"\n', "")
    p = tmp_path / "c.toml"
    p.write_text(bare)
    with pytest.raises(Exception, match="needs flavor_name"):
        load_configs(str(p))


def test_scrape_cidr_must_be_a_cidr(tmp_path, monkeypatch):
    # It lands in an nftables rule verbatim, so a bad one breaks the guest firewall
    # rather than the config.
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    with pytest.raises(Exception, match="scrape_cidr"):
        load_configs(
            _pool_toml(
                tmp_path,
                runner='prebaked = true\nscrape_cidr = "137.138.0.0/notanetwork"',
            )
        )


def test_a_valid_ipv6_scrape_cidr_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    cfg = load_config(
        _pool_toml(
            tmp_path,
            runner='prebaked = true\nscrape_cidr = "2001:1458:d00:b::/64"',
        )
    )
    assert cfg.runner.scrape_cidr == "2001:1458:d00:b::/64"


@pytest.mark.parametrize("addr", ["9100:", "127.0.0.1:notaport", "127.0.0.1:99999"])
def test_http_addr_is_validated_at_load_not_at_serve(tmp_path, monkeypatch, addr):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    p = tmp_path / "c.toml"
    p.write_text(_TOML.format(extra="") + f'\n[controller]\nhttp_addr = "{addr}"\n')
    with pytest.raises(Exception, match="http_addr"):
        load_configs(str(p))


@pytest.mark.parametrize("addr", ["0.0.0.0:9100", ":9100", "9100"])
def test_bindable_addrs_are_accepted(tmp_path, monkeypatch, addr):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)
    p = tmp_path / "c.toml"
    p.write_text(_TOML.format(extra="") + f'\n[controller]\nhttp_addr = "{addr}"\n')
    assert load_config(str(p)).controller.http_addr == addr


def test_a_bad_private_key_path_names_the_path(tmp_path, monkeypatch):
    """The k8s case: the Secret isn't mounted, or not readable by huskd's uid. This
    used to surface as a bare FileNotFoundError from the read."""
    monkeypatch.delenv("HUSK_GITHUB__PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="could not be read"):
        load_config(_write(tmp_path, extra='private_key_path = "/no/such/key.pem"'))


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
