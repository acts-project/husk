"""Cloud-init rendering — the per-cycle layer on top of a golden image.

Every pool boots a husk golden image (images/build.sh): rootless Podman + the
Docker→Podman compatibility shim, the runner binary and its systemd units,
node_exporter, and the CernVM-FS client are all baked in. Cloud-init therefore
does ONLY what cannot be baked — the per-cycle JIT config, the egress firewall
(the one tunable security policy, deliberately left out of the image), GPU
runtime activation against hardware that does not exist at image-build time, and
starting the runner.

The baked `husk-runner.service` is single-use: when run.sh exits (job done) or
fails, it powers the slot off via `husk-poweroff.service`, which the controller
observes as SHUTOFF → recycle. The coarse egress firewall (allow the public
internet, deny CERN-internal CIDRs) is applied just before the runner starts, so
the untrusted job — and nothing else — runs locked down.

`@@JIT@@` is substituted per recycle. Everything the guest does with it — the
unit that reads it, the uid-1000 runner user it runs as — lives in images/files/.
"""

from __future__ import annotations

import base64
import importlib.resources
import re
import textwrap

# The egress ruleset lives in its own file rather than inline in the template
# below: it is the security-critical half of this module, it is nftables and not
# YAML, and authoring it at its own indentation means the conditional fragments
# spliced into it are written at nft's indentation instead of nft's plus the
# YAML block scalar's. Read once at import so a missing/miss-packaged file fails
# at startup rather than on the first slot rebuild.
#
# The ruleset is idempotent by construction — it drops and recreates husk's OWN
# table — because cloud-init's runcmd re-runs on every rebuild, and reapplying
# must not disturb any other table on the guest.
_EGRESS_RULESET = (
    importlib.resources.files("husk")
    .joinpath("files/husk-egress.nft")
    .read_text(encoding="utf-8")
)

# Column the ruleset sits at inside the `content: |` block scalar.
_YAML_BLOCK_INDENT = " " * 6

RUNNER_CLOUD_INIT = r"""#cloud-config
# Golden image: everything slow and static is in the image already, so this is
# intentionally minimal. See render_cloud_init.

write_files:
  - path: /var/lib/husk/jitconfig
    permissions: '0600'
    content: "@@JIT@@"
@@CONTAINER_ENV@@@@CVMFS_WRITE_FILES@@
  # Coarse egress firewall ruleset — the tunable security policy, kept in
  # cloud-init (NOT baked) so a pool can change it without rebuilding the image.
  - path: /etc/nftables/husk-egress.nft
    content: |
@@EGRESS_RULESET@@

runcmd:
  # jitconfig is written root-owned above; the runner service runs as `runner`.
  - mkdir -p /var/lib/husk
  - chown -R runner:runner /var/lib/husk
  # GPU runtime activation is spliced here for GPU pools (before the firewall).
  # Lock down egress just before the (untrusted) runner starts.
  - /usr/sbin/nft -f /etc/nftables/husk-egress.nft
@@ALLOW_SETUP@@@@CVMFS_SETUP@@  # The runner unit is baked but NOT enabled for boot; cloud-init starts it each
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

# GPU runtime activation: load the precompiled open kmod against the passed-through
# GPU and (re)generate the CDI spec the rootless runner uses to inject the GPU into
# job containers. The driver and container-toolkit packages are baked; this half is
# hardware-dependent (no GPU exists at image build time), so it runs every boot.
# Failures are left LOUD (no `|| true`) so a broken driver surfaces as a failed
# nvidia-smi in the job rather than a silent no-GPU.
_GPU_RUNTIME = r"""  # GPU runtime activation (every boot — the kmod load + CDI spec are
  # hardware-dependent and cannot be baked into the image).
  - modprobe nvidia
  - nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
"""

# Splice point for the GPU runtime block: the line right after it is the firewall
# apply, so activation runs with the network still fully open.
_GPU_ANCHOR = "  # Lock down egress just before the (untrusted) runner starts.\n"


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
# The client + autofs are baked into the golden image (images/build.sh), so
# cloud-init lays only the per-pool dynamic layer — default.local (proxy + repos +
# quota), the containers.conf.d drop-in that binds each repo into every job
# container, the in-guest firewall hole for the proxy, and the eager-mount of each
# repo. Empty repo list → every CVMFS placeholder resolves away and the output is
# exactly the non-CVMFS render.


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


# podman's shipped default for this key. A containers.conf.d drop-in REPLACES a
# list-valued key rather than appending to it, so writing `env` without carrying
# the default forward would silently unset TERM for every job container — the kind
# of change that shows up much later as a tool rendering escape codes into a log.
_PODMAN_DEFAULT_ENV = ("TERM=xterm",)


def _container_env_write_files(env: tuple[str, ...]) -> str:
    """A containers.conf.d drop-in exporting `env` into EVERY job container.

    This is the seam that lets a fleet change what a job sees without changing the
    image the job runs. The ACTS images are shared between GitHub-hosted runners
    and husk slots, so anything baked into them would have to be true in both
    places; an environment variable is true only here. podman applies
    containers.conf.d to both the CLI shim and the API socket, so this reaches
    `container:` jobs and step-level `docker run` alike — the same mechanism, and
    the same validated path, as the CVMFS binds.

    The variables are husk's half of a contract: husk states a fact about where
    the slot is (e.g. which package mirror is near it), and the workflow decides
    whether it cares. Nothing here knows what any given variable means."""
    if not env:
        return ""
    items = ", ".join(f'"{v}"' for v in (*_PODMAN_DEFAULT_ENV, *env))
    return (
        "  # Environment for every job container (see [pool.container] env). The\n"
        "  # image is shared with GitHub-hosted runners, so fleet-specific facts\n"
        "  # arrive as environment, not as image content.\n"
        "  - path: /etc/containers/containers.conf.d/20-env.conf\n"
        "    content: |\n"
        "      [containers]\n"
        f"      env = [{items}]\n"
    )


def _egress_allow_setup(hosts: tuple[str, ...]) -> str:
    """runcmd step, spliced AFTER the firewall apply: resolve the allowlisted
    hostnames in-guest and add their IPs to the `egress_allow` set.

    Resolved per cycle rather than pinned as CIDRs because the hosts this exists
    for are load-balanced CERN aliases — linuxsoft.cern.ch is a rotating A-record
    set, so a literal address would work until it silently didn't. Same reasoning
    (and same mechanism) as the CVMFS proxy hole; DNS stays allowed after lockdown
    precisely so this resolve can happen here.

    Failure is deliberately soft: `[ -n "$ips" ]` means an unresolvable host opens
    no hole and the slot still boots, degrading to "that package mirror is
    unreachable" rather than "the slot never registers"."""
    if not hosts:
        return ""
    return (
        "  # Open the firewall to the allowlisted hosts (CERN package mirrors and\n"
        "  # the like): resolve in-guest, since these are load-balanced aliases\n"
        "  # whose addresses rotate, and add the IPs to the nft set.\n"
        "  - |\n"
        f"    ips=$(getent ahostsv4 {' '.join(hosts)} | awk '{{print $1}}' "
        "| sort -u | paste -sd, -)\n"
        '    [ -n "$ips" ] && nft add element inet husk egress_allow "{ $ips }"\n'
    )


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


def _egress_ruleset(
    *,
    scrape_cidr: str,
    cvmfs: bool,
    egress_allow: bool,
) -> str:
    """Fill files/husk-egress.nft. Returned at the file's own indentation; the
    caller indents it into the YAML block scalar.

    Each conditional resolves to nothing when its feature is off, so a plain slot
    gets exactly the ruleset as authored — that is what makes the features
    fail-closed rather than merely inert."""
    if scrape_cidr:
        saddr = "ip6 saddr" if ":" in scrape_cidr else "ip saddr"
        ingress = _METRICS_INGRESS.replace("@@SADDR@@", saddr).replace(
            "@@SCRAPE_CIDR@@", scrape_cidr
        )
    else:
        ingress = ""

    # The two named holes are the same shape: a set declared in the table, an
    # accept placed BEFORE the CERN-internal drops (nftables takes the first
    # match, so an accept after them would parse and do nothing), and a runcmd
    # step that fills the set from an in-guest resolve.
    cvmfs_set = "  set cvmfs_proxy { type ipv4_addr; }\n" if cvmfs else ""
    cvmfs_rule = (
        (
            "    # Allow the CVMFS HTTP proxy. Its CERN squids live in the\n"
            "    # CERN-internal ranges dropped below, so this accept must\n"
            "    # precede them; the set is populated in runcmd after an\n"
            "    # in-guest resolve of the proxy hostnames.\n"
            "    ip daddr @cvmfs_proxy accept\n\n"
        )
        if cvmfs
        else ""
    )
    allow_set = "  set egress_allow { type ipv4_addr; }\n" if egress_allow else ""
    allow_rule = (
        (
            "    # Allow the operator-listed hosts. They live in the\n"
            "    # CERN-internal ranges dropped below, so this accept must\n"
            "    # precede them; the set is populated in runcmd after an\n"
            "    # in-guest resolve.\n"
            "    ip daddr @egress_allow accept\n\n"
        )
        if egress_allow
        else ""
    )
    return (
        _EGRESS_RULESET.replace("@@CVMFS_SET@@", cvmfs_set)
        .replace("@@CVMFS_PROXY@@", cvmfs_rule)
        .replace("@@ALLOW_SET@@", allow_set)
        .replace("@@ALLOW_RULE@@", allow_rule)
        .replace("@@METRICS_INGRESS@@", ingress)
    )


def render_cloud_init(
    jit_blob: str,
    *,
    gpu: bool = False,
    scrape_cidr: str = "",
    cvmfs_repos: tuple[str, ...] = (),
    cvmfs_proxy: str = "",
    cvmfs_quota_mb: int = 4000,
    egress_allow_hosts: tuple[str, ...] = (),
    container_env: tuple[str, ...] = (),
) -> bytes:
    """Render the cloud-init user-data for one slot.

    `gpu=True` activates the GPU on every boot (`modprobe` + `nvidia-ctk cdi
    generate`). The driver and container toolkit are baked into the golden image;
    only the kmod load and the CDI spec are hardware-dependent, and those cannot be.

    `scrape_cidr` turns on in-guest metrics: it opens `:9100` to that source only
    and starts the (baked) node_exporter. **Opt-in and fail-closed** — unset means
    no ingress rule and no exporter running, i.e. nothing listening, which is why
    a pool whose scraper source isn't known yet can simply leave it out.

    `cvmfs_repos`/`cvmfs_proxy`/`cvmfs_quota_mb` turn on CernVM-FS: cloud-init writes
    the client config + a per-repo containers.conf.d bind drop-in, opens the proxy
    hole in the firewall, and eager-mounts each repo before the runner starts. Also
    fail-closed — empty `cvmfs_repos` renders identically to a non-CVMFS slot.

    `egress_allow_hosts` punches named holes in the coarse egress firewall for hosts
    inside the dropped CERN-internal ranges — CERN's package mirrors above all, which
    a job's `dnf install` needs and which the firewall otherwise blackholes. Empty
    renders byte-identically to a slot without it.

    `container_env` exports variables into every job container. It exists because
    job images are shared with GitHub-hosted runners and so cannot carry anything
    that is only true on a husk slot — the environment can. Empty renders
    byte-identically to a slot without it."""
    ruleset = _egress_ruleset(
        scrape_cidr=scrape_cidr,
        cvmfs=bool(cvmfs_repos),
        egress_allow=bool(egress_allow_hosts),
    )
    cvmfs_wf = cvmfs_setup = ""
    if cvmfs_repos:
        cvmfs_wf = _cvmfs_write_files(cvmfs_repos, cvmfs_proxy, cvmfs_quota_mb)
        cvmfs_setup = _cvmfs_setup(cvmfs_repos, cvmfs_proxy)

    template = RUNNER_CLOUD_INIT
    if gpu:
        template = template.replace(_GPU_ANCHOR, _GPU_RUNTIME + _GPU_ANCHOR, 1)
    return (
        template.replace(
            # rstrip: the ruleset's own trailing newline would double the blank
            # line the template already has before `runcmd:`.
            "@@EGRESS_RULESET@@",
            textwrap.indent(ruleset, _YAML_BLOCK_INDENT).rstrip("\n"),
        )
        .replace("@@METRICS_START@@\n", _METRICS_START if scrape_cidr else "")
        .replace("@@CVMFS_WRITE_FILES@@", cvmfs_wf)
        .replace("@@CVMFS_SETUP@@", cvmfs_setup)
        .replace("@@ALLOW_SETUP@@", _egress_allow_setup(egress_allow_hosts))
        .replace("@@CONTAINER_ENV@@", _container_env_write_files(container_env))
        .replace("@@JIT@@", jit_blob)
        .encode()
    )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
