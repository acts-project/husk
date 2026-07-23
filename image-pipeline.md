# Husk VM Image Pipeline

How husk builds, distributes, and rolls out the golden VM images that back its
runner slots. Companion to `plan.md` (which deliberately deferred custom images
in "Phase 4"); this document un-defers that work with a concrete design.

> **Status (2026-06-16):** Phase A (CI build → ghcr via ORAS) merged. **Phases B
> + C are built for BOTH backends.** The shared registry half
> (`src/husk/image_sync.py`, now a base dep — no longer libvirt-only) pulls the
> config-pinned `image_ref` via the pure-Python `oras` client to a controller
> cache, content-addressed by qcow2 digest. From there:
> - **libvirt** (`LibvirtBackend.sync_images`): scp's the golden to each host pool
>   by digest; stamps the digest into domain metadata; drains slots onto a new ref
>   after a restart; GC's orphaned goldens.
> - **OpenStack** (`OpenStackBackend.sync_images`): uploads the qcow2 to Glance as
>   `husk-golden-<digest>` (idempotent on digest), rotates the current image id,
>   drains stale slots through the recycle loop on a ref change, and GC's
>   superseded Glance goldens. The **same qcow2 serves both backends** — the goal
>   in Goal 4. On both, a new `image_ref` is picked up when huskd restarts.
>
> The controller cache itself is GC'd too (`ImageSync.gc`): each pool pins the
> digests it still needs, and a digest nobody pins is dropped 24h after its last
> use, along with `.pull-*` debris from a huskd killed mid-download.
>
> Every pool boots a golden image: the stock-base path (cloud-init installing the
> runner/podman stack at boot) has been removed, so `image_ref`/`image_name` must
> name a husk golden. **Open:** live-confirm one qcow2 boots cleanly on a CERN
> Glance upload (datasource/login/ConfigDrive) as it already does on libvirt.

-----

## Goals

1. **Pre-bake the slow, static layers** into a VM image so slot recycle drops
   from ~65s (current, cloud-init reinstalls everything) toward the ~5s
   rebuild+start floor. Recycle latency was the measured trigger for this work
   (`plan.md` Phase 3/4).
2. **One declarative source of truth** for image contents, producing **two
   variants**: `husk-base` (CPU) and `husk-gpu` (CPU + NVIDIA driver/toolkit).
3. **Build in GitHub CI**, publish the qcow2 artifacts to **public `ghcr.io`**
   via **ORAS** (OCI artifacts), versioned by tag + digest.
4. **husk orchestrates delivery**: the controller pulls a config-pinned image
   ref and delivers it to every libvirt host *and* uploads the base variant to
   OpenStack Glance — the **same qcow2 serves both backends**.

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Image substrate | **qcow2** | Backing file for libvirt COW overlays *and* uploadable to Glance — one artifact, both backends. |
| Registry / transport | **public ghcr.io via ORAS** | OCI artifacts give us tags + content-addressed digests for free; public ⇒ no pull creds. |
| Pull location | **ORAS on the controller** | huskd pulls once, fans out over its existing SSH channel + the OpenStack SDK. Hosts need no oras/creds. |
| Rollout trigger | **config-pinned tag/digest** | Edit the ref, restart huskd; `sync_images` diffs it against what the hosts/Glance already hold. Explicit, auditable. |
| OpenStack scope | **base (non-GPU) variant only** | OpenStack is the CPU-runner path; GPU is libvirt/bare-metal. The GPU image ships only to libvirt hosts. |
| Image vs cloud-init | **image = slow/static capability; cloud-init = dynamic/tunable policy** | See boundary below. |
| Firewall | **capability baked, policy in cloud-init** | The ruleset is the one security knob meant to change without an image rebuild. |

-----

## Image vs cloud-init boundary

The image carries everything slow and static; cloud-init carries everything
per-slot, per-job, or meant to change without a rebuild.

**Baked into the image (both variants unless noted):**

- Runner binary (pinned version) + `installdependencies.sh` native deps
  (`libicu`, `krb5`, `openssl`, …) already installed.
- `podman` + rootless stack (`fuse-overlayfs`, `slirp4netns`, `netavark`,
  `aardvark-dns`), `podman-docker`.
- The Docker→Podman compatibility artifacts (all static): the `/usr/local/bin/docker`
  shim, `containers.conf` (`label = false`), per-user `storage.conf`,
  `/etc/containers/nodocker`, the `husk-docker-sock` tmpfiles rule.
- The `runner` user (uid 1000) + `husk-runner.service` / `husk-poweroff.service`
  unit files (static; **not** enabled for boot — cloud-init still starts them).
- `husk-bootreport.service` — a oneshot that dumps `systemd-analyze` +
  `cloud-init analyze blame` to the serial console after the runner starts, so
  recycle timing is observable without SSH (static; started by cloud-init, like
  the runner unit). Reads only timestamps already recorded, so it's always-on.
- `nftables` package + service (the firewall *engine*, not the ruleset).
- `node_exporter` (pinned + checksummed in `versions.env`) + `husk-node-exporter
  .service` — the in-guest per-VM metrics source (`observability.md`). Runs as its
  own unprivileged user, never `runner`; **no TLS, no auth** (access control is the
  `:9100` nftables allowlist, which is *policy* and therefore cloud-init's). Static
  capability, so it's baked; **not** enabled for boot — cloud-init starts it only
  when the pool sets `scrape_cidr`, and only after the ruleset lands.
- **GPU variant only:** NVIDIA driver + `nvidia-container-toolkit`, plus the
  CDI-on-first-boot oneshot (`husk-cdi.service`). CDI generation stays first-boot
  — it needs the driver loaded against a present GPU, which an offline build
  can't provide.
- The **CernVM-FS client** (`cvmfs` pinned in `versions.env` + `cvmfs-config-default`)
  and `autofs`, wired by `cvmfs_config setup`. The slow/static capability is baked;
  the per-pool repo list, HTTP proxy, cache quota, and the per-cycle eager-mounts
  are cloud-init's (below). `autofs` **is** enabled for boot (unlike the runner
  units) since it only arms the `/cvmfs` automount map — nothing mounts or reaches
  the network until cloud-init probes a repo.

**Delivered by cloud-init each create/recycle (dynamic):**

- The JIT config blob (`/var/lib/husk/jitconfig`) — fresh per cycle.
- The **firewall ruleset** (`husk-egress.nft`) + `nft -f` apply — the tunable
  policy. Same conceptual rule across backends; rendered from `husk-policy`
  later (`plan.md` "Network policy rollout"). This now includes the **`:9100`
  metrics ingress allowlist** (per-pool `scrape_cidr`): it's policy, and its value
  differs per backend, so it must be changeable without an image rebuild.
- The NoCloud seed / instance-id (rotates so cloud-init re-runs).
- Start orchestration: the (baked, not-enabled) units, in this order —
  `husk-node-exporter.service` if `scrape_cidr` is set (after the ruleset, so
  `:9100` is never briefly open), then `systemctl start husk-runner.service` (the
  sole boot orchestrator, as today), then a non-blocking `husk-bootreport`.
- **CernVM-FS** when the pool sets `[pool.cvmfs]`: the client
  config (`/etc/cvmfs/default.local` — proxy, repo list, quota), a
  `containers.conf.d` drop-in that binds each `/cvmfs/<repo>` into every job
  container (**per-repo**, not a whole-`/cvmfs` bind — the autofs root readdir is
  denied under the rootless user namespace, but a bind of an already-mounted repo
  tree is not), a proxy hole in the ruleset (a `cvmfs_proxy` set populated by an
  **in-guest** resolve of the proxy hostnames, since CERN squids sit inside the
  dropped CERN-internal ranges), and an eager-mount of each repo — all after the
  `nft` apply and before the runner, so the binds land on already-mounted trees.
- **OOM policy drop-ins:** always an `OOMScoreAdjust=-900` drop-in on
  `husk-runner.service` (protects the runner agent so the baked earlyoom kills the
  job instead); and, when the pool sets `[pool.container] memory_max`, a
  `MemoryMax=` drop-in on `user-1000.slice` — the cgroup every rootless-podman job
  container nests under — for a hard, memcg-confined cap on top of earlyoom's
  soft, percentage-based backstop. Both are picked up by the `daemon-reload`
  before the runner starts.
- Wall-clock backstop (`shutdown -h +N`).

> **Side effect of baking the packages:** the reason the firewall is applied at
> the very end of `runcmd` today — keep CERN mirrors reachable during package
> install — largely goes away (no runtime install). The default-allow-public
> ruleset still lets runner/action downloads through even if applied earlier.

**cloud-init multi-datasource requirement:** the image must enable both
`NoCloud` (libvirt seed ISO) and `OpenStack` (Glance metadata / ConfigDrive) in
`datasource_list`, so one artifact boots on both backends.

-----

## Build (GitHub CI → ghcr.io)

- **Single spec, two variants.** A declarative package/setup list drives a
  `virt-customize` build parameterized by `--variant {base,gpu}`. This replaces
  the manual, GPU-only `scripts/build-golden-image.sh`.
- **Pinned inputs** (recorded in a manifest annotation on the artifact):
  base-image URL (a specific dated qcow2, **not** `-latest`), runner version
  (shared with `[runner] version` in huskd config), driver + toolkit package
  versions.
- **Publish via ORAS** to `ghcr.io/<org>/husk-{base,gpu}`, tagged with a
  release version + the git SHA, and referenced elsewhere by immutable digest.
  Artifact type e.g. `application/vnd.husk.vmimage`, layer mediaType
  `application/vnd.husk.qcow2`.
- **CI gotchas to handle** (see implementation plan below): `/dev/kvm` may be
  absent on the runner (libguestfs falls back to TCG emulation, slower);
  libguestfs needs a world-readable kernel on Ubuntu runners
  (`chmod 0644 /boot/vmlinuz-*`).

## Delivery & sync (huskd, control machine)

Driven by a **config-pinned image ref per backend** (replacing the fixed
`image_name` filename). On a config change (picked up at the next huskd start):

1. `oras pull` the ref once to a controller-local cache.
2. **libvirt:** push the qcow2 to each host's pool dir over the existing SSH
   channel, named by **digest** (`husk-gpu-<digest>.qcow2`). Idempotent: skip
   hosts already holding that digest. Never overwrite an in-use backing file.
3. **OpenStack:** upload the base qcow2 to Glance (`--disk-format qcow2
   --container-format bare`) with the right image properties; the new Glance
   image ID becomes "current." Idempotent on digest; retain N, GC older
   (CERN image quota is tight, `plan.md` Phase 0).

## Versioning, rollout, drain

The one genuinely new mechanism. A golden qcow2 is the **backing file** of every
live per-slot overlay — overwriting it in place corrupts running slots. So:

- Images are **content-addressed / version-tagged**, never overwritten. The
  configured `image_name` filename becomes a *derived* on-disk name; the config
  holds an image **ref**.
- **Stamp the image digest into slot metadata** (libvirt domain metadata already
  carries `cycle`/`unit`; add `image_digest`). OpenStack slots already record an
  image id.
- **Drain on digest change:** new creates/rebuilds use the new ref; slots whose
  `image_digest != current` are classified as needing recycle and drain through
  the existing recycle loop; the old backing file / Glance image is GC'd once
  nothing references it.

## Code changes (delivery side — separate phase)

- `config.py`: `image_name` → image **ref** (tag or digest) per backend/host;
  filename / Glance ID become derived.
- New `image_sync.py`: `resolve(ref) → ensure present on every target →
  concrete local id` (qcow2 path per host; Glance image-id). Holds oras pull +
  scp + Glance upload + GC.
- `libvirt_backend.py`: `_make_overlay` builds off the *resolved* golden path;
  stamp `image_digest` into domain metadata.
- `openstack_backend.py`: stop caching one `image_id` at init
  (`openstack_backend.py:58-67`); let it rotate from the sync result.
- `controller.py`: each tick ensure the desired image is delivered before
  create/rebuild; classify stale-digest slots as needs-recycle (drain); GC.

-----

## Phasing

- **Phase A — CI image build. ✅ DONE.** Single spec, two variants, built in
  GitHub CI, pushed to ghcr via ORAS. Output: pullable `husk-base` + `husk-gpu`
  artifacts (`build-images.yml`). *Does not touch huskd.*
- **Phase B — delivery & sync. ✅ DONE (both backends).** `image_sync.py` (`oras
  resolve`+`pull` to a controller cache, content-addressed) is the shared registry
  half. `LibvirtBackend.sync_images` scp's the golden to each host pool by digest
  (idempotent, atomic); `OpenStackBackend.sync_images` uploads the qcow2 to Glance
  as `husk-golden-<digest>` (idempotent on digest, rotates the current image id).
  Both driven by the config `image_ref`.
- **Phase C — versioned rollout / drain. ✅ DONE (both backends).** libvirt stamps
  the digest into domain metadata; OpenStack compares each slot's Glance image id
  to the current. `Slot.image_stale` is set when they diverge, and the controller
  drains stale idle slots (rebuild adopts the new golden) — same drain path on both
  backends. libvirt `_gc_goldens` removes unreferenced backing files; OpenStack
  `_gc_glance` deletes superseded `husk-golden-*` Glance images; `ImageSync.gc`
  evicts unpinned digests (and dead `.pull-*` dirs) from the controller cache. A changed
  `image_ref` takes effect on the next huskd start (huskd never reloads config).

Each phase is independently useful: A produces artifacts you can place by hand
(exactly the manual step today, just reproducible); B automates delivery against
a static ref; C makes updates safe while slots run.

## Validation / open questions

- **One artifact, two targets:** confirm a hand-built qcow2 with
  `datasource_list: [NoCloud, OpenStack]` boots cleanly on *both* a libvirt slot
  and a CERN Glance upload (login user, network config, ConfigDrive). The whole
  "same qcow2 both backends" idea rests on this.
- **GPU driver kept out of cloud-init:** retire the runtime `_GPU_RUNCMD` path
  once the driver is baked; keep only CDI-on-first-boot.
- **Glance image properties:** match what CERN's stock image sets (cloud-init
  datasource, `hw_*` props, login user) so a custom image boots under Nova.
- **ghcr artifact size:** multi-GB qcow2 over ORAS — fine for ghcr, but confirm
  push/pull throughput in CI is acceptable; consider compression.
