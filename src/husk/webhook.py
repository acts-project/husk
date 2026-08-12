"""GitHub `workflow_job` webhooks — signature verification, payload parsing, and
the in-memory registry the dashboard reads job identity out of.

huskd stays poll-driven: the runner poller (`husk.poller`) remains the source of
truth for *whether* a slot is busy, and reconcile sizing is untouched by anything
here. What the poll cannot answer is *which job* a busy slot is running — the
runners list returns only `{id, name, status, busy}`, with no job, workflow or run
anywhere in it. That is the whole gap this module fills, and the reason it exists
at all.

Three deliberate non-goals, each of which would otherwise look like an omission:

* **It does not drive scaling.** `workflow_job(queued)` is recorded for metrics
  only; `DemandRegistry` is never written from here. Sizing stays
  `min(max_total, busy + min_ready)` off the poll, so a lost or forged delivery
  cannot change fleet size — the blast radius of this whole module is "the
  dashboard shows a stale job name".
* **It does not persist.** Job assignments are ephemeral (bounded by
  `max_job_duration_sec`) and reconstructible: after a restart, a busy slot shows
  no job until its next one starts. Paying for durability here would cost huskd
  its "no durable state, rebuild from provider tags" property for a display field.
* **It is not the security boundary's only layer, but it is the real one.** The
  OpenShift Route scopes the path, but path matching is a prefix match and the
  endpoint is internet-facing by design. `verify()` below is what actually keeps
  strangers out.

Keyed by runner NAME rather than slot id, because that is the only identifier
GitHub and husk share: runners are named `f"{vm}-c{cycle}"` (see `husk.slot`), so
the webhook's `runner_name` joins straight onto a matched runner at snapshot-build
time without this module needing to know anything about slots or backends.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

log = logging.getLogger("husk.webhook")

_SIG_HEADER = "X-Hub-Signature-256"
_SIG_PREFIX = "sha256="

# A job whose `completed` delivery never arrives would otherwise pin its runner
# name in the registry forever, and the dashboard would show a job that finished
# hours ago. GitHub retries failed deliveries, but huskd can be down for the whole
# retry window, so a TTL is the backstop. Deliberately generous: it only has to
# beat "wrong for ever", and evicting a genuinely long job early just falls back
# to the plain busy indicator.
DEFAULT_JOB_TTL_S = 6 * 3600.0

# Queued entries have no runner assigned yet, so they cannot be evicted by
# completion in the way in-progress jobs are — a `queued` job that is cancelled
# before it is ever picked up produces no further event naming it. A shorter TTL
# plus a hard cap keeps the map bounded: every repo in an installed org can add to
# it, so it must not be able to grow without limit.
DEFAULT_QUEUED_TTL_S = 3600.0
MAX_QUEUED = 5000


def verify(body: bytes, header: str | None, secret: str | None) -> bool:
    """Is `body` signed with `secret`, per `X-Hub-Signature-256`?

    Called on the RAW request bytes before any JSON parsing — re-serializing a
    parsed payload does not reproduce the signed bytes, and parsing first would
    mean running a JSON decoder over unauthenticated input.

    An unset `secret` returns False rather than True: "no webhook configured"
    must refuse deliveries, never wave them through. Same for a missing or
    malformed header, so a stripped header cannot bypass the check.
    """
    if not secret or not header or not header.startswith(_SIG_PREFIX):
        return False
    expected = _SIG_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # compare_digest, not ==, so the comparison time does not leak how much of a
    # forged signature was correct.
    return hmac.compare_digest(expected, header)


@dataclass(frozen=True)
class JobInfo:
    """One in-progress Actions job, as last reported for a runner name."""

    job_id: int
    name: str  # the job's name within its workflow ("build (ubuntu-24.04)")
    workflow: str  # the workflow's name ("CI")
    repo: str  # "owner/name" — jobs come from repos even on org targets
    run_url: str  # html_url of the RUN, which is what a human wants to open
    started_at: float  # wall-clock, for the dashboard's elapsed column
    epoch: float  # when huskd recorded it — drives TTL eviction


def parse_job(payload: dict[str, Any]) -> tuple[str, JobInfo | None, str | None]:
    """Pull `(action, JobInfo | None, runner_name | None)` out of a `workflow_job`.

    Returns the action even when no `JobInfo` can be built, because the action is
    what the caller dispatches on: a `completed` delivery is meaningful precisely
    for its runner name, and needs no job detail.

    Tolerant of missing fields by design. This is parsing a third party's evolving
    payload, and the cost of a KeyError here is a 500 back to GitHub plus a
    delivery retried until it fails permanently — far worse than a dashboard cell
    reading "unknown".
    """
    action = str(payload.get("action") or "")
    job = payload.get("workflow_job") or {}
    if not isinstance(job, dict):
        return action, None, None

    runner_name = job.get("runner_name") or None
    if runner_name is not None:
        runner_name = str(runner_name)

    job_id = job.get("id")
    if not isinstance(job_id, int):
        return action, None, runner_name

    repo = ""
    repository = payload.get("repository")
    if isinstance(repository, dict):
        repo = str(repository.get("full_name") or "")

    # `run_url` on the job object is an API url; the html_url on the job points at
    # the job's own view within the run, which is the more useful landing page.
    run_url = str(job.get("html_url") or "")

    started = job.get("started_at")
    started_at = _parse_iso8601(started) if isinstance(started, str) else 0.0

    return (
        action,
        JobInfo(
            job_id=job_id,
            name=str(job.get("name") or ""),
            workflow=str(job.get("workflow_name") or ""),
            repo=repo,
            run_url=run_url,
            started_at=started_at,
            epoch=time.time(),
        ),
        runner_name,
    )


def job_labels(payload: dict[str, Any]) -> tuple[str, ...]:
    """The `runs-on` labels of a `workflow_job`, sorted and deduplicated.

    Sorted so that the same label SET always produces the same metric series
    regardless of the order the workflow author wrote them in — otherwise
    `[self-hosted, husk-x64]` and `[husk-x64, self-hosted]` are two series
    describing one queue.
    """
    job = payload.get("workflow_job") or {}
    if not isinstance(job, dict):
        return ()
    labels = job.get("labels")
    if not isinstance(labels, list):
        return ()
    return tuple(sorted({str(x) for x in labels if x}))


def _parse_iso8601(s: str) -> float:
    """GitHub's `2024-01-01T00:00:00Z` → epoch seconds; 0.0 if unparseable."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class JobRegistry:
    """Thread-safe `runner_name → JobInfo`, plus a bounded queued-job tally.

    Deliberately shaped like `husk.demand.DemandRegistry` — a plain lock around
    two dicts — because it has the same job: a hand-off point between a producer
    on one task (the webhook handler) and readers on others (snapshot build,
    metrics scrape). The lock is uncontended in practice and makes the sharing
    correct without anyone reasoning about the event loop.

    Both maps self-evict on TTL. Nothing outside this class calls `sweep()`: every
    reader sweeps first, so a registry nobody reads cannot leak, and a registry
    that is read stays bounded without a background task to own and shut down.
    """

    def __init__(
        self,
        *,
        job_ttl_s: float = DEFAULT_JOB_TTL_S,
        queued_ttl_s: float = DEFAULT_QUEUED_TTL_S,
        max_queued: int = MAX_QUEUED,
    ) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, JobInfo] = {}
        # (target_key, labels) → [epochs of still-queued jobs]. Keyed by label SET
        # rather than by job so the metric can report queue depth per `runs-on`,
        # which is the dimension that maps onto pools; job ids are held only to
        # deduplicate repeat deliveries of the same queued job.
        self._queued: dict[tuple[str, tuple[str, ...]], dict[int, float]] = {}
        self._job_ttl_s = job_ttl_s
        self._queued_ttl_s = queued_ttl_s
        self._max_queued = max_queued

    def start(self, runner_name: str, info: JobInfo) -> None:
        """Record that `runner_name` is running `info` (a `queued`→`in_progress`).

        Stamps `epoch` at insertion rather than trusting the caller's: the TTL
        measures how long HUSKD has held the entry, which is the thing it is
        bounding. Taking the payload's value would also make a `JobInfo` built
        without an explicit epoch (0.0) instantly older than any TTL and vanish on
        the very next read — a silent no-op that looks like a lost delivery.
        """
        with self._lock:
            self._jobs[runner_name] = replace(info, epoch=time.time())
            self._sweep_locked()

    def finish(self, runner_name: str, job_id: int | None = None) -> None:
        """Drop `runner_name`'s job on completion.

        `job_id` guards against a late `completed` for a PREVIOUS job evicting the
        one that runner has since started — deliveries are not ordered, and on a
        busy runner the gap between one job ending and the next starting is small.
        """
        with self._lock:
            cur = self._jobs.get(runner_name)
            if cur is not None and (job_id is None or cur.job_id == job_id):
                del self._jobs[runner_name]
            self._sweep_locked()

    def get(self, runner_name: str) -> JobInfo | None:
        with self._lock:
            self._sweep_locked()
            return self._jobs.get(runner_name)

    def enqueue(self, target_key: str, labels: tuple[str, ...], job_id: int) -> None:
        """Record a queued job. Metrics only — this never influences sizing."""
        with self._lock:
            self._sweep_locked()
            if self._total_queued_locked() >= self._max_queued:
                # Drop rather than evict-oldest: the cap exists to bound memory
                # against a runaway workflow, and under that condition the depth
                # metric is already saying "very large". Losing precision at the
                # top of a flood is the right failure.
                return
            self._queued.setdefault((target_key, labels), {})[job_id] = time.time()

    def dequeue(self, target_key: str, labels: tuple[str, ...], job_id: int) -> None:
        """A queued job started or was cancelled — it is no longer waiting."""
        with self._lock:
            bucket = self._queued.get((target_key, labels))
            if bucket is not None:
                bucket.pop(job_id, None)
                if not bucket:
                    del self._queued[(target_key, labels)]
            self._sweep_locked()

    def queued_depth(self) -> dict[tuple[str, tuple[str, ...]], int]:
        """`(target, labels) → count` of jobs waiting for a runner."""
        with self._lock:
            self._sweep_locked()
            return {k: len(v) for k, v in self._queued.items() if v}

    def jobs(self) -> dict[str, JobInfo]:
        """A copy of the whole map, for one consistent read per snapshot build."""
        with self._lock:
            self._sweep_locked()
            return dict(self._jobs)

    def _total_queued_locked(self) -> int:
        return sum(len(v) for v in self._queued.values())

    def _sweep_locked(self) -> None:
        now = time.time()
        for name, info in list(self._jobs.items()):
            if now - info.epoch > self._job_ttl_s:
                log.debug("evicting stale job for runner %s (ttl)", name)
                del self._jobs[name]
        for key, bucket in list(self._queued.items()):
            for job_id, epoch in list(bucket.items()):
                if now - epoch > self._queued_ttl_s:
                    del bucket[job_id]
            if not bucket:
                del self._queued[key]
