# Husk Observability

How husk exposes metrics for the fleet: the long-lived infrastructure (controller,
libvirt hosts) and the ephemeral, single-use runner slots. Companion to
`plan.md` and `image-pipeline.md`; this document defines the metrics story those
left open.

> **Status (2026-07-14):** **O1–O4 are built.** Only the optional **O5** remains.
>
> **O4 changed shape:** there is **no per-host proxy**. huskd bridges the last hop
> to a libvirt guest over the SSH channel it already holds to the hypervisor, so
> **nothing is deployed on any host** — see Phase O4 below for why, and what it
> costs. Sections below that describe a "dumb host proxy" are superseded by that.
>
> - **O1 ✅** boot-timing exfil: huskd reads the `husk-bootreport` block off the
>   serial console and exposes `husk_slot_boot_*`.
> - **O2 ✅** node_exporter is baked into both golden variants
>   (`images/build.sh`, `husk-node-exporter.service`, no TLS/auth) and cloud-init
>   opens `:9100` to the pool's `scrape_cidr` and starts it. **Takes effect on an
>   image rebuild** (`just rebuild-all` + republish).
> - **O3 ✅** discovery + join: `GET /sd/targets` (Prometheus `http_sd`, one feed,
>   both backends) and the `husk_slot_info` join table on `/metrics`.
> - **O4 ✅** libvirt guest bridge **inside huskd** (`GET /slot/<pool>/<slot>/metrics`
>   → SSH → `guest:9100`). No per-host component.
>
> **Both backends are code-complete.** What's left is operational, not code:
> **rebuild + republish the golden image** (O2 only lands on a rebuild), and **set
> `scrape_cidr`** per pool — known for libvirt (`192.168.122.1/32`, the bridge),
> still unknown for OpenStack (see open questions: it depends on where central
> Prometheus lives, and it's fail-closed until set).
>
> The controller `/metrics` endpoint now renders from a `prometheus_client`
> `CollectorRegistry` (`src/husk/metrics.py`) instead of hand-built strings. See
> **huskd's own metrics** below for the snapshot/event-time split, what changed
> names, and how counters are persisted across restarts.

-----

## Goals

1. **Live per-slot resource metrics during a job** — CPU, memory, disk, network of
   the runner VM while it executes a (10min+) job, so we can see whether a build is
   CPU/mem/IO-bound and size pools accordingly. This is the primary new ask.
2. **Host-level metrics for the libvirt hypervisors** — the boxes husk owns
   (node_exporter + per-domain libvirt metrics). OpenStack hypervisors aren't ours.
3. **Surface the boot-timing report** (`husk-bootreport`) that is baked into the
   golden image today but goes only to the serial console and is never collected.
4. **Attribute in-guest metrics to a pool and a GitHub job** — a metric join, so a
   spike on `10.1.2.3:9100` is legible as "pool X, job Y, cycle N."
5. **Keep the controller out of the metrics hot path.** huskd orchestrates
   *discovery* and *control-plane facts*; it does **not** relay per-instant guest
   metrics.

### Non-goals

- huskd proxying/relaying node_exporter scrapes. **Amended in O4:** still true for
  OpenStack (routable guests, scraped directly), but **knowingly violated for
  libvirt**, whose guests are unreachable except through their hypervisor. Paying a
  per-host proxy + a new network path per host to preserve the principle was judged
  not worth it at this fleet size. See "Why huskd bridges, and not a per-host proxy".
- Scraping boot-timing off ephemeral VMs by pull (anti-pattern; it's a
  control-plane fact the controller already half-owns).
- Managing the central Prometheus / long-term storage — that's existing infra;
  we produce targets + endpoints it consumes.

-----

## The two layers (the core mental model)

Husk observability splits cleanly into two categories that want **opposite** tools.
Conflating them is the main design trap.

| | **Control-plane facts** | **In-guest resource metrics** |
|---|---|---|
| Examples | boot/recycle timing, slot state, desired/ready counts, `husk-bootreport` breakdown | CPU, memory, disk, net *during* a job |
| Source of truth | the controller already observes these | only the guest knows |
| Lifetime of source | long (the controller) | ephemeral (the slot) |
| Right pattern | **expose from huskd `/metrics`** — one stable target | **scrape the guest directly** (node_exporter) |
| Who Prometheus scrapes | the controller (1 target) | each running slot (N targets) |
| huskd's role | **produce the metric** | **produce discovery + a join table**, not the metric |

The consequence: **the controller is a metrics *source* for layer 1 and a
*discovery/label service* for layer 2 — and, wherever it can be avoided, not a
metrics proxy for layer 2.** Relaying guest metrics through huskd adds a
bottleneck, a failure mode, and loses node_exporter's native `up`/staleness
semantics.

> **Amended in O4.** This holds for OpenStack, whose guests are routable and are
> scraped directly. It does **not** hold for libvirt: those guests are reachable
> only from their hypervisor, so *something* has to bridge, and the alternatives all
> put a component + a new network path on every host. huskd bridges them over the
> SSH channel it already has. We pay exactly the costs named above (a huskd outage
> gaps libvirt metrics; `up` conflates guest-sick with huskd-sick) and judged them
> cheaper than per-host infrastructure at this fleet size.

Why direct scraping is fine for the ephemeral slots here: jobs are 10min+, so a
slot lives long enough for dozens of 15s scrapes. (This is exactly why boot-timing
is *not* scraped this way — a one-shot value known only at end-of-boot on a
short-lived target is the ephemeral-scrape anti-pattern, so it stays in layer 1.)

**Layer-2 refinement — in-guest node_exporter is the uniform per-VM source; only
transport differs.** The hypervisor view (`prometheus-libvirt-exporter`, available
on libvirt because we own the host) can supply per-VM CPU/disk/net with no in-guest
agent — but it **cannot** see guest-internal state: **filesystem fill**, load
average, CPU-mode split, fine memory. We need filesystem fill, so the in-guest
node_exporter is required on both backends and is the **primary** per-VM source
everywhere. What changes by backend is *reachability*, hence transport + discovery
(details below), not the source. The host-side libvirt-exporter is kept only as an
optional secondary on libvirt (a hypervisor-view cross-check, and per-domain data
when a guest's agent is down); it is not the primary path.

-----

## Architecture by backend

Reachability, not the discovery mechanism, dictates the topology. The runner
firewall's `:9100` ingress rule is the primary access control in both cases (see
Security).

### OpenStack (CERN) — in-guest node_exporter, direct scrape

Unlike libvirt, there is **no host-side option**: CERN owns the compute nodes, so
we can't run the libvirt-exporter equivalent ourselves. OpenStack's conceptual
analog is **Ceilometer → Gnocchi/Prometheus**, which polls the *same* libvirt
per-domain data on the compute nodes (`cpu`, `disk.device.*`, `network.*`,
`memory.usage`) — but it's operator-side telemetry we could only *consume* **if
CERN exposes a tenant-facing API**, and its polling is typically minute-scale
(vs. node_exporter's 15s) with the same balloon/memstat caveat on memory. So the
default OpenStack path is the **in-guest node_exporter, direct-scraped** (verify
CERN Gnocchi/telemetry as a possible lower-fidelity alternative — see open
questions).

Runner VMs have CERN-internal-routable IPs, reachable from a Prometheus that sits
inside (or is routed into) the CERN network.

```
[central Prometheus] ──HTTP scrape──▶ slot:9100  (node_exporter, no TLS/auth)
        │  discovery via huskd http_sd (native OpenStack SD = fallback)
        │  access control = nftables :9100 allow, scoped to the Prometheus source
        └── join: node_* × husk_slot_info (from huskd /metrics)
```

- **Discovery: huskd `http_sd` (preferred, unified across both backends).** Central
  Prometheus polls huskd's `GET /sd/targets` for live slot targets. This is chosen
  over native `openstack_sd_config` deliberately, not just for config uniformity:
  it removes CERN Nova/Keystone credentials + reachability from Prometheus (huskd
  already holds them); rides the Prometheus→huskd connection that already exists
  for `/metrics` scraping (no new path/dependency); carries huskd's native labels
  (`pool`/`slot`/`cycle`/`job_id`) directly instead of reconstructing them from
  `__meta_openstack_*`; and emits a target only once the runner is **online**
  (node_exporter up), avoiding the scrape-`down` edge noise native SD produces at
  `ACTIVE`.
- **Native `openstack_sd_config` (role `instance`) is the fallback** — use it only
  if metrics discovery must be fully decoupled from huskd (e.g. Prometheus owned by
  a separate team that won't depend on an app endpoint). It queries Nova, returns
  one target per NIC with `__meta_openstack_*` labels; you then relabel to keep
  only husk slots, pick the fixed IP, set port 9100.
- **No proxy.** Central Prometheus scrapes the slot directly. A huskd blip stops
  discovery of *new* slots (http_sd keeps the last target list on a failed refresh)
  but never interrupts scraping of existing ones — huskd is not in the metrics path.

### libvirt — in-guest node_exporter, bridged by huskd over SSH

Guests sit on a **private libvirt net**: only the hypervisor can reach
`slot:9100`. Something must bridge that last hop. **huskd does it, in-process**,
over the SSH channel it *already* holds to every host (the one
`libvirt_backend._ssh` uses for qemu-img/genisoimage). The baked node_exporter
(same image as OpenStack) is the source here too, because we need filesystem fill:

```
[central Prometheus] ──scrape──▶ [huskd] ──ssh──▶ hypervisor ──▶ slot:9100 (node_exporter)
        │  target from huskd http_sd:  __address__=<advertise_addr>,
        │                              __metrics_path__=/slot/<pool>/<slot>/metrics
        │  NOTHING is deployed on the hypervisor
        └── join: node_* × husk_slot_info (from huskd /metrics)
```

- **Primary source — in-guest node_exporter** (baked, same image). Full guest view:
  **filesystem fill**, load, CPU-mode split, fine memory, plus CPU/disk/net.
- **Transport — huskd, over its existing SSH channel.** Central scrapes
  `huskd/slot/<pool>/<slot>/metrics`; huskd SSHes to the host and curls the guest.
  Because the request is issued *from the hypervisor*, the guest sees the **libvirt
  bridge** as the client — which is exactly what the `:9100` allowlist admits.
- **Discovery — huskd `http_sd`, the same endpoint as OpenStack.** One feed serves
  both backends via per-target routing: OpenStack targets point at the guest
  directly; libvirt targets point back at huskd.
- **`:9100` ingress rule on the guest** — scoped to the libvirt-bridge address
  (`scrape_cidr`), rendered by huskd into the cloud-init ruleset.
- **Optional secondary — `prometheus-libvirt-exporter` on the host** for a
  hypervisor-view cross-check. Not required (O5).

### Why huskd bridges, and not a per-host proxy / agent

The hypervisor has to be traversed; the question was *by what*. The original plan
here was a stateless path-routing proxy on each host. **We rejected it — along with
the heavier agent options — on operational cost:**

| | **huskd SSH bridge** (chosen) | per-host proxy | vmagent `remote_write` | Prometheus + `/federate` |
|---|---|---|---|---|
| Deployed on each hypervisor | **nothing** | a proxy + unit | agent + disk buffer | full TSDB |
| New network path to open, per host | **none** (reuses SSH) | yes: central → host:9101 | none (push) | yes |
| Host holds a reach-central secret | no (pull) | no (pull) | **yes** | no |
| huskd in the metrics data path | **yes** (libvirt only) | no | no | no |
| `up` distinguishes guest-sick from infra-sick | **no** | yes | yes | yes |

The decider was that a per-host proxy needs a **new Prometheus → hypervisor network
path opened on every host, forever**, plus a component to install and upgrade
there — recurring network-admin and orchestration overhead. Prometheus **already**
scrapes huskd, so that path is proven and free.

**The honest cost:** this contradicts the "keep the controller out of the metrics
hot path" principle above — for libvirt. We take it knowingly, because the fleet is
small enough that the bottleneck argument is theoretical, and because the two real
costs are bounded: a huskd outage gaps libvirt guest metrics, and `up` for those
targets no longer separates "the guest is sick" from "huskd/SSH is sick".
**OpenStack keeps the pure design** — routable guests, scraped directly, huskd
nowhere near the data path. Revisit if the libvirt fleet grows enough that one
process fanning out N SSH scrapes every 15s becomes a real bottleneck.

### Summary

| | OpenStack | libvirt |
|---|---|---|
| Own the hypervisor? | no (tenant) | yes |
| Primary per-VM source | **in-guest node_exporter** | **in-guest node_exporter** (same baked image) |
| Guest reachability | CERN-routable IP, direct | host-only (private net) → bridged by huskd over SSH |
| Scrape transport | central → `slot:9100` | central → `huskd/slot/<pool>/<slot>/metrics` → ssh → `slot:9100` |
| Discovery | huskd `http_sd` (single feed, per-target routing) | huskd `http_sd` (same feed) |
| Host component | none | **none** (huskd reuses its existing SSH channel) |
| huskd in the data path | no | yes (accepted trade — see above) |
| Access control | nftables `:9100` allow (Prometheus source) | nftables `:9100` allow (libvirt-bridge source — the scrape is issued from the host) |

-----

## Configuring Prometheus (the consumer side)

Everything above defines huskd's *contract*; this is how central Prometheus
consumes it. **Two scrape jobs, matching the two layers** — deliberately not one.

```yaml
scrape_configs:
  # ── Layer 1: the controller ────────────────────────────────────────────────
  # huskd's own state-derived gauges: husk_slots*, husk_slot_boot_*, and the
  # husk_slot_info join table. ONE static target that never changes.
  - job_name: huskd
    static_configs:
      - targets: ["huskd.internal:9100"]      # → GET /metrics

  # ── Layer 2: the runner slots ──────────────────────────────────────────────
  # Per-slot in-guest node_exporter, discovered live. ONE feed, BOTH backends:
  # per-target routing means OpenStack targets resolve to <guest-ip>:9100 (direct)
  # and libvirt targets resolve back to huskd, which bridges the scrape over SSH.
  - job_name: husk-slots
    http_sd_configs:
      - url: http://huskd.internal:9100/sd/targets
        refresh_interval: 30s
    # __address__ and __metrics_path__ arrive already set by huskd; the `backend`
    # and `slot` labels come through as-is and are the join key below.
```

**Why two jobs, not one** (i.e. why huskd does *not* advertise itself through
`/sd/targets`): Prometheus must already know huskd's URL to call `/sd/targets` at
all, so self-advertising is circular — it can only restate what the `http_sd_config`
already hardcodes. And the two layers *want* to differ: the controller job is
pool-scoped (no `slot` label) and fine at a slow interval; the slots job is
per-slot and wants a tight one. Folding them into one feed just forces relabeling
to pull back apart a distinction you erased for nothing. The same `huskd.internal`
address legitimately appears in both — that redundancy is the whole reason
self-advertising buys nothing.

### The join: making a `node_*` spike legible as pool / job

`node_*` series are keyed only by the target's minimal identity (`backend`,
`slot`) — cardinality is kept low on purpose (see Division of labor #4). The rich
attribution (`ip`, `host`, `runner`, `cycle`, and later `job_id`) lives on the
`husk_slot_info` gauge from layer 1. Join them at query time on `(backend, slot)`:

```promql
# "show me each running slot's root-fs fill, labelled by pool + runner"
node_filesystem_avail_bytes{mountpoint="/"}
  * on(backend, slot) group_left(host, runner, cycle)
    husk_slot_info
```

`group_left(...)` copies the named `husk_slot_info` labels onto every matching
`node_*` series; `on(backend, slot)` is the shared key. Any `node_*` metric joins
the same way — this is the whole point of emitting `husk_slot_info` rather than
stamping pool/job onto every guest series (which would inflate cardinality and
churn it on every recycle).

> **`up` semantics differ by backend, and it matters when alerting.** An OpenStack
> target's `up == 0` means the guest is unreachable. A libvirt target's `up == 0`
> means the guest *or* huskd *or* the SSH hop is unreachable (huskd is in that data
> path — see "Why huskd bridges"). Don't page on a raw libvirt `up == 0` as though
> it were guest-specific; correlate with the `huskd` job being up first.

-----

## Division of labor: huskd-orchestrated vs. per-node setup

This is the crux question. The line: **anything that is per-cycle, dynamic, or a
control-plane fact → huskd. Anything static that lives on a machine → baked into an
image or set up once per host.**

### huskd-orchestrated (code in this repo, no manual steps)

1. **Extend `/metrics` (`render_prometheus`) with the boot-timing breakdown.**
   Parse the `husk-bootreport` block from the console log (see below) and emit
   `husk_slot_boot_*` per-unit gauges alongside the existing
   `husk_slot_last_cloudinit_seconds` / `husk_slot_last_recycle_seconds`. Pure
   controller work; Prometheus scrapes the one existing controller target.
2. **Console-log exfil (the `husk-bootreport` consumer).**
   - OpenStack: call Nova `get_console_output` after the slot's runner comes
     online; parse the `===== husk-bootreport =====` block.
   - libvirt: set `console_log_path` in the domain XML
     (`libvirt_backend.py:724`, currently unset by design), read the serial log
     file. Requires the host-setup file-ownership fix noted in that comment — so
     this half straddles into per-node setup (see below).
3. **The `husk_slot_info` join table** on `/metrics`:
   ```
   husk_slot_info{backend="cern-cpu", slot="...", ip="...",
                  host="...", runner="..."} 1
   ```
   Note `cycle` is **not** a label here (it is `husk_slot_cycle`): it increments
   on every recycle, so as a label it minted a fresh series per recycle — exactly
   the churn this join table exists to avoid.
   This is what makes `node_*` (keyed by IP) legible as pool/job. huskd already
   owns every one of these labels.
4. **Discovery — a single `http_sd` endpoint** (a Quart route, `GET /sd/targets`)
   returning live `{targets, labels}` JSON for running slots, serving **both**
   backends. Per-target routing (`__address__` + `__metrics_path__` are
   relabelable) means one feed covers OpenStack (target = the guest IP directly)
   and libvirt (target = `host:PORT`, `__metrics_path__=/<slot>/metrics` through
   the host proxy). **Preferred over native OpenStack SD** (removes Nova creds from
   Prometheus, reuses the existing Prometheus→huskd connection, carries
   huskd-native labels, emits only runner-online slots). Keep **target labels
   minimal** (identity to join — `slot`); leave rich attribution (`job_id`,
   `cycle`) to `husk_slot_info` so per-recycle churn doesn't inflate `node_*`
   cardinality. (No per-host `file_sd` — the proxy makes the guests reachable
   through the host address, so the single central feed suffices.)
5. **The dynamic firewall ingress rule.** The `:9100`-from-scraper allow is
   *policy*, so it rides the existing per-cycle cloud-init ruleset
   (`husk-egress.nft`), not the image. huskd renders it (one config knob: the
   scraper source CIDR). **Both backends**: OpenStack scoped to the central
   Prometheus source; libvirt scoped to the host's own libvirt-bridge address.

### Baked into the golden image (built once, in CI — `images/build.sh`)

Static capability, per the image/cloud-init boundary (`image-pipeline.md`):

6. **node_exporter binary + `husk-node-exporter.service`** (a new file under
   `images/files/`), **not enabled for boot** — cloud-init starts it like the
   runner unit, or it's enabled to start on boot since it has no per-cycle input.
   Runs as `root` or a dedicated `node_exporter` user — **never** as `runner`.
   **No `--web.config.file`, no TLS, no basic-auth** — access is controlled purely
   at the network layer (see Security). Nothing secret is baked, so there's no
   credential to protect on a job-executing box.
7. **(GPU variant) the DCGM/nvidia metrics exporter** if we want GPU utilization —
   deferred; note it here so the boundary is explicit.

### Per-node setup (once per libvirt host)

**Metrics need NO per-node setup.** This section originally carried a host metrics
proxy; O4 removed the need for it (huskd bridges over its existing SSH channel), so
what remains here is unrelated to metrics collection:

9. **The serial-log file ownership/relabel fix** so libvirt/qemu can write the
   per-domain console log that huskd's boot-timing exfil (item 2) reads — the thing
   the `libvirt_backend.py` `console_log_path` comment defers to host setup. Needed
   for boot-timing on libvirt regardless.
10. **L2 isolation between sibling slots** on the shared libvirt bridge (see
    Security) — per-slot isolated networks or bridge port isolation. This protects
    slot↔slot and is the assumption the `:9100` allowlist rests on.
11. **(Optional, O5)** `prometheus-libvirt-exporter` for a hypervisor-view
    cross-check.

> Automating these is tracked as **deferred Ansible host provisioning** (`plan.md` /
> memory); this plan does not un-defer it. **Nothing here blocks either backend's
> per-VM metrics** — that path is complete without any host setup.

### At-a-glance

| Concern | huskd | Image (CI) | Per libvirt host |
|---|---|---|---|
| Boot-timing metrics | ✅ parse + expose | — | serial-log fix (libvirt only) |
| `husk_slot_info` join | ✅ | — | — |
| qcow2 storage usage | ✅ cache scan + per-host `stat` | — | — |
| Discovery (`http_sd`, single feed) | ✅ | — | — |
| `:9100` ingress rule | ✅ (cloud-init, `scrape_cidr`) | — | — |
| node_exporter (no TLS/auth) | — | ✅ baked (primary per-VM source, both backends) | — |
| Guest bridge (libvirt) | ✅ `/slot/<pool>/<slot>/metrics` over its existing SSH channel | — | **nothing** |
| Scrape transport | — | — | central → huskd → ssh → guest (libvirt); direct pull (OpenStack) |
| Optional per-domain metrics | — | — | optional `prometheus-libvirt-exporter` |

-----

## huskd's own metrics

Everything above is about the *fleet*. This section is about **layer 1** — huskd's
own `/metrics`, which is scraped as one static target and describes the control
plane itself.

It is built from a `prometheus_client` `CollectorRegistry` (`src/husk/metrics.py`)
split into two halves that behave very differently.

### Snapshot-derived — `SnapshotCollector`

`husk_slots*`, `husk_slot_last_*_seconds`, `husk_slot_boot_seconds`,
`husk_slot_cycle`, `husk_slot_info`, `husk_image*`. These describe the **present**,
and huskd already holds a complete immutable description of the present: the
per-pool `ControllerState` the reconcile loop swaps in each tick. They are rendered
straight from it at scrape time and are never stored.

These are registered as a **collector**, not as library `Gauge` objects, and that
distinction is load-bearing. A `Gauge`'s labelsets never expire, so
`Gauge.labels(slot="husk-a-7").set(...)` keeps reporting a slot forever after it is
destroyed — you would have to hand-roll clear-and-repopulate every tick. A
collector reads the current snapshot, so a destroyed slot simply produces no
sample and Prometheus's own staleness handling finishes the job.

### Event-time — `Metrics`

`husk_reconcile_ticks_total`, `husk_reconcile_aborts_total`,
`husk_reconcile_duration_seconds`, `husk_action_failures_total`,
`husk_slots_created_total`, `husk_slots_destroyed_total`,
`husk_slot_recycles_total`, `husk_recycle_duration_seconds`,
`husk_cloudinit_duration_seconds`, `husk_boot_phase_seconds`,
`husk_github_polls_total`, `husk_github_poll_failures_total`,
`husk_guest_scrape_failures_total`.

These describe **what happened between scrapes**, which no snapshot can express: a
rebuild that failed and was retried leaves no trace in the current state, and a
failed GitHub poll deliberately leaves the last good snapshot published. They are
recorded as they occur (in `Controller` and `RunnerPoller`) and accumulate across
ticks.

The two most operationally valuable, and the reason this half exists:

- **`husk_reconcile_aborts_total{backend,reason}`** — huskd is up, scraping fine,
  and has silently stopped acting on reality. `reason` separates a backend listing
  failure from a stale/absent GitHub runner snapshot, which have different fixes.
  ```promql
  increase(husk_reconcile_aborts_total[15m]) > 0
  ```
- **`husk_action_failures_total{backend,action}`** — rebuild/create/destroy/… that
  failed. Previously these were only pinned to a slot as a dashboard string, so
  "rebuild failure rate climbed after the image bump" was unanswerable.

`action` is the **verb only**. The controller's internal descriptions embed the
slot id for the log line (`"destroy vm-abc123"`); `_action()` strips it, because a
slot id in a label mints a new series per slot per action and leaves it behind.

**Cardinality rule, enforced by a test:** no event-time instrument carries a
per-slot label. Every label value comes from config (pool names, target keys) or a
fixed vocabulary (action, reason, phase). Per-slot detail lives only in the
snapshot half, where it expires on its own.

### Histograms vs. the "last value" gauges

`husk_slot_last_recycle_seconds` is a gauge holding one slot's most recent
bring-up. It answers "why is *this* slot slow" — which is what the dashboard wants
— but it cannot answer "what is the p95 over the last day, and did it move when we
bumped the image": `quantile()` over a gauge is the quantile *across slots at one
instant*, not across events over time.

So both exist. The gauges stayed; `husk_recycle_duration_seconds`,
`husk_cloudinit_duration_seconds` and `husk_boot_phase_seconds` were added as
histograms, observed at the moment a bring-up completes.

```promql
histogram_quantile(0.95, sum by (le, backend) (
  rate(husk_recycle_duration_seconds_bucket[6h])))
```

Bucket bounds are in `husk.metrics` (`BRINGUP_BUCKETS`, `BOOT_BUCKETS`,
`TICK_BUCKETS`). The library defaults top out at 10s, which is useless here — a
slot bring-up is a minute or more.

### `husk_slot_state_seconds_total` replaces `husk_slot_live_fraction`

`live_fraction` was a ratio computed inside husk over "all time since huskd
started". That fixes the window at query time and resets silently on restart, so
"live fraction over the last hour" was unanswerable. The raw seconds are exposed
instead — the accumulator (`SlotTiming.state_seconds`) already existed — and the
ratio is derived in PromQL, over whatever window you like:

```promql
sum by (backend, slot) (rate(husk_slot_state_seconds_total{state=~"busy|idle"}[1h]))
/ sum by (backend, slot) (rate(husk_slot_state_seconds_total[1h]))
```

It lives in the *snapshot* half despite being a counter, because it is per-slot and
its accumulator is owned by the slot — so it must expire when the slot does.

`live_fraction` is still in `/status` for the dashboard, which has no PromQL to
divide with.

### Persistence across restarts

huskd restarts on every config change (there is no hot reload), and a restart zeros
every counter. Prometheus copes — `rate()` treats a drop as a reset — but the
long-horizon questions quietly stop working: `increase(husk_action_failures_total[30d])`
after a deploy only sees failures since the deploy.

Set `controller.metrics_state_path` and the event-time half is written to a small
JSON file (a few KB; a modest PVC is plenty) every 60s and once more on shutdown,
then folded back in at startup. Unset ⇒ disabled, everything starts from zero.

Only `Metrics` is persisted — never the snapshot half, which is re-derived from
live state on every scrape. **No slot state is stored here**; slots are still
re-adopted from backend metadata on the first tick, as before.

Writes are atomic (temp file in the same directory, then `os.replace`), and every
failure mode is non-fatal: a corrupt file, a schema-version mismatch, or changed
bucket bounds all mean "start that metric from zero, loudly". There is deliberately
**no migration path** — half-restored data is worse than a clean reset, because a
counter that silently loses part of its history is indistinguishable from one that
is simply low. A save failure (full or read-only PVC) is logged and ignored; a
bookkeeping file must never take down a runner fleet.

### Renames

Per husk's no-back-compat rule, these changed outright rather than being aliased:

| Before | After | Why |
|---|---|---|
| `husk_reconcile_generation` | `husk_reconcile_generation_total` | it declared `TYPE counter` while omitting the suffix |
| `husk_slot_info{...,cycle="N"}` | `husk_slot_cycle{backend,slot}` | a label that churned a new series every recycle |
| `husk_slot_live_fraction` | `husk_slot_state_seconds_total{...,state}` | a baked-in ratio can't be re-windowed |

Label **names** are also now emitted alphabetically (prometheus_client normalizes
them). Label order is not semantically meaningful, but it will change the text of
any hand-written diff or golden-file test.

-----

## qcow2 storage usage

`/metrics` carries one **daemon-wide** block (no `backend` label — see below)
counting the qcow2 images husk has put on disk:

```
husk_images{host="",kind="cache"}            2.0
husk_image_bytes{host="",kind="cache"}       7516192768.0
husk_images{host="gpu-1",kind="golden"}      1.0
husk_image_bytes{host="gpu-1",kind="golden"} 3221225472.0
husk_images{host="gpu-1",kind="overlay"}     4.0
husk_image_bytes{host="gpu-1",kind="overlay"} 88604672.0
```

Three populations, two machines (`storage.py`):

- **`cache`** — the controller-local OCI pull cache (`~/.cache/husk/images`).
  Nothing GCs it, so it grows by one multi-GB golden per image bump forever.
  This gauge is what makes that leak visible; alert on it.
- **`golden`** — the backing files staged into each hypervisor's storage pool.
  `_gc_goldens` prunes unreferenced ones, so this should stay flat across a
  rollout, briefly doubling while the new golden lands.
- **`overlay`** — per-slot COW disks. Grows with runner churn; this is the
  series that actually predicts a full hypervisor disk.

**No `backend` label, deliberately.** The cache is shared by every pool, and two
libvirt pools can target one hypervisor's storage pool dir — a per-pool label
would make `sum(husk_image_bytes)` double-count a disk that only filled once.
`storage.collect` dedupes by `(host, kind)` for the same reason. Labels stop at
`kind`/`host`: per-image series would churn on every image bump for no
analytical gain.

Neither side does I/O during a scrape. Hosts are `stat`'d once per reconcile
tick (one command, riding the existing `sync_images` pass) and `/metrics` reads
the cached result, so a wedged hypervisor can't stall a scrape; the controller
cache scan is local and memoized for 30s. A host that can't be reached keeps its
last-known numbers rather than reporting zero — a transient SSH failure must not
look like a disk that emptied itself.

OpenStack pools report no host rows: Nova/Glance own that storage. The Glance
image listing already returns per-image sizes (`_gc_glance`), so a `kind="glance"`
row is a small follow-up, not yet exposed.

-----

## Boot-timing exfil (the `husk-bootreport` consumer)

The producer shipped in v3: `husk-bootreport.service` is baked and cloud-init
starts it after the runner; it dumps `systemd-analyze` + `cloud-init analyze blame`
to the serial console between `===== husk-bootreport =====` markers. **Nothing
reads it today** — it's write-only (`libvirt_backend.py` leaves `console_log_path`
unset; `openstack_backend.py` never calls `get_console_output`).

The consumer is a control-plane concern (layer 1), so it lives in huskd:

- **Trigger:** once per recycle, after the slot's runner is detected online (huskd
  already has this signal — `timing.on_runner_online`).
- **OpenStack:** `get_console_output(instance)` via the SDK; parse the marked block.
- **libvirt:** read the serial log file (needs `console_log_path` set +
  host-side file ownership fix).
- **Parse → `SlotTiming`:** add per-unit fields (e.g. `network-online.target`
  wait, podman socket wait) and expose as `husk_slot_boot_*` gauges. Also renders
  on the existing dashboard.

Both exfil channels are **control-plane / host-side** (console API, serial file) —
no in-guest network, so they sidestep the runner egress firewall entirely.

-----

## Security model

Asset is low-value (host metrics, not secrets); adversary is other tenants on the
CERN-internal network **and** the untrusted CI job itself. **Decision: no TLS, no
basic-auth — network-layer access control only.** TLS/basic-auth would protect
low-value data and a credential whose entire blast radius is "read another slot's
host metrics"; the margin over the network controls rounds to zero, and it buys
real cost (baking/rotating certs, the dynamic-IP SAN wrinkle). Controls:

1. **Primary: nftables source-IP allowlist on `:9100`.** Only the scraper source
   may connect — central Prometheus on OpenStack, the host proxy on libvirt.
   Network-layer, nothing on the VM to steal, and it's the mechanism husk already
   has (cloud-init ruleset). Sufficient on its own for this asset.
2. **libvirt: private net + the locked-down host proxy.** Guests aren't reachable
   at all except through a proxy pinned to `:9100` on the guest subnet.
3. **Nothing secret is baked or held on any host** — no server cert, no bcrypt
   hash, no push credential. The metrics endpoint serves in the clear to whoever
   the firewall admits (only the scraper).

Spend the effort *instead* on the network assumptions the allowlist rests on:
- **libvirt:** stop sibling slots sniffing/ARP-spoofing each other at L2 on the
  shared bridge (per-slot isolated networks or bridge port isolation) — this
  protects slot↔slot better than TLS would, and it's a host-config item.
- **OpenStack:** confirm CERN's tenant network isolates tenants (Neutron normally
  does), so the source-IP allowlist can't be defeated by an on-path tenant spoofing
  the scraper IP.

If either assumption fails, or defense-in-depth is later wanted, **mTLS is the
add-back** (client key on central only) — not built now.

-----

## Phasing

Independent tracks; ship in any order.

- **Phase O1 ✅ — boot-timing exfil (huskd only, OpenStack first).** `get_console_output`
  → parse `husk-bootreport` → `husk_slot_boot_*` on `/metrics` + dashboard. No
  image change, no per-node setup. Highest value / lowest cost; validates the
  console-parse path. libvirt half follows once the serial-log host fix lands.
- **Phase O2 ✅ — node_exporter in the image.** node_exporter (pinned +
  checksummed in `images/versions.env`) and `husk-node-exporter.service` are baked
  into both variants, running as a dedicated unprivileged user, **no TLS/auth**.
  The `:9100` ingress rule rides the per-cycle cloud-init ruleset, gated on the
  per-pool `scrape_cidr` knob. **Opt-in and fail-closed**: unset → no ingress rule
  and no exporter started (nothing listening), so a pool whose scraper source
  isn't known yet renders exactly today's ruleset. The exporter is started *after*
  the firewall is applied and *before* the runner, so `:9100` is never briefly
  open during boot. Requires `prebaked` (the loader rejects the combination
  otherwise — a stock image has no baked exporter). Takes effect on a rebuild.
- **Phase O3 ✅ — discovery + join (huskd).** A single `http_sd` endpoint
  (`GET /sd/targets`) serving both backends via per-target routing (OpenStack →
  `ip:9100`; libvirt → `host:PORT/<slot>/metrics`) + the `husk_slot_info` join
  table. `Slot`/`SlotView` carry `ip` (OpenStack) and `host` (libvirt), plus a
  per-host `metrics_proxy` config knob. Pure huskd; OpenStack goes fully direct
  after this. Only runner-online slots are published (no scrape-`down` edge noise).
- **Phase O4 ✅ — libvirt guest bridge, in huskd (NOT a per-host proxy).** Central
  Prometheus scrapes `huskd/slot/<pool>/<slot>/metrics`; huskd fetches the guest's
  node_exporter over the SSH channel it **already holds** to the hypervisor (the one
  `libvirt_backend._ssh` uses for qemu-img). **Nothing is deployed on any
  hypervisor** — adding a host stays a pure config change.
  - **Why this and not the per-host proxy originally planned here:** the proxy
    needed a *new* network path (Prometheus → hypervisor:9101) opened on every host,
    forever. Prometheus already scrapes huskd, so that path is proven and free. The
    network-admin + orchestration overhead of the per-host component was judged to
    outweigh its architectural tidiness.
  - **The trade, stated plainly:** this puts huskd in the metrics *data* path for
    libvirt, which the "two layers" section above rejects as a general principle. We
    accept it *for libvirt only*, because the fleet is small (the bottleneck argument
    is theoretical at this size). **OpenStack is unaffected** — those guests are
    routable and still scraped directly; huskd is never in their data path.
  - **What it costs:** a huskd outage gaps libvirt guest metrics (whereas discovery
    alone degrades gracefully), and `up` for those targets folds together "the guest
    is sick" with "huskd/SSH is sick". Losing that distinction is the real price.
  - **Implementation guards:** the scrape is an async subprocess, hard-bounded by a
    timeout, so a wedged host degrades one scrape and never the control plane; the
    SSH connection is multiplexed (`ControlMaster`/`ControlPersist`) so a 15s scrape
    interval doesn't mean a TCP+auth handshake every 15s; and the slot is resolved
    from huskd's own snapshot, never from the URL, so the route is not a general
    relay. The guest-IP lookup inside `list_slots` cannot raise — a metrics nicety
    must never abort a reconcile tick.
- **Phase O5 (optional) — libvirt hypervisor cross-check.** Add
  `prometheus-libvirt-exporter` on the host for per-domain CPU/disk/net independent
  of the guest agent. Not required.

OpenStack reaches full per-VM observability at O1+O2+O3 with **zero** per-node
setup. libvirt reaches it at O2+O3+O4 (baked node_exporter + huskd `http_sd` + the
host proxy); O5 is a conditional add-on, not a requirement.

-----

## Open questions

- **Where does central Prometheus live** relative to the CERN network — can it
  route to runner fixed IPs directly (O3 direct scrape) or must even OpenStack go
  through a tenant-resident scraper? Gates the OpenStack transport. **This is the
  one thing still blocking OpenStack**, and it's a config value, not code: set
  `scrape_cidr` on the pool and recycle (the rule is in cloud-init, *not* baked, so
  getting it wrong costs a recycle, not an image rebuild). Until it's set the pool
  is fail-closed (no rule, no exporter).
  - **If Prometheus runs in k8s, the value is the WORKER-NODE subnet, not the pod
    CIDR.** A pod egressing *out of the cluster* to the runner VM is normally
    SNAT'd to its node, so the guest never sees a pod IP — and pod IPs are
    ephemeral anyway, so they couldn't be allowlisted. Exceptions to check:
    routable pod IPs with no masquerade (allowlist the pod CIDR), or a dedicated
    egress gateway/NAT (allowlist that).
  - **Settle it empirically, not from docs** — let the guest tell you the source:
    add a `tcp dport 9100 counter` rule and see what hits it, or `curl
    <slot-ip>:9100/metrics` from a pod in the cluster and look at where the
    connection came from. One observation ends the question.
- **Does CERN expose tenant telemetry (Ceilometer → Gnocchi / a Prometheus
  endpoint)?** If yes, it's the host-side equivalent of libvirt-exporter for the
  OpenStack VMs — could drop the in-guest node_exporter there too. Verify (a) the
  Gnocchi/metric API answers for our project, (b) which meters (`cpu`,
  `disk.device.*`, `network.*`, `memory.usage`) are collected, and (c) the polling
  interval. Likely minute-scale and memory needs balloon/memstat — so probably a
  lower-fidelity *fallback*, not a replacement, but worth confirming before
  committing to node_exporter as the only OpenStack source.
- **Is the hypervisor view (libvirt-exporter) enough on libvirt**, or do we need
  fs-fill / load / CPU-mode / fine-memory badly enough to also run the O5 guest
  scrape? Decides whether libvirt ever needs the in-guest apparatus at all.
- **node_exporter collector set + cardinality** — default collectors are fine, but
  confirm we're not exploding series per ephemeral slot (short-lived `slot=` label
  churn). Consider dropping high-cardinality collectors and relying on
  `husk_slot_info` for identity.
- **Series lifecycle for ephemeral slots** — stale-marking / `up==0` handling and
  retention so a recycled slot's series ages out cleanly.
- ~~**mTLS vs basic-auth** final call (O2)~~ — **resolved in O2: neither.** No TLS,
  no basic-auth; the nftables source allowlist on `:9100` is the whole access
  control, so nothing secret is baked into an image that runs untrusted CI jobs.
  mTLS remains the add-back if the network assumptions below ever fail.
- **IPv6 scrape sources** are supported (the rule renders `ip6 saddr` when
  `scrape_cidr` is v6 — inside an `inet` table `ip saddr` matches v4 only, so a v6
  source under it would never match and the port would silently close), but a
  **single** family per pool. A dual-stack scraper would need the rule to emit both.
- **GPU utilization** (DCGM exporter in the gpu variant) — in scope or separate?
- **libvirt-exporter choice** — `prometheus-libvirt-exporter` vs alternatives;
  per-domain label alignment with `husk_slot_info`.
