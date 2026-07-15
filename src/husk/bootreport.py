"""Pure parser for the `husk-bootreport` serial-console block.

`husk-bootreport.service` (baked into the golden image) dumps `systemd-analyze` +
`cloud-init analyze blame` to the serial console each boot, between marker lines::

    ===== husk-bootreport 2026-07-10T12:34:56Z =====
    Startup finished in 2.1s (kernel) + 4.5s (initrd) + 8.9s (userspace) = 15.6s
    ...  (systemd-analyze blame, then cloud-init analyze blame)  ...
    ===== husk-bootreport end =====

Reality on the wire is messier than that clean sketch, and this parser is built
for the mess (verified against a real Nova console log):

* **Every line is prefixed.** journald forwards each service line to the serial
  console as ``[   10.746092] sh[1198]: <payload>`` (kernel timestamp + syslog
  ident). We strip that prefix before matching.
* **The block is interleaved** with unrelated concurrent console output (SSH
  host-key fingerprints etc. land *between* the markers), so we scan for the data
  lines by shape and ignore anything that doesn't match.
* **`systemd-analyze time` may be absent.** It only emits once startup has
  *finished*; if the report ran too early the kernel/initrd/userspace/total line
  is simply missing (phases stay None) — the blame sections still parse.

No `husk` imports — the controller reads a slot's console, hands the text here,
and feeds the result into `SlotTiming.on_bootreport`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# How many slowest entries to keep from each blame section (for the hover).
_TOP_N = 5

# A duration token as systemd/cloud-init print them: "1min 5.402s", "8.9s",
# "526ms", "02.26400s".
_DUR = r"(?:\d+min\s+)?[\d.]+m?s"
_DUR_RE = re.compile(r"(?:(\d+)min\s+)?([\d.]+)(m?s)$")

# journald console prefix: "[   10.746092] sh[1198]: ". Both parts optional so a
# clean (unprefixed) line passes through untouched.
_PREFIX_RE = re.compile(r"^(?:\[\s*\d+\.\d+\]\s*)?(?:[\w./@-]+\[\d+\]:\s?)?")

_START_RE = re.compile(r"^===== husk-bootreport (\S+) =====$")
_END_RE = re.compile(r"^===== husk-bootreport end =====$")

_TIME_RE = re.compile(r"^Startup finished in ")
_PHASE_RE = {
    phase: re.compile(rf"({_DUR})\s*\({phase}\)")
    for phase in ("kernel", "initrd", "userspace")
}
_TOTAL_RE = re.compile(rf"=\s*({_DUR})\s*$")

# systemd-analyze blame: "2.923s cloud-init-local.service". Anchor on a known unit
# suffix so interleaved noise can't masquerade as a unit line.
_UNIT_SUFFIX = (
    r"(?:service|socket|device|mount|automount|swap|target|path|timer|slice|scope)"
)
_SYSTEMD_BLAME_RE = re.compile(rf"^({_DUR})\s+(\S+\.{_UNIT_SUFFIX})$")
# cloud-init analyze blame: "02.26400s (init-local/search-OpenStackLocal)".
_CLOUDINIT_BLAME_RE = re.compile(rf"^({_DUR})\s+\(([^)]+)\)$")


@dataclass(frozen=True)
class BootReport:
    """Parsed husk-bootreport block.

    `timestamp` is the start-marker string — the controller uses it to reject a
    still-present previous cycle's block (the Nova console is a ring buffer).
    Phases come from `systemd-analyze time` (any may be None if that line was
    absent). `systemd_units` / `cloudinit_stages` are the slowest entries from the
    two blame sections, `(name, seconds)`, slowest first.
    """

    timestamp: str
    kernel_seconds: float | None
    initrd_seconds: float | None
    userspace_seconds: float | None
    total_seconds: float | None
    systemd_units: tuple[tuple[str, float], ...] = ()
    cloudinit_stages: tuple[tuple[str, float], ...] = ()


def _parse_duration(tok: str) -> float | None:
    m = _DUR_RE.match(tok.strip())
    if m is None:
        return None
    minutes = int(m.group(1)) if m.group(1) else 0
    value = float(m.group(2))
    seconds = value / 1000.0 if m.group(3) == "ms" else value
    return minutes * 60.0 + seconds


def _strip_prefix(line: str) -> str:
    return _PREFIX_RE.sub("", line, count=1).strip()


def parse_bootreport(console_text: str) -> BootReport | None:
    """Return the LAST COMPLETE husk-bootreport block, or ``None``.

    ``None`` means no complete (start→end) block was found — e.g. the current
    block's `end` marker hasn't flushed to the console yet — so the caller should
    retry on a later tick.
    """
    last: tuple[str, list[str]] | None = None
    cur_ts: str | None = None
    cur: list[str] = []
    for raw in console_text.splitlines():
        line = _strip_prefix(raw)
        if _END_RE.match(line):
            if cur_ts is not None:
                last = (cur_ts, cur)
                cur_ts = None
                cur = []
            continue
        start = _START_RE.match(line)
        if start is not None and start.group(1) != "end":
            cur_ts = start.group(1)
            cur = []
            continue
        if cur_ts is not None:
            cur.append(line)

    if last is None:
        return None

    ts, body = last
    kernel = initrd = userspace = total = None
    units: list[tuple[str, float]] = []
    stages: list[tuple[str, float]] = []
    for line in body:
        if _TIME_RE.match(line):
            for phase, rx in _PHASE_RE.items():
                m = rx.search(line)
                if m is not None:
                    val = _parse_duration(m.group(1))
                    if phase == "kernel":
                        kernel = val
                    elif phase == "initrd":
                        initrd = val
                    else:
                        userspace = val
            mt = _TOTAL_RE.search(line)
            if mt is not None:
                total = _parse_duration(mt.group(1))
            continue
        mu = _SYSTEMD_BLAME_RE.match(line)
        if mu is not None:
            d = _parse_duration(mu.group(1))
            if d is not None:
                units.append((mu.group(2), d))
            continue
        mc = _CLOUDINIT_BLAME_RE.match(line)
        if mc is not None:
            d = _parse_duration(mc.group(1))
            if d is not None:
                stages.append((mc.group(2), d))

    units.sort(key=lambda x: x[1], reverse=True)
    stages.sort(key=lambda x: x[1], reverse=True)
    return BootReport(
        timestamp=ts,
        kernel_seconds=kernel,
        initrd_seconds=initrd,
        userspace_seconds=userspace,
        total_seconds=total,
        systemd_units=tuple(units[:_TOP_N]),
        cloudinit_stages=tuple(stages[:_TOP_N]),
    )
