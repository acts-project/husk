"""Small asyncio helpers shared by the long-lived loops (poller, reconcile)."""

from __future__ import annotations

import asyncio


async def sleep_or_stop(stop: asyncio.Event, timeout: float) -> None:
    """Sleep `timeout` seconds, returning early if `stop` is set.

    Every long-lived loop's inter-tick wait goes through this, so shutdown is
    prompt instead of waiting out a full poll interval."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        pass  # normal cadence — the interval elapsed without a stop
