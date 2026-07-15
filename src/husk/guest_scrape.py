"""Scrape a libvirt guest's node_exporter through the host, from huskd itself.

A libvirt runner slot sits on a private NAT net (192.168.122.0/24): only its
hypervisor can reach the guest's node_exporter on :9100. Something has to bridge
that. huskd does it, over the SSH channel it *already* holds to every host (the
one `libvirt_backend._ssh` uses for qemu-img/genisoimage):

    [central Prometheus] --GET /slot/<pool>/<slot>/metrics--> [huskd] --ssh--> guest:9100

Why huskd and not a proxy deployed on each hypervisor (the original O4 plan):

  * **No new network path.** Prometheus already scrapes huskd for `/metrics`, so
    that route is proven. A per-host proxy would need Prometheus → hypervisor:9101
    opened up on every host — network-admin work, per host, forever.
  * **No per-host orchestration.** Nothing to install, unit-file, or upgrade on a
    hypervisor. Adding a host to the fleet stays a pure config change.

The cost, stated plainly: huskd is now in the metrics *data* path for libvirt (it
is not, and must not become, one for OpenStack — those guests are scraped
directly). So a huskd outage gaps libvirt guest metrics, and Prometheus's `up`
for those targets folds together "guest is sick" and "huskd/SSH is sick". That
was an accepted trade: the fleet is small, and the alternative cost recurring
network+deploy overhead on every host.

Consequences that shape the code below:

  * **Never block the event loop.** Quart serves this on the same loop as
    /status, /events and the dashboard, so the SSH exec is an async subprocess
    and every call is hard-bounded by a timeout. A wedged host degrades one
    scrape, not the control plane.
  * **Multiplex the SSH connection.** A fresh TCP+auth handshake per scrape
    (every 15s, per slot) would be wasteful and slow; ControlMaster/ControlPersist
    reuses one connection per host.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# A unix-domain socket path (ControlPath after %C expansion) must fit the kernel's
# sun_path limit — 104 bytes on macOS, 108 on Linux. ssh appends a 40-char %C hash
# to our dir, so the DIR must stay short. macOS's default $TMPDIR
# (/var/folders/.../T/, ~50 chars) blows the budget on its own, so we anchor the
# control sockets under a deliberately short base instead of tempfile's default.
_SUN_PATH_MAX = 104  # the smaller of the two, so it's safe on both
_CONTROL_HASH_LEN = 43  # %C is a 40-char hex hash; a few bytes of headroom


def _short_tmp_base() -> str:
    """Shortest writable temp dir for SSH control sockets. Prefer /tmp (5 chars)
    over $TMPDIR, which on macOS is far too long for a sun_path (see above)."""
    for base in ("/tmp", tempfile.gettempdir()):
        if os.path.isdir(base) and os.access(base, os.W_OK):
            return base
    return tempfile.gettempdir()


EXPORTER_PORT = (
    9100  # node_exporter in the guest (images/files/husk-node-exporter.service)
)

_CONNECT_TIMEOUT_S = 5
_PERSIST = "60s"  # keep the multiplexed SSH connection warm between scrapes


class GuestScrapeError(RuntimeError):
    """A guest's metrics could not be fetched (host down, slot rebooting, no
    exporter). Surfaces as a 5xx, which Prometheus records as `up == 0`."""


class GuestScraper:
    """Fetches `http://<guest-ip>:9100/metrics` from inside a hypervisor.

    `ssh_targets` maps (pool/backend name, host name) → the host's ssh_target, i.e.
    exactly what `HostConfig.ssh_target` already carries. An empty target means the
    host is local (huskd runs on it), so we curl the guest directly — mirroring
    `libvirt_backend._ssh`.
    """

    def __init__(
        self,
        ssh_targets: dict[tuple[str, str], str],
        *,
        timeout: float = 10.0,
    ) -> None:
        self._ssh_targets = ssh_targets
        self._timeout = timeout
        # Multiplexed-connection sockets. Private dir (mkdtemp is 0700): the control
        # socket grants use of an authenticated SSH connection to a hypervisor, so it
        # must not be world-accessible. Anchored under a SHORT base so the socket
        # path stays within the sun_path limit (see _short_tmp_base). Cleaned up in
        # close().
        self._control_dir = tempfile.mkdtemp(prefix="hsk-", dir=_short_tmp_base())
        # Guard: if even the short path can't fit ssh's %C socket name, multiplexing
        # is impossible on this box. Degrade to a fresh connection per scrape rather
        # than fail every scrape — correctness over the handshake saving.
        self._multiplex = (
            len(self._control_dir) + 1 + _CONTROL_HASH_LEN <= _SUN_PATH_MAX
        )
        if not self._multiplex:
            log.warning(
                "ssh control path %r too long for socket multiplexing; scraping "
                "without connection reuse",
                self._control_dir,
            )

    def knows(self, backend: str, host: str) -> bool:
        return (backend, host) in self._ssh_targets

    async def fetch(self, backend: str, host: str, ip: str) -> bytes:
        """The guest's raw Prometheus exposition text. Raises GuestScrapeError."""
        try:
            target = self._ssh_targets[(backend, host)]
        except KeyError:
            raise GuestScrapeError(f"no ssh route to host {host!r} of pool {backend!r}")

        # `curl` runs ON the hypervisor, so the connection to :9100 originates from
        # the libvirt bridge — which is exactly the source the guest's nftables
        # allowlist admits (the pool's `scrape_cidr`, e.g. 192.168.122.1/32).
        url = f"http://{ip}:{EXPORTER_PORT}/metrics"
        remote = f"curl -sS -f --max-time {int(self._timeout)} {url}"
        if target:
            argv = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={_CONNECT_TIMEOUT_S}",
            ]
            if self._multiplex:
                # Reuse one authenticated connection per host across scrapes.
                argv += [
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    f"ControlPath={self._control_dir}/%C",
                    "-o",
                    f"ControlPersist={_PERSIST}",
                ]
            argv += [target, remote]
        else:
            argv = ["bash", "-c", remote]  # local hypervisor

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise GuestScrapeError(f"could not exec ssh: {e}") from e

        try:
            # Hard outer bound. The inner --max-time/ConnectTimeout should fire
            # first; this catches the case where ssh itself wedges, so a bad host
            # can never pin a request (or a thread) open indefinitely.
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout + _CONNECT_TIMEOUT_S + 2
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise GuestScrapeError(f"scrape of {ip} via {host} timed out") from None

        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip() or f"exit {proc.returncode}"
            raise GuestScrapeError(f"scrape of {ip} via {host} failed: {msg}")
        return out

    def close(self) -> None:
        shutil.rmtree(self._control_dir, ignore_errors=True)

    @property
    def control_dir(self) -> Path:
        return Path(self._control_dir)
