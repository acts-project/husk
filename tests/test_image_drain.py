"""Controller drains a slot whose image is stale (config image_ref advanced):
an idle stale slot has its runner deregistered so it recycles onto the new image,
without waiting for the idle timeout. Fresh-image idle slots are left alone."""

from __future__ import annotations

from conftest import make_config, make_controller, make_runner, make_slot, tick

from husk.fake_backend import FakeBackend, FakeGitHub


def _idle_setup(*, image_stale: bool, clock):
    # ACTIVE slot with an online, not-busy runner → classifies IDLE.
    slot = make_slot(id="vm-1", name="husk-1", status="ACTIVE", image_stale=image_stale)
    backend = FakeBackend(slots=[slot])
    github = FakeGitHub(runners=[make_runner(id=7, name="husk-1-c0")])
    # idle_timeout huge so only the stale-image path can trigger a deregister.
    cfg = make_config(min_ready=1, max_total=1, idle_timeout=10**9)
    return make_controller(backend, github, cfg, clock), github


def test_stale_idle_slot_is_drained(clock):
    ctrl, github = _idle_setup(image_stale=True, clock=clock)
    tick(ctrl)
    assert ("delete_runner", 7) in github.calls  # deregistered → will recycle


def test_fresh_idle_slot_is_not_drained(clock):
    ctrl, github = _idle_setup(image_stale=False, clock=clock)
    tick(ctrl)
    assert "delete_runner" not in github.ops()  # nothing stale, far from idle_timeout


def test_sync_hook_absent_backend_is_noop(clock):
    # FakeBackend has no sync_images; the tick's image-sync step must no-op, not raise.
    slot = make_slot(id="vm-1", name="husk-1", status="ACTIVE")
    backend = FakeBackend(slots=[slot])
    github = FakeGitHub(runners=[make_runner(id=1, name="husk-1-c0")])
    ctrl = make_controller(backend, github, make_config(), clock)
    tick(ctrl)  # must not raise


def test_recycle_deferred_while_image_stages(clock):
    # SHUTOFF + no runner ⇒ NEEDS_RECYCLE. While the golden is still staging the
    # backend reports image not ready: the controller must NOT rebuild (nor mint a
    # JIT) — it defers until the image lands rather than erroring every tick.
    slot = make_slot(id="vm-1", name="husk-1", status="SHUTOFF", cycle=4)
    backend = FakeBackend(slots=[slot], image_ready=False)
    github = FakeGitHub()
    ctrl = make_controller(backend, github, make_config(), clock)

    tick(ctrl)

    assert "rebuild" not in backend.ops()
    assert "mint" not in [c[0] for c in github.calls]


def test_recycle_proceeds_once_image_ready(clock):
    # Same slot, image now staged: the recycle rebuilds and mints a fresh JIT.
    slot = make_slot(id="vm-1", name="husk-1", status="SHUTOFF", cycle=4)
    backend = FakeBackend(slots=[slot], image_ready=True)
    github = FakeGitHub()
    ctrl = make_controller(backend, github, make_config(), clock)

    tick(ctrl)

    assert "rebuild" in backend.ops()
    assert ("mint", "husk-1-c5") in github.calls
