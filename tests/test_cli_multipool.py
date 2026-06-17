"""huskctl status renders every pool from the published state-file list, and
--pool / --json scope it. Exercises the full CLI command (file source)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from conftest import make_runner, make_slot
from husk.cli import huskctl_app
from husk.slot import SlotState
from husk.snapshot import ControllerState, write_states

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


_CONFIG = """
[github]
repo = "acts-project/husk-test"
[controller]
http_addr = ""
state_path = "{state}"

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


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    state = tmp_path / "state.json"
    write_states(
        str(state),
        [
            _snap("openstack-cpu", "husk-openstack-cpu-1"),
            _snap("libvirt-gpu", "husk-libvirt-gpu-1"),
        ],
    )
    cfg = tmp_path / "config.toml"
    cfg.write_text(_CONFIG.format(state=state))
    return str(cfg)


def test_status_renders_all_pools(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    result = runner.invoke(huskctl_app, ["status", "-c", cfg])
    assert result.exit_code == 0
    assert "openstack-cpu" in result.stdout and "libvirt-gpu" in result.stdout


def test_status_pool_filter(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    result = runner.invoke(huskctl_app, ["status", "-c", cfg, "--pool", "libvirt-gpu"])
    assert result.exit_code == 0
    assert "libvirt-gpu" in result.stdout and "openstack-cpu" not in result.stdout


def test_status_unknown_pool_errors(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    result = runner.invoke(huskctl_app, ["status", "-c", cfg, "--pool", "nope"])
    assert result.exit_code == 1


def test_status_json_is_a_list(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    result = runner.invoke(huskctl_app, ["status", "-c", cfg, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert {p["backend"] for p in payload} == {"openstack-cpu", "libvirt-gpu"}
