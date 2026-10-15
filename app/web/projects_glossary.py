"""projects: glossary

Documented in: docs/architecture/content-and-projects.md
"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session

from app import config
from app.db import get_session
from app.kernel import (
    audit,
    content,
    delivery,
    export,
    glossary,
    glossary_tbx,
    security,
    tm,
)
from app.kernel import (
    keys as keys_svc,
)
from app.models import (
    Project,
)
from app.web import page_guard
from app.web.deps import (
    WorkerBusy,
    _actor_email,
    _error_page,
    _ip,
    _missing_block,
    _missing_or,
    _visibility_block,
    _wants_dialog,
    _wants_html,
    offload_parse,
    read_capped_upload,
)
from app.web.form_errors import redirect_with_error
from app.web.streaming import StreamingResponse

router = APIRouter()


# --- delivery / download (P1, item 3) ---
@router.get("/projects/{project_id}/export")
def project_export(project_id: int, request: Request, session: Session = Depends(get_session)):
    """Download a project's translated files. Accepts `locales` as EITHER repeated
    query params (the checkbox form: ?locales=fr-FR&locales=de-DE) OR a single
    comma/space list (?locales=fr-FR,de-DE); blank = all. A single locale streams
    the one file; multiple (or blank/all) stream a ZIP with per-locale subfolders +
    name_locale.ext. Same visibility gate as the sibling export routes; audited."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    # Read every repeated `locales` param, then split each on commas/space so both
    # the checkbox form and the quick-links / CLI-style comma list work.
    req = []
    for raw in request.query_params.getlist("locales"):
        req.extend(x for x in (raw or "").replace(",", " ").split() if x)
    want = req or None
    # One locale, explicitly requested -> single file; else a ZIP.
    as_zip = not (len(req) == 1)  # noqa: SIM201
    sdl_guid = request.query_params.get("sdl_return_package", "")
    if sdl_guid:
        # Batch 13: one Trados return package (.sdlrpx) for the package documents.
        try:
            data, filename, skipped = delivery.export_sdl_return_package(
                session, project_id, None if sdl_guid in ("1", "true", "yes") else sdl_guid
            )
        except ValueError as exc:
            from urllib.parse import quote

            page_guard.attest(request, "err", str(exc))
            return RedirectResponse(
                f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303
            )
        audit.log(
            actor=_actor_email(request),
            action="project.export",
            target=f"project {project_id}",
            detail="sdl_return_package" + (f" skipped={len(skipped)}" if skipped else ""),
            ip=_ip(request),
            customer_id=project.customer_id,
        )
        headers = {"Content-Disposition": content.content_disposition_attachment(filename)}
        if skipped:
            headers["X-Export-Skipped"] = str(len(skipped))
        return Response(content=data, media_type="application/zip", headers=headers)
    # Batch 13: ?batch=<id> limits the bundle to one ZIP upload, laid out with its own
    # folders under one folder per language (always a ZIP).
    batch = (request.query_params.get("batch") or "").strip()[:64] or None
    # BUG-04: the bundle's SUBSTITUTION channel. A quarantine is loud (a document is
    # missing); a source-policy substitution is silent (the file is there and one
    # segment is quietly English), so it rides the headers exactly like the per-document
    # download's X-Export-Substituted — same names, so a client reads one convention.
    rep: list = []
    try:
        data, filename, ctype, skipped = delivery.export_project(
            session, project_id, locales=want, zip=as_zip, report=rep, batch=batch
        )
    except ValueError as exc:
        from urllib.parse import quote

        # B13: a key project's refusal NAMES the key surface, so it is surfaced
        # verbatim rather than folded into the generic `err=export` flash.
        if keys_svc.is_key_project(project):
            # A GET: record the service's own sentence for the page guard (it names the
            # key surface and the project id, never a value from this request).
            page_guard.attest(request, "err", str(exc))
            return RedirectResponse(
                f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303
            )
        return RedirectResponse(f"/projects/{project_id}?err=export", status_code=303)
    except (
        export.TagExportQuarantine,
        export.ExportHeldLocale,
        export.ExportEmptyTargetsHeld,
    ) as exc:
        # AI Tagging 1.6 / chip task_9c665111 / T2: the single-file fast path was refused by
        # a delivery guard (tag quarantine, held locale or quality-gate hold, TM-only empty
        # hold) -- no clean file to stream, so never a 500 or a source-only download.
        # #593: a BROWSER gets the document's own refusal page (what is wrong, which
        # segments, the buttons) by being sent to the document export that re-asks the same
        # gate; every other caller keeps the flash redirect it always had.
        _doc_id = getattr(exc, "doc_id", None)
        _loc = getattr(exc, "locale", None)
        if (_wants_html(request) or _wants_dialog(request)) and _doc_id and _loc:
            from urllib.parse import quote

            return RedirectResponse(
                f"/documents/{int(_doc_id)}/export?locale={quote(str(_loc), safe='')}",
                status_code=303,
            )
        if isinstance(exc, export.TagExportQuarantine):
            return RedirectResponse(
                f"/projects/{project_id}?err=export_quarantined", status_code=303
            )
        return RedirectResponse(f"/projects/{project_id}?err=export_held", status_code=303)
    subs = sum(r["count"] for r in rep)
    audit.log(
        actor=_actor_email(request),
        action="project.export",
        target=f"project {project_id}",
        detail=f"locales={','.join(req) or 'all'}"
        + (f" batch={batch}" if batch else "")
        + (f" skipped={len(skipped)}" if skipped else "")
        + (f" substituted={subs} docs={len(rep)}" if rep else ""),
        ip=_ip(request),
        customer_id=project.customer_id,
    )
    # `filename` (the ZIP name `<proj_slug>_translated.zip` OR the single-file fast-path name)
    # derives from delivery._safe(project.name)/file_name_for(doc.filename), which preserve
    # Unicode word chars -> a non-latin-1 project/doc name reaches this bare filename="..." and
    # 500s on the wire (probe-proven CJK project export). Build via the RFC-6266 helper.
    headers = {"Content-Disposition": content.content_disposition_attachment(filename)}
    if skipped:
        # Honest, machine-readable notice that some docs were withheld (also in the ZIP
        # _QUARANTINED.txt manifest); a header keeps the streamed file itself clean.
        headers["X-Export-Quarantined"] = str(len(skipped))
    if rep:
        headers["X-Export-Substituted"] = str(subs)
        # ASCII BY CONSTRUCTION, and deliberately doc_id-keyed rather than
        # filename-keyed: `substitution_detail` emits only kind/unit/index and a locale
        # is a BCP-47 tag, but an UPLOADED FILENAME can hold any codepoint and a header
        # value must be latin-1 — a Japanese filename would 500 the download. The ZIP's
        # _SUBSTITUTED.txt (UTF-8) is where the human-readable filenames live. Capped so
        # a 200-document bundle cannot emit a header a proxy refuses (measured at 12 KB
        # on the prompt-tap route: a real 502).
        # ...and the cap DECLARES ITSELF (review). A silent `[:900]` slice can cut a
        # record mid-token, so a client reading the header sees a short, well-formed-
        # looking list and cannot tell it is missing documents — the same silent-omission
        # failure this whole channel exists to close (prompt-tap states truncation in its
        # headers for the same reason). The full count stays on X-Export-Substituted.
        _detail = "; ".join(
            "doc {} [{}] {}".format(r["doc_id"], r["locale"], r["detail"]) for r in rep
        )
        if len(_detail) > 900:
            _detail = _detail[:880].rstrip("; ") + "; TRUNCATED docs=%d" % len(rep)  # noqa: UP031
        headers["X-Export-Substituted-Detail"] = _detail
    return Response(content=data, media_type=ctype, headers=headers)


# --- translation memory ---
@router.post("/projects/{project_id}/tm/import")
async def tm_import(
    project_id: int,
    request: Request,
    source_locale: str = Form(...),
    target_locale: str = Form(...),
    file: UploadFile = File(...),
    acknowledge_placeholder_loss: str = Form(""),
    session: Session = Depends(get_session),
):
    blocked = _visibility_block(request, session, project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Importing TM requires a write role.")
    # Bounded read: abort past the cap so a hostile/huge upload can never be
    # fully buffered into memory (shared helper in app/web/deps.py).
    data = await read_capped_upload(file, config.MAX_UPLOAD_BYTES)
    if data is None:
        return RedirectResponse(f"/projects/{project_id}?err=toolarge_tmx", status_code=303)
    try:
        res = await offload_parse(
            tm.import_tmx,
            session,
            project_id,
            data,
            source_locale,
            target_locale,
            acknowledge_placeholder_loss=acknowledge_placeholder_loss in ("1", "true", "yes"),
        )
    except tm.LocaleMismatchError:
        # QA BUG-03: a pair this project does not translate would be stored in a
        # bucket the read path can never reach. Distinct from a malformed-file error
        # so the operator is told WHICH thing went wrong (CLI/api say the same).
        return RedirectResponse(f"/projects/{project_id}?err=tmx_locales", status_code=303)
    except tm.SdltmError as exc:
        # A Trados .sdltm that cannot be read (not a TM, wrong language pair): say why.
        return redirect_with_error(f"/projects/{project_id}", str(exc))
    except tm.TmxImportIncomplete as exc:
        # #615: a big import that failed after some chunks committed. Say so, and that a
        # retry of the same file finishes it (the message carries the row counts).
        return redirect_with_error(f"/projects/{project_id}", str(exc))
    except WorkerBusy:
        raise  # the app-wide handler answers a retryable 503 (nothing was imported)
    except Exception:
        return RedirectResponse(f"/projects/{project_id}?err=tmx", status_code=303)
    # #605: name the withheld units by their position in the file. Only integer row numbers
    # travel in the URL, never the TM text itself.
    withheld_rows = ",".join(str(int(r["row"])) for r in (res.get("skipped_rows") or [])[:20])
    n_deleted = int(res.get("concurrent_deleted") or 0)  # #615: rows deleted mid-import
    return RedirectResponse(
        f"/projects/{project_id}?tm_imported={res['imported']}"
        f"&tm_updated={res['updated']}&tm_skipped={res['skipped']}"
        f"&tm_skipped_refusal={res.get('skipped_refusal', 0)}"
        f"&tm_skipped_integrity={res.get('skipped_integrity', 0)}"
        + (
            f"&tm_skipped_unreadable={int(res['skipped_unreadable'])}"
            if res.get("skipped_unreadable")
            else ""
        )
        + (f"&tm_withheld_rows={withheld_rows}" if withheld_rows else "")
        + f"&tm_ack_loss={res.get('acknowledged_placeholder_loss', 0)}"
        f"&tm_normalized={1 if res.get('locales_normalized') else 0}"
        + (f"&tm_deleted={n_deleted}" if n_deleted else ""),
        status_code=303,
    )


@router.get("/projects/{project_id}/tm/export")
def tm_export(
    project_id: int,
    request: Request,
    source: str,
    target: str,
    session: Session = Depends(get_session),
):
    blocked = _visibility_block(request, session, project_id)
    if blocked:
        return blocked
    # #615: streamed in id-ordered pages (constant memory) instead of one in-memory TMX.
    _src, _tgt, _count, pieces = tm.export_tmx_stream(session, project_id, source, target)
    # HIGH-1: `source`/`target` are RAW query params -> a bare filename="..." here is a
    # Content-Disposition breakout (a `"` closes the quoted-string and injects a parameter /
    # a spoofed filename*), and a non-latin-1 value 500s on the wire (probe-proven). Build the
    # header via content.content_disposition_attachment (RFC-6266: neutralised ASCII fallback +
    # percent-encoded filename*), the same seam item 6 used for the document download.
    return StreamingResponse(
        pieces,
        media_type="application/x-tmx+xml",
        headers={
            "Content-Disposition": content.content_disposition_attachment(
                f"tm_{source}_{target}.tmx"
            )
        },
    )


# --- attach an already-established TM / glossary (feedback item 5) -------------
# Four thin routes over app/services/project_sources. Each carries the SAME two gates
# every other project mutation on this page carries — the visibility block, then
# can_write — because attaching is a write to how the project translates, not a read.
# All scoping (org/customer ring, source visibility) happens in the service, so the
# web and CLI surfaces cannot drift on it.
def _sources_write_block(request, session, project_id, what: str):
    """Shared preamble: resolve + visibility-block + can_write-gate. Returns
    (project, response) — `response` non-None means stop and return it."""
    project = session.get(Project, project_id)
    if not project:
        return None, _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return None, blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return None, _error_page(request, 403, f"{what} requires a write role.")
    return project, None


# --- glossary ---
@router.post("/projects/{project_id}/glossary/import")
async def glossary_import(
    project_id: int,
    request: Request,
    source_locale: str = Form(...),
    target_locale: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    blocked = _visibility_block(request, session, project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Importing a glossary requires a write role.")
    # Bounded read: abort past the cap so a hostile/huge upload can never be
    # fully buffered into memory (shared helper in app/web/deps.py).
    data = await read_capped_upload(file, config.MAX_UPLOAD_BYTES)
    if data is None:
        return RedirectResponse(f"/projects/{project_id}?err=toolarge_glossary", status_code=303)
    # CSV or native TBX — the format is chosen from the uploaded FILENAME (.tbx),
    # with a content sniff as the fallback (glossary.detect_format).
    try:
        res = await offload_parse(
            glossary.import_bytes,
            session,
            project_id,
            data,
            source_locale,
            target_locale,
            filename=(file.filename or ""),
        )
    except WorkerBusy:
        raise  # the app-wide handler answers a retryable 503 (nothing was imported)
    except (glossary.GlossaryCsvHeaderError, glossary.GlossaryImportIncomplete) as exc:
        # #615: say which columns the file needs (and which it had) instead of "failed"; and
        # for a big import that stopped after some chunks were stored, how far it got and that
        # importing the same file again finishes it (the message carries the counts, never the
        # database error text).
        return redirect_with_error(f"/projects/{project_id}", str(exc))
    except Exception:
        return RedirectResponse(f"/projects/{project_id}?err=glossary", status_code=303)
    n_deleted = int(res.get("concurrent_deleted") or 0)  # #615: entries deleted mid-import
    return RedirectResponse(
        f"/projects/{project_id}?gl_entries={res['entries']}&gl_senses={res['senses']}"
        f"&gl_skipped={res.get('skipped', 0)}&gl_changed={res.get('changed', 0)}"
        + (f"&gl_deleted={n_deleted}" if n_deleted else "")
        + glossary_tbx.report_query(res),
        status_code=303,
    )


@router.get("/projects/{project_id}/glossary/export")
def glossary_export(
    project_id: int,
    request: Request,
    format: str = "csv",
    session: Session = Depends(get_session),
):
    """Download the project's legacy glossary as CSV (SPEC-context-metadata phase 4)
    or, with `?format=tbx`, as a native TBX v3 termbase.

    Round-trips /glossary/import; the four TBX-ish meta columns are always present
    in the CSV (blank when a term has no meta). CSV stays the LOSSLESS format —
    TBX has no home for our context-dependent senses (DNT round-trips via a
    private termNote extension when a term has a target; a target-less DNT term
    still exports source-only and re-imports as a skipped entry either way). Web+
    CLI parity with `glossary export <id> <out.csv|.tbx>`. An unknown format value
    falls back to CSV rather than erroring."""
    blocked = _visibility_block(request, session, project_id)
    if blocked:
        return blocked
    fmt = (
        glossary.TBX_FORMAT
        if (format or "").strip().lower() == glossary.TBX_FORMAT
        else glossary.CSV_FORMAT
    )
    data = glossary.export_bytes(session, project_id, fmt=fmt)
    is_tbx = fmt == glossary.TBX_FORMAT
    return Response(
        content=data,
        media_type=(glossary_tbx.TBX_MEDIA_TYPE if is_tbx else "text/csv"),
        headers={
            "Content-Disposition": f'attachment; filename="glossary_{project_id}'
            f'{".tbx" if is_tbx else ".csv"}"'
        },
    )
