"""orchestrator: part8

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
import time
from datetime import datetime

from sqlmodel import Session

from app.models import (
    RunStatus,
    StepType,
    WorkflowStep,
)
from app.services import (
    ai_tagging,
    pii,
    pii_boundary,
    prompts,
    quality,
    refusal,
    tag_repair,
)
from app.services.orchestrator_part3 import (
    _bound_findings_block,
    _last_gate_judged_text,
    _render_glossary_block,
)
from app.services.orchestrator_part4 import _seg_chunks
from app.services.orchestrator_part5 import _reason_patterns, _system_for
from app.services.orchestrator_part6 import (
    PROGRESS_MIN_INTERVAL_S,
    _may_clear_gate_hold,
    _progress_dirty,
    _progress_idle,
    _progress_lock,
    _progress_snapshot,
    _progress_state,
    _progress_wake,
    _progress_writer_start,
)
from app.services.orchestrator_terminology_backen import (
    _LQA_FINDINGS_PREAMBLE,
    _SINGLE_UNIT_INSTRUCTION,
    SEG_JOINER,
    _inject_missing_prefixes,
    _looks_like_unit_translation,
    _over_translation_guard_on,
)
from app.services.providers import (
    ProviderSpec,
    run_completion,
    usage_token_counts,
)

log = logging.getLogger("loc_tms.orchestrator")


def _gate_release_mask_in_scope(
    session: Session, run, unit_texts: list[str], scope: frozenset
) -> list[bool]:
    """`_gate_release_mask` for ONE verdict scope. Split out so the three-case rule above is
    written once and the scope grouping stays a pure dispatch."""
    n = len(unit_texts)
    if not _may_clear_gate_hold(session, run, scope):
        return [False] * n
    judged, indices = _last_gate_judged_text(session, run.id, scope, with_indices=True)
    if judged is None:
        return [False] * n
    if indices is not None:
        # #159: compact judged blobs retain their original segment positions. Excluded
        # units were not approved by this verdict and must never have a hold cleared.
        # A stale hold on an excluded TM unit therefore survives later excluded runs.
        # Releasing it requires an explicit review or a judging run with exclusion
        # disabled. TM provenance alone is not authority to clear an existing hold.
        parts = judged.split(SEG_JOINER)
        out = [False] * n
        if len(parts) == len(indices):
            for j, text in zip(indices, parts, strict=True):
                if 0 <= j < n:
                    out[j] = text == unit_texts[j]
        return out
    if judged == SEG_JOINER.join(unit_texts):
        return [True] * n
    parts = judged.split(SEG_JOINER)
    if len(parts) != n:
        return [False] * n
    return [p == t for p, t in zip(parts, unit_texts, strict=True)]


def _run_step_on_segment(
    spec: ProviderSpec,
    template: str,
    base_ctx: dict,
    step: WorkflowStep,
    seg_source: str,
    seg_text: str,
    locale: str,
    terms: list[dict],
    redact: bool = False,
    brief_block: str = "",
    feedback_block: str = "",
    tm_examples_block: str = "",
    tap_meta: dict | None = None,
    findings_suffix: str = "",
    memory_block: str = "",
    images: list[bytes] | None = None,
    seg_concepts: list[dict] | None = None,
    render_cfg: dict | None = None,
    segment_id: int | None = None,
    excluded_findings: frozenset[str] = frozenset(),
    relevant_findings: frozenset[str] | None = None,
    system_head: str | None = None,
) -> dict:
    """Run ONE workflow step over ONE segment's current text. Pure/thread-safe:
    no ORM, no session — only run_completion (HTTP/LLM). Returns a dict with the
    output text + provider/model/attempts/error so the caller (on the DB thread)
    can aggregate a single StepRun and write back.

    `images` (Batch 14 T2, SPEC §6.2/§6.3): OPTIONAL per-string image context (send-ready
    PNG bytes the caller resolved on the DB thread). Plain data like `memory_block` —
    session-free. Threaded to run_completion ONLY on the FIRST prompt chunk (index 0); a
    key value that splits into >1 chunk must not re-send the image per chunk (N-2). The
    returned dict carries `images_present` (bool: this leg WAS handed images — minted AT
    THE LEG from what it actually received, M-2, so an opted-out/non-agent leg that got []
    reads False and contributes nothing to skip accounting, §25.4) and `images_sent`
    (bool: run_completion actually egressed the bytes on chunk 0 — False on a non-vision
    engine or a failover to text-only, which is the caller's single skip condition
    `images_present and not images_sent`, §6.3/LAW-IMG-4). Empty/absent images keep the
    assembled text prompt + wire message byte-identical (LAW-IMG-7).

    When `redact` is True (step.redact_pii or the global pii_redaction setting),
    PII in the segment's working text (and its {source} context) is scrubbed BEFORE
    the engine sees it and restored in the output — so raw PII never reaches the
    engine, and the returned output (which becomes the next step's input, the
    StepRun blob, and Segment.target_text) is always the restored text. Each
    segment gets its OWN pii_map (per-segment redaction).

    `brief_block` (F2.2) and `feedback_block` (F2.1) are optional pre-rendered,
    delimited context blocks (app/services/adaptive.py). Both default "" and, when
    empty, the assembled prompt is BYTE-IDENTICAL to before these params existed.
    The stable brief is prepended (a stable per-doc prefix, cache-friendly) and the
    per-segment feedback is inserted just before the rendered instruction so the
    model reads orientation -> examples -> task.

    The returned dict also carries `fb_source`/`fb_output`: the pair a CALLER should
    seed an adaptive.FeedbackBuffer with for THIS segment. When `redact` is True
    these are the REDACTED source (eng_source, the exact text sent to the engine)
    and the PRE-RESTORE engine output (never the real PII the restored `output`
    carries) — the feedback block is injected into a LATER segment's prompt, which
    the engine DOES see, so it must never carry raw PII. When `redact` is False
    they equal seg_source/output (unchanged behavior).

    `memory_block` (LLMemory Phase 1, PROPOSAL-llmemory §5) is the optional pre-rendered
    "Project memory:" block for THIS segment — the caller composes it from
    `llmemory.select_for_segment` (per-segment term memories) plus the run-cached
    `llmemory.select_doc_note` (at most one doc-level note per run) and is responsible
    for the eligibility gate (`_memory_eligible_step`). It follows the SAME convention as
    the three blocks above: default "", and when empty the assembled prompt is
    BYTE-IDENTICAL to before the parameter existed. It is masked with the same
    `mask_like` seam and sits AFTER `tm_examples_block` in the assembly order, i.e.
    below the customer's own approved TM material — memory ranks under TM/glossary/style
    guide (proposal §2 "LLMemory is additive and ranks below them")."""
    from app.services import tags_only as _tags_only  # lazy: keeps the kernel surface narrow

    if _tags_only.is_tags_only(seg_source) and _tags_only.is_tags_only(seg_text):
        # #705 defense in depth: a segment with no translatable text never reaches an engine
        # (the workflow run already keeps it out of every step's pending set; the
        # Evaluations Lab producer calls this directly). The step's answer is its input:
        # no call, no spend, nothing to seed a feedback buffer with.
        return _tags_only_step_result(seg_source, seg_text)
    eng_source = (
        pii.redact(seg_source, lang=base_ctx.get("source_locale", ""))[0] if redact else seg_source
    )
    # Scope both findings channels using the caller's document-wide attribution.
    _fb = base_ctx.get("lqa_findings", "") or ""
    if _fb:
        base_ctx = {
            **base_ctx,
            "lqa_findings": _bound_findings_block(
                _fb,
                seg_source,
                seg_text,
                excluded_findings=excluded_findings,
                relevant_findings=relevant_findings,
            ),
        }
    if findings_suffix.startswith(_LQA_FINDINGS_PREAMBLE):
        bounded = _bound_findings_block(
            findings_suffix[len(_LQA_FINDINGS_PREAMBLE) :],
            seg_source,
            seg_text,
            excluded_findings=excluded_findings,
            relevant_findings=relevant_findings,
        )
        findings_suffix = (_LQA_FINDINGS_PREAMBLE + bounded) if bounded else ""
    # AI Tagging 1.1: mask the segment's ⟦⟧ tokens ONCE (segment-granular) before
    # chunking, so a paired token never gets colliding per-chunk numbering. Restored
    # (1.5) after the join, BEFORE the output feeds the next step / write gate / feedback
    # buffer. eng_source is deliberately NOT masked (it carries real ⟦⟧ for the {source}
    # context and the feedback pair); the buffer never receives «TAG_N» (MED-5) because
    # fb_output is tag-restored below. No-op when off / no token.
    tag_text, tag_map = ai_tagging.mask(seg_text) if ai_tagging.enabled() else (seg_text, {})
    # AI Tagging mask-hole fix (live QA 2026-07-25): the LQA findings block quotes REAL
    # ⟦⟧ tokens (the judge scores the RESTORED text), so injecting it beside a MASKED
    # {text} showed the model both notations at once and taught it to emit raw tokens
    # into masked output. Rewrite the findings into THIS segment's sentinel vocabulary,
    # using the same mapping, so the whole prompt speaks one language. No-op when
    # masking is off (tag_map empty) or the findings carry no tokens.
    if ai_tagging.enabled():
        base_ctx = {
            **base_ctx,
            "lqa_findings": ai_tagging.mask_like(base_ctx.get("lqa_findings", ""), tag_map),
        }
        findings_suffix = ai_tagging.mask_like(findings_suffix, tag_map)
        # SAME HOLE, TWO MORE CHANNELS (live QA 2026-07-25, found at scale). Every
        # auxiliary block that QUOTES REAL SEGMENT TEXT carries raw `⟦⟧` tokens into a
        # prompt whose {text} is masked: the TM few-shot examples are prior
        # source->target pairs, the adaptive-feedback block is prior confirmed pairs, and
        # the doc brief can quote source. These only leak ONCE THE RUN HAS PRODUCED
        # MATERIAL (an empty TM leaks nothing), which is exactly why a short probe run
        # showed zero leaks while a full document still had the model inventing tokens.
        # One seam, applied uniformly, so the whole prompt speaks one notation.
        tm_examples_block = ai_tagging.mask_like(tm_examples_block, tag_map)
        feedback_block = ai_tagging.mask_like(feedback_block, tag_map)
        brief_block = ai_tagging.mask_like(brief_block, tag_map)
        # SAME SEAM, FOURTH CHANNEL (LLMemory Phase 1). A memory body is distilled from
        # reviewer corrections and LQA notes — i.e. from REAL SEGMENT TEXT — so it is
        # exactly the kind of auxiliary material that can quote a raw token. LLMemory's
        # own admission screen rejects `⟦⟧` at write time (proposal §4 "a memory must
        # never carry mask literals"), which makes this the SECOND lock, not the first:
        # a memory created before that screen existed, or edited around it, still cannot
        # reach a masked prompt speaking the wrong notation.
        memory_block = ai_tagging.mask_like(memory_block, tag_map)

    chunks = _seg_chunks(step, tag_text)
    from app.services import mt_chunking  # lazy: keeps the kernel surface narrow

    # #683: a chunk whose translation would not fit the engine's output budget is cut further
    # at sentence / clause / space boundaries (never inside a token, nor inside a PII value
    # that will be redacted per piece) and rejoined with the #668 seam rules below. A chunk
    # no safe cut fits is not sent: the segment is withheld to review with the reason. Every
    # ordinary segment keeps its one-piece plan (byte-identical requests and join).
    _too_long_reason = ""
    _chunk_plans: list = []
    if step.chunk_policy != "none":
        _planner = mt_chunking.Planner(spec, locale)
        for _ch in chunks:
            _protect = (
                list(pii.redact(_ch, lang=base_ctx.get("source_locale", ""))[1].values())
                if redact
                else ()
            )
            _cp = _planner.plan(_ch, protect=_protect)
            if _cp.unfit:
                _too_long_reason = _cp.unfit_reason
                break
            _chunk_plans.append(_cp)
        if _too_long_reason:
            chunks, _chunk_plans = [], []
        elif any(_cp.chunked for _cp in _chunk_plans):
            chunks = [_pc for _cp in _chunk_plans for _pc in _cp.pieces]
        else:
            _chunk_plans = []

    def _join_chunks(parts: list[str]) -> str:
        if not _chunk_plans:
            return "\n\n".join(parts)
        groups, k = [], 0
        for _cp in _chunk_plans:
            n = len(_cp.pieces)
            groups.append(_cp.join(parts[k : k + n]) if n > 1 else parts[k])
            k += n
        return "\n\n".join(groups)

    outputs, raw_outputs = [], []
    provider_used, model_used, attempts, err = None, None, 0, None
    # Real per-step-visit spend (feat/workflow-spend): accumulate this SEGMENT's exact
    # ledger cost + tokens across its chunk calls into THREAD-LOCAL locals. This
    # function runs on a worker thread (the segment executor / pool.submit); keeping
    # the accumulation in locals it returns — rather than mutating any shared state —
    # is what makes the fan-out race-safe. The caller (on the DB thread) sums these
    # returned per-segment figures across all segments AFTER join, so no update is lost.
    seg_cost, seg_tin, seg_tout = 0.0, 0, 0
    # Batch 14 T2: images ride ONLY the FIRST prompt chunk (index 0); every later chunk
    # gets images=None so a multi-chunk key value never re-sends the image (N-2). The
    # segment's images_sent is chunk 0's run_completion verdict.
    images_present = bool(images)
    images_sent = False
    # run-190 Item D: True once any chunk's candidate was a page dump the over-translation
    # guard rejected even after its one strengthened-instruction retry. The DB thread reads
    # it and WITHHOLDS the segment (routes to review, never commits the dump).
    over_translation = False
    # run-190 Item D / M-1: the SHAPE of the rejected dump (the reason token
    # _looks_like_unit_translation returns: code_fence / markdown_table / multi_line /
    # too_long, or "" when the retry errored) plus the source/target char counts, so the DB
    # thread can build an HONESTLY-NAMED over-translation qa_flag (never "model refusal").
    over_translation_reason = ""
    over_translation_src_len = 0
    over_translation_tgt_len = 0
    if _too_long_reason:
        # #683: withheld before any engine call; the reason names the engine limit.
        over_translation = True
        over_translation_reason = mt_chunking.ENGINE_LIMIT_SHAPE
        over_translation_src_len = len(tag_text or "")
    # run-190 Item C / M-2 (gate round 3): the finish SHAPE of this segment's LAST chunk (the
    # wire finish_reason + the terminal exception class run_completion threads in its result
    # dict). These were NEVER returned before, so the caller's
    # `seg_finish_reason = r.get("finish_reason") or seg_finish_reason` was dead code and the
    # segmented StepRun stamped NULL for both columns on every run. Mirrors the FULL_TEXT twin
    # (ft_finish_reason / ft_error_class), last chunk wins.
    chunk_finish_reason = None
    chunk_error_class = None
    # Terminology T3 (SPEC §9-T3, residue [15]): resolve the glossary block ONCE here,
    # ABOVE the per-chunk loop (today's glossary_line was recomputed per chunk — this is a
    # free win, byte-identical when OFF). OFF -> prompts.glossary_line(terms) verbatim and
    # `shown_terms` stays None; ON -> the audience-gated/ordered/capped render + this leg's
    # shown set + the mt-audience payload glossary (H7). Pure (plain dicts), so it is safe on
    # this MT worker thread. When the toggle is ON, stamp the shown set onto the per-call
    # capture tap_meta for the adherence audit (LAW-TERM-16); the caller pins
    # tap_meta["segment_index"] to the persisted Segment.id (M1, the leg join key) and this
    # is a session-free dict mutation.
    if render_cfg and render_cfg.get("on") and seg_concepts is not None:
        _seg_gloss_block, seg_shown_terms, _seg_mt_terms = _render_glossary_block(
            seg_concepts, terms, render_cfg
        )
    else:
        # OFF fast path (residue L6): skip the render on a toggle-OFF step — glossary_line
        # verbatim, no shown set, full-match payload. Byte-identical to main (LAW-TERM-1/14).
        _seg_gloss_block, seg_shown_terms, _seg_mt_terms = prompts.glossary_line(terms), None, terms
    if render_cfg and render_cfg.get("on") and seg_shown_terms is not None and tap_meta is not None:
        tap_meta = {
            **tap_meta,
            "shown_terms": list(seg_shown_terms),
            "target_locale": locale,
            # M1: pin the leg join key to the persisted Segment.id (NOT order_index) so the
            # adherence leg's _adherence join (on seg.id) selects exactly this leg's records.
            "segment_index": (
                segment_id if segment_id is not None else tap_meta.get("segment_index")
            ),
        }
    for _ci, ch in enumerate(chunks):
        eng_text, pii_map = (
            pii.redact(ch, lang=base_ctx.get("source_locale", "")) if redact else (ch, {})
        )
        ctx = {
            **base_ctx,
            "source": eng_source,
            "text": eng_text,
            "prior": eng_text,
            "glossary": _seg_gloss_block,
            # {tm_offers} is a FULL_TEXT offer placeholder; on this segmented path TM
            # fills land inline and no offer block exists, so an author who left the
            # placeholder in a segmented template used to get the literal
            # "{tm_offers}" in the prompt. Substitute it empty here (#334 L3).
            "tm_offers": "",
        }
        # GitHub #344: the single llm_agent render seam -- the chunk is fenced in
        # <content>...</content> so a segment like "1." is content, not instruction.
        prompt, _fenced = prompts.render_step_prompt(template, ctx)
        # Change 2: always-inject a resolved context / style-guide block the template
        # omitted (topmost, before the brief/tm/feedback prepends so the assembled order
        # matches the placeholder-present case); byte-identical when nothing resolved.
        prompt = _inject_missing_prefixes(
            template, prompt, ctx.get("context", ""), ctx.get("style_guide", "")
        )
        # F2 / 2B: prepend the stable doc brief, then the TM few-shot examples, then the
        # per-segment feedback block. Prepends apply in REVERSE order of final position,
        # so the assembled order is: brief (stable prefix) -> tm_examples -> feedback ->
        # template. Empty blocks add nothing (byte-identical when every toggle is off).
        if feedback_block:
            prompt = feedback_block + "\n\n" + prompt
        # LLMemory sits BELOW the TM examples in the final prompt (prepended BEFORE
        # them, since prepends apply in reverse order of final position), so the
        # assembled order is: brief -> tm_examples -> memory -> feedback -> template.
        # That placement IS the precedence law of proposal §2 — the customer's own
        # approved TM material is read first, and memory is additive context under it.
        if memory_block:
            prompt = memory_block + "\n\n" + prompt
        if tm_examples_block:
            prompt = tm_examples_block + "\n\n" + prompt
        if brief_block:
            prompt = brief_block + "\n\n" + prompt
        # Change 1: append the correction-loop LQA findings at the very END (freshest
        # task instruction nearest the output); "" for a non-fix step / no findings.
        prompt = prompt + findings_suffix
        # GitHub #43 (see the FULL_TEXT twin): language pair unconditionally, notation
        # keyed on THIS segment's own tokens. `seg_text` is the pre-mask working text;
        # `source_locale` rides base_ctx (this function is session-free by contract).
        # GitHub #344: the <content> fence sentence rides the system message only on
        # calls whose prompt actually carries the fence (render_step_prompt's verdict).
        _step_system = _system_for(
            tag_map,
            seg_text,
            src_locale=base_ctx.get("source_locale", ""),
            tgt_locale=locale,
            head_body=system_head,
        )
        if _fenced:
            _step_system += " " + prompts.CONTENT_FENCE_INSTRUCTION
        if memory_block:
            # #362: the one reference-data sentence rides the system message of every call
            # whose prompt carries the <project_memory> block (the overtranslation retry below
            # builds on this same base, so it keeps it).
            _step_system += " " + prompts.MEMORY_FENCE_INSTRUCTION
        res = pii_boundary.call_with_redaction(
            redact or step.redact_pii,
            run_completion,
            spec,
            prompt,
            system=_step_system,
            task="translate",
            payload={"text": eng_text, "target": locale, "glossary": _seg_mt_terms},
            tap_meta=tap_meta,
            images=images if _ci == 0 else None,
        )
        # run-190 Item D: OVER-TRANSLATION (page-dump) HARD guard. When an llm_agent step's
        # candidate for this chunk looks like the WHOLE PAGE (fence / cross-language table /
        # multi-line-from-single-line / far longer than the source), retry ONCE with a
        # strengthened single-unit instruction appended to the system text; if it STILL dumps
        # (or errors), flag the segment for withholding — the caller routes it to review and
        # NEVER commits the dump. Applies regardless of image presence. Kill switch:
        # OVER_TRANSLATION_MAX_RATIO == 0 (then the guard is skipped entirely).
        if (
            step.step_type == StepType.LLM_AGENT
            and _over_translation_guard_on(step)  # M5: global knob AND per-step opt-out
            and not res.get("error")
            and res.get("text")
            and not _looks_like_unit_translation(
                prompts.strip_content_fence(res["text"], source=eng_text), eng_text
            )[0]
        ):
            # Account the REJECTED first attempt's spend/tokens/attempts before retrying
            # (the loop body below accounts the retry's), so the StepRun spend stays honest.
            seg_cost += res.get("cost_usd") or 0.0
            _ot_tin, _ot_tout = usage_token_counts(res.get("usage"))
            seg_tin += _ot_tin
            seg_tout += _ot_tout
            attempts += res["attempts"]
            # _step_system already carries the #344 fence sentence when this call is
            # fenced, so the retry keeps it by building on the same base.
            _ot_system = _step_system + " " + _SINGLE_UNIT_INSTRUCTION
            res = pii_boundary.call_with_redaction(
                redact or step.redact_pii,
                run_completion,
                spec,
                prompt,
                system=_ot_system,
                task="translate",
                payload={"text": eng_text, "target": locale, "glossary": _seg_mt_terms},
                tap_meta=tap_meta,
                # L9 (gate round 1): the guard's strengthened-instruction retry DROPS images —
                # the retry is about the TEXT SHAPE (the model already saw the image on the
                # first attempt), so re-sending the payload only doubles vision-input spend for
                # every tripped unit. Mirrors run_completion's own LAW-IMG-3 (drop images on a
                # retry within a target).
                images=None,
            )
            _ot_usable, _ot_shape = _looks_like_unit_translation(
                prompts.strip_content_fence(res.get("text") or "", source=eng_text), eng_text
            )
            if res.get("error") or not _ot_usable:
                over_translation = True
                # M-1: capture the shape rule + char counts of the FIRST chunk that tripped
                # so the withhold flag names what actually happened (not a model refusal). A
                # retry that errored carries no shape token -> generic phrasing downstream.
                if not over_translation_reason:
                    over_translation_reason = _ot_shape
                    over_translation_src_len = len(str(eng_text or ""))
                    over_translation_tgt_len = len(str(res.get("text") or ""))
        if _ci == 0:
            images_sent = bool(res.get("images_sent"))
        seg_cost += res.get("cost_usd") or 0.0
        _tin, _tout = usage_token_counts(res.get("usage"))
        seg_tin += _tin
        seg_tout += _tout
        # GitHub #344: strip a model-emitted <content> wrapper from the edges before
        # the output is stored / feeds the next step. Applied per chunk (each chunk is
        # its own completion call, so each may be wrapped) and to the pre-restore raw
        # output too, so a redacting step's feedback buffer never carries the markers.
        # Strip BEFORE the PII restore (a restored value must not lose an edge marker).
        _stripped = prompts.strip_content_fence(res["text"] or "", source=eng_text)
        raw_outputs.append(_stripped)
        out_text = pii.restore(_stripped, pii_map) if pii_map else _stripped
        if pii_map and "«PII_" in out_text:
            # F6 safety net: residual placeholder ⇒ some PII could not be restored.
            # Warn (do not fail the run).
            log.warning("Residual PII placeholder after restore (segmented step)")
        outputs.append(out_text)
        provider_used, model_used = res["provider_used"], res["model_used"]
        attempts += res["attempts"]
        err = res["error"] or err
        # run-190 Item C / M-2 (gate round 3): carry this chunk's finish shape out (a clean
        # "stop" as well as an errored "length"/"ThinkingBurn"), last chunk wins -- the
        # FULL_TEXT twin captures it unconditionally too.
        chunk_finish_reason = res.get("finish_reason")
        chunk_error_class = res.get("error_class")
    output = _join_chunks(outputs)
    # AI Tagging 1.1 restore + 1.5 inter-step verify: restore ⟦⟧ from sentinels, then
    # repair recoverable damage vs the segment's INPUT (seg_text) so it never flows
    # downstream (the write gate is the backstop for the rest). No-op when no token.
    if tag_map:
        output, _tag_resid = ai_tagging.restore(output, tag_map)
        if quality.tag_damage(seg_text, output):
            rep = tag_repair.repair(seg_text, output)
            if rep is not None:
                output = rep[0]  # repaired text; the commit gate re-flags a
                # best-guess (non-clean) repair for review
    # Change 3: conservative output-hygiene strip of a leading meta line (per segment,
    # against THIS segment's source) BEFORE the output feeds the next step / refusal check
    # / write-back. Applied to the restored output AND (when redacting) the pre-restore
    # engine text that seeds a later segment's feedback buffer, so both stay clean.
    output, hygiene_line = refusal.strip_meta_preamble(output, seg_source)
    if redact:
        raw_join, _ = refusal.strip_meta_preamble(_join_chunks(raw_outputs), eng_source)
        # MED-5: the feedback buffer feeds a LATER segment's prompt, so it must carry
        # RESTORED ⟦⟧ (or nothing), never a «TAG_N» sentinel. raw_outputs are pre-PII-
        # restore (PII stays masked for the buffer, correct) but still tag-MASKED — so
        # tag-restore them here, leaving PII masked and tokens real.
        if tag_map:
            raw_join, _ = ai_tagging.restore(raw_join, tag_map)
        fb_source, fb_output = eng_source, raw_join
    else:
        fb_source, fb_output = seg_source, output
    if _too_long_reason:
        # #683: nothing was sent. Hand back the pre-step text so the step is not "empty" (a
        # one-segment document would otherwise fail the run); the withhold above guarantees
        # it is never committed as a translation, and it never seeds the feedback buffer.
        output = seg_text
    return {
        "output": output,
        "provider_used": provider_used,
        "model_used": model_used,
        "attempts": attempts,
        "error": err,
        "fb_source": fb_source,
        "fb_output": fb_output,
        "hygiene_stripped": bool(hygiene_line),
        # Thread-local per-segment spend (feat/workflow-spend); the DB thread sums
        # these across segments after join (race-safe — no shared mutable state).
        "cost_usd": seg_cost,
        "tokens_in": seg_tin,
        "tokens_out": seg_tout,
        # Batch 14 T2: per-leg image disposition, minted AT THE LEG (M-2). The DB thread
        # folds these over results.values() AFTER join for the single skip note (§6.3).
        "images_present": images_present,
        "images_sent": images_sent,
        # Terminology T3 (LAW-TERM-15 (ii), residue [12]): this leg's shown set (audience-
        # gated/ordered/capped five-key dicts) when the toggle was ON, else None. The DB
        # thread records it into shown_terms_by_segment[j] AFTER the produce decision for a
        # toggle-ON producing step so cross-step guards read what the model was shown.
        "shown_terms": seg_shown_terms,
        # run-190 Item D: True when a chunk's candidate was a page dump the over-translation
        # guard rejected after its one strengthened retry. The DB thread WITHHOLDS the
        # segment (adds it to refused_seg -> NEEDS_REVIEW, dump never committed).
        "over_translation": over_translation,
        # M-1: the shape rule + char counts of the rejected dump, so the withhold is flagged
        # HONESTLY (a nontranslation-family over-translation entry, never a "model refusal").
        "over_translation_reason": over_translation_reason,
        "over_translation_src_len": over_translation_src_len,
        "over_translation_tgt_len": over_translation_tgt_len,
        # #683: the engine-output-limit withhold's own reason ("" for every other case) and
        # the number of pieces this segment's translation was produced in (1 = one request).
        "too_long_reason": _too_long_reason,
        "mt_chunks": len(chunks) if _chunk_plans else 1,
        # run-190 Item C / M-2 (gate round 3): the finish shape the caller stamps onto the
        # segmented StepRun.finish_reason / error_class (previously absent -> always NULL).
        "finish_reason": chunk_finish_reason,
        "error_class": chunk_error_class,
    }


def _tags_only_step_result(seg_source: str, seg_text: str) -> dict:
    """The `_run_step_on_segment` result for a tags-only segment (#705): the input text
    unchanged, no provider, no attempt, zero spend; every key the callers read."""
    return {
        "output": seg_text,
        "provider_used": None,
        "model_used": None,
        "attempts": 0,
        "error": None,
        "fb_source": seg_source,
        "fb_output": seg_text,
        "hygiene_stripped": False,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "images_present": False,
        "images_sent": False,
        "shown_terms": None,
        "over_translation": False,
        "over_translation_reason": "",
        "over_translation_src_len": 0,
        "over_translation_tgt_len": 0,
        "too_long_reason": "",
        "mt_chunks": 1,
        "finish_reason": None,
        "error_class": None,
        "tags_only": True,
    }


def run_reason(run) -> dict | None:
    """WHY this run ENDED THE WAY IT DID, as {"code": <stable token>, "text": <sentence>},
    or None when there is nothing to say.

    SCOPED TO FAILED / CANCELLED ON PURPOSE. #106 is about a run that ends with no
    explanation anywhere; a run that is still running, is paused, or COMPLETED did not end
    badly and must not grow a "reason" line.

    AND SCOPED TO THE TERMINAL RECONCILERS' OWN SENTENCES. `run.notes` is a shared,
    APPENDING column -- informational sentences land in it too (a tm_match pool that was
    not readable, a vendor dead-letter escalation) and they do NOT reach
    `run.context["warnings"]`. So an unrecognised note is NOT reported as a reason:
    a FAILED run whose notes hold
    only "tm_match TM pool 5 not readable -- skipped" reports None, and the operator reads
    the real cause from `failure_shape` instead of an unrelated sentence rendered under
    "Reason:". `text` starts at the MATCHED terminal sentence rather than at the beginning
    of the accumulated column, so an earlier informational note is never republished as the
    cause.

    Pure + defensive: it reads two columns and never queries, so every surface can call it.
    `text` is bounded to 500 chars like `_failure_reason` (it is rendered in a browser and
    can carry provider-supplied text via _crash_note)."""
    if getattr(run, "status", None) not in (RunStatus.FAILED, RunStatus.CANCELLED):
        return None
    notes = (getattr(run, "notes", "") or "").strip()
    if not notes:
        return None
    for fragment, code in _reason_patterns():
        if not fragment:
            continue
        at = notes.find(fragment)
        if at < 0:
            continue
        return {"code": code, "text": notes[at:].strip()[:500]}
    return None


def run_progress(run) -> dict | None:
    """This run's IN-STEP progress as {"done", "total", "unit", "step", "step_index", "at"},
    or None when the run has never published any (GitHub #101).

    None is the honest answer for a run whose step publishes nothing (a single-unit step, a
    human_review-only workflow, every run that predates these columns) -- a surface must show
    "no progress recorded", never a false 0%. `at` is the liveness half: a counter that last
    moved an hour ago says something very different from one that moved ten seconds ago, and
    it is what lets a monitor distinguish a slow run from a dead one WITHOUT the default-OFF
    Ledger.

    THE COUNTER IS PER STEP, AND IT SAYS WHICH (gate F round 1, L3). Each long step starts a
    fresh 0-of-M, so a bare `done` legitimately DECREASES at every step boundary and `unit`
    changes under it (segments -> chunks). `step` / `step_index` name the step the current
    N-of-M belongs to, so a monitor branching on "the counter went backwards" can see it was
    a step transition rather than a regression.

    TWO SOURCES, NEWEST WINS. The persisted columns are what a DIFFERENT process reads (the
    server answering `/api/runs/{id}` for a run a headless CLI is executing); the in-memory
    map is what THIS process's own executor is publishing right now, between the coarse
    persists. Comparing `progress_at` rather than trusting either blindly means a leaked
    in-memory entry (a ticker whose step died hard) can never shadow a fresher row.

    Pure reads -- one dict lookup and the run's own columns, no query, no bodies, no prompts
    -- so every surface can call it."""
    live = _progress_snapshot(getattr(run, "id", None))
    row = None
    total = int(getattr(run, "progress_total", 0) or 0)
    at = getattr(run, "progress_at", None)
    if total > 0 or at is not None:
        row = {
            "done": int(getattr(run, "progress_done", 0) or 0),
            "total": total,
            "unit": getattr(run, "progress_unit", "") or "units",
            "step": getattr(run, "progress_step", "") or "",
            "step_index": int(getattr(run, "progress_step_index", 0) or 0),
            "at": at,
        }
    best = live or row
    if live and row and row["at"] and (live["at"] is None or row["at"] > live["at"]):
        best = row
    if best is None:
        return None
    return {
        "done": best["done"],
        "total": best["total"],
        "unit": best["unit"] or "units",
        "step": best.get("step") or "",
        "step_index": int(best.get("step_index") or 0),
        "at": best["at"].isoformat() if best["at"] else None,
    }


def progress_drain(timeout: float = 5.0) -> bool:
    """Wait (bounded) until the background writer has nothing pending. For TESTS and
    shutdown only -- no production path may wait on telemetry."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _progress_lock:
            pending = bool(_progress_dirty)
        if not pending and _progress_idle.is_set():
            return True
        time.sleep(0.01)
    return False


def _flush_progress(run_id, *, force: bool = False) -> None:
    """QUEUE a persist of `run_id`'s in-memory counter, subject to the throttle.

    NEVER TOUCHES THE DATABASE ON THE CALLER'S THREAD (gate F round 1, H1): it takes a
    short lock, adds an id to a set and sets an Event. Safe to call from the run's own
    thread between provider calls at any concurrency, including with the connection pool
    fully exhausted -- the write itself happens on the background writer, which skips
    rather than queues when there is no free connection.

    The throttle is attempt-based: `attempt_at` is stamped here, `flushed_at`/`flushed_done`
    only after a write actually lands, so a skipped write is retried on the next unit rather
    than recorded as done."""
    if not run_id:
        return
    now = time.monotonic()
    with _progress_lock:
        st = _progress_state.get(run_id)
        if not st:
            return
        if not force:
            if st["done"] == st["flushed_done"]:
                return  # nothing moved since the last persisted value
            if (now - st["attempt_at"]) < PROGRESS_MIN_INTERVAL_S:
                return
        st["attempt_at"] = now
        _progress_dirty.add(run_id)
    _progress_idle.clear()
    _progress_wake.set()
    _progress_writer_start()


def _bump_run_progress(run_id, n: int = 1) -> None:
    """Advance `run_id`'s counter by `n` IN MEMORY ONLY -- no DB, no pool checkout, no
    writer lock. This is what a fan-out WORKER thread calls as its call lands (#100's
    lesson: nothing on a pool thread may open a Session); the run's own thread queues the
    persist later through `_flush_progress`."""
    if not run_id:
        return
    with _progress_lock:
        st = _progress_state.get(run_id)
        if st is None or not st.get("total"):
            return
        st["done"] = min(st["total"], st["done"] + max(0, int(n)))
        st["at"] = datetime.utcnow()


def _progress_forget(run_id) -> None:
    """Drop `run_id`'s live counter + throttle state (called when a step's ticker is done,
    and again on every executor's way out -- see `run_workflow_stream`'s finally)."""
    with _progress_lock:
        _progress_state.pop(run_id, None)
        _progress_dirty.discard(run_id)
