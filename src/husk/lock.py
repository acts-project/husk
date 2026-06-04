"""Single-controller lock — a flock'd pidfile so two `huskd`s can't fight over
the same managed-by=husk slot set."""

from __future__ import annotations

import fcntl
import os


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
