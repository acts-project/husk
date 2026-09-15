#!/usr/bin/env python3
"""Upload only daemon build inputs and return this OpenShift build's digest."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time

INPUTS = ("Dockerfile", "pyproject.toml", "uv.lock", "src")


def stage_source(root: Path, dest: Path, source: str) -> None:
    if source == "HEAD":
        archive = subprocess.check_output(
            ["git", "archive", "HEAD", "--", *INPUTS], cwd=root
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar:
                if member.isdir():
                    continue
                if not member.isfile() or ".." in Path(member.name).parts:
                    raise ValueError(f"Unsupported build input: {member.name}")
                target = dest / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
    else:
        names = (
            subprocess.check_output(
                [
                    "git",
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    *INPUTS,
                ],
                cwd=root,
            )
            .decode()
            .split("\0")
        )
        for name in sorted(set(filter(None, names))):
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Unsupported build input: {name}")
            if not path.exists():  # tracked file deleted in the working tree
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    for name in INPUTS[:3]:
        if not (dest / name).is_file():
            raise ValueError(f"Missing build input: {name}")


def image_reference(build: dict) -> str:
    status = build["status"]
    if status["phase"] != "Complete":
        raise ValueError(f"Build did not complete: {status['phase']}")
    digest = status.get("output", {}).get("to", {}).get("imageDigest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Build has no valid output digest")
    reference = status.get("outputDockerImageReference", "")
    if "/" not in reference:
        raise ValueError("Build has no output repository")
    repository = reference.split("@", 1)[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    return f"{repository}@{digest}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("HEAD", "working-tree"), default="HEAD")
    parser.add_argument("--namespace", default="husk")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    oc = ["oc", "-n", args.namespace]
    if args.source == "HEAD":
        print("Building committed HEAD; uncommitted changes are excluded.", flush=True)
    else:
        print(
            "Building working-tree daemon inputs, including uncommitted changes.",
            flush=True,
        )
    with tempfile.TemporaryDirectory(prefix="husk-build-") as tmp:
        stage_source(root, Path(tmp), args.source)
        subprocess.run([*oc, "apply", "-f", str(root / "k8s/build.yaml")], check=True)
        build = subprocess.check_output(
            [*oc, "start-build", "huskd", f"--from-dir={tmp}", "-o", "name"],
            text=True,
        ).strip()
    print(
        f"Build: {build}\nFollow logs: oc -n {args.namespace} logs -f {build}",
        flush=True,
    )
    deadline = time.monotonic() + 1500
    last_phase = None
    while time.monotonic() < deadline:
        data = json.loads(subprocess.check_output([*oc, "get", build, "-o", "json"]))
        phase = data["status"]["phase"]
        if phase != last_phase:
            print(f"Build phase: {phase}", flush=True)
            last_phase = phase
        if phase in ("Complete", "Failed", "Error", "Cancelled"):
            if phase != "Complete":
                subprocess.run([*oc, "logs", build, "--tail=80"], check=False)
            reference = image_reference(data)
            if args.output:
                args.output.write_text(reference + "\n")
            print(reference, flush=True)
            return
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for {build}; inspect it with oc")


if __name__ == "__main__":
    main()
