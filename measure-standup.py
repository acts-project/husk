#!/usr/bin/env python3
"""
Timed VM lifecycle test for OpenStack.

Boots a VM, measures the time to each lifecycle milestone, then cleans up.
Useful for understanding where boot latency comes from before deciding on
custom image building.

Stages measured:
    1. create_server returns          — Nova accepted the request
    2. status BUILD                   — first state transition observed
    3. status ACTIVE                  — Nova considers the VM running
    4. IP address assigned            — network plumbing done
    5. SSH port open                  — guest OS booted enough to listen
    6. cloud-init "done" via SSH      — cloud-init fully finished

The first 3 stages depend on Nova/scheduler/storage. Stages 4-6 depend on the
guest OS and cloud-init configuration.

USAGE:
    python timed-boot.py [--runs N] [--ssh-probe]

If --ssh-probe is omitted, stages 5 and 6 are skipped (no SSH key required).
"""

import argparse
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import openstack

# ---------------------------------------------------------------------------
# Configuration — edit these for your environment
# ---------------------------------------------------------------------------
CLOUD_NAME = "cern"
IMAGE_NAME = "ALMA10 - x86_64"  # e.g. "Ubuntu 24.04 LTS"
FLAVOR_NAME = "m2.small"  # e.g. "m1.small"
NETWORK_NAME = "CERN_NETWORK"  # e.g. "CERN_NETWORK"
KEYPAIR_NAME = "acts-ci"  # set to your keypair name if using --ssh-probe
SSH_USER = "almalinux"  # cloud-image default; varies by distro
TAG = "timed-boot-test"  # metadata + name prefix

# Limits
MAX_WAIT_ACTIVE = 1200  # seconds to wait for ACTIVE
MAX_WAIT_REBUILD = 600  # seconds to wait for rebuild back to ACTIVE
MAX_WAIT_SSH = 300  # seconds after ACTIVE to wait for SSH
POLL_INTERVAL = 1  # seconds between polls
# ---------------------------------------------------------------------------


def fmt(seconds: float) -> str:
    """Format seconds as MM:SS.s."""
    if seconds != seconds:  # NaN
        return "  --  "
    m, s = divmod(seconds, 60)
    return f"{int(m):02d}:{s:05.2f}"


@contextmanager
def timer():
    t0 = time.monotonic()
    yield lambda: time.monotonic() - t0


def get_task_state(server):
    task = getattr(server, "task_state", None)
    if task is not None:
        return task
    return server.to_dict().get("OS-EXT-STS:task_state")


def get_host(server):
    return (
        getattr(server, "compute_host", None)
        or server.to_dict().get("OS-EXT-SRV-ATTR:host")
        or server.to_dict().get("OS-EXT-SRV-ATTR:hypervisor_hostname")
    )


def watch_until(
    conn, server, target_status, deadline_s, on_event, valid_intermediate=None
):
    """
    Poll until server.status == target_status, calling on_event for each
    status/task_state transition observed.

    valid_intermediate: set of status strings that are OK to be in while waiting.
    If status falls outside {target} ∪ valid_intermediate, we error out.
    Used to distinguish "creating, waiting to reach ACTIVE" from "ERROR" etc.
    """
    t0 = time.monotonic()
    last_status = None
    last_task = "__unset__"
    valid = set(valid_intermediate or []) | {target_status, "ERROR"}

    while time.monotonic() - t0 < deadline_s:
        server = conn.compute.get_server(server.id)

        if server.status != last_status:
            on_event("status", server.status, time.monotonic() - t0)
            last_status = server.status

        task = get_task_state(server)
        if task != last_task:
            on_event("task", task, time.monotonic() - t0)
            last_task = task

        if server.status == target_status:
            return server
        if server.status == "ERROR":
            raise RuntimeError(f"Server entered ERROR: {server.fault}")
        if server.status not in valid:
            # Unexpected status — log and continue but flag it
            print(f"  !! unexpected status during wait: {server.status}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Timed out waiting for {target_status} (last={last_status})")


def first_ip(server, network_name):
    addrs = server.addresses.get(network_name, [])
    return addrs[0]["addr"] if addrs else None


def wait_for_ip(conn, server, network_name, deadline_s):
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        server = conn.compute.get_server(server.id)
        ip = first_ip(server, network_name)
        if ip:
            return server, ip
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"No IP on {network_name} within {deadline_s}s")


def wait_for_ssh_port(ip, deadline_s):
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        try:
            with socket.create_connection((ip, 22), timeout=3):
                return
        except OSError:
            time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"SSH port not open within {deadline_s}s")


def wait_for_cloudinit(ip, deadline_s):
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "LogLevel=ERROR",
        f"{SSH_USER}@{ip}",
        "cloud-init status --wait",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=deadline_s)
        return result.returncode == 0, (result.stdout or result.stderr).strip()
    except subprocess.TimeoutExpired:
        raise TimeoutError("cloud-init status --wait did not complete")


def make_event_recorder(timings, label_prefix):
    """
    Return an on_event callback that records every status/task transition
    into the `timings` dict with keys prefixed by `label_prefix`.

    Example keys:
      "create.status_build", "create.task_networking",
      "rebuild1.status_rebuild", "rebuild1.task_rebuild_spawning"
    """

    def on_event(kind, value, t):
        if kind == "status":
            key = f"{label_prefix}.status_{value.lower()}"
            tag = "status     "
        else:
            value_str = value if value is not None else "none"
            key = f"{label_prefix}.task_{value_str.lower()}"
            tag = "task_state "
        timings[key] = t
        value_display = value if value is not None else "<none>"
        print(f"  [{fmt(t)}] {tag} -> {value_display}")

    return on_event


def measure_ssh_stages(timings, ip, label_prefix, base_offset):
    """SSH-port-open and cloud-init-done, in absolute time from cycle start."""
    with timer() as elapsed:
        wait_for_ssh_port(ip, MAX_WAIT_SSH)
        ssh_t = base_offset + elapsed()
        timings[f"{label_prefix}.ssh_open"] = ssh_t
        print(f"  [{fmt(ssh_t)}] SSH port open")

    with timer() as elapsed:
        ok, msg = wait_for_cloudinit(ip, MAX_WAIT_SSH)
        ci_t = ssh_t + elapsed()
        timings[f"{label_prefix}.cloudinit_done"] = ci_t
        status_str = "done" if ok else f"FAILED: {msg}"
        print(f"  [{fmt(ci_t)}] cloud-init {status_str}")


def do_create(conn, image, flavor, network, with_ssh_probe, timings, label):
    """Create a server, time the lifecycle, return the server object."""
    print(f"  --- {label} (create) ---")

    with timer() as elapsed:
        create_kwargs = dict(
            name=f"{TAG}-{int(time.time())}",
            image_id=image.id,
            flavor_id=flavor.id,
            networks=[{"uuid": network.id}],
            metadata={"managed-by": TAG},
        )
        if KEYPAIR_NAME:
            create_kwargs["key_name"] = KEYPAIR_NAME
        server = conn.compute.create_server(**create_kwargs)
        timings[f"{label}.create_returned"] = elapsed()
        print(
            f"  [{fmt(timings[f'{label}.create_returned'])}] create_server returned ({server.id})"
        )

    on_event = make_event_recorder(timings, label)
    server = watch_until(
        conn,
        server,
        target_status="ACTIVE",
        deadline_s=MAX_WAIT_ACTIVE,
        on_event=on_event,
        valid_intermediate={"BUILD"},
    )

    host = get_host(server)
    if host:
        timings[f"{label}._host"] = host
        print(f"  (landed on hypervisor: {host})")

    # IP assigned
    active_t = timings.get(f"{label}.status_active", 0)
    with timer() as elapsed:
        server, ip = wait_for_ip(conn, server, NETWORK_NAME, deadline_s=60)
        ip_t = active_t + elapsed()
        timings[f"{label}.ip_assigned"] = ip_t
        print(f"  [{fmt(ip_t)}] IP assigned: {ip}")

    if with_ssh_probe:
        measure_ssh_stages(timings, ip, label, base_offset=ip_t)

    return server, ip


def do_rebuild(conn, server, image, with_ssh_probe, timings, label):
    """Rebuild an existing server, time the lifecycle."""
    print(f"  --- {label} (rebuild) ---")

    rebuild_kwargs = dict(image=image.id)
    # Note: passing user-data on rebuild requires microversion 2.57+; not all
    # clouds support it. We don't pass new user-data here — just measure the
    # baseline rebuild cost.

    with timer() as elapsed:
        # CERN's Nova rejects any rebuild request that includes a "name"
        # field — even if the value is unchanged — with "Hostname cannot
        # be updated". The SDK's rebuild_server helper always passes name
        # on older versions, so we bypass it and POST the action directly
        # with a minimal body.
        #
        # Nova API: POST /servers/{id}/action
        # Body:    {"rebuild": {"imageRef": <image-id>}}
        url = f"/servers/{server.id}/action"
        body = {"rebuild": {"imageRef": image.id}}
        response = conn.compute.post(url, json=body)
        if response.status_code not in (200, 202):
            raise RuntimeError(
                f"rebuild failed: {response.status_code} {response.text}"
            )
        timings[f"{label}.rebuild_returned"] = elapsed()
        print(
            f"  [{fmt(timings[f'{label}.rebuild_returned'])}] rebuild action accepted"
        )

    on_event = make_event_recorder(timings, label)
    # During rebuild, status goes ACTIVE -> REBUILD -> ACTIVE
    server = watch_until(
        conn,
        server,
        target_status="ACTIVE",
        deadline_s=MAX_WAIT_REBUILD,
        on_event=on_event,
        valid_intermediate={"REBUILD", "BUILD"},  # some clouds use BUILD here
    )

    # IP should be the same; re-fetch to confirm
    ip = first_ip(server, NETWORK_NAME)
    if ip:
        print(f"  (IP preserved: {ip})")

    active_t = timings.get(f"{label}.status_active", 0)
    if with_ssh_probe and ip:
        measure_ssh_stages(timings, ip, label, base_offset=active_t)

    return server, ip


def run_once(conn, image, flavor, network, with_ssh_probe, rebuild_cycles):
    """One full create + optional N rebuild cycles. Returns timings dict."""
    timings = {}
    server = None
    try:
        # Initial create
        server, ip = do_create(
            conn, image, flavor, network, with_ssh_probe, timings, label="create"
        )

        # Optional rebuild cycles
        for i in range(rebuild_cycles):
            label = f"rebuild{i + 1}"
            server, ip = do_rebuild(
                conn, server, image, with_ssh_probe, timings, label=label
            )

        return timings

    finally:
        if server is not None:
            print(f"  Cleaning up {server.id}...")
            try:
                conn.compute.delete_server(server.id)
            except Exception as e:
                print(f"  WARNING: cleanup failed: {e}", file=sys.stderr)


def summarize(all_timings):
    """Summary across runs, grouped by cycle (create / rebuild1 / rebuild2...)."""
    runs = [t for t in all_timings if t]
    if not runs:
        return

    # Discover all label prefixes that appeared
    labels = []
    for t in runs:
        for k in t:
            if "." not in k or k.startswith("_"):
                continue
            label = k.split(".", 1)[0]
            if label not in labels:
                labels.append(label)

    for label in labels:
        # Stages for this label, in insertion order across runs
        stages = []
        for t in runs:
            for k in t:
                if not k.startswith(label + "."):
                    continue
                stage = k.split(".", 1)[1]
                if stage.startswith("_"):
                    continue
                if stage not in stages:
                    stages.append(stage)

        print(f"\n=== Summary: {label} cycle ===")
        header = f"{'stage':<28} " + " ".join(
            f"run{i + 1:>3}" for i in range(len(runs))
        )
        print(header)
        print("-" * len(header))
        for stage in stages:
            key = f"{label}.{stage}"
            row = f"{stage:<28} "
            row += " ".join(f"{fmt(t.get(key, float('nan'))):>6}" for t in runs)
            print(row)

    # Hosts
    print("\nHypervisors:")
    for i, t in enumerate(runs, 1):
        host = t.get("create._host", "<unknown>")
        print(f"  run{i}: {host}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of independent test runs (each does create + N rebuilds + delete)",
    )
    parser.add_argument(
        "--ssh-probe",
        action="store_true",
        help="Also measure SSH and cloud-init stages",
    )
    parser.add_argument(
        "--rebuild-cycles",
        type=int,
        default=0,
        help="After initial create, perform this many rebuilds (default: 0)",
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

    print(f"Image:   {image.name}")
    print(
        f"Flavor:  {flavor.name}  ({flavor.vcpus} vCPU, {flavor.ram} MiB RAM, {flavor.disk} GiB disk)"
    )
    print(f"Network: {network.name}")
    print(f"Runs:    {args.runs}, rebuild cycles per run: {args.rebuild_cycles}")
    if args.ssh_probe and not KEYPAIR_NAME:
        sys.exit("--ssh-probe requires KEYPAIR_NAME to be set in the config block")

    all_timings = []
    for i in range(args.runs):
        print(f"\n=== Run {i + 1}/{args.runs} ===")
        try:
            timings = run_once(
                conn, image, flavor, network, args.ssh_probe, args.rebuild_cycles
            )
            all_timings.append(timings)
        except Exception as e:
            print(f"  Run failed: {e}", file=sys.stderr)
            all_timings.append({})

    summarize(all_timings)


if __name__ == "__main__":
    main()
