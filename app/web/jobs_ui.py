"""Background jobs: history page + status polling endpoint.

Jobs are scoped like projects: privileged roles (admin/manager/linguist — or
auth/scoping off) see every job; everyone else sees only jobs they created.
status_dict includes the result payload, so leaking it cross-user would leak
other projects' filenames and translation stats.

MULTI-ORG (the sacred gate): a Job carries no org_id and no project link — its
org is inferred from its `created_by` user's org (jobs.job_org_id). With the gate
ON that inference org-scopes BOTH the list (leak vector 4) and the status endpoint
(leak vector 5) so a see-all admin never counts / reads another org's jobs; a
foreign job looks identical to a missing one (404). A strict no-op while the gate
is OFF, so single-tenant behavior is byte-identical.

Documented in: docs/architecture/jobs-and-scheduler.md
"""

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import engine
from app.kernel import access, job_history, jobs, security
from app.kernel import spend as _spend
from app.web.deps import optional_id_param, templates

router = APIRouter()

_PAGE_SIZE = 50


def _owner_filter(request: Request) -> str | None:
    """None = the caller may see every job; else the created_by email a job
    must carry to be visible to this caller.

    R2-3: a CUSTOMER-BOUND user is see-all only WITHIN its customer's projects, but
    a Job carries no project/customer link (only created_by), so we cannot cheaply
    confine the list to the customer's jobs here. Fall back to OWNER filtering for a
    bound user (they see only jobs THEY created) — the SAFE direction (never a
    cross-customer job), matching the org-scoping precedent that under-shows rather
    than leaks. A bound user is thus never treated as see-all for the jobs list."""
    me = security.session_user(request)
    if access.is_customer_bound(me):
        return getattr(me, "email", "") or ""
    if access.can_see_all(me):
        return None
    return getattr(me, "email", "") or ""


def _job_in_org(request: Request, job) -> bool:
    """Multi-org isolation for a single Job: True unless the gate is ON and the
    job belongs to another org. Byte-identical no-op (always True) while the gate
    is OFF."""
    if not access.multi_org_active():
        return True
    me = security.session_user(request)
    return access.same_org(me, jobs.job_org_id(job))


# --- unified execution history (jobs + workflow runs) ---
@router.get("/jobs")
def jobs_index(
    request: Request,
    type: str = "all",
    status: str = "",
    kind: str = "",
    actor: str = "",
    page: str = "1",
    document: str = "",
    workflow: str = "",
):
    """Chronological, newest-first history of BOTH background Jobs and workflow runs,
    scoped, filtered and paginated. Scope + filter + pagination all live in the
    shared job_history service (web+CLI+api parity); this route only reads query
    params and renders. Scope is applied BEFORE pagination so a page/count can never
    leak across scopes.

    `document`/`workflow` are additive id filters (parity with `job list
    --document/--workflow` + /api/jobs). Scoped-existence: an out-of-scope id is
    confined out by `me`, so it yields an empty list — never an existence oracle."""
    me = security.session_user(request)
    # #603: the pager and the filter form send blank `document=` / `workflow=` / `page=`;
    # a blank or malformed value means "no filter" / page 1, never a raw 422.
    try:
        page = optional_id_param("page", page) or 1
    except RequestValidationError:
        page = 1  # unchanged contract: a malformed page number is page 1, never a 422
    offset = (page - 1) * _PAGE_SIZE
    doc_f = optional_id_param("document", document)
    wf_f = optional_id_param("workflow", workflow)
    with Session(engine) as session:
        rows, total = job_history.unified_history(
            session,
            me,
            type=type,
            status=status,
            kind=kind,
            actor=actor,
            limit=_PAGE_SIZE,
            offset=offset,
            document_id=doc_f,
            workflow_id=wf_f,
        )
    blocked, _, cap = _spend.would_block()
    has_older = offset + _PAGE_SIZE < total
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "request": request,
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": _PAGE_SIZE,
            "has_older": has_older,
            "has_newer": page > 1,
            "f": {
                "type": type or "all",
                "status": status or "",
                "kind": kind or "",
                "actor": actor or "",
                "document": doc_f or "",
                "workflow": wf_f or "",
            },
            "job_kinds": job_history.JOB_KINDS,
            # Install-wide AI spend is manager-and-above data (ADR 0042): other roles get no
            # figures at all (the template renders the line only when this is set).
            "spend": (
                {
                    "spent": _spend.spent_this_period(),
                    "cap": cap,
                    "tripped": blocked,
                    "on": _spend.cap_enabled(),
                }
                if security.can_manage(security.session_user(request))
                else None
            ),
        },
    )


@router.get("/jobs/{jid}/status")
def job_status(jid: int, request: Request):
    job = jobs.get(jid)
    owner = _owner_filter(request)
    if not job or (owner is not None and job.created_by != owner) or not _job_in_org(request, job):
        # 404 (not 403) so ids cannot be enumerated for existence
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(jobs.status_dict(job))
