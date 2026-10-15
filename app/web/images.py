"""Images (Beta) — the image-localization module surface.

SPEC-image-localization Phase 1. Customer-first module page (images ARE
documents under projects — the picker/scoping conventions mirror /tm), a
per-image REVIEW page (original vs localized per locale, per-region list with
confidence/overflow flags, region-translation edit + re-render, export), and
minimal /api read+export parity (spec 3.5).

Every route gates on the `image_localization_enabled` kill switch (404 when
off) AND on the standard visibility rings (_visibility_block / doc-scope for
/api). Logic lives in services/image_localization.py; this stays a thin caller.

Documented in: docs/architecture/image-localization.md
"""

import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.kernel import (
    access,
    audit,
    connected_projects,
    content,
    qe,
    security,
    segment_review,
    tm_module,
)
from app.kernel import image_localization as il
from app.kernel.ids import coerce_id
from app.models import Document, Project, Segment, SegStatus
from app.web import deps
from app.web.deps import (
    _actor_email,
    _error_page,
    _ip,
    _missing_or,
    _visibility_block,
    mime_for_fmt,
    templates,
)

router = APIRouter()
# The token-authed /api image routes live on a SEPARATE router so the embedded/headless
# profile can mount them while dropping the Jinja image-review pages above. Both are
# registered in a default boot (app.web.__init__: images.router -> WEB_ROUTERS,
# images.api_router -> API_ROUTERS), so the served route set is unchanged. Every
# @api_router route below is /api-prefixed.
api_router = APIRouter()
_logger = logging.getLogger("loc_tms")

IMAGE_FMTS = list(il.IMAGE_EXTS)

# An uploaded SVG is attacker-controlled markup that CAN carry <script>, event
# handlers, and foreign objects. Serving it inline, same-origin, as image/svg+xml
# is stored XSS: a victim who opens the raw endpoint executes the SVG's script in
# the app's origin (session-riding). SVG is therefore ALWAYS served as a download
# (Content-Disposition: attachment — <img> subresource previews still render, but
# a top-level navigation downloads instead of executing) under a locked-down CSP
# that neuters script/network even if a browser renders it inline anyway. nosniff
# stops a raster response being re-sniffed into an executable type.
_SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def _serve_image(
    data: bytes, fmt: str, *, download_name: str | None = None, force_attachment: bool = False
) -> Response:
    """Serve stored image bytes with anti-XSS headers. SVG is ALWAYS a download
    (+ CSP) because it can execute script inline; rasters serve inline for the
    <img> review previews unless force_attachment (the explicit export routes)."""
    fmt = (fmt or "").lower()
    headers = {"X-Content-Type-Options": "nosniff"}
    if fmt == "svg":
        headers["Content-Security-Policy"] = _SVG_CSP
    if force_attachment or fmt == "svg":
        name = download_name or f"image.{fmt}"
        # HIGH-2: `download_name` is a RAW doc.filename -> a bare filename="..." here is a
        # Content-Disposition breakout on a `"` and a 500 on a non-latin-1 name (probe-proven
        # via an image upload + original-download). Build via content.content_disposition_
        # attachment (RFC-6266: neutralised ASCII fallback + percent-encoded filename*).
        headers["Content-Disposition"] = content.content_disposition_attachment(name)
    return Response(content=data, media_type=mime_for_fmt(fmt), headers=headers)


def _flag_off_block(request: Request):
    """Layer-2 WEB kill switch (the middleware in app/main is layer 1): 404 when the
    'images' feature is off. Delegated to the ONE shared deps.feature_block, whose
    features.is_enabled uses the SAME strict `str(get(...)) == "1"` predicate as the old
    inline `il.enabled()` -- so this is behaviour-identical for every setting value, a
    non-canonical value ("true") failing toward OFF at both layers. The call sites below
    are UNMOVED (defense in depth); nav is the single `feature_enabled('images')`
    global."""
    return deps.feature_block(request, "images")


def _visible_pids(request, session):
    me = security.session_user(request)
    return access.visible_project_ids(session, me)


def _image_docs(session, pids) -> list[Document]:
    q = select(Document).where(Document.fmt.in_(IMAGE_FMTS)).order_by(Document.created_at.desc())
    if pids is not None:
        q = q.where(Document.project_id.in_(pids))
    return list(session.exec(q).all())


def _doc_locales(session, doc_id: int) -> list[str]:
    return sorted(
        {
            s.target_locale
            for s in session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        }
    )


# --- module page (customer-first, mirrors /tm) --------------------------------
@router.get("/images")
def images_index(request: Request, session: Session = Depends(get_session)):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    me = security.session_user(request)
    base_pids = _visible_pids(request, session)
    org_id = access.current_org_id(me)
    bound_cid = access.current_customer_id(me)

    qp = request.query_params
    customers = tm_module.browsable_customers(session, org_id=org_id, bound_customer_id=bound_cid)
    selected = tm_module.resolve_browse_customer(customers, qp.get("customer"))
    show_picker = bound_cid is None and len(customers) > 1

    ctx = {
        "request": request,
        "customers": customers,
        "show_picker": show_picker,
        "selected_customer": selected,
        "no_customers": len(customers) == 0,
        "detector": il.detector_status(),
        "image_fmts": IMAGE_FMTS,
        "err": qp.get("err"),
        "uploaded": qp.get("uploaded"),
    }
    if selected is None:
        return templates.TemplateResponse(
            request, "images.html", {**ctx, "rows": [], "projects": []}
        )

    pids = tm_module.scoped_pids_for_customer(session, base_pids, selected.id)
    docs = _image_docs(session, pids)
    projects = (
        {p.id: p for p in session.exec(select(Project).where(Project.id.in_(pids))).all()}
        if pids
        else {}
    )
    rows = []
    for d in docs:
        skel = d.skeleton or {}
        rows.append(
            {
                "doc": d,
                "project": projects.get(d.project_id),
                "kind": skel.get("kind", "raster"),
                "regions": len(skel.get("regions") or skel.get("units") or []),
                "locales": _doc_locales(session, d.id),
            }
        )
    return templates.TemplateResponse(
        request,
        "images.html",
        {**ctx, "rows": rows, "projects": sorted(projects.values(), key=lambda p: p.name.lower())},
    )


# --- upload -------------------------------------------------------------------
@router.post("/images/upload")
async def images_upload(
    request: Request,
    project_id: int = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(
            request, session, RedirectResponse("/images?err=noproject", status_code=303)
        )
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    if connected_projects.is_connected(project):
        return _error_page(request, 400, connected_projects.UPLOAD_REFUSED)
    # Bounded read (shared helper): never buffer past the cap.
    data = await deps.read_capped_upload(file, config.MAX_UPLOAD_BYTES)
    if data is None:
        return RedirectResponse("/images?err=toolarge", status_code=303)
    try:
        # #271: parse in a worker thread, never on the event loop.
        doc = await deps.offload_parse(
            il.ingest_image,
            session,
            project,
            file.filename or "upload",
            data,
            actor=_actor_email(request),
        )
    except deps.WorkerBusy:
        return RedirectResponse("/images?err=busy", status_code=303)
    except il.ImageLocalizationError as exc:
        _logger.info("Image upload refused: %s", exc)
        return RedirectResponse("/images?err=badimage", status_code=303)
    except Exception:
        _logger.exception("Image ingest failed for %r", file.filename)
        session.rollback()
        return RedirectResponse("/images?err=parse", status_code=303)
    return RedirectResponse(f"/images/{doc.id}", status_code=303)


# --- review page ----------------------------------------------------------------
def _get_image_doc(request, session, doc_id):
    """(doc, project, blocked): 404 for a missing/non-image doc, the standard
    visibility ring for the rest."""
    doc = session.get(Document, doc_id)
    if not doc:
        return None, None, _missing_or(request, session, _error_page(request, 404, "Not found."))
    project = session.get(Project, doc.project_id)
    # The ring BEFORE the image-type check: a hidden image and a hidden non-image document
    # must answer alike (#575), so the type of a document the caller cannot see never shows.
    blocked = _visibility_block(request, session, project)
    if blocked:
        return None, None, blocked
    if not il.is_image_fmt(doc.fmt):
        return None, None, _error_page(request, 404, "Not found.")
    return doc, project, None


@router.get("/images/{doc_id}")
def image_detail(
    doc_id: int,
    request: Request,
    locale: str | None = None,
    session: Session = Depends(get_session),
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    doc, project, blocked = _get_image_doc(request, session, doc_id)
    if blocked:
        return blocked
    locales = _doc_locales(session, doc.id)
    locale = locale if locale in locales else (locales[0] if locales else None)
    skel = doc.skeleton or {}
    rows = il.region_rows(session, doc, locale) if locale else []
    n_overflow = sum(1 for r in rows if r.get("overflow"))
    n_lowconf = sum(1 for r in rows if r.get("low_confidence"))
    # #125 (MED): the vision engine target was refused by egress policy at escalate time.
    # One distinct message per install/run (the same target refuses every region), so the
    # banner shows the first one, mirroring OCR making its "[OCR error: ...]" visible.
    # a11y T7: a standing status notice about the stored regions, not a page error (no
    # user input caused it), so it is named for what it is and rendered as detail text
    # under a sentence that says what to do; page errors go through form_errors.
    vlm_blocked_detail = next((r["vlm_error"] for r in rows if r.get("vlm_error")), "")
    return templates.TemplateResponse(
        request,
        "image_detail.html",
        {
            "request": request,
            "doc": doc,
            "project": project,
            "kind": skel.get("kind", "raster"),
            "locales": locales,
            "locale": locale,
            "rows": rows,
            "n_overflow": n_overflow,
            "n_lowconf": n_lowconf,
            "vlm_blocked_detail": vlm_blocked_detail,
            "any_translated": any(r["translated"] for r in rows),
            "context_notes": (doc.context or {}).get("notes", ""),
            "saved": request.query_params.get("saved"),
            "translated": request.query_params.get("translated"),
            "err": request.query_params.get("err"),
        },
    )


@router.get("/images/{doc_id}/original")
def image_original(doc_id: int, request: Request, session: Session = Depends(get_session)):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    doc, _project, blocked = _get_image_doc(request, session, doc_id)
    if blocked:
        return blocked
    from app.ports import blobstore

    data = blobstore.get().get(blobstore.document_key(doc))
    return _serve_image(data, doc.fmt, download_name=doc.filename)


@router.get("/images/{doc_id}/localized")
def image_localized(
    doc_id: int, request: Request, locale: str, session: Session = Depends(get_session)
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    doc, _project, blocked = _get_image_doc(request, session, doc_id)
    if blocked:
        return blocked
    if locale not in _doc_locales(session, doc.id):
        return _error_page(request, 404, "Locale not on this image.")
    data, _flags = il.render_localized(session, doc, locale)
    # Gate MED-2: header-only, but composed through the SAME helper as the export sinks.
    name = content.safe_output_name(doc.filename, locale, doc.fmt, neutralise=False)
    return _serve_image(data, doc.fmt, download_name=name)


# --- actions -------------------------------------------------------------------
@router.post("/images/{doc_id}/translate")
def image_translate(
    doc_id: int, request: Request, locale: str = Form(...), session: Session = Depends(get_session)
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    doc, _project, blocked = _get_image_doc(request, session, doc_id)
    if blocked:
        return blocked
    try:
        il.translate_image(session, doc, locale, actor=_actor_email(request) or "web")
    except il.ImageLocalizationError as exc:
        from urllib.parse import quote

        return RedirectResponse(
            f"/images/{doc_id}?locale={locale}&err=" + quote(str(exc)), status_code=303
        )
    return RedirectResponse(f"/images/{doc_id}?locale={locale}&translated=1", status_code=303)


@router.post("/images/{doc_id}/regions/{seg_id}")
def image_region_save(
    doc_id: int,
    seg_id: int,
    request: Request,
    target_text: str = Form(""),
    locale: str = Form(""),
    session: Session = Depends(get_session),
):
    """Edit a region's translation — writes the Segment exactly like the CAT
    editor's save (target_text + EDITED/NEW + clear the stale QE badge). The
    redirect re-renders the localized preview, so 'save' IS the re-render."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    doc, _project, blocked = _get_image_doc(request, session, doc_id)
    if blocked:
        return blocked
    seg = session.get(Segment, seg_id)
    if not seg or seg.document_id != doc.id:
        return _error_page(request, 404, "Not found.")
    # #617 item 4 (gate): the same refusal as the CAT editor save while a run waits on review.

    try:
        segment_review.refuse_if_awaiting_review(session, doc.id, seg.target_locale)
    except segment_review.AwaitingReviewError as exc:
        return _error_page(request, exc.status, str(exc))
    _prev = seg.target_text or ""
    seg.target_text = target_text
    seg.status = SegStatus.EDITED if target_text.strip() else SegStatus.NEW
    # #617 item 6: a human region save is the linguist's own text, no longer a propagated copy.
    segment_review.clear_propagation_marker(seg)
    seg.qa_flags = qe.clear_qe_flag(seg.qa_flags)
    # item 5a / HIGH-3 (PROPOSAL-tm-matching §5): the image region save is a THIRD human
    # direct writer (same edit semantics as the CAT editor's segment_save), so a human
    # edit of a TM-locked region WINS -- clear the lock + provenance (audited), gated on an
    # ACTUAL text change (MED-10) so a save-without-edit never destroys a lock.
    if target_text != _prev:
        from app.kernel import tm_lock

        tm_lock.clear_lock_on_human_edit(session, seg, actor=_actor_email(request), ip=_ip(request))
    session.add(seg)
    session.commit()
    loc = locale or seg.target_locale
    return RedirectResponse(f"/images/{doc_id}?locale={loc}&saved=1", status_code=303)


@router.get("/images/{doc_id}/export")
def image_export(
    doc_id: int, request: Request, locale: str, session: Session = Depends(get_session)
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    doc, _project, blocked = _get_image_doc(request, session, doc_id)
    if blocked:
        return blocked
    if locale not in _doc_locales(session, doc.id):
        return _error_page(request, 404, "Locale not on this image.")
    data, _flags = il.render_localized(session, doc, locale)
    audit.log(
        actor=_actor_email(request),
        action="document.export",
        target=f"doc {doc.id} {locale}",
        ip=_ip(request),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    # Gate MED-2: header-only, but composed through the SAME helper as the export sinks.
    fname = content.safe_output_name(doc.filename, locale, doc.fmt, neutralise=False)
    return _serve_image(data, doc.fmt, download_name=fname, force_attachment=True)


# --- /api parity (read + export; docs-scope, mirrors the web predicate) ---------
def _api_flag_block():
    # Layer-2 /api kill switch, delegated to the shared deps.feature_api_block; the JSON
    # 404 body is byte-identical to the old inline check, and its strict is_enabled
    # matches the old inline il.enabled() (`str(get(...)) == "1"`) for every value.
    return deps.feature_api_block("images")


def _api_image_doc(s, ident, doc_id):
    """(doc, denied) with the SAME object-authz the /api document routes use
    (api._doc_authz semantics: 404, never enumerable) + the image-fmt check."""
    doc = s.get(Document, doc_id)
    if doc is None or not il.is_image_fmt(doc.fmt):
        return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    project = s.get(Project, doc.project_id)
    if project is None:
        return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if security.auth_required() and not access.can_view_project(s, ident.get("user"), project):
        return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return doc, None


_API_FORBIDDEN = JSONResponse(
    {"ok": False, "error": "your role does not allow this action."}, status_code=403
)


def _api_write_block(ident):
    """can_write parity (SPEC-full-remote-parity R1b): the image WRITE routes (upload,
    translate) mirror the web /images upload/translate gate, which follows the standard
    write role. No-op with the login wall off (single-tenant), like every /api route."""
    if not security.auth_required():
        return None
    if security.can_write(ident.get("user")):
        return None
    return _API_FORBIDDEN


def _api_project_writable(s, ident, project_id):
    """(project, denied): the project a docs-scoped caller may WRITE an image into — the
    SAME can_view_project ring _api_image_doc uses (a foreign-org/customer project is a
    404, never enumerable), resolved BEFORE the write role gate (scope-before-validate).
    The role gate itself is applied by the caller via _api_write_block."""
    project = s.get(Project, project_id) if project_id is not None else None
    if project is None:
        return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if security.auth_required() and not access.can_view_project(s, ident.get("user"), project):
        return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return project, None


def _api_image_mock_block(s, doc, locale):
    """409 when the image's project would resolve `locale` to the keyless offline mock
    (the day-one trust guard, mirroring document translate). The image pipeline shares
    the same per-pair provider resolution, so this refuses a 'real' image translate that
    would silently echo through the mock BEFORE translate_image spends anything."""
    from app.kernel import pair_providers, providers

    project = s.get(Project, doc.project_id)
    if project is None:
        return None
    try:
        eff = pair_providers.resolve_provider(s, project, project.source_locale, locale).lower()
    except Exception:
        return None
    if eff != "mock" and not providers.provider_is_live(eff):
        return JSONResponse(
            {
                "ok": False,
                "error": "No translation engine configured - set an API key "
                "before submitting for translation.",
            },
            status_code=409,
        )
    return None


@api_router.get("/api/images")
def api_images_list(request: Request, session: Session = Depends(get_session)):
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    me = ident.get("user")
    pids = access.visible_project_ids(session, me) if security.auth_required() else None
    docs = _image_docs(session, pids)
    return JSONResponse(
        {
            "ok": True,
            "images": [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "fmt": d.fmt,
                    "project_id": d.project_id,
                    "kind": (d.skeleton or {}).get("kind", "raster"),
                    "regions": len(
                        (d.skeleton or {}).get("regions") or (d.skeleton or {}).get("units") or []
                    ),
                    "locales": _doc_locales(session, d.id),
                }
                for d in docs
            ],
        }
    )


@api_router.get("/api/images/{doc_id}")
def api_image_show(
    doc_id: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    doc, denied = _api_image_doc(session, ident, doc_id)
    if denied:
        return denied
    locales = _doc_locales(session, doc.id)
    loc = locale if locale in locales else (locales[0] if locales else None)
    rows = il.region_rows(session, doc, loc) if loc else []
    return JSONResponse(
        {
            "ok": True,
            "id": doc.id,
            "filename": doc.filename,
            "fmt": doc.fmt,
            "kind": (doc.skeleton or {}).get("kind", "raster"),
            "locales": locales,
            "locale": loc,
            "regions": [
                {
                    "id": r["region"]["id"],
                    "reading_order_index": r["region"]["reading_order_index"],
                    "bbox": r["region"]["bbox_axis_aligned"],
                    "polygon": r["region"]["polygon"],
                    "confidence": r["confidence"],
                    "source": r["region"].get("source", ""),
                    "source_text": r["source_text"],
                    "target_text": r["target_text"],
                    "translated": r["translated"],
                    "overflow": r["overflow"],
                    # #125 (MED): ADDITIVE field. The egress refusal recorded on this region
                    # at escalate time (empty string when none), so an /api/images client
                    # learns the vision engine target was blocked -- the parallel to OCR's
                    # "[OCR error: ...]" riding in the extracted text. Additive only; never a
                    # rename/removal. The route returns an untyped JSONResponse (no
                    # response_model), so the OpenAPI schema / UTCP manual are unaffected.
                    "vlm_error": r.get("vlm_error", ""),
                    "segment_ids": [s.id for s in r["segments"]],
                }
                for r in rows
            ],
        }
    )


@api_router.get("/api/images/{doc_id}/export")
def api_image_export(
    doc_id: int,
    request: Request,
    locale: str | None = None,
    session: Session = Depends(get_session),
):
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    if not locale or not locale.strip():
        return JSONResponse({"ok": False, "error": "locale is required"}, status_code=400)
    doc, denied = _api_image_doc(session, ident, doc_id)
    if denied:
        return denied
    if locale not in _doc_locales(session, doc.id):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    data, _flags = il.render_localized(session, doc, locale)
    audit.log(
        actor=ident["actor"],
        action="document.export",
        target=f"doc {doc.id} {locale}",
        ip=_ip(request),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    # Gate MED-2: header-only, but composed through the SAME helper as the export sinks.
    fname = content.safe_output_name(doc.filename, locale, doc.fmt, neutralise=False)
    return _serve_image(data, doc.fmt, download_name=fname, force_attachment=True)


# --- /api WRITE parity (upload + translate; SPEC-full-remote-parity R1b) ---------
# Completes the read-only image /api family: POST /api/images (upload, multipart,
# can_write) and POST /api/images/{doc_id}/translate (can_write AND SPEND — mock-409
# before it bills). Both 404 when image_localization_enabled is off (the kill switch,
# like keys). The translate route is the /api arm of the `image translate` CLI verb and
# the MCP image-translate spend tool.
@api_router.post("/api/images")
async def api_images_upload(
    request: Request,
    project_id: str | None = Form(None),
    file: UploadFile | None = File(None),
    session: Session = Depends(get_session),
):
    """Upload an image (svg/png/jpg/jpeg/tif/tiff/bmp/gif/webp) into a project (mirrors
    `image upload` + the
    web /images/upload form). docs scope, can_write. The project_id funnels through the
    can_view_project ring (a foreign-org/customer project is 404) BEFORE the write gate;
    a corrupt/oversized/non-image file is a clean 4xx, never a 500."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    if not project_id or not project_id.strip():
        return JSONResponse({"ok": False, "error": "project_id is required"}, status_code=400)
    pid = coerce_id(project_id) if project_id.isascii() and project_id.isdecimal() else None
    if pid is None:
        return JSONResponse(
            {"ok": False, "error": "project_id must be an integer"}, status_code=400
        )
    if file is None:
        return JSONResponse({"ok": False, "error": "file is required"}, status_code=400)
    project, denied = _api_project_writable(session, ident, pid)
    if denied:
        return denied
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    if connected_projects.is_connected(project):
        return JSONResponse(
            {"ok": False, "error": connected_projects.UPLOAD_REFUSED}, status_code=400
        )
    data = await deps.read_capped_upload(file, config.MAX_UPLOAD_BYTES)
    if data is None:
        return JSONResponse(
            {"ok": False, "error": "file too large (over the upload cap)."}, status_code=413
        )
    try:
        # #271: parse in a worker thread, never on the event loop.
        doc = await deps.offload_parse(
            il.ingest_image, session, project, file.filename or "upload", data, actor=ident["actor"]
        )
    except deps.WorkerBusy:
        return deps.busy_response()
    except il.ImageLocalizationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:
        _logger.exception("Image ingest failed for %r", file.filename)
        session.rollback()
        return JSONResponse(
            {"ok": False, "error": "could not parse the uploaded image."}, status_code=400
        )
    audit.log(
        actor=ident["actor"],
        action="image.upload",
        target=f"doc {doc.id} in project {project.id}",
        ip=_ip(request),
        customer_id=audit.customer_for_project(project.id),
    )
    return JSONResponse(
        {
            "ok": True,
            "id": doc.id,
            "document_id": doc.id,
            "project_id": project.id,
            "filename": doc.filename,
            "fmt": doc.fmt,
            "kind": (doc.skeleton or {}).get("kind", "raster"),
            "regions": len(
                (doc.skeleton or {}).get("regions") or (doc.skeleton or {}).get("units") or []
            ),
        }
    )


@api_router.post("/api/images/{doc_id}/translate")
async def api_image_translate(
    doc_id: int, request: Request, session: Session = Depends(get_session)
):
    """Translate an image's regions into a target locale SYNCHRONOUSLY (mirrors `image
    translate` + the web action). docs scope, can_write AND SPEND. The doc funnels
    through _api_image_doc (404 foreign / non-image / feature off); the mock-409 guard
    refuses a keyless-mock 'real' translate BEFORE spending. Body: {"locale": "de-DE"}."""
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    doc, denied = _api_image_doc(session, ident, doc_id)
    if denied:
        return denied
    blocked = _api_write_block(ident)
    if blocked:
        return blocked
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    locale = str(body.get("locale") or "").strip()
    if not locale:
        return JSONResponse({"ok": False, "error": "locale is required."}, status_code=400)
    present = _doc_locales(session, doc.id)
    if locale not in present:
        return JSONResponse(
            {
                "ok": False,
                "error": f"locale {locale!r} is not on image #{doc.id} "
                f"(present: {', '.join(present) or 'none'}).",
            },
            status_code=400,
        )
    mock = _api_image_mock_block(session, doc, locale)
    if mock:
        return mock
    # #617 item 4 (gate): 409 while a run waits on a human review step for this locale.

    try:
        segment_review.refuse_if_awaiting_review(session, doc.id, locale)
    except segment_review.AwaitingReviewError as exc:
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": str(exc), "run_id": exc.run_id},
            status_code=exc.status,
        )
    try:
        # Per-segment LLM + TM/glossary DB work: off the event loop (#495).
        stats = await deps.offload_parse(
            il.translate_image, session, doc, locale, actor=ident["actor"]
        )
    except il.ImageLocalizationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    audit.log(
        actor=ident["actor"],
        action="image.translate",
        target=f"doc {doc.id} {locale}",
        ip=_ip(request),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    return JSONResponse({"ok": True, "document_id": doc.id, "locale": locale, "stats": stats or {}})
