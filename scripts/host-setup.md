# libvirt VM-host setup (runbook)

One-time prep to turn a GPU (or CPU-only) box into a husk libvirt VM-host. This is
the **authoritative, live-validated** sequence (validated on `lenovo-gpu-acts`,
Fedora 42, kernel 6.17, NVIDIA RTX 500 Ada). It is written to be mechanically
translatable into an **Ansible role / Puppet module later** — each step notes
whether it needs root and is idempotent. Automating it is **deferred** (see the
project memory `deferred-ansible-host-provisioning`); do it by hand for now.

> The VFIO/IOMMU groundwork (GPU isolated in its own IOMMU group, bound to
> `vfio-pci`) was validated separately in `gpu-passthrough-poc-findings.md`.

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
