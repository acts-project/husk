"""GitHub Actions runner management — async client over `httpx`.

PAT (Bearer) auth; the JIT mint keeps its 409→delete→retry idempotency
(load-bearing on controller restart).

Two layers bound every call, mirroring what the old sync client did with a
hand-rolled daemon thread:

* `_HTTP_TIMEOUT_S` is httpx's per-operation timeout (connect/read/write/pool).
* `_DEADLINE_S` is a wall-clock backstop via `asyncio.wait_for`, for the one thing
  a per-op timeout can't bound: DNS. CPython resolves names through
  `socket.getaddrinfo`, which has no timeout of its own and can hang indefinitely
  after a laptop sleep/wake or a Wi-Fi/VPN flap wedges the resolver.

Unlike the old daemon-thread deadline, `wait_for` genuinely *cancels* socket
connect/read — it closes the transport rather than abandoning a thread. (The DNS
case stays abandonment either way: asyncio resolves via its default thread
executor, so a wedged `getaddrinfo` thread outlives the cancelled coroutine. That
is a CPython property, not something this design gives up.)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from husk.slot import Runner

log = logging.getLogger("husk.github")

GH_API = "https://api.github.com"

# Cap every GitHub call so a hung/black-holed request can't wedge a reconcile
# task. Pools reconcile concurrently on one event loop, so an unbounded call in
# one pool must never stall another's ticks (or the HTTP surface).
_HTTP_TIMEOUT_S = 30

# Wall-clock backstop over the whole request — see the module docstring (DNS).
_DEADLINE_S = _HTTP_TIMEOUT_S + 15


class GitHubError(Exception):
    """A GitHub API call failed."""


class GitHubClient:
    def __init__(
        self,
        *,
        repo: str,
        token: str,
        labels: list[str],
        runner_group_id: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.repo = repo
        self.labels = labels
        self.runner_group_id = runner_group_id
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(_HTTP_TIMEOUT_S), headers=headers
            )
        else:  # injected (tests): keep its transport, add our auth headers
            self._client = client
            self._client.headers.update(headers)

    async def aclose(self) -> None:
        """Release the connection pool. Called once at daemon shutdown."""
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Single choke point for outbound calls: httpx's per-op timeout plus the
        wall-clock deadline backstop.

        The deadline surfaces as `httpx.TimeoutException` (a `RequestError`) so
        every caller's existing `httpx.HTTPError` handler wraps it in the same
        contextual `GitHubError` as any other transport failure."""
        try:
            return await asyncio.wait_for(
                self._client.request(method, url, **kwargs), timeout=_DEADLINE_S
            )
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise httpx.TimeoutException(
                f"{method} {url} exceeded {_DEADLINE_S:.0f}s deadline (DNS/connect hang?)"
            ) from e

    # ------------------------------------------------------------------ reads
    async def _list_raw(self) -> list[dict]:
        try:
            r = await self._request(
                "GET", f"{GH_API}/repos/{self.repo}/actions/runners?per_page=100"
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitHubError(f"list runners failed: {e}") from e
        runners = r.json().get("runners", [])
        log.debug(
            "GET runners -> HTTP %d, %d runner(s): %s",
            r.status_code,
            len(runners),
            [
                f"{x['name']}={x['status']}{'/busy' if x.get('busy') else ''}"
                for x in runners
            ],
        )
        return runners

    async def list_runners(self) -> list[Runner]:
        return [
            Runner(id=x["id"], name=x["name"], status=x["status"], busy=bool(x["busy"]))
            for x in await self._list_raw()
        ]

    async def find_runner(self, name: str) -> dict | None:
        for x in await self._list_raw():
            if x["name"] == name:
                return x
        return None

    # ----------------------------------------------------------------- writes
    async def generate_jitconfig(self, name: str) -> str:
        """Mint a single-use JIT config. Idempotent: a lingering same-name
        registration (interrupted run / restart) is deleted and the mint retried."""
        body = {
            "name": name,
            "runner_group_id": self.runner_group_id,
            "labels": self.labels,
            "work_folder": "_work",
        }
        url = f"{GH_API}/repos/{self.repo}/actions/runners/generate-jitconfig"
        log.debug("POST generate-jitconfig name=%s labels=%s", name, self.labels)
        try:
            r = await self._request("POST", url, json=body)
            if r.status_code == 409:
                existing = await self.find_runner(name)
                if existing:
                    log.info(
                        "runner %s already exists (%s); deleting and retrying",
                        name,
                        existing.get("status"),
                    )
                    await self.delete_runner(existing["id"])
                r = await self._request("POST", url, json=body)
        except httpx.HTTPError as e:
            raise GitHubError(f"generate-jitconfig failed: {e}") from e
        if r.status_code != 201:
            raise GitHubError(f"JIT mint failed: HTTP {r.status_code}: {r.text[:300]}")
        log.debug("minted JIT for runner %s", name)
        return r.json()["encoded_jit_config"]

    async def delete_runner(self, runner_id: int) -> None:
        try:
            r = await self._request(
                "DELETE", f"{GH_API}/repos/{self.repo}/actions/runners/{runner_id}"
            )
        except httpx.HTTPError as e:
            raise GitHubError(f"delete runner {runner_id} failed: {e}") from e
        if r.status_code not in (204, 404):
            raise GitHubError(
                f"delete runner {runner_id}: HTTP {r.status_code}: {r.text[:200]}"
            )
        log.debug("DELETE runner %d -> HTTP %d", runner_id, r.status_code)

    async def reap_offline(self) -> list[str]:
        """Delete every offline runner — clears leftover/dead JIT registrations."""
        reaped = []
        for x in await self._list_raw():
            if x["status"] == "offline":
                await self.delete_runner(x["id"])
                reaped.append(x["name"])
        return reaped
