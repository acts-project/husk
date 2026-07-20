# Targets, pools, and how capacity is shared

How huskd decides *where* runners go, and what stops two of them fighting over
the same hardware. Written up after the GitHub-App migration (Phases 0–3), which
turned huskd from a single-repo PAT autoscaler into a GitHub App serving pools
that each name the org or repo they belong to.

## The two axes

- A **pool** is a *runner type* — labels plus a backend. "The GPU pool."
- A **target** is a *place to put runners* — an org or a repo.

**Each `[[pool]]` names the one target it serves:**

```toml
[[pool]]
name   = "gpu"
target = { org = "acts-project", group = "husk" }   # org-level runners

[[pool]]
name   = "test"
target = { repo = "paulgessinger/husk-test" }       # that repo only
```

The key names the scope, so there is no `kind:name` string to parse. `group` sits
inside the target table because **runner groups are an org-only concept** — repo
scope has none — so the schema only lets you write one where it can mean
something, instead of accepting it on a repo pool and silently ignoring it. The
loader flattens it onto `runner.runner_group`, which is what the GitHub client
consumes.

### Why explicit, and not a global allowlist fanned out over pools

An earlier design had an `[access]` allowlist and ran **every pool against every
discovered target**. It was replaced, for one reason:

> **Warm capacity cannot be shared across targets.** A JIT runner registration
> belongs to exactly one org or repo, so a warm slot for org A physically cannot
> pick up org B's job.

Fan-out therefore never amortized. Every extra target cost a *full* `min_ready`
of permanently idle VMs, which silently over-subscribed scarce hardware — a GPU
pool with `min_ready = 1` serving two orgs needs two GPUs, and libvirt's capacity
check would refuse the second, leaving one org starved and retrying forever.

And it bought little: **org scope already covers the common case.** One
`org:acts-project` target serves every repo in the org with one warm pool.
Multi-target only spans separate *accounts*, which is rare and inherently costly.

What was given up is zero-touch onboarding — a new org now needs a `[[pool]]` and
a restart, rather than just installing the App. That was the honest trade: the
cost was always being paid, just not declared.

### `Target`

| | org scope | repo scope |
|---|---|---|
| API base | `/orgs/{login}` | `/repos/{owner}/{name}` |
| serves | every repo in the org | exactly that repo |
| runner groups | yes, resolved by name per target | none |
| permission | `Organization > Self-hosted runners` | `Repository > Administration: write` |

Repo scope exists because **personal accounts have no org-level runners**, so it
is the only way to serve a personal account's repos.

## Availability, not discovery

The target set is configured; what moves at runtime is whether each target is
*servable*:

- **org target** — is the App installed on that org?
- **repo target** — is it installed on the owner, **and** did that install grant
  this repo? Both must hold: the installer chose the repo, and the operator
  configured it. Either alone is not enough.

A pool whose target isn't servable simply doesn't run. huskd never declines an
installation — that is destructive and irreversible, and an unrecognized install
already gets no runners.

## What stops two units fighting

Two pools sharing one backend must not manage each other's slots, and must not
both claim the same physical hardware. Two mechanisms, and they are **not
uniform across backends**.

### 1. Name isolation

Runner names are `f"{vm_name}-c{cycle}"` and `match_runner` is prefix-based, so
distinct pools must mint distinct names. `load_configs` enforces unique pool
names *and* unique `vm_prefix`, which is all that is needed now that a pool maps
to exactly one target — there is no target-folded renaming, and no migration
hazard from the target set changing shape.

### 2. Slot ownership — who does `list_slots()` return?

This is where the backends diverge.

**libvirt — scoped.** Domains are stamped with a `pool` metadata tag at create,
and `list_slots()` returns only domains matching this backend's pool
(`libvirt_backend.py`). Two units on one host see only their own domains.

**OpenStack — scoped, via `husk-pool` metadata.** Servers are stamped with a
`husk-pool` tag at create, and `_owns()` requires it: `managed-by = husk` alone
is not ownership. A husk server without the tag is *not* adopted — huskd always
stamps it, so an untagged one is foreign or hand-made, and adopting it would mean
rebuilding something nobody asked huskd to manage.

> ### Historical: the collision this fixed
>
> Until `husk-pool` landed, `list_slots()` filtered solely on
> `managed-by == husk`. Every unit sharing an OpenStack project saw every other
> unit's servers as its own: it counted them toward its own sizing, and
> `match_runner` found no runner for them (their names carry a different
> prefix), so they classified as unhealthy or surplus and were rebuilt or
> destroyed by a controller that did not own them. Two units tore each other
> down.
>
> Verified directly at the time: a unit with a distinct pool name, distinct
> `vm_prefix` *and* a distinct target still classified a sibling's slot
> `unhealthy` and issued a rebuild. Distinguishing units by name is not enough —
> ownership has to be enforced where the listing happens.
>
> It predated the App migration (two OpenStack `[[pool]]`s collided the same
> way) but was latent, since only one OpenStack pool had ever been configured.
> Regression coverage: `tests/test_openstack_pool_scoping.py`.

### 3. Physical capacity

Distinct from ownership. Even when units correctly ignore each other's slots,
they must not both claim the same GPU.

**libvirt** handles this explicitly. `_occupied()` is **pool-blind on purpose**:
it counts units held by *all* husk domains on the host, not just this pool's, so
a GPU PCI address can never be double-booked across units. `capacity()` derives
free slot-units from that, and `create_slot` raises rather than overcommitting.

(It requires a `pool` tag to count a domain, so a pre-upgrade untagged leftover
cannot silently consume a unit forever.)

**OpenStack** capacity is project quota (`total_instances_used` vs the instance
limit), which is naturally global, so pools sharing a project share quota
correctly. Quota exhaustion is a clean failure — a create is refused, and the
pool retries.

## Lifecycle

The availability check runs every 60s. `MultiPoolController.discover_once()`
compares the servable set against the running pools: a pool whose target became
available starts; one whose target went away drains.

Starting is easy. **Draining destroys VMs, so it is guarded:**

| Situation | Behaviour |
|---|---|
| Check raises (GitHub 5xx) | Nothing changes; live pools held as-is |
| Sweep is *partial* (an install's repo listing failed) | May only **enable** — absence is not evidence of an uninstall |
| Target genuinely gone | Stop reconciling immediately, then drain |
| Draining, slot **idle** | Deregister runner, destroy slot |
| Draining, slot **busy** | Left running, retried next sweep — the job finishes |
| Target returns mid-drain | **Revived**: same Controller, slots intact, no rebuild |
| Backend can't list slots | Drain held open, never read as "nothing to clean up" |

The `complete` flag exists solely for row 2. Without it, one installation's
failed repo listing would look identical to that repo having been revoked, and a
transient 500 would tear down live runners.

## Where the data lives

Two in-memory registries, easy to confuse — both keyed by target, but carrying
opposite directions of information:

- **`SnapshotRegistry`** (`poller.py`) — *GitHub's* view: which runners exist per
  target and whether they're busy. One centralized `RunnerPoller` writes it;
  every reconcile task reads it. N pools on one target therefore cost **one**
  listing per interval, not one per pool per tick. A failed poll keeps the last
  good snapshot; the controller refuses one older than 180s and fail-safes the
  tick — that age check is what preserves "GitHub is down ⇒ take no action".
- **`DemandRegistry`** (`demand.py`) — *huskd's* sizing signal (`busy`,
  `desired`) per `(target, pool)`. Reconcile currently both writes and reads it
  in the same tick, so it is behaviourally a no-op. It is a **seam**: when
  webhooks land (Phase 4), the webhook handler becomes a second producer nudging
  the same map and the reconcile loop does not change.

## Open

- **Per-pool `min_ready` is per target by construction now.** If you ever want
  one pool's warm capacity spread across several targets, that is not a config
  change — it is impossible without GitHub letting one runner registration serve
  more than one org/repo.
- **Onboarding is a config edit + restart.** If that becomes a burden (many orgs,
  frequent churn), the fix is to make the *pool set* reloadable rather than to
  reintroduce fan-out.
