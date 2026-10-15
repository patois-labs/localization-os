"""Workflow EDIT-STEP verification (feat/workflow-edit-step).

The maintainer's ask: "once a step is created there's no way to edit it — you can only remove it
and recreate it. Add the ability to EDIT a workflow step, at the very least edit the
prompt." This suite proves the new `wf.update_step` service seam + its web route (POST
/workflows/{id}/steps/{step_id}/edit) + its CLI (`workflow edit-step`) across the bar:

  (1)  EDIT PROMPT PERSISTS (the must-have): a prompt edit is written; order_index +
       every other field are untouched.
  (2)  PARTIAL UPDATE: only supplied fields change; unset leaves the rest byte-identical.
  (3)  FULL FIELD SET round-trips (every add_step field editable).
  (4)  ORDER_INDEX preserved when editing a middle step of a 3-step workflow.
  (5)  GRAPH INTEGRITY (fail-closed, changing NOTHING):
        (a) renaming/clearing a key a DOWNSTREAM branch targets is REFUSED (names the dep);
        (b) turning off a qe_gate a downstream prev_qe guard depends on is REFUSED (D3);
            and changing an LQA step's type out from under its OWN branch is REFUSED (D3);
        (c) a run_condition referencing no earlier producing step is REFUSED;
        (d) a valid PROMPT-ONLY edit on a step WITH downstream dependents SUCCEEDS;
        (e) key uniqueness EXCLUDING self (dup rejected; re-setting own key accepted).
  (6)  RUN-SAFETY: an edit is REFUSED while a pending/running/paused run exists; a
       COMPLETED (terminal) run does NOT block.
  (7)  WEB: the builder pre-fills a per-step edit form with current values; a POST round-
       trips; a graph-breaking POST renders a clean 400 (never a 500).
  (8)  CLI: `workflow edit-step` is PARTIAL (only passed flags change; tri-state bools);
       a graph-breaking edit exits nonzero; no-flags is a usage error; ASCII-only.
  (9)  BYTE-STABILITY: a step that is never edited is unchanged when a sibling is edited.
  (10) ENGINE-PIN SURVIVAL (adversarial-review finding, data loss): a step pinned to
       engine:<id> that's since been DISABLED has no matching <option> in the edit
       form's provider/fallback_provider/qe_provider pickers, so a real browser submits
       "" for that field on ANY edit (even an unrelated prompt fix). A web POST that
       mirrors exactly that ("" provider, changed prompt) must leave the engine pin
       INTACT while the prompt edit still applies; a companion case proves a NON-engine
       provider (e.g. "mock") can still be genuinely cleared to default via "".
  (11) EDIT-FORM MODEL PICKER PARITY (this change): the per-step edit form's Model and
       Fallback model fields use the SAME .wfmodel select+manual+fetch picker as the
       add-step form (not a bare text input), each wired to a PER-STEP-UNIQUE provider
       select id (data-provider) so the shared page JS resolves the right driving
       provider per step; the manual input is pre-filled with the step's current
       model/fallback_model. A plain (no-JS) POST with model=<id> still persists,
       proving the no-JS-safe path the shared name="model" pair relies on.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (MockProvider). ASCII-only.
Final line: "WORKFLOW EDIT-STEP OK".
"""

import io
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wfedit_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    RunStatus,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import engines
from app.services import workflows as wf


# --- small builders ----------------------------------------------------------
def _new_wf(name):
    with Session(engine) as s:
        w = Workflow(name=name, scope="global")
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _add(session, wid, **kw):
    """add_step with a sensible default prompt so the T1-01 validator is satisfied."""
    kw.setdefault("prompt_text", "Translate: {text}")
    return wf.add_step(session, wid, **kw)


def _get(step_id):
    with Session(engine) as s:
        return s.get(WorkflowStep, step_id)


def _mk_doc(name="wfedit-proj"):
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.docx")
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _mk_run(wid, doc_id, status):
    with Session(engine) as s:
        r = WorkflowRun(workflow_id=wid, document_id=doc_id, status=status)
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


def _raises(fn):
    try:
        fn()
    except wf.WorkflowError:
        return True
    return False


# ============================================================================
def test_edit_prompt_persists():
    wid = _new_wf("edit-prompt")
    with Session(engine) as s:
        st = _add(s, wid, name="Translator", provider="mock", prompt_text="Translate: {text}")
        sid = st.id
    with Session(engine) as s:
        out = wf.update_step(s, sid, prompt_text="Refined instruction: {text}")
        assert out.prompt_text == "Refined instruction: {text}", out.prompt_text
    row = _get(sid)
    assert row.prompt_text == "Refined instruction: {text}"
    assert row.order_index == 0 and row.name == "Translator" and row.provider == "mock"
    print("  (1) prompt edit persists; order_index + other fields untouched: OK")


def test_partial_update_leaves_others():
    wid = _new_wf("partial")
    with Session(engine) as s:
        st = _add(
            s,
            wid,
            name="Orig",
            provider="mock",
            qe_threshold=83,
            tm_examples_window=4,
            prompt_text="P: {text}",
        )
        sid = st.id
    with Session(engine) as s:
        wf.update_step(s, sid, name="Renamed")
    row = _get(sid)
    assert row.name == "Renamed", row.name
    assert row.qe_threshold == 83 and row.tm_examples_window == 4, "untouched fields kept"
    assert row.provider == "mock" and row.prompt_text == "P: {text}"
    print("  (2) partial update changes ONLY the supplied field: OK")


def test_full_field_roundtrip():
    wid = _new_wf("fullset")
    with Session(engine) as s:
        # prompt_text is dead on an lqa step (SPEC-lqa-step-honest-options) -- explicit
        # "" overrides _add's "Translate: {text}" setdefault (a prior LQA so prev_lqa
        # resolves for the update_step call below).
        _add(s, wid, name="LQA", step_type=StepType.LQA, key="lqa", prompt_text="")
        st = _add(s, wid, name="Fix", prompt_text="Fix: {text}")
        sid = st.id
    with Session(engine) as s:
        wf.update_step(
            s,
            sid,
            name="Full",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            model="m1",
            fallback_provider="mock",
            fallback_model="m2",
            prompt_text="Do: {text}",
            chunk_policy="none",
            redact_pii=True,
            glossary_id=None,
            adaptive_feedback=True,
            adaptive_feedback_window=7,
            doc_context_prepass=True,
            qe_gate=True,
            qe_threshold=55,
            qe_provider="mock",
            tm_examples=False,
            tm_examples_window=2,
            run_condition="prev_lqa:failed",
            key="fix",
            branch={"on_pass": "end"},
            max_visits=4,
        )
    r = _get(sid)
    assert (r.name, r.step_type, r.provider, r.model) == ("Full", "llm_agent", "mock", "m1")
    assert (r.fallback_provider, r.fallback_model) == ("mock", "m2")
    assert r.prompt_text == "Do: {text}" and r.chunk_policy == "none" and r.redact_pii
    assert r.adaptive_feedback and r.adaptive_feedback_window == 7 and r.doc_context_prepass
    assert r.qe_gate and r.qe_threshold == 55 and r.qe_provider == "mock"
    assert r.tm_examples is False and r.tm_examples_window == 2
    assert r.run_condition == {"ref": "prev_lqa", "op": "failed", "value": None}, r.run_condition
    assert r.key == "fix" and r.branch == {"on_pass": "end"} and r.max_visits == 4
    assert r.order_index == 1, ("order_index preserved", r.order_index)
    print("  (3) full field set round-trips; order_index preserved: OK")


def test_order_index_preserved_middle():
    wid = _new_wf("orderidx")
    with Session(engine) as s:
        _add(s, wid, name="A")
        b = _add(s, wid, name="B")
        _add(s, wid, name="C")
        sid = b.id
    with Session(engine) as s:
        wf.update_step(s, sid, name="B2", prompt_text="B2: {text}")
    row = _get(sid)
    assert row.order_index == 1 and row.name == "B2", (row.order_index, row.name)
    print("  (4) editing the middle step keeps its order_index: OK")


def test_graph_key_referenced_by_downstream():
    # step0 'fix' (key=fix); step1 LQA branches on_fail -> {step: fix}. Renaming/clearing
    # fix's key would orphan step1's branch -> REFUSED, changing nothing.
    wid = _new_wf("graph-key")
    with Session(engine) as s:
        fix = _add(s, wid, name="Fix", key="fix", prompt_text="Fix: {text}")
        _add(
            s,
            wid,
            name="LQA",
            step_type=StepType.LQA,
            key="lqa",
            prompt_text="",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        fid = fix.id
    with Session(engine) as s:
        assert _raises(lambda: wf.update_step(s, fid, key="fixed")), "rename must fail closed"
    assert _get(fid).key == "fix", "fail-closed: key unchanged after refused rename"
    with Session(engine) as s:
        assert _raises(lambda: wf.update_step(s, fid, key="")), "clearing key must fail closed"
    assert _get(fid).key == "fix"
    print("  (5a) renaming/clearing a key a downstream branch targets is REFUSED: OK")


def test_graph_gate_dependency():
    # (b1) step0 qe_gate ON (key=q); step1 guard prev_qe:failed. Turning off step0's gate
    #      breaks step1's guard -> REFUSED.
    wid = _new_wf("graph-gate")
    with Session(engine) as s:
        q = _add(s, wid, name="Draft", qe_gate=True, key="q", prompt_text="{text}")
        _add(s, wid, name="Fix", run_condition="prev_qe:failed", prompt_text="Fix: {text}")
        qid = q.id
    with Session(engine) as s:
        assert _raises(lambda: wf.update_step(s, qid, qe_gate=False)), (
            "turning off a depended-on gate must fail closed"
        )
    assert _get(qid).qe_gate is True, "fail-closed: qe_gate unchanged"
    # (b2) D3 on the step itself: an LQA step with its OWN branch, retyped to a gate-less
    #      llm_agent, loses the gate its branch needs -> REFUSED.
    wid2 = _new_wf("graph-d3")
    with Session(engine) as s:
        _add(s, wid2, name="Fix", key="fix", prompt_text="Fix: {text}")
        lqa = _add(
            s,
            wid2,
            name="LQA",
            step_type=StepType.LQA,
            key="lqa",
            prompt_text="",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        lid = lqa.id
    with Session(engine) as s:
        assert _raises(lambda: wf.update_step(s, lid, step_type=StepType.LLM_AGENT)), (
            "retyping a branching LQA to a gate-less step must fail closed (D3)"
        )
    assert _get(lid).step_type == StepType.LQA
    print("  (5b) breaking a gate a downstream guard / own branch depends on is REFUSED: OK")


def test_graph_run_condition_unresolvable():
    # step0 translator (no LQA); editing step1's guard to prev_lqa:failed can't resolve.
    wid = _new_wf("graph-rc")
    with Session(engine) as s:
        _add(s, wid, name="Translator", prompt_text="Translate: {text}")
        fix = _add(s, wid, name="Fix", prompt_text="Fix: {text}")
        fid = fix.id
    with Session(engine) as s:
        assert _raises(lambda: wf.update_step(s, fid, run_condition="prev_lqa:failed")), (
            "a guard with no earlier producing step must fail closed"
        )
    assert _get(fid).run_condition is None, "fail-closed: run_condition unchanged"
    print("  (5c) a run_condition referencing no earlier producing step is REFUSED: OK")


def test_valid_prompt_edit_with_dependents_succeeds():
    wid = _new_wf("graph-ok")
    with Session(engine) as s:
        fix = _add(s, wid, name="Fix", key="fix", prompt_text="Fix: {text}")
        _add(
            s,
            wid,
            name="LQA",
            step_type=StepType.LQA,
            key="lqa",
            prompt_text="",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        fid = fix.id
    with Session(engine) as s:
        wf.update_step(s, fid, prompt_text="Fix better: {text}")
    row = _get(fid)
    assert row.prompt_text == "Fix better: {text}" and row.key == "fix"
    print("  (5d) a valid prompt-only edit on a step WITH dependents SUCCEEDS: OK")


def test_key_uniqueness_excludes_self():
    wid = _new_wf("key-uniq")
    with Session(engine) as s:
        a = _add(s, wid, name="A", key="a")
        b = _add(s, wid, name="B", key="b")
        aid, bid = a.id, b.id
    with Session(engine) as s:
        assert _raises(lambda: wf.update_step(s, bid, key="a")), "dup key must be rejected"
    assert _get(bid).key == "b"
    with Session(engine) as s:
        wf.update_step(s, bid, key="b", name="B2")  # re-setting OWN key is fine
        wf.update_step(s, aid, key="a", name="A2")
    assert _get(bid).name == "B2" and _get(aid).name == "A2"
    print("  (5e) key uniqueness EXCLUDES self (dup rejected; own key accepted): OK")


def test_run_safety_blocks_active_runs():
    doc = _mk_doc()
    for status in (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED):
        wid = _new_wf(f"safety-{status}")
        with Session(engine) as s:
            st = _add(s, wid, name="T", prompt_text="P: {text}")
            sid = st.id
        _mk_run(wid, doc, status)
        with Session(engine) as s:
            try:
                wf.update_step(s, sid, prompt_text="X: {text}")
                raise AssertionError(f"edit must fail closed with a {status} run")
            except wf.WorkflowError as exc:
                assert "in-flight" in str(exc), str(exc)
        assert _get(sid).prompt_text == "P: {text}", "fail-closed: unchanged during active run"
    # a COMPLETED (terminal) run does NOT block
    wid = _new_wf("safety-completed")
    with Session(engine) as s:
        st = _add(s, wid, name="T", prompt_text="P: {text}")
        sid = st.id
    _mk_run(wid, doc, RunStatus.COMPLETED)
    with Session(engine) as s:
        wf.update_step(s, sid, prompt_text="Y: {text}")
    assert _get(sid).prompt_text == "Y: {text}", "terminal run must NOT block an edit"
    print("  (6) edit refused while pending/running/paused; terminal run does not block: OK")


def test_web_prefill_and_roundtrip():
    client = TestClient(app)
    wid = _new_wf("web-edit")
    with Session(engine) as s:
        st = _add(s, wid, name="WebStep", provider="mock", prompt_text="WEBPROMPT: {text}")
        sid = st.id
    # GET the builder: the edit form must be present + pre-filled with current values.
    r = client.get(f"/workflows/{wid}")
    assert r.status_code == 200, r.status_code
    body = r.text
    assert f"/workflows/{wid}/steps/{sid}/edit" in body, "edit form action must be present"
    assert "WEBPROMPT: {text}" in body, "the current prompt must be pre-filled"
    assert 'value="WebStep"' in body, "the current name must be pre-filled"
    # POST an edit (full form; unchecked boxes omitted): change prompt + name.
    r = client.post(
        f"/workflows/{wid}/steps/{sid}/edit",
        data={
            "name": "WebEdited",
            "step_type": "llm_agent",
            "provider": "mock",
            "chunk_policy": "auto",
            "prompt_text": "NEWWEB: {text}",
            "qe_threshold": "70",
            "adaptive_feedback_window": "5",
            "tm_examples_window": "3",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, ("edit POST redirects on success", r.status_code)
    row = _get(sid)
    assert row.prompt_text == "NEWWEB: {text}" and row.name == "WebEdited", (
        row.prompt_text,
        row.name,
    )
    # A graph-breaking POST must be a CLEAN 400, not a 500.
    with Session(engine) as s:
        fix = _add(s, wid, name="Fix", key="fix", prompt_text="Fix: {text}")
        _add(
            s,
            wid,
            name="LQA",
            step_type=StepType.LQA,
            key="lqa",
            prompt_text="",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        fid = fix.id
    r = client.post(
        f"/workflows/{wid}/steps/{fid}/edit",
        data={
            "name": "Fix",
            "step_type": "llm_agent",
            "chunk_policy": "auto",
            "prompt_text": "Fix: {text}",
            "step_key": "renamed",
            "qe_threshold": "70",
            "adaptive_feedback_window": "5",
            "tm_examples_window": "3",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400, ("graph-breaking edit must be a clean 400", r.status_code)
    assert _get(fid).key == "fix", "fail-closed: web edit changed nothing"
    print("  (7) web edit form pre-fills + round-trips; graph-breaking POST is a clean 400: OK")


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    out = buf.getvalue()
    assert out.isascii(), ("CLI output must be ASCII-only", out)
    return rc, out


def test_cli_partial_semantics():
    wid = _new_wf("cli-edit")
    with Session(engine) as s:
        st = _add(s, wid, name="Orig", qe_threshold=88, prompt_text="P: {text}")
        sid = st.id
    # partial: only --prompt changes; qe_threshold + name stay.
    rc, out = _run_cli(["workflow", "edit-step", str(wid), str(sid), "--prompt", "CLINEW: {text}"])
    assert rc == 0, out
    row = _get(sid)
    assert row.prompt_text == "CLINEW: {text}" and row.qe_threshold == 88 and row.name == "Orig", (
        row.prompt_text,
        row.qe_threshold,
        row.name,
    )
    # tri-state bool: --no-tm-examples turns it OFF (was ON by default).
    rc, out = _run_cli(["workflow", "edit-step", str(wid), str(sid), "--no-tm-examples"])
    assert rc == 0 and _get(sid).tm_examples is False, out
    # tri-state bool: --qe-gate turns it ON.
    rc, out = _run_cli(["workflow", "edit-step", str(wid), str(sid), "--qe-gate"])
    assert rc == 0 and _get(sid).qe_gate is True, out
    # no field flags -> usage error (nonzero).
    rc, out = _run_cli(["workflow", "edit-step", str(wid), str(sid)])
    assert rc != 0, ("no-op edit must be a usage error", out)
    # a graph-breaking CLI edit exits nonzero + changes nothing.
    with Session(engine) as s:
        fix = _add(s, wid, name="Fix", key="fix", prompt_text="Fix: {text}")
        _add(
            s,
            wid,
            name="LQA",
            step_type=StepType.LQA,
            key="lqa",
            prompt_text="",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
        fid = fix.id
    rc, out = _run_cli(["workflow", "edit-step", str(wid), str(fid), "--key", "renamed"])
    assert rc != 0, ("graph-breaking CLI edit must exit nonzero", out)
    assert _get(fid).key == "fix", "fail-closed: CLI edit changed nothing"
    print("  (8) CLI edit-step is partial (tri-state bools); breaking edit exits nonzero: OK")


def test_byte_stability_unedited_sibling():
    wid = _new_wf("byte-stable")
    with Session(engine) as s:
        a = _add(s, wid, name="A", provider="mock", qe_threshold=77, prompt_text="A: {text}")
        b = _add(s, wid, name="B", prompt_text="B: {text}")
        aid, bid = a.id, b.id
    snap = _get(aid)
    before = (
        snap.name,
        snap.provider,
        snap.qe_threshold,
        snap.prompt_text,
        snap.order_index,
        snap.tm_examples,
        snap.tm_examples_window,
    )
    with Session(engine) as s:
        wf.update_step(s, bid, prompt_text="B changed: {text}")
    after_a = _get(aid)
    after = (
        after_a.name,
        after_a.provider,
        after_a.qe_threshold,
        after_a.prompt_text,
        after_a.order_index,
        after_a.tm_examples,
        after_a.tm_examples_window,
    )
    assert before == after, ("an unedited sibling must be byte-identical", before, after)
    print("  (9) editing a step leaves its unedited siblings byte-identical: OK")


def test_engine_pin_survives_unmatched_edit():
    # A step pinned to engine:<id>, then the engine is DISABLED -> engine_options()
    # (app/web/deps.py) no longer offers it, so a real browser's provider <select> has
    # no matching <option> and submits "" on ANY save, even an unrelated prompt fix.
    prof = engines.create_profile(name="wfedit-pin-engine", kind="local", enabled=True)
    wid = _new_wf("engine-pin")
    with Session(engine) as s:
        st = _add(s, wid, name="Pinned", provider=f"engine:{prof.id}", prompt_text="Old: {text}")
        sid = st.id
    engines.update_profile(prof.id, enabled=False)  # falls out of engine_options()

    client = TestClient(app)
    # GET first: the template fix must splice the disabled pin back in as a selected
    # option (else the form itself lies about the step's state).
    r = client.get(f"/workflows/{wid}")
    assert r.status_code == 200, r.status_code
    assert f'value="engine:{prof.id}" selected' in r.text, (
        "the disabled engine pin must be rendered as an extra selected option"
    )

    # POST exactly what a real browser would submit: provider="" (no matching option),
    # only the prompt actually changed.
    r = client.post(
        f"/workflows/{wid}/steps/{sid}/edit",
        data={
            "name": "Pinned",
            "step_type": "llm_agent",
            "provider": "",
            "chunk_policy": "auto",
            "prompt_text": "New: {text}",
            "qe_threshold": "70",
            "adaptive_feedback_window": "5",
            "tm_examples_window": "3",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, ("edit POST redirects on success", r.status_code, r.text)
    row = _get(sid)
    assert row.provider == f"engine:{prof.id}", (
        "the engine pin must SURVIVE an unmatched provider='' post",
        row.provider,
    )
    assert row.prompt_text == "New: {text}", "the prompt edit must still apply"
    print("  (10a) a disabled-engine pin survives an unmatched provider='' web edit: OK")

    # Companion: a NON-engine provider (always representable in the picker) still
    # clears normally -- the guard must not neuter a genuine user-clear.
    with Session(engine) as s:
        st2 = _add(s, wid, name="Plain", provider="mock", prompt_text="P: {text}")
        sid2 = st2.id
    r2 = client.post(
        f"/workflows/{wid}/steps/{sid2}/edit",
        data={
            "name": "Plain",
            "step_type": "llm_agent",
            "provider": "",
            "chunk_policy": "auto",
            "prompt_text": "P2: {text}",
            "qe_threshold": "70",
            "adaptive_feedback_window": "5",
            "tm_examples_window": "3",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 303, (r2.status_code, r2.text)
    row2 = _get(sid2)
    assert row2.provider is None, (
        "a non-engine provider must still clear to default via ''",
        row2.provider,
    )
    assert row2.prompt_text == "P2: {text}"
    print("  (10b) a NON-engine provider still clears to default via a genuine '': OK")

    # Service-level (belt-and-suspenders) direct check: update_step itself protects the
    # pin even off the web surface (e.g. a hand-crafted call), for fallback_provider and
    # qe_provider too.
    with Session(engine) as s:
        st3 = _add(
            s,
            wid,
            name="AllThree",
            provider=f"engine:{prof.id}",
            fallback_provider=f"engine:{prof.id}",
            qe_gate=True,
            qe_provider=f"engine:{prof.id}",
            prompt_text="{text}",
        )
        sid3 = st3.id
    with Session(engine) as s:
        wf.update_step(
            s,
            sid3,
            provider="",
            fallback_provider="",
            qe_provider="",
            prompt_text="Changed: {text}",
        )
    row3 = _get(sid3)
    assert row3.provider == f"engine:{prof.id}"
    assert row3.fallback_provider == f"engine:{prof.id}"
    assert row3.qe_provider == f"engine:{prof.id}"
    assert row3.prompt_text == "Changed: {text}"
    print("  (10c) update_step protects provider/fallback_provider/qe_provider engine pins: OK")


def test_web_edit_model_picker_parity():
    """The edit form's Model/Fallback model fields must be the SAME .wfmodel picker as
    the add-step form -- select + fetch button -- not a bare text input, wired to a
    PER-STEP-UNIQUE provider id via data-provider so the shared page JS resolves the
    right driving <select> for each step's pickers, distinct from the add-step form's
    fixed wfProvider/wfFallbackProvider ids."""
    client = TestClient(app)
    wid = _new_wf("model-picker")
    with Session(engine) as s:
        st = _add(
            s,
            wid,
            name="Picker",
            provider="mock",
            model="old-model",
            fallback_provider="mock",
            fallback_model="old-fallback",
            prompt_text="P: {text}",
        )
        sid = st.id
    r = client.get(f"/workflows/{wid}")
    assert r.status_code == 200, r.status_code
    body = r.text

    prov_id = f"wfeProvider-{sid}"
    fbprov_id = f"wfeFallbackProvider-{sid}"
    # Unique per-step ids present exactly once, distinct from the add-step form's
    # fixed ids (which must still be there, unchanged).
    assert body.count(f'id="{prov_id}"') == 1, "per-step provider select must have a unique id"
    assert body.count(f'id="{fbprov_id}"') == 1, (
        "per-step fallback provider select must have a unique id"
    )
    assert 'id="wfProvider"' in body and 'id="wfFallbackProvider"' in body, (
        "add-step form's fixed provider ids must be unchanged"
    )
    assert prov_id != "wfProvider" and fbprov_id != "wfFallbackProvider"

    # Both .wfmodel blocks for this step exist and target the matching unique id.
    assert f'data-provider="{prov_id}"' in body, (
        "model picker must target the per-step provider select"
    )
    assert f'data-provider="{fbprov_id}"' in body, (
        "fallback model picker must target the per-step fallback provider select"
    )
    # 2 wfmodel-fetch buttons for add-step + 2 for this one step's edit form.
    assert body.count("wfmodel-fetch") >= 4

    # The manual input starts pre-filled with the CURRENT stored value (pre-fetch state).
    assert 'class="wfmodel-manual" value="old-model"' in body, (
        "model manual input must be pre-filled"
    )
    assert 'class="wfmodel-manual" value="old-fallback"' in body, (
        "fallback model manual input must be pre-filled"
    )

    # No-JS-safe path: a plain POST with model=<id> (as the manual input, or a
    # JS-populated select, would submit) still persists through the existing
    # route/service -- no route/service change needed for this feature.
    r2 = client.post(
        f"/workflows/{wid}/steps/{sid}/edit",
        data={
            "name": "Picker",
            "step_type": "llm_agent",
            "provider": "mock",
            "model": "picked-model-id",
            "fallback_provider": "mock",
            "fallback_model": "picked-fallback-id",
            "chunk_policy": "auto",
            "prompt_text": "P: {text}",
            "qe_threshold": "70",
            "adaptive_feedback_window": "5",
            "tm_examples_window": "3",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 303, (r2.status_code, r2.text)
    row = _get(sid)
    assert row.model == "picked-model-id", row.model
    assert row.fallback_model == "picked-fallback-id", row.fallback_model
    print(
        "  (11) edit-form model/fallback pickers: unique per-step ids, pre-filled, POST persists: OK"  # noqa: E501
    )


def main():
    init_db()
    test_edit_prompt_persists()
    test_partial_update_leaves_others()
    test_full_field_roundtrip()
    test_order_index_preserved_middle()
    test_graph_key_referenced_by_downstream()
    test_graph_gate_dependency()
    test_graph_run_condition_unresolvable()
    test_valid_prompt_edit_with_dependents_succeeds()
    test_key_uniqueness_excludes_self()
    test_run_safety_blocks_active_runs()
    test_web_prefill_and_roundtrip()
    test_cli_partial_semantics()
    test_byte_stability_unedited_sibling()
    test_engine_pin_survives_unmatched_edit()
    test_web_edit_model_picker_parity()
    print("WORKFLOW EDIT-STEP OK")


if __name__ == "__main__":
    main()
