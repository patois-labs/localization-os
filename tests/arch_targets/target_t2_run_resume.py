"""TARGET T2: an in-flight workflow run survives a server restart and resumes, instead of being failed.

Why (2026-10-22): a queue-based design runs every pipeline step as a queued task; a killed process leaves the
task on the queue and another worker re-runs it. Our workflow runs execute on a daemon thread per run
(orchestrator._run_in_background). A restart kills the thread and recover_interrupted_runs() marks the run FAILED
("Interrupted by a server restart."). With production deploys swapping containers, every deploy fails the runs
that were mid-step, and the user has to notice and re-run them.

Done when every check passes:
  1. After a simulated restart (thread gone, no recent activity), recover_interrupted_runs() does NOT fail a run
     that was RUNNING between steps: it is requeued to resume.
  2. The resumed run continues from the step it was on (completed steps are not re-billed) and reaches the human
     review step.
  3. The resumed run does not duplicate segment writes (the step's output lands once).
"""

import os
import secrets
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env

init_env("loctms_t2_")

from _targets import check, finish, wait_until  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db import engine, init_db  # noqa: E402

init_db()
from app.main import app  # noqa: E402
from app.models import Role, StepRun, WorkflowRun  # noqa: E402
from app.services import apikeys, orchestrator_part2, orchestrator_part6, providers_hub, security  # noqa: E402

RELEASE = threading.Event()
CALLS = {"n": 0}


class SlowMock(providers_hub.MockProvider):
    name = "slowmock"

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        CALLS["n"] += 1
        RELEASE.wait(30)
        return super().complete(prompt, system=system, task=task, payload=payload, images=images, **kw)


providers_hub.register_provider("slowmock", SlowMock())
u = security.create_user("pm@target.test", "Target PM", Role.ADMIN, secrets.token_urlsafe(24))
_row, KEY = apikeys.create_key(u.id, name="t2", scopes="api docs")
c = TestClient(app, headers={"X-Loc-Token": KEY})


def ok(r):
    assert r.status_code < 300, f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}"
    return r.json()


cust = ok(c.post("/api/customers", json={"name": "T2 Co"}))["id"]
pid = ok(c.post("/api/projects", json={"name": "t2", "customer_id": cust, "source_locale": "en-US",
                                        "targets": "es-ES", "provider": "mock"}))["id"]
html = b"<html><body><p>Shorter days, emptier trails.</p><p>Pack the stove fuel in the lid.</p></body></html>"
doc = ok(c.post("/api/documents", data={"project_id": str(pid)}, files={"file": ("t2.html", html)}))["document_id"]
wid = ok(c.post("/api/workflows", json={"name": "T2", "customer_id": cust, "scope": "customer"}))["id"]
ok(c.post(f"/api/workflows/{wid}/steps", json={"name": "Fast", "step_type": "llm_agent", "provider": "mock",
                                                "prompt_text": "Translate to {target_locale}:\n{text}"}))
ok(c.post(f"/api/workflows/{wid}/steps", json={"name": "Slow", "step_type": "llm_agent", "provider": "slowmock",
                                                "prompt_text": "Polish:\n{text}"}))
ok(c.post(f"/api/workflows/{wid}/steps", json={"name": "Review", "step_type": "human_review",
                                                "assignee_email": "pm@target.test"}))
b = ok(c.post(f"/api/workflows/{wid}/run-batch", json={"doc_id": doc, "locales": ["es-ES"], "max_parallel": 1}))
RUN = b["members"][0]["run_id"]


def run_row():
    with Session(engine) as s:
        return s.get(WorkflowRun, RUN)


def stepruns():
    """[(step order index, status)] for every StepRun of the run."""
    from app.models import WorkflowStep

    with Session(engine) as s:
        order = {st.id: st.order_index for st in s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid))}
        return [(order.get(sr.step_id), str(sr.status).lower())
                for sr in s.exec(select(StepRun).where(StepRun.workflow_run_id == RUN))]


# The run is now inside step 1 (the slow call). Simulate the restart: the run's thread "is gone" and its last
# activity is old enough, which is what a booting server sees.
wait_until(lambda: CALLS["n"] >= 1, 20)
STATE = {}


def t_not_failed():
    """Run the boot recovery exactly as app/main.py does, with the run's thread treated as gone."""
    from datetime import timedelta

    from app.services import batch_runs, batch_runs_hub, orchestrator, orchestrator_part4

    mods = [orchestrator, orchestrator_part4, orchestrator_part6, batch_runs_hub]
    saved = [(m, m.__dict__.get("_run_thread_alive")) for m in mods if "_run_thread_alive" in m.__dict__]
    saved_grace = orchestrator_part6.RECOVERY_ACTIVITY_GRACE
    for m, _ in saved:
        m._run_thread_alive = lambda run_id: False
    orchestrator_part6.RECOVERY_ACTIVITY_GRACE = timedelta(seconds=0)
    try:
        batch_runs.recover_interrupted_batches(at_boot=True)
        orchestrator.recover_interrupted_runs()
        orchestrator.adopt_queued_runs()
    finally:
        for m, fn in saved:
            m._run_thread_alive = fn
        orchestrator_part6.RECOVERY_ACTIVITY_GRACE = saved_grace
    r = run_row()
    STATE["after_recover"] = (r.status, r.current_step_index, r.reason if hasattr(r, "reason") else None)
    return str(r.status).lower() not in ("failed", "runstatus.failed"), f"after recovery: status={r.status} " \
        f"step={r.current_step_index} note={getattr(r, 'notes', None) or orchestrator_part2.RESTART_NOTE}"


def t_resumes_to_review():
    RELEASE.set()
    r = wait_until(lambda: run_row() if str(run_row().status).lower().endswith("paused") else None, 60)
    if r is None:
        rr = run_row()
        return False, f"run ended {rr.status} at step {rr.current_step_index}, not paused at review"
    fast = [s for s in stepruns() if s[0] == 0]
    return (r.current_step_index == 2 and len(fast) == 1), \
        f"paused at step {r.current_step_index}; step 0 ran {len(fast)} time(s) (want 1: no re-billing)"


def t_single_write():
    slow = [s for s in stepruns() if s[0] == 1 and s[1].endswith("completed")]
    return len(slow) == 1, f"step 1 completed {len(slow)} time(s) (want exactly 1); stepruns={stepruns()}"


check("1 restart does not fail a mid-run workflow", t_not_failed)
check("2 the run resumes where it was and reaches review", t_resumes_to_review)
check("3 the resumed step lands once", t_single_write)
finish("T2", "workflow runs survive restart")
