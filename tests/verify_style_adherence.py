"""Style-guide adherence verification (SPEC-workflows-redesign Track B, B3).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> no network,
MockProvider forced). ZERO live calls. Closes the inject-AND-verify loop B2 opened: an
LQA step now JUDGES whether the output followed the resolved style guide, and a major
violation drives the existing correction loop. Plus the style-guide A/B harness.

  1. NO_STYLE_GUIDE rulebook directive (lqa.style_guide_suppressed): the zero-schema
     per-pair opt-out; parsing edge cases; FORBID/REQUIRE unaffected.
  2. _style_clause: "" for no block (the byte-identity primitive); the adherence
     instruction + the fenced block when present.
  3. Captured-prompt BYTE-IDENTITY: with no guide, the LQA judge prompt is byte-identical
     to HEAD's pre-B3 _llm_errors (git-show reconstruction, fail-before/pass-after); with
     a guide, the prompt carries the guide body + the "Style" instruction; NO_STYLE_GUIDE
     suppresses it back to byte-identical.
  4. Merge + gate: a judge "Style" error merges into the SAME card["errors"] -> the SAME
     score/gate; a MAJOR style violation FAILS the gate (a compliant guide-present run
     passes; a minor dents the score but can still pass).
  5. Soft-degrade: a not-live / dead-engine provider contributes NOTHING from the judge
     (structural + rulebook checks intact) — exactly today's _llm_errors posture.
  6. Orchestrator injection: a workflow LQA step feeds the RESOLVED guide into the judge
     prompt (captured) — and "" when none resolves.
  7. The correction loop end-to-end: translate -> LQA(style violation, guide resolves) ->
     fix{run_condition prev_lqa:failed} RUNS the fix; the control (NO guide -> LQA passes)
     SKIPS it. The guide's presence is what flips the gate (asserted via the LQAResult).
  8. Style-guide A/B harness (style_ab.run_ab): runs the same segments guide-ON vs -OFF,
     scored by the QE judge, reports QE + style-error deltas, writes NO target/TM; the
     no-guide case is a clean "nothing to A/B".

Final line: "STYLE ADHERENCE OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_styleadh_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    LQAResult,
    Organization,
    Project,
    RunStatus,
    Segment,
    StepType,
    StyleGuide,
    StyleGuideStatus,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import lqa, orchestrator, style_ab
from app.services import style_guides as sg

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- canned judge return + prompt-capture mock -------------------------------
def _canned(text):
    return {
        "text": text,
        "provider_used": "mock",
        "model_used": "mock",
        "attempts": 1,
        "error": None,
        "usage": {},
    }


_STYLE_MAJOR = json.dumps(
    {
        "errors": [
            {
                "dimension": "Style",
                "severity": "major",
                "note": "register is too casual for the style guide",
            }
        ]
    }
)
_NO_ERRORS = json.dumps({"errors": []})


class _JudgeMock:
    """Make the LQA LLM judge RUN offline + deterministically, and capture its prompt.

    Patches lqa.provider_is_live -> True (so _llm_errors doesn't short-circuit) and
    lqa.run_completion -> a recorder. Its canned reply is chosen by `mode`:
      * 'guide_keyed' (default): a MAJOR Style error IFF the judge prompt carries a
        `<style_guide` block (i.e. an APPROVED guide resolved) — else no errors. This
        isolates the GUIDE'S PRESENCE as the sole cause of the gate flip (the B1 pattern,
        applied to style).
      * 'always_major' / 'always_clean' / 'minor': fixed replies for gate-mechanics tests.
    Captured prompts land in `.prompts`."""

    def __init__(self, mode="guide_keyed", wrap_prose=False):
        # wrap_prose: emulate meta-llama-3-8b, which prefixes its JSON with a prose
        # preamble / ```json fence (QA-2). With the tolerant extractor (llm_json.loads)
        # the wrapped finding must still land; without it the judge silently no-op'd.
        self.mode, self.prompts, self.wrap_prose = mode, [], wrap_prose
        self._orig_live = None
        self._orig_rc = None

    def _reply(self, prompt):
        if self.mode == "always_major":
            return _STYLE_MAJOR
        if self.mode == "always_clean":
            return _NO_ERRORS
        if self.mode == "minor":
            return json.dumps(
                {
                    "errors": [
                        {
                            "dimension": "Style",
                            "severity": "minor",
                            "note": "a mild tone drift from the guide",
                        }
                    ]
                }
            )
        # guide_keyed
        return _STYLE_MAJOR if "<style_guide" in prompt else _NO_ERRORS

    def __enter__(self):
        self._orig_live = lqa.provider_is_live
        self._orig_rc = lqa.run_completion

        def fake_live(name):
            return True

        def fake_rc(spec, prompt, system=None, task=None, payload=None, **kw):
            self.prompts.append(prompt)
            reply = self._reply(prompt)
            if self.wrap_prose:
                # A prose preamble + ```json fence around the valid JSON — the exact
                # shape the reference local model emits (QA-2). llm_json.loads must see
                # through it; a bare json.loads would drop the finding.
                reply = f"Here is the JSON output:\n\n```json\n{reply}\n```\n\nDone."
            return _canned(reply)

        lqa.provider_is_live = fake_live
        lqa.run_completion = fake_rc
        return self

    def __exit__(self, *exc):
        lqa.provider_is_live = self._orig_live
        lqa.run_completion = self._orig_rc
        return False


# --- fixture builders --------------------------------------------------------
def _mk_org(oid):
    with Session(engine) as s:
        if s.get(Organization, oid) is None:
            s.add(Organization(id=oid, name=f"SA Org {oid}"))
            s.commit()
    return oid


def _mk_customer(org_id):
    _mk_org(org_id)
    with Session(engine) as s:
        c = Customer(name="SAC %d" % _seq(), org_id=org_id)  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_guide(org_id, customer_id, body="VOICE: use the FORMAL register. No slang.", approved=True):
    from datetime import datetime

    _mk_org(org_id)
    now = datetime.utcnow()
    with Session(engine) as s:
        g = StyleGuide(
            name="SA Guide %d" % _seq(),  # noqa: UP031
            status=(StyleGuideStatus.APPROVED if approved else StyleGuideStatus.DRAFT),
            org_id=org_id,
            customer_id=customer_id,
            content_type_id=None,
            source_locale=None,
            target_locale=None,
            body=body,
            created_at=now,
            updated_at=now,
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        return g.id


def _mk_project(org_id=None, customer_id=None):
    with Session(engine) as s:
        p = Project(
            name="sa-proj-%d" % _seq(),  # noqa: UP031
            org_id=org_id,
            source_locale="en-US",
            default_provider="mock",
            customer_id=customer_id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_doc(project_id, sources, locale="es-ES"):
    with Session(engine) as s:
        doc = Document(project_id=project_id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, src in enumerate(sources):
            s.add(Segment(document_id=doc.id, target_locale=locale, order_index=i, source_text=src))
        s.commit()
        return doc.id


def _mk_fix_loop_wf(name):
    """translate(mock) -> LQA(mock) -> fix{prev_lqa failed}."""
    guard = {"ref": "prev_lqa", "op": "failed", "value": None}
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="Translator",
                step_type=StepType.LLM_AGENT,
                provider="mock",
                prompt_text="Translate: {text}",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=1,
                name="LQA",
                step_type=StepType.LQA,
                provider="mock",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=2,
                name="Fix",
                step_type=StepType.LLM_AGENT,
                provider="mock",
                prompt_text="Fix: {text}",
                chunk_policy="none",
                retry_max=1,
                backoff_base=0.0,
                run_condition=guard,
            )
        )
        s.commit()
        return wf.id


def _stepruns(run_id):
    with Session(engine) as s:
        return s.exec(
            select(orchestrator.StepRun)
            .where(orchestrator.StepRun.workflow_run_id == run_id)
            .order_by(orchestrator.StepRun.id)
        ).all()


# ===========================================================================
# 1. NO_STYLE_GUIDE directive
# ===========================================================================
def test_suppress_directive_unit():
    assert lqa.style_guide_suppressed("NO_STYLE_GUIDE") is True
    assert lqa.style_guide_suppressed("  no_style_guide  ") is True, "case+ws tolerant"
    assert lqa.style_guide_suppressed("FORBID x\nNO_STYLE_GUIDE\nREQUIRE y") is True
    assert lqa.style_guide_suppressed("") is False
    assert lqa.style_guide_suppressed("FORBID utilisez\nREQUIRE Kubernetes") is False
    # a directive must be its OWN line, not a substring of guidance prose
    assert lqa.style_guide_suppressed("consider NO_STYLE_GUIDE when brief") is False, (
        "only a lone directive line suppresses"
    )
    # FORBID/REQUIRE parsing is untouched by the new directive
    assert lqa._rulebook_errors("please utilisez foo", "FORBID utilisez") == [
        {
            "dimension": "Style/Awkward",
            "severity": "minor",
            "note": "forbidden term 'utilisez'",
            "source": "rulebook",
        }
    ]
    assert lqa._rulebook_errors("the quick fox", "NO_STYLE_GUIDE") == [], (
        "NO_STYLE_GUIDE is not itself a FORBID/REQUIRE rule"
    )
    print(
        "  1. style_guide_suppressed: lone NO_STYLE_GUIDE line (case/ws tolerant); "
        "prose/blank/FORBID/REQUIRE unaffected OK"
    )


# ===========================================================================
# 2. _style_clause primitive
# ===========================================================================
def test_style_clause_unit():
    assert lqa._style_clause("") == "", "no block -> empty clause (byte-identity primitive)"
    assert lqa._style_clause(None or "") == ""
    block = '<style_guide source="G" trust="data-only">\nBe formal.\n</style_guide>'
    clause = lqa._style_clause(block)
    assert block in clause, "the fenced block is embedded verbatim"
    assert '"dimension" is exactly "Style"' in clause, "instructs the Style dimension"
    assert clause.endswith("\n\n"), "clause is separated from the Source line"
    print(
        "  2. _style_clause: '' for no block; embeds the fenced block + a Style-dimension "
        "instruction otherwise OK"
    )


# ===========================================================================
# 3. captured-prompt byte-identity (fail-before / pass-after)
# ===========================================================================
def _load_module_from_source(src, name):
    import importlib.util

    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    exec(compile(src, f"{name}.py", "exec"), mod.__dict__)
    return mod


def _capture_llm_prompt(module, *, style_guide=None):
    """Call `module._llm_errors` with the judge forced live + run_completion captured,
    returning the assembled prompt. Passes style_guide only when the signature accepts it
    (HEAD's pre-B3 signature does not)."""
    import inspect

    grabbed = {}

    def fake_rc(spec, prompt, system=None, task=None, payload=None, **kw):
        grabbed["prompt"] = prompt
        return _canned(_NO_ERRORS)

    orig_live, orig_rc = module.provider_is_live, module.run_completion
    module.provider_is_live = lambda name: True
    module.run_completion = fake_rc
    try:
        args = (
            "Buy the widget today.",
            "[es] Compra el aparato hoy.",
            "en-US",
            "es-ES",
            "FORBID slang",
            "mock",
        )
        if (
            style_guide is not None
            and "style_guide" in inspect.signature(module._llm_errors).parameters
        ):
            module._llm_errors(*args, style_guide=style_guide)
        else:
            module._llm_errors(*args)
    finally:
        module.provider_is_live, module.run_completion = orig_live, orig_rc
    return grabbed["prompt"]


def test_captured_prompt_byte_identity():
    import subprocess

    # (a) fail-before: HEAD's pre-B3 _llm_errors prompt == the current no-guide prompt.
    old_src = None
    try:
        old_src = subprocess.check_output(
            ["git", "show", "HEAD:app/services/lqa.py"],
            text=True,
            cwd=_ROOT,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        old_src = None

    p_new_noguide = _capture_llm_prompt(lqa, style_guide="")
    if old_src is not None and "style_guide" not in old_src:
        oldl = _load_module_from_source(old_src, "old_lqa_b3")
        p_head = _capture_llm_prompt(oldl)
        assert p_new_noguide == p_head, (
            "no-guide judge prompt must be BYTE-IDENTICAL to HEAD's pre-B3 prompt",
            repr(p_new_noguide),
            repr(p_head),
        )
        print(
            "  3a. byte-identity (git HEAD reconstruction): no-guide LQA prompt == pre-B3 prompt OK"
        )
    else:
        # HEAD already carries B3 (post-commit): the always-valid splice proof stands in.
        block = sg.render_style_guide(_G("Be formal."))
        p_guide = _capture_llm_prompt(lqa, style_guide=block)
        assert p_guide.replace(lqa._style_clause(block), "") == p_new_noguide, (
            "the ONLY delta between guide/no-guide prompts is the spliced style clause"
        )
        print(
            "  3a. byte-identity: HEAD carries B3 -> splice-delta proof (guide prompt "
            "minus the clause == no-guide prompt) OK"
        )

    # (b) pass-after: a resolved guide's body + the Style instruction DO reach the prompt.
    block = sg.render_style_guide(_G("VOICE: use the FORMAL register."))
    p_guide = _capture_llm_prompt(lqa, style_guide=block)
    assert "<style_guide" in p_guide and "VOICE: use the FORMAL register." in p_guide, (
        "guide body reaches the judge prompt",
        p_guide,
    )
    assert '"dimension" is exactly "Style"' in p_guide, "Style-dimension instruction present"
    assert "style_guide" not in p_new_noguide, ("no-guide prompt carries no guide", p_new_noguide)
    # the clause sits AFTER the rulebook and BEFORE the Source line (the documented slot)
    assert p_guide.index("</style_guide>") < p_guide.index("Source:"), (
        "guide precedes the Source/Translation block"
    )
    print(
        "  3b. pass-after: a resolved guide's body + Style instruction reach the judge "
        "prompt, seated before Source OK"
    )


class _G:
    """A minimal guide-like object for render_style_guide (name/body/pair)."""

    def __init__(self, body, name="G"):
        self.body, self.name = body, name
        self.source_locale, self.target_locale = None, None


# ===========================================================================
# 4. merge + gate mechanics
# ===========================================================================
def test_merge_and_gate():
    block = sg.render_style_guide(_G("Be formal."))
    S, T = "Hey there!", "[es] Que onda!"

    # guide present + judge finds a MAJOR Style violation -> merged error FAILS the gate
    with _JudgeMock("always_major"):
        card = lqa.score_text(S, T, "en-US", "es-ES", "", "mock", style_guide=block)
    styles = [e for e in card["errors"] if e["dimension"] == "Style"]
    assert styles and styles[0]["severity"] == "major", ("style error merged", card["errors"])
    assert card["passed"] is False and card["gate"] == "needs_review", (
        "a MAJOR style violation fails the gate",
        card,
    )
    print("  4a. gate: a MAJOR judge Style error merges into card['errors'] -> gate FAILED OK")

    # guide present + judge finds NO violation -> guide-present run PASSES (no spurious fail)
    with _JudgeMock("always_clean"):
        ok = lqa.score_text("Hello.", "[es] Hola.", "en-US", "es-ES", "", "mock", style_guide=block)
    assert ok["passed"] is True and ok["gate"] == "auto_approved", (
        "a compliant guide-present run passes",
        ok,
    )
    assert not any(e["dimension"] == "Style" for e in ok["errors"]), ok["errors"]
    print("  4b. gate: guide present + judge clean -> PASSES (no spurious style failure) OK")

    # a MINOR style violation dents the score but (alone) still clears the >=90 gate
    with _JudgeMock("minor"):
        minor = lqa.score_text(
            "Hello.", "[es] Hola.", "en-US", "es-ES", "", "mock", style_guide=block
        )
    assert minor["score"] == 99 and minor["passed"] is True, (
        "a lone minor: 100-1=99, no major -> still passes",
        minor,
    )
    assert any(e["severity"] == "minor" and e["dimension"] == "Style" for e in minor["errors"]), (
        minor["errors"]
    )
    print(
        "  4c. gate: a lone MINOR style error scores 99 and still passes (gate needs a "
        "major/critical to fail) OK"
    )


# ===========================================================================
# 5. soft-degrade + NO_STYLE_GUIDE suppression
# ===========================================================================
def test_soft_degrade():
    block = sg.render_style_guide(_G("Be formal."))
    # provider NOT live (offline default) -> the judge contributes NOTHING; only the
    # deterministic structural + rulebook checks remain. An untranslated echo is a
    # structural MAJOR, proving the non-LLM checks are intact regardless of the guide.
    card = lqa.score_text(
        "Hello there.", "Hello there.", "en-US", "es-ES", "FORBID slang", "mock", style_guide=block
    )
    assert not any(
        e["dimension"] == "Style" and "casual" in e.get("note", "") for e in card["errors"]
    ), "no LLM style error offline"
    assert any(e["dimension"].startswith("Accuracy/Untranslated") for e in card["errors"]), (
        "structural check intact offline",
        card["errors"],
    )
    print(
        "  5a. soft-degrade: not-live provider -> judge adds nothing; structural + "
        "rulebook checks intact OK"
    )

    # NO_STYLE_GUIDE directive: even with the judge LIVE and a guide supplied, the block
    # is suppressed -> the prompt is byte-identical to a no-guide run (no Style clause).
    with _JudgeMock("guide_keyed") as jm:
        card = lqa.score_text(
            "Hello.", "[es] Hola.", "en-US", "es-ES", "NO_STYLE_GUIDE", "mock", style_guide=block
        )
    assert jm.prompts and "<style_guide" not in jm.prompts[-1], (
        "NO_STYLE_GUIDE suppresses guide injection into the judge prompt",
        jm.prompts[-1],
    )
    assert not any(e["dimension"] == "Style" for e in card["errors"]), (
        "suppressed -> no style errors",
        card["errors"],
    )
    print(
        "  5b. NO_STYLE_GUIDE: guide suppressed from the judge prompt (byte-identical) "
        "even when a guide resolves + judge is live OK"
    )


# ===========================================================================
# 6. orchestrator injection — the resolved guide reaches the workflow LQA judge
# ===========================================================================
def test_orchestrator_injects_guide():
    org = 8801
    cust = _mk_customer(org)
    _mk_guide(org, cust, body="ORCH-VOICE: keep it FORMAL.")
    pid = _mk_project(org_id=org, customer_id=cust)
    doc = _mk_doc(pid, ["Buy the widget today."])
    wf = _mk_fix_loop_wf("B3 inject")
    with _JudgeMock("guide_keyed") as jm, Session(engine) as s:
        orchestrator.run_workflow(s, wf, doc, "es-ES")
    assert jm.prompts, "the LQA judge ran during the workflow"
    assert any("ORCH-VOICE: keep it FORMAL." in p for p in jm.prompts), (
        "the RESOLVED guide body reached the workflow LQA judge prompt",
        jm.prompts,
    )
    print(
        "  6. orchestrator: a workflow LQA step feeds the RESOLVED guide into the judge prompt OK"
    )


# ===========================================================================
# 7. the correction loop end-to-end (guide presence flips the gate)
# ===========================================================================
def test_fix_loop_e2e():
    # FAIL arm: an APPROVED guide resolves -> the judge (guide_keyed) returns a MAJOR
    # Style violation -> LQA gate FAILS -> the Fix step RUNS.
    org = 8802
    cust = _mk_customer(org)
    _mk_guide(org, cust, body="Use the FORMAL register only.")
    pid = _mk_project(org_id=org, customer_id=cust)
    doc = _mk_doc(pid, ["Buy the widget today.", "Ship it now."])
    wf = _mk_fix_loop_wf("B3 fix-loop FAIL")
    with _JudgeMock("guide_keyed"), Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf, doc, "es-ES")
    assert summary["status"] == "completed", summary
    srs = _stepruns(summary["run_id"])
    assert srs[1].gate == "failed", (
        "precondition: style violation FAILS the LQA gate",
        srs[1].gate,
    )
    assert srs[2].status == RunStatus.COMPLETED, (
        "Fix must RUN when the LQA failed on the style violation",
        [sr.status for sr in srs],
    )
    with Session(engine) as s:
        notes = [
            r.note for r in s.exec(select(LQAResult).where(LQAResult.document_id == doc)).all()
        ]
    assert any("style guide" in n for n in notes), (
        "causation: the style-adherence judge drove the fail",
        notes,
    )
    print("  7a. guide resolves -> LQA style gate FAILED -> Fix step RUNS (correction loop) OK")

    # CONTROL arm: NO approved guide -> the judge sees no <style_guide> block -> returns
    # NO errors -> LQA PASSES -> the Fix step is SKIPPED. The ONLY difference from the
    # FAIL arm is the guide's presence, so the guide is what flipped the gate.
    org2 = 8803
    cust2 = _mk_customer(org2)  # no guide created for this org/customer
    pid2 = _mk_project(org_id=org2, customer_id=cust2)
    doc2 = _mk_doc(pid2, ["Buy the widget today.", "Ship it now."])
    wf2 = _mk_fix_loop_wf("B3 fix-loop CONTROL")
    with _JudgeMock("guide_keyed"), Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf2, doc2, "es-ES")
    assert summary["status"] == "completed", summary
    srs = _stepruns(summary["run_id"])
    assert srs[1].gate == "passed", ("control: no guide -> LQA passes", srs[1].gate)
    assert srs[2].status == RunStatus.SKIPPED, (
        "Fix must be SKIPPED when the LQA passed",
        [sr.status for sr in srs],
    )
    print(
        "  7b. no guide -> LQA style gate PASSED -> Fix step SKIPPED (control; guide "
        "presence is the sole cause) OK"
    )


# ===========================================================================
# 7b. QA-2: the correction loop end-to-end when the judge WRAPS its JSON in prose
#     (meta-llama-3-8b behavior). Same fixtures as 7a, but the judge's MAJOR Style
#     finding arrives inside a "Here is the JSON:\n\n```json\n{…}\n```" wrapper. With
#     the tolerant extractor the finding still lands -> LQA gate FAILS -> Fix RUNS.
#     Pre-fix (bare json.loads) the wrapper was dropped -> gate PASSED -> Fix SKIPPED,
#     so this test fails against the unfixed tree (the regression).
# ===========================================================================
def test_fix_loop_e2e_prose_wrapped():
    org = 8806
    cust = _mk_customer(org)
    _mk_guide(org, cust, body="Use the FORMAL register only.")
    pid = _mk_project(org_id=org, customer_id=cust)
    doc = _mk_doc(pid, ["Buy the widget today.", "Ship it now."])
    wf = _mk_fix_loop_wf("B3 fix-loop PROSE")
    with _JudgeMock("guide_keyed", wrap_prose=True), Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf, doc, "es-ES")
    assert summary["status"] == "completed", summary
    srs = _stepruns(summary["run_id"])
    assert srs[1].gate == "failed", (
        "a PROSE-WRAPPED style violation must still FAIL the LQA gate (QA-2)",
        srs[1].gate,
    )
    assert srs[2].status == RunStatus.COMPLETED, (
        "Fix must RUN when the prose-wrapped LQA style finding fails the gate",
        [sr.status for sr in srs],
    )
    with Session(engine) as s:
        notes = [
            r.note for r in s.exec(select(LQAResult).where(LQAResult.document_id == doc)).all()
        ]
    assert any("style guide" in n for n in notes), (
        "the prose-wrapped style-adherence judge drove the fail",
        notes,
    )
    print(
        "  7c. QA-2: a PROSE/fence-WRAPPED judge Style finding still lands -> LQA gate "
        "FAILED -> Fix step RUNS (the silent-no-op is closed) OK"
    )


# ===========================================================================
# 8. style-guide A/B harness
# ===========================================================================
def _seg_targets(doc_id):
    with Session(engine) as s:
        return [
            seg.target_text
            for seg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        ]


def test_style_ab_harness():
    org = 8804
    cust = _mk_customer(org)
    gid = _mk_guide(org, cust, body="Use the FORMAL register only.")
    pid = _mk_project(org_id=org, customer_id=cust)
    doc = _mk_doc(pid, ["Buy the widget today.", "Ship it now.", "Contact support."])

    with Session(engine) as s:
        d = s.get(Document, doc)
        res = style_ab.run_ab(s, d, locale="es-ES", org_id=org)
    data = res.as_dict()

    assert data["guide_present"] is True and data["guide_id"] == gid, (
        "the approved guide resolved for the A/B",
        data,
    )
    assert data["is_mock"] is True, "offline install -> a structural (mock) measurement"
    assert data["n"] == 3 and data["n_available"] == 3, data
    # the QE judge runs offline (MockProvider) so both arms are SCORED, not skipped
    assert data["on"]["scored"] == 3 and data["off"]["scored"] == 3, (
        "both arms scored by the offline QE judge",
        data,
    )
    # Mock is style-blind -> both arms identical -> ~0 deltas (structural test, honest)
    assert data["delta"]["mean_qe"] == 0.0, ("mock delta ~0 by construction", data)
    assert data["on"]["mean_style_errors"] == 0.0, data
    assert "structural test" in data["verdict"], ("honest mock verdict", data["verdict"])
    assert "<style_guide" in data["guide_block"], "the guide block is carried in the result"

    # SAFETY RAIL: the measurement wrote NO targets (a pure read + in-memory score) —
    # every segment's target_text stays empty/unset (never a produced translation).
    assert all(not (t or "") for t in _seg_targets(doc)), (
        "style_ab must NOT write Segment.target_text",
        _seg_targets(doc),
    )
    print(
        "  8a. style_ab: guide-ON vs -OFF over 3 segments, both scored, ~0 mock delta, "
        "NO targets written OK"
    )

    # no-guide document -> a clean "nothing to A/B"
    org2 = 8805
    cust2 = _mk_customer(org2)
    pid2 = _mk_project(org_id=org2, customer_id=cust2)
    doc2 = _mk_doc(pid2, ["Buy the widget today."])
    with Session(engine) as s:
        d2 = s.get(Document, doc2)
        res2 = style_ab.run_ab(s, d2, locale="es-ES", org_id=org2)
    data2 = res2.as_dict()
    assert data2["guide_present"] is False, data2
    assert "nothing to A/B" in data2["verdict"], data2["verdict"]
    print("  8b. style_ab: no approved guide -> guide_present False + 'nothing to A/B' verdict OK")


def main():
    test_suppress_directive_unit()
    test_style_clause_unit()
    test_captured_prompt_byte_identity()
    test_merge_and_gate()
    test_soft_degrade()
    test_orchestrator_injects_guide()
    test_fix_loop_e2e()
    test_fix_loop_e2e_prose_wrapped()
    test_style_ab_harness()
    print("STYLE ADHERENCE OK")


if __name__ == "__main__":
    main()
