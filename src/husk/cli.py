"""Command-line entry points: `huskd` (the daemon) and `huskctl` (one-shots)."""

from __future__ import annotations

import json
import logging
import os
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
        help="DEBUG/INFO/WARNING/ERROR (default: $HUSK_LOG_LEVEL or INFO)",
    ),
]


def _setup_logging(level: Optional[str]) -> None:
    name = (level or os.environ.get("HUSK_LOG_LEVEL") or "INFO").upper()
    resolved = logging.getLevelName(name)
    if not isinstance(resolved, int):  # unknown name → fall back to INFO
        typer.echo(f"unknown log level {name!r}; using INFO", err=True)
        resolved = logging.INFO
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


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


def _print_status(snap: ControllerState | None) -> None:
    if snap is None:
        typer.echo("no snapshot yet")
        return
    typer.echo(
        f"backend={snap.backend} gen={snap.generation} "
        f"desired={snap.desired_total} (min_ready={snap.min_ready} max_total={snap.max_total})"
    )
    for state, n in snap.counts.items():
        if n:
            typer.echo(f"  {state:14s} {n}")
    for v in snap.slots:
        typer.echo(f"    {v.id:18s} {v.name:24s} {v.state:14s} ({v.status})")


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
) -> None:
    """Show the current pool state (read-only)."""
    _setup_logging(log_level)
    cfg = _load(config, secrets_dir)
    backend, github = _build(cfg)
    snap = Controller(backend, github, cfg).observe()
    if json_out:
        typer.echo(json.dumps(snap.to_dict(), indent=2))
    else:
        _print_status(snap)


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
