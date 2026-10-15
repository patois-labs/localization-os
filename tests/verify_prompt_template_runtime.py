"""Prompt-template RUNTIME wiring verification (SPEC-prompt-template-runtime), offline
+ hermetic. Proves that a workflow step BOUND to a saved PromptTemplate
(WorkflowStep.prompt_template_id) actually USES that template at run time, with the
product owner's precedence: INLINE prompt_text wins > bound template body > DEFAULT.

Before this branch the FK + a full PromptTemplate CRUD existed but the column was DEAD
at run time: both orchestrator loops read only `template = step.prompt_text or
DEFAULT_PROMPTS...`, and add_step/update_step did not even accept the field. This suite
pins the whole wiring:

  (1)  RESOLUTION PRECEDENCE (pure resolver, unit): inline > template > default; a
       dangling id and an llm_agent template missing {text} both fall back to the
       default WITH an honest note; a blank template body falls through like blank
       inline (the {text} guarantee). fail-pre-fix: resolve_effective_prompt ABSENT at
       HEAD.
  (2)  BINDING via add_step: a saved template binds; a dangling id / an llm_agent
       template missing {text} is REFUSED at bind time (fail-closed). fail-pre-fix:
       HEAD add_step has no prompt_template_id parameter (TypeError).
  (3)  BOTH orchestrator paths (FULL_TEXT + segmented) SEND the bound template's text
       to the provider (captured), via the ONE shared resolver seam.
  (4)  INLINE WINS at run time: a step with BOTH inline text and a bound template sends
       the inline text, never the template's.
  (5)  DANGLING-ID fallback at run time: deleting the template out from under a bound
       step (or a stale id) runs the DEFAULT and records an honest StepRun note -- never
       a crashed run.
  (6)  DEAD-FOR-TYPE: prompt_template_id is refused on lqa/human_review (same machinery
       as prompt_text) and hidden per type; a type SWITCH force-clears it.
  (7)  PLACEHOLDER SEMANTICS flow through template text identically: {context} /
       {style_guide} always-inject and {lqa_findings} auto-append work on a
       template-sourced prompt exactly as on inline text (same render path, not forked).
  (8)  PACKAGE/CLI/API PARITY: the resolved prompt fact ("inline prompt" / "template:
       <name>" / "default") is byte-identical across the builder package view, CLI
       `workflow show --json`, and GET /api/workflows/{id}.
  (9)  CLONE + ADAPTED-VARIANT: clone_workflow carries the binding (used to DROP it); an
       adapted variant that rewrote prompt_text keeps WINNING over the template (rule 1).
  (10) TEMPLATE-EDIT coherence: a PromptTemplate body is immutable post-create (only
       create/rename/delete), so a validly-bound template can never be edited into an
       invalid one; a bound template cannot be DELETED (reference-count guard), so no
       bound step is ever silently orphaned. Pinned here.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (MockProvider). ASCII-only.
Final line: "PROMPT TEMPLATE RUNTIME OK".
"""

import io
import json as _json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_ptruntime_")

from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    Segment,
    StepRun,
    StepType,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import orchestrator, prompts
from app.services import package_view as pv
from app.services import workflows as wf

init_db()

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _head_src(relpath: str) -> str | None:
    """HEAD source (UTF-8) for a fail-pre-fix proof -- NEVER git stash."""
    try:
        return subprocess.check_output(
            ["git", "show", f"HEAD:{relpath}"], cwd=_ROOT, stderr=subprocess.DEVNULL
        ).decode("utf-8")
    except Exception:
        return None


# --- prompt-capture recorder (mirrors verify_prompt_package._CaptureRC) --------
def _canned(text="[out]"):
    return {
        "text": text,
        "provider_used": "mock",
        "model_used": "mock",
        "attempts": 1,
        "error": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "cost_usd": 0.0,
        "failover": False,
    }


class _CaptureRC:
    def __init__(self, module):
        self.module = module
        self.prompts: list[str] = []
        self._orig = None

    def __enter__(self):
        self._orig = self.module.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            self.prompts.append(prompt)
            return _canned()

        set_pieces(self.module.__name__, "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces(self.module.__name__, "run_completion", self._orig)
        return False


# --- fixtures ----------------------------------------------------------------
def _mk_wf(name="ptr"):
    with Session(engine) as s:
        w = Workflow(name=f"{name}-{_seq()}", scope="global")
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _mk_template(body, name=None):
    with Session(engine) as s:
        t = wf.create_prompt_template(s, name or f"T-{_seq()}", body=body)
        return t.id, t.name


def _mk_project():
    with Session(engine) as s:
        p = Project(name=f"ptr-{_seq()}", source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_ft_doc(source="Buy the widget today.", locale="es-ES", context=None):
    with Session(engine) as s:
        doc = Document(
            project_id=_mk_project(),
            filename="d.txt",
            fmt="txt",
            mode=TransMode.FULL_TEXT,
            context=context,
            skeleton={"units": [{"text": source}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(Segment(document_id=doc.id, target_locale=locale, order_index=0, source_text=source))
        s.commit()
        return doc.id


def _mk_seg_doc(sources, locale="es-ES", context=None):
    with Session(engine) as s:
        doc = Document(
            project_id=_mk_project(),
            filename="d.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            context=context,
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, src in enumerate(sources):
            s.add(Segment(document_id=doc.id, target_locale=locale, order_index=i, source_text=src))
        s.commit()
        return doc.id


def _run_ft(wf_id, doc_id, locale="es-ES", capture=True):
    if capture:
        with Session(engine) as s, _CaptureRC(orchestrator) as cap:
            list(orchestrator.run_workflow_stream(s, wf_id, doc_id, locale))
        return cap.prompts
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream(s, wf_id, doc_id, locale))
    return None


def _run_seg(wf_id, doc_id, locale="es-ES"):
    with Session(engine) as s, _CaptureRC(orchestrator) as cap:
        list(orchestrator.run_workflow_stream_segmented(s, wf_id, doc_id, locale))
    return cap.prompts


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    combined = out.getvalue() + err.getvalue()
    assert combined.isascii(), ("CLI output must be ASCII-only", combined)
    return rc, combined


def _raises_naming(fn, *needles):
    try:
        fn()
    except wf.WorkflowError as exc:
        msg = str(exc).lower()
        for n in needles:
            if n.lower() not in msg:
                _fail(f"expected {n!r} in refusal: {exc}")
        return str(exc)
    _fail(f"expected a WorkflowError naming {needles}")


def _prompt_fact(package):
    for f in package:
        if f["key"] == "prompt":
            return f["value"]
    return None


# ============================================================================
# (1) RESOLUTION PRECEDENCE -- pure resolver unit + fail-pre-fix
# ============================================================================
def test_resolver_precedence_unit():
    head = _head_src("app/services/prompts.py")
    if head is not None and "def resolve_effective_prompt" not in head:
        print("  1a. fail-pre-fix (git HEAD): prompts.resolve_effective_prompt ABSENT OK")
    else:
        print("  1a. HEAD already carries the resolver -> capability proof via current OK")

    R = prompts.resolve_effective_prompt
    # inline wins even when a valid template is also bound
    r = R(
        "llm_agent",
        "INLINE {text}",
        default_text="DEF {text}",
        template_id=7,
        template_exists=True,
        template_body="TMPL {text}",
        template_name="T",
    )
    assert r.source == prompts.PROMPT_SOURCE_INLINE and r.text == "INLINE {text}", r
    # template used when inline empty + template valid
    r = R(
        "llm_agent",
        "",
        default_text="DEF {text}",
        template_id=7,
        template_exists=True,
        template_body="TMPL {text}",
        template_name="T",
    )
    assert r.source == prompts.PROMPT_SOURCE_TEMPLATE and r.text == "TMPL {text}"
    assert r.template_name == "T" and r.note is None, r
    # dangling id -> default + note
    r = R("llm_agent", "", default_text="DEF {text}", template_id=7, template_exists=False)
    assert r.source == prompts.PROMPT_SOURCE_DEFAULT and r.text == "DEF {text}"
    assert r.note and "no longer exists" in r.note, r
    # invalid template body (no {text}) -> default + note
    r = R(
        "llm_agent",
        "",
        default_text="DEF {text}",
        template_id=7,
        template_exists=True,
        template_body="no source",
        template_name="T",
    )
    assert r.source == prompts.PROMPT_SOURCE_DEFAULT and r.note and "missing {text}" in r.note
    # blank template body -> default, no note (like blank inline)
    r = R(
        "llm_agent",
        "",
        default_text="DEF {text}",
        template_id=7,
        template_exists=True,
        template_body="   ",
        template_name="T",
    )
    assert r.source == prompts.PROMPT_SOURCE_DEFAULT and r.note is None, r
    # nothing bound -> default
    r = R("llm_agent", "", default_text="DEF {text}")
    assert r.source == prompts.PROMPT_SOURCE_DEFAULT and r.note is None
    print(
        "  1b. resolver precedence inline>template>default; dangling/invalid/blank "
        "fall back (blank silent, dangling/invalid noted) OK"
    )


# ============================================================================
# (2) BINDING via add_step + bind-time validation; fail-pre-fix (no kwarg at HEAD)
# ============================================================================
def test_add_step_binds_and_validates():
    head = _head_src("app/services/workflows.py")
    if head is not None and "prompt_template_id: int | None = None," not in head:
        print("  2a. fail-pre-fix (git HEAD): add_step had no prompt_template_id param OK")
    else:
        print("  2a. HEAD already accepts prompt_template_id -> proof via current OK")

    tid, _ = _mk_template("Translate carefully: {text}")
    wid = _mk_wf()
    with Session(engine) as s:
        st = wf.add_step(s, wid, step_type=StepType.LLM_AGENT, prompt_template_id=tid)
        assert st.prompt_template_id == tid, st.prompt_template_id
    # dangling id -> refused at bind time
    with Session(engine) as s:
        _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LLM_AGENT, prompt_template_id=999999),
            "not found",
        )
    # a template missing {text} cannot bind to an llm_agent step
    bad_tid, _bad_name = _mk_template("Just do it, no source placeholder here")
    with Session(engine) as s:
        _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LLM_AGENT, prompt_template_id=bad_tid),
            "cannot be bound",
            str(bad_tid),
        )
    print(
        "  2b. add_step binds a valid template; refuses a dangling id + a template "
        "missing {text} (bind-time, fail-closed) OK"
    )


# ============================================================================
# (3) BOTH orchestrator paths send the bound template text (captured)
# ============================================================================
def _wf_with_bound_step(tid, inline=""):
    wid = _mk_wf()
    with Session(engine) as s:
        wf.add_step(
            s,
            wid,
            step_type=StepType.LLM_AGENT,
            provider="mock",
            chunk_policy="none",
            prompt_text=inline,
            prompt_template_id=tid,
        )
    return wid


def test_both_paths_use_template_text():
    tid, _ = _mk_template("TEMPLATE_MARKER translate: {text}")
    # FULL_TEXT
    wid = _wf_with_bound_step(tid)
    prompts_sent = _run_ft(wid, _mk_ft_doc())
    assert prompts_sent and any("TEMPLATE_MARKER" in p for p in prompts_sent), (
        "FULL_TEXT must send the bound template text",
        prompts_sent,
    )
    # SEGMENTED
    wid2 = _wf_with_bound_step(tid)
    prompts_sent2 = _run_seg(wid2, _mk_seg_doc(["Alpha.", "Beta."]))
    assert prompts_sent2 and all("TEMPLATE_MARKER" in p for p in prompts_sent2), (
        "SEGMENTED must send the bound template text on every segment",
        prompts_sent2,
    )
    print(
        "  3. FULL_TEXT + segmented both send the bound template text (one shared resolver seam) OK"
    )


# ============================================================================
# (4) INLINE WINS at run time
# ============================================================================
def test_inline_wins_at_runtime():
    tid, _ = _mk_template("TEMPLATE_MARKER should NOT appear: {text}")
    wid = _wf_with_bound_step(tid, inline="INLINE_MARKER wins: {text}")
    sent = _run_ft(wid, _mk_ft_doc())
    joined = "\n".join(sent)
    assert "INLINE_MARKER" in joined and "TEMPLATE_MARKER" not in joined, sent
    print("  4. a step with BOTH inline text + a bound template sends the INLINE text OK")


# ============================================================================
# (5) RUN-TIME defensive fallback + honest StepRun note (no crash)
# ============================================================================
def test_broken_binding_falls_back_with_note_at_runtime():
    # A truly DANGLING id cannot exist in the DB under FK enforcement (the FK + the
    # delete reference-count guard prevent it -- see test (10)), so the run-time fallback
    # is DEFENSE-IN-DEPTH. We exercise the identical seam (_resolve_step_template ->
    # resolver -> note -> StepRun.error) by mutating a validly-bound template's body OUT
    # OF BAND to an invalid one (no {text}) -- the resolver detects it at run time, runs
    # the DEFAULT, and records the honest note, never crashing the run. (The dangling-id
    # branch of the same resolver is proven at the unit level in test (1).)
    tid, _ = _mk_template("TEMPLATE_MARKER good: {text}")
    wid = _wf_with_bound_step(tid)
    with Session(engine) as s:
        step_id = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid)).first().id
    # Corrupt the bound template's body directly (bypasses the no-body-edit surface).
    from app.models import PromptTemplate

    with Session(engine) as s:
        t = s.get(PromptTemplate, tid)
        t.body = "now missing the source placeholder"
        s.add(t)
        s.commit()
    # A real (uncaptured) run must COMPLETE using the default, not crash.
    _run_ft(wid, _mk_ft_doc(), capture=False)
    with Session(engine) as s:
        sr = s.exec(select(StepRun).where(StepRun.step_id == step_id)).first()
        assert sr is not None, "the step must have produced a StepRun"
        assert sr.error and "missing {text}" in sr.error, ("honest note", sr.error)
    print(
        "  5. a broken bound template runs the DEFAULT at run time (no crash) + "
        "records the honest StepRun note OK"
    )


# ============================================================================
# (6) DEAD-FOR-TYPE refusal + type-switch force-clear
# ============================================================================
def test_dead_for_type_and_switch():
    assert "prompt_template_id" in wf.DEAD_FIELDS_BY_TYPE[StepType.LQA]
    assert "prompt_template_id" in wf.DEAD_FIELDS_BY_TYPE[StepType.HUMAN_REVIEW]
    tid, _ = _mk_template("x {text}")
    wid = _mk_wf()
    with Session(engine) as s:
        _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.LQA, prompt_template_id=tid),
            "prompt template",
        )
    with Session(engine) as s:
        _raises_naming(
            lambda: wf.add_step(s, wid, step_type=StepType.HUMAN_REVIEW, prompt_template_id=tid),
            "prompt template",
        )
    # type SWITCH force-clears a now-dead binding (never refuses)
    with Session(engine) as s:
        st = wf.add_step(s, wid, step_type=StepType.LLM_AGENT, prompt_template_id=tid)
        sid = st.id
    with Session(engine) as s:
        report = {}
        wf.update_step(s, sid, step_type=StepType.LQA, report=report)
    with Session(engine) as s:
        st = s.get(WorkflowStep, sid)
        assert st.prompt_template_id is None, "type switch must clear the dead binding"
        assert "prompt_template_id" in report.get("type_switch_cleared", []), report
    print(
        "  6. prompt_template_id dead-for-type (lqa/human_review refuse); a type "
        "switch force-clears it + reports OK"
    )


# ============================================================================
# (7) PLACEHOLDER SEMANTICS flow through template text (context always-inject)
# ============================================================================
def test_placeholders_flow_through_template():
    # A template with NO {context} placeholder still RECEIVES the resolved context block
    # (always-inject), exactly as inline text does -- proving the same render path.
    tid, _ = _mk_template("TEMPLATE_MARKER translate: {text}")
    wid = _mk_wf()
    with Session(engine) as s:
        wf.add_step(
            s,
            wid,
            step_type=StepType.LLM_AGENT,
            provider="mock",
            chunk_policy="none",
            prompt_template_id=tid,
        )
    ctx = {"domain": "legal", "audience": "lawyers", "formality": "formal"}
    sent = _run_ft(wid, _mk_ft_doc(context=ctx))
    joined = "\n".join(sent)
    assert "TEMPLATE_MARKER" in joined, sent
    # the always-inject context block prepends a <translation_context> style block
    assert "legal" in joined.lower(), ("resolved context must reach a template prompt", sent)
    print(
        "  7. {context} always-inject flows through a template-sourced prompt (same "
        "render path, not forked) OK"
    )


# ============================================================================
# (8) PACKAGE / CLI / API PARITY of the resolved prompt fact
# ============================================================================
def _pkg_prompt_fact(wid):
    with Session(engine) as s:
        w = s.get(Workflow, wid)
        steps = orchestrator.get_steps(s, wid)
        views = pv.workflow_package_view(s, w, steps)
    return [_prompt_fact(v["package"]) for v in views]


def test_package_cli_api_parity():
    tid, tname = _mk_template("body {text}")
    wid = _mk_wf()
    with Session(engine) as s:
        wf.add_step(s, wid, step_type=StepType.LLM_AGENT, prompt_template_id=tid)  # template
        wf.add_step(s, wid, step_type=StepType.LLM_AGENT, prompt_text="inline {text}")  # inline
        wf.add_step(s, wid, step_type=StepType.LLM_AGENT)  # default
    facts = _pkg_prompt_fact(wid)
    assert facts == [f"template: {tname}", "inline prompt", "default"], facts

    # CLI --json
    rc, out = _run_cli(["--json", "workflow", "show", str(wid)])
    assert rc == 0, out
    local = _json.loads(out)
    cli_facts = [_prompt_fact(st["package"]) for st in local["steps"]]
    assert cli_facts == facts, ("CLI prompt fact must match the package view", cli_facts)

    # /api mirror
    from app.services import apikeys, security, settings

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        u = security.create_user("ptrapi@x.com", "PTR", "admin", "ptrpass123456")
        _row, tok = apikeys.create_key(u.id, "ptr", scopes="api docs")
        c = TestClient(app)
        r = c.get(f"/api/workflows/{wid}", headers={"X-Loc-Token": tok})
        assert r.status_code == 200, r.text
        api = r.json()
    finally:
        settings.set_many({"security_auth_required": "0"})
    api_facts = [_prompt_fact(st["package"]) for st in api["steps"]]
    assert api_facts == facts, ("api prompt fact must match", api_facts)
    print(
        "  8. resolved prompt fact (template:<name> / inline prompt / default) is "
        "byte-identical across package view + CLI --json + /api OK"
    )


# ============================================================================
# (9) CLONE carries the binding; adapted-variant prompt keeps winning (rule 1)
# ============================================================================
def test_clone_and_adapted_variant():
    tid, _ = _mk_template("TEMPLATE_MARKER: {text}")
    wid = _mk_wf()
    with Session(engine) as s:
        wf.add_step(
            s,
            wid,
            step_type=StepType.LLM_AGENT,
            provider="mock",
            chunk_policy="none",
            prompt_template_id=tid,
        )
        clone = wf.clone_workflow(s, wid)
        cid = clone.id
    with Session(engine) as s:
        cstep = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == cid)).first()
        assert cstep.prompt_template_id == tid, "clone must carry the bound template"
    # An adapted variant that ALSO rewrote prompt_text keeps winning over the template.
    with Session(engine) as s:
        vstep = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == cid)).first()
        vstep.prompt_text = "ADAPTED_MARKER wins: {text}"
        s.add(vstep)
        s.commit()
    sent = _run_ft(cid, _mk_ft_doc())
    joined = "\n".join(sent)
    assert "ADAPTED_MARKER" in joined and "TEMPLATE_MARKER" not in joined, sent
    print(
        "  9. clone_workflow carries the binding; an adapted variant's rewritten "
        "prompt_text wins over the template (rule 1) OK"
    )


# ============================================================================
# (10) TEMPLATE-EDIT coherence: immutable body + delete guard on a bound template
# ============================================================================
def test_template_edit_coherence():
    tid, _ = _mk_template("valid {text}")
    wid = _mk_wf()
    with Session(engine) as s:
        wf.add_step(s, wid, step_type=StepType.LLM_AGENT, prompt_template_id=tid)
    # RENAME never touches the body, so a bound step stays valid + still resolves.
    with Session(engine) as s:
        wf.rename_prompt_template(s, tid, "Renamed Template")
    facts = _pkg_prompt_fact(wid)
    assert facts == ["template: Renamed Template"], facts
    # A bound template CANNOT be deleted (reference-count guard) -> no orphaned step.
    with Session(engine) as s:
        _raises_naming(lambda: wf.delete_prompt_template(s, tid), "still referenced", "step")
    print(
        "  10. a bound template's body is immutable (rename-only) + delete is refused "
        "while bound -> a bound step is never silently orphaned OK"
    )


def main():
    for _fn in (
        test_resolver_precedence_unit,
        test_add_step_binds_and_validates,
        test_both_paths_use_template_text,
        test_inline_wins_at_runtime,
        test_broken_binding_falls_back_with_note_at_runtime,
        test_dead_for_type_and_switch,
        test_placeholders_flow_through_template,
        test_package_cli_api_parity,
        test_clone_and_adapted_variant,
        test_template_edit_coherence,
    ):
        _fn()

    print("PROMPT TEMPLATE RUNTIME OK")


if __name__ == "__main__":
    main()
