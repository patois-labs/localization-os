"""S1 engine reliability (ADR 0090): an interrupted workflow run RESUMES after a restart.

Promotes target T2 (tests/arch_targets/target_t2_run_resume.py) into the release gate and adds
the cases around it. Before S1, boot recovery failed every run that was mid-step ("Interrupted
by a server restart."), so every production deploy failed the runs in flight.

GROUPS
  [A] BATCH MEMBER RESUME (the T2 scenario). A member is inside step 1 (a blocking engine)
      when the restart is simulated (its thread treated as gone). Recovery re-queues it
      (QUEUED, resume_count 1, the resume note), admission resumes it from step 1, it
      reaches the human review step, step 0 ran exactly once, step 1 landed exactly once
      although the superseded thread also finished its call, and every completed visit has
      a distinct idempotency key. A second recovery pass leaves the PAUSED run alone.
  [B] FULL_TEXT PLAIN RUN RESUME. The same on a FULL_TEXT document launched as a plain run
      (orchestrator_part6.recover_interrupted_runs): it resumes and COMPLETES, each step
      landed once, the target body is written, and the run's checkpoints are cleared.
  [C] KEYS AND CHECKPOINTS ON A NORMAL RUN. A segmented run that completes keys every
      visit and keeps no checkpoint; a duplicate key is refused by the UNIQUE index.
  [D] WHAT RECOVERY LEAVES ALONE OR FAILS (planted rows, no thread):
      a run with no owner record (pre-S1 code) is failed as before; a run owned by another
      LIVE process on this host is left running; one owned by another host with a fresh
      heartbeat is left running, and once that heartbeat is stale the live sweep re-queues
      it and it completes; a dead owner whose step has no checkpoint is failed; a run that
      already used its restart resumes is failed; a PAUSED run is never touched.
  [E] THE SUPERSEDED WRITER. VisitWriter refuses to commit once another token owns the run.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider plus a gated mock). ASCII
output. Final line: "RUN RESTART RESUME OK".
"""

import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_runresume_")

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine, init_db

init_db()
from app.main import app
from app.models import Document, Role, RunStatus, StepRun, WorkflowRun, WorkflowStep
from app.services import (
    apikeys,
    batch_runs,
    orchestrator,
    orchestrator_part4,
    orchestrator_part6,
    providers_hub,
    run_resume,
    security,
)
from app.services.orchestrator_part2 import RESTART_NOTE, RESTART_RESUME_NOTE

GATES = {"a": threading.Event(), "b": threading.Event()}
CALLS = {"a": 0, "b": 0}


class GatedMock(providers_hub.MockProvider):
    def __init__(self, key):
        super().__init__()
        self.key = key
        self.name = f"gated{key}"

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        CALLS[self.key] += 1
        GATES[self.key].wait(60)
        return super().complete(
            prompt, system=system, task=task, payload=payload, images=images, **kw
        )


providers_hub.register_provider("gateda", GatedMock("a"))
providers_hub.register_provider("gatedb", GatedMock("b"))

user = security.create_user("pm@resume.test", "Resume PM", Role.ADMIN, secrets.token_urlsafe(24))
_row, KEY = apikeys.create_key(user.id, name="resume", scopes="api docs")
client = TestClient(app, headers={"X-Loc-Token": KEY})


def ok(r):
    assert r.status_code < 300, (
        f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}"
    )
    return r.json()


def wait_until(pred, timeout, every=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def run_row(rid):
    with Session(engine) as s:
        return s.get(WorkflowRun, rid)


def stepruns(rid, wid):
    with Session(engine) as s:
        order = {
            st.id: st.order_index
            for st in s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid))
        }
        return [
            (order.get(sr.step_id), str(sr.status), sr.idempotency_key, sr.checkpoint)
            for sr in s.exec(
                select(StepRun).where(StepRun.workflow_run_id == rid).order_by(StepRun.id)
            )
        ]


def simulate_restart():
    """Boot recovery exactly as app/main.py runs it, with every run thread treated as gone."""
    mods = [orchestrator, orchestrator_part4, orchestrator_part6]
    saved = [
        (m, m.__dict__["_run_thread_alive"]) for m in mods if "_run_thread_alive" in m.__dict__
    ]
    for m, _ in saved:
        m._run_thread_alive = lambda run_id: False
    try:
        batch_runs.recover_interrupted_batches(at_boot=True)
        orchestrator.recover_interrupted_runs()
    finally:
        for m, fn in saved:
            m._run_thread_alive = fn
    orchestrator.adopt_queued_runs()


cust = ok(client.post("/api/customers", json={"name": "Resume Co"}))["id"]
pid = ok(
    client.post(
        "/api/projects",
        json={
            "name": "resume",
            "customer_id": cust,
            "source_locale": "en-US",
            "targets": "es-ES,fr-FR,de-DE",
            "provider": "mock",
        },
    )
)["id"]
HTML = (
    b"<html><body><p>Shorter days, emptier trails.</p>"
    b"<p>Pack the stove fuel in the lid.</p></body></html>"
)


def upload(name, mode="segmented"):
    return ok(
        client.post(
            "/api/documents",
            data={"project_id": str(pid), "mode": mode},
            files={"file": (name, HTML)},
        )
    )["document_id"]


def workflow(name, steps):
    wid = ok(
        client.post("/api/workflows", json={"name": name, "customer_id": cust, "scope": "customer"})
    )["id"]
    for st in steps:
        ok(client.post(f"/api/workflows/{wid}/steps", json=st))
    return wid


FAST = {
    "name": "Fast",
    "step_type": "llm_agent",
    "provider": "mock",
    "prompt_text": "Translate to {target_locale}:\n{text}",
}


def gated(key):
    return {
        "name": "Slow",
        "step_type": "llm_agent",
        "provider": f"gated{key}",
        "prompt_text": "Polish:\n{text}",
    }


REVIEW = {"name": "Review", "step_type": "human_review", "assignee_email": "pm@resume.test"}


# --- [A] batch member resume ------------------------------------------------------------
print("[A] batch member resume")
wid_a = workflow("A", [FAST, gated("a"), REVIEW])
doc_a = upload("a.html")
b = ok(
    client.post(
        f"/api/workflows/{wid_a}/run-batch",
        json={"doc_id": doc_a, "locales": ["es-ES"], "max_parallel": 1},
    )
)
RUN_A = b["members"][0]["run_id"]
assert wait_until(lambda: CALLS["a"] >= 1, 30), "the run never reached step 1"
owner_before = run_row(RUN_A).owner
assert owner_before and run_resume._TOKEN_RE.fullmatch(owner_before), owner_before
assert run_row(RUN_A).heartbeat_at is not None
simulate_restart()
r = run_row(RUN_A)
assert r.status in (RunStatus.QUEUED, RunStatus.RUNNING), r.status
assert r.resume_count == 1, r.resume_count
assert RESTART_RESUME_NOTE in (r.notes or ""), r.notes
assert RESTART_NOTE not in (r.notes or ""), r.notes
print(f"  after recovery: {r.status}, resume_count={r.resume_count}")
assert wait_until(lambda: CALLS["a"] >= 2, 30), "the resumed execution never re-entered step 1"
GATES["a"].set()
r = wait_until(lambda: run_row(RUN_A) if run_row(RUN_A).status == RunStatus.PAUSED else None, 60)
assert r is not None, f"run ended {run_row(RUN_A).status}, not paused at review"
assert r.current_step_index == 2, r.current_step_index
assert r.owner and r.owner != owner_before, "the resumed execution must own the run"
rows = stepruns(RUN_A, wid_a)
assert [x[0] for x in rows if x[0] == 0] == [0], f"step 0 ran more than once: {rows}"
done1 = [x for x in rows if x[0] == 1 and x[1] == RunStatus.COMPLETED]
assert len(done1) == 1, f"step 1 landed {len(done1)} times: {rows}"
keys = [x[2] for x in rows if x[1] == RunStatus.COMPLETED]
assert all(keys) and len(set(keys)) == len(keys), keys
assert keys == [f"{RUN_A}:0:0", f"{RUN_A}:1:1"], keys
assert all(x[3] and x[3]["cursor"] == x[0] + 1 for x in rows if x[1] == RunStatus.COMPLETED)
print(f"  paused at review; step 0 x1, step 1 x1, keys={keys}")
time.sleep(0.5)  # the superseded thread has finished its call by now
assert len([x for x in stepruns(RUN_A, wid_a) if x[0] == 1]) == 1
simulate_restart()
assert run_row(RUN_A).status == RunStatus.PAUSED, "recovery must never touch a PAUSED run"
print("  a second recovery pass leaves the paused run alone")

# --- [B] FULL_TEXT plain run resume ------------------------------------------------------
print("[B] FULL_TEXT plain run resume")
wid_b = workflow("B", [FAST, gated("b")])
doc_b = upload("b.html", mode="full_text")
with Session(engine) as s:
    _mode = str(s.get(Document, doc_b).mode)
    assert _mode.lower().endswith("full_text"), _mode
RUN_B = ok(client.post(f"/api/workflows/{wid_b}/run", json={"doc_id": doc_b, "locale": "fr-FR"}))[
    "run_id"
]
assert wait_until(lambda: CALLS["b"] >= 1, 30), "the run never reached step 1"
simulate_restart()
r = run_row(RUN_B)
assert r.status in (RunStatus.QUEUED, RunStatus.RUNNING), (r.status, r.notes)
assert r.resume_count == 1
assert wait_until(lambda: CALLS["b"] >= 2, 30), "the resumed execution never re-entered step 1"
GATES["b"].set()
r = wait_until(lambda: run_row(RUN_B) if run_row(RUN_B).status == RunStatus.COMPLETED else None, 60)
assert r is not None, f"run ended {run_row(RUN_B).status}: {run_row(RUN_B).notes}"
rows = stepruns(RUN_B, wid_b)
assert len([x for x in rows if x[0] == 0]) == 1, rows
assert len([x for x in rows if x[0] == 1 and x[1] == RunStatus.COMPLETED]) == 1, rows
assert all(x[3] is None for x in rows), "a completed run keeps no checkpoint"
assert all(x[2] for x in rows), rows
print(f"  completed after resume; steps {[(x[0], x[1]) for x in rows]}")

# --- [C] keys and checkpoints on a normal run --------------------------------------------
print("[C] keys on a normal run")
wid_c = workflow("C", [FAST, dict(FAST, name="Fast 2")])
doc_c = upload("c.html")
RUN_C = ok(client.post(f"/api/workflows/{wid_c}/run", json={"doc_id": doc_c, "locale": "de-DE"}))[
    "run_id"
]
r = wait_until(lambda: run_row(RUN_C) if run_row(RUN_C).status == RunStatus.COMPLETED else None, 60)
assert r is not None, run_row(RUN_C).status
rows = stepruns(RUN_C, wid_c)
assert [x[2] for x in rows] == [f"{RUN_C}:0:0", f"{RUN_C}:1:1"], rows
assert all(x[3] is None for x in rows), rows
assert not r.resume_count, r.resume_count
with Session(engine) as s:
    s.add(
        StepRun(
            workflow_run_id=RUN_C,
            step_id=s.exec(
                select(WorkflowStep.id).where(WorkflowStep.workflow_id == wid_c)
            ).first(),
            status=RunStatus.COMPLETED,
            idempotency_key=f"{RUN_C}:0:0",
        )
    )
    try:
        s.commit()
        raise AssertionError("a duplicate idempotency key must be refused")
    except IntegrityError:
        s.rollback()
print("  keys <run>:<cursor>:<seq>, no checkpoint kept, duplicate key refused")

# --- [D] what recovery leaves alone or fails ---------------------------------------------
print("[D] planted rows")
host = socket.gethostname()
live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
doc_d = upload("d.html")
old = datetime.utcnow() - timedelta(days=1)


def plant(
    status=RunStatus.RUNNING,
    owner=None,
    heartbeat=None,
    cursor=0,
    resumes=None,
    step_row=False,
    wid=wid_c,
    locale="es-ES",
):
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=wid,
            document_id=doc_d,
            target_locale=locale,
            status=status,
            current_step_index=cursor,
            started_at=old,
            owner=owner,
            heartbeat_at=heartbeat,
            resume_count=resumes,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        if step_row:
            sid = s.exec(select(WorkflowStep.id).where(WorkflowStep.workflow_id == wid)).first()
            s.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=sid,
                    status=RunStatus.COMPLETED,
                    started_at=old,
                    finished_at=old,
                )
            )
            s.commit()
        return run.id


legacy = plant(cursor=1, step_row=True)
live_owner = plant(owner=f"{host}:{live.pid}:abcdef12/12345678", heartbeat=datetime.utcnow())
# A live PID whose heartbeat is long gone is a REUSED pid, not the owner: the run is dead.
reused_pid = plant(
    owner=f"{host}:{live.pid}:abcdef12/22345678", heartbeat=old, cursor=1, step_row=True
)
fresh_remote = plant(
    owner="otherhost-s1:4242:abcdef12/12345678", heartbeat=datetime.utcnow(), locale="fr-FR"
)
no_ckpt = plant(
    owner=f"{host}:{dead.pid}:abcdef12/12345678", heartbeat=old, cursor=1, step_row=True
)
spent = plant(
    owner=f"{host}:{dead.pid}:abcdef12/12345678",
    heartbeat=old,
    resumes=run_resume.MAX_RESTART_RESUMES,
)
paused = plant(status=RunStatus.PAUSED, owner=f"{host}:{dead.pid}:abcdef12/12345678", heartbeat=old)
orchestrator.recover_interrupted_runs()
try:
    planted = {
        "legacy": legacy,
        "live_owner": live_owner,
        "fresh_remote": fresh_remote,
        "no_ckpt": no_ckpt,
        "spent": spent,
        "reused_pid": reused_pid,
        "paused": paused,
    }
    got = {k: run_row(v) for k, v in planted.items()}
    assert got["legacy"].status == RunStatus.FAILED and RESTART_NOTE in got["legacy"].notes
    assert got["live_owner"].status == RunStatus.RUNNING, got["live_owner"].status
    assert got["fresh_remote"].status == RunStatus.RUNNING, got["fresh_remote"].status
    assert got["no_ckpt"].status == RunStatus.FAILED and RESTART_NOTE in got["no_ckpt"].notes
    assert got["spent"].status == RunStatus.FAILED, got["spent"].status
    assert got["reused_pid"].status == RunStatus.FAILED, got["reused_pid"].status
    assert got["paused"].status == RunStatus.PAUSED, got["paused"].status
    print(
        "  legacy failed, live owner kept, fresh remote kept, no checkpoint failed, "
        "spent failed, reused pid failed, paused untouched"
    )
    # The remote owner's heartbeat goes stale: the live sweep re-queues it to resume.
    with Session(engine) as s:
        row = s.get(WorkflowRun, fresh_remote)
        row.heartbeat_at = datetime.utcnow() - run_resume.HEARTBEAT_STALE - timedelta(seconds=5)
        s.add(row)
        s.commit()
    # A remote owner that dies with an unresumable step: the live sweep fails it.
    stale_hb = datetime.utcnow() - run_resume.HEARTBEAT_STALE - timedelta(seconds=5)
    remote_dead = plant(
        owner="otherhost-s1:4243:abcdef12/32345678",
        heartbeat=stale_hb,
        cursor=1,
        step_row=True,
        locale="de-DE",
    )
    assert run_resume.sweep_stale_runs() == 1
    assert run_row(remote_dead).status == RunStatus.FAILED, run_row(remote_dead).status
    assert RESTART_NOTE in (run_row(remote_dead).notes or "")
    assert run_row(fresh_remote).resume_count == 1
    r = wait_until(
        lambda: (
            run_row(fresh_remote) if run_row(fresh_remote).status == RunStatus.COMPLETED else None
        ),
        60,
    )
    assert r is not None, run_row(fresh_remote).status
    assert run_resume.sweep_stale_runs() == 0
    print("  stale remote heartbeat: swept, resumed, completed; an unresumable one failed")
finally:
    live.kill()

# --- [E] the superseded writer ------------------------------------------------------------
print("[E] superseded writer")
with Session(engine) as s:
    run = s.get(WorkflowRun, RUN_C)
    run.owner = "someone-else"
    s.add(run)
    s.commit()
    writer = run_resume.VisitWriter(run, "this-execution", 0, lambda: {})
    calls = []
    assert writer.commit(s, lambda: calls.append(1)) is False and not calls
    assert run_resume.VisitWriter(run, "someone-else", 0, lambda: {}).commit(
        s, lambda: calls.append(1)
    ) is True and calls == [1]
print("  a writer that lost ownership commits nothing")

# Two executions of one run in one process (the T2 shape): each thread keeps its own token,
# and the heartbeat quiets (never forgets) the one that was taken over.
tokens = {}


def _hold(name, tok):
    run_resume.register(RUN_C, tok)
    tokens[name] = tok
    time.sleep(0.5)
    tokens[name + "_seen"] = run_resume.held_by_this_thread(RUN_C)
    tokens[name + "_over"] = run_resume.taken_over(RUN_C)


with Session(engine) as s:
    run = s.get(WorkflowRun, RUN_C)
    run.owner = "tok-new"
    run.status = RunStatus.RUNNING
    s.add(run)
    s.commit()
threads = [
    threading.Thread(target=_hold, args=("old", "tok-old")),
    threading.Thread(target=_hold, args=("new", "tok-new")),
]
for t in threads:
    t.start()
time.sleep(0.2)
run_resume._beat_once()
for t in threads:
    t.join()
assert tokens["old_seen"] == "tok-old" and tokens["new_seen"] == "tok-new", tokens
assert tokens["old_over"] is True and tokens["new_over"] is False, tokens
with Session(engine) as s:
    run = s.get(WorkflowRun, RUN_C)
    run.status = RunStatus.COMPLETED
    s.add(run)
    s.commit()
print("  each execution keeps its own token; the taken-over one reads as taken over")

# The resume cap counts resumes IN A ROW: a checkpoint written under the current resume count
# means progress, so a fourth deploy does not fail a run that keeps moving.
with Session(engine) as s:
    sr = s.exec(
        select(StepRun).where(StepRun.workflow_run_id == RUN_C).order_by(StepRun.id.desc())
    ).first()
    run = s.get(WorkflowRun, RUN_C)
    run.current_step_index = 1
    run.resume_count = run_resume.MAX_RESTART_RESUMES + 2
    sr.checkpoint = {"cursor": 1, "seq": 1, "working": [], "resume_count": run.resume_count}
    s.add(sr)
    s.add(run)
    s.commit()
    assert run_resume.resumable(s, run)[0] == "resume"
    sr.checkpoint = {"cursor": 1, "seq": 1, "working": [], "resume_count": 0}
    s.add(sr)
    s.commit()
    assert run_resume.resumable(s, run)[0] == "fail"
print("  the resume cap counts resumes without progress, not lifetime resumes")

print("RUN RESTART RESUME OK")
