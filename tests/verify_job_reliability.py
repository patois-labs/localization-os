"""Job reliability verification (S1 engine redesign, items 2/3/5; offline, hermetic).

  * LEASE HEARTBEAT (DB queue): a job that runs far longer than its lease keeps being renewed by
    the worker's process, so the LIVE recovery sweep (recover(at_boot=False), lease-only) never
    reclaims it: exactly one execution, attempts == 1. A claim whose row was reclaimed stops
    being renewed.
  * TRANSIENT RETRY: a handler raising jobs.TransientJobError goes back to QUEUED with
    retries+1 and a "Retry n of m" message, is re-dispatched after a backoff and completes;
    booked cost accumulates across attempts; retries exhausted -> FAILED with the last error and
    the retry count; the DB queue row honors not_before (claim skips it until due); status_dict
    carries ``retries``. Both backends.
  * GRACEFUL DRAIN: jobs.drain(timeout) stops claiming, waits, then hands still-RUNNING jobs back
    (Job QUEUED + started_at cleared; DB row pending, worker/lease cleared); a handler that
    finishes late does NOT overwrite the handed-back job; resume_after_drain() lets a worker
    re-run it to COMPLETED. Both backends.

Final line: JOB RELIABILITY OK. ASCII-only.
"""

import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
os.environ.setdefault("LOC_TMS_JOB_WORKERS", "2")
from tests._bootstrap import init_env

init_env("loctms_jobrel_")

from sqlmodel import Session, select

from app import ports
from app.db import engine, init_db
from app.models import Job, JobQueue, JobQueueState, JobStatus
from app.ports import jobqueue
from app.services import jobs, settings, spend

RUNS: dict[str, int] = {}
_RUNS_LOCK = threading.Lock()
DONE = (JobStatus.COMPLETED, JobStatus.FAILED)


def _count(kind):
    with _RUNS_LOCK:
        RUNS[kind] = RUNS.get(kind, 0) + 1
        return RUNS[kind]


def _wait(pred, timeout=30.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"condition not met in {timeout}s")


def _wait_status(jid, want, timeout=30.0):
    return _wait(lambda: jobs.get(jid) if jobs.get(jid).status in want else None, timeout)


def _qrows(jid):
    with Session(engine) as s:
        return s.exec(select(JobQueue).where(JobQueue.job_id == jid).order_by(JobQueue.id)).all()


def _long(payload, progress):
    _count("rel_long")
    time.sleep(float(payload.get("secs", 1)))
    progress(1, 1, "finishing")
    return {"ok": True}


jobs.HANDLERS["rel_long"] = _long


def _flaky_factory(kind, fail_times, cost=0.25):
    calls = {"n": 0}

    def flaky(payload, progress):
        calls["n"] += 1
        spend._note_booked(cost)
        if calls["n"] <= fail_times:
            raise jobs.TransientJobError("upstream 503")
        return {"ok": True}

    jobs.HANDLERS[kind] = flaky
    return calls


def _use_db(lease=2, poll=0.1):
    settings.set_many({"job_queue_backend": "db"}, actor="test")
    q = jobqueue.DbJobQueue(lease_seconds=lease, poll_seconds=poll)
    ports.register("jobqueue", q)
    return q


def _use_memory():
    settings.set_many({"job_queue_backend": "memory"}, actor="test")
    ports.unregister("jobqueue")


def test_helpers():
    assert issubclass(jobs.TransientJobError, RuntimeError)
    assert jobs.TransientJobError("x", retry_after=3).retry_after == 3

    class Resp:
        status_code = 503

    class HttpErr(Exception):
        def __init__(self, code):
            self.response = type("R", (), {"status_code": code})()

    assert jobs.is_transient(jobs.TransientJobError("x"))
    assert jobs.is_transient(TimeoutError()) and jobs.is_transient(ConnectionResetError())
    assert jobs.is_transient(HttpErr(429)) and jobs.is_transient(HttpErr(504))
    assert not jobs.is_transient(HttpErr(400)) and not jobs.is_transient(ValueError("x"))
    assert jobs.RETRY_MAX == 4
    for n in range(4):
        d = jobs.retry_delay(n)
        base = jobs.RETRY_BASE_S * (2**n)
        assert 0.7 * base <= d <= 1.3 * base, (n, d)
    assert jobs.retry_delay(0, retry_after=7) == 7
    assert jobs.retry_delay(0, retry_after=10_000) <= jobs.RETRY_CAP_S
    print("  helpers: TransientJobError, is_transient, retry_delay (2,4,8,16 s with jitter)")


def test_transient_retry(label):
    saved = (jobs.RETRY_BASE_S, jobs.RETRY_CAP_S)
    jobs.RETRY_BASE_S, jobs.RETRY_CAP_S = 0.3, 5.0
    try:
        calls = _flaky_factory(f"rel_flaky_{label}", 2)
        jid = jobs.submit(f"rel_flaky_{label}", {})
        j = _wait_status(jid, DONE, 40)
        assert j.status == JobStatus.COMPLETED, (j.status, j.error)
        assert calls["n"] == 3, calls
        assert (j.retries or 0) == 2, j.retries
        assert abs((j.cost_usd or 0) - 0.75) < 1e-9, j.cost_usd  # cost kept across attempts
        assert jobs.status_dict(j)["retries"] == 2
        rows = _qrows(jid)
        if label == "db":
            # The worker releases the queue row only after _execute returns: wait for DONE.
            deadline = time.time() + 10
            while time.time() < deadline and not (rows and rows[0].state == JobQueueState.DONE):
                time.sleep(0.05)
                rows = _qrows(jid)
            assert len(rows) == 1 and rows[0].attempts == 3, [(r.state, r.attempts) for r in rows]
            assert rows[0].state == JobQueueState.DONE and "503" in (rows[0].last_error or "")
        print(f"  {label}: 2 transient failures -> 3 calls, COMPLETED, retries=2, cost 0.75 kept")

        # Retries exhausted -> FAILED with the last error and the retry count.
        calls = _flaky_factory(f"rel_dead_{label}", 99)
        jobs.RETRY_BASE_S = 0.05
        jid = jobs.submit(f"rel_dead_{label}", {})
        j = _wait_status(jid, DONE, 40)
        assert j.status == JobStatus.FAILED, j.status
        assert calls["n"] == jobs.RETRY_MAX + 1, calls
        assert j.retries == jobs.RETRY_MAX
        assert "upstream 503" in (j.error or "") and "4 of 4" in (j.error or ""), j.error
        assert abs((j.cost_usd or 0) - 0.25 * (jobs.RETRY_MAX + 1)) < 1e-9, j.cost_usd
        print(f"  {label}: retries exhausted -> FAILED after {calls['n']} calls: {j.error}")
    finally:
        jobs.RETRY_BASE_S, jobs.RETRY_CAP_S = saved


def test_drain(label):
    kind = "rel_long"
    RUNS.pop(kind, None)
    jid = jobs.submit(kind, {"secs": 3})
    _wait_status(jid, (JobStatus.RUNNING,), 15)
    handed = jobs.drain(timeout=0.5)
    assert handed == 1, handed
    j = jobs.get(jid)
    assert j.status == JobStatus.QUEUED and j.started_at is None, (j.status, j.started_at)
    if label == "db":
        rows = _qrows(jid)
        assert len(rows) == 1 and rows[0].state == JobQueueState.PENDING, rows
        assert rows[0].worker is None and rows[0].lease_expires_at is None, rows[0]
    # The handler thread cannot be killed: let it finish; its late write must be discarded.
    _wait(lambda: not jobs._RUNS and RUNS.get(kind) == 1, 10)
    time.sleep(3.3)
    j = jobs.get(jid)
    assert j.status == JobStatus.QUEUED, f"late handler flipped a drained job to {j.status}"
    assert not j.result, j.result
    if label == "db":
        rows = _qrows(jid)
        assert len(rows) == 1 and rows[0].state == JobQueueState.PENDING, [r.state for r in rows]
    print(f"  {label}: drain handed 1 RUNNING job back (QUEUED); late handler finish discarded")
    # Next boot (memory: recover re-enqueues QUEUED rows) / a resumed worker re-runs it.
    jobs.resume_after_drain()
    if label == "memory":
        jobqueue.get().recover()
    j = _wait_status(jid, DONE, 30)
    assert j.status == JobStatus.COMPLETED and RUNS.get(kind) == 2, (j.status, RUNS)
    print(f"  {label}: after resume the job re-ran to COMPLETED (2 executions)")
    # drain with nothing running is a clean no-op
    assert jobs.drain(timeout=0.2) == 0
    jobs.resume_after_drain()


def test_heartbeat_and_not_before():
    q = _use_db(lease=2, poll=0.1)
    kind = "rel_long"
    RUNS.pop(kind, None)
    jid = jobs.submit(kind, {"secs": 6})
    stop = threading.Event()

    def sweeper():
        while not stop.is_set():
            q.recover(at_boot=False)
            time.sleep(0.4)

    th = threading.Thread(target=sweeper, daemon=True)
    th.start()
    seen = set()
    try:
        _wait_status(jid, (JobStatus.RUNNING,), 10)

        def lease():
            r = _qrows(jid)[0]
            if r.lease_expires_at:
                seen.add(r.lease_expires_at)
            return jobs.get(jid).status in DONE

        _wait(lease, 30, step=0.3)
    finally:
        time.sleep(0.8)
        stop.set()
    rows = _qrows(jid)
    assert jobs.get(jid).status == JobStatus.COMPLETED
    assert RUNS.get(kind) == 1, f"long job dispatched {RUNS.get(kind)} times"
    assert rows[0].attempts == 1, rows[0].attempts
    assert len(seen) >= 3, f"lease was renewed only {len(seen)} distinct times"
    print(f"  heartbeat: 6 s job on a 2 s lease ran once ({len(seen)} lease renewals seen)")

    # Park the workers (drain flag) so this section drives the adapter by hand.
    jobs._draining.set()
    time.sleep(1.3)
    with Session(engine) as s:
        job = Job(kind="rel_long", status=JobStatus.QUEUED, payload={"secs": 0})
        s.add(job)
        s.commit()
        s.refresh(job)
        jid2 = job.id
    q.enqueue(jid2)
    assert q.claim("w-nb", timeout=3) == jid2
    q.requeue(jid2, delay=1.5, error="upstream 503")
    row = _qrows(jid2)[0]
    assert row.state == JobQueueState.PENDING and row.not_before is not None
    assert row.last_error == "upstream 503" and row.attempts == 1, row
    t0 = time.time()
    assert q.claim("w-nb", timeout=0.6) is None, "claim must skip a row whose not_before is future"
    assert q.claim("w-nb", timeout=5) == jid2
    assert time.time() - t0 >= 1.3, time.time() - t0
    assert _qrows(jid2)[0].attempts == 2
    # renew extends the lease; a reclaimed (no longer running) row stops being renewed
    before = _qrows(jid2)[0].lease_expires_at
    time.sleep(0.05)
    q.renew(jid2, "w-nb")
    assert _qrows(jid2)[0].lease_expires_at > before
    q.hand_back([jid2])
    r = _qrows(jid2)[0]
    assert r.state == JobQueueState.PENDING and r.worker is None and r.lease_expires_at is None
    q.renew(jid2, "w-nb")  # no running row: no-op, nothing resurrected
    assert _qrows(jid2)[0].state == JobQueueState.PENDING
    print("  db adapter: claim skips not_before in the future; renew extends; hand_back resets")
    # clear the manual row so parked/resumed workers do not run it
    with Session(engine) as s:
        for r in _qrows(jid2):
            s.delete(s.get(JobQueue, r.id))
        s.commit()
    jobs._draining.clear()


def main() -> None:
    init_db()
    test_helpers()
    _use_memory()
    jobs.ensure_workers()
    test_transient_retry("memory")
    test_drain("memory")
    test_heartbeat_and_not_before()  # switches to the DB backend
    test_transient_retry("db")
    test_drain("db")
    _use_memory()
    print("JOB RELIABILITY OK")


if __name__ == "__main__":
    main()
