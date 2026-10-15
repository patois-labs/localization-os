"""translate: shared leaf helpers (layered pure move)

Documented in: docs/architecture/translation-pipeline.md
"""

import logging

from sqlmodel import Session

from app import config
from app import db as _db
from app.models import Document, Project
from app.services import (
    ai_tagging,
    tap_envelope,
)
from app.services.providers import ProviderSpec

_logger = logging.getLogger("loc_tms.translate")


class DocumentNotFound(LookupError):
    """A translate entry-point was handed a document_id that no longer exists.

    Raised at the service seam so EVERY caller (CLI `doc translate`, the web
    stream, and `job run translate_document`) gets the same clean, catchable
    "no such document" error instead of an AttributeError on `doc.project_id`.
    Callers that already guard doc existence upstream never trigger it."""


def _mt_workers() -> int:
    """Bounded concurrency for per-segment provider calls (`mt_concurrency`
    setting). Only the HTTP/LLM calls run in threads — DB stays single-threaded."""
    from app.services import settings

    try:
        n = int(settings.get("mt_concurrency") or 4)
    except (TypeError, ValueError):
        n = 4
    return max(1, min(16, config.RUN_WORKERS, n))


def spec_for_provider(provider: str) -> ProviderSpec:
    """A ProviderSpec pinned to an explicit provider (a routing-rule / caller
    override), same fallback wiring as spec_for_project. Used by the job handler
    when the API stamped a provider override into the payload (Finding 1)."""
    fallback = config.DEFAULT_FALLBACK_PROVIDER or None
    return ProviderSpec(
        provider=provider,
        model=config.DEFAULT_MODELS.get(provider),
        fallback_provider=fallback,
        fallback_model=config.DEFAULT_MODELS.get(fallback) if fallback else None,
    )


def spec_for_project(project: Project) -> ProviderSpec:
    # Bug (ENTERPRISE_ROADMAP "known stats bug"): stats['failovers'] never
    # incremented because this spec never carried a fallback_provider, so
    # run_completion's `targets` list only ever had the primary engine — the
    # failover branch (and the counter that depends on it) was unreachable on
    # the per-segment translate path, even though it worked on the workflow
    # orchestrator path (WorkflowStep.fallback_provider). A configured backup
    # engine (Setup AI / DEFAULT_FALLBACK_PROVIDER, blank by default) now flows
    # through here too, so a real provider error can actually fail over.
    fallback = config.DEFAULT_FALLBACK_PROVIDER or None
    return ProviderSpec(
        provider=project.default_provider,
        model=config.DEFAULT_MODELS.get(project.default_provider),
        fallback_provider=fallback,
        fallback_model=config.DEFAULT_MODELS.get(fallback) if fallback else None,
    )


def _new_stats():
    # `needs_review` (issue #58/#59): committed segments a SOFT quality gate routed to
    # review (wrong-script/nontranslation, tag-injection, number, placeholder, ...). These
    # do NOT bump `errors` (nothing was withheld — the text IS stored), so before this
    # counter the job summary read `errors: 0` for a document delivered entirely in the
    # wrong language: a silent success. It is the anti-silent surface the whole "wrong-
    # language output silently accepted" class needs — every soft-flagged segment now shows.
    return {
        "translated": 0,
        "needs_review": 0,
        "from_segment_tm": 0,
        "from_full_text_tm": 0,
        "provider_calls": 0,
        "failovers": 0,
        "errors": 0,
        "provider_used": {},
        "tokens_in": 0,
        "tokens_out": 0,
        # Prompt-cache accounting (Feature 0): cache_read is billed ~0.1x and
        # cache_write ~1.25x of normal input. Surfacing both makes real caching
        # visible when it engages (large stable prefixes / OpenAI+Gemini
        # implicit caching); on the stock per-segment path with the ~128-token
        # system prefix these stay 0 because no prefix reaches the cacheable
        # minimum — see config.PROMPT_CACHING for why.
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
    }


_USAGE_INTO_USAGE = {
    k: k for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
}


_USAGE_INTO_STATS = {
    "input_tokens": "tokens_in",
    "output_tokens": "tokens_out",
    "cache_read_tokens": "tokens_cache_read",
    "cache_write_tokens": "tokens_cache_write",
}


# The four token counters accumulated from a run_completion result's usage, mapped
# to their destination-dict key: identity for an aggregate usage dict (_merge_usage),
# or the stats-dict names for the run stats (_merge_usage_into_stats).
def _accumulate_usage(res: dict, dst: dict, keymap: dict) -> None:
    """Accumulate one run_completion result's usage into `dst`, adding each usage
    counter to the dst key `keymap` maps it to. dst.get(...) tolerates a fresh
    aggregate dict (missing keys) and a pre-seeded stats dict alike."""
    u = res.get("usage") or {}
    for src_k, dst_k in keymap.items():
        dst[dst_k] = dst.get(dst_k, 0) + (u.get(src_k, 0) or 0)


def _merge_usage(dst: dict, res: dict) -> None:
    """Accumulate one run_completion result's usage into an aggregate usage dict."""
    _accumulate_usage(res, dst, _USAGE_INTO_USAGE)


def _mask_aux(text, tag_map):
    """Rewrite the RAW `⟦⟧` tokens an AUXILIARY prompt block quotes into THIS call's
    sentinel vocabulary — the seam `orchestrator._run_step_on_segment` already applies
    to its tm/feedback/brief blocks (the 2026-07-25 live-QA mask-hole fix), applied
    here so the AUXILIARY BLOCKS of the plain-MT path stop leaking the notation the
    payload is hiding.

    WHAT THIS DOES **NOT** FIX (stated here because the original wording of this
    docstring read as though the notation mismatch on this path were closed — adversarial
    review S19): the SYSTEM MESSAGE. `prompts.translation_prompt` always describes the
    RAW bracket notation, including on a masked call, because this path does not use the
    orchestrator's notation-specific system text (`orchestrator._system_for`). So a
    masked plain-MT call still shows the model one notation in its instructions and
    another in its content — the same class of mismatch, one layer up, still open. It is
    left for its own reviewed commit because changing that string moves the system bytes
    of every plain-MT call.

    THE HOLE THIS CLOSES (pre-existing; proven against commit d402dd8 by a probe that
    drove this very path and read the outgoing prompt out of a stub provider). This
    module masks ONLY `{text}`/`{source}` (`ai_tagging.mask` on the segment source),
    while the front matter it assembles beside them — the doc/context brief, the TM
    few-shot examples, the adaptive-feedback pairs — QUOTES REAL SEGMENT TEXT and so
    carries raw `⟦g1⟧` literals. The model was shown both notations in one prompt and
    duly learned to emit raw tokens into masked output. Same failure, same fix, one
    helper so the three call sites cannot drift.

    CALL-SITE GATED exactly like `ai_tagging.mask_like` requires: callers invoke this
    only when masking is ON, because an empty `tag_map` is NOT the same as "masking is
    off" (a segment with no tokens of its own can still be handed a brief that quotes
    tokens from elsewhere in the document — the neutral-phrase degradation is correct
    there). Byte-identical when masking is off (never called), when the block is empty,
    and when it quotes no token (the regex matches nothing)."""
    if not text:
        return text
    return ai_tagging.mask_like(text, tag_map)


def _merge_usage_into_stats(res, stats):
    _accumulate_usage(res, stats, _USAGE_INTO_STATS)


def _split_for_chunks(text: str, max_tokens: int) -> list[str]:
    """Paragraph-boundary chunker (shared shape with orchestrator._split_for_chunks)."""
    from app.services.segment import est_tokens

    paras = text.split("\n\n")
    chunks, cur, tok = [], [], 0
    for p in paras:
        t = est_tokens(p)
        if cur and tok + t > max_tokens:
            chunks.append("\n\n".join(cur))
            cur, tok = [], 0
        cur.append(p)
        tok += t
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks or [text]


def _begin_direct_envelope(session: Session, doc_id: int, locale: str):
    """(action_id, run_id) for THIS direct-MT pass's envelope (proposal Wave A): a
    minimal auto-action of kind 'direct' so the per-segment MT calls belong to exactly
    one action (never an 'ungrouped' bucket). Best-effort: (None, None) on any failure
    or when envelopes are off. Called at wrapper entry, before the per-segment fan-out.

    LOCK SAFETY (M4/R2): the envelope is written on an ISOLATED session, so it never
    shares the caller's transaction. We DELIBERATELY do NOT commit_if_staged the caller
    here: this seam runs SYNCHRONOUSLY on the caller's thread at pretranslate ENTRY,
    where the caller may hold UNRELATED staged work it intends to roll back, and
    committing it would move a boundary that is not ours to move
    (verify_llmemory_dispatch_lock enforces exactly this: a translate on a doc with
    nothing pending must leave a rolled-back caller transaction with no trace).

    HONEST failure mode: this is NOT a deadlock, but it is also not free. Because we run
    on the caller's thread, a caller that ENTERS with an uncommitted write would make the
    isolated envelope write block on that caller's own writer lock for the FULL
    busy_timeout (~5s, bounded) and then degrade to (None, None) — a bounded stall plus a
    lost envelope, not a hang. Today's four callers (jobs / CLI / web / image-loc) all
    open a FRESH session for the stream and are read-only at entry, so this never fires;
    it is a latent trap for a future caller. R2 guard: if the caller session already has
    staged work, we SKIP the envelope with a WARN rather than stall — cheap, honest, and
    it keeps the never-move-a-boundary rule."""
    try:
        if _db.session_has_uncommitted_work(session):
            _logger.warning(
                "tap_envelope: skipping direct-MT envelope — caller session "
                "has uncommitted work (would stall the writer lock)"
            )
            return (None, None)
        doc = session.get(Document, doc_id)
        project = session.get(Project, doc.project_id) if doc is not None else None
        return tap_envelope.begin_direct_envelope(
            org_id=getattr(project, "org_id", None),
            customer_id=getattr(project, "customer_id", None),
            project_id=getattr(doc, "project_id", None),
            document_id=doc_id,
            source_locale=getattr(project, "source_locale", "") or "",
            target_locale=locale,
        )
    except Exception:
        return (None, None)
