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


# The tables this file may define. Anything else at the top level is a typo
# (`[controler]`, `[[pools]]`) that pydantic's env-facing `extra="ignore"` cannot
# catch — see `_check_top_level`.
_TOP_LEVEL = {"github", "controller", "pool"}

# domain:bus:device.function, as libvirt's <hostdev> wants it (0000:01:00.0).
_PCI_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$", re.I)


def _check_top_level(path: str) -> None:
    """Reject unknown top-level tables before pydantic runs.

    The settings model has to keep `extra="ignore"`, because its *env* source sees
    every `HUSK_*` var in the environment — including ones that are not config at
    all (`HUSK_LOG_LEVEL`, `HUSK_SMOKE_*`). That leniency would also swallow a
    misspelt table, so the TOML file gets its own strict pass here. Nested tables
    are covered by `extra="forbid"` on the models themselves.
    """
    import tomllib

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"config file not found: {path} (huskd needs one; pass --config, or "
            "check the ConfigMap is mounted where you think it is)"
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"{path} is not valid TOML: {e}") from None
    if unknown := sorted(set(raw) - _TOP_LEVEL):
        # The overwhelmingly likely cause is the old flat format, so name it rather
        # than making someone diff against the example.
        hint = (
            " — this looks like the old flat format; wrap [runner]/[backend]/"
            "[timeouts] under a [[pool]] (with a name). See config.example.toml"
            if {"runner", "backend"} & set(unknown)
            else f" — expected one of {', '.join(sorted(_TOP_LEVEL))}"
        )
        raise RuntimeError(
            f"{path}: unknown top-level {'tables' if len(unknown) > 1 else 'table'} "
            f"{', '.join(repr(k) for k in unknown)}{hint}"
        )


def _check_addr(label: str, addr: str) -> str:
    """Validate a `host:port` bind address at load time rather than at serve time.

    Mirrors `husk.web.app.parse_addr` (bare `:9100` / `9100` means all interfaces),
    reimplemented here so config stays free of the web/Quart import.
    """
    _, _, port = addr.strip().rpartition(":")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(
            f"{label} {addr!r} must be host:port with a valid port, e.g. 0.0.0.0:9100"
        )
    return addr.strip()


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
    import ipaddress
    from pathlib import Path
    from typing import Literal

    from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
    from pydantic_settings import (
        BaseSettings,
        PydanticBaseSettingsSource,
        SettingsConfigDict,
        TomlConfigSettingsSource,
    )

    _check_top_level(path)

    class _Strict(BaseModel):
        """Base for every config table: an unknown key is an error, not a default.

        Silently ignoring a typo is the worst failure mode for a config that runs
        unattended — `min_redy = 5` would leave min_ready at 1 and look healthy.
        Under k8s this turns a bad ConfigMap into a crash-loop with a message
        naming the key, instead of a fleet that is quietly the wrong size."""

        model_config = ConfigDict(extra="forbid")

    class _Github(_Strict):
        app_id: int
        # PEM contents (env HUSK_GITHUB__PRIVATE_KEY); never in TOML
        private_key: str | None = None
        private_key_path: str | None = None  # file / k8s Secret mount

    class _Target(_Strict):
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

    class _Runner(_Strict):
        version: str
        labels: list[str] = Field(min_length=1)
        gpu: bool = False
        prebaked: bool = False
        scrape_cidr: str = ""

        @field_validator("scrape_cidr")
        @classmethod
        def _is_a_cidr(cls, v: str) -> str:
            # This string becomes an nftables rule verbatim; a malformed one would
            # surface as a broken guest firewall, not a config error.
            if v:
                ipaddress.ip_network(v, strict=False)  # raises → validation error
            return v

    class _Host(_Strict):
        name: str
        libvirt_uri: str
        ssh_target: str | None = None  # derived from the URI when omitted
        storage_pool: str = "husk"  # libvirt storage pool (NOT the husk [[pool]])
        network: str = "default"
        memory_mb: int = Field(4096, gt=0)
        vcpus: int = Field(4, gt=0)
        gpu_pci_addresses: list[str] = []
        max_slots: int | None = Field(None, gt=0)
        image_name: str | None = None
        image_ref: str | None = None

        @field_validator("libvirt_uri")
        @classmethod
        def _is_a_libvirt_uri(cls, v: str) -> str:
            # A URI with no recognisable transport makes `_ssh_target_from_uri`
            # return "", which LibvirtBackend._ssh reads as "run it locally" — a
            # typo would silently execute against the wrong machine.
            if not urlparse(v).scheme.startswith("qemu"):
                raise ValueError(
                    f"libvirt_uri {v!r} must be a qemu URI, e.g. "
                    "qemu+ssh://user@host/system (or qemu:///system for local)"
                )
            return v

        @field_validator("gpu_pci_addresses")
        @classmethod
        def _are_pci_addresses(cls, v: list[str]) -> list[str]:
            for addr in v:
                if not _PCI_RE.match(addr):
                    raise ValueError(
                        f"gpu_pci_address {addr!r} must be domain:bus:device.function, "
                        "e.g. 0000:01:00.0 (see `lspci -D`)"
                    )
            return v

        @model_validator(mode="after")
        def _capacity_is_declared_one_way(self):
            if self.gpu_pci_addresses and self.max_slots is not None:
                raise ValueError(
                    f"host {self.name!r}: set either gpu_pci_addresses (one slot per "
                    "GPU) or max_slots (CPU capacity), not both"
                )
            return self

    class _Backend(_Strict):
        name: str = ""  # defaults to the pool name
        type: Literal["openstack", "libvirt"] = "openstack"
        vm_prefix: str = ""  # defaults to husk-<slug(pool name)>
        image_name: str = ""
        image_ref: str = ""
        min_ready: int = Field(1, ge=0)
        max_total: int = Field(2, ge=1)
        # OpenStack-only (optional for the libvirt backend)
        cloud: str = ""
        flavor_name: str = ""
        network_name: str = ""
        keypair: str = ""
        rebuild_microversion: str = "2.79"
        # libvirt-only (optional for the OpenStack backend)
        hosts: list[_Host] = []

        @model_validator(mode="after")
        def _ready_fits_in_total(self):
            if self.min_ready > self.max_total:
                raise ValueError(
                    f"min_ready ({self.min_ready}) exceeds max_total "
                    f"({self.max_total}) — the warm pool can never reach it"
                )
            return self

    class _Timeouts(_Strict):
        poll_interval_sec: float = Field(30, gt=0)
        idle_timeout_sec: float = Field(1800, gt=0)
        startup_grace_sec: float = Field(300, gt=0)
        max_job_duration_sec: float = Field(21600, gt=0)

    class _Pool(_Strict):
        name: str  # pool identity → backend.name + default vm_prefix
        target: _Target  # the one org/repo this pool serves
        runner: _Runner
        backend: _Backend = Field(default_factory=_Backend)
        timeouts: _Timeouts = Field(default_factory=_Timeouts)

        @model_validator(mode="after")
        def _backend_fields_match_its_type(self):
            """Cross-check the pool against the backend it names.

            `type` selects which fields mean anything; without this, OpenStack keys
            on a libvirt pool (or the reverse) parse fine and are then ignored at
            runtime, so the pool comes up subtly misconfigured instead of failing.
            These run here, not in the backend constructors, because those need
            libvirt-python / a live cloud connection — neither of which a config
            typo should have to wait for."""
            b, r = self.backend, self.runner
            if b.type == "libvirt":
                if stray := [
                    k
                    for k in ("cloud", "flavor_name", "network_name", "keypair")
                    if getattr(b, k)
                ]:
                    raise ValueError(
                        f"{', '.join(stray)} are OpenStack-only, but this pool is "
                        'type = "libvirt"'
                    )
                if not b.hosts:
                    raise ValueError(
                        'type = "libvirt" needs at least one [[pool.backend.hosts]]'
                    )
                seen: set[str] = set()
                for h in b.hosts:
                    if h.name in seen:
                        raise ValueError(f"duplicate host name {h.name!r}")
                    seen.add(h.name)
                    # Every host needs an image from somewhere: its own override, or
                    # the backend's OCI ref / qcow2 name.
                    if not (h.image_ref or h.image_name or b.image_ref or b.image_name):
                        raise ValueError(
                            f"host {h.name!r}: no image source — set [pool.backend]."
                            "image_ref (OCI) or image_name (a qcow2 already in the "
                            "host's storage pool)"
                        )
                if r.gpu and not any(h.gpu_pci_addresses for h in b.hosts):
                    raise ValueError(
                        "runner.gpu = true but no host declares gpu_pci_addresses — "
                        "the slots would boot without a GPU attached"
                    )
            else:
                if b.hosts:
                    raise ValueError(
                        'hosts are libvirt-only, but this pool is type = "openstack"'
                    )
                if missing := [
                    k
                    for k in ("cloud", "flavor_name", "network_name")
                    if not getattr(b, k)
                ]:
                    raise ValueError(f'type = "openstack" needs {", ".join(missing)}')
                if not (b.image_ref or b.image_name):
                    raise ValueError(
                        "no image source: set [pool.backend].image_ref (OCI) or "
                        "image_name (a Glance image name)"
                    )
            # node_exporter only exists in the golden image, so scrape_cidr on a
            # stock-image pool would open :9100 to a port with nothing behind it.
            # Fail loudly rather than silently not collecting the metrics someone
            # just asked for.
            if r.scrape_cidr and not r.prebaked:
                raise ValueError(
                    "scrape_cidr requires prebaked = true (node_exporter is baked "
                    "into the golden image; a stock image has none)"
                )
            return self

    class _Controller(_Strict):
        lock_path: str = "/tmp/huskd.lock"
        http_addr: str = "127.0.0.1:9100"
        shrink_ticks: int = Field(3, ge=1)
        advertise_addr: str = ""
        image_cache_dir: str = ""

        @field_validator("http_addr")
        @classmethod
        def _http_addr_is_bindable(cls, v: str) -> str:
            # Otherwise a bad port raises a bare ValueError from parse_addr at
            # serve time — after the lock is taken and the pools have started.
            if not v.strip():
                raise ValueError("http_addr must be set (it is huskd's only surface)")
            return _check_addr("http_addr", v)

        @field_validator("advertise_addr")
        @classmethod
        def _advertise_addr_is_a_host_port(cls, v: str) -> str:
            return _check_addr("advertise_addr", v) if v.strip() else v

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
        try:
            private_key = Path(s.github.private_key_path).read_text()
        except OSError as e:
            # Almost always a k8s Secret that isn't mounted, or is mounted 0400 for
            # a different uid. Say which, rather than raising a bare OSError.
            raise RuntimeError(
                f"[github].private_key_path {s.github.private_key_path!r} could not "
                f"be read: {e.strerror} — check the Secret is mounted there and "
                "readable by the huskd uid"
            ) from None
    if not private_key or "PRIVATE KEY" not in private_key:
        raise RuntimeError(
            "GitHub App private key not configured: set HUSK_GITHUB__PRIVATE_KEY "
            "(PEM contents) or [github].private_key_path (a path to the .pem the "
            "App settings page generated)"
        )

    if not s.pool:
        # The old-flat-format case is already caught upstream by `_check_top_level`,
        # which sees the stray [runner]/[backend] tables; this is the genuinely
        # empty file.
        raise RuntimeError(
            "no [[pool]] defined: huskd needs at least one pool (each with its own "
            "[pool.runner] and [pool.backend])"
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
