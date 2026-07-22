"""Rewrite the cloud-init golden files. Run deliberately, then read the diff:

    uv run python tests/golden/regen.py

The diff is the point. These bytes are what boots a slot, and nothing else in the
test suite executes them, so a review of the diff is the only place an accidental
change to the guest's behaviour can be caught."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from golden_cases import CASES  # noqa: E402

from husk.cloudinit import render_cloud_init  # noqa: E402

out = pathlib.Path(__file__).parent
for name, kw in CASES.items():
    (out / f"{name}.yaml").write_bytes(render_cloud_init("JITBLOB", **kw))
print(f"wrote {len(CASES)} golden files to {out}")
