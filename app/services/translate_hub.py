"""translate: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/translation-pipeline.md
"""

import logging

from sqlmodel import Session, select

from app import config
from app.models import Document, Project, Segment, SegStatus
from app.services import (
    quality,
    tap_enrich,
    tap_envelope,
)
from app.services.prompts import translation_prompt as build_prompt
from app.services.providers import ProviderSpec, run_completion
from app.services.translate_common import (
    _merge_usage,
    _merge_usage_into_stats,
    _split_for_chunks,
    spec_for_provider,
)

_logger = logging.getLogger("loc_tms.translate")


def spec_for_pair(session: Session, project: Project, target_locale: str) -> ProviderSpec:
    """A ProviderSpec for a project's (source -> target_locale) pair, honoring the
    per-pair provider mode (Phase 2). For a UNIFORM project this is byte-identical
    to spec_for_project (the resolver returns project.default_provider); for a
    PER_PAIR project it picks the pair's own provider (or the project/global
    default when the pair has no override). Same fallback wiring as
    spec_for_project. This is the per-pair-aware entry point every translate run
    site uses so the right engine runs for each language pair."""
    from app.services import pair_providers

    provider = pair_providers.resolve_provider(
        session, project, project.source_locale, target_locale
    )
    fallback = config.DEFAULT_FALLBACK_PROVIDER or None
    return ProviderSpec(
        provider=provider,
        model=config.DEFAULT_MODELS.get(provider),
        fallback_provider=fallback,
        fallback_model=config.DEFAULT_MODELS.get(fallback) if fallback else None,
    )


def estimate_translation(
    session: Session, doc_id: int, locale: str, spec: ProviderSpec | None = None
) -> dict:
    """Pre-flight estimate for a translation, BEFORE it kicks off (no live token
    count, no provider call). Sums the un-translated segments' source size
    (chars/4 heuristic), pads output 1.5x, and derives a rough duration from a
    per-provider tokens/second constant. Surfaced on the web progress UI and the
    CLI `doc translate` line so a big job's expected time is visible up front
    (Feature 7)."""
    from app.services.providers import est_output_tokens
    from app.services.segment import est_tokens

    doc = session.get(Document, doc_id)
    if doc is None:
        return {
            "segments": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "provider": None,
            "tokens_per_sec": 0,
            "seconds": 0,
        }
    project = session.get(Project, doc.project_id)
    # Phase 2: estimate against the per-pair provider (its tokens/sec constant), so
    # a per-pair engine's estimate matches what will actually run.
    spec = spec or spec_for_pair(session, project, locale)
    segments = session.exec(
        select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
    ).all()
    pending = [s for s in segments if not (s.target_text or "").strip()]
    input_tokens = sum(est_tokens(s.source_text) for s in pending)
    output_tokens = est_output_tokens(input_tokens) if input_tokens else 0
    tps = (
        config.PROVIDER_TOKENS_PER_SEC.get(
            spec.provider, config.PROVIDER_TOKENS_PER_SEC.get("mock", 80)
        )
        or 80
    )
    seconds = int(round(output_tokens / tps)) if output_tokens else 0  # noqa: RUF046
    return {
        "segments": len(pending),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "provider": spec.provider,
        "tokens_per_sec": tps,
        "seconds": seconds,
    }


def _lang_guard_for(res) -> bool:
    """Whether the QA-3 layer-(b) language-mismatch guard (refusal.looks_untranslated,
    applied inside quality.commit_mt_output) should run on THIS result.

    It runs only for output from a REAL provider — never the offline MockProvider, which
    deliberately ECHOES the (English) source with a "[locale]" tag, i.e. English for a
    non-English target, and would be a systemic false positive. Resolve the ACTUAL
    provider that produced the text (post-failover) by its recorded name: a keyless
    install resolves ANY real name to the mock and is correctly exempt; a live cloud or
    local engine is not. Any resolution error -> treat as non-real (guard off) so this
    can never itself break a commit."""
    from app.services.providers import MockProvider, get_provider

    try:
        return not isinstance(get_provider(res.get("provider_used")), MockProvider)
    except Exception:
        return False


def _apply_result(session, seg, res, stats, glossary_terms=None, llm_tag_repair=None):
    stats["provider_calls"] += 1
    usage = res.get("usage") or {}
    stats["tokens_in"] += usage.get("input_tokens", 0)
    stats["tokens_out"] += usage.get("output_tokens", 0)
    stats["tokens_cache_read"] += usage.get("cache_read_tokens", 0) or 0
    stats["tokens_cache_write"] += usage.get("cache_write_tokens", 0) or 0
    if res["error"] and not res["text"]:
        stats["errors"] += 1
        # Issue #572: keep the FIRST engine error text so a run that translated nothing
        # can say WHY on the Job row (the job handler promotes it to Job.error). setdefault
        # so only the first error survives, and so a clean run's stats dict stays
        # byte-identical to before: the key never appears when nothing failed.
        stats.setdefault("first_error", str(res["error"])[:300])
        return
    # Defense-in-depth against the empty-translation billing trap (belt-and-braces
    # alongside providers._require_nmt_output, which is the PRIMARY fix and already
    # stops every NMT provider from returning "" for a non-empty source without
    # raising). This guard only fires for an NMT-channel result (usage carries
    # `nmt_kind` — see providers._nmt_usage; token-based LLM usage never sets it):
    # an empty target for a non-empty source on the char-billed NMT path is never
    # legitimate output, so treat it as an error rather than committing "" as a
    # SegStatus.MT "success" (which would count as translated while chars were
    # billed). Scoped to NMT (not every provider) because an LLM CAN legitimately
    # be asked to translate something into an empty string in edge cases (e.g. a
    # segment the model judges untranslatable) and we have no evidence that never
    # happens — the NMT text-in/text-out contract has no such ambiguity.
    if usage.get("nmt_kind") and not res["text"] and (seg.source_text or "").strip():
        stats["errors"] += 1
        return
    # Refusal / meta-output GUARD via the sanctioned CHOKE POINT (arch-review Finding
    # 2). `quality.commit_mt_output` is the ONE place fresh MT lands in target_text:
    # a produced target that reads as a conversational refusal / assistant hedge
    # ("I'm ready to translate ... please provide the source text ...") is NOT a
    # translation, so the seam does NOT commit it — it routes the segment to review
    # (NEEDS_REVIEW) with a refusal qa_flag (provenance source="mt"). We count that as
    # an error here, exactly as before. The connector write-back already excludes
    # NEEDS_REVIEW, so the hedge is never posted back. Non-refusal behavior is
    # byte-identical: the seam writes target_text + SegStatus.MT, and mt_confidence /
    # provider accounting stay here (only the refusal check + the write moved).
    # glossary_terms feeds the deterministic DNT verification INSIDE the choke point
    # (2B): a DNT term present in source but translated away in the target flags the
    # segment for review (NEEDS_REVIEW + dnt_violation qa_flag), WITHOUT blocking the
    # write. No terms / no violation -> byte-identical.
    outcome = quality.commit_mt_output(
        session,
        seg,
        res["text"],
        source="mt",
        glossary_terms=glossary_terms,
        lang_guard=_lang_guard_for(res),
        llm_tag_repair=llm_tag_repair,
    )
    if outcome.flagged:
        stats["errors"] += 1
        return
    if res["provider_used"]:
        stats["provider_used"][res["provider_used"]] = (
            stats["provider_used"].get(res["provider_used"], 0) + 1
        )
    if res.get("failover"):
        stats["failovers"] += 1
    seg.mt_confidence = 0.9
    session.add(seg)
    stats["translated"] += 1
    # #58/#59: a soft gate (wrong-script/nontranslation, tag-injection, number, ...)
    # committed the text but routed it to review. Count it so the job summary stops
    # reporting a wrong-language delivery as a clean `errors: 0` success.
    if seg.status == SegStatus.NEEDS_REVIEW:
        stats["needs_review"] += 1


def _translate_full_text(
    spec, source_text, src, locale, terms, stats, brief_block="", head_override=None
):
    """Translate a FULL_TEXT document body. A body that fits the per-request token
    budget is ONE call (as before). An OVERSIZED body is split by the SAME
    paragraph chunker the workflow orchestrator uses and translated chunk-by-chunk,
    then rejoined — so a huge whole-doc translation is never sent as one giant call
    that the model truncates at its output cap (Feature 7 root cause #1).

    Returns a run_completion-shaped dict {text, provider_used, model_used, attempts,
    error, usage, failover}. If ANY chunk errors (including a truncation that
    survived retry/failover), `error` is set and `text` is "" — a partial
    translation is NEVER returned as success (the caller must not write it)."""
    from app.services.segment import est_tokens

    def _one(text):
        system, prompt, payload = build_prompt(
            text,
            src,
            locale,
            terms,
            brief_block=brief_block or None,
            head_override=head_override,
        )
        return run_completion(spec, prompt, system=system, task="translate", payload=payload)

    if est_tokens(source_text) <= config.MAX_CHUNK_TOKENS:
        res = _one(source_text)
        stats["provider_calls"] += 1
        if res.get("failover"):
            stats["failovers"] += 1
        return res

    # oversized — split on paragraph boundaries and translate each chunk.
    chunks = _split_for_chunks(source_text, config.MAX_CHUNK_TOKENS)
    parts, agg_usage = [], {}
    provider_used = model_used = None
    attempts = 0
    failover = False
    for ch in chunks:
        res = _one(ch)
        stats["provider_calls"] += 1  # each chunk is a real provider call
        _merge_usage(agg_usage, res)
        if res.get("failover"):
            failover = True
            stats["failovers"] += 1
        if res["error"] or not res["text"]:
            # A failed/truncated chunk fails the WHOLE full-text translation — never
            # stitch a partial document together and store it as a success.
            return {
                "text": "",
                "provider_used": res["provider_used"],
                "model_used": res["model_used"],
                "attempts": attempts + res["attempts"],
                "error": res["error"] or "empty chunk output",
                "usage": agg_usage,
                "failover": failover,
            }
        parts.append(res["text"])
        provider_used, model_used = res["provider_used"], res["model_used"]
        attempts += res["attempts"]
    return {
        "text": "\n\n".join(parts),
        "provider_used": provider_used,
        "model_used": model_used,
        "attempts": attempts,
        "error": None,
        "usage": agg_usage,
        "failover": failover,
    }


def _apply_result_ft(session, seg, res, stats, glossary_terms=None, llm_tag_repair=None):
    """Apply a _translate_full_text result to the single full-text segment, returning
    the `quality.CommitOutcome` (so the caller's Full-Text-TM store can consult it —
    a refusal must never be stored as a 100%%-match).

    Unlike _apply_result (per-segment path), the chunked full-text path already
    counted provider_calls/failovers per chunk in _translate_full_text, so this
    only records token usage + the segment write (avoiding a double count)."""
    _merge_usage_into_stats(res, stats)
    if res["error"] and not res["text"]:
        stats["errors"] += 1
        # Same first-error capture as _apply_result (issue #572), so a full-text body
        # whose engine call failed carries the reason too.
        stats.setdefault("first_error", str(res["error"])[:300])
        return quality.CommitOutcome(committed=False, flagged=False, tm_eligible=False)
    # Refusal / meta-output GUARD via the sanctioned CHOKE POINT (see _apply_result).
    # A full-text body that came back as a conversational refusal is routed to review
    # by the seam rather than committed as the document target. glossary_terms feeds
    # the deterministic DNT verification (2B) on the committed body.
    outcome = quality.commit_mt_output(
        session,
        seg,
        res["text"],
        source="mt",
        glossary_terms=glossary_terms,
        lang_guard=_lang_guard_for(res),
        # reviewer H1: this IS the full-text body (raw
        # provider output keeps its trailing "\n"), so
        # exclude it from the whitespace seam gate — else
        # every real full-text `doc translate` routes to
        # review on trailing whitespace. Other gates run.
        full_text=True,
        llm_tag_repair=llm_tag_repair,
    )
    if outcome.status_protected:
        # STATUS PROTECTION: the body segment is human-approved/edited, so the seam refused
        # the overwrite and left the row byte-untouched. It is neither a translation nor an
        # error -- count neither, and do NOT stamp mt_confidence. The caller's FT-store gate
        # already consults `committed` (False here), so the stored target never poisons TM.
        return outcome
    if outcome.flagged:
        stats["errors"] += 1
        return outcome
    if res["provider_used"]:
        stats["provider_used"][res["provider_used"]] = (
            stats["provider_used"].get(res["provider_used"], 0) + 1
        )
    seg.mt_confidence = 0.9
    session.add(seg)
    stats["translated"] += 1
    # #58/#59: mirror the segmented path — a soft-gated full-text segment routed to
    # review is counted, so a wrong-language body is no longer a silent `errors: 0`.
    if seg.status == SegStatus.NEEDS_REVIEW:
        stats["needs_review"] += 1
    return outcome


def _grapheme_re():
    import regex  # a direct dependency; \X is the full UAX #29 extended grapheme cluster

    return regex.compile(r"\X")


_GRAPHEME_RE = None


def stacked_cluster_error(text: str | None, limit: int | None = None) -> str:
    """A plain refusal message when ONE user-perceived character of `text` (an extended
    grapheme cluster) carries more than `limit` code points, else "" (#599).

    "e" + 5000 combining accents is a single character of 5001 code points. It looks short,
    but an engine tokenizes every mark, so the output overflows the input-sized token budget
    and the call walks the truncation-escalation ladder for minutes. No real text comes
    close (see config.TRANSLATE_TEXT_MAX_CLUSTER_CODEPOINTS). One linear pass."""
    global _GRAPHEME_RE
    limit = limit or config.TRANSLATE_TEXT_MAX_CLUSTER_CODEPOINTS
    if not text or len(text) <= limit:
        return ""
    if _GRAPHEME_RE is None:
        _GRAPHEME_RE = _grapheme_re()
    for m in _GRAPHEME_RE.finditer(text):
        size = m.end() - m.start()
        if size > limit:
            return (
                "text contains a character with %d stacked marks or joiners at position %d "  # noqa: UP031
                "(limit %d code points per character); remove the stacked marks and retry."
                % (size - 1, m.start(), limit)
            )
    return ""


def translate_text_once(
    text: str,
    source_locale: str,
    target_locale: str,
    *,
    spec: ProviderSpec | None = None,
    context_brief: str | None = None,
    head_override: str | None = None,
) -> dict:
    """Translate ONE raw text string in a single provider call — the seam behind the
    NEW POST /api/translate/text external front door (SPEC-context-metadata phase 1).

    Reuses the SAME prompt builder + `context_brief` seam every other translate path
    uses: `context_brief` (the already-rendered context_pkg block, or None) is passed
    straight into build_prompt's existing `brief_block` parameter — no prompts.py /
    orchestrator.py change (those are phase 2). Goes through run_completion, so the
    SPEND CAP, provider failover, DeadEngineError, mock-awareness and reporting ALL
    apply for free (verified in tests, never reimplemented here). A keyless install
    resolves to the MockProvider exactly like every other translate path.

    The refusal guard applies via quality.is_refusal_check (the sanctioned gated
    predicate) — there is no Segment to write, so this never touches target_text (the
    choke-point tripwire stays satisfied): a hedged/refusal output is reported as an
    error with empty text rather than returned as a translation.

    Returns {text, provider_used, model_used, error} — `text` is "" on an error or a
    suspected refusal. Raises DeadEngineError only if the caller pins a dead engine
    (the API layer catches it); a keyless real provider never reaches here because the
    caller resolves to mock first."""
    # #599 defense in depth: the front door refuses this with a 422 first; a direct caller
    # gets the same refusal as an error result, never a provider call.
    stacked = stacked_cluster_error(text)
    if stacked:
        return {"text": "", "provider_used": None, "model_used": None, "error": stacked}
    from app.services import tags_only as _tags_only

    if _tags_only.is_tags_only(text):
        # #705: no translatable text (only tags / placeholders): the text is its own
        # translation; no provider call, no spend.
        return {"text": text, "provider_used": None, "model_used": None, "error": None}
    spec = spec or spec_for_provider(config.DEFAULT_PROVIDER or "mock")
    system, prompt, payload = build_prompt(
        text,
        source_locale,
        target_locale,
        [],
        brief_block=context_brief or None,
        head_override=head_override,
    )
    # Run envelope (proposal Wave A): a 'direct' auto-action + run so this ad-hoc call
    # belongs to exactly one action, its call record carrying action_id + run_id. There
    # is no project/document here (a raw string through the API front door), so the
    # envelope has only locale identity. Best-effort; (None, None) leaves the call
    # byte-identical. Finalized in the finally.
    from app.models import TapStatus

    _aid, _rid = tap_envelope.begin_direct_envelope(
        source_locale=source_locale, target_locale=target_locale
    )
    _status = TapStatus.FAILED
    try:
        res = run_completion(
            spec,
            prompt,
            system=system,
            task="translate",
            payload=payload,
            tap_meta=tap_envelope.stamp_meta({}, _aid, _rid) or None,
        )
        _status = TapStatus.COMPLETED
    finally:
        tap_envelope.finalize_pair(_aid, _rid, _status)
        # Wave B: enrich the ad-hoc leg. No project/document -> manifest is sparse; the
        # call-file roll-up (one call) still records honestly. Best-effort, never raises.
        tap_enrich.enrich(_aid, _rid, source_locale=source_locale, target_locale=target_locale)
    out_text = res.get("text") or ""
    if res.get("error") and not out_text:
        return {
            "text": "",
            "provider_used": res.get("provider_used"),
            "model_used": res.get("model_used"),
            "error": res.get("error"),
        }
    # Refusal guard (choke-point parity): a conversational hedge is NOT a translation.
    if quality.is_refusal_check(out_text):
        return {
            "text": "",
            "provider_used": res.get("provider_used"),
            "model_used": res.get("model_used"),
            "error": "the engine returned a non-translation (suspected refusal)",
        }
    return {
        "text": out_text,
        "provider_used": res.get("provider_used"),
        "model_used": res.get("model_used"),
        "error": None,
    }
