"""keys: second pass part 2 (layered pure move)

Documented in: docs/architecture/string-keys.md
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session

from app import config
from app.db import get_session
from app.kernel import (
    audit,
    connected_projects,
    export,
    key_files,
    key_images,
    security,
    tm_delivery,
)
from app.kernel import (
    keys as keys_svc,
)
from app.web import deps
from app.web.deps import (
    _error_page,
    _ip,
    templates,
)
from app.web.keys_common import (
    _NOT_FOUND,
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
    _image_flag_off_block,
    _img_customer_id,
    _json_body,
    _limit_refusal,
    _write_gate,
    api_router,
)
from app.web.keys_hub import _api_project, _api_sees_hidden_keys, _grid_key
from app.web.streaming import StreamingResponse

_logger = logging.getLogger("loc_tms")


router = APIRouter()


@router.post("/projects/{pid}/keys/{key_id}/value")
def key_set_value(
    pid: int,
    key_id: int,
    request: Request,
    locale: str = Form(...),
    text: str = Form(""),
    plural_category: str = Form(""),
    session: Session = Depends(get_session),
):
    """Inline grid edit of ONE value (one plural form). can_write-gated; a viewer
    never sees the input, and would be refused here anyway."""
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    _project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    try:
        keys_svc.set_value(
            session,
            key,
            locale,
            text,
            origin=keys_svc.ORIGIN_HUMAN,
            plural_category=plural_category or None,
            actor=_actor(request),
        )
    except keys_svc.KeyServiceError as exc:
        return _back(pid, request, err=str(exc))
    return _back(pid, request, ok="Saved.")


@router.post("/projects/{pid}/keys/{key_id}/base")
def key_set_base(
    pid: int,
    key_id: int,
    request: Request,
    text: str = Form(""),
    plural_category: str = Form(""),
    session: Session = Depends(get_session),
):
    """Edit the BASE (source-locale) value. Auto-unverify applies: every locale that
    already held a translation flips to needs-review."""
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    _project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    base = keys_svc.base_values(session, key)
    value = {**base, (plural_category or "other"): text} if key.is_plural else text
    try:
        # `fill_missing=False`: this map is COMPLETE and a form it names as EMPTY is
        # empty (fix-round F6). Merging the stored base map hands `_source_for` `""`
        # for every form `--base` / the add form deliberately left blank, and `""` is
        # falsy, so the fallback re-fanned the singular `other` string across every
        # plural form on the FIRST edit -- the K5 defect, back, plus auto-unverify on
        # every translation that had one. Same call, same reason, on the CLI's
        # `keys set` (see `cmd_keys._keys_set`).
        flagged = keys_svc.set_base_value(
            session, key, value, actor=_actor(request), fill_missing=False
        )
    except keys_svc.KeyServiceError as exc:
        return _back(pid, request, err=str(exc))
    note = "Saved. %d translation(s) flagged for review." % len(flagged) if flagged else "Saved."  # noqa: UP031
    return _back(pid, request, ok=note)


@router.post("/projects/{pid}/keys/{key_id}/meta")
def key_set_meta(
    pid: int,
    key_id: int,
    request: Request,
    description: str = Form(""),
    context: str = Form(""),
    tags: str = Form(""),
    char_limit: str = Form(""),
    is_hidden: str = Form(""),
    is_archived: str = Form(""),
    session: Session = Depends(get_session),
):
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    _project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    try:
        limit = int(char_limit) if str(char_limit).strip() else None
    except (TypeError, ValueError):
        return _back(pid, request, err="char_limit must be a whole number.")
    try:
        keys_svc.update_key(
            session,
            key,
            description=description,
            context=context,
            char_limit=limit,
            tags=[t.strip() for t in tags.split(",") if t.strip()] or None,
            is_hidden=str(is_hidden).strip() in ("1", "on", "true"),
            is_archived=str(is_archived).strip() in ("1", "on", "true"),
            actor=_actor(request),
        )
    except keys_svc.KeyServiceError as exc:
        return _back(pid, request, err=str(exc))
    return _back(pid, request, ok=f"Updated {key.name}.")


@router.post("/projects/{pid}/keys/{key_id}/plural")
def key_set_plural(
    pid: int,
    key_id: int,
    request: Request,
    is_plural: str = Form(""),
    plural_arg_name: str = Form(""),
    force: str = Form(""),
    session: Session = Depends(get_session),
):
    """The plural drawer's promote/demote. A DEMOTION that would destroy a
    translated non-`other` form is refused with the service's own message unless the
    operator ticked "discard them" — the refusal is the feature."""
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    _project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    try:
        keys_svc.set_plural(
            session,
            key,
            str(is_plural).strip() in ("1", "on", "true"),
            plural_arg_name=plural_arg_name.strip() or None,
            force=str(force).strip() in ("1", "on", "true"),
            actor=_actor(request),
        )
    except keys_svc.KeyServiceError as exc:
        return _back(pid, request, err=str(exc))
    return _back(pid, request, ok=f"Updated plural form of {key.name}.")


@router.get("/projects/{pid}/keys/{key_id}/delete")
def key_delete_confirm(
    pid: int, key_id: int, request: Request, session: Session = Depends(get_session)
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    return templates.TemplateResponse(
        request,
        "project_key_delete.html",
        {
            "request": request,
            "project": project,
            "key": keys_svc.key_view(session, key),
            "can_write": _can_write(request),
        },
    )


@router.post("/projects/{pid}/keys/{key_id}/delete")
def key_delete(
    pid: int,
    key_id: int,
    request: Request,
    confirm_name: str = Form(""),
    session: Session = Depends(get_session),
):
    """SOFT delete + tombstone. Typed-name confirmation, the same gate the project
    and template deletes use."""
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    _project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    if confirm_name.strip() != key.name:
        return _back(pid, request, err="Type the key's exact name to delete it.")
    name = key.name
    keys_svc.delete_key(session, key, actor=_actor(request), ip=_ip(request))
    return _back(pid, request, ok=f"Deleted {name} (restorable).")


@router.post("/projects/{pid}/keys/{key_id}/restore")
def key_restore(pid: int, key_id: int, request: Request, session: Session = Depends(get_session)):
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    _project, key, blocked = _grid_key(request, session, pid, key_id, include_deleted=True)
    if blocked:
        return blocked
    try:
        keys_svc.restore_key(session, key, actor=_actor(request), ip=_ip(request))
    except keys_svc.KeyServiceError as exc:
        return _back(pid, request, err=str(exc))
    return _back(pid, request, ok=f"Restored {key.name}.")


@router.post("/projects/{pid}/keys/{key_id}/images")
async def key_image_add(
    pid: int,
    key_id: int,
    request: Request,
    file: UploadFile = File(...),
    caption: str = Form(""),
    session: Session = Depends(get_session),
):
    """Attach one in-context image to a key (multipart upload). can_write-gated; confined
    by the `_grid_key` triple."""
    blocked = _image_flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    data = await deps.read_capped_upload(file, config.KEY_IMAGE_MAX_BYTES)
    if data is None:
        return _back(pid, request, err="That image is too large.")
    try:
        img = key_images.attach_image(
            session, key, data, caption=caption, uploaded_by=_actor(request)
        )
    except key_images.KeyImageError as exc:
        session.rollback()
        return _back(pid, request, err=str(exc))
    audit.log(
        actor=_actor(request),
        action="key_image.attach",
        target="key=%d image=%d" % (key.id, img.id),  # noqa: UP031
        customer_id=_img_customer_id(project),
        ip=_ip(request),
    )
    return _back(pid, request, ok="Image attached.")


@router.post("/projects/{pid}/keys/{key_id}/images/{image_id}/delete")
def key_image_delete(
    pid: int,
    key_id: int,
    image_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Soft-delete one image; the primary is promoted if this was it. can_write-gated."""
    blocked = _image_flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    # include_pending: a DELETE may target a ready=False stub (a failed prior upload), M-2.
    img = key_images.get_image(session, key, image_id, include_pending=True)
    if img is None:
        return _error_page(request, 404, "Not found.")
    key_images.delete_image(session, key, img, actor=_actor(request))
    audit.log(
        actor=_actor(request),
        action="key_image.delete",
        target="key=%d image=%d" % (key.id, image_id),  # noqa: UP031
        customer_id=_img_customer_id(project),
        ip=_ip(request),
    )
    return _back(pid, request, ok="Image deleted.")


@router.post("/projects/{pid}/keys/{key_id}/images/{image_id}/primary")
def key_image_primary(
    pid: int,
    key_id: int,
    image_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Make one image the key's primary (the one sent by default). can_write-gated."""
    blocked = _image_flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    project, key, blocked = _grid_key(request, session, pid, key_id)
    if blocked:
        return blocked
    img = key_images.get_image(session, key, image_id)
    if img is None:
        return _error_page(request, 404, "Not found.")
    key_images.set_primary(session, key, img, actor=_actor(request))
    audit.log(
        actor=_actor(request),
        action="key_image.set_primary",
        target="key=%d image=%d" % (key.id, image_id),  # noqa: UP031
        customer_id=_img_customer_id(project),
        ip=_ip(request),
    )
    return _back(pid, request, ok="Primary image updated.")


@api_router.post("/api/projects/{pid}/keys")
async def api_keys_upsert(pid: int, request: Request, session: Session = Depends(get_session)):
    """BULK create/upsert. PARTIAL SUCCESS by contract (Lokalise's `errors[]`): one
    bad key is reported and the rest of the batch still lands, because a CI job
    pushing 4,000 keys must not lose 3,999 of them to one typo."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    project, denied = _api_project(session, ident, pid)
    if denied:
        return denied
    if connected_projects.is_connected(project):
        return _api_error(connected_projects.UPLOAD_REFUSED)
    body = await _json_body(request)
    if body is None:
        return _api_error("body must be JSON")
    rows = body.get("keys") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return _api_error('expected a JSON array of keys (or {"keys": [...]})')
    actor = ident["actor"]
    created, updated, errors = [], [], []
    see_hidden = _api_sees_hidden_keys(ident)
    for i, rec in enumerate(rows):
        if not isinstance(rec, dict) or not str(rec.get("name") or "").strip():
            errors.append(
                {
                    "index": i,
                    "name": (rec or {}).get("name") if isinstance(rec, dict) else None,
                    "error": "each entry needs a name",
                }
            )
            continue
        name = str(rec["name"]).strip()
        ns, ctx = str(rec.get("namespace") or ""), str(rec.get("context") or "")
        try:
            existing = keys_svc.get_key(session, project.id, name, ns, ctx, **_api_scope(ident))
            if existing is not None and existing.is_hidden and not see_hidden:
                # #540: a caller below manager must not edit, unhide or probe a key hidden
                # from translators by upserting its name; answer as for any refused entry.
                errors.append({"index": i, "name": name, "error": keys_svc.KEY_NAME_UNAVAILABLE})
                continue
            if existing is None:
                key = keys_svc.create_key(
                    session,
                    project,
                    name,
                    namespace=ns,
                    context=ctx,
                    description=str(rec.get("description") or ""),
                    char_limit=rec.get("char_limit"),
                    is_plural=bool(rec.get("is_plural")),
                    plural_arg_name=rec.get("plural_arg_name"),
                    tags=rec.get("tags"),
                    source_file=str(rec.get("source_file") or ""),
                    custom=rec.get("custom"),
                    is_hidden=bool(rec.get("is_hidden")),
                    is_archived=bool(rec.get("is_archived")),
                    base_value=rec.get("base"),
                    actor=actor,
                )
                created.append(key.id)
            else:
                key = existing
                meta = {
                    f: rec[f]
                    for f in (
                        "description",
                        "context",
                        "char_limit",
                        "tags",
                        "source_file",
                        "is_hidden",
                        "is_archived",
                        "custom",
                        "plural_arg_name",
                    )
                    if f in rec
                }
                if "is_plural" in rec:
                    # #532: update_key refuses is_plural (promotion/demotion materializes or
                    # destroys value rows), so the old path reported the key `updated` and
                    # left it singular. apply_key_edit is the validated seam the single-key
                    # PATCH uses; a refused demotion lands in errors[] like any bad row.
                    keys_svc.apply_key_edit(
                        session, key, actor=actor, is_plural=bool(rec["is_plural"]), **meta
                    )
                elif meta:
                    keys_svc.update_key(session, key, actor=actor, **meta)
                if rec.get("base") is not None:
                    keys_svc.set_base_value(session, key, rec["base"], actor=actor)
                updated.append(key.id)
        except keys_svc.KeyServiceError as exc:
            session.rollback()
            errors.append({"index": i, "name": name, "error": str(exc)})
    return JSONResponse(
        {
            "ok": True,
            "project_id": project.id,
            "created": created,
            "updated": updated,
            "errors": errors,
        }
    )


@api_router.post("/api/projects/{pid}/keys/import")
async def api_keys_import(pid: int, request: Request, session: Session = Depends(get_session)):
    """File import. Body: {filename, data_b64 | data, fmt, locale, cleanup,
    namespace}. `namespace: "@from-key"` is the `NS_FROM_KEY` sentinel — accepted
    ONLY on a marked, namespace-qualified export (the seam refuses otherwise)."""
    import base64
    import binascii

    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    project, denied = _api_project(session, ident, pid)
    if denied:
        return denied
    if connected_projects.is_connected(project):
        return _api_error(connected_projects.UPLOAD_REFUSED)
    # #275: bounded read BEFORE json parsing and base64 decoding. The body carries the
    # file base64-encoded (4/3 of its size) plus a small JSON envelope.
    raw_cap = config.MAX_UPLOAD_BYTES * 4 // 3 + 64 * 1024
    raw_body = await deps.read_capped_body(request, raw_cap)
    if raw_body is None:
        return JSONResponse(
            {
                "ok": False,
                "error": f"payload exceeds the upload cap ({config.MAX_UPLOAD_BYTES} bytes)",
            },
            status_code=413,
        )
    try:
        body = json.loads(raw_body)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return _api_error("body must be a JSON object")
    raw = body.get("data_b64")
    if raw is not None:
        try:
            data = base64.b64decode(str(raw), validate=True)
        except (ValueError, binascii.Error):
            return _api_error("data_b64 is not valid base64")
    elif body.get("data") is not None:
        data = str(body["data"]).encode("utf-8")
    else:
        return _api_error("nothing to import: pass data_b64 (or data)")
    if len(data) > config.MAX_UPLOAD_BYTES:
        return _api_error("payload exceeds the upload cap")
    ns = str(body.get("namespace") or "").strip()
    try:
        # #599: the file parse runs on a bounded worker, never on the event loop.
        report = await deps.offload_blocking(
            key_files.import_keys,
            session,
            project,
            str(body.get("filename") or "upload.json"),
            data,
            fmt=str(body.get("fmt") or ""),
            locale=str(body.get("locale") or ""),
            mode=(key_files.MODE_CLEANUP if body.get("cleanup") else key_files.MODE_MERGE),
            namespace=(key_files.NS_FROM_KEY if ns == "@from-key" else (ns if ns else None)),
            actor=ident["actor"],
            ip=_ip(request),
            include_hidden=_api_sees_hidden_keys(ident),
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    except keys_svc.KeyServiceError as exc:
        return _api_error(exc)
    except Exception as exc:
        _logger.exception("API key import failed")
        session.rollback()
        return _api_error(f"that file could not be read: {exc}")
    return JSONResponse({"ok": True, **report})


@api_router.get("/api/projects/{pid}/keys/export")
def api_keys_export(
    pid: int,
    request: Request,
    locale: str = "",
    fmt: str = "json",
    namespace: str = "",
    empty_as: str = key_files.EMPTY_EMPTY,
    status: str = "",
    archived: str = "",
    session: Session = Depends(get_session),
):
    """One locale's file as raw bytes. `namespace=` gives the BARE build file; no
    namespace gives the MARKED, namespace-qualified transport file.

    `include_string_images=1` (Batch 14 Phase 1.5) returns a ZIP BUNDLE instead: the key
    file PLUS each exported key's ready in-context images under `images/` PLUS a
    `manifest.json` linking key to image path. Off (default) is byte-identical to today;
    ignored (plain file) when the image feature is off."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    project, denied = _api_project(session, ident, pid)
    if denied:
        return denied
    rep: list = []
    # H1 (gate round 2): read from the RAW query string, not a declared FastAPI param, so the
    # flag never lands in /openapi.json and therefore never in the OpenAPI-derived UTCP manual
    # (the second agent-facing tool descriptor). Operators keep it on web + /api; agents (MCP +
    # UTCP) have no bulk-pixel export tool by design (SPEC section 8).
    _isi = request.query_params.get("include_string_images", "")
    want_images = str(_isi).strip() in ("1", "true", "on") and key_images.enabled()
    ekeys: list = [] if want_images else None
    try:
        data = key_files.export_locale(
            session,
            project,
            locale,
            fmt=fmt,
            namespace=(namespace if namespace.strip() else None),
            empty_as=empty_as,
            # K9: KEY-LEVEL status filter -- same semantics/name as the grid route
            # and `keys export --status`. Empty string is no filter.
            status=(status.strip() or None),
            include_archived=str(archived) in ("1", "true"),
            include_hidden=_api_sees_hidden_keys(ident),
            report=rep,
            exported_keys=ekeys,
        )
    except export.ExportEmptyTargetsHeld as exc:
        # Batch 15 T2 (§9.2): the TM-only empty gate held this key export. 409; the way out
        # is the AUDITED approval (POST .../keys/deliver-approve), never a source-filled file.
        _loc = locale.strip() or project.source_locale
        return JSONResponse(
            {
                "ok": False,
                "empty_held": True,
                "error": str(exc),
                "locale": _loc,
                "empties": exc.empties,
                "approve_url": f"/api/projects/{pid}/keys/deliver-approve?locale={_loc}",
            },
            status_code=409,
        )
    except keys_svc.KeyServiceError as exc:
        return _api_error(exc)
    loc = locale.strip() or project.source_locale
    # Batch 9: a resource format downloads under its own extension, in its own encoding.
    fname = key_files.download_filename(
        namespace.strip() or "project", loc, key_files.export_ext(fmt)
    )
    media_type = key_files.export_media_type(fmt)
    img_info: dict = {}
    bundle = None
    if want_images:
        when = datetime.now(timezone.utc)  # the export's own timestamp (manifest generated_at, M3)
        try:
            bundle, img_info = key_images.build_export_bundle(
                session,
                project,
                loc,
                key_file_bytes=data,
                key_file_name=fname,
                exported_keys=ekeys,
                qualify=(not namespace.strip()),
                generated_at=when,
            )
        except key_images.KeyImageError as exc:
            return _api_error(exc)
        fname = img_info["download_name"]  # shares the SANITISED key-file base (H1)
        media_type = "application/zip"
    audit.log(
        actor=ident["actor"],
        action="keys.export",
        target="project %d %s" % (project.id, loc),  # noqa: UP031
        ip=_ip(request),
        detail=key_files.audit_detail(rep)  # fix-round F9, as above
        + (
            f" [include_string_images: {img_info.get('image_count', 0)} image(s)]"
            if want_images
            else ""
        ),
        customer_id=audit.customer_for_project(project.id),
    )
    if want_images and img_info.get("image_ids"):
        # Per-export pixel-pull row (H2), mirroring the raw-serve route: ONE key_image.raw row
        # listing the image ids that left in the bundle.
        audit.log(
            actor=ident["actor"],
            action="key_image.raw",
            target="project %d %s" % (project.id, loc),  # noqa: UP031
            ip=_ip(request),
            detail="export bundle image ids: " + ",".join(str(i) for i in img_info["image_ids"]),
            customer_id=audit.customer_for_project(project.id),
        )
    headers = {
        "X-Content-Type-Options": "nosniff",
        # Sanitized for the same reason as the grid route above.
        "Content-Disposition": f'attachment; filename="{fname}"',
    }
    if want_images:
        # The bundle facts in the one channel a raw-bytes response has, so the `--remote` CLI
        # arm can report the image count/bytes exactly as the local arm does (L10).
        headers["X-Key-Image-Count"] = str(img_info.get("image_count", 0))
        headers["X-Key-Image-Bytes"] = str(img_info.get("image_bytes", 0))
        # L2: the compressed archive size is known, so set a real Content-Length on the stream.
        if img_info.get("bundle_bytes") is not None:
            headers["Content-Length"] = str(img_info["bundle_bytes"])
    # The tag-guard facts, in the ONE channel a raw-bytes response has (live-QA K2) --
    # the same header vocabulary the document export uses, and the channel the
    # `--remote` CLI arm rebuilds its report from.
    headers.update(key_files.export_headers(rep))
    if bundle is not None:
        # STREAM the ZIP from the spooled temp file (M2).
        return StreamingResponse(
            key_images.iter_bundle(bundle), media_type=media_type, headers=headers
        )
    return Response(
        content=data,
        media_type=media_type,
        headers=headers,
    )


# --- TM-only delivery empty gate: KEY/project mode (Batch 15 T2, §9.3/§9.6, N1) ----
# Siblings of /api/projects/{pid}/keys/export (NOT /api/keys/{id}, which is a StringKey id
# everywhere -- a confinement hazard). KEYS-module owned: 404 when key_projects_enabled=0.
# MCP-EXEMPT (approve/revoke human-decision, empties human-repair-surface); no new tool.
@api_router.get("/api/projects/{pid}/keys/empties")
def api_keys_empties(
    pid: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    """The untranslated (empty) keys for a held (project, locale) key export -- the actual
    key names a reviewer approves before delivery. 404 when key projects are disabled."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    project, denied = _api_project(session, ident, pid)
    if denied:
        return denied
    loc = (locale or "").strip() or project.source_locale
    # M1: a REVIEW surface -- compute the decision at the WIDEST universe (matching the
    # key_empties list it shows and what approve_keys will cover), so an archived-only empty
    # never reads "decision=pass" while the key is listed.
    # #540: a caller below manager neither sees hidden keys listed nor has them counted.
    see_hidden = _api_sees_hidden_keys(ident)
    decision = tm_delivery.empty_gate_decision(
        session, project=project, locale=loc, include_archived=True, include_hidden=see_hidden
    )
    empties = tm_delivery.key_empties(session, project, loc, include_hidden=see_hidden)
    row = tm_delivery._key_approval_row(session, project.id, loc)
    return JSONResponse(
        {
            "ok": True,
            "locale": loc,
            "decision": decision,
            "empty_count": len(empties),
            "empties": empties,
            "approved": bool(row is not None and row.status == "approved"),
        }
    )


@api_router.post("/api/projects/{pid}/keys/deliver-approve")
def api_keys_deliver_approve(
    pid: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    """Approve delivering the CURRENT empty keys of (project, locale) per empty_as. A human
    delivery decision (can_write); audited delivery.empty_approved. 404 when off."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    project, denied = _api_project(session, ident, pid)
    if denied:
        return denied
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    try:
        row = tm_delivery.approve_keys(session, project, locale, approved_by=ident["actor"])
    except tm_delivery.LocaleNotDeliverable as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        {"ok": True, "approved": True, "locale": row.locale, "covered_count": row.covered_count}
    )


@api_router.post("/api/projects/{pid}/keys/deliver-revoke")
def api_keys_deliver_revoke(
    pid: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    """Revoke a prior empty-delivery approval for (project, locale) -> re-held. A no-record
    revoke is a 200 no-op. 404 when key projects are disabled."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    project, denied = _api_project(session, ident, pid)
    if denied:
        return denied
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    loc = (locale or "").strip() or project.source_locale
    try:
        revoked = tm_delivery.revoke_keys(session, project, locale, approved_by=ident["actor"])
    except tm_delivery.LocaleNotDeliverable as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "revoked": revoked, "locale": loc})


@api_router.post("/api/projects/{pid}/keys/translate")
async def api_keys_translate(pid: int, request: Request, session: Session = Depends(get_session)):
    """Launch translation over a KEY project, for one target locale or `locale=all`
    (spec R1: closes the 'a key run is web-only' gap named in llm.md). can_write, keys
    scope. The port of the web keys_translate route: the SAME keys.launch_translation
    seam against this project's internal namespace documents, so run views, spend safety
    and write-back behave exactly as for a document.

    THREE FUNNELS, in order: the project through _api_project (a foreign key project is
    404), the workflow_id through _api_workflow_visible (a foreign-org/customer workflow
    is 404 — it must not be launchable, and launching bills), and the mock-409 preflight
    (inside keys_svc.launch_translation, the shared seam) BEFORE any launch.
    Returns launched run ids admitted immediately and queued rows with run ids created
    in QUEUED status, plus busy/paused/skipped lists. queue_group identifies the durable
    fan-out; queue_incomplete names members that could not be queued."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    project, denied = _api_project(session, ident, pid)
    if denied:
        return denied
    body = await _json_body(request)
    if not isinstance(body, dict):
        return _api_error("body must be a JSON object")
    from app.kernel import ids as _ids

    workflow_id = _ids.coerce_id(body.get("workflow_id"))
    locale = str(body.get("locale") or "").strip()
    if workflow_id is None or not locale:
        return _api_error('workflow_id and locale (a target locale, or "all") are required.')
    # The workflow_id funnel — resolved INSIDE the caller's ring, so a foreign one is a
    # 404 indistinguishable from a workflow that never existed (never a launch).
    if _api_workflow_visible(session, ident, workflow_id) is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    # THE launch loop, the archive gates and the mock-409 preflight all live in the
    # shared seam (keys_svc.launch_translation) so this route and `keys translate`
    # cannot drift. This route keeps ONLY its identity ring (above) + response shaping.
    from app.kernel import content as content_svc
    from app.kernel import workflows as wf_service

    try:
        outcome = keys_svc.launch_translation(
            session,
            project,
            workflow_id,
            locale,
            actor=ident["actor"],
            ip=security.client_ip(request),
            include_hidden=_api_sees_hidden_keys(ident),
        )
    except keys_svc.TranslationEngineError as exc:
        # The mock-409 law: a real-but-not-live engine would silently echo. 409, exactly
        # as document translate and the old inline _keys_translate_mock_block answered.
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except keys_svc.KeyBudgetError as exc:
        # CV-2 (M1): the project is at its monthly USD cap -> 429, unified with the
        # document / workflow-run / batch budget seams (never a 500 or half-launch).
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
    except (
        keys_svc.KeyServiceError,
        content_svc.ProjectArchivedError,
        wf_service.WorkflowArchivedError,
    ) as exc:
        # Bad target locale / archived project / archived workflow -> 400 (unchanged).
        return _api_error(str(exc))
    launched = outcome["launched"]
    # Audit and launch state are shared with the web form and in-process CLI.
    queued = outcome.get("queued") or []
    queue_incomplete = outcome.get("queue_incomplete") or []
    # GitHub #213 part 2 (fix round 1): pairs REFUSED because a run of a DIFFERENT
    # workflow is already in flight on that (document, locale). Dropped by every
    # surface in the first build (the web even answered "add keys first" for a pair
    # that had keys and a run); now every surface carries them, same as `queued`.
    conflicts = outcome.get("conflicts") or []
    body = {
        "ok": True,
        "project_id": project.id,
        "launched": launched,
        "started": outcome["started"],
        "restarted": outcome["restarted"],
        "busy": outcome["busy"],
        "paused": outcome["paused"],
        "skipped": outcome["skipped"],
        # Item A: FULL_TEXT documents skipped because the workflow has a tm_match step
        # (per-doc; the fan-out is never aborted). [] on the common case.
        "skipped_incompatible": outcome.get("skipped_incompatible", []),
        # THE PAIRS THIS INSTALL HAD NO FREE SLOT FOR (gate A round 3, HIGH-1), each with
        # the id of the durable QUEUED run row that is waiting for one. A client polling
        # `launched` to decide whether the project is translated MUST see these: without
        # them the same body reported a fan-out it had done 3% of as an unqualified success.
        "queued": queued,
        "queue_group": outcome.get("queue_group") or "",
    }
    if queue_incomplete:
        # Pairs whose run row could not be written at all (write contention). Present only
        # when non-empty, so the common body is unchanged.
        body["queue_incomplete"] = queue_incomplete
    if conflicts:
        # Present only when non-empty. Each row: locale, document, the running run's id,
        # code `run_conflict` and the standard refusal sentence. A partial fan-out stays
        # 200 with the conflicts listed alongside what DID start.
        body["conflicts"] = conflicts
        _requested = keys_svc.active_locales(session, project.id) if locale == "all" else [locale]
        _nothing_ran = not (
            launched
            or outcome["started"]
            or outcome["restarted"]
            or outcome["busy"]
            or outcome["paused"]
            or queued
            or queue_incomplete
        )
        if _nothing_ran and set(_requested) <= (
            {r["locale"] for r in conflicts} | set(outcome["skipped"])
        ):
            # EVERY requested locale is either bound to another workflow's run or has no
            # content at all (`skipped`): the whole call was refused or was a no-op, so
            # it answers like the single-run refusal -- 409 with the same machine shape
            # (`code`, the running `run_id`, the sentence naming it; multiple conflicts'
            # sentences are joined, and the per-locale rows ride in `conflicts`). #337 N-3:
            # the pre-fix set test demanded conflicts cover EVERY requested locale, so a
            # fan-out with one empty locale + the rest conflicted (nothing launched)
            # slipped through as a 200 ok:true.
            body["ok"] = False
            body["error"] = " ".join(r["reason"] for r in conflicts)
            body["code"] = conflicts[0]["code"]
            body["run_id"] = conflicts[0]["run_id"]
            return JSONResponse(body, status_code=409)
    return JSONResponse(body)


@api_router.get("/api/projects/{pid}/changes")
def api_changes(
    pid: int,
    request: Request,
    since: int | None = None,
    limit: int = 0,
    modified_after: str | None = None,
    after_id: int | None = None,
    locale: str = "",
    session: Session = Depends(get_session),
):
    """THE DELTA API — the differentiator (spec section 5). Two mutually-exclusive
    modes:

      * `since=<cursor>` (default) — the CHANGELOG feed: `{entries, next_cursor,
        has_more, resync_required}` ordered by the changelog PK. DELETIONS ARE ENTRIES
        (`op: "deleted"`), never an absence; `since=0` is a full bootstrap; a cursor
        below the retention floor answers `resync_required: true` with an honest
        message instead of a confident, wrong "you are up to date". This is the ONLY
        mode that reports deletions and metadata-only key edits. `since` is a whole
        number (a non-integer is FastAPI's own 422); a negative cursor clamps to 0.

      * `modified_after=<ISO-8601>` — the CURRENT-STATE feed: value rows of keys whose
        content/status/source changed after that UTC timestamp, KEYSET-paged by
        `{entries, next_modified_after, next_after_id, has_more}` (feed both cursor
        parts back). NEVER reports deletions (use `since` for those). Optional `locale=`
        narrows to one target locale and is VALIDATED (400 on an unknown locale). A
        naive timestamp is assumed UTC; an unparseable one is a 400.

    Passing BOTH `since` and `modified_after` is a 400; `locale`/`after_id` outside
    modified_after mode is a 400. In both modes VALUE entries carry `text` +
    `content_hash` (additive) so a consumer need not N+1 back per key."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    project, denied = _api_project(session, ident, pid)
    if denied:
        return denied
    if since is not None and modified_after is not None:
        return _api_error("since and modified_after are mutually exclusive; pass one.")
    # H2a: `locale`/`after_id` belong ONLY to modified_after mode. Accept-and-ignore is
    # the exact silent-wrong-answer this feed forbids, so refuse the combination loudly.
    if modified_after is None and (locale or after_id is not None):
        return _api_error("locale and after_id are only valid with modified_after.")
    # NO UPPER REFUSAL HERE, deliberately: `/changes` has always CLAMPED an absurd
    # limit to MAX_DELTA_LIMIT, a delta consumer may reasonably ask for "as much as
    # you will give me", and that behaviour is pinned by verify_delta_api. A-F10's
    # finding was not the clamp, it was the SILENCE -- so the clamp stays and the
    # effective limit rides back below. A NEGATIVE limit is still a refusal: it has
    # never had a meaning, and clamping it to 1 walks a consumer through the feed one
    # entry at a time with nothing saying its request was wrong.
    bad = _limit_refusal(limit, None, allow_zero=True)
    if bad:
        return bad
    effective = min(int(limit or keys_svc.DEFAULT_DELTA_LIMIT), keys_svc.MAX_DELTA_LIMIT)
    try:
        if modified_after is not None:
            out = keys_svc.changes_modified_after(
                session,
                project.id,
                modified_after,
                effective,
                after_id=after_id,
                locale=(locale or None),
                include_hidden=_api_sees_hidden_keys(ident),
                **_api_scope(ident),
            )
        else:
            out = keys_svc.changes_since(
                session,
                project.id,
                since or 0,
                effective,
                include_hidden=_api_sees_hidden_keys(ident),
                **_api_scope(ident),
            )
    except keys_svc.NotAKeyProjectError:
        return JSONResponse(_NOT_FOUND, status_code=404)
    except (keys_svc.BadTimestampError, keys_svc.UnknownLocaleError) as exc:
        return _api_error(exc)
    # THE EFFECTIVE LIMIT RIDES BACK (review A-F10). The service clamps to
    # MAX_DELTA_LIMIT, so a consumer asking for 5,000 got 1,000 rows, `has_more: true`
    # and no way to tell whether the cap was ITS request or the server's -- i.e. it
    # could not know its page size, which is the one number a paging client tunes.
    return JSONResponse(
        {
            "ok": True,
            "project_id": project.id,
            "limit": effective,
            "max_limit": keys_svc.MAX_DELTA_LIMIT,
            **out,
        }
    )
