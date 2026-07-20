"""GitHub App authentication — JWT → per-installation access tokens.

huskd holds no long-lived credential. It signs an RS256 JWT with the App's
private key, uses that to discover installations, and exchanges it for an
*installation token* scoped to one account. Those tokens last an hour, so they
are cached per installation and refreshed ahead of expiry (or immediately on a
401, which is how a revoked/rotated install shows up).

The seam that matters for later phases: everything is keyed by `Target`. Phase 2
resolves a *configured* target to its installation; Phase 3 inverts the same
`installations()` call to *derive* the target set from installs ∩ allowlist.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from husk.ghhttp import GH_API, GitHubError, adopt, request
from husk.target import Target

log = logging.getLogger("husk.appauth")

# App JWTs may live at most 10 minutes. Sign for 9 and re-sign a minute early, so
# a slow request can never carry an already-expired assertion.
_JWT_TTL_S = 540
_JWT_SKEW_S = 60

# Installation tokens last ~60 min; renew with 5 min to spare.
_TOKEN_MARGIN_S = 300

# How long a resolved installation list is trusted. Installs change rarely, but a
# reinstall mints a NEW installation id, so this can't be cached forever.
_INSTALLATIONS_TTL_S = 300


class AppAuthError(GitHubError):
    """The App could not authenticate, or has no installation for a target."""


def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def sign_app_jwt(app_id: int, private_key_pem: str, *, now: float) -> str:
    """RS256 App JWT. `iat` is backdated for clock skew (GitHub rejects a future
    `iat` outright) and `exp` kept under GitHub's 10-minute ceiling."""
    try:
        key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None
        )
    except Exception as e:
        raise AppAuthError(f"could not load App private key: {e}") from e
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": int(now) - _JWT_SKEW_S,
        "exp": int(now) + _JWT_TTL_S,
        "iss": str(app_id),
    }
    signing_input = b".".join(
        _b64url(json.dumps(p, separators=(",", ":")).encode())
        for p in (header, payload)
    )
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + _b64url(sig)).decode()


def _expiry_epoch(iso: str | None) -> float:
    """Parse GitHub's `expires_at` (RFC3339 'Z'). An unparseable value is treated
    as immediately stale rather than trusted — better an extra mint than a 401."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class InstallationTokenProvider:
    """Mints and caches installation tokens, keyed by `Target`."""

    def __init__(
        self,
        app_id: int,
        private_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        clock=time.time,
    ) -> None:
        self._app_id = app_id
        self._key = private_key
        self._client = adopt(client)
        self._clock = clock
        self._jwt: tuple[str, float] | None = None  # (jwt, expiry epoch)
        self._installs: tuple[list[dict], float] | None = None  # (list, fetched epoch)
        self._install_ids: dict[str, int] = {}  # target.key -> installation id
        self._tokens: dict[int, tuple[str, float]] = {}  # id -> (token, expiry)
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -------------------------------------------------------------------- jwt
    def app_jwt(self) -> str:
        """The current App JWT, re-signed when it is close to expiring."""
        now = self._clock()
        if self._jwt is None or now >= self._jwt[1] - _JWT_SKEW_S:
            token = sign_app_jwt(self._app_id, self._key, now=now)
            self._jwt = (token, now + _JWT_TTL_S)
            log.debug("signed a fresh App JWT for app_id=%s", self._app_id)
        return self._jwt[0]

    # ---------------------------------------------------------- installations
    async def installations(self, *, refresh: bool = False) -> list[dict]:
        """Every installation of this App. Cached briefly; a reinstall changes the
        installation id, so this is refreshed rather than memoized forever."""
        now = self._clock()
        if (
            not refresh
            and self._installs is not None
            and now - self._installs[1] < _INSTALLATIONS_TTL_S
        ):
            return self._installs[0]
        try:
            r = await request(
                self._client, "GET", f"{GH_API}/app/installations", token=self.app_jwt()
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise AppAuthError(f"list installations failed: {e}") from e
        installs = r.json()
        self._installs = (installs, now)
        log.debug(
            "app has %d installation(s): %s",
            len(installs),
            [i.get("account", {}).get("login") for i in installs],
        )
        return installs

    async def installation_id(self, target: Target) -> int:
        """The installation serving `target`.

        Both target kinds resolve through the *account* that owns them — an org
        login, or the owner half of `owner/name` — since an App is installed on an
        account, not on a repo. Whether that install actually granted the specific
        repo is a separate check, and belongs to the Phase 3 allowlist."""
        cached = self._install_ids.get(target.key)
        if cached is not None:
            return cached
        owner = target.name.split("/", 1)[0] if target.kind == "repo" else target.name
        for refresh in (False, True):  # a miss may just mean a stale install list
            for inst in await self.installations(refresh=refresh):
                login = inst.get("account", {}).get("login") or ""
                if login.lower() == owner.lower():
                    self._install_ids[target.key] = inst["id"]
                    return inst["id"]
        raise AppAuthError(
            f"no installation of this App on {owner!r} (needed for target {target}) — "
            "install the App on that account, or drop the target from config"
        )

    # ------------------------------------------------------------------ token
    async def token_for(self, target: Target) -> str:
        """A valid installation token for `target`, minting/refreshing as needed.

        Serialized on one lock so N pools sharing a target can't stampede the
        mint endpoint when a token expires."""
        async with self._lock:
            iid = await self.installation_id(target)
            cached = self._tokens.get(iid)
            if cached is not None and self._clock() < cached[1] - _TOKEN_MARGIN_S:
                return cached[0]
            try:
                r = await request(
                    self._client,
                    "POST",
                    f"{GH_API}/app/installations/{iid}/access_tokens",
                    token=self.app_jwt(),
                )
            except httpx.HTTPError as e:
                raise AppAuthError(f"token exchange for {target} failed: {e}") from e
            if r.status_code != 201:
                raise AppAuthError(
                    f"token exchange for {target}: HTTP {r.status_code}: {r.text[:200]}"
                )
            body = r.json()
            token, expiry = body["token"], _expiry_epoch(body.get("expires_at"))
            self._tokens[iid] = (token, expiry)
            log.info(
                "minted installation token for %s (expires %s)",
                target,
                body.get("expires_at"),
            )
            return token

    def invalidate(self, target: Target) -> None:
        """Drop the cached token for `target` — call on a 401 so the next request
        re-mints. Also forgets the installation id, since a 401 can mean the App
        was reinstalled (new id) rather than merely token expiry."""
        iid = self._install_ids.pop(target.key, None)
        if iid is not None:
            self._tokens.pop(iid, None)
        self._installs = None
        log.info("invalidated cached credentials for %s", target)
