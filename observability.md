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

**Layer-2 refinement — where we own the hypervisor, prefer the hypervisor view.**
"Scrape the guest directly" is the pattern when the guest is the *only* source. But
CPU/disk/net per VM is also visible from the hypervisor, and on **libvirt we own
the host**, so `prometheus-libvirt-exporter` supplies those with no in-guest agent
at all (details per-backend below). It can't see guest-internal state
(filesystem fill, load, CPU-mode split, fine memory), so the in-guest node_exporter
remains the fallback when those matter — and the **only** option on OpenStack,
where we don't own the hypervisor.

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
[central Prometheus] ──HTTP scrape──▶ slot:9100  (node_exporter, TLS + basic-auth)
        │  discovery via huskd http_sd (native OpenStack SD = fallback)
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

### libvirt — host-side exporter (preferred), guest scrape optional

We **own the libvirt host**, so we get per-domain metrics from the *hypervisor's
view* with no in-guest agent and no per-guest reachability at all:

```
[central Prometheus] ◀──remote_write── [vmagent on libvirt host]
                                              ├─ [libvirt-exporter]  per-domain CPU/disk/net
                                              └─ [node_exporter]     host-level metrics
                                        (optional, only if guest-internal metrics needed:)
                                              └─ scrape host-local slot:9100
```

- **Primary path — `prometheus-libvirt-exporter` on the host.** It scrapes libvirtd
  and emits per-domain **CPU time, block I/O, network I/O** (and memory *if* the
  guest balloon/memstat is enabled). For the resource-**shape** question (is a build
  CPU/disk/net-bound) this is sufficient, and it deletes the entire in-guest scrape
  apparatus: **no baked node_exporter dependency, no `:9100` firewall hole, no
  per-guest `file_sd` discovery, no secret** on the libvirt side. It's just another
  exporter on the host next to `node_exporter`.
- **What the hypervisor view can't give** (accept, or fall back to the guest
  scrape): **filesystem fill, load average, CPU-mode breakdown** (user/system/
  iowait/steal), and fine memory. These need an in-guest agent.
- **Optional guest scrape (fallback, only if the above matters).** The host can
  reach its own guests on the private libvirt net, so the same host `vmagent` can
  additionally scrape `slot:9100` (node_exporter, baked in the shared image). This
  re-introduces per-guest `file_sd` discovery (huskd writes the targets file over
  its existing SSH channel) — enable it only when host-view metrics are
  insufficient.
- **Transport = host `vmagent` `remote_write`.** Central Prometheus never reaches
  the guests; huskd is never in the metrics path (it only writes `file_sd` if the
  optional guest scrape is on).

### Summary

| | OpenStack | libvirt |
|---|---|---|
| Own the hypervisor? | no (tenant) | yes |
| Primary per-VM source | **in-guest node_exporter** (no host-side option) | **host-side libvirt-exporter** (no in-guest agent) |
| Guest reachability | CERN-routable IP, direct | host-only (private net) |
| Scrape transport | central Prometheus → `slot:9100` | host vmagent → libvirt-exporter, remote_write up |
| Discovery | huskd `http_sd` (native OpenStack SD = fallback) | none for host exporters; `file_sd` only if guest scrape is enabled |
| Guest scrape needed? | yes | **optional** — only for fs/load/CPU-mode/fine-mem |
| Host-level metrics | n/a (not our hypervisors) | node_exporter + libvirt-exporter on the host |
| `:9100` exposure | CERN-internal — restrict source + auth | none (host-side path) / host-private if guest scrape on |

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
4. **Discovery endpoints.**
   - `http_sd`: a Quart route (e.g. `GET /sd/targets`) returning live
     `{targets, labels}` JSON for running slots — the single discovery source
     Prometheus (OpenStack) and host vmagents can both consume. **Preferred over
     native OpenStack SD** (removes Nova creds from Prometheus, reuses the existing
     Prometheus→huskd connection, carries huskd-native labels, emits only
     runner-online slots). Keep **target labels minimal** (identity to join —
     `slot`, `ip`); leave rich attribution (`job_id`, `cycle`) to `husk_slot_info`
     so per-recycle churn doesn't inflate `node_*` series cardinality.
   - `file_sd` writer (libvirt): **only needed if the optional guest scrape is
     enabled** (the default libvirt path uses host-side libvirt-exporter and needs
     no per-guest discovery). Renders the same target list to a file on each host
     over the existing SSH channel, for host-local vmagents.
5. **The dynamic firewall ingress rule.** The `:9100`-from-Prometheus allow is
   *policy*, so it rides the existing per-cycle cloud-init ruleset
   (`husk-egress.nft`), not the image. Scoped to the Prometheus/vmagent source IP.
   huskd renders it (one config knob: the scraper source CIDR). **OpenStack path
   always; libvirt only if the optional guest scrape is enabled.**

### Baked into the golden image (built once, in CI — `images/build.sh`)

Static capability, per the image/cloud-init boundary (`image-pipeline.md`):

6. **node_exporter binary + `husk-node-exporter.service`** (a new file under
   `images/files/`), **not enabled for boot** — cloud-init starts it like the
   runner unit, or it's enabled to start on boot since it has no per-cycle input.
   Runs as `root` or a dedicated `node_exporter` user — **never** as `runner`.
7. **node_exporter `--web.config.file`** (`root:root 0600`) carrying TLS server
   cert + `basic_auth_users` (bcrypt). Safe to bake: the runner is unprivileged
   (`useradd ... runner; passwd -l runner`; no sudo/wheel), so uid 1000 can't read
   a `0600` root file, and `basic_auth` stores a hash, not the plaintext. The
   Prometheus-side credential (basic-auth password / mTLS client key) **never**
   goes into the image.
8. **(GPU variant) the DCGM/nvidia metrics exporter** if we want GPU utilization —
   deferred; note it here so the boundary is explicit.

### Per-node setup (once per libvirt host — possibly automated later)

Static machine state that isn't a VM image. Today this is manual; automating it is
tracked as **deferred Ansible host provisioning** (`plan.md` / memory) and this
plan does **not** un-defer it — it just enumerates what belongs there:

9. **A metrics agent on each libvirt host:** `vmagent` (or prometheus-agent)
   running `node_exporter` (host) + `prometheus-libvirt-exporter` (per-domain,
   hypervisor view) and `remote_write`-ing to central Prometheus. This is the
   **default, complete** libvirt per-VM path — no in-guest agent, no per-guest
   discovery. Only if fs/load/CPU-mode/fine-memory are required does the agent
   *additionally* tail huskd's `file_sd` and scrape `slot:9100`.
10. **The serial-log file ownership/relabel fix** so libvirt/qemu can write the
    per-domain console log that huskd's boot-timing exfil (item 2) reads — the exact
    thing the `libvirt_backend.py:724` comment defers to host setup. (Independent of
    the metrics agent; needed for boot-timing on libvirt regardless.)
11. **Network path** from the host to central Prometheus for `remote_write` (and, if
    the optional guest scrape is on, from the host agent to the guest subnet —
    usually already there via the host's own libvirt bridge).

> When the deferred host-provisioning work lands, items 9–11 become Ansible roles.
> Until then they're a documented per-host checklist. **Nothing in 9–11 blocks the
> OpenStack path**, which needs no per-node setup at all (direct scrape + baked
> image + huskd discovery).

### At-a-glance

| Concern | huskd | Image (CI) | Per libvirt host |
|---|---|---|---|
| Boot-timing metrics | ✅ parse + expose | — | serial-log fix (libvirt only) |
| `husk_slot_info` join | ✅ | — | — |
| Discovery (http_sd/file_sd) | ✅ | — | consume file_sd |
| `:9100` ingress rule | ✅ (cloud-init) | — | — |
| node_exporter + web.config | — | ✅ baked (used by OpenStack; libvirt only if guest scrape on) | — |
| libvirt-exporter + node_exporter on host | — | — | ✅ (default libvirt per-VM path) |
| Scrape transport | — | — | vmagent (libvirt); direct (OpenStack) |
| Host + per-domain metrics | — | — | node_exporter + libvirt-exporter |

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
CERN-internal network **and** the untrusted CI job itself. Controls, in order of
importance:

1. **Primary: nftables source-IP allowlist on `:9100`.** Only the Prometheus /
   vmagent source may connect. Network-layer, nothing on the VM to steal, and it's
   the mechanism husk already has (cloud-init ruleset). Sufficient on its own for
   this asset.
2. **Auth/encryption: baked basic-auth over TLS, or mTLS.** node_exporter
   `--web.config.file` supports `tls_server_config` (incl. `client_ca_file` for
   mTLS) and `basic_auth_users` (bcrypt). A baked basic-auth secret is adequate
   **because the runner is unprivileged** — uid 1000 can't read the `root:0600`
   config, and it holds a bcrypt hash anyway. mTLS is the marginal upgrade: the
   client key lives only on Prometheus, so even the hash never sits on a
   job-executing box, plus it encrypts the CERN-internal wire.
3. **What never touches the image:** the Prometheus-side credential (basic-auth
   password / mTLS client key). Only server cert + CA + bcrypt hash are baked.

Residual risk: a local privilege escalation in the guest (kernel/container-escape)
defeats the `0600`. Accepted — the ephemeral single-use slot bounds the blast
radius, and it's a high bar for a host-metrics endpoint. Not engineered around.

-----

## Phasing

Independent tracks; ship in any order.

- **Phase O1 — boot-timing exfil (huskd only, OpenStack first).** `get_console_output`
  → parse `husk-bootreport` → `husk_slot_boot_*` on `/metrics` + dashboard. No
  image change, no per-node setup. Highest value / lowest cost; validates the
  console-parse path. libvirt half follows once the serial-log host fix lands.
- **Phase O2 — node_exporter in the image.** Bake node_exporter +
  `husk-node-exporter.service` + `--web.config.file` into both variants; add the
  `:9100` ingress rule to the cloud-init ruleset (config knob: scraper source
  CIDR). Produces scrapeable slots. No huskd delivery change beyond the ruleset.
- **Phase O3 — discovery + join (huskd).** `http_sd` endpoint + `husk_slot_info`
  join table. Turns "scrapeable" into "discovered + attributable." OpenStack goes
  fully direct after this.
- **Phase O4 — libvirt host exporters.** Per-host `vmagent` + `node_exporter` +
  `prometheus-libvirt-exporter` (host + per-domain, hypervisor view) with
  `remote_write`. This is the **complete default** libvirt per-VM path — no in-guest
  agent, no `file_sd`, no `:9100` rule. Per-node-setup track; folds into the
  deferred Ansible host-provisioning work when that lands.
- **Phase O5 (optional) — libvirt guest scrape.** Only if fs/load/CPU-mode/fine-mem
  are needed beyond the hypervisor view: enable the host `vmagent` to also scrape
  `slot:9100` (baked node_exporter from O2), add the huskd `file_sd` writer and the
  `:9100` ingress rule. Skip unless O4's host view proves insufficient.

OpenStack reaches full per-VM observability at O1+O2+O3 with **zero** per-node
setup. libvirt reaches it at O4 (host-side, no image/guest work); O5 is a
conditional add-on, not a requirement.

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
