"""Run-view GRAPH OVERLAY verification (SPEC-conditional-workflows Phase 4, deferred item).

Hermetic + offline: throwaway LOC_TMS_DATA_DIR + provider keys blanked. Rather than drive
the orchestrator, the run state is built by inserting StepRun rows DIRECTLY — the overlay is
a pure read-only projection, so hand-built rows exercise every case deterministically
(loops, skips, pauses, orphans) without a live run.

Covers:
  build_overlay (pure)     per-node status = LATEST row; visits count; score/gate carry;
                           looped flag; ORPHAN rows (step id no longer in the graph) split
                           out honestly; `any` False for a run with no rows.
  wf_graph BYTE-IDENTITY   the macro rendered WITHOUT the overlay param carries ZERO overlay
                           artifacts (wfg-state / wfg-run-note / the xN visit glyph) — the
                           builder + author-review call paths are unchanged; WITH overlay the
                           per-node state artifacts appear.
  web run-view             GET /runs/{id} draws the overlaid graph: a completed linear run
                           ("done"), a correction loop with visits (x2) + a TRAVERSED loop-back
                           (solid, not dashed), a run_condition SKIP ("skipped"), a paused
                           human-review step ("paused"), a not-reached step ("not run"); an
                           orphan-step run degrades to an honest note and never 500s.
  headless parity          GET /api/runs/{id} + CLI `workflow show-run` (--json + human) expose
                           step_states (key/index/status/visits/score/gate) + orphan_steps —
                           the same facts the graph draws, available without a browser.

Final line: "RUN VIEW GRAPH OK".
"""

import io
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_runviz_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    StepRun,
    StepType,
    TransMode,
    WorkflowRun,
)
from app.services import apikeys, orchestrator, run_overlay, security, settings
from app.services import workflows as W
from app.web.deps import templates

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

_N = 0


def _uniq(prefix):
    global _N
    _N += 1
    return f"{prefix}-{_N}"


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


# --- fixtures -----------------------------------------------------------------
def _mk_project_doc(locale="es-ES"):
    with Session(engine) as s:
        p = Project(name=_uniq("RVProj"), source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        doc = Document(
            project_id=p.id,
            filename="rv.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Hello world."}]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return p.id, doc.id


def _mk_project_doc_seg(locale="es-ES"):
    """A project + a SEGMENTED doc WITH one real Segment for `locale` — enough for the
    orchestrator to actually run (used by the orchestrator-DRIVEN tests, not hand-inserted)."""
    pid, did = _mk_project_doc(locale)
    with Session(engine) as s:
        s.add(
            Segment(
                document_id=did,
                target_locale=locale,
                order_index=0,
                source_text="Hello world.",
                target_text="",
            )
        )
        s.commit()
    return pid, did


def _mk_review_wf():
    """Translate (mock) -> Human review — pauses, then resume completes it."""
    with Session(engine) as s:
        w = W.create_workflow(s, _uniq("RVReview"))
        W.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            model="m1",
            prompt_text="T {text}",
            key="translate",
        )
        W.add_step(
            s, w.id, name="Review", step_type=StepType.HUMAN_REVIEW, prompt_text="", key="review"
        )
        return w.id


def _mk_linear_wf():
    """Translate -> LQA (no branch)."""
    with Session(engine) as s:
        w = W.create_workflow(s, _uniq("RVLinear"))
        W.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            model="m1",
            prompt_text="T {text}",
            key="translate",
        )
        # model/prompt_text are dead on an lqa step (SPEC-lqa-step-honest-options) --
        # omitted; only provider/key are meaningful here.
        W.add_step(s, w.id, name="Check", step_type=StepType.LQA, provider="mock", key="lqa")
        return w.id


def _mk_loop_wf():
    """Translate -> LQA(gate, on_fail loops back to translate) -> Review(human)."""
    with Session(engine) as s:
        w = W.create_workflow(s, _uniq("RVLoop"))
        W.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            model="m1",
            prompt_text="T {text}",
            key="translate",
        )
        # model/qe_gate/qe_threshold are dead on an lqa step (SPEC-lqa-step-honest-
        # options) -- an LQA step already produces a gate for `branch` on its own,
        # with no qe_gate needed.
        W.add_step(
            s,
            w.id,
            name="Check",
            step_type=StepType.LQA,
            provider="mock",
            key="lqa",
            branch={"on_fail": {"step": "translate"}, "on_pass": "next"},
        )
        W.add_step(
            s, w.id, name="Review", step_type=StepType.HUMAN_REVIEW, prompt_text="", key="review"
        )
        return w.id


def _mk_skip_wf():
    """Translate -> Fix (guarded by a run_condition, so it can be SKIPPED)."""
    with Session(engine) as s:
        w = W.create_workflow(s, _uniq("RVSkip"))
        W.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            model="m1",
            prompt_text="T {text}",
            key="translate",
            qe_gate=True,
            qe_threshold=80,
        )
        W.add_step(
            s,
            w.id,
            name="Fix",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            model="m1",
            prompt_text="F {text}",
            key="fix",
            run_condition="prev_qe:failed",
        )
        return w.id


def _mk_run(wf_id, doc_id, status=RunStatus.COMPLETED, locale="es-ES"):
    with Session(engine) as s:
        r = WorkflowRun(workflow_id=wf_id, document_id=doc_id, target_locale=locale, status=status)
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


def _step_ids(wf_id):
    from app.services import orchestrator

    with Session(engine) as s:
        return [st.id for st in orchestrator.get_steps(s, wf_id)]


def _add_stepruns(run_id, rows):
    """rows = [(step_id, status, outcome_index, score, gate), ...]."""
    with Session(engine) as s:
        for sid, status, oi, score, gate in rows:
            s.add(
                StepRun(
                    workflow_run_id=run_id,
                    step_id=sid,
                    status=status,
                    outcome_index=oi,
                    score=score,
                    gate=gate,
                )
            )
        s.commit()


# ============================================================================
# 1. build_overlay — pure projection
# ============================================================================
def test_overlay_build_unit():
    steps = [
        SimpleNamespace(id=10, key="translate", name="Translate"),
        SimpleNamespace(id=11, key="lqa", name="Check"),
    ]
    # translate visited twice (loop), lqa failed then... only one lqa row here.
    rows = [
        SimpleNamespace(id=1, step_id=10, status="completed", score=None, gate=None),
        SimpleNamespace(id=2, step_id=11, status="failed", score=50.0, gate="failed"),
        SimpleNamespace(id=3, step_id=10, status="completed", score=None, gate=None),
        # an ORPHAN — step 99 is not in the current graph (workflow edited)
        SimpleNamespace(id=4, step_id=99, status="completed", score=None, gate=None),
    ]
    ov = run_overlay.build_overlay(rows, steps)
    assert ov["any"] is True
    tnode = ov["by_id"][10]
    assert tnode["visits"] == 2 and tnode["looped"] is True, tnode
    assert tnode["status"] == "completed" and tnode["label"] == "done", tnode
    lnode = ov["by_id"][11]
    assert lnode["visits"] == 1 and lnode["status"] == "failed", lnode
    assert lnode["score"] == 50.0 and lnode["gate"] == "failed", lnode
    assert lnode["token"] == "var(--danger)", lnode
    # step_states is ordered by graph position, no step_id, key is the human handle
    ss = ov["step_states"]
    assert [r["key"] for r in ss] == ["translate", "lqa"], ss
    assert ss[0]["visits"] == 2 and ss[1]["gate"] == "failed", ss
    assert all("step_id" not in r for r in ss), "step_states must not leak the pk"
    # the orphan is split out honestly, never crashes
    assert len(ov["orphan_steps"]) == 1 and ov["orphan_steps"][0]["step_id"] == 99, ov
    # a latest-row-wins check: failed then re-run completed reads completed
    rows2 = [
        SimpleNamespace(id=1, step_id=10, status="failed", score=None, gate=None),
        SimpleNamespace(id=2, step_id=10, status="completed", score=None, gate=None),
    ]
    ov2 = run_overlay.build_overlay(rows2, steps)
    assert ov2["by_id"][10]["status"] == "completed", "latest StepRun row is authoritative"
    # empty run -> no overlay data (degrade to plain graph)
    assert run_overlay.build_overlay([], steps)["any"] is False
    # never raises on junk
    assert run_overlay.build_overlay(None, None)["any"] is False
    print(
        "  build_overlay: status=latest, visits/looped, score/gate carry, orphan split, "
        "any=False for empty OK"
    )


# ============================================================================
# 2. wf_graph macro — BYTE-IDENTITY without the overlay param
# ============================================================================
def _render_macro(steps, overlay=None):
    tpl = templates.env.from_string(
        "{% import '_macros.html' as m %}{{ m.wf_graph(steps, overlay=ov) }}"
    )
    return tpl.render(steps=steps, ov=overlay)


def test_macro_byte_identity():
    steps = [
        SimpleNamespace(
            id=10,
            key="translate",
            name="Translate",
            step_type="llm_agent",
            provider="mock",
            model="m1",
            branch=None,
            qe_gate=False,
            glossary_id=None,
            tm_examples=False,
            redact_pii=False,
            needs_review=False,
            max_visits=None,
            run_condition=None,
        ),
        SimpleNamespace(
            id=11,
            key="lqa",
            name="Check",
            step_type="lqa",
            provider="mock",
            model="m1",
            branch=None,
            qe_gate=False,
            glossary_id=None,
            tm_examples=False,
            redact_pii=False,
            needs_review=False,
            max_visits=None,
            run_condition=None,
        ),
    ]
    plain = _render_macro(steps, overlay=None)
    # ZERO overlay artifacts on the no-overlay path (the builder / author-review call path)
    for artifact in (
        "wfg-state",
        "wfg-run-note",
        "Workflow run diagram",
        "&#215;",
        "Overlaid with this run",
    ):
        assert artifact not in plain, f"no-overlay render leaked {artifact!r}"
    # it still renders the graph (regression sanity)
    assert "wf-graph-svg" in plain and "1. Translate" in plain, plain[:200]
    # WITH overlay the per-node state artifacts appear
    ov = run_overlay.build_overlay(
        [
            SimpleNamespace(id=1, step_id=10, status="completed", score=None, gate=None),
            SimpleNamespace(id=2, step_id=11, status="completed", score=None, gate=None),
        ],
        steps,
    )
    withov = _render_macro(steps, overlay=ov)
    assert "wfg-state" in withov and "done" in withov, withov[:400]
    assert "Overlaid with this run" in withov
    assert 'aria-label="Workflow run diagram"' in withov
    print(
        "  wf_graph: no-overlay render is artifact-free (byte-stable builder path); overlay "
        "adds per-node state OK"
    )


# ============================================================================
# 3. web run-view — linear completed run
# ============================================================================
def test_web_linear():
    _pid, did = _mk_project_doc()
    wf = _mk_linear_wf()
    sids = _step_ids(wf)
    run = _mk_run(wf, did, status=RunStatus.COMPLETED)
    _add_stepruns(
        run, [(sids[0], "completed", 0, None, None), (sids[1], "completed", 0, 92.0, "passed")]
    )
    client = TestClient(app)
    r = client.get(f"/runs/{run}")
    assert r.status_code == 200, r.text
    body = r.text
    assert "How this run went" in body and "wf-graph-svg" in body
    assert "wfg-state" in body and ">done<" in body
    assert "var(--success)" in body  # completed nodes carry the success token
    print("  web: linear completed run draws the overlaid graph with 'done' states OK")


# ============================================================================
# 4. web run-view — correction loop: visits (x2) + a TRAVERSED loop-back
# ============================================================================
def test_web_loop_and_pause():
    _pid, did = _mk_project_doc()
    wf = _mk_loop_wf()
    sids = _step_ids(wf)  # [translate, lqa, review]
    run = _mk_run(wf, did, status=RunStatus.PAUSED)
    # translate x2, lqa x2 (failed then passed), review PAUSED
    _add_stepruns(
        run,
        [
            (sids[0], "completed", 0, None, None),
            (sids[1], "failed", 0, 40.0, "failed"),
            (sids[0], "completed", 1, None, None),
            (sids[1], "completed", 1, 88.0, "passed"),
            (sids[2], "paused", 0, None, None),
        ],
    )
    client = TestClient(app)
    r = client.get(f"/runs/{run}")
    assert r.status_code == 200, r.text
    body = r.text
    # visit chip x2 on the looped nodes
    assert "&#215;2" in body, "loop visit chip x2 must render"
    # a TRAVERSED loop-back is drawn SOLID (thicker), not dashed
    assert 'stroke-width="2.6"' in body, "traversed loop-back must be emphasised (solid)"
    # the paused human-review step reads 'paused'
    assert ">paused<" in body, body[body.find("wfg-state") : body.find("wfg-state") + 400]
    print("  web: correction loop shows x2 visits + a solid traversed loop-back + a paused step OK")


# ============================================================================
# 5. web run-view — a run_condition SKIP + a not-reached step
# ============================================================================
def test_web_skip_and_not_reached():
    _pid, did = _mk_project_doc()
    wf = _mk_skip_wf()
    sids = _step_ids(wf)  # [translate, fix]
    run = _mk_run(wf, did, status=RunStatus.COMPLETED)
    # translate completed; fix SKIPPED by its run_condition
    _add_stepruns(
        run, [(sids[0], "completed", 0, 95.0, "passed"), (sids[1], "skipped", 0, None, None)]
    )
    client = TestClient(app)
    body = client.get(f"/runs/{run}").text
    assert ">skipped<" in body, "a run_condition skip must read 'skipped'"

    # a run that never reached the 2nd step -> 'not run'
    run2 = _mk_run(wf, did, status=RunStatus.FAILED)
    _add_stepruns(run2, [(sids[0], "failed", 0, None, None)])
    body2 = client.get(f"/runs/{run2}").text
    assert ">failed<" in body2 and ">not run<" in body2, "unreached node reads 'not run'"
    print("  web: run_condition skip reads 'skipped'; an unreached node reads 'not run' OK")


# ============================================================================
# 6. web run-view — ORPHAN step run (workflow edited mid-flight) degrades, no 500
# ============================================================================
def test_web_orphan_degrades():
    _pid, did = _mk_project_doc()
    wf = _mk_linear_wf()
    other = _mk_linear_wf()  # a DIFFERENT workflow, whose step ids are valid FKs but
    other_sids = _step_ids(other)  # not part of `wf`'s current graph -> orphan
    sids = _step_ids(wf)
    run = _mk_run(wf, did, status=RunStatus.COMPLETED)
    _add_stepruns(
        run, [(sids[0], "completed", 0, None, None), (other_sids[0], "completed", 0, None, None)]
    )  # <- orphan
    client = TestClient(app)
    r = client.get(f"/runs/{run}")
    assert r.status_code == 200, "an orphan step run must never 500"
    assert "no longer exist" in r.text, "the orphan is surfaced honestly"
    print(
        "  web: an orphan StepRun (workflow edited after the run) degrades to an honest "
        "note, never 500s OK"
    )


# ============================================================================
# 7. headless parity — /api + CLI expose step_states/orphan_steps
# ============================================================================
def test_headless_parity():
    _pid, did = _mk_project_doc()
    wf = _mk_loop_wf()
    sids = _step_ids(wf)
    run = _mk_run(wf, did, status=RunStatus.COMPLETED)
    _add_stepruns(
        run,
        [
            (sids[0], "completed", 0, None, None),
            (sids[1], "failed", 0, 40.0, "failed"),
            (sids[0], "completed", 1, None, None),
            (sids[1], "completed", 1, 88.0, "passed"),
        ],
    )
    # --- /api/runs/{id} ---
    admin = security.create_user(_uniq("rvapi") + "@x.io", "rvapi", "admin", "fixture-pass-1234!!")
    _row, raw = apikeys.create_key(admin.id, "rv-key", scopes="api docs")
    h = {"X-Loc-Token": raw}
    client = TestClient(app)
    g = client.get(f"/api/runs/{run}", headers=h)
    assert g.status_code == 200, g.text
    gb = g.json()
    assert "step_states" in gb and "orphan_steps" in gb, gb
    by_key = {r["key"]: r for r in gb["step_states"]}
    assert by_key["translate"]["visits"] == 2, gb["step_states"]
    assert by_key["lqa"]["visits"] == 2 and by_key["lqa"]["gate"] == "passed", gb
    # per-edge loop-back traversal is available headless (parity with the graph's xN)
    assert gb["loopback_traversals"] == [
        {"source_key": "lqa", "slot": "on_fail", "target_key": "translate", "count": 1}
    ], gb["loopback_traversals"]
    # --- CLI show-run --json (in-process) matches the /api shape ---
    code, out, _err = _run_cli(["--json", "workflow", "show-run", str(run)])
    assert code == 0, out
    import json as _json

    payload = _json.loads(out)
    cli_by_key = {r["key"]: r for r in payload["step_states"]}
    assert cli_by_key["translate"]["visits"] == 2, payload["step_states"]
    assert cli_by_key == by_key, "CLI and /api step_states must agree"
    assert payload["loopback_traversals"] == gb["loopback_traversals"], (
        "CLI and /api loopback_traversals must agree"
    )
    # --- CLI human output carries the per-step block + the loop-back line ---
    code, out, _err = _run_cli(["workflow", "show-run", str(run)])
    assert code == 0 and "Steps this run:" in out, out
    assert "translate completed x2" in out, out
    assert "Loop-back: lqa on_fail -> translate x1" in out, out
    print(
        "  headless: /api + CLI (json + human) expose step_states/orphan_steps/"
        "loopback_traversals; visits/gate/edge-counts agree OK"
    )


# ============================================================================
# 8. FINDING 1 — an ORCHESTRATOR-DRIVEN resume-to-completion review run must NOT
#    render 'paused' (the review StepRun row is never resolved on resume)
# ============================================================================
def test_finding1_review_supersede_orchestrator_driven():
    _pid, did = _mk_project_doc_seg("es-ES")
    wf = _mk_review_wf()
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf, did, "es-ES", created_by="t")
    run_id = summary["run_id"]
    assert summary["status"] == "paused", summary
    with Session(engine) as s:
        r = s.get(WorkflowRun, run_id)
        assert r.status == RunStatus.PAUSED, r.status
        done = orchestrator.resume_workflow(s, run_id, "es-ES", "Reviewed target.")
    assert done["status"] == "completed", done
    with Session(engine) as s:
        r = s.get(WorkflowRun, run_id)
        assert r.status == RunStatus.COMPLETED, r.status
        step_runs = list(
            s.exec(
                select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
            ).all()
        )
        steps = orchestrator.get_steps(s, wf)

    # FAIL-PRE-FIX PROOF (durable, no git): the pre-fix code had NO run_status supersede, so
    # calling build_overlay WITHOUT run_status reproduces exactly that path — latest-row-wins
    # reports the resolved review node as 'paused' even though the run COMPLETED (the defect).
    review_id = steps[1].id
    ov_pre = run_overlay.build_overlay(step_runs, steps)  # pre-fix behaviour (no supersede)
    assert ov_pre["by_id"][review_id]["status"] == "paused", (
        "without the terminal-run supersede a completed review lies 'paused'",
        ov_pre["by_id"][review_id],
    )

    # POST-FIX: the terminal run supersedes the stuck paused row to completed/'reviewed'.
    ov = run_overlay.build_overlay(step_runs, steps, run_status=RunStatus.COMPLETED)
    rn = ov["by_id"][review_id]
    assert rn["status"] == "completed" and rn["label"] == "reviewed", rn
    assert rn["token"] == "var(--success)", rn
    # step_states (headless) carries the SUPERSEDED status so web + JSON agree
    ss = {r["key"]: r for r in ov["step_states"]}
    assert ss["review"]["status"] == "completed", ss

    # the GRAPH must NOT show 'paused' for the completed run; it shows 'reviewed'. (The raw
    # StepRun chip in the Steps LIST still reads 'paused' — that is the honest row; the fix
    # is scoped to the graph overlay, so assert on the wfg-state labels specifically.)
    import re

    client = TestClient(app)
    body = client.get(f"/runs/{run_id}").text
    graph_states = re.findall(r"wfg-state[^>]*>([^<]*)<", body)
    assert "reviewed" in graph_states and "paused" not in graph_states, graph_states
    # /api step_states agrees (not paused)
    admin = security.create_user(_uniq("f1api") + "@x.io", "f1api", "admin", "fixture-pass-1234!!")
    _row, raw = apikeys.create_key(admin.id, "f1-key", scopes="api docs")
    gb = client.get(f"/api/runs/{run_id}", headers={"X-Loc-Token": raw}).json()
    api_ss = {r["key"]: r for r in gb["step_states"]}
    assert api_ss["review"]["status"] == "completed", gb["step_states"]

    # a run that is GENUINELY still paused keeps 'paused' (no supersede)
    _pid2, did2 = _mk_project_doc_seg("es-ES")
    wf2 = _mk_review_wf()
    with Session(engine) as s:
        summ2 = orchestrator.run_workflow(s, wf2, did2, "es-ES", created_by="t")
    body2 = client.get(f"/runs/{summ2['run_id']}").text
    assert "paused" in re.findall(r"wfg-state[^>]*>([^<]*)<", body2), (
        "a genuinely-paused run must still read 'paused' on the graph"
    )
    print(
        "  FINDING 1: orchestrator-driven completed review run reads 'reviewed' not "
        "'paused' (web+api+step_states); no-supersede path lies 'paused'; still-paused "
        "run keeps 'paused' OK"
    )


# ============================================================================
# 9. FINDING 2 — two loop-backs to the SAME target: only the edge that actually
#    fired is marked traversed, with the correct per-edge count
# ============================================================================
def test_finding2_two_loopbacks_same_target():
    def mk(**k):
        base = dict(  # noqa: C408
            step_type="llm_agent",
            provider="mock",
            model="m1",
            branch=None,
            qe_gate=False,
            glossary_id=None,
            tm_examples=False,
            redact_pii=False,
            needs_review=False,
            max_visits=None,
            run_condition=None,
            key=None,
            qe_threshold=80,
        )
        base.update(k)
        return SimpleNamespace(**base)

    # translate(0) <- lqaA(1, on_fail) ; mid(2) ; lqaB(3, on_fail -> translate too)
    steps = [
        mk(id=1, key="translate", name="Translate"),
        mk(
            id=2,
            key="lqaA",
            name="Check A",
            step_type="lqa",
            qe_gate=True,
            branch={"on_fail": {"step": "translate"}, "on_pass": "next"},
        ),
        mk(id=3, key="mid", name="Mid"),
        mk(
            id=4,
            key="lqaB",
            name="Check B",
            step_type="lqa",
            qe_gate=True,
            branch={"on_fail": {"step": "translate"}, "on_pass": "next"},
        ),
    ]
    # Execution order (id-ascending): translate, lqaA FAILED (loops), translate again,
    # mid, lqaB PASSED (does NOT loop). Only lqaA's edge fired.
    rows = [
        SimpleNamespace(id=1, step_id=1, status="completed", score=None, gate=None),
        SimpleNamespace(id=2, step_id=2, status="completed", score=40.0, gate="failed"),
        SimpleNamespace(id=3, step_id=1, status="completed", score=None, gate=None),
        SimpleNamespace(id=4, step_id=3, status="completed", score=None, gate=None),
        SimpleNamespace(id=5, step_id=4, status="completed", score=90.0, gate="passed"),
    ]
    ov = run_overlay.build_overlay(rows, steps, run_status="completed")
    # per-edge honesty: ONLY lqaA's on_fail edge is counted, exactly once
    assert ov["traversals"] == {"2:on_fail": 1}, ov["traversals"]

    # HEADLESS parity: the JSON loopback_traversals must match the macro's traversals
    # (same _edge_traversals result, ids translated to stable keys at the seam).
    graph = run_overlay.run_step_states(rows, steps, run_status="completed")
    assert graph["loopback_traversals"] == [
        {"source_key": "lqaA", "slot": "on_fail", "target_key": "translate", "count": 1}
    ], graph["loopback_traversals"]
    # exactly one edge fired -> exactly one solid edge rendered, agreeing with the JSON count
    assert len(graph["loopback_traversals"]) == _render_macro(steps, overlay=ov).count(
        'stroke-width="2.6"'
    ), "JSON loopback count must equal the solid edges drawn"

    # POST-FIX render: exactly ONE solid (traversed) loop-back edge; the other stays dashed
    new = _render_macro(steps, overlay=ov)
    assert new.count('stroke-width="2.6"') == 1, (
        "only the fired edge is solid",
        new.count('stroke-width="2.6"'),
    )
    assert 'stroke-dasharray="4 3"' in new, "the non-fired loop-back must stay dashed"

    # FAIL-PRE-FIX PROOF (durable, no git): the pre-fix rule marked a loop-back traversed
    # when its TARGET node was visited >1. Both lqaA and lqaB loop back to translate, which
    # was visited TWICE, so the old rule marks BOTH edges — the reviewer's over-marking. The
    # new per-edge logic marks only the one that actually fired.
    assert ov["by_id"][1]["visits"] == 2, ("shared target visited twice", ov["by_id"][1])
    edges_to_translate = [
        s
        for s in steps
        if getattr(s, "branch", None) and (s.branch.get("on_fail") or {}).get("step") == "translate"
    ]
    old_rule_marked = sum(1 for _s in edges_to_translate if ov["by_id"][1]["visits"] > 1)
    assert old_rule_marked == 2, ("pre-fix target-visits>1 rule over-marks BOTH", old_rule_marked)
    assert len(ov["traversals"]) == 1, (
        "new per-edge logic marks only the fired edge",
        ov["traversals"],
    )
    print(
        "  FINDING 2: two loop-backs to one target -> only the fired edge is solid (x1) "
        "+ headless loopback_traversals agrees; pre-fix target-visits rule marked BOTH OK"
    )


# ============================================================================
# 10. CLI-parity backlog item 3 — per-VISIT row-level history (--visits / ?visits=1)
#
# step_states (tested above) folds every StepRun row per step into ONE latest-
# wins node — a loop step that failed then re-ran only ever reports its LAST
# status/score/gate, and never exposes provider/model/attempts/error/latency at
# all. visit_history is the additive, OPT-IN gap-filler: one entry PER StepRun
# ROW. Proves: (a) row-level facts survive (the FAILED row's own status/score/
# gate, not superseded by the later PASSED row); (b) default output (no
# --visits / no ?visits=1) carries NO visit_history key -> byte-identical;
# (c) local CLI / /api / --remote CLI all agree.
# ============================================================================
def test_visit_history_row_level_parity():
    _pid, did = _mk_project_doc()
    wf = _mk_loop_wf()
    sids = _step_ids(wf)  # [translate, lqa, review]
    run = _mk_run(wf, did, status=RunStatus.COMPLETED)
    with Session(engine) as s:
        s.add(
            StepRun(
                workflow_run_id=run,
                step_id=sids[0],
                status="completed",
                outcome_index=0,
                provider_used="mock",
                model_used="m1",
                attempts=1,
                latency_ms=12,
            )
        )
        s.add(
            StepRun(
                workflow_run_id=run,
                step_id=sids[1],
                status="failed",
                outcome_index=0,
                score=40.0,
                gate="failed",
                provider_used="mock",
                model_used="m1",
                error="LQA below threshold",
            )
        )
        s.add(
            StepRun(
                workflow_run_id=run,
                step_id=sids[0],
                status="completed",
                outcome_index=1,
                provider_used="mock",
                model_used="m1",
                attempts=1,
                latency_ms=9,
            )
        )
        s.add(
            StepRun(
                workflow_run_id=run,
                step_id=sids[1],
                status="completed",
                outcome_index=1,
                score=88.0,
                gate="passed",
                provider_used="mock",
                model_used="m1",
            )
        )
        s.commit()

    # --- unit: row-level facts survive; step_states would have folded these away ---
    with Session(engine) as s:
        step_runs = list(
            s.exec(select(StepRun).where(StepRun.workflow_run_id == run).order_by(StepRun.id)).all()
        )
        steps = orchestrator.get_steps(s, wf)
    vh = run_overlay.visit_history(step_runs, steps)
    assert len(vh) == 4, vh
    lqa_visits = [v for v in vh if v["key"] == "lqa"]
    assert [v["visit"] for v in lqa_visits] == [1, 2], lqa_visits
    assert lqa_visits[0]["status"] == "failed" and lqa_visits[0]["gate"] == "failed", lqa_visits[0]
    assert lqa_visits[0]["error"] == "LQA below threshold", lqa_visits[0]
    assert lqa_visits[1]["status"] == "completed" and lqa_visits[1]["gate"] == "passed", lqa_visits[
        1
    ]
    # step_states, by contrast, folds lqa to its LAST row only (gate=passed, no
    # trace of the earlier failure) -- proving visit_history is a REAL gap-fill,
    # not a duplicate of the existing aggregate.
    ss = run_overlay.run_step_states(step_runs, steps, run_status="completed")
    lqa_state = next(r for r in ss["step_states"] if r["key"] == "lqa")
    assert lqa_state["gate"] == "passed" and lqa_state["visits"] == 2, lqa_state
    translate_visits = [v for v in vh if v["key"] == "translate"]
    assert [v["latency_ms"] for v in translate_visits] == [12, 9], translate_visits
    print(
        "  visit_history unit: row-level status/score/gate/error survive per visit "
        "(step_states would fold lqa's failure away) OK"
    )

    # --- default show-run (no --visits): NO visit_history key -> byte-identical ---
    code, out, _err = _run_cli(["--json", "workflow", "show-run", str(run)])
    assert code == 0, out
    import json as _json

    default_payload = _json.loads(out)
    assert "visit_history" not in default_payload, default_payload
    print(
        "  visit_history: omitted from the default (no --visits) payload -> "
        "byte-identical to before this feature OK"
    )

    # --- CLI --visits --json ---
    code, out, _err = _run_cli(["--json", "workflow", "show-run", str(run), "--visits"])
    assert code == 0, out
    cli_payload = _json.loads(out)
    assert "visit_history" in cli_payload and len(cli_payload["visit_history"]) == 4, cli_payload

    # --- /api ?visits=1 agrees ---
    admin = security.create_user(_uniq("vhapi") + "@x.io", "vhapi", "admin", "fixture-pass-1234!!")
    _row, raw = apikeys.create_key(admin.id, "vh-key", scopes="api docs")
    client = TestClient(app)
    g = client.get(f"/api/runs/{run}?visits=1", headers={"X-Loc-Token": raw})
    assert g.status_code == 200, g.text
    api_payload = g.json()
    assert api_payload["visit_history"] == cli_payload["visit_history"], (
        "CLI --visits and /api ?visits=1 must agree"
    )
    # /api WITHOUT ?visits=1 carries no visit_history either (same additive contract).
    g_light = client.get(f"/api/runs/{run}", headers={"X-Loc-Token": raw})
    assert "visit_history" not in g_light.json(), g_light.json()

    # --- CLI human output carries the per-visit block ---
    code, out, _err = _run_cli(["workflow", "show-run", str(run), "--visits"])
    assert code == 0 and "Per-visit history:" in out, out
    assert "lqa visit=1 failed" in out and "gate=failed" in out, out
    assert "lqa visit=2 completed" in out and "gate=passed" in out, out
    assert "ERROR: LQA below threshold" in out, out
    print(
        "  visit_history: CLI --visits --json == /api ?visits=1 (local+remote parity); "
        "human output renders the per-visit block OK"
    )


# ============================================================================
# 11. visit_history — ORPHAN StepRun (test gap flagged in review): a workflow
# edited/deleted after the run leaves StepRun rows whose step id is no longer in
# the current graph. build_overlay/step_states already handle this honestly
# (orphan_steps); visit_history must too, at the row level: index/key=None,
# never a crash, never silently dropped, and never mixed up with the run's own
# (still-current) steps. Pure unit (SimpleNamespace rows), independent fixtures.
# ============================================================================
def test_visit_history_orphan_step():
    steps = [
        SimpleNamespace(id=10, key="translate", name="Translate"),
        SimpleNamespace(id=11, key="lqa", name="Check"),
    ]
    rows = [
        SimpleNamespace(
            id=1,
            step_id=10,
            status="completed",
            score=None,
            gate=None,
            provider_used="mock",
            model_used="m1",
            attempts=1,
            error=None,
            latency_ms=7,
            started_at=None,
            finished_at=None,
        ),
        SimpleNamespace(
            id=2,
            step_id=11,
            status="completed",
            score=92.0,
            gate="passed",
            provider_used="mock",
            model_used="m1",
            attempts=1,
            error=None,
            latency_ms=15,
            started_at=None,
            finished_at=None,
        ),
        # an ORPHAN — step 99 is not in the current graph (workflow edited after
        # the run), visited TWICE.
        SimpleNamespace(
            id=3,
            step_id=99,
            status="completed",
            score=None,
            gate=None,
            provider_used="mock",
            model_used="m1",
            attempts=2,
            error="orphan visit one",
            latency_ms=5,
            started_at=None,
            finished_at=None,
        ),
        SimpleNamespace(
            id=4,
            step_id=99,
            status="failed",
            score=None,
            gate=None,
            provider_used=None,
            model_used=None,
            attempts=1,
            error="orphan visit two",
            latency_ms=None,
            started_at=None,
            finished_at=None,
        ),
    ]
    vh = run_overlay.visit_history(rows, steps)
    assert len(vh) == 4, vh

    # the run's OWN (still-current) steps resolve index/key normally, unaffected
    # by the orphan rows being present in the same run.
    own = [v for v in vh if v["step_id"] != 99]
    assert len(own) == 2, own
    assert all(v["index"] is not None and v["key"] is not None for v in own), own
    assert {v["key"] for v in own} == {"translate", "lqa"}, own

    # the orphan step gets an honest per-visit entry: index/key=None, its OWN
    # status/error intact per row, correct 1-based visit numbering, never crashes.
    orphan = [v for v in vh if v["step_id"] == 99]
    assert len(orphan) == 2, orphan
    assert [v["index"] for v in orphan] == [None, None], orphan
    assert [v["key"] for v in orphan] == [None, None], orphan
    assert [v["visit"] for v in orphan] == [1, 2], orphan
    assert orphan[0]["status"] == "completed" and orphan[0]["error"] == "orphan visit one", orphan[
        0
    ]
    assert orphan[1]["status"] == "failed" and orphan[1]["error"] == "orphan visit two", orphan[1]

    # never raises on totally empty/junk input either (mirrors build_overlay's
    # robustness contract, which visit_history's docstring claims).
    assert run_overlay.visit_history([], steps) == []
    assert run_overlay.visit_history(None, None) == []

    # the ASCII renderer marks an orphan visit distinctly ("[orphan]", not a
    # numeric index) and never crashes on one either.
    block = run_overlay.format_visit_history_ascii(vh)
    assert "[orphan]" in block, block
    assert block.count("[orphan]") == 2, block
    print(
        "  visit_history: an ORPHAN StepRun (workflow edited after the run) gets "
        "an honest per-visit entry (index/key=None, own status/error/visit-number "
        "intact); own steps unaffected; never crashes on empty/junk input; ASCII "
        "renderer marks it [orphan] OK"
    )


import re as _re


def _gantt_region(body):
    """The rendered .rv-gantt block, or '' when absent."""
    m = _re.search(r'<div class="rv-gantt"[^>]*>.*?</div>\s*</section>', body, _re.DOTALL)
    return m.group(0) if m else ""


def _graph_card(body):
    """The <section ... id="runGraphCard"> ... </section> slice, or ''."""
    m = _re.search(r'<section class="card rv-graphcard"[^>]*>.*?</section>', body, _re.DOTALL)
    return m.group(0) if m else ""


def _add_timed_stepruns(run_id, rows):
    """rows = [(step_id, status, outcome_index, latency_ms, cost_usd), ...] — like
    _add_stepruns but carrying the per-visit timing/spend the Gantt reads."""
    with Session(engine) as s:
        for sid, status, oi, lat, cost in rows:
            s.add(
                StepRun(
                    workflow_run_id=run_id,
                    step_id=sid,
                    status=status,
                    outcome_index=oi,
                    latency_ms=lat,
                    cost_usd=cost,
                    provider_used="mock",
                    model_used="m1",
                )
            )
        s.commit()


# ============================================================================
# 12. GANTT — per-step-visit bars with durations/costs, incl. a loop (two bars
#     for the revisited step), grouped under the step, with a run-total footer
# ============================================================================
def test_gantt_bars_loop_and_totals():
    _pid, did = _mk_project_doc()
    wf = _mk_loop_wf()
    sids = _step_ids(wf)  # [translate, lqa, review]
    run = _mk_run(wf, did, status=RunStatus.COMPLETED)
    # translate x2 (latency 40ms/10ms, cost .01/.02), lqa x2 (latency present),
    # review (NULL latency — human_review never records one).
    _add_timed_stepruns(
        run,
        [
            (sids[0], "completed", 0, 40, 0.01),
            (sids[1], "completed", 0, 8, 0.005),
            (sids[0], "completed", 1, 10, 0.02),
            (sids[1], "completed", 1, 6, 0.004),
            (sids[2], "completed", 0, None, None),
        ],
    )
    client = TestClient(app)
    body = client.get(f"/runs/{run}").text
    g = _gantt_region(body)
    assert g, "the Gantt card must render for a run with recorded step visits"
    assert "Step timeline" in body
    # the looped step contributes TWO bars -> two visit chips (#1, #2)
    assert ">#1<" in g and ">#2<" in g, "loop visits render as separate bars (#1, #2)"
    # honest per-visit durations appear
    assert "40ms" in g and "10ms" in g, "each visit's own latency labels the bar"
    # bar width scales to the longest visit: the 40ms visit is the max -> width 100
    assert 'width="100"' in g, g[:600]
    # per-visit cost annotations (data-mono $), and a run-total footer
    assert "$0.0100" in g and "$0.0200" in g, "per-visit cost annotations render"
    # run total = sum of visit costs (.01+.005+.02+.004 = .039)
    assert "$0.0390" in g, "the footer totals the run's per-visit spend"
    assert "Run total" in g
    print(
        "  GANTT: loop step draws two bars (#1/#2) with per-visit durations + costs; "
        "widths scale to the longest visit; footer totals spend OK"
    )


# ============================================================================
# 13. GANTT — a legacy/untracked visit with NULL cost OMITS the $ annotation
#     (never rendered as $0), while its duration bar still draws
# ============================================================================
def test_gantt_null_cost_omitted():
    _pid, did = _mk_project_doc()
    wf = _mk_linear_wf()
    sids = _step_ids(wf)
    run = _mk_run(wf, did, status=RunStatus.COMPLETED)
    # latency present, cost NULL (a legacy/untracked run predating spend tracking)
    _add_timed_stepruns(
        run, [(sids[0], "completed", 0, 15, None), (sids[1], "completed", 0, 7, None)]
    )
    client = TestClient(app)
    g = _gantt_region(client.get(f"/runs/{run}").text)
    assert g, "the Gantt still renders for a cost-less run"
    assert "15ms" in g and "7ms" in g, "durations still label the bars"
    assert "$" not in g, "a NULL-cost visit must OMIT the annotation, never show $0"
    print(
        "  GANTT: a NULL-cost (legacy/untracked) visit omits the $ annotation entirely "
        "while its duration bar still draws OK"
    )


# ============================================================================
# 14. STEPS LIST supersede (Run View v2 #3) — a COMPLETED run's stuck 'paused'
#     human_review row reads 'reviewed' in the flat Steps list too, so the list
#     and the graph can never disagree. FAILS pre-fix (the old list rendered the
#     raw sr.status).
# ============================================================================
def test_steps_list_supersede_completed_review():
    _pid, did = _mk_project_doc_seg("es-ES")
    wf = _mk_review_wf()
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf, did, "es-ES", created_by="t")
        run_id = summary["run_id"]
        orchestrator.resume_workflow(s, run_id, "es-ES", "Reviewed target.")
        r = s.get(WorkflowRun, run_id)
        assert r.status == RunStatus.COMPLETED, r.status
        # the review StepRun row is GENUINELY still 'paused' in the DB (never resolved)
        rows = list(
            s.exec(
                select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
            ).all()
        )
    assert any(sr.status == "paused" for sr in rows), (
        "precondition: the stuck human_review row is raw 'paused' in the DB"
    )

    # FAIL-PRE-FIX (durable, no git): the old Steps list rendered m.status_badge(sr.status)
    # RAW, so the stuck paused row showed a 'paused' badge on a COMPLETED run. The fix
    # single-sources the graph's terminal supersede via display_status -> 'reviewed'.
    raw = ("paused", None)  # what the old code fed status_badge
    disp = run_overlay.display_status("paused", RunStatus.COMPLETED)
    assert raw != disp, "the supersede must change the displayed status (fails pre-fix)"
    assert disp == ("completed", "reviewed"), disp
    # a genuinely non-terminal run does NOT supersede (still shows the row honestly)
    assert run_overlay.display_status("paused", RunStatus.RUNNING) == ("paused", None)
    assert run_overlay.display_status("paused", RunStatus.PAUSED) == ("paused", None)

    client = TestClient(app)
    body = client.get(f"/runs/{run_id}").text
    # the Steps-list badge (class="badge ...") reads 'reviewed'; no 'paused' badge remains
    assert 'title="reviewed"' in body, "the Steps list must show a 'reviewed' badge"
    assert 'class="badge paused"' not in body, (
        "no stale 'paused' badge may remain on a completed run's Steps list"
    )
    print(
        "  STEPS LIST: a completed run's stuck-paused review row reads 'reviewed' in the "
        "flat list (single-sourced with the graph); raw row stays 'paused'; fails pre-fix; "
        "non-terminal runs keep 'paused' OK"
    )


# ============================================================================
# 15. FRAGMENT endpoint — returns the graph+Gantt card for an authorized user,
#     is BYTE-CONSISTENT with the full-page card, and mirrors the run page's
#     not-found posture.
# ============================================================================
def test_graph_fragment_endpoint():
    _pid, did = _mk_project_doc()
    wf = _mk_loop_wf()
    sids = _step_ids(wf)
    run = _mk_run(wf, did, status=RunStatus.COMPLETED)
    _add_timed_stepruns(
        run,
        [
            (sids[0], "completed", 0, 40, 0.01),
            (sids[1], "completed", 0, 8, 0.005),
            (sids[0], "completed", 1, 10, 0.02),
            (sids[1], "completed", 1, 6, 0.004),
        ],
    )
    client = TestClient(app)
    # authorized user gets the card
    fr = client.get(f"/runs/{run}/graph-fragment")
    assert fr.status_code == 200, fr.text
    frag = fr.text
    assert 'id="runGraphCard"' in frag and "How this run went" in frag
    assert "Step timeline" in frag and "wf-graph-svg" in frag

    # BYTE-CONSISTENT with the same card embedded in the full run page
    full_card = _graph_card(client.get(f"/runs/{run}").text)
    frag_card = _graph_card(frag)
    assert full_card and frag_card, "both must contain the graph card"
    assert full_card == frag_card, "fragment card must be byte-identical to the page card"

    # not-found posture identical to the run page (both redirect the same way)
    bad = 99999
    page = client.get(f"/runs/{bad}", follow_redirects=False)
    frg = client.get(f"/runs/{bad}/graph-fragment", follow_redirects=False)
    assert page.status_code == frg.status_code, (page.status_code, frg.status_code)
    assert page.headers.get("location") == frg.headers.get("location"), (
        "fragment 404 posture must match the run page"
    )
    print(
        "  FRAGMENT: /runs/{id}/graph-fragment returns the card (200), is byte-identical "
        "to the full-page card, and mirrors the run page's not-found posture OK"
    )


# ============================================================================
# 16. SSE script references the fragment endpoint (static assertion) — the live
#     handler fetches + swaps the graph card on step/paused/done events.
# ============================================================================
def test_sse_script_references_fragment():
    _pid, did = _mk_project_doc()
    wf = _mk_linear_wf()
    run = _mk_run(wf, did, status=RunStatus.RUNNING)
    client = TestClient(app)
    body = client.get(f"/runs/{run}?live=1").text
    assert "graph-fragment" in body, "the live SSE script must fetch the graph-fragment"
    assert "refreshGraph" in body, "the live script must define the bounded refresh"
    refresh = body.split("function refreshGraph()", 1)[1].split(".finally", 1)[0]
    assert "window.locA11yEnhance" in refresh, "replacement Gantt must regain keyboard access"
    # the static (no-JS) card is still present, so it degrades gracefully
    assert 'id="runGraphCard"' in body
    print(
        "  SSE: the live run page's script references /runs/{id}/graph-fragment + "
        "refreshGraph, and the static card is present for the no-JS path OK"
    )


def test_step_notes_are_not_failures():
    _pid, did = _mk_project_doc()
    wf = _mk_linear_wf()
    sid = _step_ids(wf)[0]
    client = TestClient(app)
    for status in ("completed", "skipped", "failed"):
        run = _mk_run(wf, did, status=RunStatus.COMPLETED)
        with Session(engine) as s:
            s.add(
                StepRun(
                    workflow_run_id=run,
                    step_id=sid,
                    status=status,
                    error="LQA score 96 passed <img src=x>",
                )
            )
            s.commit()
        response = client.get(f"/runs/{run}")
        assert response.status_code == 200
        head = next(
            h
            for h in re.findall(r'<div class="step-head">(.*?)</div>', response.text, re.S)
            if "LQA score 96" in h
        )
        assert ('class="error-inline"' in head) == (status == "failed"), head
        assert (">Failed</span>" in head) == (status == "failed"), head
        assert ('class="muted small step-note"' in head) == (status != "failed"), head
        assert "&lt;img src=x&gt;" in head and "<img src=x>" not in head
    print("  Step notes: completed/skipped neutral, failed red, all escaped OK")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failures += 1
                import traceback

                print(f"  FAIL {name}: {exc}")
                traceback.print_exc()
    if failures:
        print(f"verify_run_view_graph FAILED ({failures})")
        raise SystemExit(1)
    print("RUN VIEW GRAPH OK")
