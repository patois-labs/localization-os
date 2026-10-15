"""reports_the_query: shared leaf helpers (layered pure move)

Documented in: docs/architecture/reporting-and-dashboard.md
"""

from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import or_
from sqlmodel import Session, select

from app.models import (
    LQAResult,
    SegmentPostEdit,
    StepRun,
    StepType,
    WorkflowRun,
    WorkflowStep,
)

_QA_QUERY_COLS = (
    LQAResult.document_id,
    LQAResult.segment_id,
    LQAResult.dimension,
    LQAResult.severity,
    LQAResult.score,
    LQAResult.created_at,
    LQAResult.target_locale,
    LQAResult.finding_source,
    LQAResult.judge,
    LQAResult.scoring_id,
    LQAResult.vendor_id,
    LQAResult.actor,
    LQAResult.withdrawn_at,
)


_VENDOR_LQA_COLS = (
    LQAResult.vendor_id,
    LQAResult.document_id,
    LQAResult.severity,
    LQAResult.scoring_id,
    LQAResult.score,
    LQAResult.created_at,
    LQAResult.withdrawn_at,
)


_PE_QUERY_COLS = (
    SegmentPostEdit.segment_id,
    SegmentPostEdit.document_id,
    SegmentPostEdit.target_locale,
    SegmentPostEdit.episode_seq,
    SegmentPostEdit.unit_kind,
    SegmentPostEdit.actor,
    SegmentPostEdit.actor_kind,
    SegmentPostEdit.vendor_id,
    SegmentPostEdit.baseline_kind,
    SegmentPostEdit.baseline_chars,
    SegmentPostEdit.final_chars,
    SegmentPostEdit.source_words,
    SegmentPostEdit.distance_char,
    SegmentPostEdit.distance_word,
    SegmentPostEdit.distance_pct,
    SegmentPostEdit.cum_distance_char,
    SegmentPostEdit.cum_distance_word,
    SegmentPostEdit.cum_distance_pct,
    SegmentPostEdit.metric_version,
    SegmentPostEdit.edit_count,
    SegmentPostEdit.last_edited_at,
)


_REPORT_FORBIDDEN_BLOB_COLS = ("note", "baseline_text", "final_sha")


# WAVE 3 (SPEC-wave3-collaboration §6.6, HIGH-3): the qa reader now needs `actor` (a
# human finding buckets on it on the actor axis) and `withdrawn_at` (a retracted human
# finding is excluded from every bucket). Both are SCALARS (a short email, a timestamp),
# never a TEXT blob, so the F1 law is honoured — the query-shape test asserts these two
# are PRESENT and `note` is still ABSENT, so the widening is a decision on the record.
# WAVE 3 §6.6: the vendor scorecard needs the withdrawn exclusion (never the actor).
# F1 memory law (MED-1): the qa/postedit/vendor-quality readers project ONLY these columns
# and NEVER the TEXT blobs — LQAResult.note, SegmentPostEdit.baseline_text/final_sha — that
# the +262 MB F1 finding forbids loading on a report run. A query-shape test asserts the
# blob columns are absent from these tuples so a future edit cannot silently re-add the load.
# WAVE 3 (SPEC-wave3-collaboration §6.6, HIGH-3): the qa reader now needs `actor` (a
# human finding buckets on it on the actor axis) and `withdrawn_at` (a retracted human
# finding is excluded from every bucket). Both are SCALARS (a short email, a timestamp),
# never a TEXT blob, so the F1 law is honoured — the query-shape test asserts these two
# are PRESENT and `note` is still ABSENT, so the widening is a decision on the record.
# WAVE 3 §6.6: the vendor scorecard needs the withdrawn exclusion (never the actor).
#: The blob columns a report reader must NEVER load (the query-shape lock asserts against these).
def human_findings_for_document(
    session: Session,
    *,
    document_id: int,
    target_locale: str | None = None,
    include_withdrawn: bool = False,
) -> list:
    """The ONE reader every reviewer-finding DISPLAY surface shares (LQA page web +
    GET /api/documents/{id}/lqa/findings + `lqa findings` CLI), so no surface hand-rolls
    the filter and they can never disagree (SPEC-wave3 §6.5). Returns whole `LQAResult`
    rows (finding_source='human') for ONE document, newest-first within a segment, ordered
    by segment then created_at.

    This is a DISPLAY read of a SINGLE, bounded document — the `note` IS loaded here
    because the page shows it — which is why it lives here and not among the F1-locked
    _QA_QUERY_COLS report projections (those fan out across every visible document and
    forbid the note blob). `target_locale` case-folds both sides (the score_document L5
    lesson). Withdrawn rows are excluded by default (§6.6.5); pass include_withdrawn to
    read them (nothing does today)."""
    from sqlalchemy import func as _f

    stmt = select(LQAResult).where(
        LQAResult.document_id == document_id, LQAResult.finding_source == "human"
    )
    if target_locale is not None:
        stmt = stmt.where(_f.lower(LQAResult.target_locale) == (target_locale or "").lower())
    if not include_withdrawn:
        stmt = stmt.where(LQAResult.withdrawn_at.is_(None))
    # LOW-3: a generous cap so one pathological document (thousands of hand-logged
    # findings) can never make this DISPLAY read unbounded; a real reviewer never
    # approaches it, and the page/CLI/api show a bounded list either way.
    return list(
        session.exec(stmt.order_by(LQAResult.segment_id, LQAResult.created_at).limit(5000)).all()
    )


_MACHINE_FINDINGS_DISPLAY_CAP = 500


# The scorecard READ display cap for stored MACHINE findings. A pathological document
# (thousands of persisted rows) can never make this DISPLAY read unbounded; the surface
# shows a bounded list with an honest "showing N of M" and the counts below are exact.
def machine_findings_for_document(
    session: Session, *, document_id: int, target_locale: str | None = None
) -> dict:
    """The stored MACHINE-scored LQA findings for ONE document+locale, for the scorecard
    READ surface (issue #52 point 2). The scorecard GET recomputes a fresh structural-only
    card and never reads machine rows into it; but the run that ACTUALLY GATED this document
    persisted its findings, and the qa report + Databricks export DO count them. Hiding them
    let the page show a clean 100 while the run-gate score was (say) 96 with real findings —
    reviewer-visible truth diverging from report truth for the same doc+locale.

    Returns BOTH persisted machine regimes, kept apart because they mean different things
    (§3.1.5): `run` rows (`segment_id IS NULL`) are workflow-STEP history — the gate reality,
    incl. cross-segment findings ad-hoc per-segment judging cannot represent; `adhoc` rows
    (`segment_id IS NOT NULL`) are the last "Re-score & save" segment-level scoring. HUMAN
    rows are excluded (they have their own reader/block). Rows are newest-first, capped for
    display; `run_total`/`adhoc_total` are the EXACT (uncapped) counts so the surface can say
    "showing N of M" honestly. This reader is the FINDINGS LIST for the read-only display
    block; the AUTHORITATIVE headline (score/gate) comes from `lqa_scorecard_headline`."""
    from sqlalchemy import func as _f

    base = [
        LQAResult.document_id == document_id,
        # A machine finding is anything not hand-logged: 'llm'/'structural'/'rulebook'
        # or a legacy NULL row. `finding_source != 'human'` misses NULLs (SQL 3-valued
        # logic), so spell out the NULL arm.
        or_(LQAResult.finding_source.is_(None), LQAResult.finding_source != "human"),
    ]
    if target_locale is not None:
        base.append(_f.lower(LQAResult.target_locale) == (target_locale or "").lower())

    def _q(regime_pred):
        return select(LQAResult).where(*base, regime_pred)

    run_pred = LQAResult.segment_id.is_(None)
    adhoc_pred = LQAResult.segment_id.is_not(None)
    run_total = session.exec(select(_f.count()).select_from(LQAResult).where(*base, run_pred)).one()
    adhoc_total = session.exec(
        select(_f.count()).select_from(LQAResult).where(*base, adhoc_pred)
    ).one()
    run_rows = list(
        session.exec(
            _q(run_pred).order_by(LQAResult.created_at.desc()).limit(_MACHINE_FINDINGS_DISPLAY_CAP)
        ).all()
    )
    adhoc_rows = list(
        session.exec(
            _q(adhoc_pred)
            .order_by(LQAResult.segment_id, LQAResult.created_at)
            .limit(_MACHINE_FINDINGS_DISPLAY_CAP)
        ).all()
    )
    return {
        "run": run_rows,
        "adhoc": adhoc_rows,
        "run_total": int(run_total or 0),
        "adhoc_total": int(adhoc_total or 0),
        "cap": _MACHINE_FINDINGS_DISPLAY_CAP,
    }


def lqa_scorecard_headline(
    session: Session, *, document_id: int, target_locale: str | None = None
) -> dict:
    """The ONE authoritative LQA headline the scorecard GET and `lqa findings --machine`
    both render (issue #52 F3, gate round 2) — so the web page and the CLI can never derive
    a different verdict. Returns {has_scoring, regime, score, gate}:

      * REGIME 'run' — the LATEST workflow LQA StepRun for this doc+locale. Its `gate` is
        the DURABLE, THRESHOLD-CORRECT pass/fail the orchestrator computed against the step's
        OWN lqa_threshold (a run that FAILED at threshold 99 with score 95 reads needs_review,
        NOT the auto_approved a hardcoded-90 re-derivation would invent — F3a). The LATEST
        StepRun wins (order by finished_at, id desc), NEVER min-across-history, so a stale
        40/needs_review row cannot pin the headline after clean re-scores (F3b). gate maps
        'passed'->auto_approved, 'failed'->needs_review.
      * REGIME 'adhoc' — only when NO workflow LQA StepRun exists: the last "Re-score & save"
        segment-level scoring. `score` is that scoring's worst segment (the delete-before-
        insert in score_document keeps only ONE ad-hoc generation per doc+locale, so the
        current rows ARE the latest scoring — again never min-across-history). `gate` is None:
        an ad-hoc per-segment scoring has no single durable threshold/gate, so the surface
        shows the SCORE ONLY, honestly labelled.
      * NEITHER — has_scoring False: no workflow LQA scoring and no ad-hoc findings on record
        (never scored, or a clean scoring that left no findings — not cheaply distinguishable,
        so the surface uses one honest phrase for both)."""
    from sqlalchemy import func as _f

    loc = (target_locale or "").lower()
    sr_q = (
        select(StepRun)
        .join(WorkflowRun, StepRun.workflow_run_id == WorkflowRun.id)
        .join(WorkflowStep, StepRun.step_id == WorkflowStep.id)
        .where(
            WorkflowRun.document_id == document_id,
            WorkflowStep.step_type == StepType.LQA,
            StepRun.gate.is_not(None),
        )
    )
    if target_locale is not None:
        sr_q = sr_q.where(_f.lower(WorkflowRun.target_locale) == loc)
    sr = session.exec(sr_q.order_by(StepRun.finished_at.desc(), StepRun.id.desc())).first()
    if sr is not None:
        return {
            "has_scoring": True,
            "regime": "run",
            "score": None if sr.score is None else int(round(sr.score)),  # noqa: RUF046
            "gate": "auto_approved" if sr.gate == "passed" else "needs_review",
        }
    # No workflow LQA gate on record -> fall back to the last ad-hoc scoring, SCORE ONLY.
    adhoc_base = [
        LQAResult.document_id == document_id,
        LQAResult.segment_id.is_not(None),
        or_(LQAResult.finding_source.is_(None), LQAResult.finding_source != "human"),
    ]
    if target_locale is not None:
        adhoc_base.append(_f.lower(LQAResult.target_locale) == loc)
    adhoc_ct = session.exec(select(_f.count()).select_from(LQAResult).where(*adhoc_base)).one()
    if int(adhoc_ct or 0) > 0:
        adhoc_min = session.exec(select(_f.min(LQAResult.score)).where(*adhoc_base)).one()
        return {
            "has_scoring": True,
            "regime": "adhoc",
            "score": None if adhoc_min is None else int(round(adhoc_min)),  # noqa: RUF046
            "gate": None,
        }
    return {"has_scoring": False, "regime": None, "score": None, "gate": None}


def _dt_lo(d):
    """Inclusive lower datetime bound for a date filter pushed into SQL (day start)."""
    return datetime.combine(d, time.min) if d is not None else None
