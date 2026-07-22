"""The guest-side boot reporter (images/files/husk-bootreport).

That script runs on the slot, not in huskd, so it imports nothing from `husk` and
is loaded here by path. It is tested from this repo anyway because it is the only
producer of the boot-timing metrics: a parsing bug there is invisible until a
dashboard is quietly empty, and the guest is the one place we cannot iterate
quickly (every change costs a golden-image rebuild and a rollout).

The blame/duration fixtures come from a real Nova console capture — the same one
the old controller-side parser was built against.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

# No .py extension (it is installed as a plain executable on the guest), so it
# has to be loaded through a SourceFileLoader rather than imported.
_SCRIPT = Path(__file__).parents[1] / "images" / "files" / "husk-bootreport"


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "husk_bootreport_script", str(_SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hb = _load()


# ── duration tokens ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token,expected",
    [
        ("8.9s", 8.9),
        ("526ms", 0.526),
        ("02.26400s", 2.264),
        ("1min 5.402s", 65.402),
        ("2.923s", 2.923),
    ],
)
def test_parse_duration(token, expected):
    assert hb.parse_duration(token) == pytest.approx(expected)


@pytest.mark.parametrize("token", ["", "nonsense", "12", "5 seconds"])
def test_parse_duration_rejects_junk(token):
    assert hb.parse_duration(token) is None


# ── markers ───────────────────────────────────────────────────────────────────


def test_parse_markers_in_file_order():
    text = "runcmd_start 12.5\nfirewall_applied 12.9\nrunner_started 20.25\n"
    assert hb.parse_markers(text) == [
        ("runcmd_start", 12.5),
        ("firewall_applied", 12.9),
        ("runner_started", 20.25),
    ]


def test_parse_markers_drops_malformed_lines_but_keeps_the_rest():
    # A corrupt marker must cost us that line, not the whole report.
    text = "a 1.0\ngarbage\nb notanumber\n\nc 3.0\n"
    assert hb.parse_markers(text) == [("a", 1.0), ("c", 3.0)]


# ── systemd-analyze time ──────────────────────────────────────────────────────


def test_parse_systemd_time():
    line = (
        "Startup finished in 2.1s (kernel) + 4.5s (initrd) + 8.9s (userspace) = 15.6s"
    )
    assert hb.parse_systemd_time(line) == {
        "kernel": pytest.approx(2.1),
        "initrd": pytest.approx(4.5),
        "userspace": pytest.approx(8.9),
        "total": pytest.approx(15.6),
    }


def test_parse_systemd_time_absent_is_empty_not_an_error():
    # `systemd-analyze time` prints nothing until startup has finished, and this
    # report deliberately runs before that. Empty is the expected case, not a bug.
    assert hb.parse_systemd_time("") == {}


# ── blame sections ────────────────────────────────────────────────────────────


def test_parse_systemd_blame_sorted_slowest_first():
    text = "987ms docker.service\n2.923s cloud-init-local.service\n1min 5.402s slow.service\n"
    assert hb.parse_blame(text, hb._SYSTEMD_BLAME_RE) == [
        ("slow.service", pytest.approx(65.402)),
        ("cloud-init-local.service", pytest.approx(2.923)),
        ("docker.service", pytest.approx(0.987)),
    ]


def test_parse_cloudinit_blame():
    text = (
        "     02.26400s (init-local/search-OpenStackLocal)\n"
        "     00.10900s (init-network/config-ssh)\n"
    )
    assert hb.parse_blame(text, hb._CLOUDINIT_BLAME_RE) == [
        ("init-local/search-OpenStackLocal", pytest.approx(2.264)),
        ("init-network/config-ssh", pytest.approx(0.109)),
    ]


def test_blame_ignores_interleaved_noise():
    text = (
        "-- Boot Record 01 --\nci-info: something entirely unrelated\n2.9s a.service\n"
    )
    assert hb.parse_blame(text, hb._SYSTEMD_BLAME_RE) == [("a.service", 2.9)]


# ── exposition output ─────────────────────────────────────────────────────────


def _metrics(**kw) -> str:
    base = dict(markers=[], phases={}, units=[], stages=[])
    base.update(kw)
    return hb.build_metrics(**base)


def test_steps_are_deltas_between_consecutive_markers():
    out = _metrics(
        markers=[("runcmd_start", 10.0), ("firewall_applied", 10.5), ("done", 13.0)]
    )
    assert 'husk_cloudinit_step_seconds{step="firewall_applied"} 0.500' in out
    assert 'husk_cloudinit_step_seconds{step="done"} 2.500' in out
    # The first marker has no predecessor inside runcmd, so it gets no step.
    assert 'step="runcmd_start"' not in out


def test_markers_also_exposed_as_absolute_uptime():
    out = _metrics(markers=[("runner_started", 21.75)])
    assert 'husk_cloudinit_marker_uptime_seconds{marker="runner_started"} 21.750' in out


def test_total_runcmd_spans_first_to_last_marker():
    out = _metrics(markers=[("a", 10.0), ("b", 11.0), ("c", 18.5)])
    assert "husk_cloudinit_runcmd_seconds 8.500" in out


def test_no_markers_still_produces_valid_output():
    # A slot whose marker file never appeared must still emit the rest.
    out = _metrics(phases={"kernel": 2.0})
    assert 'husk_boot_phase_seconds{phase="kernel"} 2.000' in out
    assert "husk_cloudinit_runcmd_seconds" not in out


def test_blame_entries_are_capped():
    units = [(f"u{i}.service", float(100 - i)) for i in range(50)]
    out = _metrics(units=units)
    assert out.count("husk_boot_unit_seconds{") == hb.TOP_N


def test_help_and_type_emitted_once_per_metric():
    out = _metrics(markers=[("a", 1.0), ("b", 2.0), ("c", 3.0)])
    assert out.count("# TYPE husk_cloudinit_step_seconds gauge") == 1
    assert out.count("# HELP husk_cloudinit_step_seconds") == 1


def test_samples_of_a_family_are_contiguous():
    """The exposition format requires each family to be one uninterrupted group.
    Building the markers loop the obvious way interleaves two families, which
    node_exporter's parser rejects — and the only symptom is a missing metric."""
    out = _metrics(
        markers=[("a", 1.0), ("b", 2.0), ("c", 3.0)],
        phases={"kernel": 1.0, "total": 9.0},
        units=[("x.service", 1.0), ("y.service", 2.0)],
    )
    seen: list[str] = []
    for line in out.splitlines():
        if line.startswith("#"):
            continue
        name = line.split("{")[0].split(" ")[0]
        if not seen or seen[-1] != name:
            # A family may only start once; seeing it again after another
            # family's samples means the groups are interleaved.
            assert name not in seen, f"{name} is not contiguous in:\n{out}"
            seen.append(name)


def test_label_values_are_escaped():
    # Stage names come from cloud-init's output; a quote in one must not be able
    # to produce a malformed exposition line.
    out = _metrics(stages=[('we"ird', 1.0)])
    assert r'stage="we\"ird"' in out


def test_every_line_is_a_comment_or_a_sample():
    out = _metrics(
        markers=[("a", 1.0), ("b", 2.0)],
        phases={"total": 15.6},
        units=[("x.service", 1.0)],
        stages=[("init/y", 2.0)],
    )
    for line in out.splitlines():
        assert line.startswith("#") or " " in line, line
        if not line.startswith("#"):
            # A sample is "<name>[{labels}] <float>".
            float(line.rsplit(" ", 1)[1])


# ── atomic write ──────────────────────────────────────────────────────────────


def test_write_textfile_creates_the_directory_and_file(tmp_path):
    target = tmp_path / "nested" / "husk_boot.prom"
    assert hb.write_textfile(str(target), "husk_x 1.0\n") is True
    assert target.read_text() == "husk_x 1.0\n"


def test_write_textfile_leaves_no_temp_files_behind(tmp_path):
    target = tmp_path / "husk_boot.prom"
    hb.write_textfile(str(target), "husk_x 1.0\n")
    assert [p.name for p in tmp_path.iterdir()] == ["husk_boot.prom"]


def test_write_textfile_replaces_an_existing_file(tmp_path):
    target = tmp_path / "husk_boot.prom"
    target.write_text("stale\n")
    hb.write_textfile(str(target), "fresh\n")
    assert target.read_text() == "fresh\n"


def test_write_textfile_reports_failure_instead_of_raising(tmp_path):
    # Diagnostics must never fail the unit — an unwritable target is a False,
    # not an exception.
    blocker = tmp_path / "notadir"
    blocker.write_text("")
    assert hb.write_textfile(str(blocker / "x.prom"), "husk_x 1.0\n") is False


def test_run_returns_empty_for_a_missing_command():
    assert hb.run("/nonexistent/husk/definitely-not-here") == ""
