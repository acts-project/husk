# libvirt VM-host setup (runbook)

One-time prep to turn a GPU (or CPU-only) box into a husk libvirt VM-host. This is
the **authoritative, live-validated** sequence (validated on `lenovo-gpu-acts`,
Fedora 42, kernel 6.17, NVIDIA RTX 500 Ada). It is written to be mechanically
translatable into an **Ansible role / Puppet module later** — each step notes
whether it needs root and is idempotent. Automating it is **deferred** (see the
project memory `deferred-ansible-host-provisioning`); do it by hand for now.

> The VFIO/IOMMU groundwork (GPU isolated in its own IOMMU group, bound to
> `vfio-pci`) was validated separately in `gpu-passthrough-poc-findings.md`.

**Steps 1–6 below are Fedora/EL-specific.** For an Ubuntu or Debian host, work
through the same numbered steps but apply the deltas in
[Ubuntu / Debian hosts](#ubuntu--debian-hosts-untested) — that path has **not**
been validated on real hardware.

## Host facts this assumes

- **Modular libvirt daemons** (Fedora ≥ 35 / RHEL 9+): the active daemon is
  `virtqemud` (+ `virtnetworkd`, `virtstoraged`, `virtnodedevd`), *not* the
  monolithic `libvirtd`. Config lives in `/etc/libvirt/virtqemud.conf` etc.
- The system socket `/run/libvirt/virtqemud-sock` is **world-writable**
  (`srw-rw-rw-`) by default; access is gated by **polkit**, not by socket group
  ownership. (A `libvirt` group may exist but is *not* the access lever here.)
- huskd runs on a **control machine** (e.g. a Mac) and reaches the host over
  `qemu+ssh://USER@HOST/system`. The **guest VMs are never SSHed** — only the host
  is, for the libvirt API plus `qemu-img`/`mkisofs` disk+seed prep.

## 1. Packages (root)

```bash
sudo dnf install -y qemu-kvm libvirt virt-install guestfs-tools \
                    mkisofs            # or: genisoimage (the backend uses either)
sudo systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket
```

`guestfs-tools` provides `virt-customize` (used to build the golden image in
`build-golden-image.sh`). The backend's seed-ISO step auto-selects whichever of
`genisoimage`/`mkisofs` is present.

## 2. Read-write libvirt access for the SSH user via polkit (root) — **key step**

Read-only libvirt access works for any local user, but **read-write**
(`org.libvirt.unix.manage`) is denied: Fedora's stock polkit rule only auto-grants
it to an *active local login session*, and a headless SSH connection has **no
polkit agent** to authenticate against. Symptom:

```
error: authentication unavailable: no polkit agent available to authenticate
       action 'org.libvirt.unix.manage'
```

Fix: a polkit JS rule granting the husk SSH user. polkitd auto-reloads
`rules.d`, so **no restart or re-login is needed** (a `systemctl restart polkit`
forces it if in doubt). Replace `pagessin` with the actual SSH user:

```bash
sudo tee /etc/polkit-1/rules.d/50-husk-libvirt.rules >/dev/null <<'RULE'
polkit.addRule(function(action, subject) {
    if (action.id == "org.libvirt.unix.manage" &&
        subject.user == "pagessin") {
        return polkit.Result.YES;
    }
});
RULE
```

Verify locally on the host: `virsh -c qemu:///system list --all` must succeed with
no polkit error.

> Fallback if polkit rules aren't honored: since the socket is already
> world-writable, set `auth_unix_rw = "none"` in `/etc/libvirt/virtqemud.conf` and
> `sudo systemctl restart virtqemud.service virtqemud.socket`. Coarser (any local
> user gets RW); acceptable only on a dedicated single-tenant box.

## 3. Storage pool `husk` (can be done remotely once step 2 works)

The backend drops per-slot overlay qcow2s + NoCloud seed ISOs here, and the golden
image lives here too. It can be created over `qemu+ssh` from the control machine
(libvirtd runs as root, so it builds the dir), **but the target dir must then be
made writable by the SSH user** — the backend runs `qemu-img`/`mkisofs` as that
user over SSH, and `pool-build` creates the dir `root:root 0711` (not writable).

Define + build + autostart the pool (host or remote):

```bash
virsh -c qemu:///system pool-define-as husk dir --target /var/lib/libvirt/images/husk
virsh -c qemu:///system pool-build husk
virsh -c qemu:///system pool-start husk
virsh -c qemu:///system pool-autostart husk
```

Then make the dir writable by the SSH user (root, on the host):

```bash
sudo chown pagessin:pagessin /var/lib/libvirt/images/husk
sudo chmod 0755 /var/lib/libvirt/images/husk
```

`0755` lets the SSH user create overlays/seeds while qemu (running as user `qemu`)
can still traverse and read; libvirt's dynamic DAC ownership chowns each disk to
`qemu` at domain start and back on stop.

## 4. Network `default` (NAT)

The guest needs only outbound (to GitHub); libvirt's built-in `default` NAT
network suffices. The control machine never connects *to* the guest.

```bash
virsh -c qemu:///system net-start default 2>/dev/null || true
virsh -c qemu:///system net-autostart default
```

## 5. GPU → vfio-pci (root; already validated — see findings)

For a GPU host, confirm the GPU is isolated in its own IOMMU group and bound to
`vfio-pci` (kernel cmdline `vfio-pci.ids=10de:XXXX`, `nouveau` blacklisted). Record
its PCI address for `gpu_pci_addresses` in the huskd config.

```bash
lspci -nnk -d 10de:    # want: "Kernel driver in use: vfio-pci"
# validated: 0000:01:00.0  NVIDIA AD107GLM [RTX 500 Ada]  [10de:28ba] -> vfio-pci
```

A **CPU-only host** skips this entirely and declares `max_slots` instead of
`gpu_pci_addresses`.

## 6. Golden image (GPU hosts)

Build it on the host (needs `guestfs-tools` from step 1):

```bash
scripts/build-golden-image.sh        # → /var/lib/libvirt/images/husk/husk-gpu-golden.qcow2
```

CDI is generated at **first boot**, not in the image (the driver must load against
a present GPU). Validate by hand-booting a throwaway VM with the GPU `<hostdev>`
before pointing huskd at it (plan Stage 1):

```
nvidia-smi                                                   # in the guest
podman run --rm --device nvidia.com/gpu=all <cuda-img> nvidia-smi
```

For a quick **CPU-path** smoke test you don't need the golden image — a stock
AlmaLinux 10 GenericCloud qcow2 in the pool works as the backing image.

### Debugging a guest's boot / cloud-init

The guest is never SSHed, so to watch a slot's boot + cloud-init, attach to its
serial console from the control machine (Ctrl-] to detach):

```bash
virsh -c qemu+ssh://USER@HOST/system console <domain-name>
```

> A file-backed serial log (`domain_xml` supports `console_log_path`) is **not**
> enabled by default: under SELinux the `qemu` user must own/relabel the log file
> in the pool dir, which fails while the pool dir is owned by the SSH user. Enabling
> it is deferred to host setup (root-owned pool dir + a `qemu`-writable console dir,
> or a relabel rule) — a natural Ansible concern.

## 7. Control machine (where huskd runs)

huskd needs `libvirt-python`, which builds against the libvirt client libs:

```bash
# macOS:
brew install libvirt pkg-config
export PKG_CONFIG_PATH="$(brew --prefix libvirt)/lib/pkgconfig:$PKG_CONFIG_PATH"
uv sync --extra libvirt --extra dev          # build/import libvirt-python

# Linux: install libvirt-devel / libvirt-dev, then `uv sync --extra libvirt`
```

Add the host to `~/.ssh/config` (key-based, `BatchMode`-friendly) so the
`qemu+ssh://HOST/system` URI and the disk/seed SSH-exec share one alias. Confirm
both channels:

```bash
ssh HOST true                                              # key works
virsh -c qemu+ssh://HOST/system list                      # libvirt RW over ssh
```

## Verification checklist (Stage 0 "done")

```bash
# from the control machine:
virsh -c qemu+ssh://HOST/system list --all                # RW, no polkit error
virsh -c qemu+ssh://HOST/system pool-info husk            # active, autostart
virsh -c qemu+ssh://HOST/system net-info default          # active, autostart
ssh HOST 'touch /var/lib/libvirt/images/husk/.w && rm /var/lib/libvirt/images/husk/.w && echo writable'
```

## Ubuntu / Debian hosts (untested)

> **Status: not validated.** Every host husk has run on is Fedora. This section is
> derived from the Fedora runbook plus known Debian-family differences; treat each
> step as a hypothesis to confirm, and fold corrections back in once a real Ubuntu
> host is stood up. The **verification checklist above is the actual acceptance
> test** and is distro-independent — if all four commands pass, the host is good
> regardless of how you got there.

Nothing in huskd itself is distro-aware. The backend only needs `qemu-img`,
`genisoimage` **or** `mkisofs`, and `rm` to be on `PATH` for the SSH user
(`libvirt_backend.py:293`), plus a RW libvirt connection. The deltas are all in
*how the distro gates those*.

### Step 0 (Ubuntu only): which libvirt daemon model?

The Fedora runbook assumes **modular** daemons (`virtqemud`). Debian-family
releases have been slower to switch, so detect rather than assume — it decides
which unit you enable in step 1 and which config file the step-2 fallback edits:

```bash
systemctl list-unit-files 'virtqemud*' 'libvirtd*'   # whichever exists is your model
virsh --version
```

Monolithic ⇒ `libvirtd.socket` + `/etc/libvirt/libvirtd.conf`.
Modular ⇒ `virtqemud.socket` + `/etc/libvirt/virtqemud.conf`, i.e. exactly the
Fedora text.

### Step 1 — packages

```bash
sudo apt install -y qemu-system-x86 libvirt-daemon-system libvirt-clients \
                    virtinst guestfs-tools genisoimage
```

`libvirt-daemon-system` installs *and enables* the socket units and pulls
`dnsmasq-base` for the `default` network, so the explicit `systemctl enable --now`
of step 1 is usually redundant (harmless to run against whichever unit step 0
found). `genisoimage` is the Debian name for the tool Fedora calls `mkisofs`; the
backend accepts either.

### Step 2 — read-write access: **group, not polkit**

This is the single biggest divergence. On Fedora the socket is world-writable and
polkit is the lever; on Debian-family the socket is `0770 root:libvirt`
(`unix_sock_group = "libvirt"`), so **group membership is the lever** and the
Fedora "no polkit agent available" symptom typically never appears:

```bash
sudo usermod -aG libvirt pagessin
```

A **new** SSH session picks the group up — no reboot, but existing connections
(and any `ControlMaster` mux socket) must be dropped. Confirm from the control
machine:

```bash
ssh HOST id -nG          # must list: libvirt
```

The polkit rule from step 2 is still valid and harmless to add as a belt-and-braces
measure, but do not treat its absence as the fault when RW fails here — check group
membership and the socket's mode/ownership first (`ls -l /run/libvirt/*-sock`).

### Steps 3–4 — pool and network

The `virsh` commands are identical. Two Debian-family notes:

- qemu runs as **`libvirt-qemu:kvm`**, not `qemu:qemu`. The recommended
  `chown SSHUSER + chmod 0755` on the pool dir still works (world-traversable), but
  if you tighten it to `0750`, the group must be one `libvirt-qemu` is in.
- If a **system `dnsmasq`** is installed and bound to `0.0.0.0:53`, libvirt's
  `default` network fails to start with a bind error. Either don't install it, or
  bind it to specific interfaces. `dnsmasq-base` alone (what libvirt pulls) does not
  cause this.

### Step 5 — VFIO/IOMMU: GRUB + initramfs-tools, not dracut

Same end state as `gpu-passthrough-poc-findings.md` (GPU alone in its IOMMU group,
`Kernel driver in use: vfio-pci`), different plumbing:

```bash
# 1. kernel cmdline — append to GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub:
#    intel_iommu=on iommu=pt vfio-pci.ids=10de:28ba
sudo update-grub                     # not grub2-mkconfig

# 2. make vfio-pci win the race for the device
sudo tee /etc/modprobe.d/husk-vfio.conf >/dev/null <<'EOF'
options vfio-pci ids=10de:28ba
blacklist nouveau
blacklist nova-core
EOF
printf 'vfio\nvfio_iommu_type1\nvfio_pci\n' | sudo tee -a /etc/initramfs-tools/modules

# 3. rebuild the initramfs (dracut's job on Fedora) and reboot
sudo update-initramfs -u -k all
sudo reboot
```

Verify exactly as in step 5: `lspci -nnk -d 10de:` must report `vfio-pci`.

The host needs **no NVIDIA driver at all** — it never touches the GPU. The Secure
Boot / MOK signing pain recorded in the findings doc is a *guest* concern (DKMS
modules inside the runner image); `vfio-pci` ships signed with the distro kernel.

### AppArmor replaces SELinux

Ubuntu confines qemu with AppArmor (`security_driver` in `/etc/libvirt/qemu.conf`).
`virt-aa-helper` generates a per-domain profile and adds each disk path from the
domain XML, so overlays and seed ISOs under a non-default pool dir are expected to
work unmodified. If a domain fails to start with a permission error, check
`dmesg | grep DENIED` / `journalctl -b -u apparmor` before suspecting libvirt, and
add the pool dir to `/etc/apparmor.d/local/abstractions/libvirt-qemu`.

The deferred file-backed serial console (`console_log_path`) is blocked here too,
just by AppArmor rather than SELinux relabeling — same workaround shape, different
mechanism.

### Step 6 — building the golden image on an Ubuntu host

Prefer **not** to. `images/build.sh` documents (and `c73b728` fixed) the fallout of
building an EL guest on a non-SELinux Ubuntu host: `virt-customize --selinux-relabel`
can only defer via `/.autorelabel`, and a first boot under *enforcing* then wedges
with every unit at status=127. That is why the CI-built image boots permissive.
Pulling the CI-built image (see `image-pipeline.md`) sidesteps this entirely.

If you do build locally on Ubuntu, you hit the same two libguestfs gotchas as the
`ubuntu-22.04` CI runner (`.github/workflows/build-images.yml:56`):

```bash
sudo chmod 0644 /boot/vmlinuz-*      # Debian ships the kernel 0600; libguestfs can't read it
export LIBGUESTFS_BACKEND=direct
ls -l /dev/kvm                       # absent ⇒ TCG emulation, very slow
```

### Summary of deltas

| Step | Fedora | Ubuntu / Debian |
|---|---|---|
| 1 packages | `dnf`, `virtqemud.socket` | `apt`, `libvirt-daemon-system` (auto-enables), `genisoimage` |
| 2 RW access | **polkit rule** (socket world-writable) | **`libvirt` group** (socket `0770 root:libvirt`) |
| 3 pool | qemu runs as `qemu:qemu` | qemu runs as `libvirt-qemu:kvm` |
| 4 network | `default` NAT | same; watch for a system `dnsmasq` on `:53` |
| 5 vfio | kernel cmdline + `dracut` | `/etc/default/grub` + `update-grub` + `update-initramfs` |
| — MAC | SELinux (relabel, `.autorelabel`) | AppArmor (`virt-aa-helper`, `DENIED` in dmesg) |
| 6 image | builds cleanly | SELinux relabel unreliable; 0600 kernel — prefer the CI image |

## Automation notes (for the future Ansible role / Puppet module)

Map of steps → tasks, with the root/idempotency notes that matter for automation:

| Step | Ansible-ish | Root | Idempotent | Gotcha |
|---|---|---|---|---|
| 1 packages | `dnf`, `systemd` | yes | yes | enable the **modular** `.socket` units, not `libvirtd` |
| 2 polkit rule | `copy`/`template` of `50-husk-libvirt.rules` | yes | yes | this — not group membership — is the RW lever for headless SSH |
| 3 pool | `community.libvirt.virt_pool` + `file` (owner) | yes | yes | **chown the target dir to the SSH user** after build |
| 4 network | `community.libvirt.virt_net` | yes | yes | start + autostart the built-in `default` |
| 5 vfio | kernel cmdline + `modprobe.d` + dracut | yes | needs reboot | out of scope of the libvirt role; pairs with host provisioning |
| 6 golden image | `command: build-golden-image.sh creates=…` | no¹ | via `creates=` | long-running; driver/kernel is the risk (see findings) |
| 7 control machine | not host-side | n/a | — | `PKG_CONFIG_PATH` for `libvirt-python` on macOS |

¹ runs as the SSH user but needs `guestfs-tools` installed (step 1).

The SSH user, pool path, and `gpu_pci_addresses` are the obvious role variables.

If the role ever has to cover both families, steps 1, 2 and 5 are the ones that
need `ansible_os_family` branches (package names + unit, group-vs-polkit,
`update-initramfs`-vs-`dracut`); 3, 4 and 7 are already portable. See
[Ubuntu / Debian hosts](#ubuntu--debian-hosts-untested).
