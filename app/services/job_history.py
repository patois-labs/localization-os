"""Unified execution-history log for /jobs — background Jobs + workflow runs.

The /jobs page was a dead-looking `recent(50)` over the Job table ALONE. But the
Job table is written only by *background* work (connector_translate, API/queued
translate_document, backfill_embeddings) — every workflow a user launches from the
web UI goes through the WorkflowRun/StepRun path and NEVER creates a Job row, so
the page showed a stale handful and looked broken.

This module is the ONE seam the web route, the CLI (`job list --include-runs`) and
/api all call to get a chronological, newest-first, SCOPED, filtered, paginated
history that interleaves BOTH row types. Keeping the scope logic here (not in a
route) is what keeps the three surfaces from drifting on isolation.

SCOPING (the load-bearing part — scope FIRST, then paginate, so a page/count can
never leak across scopes):
  * Job rows reuse the EXACT jobs_ui semantics: the owner filter (customer-bound /
    non-see-all users see only jobs they created — leak vectors + the R2-3 under-
    show rule) AND the multi-org gate (jobs.job_org_id, a strict no-op while the
    sacred gate is OFF).
  * Run rows are scoped by PROJECT VISIBILITY via access.visible_project_ids — the
    same canonical list-scoping helper the project list uses. That folds org +
    customer + membership into one confined id set (None = unrestricted single-
    tenant; [] = the user sees no projects -> no runs). When in doubt it under-shows.

A hard scan cap bounds the in-memory merge (this is a single-process SQLite app;
the tables are bounded by real usage). The cap is applied AFTER scope filtering, so
it can only ever drop the OLDEST in-scope rows, never leak an out-of-scope one.

Documented in: docs/architecture/jobs-and-scheduler.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, select

from app.models import Connection, Document, Job, Project, Workflow, WorkflowBatch, WorkflowRun
from app.services import access
from app.services import jobs as jobs_svc

# Safety ceiling on the per-type candidate scan (post-scope). Sized well above any
# realistic single-tenant history; a caller wanting older rows pages via offset.
_SCAN_CAP = 2000

# Job kinds (for the CLI/UI filter help + validation).
JOB_KINDS = tuple(sorted(jobs_svc.HANDLERS))

_MIN_TS = datetime.min


@dataclass
class HistoryRow:
    """One unified history entry. `type` distinguishes a background Job ('job') from a
    workflow run ('run') from a fan-out batch ('batch', Phase 5a); the rest is a superset
    all fill where meaningful."""

    type: str  # "job" | "run" | "batch"
    id: int
    kind: str  # job kind, or the workflow name for a run
    status: str
    actor: str  # created_by / launcher ("" -> UI shows "-")
    subject: str  # human-readable subject (filename / connection name)
    created_at: datetime | None
    finished_at: datetime | None
    duration_s: float | None
    progress_done: int
    progress_total: int
    link: str | None  # run view link for runs; None for jobs
    project: str  # run's project name (jobs: "" unless cheap)
    target_locale: str  # run's launch locale (jobs: "")
    error: str  # job error summary (runs: "")
    result: dict  # job structured result (runs: {})
    _sort_ts: datetime  # internal sort key (never serialized raw)

    def as_api_dict(self) -> dict:
        """The ONE stable JSON row shape emitted by BOTH the /api route and the CLI
        (`job list --json`, in-process AND --remote). Keeping a single serializer is
        what stops the surfaces from drifting on field NAMES (an earlier version had
        the in-process CLI emit `as_dict()` — actor/progress_done/started_at — while
        /api emitted created_by/done/started_at, so `job list --json` returned
        different keys with vs without --remote, and silently dropped the pre-history
        created_by/done/total keys existing scripts parse). Backward-compatible: the
        legacy keys (id/kind/status/done/total/created_by) survive unchanged; every
        new field (type/subject/link/duration_s/project/target_locale/timestamps) is
        additive. `total` here is the row's progress total (not the top-level count)."""
        return {
            "type": self.type,
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "done": self.progress_done,
            "total": self.progress_total,
            "created_by": self.actor,
            "subject": self.subject,
            "link": self.link,
            "duration_s": self.duration_s,
            "project": self.project,
            "target_locale": self.target_locale,
            "started_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# --- scope helpers (mirrors app/web/jobs_ui.py, but user-driven not request-driven)
def job_owner_filter(user) -> str | None:
    """None = this user may see every job; else the created_by email a job must
    carry to be visible. Byte-identical to jobs_ui._owner_filter (customer-bound ->
    own jobs only; see-all -> all; otherwise own jobs only)."""
    if access.is_customer_bound(user):
        return getattr(user, "email", "") or ""
    if access.can_see_all(user):
        return None
    return getattr(user, "email", "") or ""


def job_visible_in_org(user, job) -> bool:
    """Multi-org isolation for a single Job (jobs_ui._job_in_org). No-op True while
    the sacred gate is OFF."""
    if not access.multi_org_active():
        return True
    return access.same_org(user, jobs_svc.job_org_id(job))


# --- subject resolution (batched, no N+1) ------------------------------------
def _job_subject(job: Job, docs: dict, conns: dict) -> str:
    """A cheap human-readable subject for a Job. translate_document -> the document
    filename; connector_translate -> the connection name; else "" (the kind alone is
    the label). Uses pre-fetched maps so this is O(1) per row."""
    payload = job.payload or {}
    if job.kind == "translate_document":
        doc = docs.get(payload.get("document_id"))
        loc = payload.get("locale")
        if doc is not None:
            return f"{doc.filename} -> {loc}" if loc else doc.filename
    elif job.kind == "connector_translate":
        conn = conns.get(payload.get("connection_id"))
        if conn is not None:
            return conn.name
    elif job.kind == "backfill_embeddings":
        pid = payload.get("project_id")
        return f"project #{pid}" if pid else "all projects"
    elif job.kind == "api_task":
        # A background or idempotent /api call (app/web/background_calls.py): its route.
        return str(payload.get("route") or "")
    return ""


def _duration(started: datetime | None, finished: datetime | None) -> float | None:
    if started is None or finished is None:
        return None
    return max(0.0, (finished - started).total_seconds())


# --- the one query the three surfaces call -----------------------------------
def unified_history(
    session: Session,
    user,
    *,
    type: str = "all",
    status: str = "",
    kind: str = "",
    actor: str = "",
    limit: int = 50,
    offset: int = 0,
    trusted_operator: bool = False,
    org_id: int | None = None,
    document_id: int | None = None,
    workflow_id: int | None = None,
) -> tuple[list[HistoryRow], int]:
    """Return (page_rows, total_in_scope) — a scoped, filtered, chronologically
    merged (newest first) history of Jobs + workflow runs.

    Filters (all optional, server-side, no-JS-safe):
      type        : "all" | "job" | "run".
      status      : matches a Job's or a run's status string (both share queued/
                    running/completed/failed; runs also pending/paused/skipped).
      kind        : a JOB kind. Setting it EXCLUDES runs (a run has no job kind).
      actor       : case-insensitive substring of created_by / launcher.
      document_id : the document a job/run/batch targeted. Jobs match on
                    payload.document_id (translate_document); runs/batches on their
                    document_id column.
      workflow_id : the workflow a run/batch executed. A job has NO workflow, so a
                    workflow_id filter EXCLUDES jobs (like `kind` excludes runs).

    SCOPED-EXISTENCE (no cross-org oracle): both id filters are applied as ADDITIONAL
    predicates on the ALREADY-scope-confined queries — never as a pre-existence check.
    A document/workflow in another org (or a nonexistent id) is confined out first, so
    it matches nothing and the result is an empty list, indistinguishable from a truly
    unknown id. This preserves the issue #16 trusted_operator seam (the in-process CLI
    still sees every in-scope row; op_org_id still hard-confines it).

    Scope is applied BEFORE the merge+slice (see module docstring), so `total` and
    every page are already confined to what this user may see.

    TRUSTED OPERATOR (issue #16a): the in-process CLI is the documented local-root
    operator (docs/CLI.md: "no login and no RBAC") — but once `init` turns the login
    wall ON, a plain user=None is treated by the access helpers as the WEB "no user ->
    sees nothing" semantic (job_owner_filter(None) -> "" matches no job; and
    visible_project_ids(None) -> [] hides every run), so the operator's own `job list`
    went BLIND. `trusted_operator=True` restores the trusted-read semantic for that ONE
    caller: no per-user owner filter and no project-visibility confinement. It is a
    KEYWORD-ONLY parameter that ONLY the in-process CLI (`cmd_job._job_list`) sets;
    every request path (web `jobs_index`, /api `api_jobs_list`) calls this with the
    resolved User and NEVER passes it, so a WEB user=None stays sees-nothing and the
    operator seam is structurally unreachable from a request. `org_id` (the CLI's
    resolved --org under the multi-org gate) still hard-confines the operator view to
    one org so a shared-host operator can't sweep every org unscoped; org_id=None means
    unscoped (single-tenant, or --operator-authorized cross-org), mirroring
    reports.run_report's trusted-operator scoping exactly.
    """
    type = (type or "all").lower()
    status = (status or "").strip()
    kind = (kind or "").strip()
    actor_q = (actor or "").strip().lower()

    rows: list[HistoryRow] = []

    # --- Jobs -----------------------------------------------------------------
    # A `kind` filter is job-only; with it set there is nothing on the run side to
    # show, so a "kind" query naturally narrows to jobs. A `workflow_id` filter is the
    # mirror image: a Job has no workflow, so it EXCLUDES jobs entirely.
    want_jobs = type in ("all", "job") and workflow_id is None
    if want_jobs:
        # Trusted in-process operator sees every job (no per-user owner filter);
        # a request-path caller is confined by job_owner_filter(user) as before.
        owner = None if trusted_operator else job_owner_filter(user)
        stmt = select(Job)
        if owner is not None:
            stmt = stmt.where(Job.created_by == owner)
        if kind:
            stmt = stmt.where(Job.kind == kind)
        if status:
            stmt = stmt.where(Job.status == status)
        stmt = stmt.order_by(Job.created_at.desc(), Job.id.desc()).limit(_SCAN_CAP)
        job_rows = list(session.exec(stmt).all())
        # Org gate. For the trusted operator, an explicit --org (org_id) hard-confines
        # to that org's jobs (job_org_id resolves a Job's org from its creator/content);
        # org_id=None = unscoped cross-org (single-tenant no-op). For a request caller,
        # the sacred multi-org gate applies verbatim (strict no-op while it is OFF).
        if trusted_operator:
            if org_id is not None:
                job_rows = [j for j in job_rows if jobs_svc.job_org_id(j) == org_id]
        elif access.multi_org_active():
            job_rows = [j for j in job_rows if job_visible_in_org(user, j)]
        # Free-actor substring filter.
        if actor_q:
            job_rows = [j for j in job_rows if actor_q in (j.created_by or "").lower()]
        # Document filter (payload.document_id lives in JSON — filtered in Python, like
        # the actor filter, over the already-scoped+capped candidate set). Only a
        # translate_document job carries a document_id; any other kind never matches.
        if document_id is not None:
            job_rows = [j for j in job_rows if _p(j).get("document_id") == document_id]
        # Batch-resolve subjects (one query per referenced table, not per row).
        doc_ids = {(_p(j).get("document_id")) for j in job_rows if j.kind == "translate_document"}
        conn_ids = {
            (_p(j).get("connection_id")) for j in job_rows if j.kind == "connector_translate"
        }
        docs = _fetch_map(session, Document, doc_ids)
        conns = _fetch_map(session, Connection, conn_ids)
        for j in job_rows:
            if not trusted_operator and not access.can_view_job_projects(session, user, j):
                continue
            rows.append(
                HistoryRow(
                    type="job",
                    id=j.id,
                    kind=j.kind,
                    status=j.status,
                    actor=j.created_by or "",
                    subject=_job_subject(j, docs, conns),
                    created_at=j.created_at,
                    finished_at=j.finished_at,
                    duration_s=_duration(j.started_at, j.finished_at),
                    progress_done=j.progress_done,
                    progress_total=j.progress_total,
                    link=None,
                    project="",
                    target_locale="",
                    error=j.error or "",
                    result=j.result or {},
                    _sort_ts=j.created_at or _MIN_TS,
                )
            )

    # --- Workflow runs --------------------------------------------------------
    want_runs = type in ("all", "run") and not kind
    if want_runs:
        if trusted_operator:
            # Trusted operator: no project-visibility confinement. An explicit --org
            # scopes runs to that org's projects; org_id=None = unrestricted (the
            # None sentinel below), mirroring reports.allowed_project_ids.
            if org_id is None:
                visible = None
            else:
                visible = sorted(
                    session.exec(select(Project.id).where(Project.org_id == org_id)).all()
                )
        else:
            visible = access.visible_project_ids(session, user)  # None = unrestricted
        # A user who can see NO projects sees NO runs (empty confinement set).
        if visible is None or visible:
            stmt = select(WorkflowRun)
            if visible is not None:
                stmt = stmt.join(Document, Document.id == WorkflowRun.document_id)
                stmt = stmt.where(Document.project_id.in_(visible))
            # CALM history (Phase 5a): a run that carries a batch_id is a batch MEMBER —
            # it is folded into its batch's single row (below), NOT shown as a separate
            # entry. A NULL batch_id is a plain single run (every pre-5a row), unaffected.
            stmt = stmt.where(WorkflowRun.batch_id.is_(None))
            if status:
                stmt = stmt.where(WorkflowRun.status == status)
            if document_id is not None:
                stmt = stmt.where(WorkflowRun.document_id == document_id)
            if workflow_id is not None:
                stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
            stmt = stmt.order_by(WorkflowRun.started_at.desc(), WorkflowRun.id.desc()).limit(
                _SCAN_CAP
            )
            run_rows = list(session.exec(stmt).all())
            if actor_q:
                run_rows = [
                    r for r in run_rows if actor_q in (getattr(r, "created_by", "") or "").lower()
                ]
            wf_ids = {r.workflow_id for r in run_rows}
            rdoc_ids = {r.document_id for r in run_rows}
            wfs = _fetch_map(session, Workflow, wf_ids)
            rdocs = _fetch_map(session, Document, rdoc_ids)
            proj_ids = {d.project_id for d in rdocs.values()}
            projs = _fetch_map(session, Project, proj_ids)
            for r in run_rows:
                wf = wfs.get(r.workflow_id)
                doc = rdocs.get(r.document_id)
                proj = projs.get(doc.project_id) if doc is not None else None
                subject = doc.filename if doc is not None else f"document #{r.document_id}"
                if r.target_locale:
                    subject = f"{subject} -> {r.target_locale}"
                rows.append(
                    HistoryRow(
                        type="run",
                        id=r.id,
                        kind=(wf.name if wf is not None else f"workflow #{r.workflow_id}"),
                        status=r.status,
                        actor=getattr(r, "created_by", "") or "",
                        subject=subject,
                        created_at=r.started_at,
                        finished_at=r.finished_at,
                        duration_s=_duration(r.started_at, r.finished_at),
                        progress_done=0,
                        progress_total=0,
                        link=f"/runs/{r.id}",
                        project=(proj.name if proj is not None else ""),
                        target_locale=r.target_locale or "",
                        error="",
                        result={},
                        _sort_ts=r.started_at or _MIN_TS,
                    )
                )

    # --- Fan-out batches (Phase 5a) ------------------------------------------
    # ONE row per batch (kind=workflow name, link -> /batches/{id}); the member runs are
    # excluded above, so a batch reads as a single calm entry. Scoped by the SAME project
    # visibility as runs (a batch's project = its document's project). A `kind` filter is
    # job-only, so it excludes batches just as it excludes runs.
    want_batches = type in ("all", "batch") and not kind
    if want_batches:
        if trusted_operator:
            if org_id is None:
                bvisible = None
            else:
                bvisible = sorted(
                    session.exec(select(Project.id).where(Project.org_id == org_id)).all()
                )
        else:
            bvisible = access.visible_project_ids(session, user)  # None = unrestricted
        if bvisible is None or bvisible:
            bstmt = select(WorkflowBatch)
            if bvisible is not None:
                bstmt = bstmt.join(Document, Document.id == WorkflowBatch.document_id)
                bstmt = bstmt.where(Document.project_id.in_(bvisible))
            if status:
                bstmt = bstmt.where(WorkflowBatch.status == status)
            if document_id is not None:
                bstmt = bstmt.where(WorkflowBatch.document_id == document_id)
            if workflow_id is not None:
                bstmt = bstmt.where(WorkflowBatch.workflow_id == workflow_id)
            bstmt = bstmt.order_by(WorkflowBatch.created_at.desc(), WorkflowBatch.id.desc()).limit(
                _SCAN_CAP
            )
            batch_rows = list(session.exec(bstmt).all())
            if actor_q:
                batch_rows = [
                    b for b in batch_rows if actor_q in (getattr(b, "created_by", "") or "").lower()
                ]
            bwf_ids = {b.workflow_id for b in batch_rows}
            bdoc_ids = {b.document_id for b in batch_rows}
            bwfs = _fetch_map(session, Workflow, bwf_ids)
            bdocs = _fetch_map(session, Document, bdoc_ids)
            bproj_ids = {d.project_id for d in bdocs.values()}
            bprojs = _fetch_map(session, Project, bproj_ids)
            # Live member counts for the progress column (done/total), in one query.
            member_counts = _batch_member_counts(session, {b.id for b in batch_rows})
            for b in batch_rows:
                wf = bwfs.get(b.workflow_id)
                doc = bdocs.get(b.document_id)
                proj = bprojs.get(doc.project_id) if doc is not None else None
                requested = list(b.requested_locales or [])
                fname = doc.filename if doc is not None else f"document #{b.document_id}"
                subject = f"{fname} -> {len(requested)} locales"
                # finished_at is the terminal authority (mirrors batch_runs.batch_status):
                # a batch reads RUNNING until it is stamped, so history never shows a
                # stale terminal or a premature 'running'.
                bstatus = b.status if b.finished_at is not None else "running"
                done, total = member_counts.get(b.id, (0, 0))
                rows.append(
                    HistoryRow(
                        type="batch",
                        id=b.id,
                        kind=(wf.name if wf is not None else f"workflow #{b.workflow_id}"),
                        status=bstatus,
                        actor=getattr(b, "created_by", "") or "",
                        subject=subject,
                        created_at=b.created_at,
                        finished_at=b.finished_at,
                        duration_s=_duration(b.created_at, b.finished_at),
                        progress_done=done,
                        progress_total=total or len(requested),
                        link=f"/batches/{b.id}",
                        project=(proj.name if proj is not None else ""),
                        target_locale="",
                        error="",
                        result={},
                        _sort_ts=b.created_at or _MIN_TS,
                    )
                )

    # --- merge, count, paginate ----------------------------------------------
    rows.sort(key=lambda x: (x._sort_ts, x.type, x.id), reverse=True)
    total = len(rows)
    offset = max(0, offset)
    limit = max(1, limit)
    page = rows[offset : offset + limit]
    return page, total


def _p(job: Job) -> dict:
    return job.payload or {}


def _fetch_map(session: Session, model, ids) -> dict:
    """{"id": row} for the given ids in ONE query (drops None ids). Avoids the
    per-row session.get N+1 during subject resolution."""
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    rows = session.exec(select(model).where(model.id.in_(ids))).all()
    return {r.id: r for r in rows}


def _batch_member_counts(session: Session, batch_ids) -> dict:
    """{batch_id: (completed_members, total_members)} for the given batches in ONE query,
    for the history progress column. Avoids an N+1 over member runs."""
    from app.models import RunStatus

    batch_ids = {i for i in batch_ids if i is not None}
    if not batch_ids:
        return {}
    out: dict = {}
    for r in session.exec(
        select(WorkflowRun.batch_id, WorkflowRun.status).where(WorkflowRun.batch_id.in_(batch_ids))
    ).all():
        bid, st = r
        done, total = out.get(bid, (0, 0))
        out[bid] = (done + (1 if st == RunStatus.COMPLETED else 0), total + 1)
    return out
