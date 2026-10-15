"""#505: smaller run-state defects from the PR 472 review. Hermetic + offline.

One section per defect; each fails against the pre-fix tree.

  [1] queue_runs      a stale-run supersede that loses the writer lock must not fail the pair
                      whose QUEUED row is committed, and must not poison later pairs
                      (PendingRollbackError).
  [2] resume race     two overlapping SYNC resumes of one paused run: exactly one executes,
                      the other is refused, and the cursor advances exactly once.
  [3] over-translation an "accept as is" resume must not commit a withheld page dump as MT.
  [4] replay text     observe_run's replayed `done` text equals the live one when TM-LQA
                      exclusion left the last step's blob compact (or empty).
  [5] gate supersede  a repair from an EARLIER loop does not clear a LATER gate failure.
  [6] parallel lane   a judge outage on a NON-routing parallel LQA lane holds the run.
  [7] crash handler   a crash with run_id=None marks only the run of THIS locale.
  [8] progress        step index 0 is published (a loop re-entering step 0 overwrites).

Final line: "ORCHESTRATOR RUN STATE 505 OK".
"""

import os
import sys
import threading
import time
from datetime import datetime, timedelta
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_runstate505_")

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app import db as _db
from app.db import engine, init_db
from app.models import (
    Document,
    GlossaryEntry,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import lqa, orchestrator, settings, tm
from app.services import orchestrator_part6 as part6
from app.services import orchestrator_part9 as part9
from app.services.providers import Provider, register_provider

init_db()
settings.set_many(
    {
        "security_auth_required": "0",
        "multi_org_enabled": "0",
        "project_scoping": "0",
        "parallel_routing_enabled": "1",
    }
)
settings.invalidate_cache()

_FAILS = []
_N = [0]


def _check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILS.append(name)


def _seq():
    _N[0] += 1
    return _N[0]


def _mk_project(locales=("fr-FR",)):
    with Session(engine) as s:
        p = Project(name=f"RS505-{_seq()}", source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in locales:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        return p.id


def _mk_doc(pid, sources, locale="fr-FR"):
    with Session(engine) as s:
        d = Document(project_id=pid, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(sources):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale=locale,
                    order_index=i,
                    source_text=src,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return d.id


def _mk_wf(steps):
    with Session(engine) as s:
        wf = Workflow(name=f"RS505WF-{_seq()}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for i, kw in enumerate(steps):
            kw = dict(kw)
            kw.setdefault("provider", "mock")
            s.add(
                WorkflowStep(
                    workflow_id=wf.id,
                    order_index=i,
                    chunk_policy="auto",
                    retry_max=1,
                    backoff_base=0.0,
                    **kw,
                )
            )
        s.commit()
        return wf.id


def _locked():
    return OperationalError("UPDATE workflowrun", {}, Exception("database is locked"))


# =============================================================================================
# [1] queue_runs: a locked stale-run supersede
# =============================================================================================
def _stale_run(wf_id, doc_id, locale):
    with Session(engine) as s:
        r = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_id,
            target_locale=locale,
            status=RunStatus.RUNNING,
            started_at=datetime.utcnow() - timedelta(hours=30),
        )
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


def _queue_with_locked_supersede(always: bool):
    pid = _mk_project(("fr-FR", "de-DE"))
    doc = _mk_doc(pid, ["One."], "fr-FR")
    wf = _mk_wf(
        [{"name": "T", "step_type": StepType.LLM_AGENT, "prompt_text": "Translate: {text}"}]
    )
    stale_id = _stale_run(wf, doc, "fr-FR")
    plan = [(wf, doc, "fr-FR", "d.txt"), (wf, doc, "de-DE", "d.txt")]
    state = {"raised": 0}
    with Session(engine) as s:
        orig = s.commit

        def flaky():
            doomed = any(
                isinstance(o, WorkflowRun) and o.status == RunStatus.FAILED for o in s.dirty
            )
            if doomed and (always or not state["raised"]):
                state["raised"] += 1
                raise _locked()
            return orig()

        s.commit = flaky
        with mock.patch.object(_db.time, "sleep", lambda _x: None):
            out = orchestrator.queue_runs(s, plan, created_by="t", start_scheduler=False)
        s.__dict__.pop("commit", None)
    with Session(engine) as s:
        stale = s.get(WorkflowRun, stale_id)
        rows = s.exec(
            select(WorkflowRun).where(
                WorkflowRun.document_id == doc, WorkflowRun.status == RunStatus.QUEUED
            )
        ).all()
        return out, stale.status, sorted(r.target_locale for r in rows), state["raised"]


out, stale_status, queued_locales, raised = _queue_with_locked_supersede(always=False)
_check("[1] the locked supersede commit fired", raised >= 1, str(raised))
_check(
    "[1] pair 1 is reported queued, not failed",
    len(out["queued"]) == 2 and not out["failed"],
    str(out),
)
_check("[1] both QUEUED rows exist", queued_locales == ["de-DE", "fr-FR"], str(queued_locales))
_check(
    "[1] the stale run was superseded after the retry",
    stale_status == RunStatus.FAILED,
    str(stale_status),
)

out, stale_status, queued_locales, raised = _queue_with_locked_supersede(always=True)
_check(
    "[1] supersede that never wins the lock: pairs still queued, none failed",
    len(out["queued"]) == 2 and not out["failed"],
    str(out),
)
_check(
    "[1] ...and the stale run is left for the next sweep",
    stale_status == RunStatus.RUNNING,
    str(stale_status),
)

# =============================================================================================
# [2] sync resume overlap
# =============================================================================================
pid = _mk_project()
doc = _mk_doc(pid, ["Alpha one.", "Beta two."])
wf = _mk_wf(
    [
        {"name": "T", "step_type": StepType.LLM_AGENT, "prompt_text": "Translate: {text}"},
        {"name": "H", "step_type": StepType.HUMAN_REVIEW},
        {"name": "T2", "step_type": StepType.LLM_AGENT, "prompt_text": "Polish: {text}"},
    ]
)
with Session(engine) as s:
    first = orchestrator.run_workflow(s, wf, doc, "fr-FR")
run_id = first["run_id"]
with Session(engine) as s:
    paused_at = s.get(WorkflowRun, run_id).current_step_index
_check("[2] fixture run is paused", first["status"] == "paused", str(first["status"]))

streams = {"n": 0}
real_stream = orchestrator.run_workflow_stream


def slow_stream(session, *a, **kw):
    streams["n"] += 1
    time.sleep(0.6)
    yield {"type": "done", "run_id": run_id, "text": "x"}


results = []


def _resume():
    with Session(engine) as s:
        results.append(orchestrator.resume_workflow(s, run_id, "fr-FR", ""))


with mock.patch.object(orchestrator, "run_workflow_stream", slow_stream):
    a = threading.Thread(target=_resume)
    b = threading.Thread(target=_resume)
    a.start()
    time.sleep(0.15)
    b.start()
    a.join(20)
    b.join(20)
with Session(engine) as s:
    advanced = s.get(WorkflowRun, run_id).current_step_index
_check("[2] the resumed segment set executed exactly once", streams["n"] == 1, str(streams["n"]))
_check(
    "[2] the second resume was refused",
    sorted(r["status"] for r in results) == ["completed", "rejected"],
    str([r["status"] for r in results]),
)
_check(
    "[2] the cursor advanced exactly once", advanced == paused_at + 1, f"{paused_at} -> {advanced}"
)

# =============================================================================================
# [3] over-translation dump survives a pause
# =============================================================================================


class DumpProvider(Provider):
    name = "dumpprov505"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        t = (payload or {}).get("text", "")
        if "Two" in t:
            return "Voici la page complete. " + " ".join(
                ["Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod."] * 30
            )
        return "ok-" + t


register_provider("dumpprov505", DumpProvider())
pid = _mk_project()
doc = _mk_doc(pid, ["One.", "Two.", "Three."])
wf = _mk_wf(
    [
        {
            "name": "T",
            "step_type": StepType.LLM_AGENT,
            "provider": "dumpprov505",
            "prompt_text": "Translate: {text}",
        },
        {"name": "H", "step_type": StepType.HUMAN_REVIEW},
    ]
)
with Session(engine) as s:
    r1 = orchestrator.run_workflow(s, wf, doc, "fr-FR")
with Session(engine) as s:
    r2 = orchestrator.resume_workflow(s, r1["run_id"], "fr-FR", "")
with Session(engine) as s:
    segs = s.exec(
        select(Segment).where(Segment.document_id == doc).order_by(Segment.order_index)
    ).all()
_check(
    "[3] the run paused then completed", r1["status"] == "paused" and r2["status"] == "completed"
)
_check(
    "[3] the dump was NOT committed as target text on accept-as-is",
    not (segs[1].target_text or "").strip(),
    repr((segs[1].target_text or "")[:60]),
)
_check(
    "[3] the dump is routed to review",
    segs[1].status == SegStatus.NEEDS_REVIEW,
    str(segs[1].status),
)
_check(
    "[3] over-translation flag recorded",
    any(f.get("type") == "nontranslation" for f in (segs[1].qa_flags or [])),
    str(segs[1].qa_flags),
)
_check(
    "[3] the clean neighbours committed",
    segs[0].target_text == "ok-One." and segs[2].target_text == "ok-Three.",
)

# =============================================================================================
# [4] replayed done text with TM-LQA exclusion
# =============================================================================================


def _exclusion_run(all_locked):
    settings.set_many(
        {
            "tm_hundreds_enter_lqa": "1",
            "tm_fix_protection_enabled": "1",
            "lqa_tm_lock_exclusion_enabled": "1",
        }
    )
    pid = _mk_project()
    with Session(engine) as s:
        doc = Document(project_id=pid, filename="scope.txt", fmt="txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        sources = ["Save changes."] + ([] if all_locked else ["Fresh line."])
        for i, src in enumerate(sources):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale="fr-FR",
                    order_index=i,
                    source_text=src,
                    target_text="",
                )
            )
        s.add(
            GlossaryEntry(
                project_id=pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_term="Save",
                target_term="Enregistrer",
            )
        )
        s.commit()
        tm.write_back(s, pid, sources[0], "Enregistrer.", "en-US", "fr-FR")
        doc_id = doc.id
    wf = _mk_wf(
        [
            {"name": "TM", "key": "s0", "step_type": StepType.TM_MATCH},
            {
                "name": "MT",
                "key": "s1",
                "step_type": StepType.LLM_AGENT,
                "prompt_text": "Translate: {text}",
                "max_visits": 3,
            },
            {
                "name": "LQA",
                "key": "s2",
                "step_type": StepType.LQA,
                "max_visits": 3,
                "branch": {"on_fail": {"step": "s1"}, "on_pass": "end"},
            },
        ]
    )
    errors = [{"dimension": "style", "severity": "minor", "note": "fresh issue"}]
    original = lqa.score_text

    def score(source, target, *a, **kw):
        with mock.patch.object(
            lqa, "_llm_errors", return_value=(errors, 0, 0, 0, lqa.JUDGE_OK, None)
        ):
            return original(source, target, *a, **kw)

    with Session(engine) as s, mock.patch.object(lqa, "score_text", side_effect=score):
        events = list(orchestrator.run_workflow_stream(s, wf, doc_id, "fr-FR"))
    run_id = next(e["run_id"] for e in events if e["type"] == "run_started")
    live = next(e for e in events if e["type"] == "done")["text"]
    replay_events = list(orchestrator.observe_run(run_id, poll_interval=0.05, max_seconds=20))
    replay = next((e for _r, e in replay_events if e["type"] == "done"), {}).get("text")
    return live, replay


for label, locked in (("some units excluded", False), ("every unit excluded", True)):
    live, replay = _exclusion_run(locked)
    _check(
        f"[4] replayed done text equals the live one ({label})",
        live == replay,
        f"{live!r} vs {replay!r}",
    )
    _check(f"[4] the live text is not empty ({label})", bool(live.strip()), repr(live))

# Review: a RUNNING run is polled every 0.4 s and never emits 'done', so the full-width blob
# lookup (segment load) must not run for it; only a COMPLETED run pays for it.
from app.services import orchestrator_part10 as _part10

with Session(engine) as _s:
    _live_run = _s.exec(select(WorkflowRun).order_by(WorkflowRun.id.desc())).first()
    _live_run.status = RunStatus.RUNNING
    _s.add(_live_run)
    _s.commit()
    _live_run_id = _live_run.id
_calls = []
_orig_blob = _part10._replay_final_blob


def _spy_blob(*a, **kw):
    _calls.append(1)
    return _orig_blob(*a, **kw)


with mock.patch.object(_part10, "_replay_final_blob", side_effect=_spy_blob):
    list(orchestrator.observe_run(_live_run_id, poll_interval=0.05, max_seconds=0.6))
    _running_calls = len(_calls)
    with Session(engine) as _s:
        _live_run = _s.get(WorkflowRun, _live_run_id)
        _live_run.status = RunStatus.COMPLETED
        _s.add(_live_run)
        _s.commit()
    list(orchestrator.observe_run(_live_run_id, poll_interval=0.05, max_seconds=5))
_check("[4] a RUNNING observer never computes the replay blob", _running_calls == 0, _running_calls)
_check("[4] a COMPLETED observer computes it once", len(_calls) == 1, len(_calls))

# =============================================================================================
# [5] a repair from an EARLIER loop must not clear a LATER gate failure
# =============================================================================================


def _gate_fixture(order):
    """steps: lqa1 (0), fix llm_agent (1), lqa2 (2). `order` is the StepRun write order of
    ('lqa1','failed'|'passed'), ('fix',None), ('lqa2', ...) tuples."""
    wf = _mk_wf(
        [
            {"name": "LQA1", "key": "lqa1", "step_type": StepType.LQA},
            {
                "name": "Fix",
                "key": "fix",
                "step_type": StepType.LLM_AGENT,
                "prompt_text": "Fix {text}",
            },
            {"name": "LQA2", "key": "lqa2", "step_type": StepType.LQA},
        ]
    )
    pid = _mk_project()
    doc = _mk_doc(pid, ["One."])
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wf)
        by_key = {st.key: st for st in steps}
        run = WorkflowRun(
            workflow_id=wf,
            document_id=doc,
            target_locale="fr-FR",
            status=RunStatus.COMPLETED,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        for key, gate in order:
            s.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=by_key[key].id,
                    status=RunStatus.COMPLETED,
                    gate=gate,
                    score=(None if gate is None else (100.0 if gate == "passed" else 40.0)),
                )
            )
            s.commit()
        s.refresh(run)
        steps = orchestrator.get_steps(s, wf)
        return part6._terminal_gate_hold(s, run, steps)


stale_failure = _gate_fixture([("fix", None), ("lqa2", "passed"), ("lqa1", "failed")])
_check(
    "[5] a repair BEFORE the gate's latest failure does not supersede it (run is held)",
    stale_failure is not None,
    str(stale_failure),
)
healthy = _gate_fixture([("lqa1", "failed"), ("fix", None), ("lqa2", "passed")])
_check(
    "[5] a repair between the failure and a later pass still supersedes it",
    healthy is None,
    str(healthy),
)

# =============================================================================================
# [6] judge outage on a NON-routing parallel LQA lane
# =============================================================================================
_real_score = lqa.score_text


def _stub_score(
    source,
    target,
    source_locale,
    target_locale,
    rulebook="",
    provider_name=None,
    style_guide="",
    tap_meta=None,
    spec=None,
    glossary_terms=None,
    lqa_threshold=None,
):
    key = (tap_meta or {}).get("step_key")
    card = {
        "score": 100.0,
        "passed": True,
        "errors": [],
        "gate": "auto_approved",
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
    }
    if key == "lane_term":
        card.update({"score": 0.0, "passed": False, "gate": "needs_review"})
        card["llm_judge"] = lqa.JUDGE_UNAVAILABLE
    return card


pid = _mk_project()
doc = _mk_doc(pid, ["Hello world.", "The cat sat."])
wf = _mk_wf(
    [
        {"name": "T", "step_type": StepType.LLM_AGENT, "prompt_text": "Translate: {text}"},
        {
            "name": "Main",
            "key": "lane_main",
            "step_type": StepType.LQA,
            "parallel_group": "checks",
            "branch": {"on_fail": "end", "on_pass": "next"},
        },
        {"name": "Term", "key": "lane_term", "step_type": StepType.LQA, "parallel_group": "checks"},
        {
            "name": "After",
            "key": "after",
            "step_type": StepType.LLM_AGENT,
            "prompt_text": "Polish: {text}",
        },
    ]
)
lqa.score_text = _stub_score
try:
    with Session(engine) as s:
        res = orchestrator.run_workflow(s, wf, doc, "fr-FR")
finally:
    lqa.score_text = _real_score
with Session(engine) as s:
    ctx = dict(s.get(WorkflowRun, res["run_id"]).context or {})
    rows = s.exec(select(StepRun).where(StepRun.workflow_run_id == res["run_id"])).all()
    after_id = next(st.id for st in orchestrator.get_steps(s, wf) if st.key == "after")
hold = ctx.get("gate_hold") or {}
_check(
    "[6] the outage on the non-routing lane holds the run",
    hold.get("cause") == orchestrator.GATE_HOLD_JUDGE_UNAVAILABLE,
    str(hold),
)
_check(
    "[6] the run ended at the group (the downstream step never ran)",
    not any(r.step_id == after_id for r in rows),
    str([r.step_id for r in rows]),
)

# =============================================================================================
# [7] crash handler with run_id=None
# =============================================================================================
pid = _mk_project(("fr-FR", "de-DE"))
doc = _mk_doc(pid, ["One."], "de-DE")
wf = _mk_wf([{"name": "T", "step_type": StepType.LLM_AGENT, "prompt_text": "Translate: {text}"}])
with Session(engine) as s:
    sibling = WorkflowRun(
        workflow_id=wf, document_id=doc, target_locale="de-DE", status=RunStatus.RUNNING
    )
    s.add(sibling)
    s.commit()
    s.refresh(sibling)
    sibling_id = sibling.id


def _boom(*a, **kw):
    raise RuntimeError("crash before any run row exists")
    yield  # pragma: no cover  (makes this a generator like run_workflow_stream)


with mock.patch.object(orchestrator, "run_workflow_stream", _boom):
    orchestrator._run_in_background(wf, doc, "fr-FR", run_id=None)
    deadline = time.time() + 15
    while time.time() < deadline and any(
        t.is_alive() and t.name.startswith("run-") and f"wf{wf}" in t.name
        for t in threading.enumerate()
    ):
        time.sleep(0.05)
    time.sleep(0.5)
with Session(engine) as s:
    still = s.get(WorkflowRun, sibling_id)
_check(
    "[7] a sibling locale's run is not marked FAILED by another locale's crash",
    still.status == RunStatus.RUNNING,
    str(still.status),
)

# =============================================================================================
# [8] progress step index 0
# =============================================================================================
rid = 987001
part9.publish_run_progress(rid, 1, 5, "segments", step="Later", step_index=2)
part9.publish_run_progress(rid, 0, 5, "segments", step="First", step_index=0, force=True)
with part9._progress_lock:
    published = dict(part9._progress_state.get(rid) or {})
_check(
    "[8] step_index 0 is published (not skipped as falsy)",
    published.get("step_index") == 0 and published.get("step") == "First",
    str(published),
)
orchestrator._progress_forget(rid)

if _FAILS:
    print("FAILED:", "; ".join(_FAILS))
    sys.exit(1)
print("ORCHESTRATOR RUN STATE 505 OK")
