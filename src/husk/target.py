"""Target — the *place* runners are provisioned for.

Phase 0 of the GitHub-App migration re-keys the reconcile unit from `pool` to
`(target, pool)`. A *pool* is a runner type (labels + backend); a *target* is a
place to put runners. Today exactly one static target exists per pool, derived
from the configured ``owner/repo`` (cardinality 1). The App migration later makes
the target set dynamic — org-scoped (``org:<login>``) or repo-scoped
(``repo:<owner/name>``) — without this type or its consumers changing.

The value is a small frozen key so it can index the demand registry and, later,
per-target reconcile tasks. `key` is the canonical serialization
(``org:acts-project`` / ``repo:paulgessinger/husk-test``); `parse` is its inverse.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    kind: str  # "org" | "repo"
    name: str  # org login, or "owner/name" for a repo

    def __post_init__(self) -> None:
        if self.kind not in ("org", "repo"):
            raise ValueError(f"unknown target kind: {self.kind!r} (want org|repo)")
        if not self.name:
            raise ValueError("target name must be non-empty")
        if self.kind == "repo" and "/" not in self.name:
            raise ValueError(f"repo target needs owner/name, got {self.name!r}")

    @classmethod
    def org(cls, login: str) -> "Target":
        return cls("org", login)

    @classmethod
    def repo(cls, owner_repo: str) -> "Target":
        return cls("repo", owner_repo)

    @classmethod
    def parse(cls, key: str) -> "Target":
        """Inverse of `key`: ``org:acts-project`` / ``repo:owner/name`` → Target.
        A repo name legitimately contains no extra colon, so split only once."""
        kind, sep, name = key.partition(":")
        if not sep:
            raise ValueError(f"malformed target key: {key!r} (want kind:name)")
        return cls(kind, name)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    def __str__(self) -> str:
        return self.key
