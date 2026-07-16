# husk

Ephemeral GitHub Actions runners on demand. `huskd` watches a repo's job queue and
keeps a pool of single-use runner VMs warm, recycling each slot (power off →
rebuild → re-register) after every job. Slots boot from a **golden image** that
bakes the slow/static layers (rootless podman, the runner binary, systemd units,
and — for GPU pools — the NVIDIA driver + toolkit); cloud-init then delivers only
the per-cycle dynamic bits (JIT config, egress firewall ruleset, start).

- Backends: OpenStack (CERN) and libvirt/QEMU (GPU passthrough).
- Deeper design docs: [`image-pipeline.md`](image-pipeline.md) (image ↔ cloud-init
  boundary), [`plan.md`](plan.md) (roadmap).

## Running huskd in Docker

The controller ships as a container image at `ghcr.io/acts-project/husk`, built by
the [`build-app-image`](.github/workflows/build-app-image.yml) workflow. Pushes to
`main` publish `:latest` and `:sha-<short>`; version tags (`v*`) also publish
`:X.Y.Z` and `:X.Y`. (This is the **daemon** image — distinct from the
`husk-{base,gpu}` **VM** images above, which are the qcow2s slots boot from.)

`huskd` is the whole process: it serves the HTTP surface (dashboard +
`/status` `/metrics` `/healthz` `/events`) on hypercorn *and* runs the reconcile
loop on a background thread under one process-wide lock, with SIGTERM wired to a
graceful shutdown. There is **no** separate ASGI entrypoint to add — an external
`uvicorn`/`gunicorn` worker would serve the web routes but skip the reconcile
loop, and multiple workers would fight the single-controller lock. So run exactly
one container per config.

Mount your config (and any secrets), pass the GitHub PAT (`github.pat_env`,
default `GH_TOKEN`), and expose the dashboard port:

```sh
docker run --rm \
  -p 9100:9100 \
  -v ./config.toml:/etc/husk/config.toml:ro \
  -e GH_TOKEN="$(gh auth token)" \
  ghcr.io/acts-project/husk:latest
```

For local dev, `just docker-run [config]` wraps this — it builds the image,
mounts the config, forwards `GH_TOKEN` (falling back to `gh auth token`) and any
`OS_*` OpenStack vars, and mounts `~/.config/openstack`.

Two things the mounted config must account for:

- Set `controller.http_addr = "0.0.0.0:9100"` — the default `127.0.0.1:9100` is
  only reachable inside the container. (The image's `HEALTHCHECK` uses
  `127.0.0.1`, so it works either way.)
- For k8s-style secret mounts, add `--secrets-dir` by overriding the command:
  `... ghcr.io/acts-project/husk:latest --config /etc/husk/config.toml --secrets-dir /etc/husk/secrets`.

The image carries **both** backends — OpenStack and libvirt/QEMU (the `libvirt`
extra is compiled in). For libvirt pools, huskd reaches each host over `qemu+ssh://`
and bridges guest metrics scrapes over that same SSH channel, so also mount an SSH
identity and `known_hosts` for the runtime user (`/app/.ssh`), e.g.
`-v ./husk-ssh:/app/.ssh:ro`, and point each host's `ssh_target` at it.

## Rebuilding the golden image

The image is the single source of truth for everything baked into a slot. Its
contents are defined by [`images/build.sh`](images/build.sh) +
[`images/files/`](images/files/), with pinned inputs in
[`images/versions.env`](images/versions.env). Rebuild whenever you change any of
those — e.g. bumping the runner version, the base AlmaLinux image, or a baked unit
(like `husk-bootreport.service`).

There are two variants, built from the same spec: **`base`** (CPU) and **`gpu`**
(base + NVIDIA driver/toolkit).

### Recommended: build + publish in CI

The [`build-images`](.github/workflows/build-images.yml) workflow builds both
variants with `virt-customize` and publishes the qcow2s to `ghcr.io` via ORAS.
Publishing happens **only** on a manual dispatch (pushes just build + smoke-test,
so branches never publish by accident). Pin the release tag with the `version`
input:

```sh
just publish v3        # → gh workflow run build-images.yml -f version=v3
```

Each variant is pushed to `ghcr.io/<org>/husk-{base,gpu}`, tagged with both
`version` and the short git SHA, and referenceable by immutable digest. The run
summary prints the repo, tags, and digest for each. Watch it with:

```sh
gh run watch "$(gh run list --workflow build-images.yml -L1 --json databaseId -q '.[0].databaseId')"
```

### Local build (no CI, no KVM)

`build.sh` runs **offline** via libguestfs (`virt-customize`), so it needs no KVM
or GPU — CDI generation for the GPU variant is deferred to first boot. Install
`guestfs-tools` + `qemu-utils`, then:

```sh
just rebuild            # CPU variant → husk-base.qcow2
just rebuild gpu        # GPU variant → husk-gpu.qcow2
just rebuild-all        # both

# extra flags pass through to build.sh:
just rebuild base --runner-version 2.334.0 --out /tmp/husk-base.qcow2
```

(These wrap [`images/build.sh`](images/build.sh) directly — run it by hand if you
don't have [`just`](https://github.com/casey/just).)

Handy flags: `--out FILE`, `--runner-version V`, `--base-url URL`,
`--disk-size SIZE` (defaults per variant come from `versions.env`). To publish a
locally built image, `oras push` it the same way the workflow's "Publish" step
does (see [`build-images.yml`](.github/workflows/build-images.yml)).

### Rolling it out

huskd pulls the image by its config-pinned `image_ref` and fans it out (libvirt:
scp to each host's pool by digest; OpenStack: upload to Glance) — idempotent on
digest, and it never overwrites an in-use backing file. To adopt a new build,
point each pool's `image_ref` at the new tag/digest in your config (e.g.
`config.multi.toml`) and reload; slots pick it up on their next recycle. See
[`image-pipeline.md`](image-pipeline.md#versioning-rollout-drain) for the
drain/rollout mechanics.

### Reading recycle timing

The golden image bakes `husk-bootreport.service`, a oneshot that cloud-init starts
right after the runner. It dumps `systemd-analyze` + `cloud-init analyze blame` to
the serial console on every boot (no SSH needed, off the registration critical
path). After a rebuild + recycle, read the slot's console log — libvirt's console
log file or OpenStack `nova console-log` — for the block between
`===== husk-bootreport =====` markers to see exactly where boot time goes
(network-online wait, podman socket wait, per-unit times).
