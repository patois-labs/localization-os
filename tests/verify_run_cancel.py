"""Run-cancel + held-item hardening (AUDIT-fulltest-2026-08-08 BUG-A205 / A203 / INFO-1).

Proves the operator run-cancel feature end to end plus the two smaller held items, all
hermetic + offline (MockProvider, injected httpx.MockTransport for the one vendor leg).

  ITEM 1 -- RUN CANCEL (the state machine + every safety property):
    [A] cancel a PAUSED (human_review) run  -> CANCELLED + finished_at + note; the PAUSED
        StepRun is retired to CANCELLED (no stale 'paused' row); NOT resumable (the guarded
        resume seam no-ops, /api resume is 400); re-authoring is UNBLOCKED.
    [B] cancel a RUNNING run  -> CANCELLED (the transition works from RUNNING too).
    [C] cancel an already-terminal run  -> RunCancelError (clean 4xx, idempotent-ish, no
        crash), for a COMPLETED run AND a second cancel of a CANCELLED run.
    [D] cancel a run PAUSED at an EXTERNAL vendor step  -> the open leg is CLOSED
        (CANCELLED, retry_contract cleared) and a LATE vendor return is DROPPED loudly.
    [E] /api happy path + the terminal 409, and the cross-tenant 404 (org A can't cancel
        org B's run).
  ITEM 2 -- add-step is REFUSED while a run is in flight, and ALLOWED once it is cancelled.
  ITEM 3 -- POST /api/vendors/returns rejects an oversized body 413 (the webhook's cap).

ASCII-only; standalone one-process-per-suite script (throwaway LOC_TMS_DATA_DIR, keys
cleared -> MockProvider, deterministic + free).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_runcancel_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Organization,
    Project,
    Role,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TransMode,
    User,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
)
from app.services import (
    apikeys,
    orchestrator,
    security,
    settings,
    vendor_dispatch,
    vendor_return,
    vendors,
)
from app.services import workflows as wf

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _mock_transport():
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def _app():
    from app.main import app

    return app


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #
def _seed_project(org_id=None):
    """A customer + project + a 2-segment document. Returns (project_id, doc_id, cust_id)."""
    with Session(engine) as s:
        cust = Customer(name="C-%d" % _seq(), org_id=org_id)  # noqa: UP031
        s.add(cust)
        s.commit()
        s.refresh(cust)
        p = Project(
            name="P-%d" % _seq(),  # noqa: UP031
            source_locale="en-US",
            default_provider="mock",
            customer_id=cust.id,
            org_id=org_id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(("Hello world.", "The cat sat on the mat.")):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=src,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return p.id, d.id, cust.id


def _wf_human_review():
    """A workflow [translate -> human_review]."""
    with Session(engine) as s:
        w = Workflow(
            name="W-%d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale="en-US",
            target_locale="es-ES",  # noqa: RUF100, UP031
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(s, w.id, name="Review", step_type=StepType.HUMAN_REVIEW)
        return w.id


def _wf_translate_only():
    """A workflow with a single llm_agent step -> runs straight to completion (no pause),
    so it reaches the terminal write-back CAS."""
    with Session(engine) as s:
        w = Workflow(
            name="W-%d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale="en-US",
            target_locale="es-ES",  # noqa: RUF100, UP031
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        return w.id


def _run_to_pause(wid, doc_id, locale="es-ES"):
    with Session(engine) as s:
        events = list(orchestrator.run_workflow_stream(s, wid, doc_id, locale))
    assert any(e.get("type") == "paused" for e in events), (
        "workflow with a human_review step must pause",
        [e.get("type") for e in events],
    )
    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == wid)
            .order_by(WorkflowRun.id.desc())
        ).first()
        assert run.status == RunStatus.PAUSED, run.status
        return run.id


def _seed_external_parked_leg():
    """A run [translate -> external] driven to a PARKED vendor leg (DISPATCHED). Returns
    {run_id, leg_id, claim, doc_id}."""
    _p, doc_id, cust_id = _seed_project()
    with Session(engine) as s:
        v = Vendor(name="V-%d" % _seq(), endpoint_url="https://vendor.example.com/intake")  # noqa: UP031
        v.secret_enc = vendors._enc_secret({"signing_secret": "topsecret"})
        s.add(v)
        s.commit()
        s.refresh(v)
        vendors.link_customer(s, v.id, cust_id)
        w = Workflow(
            name="W-%d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale="en-US",
            target_locale="es-ES",  # noqa: RUF100, UP031
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(s, w.id, name="Vendor", step_type=StepType.EXTERNAL, vendor_id=v.id)
        wid = w.id
    vendor_dispatch.set_test_transport(_mock_transport())
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, wid, doc_id, "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wid)).first()
        leg = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).first()
        assert leg is not None and leg.status == VendorDispatchStatus.DISPATCHED
        return {"run_id": run.id, "leg_id": leg.id, "claim": leg.claim_token, "doc_id": doc_id}


# ============================================================================
# [A] cancel a PAUSED run: full semantics
# ============================================================================
def test_cancel_paused_run():
    _p, doc_id, _c = _seed_project()
    wid = _wf_human_review()
    run_id = _run_to_pause(wid, doc_id)
    with Session(engine) as s:
        out = orchestrator.cancel_run(s, run_id, actor="tester@x.com")
    assert out["ok"] and out["status"] == RunStatus.CANCELLED and out["run_id"] == run_id
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.CANCELLED, run.status
        assert run.finished_at is not None, "a cancelled run is finished"
        assert orchestrator.CANCEL_NOTE in (run.notes or ""), run.notes
        # The PAUSED StepRun was retired -> no stale 'paused' row survives.
        paused = s.exec(
            select(StepRun).where(
                StepRun.workflow_run_id == run_id, StepRun.status == RunStatus.PAUSED
            )
        ).all()
        assert not paused, "the cancelled run must carry no PAUSED StepRun"
        cancelled_sr = s.exec(
            select(StepRun).where(
                StepRun.workflow_run_id == run_id, StepRun.status == RunStatus.CANCELLED
            )
        ).all()
        assert cancelled_sr, "the human_review StepRun should read CANCELLED"

    # NOT resumable: the guarded resume seam no-ops and the run stays CANCELLED.
    with Session(engine) as s:
        orchestrator.resume_run_async(s, run_id, "es-ES", "", actor="tester@x.com")
        assert s.get(WorkflowRun, run_id).status == RunStatus.CANCELLED, (
            "resume_run_async must not revive a cancelled run"
        )
    print("  [A] cancel PAUSED -> CANCELLED + finished + note; StepRun retired; not resumable: OK")


# ============================================================================
# [B] cancel a RUNNING run
# ============================================================================
def test_cancel_running_run():
    _p, doc_id, _c = _seed_project()
    with Session(engine) as s:
        w = Workflow(
            name="W-%d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale="en-US",
            target_locale="es-ES",  # noqa: RUF100, UP031
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        run = WorkflowRun(
            workflow_id=w.id, document_id=doc_id, target_locale="es-ES", status=RunStatus.RUNNING
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
    with Session(engine) as s:
        out = orchestrator.cancel_run(s, run_id)
    assert out["status"] == RunStatus.CANCELLED
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.CANCELLED
    print("  [B] cancel RUNNING -> CANCELLED: OK")


# ============================================================================
# [C] cancel an already-terminal run -> clean refusal, no crash
# ============================================================================
def test_cancel_terminal_run_refused():
    _p, doc_id, _c = _seed_project()
    with Session(engine) as s:
        w = Workflow(name="W-%d" % _seq(), source_locale="en-US", target_locale="es-ES")  # noqa: UP031
        s.add(w)
        s.commit()
        s.refresh(w)
        run = WorkflowRun(
            workflow_id=w.id, document_id=doc_id, target_locale="es-ES", status=RunStatus.COMPLETED
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
    # A completed run cannot be cancelled.
    with Session(engine) as s:
        try:
            orchestrator.cancel_run(s, run_id)
            assert False, "cancelling a COMPLETED run must raise RunCancelError"
        except orchestrator.RunCancelError:
            pass
    # A missing run is a clean refusal too.
    with Session(engine) as s:
        try:
            orchestrator.cancel_run(s, 9_999_999)
            assert False, "cancelling a missing run must raise RunCancelError"
        except orchestrator.RunCancelError:
            pass
    # Idempotent-ish: a SECOND cancel of a cancelled run is refused, never a crash.
    _p2, doc2, _c2 = _seed_project()
    wid = _wf_human_review()
    rid = _run_to_pause(wid, doc2)
    with Session(engine) as s:
        orchestrator.cancel_run(s, rid)
    with Session(engine) as s:
        try:
            orchestrator.cancel_run(s, rid)
            assert False, "a second cancel must raise RunCancelError"
        except orchestrator.RunCancelError:
            pass
    print("  [C] cancel of completed / missing / already-cancelled run -> clean RunCancelError: OK")


# ============================================================================
# [D] cancel closes the open vendor leg + a late return is dropped
# ============================================================================
def test_cancel_closes_leg_and_drops_late_return():
    env = _seed_external_parked_leg()
    with Session(engine) as s:
        out = orchestrator.cancel_run(s, env["run_id"], actor="tester@x.com")
    assert out["legs_closed"] == 1, ("the open leg must be closed", out)
    with Session(engine) as s:
        leg = s.get(VendorDispatch, env["leg_id"])
        assert leg.status == VendorDispatchStatus.CANCELLED, leg.status
        assert leg.retry_contract is None and leg.next_attempt_at is None
        # A late vendor return for the closed leg of the cancelled run is DROPPED loudly.
        try:
            vendor_return.assert_leg_admitting(s, leg)
            assert False, "a late return for a cancelled leg must be dropped"
        except vendor_return.ReturnStateError:
            pass
    print("  [D] cancel closes the open vendor leg + drops a late return: OK")


# ============================================================================
# [F] resurrection guards: the run-finishing thread must never overwrite a CANCELLED
#     row with COMPLETED (LOW-1 CAS + the _abort_if_superseded CANCELLED branch, LOW-2).
# ============================================================================
def test_abort_if_superseded_catches_a_cancelled_row():
    """MUTATION-KILL for the _abort_if_superseded CANCELLED branch: a row another actor
    made CANCELLED must abort the finishing thread's write-back. Reverting the guard to
    check only FAILED makes this go RED (it would return False and let the write-back
    proceed)."""
    _p, doc_id, _c = _seed_project()
    with Session(engine) as s:
        w = Workflow(name="W-%d" % _seq(), source_locale="en-US", target_locale="es-ES")  # noqa: UP031
        s.add(w)
        s.commit()
        s.refresh(w)
        run = WorkflowRun(
            workflow_id=w.id, document_id=doc_id, target_locale="es-ES", status=RunStatus.RUNNING
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
    # Another actor cancels it (committed).
    with Session(engine) as s:
        r = s.get(WorkflowRun, run_id)
        r.status = RunStatus.CANCELLED
        s.add(r)
        s.commit()
    # The finishing thread's checkpoint reads COMMITTED state and must abort.
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert orchestrator._abort_if_superseded(s, run) is True, (
            "the CANCELLED branch of _abort_if_superseded must abort the write-back "
            "(reverting it to FAILED-only regresses this)"
        )
    print("  [F] _abort_if_superseded aborts a CANCELLED row (mutation-kill): OK")


def test_terminal_cas_prevents_resurrection_of_a_cancelled_run():
    """MUTATION-KILL for the LOW-1 terminal CAS (_commit_run_terminal). A cancel that lands
    AFTER _abort_if_superseded has passed but BEFORE the terminal commit must NOT be
    overwritten with COMPLETED. Reverting the write-back to an unconditional commit makes
    this go RED (the finishing thread resurrects the run to COMPLETED)."""
    from app.services import quality

    _p, doc_id, _c = _seed_project()
    wid = _wf_translate_only()
    real = quality.commit_mt_output
    state = {"fired": False, "run_id": None}

    def _patched(session, segment, text, **kw):
        # On the FIRST write-back seam call, cancel the run through a SEPARATE session
        # (a concurrent operator cancel). This is AFTER _abort_if_superseded passed, so
        # ONLY the terminal CAS stands between this and a COMPLETED resurrection.
        if not state["fired"]:
            state["fired"] = True
            with Session(engine) as s2:
                run = s2.exec(
                    select(WorkflowRun)
                    .where(WorkflowRun.workflow_id == wid)
                    .order_by(WorkflowRun.id.desc())
                ).first()
                state["run_id"] = run.id
                orchestrator.cancel_run(s2, run.id)
        return real(session, segment, text, **kw)

    set_pieces("app.services.quality", "commit_mt_output", _patched)
    try:
        with Session(engine) as s:
            events = list(orchestrator.run_workflow_stream(s, wid, doc_id, "es-ES"))
    finally:
        set_pieces("app.services.quality", "commit_mt_output", real)

    assert state["fired"], "the write-back seam never called commit_mt_output"
    with Session(engine) as s:
        run = s.get(WorkflowRun, state["run_id"])
        assert run.status == RunStatus.CANCELLED, (
            f"the terminal CAS must not resurrect a cancelled run to {run.status}"
        )
    # The finishing thread discarded its write-back -> it emits an error, never 'done'.
    assert not any(e.get("type") == "done" for e in events), (
        "a cancelled-mid-finish run must not emit a terminal 'done'",
        [e.get("type") for e in events],
    )
    print(
        "  [F] terminal CAS keeps a mid-finish cancel CANCELLED, not resurrected to "
        "COMPLETED (mutation-kill): OK"
    )


# ============================================================================
# ITEM 2 -- add-step blocked mid-flight, unblocked by cancel
# ============================================================================
def test_add_step_blocked_then_unblocked_by_cancel():
    _p, doc_id, _c = _seed_project()
    wid = _wf_human_review()
    run_id = _run_to_pause(wid, doc_id)
    # While the run is PAUSED (in flight), add-step is REFUSED (parity with move/edit/delete).
    with Session(engine) as s:
        try:
            wf.add_step(s, wid, name="Late", step_type=StepType.LQA)
            assert False, "add_step must be refused while a run is in flight"
        except wf.WorkflowError as exc:
            assert "in-flight run" in str(exc), str(exc)
    # Cancel the run -> a CANCELLED run does not count as in-flight -> add-step is ALLOWED.
    with Session(engine) as s:
        orchestrator.cancel_run(s, run_id)
    with Session(engine) as s:
        step = wf.add_step(s, wid, name="Late", step_type=StepType.LQA)
        assert step.id is not None, "add_step must succeed once the run is cancelled"
    print("  [ITEM2] add-step refused while in flight, allowed after cancel: OK")


# ============================================================================
# [E] /api cancel: happy path + terminal 409 + cross-tenant 404
# ============================================================================
def _admin_key():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    u = security.create_user(
        "rc-admin-%d@x.com" % _seq(),  # noqa: UP031
        "RC Admin",
        Role.ADMIN,
        "rc-admin-password-123",  # noqa: RUF100, UP031
    )
    _row, raw = apikeys.create_key(u.id, "rc-key-%d" % _seq(), scopes="api docs")  # noqa: UP031
    return {"X-Loc-Token": raw}


def test_api_cancel_happy_and_terminal_409():
    hdr = _admin_key()
    _p, doc_id, _c = _seed_project()
    wid = _wf_human_review()
    run_id = _run_to_pause(wid, doc_id)
    api = TestClient(_app())
    r = api.post("/api/runs/%d/cancel" % run_id, headers=hdr, json={})  # noqa: UP031
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["ok"] and body["status"] == RunStatus.CANCELLED, body
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.CANCELLED
    # A second cancel of the now-terminal run is a clean 409.
    r2 = api.post("/api/runs/%d/cancel" % run_id, headers=hdr, json={})  # noqa: UP031
    assert r2.status_code == 409, (r2.status_code, r2.text)
    assert r2.json().get("ok") is False
    # And a resume of the cancelled run is a clean 400 (not paused).
    r3 = api.post("/api/runs/%d/resume" % run_id, headers=hdr, json={"locale": "es-ES"})  # noqa: UP031
    assert r3.status_code == 400, (r3.status_code, r3.text)
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    print("  [E] /api cancel 200 -> CANCELLED; second cancel 409; resume 400: OK")


def test_api_cancel_cross_tenant_404():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    with Session(engine) as s:
        org_a = Organization(name="RC Org A %d" % _seq())  # noqa: UP031
        org_b = Organization(name="RC Org B %d" % _seq())  # noqa: UP031
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        oa, ob = org_a.id, org_b.id
    # #181: created IN its org (the seam refuses an org-less create under the gate).
    ua = security.create_user(
        "rc-a-%d@x.com" % _seq(),  # noqa: UP031
        "A",
        Role.ADMIN,
        "rc-a-pw-1234",
        org_id=oa,
    )
    with Session(engine) as s:
        row = s.get(User, ua.id)
        row.org_id = oa
        s.add(row)
        s.commit()
    _row, raw_a = apikeys.create_key(ua.id, "rc-a-key", scopes="api docs")
    hdr_a = {"X-Loc-Token": raw_a}
    # A run belonging to ORG B (its project is in org B).
    _pb, doc_b, _cb = _seed_project(org_id=ob)
    with Session(engine) as s:
        w = Workflow(name="W-%d" % _seq(), org_id=ob, source_locale="en-US", target_locale="es-ES")  # noqa: UP031
        s.add(w)
        s.commit()
        s.refresh(w)
        run_b = WorkflowRun(
            workflow_id=w.id, document_id=doc_b, target_locale="es-ES", status=RunStatus.PAUSED
        )
        s.add(run_b)
        s.commit()
        s.refresh(run_b)
        run_b_id = run_b.id
    api = TestClient(_app())
    r = api.post("/api/runs/%d/cancel" % run_b_id, headers=hdr_a, json={})  # noqa: UP031
    assert r.status_code == 404, (
        "org A must get 404 (not 403) on org B's run",
        r.status_code,
        r.text,
    )
    # The run in org B is untouched (still PAUSED).
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_b_id).status == RunStatus.PAUSED
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    print("  [E] /api cancel cross-tenant -> 404 (no enumeration), org B run untouched: OK")


# ============================================================================
# ITEM 3 -- the manual vendor-return body cap
# ============================================================================
def test_api_manual_return_body_cap():
    from app import config

    hdr = _admin_key()
    api = TestClient(_app())
    huge = b"x" * (config.WEBHOOK_MAX_BODY_BYTES + 1024)
    r = api.post("/api/vendors/returns", headers=hdr, content=huge)
    assert r.status_code == 413, (
        "an oversized manual return must be 413 before parse",
        r.status_code,
        r.text,
    )
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    print("  [ITEM3] /api/vendors/returns rejects an oversized body 413: OK")


def main():
    test_cancel_paused_run()
    test_cancel_running_run()
    test_cancel_terminal_run_refused()
    test_cancel_closes_leg_and_drops_late_return()
    test_abort_if_superseded_catches_a_cancelled_row()
    test_terminal_cas_prevents_resurrection_of_a_cancelled_run()
    test_add_step_blocked_then_unblocked_by_cancel()
    test_api_cancel_happy_and_terminal_409()
    test_api_cancel_cross_tenant_404()
    test_api_manual_return_body_cap()
    print("VERIFY RUN CANCEL OK")


if __name__ == "__main__":
    main()
