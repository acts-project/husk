"""Target availability — which configured targets the App can actually serve.

Each `[[pool]]` names the one target it serves. This module answers the only
question that remains open at runtime: *is the App actually installed there, and
did that installation grant us the repo?*

- **org target** — served if the App is installed on that org.
- **repo target** — served if the App is installed on the owner **and** that
  installation granted this specific repo (`GET /installation/repositories`).
  Both must hold: the installer chose the repo, and huskd's operator configured
  it. Either alone is not enough.

A target that is configured but not installed is simply not served, and its pool
drains. huskd never declines an installation
(`DELETE /app/installations/{id}`) — that is destructive and irreversible from
huskd's side, and an install it doesn't recognize already gets no runners.

Failure policy mirrors the runner poller's: a sweep reports whether it was
**complete**. A partial sweep (some installation's repo listing failed) may still
mark targets available, but the supervisor must not treat absence from an
incomplete result as "no longer installed" — otherwise a transient GitHub 500
would drain live runners.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from husk.ghhttp import GH_API, GitHubError, adopt, request
from husk.target import Target

log = logging.getLogger("husk.discovery")

_PER_PAGE = 100
_MAX_PAGES = 50


class DiscoveryError(GitHubError):
    """Target availability could not be determined at all."""


@dataclass(frozen=True)
class Discovery:
    """One sweep. `complete` is False when at least one installation could not be
    read, which makes absence unreliable (see module docstring)."""

    targets: tuple[Target, ...]  # configured targets confirmed available
    complete: bool


class TargetDiscovery:
    """Checks the configured targets against the App's live installations."""

    def __init__(self, tokens, targets, *, client=None) -> None:
        # De-duplicated: several pools commonly serve one target (a gpu and a cpu
        # pool for the same org), and it should cost one check, not two.
        self._targets: tuple[Target, ...] = tuple(dict.fromkeys(targets))
        self._tokens = tokens
        self._client = adopt(client)
        self._missing: set[str] = set()  # already-logged, to avoid per-sweep churn

    async def aclose(self) -> None:
        await self._client.aclose()

    async def discover(self) -> Discovery:
        """Which configured targets are currently servable.

        Raises `DiscoveryError` only if the installation list itself is
        unreadable; one unreadable installation degrades to `complete=False`."""
        try:
            installs = await self._tokens.installations(refresh=True)
        except Exception as e:
            raise DiscoveryError(f"could not list installations: {e}") from e

        # login (lowercased) -> installation id
        by_login = {
            (inst.get("account") or {}).get("login", "").lower(): inst["id"]
            for inst in installs
            if (inst.get("account") or {}).get("login") and inst.get("id") is not None
        }

        available: list[Target] = []
        complete = True
        granted_cache: dict[int, set[str]] = {}

        for target in self._targets:
            owner = (
                target.name.split("/", 1)[0] if target.kind == "repo" else target.name
            )
            iid = by_login.get(owner.lower())
            if iid is None:
                self._log_missing(target, f"the App is not installed on {owner!r}")
                continue

            if target.kind == "org":
                available.append(target)
                self._missing.discard(target.key)
                continue

            # Repo target: the install must also have granted this repo.
            if iid not in granted_cache:
                try:
                    granted_cache[iid] = {
                        n.lower() for n in await self._granted_repos(iid)
                    }
                except Exception:
                    log.warning(
                        "could not list repositories for installation %s (%s); "
                        "treating this sweep as partial",
                        iid,
                        owner,
                        exc_info=True,
                    )
                    complete = False
                    continue
            if target.name.lower() in granted_cache[iid]:
                available.append(target)
                self._missing.discard(target.key)
            else:
                self._log_missing(
                    target,
                    f"the App is installed on {owner!r} but that installation does "
                    "not grant this repo",
                )

        log.debug(
            "availability: %d/%d configured target(s)%s",
            len(available),
            len(self._targets),
            "" if complete else " (partial sweep)",
        )
        return Discovery(targets=tuple(available), complete=complete)

    def _log_missing(self, target: Target, why: str) -> None:
        """Say it once per transition, not once per sweep."""
        if target.key not in self._missing:
            self._missing.add(target.key)
            log.warning("target %s is not servable: %s", target, why)

    async def _granted_repos(self, installation_id: int) -> list[str]:
        """Every repo full-name this installation granted, following pagination."""
        token = await self._tokens.token_for_installation(installation_id)
        out: list[str] = []
        for page in range(1, _MAX_PAGES + 1):
            r = await request(
                self._client,
                "GET",
                f"{GH_API}/installation/repositories",
                token=token,
                params={"per_page": _PER_PAGE, "page": page},
            )
            r.raise_for_status()
            batch = (r.json() or {}).get("repositories") or []
            out += [b["full_name"] for b in batch if b.get("full_name")]
            if len(batch) < _PER_PAGE:
                break
        return out


async def discover_targets(tokens, targets) -> list[Target]:
    """One-shot availability check for the `huskctl` paths, which have no daemon
    loop. A partial sweep is fine here: these commands act on what they can see
    and say so, rather than refusing to run."""
    d = TargetDiscovery(tokens, targets)
    try:
        result = await d.discover()
    finally:
        await d.aclose()
    if not result.complete:
        log.warning("availability sweep was partial; some targets may be missing")
    return list(result.targets)


__all__ = ["Discovery", "DiscoveryError", "TargetDiscovery", "discover_targets"]
