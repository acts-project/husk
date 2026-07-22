"""GPU opt-in cloud-init: off is byte-identical (OpenStack safety); on installs
the native AlmaLinux driver + CDI before the egress firewall."""

from __future__ import annotations

from husk.cloudinit import RUNNER_CLOUD_INIT, render_cloud_init
from husk.config import RunnerConfig


def _legacy(jit: str, url: str) -> bytes:
    # The validated recipe = the template with nothing opted in (no scrape_cidr, no
    # cvmfs), i.e. every optional placeholder resolves away to nothing.
    return (
        RUNNER_CLOUD_INIT.replace("@@JIT@@", jit)
        .replace("@@RUNNER_URL@@", url)
        .replace("@@METRICS_INGRESS@@", "")
        .replace("@@CVMFS_SET@@", "")
        .replace("@@CVMFS_PROXY@@", "")
        .replace("@@ALLOW_SET@@", "")
        .replace("@@ALLOW_RULE@@", "")
        .replace("@@ALLOW_SETUP@@", "")
    ).encode()


def test_gpu_off_is_byte_identical_to_legacy_render():
    # The OpenStack/CPU path must be untouched: gpu=False (the default) reproduces
    # the validated template exactly.
    assert render_cloud_init("JIT", "URL") == _legacy("JIT", "URL")
    assert render_cloud_init("JIT", "URL", gpu=False) == _legacy("JIT", "URL")


def test_gpu_off_mentions_no_nvidia():
    assert "nvidia" not in render_cloud_init("JIT", "URL").decode().lower()


def test_gpu_on_installs_native_driver_and_cdi():
    out = render_cloud_init("JIT", "URL", gpu=True).decode()
    assert "almalinux-release-nvidia-driver" in out  # repo-enabling release pkg
    assert "nvidia-open-kmod" in out  # precompiled open kmod — no DKMS
    assert "nvidia-driver" in out
    assert "nvidia-driver-cuda" in out  # provides nvidia-smi + libcuda for the CDI hook
    assert "nvidia-container-toolkit" in out
    assert "modprobe nvidia" in out
    assert "nvidia-ctk cdi generate" in out


def test_gpu_block_runs_before_egress_firewall():
    # Driver install needs the network; it must precede the firewall lockdown.
    out = render_cloud_init("JIT", "URL", gpu=True).decode()
    # Compare against where the firewall is *applied* in runcmd (the nft path also
    # appears earlier in write_files, where the ruleset file is merely defined).
    assert out.index("nvidia-open-kmod") < out.index(
        "/usr/sbin/nft -f /etc/nftables/husk-egress.nft"
    )


def test_gpu_on_still_a_superset_of_the_base_template():
    # Everything the base template does (runner unit, JIT, firewall) is preserved.
    out = render_cloud_init("JIT-BLOB", "http://runner.tgz", gpu=True).decode()
    assert "JIT-BLOB" in out
    assert "http://runner.tgz" in out
    assert "husk-runner.service" in out
    assert "husk-egress.nft" in out


def test_runner_config_defaults_gpu_off():
    cfg = RunnerConfig(version="2.334.0", labels=["self-hosted"], runner_group="husk")
    assert cfg.gpu is False
