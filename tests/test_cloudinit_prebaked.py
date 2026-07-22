"""Prebaked cloud-init: a golden image carries the slow/static layers, so
cloud-init only does the dynamic per-cycle work. The GPU runtime half (modprobe +
CDI) still runs every boot; the install half is dropped. prebaked=False is
unaffected (the OpenStack/stock path)."""

from __future__ import annotations

from husk.cloudinit import (
    PREBAKED_RUNNER_CLOUD_INIT,
    RUNNER_CLOUD_INIT,
    render_cloud_init,
)
from husk.config import RunnerConfig

_NFT_APPLY = "/usr/sbin/nft -f /etc/nftables/husk-egress.nft"


def _nft_section(template: str) -> str:
    """The egress-firewall write_files entry, from the path to just before
    runcmd: — the security-critical ruleset that must not drift between the full
    and prebaked templates."""
    start = template.index("/etc/nftables/husk-egress.nft")
    return template[start : template.index("\nruncmd:", start)]


def test_prebaked_firewall_matches_full():
    # The ruleset is duplicated across the two templates; this is the drift guard.
    assert _nft_section(PREBAKED_RUNNER_CLOUD_INIT) == _nft_section(RUNNER_CLOUD_INIT)


def test_prebaked_drops_all_install_steps():
    out = render_cloud_init("JIT", "http://runner.tgz", prebaked=True).decode()
    # No package installs, no runner download, no installdependencies.
    assert "packages:" not in out
    assert "installdependencies" not in out
    assert "curl" not in out
    assert "@@RUNNER_URL@@" not in out  # the URL is never referenced
    assert "http://runner.tgz" not in out
    # Static files are baked, not re-laid.
    assert "/usr/local/bin/docker" not in out
    assert "label = false" not in out


def test_prebaked_keeps_the_dynamic_work():
    out = render_cloud_init("JIT-BLOB", "URL", prebaked=True).decode()
    assert "JIT-BLOB" in out  # fresh JIT config substituted
    assert "husk-egress.nft" in out and _NFT_APPLY in out  # firewall policy
    assert "systemctl start husk-runner.service" in out  # start the (baked) unit
    assert "shutdown -h" in out  # wall-clock cap


def test_prebaked_cpu_has_no_gpu():
    out = render_cloud_init("JIT", "URL", prebaked=True, gpu=False).decode()
    assert "nvidia" not in out.lower()


def test_prebaked_gpu_keeps_runtime_skips_install():
    out = render_cloud_init("JIT", "URL", prebaked=True, gpu=True).decode()
    # install half skipped...
    assert "dnf -y install nvidia-open-kmod" not in out
    assert "almalinux-release-nvidia-driver" not in out
    assert "nvidia-container-toolkit" not in out
    # ...runtime half kept (hardware-dependent, can't be baked)
    assert "modprobe nvidia" in out
    assert "nvidia-ctk cdi generate" in out
    # and it activates before the egress lockdown (needs the network open)
    assert out.index("modprobe nvidia") < out.index(_NFT_APPLY)


def test_prebaked_false_is_unchanged_full_path():
    # The non-prebaked path is the validated template, GPU still installs+activates.
    # No scrape_cidr → the metrics ingress placeholder resolves away to nothing.
    assert render_cloud_init("J", "U") == (
        RUNNER_CLOUD_INIT.replace("@@JIT@@", "J")
        .replace("@@RUNNER_URL@@", "U")
        .replace("@@METRICS_INGRESS@@", "")
        .replace("@@CVMFS_SET@@", "")
        .replace("@@CVMFS_PROXY@@", "")
        .replace("@@ALLOW_SET@@", "")
        .replace("@@ALLOW_RULE@@", "")
        .replace("@@ALLOW_SETUP@@", "")
        .encode()
    )
    gpu = render_cloud_init("J", "U", gpu=True).decode()
    assert "dnf -y install nvidia-open-kmod" in gpu  # install present on stock GPU
    assert "modprobe nvidia" in gpu


def test_runner_config_defaults_prebaked_off():
    cfg = RunnerConfig(version="2.334.0", labels=["self-hosted"], runner_group="husk")
    assert cfg.prebaked is False
