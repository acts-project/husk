"""In-guest metrics (observability Phase O2): `scrape_cidr` opens :9100 to exactly
one source and starts the baked node_exporter. It is opt-in and fail-closed —
unset means no ingress rule and no exporter, so a pool whose scraper source isn't
known yet (e.g. OpenStack before we know where central Prometheus lives) renders
exactly the ruleset it does today."""

from __future__ import annotations

import pytest

from husk.cloudinit import render_cloud_init

FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\\nnotreal\\n-----END RSA PRIVATE KEY-----\\n"
)

_START = "systemctl start husk-node-exporter.service"
_NFT_APPLY = "/usr/sbin/nft -f /etc/nftables/husk-egress.nft"


def _render(**kw) -> str:
    return render_cloud_init("JIT", "URL", prebaked=True, **kw).decode()


def test_no_scrape_cidr_is_fail_closed():
    out = _render()
    assert "9100" not in out  # no ingress rule at all...
    assert _START not in out  # ...and nothing listening
    assert "chain input" not in out


def test_scrape_cidr_opens_9100_to_that_source_only():
    out = _render(scrape_cidr="137.138.50.0/24")
    assert "tcp dport 9100 ip saddr { 137.138.50.0/24 } accept" in out
    # Everything else on :9100 is dropped — the allowlist is the access control,
    # since node_exporter itself has no TLS and no auth.
    assert "tcp dport 9100 drop" in out
    assert out.index("accept") < out.index("tcp dport 9100 drop")  # allow precedes drop


def test_ingress_chain_narrows_only_9100():
    # policy accept: the chain must not change the rest of the ingress posture.
    out = _render(scrape_cidr="10.0.0.0/8")
    assert "type filter hook input priority 0; policy accept;" in out


def test_hidden_from_the_guests_own_job():
    # The untrusted runner must not read host metrics. Traffic to any LOCAL address
    # (loopback or the guest's own IP) is delivered via lo, so this drops all
    # in-guest access — and it must precede the accept so it holds even wide open.
    out = _render(scrape_cidr="0.0.0.0/0")
    assert 'iif "lo" tcp dport 9100 drop' in out
    assert out.index('iif "lo" tcp dport 9100 drop') < out.index("saddr")


def test_exporter_starts_after_the_firewall_and_before_the_runner():
    out = _render(scrape_cidr="192.168.122.1/32")
    # After the firewall: :9100 is never briefly open to the world during boot.
    assert out.index(_NFT_APPLY) < out.index(_START)
    # Before the runner: metrics are live for the whole (untrusted) job.
    assert out.index(_START) < out.index("systemctl start husk-runner.service")


def test_libvirt_bridge_source_is_expressible():
    # libvirt's scraper is the host's bridge — the host proxy, not Prometheus, is
    # the only client the guest ever sees.
    out = _render(scrape_cidr="192.168.122.1/32")
    assert "tcp dport 9100 ip saddr { 192.168.122.1/32 } accept" in out


def test_ipv6_source_uses_ip6_saddr():
    # Inside an `inet` table `ip saddr` matches v4 only — a v6 scraper under it
    # would never match, and the drop would silently close the port.
    out = _render(scrape_cidr="2001:1458:d00:b::/64")
    assert "tcp dport 9100 ip6 saddr { 2001:1458:d00:b::/64 } accept" in out
    assert "ip saddr { 2001" not in out


def test_stock_image_ignores_scrape_cidr():
    # node_exporter lives only in the golden image; the loader rejects this combo,
    # but the renderer must not emit a rule for a port with nothing behind it.
    out = render_cloud_init("JIT", "URL", prebaked=False, scrape_cidr="10.0.0.0/8")
    assert b"9100" not in out
    assert b"node-exporter" not in out


def test_metrics_compose_with_gpu():
    out = _render(gpu=True, scrape_cidr="192.168.122.1/32")
    assert "tcp dport 9100 ip saddr { 192.168.122.1/32 } accept" in out
    assert _START in out
    assert "modprobe nvidia" in out


def test_loader_rejects_scrape_cidr_without_prebaked(tmp_path, monkeypatch):
    from husk.config import load_configs

    monkeypatch.setenv(
        "HUSK_GITHUB__PRIVATE_KEY",
        FAKE_PEM,
    )

    cfg = tmp_path / "c.toml"
    cfg.write_text(
        """
[github]
app_id = 123456

[access]
allowed_orgs = ["acts-project"]

[[pool]]
name = "p1"
[pool.runner]
version = "2.334.0"
labels = ["self-hosted"]
prebaked = false
scrape_cidr = "10.0.0.0/8"
[pool.backend]
type = "openstack"
min_ready = 1
max_total = 2
"""
    )
    with pytest.raises(RuntimeError, match="scrape_cidr requires prebaked"):
        load_configs(str(cfg))
