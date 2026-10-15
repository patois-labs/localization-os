"""documents: tm-only delivery empty gate: document delivery-review panel (batch 15 t2, §9.6)

Documented in: docs/architecture/translation-pipeline.md
"""

import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.filters.base import InputLimitError
from app.filters.xml_its import XmlFilterError
from app.kernel import (
    audit,
    connected_projects,
    content,
    export,
    lqa,
    reports,
    security,
    tm_delivery,
)
from app.kernel.ingest import NotADocumentProject, UnsupportedFormat, ingest_document
from app.models import (
    Project,
    Segment,
    TransMode,
)
from app.web.deps import (
    WorkerBusy,
    _actor_email,
    _admin_block,
    _error_page,
    _ip,
    _missing_block,
    _missing_or,
    _visibility_block,
    _visible_doc,
    _wants_dialog,
    _wants_html,
    mime_for_fmt,
    offload_parse,
    read_capped_upload,
    templates,
)

_logger = logging.getLogger("loc_tms")


router = APIRouter()


# --- documents ---
# Gate (train-9): the ZIP batch result rides the redirect URL; keep it short enough to
# percent-encode under the request-line limit even for CJK member paths (9 bytes a char).
_ZIP_MSG_CHARS = 600


@router.post("/projects/{project_id}/documents")
async def upload_document(
    project_id: int,
    request: Request,
    mode: str = Form(TransMode.SEGMENTED),
    file: UploadFile = File(...),
    ctx_domain: str = Form(""),
    ctx_audience: str = Form(""),
    ctx_formality: str = Form(""),
    ctx_tone: str = Form(""),
    ctx_notes: str = Form(""),
    columns: str = Form(""),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    # Connected projects take their content from the source, never from uploads: the page
    # hides the form, and this refuses a hand-made POST too.
    if connected_projects.is_connected(project):
        return _error_page(request, 400, connected_projects.UPLOAD_REFUSED)
    # SPEC-context-metadata phase 1: assemble + validate the optional context section.
    from urllib.parse import quote

    from app.kernel import context_pkg

    ctx_raw = {
        k: v
        for k, v in (
            ("domain", ctx_domain),
            ("audience", ctx_audience),
            ("formality", ctx_formality),
            ("notes", ctx_notes),
        )
        if (v or "").strip()
    }
    if (ctx_tone or "").strip():
        ctx_raw["tone"] = [t.strip() for t in ctx_tone.split(",") if t.strip()]
    try:
        ctx_pkg = context_pkg.validate_and_sanitize(ctx_raw) or None
    except context_pkg.ContextError as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    # Bounded read: abort past the cap so a hostile/huge upload can never be
    # fully buffered into memory (shared helper in app/web/deps.py).
    data = await read_capped_upload(file, config.MAX_UPLOAD_BYTES)
    if data is None:
        return RedirectResponse(f"/projects/{project_id}?err=toolarge_doc", status_code=303)
    from app.filters.zip_batch import is_batch_upload

    if is_batch_upload(file.filename or ""):
        # Batch 13: a .zip is a BATCH, one document per supported member; the result
        # (created / skipped / failed members, each named) comes back on the project page.
        from app.kernel.ingest import ingest_zip_batch, zip_batch_summary

        try:
            result = await offload_parse(
                ingest_zip_batch,
                session,
                project,
                file.filename or "upload.zip",
                data,
                mode,
                created_by=_actor_email(request),
                context=ctx_pkg,
            )
        except InputLimitError as exc:
            session.rollback()
            return RedirectResponse(
                f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303
            )
        except WorkerBusy as exc:
            return RedirectResponse(
                f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303
            )
        except NotADocumentProject as exc:
            return RedirectResponse(
                f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303
            )
        except UnsupportedFormat:
            return RedirectResponse(f"/projects/{project_id}?err=format", status_code=303)
        if not result["documents"]:
            return RedirectResponse(
                f"/projects/{project_id}?err="
                + quote("No document was created. " + zip_batch_summary(result, _ZIP_MSG_CHARS)),
                status_code=303,
            )
        return RedirectResponse(
            f"/projects/{project_id}?zip_msg="
            + quote(zip_batch_summary(result, _ZIP_MSG_CHARS))
            + "#zip-batches",
            status_code=303,
        )
    try:
        # #224/#271: the parse runs in a worker thread, so a slow file never stalls the
        # event loop (every other user's requests) while it is read.
        doc = await offload_parse(
            ingest_document,
            session,
            project,
            file.filename or "upload",
            data,
            mode,
            created_by=_actor_email(request),
            context=ctx_pkg,
            columns=(columns or "").strip() or None,  # Batch 6: CSV/TSV/XLSX mapping
        )
    except InputLimitError as exc:
        # #224/#272: over a safety limit (zip bomb, too many PDF pages); nothing stored.
        session.rollback()
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    except WorkerBusy as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    except NotADocumentProject as exc:
        # A-F4: the upload card is not rendered on a key project, so this is a forged
        # or stale POST — answer with the seam's own message, which names the fix.
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    except UnsupportedFormat:
        return RedirectResponse(f"/projects/{project_id}?err=format", status_code=303)
    except XmlFilterError as exc:
        # Batch 7: unsafe or malformed XML/DITA/DocBook; the message names the reason.
        session.rollback()
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    except Exception:
        _logger.exception("Ingest failed for %r", file.filename)
        session.rollback()
        return RedirectResponse(f"/projects/{project_id}?err=parse", status_code=303)
    if doc.fmt == "sdlrpx":  # a Trados package: one document per target file
        return RedirectResponse(f"/projects/{project_id}#sdl-package", status_code=303)
    return RedirectResponse(f"/documents/{doc.id}", status_code=303)


_LQA_ROWS_PER_PAGE = 100


# --- LQA (MQM/DQF) ---
# The scorecard renders at most this many segment rows per page (issue #52: a 6,000-segment
# document rendered ~6 MB of HTML in one GET). Pagination bounds the response; the machine
# overall/gate is still computed across EVERY segment (structural-only, cheap).
@router.get("/documents/{doc_id}/lqa")
def lqa_view(
    doc_id: int,
    request: Request,
    locale: str | None = None,
    page: str | None = None,
    judge: str | None = None,
    session: Session = Depends(get_session),
):
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_block(request, session)
    project = session.get(Project, doc.project_id)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    # F10: coerce/clamp ?page like the other pagers — a junk value (`?page=abc`) must not
    # 422 the page. `page` is typed str|None so FastAPI never rejects it; parse leniently.
    try:
        page = int(page) if page is not None else 1
    except (TypeError, ValueError):
        page = 1
    all_segments = session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
    locales = sorted({s.target_locale for s in all_segments})
    selected = locale if locale in locales else (locales[0] if locales else None)
    # #52: the GET NEVER does live LLM-judge work — `llm=False` scores structural+rulebook
    # only (cheap, O(segs) with ZERO provider calls / spend). The LLM dimension runs ONLY on
    # the POST (Re-score & save) path. RE-SCORE SAFETY (§6.5): still persist=False — it never
    # reads or writes machine rows, so the Wave 2 "no stale machine row renders" property
    # holds. Persisted HUMAN findings (below) AND stored MACHINE findings (below) are read
    # separately and shown in their own blocks; they are never folded into `card`, so the
    # fresh machine score stays machine-only.
    card = (
        lqa.score_document(session, doc_id, selected, persist=False, llm=False)
        if selected
        else {
            "rows": [],
            "overall": 100,
            "gate": "auto_approved",
            "segments_scored": 0,
            "llm_judge": lqa.JUDGE_SKIPPED,
        }
    )
    # #52: PAGE the rendered rows so a large document cannot emit multi-MB HTML. The overall
    # score/gate above already reflect the WHOLE document; only the row TABLE is windowed.
    all_rows = card.get("rows", [])
    total_rows = len(all_rows)
    total_pages = max(1, (total_rows + _LQA_ROWS_PER_PAGE - 1) // _LQA_ROWS_PER_PAGE)
    page = max(1, min(page, total_pages))
    row_lo = (page - 1) * _LQA_ROWS_PER_PAGE
    rows_page = all_rows[row_lo : row_lo + _LQA_ROWS_PER_PAGE]
    # WAVE 3 §6.5: persisted, non-withdrawn reviewer-authored findings for this doc+locale,
    # via the ONE shared reader (reports.human_findings_for_document) every surface uses.
    human = (
        reports.human_findings_for_document(session, document_id=doc_id, target_locale=selected)
        if selected
        else []
    )
    human_by_seg: dict = {}
    for f in human:
        human_by_seg.setdefault(f.segment_id, []).append(f)
    # #52 point 2: surface the PERSISTED MACHINE findings — the workflow-run gate reality
    # (segment_id NULL) AND the last ad-hoc segment-level scoring — so the page cannot show a
    # clean 100 while the run that gated this document scored lower and logged findings the
    # qa report/Databricks export count.
    machine = (
        reports.machine_findings_for_document(session, document_id=doc_id, target_locale=selected)
        if selected
        else {"run": [], "adhoc": [], "run_total": 0, "adhoc_total": 0, "cap": 0}
    )
    # F3: the AUTHORITATIVE headline is the STORED scoring — never the fresh structural-only
    # recompute (which would show auto_approved over a doc whose saved scoring failed, the
    # 96-vs-100 contradiction #52 cites). It comes from the ONE shared helper (web + CLI):
    # the LATEST workflow LQA StepRun's DURABLE, threshold-correct gate (regime 'run'), else
    # the last ad-hoc scoring's score with NO derived gate (regime 'adhoc'), else nothing.
    headline = (
        reports.lqa_scorecard_headline(session, document_id=doc_id, target_locale=selected)
        if selected
        else {"has_scoring": False, "regime": None, "score": None, "gate": None}
    )
    stored_score = headline.get("score")
    stored_gate = headline.get("gate")
    stored_regime = headline.get("regime")
    has_scoring = bool(headline.get("has_scoring"))
    # F1: the POST redirected here with ?judge=unavailable when a dead judge held the
    # re-score (prior scoring preserved). Surface it as a banner the GET can actually render
    # (the recompute is always JUDGE_SKIPPED, so the card's own marker never fires on a GET).
    judge_unavailable = judge == "unavailable"
    me = security.session_user(request)
    return templates.TemplateResponse(
        request,
        "lqa.html",
        {
            "request": request,
            "doc": doc,
            "project": project,
            "locales": locales,
            "selected": selected,
            "card": card,
            "rows_page": rows_page,
            "page": page,
            "total_pages": total_pages,
            "total_rows": total_rows,
            "row_lo": row_lo,
            "per_page": _LQA_ROWS_PER_PAGE,
            "machine": machine,
            "stored_score": stored_score,
            "stored_gate": stored_gate,
            "stored_regime": stored_regime,
            "has_scoring": has_scoring,
            "judge_unavailable": judge_unavailable,
            # #425: under multi-org the install-wide rulebook text is never sent to the page
            # (another org's rules must not be readable here); the host reads it by CLI.
            "rulebook": (
                lqa.load_rulebook(project.source_locale, selected)
                if selected and not lqa.rulebook_host_only()
                else ""
            ),
            "dimensions": lqa.MQM_DIMENSIONS,
            "severities": list(lqa.PENALTY.keys()),
            "human_findings": human,
            "human_by_seg": human_by_seg,
            "can_log": (not security.auth_required()) or security.can_review(me),
            "can_manage": (not security.auth_required()) or security.can_manage(me),
            # GATE R1 MED-1: the rulebook POST is _admin_block, so the form must be gated on
            # the SAME predicate. can_manage is broader, so a manager (and previously any
            # reviewer) saw a Save button that answered 403. Offering an action the caller
            # cannot perform is its own defect, separate from the authorization fix.
            # #425: under multi-org the rulebook is host-operator only, so no editor at all.
            "can_edit_rulebook": (
                ((not security.auth_required()) or security.is_admin(me))
                and not lqa.rulebook_host_only()
            ),
            "rulebook_host_only": lqa.rulebook_host_only(),
            "me_actor": _actor_email(request),
        },
    )


@router.post("/documents/{doc_id}/lqa/run")
def lqa_run(
    doc_id: int, request: Request, locale: str = Form(...), session: Session = Depends(get_session)
):
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    card = lqa.score_document(session, doc_id, locale, persist=True)
    # F1: when a configured judge was UNAVAILABLE the scoring was ABORTED (prior findings
    # preserved, nothing written). Redirect with a banner param so the GET renders "LLM
    # judge unavailable — previous scoring preserved" (the GET's own recompute is always
    # structural-only, so its card marker never fires — the param carries the truth).
    suffix = "&judge=unavailable" if card.get("persisted") is False else ""
    return RedirectResponse(f"/documents/{doc_id}/lqa?locale={locale}{suffix}", status_code=303)


# --- reviewer-authored MQM findings (QA-2, SPEC-wave3-collaboration §6.5) --------- #
# These ride the CORE, UNFLAGGED LQA surface (§5.3 — a flag on "may a human write down
# what is wrong" would be a flag on the product's premise). RBAC is REVIEWER+ on every
# write, mirrored byte-for-byte on the /api twin; edit/withdraw additionally need own-row
# OR can_manage+. A refused attempt returns BEFORE the service call, so it writes no audit
# row (the project convention). The service (lqa.*_human_finding) enforces the SAME rules
# as defence in depth.
def _lqa_finding_error(request, exc):
    """Map the service's typed finding errors to a themed page (400 input / 403 rbac)."""
    if isinstance(exc, lqa.HumanFindingForbidden):
        return _error_page(request, 403, str(exc))
    return _error_page(request, 400, str(exc))


@router.post("/documents/{doc_id}/rulebook")
def rulebook_save(
    doc_id: int,
    request: Request,
    locale: str = Form(...),
    text: str = Form(""),
    session: Session = Depends(get_session),
):
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    project = session.get(Project, doc.project_id)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    # #425: the rulebook is one install-wide file per language pair, read by every org's
    # LQA, so under multi-org only the host operator may change it. After the ring checks
    # (a foreign document answers as before), before any write. No-op with the gate off.
    if lqa.rulebook_host_only():
        return _error_page(request, 403, lqa.RULEBOOK_HOST_ONLY)
    locales = set(
        session.exec(select(Segment.target_locale).where(Segment.document_id == doc_id)).all()
    )
    if locale not in locales:
        return _error_page(request, 400, "Locale is not on this document.")
    try:
        lqa.save_rulebook(project.source_locale, locale, text)
    except ValueError as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/documents/{doc_id}/lqa?locale={locale}", status_code=303)


# --- export (format-preserving round-trip) ---
# Issue #593: a delivery refusal is a JSON body for /api and scripts, but a person clicking
# "Export" in the browser must get a page, not raw JSON. A browser navigation sends
# `Accept: text/html`; every client that wants the JSON (curl, requests, fetch with
# application/json, the test clients' default `*/*`) keeps the exact body it always had.
_EXPORT_ITEMS_SHOWN = 10

_QUARANTINE_KIND_COPY = {
    "tokens": (
        "Formatting in a translated paragraph no longer matches the source",
        "An inline formatting tag (bold, italic, a link) differs from the source in the "
        "paragraphs below. Exporting would deliver a file with broken formatting, so the "
        "export is held.",
    ),
    "share_ceiling": (
        "Too much of the document would fall back to source text",
        "Replacing the damaged paragraphs with source text would cover more of the document "
        "than the allowed share, so the override is refused. Fix or re-translate the "
        "paragraphs below first.",
    ),
    "untranslated": (
        "The translated text is missing paragraphs",
        "The translation has fewer paragraphs than the source (or is blank). Exporting would "
        "silently put source-language text into the file, so the export is held.",
    ),
    "count": (
        "The translated paragraphs no longer line up with the source",
        "The paragraph structure of the translation does not match the source, so no "
        "paragraph can be trusted to sit in the right place. Exporting would deliver a "
        "scrambled file, so the export is held. Re-run the translation or edit the text.",
    ),
    "segments": (
        "Tag-damaged segments are quarantined",
        "Some translated segments lost their formatting tags and the install policy is to "
        "quarantine them rather than ship them.",
    ),
}


def _export_refused_page(
    request: Request,
    session: Session,
    doc,
    locale: str,
    *,
    status_code: int,
    headline: str,
    plain: str,
    why: str,
    error: str,
    items: list | None = None,
    items_heading: str = "",
    items_unit: str = "",
    items_total: int = 0,
    review_url: str = "",
    paragraphs_url: str = "",
    empties_url: str = "",
    export_anyway_url: str = "",
    anyway_label: str = "Export anyway",
    anyway_note: str = "",
    not_bypassable: bool = False,
):
    project = session.get(Project, doc.project_id)
    me = security.session_user(request)
    items = items or []
    can_override = (not security.auth_required()) or security.can_write(me)
    steps: list[str] = []
    if review_url:
        steps.append(
            "Click Open Post Editor and approve or edit each segment that is waiting for review."
        )
        steps.append("Come back to this page and export again. The hold clears by itself.")
    if paragraphs_url:
        steps.append("Click View paragraphs and fix, or revert to source, each damaged paragraph.")
        steps.append("Export again.")
    if empties_url:
        steps.append("Click Review empty targets, then approve delivering them as empty strings.")
        steps.append("Export again.")
    if export_anyway_url and can_override:
        steps.append("Or, if source text in those places is acceptable: " + anyway_label + ".")
    if not steps:
        steps.append("Fix the cause named above, then export again.")
    shown = {
        "headline": headline,
        "plain": plain,
        "why": why,
        "check_message": error,
        "steps": steps,
        "items": [
            {k: (str(v)[:160] if k in ("source", "target") else v) for k, v in it.items()}
            for it in items
        ],
        "items_heading": items_heading,
        "items_unit": items_unit,
        "items_total": items_total or len(items),
        "items_more": max(0, (items_total or len(items)) - len(items)),
        "review_url": review_url,
        "paragraphs_url": paragraphs_url,
        "empties_url": empties_url,
        "export_anyway_url": export_anyway_url if can_override else "",
        # The check offers an override but this role may not use it (the dialog says so).
        "override_refused": bool(export_anyway_url) and not can_override,
        "anyway_label": anyway_label,
        "anyway_note": anyway_note,
        "not_bypassable": not_bypassable,
        "can_override": can_override,
        "filename": doc.filename,
        "locale": locale,
    }
    if _wants_dialog(request):
        # The in-page dialog (export_dialog.js) asked for the refusal as data.
        return JSONResponse({"ok": False, "refusal": shown}, status_code=status_code)
    return templates.TemplateResponse(
        request,
        "export_refused.html",
        {
            "request": request,
            "doc": doc,
            "source_locale": project.source_locale if project else "",
            "export_anyway_url_offered": bool(export_anyway_url),
            **shown,
        },
        status_code=status_code,
    )


def _wants_ui(request: Request) -> bool:
    return _wants_html(request) or _wants_dialog(request)


@router.get("/documents/{doc_id}/export")
def export_document(
    doc_id: int,
    locale: str,
    request: Request,
    allow_source_fallback: int = 0,
    allow_held: int = 0,
    as_fmt: str = "",
    session: Session = Depends(get_session),
):
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    # #593 review: the two override flags ship source text where a translation is missing or
    # damaged. The refusal page and dialog only offer them to a write role, so the route
    # itself must agree: a read-only role that types the URL gets the refusal, never the
    # override (fail closed; the flags are ignored, not a 403, so the normal export still works).
    if (
        (allow_held or allow_source_fallback)
        and security.auth_required()
        and not security.can_write(security.session_user(request))
    ):
        allow_held = 0
        allow_source_fallback = 0
    # L2: an unknown locale for this doc is a clean 404 (parity with the /api download),
    # not a source-filled 200 (build would fall back to source for a locale with no
    # segments) nor a held-409 (held_locale_reason no-ops when there are no segments).
    # L4: a projected DISTINCT query for the locale set, not full ORM Segment rows.
    doc_locales = set(
        session.exec(
            select(Segment.target_locale).where(Segment.document_id == doc_id).distinct()
        ).all()
    )
    if locale not in doc_locales:
        if _wants_ui(request):
            return _error_page(request, 404, "That language is not on this document.")
        return JSONResponse(
            {"ok": False, "error": "not found (no such locale on this document)", "locale": locale},
            status_code=404,
        )
    # `as_fmt=po`: a gettext MO upload delivered as its PO source (export_as_formats).
    as_fmt = (as_fmt or "").strip().lower()
    if as_fmt and as_fmt not in export.export_as_formats(doc.fmt):
        if _wants_ui(request):
            return _error_page(request, 400, "That export format is not available for this file.")
        return JSONResponse(
            {"ok": False, "error": f"as_fmt={as_fmt} is not available for a {doc.fmt} file"},
            status_code=400,
        )
    # AI Tagging 1.6: a FULL_TEXT paragraph/token desync quarantines the WHOLE export
    # (no corrupt file leaves the system). Surface it as a clean 409 naming the reason,
    # not a 500 — the operator resolves it via the editor / needs-review queue.
    # AI Tagging 1.7 (#41): `rep` collects every segment/paragraph whose tag-damaged
    # translation was replaced by its SOURCE text. The bytes stay clean, so without this
    # the operator cannot tell a degraded deliverable from a good one.
    # GitHub #42 (C-3): `?allow_source_fallback=1` is the EXPLICIT per-export override
    # for a FULL_TEXT guard-B desync — the damaged paragraph(s) ship as SOURCE text and
    # are reported through the SAME #41 channel. Bounded by
    # export_source_fallback_max_share (above it the override is REFUSED, not warned);
    # guard A (count desync) is untouched and still quarantines globally.
    # chip task_9c665111: refuse a HELD locale (no committed translation -> a pure-source
    # file that reads as delivered). A delivery guard, checked before the build. 409.
    # M1 (#42 lesson: a 409 that names no way out bricks the operator): `?allow_held=1`
    # is the EXPLICIT override to ship the source-filled file anyway, and the 409 body
    # names it (export_anyway_url) exactly like the tag-quarantine's does.
    # Batch 15 T2 (§9.1/LAW-TM-14): the TM-only empty gate is a COMPANION consulted BEFORE
    # the classic held check and placed so `allow_held` cannot bypass a HOLD. A HOLD refuses
    # (409) naming the AUDITED approval way-out (never `allow_held`, which would ship the
    # source-filled file); a RELEASE_EMPTIES delivers with empty strings (the seam emits
    # them); a PASS falls through to the unchanged classic held/allow_held logic.
    # ISSUE #95 (gate C round 1, H1): the QUALITY-GATE hold COMPANION, consulted
    # first and NOT bypassable by allow_held -- these segments were produced by a
    # run whose correction loop ended with its gate still FAILING, so they are
    # unreviewed failing text, not a missing translation.
    _qgate = export.gate_held_reason(session, doc, locale)
    if _qgate is not None:
        if _wants_ui(request):
            held = export.gate_held_segments(session, doc, locale, limit=_EXPORT_ITEMS_SHOWN)
            return _export_refused_page(
                request,
                session,
                doc,
                locale,
                status_code=409,
                headline="This export is held by a quality check",
                plain=(
                    "The export was stopped so unchecked translations do not reach a "
                    "delivered file."
                ),
                why=(
                    "Some segments came out of a translation run whose quality check did "
                    "not pass them, and nobody has reviewed them yet. A person must approve "
                    "or edit each one. The hold clears by itself once they have."
                    if held["total"]
                    else "This language is not ready to deliver yet."
                ),
                error=_qgate,
                items=[
                    {
                        "label": f"Segment {r['order_index'] + 1}",
                        "problem": (
                            f"Quality check {r['step']!r} did not pass"
                            if r["step"]
                            else "Quality check did not pass"
                        ),
                        "source": r["source_text"],
                        "target": r["target_text"],
                    }
                    for r in held["rows"]
                ],
                items_heading="Segments waiting for review",
                items_unit="Segment",
                items_total=held["total"],
                review_url=f"/documents/{doc.id}/editor?locale={locale}",
                not_bypassable=True,
            )
        return JSONResponse(
            {
                "ok": False,
                "gate_held": True,
                "error": _qgate,
                "locale": locale,
                "review_url": f"/documents/{doc.id}/editor?locale={locale}",
            },
            status_code=409,
        )
    _gate = tm_delivery.empty_gate_decision(session, doc=doc, locale=locale)
    if _gate == tm_delivery.HOLD:
        empties = tm_delivery.doc_empties(session, doc, locale)
        if _wants_ui(request):
            _msg = tm_delivery.held_message(
                session,
                doc=doc,
                locale=locale,
                count=len(empties),
                unit="segment",
                way_out=(
                    "It is not bypassable with allow_held: review + approve at "
                    f"/documents/{doc.id}/empties?locale={locale}."
                ),
            )
            return _export_refused_page(
                request,
                session,
                doc,
                locale,
                status_code=409,
                headline="This export is held: some targets are empty",
                plain="The export was stopped so empty translations do not ship by accident.",
                why=(
                    "Some segments have no translation. A person has to review them and "
                    "approve delivering them as empty strings before the file can be exported."
                ),
                error=_msg,
                items=[
                    {
                        "label": f"Segment {int(e.get('order_index', 0)) + 1}",
                        "problem": "No translation",
                        "source": e.get("source_text", ""),
                        "target": "",
                    }
                    for e in empties[:_EXPORT_ITEMS_SHOWN]
                ],
                items_heading="Segments with empty targets",
                items_unit="Segment",
                items_total=len(empties),
                empties_url=f"/documents/{doc.id}/empties?locale={locale}",
                not_bypassable=True,
            )
        return JSONResponse(
            {
                "ok": False,
                "empty_held": True,
                # #105: shared diagnosis seam + this surface's own way-out.
                "error": tm_delivery.held_message(
                    session,
                    doc=doc,
                    locale=locale,
                    count=len(empties),
                    unit="segment",
                    way_out=(
                        "It is not bypassable with allow_held: review + approve at "
                        f"/documents/{doc.id}/empties?locale={locale}."
                    ),
                ),
                "locale": locale,
                "empties": empties,
                "empties_url": f"/documents/{doc.id}/empties?locale={locale}",
                "approve_url": f"/documents/{doc.id}/deliver-approve?locale={locale}",
            },
            status_code=409,
        )
    if _gate != tm_delivery.RELEASE_EMPTIES and not allow_held:
        held = export.held_locale_reason(session, doc, locale)
        if held is not None:
            if _wants_ui(request):
                return _export_refused_page(
                    request,
                    session,
                    doc,
                    locale,
                    status_code=409,
                    headline="This language has no translation yet",
                    plain="The export was stopped because the file would be entirely source text.",
                    why=(
                        "Nothing has been translated for this language, so the exported file "
                        "would just repeat the source. Finish or resume the translation, or "
                        "export the source text on purpose."
                    ),
                    error=held,
                    export_anyway_url=f"/documents/{doc.id}/export?locale={locale}&allow_held=1",
                    anyway_label="Export source text anyway",
                    anyway_note=(
                        "Exports the file with the untranslated source text in place of "
                        "translations. The export is recorded in the audit log."
                    ),
                )
            return JSONResponse(
                {
                    "ok": False,
                    "held": True,
                    "error": held,
                    "locale": locale,
                    "export_anyway_url": (
                        f"/documents/{doc.id}/export?locale={locale}&allow_held=1"
                    ),
                },
                status_code=409,
            )
    rep: list = []
    try:
        data = export.build_translated_file(
            session,
            doc,
            locale,
            report=rep,
            allow_source_fallback=bool(allow_source_fallback),
            as_fmt=as_fmt or None,
        )
    except export.ExportMissingBlob as exc:
        # Issue #54 L3: a missing original upload blob is an honest 4xx ("original file
        # missing"), not a 500 traceback. 409 (a delivery-state conflict, like held).
        if _wants_ui(request):
            return _export_refused_page(
                request,
                session,
                doc,
                locale,
                status_code=409,
                headline="The original file is missing",
                plain="The export was stopped because the uploaded original cannot be found.",
                why=(
                    "The translated file is rebuilt from the original upload, and that upload "
                    "is no longer in storage. Upload the document again to export it."
                ),
                error=str(exc),
            )
        return JSONResponse(
            {"ok": False, "missing_blob": True, "error": str(exc), "locale": locale},
            status_code=409,
        )
    except export.ExportUnreadableBlob as exc:
        # F4: a PRESENT-but-corrupt/undecryptable blob is a distinct honest 4xx.
        if _wants_ui(request):
            return _export_refused_page(
                request,
                session,
                doc,
                locale,
                status_code=409,
                headline="The original file cannot be read",
                plain="The export was stopped because the uploaded original is damaged.",
                why=(
                    "The translated file is rebuilt from the original upload, which is stored "
                    "but cannot be read back. Upload the document again to export it."
                ),
                error=str(exc),
            )
        return JSONResponse(
            {"ok": False, "unreadable_blob": True, "error": str(exc), "locale": locale},
            status_code=409,
        )
    except export.TagExportQuarantine as exc:
        body = {
            "ok": False,
            "quarantined": True,
            "error": str(exc),
            "kind": exc.kind,
            "paragraphs": exc.paragraphs,
        }
        # A 409 that names no way out is how doc 44 became "bricked" (#42). Guard B HAS
        # a way out, so say so: the paragraph view, and the explicit override the
        # operator must ask for by name.
        # A DIAGNOSIS link (not an override): the per-paragraph view names which paragraphs
        # are damaged ("tokens"/"share_ceiling") or blank/missing ("untranslated"), so an
        # operator can fill/revert the right ones. "count" (positional map scrambled) has
        # no trustworthy per-paragraph view, so no link.
        if exc.kind in ("tokens", "share_ceiling", "untranslated"):
            body["paragraphs_url"] = f"/documents/{doc.id}/paragraphs?locale={locale}"
        # Only the recoverable TAGGED "tokens" case has a WAY OUT: `allow_source_fallback=1`
        # ships source for the damaged paragraph(s), the map being exact there. A tagless
        # under-translation ("untranslated") / count desync ("count") leaves no trustworthy
        # positional map, so it offers no override — re-run or edit the body is the way out.
        if exc.kind == "tokens":
            body["export_anyway_url"] = (
                f"/documents/{doc.id}/export?locale={locale}&allow_source_fallback=1"
            )
        if _wants_ui(request):
            _head, _why = _QUARANTINE_KIND_COPY.get(
                exc.kind, ("This export is quarantined", "The translated file failed a check.")
            )
            _paras = [p for p in (exc.paragraphs or []) if isinstance(p, dict)]
            return _export_refused_page(
                request,
                session,
                doc,
                locale,
                status_code=409,
                headline=_head,
                plain="The export was stopped so a damaged file does not reach a customer.",
                why=_why,
                error=str(exc),
                items=[
                    {
                        "label": f"Paragraph {p.get('index', '')}",
                        "problem": p.get("reason") or "An inline tag differs from the source",
                        "source": p.get("source_text", ""),
                        "target": p.get("target_text", ""),
                    }
                    for p in _paras[:_EXPORT_ITEMS_SHOWN]
                ],
                items_heading="Affected paragraphs",
                items_unit="Paragraph",
                items_total=len(_paras),
                paragraphs_url=body.get("paragraphs_url", ""),
                export_anyway_url=body.get("export_anyway_url", ""),
                anyway_label="Export anyway with source text for these paragraphs",
                anyway_note=(
                    "The damaged paragraphs ship as source-language text instead of the "
                    "broken translation; everything else is delivered as translated. The "
                    "export is recorded in the audit log and the file is marked as substituted."
                ),
                not_bypassable=not body.get("export_anyway_url"),
            )
        return JSONResponse(body, status_code=409)
    # Downloads leave the system — always leave a trace in the audit log. The detail
    # names the KIND (a routine #41 substitution vs an operator's explicit
    # ?allow_source_fallback=1 force-export) and the UNIT (segment order_index vs
    # paragraph index), rendered by the seam so all surfaces say it the same way.
    audit.log(
        actor=_actor_email(request),
        action="document.export",
        target=f"doc {doc.id} {locale}",
        ip=_ip(request),
        detail=export.substitution_detail(rep),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    # Gate MED-2: not a filesystem sink (this name only reaches a header), but it is
    # composed from the STORED filename like the CLI's export default, so it goes through
    # the SAME helper -- one rule for every `<stem>.<locale>.<fmt>` in the product.
    from app.kernel.mime import export_ext_for

    fname = content.safe_output_name(
        doc.filename, locale, as_fmt or export_ext_for(doc.fmt), neutralise=False
    )
    # RFC 6266 (content_disposition_attachment): a NON-ASCII filename (CJK/emoji -- rename
    # accepts them; only `"`/CRLF are rejected) cannot latin-1-encode into a bare
    # `filename="..."` on the ASGI wire -> a 500. Emit an ASCII `filename=` fallback plus a
    # `filename*=UTF-8''<percent-encoded>` ext-value. Still neutralises `"`/`\`/control chars.
    headers = {"Content-Disposition": content.content_disposition_attachment(fname)}
    if rep:
        # SUBSTITUTION-RECORD count. Emitted only when non-empty, so a clean export's
        # headers are byte-identical to pre-1.7.
        headers["X-Export-Substituted"] = str(len(rep))
        # ...and the SAME detail the /api twin has emitted since #42. A count alone
        # cannot say WHICH units are untranslated, nor whether they are segments or
        # paragraphs, so the browser download was the one surface that could not answer
        # the operator's next question. ASCII by construction (kind/unit/index only).
        headers["X-Export-Substituted-Detail"] = export.substitution_detail(rep)
    return Response(
        content=data,
        media_type=mime_for_fmt(as_fmt or doc.fmt),
        headers=headers,
    )


# --- TM-only delivery empty gate: document delivery-review panel (Batch 15 T2, §9.6) ---
@router.get("/documents/{doc_id}/empties")
def document_empties(
    doc_id: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    """The delivery-review panel: the ACTUAL empty strings (the maintainer's flag, not just a count)
    a held TM-only (doc, locale) carries, with Approve / Revoke. Web+CLI parity."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    locale = (locale or "").strip()
    me = security.session_user(request)
    decision = tm_delivery.empty_gate_decision(session, doc=doc, locale=locale)
    empties = tm_delivery.doc_empties(session, doc, locale)
    row = tm_delivery._doc_approval_row(session, doc_id, locale)
    live_ref = tm_delivery.active_run_ref(
        session, doc_id=doc_id, project_id=doc.project_id, locale=locale
    )
    project = session.get(Project, doc.project_id)
    return templates.TemplateResponse(
        request,
        "delivery_empties.html",
        {
            "request": request,
            "mode": "doc",
            "doc": doc,
            "project_id": doc.project_id,
            # a11y T5: the language of the untranslated source text the panel lists.
            "source_locale": project.source_locale if project else "",
            "locale": locale,
            "decision": decision,
            "empties": empties,
            "approved": bool(row is not None and row.status == "approved"),
            # #105: a run still executing over this (doc, locale) is WHY the targets are
            # empty; the panel must not offer an approval the service will refuse. Round-2
            # HIGH-1: `active_run_ref` also sees an Advanced-Workflow run, and the panel names
            # the tier rather than calling an Advanced mission "Run #N".
            "live_run_id": live_ref[1] if live_ref else None,
            "live_run_label": tm_delivery.run_ref_label(live_ref),
            "approve_action": f"/documents/{doc_id}/deliver-approve?locale={locale}",
            "revoke_action": f"/documents/{doc_id}/deliver-revoke?locale={locale}",
            "back_url": f"/documents/{doc_id}",
            "can_write": (not security.auth_required()) or security.can_write(me),
        },
    )


@router.post("/documents/{doc_id}/deliver-approve")
def document_deliver_approve(
    doc_id: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    """Approve delivering (doc, locale)'s empty targets as empty strings. can_write."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Approving an empty delivery requires a write role.")
    try:
        tm_delivery.approve_doc(session, doc, locale, approved_by=_actor_email(request) or "web")
    except tm_delivery.LocaleNotDeliverable as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/documents/{doc_id}/empties?locale={locale}&saved=1", status_code=303)


@router.post("/documents/{doc_id}/deliver-revoke")
def document_deliver_revoke(
    doc_id: int, request: Request, locale: str = "", session: Session = Depends(get_session)
):
    """Revoke a prior empty-delivery approval for (doc, locale) -> re-held. can_write."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Revoking an empty delivery requires a write role.")
    try:
        tm_delivery.revoke_doc(session, doc, locale, approved_by=_actor_email(request) or "web")
    except tm_delivery.LocaleNotDeliverable as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/documents/{doc_id}/empties?locale={locale}&saved=1", status_code=303)
