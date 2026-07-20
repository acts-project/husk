"""GitHubClient's HTTP plumbing on httpx: error wrapping, the JIT 409 retry, and
the wall-clock deadline backstop.

The deadline exists for the one thing a per-operation timeout can't bound: DNS.
CPython's `socket.getaddrinfo` has no timeout of its own, so `asyncio.wait_for`
wraps the whole request as a backstop."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from husk import github as ghmod
from husk.github import GitHubClient, GitHubError


def _client(handler) -> GitHubClient:
    """A client whose transport is driven by `handler(request) -> httpx.Response`."""
    return GitHubClient(
        repo="acme/widgets",
        token="tok",
        labels=["self-hosted"],
        runner_group_id=1,
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
    assert seen["authorization"] == "Bearer tok"
    assert seen["x-github-api-version"] == "2022-11-28"


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
    monkeypatch.setattr(ghmod, "_DEADLINE_S", 0.2)

    async def _hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()  # never returns
        raise AssertionError("unreachable")

    client = GitHubClient(
        repo="acme/widgets",
        token="tok",
        labels=["self-hosted"],
        runner_group_id=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(_hang)),
    )
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
