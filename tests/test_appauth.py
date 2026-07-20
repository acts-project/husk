"""InstallationTokenProvider: JWT signing, installation lookup, token caching.

The token cache is the load-bearing part — huskd holds no long-lived credential,
so every GitHub call depends on this refreshing correctly and not stampeding the
mint endpoint when N pools share a target."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from husk.appauth import AppAuthError, InstallationTokenProvider, sign_app_jwt
from husk.target import Target

ORG = Target.org("acts-project")
REPO = Target.repo("paulgessinger/husk-test")

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def _run(coro):
    return asyncio.run(coro)


def _b64d(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


INSTALLS = [
    {"id": 11, "account": {"login": "acts-project", "type": "Organization"}},
    {"id": 22, "account": {"login": "paulgessinger", "type": "User"}},
]


def _provider(handler, *, clock=None):
    return InstallationTokenProvider(
        123456,
        PEM,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=clock or Clock(),
    )


def _routes(*, expires="2033-01-01T00:00:00Z", counter=None, installs=INSTALLS):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations":
            if counter is not None:
                counter["installs"] = counter.get("installs", 0) + 1
            return httpx.Response(200, json=installs)
        if request.url.path.endswith("/access_tokens"):
            if counter is not None:
                counter["mints"] = counter.get("mints", 0) + 1
            iid = request.url.path.split("/")[3]
            return httpx.Response(
                201,
                json={
                    "token": f"ghs_{iid}_{counter['mints'] if counter else 1}",
                    "expires_at": expires,
                },
            )
        return httpx.Response(404, json={})

    return handler


# ---------------------------------------------------------------------- JWT
def test_jwt_claims_and_signature():
    clock = Clock()
    tok = sign_app_jwt(123456, PEM, now=clock())
    header, payload, sig = tok.split(".")
    assert json.loads(_b64d(header))["alg"] == "RS256"
    claims = json.loads(_b64d(payload))
    assert claims["iss"] == "123456"
    assert claims["iat"] < clock()  # backdated for clock skew
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's 10-minute ceiling
    _KEY.public_key().verify(
        _b64d(sig), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_jwt_is_reused_then_resigned_near_expiry():
    clock = Clock()
    p = _provider(_routes(), clock=clock)
    first = p.app_jwt()
    assert p.app_jwt() is first  # cached, not re-signed every call
    clock.advance(600)
    assert p.app_jwt() != first  # past its life → fresh assertion


def test_bad_private_key_is_reported_clearly():
    with pytest.raises(AppAuthError, match="could not load App private key"):
        sign_app_jwt(1, "not a pem", now=0.0)


# ------------------------------------------------------------- installations
def test_resolves_org_and_repo_targets_to_installations():
    p = _provider(_routes())
    assert _run(p.installation_id(ORG)) == 11
    # A repo target resolves through its OWNER — an App installs on an account.
    assert _run(p.installation_id(REPO)) == 22


def test_installation_lookup_is_case_insensitive():
    p = _provider(_routes())
    assert _run(p.installation_id(Target.org("ACTS-Project"))) == 11


def test_missing_installation_names_the_account():
    p = _provider(_routes())
    with pytest.raises(AppAuthError, match="no installation of this App on 'nobody'"):
        _run(p.installation_id(Target.org("nobody")))


def test_installations_are_cached_then_refreshed():
    counter: dict = {}
    clock = Clock()
    p = _provider(_routes(counter=counter), clock=clock)
    _run(p.token_for(ORG))
    _run(p.installations())
    assert counter["installs"] == 1  # cached
    clock.advance(1000)  # past the TTL
    _run(p.installations())
    assert counter["installs"] == 2


# -------------------------------------------------------------------- token
def test_token_is_cached_across_calls():
    counter: dict = {}
    p = _provider(_routes(counter=counter))
    a = _run(p.token_for(ORG))
    b = _run(p.token_for(ORG))
    assert a == b and counter["mints"] == 1  # N pools sharing a target share a mint


def test_token_refreshes_before_expiry():
    counter: dict = {}
    clock = Clock()
    # Expiry ~1h out in the clock's frame.
    from datetime import datetime, timezone

    expires = (
        datetime.fromtimestamp(clock() + 3600, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    p = _provider(_routes(expires=expires, counter=counter), clock=clock)
    _run(p.token_for(ORG))
    clock.advance(3000)  # still inside the 5-minute margin
    _run(p.token_for(ORG))
    assert counter["mints"] == 1
    clock.advance(500)  # now within the margin of expiry
    _run(p.token_for(ORG))
    assert counter["mints"] == 2


def test_separate_targets_get_separate_tokens():
    counter: dict = {}
    p = _provider(_routes(counter=counter))
    assert _run(p.token_for(ORG)) != _run(p.token_for(REPO))
    assert counter["mints"] == 2


def test_invalidate_forces_a_fresh_mint():
    counter: dict = {}
    p = _provider(_routes(counter=counter))
    _run(p.token_for(ORG))
    p.invalidate(ORG)
    _run(p.token_for(ORG))
    assert counter["mints"] == 2


def test_unparseable_expiry_is_treated_as_stale():
    # Better an extra mint than handing out a token we can't reason about.
    counter: dict = {}
    p = _provider(_routes(expires="not-a-date", counter=counter))
    _run(p.token_for(ORG))
    _run(p.token_for(ORG))
    assert counter["mints"] == 2


def test_mint_failure_surfaces_as_appautherror():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=INSTALLS)
        return httpx.Response(403, text="suspended")

    with pytest.raises(AppAuthError, match="token exchange"):
        _run(_provider(handler).token_for(ORG))


def test_listing_failure_surfaces_as_appautherror():
    with pytest.raises(AppAuthError, match="list installations failed"):
        _run(_provider(lambda r: httpx.Response(500, text="oops")).token_for(ORG))
