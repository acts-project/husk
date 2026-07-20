"""GitHubClient's HTTP plumbing on httpx: error wrapping, the JIT 409 retry, and
the wall-clock deadline backstop.

The deadline exists for the one thing a per-operation timeout can't bound: DNS.
CPython's `socket.getaddrinfo` has no timeout of its own, so `asyncio.wait_for`
wraps the whole request as a backstop."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from husk import ghhttp
from husk.github import GitHubClient, GitHubError
from husk.target import Target

ORG = Target.org("acme")
REPO = Target.repo("acme/widgets")


class FakeTokens:
    """Stands in for InstallationTokenProvider: hands out a token, counts mints,
    and records invalidations so the 401 retry path can be asserted."""

    def __init__(self) -> None:
        self.mints = 0
        self.invalidations: list[str] = []

    async def token_for(self, target) -> str:
        self.mints += 1
        return f"ghs_{self.mints}"

    def invalidate(self, target) -> None:
        self.invalidations.append(target.key)


def _client(handler, *, target=REPO, tokens=None, runner_group="Default"):
    """A client whose transport is driven by `handler(request) -> httpx.Response`."""
    return GitHubClient(
        target=target,
        tokens=tokens or FakeTokens(),
        labels=["self-hosted"],
        runner_group=runner_group,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _run(coro):
    return asyncio.run(coro)


def test_list_runners_success() -> None:
    payload = {"runners": [{"id": 1, "name": "r1", "status": "online", "busy": False}]}
    client = _client(lambda request: httpx.Response(200, json=payload))
    runners = _run(client.list_runners())
    assert [r.id for r in runners] == [1]


def test_auth_headers_are_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"runners": []})

    _run(_client(handler).list_runners())
    assert seen["authorization"] == "Bearer ghs_1"  # installation token, not a PAT
    assert seen["x-github-api-version"] == "2022-11-28"


# ------------------------------------------------------------------ scoping
def test_org_and_repo_targets_hit_different_paths() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"runners": [], "runner_groups": []})

    _run(_client(handler, target=REPO).list_runners())
    _run(_client(handler, target=ORG).list_runners())
    assert paths == [
        "/repos/acme/widgets/actions/runners",
        "/orgs/acme/actions/runners",
    ]


def test_401_reminted_and_retried_once() -> None:
    tokens = FakeTokens()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        # First token is stale; the re-minted one works.
        if len(seen) == 1:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json={"runners": []})

    _run(_client(handler, tokens=tokens).list_runners())
    assert seen == ["Bearer ghs_1", "Bearer ghs_2"]  # retried with a fresh token
    assert tokens.invalidations == [REPO.key]


# ------------------------------------------------------------ runner groups
def test_repo_scope_has_no_runner_group() -> None:
    client = _client(lambda r: httpx.Response(200, json={"runners": []}), target=REPO)
    assert _run(client.group_id()) is None


def test_org_group_name_resolves_to_id() -> None:
    groups = {
        "runner_groups": [{"id": 1, "name": "Default"}, {"id": 7, "name": "husk"}]
    }
    client = _client(
        lambda r: httpx.Response(200, json=groups), target=ORG, runner_group="husk"
    )
    assert _run(client.group_id()) == 7


def test_unknown_org_group_falls_back_to_default() -> None:
    # A free-plan org can't create groups at all, so an unknown name must degrade
    # rather than fail the mint.
    groups = {"runner_groups": [{"id": 1, "name": "Default"}]}
    client = _client(
        lambda r: httpx.Response(200, json=groups), target=ORG, runner_group="husk"
    )
    assert _run(client.group_id()) == 1


def test_group_listing_failure_falls_back_to_default() -> None:
    client = _client(
        lambda r: httpx.Response(403, text="no"), target=ORG, runner_group="husk"
    )
    assert _run(client.group_id()) == 1


def test_org_mint_sends_resolved_group_repo_mint_does_not() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runner-groups"):
            return httpx.Response(
                200, json={"runner_groups": [{"id": 7, "name": "husk"}]}
            )
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(201, json={"encoded_jit_config": "x"})
        return httpx.Response(200, json={"runners": []})

    _run(_client(handler, target=ORG, runner_group="husk").generate_jitconfig("r1"))
    _run(_client(handler, target=REPO, runner_group="husk").generate_jitconfig("r1"))
    assert bodies[0]["runner_group_id"] == 7  # org scope carries the group
    assert "runner_group_id" not in bodies[1]  # repo scope has no groups


def test_list_runners_wraps_immediate_connection_error() -> None:
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(GitHubError, match="list runners failed"):
        _run(_client(_raise).list_runners())


def test_list_runners_wraps_http_error_status() -> None:
    client = _client(lambda request: httpx.Response(500, text="server oops"))
    with pytest.raises(GitHubError, match="list runners failed"):
        _run(client.list_runners())


def test_hung_request_is_bounded_by_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request that never returns (the DNS-hang case) must not wedge the caller
    forever — `asyncio.wait_for` trips and surfaces as a wrapped GitHubError."""
    monkeypatch.setattr(ghhttp, "DEADLINE_S", 0.2)

    async def _hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()  # never returns
        raise AssertionError("unreachable")

    client = _client(_hang)
    start = time.monotonic()
    with pytest.raises(GitHubError, match="list runners failed"):
        _run(client.list_runners())
    assert time.monotonic() - start < 2.0


def test_delete_runner_tolerates_404() -> None:
    client = _client(lambda request: httpx.Response(404))
    _run(client.delete_runner(7))  # must not raise


def test_delete_runner_raises_on_unexpected_status() -> None:
    client = _client(lambda request: httpx.Response(500, text="nope"))
    with pytest.raises(GitHubError, match="delete runner 7"):
        _run(client.delete_runner(7))


def test_generate_jitconfig_retries_after_409() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls.append("post")
            if calls.count("post") == 1:
                return httpx.Response(409)
            return httpx.Response(201, json={"encoded_jit_config": "abc"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "runners": [
                        {"id": 5, "name": "r5", "status": "offline", "busy": False}
                    ]
                },
            )
        if request.method == "DELETE":
            calls.append("delete")
            return httpx.Response(204)
        raise AssertionError(f"unexpected method {request.method}")

    jit = _run(_client(handler).generate_jitconfig("r5"))
    assert jit == "abc"
    assert calls == ["post", "delete", "post"]


def test_reap_offline_deletes_only_offline() -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "runners": [
                        {"id": 1, "name": "live", "status": "online", "busy": False},
                        {"id": 2, "name": "dead", "status": "offline", "busy": False},
                    ]
                },
            )
        deleted.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(204)

    assert _run(_client(handler).reap_offline()) == ["dead"]
    assert deleted == ["2"]
