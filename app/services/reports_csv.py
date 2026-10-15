"""reports: csv

Documented in: docs/architecture/reporting-and-dashboard.md
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlmodel import Session, select

from app import output_csv as csv
from app.models import (
    Document,
    Job,
    LQAResult,
    SegmentPostEdit,
)
from app.services.maintenance import admitted_work
from app.services.reports_the_query import (
    _KIND_COLUMNS,
    _NON_SUMMABLE_COLUMNS,
    _PE_QUERY_COLS,
    _QA_QUERY_COLS,
    _QA_SEVERITIES,
    _VENDOR_COST_COLS,
    _VENDOR_LQA_COLS,
    _VENDOR_QUALITY_COLS,
    GROUP_BYS,
    KINDS,
    _axis_key,
    _can_see_human_spend,
    _csv_cell,
    _derived_words,
    _doc_to_proj,
    _document_project_id,
    _dt_hi,
    _dt_lo,
    _int_or_none,
    _pe_zero,
    _round_total,
    _run_date,
    _supported_axis,
    _vendor_caveats,
    _zero_vendor_metrics,
    validate_kind,
)

_METRIC_KEYS = (
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
)


_FLOAT_METRIC_KEYS = frozenset({"cost_usd", "human_cost_usd"})


# The full set of metric keys every aggregate row carries (computed once regardless
# of kind); render picks a subset per kind.
# The metric keys carried as FLOAT USD (zeroed to 0.0, rounded on render); every other
# key is an integer counter.
@dataclass
class ReportQuery:
    """A report request. Pure data - no Request, no identity (that is passed to
    run_report separately, like get_customer_360)."""

    kind: str = "cost"
    group_by: str = "none"
    date_from: date | None = None  # inclusive lower bound (on job date)
    date_to: date | None = None  # inclusive upper bound (on job date)
    customer_id: int | None = None  # filter to one customer (clamped if bound)
    project_id: int | None = None  # filter to one project
    provider: str | None = None  # filter to one provider name
    # SPEC-wave2 §4.3: set by normalized() when a KNOWN axis is not supported by the kind
    # and was coerced to 'none'. Carries the original axis name so the caveat surface can
    # name the degradation (a truly-unknown axis is coerced silently, as before).
    degraded_axis: str | None = None

    def normalized(self) -> ReportQuery:
        """A copy with kind validated STRICTLY and group_by coerced leniently.

        kind: raises UnknownReportKind on an unrecognized value (issue #25) — a typo
        must fail loudly on every surface, not silently run the WRONG (cost) report.
        group_by stays lenient (an unknown grouping degrades to 'none'): unlike kind,
        a bad group_by changes only ROW BUCKETING, never which metrics or whose data
        are shown, so a clean default is harmless and keeps the web/api forgiving of a
        stale bookmark.

        Wave 2 (§4.3): a group_by that IS a real axis but is not supported by the chosen
        kind (e.g. `--kind cost --group-by dimension`, `--kind qa --group-by provider`)
        also degrades to 'none', but LOUDLY — `degraded_axis` records the original so the
        caveat surface names it. Idempotent: re-normalizing a already-degraded query
        preserves `degraded_axis` (surfaces normalize once, run_report normalizes again)."""
        kind = validate_kind(self.kind)
        gb = (self.group_by or "none").strip().lower()
        gb = gb if gb in GROUP_BYS else "none"
        degraded = self.degraded_axis
        if gb != "none" and not _supported_axis(kind, gb):
            degraded = gb
            gb = "none"
        return ReportQuery(
            kind=kind,
            group_by=gb,
            date_from=self.date_from,
            date_to=self.date_to,
            customer_id=self.customer_id,
            project_id=self.project_id,
            provider=(self.provider or None) or None,
            degraded_axis=degraded,
        )


def _in_range(d: date | None, q: ReportQuery) -> bool:
    """True if a bucketed date is inside the query's [date_from, date_to] window
    (inclusive). A None date fails a set bound (mirrors the job loop's guard)."""
    if q.date_from is not None and (d is None or d < q.date_from):
        return False
    if q.date_to is not None and (d is None or d > q.date_to):  # noqa: SIM103
        return False
    return True


def _fetch_scoped_runs(session: Session, q: ReportQuery, project_ids: set):
    """(runs, srs_by_run, doc_to_proj) for the WorkflowRuns of the scoped projects,
    date-window-filtered — the ONE fetch both the workflow axis and the non-workflow
    overlay share.

    F1 (availability): projects ONLY the small columns needed. StepRun carries
    input_text/output_text (~21 KB each on a big-doc run); a `select(StepRun)` on the
    default no-date group_by=none report (viewer- and api-key-reachable) pulled those
    blobs for every step and measured +262 MB on 2000 runs × 3 steps — on a 1 GB box.
    We project StepRun -> (workflow_run_id, provider_used, cost_usd, tokens_in,
    tokens_out) and WorkflowRun -> (id, document_id, workflow_id, finished_at,
    started_at), so neither blob nor the WorkflowRun.context/notes JSON ever loads.

    The run-date window (COALESCE(finished_at, started_at) — the finished-or-started
    convention _run_date encodes) is pushed into SQL as a COARSE bound WIDENED a day on
    each side (tz/storage-format safety: SQLite compares datetimes lexically, so a naive
    tight bound could drop a boundary row). The precise, authoritative filter stays the
    Python `_in_range(_run_date(r), q)` below — the SQL bound only cuts the bulk of
    out-of-window rows before they load, and can never itself exclude an in-window one.

    Returns Row objects (column projections), which `_run_date` and the callers read by
    attribute exactly like the ORM instances they replaced."""
    from app.models import StepRun, WorkflowRun

    if not project_ids:
        return [], {}, {}
    docs = session.exec(
        select(Document.id, Document.project_id).where(Document.project_id.in_(project_ids))
    ).all()
    doc_to_proj = {d[0]: d[1] for d in docs}
    if not doc_to_proj:
        return [], {}, {}
    doc_ids = list(doc_to_proj.keys())
    from sqlalchemy import func as _sqlfunc

    run_date_col = _sqlfunc.coalesce(WorkflowRun.finished_at, WorkflowRun.started_at)
    lo = (
        datetime.combine(q.date_from - timedelta(days=1), time.min)
        if q.date_from is not None
        else None
    )
    hi = (
        datetime.combine(q.date_to + timedelta(days=1), time.max) if q.date_to is not None else None
    )
    CHUNK = 150
    runs = []
    for i in range(0, len(doc_ids), CHUNK):
        batch = doc_ids[i : i + CHUNK]
        stmt = select(
            WorkflowRun.id,
            WorkflowRun.document_id,
            WorkflowRun.workflow_id,
            WorkflowRun.finished_at,
            WorkflowRun.started_at,
            # target_locale (additive projection): the locale axis buckets a
            # run by the locale it was launched for (one member run per
            # requested locale). Harmless to every other caller (extra Row
            # attribute, read only where needed).
            WorkflowRun.target_locale,
        ).where(WorkflowRun.document_id.in_(batch))
        if lo is not None:
            stmt = stmt.where(run_date_col >= lo)
        if hi is not None:
            stmt = stmt.where(run_date_col <= hi)
        runs.extend(session.exec(stmt).all())
    runs = [r for r in runs if _in_range(_run_date(r), q)]  # precise + authoritative
    if not runs:
        return [], {}, doc_to_proj
    run_ids = [r.id for r in runs]
    srs_by_run: dict[int, list] = {}
    for i in range(0, len(run_ids), CHUNK):
        batch = run_ids[i : i + CHUNK]
        for sr in session.exec(
            select(
                StepRun.workflow_run_id,
                StepRun.provider_used,
                StepRun.cost_usd,
                StepRun.tokens_in,
                StepRun.tokens_out,
            ).where(StepRun.workflow_run_id.in_(batch))
        ).all():
            srs_by_run.setdefault(sr.workflow_run_id, []).append(sr)
    return runs, srs_by_run, doc_to_proj


def _zero_metrics() -> dict:
    return {k: (0.0 if k in _FLOAT_METRIC_KEYS else 0) for k in _METRIC_KEYS}


def _vendor_buckets(session: Session, q: ReportQuery, proj_ids: set) -> dict:
    """The `--group-by vendor` axis, sourced from VendorDispatch legs of the in-scope
    projects. Per vendor: legs (dispatch count), word_count (Σ leg word_count),
    returned_on_time (legs whose signed return arrived on or before expiry:
    returned_at <= expires_at, both set), and estimate/actual HUMAN (MSA) cost.

    SCOPING: a leg is included only when its document (leg.document_id, else its
    workflow_run's document) belongs to `proj_ids` — the SAME already-confined project
    set (org + membership + R2-3 customer + explicit filters) run_report resolves for
    every axis, so a vendor bucket can never sum a foreign tenant's legs. The `provider`
    filter is an AI-provider concept and does NOT apply to a human vendor leg (a caveat
    says so). Legs are date-filtered on dispatched_at (the leg's own event date)."""
    from app.models import LQAResult, Vendor, VendorDispatch, VendorDispatchStatus, WorkflowRun
    from app.services import lqa

    # IN-FLIGHT = money committed/estimated but not yet resolved (open exposure): the leg is
    # still workable or parked, not returned and not aborted. Excludes RETURNED (the
    # returned-only estimate/actual columns) and the aborted/superseded terminals
    # CANCELLED/DEAD_LETTER/REJECTED (no live exposure).
    _INFLIGHT = frozenset(
        {
            VendorDispatchStatus.DISPATCHED,
            VendorDispatchStatus.QUOTED,
            VendorDispatchStatus.RETRY_WAIT,
            VendorDispatchStatus.ESCALATED,
        }
    )
    # An APPROVED quote on an aborted/superseded leg is not honest spend (a REJECTED leg is
    # superseded by its rework, which carries its own approved quote — counting both would
    # double-count), so exclude the same terminals the returned-only rule excludes.
    _QUOTE_EXCLUDED = frozenset(
        {
            VendorDispatchStatus.CANCELLED,
            VendorDispatchStatus.DEAD_LETTER,
            VendorDispatchStatus.REJECTED,
        }
    )
    buckets: dict[str, dict] = {}
    if not proj_ids:
        return buckets
    # Chunk the IN() at 150 (SQLite variable limit), like the sibling segment/run
    # fetchers. (Legs are loaded install-wide below and filtered by scoped_docs in Python
    # — acceptable while dispatch legs are far fewer than jobs/segments; the filter, not
    # the load, is the isolation boundary.)
    scoped_docs: set = set()
    proj_id_list = list(proj_ids)
    CHUNK = 150
    for i in range(0, len(proj_id_list), CHUNK):
        batch = proj_id_list[i : i + CHUNK]
        scoped_docs.update(
            session.exec(select(Document.id).where(Document.project_id.in_(batch))).all()
        )
    vendor_names: dict[int, str] = {}
    run_doc_cache: dict[int, int | None] = {}

    def _leg_doc(leg) -> int | None:
        if leg.document_id is not None:
            return leg.document_id
        rid = leg.workflow_run_id
        if rid not in run_doc_cache:
            run = session.get(WorkflowRun, rid) if rid else None
            run_doc_cache[rid] = run.document_id if run else None
        return run_doc_cache[rid]

    for leg in session.exec(select(VendorDispatch)).all():
        if _leg_doc(leg) not in scoped_docs:
            continue
        dd = (
            leg.dispatched_at.date()
            if isinstance(leg.dispatched_at, datetime)
            else leg.dispatched_at
        )
        if q.date_from is not None and (dd is None or dd < q.date_from):
            continue
        if q.date_to is not None and (dd is None or dd > q.date_to):
            continue
        vid = leg.vendor_id
        if vid not in vendor_names:
            v = session.get(Vendor, vid)
            vendor_names[vid] = v.name if v else f"vendor#{vid}"
        key = f"{vid}: {vendor_names[vid]}"
        row = buckets.setdefault(key, _zero_vendor_metrics())
        # legs + word_count count EVERY dispatch (all statuses) — the volume of work sent.
        row["legs"] += 1
        row["word_count"] += int(leg.word_count or 0)
        if (
            leg.returned_at is not None
            and leg.expires_at is not None
            and leg.returned_at <= leg.expires_at
        ):
            row["returned_on_time"] += 1
        # M3: MONEY (estimate/actual) sums ONLY legs whose return was ACCEPTED (status
        # RETURNED), so a rejected->rework pair never double-counts and a
        # cancelled/dead-letter/in-flight leg never reads as spend you owe. This makes
        # the two money columns an apples-to-apples estimate-vs-actual over the SAME
        # (returned) leg set. The disclosure caveat states this in/out rule explicitly.
        if leg.status == VendorDispatchStatus.RETURNED:
            row["estimate_cost_usd"] += float(leg.estimate_cost_usd or 0.0)
            if leg.actual_cost_usd is not None:
                row["actual_cost_usd"] += float(leg.actual_cost_usd)
        # 2026-08-19: open in-flight estimate exposure (the returned-only columns
        # above cannot see it) — so an in-flight-only vendor no longer reads as free.
        if leg.status in _INFLIGHT:
            row["open_estimate_usd"] += float(leg.estimate_cost_usd or 0.0)
        # 2026-08-19: the operator-APPROVED quote figure (Wave E) — a person
        # authorized this spend. Same 'quote' grade vendor_spend.leg_human_cost recognizes
        # (quote_approved_at + quoted_cost_usd), minus the aborted/superseded terminals. It
        # is a SEPARATE column from estimate/actual — a leg carrying both a quote and an
        # estimate contributes to each, never blended into one figure.
        if (
            leg.quote_approved_at is not None
            and leg.quoted_cost_usd is not None
            and leg.status not in _QUOTE_EXCLUDED
        ):
            row["quoted_cost_usd"] += float(leg.quoted_cost_usd or 0.0)

    # SPEC-wave2 §4.6: the VR-5 quality columns. LQAResult rows attributed to a vendor
    # (LQAResult.vendor_id set by Train A's lqa_attribution) that belong to an in-scope
    # document, WINDOWED ON THEIR OWN created_at (the M7 window-divergence caveat: leg
    # columns window on dispatched_at, quality columns on created_at). A vendor that has
    # findings but no leg in the window gets a bucket too (its quality is still visible;
    # its leg columns read 0), and a vendor with legs but no LQA reads 0/0/"" (rule 3).
    # F1 (MED-1): the _VENDOR_LQA_COLS projection never loads the `note` blob; the load is
    # chunk-filtered on the confined documents + vendor_id NOT NULL + the created_at window
    # pushed into SQL. All rows of a card carry an identical score by construction, so
    # first-seen (setdefault) per distinct scoring_id is deterministic without an ORDER BY.
    lo, hi = _dt_lo(q.date_from), _dt_hi(q.date_to)
    scoped_doc_list = list(scoped_docs)
    for i in range(0, len(scoped_doc_list), CHUNK):
        dbatch = scoped_doc_list[i : i + CHUNK]
        stmt = select(*_VENDOR_LQA_COLS).where(
            LQAResult.document_id.in_(dbatch), LQAResult.vendor_id.is_not(None)
        )
        if lo is not None:
            stmt = stmt.where(LQAResult.created_at >= lo)
        if hi is not None:
            stmt = stmt.where(LQAResult.created_at <= hi)
        for r in session.exec(stmt).all():
            # WAVE 3 §6.6.5: a withdrawn reviewer finding never scores a vendor either.
            if r.withdrawn_at is not None:
                continue
            vid = r.vendor_id
            if vid not in vendor_names:
                v = session.get(Vendor, vid)
                vendor_names[vid] = v.name if v else f"vendor#{vid}"
            key = f"{vid}: {vendor_names[vid]}"
            row = buckets.setdefault(key, _zero_vendor_metrics())
            row["lqa_findings"] += 1
            if lqa.canon_severity(r.severity) in ("major", "critical"):
                row["lqa_major_critical"] += 1
            if r.scoring_id:
                row["_lqa_card_scores"].setdefault(r.scoring_id, r.score)
    return buckets


def _finish_vendor(buckets: dict, *, q: ReportQuery, user=None) -> dict:
    """Shape vendor buckets into the scorecard columns. The estimate/actual HUMAN-spend
    columns are dropped for any ident that may not read vendor margin (non-admin /
    customer-bound), exactly like human_cost_usd — never serialized to columns/rows/CSV
    for them. Deterministic row order (by key)."""
    show_cost = _can_see_human_spend(user)
    # §4.6: quality columns sit AFTER the operational leg columns and BEFORE the money
    # pair, so the admin-only cost slice stays last and its gate is untouched.
    metric_cols = ["legs", "word_count", "returned_on_time"] + list(_VENDOR_QUALITY_COLS)  # noqa: RUF005
    if show_cost:
        metric_cols += list(_VENDOR_COST_COLS)
    columns = ["vendor"] + metric_cols  # noqa: RUF005
    rows = []
    for key in sorted(buckets.keys()):
        m = buckets[key]
        row = {"vendor": _axis_key(key)}  # E1: sanitize the vendor key too
        for c in metric_cols:
            if c == "avg_lqa_score":
                # NULL != 0 (rule 3): mean over distinct scored cards, or "" when the
                # vendor has no card-identified finding — never a false 0.0.
                scores = [s for s in m.get("_lqa_card_scores", {}).values() if s is not None]
                row[c] = round(sum(scores) / len(scores), 4) if scores else ""
                continue
            v = m[c]
            row[c] = round(v, 6) if c in _VENDOR_COST_COLS else v
        rows.append(row)
    return {
        "columns": columns,
        "rows": rows,
        "key_column": "vendor",
        "kind": q.kind,
        "group_by": "vendor",
        "caveats": _vendor_caveats(show_cost),
    }


def _degrade_caveat(q: ReportQuery) -> list:
    """SPEC-wave2 §4.3: when a KNOWN axis was not supported by the chosen kind and was
    coerced to 'none', say so LOUDLY. Empty otherwise. Shared by every kind's caveat
    builder (cost/tokens/volume via _caveats, qa/postedit via their own), so the
    degradation reads identically on the web caption, the CSV NOTE footer and the /api
    caveats field. `degraded_axis` is only ever a KNOWN axis name (set by normalized), so
    it is safe ASCII — no sanitization needed."""
    if q.degraded_axis:
        return [
            f"axis '{q.degraded_axis}' is not available for the '{q.kind}' report kind; showing an "
            "ungrouped ('none') report instead."
        ]
    return []


def _job_project_id(session: Session, job: Job):
    """The project id a job attributes to. Tried in order (cheapest / most
    direct first):

      1. payload.project_id - the cheap, no-join path for any caller that
         passes it explicitly (kept first for callers that DO set it, and for
         the test fixture shape).
      2. payload.document_id -> Document.project_id - what `translate_document`
         jobs ACTUALLY carry in production (web app/web/documents.py:182 submits
         {document_id, locale}; the API front door app/web/api.py:706 submits
         {document_id, locale, est_tokens}; NEITHER ever sets project_id). Without
         this fallback every real translate job returns None here and is silently
         skipped by run_report -> cost/token reports are EMPTY on a live install
         (the CRITICAL finding this fixes).
      3. payload.connection_id -> Connection.project_id - `connector_translate`
         jobs (app/services/jobs.py:116 `_run_connector_translate`) carry
         connection_id, never project_id or document_id directly. Every
         connector flow (sync/dynamic/jira/confluence) resolves its documents
         under Connection.project_id via `_ensure_project` at connect/first-run
         time, so the connection's project_id is the stable, ALWAYS-present
         attribution anchor for connector spend (confluence in particular
         creates one Document PER PAGE per run, so there is no single
         result.document_id to key off for that connector kind - the
         connection's project_id covers all four connector kinds uniformly).
      4. result.document_id -> Document.project_id - a belt-and-suspenders
         fallback for a connector job whose payload predates connection_id
         (or any future handler shape) but whose result happens to carry a
         document_id (sync_connection/dynamic_translate/jira_translate all do).

    Returns None only when NONE of the above resolve - e.g. a connection was
    deleted after the job ran, or a malformed payload. Such a job is currently
    dropped from the report (documented v1 gap: there is no per-job customer/org
    to fold it into an "(unattributed)" bucket without a project, so silent
    omission is the only safe option; in practice this needs the connection
    row itself to be gone, which does not happen in the shipped flows above)."""
    payload = job.payload or {}
    pid = _int_or_none(payload.get("project_id"))
    if pid is not None:
        return pid
    doc_id = payload.get("document_id")
    if doc_id is not None:
        pid = _document_project_id(session, doc_id)
        if pid is not None:
            return pid
    cid = payload.get("connection_id")
    if cid is not None:
        cid = _int_or_none(cid)
        if cid is not None:
            from app.services.connectors.store import get as _get_connection

            conn = _get_connection(cid)
            if conn is not None and conn.project_id is not None:
                return conn.project_id
    result = job.result or {}
    doc_id = result.get("document_id")
    if doc_id is not None:
        pid = _document_project_id(session, doc_id)
        if pid is not None:
            return pid
    return None


# --- RV-2: the `locale` axis (target-locale attribution) ---------------------
def _segment_metrics_by_locale(session: Session, project_ids: set) -> dict:
    """Per-TARGET-LOCALE {segments, chars, words_derived} over TRANSLATED segments only
    (non-empty target_text), across the in-scope projects. Unlike _segment_metrics (which
    keys by project and the caller folds once per project), this keys by
    Segment.target_locale directly — so the locale axis attributes each translated
    segment's volume to the ACTUAL locale it was translated into (no project-level fold
    approximation). Same TRANSLATED-only + derived-words rules as _segment_metrics."""
    from app.models import Segment

    out: dict[str, dict] = {}
    if not project_ids:
        return out
    docs = session.exec(
        select(Document.id, Document.project_id).where(Document.project_id.in_(project_ids))
    ).all()
    doc_ids = [d[0] for d in docs]
    if not doc_ids:
        return out
    CHUNK = 150
    for i in range(0, len(doc_ids), CHUNK):
        batch = doc_ids[i : i + CHUNK]
        # Issue #53 (round-2 A5): projected tuple (target_locale/source_text/target_text)
        # in place of `select(Segment)`, same reasoning as _segment_metrics — this axis keys
        # by target_locale, sums source lengths/words, and gates on target_text's exact
        # Python `.strip()`. The full-row JSON blobs + lock metadata are no longer loaded;
        # output is byte-identical.
        for s in session.exec(
            select(Segment.target_locale, Segment.source_text, Segment.target_text).where(
                Segment.document_id.in_(batch)
            )
        ).all():
            if not (s.target_text or "").strip():
                continue  # only TRANSLATED segments count toward derived volume
            loc = s.target_locale or "(untracked locale)"
            m = out.setdefault(loc, {"segments": 0, "chars": 0, "words_derived": 0})
            m["segments"] += 1
            m["chars"] += len(s.source_text or "")
            m["words_derived"] += _derived_words(s.source_text or "")
    return out


def _qa_zero() -> dict:
    m = {"findings": 0}
    for s in _QA_SEVERITIES:
        m[s] = 0
    m["human"] = 0  # WAVE 3 §6.6.3: non-withdrawn reviewer-authored findings
    m["_card_scores"] = {}  # scoring_id -> score (distinct cards; internal)
    m["_regimes"] = set()  # 'seg' (score_document REPLACE) / 'doc' (workflow APPEND)
    return m


def _qa_buckets(
    session: Session, q: ReportQuery, projects: dict, proj_ids: set, *, unrestricted: bool = False
) -> dict:
    """The `kind=qa` aggregate over LQAResult (SPEC-wave2 §4.2), bucketed by the active
    axis and date-windowed on created_at. Returns {"buckets":..., "meta":...} where meta
    carries the report-level caveat flags. Provider filter does NOT apply (a caveat says
    so); scoping rides entirely on _doc_to_proj.

    F1 (MED-1): rows are read with the _QA_QUERY_COLS projection (never the `note` blob),
    chunk-filtered on document_id.in_(scoped) + the created_at window pushed into SQL, so a
    foreign tenant's rows never load. MED-5: a doc-less LQAResult (document_id NULL) cannot
    be attributed to a project, so it is included ONLY for an unrestricted caller (who sees
    every tenant anyway) as '(untracked project)'/'(no customer)' — never leaked to a
    scope-confined caller — while its other axis fields still bucket honestly."""
    from app.models import Customer as _Customer
    from app.models import Vendor
    from app.services import lqa

    buckets: dict[str, dict] = {}
    meta = {
        "any": False,
        "has_null_scoring": False,
        "mixed_regime": False,
        "provider": bool(q.provider),
        "has_human": False,
        "human_count": 0,
    }
    if not proj_ids:
        return {"buckets": buckets, "meta": meta}
    doc_to_proj = _doc_to_proj(session, proj_ids)
    if not doc_to_proj:
        return {"buckets": buckets, "meta": meta}
    cust_name_by_id = {c.id: c.name for c in session.exec(select(_Customer)).all()}
    vendor_names: dict[int, str] = {}
    gb = q.group_by
    lo, hi = _dt_lo(q.date_from), _dt_hi(q.date_to)

    def _vname(vid):
        if vid not in vendor_names:
            v = session.get(Vendor, vid)
            vendor_names[vid] = v.name if v else f"vendor#{vid}"
        return vendor_names[vid]

    def _key_for(r, pid):
        cd = r.created_at.date() if isinstance(r.created_at, datetime) else r.created_at
        proj = projects.get(pid) if pid is not None else None
        if gb == "project":
            return f"{pid}: {proj.name}" if proj else "(untracked project)"
        if gb == "customer":
            if proj is None or proj.customer_id is None:
                return "(no customer)"
            return f"{proj.customer_id}: {cust_name_by_id.get(proj.customer_id, '?')}"
        if gb == "locale":
            # N5: a real locale, or BOTH "" and NULL -> the shared (untracked locale) key.
            return r.target_locale or "(untracked locale)"
        if gb == "vendor":
            return f"{r.vendor_id}: {_vname(r.vendor_id)}" if r.vendor_id else "(no vendor)"
        if gb == "dimension":
            # MED-2: canon_dimension is never falsy (a blank folds to "Other/"), so guard
            # the empty case explicitly rather than rely on a dead `or` fallback.
            return (
                lqa.canon_dimension(r.dimension)
                if (r.dimension or "").strip()
                else "(untracked dimension)"
            )
        if gb == "actor":
            # WAVE 3 §6.6.4: a HUMAN finding buckets on its `actor` (the reviewer's email)
            # — that is what makes the qa actor axis PII, exactly like the postedit axis, so
            # the always-on manager+ gate (api_cli) already covers it. A machine finding
            # keeps the finding_source + judge split (§4.3).
            if r.finding_source == "human":
                return r.actor or "(untracked actor)"
            if r.finding_source == "llm":
                return f"llm: {r.judge}" if r.judge else "llm"
            if r.finding_source:
                return r.finding_source
            return "(untracked actor)"
        if gb == "date":
            return cd.isoformat() if cd else "(undated)"
        return "all"

    def _accumulate(r, pid):
        # WAVE 3 §6.6.5: a WITHDRAWN reviewer finding is a retracted judgement — excluded
        # from every qa bucket (the LQA page drops it too). It is NOT deleted; the
        # append-only lakehouse still holds it (the declared L5 asymmetry). Machine rows
        # have withdrawn_at IS NULL, so this only ever drops human retractions.
        if getattr(r, "withdrawn_at", None) is not None:
            return
        b = buckets.setdefault(_key_for(r, pid), _qa_zero())
        b["findings"] += 1
        b[lqa.canon_severity(r.severity)] += 1
        if r.finding_source == "human":
            # WAVE 3 §6.6.1: a hand-logged bug counts in findings + its severity + `human`,
            # and sets has_human — but it NEVER touches _card_scores (so `cards` and
            # avg_card_score stay machine-card metrics, un-inflatable by hand-logged bugs)
            # and its NULL scoring_id must NOT raise has_null_scoring (that flag means
            # "predates Wave 2", which a human row does not — §6.6.2).
            b["human"] += 1
            meta["has_human"] = True
            meta["human_count"] += 1
        elif r.scoring_id:
            b["_card_scores"].setdefault(r.scoring_id, r.score)
        else:
            meta["has_null_scoring"] = True
        b["_regimes"].add("seg" if r.segment_id is not None else "doc")
        meta["any"] = True

    # Scoped rows: projected columns, chunk-filtered on the confined documents + SQL window.
    scoped_docs = list(doc_to_proj.keys())
    CHUNK = 150
    for i in range(0, len(scoped_docs), CHUNK):
        batch = scoped_docs[i : i + CHUNK]
        stmt = select(*_QA_QUERY_COLS).where(LQAResult.document_id.in_(batch))
        if lo is not None:
            stmt = stmt.where(LQAResult.created_at >= lo)
        if hi is not None:
            stmt = stmt.where(LQAResult.created_at <= hi)
        for r in session.exec(stmt).all():
            _accumulate(r, doc_to_proj.get(r.document_id))

    # MED-5: doc-less rows, unrestricted callers only (tenant-safe — never for a scoped one,
    # and never for an operator whose report is narrowed by a project/customer filter).
    if unrestricted:
        stmt = select(*_QA_QUERY_COLS).where(LQAResult.document_id.is_(None))
        if lo is not None:
            stmt = stmt.where(LQAResult.created_at >= lo)
        if hi is not None:
            stmt = stmt.where(LQAResult.created_at <= hi)
        for r in session.exec(stmt).all():
            _accumulate(r, None)
            meta["docless"] = True

    meta["mixed_regime"] = any(len(b["_regimes"]) > 1 for b in buckets.values())
    return {"buckets": buckets, "meta": meta}


def _postedit_buckets(session: Session, q: ReportQuery, projects: dict, proj_ids: set) -> dict:
    """The `kind=postedit` aggregate over SegmentPostEdit (SPEC-wave2 §4.2). The MEASURE
    depends on the axis (H3): the actor/vendor axes report the EPISODE-LOCAL distance from
    each episode row (the only figure truthfully attributable to one actor), every other
    axis reports the CUMULATIVE machine->final distance ONCE per segment from the
    max-episode row. On the multi-bucket axes the segment-scoped columns are dropped (N7,
    handled in _finish_postedit) so TOTALS never double-counts. Returns
    {"buckets":..., "meta":...}. Provider filter does not apply."""
    from app.models import Customer as _Customer
    from app.models import Segment, Vendor

    buckets: dict[str, dict] = {}
    gb = q.group_by
    multi = gb in ("actor", "vendor")
    meta = {
        "any": False,
        "multi": multi,
        "mixed_unit": False,
        "mixed_metric": False,
        "has_none_baseline": False,
        "no_machine_origin": 0,
        "provider": bool(q.provider),
    }
    if not proj_ids:
        return {"buckets": buckets, "meta": meta}
    doc_to_proj = _doc_to_proj(session, proj_ids)
    if not doc_to_proj:
        return {"buckets": buckets, "meta": meta}
    cust_name_by_id = {c.id: c.name for c in session.exec(select(_Customer)).all()}
    vendor_names: dict[int, str] = {}

    def _vname(vid):
        if vid not in vendor_names:
            v = session.get(Vendor, vid)
            vendor_names[vid] = v.name if v else f"vendor#{vid}"
        return vendor_names[vid]

    def _in_window(ts):
        d = ts.date() if isinstance(ts, datetime) else ts
        if q.date_from is not None and (d is None or d < q.date_from):
            return False
        if q.date_to is not None and (d is None or d > q.date_to):  # noqa: SIM103
            return False
        return True

    # Load every in-scope PE row, grouped by segment so the CUMULATIVE path can find the
    # true origin (episode 1) even when it is out of the date window. F1 (MED-1): the
    # _PE_QUERY_COLS projection NEVER loads baseline_text (the highest-cardinality blob in
    # the product) or final_sha, and the load is chunk-filtered on the confined documents
    # so a foreign tenant's episodes never enter the process. The per-segment date filter
    # stays in Python (it keys off the max-episode row, which SQL cannot pre-select).
    from collections import defaultdict

    by_seg: dict = defaultdict(list)
    scoped_docs = list(doc_to_proj.keys())
    CHUNK = 150
    for i in range(0, len(scoped_docs), CHUNK):
        batch = scoped_docs[i : i + CHUNK]
        for r in session.exec(
            select(*_PE_QUERY_COLS).where(SegmentPostEdit.document_id.in_(batch))
        ).all():
            by_seg[r.segment_id].append(r)

    def _proj_customer_key(pid):
        proj = projects.get(pid)
        if gb == "project":
            return f"{pid}: {proj.name}" if proj else "(unknown project)"
        if gb == "customer":
            if proj is None or proj.customer_id is None:
                return "(no customer)"
            return f"{proj.customer_id}: {cust_name_by_id.get(proj.customer_id, '?')}"
        return None

    if multi:
        # EPISODE-LOCAL: each episode row buckets by its actor/vendor, on its OWN date.
        for seg_id, eps in by_seg.items():
            for r in eps:
                if not _in_window(r.last_edited_at):
                    continue
                if gb == "vendor":
                    key = f"{r.vendor_id}: {_vname(r.vendor_id)}" if r.vendor_id else "(no vendor)"
                else:  # actor
                    if r.actor_kind == "vendor":
                        key = (
                            f"vendor: {_vname(r.vendor_id)}" if r.vendor_id else "(untracked actor)"
                        )
                    elif r.actor:
                        key = r.actor
                    else:
                        key = "(untracked actor)"
                b = buckets.setdefault(key, _pe_zero())
                b["episodes"] += 1
                b["edits"] += r.edit_count
                b["_seg_ids"].add(seg_id)
                b["_metric_versions"].add(r.metric_version)
                b["_unit_kinds"].add(r.unit_kind)
                if r.baseline_kind == "none":
                    meta["has_none_baseline"] = True
                if r.distance_char is not None:
                    b["_usable_seg_ids"].add(seg_id)
                    b["distance_char"] += r.distance_char
                    b["distance_word"] += r.distance_word or 0
                    b["mt_chars"] += r.baseline_chars  # episode-scoped (N7)
                    b["final_chars"] += r.final_chars
                    if r.distance_pct is not None:
                        b["_pct"].append(r.distance_pct)
                meta["any"] = True
    else:
        # CUMULATIVE: once per segment, from the max-episode row (current state). The
        # segment reports wholly in the window of its LATEST edit (§4.3).
        for seg_id, eps in by_seg.items():
            eps.sort(key=lambda e: e.episode_seq)
            org, cur = eps[0], eps[-1]
            if not _in_window(cur.last_edited_at):
                continue
            if gb == "locale":
                key = cur.target_locale or "(untracked locale)"
            elif gb == "date":
                cd = (
                    cur.last_edited_at.date()
                    if isinstance(cur.last_edited_at, datetime)
                    else cur.last_edited_at
                )
                key = cd.isoformat() if cd else "(undated)"
            elif gb in ("project", "customer"):
                key = _proj_customer_key(doc_to_proj.get(cur.document_id))
            else:
                key = "all"
            b = buckets.setdefault(key, _pe_zero())
            b["episodes"] += len(eps)
            b["edits"] += sum(e.edit_count for e in eps)
            b["words_derived"] += org.source_words or 0
            b["_seg_ids"].add(seg_id)
            for e in eps:
                b["_metric_versions"].add(e.metric_version)
                b["_unit_kinds"].add(e.unit_kind)
            # HIGH-1: the two disclosures are DISTINCT and keyed to their real predicates.
            #  * has_none_baseline = ANY episode started from an empty target (the
            #    "no baseline" fact), not just the origin.
            #  * no_machine_origin = the segment is EXCLUDED from the cumulative distance
            #    (cum is None) because episode 1's baseline was not mt/tm. Train A NULLs
            #    cum_* for EVERY non-mt/tm origin — human included — so a human-origin
            #    segment is silently zeroed on the cumulative axes unless this fires.
            if any(e.baseline_kind == "none" for e in eps):
                meta["has_none_baseline"] = True
            if cur.cum_distance_char is not None:
                b["_usable_seg_ids"].add(seg_id)
                b["distance_char"] += cur.cum_distance_char
                b["distance_word"] += cur.cum_distance_word or 0
                b["mt_chars"] += org.baseline_chars  # origin baseline length
                b["final_chars"] += cur.final_chars  # current final length
                if cur.cum_distance_pct is not None:
                    b["_pct"].append(cur.cum_distance_pct)
            else:
                meta["no_machine_origin"] += 1
            meta["any"] = True

        # tte_ms (§4.2): Σ Segment.tte_ms over the bucket's distinct segments. Load ONLY
        # id + tte_ms (no text) so the F1 memory law is respected (the reason this kind
        # uses a side table at all). Only on the cumulative axes (dropped on multi, N7).
        all_seg_ids = set()
        for b in buckets.values():
            all_seg_ids |= b["_seg_ids"]
        tte_map: dict = {}
        seg_id_list = list(all_seg_ids)
        CHUNK = 150
        for i in range(0, len(seg_id_list), CHUNK):
            batch = seg_id_list[i : i + CHUNK]
            for sid, tte in session.exec(
                select(Segment.id, Segment.tte_ms).where(Segment.id.in_(batch))
            ).all():
                tte_map[sid] = tte or 0
        for b in buckets.values():
            b["tte_ms"] = sum(tte_map.get(s, 0) for s in b["_seg_ids"])

    meta["mixed_unit"] = any(len(b["_unit_kinds"]) > 1 for b in buckets.values())
    meta["mixed_metric"] = any(len(b["_metric_versions"]) > 1 for b in buckets.values())
    return {"buckets": buckets, "meta": meta}


# --- CSV ---------------------------------------------------------------------
@admitted_work(operation="report")
def to_csv(report: dict) -> str:
    """Serialize a run_report() result to CSV text (stdlib csv, RFC-4180 CRLF).

    Deterministic column order (report["columns"]); one row per bucket in the order
    run_report emitted them; a trailing TOTALS row summing every numeric column (the
    key column reads "TOTALS"). ASCII headers. Returns a str (the caller sets the
    HTTP Content-Type / writes the file).

    D4: any reconciliation caveats (report["caveats"]) are appended AFTER the TOTALS row,
    each on its own single-cell `NOTE,<text>` line, so a downloaded CSV carries the same
    provider-filter / workflow-axis notes the web page shows. A plain report has no
    caveats, so its CSV is byte-identical to before (no trailing rows)."""
    columns = report["columns"]
    rows = report["rows"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(columns)
    key_col = report["key_column"]
    # M3 (§4.5 rule 4): a NON-SUMMABLE (avg_*) column must not be summed OR zeroed into
    # TOTALS — the isinstance guard alone would leave an all-empty average totalling the
    # integer 0 it was seeded with. Such columns accumulate nothing and emit "" below.
    totals = {c: 0 for c in columns if c != key_col and c not in _NON_SUMMABLE_COLUMNS}
    for row in rows:
        w.writerow([_csv_cell(row.get(c, "")) for c in columns])
        for c in totals:
            v = row.get(c, 0)
            if isinstance(v, (int, float)):
                totals[c] += v
    total_row = []
    for c in columns:
        if c == key_col:
            total_row.append("TOTALS")
        elif c in _NON_SUMMABLE_COLUMNS:
            total_row.append("")
        else:
            total_row.append(_csv_cell(_round_total(totals[c])))
    w.writerow(total_row)
    for caveat in report.get("caveats") or []:
        w.writerow(["NOTE", caveat])
    return buf.getvalue()


def totals_row(report: dict) -> dict:
    """The same TOTALS the CSV footer carries, as a dict (for the HTML table footer
    and the parity tests). Sums every numeric metric column; the key column is
    'TOTALS'."""
    columns = report["columns"]
    key_col = report["key_column"]
    out = {key_col: "TOTALS"}
    for c in columns:
        if c == key_col:
            continue
        # M3: mirror to_csv exactly — a NON-SUMMABLE (avg_*) column is blank, never 0.
        if c in _NON_SUMMABLE_COLUMNS:
            out[c] = ""
            continue
        s = 0
        for row in report["rows"]:
            v = row.get(c, 0)
            if isinstance(v, (int, float)):
                s += v
        out[c] = _round_total(s)
    return out


def kinds_catalog() -> list:
    """[(kind, [rendered columns])] - for `report kinds` and the web form."""
    return [(k, list(_KIND_COLUMNS[k])) for k in KINDS]
