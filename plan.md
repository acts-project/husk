# Husk — Ephemeral GitHub Actions Runners on OpenStack

A staged validation plan for building an org-wide, multi-backend GitHub Actions
runner system on CERN OpenStack. Focus is on getting the OpenStack/Linux path
working end-to-end first, with the architecture designed so GPU bare-metal and
macOS backends can be added later without rework.

> **Naming**: `husk` — VMs are persistent husks that get reseeded with a
> fresh runner between jobs. The husk endures; what runs inside it is
> ephemeral. Daemon = `huskd`, CLI = `huskctl`.

-----

## Architecture Overview

### Goals

- **Clean state per job**: every job runs on a VM whose disk has been freshly
  reset, so no state leaks between jobs
- **Fork-safe**: suitable for public/contributed repositories without exposing
  org infrastructure to attacker-controlled code
- **Org-wide**: serves multiple repositories in a GitHub organization via
  runner groups and labels
- **Multi-backend**: pluggable infrastructure backends (start with OpenStack;
  later add GPU bare metal and macOS)
- **Capacity-aware**: respects per-backend quotas and per-host inventory
- **Low-key**: minimal external dependencies, no inbound webhooks required,
  understandable by one person, no production credentials beyond what's needed

### Key insight from validation: slots, not ephemeral VMs

The initial design assumed ephemeral VMs created and destroyed per job. After
measuring CERN OpenStack's boot characteristics, we found:

- **Fresh VM create takes ~5 minutes**, dominated by Neutron port provisioning
  (the `networking` task_state). Independent of image content, image caching,
  or CPU class. This is a CERN-infrastructure-level cost we cannot reduce.
- **Nova `rebuild` on the same VM takes ~25 seconds**, ~13× faster, because
  the Neutron port is preserved. The disk is wiped and re-imaged; cloud-init
  reruns with fresh user-data.

**The architecture is therefore "slots that recycle" rather than "ephemeral
VMs"**: a fixed pool of long-lived OpenStack VMs ("slots"), each in one of
three states (idle / busy / rebuilding). Between jobs, the slot's disk is
wiped and re-imaged via `server rebuild` with a fresh JIT config in user-data.

This preserves the security property we wanted from ephemerality (clean
filesystem per job, no state leaks between jobs) while sidestepping the slow
network provisioning. Slots only get fully destroyed on hypervisor failure or
manual decommission.

### Security and privilege model

Husk runs the actions-runner as an **unprivileged user** with **no sudo**,
**no docker group**, and **kernel-enforced egress filtering** installed by
host root at cloud-init time. Container support is provided via **rootless
Podman**, which presents a Docker-compatible API so workflow `container:`
directives work transparently.

This differs from the `ubuntu-latest` GitHub-hosted model (which gives the
runner sudo and Docker daemon access). It parallels the `ubuntu-slim` model
more closely: container-based isolation, restricted privileges, smaller
capability surface.

The privilege model:

- **Host root**: only cloud-init at boot. Configures the firewall, creates
  the runner user, starts the runner service, then nothing further runs as
  host root for the lifetime of the job.
- **Runner user**: regular unprivileged user. No sudo, no wheel, no docker
  group. Can run Podman (rootlessly), can use the container Podman creates,
  can write to its own home directory. Cannot modify host firewall, install
  packages on the host, or affect anything outside its own files.
- **Inside containers spawned by workflows**: rootless Podman gives them
  user-namespaced capabilities. "Root" in the container is the runner user
  on the host, with namespace-bounded privileges.

The egress firewall:

- Installed by cloud-init at boot, owned by host root
- **Primary policy is default-allow with a CERN-internal denylist**: drop
  traffic to CERN-internal CIDRs (with logging), allow everything else.
  The threat being mitigated is workflows pivoting into CERN-internal
  infrastructure, not workflows reaching the public internet. A strict
  allowlist would be a maintenance burden against pypi/npm/docker.io/CDN
  sprawl with minimal extra safety given the credential-scoping and
  slot-rebuild defenses.
- An explicit *allow-before-deny* list covers CERN-public services on
  CERN IPs (`gitlab.cern.ch`, the CERN-customized OS mirrors, etc.) —
  policy lives in `husk-policy/policy.toml`, see [Network policy
  rollout](#network-policy-rollout)
- DNS resolver pinned to a specific upstream; other DNS destinations are
  dropped (prevents split-horizon bypass via `8.8.8.8` etc.)
- IPv6 disabled on the slot entirely (simpler than maintaining a parallel
  v6 denylist)
- Cannot be modified by the runner user (no CAP_NET_ADMIN, no sudo)
- Applies to all outbound traffic from the slot, whether from the runner
  process directly or from any container Podman spawns

A stricter default-deny mode is feasible — same enforcement machinery,
inverted policy — but is not the primary plan; see open questions for
when it might be worth enabling per runner group.

**Workflow compatibility cost** (deliberate, accepted):

- No host-level `sudo` (apt-uninstall etc.) — use a lean base image instead
- No Docker-in-Docker (workflows can't run `docker:dind` services)
- No `--privileged` containers expecting host root capabilities
- No binding to privileged ports on the slot's host interface
- No service containers (`services:` workflow directive) — not needed for
  our use case

Workflows that need any of the above are out of scope for Husk and should
run on the existing manually-managed infrastructure or a different
deployment.

**What this buys us**:

- Network restrictions that have teeth — kernel-enforced, runner-unbypassable
- Don't need OpenStack security group admin (which we lack)
- Slot rebuild provides per-job clean state at the disk level
- Force-destroy from controller handles non-cooperative shutdown
- Defense in depth: even a compromised runner inside the slot is bounded
  by the host firewall and the slot's eventual destruction

### Network policy rollout

The egress policy is the same conceptual rule across all three backends,
but each backend enforces it through its native primitives. The
enforcement surface and delivery mechanism differ:

|Backend         |Where enforced                       |Mechanism                                                    |Delivery                                                                          |
|----------------|-------------------------------------|-------------------------------------------------------------|----------------------------------------------------------------------------------|
|OpenStack (husk)|Inside the slot, by host root        |`nftables` ruleset loaded at boot                            |Rendered into cloud-init `user_data` by the controller before each create/rebuild |
|Libvirt (GPU)   |On the bare-metal host, outside the VM|libvirt `nwfilter` XML or host `nftables` pinned to the VM's tap|Ansible playbook, run on cron / change                                            |
|Cilicon (macOS) |On the host Mac, outside the VM      |`pf` anchor under `/etc/pf.anchors/husk`                     |Ansible playbook (or MDM payload if Jamf is in play)                              |

For libvirt and Cilicon, enforcement sits *outside* the guest on a host
we own — strictly stronger than husk, where the OpenStack hypervisor is
not ours and the rules live inside the slot, relying on the
unprivileged-runner property.

**Single source of truth.** A small repo (`husk-policy/`) holds:

- `policy.toml`: structured definitions
  - `cern_internal_deny`: CIDR ranges to drop with logging
  - `cern_public_allow`: DMZ ranges to accept *before* the denylist
    (e.g. `gitlab.cern.ch`, the CERN-customized AlmaLinux mirrors)
  - `dns_resolver`: pinned upstream resolver IP
- Jinja templates for `nftables`, libvirt `nwfilter`, and `pf` anchors

Each backend renders from the same `policy.toml`. CERN's ranges are
stable, so no `meta`-style refresh job is needed for the denylist itself.

**Eventual consistency is fine.** Ansible runs on cron for the non-husk
backends; husk slots pick up the current policy at next create/rebuild.
Brief divergence between backends after a policy change fails closed —
one backend may reject what another accepts for a few minutes, never
the other way round.

**Implemented (POC, 2026-06-04).** A coarse first cut of the husk egress
policy now ships in the controller's cloud-init (`src/husk/cloudinit.py`): an
`nftables` ruleset loaded by host root just before the (untrusted) runner
starts — *after* provisioning, so package/runner installs keep full network
(CERN mirrors included). It is default-*allow* with explicit drops of the
CERN-internal CIDRs (`53`/`123` kept open, since CERN's own resolvers live in
those ranges); idempotent, so each rebuild reapplies it. This is the
"deny CERN-internal, allow public" property — not yet the full `husk-policy`
single-source-of-truth repo, nor the default-deny stricter mode below. Verified
by the `husk-firewall` workflow (see Phase 5). Rolled onto live slots with
`huskctl recycle --all`.

**Stricter mode (future).** The same machinery can render a
default-deny ruleset from a second profile in `policy.toml`, with the
GitHub `meta` refresh job becoming relevant for the allowlist. Selected
per runner group. Not implementing now — see open questions.

### High-level component diagram

```
                ┌────────────────────────────────────────────┐
                │  Controller (Python service, one VM)       │
                │                                            │
                │  ┌──────────────────────────────────────┐  │
                │  │  GitHub App auth                     │  │
                │  │  (App ID + private key → installation│  │
                │  │   tokens, cached, refreshed)         │  │
                │  └──────────────────────────────────────┘  │
                │  ┌──────────────────────────────────────┐  │
                │  │  OpenStack app-credential auth       │  │
                │  │  (clouds.yaml, SDK handles tokens)   │  │
                │  └──────────────────────────────────────┘  │
                │  ┌──────────────────────────────────────┐  │
                │  │  Reconciliation loop (every 30s)     │  │
                │  │  - Poll GitHub for queued jobs       │  │
                │  │  - Poll GitHub for runner states     │  │
                │  │  - For each backend: list slots      │  │
                │  │  - For each slot: classify state     │  │
                │  │    (idle/busy/needs-recycle/dead)    │  │
                │  │  - Issue rebuilds and (rarely)       │  │
                │  │    fresh creates                     │  │
                │  └──────────────────────────────────────┘  │
                │  ┌──────────────────────────────────────┐  │
                │  │  Backend interface (abstract)        │  │
                │  └────┬──────────────┬─────────────┬────┘  │
                └───────┼──────────────┼─────────────┼───────┘
                        │              │             │
              ┌─────────▼────┐ ┌───────▼─────┐ ┌────▼──────┐
              │  OpenStack   │ │  Libvirt /  │ │   (None — │
              │  backend     │ │  GPU bare   │ │   Cilicon │
              │  (Nova)      │ │  metal      │ │   runs    │
              │              │ │  (future)   │ │   itself) │
              └──────────────┘ └─────────────┘ └───────────┘
                        │              │
                        ▼              ▼
                  ┌──────────┐   ┌──────────┐
                  │ Glance   │   │ qcow2    │
                  │ image    │   │ image    │
                  │          │   │ + VFIO   │
                  └──────────┘   └──────────┘
                        │              │
                        └──────┬───────┘
                               │
                               ▼
              ┌──────────────────────────────────────────┐
              │  Inside each slot (long-lived OpenStack VM)
              │                                          │
              │  Per-job cycle:                          │
              │   1. cloud-init applies fresh JIT blob   │
              │      from user-data                      │
              │   2. systemd starts runner               │
              │   3. Runner picks up 1 job, runs it      │
              │   4. Runner exits (ephemeral mode)       │
              │   5. ExecStopPost → poweroff             │
              │   6. Controller sees SHUTOFF →           │
              │      issues server rebuild with new JIT  │
              │   7. ~25s later, slot is idle again      │
              └──────────────────────────────────────────┘
```

A separate **Cilicon** deployment will handle macOS hosts later, with its own
GitHub App. The controller doesn't manage Cilicon — Cilicon-created runners
just appear in the same org runner pool, distinguished by labels.

### Key design decisions

|Decision              |Choice                                                         |Rationale                                                                                                 |
|----------------------|---------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|
|Slot lifecycle        |Rebuild-between-jobs, not destroy/create                       |~25s recycle vs ~5min create on CERN OpenStack                                                            |
|Runner privilege      |Unprivileged user, no sudo, no docker group                    |Kernel-enforced restrictions become possible because runner can't tamper with them                        |
|Container runtime     |Rootless Podman with Docker API emulation                      |Workflow `container:` works unchanged; no host Docker daemon needed                                       |
|Network egress        |nftables firewall, default-deny, installed by host root at boot|Restrictions runner can't disable; doesn't require OpenStack security group admin                         |
|GitHub credential     |GitHub App (org-scoped, private)                               |Higher rate limit, fine-grained perms, not tied to a user. PAT for early phases.                          |
|OpenStack credential  |Application credential                                         |Long-lived, project-scoped, revocable. SDK handles token refresh. Validated in Phase 0b.                  |
|Registration mechanism|JIT config (`generate-jitconfig`)                              |One API call, implicit ephemeral, bound to specific runner identity                                       |
|JIT delivery to slot  |`user_data` on rebuild request (Nova ≥ 2.57)                   |Fresh blob per recycle. CERN runs Nova 2.96.                                                              |
|Trigger model         |Polling (every 30s)                                            |No inbound webhook needed; works in restricted network envs                                               |
|Runner-to-job ratio   |1:1 (ephemeral runner)                                         |Clean state per job; fork-safe                                                                            |
|Recycle trigger       |`ExecStopPost=/sbin/poweroff` on runner exit                   |Decouples runner-done from infra-cleanup                                                                  |
|Liveness backstop     |Controller force-destroy after timeout                         |Inside-VM mechanisms (`shutdown -h +N`) are bypassable; controller-side enforcement is the real safety net|
|Backend abstraction   |Python `abc.ABC` with `boot/list/rebuild/destroy` interface    |Future backends slot in; pluggy considered and rejected (no third-party plugins, no multi-stage hooks)    |
|Capacity model        |Per-backend (quota for OpenStack; inventory for bare metal)    |OpenStack has a global quota; bare metal hosts have fixed GPU counts                                      |
|Nova microversion     |Pin to 2.79 in clouds.yaml                                     |Above what we use (2.57), below stricter hostname semantics (2.90+)                                       |

### Reconciliation loop (revised for slot model)

Every 30 seconds, for each backend:

```
queued_jobs   = github.list_queued_jobs(labels matching this backend)
runners       = github.list_runners(labels matching this backend)
slots         = backend.list_slots()        # all VMs tagged managed-by=husk

# Classify each slot
for slot in slots:
    runner = match_runner_to_slot(runners, slot)
    if slot.status == "SHUTOFF":
        slot.state = "needs-recycle"        # job done, ready for rebuild
    elif runner and runner.busy:
        slot.state = "busy"                 # job in progress
    elif runner and runner.online:
        slot.state = "idle"                 # warm, waiting for job
    elif slot.created_recently:
        slot.state = "starting"             # just provisioned, runner not up yet
    else:
        slot.state = "unhealthy"            # ACTIVE but no runner

# Compute desired actions
busy_count    = count(state="busy")
idle_count    = count(state="idle")
wanted_idle   = max(min_ready, len(queued_jobs))
desired_total = min(max_total, busy_count + wanted_idle)

# Issue rebuilds for slots needing recycle
for slot in slots if slot.state == "needs-recycle":
    jit = github.generate_jitconfig(labels=...)
    user_data = render_cloud_init(jit)
    backend.rebuild(slot, user_data=user_data)

# Reap idle-too-long slots: rebuild them to refresh credentials
for slot in slots if slot.state == "idle" and slot.idle_age > IDLE_TIMEOUT:
    github.delete_runner(slot.runner_id)  # triggers runner exit → poweroff → recycle

# Reap unhealthy slots: rebuild them
for slot in slots if slot.state == "unhealthy" and slot.unhealthy_age > STARTUP_GRACE:
    backend.rebuild(slot, user_data=...)   # try recovery via rebuild
    # If rebuild fails repeatedly, fall through to destroy/create

# Grow pool to desired_total
need_new_slots = desired_total - len(slots)
for _ in range(max(0, need_new_slots)):
    jit = github.generate_jitconfig(labels=...)
    backend.create(user_data=render_cloud_init(jit))

# Shrink pool: destroy oldest idle slots if over desired_total
# (only if we're consistently over, to avoid thrashing)
```

### Timeouts (defaults to validate in Phase 5)

|Timer          |Default     |Purpose                                                                  |
|---------------|------------|-------------------------------------------------------------------------|
|Poller tick    |30s         |Reconciliation frequency                                                 |
|Idle timeout   |30 min      |Force-recycle idle slots (refreshes credentials)                         |
|Startup grace  |5 min       |Slot created/rebuilt but no runner registered → consider unhealthy       |
|Hard wall-clock|2–6 hours   |`shutdown -h +N` in cloud-init, belt-and-suspenders for runaway jobs     |
|Job timeout    |per workflow|`timeout-minutes:` in workflow YAML                                      |
|Slot age       |unbounded?  |Maybe periodically destroy/recreate slots to refresh hypervisor placement|

### Performance characteristics (measured on CERN OpenStack)

From Phase 0b + initial validation:

|Operation                      |Time                            |Notes                                                        |
|-------------------------------|--------------------------------|-------------------------------------------------------------|
|Fresh `server create` to ACTIVE|~5 min                          |Dominated by Neutron `networking` task_state (3-4 min)       |
|`server rebuild` to ACTIVE     |~25s                            |No `networking` phase; just `rebuilding` + `rebuild_spawning`|
|Keystone token expiry          |~1 hour                         |SDK refreshes transparently                                  |
|OpenStack image quota          |unknown, ≥1 image               |CERN policy, not API-enforced                                |
|Compute quota (current)        |5 instances, 10 vCPU, 20 GiB RAM|Tight; controller + 4 slots fits                             |

Throughput modelling at quota=5 (1 controller + 4 slots):

|Job duration|Old model (create/delete)|New model (rebuild)|
|------------|-------------------------|-------------------|
|1 min job   |40 jobs/hour max         |~160 jobs/hour max |
|10 min job  |16 jobs/hour max         |~23 jobs/hour max  |
|30 min job  |8 jobs/hour max          |~8 jobs/hour max   |

User-visible latency with `min_ready=2`: 2 jobs start instantly; the 3rd waits
~25s for a slot to recycle; old model was ~5 min for #3.

### Project layout (suggested)

```
husk/
├── README.md
├── pyproject.toml
├── src/husk/
│   ├── __init__.py
│   ├── controller.py        # reconciliation loop
│   ├── github_client.py     # App auth, JIT generation, runner mgmt
│   ├── backends/
│   │   ├── __init__.py      # Backend ABC
│   │   ├── openstack.py     # OpenStackBackend (slot-based)
│   │   └── libvirt.py       # future: LibvirtBackend for GPU
│   ├── slots.py             # slot state classification
│   └── cli.py               # huskctl
├── provisioning/
│   ├── cloud-init.yaml.j2   # template with JIT placeholder
│   └── runner.service       # systemd unit for the runner
├── tests/
│   ├── timed-boot.py        # measurement / Phase 0-1 testing
│   └── ...
├── docs/
│   ├── architecture.md      # this file's content
│   ├── failure-modes.md     # Phase 5 outcomes
│   └── operations.md        # runbook
└── deploy/
    └── systemd/
        └── huskd.service    # for the controller VM
```

-----

## Validation Plan

A staged plan that builds confidence incrementally. Each phase has a clear
exit criterion. **Don't skip ahead.**

### Phase 0 — Reconnaissance ✅

Status: completed.

- [x] OpenStack CLI access confirmed
- [x] Quotas: 5 VMs, 10 vCPU, 20 GiB RAM, 250 GiB volumes (tight on volumes)
- [x] Base images: AlmaLinux 10 available; default user `almalinux`
- [x] One network: `CERN_NETWORK` (provider VLAN)
- [x] Security groups: `default`, `ssh`, `icmp`, `rdp` (minimal, fine)
- [x] Image quota: at least one snapshot supported empirically
- [x] Outbound to `api.github.com` works from a test VM
- [x] cloud-init present and functional on central image

**Findings to remember:**

- Volume quota at 230/250 GiB used → use ephemeral disks, not boot-from-volume
- Nova microversion: 2.96 (more than enough for everything we need)

-----

### Phase 0b — Non-interactive OpenStack authentication ✅

Status: completed.

The controller cannot use interactive SSO. Validated that application
credentials work end-to-end.

- [x] Application credentials supported in CERN deployment
- [x] Test app credential created (`gha-controller-test`, 7-day expiry)
- [x] `clouds.yaml` configured with app credential auth
- [x] CLI authentication works (`openstack --os-cloud cern-appcred-test ...`)
- [x] Python SDK authentication works
- [x] Token refresh works transparently (1h tokens, SDK handles)
- [x] Boot/delete via SDK works

**Gotchas discovered:**

- System Python 3.9 ships with old openstacksdk that has multiple bugs:
  - `metadata={...}` filter on `compute.servers()` rejected
  - `limit list` raises `'username'` keyword argument error
  - `rebuild_server()` always sends `name` field, which CERN rejects
- **Solution**: use a virtualenv with a modern Python (3.11+) and recent SDK
- **`OS_*` env vars from interactive openrc clobber app-credential auth**
  - Must `unset $(env | awk -F= '/^OS_/ {print $1}')` before app-cred work
  - Or use a separate shell / wrapper script

**Phase 0b deliverables:**

- Working `clouds.yaml` entry for app credential
- Python venv with `openstacksdk` ≥ 3.x
- Documented credential rotation policy:
  - Default expiry behavior: <document from `application credential show`>
  - Rotation cadence: every 6 months

-----

### Phase 1 — Cloud-init on the central image

Status: **DONE** (2026-06-03). Exit criterion met. Automated by
`verify-phase1.py` (create marker A → verify → rebuild with fresh user-data
marker B → verify → cleanup); 13/13 checks pass on `ALMA10 - x86_64`.

Prove cloud-init works as expected on the central AlmaLinux image. Validate
that user-data is applied and that cloud-init re-runs on rebuild.

- [x] Boot a VM with a trivial cloud-init that writes a marker file:

  ```yaml
  #cloud-config
  write_files:
    - path: /var/lib/husk-marker
      content: "PHASE1-MARKER-A\n"
  runcmd:
    - echo "runcmd-A ran at $(date -u +%FT%TZ)" >> /var/lib/husk-marker
  ```
- [x] SSH in, verify:
  - [x] marker file shows the content
  - [x] `cloud-init status` returns "done"
  - [x] `cloud-init analyze show` works
- [x] Time the boot (baseline for comparison) — create→ACTIVE ~155s (m2.small)
- [x] **Critical**: rebuild the same VM with *new* user-data containing a
  different marker, verify the new marker appears in the rebuilt VM:

  ```python
  body = {"rebuild": {"imageRef": image_id, "user_data": base64.b64encode(new_cloud_init).decode()}}
  # POST /servers/{id}/action with header OpenStack-API-Version: compute 2.79
  ```
  - [x] cloud-init re-ran on rebuild (marker B present, status "done")
  - [x] **No `cloud-init clean` needed** — rebuild restores the disk from the
    image, which wipes `/var/lib/cloud`, so cloud-init runs fresh every time
- [x] Clean up VMs

**Exit criterion**: cloud-init runs reliably on first boot AND re-runs with
fresh user-data on rebuild. Latter is critical for the slot-recycle architecture. ✅

**Findings (carry into later phases):**
- **Login user is `root`** on the CERN `ALMA10 - x86_64` image, not
  `almalinux`. The keypair public key is installed to `root` by cloud-init.
- **Rebuild with `user_data` requires Nova microversion ≥ 2.57**; we send
  `compute 2.79` explicitly. CERN's Nova rejects a rebuild body containing a
  `name` field ("Hostname cannot be updated"), so POST the action directly
  with a minimal `{"rebuild": {...}}` body rather than via the SDK helper.
- **Nova reports `ACTIVE` ~2–3s after a rebuild**, but the *guest* reboot +
  cloud-init re-run take longer underneath that status flip. "Slot ready
  again" is gated by guest boot + cloud-init finishing, not by Nova's status.
  The controller must wait for an in-guest readiness signal (cloud-init done /
  runner registered), not just `ACTIVE`. Verification ordering matters too:
  `write_files` output appears before `runcmd` runs (final stage), so gate
  any readiness check on `cloud-init status --wait`.

-----

### Phase 2 — Manual runner lifecycle

Status: **DONE** (2026-06-04). Exit criterion met. Validated by hand on a single
CERN ALMA10 VM (`husk-test` repo, `m2.small`). Every path below works on rootless
Podman with **no Docker daemon installed**.

Did the whole runner registration + execution flow by hand on a single VM.

- [x] Create a fine-grained PAT scoped to a test repo (`Administration: write`)
- [x] Test the JIT endpoint:

  ```bash
  curl -fsS -X POST -H "Authorization: Bearer $PAT" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/repos/$OWNER/$REPO/actions/runners/generate-jitconfig \
    -d '{"name":"manual-test-1","runner_group_id":1,"labels":["self-hosted","linux","x64","manual-test"],"work_folder":"_work"}'
  # returns .encoded_jit_config (single-use, short-lived)
  ```
- [x] Boot a fresh VM, SSH in (as **root**), manually:
  - [x] Install `podman podman-docker fuse-overlayfs slirp4netns libicu git`
        (`libicu` is a hard dep of the .NET runner; `bin/installdependencies.sh`
        pulls the full set)
  - [x] `touch /etc/containers/nodocker` (silence the shim nag)
  - [x] Create unprivileged `runner` user — no sudo, no docker/wheel groups
  - [x] `loginctl enable-linger runner` + verify `/run/user/<uid>` exists
  - [x] As that user, verify `podman run hello-world` rootlessly
  - [x] As that user, verify the `docker` shim works: `docker run hello-world`
  - [x] Enable user Podman socket: `systemctl --user enable --now podman.socket`
        (ships enabled by default on Podman 5.x)
  - [x] `export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock`
  - [x] Download pinned actions-runner tarball into the runner user's home
  - [x] Run `./run.sh --jitconfig <blob>` as the runner user
- [x] Push `.github/workflows/*.yml` (`workflow_dispatch`, `runs-on:
      [self-hosted, linux, x64, manual-test]`)
- [x] Trigger from GitHub UI, verify:
  - [x] Runner picks up the job
  - [x] Job completes
  - [x] Runner exits cleanly
  - [x] Runner auto-deregisters from GitHub (JIT single-use semantics)
- [x] Container-**job** path (the compatibility-critical case):
  - [x] Workflow with `container: image: ubuntu:24.04`
  - [x] Job runs inside the container (Podman fulfills the Docker API call)
  - [x] Workspace mounted, `apt-get install` inside works
- [x] JS / composite action path (`actions/checkout@v4`, `actions/setup-node@v4`)
      — `_actions` download + `_tool` cache exercised
- [x] Container-**action** path (`docker://alpine:3.20` with entrypoint/args)

**Exit criterion**: MET. End-to-end manual flow works; PAT scoped correctly;
runner picks up label-matched jobs; `container:` jobs **and** container actions
work via Podman transparently; no `docker`/`dockerd` daemon on the host.

#### Findings — the host setup the bare CERN image needs (feeds Phase 3)

Getting from a bare ALMA10 image to a working runner surfaced several **real
Docker→Podman compatibility gaps**. The GitHub runner is written against the
Docker CLI + Docker Engine API and hardcodes assumptions Podman doesn't satisfy
out of the box. The fixes below are mandatory in the Phase 3 image, not optional
polish.

1. **Login user is `root`**, sshd has GSSAPI/Kerberos enabled (carried over from
   Phase 0/1).

2. **`libicu` is required** — the .NET runner aborts with "Couldn't find a valid
   ICU package" without it. Bare ALMA10 doesn't ship it.

3. **Rootless prerequisites**: `loginctl enable-linger runner` (so
   `/run/user/<uid>` and the user Podman socket exist at boot with no login) and
   a correct `XDG_RUNTIME_DIR=/run/user/<uid>`. Interactive `su -` does **not**
   set `XDG_RUNTIME_DIR` — the systemd unit must set it explicitly (this bit us).

4. **The runner talks the Docker Engine API over a socket for `container:` jobs**,
   at the **hardcoded path `/var/run/docker.sock`** (ignores `DOCKER_HOST` for the
   *mount source*). Must symlink `/var/run/docker.sock` → the rootless Podman user
   socket (`/run/user/<uid>/podman/podman.sock`). Use `tmpfiles.d` so it survives
   reboot (`/run` is tmpfs). **Security note:** the runner also bind-mounts this
   socket *into* job containers, so a job can reach the (rootless) engine — same
   as stock self-hosted runners, blast radius confined to the unprivileged
   `runner` userns. State this in the security model.

5. **`/usr/bin/docker` must be replaced by a shim** at `/usr/local/bin/docker`
   (ahead of `podman-docker`'s in `PATH`; the runner resolves `docker` via `PATH`).
   The shim closes **three** gaps and is the central compatibility artifact:
   - **(a) Auto-create missing `-v` bind-mount SOURCE dirs.** Docker auto-creates
     a missing source; Podman errors `statfs … no such file or directory`. The
     runner deletes/recreates `_work/_actions`, `_tool`, `_temp/*` per job and
     relies on Docker auto-creating the mount sources — so pre-creating is futile
     (wiped at job start); the shim must `mkdir -p` each source at `docker
     run/create` time.
   - **(b) Sanitize `$HOME` for Podman.** Container-**action** steps invoke the
     docker process with `HOME=/github/home`. Rootless Podman derives its
     storage/config from `$HOME`, so it fails with the cryptic
     `cannot resolve /github/home: lstat /github: no such file or directory`
     (this is Podman choking on its own `$HOME`, **not** a mount error — it cost
     us a long detour). The shim runs Podman with the user's real home
     (`env HOME=<real>`) while re-emitting the container's intended
     `HOME=/github/home` as an explicit `-e` value. An explicit `storage.conf`
     does **not** fix this — Podman never finds it under the wrong `$HOME`.
   - The shim **must have a valid shebang** (`#!/bin/bash` on line 1, LF endings).
     The runner execs it via raw `execve` (no shell fallback), so a malformed
     shebath fails with "Exec format error" even though an interactive
     `bash docker …` silently works via the shell's `ENOEXEC`→`/bin/sh` fallback.
     Verify with `env /usr/local/bin/docker version`, never a bare interactive call.
   - **Non-findings (ruled out):** mount *reordering* and bind-source overlap were
     red herrings — both traced back to (a) missing sources and (b) `$HOME`. The
     shim does **not** need reorder logic.

6. **SELinux**: ALMA10 is `Enforcing`. The runner bind-mounts the workspace
   (`user_home_t`) into containers **without** `:Z` relabeling (it controls the
   mount flags, not us), so `container_t` is denied `read` → step scripts fail
   with `Permission denied`. Fix: `/etc/containers/containers.conf` `[containers]
   label = false` (drops per-container SELinux confinement; host stays
   `Enforcing`). `:Z` isn't available to us. **Security trade-off** to record:
   isolation then rests on the VM + user-namespace boundary, not SELinux —
   defensible for throwaway slots, but state it.

7. **Podman version**: 5.8.2 on the image works for all paths. (Earlier
   nested-mount worries were the `$HOME` bug in disguise, not a Podman version
   issue.)

8. **JIT runners skip `config.sh`**, so the `_work` skeleton is never bootstrapped
   — another reason gap (5a) must be handled by the shim rather than at configure
   time.

-----

### Phase 3 — Automate via cloud-init + systemd

Status: **DONE** (2026-06-04). Scope validated: **A–C (recycle mechanics)**.
A coarse `nftables` egress firewall has since landed in the controller's
cloud-init (deny CERN-internal, allow public; see *Network policy rollout* and
the `husk-firewall` probe in Phase 5); the full `husk-policy` allowlist and the
container/action compat re-run (already validated by hand in Phase 2) remain
deferred.

Encode Phase 2 as a reproducible cloud-init script. This is the **architecture
that goes into Phase 6**, not a throwaway test.

**Authoritative artifacts** (the validated recipe lives here, not in the draft
YAML below — the draft predates the findings and has several of the bugs the
findings call out):

- `phase3-recycle.py` — single-slot recycle driver (embryo of huskd's reconcile
  loop). Embeds the corrected cloud-init, a GitHub client (JIT mint / runner
  match / dispatch / reap), and Phase 1's OpenStack plumbing (CERN rebuild
  incantation, the `require_seen` race guard). Subcommands: `create` (B),
  `diag`, `watch`, `recycle`, `loop --cycles N [--no-dispatch]` (C), `clean`.
- `phase3.yml` — single-job `workflow_dispatch` workflow on the dedicated
  `husk-phase3` label. **Single job on purpose**: JIT runners are single-use
  (one job, then deregister + exit), so a multi-job workflow strands jobs 2+.

#### Findings — what automating the Phase 2 recipe surfaced

Doing the recipe by hand (Phase 2) hid a pile of ordering/privilege/state
issues that only bite under cloud-init + systemd + Nova rebuild. Each of these
is a correction to the draft YAML below and is baked into `phase3-recycle.py`:

1. **`write_files` runs BEFORE the `users-groups` module.** Any entry with
   `owner: runner:runner` does a chown to a not-yet-existent user, which throws
   and **aborts the entire `write_files` module** — silently dropping every
   later file (the unit, the docker shim, the tmpfiles conf). Fix: no `owner:`
   on any `write_files` entry; `chown` in `runcmd` (final stage, user exists).
2. **The bare CERN image has no `sudo`.** Use `runuser -u runner --` for
   root→runner drops (util-linux, no PAM/sudoers). Install the `sudo` *package*
   anyway (no sudoers entry → runner still unprivileged) because the runner's
   `installdependencies.sh` shells out to it.
3. **`systemctl --user` from a system service can't reach a user bus.** The
   old `ExecStartPre=systemctl --user start podman.socket` failed with "Failed
   to connect to user scope bus". Fix: `systemctl --global enable podman.socket`
   (root, no bus) **before** `loginctl enable-linger runner` (which brings up
   `user@1000`, auto-starting the now-enabled socket). The unit's `ExecStartPre`
   just polls for the socket *file* — no bus involved. Keeps the Phase-2-proven
   linger + `user@1000` runtime (so the systemd cgroup manager still works).
4. **`ExecStopPost=/sbin/poweroff` runs as the unprivileged runner → "Access
   denied".** Poweroff is the recycle trigger, so split it out: the runner unit
   triggers a **root** oneshot `husk-poweroff.service` via `OnSuccess=` and
   `OnFailure=` (systemd ≥249; ALMA10 has 252). `OnFailure=` too, so a failed
   boot recycles instead of wedging.
5. **`runcmd` must be the sole boot orchestrator.** Do NOT enable the units for
   boot-time start: on a rebuild, `multi-user.target` would launch the runner
   before cloud-init has reinstalled `run.sh`. `runcmd` installs the runner then
   `systemctl start` (not `enable --now`) — `Type=simple` returns once ExecStart
   forks, so the long job doesn't block runcmd.
6. **Nova `rebuild` PRESERVES power state.** A `SHUTOFF` slot (the runner powers
   off after its job) stays `SHUTOFF` after rebuild — it does NOT boot. So
   **recycle = `rebuild` + `os-start`**, not rebuild alone. (Phase 1 only saw
   rebuild→ACTIVE because it rebuilt an already-ACTIVE VM.) This updates the
   reconcile loop: "needs-recycle" → rebuild, then start.
7. **JIT runner names must be globally unique.** An interrupted cycle (mint
   happens before rebuild) or a re-run reusing cycle numbers leaves an offline
   registration and the next mint 409s. Minting must be **idempotent**: on 409,
   delete the stale same-name runner and retry.
8. **The driver's fine-grained PAT can mint JIT + read runners (Administration)
   but CANNOT `workflow_dispatch` (needs `Actions: write`) nor commit
   `.github/workflows/*` (needs the `Workflows` permission).** Phase 3 jobs were
   driven manually (`--no-dispatch`). **Phase 6 controller / Phase 7 App must
   carry `Actions: write`.** Naming: VM name is timestamped (CERN registers VM
   names in DNS/LANDB and rejects dupes) and stable across rebuilds; the runner
   name is GitHub-side only and varies per cycle.

#### Timing (the headline result, and the Phase 4 trigger)

Recycle (rebuild issued → new runner online): **~65s**, of which:

- `rebuild` + `os-start` → `ACTIVE`: **~5s** (matches the plan's ~25s model
  upper-bounded; CERN was faster here).
- The remaining **~60s** is cloud-init reinstalling the runner on the wiped
  disk: downloading the runner tarball **and `installdependencies.sh`
  dnf-installing the .NET native deps** (libicu/krb5/openssl/…).

So recycle **misses the <60s target**, and the bottleneck is entirely the
per-rebuild package/runner install — which is exactly what a pre-baked image
removes. See Phase 4 (now un-deferred as a *future* optimization, but not
blocking: the cloud-init PoC works end-to-end).

- [ ] Write `provisioning/runner-cloud-init.yaml.j2`:

  ```yaml
  #cloud-config
  # NOTE: package + setup list below is the validated Phase 2 recipe. See the
  # Phase 2 "Findings" block for why each piece is mandatory.
  packages:
    - podman
    - podman-docker         # provides a /usr/bin/docker shim (we override it, see below)
    - fuse-overlayfs        # rootless storage driver
    - slirp4netns           # rootless networking
    - netavark              # modern container network backend
    - aardvark-dns          # DNS for container networks
    - libicu                # REQUIRED by the .NET actions-runner (aborts without it)
    - nftables
    - curl
    - jq
    - git

  users:
    - default
    - name: runner
      groups: []            # NO docker, NO wheel, NO sudo
      shell: /bin/bash
      lock_passwd: true
      ssh_authorized_keys: []

  write_files:
    # Host firewall — owned by host root, runner cannot modify
    - path: /etc/nftables/husk.nft
      content: |
        # Primary policy: default-allow, deny CERN-internal.
        # Rendered from husk-policy/policy.toml at controller startup;
        # CIDRs below are placeholders to be substituted at render time.
        table inet husk {
            chain output {
                type filter hook output priority 0; policy accept;

                oif lo accept
                ct state established,related accept

                # No IPv6 outbound — drop all v6 (belt-and-suspenders
                # alongside the sysctl below)
                meta nfproto ipv6 log prefix "husk-blocked-v6: " drop

                # Pin DNS to the configured resolver
                udp dport 53 ip daddr != <resolver-ip> \
                    log prefix "husk-blocked-dns: " drop
                tcp dport 53 ip daddr != <resolver-ip> \
                    log prefix "husk-blocked-dns: " drop

                # CERN-public DMZ — accept before the denylist below
                ip daddr { <cern-public-allow-cidrs> } accept

                # CERN-internal — drop with logging
                ip daddr { 137.138.0.0/16, 188.184.0.0/16, 188.185.0.0/16 } \
                    log prefix "husk-blocked-cern: " drop

                # Default: accept (public internet allowed)
            }
        }

    - path: /etc/sysctl.d/99-husk-no-v6.conf
      content: |
        net.ipv6.conf.all.disable_ipv6 = 1
        net.ipv6.conf.default.disable_ipv6 = 1

    - path: /var/lib/husk/jitconfig
      permissions: '0600'
      owner: runner:runner
      content: "{{ JIT_BLOB }}"

    - path: /home/runner/.config/containers/storage.conf
      owner: runner:runner
      content: |
        [storage]
        driver = "overlay"
        runroot = "/run/user/1000/containers"
        graphroot = "/home/runner/.local/share/containers/storage"

        [storage.options.overlay]
        mount_program = "/usr/bin/fuse-overlayfs"

    # --- Docker→Podman compatibility layer (validated in Phase 2) ---

    # Silence the podman-docker shim's "Emulate Docker CLI" banner
    - path: /etc/containers/nodocker
      content: ""

    # Drop per-container SELinux confinement so the runner's un-relabeled
    # ($user_home_t) workspace bind-mounts are readable inside containers.
    # Host stays Enforcing. See Phase 2 finding #6 (security trade-off).
    - path: /etc/containers/containers.conf
      content: |
        [containers]
        label = false

    # The runner hardcodes /var/run/docker.sock for container jobs (and mounts it
    # into job containers). Point it at the rootless Podman user socket. tmpfiles.d
    # so it survives reboot (/run is tmpfs). See Phase 2 finding #4.
    - path: /etc/tmpfiles.d/husk-docker-sock.conf
      content: |
        L+ /run/docker.sock - - - - /run/user/1000/podman/podman.sock

    # The compatibility shim that REPLACES /usr/bin/docker (must be ahead of it in
    # PATH). Closes the three Docker→Podman gaps from Phase 2 finding #5:
    #  (a) auto-create missing -v bind SOURCE dirs (podman won't; docker does)
    #  (b) sanitize $HOME for podman (container actions set HOME=/github/home,
    #      which breaks rootless podman's storage/config resolution)
    # Must have a valid shebang on line 1 — the runner execs it via raw execve.
    - path: /usr/local/bin/docker
      permissions: '0755'
      content: |
        #!/bin/bash
        REAL_HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)"
        [ -n "$REAL_HOME" ] || REAL_HOME="$HOME"
        out=(); prev=""
        for a in "$@"; do
          if [ "$prev" = "-v" ] || [ "$prev" = "--volume" ]; then
            src="${a%%:*}"; case "$src" in /*) [ -e "$src" ] || mkdir -p "$src" ;; esac
          fi
          if { [ "$prev" = "-e" ] || [ "$prev" = "--env" ]; } && [ "$a" = "HOME" ]; then
            out+=("HOME=$HOME")
          else
            out+=("$a")
          fi
          prev="$a"
        done
        exec env HOME="$REAL_HOME" /usr/bin/podman "${out[@]}"

    - path: /etc/systemd/system/husk-runner.service
      content: |
        [Unit]
        Description=Husk GitHub Actions ephemeral runner
        After=network-online.target nftables.service
        Wants=network-online.target

        [Service]
        Type=simple
        User=runner
        Group=runner
        WorkingDirectory=/home/runner
        Environment="HOME=/home/runner"
        Environment="XDG_RUNTIME_DIR=/run/user/1000"
        Environment="DOCKER_HOST=unix:///run/user/1000/podman/podman.sock"
        ExecStartPre=/bin/bash -c 'sudo -u runner XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start podman.socket'
        ExecStart=/bin/bash -c '/opt/actions-runner/run.sh --jitconfig $(cat /var/lib/husk/jitconfig)'
        ExecStopPost=/sbin/poweroff
        Restart=no

        [Install]
        WantedBy=multi-user.target

  runcmd:
    # Apply IPv6 disable, then load nftables firewall
    - sysctl --system
    - nft -f /etc/nftables/husk.nft
    - systemctl enable nftables

    # Materialize the /run/docker.sock symlink now (tmpfiles.d also re-creates it
    # on every boot). /var/run → /run on this image.
    - systemd-tmpfiles --create /etc/tmpfiles.d/husk-docker-sock.conf

    # Set up runner home and install
    - mkdir -p /opt/actions-runner /var/lib/husk
    - chown runner:runner /opt/actions-runner /var/lib/husk
    - sudo -u runner bash -c 'cd /opt/actions-runner && curl -L <pinned-runner-url> | tar xz'
    # Pull the full set of runner native deps (libicu, krb5, openssl, …)
    - /opt/actions-runner/bin/installdependencies.sh

    # Enable user-level systemd for the runner user (for Podman socket)
    - loginctl enable-linger runner
    - sudo -u runner XDG_RUNTIME_DIR=/run/user/1000 systemctl --user enable podman.socket

    # Start the runner
    - systemctl daemon-reload
    - systemctl enable --now husk-runner.service

    # Belt-and-suspenders wall-clock timeout (bypassable by anything as root,
    # but runner is unprivileged so this is actually a real safety net here)
    - shutdown -h +360
  ```
- [ ] Pin the actions-runner version explicitly (don't use "latest")
- [ ] Write a small Python script (`tests/launch-runner.py`) that:
  1. Calls `/generate-jitconfig` to get a JIT blob
  2. Renders the template with the blob
  3. Calls `conn.compute.create_server(..., user_data=base64.b64encode(...))`
- [ ] First create test:
  - [ ] VM boots → cloud-init runs → runner registers within ~6 min total
  - [ ] Push test workflow → runner picks it up → completes → exits
  - [ ] `ExecStopPost` triggers poweroff → VM ends up in `SHUTOFF`
- [ ] **Workflow compatibility test matrix**:
  - [ ] Plain shell job: `run: echo hello`
  - [ ] Job with `container: image: node:20` running a `run: npm --version`
  - [ ] Job using `actions/setup-node@v4` (container action)
  - [ ] Job using `actions/checkout@v4` (repo checkout)
  - [ ] Job that runs `docker pull alpine` (should work via shim)
  - [ ] Job that runs `docker build .` on a small Dockerfile (Buildah via Podman)
  - [ ] Confirm: workflows that need `sudo apt install` fail gracefully and are
    documented as out-of-scope
- [ ] **Network restriction test** (denylist policy):
  - [ ] Verify `curl http://<cern-internal-host>/` from a job fails (denylist)
  - [ ] Verify `curl https://gitlab.cern.ch/` from a job succeeds (DMZ
    allow-before-deny works)
  - [ ] Verify `curl https://api.github.com/` succeeds (default-allow)
  - [ ] Verify `curl https://pypi.org/` succeeds (default-allow)
  - [ ] Verify `curl -6 https://ipv6.google.com/` fails (v6 dropped)
  - [ ] Verify DNS to a non-pinned resolver fails: `dig @8.8.8.8 example.com`
  - [ ] Verify the same denylist/allow behavior from inside a container
    spawned via `container:`
  - [ ] Check `journalctl -k | grep husk-blocked` for logged drops
- [ ] **Rebuild recycle test** (the critical one):
  - [ ] After the SHUTOFF, generate a fresh JIT
  - [ ] Rebuild the VM via direct POST or SDK with new user-data
  - [ ] VM comes back up within ~30s
  - [ ] New runner registers with new identity
  - [ ] Push another workflow → it picks it up
- [ ] Measure full recycle time: SHUTOFF detected → rebuild issued → runner online
- [ ] Run the rebuild cycle 5+ times on the same slot to verify stability

**Exit criterion**: a single slot can run multiple jobs in sequence via the
rebuild cycle. Recycle time consistently <60s. No state leaks between jobs.
Network restrictions verified by attempted-and-blocked CERN-internal access.
Workflows using `container:` work transparently via Podman.

**Result (2026-06-04)**: ✅ single slot runs multiple jobs in sequence via
rebuild + os-start; no state leaks (the `/tmp` leak-canary in `phase3.yml` is
absent every cycle — the disk wipe guarantees clean state). ⚠️ recycle ~65s,
*over* the <60s target — entirely the per-rebuild runner reinstall (see Timing
above); the target is achievable with a pre-baked image (Phase 4). Network
restriction + full container compat matrix were **out of A–C scope** and remain
to validate (here or rolled into a later phase).

-----

### Phase 4 — Custom VM image — **deferred, but now a known optimization**

Status: **not blocking; revisit as a recycle-time optimization.** Updated by
Phase 3 evidence (was "deferred indefinitely" on Phase 0 reasoning alone).

Original Phase 0 reasoning still holds for *fresh create*: boot time there is
dominated by Neutron port provisioning (~4 min of the 5-min create), upstream of
image content. But Phase 3 measured the thing that actually matters — the
**recycle** path — and found a concrete, image-fixable cost:

- **Recycle is ~65s, of which ~60s is cloud-init reinstalling the runner +
  `installdependencies.sh` (dnf native deps) on the wiped disk.** A custom image
  with the runner binary and its deps pre-baked would cut recycle toward the
  ~5s rebuild+start floor — i.e. comfortably under the <60s target Phase 3 just
  missed.

**Decision (2026-06-04, deliberate): delay it.** The cloud-init path works
end-to-end as a PoC and is the tested fallback; ~65s recycle is acceptable for
now. Bake an image only when recycle latency becomes a real constraint (job
throughput at scale, or the <60s target becomes a hard requirement). Until then,
keep iterating on the controller (Phase 6) against the cloud-init slot.

**May revisit (now with a concrete trigger):**

- Recycle latency (~60s install) becomes a throughput bottleneck → this is the
  primary, now-quantified trigger
- Job logs show consistent slow `cloud-init` runs that custom-image could shorten
- A specific job type needs heavily pre-installed dependencies that don't
  belong in containers (rare)
- Image-based reproducibility becomes a requirement (e.g., compliance)

**If revisited:**

- Try Packer with the OpenStack builder first
- Fall back to manual snapshot + `cloud-init clean` if Packer is restricted
- Keep the central-image-plus-cloud-init path as a tested fallback regardless

-----

### Phase 5 — Deliberate failure-mode testing

Status: **prioritized for a non-HA system** (2026-06-04). The controller
(Phase 6) now exists and runs live against CERN OpenStack, and a coarse egress
firewall ships in cloud-init — so the previously-gated controller and
network-egress scenarios are now runnable. The fail-safe matrix is covered by
unit tests (FakeBackend); the live smoke runs remain to be ticked off.

#### Prioritization (decided 2026-06-04 — this is NOT an HA system)

There is no HA, and after-the-fact manual recovery during edge cases is
acceptable. So the goal of Phase 5 is **not auto-recovery**. In priority order:
(1) are the safety boundaries actually enforced, (2) does the controller fail
*safe* — never destroy what it didn't create, never run away creating, (3) are
bad states *detectable* for manual cleanup.

**Must test (manual recovery can't save you here):**

1. **Timeout / poweroff enforcement** — the security keystone. An unprivileged
   runner must not outlive its slot. The slot side is testable now (see below);
   the real bound on a hung/malicious job is the **controller**, because the slot
   does NOT self-bound a *running* job — poweroff fires only when the runner
   *exits*, and `shutdown -h +360` is 6h out.
   - **Timeout action = `server stop` (→ SHUTOFF → rebuild), NOT destroy.**
     Powering off kills the runner, and SHUTOFF is already the recycle state.
     Reserve `destroy` for unrecoverable (`ERROR`) slots.
   - **The controller does not need to signal GitHub.** When the runner vanishes,
     GitHub fails the job itself once heartbeats stop ("lost communication with
     the server"). The "zombie window" (how long that takes) is worth
     **measuring**. Optional prompt-fail: `POST /actions/runs/{run_id}/cancel`
     (needs `Actions: write` + runner→run mapping via `/runs?status=in_progress`
     then `/runs/{id}/jobs` matching `runner_name`) — Phase 6 polish, not required.
     - Empirically, the zombie window is about 10 min, probably worth signaling to GitHub early, but can treat as follow up optimization
2. **Controller fail-safe** — the only irreversible automated action.
   - Tag filter: `list_slots()` always filters `managed-by=husk`; destroy/rebuild
     only ever target slots returned by `list_slots()`. (Live-testable now.)
   - Auth/API failure must **ABORT the tick**, never be read as "no slots ⇒
     destroy". Distinct branches: list *raises* (abort) vs list returns `[]`
     (may create). → **Phase 6 unit test** against a FakeBackend; there's no
     reconcile loop to exercise yet.
3. **Runaway-cost guards** — needs the pool/controller; all Phase 6:
   - capacity full ⇒ zero creates; failed create ⇒ caught, logged, no orphaned
     `BUILD`/`ERROR` ghost; persistent failure ⇒ one attempt/slot/tick, no retry
     storm; **single-controller lock** (flock pidfile, or an OpenStack-side
     lease) so an accidental double-run can't fight over slots.

**Network-egress / firewall-tamper tests** — a coarse `nftables` egress policy
now ships in cloud-init (see *Network policy rollout*), so these are no longer
gated. The `husk-firewall` workflow asserts the core property (public reachable,
`landb.cern.ch:443` blocked); `evade.yml` covers firewall-tamper from inside an
unprivileged job. The full default-deny `husk-policy` allowlist remains future
work.

**Fail-safe-only (manual recovery is fine — do NOT build auto-recovery for
these):** rebuild-fails, hypervisor-dies-mid-rebuild, network-drop-mid-job,
GitHub API outage, cloud-init-didn't-re-run. Sole requirement: the controller
doesn't crash and doesn't react destructively (covered by fail-safe test #2).
Ending in a *detectable* bad state is enough.

**Test as unit tests (FakeBackend), not live:** all of #2's auth-safety and all
of #3 — inject raise/empty/full, assert no-destroy / no-create / bounded-create.
This argues for building the `Backend` protocol seam (Phase 6) **first** so
failures are injectable. ~4 live smokes on top: tag filter,
broken-cred-no-destroy, over-quota-no-orphan, double-run-refused.

#### Testable with just a dispatched workflow (no controller internals needed)

Slot-level properties testable with just a dispatched workflow (the controller
and a coarse firewall now exist, so all four run today):

1. **Unprivileged boundary** — `evade.yml` (single job, `husk-phase3` label):
   `id`, `sudo -n true`, `sudo shutdown -c`, `systemctl mask poweroff.target`,
   `nft flush ruleset`, then `sleep`. Every privileged attempt must fail in the
   job log. Highest value — the premise of the untrusted-runner model.
2. **Stop-mid-job + measure the GitHub zombie window** — during a sleeping job,
   `openstack --os-cloud cern server stop <id>`. Confirm `SHUTOFF` + runner dies,
   and **time how long until GitHub marks the job failed** (and capture the
   message). Answers "do we need to signal GitHub?" empirically.
3. **`OnFailure` poweroff** — a one-step `exit 1` job; confirm the slot powers
   off on a *failed* job too (not just success), so a crash still recycles
   instead of stranding the slot `ACTIVE`.
4. **Egress firewall** — `husk-firewall.yml` (single job, `husk-phase3` label):
   confirms the runner reaches the public internet but a TCP connect to
   `landb.cern.ch:443` is refused. Green once a slot has recycled onto the coarse
   cloud-init policy (`huskctl recycle --all` rolls it out to live slots).

Optional recon (informs the Phase 6 catch): revoke the OpenStack credential,
make one SDK call (`conn.compute.servers()`), note the exception type/message.

#### Detailed scenario reference

Break things on purpose. Each scenario teaches what the controller needs.
(Priorities above govern which of these actually get done and when.)

- [ ] **Idle slot, no jobs**: bring up a slot, don't trigger any workflow
  - [ ] Verify: runner sits idle indefinitely (no native timeout)
  - [ ] Manually `DELETE /runners/{id}` → runner exits → VM powers off
  - [ ] This validates the idle-reaper mechanism
- [ ] **Long-running job**: workflow with `run: sleep 99999`
  - [ ] Controller `server stop` fires after `MAX_JOB_DURATION` (primary) →
    SHUTOFF → rebuild. NOT destroy (stop kills the runner and SHUTOFF is the
    recycle state; destroy is only for unrecoverable slots).
  - [ ] `shutdown -h +360` is the slot-side fallback only (6h; the runner is
    unprivileged so it can't `shutdown -c`). The slot does NOT self-bound a
    running job — the controller stop is the real bound.
  - [ ] GitHub fails the job on its own after the runner stops heartbeating;
    measure that "zombie window". Proactive cancel is optional (needs
    `Actions: write`), not required.
- [ ] **Non-cooperative shutdown attempt**: workflow that tries to evade timeout:

  ```yaml
  steps:
    - run: |
        # Attempt various bypasses
        sudo shutdown -c || true            # should fail (no sudo)
        systemctl mask poweroff.target || true  # should fail (no sudo)
        sleep infinity
  ```
  - [ ] Verify all bypass attempts fail (runner is unprivileged)
  - [ ] Controller's force-destroy eventually fires
  - [ ] Document timing in the runbook
- [ ] **Network restriction bypass attempts**: workflow that tries to reach
  CERN-internal:

  ```yaml
  steps:
    - run: |
        # Direct
        curl --max-time 5 http://<internal-host>/ || echo "blocked-good"
        # Via container
        docker run --rm --network host alpine \
          sh -c 'apk add curl && curl --max-time 5 http://<internal-host>/' \
          || echo "blocked-good"
        # Try to modify firewall
        nft flush ruleset 2>&1 || echo "blocked-good"
        sudo nft flush ruleset 2>&1 || echo "blocked-good"
  ```
  - [ ] All three attempts fail
  - [ ] Drops are logged in journalctl on the slot
- [ ] **Bad JIT blob**: rebuild with corrupted JIT
  - [ ] systemd unit fails fast
  - [ ] VM either powers off or stays detectably-broken
- [ ] **Rebuild fails**: deliberately pass invalid image
  - [ ] Controller logs the failure
  - [ ] Slot ends up in a defined state (probably `ERROR`)
  - [ ] Controller's recovery: destroy slot, create fresh
- [ ] **Cloud-init didn't re-run after rebuild**: simulate by not cleaning
  cloud-init state before snapshot (relevant if Phase 4 ever happens)
  - [ ] How does the slot end up? (probably: old runner re-registers but with
    stale credentials, eventually fails)
- [ ] **Hypervisor goes away mid-rebuild**: rare but possible
  - [ ] Slot likely ends up `ERROR`
  - [ ] Controller fallback: destroy and recreate on different host
- [ ] **Network drop mid-job**: detach the slot's network port
  - [ ] Runner retries, eventually gives up
  - [ ] VM ends up powered off
- [ ] **GitHub API outage**: block `api.github.com` on the controller
  - [ ] Controller logs errors but doesn't crash
  - [ ] Recovers when network is restored
- [ ] **Quota exhaustion**: try to grow pool beyond `max_total` (or beyond CERN
  quota)
  - [ ] Graceful "no boot this tick" behavior
  - [ ] No crash, no orphaned partial creates
- [ ] **OpenStack credential expiry / revocation**: simulate by deleting the
  app credential while controller is running
  - [ ] Controller detects auth failure
  - [ ] Logs clearly
  - [ ] Does NOT silently destroy slots it can no longer manage

Document each scenario's outcome in `docs/failure-modes.md`.

**Exit criterion**: every failure mode has a known, expected outcome the
controller will handle correctly.

-----

### Phase 6 — Build the controller (`huskd`)

Status: **POC built and validated live (2026-06-04).** The controller runs
against real CERN OpenStack from `src/husk/` — `Backend` protocol + slot
classifier + non-blocking tick loop + `OpenStackBackend` + `FakeBackend`,
`pydantic-settings` config, and the `huskd`/`huskctl` typer CLIs, behind ~80 unit
tests. It maintains `min_ready`, respects `max_total`, recycles slots via
rebuild→start, enforces the fail-safe invariants (list-raises aborts the tick;
destroy only on ERROR/decommission), does hysteresis-guarded ramp-down, publishes
a `ControllerState` snapshot served over HTTP (`/status` `/metrics` `/healthz`),
and tracks per-slot timing (cloud-init / recycle durations, live fraction).
Operator tooling: `huskctl status [-w]`, `huskctl recycle [--all]` (stops slots so
the loop rebuilds them with freshly rendered cloud-init), `huskctl reap`.

**Remaining for production** (the checklist below is the original target; much of
the core is done): the prod OpenStack application credential + rotation, GitHub
5xx/backoff hardening, the full Prometheus counter/histogram set + Grafana
alerting (only a gauge subset is emitted today), `MAX_SLOT_AGE` periodic recycle,
GitHub App auth (Phase 7), and PaaS deploy (Phase 6b).

By this point all mechanics are validated. Implementation should be mechanical.

- [ ] Define the `Backend` protocol/abstract class:

  ```python
  class Backend(Protocol):
      def list_slots(self) -> list[Slot]: ...
      def create_slot(self, user_data: str, labels: list[str], ...) -> Slot: ...
      def rebuild_slot(self, slot: Slot, user_data: str) -> None: ...
      def destroy_slot(self, slot: Slot) -> None: ...
      def capacity(self) -> Capacity: ...  # free vCPU / RAM / instance slots
  ```
- [ ] Implement `OpenStackBackend`:
  - [ ] Uses `openstacksdk` via `openstack.connect(cloud=<name>)`
  - [ ] Tags slots with `metadata={'managed-by': 'husk', 'controller-instance': '<id>'}`
  - [ ] `list_slots()` filters by tag (never touches VMs it didn't create)
  - [ ] `rebuild_slot()` uses direct POST to `/servers/{id}/action` with body
    `{"rebuild": {"imageRef": <id>, "user_data": <b64>}}` —
    **does NOT include `name` field** (CERN Nova rejects it)
  - [ ] Pins Nova microversion to 2.79
- [ ] Create the **production** OpenStack application credential:
  - [ ] `openstack application credential create husk-controller-prod`
  - [ ] Place credential in `/etc/openstack/clouds.yaml` on controller VM,
    mode 0400, owned by `husk` service user
  - [ ] Document rotation procedure
- [ ] Implement `GitHubClient`:
  - [ ] Starts with PAT auth; refactor to App auth in Phase 7
  - [ ] Methods: `generate_jitconfig`, `list_runners`, `delete_runner`,
    `list_queued_jobs`
  - [ ] Handles rate limits and 5xx with exponential backoff
- [ ] Implement slot state classifier (`slots.py`):
  - [ ] Maps (Nova server, GitHub runner) tuples → state enum
  - [ ] Handles edge cases: rebuilt slot but runner not yet registered, etc.
- [ ] Implement reconciliation loop (`controller.py`):
  - [ ] Single-threaded, runs every 30s
  - [ ] Tracks per-slot timestamps in memory (idle_since, unhealthy_since, busy_since)
  - [ ] Calls into one or more backends
  - [ ] Logs every action taken with structured logging
  - [ ] **Force-destroy enforcement**: any slot busy longer than
    `MAX_JOB_DURATION` is destroyed (not rebuilt) via Nova `DELETE`.
    This is the primary safety net against runaway jobs, since in-VM
    `shutdown -h +N` is bypassable. Force-destroys are logged loudly and
    counted in a separate metric.
  - [ ] **Slot age cap**: optionally destroy slots older than `MAX_SLOT_AGE`
    even when idle, to redistribute hypervisor placement
- [ ] Configuration via `config.toml`:

  ```toml
  [github]
  org = "..."
  pat_path = "/etc/husk/pat"  # later: app_id + key_path

  [[backends]]
  name = "openstack-default"
  type = "openstack"
  cloud = "cern-husk-prod"    # clouds.yaml profile (app credential)
  image_name = "AlmaLinux 10"
  flavor_name = "..."
  network_name = "CERN_NETWORK"
  labels = ["self-hosted", "linux", "x64", "husk"]
  min_ready = 1
  max_total = 3                # leaves 1 controller + 1 spare in CERN quota of 5

  [timeouts]
  poll_interval_sec = 30
  idle_timeout_sec = 1800        # rebuild idle slots to refresh credentials
  startup_grace_sec = 300         # post-rebuild grace for runner to register
  max_job_duration_sec = 21600    # 6h — force-destroy busy slots beyond this
  max_slot_age_sec = 86400        # 24h — optional periodic recycle
  ```
- [ ] Deploy as systemd unit on a small VM in the same OpenStack project,
  or in CERN PaaS (OpenShift) — see Phase 6b
- [ ] Metrics (Prometheus, exposed on :9100/metrics):

  **Slot pool state** (gauges):
  - [ ] `husk_slots_total{backend, state="idle|busy|rebuilding|starting|unhealthy|error"}`
  - [ ] `husk_slots_max_total{backend}`
  - [ ] `husk_slots_min_ready{backend}`

  **Lifecycle events** (counters):
  - [ ] `husk_slot_creates_total{backend, result="success|failure"}`
  - [ ] `husk_slot_rebuilds_total{backend, result}`
  - [ ] `husk_slot_destroys_total{backend, reason="quota|unhealthy|decommission|error|job_timeout|slot_age"}`
  - [ ] `husk_force_destroys_total{reason}` — runaway-job safety alerts
  - [ ] `husk_jobs_completed_total{conclusion="success|failure|cancelled"}`

  **Latencies** (histograms):
  - [ ] `husk_slot_create_seconds` — buckets at 60, 120, 240, 480, 600s
  - [ ] `husk_slot_rebuild_seconds` — buckets at 10, 20, 30, 60, 120s
  - [ ] `husk_slot_recycle_seconds` — job-done to next-job-ready
  - [ ] `husk_job_wait_seconds` — user-facing queue time

  **API health**:
  - [ ] `husk_github_api_requests_total{endpoint, status_code}`
  - [ ] `husk_github_api_duration_seconds{endpoint}`
  - [ ] `husk_github_rate_limit_remaining{resource}`
  - [ ] `husk_openstack_api_requests_total{operation, status}`

  **Credentials** (gauges, for rotation reminders):
  - [ ] `husk_github_token_expires_in_seconds`
  - [ ] `husk_github_app_key_age_days`
  - [ ] `husk_openstack_credential_age_days`

  **Reconcile loop**:
  - [ ] `husk_reconcile_iterations_total{result="ok|error"}`
  - [ ] `husk_reconcile_duration_seconds`
  - [ ] `husk_last_reconcile_timestamp_seconds` — alert if stale
- [ ] Alerting rules (Prometheus):
  - [ ] Controller heartbeat: `time() - husk_last_reconcile_timestamp_seconds > 300`
  - [ ] Force-destroy elevated: `rate(husk_force_destroys_total[1h]) > 0`
  - [ ] Recycle p99 degraded: `histogram_quantile(0.99, husk_slot_rebuild_seconds) > 90`
  - [ ] Quota near limit: `husk_slots_total{state="busy"} == husk_slots_max_total for 15m`
  - [ ] Credentials aging: `husk_openstack_credential_age_days > 150`

**Exit criterion**: the controller runs as a service. Correctly maintains
`min_ready`, respects `max_total`, recycles slots after jobs, force-destroys
runaway slots, handles the failure modes from Phase 5. Metrics visible in
Grafana.

-----

### Phase 6b — Migrate controller to CERN PaaS (optional)

Status: not yet done. Optional but recommended for production deployment.

Move the controller from an OpenStack VM to a containerized deployment on
CERN's OpenShift PaaS service.

- [ ] Package the controller as a container image
- [ ] Push to CERN GitLab container registry
- [ ] Create OpenShift Deployment manifest with `replicas=1, strategy=Recreate`
- [ ] Move credentials to OpenShift Secrets:
  - [ ] `husk-openstack-creds` (clouds.yaml)
  - [ ] `husk-github-app-key` (.pem)
- [ ] Configure scraping by Prometheus (in-cluster or external)
- [ ] Decommission the controller VM (frees 1 OpenStack quota slot)

**Benefits over a dedicated VM**:

- No VM to maintain (OpenShift handles OS, patching, restarts)
- Auto-restart on crash
- Doesn't consume OpenStack quota
- Build + deploy via GitLab CI
- First-class secrets management

**Exit criterion**: controller runs in OpenShift, dedicated VM decommissioned.

-----

### Phase 7 — GitHub App migration

Status: not yet done.

Replace PAT with proper App auth.

- [ ] Create the GitHub App in org settings:
  - [ ] Name: `<org>-husk-controller`
  - [ ] Private (only installable by your org)
  - [ ] No webhook URL
  - [ ] Permissions:
    - [ ] Organization → Self-hosted runners: Read & write
    - [ ] Repository → Metadata: Read
- [ ] Download private key (`.pem`), store on controller (mode 0400)
- [ ] Install App on org, note Installation ID
- [ ] Refactor `GitHubClient` to support App auth:
  - [ ] JWT signing with `PyJWT` (10 min expiry)
  - [ ] Exchange JWT for installation token
  - [ ] Cache token; refresh ~5 min before expiry
  - [ ] On 401, force-refresh and retry once
- [ ] Switch all API calls to use installation tokens
- [ ] Decommission the PAT
- [ ] Document key rotation procedure

**Exit criterion**: controller authenticates as the App. PAT revoked.

-----

### Phase 8 — Org rollout

Status: not yet done.

- [ ] Configure runner groups in org settings:
  - [ ] `husk-default` — restrict to curated repo list initially
  - [ ] Org-level only (disable repository-level self-hosted runner creation)
- [ ] Update controller config to use org-level endpoints
- [ ] **Configure GitHub fork-PR approval policy** (must do before opening to
  any repo with external contributors):
  - [ ] Set approval policy at org level to "Require approval for all outside
    collaborators" (the strongest non-enterprise option)
  - [ ] Note: org membership = bypass. Adding a user to the org is enough to
    let them run workflows on Husk without approval. Vet org membership
    accordingly.
  - [ ] If on Enterprise: consider "Require approval for fork pull request
    workflows" which extends approval to any contributor without write access
- [ ] Document workflow constraints for repo owners:
  - [ ] **Supported**: plain shell jobs, `container:` directive, third-party
    container actions, `actions/checkout`, `actions/setup-*`
  - [ ] **Not supported** (workflow will fail or be unreliable):
    - Direct `sudo` invocations (no host sudo available)
    - Docker-in-Docker patterns (`docker:dind` service, mounting docker.sock)
    - `--privileged` containers expecting host root
    - `services:` directive (service containers not supported)
    - `pull_request_target` workflows on Husk labels (banned by policy)
  - [ ] **Migration patterns** for workflows currently using sudo:
    - `sudo apt install foo` → use a container image with foo pre-installed
    - `sudo apt-get clean` (disk cleanup) → use a leaner base image
    - `sudo systemctl ...` → not applicable in CI; remove from workflow
- [ ] Document `runs-on` label structure:
  - [ ] Primary: `runs-on: [self-hosted, linux, x64, husk]`
  - [ ] Variants TBD as needs emerge
- [ ] Onboarding checklist for repos joining Husk:
  - [ ] Add repo to `husk-default` runner group allowed-repos list
  - [ ] Verify approval policy is set correctly (cascades from org)
  - [ ] Verify no `pull_request_target` workflows reference Husk labels
  - [ ] Pilot with one workflow, confirm it works end-to-end
- [ ] Set up monitoring/alerting (see Phase 6 metrics list):
  - [ ] Alert if controller hasn't logged a successful poll in 5 min
  - [ ] Alert if `max_total` is hit for >15 min (capacity issue)
  - [ ] Alert if recycle p99 latency >2 min (something wrong with rebuild)
  - [ ] Alert if `husk_force_destroys_total` increments (runaway jobs)
- [ ] Document operational runbook:
  - [ ] How to bump runner version
  - [ ] How to add a new backend
  - [ ] How to rotate the App private key
  - [ ] How to rotate the OpenStack app credential
  - [ ] How to investigate a stuck slot
  - [ ] How to drain and decommission
  - [ ] How to update the network policy in `husk-policy/policy.toml`
    and roll it out across husk (next slot rebuild), libvirt (Ansible),
    and Cilicon (Ansible)

**Exit criterion**: multiple repos use husk successfully. Monitoring exists.
Operational duties can be handed off.

-----

## Future Extensions

These are sketched here so the Phase 6 controller design leaves the right
seams. **Do not implement these in the initial validation flow.**

### GPU bare-metal backend (`LibvirtBackend`)

- **Host setup** (one-time per bare-metal host):
  - BIOS: VT-d / AMD-Vi enabled, "Above 4G decoding" and "Resizable BAR" on
  - Kernel cmdline: `intel_iommu=on iommu=pt` (or `amd_iommu=on`)
  - vfio-pci binds the GPU
  - Verify IOMMU groups
  - Install libvirt + qemu
- **Image**: qcow2 with NVIDIA driver, Docker, nvidia-container-toolkit, same
  runner systemd unit as OpenStack image
- **Backend implementation**:
  - Slot model maps poorly to bare metal — fewer slots, GPUs as inventory
  - May want a different abstraction here: "ephemeral VM per job" via
    qcow2 overlays, since libvirt doesn't have the Neutron-port-binding cost
- **Labels**: `linux-gpu-1x`, `linux-gpu-4x`, `linux-gpu-a100`, etc.

### macOS backend (deploy Cilicon, not a custom backend)

- **Per Mac**: fresh macOS, dedicated, Cilicon installed and auto-launching
- **Separate GitHub App** (`<org>-cilicon`):
  - Same permissions as husk's App
  - Separate App for blast-radius isolation (different threat model)
  - Distribute `.pem` to each Mac
- **Cilicon config** per Mac: see Cilicon docs
- **No husk controller changes needed**: Cilicon manages its own lifecycle
- **Constraints**: 2 VMs per Mac max (Virtualization.framework limit), no
  nested virtualization → no Docker inside macOS jobs

-----

## Risks and Open Questions

- [x] **OpenStack application credential support**: validated in Phase 0b
- [ ] **OpenStack application credential expiry policy**: needs documenting
- [ ] **CERN OpenStack project visibility**: who else has access?
- [ ] **CERN network egress**: persistent long-poll behavior through any proxy?
- [ ] **Glance snapshot quota**: empirically ≥1 image, need exact number
- [ ] **Compute quota**: 5 instances is tight. Should we request more?
- [x] **Cloud-init re-run on rebuild**: validated in Phase 1 — re-runs with
  fresh user-data on every rebuild; no `cloud-init clean` needed
- [ ] **Hypervisor stickiness**: rebuild ties slots to one hypervisor; what
  happens during host maintenance?
- [ ] **Multi-org future**: design leaves room but don't over-engineer
- [ ] **CERN-internal review**: at what point does this need formal approval?
  Probably before org rollout, possibly earlier.

-----

## Open architectural questions

These came up during validation and are worth resolving (or deciding to defer)
before Phase 6:

1. **When does a slot get destroyed vs. rebuilt?** Rebuild is faster but ties
   the slot to one hypervisor. Periodic destroy/create would spread placement
   but cost the 5-min create each time. Probably: destroy only on
   `ERROR`/maintenance/explicit decommission; rebuild for all normal
   recycling.
2. **What's `min_ready` for a slot pool of 4?** Probably 1 (one warm slot
   ready). Set to 0 only if jobs are very infrequent. Set to 2 if bursts
   are common and you can afford it.
3. **How does the controller know a runner has exited?** Two signals:
   - VM transitions to `SHUTOFF` (proxy for "runner exited and `ExecStopPost`
     fired")
   - Runner record removed from GitHub (for `--ephemeral` runners)
   Use both, with `SHUTOFF` as primary trigger and GitHub state as
   confirmation.
4. **Cloud-init re-run reliability**: ✅ **resolved in Phase 1**. Rebuild
   restores the disk from the image (wiping `/var/lib/cloud`), so cloud-init
   re-runs fresh on every rebuild and applies new `user_data`. No
   `cloud-init clean` and no side-channel needed for JIT delivery. (Caveat:
   readiness is gated by guest boot + cloud-init finishing, not Nova's
   sub-3s `ACTIVE` flip — see Phase 1 findings.)
5. **What to do with the residual SHUTOFF VM if rebuild fails repeatedly?**
   Probably: destroy and create. Add a backoff/retry policy. Don't keep
   re-rebuilding a broken slot.
6. **Which CERN-public ranges go in the allow-before-deny list?**
   `gitlab.cern.ch`, the CERN-customized AlmaLinux mirrors, and possibly
   `registry.cern.ch` need to be reachable but may sit on CERN IP space.
   Approach: validate concretely in Phase 3 by trying `git clone` from
   `gitlab.cern.ch` and a `dnf update` inside a job; expand
   `cern_public_allow` in `policy.toml` until the minimum legitimate
   CERN-public surface works. Document the authoritative source (CERN
   networking docs, ip-services portal, or empirical) for each entry.
7. **What's the right `MAX_JOB_DURATION` default?** Too short and legitimate
   long-compile jobs get killed; too long and runaway jobs sit idle for
   ages. Probably 6h as a starting point, with per-runner-group overrides.
8. **What if Podman compatibility breaks for a specific workflow?** Partly
   answered by Phase 2: the major paths (`container:` jobs, JS/composite
   actions, `docker://` container actions, the `docker` CLI shim) all work,
   with the compatibility gaps closed by the `docker` shim + `containers.conf`
   + `docker.sock` symlink (see Phase 2 findings). For the residual long tail,
   the policy stands: document the symptom, file a docs issue, recommend the
   workflow either adapt (move to a container-based approach) or run on the
   existing manually-managed infrastructure. Husk is opinionated about what it
   supports. Known unsupported: DinD (`docker:dind` services), `--privileged`,
   `services:`, host `sudo apt install`.
9. **When (if ever) to enable strict-allowlist mode?** Runner groups
   handling production secrets or restricted projects might warrant
   default-deny instead of the CERN-denylist primary policy. The
   renderer machinery supports it — a second profile in `policy.toml`
   selected per runner group, with the GitHub `api.github.com/meta`
   refresh job becoming relevant. Defer until there is a concrete need;
   don't pre-build the refresh job.
10. **Should Cilicon and libvirt converge on the same firewall
    primitive on the host side?** Both could use host `nftables`; `pf`
    is macOS-native but means maintaining two template languages in
    `husk-policy/`. Probably keep `pf` for Cilicon (native, no extra
    deps on the Mac) and `nftables` for libvirt — accept the duplicated
    template.
11. Should we support flavor migration? If the target flavor (or image for that matter) changes,
    the controller should try to recreate outdated VMs using the new configuration (reconcile existing vs target).
