"""api: part3

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import engine
from app.filters import supported_exts
from app.kernel import (
    jobs,
    tm_delivery,
)
from app.kernel.connectors import store as conn_store
from app.models import Document
from app.web import deps
from app.web.api_discovery import (
    _UNAUTHORIZED,
    _doc_authz,
    _ident_org_id,
    _jira_batch_admin_block,
    _jira_named_keys_block,
    _job_readable,
)
from app.web.api_discovery_common import checked_retry_failures
from app.web.api_part2 import (
    _FMT_LABELS,
    _conn_gate,
    _conn_in_org,
    _conn_role_block,
    _write_role_block,
)

router = APIRouter()


async def _article_translate(request: Request, cid: int, translate_fn):
    """Shared body for the pure per-connector article-translate API routes
    (document360/freshdesk/zoho/intercom/salesforce/notion/helpscout): gate ->
    normalize article_ids/article_id (str -> [str]) -> call the connector's
    translate function -> the standard {ok,message,**summary} envelope. Byte-for-
    byte identical to the inlined bodies it replaces; each route keeps its own
    decorator + docstring so companion apps hit the exact same URL and JSON."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    arts = payload.get("article_ids") or (
        [payload["article_id"]] if payload.get("article_id") else None
    )
    if isinstance(arts, str):
        arts = [arts]
    # #599: a connector run calls the engine and the external system; never on the loop.
    try:
        ok, msg, summary = await deps.offload_blocking(
            translate_fn,
            cid,
            article_ids=arts,
            target_locales=payload.get("target_locales"),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/pull")
def api_pull(cid: int, request: Request):
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    ok, msg, doc_id = conn_store.pull_connection(cid, actor=ident["actor"])
    return JSONResponse(
        {"ok": ok, "message": msg, "document_id": doc_id}, status_code=200 if ok else 400
    )


@router.post("/api/connectors/{cid}/push")
def api_push(cid: int, request: Request):
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    ok, msg, n = conn_store.push_connection(cid, actor=ident["actor"])
    return JSONResponse({"ok": ok, "message": msg, "pushed": n}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/sync")
async def api_sync(cid: int, request: Request):
    """One-click 'Translate now', dispatched per kind. The companion-app button calls
    this. Pass {"async": true} to enqueue instead and get a job id back immediately
    (poll GET /api/jobs/{id}) — recommended for big bases; callers with short
    timeouts (Airtable automations) otherwise risk cutting the run off."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    blocked = _jira_batch_admin_block(ident, conn_store.get(cid).kind)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    if payload.get("async"):
        # Deduped: a retry (companion-app automations time out and re-POST) while
        # a run for this connection is in flight gets the LIVE job back instead of
        # stacking a second run against paid MT + the external system.
        jid, created = jobs.submit_unique(
            "connector_translate",
            {"connection_id": cid, "actor": ident["actor"]},
            actor=ident["actor"],
        )
        body = {"ok": True, "job_id": jid, "status_url": f"/api/jobs/{jid}"}
        if not created:
            body["existing"] = True
        return JSONResponse(body, status_code=202)
    # #599: a connector run calls the engine and the external system; never on the loop.
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.translate_now, cid, actor=ident["actor"]
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/retry")
async def api_retry(cid: int, request: Request):
    """Re-run only the failed items of a prior connector run. Body is either
    {"job_id": N} (failures read from that job's stored result) or an explicit
    {"failures": [...]} list. Always enqueued (202 + job id); submit_unique
    dedups against an in-flight run of the same connection."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    failures = payload.get("failures")
    caller_built = bool(failures)
    if not failures and payload.get("job_id"):
        job = jobs.get(payload["job_id"])
        if not job or not _job_readable(ident, job):
            return JSONResponse({"ok": False, "error": "no failed items to retry"}, status_code=400)
        # Cross-connection guard: the job must belong to THIS connection and be a
        # connector_translate job — reject a job_id from another connection/kind.
        if job.kind != "connector_translate" or (job.payload or {}).get("connection_id") != cid:
            return JSONResponse(
                {"ok": False, "error": "Job does not belong to this connection."}, status_code=400
            )
        # Dual-surface parity (review F5): retry the run's real failures AND any
        # INCOMPLETE / validation-BLOCKED pairs (Contentful partial/blocked writes).
        # Both keys are absent for other connectors, so this is a no-op for them.
        jr = job.result or {}
        failures = (
            (jr.get("failures") or []) + (jr.get("incomplete") or []) + (jr.get("blocked") or [])
        )
    failures, refused = checked_retry_failures(failures)
    if refused:
        return refused
    if caller_built:
        # A hand-written failures list on Jira is a named-issue batch (#517 gate).
        blocked = _jira_named_keys_block(
            ident, conn_store.get(cid).kind, [f.get("issue_key") for f in failures]
        )
        if blocked:
            return blocked
    jid, created = jobs.submit_unique(
        "connector_translate",
        {"connection_id": cid, "actor": ident["actor"], "retry_failures": failures},
        actor=ident["actor"],
    )
    body = {"ok": True, "job_id": jid, "status_url": f"/api/jobs/{jid}"}
    if not created:
        body["existing"] = True
    return JSONResponse(body, status_code=202)


@router.post("/api/connectors/{cid}/jira/translate")
async def api_jira_translate(cid: int, request: Request):
    """Translate specific Jira issue(s) on the fly into one or more languages —
    the backend for an in-Jira 'Translate' button."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    keys = payload.get("issue_keys") or (
        [payload["issue_key"]] if payload.get("issue_key") else None
    )
    if isinstance(keys, str):
        keys = [keys]
    if keys is not None and not isinstance(keys, list):
        return JSONResponse(
            {"ok": False, "error": "issue_keys must be a list of issue keys."}, status_code=400
        )
    if keys:
        keys = [k.strip() if isinstance(k, str) else k for k in keys]
    # No explicit issues means a JQL batch, and a long key list is a batch in disguise
    # (a loop of single-issue calls): admin only (#517). Each key is shape-checked at the
    # connector's single scope choke point, so one element cannot smuggle a batch either.
    blocked = (
        _jira_batch_admin_block(ident, "jira")
        if not keys
        else _jira_named_keys_block(ident, "jira", keys)
    )
    if blocked:
        return blocked
    # #599: a connector run calls the engine and the external system; never on the loop.
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.jira_translate,
            cid,
            issue_keys=keys,
            jql=payload.get("jql"),
            target_locales=payload.get("target_locales"),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/documents/{doc_id}/deliver-approve")
def api_document_deliver_approve(doc_id: int, request: Request, locale: str = ""):
    """Approve delivering the CURRENT empty targets of (doc, locale) as empty strings. A
    human delivery decision (can_write + doc authz); audited delivery.empty_approved."""
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    blocked = _write_role_block(ident)
    if blocked:
        return blocked
    locale = (locale or "").strip()
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        _project, denied = _doc_authz(s, ident, doc)
        if denied:
            return denied
        try:
            row = tm_delivery.approve_doc(s, doc, locale, approved_by=ident["actor"])
        except tm_delivery.LocaleNotDeliverable as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "approved": True,
                "locale": locale,
                "covered_count": row.covered_count,
            }
        )


@router.post("/api/documents/{doc_id}/deliver-revoke")
def api_document_deliver_revoke(doc_id: int, request: Request, locale: str = ""):
    """Revoke a prior empty-delivery approval for (doc, locale) -> re-held. A no-record
    revoke is a 200 no-op (already held by the live default). Audited when it revokes."""
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    blocked = _write_role_block(ident)
    if blocked:
        return blocked
    locale = (locale or "").strip()
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        _project, denied = _doc_authz(s, ident, doc)
        if denied:
            return denied
        try:
            revoked = tm_delivery.revoke_doc(s, doc, locale, approved_by=ident["actor"])
        except tm_delivery.LocaleNotDeliverable as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "revoked": revoked, "locale": locale})


@router.get("/api/formats")
def api_formats(request: Request):
    """Supported upload/download formats (filter registry) with friendly names.
    `batch_formats` lists the containers an upload expands into one document per
    supported member (Batch 13: `.zip`); they are not document formats themselves.
    Requires a valid user identity (docs scope)."""
    _ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    exts = supported_exts()
    return JSONResponse(
        {
            "ok": True,
            "formats": [{"ext": e, "name": _FMT_LABELS.get(e, e.upper())} for e in exts],
            "batch_formats": [
                {"ext": "zip", "name": "ZIP batch (one document per supported file, folders kept)"}
            ],
        }
    )


def _conn_create_gate(request: Request, predicate):
    """Preamble for connector CREATE (no cid to ring): identity (401) -> unbound-shared-
    token wall -> role. Returns (ident, org_id, blocked)."""
    ident = deps.api_identity(request)
    if ident is None:
        return None, None, JSONResponse(_UNAUTHORIZED, status_code=401)
    blocked = deps._api_org_block(request, ident) or _conn_role_block(ident, predicate)
    if blocked:
        return ident, None, blocked
    return ident, _ident_org_id(ident), None


def _conn_cid_gate(request: Request, cid: int, predicate):
    """Preamble for connector {cid} CRUD, RING-BEFORE-ROLE: identity (401) -> unbound-
    shared-token wall -> _conn_in_org (a foreign/missing cid is 404, indistinguishable
    from missing, BEFORE the role gate can turn it into a 403 existence oracle) -> role.
    Returns (ident, blocked). Mirrors the members routes' ring-before-role order and
    keeps every /api/connectors/{cid}/* route 404-on-foreign for any caller (the
    permanent connector-isolation sweep)."""
    ident = deps.api_identity(request)
    if ident is None:
        return None, JSONResponse(_UNAUTHORIZED, status_code=401)
    blocked = (
        deps._api_org_block(request, ident)
        or _conn_in_org(ident, cid)
        or _conn_role_block(ident, predicate)
    )
    return ident, blocked
