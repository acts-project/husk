#!/usr/bin/env python
"""Credential + permission smoke for the husk GitHub App — no husk App code needed.

Validates, against the real API, everything Phases 2-3 will depend on:

  App JWT (RS256)  →  GET /app                      (App ID + PEM are valid)
                   →  GET /app/installations        (who installed it)
  per installation →  POST .../access_tokens        (installation token exchange)
                   →  GET /installation/repositories (granted repo set)
    org account    →  GET /orgs/{org}/actions/runners
                   →  GET /orgs/{org}/actions/runner-groups   (name → id, Phase 2)
    each repo      →  GET /repos/{owner}/{repo}/actions/runners

Read-only by default. `--mint` additionally mints a real JIT config per scope and
immediately deletes the registration — the only way to prove *write* access,
since listing runners needs just `administration:read`.

Run:
    HUSK_APP_ID=123456 HUSK_APP_KEY=/path/husk-app.pem \\
        uv run python scripts/smoke_github_app.py [--mint] [--group NAME] \\
            [--repo-targets owner/repo,owner/other]

Mint scope follows the design, not raw capability: an Organization install is
served by ORG-level runners, so repo-level JIT write is not checked there (that
would invite `Administration: write` across the whole org). A personal install
is checked at repo level, as are any repos named in --repo-targets — i.e. the
ones you plan to put in `allowed_repos`.

A 403 is the interesting failure: the hint printed alongside names the permission
that is probably missing. Note the scope asymmetry — org runner management has a
dedicated `Organization > Self-hosted runners: write`, while repo-level rides on
the much broader `Repository > Administration: write`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

GH = "https://api.github.com"
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# What a 403 on each endpoint most likely means, in App-settings terms.
HINTS = {
    "org-runners": "Organization permissions > Self-hosted runners: read/write",
    "org-groups": "Organization permissions > Self-hosted runners: read/write",
    "org-mint": "Organization permissions > Self-hosted runners: WRITE",
    "repo-runners": "Repository permissions > Administration: read/write",
    "repo-mint": "Repository permissions > Administration: WRITE",
    "repos": "Repository permissions > Metadata: read",
}


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "", hint: str = "") -> bool:
        print(
            f"  {'PASS' if ok else 'FAIL'}  {label}{'  — ' + detail if detail else ''}"
        )
        if not ok:
            self.failures.append(label)
            if hint:
                print(f"        ↳ likely missing: {hint}")
        return ok


def b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def app_jwt(app_id: str, pem_path: str) -> str:
    """RS256 App JWT — the same signing Phase 2's InstallationTokenProvider needs.

    `iat` is backdated 60s for clock skew and `exp` kept under GitHub's 10-minute
    ceiling."""
    with open(pem_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    now = int(time.time())
    segments = [
        b64url(
            json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode()
        ),
        b64url(
            json.dumps(
                {"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
                separators=(",", ":"),
            ).encode()
        ),
    ]
    signing_input = b".".join(segments)
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + b64url(sig)).decode()


def hdrs(token: str) -> dict:
    return {**API_HEADERS, "Authorization": f"Bearer {token}"}


async def probe(
    client: httpx.AsyncClient, rep: Report, label: str, url: str, token: str, hint: str
):
    """GET `url`, report, and return the decoded body (or None on failure)."""
    r = await client.get(url, headers=hdrs(token))
    ok = r.status_code == 200
    rep.check(label, ok, f"HTTP {r.status_code}", hint if r.status_code == 403 else "")
    return r.json() if ok else None


async def mint_and_delete(
    client: httpx.AsyncClient,
    rep: Report,
    scope_label: str,
    base: str,
    token: str,
    hint: str,
    *,
    group_id: int | None,
) -> None:
    """Mint a throwaway JIT registration then delete it — the write-access proof."""
    body: dict = {
        "name": f"husk-smoke-{int(time.time())}",
        "labels": ["husk-smoke"],
        "work_folder": "_work",
    }
    if group_id is not None:  # org scope requires a runner group
        body["runner_group_id"] = group_id
    r = await client.post(
        f"{base}/actions/runners/generate-jitconfig", headers=hdrs(token), json=body
    )
    if not rep.check(
        f"{scope_label}: JIT mint (write)",
        r.status_code == 201,
        f"HTTP {r.status_code} {r.text[:120] if r.status_code != 201 else ''}".strip(),
        hint if r.status_code == 403 else "",
    ):
        return
    runner_id = r.json().get("runner", {}).get("id")
    d = await client.delete(f"{base}/actions/runners/{runner_id}", headers=hdrs(token))
    rep.check(
        f"{scope_label}: delete test runner {runner_id}",
        d.status_code in (204, 404),
        f"HTTP {d.status_code}",
    )


async def main() -> int:
    app_id = os.environ.get("HUSK_APP_ID")
    pem = os.environ.get("HUSK_APP_KEY")
    do_mint = "--mint" in sys.argv
    group_name = None
    if "--group" in sys.argv:
        group_name = sys.argv[sys.argv.index("--group") + 1]
    # Repos you intend to list in `allowed_repos` — these get the repo-LEVEL
    # checks even when they live inside an org that is otherwise org-served.
    repo_targets: set[str] = set()
    if "--repo-targets" in sys.argv:
        repo_targets = {
            s.strip()
            for s in sys.argv[sys.argv.index("--repo-targets") + 1].split(",")
            if s.strip()
        }

    if not app_id or not pem:
        print("set HUSK_APP_ID and HUSK_APP_KEY (path to the App's private-key PEM)")
        return 2
    if not os.path.exists(pem):
        print(f"private key not found: {pem}")
        return 2

    rep = Report()
    print(f"App {app_id}  key={pem}  mint={'yes' if do_mint else 'no (read-only)'}\n")

    try:
        jwt = app_jwt(app_id, pem)
    except Exception as e:
        print(f"  FAIL  sign App JWT — {e}")
        return 1

    async with httpx.AsyncClient(timeout=30) as client:
        print("-- app identity --")
        r = await client.get(f"{GH}/app", headers=hdrs(jwt))
        if not rep.check(
            "GET /app (App ID + PEM valid)",
            r.status_code == 200,
            f"HTTP {r.status_code}",
            "a 401 here means the PEM doesn't match this App ID",
        ):
            return 1
        app = r.json()
        print(
            f"        app: {app.get('slug')!r} owner={app.get('owner', {}).get('login')}"
        )

        r = await client.get(f"{GH}/app/installations", headers=hdrs(jwt))
        if not rep.check(
            "GET /app/installations", r.status_code == 200, f"HTTP {r.status_code}"
        ):
            return 1
        installs = r.json()
        rep.check("at least one installation", bool(installs), f"{len(installs)} found")

        for inst in installs:
            acct = inst.get("account", {})
            login, kind, iid = acct.get("login"), acct.get("type"), inst["id"]
            print(f"\n-- installation {iid}: {login} ({kind}) --")

            t = await client.post(
                f"{GH}/app/installations/{iid}/access_tokens", headers=hdrs(jwt)
            )
            if not rep.check(
                "installation token exchange",
                t.status_code == 201,
                f"HTTP {t.status_code}",
            ):
                continue
            tok = t.json()["token"]
            print(f"        token expires {t.json().get('expires_at')}")

            repos_body = await probe(
                client,
                rep,
                "GET /installation/repositories",
                f"{GH}/installation/repositories",
                tok,
                HINTS["repos"],
            )
            repos = [x["full_name"] for x in (repos_body or {}).get("repositories", [])]
            print(f"        granted repos ({len(repos)}): {repos[:8]}")

            group_id = None
            if kind == "Organization":
                await probe(
                    client,
                    rep,
                    f"org runners: GET /orgs/{login}/actions/runners",
                    f"{GH}/orgs/{login}/actions/runners",
                    tok,
                    HINTS["org-runners"],
                )
                groups = await probe(
                    client,
                    rep,
                    f"org runner-groups: GET /orgs/{login}/actions/runner-groups",
                    f"{GH}/orgs/{login}/actions/runner-groups",
                    tok,
                    HINTS["org-groups"],
                )
                if groups:
                    found = groups.get("runner_groups", [])
                    print(
                        "        groups: "
                        + ", ".join(f"{g['name']}(id={g['id']})" for g in found)
                    )
                    # Phase 2 resolves a group NAME to an id per target; prove it here.
                    want = group_name or "Default"
                    match = next((g for g in found if g["name"] == want), None)
                    rep.check(
                        f"resolve runner group {want!r} → id",
                        match is not None,
                        f"id={match['id']}"
                        if match
                        else "not found (will fall back to 1)",
                    )
                    group_id = match["id"] if match else 1
                if do_mint:
                    await mint_and_delete(
                        client,
                        rep,
                        f"org {login}",
                        f"{GH}/orgs/{login}",
                        tok,
                        HINTS["org-mint"],
                        group_id=group_id or 1,
                    )

            # Which repos get the repo-LEVEL check is a design question, not a
            # coverage one. An org install is served by org-level runners, so
            # demanding repo-level JIT write across every org repo would be
            # testing (and inviting) `Administration: write` on the whole org —
            # far broader than the org runner permission husk actually needs.
            # So: mint at the scope husk would really use — org-level for an
            # Organization account, repo-level for a personal one — plus any
            # repo named explicitly via --repo-targets (a planned allowed_repos
            # entry, which may legitimately sit inside an org).
            repo_mint = {r for r in repos if r in repo_targets}
            if kind != "Organization":
                repo_mint |= set(repos[:3])

            for full in repos[:3]:  # a sample is enough to prove read access
                await probe(
                    client,
                    rep,
                    f"repo runners: GET /repos/{full}/actions/runners",
                    f"{GH}/repos/{full}/actions/runners",
                    tok,
                    HINTS["repo-runners"],
                )
            for full in sorted(repo_mint):
                if do_mint:
                    await mint_and_delete(
                        client,
                        rep,
                        f"repo {full}",
                        f"{GH}/repos/{full}",
                        tok,
                        HINTS["repo-mint"],
                        group_id=None,
                    )
            if kind == "Organization" and not repo_mint:
                print(
                    "        (org install → org-level runners; repo-level JIT not\n"
                    "         required here. Use --repo-targets to check a repo you\n"
                    "         intend to put in allowed_repos.)"
                )

    print("\n" + "=" * 60)
    if rep.failures:
        print(f"FAILURES ({len(rep.failures)}):")
        for f in rep.failures:
            print(f"  - {f}")
        if not do_mint:
            print("\nnote: this was a READ-ONLY run; re-run with --mint to prove write")
        return 1
    print("ALL CHECKS PASSED")
    if not do_mint:
        print("note: READ-ONLY run — re-run with --mint to prove JIT write access")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
