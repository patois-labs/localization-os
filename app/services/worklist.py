"""The cross-project reviewer worklist — "My work" (SPEC-wave3-collaboration §8, LC-5).

ONE service, THREE sections, ONE scoping ring (§8.1). Every surface (web /my-work,
GET /api/worklist, CLI `work list`, MCP `my_work`) is a thin caller of `my_work`.

The three sections, each defined on data that ACTUALLY EXISTS (§8.2):

  * review  — paused workflow steps whose STEP DEFINITION names my email. Assignment
              lives on `WorkflowStep.assignee_email` (the workflow definition, not the
              run), so "assigned to me" means every paused run whose current step's
              definition names me — NOT a per-run assignment (§8.3). Pinned as a test so
              nobody "fixes" it silently.
  * query   — query threads assigned to me (open/answered) plus my OWN answered ones.
              queries_enabled-gated: the section DISAPPEARS when the kill switch is off
              (§5.2), while review + quality survive. This service reaches the query
              TABLE through app.models (like content/retention/gdpr — §5.2's fourth
              seam), NOT the query_threads service, so no static check defends the
              kill-switch gate here; it is asserted directly (verify_worklist §33).
  * quality — documents in my visible scope with >=1 LIVE (non-withdrawn) major/critical
              LQAResult within a declared recency window (default 90 days, `?days=` /
              `--days`). NOT keyed on SegStatus.NEEDS_REVIEW: §2.8 shows that status
              FLOODS on a mature install (35 write sites, every soft QA gate, cleared
              only by a human edit), so it is DEMOTED to an off-by-default filter
              (`?quality=needs_review` / `--quality needs_review`) with its own caption
              (§8.2, HIGH-4). Shown only to can_review+ (a translator does not see it).

THE F1 MEMORY LAW (§8.2, MED-1). The quality + needs_review queries are PROJECTIONS,
never whole rows and never the blob: `LQAResult.note` is NEVER loaded. They are chunked
over the confined document/project set (CHUNK=150, the reports.py idiom). The
`visible_project_ids() is None` arm means UNRESTRICTED and SKIPS the `IN` filter entirely
— it must never pass `None` into `.in_()` (an always-false predicate that would show an
admin an empty worklist) and never enumerate every project id (the load F1 forbids).

TENANCY (§8.2, HIGH). Every section funnels through the SAME `access.visible_project_ids`
ring on `project_id`; a leak here is tenant isolation. A customer-bound user sees only
its customer's work; a non-member under `project_scoping` only member/created projects.
Scope is applied BEFORE the merge and the slice, and `total` counts only in-scope rows.

THE TRUSTED OPERATOR (§8.4). The in-process CLI runs as root with no session user; it
passes `trusted_operator=True` and supplies the acting identity through the `user`
object's `.email` (a real User when one resolves, else a minimal email carrier). Under
`trusted_operator` the scope ring is unrestricted and can_review is implied — the same
posture every CLI verb takes. `/api/worklist` and `work list` REFUSE without a resolvable
identity rather than return an empty list that would read as "you have no work" (§8.4);
that refusal lives on the surfaces (they own the 400 / usage message), not here.

Documented in: docs/architecture/collaboration-and-review.md
"""

from __future__ import annotations

import logging
from collections import namedtuple
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import (
    Document,
    LQAResult,
    Project,
    QueryStatus,
    QueryThread,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    WorkflowRun,
    WorkflowStep,
)
from app.services import access

log = logging.getLogger("loc.worklist")

# --- constants (§8.2) -------------------------------------------------------- #
SECTIONS = ("review", "query", "quality")
QUALITY_WINDOW_DAYS = 90
# Canonical severities (lqa.canon_severity persists these): a "live" quality item is a
# non-withdrawn finding at one of these two.
QUALITY_SEVERITIES = ("critical", "major")
# The reports.py:1752-1757 idiom: chunk the confined id set so a big install never
# builds an unbounded `IN (...)` list (the SQLite variable ceiling + the F1 load law).
CHUNK = 150
LIST_PAGE_MAX = 50

# The quality section's PROJECTION (§8.2.1, the F1 law) — the five columns a worklist row
# needs and NOTHING else. `LQAResult.note` is deliberately absent; a query-shape test
# (verify_worklist 30b) asserts it, exactly as reports.py pins its own tuples.
_QUALITY_COLS = (
    LQAResult.document_id,
    LQAResult.target_locale,
    LQAResult.severity,
    LQAResult.created_at,
    LQAResult.withdrawn_at,
)

# One merged, section-tagged row (§8.1). `updated_at` is the merge sort key (newest
# first); `reason` is the honest one-line "why this is here"; `url` is where a person
# acts. `ref_id` is the section's actionable id (a run id / a thread id / a document id).
WorkItem = namedtuple(
    "WorkItem",
    "section ref_id title project_id project_name document_id target_locale "
    "status reason due url updated_at",
)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _chunks(seq, n=CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _resolve_allowed(session: Session, user, *, trusted: bool, org_id):
    """The ONE scoping decision (§8.1/§8.2). Returns the allowed project-id set or None
    meaning UNRESTRICTED (the F1 arm). `trusted` (the console) is unrestricted; an
    `org_id` (multi-org confinement / --org) intersects the ring to that org and always
    yields a concrete set (never None), so a scoped caller can never widen to
    unrestricted."""
    allowed = None if trusted else access.visible_project_ids(session, user)
    if org_id is not None:
        org_pids = set(session.exec(select(Project.id).where(Project.org_id == org_id)).all())
        allowed = org_pids if allowed is None else (set(allowed) & org_pids)
    if allowed is None:
        return None
    return list(allowed)


def _project_scope_clause(model_col, allowed):
    """A generator of (needs_filter, clause) — the F1-safe `IN` handling shared by every
    section. `allowed is None` => UNRESTRICTED: yields ONE pass with no clause (the arm
    r1 dropped). An empty list => nothing visible: yields nothing (the caller returns
    []). Otherwise yields one chunked `.in_()` clause per CHUNK batch."""
    if allowed is None:
        yield None
        return
    for batch in _chunks(allowed):
        yield model_col.in_(batch)


# --- section 1: paused review steps that name me (§8.2) ---------------------- #
def _review_items(session: Session, me: str, allowed) -> list[dict]:
    """Paused workflow steps whose DEFINITION names my email (§8.2/§8.3). Joins
    StepRun(PAUSED) -> WorkflowStep(assignee==me) -> WorkflowRun -> Document(project in
    scope). Assignment is on the STEP DEFINITION, so two paused runs of the same workflow
    both appear (§8.3) — pinned as behaviour, not a bug."""
    if not me:
        return []
    if allowed is not None and not allowed:
        return []
    cols = (
        WorkflowRun.id,
        WorkflowRun.target_locale,
        WorkflowRun.started_at,
        Document.id,
        Document.filename,
        Document.project_id,
        WorkflowStep.name,
        StepRun.started_at,
    )
    out: list[dict] = []
    seen: set = set()
    for clause in _project_scope_clause(Document.project_id, allowed):
        stmt = (
            select(*cols)
            .join(StepRun, StepRun.workflow_run_id == WorkflowRun.id)
            .join(WorkflowStep, WorkflowStep.id == StepRun.step_id)
            .join(Document, Document.id == WorkflowRun.document_id)
            .where(
                StepRun.status == RunStatus.PAUSED,
                WorkflowRun.status == RunStatus.PAUSED,
                func.lower(WorkflowStep.assignee_email) == me,
            )
        )
        if clause is not None:
            stmt = stmt.where(clause)
        for (
            run_id,
            run_loc,
            run_started,
            doc_id,
            filename,
            pid,
            step_name,
            step_started,
        ) in session.exec(stmt).all():
            if run_id in seen:
                continue
            seen.add(run_id)
            out.append(
                {
                    "section": "review",
                    "ref_id": run_id,
                    "title": filename or (f"run #{run_id}"),
                    "project_id": pid,
                    "document_id": doc_id,
                    "target_locale": run_loc or "",
                    "status": "paused",
                    "reason": (step_name or "Human review"),
                    "due": None,
                    "url": f"/runs/{run_id}",
                    "updated_at": step_started or run_started,
                }
            )
    return out


# --- section 2: queries waiting on me / my answered ones (§8.2) -------------- #
def _query_items(session: Session, me: str, allowed) -> list[dict]:
    """Query threads assigned to me (open/answered) OR my OWN answered ones (§8.2).

    KILL SWITCH (§5.2): the WHOLE section disappears when queries_enabled is off, while
    review + quality survive. This reaches the query TABLE through app.models — the
    §5.2 fourth-seam pattern — so nothing static defends the gate; verify_worklist §33
    asserts it directly. Reads the thread rows with the SAME `allowed` ring the other
    sections use (§8.1's one ring), never a second ring."""
    from app import features

    if not features.is_enabled("queries"):
        return []
    if not me:
        return []
    if allowed is not None and not allowed:
        return []
    cols = (
        QueryThread.id,
        QueryThread.subject,
        QueryThread.project_id,
        QueryThread.document_id,
        QueryThread.target_locale,
        QueryThread.status,
        QueryThread.updated_at,
        QueryThread.assignee_email,
        QueryThread.author,
    )
    out: list[dict] = []
    seen: set = set()
    for clause in _project_scope_clause(QueryThread.project_id, allowed):
        stmt = select(*cols).where(
            QueryThread.status.in_((QueryStatus.OPEN, QueryStatus.ANSWERED)),
            (
                (func.lower(QueryThread.assignee_email) == me)
                | (
                    (func.lower(QueryThread.author) == me)
                    & (QueryThread.status == QueryStatus.ANSWERED)
                )
            ),
        )
        if clause is not None:
            stmt = stmt.where(clause)
        for tid, subject, pid, doc_id, loc, status, updated, assignee, author in session.exec(  # noqa: B007
            stmt
        ).all():
            if tid in seen:
                continue
            seen.add(tid)
            # Assignment takes precedence over authorship for the reason label (§8.2:
            # assignee==me -> assigned, else author==me & answered -> answered).
            reason = "assigned" if (assignee or "").lower() == me else "answered"
            out.append(
                {
                    "section": "query",
                    "ref_id": tid,
                    "title": subject or (f"query #{tid}"),
                    "project_id": pid,
                    "document_id": doc_id,
                    "target_locale": loc or "",
                    "status": status,
                    "reason": reason,
                    "due": None,
                    "url": f"/queries/{tid}",
                    "updated_at": updated,
                }
            )
    return out


# --- section 3: documents needing quality attention (§8.2, HIGH-4) ---------- #
def _quality_items(session: Session, allowed, *, days: int) -> list[dict]:
    """Documents (grouped by document + target_locale) with >=1 LIVE major/critical
    LQAResult within the last `days` (§8.2). F1 law: PROJECTION only (LQAResult.note is
    NEVER loaded), chunked over the confined set, `None` => unrestricted (no IN filter).
    Grain (document, target_locale), so one noisy document is one row."""
    if allowed is not None and not allowed:
        return []
    cutoff = _utcnow() - timedelta(days=max(1, int(days)))
    groups: dict = {}  # (doc_id, locale) -> {"counts": {sev: n}, "updated": dt}
    for clause in _project_scope_clause(Document.project_id, allowed):
        stmt = select(*_QUALITY_COLS).where(
            func.lower(LQAResult.severity).in_(QUALITY_SEVERITIES),
            LQAResult.withdrawn_at.is_(None),
            LQAResult.created_at >= cutoff,
        )
        if clause is not None:
            # Join Document ONLY to filter on its project (the projection stays LQAResult
            # columns — no note, no Document blob).
            stmt = stmt.join(Document, Document.id == LQAResult.document_id).where(clause)
        for doc_id, loc, sev, created, _withdrawn in session.exec(stmt).all():
            if doc_id is None:
                continue
            key = (doc_id, loc or "")
            g = groups.setdefault(key, {"counts": {}, "updated": created})
            s = (sev or "").lower()
            g["counts"][s] = g["counts"].get(s, 0) + 1
            if created is not None and (g["updated"] is None or created > g["updated"]):
                g["updated"] = created
    return _group_docs(
        session,
        groups,
        section="quality",
        status="quality",
        reason_fn=_quality_reason,
        url_fn=lambda doc_id, loc: _lqa_url(doc_id, loc),
    )


def _needs_review_items(session: Session, allowed) -> list[dict]:
    """The OFF-BY-DEFAULT flood filter (§8.2, HIGH-4): documents with SegStatus.
    NEEDS_REVIEW segments, grouped (document, target_locale). Its own labelled list with
    its own total and honest caption (a machine flag, not an assignment; it persists
    until a human edits the segment). Same F1 treatment as the quality query."""
    if allowed is not None and not allowed:
        return []
    cols = (Segment.document_id, Segment.target_locale)
    groups: dict = {}
    for clause in _project_scope_clause(Document.project_id, allowed):
        stmt = select(*cols).where(Segment.status == SegStatus.NEEDS_REVIEW)
        if clause is not None:
            stmt = stmt.join(Document, Document.id == Segment.document_id).where(clause)
        for doc_id, loc in session.exec(stmt).all():
            if doc_id is None:
                continue
            key = (doc_id, loc or "")
            g = groups.setdefault(key, {"counts": {"needs_review": 0}, "updated": None})
            g["counts"]["needs_review"] += 1
    return _group_docs(
        session,
        groups,
        section="quality",
        status="needs_review",
        reason_fn=_needs_review_reason,
        url_fn=lambda doc_id, loc: f"/documents/{doc_id}",
    )


def _quality_reason(counts: dict) -> str:
    parts = []
    for sev in ("critical", "major"):
        n = counts.get(sev, 0)
        if n:
            parts.append("%d %s" % (n, sev))  # noqa: UP031
    return ", ".join(parts) or "quality finding"


def _needs_review_reason(counts: dict) -> str:
    n = counts.get("needs_review", 0)
    return "%d segment%s need review" % (n, "" if n == 1 else "s")  # noqa: UP031


def _lqa_url(doc_id, loc: str) -> str:
    base = f"/documents/{doc_id}/lqa"
    return f"{base}?locale={loc}" if loc else base


def _group_docs(
    session: Session, groups: dict, *, section: str, status: str, reason_fn, url_fn
) -> list[dict]:
    """Turn the (doc_id, locale) -> {counts, updated} map into WorkItem dicts, resolving
    the document filename + project_id in ONE bounded lookup (only the docs that HAVE
    findings — never every visible document)."""
    if not groups:
        return []
    doc_ids = sorted({doc_id for (doc_id, _loc) in groups})
    docmeta: dict = {}
    for batch in _chunks(doc_ids):
        for d_id, filename, pid in session.exec(
            select(Document.id, Document.filename, Document.project_id).where(
                Document.id.in_(batch)
            )
        ).all():
            docmeta[d_id] = (filename, pid)
    out: list[dict] = []
    for (doc_id, loc), g in groups.items():
        filename, pid = docmeta.get(doc_id, (None, None))
        if pid is None:
            # The finding's document vanished under us (a concurrent delete); skip it
            # rather than emit a row with no project (which would also dodge scoping).
            continue
        out.append(
            {
                "section": section,
                "ref_id": doc_id,
                "title": filename or (f"document #{doc_id}"),
                "project_id": pid,
                "document_id": doc_id,
                "target_locale": loc,
                "status": status,
                "reason": reason_fn(g["counts"]),
                "due": None,
                "url": url_fn(doc_id, loc),
                "updated_at": g["updated"],
            }
        )
    return out


# --- the public entry point -------------------------------------------------- #
def my_work(
    session: Session,
    user,
    *,
    sections=SECTIONS,
    days=QUALITY_WINDOW_DAYS,
    quality="",
    limit=LIST_PAGE_MAX,
    offset=0,
    trusted_operator=False,
    org_id=None,
) -> tuple[list[WorkItem], int]:
    """The cross-project "my work" view for ONE identity (§8.1).

    `sections` selects which of review/query/quality to include. `days` bounds the
    quality window (§8.2, declared not silent). `quality="needs_review"` swaps the
    quality section for the off-by-default NEEDS_REVIEW flood filter (its own labelled
    list). `days` + `quality` are additions to §8.1's illustrative signature that §8.2
    mandates as the `?days=` / `?quality=` overrides.

    Identity is read off `user.email`; scope is `access.visible_project_ids(user)` unless
    `trusted_operator` (root, unrestricted). Rows are scoped BEFORE the merge + slice and
    `total` counts only in-scope rows (§8.1). Returns (page, total)."""
    me = (getattr(user, "email", "") or "").strip().lower()
    allowed = _resolve_allowed(session, user, trusted=trusted_operator, org_id=org_id)

    items: list[dict] = []
    if "review" in sections:
        items += _review_items(session, me, allowed)
    if "query" in sections:
        items += _query_items(session, me, allowed)
    if "quality" in sections:  # noqa: SIM102
        # Quality is reviewer+ only (§8.2). The trusted console is root => shown; a
        # translator does not see the section at all (test 30).
        if trusted_operator or _can_review(user):
            if (quality or "").strip().lower() == "needs_review":
                items += _needs_review_items(session, allowed)
            else:
                items += _quality_items(session, allowed, days=days)

    # One bounded project-name lookup for every item (never N+1).
    names: dict = {}
    pids = sorted({it["project_id"] for it in items if it.get("project_id")})
    for batch in _chunks(pids):
        for pid, name in session.exec(
            select(Project.id, Project.name).where(Project.id.in_(batch))
        ).all():
            names[pid] = name

    rows = [
        WorkItem(
            section=it["section"],
            ref_id=it["ref_id"],
            title=it["title"],
            project_id=it["project_id"],
            project_name=names.get(it["project_id"], ""),
            document_id=it.get("document_id"),
            target_locale=it.get("target_locale", ""),
            status=it["status"],
            reason=it["reason"],
            due=it.get("due"),
            url=it["url"],
            updated_at=it["updated_at"],
        )
        for it in items
    ]

    # Merge newest-first on updated_at; a NULL updated_at sorts last (never crashes).
    rows.sort(key=lambda r: (r.updated_at is not None, r.updated_at), reverse=True)
    total = len(rows)
    lo = max(0, offset)
    hi = lo + max(1, min(int(limit), LIST_PAGE_MAX))
    return rows[lo:hi], total


def _can_review(user) -> bool:
    from app.services import security

    return security.can_review(user)


# --- the dashboard's open-query count (§8.4, the RV-1 line) ------------------ #
def my_open_query_count(session: Session, user) -> int:
    """The count the dashboard shows as "N queries assigned to you" (§4.2/§8.4), linking
    to /my-work. Gated on queries_enabled (returns 0 when off, so the line vanishes with
    the feature). Assigned + open/answered, scoped to the caller's visible projects — the
    exact query-section population, counted."""
    from app import features

    if not features.is_enabled("queries"):
        return 0
    me = (getattr(user, "email", "") or "").strip().lower()
    if not me:
        return 0
    allowed = access.visible_project_ids(session, user)
    if allowed is not None and not allowed:
        return 0
    total = 0
    for clause in _project_scope_clause(QueryThread.project_id, allowed):
        stmt = (
            select(func.count())
            .select_from(QueryThread)
            .where(
                func.lower(QueryThread.assignee_email) == me,
                QueryThread.status.in_((QueryStatus.OPEN, QueryStatus.ANSWERED)),
            )
        )
        if clause is not None:
            stmt = stmt.where(clause)
        total += int(session.exec(stmt).one())
    return total
