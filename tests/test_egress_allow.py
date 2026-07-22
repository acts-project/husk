"""Egress allowlist: the per-pool [pool.egress] schema and the firewall holes it
renders.

The coarse egress firewall drops CERN-internal ranges wholesale. That is the
security property, but it also blackholes CERN's own package mirrors, so a job
running `dnf install` inside a CERN-built container image fails with a network
timeout far from the cause. This is the explicit, per-pool escape hatch.

Unlike the CVMFS knobs it is NOT prebaked-gated: it depends on nothing baked into
the golden image, only on DNS still being reachable after lockdown."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from husk.cloudinit import render_cloud_init

HOSTS = ("linuxsoft.cern.ch", "cern.ch")

# Contents are never parsed by the loader, only checked for the marker.
_PEM = "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----"


def _render(**kw) -> str:
    base = dict(prebaked=True, egress_allow_hosts=HOSTS)
    base.update(kw)
    return render_cloud_init("JIT", "URL", **base).decode()


# ── fail-closed: absent config changes nothing ───────────────────────────────


@pytest.mark.parametrize("prebaked", [True, False])
def test_no_allowlist_renders_identically(prebaked):
    """An empty list is not merely inert, it is invisible: the rendered user-data
    must be byte-identical to a slot that never had the feature, or every existing
    pool silently gets a new boot path."""
    assert render_cloud_init(
        "JIT", "URL", prebaked=prebaked, egress_allow_hosts=()
    ) == render_cloud_init("JIT", "URL", prebaked=prebaked)


@pytest.mark.parametrize("prebaked", [True, False])
def test_no_allowlist_leaves_no_set_or_rule(prebaked):
    out = render_cloud_init("JIT", "URL", prebaked=prebaked).decode()
    assert "egress_allow" not in out


# ── the rendered ruleset ─────────────────────────────────────────────────────


@pytest.mark.parametrize("prebaked", [True, False])
def test_applies_on_stock_images_too(prebaked):
    """The CVMFS holes are prebaked-only because the client is baked. This has no
    such dependency, so gating it on prebaked would be a limitation with no cause."""
    out = _render(prebaked=prebaked)
    assert "set egress_allow { type ipv4_addr; }" in out
    assert "ip daddr @egress_allow accept" in out


def test_accept_precedes_the_cern_internal_drop():
    """Order is the whole point: nftables takes the first matching rule, so an
    accept placed after the drop would parse fine and do nothing."""
    out = _render()
    assert out.index("ip daddr @egress_allow accept") < out.index(
        "ip daddr { 128.141.0.0/16"
    )


def test_resolve_runs_after_the_firewall_is_applied():
    """`nft add element` needs the table and set to exist, so the resolve cannot be
    hoisted above the apply — and DNS is deliberately still open afterwards."""
    out = _render()
    assert out.index("/usr/sbin/nft -f /etc/nftables/husk-egress.nft") < out.index(
        "nft add element inet husk egress_allow"
    )


def test_resolves_every_host_in_guest():
    out = _render()
    assert "getent ahostsv4 linuxsoft.cern.ch cern.ch" in out


def test_unresolvable_host_does_not_break_boot():
    """Soft failure: no IPs means no hole, not a failed runcmd and a slot that
    never registers."""
    assert '[ -n "$ips" ]' in _render()


def test_coexists_with_the_cvmfs_proxy_hole():
    """Both mechanisms mint their own set; neither placeholder may swallow the
    other's text."""
    out = _render(
        cvmfs_repos=("sft.cern.ch",),
        cvmfs_proxy="http://ca-proxy.cern.ch:3128",
    )
    assert "set cvmfs_proxy { type ipv4_addr; }" in out
    assert "set egress_allow { type ipv4_addr; }" in out
    assert "ip daddr @cvmfs_proxy accept" in out
    assert "ip daddr @egress_allow accept" in out
    assert "@@" not in out


# ── schema ───────────────────────────────────────────────────────────────────


def test_config_parses_allow_hosts(tmp_path, monkeypatch):
    from husk.config import load_config

    cfg = _pool_toml('[pool.egress]\nallow_hosts = ["linuxsoft.cern.ch"]\n')
    p = tmp_path / "c.toml"
    p.write_text(cfg)
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", _PEM)
    assert load_config(p).egress.allow_hosts == ("linuxsoft.cern.ch",)


def test_config_egress_defaults_to_none(tmp_path, monkeypatch):
    """Omitting the table must leave `egress` None, not an empty allowlist — the
    render path keys off None to stay byte-identical."""
    from husk.config import load_config

    p = tmp_path / "c.toml"
    p.write_text(_pool_toml(""))
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", _PEM)
    assert load_config(p).egress is None


@pytest.mark.parametrize(
    "bad",
    [
        "https://linuxsoft.cern.ch",  # scheme
        "linuxsoft.cern.ch:443",  # port
        "linuxsoft.cern.ch/path",  # path
        "137.138.0.0/16",  # CIDR
        "linuxsoft",  # not a FQDN
    ],
)
def test_config_rejects_non_hostnames(tmp_path, monkeypatch, bad):
    """These are pasted into a `getent` command line in-guest; a URL or CIDR would
    resolve to nothing and open no hole, silently."""
    from husk.config import load_config

    p = tmp_path / "c.toml"
    p.write_text(_pool_toml(f'[pool.egress]\nallow_hosts = ["{bad}"]\n'))
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", _PEM)
    with pytest.raises((ValidationError, ValueError)):
        load_config(p)


def _pool_toml(extra: str) -> str:
    return f"""
[github]
app_id = 1

[[pool]]
name = "p"
target = {{ org = "acts-project" }}

[pool.runner]
version = "2.334.0"

[pool.backend]
type = "openstack"
cloud = "cern"
flavor_name = "m2.small"
network_name = "n"
keypair = "k"
image_name = "img"

{extra}
"""


# ── job-container environment ────────────────────────────────────────────────
#
# Lives here because it shares the "husk states a fact, the workflow decides"
# shape with the allowlist above: both exist so a slot can differ from a
# GitHub-hosted runner without the shared job image having to know.

ENV = ("HUSK_APT_MIRROR=http://ch.archive.ubuntu.com/ubuntu",)


@pytest.mark.parametrize("prebaked", [True, False])
def test_no_container_env_renders_identically(prebaked):
    assert render_cloud_init(
        "JIT", "URL", prebaked=prebaked, container_env=()
    ) == render_cloud_init("JIT", "URL", prebaked=prebaked)


@pytest.mark.parametrize("prebaked", [True, False])
def test_container_env_drop_in_written(prebaked):
    out = render_cloud_init("JIT", "URL", prebaked=prebaked, container_env=ENV).decode()
    assert "/etc/containers/containers.conf.d/20-env.conf" in out
    assert "HUSK_APT_MIRROR=http://ch.archive.ubuntu.com/ubuntu" in out


def test_container_env_carries_podman_default_forward():
    """A containers.conf.d drop-in REPLACES a list key rather than appending, so
    omitting the shipped default would silently unset TERM in every job."""
    out = render_cloud_init("JIT", "URL", prebaked=True, container_env=ENV).decode()
    assert 'env = ["TERM=xterm", "HUSK_APT_MIRROR=' in out


def test_container_env_is_valid_yaml_and_leaves_no_placeholder():
    import yaml

    out = render_cloud_init(
        "JIT",
        "URL",
        prebaked=True,
        container_env=ENV,
        cvmfs_repos=("sft.cern.ch",),
        cvmfs_proxy="http://ca-proxy.cern.ch:3128",
        egress_allow_hosts=("linuxsoft.cern.ch",),
    ).decode()
    assert "@@" not in out
    paths = [f["path"] for f in yaml.safe_load(out)["write_files"]]
    assert "/etc/containers/containers.conf.d/20-env.conf" in paths
    assert "/etc/containers/containers.conf.d/10-cvmfs.conf" in paths


@pytest.mark.parametrize(
    "bad",
    [
        "NOEQUALS",  # not an assignment
        "2BAD=x",  # NAME must not start with a digit
        'X="q"',  # quotes would break containers.conf
        "X=a\nY=b",  # newline likewise
    ],
)
def test_config_rejects_bad_container_env(tmp_path, monkeypatch, bad):
    """podman failing to parse containers.conf breaks EVERY container on the slot,
    so a typo here must not survive config load."""
    from husk.config import load_config

    p = tmp_path / "c.toml"
    # json.dumps gives a TOML-legal escaped string, so the quote/newline cases
    # reach the validator instead of failing as malformed TOML.
    p.write_text(_pool_toml(f"[pool.container]\nenv = [{json.dumps(bad)}]\n"))
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", _PEM)
    with pytest.raises((ValidationError, ValueError)):
        load_config(p)
