"""LQA correction-loop honesty + judge attribution + the per-step visit ceiling.

Closes three prod findings from the 2026-09-06 night stress pass (lane L5):

  #95  A correction loop whose "repair" changes nothing degenerates into RE-ROLLING the
       same non-deterministic judge on the SAME BYTES until it happens to pass. On prod
       the judge graded byte-identical text `critical`/89/failed on visit 1 and
       `minor`/97/auto_approved on visit 2, and the run DELIVERED text carrying a literal
       'TODO-FIXME' in 100% of segments. FIX: a re-entry into an LQA step whose judged
       text is BYTE-IDENTICAL to what that same step judged on its previous visit is NOT
       re-judged. The previous verdict is carried forward unchanged and, when it was a
       FAIL, the run HOLDS at that step (never falls through to a downstream deliver
       step) with a named reason. No second lottery ticket.

  #96  An `lqa` step pinned to `--provider engine:1` recorded `provider null / attempts 0
       / elapsed_ms null` in `failure_shape` while its judge burned real tokens, and
       `report run tokens --group-by engine` filed those tokens under an unexplained
       `(none)`. So a
       FAILING judge call had NO visible failure shape at all. FIX: the LQA step stamps
       the judge call's resolved provider/model/attempts/finish_reason/error_class and the
       visit's elapsed on its StepRun, exactly like an `llm_agent` step does.

  #94  `--max-visits` is documented as a "per-step per-run execution ceiling" but only
       bound a step as a loop-back TARGET, so an `lqa` step with `--max-visits 1` executed
       twice. FIX: the ceiling is enforced BEFORE a step executes, whatever role it plays
       in the graph; a step that has already run its ceiling is not run again and the run
       ends there gracefully (soft cap, flagged in run.context.loop_ended).

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys, ASCII-only prints.
A deterministic IDEMPOTENT "repair" provider (append the marker once, then no-op) is the
offline stand-in for prod's poisoned MT prompt.
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_lqaloop_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, quality, reports, run_overlay, settings
from app.services.providers import Provider, register_provider

MARKER = " TODO-FIXME"


class IdempotentRepair(Provider):
    """The offline stand-in for prod's poisoned MT step: it appends a marker ONCE and is a
    no-op on text that already carries it, so f(f(x)) == f(x). A loop-back through it
    therefore hands the judge BYTE-IDENTICAL text on the next visit -- the "repair that
    changed nothing" shape #95 is about."""

    name = "idemrepair"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        IdempotentRepair.calls += 1
        text = (payload or {}).get("text", "")
        return text if text.endswith(MARKER) else text + MARKER


class GrowingRepair(Provider):
    """The CONTROL: a repair that genuinely changes the text on every visit, so the judge
    is legitimately re-run. Proves the no-progress guard is not 'always hold'."""

    name = "growrepair"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        GrowingRepair.calls += 1
        text = (payload or {}).get("text", "")
        return f"{text} v{GrowingRepair.calls}"


class LqaFailThenPass(Provider):
    """A live judge that FAILS its first `n` calls (a critical error) then PASSES. On the
    identical-bytes loop this is exactly the prod re-roll: the second call would flip the
    same text from failed to auto_approved."""

    name = "lqaftp"

    def __init__(self, n=1):
        self.n = n
        self.calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.calls += 1
        if self.calls <= self.n:
            return '{"errors":[{"dimension":"accuracy","severity":"critical","note":"bad"}]}'
        return '{"errors":[]}'


class OscillatingRepair(Provider):
    """A "repair" that ALTERNATES between two outputs (x <-> x + " X"). Every visit changes
    the bytes, so a newest-row-only no-progress rule sees progress forever and the re-roll
    lottery is fully intact -- the gap gate C round 1 M3 names. Returning to text this gate
    has ALREADY judged is not progress, whichever visit judged it."""

    name = "oscrepair"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        OscillatingRepair.calls += 1
        text = (payload or {}).get("text", "")
        return text[:-2] if text.endswith(" X") else text + " X"


class LqaAlwaysPass(Provider):
    name = "lqaalwayspass"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        LqaAlwaysPass.calls += 1
        return '{"errors":[]}'


class LqaAlwaysFail(Provider):
    name = "lqaalwaysfail"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        LqaAlwaysFail.calls += 1
        return '{"errors":[{"dimension":"accuracy","severity":"critical","note":"bad"}]}'


class MangleEdit(Provider):
    """A POST-EDIT step: it rewrites whatever it is given. Round 3's M1 shape -- a producer
    AFTER the passing check, so the text that ships is text the check never saw."""

    name = "mangle"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        MangleEdit.calls += 1
        return "MANGLED:" + ((payload or {}).get("text", "") or "")


class MangleSecondOnly(Provider):
    """The per-UNIT control for M1: it rewrites only the segment containing "two", so one
    unit of the document is post-edited and the other is exactly the text the check
    passed."""

    name = "secondonly"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        MangleSecondOnly.calls += 1
        text = (payload or {}).get("text", "") or ""
        return ("MANGLED:" + text) if "two" in text else text


class QEJudge(Provider):
    """The QE ROUTING gate's provider (round 3, H2): it translates, and as a `qe` judge it
    scores a target containing 'BAD' far below any sane threshold and everything else 100.
    Mirrors `verify_qe_gate`'s steering fixture."""

    name = "qejudge"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        QEJudge.calls += 1
        if task == "qe":
            tgt = (payload or {}).get("target", "")
            if "BAD" in tgt:
                return (
                    '{"accuracy":3,"fluency":1,"terminology":0,"style":0,'
                    '"rationale":"multiple mistranslations"}'
                )
            return '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"clean"}'
        return (payload or {}).get("text", prompt or "")


class LqaDeadJudge(Provider):
    """A CONFIGURED-LIVE judge that returns an EMPTY completion -> JUDGE_UNAVAILABLE. The
    #96 case that matters most: a judge call that FAILED must still have a failure shape."""

    name = "lqadead"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        LqaDeadJudge.calls += 1
        return ""


class QECleanRepair(Provider):
    """Round 4, arm (35): the SAME provider slot as `QEJudge`, re-registered for the CLEAN
    re-run. It repairs the body and scores every target clean, so the identical one-step
    `qe_gate` workflow that recorded E1's loop-ending hold now ends with its own gate
    PASSING -- the release path the round-3 scope had closed."""

    name = "qejudge"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        QECleanRepair.calls += 1
        if task == "qe":
            return '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"clean"}'
        return "FIXED one."


class RetypingJudge(Provider):
    """Round 4, arm (36): a judge that RETYPES its own step row (`lqa` -> `llm_agent`, no QE
    gate) WHILE the run is executing, and then fails the text. The mid-run neighbour of
    round 2's deleted-step case: the verdict row survives, its step no longer looks like a
    check, and dropping the row would let gate-failed text download clean."""

    name = "retypejudge"
    wf_id = None

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if RetypingJudge.wf_id is not None:
            with Session(engine) as s:
                for st in s.exec(
                    select(WorkflowStep).where(WorkflowStep.workflow_id == RetypingJudge.wf_id)
                ).all():
                    if st.step_type == StepType.LQA:
                        st.step_type = StepType.LLM_AGENT
                        s.add(st)
                s.commit()
        return '{"errors":[{"dimension":"accuracy","severity":"critical","note":"bad"}]}'


# --- fixtures ---------------------------------------------------------------


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


def _new_wf(name):
    with Session(engine) as s:
        wf = Workflow(name=name, scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        return wf.id


def stepruns_for(run_id):
    with Session(engine) as s:
        return s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()


def _runs_for_step(run_id, step_id):
    return [sr for sr in stepruns_for(run_id) if sr.step_id == step_id]


def run_context(run_id):
    with Session(engine) as s:
        return s.get(WorkflowRun, run_id).context


def _loop_wf(name, mt_provider, judge_provider, lqa_max=None):
    """mt(0, key=mt) -> lqa(1, key=lqa, on_pass=next / on_fail={step:mt}) -> deliver(2).

    The canonical correction loop from #94/#95: the GATE is the branch SOURCE and `mt` the
    target, and a DELIVER step sits after the gate so a fall-through is visible."""
    wf_id = _new_wf(name)
    with Session(engine) as s:
        add_step(s, wf_id, 0, "MT", provider=mt_provider, prompt_text="Translate: {text}", key="mt")
        add_step(
            s,
            wf_id,
            1,
            "LQA",
            step_type=StepType.LQA,
            provider=judge_provider,
            key="lqa",
            max_visits=lqa_max,
            branch={"on_pass": "next", "on_fail": {"step": "mt"}},
        )
        add_step(
            s, wf_id, 2, "Deliver", provider="mock", prompt_text="Deliver: {text}", key="deliver"
        )
    return wf_id


def _deliver_ran(wf_id, run_id):
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    did = next(st.id for st in steps if st.key == "deliver")
    return len(_runs_for_step(run_id, did)) > 0


def _step_states(run_id, wf_id):
    """`GET /api/runs/{id}`'s DEFAULT `step_states` block, keyed by step key -- the exact
    projection issue #94's reproduction reads (and the one `workflow show-run` prints)."""
    with Session(engine) as s:
        rows = s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()
        steps = orchestrator.get_steps(s, wf_id)
        run = s.get(WorkflowRun, run_id)
        graph = run_overlay.run_step_states(rows, steps, run_status=run.status)
    return {n["key"]: n for n in graph["step_states"]}


def _run_warnings(run_id):
    """The run-level warnings block -- the ONE field the web run page (`run_warnings`),
    `GET /api/runs/{id}`'s default meta and `workflow show-run`'s default human output all
    read verbatim, and therefore the four-surface signal (MCP `work_status` is a thin
    client of the /api route)."""
    return (run_context(run_id) or {}).get("warnings") or []


def _held_warning(run_id):
    for w in _run_warnings(run_id):
        if w.get("code") == orchestrator.GATE_HOLD_WARNING_CODE:
            return w
    return None


def _segments(doc_id, locale="fr-FR"):
    with Session(engine) as s:
        return s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == locale)
            .order_by(Segment.order_index)
        ).all()


def _approve_all(doc_id, locale="fr-FR"):
    """The reviewer approval the hold waits for -- the ordinary segment lifecycle, no new
    route and no approval table."""
    with Session(engine) as s:
        for seg in s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).all():
            seg.status = SegStatus.APPROVED
            s.add(seg)
        s.commit()


def _last_run_id(doc_id):
    """The newest WorkflowRun over `doc_id` -- the run whose gate-hold record arm (35)
    reads back to assert WHICH family held."""
    with Session(engine) as s:
        return s.exec(
            select(WorkflowRun.id)
            .where(WorkflowRun.document_id == doc_id)
            .order_by(WorkflowRun.id.desc())
        ).first()


def _scored(rows):
    """StepRun rows that actually carry a judge verdict (a cap-halt / no-progress marker
    row is not a scoring event when it made no call)."""
    return [r for r in rows if r.gate is not None]


# --- (1) #95 no-progress -----------------------------------------------------


def test_no_progress_loop_is_not_rejudged():
    """#95: a loop-back that hands the gate BYTE-IDENTICAL text must NOT be re-judged.

    The judge would PASS on its second call (LqaFailThenPass(1)) -- exactly the prod
    re-roll that delivered TODO-FIXME text. With the guard the judge is called ONCE, the
    second visit carries the FAILED verdict forward, the run HOLDS (deliver never runs),
    and the reason is named on the step."""
    IdempotentRepair.calls = 0
    client = TestClient(app)
    judge = LqaFailThenPass(1)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaftp", judge)
    wf_id = _loop_wf("no-progress", "idemrepair", "lqaftp")
    pid = new_project(client, "no-progress")
    doc = upload(client, pid, ["Save changes.", "Backup finished."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    lqa_rows = _runs_for_step(summary["run_id"], steps[1].id)
    mt_rows = _runs_for_step(summary["run_id"], steps[0].id)
    assert len(mt_rows) == 2, ("the loop-back must have re-run mt once", len(mt_rows))
    assert judge.calls == 1, (
        "#95: identical bytes must NOT be re-judged (the re-roll is the bug)",
        judge.calls,
    )
    assert len(lqa_rows) == 2, ("both visits must leave a durable row", len(lqa_rows))
    assert [r.gate for r in lqa_rows] == ["failed", "failed"], (
        "#95: the previous (worse) verdict is carried forward, never re-rolled",
        [r.gate for r in lqa_rows],
    )
    assert lqa_rows[1].score == lqa_rows[0].score, (
        "the carried verdict keeps the previous score",
        (lqa_rows[0].score, lqa_rows[1].score),
    )
    assert "no progress" in (lqa_rows[1].error or "").lower(), (
        "the hold must NAME the reason",
        lqa_rows[1].error,
    )
    assert _deliver_ran(wf_id, summary["run_id"]) is False, (
        "#95 BLOCKER: a no-progress loop must HOLD, never fall through to deliver"
    )
    print("  (1) #95: an unchanged repair is not re-judged -- verdict carried, run holds")


def test_real_repair_is_still_rejudged():
    """CONTROL for (1): when the repair genuinely CHANGES the text the judge IS re-run and
    a real pass still delivers. The guard is byte-equality, not 'always hold'."""
    GrowingRepair.calls = 0
    client = TestClient(app)
    judge = LqaFailThenPass(1)
    register_provider("growrepair", GrowingRepair())
    register_provider("lqaftp", judge)
    wf_id = _loop_wf("real-repair", "growrepair", "lqaftp")
    pid = new_project(client, "real-repair")
    doc = upload(client, pid, ["Save changes.", "Backup finished."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    lqa_rows = _runs_for_step(summary["run_id"], steps[1].id)
    assert judge.calls == 2, ("a CHANGED text must be re-judged", judge.calls)
    assert [r.gate for r in lqa_rows] == ["failed", "passed"], [r.gate for r in lqa_rows]
    assert _deliver_ran(wf_id, summary["run_id"]) is True, "a genuine pass must still deliver"
    print("  (2) control: a repair that changes the text IS re-judged and can pass")


def test_no_progress_full_text_path():
    """#95 on the FULL_TEXT loop too -- the two orchestrator loops must not drift."""
    client = TestClient(app)
    judge = LqaFailThenPass(1)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaftp", judge)
    wf_id = _loop_wf("no-progress ft", "idemrepair", "lqaftp")
    pid = new_project(client, "no-progress ft")
    doc = upload(client, pid, ["Save changes.", "Backup finished."], mode="full_text")
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    lqa_rows = _runs_for_step(summary["run_id"], steps[1].id)
    assert judge.calls == 1, ("FULL_TEXT must guard identically", judge.calls)
    assert [r.gate for r in lqa_rows] == ["failed", "failed"], [r.gate for r in lqa_rows]
    assert _deliver_ran(wf_id, summary["run_id"]) is False, "FULL_TEXT must hold too"
    print("  (3) #95: the FULL_TEXT loop guards identically (no drift between the loops)")


def test_no_progress_parallel_group_lane():
    """#95 on the PARALLEL LQA group: each LANE carries its own previous verdict forward
    on byte-identical text, and a carried FAIL on the ROUTING lane holds the whole group
    (never a fall-through to deliver). Two check lanes read the text and produce none, so
    a loop-back through an idempotent repair re-judges nothing."""
    client = TestClient(app)
    judge_a = LqaFailThenPass(1)
    judge_b = LqaFailThenPass(0)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaftp", judge_a)
    register_provider("lqaftp2", judge_b)
    judge_b.name = "lqaftp2"
    wf_id = _new_wf("no-progress group")
    with Session(engine) as s:
        add_step(
            s, wf_id, 0, "MT", provider="idemrepair", prompt_text="Translate: {text}", key="mt"
        )
        add_step(
            s,
            wf_id,
            1,
            "LQA A",
            step_type=StepType.LQA,
            provider="lqaftp2",
            key="lqa_a",
            parallel_group="checks",
        )
        add_step(
            s,
            wf_id,
            2,
            "LQA B",
            step_type=StepType.LQA,
            provider="lqaftp",
            key="lqa_b",
            parallel_group="checks",
            branch={"on_pass": "next", "on_fail": {"step": "mt"}},
        )
        add_step(
            s, wf_id, 3, "Deliver", provider="mock", prompt_text="Deliver: {text}", key="deliver"
        )
    pid = new_project(client, "no-progress group")
    doc = upload(client, pid, ["Save changes.", "Backup finished."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    rows_b = _runs_for_step(summary["run_id"], steps[2].id)
    assert judge_a.calls == 1, ("#95: the routing lane must not re-roll", judge_a.calls)
    assert [r.gate for r in rows_b] == ["failed", "failed"], [r.gate for r in rows_b]
    assert "no progress" in (rows_b[1].error or "").lower(), rows_b[1].error
    assert _deliver_ran(wf_id, summary["run_id"]) is False, (
        "#95: a carried FAIL on the routing lane must hold the group, not fall through"
    )
    print("  (4) #95: a parallel LQA group carries per-lane verdicts and holds at the join")


# --- (2) #96 judge attribution ----------------------------------------------


def test_judge_attribution_on_step_and_shape():
    """#96: an LQA step stamps its judge's resolved provider/model/attempts/elapsed on the
    StepRun, so failure_shape names the engine instead of null/0/null."""
    client = TestClient(app)
    register_provider("lqaftp", LqaFailThenPass(0))
    wf_id = _new_wf("attribution")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "MT", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "LQA", step_type=StepType.LQA, provider="lqaftp", key="lqa")
    pid = new_project(client, "attribution")
    doc = upload(client, pid, ["Alpha one.", "Beta two."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        steps = orchestrator.get_steps(s, wf_id)
    lqa_row = _runs_for_step(summary["run_id"], steps[1].id)[0]
    assert lqa_row.provider_used == "lqaftp", (
        "#96: the judge's resolved provider must be stamped on the StepRun",
        lqa_row.provider_used,
    )
    assert lqa_row.attempts >= 1, ("#96: a judge call is at least one attempt", lqa_row.attempts)
    assert lqa_row.latency_ms is not None, "#96: the judge visit's elapsed must be recorded"
    assert (lqa_row.tokens_in or 0) > 0, "the judge burned tokens (sanity)"
    shape = next(
        r
        for r in run_overlay.failure_shape(
            stepruns_for(summary["run_id"]),
            [{"id": st.id, "key": st.key} for st in steps],
        )
        if r["step_id"] == steps[1].id
    )
    assert shape["provider"] == "lqaftp", ("failure_shape must name the judge engine", shape)
    assert shape["attempts"] >= 1 and shape["elapsed_ms"] is not None, shape
    print("  (5) #96: the LQA StepRun + failure_shape carry the judge's provider/attempts/elapsed")


def test_failed_judge_has_a_visible_failure_shape():
    """#96's sharpest point: a judge call that FAILS must be diagnosable. A dead judge
    (empty completion -> JUDGE_UNAVAILABLE) still names its engine and its attempts."""
    LqaDeadJudge.calls = 0
    client = TestClient(app)
    register_provider("lqadead", LqaDeadJudge())
    wf_id = _new_wf("dead judge shape")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "MT", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "LQA", step_type=StepType.LQA, provider="lqadead", key="lqa")
    pid = new_project(client, "dead judge shape")
    doc = upload(client, pid, ["Gamma one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        steps = orchestrator.get_steps(s, wf_id)
    row = _runs_for_step(summary["run_id"], steps[1].id)[0]
    assert LqaDeadJudge.calls >= 1, LqaDeadJudge.calls
    assert row.provider_used == "lqadead", (
        "#96: a FAILING judge call must still name its engine",
        row.provider_used,
    )
    assert row.attempts >= 1, ("#96: a failing judge made attempts", row.attempts)
    assert "judge unavailable" in (row.error or ""), row.error
    print("  (6) #96: a FAILED judge call has a visible failure shape (engine + attempts)")


def test_report_group_by_engine_attributes_judge_tokens():
    """#96 part 2: `report run tokens --group-by engine` must file judge tokens under the
    engine that
    ran them, not under an unexplained '(none)'."""
    client = TestClient(app)
    register_provider("lqaftp", LqaFailThenPass(0))
    wf_id = _new_wf("report attribution")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "MT", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "LQA", step_type=StepType.LQA, provider="lqaftp", key="lqa")
    pid = new_project(client, "report attribution")
    doc = upload(client, pid, ["Delta one.", "Epsilon two."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="engine", project_id=pid)
        )
    rows = {r[rep["key_column"]]: r for r in rep["rows"] if r.get(rep["key_column"]) != "TOTALS"}
    none_row = rows.get("(none)")
    assert none_row is None or (none_row.get("tokens_in") or 0) == 0, (
        "#96: judge tokens must not land in the unexplained '(none)' bucket",
        rows,
    )
    assert (rows.get("lqaftp", {}).get("tokens_in") or 0) > 0, (
        "#96: the judge's tokens must be attributed to its engine",
        rows,
    )
    print("  (7) #96: report run tokens --group-by engine attributes judge tokens OK")


def test_none_bucket_is_labelled_like_untracked():
    """#96 part 3: the `(none)` row that REMAINS on the provider/engine axis (a step that
    genuinely made no provider call) must be LABELLED, the way `(untracked)` already is.

    The issue's fallback ask -- the report explains `(untracked)` but left `(none)`
    unexplained, so an operator meeting that row had no way to know what it was. The
    label rides the ONE `_caveats` seam, so the web caption, the CSV footer, the /api
    JSON and the CLI `--json` all say the same thing; and it is emitted only when the row
    is actually on screen, so a report without one gains no CSV rows."""
    client = TestClient(app)
    wf_id = _new_wf("none bucket")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "MT", prompt_text="Translate: {text}")
        # ORCHESTRATION-ONLY: a tm_match step makes no provider call, so its StepRun has
        # no engine to attribute to -> the honest `(none)` bucket.
        add_step(s, wf_id, 1, "TM", step_type=StepType.TM_MATCH, provider=None)
    pid = new_project(client, "none bucket")
    doc = upload(client, pid, ["Lambda one.", "Mu two."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    with Session(engine) as s:
        rep = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="engine", project_id=pid)
        )
        plain = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="customer", project_id=pid)
        )
    keys = {r[rep["key_column"]] for r in rep["rows"]}
    assert "(none)" in keys, ("fixture must produce a (none) row", keys)
    assert any("'(none)' row" in c for c in rep["caveats"]), (
        "#96: the (none) bucket must be labelled on every surface",
        rep["caveats"],
    )
    # gate C round 1, L3: assert on the CAVEAT SENTENCE, not on the bucket KEY. `"(none)"
    # in to_csv(rep)` passed on the key alone and would still pass with `_caveats` dropped
    # from the footer entirely -- a tautology, not a pin.
    _csv = reports.to_csv(rep)
    _caveat = next(c for c in rep["caveats"] if "'(none)' row" in c)
    assert _caveat in _csv, (
        "the CSV FOOTER must carry the caveat sentence itself, not just the bucket key",
        _csv[-600:],
    )
    assert not plain.get("caveats"), (
        "a report with no (none) row must gain no caveat (and no extra CSV rows)",
        plain.get("caveats"),
    )
    print("  (8) #96: the surviving '(none)' bucket is labelled on every report surface")


# --- (3) #94 per-step execution ceiling -------------------------------------


def test_max_visits_is_a_per_step_execution_ceiling():
    """#94: a step never executes more times than its OWN --max-visits, whatever role it
    plays in the graph. The prod repro: an `lqa` step with --max-visits 1 that is the
    branch SOURCE ran twice. The run ends gracefully (soft cap), deliver never runs."""
    LqaAlwaysFail.calls = 0
    GrowingRepair.calls = 0
    client = TestClient(app)
    register_provider("growrepair", GrowingRepair())
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    wf_id = _loop_wf("visit ceiling", "growrepair", "lqaalwaysfail", lqa_max=1)
    pid = new_project(client, "visit ceiling")
    doc = upload(client, pid, ["Zeta one.", "Eta two."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", (
        "max_visits stays a SOFT cap: the run completes, flagged",
        summary,
    )
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    lqa_rows = _runs_for_step(summary["run_id"], steps[1].id)
    assert len(_scored(lqa_rows)) == 1, (
        "#94: an lqa step with --max-visits 1 must EXECUTE once",
        [(r.status, r.gate, r.error) for r in lqa_rows],
    )
    assert LqaAlwaysFail.calls == 1, ("#94: one execution == one judge call", LqaAlwaysFail.calls)
    ctx = run_context(summary["run_id"])
    assert ctx and "lqa" in (ctx.get("loop_ended") or []), ("the cap must be flagged", ctx)
    assert _deliver_ran(wf_id, summary["run_id"]) is False, (
        "#94: hitting the ceiling must not fall through to a downstream deliver step"
    )
    # gate C round 1, M1 -- ISSUE #94's OWN REPRODUCTION, re-run: the reporter read
    # `step_states[].visits` from GET /api/runs/{id}. A marker row for a step that did NOT
    # execute made that read 2 for a ceiling of 1 and flipped the node status to `skipped`
    # on a gate that had executed once and FAILED, while run.context["visits"] said 1.
    # Both run surfaces must count EXECUTIONS, and must agree.
    _states = _step_states(summary["run_id"], wf_id)
    assert _states["lqa"]["visits"] == 1, (
        "#94 repro: step_states[].visits must equal the ceiling that was set",
        _states,
    )
    assert _states["lqa"]["gate"] == "failed" and _states["lqa"]["status"] == "completed", (
        "a gate that executed once and FAILED must not read `skipped`",
        _states,
    )
    assert (ctx.get("visits") or {}).get("lqa") == _states["lqa"]["visits"], (
        "run.context['visits'] and step_states[].visits must agree",
        ctx.get("visits"),
        _states,
    )
    print("  (9) #94: --max-visits binds a step's OWN executions (lqa 1 -> ran once)")


def test_max_visits_two_executes_twice():
    """CONTROL for (9): --max-visits 2 executes exactly twice (the ceiling is the number
    the operator set, not an off-by-one)."""
    LqaAlwaysFail.calls = 0
    GrowingRepair.calls = 0
    client = TestClient(app)
    register_provider("growrepair", GrowingRepair())
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    wf_id = _loop_wf("visit ceiling 2", "growrepair", "lqaalwaysfail", lqa_max=2)
    pid = new_project(client, "visit ceiling 2")
    doc = upload(client, pid, ["Theta one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    assert len(_scored(_runs_for_step(summary["run_id"], steps[1].id))) == 2, "ceiling 2 -> 2 runs"
    assert LqaAlwaysFail.calls == 2, LqaAlwaysFail.calls
    print("  (10) control: --max-visits 2 executes exactly twice")


def test_max_visits_full_text_path():
    """#94 on the FULL_TEXT loop too. The orchestrator carries TWO independent step loops
    (segmented + FULL_TEXT) and a guard added to only one of them is a silent hole -- the
    same drift this suite pins for #95 in arm (3)."""
    LqaAlwaysFail.calls = 0
    GrowingRepair.calls = 0
    client = TestClient(app)
    register_provider("growrepair", GrowingRepair())
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    wf_id = _loop_wf("visit ceiling ft", "growrepair", "lqaalwaysfail", lqa_max=1)
    pid = new_project(client, "visit ceiling ft")
    doc = upload(client, pid, ["Nu one.", "Xi two."], mode="full_text")
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    lqa_rows = _runs_for_step(summary["run_id"], steps[1].id)
    assert len(_scored(lqa_rows)) == 1, (
        "#94: FULL_TEXT must bound the step's own executions identically",
        [(r.status, r.gate, r.error) for r in lqa_rows],
    )
    assert LqaAlwaysFail.calls == 1, LqaAlwaysFail.calls
    # gate C round 1, M1: NO row is written for a step that did not execute -- on either
    # loop. The ceiling event is carried by run.context (loop_ended + the run-level hold
    # warning) and by the live step_skipped event.
    assert len(lqa_rows) == 1, (
        "M1: a step that did not execute must not leave a StepRun row",
        [(r.status, r.error) for r in lqa_rows],
    )
    assert _step_states(summary["run_id"], wf_id)["lqa"]["visits"] == 1, (
        "M1: FULL_TEXT must report visits == the ceiling too",
        _step_states(summary["run_id"], wf_id),
    )
    assert any(
        w.get("code") == orchestrator.GATE_HOLD_WARNING_CODE
        for w in (run_context(summary["run_id"]) or {}).get("warnings") or []
    ), ("the capped GATE must record a run-level HOLD warning", run_context(summary["run_id"]))
    assert _deliver_ran(wf_id, summary["run_id"]) is False, (
        "#94: FULL_TEXT must not fall through to deliver either"
    )
    print("  (11) #94: the FULL_TEXT loop enforces the ceiling identically")


def test_linear_run_is_byte_stable():
    """The sacred gate: a plain workflow with no key/branch/max_visits is untouched by any
    of the three fixes -- one row per step, no cap/hold rows, RunGuard still inert (the
    guard never writes visits/loop_ended into run.context)."""
    client = TestClient(app)
    wf_id = _new_wf("linear")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "MT", prompt_text="Translate: {text}")
        add_step(s, wf_id, 1, "LQA", step_type=StepType.LQA, provider="mock")
        add_step(s, wf_id, 2, "Deliver", prompt_text="Deliver: {text}")
    for mode in ("segmented", "full_text"):
        pid = new_project(client, f"linear {mode}")
        doc = upload(client, pid, ["Iota one.", "Kappa two."], mode=mode)
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "completed", (mode, summary)
        rows = stepruns_for(summary["run_id"])
        assert len(rows) == 3, (mode, "a linear run writes exactly one row per step", len(rows))
        assert all(r.status == RunStatus.COMPLETED for r in rows), (
            mode,
            [(r.status, r.error) for r in rows],
        )
        ctx = run_context(summary["run_id"]) or {}
        assert "visits" not in ctx and "loop_ended" not in ctx, (
            mode,
            "the RunGuard must stay INERT on a linear workflow",
            ctx,
        )
    print("  (12) byte-stable: a linear run is untouched by all three fixes")


# --- (13-20) gate C round 1: the HOLD must actually withhold -----------------


def test_hold_withholds_the_document_and_the_export():
    """GATE C H1 -- the blocker. "The run HOLDS" has to mean the defective text does not
    ship. In a classic workflow there IS no deliver step to withhold: delivery IS the
    post-loop segment write-back, so before this fix the exported .docx from issue #95's
    own reproduction was byte-identical to the pre-fix one while four surfaces said the
    text was held.

    Pins the whole chain: the run records a run-level HOLD, every produced unit lands
    NEEDS_REVIEW under the critical gate-hold badge (never plain `mt`), the document
    export is REFUSED with an honest 409 naming the gate -- and `allow_held=1`, which
    exists to ship a source-filled file anyway, cannot bypass it. Then the release: a
    REVIEWER APPROVAL (the ordinary segment lifecycle) lets the export through."""
    IdempotentRepair.calls = 0
    client = TestClient(app)
    judge = LqaFailThenPass(1)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaftp", judge)
    wf_id = _loop_wf("hold withholds", "idemrepair", "lqaftp")
    pid = new_project(client, "hold withholds")
    doc = upload(client, pid, ["Save changes.", "Backup finished."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary

    segs = _segments(doc)
    assert segs and all(sg.status == SegStatus.NEEDS_REVIEW for sg in segs), (
        "H1: a gate-held unit must NOT land as clean `mt`",
        [(sg.order_index, sg.status) for sg in segs],
    )
    assert all(quality.has_gate_hold_flag(sg.qa_flags) for sg in segs), (
        "H1: every held unit must carry the gate-hold badge a reviewer can see",
        [sg.qa_flags for sg in segs],
    )
    r = client.get(f"/documents/{doc}/export?locale=fr-FR")
    assert r.status_code == 409 and r.json().get("gate_held") is True, (
        "H1 BLOCKER: the export must be REFUSED while the gate hold stands",
        r.status_code,
    )
    assert "FAILING" in r.json()["error"] and "LQA" in r.json()["error"], (
        "the 409 must NAME the gate and the reason",
        r.json()["error"],
    )
    r2 = client.get(f"/documents/{doc}/export?locale=fr-FR&allow_held=1")
    assert r2.status_code == 409 and r2.json().get("gate_held") is True, (
        "H1: allow_held ships a SOURCE-filled file; it must never wave through text a judge FAILED",
        r2.status_code,
    )
    w = _held_warning(summary["run_id"])
    assert w and "HELD" in w["message"], (
        "H1: the hold must carry a run-level reason on the four surfaces that render "
        "run.context['warnings']",
        _run_warnings(summary["run_id"]),
    )
    assert w["cause"] == orchestrator.GATE_HOLD_NO_PROGRESS, w

    _approve_all(doc)
    r3 = client.get(f"/documents/{doc}/export?locale=fr-FR")
    assert r3.status_code == 200, (
        "the hold must RELEASE on the reviewer approval it is waiting for",
        r3.status_code,
    )
    print("  (13) H1: a held run withholds the segments AND the export until review")


def test_hold_withholds_on_the_full_text_path():
    """H1 on the FULL_TEXT loop -- the two write-backs must not drift (arm (3)'s rule for
    the hold itself)."""
    client = TestClient(app)
    judge = LqaFailThenPass(1)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaftp", judge)
    wf_id = _loop_wf("hold withholds ft", "idemrepair", "lqaftp")
    pid = new_project(client, "hold withholds ft")
    doc = upload(client, pid, ["Save changes.", "Backup finished."], mode="full_text")
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    segs = _segments(doc)
    assert segs and all(
        sg.status == SegStatus.NEEDS_REVIEW and quality.has_gate_hold_flag(sg.qa_flags)
        for sg in segs
    ), ("FULL_TEXT must hold its body identically", [(sg.status, sg.qa_flags) for sg in segs])
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409, (
        "the FULL_TEXT export must be refused too"
    )
    # ROUND 2, H1 on this write-back too: the round-1 stale-badge clear un-did the hold
    # identically on the FULL_TEXT arm, so the release rule is pinned on BOTH paths.
    nogate_wf = _new_wf("hold withholds ft no gate")
    with Session(engine) as s:
        add_step(s, nogate_wf, 0, "MT", prompt_text="Translate: {text}")
    with Session(engine) as s:
        orchestrator.run_workflow(s, nogate_wf, doc, "fr-FR")
    assert all(quality.has_gate_hold_flag(sg.qa_flags) for sg in _segments(doc)), (
        "H1/FULL_TEXT: a later run with NO gate must not clear the badge",
        [sg.qa_flags for sg in _segments(doc)],
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409
    register_provider("lqaalwayspass", LqaAlwaysPass())
    pass_wf = _new_wf("hold withholds ft passing gate")
    with Session(engine) as s:
        add_step(s, pass_wf, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(s, pass_wf, 1, "LQA", step_type=StepType.LQA, provider="lqaalwayspass", key="lqa")
    with Session(engine) as s:
        orchestrator.run_workflow(s, pass_wf, doc, "fr-FR")
    assert all(not quality.has_gate_hold_flag(sg.qa_flags) for sg in _segments(doc)), (
        "H1/FULL_TEXT: a later run whose OWN gate PASSED does clear it",
        [sg.qa_flags for sg in _segments(doc)],
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 200
    print("  (14) H1: the FULL_TEXT write-back holds -- and releases -- identically")


def test_only_a_passing_later_run_clears_the_hold():
    """GATE C ROUND 2, H1 -- the blocker of round 2, and the STALE-BADGE rule it corrects.

    The hold is LIVE, not armed, so it needs a stale rule: a badge describing text a later
    run has REPLACED must clear, or the document is undeliverable forever. Round 1 cleared
    it on ANY later run that reached its write-back without a hold of its own -- which is a
    one-run release requiring no reviewer and no `allow_held`. Two runs and the gate-failed
    text was a 200 download wearing no badge, reached by following the documented remedy
    ("or a later CLEAN run replaces the text") verbatim: *clean* was never checked.

    Four later-run shapes, in one document, in order. Only the LAST one is a release:

      (a) a run with NO GATE AT ALL           -> still 409  (the pure H1 mechanism)
      (b) a run whose own gate ends FAILED    -> still 409  (the gate's own probe)
      (c) a reviewer approval                 -> 200        (release (b) of the rule)
      (d) a run whose own gate PASSES         -> 200, badge gone (release (a))"""
    client = TestClient(app)
    LqaAlwaysFail.calls = 0
    LqaAlwaysPass.calls = 0
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaftp", LqaFailThenPass(1))
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    register_provider("lqaalwayspass", LqaAlwaysPass())
    held_wf = _loop_wf("stale badge held", "idemrepair", "lqaftp")
    pid = new_project(client, "stale badge")
    doc = upload(client, pid, ["Save changes.", "Backup finished."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, held_wf, doc, "fr-FR")
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409

    # (a) a LATER RUN WITH NO GATE. Round 1 released the hold here.
    nogate_wf = _new_wf("stale badge no gate")
    with Session(engine) as s:
        add_step(s, nogate_wf, 0, "MT", prompt_text="Translate: {text}")
    with Session(engine) as s:
        orchestrator.run_workflow(s, nogate_wf, doc, "fr-FR")
    assert all(quality.has_gate_hold_flag(sg.qa_flags) for sg in _segments(doc)), (
        "H1: a later run with NO gate has passed nothing -- it must not clear the badge",
        [sg.qa_flags for sg in _segments(doc)],
    )
    assert all(sg.status == SegStatus.NEEDS_REVIEW for sg in _segments(doc)), (
        "a surviving badge must not sit on a row reading clean `mt`",
        [(sg.order_index, sg.status) for sg in _segments(doc)],
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409, (
        "H1 BLOCKER: a gate-less re-run must not turn the hold into a 200 download"
    )

    # (b) a LATER RUN WHOSE OWN GATE FAILS -- the gate's own two-run probe. An ordinary
    # "just add a check" workflow: no branch, so no loop and no correction seam.
    fail_wf = _new_wf("stale badge failing gate")
    with Session(engine) as s:
        add_step(s, fail_wf, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(s, fail_wf, 1, "LQA", step_type=StepType.LQA, provider="lqaalwaysfail", key="lqa")
    with Session(engine) as s:
        orchestrator.run_workflow(s, fail_wf, doc, "fr-FR")
    r = client.get(f"/documents/{doc}/export?locale=fr-FR")
    assert r.status_code == 409, (
        "H1 BLOCKER: a later run whose OWN gate ended FAILED must not release the hold",
        r.status_code,
    )
    assert all(quality.has_gate_hold_flag(sg.qa_flags) for sg in _segments(doc)), (
        "and the badge stays on (this run holds in its own right too -- M2)",
        [sg.qa_flags for sg in _segments(doc)],
    )

    # (c) the REVIEWER APPROVAL -- release (b) of the rule.
    _approve_all(doc)
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 200, (
        "a reviewer approval always releases the hold"
    )

    # (d) a LATER RUN WHOSE OWN GATE PASSES -- release (a). The approval above made the
    # segments APPROVED (status-protected), so this run needs a fresh held document.
    doc2 = upload(client, pid, ["Save changes.", "Backup finished."])
    register_provider("lqaftp", LqaFailThenPass(1))  # a fresh judge: fail once, then pass
    with Session(engine) as s:
        orchestrator.run_workflow(s, held_wf, doc2, "fr-FR")
    assert client.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 409
    pass_wf = _new_wf("stale badge passing gate")
    with Session(engine) as s:
        add_step(s, pass_wf, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(s, pass_wf, 1, "LQA", step_type=StepType.LQA, provider="lqaalwayspass", key="lqa")
    with Session(engine) as s:
        orchestrator.run_workflow(s, pass_wf, doc2, "fr-FR")
    assert all(not quality.has_gate_hold_flag(sg.qa_flags) for sg in _segments(doc2)), (
        "a later run whose OWN gate PASSED over the new text DOES clear the stale badge",
        [sg.qa_flags for sg in _segments(doc2)],
    )
    assert client.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 200, (
        "and the document is deliverable again -- the hold is never permanent"
    )
    print("  (15) H1: only a reviewer or a PASSING later run releases the hold")


def test_exhausted_loop_back_target_holds_instead_of_delivering():
    """GATE C H2 -- the D4 fall-through, and the DEFAULT shape of an exhausted loop (every
    step inherits workflow_max_visits). The ceiling sits on the loop-back TARGET, so the
    branch seam refused the jump and fell through to `cursor + 1` -- straight into the
    downstream deliver step, on text whose last verdict was FAILED, with the run reporting
    `completed`. It now ends HELD at the gate."""
    GrowingRepair.calls = 0
    LqaAlwaysFail.calls = 0
    client = TestClient(app)
    register_provider("growrepair", GrowingRepair())
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    wf_id = _new_wf("target ceiling")
    with Session(engine) as s:
        add_step(
            s,
            wf_id,
            0,
            "MT",
            provider="growrepair",
            prompt_text="Translate: {text}",
            key="mt",
            max_visits=2,
        )
        add_step(
            s,
            wf_id,
            1,
            "LQA",
            step_type=StepType.LQA,
            provider="lqaalwaysfail",
            key="lqa",
            branch={"on_pass": "next", "on_fail": {"step": "mt"}},
        )
        add_step(s, wf_id, 2, "Deliver", prompt_text="Deliver: {text}", key="deliver")
    pid = new_project(client, "target ceiling")
    doc = upload(client, pid, ["Theta one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert _deliver_ran(wf_id, summary["run_id"]) is False, (
        "H2 BLOCKER: an exhausted on_fail loop must NOT run a downstream deliver step on "
        "text the gate failed"
    )
    w = _held_warning(summary["run_id"])
    assert w and w["cause"] == orchestrator.GATE_HOLD_LOOP_EXHAUSTED, (
        "the exhausted loop must be recorded as a HOLD, naming its cause",
        _run_warnings(summary["run_id"]),
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409, (
        "and the text it produced must not be exportable"
    )
    print("  (16) H2: an exhausted loop-back target HOLDS instead of delivering")


def test_capped_producer_is_skipped_so_the_gate_still_judges():
    """GATE C H3 -- the regression this train introduced in round 1. Round 1 ended the run
    at ANY capped step, so a capped PRODUCER/FIXER ended the run mid-loop: `mt` had already
    produced fresh text, the gate never ran again, and that never-judged text was still
    committed by the write-back while the run read `completed`. Ending at a producer is the
    MORE permissive choice, not the safer one.

    Now a capped non-gate step is SKIPPED and the run continues, so the gate always judges
    the last produced text; the loop still terminates (via the ceiling on the loop-back
    target, arm (16))."""
    GrowingRepair.calls = 0
    IdempotentRepair.calls = 0
    LqaAlwaysFail.calls = 0
    client = TestClient(app)
    register_provider("growrepair", GrowingRepair())
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    wf_id = _new_wf("capped producer")
    with Session(engine) as s:
        add_step(
            s, wf_id, 0, "MT", provider="growrepair", prompt_text="Translate: {text}", key="mt"
        )
        add_step(
            s,
            wf_id,
            1,
            "Fix",
            provider="idemrepair",
            prompt_text="Fix: {text}",
            key="fix",
            max_visits=1,
        )
        add_step(
            s,
            wf_id,
            2,
            "LQA",
            step_type=StepType.LQA,
            provider="lqaalwaysfail",
            key="lqa",
            branch={"on_pass": "next", "on_fail": {"step": "mt"}},
        )
        add_step(s, wf_id, 3, "Deliver", prompt_text="Deliver: {text}", key="deliver")
    pid = new_project(client, "capped producer")
    doc = upload(client, pid, ["Kappa one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    fix_rows = _runs_for_step(summary["run_id"], steps[1].id)
    lqa_rows = _scored(_runs_for_step(summary["run_id"], steps[2].id))
    assert len(fix_rows) == 1, (
        "H3: the capped fixer executes exactly its ceiling and leaves no non-execution row",
        [(r.status, r.error) for r in fix_rows],
    )
    assert len(lqa_rows) >= 2, (
        "H3 BLOCKER: the gate must keep running after the fixer is capped",
        len(lqa_rows),
    )
    committed = [sg.target_text or "" for sg in _segments(doc)]
    assert committed and all(t in (lqa_rows[-1].input_text or "") for t in committed), (
        "H3 BLOCKER: the text that landed in the document must be the text the gate LAST "
        "judged -- never produced after the last gate visit",
        (committed, lqa_rows[-1].input_text),
    )
    assert _deliver_ran(wf_id, summary["run_id"]) is False, "the failing gate still holds"
    assert _held_warning(summary["run_id"]), _run_warnings(summary["run_id"])
    print("  (17) H3: a capped PRODUCER is skipped so the gate judges the final text")


def test_capped_step_with_a_false_run_condition_is_still_only_skipped():
    """GATE C H3(ii) -- the ordering half. A step whose `run_condition` is FALSE would have
    been SKIPPED and the run would have carried on; round 1 evaluated the ceiling FIRST, so
    such a step ABORTED the run purely because it happened to sit at its ceiling. Here the
    conditional step is at its ceiling on pass 2 and its condition is false: the run must
    continue, the gate must get its second look, and (this judge passes on call 2) the
    downstream deliver step must run."""
    GrowingRepair.calls = 0
    client = TestClient(app)
    judge = LqaFailThenPass(1)
    register_provider("growrepair", GrowingRepair())
    register_provider("lqaftp2", judge)
    wf_id = _new_wf("capped conditional")
    with Session(engine) as s:
        add_step(
            s, wf_id, 0, "MT", provider="growrepair", prompt_text="Translate: {text}", key="mt"
        )
        add_step(
            s,
            wf_id,
            1,
            "Polish",
            prompt_text="Polish: {text}",
            key="polish",
            max_visits=1,
            run_condition={"ref": "prev_lqa", "op": "passed"},
        )
        add_step(
            s,
            wf_id,
            2,
            "LQA",
            step_type=StepType.LQA,
            provider="lqaftp2",
            key="lqa",
            branch={"on_pass": "next", "on_fail": {"step": "mt"}},
        )
        add_step(s, wf_id, 3, "Deliver", prompt_text="Deliver: {text}", key="deliver")
    pid = new_project(client, "capped conditional")
    doc = upload(client, pid, ["Rho one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert judge.calls == 2, (
        "H3(ii): the gate must get its second look -- the capped conditional step is a "
        "SKIP, not a run end",
        judge.calls,
    )
    assert _deliver_ran(wf_id, summary["run_id"]) is True, (
        "H3(ii) BLOCKER: a step that would have been SKIPPED must not abort the run "
        "because it sits at its ceiling"
    )
    assert _held_warning(summary["run_id"]) is None, "a passing run is not held"
    print("  (18) H3(ii): a capped step with a FALSE run_condition is skipped, not fatal")


def test_oscillating_repair_is_no_progress():
    """GATE C M3. Round 1 compared only against the SINGLE most recent verdict row, so a
    repair that alternates between two outputs made "progress" on every visit and the
    re-roll lottery survived intact -- with a real (non-greedy) engine two byte-identical
    consecutive visits are the RARE case; prod only hit it because LM Studio decoded
    deterministically. Returning to text this gate has ALREADY judged, on ANY earlier
    visit, is not progress.

    This judge would PASS on call 4. It must never get there."""
    OscillatingRepair.calls = 0
    client = TestClient(app)
    judge = LqaFailThenPass(3)
    register_provider("oscrepair", OscillatingRepair())
    register_provider("lqaosc", judge)
    wf_id = _loop_wf("oscillating", "oscrepair", "lqaosc")
    pid = new_project(client, "oscillating")
    doc = upload(client, pid, ["Delta."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert judge.calls == 2, (
        "M3 BLOCKER: an oscillating repair must be caught the moment it returns to "
        "already-judged bytes, not re-rolled until it passes",
        judge.calls,
    )
    assert _deliver_ran(wf_id, summary["run_id"]) is False, (
        "M3 BLOCKER: the oscillation must HOLD, never deliver"
    )
    assert _held_warning(summary["run_id"]), _run_warnings(summary["run_id"])
    print("  (19) M3: an oscillating repair is no progress (any prior verdict counts)")


def test_carried_pass_ends_the_loop():
    """GATE C M4. The guard suppressed the judge CALL on a carried PASS but did nothing
    about the LOOP: an `on_pass={step:mt}` gate then re-ran the full-document MT pass over
    and over -- each visit carrying the same PASS forward -- until some other step hit its
    ceiling. A repair that provably changes nothing will not change anything next visit, so
    a carried verdict ENDS the loop; a PASS is not a hold, so the run goes FORWARD."""
    IdempotentRepair.calls = 0
    LqaAlwaysPass.calls = 0
    client = TestClient(app)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaalwayspass", LqaAlwaysPass())
    wf_id = _new_wf("carried pass")
    with Session(engine) as s:
        add_step(
            s, wf_id, 0, "MT", provider="idemrepair", prompt_text="Translate: {text}", key="mt"
        )
        add_step(
            s,
            wf_id,
            1,
            "LQA",
            step_type=StepType.LQA,
            provider="lqaalwayspass",
            key="lqa",
            branch={"on_pass": {"step": "mt"}, "on_fail": "end"},
        )
        add_step(s, wf_id, 2, "Deliver", prompt_text="Deliver: {text}", key="deliver")
    pid = new_project(client, "carried pass")
    doc = upload(client, pid, ["Sigma one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    mt_rows = _runs_for_step(summary["run_id"], steps[0].id)
    assert len(mt_rows) == 2, (
        "M4: the loop must end at the carried PASS -- one loop-back, then forward, not "
        "four more full-document MT passes",
        len(mt_rows),
    )
    assert LqaAlwaysPass.calls == 1, (
        "the carried visit still makes no judge call",
        LqaAlwaysPass.calls,
    )
    assert _deliver_ran(wf_id, summary["run_id"]) is True, (
        "M4: a carried PASS is not a hold -- the downstream steps still run on passed text"
    )
    assert _held_warning(summary["run_id"]) is None, "a PASS never holds"
    print("  (20) M4: a carried PASS ends the loop and goes forward")


def test_dead_engine_judge_is_not_attributed_to_an_engine():
    """GATE C L4. `judge_shape`'s `provider`/`model` are written to
    `StepRun.provider_used`/`model_used`, which `report --group-by engine` buckets on. A
    DeadEngineError judge made NO call at all (undecryptable secret / raced disable), so
    stamping the engine it could not reach added a row and a count for an engine that ran
    nothing -- while `_judge_shape`'s own contract says the fields name the engine that
    ACTUALLY RAN. The unreachable target is still named, just not in the bucketing
    columns."""
    from app.services import lqa as _lqa
    from app.services.providers import DeadEngineError, ProviderSpec

    spec = ProviderSpec(provider="engine:9", model="ghost-1")
    orig = _lqa.run_completion

    def _dead(*a, **kw):
        raise DeadEngineError("engine 9 is disabled")

    _lqa.run_completion = _dead
    _live = _lqa.provider_is_live
    _lqa.provider_is_live = lambda *a, **kw: True
    try:
        out = _lqa._llm_errors("Source.", "Cible.", "en-US", "fr-FR", {}, "engine:9", spec=spec)
    finally:
        _lqa.run_completion = orig
        _lqa.provider_is_live = _live
    _errs, _cost, _tin, _tout, status, shape = out
    assert status == _lqa.JUDGE_UNAVAILABLE, status
    assert shape["attempts"] == 0, ("L4: no call was made", shape)
    assert shape["provider"] is None and shape["model"] is None, (
        "L4: an engine that ran nothing must never appear in the columns "
        "`report --group-by engine` buckets on",
        shape,
    )
    assert shape["error_class"] == "DeadEngineError", shape
    assert "engine:9" in (shape.get("unreachable") or ""), (
        "the engine we could NOT reach must still be named, outside the bucketing columns",
        shape,
    )
    print("  (21) L4: a dead-engine judge reports attempts 0 and claims no engine")


def test_key_mode_export_is_held_too():
    """H1, KEY mode. A key project's values ARE Segments living in its internal
    `fmt="keys"` documents and they run through the SAME segmented loop, so a held
    correction loop flags them exactly as it flags a document's. The whole-project key
    export is the canonical TM-only delivery -- if it did not consult the hold, the text
    the document export refuses would ship out of the key file instead.

    Also pins the two carve-outs: a REVIEWED (approved/edited) value stops holding, and
    the BASE locale (whose export serves source text) is never held by a target-locale
    hold."""
    from app.models import Document
    from app.services import export as export_svc

    client = TestClient(app)
    pid = new_project(client, "key mode hold")
    with Session(engine) as s:
        kdoc = Document(
            project_id=pid,
            filename="keys.json",
            fmt="keys",
            mode="segmented",
            source_locale="en-US",
        )
        s.add(kdoc)
        s.commit()
        s.refresh(kdoc)
        kdoc_id = kdoc.id
        s.add(
            Segment(
                document_id=kdoc_id,
                order_index=0,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="Save changes.",
                target_text="Enregistrer TODO-FIXME",
                status=SegStatus.NEEDS_REVIEW,
                qa_flags=[quality.gate_hold_flag("LQA", "loop ended failing")],
            )
        )
        s.commit()
        held = export_svc.gate_held_reason_for_project(s, pid, "fr-FR")
        base = export_svc.gate_held_reason_for_project(s, pid, "en-US")
    assert held and "quality gate" in held and "key value" in held, (
        "H1 KEY mode: the whole-project key export must be held by the same rule",
        held,
    )
    assert base is None, ("the BASE locale export serves source text and is never held", base)
    with Session(engine) as s:
        for sg in s.exec(select(Segment).where(Segment.document_id == kdoc_id)).all():
            sg.status = SegStatus.APPROVED
            s.add(sg)
        s.commit()
        released = export_svc.gate_held_reason_for_project(s, pid, "fr-FR")
    assert released is None, ("a reviewed value releases the key hold too", released)
    print("  (22) H1: the KEY-mode export is held by the same rule, and releases the same way")


# --- (23-27) gate C round 2 --------------------------------------------------


def test_a_failing_gate_with_no_repair_loop_holds():
    """GATE C ROUND 2, M2. Round 1 held only the ENDINGS THAT HAVE A LOOP: the carried
    FAIL, the exhausted loop-back, the capped check. The three endings with no correction
    loop at all still shipped their gate-failed text at 200 with the run reading
    `completed` -- and `on_fail: "end"` is the most explicit way an author can say "if the
    check fails, stop". The posture keys on the standing VERDICT, not on the loop's SHAPE.

    ROUTING IS NOT THE POSTURE. `on_fail: "next"` into a `run_condition: prev_lqa:failed`
    fixer is the canonical Phase-2 correction shape, so the cursor still goes exactly where
    §5.4 always sent it (`downstream` below records that); what changed is that the run does
    not DELIVER text its own check is still failing.

    All three shapes, each with a trailing step so the routing half stays visible:
    `on_fail: "end"`, `on_fail: "next"`, and a branch-less check -- plus the branch-less
    shape once more on FULL_TEXT, because the posture lives in the two write-backs and
    they must not drift (the rule arm (3) applies to the no-progress guard)."""
    client = TestClient(app)
    pid = new_project(client, "unrepaired endings")
    for label, branch, downstream, mode in (
        ("on_fail=end", {"on_pass": "next", "on_fail": "end"}, False, "segmented"),
        ("on_fail=next", {"on_pass": "next", "on_fail": "next"}, True, "segmented"),
        ("no branch", None, True, "segmented"),
        ("no branch/FULL_TEXT", None, True, "full_text"),
    ):
        GrowingRepair.calls = 0
        LqaAlwaysFail.calls = 0
        register_provider("growrepair", GrowingRepair())
        register_provider("lqaalwaysfail", LqaAlwaysFail())
        wf_id = _new_wf(f"unrepaired {label}")
        with Session(engine) as s:
            add_step(
                s, wf_id, 0, "MT", provider="growrepair", prompt_text="Translate: {text}", key="mt"
            )
            add_step(
                s,
                wf_id,
                1,
                "LQA",
                step_type=StepType.LQA,
                provider="lqaalwaysfail",
                key="lqa",
                branch=branch,
            )
            add_step(s, wf_id, 2, "Deliver", prompt_text="Deliver: {text}", key="deliver")
        doc = upload(client, pid, ["Omega one."], mode=mode)
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert summary["status"] == "completed", (label, summary)
        w = _held_warning(summary["run_id"])
        assert w and w["cause"] == orchestrator.GATE_HOLD_UNREPAIRED, (
            f"M2 [{label}]: a check that FAILED with no repair loop must end the run HELD",
            _run_warnings(summary["run_id"]),
        )
        segs = _segments(doc)
        assert segs and all(
            sg.status == SegStatus.NEEDS_REVIEW and quality.has_gate_hold_flag(sg.qa_flags)
            for sg in segs
        ), (f"M2 [{label}]: the units it produced must be flagged", [(s_.status) for s_ in segs])
        r = client.get(f"/documents/{doc}/export?locale=fr-FR")
        assert r.status_code == 409, (f"M2 [{label}]: the export must be REFUSED", r.status_code)
        assert _deliver_ran(wf_id, summary["run_id"]) is downstream, (
            f"M2 [{label}]: routing is UNCHANGED -- only the delivery posture moved",
            _deliver_ran(wf_id, summary["run_id"]),
        )
    # FAIL CLOSED when the failing gate's step row is GONE from the graph (deleted while
    # the run executed). Naming the gate is best-effort; HOLDING is not -- returning None
    # there would make "delete the step mid-run" a way past the hold.
    with Session(engine) as s:
        run = s.get(WorkflowRun, summary["run_id"])
        ctx = dict(run.context or {})
        ctx.pop("gate_hold", None)
        ctx.pop("warnings", None)
        run.context = ctx
        s.add(run)
        s.commit()
        held = orchestrator._terminal_gate_hold(s, run, [])
    assert held and held["cause"] == orchestrator.GATE_HOLD_UNREPAIRED, (
        "M2: a failing gate whose step was deleted mid-run must still hold",
        held,
    )
    print("  (23) M2: end / next / branch-less -- a FAILING check with no repair loop holds")


def test_carried_pass_ends_the_loop_at_the_parallel_join_too():
    """GATE C ROUND 2, M1. The M4 clamp was applied at the SEQUENTIAL carried-verdict site
    only; the parallel-group JOIN called `_branch_cursor` raw, so a carried PASS took its
    `on_pass` loop-back again and again. Since a branch may only target an EARLIER step,
    every `on_pass: {step: ...}` is a backward jump -- the ordinary shape, not an exotic
    one. Identical logical shape to arm (20): FIVE full-document MT passes at the join
    versus TWO in sequence."""
    IdempotentRepair.calls = 0
    LqaAlwaysPass.calls = 0
    client = TestClient(app)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaalwayspass", LqaAlwaysPass())
    wf_id = _new_wf("carried pass join")
    with Session(engine) as s:
        add_step(
            s, wf_id, 0, "MT", provider="idemrepair", prompt_text="Translate: {text}", key="mt"
        )
        add_step(
            s,
            wf_id,
            1,
            "LQA A",
            step_type=StepType.LQA,
            provider="lqaalwayspass",
            key="la",
            parallel_group="g",
            branch={"on_pass": {"step": "mt"}, "on_fail": "end"},
        )
        add_step(
            s,
            wf_id,
            2,
            "LQA B",
            step_type=StepType.LQA,
            provider="lqaalwayspass",
            key="lb",
            parallel_group="g",
        )
        add_step(s, wf_id, 3, "Deliver", prompt_text="Deliver: {text}", key="deliver")
    pid = new_project(client, "carried pass join")
    doc = upload(client, pid, ["Join one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf_id)
    mt_rows = _runs_for_step(summary["run_id"], steps[0].id)
    la_rows = _runs_for_step(summary["run_id"], steps[1].id)
    assert len(mt_rows) == 2 and len(la_rows) == 2, (
        "M1: a carried PASS must end the loop at the JOIN exactly as in sequence -- 2 "
        "full-document passes, not 5",
        len(mt_rows),
        len(la_rows),
    )
    assert IdempotentRepair.calls <= 2, (
        "M1: the provider passes are the cost the clamp exists to stop",
        IdempotentRepair.calls,
    )
    assert _deliver_ran(wf_id, summary["run_id"]) is True, (
        "M1: a carried PASS is not a hold -- the run still goes forward"
    )
    assert _held_warning(summary["run_id"]) is None, "a PASS never holds"
    print("  (24) M1: a carried PASS ends the loop at the parallel-group join too")


def test_a_non_routing_lane_carried_fail_holds_the_join():
    """GATE C ROUND 2, L3. §5.3 routes on the branch-carrying member alone, which was
    harmless while a carried FAIL only stopped the cursor. Now that a carried FAIL BLOCKS
    DELIVERY, "only the routing lane's verdict can block delivery" is not a rule anyone
    would choose: a second lane that carried a FAIL holds the join like the routing lane.

    Routing lane PASSES (so §5.3 would route forward and deliver); lane B fails, and on
    the second visit carries that FAIL over byte-identical text."""
    IdempotentRepair.calls = 0
    client = TestClient(app)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaalwayspass", LqaAlwaysPass())
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    wf_id = _new_wf("lane hold join")
    with Session(engine) as s:
        add_step(
            s, wf_id, 0, "MT", provider="idemrepair", prompt_text="Translate: {text}", key="mt"
        )
        add_step(
            s,
            wf_id,
            1,
            "LQA A",
            step_type=StepType.LQA,
            provider="lqaalwayspass",
            key="la",
            parallel_group="g",
            branch={"on_pass": {"step": "mt"}, "on_fail": "end"},
        )
        add_step(
            s,
            wf_id,
            2,
            "LQA B",
            step_type=StepType.LQA,
            provider="lqaalwaysfail",
            key="lb",
            parallel_group="g",
        )
        add_step(s, wf_id, 3, "Deliver", prompt_text="Deliver: {text}", key="deliver")
    pid = new_project(client, "lane hold join")
    doc = upload(client, pid, ["Lane one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    w = _held_warning(summary["run_id"])
    assert w and w["step_name"] == "LQA B", (
        "L3: the NON-routing lane's carried FAIL must hold the join",
        _run_warnings(summary["run_id"]),
    )
    assert _deliver_ran(wf_id, summary["run_id"]) is False, (
        "L3: the run must not deliver text a lane failed and no loop repaired"
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409, (
        "L3: and the export is refused like any other hold"
    )
    print("  (25) L3: a NON-routing lane's carried FAIL holds the join too")


def test_judge_outage_holds_and_says_the_gate_was_not_evaluated():
    """GATE C ROUND 2, M3 (ruled). A CONFIGURED-live judge that delivers no verdict ended
    the run through the same seam and the same WORD as a gate hold -- and recorded
    nothing: no `gate_hold`, no `run.context["warnings"]` entry, so both DEFAULT headless
    views showed a bare `completed` while text NO gate judged shipped at 200. Text a gate
    never judged is not obviously better than text a gate judged and failed.

    FAIL CLOSED: a configured-but-unevaluated gate does not deliver. Same posture, its own
    cause and its own sentence -- "not evaluated: judge unavailable", never "FAILED"."""
    LqaDeadJudge.calls = 0
    client = TestClient(app)
    register_provider("lqadead", LqaDeadJudge())
    wf_id = _new_wf("judge outage hold")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(s, wf_id, 1, "LQA", step_type=StepType.LQA, provider="lqadead", key="lqa")
        add_step(s, wf_id, 2, "Deliver", prompt_text="Deliver: {text}", key="deliver")
    pid = new_project(client, "judge outage hold")
    doc = upload(client, pid, ["Outage one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    w = _held_warning(summary["run_id"])
    assert w and w["cause"] == orchestrator.GATE_HOLD_JUDGE_UNAVAILABLE, (
        "M3: the outage must record a run-level warning on the four default surfaces",
        _run_warnings(summary["run_id"]),
    )
    assert "not evaluated: judge unavailable" in w["message"], (
        "M3: the warning must NAME the reason, and must not claim the gate FAILED the text",
        w["message"],
    )
    assert "FAILED" not in w["message"], w["message"]
    segs = _segments(doc)
    assert segs and all(
        sg.status == SegStatus.NEEDS_REVIEW and quality.has_gate_hold_flag(sg.qa_flags)
        for sg in segs
    ), ("M3: the produced units are flagged under the same badge", [sg.status for sg in segs])
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409, (
        "M3: fail closed -- a configured gate that could not be EVALUATED does not deliver"
    )
    assert _deliver_ran(wf_id, summary["run_id"]) is False
    print("  (26) M3: a judge OUTAGE holds, and says the gate was not evaluated")


# --- (28-32) gate C round 3: the human body, the QE scope, the release, the refusal ---


def _human_edit(doc_id, text, status=SegStatus.EDITED, locale="fr-FR"):
    """The disposition a linguist's clean save leaves on a unit: their body, status EDITED
    (`fulltext_paragraphs.save_paragraph` / the editor's `segment_save`). Written directly
    so the arm pins the ORCHESTRATOR's treatment of that disposition and not the editor's
    own badge policy."""
    with Session(engine) as s:
        for seg in s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).all():
            seg.target_text = text
            seg.status = status
            s.add(seg)
        s.commit()


def test_a_human_disposition_survives_a_later_gate_less_run():
    """GATE C ROUND 3, H1 -- and it is the badge-SURVIVES arm, not the hold, that broke it.

    The stale-badge rule keeps a surviving badge's unit at `needs_review` so a CRITICAL
    badge never sits on a row reading clean `mt`. On the FULL_TEXT write-back that
    re-assertion sat OUTSIDE the `ft_status_protected` guard, so it fired on the one body
    it must never touch:

      * a reviewer APPROVES the held body (export 200, the ratified release), then ONE
        ordinary gate-less run flips it back to `needs_review` -- export 409 again. A
        machine revoked a human's sign-off, and "run the workflow again" became a way to
        RE-IMPOSE a hold a reviewer lifted (the mirror of the round-2 H1);
      * worse, a linguist's EDITED body is demoted the same way, and `needs_review` is not
        a protected status -- so the NEXT run's write seam no longer refuses and the human
        text is REPLACED by fresh MT. Two clicks from finished to gone.

    Both modes, because the posture lives in two write-backs and they must not drift."""
    client = TestClient(app)
    register_provider("idemrepair", IdempotentRepair())
    nogate_wf = _new_wf("r3 h1 no gate")
    with Session(engine) as s:
        add_step(s, nogate_wf, 0, "MT", prompt_text="Translate: {text}")
    for mode in ("full_text", "segmented"):
        register_provider("lqaftp", LqaFailThenPass(1))
        held_wf = _loop_wf(f"r3 h1 held {mode}", "idemrepair", "lqaftp")
        pid = new_project(client, f"r3 h1 {mode}")

        # (a) the APPROVAL the four surfaces call a release must SURVIVE a later run.
        doc = upload(client, pid, ["Alpha one.", "Alpha two."], mode=mode)
        with Session(engine) as s:
            orchestrator.run_workflow(s, held_wf, doc, "fr-FR")
        assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409, mode
        _approve_all(doc)
        assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 200, mode
        with Session(engine) as s:
            orchestrator.run_workflow(s, nogate_wf, doc, "fr-FR")
        assert all(sg.status == SegStatus.APPROVED for sg in _segments(doc)), (
            f"H1 [{mode}]: an ordinary later run must NOT flip a reviewer's APPROVED body "
            "back to needs_review",
            [(sg.order_index, sg.status) for sg in _segments(doc)],
        )
        assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 200, (
            f"H1 BLOCKER [{mode}]: re-running a workflow must not RE-IMPOSE a hold the "
            "reviewer lifted"
        )

        # (b) the linguist's EDITED body must still be there after TWO later runs.
        doc2 = upload(client, pid, ["Alpha one."], mode=mode)
        register_provider("lqaftp", LqaFailThenPass(1))
        with Session(engine) as s:
            orchestrator.run_workflow(s, held_wf, doc2, "fr-FR")
        _human_edit(doc2, "HUMAN BODY.")
        for _ in range(2):
            with Session(engine) as s:
                orchestrator.run_workflow(s, nogate_wf, doc2, "fr-FR")
        rows = _segments(doc2)
        assert all(sg.target_text == "HUMAN BODY." for sg in rows), (
            f"H1 BLOCKER [{mode}]: the human's text must still be stored -- the demotion to "
            "needs_review strips the protection the NEXT run then overwrites",
            [(sg.order_index, sg.status, sg.target_text) for sg in rows],
        )
        assert all(sg.status == SegStatus.EDITED for sg in rows), (
            f"H1 [{mode}]: and the EDITED disposition itself is not the machine's to demote",
            [(sg.order_index, sg.status) for sg in rows],
        )
    print("  (28) H1: a reviewer's approval and a linguist's edit survive later runs")


def test_a_qe_routing_gate_is_not_a_delivery_block():
    """GATE C ROUND 3, H2 -- the SCOPE of ERRATUM E2, ruled to `lqa` steps only.

    E2 says "a run that ENDS with any gate's standing verdict still FAILED is HELD", and
    `StepRun.gate` is also written by an `llm_agent` step with the QE routing gate armed.
    So a ONE-STEP translate workflow with `qe_gate` on and ONE below-threshold segment
    held the WHOLE locale at 409 and badged every produced unit CRITICAL -- an install
    that had turned `qe_gate` on went from "ships, some segments flagged" to "nothing
    ships", under a hold whose sentence calls the translate step a "quality check" whose
    "correction loop" did not repair the text (it is not a check, it has no branch and it
    had no loop).

    The QE gate's contract is PER-SEGMENT and documented as such: score every produced
    segment, route the low ones to review, ship the document. It stays that. The second
    half of the scope: a QE verdict cannot RELEASE an `lqa` hold either."""
    client = TestClient(app)
    register_provider("qejudge", QEJudge())
    wf_id = _new_wf("qe gate not a hold")
    with Session(engine) as s:
        add_step(
            s,
            wf_id,
            0,
            "QE Translate",
            provider="qejudge",
            prompt_text="Translate: {text}",
            key="t",
            qe_gate=True,
            qe_provider="qejudge",
            qe_threshold=90,
        )
    pid = new_project(client, "qe gate not a hold")
    doc = upload(client, pid, ["Alpha one.", "BAD gamma."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert summary["status"] == "completed", summary
    rows = _scored(stepruns_for(summary["run_id"]))
    assert rows and rows[-1].gate == "failed", (
        "the fixture must really drive a FAILED QE aggregate (else it proves nothing)",
        [(r.gate, r.score) for r in rows],
    )
    assert _held_warning(summary["run_id"]) is None, (
        "H2: a below-threshold QE segment must not record a run-level gate HOLD",
        _run_warnings(summary["run_id"]),
    )
    assert (run_context(summary["run_id"]) or {}).get("gate_hold") is None, run_context(
        summary["run_id"]
    )
    segs = _segments(doc)
    assert not any(quality.has_gate_hold_flag(sg.qa_flags) for sg in segs), (
        "H2: and no produced unit wears the CRITICAL gate-hold badge",
        [sg.qa_flags for sg in segs],
    )
    assert segs[1].status == SegStatus.NEEDS_REVIEW, (
        "the QE gate's OWN routing is untouched -- the low segment still goes to review",
        [(sg.order_index, sg.status) for sg in segs],
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 200, (
        "H2 BLOCKER: one below-threshold segment must not refuse the whole locale"
    )

    # The other half of the scope: a QE PASS is not a release either. A document held by
    # an `lqa` check stays held when a QE-gated run rewrites it.
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaftp", LqaFailThenPass(1))
    held_wf = _loop_wf("qe cannot release", "idemrepair", "lqaftp")
    doc2 = upload(client, pid, ["Alpha one."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, held_wf, doc2, "fr-FR")
    assert client.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 409
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc2, "fr-FR")
    assert all(quality.has_gate_hold_flag(sg.qa_flags) for sg in _segments(doc2)), (
        "H2: a PASSING QE routing gate is not the document-level verdict the release rule "
        "is about -- it must not clear an lqa hold",
        [sg.qa_flags for sg in _segments(doc2)],
    )
    assert client.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 409
    print("  (29) H2: the QE routing gate routes -- it does not hold, and does not release")


def test_a_producer_after_the_passing_gate_does_not_release():
    """GATE C ROUND 3, M1 -- "a check PASSED over the unit's CURRENT text", which is what
    all five surfaces promise and what the code did not check.

    `_run_gate_outcome` answered "passed" whenever no check was left standing FAILED, with
    no relation to the text the write-back was about to commit. So the ordinary "translate,
    check, then polish" shape -- and every `step:<key>` correction workflow, which is a
    check followed by a conditional fixer -- released a standing hold and downloaded the
    POST-EDIT body at 200: the check passed the PRE-post-edit text, the badge cleared
    anyway, and the delivered bytes were text no judge ever saw.

    The control matters as much as the defect: when the check IS the run's last
    text-touching event, the release still happens (arms (14)/(15) pin that), and a
    post-edit that rewrites ONE segment must not re-hold its untouched siblings."""
    client = TestClient(app)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaftp", LqaFailThenPass(1))
    register_provider("lqaalwayspass", LqaAlwaysPass())
    register_provider("mangle", MangleEdit())
    held_wf = _loop_wf("r3 m1 held", "idemrepair", "lqaftp")
    pid = new_project(client, "r3 m1")
    doc = upload(client, pid, ["Gamma one."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, held_wf, doc, "fr-FR")
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409

    post_wf = _new_wf("r3 m1 check then polish")
    with Session(engine) as s:
        add_step(s, post_wf, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(s, post_wf, 1, "LQA", step_type=StepType.LQA, provider="lqaalwayspass", key="lqa")
        add_step(s, post_wf, 2, "PostEdit", provider="mangle", prompt_text="Polish: {text}")
    with Session(engine) as s:
        orchestrator.run_workflow(s, post_wf, doc, "fr-FR")
    segs = _segments(doc)
    assert all(sg.target_text.startswith("MANGLED:") for sg in segs), (
        "the fixture must really have rewritten the text AFTER the passing check",
        [sg.target_text for sg in segs],
    )
    assert all(quality.has_gate_hold_flag(sg.qa_flags) for sg in segs), (
        "M1: a producer that runs after the passing check leaves the badge in place -- the "
        "check never saw this text",
        [sg.qa_flags for sg in segs],
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409, (
        "M1 BLOCKER: the post-edit body must not download at 200 off a check that passed "
        "the PREVIOUS text"
    )

    # PER UNIT, not per run: a two-segment held document, then a check that passes and a
    # post-edit that rewrites ONLY the second segment. The first releases, the second does
    # not -- so the rule is not "any change anywhere re-holds everything".
    register_provider("lqaftp", LqaFailThenPass(1))
    doc2 = upload(client, pid, ["Gamma one.", "Gamma two."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, held_wf, doc2, "fr-FR")
    assert client.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 409
    one_wf = _new_wf("r3 m1 one segment polished")
    with Session(engine) as s:
        add_step(s, one_wf, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(s, one_wf, 1, "LQA", step_type=StepType.LQA, provider="lqaalwayspass", key="lqa")
        add_step(
            s,
            one_wf,
            2,
            "PostEdit second only",
            provider="secondonly",
            prompt_text="Polish: {text}",
        )
    register_provider("secondonly", MangleSecondOnly())
    with Session(engine) as s:
        orchestrator.run_workflow(s, one_wf, doc2, "fr-FR")
    rows = _segments(doc2)
    assert not quality.has_gate_hold_flag(rows[0].qa_flags), (
        "M1: the segment the check DID pass, and nothing touched afterwards, releases",
        [sg.qa_flags for sg in rows],
    )
    assert quality.has_gate_hold_flag(rows[1].qa_flags), (
        "M1: and only the rewritten one stays held",
        [sg.qa_flags for sg in rows],
    )
    assert client.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 409
    print("  (30) M1: the release is a check that passed over the unit's CURRENT text")


def test_the_export_refusal_names_the_actual_cause():
    """GATE C ROUND 3, M2 -- the loudest operator-facing surface of the hold said only one
    thing, and for two of the causes it was false.

    The refusal is the body of the web/`/api` 409, `doc export`'s `export refused: <this>`,
    the bundle skip reason and the key-export refusal. It read "produced by a run whose
    correction loop ENDED with 'LQA' still FAILING" for EVERY hold: for `unrepaired` there
    was no correction loop (that ending is defined by the absence of one), and for
    `judge_unavailable` there was no failing verdict at all -- it reported a judge as having
    FAILED text that no judge had judged. The run-level record has been honest since round
    2; this is the same honesty at the delivery seam."""
    client = TestClient(app)
    register_provider("growrepair", GrowingRepair())
    register_provider("lqaalwaysfail", LqaAlwaysFail())
    register_provider("lqadead", LqaDeadJudge())
    pid = new_project(client, "r3 m2 refusal")

    # (a) UNREPAIRED: a branch-less failing check. No loop existed.
    unrep_wf = _new_wf("r3 m2 unrepaired")
    with Session(engine) as s:
        add_step(s, unrep_wf, 0, "MT", provider="growrepair", prompt_text="Translate: {text}")
        add_step(s, unrep_wf, 1, "LQA", step_type=StepType.LQA, provider="lqaalwaysfail", key="lqa")
    doc = upload(client, pid, ["Sigma one."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, unrep_wf, doc, "fr-FR")
    err = client.get(f"/documents/{doc}/export?locale=fr-FR").json()["error"]
    assert "no correction loop" in err and "'LQA'" in err, (
        "M2: an unrepaired ending must not be described as a correction loop that ENDED",
        err,
    )
    assert "correction loop RAN OUT" not in err and "STOPPED MAKING PROGRESS" not in err, err

    # (b) JUDGE_UNAVAILABLE: no verdict at all. The word FAILING must not appear.
    out_wf = _new_wf("r3 m2 outage")
    with Session(engine) as s:
        add_step(s, out_wf, 0, "MT", prompt_text="Translate: {text}")
        add_step(s, out_wf, 1, "LQA", step_type=StepType.LQA, provider="lqadead", key="lqa")
    doc2 = upload(client, pid, ["Sigma two."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, out_wf, doc2, "fr-FR")
    err2 = client.get(f"/documents/{doc2}/export?locale=fr-FR").json()["error"]
    assert "could NOT BE EVALUATED" in err2 and "never judged" in err2, (
        "M2 BLOCKER: the outage refusal must say the text was never judged",
        err2,
    )
    assert "FAILING" not in err2 and "correction loop" not in err2, (
        "M2 BLOCKER: and must not report a failing verdict that never existed",
        err2,
    )

    # (c) LOOP_EXHAUSTED: the loop-back target was at its ceiling. That IS a loop ending.
    ex_wf = _new_wf("r3 m2 exhausted")
    with Session(engine) as s:
        add_step(
            s,
            ex_wf,
            0,
            "MT",
            provider="growrepair",
            prompt_text="Translate: {text}",
            key="mt",
            max_visits=2,
        )
        add_step(
            s,
            ex_wf,
            1,
            "LQA",
            step_type=StepType.LQA,
            provider="lqaalwaysfail",
            key="lqa",
            branch={"on_pass": "next", "on_fail": {"step": "mt"}},
        )
    doc3 = upload(client, pid, ["Sigma three."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, ex_wf, doc3, "fr-FR")
    err3 = client.get(f"/documents/{doc3}/export?locale=fr-FR").json()["error"]
    assert "RAN OUT OF VISITS" in err3 and "FAILING" in err3, (
        "M2: an exhausted loop is described as an exhausted loop",
        err3,
    )
    for e in (err, err2, err3):
        assert e.isascii(), ("L1: the refusal is raised verbatim as CLI chrome", e)
        assert "not bypassable with allow_held" in e, e
    print("  (31) M2: the export refusal describes the ending it is actually refusing")


# --- (33-37) gate C round 4: the pause-edit, ONE scope, the QE hold's own exit ---


def _pause_edit_wf(name, judge_provider):
    """mt(0) -> human_review(1) -> lqa(2, branch-less).

    The operator pauses at the human step and edits the body; the check that then runs
    FAILS, so the run ends held with `unrepaired`. That is the ONE arm round 3 left alone."""
    wf_id = _new_wf(name)
    with Session(engine) as s:
        add_step(s, wf_id, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(s, wf_id, 1, "Review", step_type=StepType.HUMAN_REVIEW, provider="mock")
        add_step(s, wf_id, 2, "LQA", step_type=StepType.LQA, provider=judge_provider, key="lqa")
    return wf_id


def test_an_operator_pause_edit_is_not_demoted_and_survives_the_next_run():
    """GATE C ROUND 4, H1 -- the hold-ASSERT arm, the twin round 3 deliberately left alone.

    Round 3 guarded the badge RE-ASSERTION arm against human-authored bodies and argued the
    hold-assert arm was different: the pause-edit is "content this run produced and this
    run's own check failed". The reasoning was about the status LABEL. The harm is not the
    label -- `NEEDS_REVIEW` is the system's OVERWRITE PERMISSION, so demoting a
    human-authored body is exactly how human bytes get deleted, and measured it took ONE
    ordinary later run: the linguist's body was replaced by fresh MT, silently. A regression
    too: main carries no gate-hold machinery at all, so before this train that edit survived.

    The ruling: the hold is recorded at run level and on the BADGE (which may sit on a human
    unit as information), but the unit's STATUS and its overwrite protection stay. Both
    modes, and the segmented twin must land in the same disposition."""
    client = TestClient(app)
    pid = new_project(client, "r4 h1 pause edit")
    for mode in ("full_text", "segmented"):
        register_provider("lqaalwaysfail", LqaAlwaysFail())
        wf_id = _pause_edit_wf(f"r4 h1 {mode}", "lqaalwaysfail")
        doc = upload(client, pid, ["Epsilon one."], mode=mode)
        with Session(engine) as s:
            paused = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
        assert paused["status"] == "paused", (mode, paused)
        with Session(engine) as s:
            done = orchestrator.resume_workflow(s, paused["run_id"], "fr-FR", "LINGUIST BODY.")
        assert done["status"] == "completed", (mode, done)
        assert _held_warning(paused["run_id"]) is not None, (
            f"the fixture must really end HELD [{mode}] (else it proves nothing)",
            _run_warnings(paused["run_id"]),
        )
        rows = _segments(doc)
        assert all(sg.target_text == "LINGUIST BODY." for sg in rows), (
            f"H1 [{mode}]: the resume must really have landed the operator's body",
            [(sg.status, sg.target_text) for sg in rows],
        )
        assert all(sg.status == SegStatus.EDITED for sg in rows), (
            f"H1 BLOCKER [{mode}]: the hold-assert arm must NOT demote a human-authored "
            "body to needs_review -- that status IS the overwrite permission",
            [(sg.status, sg.target_text) for sg in rows],
        )
        assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 200, (
            f"H1 [{mode}]: the human review this hold waits for already happened, so the "
            "export is not refused on the human's own unit"
        )

        # ONE ordinary gate-less run -- the step that used to destroy the body.
        nogate_wf = _new_wf(f"r4 h1 no gate {mode}")
        with Session(engine) as s:
            add_step(s, nogate_wf, 0, "MT", prompt_text="Translate: {text}")
        with Session(engine) as s:
            orchestrator.run_workflow(s, nogate_wf, doc, "fr-FR")
        rows = _segments(doc)
        assert all(sg.target_text == "LINGUIST BODY." for sg in rows), (
            f"H1 BLOCKER [{mode}]: ONE later run must not replace the linguist's body with "
            "fresh MT",
            [(sg.status, sg.target_text) for sg in rows],
        )
        assert all(sg.status == SegStatus.EDITED for sg in rows), (
            f"H1 [{mode}]: and the EDITED disposition is not the machine's to demote",
            [(sg.status, sg.target_text) for sg in rows],
        )
    print("  (33) H1: an operator's pause-edit keeps its status, its protection and its bytes")


def test_the_release_does_not_depend_on_step_order():
    """GATE C ROUND 4, M1 -- `_last_gate_judged_text` scoped by BAILING on the newest row.

    Its sibling `_standing_gate_verdicts` skips PAST a foreign verdict row and keeps
    looking; this one took the newest row of ANY type and returned None when it was not
    `lqa`. So a `qe_gate` step placed anywhere AFTER the check blocked the release
    PERMANENTLY -- even when it rewrote nothing and the check had judged the exact bytes
    being committed -- while the SAME three steps in the other order released. Five surfaces
    promise the opposite ("a post-edit or fix step that REWRITES a unit after the check
    leaves that unit held"); nothing was rewritten here.

    The two orders must reach the same decision on identical text. Arm (30) keeps the other
    half honest: a producer that really does rewrite still re-holds what it touched."""
    client = TestClient(app)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaalwayspass", LqaAlwaysPass())
    register_provider("qejudge", QEJudge())
    pid = new_project(client, "r4 m1 order")

    after_wf = _new_wf("r4 m1 qe after the check")
    with Session(engine) as s:
        add_step(s, after_wf, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(s, after_wf, 1, "LQA", step_type=StepType.LQA, provider="lqaalwayspass", key="lqa")
        add_step(
            s,
            after_wf,
            2,
            "QE noop",
            provider="qejudge",
            prompt_text="Polish: {text}",
            qe_gate=True,
            qe_provider="qejudge",
            qe_threshold=90,
        )
    before_wf = _new_wf("r4 m1 qe before the check")
    with Session(engine) as s:
        add_step(s, before_wf, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(
            s,
            before_wf,
            1,
            "QE noop",
            provider="qejudge",
            prompt_text="Polish: {text}",
            qe_gate=True,
            qe_provider="qejudge",
            qe_threshold=90,
        )
        add_step(
            s, before_wf, 2, "LQA", step_type=StepType.LQA, provider="lqaalwayspass", key="lqa"
        )

    outcomes = {}
    for label, wf in (("after", after_wf), ("before", before_wf)):
        register_provider("lqaftp", LqaFailThenPass(1))
        held_wf = _loop_wf(f"r4 m1 held {label}", "idemrepair", "lqaftp")
        doc = upload(client, pid, ["Kappa one."])
        with Session(engine) as s:
            orchestrator.run_workflow(s, held_wf, doc, "fr-FR")
        assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409, label
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf, doc, "fr-FR")
        assert summary["status"] == "completed", (label, summary)
        rows = _scored(stepruns_for(summary["run_id"]))
        assert any(r.gate == "passed" for r in rows), (
            "the fixture must really produce a PASSING QE verdict row",
            [(r.gate, r.score) for r in rows],
        )
        segs = _segments(doc)
        outcomes[label] = (
            client.get(f"/documents/{doc}/export?locale=fr-FR").status_code,
            [sg.target_text for sg in segs],
            [quality.has_gate_hold_flag(sg.qa_flags) for sg in segs],
        )
    assert outcomes["after"][1] == outcomes["before"][1], (
        "the two orders must commit IDENTICAL text (else the comparison proves nothing)",
        outcomes,
    )
    assert outcomes["after"] == outcomes["before"], (
        "M1 BLOCKER: identical text and identical verdicts must reach the same release "
        "decision -- step ORDER is not a delivery predicate",
        outcomes,
    )
    assert outcomes["after"][0] == 200, (
        "M1: a QE-gated step that rewrote NOTHING must not block the release forever",
        outcomes,
    )
    print("  (34) M1: one scope, one release decision, whichever order the steps sit in")


def test_a_qe_loop_ending_hold_is_releasable_by_its_own_workflow():
    """GATE C ROUND 4, M2 -- the hold and the release had different scopes.

    ERRATUM E1 still records a hold when a QE-gated step's own CORRECTION LOOP ends, and
    round 3 scoped the RELEASE to `lqa` rows. That closed the only non-human way out of a
    hold E1 creates: a clean re-run of the very workflow that recorded it wrote a QE verdict
    row the release refused to read, so an install with QE-only workflows had holds whose
    only exit was approving every unit by hand -- and three surfaces told the operator that
    "a later run whose own check PASSES over the unit's new text" would release it.

    THE RULING (round 4): the hold carries the FAMILY that recorded it and the release
    matches that family. E1 stays. `lqa` remains in every scope, so arm (29)'s other half --
    a passing QE gate is NOT a release for an `lqa` hold -- is untouched and re-asserted."""
    client = TestClient(app)
    register_provider("qejudge", QEJudge())
    wf_id = _new_wf("r4 m2 qe loop")
    with Session(engine) as s:
        add_step(
            s,
            wf_id,
            0,
            "QE MT",
            provider="qejudge",
            prompt_text="Translate: {text}",
            key="t",
            qe_gate=True,
            qe_provider="qejudge",
            qe_threshold=90,
            max_visits=2,
            branch={"on_pass": "next", "on_fail": {"step": "t"}},
        )
    pid = new_project(client, "r4 m2")
    doc = upload(client, pid, ["BAD one."])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    held = _held_warning(summary["run_id"])
    assert held is not None, (
        "the fixture must really drive E1's QE loop-ending hold (else it proves nothing)",
        _run_warnings(summary["run_id"]),
    )
    assert held.get("family") == quality.GATE_HOLD_FAMILY_QE, (
        "M2: the hold must record WHICH family held, so the release can match it",
        held,
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409
    segs = _segments(doc)
    assert all(quality.has_gate_hold_flag(sg.qa_flags) for sg in segs), [sg.qa_flags for sg in segs]
    assert all(
        quality.gate_hold_families(sg.qa_flags) == frozenset({quality.GATE_HOLD_FAMILY_QE})
        for sg in segs
    ), ("M2: and the family rides the unit's badge", [sg.qa_flags for sg in segs])

    # A CLEAN run of the SAME workflow: the body is repaired and its own QE gate PASSES.
    register_provider("qejudge", QECleanRepair())
    with Session(engine) as s:
        clean = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    assert clean["status"] == "completed", clean
    rows = _scored(stepruns_for(clean["run_id"]))
    assert rows and rows[-1].gate == "passed", (
        "the clean re-run's own QE gate must really PASS",
        [(r.gate, r.score) for r in rows],
    )
    segs = _segments(doc)
    assert not any(quality.has_gate_hold_flag(sg.qa_flags) for sg in segs), (
        "M2 BLOCKER: a hold recorded by a QE loop must be releasable by a clean run of the "
        "workflow that recorded it -- otherwise its only exit is a human approving by hand",
        [(sg.status, sg.target_text, sg.qa_flags) for sg in segs],
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 200

    # THE OTHER HALF, re-asserted: an `lqa` hold does NOT widen. A passing QE gate is not a
    # document-level verdict and must not release it (arm (29)'s rule, now that scopes vary).
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaftp", LqaFailThenPass(1))
    lqa_held = _loop_wf("r4 m2 lqa held", "idemrepair", "lqaftp")
    doc2 = upload(client, pid, ["Lambda one."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, lqa_held, doc2, "fr-FR")
    assert (run_context(_last_run_id(doc2)) or {}).get("gate_hold", {}).get(
        "family"
    ) == quality.GATE_HOLD_FAMILY_LQA
    register_provider("qejudge", QECleanRepair())
    with Session(engine) as s:
        orchestrator.run_workflow(s, wf_id, doc2, "fr-FR")
    assert all(quality.has_gate_hold_flag(sg.qa_flags) for sg in _segments(doc2)), (
        "M2: widening the scope for a QE hold must not widen it for an LQA hold",
        [sg.qa_flags for sg in _segments(doc2)],
    )
    assert client.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 409
    print("  (35) M2: the release matches the family that held, and E1 keeps its exit")


def test_a_retyped_gate_step_fails_closed_like_a_deleted_one():
    """GATE C ROUND 4, L1 -- the neighbouring edit to round 2's deleted-step case.

    A verdict row whose step is GONE is KEPT (deleting the gate mid-run is not a way past
    the hold). A verdict row whose step was RETYPED mid-run was DROPPED, so gate-failed text
    downloaded clean with no badge through the neighbouring edit. Both are rows this code
    cannot positively identify as QE rows, and both now fail closed (`_verdict_family`).

    `workflows.update_step` Guard 1 refuses every step edit while the workflow has a
    pending/running/paused run, so the mid-run retype is not reachable through the product
    today -- pinned below as the OUTER door. The point of the inner one is that the hold's
    fail-closed property must not be a consequence of an unrelated guard two modules away."""
    client = TestClient(app)
    register_provider("retypejudge", RetypingJudge())
    wf_id = _new_wf("r4 l1 retype")
    with Session(engine) as s:
        add_step(s, wf_id, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(s, wf_id, 1, "Check", step_type=StepType.LQA, provider="retypejudge", key="lqa")
    RetypingJudge.wf_id = wf_id
    pid = new_project(client, "r4 l1")
    doc = upload(client, pid, ["Delta one."])
    try:
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc, "fr-FR")
    finally:
        RetypingJudge.wf_id = None
    with Session(engine) as s:
        retyped = [
            st.step_type
            for st in s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).all()
        ]
    assert StepType.LQA not in retyped, (
        "the fixture must really have retyped the gate step mid-run",
        retyped,
    )
    assert _held_warning(summary["run_id"]) is not None, (
        "L1 BLOCKER: a verdict row whose step was RETYPED mid-run must still hold -- it was "
        "an lqa step when it judged",
        _run_warnings(summary["run_id"]),
    )
    segs = _segments(doc)
    assert all(quality.has_gate_hold_flag(sg.qa_flags) for sg in segs), (
        "L1: and the units this run produced still wear the badge",
        [(sg.status, sg.qa_flags) for sg in segs],
    )
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409

    # The POSITIVE control: a step that IS a genuine QE row (qe_gate armed) is still scoped
    # OUT, so "fail closed" did not degrade into "keep every row" and re-open round 3's H2.
    with Session(engine) as s:
        for st in s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_id)).all():
            if st.key == "lqa":
                st.qe_gate = True
                s.add(st)
        s.commit()
    with Session(engine) as s:
        assert orchestrator._standing_gate_verdicts(s, summary["run_id"]) == {}, (
            "L1 control: a genuine QE verdict row must still be scoped out of the posture"
        )

    # The OUTER door, named in the docstring so the coupling is pinned rather than assumed.
    from app.services import workflows as wf_svc

    live_wf = _new_wf("r4 l1 guard 1")
    with Session(engine) as s:
        add_step(s, live_wf, 0, "MT", prompt_text="Translate: {text}", key="mt")
        add_step(
            s, live_wf, 1, "Check", step_type=StepType.LQA, provider="lqaalwaysfail", key="lqa"
        )
        step_id = next(
            st.id
            for st in s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == live_wf)).all()
            if st.key == "lqa"
        )
        s.add(
            WorkflowRun(
                workflow_id=live_wf,
                document_id=doc,
                target_locale="fr-FR",
                status=RunStatus.RUNNING,
            )
        )
        s.commit()
    with Session(engine) as s:
        try:
            wf_svc.update_step(s, step_id, {"step_type": StepType.LLM_AGENT})
        except Exception as exc:  # the guard raises; the type is the service's own
            guard_msg = str(exc)
        else:
            guard_msg = None
    assert guard_msg, (
        "L1: workflows.update_step Guard 1 must refuse a step edit while a run is in "
        "flight -- it is the outer door the fail-closed read no longer depends on"
    )
    print("  (36) L1: a retyped gate step fails closed, and Guard 1 is the outer door")


def test_approval_clears_the_badge_and_a_release_reads_mt():
    """GATE C ROUND 4, L3 + L4 -- the two ends of the badge's life.

    L3: an APPROVED unit kept its CRITICAL gate-hold badge forever. The release path skips a
    status-protected slot and `_gate_held_reason_for_docs` skips APPROVED rows, so nothing
    could ever clear it -- the editor showed a permanent CRITICAL on signed-off work. The
    ratified rule already names reviewer approval as a release; the approve SEAM now
    performs it.

    L4: on the RELEASE the two write-backs drifted the other way -- FULL_TEXT left the body
    reading `needs_review` with no badge, at 200. A body that just passed its check and
    downloads clean must read `mt`, as the segmented twin does."""
    from app.models import Document, Project
    from app.services import segment_review

    client = TestClient(app)
    register_provider("idemrepair", IdempotentRepair())
    register_provider("lqaalwayspass", LqaAlwaysPass())
    pid = new_project(client, "r4 l3 l4")

    # L3 -- the reviewer's own approval clears the badge it resolves.
    register_provider("lqaftp", LqaFailThenPass(1))
    held_wf = _loop_wf("r4 l3 held", "idemrepair", "lqaftp")
    doc = upload(client, pid, ["Eta one.", "Eta two."])
    with Session(engine) as s:
        orchestrator.run_workflow(s, held_wf, doc, "fr-FR")
    assert client.get(f"/documents/{doc}/export?locale=fr-FR").status_code == 409
    with Session(engine) as s:
        d = s.get(Document, doc)
        project = s.get(Project, d.project_id)
        seg = s.exec(
            select(Segment)
            .where(Segment.document_id == doc, Segment.target_locale == "fr-FR")
            .order_by(Segment.order_index)
        ).first()
        segment_review.approve_and_write_tm(s, seg, project, d)
    rows = _segments(doc)
    assert rows[0].status == SegStatus.APPROVED, [(r.order_index, r.status) for r in rows]
    assert not quality.has_gate_hold_flag(rows[0].qa_flags), (
        "L3: the reviewer's approval IS the release this hold waits for, so it must clear "
        "the badge rather than leave a permanent CRITICAL on signed-off work",
        rows[0].qa_flags,
    )
    assert quality.has_gate_hold_flag(rows[1].qa_flags), (
        "L3: and only the approved unit's badge -- the sibling is untouched",
        rows[1].qa_flags,
    )

    # L4 -- the RELEASE lands the same disposition on both write-backs.
    for mode in ("segmented", "full_text"):
        register_provider("lqaftp", LqaFailThenPass(1))
        doc2 = upload(client, pid, ["Theta one."], mode=mode)
        with Session(engine) as s:
            orchestrator.run_workflow(s, held_wf, doc2, "fr-FR")
        assert client.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 409, mode
        clean_wf = _new_wf(f"r4 l4 clean {mode}")
        with Session(engine) as s:
            add_step(s, clean_wf, 0, "MT", prompt_text="Translate: {text}", key="mt")
            add_step(
                s, clean_wf, 1, "LQA", step_type=StepType.LQA, provider="lqaalwayspass", key="lqa"
            )
        with Session(engine) as s:
            orchestrator.run_workflow(s, clean_wf, doc2, "fr-FR")
        rows = _segments(doc2)
        assert not any(quality.has_gate_hold_flag(r.qa_flags) for r in rows), (
            mode,
            rows[0].qa_flags,
        )
        assert all(r.status == SegStatus.MT for r in rows), (
            f"L4 [{mode}]: a released body downloads at 200 and must not still read "
            "needs_review with nothing on it saying why",
            [(r.order_index, r.status) for r in rows],
        )
        assert client.get(f"/documents/{doc2}/export?locale=fr-FR").status_code == 200, mode
    print("  (37) L3/L4: approval clears the badge, and a release reads mt on both paths")


def test_key_mode_export_is_driven_and_refused():
    """GATE C ROUND 2, L4 (the round-1 builder's own flag). Arm (22) pins the PREDICATE
    and its carve-outs; this drives the actual whole-project KEY EXPORT that all three key
    surfaces (web, `/api`, `keys export`) funnel through, so a future fourth surface that
    forgets the companion is caught by a real refusal rather than by reading call sites."""
    from app.models import Customer, Project, ProjectType
    from app.services import content, key_files
    from app.services import keys as keys_svc

    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    with Session(engine) as s:
        cust = Customer(name="LQA keys cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        proj = content.project_create(
            s,
            "LQA keys export",
            source_locale="en-US",
            target_locales="fr-FR",
            customer_id=str(cust.id),
            project_type=ProjectType.KEY_BASED,
        )
        pid = proj.id
        key = keys_svc.create_key(
            s, proj, "save", namespace="app", base_value="Save changes.", actor="fixture"
        )
        keys_svc.set_value(
            s, key, "fr-FR", "Enregistrer TODO-FIXME", origin=keys_svc.ORIGIN_API, actor="fixture"
        )
        s.commit()
    # A held run's write-back leaves exactly this on the value segment.
    with Session(engine) as s:
        val = s.exec(
            select(Segment).where(
                Segment.target_locale == "fr-FR", Segment.source_text == "Save changes."
            )
        ).all()[-1]
        val.status = SegStatus.NEEDS_REVIEW
        val.qa_flags = [quality.gate_hold_flag("LQA", "loop ended failing")]
        s.add(val)
        s.commit()
        val_id = val.id
    from app.services import export as export_svc

    with Session(engine) as s:
        proj = s.get(Project, pid)
        try:
            key_files.export_locale(s, proj, "fr-FR", fmt="json", namespace="app")
            raise AssertionError("L4: the DRIVEN key export must be refused while held")
        except export_svc.ExportHeldLocale as exc:
            assert "quality gate" in str(exc), str(exc)
    with Session(engine) as s:
        val = s.get(Segment, val_id)
        val.status = SegStatus.APPROVED
        s.add(val)
        s.commit()
        proj = s.get(Project, pid)
        body = key_files.export_locale(s, proj, "fr-FR", fmt="json", namespace="app")
    assert b"Enregistrer" in body, ("and the reviewed value ships once released", body[:200])
    settings.set_many({"key_projects_enabled": "0"})
    settings.invalidate_cache()
    print("  (27) L4: the KEY export is DRIVEN -- refused while held, ships once reviewed")


def main():
    init_db()
    settings.set_many({"spend_cap_usd": "0"})
    test_no_progress_loop_is_not_rejudged()
    test_real_repair_is_still_rejudged()
    test_no_progress_full_text_path()
    test_no_progress_parallel_group_lane()
    test_judge_attribution_on_step_and_shape()
    test_failed_judge_has_a_visible_failure_shape()
    test_report_group_by_engine_attributes_judge_tokens()
    test_none_bucket_is_labelled_like_untracked()
    test_max_visits_is_a_per_step_execution_ceiling()
    test_max_visits_two_executes_twice()
    test_max_visits_full_text_path()
    test_hold_withholds_the_document_and_the_export()
    test_hold_withholds_on_the_full_text_path()
    test_only_a_passing_later_run_clears_the_hold()
    test_exhausted_loop_back_target_holds_instead_of_delivering()
    test_capped_producer_is_skipped_so_the_gate_still_judges()
    test_capped_step_with_a_false_run_condition_is_still_only_skipped()
    test_oscillating_repair_is_no_progress()
    test_carried_pass_ends_the_loop()
    test_dead_engine_judge_is_not_attributed_to_an_engine()
    test_key_mode_export_is_held_too()
    test_a_failing_gate_with_no_repair_loop_holds()
    test_carried_pass_ends_the_loop_at_the_parallel_join_too()
    test_a_non_routing_lane_carried_fail_holds_the_join()
    test_judge_outage_holds_and_says_the_gate_was_not_evaluated()
    test_a_human_disposition_survives_a_later_gate_less_run()
    test_a_qe_routing_gate_is_not_a_delivery_block()
    test_a_producer_after_the_passing_gate_does_not_release()
    test_the_export_refusal_names_the_actual_cause()
    test_an_operator_pause_edit_is_not_demoted_and_survives_the_next_run()
    test_the_release_does_not_depend_on_step_order()
    test_a_qe_loop_ending_hold_is_releasable_by_its_own_workflow()
    test_a_retyped_gate_step_fails_closed_like_a_deleted_one()
    test_approval_clears_the_badge_and_a_release_reads_mt()
    test_linear_run_is_byte_stable()
    # LAST: this arm creates a CUSTOMER, and once any customer exists the web project
    # form requires one -- so every `new_project()` fixture above must run before it.
    test_key_mode_export_is_driven_and_refused()
    print("LQA LOOP PROGRESS OK")


if __name__ == "__main__":
    main()
