"""AUDIT-core-qa-part1 Q2 FOLLOW-UP — the three /api routes adjacent to the Q2 pair must
NOT execute their blocking orchestrator/batch work inline on the FastAPI event loop:

  POST /api/batches/{id}/rerun          (was rerun_batch(synchronous=True) on the loop)
  POST /api/batches/{id}/retry-failed   (was rerun_batch(synchronous=True) on the loop)
  POST /api/runs/{run_id}/resume        (was orchestrator.resume_workflow on the loop)

Same invariant as verify_api_workflow_run_async: the blocking execution NEVER runs on the
event loop. Each route now LAUNCHES async by default (returns a running ack/shape at once)
and offers opt-in `wait=true` that runs the SAME work OFF the loop in a threadpool worker
and returns the finished/finalized shape.

Because a TestClient drives the app WITHOUT a real event loop, loop-starvation is not
directly observable; instead we prove the ROUTE HANDLER never runs the work inline:

  rerun / retry-failed async  the default route calls batch_runs.rerun_batch with
                              synchronous=False (spy) and returns the RUNNING matrix; the
                              wait=true path hands _rerun_batch_blocking to run_in_threadpool
                              (off-loop) and returns the re-finalized matrix.
  resume async                the default route NEVER calls orchestrator.resume_workflow
                              (tripwire) — it launches via resume_run_async and returns
                              {kind:"run", run_id, status:"running"}; wait=true offloads only
                              the read-only DRAIN (_drain_run_to_terminal) to run_in_threadpool
                              and returns the finished-run shape (text/steps).
  resume validation           a segment-count mismatch is a clean 400 BEFORE any launch on
                              BOTH the async and the wait path; the run stays PAUSED.
  resume no double-capture    the async launch passes capture=False to resume_run_async, so
                              the resume post-edit records are written exactly once.
  resume double-submit        a second resume of an already-running run is a clean 400 with no
                              second capture and exactly one post-human execution (HIGH-1).
  remote CLI launch+poll      `workflow batch rerun` / `retry-failed` / `resume` over
                              --remote LAUNCH then POLL to a terminal state and print the
                              SAME human line as the in-process arm; BOTH poll constants are
                              shrunk so a broken terminal condition fails fast (review F4).

Final line: "API BATCH+RESUME ASYNC OK".
"""

import io
import json as _json
import os
import sys
import threading
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._api_cli_patch import set_api_cli
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_batchresumeasync_")

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


# --- a provider that FAILS while `fail` is set (to produce a FAILED batch member the
#     retry-failed path can then re-run to green once `fail` is cleared). ---------------
class FlakyProvider(Provider):
    name = "flakymock"
    fail = True

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if FlakyProvider.fail:
            raise RuntimeError("flaky boom (test)")
        return "ok-" + ((payload or {}).get("text", "") or "x")


register_provider("flakymock", FlakyProvider())


# --- a provider that BLOCKS until released (holds a resumed run in RUNNING so a second
#     resume of the SAME run hits the double-submit guard). -----------------------------
class HoldProvider(Provider):
    name = "holdmock2"
    release = threading.Event()

    @classmethod
    def reset(cls):
        cls.release = threading.Event()

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        HoldProvider.release.wait(timeout=15)
        return "ok-" + ((payload or {}).get("text", "") or "x")


register_provider("holdmock2", HoldProvider())


# --- fixtures -----------------------------------------------------------------------
def _mk_project_doc(locales=("es-ES", "fr-FR"), source="en-US"):
    with Session(engine) as s:
        p = Project(name=_uniq("BRProj"), source_locale=source)
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


def _mk_wf(provider="mock", human_review=False, post_human_provider=None):
    with Session(engine) as s:
        w = W.create_workflow(s, _uniq("BRWf"))
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
        if post_human_provider:
            # a step that runs ONLY after the human step is resumed past — used to hold a
            # resumed run in RUNNING while a second resume is attempted (double-submit).
            W.add_step(
                s,
                w.id,
                name="Finalizer",
                provider=post_human_provider,
                model="m1",
                prompt_text="Refine {text}",
                chunk_policy="none",
            )
        return w.id


def _api_key(email=None, scopes="api docs"):
    email = email or _uniq("batchresume") + "@x.io"
    admin = security.create_user(email, _uniq("u"), "admin", "fixture-pass-1234!!")
    _row, raw = apikeys.create_key(admin.id, _uniq("key"), scopes=scopes)
    return raw


def _client_and_head():
    return TestClient(app), {"X-Loc-Token": _api_key()}


def _poll_batch_terminal(client, head, bid, timeout=20.0):
    deadline = time.time() + timeout
    g = None
    while time.time() < deadline:
        g = client.get(f"/api/batches/{bid}", headers=head).json()
        if g["status"] != "running":
            return g
        time.sleep(0.03)
    return g


def _poll_run_terminal(client, head, rid, timeout=20.0):
    deadline = time.time() + timeout
    b = None
    while time.time() < deadline:
        b = client.get(f"/api/runs/{rid}?visits=1", headers=head).json()
        st = str(b.get("status") or "").lower()
        if st in ("completed", "failed", "cancelled", "rejected", "paused"):
            return b
        time.sleep(0.03)
    return b


def _make_completed_batch(client, head, wf, did):
    """Run a batch to a finished state via wait=true and return its id."""
    r = client.post(
        f"/api/workflows/{wf}/run-batch", headers=head, json={"doc_id": did, "wait": True}
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _paused_run(client, head, wf, did, locale):
    """Run a human-review workflow to its PAUSE via wait=true; return the run_id."""
    r = client.post(
        f"/api/workflows/{wf}/run",
        headers=head,
        json={"doc_id": did, "locale": locale, "wait": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paused", body
    return body["run_id"]


# ============================================================================
# 1. rerun: default = async daemon (synchronous=False) + poll; wait=true = off-loop
# ============================================================================
def test_rerun_async_default_and_wait():
    _pid, did = _mk_project_doc(("es-ES", "fr-FR"))
    wf = _mk_wf(provider="mock")
    client, head = _client_and_head()
    bid = _make_completed_batch(client, head, wf, did)

    calls = []
    real_rr = batch_runs.rerun_batch

    def spy_rerun(*a, **k):
        calls.append(k.get("synchronous"))
        return real_rr(*a, **k)

    set_pieces("app.services.batch_runs", "rerun_batch", spy_rerun)
    try:
        r = client.post(f"/api/batches/{bid}/rerun", headers=head, json={"locale": "es-ES"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert calls and calls[-1] is False, (
            "default rerun must re-dispatch async (synchronous=False)",
            calls,
        )
        # the async launch returns the matrix immediately, status RUNNING (finished_at cleared)
        assert body["status"] == "running", body
        g = _poll_batch_terminal(client, head, bid)
        assert g["status"] == "completed", g

        # wait=true re-runs the SAME dispatch synchronously OFF the loop and finalizes.
        calls.clear()
        r2 = client.post(
            f"/api/batches/{bid}/rerun", headers=head, json={"locale": "es-ES", "wait": True}
        )
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert calls and calls[-1] is True, ("wait=true rerun must be synchronous", calls)
        assert b2["status"] == "completed", b2
    finally:
        set_pieces("app.services.batch_runs", "rerun_batch", real_rr)
    print(
        "  rerun: default re-dispatches async (synchronous=False) then polls to completed; "
        "wait=true finalizes off-loop (synchronous=True) OK"
    )


# ============================================================================
# 1b. rerun off-loop is OBSERVABLE: wait=true hands _rerun_batch_blocking to
#     run_in_threadpool; the default async launch never offloads (review F4).
# ============================================================================
def test_rerun_offloop_observable():
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="mock")
    client, head = _client_and_head()
    bid = _make_completed_batch(client, head, wf, did)

    offloaded = []
    real_ritp = api_cli.run_in_threadpool

    def spy_ritp(fn, *a, **k):
        offloaded.append(getattr(fn, "__name__", repr(fn)))
        return real_ritp(fn, *a, **k)

    set_api_cli("run_in_threadpool", spy_ritp)
    try:
        client.post(f"/api/batches/{bid}/rerun", headers=head, json={"locale": "es-ES"})
        assert offloaded == [], (
            "the default async rerun offloaded to run_in_threadpool",
            offloaded,
        )
        _poll_batch_terminal(client, head, bid)
        client.post(
            f"/api/batches/{bid}/rerun", headers=head, json={"locale": "es-ES", "wait": True}
        )
        assert "_rerun_batch_blocking" in offloaded, (
            "wait=true rerun did not run _rerun_batch_blocking via run_in_threadpool -- the "
            "blocking re-dispatch may be executing ON the event loop",
            offloaded,
        )
    finally:
        set_api_cli("run_in_threadpool", real_ritp)
    print(
        "  rerun off-loop: wait=true hands _rerun_batch_blocking to run_in_threadpool; the "
        "default launch never offloads (off-loop invariant made observable) OK"
    )


# ============================================================================
# 2. retry-failed: a FAILED member re-runs to green; default async, wait=true off-loop
# ============================================================================
def test_retry_failed_async_and_wait():
    FlakyProvider.fail = True
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="flakymock")
    client, head = _client_and_head()
    # a batch whose only member FAILS (flaky provider raises)
    bid = _make_completed_batch(client, head, wf, did)
    g0 = client.get(f"/api/batches/{bid}", headers=head).json()
    assert g0["counts"]["failed"] == 1, g0

    # nothing-to-clear case is a no-op only when nothing failed; here es-ES IS failed.
    FlakyProvider.fail = False  # the retry will now succeed

    calls = []
    real_rr = batch_runs.rerun_batch

    def spy_rerun(*a, **k):
        calls.append(k.get("synchronous"))
        return real_rr(*a, **k)

    set_pieces("app.services.batch_runs", "rerun_batch", spy_rerun)
    try:
        r = client.post(f"/api/batches/{bid}/retry-failed", headers=head, json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["retried"] == ["es-ES"], body
        assert calls and calls[-1] is False, (
            "default retry-failed must be async (synchronous=False)",
            calls,
        )
        assert body["status"] == "running", body
        g = _poll_batch_terminal(client, head, bid)
        assert g["status"] == "completed" and g["counts"]["completed"] == 1, g
    finally:
        set_pieces("app.services.batch_runs", "rerun_batch", real_rr)

    # nothing failed now -> a clean no-op that returns retried=[] at once (no launch).
    r2 = client.post(f"/api/batches/{bid}/retry-failed", headers=head, json={})
    assert r2.status_code == 200 and r2.json()["retried"] == [], r2.text
    print(
        "  retry-failed: a FAILED member re-runs async to completed; a batch with nothing "
        "failed is a clean retried=[] no-op OK"
    )


def test_retry_failed_wait_offloop():
    FlakyProvider.fail = True
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="flakymock")
    client, head = _client_and_head()
    bid = _make_completed_batch(client, head, wf, did)
    assert client.get(f"/api/batches/{bid}", headers=head).json()["counts"]["failed"] == 1
    FlakyProvider.fail = False

    offloaded = []
    real_ritp = api_cli.run_in_threadpool

    def spy_ritp(fn, *a, **k):
        offloaded.append(getattr(fn, "__name__", repr(fn)))
        return real_ritp(fn, *a, **k)

    set_api_cli("run_in_threadpool", spy_ritp)
    try:
        r = client.post(f"/api/batches/{bid}/retry-failed", headers=head, json={"wait": True})
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["retried"] == ["es-ES"] and b["status"] == "completed", b
        assert "_rerun_batch_blocking" in offloaded, (
            "wait=true retry-failed did not offload _rerun_batch_blocking",
            offloaded,
        )
    finally:
        set_api_cli("run_in_threadpool", real_ritp)
    print(
        "  retry-failed: wait=true re-runs the failed set off-loop (via "
        "_rerun_batch_blocking) and returns the re-finalized (completed) matrix OK"
    )


# ============================================================================
# 3. resume: default async NEVER runs resume_workflow inline; returns a running ack
# ============================================================================
def test_resume_async_default_never_inline():
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="mock", human_review=True)
    client, head = _client_and_head()
    rid = _paused_run(client, head, wf, did, "es-ES")

    tripwire = []
    real_rw = orchestrator.resume_workflow

    def tripwire_resume_workflow(*a, **k):
        tripwire.append((a, k))
        raise AssertionError(
            "orchestrator.resume_workflow was called on the /resume route "
            "-- the workflow is executing INLINE on the event loop"
        )

    set_pieces("app.services.orchestrator", "resume_workflow", tripwire_resume_workflow)
    try:
        r = client.post(
            f"/api/runs/{rid}/resume", headers=head, json={"locale": "es-ES", "edited_text": ""}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["kind"] == "run", body
        assert body["run_id"] == rid and body["status"] == "running", body
        assert "text" not in body, ("the launch ack must not carry finished text", body)
        assert not tripwire, "resume_workflow was invoked by the async /resume route"
    finally:
        set_pieces("app.services.orchestrator", "resume_workflow", real_rw)
    # the background resume still drives the run to completion
    b = _poll_run_terminal(client, head, rid)
    assert str(b.get("status")).lower() == "completed", b
    print(
        "  resume: the default route launches via resume_run_async, returns "
        "{kind,run_id,status:running} and NEVER calls resume_workflow (tripwire) OK"
    )


# ============================================================================
# 3b. resume wait=true offloads ONLY the read-only DRAIN (_drain_run_to_terminal)
#     to run_in_threadpool and returns the finished shape.
# ============================================================================
def test_resume_wait_offloop_finished_shape():
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="mock", human_review=True)
    client, head = _client_and_head()
    rid = _paused_run(client, head, wf, did, "es-ES")

    offloaded = []
    real_ritp = api_cli.run_in_threadpool

    def spy_ritp(fn, *a, **k):
        offloaded.append(getattr(fn, "__name__", repr(fn)))
        return real_ritp(fn, *a, **k)

    set_api_cli("run_in_threadpool", spy_ritp)
    try:
        r = client.post(
            f"/api/runs/{rid}/resume",
            headers=head,
            json={"locale": "es-ES", "edited_text": "", "wait": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["status"] == "completed", body
        assert "text" in body, body
        # only the read-only DRAIN is offloaded; the launch happens on the request thread.
        assert "_drain_run_to_terminal" in offloaded, (
            "wait=true resume did not run _drain_run_to_terminal via run_in_threadpool -- the "
            "minutes-long poll may be blocking ON the event loop",
            offloaded,
        )
    finally:
        set_api_cli("run_in_threadpool", real_ritp)
    print(
        "  resume: wait=true drains off-loop via _drain_run_to_terminal and returns the "
        "finished-run shape (text/steps) OK"
    )


# ============================================================================
# 3c. resume validation: a segment-count mismatch is a clean 400 BEFORE any launch,
#     on BOTH the async and the wait path; the run stays PAUSED.
# ============================================================================
def test_resume_mismatch_400_before_launch():
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="mock", human_review=True)
    client, head = _client_and_head()

    for wait in (False, True):
        rid = _paused_run(client, head, wf, did, "es-ES")
        # a 2-segment payload against a 1-segment paused blob is a mismatch
        j = {"locale": "es-ES", "edited_segments": ["a", "b"]}
        if wait:
            j["wait"] = True
        r = client.post(f"/api/runs/{rid}/resume", headers=head, json=j)
        assert r.status_code == 400, (wait, r.text)
        assert r.json()["ok"] is False and r.json()["status"] == "paused", (wait, r.text)
        # the run is still PAUSED + re-resumable (no state change, no dead run)
        with Session(engine) as s:
            assert s.get(WorkflowRun, rid).status == RunStatus.PAUSED, (wait, rid)
    print(
        "  resume: a segment-count mismatch is a clean 400 (run stays PAUSED) on BOTH the "
        "async and the wait path -- validated before any launch OK"
    )


# ============================================================================
# 3d. resume writes its correction post-edit records EXACTLY ONCE (capture=False on the
#     async launch; prepare_resume_seed already captured on the request thread).
# ============================================================================
def _post_edits(doc_id):
    """The correction records a resume leaves (SegmentPostEdit, the core's post-edit capture;
    the LLMemory MemoryEvent capture is a paid add-on and is not in the open core)."""
    from app.models import Segment, SegmentPostEdit

    with Session(engine) as s:
        seg_ids = [x.id for x in s.exec(select(Segment).where(Segment.document_id == doc_id))]
        return s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id.in_(seg_ids))).all()


def test_resume_no_double_capture():
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="mock", human_review=True)
    client, head = _client_and_head()
    rid = _paused_run(client, head, wf, did, "es-ES")

    def _events():
        return _post_edits(did)

    before = len(_events())
    # an edit that DIFFERS from the produced MT -> exactly one correction event per segment
    r = client.post(
        f"/api/runs/{rid}/resume",
        headers=head,
        json={"locale": "es-ES", "edited_segments": ["EDITED text."]},
    )
    assert r.status_code == 200, r.text
    _poll_run_terminal(client, head, rid)
    after = len(_events())
    # one paused blob had one segment -> one correction event; NEVER two (double capture)
    assert after - before == 1, (
        "resume captured the correction event %d times (expected 1) "  # noqa: UP031
        "-- capture=False must suppress the resume_run_async capture" % (after - before)
    )
    print(
        "  resume: the correction post-edit record is written exactly once (no double capture "
        "across prepare_resume_seed + resume_run_async) OK"
    )


# ============================================================================
# 3e. resume double-submit guard (adversarial review HIGH-1): a second resume of a run
#     that is ALREADY resumed+running is refused (400) with NO second capture and NO
#     second background execution. resume_run_async takes _RUN_CREATE_LOCK for the flip,
#     and the /api route launches on the request thread (before any await), so the
#     PAUSED->RUNNING transition is atomic against a rival resume.
# ============================================================================
def test_resume_double_submit_guard():
    from app.models import StepRun

    HoldProvider.reset()
    _pid, did = _mk_project_doc(("es-ES",))
    # Translator(mock) -> Human -> Finalizer(holdmock2): the resumed run blocks in Finalizer
    wf = _mk_wf(provider="mock", human_review=True, post_human_provider="holdmock2")
    client, head = _client_and_head()
    rid = _paused_run(client, head, wf, did, "es-ES")

    def _captures():
        return len(_post_edits(did))

    before = _captures()
    try:
        # resume #1 (async default) with a real edit -> advances past Human, Finalizer BLOCKS
        r1 = client.post(
            f"/api/runs/{rid}/resume",
            headers=head,
            json={"locale": "es-ES", "edited_segments": ["EDITED text."]},
        )
        assert r1.status_code == 200 and r1.json()["status"] == "running", r1.text
        # wait until the run is actually RUNNING (past the human step, parked in Finalizer)
        deadline = time.time() + 10
        while time.time() < deadline:
            with Session(engine) as s:
                if s.get(WorkflowRun, rid).status == RunStatus.RUNNING:
                    break
            time.sleep(0.02)
        # resume #2 over the SAME run -> it is no longer PAUSED -> clean 400, NO second
        # capture, NO second execution (the guard, not resume_workflow-advances-again).
        r2 = client.post(
            f"/api/runs/{rid}/resume",
            headers=head,
            json={"locale": "es-ES", "edited_segments": ["SECOND edit."]},
        )
        assert r2.status_code == 400, r2.text
        assert "not paused" in (r2.json().get("error") or "").lower(), r2.text
        assert _captures() == before + 1, (
            "the refused second resume double-captured (expected exactly one correction event)",
            _captures(),
            before,
        )
    finally:
        HoldProvider.release.set()
    _poll_run_terminal(client, head, rid)
    # exactly ONE Finalizer StepRun -> the run executed the post-human step exactly once
    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        fin_step = [  # noqa: RUF015
            st for st in orchestrator.get_steps(s, run.workflow_id) if st.name == "Finalizer"
        ][0]
        fin_runs = s.exec(
            select(StepRun).where(StepRun.workflow_run_id == rid, StepRun.step_id == fin_step.id)
        ).all()
    assert len(fin_runs) == 1, (
        "the double-submit spawned a second execution of the post-human step",
        [x.id for x in fin_runs],
    )
    assert str(run.status).lower() == "completed", run.status
    print(
        "  resume double-submit: a second resume of an already-running run is a clean 400 "
        "with no second capture and exactly one post-human execution (HIGH-1 guard) OK"
    )


# ============================================================================
# 3f. resume _RUN_CREATE_LOCK genuine-concurrency defense (adversarial review F1): the
#     bare `status != PAUSED` guard alone is NOT enough — two resumes that both observe
#     PAUSED at the SAME instant (before either commits the RUNNING flip) would BOTH
#     launch (double provider spend, two threads racing one Segment set). The lock closes
#     that window. test_resume_double_submit_guard above does NOT defend it: it waits for
#     the first resume to reach RUNNING before firing the second, so the two never overlap
#     inside the check-then-flip section. THIS test forces the overlap with a barrier
#     patched into _mark_execution_start (the last step inside the locked section, just
#     before the commit): two threads, each its OWN Session, driving resume_run_async on
#     ONE paused run simultaneously.
#
#       WITH the lock  -> the second thread cannot enter the critical section while the
#                         first holds the lock; by the time it acquires, the row is
#                         RUNNING and it no-ops. EXACTLY ONE launch. (The barrier is never
#                         satisfied — only one thread is ever inside — so the first thread
#                         waits out the short timeout, then commits and releases.)
#       WITHOUT it (`with _RUN_CREATE_LOCK:` -> `if True:`) -> both threads read PAUSED
#                         (neither has committed), both advance + flip, BOTH reach the
#                         barrier (which then releases them), BOTH call _run_in_background.
#                         TWO launches -> this test FAILS. That mutation is what this
#                         defends; the plain guard test stays green under it.
# ============================================================================
def test_resume_lock_serializes_concurrent_flip():
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="mock", human_review=True)
    client, head = _client_and_head()
    rid = _paused_run(client, head, wf, did, "es-ES")

    # count launches WITHOUT actually running the background workflow: this probe is about
    # the check-then-flip DECISION only, and two real background runs racing one segment set
    # is exactly the corruption we are proving cannot be launched.
    launches = []
    real_bg = orchestrator._run_in_background

    def spy_bg(*a, **k):
        launches.append(threading.current_thread().name)
        return None  # do not spawn a real execution thread

    # Barrier patched into the LAST statement inside the locked section (just before the
    # commit): a thread that gets here signals it is INSIDE the critical section, then waits
    # for a SECOND thread to also get inside. With the lock, no second thread can — so it
    # waits out `_INSIDE_TIMEOUT` and proceeds alone. Without the lock, the second thread
    # arrives, `gate` is set, and both proceed together.
    real_mark = orchestrator._mark_execution_start
    inside = []
    gate = threading.Event()
    _INSIDE_TIMEOUT = 1.5

    def patched_mark(run):
        inside.append(threading.current_thread().name)
        if len(inside) >= 2:  # a second thread reached the critical section too
            gate.set()
        gate.wait(timeout=_INSIDE_TIMEOUT)
        return real_mark(run)

    results = {}

    def fire(tag):
        try:
            with Session(engine) as s:  # each thread its OWN Session (not thread-safe)
                results[tag] = orchestrator.resume_run_async(s, rid, "es-ES", "", capture=False)
        except Exception as exc:
            results[tag] = ("ERR", repr(exc))

    set_pieces("app.services.orchestrator", "_run_in_background", spy_bg)
    set_pieces("app.services.orchestrator", "_mark_execution_start", patched_mark)
    try:
        t1 = threading.Thread(target=fire, args=("a",), name="resA", daemon=True)
        t2 = threading.Thread(target=fire, args=("b",), name="resB", daemon=True)
        t1.start()
        t2.start()
        t1.join(15)
        t2.join(15)
    finally:
        set_pieces("app.services.orchestrator", "_run_in_background", real_bg)
        set_pieces("app.services.orchestrator", "_mark_execution_start", real_mark)

    assert set(results) == {"a", "b"}, ("a resume thread did not finish", results)
    launched = [tag for tag, r in results.items() if r is True]
    noop = [tag for tag, r in results.items() if r is False]
    # EXACTLY ONE of the two concurrent resumes may launch; the other must no-op.
    assert len(launched) == 1 and len(noop) == 1, (
        "two simultaneous resumes of one PAUSED run did not serialize to one launch -- the "
        "_RUN_CREATE_LOCK window is undefended (both observed PAUSED and both flipped)",
        results,
    )
    # ...and exactly ONE background execution was launched (the thing the lock guards).
    assert len(launches) == 1, (
        "resume_run_async spawned %d background executions for one run under a concurrent "  # noqa: UP031
        "double-resume (expected exactly 1)" % len(launches),
        launches,
    )
    # the run advanced past the human step EXACTLY once and is RUNNING (not double-advanced).
    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        assert run.status == RunStatus.RUNNING, run.status
    print(
        "  resume lock: two barrier-overlapped simultaneous resumes of one PAUSED run "
        "serialize to EXACTLY ONE launch (defends _RUN_CREATE_LOCK; the plain guard test "
        "cannot — reverting the lock to `if True:` fails this) OK"
    )


# ============================================================================
# 4. org-scope gating unchanged on all three routes (a foreign/missing target 404s;
#    a rerun with no locale is a 400; resume of a non-paused run is a 400).
# ============================================================================
def test_gating_unchanged():
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="mock", human_review=True)
    client, head = _client_and_head()
    # a missing batch is 404 on both batch routes
    assert (
        client.post("/api/batches/999999/rerun", headers=head, json={"locale": "es-ES"}).status_code
        == 404
    )
    assert client.post("/api/batches/999999/retry-failed", headers=head, json={}).status_code == 404
    # a missing run is 404 on resume
    assert (
        client.post("/api/runs/999999/resume", headers=head, json={"locale": "es-ES"}).status_code
        == 404
    )
    # a rerun with no locale is a 400 (never launches)
    bid = _make_completed_batch(client, head, _mk_wf(provider="mock"), did)
    r = client.post(f"/api/batches/{bid}/rerun", headers=head, json={})
    assert r.status_code == 400 and r.json()["ok"] is False, r.text
    # resume of a non-paused (completed) run is a 400 -- drive a real run to completion first
    rid = _paused_run(client, head, wf, did, "es-ES")
    _ok = client.post(
        f"/api/runs/{rid}/resume",
        headers=head,
        json={"locale": "es-ES", "edited_text": "", "wait": True},
    )
    assert _ok.status_code == 200 and _ok.json()["status"] == "completed", _ok.text
    r3 = client.post(f"/api/runs/{rid}/resume", headers=head, json={"locale": "es-ES"})
    assert r3.status_code == 400 and r3.json()["ok"] is False, r3.text
    assert "not paused" in (r3.json().get("error") or "").lower(), r3.text
    print(
        "  gating: missing batch/run -> 404, a rerun with no locale -> 400, a resume of a "
        "non-paused run -> 400 (scope + semantics unchanged) OK"
    )


# ============================================================================
# 5. remote CLI: launch-and-poll for rerun / retry-failed / resume (review F4:
#    BOTH poll constants shrunk so a broken terminal condition fails fast).
# ============================================================================
@contextmanager
def _fast_remote_poll(interval=0.05, ceiling=20.0):
    """Shrink BOTH remote-poll constants (review F4): patching only the interval would leave
    the 30-min ceiling in place, so a BROKEN terminal condition would spin for the full half
    hour (the test would appear to HANG) rather than fail fast. Restores both on exit."""
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


def test_remote_cli_rerun_launch_poll():
    _pid, did = _mk_project_doc(("es-ES", "fr-FR"))
    wf = _mk_wf(provider="mock")
    key = _api_key(scopes="api docs")
    head = {"X-Loc-Token": key}
    client = TestClient(app)
    bid = _make_completed_batch(client, head, wf, did)
    with _fast_remote_poll():
        run = _remote_runner(key, as_json=False)
        rc, out = run(["workflow", "batch", "rerun", str(bid), "--locale", "es-ES"])
        assert rc == 0, (rc, out)
        assert "launched; polling" in out, out
    print("  remote: `workflow batch rerun` launches then polls to a terminal matrix (exit 0) OK")


def test_remote_cli_retry_failed_launch_poll():
    FlakyProvider.fail = True
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="flakymock")
    key = _api_key(scopes="api docs")
    head = {"X-Loc-Token": key}
    client = TestClient(app)
    bid = _make_completed_batch(client, head, wf, did)
    assert client.get(f"/api/batches/{bid}", headers=head).json()["counts"]["failed"] == 1
    FlakyProvider.fail = False
    with _fast_remote_poll():
        run = _remote_runner(key, as_json=False)
        rc, out = run(["workflow", "batch", "retry-failed", str(bid)])
        assert rc == 0, (rc, out)
        assert "retry-failed launched" in out and "polling" in out, out
    print(
        "  remote: `workflow batch retry-failed` launches then polls to a terminal matrix "
        "(exit 0) OK"
    )


def test_remote_cli_resume_launch_poll():
    _pid, did = _mk_project_doc(("es-ES",))
    wf = _mk_wf(provider="mock", human_review=True)
    key = _api_key(scopes="api docs")
    head = {"X-Loc-Token": key}
    client = TestClient(app)
    rid = _paused_run(client, head, wf, did, "es-ES")
    with _fast_remote_poll():
        run = _remote_runner(key, as_json=False)
        rc, out = run(["workflow", "resume", str(rid), "--edited-text", "", "--locale", "es-ES"])
        assert rc == 0, (rc, out)
        assert "completed:" in out and "step(s)" in out, out
    print(
        "  remote: `workflow resume` launches then polls to completion and prints the "
        "'Run #N completed: K step(s).' line (exit 0) OK"
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
        print(f"verify_api_batch_resume_async FAILED ({failures})")
        raise SystemExit(1)
    print("API BATCH+RESUME ASYNC OK")
