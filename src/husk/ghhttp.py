"""Shared GitHub HTTP plumbing.

Both the App-auth token provider (`husk.appauth`) and the runner client
(`husk.github`) talk to the same API with the same bounds, so the base URL,
headers, error type, and the two-layer timeout live here rather than being
duplicated (or creating an import cycle between them).

Two layers bound every call:

* `HTTP_TIMEOUT_S` — httpx's per-operation timeout (connect/read/write/pool).
* `DEADLINE_S` — a wall-clock backstop via `asyncio.wait_for`, for the one thing
  a per-op timeout can't bound: DNS. CPython's `socket.getaddrinfo` has no
  timeout of its own and can hang indefinitely after a sleep/wake or a VPN flap.

`wait_for` genuinely *cancels* socket connect/read — it closes the transport
rather than abandoning a thread. (The DNS case stays abandonment either way:
asyncio resolves in its default thread executor, so a wedged `getaddrinfo`
outlives the cancelled coroutine. That's a CPython property, not a design gap.)
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

GH_API = "https://api.github.com"

HTTP_TIMEOUT_S = 30
DEADLINE_S = HTTP_TIMEOUT_S + 15

API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubError(Exception):
    """A GitHub API call failed."""


def new_client() -> httpx.AsyncClient:
    """An httpx client carrying the standard API headers and per-op timeout."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT_S), headers=dict(API_HEADERS)
    )


def adopt(client: httpx.AsyncClient | None) -> httpx.AsyncClient:
    """Return a usable client: a fresh one, or an injected one (tests, custom
    transports) with the API headers applied so it behaves identically."""
    if client is None:
        return new_client()
    client.headers.update(API_HEADERS)
    return client


async def request(
    client: httpx.AsyncClient, method: str, url: str, *, token: str, **kwargs: Any
) -> httpx.Response:
    """One bounded, authenticated request.

    The bearer is passed per-call rather than pinned to the client, because App
    installation tokens expire hourly and are refreshed underneath us.

    A blown deadline surfaces as `httpx.TimeoutException` (a `RequestError`) so
    callers' existing `httpx.HTTPError` handlers wrap it in the same contextual
    `GitHubError` as any other transport failure."""
    headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
    try:
        # DEADLINE_S is read at call time so tests can shrink it via monkeypatch.
        return await asyncio.wait_for(
            client.request(method, url, headers=headers, **kwargs),
            timeout=DEADLINE_S,
        )
    except (TimeoutError, asyncio.TimeoutError) as e:
        raise httpx.TimeoutException(
            f"{method} {url} exceeded {DEADLINE_S:.0f}s deadline (DNS/connect hang?)"
        ) from e
