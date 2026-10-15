"""#504: a lock-retried step-write commit must re-apply the run's FULL staged state.

`db.commit_with_retry` rolls the session back between "database is locked" attempts; the
rollback reverts every pending change on the WorkflowRun (and expires its JSON `context`).
The step-write retry stage used to re-apply only the StepRun, `qe_steps` and the cursor, so a
QE-gated step whose correction loop was exhausted lost its `gate_hold` (and the warning), the
retry succeeded, and the failed-gate text was committed as clean MT. The TM-match branch had
the same flaw: a retry lost `run.used_tm_match`.

Hermetic + offline. A `database is locked` error is injected into the FIRST commit attempt of
the targeted stage, exactly where the writer lock is lost in production, and the run is driven
end to end.

  QE gate hold   a QE-gated step fails its gate, its on_fail loop-back target is at its visit
                 ceiling (GATE_HOLD_LOOP_EXHAUSTED); one locked commit on the step write; the
                 run's context still carries `gate_hold` + the gate-hold warning + `qe_steps`.
  control        the same run with NO injected lock holds identically (the injection really
                 exercises the retry path: the injected run's lock error fired exactly once).
  TM match       a tm_match step; one locked commit on its step write; `used_tm_match` is True
                 and the StepRun + cursor advance landed.

Final line: "STEP WRITE RETRY STATE OK".
"""

import os
import sys
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_steprty_")

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app import db as _db
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
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
from app.services import orchestrator, settings
from app.services.providers import MockProvider, register_provider

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

_FAILS = []


def _check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILS.append(name)


class QeFailExempt(MockProvider):
    """A QE judge that scores every segment below threshold (gate 'failed'); a MockProvider
    subclass so it is spend-exempt and fully offline."""

    name = "qefailexempt"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return '{"accuracy":5,"fluency":5,"terminology":5,"style":5,"rationale":"bad"}'


register_provider("qefailexempt", QeFailExempt())

_N = 0


def _mk_doc(texts):
    global _N
    _N += 1
    with Session(engine) as s:
        p = Project(name=f"RetryState{_N}", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, t in enumerate(texts):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale="fr-FR",
                    order_index=i,
                    source_text=t,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return d.id


def _mk_wf(steps):
    global _N
    _N += 1
    with Session(engine) as s:
        wf = Workflow(name=f"RetryWF{_N}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for i, kw in enumerate(steps):
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


class Injector:
    """Wraps db.commit_with_retry: for the first call whose stage is `stage_name` (and whose
    run satisfies `when`), the FIRST session.commit() raises a locked error, so the helper
    rolls back and retries exactly as it does under writer contention."""

    def __init__(self, stage_name, when=lambda run: True):
        self.stage_name = stage_name
        self.when = when
        self.fired = 0
        self.real = _db.commit_with_retry

    def __call__(self, session, *, stage=None, **kw):
        armed = False
        if (
            stage is not None
            and getattr(stage, "__name__", "") == self.stage_name
            and not self.fired
        ):
            free = dict(
                zip(
                    stage.__code__.co_freevars,
                    [c.cell_contents for c in (stage.__closure__ or ())],
                    strict=False,
                )
            )
            run = free.get("run")
            armed = run is not None and self.when(run)
        if not armed:
            return self.real(session, stage=stage, **kw)
        orig_commit = session.commit

        def flaky():
            session.__dict__.pop("commit", None)
            self.fired += 1
            raise _locked()

        session.commit = flaky
        try:
            return self.real(session, stage=stage, **kw)
        finally:
            session.__dict__.pop("commit", None)
            del orig_commit


def _hold_wf():
    # step 0: plain MT; step 1: a QE-gated step that self-loops on_fail but may only visit
    # once, so a failed gate has its loop-back REFUSED -> GATE_HOLD_LOOP_EXHAUSTED.
    return _mk_wf(
        [
            {
                "name": "Translator",
                "step_type": StepType.LLM_AGENT,
                "provider": "mock",
                "prompt_text": "Translate: {text}",
            },
            {
                "name": "Fix",
                "key": "fix",
                "step_type": StepType.LLM_AGENT,
                "provider": "mock",
                "prompt_text": "Fix: {text}",
                "qe_gate": True,
                "qe_provider": "qefailexempt",
                "qe_threshold": 90,
                "max_visits": 1,
                "branch": {"on_pass": "end", "on_fail": {"step": "fix"}},
            },
        ]
    )


def _ctx(run_id):
    with Session(engine) as s:
        return dict(s.get(WorkflowRun, run_id).context or {})


def _holds(ctx):
    return [
        w
        for w in (ctx.get("warnings") or [])
        if w.get("code") == orchestrator.GATE_HOLD_WARNING_CODE
    ]


# --- control: no injection, the run holds ---------------------------------------------------
doc = _mk_doc(["Alpha one.", "Beta two."])
wf = _hold_wf()
with Session(engine) as s:
    ctl = orchestrator.run_workflow(s, wf, doc, "fr-FR")
cctx = _ctx(ctl["run_id"])
_check(
    "control: gate_hold recorded without any lock",
    bool(cctx.get("gate_hold")),
    str(cctx.get("gate_hold")),
)
_check(
    "control: hold cause is loop_exhausted",
    bool(_holds(cctx)) and _holds(cctx)[0]["cause"] == orchestrator.GATE_HOLD_LOOP_EXHAUSTED,
    str(cctx.get("warnings")),
)

# --- the bug: one locked commit on the gate-hold step write ----------------------------------
doc = _mk_doc(["Gamma one.", "Delta two."])
wf = _hold_wf()
inj = Injector("_stage_step_write", when=lambda run: bool((run.context or {}).get("gate_hold")))
with mock.patch.object(_db, "commit_with_retry", inj), Session(engine) as s:
    res = orchestrator.run_workflow(s, wf, doc, "fr-FR")
ictx = _ctx(res["run_id"])
_check("QE hold: the locked commit fired exactly once", inj.fired == 1, str(inj.fired))
_check(
    "QE hold: gate_hold survived the retried commit",
    bool(ictx.get("gate_hold")),
    str(ictx.get("gate_hold")),
)
_check(
    "QE hold: gate-hold warning survived (cause loop_exhausted)",
    bool(_holds(ictx)) and _holds(ictx)[0]["cause"] == orchestrator.GATE_HOLD_LOOP_EXHAUSTED,
    str(ictx.get("warnings")),
)
_check("QE hold: qe_steps history survived", bool(ictx.get("qe_steps")), str(ictx.get("qe_steps")))
with Session(engine) as s:
    srs = s.exec(select(StepRun).where(StepRun.workflow_run_id == res["run_id"])).all()
_check(
    "QE hold: the Fix StepRun landed once with a failed gate",
    sum(1 for sr in srs if sr.gate == "failed") == 1,
    str([(sr.step_id, sr.gate) for sr in srs]),
)
_check("QE hold: same warnings as the lock-free control", len(_holds(ictx)) == len(_holds(cctx)))

# --- TM match branch: used_tm_match survives a locked step-write commit ----------------------
doc = _mk_doc(["Epsilon one.", "Zeta two."])
wf = _mk_wf([{"name": "TM", "step_type": StepType.TM_MATCH}])
inj = Injector("_stage_tm_write")
with mock.patch.object(_db, "commit_with_retry", inj), Session(engine) as s:
    tres = orchestrator.run_workflow(s, wf, doc, "fr-FR")
_check("TM match: the locked commit fired exactly once", inj.fired == 1, str(inj.fired))
with Session(engine) as s:
    trun = s.get(WorkflowRun, tres["run_id"])
    tsr = s.exec(select(StepRun).where(StepRun.workflow_run_id == tres["run_id"])).all()
    _check("TM match: used_tm_match survived the retried commit", bool(trun.used_tm_match))
    _check("TM match: StepRun landed exactly once", len(tsr) == 1, str(len(tsr)))
    _check("TM match: run completed", trun.status == "completed", str(trun.status))

if _FAILS:
    print("FAILED:", ", ".join(_FAILS))
    sys.exit(1)
print("STEP WRITE RETRY STATE OK")
