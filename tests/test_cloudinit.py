"""Cloud-init rendering against a golden image.

Every pool boots a husk golden image, so cloud-init carries only the dynamic
per-cycle layer. Two kinds of assertion here:

  * behavioural — what must (and must not) appear in the render, and in what order;
  * golden-file — the exact bytes, per option combination, in tests/golden/.

The golden files exist because this output is never executed by the test suite: it
runs once, on a VM, at boot. A refactor that changes one byte of indentation inside
a `write_files` block is invisible to every behavioural assertion and fatal on the
guest. Regenerate deliberately with `uv run python tests/golden/regen.py`, and read
the diff — it IS the review."""

from __future__ import annotations

import pathlib

import pytest

from husk.cloudinit import RUNNER_CLOUD_INIT, render_cloud_init
from husk.config import RunnerConfig

from golden_cases import CASES

_NFT_APPLY = "/usr/sbin/nft -f /etc/nftables/husk-egress.nft"
_GOLDEN = pathlib.Path(__file__).parent / "golden"


# ── golden files ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(CASES))
def test_render_matches_golden(name):
    expected = (_GOLDEN / f"{name}.yaml").read_bytes()
    assert render_cloud_init("JITBLOB", **CASES[name]) == expected


def test_no_placeholders_survive_any_combination():
    for name, kw in CASES.items():
        assert "@@" not in render_cloud_init("JIT", **kw).decode(), name


# ── the image carries the static layer; cloud-init does not ──────────────────


def test_no_install_steps():
    out = render_cloud_init("JIT").decode()
    # Packages, the runner tarball and its dependency script are all baked.
    assert "packages:" not in out
    assert "installdependencies" not in out
    assert "actions-runner-linux" not in out
    # As are the static files.
    assert "/usr/local/bin/docker" not in out
    assert "label = false" not in out


def test_keeps_the_dynamic_work():
    out = render_cloud_init("JIT-BLOB").decode()
    assert "JIT-BLOB" in out  # fresh JIT config substituted
    assert "husk-egress.nft" in out and _NFT_APPLY in out  # firewall policy
    assert "systemctl start husk-runner.service" in out  # start the (baked) unit
    assert "shutdown -h" in out  # wall-clock cap


# ── GPU ───────────────────────────────────────────────────────────────────────


def test_gpu_off_mentions_no_nvidia():
    assert "nvidia" not in render_cloud_init("JIT").decode().lower()


def test_gpu_off_is_byte_identical_to_the_default():
    assert render_cloud_init("JIT") == render_cloud_init("JIT", gpu=False)


def test_gpu_on_activates_runtime_only():
    out = render_cloud_init("JIT", gpu=True).decode()
    # The driver and toolkit are baked — no install half survives.
    assert "dnf -y install" not in out
    assert "almalinux-release-nvidia-driver" not in out
    assert "nvidia-container-toolkit" not in out
    # The hardware-dependent half runs every boot (no GPU exists at build time).
    assert "modprobe nvidia" in out
    assert "nvidia-ctk cdi generate" in out


def test_gpu_activates_before_the_egress_lockdown():
    # Generating the CDI spec reaches out; it must precede the firewall.
    out = render_cloud_init("JIT", gpu=True).decode()
    assert out.index("modprobe nvidia") < out.index(_NFT_APPLY)


# ── shape of the module ───────────────────────────────────────────────────────


def test_runner_config_defaults_gpu_off():
    cfg = RunnerConfig(version="2.334.0", labels=["self-hosted"], runner_group="husk")
    assert cfg.gpu is False


def test_there_is_exactly_one_template():
    # The stock/prebaked split is gone. If a second template ever reappears, the
    # duplicated nft ruleset — and the drift guard it needed — comes back with it.
    import husk.cloudinit as m

    assert [n for n in vars(m) if n.endswith("CLOUD_INIT")] == ["RUNNER_CLOUD_INIT"]
    assert "@@RUNNER_URL@@" not in RUNNER_CLOUD_INIT
