"""Single-controller lock — a flock'd pidfile so two `huskd`s can't fight over
the same managed-by=husk slot set."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os

log = logging.getLogger("husk.lock")


class LockHeld(Exception):
    """Raised when another process already holds the controller lock."""


class SingleControllerLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self._fd = None

    def acquire(self) -> None:
        fd = open(self.path, "w")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            other = ""
            try:
                with open(self.path) as r:
                    other = r.read().strip()
            except OSError:
                pass
            fd.close()
            raise LockHeld(
                f"another huskd holds the lock at {self.path}"
                + (f" (pid {other})" if other else "")
            )
        fd.truncate(0)
        fd.write(str(os.getpid()))
        fd.flush()
        self._fd = fd  # held for process lifetime; kernel releases on exit/crash

    async def wait_acquire(
        self, stop: asyncio.Event | None = None, *, poll_interval: float = 1.0
    ) -> bool:
        """Block until the lock is acquired, retrying while another huskd holds it.

        Returns True once acquired, or False if `stop` fires first. This is how a
        rolling-update standby waits: it binds and serves the dashboard immediately,
        then sits here until the outgoing pod drains and releases the lock, at which
        point it becomes the sole reconciler. Unlike `acquire()`, contention is not
        an error — it is the normal handoff.

        The retry is a poll rather than an inotify/blocking-flock wait on purpose:
        the lock lives on a shared network filesystem (CephFS) where a held
        `LOCK_EX` can outlive the holder by the MDS session timeout, so a coarse
        poll that re-tests `LOCK_NB` is both simpler and robust to that reclaim
        delay."""
        waited = False
        while True:
            try:
                self.acquire()
                if waited:
                    log.info("controller lock acquired; becoming the active reconciler")
                return True
            except LockHeld as e:
                if not waited:
                    log.info("%s; waiting for it to release", e)
                    waited = True
                if stop is not None and stop.is_set():
                    return False
                if stop is None:
                    await asyncio.sleep(poll_interval)
                else:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=poll_interval)
                        return False  # stop fired while we were waiting
                    except asyncio.TimeoutError:
                        pass  # poll interval elapsed; re-test the lock

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            finally:
                self._fd.close()
                self._fd = None

    def __enter__(self) -> "SingleControllerLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
