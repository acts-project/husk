# Husk Observability

How husk exposes metrics for the fleet: the long-lived infrastructure (controller,
libvirt hosts) and the ephemeral, single-use runner slots. Companion to
`plan.md` and `image-pipeline.md`; this document defines the metrics story those
left open.

> **Status (2026-07-10):** Design. Nothing here is built yet **except** the
> controller `/metrics` endpoint, which already exists (`src/husk/web/app.py`,
> `render_prometheus`) and exposes state-derived per-pool / per-slot gauges
> (`husk_slots*`, `husk_slot_last_cloudinit_seconds`,
> `husk_slot_last_recycle_seconds`, `husk_slot_live_fraction`). This plan extends
> that surface and adds two new capabilities: **(1)** in-guest resource metrics
> (node_exporter) scraped per running slot, and **(2)** boot-timing exfil from the
> serial console (`husk-bootreport`, baked but currently write-only). The two are
> independent and separately useful.

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

- huskd proxying/relaying node_exporter scrapes (explicitly rejected — see
  "The two layers").
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
*discovery/label service* for layer 2 — never a metrics proxy for layer 2.**
Relaying guest metrics through huskd would add a bottleneck, a failure mode, and
lose node_exporter's native `up`/staleness semantics, all for zero benefit on a
per-instant signal.

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

### libvirt — in-guest node_exporter, reached through a dumb host proxy

Guests sit on a **private libvirt net**: only the host can reach `slot:9100`. So
the host must bridge — but it should be a **dumb, stateless proxy, not a metrics
system**. No agent, no local TSDB, no push credentials. The baked node_exporter
(same image as OpenStack) is the primary source here too, because we need
filesystem fill:

```
[central Prometheus] ──scrape──▶ [path-routing proxy on host] ──forward──▶ slot:9100 (node_exporter)
        │  target from huskd http_sd:  __address__=host:PORT, __metrics_path__=/<slot>/metrics
        │  proxy is locked to guest-subnet:9100 only — no secrets, no state, no TSDB
        └── join: node_* × husk_slot_info (from huskd /metrics)
```

- **Primary source — in-guest node_exporter** (baked, same image). Full guest view:
  **filesystem fill**, load, CPU-mode split, fine memory, plus CPU/disk/net.
- **Transport — a stateless path-routing reverse proxy on the host.** Central
  scrapes `host:PORT/<slot>/metrics`; the proxy forwards to `slot:9100` on the
  private net. It is **pull** (central → host), so the host holds **no
  reach-central credential**; it is stateless (no TSDB); and it's locked to
  forward only to the guest subnet on `:9100` (no open relay). Off-the-shelf
  `tinyproxy` or ~40 lines of Python. This deliberately replaces the heavier
  options (a `vmagent` remote_write agent needs a push secret on every host; a
  per-host Prometheus + `/federate` adds a TSDB and is lossy for raw per-job
  series) — see "Why a proxy, not an agent."
- **Discovery — huskd `http_sd`, the same endpoint as OpenStack.** Because
  `__address__` and `__metrics_path__` are per-target relabelable, one `http_sd`
  feed serves both backends: OpenStack targets point at the guest directly;
  libvirt targets point at `host:PORT/<slot>/metrics`. huskd owns the slot→host
  mapping, so it emits the target already routed through the right host proxy.
  (`proxy_url` + a CONNECT tunnel is the alternative but is per-scrape-job, forcing
  one job per host — the path-routing proxy keeps it to a single feed.)
- **`:9100` ingress rule on the guest** — scoped to the host proxy's source
  (the libvirt-bridge address), rendered by huskd into the cloud-init ruleset.
- **Optional secondary — `prometheus-libvirt-exporter` on the host** for a
  hypervisor-view cross-check (per-domain CPU/disk/net when a guest's agent is
  down). Not required.

### Why a proxy, not an agent (remote_write / federation)

The host has to bridge; the question was *what*. We rejected the two heavier
options on their costs:

| | **Path-routing proxy** (chosen) | vmagent `remote_write` | Prometheus + `/federate` |
|---|---|---|---|
| Host holds a reach-central secret | **no** (pull) | yes (push credential per host) | no (pull) |
| Host footprint | stateless proxy | agent + disk buffer | full TSDB + query |
| Raw per-job series | ✅ live passthrough | ✅ | ⚠️ aggregate-oriented, lossy at scrape edges |
| huskd discovery | single `http_sd` (per-target routing) | per-host `file_sd` | per-host `file_sd` |

The decider was **no secret orchestration on the hosts** (write credentials on
every hypervisor are admin overhead and a needless spread) plus **no TSDB
footprint** for what is a simple pull. The proxy gives both: central pulls, the
host holds nothing. Only if central *cannot* reach the hosts (pull impossible)
would remote_write come back into play.

### Summary

| | OpenStack | libvirt |
|---|---|---|
| Own the hypervisor? | no (tenant) | yes |
| Primary per-VM source | **in-guest node_exporter** | **in-guest node_exporter** (same baked image) |
| Guest reachability | CERN-routable IP, direct | host-only (private net) → dumb host proxy |
| Scrape transport | central → `slot:9100` | central → `host:PORT/<slot>/metrics` → `slot:9100` |
| Discovery | huskd `http_sd` (single feed, per-target routing) | huskd `http_sd` (same feed) |
| Host component | none | stateless path-routing proxy (no secret, no TSDB) |
| Access control | nftables `:9100` allow (Prometheus source) | nftables `:9100` allow (host-proxy source) + proxy locked to subnet |

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
                  runner_name="...", job_id="...", cycle="...",
                  image_digest="..."} 1
   ```
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

### Per-node setup (once per libvirt host — possibly automated later)

Static machine state that isn't a VM image. Today this is manual; automating it is
tracked as **deferred Ansible host provisioning** (`plan.md` / memory) and this
plan does **not** un-defer it — it just enumerates what belongs there:

9. **A dumb metrics proxy on each libvirt host** — a stateless path-routing reverse
   proxy (`tinyproxy`, or ~40 lines of Python) that forwards
   `host:PORT/<slot>/metrics` → `guest:9100` and is **locked to the guest subnet on
   `:9100`** (no open relay). No secret, no state, no TSDB. Central pulls through
   it. Optionally also run `prometheus-libvirt-exporter` for a hypervisor cross-check.
10. **The serial-log file ownership/relabel fix** so libvirt/qemu can write the
    per-domain console log that huskd's boot-timing exfil (item 2) reads — the exact
    thing the `libvirt_backend.py:724` comment defers to host setup. (Independent of
    the proxy; needed for boot-timing on libvirt regardless.)
11. **Network path** so central Prometheus can reach the host proxy (pull), and L2
    isolation between sibling slots on the shared libvirt bridge (see Security).

> When the deferred host-provisioning work lands, items 9–11 become Ansible roles.
> Until then they're a documented per-host checklist. **Nothing in 9–11 blocks the
> OpenStack path**, which needs no per-node setup at all (direct scrape + baked
> image + huskd discovery).

### At-a-glance

| Concern | huskd | Image (CI) | Per libvirt host |
|---|---|---|---|
| Boot-timing metrics | ✅ parse + expose | — | serial-log fix (libvirt only) |
| `husk_slot_info` join | ✅ | — | — |
| Discovery (`http_sd`, single feed) | ✅ | — | — |
| `:9100` ingress rule | ✅ (cloud-init) | — | — |
| node_exporter (no TLS/auth) | — | ✅ baked (primary per-VM source, both backends) | — |
| Metrics proxy (libvirt) | emits proxy-routed target | — | ✅ stateless path-routing proxy (no secret/TSDB) |
| Scrape transport | — | — | central → host proxy → guest (libvirt); direct pull (OpenStack) |
| Optional per-domain metrics | — | — | optional `prometheus-libvirt-exporter` |

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

- **Phase O1 — boot-timing exfil (huskd only, OpenStack first).** `get_console_output`
  → parse `husk-bootreport` → `husk_slot_boot_*` on `/metrics` + dashboard. No
  image change, no per-node setup. Highest value / lowest cost; validates the
  console-parse path. libvirt half follows once the serial-log host fix lands.
- **Phase O2 — node_exporter in the image.** Bake node_exporter +
  `husk-node-exporter.service` (no TLS/auth) into both variants; add the `:9100`
  ingress rule to the cloud-init ruleset (config knob: scraper source CIDR).
  Produces scrapeable slots. No huskd delivery change beyond the ruleset.
- **Phase O3 — discovery + join (huskd).** A single `http_sd` endpoint
  (`GET /sd/targets`) serving both backends via per-target routing (OpenStack →
  `ip:9100`; libvirt → `host:PORT/<slot>/metrics`) + the `husk_slot_info` join
  table. Needs `Slot`/`SlotView` to carry `ip` (OpenStack) and `host` (libvirt),
  and a per-host `metrics_proxy` config knob. Pure huskd; OpenStack goes fully
  direct after this.
- **Phase O4 — libvirt host proxy.** Deploy the stateless path-routing proxy on
  each host (`tinyproxy` or ~40 lines of Python), locked to the guest subnet on
  `:9100`. This completes the libvirt per-VM path — no agent, no secret, no TSDB.
  Per-node-setup track; folds into the deferred Ansible host-provisioning work.
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
  through a tenant-resident scraper? Gates the OpenStack transport.
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
- **mTLS vs basic-auth** final call (O2) — mTLS if we want the credential off the
  guest entirely; basic-auth if simplicity wins given the unprivileged runner.
- **GPU utilization** (DCGM exporter in the gpu variant) — in scope or separate?
- **libvirt-exporter choice** — `prometheus-libvirt-exporter` vs alternatives;
  per-domain label alignment with `husk_slot_info`.
