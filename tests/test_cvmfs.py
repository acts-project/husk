"""CernVM-FS support: the per-pool [pool.cvmfs] schema and the cloud-init it
renders. The client + autofs are baked into the golden image, so cloud-init lays
only the dynamic layer. Fail-closed (no [pool.cvmfs] → byte-identical to a
non-CVMFS slot), and opens exactly one firewall hole for the configured HTTP
proxy.

The container-side mechanism these tests encode was validated on the real rootless
podman engine: a per-repo containers.conf.d bind injects /cvmfs/<repo> with no -v
flag, whereas a whole-/cvmfs bind trips an autofs-root readdir denial under the
user namespace — hence per-repo binds of eager-mounted trees."""

from __future__ import annotations

import pytest

from husk.cloudinit import _proxy_hosts, render_cloud_init

REPOS = ("sft.cern.ch", "atlas.cern.ch", "atlas-condb.cern.ch")
PROXY = "http://ca-proxy-atlas.cern.ch:3128;http://ca-proxy.cern.ch:3128"


def _render(**kw) -> str:
    base = dict(cvmfs_repos=REPOS, cvmfs_proxy=PROXY, cvmfs_quota_mb=6000)
    base.update(kw)
    return render_cloud_init("JIT", **base).decode()


# ── proxy-host parsing (feeds the in-guest firewall resolve) ──────────────────


def test_proxy_hosts_parses_failover_chain():
    assert _proxy_hosts(PROXY) == ["ca-proxy-atlas.cern.ch", "ca-proxy.cern.ch"]


def test_proxy_hosts_dedupes_and_skips_direct():
    # load-balance (|) + failover (;) separators, a duplicate, and a DIRECT fallback
    assert _proxy_hosts(
        "http://squid-a:3128|http://squid-b:3128;http://squid-a:3128;DIRECT"
    ) == ["squid-a", "squid-b"]


def test_proxy_hosts_empty_for_direct_only():
    assert _proxy_hosts("DIRECT") == []


# ── rendered cloud-init ───────────────────────────────────────────────────────


def test_client_config_written():
    out = _render()
    assert "/etc/cvmfs/default.local" in out
    assert f'CVMFS_HTTP_PROXY="{PROXY}"' in out
    assert "CVMFS_REPOSITORIES=sft.cern.ch,atlas.cern.ch,atlas-condb.cern.ch" in out
    assert "CVMFS_QUOTA_LIMIT=6000" in out


def test_per_repo_bind_drop_in():
    out = _render()
    assert "/etc/containers/containers.conf.d/10-cvmfs.conf" in out
    # Each repo bound individually (NOT a whole-/cvmfs bind).
    for r in REPOS:
        assert f'"/cvmfs/{r}:/cvmfs/{r}"' in out
    assert '"/cvmfs:/cvmfs"' not in out


def test_firewall_proxy_hole_precedes_the_cern_drop():
    out = _render()
    assert "set cvmfs_proxy { type ipv4_addr; }" in out
    assert "ip daddr @cvmfs_proxy accept" in out
    # The accept must sit ABOVE the CERN-internal drop or it never matches.
    assert out.index("ip daddr @cvmfs_proxy accept") < out.index("128.141.0.0/16")


def test_in_guest_resolve_then_eager_mount_ordering():
    out = _render()
    apply_fw = out.index("/usr/sbin/nft -f /etc/nftables/husk-egress.nft")
    resolve = out.index("getent ahostsv4 ca-proxy-atlas.cern.ch ca-proxy.cern.ch")
    populate = out.index("nft add element inet husk cvmfs_proxy")
    autofs = out.index("systemctl start autofs")
    probe = out.index('cvmfs_config probe "$r"')
    start = out.index("systemctl start husk-runner.service")
    # firewall up → resolve+open proxy → eager-mount → runner. The set is populated
    # only after `nft -f` has created the table + empty set.
    assert apply_fw < resolve < populate < autofs < probe < start


def test_eager_mounts_every_repo():
    out = _render()
    assert "for r in sft.cern.ch atlas.cern.ch atlas-condb.cern.ch; do" in out
    assert 'cvmfs_config probe "$r" || true' in out


# ── fail-closed: off unless explicitly + validly configured ───────────────────


def test_no_repos_is_byte_identical_to_plain():
    assert _render(cvmfs_repos=()) == render_cloud_init("JIT").decode()


def test_plain_render_has_no_cvmfs_leftovers():
    out = render_cloud_init("JIT")
    assert b"cvmfs" not in out.lower()
    assert b"@@CVMFS" not in out  # every placeholder resolved away


# ── config schema ─────────────────────────────────────────────────────────────

_CVMFS_TOML = """
[github]
app_id = 1
private_key_path = "{pem}"

[[pool]]
name = "cern"
target = {{ org = "acts-project" }}
[pool.runner]
version = "2.334.0"
arch = "x64"
[pool.backend]
cloud = "cern"
image_name = "husk-base"
flavor_name = "m2.small"
network_name = "CERN_NETWORK"
{cvmfs}
"""


def _load(tmp_path, *, cvmfs=""):
    from husk.config import load_config

    pem = tmp_path / "app.pem"
    pem.write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----\n"
    )
    p = tmp_path / "c.toml"
    p.write_text(_CVMFS_TOML.format(pem=pem, cvmfs=cvmfs))
    return load_config(str(p))


_CVMFS_TABLE = (
    "[pool.cvmfs]\n"
    'repositories = ["sft.cern.ch", "atlas.cern.ch"]\n'
    'http_proxy = "http://ca-proxy.cern.ch:3128"\n'
    "quota_limit_mb = 8000\n"
)


def test_cvmfs_config_parses(tmp_path):
    cfg = _load(tmp_path, cvmfs=_CVMFS_TABLE)
    assert cfg.cvmfs is not None
    assert cfg.cvmfs.repositories == ("sft.cern.ch", "atlas.cern.ch")
    assert cfg.cvmfs.http_proxy == "http://ca-proxy.cern.ch:3128"
    assert cfg.cvmfs.quota_limit_mb == 8000


def test_cvmfs_absent_is_none(tmp_path):
    assert _load(tmp_path).cvmfs is None


def test_cvmfs_defaults_quota(tmp_path):
    cfg = _load(
        tmp_path,
        cvmfs='[pool.cvmfs]\nrepositories = ["sft.cern.ch"]\n'
        'http_proxy = "http://p:3128"\n',
    )
    assert cfg.cvmfs.quota_limit_mb == 4000


def test_cvmfs_rejects_url_like_repo(tmp_path):
    with pytest.raises(Exception, match="bare repo name"):
        _load(
            tmp_path,
            cvmfs='[pool.cvmfs]\nrepositories = ["http://sft.cern.ch"]\n'
            'http_proxy = "http://p:3128"\n',
        )


def test_cvmfs_rejects_empty_repos(tmp_path):
    with pytest.raises(Exception):
        _load(
            tmp_path,
            cvmfs='[pool.cvmfs]\nrepositories = []\nhttp_proxy = "http://p:3128"\n',
        )
