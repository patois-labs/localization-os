"""documents: part3

Documented in: docs/architecture/translation-pipeline.md
"""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app import db
from app import extensions as _extensions
from app.db import get_session
from app.kernel import (
    postedit,
    qe,
    quality,
    segment_review,
)
from app.models import (
    Document,
    Segment,
    SegStatus,
    TransMode,
)
from app.web.deps import (
    _actor_email,
    _ip,
    _missing_or,
    _visibility_block,
)
from app.web.documents_document_level_context_o import _deleted_key_block, _fulltext_residual_reason

# Optional add-on: reached through the extension facade (no-op when absent); name unchanged.
llmemory = _extensions.use("memory")

_logger = logging.getLogger("loc_tms")


router = APIRouter()


def _same_text(posted: str, stored: str | None) -> bool:
    """Is a posted editor value the stored target? A browser textarea hands back LF for every
    line break (CR LF and lone CR are normalised on load), so a stored CRLF target (an imported
    Windows file) would never equal its own untouched round trip and #601 would persist for it.
    Only line-break style is ignored: any other difference is a real edit."""

    def norm(t: str) -> str:
        return t.replace("\r\n", "\n").replace("\r", "\n")

    return norm(posted) == norm(stored or "")


@router.post("/segments/{seg_id}/save")
def segment_save(
    seg_id: int,
    request: Request,
    target_text: str = Form(""),
    tte_ms: str = Form("0"),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
):
    seg = session.get(Segment, seg_id)
    if not seg:
        return _missing_or(request, session, JSONResponse({"ok": False}, status_code=404))
    doc = session.get(Document, seg.document_id)
    blocked = _visibility_block(request, session, doc.project_id if doc else None)
    if blocked:
        return blocked
    # A-F2/B-4: a SOFT-DELETED key's value row is not writable here either. The key
    # grid renders it read-only and `keys.set_value` refuses it; without this the
    # editor was a second, silent way in (a tombstoned key emits no `value` entries,
    # so the write left no trace in the delta feed at all).
    blocked = _deleted_key_block(session, seg)
    if blocked:
        return blocked
    # #617 item 4: while a run waits on a human review step the editor is read-only; refuse the
    # write here too (a stale tab or a script), after the visibility gate so it is no oracle.

    if segment_review.awaiting_review_run(session, seg) is not None:
        return JSONResponse(
            {
                "ok": False,
                "error": "This document is waiting for a human review step; approve and "
                "resume the run instead of editing here.",
            },
            status_code=409,
        )
    # AI Tagging 1.7 — editor save guard. A human save that DROPS / reorders / breaks
    # the inline-formatting tokens would silently collapse formatting at export, the
    # same failure a bad MT step causes. Return a BLOCKING confirmation naming the
    # specific damage rather than a passive chip; the save proceeds only on an explicit
    # re-submit with confirm=1 (non-destructive — the operator can also fix the tokens).
    # Skipped for an empty target (the operator is clearing the field, status -> NEW).
    # Conservative quarantine semantics (advisory dropped, GitHub #42): ANY divergence
    # blocks, the same predicate `export.paragraph_texts_for` and
    # `fulltext_paragraphs._rows` use.
    #
    # FULL_TEXT SHARPNESS (review MEDIUM). In full_text mode the segment IS the whole
    # document, and the whole-body `tag_damage` is BLIND to a paragraph-local emptied
    # span hidden by a same-named token in another paragraph — token ids restart at 1 per
    # paragraph and `segment.paired_span_bodies` keeps only the LAST body per name. So
    # editing the giant unit could save an export-BLOCKED body, land it EDITED, and CLEAR
    # a valid tag_integrity badge, the soft surface `save_paragraph` forbids. OR the
    # whole-body verdict with the per-paragraph residual (mirrors
    # `fulltext_paragraphs._rows`), so the editor blocks/flags/keeps-badge on exactly what
    # export and the split view do.
    tag_reason = ""
    if target_text.strip():
        tag_reason = quality.tag_damage(seg.source_text, target_text)
        if not tag_reason and doc is not None and doc.mode == TransMode.FULL_TEXT:
            tag_reason = _fulltext_residual_reason(doc, target_text)
    if tag_reason and confirm not in ("1", "true", "yes"):
        return JSONResponse(
            {
                "ok": False,
                "needs_confirm": True,
                "reason": tag_reason,
                "message": "This save breaks inline formatting tags "
                f"({tag_reason}) — formatting will be lost on "
                "export. Save anyway?",
            }
        )
    # Issues #601 / #609: a save whose text equals the stored target changes NOTHING. The
    # editor saves on every blur, so moving focus through a row (or off an approved row after
    # Ctrl+Enter) used to flip an untouched MT row to EDITED and an APPROVED row back to
    # EDITED (the confirmed count then read 0 after a reload). State follows the text: no
    # text change, no status change, no capture, no lock clear. Placed AFTER the tag guard so
    # an unchanged but tag-damaged body is still asked about and judged exactly as before.
    if not tag_reason and _same_text(target_text, seg.target_text):
        return JSONResponse(
            {
                "ok": True,
                "status": seg.status,
                # Nothing changed, so the editor leaves the row's origin chip ("TM" for a
                # 100% TM fill, which is stored as status "mt") exactly as it is.
                "unchanged": True,
                "tm_fill": bool(
                    seg.status == SegStatus.MT
                    and seg.lock_band
                    and str(seg.lock_band).lower() != "fuzzy"
                ),
                "qa": _editor_qa_for(seg) + _alt_prompts_for(session, seg),
            }
        )
    # LLMemory capture seam 1 (PROPOSAL-llmemory §4.1). The reviewer's PREVIOUS target
    # is the `before` half of the correction pair, and the very next statement destroys
    # it — so it is snapshotted HERE, above the overwrite, not read back afterwards.
    # Recorded after the commit below (a memory event must never describe a save that
    # did not happen).
    _mem_before = seg.target_text or ""
    # Post-edit capture seam 1 (SPEC-wave2 §3.2.4). Snapshot the PRE-WRITE state HERE,
    # before the status rewrite at :974-980 (which can leave the segment NEW/NEEDS_REVIEW)
    # and before the lock clear at :1052-1055 — `classify_baseline` needs the target's
    # ORIGIN, not the post-save status.
    _pe_pre_status = seg.status
    _pe_pre_tm_pct = seg.tm_match_pct
    _pe_pre_lock_band = seg.lock_band
    seg.target_text = target_text
    # Issue #617 item 6: a human edit makes this text the linguist's own, no longer "a copy of
    # segment N" (the propagated badge goes, and a later propagation will not overwrite it).
    segment_review.clear_propagation_marker(seg)
    # A save known to break tags (a CONFIRMED override) routes to review, not EDITED —
    # an override that reads EDITED with a "tags broken" badge is the inconsistency #42
    # is about, and it mirrors `save_paragraph`'s posture.
    if not target_text.strip():
        seg.status = SegStatus.NEW
    elif tag_reason:
        seg.status = SegStatus.NEEDS_REVIEW
    else:
        seg.status = SegStatus.EDITED
    # tte_ms is a request-supplied form scalar: str.isdigit() is True for a Unicode digit
    # int() rejects (U+00B2 -> ValueError), and a >2**63 value overflows the SQLite bind
    # when accumulated into the int column -- guard + bound so a malformed value is simply
    # ignored (no time added), never a 500 on a segment save.
    try:
        _tte = int(tte_ms)
    except (TypeError, ValueError):
        _tte = 0
    if 0 < _tte < 2**63:
        seg.tte_ms = (seg.tte_ms or 0) + _tte
    # Clear a stale QE "routed to review" badge: the reviewer just acted on this
    # segment (edited it), so the old gate decision no longer describes it — the
    # editor must stop showing "routed to review" for a fixed segment.
    seg.qa_flags = qe.clear_qe_flag(seg.qa_flags)
    # ...and the SAME courtesy for the tag_integrity badge (GitHub #42: there was no
    # route to clear it, so a hand-repaired segment kept its "tags broken" badge
    # forever). A human save is the confirmation event: on text that no longer diverges
    # the badge goes; a CONFIRMED damaging save keeps a major badge naming the real
    # break, re-derived from the text actually written so it can never describe a
    # previous version of the segment.
    seg.qa_flags = (
        quality.merge_tag_flags(seg.qa_flags, tag_reason)
        if tag_reason
        else quality.clear_tag_flags(seg.qa_flags)
    )
    # K8 (spec §2a) — the CHAR-LIMIT family gets the SAME courtesy as the qe/tag badges
    # above, and this save is the one write seam that was missing it. Without it a
    # machine-set char_limit flag SURVIVES a human fix verbatim (project_keys.html then
    # suppresses the derived over-limit marker because a stored flag "already says the same
    # thing" — so the grid badge describes text that no longer exists), and a human-
    # introduced violation gets NO flag while the identical edit through `keys.set_value`'s
    # human branch IS flagged. Re-judge the text ACTUALLY written and clear/merge to match
    # (the merge_number_flags pattern). Status is deliberately left as set above: human text
    # is content and is never gated (the human/machine asymmetry keys.set_value documents).
    # A document segment (`key_id` None) resolves to no limit and is byte-identical.
    _cl_limit = quality.segment_char_limit(session, seg)
    _cl_reason = quality.char_limit_violation(target_text, _cl_limit)
    if _cl_reason:
        seg.qa_flags = quality.merge_char_limit_flags(
            seg.qa_flags,
            _cl_reason,
            quality.char_count(target_text),
            quality.coerce_char_limit(_cl_limit),
        )
    else:
        seg.qa_flags = quality.clear_char_limit_flags(seg.qa_flags)
    # LC-7 / QA-1 (reviewer M1): the placeholder_integrity and whitespace families get the
    # SAME courtesy as the qe/tag/char_limit badges above -- this human save is the one write
    # seam that was missing it, so a machine-set badge would SURVIVE a human fix forever.
    # Re-judge the text ACTUALLY written and clear/merge to match (the tag_reason pattern):
    # a confirmed still-broken save keeps a badge naming the real break, a fixed one loses it.
    # Status stays as set above -- human text is content, never gated. Whitespace has no mock
    # concern here (this is the human's literal text, not a provider echo), so it is judged
    # unconditionally, unlike the lang_guard-gated write seam.
    _ph_reason = quality.placeholder_violation(seg.source_text, target_text, segment=seg)
    seg.qa_flags = (
        quality.merge_placeholder_flags(seg.qa_flags, _ph_reason)
        if _ph_reason
        else quality.clear_placeholder_flags(seg.qa_flags)
    )
    _ws_reason = quality.whitespace_violation(seg.source_text, target_text)
    seg.qa_flags = (
        quality.merge_whitespace_flags(seg.qa_flags, _ws_reason)
        if _ws_reason
        else quality.clear_whitespace_flags(seg.qa_flags)
    )
    # Decisions 11 / 14 / 16: a person wrote this text, so the review-hold badge (ambiguous
    # date, line breaks, glued words) has been looked at; it is cleared, never re-judged
    # (a translator keeping 10/11/2026 on purpose must not be held again on every save).
    seg.qa_flags = quality.clear_review_hold_flags(seg.qa_flags)
    # number_integrity (maintainer-authorized micro-round-2 extension; the GitHub #42 defect
    # class): a human who FIXED a corrupted figure had NO route to clear the persisted
    # number_integrity badge -- the seam only ever cleared it on a later MACHINE commit, so a
    # human fix left a MAJOR "number value changed" badge describing text that no longer
    # exists. Recompute number_violation against the saved text with the segment's OWN target
    # locale (the L2-threaded locale, matching seam semantics so "25.000" is read as es-ES
    # would) and clear-or-merge like the families above. Status stays as set -- human text is
    # never gated.
    _num_reason = quality.number_violation(seg.source_text, target_text, seg.target_locale)
    seg.qa_flags = (
        quality.merge_number_flags(seg.qa_flags, _num_reason)
        if _num_reason
        else quality.clear_number_flags(seg.qa_flags)
    )
    # item 5a (PROPOSAL-tm-matching §5): a human CAT-editor save of a TM-LOCKED segment
    # WINS -- this is a direct writer (human text is content, never gated), so it CLEARS
    # the lock + provenance and audits it, rather than leaving a band=100 / hard=True
    # claim over text the reviewer just replaced. Gated on an ACTUAL text change (MED-10:
    # `_mem_before` is the pre-save target) so opening + saving WITHOUT editing never
    # destroys a lock with an audit row claiming an edit that did not happen.
    if target_text != _mem_before:
        from app.kernel import tm_lock

        tm_lock.clear_lock_on_human_edit(session, seg, actor=_actor_email(request), ip=_ip(request))
    session.add(seg)
    session.commit()
    # LLMemory capture seam 1, continued. ONLY when the text ACTUALLY CHANGED: the
    # editor posts this endpoint on approve/confirm too, and an approve-without-edit is
    # a reviewer saying "this was already right" — recording it as a correction would
    # feed the distiller identical before/after pairs and dilute its evidence
    # thresholds. `record_segment_edit` re-checks the no-op itself (defence in depth),
    # and is a hard no-op when `llmemory_capture_enabled` is off, so this line adds no
    # behaviour to an install with the switch down. Guarded: capture is an OBSERVER of
    # the save, never a participant — a memory failure must not turn a committed save
    # into a 500 for the reviewer.
    if (seg.target_text or "") != _mem_before:
        try:
            llmemory.record_segment_edit(
                session, seg, _mem_before, seg.target_text or "", actor=_actor_email(request)
            )
        except Exception:
            _logger.warning("llmemory: segment_edit capture failed", exc_info=True)
        # Post-edit capture seam 1 (SPEC-wave2 §3.2.4) — an in-house CAT save. Runs AFTER
        # the llmemory call so that call's ordering is byte-identical, and inside the
        # change-gated branch so a no-op approve records nothing. `capture_guarded` is
        # savepoint-wrapped (never raises, never poisons this committed session — the
        # llmemory capture that just ran is likewise protected). This seam runs POST-commit
        # (the seg save committed at :1057) and the llmemory switch may be off (no commit of
        # its own), so the PE row needs its OWN commit here — but through `commit_or_shed`
        # (gate H2), NOT a bare commit: a writer-lock on a bare commit would 500 the request
        # AFTER the save already committed, violating invariant 6 (capture never fails a
        # committed save). `commit_or_shed` sheds a transient lock and drops only the PE row.
        # unit_kind follows the document mode (L4). Only the SEGMENTED CAT save is web-only
        # (§6); full-text + vendor post-edits are captured on every surface.
        pe_row = postedit.capture_guarded(
            session,
            segment=seg,
            pre=_mem_before,
            post=seg.target_text or "",
            actor_kind="human",
            actor=_actor_email(request),
            unit_kind=("full_text" if (doc and doc.mode == TransMode.FULL_TEXT) else "segment"),
            target_locale=seg.target_locale,
            pre_status=_pe_pre_status,
            tm_match_pct=_pe_pre_tm_pct,
            lock_band=_pe_pre_lock_band,
        )
        # M5: pass a `stage=` re-add. commit_with_retry's rollback on a transient lock
        # EXPUNGES the pending PE INSERT (db.py:1289-1297), so a bare re-commit would
        # persist NOTHING while returning True (silent loss). Re-staging the row makes the
        # retry re-apply it — the llmemory._add_event pattern. keep_loaded keeps its PK
        # readable without a follow-up SELECT that could itself lose the lock. (A rare
        # continuation-episode UPDATE that loses the writer lock re-adds the row but not the
        # reverted in-memory increment — acceptable best-effort degradation; the row survives.)
        if pe_row is not None and not db.commit_or_shed(
            session, stage=lambda: session.add(pe_row), keep_loaded=True
        ):
            _logger.warning(
                "postedit: segment-save PE capture may not have persisted "
                "under sustained write contention (the save is unaffected)"
            )
    return JSONResponse(
        {
            "ok": True,
            "status": seg.status,
            # L2: thread the segment's target locale (punctuation script-guard +
            # decisive number verdict), matching the grid render.
            # #562: the SAME builder the reloaded grid uses (live checks + the persisted
            # flags this save just re-judged, de-duped and labelled), so a badge reads the
            # same right after a save as after a reload.
            "qa": _editor_qa_for(seg) + _alt_prompts_for(session, seg),
        }
    )


def _editor_qa_for(seg) -> list:
    from app.web.documents import editor_qa_flags

    return editor_qa_flags(seg.source_text, seg.target_text, seg.qa_flags, seg.target_locale)


def _alt_prompts_for(session, seg) -> list:
    """The VPAT 504.3 missing-alt prompt for this segment (the same flags the grid
    renders), so a save does not make it vanish from the row. Never raises."""
    try:
        from app.filters import a11y
        from app.models import Document

        doc = session.get(Document, seg.document_id)
        return a11y.alt_prompt_flags(doc.skeleton if doc else None).get(seg.order_index, [])
    except Exception:
        return []
