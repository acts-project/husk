"""The option combinations the golden files in tests/golden/ cover.

Shared by the test and by tests/golden/regen.py, so a case can never be rendered
one way for the fixture and another way for the assertion. Every knob appears
alone (to see its isolated contribution to the diff) and once all together (to
catch interactions between the splice points)."""

from __future__ import annotations

CASES: dict[str, dict] = {
    "plain": {},
    "gpu": {"gpu": True},
    "metrics": {"scrape_cidr": "10.0.0.0/8"},
    "metrics_v6": {"scrape_cidr": "2001:db8::/32"},
    "cvmfs": {
        "cvmfs_repos": ("sft.cern.ch", "atlas.cern.ch"),
        "cvmfs_proxy": "http://squid1.cern.ch:3128;http://squid2.cern.ch:3128",
        "cvmfs_quota_mb": 6000,
    },
    # DIRECT opens no proxy hole — the one CVMFS case with no firewall set.
    "cvmfs_direct": {"cvmfs_repos": ("sft.cern.ch",), "cvmfs_proxy": "DIRECT"},
    "allow": {"egress_allow_hosts": ("linuxsoft.cern.ch", "cvmfs-stratum-one.cern.ch")},
    "env": {"container_env": ("APT_MIRROR=http://mirror.cern.ch", "FOO=bar")},
    "everything": {
        "gpu": True,
        "scrape_cidr": "192.168.122.1/32",
        "cvmfs_repos": ("sft.cern.ch",),
        "cvmfs_proxy": "http://squid.cern.ch:3128",
        "cvmfs_quota_mb": 8000,
        "egress_allow_hosts": ("linuxsoft.cern.ch",),
        "container_env": ("APT_MIRROR=http://mirror.cern.ch",),
    },
}
