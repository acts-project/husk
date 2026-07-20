# Targets, pools, and how capacity is shared

How huskd decides *where* runners go, and what stops two of them fighting over
the same hardware. Written up after the GitHub-App migration (Phases 0–3), which
turned the target set from one configured repo into a discovered, dynamic set.

## The two axes

huskd's reconcile unit is a pair:

- A **pool** is a *runner type* — labels plus a backend. "The GPU pool." Declared
  as a `[[pool]]` in config.
- A **target** is a *place to put runners* — an org or a repo. Discovered at
  runtime from the App's installations, intersected with the allowlist.

```
                targets  (discovered: installations ∩ allowlist)
                ┌────────────────────┬──────────────────────────┐
                │ org:acts-project   │ repo:paulgessinger/…     │
      ┌─────────┼────────────────────┼──────────────────────────┤
pools │ gpu     │ (org:acts, gpu)    │ (repo:pg/…, gpu)         │
      │ cpu     │ (org:acts, cpu)    │ (repo:pg/…, cpu)         │
      └─────────┴────────────────────┴──────────────────────────┘
                        each cell = one Controller + one asyncio task
```

Every cell is an independent `Controller` with its own backend instance, GitHub
client, and reconcile task. **Today every pool serves every discovered target**
— the grid is fully populated. (`serve_targets`, a per-pool subset, is designed
but not built; see "Open" below.)

### `Target`

```python
Target(kind="org",  name="acts-project")            # key: "org:acts-project"
Target(kind="repo", name="paulgessinger/husk-test") # key: "repo:paulgessinger/husk-test"
```

| | org scope | repo scope |
|---|---|---|
| API base | `/orgs/{login}` | `/repos/{owner}/{name}` |
| serves | every repo in the org | exactly that repo |
| runner groups | yes, resolved by name per target | none — groups don't exist here |
| permission | `Organization > Self-hosted runners` | `Repository > Administration: write` |

Org scope is the scalable default: one warm pool serves the whole org. Repo scope
exists because **personal accounts have no org-level runners**, so it is the only
way to serve a personal account's repos.

## How runners are assigned to targets

There is **no global allocator**. Each `(target, pool)` unit sizes itself
independently, from its own view of its own target:

```
desired = min(max_total, busy + min_ready)
```

where `busy` counts that target's runners currently running a job. So
`min_ready` and `max_total` are **per unit, not per pool**. This is the single
most surprising consequence of the target grid, and it multiplies:

> A pool with `min_ready = 1, max_total = 3`, serving 4 discovered targets, holds
> **4 idle slots** warm and can grow to **12** — not 1 and 3.

That is intentional (a warm slot for org A cannot pick up org B's job — GitHub
routes by registration, not by huskd), but it means widening the allowlist
widens your resting footprint. Size `min_ready` with the target count in mind.

## What stops two units fighting

Two units sharing one backend must not manage each other's slots, and must not
both claim the same physical hardware. Three mechanisms, and they are **not
uniform across backends**.

### 1. Name isolation

Runner names are `f"{vm_name}-c{cycle}"` and `match_runner` is prefix-based, so
distinct units must mint distinct names. With more than one allowlist entry, the
target is folded into the pool's identity:

```
pool name : gpu          ->  gpu@acts-project
vm_prefix : husk-gpu     ->  husk-gpu-acts-project
```

With exactly **one** allowlist entry, names are left plain (`husk-gpu`). This is
deliberate: changing a `vm_prefix` under a running VM orphans it — the slot stops
matching the prefix and becomes invisible to reconcile.

Two consequences worth internalising:

- Naming keys off the **allowlist size** (static, from config), *not* the live
  discovered count. Otherwise an install or uninstall would silently rename
  pools and orphan every running slot.
- **Going from one allowlist entry to two renames the pools.** That is a
  restart-and-drain migration, not a hot edit.

`@` is reserved in configured pool names, because config reload splits on it to
map a live unit back to its `[[pool]]`.

### 2. Slot ownership — who does `list_slots()` return?

This is where the backends diverge.

**libvirt — scoped.** Domains are stamped with a `pool` metadata tag at create,
and `list_slots()` returns only domains matching this backend's pool
(`libvirt_backend.py`). Two units on one host see only their own domains.

**OpenStack — scoped, via `husk-pool` metadata.** Servers are stamped with a
`husk-pool` tag at create, and `_owns()` filters on it. Servers created *before*
that tag existed are claimed by **name prefix** instead — `vm_prefix` is unique
per pool (enforced by `load_configs`), so exactly one pool adopts each legacy
server, and the tag is backfilled on its next `mark_active`. The fallback is
transitional, and deliberately not a strict filter: an untagged server that
became invisible would be reconciled and deleted by nobody, and bill forever.

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
limit), which is naturally global — so quota is shared correctly across units
even though *ownership* is not. Quota exhaustion is a clean failure; the
ownership gap above is the dangerous one.

## Lifecycle

Discovery runs every 60s. `MultiPoolController.discover_once()` diffs the result
against the live set: new targets get built and spawned; departed targets drain.

Adding is easy. **Removal destroys VMs, so it is guarded three ways:**

| Situation | Behaviour |
|---|---|
| Discovery raises (GitHub 5xx) | Nothing changes; target set held as-is |
| Sweep is *partial* (one install's repo listing failed) | May only **add** — absence is not evidence of removal |
| Target genuinely gone | Stop reconciling immediately, then drain |
| Draining, slot **idle** | Deregister runner, destroy slot |
| Draining, slot **busy** | Left running, retried next sweep — the job finishes |
| Target reappears mid-drain | **Revived**: same Controller, slots intact, no rebuild |
| Backend can't list slots | Drain held open, never read as "nothing to clean up" |

The `complete` flag on a discovery sweep exists solely for row 2. Without it, one
installation's failed repo listing would look identical to that repo having been
removed, and a transient 500 would tear down live runners.

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

- **Explicit pool→target binding** (decided 2026-07-20, not yet built). Each
  `[[pool]]` will name the one target it serves, replacing the full
  pool × target grid. Rationale: warm capacity cannot be shared across targets,
  so fan-out never amortized — it silently multiplied `min_ready` and
  over-subscribed scarce hardware. Org scope already covers the common
  "many repos" case with a single target. This also deletes `_target_naming`
  and the 1→2-target rename hazard. Cost: onboarding a new org becomes a config
  edit rather than zero-touch.
