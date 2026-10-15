"""/api routes: smaller areas

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app.db import engine
from app.kernel import (
    content,
)
from app.web.api_cli_common import _NOT_FOUND, _gate, _int_id, _json
from app.web.streaming import StreamingResponse

router = APIRouter()


# ============================================================================
# Data export (Batch 13, Train B) -- the /api surface over the Train-A service.
#
# POST /api/exports starts an async job (202 + status_url; `existing:true` on a
# dedupe hit); GET /api/exports lists the CALLER's runs (actor-first); GET
# /api/exports/{run_uuid} polls one; GET /api/exports/{run_uuid}/artifact serves
# the completed ZIP (local: Range-resumable 206 over exportstore.open_range; S3:
# 302 to a presigned URL -- the S3 branch is Train C's adapter).
#
# RBAC: _gate (docs scope -- an `api`-only key is 403'd by _require_doc_scope; the
# workspace admin-check lives INSIDE _resolve_scope and refuses 404, not 403). The
# read routes add _export_owns() (DX-01): actor identity FIRST, the identical
# resolved-scope tuple, and -- for a workspace-kind artifact -- the `admin` scope
# AT FETCH; NEVER can_manage-widened; every mismatch is a non-enumerable 404.
#
# TB-NOTE-1 (a HARD LAW from the Train-A gate): the POST ALWAYS passes the
# AUTHENTICATED User to submit_export, NEVER None -- `trusted_operator` is set by
# (user is None), so a remote principal resolving to None would become a
# full-workspace trusted operator. The explicit user-None guard below refuses
# before that can happen; org_id is never taken from a wire field for a real user.
# ============================================================================
def _export_scope_set(ident) -> set:
    from app.kernel import apikeys as _apikeys

    return _apikeys.scope_set((ident or {}).get("scopes", "")) if ident else set()


def _export_run_view(row) -> dict:
    """One DataExportRun -> a JSON-safe poll/list dict (no secret columns exist on the row)."""
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
        "manifest_sha256": row.manifest_sha256,
        "org_id": row.org_id,
        "customer_id": row.customer_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def _export_owns(ident, row) -> bool:
    """DX-01 ownership (STRICTER than the job-poll idiom, NEVER can_manage): the caller's
    `actor` must equal row.actor AND the caller's re-resolved scope tuple must equal the row's
    stored (scope_kind, scope_ref, org_id, customer_id), AND a workspace-kind artifact requires
    the `admin` scope AT FETCH (a key downgraded/rotated after the POST cannot still pull it).
    Any mismatch (incl. an un-resolvable scope) -> False (the route answers a 404)."""
    from app.kernel import data_export as dx

    actor = (ident or {}).get("actor")
    if not actor or row.actor != actor:
        return False
    if row.scope_kind == "workspace" and "admin" not in _export_scope_set(ident):
        return False  # admin AT FETCH (DX-01), not only at POST
    try:
        with Session(engine) as s:
            scope = dx._resolve_scope(
                s,
                user=ident.get("user"),
                ident=ident,
                org_id=None,  # a real user's org derives from the user, never a wire field
                scope_kind=row.scope_kind,
                scope_ref=row.scope_ref,
                require_admin=False,  # the admin-at-fetch check above is the workspace gate
            )
    except Exception:
        return False
    return (scope.scope_kind, scope.scope_ref, dx._org_val(scope), scope.clamp) == (
        row.scope_kind,
        row.scope_ref,
        row.org_id,
        row.customer_id,
    )


@router.post("/api/exports")
async def api_exports_start(request: Request):
    """Start a full-object-graph data export as an async job (mirrors `export run` +
    the /exports form). `docs` scope; the workspace admin-check is inside _resolve_scope
    (404, not 403). Body (all optional): `scope` (self|customer|project|workspace, default
    self), `ref` (the customer/project id for the narrowed scopes), `history`
    (current|full), `include_audit` (workspace-only), `include_eval`, `include_embeddings`.
    Returns **202** `{ok, run_uuid, job_id, status_url}` (+ `existing:true` on a dedupe hit)."""
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    # TB-NOTE-1: a valid docs-scoped per-user key ALWAYS resolves ident["user"]; refuse if it
    # somehow does not, so a None principal can NEVER reach submit_export as a trusted operator.
    user = ident.get("user")
    if user is None:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    body = await _json(request)
    scope_kind = str(body.get("scope") or "self").strip().lower()
    if scope_kind not in ("self", "customer", "project", "workspace"):
        return JSONResponse(
            {"ok": False, "error": "scope must be self|customer|project|workspace."},
            status_code=400,
        )
    scope_ref = None
    if body.get("ref") not in (None, ""):
        scope_ref = _int_id(body.get("ref"))
        if scope_ref is None:
            return JSONResponse({"ok": False, "error": "ref must be an id."}, status_code=400)
    if scope_kind in ("customer", "project") and scope_ref is None:
        return JSONResponse(
            {"ok": False, "error": f"scope={scope_kind} requires a numeric ref."}, status_code=400
        )
    history = "full" if str(body.get("history") or "current").lower() == "full" else "current"
    # Emitters (Train C, §8.1): JSONL is always the backbone; `csv`/`xlsx` add optional human
    # views riding the SAME walker. Accept a list or comma-string; ignore unknowns; force jsonl in.
    raw_em = body.get("emitters") or body.get("formats") or []
    if isinstance(raw_em, str):
        raw_em = raw_em.split(",")
    emitters = tuple(
        e
        for e in ("jsonl", "csv", "xlsx")
        if e == "jsonl" or e in {str(x).strip().lower() for x in raw_em}
    )
    from app.kernel import data_export as dx

    try:
        run_uuid, job_id, created = dx.submit_export(
            user=user,  # TB-NOTE-1: the AUTHENTICATED user, never None (trusted_operator=False)
            ident=ident,
            org_id=None,  # never let a wire field drive org for a real-user resolution
            scope_kind=scope_kind,
            scope_ref=scope_ref,
            history=history,
            emitters=emitters,
            include_audit=bool(body.get("include_audit")),
            include_eval=bool(body.get("include_eval")),
            include_embeddings=bool(body.get("include_embeddings")),
            actor=ident.get("actor"),
        )
    except dx.NotFound:
        # LAW2: an out-of-scope target, or a non-admin asking for workspace -> non-enumerable 404.
        return JSONResponse(_NOT_FOUND, status_code=404)
    except dx.ExportRefused as exc:
        # LAW9 kill switch / DX-208 plaintext-staging refuse-gate.
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    out = {
        "ok": True,
        "run_uuid": run_uuid,
        "job_id": job_id,
        "status_url": f"/api/exports/{run_uuid}",
    }
    if not created:
        out["existing"] = True
    return JSONResponse(out, status_code=202)


@router.get("/api/exports")
def api_exports_list(request: Request):
    """List the CALLER's data-export runs, newest first (mirrors `export list`). `docs`
    scope; filtered ACTOR-first (DataExportRun.actor == the key's actor), then org -- never
    can_manage-widened (DX-01), so a non-owner never sees another caller's run_uuid."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.models import DataExportRun

    actor = ident.get("actor")
    with Session(engine) as s:
        stmt = select(DataExportRun).where(DataExportRun.actor == actor)
        if org_id is not None:
            stmt = stmt.where(DataExportRun.org_id == org_id)
        rows = s.exec(stmt.order_by(DataExportRun.id.desc()).limit(100)).all()
        runs = [_export_run_view(r) for r in rows]
    return JSONResponse({"ok": True, "runs": runs})


@router.get("/api/exports/{run_uuid}")
def api_exports_status(run_uuid: str, request: Request):
    """Poll one data-export run (mirrors `export status`). `docs` scope + _export_owns()
    (DX-01). A run the caller does not own -- or does not exist -- is a non-enumerable 404."""
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.models import DataExportRun

    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if row is None or not _export_owns(ident, row):
            return JSONResponse(_NOT_FOUND, status_code=404)
        view = _export_run_view(row)
    view["download_url"] = f"/api/exports/{run_uuid}/artifact"
    return JSONResponse({"ok": True, **view})


def _parse_range(header: str, total: int):
    """Parse a single `bytes=start-end` Range header against a known `total` size.
    Returns an inclusive (start, end) or None when unsatisfiable (the caller answers 416).
    Only the first range of a single-range request is honored (multipart ranges are not)."""
    header = (header or "").strip()
    if not header.lower().startswith("bytes=") or total <= 0:
        return None
    spec = header[len("bytes=") :].split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    lo, hi = spec.split("-", 1)
    lo, hi = lo.strip(), hi.strip()
    try:
        if lo == "":  # suffix range: bytes=-N -> the last N bytes
            n = int(hi)
            if n <= 0:
                return None
            start = max(0, total - n)
            end = total - 1
        else:
            start = int(lo)
            end = int(hi) if hi != "" else total - 1
    except ValueError:
        return None
    if start < 0 or start > end or start >= total:
        return None
    return start, min(end, total - 1)


@router.get("/api/exports/{run_uuid}/artifact")
def api_exports_artifact(run_uuid: str, request: Request):
    """Download a COMPLETED export artifact ZIP (mirrors `export download`). `docs` scope +
    _export_owns() (DX-01: actor + resolved-scope tuple + admin-at-fetch for a workspace dump).
    Local staging: Range-resumable -- a `Range` header yields a **206** over
    exportstore.open_range() (plaintext staging), else a full **200**. S3 staging (Train C):
    **302** to exportstore.presign_get(). Only an `ok`/`partial` run with a staged key is
    served; anything else is a non-enumerable 404."""
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from datetime import datetime as _dt

    from app.kernel import settings as _settings
    from app.models import DataExportRun
    from app.ports import exportstore

    with Session(engine) as s:
        row = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if row is None or not _export_owns(ident, row):
            return JSONResponse(_NOT_FOUND, status_code=404)
        if row.status not in ("ok", "partial") or not row.artifact_key:
            return JSONResponse(_NOT_FOUND, status_code=404)  # only a COMPLETED artifact is served
        key = row.artifact_key
        total = int(row.artifact_bytes or 0)
    from app.kernel import data_export as dx

    # files-03: re-check coverage AT DOWNLOAD (a run whose scope target was deleted is purged
    # on the spot and refused), the same seam as the web route and the CLI.
    if not dx.artifact_servable(run_uuid):
        return JSONResponse(_NOT_FOUND, status_code=404)

    store = exportstore.get()
    # S3 branch (Train C adapter): a presigned, Range-capable URL -> 302. Disk returns None.
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
    start, end = 0, None  # None end = stream to EOF (the whole file)
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

    # DX-16: stamp the download lease so the Train-C reaper defers deletion mid Range-resume.
    with Session(engine) as s:
        r2 = s.exec(select(DataExportRun).where(DataExportRun.run_uuid == run_uuid)).first()
        if r2 is not None:
            r2.last_downloaded_at = _dt.utcnow()
            s.add(r2)
            s.commit()

    try:
        gen = store.open_range(key, start, end)
    except FileNotFoundError:
        return JSONResponse(_NOT_FOUND, status_code=404)
    return StreamingResponse(
        gen, status_code=status_code, media_type="application/zip", headers=headers
    )
