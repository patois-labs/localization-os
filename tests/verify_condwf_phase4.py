"""Conditional-workflow PHASE 4 verification (offline, hermetic).

SPEC-conditional-workflows Phase 4 = PROMPTABLE DRAFTING of branches/loops + the BUILDER
GRAPH visualization + web/CLI parity. Phases 1-3 shipped the schema, the cursor engine,
the guards, and the branch/loop state machine; Phase 4 lets `author_workflow_setup` DRAFT
`key`/`branch`/`max_visits` from a plain-language description (the LLM PROPOSES; the server
validates the drafted graph with the SAME fail-closed rules as save-time — skip-and-warn on
anything invalid, NEVER a saved-invalid graph), renders the drafted graph on the review
screen, and draws a read-only graph on the builder page.

Acceptance bar proven here:
  (1)  DRAFT a valid loop -> the graph persists EXACTLY (key/branch/max_visits) through
       apply_workflow_draft; the applied workflow's builder page renders the graph.
  (2)  DRAFT an invalid branch TARGET -> a warning + the branch is dropped + the rest of
       the step/workflow is intact (skip-and-warn).
  (3)  DRAFT a branch on a GATE-LESS step -> a warning + dropped (D3).
  (4)  DRAFT a DUPLICATE key -> a warning + the dup key dropped (the first keeps it).
  (5)  APPLY (confirm) persists keys/branches/max_visits atomically; a directly-supplied
       INVALID graph (unknown target / bad max_visits) is REJECTED, creating nothing.
  (6)  BUILDER page renders the graph markup for a branched workflow and OMITS it for a
       plain one.
  (7)  WEB review screen shows the drafted graph + editable branch/key controls; a confirm
       with those controls persists the branch (web parity).
  (8)  CLI `workflow author` review output shows the drafted branches/keys/caps; --create
       persists them (CLI parity).
  (9)  BYTE-STABILITY: drafting NOTHING conditional -> key/branch/max_visits all NULL, zero
       conditional warnings, apply persists the plain steps, the builder omits the graph
       (exactly the pre-Phase-4 behavior).

Provider wiring goes through the REAL OpenAICompatibleProvider with an injected
httpx.MockTransport (the promptable-workflow fixture pattern) so the service, CLI, and web
routes all resolve the canned JSON. ASCII-only prints (Windows consoles).
"""

import io
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_condwf4_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.cli.__main__ import main as cli_main
from app.db import engine as db_engine
from app.db import init_db
from app.models import StepType, Workflow, WorkflowStep
from app.services import setup_author, workflows
from app.services.engines import create_profile, get_profile
from app.services.providers import (
    OpenAICompatibleProvider,
    register_provider,
)

init_db()


# --- transports (openai chat-completions wire shape) -------------------------
def json_transport(responses, counter):
    def h(req):
        i = counter["n"]
        counter["n"] += 1
        content_str = responses[min(i, len(responses) - 1)]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content_str}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20},
            },
        )

    return httpx.MockTransport(h)


_ENGINE_SEQ = {"n": 0}


def make_engine(responses, counter=None):
    counter = counter if counter is not None else {"n": 0}
    _ENGINE_SEQ["n"] += 1
    p = create_profile(
        f"LocalP4_{_ENGINE_SEQ['n']}",
        "openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="sk-local",
        model="local-model",
    )
    ref = f"engine:{p.id}"
    register_provider(
        ref,
        OpenAICompatibleProvider(
            profile=get_profile(p.id), transport=json_transport(responses, counter)
        ),
    )
    return ref


def _session():
    return Session(db_engine)


def _counts():
    with _session() as s:
        wf = s.exec(select(func.count()).select_from(Workflow)).one()
        st = s.exec(select(func.count()).select_from(WorkflowStep)).one()
        return wf, st


_APP = None


def app_ref():
    global _APP
    if _APP is None:
        from app.main import app

        _APP = app
    return _APP


# The canonical the maintainer case as a drafted graph: translate -> Fix(key=fix, max_visits=3) ->
# LQA(key=lqa, on_pass=end / on_fail loops back to fix). Ordered fix-before-lqa because a
# branch target can only name an EARLIER step (the incremental save-time constraint).
# QA-7 (#37): the steps are drafted with a BLANK provider (inherit the run default). On
# this hermetic env config.DEFAULT_PROVIDER is keyless 'claude' (NON-live), so a blank
# executable step would silently resolve to MockProvider and is therefore bound to the
# selected engine with ONE "providers bound" warning (QA-7 review follow-up) -- orthogonal
# to the GRAPH, which stays fully resolvable (no branch/graph warning). The checks below
# filter that provider-binding warning out. (Engine-binding is proven exhaustively in
# verify_qa_7_8_10_authoring.py.)
LOOP_DRAFT = (
    '{"name": "P4 Loop WF", "source_locale": "en-US", "target_locale": "fr-FR", '
    '"steps": ['
    '{"name": "Translate", "step_type": "llm_agent", '
    '"prompt_text": "Translate {text} to {target_locale}."}, '
    # The Fix prompt NAMES its target language ({target_locale}): GitHub #43 made a
    # locale-less llm_agent prompt draw an authoring warning, and this check asserts the
    # draft is warning-clean apart from the orthogonal provider-binding one. A fix step
    # that never says which language to produce is exactly the shape #43 exists to warn
    # about, so the fixture models the good one.
    '{"name": "Fix", "step_type": "llm_agent", "key": "fix", "max_visits": 3, '
    '"prompt_text": "Fix the errors in {prior} ({target_locale})."}, '
    '{"name": "LQA", "step_type": "lqa", "key": "lqa", '
    '"branch": {"on_pass": "end", "on_fail": {"step": "fix"}}}]}'
)


# --- 1. draft a valid loop -> persists exactly + builder renders the graph -----
def check_draft_valid_loop_persists():
    ref = make_engine([LOOP_DRAFT])
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s,
            description="translate, LQA at 90, if it fails run a fix step and re-check "
            "up to 3 times",
            provider=ref,
            org_id=None,
            actor="tester",
        )
    f, w = out["fields"], out["warnings"]
    # The GRAPH is fully resolvable -> no branch/graph warning. TWO orthogonal PROVIDER
    # warnings are filtered: the QA-7 binding one (blank steps + a non-live run default, see
    # the LOOP_DRAFT note) and its direct consequence, the #148 self-judging one -- binding
    # every step to the ONE selected engine necessarily puts the LQA step on the same engine
    # as the step it grades. Both are about engine CHOICE, not about the shape of the graph.
    graph_w = [x for x in w if "bound to" not in x and "judging the engine" not in x]
    assert graph_w == [], ("a fully-resolvable graph draft must have no graph warnings", w)
    steps = f["steps"]
    assert len(steps) == 3, steps
    assert steps[1]["key"] == "fix" and steps[1]["max_visits"] == 3, steps[1]
    assert steps[2]["key"] == "lqa", steps[2]
    assert steps[2]["branch"] == {"on_pass": "end", "on_fail": {"step": "fix"}}, steps[2]["branch"]
    assert steps[0]["key"] is None and steps[0]["branch"] is None, (
        "a plain step drafts NULL graph fields",
        steps[0],
    )

    # Apply -> the persisted graph is byte-exact.
    with _session() as s:
        wf = setup_author.apply_workflow_draft(s, f, org_id=None, actor="tester")
        wf_id = wf.id
    with _session() as s:
        rows = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf_id)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert rows[1].key == "fix" and rows[1].max_visits == 3, (rows[1].key, rows[1].max_visits)
        assert rows[2].key == "lqa"
        assert rows[2].branch == {"on_pass": "end", "on_fail": {"step": "fix"}}, rows[2].branch

    # The applied workflow's builder page renders the read-only graph.
    r = TestClient(app_ref()).get(f"/workflows/{wf_id}")
    assert r.status_code == 200 and "wf-graph" in r.text and "wf-graph-svg" in r.text, (
        "builder must render the graph for a branched workflow",
        r.status_code,
    )
    assert "stroke-dasharray" in r.text, "the loop-back edge must be drawn distinctly (dashed)"
    print(
        "  1. draft a valid loop -> key/branch/max_visits persist EXACTLY; builder page "
        "renders the graph (loop-back dashed)"
    )


# --- 2. draft an invalid branch TARGET -> warning + branch dropped, rest intact -
def check_draft_invalid_target_warns_and_drops():
    bad = (
        '{"name": "P4 Bad target", "source_locale": "en-US", "target_locale": "fr-FR", '
        '"steps": ['
        '{"name": "Translate", "step_type": "llm_agent", "prompt_text": "Translate {text}."}, '
        '{"name": "LQA", "step_type": "lqa", "key": "lqa", '
        '"branch": {"on_pass": "end", "on_fail": {"step": "ghost"}}}]}'
    )
    ref = make_engine([bad])
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s, description="x", provider=ref, org_id=None, actor="tester"
        )
    f, w = out["fields"], out["warnings"]
    assert any("branch dropped" in x for x in w), ("an unknown target must WARN", w)
    lqa = f["steps"][1]
    assert lqa["branch"] is None, ("the unresolvable branch must be DROPPED", lqa["branch"])
    assert lqa["key"] == "lqa", ("the rest of the step stays intact", lqa)
    assert lqa["step_type"] == "lqa" and len(f["steps"]) == 2
    print(
        "  2. draft an invalid branch target -> warning + branch dropped; the step + "
        "the rest of the workflow stay intact (skip-and-warn)"
    )


# --- 3. draft a branch on a GATE-LESS step -> warning + dropped (D3) -----------
def check_draft_branch_on_gateless_dropped():
    bad = (
        '{"name": "P4 Gateless", "source_locale": "en-US", "target_locale": "fr-FR", '
        '"steps": ['
        '{"name": "Translate", "step_type": "llm_agent", "key": "t", '
        '"prompt_text": "Translate {text}."}, '
        '{"name": "Plain", "step_type": "llm_agent", "prompt_text": "Rework {prior}.", '
        '"branch": {"on_pass": "end", "on_fail": {"step": "t"}}}]}'
    )
    ref = make_engine([bad])
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s, description="x", provider=ref, org_id=None, actor="tester"
        )
    f, w = out["fields"], out["warnings"]
    assert any("branch dropped" in x for x in w), ("a branch on a non-gate step must WARN", w)
    plain = f["steps"][1]
    assert plain["branch"] is None, ("D3: a branch on a gate-less step is DROPPED", plain["branch"])
    print("  3. draft a branch on a gate-less llm_agent step -> warning + dropped (D3)")


# --- 4. draft a DUPLICATE key -> warning + dup key dropped ---------------------
def check_draft_duplicate_key_warned():
    dup = (
        '{"name": "P4 Dup key", "source_locale": "en-US", "target_locale": "fr-FR", '
        '"steps": ['
        '{"name": "A", "step_type": "llm_agent", "key": "fix", "prompt_text": "A {text}."}, '
        '{"name": "B", "step_type": "llm_agent", "key": "fix", "prompt_text": "B {prior}."}]}'
    )
    ref = make_engine([dup])
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s, description="x", provider=ref, org_id=None, actor="tester"
        )
    f, w = out["fields"], out["warnings"]
    assert any("step key dropped" in x and "already used" in x for x in w), (
        "a duplicate key must WARN",
        w,
    )
    a, b = f["steps"]
    assert a["key"] == "fix", ("the FIRST use keeps the key", a["key"])
    assert b["key"] is None, ("the duplicate key is DROPPED off the second step", b["key"])
    print("  4. draft a duplicate step key -> warning + dup dropped (first keeps it)")


# --- 5. apply persists atomically; a directly-supplied invalid graph is rejected -
def check_apply_atomic_and_reject():
    # (a) unknown branch target passed straight to apply -> WorkflowApplyError, no rows.
    before = _counts()
    payload = {
        "name": "P4 apply reject",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {"step_type": "llm_agent", "prompt_text": "Translate {text}."},
            {
                "step_type": "lqa",
                "key": "lqa",
                "branch": {"on_pass": "end", "on_fail": {"step": "ghost"}},
            },
        ],
    }
    raised = False
    with _session() as s:
        try:
            setup_author.apply_workflow_draft(s, payload, org_id=None, actor="tester")
        except setup_author.WorkflowApplyError as exc:
            raised = True
            assert any("ghost" in e or "does not exist" in e for e in exc.errors), exc.errors
    assert raised, "an unknown branch target must reject the whole confirm"
    assert _counts() == before, "a rejected confirm creates nothing"

    # (b) a bad max_visits (0) is rejected atomically.
    before = _counts()
    payload2 = {
        "name": "P4 bad maxvisits",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {
                "step_type": "llm_agent",
                "prompt_text": "Translate {text}.",
                "key": "t",
                "max_visits": 0,
            }
        ],
    }
    raised = False
    with _session() as s:
        try:
            setup_author.apply_workflow_draft(s, payload2, org_id=None, actor="tester")
        except setup_author.WorkflowApplyError as exc:
            raised = True
            assert any("max visits" in e for e in exc.errors), exc.errors
    assert raised, "max_visits < 1 must reject"
    assert _counts() == before, "a rejected confirm creates nothing"

    # (c) a valid graph passed directly persists exactly.
    payload3 = {
        "name": "P4 apply ok",
        "customer_id": "",
        "source_locale": "en-US",
        "target_locales": ["fr-FR"],
        "notes_for_reviewer": "",
        "steps": [
            {"step_type": "llm_agent", "prompt_text": "Translate {text}."},
            {
                "step_type": "llm_agent",
                "key": "fix",
                "max_visits": 2,
                "prompt_text": "Fix {prior}.",
            },
            {
                "step_type": "lqa",
                "key": "lqa",
                "branch": {"on_pass": "end", "on_fail": {"step": "fix"}},
            },
        ],
    }
    with _session() as s:
        wf = setup_author.apply_workflow_draft(s, payload3, org_id=None, actor="tester")
        wid = wf.id
    with _session() as s:
        rows = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wid)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert rows[1].key == "fix" and rows[1].max_visits == 2
        assert rows[2].branch == {"on_pass": "end", "on_fail": {"step": "fix"}}
    print(
        "  5. apply: unknown target + bad max_visits rejected atomically (zero rows); a "
        "valid graph persists exactly"
    )


# --- 6. builder page renders the graph for BOTH linear + branched (SPEC 3.1) ------
def check_builder_graph_render_gate():
    client = TestClient(app_ref())
    with _session() as s:
        wf = Workflow(name="P4 plainbuild", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        pid = wf.id
        workflows.add_step(s, pid, name="Translator", prompt_text="Translate: {text}")
        workflows.add_step(s, pid, name="Reviewer", prompt_text="Review: {text}")
    r = client.get(f"/workflows/{pid}")
    # SPEC-workflows-redesign Phase 3.1 relaxed the branch-only early-return: a plain
    # linear workflow now ALSO renders the "how it works" diagram (a simple spine), but
    # WITHOUT any branch/loop edges (no dashed loop-back).
    assert r.status_code == 200 and "wf-graph-svg" in r.text, (
        "a linear workflow must now render the graph (Phase 3.1)",
        r.status_code,
    )
    assert "stroke-dasharray" not in r.text, (
        "a linear workflow has no loop-back edge to draw",
        pid,
    )

    with _session() as s:
        wf = Workflow(name="P4 branchbuild", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        bid = wf.id
        workflows.add_step(s, bid, name="Translator", prompt_text="Translate: {text}")
        workflows.add_step(s, bid, name="Fix", key="fix", prompt_text="Fix: {text}", max_visits=3)
        workflows.add_step(
            s,
            bid,
            name="LQA",
            step_type=StepType.LQA,
            key="lqa",
            branch={"on_pass": "end", "on_fail": {"step": "fix"}},
        )
    r = client.get(f"/workflows/{bid}")
    assert "wf-graph-svg" in r.text and "stroke-dasharray" in r.text, (
        "a branched workflow must render the SVG graph with a dashed loop-back edge"
    )
    print(
        "  6. builder page: renders the graph for linear (spine, no loop edge) AND "
        "branched (dashed loop-back) workflows"
    )


# --- 7. web review shows the graph + editable branch controls; confirm persists -
def check_web_review_and_confirm():
    ref = make_engine([LOOP_DRAFT])
    client = TestClient(app_ref())
    r = client.post("/workflows/author", data={"description": "loop until pass", "provider": ref})
    assert r.status_code == 200 and "Review the drafted workflow" in r.text, r.status_code
    # The drafted graph is SHOWN on the review screen.
    assert "wf-graph-svg" in r.text, "the review screen must SHOW the drafted graph"
    # Editable branch/key controls are present, pre-filled from the draft.
    assert 'name="s2_branch_on_fail"' in r.text and 'name="s1_key"' in r.text, (
        "the review screen must render editable branch/key controls"
    )
    assert 'value="fix" selected' in r.text, (
        "the drafted on_fail=fix loop-back must be pre-selected on the review screen"
    )

    # Confirm with the branch controls -> the workflow persists the branch (web parity).
    r = client.post(
        "/workflows/author/confirm",
        data={
            "step_count": "3",
            "name": "P4 Web Loop",
            "customer_id": "",
            "source_locale": "en-US",
            "target_locales": ["fr-FR"],
            "s0_step_type": "llm_agent",
            "s0_prompt_text": "Translate {text} to French.",
            "s1_step_type": "llm_agent",
            "s1_prompt_text": "Fix {prior}.",
            "s1_key": "fix",
            "s1_max_visits": "3",
            "s2_step_type": "lqa",
            "s2_key": "lqa",
            "s2_branch_on_pass": "end",
            "s2_branch_on_fail": "fix",
            "notes_for_reviewer": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, ("confirm must create + redirect", r.status_code, r.text[:400])
    with _session() as s:
        wf = s.exec(select(Workflow).where(Workflow.name == "P4 Web Loop")).first()
        assert wf is not None
        rows = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf.id)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert rows[1].key == "fix" and rows[1].max_visits == 3, (rows[1].key, rows[1].max_visits)
        assert rows[2].branch == {"on_pass": "end", "on_fail": {"step": "fix"}}, rows[2].branch
    print(
        "  7. web review shows the drafted graph + editable branch/key controls "
        "(loop-back pre-selected); confirm persists the branch (web parity)"
    )


# --- 8. CLI `workflow author` review output shows branches; --create persists ----
def check_cli_parity():
    # Default (print): the review output shows the drafted key/branch/cap.
    ref = make_engine([LOOP_DRAFT])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["workflow", "author", "loop until pass", "--provider", ref])
    out = buf.getvalue()
    assert rc == 0, (rc, out)
    assert "key:fix" in out and "max_visits:3" in out, ("CLI must show key + cap", out)
    assert "branch[" in out and "->fix" in out, ("CLI must show the branch loop-back", out)

    # --create: the drafted graph is persisted (CLI parity with web confirm).
    ref2 = make_engine([LOOP_DRAFT])
    rc = cli_main(["workflow", "author", "loop until pass", "--provider", ref2, "--create"])
    assert rc == 0, rc
    with _session() as s:
        wf = s.exec(
            select(Workflow).where(Workflow.name == "P4 Loop WF").order_by(Workflow.id.desc())
        ).first()
        rows = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf.id)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert rows[1].key == "fix" and rows[1].max_visits == 3
        assert rows[2].branch == {"on_pass": "end", "on_fail": {"step": "fix"}}, rows[2].branch
    print(
        "  8. CLI `workflow author` review output shows key/branch/max_visits; --create "
        "persists the drafted graph (CLI parity)"
    )


# --- 9. byte-stability: a non-conditional draft is the pre-Phase-4 behavior -----
def check_byte_stable_when_not_conditional():
    plain = (
        '{"name": "P4 Plain draft", "source_locale": "en-US", "target_locale": "fr-FR", '
        '"steps": ['
        # Names its target language -- see the LOOP_DRAFT note (GitHub #43).
        '{"name": "Translate", "step_type": "llm_agent", '
        '"prompt_text": "Translate {text} into {target_locale}."}, '
        '{"name": "Review", "step_type": "human_review"}]}'
    )
    ref = make_engine([plain])
    with _session() as s:
        out = setup_author.author_workflow_setup(
            s, description="a simple two-step flow", provider=ref, org_id=None, actor="t"
        )
    f, w = out["fields"], out["warnings"]
    # No CONDITIONAL/graph warnings; the lone provider-binding warning (blank Translate step
    # + a non-live run default, QA-7 follow-up) is orthogonal to byte-stability and filtered.
    graph_w = [x for x in w if "bound to" not in x and "judging the engine" not in x]
    assert graph_w == [], ("a plain draft has no conditional warnings", w)
    for st in f["steps"]:
        assert st["key"] is None and st["branch"] is None and st["max_visits"] is None, (
            "a non-conditional draft leaves the graph fields NULL",
            st,
        )
    with _session() as s:
        wf = setup_author.apply_workflow_draft(s, f, org_id=None, actor="t")
        wid = wf.id
    with _session() as s:
        rows = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wid)
            .order_by(WorkflowStep.order_index)
        ).all()
        assert all(r.key is None and r.branch is None and r.max_visits is None for r in rows)
    r = TestClient(app_ref()).get(f"/workflows/{wid}")
    # SPEC 3.1: the plain workflow still stores NULL graph fields (byte-stable data), but
    # its builder now renders the linear "how it works" diagram WITHOUT branch/loop edges.
    assert "wf-graph-svg" in r.text and "stroke-dasharray" not in r.text, (
        "a plain applied workflow renders a linear graph with no loop-back edge"
    )
    print(
        "  9. byte-stable: a non-conditional draft leaves key/branch/max_visits NULL, "
        "zero conditional warnings, applies plainly; builder renders a linear diagram"
    )


def main() -> None:
    check_draft_valid_loop_persists()
    check_draft_invalid_target_warns_and_drops()
    check_draft_branch_on_gateless_dropped()
    check_draft_duplicate_key_warned()
    check_apply_atomic_and_reject()
    check_builder_graph_render_gate()
    check_web_review_and_confirm()
    check_cli_parity()
    check_byte_stable_when_not_conditional()
    print("CONDWF PHASE 4 OK")


if __name__ == "__main__":
    main()
