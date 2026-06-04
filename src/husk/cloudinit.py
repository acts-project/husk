"""Cloud-init rendering — the validated Phase 2/3 recipe, lifted verbatim.

The template installs rootless Podman + the Docker→Podman compatibility shim and
runs a single-use JIT runner that powers the slot off when the job finishes
(`husk-poweroff.service`), which the controller observes as SHUTOFF → recycle.
The firewall is intentionally omitted (the network-policy milestone is gated).

`@@JIT@@` / `@@RUNNER_URL@@` are substituted per recycle. The runner is pinned to
uid 1000 so `/run/user/1000` lines up with the systemd unit.
"""

from __future__ import annotations

import base64

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


def render_cloud_init(jit_blob: str, runner_url: str) -> bytes:
    """Substitute the JIT config and runner download URL into the template."""
    return (
        RUNNER_CLOUD_INIT.replace("@@JIT@@", jit_blob).replace("@@RUNNER_URL@@", runner_url)
    ).encode()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
