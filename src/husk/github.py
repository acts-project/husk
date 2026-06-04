"""GitHub Actions runner management — lifted from phase3-recycle.py's gh_*
primitives and wrapped in a client class. PAT (Bearer) auth; the JIT mint keeps
its 409→delete→retry idempotency (load-bearing on controller restart)."""

from __future__ import annotations

import logging

import requests

from husk.slot import Runner

log = logging.getLogger("husk.github")

GH_API = "https://api.github.com"


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
        session: requests.Session | None = None,
    ) -> None:
        self.repo = repo
        self.labels = labels
        self.runner_group_id = runner_group_id
        self._s = session or requests.Session()
        self._s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    # ------------------------------------------------------------------ reads
    def _list_raw(self) -> list[dict]:
        try:
            r = self._s.get(f"{GH_API}/repos/{self.repo}/actions/runners?per_page=100")
            r.raise_for_status()
        except requests.RequestException as e:
            raise GitHubError(f"list runners failed: {e}") from e
        return r.json().get("runners", [])

    def list_runners(self) -> list[Runner]:
        return [
            Runner(id=x["id"], name=x["name"], status=x["status"], busy=bool(x["busy"]))
            for x in self._list_raw()
        ]

    def find_runner(self, name: str) -> dict | None:
        for x in self._list_raw():
            if x["name"] == name:
                return x
        return None

    # ----------------------------------------------------------------- writes
    def generate_jitconfig(self, name: str) -> str:
        """Mint a single-use JIT config. Idempotent: a lingering same-name
        registration (interrupted run / restart) is deleted and the mint retried."""
        body = {
            "name": name,
            "runner_group_id": self.runner_group_id,
            "labels": self.labels,
            "work_folder": "_work",
        }
        url = f"{GH_API}/repos/{self.repo}/actions/runners/generate-jitconfig"
        r = self._s.post(url, json=body)
        if r.status_code == 409:
            existing = self.find_runner(name)
            if existing:
                log.info("runner %s already exists (%s); deleting and retrying",
                         name, existing.get("status"))
                self.delete_runner(existing["id"])
            r = self._s.post(url, json=body)
        if r.status_code != 201:
            raise GitHubError(f"JIT mint failed: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["encoded_jit_config"]

    def delete_runner(self, runner_id: int) -> None:
        try:
            r = self._s.delete(f"{GH_API}/repos/{self.repo}/actions/runners/{runner_id}")
        except requests.RequestException as e:
            raise GitHubError(f"delete runner {runner_id} failed: {e}") from e
        if r.status_code not in (204, 404):
            raise GitHubError(f"delete runner {runner_id}: HTTP {r.status_code}: {r.text[:200]}")

    def reap_offline(self) -> list[str]:
        """Delete every offline runner — clears leftover/dead JIT registrations."""
        reaped = []
        for x in self._list_raw():
            if x["status"] == "offline":
                self.delete_runner(x["id"])
                reaped.append(x["name"])
        return reaped
