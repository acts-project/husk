"""GitHubClient's HTTP plumbing: timeout wrapping and the DNS-hang deadline
backstop (a `requests` timeout only bounds connect/read, not the DNS lookup
urllib3 does before that timeout ever applies)."""

from __future__ import annotations

import threading
import time

import pytest
import requests

from husk import github as ghmod
from husk.github import GitHubClient, GitHubError


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self, request_fn) -> None:
        self.headers: dict = {}
        self._request_fn = request_fn

    def request(self, method: str, url: str, **kwargs):
        return self._request_fn(method, url, **kwargs)


def _client(request_fn) -> GitHubClient:
    return GitHubClient(
        repo="acme/widgets",
        token="tok",
        labels=["self-hosted"],
        runner_group_id=1,
        session=_FakeSession(request_fn),
    )


def test_list_runners_success() -> None:
    payload = {"runners": [{"id": 1, "name": "r1", "status": "online", "busy": False}]}
    client = _client(lambda method, url, **kw: _FakeResponse(200, payload))
    runners = client.list_runners()
    assert [r.id for r in runners] == [1]


def test_list_runners_wraps_immediate_connection_error() -> None:
    def _raise(method, url, **kw):
        raise requests.ConnectionError("boom")

    client = _client(_raise)
    with pytest.raises(GitHubError, match="list runners failed"):
        client.list_runners()


def test_hung_request_is_bounded_by_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request that never returns (the DNS-hang case) must not wedge the
    caller forever — the deadline backstop abandons it and raises instead."""
    monkeypatch.setattr(ghmod, "_DEADLINE_S", 0.2)

    def _hang(method, url, **kw):
        threading.Event().wait()  # never returns; simulates a wedged getaddrinfo

    client = _client(_hang)
    start = time.monotonic()
    with pytest.raises(GitHubError, match="list runners failed"):
        client.list_runners()
    assert time.monotonic() - start < 2.0


def test_delete_runner_tolerates_404() -> None:
    client = _client(lambda method, url, **kw: _FakeResponse(404))
    client.delete_runner(7)  # must not raise


def test_generate_jitconfig_retries_after_409() -> None:
    calls: list[str] = []

    def _request(method, url, **kw):
        if method == "POST":
            calls.append("post")
            if calls.count("post") == 1:
                return _FakeResponse(409)
            return _FakeResponse(201, {"encoded_jit_config": "abc"})
        if method == "GET":
            return _FakeResponse(
                200,
                {
                    "runners": [
                        {"id": 5, "name": "r5", "status": "offline", "busy": False}
                    ]
                },
            )
        if method == "DELETE":
            calls.append("delete")
            return _FakeResponse(204)
        raise AssertionError(f"unexpected method {method}")

    client = _client(_request)
    jit = client.generate_jitconfig("r5")
    assert jit == "abc"
    assert calls == ["post", "delete", "post"]
