"""Command-line entry points: `huskd` (the daemon) and `huskctl` (one-shots)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Annotated, Callable, Optional

import typer

from husk.config import Config, load_configs
from husk.controller import Controller
from husk.lock import LockHeld, SingleControllerLock
from husk.multipool import MultiPoolController
from husk.snapshot import ControllerState

log = logging.getLogger("husk.cli")

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


def _load_all(config: Path, secrets_dir: Optional[Path]) -> list[Config]:
    try:
        return load_configs(
            str(config), secrets_dir=str(secrets_dir) if secrets_dir else None
        )
    except Exception as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(code=2)


def _configs_reloader(
    config: Path, secrets_dir: Optional[Path]
) -> "Callable[[], Optional[list[Config]]]":
    """mtime-guarded multi-pool reloader for the MultiPoolController (returns the
    full per-pool list, or None when unchanged / unparseable)."""
    path = str(config)
    last: dict[str, float | None] = {"mtime": None}

    def reload() -> Optional[list[Config]]:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return None
        if last["mtime"] is None:
            last["mtime"] = mtime
            return None
        if mtime == last["mtime"]:
            return None
        last["mtime"] = mtime
        try:
            cfgs = load_configs(
                path, secrets_dir=str(secrets_dir) if secrets_dir else None
            )
            logging.getLogger("husk.multipool").info("config file changed; reloading")
            return cfgs
        except Exception:
            logging.getLogger("husk.multipool").warning(
                "config reload skipped: %s could not be loaded", path, exc_info=True
            )
            return None

    return reload


def _tokens(cfg: Config):
    """The process-wide App credential. One provider serves every (target, pool):
    installation tokens are per *account*, so pools sharing a target share a
    token (and its refresh)."""
    from husk.appauth import InstallationTokenProvider

    return InstallationTokenProvider(cfg.github.app_id, cfg.github.private_key)


def _backend_for(cfg: Config, image_sync=None):
    if cfg.backend.type == "libvirt":
        from husk.libvirt_backend import LibvirtBackend

        return LibvirtBackend(cfg.backend, image_sync=image_sync)
    from husk.openstack_backend import OpenStackBackend

    return OpenStackBackend(cfg.backend, image_sync=image_sync)


def _build(cfg: Config, image_sync=None, *, tokens=None):
    """One (backend, github client) pair for a pool, scoped to the target it serves."""
    from husk.github import GitHubClient

    backend = _backend_for(cfg, image_sync=image_sync)
    github = GitHubClient(
        target=cfg.target,
        tokens=tokens or _tokens(cfg),
        labels=cfg.runner.labels,
        runner_group=cfg.runner.runner_group,
    )
    return backend, github


def _select_pool(cfgs: list[Config], pool: Optional[str]) -> Config:
    """Pick one pool's Config for a per-pool one-shot (recycle). `--pool` is
    required when more than one pool is configured."""
    if pool is not None:
        for c in cfgs:
            if c.backend.name == pool:
                return c
        names = [c.backend.name for c in cfgs]
        typer.echo(f"no pool named {pool!r}; have: {names}", err=True)
        raise typer.Exit(code=2)
    if len(cfgs) == 1:
        return cfgs[0]
    names = [c.backend.name for c in cfgs]
    typer.echo(f"multiple pools configured; pass --pool <name>: {names}", err=True)
    raise typer.Exit(code=2)


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
    """Full-screen live-updating status (all pools) until Ctrl-C."""
    from rich.console import Console, Group
    from rich.live import Live
    from rich.text import Text

    console = Console()
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                try:
                    snaps = observe()
                    parts: list = []
                    for i, snap in enumerate(snaps):
                        if i:
                            parts.append(Text(""))
                        parts.append(_status_renderable(snap))
                    renderable = (
                        Group(*parts) if parts else Text("(no pools)", style="dim")
                    )
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
    """Run the reconcile loop across every [[pool]] (or a single tick with --once)."""
    _setup_logging(log_level)
    cfgs = _load_all(config, secrets_dir)
    # One lock / HTTP port for the whole daemon (shared [controller]).
    shared = cfgs[0].controller
    lock = SingleControllerLock(shared.lock_path)
    try:
        lock.acquire()
    except LockHeld as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    try:
        # One Controller per pool. The facade owns the loop + HTTP + reload, so the
        # sub-controllers get a blanked http_addr and no reload hook. A pool that
        # can't be built (bad backend config, unreachable cloud) is skipped with a
        # loud error rather than taking the whole daemon down — the other pools run.
        # One process-wide image coordinator: the registry pull is single-flighted
        # per content digest and the controller cache is shared across all pools.
        from husk.discovery import TargetDiscovery
        from husk.image_sync import ImageSync
        from husk.poller import RunnerPoller, SnapshotRegistry

        image_sync = ImageSync(shared.image_cache_dir or None)
        # One registry for the whole daemon: the centralized poller writes each
        # target's runner listing here and every pool's reconcile task reads it.
        registry = SnapshotRegistry()
        tokens = _tokens(cfgs[0])  # shared App credential (one App, many targets)
        poller = RunnerPoller(
            registry,
            {},
            # Cadence follows the most eager pool, so no pool ever reads a snapshot
            # older than its own tick interval.
            interval=min(c.timeouts.poll_interval_sec for c in cfgs),
        )
        # One Controller per [[pool]] — each names the one target it serves. A pool
        # that can't be built (bad backend config, unreachable cloud) is skipped
        # with a loud error rather than taking the whole daemon down.
        controllers = []
        for cfg in cfgs:
            label = f"{cfg.target}/{cfg.backend.name}"
            try:
                backend, github = _build(cfg, image_sync=image_sync, tokens=tokens)
            except Exception as e:
                typer.echo(f"pool {label!r} failed to start, skipping: {e}", err=True)
                logging.getLogger("husk").error(
                    "pool %s failed to build; skipping", label, exc_info=True
                )
                continue
            sub = dataclasses.replace(
                cfg, controller=dataclasses.replace(cfg.controller, http_addr="")
            )
            controllers.append(
                Controller(backend, github, sub, target=cfg.target, registry=registry)
            )
        if not controllers:
            typer.echo("no pools could be started", err=True)
            raise typer.Exit(code=1)

        discovery = TargetDiscovery(tokens, [c.target for c in controllers])
        facade = MultiPoolController(
            controllers,
            reload_configs=_configs_reloader(config, secrets_dir),
            discover=discovery.discover,
            # One listing per distinct target, not per pool: the runner API is
            # target-wide, so pools sharing a target share the poll.
            attach=lambda c: poller.add_target(c.target, c.github.list_runners),
            detach=lambda t: (poller.remove_target(t), registry.forget(t)),
        )
        if once:
            asyncio.run(_once(facade, poller, discovery, tokens))
        else:
            if not shared.http_addr:
                typer.echo("controller.http_addr must be set", err=True)
                raise typer.Exit(code=1)
            # libvirt guests are private to their hypervisor, so huskd bridges the
            # last hop of a metrics scrape over the SSH channel it already holds to
            # each host. (pool, host) → ssh_target; "" means the host is local.
            ssh_targets = {
                (cfg.backend.name, h.name): h.ssh_target
                for cfg in cfgs
                for h in cfg.backend.hosts
            }
            asyncio.run(
                _serve(
                    facade,
                    poller,
                    discovery,
                    tokens,
                    shared.http_addr,
                    ssh_targets=ssh_targets,
                    advertise_addr=shared.advertise_addr or shared.http_addr,
                )
            )
    finally:
        lock.release()


async def _shutdown(facade: MultiPoolController, discovery, tokens) -> None:
    """Close every client the daemon opened. The GitHub clients are per
    `(target, pool)` and the target set is dynamic, so they're collected from the
    facade at shutdown rather than captured at startup."""
    for c in facade.controllers:
        try:
            await c.github.aclose()
        except Exception:
            log.debug("closing github client for %s failed", c.target, exc_info=True)
    await discovery.aclose()
    await tokens.aclose()


async def _once(facade: MultiPoolController, poller, discovery, tokens) -> None:
    """Single reconcile pass: discover targets, warm the registry, tick, print."""
    try:
        # Discovery has to run first here: with no daemon loop there is nothing to
        # create the (target, pool) units this pass is supposed to tick.
        await facade.discover_once()
        await poller.poll_once()
        await facade.tick_all()
        for snap in facade.snapshots():
            _print_status(snap)
    finally:
        await _shutdown(facade, discovery, tokens)


async def _serve(
    facade: MultiPoolController,
    poller,
    discovery,
    tokens,
    http_addr: str,
    *,
    ssh_targets: dict[tuple[str, str], str] | None = None,
    advertise_addr: str = "",
) -> None:
    """Run the whole daemon on this one event loop: the centralized runner poller,
    every pool's reconcile task, and Quart serving each endpoint. SIGINT/SIGTERM
    trip the shutdown event, which stops hypercorn (via `shutdown_trigger`) and
    then drains the poller and reconcile tasks.

    Blocking backend work never runs here — `Controller.tick()` pushes it through
    `asyncio.to_thread` — so a wedged hypervisor stalls only its own pool."""
    from husk.guest_scrape import GuestScraper
    from husk.web import make_app, parse_addr, serve_app

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    scraper = GuestScraper(ssh_targets) if ssh_targets else None
    # Discover the target set, then warm its registry entries, both before the
    # first ticks: otherwise the opening tick of every pool would fail-safe purely
    # because nothing had been polled yet. Units created here are registered but
    # not yet spawned (no `stop` event); `facade.run` starts them below.
    await facade.discover_once()
    await poller.poll_once()
    tasks = [
        asyncio.create_task(poller.run(stop), name="husk-poller"),
        asyncio.create_task(facade.run(stop), name="husk-reconcile"),
    ]
    try:
        host, port = parse_addr(http_addr)
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        log.info("dashboard: http://%s:%d/", display_host, port)
        await serve_app(
            make_app(
                facade.snapshots,
                shutdown=stop,
                scraper=scraper,
                advertise_addr=advertise_addr,
            ),
            host,
            port,
            shutdown_trigger=stop.wait,
        )
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _shutdown(facade, discovery, tokens)
        if scraper is not None:
            scraper.close()  # drop the multiplexed SSH control sockets


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
    pool: Annotated[
        Optional[str],
        typer.Option(
            "--pool", help="Show only this pool (by name); default: all pools"
        ),
    ] = None,
) -> None:
    """Show the current state of every pool (or one with --pool).

    Reads huskd's live snapshot over HTTP (`--url` or `controller.http_addr`).
    huskd must be running; there is no offline source."""
    _setup_logging(log_level)
    cfgs = _load_all(config, secrets_dir)
    getter = _snapshot_getter(cfgs, url=url)

    if watch:
        _watch_status(getter, interval)
        return
    try:
        snaps = getter()
    except Exception as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    if pool is not None:
        snaps = [s for s in snaps if s.backend == pool]
        if not snaps:
            typer.echo(f"no pool named {pool!r}", err=True)
            raise typer.Exit(code=1)
    if json_out:
        typer.echo(json.dumps([s.to_dict() for s in snaps], indent=2))
    else:
        for i, snap in enumerate(snaps):
            if i:
                typer.echo("")  # blank line between pools
            _print_status(snap)


def _snapshot_getter(cfgs: list[Config], *, url: Optional[str] = None):
    """Return a callable that fetches the per-pool snapshots from huskd over HTTP
    each call. Raises a clear error on failure so the one-shot exits non-zero and
    the watcher shows the error and keeps polling."""
    import urllib.request

    shared = cfgs[0].controller
    target = url or (f"http://{shared.http_addr}/status" if shared.http_addr else None)

    def from_http() -> list[ControllerState]:
        if not target:
            raise RuntimeError(
                "no huskd HTTP endpoint: set controller.http_addr or pass --url"
            )
        try:
            with urllib.request.urlopen(target, timeout=5) as r:
                return [ControllerState.from_dict(d) for d in json.loads(r.read())]
        except Exception as e:
            raise RuntimeError(
                f"could not fetch status from {target}: {e} (is huskd running?)"
            )

    return from_http


@huskctl_app.command()
def reap(
    config: _ConfigOpt = Path("config.toml"),
    secrets_dir: _SecretsOpt = None,
    log_level: _LogLevelOpt = None,
) -> None:
    """Delete all offline runner registrations from GitHub, across every target."""
    _setup_logging(log_level)
    cfgs = _load_all(config, secrets_dir)
    from husk.github import GitHubClient

    # reap_offline is target-wide and label-agnostic, so one client per target
    # covers every pool serving it.
    tokens = _tokens(cfgs[0])

    async def go():
        from husk.discovery import discover_targets

        out: list[tuple[str, list[str]]] = []
        targets = await discover_targets(tokens, [c.target for c in cfgs])
        if not targets:
            typer.echo(
                "no servable targets: the App is not installed on any target named "
                "by a [[pool]]",
                err=True,
            )
        clients = [
            GitHubClient(
                target=t,
                tokens=tokens,
                labels=cfgs[0].runner.labels,
                runner_group=cfgs[0].runner.runner_group,
            )
            for t in targets
        ]
        try:
            for gh in clients:
                try:
                    out.append((str(gh.target), await gh.reap_offline()))
                except Exception as e:  # one bad target must not hide the others
                    typer.echo(f"reap failed for {gh.target}: {e}", err=True)
        finally:
            for gh in clients:
                await gh.aclose()
            await tokens.aclose()
        return out

    total = 0
    for target, names in asyncio.run(go()):
        typer.echo(f"{target}: reaped {len(names)} offline runner(s): {names}")
        total += len(names)
    typer.echo(f"reaped {total} offline runner(s) in total")


async def _recycle(backend, githubs, *, names, all_slots, force, dry_run):
    """Select and stop slots so huskd rebuilds them on its next tick.

    Stopping a slot drives it to SHUTOFF, which the controller classifies as
    NEEDS_RECYCLE and rebuilds with freshly rendered cloud-init. Pure of console
    I/O so it's testable against the fakes; returns (acted, skipped, unknown)
    where acted = slots stopped (or, under dry_run, that would be), skipped =
    [(slot, reason)], unknown = unmatched tokens. Busy and non-ACTIVE slots are
    skipped unless `force` (busy only — a non-ACTIVE slot is already mid-cycle)."""
    from husk.slot import match_runner

    current = backend.list_slots()  # may raise ListSlotsError → caller aborts
    # Busy detection is best-effort and spans every target the pool serves — a
    # slot's runner lives on whichever target minted it.
    runners = []
    for gh in githubs:
        try:
            runners += await gh.list_runners()
        except Exception:
            pass  # without it, don't block the recycle

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
    pool: Annotated[
        Optional[str],
        typer.Option(
            "--pool",
            help="Which pool to recycle in. With --all, omit to recycle EVERY pool",
        ),
    ] = None,
) -> None:
    """Stop slots so huskd rebuilds them on its next tick.

    A stop drives the slot to SHUTOFF, which the controller reads as
    NEEDS_RECYCLE and rebuilds with freshly rendered cloud-init — the way to roll
    out a new image or firewall onto already-running slots. huskd must be running
    for the rebuild to follow; this command only issues the stop. Busy and
    non-ACTIVE slots are skipped unless --force.

    `--all` with no `--pool` recycles every pool (the whole-fleet roll). A named
    slot still needs `--pool` when more than one pool is configured, since the name
    alone is ambiguous."""
    _setup_logging(log_level)
    if all_slots and names:
        typer.echo("--all takes no slot arguments", err=True)
        raise typer.Exit(code=2)
    if not all_slots and not names:
        typer.echo("specify slot id(s)/name(s) or --all", err=True)
        raise typer.Exit(code=2)

    cfgs = _load_all(config, secrets_dir)
    # --all + no --pool → fan out over every pool. A named slot keeps requiring an
    # unambiguous pool (via _select_pool) when more than one is configured.
    pools = cfgs if (all_slots and pool is None) else [_select_pool(cfgs, pool)]
    multi = len(pools) > 1

    from husk.discovery import discover_targets
    from husk.github import GitHubClient

    tokens = _tokens(cfgs[0])
    # Discovered once and reused across pools: recycle is target-agnostic (it acts
    # on slots), and the targets only matter for best-effort busy detection.
    try:
        targets = asyncio.run(discover_targets(tokens, [c.target for c in cfgs]))
    except Exception as e:
        typer.echo(f"target discovery failed: {e}", err=True)
        raise typer.Exit(code=1)
    total_acted, any_unknown, any_err = 0, False, False
    for cfg in pools:
        backend = _backend_for(cfg)
        # A pool's slots are always minted against the one target it serves, so
        # busy detection needs exactly that target's runner listing.
        githubs = (
            [
                GitHubClient(
                    target=cfg.target,
                    tokens=tokens,
                    labels=cfg.runner.labels,
                    runner_group=cfg.runner.runner_group,
                )
            ]
            if cfg.target in targets
            else []  # not servable → skip the listing, recycle on slots alone
        )
        if multi:
            typer.echo(f"── {cfg.backend.name} ──")

        async def go(backend=backend, githubs=githubs):
            try:
                return await _recycle(
                    backend,
                    githubs,
                    names=names or [],
                    all_slots=all_slots,
                    force=force,
                    dry_run=dry_run,
                )
            finally:
                for gh in githubs:
                    await gh.aclose()

        try:
            acted, skipped, unknown = asyncio.run(go())
        except Exception as e:
            # One pool failing (e.g. a wedged libvirt host) must not abort the
            # rest of a fleet-wide recycle; report and keep going.
            typer.echo(f"recycle failed for {cfg.backend.name}: {e}", err=True)
            any_err = True
            continue

        verb = "would recycle" if dry_run else "recycling"
        for s in acted:
            typer.echo(f"{verb}: {s.name} ({s.id}) cycle={s.cycle}")
        for s, why in skipped:
            typer.echo(f"skipped {s.name} ({s.id}): {why}", err=True)
        for tok in unknown:
            typer.echo(f"not found (managed-by=husk): {tok}", err=True)
        if not acted and not skipped and not unknown:
            typer.echo("no matching slots")
        total_acted += len(acted)
        any_unknown = any_unknown or bool(unknown)

    asyncio.run(tokens.aclose())

    if total_acted and not dry_run:
        typer.echo(
            f"\nstopped {total_acted} slot(s) → SHUTOFF; huskd will rebuild them "
            "with fresh cloud-init on the next tick (watch: huskctl status -w)."
        )
    if any_unknown or any_err:
        raise typer.Exit(code=1)


def huskd() -> None:
    huskd_app()


def huskctl() -> None:
    huskctl_app()
