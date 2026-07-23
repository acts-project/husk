"""Single-controller flock guard."""

from __future__ import annotations

import asyncio

import pytest

from husk.lock import LockHeld, SingleControllerLock


def test_flock_double_run_refused(tmp_path):
    pidfile = str(tmp_path / "huskd.lock")
    first = SingleControllerLock(pidfile)
    first.acquire()
    try:
        with pytest.raises(LockHeld):
            SingleControllerLock(pidfile).acquire()
    finally:
        first.release()


def test_lock_reacquire_after_release(tmp_path):
    pidfile = str(tmp_path / "huskd.lock")
    with SingleControllerLock(pidfile):
        pass
    # released on exit → a fresh acquire succeeds
    with SingleControllerLock(pidfile):
        pass


def test_wait_acquire_uncontended_returns_immediately(tmp_path):
    pidfile = str(tmp_path / "huskd.lock")
    lock = SingleControllerLock(pidfile)
    assert asyncio.run(lock.wait_acquire()) is True
    lock.release()


def test_wait_acquire_blocks_until_holder_releases(tmp_path):
    """The rolling-update handoff: a standby waits on the lock and takes it the
    moment the incumbent lets go."""
    pidfile = str(tmp_path / "huskd.lock")
    held = SingleControllerLock(pidfile)
    held.acquire()

    async def go():
        waiter = SingleControllerLock(pidfile)
        task = asyncio.create_task(waiter.wait_acquire(poll_interval=0.01))
        # Give it a few poll cycles to prove it does NOT acquire while held.
        await asyncio.sleep(0.05)
        assert not task.done()
        held.release()  # incumbent steps down
        assert await asyncio.wait_for(task, timeout=1.0) is True
        waiter.release()

    asyncio.run(go())


def test_wait_acquire_gives_up_when_stop_fires(tmp_path):
    """A pod told to shut down before it ever won the lock returns False rather
    than blocking forever."""
    pidfile = str(tmp_path / "huskd.lock")
    held = SingleControllerLock(pidfile)
    held.acquire()

    async def go():
        stop = asyncio.Event()
        waiter = SingleControllerLock(pidfile)
        task = asyncio.create_task(waiter.wait_acquire(stop, poll_interval=0.01))
        await asyncio.sleep(0.05)
        assert not task.done()
        stop.set()
        assert await asyncio.wait_for(task, timeout=1.0) is False

    asyncio.run(go())
    held.release()
