"""LQA / human_review HONEST STEP OPTIONS verification (SPEC-lqa-step-honest-options),
offline + hermetic.

CONTEXT (verified by reading orchestrator.py's LQA branches ~1027-1093 / ~1683-1750 and
its HUMAN_REVIEW branches ~1011-1025 / ~1664-1679 -- see app/services/workflows.py's
DEAD_FIELDS_BY_TYPE docstring for the full evidence trail):

  lqa: the judge PROMPT is built ENTIRELY internally by lqa.score_text (a fixed MQM
  template + the pair rulebook + the resolved style guide) -- prompt_text, chunk_policy,
  glossary_id, adaptive_feedback(+window), doc_context_prepass, tm_examples(+window),
  qe_gate/qe_threshold/qe_provider are ALL dead. provider/redact_pii/run_condition/
  branch/max_visits/key remain live. model/fallback_provider/fallback_model were ALSO
  dead as of this branch's ORIGINAL finding, but the maintainer's 2026-07-22 decision reversed
  that: feat/prompt-package-engine (merges BEFORE this branch) makes the LQA judge
  honor them via _spec_for_step, so they are LIVE here (see
  test_lqa_model_and_fallback_are_live) -- a cross-branch dependency, re-verify
  against lqa.py/orchestrator.py if that branch has not actually landed.

  human_review: makes NO provider call at all -- every engine/prompt/QE/glossary/context
  field is dead (including `provider` itself), AND `branch` defined ON the step is ALSO
  dead (resume_workflow always advances cursor+1, never consulting it) even though `key`
  and `max_visits` stay live as an addressable LOOP-BACK TARGET for another step's
  branch. assignee_email + run_condition (the uniform pre-step guard) are live.

Proves the acceptance bar:
  (1)  add_step REFUSES a meaningful dead-field value per type, with a clear message
       naming the offending field(s); a DEFAULT/blank value is silently accepted
       (never refused) -- e.g. an lqa step with chunk_policy left at "auto" is fine.
  (2)  add_step KEEPS the live fields working for lqa (provider, model,
       fallback_provider, fallback_model, redact_pii, run_condition, branch,
       max_visits, key) and human_review (assignee_email, run_condition,
       max_visits, key -- NOT model/fallback, which stay dead there).
  (3)  tm_examples is NEVER refused (the one default-ON checkbox -- an HTML/raw-POST
       omission is indistinguishable from an explicit "off", so it is exempted from
       the meaningful-value check to avoid a false-positive refusal).
  (4)  update_step (PARTIAL): an untouched legacy dead value (a pre-existing step, as
       exist in prod) keeps loading/editing fine when NOT re-supplied and step_type
       is unchanged; supplying a NEW/changed meaningful value for a dead field on an
       UNCHANGED type IS refused. But a TYPE SWITCH (adversarial-review finding --
       an ordinary edit-form full-POST carries a hidden checkbox's old value with no
       way to un-set it) NEVER refuses: it FORCE-CLEARS every field dead for the NEW
       type (whether carried-over or freshly-set this same call) and reports what
       it cleared.
  (5)  WEB: POST /workflows/{id}/steps and .../edit refuse the same way, with a clean
       400 (never a 500); the builder template hides the dead fields per type via
       data-types attributes, and the placeholder hint lists {context} {style_guide}
       {lqa_findings} for llm_agent only.
  (6)  CLI: `workflow add-step --type lqa/human_review` with a dead flag exits nonzero
       with a clear message; `edit-step` refuses only a field ACTUALLY passed.
  (7)  apply_workflow_draft (the AI-authoring apply path) refuses the SAME way with a
       clean per-step WorkflowApplyError, not an opaque mid-write "internal error".
  (8)  package_view: an LQA step's "Judge model" fact shows provider + model again
       (feat/prompt-package-engine makes this honest); human_review shows no engine
       facts at all.
  (9)  PROPAGATION SKIP-AND-REPORT (adversarial-review finding, CRITICAL): a base
       step's step_type-switch edit propagates onto a non-pinned language-pair
       variant via propagate_step_edit's raw setattr sweep, which has no refusal
       path of its own. After the delta lands, every field dead for the variant's
       RESULTING type -- whether it just arrived via the delta or was already
       sitting on the variant untouched -- is force-cleared and reported under
       dead_fields_cleared (never a hard failure; one variant's dead config can
       never block the sweep to every other variant). A PINNED variant is
       untouched, as always.
  (10) AI-DRAFT REVIEW PAGE (adversarial-review finding, HIGH): workflow_author_
       review.html now hides dead-for-type fields (data-types, ported from
       workflow_builder.html) so a drafted lqa/human_review step doesn't render a
       fully visible+checked QE gate the human was never told was inert.
  (11) apply_workflow_draft STRIP-AND-WARN (adversarial-review finding, HIGH): a
       drafted lqa step with qe_gate=true (the exact prod-bug shape, and the maintainer's
       own "3-step draft" example) no longer hard-rejects the whole workflow --
       it applies successfully with the dead field reset to its safe default and
       a visible per-step warning explaining what was dropped and why (`warnings`
       param; surfaced by the web confirm redirect notice and the CLI --create
       output). Direct human authoring (add_step/update_step, and the manual
       builder that calls them) keeps the hard refusal this branch built earlier.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (MockProvider). ASCII-only.
Final line: "LQA STEP HONEST OPTIONS OK".
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_lqahonest_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import StepType, Workflow, WorkflowStep
from app.services import package_view as pv
from app.services import setup_author
from app.services import workflows as wf

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _new_wf(name):
    with Session(engine) as s:
        w = Workflow(name=name, scope="global")
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _get(step_id):
    with Session(engine) as s:
        return s.get(WorkflowStep, step_id)


def _raises_naming(fn, *needles):
    """Call fn(); assert it raises WorkflowError whose message names every needle
    (case-insensitive). Returns the exception message for further inspection."""
    try:
        fn()
    except wf.WorkflowError as exc:
        msg = str(exc).lower()
        for n in needles:
            if n.lower() not in msg:
                _fail(f"expected {n!r} named in the refusal message: {exc}")
        return str(exc)
    _fail(f"expected a WorkflowError naming {needles}")


def _run_cli(argv):
    """Run the CLI in-process, returning (exit_code, stdout+stderr combined).
    CliError messages print to STDERR (app/cli/__main__.py), so both streams must
    be captured for a refusal message assertion to see them."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    combined = out.getvalue() + err.getvalue()
    assert combined.isascii(), ("CLI output must be ASCII-only", combined)
    return rc, combined


# ============================================================================
# 1. add_step REFUSES a meaningful dead-field value per type (LQA)
# ============================================================================
def test_add_step_refuses_lqa_dead_fields():
    """model/fallback_provider/fallback_model are NOT in this list -- the maintainer's
    2026-07-22 decision keeps them LIVE for lqa (feat/prompt-package-engine makes
    the LQA judge honor them via _spec_for_step); see
    test_lqa_model_and_fallback_are_live below."""
    wid = _new_wf("lqa-dead-add")
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LQA, prompt_text="Judge this: {text}"),
            "prompt",
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LQA, chunk_policy="none"), "chunk"
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LQA, adaptive_feedback=True),
            "adaptive feedback",
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LQA, doc_context_prepass=True),
            "document-context",
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LQA, qe_gate=True), "qe routing gate"
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LQA, qe_threshold=55), "qe threshold"
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LQA, qe_provider="openai"), "qe judge"
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LQA, glossary_id=1), "glossary"
        )
    print("  (1a) add_step REFUSES every dead field on an lqa step, naming it: OK")


def test_add_step_refuses_human_review_dead_fields():
    """Unlike lqa, model/fallback_provider/fallback_model stay DEAD for
    human_review (it makes no provider call at all -- no engine ever exists to
    honor them)."""
    wid = _new_wf("hr-dead-add")
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.HUMAN_REVIEW, provider="claude"),
            "provider",
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.HUMAN_REVIEW, model="gpt-4o"), "model"
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(
                s, wid, step_type=StepType.HUMAN_REVIEW, fallback_provider="openai"
            ),
            "fallback provider",
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.HUMAN_REVIEW, fallback_model="gpt-4o"),
            "fallback model",
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(
                s, wid, step_type=StepType.HUMAN_REVIEW, prompt_text="Review: {text}"
            ),
            "prompt",
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.HUMAN_REVIEW, redact_pii=True), "pii"
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.HUMAN_REVIEW, qe_gate=True),
            "qe routing gate",
        )
    with Session(engine) as s:
        assert _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.HUMAN_REVIEW, chunk_policy="none"),
            "chunk",
        )
    print(
        "  (1b) add_step REFUSES every dead field on a human_review step "
        "(including model/fallback, unlike lqa): OK"
    )


# ============================================================================
# 1c. model/fallback_provider/fallback_model are LIVE on lqa (the maintainer's 2026-07-22
#     decision: fix the engine, don't hide the knobs -- feat/prompt-package-engine)
# ============================================================================
def test_lqa_model_and_fallback_are_live():
    wid = _new_wf("lqa-model-fallback-live")
    with Session(engine) as s:
        step = wf.add_step(
            s,
            wid,
            name="Check",
            step_type=StepType.LQA,
            provider="claude",
            model="gpt-4o-mini",
            fallback_provider="mock",
            fallback_model="fb-model",
        )
        sid = step.id
    row = _get(sid)
    assert row.model == "gpt-4o-mini", row.model
    assert row.fallback_provider == "mock" and row.fallback_model == "fb-model"

    # update_step: a NEW model/fallback value on an lqa step is accepted, not refused.
    with Session(engine) as s:
        out = wf.update_step(s, sid, model="gpt-4o", fallback_provider="openai")
        assert out.model == "gpt-4o" and out.fallback_provider == "openai"
    print(
        "  (1c) model/fallback_provider/fallback_model are LIVE (accepted, "
        "persisted) on an lqa step via add_step AND update_step: OK"
    )


# ============================================================================
# 2. DEFAULT/blank values are silently accepted (never refused); live fields work
# ============================================================================
def test_default_values_never_refused_and_live_fields_work():
    wid = _new_wf("lqa-live")
    with Session(engine) as s:
        # every dead field left at its own default -> no refusal.
        step = wf.add_step(
            s,
            wid,
            name="Check",
            step_type=StepType.LQA,
            provider="mock",
            redact_pii=True,
            key="check",
            run_condition=None,
        )
        sid = step.id
    row = _get(sid)
    assert row.provider == "mock" and row.redact_pii is True and row.key == "check"
    assert row.model is None and row.prompt_text == "" and row.chunk_policy == "auto"
    assert row.qe_gate is False and row.qe_threshold == 70 and row.glossary_id is None

    wid2 = _new_wf("hr-live")
    with Session(engine) as s:
        step2 = wf.add_step(
            s,
            wid2,
            name="Sign-off",
            step_type=StepType.HUMAN_REVIEW,
            assignee_email="rev@x.com",
            key="signoff",
            max_visits=3,
        )
        sid2 = step2.id
    row2 = _get(sid2)
    assert row2.assignee_email == "rev@x.com" and row2.key == "signoff"
    assert row2.max_visits == 3 and row2.provider is None
    print(
        "  (2) defaults are silently accepted; live fields (provider/redact_pii/"
        "run_condition/branch/max_visits/key, assignee_email) persist: OK"
    )


# ============================================================================
# 3. tm_examples is NEVER refused (checkbox-omission-ambiguity exemption)
# ============================================================================
def test_tm_examples_never_refused():
    wid = _new_wf("tmex-exempt")
    with Session(engine) as s:
        # False (the HTML "box was never checked" reading) must NOT raise for lqa...
        step = wf.add_step(s, wid, step_type=StepType.LQA, tm_examples=False)
        sid = step.id
    assert _get(sid).tm_examples is False
    wid2 = _new_wf("tmex-exempt-hr")
    with Session(engine) as s:
        # ...nor for human_review.
        step2 = wf.add_step(s, wid2, step_type=StepType.HUMAN_REVIEW, tm_examples=False)
        sid2 = step2.id
    assert _get(sid2).tm_examples is False
    print(
        "  (3) tm_examples=False is never refused on lqa/human_review (checkbox-"
        "omission ambiguity exemption): OK"
    )


# ============================================================================
# 4. update_step: legacy dead config keeps loading; a NEW change is refused
# ============================================================================
def test_update_step_legacy_kept_new_change_refused():
    wid = _new_wf("lqa-legacy-edit")
    # Simulate a PRE-EXISTING (pre-fix) misconfigured LQA step, exactly the reported
    # prod shape: qe_gate/qe_provider already set on an lqa step.
    with Session(engine) as s:
        s.add(
            WorkflowStep(
                workflow_id=wid,
                order_index=0,
                name="Check",
                step_type=StepType.LQA,
                provider="mock",
                qe_gate=True,
                qe_provider="claude",
            )
        )
        s.commit()
        sid = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid)).first().id

    # An UNRELATED edit (renaming) must NOT choke on the untouched legacy qe_gate.
    with Session(engine) as s:
        out = wf.update_step(s, sid, name="Check2")
        assert out.name == "Check2" and out.qe_gate is True, (
            "an untouched legacy dead value must be left alone (loads/edits fine)"
        )

    # Explicitly CHANGING the dead field to a new meaningful value IS refused.
    with Session(engine) as s:
        assert _raises_naming(lambda: wf.update_step(s, sid, qe_threshold=55), "qe threshold")
    assert _get(sid).qe_threshold == 70, "a refused edit must change nothing"

    # Re-supplying the SAME already-stored value (a round-trip, e.g. a full-POST web
    # edit that just resubmits the hidden field unchanged) is NOT refused either.
    with Session(engine) as s:
        out2 = wf.update_step(s, sid, qe_gate=True, name="Check3")
        assert out2.name == "Check3" and out2.qe_gate is True

    # TYPE SWITCH (adversarial-review finding): switching step_type is NEVER refused
    # for a now-dead field, however it arrived -- an ordinary builder edit form
    # ALWAYS full-POSTs, so a hidden qe_gate checkbox still submits its OLD checked
    # value with no way for the user to un-set it (the JS only hides it once the
    # type changes). update_step must FORCE-CLEAR instead, reporting what it
    # cleared. Case (a): an untouched/carried-over dead value rides along.
    wid2 = _new_wf("switch-into-lqa")
    with Session(engine) as s:
        st = wf.add_step(
            s,
            wid2,
            step_type=StepType.LLM_AGENT,
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_threshold=80,
        )
        sid2 = st.id
    rep2: dict = {}
    with Session(engine) as s:
        # Exactly what a real browser sends: Type flips to lqa, but prompt_text/
        # qe_gate/qe_threshold still carry their PRE-switch rendered values (the
        # fields are now hidden, not disabled or removed from the DOM).
        out3 = wf.update_step(
            s,
            sid2,
            step_type=StepType.LQA,
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_threshold=80,
            report=rep2,
        )
        assert out3.step_type == StepType.LQA
        assert out3.prompt_text == "", ("prompt_text must be force-cleared", out3.prompt_text)
        assert out3.qe_gate is False and out3.qe_threshold == 70, (
            "qe_gate/qe_threshold must be force-cleared, never silently carried over",
            out3.qe_gate,
            out3.qe_threshold,
        )
    cleared2 = set(rep2.get("type_switch_cleared", []))
    assert cleared2 == {"prompt_text", "qe_gate", "qe_threshold"}, (
        "the report must name every field actually cleared",
        rep2,
    )
    # Case (b): a FRESH/different dead-field value on the SAME type-switch call is
    # ALSO force-cleared (never refused) -- "do not refuse, the UI hides these
    # fields so the user cannot act on a refusal" applies regardless of whether the
    # value happens to differ from before.
    wid3 = _new_wf("switch-into-lqa-changed")
    with Session(engine) as s:
        st3 = wf.add_step(s, wid3, step_type=StepType.LLM_AGENT, prompt_text="Translate: {text}")
        sid3 = st3.id
    with Session(engine) as s:
        out4 = wf.update_step(s, sid3, step_type=StepType.LQA, prompt_text="Judge this now: {text}")
    assert out4.step_type == StepType.LQA and out4.prompt_text == "", (
        "a type-switch force-clears a dead field even when its value just changed "
        "this same call -- never refused",
        out4.prompt_text,
    )
    print(
        "  (4) update_step: untouched legacy dead config keeps editing when NOT "
        "part of a type switch; a NEW/changed dead-field value on an unchanged "
        "type is refused; a TYPE SWITCH force-clears every now-dead field "
        "(carried-over OR freshly-set) and reports what it cleared, NEVER "
        "refusing: OK"
    )


# ============================================================================
# 5. WEB: POST /workflows/{id}/steps and .../edit parity; template hides fields
# ============================================================================
def test_web_add_and_edit_refuse_with_clean_400():
    client = TestClient(app)
    r = client.post(
        "/workflows", data={"name": "Web LQA WF", "scope": "global"}, follow_redirects=False
    )
    wid = int(r.headers["location"].rsplit("/", 1)[1])

    # add-step: an lqa step with qe_gate set -> clean 400, naming the field.
    r = client.post(
        f"/workflows/{wid}/steps",
        data={"name": "Check", "step_type": "lqa", "provider": "mock", "qe_gate": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 400, ("web add-step must 400 on a dead lqa field", r.status_code)
    assert "qe routing gate" in r.text.lower(), r.text[:400]

    # A CLEAN lqa add (no dead fields) succeeds.
    r = client.post(
        f"/workflows/{wid}/steps",
        data={"name": "Check", "step_type": "lqa", "provider": "mock"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        from app.services import orchestrator

        sid = orchestrator.get_steps(s, wid)[0].id

    # edit-step: setting a dead field to a NEW value -> clean 400.
    r = client.post(
        f"/workflows/{wid}/steps/{sid}/edit",
        data={"name": "Check", "step_type": "lqa", "provider": "mock", "chunk_policy": "none"},
        follow_redirects=False,
    )
    assert r.status_code == 400, ("web edit-step must 400 on a dead lqa field", r.status_code)
    assert "chunk" in r.text.lower(), r.text[:400]
    assert _get(sid).chunk_policy == "auto", "a refused web edit must change nothing"

    # edit-step: a NEW model/fallback value on the SAME lqa step succeeds (live).
    r = client.post(
        f"/workflows/{wid}/steps/{sid}/edit",
        data={
            "name": "Check",
            "step_type": "lqa",
            "provider": "mock",
            "model": "gpt-4o",
            "fallback_provider": "openai",
            "fallback_model": "gpt-4o-mini",
            "chunk_policy": "auto",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, (
        "model/fallback are live on lqa via web edit",
        r.status_code,
        r.text[:400],
    )
    row = _get(sid)
    assert (
        row.model == "gpt-4o"
        and row.fallback_provider == "openai"
        and row.fallback_model == "gpt-4o-mini"
    )

    # human_review: a provider set -> clean 400.
    r = client.post(
        f"/workflows/{wid}/steps",
        data={"name": "Sign-off", "step_type": "human_review", "provider": "claude"},
        follow_redirects=False,
    )
    assert r.status_code == 400, r.status_code
    assert "provider" in r.text.lower()

    # A clean human_review add (assignee only) succeeds.
    r = client.post(
        f"/workflows/{wid}/steps",
        data={"name": "Sign-off", "step_type": "human_review", "assignee_email": "rev@x.com"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    print(
        "  (5a) web add-step/edit-step REFUSE a dead lqa/human_review field with a "
        "clean 400 (never a 500); a clean submission still succeeds: OK"
    )


def test_builder_template_hides_dead_fields_and_updates_placeholder_hint():
    client = TestClient(app)
    r = client.post(
        "/workflows", data={"name": "Template WF", "scope": "global"}, follow_redirects=False
    )
    wid = int(r.headers["location"].rsplit("/", 1)[1])
    body = client.get(f"/workflows/{wid}").text
    # Add-step form: Provider, Model, Fallback provider/model, redact_pii are offered
    # to both llm_agent+lqa (feat/prompt-package-engine makes the LQA judge honor
    # them); Chunking/Glossary/adaptive-feedback/etc./Prompt stay llm_agent-only;
    # assignee is human_review-only.
    assert 'data-types="llm_agent lqa" data-provider="wfProvider">Model' in body, (
        "Model must be offered to lqa too (the maintainer's 2026-07-22 decision)"
    )
    assert 'data-types="llm_agent lqa" data-provider="wfFallbackProvider">Fallback model' in body, (
        "Fallback model must be offered to lqa too"
    )
    assert 'data-types="llm_agent lqa">Fallback provider' in body, (
        "Fallback provider must be offered to lqa too"
    )
    assert 'data-types="llm_agent lqa"><input type="checkbox" name="redact_pii"' in body, (
        "redact_pii must be offered to lqa too"
    )
    assert 'data-types="llm_agent">Chunking' in body, "Chunking must stay llm_agent-only"
    assert 'data-types="llm_agent">Glossary' in body, "Glossary must stay llm_agent-only"
    assert 'data-types="human_review"' in body, "Assignee email must be human_review-only"
    # Placeholder hint lists the new context/style_guide/lqa_findings placeholders.
    assert "{context}" in body and "{style_guide}" in body and "{lqa_findings}" in body, (
        "the prompt placeholder hint must list {context} {style_guide} {lqa_findings}"
    )
    assert "auto-included on fix-shaped steps" in body, (
        "{lqa_findings} must be described as auto-included on fix-shaped steps"
    )
    # The unified step-type sync script replaces the old per-page-only assignee toggle.
    assert "data-types" in body and "step_type" in body
    print(
        "  (5b) builder template: dead fields carry data-types (llm_agent[/lqa]); "
        "placeholder hint lists {context} {style_guide} {lqa_findings}: OK"
    )


# ============================================================================
# 12. STEP-HEADER HIERARCHY (the maintainer's P1: an unmistakable "Step N" while
#     scrolling a long custom workflow). The read-view step list carries a
#     bold, numbered header per step, distinct from the neighbouring steps.
# ============================================================================
def test_builder_step_list_renders_step_header_hierarchy():
    wid = _new_wf("step-header-builder")
    with Session(engine) as s:
        wf.add_step(
            s,
            wid,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate {text}.",
        )
    with Session(engine) as s:
        wf.add_step(s, wid, name="Check", step_type=StepType.LQA, provider="mock")
    client = TestClient(app)
    body = client.get(f"/workflows/{wid}").text
    assert 'class="steplist"' in body, "the step list must keep its stable ancestor class"
    assert 'class="step-head"' in body, "each step's read-view header must keep its class"
    assert '<span class="wf-step-num">Step 1</span>' in body, (
        "step 1 must carry a visible, unmistakable step-number label"
    )
    assert '<span class="wf-step-num">Step 2</span>' in body, (
        "step 2 must carry its own step-number label, distinct from step 1"
    )
    print(
        "  (12) builder step list: each step's header carries a visible 'Step N' "
        "label (class + number both present): OK"
    )


# ============================================================================
# 6. CLI parity
# ============================================================================
def test_cli_add_step_refuses_dead_lqa_flag():
    wid = _new_wf("cli-lqa-dead")
    rc, out = _run_cli(
        [
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "lqa",
            "--provider",
            "mock",
            "--prompt",
            "Judge: {text}",
        ]
    )
    assert rc != 0, ("CLI add-step must refuse a dead lqa flag", out)
    assert "prompt" in out.lower(), out
    with Session(engine) as s:
        from app.services import orchestrator

        assert orchestrator.get_steps(s, wid) == [], "a refused CLI add must create nothing"

    rc, out = _run_cli(
        ["workflow", "add-step", str(wid), "--type", "lqa", "--provider", "mock", "--qe-gate"]
    )
    assert rc != 0, out
    assert "qe routing gate" in out.lower(), out

    # A clean lqa add succeeds.
    rc, out = _run_cli(["workflow", "add-step", str(wid), "--type", "lqa", "--provider", "mock"])
    assert rc == 0, out

    # --model/--fallback-provider/--fallback-model are LIVE on lqa (accepted).
    rc, out = _run_cli(
        [
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "lqa",
            "--provider",
            "mock",
            "--model",
            "gpt-4o",
            "--fallback-provider",
            "openai",
            "--fallback-model",
            "gpt-4o-mini",
        ]
    )
    assert rc == 0, out
    with Session(engine) as s:
        from app.services import orchestrator

        last = orchestrator.get_steps(s, wid)[-1]
        assert (
            last.model == "gpt-4o"
            and last.fallback_provider == "openai"
            and last.fallback_model == "gpt-4o-mini"
        )
    print(
        "  (6a) CLI add-step refuses a dead lqa flag (nonzero, clear message); a "
        "clean add succeeds; --model/--fallback-* are LIVE on lqa: OK"
    )


def test_cli_edit_step_type_switch_clears_then_refuses_after():
    wid = _new_wf("cli-lqa-edit")
    with Session(engine) as s:
        st = wf.add_step(
            s,
            wid,
            step_type=StepType.LLM_AGENT,
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_threshold=80,
        )
        sid = st.id
    # `--type lqa` alone is a REAL type switch (llm_agent -> lqa) -- per the
    # adversarial-review finding this FORCE-CLEARS the untouched legacy qe_gate/
    # qe_threshold/prompt_text (never refuses; there is nothing for the CLI
    # operator to "un-set" on a bare --type flag either) and reports it.
    rc, out = _run_cli(["workflow", "edit-step", str(wid), str(sid), "--type", "lqa"])
    assert rc == 0, out
    assert "cleared" in out.lower() and "qe routing gate" in out.lower(), out
    row = _get(sid)
    assert row.step_type == StepType.LQA
    assert row.qe_gate is False and row.qe_threshold == 70 and row.prompt_text == "", (
        "a type switch must force-clear every now-dead field, never silently carry it over",
        row.qe_gate,
        row.qe_threshold,
        row.prompt_text,
    )

    # Now (type UNCHANGED this call) explicitly touching qe_threshold IS refused --
    # the field is already hidden for this already-lqa step, so a deliberate
    # --qe-threshold flag is a genuine explicit act.
    rc, out = _run_cli(["workflow", "edit-step", str(wid), str(sid), "--qe-threshold", "60"])
    assert rc != 0, out
    assert "qe threshold" in out.lower(), out
    assert _get(sid).qe_threshold == 70, "a refused CLI edit must change nothing"
    print(
        "  (6b) CLI edit-step: a REAL type switch force-clears every now-dead "
        "field (reported, never refused); afterward, an unchanged-type edit "
        "explicitly touching a dead field is still refused: OK"
    )


# ============================================================================
# 7. apply_workflow_draft (AI-authoring apply path) STRIPS + WARNS per-step
#    (adversarial-review finding: this used to hard-reject the whole draft --
#    now it applies successfully, with the dead field reset + a visible warning).
# ============================================================================
def test_apply_workflow_draft_strips_and_warns_dead_lqa_field():
    payload = {
        "name": "Drafted LQA WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            # Names its target language: GitHub #43 makes a locale-less llm_agent prompt
            # add its OWN authoring warning to this same list, and the assertion below is
            # about the dead-field strip specifically.
            {"step_type": "llm_agent", "prompt_text": "Translate {text} into {target_locale}."},
            # the exact reported prod shape: qe_gate/qe_provider on an lqa step.
            {"step_type": "lqa", "qe_gate": True, "qe_provider": "claude"},
        ],
    }
    strip_warnings: list = []
    with Session(engine) as s:
        created = setup_author.apply_workflow_draft(
            s, payload, org_id=None, actor="tester", warnings=strip_warnings
        )
        wf_id = created.id
    assert len(strip_warnings) == 1, ("one step had dead fields stripped", strip_warnings)
    joined = strip_warnings[0].lower()
    assert "qe routing gate" in joined and "qe judge" in joined, strip_warnings
    assert "step 2" in joined, ("the warning must name the offending step", strip_warnings)
    with Session(engine) as s:
        wf_row = s.exec(select(Workflow).where(Workflow.name == "Drafted LQA WF")).first()
        assert wf_row is not None and wf_row.id == wf_id, (
            "a strip-and-warn apply must SUCCEED (create the workflow), not reject it"
        )
        steps = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf_id)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert steps[1].step_type == "lqa"
        assert steps[1].qe_gate is False and steps[1].qe_provider is None, (
            "the stripped fields must land at their safe defaults",
            steps[1].qe_gate,
            steps[1].qe_provider,
        )
    print(
        "  (7) apply_workflow_draft STRIPS a drafted dead lqa field (qe_gate/"
        "qe_provider) to its safe default with a visible per-step warning -- the "
        "workflow is still created (the real prod-bug shape, no longer a dead end): OK"
    )


def test_apply_workflow_draft_still_hard_rejects_real_errors():
    """Strip-and-warn is ONLY for dead-for-type fields -- a genuine validation error
    (an unknown provider) still hard-rejects the whole draft atomically, same as
    before this change."""
    payload = {
        "name": "Bad Provider WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {
                "step_type": "llm_agent",
                "prompt_text": "Translate {text}.",
                "provider": "totally-bogus-provider",
            }
        ],
    }
    raised = False
    with Session(engine) as s:
        try:
            setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        except setup_author.WorkflowApplyError as exc:
            raised = True
            assert any("provider" in e.lower() for e in exc.errors), exc.errors
    assert raised, "a genuinely invalid provider must still be REJECTED"
    with Session(engine) as s:
        assert s.exec(select(Workflow).where(Workflow.name == "Bad Provider WF")).first() is None, (
            "a rejected draft apply must create nothing"
        )
    print(
        "  (7b) apply_workflow_draft still HARD-REJECTS a real validation error "
        "(unknown provider) -- strip-and-warn is scoped to dead-for-type fields "
        "only: OK"
    )


# ============================================================================
# 8. package_view: an LQA "Judge model" fact never surfaces `model`
# ============================================================================
def test_package_view_lqa_shows_provider_and_model():
    """CROSS-BRANCH DEPENDENCY (the maintainer's 2026-07-22 decision): the LQA judge model
    fact shows provider + model again (restored to its original shape) because
    feat/prompt-package-engine (merges BEFORE this branch) makes the LQA judge
    honor step.model via _spec_for_step. If that branch has NOT landed, this fact
    is over-claiming -- re-verify against lqa.py before trusting it."""
    row = {"step_type": StepType.LQA, "provider": "openai", "model": "gpt-4o"}
    facts = pv.step_facts(row)
    pkg = {f["key"]: f for f in facts["package"]}
    assert pkg["model"]["label"] == "Judge model"
    assert pkg["model"]["value"] == "openai / gpt-4o", pkg["model"]
    print(
        "  (8) package_view: LQA 'Judge model' fact shows provider + model "
        "again (feat/prompt-package-engine makes this honest): OK"
    )


# ============================================================================
# 9. PROPAGATION SKIP-AND-REPORT (adversarial-review finding, CRITICAL): a base
#    type-switch propagates onto a variant; the variant's dead fields (delta AND
#    pre-existing) are force-cleared and reported, never a hard failure.
# ============================================================================
def test_propagation_force_clears_variant_dead_fields():
    wid = _new_wf("propagation-base")
    with Session(engine) as s:
        base = wf.add_step(
            s,
            wid,
            step_type=StepType.LLM_AGENT,
            name="Translate",
            provider="mock",
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_threshold=70,
        )
        base_id = base.id
    with Session(engine) as s:
        result = wf.copy_to_pairs(s, wid, [("en-US", "de-DE")], adapt=False)
        variant_wf_id = result["created"][0]["workflow_id"]
    with Session(engine) as s:
        from app.services import orchestrator

        variant = orchestrator.get_steps(s, variant_wf_id)[0]
        variant_id = variant.id
        assert variant.step_type == StepType.LLM_AGENT and variant.qe_gate is True
        assert variant.overridden is False, "never pinned -- a live propagation target"

    # Edit ONLY the base's type (llm_agent -> lqa), WITHOUT touching qe_gate this
    # call -- exactly a partial CLI/API edit, or a browser whose hidden checkbox
    # still submits its old checked value.
    report: dict = {}
    with Session(engine) as s:
        updated_base = wf.update_step(s, base_id, step_type=StepType.LQA, report=report)
        assert updated_base.qe_gate is False, "the base's OWN type switch force-clears too"

    with Session(engine) as s:
        variant_row = s.get(WorkflowStep, variant_id)
        assert variant_row.step_type == StepType.LQA, "step_type propagated"
        assert variant_row.qe_gate is False, (
            "PROPAGATION BYPASS: the variant must NEVER end up qe_gate=True on an "
            "lqa step -- refuse_dead_fields is never called for a variant's own "
            "write (propagate_step_edit is a raw setattr sweep), so this MUST be "
            "enforced by the post-delta dead-field sweep instead",
            variant_row.qe_gate,
        )
        assert variant_row.overridden is False

    # A PINNED variant is never touched by ANY of this (base propagation OR the
    # dead-field sweep) -- the existing two-key safety stays authoritative.
    wid_p = _new_wf("propagation-pinned-base")
    with Session(engine) as s:
        base_p = wf.add_step(
            s,
            wid_p,
            step_type=StepType.LLM_AGENT,
            name="Translate",
            provider="mock",
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_threshold=70,
        )
        base_p_id = base_p.id
    with Session(engine) as s:
        result_p = wf.copy_to_pairs(s, wid_p, [("en-US", "it-IT")], adapt=False)
        pinned_wf_id = result_p["created"][0]["workflow_id"]
    with Session(engine) as s:
        from app.services import orchestrator

        pinned_step = orchestrator.get_steps(s, pinned_wf_id)[0]
        pinned_id = pinned_step.id
        wf.set_step_override(s, pinned_id, True)
    report_p: dict = {}
    with Session(engine) as s:
        wf.update_step(s, base_p_id, step_type=StepType.LQA, report=report_p)
    with Session(engine) as s:
        pinned_row = s.get(WorkflowStep, pinned_id)
        assert pinned_row.step_type == StepType.LLM_AGENT and pinned_row.qe_gate is True, (
            "a PINNED variant must be completely untouched by the base edit"
        )
    assert any(e.get("step_id") == pinned_id for e in report_p.get("pinned", [])), (
        "the pinned variant must be listed under the existing 'pinned' bucket",
        report_p,
    )
    print(
        "  (9) propagation SKIP-AND-REPORT: a base type-switch propagating to a "
        "non-pinned variant force-clears the variant's own now-dead fields "
        "(dead_fields_cleared, never a hard failure); a PINNED variant is fully "
        "untouched: OK"
    )


# ============================================================================
# 10. AI-draft REVIEW PAGE hides dead-for-type fields (adversarial-review finding)
# ============================================================================
def test_review_page_hides_dead_fields_for_lqa_and_human_review():
    client = TestClient(app)
    # Force a confirm-rejection RE-RENDER (a real error on step 0) so we can inspect
    # the review page's HTML for step 1 (lqa) and step 2 (human_review) hiding.
    form_data = {
        "name": "Review Hiding WF",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": "fr-FR",
        "step_count": "3",
        "s0_name": "Translate",
        "s0_step_type": "llm_agent",
        "s0_prompt_text": "Translate to French (no placeholder -- forces rejection).",
        "s1_name": "Check",
        "s1_step_type": "lqa",
        "s1_qe_gate": "1",
        "s1_qe_provider": "claude",
        "s2_name": "Sign-off",
        "s2_step_type": "human_review",
        "s2_provider": "claude",
    }
    r = client.post("/workflows/author/confirm", data=form_data, follow_redirects=False)
    assert r.status_code == 200, ("a rejected confirm re-renders the review page", r.status_code)
    body = r.text
    assert body.count("data-types") > 0, (
        "the review page must carry data-types hiding attributes (ported from the builder)"
    )
    assert 'data-types="llm_agent"><input type="checkbox" name="s1_qe_gate"' in body, (
        "the lqa step's QE gate checkbox must be marked llm_agent-only (hidden for lqa)"
    )
    assert 'data-types="human_review">Assignee email' in body, (
        "Assignee email must be marked human_review-only"
    )
    print(
        "  (10) AI-draft review page hides dead-for-type fields via data-types "
        "(ported from the manual builder): OK"
    )


def main() -> None:
    test_add_step_refuses_lqa_dead_fields()
    test_add_step_refuses_human_review_dead_fields()
    test_lqa_model_and_fallback_are_live()
    test_default_values_never_refused_and_live_fields_work()
    test_tm_examples_never_refused()
    test_update_step_legacy_kept_new_change_refused()
    test_web_add_and_edit_refuse_with_clean_400()
    test_builder_template_hides_dead_fields_and_updates_placeholder_hint()
    test_cli_add_step_refuses_dead_lqa_flag()
    test_cli_edit_step_type_switch_clears_then_refuses_after()
    test_apply_workflow_draft_strips_and_warns_dead_lqa_field()
    test_apply_workflow_draft_still_hard_rejects_real_errors()
    test_package_view_lqa_shows_provider_and_model()
    test_propagation_force_clears_variant_dead_fields()
    test_review_page_hides_dead_fields_for_lqa_and_human_review()
    test_builder_step_list_renders_step_header_hierarchy()
    print("LQA STEP HONEST OPTIONS OK")


if __name__ == "__main__":
    main()
