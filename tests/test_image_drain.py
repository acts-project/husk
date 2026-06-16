"""Controller drains a slot whose image is stale (config image_ref advanced):
an idle stale slot has its runner deregistered so it recycles onto the new image,
without waiting for the idle timeout. Fresh-image idle slots are left alone."""

from __future__ import annotations

from conftest import make_config, make_controller, make_runner, make_slot

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
    ctrl.tick()
    assert ("delete_runner", 7) in github.calls  # deregistered → will recycle


def test_fresh_idle_slot_is_not_drained(clock):
    ctrl, github = _idle_setup(image_stale=False, clock=clock)
    ctrl.tick()
    assert "delete_runner" not in github.ops()  # nothing stale, far from idle_timeout


def test_sync_hook_absent_backend_is_noop(clock):
    # FakeBackend has no sync_images; the tick's image-sync step must no-op, not raise.
    slot = make_slot(id="vm-1", name="husk-1", status="ACTIVE")
    backend = FakeBackend(slots=[slot])
    github = FakeGitHub(runners=[make_runner(id=1, name="husk-1-c0")])
    ctrl = make_controller(backend, github, make_config(), clock)
    ctrl.tick()  # must not raise
