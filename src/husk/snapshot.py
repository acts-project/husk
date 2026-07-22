"""Published controller state — the metrics / status-board data seam.

The reconcile loop builds one `ControllerState` per tick and swaps it onto the
Controller atomically; the HTTP/SSE endpoints (`husk.web`) render this same
object straight from memory — no controller change required, no file IPC.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from husk.ops import OpView
from husk.slot import SlotState


def _round1(v: float | None) -> float | None:
    return round(v, 1) if v is not None else None


@dataclass(frozen=True)
class SlotView:
    """A flat, serializable summary of one classified slot."""

    id: str
    name: str
    state: str  # SlotState.value (husk's classification)
    status: str  # backend/Nova status (ACTIVE/SHUTOFF/...)
    task_state: str | None  # in-flight provisioning task, if any
    runner: str | None  # matched GitHub runner name, if any
    runner_status: str | None  # "online" | "offline" | None
    busy: bool  # runner currently running a job
    cycle: int  # recycle cycle (durable husk-cycle)
    ip: str | None = None  # guest fixed IP (OpenStack) — metrics http_sd target
    host: str | None = None  # libvirt host name — metrics routes via its proxy
    image: str | None = None  # short id of the slot's ACTIVE image (digest / glance id)
    image_stale: bool = False  # active image differs from the pool's current target
    error: str | None = None  # last failed backend action (rebuild/start/…), if any
    error_epoch: float | None = None  # when that error was recorded (wall-clock)
    cloudinit_seconds: float | None = (
        None  # last ACTIVE→runner-online (cloud-init step)
    )
    recycle_seconds: float | None = None  # last issue→runner-online (whole bring-up)
    live_fraction: float | None = (
        None  # (busy+idle) / total tracked ("available to serve")
    )
    # Cumulative seconds in each classified state, since the controller first saw
    # this slot. `live_fraction` above is one ratio over these, precomputed for the
    # dashboard; the raw seconds are what /metrics exposes, so a query can pick its
    # own window instead of being stuck with "since huskd started".
    state_seconds: dict[str, float] = field(default_factory=dict)
    boot_seconds: float | None = None  # spawn: issue→ACTIVE (controller clock)


def _short_image(ref: str | None) -> str | None:
    """A compact, human-scannable id for the dashboard: the leading 12 chars of a
    content digest (sans the `sha256:` prefix) or a Glance uuid. None stays None."""
    if not ref:
        return None
    if ref.startswith("sha256:"):
        ref = ref[len("sha256:") :]
    return ref[:12]


def _ref_tag(ref: str) -> str:
    """The human tag from a configured image ref (`…/husk-gpu:v4` → `v4`). Empty for
    a digest-pinned ref (`…@sha256:…`), a bare Glance name, or a tagless ref — the
    `:` in a `registry:port` host is not mistaken for a tag."""
    if not ref:
        return ""
    ref = ref.split("@", 1)[0]  # drop any @sha256:... digest pin
    last = ref.rsplit("/", 1)[-1]  # only the final path segment can carry the tag
    return last.rsplit(":", 1)[-1] if ":" in last else ""


def _slot_image_label(active_image: str | None, stale: bool, tag: str) -> str | None:
    """What to show in the dashboard IMAGE cell. A slot that is NOT stale is running
    the pool's current target, so we can name its tag (`v4`) — the useful signal
    during a rollout. A stale slot's tag isn't recorded anywhere (only its digest
    is baked in), so it falls back to the short digest, flagged stale by the caller.
    A tagless/manual pool just shows the short id."""
    if not stale and tag:
        return tag
    return _short_image(active_image)


@dataclass(frozen=True)
class ControllerState:
    """Immutable snapshot the loop swaps in atomically each tick."""

    generation: int
    last_reconcile_epoch: float
    backend: str
    min_ready: int
    max_total: int
    desired_total: int
    counts: dict[str, int]  # SlotState.value -> count
    image_ref: str = ""  # pool's configured target image ref (for the header + tags)
    slots: list[SlotView] = field(default_factory=list)
    ops: list[OpView] = field(default_factory=list)  # in-flight/recent staging ops

    @classmethod
    def from_classified(
        cls,
        *,
        generation: int,
        backend: str,
        min_ready: int,
        max_total: int,
        desired_total: int,
        classified: list[tuple],  # list of (Slot, Runner|None, SlotState)
        timing: dict | None = None,  # slot_id -> SlotTiming (optional)
        ops: list[OpView] | None = None,  # backend async ops (image staging)
        image_ref: str = "",  # pool's configured target ref → per-slot tag labels
        errors: dict | None = None,  # slot_id -> (epoch, message) last-action failure
    ) -> "ControllerState":
        timing = timing or {}
        errors = errors or {}
        tag = _ref_tag(image_ref)
        counts = {st.value: 0 for st in SlotState}
        views: list[SlotView] = []
        for slot, runner, state in classified:
            counts[state.value] += 1
            t = timing.get(slot.id)
            lf = t.live_fraction if t is not None else None
            views.append(
                SlotView(
                    id=slot.id,
                    name=slot.name,
                    state=state.value,
                    status=slot.status,
                    task_state=slot.task_state,
                    runner=runner.name if runner else None,
                    runner_status=runner.status if runner else None,
                    busy=runner.busy if runner else False,
                    cycle=slot.cycle,
                    ip=slot.ip,
                    host=slot.host,
                    image=_slot_image_label(slot.active_image, slot.image_stale, tag),
                    image_stale=slot.image_stale,
                    error=(errors.get(slot.id) or (None, None))[1],
                    error_epoch=(errors.get(slot.id) or (None, None))[0],
                    cloudinit_seconds=(
                        round(t.last_cloudinit_seconds, 1)
                        if t is not None and t.last_cloudinit_seconds is not None
                        else None
                    ),
                    recycle_seconds=(
                        round(t.last_recycle_seconds, 1)
                        if t is not None and t.last_recycle_seconds is not None
                        else None
                    ),
                    live_fraction=round(lf, 3) if lf is not None else None,
                    state_seconds=(
                        {k: round(v, 1) for k, v in t.state_seconds.items()}
                        if t is not None
                        else {}
                    ),
                    boot_seconds=_round1(
                        t.last_boot_seconds if t is not None else None
                    ),
                )
            )
        return cls(
            generation=generation,
            last_reconcile_epoch=time.time(),
            backend=backend,
            min_ready=min_ready,
            max_total=max_total,
            desired_total=desired_total,
            counts=counts,
            image_ref=image_ref,
            slots=views,
            ops=list(ops or []),
        )

    def to_dict(self) -> dict:
        """Plain dict for JSON rendering (huskctl status / future /status)."""
        return {
            "generation": self.generation,
            "last_reconcile_epoch": self.last_reconcile_epoch,
            "backend": self.backend,
            "min_ready": self.min_ready,
            "max_total": self.max_total,
            "desired_total": self.desired_total,
            "counts": dict(self.counts),
            "image_ref": self.image_ref,
            "slots": [vars(v) for v in self.slots],
            "ops": [vars(o) for o in self.ops],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ControllerState":
        return cls(
            generation=d["generation"],
            last_reconcile_epoch=d["last_reconcile_epoch"],
            backend=d["backend"],
            min_ready=d["min_ready"],
            max_total=d["max_total"],
            desired_total=d["desired_total"],
            counts=dict(d["counts"]),
            image_ref=d.get("image_ref", ""),
            slots=[SlotView(**sv) for sv in d["slots"]],
            ops=[OpView(**o) for o in d.get("ops", [])],
        )
