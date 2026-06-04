"""PAT resolution + env-override precedence in load_config."""

from __future__ import annotations

import pytest

from husk.config import load_config

_TOML = """
[github]
repo = "acts-project/husk-test"
{extra}
[runner]
version = "2.334.0"
labels = ["self-hosted"]
runner_group_id = 1
[backend]
name = "openstack-cern"
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


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("GH_TOKEN", "HUSK_GITHUB__PAT", "MY_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_pat_from_gh_token_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_local")
    assert load_config(_write(tmp_path)).github.token == "ghp_local"


def test_husk_env_overrides_gh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_local")
    monkeypatch.setenv("HUSK_GITHUB__PAT", "ghp_explicit")
    assert load_config(_write(tmp_path)).github.token == "ghp_explicit"


def test_custom_pat_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "ghp_custom")
    cfg = load_config(_write(tmp_path, extra='pat_env = "MY_TOKEN"'))
    assert cfg.github.token == "ghp_custom"


def test_pat_path_file_fallback(tmp_path):
    secret = tmp_path / "pat"
    secret.write_text("ghp_fromfile\n")
    cfg = load_config(_write(tmp_path, extra=f'pat_path = "{secret}"'))
    assert cfg.github.token == "ghp_fromfile"


def test_fail_closed_when_no_pat(tmp_path):
    with pytest.raises(RuntimeError, match="GitHub PAT not configured"):
        load_config(_write(tmp_path))
