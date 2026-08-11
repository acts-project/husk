# libvirt VM-host setup (runbook)

One-time prep to turn a GPU (or CPU-only) box into a husk libvirt VM-host. This is
the **authoritative, live-validated** sequence. Two hosts back it:

| host | OS | validated |
|---|---|---|
| `lenovo-gpu-acts` | Fedora 42, kernel 6.17, NVIDIA RTX 500 Ada | steps 1–6 incl. **GPU passthrough** |
| `acts-gpu-ci-1` | Ubuntu, libvirt 8.0.0, monolithic `libvirtd` | steps 1–4 + image staging + slot lifecycle; **no GPU yet** |

Between them every step is covered, but **no single host has run the whole thing**
— the Fedora box predates the `husk` service account (step 1b), and the Ubuntu box
has not done vfio (step 5). Both gaps are called out where they occur.

It is written to be mechanically translatable into an **Ansible playbook** — each
step notes whether it needs root and is idempotent. See the project memory
`deferred-ansible-host-provisioning` for the agreed scope (steps 1–4 only).

> The VFIO/IOMMU groundwork (GPU isolated in its own IOMMU group, bound to
> `vfio-pci`) was validated separately in `gpu-passthrough-poc-findings.md`.

**Steps 1–6 below are written for Fedora.** For an Ubuntu or Debian host, work
through the same numbered steps but apply the deltas in
[Ubuntu / Debian hosts](#ubuntu--debian-hosts). For an EL9-family host (RHEL/CentOS
Stream/Rocky/Alma), the packaging is close to Fedora's but **not** identical — see
the callout under step 1.

## Host facts this assumes

- **Modular libvirt daemons** (Fedora ≥ 35 / RHEL 9+): the active daemon is
  `virtqemud` (+ `virtnetworkd`, `virtstoraged`, `virtnodedevd`), *not* the
  monolithic `libvirtd`. Config lives in `/etc/libvirt/virtqemud.conf` etc.
- The system socket `/run/libvirt/virtqemud-sock` is **world-writable**
  (`srw-rw-rw-`) by default; access is gated by **polkit**, not by socket group
  ownership. (A `libvirt` group may exist but is *not* the access lever here.)
- huskd runs on a **control machine** (a laptop, or the k8s pod) and reaches the
  host over `qemu+ssh://husk@HOST/system`. The **guest VMs are never SSHed** — only
  the host is, for the libvirt API plus `qemu-img`/`mkisofs` disk+seed prep.
- It logs in as a **dedicated `husk` service account** (step 1b), not a human's
  login. Every command below that needs a username uses `husk`.

## 1. Packages (root)

```bash
sudo dnf install -y qemu-kvm libvirt virt-install guestfs-tools \
                    mkisofs            # or: genisoimage (the backend uses either)
sudo systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket
```

`guestfs-tools` provides `virt-customize` (used to build the golden image in
`build-golden-image.sh`). The backend's seed-ISO step auto-selects whichever of
`genisoimage`/`mkisofs` is present.

> **EL9-family diverges on the emulator binary path (confirmed on AlmaLinux 9)** —
> nothing to do on the host, huskd handles it. Fedora's `qemu-kvm` install pulls in
> `qemu-system-x86`, which puts the binary at `/usr/bin/qemu-system-x86_64`.
> RHEL/CentOS Stream/Rocky/Alma's `qemu-kvm` ships no `/usr/bin` entry at all
> (`rpm -ql qemu-kvm-core` is empty for `bin`); the binary lives only at
> `/usr/libexec/qemu-kvm`. `LibvirtBackend` probes each host once for a known
> binary path (`_EMULATOR_CANDIDATES` in `libvirt_backend.py`) and writes whichever
> one exists into the domain XML, so this is transparent as of that change. If you
> see `libvirt.libvirtError: Cannot check QEMU binary ... No such file or
> directory` on an older huskd, that's this issue — upgrade rather than
> symlinking, so the fix travels with the code instead of being a manual step per
> host.

## 1b. The `husk` service account (root)

huskd logs in as its own unprivileged account — never a human's login. That keeps
the credential rotatable without touching anyone's shell access, and keeps the
audit trail on the host readable.

> **Not yet re-verified on Fedora.** The `lenovo-gpu-acts` validation predates this
> step — it ran as a personal login. Steps 1 and 3–6 are unaffected, but this step
> and the step-2 rule now naming `husk` have only been exercised on Ubuntu.

```bash
sudo useradd -m -s /bin/bash husk
sudo mkdir -p /home/husk/.ssh && sudo chmod 0700 /home/husk/.ssh
sudo tee /home/husk/.ssh/authorized_keys >/dev/null < /path/to/id_ed25519.pub
sudo chmod 0600 /home/husk/.ssh/authorized_keys
sudo chown -R husk:husk /home/husk/.ssh
```

**No password is set, deliberately** — `useradd` without `-p` leaves `!` in
`/etc/shadow`, which disables password authentication while leaving public-key auth
working. Don't "fix" this by assigning one. Equally deliberately, `husk` gets **no
sudo**: nothing huskd runs on the host needs root. It needs exactly three things,
all granted below — libvirt RW (step 2), write access to the pool dir (step 3), and
`qemu-img`/`genisoimage`/`curl` on `PATH` (step 1).

A real shell is required (not `/usr/sbin/nologin`): the backend runs commands over
SSH, so the account must be able to execute them.

> The matching **private** key is the one huskd holds — `secrets/id_ed25519` on the
> control machine, or the `huskd-ssh` Secret in k8s (`k8s/README.md`). Generate it
> there, copy only the `.pub` here.

## 2. Read-write libvirt access for the `husk` user via polkit (root) — **key step**

Read-only libvirt access works for any local user, but **read-write**
(`org.libvirt.unix.manage`) is denied: Fedora's stock polkit rule only auto-grants
it to an *active local login session*, and a headless SSH connection has **no
polkit agent** to authenticate against. Symptom:

```
error: authentication unavailable: no polkit agent available to authenticate
       action 'org.libvirt.unix.manage'
```

Fix: a polkit JS rule granting the `husk` account from step 1b. polkitd auto-reloads
`rules.d`, so **no restart or re-login is needed** (a `systemctl restart polkit`
forces it if in doubt):

```bash
sudo tee /etc/polkit-1/rules.d/50-husk-libvirt.rules >/dev/null <<'RULE'
polkit.addRule(function(action, subject) {
    if (action.id == "org.libvirt.unix.manage" &&
        subject.user == "husk") {
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
made writable by the `husk` user** — the backend runs `qemu-img`/`mkisofs` as that
user over SSH, and `pool-build` creates the dir `root:root 0711` (not writable).

Define + build + autostart the pool (host or remote):

```bash
virsh -c qemu:///system pool-define-as husk dir --target /var/lib/libvirt/images/husk
virsh -c qemu:///system pool-build husk
virsh -c qemu:///system pool-start husk
virsh -c qemu:///system pool-autostart husk
```

Then make the dir writable by the `husk` user (root, on the host):

```bash
sudo chown husk:husk /var/lib/libvirt/images/husk
sudo chmod 0755 /var/lib/libvirt/images/husk
```

`0755` lets the `husk` user create overlays/seeds while qemu (running as user `qemu`)
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

## 6. Golden image — **no longer a host-setup step**

**Do not build an image on the host.** huskd delivers goldens itself: set
`image_ref` on the pool (e.g. `ghcr.io/acts-project/husk-base:v8`) and it pulls
that OCI artifact once into a controller-local cache keyed by the qcow2's layer
digest (`image_sync.py`), then `scp`s it into every host's pool dir under a
digest-derived name and GCs superseded ones (`libvirt_backend.sync_images`,
`_gc_goldens`). **Hosts need no registry client and no credentials.** Changing
`image_ref` and restarting huskd stages the new image and drains idle slots onto
it; running jobs finish first.

`scripts/build-golden-image.sh` survives for building images by hand, and
`image_name` still names a qcow2 you placed yourself — but neither is part of
standing up a host, and on a **non-SELinux host the local build is actively
wrong** (see the Ubuntu step 6 note). CI builds both variants:
`.github/workflows/build-images.yml` → `image-pipeline.md`.

What you *do* need before the first boot test: **one qcow2 in the pool dir** for
`scripts/smoke_libvirt.py`, which takes a plain filename (`HUSK_SMOKE_IMAGE`), not
an `image_ref`. A stock AlmaLinux 10 GenericCloud image works as the backing file —
no golden required for the CPU path.

### GPU note (Stage 1, not host setup)

CDI is generated at **first boot**, not in the image (the driver must load against
a present GPU). Validate by hand-booting a throwaway VM with the GPU `<hostdev>`
before pointing huskd at it:

```
nvidia-smi                                                   # in the guest
podman run --rm --device nvidia.com/gpu=all <cuda-img> nvidia-smi
```

### Debugging a guest's boot / cloud-init

The guest is never SSHed, so to watch a slot's boot + cloud-init, attach to its
serial console from the control machine (Ctrl-] to detach):

```bash
virsh -c qemu+ssh://husk@HOST/system console <domain-name>
```

> A file-backed serial log (`domain_xml` supports `console_log_path`) is **not**
> enabled by default: under SELinux the `qemu` user must own/relabel the log file
> in the pool dir, which fails while the pool dir is owned by the `husk` user. Enabling
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
`qemu+ssh://husk@HOST/system` URI and the disk/seed SSH-exec share one alias. Confirm
both channels:

```bash
ssh husk@HOST true                                              # key works
virsh -c qemu+ssh://husk@HOST/system list                      # libvirt RW over ssh
```

### If huskd runs in Kubernetes instead

Same key from step 1b, delivered as a Secret rather than `~/.ssh/config`:

```bash
ssh-keygen -t ed25519 -N '' -f secrets/id_ed25519 -C huskd
# install secrets/id_ed25519.pub on the host's husk account (step 1b above —
# NOT ssh-copy-id, that account has no password to auth with)
ssh-keyscan -t ed25519 HOST > secrets/known_hosts
just k8s-secrets                     # creates/rotates the huskd-ssh Secret
```

`k8s-secrets` bundles `secrets/id_ed25519` + `secrets/known_hosts` into the
**`huskd-ssh`** Secret, mounted at `/app/.ssh` (`HOME=/app` in the image) — no
`IdentityFile` needed since `id_ed25519` is ssh's default identity filename.
`known_hosts` is mandatory (everything runs `BatchMode=yes`, so an unknown host
key fails hard with no prompt); the `ssh` volume is `optional: true`, so an
OpenStack-only deployment can skip this entirely. The host sees the **worker
node's** IP, not the pod's (egress is SNAT'd) — that's what a firewall rule or
`authorized_keys from=` restriction has to name. Full detail (permission bits,
why `0440` not `0600`, the three things in the pod that shell out to `ssh`):
`k8s/README.md` → "SSH to libvirt hosts".

## Verification checklist (Stage 0 "done")

Run these **from the control machine, over SSH** — not as `sudo virsh` on the host.
Every failure mode steps 1–4 can leave behind (polkit/group, pool-dir ownership)
appears only on a *headless remote* connection; a local root shell proves nothing.

```bash
# from the control machine:
ssh husk@HOST id -nG                                           # group took effect
virsh -c qemu+ssh://husk@HOST/system list --all                # RW, no polkit error
virsh -c qemu+ssh://husk@HOST/system pool-info husk            # active, autostart
virsh -c qemu+ssh://husk@HOST/system net-info default          # active, autostart
ssh husk@HOST 'touch /var/lib/libvirt/images/husk/.w && rm /var/lib/libvirt/images/husk/.w && echo writable'
ssh husk@HOST 'command -v qemu-img genisoimage curl'           # what the backend shells out to
```

> If the private key lives outside `~/.ssh` (e.g. this repo's `secrets/id_ed25519`),
> plain `ssh husk@HOST` will not find it and the first `virsh -c qemu+ssh://` fails
> as an auth error that looks like a polkit or group problem. Give it an
> `~/.ssh/config` alias with `IdentityFile` + `IdentitiesOnly yes` before running
> any of the above — libvirt's ssh transport reads the same config.

## Ubuntu / Debian hosts

> **Status: live-validated** on `acts-gpu-ci-1` (Ubuntu, libvirt 8.0.0, monolithic
> `libvirtd`) — steps 1–4 by hand, then `scripts/smoke_libvirt.py` green end to end:
> golden staged from `ghcr.io/acts-project/husk-base:v8` through the real delivery
> path (oras pull → controller cache → scp), slot created, metadata round-tripped,
> domain stayed RUNNING, destroyed and cleaned up. **Step 5 (vfio/GPU) is still
> untested here** — that box's GPU is in use by GitLab CI and the cutover is
> deliberately deferred.

Nothing in huskd itself is distro-aware. The backend only needs `qemu-img`,
`genisoimage` **or** `mkisofs`, and `rm` to be on `PATH` for the `husk` user
(`libvirt_backend.py:293`), plus a RW libvirt connection. The deltas are all in
*how the distro gates those*.

### Step 0 (Ubuntu only): can the host terminate `qemu+ssh://`?

The one check that can hard-fail your first remote connect. libvirt's ssh
transport runs a helper **on the host** to reach the local socket: `virt-ssh-helper`
since **libvirt 6.9**, and plain `nc -U` before that. On an older release with
neither installed, `virsh -c qemu+ssh://…` fails in a way that reads like an
authentication problem, which sends you debugging step 2 for no reason.

```bash
libvirtd --version
command -v virt-ssh-helper nc     # need at least one; below 6.9 it must be nc
```

< 6.9 and no `nc` ⇒ add `netcat-openbsd` to step 1.

Nothing else about the host's age matters. huskd names no daemon, socket path or
version (there is no `getLibVersion` check in the backend), and the domain XML it
generates is deliberately plain — BIOS boot with no `<loader>`/`<nvram>` (so OVMF
is never needed), `q35`, virtio disk/net/balloon, a SATA cdrom for the seed. Its
newest requirement is `<cpu check='none'>`, libvirt **3.2** (2017).

> **Confirmed on the first Ubuntu host:** libvirt **8.0.0**, **monolithic**
> `libvirtd`, and `virt-ssh-helper` **present**. Step 0 passes there — no
> `netcat-openbsd`, no version-driven deviation.

### Step 1 — packages

```bash
sudo apt install -y qemu-system-x86 libvirt-daemon-system libvirt-clients \
                    virtinst guestfs-tools genisoimage
#                   + netcat-openbsd   # ONLY if step 0 found libvirt < 6.9
```

`libvirt-daemon-system` installs *and enables* the socket units and pulls
`dnsmasq-base` for the `default` network, so the Fedora step's explicit
`systemctl enable --now` is redundant here — which is also why the monolithic-vs-
modular question mostly does not arise on Ubuntu. It surfaces only when you name a
unit or a config file by hand: monolithic ⇒ `libvirtd.socket` +
`/etc/libvirt/libvirtd.conf`, modular ⇒ `virtqemud.socket` +
`/etc/libvirt/virtqemud.conf` (the Fedora text). `systemctl list-unit-files
'virtqemud*' 'libvirtd*'` settles it if you need to know.

`genisoimage` is the Debian name for the tool Fedora calls `mkisofs`; the backend
accepts either. The emulator path is **hardcoded** to `/usr/bin/qemu-system-x86_64`
(`libvirt_xml.py:31`, not config-overridable) — which is exactly where
`qemu-system-x86` puts it, so there is nothing to do, but a host that keeps qemu
elsewhere would need a code change rather than a config one.

### Step 2 — read-write access: **group, not polkit** (confirmed)

This is the single biggest divergence, and the mechanism is now measured rather
than assumed. On `acts-gpu-ci-1` (Ubuntu, libvirt 8.0.0):

```
/etc/libvirt/libvirtd.conf:179:  auth_unix_rw = "none"
/usr/share/polkit-1/rules.d/60-libvirt.rules:
    action.id == "org.libvirt.unix.manage" && subject.isInGroup("libvirt") -> YES
```

`auth_unix_rw = "none"` means libvirtd **does not consult polkit at all** for the
unix socket — whoever can open it gets read-write. Access is therefore gated purely
by the socket's group, so the shipped `60-libvirt.rules` is inert on this host. It
is a useful backstop rather than dead weight: it grants by *group membership* with
`Result.YES`, which needs no agent, so it would still work headless if someone set
`auth_unix_rw = "polkit"`. Either way, one thing does it:

```bash
sudo usermod -aG libvirt husk
```

**Do not add the step-2 polkit rule on Debian-family** — it is redundant under
both settings.

`unix_sock_group` is *commented out* in `libvirtd.conf`; the socket's group comes
from the systemd socket unit instead. Worth reading rather than assuming if RW
fails:

```bash
ls -l /run/libvirt/libvirt-sock
systemctl cat libvirtd.socket | grep -iE 'SocketMode|SocketGroup'
```

A **new** SSH session picks the group up — no reboot, but existing connections
(and any `ControlMaster` mux socket) must be dropped. Confirm from the control
machine:

```bash
ssh husk@HOST id -nG          # must list: libvirt
```

> **Security consequence of `auth_unix_rw = "none"`:** the `libvirt` group *is* the
> privilege boundary — membership means full control of every VM on the host, with
> no further authentication. Fine for `husk` on a dedicated box; a reason not to add
> human logins to that group casually. Note this is precisely the setting the Fedora
> step 2 fallback offers as a coarse workaround, cautioning it suits only a
> single-tenant host. On Debian-family it is simply the default.

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
**Nothing had to be done for it** — a slot booted on `acts-gpu-ci-1` with no profile
edits at all: `virt-aa-helper` builds a per-domain profile from the domain XML and
grants each disk path, including the **backing chain** (the overlay's golden) and
the seed ISO, wherever they live.

This was the section's biggest open question and the answer is "not a problem." It
is worth being clear *why*, since the earlier framing here tied the risk to the pool
dir being owned by `husk`: AppArmor is **path**-based, so DAC ownership is
irrelevant to it, and the paths are granted per domain rather than by a static rule
over the pool dir.

If a domain ever does fail to start with a permission error, `dmesg | grep DENIED`
is the first stop and `/etc/apparmor.d/libvirt/libvirt-<uuid>.files` is the decisive
one — it lists exactly what `virt-aa-helper` granted. The fix would be a line in
`/etc/apparmor.d/local/abstractions/libvirt-qemu` plus `systemctl reload apparmor`.

The deferred file-backed serial console (`console_log_path`) is a separate matter
and remains untested here: it is blocked on Fedora by SELinux relabeling, and would
be AppArmor's business on Ubuntu.

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
| 0 transport | n/a (`virt-ssh-helper` present) | check libvirt ≥ 6.9, else `netcat-openbsd` |
| 1 packages | `dnf`, `virtqemud.socket` | `apt`, `libvirt-daemon-system` (auto-enables), `genisoimage` |
| 2 RW access | **polkit rule** (socket world-writable) | **`libvirt` group** (`auth_unix_rw = "none"`; do NOT add the polkit rule) |
| 3 pool | qemu runs as `qemu:qemu` | qemu runs as `libvirt-qemu:kvm` |
| 4 network | `default` NAT | same; watch for a system `dnsmasq` on `:53` |
| 5 vfio | kernel cmdline + `dracut` | `/etc/default/grub` + `update-grub` + `update-initramfs` |
| — MAC | SELinux (relabel, `.autorelabel`) | AppArmor — **nothing to do**; `virt-aa-helper` grants per domain |
| 6 image | builds cleanly | SELinux relabel unreliable; 0600 kernel — prefer the CI image |

## Automation notes (for the Ansible playbook)

Map of steps → tasks, with the root/idempotency notes that matter for automation:

| Step | Ansible-ish | Root | Idempotent | Gotcha |
|---|---|---|---|---|
| 1 packages | `ansible.builtin.package`, `systemd_service` | yes | yes | Fedora: enable the **modular** `.socket` units. Debian: `libvirt-daemon-system` self-enables. `python3-libvirt` is needed for the `virt_*` modules below |
| 1b account | `ansible.builtin.user` + `ansible.posix.authorized_key` | yes | yes | omit `password` (leaves `!` = no password auth); a real shell, no sudo; `authorized_key` is idempotent by key content |
| 2 RW access | Fedora: `copy` of `50-husk-libvirt.rules`. Debian: `groups: libvirt` on the user | yes | yes | **the lever differs by family** — polkit on Fedora, socket group on Debian (`auth_unix_rw = "none"`). Do NOT ship the polkit rule to Debian |
| 3 pool | `community.libvirt.virt_pool` + `file` (owner) | yes | yes | **chown the target dir to the `husk` user** after build |
| 4 network | `community.libvirt.virt_net` | yes | yes | start + autostart the built-in `default` |
| 5 vfio | kernel cmdline + `modprobe.d` + dracut | yes | needs reboot | out of scope of the libvirt role; pairs with host provisioning |
| 6 golden image | **nothing — out of scope** | — | — | huskd syncs goldens itself from `image_ref`; hosts need no registry access |
| 7 control machine | not host-side | n/a | — | `PKG_CONFIG_PATH` for `libvirt-python` on macOS |

`guestfs-tools` is only needed if you build an image on the host by hand — the
standing path never does, so a role may reasonably drop it from step 1.

The SSH user (`husk` here), pool path, and `gpu_pci_addresses` are the obvious role variables.

Steps 1, 2 and 5 need `ansible_os_family` branches (package names + unit,
group-vs-polkit, `update-initramfs`-vs-`dracut`); 1b, 3, 4 and 7 are portable as-is.
The playbook connects as a **privileged admin login, never as `husk`** — that
account has no password and no sudo, so it is the playbook's output, not its
credential. See
[Ubuntu / Debian hosts](#ubuntu--debian-hosts).
