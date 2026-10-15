"""#159 gate regressions: provenance, per-check repair policy, and empty routing."""

import os
import sys
from contextlib import ExitStack
from itertools import product
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_lqa_tm_shapes_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
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
from app.services import lqa, lqa_safety, orchestrator, settings, tm, tm_lock

init_db()
assert settings.DEFAULTS["lqa_tm_lock_exclusion_enabled"] == "1"


def exercise(shape, *, identity=False):
    settings.set_many(
        {
            "tm_hundreds_enter_lqa": "1",
            "tm_fix_protection_enabled": "1",
            "lqa_tm_lock_exclusion_enabled": "0" if identity else "1",
            "parallel_routing_enabled": "1",
        }
    )
    calls = []
    original = lqa.score_text

    def score(source, target, *args, **kwargs):
        calls.append((source, target))
        errors = []
        if (shape == "prior_soft" and len(calls) == 1) or (
            shape in {"prior_hard", "content", "approved"} and "Enregistrer" in target
        ):
            errors = [{"dimension": "accuracy", "severity": "major", "note": "repair"}]
        with patch.object(lqa, "_llm_errors", return_value=(errors, 0, 0, 0, lqa.JUDGE_OK, None)):
            return original(source, target, *args, **kwargs)

    with Session(engine) as s, ExitStack() as stack:
        p = Project(name=shape, source_locale="en-US", default_provider="mock")
        w = Workflow(name=shape)
        s.add(p)
        s.add(w)
        s.commit()
        d = Document(project_id=p.id, filename="test.txt", fmt="txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        empty = shape in {
            "end",
            "guard",
            "key_guard",
            "parallel_end",
            "parallel_guard",
            "passed_guard",
        }
        sources = ["Save changes."] + ([] if empty else ["Fresh line."])
        for i, source in enumerate(sources):
            seg = Segment(
                document_id=d.id,
                target_locale="fr-FR",
                order_index=i,
                source_text=source,
                target_text="Enregistrer."
                if shape in {"prior_soft", "content", "approved"} and i == 0
                else "",
            )
            if shape == "prior_soft" and i == 0:
                tm_lock._stamp_lock(seg, band="100", hard=False, tm_id=1, entry_id=1)
            if shape == "content" and i == 0:
                seg.status = SegStatus.NEEDS_REVIEW
                seg.tm_match_pct = 100
                seg.qa_flags = [{"type": "qe", "severity": "major", "message": "review"}]
            if shape == "approved" and i == 0:
                seg.status = SegStatus.APPROVED
            s.add(seg)
        s.commit()
        if shape != "prior_soft":
            tm.write_back(s, p.id, sources[0], "Enregistrer.", "en-US", "fr-FR")
        if shape == "prior_hard":
            seed = Workflow(name="persist hard lock")
            s.add(seed)
            s.commit()
            s.add(WorkflowStep(workflow_id=seed.id, order_index=0, step_type=StepType.TM_MATCH))
            s.commit()
            settings.set_many({"tm_hundreds_enter_lqa": "0"})
            list(orchestrator.run_workflow_stream(s, seed.id, d.id, "fr-FR"))
            s.expire_all()
            locked = s.exec(
                select(Segment).where(Segment.document_id == d.id, Segment.order_index == 0)
            ).one()
            assert locked.lock_band and locked.lock_hard
        specs = [
            ("translate", StepType.LLM_AGENT),
            ("check", StepType.LQA),
            ("sentinel", StepType.LLM_AGENT),
        ]
        if shape in {
            "prior_hard",
            "content",
            "approved",
            "passed_guard",
            "scope",
            "fuzzy_none",
            "two_checks",
            "opt_out",
            "end",
            "guard",
            "key_guard",
            "parallel_end",
            "parallel_guard",
        }:
            specs.insert(0, ("tm", StepType.TM_MATCH))
        if shape == "parallel_guard":
            specs.insert(2, ("earlier", StepType.LQA))
        if shape in {"two_checks", "opt_out"}:
            specs.insert(specs.index(("check", StepType.LQA)), ("standalone", StepType.LQA))
        if shape in {"parallel_end", "parallel_guard"}:
            specs.insert(-1, ("peer", StepType.LQA))
        steps = {}
        for i, (key, kind) in enumerate(specs):
            st = WorkflowStep(
                workflow_id=w.id,
                order_index=i,
                key=key,
                name=key,
                step_type=kind,
                provider="mock",
                max_visits=3,
            )
            if key == "earlier":
                st.branch = {"on_fail": {"step": "translate"}, "on_pass": "next"}
            if key == "check":
                st.branch = {"on_fail": {"step": "translate"}, "on_pass": "next"}
                if shape in {
                    "end",
                    "parallel_end",
                    "parallel_guard",
                    "no_tm_step",
                    "fuzzy_none",
                    "locks_off",
                }:
                    st.branch["on_pass"] = "end"
                if shape == "guard":
                    st.branch = None
            if key == "sentinel" and shape in {"guard", "key_guard", "passed_guard"}:
                st.run_condition = {
                    "ref": "step:check" if shape == "key_guard" else "prev_lqa",
                    "op": "passed" if shape == "passed_guard" else "failed",
                }
            if shape == "opt_out":
                if key == "translate":
                    st.tm_protect = False
                if key == "sentinel":
                    st.run_condition = {"ref": "prev_lqa", "op": "failed"}
            if shape in {"parallel_end", "parallel_guard"} and key in {"check", "peer"}:
                st.parallel_group = "checks"
            if shape == "parallel_guard" and key == "peer":
                st.run_condition = {"ref": "prev_lqa", "op": "passed"}
            if shape == "fuzzy_none" and key == "tm":
                st.tm_fill_threshold = 80
                st.tm_fuzzy_lock_mode = "none"
            s.add(st)
            steps[key] = st
        s.commit()
        if shape == "locks_off":
            stack.enter_context(patch.object(config, "TM_LOCKS_ENABLED", False))
        if shape == "fuzzy_none":
            lookup = tm.lookup_batch

            def fuzzy(*args, **kwargs):
                hits = lookup(*args, **kwargs)
                for hit in hits.values():
                    if hit:
                        hit["match_pct"] = 85
                return hits

            stack.enter_context(patch.object(tm, "lookup_batch", side_effect=fuzzy))
        stack.enter_context(patch.object(lqa, "score_text", side_effect=score))
        events = list(orchestrator.run_workflow_stream(s, w.id, d.id, "fr-FR"))
        run_id = next(e["run_id"] for e in events if e["type"] == "run_started")
        s.expire_all()
        rows = s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()
        checks = [r for r in rows if r.step_id == steps["check"].id]
        if identity:
            return (
                calls,
                [(r.input_text, r.output_text, r.status, r.score, r.gate, r.error) for r in rows],
                [(e.get("step_name"), e.get("output"), e.get("lqa_safety")) for e in events],
                [
                    (seg.target_text, seg.status, seg.tm_match_pct, seg.lock_band, seg.lock_hard)
                    for seg in s.exec(
                        select(Segment)
                        .where(Segment.document_id == d.id)
                        .order_by(Segment.order_index)
                    ).all()
                ],
            )
        expected_excluded = 0 if shape in {"prior_soft", "opt_out"} else 1
        done = next(e for e in events if e["type"] == "done")
        assert done.get("lqa_safety", {}).get("lqa_tm_excluded", 0) == expected_excluded, done
        if shape in {
            "prior_hard",
            "content",
            "approved",
            "scope",
            "no_tm_step",
            "fuzzy_none",
            "locks_off",
        }:
            assert calls and calls[0][0] == "Fresh line.", f"protected TM was judged: {calls}"
            filled = s.exec(
                select(Segment).where(Segment.document_id == d.id, Segment.order_index == 0)
            ).one()
            assert len(checks) == 1 and len(calls) == 1, (checks, calls)
            run = s.get(WorkflowRun, run_id)
            assert run.status == RunStatus.COMPLETED
            assert not run.context.get("loop_ended") and not run.context.get("gate_hold")
            if shape not in {"prior_hard", "scope"}:
                assert filled.target_text == "Enregistrer." and not filled.lock_band, filled
            if shape == "scope":
                ctx = s.get(WorkflowRun, run_id).context
                assert "lqa_judged_indices" not in ctx, ctx
                assert list(ctx["lqa_excluded_indices"].values()) == [[0]], ctx
                assert all("indices" not in e.get("lqa", {}) for e in events)
                assert any(e.get("lqa", {}).get("excluded_indices") == [0] for e in events)
                from app.services.orchestrator_part3 import _last_gate_judged_text

                text, indices = _last_gate_judged_text(s, run_id, with_indices=True)
                assert text == calls[0][1] and indices == [1], (text, indices)
                assert orchestrator._gate_release_mask(
                    s, s.get(WorkflowRun, run_id), ["Enregistrer.", calls[0][1]]
                ) == [False, True]

            if shape == "fuzzy_none":
                assert filled.tm_match_pct == 85, filled
        elif shape == "prior_soft":
            assert calls and "Save changes." in calls[0][0], (
                f"editable prior soft lock was unjudged: {calls}"
            )
            assert len(calls) == 2 and "[fr-FR] Enregistrer." in calls[-1][1], calls
        elif shape == "two_checks":
            assert "Save changes." in calls[0][0], f"standalone LQA lost TM: {calls}"
            assert calls[1][0] == "Fresh line.", calls
        elif shape == "opt_out":
            assert (
                "Save changes." in checks[0].input_text or "Enregistrer" in checks[0].input_text
            ), checks
            assert not checks[0].error or "LQA excluded" not in checks[0].error, checks[0].error
        else:
            assert not calls, calls
            assert (
                checks[0].status == RunStatus.SKIPPED
                and checks[0].score is None
                and checks[0].gate is None
            )
            assert "no score or verdict" in checks[0].error
            if shape == "parallel_guard":
                peers = [r for r in rows if r.step_id == steps["peer"].id]
                assert len(peers) == 1 and peers[0].status == RunStatus.SKIPPED
                assert peers[0].error is None and peers[0].gate is None, peers
                from app.services.orchestrator_part4 import _latest_outcome_steprun

                assert _latest_outcome_steprun(s, s.get(WorkflowRun, run_id), "step:peer") is None
                assert all("lqa" not in e for e in events if e.get("step_name") == "peer")
            if shape == "passed_guard":
                assert "prev_lqa:passed" in checks[0].error and "false" in checks[0].error, checks[
                    0
                ].error
            sentinel = [r for r in rows if r.step_id == steps["sentinel"].id]
            if shape in {"end", "parallel_end"}:
                assert not sentinel, "on_pass:end must never visit the downstream step"
            assert not sentinel or all(r.status == RunStatus.SKIPPED for r in sentinel), (
                "sentinel/fixer ran after empty check"
            )
            assert s.get(WorkflowRun, run_id).status == RunStatus.COMPLETED


def legacy_refusal(
    pending,
    origin,
    step,
    correction,
    *,
    tm_hard_idx,
    tm_soft_idx,
    content_protected,
    status_protected,
    refused_seg,
    tm_protected_idx=None,
):
    """Frozen pre-round-3 dispatch filters, including the D1-only telemetry."""
    d1 = lqa_safety.tm_protected_indices(pending, origin, step, correction)
    if tm_protected_idx is not None:
        tm_protected_idx.extend(d1)
    remaining = [j for j in pending if j not in d1]
    locks = set(tm_hard_idx)
    if not correction:
        locks |= tm_soft_idx
    remaining = [j for j in remaining if j not in locks]
    remaining = [j for j in remaining if j not in refused_seg]
    remaining = [j for j in remaining if j not in content_protected]
    remaining = [j for j in remaining if j not in status_protected]
    return set(pending) - set(remaining)


def identity_probe():
    # Every switch/tristate/loop combination, including overlaps and nonpending slots.
    for enabled, flag, correction in product((False, True), (None, False, True), (False, True)):
        args = (
            [0, 1, 2, 3, 4, 5, 6, 7],
            [True, False, False, False, False, False, True, False],
            SimpleNamespace(tm_protect=flag),
            correction,
        )
        kw = {
            "tm_hard_idx": {1, 6, 99},
            "tm_soft_idx": {2},
            "content_protected": {3},
            "status_protected": {4},
            "refused_seg": {5},
        }
        old_d1, new_d1 = [], []
        with patch.object(lqa_safety, "tm_protection_enabled", return_value=enabled):
            old = legacy_refusal(*args, **kw, tm_protected_idx=old_d1)
            new = lqa_safety.step_refused_indices(*args, **kw, tm_protected_idx=new_d1)
        assert old == new and old_d1 == new_d1, (enabled, flag, correction, old, new)
    # Real runs compare repaired bytes, provider/judge inputs, all durable step
    # notes/verdicts and safety payloads with exclusion OFF, against the old filters.
    for shape in ("prior_hard", "content", "approved", "prior_soft", "no_tm_step", "opt_out"):
        current = exercise(shape, identity=True)
        with patch.object(lqa_safety, "step_refused_indices", side_effect=legacy_refusal):
            before = exercise(shape, identity=True)
        assert current == before, (shape, current, before)
    print("  fixer byte identity: all switch/tristate combinations and six real workflows: OK")


failures = []
try:
    identity_probe()
except (AssertionError, AttributeError) as exc:
    failures.append("fixer_identity")
    print(f"  fixer_identity: FAIL: {exc}")
for shape in (
    "prior_hard",
    "content",
    "approved",
    "passed_guard",
    "scope",
    "no_tm_step",
    "fuzzy_none",
    "locks_off",
    "prior_soft",
    "two_checks",
    "opt_out",
    "end",
    "guard",
    "key_guard",
    "parallel_end",
    "parallel_guard",
):
    try:
        exercise(shape)
        print(f"  {shape}: OK")
    except AssertionError as exc:
        failures.append(shape)
        print(f"  {shape}: FAIL: {exc}")
assert not failures, f"regressions: {failures}"
print("verify_lqa_tm_exclusion_shapes: OK")
