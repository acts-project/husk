"""The optional Quart web dashboard: index renders, /status streams the pool list.
Skipped unless the `web` extra (quart) is installed."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("quart")

from conftest import make_runner, make_slot  # noqa: E402
from husk.slot import SlotState  # noqa: E402
from husk.snapshot import ControllerState  # noqa: E402
from husk.web import make_app  # noqa: E402


def _snaps():
    return [
        ControllerState.from_classified(
            generation=2,
            backend="pool-a",
            min_ready=1,
            max_total=2,
            desired_total=1,
            classified=[
                (
                    make_slot(name="husk-a-1", status="ACTIVE"),
                    make_runner(name="husk-a-1-c0"),
                    SlotState.IDLE,
                )
            ],
        )
    ]


def test_dashboard_index_renders():
    app = make_app(lambda: _snaps())

    async def go():
        r = await app.test_client().get("/")
        assert r.status_code == 200
        body = (await r.get_data()).decode()
        assert "husk" in body
        assert "/events" in body  # the page subscribes to the SSE stream

    asyncio.run(go())


def test_status_returns_pool_list():
    app = make_app(lambda: _snaps())

    async def go():
        r = await app.test_client().get("/status")
        assert r.status_code == 200
        data = json.loads(await r.get_data())
        assert [p["backend"] for p in data] == ["pool-a"]
        assert data[0]["slots"][0]["name"] == "husk-a-1"

    asyncio.run(go())
