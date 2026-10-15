"""Key-based projects — the WEB grid + the `/api/keys` group and the Delta API.

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports _logger, _key_project, _api_project, _api_key_row,
keys_grid, keys_add, keys_import, keys_export, keys_empties, keys_deliver_approve,
keys_deliver_revoke, keys_translate, _grid_key, keys_set_autounverify, api_keys_list, key_set_value,
key_set_base, key_set_meta, key_set_plural, key_delete_confirm, key_delete, key_restore,
key_image_add, key_image_delete, key_image_primary, api_keys_upsert, api_keys_import,
api_keys_export, api_keys_empties, api_keys_deliver_approve, api_keys_deliver_revoke,
api_keys_translate, api_changes from app.web.keys_hub, app.web.keys_hub2.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports api_router, _logger, KEY_STATUSES, _KEY_PAGE_MAX,
_NOT_FOUND, _FORBIDDEN, _flag_off_block, _api_flag_block, _image_flag_off_block,
_image_api_flag_block, _write_gate, _can_write, _scope, _actor, _EXPORT_NOTICE_COOKIE,
_EXPORT_NOTICE_CAP, _NOTICE_OVERFLOW_ROOM, _notice_path, _set_export_notice, _take_export_notice,
_RUNS_COOKIE, _RUNS_COOKIE_MAX, _MAX_RUN_ID, _set_run_cookie, _take_run_cookie, _live_runs,
_queued_run_pills, _back, _require_keys_scope, _api_gate, _api_write_block, _api_scope, _api_error,
_limit_refusal, _grid_filters, _img_customer_id, _api_workflow_visible, _json_body from
app.web.keys_common.

SPEC-key-based-projects sections 5 and 6 (Batch 4, Wave B). Wave A built the engine
(`app/services/keys.py`, `app/services/key_files.py`); this module is the surface and
holds no logic of its own beyond translating HTTP to those seams and back.

THREE THINGS THIS FILE IS RESPONSIBLE FOR, none of which the service can do for it:

1. **The kill switch on every route.** `key_projects_enabled` off -> 404 (never 403:
   a switched-off feature must not be enumerable), the create-form type selector
   disappears, and the CLI refuses. The image-localization / Evaluations-Lab recipe.

2. **Identity, threaded into the service guards.** Every `keys` / `key_files` seam
   takes `org_id=` / `customer_id=` (Wave A review A11) and answers a cross-scope
   project with the same non-enumerable refusal a missing id gets. These routes derive
   both from `app/web/deps.py` (web session) or from the inbound token's identity
   (`/api`) and PASS THEM DOWN — a route that resolved the project itself and forgot
   would be exactly the isolation defect this project's reviews find most often.

3. **The ACTOR, explicitly.** `keys.actor_scope` is thread-local and inherited across
   `Thread.start`, but NOT across a thread POOL — and FastAPI runs a sync route handler
   on anyio's worker-thread pool, so `current_actor()` on this thread is "" at best and
   another user's name at worst (re-review R5). Every write seam here therefore passes
   `actor=` taken from the request identity, and the workflow launcher WRAPS the launch
   in `actor_scope` so the raw run thread it starts inherits a correct one.

Documented in: docs/architecture/string-keys.md
"""

import logging
from copy import copy as _srp_copy
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session

from app import config
from app.db import get_session
from app.kernel import (
    access,
    audit,
    export,
    key_files,
    key_images,
    security,
    tm_delivery,
)
from app.kernel import (
    keys as keys_svc,
)
from app.models import Project, SegStatus, StringKey
from app.web import deps
from app.web.deps import (
    _actor_email,
    _error_page,
    _ip,
    _my_customer_id,
    _my_org_id,
    _visibility_block,
    templates,
)
from app.web.keys_common import (
    _EXPORT_NOTICE_CAP,
    _EXPORT_NOTICE_COOKIE,
    _FORBIDDEN,
    _KEY_PAGE_MAX,
    _MAX_RUN_ID,
    _NOT_FOUND,
    _NOTICE_OVERFLOW_ROOM,
    _RUNS_COOKIE,
    _RUNS_COOKIE_MAX,
    KEY_STATUSES,
    _actor,
    _api_error,
    _api_flag_block,
    _api_gate,
    _api_scope,
    _api_workflow_visible,
    _api_write_block,
    _back,
    _can_write,
    _flag_off_block,
    _grid_filters,
    _image_api_flag_block,
    _image_flag_off_block,
    _img_customer_id,
    _json_body,
    _limit_refusal,
    _live_runs,
    _notice_path,
    _queued_run_pills,
    _require_keys_scope,
    _scope,
    _set_export_notice,
    _set_run_cookie,
    _take_export_notice,
    _take_run_cookie,
    _write_gate,
    api_router,
)
from app.web.keys_hub import (
    _api_key_row,
    _api_project,
    _grid_key,
    _key_project,
    api_keys_list,
    keys_add,
    keys_deliver_approve,
    keys_deliver_revoke,
    keys_empties,
    keys_export,
    keys_grid,
    keys_import,
    keys_set_autounverify,
    keys_translate,
)
from app.web.keys_hub import router as keys_hub_router
from app.web.keys_hub2 import (
    api_changes,
    api_keys_deliver_approve,
    api_keys_deliver_revoke,
    api_keys_empties,
    api_keys_export,
    api_keys_import,
    api_keys_translate,
    api_keys_upsert,
    key_delete,
    key_delete_confirm,
    key_image_add,
    key_image_delete,
    key_image_primary,
    key_restore,
    key_set_base,
    key_set_meta,
    key_set_plural,
    key_set_value,
)
from app.web.keys_hub2 import router as keys_hub2_router
from app.web.streaming import StreamingResponse

__all__ = [
    "KEY_STATUSES",
    "_EXPORT_NOTICE_CAP",
    "_EXPORT_NOTICE_COOKIE",
    "_FORBIDDEN",
    "_KEY_PAGE_MAX",
    "_MAX_RUN_ID",
    "_NOTICE_OVERFLOW_ROOM",
    "_NOT_FOUND",
    "_RUNS_COOKIE",
    "_RUNS_COOKIE_MAX",
    "File",
    "Form",
    "Project",
    "RedirectResponse",
    "Response",
    "SegStatus",
    "StreamingResponse",
    "StringKey",
    "UploadFile",
    "_actor",
    "_actor_email",
    "_api_error",
    "_api_flag_block",
    "_api_gate",
    "_api_key_row",
    "_api_project",
    "_api_scope",
    "_api_workflow_visible",
    "_api_write_block",
    "_back",
    "_can_write",
    "_flag_off_block",
    "_grid_filters",
    "_grid_key",
    "_image_api_flag_block",
    "_image_flag_off_block",
    "_img_customer_id",
    "_json_body",
    "_key_project",
    "_limit_refusal",
    "_live_runs",
    "_my_customer_id",
    "_my_org_id",
    "_notice_path",
    "_queued_run_pills",
    "_require_keys_scope",
    "_scope",
    "_set_export_notice",
    "_set_run_cookie",
    "_take_export_notice",
    "_take_run_cookie",
    "_visibility_block",
    "_write_gate",
    "access",
    "api_changes",
    "api_keys_deliver_approve",
    "api_keys_deliver_revoke",
    "api_keys_empties",
    "api_keys_export",
    "api_keys_import",
    "api_keys_list",
    "api_keys_translate",
    "api_keys_upsert",
    "api_router",
    "datetime",
    "export",
    "key_delete",
    "key_delete_confirm",
    "key_files",
    "key_image_add",
    "key_image_delete",
    "key_image_primary",
    "key_restore",
    "key_set_base",
    "key_set_meta",
    "key_set_plural",
    "key_set_value",
    "keys_add",
    "keys_deliver_approve",
    "keys_deliver_revoke",
    "keys_empties",
    "keys_export",
    "keys_grid",
    "keys_import",
    "keys_set_autounverify",
    "keys_translate",
    "quote",
    "security",
    "templates",
    "timezone",
    "tm_delivery",
    "unquote",
]


router = APIRouter()


_logger = logging.getLogger("loc_tms")


_srp_piece_router = keys_hub_router
keys_hub_router = _srp_copy(keys_hub_router)
keys_hub_router.routes = keys_hub_router.routes[0:8]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(keys_hub_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
keys_hub_router = _srp_piece_router


_srp_piece_router = keys_hub2_router
keys_hub2_router = _srp_copy(keys_hub2_router)
keys_hub2_router.routes = keys_hub2_router.routes[0:7]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(keys_hub2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
keys_hub2_router = _srp_piece_router


_srp_piece_router = keys_hub_router
keys_hub_router = _srp_copy(keys_hub_router)
keys_hub_router.routes = keys_hub_router.routes[8:9]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(keys_hub_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
keys_hub_router = _srp_piece_router


_srp_piece_router = keys_hub2_router
keys_hub2_router = _srp_copy(keys_hub2_router)
keys_hub2_router.routes = keys_hub2_router.routes[7:10]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(keys_hub2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
keys_hub2_router = _srp_piece_router


@router.get("/projects/{pid}/keys/{key_id}/images/{image_id}/raw")
def key_image_raw(
    pid: int,
    key_id: int,
    image_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Serve one image's stored PNG bytes inline for review. A not-ready stub OR an absent
    blob is a non-enumerable 404 (never a 500), mirroring the send-path swallow (LOW-1)."""
    blocked = _image_flag_off_block(request)
    if blocked:
        return blocked
    _project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    img = key_images.get_image(session, key, image_id)
    if img is None:
        return _error_page(request, 404, "Not found.")
    data = key_images.read_bytes(img)
    if data is None:
        return _error_page(request, 404, "Not found.")
    # #489: every human pull of customer pixels is audited, same row as the /api twin.
    audit.log(
        actor=_actor(request),
        action="key_image.raw",
        target="key=%d image=%d" % (key.id, image_id),  # noqa: UP031
        customer_id=audit.customer_for_project(pid),
        ip=_ip(request),
    )
    from app.web.images import _serve_image

    return _serve_image(data, img.fmt)


@api_router.patch("/api/keys/{key_id}")
async def api_key_patch(key_id: int, request: Request, session: Session = Depends(get_session)):
    """Update metadata, identity and/or the base value. Body: any of description,
    context, tags, char_limit, source_file, custom, is_hidden, is_archived,
    plural_arg_name, name, namespace, `is_plural` (+ `force`) and `base` (a string,
    or {category: text}).

    ALL-OR-NOTHING FOR EVERY DETERMINISTIC REFUSAL (review B6). Each of the five
    service seams an edit can reach COMMITS, so applying them in sequence used to
    answer 400 with part of the edit already durable and an `updated` entry already on
    the change feed — a consumer that reads a refusal as "nothing happened" then drifts
    from the server for good. `keys.apply_key_edit` validates the whole edit before the
    first write; this route is now the thin shell it always claimed to be, and `keys
    edit` calls the SAME seam."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    key, _project, denied = _api_key_row(session, ident, key_id)
    if denied:
        return denied
    body = await _json_body(request)
    if not isinstance(body, dict):
        return _api_error("body must be a JSON object")
    fields = {f: body[f] for f in keys_svc.EDIT_FIELDS if f in body}
    try:
        keys_svc.apply_key_edit(session, key, actor=ident["actor"], **fields)
    except keys_svc.KeyServiceError as exc:
        session.rollback()
        return _api_error(exc)
    session.refresh(key)
    return JSONResponse({"ok": True, "key": keys_svc.key_view(session, key)})


@api_router.post("/api/keys/{key_id}/restore")
def api_key_restore(key_id: int, request: Request, session: Session = Depends(get_session)):
    """Undo a soft delete — the /api twin of the grid's Restore button and of
    `keys restore` (the parity law; restore was WEB-ONLY, so a headless consumer that
    ran a wrong `--cleanup` import had no way back at all)."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    key, _project, denied = _api_key_row(session, ident, key_id, include_deleted=True)
    if denied:
        return denied
    try:
        keys_svc.restore_key(session, key, actor=ident["actor"], ip=_ip(request))
    except keys_svc.KeyServiceError as exc:
        session.rollback()
        return _api_error(exc)
    return JSONResponse(
        {"ok": True, "id": key_id, "deleted": False, "key": keys_svc.key_view(session, key)}
    )


@api_router.delete("/api/keys/{key_id}")
def api_key_delete(key_id: int, request: Request, session: Session = Depends(get_session)):
    """SOFT delete — the tombstone the delta feed hands consumers."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    key, _project, denied = _api_key_row(session, ident, key_id)
    if denied:
        return denied
    keys_svc.delete_key(session, key, actor=ident["actor"], ip=_ip(request))
    return JSONResponse({"ok": True, "id": key_id, "deleted": True})


@api_router.get("/api/keys/{key_id}/values")
def api_key_values(key_id: int, request: Request, session: Session = Depends(get_session)):
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    key, _project, denied = _api_key_row(session, ident, key_id)
    if denied:
        return denied
    return JSONResponse({"ok": True, "key": keys_svc.key_view(session, key)})


@api_router.put("/api/keys/{key_id}/values/{locale}")
async def api_key_set_values(
    key_id: int, locale: str, request: Request, session: Session = Depends(get_session)
):
    """Set one locale's value(s). Body: {"text": "..."} for a simple key, or
    {"forms": {"one": "...", "other": "..."}} for a plural one."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    key, _project, denied = _api_key_row(session, ident, key_id)
    if denied:
        return denied
    body = await _json_body(request)
    if not isinstance(body, dict):
        return _api_error("body must be a JSON object")
    forms = body.get("forms")
    if forms is None and body.get("text") is None:
        return _api_error("pass text (simple key) or forms (plural key)")
    # F4 (adversarial review): a machine (/api) write over a human-approved/edited value --
    # or a hard-locked one -- is REFUSED by the seam and leaves the row byte-untouched. Collect
    # any such refusal so this route answers 409 with a named reason instead of the silent
    # ok:true it used to return for a write that never landed.
    refusals: list = []
    refused_cats: list = []  # F4-plural LOW: WHICH forms were refused (so the caller knows)
    try:
        if forms is not None:
            if not isinstance(forms, dict):
                return _api_error("forms must be an object of {category: text}")
            for cat, text in forms.items():
                _sink: list = []
                keys_svc.set_value(
                    session,
                    key,
                    locale,
                    str(text or ""),
                    origin=keys_svc.ORIGIN_API,
                    plural_category=str(cat),
                    actor=ident["actor"],
                    refusal_sink=_sink,
                )
                if _sink:
                    refusals.extend(_sink)
                    refused_cats.append(str(cat))
        else:
            keys_svc.set_value(
                session,
                key,
                locale,
                str(body["text"] or ""),
                origin=keys_svc.ORIGIN_API,
                actor=ident["actor"],
                refusal_sink=refusals,
            )
    except keys_svc.KeyServiceError as exc:
        session.rollback()
        if getattr(exc, "status", None) == 409:  # #617: a run waits on human review
            return JSONResponse({"ok": False, "code": exc.code, "error": str(exc)}, status_code=409)
        return _api_error(exc)
    if refusals:
        reason = "status_protected" if "status_protected" in refusals else "locked"
        msg = (
            "this value was approved or edited by a person and cannot be overwritten by "
            "an API write -- clear it first"
            if reason == "status_protected"
            else "this value is hard-locked by a TM match and cannot be overwritten -- unlock "
            "it first"
        )
        # F4-plural LOW: on a plural key some forms may have COMMITTED while others were refused;
        # name the refused categories so the caller can tell which (the committed ones stand).
        resp = {"ok": False, "reason": reason, "error": msg, "key": keys_svc.key_view(session, key)}
        if refused_cats:
            resp["refused"] = refused_cats
        return JSONResponse(resp, status_code=409)
    return JSONResponse({"ok": True, "key": keys_svc.key_view(session, key)})


# ============================================================================
# /api — per-string in-context images (Batch 14 T1; the _api_key_row triple + MCP-covered)
# ============================================================================
@api_router.get("/api/keys/{key_id}/images")
def api_key_images_list(
    key_id: int, request: Request, pending: str = "", session: Session = Depends(get_session)
):
    """List a key's images (METADATA only, never bytes). The read the `list_key_images`
    MCP tool + the /utcp manual bind. `pending=1` also surfaces a `ready=False` stub (a
    failed prior upload) so a caller can DELETE it -- otherwise a stub is an unreachable,
    unremovable slot-eater (M-2)."""
    blocked = _image_api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    key, _project, denied = _api_key_row(session, ident, key_id)
    if denied:
        return denied
    imgs = key_images.list_images(session, key, ready_only=(str(pending) not in ("1", "true")))
    return JSONResponse(
        {"ok": True, "key_id": key.id, "images": [key_images.view(i) for i in imgs]}
    )


@api_router.post("/api/keys/{key_id}/images")
async def api_key_image_add(key_id: int, request: Request, session: Session = Depends(get_session)):
    """Attach one image: EITHER multipart (`file` part, web/file) OR a JSON body
    {"image_base64","mime","caption"} (MCP/programmatic).

    D2-OOM: the FULL gate chain runs FIRST (a foreign/invalid key -> 404 BEFORE the body is
    read). The JSON variant does NOT ride read_capped_upload (multipart-only) and there is
    no JSON body-cap seam, so it uses the WEBHOOK stream-cap pattern: Content-Length
    fast-reject + request.stream() abort at KEY_IMAGE_MAX_RAW_BODY_BYTES BEFORE
    json.loads/b64decode. `request.json()` and a Pydantic body model are FORBIDDEN here
    (both buffer the whole body during validation, before the gate)."""
    blocked = _image_api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    key, project, denied = _api_key_row(session, ident, key_id)
    if denied:
        return denied
    ctype = (request.headers.get("content-type") or "").lower()
    caption = ""
    if ctype.startswith("multipart/"):
        form = await request.form()
        file = form.get("file")
        caption = str(form.get("caption") or "")
        if file is None or isinstance(file, str):
            return _api_error("multipart upload needs a `file` part")
        data = await deps.read_capped_upload(file, config.KEY_IMAGE_MAX_BYTES)
        if data is None:
            return _api_error("image exceeds the upload cap", status=413)
    else:
        import base64
        import binascii
        import json as _json

        cap = config.KEY_IMAGE_MAX_RAW_BODY_BYTES
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > cap:
                    return _api_error("image payload too large", status=413)
            except ValueError:
                pass  # unparseable -> fall through to the bounded stream read
        chunks: list = []
        total = 0
        async for piece in request.stream():
            total += len(piece)
            if total > cap:
                return _api_error("image payload too large", status=413)
            chunks.append(piece)
        raw = b"".join(chunks)
        try:
            body = _json.loads(raw or b"{}")
        except Exception:
            return _api_error("body must be a JSON object or a multipart upload")
        if not isinstance(body, dict):
            return _api_error("body must be a JSON object")
        b64 = body.get("image_base64")
        if not b64:
            return _api_error("pass image_base64 (base64 of a PNG or JPEG image)")
        try:
            data = base64.b64decode(str(b64), validate=True)
        except (ValueError, binascii.Error):
            return _api_error("image_base64 is not valid base64")
        if len(data) > config.KEY_IMAGE_MAX_BYTES:
            return _api_error("image exceeds the upload cap", status=413)
        caption = str(body.get("caption") or "")
    try:
        img = key_images.attach_image(
            session, key, data, caption=caption, uploaded_by=ident["actor"]
        )
    except key_images.KeyImageError as exc:
        session.rollback()
        return _api_error(exc)
    audit.log(
        actor=ident["actor"],
        action="key_image.attach",
        target="key=%d image=%d" % (key.id, img.id),  # noqa: UP031
        customer_id=getattr(project, "customer_id", None),
        ip=_ip(request),
    )
    return JSONResponse({"ok": True, "image": key_images.view(img)})


@api_router.delete("/api/keys/{key_id}/images/{image_id}")
def api_key_image_delete(
    key_id: int, image_id: int, request: Request, session: Session = Depends(get_session)
):
    """SOFT-delete one image (reversible; the blob is reaped by project delete / the
    retention backstop). The DELETE the `manage_key_image` MCP tool binds (write-tier)."""
    blocked = _image_api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    key, project, denied = _api_key_row(session, ident, key_id)
    if denied:
        return denied
    # include_pending: a DELETE may target a ready=False stub (a failed prior upload), M-2.
    img = key_images.get_image(session, key, image_id, include_pending=True)
    if img is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    key_images.delete_image(session, key, img, actor=ident["actor"])
    audit.log(
        actor=ident["actor"],
        action="key_image.delete",
        target="key=%d image=%d" % (key.id, image_id),  # noqa: UP031
        customer_id=getattr(project, "customer_id", None),
        ip=_ip(request),
    )
    return JSONResponse({"ok": True, "id": image_id, "deleted": True})


@api_router.post("/api/keys/{key_id}/images/{image_id}/primary")
def api_key_image_primary(
    key_id: int, image_id: int, request: Request, session: Session = Depends(get_session)
):
    """Make one image the key's primary (the one sent by default, S3)."""
    blocked = _image_api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    key, project, denied = _api_key_row(session, ident, key_id)
    if denied:
        return denied
    img = key_images.get_image(session, key, image_id)
    if img is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    key_images.set_primary(session, key, img, actor=ident["actor"])
    audit.log(
        actor=ident["actor"],
        action="key_image.set_primary",
        target="key=%d image=%d" % (key.id, image_id),  # noqa: UP031
        customer_id=getattr(project, "customer_id", None),
        ip=_ip(request),
    )
    return JSONResponse({"ok": True, "image": key_images.view(img)})


@api_router.get("/api/keys/{key_id}/images/{image_id}/raw")
def api_key_image_raw(
    key_id: int, image_id: int, request: Request, session: Session = Depends(get_session)
):
    """Serve one image's stored PNG bytes (audit-logged). NOT bound to any MCP tool -- an
    agent has no use for pulling customer pixels back (exfiltration surface); this stays
    EXEMPT (authoring-surface). A stub/absent blob is a non-enumerable 404, never a 500."""
    blocked = _image_api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    key, project, denied = _api_key_row(session, ident, key_id)
    if denied:
        return denied
    img = key_images.get_image(session, key, image_id)
    if img is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    data = key_images.read_bytes(img)
    if data is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(
        actor=ident["actor"],
        action="key_image.raw",
        target="key=%d image=%d" % (key.id, image_id),  # noqa: UP031
        customer_id=getattr(project, "customer_id", None),
        ip=_ip(request),
    )
    from app.web.images import _serve_image

    return _serve_image(data, img.fmt)
