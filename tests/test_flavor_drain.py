"""Controller destroys (never rebuilds) a NEEDS_RECYCLE slot whose flavor is
stale: Nova's rebuild action can't change a server's flavor, only resize can,
and that stateful two-phase operation isn't attempted here. Destroying an
already-idle/drained slot and letting the pool's normal _grow recreate it is
enough — see controller.py's NEEDS_RECYCLE branch."""

from __future__ import annotations

from conftest import make_config, make_controller, make_runner, make_slot, tick

from husk.fake_backend import FakeBackend, FakeGitHub


def test_flavor_stale_slot_is_destroyed_not_rebuilt(clock):
    slot = make_slot(id="vm-1", name="husk-1", status="SHUTOFF", flavor_stale=True)
    backend = FakeBackend(slots=[slot])
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=1), clock
    )

    tick(ctrl)

    assert ("destroy", "vm-1", "flavor_stale") in backend.calls
    assert "rebuild" not in backend.ops()


def test_fresh_flavor_slot_is_rebuilt_as_normal(clock):
    slot = make_slot(id="vm-1", name="husk-1", status="SHUTOFF", flavor_stale=False)
    backend = FakeBackend(slots=[slot])
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=1), clock
    )

    tick(ctrl)

    assert "rebuild" in backend.ops()
    assert "destroy" not in backend.ops()


def test_only_one_flavor_stale_slot_is_destroyed_per_tick(clock):
    # Both SHUTOFF and flavor-stale. A bad-fit new flavor must not be able to
    # destroy the whole pool's still-working capacity in one tick — only one
    # destroy is allowed per tick, so a create failure surfaces (and can be
    # noticed/stopped) before more capacity is thrown away.
    slots = [
        make_slot(id="vm-1", name="husk-1", status="SHUTOFF", flavor_stale=True),
        make_slot(id="vm-2", name="husk-2", status="SHUTOFF", flavor_stale=True),
    ]
    backend = FakeBackend(slots=slots)
    github = FakeGitHub()
    ctrl = make_controller(
        backend, github, make_config(min_ready=2, max_total=2), clock
    )

    tick(ctrl)

    destroys = [c for c in backend.calls if c[0] == "destroy"]
    assert len(destroys) == 1
    assert destroys[0][2] == "flavor_stale"
    # The other flavor-stale slot falls through to an ordinary rebuild this tick
    # (harmless — it will still be flavor_stale and eligible again next cycle).
    assert "rebuild" in backend.ops()


def test_surplus_retirement_takes_priority_over_flavor_stale(clock):
    # A slot that is BOTH excess (sustained surplus) AND flavor-stale must be
    # retired via the surplus path, not the flavor path — it doesn't need
    # replacing at all, so recreating it under the new flavor would be wasted
    # work the very next tick's downscale would just destroy again.
    backend = FakeBackend(
        slots=[
            make_slot(id="vm-a", name="husk-a", status="ACTIVE", created_at=1.0),
            make_slot(
                id="vm-b",
                name="husk-b",
                status="SHUTOFF",
                created_at=2.0,
                flavor_stale=True,
            ),
        ]
    )
    github = FakeGitHub(runners=[make_runner(id=1, name="husk-a-c0")])
    ctrl = make_controller(
        backend, github, make_config(min_ready=1, max_total=2, shrink_ticks=3), clock
    )

    for _ in range(3):
        clock.advance(5)
        tick(ctrl)

    destroys = [c for c in backend.calls if c[0] == "destroy"]
    assert destroys == [("destroy", "vm-b", "decommission")]  # not "flavor_stale"
