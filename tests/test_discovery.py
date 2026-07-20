"""Target availability: are the configured targets actually installed?

Each [[pool]] names the one target it serves; this decides whether that target is
servable right now. A bug here either serves something nobody configured, or
drains a pool that was fine. The `complete` flag matters just as much: it is what
stops a transient GitHub failure reading as "no longer installed"."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from husk.discovery import DiscoveryError, TargetDiscovery
from husk.target import Target

ORG = Target.org("acts-project")
REPO = Target.repo("paulgessinger/husk-test")

ORG_INSTALL = {"id": 11, "account": {"login": "acts-project", "type": "Organization"}}
USER_INSTALL = {"id": 22, "account": {"login": "paulgessinger", "type": "User"}}


def _run(coro):
    return asyncio.run(coro)


class FakeTokens:
    def __init__(self, installs, *, fail=False) -> None:
        self._installs = installs
        self._fail = fail
        self.token_calls: list[int] = []

    async def installations(self, *, refresh: bool = False) -> list[dict]:
        if self._fail:
            raise RuntimeError("github is down")
        return self._installs

    async def token_for_installation(self, iid: int) -> str:
        self.token_calls.append(iid)
        return f"ghs_{iid}"


def _discovery(tokens, targets, repos_by_token=None, *, repo_status=200):
    repos_by_token = repos_by_token or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if repo_status != 200:
            return httpx.Response(repo_status, json={})
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        names = repos_by_token.get(token, [])
        return httpx.Response(
            200, json={"repositories": [{"full_name": n} for n in names]}
        )

    return TargetDiscovery(
        tokens,
        targets,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ----------------------------------------------------------------- org scope
def test_installed_org_target_is_available():
    d = _discovery(FakeTokens([ORG_INSTALL]), [ORG])
    result = _run(d.discover())
    assert [t.key for t in result.targets] == ["org:acts-project"]
    assert result.complete


def test_uninstalled_org_target_is_not_available():
    d = _discovery(FakeTokens([]), [ORG])
    assert _run(d.discover()).targets == ()


def test_matching_is_case_insensitive_but_keeps_configured_spelling():
    """GitHub logins are case-insensitive; the configured spelling has to win,
    since it is what names the pool's slots."""
    d = _discovery(FakeTokens([{"id": 1, "account": {"login": "ACTS-Project"}}]), [ORG])
    assert [t.key for t in _run(d.discover()).targets] == ["org:acts-project"]


def test_org_targets_never_list_repositories():
    """One API call per installation saved: an org target's availability is
    settled by the installation list alone."""
    tokens = FakeTokens([ORG_INSTALL])
    _run(_discovery(tokens, [ORG]).discover())
    assert tokens.token_calls == []


def test_duplicate_targets_are_checked_once():
    """Several pools commonly serve one target (a gpu and a cpu pool for the same
    org) — that should not double the API work."""
    tokens = FakeTokens([USER_INSTALL])
    d = _discovery(tokens, [REPO, REPO], {"ghs_22": ["paulgessinger/husk-test"]})
    assert len(_run(d.discover()).targets) == 1
    assert tokens.token_calls == [22]


# ---------------------------------------------------------------- repo scope
def test_repo_target_available_when_installed_and_granted():
    d = _discovery(
        FakeTokens([USER_INSTALL]),
        [REPO],
        {"ghs_22": ["paulgessinger/husk-test", "paulgessinger/other"]},
    )
    assert [t.key for t in _run(d.discover()).targets] == [
        "repo:paulgessinger/husk-test"
    ]


def test_repo_target_the_install_did_not_grant_is_not_available():
    """Defense in depth: the installer picks the repos AND the operator configures
    them. Either alone is not enough."""
    d = _discovery(
        FakeTokens([USER_INSTALL]), [REPO], {"ghs_22": ["paulgessinger/something-else"]}
    )
    assert _run(d.discover()).targets == ()


def test_repo_target_whose_owner_has_no_install_is_not_available():
    d = _discovery(FakeTokens([ORG_INSTALL]), [REPO])
    assert _run(d.discover()).targets == ()


def test_org_and_repo_targets_coexist():
    d = _discovery(
        FakeTokens([ORG_INSTALL, USER_INSTALL]),
        [ORG, REPO],
        {"ghs_22": ["paulgessinger/husk-test"]},
    )
    assert [t.key for t in _run(d.discover()).targets] == [
        "org:acts-project",
        "repo:paulgessinger/husk-test",
    ]


# ------------------------------------------------------------------- failure
def test_unreadable_installation_list_raises():
    """Nothing could be determined — the caller must keep its current set."""
    d = _discovery(FakeTokens([], fail=True), [ORG])
    with pytest.raises(DiscoveryError, match="could not list installations"):
        _run(d.discover())


def test_failed_repo_listing_makes_the_sweep_partial_not_empty():
    """The org target still resolves; the sweep is flagged so the supervisor
    won't read the missing repo target as an uninstall."""
    d = _discovery(
        FakeTokens([ORG_INSTALL, USER_INSTALL]), [ORG, REPO], repo_status=500
    )
    result = _run(d.discover())
    assert [t.key for t in result.targets] == ["org:acts-project"]
    assert not result.complete
