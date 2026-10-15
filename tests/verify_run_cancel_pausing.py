"""Run cancel vs pausing steps, the cancel/complete race and failure exits (Issue #492), plus
resume uses the run's own locale (Issue #493).

Hermetic + offline (MockProvider, injected httpx.MockTransport). ASCII-only; standalone
one-process-per-suite script (throwaway LOC_TMS_DATA_DIR, keys cleared).

  [A] a cancel landing while a step before a human_review step runs: the run stays CANCELLED,
      no PAUSED StepRun is written, the run does not reappear in a review queue.
  [B] the same for an EXTERNAL step: the vendor contract is NEVER sent and no leg is opened.
  [C] cancel_run is a compare-and-set: a run completed between its read and its write is not
      relabelled CANCELLED (RunCancelError, row stays COMPLETED).
  [D] the direct failure exit never overwrites a committed CANCELLED with FAILED.
  [E] resume_run_async / resume_workflow resume with the RUN's target_locale, never the
      caller's: a two-locale document keeps the other locale's segments untouched, on the
      service seam, the API route (no locale and a wrong explicit locale) and the web form.
  [F] a cancel committing AFTER the committed-state probe but before the PAUSED park (human
      review, and the post-dispatch window of an external step): the park is a
      compare-and-set, so the run stays CANCELLED with no PAUSED row and no open leg.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_runcancelpause_")

import httpx
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TransMode,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
)
from app.services import orchestrator, vendor_dispatch, vendors
from app.services import workflows as wf

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _seed_project(locales=("es-ES",)):
    with Session(engine) as s:
        cust = Customer(name="C-%d" % _seq())  # noqa: UP031
        s.add(cust)
        s.commit()
        s.refresh(cust)
        p = Project(
            name="P-%d" % _seq(),  # noqa: UP031
            source_locale="en-US",
            default_provider="mock",
            customer_id=cust.id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for loc in locales:
            for i, src in enumerate(("Hello world.", "The cat sat on the mat.")):
                s.add(
                    Segment(
                        document_id=d.id,
                        target_locale=loc,
                        order_index=i,
                        source_text=src,
                        status=SegStatus.NEW,
                    )
                )
        s.commit()
        return p.id, d.id, cust.id


def _new_wf(target="es-ES"):
    with Session(engine) as s:
        w = Workflow(
            name="W-%d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale="en-US",
            target_locale=target,
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


def _cancel_before(step_type, wid):
    """Patch the per-step seam so the operator's cancel lands (committed, separate session)
    right before the first step of `step_type` is dispatched: exactly the race the issue
    describes (cancel while the earlier step runs, thread then reaches the pausing step)."""
    real = orchestrator._execute_step
    state = {"fired": False}

    def _wrapped(session, run, steps, step, cursor, **kw):
        if step.step_type == step_type and not state["fired"]:
            state["fired"] = True
            with Session(engine) as s2:
                orchestrator.cancel_run(s2, run.id)
        result = yield from real(session, run, steps, step, cursor, **kw)
        return result

    set_pieces("app.services.orchestrator", "_execute_step", _wrapped)
    return real, state


def test_cancel_before_human_review_stays_cancelled():
    _p, doc_id, _c = _seed_project()
    wid = _new_wf()
    with Session(engine) as s:
        wf.add_step(s, wid, name="Review", step_type=StepType.HUMAN_REVIEW)
    real, state = _cancel_before(StepType.HUMAN_REVIEW, wid)
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, wid, doc_id, "es-ES"))
    finally:
        set_pieces("app.services.orchestrator", "_execute_step", real)
    assert state["fired"], "the human_review step was never reached"
    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == wid)
            .order_by(WorkflowRun.id.desc())
        ).first()
        assert run.status == RunStatus.CANCELLED, ("cancelled run was revived", run.status)
        paused = s.exec(
            select(StepRun).where(
                StepRun.workflow_run_id == run.id, StepRun.status == RunStatus.PAUSED
            )
        ).all()
        assert not paused, "a cancelled run must not gain a PAUSED review row"
    print("  [A] cancel before human_review: run stays CANCELLED, no PAUSED row: OK")


def test_cancel_before_external_sends_nothing():
    _p, doc_id, cust_id = _seed_project()
    wid = _new_wf()
    sent = []

    def _handler(req):
        sent.append(req)
        return httpx.Response(200, json={"ok": True})

    with Session(engine) as s:
        v = Vendor(name="V-%d" % _seq(), endpoint_url="https://vendor.example.com/intake")  # noqa: UP031
        v.secret_enc = vendors._enc_secret({"signing_secret": "topsecret"})
        s.add(v)
        s.commit()
        s.refresh(v)
        vendors.link_customer(s, v.id, cust_id)
        wf.add_step(s, wid, name="Vendor", step_type=StepType.EXTERNAL, vendor_id=v.id)
    real, state = _cancel_before(StepType.EXTERNAL, wid)
    vendor_dispatch.set_test_transport(httpx.MockTransport(_handler))
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, wid, doc_id, "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
        set_pieces("app.services.orchestrator", "_execute_step", real)
    assert state["fired"], "the external step was never reached"
    assert not sent, "the vendor contract was sent for a cancelled run"
    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == wid)
            .order_by(WorkflowRun.id.desc())
        ).first()
        assert run.status == RunStatus.CANCELLED, ("cancelled run was revived", run.status)
        legs = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).all()
        assert not legs, "no vendor leg may be opened for a cancelled run"
    print("  [B] cancel before external: nothing sent, no leg, run stays CANCELLED: OK")


def test_cancel_run_is_compare_and_set():
    _p, doc_id, _c = _seed_project()
    wid = _new_wf()
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=wid, document_id=doc_id, target_locale="es-ES", status=RunStatus.RUNNING
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id

    class _Racing(Session):
        def get(self, *a, **k):
            row = super().get(*a, **k)
            # The finishing thread's guarded terminal commit lands after cancel_run's read.
            with Session(engine) as s2:
                r2 = s2.get(WorkflowRun, run_id)
                r2.status = RunStatus.COMPLETED
                s2.add(r2)
                s2.commit()
            return row

    raised = False
    with _Racing(engine) as s:
        try:
            orchestrator.cancel_run(s, run_id)
        except orchestrator.RunCancelError:
            raised = True
    assert raised, "cancel_run must refuse a run that completed first"
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED, (
            "a delivered run was relabelled CANCELLED"
        )
    print("  [C] cancel_run is a compare-and-set; a completed run stays COMPLETED: OK")


def test_failure_exit_keeps_cancelled():
    from app.services.orchestrator_part2 import _commit_run_terminal_direct

    _p, doc_id, _c = _seed_project()
    wid = _new_wf()
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=wid, document_id=doc_id, target_locale="es-ES", status=RunStatus.RUNNING
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
    with Session(engine) as stale:
        stale_run = stale.get(WorkflowRun, run_id)
        with Session(engine) as s2:
            orchestrator.cancel_run(s2, run_id)
        _commit_run_terminal_direct(stale, stale_run, RunStatus.FAILED)
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.CANCELLED, (
            "a failure exit overwrote CANCELLED with FAILED"
        )
    print("  [D] failure exit never overwrites CANCELLED with FAILED: OK")


# --------------------------------------------------------------------------- #
# #493: resume uses the run's own locale
# --------------------------------------------------------------------------- #
def _paused_ja_run():
    """A two-locale document (de-DE, ja-JP); a ja-JP run PAUSED at human_review."""
    _p, doc_id, _c = _seed_project(locales=("de-DE", "ja-JP"))
    wid = _new_wf(target="ja-JP")
    with Session(engine) as s:
        wf.add_step(s, wid, name="Review", step_type=StepType.HUMAN_REVIEW)
    with Session(engine) as s:
        events = list(orchestrator.run_workflow_stream(s, wid, doc_id, "ja-JP"))
    assert any(e.get("type") == "paused" for e in events), [e.get("type") for e in events]
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wid)).first()
        assert run.status == RunStatus.PAUSED and run.target_locale == "ja-JP"
        return run.id, doc_id


def _de_snapshot(doc_id):
    with Session(engine) as s:
        rows = s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
            .order_by(Segment.order_index)
        ).all()
        return [(r.target_text, r.status) for r in rows]


def test_resume_uses_run_locale_service_seam():
    run_id, doc_id = _paused_ja_run()
    before = _de_snapshot(doc_id)
    with Session(engine) as s:
        # The caller passes the WRONG locale (what _first_locale yields for this document).
        orchestrator.resume_run_async(s, run_id, "de-DE", "", actor="t@x.com")
    import time

    deadline = time.time() + 60
    while time.time() < deadline:
        with Session(engine) as s:
            st = s.get(WorkflowRun, run_id).status
        if st in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            break
        time.sleep(0.2)
    assert _de_snapshot(doc_id) == before, "resume wrote into the de-DE segments"
    print("  [E] resume with a wrong caller locale leaves the other locale untouched: OK")


def _wait_terminal(run_id):
    import time

    deadline = time.time() + 60
    while time.time() < deadline:
        with Session(engine) as s:
            st = s.get(WorkflowRun, run_id).status
        if st in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            return st
        time.sleep(0.2)
    return st


def test_resume_api_and_web_use_run_locale():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Role
    from app.services import apikeys, security, settings

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    u = security.create_user("rcp-admin@x.com", "RCP Admin", Role.ADMIN, "rcp-admin-password-123")
    _row, raw = apikeys.create_key(u.id, "rcp-key", scopes="api docs")
    hdr = {"X-Loc-Token": raw}
    client = TestClient(app)
    # API, wrong explicit locale -> refused 400, run still PAUSED, de-DE untouched.
    run_id, doc_id = _paused_ja_run()
    before = _de_snapshot(doc_id)
    r = client.post("/api/runs/%d/resume" % run_id, headers=hdr, json={"locale": "de-DE"})  # noqa: UP031
    assert r.status_code == 400, (r.status_code, r.text)
    with Session(engine) as s:
        assert s.get(WorkflowRun, run_id).status == RunStatus.PAUSED
    assert _de_snapshot(doc_id) == before
    # API, no locale (the doc's first locale is de-DE) -> resumes as ja-JP.
    r = client.post("/api/runs/%d/resume" % run_id, headers=hdr, json={})  # noqa: UP031
    assert r.status_code == 200, (r.status_code, r.text)
    assert _wait_terminal(run_id) == RunStatus.COMPLETED
    assert _de_snapshot(doc_id) == before, "API resume without a locale wrote into de-DE"
    with Session(engine) as s:
        ja = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "ja-JP")
        ).all()
        assert all(g.target_text for g in ja), "the run's own locale was not translated"
    # Web form with the wrong locale field (auth off: the form route needs no session then).
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    run_id, doc_id = _paused_ja_run()
    before = _de_snapshot(doc_id)
    r = client.post(
        "/runs/%d/resume" % run_id,  # noqa: UP031
        data={"locale": "de-DE", "edited_text": ""},
        follow_redirects=False,
    )
    assert r.status_code in (303, 302), (r.status_code, r.text[:200])
    _wait_terminal(run_id)
    assert _de_snapshot(doc_id) == before, "web resume wrote into de-DE"
    print("  [E] API (no locale / wrong locale) and web form resume with the run locale: OK")


# --------------------------------------------------------------------------- #
# #492 (gate): the cancel lands AFTER the committed-state probe, before the park commit
# --------------------------------------------------------------------------- #
def _cancel_after_probe(fire_on_call):
    """Patch the part9 supersede probe so that, on its `fire_on_call`-th call, the operator's
    cancel commits (separate session) and the probe still answers "not superseded": exactly
    the microsecond window between the probe and the PAUSED commit."""
    from app.services import orchestrator_part9 as p9

    real = p9._abort_if_superseded
    state = {"calls": 0, "fired": False}

    def _probe(session, run):
        state["calls"] += 1
        answer = real(session, run)
        if state["calls"] == fire_on_call:
            with Session(engine) as s2:
                orchestrator.cancel_run(s2, run.id)
            state["fired"] = True
        return answer

    p9._abort_if_superseded = _probe
    return p9, real, state


def _latest_run(wid):
    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == wid)
            .order_by(WorkflowRun.id.desc())
        ).first()
        paused = s.exec(
            select(StepRun).where(
                StepRun.workflow_run_id == run.id, StepRun.status == RunStatus.PAUSED
            )
        ).all()
        return run, paused


def test_cancel_after_probe_human_review_stays_cancelled():
    _p, doc_id, _c = _seed_project()
    wid = _new_wf()
    with Session(engine) as s:
        wf.add_step(s, wid, name="Review", step_type=StepType.HUMAN_REVIEW)
    # call 1 = the Translate step's probe, call 2 = the Review step's probe.
    p9, real, state = _cancel_after_probe(2)
    try:
        with Session(engine) as s:
            events = list(orchestrator.run_workflow_stream(s, wid, doc_id, "es-ES"))
    finally:
        p9._abort_if_superseded = real
    assert state["fired"], "the human_review probe was never reached"
    run, paused = _latest_run(wid)
    assert run.status == RunStatus.CANCELLED, ("cancel lost to the PAUSED park", run.status)
    assert not paused, "a cancelled run must not gain a PAUSED review row"
    assert not any(e.get("type") == "paused" for e in events), "a paused event was emitted"
    print("  [F] cancel after the probe, before the human_review park: stays CANCELLED: OK")


def test_cancel_after_dispatch_probe_external_stays_cancelled():
    _p, doc_id, cust_id = _seed_project()
    wid = _new_wf()
    with Session(engine) as s:
        v = Vendor(name="V-%d" % _seq(), endpoint_url="https://vendor.example.com/intake")  # noqa: UP031
        v.secret_enc = vendors._enc_secret({"signing_secret": "topsecret"})
        s.add(v)
        s.commit()
        s.refresh(v)
        vendors.link_customer(s, v.id, cust_id)
        wf.add_step(s, wid, name="Vendor", step_type=StepType.EXTERNAL, vendor_id=v.id)
    # call 1 = Translate probe, call 2 = Vendor pre-step probe, call 3 = post-dispatch probe.
    p9, real, state = _cancel_after_probe(3)
    vendor_dispatch.set_test_transport(
        httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    )
    try:
        with Session(engine) as s:
            list(orchestrator.run_workflow_stream(s, wid, doc_id, "es-ES"))
    finally:
        vendor_dispatch.set_test_transport(None)
        p9._abort_if_superseded = real
    assert state["fired"], "the post-dispatch probe was never reached"
    run, paused = _latest_run(wid)
    assert run.status == RunStatus.CANCELLED, ("cancel lost to the PAUSED park", run.status)
    assert not paused, "a cancelled run must not gain a PAUSED vendor row"
    with Session(engine) as s:
        legs = s.exec(select(VendorDispatch).where(VendorDispatch.workflow_run_id == run.id)).all()
        open_legs = [
            g
            for g in legs
            if g.status in (VendorDispatchStatus.DISPATCHED, VendorDispatchStatus.RETRY_WAIT)
        ]
        assert not open_legs, ("a cancelled run kept an open vendor leg", [g.status for g in legs])
    print("  [F] cancel after the post-dispatch probe, before the park: stays CANCELLED: OK")


def main():
    test_cancel_before_human_review_stays_cancelled()
    test_cancel_before_external_sends_nothing()
    test_cancel_run_is_compare_and_set()
    test_failure_exit_keeps_cancelled()
    test_cancel_after_probe_human_review_stays_cancelled()
    test_cancel_after_dispatch_probe_external_stays_cancelled()
    test_resume_uses_run_locale_service_seam()
    test_resume_api_and_web_use_run_locale()
    print("VERIFY RUN CANCEL PAUSING OK")


if __name__ == "__main__":
    main()
