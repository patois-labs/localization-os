"""Inbound API (token-authed) for companion apps and per-user API keys.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _app_version, _APP_VERSION, _APP_VERSION_COARSE,
_UNAUTHORIZED, _NOT_FOUND, _FORBIDDEN, _ai_role_block, _FAILURE_KEYS, _FAILURE_IDS, _FAILURE_CAP,
_FAILURE_COUNT_CAP, _clean_failures, _job_readable, vendor_return_callback, vendor_quote_callback,
_ident_org_id, _ensure_inbox_project, _ensure_doc_locale_segments, _doc_authz, _dedupe,
_active_translate_locales, api_translate_document, api_document_paragraphs, api_download_document,
api_release, _CONNECTOR_SECRET_FIELDS, _find_secret_fields, _conn_row, _write_role_block,
_conn_in_org, _conn_gate, _FMT_LABELS, api_health, api_list_connections, api_job_status,
connector_webhook_intake, api_upload_document, api_translate_text, api_document_paragraph_revert,
api_document_paragraph_revert_all, api_document_paragraph_save, api_document_empties, api_languages,
api_capability_card, _reject_connector_secret, _conn_role_block, _article_translate, api_pull,
api_push, api_sync, api_retry, api_jira_translate, api_document_deliver_approve,
api_document_deliver_revoke, api_formats, _conn_create_gate, _conn_cid_gate from
app.web.api_discovery, app.web.api_part2, app.web.api_part3.

Identity comes from deps.api_identity(): a per-user key ("lsu_...") resolves to
that user's email for attribution; the legacy shared connector token is intended
for companion apps (Airtable Extension / Forge) ONLY and acts as the
"companion-app" pseudo-actor.

The document endpoints (POST /api/documents, .../translate, .../download and the
/api/languages + /api/formats discovery routes) are the Claude Desktop / MCP
"front door" (Phase A). They require a per-user key carrying the "docs" scope
(deps._require_doc_scope) — the shared connector token is rejected outright and a
plain "api" key is 403 — plus object-level authz on the document's project. No
`mcp` SDK import lives anywhere in the app; these are plain HTTP routes an MCP
stdio server (built separately) calls over localhost.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from copy import copy as _srp_copy

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.filters import supported_exts
from app.kernel import (
    access,
    audit,
    budgets,
    content,
    export,
    ids,
    jobs,
    languages,
    languages_catalog,
    pair_providers,
    project_budgets,
    providers,
    routing,
    security,
    tm_delivery,
)
from app.kernel import connectors as connectors_registry
from app.kernel.connectors import store as conn_store
from app.kernel.connectors import webhooks
from app.kernel.ingest import NotADocumentProject, UnsupportedFormat, ingest_document
from app.models import Document, Job, JobStatus, Project, Segment, TargetLocale
from app.release import coarse_version
from app.web import deps
from app.web.api_discovery import (
    _APP_VERSION,
    _APP_VERSION_COARSE,
    _CONNECTOR_SECRET_FIELDS,
    _FAILURE_CAP,
    _FAILURE_COUNT_CAP,
    _FAILURE_IDS,
    _FAILURE_KEYS,
    _FORBIDDEN,
    _NOT_FOUND,
    _UNAUTHORIZED,
    _active_translate_locales,
    _ai_role_block,
    _app_version,
    _clean_failures,
    _conn_row,
    _dedupe,
    _doc_authz,
    _ensure_doc_locale_segments,
    _ensure_inbox_project,
    _find_secret_fields,
    _ident_org_id,
    _jira_batch_admin_block,
    _jira_schedule_admin_block,
    _job_readable,
    api_document_paragraphs,
    api_download_document,
    api_release,
    api_translate_document,
    vendor_quote_callback,
    vendor_return_callback,
)
from app.web.api_discovery import router as api_discovery_router
from app.web.api_part2 import (
    _FMT_LABELS,
    _conn_gate,
    _conn_in_org,
    _conn_role_block,
    _reject_connector_secret,
    _write_role_block,
    api_capability_card,
    api_document_empties,
    api_document_paragraph_revert,
    api_document_paragraph_revert_all,
    api_document_paragraph_save,
    api_health,
    api_job_status,
    api_languages,
    api_list_connections,
    api_translate_text,
    api_upload_document,
    connector_webhook_intake,
)
from app.web.api_part2 import router as api_part2_router
from app.web.api_part3 import (
    _article_translate,
    _conn_cid_gate,
    _conn_create_gate,
    api_document_deliver_approve,
    api_document_deliver_revoke,
    api_formats,
    api_jira_translate,
    api_pull,
    api_push,
    api_retry,
    api_sync,
)
from app.web.api_part3 import router as api_part3_router

__all__ = [
    "_APP_VERSION",
    "_APP_VERSION_COARSE",
    "_CONNECTOR_SECRET_FIELDS",
    "_FAILURE_CAP",
    "_FAILURE_COUNT_CAP",
    "_FAILURE_IDS",
    "_FAILURE_KEYS",
    "_FMT_LABELS",
    "_FORBIDDEN",
    "_NOT_FOUND",
    "_UNAUTHORIZED",
    "Document",
    "File",
    "Form",
    "Job",
    "JobStatus",
    "NotADocumentProject",
    "Project",
    "Response",
    "Segment",
    "Session",
    "TargetLocale",
    "UnsupportedFormat",
    "UploadFile",
    "_active_translate_locales",
    "_ai_role_block",
    "_app_version",
    "_article_translate",
    "_clean_failures",
    "_conn_cid_gate",
    "_conn_create_gate",
    "_conn_gate",
    "_conn_in_org",
    "_conn_role_block",
    "_conn_row",
    "_dedupe",
    "_doc_authz",
    "_ensure_doc_locale_segments",
    "_ensure_inbox_project",
    "_find_secret_fields",
    "_ident_org_id",
    "_jira_batch_admin_block",
    "_jira_schedule_admin_block",
    "_job_readable",
    "_reject_connector_secret",
    "_write_role_block",
    "access",
    "api_capability_card",
    "api_document_deliver_approve",
    "api_document_deliver_revoke",
    "api_document_empties",
    "api_document_paragraph_revert",
    "api_document_paragraph_revert_all",
    "api_document_paragraph_save",
    "api_document_paragraphs",
    "api_download_document",
    "api_formats",
    "api_health",
    "api_jira_translate",
    "api_job_status",
    "api_languages",
    "api_list_connections",
    "api_pull",
    "api_push",
    "api_release",
    "api_retry",
    "api_sync",
    "api_translate_document",
    "api_translate_text",
    "api_upload_document",
    "budgets",
    "coarse_version",
    "config",
    "connector_webhook_intake",
    "content",
    "engine",
    "export",
    "ids",
    "ingest_document",
    "jobs",
    "languages",
    "languages_catalog",
    "pair_providers",
    "project_budgets",
    "providers",
    "routing",
    "select",
    "supported_exts",
    "tm_delivery",
    "vendor_quote_callback",
    "vendor_return_callback",
    "webhooks",
]

router = APIRouter()


_srp_piece_router = api_part2_router
api_part2_router = _srp_copy(api_part2_router)
api_part2_router.routes = api_part2_router.routes[0:2]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part2_router = _srp_piece_router


_srp_piece_router = api_part3_router
api_part3_router = _srp_copy(api_part3_router)
api_part3_router.routes = api_part3_router.routes[0:4]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part3_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part3_router = _srp_piece_router


_srp_piece_router = api_part2_router
api_part2_router = _srp_copy(api_part2_router)
api_part2_router.routes = api_part2_router.routes[2:3]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part2_router = _srp_piece_router


_srp_piece_router = api_part3_router
api_part3_router = _srp_copy(api_part3_router)
api_part3_router.routes = api_part3_router.routes[4:5]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part3_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part3_router = _srp_piece_router


@router.post("/api/connectors/{cid}/jira/translate-pending")
async def api_jira_translate_pending(cid: int, request: Request):
    """Release the connection's PENDING issue keys — the deliberate human step the
    button-trigger posture requires. Jira Phase 2: a verified push on a connection
    without auto_translate_on_change records the changed issue(s) as pending instead
    of billing MT; this translates exactly those and clears them on success."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    blocked = _jira_batch_admin_block(ident, "jira")
    if blocked:
        return blocked
    # #599: a connector run calls the engine and the external system; never on the loop.
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.jira_translate_pending, cid, actor=ident["actor"]
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/jira/count")
async def api_jira_count(cid: int, request: Request):
    """Issue-count PREVIEW (G8): how many issues a JQL — scoped to the connection —
    would touch, without translating any. Read-only UX so an operator sees the blast
    radius before spending a batch run; the scope guard applies (a hostile un-wrappable
    caller jql is refused, the count is scoped)."""
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
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.jira_count,
            cid,
            jql=payload.get("jql"),
            issue_keys=keys,
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/confluence/translate")
async def api_confluence_translate(cid: int, request: Request):
    """Translate Confluence page(s) on the fly into one or more languages,
    creating a localized child page per language."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    pages = payload.get("page_ids") or ([payload["page_id"]] if payload.get("page_id") else None)
    if isinstance(pages, str):
        pages = [pages]
    # #599: a connector run calls the engine and the external system; never on the loop.
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.confluence_translate,
            cid,
            page_ids=pages,
            space_key=payload.get("space_key"),
            cql=payload.get("cql"),
            target_locales=payload.get("target_locales"),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/document360/translate")
async def api_document360_translate(cid: int, request: Request):
    """Translate Document360 article(s) on the fly into one or more langCodes,
    writing each back as the article's per-language version (T2)."""
    return await _article_translate(request, cid, conn_store.document360_translate)


@router.post("/api/connectors/{cid}/freshdesk/translate")
async def api_freshdesk_translate(cid: int, request: Request):
    """Translate Freshdesk Solutions article(s) on the fly into one or more supported
    languages, writing each back as the article's per-language version (T2)."""
    return await _article_translate(request, cid, conn_store.freshdesk_translate)


@router.post("/api/connectors/{cid}/zoho/translate")
async def api_zoho_translate(cid: int, request: Request):
    """Translate Zoho Desk article(s) on the fly into one or more locales, writing
    each back as the article's per-locale translation object (T2)."""
    return await _article_translate(request, cid, conn_store.zoho_translate)


@router.post("/api/connectors/{cid}/strapi/translate")
async def api_strapi_translate(cid: int, request: Request):
    """Translate Strapi collection-type entry(ies) on the fly into one or more locales,
    writing each back as the entry's per-locale variant (T2; v5 documentId / v4
    localization)."""
    return await _article_translate(request, cid, conn_store.strapi_translate)


@router.post("/api/connectors/{cid}/contentful/translate")
async def api_contentful_translate(cid: int, request: Request):
    """Translate Contentful entry(ies) on the fly into one or more locales, writing
    each back as a new locale key on the same entry (T1; fields.{id}.{locale},
    version-locked read-merge-write)."""
    return await _article_translate(request, cid, conn_store.contentful_translate)


@router.post("/api/connectors/{cid}/shopify/translate")
async def api_shopify_translate(cid: int, request: Request):
    """Translate Shopify resource(s) on the fly into one or more locales, registering each
    translation against the source resource (T2; translationsRegister, addressed by
    resource + key + locale, digest-gated)."""
    return await _article_translate(request, cid, conn_store.shopify_translate)


@router.post("/api/connectors/{cid}/hubspot/translate")
async def api_hubspot_translate(cid: int, request: Request):
    """Translate HubSpot site page(s), landing page(s) or blog post(s) on the fly into one
    or more languages, each written as a HubSpot language variation linked into the
    object's own multi-language group (T3 with native linkage; draft by default, never
    published). Marketing emails/forms are not supported."""
    return await _article_translate(request, cid, conn_store.hubspot_translate)


@router.post("/api/connectors/{cid}/akeneo/translate")
async def api_akeneo_translate(cid: int, request: Request):
    """Translate Akeneo product/product-model attribute values on the fly into one or more locales, writing back only the touched (attribute, locale, scope) triples as a partial PATCH merge (T1). The content-type half of each id is validated against {product, product_model}."""  # noqa: E501
    return await _article_translate(request, cid, conn_store.akeneo_translate)


@router.post("/api/connectors/{cid}/mcp_cms/translate")
async def api_mcp_cms_translate(cid: int, request: Request):
    """Translate CMS items through the CMS's own MCP server into one or more locales,
    writing each back as the item's per-locale variant through the mapped write tool (T2)."""
    return await _article_translate(request, cid, conn_store.mcp_cms_translate)


@router.post("/api/connectors/{cid}/servicenow/translate")
async def api_servicenow_translate(cid: int, request: Request):
    """Translate ServiceNow Knowledge articles on the fly into one or more languages. Each
    translation is created (or updated) as a separate Draft kb_knowledge record in the target
    language, linked to the source article by its Parent field (T3). Each id is a 32-character
    article sys_id and must belong to the connection's configured knowledge base."""
    return await _article_translate(request, cid, conn_store.servicenow_translate)


@router.post("/api/connectors/{cid}/servicenow/resolve-link")
async def api_servicenow_resolve_link(cid: int, request: Request):
    """Resolve a ServiceNow needs_link id-map row (SPEC-t3 rule 6): a translation that already
    exists under the source's Parent but that the map never created. POST {source_id,
    target_locale, action:'adopt'|'ignore'}. 'adopt' accepts it (next run updates it while it
    is Draft); 'ignore' drops the row (it re-flags). Never overwrites content, only edits the
    id map."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.servicenow_resolve_link,
            cid,
            str(payload.get("source_id") or ""),
            str(payload.get("target_locale") or ""),
            str(payload.get("action") or ""),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/msgraph/translate")
async def api_msgraph_translate(cid: int, request: Request):
    """Translate files in the connection's source folder (SharePoint library or OneDrive) into
    one or more locales, uploading each translation as a copy in the locale's folder under the
    target root (T3 document per locale). Each id must be the id of a file inside the
    configured source folder."""
    return await _article_translate(request, cid, conn_store.msgraph_translate)


@router.post("/api/connectors/{cid}/drupal/translate")
async def api_drupal_translate(cid: int, request: Request):
    """EXPERIMENTAL (depends on the alpha JSON:API Multilingual module). Translate Drupal entity(ies) on the fly into one or more languages, writing each back as the entity's own translation through the JSON:API Multilingual module (T2; the translation is created unpublished, the source language is never written). The type half of each id (for example node--article) is validated against the connection's configured resource types."""  # noqa: E501
    return await _article_translate(request, cid, conn_store.drupal_translate)


@router.post("/api/connectors/{cid}/github/translate")
async def api_github_translate(cid: int, request: Request):
    """Localize the repository's resource files into the connection's target locales and open
    (or update) one pull request holding the target-language files (T3). article_ids are source
    file paths and must match the connection's configured source path patterns."""
    return await _article_translate(request, cid, conn_store.github_translate)


@router.post("/api/connectors/{cid}/sanity/translate")
async def api_sanity_translate(cid: int, request: Request):
    """Translate Sanity document(s) on the fly into one or more languages. Field-level projects
    get a new language item in each internationalized array field of the same document;
    document-level projects get a linked translation document. Both write to the draft and
    never publish. The document id must be one of the connection's configured document types."""
    return await _article_translate(request, cid, conn_store.sanity_translate)


@router.post("/api/connectors/{cid}/sitecore/translate")
async def api_sitecore_translate(cid: int, request: Request):
    """Translate Sitecore XM Cloud content item(s) on the fly into one or more languages, writing each back as a language version of the same item (T2; addItemVersion then updateItem). Each id is a strict item GUID that must sit under the connection's content root path."""  # noqa: E501
    return await _article_translate(request, cid, conn_store.sitecore_translate)


@router.post("/api/connectors/{cid}/zendesk/translate")
async def api_zendesk_translate(cid: int, request: Request):
    """Translate Zendesk Guide article(s) on the fly into one or more locales, writing each
    back as the article's per-locale translation object (T2). Article ids are digits only
    and must sit inside the connection's configured section or category."""
    return await _article_translate(request, cid, conn_store.zendesk_translate)


@router.post("/api/connectors/{cid}/gdocs/translate")
async def api_gdocs_translate(cid: int, request: Request):
    """Translate Google Docs in the connection's source Drive folder into one or more locales, writing each translation as a new Google Doc in the locale's folder under the target folder (T3 document per locale). Each id must be the Drive file id of a Google Doc inside the configured source folder."""  # noqa: E501
    return await _article_translate(request, cid, conn_store.gdocs_translate)


@router.post("/api/connectors/{cid}/bitbucket/translate")
async def api_bitbucket_translate(cid: int, request: Request):
    """Localize the repository's resource files into the connection's target locales and open (or update) one pull request holding the target-language files (T3). article_ids are source file paths and must match the connection's configured source path patterns."""  # noqa: E501
    return await _article_translate(request, cid, conn_store.bitbucket_translate)


@router.post("/api/connectors/{cid}/plytix/translate")
async def api_plytix_translate(cid: int, request: Request):
    """Translate Plytix product text attributes on the fly into one or more languages, writing
    each translation to its configured per-language target attribute with a partial PATCH (T1
    attribute map). The source attribute is never written. article ids are Plytix product ids."""
    return await _article_translate(request, cid, conn_store.plytix_translate)


@router.post("/api/connectors/{cid}/gitlab/translate")
async def api_gitlab_translate(cid: int, request: Request):
    """Localize the project's resource files into the connection's target locales and open (or
    update) one merge request holding the target-language files (T3). article_ids are source
    file paths and must match the connection's configured source path patterns."""
    return await _article_translate(request, cid, conn_store.gitlab_translate)


# factory:anchor api.routes  (scripts/factory inserts new connector /translate routes above this line)  # noqa: E501


@router.post("/api/connectors/{cid}/intercom/translate")
async def api_intercom_translate(cid: int, request: Request):
    """Translate Intercom Help Center article(s) on the fly into one or more locales,
    writing each into the article's per-locale translated_content map (T1)."""
    return await _article_translate(request, cid, conn_store.intercom_translate)


@router.post("/api/connectors/{cid}/salesforce/translate")
async def api_salesforce_translate(cid: int, request: Request):
    """Translate Salesforce Knowledge article(s) on the fly into one or more
    languages, writing each back as the article's own translation version (T2).
    article_id(s) are KnowledgeArticleIds."""
    return await _article_translate(request, cid, conn_store.salesforce_translate)


@router.post("/api/connectors/{cid}/salesforce/rediscover-fields")
async def api_salesforce_rediscover_fields(cid: int, request: Request):
    """Re-run Describe and persist the per-record-type translatable field map
    (CLI/web parity for the 'rediscover fields' action)."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.salesforce_rediscover_fields, cid, actor=ident["actor"]
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/notion/translate")
async def api_notion_translate(cid: int, request: Request):
    """Translate Notion page(s) on the fly into one or more locales, each written as
    a per-locale CHILD page of the source (T3). article_id(s) are source page ids."""
    return await _article_translate(request, cid, conn_store.notion_translate)


@router.post("/api/connectors/{cid}/notion/resolve-link")
async def api_notion_resolve_link(cid: int, request: Request):
    """Resolve a Notion needs_link id-map row (SPEC-t3 rule 6): a pre-existing target
    page the map never created. POST {source_id, target_locale, action:'adopt'|'ignore'}.
    'adopt' accepts it (next run updates it); 'ignore' drops the row (it re-flags).
    Never overwrites content — only edits the id map (CLI/web parity)."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.notion_resolve_link,
            cid,
            str(payload.get("source_id") or ""),
            str(payload.get("target_locale") or ""),
            str(payload.get("action") or ""),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/helpscout/translate")
async def api_helpscout_translate(cid: int, request: Request):
    """Translate Help Scout Docs article(s) on the fly into one or more locales, each
    written as a per-locale article in the mapped target collection (T3). article_id(s)
    are source article ids."""
    return await _article_translate(request, cid, conn_store.helpscout_translate)


@router.post("/api/connectors/{cid}/helpscout/resolve-link")
async def api_helpscout_resolve_link(cid: int, request: Request):
    """Resolve a Help Scout needs_link id-map row (SPEC-t3 rule 6): a pre-existing
    target article the map never created. POST {source_id, target_locale,
    action:'adopt'|'ignore'}. 'adopt' accepts it (next run updates it); 'ignore' drops
    the row (it re-flags). Never overwrites content — only edits the id map."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.helpscout_resolve_link,
            cid,
            str(payload.get("source_id") or ""),
            str(payload.get("target_locale") or ""),
            str(payload.get("action") or ""),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/wordpress/translate")
async def api_wordpress_translate(cid: int, request: Request):
    """Translate WordPress post(s)/page(s) on the fly into one or more locales, each
    written as a per-locale post in the target language (T3, doc-per-locale via Polylang).
    article_id(s) are source refs '<type>:<id>' (e.g. 'posts:12')."""
    return await _article_translate(request, cid, conn_store.wordpress_translate)


@router.post("/api/connectors/{cid}/wordpress/resolve-link")
async def api_wordpress_resolve_link(cid: int, request: Request):
    """Resolve a WordPress needs_link id-map row (SPEC-t3 rule 6): a pre-existing Polylang-
    linked target post the map never created. POST {source_id, target_locale,
    action:'adopt'|'ignore'}. 'adopt' accepts it (next run updates it); 'ignore' drops the
    row (it re-flags). Never overwrites content — only edits the id map."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.wordpress_resolve_link,
            cid,
            str(payload.get("source_id") or ""),
            str(payload.get("target_locale") or ""),
            str(payload.get("action") or ""),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/airtable/webhook/register")
async def api_airtable_webhook_register(cid: int, request: Request):
    """Register the official Airtable webhook (Phase 2). CLI-parity mirror of
    `connector airtable-webhook register`. Refuses cleanly (400) when app_base_url
    is not a public https URL (Airtable cannot reach localhost)."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    try:
        ok, msg = await deps.offload_blocking(
            conn_store.register_airtable_webhook,
            cid,
            base_id=payload.get("base_id"),
            table=payload.get("table"),
            source_field=payload.get("source_field"),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/airtable/webhook/unregister")
async def api_airtable_webhook_unregister(cid: int, request: Request):
    """Delete the official Airtable webhook + clear its stored state. CLI-parity
    mirror of `connector airtable-webhook unregister`."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        ok, msg = await deps.offload_blocking(
            conn_store.unregister_airtable_webhook, cid, actor=ident["actor"]
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@router.get("/api/connectors/{cid}/airtable/webhook")
async def api_airtable_webhook_status(cid: int, request: Request):
    """Read-only official-webhook state. CLI-parity mirror of `connector
    airtable-webhook status`."""
    _ident, blocked = _conn_gate(request, cid, role=False)
    if blocked:
        return blocked
    try:
        return JSONResponse(
            {"ok": True, **await deps.offload_blocking(conn_store.airtable_webhook_status, cid)}
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))


@router.post("/api/connectors/{cid}/airtable/webhook/translate-pending")
async def api_airtable_webhook_translate_pending(cid: int, request: Request):
    """Translate the records a ping recorded as pending (auto-translate off). CLI-
    parity mirror of `connector airtable-webhook translate-pending`."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    # #599: a connector run calls the engine and the external system; never on the loop.
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.consume_airtable_payloads, cid, actor=ident["actor"], force=True
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/airtable/webhook/health")
async def api_airtable_webhook_health(cid: int, request: Request):
    """Probe + surface the official webhook's health (round-2 #6): detect the
    watched-field-deleted / paused error state Airtable does NOT 404, persist it, and
    return the current state. CLI-parity mirror of `connector airtable-webhook health`."""
    _ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        ok, msg, info = await deps.offload_blocking(
            conn_store.check_airtable_webhook_health, cid, transport=None
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **info}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/airtable/records")
async def api_airtable_create_records(cid: int, request: Request):
    """Create new Airtable record(s) (round-2 #1). Body: {rows:[{field:value},...],
    base?, table?, typecast?}. Manual/API-triggered only — NOT part of the auto sync.
    CLI-parity mirror of `connector airtable-record create`."""
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    rows = payload.get("rows")
    if isinstance(rows, dict):
        rows = [rows]
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.airtable_create_records,
            cid,
            rows if isinstance(rows, list) else [],
            base_id=payload.get("base_id"),
            table=payload.get("table"),
            typecast=bool(payload.get("typecast")),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/airtable/records/delete")
async def api_airtable_delete_records(cid: int, request: Request):
    """Delete Airtable record(s) by id (round-2 #1). Body: {record_ids:[...], base?,
    table?}. Manual/API-triggered only. CLI-parity mirror of `connector
    airtable-record delete`."""
    ident, blocked = _conn_cid_gate(request, cid, security.is_admin)
    if blocked:
        return blocked
    if ident.get("kind") != "user":
        return JSONResponse(_FORBIDDEN, status_code=403)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    rec = payload.get("record_ids")
    if isinstance(rec, str):
        rec = [rec]
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.airtable_delete_records,
            cid,
            rec if isinstance(rec, list) else [],
            base_id=payload.get("base_id"),
            table=payload.get("table"),
            actor=ident["actor"],
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/airtable/webhook")
async def api_airtable_webhook(cid: int, request: Request):
    """Convention-driven webhook fired from an Airtable Automation. The payload
    carries the base/table/record(s); the app auto-detects target-language
    columns and translates into them. base/table/IDs need NOT be pre-configured."""
    # Automation uses a dedicated per-user key. The shared role guard refuses
    # legacy companion credentials, then enforces the user's AI role and confinement.
    ident, blocked = _conn_gate(request, cid)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    rec = payload.get("record_ids")
    if isinstance(rec, str):
        rec = [rec]
    if not rec and payload.get("record_id"):
        rec = [payload["record_id"]]
    # keep the historical webhook attribution for the shared connector token
    actor = ident["actor"] if ident["kind"] == "user" else "airtable-automation"
    # #599: a connector run calls the engine and the external system; never on the loop.
    try:
        ok, msg, summary = await deps.offload_blocking(
            conn_store.dynamic_translate,
            cid,
            base_id=payload.get("base_id"),
            table=payload.get("table"),
            record_ids=rec,
            source_field=payload.get("source_field"),
            jobs=payload.get("jobs"),
            actor=actor,
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg, **summary}, status_code=200 if ok else 400)


_srp_piece_router = api_part2_router
api_part2_router = _srp_copy(api_part2_router)
api_part2_router.routes = api_part2_router.routes[3:4]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part2_router = _srp_piece_router


_srp_piece_router = api_discovery_router
api_discovery_router = _srp_copy(api_discovery_router)
api_discovery_router.routes = api_discovery_router.routes[0:2]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_discovery_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_discovery_router = _srp_piece_router


_srp_piece_router = api_part2_router
api_part2_router = _srp_copy(api_part2_router)
api_part2_router.routes = api_part2_router.routes[4:5]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part2_router = _srp_piece_router


_srp_piece_router = api_discovery_router
api_discovery_router = _srp_copy(api_discovery_router)
api_discovery_router.routes = api_discovery_router.routes[2:3]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_discovery_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_discovery_router = _srp_piece_router


_srp_piece_router = api_part2_router
api_part2_router = _srp_copy(api_part2_router)
api_part2_router.routes = api_part2_router.routes[5:6]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part2_router = _srp_piece_router


_srp_piece_router = api_discovery_router
api_discovery_router = _srp_copy(api_discovery_router)
api_discovery_router.routes = api_discovery_router.routes[3:4]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_discovery_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_discovery_router = _srp_piece_router


_srp_piece_router = api_part2_router
api_part2_router = _srp_copy(api_part2_router)
api_part2_router.routes = api_part2_router.routes[6:9]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part2_router = _srp_piece_router


_srp_piece_router = api_discovery_router
api_discovery_router = _srp_copy(api_discovery_router)
api_discovery_router.routes = api_discovery_router.routes[4:5]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_discovery_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_discovery_router = _srp_piece_router


_srp_piece_router = api_part2_router
api_part2_router = _srp_copy(api_part2_router)
api_part2_router.routes = api_part2_router.routes[9:10]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part2_router = _srp_piece_router


_srp_piece_router = api_part3_router
api_part3_router = _srp_copy(api_part3_router)
api_part3_router.routes = api_part3_router.routes[5:7]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part3_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part3_router = _srp_piece_router


_srp_piece_router = api_discovery_router
api_discovery_router = _srp_copy(api_discovery_router)
api_discovery_router.routes = api_discovery_router.routes[5:6]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_discovery_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_discovery_router = _srp_piece_router


_srp_piece_router = api_part2_router
api_part2_router = _srp_copy(api_part2_router)
api_part2_router.routes = api_part2_router.routes[10:11]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part2_router = _srp_piece_router


_srp_piece_router = api_part3_router
api_part3_router = _srp_copy(api_part3_router)
api_part3_router.routes = api_part3_router.routes[7:8]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part3_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part3_router = _srp_piece_router


_srp_piece_router = api_part2_router
api_part2_router = _srp_copy(api_part2_router)
api_part2_router.routes = api_part2_router.routes[11:12]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_part2_router = _srp_piece_router


@router.post("/api/connectors")
async def api_connector_create(request: Request):
    """Create a connection NON-SECRET (mirrors `connector create`). can_tune_ai, BASE
    `api` scope (NOT the `docs` scope — the connector-family carve-out documented on the
    section header + _conn_role_block), customer-confined. Body: kind (required), name
    (required), config (object), customer_id, content_type_id. Any credential field is
    REFUSED (400) — creds ride Wave R3. An archived kind is refused with a clear message."""
    ident, org_id, blocked = _conn_create_gate(request, security.can_tune_ai)
    if blocked:
        return blocked
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    secret_blocked = _reject_connector_secret(body)
    if secret_blocked:
        return secret_blocked
    kind = str(body.get("kind") or "").strip()
    name = str(body.get("name") or "").strip()
    if not kind or not name:
        return JSONResponse({"ok": False, "error": "kind and name are required."}, status_code=400)
    # ffadmin Wave 5 -- DOCTRINE: a DISABLED kind is byte-identical to an UNKNOWN one here
    # -- the SAME 400 "unknown connector kind" body, so a disabled kind is indistinguishable
    # from a typo (non-enumerable), together-with the membership check and before archived.
    if kind not in {
        s.kind for s in connectors_registry.all_specs()
    } or not connectors_registry.kind_enabled(kind):
        return JSONResponse(
            {"ok": False, "error": f"unknown connector kind: {kind}"},
            status_code=400,  # noqa: RUF100, UP032
        )
    if connectors_registry.is_archived(kind):
        return JSONResponse(
            {
                "ok": False,
                "error": f"the {kind} connector is archived and no longer accepts new connections.",
            },
            status_code=400,
        )
    cfg = body.get("config")
    if cfg is not None and not isinstance(cfg, dict):
        return JSONResponse({"ok": False, "error": "config must be an object."}, status_code=400)
    blocked = _jira_schedule_admin_block(ident, kind, cfg=cfg)
    if blocked:
        return blocked
    imp_err = conn_store.gdocs_impersonation_error(
        kind,
        {},
        cfg if isinstance(cfg, dict) else {},
        caller_is_admin=(not security.auth_required()) or security.is_admin(ident.get("user")),
    )
    if imp_err:
        return JSONResponse({"ok": False, "error": imp_err}, status_code=403)
    # Review gate 2026-10-10: live-edit opt-ins (ServiceNow update_published) are admin only.
    scope_err = conn_store.scope_admin_error(
        kind,
        None,
        cfg if isinstance(cfg, dict) else {},
        caller_is_admin=(not security.auth_required()) or security.is_admin(ident.get("user")),
        actor=ident.get("actor") or "",
    )
    if scope_err:
        return JSONResponse({"ok": False, "error": scope_err}, status_code=403)
    cust_raw = body.get("customer_id")
    cust_raw = "" if cust_raw in (None, "") else str(cust_raw)
    ct_raw = body.get("content_type_id")
    ct_raw = "" if ct_raw in (None, "") else str(ct_raw)
    # store.create confines the new connection to the caller's own customer binding
    # (bound callers) and stamps the org, so a bound caller can never plant a
    # cross-customer/global connection.
    try:
        conn = await deps.offload_blocking(
            conn_store.create,
            kind,
            name,
            cfg if isinstance(cfg, dict) else {},
            {},
            actor=ident["actor"],
            org_id=org_id,
            customer_id=cust_raw,
            content_type_id=ct_raw,
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    except conn_store.ConnectorError as exc:  # e.g. an unreadable base URL (gate round 2)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    audit.log(
        actor=ident["actor"],
        action="connector.create",
        target="connection:%d" % conn.id,  # noqa: UP031
        detail=f"kind={conn.kind}",
        customer_id=conn.customer_id,
    )
    return JSONResponse({"ok": True, **_conn_row(conn)})


@router.post("/api/connectors/{cid}/update")
async def api_connector_update(cid: int, request: Request):
    """Update a connection's NON-SECRET fields (mirrors `connector update`, minus
    creds). can_tune_ai, BASE `api` scope (NOT the `docs` scope — the connector-family
    carve-out documented on the section header + _conn_role_block), ringed. Body: name,
    config (merged), customer_id, content_type_id. Any credential field is REFUSED (400)
    — creds ride Wave R3, so stored credentials are never touched here."""
    ident, blocked = _conn_cid_gate(request, cid, security.can_tune_ai)
    if blocked:
        return blocked
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    secret_blocked = _reject_connector_secret(body)
    if secret_blocked:
        return secret_blocked
    cfg = body.get("config")
    if cfg is not None and not isinstance(cfg, dict):
        return JSONResponse({"ok": False, "error": "config must be an object."}, status_code=400)
    conn = conn_store.get(cid)
    blocked = _jira_schedule_admin_block(ident, conn.kind, cfg=cfg)
    if blocked:
        return blocked
    # Security review 2026-10 (connectors-01 residual): the configured Airtable
    # base/table is the scope every per-call override is checked against, so a
    # non-admin may not move it while the connection holds a saved credential.
    # /api update never carries credentials, so there is no own-token escape here.
    retarget_err = conn_store._airtable_retarget_error(
        conn,
        cfg if isinstance(cfg, dict) else {},
        caller_is_admin=(not security.auth_required()) or security.is_admin(ident.get("user")),
        new_secret_supplied=False,
        actor=ident.get("actor") or "",
    ) or conn_store.scope_admin_error(
        conn.kind,
        conn,
        cfg if isinstance(cfg, dict) else {},
        caller_is_admin=(not security.auth_required()) or security.is_admin(ident.get("user")),
        actor=ident.get("actor") or "",
    )
    if retarget_err:
        return JSONResponse({"ok": False, "error": retarget_err}, status_code=403)
    imp_err = conn_store.gdocs_impersonation_error(
        conn.kind,
        conn.config,
        cfg if isinstance(cfg, dict) else {},
        caller_is_admin=(not security.auth_required()) or security.is_admin(ident.get("user")),
    )
    if imp_err:
        return JSONResponse({"ok": False, "error": imp_err}, status_code=403)
    upd = {}
    if "customer_id" in body:
        upd["customer_id"] = (
            "" if body.get("customer_id") in (None, "") else str(body.get("customer_id"))
        )
    if "content_type_id" in body:
        upd["content_type_id"] = (
            "" if body.get("content_type_id") in (None, "") else str(body.get("content_type_id"))
        )
    # No new credentials; the store clears old ones if the destination host changes.
    try:
        await deps.offload_blocking(
            conn_store.update,
            cid,
            str(body.get("name") or conn.name),
            cfg if isinstance(cfg, dict) else {},
            None,
            bound_customer_id=deps._ident_customer_id(ident),
            actor=ident["actor"],
            # connected projects: a language added here is added to the project it runs for,
            # which needs a manager or higher (decision 8)
            can_add_languages=(not security.auth_required())
            or security.can_manage(ident.get("user")),
            **upd,
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    except conn_store.ConnectorError as exc:  # an unreadable base URL, a language refusal
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=getattr(exc, "status", 400)
        )
    return JSONResponse({"ok": True, **_conn_row(conn_store.get(cid))})


@router.post("/api/connectors/{cid}/test")
def api_connector_test(cid: int, request: Request):
    """Test a connection's stored credentials against the external system (mirrors
    `connector test`). can_tune_ai, ringed (ring before role)."""
    ident, blocked = _conn_cid_gate(request, cid, security.can_tune_ai)
    if blocked:
        return blocked
    ok, msg = conn_store.test_connection(cid, actor=ident["actor"])
    return JSONResponse({"ok": bool(ok), "message": msg}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/browse")
def api_connector_browse(cid: int, request: Request):
    """List a connection's containers (Airtable bases / Jira projects / Confluence
    spaces) (mirrors `connector browse`). can_tune_ai, ringed (ring before role)."""
    _ident, blocked = _conn_cid_gate(request, cid, security.can_tune_ai)
    if blocked:
        return blocked
    try:
        containers = conn_store.containers(cid)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "containers": containers})


@router.post("/api/connectors/{cid}/schedule")
async def api_connector_schedule(cid: int, request: Request):
    """Set or clear the automated-sync schedule (mirrors `connector schedule`).
    can_tune_ai, ringed (ring before role). Body: every_minutes (N) OR off:true."""
    from app.kernel import scheduler

    ident, blocked = _conn_cid_gate(request, cid, security.can_tune_ai)
    if blocked:
        return blocked
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if body.get("off"):
        minutes = 0
    else:
        try:
            minutes = max(0, int(body.get("every_minutes")))
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "pass every_minutes (an integer) or off:true."},
                status_code=400,
            )
    blocked = _jira_schedule_admin_block(ident, conn_store.get(cid).kind, minutes=minutes)
    if blocked:
        return blocked
    scheduler.set_schedule(cid, minutes, actor=ident["actor"])
    effective = scheduler.interval_minutes(conn_store.get(cid))
    return JSONResponse({"ok": True, "connection_id": cid, "every_minutes": effective})


@router.post("/api/connectors/{cid}/delete")
def api_connector_delete(cid: int, request: Request):
    """Delete a connection (mirrors `connector delete`). DESTRUCTIVE: is_admin, ringed
    (ring before role, so a foreign cid is 404 for any caller, not a 403 oracle)."""
    ident, blocked = _conn_cid_gate(request, cid, security.is_admin)
    if blocked:
        return blocked
    err = conn_store.delete(cid)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    audit.log(actor=ident["actor"], action="connector.delete", target="connection:%d" % cid)  # noqa: UP031
    return JSONResponse({"ok": True, "id": cid, "deleted": True})


@router.post("/api/connectors/{cid}/prune-config")
async def api_connector_prune_config(cid: int, request: Request):
    """Prune stale content-hash entries a dropped target locale left in the connection's
    config (mirrors `connector prune-config`; residual sweep / adversarial-review H2/M3).
    is_admin, ringed (ring before role, so a foreign cid is 404 for any caller). DRY-RUN
    BY DEFAULT: returns exactly what WOULD be removed, per map; send {"apply": true} to
    execute (audited). Restricted to an allow-list of pure content-hash maps; never
    touches child-id / written / field-keyed maps."""
    ident, blocked = _conn_cid_gate(request, cid, security.is_admin)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    apply = bool(payload.get("apply"))
    try:
        rep = await deps.offload_blocking(
            conn_store.prune_orphaned_config, cid, apply=apply, actor=ident["actor"]
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": True, **rep})
