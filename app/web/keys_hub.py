"""keys: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/string-keys.md
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session

from app import config
from app.db import get_session
from app.kernel import (
    access,
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
from app.models import Project, StringKey
from app.web import deps
from app.web.deps import (
    _error_page,
    _ip,
    _missing_or,
    _visibility_block,
    templates,
)
from app.web.keys_common import (
    _EXPORT_NOTICE_COOKIE,
    _KEY_PAGE_MAX,
    _NOT_FOUND,
    _RUNS_COOKIE,
    _RUNS_COOKIE_MAX,
    KEY_STATUSES,
    _actor,
    _api_flag_block,
    _api_gate,
    _api_scope,
    _back,
    _can_write,
    _flag_off_block,
    _grid_filters,
    _limit_refusal,
    _live_runs,
    _notice_path,
    _queued_run_pills,
    _scope,
    _set_export_notice,
    _set_run_cookie,
    _take_export_notice,
    _take_run_cookie,
    _write_gate,
    api_router,
)
from app.web.streaming import StreamingResponse

_logger = logging.getLogger("loc_tms")


router = APIRouter()


def _sees_hidden_keys(request) -> bool:
    """Keys marked "hidden from translators" (SPEC-key-based-projects) are visible to
    manager and above only. Auth off: session_user is the system admin, which passes."""
    return (not security.auth_required()) or security.can_manage(security.session_user(request))


def _api_sees_hidden_keys(ident) -> bool:
    """The /api twin of `_sees_hidden_keys`: with the wall on, fail closed when no user
    rides the identity."""
    return (not security.auth_required()) or security.can_manage((ident or {}).get("user"))


def _key_project(request, session, pid):
    """(project, blocked) for a WEB route: the standard visibility ring, then the
    TYPE check. A document project answers 404 here rather than 400 — a key route
    against it is as much 'no such page' as a bad id, and it keeps the two project
    types from leaking each other's shape."""
    project = session.get(Project, pid)
    if project is None:
        return None, _missing_or(request, session, _error_page(request, 404, "Not found."))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return None, blocked
    if not keys_svc.is_key_project(project):
        return None, _error_page(request, 404, "Not found.")
    return project, None


def _api_project(session, ident, pid):
    """(project, denied): a missing project, one in another org, one belonging to
    another customer, one this caller cannot view, and one that is not KEY-BASED all
    answer the SAME 404 — never 403, never a different message. Cross-scope ids stay
    non-enumerable (deps.py:402-499's convention)."""
    project = session.get(Project, pid)
    if project is None:
        return None, JSONResponse(_NOT_FOUND, status_code=404)
    org_id = deps._ident_org_id(ident)
    if org_id is not None and getattr(project, "org_id", None) != org_id:
        return None, JSONResponse(_NOT_FOUND, status_code=404)
    cust = (ident or {}).get("_cust_id")
    if cust is not None and getattr(project, "customer_id", None) != cust:
        return None, JSONResponse(_NOT_FOUND, status_code=404)
    if security.auth_required() and not access.can_view_project(
        session, ident.get("user"), project
    ):
        return None, JSONResponse(_NOT_FOUND, status_code=404)
    if not keys_svc.is_key_project(project):
        return None, JSONResponse(_NOT_FOUND, status_code=404)
    return project, None


def _api_key_row(session, ident, key_id, include_deleted=False):
    """(key, project, denied) for the key-addressed routes. The key is resolved
    FIRST and its project then goes through `_api_project`, so a key id belonging to
    another tenant is a 404 exactly like a key id that never existed."""
    key = session.get(StringKey, key_id)
    if key is None or (key.deleted_at is not None and not include_deleted):
        return None, None, JSONResponse(_NOT_FOUND, status_code=404)
    project, denied = _api_project(session, ident, key.project_id)
    if denied:
        return None, None, denied
    if key.is_hidden and not _api_sees_hidden_keys(ident):
        # Same 404 as a key that never existed: a hidden key must not be enumerable.
        return None, None, JSONResponse(_NOT_FOUND, status_code=404)
    return key, project, None


@router.get("/projects/{pid}/keys")
def keys_grid(pid: int, request: Request, session: Session = Depends(get_session)):
    """THE key project detail page (spec section 6): namespace-grouped rows, one
    column per target locale. A key project NEVER shows a document list — its
    internal documents are an implementation detail (`is_internal_key_document`)."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return blocked
    qp = request.query_params
    filters = _grid_filters(qp)
    limit = qp.get("limit", "200")
    bad = _limit_refusal(limit, _KEY_PAGE_MAX)
    if bad:
        return bad
    locales = keys_svc.active_locales(session, project.id)
    see_hidden = _sees_hidden_keys(request)
    try:
        page = keys_svc.page_keys(
            session,
            project.id,
            limit=int(limit),
            cursor=qp.get("cursor", 0),
            include_hidden=see_hidden,
            **filters,
            **_scope(request),
        )
        rows = keys_svc.key_views(session, page["keys"], locales, project_id=project.id)
        total = keys_svc.count_keys(
            session, project.id, include_hidden=see_hidden, **filters, **_scope(request)
        )
        all_namespaces = keys_svc.namespaces(
            session, project.id, include_hidden=see_hidden, **_scope(request)
        )
    except keys_svc.NotAKeyProjectError:
        # DEFENSE IN DEPTH, and the reason it is a 404 rather than a 500: the service
        # seams carry their own org/customer guards (review A11) and answer a
        # cross-scope project by refusing. `_visibility_block` above normally gets
        # there first, so this branch is unreachable in a correct build -- but if it
        # ever is reached, the caller must get the same non-enumerable "not found"
        # every other ring gives, not a stack trace that confirms the id exists.
        return _error_page(request, 404, "Not found.")
    # Per-string in-context images (Batch 14): attach each key's ready images (batched,
    # no N+1) so the grid renders the badge + editor gallery. Only when the feature is on.
    if key_images.enabled() and rows:
        _imgs_by_key = key_images.images_for_keys(session, [r["id"] for r in rows], project.id)
        for row in rows:
            row["images"] = [key_images.view(i) for i in _imgs_by_key.get(row["id"], [])]
    # Group only this page for display; the continuation cursor remains id-based.
    rows.sort(key=lambda row: (row["namespace"], row["name"], row["context"], row["id"]))
    groups: list[dict] = []
    for row in rows:
        if not groups or groups[-1]["namespace"] != row["namespace"]:
            groups.append({"namespace": row["namespace"], "rows": []})
        groups[-1]["rows"].append(row)
    from app.web.queries import _applicable_workflows

    # The notice a DOWNLOAD could not show (live-QA K2), rendered once and then cleared.
    notice = _take_export_notice(request, pid)
    # The runs the last Translate POST launched (GitHub #47), read once and then
    # cleared: the progress strip streams them live, so a failed run says so on THIS
    # page instead of being indistinguishable from "never started" forever.
    # Cleared on PRESENCE, not on a successful parse (review F9): a cookie whose
    # tokens were all garbage would otherwise be re-sent on every request for its
    # whole 30-minute life, and any future parse bug in it would be permanent rather
    # than one bad page load.
    had_run_cookie = bool(request.cookies.get(_RUNS_COOKIE))
    run_ids = _take_run_cookie(request, pid)
    live_runs = _live_runs(session, project, run_ids) if run_ids else []
    # Batch 14 T2 (SPEC section-7.2): the DYNAMIC attach-time capability signal. Resolve
    # the project's currently-configured translation engine (default_provider, else the
    # global default) and ask the SAME is_vision_capable predicate the machine side uses,
    # so UI and machine agree on the capability question. Cheap: a pure function of the
    # configured ref (org-scoped for an engine:<id>), no run, no provider construction. A
    # non-vision engine escalates the static callout to a visible warning at the attach
    # surface. Best-effort by contract: a resolution failure falls back to "capable" so the
    # signal never blocks or misfires the page (the machine-side skip note is the backstop).
    engine_vision_capable = True
    if key_images.enabled():
        try:
            # Web shell -> app.kernel facade (NOT app.services directly), the
            # forward_web import contract (verify_import_contracts/verify_kernel_facade).
            from app.kernel.providers import is_vision_capable

            _engine_ref = project.default_provider or config.DEFAULT_PROVIDER
            engine_vision_capable = is_vision_capable(
                _engine_ref, org_id=getattr(project, "org_id", None)
            )
        except Exception:
            engine_vision_capable = True
    resp = templates.TemplateResponse(
        request,
        "project_keys.html",
        {
            "request": request,
            "project": project,
            "locales": locales,
            "groups": groups,
            "shown": len(rows),
            "total": total,
            "has_more": page["has_more"],
            "next_url": request.url.include_query_params(
                cursor=page["next_cursor"],
                limit=int(limit),
            )
            if page["has_more"]
            else None,
            "namespaces": all_namespaces,
            "filters": filters,
            "statuses": KEY_STATUSES,
            "formats": key_files.all_formats(),
            "empty_modes": key_files.EMPTY_MODES,
            "plural_categories": keys_svc.CLDR_CATEGORIES,
            "engine_vision_capable": engine_vision_capable,
            "engine_ref": project.default_provider or config.DEFAULT_PROVIDER,
            "workflows": _applicable_workflows(session, project),
            "autounverify": keys_svc.autounverify_enabled(project),
            "can_write": _can_write(request),
            "export_notice": notice,
            "live_runs": live_runs,
            "queued_runs": _queued_run_pills(session, project),
            "err": qp.get("err"),
            "ok": qp.get("ok"),
        },
    )
    if notice:
        resp.delete_cookie(_EXPORT_NOTICE_COOKIE, path=_notice_path(pid))
    if had_run_cookie:
        resp.delete_cookie(_RUNS_COOKIE, path=_notice_path(pid))
    return resp


@router.post("/projects/{pid}/keys/add")
def keys_add(
    pid: int,
    request: Request,
    name: str = Form(...),
    namespace: str = Form(""),
    context: str = Form(""),
    description: str = Form(""),
    base_value: str = Form(""),
    is_plural: str = Form(""),
    tags: str = Form(""),
    char_limit: str = Form(""),
    session: Session = Depends(get_session),
):
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return blocked
    if connected_projects.is_connected(project):
        return _back(pid, request, err=connected_projects.UPLOAD_REFUSED)
    plural = str(is_plural).strip() in ("1", "on", "true", "yes")
    try:
        limit = int(char_limit) if str(char_limit).strip() else None
    except (TypeError, ValueError):
        return _back(pid, request, err="char_limit must be a whole number.")
    try:
        key = keys_svc.create_key(
            session,
            project,
            name,
            namespace=namespace,
            context=context,
            description=description,
            is_plural=plural,
            char_limit=limit,
            tags=[t.strip() for t in tags.split(",") if t.strip()] or None,
            base_value=({"other": base_value} if plural else base_value)
            if base_value.strip()
            else None,
            # ONE string, so on a plural key it fills `other` only -- the CLI's rule,
            # verbatim (live-QA K5).
            base_fill_missing=not plural,
            actor=_actor(request),
        )
    except keys_svc.KeyServiceError as exc:
        return _back(pid, request, err=str(exc))
    msg = f"Added key {key.name}."
    # SAME RULE, SAME SENTENCE AS THE CLI (live-QA K5): a base value on a plural key
    # fills `other` only, so the remaining CLDR forms still need their own source text.
    if plural and base_value.strip():
        missing = [
            c
            for c, v in keys_svc.base_values(session, key).items()
            if c and c != "other" and not (v or "").strip()
        ]
        if missing:
            msg += (
                " The base value filled the 'other' form only; the {} form(s) "
                "still need their own source text.".format(", ".join(missing))
            )
    return _back(pid, request, ok=msg)


@router.post("/projects/{pid}/keys/import")
async def keys_import(
    pid: int,
    request: Request,
    file: UploadFile = File(...),
    locale: str = Form(""),
    fmt: str = Form(""),
    namespace: str = Form(""),
    cleanup: str = Form(""),
    confirm_cleanup: str = Form(""),
    session: Session = Depends(get_session),
):
    """Import one key file. CLEANUP (replace mode) SOFT-DELETES every live key in the
    namespace the file does not mention, so it is gated behind a typed confirmation
    exactly like the other destructive verbs — a mis-clicked checkbox must not be able
    to tombstone a catalogue."""
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return blocked
    if connected_projects.is_connected(project):
        return _back(pid, request, err=connected_projects.UPLOAD_REFUSED)
    want_cleanup = str(cleanup).strip() in ("1", "on", "true", "yes")
    if want_cleanup and str(confirm_cleanup).strip().upper() != "DELETE":
        return _back(
            pid,
            request,
            err="Cleanup deletes keys the file does not mention. Type "
            "DELETE in the confirmation box to run it.",
        )
    data = await deps.read_capped_upload(file, config.MAX_UPLOAD_BYTES)
    if data is None:
        return _back(pid, request, err="That file is larger than the upload cap.")
    ns = namespace.strip()
    try:
        # #599: the file parse runs on a bounded worker, never on the event loop.
        report = await deps.offload_blocking(
            key_files.import_keys,
            session,
            project,
            file.filename or "upload",
            data,
            fmt=fmt,
            locale=locale,
            mode=key_files.MODE_CLEANUP if want_cleanup else key_files.MODE_MERGE,
            namespace=(key_files.NS_FROM_KEY if ns == "@from-key" else (ns if ns else None)),
            actor=_actor(request),
            ip=_ip(request),
            include_hidden=_sees_hidden_keys(request),
        )
    except deps.WorkerBusy as exc:
        return _back(pid, request, err=str(exc))
    except keys_svc.KeyServiceError as exc:
        # Every honest refusal the seam raises — a bad locale, cleanup on a
        # translation file, an unmarked file under NS_FROM_KEY, an unparseable
        # payload — is surfaced VERBATIM. They all name their own fix.
        return _back(pid, request, err=str(exc))
    except Exception:
        _logger.exception("Key import failed for %r", file.filename)
        session.rollback()
        return _back(pid, request, err="That file could not be read.")
    msg = "Imported %s: %d created, %d updated, %d unchanged, %d deleted, %d error(s)." % (  # noqa: UP031
        report["filename"],
        report["created"],
        report["updated"],
        report["unchanged"],
        report["deleted"],
        len(report["errors"]),
    )
    resp = _back(pid, request, ok=msg)
    # THE NOTES, NOT JUST THE COUNTS (fix-round F4). "3 unchanged" is exactly what a
    # silently skipped write looks like; the note is the thing that says WHICH key and
    # WHY. They go in the one-shot cookie rather than the flash because `_back` puts
    # its flash in a QUERY STRING and there is no room for this there.
    #
    # NAMES, NOT A SENTENCE EACH (final round, blocker 2). Twenty sentences at ~150
    # characters overran the cookie cap by a factor of three, so 40 skipped keys were
    # NAMED 7 of -- while `capabilities/keys.md` promised the grid names them. The
    # service renders the same facts as a bounded name list with an explicit `+N more`.
    _set_export_notice(resp, pid, key_files.import_notices(report), label="Last import")
    return resp


@router.get("/projects/{pid}/keys/export")
def keys_export(
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
    """Download one locale's file.

    TWO SHAPES, and the surface says which is which (re-review R2): a NAMESPACE-scoped
    export is the BUILD file — bare key names, no marker. A WHOLE-PROJECT export is the
    TRANSPORT file — every name qualified `namespace::name` and stamped with the marker
    `NS_FROM_KEY` requires on the way back in. Which one you get follows the request,
    never the data.

    `include_string_images=1` (Batch 14 Phase 1.5) turns the download into a ZIP BUNDLE:
    the key file PLUS each exported key's ready in-context images under `images/` plus a
    `manifest.json` linking key to image path. Off (the default) is byte-identical to
    today. Ignored (plain file) when the image feature is off, so the shape still follows
    a request that can actually be served."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return blocked
    rep: list = []
    # H1 (gate round 2): `include_string_images` is read from the RAW query string, never a
    # declared FastAPI param, so it stays OUT of /openapi.json and therefore out of the
    # OpenAPI-derived UTCP tool manual. It is an operator-only download control (web + /api);
    # agents (MCP + the UTCP manual) have no bulk-pixel export tool by design (SPEC section 8).
    _isi = request.query_params.get("include_string_images", "")
    want_images = str(_isi).strip() in ("1", "on", "true") and key_images.enabled()
    ekeys: list = [] if want_images else None
    try:
        data = key_files.export_locale(
            session,
            project,
            locale,
            fmt=fmt,
            namespace=(namespace if namespace.strip() else None),
            empty_as=empty_as,
            # K9: KEY-LEVEL status filter. A key ships whole iff a
            # per-locale value carries this status, else it is omitted whole. Empty
            # string means no filter -- the default export is unchanged.
            status=(status.strip() or None),
            # ONE NAME ACROSS THE THREE SURFACES (review B18): the grid's checkbox,
            # `?archived=1` on /api and `keys export --archived` all spell it
            # `archived` now. This form used to post `include_archived`, so the same
            # question had two names depending on which surface you were on -- and a
            # caller copying one into the other silently got the DEFAULT answer.
            include_archived=str(archived).strip() in ("1", "on", "true"),
            # Keys hidden from translators stay out of a file a translator downloads.
            include_hidden=_sees_hidden_keys(request),
            report=rep,
            exported_keys=ekeys,
        )
    except export.ExportEmptyTargetsHeld as exc:
        # Batch 15 T2 (§9.2/§9.6): the TM-only empty gate held this key export. Send the
        # operator to the empties review + approve surface (never a source-filled file).
        _loc = locale.strip() or project.source_locale
        return _back(
            pid,
            request,
            err=(f"{exc} Review + approve at /projects/{pid}/keys/empties?locale={_loc}."),
        )
    except keys_svc.KeyServiceError as exc:
        return _back(pid, request, err=str(exc))
    loc = locale.strip() or project.source_locale
    stem = namespace.strip() or "project"
    # SANITIZED: `namespace` and `locale` are raw query parameters landing in a
    # response header, which is the F1 cell again and reachable by a plain READER.
    fname = key_files.download_filename(stem, loc, key_files.export_ext(fmt))
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
            return _back(pid, request, err=str(exc))
        fname = img_info["download_name"]  # shares the SANITISED key-file base (H1)
        media_type = "application/zip"
    audit.log(
        actor=_actor(request),
        action="keys.export",
        # BOTH guard outcomes, not just substitutions (fix-round F9): a TRANSPORT
        # export's kept-damaged values reached the headers, the cookie and the
        # CLI and left this row empty. The image-bundle option is named so an
        # audit reader sees when pixels left in an export (LAW-IMG-8 lifecycle).
        detail=key_files.audit_detail(rep)
        + (
            f" [include_string_images: {img_info.get('image_count', 0)} image(s)]"
            if want_images
            else ""
        ),
        target="project %d %s" % (project.id, loc),  # noqa: UP031
        ip=_ip(request),
        customer_id=audit.customer_for_project(project.id),
    )
    if want_images and img_info.get("image_ids"):
        # Per-export pixel-pull row (H2): the raw-serve route audits every human pull of
        # customer pixels; a bundle is ONE such pull of N images, recorded attributably by
        # image id under the same action (ONE row, not N -- the detail column carries the ids;
        # the HMAC chain is per-row, so N rows per export would multiply chain writes N-fold).
        audit.log(
            actor=_actor(request),
            action="key_image.raw",
            target="project %d %s" % (project.id, loc),  # noqa: UP031
            detail="export bundle image ids: " + ",".join(str(i) for i in img_info["image_ids"]),
            ip=_ip(request),
            customer_id=audit.customer_for_project(project.id),
        )
    headers = {
        "Content-Disposition": f'attachment; filename="{fname}"',
        "X-Content-Type-Options": "nosniff",
    }
    if want_images:
        # SAME bundle-fact headers as the /api route (L3): image count/bytes so a CLI/consumer
        # reads them off the web ZIP too, and a real Content-Length on the stream (L2).
        headers["X-Key-Image-Count"] = str(img_info.get("image_count", 0))
        headers["X-Key-Image-Bytes"] = str(img_info.get("image_bytes", 0))
        if img_info.get("bundle_bytes") is not None:
            headers["Content-Length"] = str(img_info["bundle_bytes"])
    headers.update(key_files.export_headers(rep))
    if bundle is not None:
        # STREAM the ZIP from the spooled temp file (M2): the archive is never re-loaded whole
        # into memory to hand to a Response.
        resp = StreamingResponse(
            key_images.iter_bundle(bundle), media_type=media_type, headers=headers
        )
    else:
        resp = Response(content=data, media_type=media_type, headers=headers)
    # A DOWNLOAD HAS NO PAGE TO WRITE ON (live-QA K2). The response body IS the file, so
    # the browser stays on the grid and there is nowhere to render a warning -- which is
    # exactly how five German strings shipped as English with nothing said anywhere. The
    # facts ride out in a short-lived cookie and the grid renders them the next time it
    # is drawn, then clears it. Not a session, not state: one notice, read once.
    _set_export_notice(resp, pid, key_files.export_notices(rep))
    return resp


# --- TM-only delivery empty gate: KEY delivery-review panel (Batch 15 T2, §9.6, N1) ---
@router.get("/projects/{pid}/keys/empties")
def keys_empties(
    pid: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    """The KEY delivery-review panel: the actual untranslated key names + Approve / Revoke.
    Web+CLI parity. 404 when the keys feature is off (layer-2)."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return blocked
    loc = (locale or "").strip() or project.source_locale
    # M1: a REVIEW surface -- compute the decision at the WIDEST universe (matching the
    # key_empties list it shows and what approve_keys will cover), so an archived-only empty
    # never reads "decision=pass" while the key is listed.
    # #540: a caller below manager neither sees hidden keys listed nor has them counted.
    see_hidden = _sees_hidden_keys(request)
    decision = tm_delivery.empty_gate_decision(
        session, project=project, locale=loc, include_archived=True, include_hidden=see_hidden
    )
    empties = tm_delivery.key_empties(session, project, loc, include_hidden=see_hidden)
    row = tm_delivery._key_approval_row(session, project.id, loc)
    live_ref = tm_delivery.active_run_ref(session, project_id=project.id, locale=loc)
    return templates.TemplateResponse(
        request,
        "delivery_empties.html",
        {
            "request": request,
            "mode": "keys",
            "doc": None,
            "project_id": project.id,
            # a11y T5: the language of the untranslated source text the panel lists.
            "source_locale": project.source_locale,
            "locale": loc,
            "decision": decision,
            "empties": empties,
            "approved": bool(row is not None and row.status == "approved"),
            # #105 (key mode): same trap, same seam -- a run over the project's key documents
            # is still filling these values. Round-2 HIGH-1: an Advanced-Workflow keys mission
            # on this project counts too, and the panel names which tier is running.
            "live_run_id": live_ref[1] if live_ref else None,
            "live_run_label": tm_delivery.run_ref_label(live_ref),
            "approve_action": f"/projects/{pid}/keys/deliver-approve?locale={loc}",
            "revoke_action": f"/projects/{pid}/keys/deliver-revoke?locale={loc}",
            "back_url": f"/projects/{pid}/keys",
            "can_write": _can_write(request),
        },
    )


@router.post("/projects/{pid}/keys/deliver-approve")
def keys_deliver_approve(
    pid: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    """Approve delivering (project, locale)'s empty keys. can_write; 404 when off."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return blocked
    blocked = _write_gate(request)
    if blocked:
        return blocked
    loc = (locale or "").strip() or project.source_locale
    try:
        tm_delivery.approve_keys(session, project, loc, approved_by=_actor(request))
    except tm_delivery.LocaleNotDeliverable as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/projects/{pid}/keys/empties?locale={loc}&saved=1", status_code=303)


@router.post("/projects/{pid}/keys/deliver-revoke")
def keys_deliver_revoke(
    pid: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    """Revoke a prior empty-delivery approval for (project, locale). can_write; 404 off."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return blocked
    blocked = _write_gate(request)
    if blocked:
        return blocked
    loc = (locale or "").strip() or project.source_locale
    try:
        tm_delivery.revoke_keys(session, project, loc, approved_by=_actor(request))
    except tm_delivery.LocaleNotDeliverable as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/projects/{pid}/keys/empties?locale={loc}&saved=1", status_code=303)


@router.post("/projects/{pid}/keys/translate")
def keys_translate(
    pid: int,
    request: Request,
    workflow_id: int = Form(...),
    locale: str = Form(...),
    session: Session = Depends(get_session),
):
    """ "Translate" for one target locale, or for ALL of them (`locale=all`, spec
    section 6) — funnelled into the EXISTING workflow-run seam against this project's
    internal namespace documents, so run views, SSE, LQA, spend safety and write-back
    behave exactly as they do for a document.

    TWO WAVE-B OBLIGATIONS LIVE HERE (spec section 5):

      * THE ACTOR IS WRAPPED AROUND THE LAUNCH. `orchestrator._run_in_background`
        starts a RAW thread, which inherits the launching thread's actor — but the
        launching thread here is one of anyio's POOL workers, whose actor is "" or
        another request's leftover. Entering `actor_scope` around the launch is what
        makes the run's write-backs attributable at all.
      * DELETED KEYS ARE NOT IN SCOPE. Their value rows were parked out of the
        namespace documents at `delete_key` (`keys.trash_document`), which is why a
        `--cleanup` import followed by this button cannot spend provider budget on
        strings the customer just deleted. Structural, not a filter this route
        applies and a future caller forgets.
    """
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return blocked
    from app.kernel import content as content_svc
    from app.kernel import workflows as wf_service
    from app.web.workflows import _wf_or_404

    _wf, blocked = _wf_or_404(request, session, workflow_id)
    if blocked:
        return blocked

    try:
        outcome = keys_svc.launch_translation(
            session,
            project,
            workflow_id,
            locale,
            actor=_actor(request),
            ip=security.client_ip(request),
            include_hidden=_sees_hidden_keys(request),
        )
    except (
        keys_svc.KeyServiceError,
        keys_svc.TranslationEngineError,
        keys_svc.KeyBudgetError,
        content_svc.ProjectArchivedError,
        wf_service.WorkflowArchivedError,
    ) as exc:
        return _back(pid, request, err=str(exc))
    from app.models import WorkflowRun

    targets = keys_svc.active_locales(session, project.id) if locale == "all" else [locale]
    started = outcome["started"]
    restarted = [(locale, 0, rid) for rid in outcome["restarted"]]
    busy = [(session.get(WorkflowRun, rid).target_locale, rid) for rid in outcome["busy"]]
    paused = [(session.get(WorkflowRun, rid).target_locale, rid) for rid in outcome["paused"]]
    skipped = outcome["skipped"]
    queued = [(r["locale"], r["document_name"], r["run_id"]) for r in outcome["queued"]]
    queue_incomplete = outcome.get("queue_incomplete", [])
    launched = outcome["launched"]
    # GitHub #213 part 2 (fix round 1): pairs REFUSED because a run of a DIFFERENT
    # workflow already holds that (document, locale). The service reported them and this
    # route dropped them -- a probe with one conflicting pair answered "Nothing to
    # translate into de-DE yet, add keys first.", false on every count. They render as
    # the row's OWN reason sentence, which names the running run and both cancel paths,
    # so the web copy cannot drift from the shared constant.
    conflicts = outcome.get("conflicts") or []
    conflict_lines = [
        "%s (%s): %s" % (_r["locale"], _r["document_name"], _r["reason"])  # noqa: UP031
        for _r in conflicts
    ]
    # `queued` counts as "something happened" (gate A round 3, HIGH-1): a wholly-queued
    # fan-out on a busy install has created real run rows, and answering "Nothing to
    # translate, add keys first" would be a plain falsehood about work that is about to run.
    # `conflicts` counts too (fix round 1): a wholly-CONFLICTED fan-out refused real
    # pairs onto real running runs, and "add keys first" is the same class of lie.
    if not launched and not busy and not paused and not queued and not queue_incomplete:
        # Compose the conflict sentence(s) and the "add keys first" line into ONE err
        # string (gate M3 shape): a mixed fan-out must never tell the operator
        # "add keys first" while silently swallowing an actionable refusal. The
        # "add keys first" line is only ever allowed for locales that REALLY have no
        # content (`skipped`), never for a conflicted one.
        parts = list(conflict_lines)
        if skipped:
            parts.append(f"Nothing to translate into {', '.join(skipped)} yet, add keys first.")
        elif not conflicts:
            parts.append(f"Nothing to translate into {locale} yet, add keys first.")
        return _back(pid, request, err=" ".join(parts))
    submitted = launched + [rid for _loc, _doc, rid in queued]
    if (
        len(submitted) == 1
        and not skipped
        and not busy
        and not paused
        and not restarted
        and not queue_incomplete
        and not conflicts
    ):
        return RedirectResponse("/runs/%d?live=1" % submitted[0], status_code=303)  # noqa: UP031
    parts = []
    if started:
        # NAMES THE LOCALES, and never says "every locale" unless every locale really
        # did start (review F8d) -- the old line claimed the full fan-out even when
        # two of three locales had in fact been left to an existing run.
        parts.append(
            "Started %d run(s)%s."  # noqa: UP031
            % (
                len(started),
                " into every locale"
                if (locale == "all" and len(started) == len(targets))
                else (f" into {locale}" if locale != "all" else ""),
            )
        )
    # One sentence per locale for everything that is NOT a plain launch: each of these
    # is a thing the operator has to know and cannot see anywhere else on this page.
    for loc_name, old_id, new_id in restarted:
        parts.append("%s: stale run #%d superseded, restarted as #%d." % (loc_name, old_id, new_id))  # noqa: UP031
    for loc_name, run_id in busy:
        parts.append("%s is already running (run #%d)." % (loc_name, run_id))  # noqa: UP031
    for loc_name, run_id in paused:
        # A PAUSED run is not "running" -- it is waiting on a person, and saying
        # "already running" sends the operator away to wait for something that will
        # never move on its own (review F8c).
        parts.append("%s is paused for review (run #%d)." % (loc_name, run_id))  # noqa: UP031
    for row in outcome.get("superseded", []):
        if row.get("superseded"):
            parts.append(
                f"{row['locale']}: stale run #{row['superseded']} superseded, "
                f"{'started' if row['run_id'] in launched else 'queued'} as #{row['run_id']}."
            )
    if queued:
        # The per-launch `queued` status (round 2 HIGH-1; round 3 HIGH-1). This install had
        # no free run slot for these pairs right now; each one is a real run row (named here
        # by id, so the operator can open it, watch it or cancel it) that starts on its own
        # as earlier runs finish. Say it plainly, and name them, so the operator does not
        # read a partial "Started N run(s)" as a silent loss.
        _names = ", ".join(f"{_loc}: {_dn} (#{_rid})" for _loc, _dn, _rid in queued[:6]) + (
            f", +{len(queued) - 6} more" if len(queued) > 6 else ""
        )
        parts.append(
            f"Queued {len(queued)} ({_names}): they start automatically as the "
            f"running work finishes, and can be cancelled from their run page."
        )
    for _row in queue_incomplete:
        # A pair whose run row could not be WRITTEN (write contention). Never silent:
        # the operator is told which locale/document was not queued, and why.
        parts.append(
            "{}: could not be queued ({}). Retry it.".format(
                _row.get("locale"), _row.get("reason") or "write contention"
            )
        )
    if skipped:
        parts.append("Nothing to translate into {}.".format(", ".join(skipped)))
    # #213 part 2 (fix round 1): a PARTIAL fan-out names the pairs it refused onto
    # another workflow's running run, next to what started. Never silent, never a
    # bare "Started N run(s)" over a locale that will not move until the rival is cancelled.
    parts.extend(conflict_lines)
    # Hand the grid every run this POST is responsible for -- freshly launched,
    # restarted, and the ones it merely attached to -- so the live progress strip
    # (GitHub #47) shows what is happening to each locale instead of leaving the
    # operator on a static page.
    # Queued rows render statically; only admitted work may occupy an SSE worker.
    tracking = launched + [r for _l, r in busy] + [r for _l, r in paused]
    # ...and OWN THE TRUNCATION (review NEW-4). The strip is capped at
    # _RUNS_COOKIE_MAX rows because each one holds a browser connection open; past
    # that the runs are unaffected but they are NOT being watched, and a strip that
    # silently showed six of nine would be a smaller version of the same
    # "you were not told" defect this whole change exists to fix.
    untracked = len(tracking) - _RUNS_COOKIE_MAX
    if untracked > 0:
        parts.append("(+%d more not tracked below.)" % untracked)  # noqa: UP031
    resp = _back(pid, request, ok=" ".join(parts))
    _set_run_cookie(resp, pid, tracking)
    return resp


# --- per-key actions (declared AFTER the literal paths above) ------------------
def _grid_key(request, session, pid, key_id, include_deleted=False):
    """(project, key, blocked) for a per-key web action."""
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return None, None, blocked
    key = session.get(StringKey, key_id)
    if (
        key is None
        or key.project_id != project.id
        or (key.deleted_at is not None and not include_deleted)
        or (key.is_hidden and not _sees_hidden_keys(request))
    ):
        return None, None, _error_page(request, 404, "Not found.")
    return project, key, None


@router.post("/projects/{pid}/keys/autounverify")
def keys_set_autounverify(
    pid: int, request: Request, on: str = Form(""), session: Session = Depends(get_session)
):
    """The per-project auto-unverify override (spec section 8)."""
    blocked = _flag_off_block(request) or _write_gate(request)
    if blocked:
        return blocked
    project, blocked = _key_project(request, session, pid)
    if blocked:
        return blocked
    raw = str(on).strip().lower()
    keys_svc.set_autounverify(
        project,
        None if raw == "inherit" else raw in ("1", "on", "true", "yes"),
        actor=_actor(request),
        ip=_ip(request),
    )
    return _back(pid, request, ok="Saved auto-unverify setting.")


# ============================================================================
# /api — the key group (spec section 5)
# ============================================================================
@api_router.get("/api/projects/{pid}/keys")
def api_keys_list(
    pid: int,
    request: Request,
    namespace: str = "",
    tag: str = "",
    q: str = "",
    status: str = "",
    untranslated_in: str = "",
    archived: str = "",
    include_deleted: str = "",
    cursor: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session),
):
    blocked = _api_flag_block()
    if blocked:
        return blocked
    ident, blocked = _api_gate(request)
    if blocked:
        return blocked
    project, denied = _api_project(session, ident, pid)
    if denied:
        return denied
    # A NONSENSE LIMIT IS A REFUSAL, NOT A GUESS (review B18). `page_keys` clamps into
    # [1, 1000] because a service seam must never crash on junk -- but a CALLER that
    # asked for -5 got a silent page of ONE and a `has_more` that would walk it through
    # the catalogue an item at a time, with nothing anywhere saying its request was
    # wrong. Clamping is right for the seam and wrong for the API.
    bad = _limit_refusal(limit, _KEY_PAGE_MAX)
    if bad:
        return bad
    try:
        page = keys_svc.page_keys(
            session,
            project.id,
            cursor=cursor,
            limit=limit,
            namespace=(namespace if "namespace" in request.query_params else None),
            tag=(tag or None),
            q=(q or None),
            status=(status if status in KEY_STATUSES else None),
            untranslated_in=(untranslated_in or None),
            # ARCHIVED KEYS ARE EXCLUDED BY DEFAULT on all three surfaces -- the web
            # grid's checkbox, `keys list --archived` and this flag all default the
            # same way, matching `export_locale`'s own default (review B13: archived
            # means "retired from the product"). A default that differed per surface
            # would make the same question give three answers.
            include_archived=str(archived) in ("1", "true"),
            include_deleted=str(include_deleted) in ("1", "true"),
            include_hidden=_api_sees_hidden_keys(ident),
            **_api_scope(ident),
        )
    except keys_svc.NotAKeyProjectError:
        # The service's own guard, answered with the resolver's own 404 (see the
        # grid route's note): unreachable behind `_api_project`, non-enumerable if
        # it ever is reached.
        return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "project_id": project.id,
            # The project's SOURCE locale rides along because a key-scoped caller has no
            # other way to learn it: /api/projects/{id} is DOCS-scoped, and "is this
            # locale the base one?" decides whether a write is a base edit (auto-unverify)
            # or a translation. Without it the `--remote` CLI arm would have to hold a
            # docs-scoped token too, which is exactly the scope-widening the separate
            # "keys" grant exists to avoid.
            "source_locale": project.source_locale,
            "target_locales": keys_svc.active_locales(session, project.id),
            "keys": keys_svc.key_views(session, page["keys"], project_id=project.id),
            "next_cursor": page["next_cursor"],
            "has_more": page["has_more"],
        }
    )
