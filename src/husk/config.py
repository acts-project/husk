"""Configuration model.

The runtime config the controller consumes is a set of plain frozen dataclasses
(import-light, pydantic-free). `load_config()` builds them from TOML + env +
k8s-mounted secret files using pydantic-settings — pydantic is scoped to that
loading boundary only (see `load_config`), never to the hot-path value objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GithubConfig:
    repo: str
    token: str  # resolved secret (PAT); never logged


@dataclass(frozen=True)
class RunnerConfig:
    version: str
    labels: list[str]
    runner_group_id: int

    @property
    def url(self) -> str:
        return (
            f"https://github.com/actions/runner/releases/download/v{self.version}/"
            f"actions-runner-linux-x64-{self.version}.tar.gz"
        )


@dataclass(frozen=True)
class BackendConfig:
    name: str
    type: str
    cloud: str
    image_name: str
    flavor_name: str
    network_name: str
    keypair: str
    rebuild_microversion: str
    min_ready: int
    max_total: int


@dataclass(frozen=True)
class TimeoutsConfig:
    poll_interval_sec: float = 30
    idle_timeout_sec: float = 1800
    startup_grace_sec: float = 300
    max_job_duration_sec: float = 21600


@dataclass(frozen=True)
class ControllerConfig:
    lock_path: str = "/tmp/huskd.lock"
    state_path: str = "/tmp/huskd-state.json"
    http_addr: str = (
        "127.0.0.1:9100"  # huskd serves /status /metrics /healthz; "" disables
    )
    shrink_ticks: int = 3


@dataclass(frozen=True)
class Config:
    github: GithubConfig
    runner: RunnerConfig
    backend: BackendConfig
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    controller: ControllerConfig = field(default_factory=ControllerConfig)


def load_config(path: str, *, secrets_dir: str | None = None) -> Config:
    """Build `Config` from a TOML file, overlaid by env vars, with the PAT read
    from a file (k8s Secret mount). Precedence: **env > TOML > defaults**.

    pydantic-settings is imported lazily here so the rest of the package (and the
    unit tests) stay pydantic-free; pydantic is scoped to this loading boundary.
    """
    import os
    from pathlib import Path

    from pydantic import BaseModel
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

    class _Backend(BaseModel):
        name: str
        type: str = "openstack"
        cloud: str
        image_name: str
        flavor_name: str
        network_name: str
        keypair: str
        rebuild_microversion: str = "2.79"
        min_ready: int = 1
        max_total: int = 2

    class _Timeouts(BaseModel):
        poll_interval_sec: float = 30
        idle_timeout_sec: float = 1800
        startup_grace_sec: float = 300
        max_job_duration_sec: float = 21600

    class _Controller(BaseModel):
        lock_path: str = "/tmp/huskd.lock"
        state_path: str = "/tmp/huskd-state.json"
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
        runner: _Runner
        backend: _Backend
        timeouts: _Timeouts = _Timeouts()
        controller: _Controller = _Controller()

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

    return Config(
        github=GithubConfig(repo=s.github.repo, token=token),
        runner=RunnerConfig(
            version=s.runner.version,
            labels=list(s.runner.labels),
            runner_group_id=s.runner.runner_group_id,
        ),
        backend=BackendConfig(
            name=s.backend.name,
            type=s.backend.type,
            cloud=s.backend.cloud,
            image_name=s.backend.image_name,
            flavor_name=s.backend.flavor_name,
            network_name=s.backend.network_name,
            keypair=s.backend.keypair,
            rebuild_microversion=s.backend.rebuild_microversion,
            min_ready=s.backend.min_ready,
            max_total=s.backend.max_total,
        ),
        timeouts=TimeoutsConfig(
            poll_interval_sec=s.timeouts.poll_interval_sec,
            idle_timeout_sec=s.timeouts.idle_timeout_sec,
            startup_grace_sec=s.timeouts.startup_grace_sec,
            max_job_duration_sec=s.timeouts.max_job_duration_sec,
        ),
        controller=ControllerConfig(
            lock_path=s.controller.lock_path,
            state_path=s.controller.state_path,
            http_addr=s.controller.http_addr,
            shrink_ticks=s.controller.shrink_ticks,
        ),
    )
