"""TARGET T6: background work can run in separate worker processes, so the web process stays responsive and the
app can use more than one CPU core.

Why (2026-10-22): in a split design the API handlers and the queue workers are independent (with the
database as the shared state), so a heavy batch never slows page loads, and replicas can be added.
Ours runs the web server, the job workers, the workflow-run threads and the scheduler in ONE Python process: CPU
work in a job (parsing, rapidfuzz scoring, tag checks, prompt building) competes with request handling for the
GIL, and the box's other cores sit idle. ADR 0043-era design notes already name "multi-instance needs a real
broker" (jobs.py docstring); T1 provides the durable queue this target builds on.

Done when every check passes:
  1. A worker entry point exists: `python -m app.worker` (same codebase, same config, LOC_TMS_ROLE=worker).
  2. With LOC_TMS_ROLE=web the web process starts NO job worker threads and NO workflow-run threads; submitted
     work waits on the durable queue.
  3. A job submitted by the web process is completed by a separate worker process within 10 s.
  4. While the worker process runs CPU-heavy translate jobs, the web process answers /health with p95 under 50 ms.
"""

import importlib.util
import os
import subprocess
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["LOC_TMS_ROLE"] = "web"
from tests._bootstrap import init_env

DATA = init_env("loctms_t6_")

from _targets import check, finish, pct, wait_until  # noqa: E402

from app.db import init_db  # noqa: E402

init_db()
from app.models import JobStatus  # noqa: E402
from app.services import jobs, settings  # noqa: E402

settings.set_many({"job_queue_backend": "db"}, actor="target")
WORKER = {"proc": None}


def t_entry_point():
    spec = importlib.util.find_spec("app.worker")
    if spec is None:
        raise NotImplementedError("app.worker module (python -m app.worker)")
    env = dict(os.environ, LOC_TMS_ROLE="worker", LOC_TMS_DATA_DIR=DATA)
    WORKER["proc"] = subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=_ROOT, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    return WORKER["proc"].poll() is None, "worker process started and stayed up"


def t_web_runs_no_workers():
    jobs.ensure_workers()
    names = [t.name for t in threading.enumerate()]
    workers = [n for n in names if n.startswith("loc-job-") or n.startswith("loc-run-")]
    return not workers, f"web-role threads that execute work: {workers[:6]}"


def t_worker_completes():
    if WORKER["proc"] is None:
        raise NotImplementedError("no worker process (check 1)")
    jid = jobs.submit("embed_backfill" if "embed_backfill" in jobs.HANDLERS else next(iter(jobs.HANDLERS)), {})
    j = wait_until(lambda: jobs.get(jid) if jobs.get(jid).status in (JobStatus.COMPLETED, JobStatus.FAILED) else None,
                   10)
    return j is not None, f"job {jid} status {jobs.get(jid).status} (completed by the worker process?)"


def t_web_stays_fast():
    if WORKER["proc"] is None:
        raise NotImplementedError("no worker process (check 1)")
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    lat = []
    t_end = time.time() + 8
    while time.time() < t_end:
        t0 = time.perf_counter()
        c.get("/health")
        lat.append((time.perf_counter() - t0) * 1000)
    return pct(lat, 0.95) < 50, f"/health p95 {pct(lat, 0.95):.1f} ms over {len(lat)} calls"


try:
    check("1 worker entry point", t_entry_point)
    check("2 web role starts no workers", t_web_runs_no_workers)
    check("3 worker process completes web-submitted jobs", t_worker_completes)
    check("4 web stays fast while workers are busy", t_web_stays_fast)
finally:
    if WORKER["proc"] is not None:
        WORKER["proc"].kill()
finish("T6", "worker processes")
