"""Label derivation, and the config rules that keep the derived set honest.

The whole point of deriving labels is that a pool cannot advertise a capability
it does not have, so most of these are about what the config REFUSES rather than
what it produces.
"""

from __future__ import annotations

import pytest

from husk.config import load_config, load_configs
from husk.labels import (
    check_extra_label,
    class_labels,
    derive_labels,
    underspecified,
)

FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nnotreal\n-----END RSA PRIVATE KEY-----\n"

_POOL = """
[github]
app_id = 1

[[pool]]
name = "{name}"
target = {{ org = "acts-project", group = "husk" }}
[pool.runner]
version = "2.334.0"
{runner}
[pool.backend]
cloud = "cern"
image_name = "img"
flavor_name = "m2.small"
network_name = "net"
"""


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("HUSK_GITHUB__PRIVATE_KEY", FAKE_PEM)


def _write(tmp_path, runner: str, name: str = "openstack-cpu") -> str:
    p = tmp_path / "c.toml"
    p.write_text(_POOL.format(name=name, runner=runner))
    return str(p)


# ------------------------------------------------------------------ derivation
def test_phase_one_pool_derives_the_standard_set(tmp_path):
    cfg = load_config(_write(tmp_path, 'arch = "x64"\nsize = "standard"'))
    assert cfg.runner.labels == [
        "self-hosted",
        "linux",
        "x64",
        "x86_64",
        "husk",
        "husk-pool-openstack-cpu",
        "husk-backend-openstack",
        "husk-size-standard",
    ]


def test_size_defaults_to_standard_rather_than_being_absent():
    """An unstated size must not mean "no size label" — that is the GPU pool's
    meaning, and a CPU pool with no class label is selectable by anything."""
    labels = derive_labels(pool_name="p", backend_type="openstack", size="standard")
    assert "husk-size-standard" in labels


def test_arm64_emits_both_spellings():
    labels = derive_labels(pool_name="p", backend_type="openstack", arch="arm64")
    assert "arm64" in labels and "aarch64" in labels
    assert "x64" not in labels


def test_gpu_pool_names_the_runtime_and_carries_no_size():
    labels = derive_labels(
        pool_name="libvirt-gpu",
        backend_type="libvirt",
        size=None,
        gpu_vendor="nvidia",
        gpu_model="A100",
    )
    assert {"gpu", "gpu-nvidia", "gpu-a100", "cuda"} <= set(labels)
    # The leak this prevents: a `husk-size-*` selector matching GPU hardware.
    assert not [x for x in labels if x.startswith("husk-size-")]


def test_amd_gets_rocm_not_cuda():
    labels = derive_labels(
        pool_name="p", backend_type="libvirt", size=None, gpu_vendor="amd"
    )
    assert "rocm" in labels and "cuda" not in labels


def test_cvmfs_label_follows_the_cvmfs_table(tmp_path):
    """The label exists because the mount does — not because someone typed it."""
    runner = 'arch = "x64"\nprebaked = true'
    toml = _POOL.format(name="lcg", runner=runner) + (
        '[pool.cvmfs]\nrepositories = ["sft.cern.ch"]\n'
        'http_proxy = "http://squid:3128"\n'
    )
    p = tmp_path / "cvmfs.toml"
    p.write_text(toml)
    assert "cvmfs" in load_config(str(p)).runner.labels
    # ...and is absent when the table is
    assert "cvmfs" not in load_config(_write(tmp_path, 'arch = "x64"')).runner.labels


def test_extra_labels_are_appended_and_deduped():
    labels = derive_labels(
        pool_name="p", backend_type="openstack", extra=["acts", "linux"]
    )
    assert labels[-1] == "acts"
    assert labels.count("linux") == 1


# ------------------------------------------------------------------ refusals
def test_husk_prefix_is_reserved_from_extras(tmp_path):
    with pytest.raises(Exception, match="reserved"):
        load_configs(_write(tmp_path, 'extra_labels = ["husk-size-large"]'))


def test_commas_rejected_because_github_splits_on_them():
    with pytest.raises(ValueError, match="no commas"):
        check_extra_label("a,b")


def test_size_on_a_gpu_pool_is_a_config_error(tmp_path):
    with pytest.raises(Exception, match="GPU pools carry no size label"):
        load_configs(_write(tmp_path, 'gpu = "nvidia"\nsize = "large"'))


def test_gpu_model_without_a_gpu_is_a_config_error(tmp_path):
    with pytest.raises(Exception, match="gpu_model needs gpu"):
        load_configs(_write(tmp_path, 'gpu_model = "a100"'))


# ------------------------------------------------------------------ consumers
def test_arch_drives_the_runner_tarball(tmp_path):
    """Stated once: an arm64 pool cannot advertise arm64 while installing an x64
    runner, because both come from the same field."""
    cfg = load_config(_write(tmp_path, 'arch = "arm64"'))
    assert "actions-runner-linux-arm64-2.334.0.tar.gz" in cfg.runner.url


def test_gpu_bool_is_derived_from_the_vendor(tmp_path):
    """cloud-init asks "is there a GPU"; the config states which one. One fact."""
    assert load_config(_write(tmp_path, 'arch = "x64"')).runner.gpu is False


def test_class_labels_identifies_underspecified_selectors():
    """`husk` never narrows anything, so it must not count as a class label —
    otherwise a plain build selecting on it looks pinned while being eligible for
    every pool that will ever exist."""
    assert class_labels(["self-hosted", "linux", "x64", "husk"]) == []
    assert class_labels(["self-hosted", "cuda"]) == ["cuda"]


@pytest.mark.parametrize(
    "selector, missing",
    [
        # Both dimensions pinned — a standard build, and a GPU job whose
        # accelerator label stands in for the size.
        (["self-hosted", "linux", "x64", "husk-size-standard"], []),
        (["self-hosted", "linux", "x64", "cuda"], []),
        (["self-hosted", "linux", "aarch64", "husk-size-large"], []),
        # Omitting arch means "any arch" — this matches an arm64 slot the day one
        # exists, which is the leak that motivated the dimension.
        (["self-hosted", "husk-size-standard"], ["arch"]),
        # Omitting the class means "any class" — GPU slots carry all of these.
        (["self-hosted", "linux", "x64"], ["class"]),
        # `husk` pins nothing at all.
        (["self-hosted", "husk"], ["arch", "class"]),
    ],
)
def test_underspecified_reports_every_unpinned_dimension(selector, missing):
    assert underspecified(selector) == missing


def test_pool_pinning_still_reads_as_underspecified():
    """A deliberate `husk-pool-*` pin is exact, but it is not how the dimensions
    are expressed — so a linter should flag it and the author should silence it,
    rather than the check quietly blessing a second way to be specific."""
    assert underspecified(["self-hosted", "husk-pool-openstack-cpu"]) == [
        "arch",
        "class",
    ]
