"""Process roles: a web process and worker processes sharing one database (S3b, ADR 0095; offline).

Promotes tests/arch_targets/target_t6_worker_split.py and pins the rest of the S3b contract. This
process runs as LOC_TMS_ROLE=web (it serves requests, queues work as durable rows, executes none of it);
the work is done by real `python -m app.worker` child processes on the same LOC_TMS_DATA_DIR (SQLite,
the mock provider only: no paid call). Checks, in order (each prints one OK line):

  1. T6 promoted: the worker entry point starts and stays up; the web process starts no loc-job- /
     loc-run- thread after jobs.ensure_workers() and ADMISSION.ensure_started(); a translate_document job
     submitted from the web process COMPLETES in the worker; /health p95 stays under 50 ms in the web
     process while the worker translates several documents.
  2. A web-role launch is a QUEUED row (detail["queued"] True), a second identical launch re-attaches to
     the same run id, and the worker executes it to PAUSED with both AI StepRuns completed.
  3. A web-role resume (the web UI route) leaves the run QUEUED with the edited text in
     run.context["_queued_resume"] and the cursor advanced by one; a worker completes it, the seed key is
     gone, and the final output equals what the same flow produces with LOC_TMS_ROLE=all in a second,
     fresh data dir (a child process of this file in WR_CHILD mode).
  4. Two workers: 6 queued runs all COMPLETE, every StepRun idempotency_key is unique, every (run, step)
     visit appears once, and both worker pids owned at least one run.
  5. Cross-process run events: a run executing in a worker publishes on events.run_channel(run_id) and the
     web process receives it; an observe_run stream opened in the web process with the fallback wait
     raised to 10 s still reports the end of the run within 3 s of its commit (an event woke it).
  6. Role guard: LOC_TMS_ROLE=web refuses the memory job queue (JobQueueError at configure_at_boot).
  7. Graceful stop: SIGTERM hands a running job back to QUEUED (POSIX only); on Windows the check is
     skipped with a SKIP line, but a worker must still exit within 15 s of proc.terminate().

NEW BEHAVIOUR vs the pre-S3b tree (every check fails there): app.worker, LOC_TMS_ROLE and the web-role
queue-instead-of-execute launch/resume, the wake and run-event channels and the memory-queue refusal do
not exist, so the first worker spawn, config.ROLE, _queued_resume and JobQueueError all fail or are
missing. Check 4 (no double execution across processes) is S3b's multi-process claim.

Final line: WORKER ROLES OK. ASCII-only.
"""

# ruff: noqa: E501 -- long assertion messages carry expected-vs-seen detail
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
_CHILD = os.environ.get("WR_CHILD") == "all"
os.environ["LOC_TMS_ROLE"] = "all" if _CHILD else "web"
from tests._bootstrap import init_env

DATA = init_env("loctms_workerroles_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
    JobStatus,
    Project,
    RunStatus,
    Segment,
    StepRun,
    StepType,
    TargetLocale,
    TransMode,
    WorkflowRun,
    WorkflowStep,
)
from app.services import jobs, orchestrator, settings
from app.services import workflows as W
from app.services.orchestrator_terminology_backen import SEG_JOINER

init_db()
settings.set_many(
    {
        "security_auth_required": "0",
        "multi_org_enabled": "0",
        "project_scoping": "0",
        "api_rate_limit_per_minute": "0",
        "api_read_rate_limit_per_minute": "0",
        "job_queue_backend": "db",
    },
    actor="verify_worker_roles",
)
settings.invalidate_cache()

WORKERS = []
EDIT_PARTS = ["Edited one, café 42.", "Edited two.", "Edited three <b>x</b>."]
EDIT = SEG_JOINER.join(EDIT_PARTS)
LOCALE = "fr-FR"
_N = [0]
PY = sys.executable


def _uniq(p):
    _N[0] += 1
    return f"{p}-{_N[0]}"


def _fail(msg):
    raise AssertionError(msg)


def _wait(pred, timeout, what, step=0.1):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = pred()
        if last:
            return last
        time.sleep(step)
    _fail(f"expected {what} within {timeout}s; saw nothing (last value {last!r})")


# ---------------------------------------------------------------------------- fixtures
def mk_doc(n_seg, name=None):
    with Session(engine) as s:
        p = Project(name=name or _uniq("WRProj"), source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOCALE))
        doc = Document(
            project_id=p.id,
            filename="doc.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": f"Sentence number {i}."} for i in range(n_seg)]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i in range(n_seg):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=LOCALE,
                    order_index=i,
                    source_text=f"Sentence number {i}.",
                    target_text="",
                )
            )
        s.commit()
        return doc.id


def mk_wf(review):
    with Session(engine) as s:
        w = W.create_workflow(s, _uniq("WRWf"))
        for nm in ("Translate", "Polish"):
            W.add_step(
                s,
                w.id,
                name=nm,
                provider="mock",
                model="m1",
                prompt_text="Translate {text}",
                chunk_policy="none",
            )
        if review:
            W.add_step(s, w.id, name="Human", step_type=StepType.HUMAN_REVIEW)
        return w.id


def run_row(rid):
    with Session(engine) as s:
        r = s.get(WorkflowRun, rid)
        if r is not None:
            s.expunge(r)
        return r


def wait_status(rid, want, timeout=60):
    def pred():
        r = run_row(rid)
        return r if r is not None and r.status in want else None

    try:
        return _wait(pred, timeout, f"run {rid} to reach {want}", step=0.2)
    except AssertionError as e:
        r = run_row(rid)
        seen = (
            "no row"
            if r is None
            else f"status {r.status}, step {r.current_step_index}, owner {r.owner!r}, "
            f"context {r.context!r}, notes {getattr(r, 'notes', None)!r}"
        )
        with Session(engine) as s2:
            srs = s2.exec(select(StepRun).where(StepRun.workflow_run_id == rid)).all()
        seen += f", step runs {[(x.step_id, x.status, x.outcome_index) for x in srs]}"
        tails = " | ".join(w.text()[-700:] for w in WORKERS)
        raise AssertionError(f"{e}; the run was: {seen}; worker log tails: {tails!r}") from None


def summary(doc_id, rid):
    """Role-independent shape of a finished flow: no ids, no timestamps."""
    with Session(engine) as s:
        segs = s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == LOCALE)
            .order_by(Segment.order_index)
        ).all()
        steps = s.exec(
            select(StepRun, WorkflowStep)
            .where(StepRun.workflow_run_id == rid, StepRun.step_id == WorkflowStep.id)
            .order_by(StepRun.id)
        ).all()
        return {
            "targets": [sg.target_text for sg in segs],
            "steps": [[st.name, sr.status, sr.output_text] for sr, st in steps],
        }


# ---------------------------------------------------------------------------- child (role=all) mode
if _CHILD:
    wf = mk_wf(True)
    doc = mk_doc(3)
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wf, doc, LOCALE, created_by="child")
    wait_status(rid, (RunStatus.PAUSED,), 60)
    with Session(engine) as s:
        orchestrator.resume_run_async(s, rid, LOCALE, EDIT)
    done = wait_status(rid, (RunStatus.COMPLETED, RunStatus.FAILED), 60)
    out = summary(doc, rid)
    out["final_status"] = done.status
    print("WR_JSON:" + json.dumps(out, ensure_ascii=True), flush=True)
    sys.stdout.flush()
    os._exit(0)


# ---------------------------------------------------------------------------- worker processes


class Worker:
    """One `python -m app.worker` child. On Windows the venv launcher is a shim process whose
    child is the real worker, so `pid` (the Popen handle) and `wpid` (the pid the worker logs
    and stamps into WorkflowRun.owner) differ; stopping goes through both."""

    def __init__(self, extra_env=None):
        with tempfile.NamedTemporaryFile(
            "w+b", prefix="wr_worker_", suffix=".log", delete=False
        ) as lf:
            self.logname = lf.name
        env = dict(os.environ, LOC_TMS_ROLE="worker", LOC_TMS_DATA_DIR=DATA, LOG_FORMAT="json")
        env.update(extra_env or {})
        self.log = open(self.logname, "wb")  # noqa: SIM115 -- the child owns it until it exits
        self.proc = subprocess.Popen(
            [PY, "-m", "app.worker"], cwd=_ROOT, env=env, stdout=self.log, stderr=self.log
        )
        self.pid = self.proc.pid
        self.wpid = None
        WORKERS.append(self)

    def text(self):
        try:
            with open(self.logname, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def wait_up(self, timeout=90):
        end = time.time() + timeout
        pat = re.compile(r"worker (\d+) up")
        while time.time() < end:
            if self.proc.poll() is not None:
                _fail(
                    f"expected the worker (pid {self.pid}) to stay up; it exited "
                    f"{self.proc.returncode}. Log tail: {self.text()[-1500:]!r}"
                )
            m = pat.search(self.text())
            if m:
                self.wpid = int(m.group(1))
                return self
            time.sleep(0.2)
        _fail(
            f"expected 'worker <pid> up' in the worker log within {timeout}s; "
            f"log tail: {self.text()[-1500:]!r}"
        )

    def kill(self):
        for pid in (self.wpid, self.pid):
            if pid:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=30
                    )
                else:
                    with contextlib.suppress(OSError):
                        os.kill(pid, 9)
        with contextlib.suppress(Exception):
            self.proc.wait(10)
        with contextlib.suppress(Exception):
            self.log.close()


def pid_alive(pid):
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, timeout=30
        ).stdout.decode("utf-8", "replace")
        return f" {pid} " in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_all():
    for w in list(WORKERS):
        with contextlib.suppress(Exception):
            w.kill()
    WORKERS.clear()


def start_worker(extra_env=None):
    return Worker(extra_env).wait_up()


def hard_stop(w):
    """Kill a worker (its runs are done) so nothing is mid-flight in it for the next check."""
    w.kill()
    if w in WORKERS:
        WORKERS.remove(w)


def web_threads():
    return [
        t.name
        for t in threading.enumerate()
        if t.name.startswith("loc-job-") or t.name.startswith("loc-run-")
    ]


def ok(msg):
    print("  OK " + msg, flush=True)


def check_1():
    assert config.ROLE == "web", (
        f"expected this process to be LOC_TMS_ROLE=web, saw {config.ROLE!r}"
    )
    w = start_worker()
    time.sleep(2)
    assert w.proc.poll() is None, f"expected the worker to stay up, exited {w.proc.returncode}"
    ok(f"worker entry point started and stayed up (pid {w.wpid})")

    jobs.ensure_workers()
    orchestrator.ADMISSION.ensure_started()
    names = web_threads()
    assert not names, f"expected no loc-job-/loc-run- threads in the web process, saw {names[:8]}"
    ok("web role started no job or run threads")

    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    docs = [mk_doc(120) for _ in range(5)]
    jids = [
        jobs.submit("translate_document", {"document_id": d, "locale": LOCALE}, actor="wr")
        for d in docs
    ]
    lat = []
    t_end = time.time() + 30
    while time.time() < t_end:
        t0 = time.perf_counter()
        r = c.get("/health")
        lat.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200, f"expected /health 200, saw {r.status_code}"
        if all(jobs.get(j).status in (JobStatus.COMPLETED, JobStatus.FAILED) for j in jids):
            break
        time.sleep(0.01)
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    assert p95 < 50, f"expected /health p95 < 50 ms while the worker runs jobs, saw {p95:.1f} ms"
    states = [(j, jobs.get(j).status, jobs.get(j).error) for j in jids]
    bad = [x for x in states if x[1] != JobStatus.COMPLETED]
    assert not bad, f"expected every translate_document job COMPLETED by the worker, saw {bad}"
    ok(
        f"{len(jids)} translate jobs completed in the worker; /health p95 {p95:.1f} ms over {len(lat)} calls"
    )
    hard_stop(w)


def check_2_3():
    global WF_REVIEW, DOC_REVIEW, RID_REVIEW
    WF_REVIEW, DOC_REVIEW = mk_wf(True), mk_doc(3)
    # No worker is running here, so QUEUED cannot be claimed under our feet.
    d1, d2 = {}, {}
    with Session(engine) as s:
        rid = orchestrator.start_run(s, WF_REVIEW, DOC_REVIEW, LOCALE, created_by="wr", detail=d1)
    RID_REVIEW = rid
    row = run_row(rid)
    assert row.status == RunStatus.QUEUED, (
        f"expected run {rid} QUEUED after web launch, saw {row.status}"
    )
    assert d1.get("queued") is True, f"expected detail['queued'] True, saw {d1}"
    assert not d1.get("reattached"), f"expected a first launch not reattached, saw {d1}"
    with Session(engine) as s:
        rid2 = orchestrator.start_run(s, WF_REVIEW, DOC_REVIEW, LOCALE, created_by="wr", detail=d2)
    assert rid2 == rid and d2.get("reattached") is True, (
        f"expected the second identical launch to re-attach to run {rid}; saw run {rid2}, detail {d2}"
    )
    assert run_row(rid).status == RunStatus.QUEUED, (
        "expected the re-attached run to still be QUEUED"
    )
    ok("web launch is a QUEUED row; second identical launch re-attaches to the same run")

    w = start_worker()
    paused = wait_status(rid, (RunStatus.PAUSED, RunStatus.FAILED, RunStatus.COMPLETED), 90)
    assert paused.status == RunStatus.PAUSED, (
        f"expected the worker to run {rid} to PAUSED, saw {paused.status}; notes {getattr(paused, 'notes', '')!r}; "
        f"worker log tail {w.text()[-1200:]!r}"
    )
    sm = summary(DOC_REVIEW, rid)
    ai = [x for x in sm["steps"] if x[0] in ("Translate", "Polish")]
    assert [x[0] for x in ai] == ["Translate", "Polish"] and all(
        x[1] == RunStatus.COMPLETED for x in ai
    ), f"expected both AI StepRuns completed, saw steps {sm['steps']}"
    ok("worker executed the queued launch to PAUSED with both AI steps completed")
    hard_stop(w)

    # ---- check 3: web-role resume
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    before = run_row(rid)
    r = c.post(
        f"/runs/{rid}/resume",
        data={"locale": LOCALE, "edited_text": EDIT},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"expected the resume route to answer 303, saw {r.status_code}"
    after = run_row(rid)
    assert after.status == RunStatus.QUEUED, (
        f"expected the resumed run QUEUED (no worker is up), saw {after.status}"
    )
    qr = (after.context or {}).get(orchestrator.QUEUED_RESUME_KEY)
    assert isinstance(qr, dict) and qr.get("seed") == EDIT, (
        f"expected run.context['_queued_resume']['seed'] == the edited text, saw context {after.context!r}"
    )
    assert after.current_step_index == before.current_step_index + 1, (
        f"expected the cursor advanced by one from {before.current_step_index}, saw {after.current_step_index}"
    )
    ok("web resume leaves the run QUEUED with the edited text and cursor + 1")

    w = start_worker()
    done = wait_status(rid, (RunStatus.COMPLETED, RunStatus.FAILED), 90)
    assert done.status == RunStatus.COMPLETED, (
        f"expected the worker to complete the resumed run, saw {done.status}; log {w.text()[-1200:]!r}"
    )
    assert orchestrator.QUEUED_RESUME_KEY not in (done.context or {}), (
        f"expected the seed key gone after the worker took the run, saw {done.context!r}"
    )
    got = summary(DOC_REVIEW, rid)
    hard_stop(w)

    env = dict(os.environ, WR_CHILD="all", LOC_TMS_ROLE="all")
    env.pop("LOC_TMS_DATA_DIR", None)
    cp = subprocess.run(
        [PY, os.path.abspath(__file__)], cwd=_ROOT, env=env, capture_output=True, timeout=240
    )
    out = cp.stdout.decode("utf-8", "replace")
    line = next((x for x in out.splitlines() if x.startswith("WR_JSON:")), None)
    assert line, (
        f"expected the role=all child to print WR_JSON; rc {cp.returncode}, stdout tail {out[-800:]!r}, "
        f"stderr tail {cp.stderr.decode('utf-8', 'replace')[-800:]!r}"
    )
    ref = json.loads(line[len("WR_JSON:") :])
    assert ref["final_status"] == RunStatus.COMPLETED, f"role=all reference did not complete: {ref}"
    assert got["targets"] == ref["targets"], (
        f"expected web+worker final targets == role=all targets {ref['targets']}, saw {got['targets']}"
    )
    assert got["steps"] == ref["steps"], (
        f"expected web+worker StepRuns == role=all StepRuns {ref['steps']}, saw {got['steps']}"
    )
    assert got["targets"] == EDIT_PARTS, (
        f"expected the final segment targets to be the edited parts {EDIT_PARTS}, saw {got['targets']}"
    )
    ok("worker completed the queued resume; seed key gone; output identical to the role=all flow")


def check_4_5():
    env = {"LOC_TMS_MAX_CONCURRENT_RUNS": "2"}
    wa, wb = Worker(env), Worker(env)
    wa.wait_up()
    wb.wait_up()
    wf = mk_wf(False)
    rids = []
    for _ in range(6):
        d = mk_doc(60)
        with Session(engine) as s:
            rids.append(orchestrator.start_run(s, wf, d, LOCALE, created_by="wr"))
    assert len(set(rids)) == 6, f"expected 6 distinct runs, saw {rids}"
    rows = {}
    for rid in rids:
        rows[rid] = wait_status(rid, (RunStatus.COMPLETED, RunStatus.FAILED), 120)
    notdone = [(r, x.status) for r, x in rows.items() if x.status != RunStatus.COMPLETED]
    assert not notdone, f"expected all 6 runs COMPLETED, saw {notdone}; log A {wa.text()[-600:]!r}"
    with Session(engine) as s:
        srs = s.exec(select(StepRun).where(StepRun.workflow_run_id.in_(rids))).all()
    keys = [x.idempotency_key for x in srs]
    assert all(keys), f"expected every StepRun to carry an idempotency_key, saw {keys}"
    assert len(keys) == len(set(keys)), (
        f"expected unique idempotency keys, saw duplicates {sorted(k for k in keys if keys.count(k) > 1)}"
    )
    visits = [(x.workflow_run_id, x.step_id, x.outcome_index) for x in srs]
    assert len(visits) == len(set(visits)), (
        f"expected each (run, step, visit) once, saw duplicates {[v for v in set(visits) if visits.count(v) > 1]}"
    )
    assert len(srs) == 12, f"expected 12 StepRuns (6 runs x 2 steps), saw {len(srs)}"
    owners = {}
    for rid, x in rows.items():
        o = x.owner or ""
        owners[rid] = next((str(p.wpid) for p in (wa, wb) if f":{p.wpid}:" in o), None)
    unknown = {r: rows[r].owner for r, o in owners.items() if o is None}
    assert not unknown, f"expected WorkflowRun.owner to name a worker host:pid, saw {unknown}"
    taken = set(owners.values())
    assert len(taken) == 2, (
        f"expected both workers ({wa.wpid}, {wb.wpid}) to take at least one run, saw ownership {owners}"
    )
    ok(
        f"two workers, 6 runs, no double execution (owners {sorted(owners.values())}, 12 unique StepRuns)"
    )

    # ---- check 5: cross-process run events
    from app.services import events
    from app.services import orchestrator_part10 as p10

    doc = mk_doc(400)
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wf, doc, LOCALE, created_by="wr")
    sub = events.subscribe(events.run_channel(rid))
    got_events = []
    stream = []
    gate = {"closed_at": None}

    def collect():
        end = time.time() + 120
        while time.time() < end and gate["closed_at"] is None:
            got_events.extend(sub.poll(timeout=0.5))

    old = p10.WEB_OBSERVE_FALLBACK_S
    p10.WEB_OBSERVE_FALLBACK_S = 10.0
    th = threading.Thread(target=collect, daemon=True)
    th.start()
    try:
        # observe_run on a QUEUED row closes at once, so open the stream once the run is live.
        _wait(
            lambda: (
                (run_row(rid).status == RunStatus.RUNNING)
                or run_row(rid).status
                in (
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                )
            ),
            60,
            f"run {rid} to start",
            step=0.05,
        )
        t_open = time.time()
        for _run, ev in p10.observe_run(rid):
            stream.append((time.time(), ev))
            if ev.get("type") in ("closed", "done"):
                gate["closed_at"] = datetime.utcnow()
                break
        t_close = time.time()
    finally:
        p10.WEB_OBSERVE_FALLBACK_S = old
        gate["closed_at"] = gate["closed_at"] or datetime.utcnow()
        th.join(5)
        sub.close()
    fin = run_row(rid)
    assert fin.status == RunStatus.COMPLETED, f"expected run {rid} COMPLETED, saw {fin.status}"
    assert got_events, (
        f"expected at least one event on {events.run_channel(rid)} published by the worker, saw none"
    )
    types = [e.get("type") for _, e in stream]
    assert stream and types[-1] in ("closed", "done"), (
        f"expected the stream to reach its end, saw {types}"
    )
    lag = (gate["closed_at"] - fin.finished_at).total_seconds() if fin.finished_at else None
    assert lag is not None and lag < 3.0, (
        f"expected the stream to report the end within 3 s of the run's commit with a 10 s fallback "
        f"(an event must wake it), saw lag {lag} s; stream types {types[-6:]}"
    )
    assert (t_close - t_open) < 60, "expected the stream to end promptly"
    ok(
        f"{len(got_events)} run events crossed processes; observe_run (10 s fallback) ended "
        f"{lag:.2f}s after the commit"
    )
    hard_stop(wa)
    hard_stop(wb)


def check_6():
    from app.ports import jobqueue

    assert config.ROLE == "web"
    err = getattr(jobqueue, "JobQueueError", None)
    assert err is not None, "expected app.ports.jobqueue.JobQueueError to exist"
    settings.set_many({"job_queue_backend": "memory"}, actor="verify_worker_roles")
    settings.invalidate_cache()
    try:
        try:
            jobqueue.configure_at_boot()
        except err as e:
            msg = str(e)
        else:
            _fail(
                "expected configure_at_boot() to raise JobQueueError for memory + role=web, it returned"
            )
    finally:
        settings.set_many({"job_queue_backend": "db"}, actor="verify_worker_roles")
        settings.invalidate_cache()
    assert "memory" in msg.lower() or "role" in msg.lower(), (
        f"expected a message naming memory/role, saw {msg!r}"
    )
    jobqueue.configure_at_boot()  # db again: must not raise
    ok("memory job queue refused with LOC_TMS_ROLE=web; db accepted")


def check_8():
    """Keys translate in the web role: rows stay QUEUED, nothing executes here, no drain_once.
    Pre-fix (ce0cbb94e): keys_hub2.launch_translation called ADMISSION.drain_once
    unconditionally (the call now guarded by `if config.runs_background_work():`), so the web
    process admitted and ran the rows itself -> drain_once recorded and loc-run- threads."""
    from unittest.mock import patch

    from app.models import ProjectType
    from app.services import content as content_svc
    from app.services import keys as keys_svc

    with Session(engine) as s:
        cust = content_svc.customer_create(s, _uniq("WRCust"))
        proj = content_svc.project_create(
            s,
            _uniq("WRKeys"),
            source_locale="en-US",
            target_locales="de-DE, fr-FR",
            customer_id=str(cust.id),
            project_type=ProjectType.KEY_BASED,
        )
        pid = proj.id
    with Session(engine) as s:
        project = s.get(Project, pid)
        for i in range(2):
            keys_svc.create_key(s, project, f"wr.key.{i}", base_value=f"Hello {i}", actor="wr")
    wid = mk_wf(False)
    with Session(engine) as s, patch.object(orchestrator.ADMISSION, "drain_once") as drain:
        out = keys_svc.launch_translation(s, s.get(Project, pid), wid, "all", actor="wr")
    ids = [r["run_id"] for r in out["queued"]]
    assert len(ids) >= 2, f"expected at least 2 queued key runs (2 locales), saw {out}"
    assert drain.call_count == 0, (
        f"expected the web role never to call ADMISSION.drain_once, saw {drain.call_count} call(s)"
    )
    names = [
        t.name
        for t in threading.enumerate()
        if t.name.startswith(("loc-run-", "loc-batch-member-", "loc-job-"))
    ]
    assert not names, f"expected no loc-run-/loc-batch-member- threads here, saw {names}"
    stat = [run_row(i).status for i in ids]
    assert all(x == RunStatus.QUEUED for x in stat), (
        f"expected every key run QUEUED right after the web launch, saw {stat}"
    )
    w = start_worker()
    for i in ids:
        r = wait_status(i, (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PAUSED), 90)
        assert r.status == RunStatus.COMPLETED, (
            f"expected key run {i} COMPLETED by the worker, saw {r.status}"
        )
    ok(
        f"keys translate (web role) queued {len(ids)} runs, ran nothing here; a worker completed them"
    )
    hard_stop(w)


def check_9():
    """A booting worker leaves a live stream alone; the web boot fails it.
    Pre-fix: jobs.recover_interrupted called fail_orphaned_live_rows in every role, so the
    worker's boot recovery turned the RUNNING live row FAILED before 'worker N up'."""
    from app.models import Job

    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            payload={"document_id": 1, "locale": LOCALE, "live_stream": True},
            created_by="wr",
            status=JobStatus.RUNNING,
            message="live translation (streamed)",
            started_at=datetime.utcnow(),
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        jid = j.id
    w = start_worker()
    st = jobs.get(jid)
    assert st.status == JobStatus.RUNNING, (
        f"expected the live-stream job still RUNNING after a worker booted, saw {st.status} / {st.error!r}"
    )
    env = dict(os.environ, LOC_TMS_ROLE="web", LOC_TMS_DATA_DIR=DATA)
    code = "import os\nfrom app import main\nmain._startup()\nos._exit(0)\n"
    cp = subprocess.run([PY, "-c", code], cwd=_ROOT, env=env, capture_output=True, timeout=180)
    assert cp.returncode == 0, (
        f"expected the web-role _startup child to exit 0, saw {cp.returncode}: "
        f"{cp.stderr.decode('utf-8', 'replace')[-800:]!r}"
    )
    st = jobs.get(jid)
    assert (
        st.status == JobStatus.FAILED and st.error == "live translation interrupted by a restart"
    ), (
        "expected the web boot to fail the live job with 'live translation interrupted by a "
        f"restart', saw {st.status} / {st.error!r}"
    )
    ok("worker boot left the live-stream job RUNNING; the web _startup failed it")
    hard_stop(w)


_LEADER_CHILD = (
    "import os, time\n"
    "from app import config\n"
    "url = os.environ.get('WR_PG_URL')\n"
    "if url:\n"
    "    import psycopg\n"
    "    from app.services import events\n"
    "    config.IS_SQLITE = False\n"
    "    events._pg_listen_connection = lambda: psycopg.connect(url, autocommit=True)\n"
    "from app.services import leader\n"
    "got = leader.try_acquire(os.environ['WR_LEADER_NAME'])\n"
    "print('LEADER', os.getpid(), got, flush=True)\n"
    "if got and os.environ.get('WR_HOLD'):\n"
    "    time.sleep(300)\n"
    "os._exit(0)\n"
)


def _leader_child(name, pg_url, hold):
    env = dict(os.environ, LOC_TMS_DATA_DIR=DATA, WR_LEADER_NAME=name)
    env.pop("WR_PG_URL", None)
    env.pop("WR_HOLD", None)
    if pg_url:
        env["WR_PG_URL"] = pg_url
    if hold:
        env["WR_HOLD"] = "1"
    return subprocess.Popen(
        [PY, "-c", _LEADER_CHILD],
        cwd=_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _leader_result(p, timeout=120):
    end = time.time() + timeout
    line = ""
    while time.time() < end:
        line = p.stdout.readline().decode("utf-8", "replace").strip()
        if line.startswith("LEADER"):
            _, pid, got = line.split()
            return int(pid), got == "True"
        if not line and p.poll() is not None:
            break
    err = p.stderr.read().decode("utf-8", "replace")[-600:] if p.poll() is not None else ""
    _fail(f"expected a LEADER line from a leader child, saw {line!r} {err!r}")


def _kill_pid(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=30)
    else:
        os.kill(pid, 9)


def _leader_round(name, pg_url):
    a = _leader_child(name, pg_url, True)
    try:
        apid, agot = _leader_result(a)
        assert agot, f"expected child A to win the {name} leadership, saw False"
        b = _leader_child(name, pg_url, False)
        _, bgot = _leader_result(b)
        b.wait(30)
        assert not bgot, f"expected child B to be refused while A holds {name}, saw it acquire"
        _kill_pid(apid)
        a.wait(30)
        got = False
        end = time.time() + 20
        while time.time() < end and not got:
            c = _leader_child(name, pg_url, False)
            _, got = _leader_result(c)
            c.wait(30)
            if not got:
                time.sleep(0.5)
        assert got, f"expected child C to take {name} within 20 s of A being killed, saw False"
    finally:
        with contextlib.suppress(Exception):
            a.kill()


def check_10():
    """One scheduler leader. Pre-fix: app/worker.py called scheduler_port.get().start() in
    every worker (no leader election), so two workers both ran the tick and retention sweep."""
    from unittest.mock import patch

    from app.services import leader

    _leader_round("scheduler", None)
    ok("sqlite: A leads, B refused, C leads after A is killed")
    pg = os.environ.get("LOC_TMS_TARGET_PG_URL")
    if pg:
        _leader_round(f"scheduler-wr{os.getpid()}", pg)
        ok("postgres: A leads, B refused, C leads after A is killed")
    else:
        print("  SKIP postgres leader half: LOC_TMS_TARGET_PG_URL is not set", flush=True)

    with open(os.path.join(_ROOT, "app", "worker.py"), encoding="utf-8") as f:
        src = f.read()
    assert re.search(r"leader\.run_when_leader\(\s*[\"']scheduler[\"']", src), (
        "expected app/worker.py to start the scheduler via leader.run_when_leader('scheduler', ...)"
    )
    assert not re.search(r"scheduler_port\.get\(\)\.start\(\)", src), (
        "expected app/worker.py not to call scheduler_port.get().start() directly"
    )

    calls = []
    stop = threading.Event()
    with (
        patch.object(leader, "try_acquire", return_value=True),
        patch.object(leader, "check", return_value=True),
        patch.object(leader, "RETRY_S", 0.05),
    ):
        th = leader.run_when_leader("unit", lambda: calls.append(1), stop=stop)
        _wait(lambda: calls, 10, "start() to be called once the leadership is won")
        time.sleep(0.4)
        assert calls == [1], f"expected start() called exactly once on winning, saw {len(calls)}"
        stop.set()
        th.join(5)
    calls2 = []
    stop2 = threading.Event()
    with (
        patch.object(leader, "try_acquire", return_value=False),
        patch.object(leader, "RETRY_S", 0.05),
    ):
        th = leader.run_when_leader("unit2", lambda: calls2.append(1), stop=stop2)
        time.sleep(0.4)
        stop2.set()
        th.join(5)
    assert not calls2, f"expected start() never called by a non-leader, saw {len(calls2)} call(s)"
    ok("worker starts the scheduler only via run_when_leader, which calls start once on winning")


def check_7():
    doc = mk_doc(4000)
    w = start_worker({"LOC_TMS_DRAIN_SECONDS": "1"})
    jid = jobs.submit("translate_document", {"document_id": doc, "locale": LOCALE}, actor="wr")
    if os.name == "nt":
        _wait(lambda: jobs.get(jid).status != JobStatus.QUEUED, 60, f"job {jid} to leave QUEUED")
        t0 = time.time()
        with contextlib.suppress(OSError):
            os.kill(w.wpid, 15)  # Windows: TerminateProcess on the real worker, not the launcher
        w.proc.terminate()
        _wait(
            lambda: not pid_alive(w.wpid) and w.proc.poll() is not None,
            15,
            f"worker pid {w.wpid} to be gone after terminate()",
            step=0.2,
        )
        print(
            "  SKIP SIGTERM hand-back: Windows cannot deliver SIGTERM to a child process "
            "(terminate() is a hard kill), so the drain path is checked on POSIX only; "
            f"worker exited in {time.time() - t0:.1f}s",
            flush=True,
        )
        ok("worker exits within 15 s of terminate()")
        return
    import signal

    _wait(lambda: jobs.get(jid).status == JobStatus.RUNNING, 60, f"job {jid} RUNNING")
    w.proc.send_signal(signal.SIGTERM)
    w.proc.wait(15)
    st = jobs.get(jid).status
    assert st == JobStatus.QUEUED, (
        f"expected the running job handed back to QUEUED after SIGTERM, saw {st}; log {w.text()[-800:]!r}"
    )
    ok("SIGTERM drained the worker and handed the running job back to QUEUED")


def main():
    t0 = time.time()
    try:
        check_1()
        check_2_3()
        check_4_5()
        check_6()
        check_8()
        check_9()
        check_10()
        check_7()
    finally:
        stop_all()
    print(f"  total {time.time() - t0:.0f}s", flush=True)
    print("WORKER ROLES OK", flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        stop_all()
        raise
    os._exit(0)
