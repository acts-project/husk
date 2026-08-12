"""`workflow_job` webhooks: signature verification, the job registry's lifecycle
and TTLs, the snapshot join, and the `POST /webhook` route's contract.

The signature tests are the load-bearing ones. This is the only huskd endpoint
that accepts input and the only one exposed outside CERN, so every way a delivery
can fail to be authentic gets its own case — a regression here is not a wrong
dashboard cell, it is an open endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest

from conftest import make_runner, make_slot, render_metrics
from husk.metrics import Metrics
from husk.slot import SlotState
from husk.snapshot import ControllerState
from husk.web import make_app
from husk.webhook import JobInfo, JobRegistry, job_labels, parse_job, verify

SECRET = "s3cret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def payload(
    action="in_progress",
    *,
    job_id=42,
    runner_name="husk-a-1-c2",
    labels=("self-hosted", "husk-x64"),
    org="acts-project",
    repo="acts-project/acts",
):
    job = {
        "id": job_id,
        "name": "build (ubuntu-24.04)",
        "workflow_name": "CI",
        "html_url": f"https://github.com/{repo}/actions/runs/99/job/{job_id}",
        "started_at": "2026-08-12T10:00:00Z",
        "labels": list(labels),
    }
    if runner_name is not None:
        job["runner_name"] = runner_name
    out = {"action": action, "workflow_job": job, "repository": {"full_name": repo}}
    if org:
        out["organization"] = {"login": org}
    return out


# --------------------------------------------------------------- signatures


def test_verify_accepts_a_correct_signature():
    body = b'{"hello":"world"}'
    assert verify(body, sign(body), SECRET) is True


@pytest.mark.parametrize(
    "desc,body,header,secret",
    [
        ("tampered body", b'{"hello":"evil"}', sign(b'{"hello":"world"}'), SECRET),
        ("wrong secret", b"x", sign(b"x", "other"), SECRET),
        ("missing header", b"x", None, SECRET),
        ("empty header", b"x", "", SECRET),
        ("no sha256= prefix", b"x", sign(b"x")[7:], SECRET),
        ("sha1 prefix", b"x", "sha1=" + sign(b"x")[7:], SECRET),
        ("not hex", b"x", "sha256=zzzz", SECRET),
        ("truncated digest", b"x", sign(b"x")[:20], SECRET),
    ],
)
def test_verify_rejects(desc, body, header, secret):
    assert verify(body, header, secret) is False, desc


def test_verify_rejects_everything_when_no_secret_is_configured():
    """An unset secret must REFUSE, never wave deliveries through — the whole
    endpoint's trust rests on this being fail-closed."""
    body = b"x"
    assert verify(body, sign(body), None) is False
    assert verify(body, sign(body), "") is False
    assert verify(body, None, None) is False


# ------------------------------------------------------------------ parsing


def test_parse_job_extracts_identity_and_runner():
    action, info, runner = parse_job(payload())
    assert action == "in_progress"
    assert runner == "husk-a-1-c2"
    assert info is not None
    assert info.job_id == 42
    assert info.name == "build (ubuntu-24.04)"
    assert info.workflow == "CI"
    assert info.repo == "acts-project/acts"
    assert info.run_url.endswith("/actions/runs/99/job/42")
    assert info.started_at > 0


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"action": "in_progress"},
        {"action": "in_progress", "workflow_job": None},
        {"action": "in_progress", "workflow_job": "not-a-dict"},
        {"action": "in_progress", "workflow_job": {}},  # no id
        {"action": "in_progress", "workflow_job": {"id": "not-an-int"}},
    ],
)
def test_parse_job_tolerates_malformed_payloads(bad):
    """A KeyError here would 500 back to GitHub and burn the delivery's retries;
    a missing field must degrade to None instead."""
    action, info, _ = parse_job(bad)
    assert isinstance(action, str)
    assert info is None


def test_parse_job_survives_a_missing_started_at():
    p = payload()
    del p["workflow_job"]["started_at"]
    _, info, _ = parse_job(p)
    assert info is not None and info.started_at == 0.0


def test_job_labels_are_sorted_and_deduplicated():
    """Same label SET, same series — order and repeats are the workflow author's
    business, not a new metric dimension."""
    a = job_labels({"workflow_job": {"labels": ["b", "a", "b"]}})
    b = job_labels({"workflow_job": {"labels": ["a", "b"]}})
    assert a == b == ("a", "b")
    assert job_labels({}) == ()
    assert job_labels({"workflow_job": {"labels": "nope"}}) == ()


# ----------------------------------------------------------------- registry


def _info(job_id=1, name="build", epoch=0.0):
    return JobInfo(
        job_id=job_id,
        name=name,
        workflow="CI",
        repo="o/r",
        run_url="https://example/1",
        started_at=100.0,
        epoch=epoch,
    )


def test_registry_start_get_finish():
    reg = JobRegistry()
    assert reg.get("husk-a-1-c2") is None
    reg.start("husk-a-1-c2", _info(name="build"))
    assert reg.get("husk-a-1-c2").name == "build"
    reg.finish("husk-a-1-c2")
    assert reg.get("husk-a-1-c2") is None


def test_finish_for_a_stale_job_id_does_not_evict_the_current_one():
    """Deliveries are not ordered: a late `completed` for job 1 must not clear the
    job 2 that runner has already started, or the dashboard goes blank mid-job."""
    reg = JobRegistry()
    reg.start("r", _info(job_id=2, name="second"))
    reg.finish("r", job_id=1)
    assert reg.get("r").name == "second"
    reg.finish("r", job_id=2)
    assert reg.get("r") is None


def test_jobs_are_evicted_past_their_ttl(monkeypatch):
    """A `completed` that never arrives must not pin a job forever."""
    reg = JobRegistry(job_ttl_s=100.0)
    now = [1000.0]
    monkeypatch.setattr("husk.webhook.time.time", lambda: now[0])
    reg.start("r", _info(epoch=now[0]))
    now[0] += 99
    assert reg.get("r") is not None
    now[0] += 2  # past the TTL
    assert reg.get("r") is None


def test_queued_depth_counts_by_target_and_labelset():
    reg = JobRegistry()
    reg.enqueue("org:acts", ("a", "b"), 1)
    reg.enqueue("org:acts", ("a", "b"), 2)
    reg.enqueue("org:acts", ("c",), 3)
    assert reg.queued_depth() == {("org:acts", ("a", "b")): 2, ("org:acts", ("c",)): 1}
    reg.dequeue("org:acts", ("a", "b"), 1)
    assert reg.queued_depth()[("org:acts", ("a", "b"))] == 1


def test_queued_entries_are_deduplicated_by_job_id():
    """GitHub redelivers; the same queued job twice is still one job waiting."""
    reg = JobRegistry()
    reg.enqueue("t", ("a",), 7)
    reg.enqueue("t", ("a",), 7)
    assert reg.queued_depth() == {("t", ("a",)): 1}


def test_emptied_queue_buckets_disappear_entirely():
    """A drained labelset must stop producing a series, not report 0 forever."""
    reg = JobRegistry()
    reg.enqueue("t", ("a",), 1)
    reg.dequeue("t", ("a",), 1)
    assert reg.queued_depth() == {}


def test_queued_map_is_bounded(monkeypatch):
    """Every repo in an installed org can enqueue; the map must not grow without
    limit on a runaway workflow."""
    reg = JobRegistry(max_queued=10)
    for i in range(50):
        reg.enqueue("t", ("a",), i)
    assert sum(reg.queued_depth().values()) == 10


def test_queued_entries_expire(monkeypatch):
    """A job cancelled while queued may never be named again."""
    now = [1000.0]
    monkeypatch.setattr("husk.webhook.time.time", lambda: now[0])
    reg = JobRegistry(queued_ttl_s=60.0)
    reg.enqueue("t", ("a",), 1)
    now[0] += 61
    assert reg.queued_depth() == {}


# ------------------------------------------------------------ snapshot join


def _snap(*, busy=True, jobs=None, runner="husk-a-1-c2"):
    classified = [
        (
            make_slot(id="vm-1", name="husk-a-1", status="ACTIVE", cycle=2),
            make_runner(name=runner, status="online", busy=busy),
            SlotState.BUSY if busy else SlotState.IDLE,
        )
    ]
    return ControllerState.from_classified(
        generation=1,
        backend="pool-a",
        min_ready=1,
        max_total=4,
        desired_total=1,
        classified=classified,
        jobs=jobs,
    )


def test_snapshot_joins_a_job_onto_its_runner():
    snap = _snap(jobs={"husk-a-1-c2": _info(name="build (ubuntu)")})
    v = snap.slots[0]
    assert v.job_name == "build (ubuntu)"
    assert v.job_url == "https://example/1"
    assert v.job_started_at == 100.0


def test_busy_slot_with_no_job_keeps_its_busy_flag():
    """The restart gap: busy from the poll, no webhook seen. Must not read idle."""
    snap = _snap(jobs={})
    v = snap.slots[0]
    assert v.busy is True
    assert v.job_name is None


def test_idle_slot_never_shows_a_job():
    """A stale entry inside its TTL must not paint a job onto a slot the poll says
    is idle — the poll stays authoritative."""
    snap = _snap(busy=False, jobs={"husk-a-1-c2": _info()})
    assert snap.slots[0].job_name is None


def test_job_for_a_different_runner_is_not_joined():
    snap = _snap(jobs={"husk-b-9-c0": _info()})
    assert snap.slots[0].job_name is None


def test_snapshot_serializes_job_fields():
    snap = _snap(jobs={"husk-a-1-c2": _info(name="build")})
    assert snap.to_dict()["slots"][0]["job_name"] == "build"


# -------------------------------------------------------------------- route


def post(
    body: dict,
    *,
    reg=None,
    secret=SECRET,
    configured=SECRET,
    event="workflow_job",
    sig=True,
    raw=None,
):
    """One delivery against a real app, synchronously.

    The suite has no pytest-asyncio, and standing up a server (`serve_in_thread`)
    is more machinery than a request/response assertion needs — Quart's test
    client driven through `asyncio.run` exercises the same handler."""
    app = make_app(lambda: [], jobs=reg or JobRegistry(), webhook_secret=configured)
    payload_bytes = raw if raw is not None else json.dumps(body).encode()
    headers = {"X-GitHub-Event": event}
    if sig:
        headers["X-Hub-Signature-256"] = sign(payload_bytes, secret)

    async def go():
        return await app.test_client().post(
            "/webhook", data=payload_bytes, headers=headers
        )

    return asyncio.run(go())


def test_valid_delivery_is_accepted():
    assert post(payload()).status_code == 204


def test_unsigned_delivery_is_rejected():
    assert post(payload(), sig=False).status_code == 401


def test_wrongly_signed_delivery_is_rejected():
    assert post(payload(), secret="not-the-secret").status_code == 401


def test_signature_is_checked_before_the_event_type():
    """Order matters: an unsigned request must 401, not 204, even for an event
    huskd would otherwise ignore — otherwise the endpoint answers probes."""
    assert post({}, event="push", sig=False).status_code == 401


def test_other_events_are_ignored_not_errored():
    """204, not 4xx: a subscription huskd does not handle is GitHub doing as it
    was told, and 4xx would show as failed deliveries in the App UI."""
    assert post({"zen": "hi"}, event="ping").status_code == 204


def test_signed_but_unparseable_body_is_a_400():
    assert post({}, raw=b"{not json").status_code == 400


def test_signed_non_object_body_is_a_400():
    """A valid-JSON scalar is not a payload; `parse_job` must never see it."""
    assert post({}, raw=b'"just-a-string"').status_code == 400


def test_route_refuses_everything_when_no_secret_is_configured():
    """An unconfigured webhook must reject, not accept — and must still ROUTE, so
    a misconfigured deployment reads as 401 rather than as a 404 routing bug."""
    assert post(payload(), configured=None).status_code == 401


def test_lifecycle_queued_to_in_progress_to_completed():
    reg = JobRegistry()
    post(payload("queued", runner_name=None), reg=reg)
    assert sum(reg.queued_depth().values()) == 1

    post(payload("in_progress"), reg=reg)
    # Leaving the queue and taking a runner are the same instant.
    assert reg.queued_depth() == {}
    assert reg.get("husk-a-1-c2").job_id == 42

    post(payload("completed"), reg=reg)
    assert reg.get("husk-a-1-c2") is None


def test_job_cancelled_while_queued_leaves_no_queue_entry():
    """Straight from queued to completed, never assigned a runner — the queued
    entry must go now rather than linger until its TTL."""
    reg = JobRegistry()
    post(payload("queued", runner_name=None), reg=reg)
    post(payload("completed", runner_name=None), reg=reg)
    assert reg.queued_depth() == {}


def test_rejected_delivery_does_not_touch_the_registry():
    """The point of the signature: a forged delivery changes nothing."""
    reg = JobRegistry()
    post(payload("in_progress"), reg=reg, secret="wrong")
    assert reg.get("husk-a-1-c2") is None


# ------------------------------------------------------------------ metrics


def test_queued_depth_is_exposed():
    reg = JobRegistry()
    reg.enqueue("org:acts-project", ("husk-x64", "self-hosted"), 1)
    reg.enqueue("org:acts-project", ("husk-x64", "self-hosted"), 2)
    text = render_metrics(jobs=reg)
    assert 'husk_jobs_queued{labels="husk-x64,self-hosted"' in text
    assert 'target="org:acts-project"} 2.0' in text


def test_no_registry_means_no_queue_series():
    """No webhook configured is "huskd does not know the depth", which is a
    missing series — not a 0 that would look like an empty queue."""
    assert "husk_jobs_queued" not in render_metrics()


def test_delivery_outcomes_are_counted():
    m = Metrics()
    m.webhook_deliveries.inc("accepted")
    m.webhook_deliveries.inc("rejected")
    m.webhook_deliveries.inc("rejected")
    text = render_metrics(metrics=m)
    assert 'husk_webhook_deliveries_total{result="rejected"} 2.0' in text
