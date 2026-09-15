"""Check exactly what leaves the laptop and which image a build selects."""

import importlib.util
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    "oc_build", Path(__file__).parents[1] / "scripts/oc-build.py"
)
oc_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc_build)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name in ("Dockerfile", "pyproject.toml", "uv.lock", "src/husk/app.py"):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("committed")
    (root / ".env").write_text("secret")
    (root / "config.toml").write_text("private config")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=root,
        check=True,
    )
    (root / "src/husk/app.py").write_text("edited")
    (root / "src/husk/new.py").write_text("new")
    return root


@pytest.mark.parametrize(
    "source,expected", [("HEAD", "committed"), ("working-tree", "edited")]
)
def test_upload_only_build_inputs(repo, tmp_path, source, expected):
    dest = tmp_path / "staged"
    oc_build.stage_source(repo, dest, source)
    assert (dest / "src/husk/app.py").read_text() == expected
    assert (dest / "src/husk/new.py").exists() == (source == "working-tree")
    assert not (dest / ".env").exists()
    assert not (dest / "config.toml").exists()
    assert not (dest / ".git").exists()


def test_working_tree_rejects_symlink(repo, tmp_path):
    (repo / "src/husk/key.py").symlink_to(repo / ".env")
    with pytest.raises(ValueError, match="Unsupported build input"):
        oc_build.stage_source(repo, tmp_path / "staged", "working-tree")


def test_digest_comes_from_the_specific_completed_build():
    digest = "sha256:" + "a" * 64
    build = {
        "status": {
            "phase": "Complete",
            "outputDockerImageReference": "registry:5000/husk/huskd:latest",
            "output": {"to": {"imageDigest": digest}},
        }
    }
    assert oc_build.image_reference(build) == f"registry:5000/husk/huskd@{digest}"
    build["status"]["phase"] = "Failed"
    with pytest.raises(ValueError, match="did not complete"):
        oc_build.image_reference(build)
    build["status"]["phase"] = "Complete"
    build["status"]["output"] = {}
    with pytest.raises(ValueError, match="no valid output digest"):
        oc_build.image_reference(build)
