# Running huskd on Kubernetes

`base/` holds the manifests; each overlay in `overlays/` supplies the `config.toml`
and the environment-specific bits. Same Deployment spec both places, so the local
run genuinely rehearses the live one.

```
base/          deployment, service          — identical everywhere
overlays/local colima/k3s: emptyDir cache, locally-built image, no ingress
overlays/cern  OpenShift: PVC cache, Route, real pool sizes
```

## Things about huskd that shape these manifests

**It is a singleton.** huskd takes an exclusive `flock` on `controller.lock_path`
(`src/husk/lock.py`) so two daemons can't fight over the same slot set. Hence
`replicas: 1` and `strategy: Recreate` — a RollingUpdate would briefly run two
reconcilers against the same backend. The lock lives on a pod-local `emptyDir`,
never shared storage: it must not survive the pod, and the kernel drops it on crash.

**`http_addr` must be `0.0.0.0:9100`.** The default is `127.0.0.1:9100`, and the
kubelet dials the pod IP for probes — a loopback bind makes every probe fail with
connection refused.

**`/healthz` is the readiness signal.** It returns 503 until every pool has
reconciled recently. A cold start has to pull a ~2 GB golden and talk to the
backend, so a `startupProbe` gives it 3 minutes before liveness starts counting.

## Config and secrets

Nothing with a real value in it is committed. Two mechanisms, following the
convention already used for the repo-root configs:

| | committed | local (gitignored) |
|---|---|---|
| config | `k8s/overlays/*/config.example.toml` | `k8s/overlays/*/config.toml` |
| credentials | — | `secrets/private-key.pem`, `secrets/clouds.yaml` |

```sh
just k8s-init      # copies each example -> config.toml, creates secrets/
# edit the config.toml files, drop your credentials into secrets/
just k8s-secrets   # loads them into the cluster as Secrets
```

`k8s-init` never overwrites an existing `config.toml`, so it's safe to re-run.
Both `config.toml`s are gitignored by the repo-wide `config.toml` rule, and
`secrets/`, `*.pem` and `*.key` are ignored outright — a stray `git add -A` cannot
pick up a credential.

Because the real `config.toml` is gitignored and the overlay's
`configMapGenerator` reads it, **a fresh clone cannot render an overlay until you
run `just k8s-init`** — `kubectl kustomize` fails with a missing-file error. That's
deliberate: it fails loudly rather than silently deploying someone else's settings.

How they reach the pod: the App PEM arrives as `HUSK_GITHUB__PRIVATE_KEY`
(contents, not a path — `config.py` prefers the env var over
`[github].private_key_path`, so no key material is ever on disk in a ConfigMap),
and `clouds.yaml` mounts at `/app/.config/openstack/` because the image pins
`HOME=/app`. `k8s-secrets` uses `create --dry-run | apply`, so re-running it
rotates the values in place.

## Local test drive (colima)

Colima can't add Kubernetes to an existing profile, so this uses a separate `husk`
profile and leaves your `default` docker setup alone. k3s there runs on
cri-dockerd, so a locally-built image is visible to the cluster with no registry
push — that's what `imagePullPolicy: Never` in the local overlay relies on.

```sh
just k8s-start          # colima profile `husk` with k3s, kubectl context switched
just k8s-secrets        # huskd-github + huskd-openstack from your local files
just k8s-render local   # eyeball the manifests before applying
just k8s-local          # build image, apply, wait for rollout
just k8s-logs           # follow huskd
just k8s-forward        # dashboard on http://localhost:9100/
```

This is a **real** run: real GitHub App, real CERN OpenStack, `min_ready = 1`, so
it builds one actual slot. Your colima VM needs to reach the CERN OpenStack API
(VPN / on-site), exactly as running huskd on the laptop does. Drop `min_ready` to
`0` in `overlays/local/config.toml` to exercise auth and Glance image sync without
creating a VM.

Teardown: `just k8s-local-down`, then `just k8s-stop` (or `colima delete husk` to
reclaim the disk).

## How the image gets built

Two different images, easily confused:

- **golden VM images** — `build-images.yml` → `ghcr.io/acts-project/husk-base`
  and `husk-gpu` (qcow2 artifacts), published on **manual dispatch only**. These
  are what slots boot.
- **the huskd daemon container** — `build-app-image.yml` →
  `ghcr.io/acts-project/husk`, pushed on **every main commit** (and `v*` tags),
  tagged `sha-<short>` among others. This is what runs on Kubernetes.

Note the naming: the daemon is the bare repo name `husk`; the goldens are
`husk-base`/`husk-gpu`. Easy to mix up.

The eager-vs-manual difference is deliberate: a golden tag is pinned in huskd
config, so moving it silently changes what every slot boots; the daemon image is
pinned explicitly at deploy time, so having one ready per commit costs nothing.

Locally it's different again — `just k8s-build` builds straight into the colima
profile's docker daemon and the pod runs it with `imagePullPolicy: Never`, no
registry involved. Note the arch gap: local builds are arm64 (Apple silicon), CI
builds amd64 for CERN. The local run exercises the manifests, config, probes and
reconcile loop faithfully, but not the exact binary artifact.

## Live (CERN OpenShift)

Untested — the local run comes first.

**Deploy is manual, by design.** The CERN OpenShift API isn't reachable from
GitHub-hosted runners, so CI stops at pushing the image and deployment is a
hands-on step over the CERN VPN. Setup: VPN up, `oc login`, `oc new-project husk`,
`just k8s-secrets`. Then:

```sh
just k8s-live-diff      # what would change
just k8s-live-deploy    # verify CI image exists, apply, pin SHA, wait
just k8s-live-rollback  # undo one revision
```

`k8s-live-deploy` pins `ghcr.io/acts-project/husk:sha-<HEAD>` — the artifact CI built
from that commit — rather than building on your laptop. So it fails fast if CI
hasn't built the current commit, and warns if the tree is dirty or HEAD isn't on
`origin/main`; all three mean the thing you're about to deploy isn't the thing you
tested. (`sentinel` routes `oc` through `ssh lxplus`; husk doesn't need that now
the VPN exists.)

### Golden-image cache sizing

The PVC is 50Gi. `image_sync.py` keys the cache by content digest (one directory
per digest) and evicts one that no pool still needs 24h after its last use, so the
steady-state footprint is *the goldens in service* plus a day of rollout churn —
not every golden ever pulled. Abandoned `.pull-*` downloads (huskd killed
mid-transfer) are swept too, after 6h.

Note the scope: husk holds qcow2s in *three* places and now garbage-collects all
three — the controller pull cache mounted here (`ImageSync.gc`), per-host goldens
(`libvirt_backend._gc_goldens`) and Glance images
(`openstack_backend._gc_glance`). They are independent sweeps with independent
keep-sets, so a digest can persist in one and not another.

Measured from the published artifacts (ghcr, 2026-07):

| artifact | v3 | v4 |
|---|---|---|
| `husk-base` | 1.93 GB | 1.96 GB |
| `husk-gpu` | 3.92 GB | 4.08 GB |

So ~6 GB in service today, and transiently ~12 GB for the day after both variants
are bumped. 50Gi leaves plenty of headroom; the reason to keep an eye on it is that
a full cache fails the pull for a *new* golden, breaking exactly the rollout you
were attempting. Check with `just k8s-live-cache`. Deleting digest directories
under `/app/.cache/husk/images/` by hand is still safe while running (a deleted
digest is re-pulled on next resolve), but shouldn't be necessary.

Don't rely on remembering to check, though: huskd publishes this PVC's headroom on
`/metrics`, so alert on it.

```promql
husk_filesystem_avail_bytes{kind="cache"}
  / husk_filesystem_size_bytes{kind="cache"} < 0.15
```

That fires on *headroom* rather than a byte count, so it survives resizing the PVC.
GC does not make it redundant: it bounds what *husk* puts on the volume and says
nothing about anything else sharing it, and it cannot help if pins stop being
released. Headroom is the measurement that stays true either way.

**Verify it once on the cluster before trusting it.** huskd gets these from
`statvfs`, and on CephFS statvfs reports the subvolume quota only if ceph-csi set
one and `client_quota_df` is on. `just k8s-live-cache` prints `df -h` for exactly
this: if Size reads ~50G the alert works; if it reads the whole Ceph cluster's
capacity, `avail/size` never drops and the alert silently never fires — use
`kubelet_volume_stats_available_bytes` instead, which is authoritative for PVCs.
See "Headroom" in `observability.md`.

### Metrics state (the second PVC)

`huskd-metrics-state` (1Gi, mounted at `/var/lib/husk`) holds huskd's accumulated
counters and histograms — action-failure counts, recycle-duration distributions,
reconcile aborts. Without it they reset to zero on every pod restart, and since
huskd has **no config hot-reload**, every config change is a restart. So
`increase(husk_action_failures_total[30d])` would quietly only see back as far as
the last deploy, and a "p95 recycle time this month" would really be "since the
last ConfigMap edit".

Two things make this cheap to reason about:

- **It cannot grow with usage.** No event-time instrument carries a per-slot label
  (there is a test asserting this), so the series count is bounded by config —
  pools × actions × reasons × buckets — not by how many slots have passed through.
  Measured: 3.4 KB for two pools after 500 recycles, and 11.7 KB for six pools with
  every labelset populated — roughly the ceiling. 1Gi is just a comfortable
  minimum request, not a sizing estimate.
- **Losing it is not an incident.** huskd logs the failure and starts from zero,
  which Prometheus reads as an ordinary counter reset. The same is true of a
  corrupt file or a bucket-boundary change: the policy is "reset loudly", never a
  migration.

It is a **separate claim from the image cache** on purpose despite being tiny. The
two have unrelated lifetimes and sizes four orders of magnitude apart, the cache is
swept on its own schedule by `ImageSync.gc`, and it is the directory operators are
pointed at when reclaiming space — so a state file parked inside it invites an
`rm -rf` that takes the metrics with it.

`Recreate` means the old pod detaches the volume before the new one attaches, so
`ReadWriteOnce` is correct and there is no multi-writer window. huskd flushes every
60s and once more on shutdown, so a clean rollout loses nothing and an ungraceful
kill loses at most a minute.

## Not covered yet

- **libvirt pools.** The cern overlay runs the OpenStack pool only. libvirt needs
  SSH from the pod to the VM hosts (`qemu+ssh://`), so a key Secret at `/app/.ssh`,
  a `known_hosts` entry, and cluster egress to those hosts. Add once proven.
- **`advertise_addr`.** Set it to the Route hostname once assigned, or
  `/sd/targets` hands central Prometheus the pod-internal bind address.
