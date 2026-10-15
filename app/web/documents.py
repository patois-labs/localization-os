"""Document upload/detail, MT translation, CAT editor, LQA, and export.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _logger, _editor_doc, _deleted_key_block,
glossary_suggest, document_context_save, translate_document, document_context_ab, document_style_ab,
translate_estimate, translate_stream, apply_tm, document_rename, document_delete,
document_paragraphs, document_paragraph_revert, document_paragraph_revert_all,
document_paragraph_save, _fulltext_residual_reason, segment_unlock, segment_approve, concordance,
upload_document, _LQA_ROWS_PER_PAGE, lqa_view, lqa_run, _lqa_finding_error, rulebook_save,
export_document, document_empties, document_deliver_approve, document_deliver_revoke, segment_save
from app.web.documents_document_level_context_o, app.web.documents_tm_only_delivery_empty_g,
app.web.documents_part3.

Documented in: docs/architecture/translation-pipeline.md
"""

import json
import logging
from copy import copy as _srp_copy

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app import config, db
from app import extensions as _extensions
from app.db import get_session
from app.kernel import ai_optout as _ai_optout
from app.kernel import (
    audit,
    batch_runs,
    budgets,
    content,
    export,
    glossary,
    jobs,
    lifecycle,
    lqa,
    lqa_safety,
    output_guard,
    postedit,
    project_budgets,
    qa,
    qe,
    quality,
    refusal,
    reports,
    security,
    segment_review,
    tag_llm_repair,
    tm,
    tm_delivery,
    translate,
    vendor_dispatch,
)
from app.kernel.ingest import NotADocumentProject, UnsupportedFormat, ingest_document
from app.kernel.providers import (
    DeadEngineError,
    MockProvider,
    ProviderSpec,
    get_provider,
    provider_is_live,
    run_completion,
)
from app.models import (
    Document,
    Project,
    Segment,
    SegStatus,
    TransMode,
    VendorDispatch,
    VendorDispatchStatus,
    WorkflowRun,
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
    _visibility_block,
    _visible_doc,
    mime_for_fmt,
    read_capped_upload,
    templates,
)
from app.web.documents_document_level_context_o import (
    _deleted_key_block,
    _editor_doc,
    _fulltext_residual_reason,
    apply_tm,
    concordance,
    document_context_ab,
    document_context_save,
    document_delete,
    document_paragraph_revert,
    document_paragraph_revert_all,
    document_paragraph_save,
    document_paragraphs,
    document_rename,
    document_style_ab,
    glossary_suggest,
    segment_approve,
    segment_unlock,
    translate_document,
    translate_estimate,
    translate_stream,
)
from app.web.documents_document_level_context_o import (
    router as documents_document_level_context_o_router,
)
from app.web.documents_part3 import router as documents_part3_router
from app.web.documents_part3 import segment_save
from app.web.documents_tm_only_delivery_empty_g import (
    _LQA_ROWS_PER_PAGE,
    _lqa_finding_error,
    document_deliver_approve,
    document_deliver_revoke,
    document_empties,
    export_document,
    lqa_run,
    lqa_view,
    rulebook_save,
    upload_document,
)
from app.web.documents_tm_only_delivery_empty_g import (
    router as documents_tm_only_delivery_empty_g_router,
)
from app.web.queries import _applicable_workflows
from app.web.queries import preferred_workflow as _queries_preferred_workflow
from app.web.streaming import StreamingResponse

# Optional add-on: reached through the extension facade (no-op when absent); name unchanged.
llmemory = _extensions.use("memory")

__all__ = [
    "_LQA_ROWS_PER_PAGE",
    "File",
    "Form",
    "NotADocumentProject",
    "Response",
    "StreamingResponse",
    "TransMode",
    "UnsupportedFormat",
    "UploadFile",
    "_admin_block",
    "_deleted_key_block",
    "_editor_doc",
    "_fulltext_residual_reason",
    "_lqa_finding_error",
    "_my_org_id",
    "apply_tm",
    "budgets",
    "concordance",
    "content",
    "db",
    "deps",
    "document_context_ab",
    "document_context_save",
    "document_delete",
    "document_deliver_approve",
    "document_deliver_revoke",
    "document_empties",
    "document_paragraph_revert",
    "document_paragraph_revert_all",
    "document_paragraph_save",
    "document_paragraphs",
    "document_rename",
    "document_style_ab",
    "export",
    "export_document",
    "glossary_suggest",
    "ingest_document",
    "jobs",
    "llmemory",
    "lqa_run",
    "lqa_view",
    "mime_for_fmt",
    "postedit",
    "project_budgets",
    "qe",
    "read_capped_upload",
    "reports",
    "rulebook_save",
    "segment_approve",
    "segment_review",
    "segment_save",
    "segment_unlock",
    "tm_delivery",
    "translate",
    "translate_document",
    "translate_estimate",
    "translate_stream",
    "upload_document",
]

router = APIRouter()

_logger = logging.getLogger("loc_tms")


_srp_piece_router = documents_document_level_context_o_router
documents_document_level_context_o_router = _srp_copy(documents_document_level_context_o_router)
documents_document_level_context_o_router.routes = documents_document_level_context_o_router.routes[
    0:1
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(documents_document_level_context_o_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
documents_document_level_context_o_router = _srp_piece_router


_srp_piece_router = documents_tm_only_delivery_empty_g_router
documents_tm_only_delivery_empty_g_router = _srp_copy(documents_tm_only_delivery_empty_g_router)
documents_tm_only_delivery_empty_g_router.routes = documents_tm_only_delivery_empty_g_router.routes[
    0:1
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(documents_tm_only_delivery_empty_g_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
documents_tm_only_delivery_empty_g_router = _srp_piece_router


SEGMENTS_PAGE_SIZE = 200
# Consistency-lite (QA-1) is an O(n) casefold+split pass over EVERY segment on every editor
# render. Above this ceiling it is skipped with an honest note rather than taxing every page
# view of a very large document (reviewer L8).
CONSISTENCY_MAX_SEGMENTS = 2000


@router.get("/documents/{doc_id}")
def document_detail(
    doc_id: int,
    request: Request,
    locale: str | None = None,
    page: int = 1,
    session: Session = Depends(get_session),
):
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
    # #583 (gate): the document page shows the same "still being added" notice as the editor.
    from app.kernel import locale_seed

    locale_seeding = bool(selected) and locale_seed.in_progress(doc, selected)
    segments = sorted(
        [s for s in all_segments if s.target_locale == selected],
        key=lambda s: s.order_index,
    )

    # window the segment grid to keep large documents fast to render (the
    # live-translate SSE + MT/apply-tm actions still operate on the whole
    # document server-side, independent of what page is displayed)
    total_segments = len(segments)
    pages = max(1, -(-total_segments // SEGMENTS_PAGE_SIZE))
    page = min(max(1, page), pages)
    page_segments = segments[(page - 1) * SEGMENTS_PAGE_SIZE : page * SEGMENTS_PAGE_SIZE]

    # attach a TM match + glossary term hits to each segment for the grid. This is a
    # DISPLAY path (best-effort glossary sense hints), NOT an MT run site: a dead
    # engine:<id> ref must fail loud when you actually TRANSLATE (SSE/job/CLI), but it
    # must not 500 the editor grid. Resolve tolerantly — a dead ref degrades to the
    # offline mock for the sense-disambiguation hint only.
    try:
        provider = get_provider(project.default_provider if project else None)
    except DeadEngineError:
        provider = MockProvider()
    matches = tm.grid_matches(session, project, selected, [s.source_text for s in page_segments])
    rows = []
    for s in page_segments:
        terms = []
        if project and selected and selected != "—":
            terms = glossary.match_terms(
                session,
                doc.project_id,
                s.source_text,
                project.source_locale,
                selected,
                provider=provider,
            )
        rows.append({"seg": s, "tm": matches.get(s.source_text), "terms": terms})

    skeleton = doc.skeleton or {}
    me = security.session_user(request)
    # SPEC-context-metadata phase 4: show the EFFECTIVE (resolved) package + per-field
    # provenance — "what the model saw" — not just this document's stored override.
    # `doc_context` (the STORED doc override) still drives the edit form's pre-fill;
    # `effective`/`provenance`/`context_block` are the resolved view.
    from app.kernel import context_pkg

    doc_context = doc.context or {}
    # Resolve with the DOCUMENT's project org (review LOW) — matches the prompt-path
    # callers (translate.py / orchestrator.py resolve with project.org_id), so the
    # "what the model saw" view can never diverge from what the model actually saw,
    # even for a future cross-org admin preview. Equal to the viewer's org today
    # (the visibility gate above already requires same-org).
    effective, provenance = context_pkg.resolve(
        session, document=doc, project=project, org_id=getattr(project, "org_id", None)
    )
    context_block = context_pkg.render_block(effective)
    prov_labels = {f: context_pkg.provenance_label(l) for f, l in provenance.items()}  # noqa: E741
    # Style guide (Track B, B2/B3): resolve the guide the model would see for THIS doc's
    # scope + the selected pair, so the page can show "what the model sees" AND offer the
    # B3 style-guide A/B (mirrors the context block + its A/B action). Resolved with the
    # project's org (same as the prompt path) so the preview never diverges from the run.
    from app.kernel import style_guides

    _sg_guide = None
    _sg_formality = None
    if selected and selected != "—":
        _sg_guide = style_guides.resolve_style_guide(
            session,
            org_id=getattr(project, "org_id", None),
            customer_id=getattr(project, "customer_id", None),
            content_type_id=getattr(project, "content_type_id", None),
            source_locale=project.source_locale,
            target_locale=selected,
        )
        # #617 item 7 (ADR 0062): the preview shows the same block the run renders, so the
        # resolved MT formality sentence for this document + locale rides along (None -> as before).
        from app.kernel import formality as _formality

        _sg_formality = _formality.resolve_for(
            session, project=project, document=doc, target_locale=selected
        )
    style_guide_block = style_guides.render_style_guide(_sg_guide, _sg_formality)
    style_guide_name = (
        _sg_guide.name if _sg_guide is not None else ("formality" if style_guide_block else "")
    )
    # feat/run-doc-xliff-surfaces: when the SELECTED locale has an OPEN WIRE-LESS (file-
    # exchange, e.g. `manual`) vendor leg for THIS document -- DISPATCHED or QUOTED, keyed off
    # the leg's protocol SNAPSHOT -- offer the vendor hand-off from the page owner looked at
    # first (Segments): a Download-XLIFF link + a link into the run view's hand-off card (the
    # inline upload form lives there, keeping this button row uncluttered). Admin-only: the
    # download route is _admin_block-gated, so the affordance renders only for a caller who
    # would pass that gate (the route stays independently gated). None -> nothing renders.
    # round-2 finding #4: a doc+locale can have MORE THAN ONE open wire-less leg (e.g. parallel
    # routing lanes), so collect EVERY one (newest first) rather than only the newest — the
    # template renders one hand-off row per leg, each linking to ITS run's card. Each dict
    # carries `state` (round-2 finding #5) so the template can gate the run-view anchor to the
    # states where that card actually exists (dispatched/quoted).
    is_admin = (not security.auth_required()) or security.is_admin(me)
    wireless_handoffs = []
    if is_admin and selected and selected != "—":
        _open_leg = session.exec(
            select(VendorDispatch)
            .join(WorkflowRun, WorkflowRun.id == VendorDispatch.workflow_run_id)
            .where(
                WorkflowRun.document_id == doc_id,
                VendorDispatch.target_locale == selected,
                VendorDispatch.status.in_(
                    (VendorDispatchStatus.DISPATCHED, VendorDispatchStatus.QUOTED)
                ),
            )
            .order_by(VendorDispatch.id.desc())
        ).all()
        for _leg in _open_leg:
            if vendor_dispatch.leg_is_wireless(_leg):
                wireless_handoffs.append(
                    {
                        "vendor_id": _leg.vendor_id,
                        "dispatch_id": _leg.id,
                        "run_id": _leg.workflow_run_id,
                        "state": (
                            "quoted" if _leg.status == VendorDispatchStatus.QUOTED else "dispatched"
                        ),
                    }
                )
    # gate round-2 M1: offered + excluded (FULL_TEXT tm_match) in one picker call so the
    # run form can state the rule when the exclusion empties the picker.
    _run_form_workflows = _applicable_workflows(session, project, doc=doc, with_excluded=True)
    # #561: the engine THIS locale would translate with (a per-pair mapping can differ from
    # the project default), and whether it can run: live, the explicitly chosen offline
    # mock, or blocked (a keyless engine refuses to translate, it is never faked).
    from app.kernel import pair_providers

    try:
        engine_name = (
            pair_providers.resolve_provider(
                session, project, project.source_locale if project else None, selected
            )
            if project
            else config.DEFAULT_PROVIDER
        ) or "mock"
    except Exception:
        engine_name = (project.default_provider if project else None) or config.DEFAULT_PROVIDER
    doc_status = lifecycle.document_status(session, doc)
    engine_name = (engine_name or "mock").lower()
    engine_is_mock = engine_name == "mock"
    engine_live = engine_is_mock or provider_is_live(engine_name)
    return templates.TemplateResponse(
        request,
        "document.html",
        {
            "request": request,
            "doc": doc,
            "project": project,
            "doc_context": doc_context,
            "effective": effective,
            "provenance": provenance,
            "prov_labels": prov_labels,
            "context_block": context_block,
            "style_guide_block": style_guide_block,
            "style_guide_name": style_guide_name,
            "can_edit_context": (not security.auth_required()) or security.can_write(me),
            "segments": segments,
            "rows": rows,
            "locales": locales,
            "selected": selected,
            "chunk_plan": skeleton.get("chunk_plan", []),
            "est_tokens_total": skeleton.get("est_tokens_total", 0),
            "unit_count": len(skeleton.get("units", [])),
            "ocr_pages": skeleton.get("ocr_pages", 0),
            "max_chunk_tokens": config.MAX_CHUNK_TOKENS,
            "provider_live": engine_live and not engine_is_mock,
            "doc_status": doc_status,
            "doc_status_label": lifecycle.DOC_STATUS_LABELS.get(doc_status, doc_status),
            "engine_name": engine_name,
            "engine_is_mock": engine_is_mock,
            "engine_blocked": not engine_live,
            "workflows": _run_form_workflows[0],
            # #605: the picker preselects the workflow that matches this locale (or a neutral
            # one); None means every offered workflow is tagged for another language.
            "preferred_workflow_id": getattr(
                _queries_preferred_workflow(
                    _run_form_workflows[0], project.source_locale if project else None, selected
                ),
                "id",
                None,
            ),
            # gate round-2 M1: how many in-scope workflows the FULL_TEXT tm_match filter
            # hid, plus the rule text, so the run form can state the rule (and count)
            # instead of a false "No workflows yet" when the exclusion emptied the picker.
            # Fan-out batch default concurrency (SPEC-workflows-redesign Phase 5a): the
            # "Run all languages" form pre-fills this (clamped 1..8) from the
            # batch_max_parallel setting.
            "batch_default_parallel": batch_runs.default_max_parallel(),
            "runs": session.exec(
                select(WorkflowRun)
                .where(WorkflowRun.document_id == doc_id)
                .order_by(WorkflowRun.started_at.desc())
            ).all(),
            "page": page,
            "pages": pages,
            "total_segments": total_segments,
            "locale_seeding": locale_seeding,
            # SPEC-rename-delete UI: gates the rename form / Delete button — the
            # POST /documents/{id}/rename route is can_write, /delete is strictly
            # is_admin (every new delete is), so the buttons must match.
            "can_write": (not security.auth_required()) or security.can_write(me),
            "is_admin": is_admin,
            "wireless_handoffs": wireless_handoffs,
        },
    )


_srp_piece_router = documents_document_level_context_o_router
documents_document_level_context_o_router = _srp_copy(documents_document_level_context_o_router)
documents_document_level_context_o_router.routes = documents_document_level_context_o_router.routes[
    1:10
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(documents_document_level_context_o_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
documents_document_level_context_o_router = _srp_piece_router


# Issue #562: human badge labels for persisted flag types whose raw code is not readable.
# The live save response and the reloaded grid MUST show the same words, so both go through
# `editor_qa_flags` below (the segment save route imports it).
_QA_BADGE_LABELS = {
    quality.PLACEHOLDER_FLAG_TYPE: "placeholders",
    quality.REVIEW_HOLD_FLAG_TYPE: "check",
}


def editor_qa_flags(source_text, target_text, persisted_flags, locale) -> list:
    """The QA badges the Editor shows for one segment: the live structural checks plus the
    persisted commit-time families, with the dropped-placeholder double badge de-duped and
    human labels applied. ONE builder for the grid render AND the `POST /segments/{id}/save`
    response, so a badge reads the same right after a save and after a reload (#562)."""
    # Live structural QA checks + the persisted commit-time flags (roadmap gap
    # #1/#6): a QE-gated run stores a {type:"qe"} entry; the write-seam adherence
    # gates store {type:"dnt_violation"} / {type:"glossary_violation"} (SPEC 2B +
    # Track B/B1). Surface all three in the same qa cell so a reviewer sees WHY a
    # segment was routed to review — they share the {type,severity,message} shape
    # the generic qa-cell template loop renders, so no per-type special-casing is
    # needed here or in editor.html. Structural checks come first.
    # L2: thread the selected target locale so qa.punctuation_findings' locale
    # script-guard runs (and the number check resolves its decimal convention decisively
    # instead of the conservative ambiguous verdict).
    qa_flags = qa.check_segment(source_text, target_text, locale)
    # QA-5: the write-seam also stores a {type:"refusal"} entry when a segment was
    # quarantined as a suspected non-translation / model refusal, and a
    # {type:"nontranslation"} entry for the QA-3 language-mismatch signal. Without
    # them here a refusal-routed needs_review segment renders only a generic "No
    # translation yet" badge — visually indistinguishable from a never-translated
    # one, even though DNT/glossary/QE reasons show explanatory chips. All carry the
    # same {type,severity,message} shape the generic qa-cell loop renders, so
    # surfacing them is a one-set addition (no per-type template branch).
    # `source_fallback` (GitHub #42) joins the set: a FULL_TEXT paragraph reverted
    # to SOURCE leaves a structurally perfect body, so this badge is the ONLY thing
    # in the grid that says part of the document is untranslated.
    # LQA safety floor (C) joins the set: a `fix_reverted` segment holds the
    # PRE-fix text on purpose and a `fix_term_regression` FULL_TEXT unit keeps a
    # term-breaking edit — in both cases the chip is the only thing in the grid
    # that says an automated edit was adjudicated, so it must render.
    # GitHub #45 joins the set for the same reason: a `step_error` segment holds its
    # PRE-STEP text because the step that was meant to improve it failed, and the
    # chip is the only thing in the grid that says the run promised an improvement
    # this segment never received.
    # LC-7 placeholder_integrity joins the persisted set (distinct from the live
    # "tags" badge check_segment recomputes, exactly as number_integrity sits beside
    # the live "numbers" flag). The QA-1 `whitespace` seam family is DELIBERATELY NOT
    # here: check_segment already recomputes "whitespace" live above, so surfacing the
    # persisted twin would render an identical duplicate badge; the persisted flag is
    # surfaced on the headless detail API instead (api_cli._SEGMENT_SURFACED).
    _SURFACED = {
        "qe",
        # QE auto-fix (Train B): editor.html renders it as "auto-fixed in N rounds" /
        # "auto-fix tried N rounds" with the rounds openable.
        "qe_autofix",
        quality.DNT_FLAG_TYPE,
        quality.GLOSSARY_FLAG_TYPE,
        quality.TAG_FLAG_TYPE,
        quality.SOURCE_FALLBACK_FLAG_TYPE,
        quality.NUMBER_FLAG_TYPE,
        quality.PLACEHOLDER_FLAG_TYPE,
        # Decisions 11 / 14 / 16: no live twin in check_segment, so the persisted flag shows.
        quality.REVIEW_HOLD_FLAG_TYPE,
        quality.LABEL_ECHO_FLAG_TYPE,  # #664: minor note, label stripped from the target
        quality.STEP_ERROR_FLAG_TYPE,
        lqa_safety.FIX_REVERTED_FLAG_TYPE,
        lqa_safety.FIX_REGRESSION_FLAG_TYPE,
        refusal.FLAG_TYPE,
        refusal.NONTRANSLATION_FLAG_TYPE,
        *output_guard.OUTPUT_GUARD_FLAG_TYPES,
    }
    qe_flags = [
        f for f in (persisted_flags or []) if isinstance(f, dict) and f.get("type") in _SURFACED
    ]
    # L1 (refined per micro-round 2): de-dupe the dropped-placeholder double-badge
    # WITHOUT hiding a real HTML/URL loss. The live "tags" finding matches real
    # placeholders ({0}/%s) too, so a segment with a persisted placeholder_integrity
    # flag would show BOTH a "tags" MAJOR and a "placeholder_integrity" MAJOR naming the
    # same drop. Suppress the broad "tags" badge ONLY when the tag divergence names
    # NOTHING beyond what the placeholder verdict covers -- if a <b>/</b>/href/URL also
    # diverged, `nonplaceholder_tag_divergence` is non-empty and BOTH badges stay.
    if any(
        f.get("type") == quality.PLACEHOLDER_FLAG_TYPE for f in qe_flags
    ) and not quality.nonplaceholder_tag_divergence(source_text, target_text):
        qa_flags = [f for f in qa_flags if f.get("type") != "tags"]
    labelled = []
    for f in qe_flags:
        label = _QA_BADGE_LABELS.get(f.get("type"))
        labelled.append({**f, "label": label} if label and not f.get("label") else f)
    return qa_flags + labelled


# --- post-editing CAT editor ---
@router.get("/documents/{doc_id}/editor")
def editor(
    doc_id: int,
    request: Request,
    locale: str | None = None,
    page: int = 1,
    session: Session = Depends(get_session),
):
    # spec 2.5's editor carve-out, minus the trash document (A-F2): see `_editor_doc`.
    doc = _editor_doc(session, doc_id)
    if not doc:
        return _missing_block(request, session)
    project = session.get(Project, doc.project_id)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    all_segments = session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
    locales = sorted({s.target_locale for s in all_segments})
    selected = locale if locale in locales else (locales[0] if locales else None)
    # #583: a locale still being cloned (chunked add-a-language) has only some rows.
    from app.kernel import locale_seed

    locale_seeding = bool(selected) and locale_seed.in_progress(doc, selected)
    segments = sorted(
        [s for s in all_segments if s.target_locale == selected], key=lambda s: s.order_index
    )

    # window the grid so the post-editing editor stays responsive on large
    # documents (translate/save/approve/apply-tm are per-segment or whole-doc
    # server-side actions, unaffected by which page is displayed)
    total_segments = len(segments)
    pages = max(1, -(-total_segments // SEGMENTS_PAGE_SIZE))
    page = min(max(1, page), pages)
    page_segments = segments[(page - 1) * SEGMENTS_PAGE_SIZE : page * SEGMENTS_PAGE_SIZE]

    # Confirmed-progress count (SPEC-editor-ux #2): approved segments for THIS
    # doc+locale across ALL pages, not just the windowed page. `segments` already
    # holds the full locale set in memory, so this is a free in-Python count — no
    # extra query.
    approved_count = sum(1 for s in segments if s.status == SegStatus.APPROVED)

    # Consistency-lite (QA-1, document scope): same normalized source -> different
    # normalized target within THIS doc+locale. Computed in ONE pass over `segments`,
    # which the load above already holds fully in memory -- no new query, no N+1. Purely
    # INFORMATIONAL: it drives a per-segment "inconsistent" badge (added to the row's qa
    # cell below) and a document-level count in the summary; it never changes status,
    # gates a write, or forces TM-ineligibility. Skipped above CONSISTENCY_MAX_SEGMENTS
    # (reviewer L8) -- `inconsistent_count` is None then, and the summary says so.
    if total_segments <= CONSISTENCY_MAX_SEGMENTS:
        _inconsistent_ids = qa.inconsistent_segment_ids(
            (s.id, s.source_text, s.target_text) for s in segments
        )
        inconsistent_count = len(_inconsistent_ids)
        # Issue #617 item 6: which segments HAVE repeats (the editor offers the per-segment
        # propagation unlink only there). Same grouping key, same one pass, no new query.
        _repeat_ids = qa.repeated_segment_ids((s.id, s.source_text) for s in segments)
    else:
        _inconsistent_ids = set()
        inconsistent_count = None
        _repeat_ids = set()

    # Glossary term hints in the grid (SPEC-editor-ux #8): mirror document.html's
    # Terms column. Resolve the provider TOLERANTLY — a dead engine:<id> ref must
    # not 500 this DISPLAY path (sense-disambiguation hint only); it degrades to the
    # offline mock. Identical seam to document_detail above.
    try:
        provider = get_provider(project.default_provider if project else None)
    except DeadEngineError:
        provider = MockProvider()

    matches = tm.grid_matches(session, project, selected, [s.source_text for s in page_segments])
    # K8: a key value's char_limit surfaced in the CAT editor (spec §8). Batch-resolved
    # from the page's key_ids in ONE query (no N+1), NOT from Segment.meta -- that column
    # is reserved for the phase-2 per-unit override and nothing writes it yet. A document
    # segment (key_id None) resolves to no limit, so document projects are unchanged.
    from app.kernel import quality as _quality
    from app.models import StringKey

    _page_key_ids = {s.key_id for s in page_segments if s.key_id is not None}
    _char_limits: dict[int, int] = {}
    if _page_key_ids:
        for _sk in session.exec(select(StringKey).where(StringKey.id.in_(_page_key_ids))).all():
            _lim = _quality.coerce_char_limit(_sk.char_limit)
            if _lim is not None:
                _char_limits[_sk.id] = _lim
    # WAVE 3 §4.2 / M-8 debt (Train B deferred the count badge to the route owner):
    # a live open+answered thread-count per editor row, now that documents.py (Train C)
    # owns this route. ONE grouped query over THIS page's already-visible segments (no
    # N+1, no cross-tenant reach — the segments belong to the visibility-checked doc),
    # reached via the QueryThread MODEL (not the query_threads service, so §5.1's
    # cross_reach_allow scan is not implicated — the same "model reach" seam §5.2's
    # fourth point describes). Computed ONLY when queries is enabled, and the template
    # ALSO gates on feature_enabled('queries'), so the badge fully vanishes when the
    # kill switch is off (the §5.2 affordance-render probe).
    from app import features as _features

    query_counts: dict = {}
    if page_segments and _features.is_enabled("queries"):
        from sqlalchemy import func as _sqlfunc

        from app.models import QueryStatus, QueryThread

        _seg_ids = [s.id for s in page_segments]
        for _sid, _n in session.exec(
            select(QueryThread.segment_id, _sqlfunc.count(QueryThread.id))
            .where(
                QueryThread.segment_id.in_(_seg_ids),
                QueryThread.status.in_((QueryStatus.OPEN, QueryStatus.ANSWERED)),
            )
            .group_by(QueryThread.segment_id)
        ).all():
            query_counts[_sid] = _n
    # VPAT 504.3 prompt: an image whose alt text is missing or empty in the SOURCE,
    # recorded at ingest (skeleton["a11y_alt_flags"], formats with images only), is
    # shown on the segment it sits in or next to. A minor warning, never a blocker.
    from app.filters import a11y as _a11y

    _alt_prompts = _a11y.alt_prompt_flags(doc.skeleton)
    rows = []
    for s in page_segments:
        qa_and_qe_flags = editor_qa_flags(s.source_text, s.target_text, s.qa_flags, selected)
        terms = []
        if project and selected and selected != "—":
            terms = glossary.match_terms(
                session,
                doc.project_id,
                s.source_text,
                project.source_locale,
                selected,
                provider=provider,
            )
        # Consistency-lite badge: a {type,severity,message} entry the generic qa-cell loop
        # renders with no template branch (like every other badge). Minor + informational.
        inconsistent = (
            [
                {
                    "type": "inconsistent",
                    "severity": "minor",
                    "message": "Same source translated differently elsewhere in this document",
                }
            ]
            if s.id in _inconsistent_ids
            else []
        )
        # Issue #617 item 6: text copied here from an approved repeat. Informational, same
        # generic qa-cell badge; it clears when a human edits or approves the segment.
        _prop_from = (
            (s.meta or {}).get("propagated_from") if segment_review.is_propagated_copy(s) else None
        )
        propagated = (
            [
                {
                    "type": "propagated",
                    "severity": "minor",
                    "label": deps.t(request, "js.prop_badge"),
                    "message": deps.t(request, "js.prop_badge_msg", n=_prop_from),
                }
            ]
            if _prop_from is not None
            else []
        )
        rows.append(
            {
                "seg": s,
                # #617 item 5: set below from ONE batched drift query for the page.
                "tm_changed": False,
                "tm": matches.get(s.source_text),
                "has_repeats": s.id in _repeat_ids,
                "unlinked": bool((s.meta or {}).get("no_propagate")),
                "qa": qa_and_qe_flags
                + inconsistent
                + propagated
                + _alt_prompts.get(s.order_index, []),
                "terms": terms,
                "char_limit": _char_limits.get(s.key_id),
            }
        )
    # SPEC-context-metadata phase 4: the linguist sees "what the model saw" — the
    # EFFECTIVE resolved doc-context block — as a compact read-only affordance, plus
    # any per-SEGMENT unit fields (Segment.meta: char_limit/key/notes) surfaced inline
    # when present (display only; nothing writes Segment.meta yet — forward-looking).
    from app.kernel import context_pkg

    # Resolve with the document's project org (review LOW) to match the prompt-path
    # callers — "what the model saw" cannot diverge from what the model saw.
    effective, _prov = context_pkg.resolve(
        session, document=doc, project=project, org_id=getattr(project, "org_id", None)
    )
    context_block = context_pkg.render_block(effective)
    # #617 item 5: "TM changed since fill" badge for locked segments whose TM entry was edited
    # after the fill (one query for the page's locked rows).
    from app.kernel import tm_lock as _tm_lock

    _drifted = _tm_lock.drifted(session, page_segments)
    for _row in rows:
        _row["tm_changed"] = _row["seg"].id in _drifted
    # #617 item 4: a run parked on a human_review step holds its machine translation in the
    # pause, not in the segments. Show that pending text read-only with a banner naming the run
    # (Approve and resume still happen on the run page), and withhold the edit/approve controls
    # so nothing is saved over what the run will resume with.
    from app.kernel import orchestrator as _orchestrator

    waiting_run = (
        _orchestrator.waiting_human_review(session, doc_id, selected) if selected else None
    )
    if waiting_run is not None:
        _pending = waiting_run["segments"]
        _aligned = len(_pending) == total_segments
        _pending_by_seg = {s.id: _pending[i] for i, s in enumerate(segments)} if _aligned else {}
        for _row in rows:
            _row["pending"] = _pending_by_seg.get(_row["seg"].id, _row["seg"].target_text)
    return templates.TemplateResponse(
        request,
        "editor.html",
        {
            "request": request,
            "doc": doc,
            "project": project,
            # AI opt-out (DPAI-08): the Editor hides its AI helpers and says why.
            "ai_off": _ai_optout.is_opted_out(session, project),
            "rows": rows,
            "locales": locales,
            "selected": selected,
            "source_locale": project.source_locale,
            "page": page,
            "pages": pages,
            "total_segments": total_segments,
            "approved_count": approved_count,
            # #605: the intro line may say "pre-translated" only when some target exists.
            "has_targets": any((s.target_text or "").strip() for s in segments),
            "inconsistent_count": inconsistent_count,
            "context_block": context_block,
            "query_counts": query_counts,
            "locale_seeding": locale_seeding,
            "waiting_run": waiting_run,
        },
    )


_srp_piece_router = documents_document_level_context_o_router
documents_document_level_context_o_router = _srp_copy(documents_document_level_context_o_router)
documents_document_level_context_o_router.routes = documents_document_level_context_o_router.routes[
    10:15
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(documents_document_level_context_o_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
documents_document_level_context_o_router = _srp_piece_router


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(documents_part3_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


@router.post("/segments/{seg_id}/tag-repair")
def segment_tag_repair(seg_id: int, request: Request, session: Session = Depends(get_session)):
    """AI Tagging 1.5c, the HUMAN-REVIEW half: repair one reviewed segment's broken
    inline tags on demand.

    The web twin of `loc-tms tagging repair --segment N --apply`; both are thin callers
    of `tag_llm_repair.repair_stored_segments`, so the two surfaces cannot drift (repo
    parity law). Same RBAC predicate as every other segment mutation
    (`_visibility_block`), and the same three locks + write-gate re-verification as the
    inline rung — this endpoint cannot write a target the gate would reject.

    Always `apply=True`: a reviewer clicking "repair" is asking for the write. The
    response reports whether placement was VERIFIED, because that is what decides
    whether the segment left the review queue or is still waiting on their eyes."""
    seg = session.get(Segment, seg_id)
    if not seg:
        return _missing_or(
            request,
            session,
            JSONResponse({"ok": False, "error": "no such segment"}, status_code=404),
        )
    doc = session.get(Document, seg.document_id)
    project = session.get(Project, doc.project_id)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    # AI opt-out (DPAI-08): refuse before any work when the project opted out.
    from app.kernel import ai_optout

    ai_optout.refuse(session, project, "AI tag repair")
    # A SOFT-DELETED key's parked value row is read-only on every surface (fix round
    # 2, W5): this route runs `apply=True` and WRITES `target_text`, so without the
    # guard it edited a tombstoned key's content — silently, since a deleted key
    # emits no `value` entries at all.
    blocked = _deleted_key_block(session, seg)
    if blocked:
        return blocked
    # #617 item 4 (gate): the same 409 as the editor save while a run waits on human review.
    from app.kernel import segment_review

    try:
        segment_review.refuse_if_awaiting_review(session, seg.document_id, seg.target_locale)
    except segment_review.AwaitingReviewError as exc:
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": str(exc)}, status_code=exc.status
        )
    if not tag_llm_repair.enabled():
        return JSONResponse({"ok": False, "error": "AI tag repair is turned off."}, status_code=400)
    rep = tag_llm_repair.repair_stored_segments(session, segment_id=seg_id, apply=True, limit=1)
    row = (rep["segments"] or [None])[0]
    if row is None:
        return JSONResponse(
            {"ok": False, "error": "this segment's inline tags are not damaged."}, status_code=400
        )
    if not row["repaired"]:
        return JSONResponse(
            {
                "ok": False,
                "segment_id": seg_id,
                "error": "no certified repair was possible; the segment stays in review.",
            },
            status_code=200,
        )
    session.refresh(seg)
    return JSONResponse(
        {
            "ok": True,
            "segment_id": seg_id,
            "repaired": True,
            "verified": row["verified"],
            "note": row["note"],
            "target_text": seg.target_text,
            "status": str(seg.status),
        }
    )


_srp_piece_router = documents_document_level_context_o_router
documents_document_level_context_o_router = _srp_copy(documents_document_level_context_o_router)
documents_document_level_context_o_router.routes = documents_document_level_context_o_router.routes[
    15:16
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(documents_document_level_context_o_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
documents_document_level_context_o_router = _srp_piece_router


@router.post("/segments/{seg_id}/alternates")
def segment_alternates(seg_id: int, request: Request, session: Session = Depends(get_session)):
    """Three LLM alternatives for one segment.

    Guarded by `_deleted_key_block` (fix round 2, W5): it writes nothing, but it
    SENDS a soft-deleted key's parked source to a provider three times and echoes it
    straight back in the response — a read of content the grid renders read-only and
    the editor refuses to open, plus provider spend on a string the customer just
    deleted."""
    seg = session.get(Segment, seg_id)
    if not seg:
        return _missing_or(request, session, JSONResponse({"alternates": []}))
    doc = session.get(Document, seg.document_id)
    project = session.get(Project, doc.project_id)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    # AI opt-out (DPAI-08): refuse before any work when the project opted out.
    from app.kernel import ai_optout

    ai_optout.refuse(session, project, "AI alternatives")
    blocked = _deleted_key_block(session, seg)
    if blocked:
        return blocked
    from app.kernel import tags_only as _tags_only

    if _tags_only.is_tags_only(seg.source_text):
        # #705: no translatable text (only tags / placeholders): the copy is the only right
        # answer, and asking an engine for three of it is spend for nothing.
        return JSONResponse({"alternates": [seg.source_text]})
    spec = ProviderSpec(
        provider=project.default_provider, model=config.DEFAULT_MODELS.get(project.default_provider)
    )
    prompt = (
        f"Give 3 distinct alternative {project.source_locale}->{seg.target_locale} "
        f"translations of:\n{seg.source_text}\nReturn ONLY a JSON array of strings."
    )
    res = run_completion(
        spec,
        prompt,
        system="Output only a JSON array of strings.",
        task="alternates",
        payload={"text": seg.source_text, "target": seg.target_locale, "n": 3},
    )
    try:
        alts = json.loads(res["text"])
        if not isinstance(alts, list):
            alts = [str(alts)]
    except Exception:
        alts = [res["text"]] if res["text"] else []
    return JSONResponse({"alternates": alts})


_srp_piece_router = documents_document_level_context_o_router
documents_document_level_context_o_router = _srp_copy(documents_document_level_context_o_router)
documents_document_level_context_o_router.routes = documents_document_level_context_o_router.routes[
    16:18
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(documents_document_level_context_o_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
documents_document_level_context_o_router = _srp_piece_router


_srp_piece_router = documents_tm_only_delivery_empty_g_router
documents_tm_only_delivery_empty_g_router = _srp_copy(documents_tm_only_delivery_empty_g_router)
documents_tm_only_delivery_empty_g_router.routes = documents_tm_only_delivery_empty_g_router.routes[
    1:3
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(documents_tm_only_delivery_empty_g_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
documents_tm_only_delivery_empty_g_router = _srp_piece_router


@router.post("/documents/{doc_id}/lqa/findings")
async def lqa_finding_log(doc_id: int, request: Request, session: Session = Depends(get_session)):
    """Log ONE reviewer-authored MQM finding on a segment of this document (§6.2/§6.5).
    REVIEWER+; the segment must belong to this (visible) document.

    RING BEFORE BODY (validation-before-authorization guard, the query-surface H-1 /
    budget-route class): the document-visibility ring + the role gate run BEFORE any form
    parsing, so a FOREIGN document 404s identically whether or not the body is valid — a
    422 must never precede the ring and leak that the doc exists. This is why the form is
    read manually here rather than via `Form(...)` params (FastAPI validates those before
    the handler body runs)."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_review(me):
        return _error_page(
            request, 403, "logging an LQA finding requires the reviewer role (or higher)."
        )
    form = await request.form()
    try:
        segment_id = int(form.get("segment_id"))
    except (TypeError, ValueError):
        return _error_page(request, 400, "a segment_id is required.")
    dimension = str(form.get("dimension") or "")
    severity = str(form.get("severity") or "")
    note = str(form.get("note") or "")
    seg = session.get(Segment, segment_id)
    if not seg or seg.document_id != doc_id:
        return _error_page(request, 404, "no such segment on this document.")
    try:
        row = lqa.log_human_finding(
            session,
            segment=seg,
            dimension=dimension,
            severity=severity,
            note=note,
            user=me,
            actor=_actor_email(request),
        )
    except (lqa.HumanFindingError, lqa.HumanFindingForbidden) as exc:
        return _lqa_finding_error(request, exc)
    audit.log(
        actor=_actor_email(request),
        action="lqa.finding.log",
        target=f"finding:{row.id}",
        detail=f"doc={doc_id} seg={seg.id} {row.dimension}/{row.severity}",
        ip=_ip(request),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    return RedirectResponse(f"/documents/{doc_id}/lqa?locale={row.target_locale}", status_code=303)


@router.post("/lqa/findings/{fid}")
async def lqa_finding_edit(fid: int, request: Request, session: Session = Depends(get_session)):
    """Edit a reviewer finding's dimension/severity/note (§6.5). REVIEWER+ AND (own-row OR
    can_manage+). A machine row is refused. Out-of-scope document -> 404 (non-enumerable).
    RING BEFORE BODY (see lqa_finding_log): the finding/doc-visibility ring + role gate run
    before form parsing, so a foreign finding 404s regardless of body validity."""
    from app.models import LQAResult

    finding = session.get(LQAResult, fid)
    if not finding:
        return _missing_or(request, session, _error_page(request, 404, "no such finding."))
    doc = _visible_doc(session, finding.document_id)
    if not doc:
        return _missing_or(request, session, _error_page(request, 404, "no such finding."))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_review(me):
        return _error_page(
            request, 403, "editing an LQA finding requires the reviewer role (or higher)."
        )
    form = await request.form()
    dimension = str(form.get("dimension") or "")
    severity = str(form.get("severity") or "")
    note = str(form.get("note") or "")
    try:
        lqa.edit_human_finding(
            session,
            finding=finding,
            dimension=dimension,
            severity=severity,
            note=note,
            user=me,
            actor=_actor_email(request),
        )
    except (lqa.HumanFindingError, lqa.HumanFindingForbidden) as exc:
        return _lqa_finding_error(request, exc)
    audit.log(
        actor=_actor_email(request),
        action="lqa.finding.edit",
        target=f"finding:{fid}",
        detail=f"doc={finding.document_id}",
        ip=_ip(request),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    return RedirectResponse(
        f"/documents/{finding.document_id}/lqa?locale={finding.target_locale or ''}",
        status_code=303,
    )


@router.post("/lqa/findings/{fid}/withdraw")
def lqa_finding_withdraw(fid: int, request: Request, session: Session = Depends(get_session)):
    """Soft-withdraw a reviewer finding (§6.7 — stamps withdrawn_at, never deletes).
    REVIEWER+ AND (own-row OR can_manage+)."""
    from app.models import LQAResult

    finding = session.get(LQAResult, fid)
    if not finding:
        return _missing_or(request, session, _error_page(request, 404, "no such finding."))
    doc = _visible_doc(session, finding.document_id)
    if not doc:
        return _missing_or(request, session, _error_page(request, 404, "no such finding."))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_review(me):
        return _error_page(
            request, 403, "withdrawing an LQA finding requires the reviewer role (or higher)."
        )
    doc_id, loc = finding.document_id, (finding.target_locale or "")
    try:
        lqa.withdraw_human_finding(session, finding=finding, user=me, actor=_actor_email(request))
    except (lqa.HumanFindingError, lqa.HumanFindingForbidden) as exc:
        return _lqa_finding_error(request, exc)
    audit.log(
        actor=_actor_email(request),
        action="lqa.finding.withdraw",
        target=f"finding:{fid}",
        detail=f"doc={doc_id}",
        ip=_ip(request),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    return RedirectResponse(f"/documents/{doc_id}/lqa?locale={loc}", status_code=303)


_srp_piece_router = documents_tm_only_delivery_empty_g_router
documents_tm_only_delivery_empty_g_router = _srp_copy(documents_tm_only_delivery_empty_g_router)
documents_tm_only_delivery_empty_g_router.routes = documents_tm_only_delivery_empty_g_router.routes[
    3:8
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(documents_tm_only_delivery_empty_g_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
documents_tm_only_delivery_empty_g_router = _srp_piece_router
