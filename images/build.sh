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
DISK_SIZE=""  # empty → resolved from the per-variant default below; --disk-size wins
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
# Per-variant disk size (versions.env), unless --disk-size overrode it explicitly.
if [[ -z "$DISK_SIZE" ]]; then
  case "$VARIANT" in
    base) DISK_SIZE="$BASE_DISK_SIZE" ;;
    gpu)  DISK_SIZE="$GPU_DISK_SIZE" ;;
  esac
fi

RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
NODE_EXPORTER_TGZ="node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz"
NODE_EXPORTER_URL="https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/${NODE_EXPORTER_TGZ}"

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

  # Give the guest a real resolver for the duration of the build. The base
  # image ships a stub/empty resolv.conf, and build hosts vary (CI uses qemu
  # SLIRP user-net; a libvirt host uses NAT) — 1.1.1.1/8.8.8.8 are reachable
  # through any outbound NAT, so dnf can resolve mirrors regardless. The booted
  # slot manages its own resolv.conf (NetworkManager/cloud-init), so this is
  # build-time only in practice.
  --run-command 'printf "nameserver 1.1.1.1\nnameserver 8.8.8.8\n" > /etc/resolv.conf'

  # Container stack + runner native deps + firewall ENGINE (ruleset is runtime).
  --install "podman,podman-docker,fuse-overlayfs,slirp4netns,netavark,aardvark-dns,libicu,sudo,curl,jq,git,nftables,tar"

  # OOM handling: earlyoom watches free RAM and, before the kernel's blunt OOM
  # killer fires, SIGTERMs the largest-oom_score process — steered to the JOB (not
  # the agent) by the -900 OOMScoreAdjust cloud-init drops on husk-runner.service.
  # Percent-based, so one config fits every flavor/host RAM. Lives in EPEL; its
  # only deps (glibc, systemd) are already present. The stock /etc/default/earlyoom
  # is overwritten by images/files/earlyoom.default (copied in below).
  --run-command 'dnf -y install epel-release'
  --run-command 'dnf -y install earlyoom'
  --run-command 'systemctl enable earlyoom.service'

  # CernVM-FS client + autofs wiring (baked; cloud-init supplies the per-pool repo
  # list, the HTTP proxy, and the per-cycle eager-mounts). cvmfs-config-default
  # pulls the CERN config-repo so any *.cern.ch repo resolves; `cvmfs_config setup`
  # wires autofs under /cvmfs and creates the cvmfs user. autofs is enabled so it
  # is up at boot (cloud-init also starts it each cycle). The cvmfs-release RPM adds
  # the pinned client's yum repo. No network to the Stratum-1/proxy is needed here —
  # the boot-time firewall + eager-mount own that (see cloudinit.py).
  --run-command 'dnf -y install https://ecsft.cern.ch/dist/cvmfs/cvmfs-release/cvmfs-release-latest.noarch.rpm'
  --run-command "dnf -y install cvmfs-${CVMFS_VERSION} cvmfs-config-default"
  --run-command 'cvmfs_config setup'
  --run-command 'systemctl enable autofs'

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
  # Boot-timing report; NOT enabled (cloud-init starts it, after the runner).
  --copy-in "$FILES/husk-bootreport.service:/etc/systemd/system/"
  --copy-in "$FILES/husk-bootreport:/usr/local/bin/"
  --run-command 'chmod 0755 /usr/local/bin/husk-bootreport'
  --copy-in "$FILES/90-husk-datasource.cfg:/etc/cloud/cloud.cfg.d/"
  # earlyoom tuning (overwrites the EPEL package's stock config). --copy-in keeps
  # the source basename, so land it then rename to the EnvironmentFile path.
  --copy-in "$FILES/earlyoom.default:/etc/default/"
  --run-command 'mv -f /etc/default/earlyoom.default /etc/default/earlyoom'

  # Bake the runner binary + its native deps so recycle doesn't reinstall them.
  --run-command "cd /opt/actions-runner && curl -fL '$RUNNER_URL' | tar xz"
  --run-command '/opt/actions-runner/bin/installdependencies.sh'

  # node_exporter — in-guest per-VM metrics (observability.md). Baked in both
  # variants; the unit is NOT enabled (cloud-init starts it per cycle, after the
  # firewall, and only when the pool sets `scrape_cidr`). Checksum-pinned like
  # the base image: this is a binary we fetch off the internet at build time.
  --run-command "curl -fL '$NODE_EXPORTER_URL' -o /tmp/$NODE_EXPORTER_TGZ"
  --run-command "echo '$NODE_EXPORTER_SHA256  /tmp/$NODE_EXPORTER_TGZ' | sha256sum -c -"
  --run-command "tar -xzf /tmp/$NODE_EXPORTER_TGZ -C /tmp"
  --run-command "install -m 0755 /tmp/node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64/node_exporter /usr/local/bin/node_exporter"
  --run-command "rm -rf /tmp/$NODE_EXPORTER_TGZ /tmp/node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64"
  # Dedicated unprivileged user — never `runner` (which is what the untrusted job runs as).
  --run-command 'id -u node_exporter >/dev/null 2>&1 || useradd -r -s /sbin/nologin node_exporter'
  --copy-in "$FILES/husk-node-exporter.service:/etc/systemd/system/"
  # Textfile collector directory: husk-bootreport (a root oneshot) writes the
  # boot-timing .prom here, node_exporter reads it unprivileged. Root-owned so
  # the untrusted job cannot forge metrics; must exist before the first scrape.
  --run-command 'install -d -m 0755 -o root -g root /var/lib/node_exporter/textfile'

  # Ownership (copy-in lands as root; fix up the runner-owned trees).
  --run-command 'chown -R runner:runner /opt/actions-runner /var/lib/husk /home/runner'

  # Rootless podman socket without a user bus: enable globally (root), then
  # linger so user@1000 auto-starts it at boot. husk-runner.service is NOT
  # enabled — cloud-init starts it each cycle after laying the JIT config.
  --run-command 'systemctl --global enable podman.socket'
  --run-command 'mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/runner'

  # SELinux: the CI build host is non-SELinux Ubuntu, which can't reliably relabel
  # an EL guest offline — virt-customize's --selinux-relabel (below) only defers by
  # touching /.autorelabel, and a first boot under *enforcing* then deadlocks:
  # nothing is labeled, so nothing — including the autorelabel service itself —
  # can exec, and every unit dies with status=127 (a wedged VM, no serial output).
  # Boot permissive so exec is never blocked; the deferred autorelabel still runs
  # and completes. Safe for husk: runners are ephemeral + firewall-isolated, and
  # cloud-init already sets containers.conf label=false, so guest SELinux is not
  # load-bearing here.
  --run-command "sed -i 's/^SELINUX=.*/SELINUX=permissive/' /etc/selinux/config"
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
