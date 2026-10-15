"""Conditional-workflow PHASE 1 verification (offline, byte-stable foundation).

SPEC-conditional-workflows Phase 1 is deliberately a NO-BEHAVIOR-CHANGE foundation:
  (A) the workflow-run path is brought under the spend cap (closes an earlier known bug);
  (B) both orchestrator loops are rewritten `for i in range()` -> `while cursor`, a
      PROVEN NO-OP (_next_cursor always returns cursor+1 this phase);
  (C) durable columns StepRun.score/gate/outcome_index + WorkflowRun.context are
      added + populated (LQA/QE) but READ BY NOBODY yet.

This suite proves the acceptance bar:

  (1) BYTE-STABLE: a plain multi-step workflow (segmented AND full_text) still
      produces the same MT output + StepRun rows; the NEW durable fields stay NEUTRAL
      on a plain run (score/gate NULL, outcome_index 0) and WorkflowRun.context stays
      NULL (a non-conditional run never writes it).
  (1b) DURABLE SIGNAL populated where there IS an outcome: an LQA step writes
      score(float)+gate("passed"/"failed"); a QE-gated step writes the run-level QE
      aggregate; a plain llm_agent step leaves them NULL.
  (2) SPEND GATE: with the cap tripped and a REAL (non-mock) provider whose WHOLE
      chain is blocked, the run fails FAST to FAILED with a "Spend cap reached" reason,
      the provider is NEVER called (fail before fan-out), and NO target is written — on
      BOTH segmented + full_text. But the gate mirrors run_completion's per-target chain
      semantics, so (2c) a capped primary with an EXEMPT (mock) FALLBACK still COMPLETES
      via the fallback (identical to main, not a regression), and (2d) a capped LQA step
      soft-degrades + COMPLETES (LQA is not gated). The MockProvider stays EXEMPT.
  (3) PAUSE/RESUME across a human_review step still works with the cursor machine
      (pause at the right index, resume re-splits + finishes), context NULL throughout.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (force MockProvider),
ASCII-only prints, FastAPI TestClient.
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_condwf1_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    RunStatus,
    Segment,
    SpendLedger,
    StepRun,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, settings
from app.services import providers as _prov
from app.services import spend as spend_svc
from app.services.providers import Provider, register_provider


class LiveStub(Provider):
    """A 'configured' engine (NOT a MockProvider, so spend.is_exempt is False). Counts
    calls so a blocked run can be proven to never reach the provider."""

    name = "claude"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        LiveStub.calls += 1
        self.last_usage = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "[live] " + (payload or {}).get("text", "")


class QEJudge(Provider):
    """A judge engine that returns a clean, all-zero MQM card (score 100 -> passes)
    so the QE-gate aggregate is DETERMINISTIC offline (MockProvider returns junk JSON
    and would soft-skip)."""

    name = "qejudge"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"ok"}'


def _reset_ledger():
    key = spend_svc.period_key()
    with Session(engine) as s:
        row = s.get(SpendLedger, key)
        if row:
            row.accumulated_usd = 0.0
            s.add(row)
            s.commit()
    spend_svc._tripped_audited.clear()


def _trip_cap():
    """Force the monthly spend cap into the TRIPPED state (tiny cap, spend over it)."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0.0001"})
    spend_svc.record_usage("claude", "claude-opus-4-8", {"input_tokens": 1_000_000})  # $5
    assert spend_svc.would_block()[0] is True, "precondition: cap must be tripped"


def docx_from(paragraphs) -> bytes:
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def new_project(client, name, targets="fr-FR"):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": targets,
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def upload(client, pid, paragraphs, mode="segmented"):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": mode},
        files={
            "file": (
                "d.docx",
                docx_from(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def add_step(
    session, wf_id, order, name, step_type=StepType.LLM_AGENT, provider="mock", prompt_text="", **kw
):
    session.add(
        WorkflowStep(
            workflow_id=wf_id,
            order_index=order,
            name=name,
            step_type=step_type,
            provider=provider,
            prompt_text=prompt_text,
            chunk_policy="auto",
            retry_max=1,
            backoff_base=0.0,
            **kw,
        )
    )
    session.commit()


def stepruns_for(run_id):
    with Session(engine) as s:
        return s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()


def segments_for(doc_id, locale="fr-FR"):
    with Session(engine) as s:
        return sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == locale
                )
            ).all(),
            key=lambda x: x.order_index,
        )


def run_context(run_id):
    with Session(engine) as s:
        return s.get(WorkflowRun, run_id).context


# ---------------------------------------------------------------------------


def test_plain_run_byte_stable():
    """(1) A plain multi-step workflow (segmented + full_text) completes unchanged;
    the new durable fields stay NEUTRAL (score/gate NULL, outcome_index 0) and
    WorkflowRun.context stays NULL — a non-conditional run never writes it."""
    client = TestClient(app)
    # --- segmented, 2 llm_agent steps over 2 segments ---
    with Session(engine) as s:
        wf = Workflow(name="P1 plain seg", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "Reviewer", prompt_text="Review: {text}")
    pid = new_project(client, "P1 plain")
    doc = upload(client, pid, ["Adventure awaits.", "Save big this weekend."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    assert len(summary["steps"]) == 2, summary
    segs = segments_for(doc)
    assert len(segs) == 2
    for sg in segs:
        # deterministic MockProvider output ([<target>] prefix), chained twice
        # (per-segment) — the exact byte-stable behavior the existing suites lock in,
        # unchanged by the refactor.
        assert (sg.target_text or "").strip() and sg.target_text.count("[fr-FR]") >= 2, (
            sg.target_text
        )
        assert sg.status == "mt" and sg.mt_confidence == 0.9, (sg.status, sg.mt_confidence)
    assert "Adventure" in segs[0].target_text and "Save big" in segs[1].target_text, (
        segs[0].target_text,
        segs[1].target_text,
    )
    # NEW durable fields neutral on a plain run; context NULL
    srs = stepruns_for(summary["run_id"])
    assert len(srs) == 2, len(srs)
    for sr in srs:
        assert sr.score is None and sr.gate is None and sr.outcome_index == 0, (
            "plain llm_agent StepRun must leave the durable signal neutral",
            sr.score,
            sr.gate,
            sr.outcome_index,
        )
    assert run_context(summary["run_id"]) is None, (
        "a non-conditional run must NEVER write WorkflowRun.context (stays NULL)"
    )

    # --- full_text, single llm_agent step ---
    with Session(engine) as s:
        wff = Workflow(name="P1 plain ft", scope="global")
        s.add(wff)
        s.commit()
        s.refresh(wff)
        wff_id = wff.id
        add_step(s, wff_id, 0, "Translator", prompt_text="Translate: {text}")
    ftdoc = upload(client, pid, ["Whole document one.", "Whole document two."], mode="full_text")
    with Session(engine) as s:
        ft = orchestrator.run_workflow(s, wff_id, ftdoc, "fr-FR")
    assert ft["status"] == "completed", ft
    ftfilled = [sg for sg in segments_for(ftdoc) if (sg.target_text or "").strip()]
    assert ftfilled and "[fr-FR]" in ftfilled[0].target_text, ftfilled
    for sr in stepruns_for(ft["run_id"]):
        assert sr.score is None and sr.gate is None and sr.outcome_index == 0, (
            "plain full_text StepRun durable signal must be neutral",
            sr.score,
            sr.gate,
        )
    assert run_context(ft["run_id"]) is None, "full_text plain run context must be NULL"
    print(
        "  (1) plain segmented+full_text runs unchanged; durable fields neutral; "
        "context NULL (non-conditional run never writes it)"
    )


def test_durable_signal_populated_on_outcome():
    """(1b) The durable signal IS populated where a step produces an outcome: an LQA
    step writes score(float)+gate; a QE-gated step writes the run-level QE aggregate;
    a plain llm_agent step in the same run stays NULL. outcome_index is 0 (no loops)."""
    client = TestClient(app)
    # translate -> LQA -> qe-gated translate step
    register_provider("qejudge", QEJudge())
    with Session(engine) as s:
        wf = Workflow(name="P1 outcome", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "LQA", step_type=StepType.LQA, provider="mockprov")
        add_step(
            s,
            wf_id,
            2,
            "QE Reviewer",
            prompt_text="Review: {text}",
            qe_gate=True,
            qe_provider="qejudge",
            qe_threshold=70,
        )
    pid = new_project(client, "P1 outcome")
    doc = upload(client, pid, ["Known good sentence.", "Another good sentence."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    srs = stepruns_for(summary["run_id"])
    assert len(srs) == 3, len(srs)
    tr, lqa_sr, qe_sr = srs
    # step 0 plain llm_agent -> neutral
    assert tr.score is None and tr.gate is None and tr.outcome_index == 0, (
        tr.score,
        tr.gate,
        tr.outcome_index,
    )
    # step 1 LQA -> score float, gate passed/failed, index 0
    assert isinstance(lqa_sr.score, float), ("LQA StepRun.score must be a float", lqa_sr.score)
    assert lqa_sr.gate in ("passed", "failed"), ("LQA gate must be canonical", lqa_sr.gate)
    assert lqa_sr.outcome_index == 0, lqa_sr.outcome_index
    # step 2 qe-gated -> run-level QE aggregate populated (all-zero judge => 100/passed)
    assert qe_sr.score is not None and qe_sr.gate in ("passed", "failed"), (
        "QE-gated StepRun must carry the run-level aggregate",
        qe_sr.score,
        qe_sr.gate,
    )
    assert qe_sr.gate == "passed" and abs(qe_sr.score - 100.0) < 1e-9, (
        "all-zero MQM judge => pass-rate 100, gate passed",
        qe_sr.score,
        qe_sr.gate,
    )
    assert qe_sr.outcome_index == 0, qe_sr.outcome_index
    print(
        "  (1b) durable signal populated on outcome: LQA score/gate, QE aggregate; "
        "plain step stays NULL; outcome_index 0"
    )


def test_spend_gate_fails_fast_segmented_and_full_text():
    """(2) Cap tripped + a REAL provider -> run FAILED with a 'Spend cap reached'
    reason, the provider is NEVER called (fail BEFORE fan-out), NO target written,
    StepRun FAILED, context NULL. Proven on BOTH the segmented and full_text paths."""
    client = TestClient(app)
    register_provider("claude", LiveStub())
    try:
        for mode in ("segmented", "full_text"):
            _trip_cap()
            LiveStub.calls = 0
            with Session(engine) as s:
                wf = Workflow(name=f"P1 spend {mode}", scope="global")
                s.add(wf)
                s.commit()
                s.refresh(wf)
                wf_id = wf.id
                # provider="claude" resolves to the LiveStub (a real, non-mock engine)
                add_step(
                    s, wf_id, 0, "Translator", provider="claude", prompt_text="Translate: {text}"
                )
            pid = new_project(client, f"P1 spend {mode}")
            doc = upload(client, pid, ["Costs money one.", "Costs money two."], mode=mode)
            with Session(engine) as s:
                run = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
            assert run["status"] == "failed", (mode, run)
            assert "Spend cap reached" in (run.get("error") or ""), (mode, run.get("error"))
            assert LiveStub.calls == 0, (
                mode,
                "spend gate must fail BEFORE any provider call",
                LiveStub.calls,
            )
            with Session(engine) as s:
                wr = s.get(WorkflowRun, run["run_id"])
                assert wr.status == RunStatus.FAILED, (mode, wr.status)
                assert wr.context is None, (mode, "failed run must not write context")
            srs = stepruns_for(run["run_id"])
            assert (
                srs
                and srs[-1].status == RunStatus.FAILED
                and "Spend cap reached" in (srs[-1].error or "")
            ), (
                mode,
                "the blocked step's StepRun must be FAILED with the reason",
                [(sr.status, sr.error) for sr in srs],
            )
            for sg in segments_for(doc):
                assert not (sg.target_text or "").strip(), (
                    mode,
                    "capped run must leave NO target written",
                    sg.target_text,
                )
            print(
                f"  (2) spend gate [{mode}]: run FAILED 'Spend cap reached', provider "
                f"never called, no target written, context NULL"
            )
    finally:
        _prov._OVERRIDES.pop("claude", None)
        settings.set_many({"spend_cap_usd": "0"})
        _reset_ledger()


def test_spend_gate_fallback_rescue_completes():
    """(2c) REGRESSION (adversarial-review MED): a capped/blocked PRIMARY with an
    EXEMPT (mock) FALLBACK must still COMPLETE via the fallback — IDENTICAL to main —
    NOT fail. run_completion checks spend per-target and runs the first unblocked one,
    so the step-level gate must return None whenever ANY chain target is runnable, or
    Phase 1 stops being a no-op. The live primary is never called (it's blocked); the
    mock fallback produces the target."""
    client = TestClient(app)
    register_provider("claude", LiveStub())
    try:
        for mode in ("segmented", "full_text"):
            _trip_cap()
            LiveStub.calls = 0
            with Session(engine) as s:
                wf = Workflow(name=f"P1 fallback {mode}", scope="global")
                s.add(wf)
                s.commit()
                s.refresh(wf)
                wf_id = wf.id
                # primary 'claude' (LiveStub, blocked over cap) + exempt 'mock' fallback
                add_step(
                    s,
                    wf_id,
                    0,
                    "Translator",
                    provider="claude",
                    prompt_text="Translate: {text}",
                    fallback_provider="mock",
                )
            pid = new_project(client, f"P1 fallback {mode}")
            doc = upload(client, pid, ["Rescue one.", "Rescue two."], mode=mode)
            with Session(engine) as s:
                run = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
            assert run["status"] == "completed", (
                mode,
                "capped primary + exempt fallback must COMPLETE (match main)",
                run,
            )
            # the blocked primary is never called; the mock fallback wrote the target
            assert LiveStub.calls == 0, (mode, "blocked primary must not be called", LiveStub.calls)
            filled = [sg for sg in segments_for(doc) if (sg.target_text or "").strip()]
            assert filled and "[fr-FR]" in filled[0].target_text, (
                mode,
                "mock fallback must write the target",
                [sg.target_text for sg in filled],
            )
            with Session(engine) as s:
                assert s.get(WorkflowRun, run["run_id"]).status == RunStatus.COMPLETED, mode
            print(
                f"  (2c) fallback rescue [{mode}]: capped primary + mock fallback -> "
                f"COMPLETED via fallback (identical to main, not FAILED)"
            )
    finally:
        _prov._OVERRIDES.pop("claude", None)
        settings.set_many({"spend_cap_usd": "0"})
        _reset_ledger()


def test_capped_lqa_soft_degrades_not_gated():
    """(2d) A capped LQA step is NOT gated: lqa.score_text soft-degrades a blocked
    judge to a clean no-LLM scorecard and COMPLETES on main, so the step-level gate
    must leave LQA alone (gating it would diverge). [translate(mock) -> LQA(claude
    over cap)] must COMPLETE, matching main."""
    client = TestClient(app)
    register_provider("claude", LiveStub())
    try:
        _trip_cap()
        LiveStub.calls = 0
        with Session(engine) as s:
            wf = Workflow(name="P1 capped lqa", scope="global")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            wf_id = wf.id
            add_step(s, wf_id, 0, "Translator", provider="mock", prompt_text="Translate: {text}")
            add_step(s, wf_id, 1, "LQA", step_type=StepType.LQA, provider="claude")
        pid = new_project(client, "P1 capped lqa")
        doc = upload(client, pid, ["Score me one.", "Score me two."])
        with Session(engine) as s:
            run = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert run["status"] == "completed", (
            "a capped LQA must soft-degrade + COMPLETE (match main), not be gated",
            run,
        )
        assert any((sg.target_text or "").strip() for sg in segments_for(doc)), (
            "translate step's target must be written (LQA didn't fail the run)"
        )
        print(
            "  (2d) capped LQA not gated: soft-degrades to a no-LLM scorecard, run "
            "COMPLETES (matches main)"
        )
    finally:
        _prov._OVERRIDES.pop("claude", None)
        settings.set_many({"spend_cap_usd": "0"})
        _reset_ledger()


def test_mock_exempt_under_tiny_cap():
    """(2b) The MockProvider is EXEMPT: an over-cap run whose step resolves to the mock
    (keyless install) still COMPLETES — the gate never fires on a $0 engine."""
    client = TestClient(app)
    _trip_cap()
    try:
        with Session(engine) as s:
            wf = Workflow(name="P1 mock exempt", scope="global")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            wf_id = wf.id
            # 'gemini' is keyless here -> resolves to MockProvider -> exempt
            add_step(s, wf_id, 0, "Translator", provider="gemini", prompt_text="Translate: {text}")
        pid = new_project(client, "P1 mock exempt")
        doc = upload(client, pid, ["Free mock sentence."])
        with Session(engine) as s:
            run = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert run["status"] == "completed", ("mock must be exempt even over cap", run)
        assert any((sg.target_text or "").strip() for sg in segments_for(doc)), (
            "mock-exempt run must still write its target"
        )
        print("  (2b) mock exempt: an over-cap run on a $0 engine still completes")
    finally:
        settings.set_many({"spend_cap_usd": "0"})
        _reset_ledger()


def test_pause_resume_with_cursor_machine():
    """(3) A human_review step still pauses at the right index and resumes to
    completion under the `while cursor` machine; context stays NULL throughout."""
    client = TestClient(app)
    with Session(engine) as s:
        wf = Workflow(name="P1 human", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "Human", step_type=StepType.HUMAN_REVIEW)
        add_step(s, wf_id, 2, "Finalizer", prompt_text="Finalize: {text}")
    pid = new_project(client, "P1 human")
    doc = upload(client, pid, ["First para.", "Second para.", "Third para."])
    with Session(engine) as s:
        paused = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert paused["status"] == "paused" and paused["paused_step"] == 1, paused
    run_id = paused["run_id"]
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert run.status == RunStatus.PAUSED and run.current_step_index == 1, (
            run.status,
            run.current_step_index,
        )
        assert run.context is None, "paused run context must be NULL"
    parts = paused["paused_text"].split(orchestrator.SEG_JOINER)
    assert len(parts) == 3, len(parts)
    edited = orchestrator.SEG_JOINER.join(["EDIT ONE", "EDIT TWO", "EDIT THREE"])
    with Session(engine) as s:
        resumed = orchestrator.resume_workflow(s, run_id, "fr-FR", edited)
    assert resumed["status"] == "completed", resumed
    hsegs = segments_for(doc)
    assert len(hsegs) == 3
    for i, sg in enumerate(hsegs):
        assert "EDIT" in sg.target_text, (
            "resume must re-split edited blob by index",
            i,
            sg.target_text,
        )
    assert "EDIT ONE" in hsegs[0].target_text and "EDIT THREE" in hsegs[2].target_text
    assert run_context(run_id) is None, "completed non-conditional run context must be NULL"
    print(
        "  (3) human_review pause@1 -> resume completes under the cursor machine; "
        "context NULL throughout"
    )


def main():
    init_db()
    settings.set_many({"spend_cap_usd": "0"})
    _reset_ledger()
    # Plain steps use the built-in "mock" provider (deterministic, spend-exempt), so
    # every non-spend-gate assertion runs offline with byte-stable MockProvider output.
    test_plain_run_byte_stable()
    test_durable_signal_populated_on_outcome()
    test_spend_gate_fails_fast_segmented_and_full_text()
    test_spend_gate_fallback_rescue_completes()
    test_capped_lqa_soft_degrades_not_gated()
    test_mock_exempt_under_tiny_cap()
    test_pause_resume_with_cursor_machine()
    settings.set_many({"spend_cap_usd": "0"})
    print("CONDWF PHASE 1 OK")


if __name__ == "__main__":
    main()
