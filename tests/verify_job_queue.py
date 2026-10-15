"""Durable job-queue backend verification (sprint Train S, STRETCH; offline).

Proves the JobQueue port (app/ports/jobqueue.py) both ways:

  FLAG-OFF (job_queue_backend="memory", the DEFAULT) is BYTE-IDENTICAL to pre-Train-S:
    * jobqueue.get() is the InMemoryJobQueue wrapping a queue.Queue;
    * jobs.submit -> a worker claims off the in-memory queue -> the job COMPLETES;
    * jobs.recover_interrupted still fails a RUNNING row and re-runs a QUEUED one.

  FLAG-ON (job_queue_backend="db"): the durable DB-backed queue (the `jobqueue` table):
    * enqueue -> claim -> _execute -> release round-trip (Job COMPLETED, queue row 'done');
    * ATOMIC single-claim: two claims race one pending job -> exactly ONE wins;
    * RESTART RECOVERY: a 'running' row with an EXPIRED lease returns to 'pending' and its
      Job returns to QUEUED (re-runs), not failed;
    * FAIL-LOUD boot: db backend selected but the table missing -> JobQueueError; and an
      invalid backend value -> JobQueueError.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, blank keys). ASCII-only. Final: JOB QUEUE OK.
"""

import builtins
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jobqueue_")

from sqlmodel import Session, select

from app import ports
from app.db import engine, init_db
from app.models import Job, JobQueue, JobQueueState, JobStatus
from app.ports import jobqueue
from app.services import jobs, settings


def _wait_job(jid, want, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = jobs.get(jid)
        if j is not None and j.status in want:
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not reach {want} in {timeout}s")


def _mk_job(kind="test_jq_noop", status=JobStatus.QUEUED, payload=None):
    with Session(engine) as s:
        job = Job(kind=kind, status=status, payload=payload or {})
        s.add(job)
        s.commit()
        s.refresh(job)
        return job.id


def test_flag_off_memory_default():
    # The default backend is the in-memory adapter -- byte-identical dispatch to pre-Train-S.
    assert settings.get("job_queue_backend") == "memory", settings.get("job_queue_backend")
    q = jobqueue.get()
    assert isinstance(q, jobqueue.InMemoryJobQueue), type(q).__name__
    assert isinstance(q, jobqueue.JobQueue)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name not in {"app.db", "sqlalchemy"}, f"memory imported {name}"
        return original_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=guarded_import):
        jobqueue.configure_at_boot()
    assert "job_queue" not in _health_detail()

    # A real submit -> a worker claims off the in-memory queue -> the job completes. This also
    # starts the two worker threads, which then block on the in-memory queue.get() forever
    # (exactly today's behavior) -- so they never poll the DB backend the next section arms.
    jobs.HANDLERS["test_jq_noop"] = lambda payload, progress: {"ok": True}
    jid = jobs.submit("test_jq_noop", {}, actor="t")
    j = _wait_job(jid, (JobStatus.COMPLETED,))
    assert j.status == JobStatus.COMPLETED, j.status
    print("  flag-off: InMemoryJobQueue default; submit -> claim -> COMPLETED (byte-identical)")

    # recover_interrupted still fails a RUNNING row and re-runs a QUEUED one (the memory
    # queue is lost on restart -> visible-not-silent), routed through the port's recover().
    rid = _mk_job(status=JobStatus.RUNNING)
    qid = _mk_job(status=JobStatus.QUEUED)
    touched = jobs.recover_interrupted()
    assert touched >= 2, touched
    with Session(engine) as s:
        assert s.get(Job, rid).status == JobStatus.FAILED, "RUNNING must fail on restart"
        assert "restart" in (s.get(Job, rid).error or "")
    _wait_job(qid, (JobStatus.COMPLETED,))
    print("  flag-off: recover_interrupted RUNNING->FAILED, QUEUED->re-run (unchanged)")


def test_flag_on_db_backend():
    # Arm the durable DB backend via configure_at_boot (the fail-loud boot path).
    settings.set_many({"job_queue_backend": "db"})
    jobqueue.configure_at_boot()
    q = jobqueue.get()
    assert isinstance(q, jobqueue.DbJobQueue), type(q).__name__
    print("  flag-on: configure_at_boot armed DbJobQueue override")

    # --- enqueue -> claim -> _execute -> release round-trip ---
    jid = _mk_job()
    q.enqueue(jid)
    with Session(engine) as s:
        row = s.exec(select(JobQueue).where(JobQueue.job_id == jid)).first()
        assert row is not None and row.state == JobQueueState.PENDING, row
    claimed = q.claim("w-rt", timeout=5)
    assert claimed == jid, (claimed, jid)
    with Session(engine) as s:
        row = s.exec(select(JobQueue).where(JobQueue.job_id == jid)).first()
        assert row.state == JobQueueState.RUNNING and row.worker == "w-rt", row
        assert row.attempts == 1 and row.lease_expires_at is not None, row
    jobs._execute(jid)  # the worker loop's body -- sets the Job COMPLETED
    q.release(jid)
    with Session(engine) as s:
        assert s.get(Job, jid).status == JobStatus.COMPLETED, "job must complete"
        row = s.exec(select(JobQueue).where(JobQueue.job_id == jid)).first()
        assert row.state == JobQueueState.DONE and row.worker is None, row
    print("  flag-on: enqueue -> claim -> _execute -> release (Job COMPLETED, queue 'done')")

    # --- ATOMIC single-claim: two claims race ONE pending job -> exactly one wins ---
    jid2 = _mk_job()
    q.enqueue(jid2)
    results = {}
    barrier = threading.Barrier(2)

    def _claimer(name):
        barrier.wait()
        results[name] = q.claim(name, timeout=3)

    t1 = threading.Thread(target=_claimer, args=("A",))
    t2 = threading.Thread(target=_claimer, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    winners = [n for n, v in results.items() if v == jid2]
    losers = [n for n, v in results.items() if v is None]
    assert len(winners) == 1 and len(losers) == 1, results
    with Session(engine) as s:
        row = s.exec(select(JobQueue).where(JobQueue.job_id == jid2)).first()
        assert row.state == JobQueueState.RUNNING and row.attempts == 1, row
        # attempts incremented exactly once: the loser's CAS matched zero rows.
    q.release(jid2)  # drain jid2 out of the pending/running pool so the recovery test is clean
    print(f"  flag-on: ATOMIC single-claim -- {winners[0]} won, {losers[0]} got None (attempts=1)")

    # BLOCKER-A: a real exited predecessor on this host, with a different UUID.
    jid3 = _mk_job()  # QUEUED
    q.enqueue(jid3)
    crash_worker = _dead_worker()
    assert q.claim(crash_worker, timeout=5) == jid3  # stamps a fresh future lease
    with Session(engine) as s:  # the worker began executing, then the process died (no release)
        job = s.get(Job, jid3)
        job.status = JobStatus.RUNNING
        job.started_at = datetime.utcnow()
        s.add(job)
        s.commit()
        row = s.exec(select(JobQueue).where(JobQueue.job_id == jid3)).first()
        assert row.state == JobQueueState.RUNNING, row
        assert row.lease_expires_at > datetime.utcnow(), "interrupted job's lease is in the FUTURE"
    # First the LIVE-SERVER SWEEP path (round-2 gate point-2 anti-double-run): the `db
    # recover-jobs` CLI can run against a running server, so recover(at_boot=False) MUST NOT
    # reclaim this job -- a live worker still holds it (fresh FUTURE lease). Reclaiming it here
    # would let the server's workers re-claim + re-run it (double execution).
    swept = q.recover(at_boot=False)
    assert swept == 0, f"live-server sweep must NOT reclaim a fresh-lease job (double-run): {swept}"
    with Session(engine) as s:
        row = s.exec(select(JobQueue).where(JobQueue.job_id == jid3)).first()
        assert row.state == JobQueueState.RUNNING and row.worker == crash_worker, row
    # BOOT must reclaim the dead predecessor despite its fresh future lease.
    recovered = q.recover(at_boot=True)
    assert recovered >= 1, recovered
    with Session(engine) as s:
        row = s.exec(select(JobQueue).where(JobQueue.job_id == jid3)).first()
        assert row.state == JobQueueState.PENDING and row.worker is None, row
        assert row.lease_expires_at is None, row
        assert s.get(Job, jid3).status == JobStatus.QUEUED, "interrupted Job must re-queue"
        assert s.get(Job, jid3).started_at is None, "started_at must be cleared for re-run"
    # ...and the recovered job is RE-CLAIMABLE and _execute re-runs it to completion.
    assert q.claim("recover-w", timeout=5) == jid3, "recovered job must be re-claimable"
    jobs._execute(jid3)
    q.release(jid3)
    with Session(engine) as s:
        assert s.get(Job, jid3).status == JobStatus.COMPLETED, "re-run job must complete"
        row = s.exec(select(JobQueue).where(JobQueue.job_id == jid3)).first()
        assert row.state == JobQueueState.DONE, row
    print("  flag-on: RESTART RECOVERY -- live-sweep keeps fresh-lease job; BOOT reclaims+re-runs")


def _health_detail():
    from starlette.requests import Request

    from app.web.health import _health_details

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "app.db", "health imported app.db"
        return original_import(name, *args, **kwargs)

    with (
        patch("builtins.__import__", side_effect=guarded_import),
        patch.object(engine, "connect", side_effect=AssertionError("health connected to DB")),
        patch("anyio.to_thread.run_sync", side_effect=AssertionError("health used threadpool")),
    ):
        return json.loads(_health_details(Request({"type": "http"})).body)["detail"]


def _dead_worker():
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    suffix = "abcdef01" if not jobs.instance_id().endswith("abcdef01") else "abcdef02"
    return f"{socket.gethostname()}:{int(child.stdout)}:{suffix}/loc-job-0"


def test_two_process_crash_boot():
    with tempfile.TemporaryDirectory(prefix="loctms_crash_boot_") as data_dir:
        env = dict(os.environ, LOC_TMS_DATA_DIR=data_dir)
        script = os.path.join(_ROOT, "tests", "_jobqueue_crash_boot.py")
        for mode in ("crash", "boot"):
            result = subprocess.run(
                [sys.executable, script, mode],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())
            assert f"{mode.upper()} OK" in result.stdout, result.stdout


def test_instance_fence():
    # Collect every regression in one red run, while still inspecting both persisted rows.
    errors = []

    def check(ok, message):
        if not ok:
            errors.append(message)
            print("RED: " + message)

    assert {"DB_BACKEND_ARMED", "FOREIGN_INSTANCE_ROWS"} <= set(jobqueue.__all__)
    for invalid in (
        None,
        "loc-job-0",
        "broken",
        "host:0:abcdef01/thread",
        "host:" + "9" * 5000 + ":abcdef01/thread",
    ):
        assert jobqueue._boot_worker_rule(invalid) == "legacy-own"
    own = jobs.instance_id()
    assert jobs.instance_id() == own
    assert own.startswith(f"{socket.gethostname()}:{os.getpid()}:")
    assert len(own.rsplit(":", 1)[1]) == 8
    assert all(c in "0123456789abcdef" for c in own.rsplit(":", 1)[1])
    foreign = "other-host:4242:abcdef01/loc-job-0"
    dead = _dead_worker()
    same_pid = f"{socket.gethostname()}:{os.getpid()}:abcdef02/loc-job-0"
    peer = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    live = f"{socket.gethostname()}:{peer.pid}:abcdef03/loc-job-0"
    q = jobqueue.DbJobQueue()
    settings.set_many({"job_queue_backend": "db"})
    jobqueue.configure_at_boot()
    check(_health_detail().get("job_queue") == "db", "armed clean health must say db")
    future = datetime.utcnow() + timedelta(hours=1)
    expired = datetime.utcnow() - timedelta(seconds=10)
    for at_boot in (True, False):
        cases = []
        for worker in (dead, same_pid, live, foreign, "loc-job-0", None):
            for lease in (future, expired, None):
                jid = _mk_job(status=JobStatus.RUNNING)
                reclaim = lease != future or (at_boot and worker not in (live, foreign))
                cases.append((jid, worker, lease, reclaim))
                with Session(engine) as s:
                    job = s.get(Job, jid)
                    job.started_at = datetime.utcnow()
                    s.add(job)
                    s.add(
                        JobQueue(
                            job_id=jid,
                            kind="test_jq_noop",
                            state=JobQueueState.RUNNING,
                            worker=worker,
                            lease_expires_at=lease,
                        )
                    )
                    s.commit()
        before_latch = getattr(jobqueue, "FOREIGN_INSTANCE_ROWS", 0)
        with patch.object(jobs, "ensure_workers"), patch.object(jobqueue.log, "error") as logged:
            count = q.recover(at_boot=at_boot)
        check(count == sum(c[3] for c in cases), f"boot={at_boot}: count excludes protected rows")
        with Session(engine) as s:
            for jid, worker, lease, reclaim in cases:
                row = s.exec(select(JobQueue).where(JobQueue.job_id == jid)).one()
                job = s.get(Job, jid)
                lease_label = "future" if lease == future else "old/null"
                label = f"boot={at_boot}, worker={worker!r}, lease={lease_label}"
                check(
                    row.state == (JobQueueState.PENDING if reclaim else JobQueueState.RUNNING),
                    label + ": queue state",
                )
                check(
                    job.status == (JobStatus.QUEUED if reclaim else JobStatus.RUNNING),
                    label + ": Job status",
                )
                check((job.started_at is None) == reclaim, label + ": started_at")
                check(
                    row.worker == (None if reclaim else worker),
                    label + ": worker preserved/cleared",
                )
                check(
                    row.lease_expires_at == (None if reclaim else lease),
                    label + ": lease preserved/cleared",
                )
                s.delete(row)
                s.delete(job)
            s.commit()
        if at_boot:
            check(
                getattr(jobqueue, "FOREIGN_INSTANCE_ROWS", 0) == 2,
                "foreign boot latch counts only two live peers",
            )
            check(logged.call_count == 1, "foreign rows log once at ERROR")
            if logged.call_count:
                check(foreign in str(logged.call_args), "error names foreign worker")
                check(
                    "foreign-live: other hostname, fresh lease" in str(logged.call_args),
                    "error names foreign-host rule",
                )
                check(
                    "foreign-live: same hostname, live PID, fresh lease" in str(logged.call_args),
                    "error names live-peer rule",
                )
            check(
                _health_detail().get("job_queue") == "db; foreign instance rows seen at boot (2)",
                "foreign health must name boot state and count",
            )
        else:
            check(
                getattr(jobqueue, "FOREIGN_INSTANCE_ROWS", 0) == before_latch,
                "live sweep leaves boot latch unchanged",
            )
            check(logged.call_count == 0, "live sweep does not log boot warning")
    peer.communicate(timeout=30)
    assert not errors, "instance fence failures: " + "; ".join(errors)
    # A clean boot resets the boot observation; a terminal backing Job stays terminal.
    jid = _mk_job(status=JobStatus.COMPLETED)
    with Session(engine) as s:
        s.add(
            JobQueue(
                job_id=jid,
                kind="test_jq_noop",
                state=JobQueueState.RUNNING,
                worker=foreign,
                lease_expires_at=expired,
            )
        )
        s.commit()
    with patch.object(jobs, "ensure_workers"), patch.object(jobqueue.log, "error") as logged:
        assert q.recover(at_boot=True) == 1
        assert logged.call_count == 0
    assert jobqueue.FOREIGN_INSTANCE_ROWS == 0
    assert _health_detail()["job_queue"] == "db"
    with Session(engine) as s:
        assert s.get(Job, jid).status == JobStatus.COMPLETED
        s.delete(s.exec(select(JobQueue).where(JobQueue.job_id == jid)).one())
        s.delete(s.get(Job, jid))
        s.commit()
    print("  instance fence: identity/lease matrix, boot latch, ERROR and DB-free health OK")


def test_fail_loud_boot():
    # An invalid backend value fails the boot loudly (before touching the registry).
    ports.unregister("jobqueue")
    settings.set_many({"job_queue_backend": "bogus"})
    try:
        jobqueue.configure_at_boot()
    except jobqueue.JobQueueError as e:
        assert "memory" in str(e) and "db" in str(e), str(e)
    else:
        raise AssertionError("invalid job_queue_backend must fail the boot")
    # db backend selected but the table missing -> JobQueueError (never a silent degrade).
    settings.set_many({"job_queue_backend": "db"})
    JobQueue.__table__.drop(engine)
    try:
        jobqueue.configure_at_boot()
    except jobqueue.JobQueueError as e:
        assert "jobqueue" in str(e) and "alembic" in str(e), str(e)
    else:
        raise AssertionError("db backend with a missing table must fail the boot")
    finally:
        JobQueue.__table__.create(engine)  # restore for a clean process exit
    print("  fail-loud: invalid value -> JobQueueError; db + missing table -> JobQueueError")


def main() -> None:
    init_db()
    test_flag_off_memory_default()
    test_two_process_crash_boot()
    test_instance_fence()
    test_flag_on_db_backend()
    test_fail_loud_boot()
    # Restore the default backend + adapter (defensive; the process exits next anyway).
    settings.set_many({"job_queue_backend": "memory"})
    ports.unregister("jobqueue")
    print("JOB QUEUE OK")


if __name__ == "__main__":
    main()
