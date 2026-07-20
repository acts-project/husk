"""Target discovery — installations ∩ huskd's allowlist.

The App is installable by *anyone* (GitHub offers no way to restrict a public App
to a set of accounts), so the restriction lives here: huskd holds the private key
and runs the VMs, and an install it does not recognize simply gets no runners.

Two lists, and the entry's *type* decides the runner scope:

* ``allowed_orgs  = ["acts-project"]``            → org-level runners for the org.
* ``allowed_repos = ["paulgessinger/husk-test"]`` → repo-level runners for exactly
  that repo, and nothing else that owner owns.

Repo entries are checked against what the installation actually granted
(``GET /installation/repositories``), so **both** sides must agree: the installer
chose the repo *and* huskd's operator listed it. Either alone is not enough.

Non-allowlisted installs are ignored, not declined. Declining
(``DELETE /app/installations/{id}``) is destructive and irreversible from
huskd's side — someone experimenting with a public App should not have their
install silently deleted by a daemon they have never heard of.

Failure policy mirrors the runner poller's: discovery reports whether the sweep
was **complete**. A partial sweep (one installation's repo listing failed) may
still add targets, but the supervisor must not treat a target's absence from a
partial result as removal — otherwise a transient GitHub 500 would tear down
live runners.
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
    """Target discovery could not be performed at all."""


@dataclass(frozen=True)
class Allowlist:
    """Which accounts/repos huskd is willing to serve.

    Entries keep the operator's spelling (it feeds pool names and `vm_prefix`),
    but all *matching* is case-insensitive, because GitHub logins are."""

    orgs: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for o in self.orgs:
            if "/" in o:
                raise ValueError(
                    f"allowed_orgs entry {o!r} looks like a repo — put owner/name "
                    "in allowed_repos instead"
                )
        for r in self.repos:
            if r.count("/") != 1 or not all(r.split("/")):
                raise ValueError(f"allowed_repos entry {r!r} must be owner/name")

    def __len__(self) -> int:
        return len(self.orgs) + len(self.repos)

    def org_for(self, login: str) -> str | None:
        """The configured spelling of `login` if the org is allowed, else None."""
        low = login.lower()
        return next((o for o in self.orgs if o.lower() == low), None)

    def repos_under(self, login: str) -> dict[str, str]:
        """`{lowercased owner/name: configured spelling}` for allowed repos owned
        by `login`. Empty means this install needs no repo listing at all."""
        low = login.lower()
        return {r.lower(): r for r in self.repos if r.split("/", 1)[0].lower() == low}


@dataclass(frozen=True)
class Discovery:
    """One discovery sweep. `complete` is False when at least one installation
    could not be read, which makes absence unreliable (see module docstring)."""

    targets: tuple[Target, ...]
    complete: bool


class TargetDiscovery:
    """Turns the App's installations into the set of targets huskd should serve."""

    def __init__(self, tokens, allowlist: Allowlist, *, client=None) -> None:
        self._tokens = tokens
        self._allow = allowlist
        self._client = adopt(client)
        self._ignored: set[str] = set()  # logins already logged, to avoid churn

    async def aclose(self) -> None:
        await self._client.aclose()

    async def discover(self) -> Discovery:
        """Sweep every installation and emit the allowed targets.

        Raises `DiscoveryError` only if the installation list itself is
        unreadable; a single unreadable installation degrades to `complete=False`.
        """
        try:
            installs = await self._tokens.installations(refresh=True)
        except Exception as e:
            raise DiscoveryError(f"could not list installations: {e}") from e

        targets: list[Target] = []
        complete = True
        for inst in installs:
            login = (inst.get("account") or {}).get("login") or ""
            iid = inst.get("id")
            if not login or iid is None:
                continue

            org = self._allow.org_for(login)
            if org is not None:
                targets.append(Target.org(org))

            wanted = self._allow.repos_under(login)
            if wanted:
                try:
                    granted = await self._granted_repos(iid)
                except Exception:
                    # Keep the org target (already emitted) but flag the sweep, so
                    # the supervisor won't read this install's missing repos as a
                    # removal.
                    log.warning(
                        "could not list repositories for installation %s (%s); "
                        "treating this sweep as partial",
                        iid,
                        login,
                        exc_info=True,
                    )
                    complete = False
                    continue
                for full in granted:
                    configured = wanted.get(full.lower())
                    if configured is not None:
                        targets.append(Target.repo(configured))

            if org is None and not wanted and login.lower() not in self._ignored:
                self._ignored.add(login.lower())
                log.info(
                    "ignoring installation on %r: not in allowed_orgs/allowed_repos",
                    login,
                )

        # De-dup while keeping order stable, so logs and pool ordering don't churn.
        seen: set[str] = set()
        unique_list: list[Target] = []
        for t in targets:
            if t.key not in seen:
                seen.add(t.key)
                unique_list.append(t)
        unique = tuple(unique_list)
        log.debug(
            "discovery: %d installation(s) -> %d target(s)%s",
            len(installs),
            len(unique),
            "" if complete else " (partial)",
        )
        return Discovery(targets=unique, complete=complete)

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


async def discover_targets(tokens, allowlist: Allowlist) -> list[Target]:
    """One-shot discovery for the `huskctl` paths, which have no daemon loop.

    A partial sweep is fine here: these commands act on what they can see and
    say so, rather than refusing to run."""
    d = TargetDiscovery(tokens, allowlist)
    try:
        result = await d.discover()
    finally:
        await d.aclose()
    if not result.complete:
        log.warning("discovery was partial; some targets may be missing")
    return list(result.targets)


__all__ = [
    "Allowlist",
    "Discovery",
    "DiscoveryError",
    "TargetDiscovery",
    "discover_targets",
]
