"""AUDIT-core-qa-part1 Q2 (HIGH) — /api workflow run + run-batch must NOT execute the
whole workflow inline on the FastAPI event loop.

Hermetic + offline: throwaway LOC_TMS_DATA_DIR + blanked provider keys, so every run
resolves through MockProvider (or an injected provider) and ZERO live calls are made.

The INVARIANT under test: `orchestrator.run_workflow` (a blocking, minutes-to-hours
function) is never awaited on the event loop. A single long run must not be able to
starve /ready / health checks / every other request, and a remote client timeout must
not abandon a run that keeps executing unobserved.

Because a TestClient drives the app WITHOUT a real event loop, loop-starvation is not
directly observable here; instead we prove the ROUTE HANDLER never runs the workflow
inline:

  async launch (default)    POST /api/workflows/{id}/run returns {kind:"run", run_id,
                            status} PROMPTLY, via orchestrator.start_run (background
                            thread) -- never calling orchestrator.run_workflow, and
                            returning BEFORE the background work finishes.
  no-inline proof           with orchestrator.run_workflow monkeypatched to a tripwire
                            AND the background dispatch stubbed out, the default route
                            still returns a run_id and the tripwire is NEVER hit.
  returns-before-work       with the background dispatch replaced by a 1s sleeper, the
                            route returns in < 0.5s and does NOT wait for it to finish.
  wait=true (opt-in)        runs the SAME workflow OFF the loop in a threadpool worker
                            (api_cli._run_workflow_blocking) and returns the finished-run
                            shape (text/steps); the DEFAULT response carries no `text`.
  off-loop observable       run_in_threadpool is spied: the wait path offloads
                            _run_workflow_blocking, the default async launch never does.
  wait=true dedup (F1)      two CONCURRENT wait=true launches over one (wf, doc, locale)
                            re-attach to ONE run (start_run's _RUN_CREATE_LOCK), never two
                            parallel executions over the same segment set -- the MCP path.
  run-batch async           POST .../run-batch defaults to start_batch(synchronous=False)
                            (a daemon dispatcher) and polls to a terminal state; wait=true
                            uses synchronous=True OFF the loop and returns the finalized batch.
  archive refusal           an archived workflow is a clean 4xx AT LAUNCH on BOTH the async
                            and the wait path (never a dead background run).
  double-submit re-attach    two rapid launches over one (wf, doc, locale) return the SAME
                            run_id (start_run's guard preserved through the async route).
  cross-workflow conflict    GitHub #213 part 2: a DIFFERENT workflow on a busy (doc,
                            locale) is a 409 {"code": "run_conflict", "run_id": N} on the
                            async AND wait arms, writes no row; another locale starts; a
                            terminal rival releases; the local and --remote CLI arms print
                            the SAME sentence and exit non-zero.
  org/locale gating         a missing workflow/doc is 404; a locale not on the doc is 400.
  remote CLI launch+poll    `workflow run` over --remote LAUNCHES then POLLS to a terminal
                            state and prints the SAME human line (completed / paused / batch
                            rollup) as the in-process arm.

Final line: "API WORKFLOW RUN ASYNC OK".
"""

import io
import json as _json
import os
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._api_cli_patch import set_api_cli
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_wfrunasync_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    StepType,
    TargetLocale,
    TransMode,
    WorkflowRun,
)
from app.services import (
    apikeys,
    batch_runs,
    orchestrator,
    security,
    settings,
)
from app.services import workflows as W
from app.services.providers import Provider, register_provider
from app.web import api_cli

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

_N = 0


def _uniq(prefix="x"):
    global _N
    _N += 1
    return f"{prefix}-{_N}"


# --- a provider that BLOCKS until released (holds a run in RUNNING for the re-attach
#     proof and lets us observe "launched but not finished"). -------------------------
class HoldProvider(Provider):
    name = "holdmock"
    release = threading.Event()

    @classmethod
    def reset(cls):
        cls.release = threading.Event()

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        HoldProvider.release.wait(timeout=15)
        return "ok-" + ((payload or {}).get("text", "") or "x")


register_provider("holdmock", HoldProvider())


# --- fixtures -----------------------------------------------------------------------
def _mk_project_doc(locales=("es-ES", "fr-FR"), source="en-US"):
    with Session(engine) as s:
        p = Project(name=_uniq("RunAsyncProj"), source_locale=source)
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in locales:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        doc = Document(
            project_id=p.id,
            filename="doc.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hello world."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for loc in locales:
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=loc,
                    order_index=0,
                    source_text="Hello world.",
                    target_text="",
                )
            )
        s.commit()
        return p.id, doc.id


def _mk_wf(provider="mock", human_review=False):
    with Session(engine) as s:
        w = W.create_workflow(s, _uniq("RunAsyncWf"))
        W.add_step(
            s,
            w.id,
            name="Translator",
            provider=provider,
            model="m1",
            prompt_text="Translate {text}",
            chunk_policy="none",
        )
        if human_review:
            W.add_step(s, w.id, name="Human", step_type=StepType.HUMAN_REVIEW)
        return w.id


def _api_key(email=None, scopes="api docs"):
    email = email or _uniq("runasync") + "@x.io"
    admin = security.create_user(email, _uniq("u"), "admin", "fixture-pass-1234!!")
    _row, raw = apikeys.create_key(admin.id, _uniq("key"), scopes=scopes)
    return raw


def _client_and_head():
    return TestClient(app), {"X-Loc-Token": _api_key()}


def _wait_run_terminal(run_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            r = s.get(WorkflowRun, run_id)
            if r is not None and r.status in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.PAUSED,
                RunStatus.CANCELLED,
            ):
                return r.status
        time.sleep(0.05)
    return None


def _wait_for_run_row(wf, did, locale, timeout=10.0):
    """Return the newest WorkflowRun id for (wf, doc, locale) once it exists (start_run
    commits the row before its background thread runs), else None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            r = s.exec(
                select(WorkflowRun)
                .where(
                    WorkflowRun.workflow_id == wf,
                    WorkflowRun.document_id == did,
                    WorkflowRun.target_locale == locale,
                )
                .order_by(WorkflowRun.id.desc())
            ).first()
            if r is not None:
                return r.id
        time.sleep(0.02)
    return None


# ============================================================================
# 1. async launch returns promptly, BEFORE the background work finishes
# ============================================================================
def test_run_returns_before_work_finishes():
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="mock")
    client, head = _client_and_head()

    started = threading.Event()
    finished = threading.Event()
    real_bg = orchestrator._run_in_background

    def slow_bg(*a, **k):
        def worker():
            started.set()
            time.sleep(1.0)
            finished.set()

        threading.Thread(target=worker, daemon=True).start()

    set_pieces("app.services.orchestrator", "_run_in_background", slow_bg)
    try:
        t0 = time.time()
        r = client.post(
            f"/api/workflows/{wf}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
        )
        elapsed = time.time() - t0
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["kind"] == "run", body
        assert isinstance(body["run_id"], int) and body["status"] == "running", body
        # the launch ack must NOT carry the finished-run text (that would mean it ran inline)
        assert "text" not in body, body
        assert elapsed < 0.5, f"route blocked on the workflow execution ({elapsed:.2f}s)"
        assert not finished.is_set(), "route waited for the background work to finish"
        finished.wait(timeout=3)  # cleanup: let the sleeper end
    finally:
        set_pieces("app.services.orchestrator", "_run_in_background", real_bg)
    print(
        "  async launch: POST /run returns {kind,run_id,status:running} in "
        f"{elapsed * 1000:.0f}ms, before the background work finishes, with no inline text OK"
    )


# ============================================================================
# 2. the route handler NEVER calls orchestrator.run_workflow (no inline execution)
# ============================================================================
def test_run_never_calls_run_workflow_inline():
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="mock")
    client, head = _client_and_head()

    tripwire = []
    real_rw = orchestrator.run_workflow
    real_bg = orchestrator._run_in_background

    def tripwire_run_workflow(*a, **k):
        tripwire.append((a, k))
        raise AssertionError(
            "orchestrator.run_workflow was called on the /run route "
            "-- the workflow is executing INLINE on the event loop"
        )

    set_pieces("app.services.orchestrator", "run_workflow", tripwire_run_workflow)
    set_pieces(
        "app.services.orchestrator", "_run_in_background", lambda *a, **k: None
    )  # no real thread; keep it clean
    try:
        r = client.post(
            f"/api/workflows/{wf}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
        )
        assert r.status_code == 200, r.text
        assert isinstance(r.json().get("run_id"), int), r.text
        assert not tripwire, "run_workflow was invoked by the async /run route"
    finally:
        set_pieces("app.services.orchestrator", "run_workflow", real_rw)
        set_pieces("app.services.orchestrator", "_run_in_background", real_bg)
    print(
        "  no-inline: the default /run route creates a run via start_run and NEVER "
        "calls orchestrator.run_workflow (proven with a tripwire) OK"
    )


# ============================================================================
# 3. wait=true runs OFF the loop (threadpool worker) and returns the finished shape
# ============================================================================
def test_wait_true_offloop_finished_shape():
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="mock")
    client, head = _client_and_head()

    seen = []
    real_blocking = api_cli._run_workflow_blocking

    def spy_blocking(*a, **k):
        seen.append((a, k))
        return real_blocking(*a, **k)

    set_api_cli("_run_workflow_blocking", spy_blocking)
    try:
        r = client.post(
            f"/api/workflows/{wf}/run",
            headers=head,
            json={"doc_id": did, "locale": "es-ES", "wait": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["kind"] == "run", body
        assert body["status"] == "completed", body
        assert "text" in body and body["steps"] >= 1, body
        assert seen, "wait=true did not route through the threadpool worker _run_workflow_blocking"
    finally:
        set_api_cli("_run_workflow_blocking", real_blocking)
    # ...and the DEFAULT (no wait) response is a bare ack with no finished-run text.
    r2 = client.post(
        f"/api/workflows/{wf}/run", headers=head, json={"doc_id": did, "locale": "fr-FR"}
    )
    assert r2.status_code == 200 and "text" not in r2.json(), r2.text
    print(
        "  wait=true: runs off-loop via _run_workflow_blocking and returns the finished "
        "shape (text+steps); the default launch carries no text OK"
    )


# ============================================================================
# 3b. the off-loop invariant is OBSERVABLE: the wait path awaits run_in_threadpool,
#     the default (async-launch) path does NOT (review F4).
# ============================================================================
def test_wait_path_awaits_run_in_threadpool():
    """Under TestClient there is no real event loop, so 'the blocking work runs OFF the
    loop' cannot be observed by timing. Make it observable structurally: spy on
    api_cli.run_in_threadpool and assert the CALL PATTERN. The default launch must NEVER
    offload (it does no blocking work — start_run returns at once); wait=true MUST hand the
    blocking worker (_run_workflow_blocking) to run_in_threadpool. This kills the mutation
    that runs the blocking helper DIRECTLY on the event loop for the wait path."""
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="mock")
    client, head = _client_and_head()

    offloaded = []
    real_ritp = api_cli.run_in_threadpool

    def spy_ritp(fn, *a, **k):
        offloaded.append(getattr(fn, "__name__", repr(fn)))
        return real_ritp(fn, *a, **k)

    set_api_cli("run_in_threadpool", spy_ritp)
    try:
        # DEFAULT (no wait): a prompt async launch — it must NOT touch run_in_threadpool.
        client.post(
            f"/api/workflows/{wf}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
        )
        assert offloaded == [], (
            "the default async launch offloaded to run_in_threadpool "
            "(it should return without blocking work)",
            offloaded,
        )
        # wait=true: the blocking worker MUST run via run_in_threadpool (off the loop).
        client.post(
            f"/api/workflows/{wf}/run",
            headers=head,
            json={"doc_id": did, "locale": "fr-FR", "wait": True},
        )
        assert "_run_workflow_blocking" in offloaded, (
            "wait=true did not run _run_workflow_blocking via run_in_threadpool -- the "
            "blocking work may be executing ON the event loop",
            offloaded,
        )
    finally:
        set_api_cli("run_in_threadpool", real_ritp)
    print(
        "  off-loop: wait=true hands _run_workflow_blocking to run_in_threadpool; the "
        "default launch never offloads (the off-loop invariant, made observable) OK"
    )


# ============================================================================
# 3c. F1: two concurrent wait=true launches over one (wf, doc, locale) re-attach to
#     ONE run (MCP always sends wait=true — this is the live double-submit path).
# ============================================================================
def test_wait_true_concurrent_dedup():
    """Before the F1 fix the wait path called orchestrator.run_workflow, which creates a
    FRESH WorkflowRun with NO _RUN_CREATE_LOCK and NO re-attach — so two concurrent
    same-(wf, doc, locale) wait=true calls ran the SAME segment set on two truly-parallel
    threadpool worker threads (SQLite 'database is locked' / duplicated provider spend).
    Routing the wait path through start_run gives it the SAME dedup the default path has:
    the second launch RE-ATTACHES to the first run and both callers poll that ONE run to
    completion. This is the exact path MCP's run_workflow tool takes (it always sends
    wait=true)."""
    HoldProvider.reset()
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="holdmock")  # every run BLOCKS in the provider until released
    key = _api_key(scopes="api docs")
    head = {"X-Loc-Token": key}
    results: dict = {}

    def fire(tag):
        c = TestClient(app)  # a separate client/loop per thread -> genuinely concurrent
        r = c.post(
            f"/api/workflows/{wf}/run",
            headers=head,
            json={"doc_id": did, "locale": "es-ES", "wait": True},
        )
        results[tag] = (r.status_code, r.json())

    t1 = threading.Thread(target=fire, args=("a",), daemon=True)
    t2 = threading.Thread(target=fire, args=("b",), daemon=True)
    t1.start()
    # let the FIRST call create + start its run (now blocked in the provider, status
    # RUNNING) BEFORE the second launches, so the second hits start_run's re-attach guard.
    first_run = _wait_for_run_row(wf, did, "es-ES", timeout=10)
    assert first_run is not None, "the first wait=true launch never created a run row"
    t2.start()
    time.sleep(0.3)  # let the second call reach start_run (re-attach) + begin observing
    HoldProvider.release.set()  # both callers' provider calls unblock
    t1.join(30)
    t2.join(30)
    assert "a" in results and "b" in results, results
    (sa, ba), (sb, bb) = results["a"], results["b"]
    assert sa == 200 and sb == 200, results
    assert ba.get("run_id") == bb.get("run_id") == first_run, (
        "concurrent wait=true launches did not re-attach to ONE run",
        results,
    )
    assert ba.get("status") == "completed" and bb.get("status") == "completed", results
    # EXACTLY ONE run row for this (wf, doc, locale) — no duplicated execution.
    with Session(engine) as s:
        rows = s.exec(
            select(WorkflowRun).where(
                WorkflowRun.workflow_id == wf,
                WorkflowRun.document_id == did,
                WorkflowRun.target_locale == "es-ES",
            )
        ).all()
    assert len(rows) == 1, ("wait=true double-submit created >1 run row", [x.id for x in rows])
    print(
        "  concurrent wait=true: two same-(wf,doc,locale) launches re-attach to ONE run "
        "and create exactly one run row (F1 dedup on the MCP path) OK"
    )


# ============================================================================
# 4. run-batch: default = async daemon (synchronous=False); wait=true = synchronous
# ============================================================================
def test_run_batch_async_default_and_wait():
    _pid, did = _mk_project_doc(("es-ES", "fr-FR"))
    wf = _mk_wf(provider="mock")
    client, head = _client_and_head()

    calls = []
    real_sb = batch_runs.start_batch

    def spy_start_batch(*a, **k):
        calls.append(k.get("synchronous"))
        return real_sb(*a, **k)

    set_pieces("app.services.batch_runs", "start_batch", spy_start_batch)
    try:
        r = client.post(f"/api/workflows/{wf}/run-batch", headers=head, json={"doc_id": did})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "batch", body
        assert calls and calls[-1] is False, (
            "default run-batch must dispatch async (synchronous=False)",
            calls,
        )
        bid = body["id"]
        # poll the daemon-dispatched batch to its terminal rollup
        for _ in range(200):
            g = client.get(f"/api/batches/{bid}", headers=head).json()
            if g["status"] != "running":
                break
            time.sleep(0.05)
        assert g["status"] == "completed", g
        assert g["counts"]["requested"] == 2 and g["counts"]["completed"] == 2, g

        # wait=true runs the SAME dispatch synchronously OFF the loop and finalizes.
        calls.clear()
        r2 = client.post(
            f"/api/workflows/{wf}/run-batch", headers=head, json={"doc_id": did, "wait": True}
        )
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert calls and calls[-1] is True, ("wait=true run-batch must be synchronous", calls)
        assert b2["status"] == "completed" and b2["counts"]["completed"] == 2, b2
    finally:
        set_pieces("app.services.batch_runs", "start_batch", real_sb)
    print(
        "  run-batch: default dispatches async (synchronous=False) then polls to "
        "completed; wait=true finalizes off-loop (synchronous=True) OK"
    )


# ============================================================================
# 5. archive refusal is a clean 4xx at launch on BOTH the async and the wait path
# ============================================================================
def test_archive_refusal_4xx_both_paths():
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="mock")
    with Session(engine) as s:
        W.archive_workflow(s, wf, actor="test")
    client, head = _client_and_head()

    r = client.post(
        f"/api/workflows/{wf}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
    )
    assert r.status_code == 400 and r.json()["ok"] is False, r.text
    assert "archiv" in (r.json().get("error") or "").lower(), r.text
    # no run row was created for the refused launch
    with Session(engine) as s:
        rows = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wf)).all()
        assert not rows, ("archived launch created a run row", [x.id for x in rows])

    r2 = client.post(
        f"/api/workflows/{wf}/run",
        headers=head,
        json={"doc_id": did, "locale": "es-ES", "wait": True},
    )
    assert r2.status_code == 400 and r2.json()["ok"] is False, r2.text
    assert "archiv" in (r2.json().get("error") or "").lower(), r2.text

    r3 = client.post(f"/api/workflows/{wf}/run-batch", headers=head, json={"doc_id": did})
    assert r3.status_code == 400 and r3.json()["ok"] is False, r3.text
    print(
        "  archive: an archived workflow is a clean 400 at launch on the async, the "
        "wait, and the run-batch paths -- no dead background run OK"
    )


# ============================================================================
# 6. double-submit re-attach survives the async route (start_run guard preserved)
# ============================================================================
def test_double_submit_reattach():
    HoldProvider.reset()
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="holdmock")  # first run BLOCKS in the provider, stays RUNNING
    client, head = _client_and_head()
    try:
        r1 = client.post(
            f"/api/workflows/{wf}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
        )
        assert r1.status_code == 200, r1.text
        rid1 = r1.json()["run_id"]
        # the run is now RUNNING (thread parked in the provider); a second launch over the
        # SAME (wf, doc, locale) must RE-ATTACH, never spawn a second execution.
        r2 = client.post(
            f"/api/workflows/{wf}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
        )
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert b2["run_id"] == rid1, (
            "second launch spawned a new run instead of re-attaching",
            rid1,
            b2,
        )
        assert b2["reattached"] is True, b2
    finally:
        HoldProvider.release.set()
    _wait_run_terminal(rid1)
    # exactly ONE run row exists for this (wf, doc, locale)
    with Session(engine) as s:
        rows = s.exec(
            select(WorkflowRun).where(
                WorkflowRun.workflow_id == wf,
                WorkflowRun.document_id == did,
                WorkflowRun.target_locale == "es-ES",
            )
        ).all()
        assert len(rows) == 1, ("re-attach failed: multiple run rows", [x.id for x in rows])
    print(
        "  double-submit: two rapid launches over one (wf, doc, locale) return the "
        "SAME run_id (reattached) and create exactly one run row OK"
    )


# ============================================================================
# 6b. GitHub #213 part 2 — a DIFFERENT workflow on a busy (document, locale) is a
#     clean 409 run_conflict (both arms), names the running run, leaves no row;
#     another locale starts; once the rival is terminal the launch goes through.
# ============================================================================
def test_cross_workflow_conflict_409():
    HoldProvider.reset()
    _pid, did = _mk_project_doc()
    wfA = _mk_wf(provider="holdmock")  # first run BLOCKS in the provider, stays RUNNING
    wfB = _mk_wf(provider="holdmock")
    client, head = _client_and_head()
    ridB = None
    try:
        r1 = client.post(
            f"/api/workflows/{wfA}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
        )
        assert r1.status_code == 200, r1.text
        rid = r1.json()["run_id"]
        r2 = client.post(
            f"/api/workflows/{wfB}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
        )
        assert r2.status_code == 409, r2.text
        b2 = r2.json()
        assert b2["ok"] is False and b2["code"] == "run_conflict" and b2["run_id"] == rid, b2
        assert f"run #{rid}" in b2["error"], b2
        # the wait=true arm refuses identically (start_run fires inside the worker)
        r3 = client.post(
            f"/api/workflows/{wfB}/run",
            headers=head,
            json={"doc_id": did, "locale": "es-ES", "wait": True},
        )
        assert r3.status_code == 409 and r3.json()["code"] == "run_conflict", r3.text
        # a DIFFERENT locale of the same document still starts
        r4 = client.post(
            f"/api/workflows/{wfB}/run", headers=head, json={"doc_id": did, "locale": "fr-FR"}
        )
        assert r4.status_code == 200 and r4.json()["run_id"] != rid, r4.text
        ridB = r4.json()["run_id"]
        with Session(engine) as s:
            rows = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wfB)).all()
        assert [x.target_locale for x in rows] == ["fr-FR"], (
            "the refused launch wrote a run row",
            [(x.id, x.target_locale) for x in rows],
        )
    finally:
        HoldProvider.release.set()
    _wait_run_terminal(rid)
    if ridB is not None:
        _wait_run_terminal(ridB)
    # the first run is terminal: the second workflow starts now (not retryable-forever)
    r5 = client.post(
        f"/api/workflows/{wfB}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
    )
    assert r5.status_code == 200 and r5.json()["reattached"] is False, r5.text
    _wait_run_terminal(r5.json()["run_id"])
    print(
        "  #213 part 2: a DIFFERENT workflow on a busy (doc, locale) is a 409 "
        "run_conflict naming the run (async + wait arms), writes no row; another locale "
        "starts; after the rival completes the launch goes through OK"
    )


# ============================================================================
# 6c. GitHub #213 part 2, CLI arms: local and --remote exit non-zero with the
#     SAME sentence (the remote arm keeps the server's message verbatim).
# ============================================================================
def test_cross_workflow_conflict_cli_arms_same_text():
    HoldProvider.reset()
    _pid, did = _mk_project_doc()
    wfA = _mk_wf(provider="holdmock")
    wfB = _mk_wf(provider="holdmock")
    key = _api_key(scopes="api docs")
    client, head = _client_and_head()
    r1 = client.post(
        f"/api/workflows/{wfA}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
    )
    assert r1.status_code == 200, r1.text
    rid = r1.json()["run_id"]
    try:
        # LOCAL arm: the in-process CLI refuses through orchestrator.run_workflow's
        # guard -- non-zero, machine code, the running run's id.
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc_local = cli_main(["--json", "workflow", "run", str(did), str(wfB), "es-ES"])
        o_local = _json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        assert rc_local != 0, rc_local
        assert o_local.get("ok") is False and o_local.get("code") == "run_conflict", o_local
        assert o_local.get("run_id") == rid, o_local
        # --REMOTE arm: the 409 body straight back out, sentence for sentence.
        run_remote = _remote_runner(key, as_json=True)
        rc_remote, o_remote = run_remote(["workflow", "run", str(did), str(wfB), "es-ES"])
        assert rc_remote != 0, rc_remote
        assert o_remote.get("code") == "run_conflict", o_remote
        assert o_remote.get("error") == o_local.get("error"), (
            "the two CLI arms must print ONE message",
            o_remote.get("error"),
            o_local.get("error"),
        )
        assert f"run #{rid}" in (o_remote.get("error") or "")
    finally:
        HoldProvider.release.set()
    _wait_run_terminal(rid)
    print(
        "  #213 part 2: local and --remote CLI both exit non-zero on run_conflict with "
        "the identical sentence naming the running run OK"
    )


# ============================================================================
# 6d. GitHub #213 fix round 1 — the WEB surface: /documents/{id}/run answers the
#     themed 409 page carrying the same refusal sentence (the /api 409 and the
#     CLI arms are pinned above; MCP relays the same body, pinned in
#     verify_mcp_tools).
# ============================================================================
def test_cross_workflow_conflict_web_page():
    HoldProvider.reset()
    _pid, did = _mk_project_doc()
    wfA = _mk_wf(provider="holdmock")
    wfB = _mk_wf(provider="holdmock")
    client, head = _client_and_head()
    r1 = client.post(
        f"/api/workflows/{wfA}/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
    )
    assert r1.status_code == 200, r1.text
    rid = r1.json()["run_id"]
    try:
        r2 = client.post(
            f"/documents/{did}/run",
            data={"workflow_id": str(wfB), "locale": "es-ES"},
            follow_redirects=False,
        )
        assert r2.status_code == 409, (r2.status_code, r2.text[:400])
        assert f"run #{rid}" in r2.text, r2.text[:400]
        assert "already running" in r2.text and "cancel" in r2.text.lower(), r2.text[:400]
        with Session(engine) as s:
            rows = s.exec(
                select(WorkflowRun).where(
                    WorkflowRun.workflow_id == wfB, WorkflowRun.document_id == did
                )
            ).all()
        assert not rows, "the refused web launch wrote a run row"
    finally:
        HoldProvider.release.set()
    _wait_run_terminal(rid)
    print(
        "  #213 fix 1: the WEB run route answers the themed 409 page naming the running "
        "run and how to cancel it, and writes no row OK"
    )


# ============================================================================
# 6e. GitHub #213 fix round 1 (MEDIUM) — the SYNCHRONOUS arm is ATOMIC. The first
#     build of the guard only CHECKED under _RUN_CREATE_LOCK in run_workflow and
#     inserted its row later, outside the lock; the review's threaded probe got
#     real overlaps (12/20 vs start_run, both-run_workflow pairs 5/5 both ran).
#     The row is now created IN the locked critical section (the batch-member
#     pattern). 20 pairs of barrier-released arms — orchestrator.run_workflow vs
#     orchestrator.start_run, different workflows, ONE document + locale, the two
#     workflows swapping arms every other pair: the winner parks inside
#     HoldProvider so its row stays genuinely RUNNING while the other arm checks.
#     Exactly one of each pair starts; the other draws RunConflictError; ZERO
#     overlaps across all 20 pairs.
# ============================================================================
def test_cross_workflow_launch_atomic_across_arms():
    HoldProvider.reset()
    _pid, did = _mk_project_doc(locales=("es-ES",))
    wfA = _mk_wf(provider="holdmock")
    wfB = _mk_wf(provider="holdmock")
    overlaps = 0
    errors = []
    started_ids = []
    for n in range(20):
        bar = threading.Barrier(2)
        out: dict = {}

        def _arm(arm, wf_id, bar=bar, out=out):  # bind this pair's barrier + outcome dict
            try:
                bar.wait(timeout=30)
                with Session(engine) as s:
                    if arm == "sync":
                        summ = orchestrator.run_workflow(s, wf_id, did, "es-ES")
                        out[arm] = ("start", summ.get("run_id"))
                    else:
                        out[arm] = ("start", orchestrator.start_run(s, wf_id, did, "es-ES"))
            except orchestrator.RunConflictError:
                out[arm] = ("conflict", None)
            except Exception as exc:  # reported red below, never swallowed
                out[arm] = ("error", f"{type(exc).__name__}: {exc}")

        sync_wf, bg_wf = (wfB, wfA) if n % 2 == 0 else (wfA, wfB)
        t_sync = threading.Thread(target=_arm, args=("sync", sync_wf), daemon=True)
        t_bg = threading.Thread(target=_arm, args=("bg", bg_wf), daemon=True)
        t_sync.start()
        t_bg.start()
        time.sleep(1.0)  # both arms have reached the guard; the winner parks in HoldProvider
        HoldProvider.release.set()  # let the winner run to completion
        t_sync.join(timeout=60)
        t_bg.join(timeout=60)
        kinds = sorted(v[0] for v in out.values())
        if "error" in kinds:
            errors.append(f"pair {n}: {out!r}")
            break
        if kinds == ["start", "start"]:
            overlaps += 1
            continue
        if kinds != ["conflict", "start"]:
            errors.append(f"pair {n}: unexpected outcomes {out!r}")
            break
        started_ids.append(next(v[1] for v in out.values() if v[0] == "start"))
        for rid in started_ids[-1:]:
            assert _wait_run_terminal(rid) is not None, f"pair {n}: the started run never ended"
        HoldProvider.reset()  # the next pair parks afresh
    HoldProvider.release.set()
    assert not errors, errors
    assert overlaps == 0, (
        f"{overlaps}/20 pairs let TWO workflows run on one document + locale "
        "-- the check-and-create window is open again"
    )
    assert len(started_ids) == 20, f"only {len(started_ids)}/20 pairs resolved cleanly"
    with Session(engine) as s:
        rows = s.exec(
            select(WorkflowRun).where(
                WorkflowRun.document_id == did, WorkflowRun.target_locale == "es-ES"
            )
        ).all()
    assert len(rows) == 20, (
        "exactly one run row per pair (20) is expected on this document + locale, "
        f"got {len(rows)}: {[x.id for x in rows]}",
    )
    print(
        "  #213 fix 1: 20 concurrent run_workflow-vs-start_run pairs on one document + "
        "locale: exactly one start each, 20 RunConflictError refusals, 0 overlaps OK"
    )


# ============================================================================
# 7. org/locale gating unchanged
# ============================================================================
def test_gating_unchanged():
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="mock")
    client, head = _client_and_head()
    # a missing workflow is 404
    assert (
        client.post(
            "/api/workflows/999999/run", headers=head, json={"doc_id": did, "locale": "es-ES"}
        ).status_code
        == 404
    )
    # a missing document is 404
    assert (
        client.post(
            f"/api/workflows/{wf}/run", headers=head, json={"doc_id": 999999, "locale": "es-ES"}
        ).status_code
        == 404
    )
    # a locale not on the document is a 400 (never launches)
    r = client.post(
        f"/api/workflows/{wf}/run", headers=head, json={"doc_id": did, "locale": "de-DE"}
    )
    assert r.status_code == 400 and r.json()["ok"] is False, r.text
    print("  gating: missing workflow/doc -> 404, a locale not on the doc -> 400 OK")


# ============================================================================
# 8. remote CLI: launch-and-poll prints the SAME human line as the in-process arm
# ============================================================================
from contextlib import contextmanager


@contextmanager
def _fast_remote_poll(interval=0.05, ceiling=20.0):
    """Shrink BOTH remote-poll constants for the launch-and-poll tests (review F4).

    Patching only `_POLL_INTERVAL_S` (as these tests originally did) left `_POLL_CEILING_S`
    at its 1800s (30-min) default, so a BROKEN terminal condition -- a poll helper that
    never recognises the run as terminal -- would spin for the full half hour before
    returning timed_out, i.e. the test would appear to HANG rather than fail. Overriding the
    ceiling too caps that failure mode at `ceiling` seconds. Restores both on exit."""
    from app.cli import _remote_impl

    old_i, old_c = _remote_impl._POLL_INTERVAL_S, _remote_impl._POLL_CEILING_S
    set_pieces("app.cli._remote_impl", "_POLL_INTERVAL_S", interval)
    set_pieces("app.cli._remote_impl", "_POLL_CEILING_S", ceiling)
    try:
        yield
    finally:
        set_pieces("app.cli._remote_impl", "_POLL_INTERVAL_S", old_i)
        set_pieces("app.cli._remote_impl", "_POLL_CEILING_S", old_c)


def _remote_runner(key, as_json=True):
    """A CLI --remote runner whose httpx transport forwards to an in-process TestClient
    (no live network), mirroring verify_api_workflow_authoring."""
    from app.cli import _remote

    tc = TestClient(app)

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = tc.request(request.method, path, headers=dict(request.headers), content=request.content)
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    _remote._TRANSPORT = httpx.MockTransport(handler)

    def run(argv):
        buf = io.StringIO()
        base = ["--remote", "http://127.0.0.1", "--token", key]
        if as_json:
            base = ["--json"] + base  # noqa: RUF005
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc = cli_main(base + argv)
        out = buf.getvalue()
        if as_json:
            return rc, (_json.loads(out) if out.strip() else {})
        return rc, out

    return run


def test_remote_cli_completed():
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="mock")
    key = _api_key(scopes="api docs")
    # speed up the poll so a completed run is picked up without a 2s wait
    with _fast_remote_poll():
        run = _remote_runner(key, as_json=True)
        rc, o = run(["workflow", "run", str(did), str(wf), "es-ES"])
        assert rc == 0, (rc, o)
        assert o.get("status") == "completed" and o.get("steps", 0) >= 1, o
        # human line (no --json)
        run_h = _remote_runner(key, as_json=False)
        rc2, out = run_h(["workflow", "run", str(did), str(wf), "fr-FR"])
        assert rc2 == 0, (rc2, out)
        assert "completed:" in out and "step(s)" in out, out
    print(
        "  remote: `workflow run` launches then polls to completion and prints "
        "'Run #N completed: K step(s).' (exit 0) OK"
    )


def test_remote_cli_paused():
    _pid, did = _mk_project_doc()
    wf = _mk_wf(provider="mock", human_review=True)
    key = _api_key(scopes="api docs")
    with _fast_remote_poll():
        run = _remote_runner(key, as_json=False)
        rc, out = run(["workflow", "run", str(did), str(wf), "es-ES"])
        assert rc == 0, (rc, out)
        assert "PAUSED at human-review step" in out, out
    print("  remote: a run that pauses is polled to PAUSED and prints the resume line OK")


def test_remote_cli_batch_rollup():
    _pid, did = _mk_project_doc(("es-ES", "fr-FR"))
    wf = _mk_wf(provider="mock")
    key = _api_key(scopes="api docs")
    with _fast_remote_poll():
        run = _remote_runner(key, as_json=False)
        rc, out = run(["workflow", "run", str(did), str(wf), "--all-locales"])
        assert rc == 0, (rc, out)
        assert "Batch #" in out and "2 of 2 locale(s) completed" in out, out
    print(
        "  remote: `workflow run --all-locales` launches the batch, polls to terminal, "
        "and prints the rollup OK"
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failures += 1
                import traceback

                print(f"  FAIL {name}: {exc}")
                traceback.print_exc()
    if failures:
        print(f"verify_api_workflow_run_async FAILED ({failures})")
        raise SystemExit(1)
    print("API WORKFLOW RUN ASYNC OK")
