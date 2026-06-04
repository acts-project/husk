#!/usr/bin/env python3
"""
Phase 1 verification — cloud-init on the central image.

Proves the two properties the slot-recycle architecture depends on:

  1. cloud-init applies user_data on FIRST boot.
  2. cloud-init RE-RUNS with FRESH user_data on a Nova `server rebuild`.

The second property is the critical one: slots are recycled via rebuild, and
each rebuild delivers a new JIT config through user_data. If cloud-init does
not re-run on rebuild, the whole "slots, not ephemeral VMs" design needs a
different config-delivery mechanism (or `cloud-init clean` baked into the
image).

Flow:
    create (marker A) -> SSH-verify A -> rebuild (marker B) -> SSH-verify B
    -> cleanup

Each step prints PASS/FAIL. Exit code is non-zero if any criterion fails.

USAGE:
    python verify-phase1.py [--key ~/.ssh/acts-gha.pem] [--keep]

    --keep   leave the VM running for manual inspection (default: delete)
"""

import argparse
import base64
import socket
import subprocess
import sys
import time

import openstack

# ---------------------------------------------------------------------------
# Configuration — matches measure-standup.py
# ---------------------------------------------------------------------------
CLOUD_NAME = "cern"
IMAGE_NAME = "ALMA10 - x86_64"
FLAVOR_NAME = "m2.small"
NETWORK_NAME = "CERN_NETWORK"
KEYPAIR_NAME = "acts-gha"
SSH_USER = "root"  # CERN ALMA10 image: default login is root, not almalinux
TAG = "husk-phase1"

# Rebuild with user_data requires Nova microversion >= 2.57. The plan pins
# 2.79; we send that explicitly on the rebuild action.
REBUILD_MICROVERSION = "2.79"

MAX_WAIT_ACTIVE = 1200
MAX_WAIT_REBUILD = 600
MAX_WAIT_SSH = 300
POLL_INTERVAL = 2

MARKER_PATH = "/var/lib/husk-marker"

# ---------------------------------------------------------------------------


def cloud_init_for(tag: str) -> bytes:
    """A cloud-config that writes a uniquely identifiable marker.

    Touches both a write_files path and a runcmd append so we can tell which
    cloud-init *modules* ran, not just that the file was templated.
    """
    return (
        "#cloud-config\n"
        "write_files:\n"
        f"  - path: {MARKER_PATH}\n"
        f'    content: "PHASE1-MARKER-{tag}\\n"\n'
        "    permissions: '0644'\n"
        "runcmd:\n"
        f'  - echo "runcmd-{tag} ran at $(date -u +%FT%TZ)" >> {MARKER_PATH}\n'
    ).encode()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


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
    """Poll until status == target AND task_state is clear.

    require_seen: if given, an intermediate status (e.g. "REBUILD") that MUST
    be observed before we accept `target`. This guards against the rebuild
    race where Nova still reports the pre-rebuild ACTIVE for a moment: we
    refuse to call it done until we've actually seen it leave ACTIVE.
    """
    t0 = time.monotonic()
    valid = set(valid_intermediate) | {target, "ERROR"}
    last = None
    last_task = "__unset__"
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


def wait_for_ip(conn, server, deadline_s):
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        server = conn.compute.get_server(server.id)
        ip = first_ip(server)
        if ip:
            return ip
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"No IP on {NETWORK_NAME} within {deadline_s}s")


def wait_for_ssh_port(ip, deadline_s):
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        try:
            with socket.create_connection((ip, 22), timeout=3):
                return
        except OSError:
            time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"SSH port not open within {deadline_s}s")


def ssh(ip, key_path, command, timeout=60):
    """Run a remote command; return (rc, stdout, stderr)."""
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


class Results:
    def __init__(self):
        self.checks = []

    def check(self, name, ok, detail=""):
        self.checks.append((name, ok))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail:
            line += f"  ({detail})"
        print(line)
        return ok

    def all_ok(self):
        return all(ok for _, ok in self.checks)


def poll_for_marker(ip, key_path, expected_tag, deadline_s):
    """Wait for cloud-init to FINISH, then read the marker file.

    Returns (ssh_ever_connected, final_content). After a rebuild the box
    reboots, so SSH may refuse connections or briefly reach a half-booted
    system. We retry `cloud-init status --wait` until it connects and reports
    completion — that blocks until cloud-init's FINAL stage (which runs
    `runcmd`) is done — and only then snapshot the marker. This avoids the
    race where `write_files` output is visible but `runcmd` hasn't appended
    yet. If cloud-init genuinely does not re-run, the expected marker never
    appears and we correctly time out into a FAIL.
    """
    want_all = (f"PHASE1-MARKER-{expected_tag}", f"runcmd-{expected_tag} ran at")
    t0 = time.monotonic()
    ssh_ever = False
    last_content = "<never connected>"
    while time.monotonic() - t0 < deadline_s:
        remaining = max(10, int(deadline_s - (time.monotonic() - t0)))
        # Block until cloud-init's final stage completes (or ssh refuses).
        rc, out, err = ssh(ip, key_path, "cloud-init status --wait", timeout=remaining)
        if rc != 0 and "denied" in err.lower():
            last_content = f"<ssh auth failed: {err}>"
            time.sleep(POLL_INTERVAL)
            continue
        if rc != 0:
            # connection refused / reset mid-reboot — retry
            time.sleep(POLL_INTERVAL)
            continue
        # cloud-init reports done: read the fully-written marker.
        rc, out, err = ssh(ip, key_path, f"cat {MARKER_PATH} 2>&1", timeout=20)
        if rc == 0:
            ssh_ever = True
            last_content = out
            if all(s in out for s in want_all):
                return True, out
        time.sleep(POLL_INTERVAL)
    return ssh_ever, last_content


def verify_marker(ip, key_path, expected_tag, results, phase):
    """Confirm the expected marker, then cloud-init status + analyze."""
    print(f"  --- SSH verification ({phase}) ---")
    ssh_ok, content = poll_for_marker(ip, key_path, expected_tag, MAX_WAIT_SSH)

    if not ssh_ok:
        # Nothing below is trustworthy without a shell. Fail explicitly rather
        # than letting empty reads produce misleading PASSes.
        results.check(f"{phase}: SSH login as {SSH_USER}", False, content)
        results.check(
            f"{phase}: marker file contains PHASE1-MARKER-{expected_tag}", False
        )
        results.check(f"{phase}: runcmd-{expected_tag} executed", False)
        if expected_tag == "B":
            results.check("rebuild: stale MARKER-A absent", False)
        results.check(f"{phase}: cloud-init analyze show works", False)
        return

    results.check(f"{phase}: SSH login as {SSH_USER}", True)

    want = f"PHASE1-MARKER-{expected_tag}"
    results.check(
        f"{phase}: marker file contains {want}",
        want in content,
        repr(content),
    )
    results.check(
        f"{phase}: runcmd-{expected_tag} executed",
        f"runcmd-{expected_tag} ran at" in content,
        repr(content),
    )
    # On rebuild the disk is rebuilt from image: the OLD marker must be gone.
    if expected_tag == "B":
        results.check(
            "rebuild: stale MARKER-A absent (disk reset, fresh user_data)",
            "PHASE1-MARKER-A" not in content,
            repr(content),
        )

    rc, out, err = ssh(ip, key_path, "cloud-init status 2>&1")
    results.check(
        f"{phase}: cloud-init status == done",
        rc == 0 and "done" in out,
        out or err,
    )
    rc, out, err = ssh(ip, key_path, "cloud-init analyze show 2>&1 | head -1")
    results.check(
        f"{phase}: cloud-init analyze show works",
        rc == 0 and "boot" in out.lower(),
        out or err,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--key",
        default=f"{__import__('os').path.expanduser('~')}/.ssh/acts-gha.pem",
        help="Path to the private key matching the keypair",
    )
    parser.add_argument(
        "--keep", action="store_true", help="Do not delete the VM at the end"
    )
    args = parser.parse_args()

    print(f"Connecting to cloud '{CLOUD_NAME}'...")
    conn = openstack.connect(cloud=CLOUD_NAME)

    print("Resolving image, flavor, network...")
    image = conn.image.find_image(IMAGE_NAME)
    if not image:
        sys.exit(f"Image '{IMAGE_NAME}' not found")
    flavor = conn.compute.find_flavor(FLAVOR_NAME)
    if not flavor:
        sys.exit(f"Flavor '{FLAVOR_NAME}' not found")
    network = conn.network.find_network(NETWORK_NAME)
    if not network:
        sys.exit(f"Network '{NETWORK_NAME}' not found")
    print(f"  image={image.name}  flavor={flavor.name}  network={network.name}")
    print(f"  keypair={KEYPAIR_NAME}  key={args.key}")

    results = Results()
    server = None
    try:
        # --- Create with marker A ---
        print("\n=== CREATE (marker A) ===")
        t0 = time.monotonic()
        server = conn.compute.create_server(
            name=f"{TAG}-{int(time.time())}",
            image_id=image.id,
            flavor_id=flavor.id,
            networks=[{"uuid": network.id}],
            key_name=KEYPAIR_NAME,
            user_data=b64(cloud_init_for("A")),
            metadata={"managed-by": TAG},
        )
        print(f"  created {server.id}")
        server = wait_for_status(
            conn, server, "ACTIVE", MAX_WAIT_ACTIVE, valid_intermediate={"BUILD"}
        )
        ip = wait_for_ip(conn, server, deadline_s=60)
        print(f"  ACTIVE, ip={ip}, boot took {time.monotonic() - t0:.0f}s")
        verify_marker(ip, args.key, "A", results, phase="first-boot")

        # --- Rebuild with marker B (the critical test) ---
        print("\n=== REBUILD (marker B, fresh user_data) ===")
        # CERN's Nova rejects a rebuild body containing "name" ("Hostname
        # cannot be updated"), so we POST the action directly with a minimal
        # body and the explicit microversion that allows user_data.
        t0 = time.monotonic()
        resp = conn.compute.post(
            f"/servers/{server.id}/action",
            json={
                "rebuild": {
                    "imageRef": image.id,
                    "user_data": b64(cloud_init_for("B")),
                }
            },
            headers={"OpenStack-API-Version": f"compute {REBUILD_MICROVERSION}"},
        )
        results.check(
            "rebuild action accepted (mv 2.79, user_data in body)",
            resp.status_code in (200, 202),
            f"HTTP {resp.status_code}: {resp.text[:200]}",
        )
        server = conn.compute.get_server(server.id)
        server = wait_for_status(
            conn,
            server,
            "ACTIVE",
            MAX_WAIT_REBUILD,
            valid_intermediate={"REBUILD", "BUILD"},
            require_seen="REBUILD",  # must actually leave ACTIVE before we trust it
        )
        ip2 = first_ip(server)
        results.check("rebuild: IP preserved", ip2 == ip, f"{ip} -> {ip2}")
        print(f"  ACTIVE, ip={ip2}, rebuild took {time.monotonic() - t0:.0f}s")
        verify_marker(ip2, args.key, "B", results, phase="rebuild")

    finally:
        if server is not None and not args.keep:
            print(f"\nCleaning up {server.id}...")
            try:
                conn.compute.delete_server(server.id)
            except Exception as e:
                print(f"  WARNING: cleanup failed: {e}", file=sys.stderr)
        elif args.keep and server is not None:
            print(f"\n--keep: leaving {server.id} running for inspection")

    # --- Verdict ---
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results.checks if ok)
    total = len(results.checks)
    print(f"Phase 1 result: {passed}/{total} checks passed")
    if results.all_ok():
        print("EXIT CRITERION MET: cloud-init runs on first boot AND re-runs")
        print("with fresh user_data on rebuild.")
        sys.exit(0)
    else:
        print("EXIT CRITERION NOT MET — see FAILures above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
