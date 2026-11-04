"""Upgrade from the in-memory job queue to the durable DB queue (S8, release readiness).

Before S2 (ADR 0092) the default job queue lived in memory and never wrote `jobqueue` rows.
After the upgrade the DB queue is the default, and its boot recovery only looks at `jobqueue`
rows, so a Job the old process left QUEUED was never claimed and one it left RUNNING read
running forever (and held the dedupe doors). `jobs.adopt_unqueued_rows` applies the old
restart contract to exactly those rows at boot. This suite fails on the tree before that fix.

  U1  boot recovery enqueues a pre-boot QUEUED job that has no queue row (pending, once)
  U2  boot recovery fails a pre-boot RUNNING ownerless job that has no queue row (role all)
  U3  left alone: rows written after this process started, rows that already have a queue
      row, live streams, background /api calls and owner-stamped rows
  U4  role worker: QUEUED rows are adopted, RUNNING rows are never failed; U4b a worker boot
      does not write the marker, so the first role-all boot still fails the RUNNING rows
  U5  memory backend: nothing is adopted (the in-memory recovery owns the restart), and the
      adoption marker is removed, so the next switch to the DB queue adopts again
  U6  once per switch (gate M1): a later DB-queue boot leaves a RUNNING row with no queue row
      alone (a one-shot CLI `job run` in flight looks exactly like that)
"""

import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jobqueue_upgrade_")

from sqlmodel import Session, select

from app import config, ports
from app.db import engine, init_db
from app.models import Job, JobQueue, JobQueueState, JobStatus
from app.ports import jobqueue
from app.services import jobs, settings

# getattr: the pre-fix tree has no marker and must fail on behaviour (U1), not on a name.
BEFORE = getattr(jobs, "_PROCESS_STARTED", datetime.utcnow()) - timedelta(minutes=5)


def _mk(status, *, created_at=BEFORE, kind="export_project", payload=None):
    with Session(engine) as s:
        job = Job(kind=kind, status=status, payload=payload or {}, created_at=created_at)
        if status == JobStatus.RUNNING:
            job.started_at = created_at
        s.add(job)
        s.commit()
        s.refresh(job)
        return job.id


def _qrows(jid):
    with Session(engine) as s:
        return s.exec(select(JobQueue).where(JobQueue.job_id == jid)).all()


def _status(jid):
    with Session(engine) as s:
        return s.get(Job, jid).status


def _reset():
    with Session(engine) as s:
        for r in s.exec(select(JobQueue)).all():
            s.delete(r)
        for j in s.exec(select(Job)).all():
            s.delete(j)
        s.commit()


def _marker():
    return config.DATA_DIR / jobs._ADOPTED_MARKER if hasattr(jobs, "_ADOPTED_MARKER") else None


def _arm_db():
    settings.set_many({"job_queue_backend": "db"}, actor="test")
    ports.register("jobqueue", jobqueue.DbJobQueue(lease_seconds=60, poll_seconds=0.1))
    jobqueue.DB_BACKEND_ARMED = True


def u1_u2_role_all():
    _reset()
    q = _mk(JobStatus.QUEUED)
    r = _mk(JobStatus.RUNNING)
    jobs.recover_interrupted(at_boot=True)
    rows = _qrows(q)
    assert [x.state for x in rows] == [JobQueueState.PENDING], rows
    assert _status(q) == JobStatus.QUEUED, _status(q)
    print("  U1 a pre-boot QUEUED job with no queue row is enqueued once")
    assert _status(r) == JobStatus.FAILED, _status(r)
    with Session(engine) as s:
        assert s.get(Job, r).error == "Interrupted by a server restart.", s.get(Job, r).error
    assert _qrows(r) == []
    print("  U2 a pre-boot RUNNING ownerless job with no queue row is failed (role all)")
    jobs.recover_interrupted(at_boot=True)
    assert len(_qrows(q)) == 1, _qrows(q)
    print("  U1b a second boot adds no duplicate queue row")


def u3_left_alone():
    _reset()
    fresh_q = _mk(JobStatus.QUEUED, created_at=datetime.utcnow() + timedelta(seconds=5))
    fresh_r = _mk(JobStatus.RUNNING, created_at=datetime.utcnow() + timedelta(seconds=5))
    queued_with_row = _mk(JobStatus.QUEUED)
    jobqueue.get().enqueue(queued_with_row)
    api_task = _mk(JobStatus.RUNNING, kind="api_task")
    owned = _mk(JobStatus.RUNNING, payload={"owner": "otherhost:1:abcd1234"})
    live = _mk(JobStatus.RUNNING, kind="translate_document", payload={"live_stream": True})
    n = jobs.adopt_unqueued_rows()
    assert n == 0, n
    assert _qrows(fresh_q) == [] and _status(fresh_q) == JobStatus.QUEUED
    assert _status(fresh_r) == JobStatus.RUNNING
    assert len(_qrows(queued_with_row)) == 1
    for jid in (api_task, owned, live):
        assert _status(jid) == JobStatus.RUNNING, (jid, _status(jid))
    print("  U3 fresh rows, queued rows, live streams, api calls and owned rows are left alone")


def u4_role_worker():
    _reset()
    q = _mk(JobStatus.QUEUED)
    r = _mk(JobStatus.RUNNING)
    old = config.ROLE
    config.ROLE = "worker"
    try:
        n = jobs.adopt_unqueued_rows()
    finally:
        config.ROLE = old
    assert n == 1, n
    assert len(_qrows(q)) == 1
    assert _status(r) == JobStatus.RUNNING, _status(r)
    print("  U4 role worker: QUEUED adopted, RUNNING never failed")
    # U4b (gate round 2 M1): an upgrade straight to worker processes must not close the switch,
    # or the RUNNING row would never be failed by anything.
    if _marker().exists():
        _marker().unlink()
    config.ROLE = "worker"
    try:
        jobs._adopt_once_after_switch(True)
    finally:
        config.ROLE = old
    assert not _marker().exists(), "a worker boot closed the switch"
    jobs._adopt_once_after_switch(True)  # the first boot in role all
    assert _status(r) == JobStatus.FAILED, _status(r)
    assert _marker().exists()
    print("  U4b a worker boot leaves the switch open; the next role-all boot fails the row")


def u5_memory_backend():
    _reset()
    q = _mk(JobStatus.QUEUED)
    settings.set_many({"job_queue_backend": "memory"}, actor="test")
    ports.register("jobqueue", jobqueue.InMemoryJobQueue())
    jobqueue.DB_BACKEND_ARMED = False
    jobs.recover_interrupted(at_boot=True)
    assert _qrows(q) == [], _qrows(q)
    print("  U5 memory backend: no queue rows written")
    jobs.recover_interrupted(at_boot=True)  # a boot on the memory queue clears the marker
    assert not _marker().exists(), _marker()
    print("  U5b a memory-queue boot removes the adoption marker")


def u6_once_per_switch():
    _reset()
    _arm_db()
    if _marker().exists():
        _marker().unlink()
    jobs.recover_interrupted(at_boot=True)  # the upgrade boot: adopts, leaves the marker
    assert _marker().exists()
    cli_row = _mk(JobStatus.RUNNING)  # e.g. `loc-tms job run ...` in another process
    queued = _mk(JobStatus.QUEUED)
    jobs.recover_interrupted(at_boot=True)  # an ordinary later boot
    assert _status(cli_row) == JobStatus.RUNNING, _status(cli_row)
    assert _qrows(queued) == [], _qrows(queued)
    print("  U6 a later DB-queue boot leaves a live CLI job row alone (once per switch)")


def main() -> None:
    init_db()
    jobs.ensure_workers = lambda: None  # deterministic: nothing claims the adopted rows
    _arm_db()
    u1_u2_role_all()
    u3_left_alone()
    u4_role_worker()
    u5_memory_backend()
    u6_once_per_switch()
    print("JOB QUEUE UPGRADE OK")


if __name__ == "__main__":
    main()
