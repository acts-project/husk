#!/usr/bin/env bash
# DEPRECATED: superseded by `images/build.sh --variant gpu` (single source of
# truth for both base+gpu variants, CI-buildable; see image-pipeline.md). Kept
# until the new gpu path is validated on a real GPU host. Prefer images/build.sh.
#
# Build the husk golden GPU guest image: AlmaLinux 10 + NVIDIA driver +
# nvidia-container-toolkit + podman, with a boot-time CDI generation oneshot.
#
# Run this ON the libvirt VM-host (it needs libguestfs-tools + network). The
# output qcow2 is dropped into the libvirt storage pool dir so the LibvirtBackend
# can use it as the COW backing image.
#
# CDI is generated at FIRST BOOT (not here): `nvidia-ctk cdi generate` needs the
# driver loaded against a present GPU, which offline virt-customize can't provide.
# So we bake the driver + toolkit + a oneshot, and the spec materializes on boot.
#
# Driver note: el10 NVIDIA package names move around. The block below uses the
# CUDA repo + DKMS open kernel modules built against the image's kernel; if your
# repo publishes a precompiled kmod stream, prefer it (no per-image DKMS build).
# Stage 1 of the plan is exactly where you iterate this until `nvidia-smi` works
# in a hand-booted VM with the GPU passed through.
set -euo pipefail

# ------------------------------------------------------------------- parameters
POOL_DIR="${POOL_DIR:-/var/lib/libvirt/images/husk}"
OUT="${OUT:-${POOL_DIR}/husk-gpu-golden.qcow2}"
BASE_URL="${BASE_URL:-https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2}"
BASE="${BASE:-/tmp/husk-alma10-base.qcow2}"
DISK_SIZE="${DISK_SIZE:-30G}"
CUDA_REPO="${CUDA_REPO:-https://developer.download.nvidia.com/compute/cuda/repos/rhel10/x86_64/cuda-rhel10.x86_64.repo}"

command -v virt-customize >/dev/null || {
  echo "need libguestfs-tools (dnf install -y libguestfs-tools-c guestfs-tools)" >&2
  exit 1
}

mkdir -p "$POOL_DIR"

# ------------------------------------------------------- fetch + size base image
if [[ ! -f "$BASE" ]]; then
  echo "==> downloading Alma 10 GenericCloud base"
  curl -fL "$BASE_URL" -o "$BASE"
fi
echo "==> copying base → $OUT and resizing to $DISK_SIZE"
qemu-img convert -O qcow2 "$BASE" "$OUT"
qemu-img resize "$OUT" "$DISK_SIZE"

# ----------------------------------------- nvidia-container-toolkit repo (CDI src)
NVCT_REPO='/tmp/nvidia-container-toolkit.repo'
cat >"$NVCT_REPO" <<'EOF'
[nvidia-container-toolkit]
name=nvidia-container-toolkit
baseurl=https://nvidia.github.io/libnvidia-container/stable/rpm/$basearch
enabled=1
gpgcheck=1
gpgkey=https://nvidia.github.io/libnvidia-container/gpgkey
EOF

# ----------------------------------------- the CDI-on-boot oneshot (golden-baked)
CDI_SERVICE='/tmp/husk-cdi.service'
cat >"$CDI_SERVICE" <<'EOF'
[Unit]
Description=Generate NVIDIA CDI spec (needs the driver + GPU at runtime)
After=multi-user.target
ConditionPathExists=/usr/bin/nvidia-ctk

[Service]
Type=oneshot
ExecStart=/usr/bin/nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# ----------------------------------------------------------- customize the image
echo "==> customizing image (driver + toolkit + podman + CDI oneshot)"
virt-customize -a "$OUT" \
  --run-command 'dnf -y install dnf-plugins-core' \
  --run-command "dnf config-manager --add-repo ${CUDA_REPO}" \
  --copy-in "${NVCT_REPO}:/etc/yum.repos.d/" \
  --run-command 'dnf -y module enable nvidia-driver || true' \
  --run-command 'dnf -y install kernel-devel kernel-headers dkms || true' \
  --run-command 'dnf -y install nvidia-driver nvidia-driver-cuda || dnf -y install nvidia-open || true' \
  --run-command 'dnf -y install nvidia-container-toolkit' \
  --install 'podman,podman-docker,fuse-overlayfs,slirp4netns,netavark,aardvark-dns' \
  --write '/etc/containers/containers.conf:[containers]\nlabel = false\n' \
  --write '/etc/containers/nodocker:' \
  --copy-in "${CDI_SERVICE}:/etc/systemd/system/" \
  --run-command 'systemctl enable husk-cdi.service' \
  --run-command 'systemctl enable nvidia-persistenced.service || true' \
  --run-command "echo 'exclude=kernel*' >> /etc/dnf/dnf.conf" \
  --run-command 'dracut -f --regenerate-all || true' \
  --selinux-relabel

echo "==> done: $OUT"
echo "Next: hand-boot a VM with the GPU <hostdev> and confirm:"
echo "  nvidia-smi   AND   podman run --rm --device nvidia.com/gpu=all <cuda-img> nvidia-smi"
