"""reports: part3

Documented in: docs/architecture/reporting-and-dashboard.md
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.services.reports_csv import ReportQuery, _degrade_caveat, _fetch_scoped_runs, _zero_metrics


def _workflow_run_buckets(
    session: Session, q: ReportQuery, project_ids: set, provider_filter: str | None
) -> dict:
    """The `--group-by workflow` axis, computed from REAL per-run spend.

    EXACT SEMANTICS (feat/workflow-spend, replacing the old most-recent-WorkflowRun
    job-cost APPROXIMATION): a workflow bucket's cost/tokens is the SUM of the stamped
    StepRun costs of the WorkflowRuns for that workflow whose run-date (finished_at,
    else started_at — the job-date convention) falls in the window. This is the same
    spend the ledger accrued for those runs (StepRun.cost_usd comes from
    run_completion's exact ledger figure), so the axis is authoritative, not a proxy.

    JOBS vs RUNS — NO DOUBLE-COUNT: `workflow run` writes WorkflowRun + StepRun rows
    and NEVER a Job row (only the plain-MT `translate_document` and `connector_translate`
    kinds write Jobs — orchestrator.run_workflow / cmd_workflow enqueue nothing). So the
    two axes are DISJOINT by construction: run spend lives only in StepRuns (→ workflow
    buckets here), job spend only in Job.result (→ the `(no workflow)` bucket in the
    caller's job loop). A cost can never feed both a workflow row and a job row.

    LEGACY HONESTY: a run whose StepRuns are ALL cost_usd IS NULL (created before this
    feature) is UNTRACKED — its real spend is genuinely unknown. Such runs are bucketed
    into a separate `(untracked)` row (counted, but cost/tokens 0 and clearly labelled),
    NEVER silently zeroed into a workflow's real total and NEVER re-approximated.

    The count column ("jobs") counts RUNS on the workflow rows + `(untracked)` row (the
    unit of this axis); the caller's `(no workflow)` row counts plain-MT jobs. A
    `provider` filter, if set, sums only StepRuns whose provider_used matches BUT the run
    is STILL COUNTED even with no matching step (F4: this axis deliberately lists every
    run of a workflow; the non-workflow axes instead DROP a run with no matching step, to
    mirror the job loop — so a provider-filtered report's run count can legitimately
    differ between the workflow axis and the others)."""
    from app.models import Workflow

    buckets: dict[str, dict] = {}
    runs, srs_by_run, _doc_to_proj = _fetch_scoped_runs(session, q, project_ids)
    if not runs:
        return buckets
    run_ids = [r.id for r in runs]
    wf_ids = {r.workflow_id for r in runs}
    wf_name = (
        {  # noqa: C416
            wid: name
            for wid, name in session.exec(
                select(Workflow.id, Workflow.name).where(Workflow.id.in_(wf_ids))
            ).all()
        }
        if wf_ids
        else {}
    )
    # Wave C (decision 5): HUMAN spend per run from its vendor dispatch legs (actual when
    # reconciled, else estimate). Additive alongside the AI (StepRun) cost. Independent of
    # the `provider` filter -- that filter is an AI-provider concept and a vendor leg has no
    # provider; a provider-filtered cost report still reports the full human spend of the
    # runs it counts.
    from app.services import vendor_spend

    human_by_run = vendor_spend.spend_by_run(session, run_ids)
    for run in runs:
        srs = srs_by_run.get(run.id, [])
        tracked = any(sr.cost_usd is not None for sr in srs)
        key = (
            f"{run.workflow_id}: {wf_name.get(run.workflow_id, '?')}" if tracked else "(untracked)"
        )
        row = buckets.setdefault(key, _zero_metrics())
        row["jobs"] += 1  # counts RUNS on this axis (see docstring)
        row["human_cost_usd"] += (human_by_run.get(run.id, {}) or {}).get("human_spend_usd", 0.0)
        if tracked:
            for sr in srs:
                if provider_filter is not None and (sr.provider_used or "") != provider_filter:
                    continue
                if sr.cost_usd is not None:
                    row["cost_usd"] += sr.cost_usd
                if sr.tokens_in is not None:
                    row["tokens_in"] += sr.tokens_in
                if sr.tokens_out is not None:
                    row["tokens_out"] += sr.tokens_out
    return buckets


def _caveats(q: ReportQuery, buckets: dict | None = None) -> list:
    """Reconciliation caveats for this query, computed ONCE here so every surface (web
    caption, CSV footer, /api + CLI JSON) states the same thing (D4). Empty for a plain,
    unfiltered non-workflow report — so a clean report gains no extra CSV rows.

    Three conditions carry a caveat:
      * a `provider` filter — a workflow run is counted only when it has a step on that
        provider (job-loop-consistent) on the non-workflow axes, while the workflow axis
        lists every run of a workflow; run counts can legitimately differ between axes
        (F4).
      * the workflow axis — its segments/words/chars (volume) columns reflect plain-MT
        jobs only; per-run volume is not attributed on this axis, so the volume TOTAL is
        NOT axis-invariant here the way it is across the six non-workflow axes (D3).
      * a `(none)` row on the provider/engine axis (issue #96) — a step that made NO
        provider call has no engine to attribute to and buckets under `(none)`. The
        report already explains `(untracked)`; `(none)` was the one unlabelled bucket an
        operator could meet, and it was the bucket the (then-unattributed) LQA judge's
        tokens landed in. Emitted ONLY when that row is actually on screen — `buckets` is
        the finished bucket dict — so a report with no such row still gains no CSV rows.

    `buckets` is optional so the seam stays callable from a caveats-only context; when it
    is omitted the `(none)` caveat simply cannot be computed (never guessed)."""
    out = []
    if q.group_by in ("provider", "engine") and "(none)" in (buckets or {}):
        out.append(
            "'(none)' row: a step that made no provider call (a human review, an "
            "orchestration-only step such as tm_match/external, or a skipped visit) has "
            "no engine to attribute to and buckets here. It is distinct from "
            "'(untracked)', which is about spend tracking rather than which engine ran."
        )
    if q.provider:
        if q.group_by == "workflow":
            out.append(
                "provider filter: this workflow axis counts every run of a workflow "
                "(summing only the matching-provider steps' spend); the other groupings "
                "count only runs that used the filtered provider, so a run count can "
                "differ between the workflow axis and the others."
            )
        else:
            out.append(
                "provider filter: a workflow run is counted only when it has at least "
                "one step on the filtered provider (matching how plain jobs are "
                "filtered); group by workflow to see every run of a workflow."
            )
    if q.group_by == "workflow":
        out.append(
            "workflow axis: the segments/words/chars (volume) columns reflect plain-MT "
            "jobs only; per-run volume is not attributed on this axis."
        )
        # LOW-5: the two facts the deleted prose block carried, restored into the single
        # _caveats() source so the workflow axis still explains its two special rows.
        out.append(
            "workflow axis: plain machine-translation and connector jobs carry no workflow "
            "and roll up as '(no workflow)'; a run finished before per-step spend tracking "
            "has no recorded cost and appears as '(untracked)' rather than a false zero."
        )
    if q.group_by == "locale":
        out.append(
            "locale axis: cost/tokens attribute to a job's target locale (payload.locale) "
            "and a run's target locale; a job/segment with no locale buckets as "
            "'(untracked locale)', and a run with no recorded per-step cost (legacy or "
            "pending) is counted in a separate '(untracked)' row rather than a false $0."
        )
        out.append(
            "locale axis: segment volume (segments/words/chars) is included for a locale "
            "only when its project had a job or run in the window (the same rule the other "
            "axes use); a project with segments but no in-window job/run contributes no "
            "volume."
        )
    return _degrade_caveat(q) + out


def _qa_caveats(q: ReportQuery, meta: dict) -> list:
    out = _degrade_caveat(q)
    if not meta.get("any"):
        return out
    # Denominator honesty (§4.2): a clean card writes zero rows, so `cards` counts cards
    # WITH findings, never cards scored, and avg_card_score is findings-biased low.
    out.append(
        "cards counts LQA cards that HAVE findings (a clean, zero-finding card writes no "
        "rows), so it is not a count of cards scored, and avg_card_score is biased low."
    )
    # E2 (Errata): the surviving accumulated workflow history drifts counts UP.
    out.append(
        "LQA counts include accumulated workflow-step findings that survive a re-score, "
        "so a count can rise over time with no new scoring (the dashboard histogram reads "
        "the same accumulated history)."
    )
    if meta.get("has_null_scoring"):
        out.append(
            "some findings predate Wave 2 (no scoring_id): they are counted in findings "
            "but excluded from cards and avg_card_score, never silently folded."
        )
    if meta.get("has_human"):
        # WAVE 3 §6.6.3: reviewer-authored findings are real findings but not scored
        # cards, and a WITHDRAWN one is already excluded above; the caveat + the withdraw/
        # lakehouse residual (L5) are also on app/capabilities/reports.md and docs/databricks.md.
        out.append(
            "%d finding(s) in this report were logged by hand by a reviewer. They are "  # noqa: UP031
            "counted in findings and in the severity columns (and in the `human` column), "
            "are excluded from cards and avg_card_score (a hand-logged bug is not a scored "
            "card), and never changed any workflow gate. A reviewer can WITHDRAW a finding; "
            "a withdrawn finding is excluded here but is RETAINED in the append-only "
            "Databricks lqa_findings stream, so a lakehouse query and this report will "
            "differ by exactly the withdrawn rows." % meta.get("human_count", 0)
        )
    if meta.get("mixed_regime"):
        # rule 6 (N8 + N2): the one caveat that states all three regime facts.
        out.append(
            "mixed LQA regimes in a bucket: findings mixes current-state rows (an ad-hoc "
            "re-score REPLACES a document+locale's segment-level findings) with "
            "accumulated workflow history (APPENDed per step visit); cards mixes two "
            "granularities (one card per segment vs one card per step visit over the whole "
            "document), so cards and avg_card_score compare unlike things; and a re-score "
            "DESTROYS that document+locale's prior findings, so a count can legitimately "
            "DROP between two runs of the same report with no deletion visible anywhere."
        )
    if meta.get("provider"):
        out.append(
            "provider filter does not apply to the qa kind (findings are not attributed "
            "to an AI provider); the filter is ignored."
        )
    if meta.get("docless"):
        out.append(
            "some findings carry no document (unattributable to any project/customer) and "
            "are shown under the '(untracked ...)' buckets; they appear only in this "
            "install-wide operator view, never in a scoped or filtered report."
        )
    return out


def _postedit_caveats(q: ReportQuery, meta: dict) -> list:
    out = _degrade_caveat(q)
    if not meta.get("any"):
        return out
    out.append(
        "tte_ms is the segment's CUMULATIVE edit time over its whole life (not just the "
        "edits inside the window); it and words_derived are per-segment figures."
    )
    if meta.get("multi"):
        out.append(
            "actor/vendor axis: distances are EPISODE-LOCAL (each actor's own baseline -> "
            "result), so the per-actor sum does NOT equal the cumulative machine->final "
            "distance the other axes report, and a segment edited by two actors is counted "
            "in segments_edited of BOTH rows — this axis deliberately does not add up to "
            "the project total. words_derived, tte_ms and avg_tte_ms are omitted here "
            "(they are per-segment and cannot be split across actors)."
        )
    if meta.get("has_none_baseline"):
        out.append(
            "an edit that started from an empty target (no baseline) is counted in "
            "episodes/edits but contributes no distance and is excluded from "
            "segments_edited — the exclusion is deliberate, not a lost row."
        )
    if meta.get("no_machine_origin"):
        # HIGH-1: keyed to the ACTUAL cumulative-exclusion predicate (cum is None), so a
        # human-origin (or otherwise non-mt/tm-origin) segment is disclosed, not silently
        # zeroed. Fires only on the cumulative axes, where cum is the measure.
        out.append(
            "%d segment(s) have no machine (mt/tm) origin, so a cumulative machine->final "  # noqa: UP031
            "distance is not computable for them; they contribute to episodes/edits but "
            "are excluded from segments_edited and the distance columns."
            % meta["no_machine_origin"]
        )
    if meta.get("mixed_unit"):
        out.append(
            "mixed unit kinds in a bucket: a full-text paragraph edit measures its "
            "distance over the ENTIRE body, so its percentage is not comparable to a "
            "segment's — the two are aggregated but not equivalent."
        )
    if meta.get("mixed_metric"):
        out.append(
            "mixed metric versions in a bucket: distances computed under different "
            "tokenizers/versions are on different scales and are not strictly comparable."
        )
    if meta.get("provider"):
        out.append(
            "provider filter does not apply to the postedit kind (edits are not "
            "attributed to an AI provider); the filter is ignored."
        )
    return out
