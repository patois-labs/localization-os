"""documents: document-level context override (spec-context-metadata phase 4)

Documented in: docs/architecture/translation-pipeline.md
"""

import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.kernel import (
    audit,
    budgets,
    content,
    glossary,
    jobs,
    project_budgets,
    quality,
    security,
    segment_review,
    tm,
    translate,
)
from app.kernel.providers import (
    DeadEngineError,
)
from app.models import (
    Document,
    Project,
    Segment,
)
from app.web import deps
from app.web.deps import (
    _actor_email,
    _admin_block,
    _error_page,
    _ip,
    _missing_block,
    _missing_or,
    _my_org_id,
    _scoped_doc,
    _visibility_block,
    _visible_doc,
    templates,
)
from app.web.streaming import StreamingResponse

_logger = logging.getLogger("loc_tms")


router = APIRouter()


# `_visible_doc` (the "an internal key document resolves as MISSING" helper every
# route below calls) now lives in app/web/deps.py — the workflow RUN routes in
# app/web/workflows.py need the same predicate and cannot import this module.
# Imported into this namespace above, so the call sites and any test that patches
# `documents._visible_doc` are unchanged.


def _editor_doc(session, doc_id):
    """The CAT editor's / `/segments/*` view of a document — spec 2.5's ONE deliberate
    carve-out from `_visible_doc`, with the Wave B fix round's correction (A-F2/B-4).

    A key project's NAMESPACE documents stay openable: a reviewer following a link
    from the key grid gets the existing editor for free, and every segment in one
    belongs to a LIVE key.

    The TRASH document does not. Its segments are a SOFT-DELETED key's parked values,
    and the carve-out was blanket, so `GET /documents/{trash_id}/editor` rendered a
    deleted key's content and `POST /segments/{id}/save` wrote to it — a silent write,
    since a tombstoned key emits no `value` entries (review A9), on rows the key grid
    itself renders read-only. Two surfaces disagreeing about whether a deleted key is
    editable is the bug; this is the agreement."""
    doc = session.get(Document, doc_id)
    if doc is not None and doc.fmt == "keys":
        from app.kernel import keys as _keys

        if _keys.is_trash_document(doc):
            return None
    return doc


def _deleted_key_block(session, seg):
    """A JSONResponse refusing a write to a SOFT-DELETED key's value row, or None.

    Mirrors `keys.set_value`'s guard (keys.py: "this key is deleted; restore it before
    writing values.") so the CAT editor and the key grid answer the same question the
    same way. Costs a document-only install one `key_id` attribute read: a segment
    with no key is not a key value, and there is nothing to look up."""
    key_id = getattr(seg, "key_id", None)
    if not key_id:
        return None
    from app.models import StringKey

    key = session.get(StringKey, key_id)
    if key is not None and key.deleted_at is not None:
        return JSONResponse(
            {
                "ok": False,
                "error": "this key is deleted; restore it in the key grid before writing values.",
            },
            status_code=404,
        )
    return None


# --- glossary ---
@router.post("/documents/{doc_id}/glossary-suggest")
def glossary_suggest(doc_id: int, request: Request, session: Session = Depends(get_session)):
    """Propose candidate glossary terms for this document (GitHub #388).

    POST, not GET (#388: the sibling of the #283 live-translate fix): the call below runs
    `glossary.build_from_text`, which fires a paid provider call through
    `spend.complete_direct` on every hit. As a bare GET it sat outside the middleware's
    mutating-request protections, so a read-only viewer (or a cross-site link that
    carried a signed-in user's SameSite=Lax cookie) could spend the provider budget with
    no CSRF token and a foreign Origin. As a POST it rides the same Origin check, CSRF
    token and write-role gate as every other paid action. The route also enforces the
    write role itself (the middleware gate is a second layer only), like the document
    write routes below."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Generating glossary suggestions requires a write role.")
    # AI opt-out (DPAI-08): suggestions are an LLM task; refuse for an opted-out project.
    from app.kernel import ai_optout

    ai_optout.refuse(session, doc.project_id, "glossary suggestions")
    units = (doc.skeleton or {}).get("units", [])
    text = "\n".join(u["text"] for u in units)
    suggestions = glossary.build_from_text(text)
    project = session.get(Project, doc.project_id)
    from app.models import TargetLocale

    locales = session.exec(
        select(TargetLocale).where(TargetLocale.project_id == doc.project_id)
    ).all()
    return templates.TemplateResponse(
        request,
        "glossary_suggest.html",
        {
            "request": request,
            "doc": doc,
            "suggestions": suggestions,
            "project": project,
            "locales": locales,
        },
    )


# --- document-level context override (SPEC-context-metadata phase 4) ---
@router.post("/documents/{doc_id}/context")
def document_context_save(
    doc_id: int,
    request: Request,
    domain: str = Form(""),
    content_kind: str = Form(""),
    audience: str = Form(""),
    formality: str = Form(""),
    tone: str = Form(""),
    notes: str = Form(""),
    char_limit: str = Form(""),
    source_title: str = Form(""),
    source_section: str = Form(""),
    source_labels: str = Form(""),
    session: Session = Depends(get_session),
):
    """Set/edit this document's context override (Document.context). Routes through
    context_pkg.validate_and_sanitize (invalid -> clean error, nothing saved); a fully
    cleared form stores None (the doc falls back to the resolved profile chain). Gated
    by project visibility AND a write role (mirrors the document write routes)."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Editing document context requires a write role.")
    from urllib.parse import quote

    from app.kernel import context_pkg

    raw = {
        k: v
        for k, v in (
            ("domain", domain),
            ("content_kind", content_kind),
            ("audience", audience),
            ("formality", formality),
            ("notes", notes),
        )
        if (v or "").strip()
    }
    if (tone or "").strip():
        raw["tone"] = [t.strip() for t in tone.split(",") if t.strip()]
    if (char_limit or "").strip():
        raw["char_limit"] = char_limit
    source = {
        k: v for k, v in (("title", source_title), ("section", source_section)) if (v or "").strip()
    }
    if (source_labels or "").strip():
        source["labels"] = [x.strip() for x in source_labels.split(",") if x.strip()]
    if source:
        raw["source"] = source
    try:
        context_pkg.set_document_context(session, doc, raw)
    except context_pkg.ContextError as exc:
        return RedirectResponse(f"/documents/{doc_id}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="document.context",
        target=f"doc {doc_id}",
        ip=_ip(request),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    return RedirectResponse(f"/documents/{doc_id}?saved=1", status_code=303)


# --- MT translation ---
@router.post("/documents/{doc_id}/translate")
def translate_document(
    doc_id: int, request: Request, locale: str = Form(...), session: Session = Depends(get_session)
):
    """Enqueue MT pre-translation as a background job and return immediately —
    a large document must never hold an HTTP request open for minutes."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    # #283: the service's MT role rule (the middleware gate is a second layer only).
    try:
        translate.require_mt_role(security.session_user(request))
    except translate.MTRoleRefused as exc:
        return _error_page(request, 403, str(exc))
    # AI opt-out (DPAI-08): refuse before any work when the project opted out.
    from app.kernel import ai_optout

    ai_optout.refuse(session, doc.project_id, "machine translation")
    # --- per-department budget parity (T4-02) ---------------------------------
    # POST /api/documents/{id}/translate has gated this 429 ceiling since Phase B
    # chunk 4; this web-UI route never called budgets.check at all, so a
    # department that had exhausted its monthly cap was blocked on the API but
    # could keep translating unbounded through the UI. Same shared budgets.check
    # seam, same clean-error posture (a styled 429 page here, not a 500 or a
    # silent enqueue) — mirrors the API's refusal-before-enqueue behavior.
    me = security.session_user(request)
    dept = (getattr(me, "department", "") or "").strip()
    allowed, _remaining, msg = budgets.check(dept, org_id=_my_org_id(request))
    if not allowed:
        return _error_page(request, 429, msg)
    # --- per-PROJECT USD budget parity (CV-2) ---------------------------------
    # The money twin of the department token cap, on the same three MT seams: at
    # 100% of the project's monthly_usd_limit, refuse before enqueue with the same
    # clean 429 page. Keyed to the project (org from the project, not the caller).
    p_allowed, _p_rem, p_msg = project_budgets.project_check(doc.project_id)
    if not p_allowed:
        # H1: the blocked submission is the 100% crossing — alert before refusing.
        project_budgets.evaluate_thresholds(doc.project_id, actor=_actor_email(request))
        return _error_page(request, 429, p_msg)
    project_budgets.evaluate_thresholds(doc.project_id, actor=_actor_email(request))
    org_for_budget = _my_org_id(request)
    if locale == "all":
        # #563: "Translate all N languages" = one background job per language the document
        # carries, the web twin of POST /api/documents/{id}/translate with several targets.
        locales = sorted(
            {
                s.target_locale
                for s in session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
            }
        )
        if not locales:
            return RedirectResponse(f"/documents/{doc_id}", status_code=303)
        # A double click (or a re-post) must not double the paid MT: the (document, locale) job
        # that is already QUEUED or RUNNING is handed back instead of stacking a second one.
        # #503: each new job carries the department and estimate stamp and is inserted under
        # the budget lock, so it counts as in-flight spend for the next locale and request.
        last = None
        for loc in locales:
            loc_payload = {
                "document_id": doc_id,
                "locale": loc,
                "est_tokens": jobs.estimate_translate_tokens(doc_id, loc),
            }
            if dept:
                loc_payload["department"] = dept
            try:
                last, _created = jobs.submit_unique_budgeted(
                    "translate_document",
                    loc_payload,
                    actor=_actor_email(request),
                    dedupe_key=("document_id", "locale"),
                    department=dept,
                    org_id=org_for_budget,
                )
            except jobs.LiveBudgetRefused as exc:
                if last is None:
                    return _error_page(request, 429, str(exc))
                break
        return RedirectResponse(
            f"/documents/{doc_id}?locale={locales[0]}&job={last}", status_code=303
        )
    # #617 item 4 (gate): refused up front while a run waits on human review for this locale
    # (the job worker refuses too, which is what covers a held locale under "all").
    try:
        segment_review.refuse_if_awaiting_review(session, doc_id, locale)
    except segment_review.AwaitingReviewError as exc:
        return _error_page(request, exc.status, str(exc))
    # #503 item 2: stamp the department and the estimate (like the /api door) so this job
    # counts as in-flight spend, and re-check the cap under one lock with the insert.
    job_payload = {
        "document_id": doc_id,
        "locale": locale,
        "est_tokens": jobs.estimate_translate_tokens(doc_id, locale or ""),
    }
    if dept:
        job_payload["department"] = dept
    try:
        jid = jobs.submit_budgeted(
            "translate_document",
            job_payload,
            actor=_actor_email(request),
            department=dept,
            org_id=org_for_budget,
        )
    except jobs.LiveBudgetRefused as exc:
        return _error_page(request, 429, str(exc))
    return RedirectResponse(f"/documents/{doc_id}?locale={locale}&job={jid}", status_code=303)


@router.post("/documents/{doc_id}/context-ab")
def document_context_ab(
    doc_id: int,
    request: Request,
    locale: str | None = Form(None),
    provider: str | None = Form(None),
    max_segments: int | None = Form(None),
    session: Session = Depends(get_session),
):
    """Context A/B measurement (SPEC-context-metadata phase 4 — Measurement): run the
    same segments with context ON vs OFF, score both with the QE judge, show the delta
    + a verdict. Read-only + non-mutating (the harness measures, never writes a
    target/TM). Foreign-org docs are blocked by the shared visibility gate; the same
    `context_ab.run_ab` service seam the CLI calls (parity).

    POST, not GET (LOW-3): each run fires up to 4xN live LLM calls and accrues real
    spend, so it must be a CSRF-protected form action (like /documents/{id}/translate),
    never a bare link a browser prefetch / crawler / retry could trigger, and the
    unvalidated `provider` must not ride in on a GET query string."""
    from app.kernel import context_ab

    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    # AI opt-out (DPAI-08): refuse before any work when the project opted out.
    from app.kernel import ai_optout

    ai_optout.refuse(session, doc.project_id, "the context A/B comparison")
    project = session.get(Project, doc.project_id)
    try:
        res = context_ab.run_ab(
            session,
            doc,
            locale=locale,
            provider=provider,
            max_segments=(
                max_segments if max_segments is not None else context_ab.DEFAULT_MAX_SEGMENTS
            ),
            org_id=_my_org_id(request),
        )
    except context_ab.ABError as exc:
        return _error_page(request, 400, str(exc))
    return templates.TemplateResponse(
        request,
        "context_ab.html",
        {"request": request, "doc": doc, "project": project, "ab": res.as_dict()},
    )


@router.post("/documents/{doc_id}/style-ab")
def document_style_ab(
    doc_id: int,
    request: Request,
    locale: str | None = Form(None),
    provider: str | None = Form(None),
    max_segments: int | None = Form(None),
    session: Session = Depends(get_session),
):
    """Style-guide A/B measurement (SPEC-workflows-redesign Track B, B3): run the same
    segments with the resolved style guide ON vs OFF, score both with the QE judge, show
    the QE + style-error delta + a verdict. Read-only + non-mutating (the harness
    measures, never writes a target/TM). The SIBLING of /documents/{id}/context-ab —
    same `style_ab.run_ab` service seam the CLI calls (parity), same POST-not-GET
    rationale (each run fires up to 4xN live LLM calls + real spend, so it must be a
    CSRF-protected form action, never a bare link a prefetch/crawler could trigger)."""
    from app.kernel import style_ab
    from app.kernel.context_ab import ABError

    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    # AI opt-out (DPAI-08): refuse before any work when the project opted out.
    from app.kernel import ai_optout

    ai_optout.refuse(session, doc.project_id, "the style A/B comparison")
    project = session.get(Project, doc.project_id)
    try:
        res = style_ab.run_ab(
            session,
            doc,
            locale=locale,
            provider=provider,
            max_segments=(
                max_segments if max_segments is not None else style_ab.DEFAULT_MAX_SEGMENTS
            ),
            org_id=_my_org_id(request),
        )
    except ABError as exc:
        return _error_page(request, 400, str(exc))
    return templates.TemplateResponse(
        request,
        "style_ab.html",
        {"request": request, "doc": doc, "project": project, "ab": res.as_dict()},
    )


@router.get("/documents/{doc_id}/estimate")
def translate_estimate(
    doc_id: int, locale: str, request: Request, session: Session = Depends(get_session)
):
    """Pre-flight translation estimate (Feature 7): expected token size + rough
    duration BEFORE kicking off. No provider call, no live token count."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, JSONResponse({"error": "not found"}, status_code=404))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    return JSONResponse(translate.estimate_translation(session, doc_id, locale))


@router.post("/documents/{doc_id}/translate-stream")
def translate_stream(
    doc_id: int, request: Request, locale: str = Form(...), session: Session = Depends(get_session)
):
    """Server-Sent Events: translate segment-by-segment, live. Uses its own DB
    session because the generator outlives the request scope.

    A POST (security review #283): starting it spends paid MT and writes segments,
    so it rides the mutating-request middleware (Origin check, CSRF token, write-role
    gate). A cross-site link or a GET can no longer start it; the Document page reads
    the event stream from the POST response with fetch. Fail closed (#282): the
    document resolves through `_scoped_doc`, so a missing, internal key, foreign or
    non-member document is the same 404 before any work, and the role rule is the
    translate service's own `require_mt_role`, not only the middleware's."""
    doc, blocked = _scoped_doc(request, session, doc_id)
    if blocked:
        return blocked
    try:
        translate.require_mt_role(security.session_user(request))
    except translate.MTRoleRefused as exc:
        return _error_page(request, 403, str(exc))
    # AI opt-out (DPAI-08): refuse before any work when the project opted out.
    from app.kernel import ai_optout

    ai_optout.refuse(session, doc.project_id, "machine translation")
    # #617 item 4 (gate): refused before any budget reservation while a run waits on review.
    try:
        segment_review.refuse_if_awaiting_review(session, doc.id, locale)
    except segment_review.AwaitingReviewError as exc:
        return _error_page(request, exc.status, str(exc))
    # Budget caps, metered (gate round 1, M1): the stream opens the same translate_document
    # Job row the background path writes, reserving its estimate atomically with the
    # department and project cap checks, so repeated and concurrent streams count
    # against both meters; the generator settles the row with the real totals.
    me = security.session_user(request)
    try:
        jid = jobs.reserve_live_translate(
            doc_id,
            locale,
            department=(getattr(me, "department", "") or ""),
            actor=_actor_email(request),
            org_id=_my_org_id(request),
        )
    except jobs.LiveBudgetRefused as exc:
        return _error_page(request, 429, str(exc))
    from app.db import engine

    def gen():
        stats, done, error = None, 0, ""
        running: dict = {}  # the impl's live totals (M1-R): what a cut-off really spent
        try:
            with Session(engine) as s:
                try:
                    for ev in translate.pretranslate_stream(s, doc_id, locale, stats_sink=running):
                        if ev.get("type") == "segment":
                            done += 1
                        elif ev.get("type") == "summary":
                            stats = {k: v for k, v in ev.items() if k != "type"}
                            # #364: the booked USD rides only on the running sink (the SSE
                            # summary never carries it), for the settled Job row.
                            stats["cost_usd"] = running.get("cost_usd")
                        yield f"data: {json.dumps(ev)}\n\n"
                except segment_review.AwaitingReviewError as exc:
                    # #617: a review pause landed between the pre-check and the stream.
                    error = str(exc)
                    yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
                    return
                except DeadEngineError as exc:
                    # A stored engine:<id> ref went dead (deleted/disabled/keyless):
                    # surface a loud SSE error event (never silently substitute mock
                    # output), then close the stream. The message names the dead ref,
                    # consistent with the CLI/job/connector surfaces.
                    error = str(exc)
                    yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
                    return
                # P1: auto-complete the project if this finished all locales/docs.
                try:
                    from app.kernel import lifecycle

                    d = s.get(Document, doc_id)
                    if d is not None:
                        lifecycle.mark_completed_if_done(s, d.project_id, actor="system")
                except Exception:  # noqa: S110 -- auto-complete is cosmetic; the stream still ends
                    pass
            yield 'data: {"type": "done"}\n\n'
        finally:
            # Runs on normal end, an engine error, and a client cut-off (GeneratorExit).
            jobs.settle_live_translate(jid, stats, done=done, error=error, partial=dict(running))

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/documents/{doc_id}/apply-tm")
def apply_tm(
    doc_id: int,
    request: Request,
    locale: str = Form(...),
    session: Session = Depends(get_session),
):
    """Fill in target text for every segment with a 100% TM match."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    project = session.get(Project, doc.project_id)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Applying TM requires a write role.")
    from urllib.parse import quote

    try:
        applied = content.apply_tm_to_document(session, doc, project, locale)
    except content.InvalidLocaleError as exc:
        return RedirectResponse(
            f"/documents/{doc_id}?locale={locale}&err=" + quote(str(exc)), status_code=303
        )
    except segment_review.AwaitingReviewError as exc:
        return _error_page(request, exc.status, str(exc))
    return RedirectResponse(
        f"/documents/{doc_id}?locale={locale}&tm_applied={applied}", status_code=303
    )


# --- document rename + delete (SPEC-rename-delete) ---
@router.post("/documents/{doc_id}/rename")
def document_rename(
    doc_id: int,
    request: Request,
    filename: str = Form(...),
    session: Session = Depends(get_session),
):
    """Rename a document's filename (SPEC-rename-delete). Blob storage is id-keyed so the
    upload is unaffected. Gated by project visibility AND a write role (translator+),
    matching the document write routes. A blank name redirects with a clean error."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Renaming a document requires a write role.")
    from urllib.parse import quote

    try:
        content.rename_document(
            session, doc_id, filename, actor=_actor_email(request), ip=_ip(request)
        )
    except ValueError as exc:
        return RedirectResponse(f"/documents/{doc_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/documents/{doc_id}?saved=1", status_code=303)


@router.post("/documents/{doc_id}/delete")
def document_delete(
    doc_id: int,
    request: Request,
    session: Session = Depends(get_session),
    privacy_erasure: bool = Form(False),
):
    """Delete a document + its upload blob + segments (SPEC-rename-delete). Admin-only
    (every new delete is strictly is_admin); project-visibility gated. Does NOT wipe the
    project's shared TM. Privacy erasure also deletes Lab snapshots and results, and only the
    TM rows that came from this document."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    pid = doc.project_id
    counts = content.delete_document(
        session,
        doc_id,
        actor=_actor_email(request),
        ip=_ip(request),
        privacy_erasure=privacy_erasure is True,
    )
    counts = counts or {}
    location = f"/projects/{pid}?doc_deleted=1"
    pending = counts.get("file_pending", 0)
    if pending:
        location += f"&file_pending={pending}"
    preserved = counts.get("lab_datasets_preserved", 0)
    if preserved:
        location += f"&lab_datasets_preserved={preserved}"
    return RedirectResponse(location, status_code=303)


# --- FULL_TEXT paragraph view + recovery (GitHub #42) ---
# In FULL_TEXT mode the segment IS the whole document, so the CAT editor offers one
# giant unit and a linguist facing a tag-damage quarantine has nowhere to go. These
# routes are thin callers of app.services.fulltext_paragraphs — the SAME seam /api and
# `loc-tms doc paragraphs / revert-paragraph / set-paragraph` call. RBAC is
# `_visibility_block`, identical to segment_save.
@router.get("/documents/{doc_id}/paragraphs")
def document_paragraphs(
    doc_id: int,
    request: Request,
    locale: str | None = None,
    session: Session = Depends(get_session),
):
    from app.kernel import fulltext_paragraphs as ftp

    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_block(request, session)
    project = session.get(Project, doc.project_id)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    all_segments = session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
    locales = sorted({s.target_locale for s in all_segments})
    selected = locale if locale in locales else (locales[0] if locales else None)
    view = (
        ftp.split_view(session, doc, selected)
        if selected
        else {"ok": False, "reason": "no_segment"}
    )
    return templates.TemplateResponse(
        request,
        "paragraphs.html",
        {
            "request": request,
            "doc": doc,
            "project": project,
            "view": view,
            "locales": locales,
            "selected": selected,
        },
        status_code=200,
    )


@router.post("/documents/{doc_id}/paragraphs/{index}/revert")
def document_paragraph_revert(
    doc_id: int,
    index: int,
    request: Request,
    locale: str = Form(""),
    session: Session = Depends(get_session),
):
    from app.kernel import fulltext_paragraphs as ftp

    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(
            request, session, JSONResponse({"ok": False, "reason": "no_document"}, status_code=404)
        )
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    res = ftp.revert_paragraph(
        session, doc, locale, index, actor=_actor_email(request), ip=_ip(request)
    )
    return JSONResponse(res, status_code=deps.paragraph_status(res))


@router.post("/documents/{doc_id}/paragraphs/revert-all-damaged")
def document_paragraph_revert_all(
    doc_id: int, request: Request, locale: str = Form(""), session: Session = Depends(get_session)
):
    """Atomic batch revert (GitHub #42, review MEDIUM) — the WEB twin of `doc
    revert-paragraph --all-damaged`. Without it a linguist reverts one paragraph at a
    time and can leave a half-reverted doc PAST the ceiling with no up-front check; this
    route reverts every damaged paragraph atomically (nothing written if the combined
    share exceeds `export_source_fallback_max_share`). Same auth as the single-index
    route."""
    from app.kernel import fulltext_paragraphs as ftp

    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(
            request, session, JSONResponse({"ok": False, "reason": "no_document"}, status_code=404)
        )
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    res = ftp.revert_all_damaged(session, doc, locale, actor=_actor_email(request), ip=_ip(request))
    return JSONResponse(res, status_code=deps.paragraph_status(res))


@router.post("/documents/{doc_id}/paragraphs/{index}/save")
def document_paragraph_save(
    doc_id: int,
    index: int,
    request: Request,
    locale: str = Form(""),
    text: str = Form(""),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
):
    from app.kernel import fulltext_paragraphs as ftp

    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(
            request, session, JSONResponse({"ok": False, "reason": "no_document"}, status_code=404)
        )
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    res = ftp.save_paragraph(
        session,
        doc,
        locale,
        index,
        text,
        actor=_actor_email(request),
        ip=_ip(request),
        confirm=confirm in ("1", "true", "yes"),
    )
    # A tag-damage refusal is a BLOCKING CONFIRMATION, not an error page — the same
    # non-destructive posture as segment_save's guard: nothing was written, the operator
    # may fix the tokens instead, and `paragraphs.html` re-POSTs with confirm=1 off the
    # `needs_confirm` flag in the body. The STATUS comes from the seam's classification
    # so this route cannot answer 200 for a refusal its /api twin answers 409 for.
    return JSONResponse(res, status_code=deps.paragraph_status(res))


def _fulltext_residual_reason(doc: Document, target_text: str) -> str:
    """The FIRST per-paragraph tag-damage reason in a FULL_TEXT `target_text`, or "".

    Mirrors `fulltext_paragraphs._rows` / `save_paragraph`: split the body on the
    paragraph delimiter against `doc.skeleton["units"]` and run `tag_damage_detail` per
    paragraph. It is STRICTLY SHARPER than the whole-body `quality.tag_damage`, which
    keeps only the LAST body per repeated token name (ids restart per paragraph) and so
    misses an emptied span hidden behind a same-named healthy span later in the body. A
    count desync (guard A) is NOT this guard's business — that is a whole-document
    quarantine, and the editor may legitimately be reflowing paragraphs — so it returns
    "" there and lets the export's guard A own it."""
    units = (doc.skeleton or {}).get("units", [])
    parts = (target_text or "").split("\n\n")
    if len(parts) != len(units):
        return ""
    for idx, u in enumerate(units):
        delta = quality.tag_damage_detail(u.get("text") or "", parts[idx])
        if delta:
            return delta["reason"]
    return ""


@router.post("/segments/{seg_id}/unlock")
def segment_unlock(seg_id: int, request: Request, session: Session = Depends(get_session)):
    """item 5b (PROPOSAL-tm-matching §5): the EXPLICIT, audited unlock of a TM-locked
    segment. RBAC: project-visibility AND the REVIEWER+ role (`security.can_review` —
    reviewer/linguist/manager/admin; a translator or viewer is refused 403). Clearing a
    hard TM lock is reserved for a human reviewer+ — the recorded policy (models.Segment
    lock_hard note; tm_lock.unlock_segment), enforced IDENTICALLY on the /api twin
    (api_cli.api_segment_unlock) so no surface is a softer way in. A refused attempt
    writes NO `tm.unlock` row (this returns before the unlock). The allowed unlock clears
    the lock + provenance and writes a `tm.unlock` audit row. Idempotent (unlocked False =
    was not locked)."""
    seg = session.get(Segment, seg_id)
    if not seg:
        return _missing_or(request, session, JSONResponse({"ok": False}, status_code=404))
    doc = session.get(Document, seg.document_id)
    blocked = _visibility_block(request, session, doc.project_id if doc else None)
    if blocked:
        return blocked
    # REVIEWER+ gate (task_223abc28 / task_938f801f): the shared mutating-request
    # middleware only enforces can_write (>= translator), so without this a translator
    # could clear a hard TM lock. can_review raises the bar to reviewer+ and mirrors the
    # /api route's _review_block exactly. No-op with the login wall off (single-operator).
    me = security.session_user(request)
    if security.auth_required() and not security.can_review(me):
        return JSONResponse(
            {"ok": False, "error": "clearing a TM lock requires the reviewer role (or higher)."},
            status_code=403,
        )
    from app.kernel import tm_lock

    try:
        cleared = tm_lock.unlock_segment(session, seg, actor=_actor_email(request), ip=_ip(request))
    except segment_review.AwaitingReviewError as exc:
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": str(exc)}, status_code=exc.status
        )
    return JSONResponse({"ok": True, "unlocked": cleared})


@router.post("/segments/{seg_id}/approve")
def segment_approve(
    seg_id: int,
    request: Request,
    acknowledge_placeholder_loss: str = Form(""),
    propagate: str = Form("1"),
    session: Session = Depends(get_session),
):
    seg = session.get(Segment, seg_id)
    if not seg:
        return _missing_or(request, session, JSONResponse({"ok": False}, status_code=404))
    doc = session.get(Document, seg.document_id)
    project = session.get(Project, doc.project_id)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    # chip task_9c665111: the WHOLE approve-and-write-TM policy (deleted-key refusal,
    # APPROVED + QE-badge clear, the refusal/language perimeter, the guarded write_back)
    # now lives in ONE seam so /api + CLI + MCP run the identical guards -- see
    # segment_review.approve_and_write_tm for the (unchanged) rationale it carries.
    try:
        out = segment_review.approve_and_write_tm(
            session,
            seg,
            project,
            doc,
            actor=_actor_email(request),
            ip=_ip(request),
            acknowledge_placeholder_loss=acknowledge_placeholder_loss in ("1", "true", "yes"),
            propagate=propagate not in ("0", "false", "no"),
        )
    except segment_review.SegmentApproveError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)
    body = {"ok": True, "status": out["status"], "tm_written": out["tm_written"]}
    if out["tm_skipped"]:
        body["tm_skipped"] = out["tm_skipped"]
    if out.get("tm_skipped_code"):
        body["tm_skipped_code"] = out["tm_skipped_code"]
    # Issue #617 item 6: the repeats this approval was applied to (ids), and any it refused.
    body["propagated"] = out["propagated"]
    body["propagation_refused"] = out["propagation_refused"]
    if out.get("propagation_skipped"):
        body["propagation_skipped"] = out["propagation_skipped"]
    return JSONResponse(body)


@router.get("/concordance")
def concordance(
    project_id: int,
    q: str,
    source: str,
    target: str,
    request: Request,
    session: Session = Depends(get_session),
):
    blocked = _visibility_block(request, session, project_id)
    if blocked:
        return blocked
    return JSONResponse({"results": tm.concordance(session, project_id, q, source, target)})


@router.post("/segments/{seg_id}/propagation")
def segment_propagation(
    seg_id: int,
    request: Request,
    action: str = Form(""),
    session: Session = Depends(get_session),
):
    """Issue #617 item 6: the per-segment repeat-propagation controls. `action` is `unlink`
    (this segment neither receives nor sends propagated text), `link` (the default again) or
    `undo` (restore the repeats the LAST approval of this segment propagated, while they are
    still exactly as propagated). Same gates as the approve route; each change is audited."""
    seg = session.get(Segment, seg_id)
    if not seg:
        return _missing_or(request, session, JSONResponse({"ok": False}, status_code=404))
    doc = session.get(Document, seg.document_id)
    blocked = _visibility_block(request, session, doc.project_id if doc else None)
    if blocked:
        return blocked
    if action not in ("unlink", "link", "undo"):
        return JSONResponse(
            {"ok": False, "error": "action must be unlink, link or undo."}, status_code=400
        )
    try:
        if action == "undo":
            res = segment_review.undo_propagation(
                session, seg, doc, actor=_actor_email(request), ip=_ip(request)
            )
            return JSONResponse({"ok": True, "segment_id": seg_id, **res})
        changed = segment_review.set_propagation(
            session,
            seg,
            doc,
            linked=(action == "link"),
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except segment_review.AwaitingReviewError as exc:
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": str(exc)}, status_code=exc.status
        )
    return JSONResponse(
        {"ok": True, "segment_id": seg_id, "linked": action == "link", "changed": changed}
    )
