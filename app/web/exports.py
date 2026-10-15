"""Data export web surface (Batch 13, Train B) -- the /exports section.

Four routes over the Train-A service (app.services.data_export):
  GET  /exports                     -- the list + the "New export" form (scope selector
                                       confined to the caller's OWN visible customers/projects;
                                       the workspace option + audit toggle show only to admins).
  POST /exports                     -- start an async export job -> redirect to the run page.
  GET  /exports/{run_uuid}          -- the run page (polls the status route below via fetch).
  GET  /exports/{run_uuid}/status   -- session-authed JSON poll (the jobs_ui idiom).
  GET  /exports/{run_uuid}/download -- session-authed artifact stream (ownership-checked).

This is a ``writes_off`` module (D3/LAW9): the page + list + status reads stay UP when
``data_export_enabled`` is off (like /memories, /evaluations); the WRITE (POST submit) is
refused at the service layer (data_export._check_refuse_gates -> ExportRefused), surfaced
here as a clean 303-error redirect. Login-walled like the rest of the app.

RBAC/confinement is the service's job: ``data_export.submit_export`` resolves + confines the
scope under the SESSION user (a foreign ref 404s in _resolve_scope), and ``_web_owns`` gates
the status/download reads to the caller's OWN runs (actor + resolved-scope tuple + admin ROLE
at fetch for a workspace dump, the DX-209/DX-01 web parity). The session user is passed to
submit_export as the principal, never None while the login wall is on (the middleware would
have redirected an unauthenticated request first).

Documented in: docs/architecture/data-export.md
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app.db import engine
from app.kernel import access, content, security
from app.models import Customer, DataExportRun, Project
from app.web.deps import _error_page, _ip, _my_org_id, templates
from app.web.form_errors import redirect_with_error
from app.web.streaming import StreamingResponse

router = APIRouter()

_SCOPES = ("self", "customer", "project", "workspace")


def _is_workspace_admin(request) -> bool:
    """DX-209 web parity: a web session with an admin ROLE (or the login wall off, the
    single-operator local mode) may take a whole-workspace dump + the audit slice."""
    if not security.auth_required():
        return True
    return security.is_admin(security.session_user(request))


def _visible_scopes(request):
    """The customers + projects the SESSION user may pick as a narrowed scope -- for the
    form's selects only (the authoritative confinement is data_export._resolve_scope on
    submit, which 404s a foreign ref). Derived from access.visible_project_ids (None =
    see-all), so it never widens past the caller's own visibility."""
    me = security.session_user(request)
    with Session(engine) as s:
        allowed = access.visible_project_ids(s, me)  # None = unrestricted (see-all / auth off)
        pq = select(Project)
        if allowed is not None:
            pq = pq.where(Project.id.in_(allowed)) if allowed else pq.where(Project.id == -1)
        org_id = _my_org_id(request)
        if org_id is not None:
            pq = pq.where(Project.org_id == org_id)
        projects = s.exec(pq.order_by(Project.name).limit(500)).all()
        cust_ids = {p.customer_id for p in projects if p.customer_id is not None}
        customers = (
            s.exec(select(Customer).where(Customer.id.in_(cust_ids)).order_by(Customer.name)).all()
            if cust_ids
            else []
        )
        proj_view = [{"id": p.id, "name": p.name} for p in projects]
        cust_view = [{"id": c.id, "name": c.name} for c in customers]
    return cust_view, proj_view


def _runs_for(request):
    """The caller's OWN export runs (actor-first, org-scoped), newest first. Single-operator
    local mode (auth off) shows the org's runs."""
    me = security.session_user(request)
    actor = getattr(me, "email", None) if me else None
    org_id = _my_org_id(request)
    with Session(engine) as s:
        stmt = select(DataExportRun)
        if security.auth_required() and actor:
            stmt = stmt.where(DataExportRun.actor == actor)
        if org_id is not None:
            stmt = stmt.where(DataExportRun.org_id == org_id)
        rows = s.exec(stmt.order_by(DataExportRun.id.desc()).limit(100)).all()
        return [_run_view(r) for r in rows]


def _run_view(row) -> dict:
    return {
        "run_uuid": row.run_uuid,
        "id": row.id,
        "scope_kind": row.scope_kind,
        "scope_ref": row.scope_ref,
        "history": row.history,
        "status": row.status,
        "counts": row.entity_counts or {},
        "dropped": row.dropped_counts or {},
        "artifact_bytes": row.artifact_bytes,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def _web_owns(request, row) -> bool:
    """DX-01/DX-209 web ownership: the SESSION user's actor must match row.actor AND the
    re-resolved scope tuple must equal the row's stored tuple, AND a workspace dump requires
    the admin ROLE at fetch. Single-operator local mode (auth off) sees everything (the
    jobs_ui idiom). Any mismatch -> False (the route answers 404)."""
    if not security.auth_required():
        return True
    me = security.session_user(request)
    actor = getattr(me, "email", None) if me else None
    if not actor or row.actor != actor:
        return False
    if row.scope_kind == "workspace" and not security.is_admin(me):
        return False
    from app.kernel import data_export as dx

    try:
        with Session(engine) as s:
            scope = dx._resolve_scope(
                s,
                user=me,
                ident=None,
                org_id=None,
                scope_kind=row.scope_kind,
                scope_ref=row.scope_ref,
                require_admin=False,
            )
    except Exception:
        return False
    return (scope.scope_kind, scope.scope_ref, dx._org_val(scope), scope.clamp) == (
        row.scope_kind,
        row.scope_ref,
        row.org_id,
        row.customer_id,
    )


@router.get("/exports")
def exports_index(request: Request):
    customers, projects = _visible_scopes(request)
    return templates.TemplateResponse(
        request,
        "exports.html",
        {
            "request": request,
            "runs": _runs_for(request),
            "customers": customers,
            "projects": projects,
            "is_admin": _is_workspace_admin(request),
            "error": request.query_params.get("error"),
            "started": request.query_params.get("started"),
        },
    )


@router.post("/exports")
def exports_start(
    request: Request,
    scope: str = Form("self"),
    ref: str = Form(""),
    history: str = Form("current"),
    include_audit: str = Form(""),
    include_eval: str = Form(""),
    include_embeddings: str = Form(""),
):
    scope = (scope or "self").strip().lower()
    if scope not in _SCOPES:
        return redirect_with_error("/exports", "bad_scope", param="error")
    scope_ref = None
    if scope in ("customer", "project"):
        try:
            scope_ref = int(ref)
        except (TypeError, ValueError):
            return redirect_with_error("/exports", "scope_needs_id", param="error")
    from app.kernel import data_export as dx

    me = security.session_user(request)
    try:
        run_uuid, _job_id, _created = dx.submit_export(
            user=me,  # the session user is the principal (never None while the login wall is on)
            ident=None,
            org_id=None,
            scope_kind=scope,
            scope_ref=scope_ref,
            history="full" if history == "full" else "current",
            emitters=("jsonl",),
            include_audit=bool(include_audit),
            include_eval=bool(include_eval),
            include_embeddings=bool(include_embeddings),
            actor=getattr(me, "email", None) or "web",
        )
    except dx.NotFound:
        return redirect_with_error("/exports", "export_not_found", param="error")
    except dx.ExportRefused as exc:
        return redirect_with_error("/exports", str(exc), param="error")
    from app.kernel import audit

    audit.log(
        actor=getattr(me, "email", None) or "web",
        action="data_export.web_start",
        target=f"scope={scope}:{scope_ref}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/exports/{run_uuid}?started=1", status_code=303)


@router.get("/exports/{run_uuid}")
def exports_run_page(run_uuid: str, request: Request):
    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if row is None or not _web_owns(request, row):
            return _error_page(request, 404, "Not found.")
        view = _run_view(row)
    return templates.TemplateResponse(
        request,
        "export_run.html",
        {"request": request, "run": view, "started": request.query_params.get("started")},
    )


@router.get("/exports/{run_uuid}/status")
def exports_run_status(run_uuid: str, request: Request):
    """Session-authed JSON poll for the run page (the jobs_ui /jobs/{jid}/status idiom).
    404 for a run the caller does not own or that does not exist (non-enumerable)."""
    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if row is None or not _web_owns(request, row):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(_run_view(row))


@router.get("/exports/{run_uuid}/download")
def exports_download(run_uuid: str, request: Request):
    """Stream a completed artifact ZIP for the SESSION user (ownership-checked). Local
    staging: streamed over exportstore.open_range (Range honored -> 206). S3 staging (Train
    C): 302 to a presigned URL. Only an ok/partial run with a staged key is served."""
    from datetime import datetime as _dt

    from app.kernel import settings as _settings
    from app.ports import exportstore
    from app.web.api_cli import _parse_range

    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if row is None or not _web_owns(request, row):
            return _error_page(request, 404, "Not found.")
        if row.status not in ("ok", "partial") or not row.artifact_key:
            return _error_page(request, 404, "Not found.")
        key = row.artifact_key
        total = int(row.artifact_bytes or 0)
    from app.kernel import data_export as dx

    # files-03: re-check coverage AT DOWNLOAD (a run whose scope target was deleted is purged
    # on the spot and refused), the same seam as /api and the CLI.
    if not dx.artifact_servable(run_uuid):
        return _error_page(request, 404, "Not found.")

    store = exportstore.get()
    ttl = int(_settings.get("data_export_signed_url_ttl", "900") or 900)
    try:
        url = store.presign_get(key, ttl_seconds=ttl)
    except Exception:
        url = None
    if url:
        return RedirectResponse(url, status_code=302)

    filename = f"dataexport_{run_uuid}.zip"
    headers = {
        "Content-Disposition": content.content_disposition_attachment(filename),
        "Accept-Ranges": "bytes",
    }
    status_code = 200
    start, end = 0, None
    range_header = request.headers.get("range")
    if range_header and total:
        parsed = _parse_range(range_header, total)
        if parsed is None:
            return JSONResponse(
                {"ok": False, "error": "range not satisfiable"},
                status_code=416,
                headers={"Content-Range": f"bytes */{total}"},
            )
        start, end = parsed
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        headers["Content-Length"] = str(end - start + 1)
    elif total:
        headers["Content-Length"] = str(total)

    with Session(engine) as s:
        r2 = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if r2 is not None:
            r2.last_downloaded_at = _dt.utcnow()
            s.add(r2)
            s.commit()

    try:
        gen = store.open_range(key, start, end)
    except FileNotFoundError:
        return _error_page(request, 404, "Not found.")
    return StreamingResponse(
        gen, status_code=status_code, media_type="application/zip", headers=headers
    )
