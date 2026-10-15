"""Conditional-workflow PHASE 2 verification (offline, hermetic).

SPEC-conditional-workflows Phase 2 = PRE-STEP GUARDS ("run this step only if the prior
LQA/QE outcome matches"). This suite proves the acceptance bar:

  (1)  BYTE-STABLE: a plain (no-run_condition) multi-step workflow is UNCHANGED — same
       output, NO SKIPPED rows, WorkflowRun.context stays NULL.
  (2)  GUARD SKIPS when false: [translate -> LQA(passes) -> fix{run only if prev_lqa
       failed}] SKIPS the fix step (segmented + full_text).
  (3)  GUARD RUNS when true: [translate -> LQA(fails) -> fix{prev_lqa:failed}] RUNS the
       fix step (segmented + full_text).
  (4)  PASS-THROUGH: a skipped step leaves the rolling text UNCHANGED, so a LATER step
       sees the prior (translate) output, not a mangled/blank one.
  (5)  prev_qe guard: [translate -> qe-gated step(fails) -> fix{prev_qe:score_lt:90}]
       RUNS the fix (QE pass-rate 0 < 90).
  (6)  RESUME-SAFE: a human_review step BETWEEN the LQA and the guarded step — the guard
       still evaluates correctly after pause/resume (it reads the DURABLE StepRun signal,
       not lost in-memory state).
  (7)  SAVE-TIME VALIDATION (fail closed): an unresolvable ref (no prior LQA) and a
       non-numeric score op are REJECTED at add_step, never at run time.
  (8)  INJECTION-SAFETY: a hand-crafted run_condition with an unknown ref/op or extra
       keys is REJECTED (never executed).
  (9)  CLONE + COPY_TO_PAIRS carry the guard to every variant (the copy-site footgun).
  (10) CLI compact form 'prev_lqa:score_lt:90' parses + round-trips through add_step.
  (11) PROMPTABLE draft proposes a guard from a description; the review confirm applies it.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (force MockProvider),
ASCII-only prints. A registered LqaFail / QeFail judge makes the LQA/QE FAIL
deterministically offline (the mock path always PASSES).
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_condwf2_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    RunStatus,
    StepRun,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, settings, workflows
from app.services.providers import Provider, register_provider


class LqaFail(Provider):
    """A LIVE LQA judge (registered -> provider_is_live True; the mock path returns [] =
    PASS) that always reports a CRITICAL error -> passed=False -> gate 'failed'."""

    name = "lqafail"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return '{"errors":[{"dimension":"accuracy","severity":"critical","note":"bad"}]}'


class QeFail(Provider):
    """A LIVE QE judge that reports enough errors to score 0 (below any threshold) ->
    the QE-gate aggregate is pass-rate 0, gate 'failed'."""

    name = "qefail"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return '{"accuracy":5,"fluency":5,"terminology":5,"style":5,"rationale":"bad"}'


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
    exercised with an arbitrary run_condition — the validation seam is tested separately."""
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


# ---------------------------------------------------------------------------


def test_plain_run_byte_stable():
    """(1) A plain workflow (no run_condition) is UNCHANGED: same output, NO SKIPPED
    StepRun rows, WorkflowRun.context stays NULL."""
    client = TestClient(app)
    for mode in ("segmented", "full_text"):
        wf_id = _new_wf(f"P2 plain {mode}")
        with Session(engine) as s:
            add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
            add_step(s, wf_id, 1, "Reviewer", prompt_text="Review: {text}")
        pid = new_project(client, f"P2 plain {mode}")
        doc = upload(client, pid, ["Alpha one.", "Beta two."], mode=mode)
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "completed", (mode, summary)
        srs = stepruns_for(summary["run_id"])
        assert all(sr.status != RunStatus.SKIPPED for sr in srs), (
            mode,
            "a plain run must produce NO SKIPPED rows",
            [sr.status for sr in srs],
        )
        assert run_context(summary["run_id"]) is None, (
            mode,
            "a plain run must leave WorkflowRun.context NULL",
        )
    print("  (1) plain workflow byte-stable: no SKIPPED rows, context NULL (segmented + full_text)")


def _guarded_lqa_workflow(name, lqa_provider, guard):
    """translate -> LQA(<lqa_provider>) -> fix{guard} -> finalize. lqa_provider 'mock'
    PASSES; 'lqafail' FAILS."""
    wf_id = _new_wf(name)
    with Session(engine) as s:
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "LQA", step_type=StepType.LQA, provider=lqa_provider)
        add_step(s, wf_id, 2, "Fix", prompt_text="Fix: {text}", run_condition=guard)
        add_step(s, wf_id, 3, "Finalize", prompt_text="Finalize: {text}")
    return wf_id


def test_guard_skips_when_false():
    """(2) LQA PASSES + guard {prev_lqa: failed} -> the Fix step is SKIPPED (its guard is
    false). Segmented + full_text."""
    client = TestClient(app)
    guard = {"ref": "prev_lqa", "op": "failed", "value": None}
    for mode in ("segmented", "full_text"):
        wf_id = _guarded_lqa_workflow(f"P2 skip {mode}", "mock", guard)
        pid = new_project(client, f"P2 skip {mode}")
        doc = upload(client, pid, ["Gamma one.", "Delta two."], mode=mode)
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "completed", (mode, summary)
        srs = stepruns_for(summary["run_id"])
        # translate(COMPLETED), LQA(COMPLETED), Fix(SKIPPED), Finalize(COMPLETED)
        fix_sr = srs[2]
        assert fix_sr.status == RunStatus.SKIPPED, (
            mode,
            "Fix must be SKIPPED when the LQA passed",
            [sr.status for sr in srs],
        )
        assert (
            srs[0].status == RunStatus.COMPLETED
            and srs[1].status == RunStatus.COMPLETED
            and srs[3].status == RunStatus.COMPLETED
        ), (mode, [sr.status for sr in srs])
    print("  (2) guard SKIPS the Fix step when the LQA passed (segmented + full_text)")


def test_guard_runs_when_true():
    """(3) LQA FAILS + guard {prev_lqa: failed} -> the Fix step RUNS (its guard is true).
    Segmented + full_text. The core the maintainer scenario."""
    client = TestClient(app)
    register_provider("lqafail", LqaFail())
    guard = {"ref": "prev_lqa", "op": "failed", "value": None}
    for mode in ("segmented", "full_text"):
        wf_id = _guarded_lqa_workflow(f"P2 run {mode}", "lqafail", guard)
        pid = new_project(client, f"P2 run {mode}")
        doc = upload(client, pid, ["Epsilon one.", "Zeta two."], mode=mode)
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "completed", (mode, summary)
        srs = stepruns_for(summary["run_id"])
        assert srs[1].gate == "failed", (mode, "precondition: LQA must FAIL", srs[1].gate)
        assert srs[2].status == RunStatus.COMPLETED, (
            mode,
            "Fix must RUN when the LQA failed",
            [sr.status for sr in srs],
        )
        assert all(sr.status != RunStatus.SKIPPED for sr in srs), (mode, [sr.status for sr in srs])
    print(
        "  (3) guard RUNS the Fix step when the LQA failed (segmented + full_text) "
        "- the core scenario"
    )


def test_skipped_passes_rolling_text_through():
    """(4) A SKIPPED step leaves the rolling text UNCHANGED, so the LATER Finalize step
    sees the prior (translate) output verbatim, not a blank/mangled one."""
    client = TestClient(app)
    guard = {"ref": "prev_lqa", "op": "failed", "value": None}
    wf_id = _guarded_lqa_workflow("P2 passthrough", "mock", guard)  # LQA passes -> Fix skipped
    pid = new_project(client, "P2 passthrough")
    doc = upload(client, pid, ["Passthrough one.", "Passthrough two."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    srs = stepruns_for(summary["run_id"])
    fix_sr, final_sr = srs[2], srs[3]
    assert fix_sr.status == RunStatus.SKIPPED, [sr.status for sr in srs]
    # the skipped step passes the rolling text straight through: its output == its input,
    # and the Finalize step's INPUT equals the skipped step's output (no gap/mangle).
    assert fix_sr.input_text == fix_sr.output_text, (
        "skipped step output_text must equal input_text (pass-through)",
    )
    assert final_sr.input_text == fix_sr.output_text, (
        "the later Finalize step must see the SAME rolling text the skip passed through",
        final_sr.input_text,
        fix_sr.output_text,
    )
    print(
        "  (4) skipped step passes rolling text through: a later step sees the prior "
        "output verbatim"
    )


def test_prev_qe_guard_runs_on_score_lt():
    """(5) prev_qe guard: [translate -> qe-gated step(FAILS) -> fix{prev_qe:score_lt:90}]
    RUNS the fix (QE pass-rate 0 < 90)."""
    client = TestClient(app)
    register_provider("qefail", QeFail())
    wf_id = _new_wf("P2 prev_qe")
    guard = {"ref": "prev_qe", "op": "score_lt", "value": 90.0}
    with Session(engine) as s:
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(
            s,
            wf_id,
            1,
            "QE Reviewer",
            prompt_text="Review: {text}",
            qe_gate=True,
            qe_provider="qefail",
            qe_threshold=70,
        )
        add_step(s, wf_id, 2, "Fix", prompt_text="Fix: {text}", run_condition=guard)
    pid = new_project(client, "P2 prev_qe")
    doc = upload(client, pid, ["Eta one.", "Theta two."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    srs = stepruns_for(summary["run_id"])
    assert srs[1].gate == "failed" and srs[1].score is not None and srs[1].score < 90, (
        "precondition: QE aggregate must be a failing pass-rate < 90",
        srs[1].score,
        srs[1].gate,
    )
    assert srs[2].status == RunStatus.COMPLETED, (
        "Fix must RUN when the QE pass-rate < 90",
        [sr.status for sr in srs],
    )
    print("  (5) prev_qe guard: QE pass-rate 0 < 90 -> Fix RUNS (score_lt)")


def test_guard_survives_pause_resume():
    """(6) A human_review step BETWEEN the LQA and the guarded step: after pause/resume,
    the guard STILL evaluates correctly because it reads the DURABLE StepRun signal (not
    lost in-memory state). LQA fails -> Fix (after the human step) RUNS post-resume."""
    client = TestClient(app)
    register_provider("lqafail", LqaFail())
    wf_id = _new_wf("P2 resume")
    guard = {"ref": "prev_lqa", "op": "failed", "value": None}
    with Session(engine) as s:
        add_step(s, wf_id, 0, "Translator", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "LQA", step_type=StepType.LQA, provider="lqafail")
        add_step(s, wf_id, 2, "Human", step_type=StepType.HUMAN_REVIEW)
        add_step(s, wf_id, 3, "Fix", prompt_text="Fix: {text}", run_condition=guard)
    pid = new_project(client, "P2 resume")
    doc = upload(client, pid, ["Iota one.", "Kappa two."])
    with Session(engine) as s:
        paused = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert paused["status"] == "paused" and paused["paused_step"] == 2, paused
    run_id = paused["run_id"]
    edited = orchestrator.SEG_JOINER.join(["EDIT ONE", "EDIT TWO"])
    with Session(engine) as s:
        resumed = orchestrator.resume_workflow(s, run_id, "fr-FR", edited)
    assert resumed["status"] == "completed", resumed
    srs = stepruns_for(run_id)
    # LQA StepRun (durable gate='failed') was committed BEFORE the pause; the Fix step
    # after resume must have RUN (COMPLETED), not skipped, proving the durable read.
    assert any(
        sr.status == RunStatus.COMPLETED for sr in srs if sr.step_id == _last_step_id(wf_id)
    ), (
        "the guarded Fix step must RUN after resume (durable LQA read)",
        [(sr.step_id, sr.status) for sr in srs],
    )
    assert all(sr.status != RunStatus.SKIPPED for sr in srs), (
        "no step should be SKIPPED here (LQA failed -> Fix runs)",
        [sr.status for sr in srs],
    )
    print("  (6) guard survives pause/resume: durable LQA signal read post-resume -> Fix RUNS")


def _last_step_id(wf_id):
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
        return steps[-1].id


def test_save_time_validation_rejects_bad_guards():
    """(7) SAVE-TIME (fail closed): an unresolvable ref (no prior LQA) and a non-numeric
    score op are REJECTED at add_step, so a run never hits them."""
    wf_id = _new_wf("P2 validate")
    with Session(engine) as s:
        # a workflow with NO prior LQA/QE step
        workflows.add_step(s, wf_id, name="Translator", prompt_text="Translate: {text}")
        # unresolvable ref: prev_lqa with no earlier LQA step
        rejected_unresolvable = False
        try:
            workflows.add_step(
                s,
                wf_id,
                name="Fix",
                prompt_text="Fix: {text}",
                run_condition={"ref": "prev_lqa", "op": "failed", "value": None},
            )
        except workflows.WorkflowError:
            rejected_unresolvable = True
        assert rejected_unresolvable, "unresolvable prev_lqa ref must be REJECTED at save"
        # add an LQA step so the ref would resolve, then a non-numeric score op
        workflows.add_step(s, wf_id, name="LQA", step_type=StepType.LQA)
        rejected_nonnumeric = False
        try:
            workflows.add_step(
                s,
                wf_id,
                name="Fix2",
                prompt_text="Fix: {text}",
                run_condition={"ref": "prev_lqa", "op": "score_lt", "value": "ninety"},
            )
        except workflows.WorkflowError:
            rejected_nonnumeric = True
        assert rejected_nonnumeric, "a score op with a non-numeric value must be REJECTED"
    print("  (7) save-time validation rejects an unresolvable ref + a non-numeric score op")


def test_score_op_rejects_non_finite():
    """GitHub #502: NaN/inf pass float() and a bare type check, then fail OPEN at run time
    (NaN/inf compare false against every score, so the guard silently never fires) or break
    a Postgres JSON bind. validate_run_condition must REFUSE a non-finite value while a
    real score (90, 90.5) still passes. The compact CLI form meets the same gate."""
    prior = [{"step_type": StepType.LQA}]  # an earlier LQA step makes prev_lqa resolve
    # a finite score still validates + round-trips (int and float both accepted)
    for ok in (90, 90.5):
        out = workflows.validate_run_condition(
            {"ref": "prev_lqa", "op": "score_lt", "value": ok}, prior
        )
        assert out == {"ref": "prev_lqa", "op": "score_lt", "value": float(ok)}, ok
    # NaN / +inf / -inf are refused (fail closed), never stored to fail open at run
    for bad in (float("nan"), float("inf"), float("-inf")):
        rejected = False
        try:
            workflows.validate_run_condition(
                {"ref": "prev_lqa", "op": "score_lt", "value": bad}, prior
            )
        except workflows.WorkflowError:
            rejected = True
        assert rejected, ("a non-finite score value must be REJECTED", bad)
    # a Python int wider than float range (the add/update-step API passes a JSON int raw)
    # makes float() itself raise OverflowError before any isfinite check; that must be
    # refused as a WorkflowError (400), not escape uncaught and 500 the route.
    rejected_huge = False
    try:
        workflows.validate_run_condition(
            {"ref": "prev_lqa", "op": "score_lt", "value": 10**400}, prior
        )
    except workflows.WorkflowError:
        rejected_huge = True
    assert rejected_huge, "an out-of-range int score value must be REFUSED"
    # the compact CLI form turns 'nan' into float('nan') in parse_run_condition_str, then
    # hits the same finite check inside validate_run_condition
    rejected_cli = False
    try:
        workflows.validate_run_condition("prev_lqa:score_lt:nan", prior)
    except workflows.WorkflowError:
        rejected_cli = True
    assert rejected_cli, "the compact form prev_lqa:score_lt:nan must be REFUSED"
    # ...and a finite compact form still round-trips unchanged
    ok_cli = workflows.validate_run_condition("prev_lqa:score_lt:90", prior)
    assert ok_cli == {"ref": "prev_lqa", "op": "score_lt", "value": 90.0}, ok_cli
    print("  (+) validate_run_condition refuses NaN/inf score values; 90 and 90.5 still pass")


def test_injection_safety():
    """(8) A hand-crafted run_condition with an unknown ref/op or EXTRA keys is REJECTED
    at save time (never executed) — allowlist + extra-key guard."""
    wf_id = _new_wf("P2 inject")
    with Session(engine) as s:
        workflows.add_step(s, wf_id, name="Translator", prompt_text="Translate: {text}")
        workflows.add_step(s, wf_id, name="LQA", step_type=StepType.LQA)
        for bad in (
            {"ref": "os.system", "op": "failed", "value": None},  # unknown ref
            {"ref": "prev_lqa", "op": "eval", "value": None},  # unknown op
            {"ref": "prev_lqa", "op": "failed", "value": None, "x": 1},  # extra key
        ):
            rejected = False
            try:
                workflows.add_step(s, wf_id, name="Bad", prompt_text="{text}", run_condition=bad)
            except workflows.WorkflowError:
                rejected = True
            assert rejected, ("a malformed/injected run_condition must be REJECTED", bad)
    print("  (8) injection-safety: unknown ref/op + extra keys rejected, never executed")


def test_clone_and_copy_carry_guard():
    """(9) clone_workflow + copy_to_pairs carry run_condition onto EVERY variant (the
    copy-site footgun)."""
    wf_id = _new_wf("P2 clone")
    guard = {"ref": "prev_lqa", "op": "failed", "value": None}
    with Session(engine) as s:
        workflows.add_step(s, wf_id, name="Translator", prompt_text="Translate: {text}")
        workflows.add_step(s, wf_id, name="LQA", step_type=StepType.LQA)
        workflows.add_step(s, wf_id, name="Fix", prompt_text="Fix: {text}", run_condition=guard)
        # set the source workflow's pair so copy_to_pairs can clone onto new pairs
        wf = s.get(Workflow, wf_id)
        wf.source_locale, wf.target_locale = "en-US", "fr-FR"
        s.add(wf)
        s.commit()
        clone = workflows.clone_workflow(s, wf_id)
        clone_steps = orchestrator.get_steps(s, clone.id)
        assert clone_steps[2].run_condition == guard, (
            "clone must carry the guard",
            clone_steps[2].run_condition,
        )
        res = workflows.copy_to_pairs(s, wf_id, [("en-US", "de-DE")])
        variant_id = res["created"][0]["workflow_id"]
        variant_steps = orchestrator.get_steps(s, variant_id)
        assert variant_steps[2].run_condition == guard, (
            "copy_to_pairs must carry the guard onto the variant",
            variant_steps[2].run_condition,
        )
    print("  (9) clone + copy_to_pairs carry the guard to every variant")


def test_cli_compact_form_round_trip():
    """(10) The CLI compact form 'prev_lqa:score_lt:90' parses + round-trips through
    add_step to the stored {ref, op, value} guard."""
    parsed = workflows.parse_run_condition_str("prev_lqa:score_lt:90")
    assert parsed == {"ref": "prev_lqa", "op": "score_lt", "value": 90.0}, parsed
    parsed2 = workflows.parse_run_condition_str("prev_lqa:failed")
    assert parsed2 == {"ref": "prev_lqa", "op": "failed", "value": None}, parsed2
    wf_id = _new_wf("P2 cli")
    with Session(engine) as s:
        workflows.add_step(s, wf_id, name="Translator", prompt_text="Translate: {text}")
        workflows.add_step(s, wf_id, name="LQA", step_type=StepType.LQA)
        step = workflows.add_step(
            s, wf_id, name="Fix", prompt_text="Fix: {text}", run_condition="prev_lqa:score_lt:90"
        )
        assert step.run_condition == {"ref": "prev_lqa", "op": "score_lt", "value": 90.0}, (
            "CLI compact form must round-trip through add_step",
            step.run_condition,
        )
    print("  (10) CLI compact form 'prev_lqa:score_lt:90' parses + round-trips")


def test_promptable_draft_and_apply():
    """(11) The promptable draft validates a model-proposed guard from a description, and
    the review confirm (apply_workflow_draft) applies it onto the created step."""
    from app.services import setup_author

    # (a) draft validation: a proposed guard resolves against an earlier LQA step.
    prior = [{"step_type": "llm_agent"}, {"step_type": "lqa", "qe_gate": False}]
    step, _warns = setup_author._validate_step_draft(
        {
            "name": "Fix",
            "step_type": "llm_agent",
            "prompt_text": "Fix: {text}",
            "run_condition": {"ref": "prev_lqa", "op": "failed", "value": None},
        },
        2,
        ["mock"],
        [],
        prior,
    )
    assert step["run_condition"] == {"ref": "prev_lqa", "op": "failed", "value": None}, (
        "the draft must keep a resolvable proposed guard",
        step["run_condition"],
    )
    # a guard with no earlier LQA is DROPPED (warned), not applied.
    step2, warns2 = setup_author._validate_step_draft(
        {
            "name": "Fix",
            "step_type": "llm_agent",
            "prompt_text": "{text}",
            "run_condition": {"ref": "prev_lqa", "op": "failed", "value": None},
        },
        0,
        ["mock"],
        [],
        [],
    )
    assert step2["run_condition"] is None and warns2, (
        "an unresolvable proposed guard must be dropped + warned",
        step2,
        warns2,
    )
    # (b) apply: a review payload with a resolvable guard creates a step carrying it.
    payload = {
        "name": "Promptable WF",
        "customer_id": None,
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "steps": [
            {"name": "Translator", "step_type": "llm_agent", "prompt_text": "Translate: {text}"},
            {"name": "LQA", "step_type": "lqa", "prompt_text": ""},
            {
                "name": "Fix",
                "step_type": "llm_agent",
                "prompt_text": "Fix: {text}",
                "run_condition": {"ref": "prev_lqa", "op": "failed", "value": None},
            },
        ],
        "notes_for_reviewer": "",
    }
    with Session(engine) as s:
        wf = setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        steps = orchestrator.get_steps(s, wf.id)
        assert steps[2].run_condition == {"ref": "prev_lqa", "op": "failed", "value": None}, (
            "apply must persist the confirmed guard onto the Fix step",
            steps[2].run_condition,
        )
    print("  (11) promptable draft proposes a guard; review confirm applies it")


def main():
    init_db()
    settings.set_many({"spend_cap_usd": "0"})
    test_plain_run_byte_stable()
    test_guard_skips_when_false()
    test_guard_runs_when_true()
    test_skipped_passes_rolling_text_through()
    test_prev_qe_guard_runs_on_score_lt()
    test_guard_survives_pause_resume()
    test_save_time_validation_rejects_bad_guards()
    test_score_op_rejects_non_finite()
    test_injection_safety()
    test_clone_and_copy_carry_guard()
    test_cli_compact_form_round_trip()
    test_promptable_draft_and_apply()
    print("CONDWF PHASE 2 OK")


if __name__ == "__main__":
    main()
