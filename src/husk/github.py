"""GitHub Actions runner management, scoped to one `Target`.

The client is now target-scoped rather than repo-scoped: the same code serves an
org (`/orgs/{login}/actions/runners`) or a single repo
(`/repos/{owner}/{name}/actions/runners`) — the endpoints are otherwise
identical, which is what makes the hybrid scope cheap.

Auth is a short-lived App installation token fetched per request from the
`InstallationTokenProvider` (see `husk.appauth`). A 401 means the token was
revoked or the App reinstalled, so it is invalidated and the call retried once.

Runner groups: ids are **not** portable across orgs, so config carries a group
*name* that is resolved per target and cached. Repo scope has no runner groups
and ignores it entirely. The JIT mint keeps its 409→delete→retry idempotency
(load-bearing on controller restart).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from husk.ghhttp import GH_API, GitHubError, adopt, request
from husk.slot import Runner
from husk.target import Target

log = logging.getLogger("husk.github")

# Every org has the Default group at id 1; it's the fallback when a configured
# group name doesn't exist on a given target.
DEFAULT_RUNNER_GROUP_ID = 1


class GitHubClient:
    def __init__(
        self,
        *,
        target: Target,
        tokens,  # InstallationTokenProvider (duck-typed to avoid an import cycle)
        labels: list[str],
        runner_group: str = "Default",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.target = target
        self.labels = labels
        self.runner_group = runner_group
        self._tokens = tokens
        self._client = adopt(client)
        self._group_id: int | None = None  # resolved lazily, per target

    async def aclose(self) -> None:
        """Release the connection pool. Called once at daemon shutdown."""
        await self._client.aclose()

    @property
    def base(self) -> str:
        """The target's API base — the ONLY structural difference between an
        org-scoped and a repo-scoped runner pool."""
        if self.target.kind == "org":
            return f"{GH_API}/orgs/{self.target.name}"
        return f"{GH_API}/repos/{self.target.name}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One authenticated call against this target, retried once on 401.

        A 401 is not necessarily an error worth surfacing: installation tokens
        expire hourly and an App can be reinstalled underneath us. Both look like
        a 401 and both are fixed by re-minting."""
        url = f"{self.base}{path}"
        token = await self._tokens.token_for(self.target)
        r = await request(self._client, method, url, token=token, **kwargs)
        if r.status_code == 401:
            log.info("401 on %s %s; re-minting installation token", method, path)
            self._tokens.invalidate(self.target)
            token = await self._tokens.token_for(self.target)
            r = await request(self._client, method, url, token=token, **kwargs)
        return r

    # ------------------------------------------------------------ runner group
    async def group_id(self) -> int | None:
        """Resolve the configured group NAME to an id for this target.

        None for repo scope (no such concept). For org scope, an unknown name
        falls back to Default rather than failing. This matters more as the App
        spreads: huskd serves orgs it does not administer, so a group named in
        *huskd's* config simply may not exist over there. A group is an isolation
        nicety, not a correctness requirement — refusing to mint runners because
        of one would be the wrong trade."""
        if self.target.kind != "org":
            return None
        if self._group_id is not None:
            return self._group_id
        try:
            r = await self._request("GET", "/actions/runner-groups?per_page=100")
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning(
                "could not list runner groups for %s (%s); using Default",
                self.target,
                e,
            )
            self._group_id = DEFAULT_RUNNER_GROUP_ID
            return self._group_id
        groups = r.json().get("runner_groups", [])
        match = next((g for g in groups if g.get("name") == self.runner_group), None)
        if match is None:
            log.warning(
                "runner group %r not found on %s (have: %s); using Default (%d)",
                self.runner_group,
                self.target,
                [g.get("name") for g in groups],
                DEFAULT_RUNNER_GROUP_ID,
            )
            self._group_id = DEFAULT_RUNNER_GROUP_ID
        else:
            self._group_id = match["id"]
            log.info(
                "runner group %r on %s resolved to id %d",
                self.runner_group,
                self.target,
                self._group_id,
            )
        return self._group_id

    # ------------------------------------------------------------------ reads
    async def _list_raw(self) -> list[dict]:
        try:
            r = await self._request("GET", "/actions/runners?per_page=100")
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise GitHubError(f"list runners failed for {self.target}: {e}") from e
        runners = r.json().get("runners", [])
        log.debug(
            "GET runners (%s) -> HTTP %d, %d runner(s): %s",
            self.target,
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
        body: dict[str, Any] = {
            "name": name,
            "labels": self.labels,
            "work_folder": "_work",
        }
        gid = await self.group_id()
        if gid is not None:  # org scope only
            body["runner_group_id"] = gid
        path = "/actions/runners/generate-jitconfig"
        log.debug("POST generate-jitconfig (%s) name=%s", self.target, name)
        try:
            r = await self._request("POST", path, json=body)
            if r.status_code == 409:
                existing = await self.find_runner(name)
                if existing:
                    log.info(
                        "runner %s already exists (%s); deleting and retrying",
                        name,
                        existing.get("status"),
                    )
                    await self.delete_runner(existing["id"])
                r = await self._request("POST", path, json=body)
        except httpx.HTTPError as e:
            raise GitHubError(
                f"generate-jitconfig failed for {self.target}: {e}"
            ) from e
        if r.status_code != 201:
            raise GitHubError(
                f"JIT mint failed for {self.target}: HTTP {r.status_code}: {r.text[:300]}"
            )
        log.debug("minted JIT for runner %s on %s", name, self.target)
        return r.json()["encoded_jit_config"]

    async def delete_runner(self, runner_id: int) -> None:
        try:
            r = await self._request("DELETE", f"/actions/runners/{runner_id}")
        except httpx.HTTPError as e:
            raise GitHubError(f"delete runner {runner_id} failed: {e}") from e
        if r.status_code not in (204, 404):
            raise GitHubError(
                f"delete runner {runner_id}: HTTP {r.status_code}: {r.text[:200]}"
            )
        log.debug(
            "DELETE runner %d (%s) -> HTTP %d", runner_id, self.target, r.status_code
        )

    async def reap_offline(self) -> list[str]:
        """Delete every offline runner — clears leftover/dead JIT registrations."""
        reaped = []
        for x in await self._list_raw():
            if x["status"] == "offline":
                await self.delete_runner(x["id"])
                reaped.append(x["name"])
        return reaped
