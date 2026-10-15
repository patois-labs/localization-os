"""Issue #438: retention.purge deletes finished (done/failed) jobqueue rows past the cutoff
(job_queue_backend=db), never pending/running ones or young ones.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR; retention is enabled only inside it).
ASCII-only prints. Final line: DATA LIFECYCLE 438 OK.
"""

import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_data_lifecycle_438_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Job, JobQueue, JobQueueState, JobStatus
from app.services import retention, settings

init_db()
settings.set_many({"retention_enabled": "0", "retention_days": "30"})


def test_retention_reaps_finished_jobqueue_rows():
    old = datetime.utcnow() - timedelta(days=400)
    with Session(engine) as s:
        ids = {}
        for state in (
            JobQueueState.DONE,
            JobQueueState.FAILED,
            JobQueueState.PENDING,
            JobQueueState.RUNNING,
        ):
            j = Job(kind="x", status=JobStatus.COMPLETED, created_at=old)
            s.add(j)
            s.commit()
            s.refresh(j)
            q = JobQueue(job_id=j.id, kind="x", state=state, enqueued_at=old, updated_at=old)
            s.add(q)
            s.commit()
            s.refresh(q)
            ids[state] = q.id
        young = JobQueue(job_id=0, kind="x", state=JobQueueState.DONE)
        s.add(young)
        s.commit()
        s.refresh(young)
        young_id = young.id
    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    try:
        counts = retention.purge()
    finally:
        settings.set_many({"retention_enabled": "0"})
    assert counts["jobqueue_rows"] == 2, counts
    with Session(engine) as s:
        assert s.get(JobQueue, ids[JobQueueState.DONE]) is None
        assert s.get(JobQueue, ids[JobQueueState.FAILED]) is None
        assert s.get(JobQueue, ids[JobQueueState.PENDING]) is not None
        assert s.get(JobQueue, ids[JobQueueState.RUNNING]) is not None
        assert s.get(JobQueue, young_id) is not None, "a young finished row is kept"
    print("  [4] retention reaps finished jobqueue rows, keeps live and young ones OK")


if __name__ == "__main__":
    test_retention_reaps_finished_jobqueue_rows()
    print("DATA LIFECYCLE 438 OK")
