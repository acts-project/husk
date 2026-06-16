"""Unit tests for the pure libvirt builders (no hypervisor needed)."""

from __future__ import annotations

import pytest

from husk import libvirt_xml as lx


# --------------------------------------------------------------- status mapping
@pytest.mark.parametrize(
    "state, expected",
    [
        (lx.DOM_RUNNING, "ACTIVE"),
        (lx.DOM_PAUSED, "ACTIVE"),
        (lx.DOM_PMSUSPENDED, "ACTIVE"),
        (lx.DOM_BLOCKED, "ACTIVE"),
        (lx.DOM_CRASHED, "ERROR"),
        (lx.DOM_SHUTOFF, "SHUTOFF"),
        (lx.DOM_SHUTDOWN, "SHUTOFF"),
        (lx.DOM_NOSTATE, "SHUTOFF"),
    ],
)
def test_domain_status(state, expected):
    status, task = lx.domain_status(state)
    assert status == expected
    assert task is None  # libvirt has no async provisioning task


# ------------------------------------------------------------------- slot-units
def test_host_units_gpu():
    assert lx.host_units(["0000:01:00.0", "0000:02:00.0"], 99) == [
        "0000:01:00.0",
        "0000:02:00.0",
    ]


def test_host_units_cpu_default_one():
    assert lx.host_units([], 1) == ["cpu0"]
    assert lx.host_units([], 3) == ["cpu0", "cpu1", "cpu2"]


def test_is_gpu_unit():
    assert lx.is_gpu_unit("0000:01:00.0")
    assert not lx.is_gpu_unit("cpu0")


# ------------------------------------------------------------------- placement
def _hosts():
    return [
        lx.HostUnits("a", ["0000:01:00.0", "0000:02:00.0"]),
        lx.HostUnits("b", ["cpu0", "cpu1"]),
    ]


def test_place_first_free_deterministic():
    assert lx.place(_hosts(), set()) == ("a", "0000:01:00.0")


def test_place_skips_occupied_gpu_no_double_assign():
    occupied = {("a", "0000:01:00.0")}
    assert lx.place(_hosts(), occupied) == ("a", "0000:02:00.0")


def test_place_spills_to_next_host():
    occupied = {("a", "0000:01:00.0"), ("a", "0000:02:00.0")}
    assert lx.place(_hosts(), occupied) == ("b", "cpu0")


def test_place_full_pool_returns_none():
    occupied = {
        ("a", "0000:01:00.0"),
        ("a", "0000:02:00.0"),
        ("b", "cpu0"),
        ("b", "cpu1"),
    }
    assert lx.place(_hosts(), occupied) is None


def test_free_unit_count():
    assert lx.free_unit_count(_hosts(), set()) == 4
    assert lx.free_unit_count(_hosts(), {("a", "0000:01:00.0")}) == 3


def test_cpu_max_slots_respected():
    hosts = [lx.HostUnits("c", lx.host_units([], 2))]
    occupied = {("c", "cpu0"), ("c", "cpu1")}
    assert lx.place(hosts, occupied) is None  # honors max_slots=2


# -------------------------------------------------------------- instance-id/seed
def test_instance_id_rotates_per_cycle():
    a = lx.instance_id("husk-vm", 0)
    b = lx.instance_id("husk-vm", 1)
    assert a != b  # the load-bearing property: cloud-init re-runs only on change


def test_meta_data_carries_instance_id():
    md = lx.meta_data("husk-vm", 2)
    assert lx.instance_id("husk-vm", 2) in md
    assert "local-hostname: husk-vm" in md


# ------------------------------------------------------------ metadata roundtrip
def test_metadata_roundtrip():
    xml = lx.metadata_xml(
        cycle=3,
        provisioned_at=1700000000.0,
        created_at=1699999000.0,
        unit="0000:01:00.0",
        image_digest="sha256:abc123",
    )
    got = lx.parse_metadata(xml)
    assert got == {
        "managed_by": "husk",
        "unit": "0000:01:00.0",
        "cycle": 3,
        "provisioned_at": 1700000000.0,
        "created_at": 1699999000.0,
        "image_digest": "sha256:abc123",
    }


def test_metadata_omits_image_digest_when_absent():
    # Manual/local-file path: no digest stamped, so no <image-digest> element and
    # the parsed digest reads None (→ slot never classified stale).
    xml = lx.metadata_xml(cycle=0, provisioned_at=1.0, created_at=1.0, unit="cpu0")
    assert "image-digest" not in xml
    assert lx.parse_metadata(xml)["image_digest"] is None


def test_parse_metadata_handles_libvirt_denamespaced_form():
    # virDomainGetMetadata(ELEMENT, uri) returns the element with the namespace
    # stripped from the children (verified live against libvirt 9/12); parsing
    # must still recover the dict by local name. Regression for the smoke-test bug.
    returned = (
        "<slot>\n  <managed-by>husk</managed-by>\n  <cycle>3</cycle>\n"
        "  <provisioned-at>1700000000</provisioned-at>\n"
        "  <created-at>1699999000</created-at>\n  <unit>cpu0</unit>\n</slot>"
    )
    assert lx.parse_metadata(returned) == {
        "managed_by": "husk",
        "unit": "cpu0",
        "cycle": 3,
        "provisioned_at": 1700000000.0,
        "created_at": 1699999000.0,
        "image_digest": None,  # pre-pipeline domains carry no digest
    }


def test_parse_metadata_rejects_foreign_and_empty():
    assert lx.parse_metadata(None) is None
    assert lx.parse_metadata("<other xmlns='urn:x'><a/></other>") is None
    assert lx.parse_metadata("not xml <<<") is None


# ------------------------------------------------------------------- domain XML
def _domain(**kw):
    base = dict(
        name="husk-123",
        uuid="11111111-2222-3333-4444-555555555555",
        memory_mb=4096,
        vcpus=4,
        overlay_path="/var/lib/libvirt/images/husk/husk-123.qcow2",
        seed_path="/var/lib/libvirt/images/husk/husk-123-seed.iso",
        network="default",
        metadata=lx.metadata_xml(cycle=0, provisioned_at=0, created_at=0, unit="cpu0"),
    )
    base.update(kw)
    return lx.domain_xml(**base)


def test_domain_xml_cpu_has_no_hostdev():
    xml = _domain()
    assert "<hostdev" not in xml
    assert "husk-123.qcow2" in xml
    assert "husk-123-seed.iso" in xml
    assert "<name>husk-123</name>" in xml
    assert "device='cdrom'" in xml  # NoCloud seed


def test_domain_xml_gpu_has_hostdev():
    xml = _domain(gpu_pci_address="0000:01:00.0")
    assert "<hostdev mode='subsystem' type='pci' managed='yes'>" in xml
    assert "<driver name='vfio'/>" in xml
    # 0000:01:00.0 → domain 0x0000 bus 0x01 slot 0x00 function 0x0
    assert "domain='0x0000'" in xml
    assert "bus='0x01'" in xml
    assert "slot='0x00'" in xml
    assert "function='0x0'" in xml


def test_domain_xml_console_pty_by_default():
    xml = _domain()
    assert "<console type='pty'/>" in xml
    assert "<serial type='file'>" not in xml


def test_domain_xml_console_log_to_file_when_set():
    xml = _domain(console_log_path="/var/lib/libvirt/images/husk/husk-123-console.log")
    assert "<serial type='file'>" in xml
    assert "husk-123-console.log" in xml
    assert "<console type='pty'/>" not in xml


def test_domain_xml_embeds_husk_metadata():
    xml = _domain()
    assert lx.HUSK_NS in xml
    assert "<managed-by>husk</managed-by>" in xml
