"""Reports (R2-1 + Wave 2) - CSV cost / token / volume / qa / postedit reports from
EXISTING data.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _QA_QUERY_COLS, _VENDOR_LQA_COLS, _PE_QUERY_COLS,
_REPORT_FORBIDDEN_BLOB_COLS, human_findings_for_document, _MACHINE_FINDINGS_DISPLAY_CAP,
machine_findings_for_document, lqa_scorecard_headline, _dt_lo, _dt_hi, KINDS, UnknownReportKind,
validate_kind, _KIND_COLUMNS, _QA_SEVERITIES, _POSTEDIT_COLUMNS_MULTI, _NON_SUMMABLE_COLUMNS,
GROUP_BYS, _STD_AXES, _SUPPORTED_GROUP_BYS, _supported_axis, _model_for_provider, _job_date,
_dominant_provider, _job_cost, _WORD_RE, _derived_words, allowed_project_ids,
_effective_customer_filter, _job_document_id, _run_date, _run_group_key, _dominant_step_provider,
_group_key, _axis_key, _key_column, _VENDOR_QUALITY_COLS, _VENDOR_METRIC_KEYS, _VENDOR_COST_COLS,
_zero_vendor_metrics, _vendor_caveats, can_see_cost, can_see_actor_axis, _can_see_human_spend,
_int_or_none, _document_project_id, _segment_metrics, _doc_to_proj, _pe_zero, _round_total,
_csv_cell, _FILENAME_SAFE, csv_filename, parse_date, _METRIC_KEYS, _FLOAT_METRIC_KEYS, ReportQuery,
_in_range, _fetch_scoped_runs, _zero_metrics, _vendor_buckets, _finish_vendor, _degrade_caveat,
_job_project_id, _segment_metrics_by_locale, _qa_zero, _qa_buckets, _postedit_buckets, to_csv,
totals_row, kinds_catalog, _workflow_run_buckets, _caveats, _qa_caveats, _postedit_caveats from
app.services.reports_the_query, app.services.reports_csv, app.services.reports_part3.

Wave 2 (SPEC-wave2-qa-pe-reporting) adds two quality/effort report kinds — `qa` (MQM
LQA findings: dimension x severity, from LQAResult) and `postedit` (edit distance +
effort + volume, from SegmentPostEdit) — plus two axes, `dimension` and `actor`. Both
new kinds are dispatched by ISOLATED paths (their columns differ from the job-loop
kinds), scoped by the SAME confined project set, and are byte-identical across
web + /api + CLI (+ MCP, read-shaped). See _finish_qa / _finish_postedit below.

The original request: "generate and download a whole assortment of
reports... Financial cost reports, number of words translated, input/output tokens,
per project, per customer, etc." This module is the ONE source of truth the web
(/reports), the in-process CLI (`report run`) and the remote /api all call, so the
same query yields byte-identical numbers on every surface (the repo parity law).

WHAT IT AGGREGATES: two DISJOINT spend sources, unioned so no install reads a false
zero.
  * every finished translation Job. A Job.result carries the translate._new_stats
    shape (translated / from_*_tm / provider_used / tokens_in / tokens_out /
    tokens_cache_read / tokens_cache_write); the Job's owning project comes from
    payload.project_id -> Project -> Customer/Org.
  * every workflow run's per-step spend (StepRun.cost_usd / tokens_in / tokens_out),
    keyed to the run's document -> project. This is what makes a WORKFLOW-DRIVEN
    install's cost/token reports non-empty: a `workflow run` writes WorkflowRun +
    StepRun rows and NEVER a Job (the only Job-writing kinds are translate_document /
    connector_translate), so the two sources are disjoint BY CONSTRUCTION and their
    union double-counts nothing — a given provider call is recorded in exactly one of
    the two. (Before F2-2 was fixed, StepRun spend fed ONLY the group_by=workflow
    axis, so a workflow-only install saw 0 tokens / 0 cost on every other axis,
    including the default group_by=none the /api/reports/run endpoint hits.)
Segments (source/target text) give segment counts, char counts and DERIVED word counts
(see below); segment volume folds in once per project whether that project was
translated via a Job, a workflow run, or both.

COST (reconciles with the spend cap): we reuse pricing.cost_usd(provider, model,
usage) VERBATIM - the exact function the ledger (spend.record_usage) bills on - so a
report's cost column reconciles with what actually accrued. The Job.result does NOT
stamp a model (translate._new_stats omits it), so we resolve the model from the job's
provider via config.DEFAULT_MODELS - the SAME map spec_for_project/spec_for_provider
used at translation time, which is exactly what was billed for the common
single-provider job. A job whose result carries explicit `chars`+`nmt_kind` (an NMT /
char-billed unit) is priced through the char path of cost_usd unchanged. Free engines
(mock, the `local` slot, and an `engine:<id>` that is a self-hosted `local` kind on a
private endpoint) contribute 0 through spend.cost_for_ref, the ledger's own free-engine rule
(#361), so offline/self-hosted jobs match the cap; a deleted engine prices conservatively.

DERIVED WORDS: v1 has NO ingest-time stamped words-translated counter (deferred - it
needs ingest-time stamping). Instead we DERIVE a word count from the source text of
TRANSLATED segments (whitespace token count) and label the column "words_derived" so
it is never mistaken for an authoritative billed word count.

THE CRITICAL CONSTRAINT - scoping (this makes it review-worthy). Reports AGGREGATE
across projects, so a scoping miss LEAKS cost/volume across tenants or customers.
run_report layers, IN ORDER, exactly the seams R2/R2-3 already established - it does
NOT hand-roll a new filter:
  1. Org scope (multi-org gate) + 2. project visibility + 3. R2-3 customer-bound
     confinement are ALL baked into access.visible_project_ids(session, user): that
     one call returns the confined project-id set (org-filtered, membership-filtered,
     and - for a customer-bound user - exactly their customer's projects). We scope
     every job to a Job whose project is in that set.
  * A trusted in-process operator (user=None) is unrestricted BY visibility, but an
    explicit org_id (the CLI's resolved --org under the gate) still hard-filters to
    that org's projects so a shared-host operator can't sweep every org unscoped.
  * A `customer_id` FILTER is CLAMPED to a bound user's own customer: a bound user
    asking for a foreign customer gets their-own/empty, never the foreign data.

All helpers are pure/testable: run_report takes a resolved user + org_id (no Request),
exactly like customers.get_customer_360.

Documented in: docs/architecture/reporting-and-dashboard.md
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import or_
from sqlmodel import Session, func, select

from app import config
from app.models import (
    Customer,
    Document,
    Job,
    LQAResult,
    Project,
    SegmentPostEdit,
    StepRun,
    StepType,
    WorkflowRun,
    WorkflowStep,
)
from app.services import access, pricing
from app.services.maintenance import admitted_work
from app.services.reports_csv import (
    _FLOAT_METRIC_KEYS,
    _METRIC_KEYS,
    ReportQuery,
    _degrade_caveat,
    _fetch_scoped_runs,
    _finish_vendor,
    _in_range,
    _job_project_id,
    _postedit_buckets,
    _qa_buckets,
    _qa_zero,
    _segment_metrics_by_locale,
    _vendor_buckets,
    _zero_metrics,
    kinds_catalog,
    to_csv,
    totals_row,
)
from app.services.reports_part3 import (
    _caveats,
    _postedit_caveats,
    _qa_caveats,
    _workflow_run_buckets,
)
from app.services.reports_the_query import (
    _FILENAME_SAFE,
    _KIND_COLUMNS,
    _MACHINE_FINDINGS_DISPLAY_CAP,
    _NON_SUMMABLE_COLUMNS,
    _PE_QUERY_COLS,
    _POSTEDIT_COLUMNS_MULTI,
    _QA_QUERY_COLS,
    _QA_SEVERITIES,
    _REPORT_FORBIDDEN_BLOB_COLS,
    _STD_AXES,
    _SUPPORTED_GROUP_BYS,
    _VENDOR_COST_COLS,
    _VENDOR_LQA_COLS,
    _VENDOR_METRIC_KEYS,
    _VENDOR_QUALITY_COLS,
    _WORD_RE,
    GROUP_BYS,
    KINDS,
    UnknownReportKind,
    _axis_key,
    _can_see_human_spend,
    _csv_cell,
    _derived_words,
    _doc_to_proj,
    _document_project_id,
    _dominant_provider,
    _dominant_step_provider,
    _dt_hi,
    _dt_lo,
    _effective_customer_filter,
    _group_key,
    _int_or_none,
    _job_cost,
    _job_date,
    _job_document_id,
    _key_column,
    _model_for_provider,
    _pe_zero,
    _round_total,
    _run_date,
    _run_group_key,
    _segment_metrics,
    _supported_axis,
    _vendor_caveats,
    _zero_vendor_metrics,
    allowed_project_ids,
    can_see_actor_axis,
    can_see_cost,
    csv_filename,
    human_findings_for_document,
    lqa_scorecard_headline,
    machine_findings_for_document,
    parse_date,
    validate_kind,
)

__all__ = [
    "GROUP_BYS",
    "KINDS",
    "_FILENAME_SAFE",
    "_FLOAT_METRIC_KEYS",
    "_KIND_COLUMNS",
    "_MACHINE_FINDINGS_DISPLAY_CAP",
    "_METRIC_KEYS",
    "_NON_SUMMABLE_COLUMNS",
    "_PE_QUERY_COLS",
    "_POSTEDIT_COLUMNS_MULTI",
    "_QA_QUERY_COLS",
    "_QA_SEVERITIES",
    "_REPORT_FORBIDDEN_BLOB_COLS",
    "_STD_AXES",
    "_SUPPORTED_GROUP_BYS",
    "_VENDOR_COST_COLS",
    "_VENDOR_LQA_COLS",
    "_VENDOR_METRIC_KEYS",
    "_VENDOR_QUALITY_COLS",
    "_WORD_RE",
    "Document",
    "LQAResult",
    "ReportQuery",
    "SegmentPostEdit",
    "StepRun",
    "StepType",
    "UnknownReportKind",
    "WorkflowRun",
    "WorkflowStep",
    "_axis_key",
    "_can_see_human_spend",
    "_caveats",
    "_csv_cell",
    "_degrade_caveat",
    "_derived_words",
    "_doc_to_proj",
    "_document_project_id",
    "_dominant_provider",
    "_dominant_step_provider",
    "_dt_hi",
    "_dt_lo",
    "_effective_customer_filter",
    "_fetch_scoped_runs",
    "_finish_vendor",
    "_group_key",
    "_in_range",
    "_int_or_none",
    "_job_cost",
    "_job_date",
    "_job_document_id",
    "_job_project_id",
    "_key_column",
    "_model_for_provider",
    "_pe_zero",
    "_postedit_buckets",
    "_postedit_caveats",
    "_qa_buckets",
    "_qa_caveats",
    "_qa_zero",
    "_round_total",
    "_run_date",
    "_run_group_key",
    "_segment_metrics",
    "_segment_metrics_by_locale",
    "_supported_axis",
    "_vendor_buckets",
    "_vendor_caveats",
    "_workflow_run_buckets",
    "_zero_metrics",
    "_zero_vendor_metrics",
    "access",
    "allowed_project_ids",
    "can_see_actor_axis",
    "can_see_cost",
    "config",
    "csv",
    "csv_filename",
    "dataclass",
    "date",
    "human_findings_for_document",
    "io",
    "kinds_catalog",
    "lqa_scorecard_headline",
    "machine_findings_for_document",
    "or_",
    "parse_date",
    "pricing",
    "re",
    "to_csv",
    "totals_row",
    "validate_kind",
]


def _run_spend_buckets(
    session: Session,
    q: ReportQuery,
    projects: dict,
    proj_ids: set,
    cust_name_by_id: dict,
    provider_filter: str | None,
    seg_by_project: dict,
    folded_projects: set,
) -> dict:
    """Workflow-run spend (StepRun tokens + cost) bucketed by the ACTIVE NON-workflow
    axis (F2-2). Without this, a workflow-driven install's token/cost spend is invisible
    on the none / project / customer / provider / engine / date reports — the very
    finding this fixes (the /api/reports/run?kind=tokens default is group_by=none, which
    read 0/0 while StepRuns held real spend). The group_by=workflow axis keeps its own
    exact path (_workflow_run_buckets); this handles every other axis.

    SPEND + RUN-COUNT routing:
      * a TRACKED run (≥1 StepRun with a non-NULL cost) contributes its (filter-passing)
        StepRun tokens + cost:
          - none/project/customer/date — summed into the one axis-key bucket, and the
            run counted there;
          - provider/engine — EXACT PER-STEPRUN (D2): each step's cost/tokens go to ITS
            OWN provider bucket (StepRun holds the true per-provider figures, unlike a
            Job.result — so a $10 claude step is never lumped onto a cheaper, more
            frequent provider), and the run is COUNTED ONCE on its dominant-provider row
            (a non-dominant provider row can therefore carry real spend with jobs=0 —
            deliberate: spend is exact, the count is one-per-run).
      * an UNTRACKED run (all StepRun costs NULL — legacy pre-tracking, or a pending/
        never-executed run) is COUNTED in a separate "(untracked)" row on every axis with
        0 cost/tokens (F3/F6), NEVER a silent $0.00 in a real bucket (that would violate
        the never-zero-for-NULL StepRun contract and understate a tenant's real total).

    VOLUME FOLD (D1) is DECOUPLED from the spend/count bucket: a run-only project's
    segment/word/char volume folds ONCE, on every axis, into a REAL project-anchored
    bucket — the axis key for none/project/customer/date, the dominant provider for
    provider/engine — NEVER into "(untracked)". This matters because the fold happens on
    the lowest-id contributing run, which on any install carrying legacy runs is an
    UNTRACKED run; routing volume by the spend bucket would park a real project's words
    in "(untracked)" (a false zero on the real row — the common prod case). So an
    untracked-run-only project shows its VOLUME on its real row and its run COUNT in
    "(untracked)"; the two are intentionally separate (spend honesty vs volume
    attribution). The install-wide volume TOTAL is axis-invariant across all six
    non-workflow axes; per-bucket splits on provider/engine/date stay "indicative, not
    authoritative" exactly as the job loop documents.

    NO DOUBLE-COUNT: runs and jobs are disjoint (a `workflow run` writes WorkflowRun +
    StepRun rows, never a Job), so this overlay never re-counts a provider call already
    summed in the caller's job loop. A mixed project translated BOTH by a plain-MT job
    and by a workflow run reports the SUM of the two — genuinely separate spend events.

    A `provider` filter sums only matching StepRuns and DROPS a run with no matching step
    (mirroring the job loop's provider skip). The workflow axis instead lists every run
    regardless — the deliberate, documented divergence (F4; surfaced in the /reports
    caption, the CSV footer, and the /api caveats field).

    SCOPING: runs are fetched only for documents of the already-scoped `proj_ids` (org +
    membership + R2-3 customer confinement + explicit project/customer filters are all
    baked into that set by run_report), so a scoped report can never sum a foreign
    tenant's StepRuns."""
    buckets: dict[str, dict] = {}
    runs, srs_by_run, doc_to_proj = _fetch_scoped_runs(session, q, proj_ids)
    if not runs:
        return buckets
    gb = q.group_by
    prov_axis = gb in ("provider", "engine")
    # Deterministic run order (by id) so a run-only project's volume folds into a stable
    # bucket (the first contributing run) on the provider/engine/date axes.
    for run in sorted(runs, key=lambda r: r.id):
        pid = doc_to_proj.get(run.document_id)
        proj = projects.get(pid) if pid is not None else None
        srs = srs_by_run.get(run.id, [])
        matching = (
            [sr for sr in srs if (sr.provider_used or "") == provider_filter]
            if provider_filter is not None
            else srs
        )
        if provider_filter is not None and not matching:
            continue  # no matching step -> drop, like the job loop's provider skip (F4)
        tracked = any(sr.cost_usd is not None for sr in srs)
        # --- spend + run-count routing ---
        if not tracked:
            buckets.setdefault("(untracked)", _zero_metrics())["jobs"] += 1
        elif prov_axis:
            # D2: EXACT per-StepRun spend to each provider's own bucket; count once on
            # the dominant provider row.
            for sr in matching:
                b = buckets.setdefault(sr.provider_used or "(none)", _zero_metrics())
                if sr.cost_usd is not None:
                    b["cost_usd"] += sr.cost_usd
                if sr.tokens_in is not None:
                    b["tokens_in"] += sr.tokens_in
                if sr.tokens_out is not None:
                    b["tokens_out"] += sr.tokens_out
            buckets.setdefault(_dominant_step_provider(matching), _zero_metrics())["jobs"] += 1
        else:
            row = buckets.setdefault(_run_group_key(q, run, proj, cust_name_by_id), _zero_metrics())
            row["jobs"] += 1
            for sr in matching:
                if sr.cost_usd is not None:
                    row["cost_usd"] += sr.cost_usd
                if sr.tokens_in is not None:
                    row["tokens_in"] += sr.tokens_in
                if sr.tokens_out is not None:
                    row["tokens_out"] += sr.tokens_out
        # --- volume fold (D1): decoupled from the spend bucket; NEVER "(untracked)" ---
        if pid is not None and pid not in folded_projects:
            folded_projects.add(pid)
            m = seg_by_project.get(pid)
            if m:
                fold_key = (
                    _dominant_step_provider(matching)
                    if prov_axis
                    else _run_group_key(q, run, proj, cust_name_by_id)
                )
                frow = buckets.setdefault(fold_key, _zero_metrics())
                frow["segments"] += m["segments"]
                frow["chars"] += m["chars"]
                frow["words_derived"] += m["words_derived"]
    return buckets


@admitted_work(operation="report")
def run_report(session: Session, q: ReportQuery, *, user=None, org_id=None) -> dict:
    """Run a report. Returns {"columns": [...], "rows": [ {col: val}, ... ],
    "key_column": <name>, "kind": <kind>, "group_by": <gb>}.

    `user` is the resolved caller (a User, or None for the trusted in-process
    operator). `org_id` is an explicit org scope (the CLI's resolved --org under the
    gate); None off the gate / for the web+api paths (whose org confinement rides on
    `user` through visible_project_ids). ALL three scoping rings are applied via
    allowed_project_ids; the customer filter is clamped for a bound user.

    Workflow-run spend (StepRun tokens/cost) is unioned onto the job buckets on EVERY
    axis, so a workflow-driven install never reads a false 0 (F2-2):
      * group_by="workflow" is EXACT (feat/workflow-spend): the workflow rows are the
        SUM of real stamped StepRun costs of the WorkflowRuns in the window (the same
        spend the ledger accrued), plain-MT/connector jobs bucket as "(no workflow)",
        and legacy runs whose StepRuns predate spend-tracking (all-NULL cost) surface
        honestly as a separate "(untracked)" row -- never silently zeroed. See
        _workflow_run_buckets.
      * every OTHER axis (none/project/customer/provider/engine/date): StepRun spend is
        bucketed by that axis's key via _run_spend_buckets, so kind=tokens/cost is
        correct for a workflow-only install (the default group_by=none previously read
        0/0).
    Runs and jobs never double-count on any axis: a `workflow run` writes StepRuns,
    never a Job, so a provider call is recorded in exactly one source."""
    q = q.normalized()
    allowed = allowed_project_ids(session, user, org_id)  # None = unrestricted
    cust_filter = _effective_customer_filter(user, q.customer_id)

    # Projects in scope (id -> Project), so a job's project is a cheap dict hit and
    # customer/org grouping needs no per-job query. Apply the id allow-list + the
    # explicit project/customer filters here so out-of-scope jobs never load.
    pstmt = select(Project)
    if allowed is not None:
        if not allowed:
            # Empty allow-list (a restricted user with no visible projects, or a
            # bound user whose customer has none): no data, but still return the
            # shaped columns + an empty body (+ TOTALS) so the surface renders. M1:
            # the shape MUST match the axis (vendor scorecard vs job columns) even at
            # zero scope, so a zero-visibility vendor report never leaks the job/cost
            # column set.
            return _empty_report(q, user)
        pstmt = pstmt.where(Project.id.in_(allowed))
    if q.project_id is not None:
        pstmt = pstmt.where(Project.id == q.project_id)
    if cust_filter is not None:
        pstmt = pstmt.where(Project.customer_id == cust_filter)
    projects = {p.id: p for p in session.exec(pstmt).all()}
    if not projects:
        return _empty_report(q, user)
    proj_ids = set(projects.keys())

    # Wave 2 (SPEC-wave2 §4.1) — the KIND arms dispatch BEFORE the axis arms. The two
    # quality kinds read their OWN tables (LQAResult / SegmentPostEdit) with their own
    # columns, so they must intercept before the locale/vendor axis arms below (and the
    # identical ordering is mirrored in _empty_report so a zero-scope `--kind qa
    # --group-by vendor` returns the qa shape, not the vendor scorecard — M1).
    if q.kind == "qa":
        # MED-5 (gate fix): the doc-less arm is safe ONLY for a caller whose report spans
        # every tenant AND is not narrowed by an explicit project/customer filter — else a
        # narrowed report would silently absorb install-wide doc-less findings it never
        # asked for. All three conditions must hold.
        _qa_unrestricted = allowed is None and q.project_id is None and cust_filter is None
        return _finish_qa(
            _qa_buckets(session, q, projects, proj_ids, unrestricted=_qa_unrestricted),
            q=q,
            user=user,
        )
    if q.kind == "postedit":
        return _finish_postedit(_postedit_buckets(session, q, projects, proj_ids), q=q, user=user)

    # RV-2 / VR-5-lite: the `locale` and `vendor` axes are computed by dedicated,
    # ISOLATED paths (their attribution / columns differ from the job-loop axes), each
    # scoped by the SAME already-confined proj_ids so tenant isolation is identical.
    if q.group_by == "locale":
        return _finish(q, _locale_buckets(session, q, projects, proj_ids), user=user)
    if q.group_by == "vendor":
        return _finish_vendor(_vendor_buckets(session, q, proj_ids), q=q, user=user)

    cust_name_by_id = {c.id: c.name for c in session.exec(select(Customer)).all()}

    # Segment counts / char sums / derived words per project (of translated segments),
    # so the volume metrics attribute to the project's jobs. We aggregate per project
    # then fold into whichever group the project's jobs land in. Only TRANSLATED
    # segments (non-empty target_text) count toward words/chars (the derived-words
    # rule: source words OF translated segments).
    seg_by_project = _segment_metrics(session, proj_ids)

    # Jobs whose project is in scope. A job attributes to a project via
    # payload.project_id; a connector job may carry connection_id instead - we only
    # aggregate jobs we can tie to an in-scope project (documented: connector jobs
    # without a resolvable project_id are out of v1's per-project/customer scope).
    buckets: dict[str, dict] = {}
    # Pre-seed volume metrics per group from the project segment aggregates, so a
    # project with segments but (somehow) no finished job still shows its volume.
    # We fold segment metrics in as each project's FIRST job appears, to avoid
    # double counting across a project's multiple jobs; track which projects folded.
    folded_projects: set[int] = set()

    # Issue #53 (round-2 A5): push the report's DATE WINDOW into SQL so we never
    # materialize the payload/result JSON blobs of jobs outside the window (the scan was
    # `select(Job)` with NO WHERE — every job ever, blobs and all, filtered in Python).
    # The prefilter is a deliberate SUPERSET, mirroring the WorkflowRun scan above: bucket
    # a job on COALESCE(finished_at, created_at) (== _job_date), widen each bound by a day
    # (time.min/time.max) to dodge any date/datetime boundary, then apply the EXACT Python
    # rule (_job_date + the < date_from / > date_to guards) UNCHANGED in the loop below.
    # Output is therefore byte-identical; only the out-of-window rows stop being loaded.
    # NB: project scope is NOT pushed — a job attributes to a project via its JSON payload
    # (payload.project_id / connection_id), not a column, so there is no cheap SQL predicate;
    # the in-Python `pid not in projects` filter stays authoritative for scope.
    jstmt = select(Job)
    job_date_col = func.coalesce(Job.finished_at, Job.created_at)
    if q.date_from is not None:
        jstmt = jstmt.where(
            job_date_col >= datetime.combine(q.date_from - timedelta(days=1), time.min)
        )
    if q.date_to is not None:
        jstmt = jstmt.where(
            job_date_col <= datetime.combine(q.date_to + timedelta(days=1), time.max)
        )
    jobs = session.exec(jstmt).all()
    for job in jobs:
        pid = _job_project_id(session, job)
        if pid is None or pid not in projects:
            continue
        d = _job_date(job)
        if q.date_from is not None and (d is None or d < q.date_from):
            continue
        if q.date_to is not None and (d is None or d > q.date_to):
            continue
        proj = projects.get(pid)
        # provider filter (on the job's dominant provider)
        if q.provider is not None:
            prov = _dominant_provider((job.result or {}).get("provider_used") or {})
            if (prov or "") != q.provider:
                continue
        key = _group_key(q, job, proj, cust_name_by_id)
        row = buckets.setdefault(key, _zero_metrics())
        row["jobs"] += 1
        result = job.result or {}
        # Defensive int() coercion (mirrors _job_cost's guard below): a legacy or
        # malformed Job.result can carry a non-int token value (e.g. "notanint").
        # A bare int() on that raises ValueError and 500s the WHOLE report for
        # every caller - one bad row must not take the feature down. Malformed
        # values contribute 0, same as _job_cost's own defensive reads.
        row["tokens_in"] += _int_or_none(result.get("tokens_in", 0)) or 0
        row["tokens_out"] += _int_or_none(result.get("tokens_out", 0)) or 0
        row["tokens_cache_read"] += _int_or_none(result.get("tokens_cache_read", 0)) or 0
        row["tokens_cache_write"] += _int_or_none(result.get("tokens_cache_write", 0)) or 0
        row["cost_usd"] += _job_cost(job)
        # Fold the project's segment volume (segments/chars/words) in ONCE, on the
        # first in-scope job we see for that project, into that job's bucket.
        # APPROXIMATION (documented): segments have no per-job / per-provider / per-date
        # attribution, so for group_by=provider|date a project's whole segment volume
        # lands in whichever (provider/date) bucket its first job fell in. The
        # install-wide TOTALS and the project/customer/none groupings are exact; the
        # volume split across provider/date buckets is indicative, not authoritative.
        if pid not in folded_projects:
            folded_projects.add(pid)
            m = seg_by_project.get(pid)
            if m:
                row["segments"] += m["segments"]
                row["chars"] += m["chars"]
                row["words_derived"] += m["words_derived"]

    # Overlay workflow-run spend (StepRun sums) on top of the job buckets. Runs and jobs
    # are DISJOINT (a `workflow run` writes StepRuns, never a Job — see the module
    # docstring), so this is always a clean merge that double-counts nothing. Both paths
    # honour the tracked/untracked split (an all-NULL legacy/pending run → "(untracked)",
    # never a silent $0 in a real bucket).
    #   * workflow axis: the EXACT per-workflow rows + the honest "(untracked)" row
    #     (unchanged; jobs already rolled up as "(no workflow)").
    #   * every OTHER axis (F2-2): StepRun tokens/cost bucketed by that axis's key, so a
    #     workflow-driven install no longer reads a false 0 on the default group_by=none
    #     (and on project/customer/provider/engine/date). Run-only projects also get
    #     their segment volume folded here, on every axis, so the TOTAL never disappears.
    if q.group_by == "workflow":
        overlay = _workflow_run_buckets(session, q, proj_ids, q.provider)
    else:
        overlay = _run_spend_buckets(
            session,
            q,
            projects,
            proj_ids,
            cust_name_by_id,
            q.provider,
            seg_by_project,
            folded_projects,
        )
    for key, wf_row in overlay.items():
        dest = buckets.setdefault(key, _zero_metrics())
        for mk in _METRIC_KEYS:
            dest[mk] += wf_row[mk]

    return _finish(q, buckets, user=user)


def _empty_report(q: ReportQuery, user) -> dict:
    """A no-data report with the columns of the REQUESTED kind/axis (M1). The qa/postedit
    kinds and the vendor axis each have their own column shapes, so a zero-scope request
    for any of them must return THAT shape — never the job/cost column set. The kind arms
    come FIRST, matching run_report's dispatch order exactly (SPEC-wave2 §4.1): otherwise a
    zero-visibility `--kind qa --group-by vendor` would leak the vendor scorecard columns."""
    if q.kind == "qa":
        return _finish_qa({}, q=q, user=user)
    if q.kind == "postedit":
        return _finish_postedit({}, q=q, user=user)
    if q.group_by == "vendor":
        return _finish_vendor({}, q=q, user=user)
    return _finish(q, {}, user=user)


def _finish(q: ReportQuery, buckets: dict, *, user=None) -> dict:
    """Shape the bucket dict into ordered columns + row dicts. Deterministic row
    order (by key), rounded cost. No TOTALS row here - to_csv adds it, and callers
    that render a table add their own footer; keeping run_report's rows PURE data
    makes the parity assertions (web==cli==api) a clean dict compare."""
    key_col = _key_column(q.group_by)
    metric_cols = list(_KIND_COLUMNS[q.kind])
    # M7 (fix round): human_cost_usd is populated ONLY on the workflow axis (vendor legs
    # attach to WorkflowRuns, which only _workflow_run_buckets pulls in). On every other
    # group-by it would be a structural zero, which reads as "no human spend" rather than
    # "not measured on this axis". HIGH-E (final round): it is ALSO admin-only margin data,
    # so it is dropped for every non-admin / customer-bound ident regardless of axis. It
    # survives ONLY on the workflow axis AND for an admin/operator (never serialized to
    # columns/rows/CSV otherwise, so no caller can read it).
    if q.group_by != "workflow" or not _can_see_human_spend(user):
        metric_cols = [c for c in metric_cols if c != "human_cost_usd"]
    columns = [key_col] + metric_cols  # noqa: RUF005
    rows = []
    for key in sorted(buckets.keys()):
        m = buckets[key]
        row = {key_col: _axis_key(key)}  # E1: every axis key sanitized at render
        for c in metric_cols:
            v = m[c]
            row[c] = round(v, 6) if c in _FLOAT_METRIC_KEYS else v
        rows.append(row)
    return {
        "columns": columns,
        "rows": rows,
        "key_column": key_col,
        "kind": q.kind,
        "group_by": q.group_by,
        "caveats": _caveats(q, buckets),
    }


def _locale_buckets(session: Session, q: ReportQuery, projects: dict, proj_ids: set) -> dict:
    """The `--group-by locale` axis: cost/tokens/volume bucketed by TARGET locale.

    Three honest, INDEPENDENT contributions (each attributed to the locale it truly
    belongs to — no project-level fold approximation, unlike the provider/date axes):
      * JOB spend/count -> the job's payload.locale (what a translate_document job carries
        — web/api both submit {document_id, locale}). A job with no locale (e.g. a
        connector job) buckets as '(untracked locale)'.
      * RUN spend/count -> WorkflowRun.target_locale (one member run per requested locale,
        so a run's locale is exact). A TRACKED run (>=1 StepRun with a non-NULL cost)
        contributes its StepRun tokens+cost to its locale bucket; an UNTRACKED run
        (all-NULL cost — legacy/pending) is counted in a separate '(untracked)' row with
        0 cost, NEVER a false $0 in a real locale bucket (the engine's never-zero-for-NULL
        contract).
      * VOLUME (segments/words/chars) -> Segment.target_locale directly (exact per-locale).

    Runs and jobs are DISJOINT (a `workflow run` writes StepRuns, never a Job), so their
    spend never double-counts. A `provider` filter drops a job / run with no step on that
    provider (mirroring the other axes). Scoping rides entirely on the already-confined
    proj_ids the caller passes, so a locale bucket can never sum a foreign tenant's data."""
    buckets: dict[str, dict] = {}
    prov = q.provider
    # H1: segment VOLUME folds only for projects that had an IN-WINDOW job or run —
    # the SAME approximation every job-loop axis uses (volume folds on a project's first
    # in-window job/run; a project with no in-window activity contributes no volume). So
    # an out-of-window date filter zeroes the locale axis exactly like the others, and a
    # segment-only project with no job/run never shows phantom volume.
    active_projects: set[int] = set()

    # 1. JOB spend/count by payload.locale ------------------------------------
    for job in session.exec(select(Job)).all():
        pid = _job_project_id(session, job)
        if pid is None or pid not in projects:
            continue
        d = _job_date(job)
        if q.date_from is not None and (d is None or d < q.date_from):
            continue
        if q.date_to is not None and (d is None or d > q.date_to):
            continue
        if prov is not None:
            jp = _dominant_provider((job.result or {}).get("provider_used") or {})
            if (jp or "") != prov:
                continue
        active_projects.add(pid)
        loc = (job.payload or {}).get("locale") or "(untracked locale)"
        row = buckets.setdefault(loc, _zero_metrics())
        row["jobs"] += 1
        result = job.result or {}
        row["tokens_in"] += _int_or_none(result.get("tokens_in", 0)) or 0
        row["tokens_out"] += _int_or_none(result.get("tokens_out", 0)) or 0
        row["tokens_cache_read"] += _int_or_none(result.get("tokens_cache_read", 0)) or 0
        row["tokens_cache_write"] += _int_or_none(result.get("tokens_cache_write", 0)) or 0
        row["cost_usd"] += _job_cost(job)

    # 2. RUN spend/count by WorkflowRun.target_locale -------------------------
    runs, srs_by_run, doc_to_proj = _fetch_scoped_runs(session, q, proj_ids)
    for run in runs:
        srs = srs_by_run.get(run.id, [])
        matching = (
            [sr for sr in srs if (sr.provider_used or "") == prov] if prov is not None else srs
        )
        if prov is not None and not matching:
            continue
        rpid = doc_to_proj.get(run.document_id)
        if rpid is not None:
            active_projects.add(rpid)  # a counted run (tracked or not) activates volume
        tracked = any(sr.cost_usd is not None for sr in srs)
        if not tracked:
            buckets.setdefault("(untracked)", _zero_metrics())["jobs"] += 1
            continue
        loc = getattr(run, "target_locale", "") or "(untracked locale)"
        row = buckets.setdefault(loc, _zero_metrics())
        row["jobs"] += 1
        for sr in matching:
            if sr.cost_usd is not None:
                row["cost_usd"] += sr.cost_usd
            if sr.tokens_in is not None:
                row["tokens_in"] += sr.tokens_in
            if sr.tokens_out is not None:
                row["tokens_out"] += sr.tokens_out

    # 3. VOLUME by Segment.target_locale (exact) — only for IN-WINDOW-active projects
    #    (H1: matches the job-loop axes' fold-on-first-in-window-job/run rule, so a date
    #    filter or a segment-only project can never leak phantom volume onto this axis).
    for loc, m in _segment_metrics_by_locale(session, active_projects).items():
        row = buckets.setdefault(loc, _zero_metrics())
        row["segments"] += m["segments"]
        row["chars"] += m["chars"]
        row["words_derived"] += m["words_derived"]

    return buckets


def _finish_qa(bundle, *, q: ReportQuery, user=None) -> dict:
    """Shape qa buckets into the findings/severity/cards columns (§4.2). Deterministic
    row order. avg_card_score is NON-SUMMABLE and blank when a bucket has no
    card-identified rows (rule 3, NULL != 0). Accepts {} from _empty_report."""
    bundle = bundle or {}
    buckets = bundle.get("buckets", {})
    meta = bundle.get("meta", {})
    key_col = _key_column(q.group_by)
    metric_cols = list(_KIND_COLUMNS["qa"])
    columns = [key_col] + metric_cols  # noqa: RUF005
    rows = []
    for key in sorted(buckets.keys()):
        m = buckets[key]
        row = {key_col: _axis_key(key)}
        for s in _QA_SEVERITIES:
            row[s] = m[s]
        row["findings"] = m["findings"]
        row["human"] = m["human"]  # WAVE 3 §6.6.3
        # cards = distinct scoring_id (NULL-scoring_id rows excluded, §3.1.3); the
        # per-card score is identical across a card's rows by construction.
        card_scores = [s for s in m["_card_scores"].values() if s is not None]
        row["cards"] = len(m["_card_scores"])
        row["avg_card_score"] = round(sum(card_scores) / len(card_scores), 4) if card_scores else ""
        rows.append(row)
    return {
        "columns": columns,
        "rows": rows,
        "key_column": key_col,
        "kind": "qa",
        "group_by": q.group_by,
        "caveats": _qa_caveats(q, meta),
    }


def _finish_postedit(bundle, *, q: ReportQuery, user=None) -> dict:
    """Shape postedit buckets (§4.2). Column set differs on the multi-bucket axes (N7):
    on actor/vendor the segment-scoped columns (words_derived / tte_ms / avg_tte_ms) are
    ABSENT and mt_chars/final_chars are episode-scoped; segments_edited stays (a segment
    touched by two actors is counted in both rows, with a caveat). avg_* columns are
    NON-SUMMABLE and blank when nothing was measured (rule 3). Accepts {} from
    _empty_report."""
    bundle = bundle or {}
    buckets = bundle.get("buckets", {})
    meta = bundle.get("meta", {})
    multi = q.group_by in ("actor", "vendor")
    metric_cols = list(_POSTEDIT_COLUMNS_MULTI if multi else _KIND_COLUMNS["postedit"])
    key_col = _key_column(q.group_by)
    columns = [key_col] + metric_cols  # noqa: RUF005
    rows = []
    for key in sorted(buckets.keys()):
        m = buckets[key]
        row = {key_col: _axis_key(key)}
        row["segments_edited"] = len(m["_usable_seg_ids"])
        row["episodes"] = m["episodes"]
        row["edits"] = m["edits"]
        row["mt_chars"] = m["mt_chars"]
        row["final_chars"] = m["final_chars"]
        row["distance_char"] = m["distance_char"]
        row["distance_word"] = m["distance_word"]
        pct = m["_pct"]
        row["avg_distance_pct"] = round(sum(pct) / len(pct), 4) if pct else ""
        if not multi:
            row["words_derived"] = m["words_derived"]
            row["tte_ms"] = m["tte_ms"]
            seg_n = len(m["_seg_ids"])
            row["avg_tte_ms"] = round(m["tte_ms"] / seg_n) if seg_n and m["tte_ms"] else ""
        rows.append({c: row[c] for c in columns})
    return {
        "columns": columns,
        "rows": rows,
        "key_column": key_col,
        "kind": "postedit",
        "group_by": q.group_by,
        "caveats": _postedit_caveats(q, meta),
    }
