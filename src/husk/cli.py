"""Command-line entry points: `huskd` (the daemon) and `huskctl` (one-shots)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Annotated, Optional

import typer

from husk.config import Config, load_config
from husk.controller import Controller
from husk.lock import LockHeld, SingleControllerLock
from husk.snapshot import ControllerState

huskd_app = typer.Typer(help="Husk controller daemon", add_completion=False)
huskctl_app = typer.Typer(help="Husk operator CLI", add_completion=False)

_ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to config.toml")]
_SecretsOpt = Annotated[
    Optional[Path], typer.Option("--secrets-dir", help="k8s secrets mount")
]
_LogLevelOpt = Annotated[
    Optional[str],
    typer.Option(
        "--log-level",
        "-l",
        help="husk log level: DEBUG/INFO/WARNING/ERROR (default: $HUSK_LOG_LEVEL or "
        "INFO). Third-party libs stay at WARNING; raise via $HUSK_ROOT_LOG_LEVEL.",
    ),
]


def _resolve_level(name: str, default: int) -> int:
    resolved = logging.getLevelName(name.upper())
    return resolved if isinstance(resolved, int) else default


def _setup_logging(level: Optional[str]) -> None:
    name = (level or os.environ.get("HUSK_LOG_LEVEL") or "INFO").upper()
    husk_level = logging.getLevelName(name)
    if not isinstance(husk_level, int):  # unknown name → fall back to INFO
        typer.echo(f"unknown log level {name!r}; using INFO", err=True)
        husk_level = logging.INFO

    # Keep the root logger (and thus noisy third-party libs like keystoneauth,
    # urllib3, openstack) at WARNING, and set ONLY the husk logger to the
    # requested level — so `-l DEBUG` shows husk's trace without the low-level
    # HTTP/auth chatter. Power users can raise the floor via $HUSK_ROOT_LOG_LEVEL.
    root_level = _resolve_level(
        os.environ.get("HUSK_ROOT_LOG_LEVEL", "WARNING"), logging.WARNING
    )
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("husk").setLevel(husk_level)


def _load(config: Path, secrets_dir: Optional[Path]) -> Config:
    try:
        return load_config(
            str(config), secrets_dir=str(secrets_dir) if secrets_dir else None
        )
    except Exception as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(code=2)


def _build(cfg: Config):
    from husk.github import GitHubClient
    from husk.openstack_backend import OpenStackBackend

    backend = OpenStackBackend(cfg.backend)
    github = GitHubClient(
        repo=cfg.github.repo,
        token=cfg.github.token,
        labels=cfg.runner.labels,
        runner_group_id=cfg.runner.runner_group_id,
    )
    return backend, github


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join("{:<%d}" % w for w in widths)
    out = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    out += [fmt.format(*row) for row in rows]
    return "\n".join(line.rstrip() for line in out)


_STATE_STYLE = {
    "idle": "green",
    "busy": "cyan",
    "starting": "yellow",
    "needs_recycle": "blue",
    "unhealthy": "red",
    "error": "bold red",
}

_STALE_AFTER_SEC = 60  # snapshot older than this likely means huskd is behind/down


def _secs(v: float | None) -> str:
    return f"{v:.0f}s" if v is not None else "-"


def _pct(v: float | None) -> str:
    return f"{v * 100:.0f}%" if v is not None else "-"


def _age_str(epoch: float) -> str:
    age = max(0.0, time.time() - epoch)
    if age < 90:
        return f"{age:.0f}s ago"
    if age < 5400:
        return f"{age / 60:.0f}m ago"
    return f"{age / 3600:.1f}h ago"


def _status_table(snap: ControllerState):
    """A rich Table of the classified slots (used by the live --watch view)."""
    from rich.table import Table
    from rich.text import Text

    table = Table(expand=False, header_style="bold")
    # (name, min_width, justify) — min_width keeps the live --watch view stable:
    # columns pad to a floor sized for their widest realistic value, so cells
    # don't jitter as values flip to "-" or states change length frame to frame.
    cols = [
        ("ID", 13, "left"),
        ("NAME", 18, "left"),
        ("STATE", 13, "left"),  # longest: needs_recycle
        ("NOVA", 7, "left"),  # longest: SHUTOFF
        ("TASK", 16, "left"),  # longest: rebuild_spawning
        ("RUNNER", 22, "left"),
        ("BUSY", 4, "left"),
        ("CYCLE", 5, "right"),
        ("CLOUD_INIT", 10, "right"),
        ("LIVE%", 5, "right"),
    ]
    for name, min_width, justify in cols:
        table.add_column(
            name,
            justify=justify,
            min_width=min_width,
            no_wrap=True,
            overflow="ellipsis",
        )
    for v in sorted(snap.slots, key=lambda v: (v.name, v.id)):
        if v.runner:  # red runner name encodes an offline registration
            runner = Text(v.runner, style="red" if v.runner_status == "offline" else "")
        else:
            runner = Text("-", style="dim")
        table.add_row(
            v.id,
            v.name,
            Text(v.state, style=_STATE_STYLE.get(v.state, "")),
            v.status,
            v.task_state or "-",
            runner,
            Text("yes", style="cyan") if v.busy else "-",
            str(v.cycle),
            _secs(v.cloudinit_seconds),
            _pct(v.live_fraction),
        )
    return table


def _status_renderable(snap: ControllerState):
    """A rich renderable (summary header + slot table) for one frame."""
    from rich.console import Group
    from rich.text import Text

    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.last_reconcile_epoch))
    age = _age_str(snap.last_reconcile_epoch)
    stale = (time.time() - snap.last_reconcile_epoch) > _STALE_AFTER_SEC
    age_markup = f"[red]{age} — huskd stale?[/]" if stale else f"[dim]{age}[/]"
    counts = "  ".join(
        (f"[{_STATE_STYLE[k]}]{k}={v}[/]" if v and k in _STATE_STYLE else f"{k}={v}")
        for k, v in snap.counts.items()
    )
    header = Text.from_markup(
        f"[bold]backend[/] : {snap.backend}\n"
        f"[bold]sizing [/] : desired={snap.desired_total}  "
        f"min_ready={snap.min_ready}  max_total={snap.max_total}\n"
        f"[bold]updated[/] : {when}  ({age_markup}, gen {snap.generation})\n"
        f"[bold]states [/] : {counts}"
    )
    if not snap.slots:
        return Group(header, Text("\n(no managed slots)", style="dim"))
    return Group(header, Text(""), _status_table(snap))


def _watch_status(observe, interval: float) -> None:
    """Full-screen live-updating status until Ctrl-C."""
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text

    console = Console()
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                try:
                    renderable = _status_renderable(observe())
                except Exception as e:  # read-only: show the error, keep watching
                    renderable = Text(f"observe failed: {e}", style="red")
                live.update(renderable, refresh=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        pass


def _print_status(snap: ControllerState | None) -> None:
    if snap is None:
        typer.echo("no snapshot yet")
        return
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.last_reconcile_epoch))
    typer.echo(f"backend : {snap.backend}")
    typer.echo(
        f"sizing  : desired={snap.desired_total}  "
        f"min_ready={snap.min_ready}  max_total={snap.max_total}"
    )
    stale = (
        " — huskd stale?"
        if (time.time() - snap.last_reconcile_epoch) > _STALE_AFTER_SEC
        else ""
    )
    typer.echo(
        f"updated : {when}  ({_age_str(snap.last_reconcile_epoch)}{stale}, gen {snap.generation})"
    )
    # All states, including zeros, so the summary line is stable/scannable.
    typer.echo("states  : " + "  ".join(f"{k}={v}" for k, v in snap.counts.items()))

    if not snap.slots:
        typer.echo("\n(no managed slots)")
        return

    headers = [
        "ID",
        "NAME",
        "STATE",
        "NOVA",
        "TASK",
        "RUNNER",
        "RUNNER_ST",
        "BUSY",
        "CYCLE",
        "CLOUD_INIT",
        "RECYCLE",
        "LIVE%",
    ]
    rows = [
        [
            v.id,
            v.name,
            v.state,
            v.status,
            v.task_state or "-",
            v.runner or "-",
            v.runner_status or "-",
            "yes" if v.busy else "-",
            str(v.cycle),
            _secs(v.cloudinit_seconds),
            _secs(v.recycle_seconds),
            _pct(v.live_fraction),
        ]
        for v in sorted(snap.slots, key=lambda v: (v.name, v.id))
    ]
    typer.echo("")
    typer.echo(_table(headers, rows))


# --------------------------------------------------------------------- huskd
@huskd_app.command()
def run(
    config: _ConfigOpt = Path("config.toml"),
    secrets_dir: _SecretsOpt = None,
    log_level: _LogLevelOpt = None,
    once: Annotated[
        bool, typer.Option(help="Run a single reconcile tick then exit")
    ] = False,
) -> None:
    """Run the reconcile loop (or a single tick with --once)."""
    _setup_logging(log_level)
    cfg = _load(config, secrets_dir)
    lock = SingleControllerLock(cfg.controller.lock_path)
    try:
        lock.acquire()
    except LockHeld as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    server = None
    try:
        backend, github = _build(cfg)
        ctrl = Controller(backend, github, cfg)
        if once:
            ctrl.tick()
            _print_status(ctrl.snapshot)
        else:
            if cfg.controller.http_addr:
                from husk.http_server import StatusServer, parse_addr

                host, port = parse_addr(cfg.controller.http_addr)
                server = StatusServer(lambda: ctrl.snapshot, host, port)
                server.start()
            try:
                ctrl.run()
            except KeyboardInterrupt:
                typer.echo("shutting down", err=True)
    finally:
        if server is not None:
            server.stop()
        lock.release()


# ------------------------------------------------------------------- huskctl
@huskctl_app.command()
def status(
    config: _ConfigOpt = Path("config.toml"),
    secrets_dir: _SecretsOpt = None,
    log_level: _LogLevelOpt = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Live-updating view (rich)")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", "-n", help="Watch refresh seconds")
    ] = 2.0,
    url: Annotated[
        Optional[str],
        typer.Option(
            "--url", help="huskd status URL (default: from controller.http_addr)"
        ),
    ] = None,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Query OpenStack/GitHub directly instead of huskd's published state",
        ),
    ] = False,
) -> None:
    """Show the current pool state.

    Source priority: --live (query OpenStack/GitHub directly) > --url / huskd's
    HTTP endpoint > the published state file. The default renders huskd's exact
    snapshot with no extra API calls; --live can't see huskd's in-memory grace
    tracking, so transient STARTING/draining slots may read UNHEALTHY there."""
    _setup_logging(log_level)
    cfg = _load(config, secrets_dir)
    getter = _snapshot_getter(cfg, live=live, url=url)

    if watch:
        _watch_status(getter, interval)
        return
    try:
        snap = getter()
    except Exception as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    if json_out:
        typer.echo(json.dumps(snap.to_dict(), indent=2))
    else:
        _print_status(snap)


def _snapshot_getter(cfg: Config, *, live: bool, url: Optional[str] = None):
    """Return a callable yielding a fresh ControllerState each call.

    Priority: --live (recompute) > HTTP (--url or controller.http_addr) > the
    published state file. Each getter raises a clear error on failure so the
    one-shot exits non-zero and the watcher shows the error and keeps polling."""
    from husk.snapshot import ControllerState as _CS
    from husk.snapshot import read_state

    if live:
        ctrl: list = []  # built lazily, reused across watch frames

        def from_live() -> ControllerState:
            if not ctrl:
                backend, github = _build(cfg)
                ctrl.append(Controller(backend, github, cfg))
            return ctrl[0].observe()

        return from_live

    target = url or (
        f"http://{cfg.controller.http_addr}/status"
        if cfg.controller.http_addr
        else None
    )
    if target:
        import urllib.request

        def from_http() -> ControllerState:
            try:
                with urllib.request.urlopen(target, timeout=5) as r:
                    return _CS.from_dict(json.loads(r.read()))
            except Exception as e:
                raise RuntimeError(
                    f"could not fetch status from {target}: {e} "
                    "(is huskd running? use --live to query directly)"
                )

        return from_http

    path = cfg.controller.state_path

    def from_file() -> ControllerState:
        snap = read_state(path)
        if snap is None:
            raise RuntimeError(
                f"no huskd state at {path} — is huskd running? (use --live to query directly)"
            )
        return snap

    return from_file


@huskctl_app.command()
def reap(
    config: _ConfigOpt = Path("config.toml"),
    secrets_dir: _SecretsOpt = None,
    log_level: _LogLevelOpt = None,
) -> None:
    """Delete all offline runner registrations from GitHub."""
    _setup_logging(log_level)
    cfg = _load(config, secrets_dir)
    _, github = _build(cfg)
    names = github.reap_offline()
    typer.echo(f"reaped {len(names)} offline runner(s): {names}")


def _recycle(backend, github, *, names, all_slots, force, dry_run):
    """Select and stop slots so huskd rebuilds them on its next tick.

    Stopping a slot drives it to SHUTOFF, which the controller classifies as
    NEEDS_RECYCLE and rebuilds with freshly rendered cloud-init. Pure of console
    I/O so it's testable against the fakes; returns (acted, skipped, unknown)
    where acted = slots stopped (or, under dry_run, that would be), skipped =
    [(slot, reason)], unknown = unmatched tokens. Busy and non-ACTIVE slots are
    skipped unless `force` (busy only — a non-ACTIVE slot is already mid-cycle)."""
    from husk.slot import match_runner

    current = backend.list_slots()  # may raise ListSlotsError → caller aborts
    try:
        runners = github.list_runners()
    except Exception:
        runners = []  # busy detection is best-effort; without it, don't block

    if all_slots:
        targets, unknown = list(current), []
    else:
        by_id = {s.id: s for s in current}
        by_name = {s.name: s for s in current}
        targets, unknown, seen = [], [], set()
        for tok in names:
            s = by_id.get(tok) or by_name.get(tok)
            if s is None:
                unknown.append(tok)
            elif s.id not in seen:
                seen.add(s.id)
                targets.append(s)

    acted, skipped = [], []
    for s in targets:
        if s.status != "ACTIVE":
            skipped.append((s, f"not ACTIVE (status={s.status}) — already mid-cycle"))
            continue
        r = match_runner(runners, s)
        if r is not None and r.online and r.busy and not force:
            skipped.append((s, "busy — use --force to recycle anyway"))
            continue
        if not dry_run:
            backend.stop_slot(s)
        acted.append(s)
    return acted, skipped, unknown


@huskctl_app.command()
def recycle(
    names: Annotated[
        Optional[list[str]],
        typer.Argument(help="Slot id(s) or name(s) to recycle (omit when using --all)"),
    ] = None,
    config: _ConfigOpt = Path("config.toml"),
    secrets_dir: _SecretsOpt = None,
    log_level: _LogLevelOpt = None,
    all_slots: Annotated[
        bool, typer.Option("--all", help="Recycle every idle slot")
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force", "-f", help="Also recycle a busy slot (kills its running job)"
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be recycled; change nothing"),
    ] = False,
) -> None:
    """Stop slots so huskd rebuilds them on its next tick.

    A stop drives the slot to SHUTOFF, which the controller reads as
    NEEDS_RECYCLE and rebuilds with freshly rendered cloud-init — the way to roll
    out a new image or firewall onto already-running slots. huskd must be running
    for the rebuild to follow; this command only issues the stop. Busy and
    non-ACTIVE slots are skipped unless --force."""
    _setup_logging(log_level)
    if all_slots and names:
        typer.echo("--all takes no slot arguments", err=True)
        raise typer.Exit(code=2)
    if not all_slots and not names:
        typer.echo("specify slot id(s)/name(s) or --all", err=True)
        raise typer.Exit(code=2)

    cfg = _load(config, secrets_dir)
    backend, github = _build(cfg)
    try:
        acted, skipped, unknown = _recycle(
            backend,
            github,
            names=names or [],
            all_slots=all_slots,
            force=force,
            dry_run=dry_run,
        )
    except Exception as e:
        typer.echo(f"recycle failed: {e}", err=True)
        raise typer.Exit(code=1)

    verb = "would recycle" if dry_run else "recycling"
    for s in acted:
        typer.echo(f"{verb}: {s.name} ({s.id}) cycle={s.cycle}")
    for s, why in skipped:
        typer.echo(f"skipped {s.name} ({s.id}): {why}", err=True)
    for tok in unknown:
        typer.echo(f"not found (managed-by=husk): {tok}", err=True)

    if not acted and not skipped and not unknown:
        typer.echo("no matching slots")
    elif acted and not dry_run:
        typer.echo(
            f"\nstopped {len(acted)} slot(s) → SHUTOFF; huskd will rebuild them "
            "with fresh cloud-init on the next tick (watch: huskctl status -w)."
        )
    if unknown:
        raise typer.Exit(code=1)


def huskd() -> None:
    huskd_app()


def huskctl() -> None:
    huskctl_app()
