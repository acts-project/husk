"""Shared test helpers: a controllable clock, slot/runner/config builders, a
controller factory wired to the in-memory fakes, and a live Quart server."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
import urllib.request

import pytest

from husk.config import (
    BackendConfig,
    Config,
    ControllerConfig,
    GithubConfig,
    RunnerConfig,
    TimeoutsConfig,
)
from husk.controller import Controller
from husk.fake_backend import FakeBackend, FakeGitHub
from husk.slot import Runner, Slot


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_config(
    *,
    min_ready: int = 1,
    max_total: int = 2,
    startup_grace: float = 300,
    idle_timeout: float = 1800,
    max_job_duration: float = 21600,
    shrink_ticks: int = 3,
) -> Config:
    return Config(
        github=GithubConfig(repo="acts-project/husk-test", token="x"),
        runner=RunnerConfig(
            version="2.334.0", labels=["self-hosted"], runner_group_id=1
        ),
        backend=BackendConfig(
            name="fake",
            type="fake",
            cloud="cern",
            image_name="ALMA10 - x86_64",
            flavor_name="m2.small",
            network_name="CERN_NETWORK",
            keypair="acts-gha",
            rebuild_microversion="2.79",
            min_ready=min_ready,
            max_total=max_total,
        ),
        timeouts=TimeoutsConfig(
            poll_interval_sec=30,
            idle_timeout_sec=idle_timeout,
            startup_grace_sec=startup_grace,
            max_job_duration_sec=max_job_duration,
        ),
        controller=ControllerConfig(lock_path="/tmp/x.lock", shrink_ticks=shrink_ticks),
    )


def make_slot(
    id: str = "vm-1",
    name: str = "husk-1",
    status: str = "ACTIVE",
    task_state: str | None = None,
    created_at: float = 0.0,
    cycle: int = 0,
    provisioned_at: float | None = None,
    image_stale: bool = False,
    ip: str | None = None,
    host: str | None = None,
    active_image: str | None = None,
) -> Slot:
    return Slot(
        id=id,
        name=name,
        status=status,
        task_state=task_state,
        created_at=created_at,
        flavor_id="flavor-current",
        image_id="image-current",
        cycle=cycle,
        provisioned_at=provisioned_at,
        image_stale=image_stale,
        ip=ip,
        host=host,
        active_image=active_image,
    )


def make_runner(
    id: int = 1, name: str = "husk-1-c0", status: str = "online", busy: bool = False
) -> Runner:
    return Runner(id=id, name=name, status=status, busy=busy)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_controller(
    backend: FakeBackend, github: FakeGitHub, config: Config, clock
) -> Controller:
    return Controller(backend, github, config, clock=clock)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def serve_in_thread(provider):
    """Run the real Quart app over `provider` on a background event loop, yielding
    its base URL. Mirrors how huskd serves it (reconcile on a thread, server on a
    loop) but inverted for tests: here the server is on the side thread."""
    from husk.web import make_app, serve_app

    port = _free_port()
    app = make_app(provider)
    loop = asyncio.new_event_loop()
    stop: asyncio.Event | None = None
    ready = threading.Event()

    def run():
        nonlocal stop
        asyncio.set_event_loop(loop)
        stop = asyncio.Event()
        ready.set()
        loop.run_until_complete(
            serve_app(app, "127.0.0.1", port, shutdown_trigger=stop.wait)
        )
        loop.close()

    t = threading.Thread(target=run, name="test-web", daemon=True)
    t.start()
    ready.wait(timeout=5)
    base = f"http://127.0.0.1:{port}"
    # Wait until the listener accepts requests before handing the URL out.
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/status", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    try:
        yield base
    finally:
        loop.call_soon_threadsafe(stop.set)
        t.join(timeout=5)
