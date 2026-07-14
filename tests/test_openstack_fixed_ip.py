"""_fixed_ip extracts the guest's fixed IPv4 from a Nova server's addresses."""

from __future__ import annotations

from types import SimpleNamespace

from husk.openstack_backend import _fixed_ip


def _server(addresses):
    return SimpleNamespace(addresses=addresses)


def test_picks_fixed_ipv4_over_floating_and_v6():
    server = _server(
        {
            "CERN_NETWORK": [
                {
                    "addr": "2001:1458:d00:b::100",
                    "version": 6,
                    "OS-EXT-IPS:type": "fixed",
                },
                {"addr": "188.184.1.2", "version": 4, "OS-EXT-IPS:type": "fixed"},
                {"addr": "10.0.0.9", "version": 4, "OS-EXT-IPS:type": "floating"},
            ]
        }
    )
    assert _fixed_ip(server) == "188.184.1.2"


def test_none_when_no_fixed_v4():
    assert _fixed_ip(_server({"net": [{"addr": "::1", "version": 6}]})) is None
    assert _fixed_ip(_server({})) is None
    assert _fixed_ip(SimpleNamespace(addresses=None)) is None
