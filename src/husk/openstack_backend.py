"""OpenStack (Nova) backend — slot-based, lifted from phase3-recycle.py.

CERN-specific bits preserved verbatim and deliberately *not* "improved":
* rebuild is a minimal direct POST to /servers/{id}/action with NO `name` field
  (CERN Nova rejects it: "Hostname cannot be updated"), pinned to microversion
  2.79;
* slots are tagged `managed-by=husk`; `list_slots` filters on it so the
  controller never touches a VM it didn't create.

All mutation methods are non-blocking: they issue the action and return. The
controller drives the multi-step rebuild→start across ticks.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import openstack

from husk.backend import ListSlotsError
from husk.cloudinit import b64
from husk.config import BackendConfig
from husk.slot import Capacity, Slot

log = logging.getLogger("husk.openstack")

MANAGED_BY = "husk"  # metadata value tagging controller-owned slots


def _task_state(server) -> str | None:
    return getattr(server, "task_state", None) or server.to_dict().get(
        "OS-EXT-STS:task_state"
    )


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _ref_id(value) -> str:
    """flavor/image may be a dict ({'id': ...}) or a bare id depending on SDK/cloud."""
    if isinstance(value, dict):
        return value.get("id") or value.get("original_name") or ""
    return str(value or "")


class OpenStackBackend:
    def __init__(self, cfg: BackendConfig) -> None:
        self.cfg = cfg
        self.conn = openstack.connect(cloud=cfg.cloud)
        image = self.conn.image.find_image(cfg.image_name)
        if not image:
            raise RuntimeError(f"image {cfg.image_name!r} not found")
        flavor = self.conn.compute.find_flavor(cfg.flavor_name)
        if not flavor:
            raise RuntimeError(f"flavor {cfg.flavor_name!r} not found")
        network = self.conn.network.find_network(cfg.network_name)
        if not network:
            raise RuntimeError(f"network {cfg.network_name!r} not found")
        self.image_id = image.id
        self.flavor_id = flavor.id
        self.network_id = network.id

    # ----------------------------------------------------------------- build
    def _slot(self, server) -> Slot:
        md = dict(getattr(server, "metadata", None) or {})
        cycle = 0
        try:
            cycle = int(md.get("husk-cycle", 0))
        except (TypeError, ValueError):
            cycle = 0
        provisioned_at = None
        if md.get("husk-provisioned-at"):
            try:
                provisioned_at = float(md["husk-provisioned-at"])
            except (TypeError, ValueError):
                provisioned_at = None
        return Slot(
            id=server.id,
            name=server.name,
            status=server.status,
            task_state=_task_state(server),
            created_at=_epoch(getattr(server, "created_at", None)),
            flavor_id=_ref_id(getattr(server, "flavor", None)),
            image_id=_ref_id(getattr(server, "image", None)),
            cycle=cycle,
            provisioned_at=provisioned_at,
            fault=getattr(server, "fault", None),
        )

    # --------------------------------------------------------------- backend
    def list_slots(self) -> list[Slot]:
        try:
            servers = list(self.conn.compute.servers(details=True))
        except Exception as e:  # auth expiry, 5xx, network — MUST raise, never []
            raise ListSlotsError(f"list servers failed: {e}") from e
        slots = [
            self._slot(s)
            for s in servers
            if (getattr(s, "metadata", None) or {}).get("managed-by") == MANAGED_BY
        ]
        log.debug(
            "listed %d server(s), %d managed-by=%s",
            len(servers),
            len(slots),
            MANAGED_BY,
        )
        return slots

    def create_slot(self, *, user_data: bytes, name: str, cycle: int) -> Slot:
        server = self.conn.compute.create_server(
            name=name,
            image_id=self.image_id,
            flavor_id=self.flavor_id,
            networks=[{"uuid": self.network_id}],
            key_name=self.cfg.keypair,
            user_data=b64(user_data),
            metadata={
                "managed-by": MANAGED_BY,
                "husk-cycle": str(cycle),
                "husk-provisioned-at": f"{time.time():.0f}",
            },
        )
        log.debug("create_server %s -> id=%s status=%s", name, server.id, server.status)
        return self._slot(server)

    def rebuild_slot(self, slot: Slot, *, user_data: bytes, cycle: int) -> None:
        # Minimal CERN-compatible rebuild: NO name field, pinned microversion.
        log.debug(
            "POST rebuild %s image=%s microversion=%s cycle=%d",
            slot.id,
            self.image_id,
            self.cfg.rebuild_microversion,
            cycle,
        )
        resp = self.conn.compute.post(
            f"/servers/{slot.id}/action",
            json={"rebuild": {"imageRef": self.image_id, "user_data": b64(user_data)}},
            headers={
                "OpenStack-API-Version": f"compute {self.cfg.rebuild_microversion}"
            },
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(
                f"rebuild rejected: HTTP {resp.status_code}: {resp.text[:300]}"
            )
        # Update durable state so a restart recovers cycle + provision clock.
        try:
            self.conn.compute.set_server_metadata(
                slot.id,
                **{
                    "husk-cycle": str(cycle),
                    "husk-provisioned-at": f"{time.time():.0f}",
                },
            )
        except Exception:
            log.warning("could not update husk metadata on %s", slot.id, exc_info=True)

    def mark_active(self, slot: Slot) -> None:
        try:
            self.conn.compute.set_server_metadata(
                slot.id, **{"husk-provisioned-at": f"{time.time():.0f}"}
            )
            log.debug("marked %s ACTIVE; reset husk-provisioned-at", slot.id)
        except Exception:
            log.warning("could not mark %s active (metadata)", slot.id, exc_info=True)

    def start_slot(self, slot: Slot) -> None:
        self._action(slot, {"os-start": None})

    def stop_slot(self, slot: Slot) -> None:
        self._action(slot, {"os-stop": None})

    def _action(self, slot: Slot, body: dict) -> None:
        action = list(body)[0]
        log.debug("POST action %s on %s", action, slot.id)
        resp = self.conn.compute.post(f"/servers/{slot.id}/action", json=body)
        if resp.status_code not in (200, 202):
            raise RuntimeError(
                f"action {action} on {slot.id} rejected: "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )

    def destroy_slot(self, slot: Slot, *, reason: str) -> None:
        log.info("destroying slot %s (reason=%s)", slot.id, reason)
        self.conn.compute.delete_server(slot.id, ignore_missing=True)

    def capacity(self) -> Capacity:
        try:
            limits = self.conn.compute.get_limits().absolute
            max_instances = getattr(limits, "instances", None) or getattr(
                limits, "max_total_instances", 0
            )
            used = getattr(limits, "total_instances_used", 0) or getattr(
                limits, "instances_used", 0
            )
            free = max(0, int(max_instances) - int(used))
            log.debug(
                "compute limits: instances used=%s max=%s -> free=%d",
                used,
                max_instances,
                free,
            )
            return Capacity(can_create=free > 0, free_instances=free)
        except Exception:
            # Best-effort second guard; max_total is the primary clamp. If we
            # can't read limits, defer to max_total rather than blocking growth.
            log.warning(
                "could not read compute limits; deferring to max_total", exc_info=True
            )
            return Capacity(can_create=True, free_instances=10**6)
