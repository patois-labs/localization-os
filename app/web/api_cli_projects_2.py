"""/api routes: projects 2

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64
import json
import logging
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.db import engine
from app.kernel import (
    audit,
    content,
    glossary,
    tm,
)
from app.kernel import keys as keys_svc
from app.models import (
    Document,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _ai_block,
    _b64,
    _cust_of,
    _gate,
    _glossary_in_scope,
    _glossary_window,
    _int_id,
    _json,
    _manage_block,
    _project_in_org,
    _resolve_user_ref,
    _write_block,
    glossary_import_error_response,
)
from app.web.api_cli_projects_1 import _tm_source_visible
from app.web.streaming import StreamingResponse

router = APIRouter()


@router.get("/api/projects/{pid}/export")
def api_projects_export(
    pid: int,
    request: Request,
    locales: str = "",
    all_locales: str = "",
    zip: str = "",
    allow_held: str = "",
    sdl_return_package: str = "",
):
    """The project delivery bundle as RAW BYTES (mirrors `project export` + the web
    download). `sdl_return_package=1` (or a package GUID) returns ONE Trados return
    package (.sdlrpx) built from the documents imported from a Trados project package;
    left-out (held) documents are counted in the X-Export-Skipped header.
    A read (no write-role gate) — org+customer bound via _project_in_org.
    `locales` is a comma/space list (omit or all_locales=1 for every target); a single
    named locale returns that one file unless zip=1 forces a ZIP. A tag-quarantined
    single-file export is a 409; a partially-substituted bundle rides its facts in the
    X-Export-Substituted headers exactly like the document download. `batch=<id>` (a ZIP
    batch upload's id, listed under `zip_batches` by GET /api/projects/{pid}/documents)
    returns only that upload's documents as a ZIP laid out `<locale>/<original path>`."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from fastapi.responses import Response

    from app.kernel import delivery
    from app.kernel.export import (
        ExportEmptyTargetsHeld,
        ExportHeldLocale,
        TagExportQuarantine,
    )

    req = [x for x in (locales or "").replace(",", " ").split() if x]
    if str(all_locales) in ("1", "true", "yes"):
        req = []
    want = req or None
    as_zip = str(zip) in ("1", "true", "yes") or not (len(req) == 1)  # noqa: SIM201
    # Batch 13: ?batch=<id> exports one ZIP upload with its own folder structure.
    batch = (request.query_params.get("batch") or "").strip()[:64] or None
    allow_held_on = str(allow_held) in ("1", "true", "yes")
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # #593 gate: allow_held ships the source-filled file; a read-only key may not.
        _ovr = deps.export_override_block(ident.get("user"), requested=allow_held_on)
        if _ovr is not None:
            return _ovr
        if sdl_return_package:
            guid = "" if str(sdl_return_package) in ("1", "true", "yes") else sdl_return_package
            try:
                data, default_name, skipped = delivery.export_sdl_return_package(
                    s, pid, guid or None, allow_held=allow_held_on
                )
            except ValueError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
            from app.kernel import content as _content

            headers = {"Content-Disposition": _content.content_disposition_attachment(default_name)}
            if skipped:
                headers["X-Export-Skipped"] = str(len(skipped))
            return Response(content=data, media_type="application/zip", headers=headers)
        rep: list = []
        try:
            data, default_name, ctype, skipped = delivery.export_project(
                s,
                pid,
                locales=want,
                zip=as_zip,
                report=rep,
                allow_held=allow_held_on,
                batch=batch,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except TagExportQuarantine as exc:
            return JSONResponse(
                {"ok": False, "error": f"export quarantined for review: {exc}"},
                status_code=409,
            )
        except ExportEmptyTargetsHeld as exc:
            # Batch 15 T2 (§9.2): the single-file fast path hit a TM-only empty-held doc.
            # 409; the way out is the AUDITED approval, NOT allow_held (which cannot bypass).
            return JSONResponse(
                {
                    "ok": False,
                    "empty_held": True,
                    "error": str(exc),
                    "locale": exc.locale,
                    "empties": exc.empties,
                    "approve_url": (
                        f"/api/documents/{exc.doc_id}/deliver-approve?locale={exc.locale}"
                    ),
                },
                status_code=409,
            )
        except ExportHeldLocale as exc:
            # chip task_9c665111: the single-file fast path hit a held locale (no
            # committed translation) -> no clean file to stream. 409, never source.
            # M1: name the way out (allow_held=1 ships the source-filled file).
            _q = "&".join(([f"locales={exc.locale}"] if exc.locale else []) + ["allow_held=1"])
            return JSONResponse(
                {
                    "ok": False,
                    "held": True,
                    "error": str(exc),
                    "locale": exc.locale,
                    "export_anyway_url": f"/api/projects/{pid}/export?{_q}",
                },
                status_code=409,
            )
        except (FileNotFoundError, OSError):
            # BUG-A6-1: a document's on-disk blob is missing/unreadable — a clean 409
            # beats a raw 500 (parity with the /api document-download route).
            return JSONResponse(
                {
                    "ok": False,
                    "error": "document content unavailable "
                    "(a stored file is missing or unreadable).",
                },
                status_code=409,
            )
        subs = sum(r["count"] for r in rep)
        audit.log(
            actor=ident["actor"],
            action="project.export",
            target=f"project {pid}",
            detail=f"locales={','.join(req) or 'all'}"
            + (f" skipped={len(skipped)}" if skipped else "")
            + (f" substituted={subs} docs={len(rep)}" if rep else ""),
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(pid),
        )
    # `default_name` (the ZIP name OR single-file fast-path name) derives from
    # delivery._safe()/file_name_for(), which preserve Unicode word chars -> a non-latin-1
    # project/doc name reaches this bare filename="..." and 500s on the wire (probe-proven CJK
    # project export). Build via the RFC-6266 helper (parity with the web export route).
    headers = {
        "Content-Disposition": content.content_disposition_attachment(default_name),
        "X-Content-Type-Options": "nosniff",
    }
    # The SILENT-substitution channel (BUG-04 parity): the bundle bytes look
    # well-formed even when a unit shipped as source text, so the facts ride in
    # headers the --remote CLI arm rebuilds its report from, and quarantined docs
    # are named too rather than dropped in silence.
    if skipped:
        headers["X-Export-Skipped"] = str(len(skipped))
    if rep:
        headers["X-Export-Substituted"] = str(subs)
        headers["X-Export-Substituted-Docs"] = str(len(rep))
    return Response(content=data, media_type=ctype, headers=headers)


@router.get("/api/projects/{pid}/tm-sources")
def api_project_tm_sources_list(pid: int, request: Request):
    """List a project's CURRENTLY-EFFECTIVE TM sources (mirrors `project tm-sources
    list`). A read — org+customer bound via _project_in_org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import project_sources

    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        rows = project_sources.list_tm_sources(s, pid)
        payload = [
            {
                "source_project_id": r["source_project_id"],
                "name": r["name"],
                "entry_count": r["entry_count"],
                "pairs": r["pairs"],
            }
            for r in rows
        ]
    return JSONResponse({"ok": True, "project_id": pid, "tm_sources": payload})


@router.post("/api/projects/{pid}/tm-sources")
async def api_project_tm_sources_add(pid: int, request: Request):
    """Attach another project's TM as a read-only source (mirrors `project tm-sources
    add`). can_tune_ai. BOTH the target `pid` AND the body's `source_project_id` are
    funnelled through the org ring: the target via _project_in_org (404), the source
    via _tm_source_visible (404 for a foreign-org source, and for a bound caller a
    foreign-CUSTOMER source — the B1/H1/H2 within-org oracle). The service then makes
    the final same-customer-or-house call (a staff caller's cross-customer source is a
    clean 400, never an attach)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # attaching language memory -> can_tune_ai
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import project_sources

    src_pid = _int_id(body.get("source_project_id"))
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if src_pid is None or _tm_source_visible(s, src_pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            view = project_sources.add_tm_source(s, pid, src_pid, created_by=ident["actor"])
        except project_sources.AttachError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        audit.log(
            actor=ident["actor"],
            action="project.tm_source.add",
            target=f"project {pid} <- project {view['source_project_id']}",
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(pid),
        )
    return JSONResponse(
        {
            "ok": True,
            "project_id": pid,
            "source_project_id": view["source_project_id"],
            "entry_count": view["entry_count"],
        }
    )


@router.post("/api/projects/{pid}/tm-sources/remove")
async def api_project_tm_sources_remove(pid: int, request: Request):
    """Detach a TM source (mirrors `project tm-sources remove`). can_tune_ai. Reversible
    re-attach — nothing was copied, so removal restores the previous matching exactly.
    Org+customer bound via _project_in_org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import project_sources

    src_pid = _int_id(body.get("source_project_id"))
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        removed = project_sources.remove_tm_source(s, pid, src_pid)
        if removed:
            audit.log(
                actor=ident["actor"],
                action="project.tm_source.remove",
                target=f"project {pid} -x- project {src_pid}",
                ip=deps._ip(request),
                customer_id=audit.customer_for_project(pid),
            )
    return JSONResponse(
        {"ok": True, "project_id": pid, "source_project_id": src_pid, "removed": removed}
    )


@router.get("/api/projects/{pid}/glossaries")
def api_project_glossaries_list(pid: int, request: Request):
    """List a project's pinned module glossaries (mirrors `project glossaries list`). A
    read — org+customer bound via _project_in_org. `active` is False for a pin whose
    glossary is still a DRAFT (pinned but contributing nothing until approved)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import project_sources

    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        rows = project_sources.list_glossary_pins(s, pid)
        payload = [
            {
                "glossary_id": r["glossary_id"],
                "name": r["name"],
                "status": r["status"],
                "active": r["active"],
                "term_count": r["term_count"],
                "pairs": r["pairs"],
            }
            for r in rows
        ]
    return JSONResponse({"ok": True, "project_id": pid, "glossaries": payload})


@router.post("/api/projects/{pid}/glossaries")
async def api_project_glossaries_add(pid: int, request: Request):
    """Pin a module glossary to a project (mirrors `project glossaries add`). can_tune_ai.
    The target `pid` funnels through _project_in_org (404); the body's `glossary_id`
    funnels through _glossary_in_scope — the SAME read-authz seam the module routes use,
    so a foreign-org (or, for a bound caller, foreign-customer) glossary 404s and cannot
    be enumerated. The service then enforces the project ring (a staff caller's
    cross-customer glossary is a clean 400)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # pinning terminology -> can_tune_ai
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import project_sources

    gid = _int_id(body.get("glossary_id"))
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if gid is None or _glossary_in_scope(s, gid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            view = project_sources.add_glossary_pin(
                s,
                pid,
                gid,
                created_by=ident["actor"],
                org_id=org_id,
                bound_customer_id=_cust_of(ident),
            )
        except project_sources.AttachError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        audit.log(
            actor=ident["actor"],
            action="project.glossary.attach",
            target=f"project {pid} <- glossary {view['glossary_id']}",
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(pid),
        )
    return JSONResponse(
        {
            "ok": True,
            "project_id": pid,
            "glossary_id": view["glossary_id"],
            "active": view["active"],
            "term_count": view["term_count"],
        }
    )


@router.post("/api/projects/{pid}/glossaries/remove")
async def api_project_glossaries_remove(pid: int, request: Request):
    """Unpin a glossary (mirrors `project glossaries remove`). can_tune_ai. Reversible
    re-pin — the glossary itself is untouched. Org+customer bound via _project_in_org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import project_sources

    gid = _int_id(body.get("glossary_id"))
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        removed = project_sources.remove_glossary_pin(s, pid, gid)
        if removed:
            audit.log(
                actor=ident["actor"],
                action="project.glossary.detach",
                target=f"project {pid} -x- glossary {gid}",
                ip=deps._ip(request),
                customer_id=audit.customer_for_project(pid),
            )
    return JSONResponse({"ok": True, "project_id": pid, "glossary_id": gid, "removed": removed})


@router.get("/api/projects/{pid}/documents")
def api_documents_list(pid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        docs = s.exec(
            select(Document)
            .where(
                Document.project_id == pid,
                # SPEC-key-based-projects 2.5: internal key documents stay invisible.
                Document.fmt != keys_svc.KEY_DOC_FMT,
            )
            .order_by(Document.created_at.desc())
        ).all()
        # #563: the derived lifecycle, the same value `doc list` prints in-process.
        from app.kernel import lifecycle

        doc_status = lifecycle.document_status_map(s, [d.id for d in docs])
        payload = [
            {
                "id": d.id,
                "filename": d.filename,
                "fmt": d.fmt,
                "mode": d.mode,
                "status": doc_status.get(d.id, "new"),
            }
            for d in docs
        ]
        # Batch 13: ZIP batch uploads in this project (id, archive, document count); pass
        # an id as `batch=` to GET /api/projects/{pid}/export for the per-language ZIP.
        from app.kernel import delivery

        batches = delivery.zip_batches(s, pid, documents=docs)
    return JSONResponse({"ok": True, "documents": payload, "zip_batches": batches})


# ============================================================================
# Translation Memory (import / export)
# ============================================================================
@router.post("/api/projects/{pid}/tm/import")
async def api_tm_import(pid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/tm/import is a write action
    if blocked:
        return blocked
    body = await _json(request)
    source, target = str(body.get("source") or ""), str(body.get("target") or "")
    data = _b64(body.get("tmx_b64"))
    if data is None or not source or not target:
        return JSONResponse(
            {"ok": False, "error": "tmx_b64, source and target are required."}, status_code=400
        )
    # #567: only a JSON boolean true acknowledges a placeholder loss (a string is refused,
    # like /api/segments/{sid}/approve; anything else is no acknowledgement = fail closed).
    ack_raw = body.get("acknowledge_placeholder_loss", False)
    if not isinstance(ack_raw, bool):
        return JSONResponse(
            {"ok": False, "error": "acknowledge_placeholder_loss must be a JSON boolean."},
            status_code=400,
        )
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            res = await deps.offload_parse(
                tm.import_tmx, s, pid, data, source, target, acknowledge_placeholder_loss=ack_raw
            )
        except deps.WorkerBusy as exc:
            return deps.busy_response(str(exc))
        except tm.TmxImportIncomplete as exc:
            # #615: some chunks committed before the failure; rows_written of rows_total are in.
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"could not import TMX: {exc}",
                    "partial": True,
                    "rows_written": exc.rows_written,
                    "rows_total": exc.rows_total,
                },
                status_code=500,
            )
        except SQLAlchemyError:
            # Review gate (#615): a database error's text carries the SQL and the bound row
            # values (TM text); it goes to the server log, never into the reply. Nothing was
            # stored (a failure after a committed chunk is TmxImportIncomplete above).
            logging.getLogger(__name__).warning("tm import: database error", exc_info=True)
            return JSONResponse(
                {
                    "ok": False,
                    "error": "could not import TMX: a database error stopped the import; "
                    "nothing was stored. Try again; if it repeats, check the server log.",
                },
                status_code=500,
            )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"could not import TMX: {exc}"}, status_code=400
            )
    # QA BUG-03: echo the locales the rows were STORED under (the service seam coerces
    # a region/case variant of the project's pair and refuses an unrelated one), never
    # the requested ones — a caller that trusted `source`/`target` was told about a
    # bucket the read path can never reach.
    return JSONResponse(
        {
            "ok": True,
            "imported": res["imported"],
            "updated": res["updated"],
            "skipped": res["skipped"],
            "skipped_refusal": res.get("skipped_refusal", 0),
            "skipped_integrity": res.get("skipped_integrity", 0),
            "skipped_by_code": res.get("skipped_by_code", {}),
            "skipped_rows": res.get("skipped_rows", []),
            "acknowledged_placeholder_loss": res.get("acknowledged_placeholder_loss", 0),
            "source": res.get("source_locale", source),
            "target": res.get("target_locale", target),
            "requested_source": source,
            "requested_target": target,
            "locales_normalized": bool(res.get("locales_normalized")),
            "write_chunks": res.get("write_chunks", 0),
            "concurrent_deleted": res.get("concurrent_deleted", 0),
            # A Trados .sdltm (SQLite) goes through the same import; these say what was read.
            "format": res.get("format", "tmx"),
            "skipped_unreadable": res.get("skipped_unreadable", 0),
            **(
                {
                    "sdltm_languages": res.get("sdltm_languages"),
                    "sdltm_units": res.get("sdltm_units", 0),
                }
                if res.get("format") == "sdltm"
                else {}
            ),
        }
    )


def _json_b64_stream(head: dict, pieces):
    """The `GET .../tm/export` JSON body as a byte stream: `head`'s fields, then `tmx_b64` (the
    pieces base64-encoded on 3-byte boundaries, so the concatenation is one valid base64
    string), then `bytes` (the TMX length, known only at the end). Memory stays one page."""
    yield (json.dumps(head)[:-1] + ', "tmx_b64": "').encode("ascii")
    carry = b""
    total = 0
    for piece in pieces:
        total += len(piece)
        carry += piece
        cut = len(carry) - len(carry) % 3
        if cut:
            yield base64.b64encode(carry[:cut])
            carry = carry[cut:]
    if carry:
        yield base64.b64encode(carry)
    yield ('", "bytes": %d}' % total).encode("ascii")  # noqa: UP031


@router.get("/api/projects/{pid}/tm/export")
def api_tm_export(
    pid: int,
    request: Request,
    source: str | None = None,
    target: str | None = None,
    raw: bool = False,
):
    """Export the project's TM as TMX. Default: one JSON object carrying `tmx_b64` (what the
    remote CLI and MCP read). `raw=true` (#615): the TMX itself, STREAMED as it is read
    (application/x-tmx+xml, constant memory, starts at once); the effective locales and the
    unit count ride in X-TM-Source, X-TM-Target and X-TM-Count response headers."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    if not source or not source.strip():
        return JSONResponse({"ok": False, "error": "source is required"}, status_code=400)
    if not target or not target.strip():
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # QA BUG-03 read-back leg: echo the EFFECTIVE locales the rows were queried
        # under (export coerces a region/case variant to the project's stored spelling,
        # the same way import does), never just the requested ones — so a caller that
        # exported `en`/`es` into an en-US -> es-419 project is told which bucket it
        # actually reached instead of silently getting an empty TMX (FINDING 5). `count`
        # is the REAL row count from the service (review H2: a substring scan of the
        # bytes missed meta-stamped `<tu creationtool=...>` human_approved rows).
        if raw:
            eff_source, eff_target, count, pieces = tm.export_tmx_stream(s, pid, source, target)
            # The locales are echoed from the caller's own query when the project has no such
            # bucket: percent-encode so a non-latin-1 or CR/LF value cannot 500 the response
            # or inject a header (a valid BCP-47 tag is unchanged).
            eff_source = urllib.parse.quote(eff_source, safe="-_.~")
            eff_target = urllib.parse.quote(eff_target, safe="-_.~")
            return StreamingResponse(
                pieces,
                media_type="application/x-tmx+xml",
                headers={
                    "X-TM-Source": eff_source,
                    "X-TM-Target": eff_target,
                    "X-TM-Count": str(count),
                },
            )
        # #615: the JSON default is STREAMED too (same fields, same base64 payload), so a
        # 100k-unit TM is never held as ORM rows + one lxml tree + one base64 copy at once.
        # `bytes` comes last because it is only known once the stream ends.
        eff_source, eff_target, count, pieces = tm.export_tmx_stream(s, pid, source, target)
        head = {
            "ok": True,
            "source": eff_source,
            "target": eff_target,
            "requested_source": source,
            "requested_target": target,
            "locales_normalized": (eff_source, eff_target) != (source, target),
            "count": count,
        }
    return StreamingResponse(_json_b64_stream(head, pieces), media_type="application/json")


# ============================================================================
# Glossary (import / add / list)
# ============================================================================
@router.post("/api/projects/{pid}/glossary/import")
async def api_glossary_import(pid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/glossary/import is a write action
    if blocked:
        return blocked
    body = await _json(request)
    source, target = str(body.get("source") or ""), str(body.get("target") or "")
    # `csv_b64` is the (historically named) BYTES field and now carries either a
    # glossary CSV or a native TBX termbase. The optional `filename` hint is what a
    # remote CLI sends so the .tbx extension rule holds end-to-end; without it the
    # server sniffs the content, so an older client still lands in the right parser.
    data = _b64(body.get("csv_b64"))
    filename = str(body.get("filename") or "")
    if data is None or not source or not target:
        return JSONResponse(
            {"ok": False, "error": "csv_b64, source and target are required."}, status_code=400
        )
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            res = await deps.offload_parse(
                glossary.import_bytes, s, pid, data, source, target, filename=filename
            )
        except deps.WorkerBusy as exc:
            return deps.busy_response(str(exc))
        except Exception as exc:
            return glossary_import_error_response(exc)
    return JSONResponse({"ok": True, **res})


@router.post("/api/projects/{pid}/glossary")
async def api_glossary_add(pid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/glossary/add is a write action
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        project = _project_in_org(s, pid, org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        src = str(body.get("source") or "").strip() or project.source_locale
        entry = glossary.add_term(
            s,
            project.id,
            str(body.get("source_term") or ""),
            target_term=str(body.get("target_term") or ""),
            source_locale=src,
            target_locale=str(body.get("target") or "").strip(),
            dnt=bool(body.get("dnt")),
        )
        if entry is None:
            return JSONResponse({"ok": False, "error": "source term is blank."}, status_code=400)
        out = {
            "ok": True,
            "project_id": pid,
            "source_term": entry.source_term,
            "target_term": entry.target_term,
            "dnt": entry.dnt,
        }
    audit.log(
        actor=ident["actor"],
        action="glossary.add",
        target=f"project {pid} '{out['source_term']}'",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.get("/api/projects/{pid}/glossary")
def api_glossary_list(pid: int, request: Request):
    """List a project's glossary entries. BOUNDED (AU-7): optional `limit` (default and
    hard cap 1000) + `offset`; the response carries `truncated`, `limit` and `offset`."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    limit, offset, err = _glossary_window(request)
    if err:
        return err
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        page = glossary.window_entries(
            s, project_id=pid, cursor=request.query_params.get("cursor"), limit=limit, offset=offset
        )
        window, truncated = page["entries"], page["has_more"]
        payload = [
            {
                "id": it["entry"].id,
                "source_term": it["entry"].source_term,
                "target_term": it["entry"].target_term,
                "source_locale": it["entry"].source_locale,
                "target_locale": it["entry"].target_locale,
                "dnt": it["entry"].dnt,
                "senses": len(it["senses"]),
            }
            for it in window
        ]
    return JSONResponse(
        {
            "ok": True,
            "entries": payload,
            "truncated": truncated,
            "limit": limit,
            "offset": offset,
            "next_cursor": page["next_cursor"],
        }
    )


@router.get("/api/projects/{pid}/glossary/export")
def api_glossary_export(pid: int, request: Request, format: str = "csv"):
    """The LEGACY per-project glossary as RAW BYTES (mirrors `glossary export` + the web
    download; the data-safety chip closed the `--remote` silent-local hole by making the
    CLI arm require_local). A read — org+customer bound via _project_in_org. `?format=tbx`
    returns a native TBX termbase; anything else is the CSV. Distinct from the dedicated
    module-glossary export (GET /api/glossaries/{gid}/export)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from fastapi.responses import Response

    fmt = (
        glossary.TBX_FORMAT
        if (format or "").strip().lower() == glossary.TBX_FORMAT
        else glossary.CSV_FORMAT
    )
    with Session(engine) as s:
        if _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        data = glossary.export_bytes(s, pid, fmt=fmt)
    ext = "tbx" if fmt == glossary.TBX_FORMAT else "csv"
    media = "application/xml" if fmt == glossary.TBX_FORMAT else "text/csv; charset=utf-8"
    headers = {
        "Content-Disposition": 'attachment; filename="project%d_glossary.%s"' % (pid, ext),  # noqa: UP031
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=data, media_type=media, headers=headers)


# --- members ---------------------------------------------------------------
@router.post("/api/projects/{pid}/members/add")
async def api_member_add(request: Request, pid: int):
    """Grant a user visibility into a project (mirrors `member add`). can_manage. The
    project is ringed via _project_in_org; the member user is ringed in-org. Body:
    user (id/email)."""
    from app.kernel import access, audit

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = _project_in_org(s, _int_id(pid), org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        mb = _manage_block(ident)  # RING BEFORE ROLE: foreign pid 404, in-scope 403
        if mb:
            return mb
        body = await _json(request)
        u = _resolve_user_ref(s, body.get("user"), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        ok, msg = access.add_member(s, project.id, u.email, added_by=ident.get("actor", ""))
        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        audit.log(
            actor=ident.get("actor", ""),
            action="member.add",
            target=u.email,
            detail="project %d" % project.id,  # noqa: UP031
        )
        return JSONResponse({"ok": True, "project_id": project.id, "user": u.email, "message": msg})


@router.post("/api/projects/{pid}/members/remove")
async def api_member_remove(request: Request, pid: int):
    """Revoke a membership (mirrors `member remove`). can_manage, ringed. Body: user
    (id/email)."""
    from app.kernel import access, audit

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = _project_in_org(s, _int_id(pid), org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        mb = _manage_block(ident)
        if mb:
            return mb
        body = await _json(request)
        u = _resolve_user_ref(s, body.get("user"), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        removed = access.remove_member(s, project.id, u.id)
        if not removed:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "%s was not a member of project %d." % (u.email, project.id),  # noqa: UP031
                },
                status_code=400,
            )
        audit.log(
            actor=ident.get("actor", ""),
            action="member.remove",
            target=u.email,
            detail="project %d" % project.id,  # noqa: UP031
        )
        return JSONResponse(
            {"ok": True, "project_id": project.id, "user": u.email, "removed": True}
        )


@router.get("/api/projects/{pid}/members")
def api_member_list(request: Request, pid: int):
    """List a project's members (mirrors `member list`). can_manage, ringed."""
    from app.kernel import access

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = _project_in_org(s, _int_id(pid), org_id, ident)
        if project is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        mb = _manage_block(ident)
        if mb:
            return mb
        payload = [
            {
                "user_id": user.id,
                "email": user.email,
                "role": user.role,
                "added_by": member.added_by or "",
            }
            for member, user in access.list_members(s, project.id)
        ]
    return JSONResponse({"ok": True, "project_id": project.id, "members": payload})
