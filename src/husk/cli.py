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


def _status_table(snap: ControllerState):
    """A rich Table of the classified slots (used by the live --watch view)."""
    from rich.table import Table
    from rich.text import Text

    table = Table(expand=False, header_style="bold")
    for col in ("ID", "NAME", "STATE", "NOVA", "TASK", "RUNNER", "BUSY", "CYCLE"):
        table.add_column(col)
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
        )
    return table


def _status_renderable(snap: ControllerState):
    """A rich renderable (summary header + slot table) for one frame."""
    from rich.console import Group
    from rich.text import Text

    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.last_reconcile_epoch))
    counts = "  ".join(
        (f"[{_STATE_STYLE[k]}]{k}={v}[/]" if v and k in _STATE_STYLE else f"{k}={v}")
        for k, v in snap.counts.items()
    )
    header = Text.from_markup(
        f"[bold]backend[/] : {snap.backend}\n"
        f"[bold]sizing [/] : desired={snap.desired_total}  "
        f"min_ready={snap.min_ready}  max_total={snap.max_total}\n"
        f"[bold]updated[/] : {when}  (gen {snap.generation})\n"
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
    typer.echo(f"updated : {when}  (gen {snap.generation})")
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
    try:
        backend, github = _build(cfg)
        ctrl = Controller(backend, github, cfg)
        if once:
            ctrl.tick()
            _print_status(ctrl.snapshot)
        else:
            ctrl.run()
    finally:
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
) -> None:
    """Show the current pool state (read-only)."""
    _setup_logging(log_level)
    cfg = _load(config, secrets_dir)
    backend, github = _build(cfg)
    ctrl = Controller(backend, github, cfg)
    if watch:
        _watch_status(ctrl.observe, interval)
    elif json_out:
        typer.echo(json.dumps(ctrl.observe().to_dict(), indent=2))
    else:
        _print_status(ctrl.observe())


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


def huskd() -> None:
    huskd_app()


def huskctl() -> None:
    huskctl_app()
