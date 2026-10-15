"""Conditional-workflow PHASE 3 verification (offline, hermetic).

SPEC-conditional-workflows Phase 3 = BRANCHING + bounded LOOP-BACK on top of the Phase-1
cursor machine + durable signal and the Phase-2 pre-step guards. maintainer decisions D1
(human-authorable step `key`), D2 (prev_qe = pass-rate aggregate), D3 (a branch requires a
gate-producing step — save-time error otherwise), D4 (per-step max_visits default 5 +
run-level total-step cap 50, both config-overridable).

This suite proves the acceptance bar:

  (1)  BYTE-STABLE WHEN UNUSED: a plain (no key/branch/max_visits) workflow runs
       byte-identically — deterministic mock output, NO SKIPPED rows, WorkflowRun.context
       stays NULL, durable fields neutral (segmented + full_text). The `while cursor`
       machine + RunGuard are INERT on a linear workflow.
  (2)  BRANCH on_pass -> end: an LQA step that PASSES routes on_pass='end', so a later
       Fix step never runs (segmented + full_text).
  (3)  BRANCH on_fail -> {step}: an LQA step that FAILS routes on_fail to an earlier Fix
       step (a bounded loop-back).
  (4)  LOOP-BACK UNTIL PASS: fix -> re-LQA -> repeat until the LQA passes. A stateful
       judge fails N times then passes; the Fix step runs exactly N+1 times, outcome_index
       increments per revisit, the run COMPLETES (segmented + full_text).
  (5)  LOOP-BACK UNTIL max_visits (D4, SOFT cap): the LQA never passes; the loop STOPS at
       the target's max_visits and the run COMPLETES (flagged loop_ended), NOT a hard fail.
  (6)  TOTAL-STEP cap (D4, HARD cap): a diverging loop trips the run-level total-step cap
       and FAILS cleanly with reason 'loop_cap_reached' (never an infinite loop).
  (7)  SPEND re-check ON REVISIT: the chain-aware spend gate is re-evaluated every loop
       visit — visit 1 runs (under cap), the 2nd visit is blocked -> FAILED
       'spend_cap_reached' (proves the gate sits inside the loop seam).
  (8)  RESUME MID-LOOP: a human_review step INSIDE a loop pauses; on resume the visit
       counts survive via WorkflowRun.context and the loop finishes correctly.
  (9)  SAVE-TIME VALIDATION (fail closed, D3): a branch on a gate-less step, an unknown
       target key, and a duplicate key are all REJECTED at add_step.
  (10) COPY / CLONE FIDELITY: clone_workflow + copy_to_pairs carry key + branch +
       max_visits onto every variant (keys are unique PER workflow -> copy verbatim).
  (11) CONFIG-OVERRIDABLE caps (D4): workflow_max_visits / workflow_total_step_cap read
       from settings.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (force MockProvider),
ASCII-only prints. Stateful LqaFailThenPass / a QE self-loop drive deterministic
fail-then-pass behavior offline.
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_condwf3_")

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
from app.services import orchestrator, settings, workflows
from app.services import providers as _prov
from app.services import spend as spend_svc
from app.services.providers import MockProvider, Provider, register_provider


class LqaFailThenPass(Provider):
    """A LIVE LQA judge that FAILS its first `n` scoring calls (a critical error ->
    gate 'failed') then PASSES (empty errors -> gate 'passed'), so a fix->re-LQA loop
    terminates deterministically offline."""

    name = "lqaftp"

    def __init__(self, n):
        self.n = n
        self.calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.calls += 1
        if self.calls <= self.n:
            return '{"errors":[{"dimension":"accuracy","severity":"critical","note":"bad"}]}'
        return '{"errors":[]}'


class LqaAlwaysFail(Provider):
    name = "lqaalwaysfail"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return '{"errors":[{"dimension":"accuracy","severity":"critical","note":"bad"}]}'


class LqaDeadJudge(Provider):
    """A CONFIGURED-LIVE judge whose every call returns an EMPTY completion, so the LQA
    card is lqa.JUDGE_UNAVAILABLE (a judge OUTAGE, issue #56). Counts its calls, so the
    F5 pin can assert the dead judge is invoked EXACTLY ONCE (no fix loop)."""

    name = "lqadead"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        LqaDeadJudge.calls += 1
        return ""


class QeFailExempt(MockProvider):
    """A QE judge that scores every segment below threshold (gate 'failed') but is a
    MockProvider subclass -> spend.is_exempt True, so it NEVER accrues spend. That keeps
    the spend-revisit test's ledger driven SOLELY by the Fix step's live engine (so the
    cap trips on the Fix step's own accumulated spend, deterministically on visit 2)."""

    name = "qefailexempt"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return '{"accuracy":5,"fluency":5,"terminology":5,"style":5,"rationale":"bad"}'


class LiveStub(Provider):
    """A non-mock ('configured') engine so spend.is_exempt is False, counting calls."""

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


def _reset_ledger():
    key = spend_svc.period_key()
    with Session(engine) as s:
        row = s.get(SpendLedger, key)
        if row:
            row.accumulated_usd = 0.0
            s.add(row)
            s.commit()
    spend_svc._tripped_audited.clear()


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
            "default_provider": "mock",
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
    """Direct-construct a step (bypasses save-time validation) so the ENGINE seam can be
    driven with arbitrary key/branch/max_visits. Validation is tested separately (9)."""
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


def _new_wf(name):
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        return wf.id


def _runs_for_step(run_id, step_id):
    return [sr for sr in stepruns_for(run_id) if sr.step_id == step_id]


# ---------------------------------------------------------------------------


def test_byte_stable_when_unused():
    """(1) A plain workflow with NO key/branch/max_visits runs byte-identically: mock
    output unchanged, no SKIPPED rows, context NULL, durable fields neutral (both paths).
    The cursor machine + RunGuard are inert on a linear workflow (the sacred gate)."""
    client = TestClient(app)
    for mode in ("segmented", "full_text"):
        wf_id = _new_wf(f"P3 plain {mode}")
        with Session(engine) as s:
            add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
            add_step(s, wf_id, 1, "Reviewer", prompt_text="Review: {text}")
        pid = new_project(client, f"P3 plain {mode}")
        doc = upload(client, pid, ["Alpha one.", "Beta two."], mode=mode)
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "completed", (mode, summary)
        srs = stepruns_for(summary["run_id"])
        assert all(sr.status != RunStatus.SKIPPED for sr in srs), (mode, "no skips")
        for sr in srs:
            assert sr.outcome_index == 0, (
                mode,
                "linear run: outcome_index stays 0",
                sr.outcome_index,
            )
        assert run_context(summary["run_id"]) is None, (
            mode,
            "a non-branching run must leave WorkflowRun.context NULL (byte-stable)",
        )
        filled = [sg for sg in segments_for(doc) if (sg.target_text or "").strip()]
        assert filled and "[fr-FR]" in filled[0].target_text, (mode, "mock output unchanged")
    print(
        "  (1) byte-stable when unused: linear run unchanged, context NULL, "
        "outcome_index 0 (segmented + full_text)"
    )


def _branch_lqa_wf(name, lqa_provider, on_pass, on_fail, lqa_max=None, fix_max=None):
    """translate(0) -> fix(1, key=fix) -> LQA(2, key=lqa, branch on_pass/on_fail).
    on_pass/on_fail are 'next'|'end'|{'step':'fix'}. fix runs the mock (deterministic)."""
    wf_id = _new_wf(name)
    with Session(engine) as s:
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "Fix", key="fix", prompt_text="Fix: {text}", max_visits=fix_max)
        add_step(
            s,
            wf_id,
            2,
            "LQA",
            key="lqa",
            step_type=StepType.LQA,
            provider=lqa_provider,
            max_visits=lqa_max,
            branch={"on_pass": on_pass, "on_fail": on_fail},
        )
    return wf_id


def test_branch_on_pass_ends():
    """(2) An LQA that PASSES routes on_pass='end' -> a trailing step after it never runs.
    Graph: translate -> LQA(pass, on_pass=end,on_fail=next) -> Finalize. Both paths."""
    client = TestClient(app)
    for mode in ("segmented", "full_text"):
        wf_id = _new_wf(f"P3 onpass {mode}")
        with Session(engine) as s:
            add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
            add_step(
                s,
                wf_id,
                1,
                "LQA",
                key="lqa",
                step_type=StepType.LQA,
                provider="mock",
                branch={"on_pass": "end", "on_fail": "next"},
            )
            add_step(s, wf_id, 2, "Finalize", prompt_text="Finalize: {text}")
        pid = new_project(client, f"P3 onpass {mode}")
        doc = upload(client, pid, ["Gamma one.", "Delta two."], mode=mode)
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "completed", (mode, summary)
        srs = stepruns_for(summary["run_id"])
        # translate + LQA only; Finalize (step index 2) must NOT have a StepRun (on_pass=end)
        with Session(engine) as s:
            steps = orchestrator.get_steps(s, wf_id)
        final_id = steps[2].id
        assert not _runs_for_step(summary["run_id"], final_id), (
            mode,
            "on_pass=end must SKIP the trailing Finalize step",
            [sr.step_id for sr in srs],
        )
        assert srs[1].gate == "passed", (mode, "mock LQA passes", srs[1].gate)
    print(
        "  (2) branch on_pass='end' finishes the run before the trailing step "
        "(segmented + full_text)"
    )


def test_branch_on_fail_loops_back():
    """(3) An LQA that FAILS routes on_fail={step:fix} -> the earlier Fix step runs a
    SECOND time (a bounded loop-back), then the (now-passing on attempt 2) LQA ends it."""
    client = TestClient(app)
    judge = LqaFailThenPass(1)  # fail once, pass on the 2nd
    register_provider("lqaftp", judge)
    wf_id = _branch_lqa_wf("P3 onfail", "lqaftp", on_pass="end", on_fail={"step": "fix"})
    pid = new_project(client, "P3 onfail")
    doc = upload(client, pid, ["Epsilon one.", "Zeta two."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    fix_runs = _runs_for_step(summary["run_id"], steps[1].id)
    lqa_runs = _runs_for_step(summary["run_id"], steps[2].id)
    assert len(fix_runs) == 2, ("Fix must run twice (initial + one loop-back)", len(fix_runs))
    assert len(lqa_runs) == 2, ("LQA must run twice (fail then pass)", len(lqa_runs))
    assert [sr.outcome_index for sr in fix_runs] == [0, 1], (
        "outcome_index must increment per revisit",
        [sr.outcome_index for sr in fix_runs],
    )
    assert lqa_runs[0].gate == "failed" and lqa_runs[1].gate == "passed", [
        sr.gate for sr in lqa_runs
    ]
    print("  (3) branch on_fail loops back to the earlier Fix step; outcome_index increments 0,1")


def test_dead_judge_holds_no_fix_loop():
    """(3b) ISSUE #56 F5: an LQA card whose judge is UNAVAILABLE fails the gate but must
    NOT drive the on_fail fix loop — the "failure" is a judge OUTAGE, not a quality defect,
    and looping would burn wasted full-document provider passes over a phantom. The dead
    judge is called EXACTLY ONCE, the Fix step runs only its single forward pass (never a
    loop-back), the run COMPLETES, and the StepRun records the honest reason."""
    LqaDeadJudge.calls = 0
    client = TestClient(app)
    register_provider("lqadead", LqaDeadJudge())
    wf_id = _branch_lqa_wf("P3 dead judge", "lqadead", on_pass="end", on_fail={"step": "fix"})
    pid = new_project(client, "P3 dead judge")
    doc = upload(client, pid, ["Eta one.", "Theta two."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    fix_runs = _runs_for_step(summary["run_id"], steps[1].id)
    lqa_runs = _runs_for_step(summary["run_id"], steps[2].id)
    assert LqaDeadJudge.calls == 1, (
        "dead judge must be called exactly once (no loop)",
        LqaDeadJudge.calls,
    )
    assert len(lqa_runs) == 1, ("LQA runs exactly once on a judge outage", len(lqa_runs))
    assert len(fix_runs) == 1, (
        "Fix must NOT loop back on a judge outage (forward pass only)",
        len(fix_runs),
    )
    assert lqa_runs[0].gate == "failed", lqa_runs[0].gate
    assert "judge unavailable" in (lqa_runs[0].error or ""), lqa_runs[0].error
    print(
        "  (3b) #56 F5: a dead judge holds (needs_review) — 1 visit, no fix loop, "
        "run completes with 'judge unavailable' on the step"
    )


def _lqa_then_downstream_wf(name, lqa_provider, on_fail):
    """translate(0) -> LQA(1, key=lqa, branch on_pass=next / on_fail=<arg>) -> DELIVER(2).
    Used to prove an UNAVAILABLE judge never lets a DOWNSTREAM step run (F12)."""
    wf_id = _new_wf(name)
    with Session(engine) as s:
        add_step(s, wf_id, 0, "Translate", prompt_text="Translate: {text}", key="t")
        add_step(
            s,
            wf_id,
            1,
            "LQA",
            step_type=StepType.LQA,
            provider=lqa_provider,
            key="lqa",
            branch={"on_pass": "next", "on_fail": on_fail},
        )
        add_step(s, wf_id, 2, "Deliver", prompt_text="Deliver: {text}", key="deliver")
    return wf_id


def test_dead_judge_terminates_never_falls_through():
    """(3c) ISSUE #56 F12 (BLOCKER): a judge OUTAGE must never be MORE permissive than a
    real failing judge. A bare cursor+1 hold fell THROUGH to a downstream DELIVER step on an
    outage, while a healthy failing judge routed on_fail:"end" and stopped. FIX: an outage
    TERMINATES the run at the LQA step — DELIVER never runs. Control: a healthy fail with
    on_fail:"end" also stops (unchanged); a PASS still routes on_pass:next so DELIVER runs
    (proving the workflow is not simply always-terminating)."""
    client = TestClient(app)
    register_provider("lqadead", LqaDeadJudge())
    register_provider("lqaalwaysfail", LqaAlwaysFail())

    def _deliver_ran(wf_id, run_id):
        with Session(engine) as s:
            steps = orchestrator.get_steps(s, wf_id)
        did = [st.id for st in steps if st.name == "Deliver"][0]  # noqa: RUF015
        return len(_runs_for_step(run_id, did)) > 0

    # (1) dead judge x on_fail:"end" x downstream -> run ends at LQA, DELIVER never runs.
    wf1 = _lqa_then_downstream_wf("F12 dead+end", "lqadead", on_fail="end")
    pid = new_project(client, "F12 dead+end")
    doc = upload(client, pid, ["Iota one."])
    with Session(engine) as s:
        sm1 = orchestrator.run_workflow(s, wf1, doc, "fr-FR")
    assert sm1["status"] == "completed", sm1
    assert _deliver_ran(wf1, sm1["run_id"]) is False, (
        "F12 BLOCKER: a downstream DELIVER step RAN on a judge outage"
    )

    # (2) control: healthy FAIL x on_fail:"end" also stops (DELIVER never runs) — the
    # outage is no more permissive than this.
    wf2 = _lqa_then_downstream_wf("F12 fail+end", "lqaalwaysfail", on_fail="end")
    pid2 = new_project(client, "F12 fail+end")
    doc2 = upload(client, pid2, ["Kappa one."])
    with Session(engine) as s:
        sm2 = orchestrator.run_workflow(s, wf2, doc2, "fr-FR")
    assert _deliver_ran(wf2, sm2["run_id"]) is False, "healthy fail on_fail:end must stop"

    # (3) control: a PASS (offline mock judge) routes on_pass:next -> DELIVER RUNS, so the
    # workflow is genuinely gate-driven, not always-terminating.
    wf3 = _lqa_then_downstream_wf("F12 pass", "mock", on_fail="end")
    pid3 = new_project(client, "F12 pass")
    doc3 = upload(client, pid3, ["Lambda one."])
    with Session(engine) as s:
        sm3 = orchestrator.run_workflow(s, wf3, doc3, "fr-FR")
    assert _deliver_ran(wf3, sm3["run_id"]) is True, "a PASS must route on_pass:next"
    print(
        "  (3c) #56 F12: a dead judge TERMINATES at the LQA step (DELIVER never runs); "
        "healthy fail:end also stops; a PASS still continues -> DELIVER runs"
    )


def test_loop_until_pass_both_paths():
    """(4) fix -> re-LQA -> repeat until pass. Judge fails 2x then passes -> Fix runs
    exactly 3 times, the run COMPLETES. Segmented + full_text."""
    client = TestClient(app)
    for mode in ("segmented", "full_text"):
        judge = LqaFailThenPass(2)
        register_provider("lqaftp", judge)
        wf_id = _branch_lqa_wf(f"P3 loop {mode}", "lqaftp", on_pass="end", on_fail={"step": "fix"})
        pid = new_project(client, f"P3 loop {mode}")
        doc = upload(client, pid, ["Eta one.", "Theta two."], mode=mode)
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "completed", (mode, summary)
        with Session(engine) as s:
            steps = orchestrator.get_steps(s, wf_id)
        fix_runs = _runs_for_step(summary["run_id"], steps[1].id)
        assert len(fix_runs) == 3, (mode, "Fix must run 3x (2 fails + final pass)", len(fix_runs))
        assert [sr.outcome_index for sr in fix_runs] == [0, 1, 2], (
            mode,
            [sr.outcome_index for sr in fix_runs],
        )
        ctx = run_context(summary["run_id"])
        assert ctx and ctx.get("visits", {}).get("fix") == 3, (
            mode,
            "context tracks 3 fix visits",
            ctx,
        )
    print("  (4) loop until pass: Fix runs N+1 times, run completes (segmented + full_text)")


def test_loop_stops_at_max_visits_soft_cap():
    """(5) The LQA NEVER passes; the loop STOPS at the target's max_visits (soft cap D4)
    and the run COMPLETES, flagged (loop_ended) — NOT a hard fail. fix.max_visits=3 ->
    Fix runs exactly 3 times.

    SPEC-conditional-workflows §4.2 ERRATUM E1 (ratified 2026-09-06, gate C round 1 H2):
    the cut used to resolve to `cursor + 1` unconditionally, so an exhausted correction
    loop ran every step AFTER the gate on text whose last verdict was FAILED. It now ends
    AT THE GATE in the HELD posture. Still a SOFT cap (the run COMPLETES, flagged); what
    changed is that the run does not fall through, and the text it produced is withheld
    from delivery until a reviewer approves. Pinned here so the erratum cannot be
    un-shipped silently; the delivery half is pinned by verify_lqa_loop_progress (16)."""
    client = TestClient(app)
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    wf_id = _branch_lqa_wf(
        "P3 maxvisits", "lqaalwaysfail", on_pass="end", on_fail={"step": "fix"}, fix_max=3
    )
    pid = new_project(client, "P3 maxvisits")
    doc = upload(client, pid, ["Iota one.", "Kappa two."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", (
        "max_visits is a SOFT cap: the run COMPLETES (flagged), not a hard fail",
        summary,
    )
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    fix_runs = _runs_for_step(summary["run_id"], steps[1].id)
    assert len(fix_runs) == 3, ("Fix must run exactly max_visits=3 times", len(fix_runs))
    ctx = run_context(summary["run_id"])
    assert ctx and "fix" in ctx.get("loop_ended", []), (
        "the graceful loop-end must be flagged in run.context.loop_ended",
        ctx,
    )
    # ERRATUM E1: the exhausted loop ends HELD at the gate, with the reason on the ONE
    # run-level channel the web run page, the DEFAULT GET /api/runs/{id} payload, the
    # DEFAULT `workflow show-run` output and MCP work_status all render.
    held = [
        w
        for w in (ctx.get("warnings") or [])
        if w.get("code") == orchestrator.GATE_HOLD_WARNING_CODE
    ]
    assert held and held[0]["cause"] == orchestrator.GATE_HOLD_LOOP_EXHAUSTED, (
        "D4 erratum: an exhausted on_fail loop must end HELD at the gate, naming the cause",
        ctx.get("warnings"),
    )
    print(
        "  (5) loop stops at max_visits (soft cap): Fix runs 3x, run COMPLETES, "
        "loop_ended flagged, gate HELD"
    )


def test_total_step_cap_hard_fail():
    """(6) A diverging loop trips the run-level TOTAL-STEP cap (hard, D4) -> the run FAILS
    cleanly with reason 'loop_cap_reached' (never spins forever). Cap set to 4 via
    settings; fix.max_visits raised so the total cap fires first."""
    client = TestClient(app)
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    settings.set_many({"workflow_total_step_cap": "4"})
    try:
        wf_id = _branch_lqa_wf(
            "P3 totalcap", "lqaalwaysfail", on_pass="end", on_fail={"step": "fix"}, fix_max=50
        )
        pid = new_project(client, "P3 totalcap")
        doc = upload(client, pid, ["Lambda one.", "Mu two."])
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "failed", ("total-step cap must FAIL the run", summary)
        assert "loop_cap_reached" in (summary.get("error") or ""), (
            "the failure reason must be loop_cap_reached",
            summary.get("error"),
        )
        srs = stepruns_for(summary["run_id"])
        assert srs[-1].status == RunStatus.FAILED and "loop_cap_reached" in (srs[-1].error or ""), (
            "the terminal StepRun must record the cap",
            [(sr.status, sr.error) for sr in srs],
        )
    finally:
        settings.set_many({"workflow_total_step_cap": "50"})
    print(
        "  (6) total-step cap: diverging loop FAILS cleanly 'loop_cap_reached' (no infinite loop)"
    )


def test_spend_recheck_on_revisit():
    """(7) The chain-aware spend gate is re-evaluated EVERY loop visit. A QE-gated Fix
    step self-loops on_fail; visit 1 runs (under cap), then the accumulated spend trips
    the cap so visit 2 is BLOCKED -> FAILED 'spend_cap_reached'. Proves the gate sits
    inside the loop seam (a per-visit pre-check), not just once at the start."""
    client = TestClient(app)
    register_provider("claude", LiveStub())
    register_provider("qefailexempt", QeFailExempt())
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0.0001"})  # any positive cap: visit1 (acc 0) runs,
    LiveStub.calls = 0  # visit2 (acc>cap) blocks
    try:
        wf_id = _new_wf("P3 spend revisit")
        with Session(engine) as s:
            add_step(s, wf_id, 0, "Translator", provider="mock", prompt_text="Translate: {text}")
            # a QE-gated llm_agent Fix on the LIVE engine that self-loops on_fail. The QE
            # judge is spend-EXEMPT (MockProvider subclass), so only Fix's live calls
            # accrue -> the cap trips on Fix's OWN spend, on the 2nd visit.
            add_step(
                s,
                wf_id,
                1,
                "Fix",
                key="fix",
                provider="claude",
                prompt_text="Fix: {text}",
                qe_gate=True,
                qe_provider="qefailexempt",
                qe_threshold=90,
                max_visits=10,
                branch={"on_pass": "end", "on_fail": {"step": "fix"}},
            )
        pid = new_project(client, "P3 spend revisit")
        # ONE segment so visit 1 is a single call that COMPLETES under the cap (and
        # accrues spend); the cap then blocks the whole of visit 2 at its pre-check.
        doc = upload(client, pid, ["Nu one."])
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "failed", ("the revisit must be spend-blocked", summary)
        assert "Spend cap reached" in (summary.get("error") or ""), (
            "the reason must be the spend cap",
            summary.get("error"),
        )
        assert LiveStub.calls > 0, ("visit 1 must have RUN before the cap tripped", LiveStub.calls)
        with Session(engine) as s:
            steps = orchestrator.get_steps(s, wf_id)
        fix_runs = _runs_for_step(summary["run_id"], steps[1].id)
        # >=2 fix StepRuns: at least one COMPLETED (visit1) + one FAILED spend (visit2)
        assert any(
            sr.status == RunStatus.FAILED and "Spend cap reached" in (sr.error or "")
            for sr in fix_runs
        ), (
            "a later visit's StepRun must be FAILED with the spend reason",
            [(sr.status, sr.error) for sr in fix_runs],
        )
        assert any(sr.status == RunStatus.COMPLETED for sr in fix_runs), (
            "the FIRST visit must have completed (under cap) before the block",
            fix_runs,
        )
    finally:
        _prov._OVERRIDES.pop("claude", None)
        settings.set_many({"spend_cap_usd": "0"})
        _reset_ledger()
    print(
        "  (7) spend re-checked on revisit: visit 1 runs, visit 2 blocked -> "
        "FAILED 'spend_cap_reached'"
    )


def test_resume_mid_loop():
    """(8) A human_review step INSIDE a loop: the run pauses, and on resume the loop's
    visit counts survive via WorkflowRun.context (the durable scratchpad). Judge fails
    once -> one loop-back -> two pauses/resumes -> completes, Fix ran twice."""
    client = TestClient(app)
    judge = LqaFailThenPass(1)
    register_provider("lqaftp", judge)
    wf_id = _new_wf("P3 resume loop")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "Fix", key="fix", prompt_text="Fix: {text}")
        add_step(s, wf_id, 2, "Human", step_type=StepType.HUMAN_REVIEW)
        add_step(
            s,
            wf_id,
            3,
            "LQA",
            key="lqa",
            step_type=StepType.LQA,
            provider="lqaftp",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
    pid = new_project(client, "P3 resume loop")
    doc = upload(client, pid, ["Omicron one.", "Pi two."])
    with Session(engine) as s:
        r = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    run_id = r["run_id"]
    # Resume until the run reaches a terminal state (each loop pass pauses at the human step).
    guard = 0
    while r["status"] == "paused" and guard < 6:
        guard += 1
        # visits must be persisted in context WHILE paused (resume-safe scratchpad)
        ctx = run_context(run_id)
        assert ctx is not None and "visits" in ctx, ("paused loop must persist visits", ctx)
        edited = orchestrator.SEG_JOINER.join(["EDIT ONE", "EDIT TWO"])
        with Session(engine) as s:
            r = orchestrator.resume_workflow(s, run_id, "fr-FR", edited)
    assert r["status"] == "completed", ("the looped run must finish after resume(s)", r)
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    fix_runs = _runs_for_step(run_id, steps[1].id)
    assert len(fix_runs) == 2, ("Fix must run twice across the pause/resume loop", len(fix_runs))
    assert [sr.outcome_index for sr in fix_runs] == [0, 1], (
        "visit counts survived pause/resume",
        [sr.outcome_index for sr in fix_runs],
    )
    print(
        "  (8) resume mid-loop: visits survive pause/resume via run.context; loop "
        "finishes, Fix ran twice"
    )


def test_lqa_branch_self_targets_rejected():
    prior = [{"key": "fix", "step_type": StepType.LLM_AGENT}]
    accepted = []
    for slot in ("on_fail", "on_pass"):
        # Check both stored dict targets and the normalized bare-key shorthand.
        for target in ({"step": "lqa1"}, " lqa1 "):
            try:
                workflows.validate_branch(
                    {slot: target}, StepType.LQA, False, prior, self_key="lqa1"
                )
            except workflows.WorkflowError as exc:
                message = str(exc).lower()
                for reason in (
                    "scores text",
                    "does not change it",
                    "re-judges",
                    "repeat its verdict",
                    "step that fixes the text",
                ):
                    assert reason in message, message
                assert slot in message and "lqa1" in message, message
                assert message.isascii(), message
            else:
                accepted.append((slot, target))
    assert not accepted, ("LQA self-targets must fail closed", accepted)
    print("  (9a) LQA on_fail/on_pass self-targets rejected with reason and remedy: OK")


def test_161_group_member_self_target_is_a_group_entry_not_a_self_loop():
    """#161: the (9a) refusal above must NOT fire for a PARALLEL-GROUP member.

    Inside a group, a member's own key is not a self-loop: `_group_errors` resolves a
    branch target to its group's span and permits exactly one shape, a target that is the
    group's FIRST step, which re-runs the WHOLE group -- siblings included, and siblings
    DO change text. That rule is adversarially reviewed (part4 MEDIUM-3) and it already
    refuses the unsafe shape (a NON-FIRST member). #149's guard fired here too, so two
    guards held contradictory opinions about one graph and the stricter won at authoring
    time: verify_parallel_routing could not build its fixture, and main shipped red to
    prod v110.
    """
    prior = [{"key": "fix", "step_type": StepType.LLM_AGENT}]
    for slot in ("on_fail", "on_pass"):
        for target in ({"step": "lb_main"}, " lb_main "):
            clean = workflows.validate_branch(
                {slot: target},
                StepType.LQA,
                False,
                prior,
                self_key="lb_main",
                parallel_group="g",
            )
            assert clean == {slot: {"step": "lb_main"}}, clean

    # The standalone refusal is untouched: #149's actual defect was a DRAFTED correction
    # loop whose check branched to itself, and the drafter never emits a parallel_group.
    for pg in (None, "", False):
        try:
            workflows.validate_branch(
                {"on_fail": {"step": "lqa1"}},
                StepType.LQA,
                False,
                prior,
                self_key="lqa1",
                parallel_group=pg,
            )
        except workflows.WorkflowError as exc:
            assert "scores text" in str(exc).lower(), exc
        else:
            raise AssertionError(f"#161: a standalone LQA self-target must still fail ({pg!r})")

    print("  (9a-161) group members may target their own key; standalone LQA still refused: OK")


def test_branch_valid_loops_and_lqa_sentinels():
    prior = [{"key": "fix", "step_type": StepType.LLM_AGENT}]
    for slot in ("on_fail", "on_pass"):
        branch = {slot: {"step": "fix"}}
        assert (
            workflows.validate_branch(branch, StepType.LQA, False, prior, self_key="lqa2") == branch
        )
        assert (
            workflows.validate_branch(branch, StepType.LLM_AGENT, True, [], self_key="fix")
            == branch
        )
        for target in ("next", "end", None):
            assert workflows.validate_branch(
                {slot: target}, StepType.LQA, False, prior, self_key="lqa1"
            ) == {slot: target or "next"}
    for branch in (None, {}, {"on_fail": "", "on_pass": " "}):
        assert (
            workflows.validate_branch(branch, StepType.LQA, False, prior, self_key="lqa1") is None
        )
    print("  (9b) LQA fix loops, QE self-loops, and LQA no-branch/next/end preserved: OK")


def test_save_time_validation():
    """(9) SAVE-TIME (fail closed, D3): a branch on a gate-less step, an unknown target
    key, and a duplicate key are ALL rejected at add_step."""
    # (a) branch on a plain llm_agent (no gate) -> D3 error
    wf_id = _new_wf("P3 val gate")
    with Session(engine) as s:
        workflows.add_step(s, wf_id, name="Translator", prompt_text="Translate: {text}", key="t")
        rejected = False
        try:
            workflows.add_step(
                s,
                wf_id,
                name="Plain",
                prompt_text="{text}",
                key="p",
                branch={"on_pass": "end", "on_fail": "next"},
            )
        except workflows.WorkflowError:
            rejected = True
        assert rejected, "a branch on a non-gate step must be REJECTED (D3)"
    # (b) unknown target key -> error (LQA branch to a step that does not exist)
    wf2 = _new_wf("P3 val unknown")
    with Session(engine) as s:
        workflows.add_step(s, wf2, name="Translator", prompt_text="Translate: {text}", key="t")
        rejected = False
        try:
            workflows.add_step(
                s,
                wf2,
                name="LQA",
                step_type=StepType.LQA,
                key="lqa",
                branch={"on_fail": {"step": "nope"}, "on_pass": "end"},
            )
        except workflows.WorkflowError:
            rejected = True
        assert rejected, "a branch to an unknown step key must be REJECTED"
    # (c) duplicate key -> error
    wf3 = _new_wf("P3 val dup")
    with Session(engine) as s:
        workflows.add_step(s, wf3, name="A", prompt_text="{text}", key="dup")
        rejected = False
        try:
            workflows.add_step(s, wf3, name="B", prompt_text="{text}", key="dup")
        except workflows.WorkflowError:
            rejected = True
        assert rejected, "a duplicate step key must be REJECTED"
    # (d) a VALID branch on an LQA step + a valid loop-back target is ACCEPTED
    wf4 = _new_wf("P3 val ok")
    with Session(engine) as s:
        workflows.add_step(s, wf4, name="Translator", prompt_text="Translate: {text}")
        workflows.add_step(s, wf4, name="Fix", prompt_text="Fix: {text}", key="fix")
        step = workflows.add_step(
            s,
            wf4,
            name="LQA",
            step_type=StepType.LQA,
            key="lqa",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        assert step.branch == {"on_pass": "end", "on_fail": {"step": "fix"}}, step.branch
        assert step.key == "lqa", step.key
    print(
        "  (9) save-time validation: branch-on-non-gate + unknown key + dup key "
        "rejected; a valid loop-back accepted"
    )


def test_copy_clone_fidelity():
    """(10) clone_workflow + copy_to_pairs carry key + branch + max_visits onto every
    variant. Keys are unique PER WORKFLOW, so they copy VERBATIM (no collision/remap)."""
    wf_id = _new_wf("P3 copy")
    with Session(engine) as s:
        workflows.add_step(s, wf_id, name="Translator", prompt_text="Translate: {text}")
        workflows.add_step(s, wf_id, name="Fix", prompt_text="Fix: {text}", key="fix", max_visits=4)
        workflows.add_step(
            s,
            wf_id,
            name="LQA",
            step_type=StepType.LQA,
            key="lqa",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        wf = s.get(Workflow, wf_id)
        wf.source_locale, wf.target_locale = "en-US", "fr-FR"
        s.add(wf)
        s.commit()
        # clone
        clone = workflows.clone_workflow(s, wf_id)
        cs = orchestrator.get_steps(s, clone.id)
        assert cs[1].key == "fix" and cs[1].max_visits == 4, (cs[1].key, cs[1].max_visits)
        assert cs[2].branch == {"on_pass": "end", "on_fail": {"step": "fix"}}, cs[2].branch
        # copy_to_pairs
        res = workflows.copy_to_pairs(s, wf_id, [("en-US", "de-DE")])
        vs = orchestrator.get_steps(s, res["created"][0]["workflow_id"])
        assert vs[1].key == "fix" and vs[1].max_visits == 4, (vs[1].key, vs[1].max_visits)
        assert vs[2].branch == {"on_pass": "end", "on_fail": {"step": "fix"}}, vs[2].branch
    print("  (10) clone + copy_to_pairs carry key/branch/max_visits verbatim to every variant")


def test_config_overridable_caps():
    """(11) The loop caps (D4) are config-overridable via settings; the RunGuard reads
    the live values."""
    settings.set_many({"workflow_max_visits": "7", "workflow_total_step_cap": "33"})
    try:
        assert orchestrator._int_setting("workflow_max_visits", 5) == 7
        assert orchestrator._int_setting("workflow_total_step_cap", 50) == 33
        # a blank/garbage stored value falls back to the default
        settings.set_many({"workflow_max_visits": ""})
        assert orchestrator._int_setting("workflow_max_visits", 5) == 5
    finally:
        settings.set_many({"workflow_max_visits": "5", "workflow_total_step_cap": "50"})
    print("  (11) loop caps are config-overridable (settings) with safe fallback")


def test_observe_run_live_pause_in_loop():
    """(12) SSE replay picks the LIVE pause, not a stale prior-iteration one. A
    human_review step revisited inside a loop persists ONE PAUSED StepRun per pause, all
    sharing order_index. observe_run must replay the NEWEST (highest-id) pause's
    input_text — selecting the first id-order match would show a prior iteration's stale
    text on every reconnect (observe_run closes at each pause, so re-observe is the norm).
    Regression for the branch-review SSE finding."""
    client = TestClient(app)
    register_provider("lqaftp_obs", LqaFailThenPass(2))  # fails 2x -> 3 pauses
    wf_id = _new_wf("P3 observe loop")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "Fix", key="fix", prompt_text="Fix: {text}")
        add_step(s, wf_id, 2, "Human", step_type=StepType.HUMAN_REVIEW)
        add_step(
            s,
            wf_id,
            3,
            "LQA",
            key="lqa",
            step_type=StepType.LQA,
            provider="lqaftp_obs",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
    pid = new_project(client, "P3 observe loop")
    doc = upload(client, pid, ["Alpha one.", "Beta two."])
    with Session(engine) as s:
        r = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    run_id = r["run_id"]
    pauses = 0
    while r["status"] == "paused" and pauses < 6:
        pauses += 1
        # A FRESH observe_run generator (== an SSE reconnect) must surface the LIVE pause.
        observed = None
        for _run, ev in orchestrator.observe_run(run_id, max_seconds=2):
            if ev["type"] == "paused":
                observed = ev["text"]
                break
            if ev["type"] in ("closed", "done"):
                break
        # The live pause text = the newest (highest-id) PAUSED StepRun's input.
        with Session(engine) as s:
            live = (
                s.exec(
                    select(StepRun)
                    .where(StepRun.workflow_run_id == run_id, StepRun.status == RunStatus.PAUSED)
                    .order_by(StepRun.id)
                )
                .all()[-1]
                .input_text
            )
        assert observed == live, (
            "observe_run must replay the LIVE (newest) pause, not a stale loop iteration",
            pauses,
            observed,
            live,
        )
        edited = orchestrator.SEG_JOINER.join([f"EDIT{pauses}A", f"EDIT{pauses}B"])
        with Session(engine) as s:
            r = orchestrator.resume_workflow(s, run_id, "fr-FR", edited)
    assert pauses >= 3, ("a 2-fail loop must pause 3 times", pauses)
    assert r["status"] == "completed", ("looped run must complete", r)
    print(
        "  (12) SSE replay surfaces the LIVE pause text in a human-in-loop (no stale "
        "prior-iteration replay)"
    )


def test_paused_review_in_loop():
    """(13) Issue #23 (a): orchestrator.paused_review (the headless show-run read) must
    surface the CURRENT visit's text at an in-loop human pause — newest-PAUSED-row
    semantics, the same rule observe_run uses. A stale prior-iteration read would hand a
    headless operator the wrong text to edit."""
    client = TestClient(app)
    register_provider("lqaftp_rev", LqaFailThenPass(1))  # fails once -> 2 pauses
    wf_id = _new_wf("P3 review loop")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "Fix", key="fix", prompt_text="Fix: {text}")
        add_step(s, wf_id, 2, "Human", step_type=StepType.HUMAN_REVIEW)
        add_step(
            s,
            wf_id,
            3,
            "LQA",
            key="lqa",
            step_type=StepType.LQA,
            provider="lqaftp_rev",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
    pid = new_project(client, "P3 review loop")
    doc = upload(client, pid, ["Sigma one.", "Tau two."])
    with Session(engine) as s:
        r = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    run_id = r["run_id"]
    seen_blobs = []
    pauses = 0
    while r["status"] == "paused" and pauses < 6:
        pauses += 1
        with Session(engine) as s:
            view = orchestrator.paused_review(s, run_id)
            # the newest (highest-id) PAUSED StepRun's input is the LIVE review text
            live = (
                s.exec(
                    select(StepRun)
                    .where(StepRun.workflow_run_id == run_id, StepRun.status == RunStatus.PAUSED)
                    .order_by(StepRun.id)
                )
                .all()[-1]
                .input_text
            )
        assert view is not None, "a paused in-loop run must be inspectable"
        assert view["review_text"] == live, (
            "paused_review must expose the LIVE (newest) pause, not a stale iteration",
            pauses,
            view["review_text"],
            live,
        )
        assert view["segment_count"] == 2, ("2 segments per pass", view)
        assert orchestrator.SEG_JOINER.join(view["segments"]) == view["review_text"], (
            "segments must re-join to the review blob exactly",
            view,
        )
        seen_blobs.append(view["review_text"])
        edited = orchestrator.SEG_JOINER.join([f"REV{pauses}A", f"REV{pauses}B"])
        with Session(engine) as s:
            r = orchestrator.resume_workflow(s, run_id, "fr-FR", edited)
    assert pauses >= 2, ("a 1-fail loop must pause twice", pauses)
    # visit 2's exposed text must DIFFER from visit 1's (it reflects the re-run Fix over
    # the visit-1 edit), proving the read is per-visit, not a frozen first-pause snapshot.
    assert seen_blobs[0] != seen_blobs[1], (
        "visit 2 review text must be the current visit's, not visit 1's",
        seen_blobs,
    )
    assert r["status"] == "completed", ("looped run must complete", r)
    print(
        "  (13) paused_review surfaces the CURRENT visit's text in a human-in-loop "
        "(newest-PAUSED-row), per-visit not stale"
    )


def main():
    test_lqa_branch_self_targets_rejected()
    test_161_group_member_self_target_is_a_group_entry_not_a_self_loop()
    test_branch_valid_loops_and_lqa_sentinels()
    init_db()
    settings.set_many({"spend_cap_usd": "0"})
    _reset_ledger()
    test_byte_stable_when_unused()
    test_branch_on_pass_ends()
    test_branch_on_fail_loops_back()
    test_dead_judge_holds_no_fix_loop()
    test_dead_judge_terminates_never_falls_through()
    test_loop_until_pass_both_paths()
    test_loop_stops_at_max_visits_soft_cap()
    test_total_step_cap_hard_fail()
    test_spend_recheck_on_revisit()
    test_resume_mid_loop()
    test_save_time_validation()
    test_copy_clone_fidelity()
    test_config_overridable_caps()
    test_observe_run_live_pause_in_loop()
    test_paused_review_in_loop()
    settings.set_many({"spend_cap_usd": "0"})
    print("CONDWF PHASE 3 OK")


if __name__ == "__main__":
    main()
