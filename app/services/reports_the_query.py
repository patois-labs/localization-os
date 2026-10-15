"""reports: the query

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _QA_QUERY_COLS, _VENDOR_LQA_COLS, _PE_QUERY_COLS,
_REPORT_FORBIDDEN_BLOB_COLS, human_findings_for_document, _MACHINE_FINDINGS_DISPLAY_CAP,
machine_findings_for_document, lqa_scorecard_headline, _dt_lo from
app.services.reports_the_query_leaf.

Documented in: docs/architecture/reporting-and-dashboard.md
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time

from sqlalchemy import or_
from sqlmodel import Session, select

from app import config
from app.models import (
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
from app.services import access, spend
from app.services.reports_the_query_leaf import (
    _MACHINE_FINDINGS_DISPLAY_CAP,
    _PE_QUERY_COLS,
    _QA_QUERY_COLS,
    _REPORT_FORBIDDEN_BLOB_COLS,
    _VENDOR_LQA_COLS,
    _dt_lo,
    human_findings_for_document,
    lqa_scorecard_headline,
    machine_findings_for_document,
)

__all__ = [
    "_MACHINE_FINDINGS_DISPLAY_CAP",
    "_PE_QUERY_COLS",
    "_QA_QUERY_COLS",
    "_REPORT_FORBIDDEN_BLOB_COLS",
    "_VENDOR_LQA_COLS",
    "LQAResult",
    "SegmentPostEdit",
    "StepRun",
    "StepType",
    "WorkflowRun",
    "WorkflowStep",
    "_dt_lo",
    "human_findings_for_document",
    "lqa_scorecard_headline",
    "machine_findings_for_document",
    "or_",
]


def _dt_hi(d):
    """Inclusive upper datetime bound for a date filter pushed into SQL (day end)."""
    return datetime.combine(d, time.max) if d is not None else None


KINDS = ("cost", "tokens", "volume", "qa", "postedit")


# --- report kinds + their rendered columns -----------------------------------
# Every kind computes the SAME shared aggregate (compute once); `kind` only decides
# which columns render. The key-column(s) come from group_by and are prepended.
class UnknownReportKind(ValueError):
    """Raised when a report is requested with a kind not in KINDS (issue #25).

    Previously `normalized()` silently coerced an unknown kind to 'cost', so a typo
    ('token' for 'tokens') quietly ran the WRONG report. Now the service seam fails
    loudly; every caller surfaces a clean usage/400 (the CLI also gates via argparse
    choices=, so a bad kind is rejected before it reaches the service at all)."""


def validate_kind(kind) -> str:
    """The canonical kind validator (the single source of truth argparse choices=
    mirror). Returns the normalized (lower/stripped) kind, or raises
    UnknownReportKind. Used by ReportQuery.normalized() so run_report and every
    surface reject an unknown kind identically."""
    k = (kind or "").strip().lower()
    if k not in KINDS:
        raise UnknownReportKind(
            f"unknown report kind: {kind!r} (choose one of: {', '.join(KINDS)})."
        )
    return k


_KIND_COLUMNS = {
    "cost": [
        "jobs",
        "segments",
        "words_derived",
        "chars",
        "tokens_in",
        "tokens_out",
        "tokens_cache_read",
        "tokens_cache_write",
        "cost_usd",
        "human_cost_usd",
    ],
    "tokens": [
        "jobs",
        "segments",
        "tokens_in",
        "tokens_out",
        "tokens_cache_read",
        "tokens_cache_write",
    ],
    "volume": ["jobs", "segments", "words_derived", "chars"],
    # Wave 2 (SPEC-wave2 §4.2). These two kinds are shaped by _finish_qa / _finish_postedit
    # (their columns can vary by axis — see the N7 multi-bucket exclusions on postedit), so
    # these entries are the CANONICAL/default column order that `kinds_catalog` advertises
    # and the non-axis-varying shape a `none`/`project`/`date` report renders.
    # WAVE 3 §6.6.3: `human` counts non-withdrawn reviewer-authored findings in the bucket.
    # It sits in the finding breakdown (a subset of `findings`), before the card metrics.
    "qa": [
        "findings",
        "critical",
        "major",
        "minor",
        "neutral",
        "other",
        "human",
        "cards",
        "avg_card_score",
    ],
    "postedit": [
        "segments_edited",
        "episodes",
        "edits",
        "words_derived",
        "mt_chars",
        "final_chars",
        "distance_char",
        "distance_word",
        "avg_distance_pct",
        "tte_ms",
        "avg_tte_ms",
    ],
}


_QA_SEVERITIES = ("critical", "major", "minor", "neutral", "other")


_POSTEDIT_COLUMNS_MULTI = [
    "segments_edited",
    "episodes",
    "edits",
    "mt_chars",
    "final_chars",
    "distance_char",
    "distance_word",
    "avg_distance_pct",
]


_NON_SUMMABLE_COLUMNS = frozenset(
    {
        "avg_card_score",
        "avg_distance_pct",
        "avg_tte_ms",
        "avg_lqa_score",
    }
)


GROUP_BYS = (
    "project",
    "customer",
    "provider",
    "engine",
    "date",
    "workflow",
    "locale",
    "vendor",
    "dimension",
    "actor",
    "none",
)


_STD_AXES = (
    "project",
    "customer",
    "provider",
    "engine",
    "date",
    "workflow",
    "locale",
    "vendor",
    "none",
)


_SUPPORTED_GROUP_BYS = {
    "cost": frozenset(_STD_AXES),
    "tokens": frozenset(_STD_AXES),
    "volume": frozenset(_STD_AXES),
    "qa": frozenset(
        ("project", "customer", "locale", "vendor", "dimension", "actor", "date", "none")
    ),
    "postedit": frozenset(("project", "customer", "locale", "vendor", "actor", "date", "none")),
}


# The metric columns each kind surfaces (in render order). words_derived is labeled
# "derived" in the header helper so it is never read as an authoritative count.
# cost_usd is the AI (model) spend; human_cost_usd is the vendor human post-edit spend
# (decision 5 -- the AI-vs-Human split). human_cost_usd is ADDITIVE and appended LAST so
# no existing column position/name changes; it is populated only on the workflow axis
# (vendor legs attach to WorkflowRuns, and only _workflow_run_buckets pulls runs in) and
# reads 0 on the job-based axes.
# Wave 2 (SPEC-wave2 §4.2). These two kinds are shaped by _finish_qa / _finish_postedit
# (their columns can vary by axis — see the N7 multi-bucket exclusions on postedit), so
# these entries are the CANONICAL/default column order that `kinds_catalog` advertises
# and the non-axis-varying shape a `none`/`project`/`date` report renders.
# WAVE 3 §6.6.3: `human` counts non-withdrawn reviewer-authored findings in the bucket.
# It sits in the finding breakdown (a subset of `findings`), before the card metrics.
# SPEC-wave2 §4.2 severity cross-tab (canonical PENALTY keys + the explicit `other`
# bucket, lqa.canon_severity). The order the qa kind renders these columns in.
# SPEC-wave2 §4.2 N7: on the MULTI-BUCKET axes (actor / vendor) a single segment can
# appear in several buckets (one per episode), and to_csv sums every numeric column into
# TOTALS — so a strictly segment-scoped column would be counted once PER bucket and the
# TOTALS row would overstate reality. On those axes the column set drops the
# segment-scoped columns (words_derived / tte_ms / avg_tte_ms) and mt_chars/final_chars
# become EPISODE-scoped; segments_edited stays (with a "counted in both rows" caveat).
# SPEC-wave2 §4.5 rule 4 (M3): the average columns must NOT be summed — or zeroed — into
# a TOTALS row. `to_csv`'s isinstance guard merely SKIPS a non-numeric cell, so an
# all-empty average column would still total the integer 0 it was seeded with
# (probe-proven). This shared frozenset is consulted at BOTH TOTALS call sites (to_csv +
# totals_row) so CSV and JSON can never diverge: those columns emit "" in TOTALS. A key
# is stored as the plain column name; every avg_* column the two new kinds and the vendor
# scorecard render is listed here.
# RV-2 / VR-5-lite: `locale` groups by TARGET locale (jobs by payload.locale, runs by
# WorkflowRun.target_locale, volume by Segment.target_locale — all honestly attributable);
# `vendor` is a SEPARATE SCORECARD axis sourced from VendorDispatch legs (its own columns,
# not the kind's — see _vendor_report), so both are handled by dedicated isolated paths in
# run_report rather than threaded through the fragile job-loop/overlay.
# SPEC-wave2 §4.3: the axes each kind supports. A known-but-unsupported (kind, axis)
# combination degrades to `none` LOUDLY — it emits a caveat naming the degradation on
# every surface (never the silent cap the doctrine forbids). `dimension`/`actor` are
# meaningful only for the quality kinds; the AI-spend kinds keep the Wave 1 axis set.
def _supported_axis(kind: str, group_by: str) -> bool:
    """True if `group_by` is a supported axis for `kind` (SPEC-wave2 §4.3). An
    unrecognized kind falls back to the full GROUP_BYS set (validate_kind has already
    rejected a truly-unknown kind before this is reached)."""
    return group_by in _SUPPORTED_GROUP_BYS.get(kind, frozenset(GROUP_BYS))


# --- derivation helpers ------------------------------------------------------
def _model_for_provider(provider: str | None) -> str | None:
    """The model that provider was billed on (config.DEFAULT_MODELS - the SAME map
    the translate specs used at run time), so a job's report cost reconciles with the
    ledger's for the common single-provider job.

    Falls back to the PROVIDER NAME itself when no default model is configured. Whether the
    provider is FREE is decided before the model matters, by spend.cost_for_ref (#361).
    (config.DEFAULT_MODELS has no 'mock' key and maps 'local' to "", so both name
    themselves.) A truly unknown provider name -> the conservative default via
    pricing.rates_for, which is the safe direction.

    NOTE (documented ambiguity, not a bug): on a KEYLESS install every requested
    provider resolves to MockProvider, so the ledger bills $0 while Job.result still
    records the REQUESTED name (e.g. "claude"). This report prices on the recorded
    name at list rates - i.e. "what this WOULD cost live" - and only a job explicitly
    recorded as mock/local reads $0. On a keyed install the recorded name IS the real
    provider, so cost reconciles with the ledger exactly."""
    if not provider:
        return None
    return config.DEFAULT_MODELS.get(provider) or provider


def _job_date(job: Job) -> date | None:
    """The date a job is bucketed / range-filtered on: finished_at if present (the
    unit of work completed then), else created_at (unfinished/failed fallback)."""
    ts = job.finished_at or job.created_at
    if isinstance(ts, datetime):
        return ts.date()
    return ts


def _dominant_provider(provider_used: dict) -> str | None:
    """The provider a job's aggregate tokens are attributed to for pricing: the one
    with the most calls (a plain single-provider job has exactly one). Ties break
    on name for determinism. None when provider_used is empty (mock/TM-only job)."""
    if not provider_used:
        return None
    return sorted(provider_used.items(), key=lambda kv: (-int(kv[1] or 0), kv[0]))[0][0]


def _priced_provider(provider_used: dict) -> str | None:
    """The provider a job's AGGREGATE tokens are priced at: the dominant one, EXCEPT when
    that is a free engine and the job also used a paid one (#361 gate: a free local engine
    that failed over to a cloud engine mid-job). Job.result keeps one token total for every
    provider, so pricing it all at $0 would under-count the paid calls the ledger booked; it
    prices at the most-used PAID provider instead (an over-count of the free share, the safe
    direction for a budget meter and its refusal check)."""
    dominant = _dominant_provider(provider_used)
    if dominant is None or len(provider_used) < 2 or not spend.ref_is_free(dominant):
        return dominant
    paid = {k: v for k, v in provider_used.items() if not spend.ref_is_free(k)}
    return _dominant_provider(paid) or dominant


def _stored_job_cost(job: Job) -> float | None:
    """The exact USD the ledger booked while the job ran (Job.cost_usd, #364), or None when
    the row has none (written before the column, or a cut-off live stream) or the stored
    value is not a finite non-negative number (-> price it from the tokens, as before)."""
    raw = getattr(job, "cost_usd", None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        cost = float(raw)
    except (TypeError, ValueError):
        return None
    return cost if math.isfinite(cost) and cost >= 0.0 else None


def _job_cost(job: Job) -> float:
    """USD cost of one job. PREFERS the cost the live ledger booked at run time
    (Job.cost_usd, #364): it cannot drift when the engine is later deleted or re-pointed, and
    a failover job carries exactly the paid share. Only a job WITHOUT a stored figure (older
    rows, a cut-off live stream) is re-priced from its tokens, as described next.

    Re-pricing: via pricing.cost_usd VERBATIM, behind the ledger's free-engine rule
    (spend.cost_for_ref, #361): a job recorded on a mock, a bare `local` slot, or an
    `engine:<id>` whose profile is a self-hosted `local` kind on a proven private endpoint is
    $0, exactly what the live ledger booked. The job records only the engine NAME, so the rule
    reads the profile as it is NOW: a DELETED engine prices conservatively (default rate), and an
    engine whose kind or URL changed after the run prices by its current kind. Paid engines and
    public endpoints price as before.

    Two shapes:
      * char-billed (NMT): the result carries `chars` + `nmt_kind` -> cost_usd routes
        to the per-character table (model is irrelevant there).
      * token-billed (LLM): build the usage dict from the result's token counters and
        price on the model config.DEFAULT_MODELS maps the job's (dominant) provider to
        - the same model the ledger billed. A mock/local job resolves to a $0 model.

    Cache tokens are billed off the input rate exactly as the ledger does (cost_usd
    owns that math); we only translate the result's key names into cost_usd's usage
    keys. Every numeric read is coerced via _int_or_none (-> 0 on a malformed/legacy
    value, e.g. a non-numeric tokens_in) so one bad row prices as 0 instead of
    raising and 500-ing the whole report."""
    stored = _stored_job_cost(job)
    if stored is not None:
        return stored
    result = job.result or {}
    provider = _priced_provider(result.get("provider_used") or {})
    chars = _int_or_none(result.get("chars", 0)) or 0
    if chars > 0:
        usage = {"chars": chars, "nmt_kind": result.get("nmt_kind")}
        return spend.cost_for_ref(provider, _model_for_provider(provider), usage, on=_job_date(job))
    usage = {
        "input_tokens": _int_or_none(result.get("tokens_in", 0)) or 0,
        "output_tokens": _int_or_none(result.get("tokens_out", 0)) or 0,
        "cache_read_tokens": _int_or_none(result.get("tokens_cache_read", 0)) or 0,
        "cache_write_tokens": _int_or_none(result.get("tokens_cache_write", 0)) or 0,
    }
    return spend.cost_for_ref(provider, _model_for_provider(provider), usage, on=_job_date(job))


_WORD_RE = re.compile(r"\S+")


def _derived_words(text: str) -> int:
    """A DERIVED word count: whitespace-delimited tokens in the source text. Labeled
    'derived' everywhere it surfaces (no stamped counter in v1)."""
    return len(_WORD_RE.findall(text or ""))


# --- the query ---------------------------------------------------------------
def allowed_project_ids(session: Session, user, org_id):
    """The project-id set a caller may aggregate over, or None meaning unrestricted.

    PUBLIC scoping seam (SPEC-databricks-exporter §5.2/B6): both `run_report` and the
    Databricks analytical exporter call this. It is the CORRECT trusted-operator seam —
    the naive `access.visible_project_ids(session, None)` returns `[]` under the login
    wall (see the NB below), which would silently emit ZERO rows for an in-process
    export on an auth-enabled install.

    Two callers:
      * WEB / API - a resolved `user`. access.visible_project_ids(session, user) layers
        ALL three rings in one authoritative call: org scope, project-visibility
        membership, AND R2-3 customer-bound confinement. We use it verbatim (this is
        THE isolation seam - never hand-rolled).
      * IN-PROCESS CLI - the TRUSTED operator (user is None). It carries no web/API
        identity, so visibility scoping does not apply; instead the CLI's resolved
        --org (org_id) hard-filters to one org under the multi-org gate (require_org_scope
        enforces that a shared-host operator MUST pass --org). user=None WITHOUT org_id
        is fully unrestricted (single-tenant / --operator cross-org), exactly like every
        other trusted CLI read command.

    Returns a concrete id set (intersecting an explicit org_id) or None = unrestricted.
    NB: visible_project_ids(None) returns [] when the login wall is on, which is the
    WEB "no user -> nothing" semantic, NOT the trusted-operator semantic - so we only
    call it when a real `user` is supplied."""
    if user is None:
        # Trusted in-process operator: no visibility scoping; org_id (if any) confines.
        if org_id is None:
            return None
        return set(session.exec(select(Project.id).where(Project.org_id == org_id)).all())
    visible = access.visible_project_ids(session, user)  # None = unrestricted (staff)
    if org_id is not None:
        org_ids = set(session.exec(select(Project.id).where(Project.org_id == org_id)).all())
        return org_ids if visible is None else (set(visible) & org_ids)
    return None if visible is None else set(visible)


def _effective_customer_filter(user, requested_customer_id):
    """The customer_id the report is actually filtered to (R2-3 clamp).

    A customer-BOUND user is confined to their own customer no matter what they ask:
      * no filter requested    -> their own customer (defensive; visibility already
                                   confines, but this makes the intent explicit);
      * their own customer      -> honored;
      * a FOREIGN customer      -> clamped to their own (their-own/empty, NEVER the
                                   foreign data - the isolation guarantee).
    An unbound caller gets exactly what they asked for (or no filter)."""
    bound = access.current_customer_id(user)
    if bound is not None:
        return bound
    return requested_customer_id


def _job_document_id(job: Job) -> int | None:
    """The document id a job's payload/result names, if any (steps 1+4 of
    _job_project_id's resolution order, kept separate since group_by=workflow needs
    the raw document id, not the project it resolves to). None for a job with no
    resolvable document (e.g. a connector job keyed only by connection_id)."""
    payload = job.payload or {}
    doc_id = _int_or_none(payload.get("document_id"))
    if doc_id is not None:
        return doc_id
    result = job.result or {}
    return _int_or_none(result.get("document_id"))


def _run_date(run) -> date | None:
    """The date a WorkflowRun is bucketed / range-filtered on: finished_at if present
    (the run completed then), else started_at — mirroring _job_date's finished-or-
    fallback convention so a run and a job in the same window bucket consistently."""
    ts = getattr(run, "finished_at", None) or getattr(run, "started_at", None)
    if isinstance(ts, datetime):
        return ts.date()
    return ts


def _run_group_key(q, run, proj, cust_name_by_id):
    """The grouping key for a TRACKED WORKFLOW RUN under a NON-workflow axis, mirroring
    _group_key's job semantics so a run and a job that belong to the same project /
    customer / date land in the SAME bucket (the union is coherent). Not called for an
    UNTRACKED run (→ "(untracked)"), nor for group_by in ('provider', 'engine') (those
    key on the run's dominant provider, computed inline), nor for 'workflow' (that axis
    keeps its exact _workflow_run_buckets path)."""
    gb = q.group_by
    if gb == "project":
        return f"{proj.id}: {proj.name}" if proj else "(unknown project)"
    if gb == "customer":
        if proj is None or proj.customer_id is None:
            return "(no customer)"
        return f"{proj.customer_id}: {cust_name_by_id.get(proj.customer_id, '?')}"
    if gb == "date":
        d = _run_date(run)
        return d.isoformat() if d else "(undated)"
    return "all"  # group_by == "none"


def _dominant_step_provider(srs) -> str:
    """The provider a run is COUNTED under on the provider/engine axis: the one its
    StepRuns call most (ties break on name for determinism). "(none)" when there are no
    steps. Used for the run's single jobs-count increment AND for its volume fold slot —
    NEVER for its spend, which is attributed EXACTLY per-StepRun (D2)."""
    counts: dict[str, int] = {}
    for sr in srs:
        p = sr.provider_used or "(none)"
        counts[p] = counts.get(p, 0) + 1
    if not counts:
        return "(none)"
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _group_key(q, job, proj, cust_name_by_id):
    """The (label) grouping key for a job under the active group_by. Returns a
    (key_field_value, ) style scalar used both as the dict bucket and the rendered
    cell. 'engine' and 'provider' both key on the job's dominant provider (engine is
    the customer-facing synonym in v1 - NMT engines and LLM providers alike surface
    by the provider name recorded on the job)."""
    gb = q.group_by
    if gb == "none":
        return "all"
    if gb == "project":
        return f"{proj.id}: {proj.name}" if proj else "(unknown project)"
    if gb == "customer":
        if proj is None or proj.customer_id is None:
            return "(no customer)"
        return f"{proj.customer_id}: {cust_name_by_id.get(proj.customer_id, '?')}"
    if gb in ("provider", "engine"):
        prov = _dominant_provider((job.result or {}).get("provider_used") or {})
        return prov or "(none)"
    if gb == "date":
        d = _job_date(job)
        return d.isoformat() if d else "(undated)"
    if gb == "workflow":
        # A Job carries NO workflow identity (it is a plain-MT / connector unit — a
        # `workflow run` never writes a Job row), so on the workflow axis EVERY job
        # buckets as "(no workflow)". The real per-workflow spend rows come from
        # WorkflowRun/StepRun sums in _workflow_run_buckets (feat/workflow-spend).
        return "(no workflow)"
    return "all"


def _axis_key(raw) -> str:
    """The ONE render-time axis-key sanitizer wiring (SPEC-wave2 Errata E1). EVERY report
    bucket key — for EVERY kind, on all four surfaces — is routed through
    `lqa.sanitize_axis_key` before it reaches HTML/CSV/JSON: `_san_finding` (HTML/prompt-
    fence neutralize) + a length cap + the CSV formula-injection guard (`=`/`+`/`-`/`@` ->
    apostrophe). `--group-by dimension`/`actor` render model-authored text, which is the
    vector this closes; on the ordinary axes it is a no-op on a plain ASCII key (a project
    or vendor name is defended too, which is the correct direction). Local import keeps the
    reports<-lqa edge one-directional and lazy (the file's established pattern)."""
    from app.services import lqa

    return lqa.sanitize_axis_key(raw)


def _key_column(group_by: str) -> str:
    """The header for the grouping key column."""
    return {
        "none": "scope",
        "project": "project",
        "customer": "customer",
        "provider": "provider",
        "engine": "engine",
        "date": "date",
        "workflow": "workflow",
        "locale": "locale",
        "vendor": "vendor",
        "dimension": "dimension",
        "actor": "actor",
    }.get(group_by, "scope")


_VENDOR_QUALITY_COLS = ("lqa_findings", "lqa_major_critical", "avg_lqa_score")


_VENDOR_METRIC_KEYS = (
    "legs",
    "word_count",
    "returned_on_time",
    "lqa_findings",
    "lqa_major_critical",
    "avg_lqa_score",
    "estimate_cost_usd",
    "actual_cost_usd",
    "open_estimate_usd",
    "quoted_cost_usd",
)


_VENDOR_COST_COLS = ("estimate_cost_usd", "actual_cost_usd", "open_estimate_usd", "quoted_cost_usd")


# --- VR-5-lite: the `vendor` axis (vendor-dispatch scorecard) -----------------
# A SEPARATE axis: sourced from VendorDispatch legs (external/human work), so its
# columns are the vendor scorecard, NOT the AI kind's cost/token/volume columns. The
# FOUR money columns (estimate/actual over returned legs, plus open_estimate over
# in-flight legs and quoted over approved quotes — 2026-08-19) are HUMAN (vendor
# MSA) spend — gated exactly like human_cost_usd (operator/admin only,
# _can_see_human_spend); legs / word_count / returned_on_time are operational and stay
# viewer-readable within scope.
# SPEC-wave2 §4.6 (VR-5, quality half): three LQAResult-sourced quality columns are
# appended AFTER the operational leg columns and BEFORE the admin-only money group, so no
# existing name or position moves (the human_cost_usd additive precedent). They are
# machine-judged quality of content attributed to the vendor (vendor_id) — NOT a measure
# of editing effort (that lives on `--kind postedit --group-by vendor`). avg_lqa_score is
# NON-SUMMABLE and renders "" for a vendor with no scored card (rule 3, NULL != 0).
# All four are HUMAN (vendor MSA) spend columns, admin/operator-only (_can_see_human_spend).
# The two existing (estimate/actual) sum only ACCEPTED (RETURNED) legs; the two new ones
# surface money the returned-only rule cannot see:
#   open_estimate_usd — Σ estimate_cost_usd over IN-FLIGHT legs (DISPATCHED/QUOTED/
#                       RETRY_WAIT/ESCALATED), so an in-flight-only vendor no longer reads
#                       as free (estimate 0 but open_estimate > 0).
#   quoted_cost_usd   — Σ the operator-APPROVED quote (quote_approved_at + quoted_cost_usd),
#                       excluding the aborted/superseded terminals, matching the honest
#                       'quote' grade vendor_spend.leg_human_cost already uses.
# All are SUMS (never averages), so none go in _NON_SUMMABLE_COLUMNS. Appended AFTER the
# existing pair so no existing column position moves.
def _zero_vendor_metrics() -> dict:
    m = {k: (0.0 if k in _VENDOR_COST_COLS else 0) for k in _VENDOR_METRIC_KEYS}
    # avg_lqa_score is computed at finish from the distinct-scoring_id score map; its
    # numeric-0 seed is replaced there with the mean or "" (never a false 0.0 quality).
    m["_lqa_card_scores"] = {}  # scoring_id -> score (internal; stripped at finish)
    return m


def _vendor_caveats(show_cost: bool) -> list:
    out = [
        "vendor axis: sourced from vendor dispatch legs (external/human work), not AI "
        "jobs or workflow runs — a provider filter and the report kind do not change it "
        "(the scorecard is the same under cost, tokens or volume).",
        "legs and word_count count EVERY dispatch attempt in the window (all statuses, "
        "including cancelled, dead-letter, rejected and rework re-dispatches) — the "
        "volume of work sent.",
        "returned-on-time counts legs whose signed return arrived on or before the "
        "leg's expiry (returned_at <= expires_at), regardless of the leg's final status "
        "(a return later rejected still counts as an on-time delivery — an SLA fact, not "
        "an acceptance one); legs with no expiry or not yet returned are not counted "
        "on-time.",
        "a leg whose run/document row is missing (dangling) is out of project scope and "
        "is dropped from the scorecard.",
        # §4.6 VR-5 quality columns — three disclosures of real imprecision (M7):
        "quality columns (lqa_findings, lqa_major_critical, avg_lqa_score) count machine-"
        "judged LQA findings attributed to the vendor; avg_lqa_score is the mean over "
        "distinct scored cards and is blank (not 0) for a vendor with no scored card.",
        "a vendor with LQA findings in the window but no dispatch leg in it appears on the "
        "scorecard with legs=0 and its quality columns populated (windowed on the findings' "
        "own dates); a vendor with legs but no findings reads 0/0/blank.",
        "window divergence: the leg columns are windowed on the leg's dispatched_at, the "
        "quality columns on each finding's own created_at — a leg dispatched late in one "
        "month and scored early the next contributes words to one window and findings to "
        "the next (each column is windowed on its own event date).",
        "whole-document attribution: a workflow LQA finding is document-level while a "
        "dispatch leg may have carried only a SUBSET of that document's segments, so "
        "findings for segments the vendor never received can land on its row; and "
        "attribution is time-bounded (returned_at <= scored_at) and cannot see a fix "
        "step or human edit made between those two instants.",
    ]
    if show_cost:
        out.append(
            "estimate/actual are HUMAN (vendor MSA) spend — what you pay the vendor "
            "(admin/operator only). They sum ONLY legs whose return was ACCEPTED "
            "(status 'returned'), so a rejected->rework pair is not double-counted and a "
            "cancelled / dead-letter / still-in-flight leg is not counted as spend — the "
            "two columns are an estimate-vs-actual over the same returned legs."
        )
        out.append(
            "open_estimate_usd is the scoping estimate summed over IN-FLIGHT legs (status "
            "dispatched / quoted / retry_wait / escalated) — money committed but not yet "
            "returned; it EXCLUDES returned legs (counted in estimate above) and the "
            "cancelled / dead-letter / rejected terminals. A vendor with only in-flight "
            "work therefore reads estimate 0 but open_estimate > 0, not free."
        )
        out.append(
            "quoted_cost_usd sums the operator-APPROVED quote (Wave E) per leg — an "
            "authorized spend figure, separate from the scoping estimate and the reconciled "
            "actual (a leg with both contributes to each column, never blended). It "
            "EXCLUDES quotes that were only proposed (never approved) and the cancelled / "
            "dead-letter / rejected-superseded terminals, so a declined or reworked quote "
            "is not counted."
        )
        out.append(
            "a DEAD-LETTER leg's money is in NO column by design — its estimate and even "
            "an approved quote vanish from the scorecard when a dispatch dead-letters; "
            "audit the leg itself for that spend history."
        )
    return out


def can_see_cost(user) -> bool:
    """CV-4: who may run the `cost` report kind at all.

    POLICY (Fable-ratified DEFAULT, deliberately overridable — this is a SOURCE edit, not
    a setting): a cost report exposes AI (model) spend, so it is manager+ data. The None
    arm is the in-process CLI local path (the trusted operator passes user=None), mirroring
    `_can_see_human_spend`; a real ident must clear `security.can_manage` (rank >= manager:
    manager/admin — a viewer/translator/reviewer/linguist is refused). Auth-off single-
    tenant web/api does NOT reach the None arm — session_user returns the SYSTEM admin
    there, which clears can_manage. The `tokens` and `volume` kinds stay viewer-readable
    within scope (unchanged).

    POLICY ASYMMETRY — CLOSED (maintainer decision 2026-08-19): a LINGUIST is refused the cost
    report by this gate AND now no longer reads install-wide AI spend on /setup-ai — that
    page stays linguist-accessible (can_tune_ai) but its spend meter (spent/cap/pct_used)
    is gated to can_manage on the resolved user, so a non-manager sees a neutral stub, not
    the figures. The two surfaces now AGREE under the login wall: whole-install AI spend is
    manager+ on both (the /api cost gate still no-ops auth-off; the actor axis is gated always).
    (Was M5, recorded here as an open follow-up; the /setup-ai gate lives in
    app/web/admin.setup_ai + templates/setup_ai.html.)

    Before CV-4 the cost report was viewer-visible by design (scope-confined, not
    seniority-confined; only the human-spend COLUMN was admin-gated via
    _can_see_human_spend). The gate lives here as the single source of truth the web
    /reports/run and GET /api/reports/run both enforce (a `--remote` CLI report is answered
    — and gated — by the server). To make cost reports read-open, widen this predicate to
    `return True` (a code change, in this one place)."""
    from app.services import security

    return user is None or security.can_manage(user)


def can_see_actor_axis(user) -> bool:
    """SPEC-wave2 §4.4: who may run the `actor` axis (on `qa` or `postedit`).

    POLICY (Fable-ratified DEFAULT, deliberately overridable — a SOURCE edit, not a
    setting): the actor axis ranks NAMED people by output — for `postedit` its bucket key
    is an individual editor's email — which is a different disclosure from a project total,
    so it is manager+ data. Mirrors `can_see_cost`'s shape exactly: the None arm is the
    in-process CLI local path (the TRUSTED operator passes user=None), a real ident must
    clear `security.can_manage` (rank >= manager). Auth-off single-tenant web/api does NOT
    reach the None arm — session_user returns the SYSTEM admin there, which clears
    can_manage. The qa/postedit kinds and their OTHER axes stay viewer-readable in scope.

    Enforcement lives at the SURFACES that carry an untrusted identity — the web
    /reports/run route, GET /api/reports/run, and a --remote CLI report (answered and
    gated by the server) all refuse the axis with a clean 403/usage error, and the web
    form hides the option (the `can_cost` pattern). The in-process CLI is the trusted
    operator and is allowed (the `user is None` arm). run_report itself does NOT gate — it
    is the pure service seam the trusted operator drives directly. To widen the axis to
    all viewers, change this predicate to `return True` (a code change, in this one
    place).

    ALWAYS-ON on /api: UNLIKE the cost gate, the GET /api/reports/run
    actor-axis 403 does NOT condition on security.auth_required() — because a postedit
    bucket key is an editor's EMAIL (PII, not a money figure), a viewer-tier docs-scoped
    key is refused the actor axis even with the login wall OFF. The web route still resolves
    the SYSTEM admin under auth-off (200), and the in-process CLI stays the trusted
    operator; only the keyed /api surface tightens."""
    from app.services import security

    return user is None or security.can_manage(user)


def _can_see_human_spend(user) -> bool:
    """HIGH-E: who may see the Human (vendor MSA) spend column in a report. It is what YOU
    pay the vendor -- margin data a customer-bound / viewer / manager ident must never read.
    Mirrors run_detail's `_can_see_vendor`: the trusted in-process operator (user is None --
    the CLI local path / auth-off web, where session_user is None) OR a real ADMIN. Every
    customer-confined lsu_ key and every non-admin role resolves to a non-admin User, so the
    column is dropped for them."""
    from app.services import security

    return user is None or security.is_admin(user)


def _int_or_none(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _document_project_id(session: Session, doc_id) -> int | None:
    """Project id of a Document, via a cheap PK get. None on a bad/missing id."""
    doc_id = _int_or_none(doc_id)
    if doc_id is None:
        return None
    doc = session.get(Document, doc_id)
    return doc.project_id if doc is not None else None


def _segment_metrics(session: Session, project_ids: set) -> dict:
    """Per-project {segments, chars, words_derived} over TRANSLATED segments only
    (non-empty target_text). segments = count of translated segments; chars = sum of
    source_text lengths of those; words_derived = whitespace token count of the same
    source text (the derived-word rule: source words OF translated segments)."""
    from app.models import Segment

    out: dict[int, dict] = {}
    if not project_ids:
        return out
    # Map document_id -> project_id for the in-scope projects, then aggregate their
    # translated segments. Bounded to the scoped projects' documents only.
    docs = session.exec(
        select(Document.id, Document.project_id).where(Document.project_id.in_(project_ids))
    ).all()
    doc_to_proj = {d[0]: d[1] for d in docs}
    if not doc_to_proj:
        return out
    doc_ids = list(doc_to_proj.keys())
    # Chunk the IN() to stay well under SQLite's expression-tree limits on big docs.
    CHUNK = 150
    for i in range(0, len(doc_ids), CHUNK):
        batch = doc_ids[i : i + CHUNK]
        # Issue #53 (round-2 A5): project ONLY the three columns this aggregate reads
        # instead of `select(Segment)` (which materialized full rows — 3 JSON blobs
        # inline_tags/qa_flags/meta + all lock/status metadata — merely to sum lengths).
        # source_text drives BOTH chars and derived words; target_text is needed for the
        # translated-only gate (its exact Python `.strip()` truthiness — Unicode-whitespace
        # aware, unlike SQL trim() — is kept verbatim so the row set stays byte-identical);
        # document_id maps the row to its project. Output is unchanged; only the unused
        # columns stop being loaded.
        segs = session.exec(
            select(Segment.document_id, Segment.source_text, Segment.target_text).where(
                Segment.document_id.in_(batch)
            )
        ).all()
        for s in segs:
            if not (s.target_text or "").strip():
                continue  # only TRANSLATED segments count toward derived volume
            pid = doc_to_proj.get(s.document_id)
            if pid is None:
                continue
            m = out.setdefault(pid, {"segments": 0, "chars": 0, "words_derived": 0})
            m["segments"] += 1
            m["chars"] += len(s.source_text or "")
            m["words_derived"] += _derived_words(s.source_text or "")
    return out


# --- Wave 2: the `qa` and `postedit` kinds (SPEC-wave2 §4) -------------------
def _doc_to_proj(session: Session, proj_ids: set) -> dict:
    """document_id -> project_id for the in-scope projects' documents (chunked IN()).
    THE scoping boundary for the LQAResult / SegmentPostEdit readers: a child row whose
    document is not in this map is silently dropped, so a qa/postedit report can never
    sum a foreign tenant's findings/edits (the same isolation shape _segment_metrics and
    _vendor_buckets use — the confined proj_ids the caller resolved)."""
    out: dict = {}
    if not proj_ids:
        return out
    pid_list = list(proj_ids)
    CHUNK = 150
    for i in range(0, len(pid_list), CHUNK):
        batch = pid_list[i : i + CHUNK]
        for did, pid in session.exec(
            select(Document.id, Document.project_id).where(Document.project_id.in_(batch))
        ).all():
            out[did] = pid
    return out


def _pe_zero() -> dict:
    return {
        "episodes": 0,
        "edits": 0,
        "words_derived": 0,
        "mt_chars": 0,
        "final_chars": 0,
        "distance_char": 0,
        "distance_word": 0,
        "tte_ms": 0,
        "_seg_ids": set(),
        "_usable_seg_ids": set(),
        "_pct": [],
        "_metric_versions": set(),
        "_unit_kinds": set(),
    }


def _round_total(v):
    """The ONE rounding rule both the CSV footer (to_csv) and the on-screen/JSON footer
    (totals_row) apply to a summed total, so they can never diverge (M2: a float sum like
    0.30000000000000004 rounds to 0.3 identically in both). A float total (cost_usd on the
    standard axes; estimate/actual on the vendor axis) rounds to 6dp; an int counter total
    stays an int."""
    return round(v, 6) if isinstance(v, float) else v


def _csv_cell(v):
    """Render a cell: cost as fixed 6dp string is avoided (keep the numeric round);
    everything else as-is. csv.writer stringifies."""
    return v


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def csv_filename(kind: str, group_by: str = "none", when: date | None = None) -> str:
    """A sanitized attachment filename: loc-report-<kind>[-<group_by>]-<YYYYMMDD>.csv.
    Only ASCII [A-Za-z0-9._-]; anything else collapses to '-'. Never path-traversal
    (no slashes survive the sanitizer)."""
    when = when or date.today()
    kind = _FILENAME_SAFE.sub("-", (kind or "report")).strip("-") or "report"
    gb = _FILENAME_SAFE.sub("-", (group_by or "")).strip("-")
    stamp = when.strftime("%Y%m%d")
    mid = f"{kind}-{gb}" if gb and gb != "none" else kind
    # Collapse any run of dots (a stray "." or ".." from a hostile kind/group_by) to
    # a single "-" so no path-traversal token survives, then normalize separators.
    mid = re.sub(r"\.+", "-", mid).strip("-.") or "report"
    name = f"loc-report-{mid}-{stamp}.csv"
    return name


def parse_date(value) -> date | None:
    """Parse a YYYY-MM-DD string (or pass a date through). None/blank -> None.
    Raises ValueError on a malformed non-empty string (the caller maps to a clean
    usage/400 error)."""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
