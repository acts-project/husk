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

This module is thin on purpose: the two documents it renders live beside it as
templates (files/cloud-init.yaml.j2, files/husk-egress.nft.j2), because they are
YAML and nftables rather than Python, and reviewing them means reading them at
their own indentation. Everything the guest does with what they lay down — the
unit that reads the JIT config, the uid-1000 runner user it runs as — lives in
images/files/.
"""

from __future__ import annotations

import base64
import importlib.resources
import json
import re
import textwrap

import jinja2

# Column the nft ruleset sits at inside the YAML `content: |` block scalar. The
# ruleset is rendered on its own, at its own indentation, and indented once on
# the way in — so the conditional fragments inside it are written at nftables'
# indentation rather than nftables' plus YAML's.
_YAML_BLOCK_INDENT = " " * 6


def _yamlstr(value: object) -> str:
    """A value as a quoted YAML scalar. JSON string syntax is a subset of YAML's
    double-quoted style, so `json.dumps` is both the correct escaping and the
    obvious one. Used for every interpolated *value* (as opposed to structure),
    so a proxy URL or an env var containing a quote cannot end the scalar early
    and silently reshape the document."""
    return json.dumps(str(value))


# StrictUndefined: a typo'd or forgotten variable must raise here, not render as
# an empty string. This document configures a firewall — a silently-missing value
# is exactly the failure that would open a hole rather than close one.
# trim_blocks/lstrip_blocks let `{% if %}` sit on its own line at its natural
# indentation without leaving a blank line or stray spaces behind.
_TEMPLATE_DIR = importlib.resources.files("husk") / "files"
_ENV = jinja2.Environment(
    loader=jinja2.PackageLoader("husk", "files"),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined,
    autoescape=False,  # YAML and nftables; HTML escaping would corrupt both
)
_ENV.filters["yamlstr"] = _yamlstr


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
    only the kmod load and the CDI spec are hardware-dependent, and those cannot
    be. Failures there are deliberately LOUD (no `|| true`) so a broken driver
    surfaces as a failed nvidia-smi in the job rather than a silent no-GPU.

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
    a job's `dnf install` needs and which the firewall otherwise blackholes. They are
    resolved in-guest on every cycle rather than pinned as CIDRs, because the hosts
    worth listing are load-balanced CERN aliases whose A-records rotate; DNS stays
    allowed after lockdown precisely so that resolve can happen. Empty renders
    byte-identically to a slot without it.

    `container_env` exports variables into every job container. It exists because
    job images are shared with GitHub-hosted runners and so cannot carry anything
    that is only true on a husk slot — the environment can. Empty renders
    byte-identically to a slot without it."""
    ruleset = _ENV.get_template("husk-egress.nft.j2").render(
        scrape_cidr=scrape_cidr,
        # Inside an `inet` table `ip saddr` matches v4 only; picking the wrong one
        # would leave the accept unmatched and the port silently closed.
        saddr="ip6 saddr" if ":" in scrape_cidr else "ip saddr",
        cvmfs=bool(cvmfs_repos),
        egress_allow=bool(egress_allow_hosts),
    )
    return (
        _ENV.get_template("cloud-init.yaml.j2")
        .render(
            jit=jit_blob,
            gpu=gpu,
            scrape_cidr=scrape_cidr,
            # rstrip: the ruleset's trailing newline would double the blank line
            # the template already has before `runcmd:`.
            egress_ruleset=textwrap.indent(ruleset, _YAML_BLOCK_INDENT).rstrip("\n"),
            cvmfs_repos=cvmfs_repos,
            cvmfs_proxy=cvmfs_proxy,
            cvmfs_quota_mb=cvmfs_quota_mb,
            cvmfs_volumes=[f"/cvmfs/{r}:/cvmfs/{r}" for r in cvmfs_repos],
            # An empty repo list turns CVMFS off *entirely*, proxy or no proxy:
            # a hole opened for a client that mounts nothing is pure attack
            # surface. `cvmfs_repos` is the single switch; everything else is
            # detail hanging off it.
            cvmfs_proxy_hosts=_proxy_hosts(cvmfs_proxy) if cvmfs_repos else [],
            egress_allow_hosts=egress_allow_hosts,
            container_env=(
                (*_PODMAN_DEFAULT_ENV, *container_env) if container_env else ()
            ),
        )
        .encode()
    )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
