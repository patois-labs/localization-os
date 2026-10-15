"""Configurable LQA pass threshold on a PARALLEL lane member (chip task_8face0e6) -- hermetic.

The lqa_threshold wave wired the per-member pass bar into BOTH run loops
(orchestrator._run_parallel_group passes each member's own `lqa_threshold` to
lqa.score_text, exactly as the sequential path does), but only the SEQUENTIAL gate was
pinned by test (verify_lqa_threshold). This adds the missing parallel-path proof: an
`lqa_threshold` on an lqa step INSIDE a parallel group gates the SAME score IDENTICALLY
to the same threshold on a serial lqa step.

The proof uses REAL scoring (deterministic FORBID rulebook -> a single document that
scores EXACTLY 92, the same mechanism verify_lqa_threshold uses), NOT the gate-forcing
stub, so the THRESHOLD -- not a stubbed verdict -- is what decides pass/fail:

  (1) a 2-member parallel LQA group [gate@94, gate@unset] over a 92-scoring document:
      the 94 member's StepRun is score=92 / gate='failed'; the unset (default 90)
      member's is score=92 / gate='passed'. Same frozen snapshot, routed by threshold
      alone -- INSIDE a fork.
  (2) IDENTICAL to serial: a standalone lqa step at 94 fails the same 92, and at unset
      passes it -- the group member StepRuns match their serial twins exactly
      (score + gate).

Hermetic: throwaway LOC_TMS_DATA_DIR + MockProvider. ASCII-only.
Final line: "LQA THRESHOLD PARALLEL OK".
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_lqathrpar_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import StepRun, StepType
from app.services import orchestrator
from app.services import workflows as wf

init_db()

_SRC, _TGT = "en-US", "fr-FR"
_TOKENS = ["zqa", "zqb", "zqc", "zqd", "zqe", "zqf", "zqg", "zqh"]  # 8 -> score 100-8=92


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _write_rulebook():
    rb_dir = config.DATA_DIR / "rulebooks"
    rb_dir.mkdir(parents=True, exist_ok=True)
    (rb_dir / f"{_SRC}__{_TGT}.md").write_text(
        "\n".join(f"FORBID {t}" for t in _TOKENS) + "\n", encoding="utf-8"
    )


def _docx(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def _new_project(client, name):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": _SRC,
            "target_locales": _TGT,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _upload(client, pid):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                _docx([" ".join(_TOKENS) + " alpha bravo charlie"]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _new_wf(name):
    from app.models import Workflow

    with Session(engine) as s:
        w = Workflow(name=name, scope="global", source_locale=_SRC, target_locale=_TGT)
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _translate(wid):
    with Session(engine) as s:
        return wf.add_step(
            s,
            wid,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        ).id


def _lqa(wid, name, **kw):
    with Session(engine) as s:
        return wf.add_step(s, wid, name=name, step_type=StepType.LQA, provider="mock", **kw).id


def _stepruns(run_id):
    with Session(engine) as s:
        return list(
            s.exec(
                select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
            ).all()
        )


def _run(client, wid, name):
    pid = _new_project(client, name)
    doc = _upload(client, pid)
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wid, doc, _TGT)
    return summary


def _sr_for(run_id, step_id):
    hits = [sr for sr in _stepruns(run_id) if sr.step_id == step_id]
    if not hits:
        _fail(f"no StepRun for step {step_id} in run {run_id}")
    return hits[-1]


# ============================================================================ #
# (1) parallel group: same 92, routed by each member's own threshold
# ============================================================================ #
def test_parallel_group_gates_by_member_threshold():
    _write_rulebook()
    client = TestClient(app)

    wid = _new_wf("lqathrpar-group")
    _translate(wid)
    a_id = _lqa(wid, "LQA @94", key="lqa_a", parallel_group="checks", lqa_threshold=94)
    b_id = _lqa(wid, "LQA default", key="lqa_b", parallel_group="checks")

    # sanity: the two lqa steps really formed a 2-member group
    with Session(engine) as s:
        st = orchestrator.get_steps(s, wid)
    if [x.parallel_group for x in st] != [None, "checks", "checks"]:
        _fail(f"expected a 2-member group, got {[x.parallel_group for x in st]}")

    summary = _run(client, wid, "lqathrpar-group")
    if summary["status"] not in ("completed", "paused"):
        _fail(f"parallel run did not finish cleanly: {summary}")

    sr_a = _sr_for(summary["run_id"], a_id)
    sr_b = _sr_for(summary["run_id"], b_id)
    if sr_a.score != 92.0:
        _fail(f"group member @94 must score 92, got {sr_a.score}")
    if sr_a.gate != "failed":
        _fail(f"group member @94 must gate 'failed' for a 92, got {sr_a.gate}")
    if sr_b.score != 92.0:
        _fail(f"group member @default must score 92, got {sr_b.score}")
    if sr_b.gate != "passed":
        _fail(f"group member @default (90) must gate 'passed' for a 92, got {sr_b.gate}")
    print(
        "  (1) parallel lane: 92 FAILS @94, PASSES @default -- routed by member threshold alone: OK"
    )
    return sr_a, sr_b


# ============================================================================ #
# (2) IDENTICAL to the serial path
# ============================================================================ #
def test_matches_serial_path():
    _write_rulebook()
    client = TestClient(app)
    group_a, group_b = test_parallel_group_gates_by_member_threshold()

    # serial @94
    wid94 = _new_wf("lqathrpar-serial94")
    _translate(wid94)
    s94 = _lqa(wid94, "LQA @94", key="lqa", lqa_threshold=94)
    r94 = _run(client, wid94, "lqathrpar-serial94")
    sr94 = _sr_for(r94["run_id"], s94)

    # serial @unset (default 90)
    widd = _new_wf("lqathrpar-serialdef")
    _translate(widd)
    sdef = _lqa(widd, "LQA default", key="lqa")
    rd = _run(client, widd, "lqathrpar-serialdef")
    srd = _sr_for(rd["run_id"], sdef)

    if (sr94.score, sr94.gate) != (group_a.score, group_a.gate):
        _fail(
            f"parallel @94 {(group_a.score, group_a.gate)} must equal serial @94 "
            f"{(sr94.score, sr94.gate)}"
        )
    if (srd.score, srd.gate) != (group_b.score, group_b.gate):
        _fail(
            f"parallel @default {(group_b.score, group_b.gate)} must equal serial "
            f"@default {(srd.score, srd.gate)}"
        )
    # and the whole point: same score, DIFFERENT gate by threshold
    if sr94.gate == srd.gate:
        _fail("the 94 gate and the default gate must DIFFER on the same 92 score")
    print("  (2) parallel-lane gates match the serial path exactly (score + gate): OK")


def main():
    test_matches_serial_path()
    print("LQA THRESHOLD PARALLEL OK")


if __name__ == "__main__":
    main()
