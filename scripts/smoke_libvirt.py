#!/usr/bin/env python
"""Backend-only smoke test for LibvirtBackend (no GitHub, no cloud-init runner).

Exercises the real code against a real hypervisor over qemu+ssh:
  capacity → list_slots(empty) → create_slot → (boot to RUNNING) →
  metadata round-trip → capacity(full) → destroy_slot → cleanup verified.

It boots a stock AlmaLinux 10 cloud image as a CPU slot (no <hostdev>), so it
needs no NVIDIA driver. The guest is never SSHed — we assert health purely via
the libvirt domain state, exactly as the controller's classifier does.

Run:
    uv run --extra libvirt python scripts/smoke_libvirt.py

Env overrides: HUSK_SMOKE_HOST (ssh alias / user@host), HUSK_SMOKE_URI,
HUSK_SMOKE_POOL, HUSK_SMOKE_IMAGE, HUSK_SMOKE_SETTLE (seconds to watch RUNNING).
"""

from __future__ import annotations

import os
import sys
import time

from husk.config import BackendConfig, HostConfig
from husk.libvirt_backend import LibvirtBackend

HOST = os.environ.get("HUSK_SMOKE_HOST", "lenovo-gpu-acts")
URI = os.environ.get("HUSK_SMOKE_URI", f"qemu+ssh://{HOST}/system")
POOL = os.environ.get("HUSK_SMOKE_POOL", "husk")
IMAGE = os.environ.get("HUSK_SMOKE_IMAGE", "husk-cpu-base.qcow2")
SETTLE = int(os.environ.get("HUSK_SMOKE_SETTLE", "25"))

# Minimal valid cloud-init: a NoCloud seed cloud-init will actually consume. We
# can't read the guest, so we don't assert on its effect — booting to RUNNING and
# staying up is the backend-level signal.
USER_DATA = b"#cloud-config\nhostname: husk-smoke\n"


def banner(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def main() -> int:
    cfg = BackendConfig(
        name="libvirt-smoke",
        type="libvirt",
        image_name=IMAGE,
        min_ready=1,
        max_total=1,
        hosts=(
            HostConfig(
                name="smoke-host",
                libvirt_uri=URI,
                ssh_target=HOST,
                pool=POOL,
                network="default",
                memory_mb=2048,
                vcpus=2,
                max_slots=1,  # CPU host → one plain VM, no GPU passthrough
            ),
        ),
    )
    be = LibvirtBackend(cfg)
    name = f"husk-smoke-{int(time.time())}"
    slot = None
    try:
        banner("capacity (expect free=1, can_create=True)")
        cap = be.capacity()
        print(f"    {cap}")
        assert cap.can_create and cap.free_instances == 1, cap

        banner("list_slots (expect [] — fedora-gpu has no husk metadata)")
        slots = be.list_slots()
        print(f"    {len(slots)} managed slot(s): {[s.name for s in slots]}")
        assert slots == [], "pool not clean; a previous smoke slot may be lingering"

        banner(f"create_slot name={name}")
        slot = be.create_slot(user_data=USER_DATA, name=name, cycle=0)
        print(
            f"    -> id={slot.id} status={slot.status} cycle={slot.cycle} "
            f"unit(from meta via list)…"
        )
        assert slot.status == "ACTIVE", (
            f"expected ACTIVE right after create, got {slot.status}"
        )

        banner("list_slots (expect exactly our slot, ACTIVE, metadata round-trips)")
        slots = be.list_slots()
        mine = [s for s in slots if s.name == name]
        assert len(mine) == 1, f"expected 1 managed slot, got {len(slots)}"
        s = mine[0]
        print(
            f"    id={s.id} status={s.status} cycle={s.cycle} "
            f"provisioned_at={s.provisioned_at!r} created_at={s.created_at!r}"
        )
        assert s.status == "ACTIVE"
        assert s.cycle == 0
        assert s.provisioned_at is not None, (
            "husk-provisioned-at metadata did not round-trip"
        )

        banner("capacity (expect free=0, can_create=False)")
        cap = be.capacity()
        print(f"    {cap}")
        assert not cap.can_create and cap.free_instances == 0, cap

        banner(f"watch it stay RUNNING for {SETTLE}s (a crash would flip to ERROR)")
        deadline = time.time() + SETTLE
        while time.time() < deadline:
            st = be.list_slots()[0].status
            print(f"    status={st}", flush=True)
            assert st != "ERROR", "domain crashed (ERROR) — check the host console log"
            time.sleep(5)

        print("\nALL CHECKS PASSED ✅")
        return 0
    finally:
        if slot is not None:
            banner(f"destroy_slot {slot.id} (cleanup)")
            be.destroy_slot(slot, reason="smoke-test")
            left = [s for s in be.list_slots() if s.name == name]
            print(f"    managed slots named {name} after destroy: {len(left)}")
            print(f"    capacity now: {be.capacity()}")


if __name__ == "__main__":
    sys.exit(main())
