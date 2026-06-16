#!/usr/bin/env bash
# Build a husk golden VM image (qcow2): the `base` (CPU) or `gpu` variant.
#
# Single source of truth for image *contents*. Bakes the slow/static layers —
# the actions-runner + its native deps, rootless podman + the Docker->Podman
# compat shim, the systemd units, the nftables ENGINE (not the ruleset). The
# gpu variant adds the NVIDIA driver + container-toolkit. cloud-init still
# delivers the per-slot dynamic bits at boot (JIT config, the firewall RULESET,
# the NoCloud seed). See image-pipeline.md for the image/cloud-init boundary.
#
# Runs OFFLINE via libguestfs (virt-customize) — no KVM or GPU needed, so this
# works in GitHub CI. CDI generation for the gpu variant is deferred to first
# boot (husk-cdi.service): it needs the driver loaded against a real GPU, which
# an offline build can't provide.
#
# Usage: images/build.sh --variant base|gpu [--out FILE] [--runner-version V]
#                        [--base-url URL] [--disk-size SIZE]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES="$HERE/files"

# shellcheck disable=SC1091
source "$HERE/versions.env"

VARIANT=base
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant)        VARIANT="$2"; shift 2 ;;
    --out)            OUT="$2"; shift 2 ;;
    --runner-version) RUNNER_VERSION="$2"; shift 2 ;;
    --base-url)       BASE_IMAGE_URL="$2"; shift 2 ;;
    --disk-size)      DISK_SIZE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
case "$VARIANT" in base|gpu) ;; *) echo "--variant must be base|gpu" >&2; exit 2 ;; esac
OUT="${OUT:-husk-${VARIANT}.qcow2}"

RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"

command -v virt-customize >/dev/null || {
  echo "need guestfs-tools (dnf install guestfs-tools / apt install guestfs-tools)" >&2
  exit 1
}

# ------------------------------------------------- fetch + size the base image
BASE_CACHE="${BASE_CACHE:-/tmp/husk-base-$(basename "$BASE_IMAGE_URL")}"
if [[ ! -f "$BASE_CACHE" ]]; then
  echo "==> downloading base image: $BASE_IMAGE_URL"
  curl -fL "$BASE_IMAGE_URL" -o "$BASE_CACHE"
fi
if [[ -n "${BASE_IMAGE_SHA256:-}" ]]; then
  echo "==> verifying base image checksum"
  echo "${BASE_IMAGE_SHA256}  ${BASE_CACHE}" | sha256sum -c -
fi

echo "==> preparing $OUT (resize to $DISK_SIZE)"
qemu-img convert -O qcow2 "$BASE_CACHE" "$OUT"
qemu-img resize "$OUT" "$DISK_SIZE"

# ----------------------------------------------------------- common (base+gpu)
# virt-customize performs operations in command-line order. Install first (so
# target dirs like /etc/containers exist), create the runner user, then lay the
# static files, then run setup, then SELinux-relabel last.
ARGS=(
  -a "$OUT"

  # Container stack + runner native deps + firewall ENGINE (ruleset is runtime).
  --install "podman,podman-docker,fuse-overlayfs,slirp4netns,netavark,aardvark-dns,libicu,sudo,curl,jq,git,nftables,tar"

  # Unprivileged runner user (uid 1000 lines up with /run/user/1000 in the unit).
  --run-command 'id -u runner >/dev/null 2>&1 || useradd -u 1000 -m -s /bin/bash runner'
  --run-command 'passwd -l runner'

  --mkdir /opt/actions-runner
  --mkdir /var/lib/husk
  --mkdir /home/runner/.config/containers
  --mkdir /etc/cloud/cloud.cfg.d

  # Static compat + unit files (single source: images/files/).
  --copy-in "$FILES/docker:/usr/local/bin/"
  --chmod '0755:/usr/local/bin/docker'
  --copy-in "$FILES/containers.conf:/etc/containers/"
  --copy-in "$FILES/nodocker:/etc/containers/"
  --copy-in "$FILES/storage.conf:/home/runner/.config/containers/"
  --copy-in "$FILES/husk-docker-sock.conf:/etc/tmpfiles.d/"
  --copy-in "$FILES/husk-runner.service:/etc/systemd/system/"
  --copy-in "$FILES/husk-poweroff.service:/etc/systemd/system/"
  --copy-in "$FILES/90-husk-datasource.cfg:/etc/cloud/cloud.cfg.d/"

  # Bake the runner binary + its native deps so recycle doesn't reinstall them.
  --run-command "cd /opt/actions-runner && curl -fL '$RUNNER_URL' | tar xz"
  --run-command '/opt/actions-runner/bin/installdependencies.sh'

  # Ownership (copy-in lands as root; fix up the runner-owned trees).
  --run-command 'chown -R runner:runner /opt/actions-runner /var/lib/husk /home/runner'

  # Rootless podman socket without a user bus: enable globally (root), then
  # linger so user@1000 auto-starts it at boot. husk-runner.service is NOT
  # enabled — cloud-init starts it each cycle after laying the JIT config.
  --run-command 'systemctl --global enable podman.socket'
  --run-command 'mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/runner'
)

# --------------------------------------------------------------- gpu additions
# AlmaLinux's precompiled open kmod (no DKMS — the GPU POC's one-time blocker).
# modprobe + CDI generation are NOT done here (no GPU at build time); husk-cdi
# .service generates the CDI spec on first boot against the passed-through GPU.
if [[ "$VARIANT" == gpu ]]; then
  ARGS+=(
    --run-command 'dnf -y install almalinux-release-nvidia-driver'
    --run-command 'dnf -y install nvidia-open-kmod nvidia-driver nvidia-driver-cuda'
    --copy-in "$FILES/nvidia-container-toolkit.repo:/etc/yum.repos.d/"
    --run-command 'dnf -y install nvidia-container-toolkit'
    --copy-in "$FILES/husk-cdi.service:/etc/systemd/system/"
    --run-command 'systemctl enable husk-cdi.service'
    --run-command 'systemctl enable nvidia-persistenced.service || true'
    # Don't let a kernel update break the precompiled kmod match.
    --run-command "echo 'exclude=kernel*' >> /etc/dnf/dnf.conf"
  )
fi

ARGS+=(--selinux-relabel)

echo "==> customizing $OUT (variant=$VARIANT, runner=$RUNNER_VERSION)"
virt-customize "${ARGS[@]}"

echo "==> done: $OUT"
qemu-img info "$OUT" | sed 's/^/    /'
