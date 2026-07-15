"""Parsing the husk-bootreport serial-console block (pure, no controller).

Built against a real Nova console log (tests/fixtures/bootreport-console-real.log):
journald-prefixed lines, output interleaved with unrelated console traffic, and —
in that particular capture — no `systemd-analyze time` line (it ran before boot
finished). The parser must cope with all three.
"""

from __future__ import annotations

from pathlib import Path

from husk.bootreport import parse_bootreport

FIXTURE = Path(__file__).parent / "fixtures" / "bootreport-console-real.log"

# A synthetic block in the *real* on-the-wire shape (kernel-timestamp + syslog
# ident prefixes), but WITH the `systemd-analyze time` line the producer fix
# restores. Mirrors what a fixed slot emits.
PREFIXED_WITH_TIME = """\
[   10.746092] sh[1198]: ===== husk-bootreport 2026-07-10T15:14:44Z =====
[   10.900000] systemd-analyze[1200]: Startup finished in 2.1s (kernel) + 4.5s (initrd) + 8.9s (userspace) = 15.6s
ci-info: interleaved noise that must be ignored
[   11.845845] sh[1232]: 2.923s cloud-init-local.service
[   11.846409] sh[1232]: 987ms docker.service
[   12.172216] cloud-init[1244]: -- Boot Record 01 --
[   12.175073] cloud-init[1244]:      02.26400s (init-local/search-OpenStackLocal)
[   12.176336] cloud-init[1244]:      00.10900s (init-network/config-ssh)
[   12.243571] sh[1247]: ===== husk-bootreport end =====
"""


def test_real_console_fixture():
    r = parse_bootreport(FIXTURE.read_text(encoding="utf-8", errors="replace"))
    assert r is not None
    assert r.timestamp == "2026-07-10T15:14:44Z"
    # This capture predates the producer fix: no `systemd-analyze time` line.
    assert (
        r.kernel_seconds,
        r.initrd_seconds,
        r.userspace_seconds,
        r.total_seconds,
    ) == (
        None,
        None,
        None,
        None,
    )
    # ...but the blame sections parse despite prefixes + interleaving.
    assert r.systemd_units[0] == ("cloud-init-local.service", 2.923)
    stage_names = [n for n, _ in r.cloudinit_stages]
    assert "init-local/search-OpenStackLocal" in stage_names
    assert r.cloudinit_stages[0] == ("init-local/search-OpenStackLocal", 2.264)


def test_prefixed_block_with_time_line():
    r = parse_bootreport(PREFIXED_WITH_TIME)
    assert r is not None
    assert r.timestamp == "2026-07-10T15:14:44Z"
    assert r.kernel_seconds == 2.1
    assert r.initrd_seconds == 4.5
    assert r.userspace_seconds == 8.9
    assert r.total_seconds == 15.6
    # Slowest-first, ms normalised to seconds, interleaved ci-info line ignored.
    assert r.systemd_units == (
        ("cloud-init-local.service", 2.923),
        ("docker.service", 0.987),
    )
    assert r.cloudinit_stages == (
        ("init-local/search-OpenStackLocal", 2.264),
        ("init-network/config-ssh", 0.109),
    )


def test_clean_unprefixed_block_still_parses():
    # A pristine (no journald prefix) block must also work — prefix stripping is
    # optional, not required.
    text = (
        "===== husk-bootreport 2026-01-01T00:00:00Z =====\n"
        "Startup finished in 1.5s (kernel) + 9.0s (userspace) = 10.5s\n"
        "===== husk-bootreport end =====\n"
    )
    r = parse_bootreport(text)
    assert r is not None
    assert r.kernel_seconds == 1.5
    assert r.initrd_seconds is None  # initrd term absent
    assert r.userspace_seconds == 9.0
    assert r.total_seconds == 10.5


def test_minutes_and_millis_durations():
    text = (
        "===== husk-bootreport 2026-01-01T00:00:00Z =====\n"
        "Startup finished in 1min 5.402s (kernel) + 526ms (userspace) = 1min 5.928s\n"
        "===== husk-bootreport end =====\n"
    )
    r = parse_bootreport(text)
    assert r is not None
    assert r.kernel_seconds == 65.402
    assert r.userspace_seconds == 0.526
    assert r.total_seconds == 65.928


def test_last_of_two_blocks_wins():
    text = (
        "[   1.0] sh[1]: ===== husk-bootreport 2026-01-01T00:00:00Z =====\n"
        "[   1.1] systemd-analyze[2]: Startup finished in 1.0s (kernel) = 1.0s\n"
        "[   1.2] sh[3]: ===== husk-bootreport end =====\n"
        "[   9.0] sh[4]: ===== husk-bootreport 2026-01-01T00:05:00Z =====\n"
        "[   9.1] systemd-analyze[5]: Startup finished in 2.0s (kernel) = 2.0s\n"
        "[   9.2] sh[6]: ===== husk-bootreport end =====\n"
    )
    r = parse_bootreport(text)
    assert r is not None
    assert r.timestamp == "2026-01-01T00:05:00Z"
    assert r.total_seconds == 2.0


def test_incomplete_trailing_block_falls_back_to_last_complete():
    text = (
        "===== husk-bootreport 2026-01-01T00:00:00Z =====\n"
        "Startup finished in 1.0s (kernel) = 1.0s\n"
        "===== husk-bootreport end =====\n"
        "===== husk-bootreport 2026-01-01T00:05:00Z =====\n"
        "Startup finished in 2.0s (kernel) = 2.0s\n"  # no end marker yet
    )
    r = parse_bootreport(text)
    assert r is not None
    assert r.timestamp == "2026-01-01T00:00:00Z"


def test_only_incomplete_block_returns_none():
    text = (
        "===== husk-bootreport 2026-01-01T00:05:00Z =====\n"
        "Startup finished in 2.0s (kernel) = 2.0s\n"  # never flushed the end marker
    )
    assert parse_bootreport(text) is None


def test_no_block_returns_none():
    assert parse_bootreport("just some unrelated console output\n") is None


def test_mid_line_marker_is_not_a_boundary():
    # A job echoing the marker phrase mid-line must not open a block. (After prefix
    # stripping the payload still doesn't start with the marker.)
    text = (
        "echo prefix ===== husk-bootreport 2026-01-01T00:00:00Z =====\n"
        "Startup finished in 9.9s (kernel) = 9.9s\n"
    )
    assert parse_bootreport(text) is None


def test_end_line_not_mistaken_for_start():
    r = parse_bootreport(PREFIXED_WITH_TIME)
    assert r is not None and r.timestamp != "end"
