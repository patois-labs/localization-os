"""#159: correction-loop LQA judges only repairable, unlocked units. Offline."""

import os
import sys
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_lqa_tm_exclusion_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    GlossaryEntry,
    Project,
    RunStatus,
    Segment,
    StepRun,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import lqa, orchestrator, settings, tm
from app.services.orchestrator_terminology_backen import SEG_JOINER

init_db()


def exercise(
    *,
    loop=True,
    override=None,
    all_locked=False,
    enabled=True,
    protection=True,
    parallel=False,
    hard=False,
    gated=False,
    repair=False,
):
    calls = []
    settings.set_many(
        {
            "tm_hundreds_enter_lqa": "0" if hard else "1",
            "tm_fix_protection_enabled": "1" if protection else "0",
            "lqa_tm_lock_exclusion_enabled": "1" if enabled else "0",
            "parallel_routing_enabled": "1",
        }
    )
    original = lqa.score_text

    def score(source, target, *args, **kwargs):
        calls.append((source, target, kwargs.get("glossary_terms")))
        errors = [{"dimension": "style", "severity": "minor", "note": "fresh issue"}]
        if repair and len(calls) == 1:
            errors.append(
                {"dimension": "accuracy", "severity": "major", "note": "repairable issue"}
            )
        if "Enregistrer" in target:
            errors.append({"dimension": "accuracy", "severity": "major", "note": "locked issue"})
        # Exercise real score assembly, adjudication and gate with deterministic findings.
        with patch.object(lqa, "_llm_errors", return_value=(errors, 0, 0, 0, lqa.JUDGE_OK, None)):
            card = original(source, target, *args, **kwargs)

        return card

    with Session(engine) as s:
        project = Project(name="Scope", source_locale="en-US", default_provider="mock")
        s.add(project)
        s.commit()
        doc = Document(
            project_id=project.id,
            filename="scope.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            status="new",
        )
        wf = Workflow(name="Scope")
        s.add(doc)
        s.add(wf)
        s.add(TargetLocale(project_id=project.id, locale="fr-FR"))
        s.commit()
        sources = ["Save changes."] + ([] if all_locked else ["Fresh line."])
        for i, source in enumerate(sources):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale="fr-FR",
                    order_index=i,
                    source_text=source,
                    target_text="",
                )
            )
        s.add(
            GlossaryEntry(
                project_id=project.id,
                source_locale="en-US",
                target_locale="fr-FR",
                source_term="Save",
                target_term="Enregistrer",
            )
        )
        s.add(
            GlossaryEntry(
                project_id=project.id,
                source_locale="en-US",
                target_locale="fr-FR",
                source_term="Fresh",
                target_term="frais",
            )
        )
        s.commit()
        tm.write_back(s, project.id, sources[0], "Enregistrer.", "en-US", "fr-FR")
        kinds = [StepType.TM_MATCH, StepType.LLM_AGENT, StepType.LQA]
        if parallel:
            kinds.append(StepType.LQA)
        if gated:
            kinds.append(StepType.LLM_AGENT)
        steps = []
        for i, kind in enumerate(kinds):
            st = WorkflowStep(
                workflow_id=wf.id,
                order_index=i,
                key=f"s{i}",
                name=f"Step {i}",
                step_type=kind,
                provider="mock",
            )
            if kind == StepType.LQA:
                st.max_visits = 3
                if loop and not gated and i == 2:
                    st.branch = {"on_fail": {"step": "s1"}, "on_pass": "end"}
                if parallel:
                    st.parallel_group = "checks"
            if i == 1 or (gated and i == len(kinds) - 1):
                st.tm_protect = override
                st.max_visits = 3
            if gated and i == len(kinds) - 1:
                st.run_condition = {"ref": "prev_lqa", "op": "failed", "value": None}
            s.add(st)
            steps.append(st)
        s.commit()
        with patch.object(lqa, "score_text", side_effect=score):
            events = list(orchestrator.run_workflow_stream(s, wf.id, doc.id, "fr-FR"))
        run_id = next(e["run_id"] for e in events if e["type"] == "run_started")
        s.expire_all()
        rows = s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()
        checks = [
            r for r in rows if r.step_id in {st.id for st in steps if st.step_type == StepType.LQA}
        ]
        run = s.get(WorkflowRun, run_id)
        excluded = loop and override is not False and enabled and protection
        if excluded:
            done = next(e for e in events if e["type"] == "done")
            assert done["lqa_safety"]["lqa_tm_excluded"] == 1, done
            assert run.status == RunStatus.COMPLETED, run
            assert not (run.context or {}).get("gate_hold"), run.context
            assert not (run.context or {}).get("loop_ended"), run.context
            assert len(checks) == (2 if parallel or repair else 1), checks
            assert all("excluded 1 protected segment(s)" in r.error for r in checks)
            assert all("fixer may not repair" in r.error for r in checks)
            if all_locked:
                assert not calls, calls
                assert all(
                    r.status == RunStatus.SKIPPED and r.score is None and r.gate is None
                    for r in checks
                )
                assert any(e.get("lqa", {}).get("skipped") for e in events)
                assert not (run.context or {}).get("lqa_findings_block")
            else:
                assert len(calls) == len(checks), calls
                assert all(
                    source == "Fresh line." and "Enregistrer" not in target
                    for source, target, _ in calls
                ), calls
                assert all(
                    [t["source_term"] for t in terms] == ["Fresh"] for _, _, terms in calls
                ), calls
                assert checks[-1].score == 99 and checks[-1].gate == "passed", checks
                if repair:
                    assert checks[0].gate == "failed", checks
                # Compact score blobs must retain original positions when releasing holds.
                targets = s.exec(
                    select(Segment)
                    .where(Segment.document_id == doc.id)
                    .order_by(Segment.order_index)
                ).all()
                assert orchestrator._gate_release_mask(
                    s, run, [t.target_text for t in targets]
                ) == [False, True]
                if parallel:
                    assert any(e.get("parallel_group") == "checks" for e in events)
                assert any(e.get("lqa", {}).get("judged") == 1 for e in events)
            assert sum(r.step_id == steps[1].id for r in rows) == (2 if repair else 1), rows
        else:
            assert calls and calls[0][0] == SEG_JOINER.join(sources), calls
            assert "Enregistrer." in calls[0][1], calls
            assert not any("LQA excluded" in (r.error or "") for r in checks)
        return calls, [(r.input_text, r.output_text, r.score, r.gate, r.error) for r in checks]


exercise()
print("  mixed correction loop: only unlocked source/target/terms; score 99; one visit; trace: OK")
a = exercise(loop=False)
b = exercise(loop=False, enabled=False)
assert a == b, "non-loop judge inputs and persisted verdicts must be byte-identical"
print("  non-loop ON/OFF byte-identical: OK")
exercise(loop=False, override=True)
exercise(override=False)
exercise(gated=True, override=False)
exercise(protection=False)
exercise(enabled=False)
print("  explicit False and both kill switches retain TM in LQA: OK")
exercise(repair=True)
exercise(all_locked=True)
exercise(parallel=True)
exercise(parallel=True, all_locked=True)
exercise(hard=True)
exercise(gated=True)
print("  zero eligible skips with no verdict; parallel, hard lock, gated-fixer shapes: OK")
print("verify_lqa_tm_exclusion: OK")
