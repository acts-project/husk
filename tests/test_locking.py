"""Single-controller flock guard."""

from __future__ import annotations

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
