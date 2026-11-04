"""TARGET T1: durable job queue by default, with lease heartbeats, retry with backoff, a short-task pool that long
jobs cannot starve, and a graceful drain.

Why (2026-10-22): a queue-based design puts every background task on a durable queue with
retries at 1, 2 and 4 minutes, a separate pool for long pipeline tasks so exports and notifications never wait
behind them, and a shutdown that hands unfinished tasks back to the queue. Ours defaults to an in-memory queue
(work in flight is lost on restart), the opt-in DB queue has a fixed one-hour lease with no heartbeat (a longer
job can be dispatched twice), there is no transient-retry policy, and one worker pool serves every job kind.

Done when every check passes:
  1. job_queue_backend defaults to "db" on a fresh install (memory stays available as an opt-out).
  2. A job that runs longer than its lease is NOT dispatched a second time while its worker is alive
     (the worker renews the lease; the live recovery sweep leaves it alone).
  3. A handler raising jobs.TransientJobError is retried with backoff and completes; attempts are recorded.
  4. With every long-pool worker busy on long jobs, a short job completes in under 2 s (separate pools; long
     kinds declared in jobs.LONG_KINDS).
  5. jobs.drain(timeout) hands RUNNING jobs back to the queue (QUEUED) instead of leaving them to be failed at
     the next boot.
"""

import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("LOC_TMS_JOB_WORKERS", "2")
from tests._bootstrap import init_env

init_env("loctms_t1_")

from _targets import check, finish, need, wait_until  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import ports  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import Job, JobQueue, JobStatus  # noqa: E402
from app.ports import jobqueue  # noqa: E402
from app.services import jobs, settings  # noqa: E402

init_db()
RUNS: dict[str, int] = {}
GATE = threading.Event()


def _count(kind):
    RUNS[kind] = RUNS.get(kind, 0) + 1


def _long(payload, progress):
    _count("arch_long")
    time.sleep(float(payload.get("secs", 5)))
    return {"ok": True}


def _short(payload, progress):
    _count("arch_short")
    return {"ok": True}


jobs.HANDLERS["arch_long"] = _long
jobs.HANDLERS["arch_short"] = _short


def _attempts(jid):
    with Session(engine) as s:
        row = s.exec(select(JobQueue).where(JobQueue.job_id == jid)).first()
        return row.attempts if row else None


def t_default_backend():
    v = settings.get("job_queue_backend")
    return v == "db", f"default job_queue_backend={v!r}"


def _arm_db(lease=2):
    settings.set_many({"job_queue_backend": "db"}, actor="target")
    ports.register("jobqueue", jobqueue.DbJobQueue(lease_seconds=lease, poll_seconds=0.1))
    jobs.ensure_workers()


def t_heartbeat():
    _arm_db(lease=2)
    RUNS.clear()
    jid = jobs.submit("arch_long", {"secs": 6})
    stop = threading.Event()

    def sweeper():  # the live recovery sweep a server runs: reclaims EXPIRED leases only
        while not stop.is_set():
            jobqueue.get().recover(at_boot=False)
            time.sleep(0.5)

    th = threading.Thread(target=sweeper, daemon=True)
    th.start()
    done = wait_until(lambda: (jobs.get(jid).status in (JobStatus.COMPLETED, JobStatus.FAILED)) or None, 20)
    time.sleep(1.0)
    stop.set()
    runs, att = RUNS.get("arch_long", 0), _attempts(jid)
    ok = bool(done) and runs == 1 and att == 1
    return ok, f"executions={runs} attempts={att} status={jobs.get(jid).status}"


def t_transient_retry():
    err = need(jobs, "TransientJobError")
    calls = {"n": 0}

    def flaky(payload, progress):
        calls["n"] += 1
        if calls["n"] < 3:
            raise err("upstream 503")
        return {"ok": True}

    jobs.HANDLERS["arch_flaky"] = flaky
    jid = jobs.submit("arch_flaky", {})
    j = wait_until(lambda: jobs.get(jid) if jobs.get(jid).status in (JobStatus.COMPLETED, JobStatus.FAILED) else None,
                   60)
    ok = j is not None and j.status == JobStatus.COMPLETED and calls["n"] == 3
    return ok, f"status={getattr(j, 'status', None)} calls={calls['n']} attempts={_attempts(jid)}"


def t_short_not_starved():
    long_kinds = getattr(jobs, "LONG_KINDS", None)
    if long_kinds is not None and "arch_long" not in long_kinds and hasattr(long_kinds, "add"):
        long_kinds.add("arch_long")
    ids = [jobs.submit("arch_long", {"secs": 6}) for _ in range(int(os.environ["LOC_TMS_JOB_WORKERS"]))]
    time.sleep(0.5)
    t0 = time.time()
    sid = jobs.submit("arch_short", {})
    j = wait_until(lambda: jobs.get(sid) if jobs.get(sid).status == JobStatus.COMPLETED else None, 15)
    took = time.time() - t0
    wait_until(lambda: all(jobs.get(i).status == JobStatus.COMPLETED for i in ids), 30)
    pools = "" if long_kinds is not None else " (jobs.LONG_KINDS not implemented: one shared pool)"
    return (j is not None and took < 2.0), f"short job took {took:.1f}s with every worker on a long job{pools}"


def t_graceful_drain():
    drain = need(jobs, "drain")
    jid = jobs.submit("arch_long", {"secs": 8})
    wait_until(lambda: jobs.get(jid).status == JobStatus.RUNNING, 10)
    drain(timeout=1.0)
    st = jobs.get(jid).status
    return st == JobStatus.QUEUED, f"after drain the running job is {st}"


check("1 durable queue is the default", t_default_backend)
check("2 lease heartbeat: no double dispatch of a long job", t_heartbeat)
check("3 transient errors retried with backoff", t_transient_retry)
check("4 short jobs not starved by long jobs", t_short_not_starved)
check("5 graceful drain requeues running jobs", t_graceful_drain)
finish("T1", "durable queue")
