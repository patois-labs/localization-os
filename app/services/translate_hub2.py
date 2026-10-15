"""translate: second pass part 2 (layered pure move)

Documented in: docs/architecture/translation-pipeline.md
"""

import functools
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlmodel import Session, select

from app import db as _db
from app import extensions as _extensions
from app.models import Document, Project, Segment, SegStatus, TransMode
from app.services import (
    ai_tagging,
    context_pkg,
    formality,
    pii,
    prompts,
    qa,
    quality,
    style_guides,
    system_prompt_overrides,
    tag_llm_repair,
    tap_envelope,
    tm,
)
from app.services.prompts import translation_prompt as build_prompt
from app.services.providers import ProviderSpec, run_completion
from app.services.translate_common import (
    DocumentNotFound,
    _mask_aux,
    _mt_workers,
    _new_stats,
)
from app.services.translate_hub import (
    _apply_result,
    _apply_result_ft,
    _translate_full_text,
    spec_for_pair,
)

# Optional add-on: reached through the extension facade (no-op when absent); name unchanged.
llmemory = _extensions.use("memory")

_logger = logging.getLogger("loc_tms.translate")


def _build_piece(src, locale, terms, piece, **blocks):
    """#683: one piece of a chunked segment, rendered like the whole segment would be."""
    return build_prompt(piece, src, locale, terms, **blocks)


def _pretranslate_stream_impl(
    session: Session,
    doc_id: int,
    locale: str,
    spec: ProviderSpec | None = None,
    force_pii: bool = False,
    adaptive_feedback: bool = False,
    adaptive_feedback_window=None,
    doc_context_prepass: bool = False,
    context_brief: str | None = None,
    _env_ids=(None, None),
    stats_sink: dict | None = None,
):
    """Translate the document, yielding an event per segment then a summary.

    `context_brief` (connectors moat, e.g. confluence_translate): a CALLER-SUPPLIED
    stable context block prepended to every segment prompt — the SAME prompt seam
    the F2.2 pre-pass feeds, but sourced from cheap harvested metadata (space name,
    ancestor title chain, page labels) instead of an LLM call. When given it is used
    VERBATIM (no provider call, no spend, no `provider_calls` bump) and takes
    precedence over `doc_context_prepass` (a supplied brief means the caller already
    has better/cheaper context than a generated one). When None the behavior is
    byte-identical to before this parameter existed.

    `force_pii=True` would demand PII redaction regardless of the global
    `pii_redaction` toggle. As of 2026-07-03 NO caller sets it — the API stopped
    FORCING redaction (a deliberate behavior change; PII is now a per-step choice on
    the workflow path, and the global `pii_redaction` setting is the blanket
    fallback for this pretranslate path). The parameter is retained (default False)
    for API compatibility with pii.should_redact(force).

    `adaptive_feedback` / `adaptive_feedback_window` / `doc_context_prepass` (F2,
    ModernMT/Lara) are ALL OFF by default; when all three are off the assembled
    prompts and event shapes are BYTE-IDENTICAL to before these params existed.
    See app/services/adaptive.py. The orchestrator segmented path reads the same
    toggles from the WorkflowStep; this simple MT path takes them as parameters
    (its callers pass the project/step choice through)."""
    # #683: imported here, not at module top, so the kernel pipeline surface stays
    # narrow (verify_kernel_facade); it is only needed once a translation runs.
    from app.services import adaptive, mt_chunking
    from app.services import tags_only as _tags_only

    doc = session.get(Document, doc_id)
    if doc is None:
        raise DocumentNotFound(f"no such document: {doc_id}")
    # #617 item 4 (gate): a run parked on a human_review step holds this locale's text; plain
    # MT now would be overwritten by (or fight) the resume. Raised before any provider call.
    from app.services import segment_review

    segment_review.refuse_if_awaiting_review(session, doc_id, locale)
    project = session.get(Project, doc.project_id)
    # Phase 2: when the caller didn't pin an explicit spec, resolve the provider
    # per (project, source -> this locale) pair. UNIFORM projects resolve to
    # project.default_provider (byte-identical to the old spec_for_project); a
    # PER_PAIR project picks the pair's own engine.
    spec = spec or spec_for_pair(session, project, locale)
    src = project.source_locale
    # GitHub #356 phases 2a/2b: the override of the MT system-message body in force for THIS
    # pass, resolved once: the project's default workflow's own override (resolved to the pair
    # variant for `locale`), else the project's org's, else None (the built-in text, byte for
    # byte; also None, with nothing read, while the `system_prompt_overrides` flag is off).
    _mt_ovr = system_prompt_overrides.resolve_mt_head_for_project(session, project, locale)
    _mt_head = _mt_ovr.body if _mt_ovr is not None else None
    # AI Tagging 1.5c: bind the LLM tag-repair rung to THIS run's engine + locales, and
    # inject it into the write gate below. It fires only when a segment's inline tags
    # come back broken AND the deterministic rung cannot fix them — otherwise it costs
    # nothing and changes nothing. Uses the run's resolved `spec`, so the repair honors
    # the same (pinned) engine + fallback chain that produced the translation. Offline /
    # MockProvider: `provider_is_live` is False inside the rung, so it self-disables and
    # this path stays byte-identical + hermetic.
    _tag_repairer = tag_llm_repair.make_repairer(
        src_locale=src,
        tgt_locale=locale,
        spec=spec,
        tap_meta=tap_envelope.stamp_meta({"document_id": doc_id}, *_env_ids),
    )
    # Context Package (SPEC-context-metadata phase 2A) resolved ONCE for this doc's
    # scope. `_ctx_block` (the rendered TranslationContext) composes with the existing
    # brief below (package FIRST -- it takes precedence per spec); `_sense_hint`
    # (domain/audience) reaches glossary sense selection. Empty when nothing resolves
    # -> the assembled prompts stay byte-identical. Org-scoped via the project's org.
    _eff_pkg, _ = context_pkg.resolve(
        session, document=doc, project=project, org_id=getattr(project, "org_id", None)
    )
    _ctx_block = context_pkg.render_block(_eff_pkg)
    _sense_hint = context_pkg.sense_hint(_eff_pkg)
    # Style Guide (SPEC-workflows-redesign Track B, B2) — DOCUMENTED DECISION: the plain
    # (non-workflow) MT path ALSO gets the guide, for consistency with the workflow paths
    # (same asset, same resolution, same fenced block). Resolved once for this doc's scope
    # (customer x content_type + optional pair) and composed into the front-matter block
    # alongside the context package below. `_sg_block` is "" when no APPROVED guide resolves
    # -> the assembled prompts stay byte-identical for installs with no guides.
    # MT formality (#617 item 7, ADR 0062): the register resolved for THIS doc + target language
    # (document override > project > customer) rides the same block, and is recorded on the
    # summary. None (nothing set, or `auto`) adds not a byte.
    _formality = formality.resolve_for(session, project=project, document=doc, target_locale=locale)
    _sg_block = style_guides.render_style_guide(
        style_guides.resolve_style_guide(
            session,
            org_id=getattr(project, "org_id", None),
            customer_id=getattr(project, "customer_id", None),
            content_type_id=getattr(project, "content_type_id", None),
            source_locale=src,
            target_locale=locale,
        ),
        _formality,
    )
    stats = _new_stats()
    if _formality is not None:
        stats["formality"] = _formality.record()
    if stats_sink is not None:
        # The caller watches the running totals (the web live stream settles its budget
        # Job from them when the client cuts off before the summary). Same dict, updated
        # in place; the events and the summary are unchanged.
        stats_sink.clear()
        stats_sink.update(stats)
        stats = stats_sink
    window = adaptive.clamp_window(
        adaptive_feedback_window
        if adaptive_feedback_window is not None
        else adaptive.DEFAULT_WINDOW
    )

    # #683: decides, per segment, whether its translation fits the engine's output budget;
    # an overlong one is sent in pieces (or, when no safe cut fits, routed to review).
    # Ordinary segments never resolve the engine (single-piece fast path).
    _planner = mt_chunking.Planner(spec, locale)

    segments = sorted(
        session.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).all(),
        key=lambda s: s.order_index,
    )

    # Glossary AUTO-ATTACH (SPEC-content-types §3.4): this plain-MT path historically
    # saw only the project's LEGACY per-project entries and NEVER any module glossary.
    # Now — for a TYPED project only — approved module glossaries matching the project
    # (customer or global, content type or General, covering the pair) are auto-attached
    # via the SAME plan/merge seam the orchestrator uses (no drift). There is no
    # workflow here, so nothing is pinned (pinned_glossary_id=None). GATE: an untyped
    # project (content_type_id None = every existing project/install) resolves to a
    # legacy-only plan, so match_terms_merged returns EXACTLY the old match_terms(...)
    # output and this path stays byte-identical.
    from app.services import glossary_module as _gm

    _gloss_plan = _gm.build_glossary_plan(session, project, src, locale)
    _gloss_conflicts: set = set()

    # F2.2 document-context pre-pass: ONE cheap call over a truncated sample BEFORE
    # any fan-out; the brief is prepended (stable) to every segment prompt below.
    # Soft-fails to None (translate normally). The call runs through run_completion,
    # so its cost counts against the spend cap + shows in reports for free. OFF by
    # default -> brief stays None -> prompts unchanged.
    brief_blk = ""
    if context_brief and context_brief.strip():
        # Caller-supplied context (harvested metadata) — no LLM call. Runs through
        # the SAME brief_block fencing (quote-prefixed, injection-safe) as the
        # generated brief, so it sits at the front of every prompt identically.
        brief_blk = adaptive.brief_block(context_brief)
    elif doc_context_prepass and segments:
        sample = adaptive.sample_for_brief([s.source_text for s in segments])
        brief, made_call = adaptive.build_document_brief(spec, sample, src, locale)
        if made_call:
            stats["provider_calls"] += 1
        brief_blk = adaptive.brief_block(brief)

    # COMPOSE the front matter: the context PACKAGE (SPEC phase 2A), then the STYLE GUIDE
    # (Track B, B2), then the brief (F2.2 LLM pre-pass OR the caller-supplied Confluence
    # brief). Order = context (finest-grained per-run steering) -> style guide (broad
    # standing brand asset) -> brief, matching the workflow presets' {context}{style_guide}
    # ordering. Every block is ALREADY independently fenced (the package via json-escape +
    # angle-bracket neutralize at ingestion; the guide via _san_body's angle-bracket
    # neutralize; the brief via adaptive.brief_block's "> " quoting), so concatenation with
    # a blank-line separator cannot corrupt any fence. When NONE resolve, brief_blk is
    # untouched -> byte-identical to pre-change (the sacred gate for guide-less installs).
    _front = [b for b in (_ctx_block, _sg_block, brief_blk) if b]
    brief_blk = "\n\n".join(_front)

    if doc.mode == TransMode.FULL_TEXT:
        seg = segments[0] if segments else None
        if seg is not None:
            cached = tm.ft_lookup(session, doc.project_id, seg.source_text, src, locale)
            if cached is not None:
                # SEAM FIX (Phase 0, THIRD sibling site -- full-text TM short-circuit).
                # Route the cached full-body TM fill through the SOLE sanctioned write
                # seam instead of writing target_text/status directly. Pre-fix a stored
                # FullTextTM hedge/poison committed as clean SegStatus.MT AND overwrote a
                # hard TM lock, bypassing the refusal perimeter, the length/number/tag/
                # meta gates and the lock enforcement. lang_guard off (a TM apply is not a
                # live-provider call); tm_eligible False (applying a stored entry never
                # re-seeds TM).
                outcome = quality.commit_mt_output(
                    session, seg, cached, source="tm", tm_eligible=False
                )
                if outcome.status_protected:
                    # F9 (adversarial review): the body is human-approved/edited, so the seam
                    # refused this cached-TM apply and left the row byte-untouched. That is a
                    # PROTECTION, not an error -- emit the STORED target (not "") and do not bump
                    # the error count. (Mirrors _apply_result_ft's explicit status_protected arm.)
                    session.commit()
                    yield {
                        "type": "segment",
                        "index": 0,
                        "target": seg.target_text or "",
                        "source": "full_text_tm-protected",
                        "status": seg.status,
                    }
                elif outcome.flagged:
                    # withheld (a refusal-shaped cache OR a hard-locked segment the seam
                    # refused to overwrite): not a successful apply, mirrors _apply_result.
                    session.commit()
                    stats["errors"] += 1
                    yield {
                        "type": "segment",
                        "index": 0,
                        "target": "",
                        "source": "full_text_tm-refused",
                        "status": seg.status,
                    }
                else:
                    tm.ft_lookup(
                        session, doc.project_id, seg.source_text, src, locale, accepted=True
                    )
                    session.add(seg)
                    session.commit()
                    stats["from_full_text_tm"] += 1
                    stats["translated"] += 1
                    # #58/#59 F2: a TM-applied segment can still land NEEDS_REVIEW at the
                    # seam (a poisoned TM entry that trips tag/number/placeholder), so count
                    # it here too — the TM short-circuit must not undercount the summary.
                    if seg.status == SegStatus.NEEDS_REVIEW:
                        stats["needs_review"] += 1
                    # QA still runs on a TM-filled target (the 100%/cached short-circuit
                    # must not bypass placeholder-integrity + other checks).
                    yield {
                        "type": "segment",
                        "index": 0,
                        "target": seg.target_text,
                        "source": "full_text_tm",
                        "qa": qa.check_segment(seg.source_text, seg.target_text),
                    }
            else:
                terms = _gm.match_terms_merged(
                    session,
                    doc.project_id,
                    seg.source_text,
                    src,
                    locale,
                    _gloss_plan,
                    conflict_state=_gloss_conflicts,
                    disambig=_sense_hint,
                )
                # AI Tagging 1.1: mask ⟦⟧ tokens (outside PII) before the engine call,
                # restore BEFORE _apply_result_ft (the commit gate sees real ⟦⟧). No-op
                # when off / no token.
                tag_src, tag_map = (
                    ai_tagging.mask(seg.source_text)
                    if ai_tagging.enabled()
                    else (seg.source_text, {})
                )
                eng_src, pii_map = (
                    pii.redact(tag_src, lang=src) if pii.should_redact(force_pii) else (tag_src, {})
                )
                # Mask-hole fix: the front matter travels beside a MASKED body, so any
                # raw token it quotes must speak the same sentinel vocabulary.
                ft_brief = _mask_aux(brief_blk, tag_map) if ai_tagging.enabled() else brief_blk
                res = _translate_full_text(
                    spec,
                    eng_src,
                    src,
                    locale,
                    terms,
                    stats,
                    brief_block=ft_brief,
                    head_override=_mt_head,
                )
                if pii_map:
                    res["text"] = pii.restore(res["text"], pii_map)
                if tag_map:
                    res["text"], _ = ai_tagging.restore(res["text"], tag_map)
                ft_outcome = _apply_result_ft(
                    session, seg, res, stats, glossary_terms=terms, llm_tag_repair=_tag_repairer
                )
                # Only store into the Full-Text TM on a genuinely successful,
                # non-refusal translation — a hedge caught by the choke point must NOT
                # poison the TM (it would be re-served as a 100%% hit forever). We
                # consult the seam's outcome instead of re-running is_refusal: same
                # verdict, single source of truth. `committed` blocks a withheld hedge;
                # `tm_eligible` additionally blocks a SOFT-flagged nontranslation (QA-3
                # layer b — committed for the reviewer to see, but kept out of TM). The
                # res["text"]/error checks stay so an empty/failed body is never stored.
                if (
                    res["text"]
                    and not res["error"]
                    and ft_outcome.committed
                    and ft_outcome.tm_eligible
                ):
                    tm.ft_store(
                        session,
                        doc.project_id,
                        seg.source_text,
                        res["text"],
                        src,
                        locale,
                        segment=seg,
                    )
                session.commit()
                yield {
                    "type": "segment",
                    "index": 0,
                    "target": seg.target_text,
                    "source": res["provider_used"] or "error",
                }
        yield {"type": "summary", **stats}
        return

    # segmented mode — phase 1: TM/glossary prep (DB-bound, sequential); TM hits
    # apply immediately, everything else becomes a work item for the pool.
    # One batched fuzzy lookup for every not-yet-translated segment's source,
    # instead of a tm.lookup() query pair per segment.
    #
    # F2.1 adaptive feedback: when ON, a FeedbackBuffer accumulates confirmed
    # source->target pairs IN order_index ORDER — TM short-circuits + already-
    # translated (human-confirmed) segments count too (they're the highest-quality
    # signal, so they seed the buffer here in phase 1). The per-segment prompt is
    # then built LAZILY in phase 2 (it needs the buffer snapshot of EARLIER
    # segments). When OFF, prompts are pre-built here exactly as before (with only
    # the stable brief, which is empty unless the pre-pass ran) so behavior is
    # byte-identical.
    # PII: whether redaction is active for THIS pretranslate call (same flag that
    # gates each item's own eng_src below). When active, the feedback buffer must
    # NEVER be seeded with raw PII — see _fb_seed_pair's docstring.
    redact_active = pii.should_redact(force_pii)
    fb = adaptive.FeedbackBuffer() if adaptive_feedback else None

    def _fb_seed_pair(source_text: str, target_text: str) -> tuple[str, str]:
        """The (source, target) pair to SEED the feedback buffer with for an
        already-confirmed segment (TM short-circuit / pre-existing human-confirmed
        target) that never goes through phase-2 redaction itself. These pairs never
        reach the engine on their own, but the buffer's render() DOES get injected
        into a later segment's prompt — which the engine DOES see. So when redaction
        is active, seed with the REDACTED source AND the REDACTED target (same
        redact() used on the engine path): an approved translation can itself carry
        PII (the target of a PII-bearing source usually repeats it, and human edits
        can reintroduce names/IDs), so BOTH halves must be scrubbed or a later
        prompt would still hand this segment's raw PII to the provider. Placeholder
        tokens in the feedback block are the correct redaction-ON semantics."""
        if not redact_active:
            return source_text, target_text
        return pii.redact(source_text, lang=src)[0], pii.redact(target_text, lang=locale)[0]

    needs_tm = [seg.source_text for seg in segments if not seg.target_text]
    tm_matches = tm.lookup_batch(session, doc.project_id, needs_tm, src, locale)
    # TM few-shot (2B): ONE batched top-N fuzzy pull for every not-yet-translated
    # source (reuses the same fuzzy seam as lookup_batch — no second similarity
    # engine, no N+1). Relevance-gated (floor + skip-not-pad + self-exclusion) inside
    # example_matches_batch; rendered per segment below. Empty when nothing clears the
    # floor -> no examples block -> byte-identical prompt (the plain-MT path uses the
    # default window, always ON, generalizing the old single tm_ref reference).
    tm_examples_map = tm.example_matches_batch(
        session,
        doc.project_id,
        needs_tm,
        src,
        locale,
        floor=context_pkg.TM_EXAMPLES_FLOOR,
        window=context_pkg.TM_EXAMPLES_DEFAULT_WINDOW,
    )
    # LLMemory recall (PROPOSAL-llmemory §5, the SECOND translate path). This whole
    # path IS a translate step — there is no workflow, no LQA judge and no correction-
    # loop fixer here, so the orchestrator's step-role gate has no analogue: every call
    # made below is an original translation and is eligible. The doc-level note is
    # selected ONCE for the whole pretranslate (the run-cache equivalent), so it is not
    # re-charged against the block budget — or re-counted in `use_count` — per segment.
    _mem_note_blk, _mem_note_ids = "", []
    # [0] = has the note already been usage-stamped on this pretranslate? The note is
    # selected with `mark_used=False` and stamped only once, on the first segment whose
    # FOLD actually kept it (re-review F1).
    _mem_note_stamped = [False]
    try:
        _mem_note_blk, _mem_note_ids = llmemory.select_doc_note(
            session,
            customer_id=getattr(project, "customer_id", None),
            project_id=doc.project_id,
            source_locale=src,
            target_locale=locale,
            org_id=getattr(project, "org_id", None),
            mark_used=False,
        )
        _mem_note_ids = list(_mem_note_ids)
    except Exception:
        _logger.warning("llmemory: doc-note recall failed", exc_info=True)

    def _memory_for(source_text, glossary_terms=None):
        """(block, memory_ids) for ONE segment source. ("", []) whenever recall is off,
        the project has no customer, or nothing clears the gates — the empty branch that
        keeps this path byte-identical to pre-LLMemory.

        `glossary_terms` is THIS segment's already-resolved approved matches (the same
        `terms` list the prompt is about to carry), so the §5 precedence law applies on
        this path exactly as it does in the orchestrator: a memory contradicting an
        approved glossary term is never injected beside it.

        THE FOLD DECIDES PROVENANCE (re-review F1). The fold re-applies the block budget
        to the join of this segment's block and the doc note, so a rendered line can
        still be dropped here — and a dropped memory shaped nothing. Only the SURVIVORS
        are returned (they become `tap_meta["memory_ids"]`) and only the survivors are
        usage-stamped; the doc note is stamped at most once per pretranslate."""
        try:
            blk, ids = llmemory.select_for_segment(
                session,
                customer_id=getattr(project, "customer_id", None),
                project_id=doc.project_id,
                source_locale=src,
                target_locale=locale,
                segment_source=source_text,
                org_id=getattr(project, "org_id", None),
                glossary_terms=glossary_terms,
                mark_used=False,
            )
        except Exception:
            _logger.warning("llmemory: segment recall failed", exc_info=True)
            blk, ids = "", []
        folded, kept = prompts.compose_memory_block_kept(
            blk, _mem_note_blk, list(ids), _mem_note_ids
        )
        note_set = set(_mem_note_ids)
        llmemory.mark_used_ids(session, [i for i in kept if i not in note_set])
        note_kept = [i for i in kept if i in note_set]
        if note_kept and not _mem_note_stamped[0]:
            _mem_note_stamped[0] = True
            llmemory.mark_used_ids(session, note_kept)
        return (folded, list(kept))

    pending = []
    applied_tm_ids: list[int] = []  # SegmentTM entries whose target we APPLIED
    for seg in segments:
        if seg.target_text:
            if fb is not None:
                s, t = _fb_seed_pair(seg.source_text, seg.target_text)
                fb.add(s, t)  # human-confirmed = best signal
            yield {
                "type": "segment",
                "index": seg.order_index,
                "target": seg.target_text,
                "source": "existing",
            }
            continue
        if _tags_only.is_tags_only(seg.source_text):
            # #702/#705: nothing but inline tags / placeholders and whitespace: the engine
            # can only hand them back (sdlxliff/3: 1,159 tags, 3 chunked requests, 20 minutes
            # of engine time for a copy). The shared copy action writes the source as the
            # translation through the commit seam (its tag gates still run); no engine call,
            # never seeds TM.
            outcome = _tags_only.commit_copy(session, seg)
            session.commit()
            if outcome.flagged:
                stats["errors"] += 1
            else:
                stats["translated"] += 1
                stats["tags_only"] = stats.get("tags_only", 0) + 1
                if seg.status == SegStatus.NEEDS_REVIEW:
                    stats["needs_review"] += 1
            yield {
                "type": "segment",
                "index": seg.order_index,
                "target": seg.target_text,
                "source": "tags-only",
                "status": seg.status,
            }
            continue
        exact = tm_matches.get(seg.source_text)
        if exact and exact["match_pct"] == 100:
            # SEAM FIX (Phase 0). Route the 100% TM fill through the SOLE sanctioned
            # write seam (quality.commit_mt_output) instead of writing target_text
            # directly. Pre-fix this block set seg.target_text/status straight, so a
            # POISONED TM entry — a stored hedge, a length-runaway, a tag-broken or
            # number-corrupted target (exactly the poison tm_module.scrub_hedges
            # exists to clean up) — sailed into the segment as clean SegStatus.MT,
            # bypassing the refusal perimeter, the length/number/tag/meta gates and
            # NEEDS_REVIEW routing. A TM hit now clears the SAME gates as fresh MT.
            # lang_guard stays OFF (a TM apply is not a live-provider call — the same
            # rule the workflow write-back uses for a tm_origin segment); tm_eligible
            # is False (applying a stored entry never re-seeds TM). glossary_terms is
            # not passed here (the DNT/glossary soft checks need this segment's matched
            # terms, which the plain-MT branch below computes; the refusal/length/
            # number/tag/meta gates the fix is about all run without them).
            outcome = quality.commit_mt_output(
                session, seg, exact["target_text"], source="tm", tm_eligible=False
            )
            if outcome.flagged:
                # A refusal-shaped TM hit: the seam WITHHELD the write and routed the
                # segment to NEEDS_REVIEW with a refusal qa_flag. It is NOT a successful
                # TM apply — count it as an error (mirroring _apply_result's flagged
                # branch), do not bump usage and do not seed the feedback buffer.
                session.commit()
                stats["errors"] += 1
                yield {
                    "type": "segment",
                    "index": seg.order_index,
                    "target": "",
                    "source": "tm-refused",
                    "pct": 100,
                    "status": seg.status,
                }
                continue
            # Committed. On a clean pass the seam set SegStatus.MT; a SOFT gate
            # (length/number/tag/meta) leaves the text committed but routes to
            # NEEDS_REVIEW — honour whatever status the seam set. Stamp TM provenance.
            seg.tm_match_pct = 100
            session.add(seg)
            session.commit()
            if exact.get("id") is not None:
                applied_tm_ids.append(exact["id"])
            stats["from_segment_tm"] += 1
            stats["translated"] += 1
            # #58/#59 F2: a soft-gated TM apply (the seam left it at NEEDS_REVIEW, e.g. a
            # poisoned 100% hit that trips tag/number/placeholder) is counted, mirroring
            # the full-text-TM and fresh-MT paths — the summary must not undercount review.
            if seg.status == SegStatus.NEEDS_REVIEW:
                stats["needs_review"] += 1
            if fb is not None:
                s, t = _fb_seed_pair(seg.source_text, seg.target_text)
                fb.add(s, t)  # 100% TM = best signal
            # QA still runs on a 100% TM hit — the short-circuit must not bypass
            # placeholder-integrity / tag / number checks on the applied target.
            yield {
                "type": "segment",
                "index": seg.order_index,
                "target": seg.target_text,
                "source": "tm",
                "pct": 100,
                "qa": qa.check_segment(seg.source_text, seg.target_text),
            }
            continue
        terms = _gm.match_terms_merged(
            session,
            doc.project_id,
            seg.source_text,
            src,
            locale,
            _gloss_plan,
            conflict_state=_gloss_conflicts,
            disambig=_sense_hint,
        )
        tm_examples_block = context_pkg.render_tm_examples(tm_examples_map.get(seg.source_text, []))
        # AI Tagging 1.1: mask ⟦⟧ tokens (segment-granular, OUTSIDE PII) before the
        # engine call; restored in _emit BEFORE _apply_result (the commit gate sees real
        # ⟦⟧) and before the feedback buffer is seeded (MED-5). No-op when off / no token.
        tag_src, tag_map = (
            ai_tagging.mask(seg.source_text) if ai_tagging.enabled() else (seg.source_text, {})
        )
        eng_src, pii_map = (
            pii.redact(tag_src, lang=src) if pii.should_redact(force_pii) else (tag_src, {})
        )
        # #683: planned on the ENGINE text (masked tags / PII sentinels are protected tokens).
        _plan = _planner.plan(eng_src)
        if _plan.unfit:
            # No safe cut fits the engine's output budget: nothing is sent; the segment goes
            # to review with the reason (the job carries on; this is not an engine error).
            mt_chunking.route_unfit(session, seg, _plan.unfit_reason)
            session.commit()
            stats["needs_review"] += 1
            stats["too_long_for_mt"] = stats.get("too_long_for_mt", 0) + 1
            yield {
                "type": "segment",
                "index": seg.order_index,
                "target": "",
                "source": "too-long",
                "status": seg.status,
            }
            continue
        # LLMemory: selected against the RAW segment source (the memories were distilled
        # from real text and are matched lexically), on this thread, before any fan-out
        # — recall touches the ORM session and the pool workers must not.
        _mem_blk, _mem_ids = _memory_for(seg.source_text, terms)
        _tap = tap_envelope.stamp_meta(
            {"document_id": doc_id, "segment_index": seg.order_index}, *_env_ids
        )
        # Provenance (proposal §5): the ids ride the tap meta, and ONLY when something
        # was actually injected — an install with no memories captures byte-identical
        # tap records.
        if _mem_ids:
            _tap["memory_ids"] = list(_mem_ids)
        item = {
            "seg": seg,
            "eng_src": eng_src,
            "terms": terms,
            "tm_examples_block": tm_examples_block,
            "memory_block": _mem_blk,
            "pii_map": pii_map,
            "tag_map": tag_map,
            "exact": exact,
            # Prompt Tap correlation (additive; observer-only). This is the plain
            # per-segment MT path, so it carries the document + segment identity.
            "tap_meta": _tap,
        }
        if _plan.chunked:
            item["plan"] = _plan
        if fb is None:
            # OFF (or brief-only) path: pre-build the prompt now, exactly as before
            # — with the stable brief block (empty string -> None -> byte-identical
            # when the pre-pass is also off). tm_examples_block "" -> byte-identical.
            # Mask-hole fix (see _mask_aux): the brief is a per-RUN string but the
            # sentinel vocabulary is per-SEGMENT, so it is masked HERE (per item)
            # rather than once up front — masking the shared variable in place would
            # stamp segment 0's sentinels onto every later segment's prompt.
            _brief = _mask_aux(brief_blk, tag_map) if ai_tagging.enabled() else brief_blk
            _tm_ex = (
                _mask_aux(tm_examples_block, tag_map) if ai_tagging.enabled() else tm_examples_block
            )
            # LLMemory rides the SAME masking seam as the other auxiliary blocks: a
            # memory body is distilled from real segment text, so it can quote a raw
            # token even though the admission screen tries to reject those at write
            # time. This is the second lock, not the first.
            _mem = _mask_aux(_mem_blk, tag_map) if ai_tagging.enabled() else _mem_blk
            system, prompt, payload = build_prompt(
                eng_src,
                src,
                locale,
                terms,
                brief_block=_brief or None,
                tm_examples_block=_tm_ex or None,
                memory_block=_mem or None,
                head_override=_mt_head,
            )
            item.update({"system": system, "prompt": prompt, "payload": payload})
            if _plan.chunked:
                # #683: each piece is rendered exactly as the whole segment would be.
                item["build"] = functools.partial(
                    _build_piece,
                    src,
                    locale,
                    terms,
                    brief_block=_brief or None,
                    tm_examples_block=_tm_ex or None,
                    memory_block=_mem or None,
                    head_override=_mt_head,
                )
        pending.append(item)

    # Usage tracking (TM module): one bulk UPDATE for every TM entry APPLIED above
    # (100% exact hits written to a segment). Accumulated then bumped once so the hot
    # loop stays a single extra statement, and it never fails the translation.
    if applied_tm_ids:
        from app.services import tm_module

        # project-scoped so an attached source project's rows are never written by
        # this project's run (see tm_module.bump_usage).
        tm_module.bump_usage(session, applied_tm_ids, project_id=doc.project_id)

    # phase 2: provider calls run concurrently (bounded); ORM writes + commits +
    # yields stay on this thread. Events arrive out of order — they carry index.
    #
    # CACHE WARM-UP (Feature 0): every pending segment of this doc/locale shares
    # ONE byte-identical system prefix (translation_prompt puts the stable
    # instructions in `system`; the glossary + per-segment source go in the USER
    # message). Anthropic's cache entry is only readable AFTER the first response
    # begins — so IF the prefix were large enough to cache, firing the whole pool
    # simultaneously would make ALL N calls cache-MISS and each pay the full prefix
    # write. Running the FIRST pending segment SYNCHRONOUSLY (it would write the
    # prefix cache) THEN fanning the rest out (which would cache-READ) is the
    # correct ordering for that case.
    #
    # HONEST CAVEAT: with the stock ~128-token system prefix this warm-up buys
    # NOTHING — that prefix is far below every model's minimum cacheable length
    # (haiku 4096 / sonnet 1024-2048), so no segment ever cache-hits and the
    # `cache: N read/write` badge stays 0 on the per-segment path. See
    # config.PROMPT_CACHING. The ordering is retained because it is correct-if-it-
    # ever-matters and the sync warm call is exactly one of the N calls we'd make
    # anyway (no extra cost). Skipped when there's only one segment.
    def _emit(item, res):
        seg, exact = item["seg"], item["exact"]
        if item["pii_map"]:
            res["text"] = pii.restore(res["text"], item["pii_map"])
        if item.get("tag_map"):
            res["text"], _ = ai_tagging.restore(res["text"], item["tag_map"])
        _apply_result(
            session, seg, res, stats, glossary_terms=item["terms"], llm_tag_repair=_tag_repairer
        )
        # #683: record that the stored translation was produced in pieces (a later
        # one-piece translation clears the note). A no-op for an ordinary segment.
        if not res.get("error") and (seg.target_text or "").strip():
            mt_chunking.stamp(seg, res.get("chunks"))
            if (res.get("chunks") or 1) > 1:
                stats["chunked_segments"] = stats.get("chunked_segments", 0) + 1
        if exact:
            seg.tm_match_pct = exact["match_pct"]
            session.add(seg)
        session.commit()
        return {
            "type": "segment",
            "index": seg.order_index,
            "target": seg.target_text,
            "source": res["provider_used"] or "error",
            "pct": exact["match_pct"] if exact else None,
        }

    if pending:
        # RELEASE THE WRITER LOCK BEFORE DISPATCH (SPEC-parallel-routing Phase 0.1,
        # the same fix as the orchestrator's per-segment seam). `_memory_for` above
        # STAGES LLMemory usage stamps on this session without committing
        # (llmemory._mark_used, deliberately: a second Session deadlocks) — and from
        # the second pending segment on, the next segment's glossary/recall queries
        # autoflush them, taking SQLite's single writer lock UNCOMMITTED. Left held,
        # that lock rides into the very next provider call, where
        # `spend.record_usage` opens its OWN Session and blocks on it: QA-1's
        # circular wait (tests/verify_orchestrator_spend_lock.py has the post-mortem).
        # The FIRST call is the one this guards — the SYNCHRONOUS warm call below,
        # which runs before `_emit` has committed anything.
        #
        # INSIDE `if pending:`, AND THAT MATTERS (review finding 1). Placed above the
        # guard it also fired on a document with NOTHING to translate — every segment
        # already has a target, so the build loop `continue`s without ever committing
        # — and there it would COMMIT A CALLER'S UNRELATED IN-FLIGHT WORK at a
        # boundary that does not exist on main: a caller that staged, called
        # pretranslate on a fully-translated doc, then rolled back would find its work
        # had survived. That is precisely the defect llmemory._mark_used was rewritten
        # to remove ("a usage COUNTER must never be able to move another subsystem's
        # transaction boundary"), and reintroducing it one layer up would be no
        # better. A run with no pending segments dispatches nothing, so it has no lock
        # worth releasing.
        _db.commit_if_staged(session)

    def _run_item(item):
        # #683: a chunked segment goes piece by piece; every other item is the one call.
        if item.get("plan") is not None:
            return mt_chunking.complete_pieces(
                spec, item["plan"], item["build"], run=run_completion, tap_meta=item["tap_meta"]
            )
        return run_completion(
            spec,
            item["prompt"],
            system=item["system"],
            task="translate",
            payload=item["payload"],
            tap_meta=item["tap_meta"],
        )

    if pending and fb is None:
        warm, rest = pending[0], pending[1:]
        # 1) warm the shared prefix cache with a single synchronous call
        warm_res = _run_item(warm)
        yield _emit(warm, warm_res)
        # 2) fan the remainder out — they read the now-warm prefix
        if rest:
            # AI opt-out (DPAI-08): carry the AI scope into each MT thread.
            from app.services import ai_optout as _ai_optout

            with ThreadPoolExecutor(max_workers=_mt_workers()) as pool:
                futures = {_ai_optout.submit(pool, _run_item, item): item for item in rest}
                for fut in as_completed(futures):
                    yield _emit(futures[fut], fut.result())
    elif pending:
        # F2.1 ON: ORDERED processing so each segment's feedback block is
        # DETERMINISTIC (segment order, never thread-completion order). Pending
        # segments run STRICTLY IN order_index ORDER; each prompt is built from the
        # buffer snapshot of EARLIER segments (prior TM/human pre-fills + already-
        # completed earlier pending segments). A completed segment's confirmed
        # target is appended to the buffer BEFORE the next segment's prompt is
        # built, so the injected examples are identical run-to-run. This trades the
        # concurrent fan-out for a serial pass — the correct cost of order-dependent
        # context (a per-segment feedback block cannot be built for a segment whose
        # predecessors haven't finished). window/char caps enforced in render().
        for item in pending:
            feedback_blk = fb.render(window=window)
            # Mask-hole fix (see _mask_aux). The feedback block is the WORST offender
            # of the three: it is literally prior segments' source->target pairs, so on
            # a tagged document it quotes real tokens on nearly every call.
            _tag_map = item.get("tag_map") or {}
            if ai_tagging.enabled():
                _brief = _mask_aux(brief_blk, _tag_map)
                _fb_blk = _mask_aux(feedback_blk, _tag_map)
                _tm_ex = _mask_aux(item["tm_examples_block"], _tag_map)
                _mem = _mask_aux(item.get("memory_block") or "", _tag_map)
            else:
                _brief, _fb_blk, _tm_ex = (brief_blk, feedback_blk, item["tm_examples_block"])
                _mem = item.get("memory_block") or ""
            system, prompt, payload = build_prompt(
                item["eng_src"],
                src,
                locale,
                item["terms"],
                brief_block=_brief or None,
                feedback_block=_fb_blk or None,
                tm_examples_block=_tm_ex or None,
                memory_block=_mem or None,
                head_override=_mt_head,
            )
            # Serial branch, same discipline, per iteration. MEASURED pre-fix state of
            # this loop: ('translate', True), then False, False — only the FIRST call
            # rode the staged stamps, because `_emit` commits at the end of every pass
            # and clears the lock for the ones after it. So the guarded release above
            # is what actually fixes this branch; this line is defense-in-depth, held
            # deliberately because it costs nothing (a strict no-op when nothing is
            # staged, asserted by [D]) and because relying on `_emit`'s commit staying
            # exactly where it is today is the kind of distant invariant that breaks
            # quietly.
            _db.commit_if_staged(session)
            if item.get("plan") is not None:
                # #683: same blocks for every piece of a chunked segment.
                res = mt_chunking.complete_pieces(
                    spec,
                    item["plan"],
                    functools.partial(
                        _build_piece,
                        src,
                        locale,
                        item["terms"],
                        brief_block=_brief or None,
                        feedback_block=_fb_blk or None,
                        tm_examples_block=_tm_ex or None,
                        memory_block=_mem or None,
                        head_override=_mt_head,
                    ),
                    run=run_completion,
                    tap_meta=item["tap_meta"],
                )
            else:
                res = run_completion(
                    spec,
                    prompt,
                    system=system,
                    task="translate",
                    payload=payload,
                    tap_meta=item["tap_meta"],
                )
            # Capture the engine's OWN output BEFORE _emit restores PII in place —
            # this (never the restored, real-PII text) is what the buffer must be
            # seeded with when redaction is active (see below).
            engine_text = res.get("text") or ""
            ev = _emit(item, res)
            # Seed the buffer for the NEXT segment's prompt. When redaction is
            # active for this item, the buffer must never carry raw PII: use the
            # ALREADY-REDACTED source (item["eng_src"], the exact text sent to the
            # engine) and the PRE-RESTORE engine target (engine_text, captured
            # above) — both are redaction-safe by construction (the placeholder
            # tokens are what the engine itself produced/echoed). When redaction is
            # off, behavior is unchanged: the real source/target. A segment whose
            # target was NOT committed never enters the buffer: the plain branch gets
            # that for free (empty target -> FeedbackBuffer.add skips it) and #469
            # gave the redacted branch the same guard EXPLICITLY, because a refusal
            # withheld by commit_mt_output still lives in engine_text - seeding it
            # made the hedge a confirmed few-shot example for every later segment.
            if item["pii_map"]:
                if (item["seg"].target_text or "").strip():
                    # MED-5: the buffer feeds a LATER segment's prompt - it must
                    # carry RESTORED ⟦⟧ (or nothing), never a «TAG_N» sentinel. Keep
                    # PII masked (correct for the buffer) but tag-restore both legs.
                    fb_s, fb_o = item["eng_src"], engine_text
                    if item.get("tag_map"):
                        fb_s = ai_tagging.restore(fb_s, item["tag_map"])[0]
                        fb_o = ai_tagging.restore(fb_o, item["tag_map"])[0]
                    fb.add(fb_s, fb_o)
            else:
                fb.add(item["seg"].source_text, item["seg"].target_text or "")
            yield ev

    yield {"type": "summary", **stats}
