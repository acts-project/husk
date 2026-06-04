#!/usr/bin/env python3
"""
Phase 3 — Automate the runner via cloud-init + systemd, and prove the
slot-recycle loop.

This is the single-slot embryo of huskd's reconcile loop (see plan.md "slots,
not ephemeral VMs"). It runs CONTROLLER-SIDE (wherever --os-cloud cern and the
GitHub API work — NOT on the slot). It drives ONE OpenStack VM ("slot") through
the recycle cycle:

    mint JIT  ->  create/rebuild slot with cloud-init(JIT)  ->  runner registers
    ->  job runs  ->  runner exits  ->  ExecStopPost poweroff  ->  SHUTOFF
    ->  mint fresh JIT  ->  rebuild  ->  ... repeat

Milestone A (cloud-init re-runs with fresh user_data on rebuild) is already
proven by verify-phase1.py, so this script assumes it and focuses on B and C:

    B  full runner via cloud-init: boots, registers, runs one job, SHUTOFF
    C  recycle loop: N cycles on the SAME slot, timed, no state leak

SCOPE: recycle mechanics only. No nftables/network-policy (deferred), no full
workflow compat matrix (validated by hand in Phase 2).

ENV:
    GH_TOKEN   GitHub PAT with repo admin on REPO (mint JIT, dispatch, reap)

USAGE:
    python phase3-recycle.py create                 # B: one slot, one job
    python phase3-recycle.py diag    <server_id>     # SSH dump for debugging B
    python phase3-recycle.py watch   <server_id>     # poll status until SHUTOFF
    python phase3-recycle.py recycle <server_id>     # one rebuild from SHUTOFF
    python phase3-recycle.py loop --cycles 5         # C: full timed recycle loop
    python phase3-recycle.py clean   [server_id]     # reap offline runners (+ del VM)
"""

import argparse
import base64
import os
import socket
import subprocess
import sys
import time

import openstack
import requests

# ---------------------------------------------------------------------------
# Configuration — OpenStack values match verify-phase1.py / measure-standup.py
# ---------------------------------------------------------------------------
CLOUD_NAME = "cern"
IMAGE_NAME = "ALMA10 - x86_64"
FLAVOR_NAME = "m2.small"
NETWORK_NAME = "CERN_NETWORK"
KEYPAIR_NAME = "acts-gha"
SSH_USER = "root"  # CERN ALMA10: default login is root
TAG = "husk-phase3"

# Rebuild with user_data requires Nova microversion >= 2.57. CERN runs 2.96;
# we pin 2.79 (matches Phase 1). CERN's Nova rejects "name" in the rebuild
# body ("Hostname cannot be updated"), so we POST a minimal action body.
REBUILD_MICROVERSION = "2.79"

MAX_WAIT_ACTIVE = 1200
MAX_WAIT_REBUILD = 600
MAX_WAIT_SSH = 300
MAX_WAIT_RUNNER = 420  # ACTIVE -> runner online (cloud-init installs the runner)
MAX_WAIT_JOB = 900  # dispatch -> SHUTOFF
POLL_INTERVAL = 2

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
REPO = "acts-project/husk-test"
RUNNER_VERSION = "2.334.0"
RUNNER_URL = (
    f"https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/"
    f"actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz"
)
# Dedicated label so dispatched jobs land ONLY on our slot, never on the
# leftover Phase 2 "manual-test" runners.
RUNNER_LABELS = ["self-hosted", "linux", "x64", "husk-phase3"]
RUNNER_GROUP_ID = 1  # "Default"
WORKFLOW_FILE = "phase3.yml"

GH_API = "https://api.github.com"


def gh_session():
    tok = os.environ.get("GH_TOKEN")
    if not tok:
        sys.exit("GH_TOKEN not set (source .env)")
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    return s


def gh_mint_jit(s, name):
    """POST generate-jitconfig -> encoded_jit_config (single-use, auto-dereg).

    Idempotent: JIT runner names must be unique, so if a stale registration with
    this name lingers (e.g. an interrupted run that minted but never connected,
    or a re-run reusing cycle numbers), delete it and retry once."""
    body = {
        "name": name,
        "runner_group_id": RUNNER_GROUP_ID,
        "labels": RUNNER_LABELS,
        "work_folder": "_work",
    }
    url = f"{GH_API}/repos/{REPO}/actions/runners/generate-jitconfig"
    r = s.post(url, json=body)
    if r.status_code == 409:
        existing = gh_find_runner(s, name)
        if existing:
            print(
                f"    runner '{name}' already exists ({existing['status']}); deleting and retrying"
            )
            gh_delete_runner(s, existing["id"])
        r = s.post(url, json=body)
    if r.status_code != 201:
        raise RuntimeError(f"JIT mint failed: HTTP {r.status_code}: {r.text[:300]}")
    return r.json()["encoded_jit_config"]


def gh_find_runner(s, name):
    r = s.get(f"{GH_API}/repos/{REPO}/actions/runners?per_page=100")
    r.raise_for_status()
    for x in r.json().get("runners", []):
        if x["name"] == name:
            return x
    return None


def gh_delete_runner(s, runner_id):
    return s.delete(f"{GH_API}/repos/{REPO}/actions/runners/{runner_id}")


def gh_reap_offline(s):
    """Delete every offline runner — clears Phase 2 leftovers & dead JIT regs."""
    r = s.get(f"{GH_API}/repos/{REPO}/actions/runners?per_page=100")
    r.raise_for_status()
    reaped = []
    for x in r.json().get("runners", []):
        if x["status"] == "offline":
            gh_delete_runner(s, x["id"])
            reaped.append(x["name"])
    return reaped


def gh_dispatch(s, ref="main"):
    r = s.post(
        f"{GH_API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        json={"ref": ref},
    )
    if r.status_code == 403:
        raise RuntimeError(
            "dispatch failed: HTTP 403 — the PAT lacks 'Actions: write'. Grant "
            "Repository permissions > Actions > Read and write on the token, or "
            "run with --no-dispatch and trigger the workflow yourself."
        )
    if r.status_code != 204:
        raise RuntimeError(f"dispatch failed: HTTP {r.status_code}: {r.text[:300]}")


def gh_latest_run_conclusion(s):
    r = s.get(
        f"{GH_API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?event=workflow_dispatch&per_page=1"
    )
    if r.status_code != 200:
        return None
    runs = r.json().get("workflow_runs", [])
    if not runs:
        return None
    return runs[0].get("status"), runs[0].get("conclusion"), runs[0].get("html_url")


# ---------------------------------------------------------------------------
# Cloud-init — the validated Phase 2 recipe (plan.md), firewall omitted (the
# network-policy milestone is out of A-C scope). @@JIT@@ / @@RUNNER_URL@@ are
# substituted per recycle. runner pinned to uid 1000 so /run/user/1000 lines up.
# ---------------------------------------------------------------------------
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


def render_cloud_init(jit_blob):
    return (
        RUNNER_CLOUD_INIT.replace("@@JIT@@", jit_blob).replace(
            "@@RUNNER_URL@@", RUNNER_URL
        )
    ).encode()


def b64(data):
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------------
# OpenStack plumbing (adapted from verify-phase1.py)
# ---------------------------------------------------------------------------
def first_ip(server):
    addrs = server.addresses.get(NETWORK_NAME, [])
    return addrs[0]["addr"] if addrs else None


def task_state(server):
    return getattr(server, "task_state", None) or server.to_dict().get(
        "OS-EXT-STS:task_state"
    )


def wait_for_status(
    conn, server, target, deadline_s, valid_intermediate, require_seen=None
):
    t0 = time.monotonic()
    valid = set(valid_intermediate) | {target, "ERROR"}
    last, last_task = None, "__unset__"
    seen_required = require_seen is None
    while time.monotonic() - t0 < deadline_s:
        server = conn.compute.get_server(server.id)
        ts = task_state(server)
        if server.status != last:
            print(f"    status -> {server.status} (+{time.monotonic() - t0:.0f}s)")
            last = server.status
        if ts != last_task:
            print(f"    task_state -> {ts} (+{time.monotonic() - t0:.0f}s)")
            last_task = ts
        if require_seen and server.status == require_seen:
            seen_required = True
        if server.status == "ERROR":
            raise RuntimeError(f"Server entered ERROR: {server.fault}")
        if server.status == target and seen_required and not ts:
            return server
        if server.status not in valid:
            print(f"    !! unexpected status: {server.status}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(
        f"Timed out waiting for {target} (last={last}, task={last_task})"
    )


def wait_for_ip(conn, server, deadline_s=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        server = conn.compute.get_server(server.id)
        ip = first_ip(server)
        if ip:
            return ip
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"No IP on {NETWORK_NAME} within {deadline_s}s")


def ssh(ip, key_path, command, timeout=60):
    cmd = [
        "ssh",
        "-i",
        key_path,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "LogLevel=ERROR",
        f"{SSH_USER}@{ip}",
        command,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def rebuild_with_user_data(conn, server, jit_blob, image_id):
    """POST a minimal rebuild action with fresh user_data (CERN-compatible)."""
    resp = conn.compute.post(
        f"/servers/{server.id}/action",
        json={
            "rebuild": {
                "imageRef": image_id,
                "user_data": b64(render_cloud_init(jit_blob)),
            }
        },
        headers={"OpenStack-API-Version": f"compute {REBUILD_MICROVERSION}"},
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(
            f"rebuild rejected: HTTP {resp.status_code}: {resp.text[:300]}"
        )


def wait_rebuild_done(conn, server, deadline_s):
    """Poll until a rebuild completes. Returns the server, which may be SHUTOFF
    (Nova rebuild preserves power state). Requires observing the rebuild task
    (task_state set, or status REBUILD) before accepting a cleared task, so we
    don't mistake the pre-rebuild state for completion."""
    t0 = time.monotonic()
    last, last_task = None, "__unset__"
    seen = False
    while time.monotonic() - t0 < deadline_s:
        server = conn.compute.get_server(server.id)
        ts = task_state(server)
        if server.status != last:
            print(f"    status -> {server.status} (+{time.monotonic() - t0:.0f}s)")
            last = server.status
        if ts != last_task:
            print(f"    task_state -> {ts} (+{time.monotonic() - t0:.0f}s)")
            last_task = ts
        if server.status == "ERROR":
            raise RuntimeError(f"Server entered ERROR: {server.fault}")
        if ts or server.status == "REBUILD":
            seen = True
        if seen and not ts and server.status in ("ACTIVE", "SHUTOFF"):
            return server
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"rebuild did not complete in {deadline_s}s (last={last})")


def recycle_rebuild(conn, server, jit_blob, image_id):
    """Recycle a slot: rebuild with fresh user_data, then ensure it's ACTIVE.
    A SHUTOFF slot stays SHUTOFF after rebuild (Nova preserves power state), so
    we explicitly os-start it. Returns the ACTIVE server."""
    rebuild_with_user_data(conn, server, jit_blob, image_id)
    server = conn.compute.get_server(server.id)
    server = wait_rebuild_done(conn, server, MAX_WAIT_REBUILD)
    if server.status != "ACTIVE":
        print(
            f"    rebuild settled to {server.status}; os-start (Nova preserves power state)"
        )
        conn.compute.start_server(server)
        server = wait_for_status(
            conn,
            server,
            "ACTIVE",
            MAX_WAIT_REBUILD,
            valid_intermediate={"SHUTOFF", "BUILD"},
        )
    return server


def wait_runner_online(s, name, deadline_s):
    """Poll GitHub until the named runner is online. Returns elapsed or None."""
    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < deadline_s:
        r = gh_find_runner(s, name)
        status = r["status"] if r else "absent"
        if status != last:
            print(f"    runner[{name}] -> {status} (+{time.monotonic() - t0:.0f}s)")
            last = status
        if status == "online":
            return time.monotonic() - t0
        time.sleep(POLL_INTERVAL)
    return None


def wait_shutoff(conn, server, deadline_s):
    """Poll the slot until SHUTOFF (job done -> runner exit -> poweroff)."""
    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < deadline_s:
        server = conn.compute.get_server(server.id)
        if server.status != last:
            print(f"    slot -> {server.status} (+{time.monotonic() - t0:.0f}s)")
            last = server.status
        if server.status == "SHUTOFF":
            return time.monotonic() - t0
        if server.status == "ERROR":
            raise RuntimeError(f"slot entered ERROR: {server.fault}")
        time.sleep(POLL_INTERVAL)
    return None


def connect_and_resolve():
    conn = openstack.connect(cloud=CLOUD_NAME)
    image = conn.image.find_image(IMAGE_NAME)
    if not image:
        sys.exit(f"Image '{IMAGE_NAME}' not found")
    flavor = conn.compute.find_flavor(FLAVOR_NAME)
    if not flavor:
        sys.exit(f"Flavor '{FLAVOR_NAME}' not found")
    network = conn.network.find_network(NETWORK_NAME)
    if not network:
        sys.exit(f"Network '{NETWORK_NAME}' not found")
    return conn, image, flavor, network


def new_vm_name():
    """Unique VM name. CERN registers VM names in DNS (LANDB) and rejects
    duplicates, so we suffix with a timestamp (matches verify-phase1.py). The
    name is stable across REBUILDs — only `create` mints a new one."""
    return f"{TAG}-{int(time.time())}"


def runner_name(vm, cycle):
    """GitHub runner name — unique per recycle cycle. GitHub-side only; does
    not touch CERN DNS. JIT requires the name be unique among registrations."""
    return f"{vm}-c{cycle}"


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_create(args):
    """Milestone B: one slot, cloud-init installs the runner, it picks up a job."""
    s = gh_session()
    conn, image, flavor, network = connect_and_resolve()
    vm = new_vm_name()
    name = runner_name(vm, 0)
    print(f"Minting JIT for runner '{name}' (labels={RUNNER_LABELS})...")
    jit = gh_mint_jit(s, name)

    print(f"Creating slot '{vm}' (runner pinned {RUNNER_VERSION})...")
    t0 = time.monotonic()
    server = conn.compute.create_server(
        name=vm,
        image_id=image.id,
        flavor_id=flavor.id,
        networks=[{"uuid": network.id}],
        key_name=KEYPAIR_NAME,
        user_data=b64(render_cloud_init(jit)),
        metadata={"managed-by": TAG},
    )
    print(f"  created {server.id}")
    server = wait_for_status(
        conn, server, "ACTIVE", MAX_WAIT_ACTIVE, valid_intermediate={"BUILD"}
    )
    ip = wait_for_ip(conn, server)
    print(
        f"  ACTIVE ip={ip} (+{time.monotonic() - t0:.0f}s) — cloud-init now installing runner"
    )

    elapsed = wait_runner_online(s, name, MAX_WAIT_RUNNER)
    if elapsed is None:
        print("  runner did NOT come online in time. Debug with:")
        print(f"    python {sys.argv[0]} diag {server.id}")
        sys.exit(1)
    print(f"  RUNNER ONLINE after +{time.monotonic() - t0:.0f}s total")
    print("\nNext: dispatch a job and watch it recycle:")
    print(f"    python {sys.argv[0]} loop --server {server.id} --cycles 1")
    print(f"    python {sys.argv[0]} watch {server.id}")
    print(f"\nslot_id={server.id}  ip={ip}  runner={name}")


def cmd_diag(args):
    """SSH into a slot and dump everything needed to debug cloud-init / runner."""
    conn = openstack.connect(cloud=CLOUD_NAME)
    server = conn.compute.get_server(args.server)
    ip = first_ip(server)
    print(f"slot {server.id}  status={server.status}  ip={ip}")
    if not ip:
        sys.exit("no IP")
    probes = [
        ("cloud-init status", "cloud-init status --long 2>&1 | head -20"),
        (
            "cloud-init errors",
            "grep -iE 'error|fail|traceback' /var/log/cloud-init-output.log 2>&1 | tail -30",
        ),
        (
            "runner unit",
            "systemctl status husk-runner.service --no-pager 2>&1 | head -25",
        ),
        (
            "runner journal",
            "journalctl -u husk-runner.service --no-pager 2>&1 | tail -40",
        ),
        (
            "podman socket (runner)",
            "sudo -u runner XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status podman.socket --no-pager 2>&1 | head -10",
        ),
        (
            "docker shim",
            "/usr/bin/env -u DOCKER_HOST /usr/local/bin/docker version --format '{{.Server.APIVersion}}' 2>&1 | tail -3",
        ),
        (
            "docker.sock",
            "ls -l /run/docker.sock /run/user/1000/podman/podman.sock 2>&1",
        ),
        (
            "runner _diag tail",
            "ls -t /opt/actions-runner/_diag/*.log 2>/dev/null | head -1 | xargs tail -20 2>&1",
        ),
    ]
    for label, cmd in probes:
        print(f"\n----- {label} -----")
        rc, out, err = ssh(ip, args.key, cmd, timeout=90)
        print(out or err or "<empty>")


def cmd_watch(args):
    conn = openstack.connect(cloud=CLOUD_NAME)
    server = conn.compute.get_server(args.server)
    print(f"Watching slot {server.id} until SHUTOFF...")
    el = wait_shutoff(conn, server, MAX_WAIT_JOB)
    if el is None:
        print("  did NOT reach SHUTOFF in time")
        sys.exit(1)
    print(f"  SHUTOFF after +{el:.0f}s")


def _one_cycle(s, conn, image, server, cycle, dispatch=True):
    """Recycle the slot once: fresh JIT -> rebuild -> online -> dispatch -> SHUTOFF.
    Returns a timing dict."""
    name = runner_name(server.name, cycle)
    t = {}
    print(f"\n=== cycle {cycle}: rebuild slot as runner '{name}' ===")
    jit = gh_mint_jit(s, name)
    t_reb = time.monotonic()
    server = recycle_rebuild(conn, server, jit, image.id)
    t["active_s"] = time.monotonic() - t_reb
    print(f"  ACTIVE after +{t['active_s']:.0f}s — cloud-init installing runner")
    online = wait_runner_online(s, name, MAX_WAIT_RUNNER)
    if online is None:
        raise RuntimeError(f"runner '{name}' never came online after rebuild")
    t["online_s"] = time.monotonic() - t_reb
    print(f"  RUNNER ONLINE after +{t['online_s']:.0f}s (recycle time)")

    if dispatch:
        print("  dispatching workflow...")
        gh_dispatch(s)
    else:
        print(
            f"  >>> TRIGGER the husk-phase3 workflow now (runner '{name}' is online):"
        )
        print(
            f"  >>>   gh workflow run {WORKFLOW_FILE} -R {REPO}   (or the Actions UI)"
        )
        print("  >>> waiting for the job to run and the slot to power off...")
    t_disp = time.monotonic()
    el = wait_shutoff(conn, conn.compute.get_server(server.id), MAX_WAIT_JOB)
    if el is None:
        raise RuntimeError("slot did not reach SHUTOFF after dispatch")
    t["job_to_shutoff_s"] = time.monotonic() - t_disp
    concl = gh_latest_run_conclusion(s)
    t["run"] = concl
    print(f"  job ran, SHUTOFF after +{t['job_to_shutoff_s']:.0f}s; run={concl}")
    return server, t


def cmd_loop(args):
    """Milestone C: N recycle cycles on one slot, timed."""
    s = gh_session()
    conn, image, flavor, network = connect_and_resolve()

    if args.server:
        server = conn.compute.get_server(args.server)
        print(f"Adopting slot {server.id} (status={server.status})")
        if server.status != "SHUTOFF":
            print("  note: slot is not SHUTOFF; rebuild will still proceed")
        start_cycle = 1
    else:
        # bootstrap a fresh slot via create (cycle 0 = first job)
        print("No --server given; bootstrapping a slot first (cycle 0)...")
        vm = new_vm_name()
        name = runner_name(vm, 0)
        jit = gh_mint_jit(s, name)
        server = conn.compute.create_server(
            name=vm,
            image_id=image.id,
            flavor_id=flavor.id,
            networks=[{"uuid": network.id}],
            key_name=KEYPAIR_NAME,
            user_data=b64(render_cloud_init(jit)),
            metadata={"managed-by": TAG},
        )
        print(f"  created {server.id}")
        server = wait_for_status(
            conn, server, "ACTIVE", MAX_WAIT_ACTIVE, valid_intermediate={"BUILD"}
        )
        wait_for_ip(conn, server)
        if wait_runner_online(s, name, MAX_WAIT_RUNNER) is None:
            print(
                f"  runner never came online; debug: python {sys.argv[0]} diag {server.id}"
            )
            sys.exit(1)
        if args.no_dispatch:
            print(
                f"  >>> TRIGGER husk-phase3 now (runner '{name}' online): gh workflow run {WORKFLOW_FILE} -R {REPO}"
            )
        else:
            gh_dispatch(s)
        if wait_shutoff(conn, conn.compute.get_server(server.id), MAX_WAIT_JOB) is None:
            sys.exit("cycle 0 never reached SHUTOFF")
        print("  cycle 0 complete (slot now SHUTOFF)")
        start_cycle = 1

    timings = []
    try:
        for cycle in range(start_cycle, start_cycle + args.cycles):
            try:
                server, t = _one_cycle(
                    s, conn, image, server, cycle, dispatch=not args.no_dispatch
                )
                timings.append((cycle, t))
            except Exception as e:
                print(f"  cycle {cycle} failed: {e}")
                break
    finally:
        print("\n" + "=" * 64)
        print(f"Recycle results for slot {server.id}:")
        print(f"  {'cycle':>5}  {'ACTIVE':>8}  {'ONLINE':>8}  {'job->off':>9}  run")
        for cycle, t in timings:
            run = t.get("run")
            run_s = f"{run[0]}/{run[1]}" if run else "?"
            print(
                f"  {cycle:>5}  {t.get('active_s', 0):>7.0f}s  {t.get('online_s', 0):>7.0f}s"
                f"  {t.get('job_to_shutoff_s', 0):>8.0f}s  {run_s}"
            )
        if timings:
            best = min(t["online_s"] for _, t in timings)
            print(
                f"\n  Best recycle (rebuild->runner online): {best:.0f}s  (target <60s)"
            )
        print(f"\n  Slot left SHUTOFF: {server.id}")
        print(
            f"  Reap runners + delete VM with: python {sys.argv[0]} clean {server.id}"
        )


def cmd_recycle(args):
    """One rebuild from SHUTOFF (no dispatch) — for manual stepping."""
    s = gh_session()
    conn, image, _, _ = connect_and_resolve()
    server = conn.compute.get_server(args.server)
    name = runner_name(server.name, args.cycle)
    print(f"Rebuilding {server.id} as runner '{name}'...")
    jit = gh_mint_jit(s, name)
    t0 = time.monotonic()
    server = recycle_rebuild(conn, server, jit, image.id)
    print(f"  ACTIVE after +{time.monotonic() - t0:.0f}s")
    online = wait_runner_online(s, name, MAX_WAIT_RUNNER)
    if online is None:
        sys.exit("runner never came online")
    print(f"  RUNNER ONLINE — recycle time +{time.monotonic() - t0:.0f}s")


def cmd_clean(args):
    s = gh_session()
    reaped = gh_reap_offline(s)
    print(f"Reaped {len(reaped)} offline runner(s): {reaped}")
    if args.server:
        conn = openstack.connect(cloud=CLOUD_NAME)
        try:
            conn.compute.delete_server(args.server)
            print(f"Deleted slot {args.server}")
        except Exception as e:
            print(f"WARNING: delete failed: {e}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description="Husk Phase 3 single-slot recycle driver")
    default_key = os.path.expanduser("~/.ssh/acts-gha.pem")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("create", help="B: create one slot + run to runner-online")
    sp.set_defaults(func=cmd_create)

    sp = sub.add_parser("diag", help="SSH dump for debugging a slot")
    sp.add_argument("server")
    sp.add_argument("--key", default=default_key)
    sp.set_defaults(func=cmd_diag)

    sp = sub.add_parser("watch", help="poll a slot until SHUTOFF")
    sp.add_argument("server")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("recycle", help="one rebuild from SHUTOFF (no dispatch)")
    sp.add_argument("server")
    sp.add_argument("--cycle", type=int, default=1)
    sp.set_defaults(func=cmd_recycle)

    sp = sub.add_parser("loop", help="C: N timed recycle cycles on one slot")
    sp.add_argument("--server", help="adopt an existing SHUTOFF slot")
    sp.add_argument("--cycles", type=int, default=5)
    sp.add_argument(
        "--no-dispatch",
        action="store_true",
        help="don't auto-trigger the workflow; you run it manually each cycle",
    )
    sp.set_defaults(func=cmd_loop)

    sp = sub.add_parser("clean", help="reap offline runners (+ optionally delete VM)")
    sp.add_argument("server", nargs="?")
    sp.set_defaults(func=cmd_clean)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
