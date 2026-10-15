"""`tm_match` workflow STEP verification (offline, hermetic, MockProvider).

PROPOSAL-tm-matching-workflow-step §7 -- the orderable step wired into the segmented
orchestrator loop:
  (1) tm_match -> MT: a 100% TM hit is filled + LOCKED by the step; the following MT
      step sees only the REMAINDER (locked segment not re-translated); provenance kept
      (SegStatus.MT + tm_match_pct=100).
  (2) default policy soft-locks; a downstream re-run's MT write to the locked segment is
      refused at the seam only when HARD (soft still protected by pending-set exclusion).
  (3) with hundreds_enter_lqa off (HARD lock), a SECOND workflow run cannot overwrite the
      locked segment even via a direct MT step -- the seam refuses.
  (4) a poisoned 100% hit is NOT locked (write-back withheld it -> NEEDS_REVIEW).

HERMETIC: throwaway LOC_TMS_DATA_DIR; MockProvider (keyless) echoes "[locale] source";
ASCII-only.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_tmstep_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    GlossaryEntry,
    Project,
    Segment,
    SegmentTM,
    SegStatus,
    StepType,
    StringKey,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    ingest,
    lqa,
    orchestrator,
    providers,
    qe,
    quality,
    refusal,
    settings,
    tm,
    tm_lock,
    translate,
)

init_db()

HEDGE = (
    "I'm ready to translate from English to French. However, I don't see any "
    "source text provided. Please provide the text you would like translated."
)


def _mk_project(s, name, src="en-US"):
    p = Project(name=name, source_locale=src, default_provider="mock")
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _mk_doc(s, project_id, locale, sources):
    doc = Document(
        project_id=project_id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
    )
    s.add(doc)
    s.commit()
    s.refresh(doc)
    for i, src in enumerate(sources):
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=locale,
                order_index=i,
                source_text=src,
                target_text="",
                status=SegStatus.NEW,
            )
        )
    s.add(TargetLocale(project_id=project_id, locale=locale))
    s.commit()
    return doc


def _mk_workflow(s, steps):
    """steps = list of (order_index, step_type, name)."""
    wf = Workflow(name="TMWF")
    s.add(wf)
    s.commit()
    s.refresh(wf)
    for oi, st, nm in steps:
        s.add(WorkflowStep(workflow_id=wf.id, order_index=oi, name=nm, step_type=st))
    s.commit()
    return wf


def _seg(s, doc_id, order):
    return s.exec(
        select(Segment).where(Segment.document_id == doc_id, Segment.order_index == order)
    ).one()


def _poison(s, pid, source, target, src="en-US", tgt="fr-FR"):
    s.add(
        SegmentTM(
            project_id=pid,
            source_locale=src,
            target_locale=tgt,
            source_text=source,
            target_text=target,
            source_hash=tm.hash_text(source, src, tgt),
        )
    )
    s.commit()


# --- (1) tm_match fills+locks; downstream MT sees only the remainder ----------
def test_tm_match_then_mt_soft():
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})  # default -> SOFT
        p = _mk_project(s, "SoftFlow")
        tm.write_back(s, p.id, "Save changes.", "Enregistrer.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Save changes.", "Fresh line."])
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summary["status"] == "completed", summary
        s.expire_all()
        locked = _seg(s, doc.id, 0)
        fresh = _seg(s, doc.id, 1)
        # the matched segment: TM fill, locked SOFT, provenance kept, NOT re-translated
        assert locked.target_text == "Enregistrer.", locked.target_text
        assert locked.tm_match_pct == 100 and locked.status == SegStatus.MT, locked
        assert locked.lock_band == "100" and locked.lock_hard is False, (
            locked.lock_band,
            locked.lock_hard,
        )
        # the fresh segment: translated by the MockProvider MT step (echo carries locale)
        assert fresh.target_text and "Fresh line." in fresh.target_text, fresh.target_text
        assert not fresh.lock_band, "an un-matched segment is never locked"
    print("  tm_match -> MT: 100% hit filled+SOFT-locked, remainder MT'd: OK")


# --- (3) HARD lock: a later run's MT step cannot overwrite the locked segment -
def test_hard_lock_survives_second_run():
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "0"})  # 100s HARD lock
        try:
            p = _mk_project(s, "HardFlow")
            tm.write_back(s, p.id, "Delete all.", "Tout supprimer.", "en-US", "fr-FR")
            doc = _mk_doc(s, p.id, "fr-FR", ["Delete all."])
            wf = _mk_workflow(
                s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")]
            )
            orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
            s.expire_all()
            locked = _seg(s, doc.id, 0)
            assert locked.lock_band == "100" and locked.lock_hard is True, locked
            assert locked.target_text == "Tout supprimer.", locked.target_text
            # a SECOND run with a bare MT step must NOT overwrite the hard lock: the seam
            # refuses, the good TM target is preserved.
            wf2 = _mk_workflow(s, [(0, StepType.LLM_AGENT, "MT only")])
            orchestrator.run_workflow(s, wf2.id, doc.id, "fr-FR")
            s.expire_all()
            still = _seg(s, doc.id, 0)
            assert still.target_text == "Tout supprimer.", (
                "hard lock must survive a later MT run",
                still.target_text,
            )
        finally:
            settings.set_many({"tm_hundreds_enter_lqa": "1"})
    print("  tm_match HARD lock survives a later MT-only run (seam refuses): OK")


# --- (4) a poisoned 100% hit is filled-withheld, never locked ----------------
def test_poisoned_hit_not_locked_in_workflow():
    with Session(engine) as s:
        p = _mk_project(s, "PoisonFlow")
        _poison(s, p.id, "Hedge here.", HEDGE)
        doc = _mk_doc(s, p.id, "fr-FR", ["Hedge here."])
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert not seg.lock_band, "a withheld hedge must never be locked"
        # write-back withheld the hedge -> NEEDS_REVIEW (then the MT step re-translated
        # it, since it was never locked -> excluded is only for clean locks). Either way
        # it must NOT carry the raw hedge as a clean lock.
        assert seg.target_text != HEDGE or seg.status == SegStatus.NEEDS_REVIEW, seg
    print("  tm_match: poisoned 100% hit withheld, never locked: OK")


# --- Q6: a SOFT glossary flag on a TM-exact fill must NOT strip the lock band ------
def test_q6_soft_glossary_flag_keeps_tm_lock():
    """Q6 (core-QA fix train 1). Decided policy: an adherence flag RIDES ALONGSIDE the
    lock, it does not GATE it. Live evidence: TM-exact fills (tm%=100, byte-exact) that
    caught a glossary flag ended lock_band=None while their unflagged siblings held
    band=100 -- deterministically, 40/160 at strings-file scale. The band was never SET:
    the write-back routed the flagged fill to NEEDS_REVIEW and the lock-apply seam gated
    on status==MT, so it skipped. The fix gates on tm_eligibility (the fill is still
    authoritative content), not status, so the band survives the flag."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})  # SOFT lock
        p = _mk_project(s, "Q6Flow")
        # a byte-exact, CORRECT TM fill that genuinely lacks the required term 'Aparato'
        # (uses a synonym) -> a real, soft glossary miss flags the segment.
        tm.write_back(s, p.id, "Buy the Widget.", "Compra el trasto.", "en-US", "fr-FR")
        s.add(
            GlossaryEntry(
                project_id=p.id,
                source_locale="en-US",
                target_locale="fr-FR",
                source_term="Widget",
                target_term="Aparato",
            )
        )
        s.commit()
        doc = _mk_doc(s, p.id, "fr-FR", ["Buy the Widget."])
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summary["status"] == "completed", summary
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # the TM fill survived byte-exact and keeps its TM provenance...
        assert seg.target_text == "Compra el trasto." and seg.tm_match_pct == 100, seg
        # ...it caught the soft glossary flag -> NEEDS_REVIEW + the badge a reviewer sees...
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert [f for f in (seg.qa_flags or []) if f.get("type") == "glossary_violation"], (
            seg.qa_flags
        )
        # ...but the lock band RIDES ALONGSIDE the flag (it was None before the fix).
        assert seg.lock_band == "100", (
            "Q6: a soft adherence flag must not strip the TM lock band",
            seg.lock_band,
        )
    print("  Q6: a soft glossary flag on a TM-exact fill keeps its lock band: OK")


# --- F4: a NON-refusal poisoned TM hit is never locked (the tm_eligible leg) --------
def test_f4_number_poisoned_tm_hit_not_locked():
    """F4 (review mutation ii). A 100% TM hit whose stored target CORRUPTS the source
    number (a NON-refusal poison class) commits as content (committed True) but the seam
    forces tm_eligible False and routes NEEDS_REVIEW. The lock gate's tm_eligible leg must
    exclude it -- dropping that leg (mutation ii) would wrongly lock a wrong-number fill,
    which returns forever as a 100% match bypassing the number gate."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})  # band WOULD be 100 if locked
        p = _mk_project(s, "F4NumPoison")
        # source has 500; the stored TM target corrupts it to 900 -> number_integrity gate.
        tm.write_back(s, p.id, "Pay 500 now.", "Paga 900 ahora.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Pay 500 now."])
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summary["status"] == "completed", summary
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # the fill committed as content (tm%=100) but caught the number gate...
        assert seg.target_text == "Paga 900 ahora." and seg.tm_match_pct == 100, seg
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert [f for f in (seg.qa_flags or []) if f.get("type") == "number_integrity"], (
            seg.qa_flags
        )
        # ...so it is TM-INELIGIBLE and must NEVER get a lock band (mutation ii dies).
        assert not seg.lock_band, ("F4: a number-poisoned TM hit must never lock", seg.lock_band)
    print("  F4: a number-corruption (non-refusal) TM hit is never locked (mutation ii): OK")


# --- F6: an over-limit TM fill must stay UNLOCKED so a reviewer can shorten it ------
def test_f6_char_limit_tm_fill_not_locked():
    """F6. A char_limit violation is SOFT and leaves tm_eligible True, so a bare
    `committed and tm_eligible` gate would wrongly lock an over-limit TM fill. The
    eligibility predicate subtracts char_limit: the over-limit fill stays UNLOCKED so a
    reviewer can shorten it (a hard lock would freeze it over its slot forever)."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, "F6CharLimit")
        tm.write_back(s, p.id, "Save.", "Enregistrer les modifications.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Save."])
        # attach a StringKey with a tight char_limit to the (document) segment.
        k = StringKey(project_id=p.id, name="btn::save", char_limit=5)
        s.add(k)
        s.commit()
        s.refresh(k)
        seg0 = _seg(s, doc.id, 0)
        seg0.key_id = k.id
        s.add(seg0)
        s.commit()
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summary["status"] == "completed", summary
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # the TM fill landed (tm%=100) but is over its 5-char slot -> char_limit flag...
        assert seg.target_text == "Enregistrer les modifications.", seg.target_text
        assert seg.tm_match_pct == 100 and seg.status == SegStatus.NEEDS_REVIEW, seg
        assert [f for f in (seg.qa_flags or []) if f.get("type") == "char_limit"], seg.qa_flags
        # ...and it must stay UNLOCKED so it can be shortened.
        assert not seg.lock_band, ("F6: an over-limit TM fill must stay UNLOCKED", seg.lock_band)
    print("  F6: an over-limit (char_limit) TM fill stays unlocked so it can be shortened: OK")


def _tm_lqa_fix_workflow(s, *, fix_qe_gate=False):
    """TM match -> LQA -> Fix (correction loop, gated on prev_lqa:failed). The LQA fails
    (a REQUIRE-a-missing-term rulebook), so the fix step RUNS and, because a correction-
    loop step MAY edit a soft lock, it REWRITES the TM segment -> tm_origin flips False."""
    wf = Workflow(name="TMFixWF")
    s.add(wf)
    s.commit()
    s.refresh(wf)
    s.add(
        WorkflowStep(workflow_id=wf.id, order_index=0, step_type=StepType.TM_MATCH, name="TM match")
    )
    s.add(
        WorkflowStep(
            workflow_id=wf.id, order_index=1, step_type=StepType.LQA, name="LQA", provider="mock"
        )
    )
    s.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=2,
            step_type=StepType.LLM_AGENT,
            name="Fix",
            provider="mock",
            prompt_text="Fix: {text}",
            qe_gate=fix_qe_gate,
            qe_threshold=70,
            run_condition={"ref": "prev_lqa", "op": "failed", "value": None},
        )
    )
    s.commit()
    return wf


# --- F10: a correction-loop REWRITE of a TM fill (tm_origin False) must NOT lock -----
def test_f10_correction_rewrite_never_locked():
    """F10. The OLD `status == MT` gate stamped a band + tm_id PROVENANCE onto text a
    correction-loop step had REWRITTEN (tm_origin flipped False but the rewrite landed at
    status MT). The new eligibility predicate keys on tm_origin, so an LLM-authored rewrite
    of a former TM fill never gets a band -- the provenance claim stays honest."""
    lqa.save_rulebook("en-US", "fr-FR", "REQUIRE Kubernetes")  # forces the LQA to fail
    try:
        with Session(engine) as s:
            settings.set_many(
                {
                    "tm_hundreds_enter_lqa": "1",  # SOFT (fix may edit)
                    "tm_fix_protection_enabled": "0",
                }
            )  # let the fix re-run it
            try:
                p = _mk_project(s, "F10Rewrite")
                tm.write_back(s, p.id, "Open the door.", "Ouvre la porte.", "en-US", "fr-FR")
                doc = _mk_doc(s, p.id, "fr-FR", ["Open the door."])
                wf = _tm_lqa_fix_workflow(s)
                summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
                assert summary["status"] == "completed", summary
                s.expire_all()
                seg = _seg(s, doc.id, 0)
                # the fix step REWROTE the TM fill (mock echoes "[fr-FR] <working>")...
                assert seg.target_text != "Ouvre la porte.", (
                    "the fix step should have rewritten the TM fill",
                    seg.target_text,
                )
                # ...so the band + tm_id provenance must NOT be stamped over LLM text.
                assert not seg.lock_band, (
                    "F10: a rewritten TM fill must never be locked",
                    seg.lock_band,
                )
            finally:
                settings.set_many({"tm_fix_protection_enabled": "1"})
    finally:
        lqa.save_rulebook("en-US", "fr-FR", "")
    print("  F10: a correction-loop rewrite of a TM fill (tm_origin False) is never locked: OK")


# --- F6: a QE-FAILED TM fill (re-translated by a QE-gated fix step) must NOT lock ----
def test_f6_qe_failed_tm_fill_not_locked():
    """F6. A QE-gated correction-loop fix step re-runs the soft-locked TM segment (a
    correction-loop step may edit a soft lock), so it is re-translated (tm_origin False)
    AND QE-scored; a FAIL routes it to NEEDS_REVIEW. It must stay UNLOCKED -- a QE-failed
    fill keeps its old unlocked behaviour under the eligibility predicate."""
    lqa.save_rulebook("en-US", "fr-FR", "REQUIRE Kubernetes")  # forces the LQA to fail
    try:
        with Session(engine) as s:
            settings.set_many({"tm_hundreds_enter_lqa": "1", "tm_fix_protection_enabled": "0"})
            try:
                p = _mk_project(s, "F6QEFail")
                # the TM target carries the whole word BAD, which the offline MockProvider
                # QE judge scores at 40 (< threshold 70) when the fix step echoes it.
                tm.write_back(s, p.id, "Close the tab.", "BAD onglet ici.", "en-US", "fr-FR")
                doc = _mk_doc(s, p.id, "fr-FR", ["Close the tab."])
                wf = _tm_lqa_fix_workflow(s, fix_qe_gate=True)
                summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
                assert summary["status"] == "completed", summary
                s.expire_all()
                seg = _seg(s, doc.id, 0)
                # the fix step re-translated it and it FAILED QE...
                qf = [f for f in (seg.qa_flags or []) if f.get("type") == "qe"]
                assert qf and qf[0].get("passed") is False, ("QE must have failed", qf)
                assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
                # ...so it must stay UNLOCKED.
                assert not seg.lock_band, (
                    "F6: a QE-failed TM fill must stay unlocked",
                    seg.lock_band,
                )
            finally:
                settings.set_many({"tm_fix_protection_enabled": "1"})
    finally:
        lqa.save_rulebook("en-US", "fr-FR", "")
    print("  F6: a QE-failed re-translated TM fill stays unlocked: OK")


# --- R1(a): a step-errored TM fill (seg_step_error leg) must NOT lock -----------------
def test_r1a_step_errored_tm_fill_not_locked():
    """R1(a). Pins the `j not in seg_step_error` leg of the lock-eligibility predicate. A
    soft-locked TM fill is re-run by a correction-loop fix step (protection off) whose
    provider call ERRORS for it -- GitHub #45 keeps its PRE-STEP text (the TM fill,
    tm_origin still True, so it commits as authoritative TM content) and routes it to review
    with a step_error flag. The predicate's `j not in seg_step_error` leg must exclude it:
    dropping that leg would lock a fill whose own step could not improve it, which then
    returns forever as a trusted 100% match. Mutation dies."""
    lqa.save_rulebook("en-US", "fr-FR", "REQUIRE Kubernetes")  # forces the LQA to fail
    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        # the fix step's provider call errors (request-shaped, so a single-segment
        # degradation COMPLETES the run -- it is not a structural failure that escalates).
        if "Fix:" in prompt:
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: Context size has been exceeded.",
            }
        return real(spec, prompt, **kw)

    try:
        with Session(engine) as s:
            settings.set_many(
                {
                    "tm_hundreds_enter_lqa": "1",  # SOFT (fix may edit)
                    "tm_fix_protection_enabled": "0",
                }
            )  # let the fix reach it
            try:
                set_pieces("app.services.orchestrator", "run_completion", _stub)
                p = _mk_project(s, "R1AStepErr")
                tm.write_back(s, p.id, "Open the door.", "Ouvre la porte.", "en-US", "fr-FR")
                doc = _mk_doc(s, p.id, "fr-FR", ["Open the door."])
                wf = _tm_lqa_fix_workflow(s)
                summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
                assert summary["status"] == "completed", summary
                s.expire_all()
                seg = _seg(s, doc.id, 0)
                # the fix call errored -> the TM fill is KEPT (tm_origin stayed True) and a
                # step_error flag routes it to review...
                assert seg.target_text == "Ouvre la porte.", seg.target_text
                assert seg.tm_match_pct == 100 and seg.status == SegStatus.NEEDS_REVIEW, seg
                assert [
                    f for f in (seg.qa_flags or []) if f.get("type") == quality.STEP_ERROR_FLAG_TYPE
                ], seg.qa_flags
                # ...so the seg_step_error leg keeps it UNLOCKED.
                assert not seg.lock_band, (
                    "R1(a): a step-errored TM fill must never lock",
                    seg.lock_band,
                )
            finally:
                set_pieces("app.services.orchestrator", "run_completion", real)
                settings.set_many({"tm_fix_protection_enabled": "1"})
    finally:
        lqa.save_rulebook("en-US", "fr-FR", "")
        set_pieces("app.services.orchestrator", "run_completion", real)
    print("  R1(a): a step-errored TM fill (seg_step_error) is never locked: OK")


# --- R1(b): a fix-reverted TM fill (fix_guard_seg leg) must NOT lock -------------------
def test_r1b_fix_reverted_tm_fill_not_locked():
    """R1(b). Pins the `j not in fix_guard_seg` leg. A soft-locked TM fill that CORRECTLY
    carries the required term 'Aparato' is re-run by a correction-loop fix step whose edit
    DROPS the term -- the glossary fix-guard REVERTS the edit (keeps the pre-fix TM text,
    tm_origin still True) and records a fix_reverted flag. The committed text is the clean TM
    fill (no glossary violation at the write seam), so ONLY the `j not in fix_guard_seg` leg
    keeps it unlocked: dropping that leg would lock a fill whose sole offered improvement was
    discarded as a term-breaker. Mutation dies."""
    lqa.save_rulebook("en-US", "fr-FR", "REQUIRE Kubernetes")
    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        # the fix DROPS 'Aparato' -> introduces a glossary violation the fix-guard reverts.
        if "Fix:" in prompt:
            return {
                "text": "Compra el trasto.",
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        return real(spec, prompt, **kw)

    try:
        with Session(engine) as s:
            settings.set_many(
                {
                    "tm_hundreds_enter_lqa": "1",
                    "tm_fix_protection_enabled": "0",
                    "glossary_fix_guard_enabled": "1",
                }
            )
            try:
                set_pieces("app.services.orchestrator", "run_completion", _stub)
                p = _mk_project(s, "R1BFixGuard")
                # the TM fill is CORRECT: it contains the required term 'Aparato'.
                tm.write_back(s, p.id, "Buy the Widget.", "Compra el Aparato.", "en-US", "fr-FR")
                s.add(
                    GlossaryEntry(
                        project_id=p.id,
                        source_locale="en-US",
                        target_locale="fr-FR",
                        source_term="Widget",
                        target_term="Aparato",
                    )
                )
                s.commit()
                doc = _mk_doc(s, p.id, "fr-FR", ["Buy the Widget."])
                wf = _tm_lqa_fix_workflow(s)
                summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
                assert summary["status"] == "completed", summary
                s.expire_all()
                seg = _seg(s, doc.id, 0)
                # the fix edit was REVERTED -> the correct TM fill stands (tm_origin True)...
                assert seg.target_text == "Compra el Aparato." and seg.tm_match_pct == 100, seg
                assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
                assert [f for f in (seg.qa_flags or []) if f.get("type") == "fix_reverted"], (
                    seg.qa_flags
                )
                # the committed TM text is clean (no glossary flag at the write seam), so
                # ONLY the fix_guard_seg leg keeps it UNLOCKED.
                assert not [
                    f for f in (seg.qa_flags or []) if f.get("type") == "glossary_violation"
                ], seg.qa_flags
                assert not seg.lock_band, (
                    "R1(b): a fix-reverted TM fill must never lock",
                    seg.lock_band,
                )
            finally:
                set_pieces("app.services.orchestrator", "run_completion", real)
                settings.set_many({"tm_fix_protection_enabled": "1"})
    finally:
        lqa.save_rulebook("en-US", "fr-FR", "")
        set_pieces("app.services.orchestrator", "run_completion", real)
    print("  R1(b): a fix-reverted TM fill (fix_guard_seg) is never locked: OK")


# --- R3: a flagged fill NEVER hard-locks (clean=hard; flagged=soft+review) -------------
def test_r3_flagged_fill_never_hard_locks():
    """R3 (policy, this fix round). Under a HARD lock policy (tm_hundreds_enter_lqa off):
    * a CLEAN 100% fill HARD-locks (lock_hard True) -- the trusted-content case;
    * a fill carrying a glossary violation gets band '100' + lock_hard False +
      NEEDS_REVIEW, NOT a hard lock. A hard lock is for clean trusted fills; a hard lock
      on a flagged fill would freeze it as unfixable by any machine path, so the flagged
      fill keeps SOFT protection (review routing retained, machine repair still possible
      after review -- a correction-loop fix may edit a soft lock)."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "0"})  # HARD lock policy
        try:
            # (a) a CLEAN fill under the hard policy -> a real HARD lock.
            pc = _mk_project(s, "R3Clean")
            tm.write_back(s, pc.id, "Save now.", "Enregistrer.", "en-US", "fr-FR")
            docc = _mk_doc(s, pc.id, "fr-FR", ["Save now."])
            wfc = _mk_workflow(
                s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")]
            )
            orchestrator.run_workflow(s, wfc.id, docc.id, "fr-FR")
            s.expire_all()
            clean = _seg(s, docc.id, 0)
            assert clean.lock_band == "100" and clean.lock_hard is True, (
                "R3: a clean fill under hard policy HARD-locks",
                clean.lock_band,
                clean.lock_hard,
            )
            # (b) a FLAGGED fill (missing required 'Aparato') under the SAME hard policy.
            pf = _mk_project(s, "R3Flagged")
            tm.write_back(s, pf.id, "Buy the Widget.", "Compra el trasto.", "en-US", "fr-FR")
            s.add(
                GlossaryEntry(
                    project_id=pf.id,
                    source_locale="en-US",
                    target_locale="fr-FR",
                    source_term="Widget",
                    target_term="Aparato",
                )
            )
            s.commit()
            docf = _mk_doc(s, pf.id, "fr-FR", ["Buy the Widget."])
            wff = _mk_workflow(
                s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")]
            )
            orchestrator.run_workflow(s, wff.id, docf.id, "fr-FR")
            s.expire_all()
            flagged = _seg(s, docf.id, 0)
            assert flagged.target_text == "Compra el trasto." and flagged.tm_match_pct == 100, (
                flagged
            )
            assert [f for f in (flagged.qa_flags or []) if f.get("type") == "glossary_violation"], (
                flagged.qa_flags
            )
            assert flagged.status == SegStatus.NEEDS_REVIEW, flagged.status
            # THE POLICY: band retained, but SOFT, never HARD.
            assert flagged.lock_band == "100" and flagged.lock_hard is False, (
                "R3: a flagged fill under hard policy is band 100 + SOFT, not a hard lock",
                flagged.lock_band,
                flagged.lock_hard,
            )
        finally:
            settings.set_many({"tm_hundreds_enter_lqa": "1"})
    print("  R3: a flagged fill never hard-locks (clean=hard, flagged=soft+review): OK")


# --- (5) step creation parity: the SHARED add_step seam (web + CLI both call it) ---
def test_add_step_parity_tm_match():
    from app.services import workflows as wf_svc
    from app.services.workflows import WorkflowError

    with Session(engine) as s:
        _mk_project(s, "AddStepParity")
        wf = Workflow(name="AddWF")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        # a tm_match step needs no provider/prompt and is created cleanly
        step = wf_svc.add_step(s, wf.id, name="Match TM", step_type=StepType.TM_MATCH)
        assert step.step_type == StepType.TM_MATCH and not step.provider, step
        # Batch 15 T1 (SPEC-tm-match-step-config §8.A): a NEW tm_match step SEEDS its five
        # scalar config columns from the current globals (a concrete snapshot, D15-4) --
        # NOT left NULL. With the shipped defaults that is threshold 100 / fuzzy "soft" /
        # lock_ice on / hundreds_enter_lqa on / context_floor 95; the pool selection is
        # NEVER snapshotted (S2 -> NULL = all readable).
        assert step.tm_fill_threshold == 100, step.tm_fill_threshold
        assert step.tm_fuzzy_lock_mode == "soft", step.tm_fuzzy_lock_mode
        assert step.tm_lock_ice is True, step.tm_lock_ice
        assert step.tm_hundreds_enter_lqa is True, step.tm_hundreds_enter_lqa
        assert step.tm_context_floor == 95, step.tm_context_floor
        assert step.tm_pool_selection is None, step.tm_pool_selection
        # Batch 15 T1 (the DE-ALIAS): the six tm_match config fields are now LIVE on a
        # tm_match step -- a value set on them is ACCEPTED, not refused (the mirror image
        # of how they are refused on every OTHER type below).
        live = wf_svc.add_step(
            s,
            wf.id,
            name="Configured",
            step_type=StepType.TM_MATCH,
            tm_fill_threshold=85,
            tm_fuzzy_lock_mode="none",
        )
        assert live.tm_fill_threshold == 85 and live.tm_fuzzy_lock_mode == "none", live
        # a dead field actively set (provider) is REFUSED, not silently dropped
        try:
            wf_svc.add_step(s, wf.id, name="Bad", step_type=StepType.TM_MATCH, provider="claude")
            raise AssertionError("a provider on a tm_match step must be refused")
        except WorkflowError as e:
            assert "tm_match" in str(e), str(e)
        # ...and the six tm_match config fields are now DEAD on a NON-tm_match step: a
        # tm_fill_threshold set on an llm_agent step is refused (LAW-TM-10).
        try:
            wf_svc.add_step(
                s, wf.id, name="BadLLM", step_type=StepType.LLM_AGENT, tm_fill_threshold=85
            )
            raise AssertionError("tm_fill_threshold on an llm_agent step must be refused")
        except WorkflowError as e:
            assert "TM fill threshold" in str(e), str(e)
    print("  add_step parity: tm_match creatable + seeded + config live; dead fields refused: OK")


# --- (6) policy resolves from global settings (the §6 mapping) ---------------
def test_policy_resolves_from_settings():
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "0", "tm_context_floor": "90"})
        try:
            pol = tm_lock.resolve_policy(s)
            assert pol.hundreds_enter_lqa is False and pol.context_floor == 90, pol
        finally:
            settings.set_many({"tm_hundreds_enter_lqa": "1", "tm_context_floor": "95"})
        pol2 = tm_lock.resolve_policy(s)
        assert pol2.hundreds_enter_lqa is True and pol2.context_floor == 95, pol2
    print("  policy: resolves lock_ice/hundreds_enter_lqa/context_floor from settings: OK")


# --- B4: tm_match never fills/locks over an existing human translation --------
def test_b4_never_clobbers_human_translation():
    with Session(engine) as s:
        p = _mk_project(s, "B4Human")
        tm.write_back(s, p.id, "Keep me.", "TM version.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Keep me."])
        # a human already approved a DIFFERENT translation of this exact source
        seg = _seg(s, doc.id, 0)
        seg.target_text = "Human approved."
        seg.status = SegStatus.APPROVED
        s.add(seg)
        s.commit()
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match")])
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # tm_match's guarantee (B4): it neither LOCKS a segment that already has a stored
        # translation nor TM-FILLS it. (The plain-workflow "re-translate from source"
        # clobber of an un-stepped segment is PRE-EXISTING on main and out of scope -- so
        # we assert tm_match's own guarantee, not target survival.)
        assert not seg.lock_band, (
            "tm_match must not lock over an existing translation",
            seg.lock_band,
        )
        assert seg.tm_match_pct != 100, "tm_match must not TM-fill an already-translated seg"
        assert seg.target_text != "TM version.", "the TM version was not applied"
    print("  B4: tm_match never fills/locks over an existing (stored) translation: OK")


# --- B3: a human_review pause + resume preserves the fill AND applies the lock -
def test_b3_lock_survives_pause_resume():
    with Session(engine) as s:
        p = _mk_project(s, "B3Pause")
        tm.write_back(s, p.id, "Locked line.", "Ligne verrouillee.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Locked line.", "Free line."])
        # tm_match -> human_review (pause) -> MT. The pause returns mid-run.
        wf = _mk_workflow(
            s,
            [
                (0, StepType.TM_MATCH, "TM match"),
                (1, StepType.HUMAN_REVIEW, "Review"),
                (2, StepType.LLM_AGENT, "MT"),
            ],
        )
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "paused", summ
        run_id = summ["run_id"]
        # resume with the paused text unchanged
        orchestrator.resume_workflow(s, run_id, "fr-FR", summ["paused_text"])
        s.expire_all()
        locked = _seg(s, doc.id, 0)
        assert locked.target_text == "Ligne verrouillee.", (
            "the TM fill must survive the pause, not be re-translated",
            locked.target_text,
        )
        assert locked.lock_band == "100", (
            "the lock must be applied after resume",
            locked.lock_band,
        )
        # MED-5: tm_origin survives the pause -> the write-back keeps TM provenance
        # (tm_match_pct=100), not MT (mt_confidence=0.9). A resumed locked row must never
        # claim BOTH a TM lock and MT provenance.
        assert locked.tm_match_pct == 100, (
            "TM provenance must survive resume",
            locked.tm_match_pct,
        )
        assert locked.mt_confidence is None, (
            "a TM-locked row is not fresh MT",
            locked.mt_confidence,
        )
    print("  B3/MED-5: fill + lock + TM provenance survive a pause/resume: OK")


# --- item 7b: a persisted HARD lock skips the provider call in a LATER run -----
def test_persisted_hard_lock_excluded_next_run():
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "0"})  # hard
        try:
            p = _mk_project(s, "Persist7b")
            tm.write_back(s, p.id, "Once.", "Une fois.", "en-US", "fr-FR")
            doc = _mk_doc(s, p.id, "fr-FR", ["Once."])
            wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM"), (1, StepType.LLM_AGENT, "MT")])
            orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
            s.expire_all()
            assert _seg(s, doc.id, 0).lock_hard is True
            # SECOND run: an MT-only workflow. The persisted hard lock is seeded into the
            # exclusion, so the segment is never sent to the provider AND keeps its target.
            wf2 = _mk_workflow(s, [(0, StepType.LLM_AGENT, "MT only")])
            orchestrator.run_workflow(s, wf2.id, doc.id, "fr-FR")
            s.expire_all()
            seg = _seg(s, doc.id, 0)
            assert seg.target_text == "Une fois." and seg.lock_hard is True, seg
        finally:
            settings.set_many({"tm_hundreds_enter_lqa": "1"})
    print("  item 7b: a persisted hard lock is excluded (skips the call) in a later run: OK")


# --- item 8: a SOFT lock enters an LQA-flagged round-2 fix; the fix clears it --
def test_item8_soft_lock_editable_in_round2_fix():
    from app.models import WorkflowStep

    with Session(engine) as s:
        p = _mk_project(s, "Item8Soft")
        tm.write_back(s, p.id, "Soft line.", "Version TM.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Soft line."])
        # run tm_match once to soft-lock the segment
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM")])
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.lock_band == "100" and seg.lock_hard is False, seg
        # A correction-loop FIX step (gated prev_lqa:failed) is NOT excluded for a soft
        # lock (D3) -- verify the exclusion set logic: a fix step may reach it.
        from app.services import orchestrator as orch

        wf2 = Workflow(name="fixwf")
        s.add(wf2)
        s.commit()
        s.refresh(wf2)
        lqa = WorkflowStep(workflow_id=wf2.id, order_index=0, name="LQA", step_type=StepType.LQA)
        fix = WorkflowStep(
            workflow_id=wf2.id,
            order_index=1,
            name="Fix",
            step_type=StepType.LLM_AGENT,
            run_condition={"ref": "prev_lqa", "op": "failed"},
        )
        s.add(lqa)
        s.add(fix)
        s.commit()
        steps = [lqa, fix]
        assert orch._is_correction_loop_step(fix, steps) is True, "fix is a correction-loop step"
    print("  item 8: a soft lock is reachable by a correction-loop fix step (D3): OK")


# --- Q9: a PRIOR TM-exact fill (pct=100, no lock) is RE-LOCKED by a later tm_match ------
_Q9_TM = {
    "Save all your files now.": "Guarde todos sus archivos ahora.",
    "Delete the old backup.": "Elimine la copia de seguridad antigua.",
    "Restart the sync service.": "Reinicie el servicio de sincronizacion.",
}
_Q9_PO = (
    b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
    b'#: q9.src:1\nmsgid "Save all your files now."\nmsgstr ""\n\n'
    b'#: q9.src:2\nmsgid "Delete the old backup."\nmsgstr ""\n\n'
    b'#: q9.src:3\nmsgid "Restart the sync service."\nmsgstr ""\n'
)
_Q9_MD = ("\n\n".join(_Q9_TM.keys()) + "\n").encode("utf-8")


def _q9_project(s, name):
    p = Project(name=name, source_locale="en-US", default_provider="mock")
    s.add(p)
    s.commit()
    s.refresh(p)
    s.add(TargetLocale(project_id=p.id, locale="es-ES"))
    s.commit()
    for src, tgt in _Q9_TM.items():
        tm.write_back(s, p.id, src, tgt, "en-US", "es-ES")
    s.commit()
    return p


def _q9_run_for_format(s, label, filename, data):
    """Ingest -> PLAIN pretranslate (fills pct=100 with the TM target but NO lock, the
    Q9 seed state) -> a tm_match->MT workflow. Returns the post-workflow segments."""
    p = _q9_project(s, label)
    doc = ingest.ingest_document(s, p, filename, data, TransMode.SEGMENTED)
    # The plain-translate path (the /documents/{id}/translate seam): its exact-TM short-
    # circuit stamps tm_match_pct=100 but never a lock band -- the exact prod pre-state.
    for _ev in translate.pretranslate_stream(s, doc.id, "es-ES"):
        pass
    s.expire_all()
    pre = s.exec(
        select(Segment).where(Segment.document_id == doc.id).order_by(Segment.order_index)
    ).all()
    core_pre = [seg for seg in pre if seg.source_text in _Q9_TM]
    assert core_pre and all(seg.tm_match_pct == 100 for seg in core_pre), (
        "seed pre-state: plain translate must record pct=100",
        core_pre,
    )
    assert all(not seg.lock_band for seg in core_pre), (
        "seed pre-state: the plain-translate fill must NOT be locked (the Q9 seed)",
        [(seg.order_index, seg.lock_band) for seg in core_pre],
    )
    wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
    summary = orchestrator.run_workflow(s, wf.id, doc.id, "es-ES")
    assert summary["status"] == "completed", summary
    s.expire_all()
    return doc, [
        seg
        for seg in s.exec(
            select(Segment).where(Segment.document_id == doc.id).order_by(Segment.order_index)
        ).all()
        if seg.source_text in _Q9_TM
    ]


def test_q9_po_prior_fill_is_relocked_and_protected():
    """Q9 (core-QA fix train 2). LIVE PROD: a PO doc's core segments ended
    tm_match_pct=100 but lock_band=None -- INCLUDING unflagged, clean rows -- then the
    post-edit lane (whose TM protection keys off the missing lock) rewrote 6/16 sentinels.
    Root cause (reproduced): a prior TM-exact fill (the plain-translate exact-TM short-
    circuit) stamps pct=100 with NO lock; tm_match then (correctly, B4) skips the
    now-non-empty target, so it never locks and sits un-protected. The fix RE-LOCKS a
    stored target that already byte-equals its 100% TM hit. Asserted on a PO doc-through-
    ingest, with an md control proving format parity, then a downstream rewrite-attempting
    step proving the protection holds."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})  # SOFT lock
        doc_po, core_po = _q9_run_for_format(s, "Q9PO", "q9.po", _Q9_PO)
        _doc_md, core_md = _q9_run_for_format(s, "Q9MD", "q9.md", _Q9_MD)
        # PO: every core segment is now RE-LOCKED, byte-exact from TM, provenance kept.
        for seg in core_po:
            assert seg.target_text == _Q9_TM[seg.source_text], (
                "the TM sentinel must survive byte-exact",
                seg.source_text,
                seg.target_text,
            )
            assert seg.lock_band == "100", (
                "Q9: a prior TM-exact fill must be RE-LOCKED by tm_match",
                seg.source_text,
                seg.lock_band,
            )
            assert seg.tm_match_pct == 100, seg
        # md CONTROL: identical outcome -- the fix is format-agnostic (parity).
        assert [seg.lock_band for seg in core_md] == ["100"] * len(core_md), (
            "md equivalence control: same re-lock outcome as PO",
            [(seg.order_index, seg.lock_band) for seg in core_md],
        )
        # PROTECTION HOLDS: a later MT-only run (a normal translate step) must NOT rewrite
        # the soft-locked TM content -- the exact downstream-rewrite the prod breach showed.
        wf2 = _mk_workflow(s, [(0, StepType.LLM_AGENT, "MT only")])
        orchestrator.run_workflow(s, wf2.id, doc_po.id, "es-ES")
        s.expire_all()
        after = [
            seg
            for seg in s.exec(
                select(Segment)
                .where(Segment.document_id == doc_po.id)
                .order_by(Segment.order_index)
            ).all()
            if seg.source_text in _Q9_TM
        ]
        for seg in after:
            assert seg.target_text == _Q9_TM[seg.source_text], (
                "Q9: the re-locked TM content must be protected from a later MT step",
                seg.source_text,
                seg.target_text,
            )
    print("  Q9: a prior TM-exact fill (PO+md) is re-locked byte-exact + protected: OK")


# --- Q9 fix round F1/F2/F3: the re-lock WHITELIST, one excluding variable per test ------
# The re-lock scope is a WHITELIST: ONLY a segment still carrying the exact machine-TM
# fingerprint -- tm_match_pct == 100 AND status == MT (what the plain-translate exact-TM
# short-circuit leaves) -- is repaired. The prior (confounded) tests each set TWO excluding
# variables at once, so a reviewer could delete EITHER fingerprint leg and still see them
# pass. These split them so each isolates ONE variable and each kills a distinct mutation.
_Q9_SRC = "Keep this line."
_Q9_EXACT = "Garde cette ligne."


def _q9_relock_probe(s, name, *, stored, status, pct, qa_flags=None, tm_target=_Q9_EXACT):
    """Set up a ONE-segment Q9 re-lock scenario and run tm_match -> human_review so the run
    PAUSES BEFORE the write-back. The pause is what makes these single-variable tests clean:
    the segment's DB row is left EXACTLY as seeded (the write-back never runs), so a negative
    case can assert status/qa_flags/lock are UNCHANGED, while the tm_match STEP EVENT still
    reveals the re-lock DECISION via its `relocked` count (1 = re-locked, 0 = skipped by the
    whitelist). This sidesteps the pre-existing downstream re-processing of an unclaimed
    segment (a tm_match-only source-overwrite, or an MT step re-translation) that would
    otherwise confound a status/badge assertion. Returns (relocked_count, paused_segment)."""
    settings.set_many({"tm_hundreds_enter_lqa": "1"})  # SOFT band
    p = _mk_project(s, name)
    tm.write_back(s, p.id, _Q9_SRC, tm_target, "en-US", "fr-FR")
    doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
    seg = _seg(s, doc.id, 0)
    seg.target_text = stored
    seg.status = status
    seg.tm_match_pct = pct
    if qa_flags is not None:
        seg.qa_flags = qa_flags
    s.add(seg)
    s.commit()
    wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.HUMAN_REVIEW, "Review")])
    summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
    assert summ["status"] == "paused", summ
    tm_ev = summ["steps"][0]["tm_match"]
    relocked = tm_ev["relocked"]
    s.expire_all()
    return relocked, _seg(s, doc.id, 0), tm_ev


def test_q9_relock_positive_baseline():
    """The POSITIVE control the negatives contrast against: the exact machine fingerprint
    (pct == 100 AND status == MT AND stored byte-equals the hit) IS admitted -> relocked."""
    with Session(engine) as s:
        relocked, _, _ = _q9_relock_probe(
            s, "Q9Base", stored=_Q9_EXACT, status=SegStatus.MT, pct=100
        )
        assert relocked == 1, (
            "the Q9 machine fingerprint (MT + pct100 + byte-equal) must be re-locked",
            relocked,
        )
    print("  Q9 baseline: MT + pct100 + byte-equal IS re-locked (relocked=1): OK")


def test_q9_relock_skips_pct_not_100():
    """SINGLE VARIABLE = pct. status == MT (admitted), byte-equal (admitted), but pct is None
    -> NOT re-locked. Kills a mutation that drops the `tm_match_pct != 100` fingerprint leg."""
    with Session(engine) as s:
        relocked, seg, _ = _q9_relock_probe(
            s, "Q9PctNone", stored=_Q9_EXACT, status=SegStatus.MT, pct=None
        )
        assert relocked == 0, ("pct != 100 must be excluded from re-lock (pct leg)", relocked)
        assert not seg.lock_band, seg.lock_band
    print("  Q9: pct != 100 is not re-locked (single var: pct leg): OK")


def test_q9_relock_skips_status_approved():
    """SINGLE VARIABLE = status. pct == 100 (admitted), byte-equal (admitted), but status is
    APPROVED -> NOT re-locked (human sign-off must never be relabelled machine + locked).
    Kills a mutation that drops the `status == MT` fingerprint leg."""
    with Session(engine) as s:
        relocked, seg, _ = _q9_relock_probe(
            s, "Q9Appr", stored=_Q9_EXACT, status=SegStatus.APPROVED, pct=100
        )
        assert relocked == 0, ("status APPROVED must be excluded (status leg)", relocked)
        assert not seg.lock_band, seg.lock_band
        assert seg.status == SegStatus.APPROVED, ("the human sign-off is untouched", seg.status)
    print("  Q9: status APPROVED is not re-locked (single var: status leg): OK")


def test_q9_relock_skips_status_edited():
    """SINGLE VARIABLE = status. pct == 100 (admitted), byte-equal (admitted), but status is
    EDITED -> NOT re-locked. A CAT-editor 'confirm unchanged' leaves EDITED + pct100 +
    byte-equal; re-locking would relabel human work as machine. Kills the status-leg
    mutation from the EDITED direction."""
    with Session(engine) as s:
        relocked, seg, _ = _q9_relock_probe(
            s, "Q9Edit", stored=_Q9_EXACT, status=SegStatus.EDITED, pct=100
        )
        assert relocked == 0, ("status EDITED must be excluded (status leg)", relocked)
        assert not seg.lock_band, seg.lock_band
        assert seg.status == SegStatus.EDITED, ("human EDITED work is untouched", seg.status)
    print("  Q9: status EDITED is not re-locked (single var: status leg): OK")


def test_q9_relock_skips_needs_review_preserves_badge():
    """SINGLE VARIABLE = status. pct == 100 (admitted), byte-equal (admitted), but status is
    NEEDS_REVIEW with a QE-fail badge -> NOT re-locked, AND the badge + review routing are
    PRESERVED. This is the harm the whitelist exists for: WITHOUT the `status == MT` leg the
    re-lock would promote this row to mt + locked (connector-pushable) and its qa_flags would
    be erased by the stale-clear. The pause leaves the DB row untouched, so we assert the
    badge is intact and the status is still NEEDS_REVIEW."""
    with Session(engine) as s:
        badge = qe.merge_qe_flag(None, qe.qe_flag(40, "bad", 85, False, False))
        relocked, seg, _ = _q9_relock_probe(
            s, "Q9NR", stored=_Q9_EXACT, status=SegStatus.NEEDS_REVIEW, pct=100, qa_flags=badge
        )
        assert relocked == 0, ("status NEEDS_REVIEW must be excluded (status leg)", relocked)
        assert not seg.lock_band, (
            "a review row must never be promoted to mt + locked",
            seg.lock_band,
        )
        assert seg.status == SegStatus.NEEDS_REVIEW, ("review routing preserved", seg.status)
        assert any(isinstance(f, dict) and f.get("type") == "qe" for f in (seg.qa_flags or [])), (
            "the QE badge must be PRESERVED, not erased by a re-lock stale-clear",
            seg.qa_flags,
        )
    print("  Q9: NEEDS_REVIEW is not re-locked + QE badge + routing PRESERVED: OK")


def test_q9_relock_skips_different_translation():
    """SINGLE VARIABLE = the stored TEXT. status == MT, pct == 100, but the stored target
    DIFFERS from the TM's exact string -> NOT RE-LOCKED (the fill_or_relock byte-equality gate,
    B4, is unchanged: a re-lock only ever restores a band on byte-EQUAL text). PART 2 (option A,
    2026-08-13): a MACHINE-status drifted row is now HEALED instead of abandoned -- re-filled
    from TM (the permanent-strand fix), surfaced under the DISTINCT `tm_healed` counter (NOT
    `relocked`). So the re-lock decision is still 0 (kills a mutation relaxing the byte gate),
    and the heal fires (kills a mutation dropping Part 2)."""
    with Session(engine) as s:
        relocked, seg, tm_ev = _q9_relock_probe(  # noqa: RUF059
            s, "Q9Diff", stored="A different translation.", status=SegStatus.MT, pct=100
        )
        assert relocked == 0, (
            "a different existing translation must never RE-LOCK (byte-equality gate unchanged)",
            relocked,
        )
        # PART 2: the drifted MACHINE row is HEALED (a distinct mechanism from re-lock), so the
        # step did genuine work -> no no-op, and the heal is surfaced under `tm_healed`.
        assert tm_ev.get("tm_healed") == 1, (
            "a drifted MACHINE sentinel must be HEALED (re-filled from TM)",
            tm_ev,
        )
        assert not tm_ev.get("noop"), ("the heal is genuine work -> no no-op diagnostic", tm_ev)
        # the heal PLANNED a soft lock (write-back applies it after the pause; the row is still
        # the pre-write-back seed here, which is what the probe deliberately inspects).
        assert tm_ev.get("locked_soft") == 1, ("the heal plans a soft lock", tm_ev)
    print(
        "  Q9/Part2: a drifted MACHINE translation (MT + pct100) is HEALED, not re-locked "
        "(byte gate intact, tm_healed surfaced): OK"
    )


# --- PART 2 (option A): tm_match HEALS a drifted MACHINE sentinel end-to-end ------------
def _seed_drift(s, name, stored, status, pct=None):
    """Seed a ONE-segment doc whose source has a byte-exact 100% TM entry but whose stored
    target is `stored`/`status` (drifted from TM). Returns (project, doc)."""
    settings.set_many({"tm_hundreds_enter_lqa": "1"})  # SOFT band
    p = _mk_project(s, name)
    tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
    doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
    seg = _seg(s, doc.id, 0)
    seg.target_text = stored
    seg.status = status
    seg.tm_match_pct = pct
    s.add(seg)
    s.commit()
    return p, doc


def test_part2_heals_drifted_machine_sentinel_end_to_end():
    """PART 2 (option A) -- the DECISION-concurrent-run-clobber permanent-strand fix, end to
    end. A MACHINE-status (`mt`) segment whose stored target DIFFERS from its own 100% TM entry
    is the strand: B4 makes tm_match skip it and the MT step re-scaffolds it every run, forever.
    Now tm_match HEALS it -- re-fills from TM through the normal fill path, so [tm_match, MT] to
    COMPLETION lands the TM target + a soft lock (NOT the MockProvider scaffold), and the MT step
    never touches it (the heal soft-locked it out of the pending set). This is the flood's
    plain-second-run shape."""
    with Session(engine) as s:
        p, doc = _seed_drift(s, "P2Heal", stored="Un vieux scaffold MT.", status=SegStatus.MT)
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "completed", summ
        assert summ["steps"][0]["tm_match"].get("tm_healed") == 1, (
            "the drifted mt sentinel must be HEALED",
            summ["steps"],
        )
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == _Q9_EXACT, (
            "healed to the TM target, NOT re-scaffolded by MT",
            seg.target_text,
        )
        assert "[fr-FR]" not in seg.target_text, (
            "the MockProvider scaffold must NOT win",
            seg.target_text,
        )
        assert seg.status == SegStatus.MT and seg.tm_match_pct == 100, (
            seg.status,
            seg.tm_match_pct,
        )
        assert seg.lock_band == "100" and not seg.lock_hard, (
            "healed fill is soft-locked",
            seg.lock_band,
            seg.lock_hard,
        )
        # the TM entry was USED by the heal (a heal is a genuine use, unlike a re-lock).
        entry = s.exec(select(SegmentTM).where(SegmentTM.project_id == p.id)).one()
        assert entry.use_count == 1, (
            "a heal bumps use_count (it is a genuine TM use)",
            entry.use_count,
        )
    print(
        "  Part2: a drifted MACHINE sentinel is HEALED to TM + soft-locked end-to-end "
        "(strand broken): OK"
    )


def test_part2_does_not_heal_needs_review():
    """PART 2 boundary: a DRIFTED needs_review row is NOT healed. needs_review is machine
    ROUTING to a human (a QE fail / refusal / operator flag); healing it back to TM would
    silently erase the reviewer's task and the routing verdict. The byte-EXACT needs_review
    sentinel is covered instead by the probe-5 content-protection (which reaches the `relock`
    decision, not the heal branch). Here the row DIFFERS from TM, so it stays exactly as
    stored, routed to the human."""
    with Session(engine) as s:
        # [tm_match, human_review] pauses BEFORE the write-back (like _q9_relock_probe), so the
        # DB row is left EXACTLY as seeded and the tm_match STEP EVENT reveals the (no-)heal
        # decision -- sidestepping the pre-existing tm_match-only source-overwrite of an
        # unclaimed segment that would otherwise confound the assertion.
        badge = qe.merge_qe_flag(None, qe.qe_flag(40, "bad", 85, False, False))
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, "P2NoHealNR")
        tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
        seg = _seg(s, doc.id, 0)
        seg.target_text = "Une revision humaine en cours."
        seg.status = SegStatus.NEEDS_REVIEW
        seg.qa_flags = badge
        s.add(seg)
        s.commit()
        wf = _mk_workflow(
            s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.HUMAN_REVIEW, "Review")]
        )
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "paused", summ
        assert summ["steps"][0]["tm_match"].get("tm_healed") is None, (
            "needs_review must NOT be healed",
            summ["steps"],
        )
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == "Une revision humaine en cours.", (
            "the routed row is left exactly as stored",
            seg.target_text,
        )
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert not seg.lock_band, ("a routed row is never healed + locked", seg.lock_band)
        assert any(isinstance(f, dict) and f.get("type") == "qe" for f in (seg.qa_flags or [])), (
            "the QE badge / routing survives",
            seg.qa_flags,
        )
    print("  Part2: a DRIFTED needs_review row is NOT healed (routing + badge preserved): OK")


def test_part2_does_not_heal_human_difference():
    """PART 2 boundary: HUMAN statuses keep ABSOLUTE B4 protection. An EDITED (or APPROVED)
    segment whose translation DIFFERS from TM is authoritative human work -- healing is for
    MACHINE drift only, and must NEVER touch it. (test_b4_never_clobbers_human_translation
    covers APPROVED; this pins EDITED.)"""
    with Session(engine) as s:
        _p, doc = _seed_drift(
            s, "P2NoHealHuman", stored="Ma revision humaine.", status=SegStatus.EDITED
        )
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match")])
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["steps"][0]["tm_match"].get("tm_healed") is None, (
            "human EDITED work must NEVER be healed",
            summ["steps"],
        )
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == "Ma revision humaine." and seg.status == SegStatus.EDITED, (
            "human work is untouched",
            seg.target_text,
            seg.status,
        )
        assert not seg.lock_band, seg.lock_band
    print("  Part2: human EDITED difference is NEVER healed (absolute B4): OK")


def test_part2_no_heal_when_tm_locks_disabled():
    """PART 2 respects the TM_LOCKS_ENABLED kill switch: with it OFF the tm_match step is a
    fully inert no-op pass-through (the existing fill semantics), so it must not heal either."""
    from app import config

    _prev = config.TM_LOCKS_ENABLED
    try:
        config.TM_LOCKS_ENABLED = False
        with Session(engine) as s:
            # pause before the write-back (see test_part2_does_not_heal_needs_review) so the
            # seeded row is inspected untouched.
            _p, doc = _seed_drift(
                s, "P2NoHealSwitch", stored="Un vieux scaffold.", status=SegStatus.MT
            )
            wf = _mk_workflow(
                s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.HUMAN_REVIEW, "Review")]
            )
            summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
            assert summ["status"] == "paused", summ
            assert summ["steps"][0]["tm_match"].get("tm_healed") is None, (
                "locks off -> no heal",
                summ["steps"],
            )
            s.expire_all()
            seg = _seg(s, doc.id, 0)
            assert seg.target_text == "Un vieux scaffold." and not seg.lock_band, (
                "locks-off tm_match is inert -- no heal, no lock",
                seg.target_text,
                seg.lock_band,
            )
    finally:
        config.TM_LOCKS_ENABLED = _prev
    print("  Part2: TM_LOCKS_ENABLED off -> tm_match does not heal (inert): OK")


def test_q9_relock_poisoned_prior_fill_refused_no_lock_no_bump():
    """Q9 fix round F3/F4. A NUMBER-CORRUPTED prior fill re-ENTERS the re-lock scope (it wears
    the machine fingerprint: pct == 100 + status == MT + stored byte-equals the poisoned
    hit), so the whitelist admits it and tm_match PLANS a re-lock -- but the write-back
    CONTENT GATE (the single write seam) refuses it (number integrity broken -> tm_eligible
    False -> NEEDS_REVIEW), so NO lock is applied and the number badge survives.

    F4: the bump was firing BEFORE the content gate, so an unlockable poisoned re-lock
    re-bumped use_count EVERY run. A re-lock is an accounting REPAIR, not a USE of the TM, so
    it must NEVER bump. Three consecutive runs over the poisoned prior fill leave use_count
    UNCHANGED (0). Kills a mutation that bumps on re-lock or moves the bump before the gate."""
    src = "Delete 5 files now."
    corrupt = "Supprimez 3 fichiers maintenant."  # 5 -> 3: number value broken
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, "Q9Poison")
        _poison(s, p.id, src, corrupt)  # direct SegmentTM add (bypasses write-time gates)
        entry = s.exec(select(SegmentTM).where(SegmentTM.project_id == p.id)).one()
        assert entry.use_count == 0, entry.use_count
        doc = _mk_doc(s, p.id, "fr-FR", [src])
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        for _ in range(3):
            # re-seed the machine fingerprint each run so a re-lock is genuinely ATTEMPTED
            # (the first run's write-back routes it to NEEDS_REVIEW, which the whitelist would
            # then exclude -- probing the bump requires the re-lock to fire all three times).
            seg = _seg(s, doc.id, 0)
            seg.target_text = corrupt
            seg.status = SegStatus.MT
            seg.tm_match_pct = 100
            seg.lock_band = None
            seg.lock_hard = False
            seg.qa_flags = None
            s.add(seg)
            s.commit()
            summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
            assert summ["status"] == "completed", summ
            assert summ["steps"][0]["tm_match"]["relocked"] == 1, (
                "the re-lock must be ATTEMPTED each run",
                summ,
            )
            s.expire_all()
            seg = _seg(s, doc.id, 0)
            assert not seg.lock_band, (
                "a poisoned re-lock must NEVER lock (content gate)",
                seg.lock_band,
            )
            assert any(
                isinstance(f, dict) and f.get("type") == "number_integrity"
                for f in (seg.qa_flags or [])
            ), ("the number badge must survive", seg.qa_flags)
            entry = s.exec(select(SegmentTM).where(SegmentTM.project_id == p.id)).one()
            assert entry.use_count == 0, (
                "F4: a re-lock must NEVER bump use_count",
                entry.use_count,
            )
    print("  Q9 F3/F4: poisoned re-lock refused (no lock, badge intact, no usage bump x3): OK")


def test_q9_relock_operator_edit_during_pause_drops_plan():
    """Q9 fix round F5. During a human_review pause an operator edits a slot the tm_match step
    planned to re-lock. On RESUME the restored plan + tm_origin would otherwise stamp TM
    provenance (band + tm_id) + SegStatus.MT/pct100 onto content a human just changed. The
    write-back guard drops the plan for a slot whose working text no longer byte-equals the
    planned TM target: no lock band, no tm_lock provenance. PART 1 (pause-edit protection,
    2026-08-13): the operator's edit now lands SegStatus.EDITED (protected) instead of `mt` --
    the F5 no-false-TM-stamp guarantee (no band, no tm_lock, pct cleared) is unchanged; only the
    landing status changed from mt to edited. Kills a mutation that removes the F5 drop-the-plan
    guard."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, "Q9F5")
        tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
        # the Q9 seed: a prior TM-exact fill, pct100 + MT + unlocked -> a re-lock candidate.
        seg = _seg(s, doc.id, 0)
        seg.target_text = _Q9_EXACT
        seg.status = SegStatus.MT
        seg.tm_match_pct = 100
        s.add(seg)
        s.commit()
        wf = _mk_workflow(
            s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.HUMAN_REVIEW, "Review")]
        )
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "paused", summ
        assert summ["steps"][0]["tm_match"]["relocked"] == 1, (
            "the re-lock is planned before the pause",
            summ,
        )
        # the operator overwrites the slot with a genuinely different value, then resumes.
        edited = "An operator's replacement, no longer the TM."
        orchestrator.resume_workflow(s, summ["run_id"], "fr-FR", edited)
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == edited, ("the operator edit stands", seg.target_text)
        assert not seg.lock_band, (
            "F5: an operator-edited slot must NEVER be TM-locked",
            seg.lock_band,
        )
        assert (seg.meta or {}).get("tm_lock") is None, (
            "F5: no TM provenance on human text",
            (seg.meta or {}).get("tm_lock"),
        )
        assert seg.status == SegStatus.EDITED, (
            "PART 1: an operator pause-edit lands EDITED (protected), not the TM-provenance branch",
            seg.status,
        )
        # F4 (Q9 final round): the row carried tm_match_pct=100 (the seeded prior fill). Once
        # the guard drops the plan for a human-edited slot the stale 100 must be CLEARED --
        # otherwise vendor_cost.classify_band / tm.leverage_bands would misprice the
        # human-edited text as a full 100% TM leverage hit. Kills a mutation that omits the
        # `seg.tm_match_pct = None` clear in the write-back guard.
        assert seg.tm_match_pct is None, (
            "F4: the stale 100% match_pct must be cleared when the guard drops the plan",
            seg.tm_match_pct,
        )
    print(
        "  Q9 F5/F4: an operator edit during a pause drops the re-lock plan (no stamp) + "
        "clears the stale pct + lands EDITED: OK"
    )


def test_q9_step0_shortcircuit_operator_edit_drops_provenance():
    """F1 (Q9 final round) -- reviewer probe-2. The STEP-0 TM-first short-circuit (NOT a
    tm_match step) fills a 100% hit and stamps TM provenance (tm_origin -> MT + pct=100) but
    records NO lock plan. The F5 pause-edit guard previously keyed on tm_lock_plan, so an
    operator edit during a later human_review pause resumed with the ORIGINAL TM provenance
    falsely stamped onto the edited text. The short-circuit now records tm_plan_tgt too, and
    the write-back guard keys on tm_plan_tgt ALONE -- so an edit drops the stamp and commits
    down the ordinary MT path (mt_confidence 0.9).

    F4: the stale tm_match_pct=100 is CLEARED (human-edited text is not a 100% TM match and
    must not price as one). PART 1: the edit lands SegStatus.EDITED (protected). This probe DIES
    if the step-0 `tm_plan_tgt[j] = ...` line is removed (the guard then never fires: pct stays
    100, the false TM stamp survives)."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, "Q9Step0")
        tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
        # step 0 is an LLM_AGENT step: its order_index==0 TM-first short-circuit fills the
        # 100% hit and skips the provider call. There is NO tm_match step, hence NO lock plan.
        wf = _mk_workflow(s, [(0, StepType.LLM_AGENT, "MT"), (1, StepType.HUMAN_REVIEW, "Review")])
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "paused", summ
        # the operator overwrites the TM fill during the pause, then resumes.
        edited = "An operator's replacement, not the TM."
        orchestrator.resume_workflow(s, summ["run_id"], "fr-FR", edited)
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == edited, ("the operator edit stands", seg.target_text)
        assert not seg.lock_band, ("the short-circuit path never locks", seg.lock_band)
        assert seg.tm_match_pct is None, (
            "F1/F4: the stale 100% stamp must be cleared on a post-pause operator edit",
            seg.tm_match_pct,
        )
        assert seg.status == SegStatus.EDITED, (
            "PART 1: an operator pause-edit lands EDITED (protected), not the TM-provenance branch",
            seg.status,
        )
    print(
        "  Q9 F1/F4: a step-0 short-circuit edit during a pause drops the TM stamp + "
        "clears pct + lands EDITED: OK"
    )


def _q9_legacy_paused_run(s, name):
    """Create a REAL paused run (tm_match FILL -> human_review) whose persisted run.context
    still carries tm_lock_plan, then STRIP tm_plan_tgt from it -- exactly the shape a run
    paused under the pre-tm_plan_tgt (pre-Q9-final) code leaves behind. Returns (run_id, doc,
    paused_text)."""
    settings.set_many({"tm_hundreds_enter_lqa": "1"})
    p = _mk_project(s, name)
    tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
    doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
    wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.HUMAN_REVIEW, "Review")])
    summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
    assert summ["status"] == "paused", summ
    assert summ["steps"][0]["tm_match"]["filled"] == 1, ("a genuine fill is planned", summ)
    run = s.get(WorkflowRun, summ["run_id"])
    ctx = dict(run.context or {})
    assert "tm_lock_plan" in ctx and "tm_plan_tgt" in ctx, ("current code writes both", ctx)
    ctx.pop("tm_plan_tgt")  # <- simulate the legacy context
    run.context = ctx
    s.add(run)
    s.commit()
    return summ["run_id"], doc, summ["paused_text"]


def test_q9_f2_legacy_context_edit_drops_lock():
    """F2 (Q9 final round, DEPLOY HAZARD). A run paused under code that predates tm_plan_tgt
    restores a tm_lock_plan with NO companion planned-target. Because the write-back guard
    keys on tm_plan_tgt, such a legacy plan would sail past the guard and LOCK whatever text
    resumes -- including operator-edited text -- with false TM lock provenance. Conservative
    rule: on restore, DROP any tm_lock_plan entry that has no tm_plan_tgt (lose a lock, never
    stamp a lie). Resume WITH an edit -> no lock, no lock provenance. Kills a mutation that
    removes the F2 restore-seam drop (which would lock + stamp the human's text)."""
    with Session(engine) as s:
        run_id, doc, _ = _q9_legacy_paused_run(s, "Q9F2Edit")
        edited = "A human's post-pause edit."
        orchestrator.resume_workflow(s, run_id, "fr-FR", edited)
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == edited, ("the operator edit stands", seg.target_text)
        assert not seg.lock_band, (
            "F2: a legacy plan with no planned-target must NOT lock human-edited text",
            seg.lock_band,
        )
        assert (seg.meta or {}).get("tm_lock") is None, (
            "F2: no lock provenance stamped on the human text",
            seg.meta,
        )
    print("  Q9 F2: a legacy paused context (no tm_plan_tgt) drops the lock on an edit: OK")


def test_q9_f2_legacy_context_no_edit_also_drops_lock():
    """F2 (Q9 final round) -- the DOCUMENTED COST. On the SAME legacy context, resuming
    WITHOUT an edit ALSO loses the lock: the conservative restore-drop cannot tell an edit
    from an unchanged fill (there is no planned-target to compare), so it drops the plan
    either way. The TM fill still stands as content (a subsequent normal run re-locks the
    clean fill from scratch); it is simply not locked on THIS resume. Kills the same F2
    mutation from the no-edit direction (which would apply the legacy lock)."""
    with Session(engine) as s:
        run_id, doc, paused_text = _q9_legacy_paused_run(s, "Q9F2NoEdit")
        # resume with the paused text UNCHANGED (no operator edit).
        orchestrator.resume_workflow(s, run_id, "fr-FR", paused_text)
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == _Q9_EXACT, ("the TM fill survives as content", seg.target_text)
        assert not seg.lock_band, (
            "F2 documented cost: a legacy plan with no planned-target "
            "does not lock even an unchanged fill",
            seg.lock_band,
        )
        assert (seg.meta or {}).get("tm_lock") is None, ("no lock provenance", seg.meta)
    print("  Q9 F2: a legacy paused context also drops the lock with NO edit (documented cost): OK")


def test_probe5_needs_review_byte_exact_tm_fill_protected_through_mt():
    """Q9 re-review, probe 5 -- the CONTENT-DESTRUCTION class this protection closes. Seed a
    segment whose stored target byte-EQUALS its own 100% TM hit but which sits at NEEDS_REVIEW
    with a qa badge (a soft gate flagged the fill at fill time). Run tm_match -> translate (MT)
    to COMPLETION -- the shape the Q9 re-lock whitelist DELIBERATELY sidesteps (it only re-locks
    status == MT).

    Without this protection the unclaimed row falls into the MT step's pending set and gets
    RE-TRANSLATED: the byte-exact sentinel is destroyed (MockProvider echo overwrites it), the
    badge is erased by the always-on stale-clear, and status returns to mt. The fix marks the
    row content-protected -- excluded from every later step's pending set AND skipped at
    write-back -- so the target stays byte-identical, the badge survives, status stays
    NEEDS_REVIEW, and the row is neither re-labelled (no tm_origin/MT) nor locked.

    Kills the fix in BOTH directions: drop the pending-set exclusion and the MT step re-translates
    the sentinel; drop the write-back skip and commit_mt_output flips status to MT + stale-clears
    the badge even on the byte-identical stored target."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})  # SOFT policy (never reached here)
        p = _mk_project(s, "Probe5")
        tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
        # the probe-5 seed: a byte-exact 100% TM fill under a soft review gate + a qa badge.
        badge = qe.merge_qe_flag(None, qe.qe_flag(40, "bad", 85, False, False))
        seg = _seg(s, doc.id, 0)
        seg.target_text = _Q9_EXACT
        seg.status = SegStatus.NEEDS_REVIEW
        seg.tm_match_pct = 100
        seg.qa_flags = badge
        s.add(seg)
        s.commit()
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "completed", summ
        # the tm_match step SURFACES the protection -- and reports NO fill / re-lock / no-op.
        tm_ev = summ["steps"][0]["tm_match"]
        assert tm_ev.get("content_protected") == 1, ("the row must be content-protected", tm_ev)
        assert tm_ev["relocked"] == 0 and tm_ev["filled"] == 0, (
            "neither filled nor re-locked",
            tm_ev,
        )
        assert "noop" not in tm_ev, ("protecting a sentinel is genuine work, not a no-op", tm_ev)
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # (a) the byte-exact TM sentinel is intact -- the MT step did NOT re-translate it (a
        #     MockProvider echo would have carried the locale tag + English source).
        assert seg.target_text == _Q9_EXACT, (
            "the MT step must not destroy the byte-exact sentinel",
            seg.target_text,
        )
        # (b) review routing preserved: still NEEDS_REVIEW with its qa badge (not stale-cleared).
        assert seg.status == SegStatus.NEEDS_REVIEW, ("review routing preserved", seg.status)
        assert any(isinstance(f, dict) and f.get("type") == "qe" for f in (seg.qa_flags or [])), (
            "the qa badge must survive, not be erased by the always-on stale-clear",
            seg.qa_flags,
        )
        # (c) neither re-labelled nor locked: the pct stamp is untouched and no lock landed.
        assert seg.tm_match_pct == 100, (
            "the TM provenance stamp is left as stored",
            seg.tm_match_pct,
        )
        assert not seg.lock_band, ("a content-protected row must never be locked", seg.lock_band)
        assert (seg.meta or {}).get("tm_lock") is None, ("no TM lock provenance stamped", seg.meta)
        # the run's final text carries the protected sentinel, not a blanked / re-translated slot.
        assert summ["text"] == _Q9_EXACT, (
            "the terminal `done` text emits the stored target",
            summ["text"],
        )
    print(
        "  probe 5: a needs_review byte-exact TM fill survives tm_match->MT byte-identical "
        "(badge + routing intact, no re-label, no lock): OK"
    )


def test_probe5_editable_needs_review_not_over_protected():
    """Q9 re-review, probe 5 -- the FALSE-POSITIVE DISCIPLINE. A NEEDS_REVIEW segment whose
    stored target is NOT byte-equal to any 100% TM hit is genuinely EDITABLE content, not a
    trusted sentinel -- it must NOT be protected, and a normal MT step must still translate it.
    fill_or_relock returns None on a divergent stored target (never a `relock` decision), so the
    content-protection branch is never reached: the row stays in the pending set and is MT'd.

    Kills a mutation that widens protection to ALL NEEDS_REVIEW pct=100 rows regardless of
    byte-equality (which would freeze a stale review target forever)."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, "Probe5Edit")
        tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
        # a NEEDS_REVIEW pct=100 row whose target DIVERGES from the TM hit (a human/MT edit that
        # was later flagged): NOT a byte-exact sentinel, so NOT eligible for protection.
        seg = _seg(s, doc.id, 0)
        seg.target_text = "Un texte divergent a reviser."
        seg.status = SegStatus.NEEDS_REVIEW
        seg.tm_match_pct = 100
        s.add(seg)
        s.commit()
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "completed", summ
        # NOTHING was content-protected -- the divergent target is not a trusted sentinel.
        assert "content_protected" not in summ["steps"][0]["tm_match"], (
            "a divergent needs_review target must never be protected",
            summ["steps"][0],
        )
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # the MT step translated it normally (MockProvider echo carries the English source +
        # locale tag) -- it was NOT frozen at the stale divergent target.
        assert _Q9_SRC in seg.target_text, (
            "a genuinely-editable needs_review row is still translated, not over-protected",
            seg.target_text,
        )
        assert seg.target_text != "Un texte divergent a reviser.", (
            "the stale divergent target was not frozen",
            seg.target_text,
        )
    print(
        "  probe 5: a divergent needs_review row (not byte-equal to a TM hit) is still "
        "translated -- protection does not over-fire: OK"
    )


def test_probe5_status_mt_byte_exact_follows_q9_relock_unchanged():
    """Q9 re-review, probe 5 -- the CONTRAST case: a status == MT byte-exact 100% fill (the
    original Q9 seed: the plain-translate exact-TM short-circuit leaves MT + pct=100 + no lock)
    still follows the Q9 RE-LOCK path unchanged. Protection is scoped to the NEEDS_REVIEW slice;
    an MT-status sentinel is repaired by being re-locked, exactly as Q9 established -- it is
    re-locked (band restored), NOT content-protected."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})  # SOFT band
        p = _mk_project(s, "Probe5MT")
        tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
        seg = _seg(s, doc.id, 0)
        seg.target_text = _Q9_EXACT
        seg.status = SegStatus.MT  # the Q9 machine-fingerprint slice
        seg.tm_match_pct = 100
        s.add(seg)
        s.commit()
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "completed", summ
        tm_ev = summ["steps"][0]["tm_match"]
        # Q9 path: RE-LOCKED (not content-protected).
        assert tm_ev["relocked"] == 1, (
            "a status=MT byte-exact fill follows the Q9 re-lock path",
            tm_ev,
        )
        assert "content_protected" not in tm_ev, ("the MT slice is re-locked, not protected", tm_ev)
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == _Q9_EXACT, ("the sentinel survives", seg.target_text)
        assert seg.lock_band == "100", ("the Q9 re-lock restored the lost band", seg.lock_band)
    print(
        "  probe 5: a status=MT byte-exact fill still follows the Q9 re-lock path "
        "(re-locked, not protected): OK"
    )


def test_probe5_operator_edit_during_pause_wins_over_protection():
    """Q9 re-review, probe 5 -- the HUMAN-EDIT-WINS corner (and the proof a protected row is NOT
    frozen forever). A protected row (byte-exact 100% TM fill + NEEDS_REVIEW + badge) rides into
    a human_review PAUSE. If the operator EDITS it during the pause, the write-back skip must NOT
    silently discard the edit and re-emit the stored sentinel: the `working[j] == target_text`
    guard detects the diverged resume seed and lets the edit commit down the ordinary MT path.

    Kills a mutation that drops the guard (an unconditional skip): with it removed the operator
    edit is thrown away and the stale stored target is emitted + kept in the DB -- i.e. the row
    would be protected forever, the exact failure the guard exists to prevent."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, "Probe5Pause")
        tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
        badge = qe.merge_qe_flag(None, qe.qe_flag(40, "bad", 85, False, False))
        seg = _seg(s, doc.id, 0)
        seg.target_text = _Q9_EXACT
        seg.status = SegStatus.NEEDS_REVIEW
        seg.tm_match_pct = 100
        seg.qa_flags = badge
        s.add(seg)
        s.commit()
        wf = _mk_workflow(
            s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.HUMAN_REVIEW, "Review")]
        )
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "paused", summ
        assert summ["steps"][0]["tm_match"].get("content_protected") == 1, (
            "protected before the pause",
            summ,
        )
        # the protected sentinel is the paused blob's content for this slot...
        assert summ["paused_text"] == _Q9_EXACT, (
            "the pause shows the stored target",
            summ["paused_text"],
        )
        # ...and the operator replaces it during the pause, then resumes.
        edited = "Une revision de l'operateur."
        orchestrator.resume_workflow(s, summ["run_id"], "fr-FR", edited)
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == edited, (
            "the operator edit must WIN over the protection, not "
            "be discarded for the stale sentinel",
            seg.target_text,
        )
        assert not seg.lock_band, ("still never locked", seg.lock_band)
    print(
        "  probe 5: an operator edit during a pause WINS over content-protection "
        "(not discarded / not frozen forever): OK"
    )


def _probe5_finalized_survives(status, name):
    """F1 shared body: a byte-exact 100% TM fill whose stored target byte-equals the current
    TM hit but sits at a HUMAN-FINALIZED status (EDITED or APPROVED) with pct=100 + no lock
    (the Q9 seed shape). Without protection the row drops into the MT step's pending set and is
    SILENTLY OVERWRITTEN with raw MT + flipped to status=mt -- a human sign-off destroyed.
    Protection keeps it byte-identical with its status unchanged, and it is never re-labelled
    or locked.

    ACCOUNTING (human-work protection, data-integrity fix): an EDITED/APPROVED segment is now
    caught by the BROAD status protection (Layer 2: excluded from EVERY provider dispatch at
    run start, not just shielded at the tm_match relock branch) BEFORE the tm_match step sees
    it -- so the tm_match step reports no candidate (`filled`/`relocked` == 0) and the run
    surfaces the protection at the run level under `lqa_safety.status_protected`, rather than
    at the tm_match step's `content_protected` (which now covers only the NEEDS_REVIEW
    byte-exact sentinel, out of the status guard's scope). The NET guarantee this probe exists
    to prove -- the human-finalized byte-exact target survives the MT run byte-identical -- is
    unchanged and still fully asserted below."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, name)
        tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
        seg = _seg(s, doc.id, 0)
        seg.target_text = _Q9_EXACT
        seg.status = status
        seg.tm_match_pct = 100
        s.add(seg)
        s.commit()
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match"), (1, StepType.LLM_AGENT, "MT")])
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "completed", summ
        tm_ev = summ["steps"][0]["tm_match"]
        # BROAD status protection (Layer 2) excludes the EDITED/APPROVED row from dispatch at
        # run start, so the tm_match step never fills or re-locks it, and the run surfaces the
        # protection at the run level (not the step's content_protected).
        assert tm_ev.get("relocked", 0) == 0 and tm_ev.get("filled", 0) == 0, (
            "neither filled nor re-locked",
            tm_ev,
        )
        assert summ.get("lqa_safety", {}).get("status_protected") == 1, (
            "the finalized fill must be protected (run-level status_protected)",
            summ,
        )
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # the byte-exact human-finalized target is intact -- the MT step did NOT overwrite it.
        assert seg.target_text == _Q9_EXACT, (
            "the MT step must not overwrite the human-finalized byte-exact sentinel",
            seg.target_text,
        )
        # status is left EXACTLY as stored -- not flipped to mt, not re-labelled.
        assert seg.status == status, ("the human sign-off status is preserved", seg.status)
        assert seg.tm_match_pct == 100, ("the pct stamp is left as stored", seg.tm_match_pct)
        assert not seg.lock_band, ("a content-protected row must never be locked", seg.lock_band)
        assert (seg.meta or {}).get("tm_lock") is None, ("no TM lock provenance", seg.meta)
        assert summ["text"] == _Q9_EXACT, ("the done text emits the stored target", summ["text"])


def test_probe5_approved_byte_exact_tm_fill_protected_through_mt():
    """F1 (adversarial follow-up) -- an APPROVED byte-exact 100% TM fill survives tm_match->MT
    byte-identical with status APPROVED unchanged, never re-labelled or locked. Kills the F1
    predicate revert (NEEDS_REVIEW-only): with APPROVED dropped from the predicate the MT step
    silently overwrites the sign-off with raw MT and flips status to mt."""
    _probe5_finalized_survives(SegStatus.APPROVED, "Probe5Approved")
    print(
        "  probe 5 F1: an APPROVED byte-exact TM fill survives tm_match->MT byte-identical "
        "(sign-off intact, no re-label, no lock): OK"
    )


def test_probe5_edited_byte_exact_tm_fill_protected_through_mt():
    """F1 (adversarial follow-up) -- an EDITED byte-exact 100% TM fill (a CAT-editor "confirm
    unchanged" that byte-equals the current TM) survives tm_match->MT byte-identical with status
    EDITED unchanged. Kills the F1 predicate revert from the EDITED direction."""
    _probe5_finalized_survives(SegStatus.EDITED, "Probe5Edited")
    print(
        "  probe 5 F1: an EDITED byte-exact TM fill survives tm_match->MT byte-identical "
        "(human edit intact, no re-label, no lock): OK"
    )


def test_probe5_protection_survives_pause_resume_no_edit():
    """F2 (adversarial follow-up) -- the content_protected PAUSE-RESTORE seam. A protected row
    (byte-exact 100% TM fill + NEEDS_REVIEW + badge) rides through a human_review PAUSE with a
    LATER MT step, and the operator resumes WITHOUT editing. The tm_match step that marked the
    row protected does NOT re-run on resume, so the restore of `content_protected` from
    run.context is the ONLY thing keeping the post-pause MT step from re-translating the
    sentinel. Assert it survives byte-identical with the badge + status intact.

    Kills a mutation that drops the _saved_cp restore: with the set empty on resume the post-
    pause MT step re-translates the sentinel and the always-on stale-clear erases the badge."""
    with Session(engine) as s:
        settings.set_many({"tm_hundreds_enter_lqa": "1"})
        p = _mk_project(s, "Probe5Restore")
        tm.write_back(s, p.id, _Q9_SRC, _Q9_EXACT, "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", [_Q9_SRC])
        badge = qe.merge_qe_flag(None, qe.qe_flag(40, "bad", 85, False, False))
        seg = _seg(s, doc.id, 0)
        seg.target_text = _Q9_EXACT
        seg.status = SegStatus.NEEDS_REVIEW
        seg.tm_match_pct = 100
        seg.qa_flags = badge
        s.add(seg)
        s.commit()
        # tm_match -> human_review -> MT: the MT step runs AFTER the pause, so only the restored
        # content_protected set can shield the sentinel from it.
        wf = _mk_workflow(
            s,
            [
                (0, StepType.TM_MATCH, "TM match"),
                (1, StepType.HUMAN_REVIEW, "Review"),
                (2, StepType.LLM_AGENT, "MT"),
            ],
        )
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "paused", summ
        assert summ["steps"][0]["tm_match"].get("content_protected") == 1, (
            "protected before the pause",
            summ,
        )
        # resume WITHOUT an operator edit (the paused text unchanged).
        orchestrator.resume_workflow(s, summ["run_id"], "fr-FR", summ["paused_text"])
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        # the post-pause MT step did NOT re-translate the sentinel (restore held).
        assert seg.target_text == _Q9_EXACT, (
            "the restored protection must survive the pause so "
            "the post-pause MT step cannot re-translate it",
            seg.target_text,
        )
        assert seg.status == SegStatus.NEEDS_REVIEW, (
            "review routing preserved across resume",
            seg.status,
        )
        assert any(isinstance(f, dict) and f.get("type") == "qe" for f in (seg.qa_flags or [])), (
            "the qa badge survives the resume",
            seg.qa_flags,
        )
        assert not seg.lock_band, ("still never locked", seg.lock_band)
    print(
        "  probe 5 F2: a protected sentinel survives a human_review pause + resume-NO-edit "
        "through a post-pause MT step (restore seam defended): OK"
    )


# ============================================================================ #
# Batch 15 FIX-TRAIN (D15-6 / LAW-TM-13, LAW-TM-16): a COMPLETED tm_match-only run
# must leave every UNMATCHED target EMPTY -- never silently source-fill it as clean
# MT (the defect that made the empty-delivery gate unreachable on its primary path).
# The terminal write-back now gates the commit on the PRODUCED mask (`j in produced`),
# NOT on a text comparison, so a slot NO step produced stays empty while a slot a step
# genuinely produced -- including an identity translation that equals its source --
# still commits exactly as before.
# ============================================================================ #
class _EchoSourceProvider(providers.MockProvider):
    """A provider double whose translate call returns the SOURCE text VERBATIM -- a
    legitimate identity translation (a product name, a number, an already-localized
    UI string). Every other task falls through to the deterministic MockProvider, so
    the double is inert outside the translate path."""

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        if task == "translate":
            return (payload or {}).get("text", "")
        return super().complete(
            prompt, system=system, task=task, payload=payload, images=images, **kw
        )


def test_tm_only_run_leaves_unmatched_empty():
    """THE CORE DEFECT PIN. A tm_match-ONLY workflow (a single TM_MATCH step, no LLM
    translator) that COMPLETES over a fresh doc: the matched segment fills, the
    UNMATCHED segment must stay EMPTY (target_text == "") at status NEW -- NOT
    source-filled as clean MT. Before the fix the terminal write-back committed the
    source-seeded `working[j]` for the never-produced slot (status mt, tm_match_pct
    None), so a completed run left ZERO empties and `empty_gate_decision` could never
    fire -- the deliverable shipped English silently. `used_tm_match` is stamped either
    way (the durable gate marker, T1)."""
    with Session(engine) as s:
        p = _mk_project(s, "TMOnlyEmpty")
        tm.write_back(s, p.id, "Save changes.", "Enregistrer.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Save changes.", "This is never matched."])
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match")])  # NO translator
        summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summary["status"] == "completed", summary
        s.expire_all()
        matched = _seg(s, doc.id, 0)
        unmatched = _seg(s, doc.id, 1)
        # the matched segment fills as before...
        assert matched.target_text == "Enregistrer." and matched.tm_match_pct == 100, matched
        # ...the UNMATCHED segment stays genuinely EMPTY and untranslated (the fix)...
        assert unmatched.target_text == "", (
            "a never-produced slot must stay EMPTY, not source-fill",
            unmatched.target_text,
        )
        assert unmatched.status == SegStatus.NEW, (
            "a never-produced slot must NOT carry status=mt (it was never machine-translated)",
            unmatched.status,
        )
        assert not unmatched.lock_band, "an unmatched segment is never locked"
        # the durable gate marker is stamped so the delivery-time predicate can fire.
        run = s.get(WorkflowRun, summary["run_id"])
        assert run.used_tm_match is True, "the tm_match run must stamp used_tm_match"
    print("  tm_match-ONLY: unmatched target stays EMPTY (NEW), never source-filled: OK")


def test_produced_tm_fill_equal_to_source_commits():
    """THE PRODUCED-MASK-NOT-TEXT-COMPARISON PIN. A TM entry whose TARGET byte-equals its
    SOURCE ("OK" -> "OK": an already-localized string) is a GENUINE production. It must
    COMMIT (target "OK", status MT, tm_match_pct 100) even though working[j] ==
    seg_sources[j]. A naive text-comparison "fix" (`skip iff working[j] == source`) would
    wrongly SKIP this legitimate fill and leave it empty; the produced-mask gate keeps it.
    """
    with Session(engine) as s:
        p = _mk_project(s, "TMFillEqSrc")
        tm.write_back(s, p.id, "OK", "OK", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["OK"])
        wf = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match")])
        summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summary["status"] == "completed", summary
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert seg.target_text == "OK", ("a produced source-identical TM fill must commit", seg)
        assert seg.status == SegStatus.MT and seg.tm_match_pct == 100, seg
    print("  produced TM fill whose target == source still commits (mask, not text-cmp): OK")


def test_translator_source_identical_output_commits():
    """The translator-source-identical case (LAW-TM-3). An LLM_AGENT step that legitimately
    outputs text byte-equal to the source went through the produce path -> `produced.add(j)`
    fired REGARDLESS of the text value, so the write-back must still COMMIT it (non-empty,
    status MT). Only a slot NO step ever produced is skipped."""
    providers.register_provider("mock", _EchoSourceProvider())
    try:
        with Session(engine) as s:
            p = _mk_project(s, "TranslatorIdentity")
            doc = _mk_doc(s, p.id, "fr-FR", ["Product Name."])
            wf = Workflow(name="IdentityWF")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            # provider="mock" explicitly (config.DEFAULT_PROVIDER is a keyless cloud name
            # that would fall through to the REAL MockProvider, bypassing the override).
            s.add(
                WorkflowStep(
                    workflow_id=wf.id,
                    order_index=0,
                    name="MT",
                    step_type=StepType.LLM_AGENT,
                    provider="mock",
                )
            )
            s.commit()
            summary = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
            assert summary["status"] == "completed", summary
            s.expire_all()
            seg = _seg(s, doc.id, 0)
            assert seg.target_text == "Product Name.", (
                "a translator's legitimate identity output must commit, not be skipped",
                seg.target_text,
            )
            assert seg.status == SegStatus.MT, seg.status
    finally:
        providers._OVERRIDES.pop("mock", None)
    print("  a translator's source-identical output commits (produced, not skipped): OK")


def test_rerun_tm_only_does_not_blank_prior_translation():
    """RE-RUN / RESUME SAFETY. A second tm_match-only run over an already-translated doc
    whose segments get NO TM hit this run must LEAVE THE EXISTING TARGETS ALONE -- never
    overwrite a prior translation with the source-seeded buffer. Before the fix the fresh
    run re-seeded working[] from source and committed source over every unlocked, non-
    approved MT segment, silently BLANKING (source-filling) the whole prior locale."""
    with Session(engine) as s:
        p = _mk_project(s, "TMRerunNoBlank")
        # Run 1: a plain MT workflow translates both segments (unlocked SegStatus.MT).
        doc = _mk_doc(s, p.id, "fr-FR", ["First line.", "Second line."])
        wf1 = _mk_workflow(s, [(0, StepType.LLM_AGENT, "MT")])
        s1 = orchestrator.run_workflow(s, wf1.id, doc.id, "fr-FR")
        assert s1["status"] == "completed", s1
        s.expire_all()
        t0, t1 = _seg(s, doc.id, 0).target_text, _seg(s, doc.id, 1).target_text
        assert t0 and t1 and "First line." in t0 and "Second line." in t1, (t0, t1)
        # neither is locked (a plain MT step never locks), so nothing but the fix protects
        # them from the re-run's source seed.
        assert not _seg(s, doc.id, 0).lock_band and not _seg(s, doc.id, 1).lock_band
        # Run 2: a tm_match-ONLY workflow with NO matching TM entries -> zero fills.
        wf2 = _mk_workflow(s, [(0, StepType.TM_MATCH, "TM match")])
        s2 = orchestrator.run_workflow(s, wf2.id, doc.id, "fr-FR")
        assert s2["status"] == "completed", s2
        s.expire_all()
        # the prior translations survive untouched (never blanked to source).
        assert _seg(s, doc.id, 0).target_text == t0, (
            "a re-run must not blank a prior translation",
            _seg(s, doc.id, 0).target_text,
        )
        assert _seg(s, doc.id, 1).target_text == t1, (
            "a re-run must not blank a prior translation",
            _seg(s, doc.id, 1).target_text,
        )
    print("  a re-run tm_match-only over a translated doc never blanks prior targets: OK")


class _LiveEchoProvider(providers.Provider):
    """A NON-Mock provider (so `translate._lang_guard_for` resolves True) that echoes the
    English source verbatim -- the exact GitHub #43 / prod-v82 live-echo shape."""

    name = "live-echo"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return (payload or {}).get("text") or ""


def test_lang_guard_echo_survives_pause_resume():
    """RESUME-SAFETY of the produced-mask gate -- the `seg_lang_guard` exclusion (gate
    verdict-round finding: REACHABLE + LOAD-BEARING). A LIVE provider that echoes the
    English source across a human_review pause lands, after resume, in the state
    produced=False / tm_origin=False / seg_lang_guard=True: `produced` was re-derived by
    text comparison (echo == source, so not reconstructed) and it is not a TM fill -- so
    the pause-PERSISTED `seg_lang_guard[j]` marker is the SOLE thing keeping the echo on
    the commit path. Dropping the `not seg_lang_guard[j]` conjunct turns the committed
    echo (a NEEDS_REVIEW-routed non-translation) into "" at status NEW -- SILENT CONTENT
    LOSS with every suite otherwise green. This pin fires RED under that mutation.

    Gate findings on the guard's OTHER exclusion legs, recorded so nobody prunes them:
      * `operator_edited` is REDUNDANT-but-kept -- a human_review pause-edit is landed by
        its own persistence path independently, so the term is defence-in-depth, not the
        sole protector of any reachable state.
      * `working[j] == seg_sources[j]` is a FAIL-OPEN term for a by-construction-
        unreachable state: every working[j] mutation is paired with produced.add(j), and
        the gate verified that breaking produced-tracking at a real production site does
        NOT cause the guard to fire (the term keeps such a slot on the commit path). It is
        the safe-direction assertion of the produced/working pairing invariant, not a
        behaviour toggle.
    """
    providers.register_provider("local", _LiveEchoProvider())
    try:
        with Session(engine) as s:
            p = _mk_project(s, "LangGuardEchoPause")
            english = "The report is ready and the user can download it."
            doc = _mk_doc(s, p.id, "fr-FR", [english])
            wf = Workflow(name="EchoPauseWF")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            s.add(
                WorkflowStep(
                    workflow_id=wf.id,
                    order_index=0,
                    name="MT",
                    step_type=StepType.LLM_AGENT,
                    provider="local",  # a live (non-mock) provider -> lang_guard opts in
                )
            )
            s.add(
                WorkflowStep(
                    workflow_id=wf.id, order_index=1, name="Review", step_type=StepType.HUMAN_REVIEW
                )
            )
            s.commit()
            summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
            assert summ["status"] == "paused", summ
            orchestrator.resume_workflow(s, summ["run_id"], "fr-FR", summ["paused_text"])
            s.expire_all()
            seg = _seg(s, doc.id, 0)
            # THE LOAD-BEARING ASSERTION: the echoed target SURVIVES the terminal write-back
            # (not skipped to "") because the seg_lang_guard leg kept it on the commit path.
            assert seg.target_text == english, (
                "the live English echo must survive the pause/resume write-back, not be skipped",
                seg.target_text,
            )
            # ...and lang_guard did its job at the commit: routed NEEDS_REVIEW with the flag.
            assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
            flags = [f.get("type") for f in (seg.qa_flags or []) if isinstance(f, dict)]
            assert refusal.NONTRANSLATION_FLAG_TYPE in flags, (seg.target_text, flags)
    finally:
        providers._OVERRIDES.pop("local", None)
    print("  a live English echo survives a pause/resume, lang-guard-routed (seg_lang_guard): OK")


def test_source_identical_tm_fill_survives_pause_resume():
    """RESUME-SAFETY of the produced-mask gate (the `tm_origin` exclusion). A TM fill whose
    target byte-equals its source ("OK" -> "OK") is a GENUINE production, but on a resumed
    run `produced` is re-derived by text comparison (working[j] == source) so it is NOT
    reconstructed there. Without the durable `tm_origin[j]` exclusion the write-back would
    treat it as never-produced and SKIP it -> the fill would be LOST (target empty). The
    exclusion keeps it on the commit path, so the fill + its TM provenance survive the pause.
    """
    with Session(engine) as s:
        p = _mk_project(s, "IdentityFillPause")
        tm.write_back(s, p.id, "OK", "OK", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["OK", "Free line."])
        wf = _mk_workflow(
            s,
            [
                (0, StepType.TM_MATCH, "TM match"),
                (1, StepType.HUMAN_REVIEW, "Review"),
            ],
        )
        summ = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert summ["status"] == "paused", summ
        orchestrator.resume_workflow(s, summ["run_id"], "fr-FR", summ["paused_text"])
        s.expire_all()
        filled = _seg(s, doc.id, 0)
        # the source-identical TM fill survived the pause: committed, not skipped/blanked.
        assert filled.target_text == "OK", (
            "a source-identical TM fill must survive a pause/resume, not be skipped",
            filled.target_text,
        )
        assert filled.tm_match_pct == 100, ("TM provenance must survive", filled.tm_match_pct)
        # the genuinely-unmatched sibling stays EMPTY across the same resume.
        assert _seg(s, doc.id, 1).target_text == "", _seg(s, doc.id, 1).target_text
    print("  a source-identical TM fill survives a human_review pause/resume (tm_origin): OK")


if __name__ == "__main__":
    test_tm_match_then_mt_soft()
    test_hard_lock_survives_second_run()
    test_poisoned_hit_not_locked_in_workflow()
    test_q6_soft_glossary_flag_keeps_tm_lock()
    test_f4_number_poisoned_tm_hit_not_locked()
    test_f6_char_limit_tm_fill_not_locked()
    test_f10_correction_rewrite_never_locked()
    test_f6_qe_failed_tm_fill_not_locked()
    test_r1a_step_errored_tm_fill_not_locked()
    test_r1b_fix_reverted_tm_fill_not_locked()
    test_r3_flagged_fill_never_hard_locks()
    test_add_step_parity_tm_match()
    test_policy_resolves_from_settings()
    test_b4_never_clobbers_human_translation()
    test_b3_lock_survives_pause_resume()
    test_persisted_hard_lock_excluded_next_run()
    test_item8_soft_lock_editable_in_round2_fix()
    test_q9_po_prior_fill_is_relocked_and_protected()
    test_q9_relock_positive_baseline()
    test_q9_relock_skips_pct_not_100()
    test_q9_relock_skips_status_approved()
    test_q9_relock_skips_status_edited()
    test_q9_relock_skips_needs_review_preserves_badge()
    test_q9_relock_skips_different_translation()
    test_part2_heals_drifted_machine_sentinel_end_to_end()
    test_part2_does_not_heal_needs_review()
    test_part2_does_not_heal_human_difference()
    test_part2_no_heal_when_tm_locks_disabled()
    test_q9_relock_poisoned_prior_fill_refused_no_lock_no_bump()
    test_q9_relock_operator_edit_during_pause_drops_plan()
    test_q9_step0_shortcircuit_operator_edit_drops_provenance()
    test_q9_f2_legacy_context_edit_drops_lock()
    test_q9_f2_legacy_context_no_edit_also_drops_lock()
    test_probe5_needs_review_byte_exact_tm_fill_protected_through_mt()
    test_probe5_editable_needs_review_not_over_protected()
    test_probe5_status_mt_byte_exact_follows_q9_relock_unchanged()
    test_probe5_operator_edit_during_pause_wins_over_protection()
    test_probe5_approved_byte_exact_tm_fill_protected_through_mt()
    test_probe5_edited_byte_exact_tm_fill_protected_through_mt()
    test_probe5_protection_survives_pause_resume_no_edit()
    test_tm_only_run_leaves_unmatched_empty()
    test_produced_tm_fill_equal_to_source_commits()
    test_translator_source_identical_output_commits()
    test_rerun_tm_only_does_not_blank_prior_translation()
    test_lang_guard_echo_survives_pause_resume()
    test_source_identical_tm_fill_survives_pause_resume()
    print("verify_tm_match_step: all OK")
