"""Two real process incarnations; invoked by runner-registered verify_job_queue."""

import asyncio
import json
import os
import sys
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select
from starlette.requests import Request

from app.db import engine, init_db
from app.models import Job, JobQueue, JobStatus
from app.ports import jobqueue
from app.services import jobs, settings

init_db()
settings.set_many({"job_queue_backend": "db"})
jobqueue.configure_at_boot()
q = jobqueue.get()
if sys.argv[1] == "crash":
    with Session(engine) as s:
        job = Job(kind="test_jq_noop", status=JobStatus.QUEUED, payload={})
        s.add(job)
        s.commit()
        s.refresh(job)
        jid = job.id
    q.enqueue(jid)
    assert q.claim(f"{jobs.instance_id()}/loc-job-0", timeout=5) == jid
    with Session(engine) as s:
        job = s.get(Job, jid)
        job.status = JobStatus.RUNNING
        job.started_at = datetime.utcnow()
        s.add(job)
        s.commit()
    print("CRASH OK: fresh lease, real claim, os._exit", flush=True)
    os._exit(0)

with patch.object(jobs, "ensure_workers"), patch.object(jobqueue.log, "error") as logged:
    recovered = q.recover(at_boot=True)
    assert recovered == 1, f"RED: dead predecessor stranded; recovered={recovered}"
    assert logged.call_count == 0, "RED: crash restart falsely logs a live peer"
assert jobqueue.FOREIGN_INSTANCE_ROWS == 0
with Session(engine) as s:
    row = s.exec(select(JobQueue)).one()
    job = s.get(Job, row.job_id)
    assert row.state == "pending" and row.worker is None and row.lease_expires_at is None
    assert job.status == JobStatus.QUEUED and job.started_at is None
from app.web.health import _health_details, health

assert asyncio.iscoroutinefunction(health)
with (
    patch.object(engine, "connect", side_effect=AssertionError("health connected")),
    patch("anyio.to_thread.run_sync", side_effect=AssertionError("health used threadpool")),
):
    body = json.loads(asyncio.run(health(Request({"type": "http"}))).body)
assert "detail" not in body, body
assert json.loads(_health_details(Request({"type": "http"})).body)["detail"]["job_queue"] == "db"
print("BOOT OK: recovered=1; errors=0; foreign_latch=0; health job_queue=db")
engine.dispose()
