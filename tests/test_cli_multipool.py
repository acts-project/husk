"""huskctl status renders every pool from huskd's live HTTP endpoint, and
--pool / --json scope it. Exercises the full CLI command against a real server."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from conftest import make_runner, make_slot, serve_in_thread
from husk.cli import huskctl_app
from husk.slot import SlotState
from husk.snapshot import ControllerState

FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\\nnotreal\\n-----END RSA PRIVATE KEY-----\\n"
)

runner = CliRunner()


def _snap(backend: str, slot_name: str):
    return ControllerState.from_classified(
        generation=2,
        backend=backend,
        min_ready=1,
        max_total=2,
        desired_total=1,
        classified=[
            (
                make_slot(id=f"{backend}-1", name=slot_name, status="ACTIVE"),
                make_runner(name=f"{slot_name}-c0"),
                SlotState.IDLE,
            )
        ],
    )


def _snaps():
    return [
        _snap("openstack-cpu", "husk-openstack-cpu-1"),
        _snap("libvirt-gpu", "husk-libvirt-gpu-1"),
    ]


_CONFIG = """
[github]
app_id = 123456

[access]
allowed_orgs = ["acts-project"]
[controller]
http_addr = "{http_addr}"

[[pool]]
name = "openstack-cpu"
[pool.runner]
version = "2.334.0"
labels = ["self-hosted", "husk-cpu"]
[pool.backend]
type = "openstack"

[[pool]]
name = "libvirt-gpu"
[pool.runner]
version = "2.334.0"
labels = ["self-hosted", "gpu"]
[pool.backend]
type = "libvirt"
"""


def _config(tmp_path, monkeypatch, http_addr: str) -> str:
    monkeypatch.setenv(
        "HUSK_GITHUB__PRIVATE_KEY",
        FAKE_PEM,
    )
    cfg = tmp_path / "config.toml"
    cfg.write_text(_CONFIG.format(http_addr=http_addr))
    return str(cfg)


def test_build_forwards_shared_image_sync(tmp_path, monkeypatch):
    # huskd builds ONE ImageSync and hands the same instance to every pool's
    # backend, so the registry pull is single-flighted and the cache is shared.
    import husk.github as gh
    import husk.libvirt_backend as lb
    import husk.openstack_backend as ob
    from husk.cli import _build
    from husk.config import load_configs
    from husk.target import Target

    captured = []

    class Cap:
        def __init__(self, cfg, *, image_sync=None):
            captured.append(image_sync)

    monkeypatch.setattr(lb, "LibvirtBackend", Cap)
    monkeypatch.setattr(ob, "OpenStackBackend", Cap)
    monkeypatch.setattr(gh, "GitHubClient", lambda **kw: object())
    monkeypatch.setenv(
        "HUSK_GITHUB__PRIVATE_KEY",
        FAKE_PEM,
    )

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_CONFIG.format(http_addr="127.0.0.1:9100"))
    cfgs = load_configs(str(cfg_path))

    sentinel = object()
    for cfg in cfgs:
        _build(cfg, image_sync=sentinel, target=Target.org("acts-project"))
    assert captured == [sentinel, sentinel]  # one shared instance, both backends


def test_status_renders_all_pools(tmp_path, monkeypatch):
    snaps = _snaps()
    with serve_in_thread(lambda: snaps) as base:
        cfg = _config(tmp_path, monkeypatch, base.removeprefix("http://"))
        result = runner.invoke(huskctl_app, ["status", "-c", cfg])
    assert result.exit_code == 0
    assert "openstack-cpu" in result.stdout and "libvirt-gpu" in result.stdout


def test_status_pool_filter(tmp_path, monkeypatch):
    snaps = _snaps()
    with serve_in_thread(lambda: snaps) as base:
        cfg = _config(tmp_path, monkeypatch, base.removeprefix("http://"))
        result = runner.invoke(
            huskctl_app, ["status", "-c", cfg, "--pool", "libvirt-gpu"]
        )
    assert result.exit_code == 0
    assert "libvirt-gpu" in result.stdout and "openstack-cpu" not in result.stdout


def test_status_unknown_pool_errors(tmp_path, monkeypatch):
    snaps = _snaps()
    with serve_in_thread(lambda: snaps) as base:
        cfg = _config(tmp_path, monkeypatch, base.removeprefix("http://"))
        result = runner.invoke(huskctl_app, ["status", "-c", cfg, "--pool", "nope"])
    assert result.exit_code == 1


def test_status_json_is_a_list(tmp_path, monkeypatch):
    snaps = _snaps()
    with serve_in_thread(lambda: snaps) as base:
        cfg = _config(tmp_path, monkeypatch, base.removeprefix("http://"))
        result = runner.invoke(huskctl_app, ["status", "-c", cfg, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert {p["backend"] for p in payload} == {"openstack-cpu", "libvirt-gpu"}
