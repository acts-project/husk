"""GitHub Actions runner management — lifted from phase3-recycle.py's gh_*
primitives and wrapped in a client class. PAT (Bearer) auth; the JIT mint keeps
its 409→delete→retry idempotency (load-bearing on controller restart)."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, TypeVar

import requests

from husk.slot import Runner

log = logging.getLogger("husk.github")

GH_API = "https://api.github.com"

# Cap every GitHub call so a hung/black-holed request can't wedge a reconcile tick.
# This matters most under multi-pool: pools tick sequentially in one process, so an
# unbounded GitHub call in one pool would stall every other pool's reconcile.
_HTTP_TIMEOUT_S = 30

# Backstop for the one thing _HTTP_TIMEOUT_S can't bound: DNS resolution.
# urllib3 calls the bare `socket.getaddrinfo()` before the requests timeout ever
# applies, and that call has no timeout of its own — it can hang indefinitely,
# most commonly after a laptop sleep/wake or a Wi-Fi/VPN flap wedges the
# resolver. Give the normal connect/read timeout a healthy margin, then abandon
# the call outright so one wedged lookup can't stall the reconcile loop forever.
_DEADLINE_S = _HTTP_TIMEOUT_S + 15

T = TypeVar("T")


def _call_with_deadline(fn: Callable[[], T], *, timeout: float, what: str) -> T:
    """Run fn() on a throwaway daemon thread and enforce a hard wall-clock
    deadline. Python has no way to interrupt a stuck C-level syscall (like a
    wedged DNS lookup) from outside the thread running it, so on timeout we
    abandon the thread rather than wait for it — it dies on its own whenever
    the syscall eventually returns."""
    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            outcome.put((True, fn()))
        except BaseException as exc:  # re-raised on the caller's thread below
            outcome.put((False, exc))

    threading.Thread(target=_run, name=f"github-{what}", daemon=True).start()
    try:
        ok, value = outcome.get(timeout=timeout)
    except queue.Empty:
        raise requests.exceptions.Timeout(
            f"{what} exceeded {timeout:.0f}s deadline (DNS/connect hang?)"
        ) from None
    if not ok:
        raise value
    return value


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

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Single choke point for outbound calls: every request gets the same
        connect/read timeout plus the DNS-hang deadline backstop."""
        kwargs.setdefault("timeout", _HTTP_TIMEOUT_S)
        return _call_with_deadline(
            lambda: self._s.request(method, url, **kwargs),
            timeout=_DEADLINE_S,
            what=f"{method} {url}",
        )

    # ------------------------------------------------------------------ reads
    def _list_raw(self) -> list[dict]:
        try:
            r = self._request(
                "GET", f"{GH_API}/repos/{self.repo}/actions/runners?per_page=100"
            )
            r.raise_for_status()
        except requests.RequestException as e:
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
        log.debug("POST generate-jitconfig name=%s labels=%s", name, self.labels)
        try:
            r = self._request("POST", url, json=body)
            if r.status_code == 409:
                existing = self.find_runner(name)
                if existing:
                    log.info(
                        "runner %s already exists (%s); deleting and retrying",
                        name,
                        existing.get("status"),
                    )
                    self.delete_runner(existing["id"])
                r = self._request("POST", url, json=body)
        except requests.RequestException as e:
            raise GitHubError(f"generate-jitconfig failed: {e}") from e
        if r.status_code != 201:
            raise GitHubError(f"JIT mint failed: HTTP {r.status_code}: {r.text[:300]}")
        log.debug("minted JIT for runner %s", name)
        return r.json()["encoded_jit_config"]

    def delete_runner(self, runner_id: int) -> None:
        try:
            r = self._request(
                "DELETE", f"{GH_API}/repos/{self.repo}/actions/runners/{runner_id}"
            )
        except requests.RequestException as e:
            raise GitHubError(f"delete runner {runner_id} failed: {e}") from e
        if r.status_code not in (204, 404):
            raise GitHubError(
                f"delete runner {runner_id}: HTTP {r.status_code}: {r.text[:200]}"
            )
        log.debug("DELETE runner %d -> HTTP %d", runner_id, r.status_code)

    def reap_offline(self) -> list[str]:
        """Delete every offline runner — clears leftover/dead JIT registrations."""
        reaped = []
        for x in self._list_raw():
            if x["status"] == "offline":
                self.delete_runner(x["id"])
                reaped.append(x["name"])
        return reaped
