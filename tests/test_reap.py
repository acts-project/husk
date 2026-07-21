"""Cleanup of dead GitHub runner registrations.

Runners are named ``f"{vm}-c{cycle}"`` and a slot that never registers strands its
registration offline forever (see `slot.orphaned_runners`), so without a reaper
they accumulate. The risk runs the other way too: deleting a registration a slot
is about to use costs a rebuild. These tests pin both edges — what must be
collected, and what must survive.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import make_config, make_controller, make_runner, make_slot, tick
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.github import GitHubClient
from husk.slot import orphaned_runners
from husk.target import Target


# --------------------------------------------------------------- policy (pure)
def test_orphan_when_slot_is_gone() -> None:
    # husk-1 exists; husk-2 was destroyed but left a registration behind.
    slots = [make_slot(id="a", name="husk-1", cycle=0)]
    runners = [
        make_runner(id=1, name="husk-1-c0", status="online"),
        make_runner(id=2, name="husk-2-c0", status="offline"),
    ]
    assert [r.id for r in orphaned_runners(runners, slots, "husk")] == [2]


def test_prior_cycle_is_orphan_but_current_is_kept() -> None:
    # The slot has moved on to cycle 2. c0/c1 are spent; c2 is offline only because
    # the slot is mid-boot — deleting it would strand the slot.
    slots = [make_slot(id="a", name="husk-1", cycle=2)]
    runners = [
        make_runner(id=1, name="husk-1-c0", status="offline"),
        make_runner(id=2, name="husk-1-c1", status="offline"),
        make_runner(id=3, name="husk-1-c2", status="offline"),
    ]
    assert [r.id for r in orphaned_runners(runners, slots, "husk")] == [1, 2]


def test_online_runner_is_never_orphaned() -> None:
    # Belt and braces: an online runner is doing work, whatever its cycle says.
    slots = [make_slot(id="a", name="husk-1", cycle=5)]
    runners = [make_runner(id=1, name="husk-1-c0", status="online")]
    assert orphaned_runners(runners, slots, "husk") == []


def test_other_prefix_is_untouchable() -> None:
    # THE safety property: someone else's offline runner must be unreachable, and
    # so must a sibling pool's.
    slots = [make_slot(id="a", name="husk-cpu-1", cycle=0)]
    runners = [
        make_runner(id=1, name="someones-laptop", status="offline"),
        make_runner(id=2, name="husk-gpu-9-c0", status="offline"),
    ]
    assert orphaned_runners(runners, slots, "husk-cpu") == []


def test_unparseable_name_is_left_alone() -> None:
    # Right prefix, but no -c<N> tail: not a name husk minted, so don't guess.
    slots = [make_slot(id="a", name="husk-1", cycle=0)]
    runners = [make_runner(id=1, name="husk-manual-thing", status="offline")]
    assert orphaned_runners(runners, slots, "husk") == []


def test_vm_name_containing_dash_c_is_split_at_the_last_one() -> None:
    # "husk-cern-oci-..." contains "-c" before the cycle suffix; rpartition must
    # take the trailing one or the vm would never match a live slot.
    slots = [make_slot(id="a", name="husk-cern-oci-7", cycle=3)]
    runners = [
        make_runner(id=1, name="husk-cern-oci-7-c1", status="offline"),
        make_runner(id=2, name="husk-cern-oci-7-c3", status="offline"),
    ]
    assert [r.id for r in orphaned_runners(runners, slots, "husk")] == [1]


# ----------------------------------------------------------- controller wiring
def _ctrl(clock, *, mode: str, runners, slots):
    backend = FakeBackend(slots=slots)
    github = FakeGitHub(runners=runners)
    return (
        backend,
        github,
        make_controller(
            backend,
            github,
            make_config(reap_runners=mode, min_ready=0, max_total=4),
            clock,
        ),
    )


def test_reaper_is_off_by_default(clock) -> None:
    slots = [make_slot(id="a", name="husk-1", status="ACTIVE", cycle=1)]
    runners = [make_runner(id=9, name="husk-dead-c0", status="offline")]
    _, github, ctrl = _ctrl(clock, mode="off", runners=runners, slots=slots)

    tick(ctrl)

    assert ("delete_runner", 9) not in github.calls


def test_dry_run_reports_but_deletes_nothing(clock) -> None:
    slots = [make_slot(id="a", name="husk-1", status="ACTIVE", cycle=1)]
    runners = [make_runner(id=9, name="husk-dead-c0", status="offline")]
    _, github, ctrl = _ctrl(clock, mode="dry-run", runners=runners, slots=slots)

    tick(ctrl)

    assert ("delete_runner", 9) not in github.calls


def test_on_deletes_the_orphan_only(clock) -> None:
    slots = [make_slot(id="a", name="husk-1", status="ACTIVE", cycle=1)]
    runners = [
        make_runner(id=1, name="husk-1-c1", status="online"),  # live slot's runner
        make_runner(id=9, name="husk-dead-c0", status="offline"),  # orphan
    ]
    _, github, ctrl = _ctrl(clock, mode="on", runners=runners, slots=slots)

    tick(ctrl)

    assert ("delete_runner", 9) in github.calls
    assert ("delete_runner", 1) not in github.calls


def test_reap_is_rate_limited(clock) -> None:
    # Two ticks in quick succession must not re-list/re-delete: the interval is
    # what keeps this off the 5s reconcile cadence.
    slots = [make_slot(id="a", name="husk-1", status="ACTIVE", cycle=1)]
    runners = [make_runner(id=9, name="husk-dead-c0", status="offline")]
    _, github, ctrl = _ctrl(clock, mode="on", runners=runners, slots=slots)

    tick(ctrl)
    deletes = [c for c in github.calls if c[0] == "delete_runner"]
    tick(ctrl)

    assert [c for c in github.calls if c[0] == "delete_runner"] == deletes


# ------------------------------------------------------------------ pagination
class _Tokens:
    async def token_for(self, target) -> str:
        return "ghs_x"

    def invalidate(self, target) -> None:
        pass


def _paged_client(pages):
    """A client whose /actions/runners returns `pages[page-1]`."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        seen.append(page)
        body = pages[page - 1] if page <= len(pages) else []
        return httpx.Response(200, json={"runners": body})

    client = GitHubClient(
        target=Target.org("acme"),
        tokens=_Tokens(),
        labels=["self-hosted"],
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return client, seen


def _runner_page(n, start):
    return [
        {"id": start + i, "name": f"r{start + i}", "status": "online", "busy": False}
        for i in range(n)
    ]


def test_listing_follows_pages() -> None:
    # A full page means "there may be more"; a short page ends the walk. Without
    # this, the controller sees only the first 100 runners and rebuilds the rest.
    client, seen = _paged_client([_runner_page(100, 0), _runner_page(7, 100)])
    runners = asyncio.run(client.list_runners())
    assert len(runners) == 107
    assert seen == [1, 2]


def test_listing_stops_on_a_short_first_page() -> None:
    client, seen = _paged_client([_runner_page(3, 0)])
    runners = asyncio.run(client.list_runners())
    assert len(runners) == 3
    assert seen == [1]  # no speculative second request


# ------------------------------------------------------------ reap_offline API
def _reap_client(runners):
    deleted: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(int(request.url.path.rsplit("/", 1)[1]))
            return httpx.Response(204)
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"runners": runners if page == 1 else []})

    client = GitHubClient(
        target=Target.org("acme"),
        tokens=_Tokens(),
        labels=["self-hosted"],
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return client, deleted


_MIXED = [
    {"id": 1, "name": "husk-a-c0", "status": "offline", "busy": False},
    {"id": 2, "name": "husk-a-c1", "status": "online", "busy": False},
    {"id": 3, "name": "someone-else", "status": "offline", "busy": False},
]


def test_reap_offline_scopes_to_prefixes() -> None:
    client, deleted = _reap_client(_MIXED)
    names = asyncio.run(client.reap_offline(prefixes=["husk-"]))
    assert names == ["husk-a-c0"]
    assert deleted == [1]  # someone-else survives


def test_reap_offline_unscoped_is_the_blunt_instrument() -> None:
    # Documents the danger `--all` opts into: no prefixes means org-wide.
    client, deleted = _reap_client(_MIXED)
    names = asyncio.run(client.reap_offline())
    assert names == ["husk-a-c0", "someone-else"]
    assert deleted == [1, 3]


def test_reap_offline_dry_run_deletes_nothing() -> None:
    client, deleted = _reap_client(_MIXED)
    names = asyncio.run(client.reap_offline(prefixes=["husk-"], dry_run=True))
    assert names == ["husk-a-c0"]
    assert deleted == []


def test_reap_offline_honours_keep() -> None:
    client, deleted = _reap_client(_MIXED)
    names = asyncio.run(client.reap_offline(prefixes=["husk-"], keep={"husk-a-c0"}))
    assert names == []
    assert deleted == []


@pytest.mark.parametrize("mode", ["off", "dry-run", "on"])
def test_config_accepts_each_mode(mode: str) -> None:
    assert make_config(reap_runners=mode).controller.reap_runners == mode
