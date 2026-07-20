"""Configuration model.

The runtime config the controller consumes is a set of plain frozen dataclasses
(import-light, pydantic-free). `load_config()` builds them from TOML + env +
k8s-mounted secret files using pydantic-settings — pydantic is scoped to that
loading boundary only (see `load_config`), never to the hot-path value objects.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from husk.target import Target

log = logging.getLogger("husk.config")


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
    """GitHub App identity — replaces the old single-repo PAT entirely.

    huskd authenticates as the App (RS256 JWT) and exchanges that for a
    short-lived *installation* token per target; there is no long-lived
    credential. `private_key` holds the resolved PEM contents and is never
    logged."""

    app_id: int
    private_key: str


@dataclass(frozen=True)
class RunnerConfig:
    version: str
    labels: list[str]
    # Runner-group NAME, not id: group ids are not portable across orgs, so the
    # client resolves this per target, falling back to Default/1 where no group of
    # this name exists (huskd serves orgs it does not administer, so the group it
    # wants may simply not be there).
    #
    # In TOML this lives inside the pool's *target* table — `target = { org =
    # "acts-project", group = "husk" }` — because runner groups are an org-only
    # concept, and nesting it there makes "group on a repo target" unrepresentable
    # rather than silently ignored. The loader flattens it to here, next to the
    # other knobs the GitHub client consumes.
    runner_group: str = "Default"
    gpu: bool = False  # GPU pools: cloud-init activates the NVIDIA driver + CDI
    prebaked: bool = False  # golden-image pools: skip the install steps (baked in)
    # Source allowed to scrape the slot's node_exporter on :9100 — the sole access
    # control for it (no TLS/auth). Per-pool because the client differs by backend:
    # OpenStack = central Prometheus, which scrapes the guest directly; libvirt =
    # the host's bridge, because the scrape is issued FROM the hypervisor (huskd
    # SSHes in and curls the guest), so the bridge is the only client it ever sees.
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
    # Controller-local oras pull cache, shared by every pool ("" → default
    # ~/.cache/husk/images). Process-wide: the registry golden is pulled once here
    # and fanned out to each pool's hosts/Glance.
    image_cache_dir: str = ""
    # Where central Prometheus reaches THIS huskd. `/sd/targets` hands it out as the
    # address of the proxied libvirt targets (their guests are private, so the scrape
    # comes back through huskd). Empty → falls back to http_addr, which is right
    # unless huskd sits behind a NAT/ingress and is reached on a different address.
    advertise_addr: str = ""


@dataclass(frozen=True)
class Config:
    github: GithubConfig
    # The ONE target this pool serves. Explicit per pool rather than a global
    # allowlist fanned out across pools: warm capacity cannot be shared between
    # targets (a JIT runner is registered to exactly one org/repo), so fan-out
    # silently multiplied min_ready and over-subscribed scarce hardware. Org scope
    # already covers the common "many repos" case with a single target.
    target: Target
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
    with the App private key read from a file (k8s Secret mount). Precedence for
    the shared
    `[github]`/`[controller]` sections: **env > TOML > defaults**; per-pool knobs
    are TOML-only.

    Each pool yields a normal `Config` carrying the shared `github`+`controller`
    (so the `Controller` is unaware of multi-pool) plus its own `runner`/`backend`/
    `timeouts`. Pools must have unique names and `vm_prefix` (the cross-pool
    isolation invariant). pydantic-settings is imported lazily here so the rest of
    the package stays pydantic-free.
    """
    from pathlib import Path

    from pydantic import BaseModel, Field, model_validator
    from pydantic_settings import (
        BaseSettings,
        PydanticBaseSettingsSource,
        SettingsConfigDict,
        TomlConfigSettingsSource,
    )

    class _Github(BaseModel):
        app_id: int
        # PEM contents (env HUSK_GITHUB__PRIVATE_KEY); never in TOML
        private_key: str | None = None
        private_key_path: str | None = None  # file / k8s Secret mount

    class _Target(BaseModel):
        """`target = { org = "acts-project", group = "husk" }` or
        `target = { repo = "owner/name" }`.

        The key names the scope, so there is no `kind:name` string to parse, and
        `group` can only be written where it means something — runner groups are
        an org-only concept."""

        org: str | None = None
        repo: str | None = None
        group: str = "Default"

        @model_validator(mode="after")
        def _exactly_one_scope(self):
            if bool(self.org) == bool(self.repo):
                raise ValueError(
                    "target needs exactly one of org / repo, e.g. "
                    '{ org = "acts-project" } or { repo = "owner/name" }'
                )
            if self.repo and "/" not in self.repo:
                raise ValueError(f"target repo {self.repo!r} must be owner/name")
            if self.org and "/" in self.org:
                raise ValueError(
                    f"target org {self.org!r} looks like a repo — use "
                    '{ repo = "owner/name" }'
                )
            return self

        def resolved(self) -> Target:
            return Target.org(self.org) if self.org else Target.repo(self.repo)

    class _Runner(BaseModel):
        version: str
        labels: list[str]
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

    class _Backend(BaseModel):
        name: str = ""  # defaults to the pool name
        type: str = "openstack"
        vm_prefix: str = ""  # defaults to husk-<slug(pool name)>
        image_name: str = ""
        image_ref: str = ""
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
        target: _Target  # the one org/repo this pool serves
        runner: _Runner
        backend: _Backend = Field(default_factory=_Backend)
        timeouts: _Timeouts = Field(default_factory=_Timeouts)

    class _Controller(BaseModel):
        lock_path: str = "/tmp/huskd.lock"
        http_addr: str = "127.0.0.1:9100"
        shrink_ticks: int = 3
        advertise_addr: str = ""
        image_cache_dir: str = ""

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

    # Resolve the App private key, in priority order:
    #   1. HUSK_GITHUB__PRIVATE_KEY     — PEM contents (pydantic-nested env)
    #   2. [github].private_key_path    — file / mounted k8s Secret
    # then fail closed. There is no PAT path any more: huskd authenticates as the
    # App and mints short-lived per-installation tokens.
    private_key = s.github.private_key
    if not private_key and s.github.private_key_path:
        private_key = Path(s.github.private_key_path).read_text()
    if not private_key or "PRIVATE KEY" not in private_key:
        raise RuntimeError(
            "GitHub App private key not configured: set HUSK_GITHUB__PRIVATE_KEY "
            "(PEM contents) or [github].private_key_path (a path to the .pem the "
            "App settings page generated)"
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

    # Shared across every pool — one App identity + target set, one lock/http
    # per daemon.
    github = GithubConfig(app_id=s.github.app_id, private_key=private_key)
    controller = ControllerConfig(
        lock_path=s.controller.lock_path,
        http_addr=s.controller.http_addr,
        shrink_ticks=s.controller.shrink_ticks,
        advertise_addr=s.controller.advertise_addr,
        image_cache_dir=s.controller.image_cache_dir,
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
            )
            for h in b.hosts
        ),
    )
    return Config(
        github=github,
        target=p.target.resolved(),
        runner=RunnerConfig(
            version=p.runner.version,
            labels=list(p.runner.labels),
            # Flattened from the target table: groups are org-only, so that is
            # the only place the schema lets you write one.
            runner_group=p.target.group,
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
