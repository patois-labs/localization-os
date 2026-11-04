"""Job pools and the durable-by-default queue (S2 engine redesign, target T1; offline, hermetic).

The five T1 checks, promoted from tests/arch_targets/target_t1_durable_queue.py, plus the pools:

  * DEFAULT: job_queue_backend is "db" on a fresh install; configure_at_boot arms DbJobQueue;
    "memory" stays a valid opt-out.
  * HEARTBEAT: a job running far longer than its lease is not dispatched twice by the live sweep.
  * TRANSIENT RETRY: TransientJobError is retried with backoff and completes; attempts recorded.
  * SHORT NOT STARVED: with every long worker busy on a long job, a short job finishes in under 2 s.
  * DRAIN: jobs.drain hands RUNNING jobs back to QUEUED.
  * POOLS: jobs.LONG_KINDS (mutable, read live) decides a kind's pool; a short-only claim never
    returns a long kind; a long worker takes long kinds first, any kind when no long is waiting;
    both adapters; not_before and requeue keep working with a pool filter; the DB wake-up beats the
    poll interval; the Postgres candidate SELECT carries FOR UPDATE OF q SKIP LOCKED
    (SQL-text check, no Postgres server needed), SQLite does not.
  * WORKERS: WORKER_COUNT long threads (loc-job-N) + SHORT_WORKER_COUNT short threads (loc-sjob-N);
    short threads never run a long kind; long threads do drain the short backlog.
  * CLI: a one-shot CLI process on the durable queue starts no daemon workers; the flag resets after
    cli main() returns.

Final line: JOB POOLS OK. ASCII-only.
"""

import os
import sys
import threading
import time
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
os.environ.setdefault("LOC_TMS_JOB_WORKERS", "2")
os.environ.pop("LOC_TMS_SHORT_JOB_WORKERS", None)
from tests._bootstrap import init_env

init_env("loctms_jobpools_")

from sqlalchemy import text
from sqlmodel import Session, select

from app import ports
from app.db import engine, init_db
from app.models import Job, JobQueue, JobQueueState, JobStatus
from app.ports import jobqueue
from app.services import jobs, settings

RUNS: dict[str, int] = {}
THREADS: dict[str, list[str]] = {}
_LOCK = threading.Lock()
DONE = (JobStatus.COMPLETED, JobStatus.FAILED)


def _note(kind):
    with _LOCK:
        RUNS[kind] = RUNS.get(kind, 0) + 1
        THREADS.setdefault(kind, []).append(threading.current_thread().name)


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


def _long_h(payload, progress):
    _note("pool_long")
    time.sleep(float(payload.get("secs", 1)))
    return {"ok": True}


def _short_h(payload, progress):
    _note("pool_short")
    time.sleep(float(payload.get("secs", 0)))
    return {"ok": True}


jobs.HANDLERS["pool_long"] = _long_h
jobs.HANDLERS["pool_short"] = _short_h
jobs.LONG_KINDS.add("pool_long")


def _mk_job(kind, status=JobStatus.QUEUED):
    with Session(engine) as s:
        job = Job(kind=kind, status=status, payload={})
        s.add(job)
        s.commit()
        s.refresh(job)
        return job.id


def _clear_queue():
    with Session(engine) as s:
        for r in s.exec(select(JobQueue)).all():
            s.delete(r)
        s.commit()


def _use_db(lease=2, poll=0.1):
    settings.set_many({"job_queue_backend": "db"}, actor="test")
    q = jobqueue.DbJobQueue(lease_seconds=lease, poll_seconds=poll)
    ports.register("jobqueue", q)
    return q


# --------------------------------------------------------------------------------------------
# no workers running yet: direct adapter checks
# --------------------------------------------------------------------------------------------
def test_default_and_boot():
    assert settings.get("job_queue_backend") == "db", settings.get("job_queue_backend")
    assert settings.DEFAULTS["job_queue_backend"] == "db"
    assert not jobqueue.DB_BACKEND_ARMED
    jobqueue.configure_at_boot()
    assert jobqueue.DB_BACKEND_ARMED and isinstance(jobqueue.get(), jobqueue.DbJobQueue)
    # the opt-out still works: memory leaves whatever is registered alone and arms nothing
    settings.set_many({"job_queue_backend": "memory"}, actor="test")
    before = jobqueue.get()
    jobqueue.configure_at_boot()
    assert jobqueue.get() is before
    settings.set_many({"job_queue_backend": "db"}, actor="test")
    print("  default backend is db; configure_at_boot arms DbJobQueue; memory stays an opt-out")


def test_pool_for_and_counts():
    assert {"translate_document", "connector_translate", "backfill_embeddings"} <= jobs.LONG_KINDS
    assert isinstance(jobs.LONG_KINDS, set)
    assert jobs.pool_for("translate_document") == "long"
    assert jobs.pool_for("data_export") == "short"
    assert jobs.pool_for("some_new_kind") == "short"
    assert jobs.SHORT_WORKER_COUNT == 2, jobs.SHORT_WORKER_COUNT
    jobs.LONG_KINDS.add("pool_dyn")
    assert jobs.pool_for("pool_dyn") == "long"
    jobs.LONG_KINDS.discard("pool_dyn")
    assert jobs.pool_for("pool_dyn") == "short"
    print("  LONG_KINDS is a live mutable set; pool_for; SHORT_WORKER_COUNT defaults to 2")


def test_db_claim_pools():
    q = _use_db()
    long_id = _mk_job("pool_long")
    short_id = _mk_job("pool_short")
    # the long job is OLDER (lower queue id) than the short one
    q.enqueue(long_id)
    q.enqueue(short_id)
    # a short-only claim never returns a long kind, even though the long row is first in line
    assert q.claim("sw", timeout=0.3, pools=("short",)) == short_id
    assert q.claim("sw", timeout=0.3, pools=("short",)) is None, "short claim returned a long kind"
    # a long-only claim takes the long row
    assert q.claim("lw", timeout=0.3, pools=("long",)) == long_id
    _clear_queue()

    # a long worker (long, short) takes long kinds first even when the short row is older
    # (gate S2 M1: a short-job flood must never starve translation)
    q.enqueue(short_id)
    q.enqueue(long_id)
    assert q.claim("lw", timeout=0.3, pools=("long", "short")) == long_id
    # ... and falls back to the short kind when no long is waiting (work-conserving)
    assert q.claim("lw", timeout=0.3, pools=("long", "short")) == short_id
    _clear_queue()

    # pools=None keeps the pre-S2 order (by id, any kind)
    q.enqueue(long_id)
    q.enqueue(short_id)
    assert q.claim("w", timeout=0.3) == long_id
    assert q.claim("w", timeout=0.3) == short_id
    _clear_queue()

    # LONG_KINDS is read live at claim time: a kind added at runtime changes its pool at once
    jobs.LONG_KINDS.add("pool_short")
    try:
        q.enqueue(short_id)
        assert q.claim("sw", timeout=0.3, pools=("short",)) is None
        assert q.claim("lw", timeout=0.3, pools=("long",)) == short_id
    finally:
        jobs.LONG_KINDS.discard("pool_short")
    _clear_queue()

    # not_before (retry backoff) is honored under a pool filter, and requeue keeps the kind's pool
    q.enqueue(short_id)
    assert q.claim("sw", timeout=0.3, pools=("short",)) == short_id
    q.requeue(short_id, delay=0.8, error="boom")
    assert q.claim("sw", timeout=0.2, pools=("short",)) is None, "claimed before not_before"
    assert q.claim("lw", timeout=2.0, pools=("long", "short")) == short_id
    _clear_queue()
    print("  db claim: short never takes long; long prefers long; live LONG_KINDS; backoff")


def test_db_wakeup():
    q = jobqueue.DbJobQueue(lease_seconds=60, poll_seconds=5.0)
    jid = _mk_job("pool_short")
    out = {}

    def waiter():
        t0 = time.time()
        out["jid"] = q.claim("w", timeout=4.0, pools=("short",))
        out["took"] = time.time() - t0

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.3)
    q.enqueue(jid)
    th.join(6)
    assert out.get("jid") == jid and out["took"] < 2.0, out
    _clear_queue()
    print("  db claim wakes on a local enqueue well inside the 5 s poll interval")


def _claim_queries(m) -> int:
    """Claim candidate queries (SELECT pick or Postgres UPDATE ... pick) in a measurement."""
    return sum(n for sql, n in m.by_statement.items() if "FROM jobqueue q JOIN job j" in sql)


def _waiters(q, n, timeout, pools=("long", "short")):
    out, threads = {}, []
    for i in range(n):

        def run(i=i):
            out[i] = q.claim(f"w{i}", timeout=timeout, pools=pools)

        threads.append(threading.Thread(target=run))
    for t in threads:
        t.start()
    return out, threads


def test_db_counted_wakeup():
    """S7b: one enqueue sends ONE idle claimer to the table, not all of them.
    Before, the wake-up was notify_all: every idle claimer thread of the process ran the claim
    query for every job (a quarter of a worker's CPU under a translate load)."""
    from tests._stmt_counter import StatementCounter

    _clear_queue()
    q = jobqueue.DbJobQueue(lease_seconds=60, poll_seconds=30.0)
    jid = _mk_job("pool_long")
    counter = StatementCounter(engine)
    try:
        out, threads = _waiters(q, 4, timeout=2.5)
        time.sleep(0.4)  # each waiter made its first look and now waits
        with counter.measure() as m:
            q.enqueue(jid)
            assert _wait(lambda: jid in out.values(), timeout=2.0), out
            time.sleep(0.3)
        for t in threads:
            t.join(5)
    finally:
        counter.close()
    assert list(out.values()).count(jid) == 1, out
    assert _claim_queries(m) == 1, (_claim_queries(m), m.top(8))
    _clear_queue()
    print("  db claim: one enqueue wakes exactly one idle claimer (one claim query)")


def test_db_idle_poll_shared():
    """S7b: idle claimers of one process share ONE poll per interval per pool set (before,
    every idle thread polled every interval)."""
    from tests._stmt_counter import StatementCounter

    _clear_queue()
    q = jobqueue.DbJobQueue(lease_seconds=60, poll_seconds=0.2)
    counter = StatementCounter(engine)
    try:
        out, threads = _waiters(q, 4, timeout=1.6)
        time.sleep(0.2)
        with counter.measure() as m:
            time.sleep(1.0)  # about 5 poll intervals, nothing pending
        for t in threads:
            t.join(5)
    finally:
        counter.close()
    assert all(v is None for v in out.values()), out
    assert _claim_queries(m) <= 8, (_claim_queries(m), "4 claimers x 5 intervals was about 20")
    print(f"  db claim: idle claimers share one poll per interval ({_claim_queries(m)} in 1 s)")


def test_wake_routing():
    """S7b: a cross-process "jobs" poke wakes one claimer per job for its pool and NOT the run
    admission scheduler; "runs" wakes only the scheduler; anything else wakes both."""
    from app.services import orchestrator, wake

    calls = []

    class _Q:
        def _notify(self, n=1, pool=None):
            calls.append(("jobs", n, pool))

    real_get, real_notify = jobqueue.get, orchestrator.ADMISSION.notify
    jobqueue.get = lambda: _Q()
    orchestrator.ADMISSION.notify = lambda: calls.append(("runs",))
    try:
        jl, js = {"what": "jobs", "pool": "long"}, {"what": "jobs", "pool": "short"}
        wake._wake_local([jl, jl, js])
        assert sorted(calls) == [("jobs", 1, "short"), ("jobs", 2, "long")], calls
        calls.clear()
        wake._wake_local([{"what": "runs"}])
        assert calls == [("runs",)], calls
        calls.clear()
        wake._wake_local([{"what": "work"}])
        assert sorted(calls, key=str) == [("jobs", 1, None), ("runs",)], calls
        calls.clear()
        wake._wake_local()
        assert sorted(calls, key=str) == [("jobs", 1, None), ("runs",)], calls
    finally:
        jobqueue.get, orchestrator.ADMISSION.notify = real_get, real_notify
    print("  wake: jobs pokes wake claimers by pool and count, runs pokes wake admission only")


def test_postgres_sql_text():
    pg, _ = jobqueue._candidate_sql(("short", "long"), {"a"}, postgres=True)
    lite, _ = jobqueue._candidate_sql(("short", "long"), {"a"}, postgres=False)
    assert pg.endswith("FOR UPDATE OF q SKIP LOCKED"), pg
    assert "SKIP LOCKED" not in lite and "FOR UPDATE" not in lite, lite
    short_only, extra = jobqueue._candidate_sql(("short",), {"b", "a"}, postgres=True)
    assert "j.kind NOT IN :long_kinds" in short_only and extra["long_kinds"] == ["a", "b"]
    assert "SKIP LOCKED" in short_only
    both, _ = jobqueue._candidate_sql(("short", "long"), {"a"}, postgres=False)
    assert "CASE WHEN j.kind IN :long_kinds THEN 1 ELSE 0 END, q.id" in both
    anyp, extra_any = jobqueue._candidate_sql(None, {"a"}, postgres=False)
    assert "ORDER BY q.id LIMIT 1" in anyp and "long_kinds" not in extra_any
    _, extra_empty = jobqueue._candidate_sql(("short",), set(), postgres=False)
    assert extra_empty["long_kinds"] == [""]
    # S7b: the Postgres claim is ONE statement whose scalar pick returns only the row id
    claim = jobqueue._pg_claim_sql(pg)
    assert claim.startswith("UPDATE jobqueue SET state='running'"), claim
    assert "WHERE id = (SELECT q.id FROM jobqueue q JOIN job j" in claim, claim
    assert "q.job_id FROM" not in claim and claim.endswith("RETURNING job_id"), claim
    assert "FOR UPDATE OF q SKIP LOCKED) AND state='pending'" in claim, claim
    # the statements are valid SQL for the active dialect and bind (sqlite here)
    from sqlalchemy import bindparam

    with Session(engine) as s:
        stmt = text(both).bindparams(bindparam("long_kinds", expanding=True))
        s.execute(stmt, {"now": datetime.utcnow(), "long_kinds": ["a"]}).all()
    print("  postgres candidate SELECT carries FOR UPDATE OF q SKIP LOCKED; sqlite does not")


def test_memory_pools():
    q = jobqueue.InMemoryJobQueue()
    assert q.claim("w", timeout=0.1) is None
    q.enqueue(1, pool="long")
    q.enqueue(2, pool="short")
    q.enqueue(3)  # old call shape: default pool is long
    assert q.claim("sw", timeout=0.1, pools=("short",)) == 2
    assert q.claim("sw", timeout=0.1, pools=("short",)) is None, "short claim returned a long job"
    q.enqueue(4, pool="short")
    assert q.claim("lw", timeout=0.1, pools=("long", "short")) == 1  # long first, FIFO
    assert q.claim("lw", timeout=0.1, pools=("long", "short")) == 3  # still long
    assert q.claim("lw", timeout=0.1, pools=("long", "short")) == 4  # then short
    q.release(1)
    # requeue returns a job to its own pool
    q.enqueue(5, pool="short")
    assert q.claim("sw", timeout=0.1, pools=("short",)) == 5
    q.requeue(5)
    assert q.claim("lw", timeout=0.1, pools=("long",)) is None
    assert q.claim("sw", timeout=0.1, pools=("short",)) == 5
    q.requeue(5, delay=0.3)
    assert q.claim("sw", timeout=0.05, pools=("short",)) is None
    assert q.claim("sw", timeout=2.0, pools=("short",)) == 5
    # a blocked claim wakes when a job arrives
    out = {}
    th = threading.Thread(
        target=lambda: out.update(jid=q.claim("w", timeout=5.0, pools=("short",)))
    )
    th.start()
    time.sleep(0.2)
    q.enqueue(9, pool="short")
    th.join(3)
    assert out.get("jid") == 9
    print("  in-memory adapter: per-pool FIFO, long-first for long workers, requeue keeps pool")


def test_memory_recover_pools():
    prev_started = jobs._started
    jobs._started = True  # keep ensure_workers (called by recover) from starting claimers
    old = ports.get("jobqueue")
    try:
        q = jobqueue.InMemoryJobQueue()
        ports.register("jobqueue", q)
        lid = _mk_job("pool_long")
        sid = _mk_job("pool_short")
        q.recover()
        assert q.claim("sw", timeout=0.2, pools=("short",)) == sid
        assert q.claim("sw", timeout=0.2, pools=("short",)) is None
        assert q.claim("lw", timeout=0.2, pools=("long",)) == lid
    finally:
        jobs._started = prev_started
        ports.register("jobqueue", old)
        with Session(engine) as s:
            for j in s.exec(select(Job)).all():
                if j.status == JobStatus.QUEUED:
                    j.status = JobStatus.FAILED
                    s.add(j)
            s.commit()
    print("  in-memory recover re-enqueues each QUEUED job into its kind's pool")


def test_db_recover_keeps_pool_by_kind():
    q = _use_db()
    lid = _mk_job("pool_long", status=JobStatus.RUNNING)
    sid = _mk_job("pool_short", status=JobStatus.RUNNING)
    now = datetime.utcnow()
    with Session(engine) as s:
        for jid, kind in ((lid, "pool_long"), (sid, "pool_short")):
            s.add(
                JobQueue(
                    job_id=jid,
                    kind=kind,
                    payload={},
                    state=JobQueueState.RUNNING,
                    attempts=1,
                    enqueued_at=now,
                    updated_at=now,
                    worker="gone",
                    lease_expires_at=now - timedelta(seconds=5),
                )
            )
        s.commit()
    prev_started = jobs._started
    jobs._started = True
    try:
        assert q.recover(at_boot=False) == 2
    finally:
        jobs._started = prev_started
    assert q.claim("sw", timeout=0.3, pools=("short",)) == sid
    assert q.claim("sw", timeout=0.3, pools=("short",)) is None
    assert q.claim("lw", timeout=0.3, pools=("long",)) == lid
    _clear_queue()
    with Session(engine) as s:
        for jid in (lid, sid):
            j = s.get(Job, jid)
            j.status = JobStatus.FAILED
            s.add(j)
        s.commit()
    print("  db recover: reclaimed rows land back in the right pool by job kind")


def test_cli_no_workers():
    from app.cli.__main__ import main as cli_main

    assert jobqueue.DB_BACKEND_ARMED
    prev_started = jobs._started
    jobs._started = False
    before = {t.name for t in threading.enumerate()}
    try:
        jobs.set_cli_no_workers(True)
        jobs.ensure_workers()
        assert not jobs._started, "CLI process started daemon workers on the durable queue"
        new = {t.name for t in threading.enumerate()} - before
        assert not any(n.startswith(("loc-job-", "loc-sjob-")) for n in new), new
        # main() releases the ban when the command returns (in-process callers get workers back)
        jobs.set_cli_no_workers(True)
        cli_main(["version"])
        assert jobs._cli_no_workers is False
        # the ban is the CLI's only: memory-armed CLI runs are unaffected by the flag
        jobs.set_cli_no_workers(True)
        saved = (jobqueue.DB_BACKEND_ARMED, jobs.WORKER_COUNT, jobs.SHORT_WORKER_COUNT)
        jobqueue.DB_BACKEND_ARMED = False
        jobs.WORKER_COUNT = jobs.SHORT_WORKER_COUNT = 0  # reach the start path without threads
        try:
            jobs.ensure_workers()
            assert jobs._started, "workers must still start when the durable queue is not armed"
        finally:
            jobqueue.DB_BACKEND_ARMED, jobs.WORKER_COUNT, jobs.SHORT_WORKER_COUNT = saved
    finally:
        jobs.set_cli_no_workers(False)
        jobs._started = prev_started
    # run_sync stays synchronous and needs no worker
    jobs.HANDLERS["pool_sync"] = lambda payload, progress: {"ok": True}
    job = jobs.run_sync("pool_sync", {})
    assert job.status == JobStatus.COMPLETED
    print("  cli: no daemon workers on the durable queue; flag resets after main(); run_sync works")


# --------------------------------------------------------------------------------------------
# with workers
# --------------------------------------------------------------------------------------------
def test_worker_threads():
    names = [t.name for t in threading.enumerate()]
    longs = [n for n in names if n.startswith("loc-job-") and n[8:].isdigit()]
    shorts = [n for n in names if n.startswith("loc-sjob-")]
    assert len(longs) == jobs.WORKER_COUNT == 2, longs
    assert len(shorts) == jobs.SHORT_WORKER_COUNT == 2, shorts
    print("  workers: 2 long (loc-job-N) + 2 short (loc-sjob-N) threads")


def t1_heartbeat():
    _use_db(lease=2)
    RUNS.clear()
    jid = jobs.submit("pool_long", {"secs": 6})
    stop = threading.Event()

    def sweeper():  # the live recovery sweep a server runs: reclaims EXPIRED leases only
        while not stop.is_set():
            jobqueue.get().recover(at_boot=False)
            time.sleep(0.5)

    th = threading.Thread(target=sweeper, daemon=True)
    th.start()
    _wait_status(jid, DONE, 25)
    time.sleep(1.0)
    stop.set()
    rows = _qrows(jid)
    assert RUNS.get("pool_long") == 1, RUNS
    assert len(rows) == 1 and rows[0].attempts == 1, [(r.state, r.attempts) for r in rows]
    assert jobs.get(jid).status == JobStatus.COMPLETED
    print("  T1.2 heartbeat: long job renewed, one execution, attempts == 1")


def t1_transient():
    calls = {"n": 0}

    def flaky(payload, progress):
        calls["n"] += 1
        if calls["n"] < 3:
            raise jobs.TransientJobError("upstream 503")
        return {"ok": True}

    jobs.HANDLERS["pool_flaky"] = flaky
    jid = jobs.submit("pool_flaky", {})
    j = _wait_status(jid, DONE, 60)
    rows = _qrows(jid)
    assert j.status == JobStatus.COMPLETED and calls["n"] == 3, (j.status, calls)
    assert rows and rows[-1].attempts == 3, [(r.state, r.attempts) for r in rows]
    print("  T1.3 transient errors retried with backoff; attempts recorded (3)")


def t1_short_not_starved():
    RUNS.clear()
    THREADS.clear()
    ids = [jobs.submit("pool_long", {"secs": 5}) for _ in range(jobs.WORKER_COUNT + 1)]
    _wait(
        lambda: sum(1 for i in ids if jobs.get(i).status == JobStatus.RUNNING) >= jobs.WORKER_COUNT,
        10,
    )
    t0 = time.time()
    sid = jobs.submit("pool_short", {})
    _wait_status(sid, (JobStatus.COMPLETED,), 15)
    took = time.time() - t0
    assert took < 2.0, f"short job took {took:.1f}s behind long jobs"
    _wait(lambda: all(jobs.get(i).status == JobStatus.COMPLETED for i in ids), 40)
    assert all(n.startswith("loc-job-") for n in THREADS["pool_long"]), THREADS["pool_long"]
    print(f"  T1.4 short job done in {took:.1f}s with every long worker busy")


def workers_conserve():
    RUNS.clear()
    THREADS.clear()
    ids = [jobs.submit("pool_short", {"secs": 0.4}) for _ in range(8)]
    _wait(lambda: all(jobs.get(i).status == JobStatus.COMPLETED for i in ids), 30)
    names = set(THREADS["pool_short"])
    assert any(n.startswith("loc-sjob-") for n in names), names
    assert any(n.startswith("loc-job-") for n in names), (
        f"long workers never drained short backlog: {names}"
    )
    print("  work-conserving: idle long workers also drain a short backlog")


def t1_drain():
    jid = jobs.submit("pool_long", {"secs": 8})
    _wait(lambda: jobs.get(jid).status == JobStatus.RUNNING, 10)
    n = jobs.drain(timeout=1.0)
    st = jobs.get(jid).status
    assert st == JobStatus.QUEUED and n >= 1, (st, n)
    rows = _qrows(jid)
    assert rows[-1].state == JobQueueState.PENDING and rows[-1].worker is None, rows[-1].state
    # drain covers both pools: a running SHORT job is handed back too
    jobs.resume_after_drain()
    _wait(lambda: jobs.get(jid).status in DONE, 30)  # the resumed worker re-runs the long job
    sid = jobs.submit("pool_short", {"secs": 6})
    _wait(lambda: jobs.get(sid).status == JobStatus.RUNNING, 10)
    n2 = jobs.drain(timeout=0.5)
    assert jobs.get(sid).status == JobStatus.QUEUED and n2 >= 1, (jobs.get(sid).status, n2)
    jobs.resume_after_drain()
    print("  T1.5 drain hands running jobs back to QUEUED (long and short pools)")


def main() -> None:
    init_db()
    test_default_and_boot()
    test_pool_for_and_counts()
    test_postgres_sql_text()
    test_memory_pools()
    test_memory_recover_pools()
    test_db_claim_pools()
    test_db_wakeup()
    test_db_counted_wakeup()
    test_db_idle_poll_shared()
    test_wake_routing()
    test_db_recover_keeps_pool_by_kind()
    test_cli_no_workers()
    jobs._started = False
    _use_db(lease=2)
    jobs.ensure_workers()
    test_worker_threads()
    t1_heartbeat()
    t1_transient()
    t1_short_not_starved()
    workers_conserve()
    t1_drain()
    print("JOB POOLS OK")


if __name__ == "__main__":
    main()
