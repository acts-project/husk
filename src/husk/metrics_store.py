"""Long-term persistence for huskd's event-time counters and histograms.

Without this, every huskd restart resets every counter to zero. Prometheus copes
— `rate()` and `increase()` treat a drop as a reset — but the long-horizon
questions quietly stop working: `increase(husk_action_failures_total[30d])` after
a deploy only sees failures since the deploy, and a p95 recycle time over a month
is really a p95 over "however long this pod has been up". Since huskd restarts on
every config change (there is no hot reload), that window can be short.

So the accumulated state is written to a small JSON file — meant for a modest PVC
mounted into the pod — and folded back in at startup.

Scope is deliberately narrow: **only `husk.metrics.Metrics` is persisted**, never
the snapshot-derived half. Snapshot metrics describe the present and are re-derived
from live state on every scrape, so persisting them would at best be redundant and
at worst resurrect a slot that no longer exists. Because no event-time instrument
carries a per-slot label (see `husk.metrics`), the labelsets here are bounded by
config and the file stays small and bounded no matter how long huskd runs or how
many slots pass through it: 3.4 KB for two pools after 500 recycles, and 11.7 KB
for six pools with every labelset populated, which is about the ceiling.

Two properties matter for correctness:

* **Writes are atomic.** A temp file in the same directory followed by
  `os.replace`, so a pod killed mid-write leaves either the old file or the new
  one, never a truncated one that fails to parse on the next boot.
* **A bad file is never fatal.** Corrupt JSON, a schema-version mismatch, or
  changed bucket boundaries all mean "start this metric from zero, loudly". huskd
  has no back-compat obligations, so there is no migration path here on purpose:
  half-restored data is worse than a clean reset, because a counter that silently
  loses part of its history is indistinguishable from one that is simply low.

Failure to save is likewise non-fatal — a full or unwritable PVC must never take
down a runner fleet over a bookkeeping file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time

from husk.metrics import Metrics

log = logging.getLogger("husk.metrics_store")

# Bumped whenever the on-disk layout changes. A mismatch discards the file
# wholesale rather than attempting a migration.
SCHEMA_VERSION = 1

# How often the daemon flushes accumulated state while running. The file is small
# and the write is atomic, so this is cheap; it exists so an ungraceful kill (OOM,
# node eviction) loses at most this much history rather than everything since the
# last clean shutdown.
SAVE_INTERVAL_S = 60.0


class MetricsStore:
    """Loads and saves a `Metrics` instance's accumulated state at `path`."""

    def __init__(self, path: str, metrics: Metrics) -> None:
        self.path = path
        self._metrics = metrics

    def load(self) -> bool:
        """Fold any previously saved state into the live instruments.

        Returns whether anything was restored. A missing file is the normal
        first-run case and is not an error."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            log.info("no metrics state at %s; starting from zero", self.path)
            return False
        except (OSError, json.JSONDecodeError):
            log.warning(
                "metrics state at %s is unreadable; starting from zero",
                self.path,
                exc_info=True,
            )
            return False

        version = doc.get("version")
        if version != SCHEMA_VERSION:
            log.warning(
                "metrics state at %s is schema v%s, expected v%d; starting from zero",
                self.path,
                version,
                SCHEMA_VERSION,
            )
            return False

        instruments = self._metrics.instruments
        stored = doc.get("metrics", {})
        # An instrument present on disk but gone from the code was renamed or
        # removed; drop it. One present in code but absent on disk is new, and
        # correctly starts at zero.
        for name, payload in stored.items():
            instrument = instruments.get(name)
            if instrument is None:
                log.info("metrics state: dropping unknown metric %s", name)
                continue
            try:
                instrument.load_dict(payload)
            except Exception:
                log.warning(
                    "metrics state: %s could not be restored; it starts from zero",
                    name,
                    exc_info=True,
                )
        age = max(0.0, time.time() - float(doc.get("saved_at", 0.0)))
        log.info(
            "restored metrics state from %s (%d metric(s), %.0fs old)",
            self.path,
            len(stored),
            age,
        )
        return True

    def save(self) -> bool:
        """Atomically write current state. Never raises; returns success."""
        doc = {
            "version": SCHEMA_VERSION,
            "saved_at": time.time(),
            "metrics": {
                name: instrument.to_dict()
                for name, instrument in self._metrics.instruments.items()
            },
        }
        try:
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            os.makedirs(directory, exist_ok=True)
            # Same directory as the target: os.replace is only atomic within a
            # filesystem, and /tmp is very often a different one from the PVC.
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".husk-metrics-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                # Includes the cancellation path: leaving a stray temp file on a
                # small PVC is exactly how it eventually fills up.
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except Exception:
            log.warning("could not save metrics state to %s", self.path, exc_info=True)
            return False
        log.debug("saved metrics state to %s", self.path)
        return True
