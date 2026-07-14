"""Configuration model.

The runtime config the controller consumes is a set of plain frozen dataclasses
(import-light, pydantic-free). `load_config()` builds them from TOML + env +
k8s-mounted secret files using pydantic-settings — pydantic is scoped to that
loading boundary only (see `load_config`), never to the hot-path value objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


def _slug(name: str) -> str:
    """A short, name-safe pool tag for the default vm_prefix (`husk-<slug>`)."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "pool"


def _ssh_target_from_uri(uri: str) -> str:
    """Derive the `user@host` SSH target from a `qemu+ssh://user@host/system` URI
    (used for host-side qemu-img/genisoimage when `ssh_target` isn't set
    explicitly). Preserves host case (SSH `Host` aliases are case-sensitive) and
    strips any port. Returns "" for a local/transportless URI."""
    netloc = urlparse(uri).netloc  # e.g. "user@host:22"; preserves original case
    if not netloc:
        return ""
    hostpart = netloc.rsplit("@", 1)
    host = hostpart[-1].rsplit(":", 1)[0]  # drop :port
    user = f"{hostpart[0]}@" if len(hostpart) == 2 and hostpart[0] else ""
    return f"{user}{host}"


@dataclass(frozen=True)
class GithubConfig:
    repo: str
    token: str  # resolved secret (PAT); never logged


@dataclass(frozen=True)
class RunnerConfig:
    version: str
    labels: list[str]
    runner_group_id: int
    gpu: bool = False  # GPU pools: cloud-init activates the NVIDIA driver + CDI
    prebaked: bool = False  # golden-image pools: skip the install steps (baked in)
    # Source allowed to scrape the slot's node_exporter on :9100 — the sole access
    # control for it (no TLS/auth). Per-pool because the client differs by backend:
    # OpenStack = central Prometheus (which scrapes the guest directly); libvirt =
    # the host's bridge, since the host proxy is the only client the guest sees.
    # Empty (default) = no ingress rule, exporter not started — fail-closed, so a
    # pool whose scraper source isn't known yet just leaves it unset.
    scrape_cidr: str = ""

    @property
    def url(self) -> str:
        return (
            f"https://github.com/actions/runner/releases/download/v{self.version}/"
            f"actions-runner-linux-x64-{self.version}.tar.gz"
        )


@dataclass(frozen=True)
class HostConfig:
    """One libvirt VM-host in the backend's pool (libvirt backend only).

    Capacity is declared as slot-units: a GPU host sets `gpu_pci_addresses` (one
    slot per address, passed through via `<hostdev>`); a CPU host sets `max_slots`
    (default 1). Setting both is rejected by the backend constructor.
    """

    name: str
    libvirt_uri: str  # e.g. qemu+ssh://user@host/system
    ssh_target: str  # user@host for host-side qemu-img/genisoimage (derived if unset)
    storage_pool: str = "husk"  # libvirt storage pool name (NOT the husk [[pool]])
    network: str = "default"
    memory_mb: int = 4096
    vcpus: int = 4
    gpu_pci_addresses: tuple[str, ...] = ()
    max_slots: int | None = None  # CPU host capacity; None → 1 (and GPU forbids it)
    image_name: str | None = None  # per-host override of the backend golden image
    image_ref: str | None = None  # per-host override of the backend OCI image ref
    # "addr:port" of this host's stateless metrics proxy (observability). Central
    # Prometheus scrapes guests via it (guests have no reachable IP). Empty → the
    # host's guests aren't published as metrics targets.
    metrics_proxy: str = ""


@dataclass(frozen=True)
class BackendConfig:
    name: str
    type: str
    min_ready: int
    max_total: int
    # VM/runner name prefix — minted into `vm_name()`. Under multi-pool every pool
    # MUST have a unique prefix: GitHub's runner APIs are repo-wide, so pool
    # isolation relies on names not colliding (match_runner is prefix-based). The
    # loader derives `husk-<slug(pool.name)>`; "husk" is the single-pool default.
    vm_prefix: str = "husk"
    # Image source. OpenStack uses `image_name` (a Glance image name). libvirt
    # uses `image_ref` (an OCI artifact ref, e.g. ghcr.io/org/husk-gpu:v1 — synced
    # to each host by the controller) when set, else `image_name` as a literal
    # qcow2 filename already present in the host pool (the manual/local path).
    image_name: str = ""
    image_ref: str = ""  # libvirt: OCI ref pulled+staged by the controller
    image_cache_dir: str = ""  # controller-local oras pull cache ("" → default)
    # OpenStack-only (optional / unused for the libvirt backend)
    cloud: str = ""
    flavor_name: str = ""
    network_name: str = ""
    keypair: str = ""
    rebuild_microversion: str = "2.79"
    # libvirt-only (optional / unused for the OpenStack backend)
    hosts: tuple[HostConfig, ...] = ()


@dataclass(frozen=True)
class TimeoutsConfig:
    poll_interval_sec: float = 30
    idle_timeout_sec: float = 1800
    startup_grace_sec: float = 300
    max_job_duration_sec: float = 21600


@dataclass(frozen=True)
class ControllerConfig:
    lock_path: str = "/tmp/huskd.lock"
    # huskd's single HTTP surface: dashboard + /status /metrics /healthz /events.
    # Always on (the only way huskctl reads state); must be set.
    http_addr: str = "127.0.0.1:9100"
    shrink_ticks: int = 3


@dataclass(frozen=True)
class Config:
    github: GithubConfig
    runner: RunnerConfig
    backend: BackendConfig
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    controller: ControllerConfig = field(default_factory=ControllerConfig)


def load_config(path: str, *, secrets_dir: str | None = None) -> Config:
    """Convenience for the single-pool / one-shot CLI paths: the first pool's
    `Config`. Most code should use `load_configs` (huskd drives every pool)."""
    return load_configs(path, secrets_dir=secrets_dir)[0]


def load_configs(path: str, *, secrets_dir: str | None = None) -> list[Config]:
    """Build one `Config` per `[[pool]]` from a TOML file, overlaid by env vars,
    with the PAT read from a file (k8s Secret mount). Precedence for the shared
    `[github]`/`[controller]` sections: **env > TOML > defaults**; per-pool knobs
    are TOML-only.

    Each pool yields a normal `Config` carrying the shared `github`+`controller`
    (so the `Controller` is unaware of multi-pool) plus its own `runner`/`backend`/
    `timeouts`. Pools must have unique names and `vm_prefix` (the cross-pool
    isolation invariant). pydantic-settings is imported lazily here so the rest of
    the package stays pydantic-free.
    """
    import os
    from pathlib import Path

    from pydantic import BaseModel, Field
    from pydantic_settings import (
        BaseSettings,
        PydanticBaseSettingsSource,
        SettingsConfigDict,
        TomlConfigSettingsSource,
    )

    class _Github(BaseModel):
        repo: str
        pat: str | None = None  # secret value (env HUSK_GITHUB__PAT); never in TOML
        pat_path: str | None = None  # k8s: path to a mounted Secret file
        pat_env: str = "GH_TOKEN"  # local dev: env var to read the PAT from

    class _Runner(BaseModel):
        version: str
        labels: list[str]
        runner_group_id: int = 1
        gpu: bool = False
        prebaked: bool = False
        scrape_cidr: str = ""

    class _Host(BaseModel):
        name: str
        libvirt_uri: str
        ssh_target: str | None = None  # derived from the URI when omitted
        storage_pool: str = "husk"  # libvirt storage pool (NOT the husk [[pool]])
        network: str = "default"
        memory_mb: int = 4096
        vcpus: int = 4
        gpu_pci_addresses: list[str] = []
        max_slots: int | None = None
        image_name: str | None = None
        image_ref: str | None = None
        metrics_proxy: str = ""

    class _Backend(BaseModel):
        name: str = ""  # defaults to the pool name
        type: str = "openstack"
        vm_prefix: str = ""  # defaults to husk-<slug(pool name)>
        image_name: str = ""
        image_ref: str = ""
        image_cache_dir: str = ""
        min_ready: int = 1
        max_total: int = 2
        # OpenStack-only (optional for the libvirt backend)
        cloud: str = ""
        flavor_name: str = ""
        network_name: str = ""
        keypair: str = ""
        rebuild_microversion: str = "2.79"
        # libvirt-only (optional for the OpenStack backend)
        hosts: list[_Host] = []

    class _Timeouts(BaseModel):
        poll_interval_sec: float = 30
        idle_timeout_sec: float = 1800
        startup_grace_sec: float = 300
        max_job_duration_sec: float = 21600

    class _Pool(BaseModel):
        name: str  # pool identity → backend.name + default vm_prefix
        runner: _Runner
        backend: _Backend = Field(default_factory=_Backend)
        timeouts: _Timeouts = Field(default_factory=_Timeouts)

    class _Controller(BaseModel):
        lock_path: str = "/tmp/huskd.lock"
        http_addr: str = "127.0.0.1:9100"
        shrink_ticks: int = 3

    class _Settings(BaseSettings):
        model_config = SettingsConfigDict(
            env_prefix="HUSK_",
            env_nested_delimiter="__",
            secrets_dir=secrets_dir,
            extra="ignore",
        )
        github: _Github
        controller: _Controller = Field(default_factory=_Controller)
        pool: list[_Pool] = []

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ):
            toml = TomlConfigSettingsSource(settings_cls, toml_file=path)
            # priority high → low: env > TOML > file-secrets > defaults
            return (
                env_settings,
                dotenv_settings,
                toml,
                file_secret_settings,
                init_settings,
            )

    s = _Settings()

    # Resolve the PAT, in priority order:
    #   1. HUSK_GITHUB__PAT        — explicit override (pydantic-nested env)
    #   2. $GH_TOKEN (= pat_env)   — local dev convenience (matches the .env flow)
    #   3. [github].pat_path file  — k8s Secret mount
    # then fail closed.
    token = s.github.pat
    if not token and s.github.pat_env:
        token = os.environ.get(s.github.pat_env)
    if not token and s.github.pat_path:
        token = Path(s.github.pat_path).read_text().strip()
    if not token:
        raise RuntimeError(
            f"GitHub PAT not configured: set ${s.github.pat_env}, HUSK_GITHUB__PAT, "
            "or [github].pat_path (a file path, e.g. a mounted k8s Secret)"
        )

    if not s.pool:
        hint = ""
        try:  # nudge the common case: an old flat [backend]/[runner] config
            import tomllib

            with open(path, "rb") as f:
                raw = tomllib.load(f)
            if "backend" in raw or "runner" in raw:
                hint = (
                    " — this looks like the old flat format; wrap [runner]/[backend]/"
                    "[timeouts] under a [[pool]] (with a name). See config.example.toml"
                )
        except Exception:
            pass
        raise RuntimeError(
            "no [[pool]] defined: huskd needs at least one pool (each with its own "
            "[pool.runner] and [pool.backend])" + hint
        )

    # Shared across every pool — one repo+PAT, one lock/http per daemon.
    github = GithubConfig(repo=s.github.repo, token=token)
    controller = ControllerConfig(
        lock_path=s.controller.lock_path,
        http_addr=s.controller.http_addr,
        shrink_ticks=s.controller.shrink_ticks,
    )

    configs = [_pool_config(p, github, controller) for p in s.pool]

    names = [c.backend.name for c in configs]
    if len(set(names)) != len(names):
        raise RuntimeError(f"duplicate pool name across [[pool]] entries: {names}")
    prefixes = [c.backend.vm_prefix for c in configs]
    if len(set(prefixes)) != len(prefixes):
        raise RuntimeError(
            f"duplicate vm_prefix across pools: {prefixes} — pools must mint "
            "distinct VM/runner names (GitHub runner APIs are repo-wide)"
        )
    # node_exporter only exists in the golden image, so scrape_cidr on a stock-image
    # pool would open :9100 to a port with nothing behind it. Fail loudly rather
    # than silently not collecting the metrics someone just asked for.
    for c in configs:
        if c.runner.scrape_cidr and not c.runner.prebaked:
            raise RuntimeError(
                f"pool {c.backend.name}: scrape_cidr requires prebaked = true "
                "(node_exporter is baked into the golden image; a stock image has none)"
            )
    return configs


def _pool_config(p, github: GithubConfig, controller: ControllerConfig) -> Config:
    """Assemble one pool's runtime `Config` from its parsed pydantic model `p`."""
    b = p.backend
    backend = BackendConfig(
        name=b.name or p.name,
        type=b.type,
        vm_prefix=b.vm_prefix or f"husk-{_slug(p.name)}",
        cloud=b.cloud,
        image_name=b.image_name,
        image_ref=b.image_ref,
        image_cache_dir=b.image_cache_dir,
        flavor_name=b.flavor_name,
        network_name=b.network_name,
        keypair=b.keypair,
        rebuild_microversion=b.rebuild_microversion,
        min_ready=b.min_ready,
        max_total=b.max_total,
        hosts=tuple(
            HostConfig(
                name=h.name,
                libvirt_uri=h.libvirt_uri,
                ssh_target=h.ssh_target or _ssh_target_from_uri(h.libvirt_uri),
                storage_pool=h.storage_pool,
                network=h.network,
                memory_mb=h.memory_mb,
                vcpus=h.vcpus,
                gpu_pci_addresses=tuple(h.gpu_pci_addresses),
                max_slots=h.max_slots,
                image_name=h.image_name,
                image_ref=h.image_ref,
                metrics_proxy=h.metrics_proxy,
            )
            for h in b.hosts
        ),
    )
    return Config(
        github=github,
        runner=RunnerConfig(
            version=p.runner.version,
            labels=list(p.runner.labels),
            runner_group_id=p.runner.runner_group_id,
            gpu=p.runner.gpu,
            prebaked=p.runner.prebaked,
            scrape_cidr=p.runner.scrape_cidr,
        ),
        backend=backend,
        timeouts=TimeoutsConfig(
            poll_interval_sec=p.timeouts.poll_interval_sec,
            idle_timeout_sec=p.timeouts.idle_timeout_sec,
            startup_grace_sec=p.timeouts.startup_grace_sec,
            max_job_duration_sec=p.timeouts.max_job_duration_sec,
        ),
        controller=controller,
    )
