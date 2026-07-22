"""Cloud-init rendering — the validated Phase 2/3 recipe, lifted verbatim.

The template installs rootless Podman + the Docker→Podman compatibility shim and
runs a single-use JIT runner that powers the slot off when the job finishes
(`husk-poweroff.service`), which the controller observes as SHUTOFF → recycle.
A coarse egress firewall (allow the public internet, deny CERN-internal CIDRs)
is loaded just before the runner starts — see the husk-egress.nft block. It is
applied AFTER provisioning so package/runner installs keep full network (CERN
mirrors included) and only the untrusted job runs locked down.

`@@JIT@@` / `@@RUNNER_URL@@` are substituted per recycle. The runner is pinned to
uid 1000 so `/run/user/1000` lines up with the systemd unit.
"""

from __future__ import annotations

import base64
import re

RUNNER_CLOUD_INIT = r"""#cloud-config
packages:
  - podman
  - podman-docker
  - fuse-overlayfs
  - slirp4netns
  - netavark
  - aardvark-dns
  - libicu
  - sudo            # installdependencies.sh may shell out to it; runner gets NO sudoers entry
  - curl
  - jq
  - git
  - nftables        # coarse egress firewall, loaded in runcmd before the runner

users:
  - name: runner
    uid: 1000
    groups: []
    shell: /bin/bash
    lock_passwd: true

write_files:
  # NB: no `owner:` on any write_files entry — write_files runs BEFORE the
  # users-groups module, so `runner` doesn't exist yet and a chown-by-name
  # would throw and abort the ENTIRE write_files module (silently dropping
  # every later file: the unit, the docker shim, the tmpfiles conf). Ownership
  # is fixed up in runcmd instead (final stage, after the user exists).
  - path: /var/lib/husk/jitconfig
    permissions: '0600'
    content: "@@JIT@@"

  - path: /home/runner/.config/containers/storage.conf
    content: |
      [storage]
      driver = "overlay"
      runroot = "/run/user/1000/containers"
      graphroot = "/home/runner/.local/share/containers/storage"

      [storage.options.overlay]
      mount_program = "/usr/bin/fuse-overlayfs"

  # Silence the podman-docker "Emulate Docker CLI" banner.
  - path: /etc/containers/nodocker
    content: ""

  # Drop per-container SELinux confinement so the runner's un-relabeled
  # ($user_home_t) workspace bind-mounts are readable inside containers.
  # Host stays Enforcing. (Phase 2 finding #6.)
  - path: /etc/containers/containers.conf
    content: |
      [containers]
      label = false

  # Runner hardcodes /var/run/docker.sock; point it at the rootless podman
  # user socket. tmpfiles.d so it survives reboot (/run is tmpfs).
  - path: /etc/tmpfiles.d/husk-docker-sock.conf
    content: |
      L+ /run/docker.sock - - - - /run/user/1000/podman/podman.sock

  # Compatibility shim that REPLACES /usr/bin/docker (ahead of it in PATH).
  # (a) auto-create missing -v bind SOURCE dirs (podman won't; docker does)
  # (b) sanitize $HOME for podman (container actions set HOME=/github/home).
  # Valid shebang on line 1 — the runner execs it via raw execve.
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
      After=network-online.target
      Wants=network-online.target
      # JIT runner is single-use: when run.sh exits (job done) OR fails, recycle
      # the slot by powering off. ExecStopPost can't do it (the service is
      # unprivileged), so trigger a root oneshot. Both On*= so a failed boot
      # also recycles instead of wedging.
      OnSuccess=husk-poweroff.service
      OnFailure=husk-poweroff.service

      [Service]
      Type=simple
      User=runner
      Group=runner
      WorkingDirectory=/opt/actions-runner
      Environment="HOME=/home/runner"
      Environment="XDG_RUNTIME_DIR=/run/user/1000"
      Environment="DOCKER_HOST=unix:///run/user/1000/podman/podman.sock"
      # Wait for the rootless podman socket (brought up by user@1000 via linger +
      # the globally-enabled podman.socket). Poll the socket FILE — no user bus,
      # which is what the old `systemctl --user` line couldn't reach.
      ExecStartPre=/bin/bash -c 'for i in $(seq 1 60); do [ -S /run/user/1000/podman/podman.sock ] && exit 0; sleep 1; done; echo "podman socket never appeared" >&2; exit 1'
      ExecStart=/bin/bash -c '/opt/actions-runner/run.sh --jitconfig $(cat /var/lib/husk/jitconfig)'
      Restart=no

  - path: /etc/systemd/system/husk-poweroff.service
    content: |
      [Unit]
      Description=Power off the Husk slot after the runner exits (recycle trigger)

      [Service]
      Type=oneshot
      ExecStart=/sbin/poweroff

  # Coarse egress firewall: allow the public internet, deny CERN-internal
  # networks (the security property — no lateral access to e.g. landb.cern.ch).
  # `inet` covers v4+v6. Idempotent (drop-then-recreate our own table) so the
  # runcmd re-run on every rebuild reapplies it without disturbing other tables.
  - path: /etc/nftables/husk-egress.nft
    content: |
      #!/usr/sbin/nft -f
      table inet husk {}
      delete table inet husk

      table inet husk {
@@CVMFS_SET@@        chain output {
          type filter hook output priority 0; policy accept;

          oif "lo" accept
          ct state established,related accept

          # Keep name resolution + time sync working: CERN's own resolvers/NTP
          # live inside the blocked ranges, so allow 53/123 to anywhere first.
          udp dport { 53, 123 } accept
          tcp dport 53 accept

@@CVMFS_PROXY@@          # Deny all other egress to CERN-internal networks. Public internet
          # falls through to `policy accept`. Extend these sets as needed.
          ip daddr { 128.141.0.0/16, 128.142.0.0/16, 137.138.0.0/16, 188.184.0.0/15 } drop
          ip6 daddr { 2001:1458::/32, 2001:1459::/32 } drop
        }
@@METRICS_INGRESS@@      }

runcmd:
  # Install the runner as the runner user (runuser, not sudo — sudo isn't on the
  # base image and runuser needs no PAM/sudoers).
  - mkdir -p /opt/actions-runner /var/lib/husk
  # Set ownership here (write_files couldn't — see note above). -R on the home
  # covers .config written by write_files and any root-owned home dir created
  # before users-groups ran; jitconfig must be runner-readable for the service.
  - chown runner:runner /opt/actions-runner
  - chown -R runner:runner /var/lib/husk
  - chown -R runner:runner /home/runner
  - runuser -u runner -- bash -c 'cd /opt/actions-runner && curl -L @@RUNNER_URL@@ | tar xz'
  - /opt/actions-runner/bin/installdependencies.sh

  # Rootless podman socket WITHOUT a user bus: enable it globally (root, no bus),
  # THEN bring up runner's user manager via linger — user@1000 auto-starts the
  # now-enabled podman.socket. Order matters: --global enable must precede the
  # linger start, or the already-running manager won't pick it up.
  - systemctl --global enable podman.socket
  - loginctl enable-linger runner

  # Runner hardcodes /var/run/docker.sock for container jobs (/var/run -> /run);
  # point it at the podman socket. Dangling until the socket appears, which is
  # fine. tmpfiles.d above re-creates it on reboot.
  - ln -sf /run/user/1000/podman/podman.sock /run/docker.sock

  # Lock down egress just before the (untrusted) runner starts. Everything
  # above ran with full network (CERN package mirrors included); the job that
  # the runner executes below runs under the coarse husk egress firewall.
  - /usr/sbin/nft -f /etc/nftables/husk-egress.nft

  # cloud-init runcmd is the SOLE orchestrator each boot (first boot AND every
  # rebuild — Phase 1 proved runcmd re-runs). The unit is NOT enabled for
  # boot-time start, so multi-user.target can't launch it before cloud-init has
  # reinstalled run.sh. `start` (not enable --now); Type=simple returns once
  # ExecStart forks, so the long-running job doesn't block runcmd.
  - systemctl daemon-reload
  - systemctl start husk-runner.service

  # Belt-and-suspenders wall-clock cap (runner is unprivileged -> real net).
  - shutdown -h +360
"""


# GPU enablement splits into two halves that gate differently:
#
#  * _GPU_INSTALL (static) — the driver + container-toolkit packages. On a stock
#    image these must be installed at boot; on a prebaked golden they are already
#    in the image, so this half is SKIPPED. Kept OUT of the `packages:` block on
#    purpose: nvidia-open-kmod lives in the repo that
#    `almalinux-release-nvidia-driver` *enables*, so it needs a second dnf pass
#    the single packages transaction can't express. Runs with the network still
#    fully open (the egress firewall is applied right after).
#
#  * _GPU_RUNTIME (dynamic) — load the precompiled open kmod against the
#    passed-through GPU and (re)generate the CDI spec the rootless runner uses to
#    inject the GPU into job containers. Hardware-dependent (no GPU exists at image
#    build time), so this ALWAYS runs for a GPU pool, prebaked or not. Failures are
#    left LOUD (no `|| true`) so a broken driver surfaces as a failed nvidia-smi in
#    the job rather than a silent no-GPU.
_GPU_INSTALL = r"""  # --- GPU install (stock-image GPU pools; full network here, before the firewall).
  - dnf -y install almalinux-release-nvidia-driver
  # nvidia-driver-cuda ships nvidia-smi AND the CUDA driver libs (libcuda) the CDI
  # hook injects into job containers — without it nvidia-smi is "command not found"
  # on the host and absent from the container.
  - dnf -y install nvidia-open-kmod nvidia-driver nvidia-driver-cuda
  - |
    cat >/etc/yum.repos.d/nvidia-container-toolkit.repo <<'REPO'
    [nvidia-container-toolkit]
    name=nvidia-container-toolkit
    baseurl=https://nvidia.github.io/libnvidia-container/stable/rpm/$basearch
    enabled=1
    gpgcheck=1
    gpgkey=https://nvidia.github.io/libnvidia-container/gpgkey
    REPO
  - dnf -y install nvidia-container-toolkit
"""

_GPU_RUNTIME = r"""  # GPU runtime activation (every boot — the kmod load + CDI spec are
  # hardware-dependent and cannot be baked into the image).
  - modprobe nvidia
  - nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
"""

# Anchor: GPU blocks are spliced in immediately before the egress-firewall
# lockdown, so the install/activation run with the network still fully open.
_FIREWALL_ANCHOR = (
    "  # Lock down egress just before the (untrusted) runner starts. Everything"
)


# Prebaked variant: a golden image (images/build.sh) already carries podman, the
# runner binary + units, the docker shim, and (GPU) the NVIDIA driver + toolkit.
# cloud-init then does ONLY the per-cycle/dynamic work — the JIT config, the
# egress firewall (the one tunable security policy, deliberately not baked), GPU
# runtime activation, and starting the runner. Composes across CPU/GPU/OpenStack:
# the same baked image boots anywhere, with `gpu` toggling runtime activation.
PREBAKED_RUNNER_CLOUD_INIT = r"""#cloud-config
# Prebaked golden image: everything slow/static is in the image already, so this
# is intentionally minimal. See render_cloud_init(prebaked=True).

write_files:
  - path: /var/lib/husk/jitconfig
    permissions: '0600'
    content: "@@JIT@@"
@@CVMFS_WRITE_FILES@@
  # Coarse egress firewall ruleset — the tunable security policy, kept in
  # cloud-init (NOT baked). MUST stay byte-identical to the full template's copy;
  # guarded by tests/test_cloudinit_prebaked.py::test_prebaked_firewall_matches_full.
  - path: /etc/nftables/husk-egress.nft
    content: |
      #!/usr/sbin/nft -f
      table inet husk {}
      delete table inet husk

      table inet husk {
@@CVMFS_SET@@        chain output {
          type filter hook output priority 0; policy accept;

          oif "lo" accept
          ct state established,related accept

          # Keep name resolution + time sync working: CERN's own resolvers/NTP
          # live inside the blocked ranges, so allow 53/123 to anywhere first.
          udp dport { 53, 123 } accept
          tcp dport 53 accept

@@CVMFS_PROXY@@          # Deny all other egress to CERN-internal networks. Public internet
          # falls through to `policy accept`. Extend these sets as needed.
          ip daddr { 128.141.0.0/16, 128.142.0.0/16, 137.138.0.0/16, 188.184.0.0/15 } drop
          ip6 daddr { 2001:1458::/32, 2001:1459::/32 } drop
        }
@@METRICS_INGRESS@@      }

runcmd:
  # jitconfig is written root-owned above; the runner service runs as `runner`.
  - mkdir -p /var/lib/husk
  - chown -R runner:runner /var/lib/husk
  # GPU runtime activation is spliced here for GPU pools (before the firewall).
  # Lock down egress just before the (untrusted) runner starts.
  - /usr/sbin/nft -f /etc/nftables/husk-egress.nft
@@CVMFS_SETUP@@  # The runner unit is baked but NOT enabled for boot; cloud-init starts it each
  # cycle once the fresh JIT config is in place (Type=simple returns immediately).
  - systemctl daemon-reload
@@METRICS_START@@
  - systemctl start husk-runner.service
  # Boot-timing report to the serial console (baked oneshot, ordered After the
  # runner so it never delays registration). Always-on: it only reads timestamps
  # systemd/cloud-init already record. `--no-block` so a slow analyze can't hold
  # up the final stage. `|| true` — diagnostics must never fail the boot.
  - systemctl start --no-block husk-bootreport.service || true
  # Belt-and-suspenders wall-clock cap (runner is unprivileged -> real net).
  - shutdown -h +360
"""

# Splice point for the GPU runtime block in the prebaked template (the line right
# after it is the firewall apply, so activation runs with the network still open).
_PREBAKED_GPU_ANCHOR = (
    "  # Lock down egress just before the (untrusted) runner starts.\n"
)


# Metrics ingress (observability.md). node_exporter is baked into the golden image
# with NO TLS and NO auth, so this source allowlist IS its access control. The
# allowed source differs by backend, which is why the CIDR is a per-pool knob:
#
#   * OpenStack — central Prometheus scrapes the guest directly, so the source is
#     Prometheus itself (if it lives in k8s, that's the *worker-node* subnet: pods
#     egressing out of the cluster are SNAT'd to the node, so the guest never sees
#     a pod IP).
#   * libvirt — Prometheus never touches the guest; it scrapes the host proxy,
#     which opens its own connection over the bridge. The bridge (192.168.122.1)
#     is therefore the only client the guest ever sees.
#
# `policy accept` deliberately leaves the rest of ingress exactly as it was: this
# chain narrows :9100 and nothing else. Replies to an admitted scrape leave via
# the output chain's `ct state established,related accept`, so they are NOT caught
# by the CERN-internal egress drops even when the scraper is itself CERN-internal.
# `@@SADDR@@` is `ip saddr`/`ip6 saddr` per the CIDR's family: inside an `inet`
# table `ip saddr` matches v4 only, so a v6 source under `ip saddr` would never
# match and the drop below would silently close the port.
#
# The `iif "lo" ... drop` hides the exporter from the guest's OWN (untrusted) job:
# a connection to any local address — 127.0.0.1 or the guest's own fixed IP — is
# delivered via the loopback interface, so this drops all in-guest access while the
# external scraper (arriving on the real NIC) is unaffected. It sits BEFORE the
# accept, so it holds even with a wide scrape_cidr, and needs no knowledge of the
# guest's IP. The runner has no business reading host metrics.
_METRICS_INGRESS = """\
        chain input {
          type filter hook input priority 0; policy accept;

          iif "lo" tcp dport 9100 drop
          tcp dport 9100 @@SADDR@@ { @@SCRAPE_CIDR@@ } accept
          tcp dport 9100 drop
        }
"""

# node_exporter is baked but NOT enabled for boot, and cloud-init starts it only
# AFTER the nft ruleset is applied — so :9100 is never briefly open to the world
# during boot. Before the runner, so metrics are live for the whole job.
_METRICS_START = "  - systemctl start husk-node-exporter.service\n"


# ── CernVM-FS ────────────────────────────────────────────────────────────────
# CVMFS is prebaked-only: the client + autofs are baked into the golden image
# (images/build.sh), so cloud-init lays only the per-pool dynamic layer —
# default.local (proxy + repos + quota), the containers.conf.d drop-in that binds
# each repo into every job container, the in-guest firewall hole for the proxy,
# and the eager-mount of each repo. Empty repo list → every CVMFS placeholder
# resolves away and the output is exactly the non-CVMFS render.


def _proxy_hosts(http_proxy: str) -> list[str]:
    """Hostnames in a CVMFS_HTTP_PROXY value (`;`/`|`-separated proxy URLs), for
    the in-guest firewall resolve. `DIRECT`/`auto` and empties are skipped, so a
    DIRECT config simply opens no proxy hole (its Stratum-1s must then be publicly
    reachable — the coarse firewall still drops CERN-internal)."""
    hosts: list[str] = []
    for part in re.split(r"[;|]", http_proxy):
        part = part.strip()
        if not part or part.upper() in {"DIRECT", "AUTO"}:
            continue
        netloc = part.split("://", 1)[-1].split("/", 1)[0]  # strip scheme + path
        host = netloc.rsplit(":", 1)[0]  # strip :port (squids are hostnames, not v6)
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _cvmfs_write_files(repos: tuple[str, ...], proxy: str, quota_mb: int) -> str:
    """The two write_files entries: the CVMFS client config, and the podman
    drop-in that binds each repo into EVERY job container. Per-repo binds, not a
    whole-/cvmfs bind — the autofs root readdir is denied under the rootless
    userns, but a bind of an already-mounted repo tree is not."""
    volumes = ", ".join(f'"/cvmfs/{r}:/cvmfs/{r}"' for r in repos)
    return (
        "  # CernVM-FS client config (client + autofs are baked; this is the\n"
        "  # per-pool dynamic layer: proxy, repo list, cache quota).\n"
        "  - path: /etc/cvmfs/default.local\n"
        "    content: |\n"
        f'      CVMFS_HTTP_PROXY="{proxy}"\n'
        f"      CVMFS_REPOSITORIES={','.join(repos)}\n"
        f"      CVMFS_QUOTA_LIMIT={quota_mb}\n"
        "\n"
        "  # Default per-repo bind mounts for every job container. podman applies\n"
        "  # containers.conf.d to BOTH the CLI shim and the API socket, so this\n"
        "  # covers `container:` jobs and step-level `docker run` alike.\n"
        "  - path: /etc/containers/containers.conf.d/10-cvmfs.conf\n"
        "    content: |\n"
        "      [containers]\n"
        f"      volumes = [{volumes}]\n"
    )


def _cvmfs_setup(repos: tuple[str, ...], proxy: str) -> str:
    """runcmd steps, spliced AFTER the firewall apply: open the proxy hole (the
    nft table + empty set now exist), then eager-mount each repo so the per-repo
    binds above land on already-mounted trees."""
    out = ""
    hosts = _proxy_hosts(proxy)
    if hosts:
        out += (
            "  # Open the firewall to the CVMFS proxy: resolve the squid hostnames\n"
            "  # in-guest (DNS/53 stays allowed post-lockdown) and add their IPs to\n"
            "  # the nft set, so rotating A-records self-heal on every recycle.\n"
            "  - |\n"
            f"    ips=$(getent ahostsv4 {' '.join(hosts)} | awk '{{print $1}}' "
            "| sort -u | paste -sd, -)\n"
            '    [ -n "$ips" ] && nft add element inet husk cvmfs_proxy "{ $ips }"\n'
        )
    out += (
        "  # Eager-mount the configured repos so a per-repo bind makes them visible\n"
        "  # inside rootless job containers (autofs triggers don't cross the userns).\n"
        "  - systemctl start autofs\n"
        f'  - for r in {" ".join(repos)}; do cvmfs_config probe "$r" || true; done\n'
    )
    return out


def render_cloud_init(
    jit_blob: str,
    runner_url: str,
    *,
    gpu: bool = False,
    prebaked: bool = False,
    scrape_cidr: str = "",
    cvmfs_repos: tuple[str, ...] = (),
    cvmfs_proxy: str = "",
    cvmfs_quota_mb: int = 4000,
) -> bytes:
    """Render the cloud-init user-data for one slot.

    Two orthogonal flags:

    * `prebaked=False` (default) boots a *stock* image: cloud-init installs
      podman + the runner + (if `gpu`) the NVIDIA driver/toolkit. With
      `gpu=False` the output is byte-for-byte the validated template — the
      OpenStack/CPU backend is untouched.
    * `prebaked=True` boots a *golden* image (images/build.sh) where all of that
      is baked: cloud-init does only the dynamic work (JIT config, egress
      firewall, GPU runtime activation, start).

    `gpu=True` adds GPU support in either mode: the install half only on a stock
    image, the runtime half (`modprobe` + `nvidia-ctk cdi generate`) always — the
    kmod load and CDI spec are hardware-dependent and can't be baked.

    `scrape_cidr` turns on in-guest metrics: it opens `:9100` to that source only
    and starts the (baked) node_exporter. **Opt-in and fail-closed** — unset means
    no ingress rule and no exporter running, i.e. nothing listening, which is why
    a pool whose scraper source isn't known yet can simply leave it out. Requires
    `prebaked` (node_exporter only exists in the golden image); the config loader
    rejects the combination, and it is ignored here on a stock image.

    `cvmfs_repos`/`cvmfs_proxy`/`cvmfs_quota_mb` turn on CernVM-FS: cloud-init writes
    the client config + a per-repo containers.conf.d bind drop-in, opens the proxy
    hole in the firewall, and eager-mounts each repo before the runner starts. Also
    prebaked-only (the client is baked) and fail-closed — empty `cvmfs_repos` renders
    identically to a non-CVMFS slot. The loader rejects CVMFS + stock; ignored here
    on a stock image."""
    metrics = scrape_cidr if (scrape_cidr and prebaked) else ""
    cvmfs = cvmfs_repos if (cvmfs_repos and prebaked) else ()
    if prebaked:
        template = PREBAKED_RUNNER_CLOUD_INIT
        if gpu:
            template = template.replace(
                _PREBAKED_GPU_ANCHOR, _GPU_RUNTIME + _PREBAKED_GPU_ANCHOR, 1
            )
        template = template.replace(
            "@@METRICS_START@@\n", _METRICS_START if metrics else ""
        )
    else:
        template = RUNNER_CLOUD_INIT
        if gpu:
            template = template.replace(
                _FIREWALL_ANCHOR, _GPU_INSTALL + _GPU_RUNTIME + _FIREWALL_ANCHOR, 1
            )
    ingress = ""
    if metrics:
        saddr = "ip6 saddr" if ":" in metrics else "ip saddr"
        ingress = _METRICS_INGRESS.replace("@@SADDR@@", saddr).replace(
            "@@SCRAPE_CIDR@@", metrics
        )
    template = template.replace("@@METRICS_INGRESS@@", ingress)

    # CVMFS placeholders. SET/PROXY live in BOTH ruleset copies (so the drift guard
    # sees identical text); WRITE_FILES/SETUP exist only in the prebaked template,
    # where .replace on the stock template is a harmless no-op. All resolve to ""
    # when CVMFS is off.
    if cvmfs:
        cvmfs_set = "        set cvmfs_proxy { type ipv4_addr; }\n"
        cvmfs_rule = (
            "          # Allow the CVMFS HTTP proxy. Its CERN squids live in the\n"
            "          # CERN-internal ranges dropped below, so this accept must\n"
            "          # precede them; the set is populated in runcmd after an\n"
            "          # in-guest resolve of the proxy hostnames.\n"
            "          ip daddr @cvmfs_proxy accept\n\n"
        )
        cvmfs_wf = _cvmfs_write_files(cvmfs, cvmfs_proxy, cvmfs_quota_mb)
        cvmfs_setup = _cvmfs_setup(cvmfs, cvmfs_proxy)
    else:
        cvmfs_set = cvmfs_rule = cvmfs_wf = cvmfs_setup = ""
    template = (
        template.replace("@@CVMFS_SET@@", cvmfs_set)
        .replace("@@CVMFS_PROXY@@", cvmfs_rule)
        .replace("@@CVMFS_WRITE_FILES@@", cvmfs_wf)
        .replace("@@CVMFS_SETUP@@", cvmfs_setup)
    )
    return (
        template.replace("@@JIT@@", jit_blob)
        .replace("@@RUNNER_URL@@", runner_url)
        .encode()
    )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
