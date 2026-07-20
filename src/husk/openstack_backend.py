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

import dataclasses
import logging
import time
from datetime import datetime

import openstack

from husk.backend import ListSlotsError
from husk.cloudinit import b64
from husk.config import BackendConfig
from husk.image_sync import ImageSync
from husk.ops import DONE, OpStore, OpView
from husk.slot import Capacity, Slot

log = logging.getLogger("husk.openstack")

MANAGED_BY = "husk"  # metadata value tagging controller-owned slots
# Metadata key scoping a server to one pool. Several pools/targets share an
# OpenStack project, so "managed-by=husk" alone is not ownership.
POOL_KEY = "husk-pool"

# Glance image-name prefix for goldens huskd uploads from an OCI ref. Content-
# addressed by the qcow2 digest so a moved tag is a new image (never an in-place
# overwrite of one a running server boots from), and so GC can recognize "ours".
GLANCE_PREFIX = "husk-golden-"


@dataclasses.dataclass(frozen=True)
class _GlancePrepared:
    """An OCI golden staged into Glance, ready for the tick thread to adopt."""

    digest: str
    image_id: str


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


def _fixed_ip(server) -> str | None:
    """The guest's fixed IPv4, for metrics discovery (already on the detailed
    server object — no extra API call). `addresses` is {net_name: [port, ...]}."""
    addresses = getattr(server, "addresses", None) or {}
    for ports in addresses.values():
        for port in ports or []:
            if not isinstance(port, dict):
                continue
            if port.get("version") == 4 and port.get("OS-EXT-IPS:type") == "fixed":
                return port.get("addr")
    return None


# Per-request bound on every Nova/Glance call. Without it a hung CERN API call
# (e.g. a rebuild POST that stalls before eventually 500-ing) pins one of the
# worker threads the reconcile task offloads to (`asyncio.to_thread`) and stalls
# that pool indefinitely. This caps it; a slow call fails the one tick and retries
# next, instead of wedging the pool.
_API_TIMEOUT_S = 30


class OpenStackBackend:
    def __init__(
        self, cfg: BackendConfig, *, image_sync: ImageSync | None = None
    ) -> None:
        self.cfg = cfg
        # Scopes list_slots() to THIS pool's servers. Several pools (and, under the
        # App migration, several targets) share one OpenStack project, and a
        # controller that sees a sibling's servers reads them as unhealthy — no
        # runner matches their prefix — and rebuilds them out from under their
        # owner. See _owns().
        self._pool = cfg.name
        # Non-fatal per-slot issues (swallowed metadata-write failures) for the
        # dashboard — slot_id -> (epoch, message). See slot_warnings().
        self._warnings: dict[str, tuple[float, str]] = {}
        self.conn = openstack.connect(cloud=cfg.cloud, api_timeout=_API_TIMEOUT_S)
        flavor = self.conn.compute.find_flavor(cfg.flavor_name)
        if not flavor:
            raise RuntimeError(f"flavor {cfg.flavor_name!r} not found")
        network = self.conn.network.find_network(cfg.network_name)
        if not network:
            raise RuntimeError(f"network {cfg.network_name!r} not found")
        self.flavor_id = flavor.id
        self.network_id = network.id

        # Image source — two paths (mirrors the libvirt backend):
        #  * image_ref (OCI): huskd pulls the golden qcow2 and uploads it to Glance,
        #    rotating self.image_id; sync_images() does that before any create. The
        #    same artifact serves the libvirt hosts (see image_sync.py).
        #  * image_name (legacy): a Glance image already present, resolved once here.
        # Shared across pools (huskd builds it once) so the registry pull is
        # single-flighted; a fresh default keeps one-shots/tests self-contained.
        self._sync = image_sync or ImageSync()
        self._backend_ref = cfg.image_ref or ""
        self._synced_ref = ""  # the ref behind the current image_id (sync no-op guard)
        self._image_digest: str | None = None
        self.image_id: str | None = None
        # Heavy staging (oras pull + Glance upload) runs off the reconcile path
        # as a keyed op; the tick only adopts a ready result. A dedicated upload
        # connection keeps the worker's Glance calls off the tick's compute conn.
        self._ops = OpStore()
        self._image_conn = None
        if not self._backend_ref:
            if not cfg.image_name:
                raise RuntimeError(
                    "no image source: set [backend].image_ref (OCI) or image_name "
                    "(a Glance image name)"
                )
            image = self.conn.image.find_image(cfg.image_name)
            if not image:
                raise RuntimeError(f"image {cfg.image_name!r} not found")
            self.image_id = image.id

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
        image_id = _ref_id(getattr(server, "image", None))
        # Stale only in OCI mode (we rotate image_id on a ref change): a slot whose
        # image differs from the current golden drains via the recycle loop. In
        # legacy image_name mode there's nothing to roll onto → never stale.
        stale = bool(
            self._backend_ref
            and self.image_id
            and image_id
            and image_id != self.image_id
        )
        return Slot(
            id=server.id,
            name=server.name,
            status=server.status,
            task_state=_task_state(server),
            created_at=_epoch(getattr(server, "created_at", None)),
            flavor_id=_ref_id(getattr(server, "flavor", None)),
            image_id=image_id,
            cycle=cycle,
            provisioned_at=provisioned_at,
            fault=getattr(server, "fault", None),
            image_stale=stale,
            active_image=image_id,  # the Glance image this server booted from
            ip=_fixed_ip(server),
        )

    # --------------------------------------------------------------- backend
    def _owns(self, server) -> bool:
        """Whether this server belongs to THIS pool.

        Ownership is the `husk-pool` metadata tag, and only that: several pools
        share one OpenStack project, so `managed-by=husk` alone is not ownership.
        A husk server without the tag is not adopted — huskd stamps it at create,
        so an untagged one is foreign or hand-made, and silently adopting it would
        mean rebuilding something nobody asked us to manage."""
        meta = getattr(server, "metadata", None) or {}
        return meta.get("managed-by") == MANAGED_BY and meta.get(POOL_KEY) == self._pool

    def list_slots(self) -> list[Slot]:
        try:
            servers = list(self.conn.compute.servers(details=True))
        except Exception as e:  # auth expiry, 5xx, network — MUST raise, never []
            raise ListSlotsError(f"list servers failed: {e}") from e
        slots = [self._slot(s) for s in servers if self._owns(s)]
        # Drop warnings for slots that no longer exist (keeps the dict bounded).
        live = {s.id for s in slots}
        self._warnings = {k: v for k, v in self._warnings.items() if k in live}
        log.debug(
            "listed %d server(s), %d managed-by=%s",
            len(servers),
            len(slots),
            MANAGED_BY,
        )
        return slots

    def slot_warnings(self) -> dict[str, tuple[float, str]]:
        return dict(self._warnings)

    # ----------------------------------------------------------- image sync
    def sync_images(self, cfg: BackendConfig | None = None) -> None:
        """Adopt the configured OCI golden as the current image (`self.image_id`)
        once it's staged in Glance, then GC superseded husk goldens.

        Non-blocking: the controller's only coupling to image delivery, called
        once per tick before any create/rebuild. The slow work (oras pull + the
        multi-GB Glance upload) runs on a background thread (`_prepare_image`); a
        tick just adopts the result when ready and otherwise keeps using the
        current image, so a transfer never stalls the reconcile loop. Cheap when
        nothing changed (short-circuits a ref already adopted). Legacy image_name
        backends (no ref) are a no-op."""
        if cfg is not None and (cfg.image_ref or "") != self._backend_ref:
            log.info(
                "image ref changed: %r -> %r (staging in the background)",
                self._backend_ref,
                cfg.image_ref,
            )
            self._backend_ref = cfg.image_ref or ""
        ref = self._backend_ref
        if not ref:
            return  # legacy image_name mode — nothing to pull/upload
        if self._synced_ref == ref and self.image_id:
            return  # already current
        key = f"glance:{ref}"
        view = self._ops.submit(
            key, "glance-upload", lambda report: self._prepare_image(ref, report)
        )
        if view.state != DONE:
            return  # still staging (or failed + backing off) — keep current image
        prepared = self._ops.result(key)
        self.image_id = prepared.image_id
        self._image_digest = prepared.digest
        self._synced_ref = ref
        log.info("adopted Glance golden %s for %s", self.image_id, ref)
        self._gc_glance()

    def staging_ops(self) -> list[OpView]:
        """In-flight / recent image-staging ops, for the status board."""
        return self._ops.views()

    def _prepare_image(self, ref: str, report) -> _GlancePrepared:
        """Heavy staging (op worker): pull the OCI golden to the controller cache
        and ensure it's uploaded to Glance. Returns what the tick adopts."""
        report("pulling golden from registry")
        resolved = self._sync.resolve(ref, report=report)
        image_id = self._ensure_in_glance(self._upload_conn(), resolved, report)
        return _GlancePrepared(digest=resolved.digest, image_id=image_id)

    def _upload_conn(self):
        """A dedicated OpenStack connection for the background uploader, so its
        Glance calls never share a connection with the tick's compute calls."""
        if self._image_conn is None:
            self._image_conn = openstack.connect(
                cloud=self.cfg.cloud, api_timeout=_API_TIMEOUT_S
            )
        return self._image_conn

    def _ensure_in_glance(self, conn, resolved, report=None) -> str:
        """Return the Glance image id for a resolved OCI golden, uploading it once
        if absent. Idempotent + content-addressed: the image is named
        `husk-golden-<digest12>`, so a present image of the same digest is reused
        and a moved tag uploads a new image rather than mutating one in use.

        The "uploading" progress is reported only on an actual upload — a reused
        image is a no-op, so a restart against an already-staged golden shows no
        spurious upload activity."""
        name = f"{GLANCE_PREFIX}{resolved.short}"
        existing = conn.image.find_image(name)
        if existing is not None:
            log.debug("Glance already has golden %s (%s)", name, existing.id)
            return existing.id
        if report is not None:
            report("uploading golden to Glance")
        log.info("uploading golden %s to Glance (qcow2, may take a while)", name)
        # create_image streams the file and waits for the image to go active.
        # disk/container formats match a bare qcow2; the digest is stamped as an
        # image property so GC and audits can map an image back to its content.
        image = conn.create_image(
            name=name,
            filename=resolved.local_path,
            disk_format="qcow2",
            container_format="bare",
            visibility="private",
            wait=True,
            husk_image_digest=resolved.digest,
        )
        return image.id

    def _gc_glance(self) -> None:
        """Delete husk goldens in Glance that no live slot boots from and that are
        not the current image. Best-effort and conservative (only touches
        `husk-golden-*` images we uploaded); Glance refuses to delete an in-use
        image anyway, so a running server is never at risk. A GC failure is
        logged and ignored."""
        try:
            live = {s.image_id for s in self.list_slots() if s.image_id}
        except ListSlotsError:
            return  # can't enumerate slots → don't risk deleting a referenced image
        # Deliberately NOT `except Exception`: ListSlotsError is the contract for
        # "couldn't enumerate", and bailing quietly on it is right. Anything else is
        # a bug in our own listing code, and swallowing it here would disable GC
        # silently and permanently. Let it out — the controller's sync_images call
        # logs it with a traceback and carries on, so a bug is visible without
        # costing a tick.
        keep = set(live)
        if self.image_id:
            keep.add(self.image_id)
        try:
            for img in self.conn.image.images():
                name = getattr(img, "name", "") or ""
                if name.startswith(GLANCE_PREFIX) and img.id not in keep:
                    log.info("GC superseded Glance golden %s (%s)", name, img.id)
                    self.conn.image.delete_image(img.id, ignore_missing=True)
        except Exception:
            log.warning("Glance golden GC failed; leaving images", exc_info=True)

    def image_ready(self, slot: Slot) -> bool:
        """OCI mode: ready once the golden has been adopted into Glance
        (`image_id` set). Legacy image_name mode resolves the id at init, so it is
        always ready. Mirrors the `capacity()` gate, for rebuilds."""
        return bool(self.image_id) or not self._backend_ref

    def _require_image(self) -> str:
        """The current Glance image id, or a clear error if an OCI sync hasn't
        landed yet (the controller calls sync_images before any create/rebuild, so
        this only trips on a sync failure — e.g. registry unreachable)."""
        if not self.image_id:
            raise RuntimeError(
                f"no current image for ref {self._backend_ref!r}: image sync has not "
                "completed (registry/Glance unreachable?)"
            )
        return self.image_id

    def create_slot(self, *, user_data: bytes, name: str, cycle: int) -> Slot:
        image_id = self._require_image()
        server = self.conn.compute.create_server(
            name=name,
            image_id=image_id,
            flavor_id=self.flavor_id,
            networks=[{"uuid": self.network_id}],
            key_name=self.cfg.keypair,
            user_data=b64(user_data),
            metadata={
                "managed-by": MANAGED_BY,
                POOL_KEY: self._pool,
                "husk-cycle": str(cycle),
                "husk-provisioned-at": f"{time.time():.0f}",
            },
        )
        log.debug("create_server %s -> id=%s status=%s", name, server.id, server.status)
        return self._slot(server)

    def rebuild_slot(self, slot: Slot, *, user_data: bytes, cycle: int) -> None:
        # Minimal CERN-compatible rebuild: NO name field, pinned microversion.
        # Rebuild adopts the CURRENT image (sync_images may have rotated it onto a
        # new golden) — this is what clears a slot's stale flag once it drains.
        image_id = self._require_image()
        log.debug(
            "POST rebuild %s image=%s microversion=%s cycle=%d",
            slot.id,
            image_id,
            self.cfg.rebuild_microversion,
            cycle,
        )
        resp = self.conn.compute.post(
            f"/servers/{slot.id}/action",
            json={"rebuild": {"imageRef": image_id, "user_data": b64(user_data)}},
            headers={
                "OpenStack-API-Version": f"compute {self.cfg.rebuild_microversion}"
            },
            timeout=_API_TIMEOUT_S,
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(
                f"rebuild rejected: HTTP {resp.status_code}: {resp.text[:300]}"
            )
        # Update durable state so a restart recovers cycle + provision clock. A
        # metadata-write failure (e.g. CERN's CernLanDB 500) must NOT fail the
        # rebuild — the rebuild action above already succeeded — but it's no longer
        # swallowed silently: it's recorded as a per-slot warning the dashboard shows.
        try:
            self.conn.compute.set_server_metadata(
                slot.id,
                **{
                    "husk-cycle": str(cycle),
                    "husk-provisioned-at": f"{time.time():.0f}",
                },
            )
            self._warnings.pop(slot.id, None)
        except Exception as e:
            log.warning("could not update husk metadata on %s", slot.id, exc_info=True)
            self._warnings[slot.id] = (time.time(), f"metadata write failed: {e}")

    def mark_active(self, slot: Slot) -> None:
        try:
            self.conn.compute.set_server_metadata(
                slot.id, **{"husk-provisioned-at": f"{time.time():.0f}"}
            )
            log.debug("marked %s ACTIVE; reset husk-provisioned-at", slot.id)
            self._warnings.pop(slot.id, None)
        except Exception as e:
            log.warning("could not mark %s active (metadata)", slot.id, exc_info=True)
            self._warnings[slot.id] = (time.time(), f"metadata write failed: {e}")

    def start_slot(self, slot: Slot) -> None:
        self._action(slot, {"os-start": None})

    def stop_slot(self, slot: Slot) -> None:
        self._action(slot, {"os-stop": None})

    def _action(self, slot: Slot, body: dict) -> None:
        action = list(body)[0]
        log.debug("POST action %s on %s", action, slot.id)
        resp = self.conn.compute.post(
            f"/servers/{slot.id}/action", json=body, timeout=_API_TIMEOUT_S
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(
                f"action {action} on {slot.id} rejected: "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )

    def destroy_slot(self, slot: Slot, *, reason: str) -> None:
        log.info("destroying slot %s (reason=%s)", slot.id, reason)
        self.conn.compute.delete_server(slot.id, ignore_missing=True)

    def console_output(self, slot: Slot, *, lines: int | None = None) -> str | None:
        try:
            out = self.conn.compute.get_server_console_output(slot.id, length=lines)
        except Exception:
            log.warning("console output for %s unavailable", slot.id, exc_info=True)
            return None
        text = (
            out.get("output") if isinstance(out, dict) else getattr(out, "output", None)
        )
        return text or None

    def capacity(self) -> Capacity:
        # OCI mode: while the golden is still staging (oras pull + Glance upload),
        # there is no image to boot — report zero so the controller doesn't attempt
        # a create (and mint a wasted JIT runner) until it lands. Legacy image_name
        # mode resolves the id at init, so this never trips there.
        if self._backend_ref and not self.image_id:
            log.debug("image not staged yet; reporting zero capacity")
            return Capacity(can_create=False, free_instances=0)
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
