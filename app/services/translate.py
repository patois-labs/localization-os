"""MT pre-translation: the simple single-engine path that proves the provider
layer end to end. (The multi-agent workflow orchestrator is Chunk 5; it reuses
run_completion + the prompt builder here.)

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports _logger, spec_for_pair, estimate_translation,
_lang_guard_for, _apply_result, _translate_full_text, _apply_result_ft, translate_text_once,
stacked_cluster_error (#599), _pretranslate_stream_impl from app.services.translate_hub,
app.services.translate_hub2.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _logger, DocumentNotFound, _mt_workers,
spec_for_provider, spec_for_project, _new_stats, _USAGE_INTO_USAGE, _USAGE_INTO_STATS,
_accumulate_usage, _merge_usage, _mask_aux, _merge_usage_into_stats, _split_for_chunks,
_begin_direct_envelope from app.services.translate_common.

Order of operations per the TMS model:
  full-text mode  -> check Full-Text TM first; on miss, one whole-doc MT call, then store
  segmented mode  -> per segment: 100% segment-TM short-circuit; else MT with glossary
                     constraints (+ fuzzy TM reference) injected into the prompt

The work is a generator (`pretranslate_stream`) yielding one event per segment so
the SSE endpoint can show it filling in live; `pretranslate_document` drains it.

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlmodel import Session, select

from app import config
from app import db as _db
from app import extensions as _extensions
from app.models import Document, Project, Segment, SegStatus, TransMode
from app.services import (
    ai_tagging,
    context_pkg,
    pii,
    prompts,
    qa,
    quality,
    style_guides,
    tag_llm_repair,
    tap_enrich,
    tap_envelope,
    tm,
)
from app.services.prompts import translation_prompt as build_prompt
from app.services.providers import ProviderSpec, run_completion
from app.services.translate_common import (
    _USAGE_INTO_STATS,
    _USAGE_INTO_USAGE,
    DocumentNotFound,
    _accumulate_usage,
    _begin_direct_envelope,
    _mask_aux,
    _merge_usage,
    _merge_usage_into_stats,
    _mt_workers,
    _new_stats,
    _split_for_chunks,
    spec_for_project,
    spec_for_provider,
)
from app.services.translate_hub import (
    _apply_result,
    _apply_result_ft,
    _lang_guard_for,
    _translate_full_text,
    estimate_translation,
    spec_for_pair,
    stacked_cluster_error,
    translate_text_once,
)
from app.services.translate_hub2 import _pretranslate_stream_impl

# Optional add-on: reached through the extension facade (no-op when absent); name unchanged.
llmemory = _extensions.use("memory")

__all__ = [
    "_USAGE_INTO_STATS",
    "_USAGE_INTO_USAGE",
    "Document",
    "DocumentNotFound",
    "Project",
    "SegStatus",
    "Segment",
    "ThreadPoolExecutor",
    "TransMode",
    "_accumulate_usage",
    "_apply_result",
    "_apply_result_ft",
    "_begin_direct_envelope",
    "_db",
    "_lang_guard_for",
    "_mask_aux",
    "_merge_usage",
    "_merge_usage_into_stats",
    "_mt_workers",
    "_new_stats",
    "_pretranslate_stream_impl",
    "_split_for_chunks",
    "_translate_full_text",
    "ai_tagging",
    "as_completed",
    "build_prompt",
    "config",
    "context_pkg",
    "estimate_translation",
    "llmemory",
    "pii",
    "prompts",
    "qa",
    "quality",
    "run_completion",
    "select",
    "spec_for_pair",
    "spec_for_project",
    "spec_for_provider",
    "stacked_cluster_error",
    "style_guides",
    "tag_llm_repair",
    "tm",
    "translate_text_once",
]


_logger = logging.getLogger("loc_tms.translate")


class MTRoleRefused(PermissionError):
    """The caller's role may not start machine translation (security review #283)."""


def can_start_mt(user) -> bool:
    """May this user START machine translation (paid provider calls that write
    segments)? The one role rule every interactive MT door shares: the web live
    stream, the web background translate and the /api translate. Translator or
    above (`security.can_write`); a viewer never may. With the login wall off
    (single-operator install) there is no role to check, so it is allowed.

    Lives in the service, not only in the web middleware: the middleware's role
    gate covers mutating web methods only, and /api bypasses it entirely."""
    from app.services import security

    if not security.auth_required():
        return True
    return security.can_write(user)


def require_mt_role(user) -> None:
    """Raise MTRoleRefused when `user` may not start machine translation."""
    if not can_start_mt(user):
        raise MTRoleRefused("Your role does not allow starting machine translation.")


def pretranslate_stream(
    session: Session,
    doc_id: int,
    locale: str,
    spec: ProviderSpec | None = None,
    force_pii: bool = False,
    adaptive_feedback: bool = False,
    adaptive_feedback_window=None,
    doc_context_prepass: bool = False,
    context_brief: str | None = None,
    stats_sink: dict | None = None,
):
    """Run-envelope FINALIZING WRAPPER over the direct-MT generator (proposal Wave A).

    A thin pass-through: it opens a 'direct' auto-action + run before the per-segment
    fan-out, forwards every event unchanged, and in a `finally` transitions the envelope
    to completed (normal exhaustion) or failed (an exception / early abandonment). The
    envelope ids are threaded into the per-segment tap_meta inside the impl so every
    captured call belongs to exactly one action. The impl body is byte-identical to the
    pre-envelope function — the split exists only to give finalization a single exit
    hook (the same shape as run_workflow_stream's wrapper)."""
    from app.models import TapStatus
    from app.services import ai_optout, spend

    # AI opt-out (DPAI-08): every direct-MT caller (web SSE, the MT job, the CLI) funnels
    # through here; refuse before the envelope opens so nothing is sent or recorded.
    ai_optout.refuse_for_document(session, doc_id, "machine translation")
    _env_ids = _begin_direct_envelope(session, doc_id, locale)
    _saw_summary = False
    _exc = False
    _abandoned = False
    # #364: the USD the ledger books for this stream (every call, failover included) rides
    # ONLY on the caller's stats_sink as "cost_usd" (for Job.cost_usd). It is never put on
    # a yielded event: the summary reaches translator-role surfaces (the live SSE, the image
    # API and CLI JSON), and AI spend is manager+ data (reports.can_see_cost).
    _booked = spend.CostSink()
    try:
        for _ev in spend.booked_stream(
            _pretranslate_stream_impl(
                session,
                doc_id,
                locale,
                spec,
                force_pii=force_pii,
                adaptive_feedback=adaptive_feedback,
                adaptive_feedback_window=adaptive_feedback_window,
                doc_context_prepass=doc_context_prepass,
                context_brief=context_brief,
                _env_ids=_env_ids,
                stats_sink=stats_sink,
            ),
            _booked,
        ):
            if stats_sink is not None:
                stats_sink["cost_usd"] = _booked.total
            # The impl's own summary event means the translation COMPLETED its work; a
            # consumer that then breaks (e.g. right after summary) has not failed it (M2).
            if isinstance(_ev, dict) and _ev.get("type") == "summary":
                _saw_summary = True
                # the impl's stats dict IS the stats_sink, which carries "cost_usd": strip it
                _ev = {k: v for k, v in _ev.items() if k != "cost_usd"}
            yield _ev
    except GeneratorExit:
        _abandoned = True
        raise
    except BaseException:
        _exc = True
        raise
    finally:
        _aid, _rid = _env_ids
        if _saw_summary:
            _status = TapStatus.COMPLETED
        elif _abandoned:
            # Consumer dropped the stream before the work finished (SSE disconnect):
            # dropped, not failed.
            _status = TapStatus.ABANDONED
        else:
            _status = TapStatus.FAILED
        tap_envelope.finalize_pair(_aid, _rid, _status)
        # Wave B: enrich the finalized direct-MT leg (manifest from project/doc, totals
        # + engine mix from the call file). Best-effort, never raises; no-ops unless
        # terminal. Observer read over persisted state -- no translate-path work.
        tap_enrich.enrich(_aid, _rid, document_id=doc_id, target_locale=locale)


def pretranslate_document(
    session: Session,
    doc_id: int,
    locale: str,
    spec: ProviderSpec | None = None,
    force_pii: bool = False,
    context_brief: str | None = None,
) -> dict:
    stats = _new_stats()
    for ev in pretranslate_stream(
        session, doc_id, locale, spec, force_pii=force_pii, context_brief=context_brief
    ):
        if ev.get("type") == "summary":
            stats = {k: v for k, v in ev.items() if k != "type"}
    return stats
