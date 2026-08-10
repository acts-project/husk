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

Two ways to get a golden onto the host:

  HUSK_SMOKE_IMAGE  a qcow2 filename you already put in the pool dir (default).
  HUSK_SMOKE_REF    an OCI ref, e.g. ghcr.io/acts-project/husk-base:v8. Stages it
                    through the REAL delivery path — oras pull to the controller
                    cache, scp to the host, digest-named — before the slot test.
                    Takes precedence over HUSK_SMOKE_IMAGE.

Prefer HUSK_SMOKE_REF on a host that has never served husk: it needs nothing
pre-placed, and it exercises image delivery (`image_sync`, `_ensure_on_host`,
`_gc_goldens`) which the IMAGE path skips entirely.

Env overrides: HUSK_SMOKE_HOST (ssh alias / user@host), HUSK_SMOKE_URI,
HUSK_SMOKE_POOL, HUSK_SMOKE_IMAGE, HUSK_SMOKE_REF, HUSK_SMOKE_SETTLE (seconds to
watch RUNNING), HUSK_SMOKE_STAGE_TIMEOUT (seconds to allow for the pull+scp).
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
REF = os.environ.get("HUSK_SMOKE_REF", "")
SETTLE = int(os.environ.get("HUSK_SMOKE_SETTLE", "25"))
# A cold pull is ~2 GB from ghcr plus an scp of the same to the host; the backend's
# own scp cap is an hour (_PUSH_TIMEOUT_S), so don't be stingier than a few minutes.
STAGE_TIMEOUT = int(os.environ.get("HUSK_SMOKE_STAGE_TIMEOUT", "900"))

# Minimal valid cloud-init: a NoCloud seed cloud-init will actually consume. We
# can't read the guest, so we don't assert on its effect — booting to RUNNING and
# staying up is the backend-level signal.
USER_DATA = b"#cloud-config\nhostname: husk-smoke\n"


def banner(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def confirm(be: LibvirtBackend) -> None:
    """Assert each host ADOPTED a golden, not merely that the op reported done.

    capacity() gates on `_host_ready`, which in OCI mode means `image_digest is not
    None` — a host that staged but never adopted contributes ZERO units, so the
    symptom lands much later as `Capacity(can_create=False, free_instances=0)` with
    nothing pointing at the image. Fail here instead, where the cause is legible.
    """
    for hname, h in be._hosts.items():  # noqa: SLF001 — smoke script, internals are fair game
        if h.image_digest is None:
            raise SystemExit(
                f"host {hname}: staging finished but no golden was adopted, so the "
                f"host reports no capacity. Check the pool dir on the host for a "
                f"husk-golden-*.qcow2 and the free space beside it."
            )
        print(f"    {hname} serving {h.image} ({h.image_digest[:19]})")


def stage(be: LibvirtBackend, cfg: BackendConfig) -> None:
    """Drive image staging to completion, the way the controller's tick does.

    sync_images is deliberately NON-blocking: it hands the multi-GB pull+scp to a
    background worker and returns, so a reconcile tick is never stalled by it. A
    one-shot script therefore has to poll it exactly as the controller would, which
    is also why this is a fair test of the real path rather than a shortcut around
    it.
    """
    banner(f"stage golden from {cfg.image_ref} (oras pull -> controller cache -> scp)")
    deadline = time.time() + STAGE_TIMEOUT
    last = None
    while time.time() < deadline:
        be.sync_images(cfg)
        # ADOPTION is the postcondition, not the op board's "done".
        #
        # sync_images adopts only on a call where submit() observes an already-DONE
        # op; a completion landing after this call's submit is adopted on the NEXT
        # call. So returning on a `staging_ops()` reading — taken after that same
        # submit — can exit in exactly that gap, leaving image_digest unset. The
        # host then contributes zero units and capacity() reports free=0 with
        # nothing pointing at the image. Poll the thing we actually need instead.
        if all(h.image_digest for h in be._hosts.values()):  # noqa: SLF001
            confirm(be)
            return
        views = be.staging_ops()
        if views:
            v = views[0]
            if v.state == "failed":
                raise SystemExit(
                    f"staging failed after {v.attempts} attempt(s): {v.error}"
                )
            note = f"{v.state}: {v.progress or ''}".strip()
            if note != last:  # only speak when something actually changed
                print(f"    {note}", flush=True)
                last = note
        time.sleep(5)
    raise SystemExit(
        f"staging did not finish within {STAGE_TIMEOUT}s — raise "
        f"HUSK_SMOKE_STAGE_TIMEOUT, or check the host's pool dir for free space"
    )


def main() -> int:
    cfg = BackendConfig(
        name="libvirt-smoke",
        type="libvirt",
        # Exactly one source wins: a ref goes through the real delivery path, a bare
        # name trusts a file already in the pool dir. Setting both would make
        # image_ref silently shadow image_name (see sync_images), so don't.
        image_name="" if REF else IMAGE,
        image_ref=REF,
        min_ready=1,
        max_total=1,
        hosts=(
            HostConfig(
                name="smoke-host",
                libvirt_uri=URI,
                ssh_target=HOST,
                storage_pool=POOL,
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
        if REF:
            stage(be, cfg)

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
