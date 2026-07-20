"""Target discovery: installations ∩ allowlist.

This is huskd's entire access-control story — the App is installable by anyone,
so a bug here means either serving an account nobody allowed, or refusing one
that was. The partial-sweep flag matters just as much: it is what stops a
transient GitHub failure from being read as "this target went away"."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from husk.discovery import Allowlist, DiscoveryError, TargetDiscovery

ORG_INSTALL = {"id": 11, "account": {"login": "acts-project", "type": "Organization"}}
USER_INSTALL = {"id": 22, "account": {"login": "paulgessinger", "type": "User"}}
STRANGER = {"id": 33, "account": {"login": "randomorg", "type": "Organization"}}


def _run(coro):
    return asyncio.run(coro)


class FakeTokens:
    """Stands in for InstallationTokenProvider. Records repo listings requested."""

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


def _discovery(tokens, allow, repos_by_token=None, *, repo_status=200):
    """Wire a TargetDiscovery to a mocked /installation/repositories."""
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
        allow,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ------------------------------------------------------------------- allowlist
def test_allowed_org_becomes_an_org_target():
    d = _discovery(FakeTokens([ORG_INSTALL]), Allowlist(orgs=("acts-project",)))
    result = _run(d.discover())
    assert [t.key for t in result.targets] == ["org:acts-project"]
    assert result.complete


def test_installation_not_on_the_allowlist_is_ignored():
    d = _discovery(
        FakeTokens([ORG_INSTALL, STRANGER]), Allowlist(orgs=("acts-project",))
    )
    assert [t.key for t in _run(d.discover()).targets] == ["org:acts-project"]


def test_matching_is_case_insensitive_but_keeps_configured_spelling():
    """GitHub logins are case-insensitive; `vm_prefix` is derived from the target
    name, so the *configured* spelling has to win or names would churn."""
    install = {"id": 1, "account": {"login": "ACTS-Project"}}
    d = _discovery(FakeTokens([install]), Allowlist(orgs=("acts-project",)))
    assert [t.key for t in _run(d.discover()).targets] == ["org:acts-project"]


def test_org_only_allowlist_never_lists_repositories():
    """The repo listing is one API call per installation; skip it when no repo
    could possibly match."""
    tokens = FakeTokens([ORG_INSTALL])
    d = _discovery(tokens, Allowlist(orgs=("acts-project",)))
    _run(d.discover())
    assert tokens.token_calls == []


# ----------------------------------------------------------------- repo scope
def test_allowed_repo_becomes_a_repo_target():
    d = _discovery(
        FakeTokens([USER_INSTALL]),
        Allowlist(repos=("paulgessinger/husk-test",)),
        {"ghs_22": ["paulgessinger/husk-test", "paulgessinger/other"]},
    )
    assert [t.key for t in _run(d.discover()).targets] == [
        "repo:paulgessinger/husk-test"
    ]


def test_allowlisted_repo_the_install_did_not_grant_is_not_served():
    """Defense in depth: the installer picks repos AND huskd's operator lists
    them. Either alone is not enough."""
    d = _discovery(
        FakeTokens([USER_INSTALL]),
        Allowlist(repos=("paulgessinger/husk-test",)),
        {"ghs_22": ["paulgessinger/something-else"]},
    )
    assert _run(d.discover()).targets == ()


def test_repo_allowlist_does_not_leak_to_the_owners_other_repos():
    d = _discovery(
        FakeTokens([USER_INSTALL]),
        Allowlist(repos=("paulgessinger/husk-test",)),
        {"ghs_22": ["paulgessinger/a", "paulgessinger/b", "paulgessinger/husk-test"]},
    )
    assert [t.key for t in _run(d.discover()).targets] == [
        "repo:paulgessinger/husk-test"
    ]


def test_org_and_repo_targets_coexist():
    d = _discovery(
        FakeTokens([ORG_INSTALL, USER_INSTALL]),
        Allowlist(orgs=("acts-project",), repos=("paulgessinger/husk-test",)),
        {"ghs_22": ["paulgessinger/husk-test"]},
    )
    assert [t.key for t in _run(d.discover()).targets] == [
        "org:acts-project",
        "repo:paulgessinger/husk-test",
    ]


# -------------------------------------------------------------------- failure
def test_unreadable_installation_list_raises():
    """Nothing could be determined at all — the caller must keep its current set."""
    d = _discovery(FakeTokens([], fail=True), Allowlist(orgs=("acts-project",)))
    with pytest.raises(DiscoveryError, match="could not list installations"):
        _run(d.discover())


def test_failed_repo_listing_makes_the_sweep_partial_not_empty():
    """The org target still resolves; the sweep is flagged so the supervisor
    won't read the missing repo target as a removal."""
    d = _discovery(
        FakeTokens([ORG_INSTALL, USER_INSTALL]),
        Allowlist(orgs=("acts-project",), repos=("paulgessinger/husk-test",)),
        repo_status=500,
    )
    result = _run(d.discover())
    assert [t.key for t in result.targets] == ["org:acts-project"]
    assert not result.complete


# ------------------------------------------------------------------ allowlist
def test_allowlist_rejects_a_repo_in_the_org_list():
    with pytest.raises(ValueError, match="allowed_repos"):
        Allowlist(orgs=("owner/name",))


def test_allowlist_rejects_a_bare_login_in_the_repo_list():
    with pytest.raises(ValueError, match="owner/name"):
        Allowlist(repos=("acts-project",))


def test_allowlist_size_counts_both_lists():
    assert len(Allowlist(orgs=("a", "b"), repos=("c/d",))) == 3
