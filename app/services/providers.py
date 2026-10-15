"""Provider seam.

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports log, _PUMP_SLOTS, _stall_watchdog, _mock_translate,
MockProvider, ClaudeProvider, GeminiProvider, _drain_openai_stream, _drain_openai_text_stream,
OpenAIProvider, local_list_models, _LIST_MODELS_CLOUD_REFS, list_models_for_ref,
resolve_default_model, LocalProvider, _OVERRIDES, _KEYS, _REAL, _ENGINE_CLASSES, register_provider,
_provider_for_profile, get_provider, _get_engine_provider, _UNREAD_PROFILE, provider_is_live from
app.services.providers_hub.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, KEY_IMAGE_TOKEN_ESTIMATE, _WARNED_MOCK_SUBST,
_TAP_EMIT_WARNED, _warn_tap_emit_once, ProviderTruncated, ThinkingBurn, ProviderStalled,
ProviderCallError, NoModelConfigured, est_output_tokens, _OUTPUT_BUDGET_FLOOR, _OUTPUT_BUDGET_MULT,
_output_token_budget, _sized_max_tokens, _explicit_max_tokens, _raise_truncated, _reasoning_tokens,
DeadEngineError, _PROVIDER_MISSING_SETTING, NmtUnsupported, NmtEmptyOutput, Provider, _tokens,
_mock_build_glossary, _QE_BAD_RE, _mock_qe, _MOCK_TERM_STOPWORDS, _mock_prune_terms,
_mock_translate_terms, _claude_system_blocks, _claude_usage, _b64_png, gemini_image_contents,
_normalize_local_base, LOCAL_CONNECT_TIMEOUT_S, LOCAL_TEST_READ_TIMEOUT_S,
local_engine_client_kwargs, _CLOUD_LIST_TIMEOUT_S, _anthropic_list_models, _CHATML_STOP,
_sanitize_chatml, _LocalPromptAdapter, _GEMMA4_RE, _NO_THINK_EFFORT, _local_chat_reasoning_effort,
_THINK_OPEN, _THINK_CLOSE, _LEADING_THINK_RE, _LEADING_THINK_OPEN_RE, _SOURCE_THINK_RE, _ZERO_WIDTH,
_strip_adapter_output, _engine_model, _effective_model, _csv_ids, _lara_extract, _http_date,
_amazon_endpoint, _sigv4_headers, _nmt_usage, _parse_retry_after, _base_lang, _lara_lang,
_profile_is_live, ProviderSpec, VisionCapabilityResolution, usage_token_counts, _require_model,
_resolve_max_tokens, _raise_thinking_burn, UnknownProviderError, ConfiguredProviderUnavailable,
_require_nmt_output, _mock_choose_sense, claude_image_content, openai_image_content, _chat_messages,
_local_client, local_list_models_result, _openai_list_models, _gemini_list_models,
cloud_list_models, _render_qwen35_chatml, _QWEN35_ADAPTER, _QWEN38_ADAPTER, _QWEN35_RE, _QWEN38_RE,
_local_prompt_adapter, LocalEmptyOutput, _require_local_output, _nmt_source_text, _ProfileProvider,
_lara_signed_headers, _raise_for_nmt_status, _deepl_lang, _google_lang, _ms_lang, _amazon_lang,
assert_provider_visible, _finalize_local_adapter_output, _LOCAL_TEST_MAX_TOKENS, local_complete,
_tap_local_path, OpenAICompatibleProvider, DeepLProvider, GoogleTranslateProvider,
MicrosoftTranslatorProvider, CohereProvider, LaraProvider, AmazonTranslateProvider,
_vision_capable_resolved, _is_terminal_error, resolve_vision_capability,
_authorized_ref_is_vision_capable, is_vision_capable from app.services.providers_common,
app.services.providers_part2, app.services.providers_part3, app.services.providers_part4.

Every LLM task in Localization OS goes through a Provider. Callers pass BOTH a
natural-language `prompt` (what a real model reads) AND a structured
`task` + `payload` (what the deterministic MockProvider uses, and what we log).
Real models ignore task/payload and answer the prompt; the mock ignores the
prompt and answers from the payload. That dual channel makes the entire
pipeline testable offline with no API keys.

Chunk 3 ships the seam + MockProvider. Chunk 4 adds real Claude/Gemini/OpenAI
clients, hot-swap, failover, backoff, and streaming on top of this same
interface (so nothing above this file has to change).

Documented in: docs/architecture/engines-and-providers.md
"""

import base64
import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app import config
from app.services.providers_common import (
    _CHATML_STOP,
    _CLOUD_LIST_TIMEOUT_S,
    _GEMMA4_RE,
    _LEADING_THINK_OPEN_RE,
    _LEADING_THINK_RE,
    _MOCK_TERM_STOPWORDS,
    _NO_THINK_EFFORT,
    _OUTPUT_BUDGET_FLOOR,
    _OUTPUT_BUDGET_MULT,
    _PROVIDER_MISSING_SETTING,
    _QE_BAD_RE,
    _SOURCE_THINK_RE,
    _TAP_EMIT_WARNED,
    _THINK_CLOSE,
    _THINK_OPEN,
    _WARNED_MOCK_SUBST,
    _ZERO_WIDTH,
    KEY_IMAGE_TOKEN_ESTIMATE,
    LOCAL_CONNECT_TIMEOUT_S,
    LOCAL_TEST_READ_TIMEOUT_S,
    DeadEngineError,
    GenerationRunaway,
    NmtEmptyOutput,
    NmtUnsupported,
    NoModelConfigured,
    Provider,
    ProviderCallError,
    ProviderRefused,
    ProviderSpec,
    ProviderStalled,
    ProviderTruncated,
    StreamIncomplete,
    ThinkingBurn,
    VisionCapabilityResolution,
    _amazon_endpoint,
    _anthropic_list_models,
    _b64_png,
    _base_lang,
    _claude_retry_after,
    _claude_system_blocks,
    _claude_usage,
    _csv_ids,
    _effective_model,
    _engine_model,
    _explicit_max_tokens,
    _http_date,
    _lara_extract,
    _lara_lang,
    _local_chat_reasoning_effort,
    _LocalPromptAdapter,
    _mock_build_glossary,
    _mock_prune_terms,
    _mock_qe,
    _mock_translate_terms,
    _nmt_usage,
    _normalize_local_base,
    _output_token_budget,
    _parse_retry_after,
    _profile_is_live,
    _raise_truncated,
    _reasoning_tokens,
    _sanitize_chatml,
    _sigv4_headers,
    _sized_max_tokens,
    _strip_adapter_output,
    _tokens,
    _warn_tap_emit_once,
    est_output_tokens,
    gemini_image_contents,
    local_engine_client_kwargs,
    usage_token_counts,
)
from app.services.providers_hub import (
    _ENGINE_CLASSES,
    _KEYS,
    _LIST_MODELS_CLOUD_REFS,
    _OVERRIDES,
    _PUMP_SLOTS,
    _REAL,
    _UNREAD_PROFILE,
    ClaudeProvider,
    GeminiProvider,
    LocalProvider,
    MockProvider,
    OpenAIProvider,
    _drain_openai_stream,
    _drain_openai_text_stream,
    _get_engine_provider,
    _mock_translate,
    _provider_for_profile,
    _stall_watchdog,
    get_provider,
    list_models_for_ref,
    local_list_models,
    provider_is_live,
    provider_name_is_unknown,
    register_provider,
    resolve_default_model,
)
from app.services.providers_part2 import (
    _QWEN35_ADAPTER,
    _QWEN35_RE,
    _QWEN38_ADAPTER,
    _QWEN38_RE,
    ConfiguredProviderUnavailable,
    LocalEmptyOutput,
    UnknownProviderError,
    _amazon_lang,
    _chat_messages,
    _deepl_lang,
    _gemini_list_models,
    _google_lang,
    _lara_signed_headers,
    _local_client,
    _local_prompt_adapter,
    _mock_choose_sense,
    _ms_lang,
    _nmt_source_text,
    _openai_list_models,
    _ProfileProvider,
    _raise_for_nmt_status,
    _raise_thinking_burn,
    _render_qwen35_chatml,
    _require_local_output,
    _require_model,
    _require_nmt_output,
    _resolve_max_tokens,
    assert_provider_visible,
    claude_image_content,
    cloud_list_models,
    local_list_models_result,
    openai_image_content,
)
from app.services.providers_part3 import (
    _LOCAL_TEST_MAX_TOKENS,
    AmazonTranslateProvider,
    CohereProvider,
    DeepLProvider,
    GoogleTranslateProvider,
    LaraProvider,
    MicrosoftTranslatorProvider,
    OpenAICompatibleProvider,
    _finalize_local_adapter_output,
    _is_terminal_error,
    _tap_local_path,
    _vision_capable_resolved,
    local_complete,
)
from app.services.providers_part4 import (
    _authorized_ref_is_vision_capable,
    is_vision_capable,
    resolve_vision_capability,
)

__all__ = [
    "ABC",
    "KEY_IMAGE_TOKEN_ESTIMATE",
    "LOCAL_CONNECT_TIMEOUT_S",
    "LOCAL_TEST_READ_TIMEOUT_S",
    "_CHATML_STOP",
    "_CLOUD_LIST_TIMEOUT_S",
    "_ENGINE_CLASSES",
    "_GEMMA4_RE",
    "_KEYS",
    "_LEADING_THINK_OPEN_RE",
    "_LEADING_THINK_RE",
    "_LIST_MODELS_CLOUD_REFS",
    "_LOCAL_TEST_MAX_TOKENS",
    "_MOCK_TERM_STOPWORDS",
    "_NO_THINK_EFFORT",
    "_OUTPUT_BUDGET_FLOOR",
    "_OUTPUT_BUDGET_MULT",
    "_OVERRIDES",
    "_PROVIDER_MISSING_SETTING",
    "_PUMP_SLOTS",
    "_QE_BAD_RE",
    "_QWEN35_ADAPTER",
    "_QWEN35_RE",
    "_QWEN38_ADAPTER",
    "_QWEN38_RE",
    "_REAL",
    "_SOURCE_THINK_RE",
    "_TAP_EMIT_WARNED",
    "_THINK_CLOSE",
    "_THINK_OPEN",
    "_UNREAD_PROFILE",
    "_WARNED_MOCK_SUBST",
    "_ZERO_WIDTH",
    "AmazonTranslateProvider",
    "ClaudeProvider",
    "CohereProvider",
    "ConfiguredProviderUnavailable",
    "DeadEngineError",
    "DeepLProvider",
    "GeminiProvider",
    "GenerationRunaway",
    "GoogleTranslateProvider",
    "LaraProvider",
    "LocalEmptyOutput",
    "LocalProvider",
    "MicrosoftTranslatorProvider",
    "MockProvider",
    "NmtEmptyOutput",
    "NmtUnsupported",
    "NoModelConfigured",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "Provider",
    "ProviderCallError",
    "ProviderRefused",
    "ProviderSpec",
    "ProviderStalled",
    "ProviderTruncated",
    "StreamIncomplete",
    "ThinkingBurn",
    "UnknownProviderError",
    "VisionCapabilityResolution",
    "_LocalPromptAdapter",
    "_ProfileProvider",
    "_amazon_endpoint",
    "_amazon_lang",
    "_anthropic_list_models",
    "_authorized_ref_is_vision_capable",
    "_b64_png",
    "_base_lang",
    "_chat_messages",
    "_claude_retry_after",
    "_claude_system_blocks",
    "_claude_usage",
    "_csv_ids",
    "_deepl_lang",
    "_drain_openai_stream",
    "_drain_openai_text_stream",
    "_effective_model",
    "_engine_model",
    "_explicit_max_tokens",
    "_finalize_local_adapter_output",
    "_gemini_list_models",
    "_get_engine_provider",
    "_google_lang",
    "_http_date",
    "_is_terminal_error",
    "_lara_extract",
    "_lara_lang",
    "_lara_signed_headers",
    "_local_chat_reasoning_effort",
    "_local_client",
    "_local_prompt_adapter",
    "_mock_build_glossary",
    "_mock_choose_sense",
    "_mock_prune_terms",
    "_mock_qe",
    "_mock_translate",
    "_mock_translate_terms",
    "_ms_lang",
    "_nmt_source_text",
    "_nmt_usage",
    "_normalize_local_base",
    "_openai_list_models",
    "_output_token_budget",
    "_parse_retry_after",
    "_profile_is_live",
    "_provider_for_profile",
    "_raise_for_nmt_status",
    "_raise_thinking_burn",
    "_raise_truncated",
    "_reasoning_tokens",
    "_render_qwen35_chatml",
    "_require_local_output",
    "_require_model",
    "_require_nmt_output",
    "_resolve_max_tokens",
    "_sanitize_chatml",
    "_sigv4_headers",
    "_sized_max_tokens",
    "_stall_watchdog",
    "_strip_adapter_output",
    "_tap_local_path",
    "_tokens",
    "_vision_capable_resolved",
    "_warn_tap_emit_once",
    "abstractmethod",
    "assert_provider_visible",
    "base64",
    "claude_image_content",
    "cloud_list_models",
    "dataclass",
    "est_output_tokens",
    "gemini_image_contents",
    "get_provider",
    "httpx",
    "is_vision_capable",
    "json",
    "list_models_for_ref",
    "local_complete",
    "local_engine_client_kwargs",
    "local_list_models",
    "local_list_models_result",
    "openai_image_content",
    "provider_is_live",
    "provider_name_is_unknown",
    "re",
    "register_provider",
    "resolve_default_model",
    "resolve_vision_capability",
    "threading",
    "usage_token_counts",
]


log = logging.getLogger("loc_tms.providers")


# #606: the longer retry ladder for a local engine that is restarting (see run_completion).
# With the default backoff_base 0.5 the waits are 2, 4, 8, 16, 16, 16, 16 s: the engine gets
# about 78 s to come back (a router restart took 44.5 s to the first good answer).
_OUTAGE_DELAY_CAP_S = 16.0
_OUTAGE_NAMES = (
    "apiconnectionerror",
    "connecterror",
    "connecttimeout",
    "readerror",
    "remoteprotocolerror",
    "streamincomplete",
)


# Review (#606): a local engine that stayed down through the WHOLE extended ladder is marked
# dead for this long. Every later call to it uses the plain short ladder, so a document with
# hundreds of segments against a dead or mistyped engine fails in seconds again instead of
# holding every worker for ~78 s per segment (hours for a big document). The flag clears on
# the next success and expires on its own, so a recovered engine is probed again.
_OUTAGE_DEAD_HOLD_S = 60.0
_OUTAGE_DEAD_UNTIL: dict[str, float] = {}


def _outage_key(prov) -> str:
    return str(getattr(prov, "_profile_base", None) or config.LOCAL_BASE_URL or "")


# Gate (#606): the marked-down state is named in the error text, so an operator reading the
# job error knows why a call failed after a short ladder (prepended: first_error keeps 300 chars).
_OUTAGE_MARKED_NOTE = (
    "engine marked down for 60 s (it stayed unreachable through the whole outage retry "
    "ladder; later calls fail fast until it answers)"
)
_OUTAGE_DOWN_NOTE = (
    "engine marked down (an outage retry ladder ran out less than 60 s ago), so this call "
    "failed fast"
)


def _outage_eligible(prov, spec) -> bool:
    """A LOCAL engine with no failover target, retries on, and the switch on."""
    return (
        getattr(prov, "name", "") == "local"
        and not spec.fallback_provider
        and spec.retry_max > 0
        and int(config.LOCAL_OUTAGE_RETRIES) > 0
    )


def _outage_marked_down(prov) -> bool:
    return _OUTAGE_DEAD_UNTIL.get(_outage_key(prov), 0.0) > time.monotonic()


def _outage_extra_attempts(prov, spec) -> int:
    """Extra attempts a LOCAL engine gets to ride out a restart; 0 for every other
    provider, when a failover target exists (fail over fast instead), when the spec
    disables retries, when config.LOCAL_OUTAGE_RETRIES is 0, or while the engine is
    marked dead by an exhausted ladder (see _OUTAGE_DEAD_UNTIL)."""
    if not _outage_eligible(prov, spec) or _outage_marked_down(prov):
        return 0
    return max(0, int(config.LOCAL_OUTAGE_RETRIES))


def _is_outage_error(e: Exception) -> bool:
    """True for the errors a restarting server produces: the gateway's instant 502/503/504,
    a refused or reset connection, a connect timeout, and a response cut off mid-stream
    (#606). A READ timeout is not one: the engine is up but slow or hung, and each extra
    attempt would hold the worker for the whole PROVIDER_TIMEOUT_S again (gate)."""
    if getattr(e, "status_code", None) in (502, 503, 504):
        return True
    if type(e).__name__.lower() == "apitimeouterror":
        cause = e.__cause__ or e.__context__
        return cause is not None and "connect" in type(cause).__name__.lower()
    return type(e).__name__.lower() in _OUTAGE_NAMES or isinstance(e, ConnectionError)


def run_completion(
    spec: ProviderSpec,
    prompt: str,
    system: str | None = None,
    task: str | None = None,
    payload: dict | None = None,
    tap_meta: dict | None = None,
    max_tokens: int | None = None,
    images: list[bytes] | None = None,
) -> dict:
    """Call the primary provider with retries + exponential backoff; on exhaustion
    (or a terminal error like a bad API key) fail over to the configured backup
    model. Returns a traceable result incl. token usage.

    `images` (Batch 14 T2, SPEC §6.1/§6.3/LAW-IMG-3): OPTIONAL per-string image context
    (send-ready PNG bytes). Capability is decided PER TARGET here: a target whose ref is
    is_vision_capable receives the bytes on the chat/messages path; a NON-vision target
    (NMT, ChatML-adapter, cohere, or a text-only fallback) receives `images=None`. Images
    ride ONLY attempt 0 of a vision target (retries within a target drop them). The result
    dict carries `images_sent` (bool): True iff the SUCCESSFUL complete() call actually
    carried the bytes — so a vision-primary that ERRORED and failed over to a text-only
    target reports False, and the caller records the skip (LAW-IMG-4). None/empty images
    keep every builder's flat-string message byte-identical (LAW-IMG-7).

    `max_tokens` (OPTIONAL starting output budget): callers whose OUTPUT dwarfs their
    short INPUT — the setup/prompt authoring paths, which emit a whole project/workflow
    JSON or a full step prompt from a one-line description — seed a generous starting
    budget here (clamped to the model ceiling) so the FIRST attempt is not truncated by
    the input-sized heuristic (_sized_max_tokens, tuned for translation where output ~=
    input). Escalate-on-truncation still GROWS it toward the ceiling from there. Absent
    (every translate caller), the input-sized default applies — byte-identical to before.
    Output tokens cost nothing unless the model generates them, so a generous seed is
    free; the only bound is the model's real ceiling.

    The result dict carries `truncated` (bool): True when the terminal failure was a
    ProviderTruncated (escalation reached the ceiling and the output still overflowed),
    so an authoring caller can re-raise its typed truncation error with a specific
    message instead of string-sniffing `error`. False on success and on every other
    failure. A pure OBSERVER field — control flow is unchanged.

    `tap_meta` (Prompt Tap): an OPTIONAL, caller-supplied correlation dict
    ({workflow_id, workflow_run_id, step_key, document_id, segment_index}) attached
    to any capture record for this call. It is a pure OBSERVER hook — read only,
    threaded through untouched, never influencing control flow. Callers that don't
    pass it produce records with no meta (honest). See app/services/prompt_tap.py.

    AI opt-out (HECVAT DPAI-08): refuses up front, before any engine is resolved, when
    the current AI scope belongs to an opted-out project (ai_optout.guard raises
    AIOptOutRefused). Every provider's complete() carries the same guard as well."""
    from app.services import ai_optout

    ai_optout.guard("an AI engine call")
    targets = [(spec.provider, spec.model)]
    if spec.fallback_provider:
        targets.append((spec.fallback_provider, spec.fallback_model))

    # Prompt Tap (opt-in observer): resolve the on/off flag ONCE up front so a
    # disabled tap costs one cached Setting read and never touches the filesystem.
    # Every _tap_capture call below is a no-op when this is False; when on, each is
    # wrapped so a tap failure can never perturb this function's control flow (the
    # spend-cap + failover logic below is load-bearing and stays untouched). Lazy
    # import (like spend/tailnet) to avoid any import-cycle at module load. Wrapped so
    # even an import/read failure here can never break a translation (observer purity).
    # The TelemetrySink PORT is now the ONLY telemetry module providers imports (Train 1,
    # BLOCKER-1): it re-exports enabled()/build_record() and owns the redact-then-dispatch
    # write path (telemetry.emit), so providers imports NOTHING from prompt_tap. This
    # import STAYS lazy (never hoisted to module scope): app.ports.telemetry module-scope-
    # imports prompt_tap -> settings -> app.db (engine construction at import), so a hoist
    # would pull app.db into providers' config-only import graph (spec HIGH-1 / lazy-import
    # invariant). It is a swap of the same-site lazy import, not a hoist.
    try:
        from app.ports import telemetry

        _tap_on = telemetry.enabled()
    except Exception:
        telemetry, _tap_on = None, False

    def _tap_capture(
        *,
        provider_name,
        model,
        prov,
        kw,
        outcome,
        response_text=None,
        usage=None,
        error=None,
        attempt=None,
        cost=None,
        finish_reason=None,
        error_class=None,
        elapsed_ms=None,
        output_chars=None,
        reasoning_tokens=None,
        temperature=None,
        seed=None,
    ):
        if not _tap_on:
            return
        try:
            rec = telemetry.build_record(
                provider=provider_name,
                provider_kind=getattr(prov, "name", None),
                model=_effective_model(prov, model),
                task=task,
                system=system,
                prompt=prompt,
                max_tokens=_resolve_max_tokens(prompt, system, model, kw or {}),
                payload=payload,
                outcome=outcome,
                tap_meta=tap_meta,
                response_text=response_text,
                usage=usage,
                error=error,
                attempt=attempt,
                cost=cost,
                finish_reason=finish_reason,
                error_class=error_class,
                elapsed_ms=elapsed_ms,
                output_chars=output_chars,
                reasoning_tokens=reasoning_tokens,
                temperature=temperature,
                seed=seed,
            )
            # Chip task_6f7784ca: stamp which LOCAL path this call took, DERIVED race-free
            # from the resolved model id (a pure function of prov+model; NOT a provider
            # instance attribute, which shared instances across job-worker threads would let
            # the wrong call read). Additive field only; absent for non-local providers.
            local_path = _tap_local_path(prov, model)
            if local_path is not None:
                rec["local_path"] = local_path
            telemetry.emit(rec)
        except Exception:
            # Never-raise stays; but surface the failure ONCE (post-gate L1) so a wedged
            # port/redact/sink is visible, not silently swallowed like the 2026-08-06 tap.
            _warn_tap_emit_once()

    attempts = 0
    last_err = None
    _outage_note = None  # #606 gate: the marked-down note prepended to a terminal outage error
    last_was_truncation = False  # was the TERMINAL failure a ProviderTruncated?
    # run-190 Item C: the structured failure SHAPE of the TERMINAL outcome, threaded out in
    # the result dict so the orchestrator can stamp StepRun.finish_reason / error_class and
    # the run/API/summary surfaces read them (metadata only, no bodies).
    last_finish_reason = None
    last_error_class = None
    # run-190 M2 (gate round 1): the HONEST resolved identity of the LAST attempted call, so a
    # FAILED step's failure_shape names the engine that failed instead of provider=null/
    # model=null. Set in the except below (after used_name/used_model resolve), read by the
    # failure return dict — mirrors the success dict's provider_used/model_used.
    last_provider_used = None
    last_model_used = None
    total_cost = 0.0
    images_sent = False  # did the SUCCESSFUL call carry the image bytes? (LAW-IMG-4)
    for provider_name, model in targets:
        # F7: get_provider RAISES ConfiguredProviderUnavailable (a DeadEngineError
        # subclass) for a keyless CONFIGURED built-in, and DeadEngineError for a dead/
        # unknown ref. That raise is DELIBERATELY here — outside the attempt loop AND
        # the try below — so a keyless configured PRIMARY propagates straight out of
        # run_completion without attempting the fallback target. This is a config
        # error, not a runtime failure: silently failing it over to a keyless built-in
        # would restore the silent-mock degradation, and even a live fallback should
        # not paper over a mis-set primary. Matches the dead-engine-ref contract.
        prov = get_provider(provider_name)
        # F6: when MOCK_FALLBACK_OK substituted a keyless configured built-in with the
        # offline mock, RECORD THE SUBSTITUTION HONESTLY (provider_used="mock",
        # model_used=None) + warn once, so the flag mode can never reproduce the
        # provider_used='claude'/model='claude-haiku-4-5' mislabeling that hid the
        # incident. The condition mirrors get_provider's flag branch EXACTLY (a real
        # built-in, no override, flag on, no key), so a deliberately-registered mock
        # override under a real name is untouched.
        # #373: resolve the model this target's wire call ACTUALLY uses ONCE, here. A spec
        # leaves `model` blank to inherit the engine profile's model; the adapter then sends
        # the profile's model, but the OLD code booked success/failure spend (and stamped
        # model_used) on the BLANK model, which prices at the conservative fallback rate --
        # so an inherited gpt-5.2-pro call booked $0.030 (blank default) not its $0.189,
        # letting expensive profiles out-spend the cap while the ledger under-reported.
        # `used_model` is the honest resolved IDENTITY (returned model_used, StepRun stamps),
        # `priced_model` the id both record_usage sites below price on. They are the same value
        # by construction; they are separate names because the F6 substitution below pins
        # `used_model` to None to label a keyless mock substitution HONESTLY, and that label
        # must never reach pricing: booking the pinned None would fall to pricing._DEFAULT and
        # over-count a call that did have a real rate (#364's failover case catches it). A
        # LocalProvider stays priced exempt either way (billable_model short-circuits it).
        used_name, used_model = provider_name, _effective_model(prov, model)
        priced_model = used_model
        if (
            provider_name in _REAL
            and provider_name not in _OVERRIDES
            and config.MOCK_FALLBACK_OK
            and not _KEYS[provider_name]()
        ):
            used_name, used_model = "mock", None
            if provider_name not in _WARNED_MOCK_SUBST:
                _WARNED_MOCK_SUBST.add(provider_name)
                log.warning(
                    "provider '%s' has no key and MOCK_FALLBACK_OK is set, so it "
                    "resolved to the offline MockProvider: output is deterministic "
                    "placeholder text, recorded as provider 'mock'. This must never "
                    "happen on a configured install (the 2026-08-07 incident).",
                    provider_name,
                )
        # Batch 14 T2: PER-TARGET vision capability (SPEC §6.3/§7.1). Decide ONCE per
        # target whether this ref can carry the image bytes; a non-vision target (NMT,
        # ChatML adapter, cohere, a text-only fallback) gets images=None. Keyed off the
        # REQUESTED ref/model (V-3), not the resolved instance's CAPS, so an offline test
        # double registered under a vision name is treated capable. The spec was authorized
        # upstream, so this private capability-only check does not re-scope it using an absent org.
        target_vision = bool(images) and _authorized_ref_is_vision_capable(
            provider_name, model=model
        )
        # Spend-cap kill switch (safety brake). Before calling a REAL provider, if
        # the cap is on and this period's spend has reached it, do NOT call — return
        # a terminal error so retries/failover treat it as non-retryable and can't
        # hammer past the cap. MockProvider is EXEMPT (it costs nothing / is priced
        # at 0), so tests + offline/local installs never trip. Checked per target so
        # a failover to a real backup is gated too.
        from app import extensions as _extensions
        from app.services import spend

        observability = _extensions.observability
        block_msg = spend.blocked_for(prov)
        if block_msg:
            last_err = block_msg
            last_was_truncation = False  # a spend refusal, not an output overflow
            # Prompt Tap: capture the REQUEST even though the spend cap refused it —
            # a blocked call is exactly when the maintainer wants to see what WOULD have gone
            # out. Observer only; does not touch the continue below.
            _tap_capture(
                provider_name=provider_name,
                model=model,
                prov=prov,
                kw={},
                outcome="refused-by-spend-cap",
                error=block_msg,
            )
            # A terminal error for this provider: skip its retries and try the
            # fallback (which the same check will also block if it's real).
            continue
        # Escalate-on-truncation state (per target). A heuristic-budget truncation
        # retried with the SAME budget truncates identically forever (the maintainer's prod
        # bug: every retry re-derived the same too-small max_tokens), so when a
        # ProviderTruncated carries its budget+ceiling we GROW max_tokens on the next
        # attempt (double toward the ceiling) by passing it as an explicit override.
        # A caller MAY seed a generous STARTING budget (`max_tokens`) — the setup/
        # prompt authoring paths do, since their large JSON/prompt output would be
        # truncated by the input-sized heuristic on the first attempt. Seeded, the
        # first attempt already sends it (clamped to this target's model ceiling) and
        # escalation grows from there; unseeded (every translate caller) it stays None
        # so the input-sized default applies — byte-identical to before.
        esc_budget = min(max_tokens, config.model_output_ceiling(model)) if max_tokens else None
        # run-190 Item B thinking-burn state (per target). `esc_reasoning_effort` is the
        # no-think cure sent on the REACTIVE retry (proactive gemma-4 already sends it inside
        # LocalProvider); `thinking_burn_retried` caps that retry at EXACTLY ONE so a model
        # that STILL burns after the cure fails fast instead of grinding the retry ladder.
        esc_reasoning_effort = None
        thinking_burn_retried = False
        # run-190 M-1 (gate round 3): the burn's own finish shape, remembered when we ARM the
        # cure, so if the cured retry itself 4xx's (a server that rejects reasoning_effort) we
        # can fall back to the honest burn message + shape rather than surfacing the raw HTTP
        # error. None until a burn arms the cure.
        _burn_finish_reason = None
        _burn_error_class = None
        # M-2 (gate round 4): a ONE-SHOT latch armed when a burn SENDS the cure and consumed on
        # the NEXT attempt only. `esc_reasoning_effort` stays set for every later attempt (so the
        # cure keeps riding after e.g. a retryable 503), but only the attempt that IS the cured
        # retry may fold a value-rejection 4xx -- an auth / not-found error on a LATER attempt
        # must surface honestly, never as a fabricated thinking burn.
        _cure_armed = False
        # #606: a LOCAL engine with no failover target rides out a server restart. The MT
        # server answers instant 502s for ~38 s while llama-router restarts (first good
        # answer ~45 s in), and the plain 2-retry / 1.5 s ladder failed whole runs in
        # seconds. Extra attempts, spaced on a longer ladder, apply ONLY to outage-shaped
        # errors (502/503/504, connection errors, a cut stream) on such an engine.
        _outage_extra = _outage_extra_attempts(prov, spec)
        _outage_skipped = not _outage_extra and _outage_eligible(prov, spec)
        for attempt in range(spec.retry_max + _outage_extra + 1):
            block_msg = spend.blocked_for(prov)
            if block_msg:
                last_err = block_msg
                last_was_truncation = False
                break
            attempts += 1
            _t0 = time.monotonic()
            prov.last_usage = None
            attempt_booked = False
            try:
                extra = {"max_tokens": esc_budget} if esc_budget is not None else {}
                # run-190 Item B: the REACTIVE no-think cure rides the retry after a
                # ThinkingBurn (a local chat model that burned its budget on reasoning). A
                # non-local provider ignores an unknown kwarg? No — pass it ONLY when set so
                # every other provider's call is byte-identical.
                # run-190 M-1 (round 3) / M-2 (round 4): is THIS attempt the CURED RETRY (the
                # one immediately after a burn armed the cure)? Consume the one-shot latch. Used
                # in the except handler to fold a cured-retry VALUE-REJECTION 4xx back to the
                # honest burn message -- but never a 4xx on a later attempt (cure still on the
                # wire after a retryable error), which must surface as the real provider error.
                _cure_sent = _cure_armed
                _cure_armed = False
                if esc_reasoning_effort is not None:
                    extra["reasoning_effort"] = esc_reasoning_effort
                # Batch 14 T2 (LAW-IMG-3): image bytes ride ONLY attempt 0 of a
                # vision-capable target; a retry within the same target drops them
                # (text-only). A non-vision target never sees them (target_vision False).
                leg_images = images if (target_vision and attempt == 0) else None
                if leg_images:
                    extra["images"] = leg_images
                text = prov.complete(
                    prompt, system=system, task=task, payload=payload, model=model, **extra
                )
                # #359: an answer carrying the engine's own (or a global) credential is
                # untrusted: fail the attempt (terminal, failover applies) so the key never
                # reaches segment text, the Prompt Tap response, a step output or an export.
                # Spend is still booked by the error path below: from prov.last_usage, or
                # (gate fix) the same length estimate a success would book when the
                # engine reported no usage block, carried as the error's partial_usage.
                from app.services.secrets_scrub import KeyEchoError, guard_answer

                usage = getattr(prov, "last_usage", None) or {
                    "input_tokens": max(1, (len(prompt or "") + len(system or "")) // 4)
                    + (len(leg_images) * KEY_IMAGE_TOKEN_ESTIMATE if leg_images else 0),
                    "output_tokens": max(0, len(text) // 4),
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "estimated": True,
                }
                try:
                    guard_answer(text, prov, provider_name)
                except KeyEchoError as echo:
                    echo.partial_usage = usage
                    raise
                # This SUCCESSFUL call is the one whose verdict the caller inherits: the
                # image rode iff this leg carried it (a vision-primary that errored and
                # failed over to a text-only target lands here with leg_images None ->
                # images_sent False -> the caller records the skip, LAW-IMG-4).
                images_sent = bool(leg_images)
                if _OUTAGE_DEAD_UNTIL:
                    _OUTAGE_DEAD_UNTIL.pop(_outage_key(prov), None)
                # Observability (Train Q2): count this SUCCESSFUL provider call + its
                # tokens. Runs for EVERY call including the offline MockProvider (before
                # the spend-exemption below), so a mock translate moves the counters; a
                # strict no-op when metrics is off. Labels the HONESTLY-RESOLVED identity
                # `used_name` ("mock" for an F6 keyless-mock substitution), NEVER the
                # requested `provider_name` -- mirroring the adjacent _tap_capture, so the
                # metric can't reproduce the 2026-08-07 provider="claude" mislabeling that
                # the F6 comment above exists to prevent. Provider label is bounded
                # (engine:<id> collapses to `engine`); never raises (observer purity).
                observability.observe_provider_call(used_name, "ok", usage)
                # Record ACTUAL spend on the caller's thread (per the ORM rule).
                # Exemption is decided on the RESOLVED instance (a keyless install
                # resolves to MockProvider even when provider_name/model name a real
                # engine — those must NOT accrue). Real calls accrue via the ledger.
                # `call_cost` is the EXACT USD this call added to the ledger (0.0 when
                # exempt, or when the priced cost is 0). Its contribution to total_cost
                # lets the workflow orchestrator stamp per-step spend from the SAME
                # ledger computation, including earlier failed attempts and failover.
                call_cost = 0.0
                if not spend.is_exempt(prov):
                    # #543: the ledger write must NOT live in the provider-failure handler's
                    # reach. A transient ledger error ("database is locked") used to bubble to
                    # the `except` below, which treated the SUCCESSFUL paid call as a provider
                    # failure and retried it (the same translation paid twice). Book separately:
                    # one more try, then log loudly and carry on with the answer we paid for.
                    _bill_model = spend.billable_model(prov, priced_model)
                    for _ledger_try in (1, 2):
                        try:
                            call_cost = spend.record_usage(provider_name, _bill_model, usage)
                            break
                        except Exception:
                            if _ledger_try == 2:
                                call_cost = 0.0
                                try:
                                    from app.services import pricing as _pricing

                                    call_cost = float(
                                        _pricing.cost_usd(provider_name, _bill_model, usage) or 0.0
                                    )
                                except Exception:
                                    call_cost = 0.0  # no estimate; the usage below still is
                                # Enough to reconcile by hand: provider, model, usage, estimate.
                                log.exception(
                                    "spend ledger write failed after a successful call; NOT "
                                    "booked against the cap: provider=%s model=%s usage=%r "
                                    "estimated_cost_usd=%.6f",
                                    provider_name,
                                    _bill_model,
                                    usage,
                                    call_cost,
                                )
                total_cost += call_cost
                attempt_booked = True
                # Prompt Tap: a successful call — capture the exact request + the
                # (capped) response + usage. AFTER the spend write so nothing here
                # can reorder a control-flow side effect.
                # run-190 Item B/C: the wire finish reason ("stop" on a clean local call)
                # + reasoning-token count are LocalProvider observer fields; None for every
                # other provider (getattr default), so their captures are byte-identical.
                _ok_finish = getattr(prov, "last_finish", None)
                _ok_reasoning = getattr(prov, "last_reasoning_tokens", None)
                # #342: the sampling params the local call sent (temperature always; seed
                # None when local_seed is blank). Same observer contract, so non-local
                # captures stay byte-identical.
                _ok_temperature = getattr(prov, "last_temperature", None)
                _ok_seed = getattr(prov, "last_seed", None)
                _tap_capture(
                    provider_name=used_name,
                    model=used_model,
                    prov=prov,
                    kw=extra,
                    outcome="ok",
                    response_text=text,
                    usage=usage,
                    attempt=attempt,
                    cost=call_cost,
                    finish_reason=_ok_finish,
                    elapsed_ms=int((time.monotonic() - _t0) * 1000),
                    output_chars=len(text or ""),
                    reasoning_tokens=_ok_reasoning,
                    temperature=_ok_temperature,
                    seed=_ok_seed,
                )
                # provider_used/model_used carry the HONEST resolved identity (F6):
                # "mock"/None for a flag-mode substitution, else the requested target.
                return {
                    "text": text,
                    "provider_used": used_name,
                    "model_used": used_model,
                    "attempts": attempts,
                    "error": None,
                    "usage": usage,
                    "cost_usd": total_cost,
                    "truncated": False,
                    "failover": provider_name != spec.provider,
                    "images_sent": images_sent,
                    # run-190 Item C: a clean success carries its finish reason ("stop"),
                    # no error class, and the produced char count (metadata only).
                    "finish_reason": _ok_finish,
                    "error_class": None,
                    "output_chars": len(text or ""),
                }
            except Exception as e:
                from app.services.qe_jev import engine_error

                failed_usage = spend.failed_usage(prov, e, prompt, system)
                if failed_usage and failed_usage.get("estimated") and leg_images:
                    failed_usage["input_tokens"] += len(leg_images) * KEY_IMAGE_TOKEN_ESTIMATE
                failed_cost = 0.0
                if failed_usage is not None and not attempt_booked and not spend.is_exempt(prov):
                    try:
                        failed_cost = spend.record_usage(
                            provider_name, spend.billable_model(prov, priced_model), failed_usage
                        )
                    except Exception:
                        # #543: a ledger error while booking a FAILED attempt must not replace
                        # the provider's own error or skip failover/retry handling.
                        log.exception(
                            "spend ledger write failed for a failed %s attempt", provider_name
                        )
                        failed_cost = 0.0
                    total_cost += failed_cost
                last_err = engine_error(f"{type(e).__name__}: {e}", provider_name)
                # M1 (gate round 1): a ThinkingBurn is a ProviderTruncated SUBCLASS, but it is
                # an EMPTY-output reasoning burn, NOT partial-content truncation. Reporting
                # truncated=True made author_prompt / setup_author tell the operator to "pick a
                # provider/model with a larger output token limit" — the exact misleading
                # message Item B(3) exists to kill, one seam over. A burn is NOT a truncation
                # for the purpose of that advice, so the honest error message survives.
                last_was_truncation = isinstance(e, ProviderTruncated) and not isinstance(
                    e, ThinkingBurn
                )
                # M2 (gate round 1): remember WHICH engine this failing attempt used.
                last_provider_used, last_model_used = used_name, used_model
                # run-190 Item C: the structured failure SHAPE of THIS attempt (the
                # exception type name + the wire finish reason it carried, e.g. "length" on
                # a ThinkingBurn/truncation). Threaded out in the result dict for StepRun.
                last_error_class = type(e).__name__
                # LOW-3 (gate round 4): read the finish reason from the EXCEPTION alone on an
                # errored attempt; do NOT fall back to the provider's last-seen `last_finish`,
                # which is a STALE value from a PRIOR success (a generic provider error would
                # otherwise stamp finish_reason='stop' beside error_class='RuntimeError' -- a
                # self-contradictory row). NULL is the honest "unknown" for an errored attempt.
                last_finish_reason = getattr(e, "finish_reason", None)
                # Observability (Train Q2): count this FAILED provider attempt (per
                # attempt, so error rate reflects retries). Labels the honestly-resolved
                # `used_name` (mirrors the ok path). No-op when metrics is off.
                observability.observe_provider_call(used_name, "error")
                # Prompt Tap: capture the request that ERRORED (each attempt) — a
                # failing call is precisely what the maintainer wants to inspect. Observer
                # only; the terminal/backoff/escalate logic below is untouched. run-190
                # Item B/C: carry the finish reason, error class, elapsed, produced chars
                # (0 on an empty burn) and reasoning tokens so the shape is visible.
                _tap_capture(
                    provider_name=provider_name,
                    model=model,
                    prov=prov,
                    kw=extra,
                    outcome="provider-error",
                    usage=failed_usage,
                    cost=failed_cost,
                    error=last_err,
                    attempt=attempt,
                    finish_reason=last_finish_reason,
                    error_class=last_error_class,
                    elapsed_ms=int((time.monotonic() - _t0) * 1000),
                    output_chars=0,
                    reasoning_tokens=getattr(prov, "last_reasoning_tokens", None),
                    # #342: stamped before the wire call (see LocalProvider.complete), so
                    # an errored local attempt still records what it sent.
                    temperature=getattr(prov, "last_temperature", None),
                    seed=getattr(prov, "last_seed", None),
                )
                # run-190 M-1 (gate round 3) / M-2 (gate round 4): the CURED retry
                # (reasoning_effort='none') itself returned a 4xx BECAUSE the server rejected the
                # non-standard value. Fold ONLY that case back to the honest thinking-burn
                # message + shape (never surface the raw HTTP error), so a server that dislikes
                # reasoning_effort still reports the real problem -- the burn. NARROWED (M-2):
                #   * status is 400 / 422 ONLY -- the codes a rejected non-standard BODY VALUE
                #     produces. 401/403/404 (auth / forbidden / not-found) and 429 (quota) are
                #     NOT parameter rejections and MUST surface as the honest provider error,
                #     never be erased into a fabricated burn.
                #   * the error body/text NAMES `reasoning_effort` -- so a 400 that is about
                #     something else (a genuine bad request) is not swallowed either.
                #   * only the actual CURED RETRY may fold (`_cure_sent`, the one-shot latch),
                #     not a later attempt that still carries the cure after a retryable error.
                # Checked BEFORE _is_terminal_error, which would otherwise break with the HTTP
                # error as last_err (400/422 are terminal).
                # LOW-3 (gate round 5): test the FULL untruncated response body (attached by
                # _raise_for_nmt_status as `e.body`) for the parameter name, falling back to the
                # 200-char-capped `last_err` when a raise site attached no body -- so a rejection
                # message naming reasoning_effort past char 200 is not silently missed.
                _fold_body = getattr(e, "body", None) or last_err or ""
                # #499: LocalProvider goes through the openai SDK, whose APIStatusError.body is
                # the PARSED JSON of a JSON error body (a dict/list), not a string. `.lower()` on
                # a dict raised AttributeError INSIDE this except handler, escaping run_completion
                # and skipping failover -- the exact case the fold exists for. Normalize to text
                # first (json.dumps a dict/list, str otherwise) so the check is safe for every
                # raise site, including _raise_for_nmt_status's already-string body.
                if isinstance(_fold_body, (dict, list)):
                    # default=str: this runs inside an except handler, so even an odd
                    # non-JSON value nested in a body must not raise here.
                    _fold_body = json.dumps(_fold_body, default=str)
                else:
                    _fold_body = str(_fold_body)
                if (
                    _cure_sent
                    and getattr(e, "status_code", None) in (400, 422)
                    and "reasoning_effort" in _fold_body.lower()
                ):
                    last_err = (
                        "the local model produced no translation text (its reasoning "
                        "consumed the output budget)"
                    )
                    last_error_class = _burn_error_class or last_error_class
                    last_finish_reason = _burn_finish_reason or last_finish_reason
                    last_was_truncation = False  # a burn is not a partial-content truncation
                    break
                if _is_terminal_error(e):
                    break  # retrying this provider can't help; try the fallback
                # run-190 Item B: a THINKING BURN (empty content + finish=length) must NOT
                # ride the escalate-on-truncation ladder — growing max_tokens only gives the
                # model more room to burn on reasoning (the run-190 waste: 2532->5064->8192
                # over 346s). Retry EXACTLY ONCE with the no-think cure (reasoning_effort=
                # 'none'), budget UNCHANGED, then fail fast with an honest message. Checked
                # BEFORE the generic ProviderTruncated branch (ThinkingBurn IS a subclass).
                if isinstance(e, ThinkingBurn):
                    last_err = (
                        "the local model produced no translation text (its reasoning "
                        "consumed the output budget)"
                    )
                    # M-1 (gate round 3): SEND the cure on the retry to any provider that
                    # FORWARDS `reasoning_effort` to the server (LocalProvider chat path AND
                    # openai_compatible now do -- SUPPORTS_REASONING_EFFORT). Live-proven on LM
                    # Studio: re-POSTing the identical body + reasoning_effort='none' turns a
                    # finish=length/empty burn into a clean finish=stop translation, so the
                    # round-2 decision to SKIP it for openai_compatible traded a REAL cure for a
                    # saved duplicate call. A CAPABILITY flag (not isinstance) so a future
                    # forwarding provider is included automatically. The 400 risk is bounded to
                    # the RETRY of an already-failed call and is caught above (a cured-retry 4xx
                    # folds to the honest burn message). A provider that DROPS reasoning_effort
                    # still skips the wasted byte-identical retry.
                    if (
                        thinking_burn_retried
                        or attempt >= spec.retry_max
                        or not getattr(prov, "SUPPORTS_REASONING_EFFORT", False)
                    ):
                        break  # cured once already, no retry slot, or the cure cannot reach it
                    thinking_burn_retried = True
                    esc_reasoning_effort = _NO_THINK_EFFORT
                    # M-2 (round 4): the NEXT attempt IS the cured retry -- arm the one-shot
                    # latch so only it can fold a value-rejection 4xx.
                    _cure_armed = True
                    _burn_finish_reason = last_finish_reason
                    _burn_error_class = last_error_class
                    # fall through to the backoff below; esc_budget is deliberately UNCHANGED
                # Escalate the output budget on a truncation that carries one (see
                # _raise_truncated). `ceiling`/`budget` are absent on a bare
                # ProviderTruncated (e.g. a provider that didn't attach them) — then
                # we retry with the SAME budget, exactly as before.
                elif isinstance(e, ProviderTruncated):
                    ceiling = getattr(e, "ceiling", None)
                    used = getattr(e, "budget", None)
                    if ceiling is not None:
                        if used is not None and used >= ceiling:
                            # Already at the model's real output ceiling and STILL
                            # truncated: the output genuinely exceeds what this model
                            # can emit. Fail honestly with an actionable message (do
                            # not weaken the guard — truncated text is never returned).
                            last_err = (
                                f"output exceeds model ceiling {ceiling} tokens — "
                                f"consider segmented mode / smaller chunks"
                            )
                            break
                        # Grow toward the ceiling: double the used budget, or jump to
                        # the ceiling when the provider sent none (model default).
                        esc_budget = min(ceiling, used * 2) if used is not None else ceiling
                _outage = bool(_outage_extra) and _is_outage_error(e)
                # Gate: name the marked-down state when it shortened this call's ladder.
                _outage_note = (
                    _OUTAGE_DOWN_NOTE if _outage_skipped and _is_outage_error(e) else None
                )
                if attempt >= spec.retry_max and not _outage:
                    break  # the extra attempts are for outage-shaped errors only
                if _outage:
                    if attempt < spec.retry_max + _outage_extra:
                        time.sleep(min(spec.backoff_base * 4 * (2**attempt), _OUTAGE_DELAY_CAP_S))
                    else:  # the whole extended ladder was spent: stop making others wait
                        _OUTAGE_DEAD_UNTIL[_outage_key(prov)] = (
                            time.monotonic() + _OUTAGE_DEAD_HOLD_S
                        )
                        _outage_note = _OUTAGE_MARKED_NOTE
                elif attempt < spec.retry_max:
                    computed = spec.backoff_base * (2**attempt)
                    # Honor a 429/503 Retry-After (attached by _raise_for_nmt_status):
                    # sleep max(header, computed backoff), CAPPED so a hostile/buggy
                    # header can't park the job. Absent header -> plain backoff
                    # (unchanged). Applies to every NMT kind (they all route status
                    # errors through _raise_for_nmt_status).
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after is None and getattr(prov, "name", "") == "claude":
                        # #634: the Anthropic SDK error carries the response headers
                        # (Retry-After, retry-after-ms, anthropic-ratelimit-*-reset) on a
                        # 429/529. Same cap and max(computed, header) rule as the NMT path.
                        retry_after = _claude_retry_after(e)
                    if retry_after is not None:
                        delay = max(computed, min(retry_after, config.PROVIDER_RETRY_AFTER_CAP_S))
                    else:
                        delay = computed
                    if delay:
                        time.sleep(delay)
    if _outage_note and last_err:
        last_err = f"{_outage_note}: {last_err}"
    return {
        "text": "",
        # M2 (gate round 1): the failing engine's honest identity (None only if no attempt
        # ever resolved a provider), so failure_shape / the Ledger summary name the engine
        # that failed instead of null/null.
        "provider_used": last_provider_used,
        "model_used": last_model_used,
        "attempts": attempts,
        "error": last_err,
        "usage": None,
        "cost_usd": total_cost,
        "truncated": last_was_truncation,
        "failover": False,
        "images_sent": False,
        # run-190 Item C: the terminal failure's structured shape (metadata only) so the
        # orchestrator can stamp StepRun.finish_reason / error_class and the run/API/summary
        # surfaces read them without an SSH into the host Ledger. 0 output chars on a
        # thinking burn / empty-output failure.
        "finish_reason": last_finish_reason,
        "error_class": last_error_class,
        "output_chars": 0,
    }
