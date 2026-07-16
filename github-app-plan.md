# husk as a GitHub App — implementation plan

Turn huskd from a single-repo, PAT-authenticated runner autoscaler into a GitHub
App installable on arbitrary orgs/repos, gated by a huskd-side allowlist, and do
it in the same pass as the async / centralized-poller refactor so the new code is
written once in its final form.

## Settled decisions

- **Restriction lives in huskd, not GitHub.** GitHub can't restrict a public App
  to a set of orgs, so the App is installable by *any* account and huskd enforces
  a two-list allowlist. huskd holds the App private key and runs the VMs, so an
  install it doesn't recognize simply gets no runners.
- **Two-list allowlist; entry type decides scope.**
  - `allowed_orgs = ["acts-project"]` → **org-level** runners for the whole org.
  - `allowed_repos = ["paulgessinger/husk-test"]` → **repo-level** runners for
    exactly that `owner/repo`, nothing else that owner owns.
  - Defense in depth: the install's granted-repo set *and* huskd's allowlist must
    both agree before a repo is served.
- **Hybrid scope.** Org-level is the scalable default (one poll + one warm pool
  per org, existing `busy + min_ready` math, scales to any repo count). Repo-level
  (today's code path) is the fallback for personal-account projects. Personal
  accounts have no org-level runners, so this is the only way to ever support
  `paulgessinger/*` projects.
- **Delivery = JIT ephemeral runners** (unchanged from today), via App
  installation tokens instead of a PAT. The `generate-jitconfig` / list / delete /
  reap endpoints are identical; only the bearer token and the `/orgs|/repos` path
  prefix change.
- **Poll, don't webhook (for now).** huskd runs in a restricted network with no
  inbound reachability; the whole design is poll-by-design. Discovery polls
  `GET /app/installations`; demand polls the runner list per target. Webhooks are
  a deferred, non-blocking accelerant (Phase 4).
- **Async-first sequencing.** The async / centralized-poller refactor lands before
  the App migration so the token provider, discovery, and scope branching are
  born async and written exactly once (see rationale below).

## The conceptual shift

- **Reconcile unit:** `pool` → `(target, pool)`, where a `target` is tagged
  `org:<login>` or `repo:<owner/name>`. A pool is a *runner type* (labels +
  backend); a target is a *place to put runners*.
- **Demand-signal seam:** reconcile consumes `desired(target, pool)` from an
  in-memory registry; it does **not** call GitHub inline. One centralized poller
  is the only producer today; a webhook handler becomes a second producer later.
- **Runner-group gotcha:** `runner_group_id` is not portable across orgs. Config
  moves to a runner-group **name**, resolved to an ID per target
  (`GET /orgs/{org}/actions/runner-groups`), falling back to Default (1). Repo /
  personal path ignores groups.
- **Name isolation** (unique `vm_prefix` / labels, already required because runner
  APIs are repo-wide) now must also be per-target so names don't collide across
  targets sharing a backend — fold the target into the prefix.

## Why async before the App (churn rationale)

Migrating to the App on the current threaded / `requests` model first would mean
writing the token provider and discovery synchronously and then porting them to
async — double work on precisely the newest code. Async-first means they're born
async. The only re-editing is the GitHub client (keying → async → auth/paths), and
those three touches are orthogonal, not rewrites.

| Concern | Written / rewritten | Times touched |
|---|---|---|
| `Target` + demand seam | Phase 0 | 1 (kept forever) |
| Async port of client + reconcile | Phase 1 | 1 (on trivial domain) |
| Token provider, discovery, scope branching | Phases 2–3 | 1 each, born async |
| Reconcile loop | untouched after Phase 1 | seam absorbs App changes |

## Target config schema (end state)

Clean cutover — no back-compat, no dual-form parsing. Rewrite `config.example.toml`
and drop `repo` / `pat` / `pat_path` / `pat_env`.

```toml
[github]                                    # App identity replaces repo + pat
app_id = 123456
private_key_path = "/etc/husk/husk-app.pem" # or HUSK_GITHUB__PRIVATE_KEY

[access]                                     # THE restriction — huskd's allowlist
allowed_orgs  = ["acts-project"]             # whole org  → org-level runners
allowed_repos = ["paulgessinger/husk-test"]  # this repo  → repo-level runners

[pool.gpu]
# ...backend + runner config as today...
runner_group = "husk"                        # was runner_group_id (now a name)
serve_targets = ["org:acts-project"]         # optional; default = all allowed targets
min_ready = 0                                # sensible default for personal repos
```

## Phases

Each phase is independently shippable and testable; you can stop after any phase
and have a working system.

### Phase 0 — Shared abstractions, zero behavior change
*(still PAT, still threads, still `requests`, one repo)*
- Add a `Target` type (`org:<login>` / `repo:<owner/name>`); re-key reconcile
  `pool` → `(target, pool)` with a single static target derived from today's
  `repo` (cardinality 1).
- Add the **demand-signal seam**: reconcile reads `desired(target, pool)` from an
  in-memory registry instead of calling GitHub inline. The same inline poll fills
  the registry behind the interface.
- **Ships:** identical behavior, verifiable against current husk.
- **Churn:** none — every abstraction here survives to the end. May be folded into
  Phase 1; kept separate because doing the re-keying in the familiar sync model
  de-risks it.

### Phase 1 — Async + centralized poller (target-architecture refactor)
*(still PAT, still one target)*
- Port the GitHub client `requests` → `httpx`, sync → async; replace the
  daemon-thread deadline hack (`github.py:20-96`) with `asyncio.wait_for`.
- One centralized async poller task fills the `SnapshotRegistry`; reconcile becomes
  **async tasks** per `(target, pool)` reading the registry. Single async Quart
  process; drop file state.
- **Ships:** behavior identical, now async. Verify against Phase 0.
- **Churn:** the one big mechanical rewrite, done while the domain is one PAT
  target so correctness is easy to check.

### Phase 2 — GitHub App auth (swap PAT → App)
*(targets still static/explicit — no discovery yet)*
- `InstallationTokenProvider`: sign RS256 JWT (10-min exp, in-memory) from
  `app_id` + PEM; exchange for per-installation tokens
  (`POST /app/installations/{id}/access_tokens`), cache per installation_id,
  refresh at ~55 min or on 401. Written once, async.
- Config cutover: App identity replaces `repo` + `pat`. A temporary explicit
  target key (e.g. `[access] targets = ["org:acts-project"]`) stands in for
  discovery.
- Scope branching in the client paths (`/orgs/…` vs `/repos/…`); runner-group
  name → ID resolution per target.
- **Ships:** App-authenticated runners against a known target.
- **Churn:** ~10 lines of throwaway (the temp `targets` key), a deliberate cost to
  isolate *auth* bugs from *discovery* bugs. Optional to merge with Phase 3.

### Phase 3 — Dynamic discovery + allowlist
- Discovery poller: `GET /app/installations` → for each install read
  `account.login`; if in `allowed_orgs` emit an **org target**; regardless,
  `GET /installation/repositories` ∩ `allowed_repos` → emit **repo targets**.
- Drive reconcile-task **lifecycle**: spawn on new target, reap (deregister
  runners + stop tasks) on removed target.
- Two-list allowlist config replaces the temp `targets` key.
- **Ships:** full "install on arbitrary org/repo, gated by huskd."
- **Churn:** none — feeds machinery already built; only makes the target set
  dynamic.

### Phase 4 — Webhooks *(deferred, unblocked)*
- `POST /webhook` + `X-Hub-Signature-256` verification as a **second producer**
  nudging the registry. Poll stays as the backstop even after this lands. Drops in
  because the seam (Phase 0) and async loop (Phase 1) already exist.

## GitHub App setup (one-time, manual, outside code)

- Create the App owned by whichever org, **installable on "Any account."**
- Permissions: `Organization self-hosted runners: write` (org path);
  `Administration: write` on repos (repo / personal fallback); `Metadata: read`;
  `Actions: read`.
- Subscribe to no events yet (webhooks deferred). Download the private-key PEM.

## Validation

- **Unit:** JWT signing, token cache expiry/refresh, allowlist filtering
  (org + repo lists), org-vs-repo path builder, runner-group name → ID resolution.
- **Live** (mirrors the POC discipline): install the App on `acts-project`, confirm
  discovery → org-level JIT mint → runner appears → job runs → reaping; then a
  personal-account install of `paulgessinger/husk-test` to exercise the repo-level
  fallback.

## Open items to revisit before/while building

- `serve_targets` per-pool mapping — default (all pools serve all targets) vs
  explicit; confirm the fan-out policy.
- Whether to auto-decline (`DELETE /app/installations/{id}`) non-allowlisted
  installs or just ignore them (starting position: ignore).
- Merge Phase 2 + Phase 3 to avoid the temp `targets` scaffold, at the cost of a
  bigger single step.
