"""providers: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/engines-and-providers.md
"""

import json
import logging
import math
import re
import threading
import time

from app import config
from app.services import providers_part5 as runaway_guard
from app.services.providers_common import (
    _PROVIDER_MISSING_SETTING,
    _ZERO_WIDTH,
    DeadEngineError,
    JudgeEngineNotAProvider,
    Provider,
    ProviderStalled,
    StreamIncomplete,
    _claude_system_blocks,
    _claude_usage,
    _effective_model,
    _explicit_max_tokens,
    _local_chat_reasoning_effort,
    _mock_build_glossary,
    _mock_prune_terms,
    _mock_qe,
    _mock_translate_terms,
    _profile_is_live,
    _raise_truncated,
    _reasoning_tokens,
    gemini_image_contents,
    is_length_finish,
)
from app.services.providers_part2 import (
    ConfiguredProviderUnavailable,
    UnknownProviderError,
    _chat_messages,
    _local_client,
    _local_prompt_adapter,
    _mock_choose_sense,
    _raise_thinking_burn,
    _require_local_output,
    _require_model,
    _resolve_max_tokens,
    claude_image_content,
    cloud_list_models,
    local_list_models_result,
)
from app.services.providers_part3 import (
    AmazonTranslateProvider,
    CohereProvider,
    DeepLProvider,
    GoogleTranslateProvider,
    LaraProvider,
    MicrosoftTranslatorProvider,
    OpenAICompatibleProvider,
    _finalize_local_adapter_output,
)

log = logging.getLogger("loc_tms.providers")


_PUMP_SLOTS = threading.BoundedSemaphore(config.STREAM_WORKERS)


# Capacity settings are boot-time constants; restart to change this import-time ceiling.
def _stall_watchdog(chunk_iter, on_chunk=None):
    """Consume a streaming chunk iterator on a helper thread, enforcing a STALL
    timeout (reset on every chunk) plus a hard total-time backstop. Yields each
    chunk to the caller as it arrives; raises ProviderStalled if the stream goes
    silent for config.PROVIDER_TIMEOUT_S or runs past config.PROVIDER_MAX_TOTAL_S.

    RUN_WORKERS bounds queued chunks; a full queue waits PROVIDER_TIMEOUT_S then
    records ProviderStalled out of band. Queued chunks precede that error. The
    process-wide STREAM_WORKERS pump reservation lasts until the source closes,
    including an upstream next() that outlives its consumer. Consumer waits are
    event-driven; a backpressured producer checks abandonment every 50 ms.

    This is what turns PROVIDER_TIMEOUT_S from a wall-clock request timeout (which
    killed long-but-healthy translations mid-generation — Feature 7 root cause #2)
    into a liveness check: a working provider streams tokens continuously and
    never trips it; a hung provider goes silent and trips it in ~2 min."""
    import queue as _q

    # Independent process-wide ceiling includes pumps whose upstream next() is stuck.
    # Only the pump releases its reservation, even if its consumer has gone away.
    if not _PUMP_SLOTS.acquire(blocking=False):
        raise ProviderStalled("provider stream pump capacity exhausted")
    try:
        q: _q.Queue = _q.Queue(maxsize=config.RUN_WORKERS)
        stopped = threading.Event()
        finished = threading.Event()
        available = threading.Event()
        failure = []
    except BaseException:
        _PUMP_SLOTS.release()
        raise

    def _pump():
        exhausted = False
        try:
            for ch in chunk_iter:
                if stopped.is_set():
                    break
                put_deadline = time.monotonic() + config.PROVIDER_TIMEOUT_S
                while not stopped.is_set():
                    remaining = put_deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProviderStalled("consumer stalled: provider chunk queue full")
                    try:
                        q.put((None, ch), timeout=min(0.05, remaining))
                        available.set()
                        break
                    except _q.Full:
                        continue
                if stopped.is_set():
                    break
            else:
                exhausted = True
        except Exception as e:
            failure.append(e)
        finally:
            try:
                close = getattr(chunk_iter, "close", None)
                if close:
                    close()
            except Exception as e:
                if not exhausted and not failure:
                    failure.append(e)
                else:
                    log.warning("Provider stream close failed (%s)", type(e).__name__)
            finally:
                finished.set()
                available.set()
                _PUMP_SLOTS.release()

    try:
        threading.Thread(target=_pump, daemon=True).start()
    except BaseException:
        _PUMP_SLOTS.release()
        raise
    deadline = time.monotonic() + config.PROVIDER_MAX_TOTAL_S
    silence_deadline = time.monotonic() + config.PROVIDER_TIMEOUT_S
    try:
        while True:
            # Clear before observing the queue/finished flag: a racing producer
            # either leaves visible state or sets the event after this clear.
            available.clear()
            # Completion/error is out of band: a full queue cannot hide a failure.
            if finished.is_set() and q.empty():
                if failure:
                    raise failure[0]
                return
            try:
                _, val = q.get_nowait()
            except _q.Empty:
                if finished.is_set():
                    continue
                if time.monotonic() > deadline:
                    raise ProviderStalled(
                        f"exceeded {config.PROVIDER_MAX_TOTAL_S}s total (hard ceiling)"
                    ) from None
                if time.monotonic() >= silence_deadline:
                    raise ProviderStalled(
                        f"no output for {config.PROVIDER_TIMEOUT_S}s (stall timeout)"
                    ) from None
                available.wait(max(0, min(deadline, silence_deadline) - time.monotonic()))
                continue
            if time.monotonic() > deadline:
                raise ProviderStalled(
                    f"exceeded {config.PROVIDER_MAX_TOTAL_S}s total (hard ceiling)"
                )
            silence_deadline = time.monotonic() + config.PROVIDER_TIMEOUT_S
            if on_chunk:
                on_chunk(val)
            yield val
    finally:
        stopped.set()


def _mock_translate(payload: dict) -> str:
    text = payload.get("text", "")
    target = payload.get("target", "xx")
    out = text
    for g in payload.get("glossary", []):
        if g.get("dnt"):
            continue  # leave do-not-translate terms verbatim
        tgt = g.get("target_term")
        src = g.get("source_term")
        if tgt and src:
            out = re.sub(rf"\b{re.escape(src)}\b", lambda _m, _t=tgt: _t, out)
    return f"[{target}] {out}"


class MockProvider(Provider):
    name = "mock"

    # When set, complete() records deterministic fake cache-usage fields into
    # last_usage so the stats/Job.result cache plumbing is testable offline.
    # First call of a (system) prefix "writes"; subsequent identical prefixes
    # "read" — mirroring Anthropic's warm-then-hit behavior. Off by default so
    # existing suites see byte-identical usage (None -> estimated).
    emit_cache_usage = False
    _seen_prefixes: set = set()  # noqa: RUF012
    # Batch 14 T2: the COUNT of images the most recent complete() call received, so a
    # hermetic offline test (a MockProvider registered under a vision name, V-3) can
    # assert the full resolve->thread->provider path carried N images WITHOUT any SDK/key.
    # NEVER folded into the returned output string (M-11: the `[locale] text` convention
    # is asserted across suites; polluting it corrupts stored translations). Stays
    # deterministic + spend-exempt: recording a count changes no output or usage.
    last_images = 0

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw) -> str:
        payload = payload or {}
        self.last_images = len(images) if images else 0
        if MockProvider.emit_cache_usage:
            key = system or ""
            first = key not in MockProvider._seen_prefixes
            MockProvider._seen_prefixes.add(key)
            pref = max(0, len(key) // 4)
            self.last_usage = {
                "input_tokens": max(1, len(prompt or "") // 4),
                "output_tokens": 8,
                "cache_write_tokens": pref if first else 0,
                "cache_read_tokens": 0 if first else pref,
            }
        if task == "choose_sense":
            return json.dumps({"index": _mock_choose_sense(payload)})
        if task == "build_glossary":
            return json.dumps(_mock_build_glossary(payload.get("text", "")))
        if task == "translate":
            return _mock_translate(payload)
        if task == "qe":
            # QE routing gate (qe.py score_segment): return canned MQM judge JSON
            # in exactly the shape qe.score_segment parses, so the gate scores +
            # routes for real on the zero-key offline install (see _mock_qe above
            # for the deterministic pass/fail rule).
            return _mock_qe(payload)
        if task == "doc_brief":
            # F2.2 document-context pre-pass: a deterministic canned brief so the
            # offline suite is hermetic. Names the pair + echoes a truncated sample
            # tag so a test can assert the brief is present + stable, and so
            # adaptive._sanitize_brief has real plain text to pass through. A real
            # model reads the sample and writes a genuine orientation brief.
            src = payload.get("source", "xx")
            tgt = payload.get("target", "yy")
            return (
                f"Domain: general. Register: neutral. Audience: general readers. "
                f"Tone: clear and idiomatic. Translate from {src} to {tgt} "
                f"consistently."
            )
        if task == "review":
            # a "reviewer" agent returns the text it was given (optionally tweaked)
            return payload.get("text", prompt or "")
        if task == "alternates":
            n = int(payload.get("n", 3))
            text = payload.get("text", "")
            target = payload.get("target", "xx")
            return json.dumps([f"[{target} v{i + 1}] {text}" for i in range(n)])
        if task == "adapt_prompt":
            # Deterministic cross-language adaptation (P5, item 4): return the source
            # instruction with an explicit "[adapted <src_pair> -> <tgt_pair>]" marker
            # prepended so a test can assert the rewrite ran + differs from the source,
            # while the placeholders in the original body pass through verbatim (the
            # integrity guard in workflows.adapt_instruction then holds). A real model
            # ignores task/payload and rewrites the prompt from the natural-language
            # instruction instead.
            body = payload.get("text", "")
            src_pair = payload.get("src_pair", "src")
            tgt_pair = payload.get("tgt_pair", "tgt")
            return f"[adapted {src_pair} -> {tgt_pair}] {body}"
        if task == "prune_terms":
            # Term-extraction precision-prune (feat/term-extraction): return the JSON
            # list of candidate source terms to KEEP (see _mock_prune_terms rule).
            return _mock_prune_terms(payload)
        if task == "translate_terms":
            # Term-extraction proposed translations (feat/term-extraction): a JSON map
            # of source term -> proposed target for the run's locale.
            return _mock_translate_terms(payload)
        if task == "author_prompt":
            desc = payload.get("description", "a localization workflow step")
            return (
                f"You are {desc}. Work on the text below, translating/transforming it "
                "from {source_locale} to {target_locale}. Honor this glossary exactly: "
                "{glossary}. Preserve all placeholders and tags. Output only the result.\n\n{text}"
            )
        # generic echo (used by free-form orchestrator agents)
        return (prompt or "").strip()


class ClaudeProvider(Provider):
    name = "claude"
    CAPS = {  # noqa: RUF012
        "supports_prompts": True,
        "supports_formality": False,
        "supports_glossary": False,
        "supports_vision": True,
    }

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw) -> str:
        model = _require_model("claude", _effective_model(self, kw.get("model")))
        import anthropic

        client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY, timeout=config.PROVIDER_TIMEOUT_S
        )
        # Batch 14 T2: when images ride this call, the user content becomes a content-block
        # LIST (image block(s) + text); absent, the flat prompt string is byte-identical
        # to before (LAW-IMG-7). run_completion only threads images to a vision-capable
        # target, so this branch never fires for a non-vision engine.
        user_content = claude_image_content(prompt, images) if images else prompt
        args = {
            "model": model,
            "max_tokens": _resolve_max_tokens(prompt, system, model, kw),
            "messages": [{"role": "user", "content": user_content}],
        }
        blocks = _claude_system_blocks(system)
        if blocks:
            args["system"] = blocks
        # Stream internally: the SDK avoids request timeouts this way (and refuses
        # very large max_tokens on non-streaming calls). The stall watchdog turns a
        # silent stream into a fast, retryable failure; a healthy long stream runs.
        with client.messages.stream(**args) as s:
            parts = []
            try:
                for piece in _stall_watchdog(s.text_stream):
                    parts.append(piece)
                msg = s.get_final_message()
            except Exception:
                from app.services.spend import estimate_usage

                # A started stream can be billed even when its final usage never arrives.
                self.last_usage = estimate_usage(prompt, "".join(parts), system)
                raise
        self.last_usage = _claude_usage(getattr(msg, "usage", None))
        # #606 review: a stream cut before message_stop leaves no stop_reason; the text so far
        # is a fragment, never a finished translation (the SDK does not raise for it).
        if getattr(msg, "stop_reason", "end_turn") is None:
            _raise_stream_incomplete("".join(parts))
        # Truncation guard: a max_tokens stop is a PARTIAL answer, never a success.
        if getattr(msg, "stop_reason", None) == "max_tokens":
            _raise_truncated(
                f"claude hit max_tokens ({args['max_tokens']}) - output truncated",
                budget=args["max_tokens"],
                model=model,
                finish_reason="max_tokens",  # M-1 (round 5): claude's wire stop reason
            )
        # #633: the API itself reported a refusal. The content is not a translation (it can be
        # a partial answer or policy text); hand back the refusal sentinel so the one refusal
        # guard quarantines it on every path instead of writing it. Usage is already booked.
        if getattr(msg, "stop_reason", None) == "refusal":
            return _provider_refusal_text()
        return "".join(b.text for b in msg.content if b.type == "text")


class GeminiProvider(Provider):
    name = "gemini"
    CAPS = {  # noqa: RUF012
        "supports_prompts": True,
        "supports_formality": False,
        "supports_glossary": False,
        "supports_vision": True,
    }

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw) -> str:
        from google import genai
        from google.genai import types

        model = _require_model("gemini", _effective_model(self, kw.get("model")))
        client = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options={"timeout": config.PROVIDER_TIMEOUT_S * 1000},
        )
        # Honor an explicit max_tokens (setup authoring's large-output budget, BUG 5)
        # as Gemini's max_output_tokens; absent, leave it unset so the model default
        # applies (translation path unchanged -- it never passes max_tokens).
        gen_kw = {}
        if system:
            gen_kw["system_instruction"] = system
        explicit_mt = _explicit_max_tokens(kw, model)
        if explicit_mt is not None:
            gen_kw["max_output_tokens"] = explicit_mt
        cfg = types.GenerateContentConfig(**gen_kw) if gen_kw else None
        # Stream internally so PROVIDER_TIMEOUT_S is a stall timeout, not a wall
        # clock that kills a long-but-healthy generation. Gemini caches implicitly;
        # we only capture its reported cached-token count.
        # Batch 14 T2: when images ride this call, contents becomes [Part(image)... , prompt];
        # absent, it is the flat prompt string (byte-identical, LAW-IMG-7).
        contents = gemini_image_contents(prompt, images) if images else prompt
        parts, final = [], None
        it = client.models.generate_content_stream(model=model, contents=contents, config=cfg)
        try:
            for chunk in _stall_watchdog(it):
                if getattr(chunk, "text", None):
                    parts.append(chunk.text)
                final = chunk
        except Exception:
            from app.services.spend import estimate_usage

            self.last_usage = (
                estimate_usage(prompt, "".join(parts), system) if final is not None else None
            )
            raise
        u = getattr(final, "usage_metadata", None)
        # Gemini's prompt_token_count INCLUDES cached_content_token_count: report the
        # non-cached remainder as input_tokens so the cached slice is counted once (#506).
        g_cached = int(getattr(u, "cached_content_token_count", 0) or 0)
        self.last_usage = (
            {
                "input_tokens": max(0, int(getattr(u, "prompt_token_count", 0) or 0) - g_cached),
                "output_tokens": getattr(u, "candidates_token_count", 0) or 0,
                "cache_read_tokens": g_cached,
                "cache_write_tokens": 0,
            }
            if u
            else None
        )
        # Truncation guard (Gemini finishReason == MAX_TOKENS on a candidate).
        cand = (getattr(final, "candidates", None) or [None])[0]
        fr = str(getattr(cand, "finish_reason", "") or "")
        # #606 review: the last chunk of a whole Gemini stream carries a finish_reason; a
        # candidate without one means the stream was cut mid-body (fragment, not an answer).
        if cand is not None and not fr:
            _raise_stream_incomplete("".join(parts))
        # #633 review: Gemini's own safety/policy block is a refusal, not a translation.
        _blocked = bool(getattr(getattr(final, "prompt_feedback", None), "block_reason", None))
        if _blocked or any(
            k in fr.upper() for k in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII")
        ):
            return _provider_refusal_text()
        if "MAX_TOKENS" in fr.upper():
            # budget is the explicit max_output_tokens if we set one, else None (the
            # model default applied) — run_completion escalates to the ceiling either way.
            _raise_truncated(
                "gemini finish_reason=MAX_TOKENS - output truncated",
                budget=explicit_mt,
                model=model,
                finish_reason=fr,  # M-1 (round 5): the candidate's live finish reason
            )
        return "".join(parts)


def _provider_refusal_text() -> str:
    """#633: the sentinel returned in place of model text when the API reports a refusal."""
    from app.services import refusal
    from app.services.providers_common import ProviderRefused

    # The sentinel is not model text: with the guard kill switch off nothing downstream
    # would quarantine it, so fail the attempt instead of letting it be written (review).
    if not config.REFUSAL_GUARD_ENABLED:
        raise ProviderRefused("the API reported a refusal (REFUSAL_GUARD_ENABLED is off)")
    return refusal.PROVIDER_REFUSAL_TEXT


def _raise_stream_incomplete(text: str) -> None:
    """Raise the retryable StreamIncomplete for a non-OpenAI stream that ended early (the
    caller already stored last_usage, which run_completion books for the billed attempt)."""
    exc = StreamIncomplete(
        "the engine's stream ended without a finish_reason (the response was cut off, "
        "for example by a server restart)"
    )
    exc.partial_output = text
    raise exc


def _require_finish(parts, usage, finish) -> None:
    """#606: a streamed completion that ends with no finish_reason was cut mid-body (the
    200 header was already sent, then the server went away). Fail it, carrying the
    partial text and usage like any other mid-stream failure, so the fragment is never
    taken as a finished translation."""
    if finish:
        return
    exc = StreamIncomplete(
        "the engine's stream ended without a finish_reason (the response was cut off, "
        "for example by a server restart)"
    )
    exc.partial_output = "".join(parts)
    exc.partial_usage = OpenAIProvider()._usage(usage)
    raise exc


def _drain_openai_stream(stream, watch=None):
    """Accumulate an OpenAI-style streamed chat completion through the stall watchdog,
    returning (text, usage, finish_reason). The delta-content accumulation + usage /
    finish_reason capture is byte-identical between OpenAIProvider and LocalProvider;
    each caller keeps its own last_usage shaping + finish=="length" truncation guard
    (their messages/usage dicts differ), so ordering is unchanged."""
    parts, usage, finish = [], None, None
    try:
        for chunk in _stall_watchdog(stream):
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            for ch in chunk.choices or []:
                if ch.delta and ch.delta.content:
                    parts.append(ch.delta.content)
                    if watch is not None:
                        # #720: raises GenerationRunaway on a loop / overlong answer.
                        watch.feed(ch.delta.content)
                if ch.finish_reason:
                    finish = ch.finish_reason
    except Exception as exc:
        exc.partial_output = "".join(parts)
        exc.partial_usage = OpenAIProvider()._usage(usage)
        raise
    _require_finish(parts, usage, finish)
    return "".join(parts), usage, finish


def _drain_openai_text_stream(stream, watch=None):
    """Sibling of _drain_openai_stream for the RAW TEXT-completions endpoint
    (client.completions.create) — the LOCAL ChatML-adapter path (qwen3.5 / qwen3.8)
    only. A chat delta carries
    choices[0].delta.content; a text-completions chunk carries choices[0].text
    instead. Everything else (the stall-watchdog accumulation + usage / finish_reason
    capture) is identical. Kept SEPARATE rather than folded into _drain_openai_stream
    so the chat drain's behaviour stays byte-identical for every non-adapter caller —
    the chat path must not grow a `getattr(ch, "text")` branch it never needs."""
    parts, usage, finish = [], None, None
    try:
        for chunk in _stall_watchdog(stream):
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            for ch in chunk.choices or []:
                if getattr(ch, "text", None):
                    parts.append(ch.text)
                    if watch is not None:
                        watch.feed(ch.text)  # #720
                if ch.finish_reason:
                    finish = ch.finish_reason
    except Exception as exc:
        exc.partial_output = "".join(parts)
        exc.partial_usage = OpenAIProvider()._usage(usage)
        raise
    _require_finish(parts, usage, finish)
    return "".join(parts), usage, finish


class OpenAIProvider(Provider):
    name = "openai"
    CAPS = {  # noqa: RUF012
        "supports_prompts": True,
        "supports_formality": False,
        "supports_glossary": False,
        "supports_vision": True,
    }

    def _usage(self, u):
        if not u:
            return None
        # OpenAI caches prompts > 1024 tokens automatically; the cached slice is
        # reported under prompt_tokens_details.cached_tokens.
        details = getattr(u, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details else 0
        # OpenAI's prompt_tokens INCLUDES the cached slice, but our usage dict's
        # input_tokens is the NON-cached remainder (pricing bills cache_read separately
        # and usage_token_counts adds it back), so subtract it once (#506 item 2).
        cached = int(cached or 0)
        return {
            "input_tokens": max(0, int(getattr(u, "prompt_tokens", 0) or 0) - cached),
            "output_tokens": getattr(u, "completion_tokens", 0) or 0,
            "cache_read_tokens": cached,
            "cache_write_tokens": 0,
        }

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw) -> str:
        from openai import OpenAI

        model = _require_model("openai", _effective_model(self, kw.get("model")))
        client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=config.PROVIDER_TIMEOUT_S)
        mt = _resolve_max_tokens(prompt, system, model, kw)
        # Stream internally (stall timeout, no wall-clock kill) and ask for a final
        # usage chunk so cached-token counts + finish_reason are captured.
        stream = client.chat.completions.create(
            model=model,
            messages=_chat_messages(prompt, system, images),
            max_tokens=mt,
            stream=True,
            stream_options={"include_usage": True},
        )
        text, usage, finish = _drain_openai_stream(stream)
        self.last_usage = self._usage(usage)
        if is_length_finish(finish):
            _raise_truncated(
                "openai finish_reason=length - output truncated",
                budget=mt,
                model=model,
                finish_reason=finish,  # M-1 (round 5): the live wire reason ("length")
            )
        if finish == "content_filter":  # #633: the provider's own refusal signal
            return _provider_refusal_text()
        return text


def local_list_models(base_url=None, api_key=None, *, transport=None) -> list[str]:
    """Models loaded on the local server. Fail soft to [] on refusal or error."""
    return local_list_models_result(base_url, api_key, transport=transport)["models"]


_LIST_MODELS_CLOUD_REFS = ("claude", "gemini", "openai")


# --- unified model-listing seam (workflow-step dropdowns + `providers models` CLI) ---
# ONE dispatch point the web route (/workflows/models) and the CLI (`providers models`)
# both call — the parity rule (one source of truth for "what models does this provider
# ref offer?"). Built on the per-provider helpers above (cloud_list_models /
# local_list_models); adds engine:<id> resolution with an org-ownership + enabled gate.
def list_models_for_ref(provider_ref: str, org_id=None, *, transport=None) -> list[str]:
    """Model ids offered for a provider REFERENCE. Fail-soft [] on ANY error — never
    raises (a model-picker helper must never break a page render or a CLI command).

    ref shapes:
      * "claude" / "gemini" / "openai" -> cloud_list_models (the CONFIGURED key ONLY).
      * "local"                        -> local_list_models(config.LOCAL_BASE_URL,
                                          config.LOCAL_API_KEY).
      * "engine:<id>"                  -> the EngineProfile must belong to `org_id`
        (when given) AND be enabled, else []. For kinds `local` / `openai_compatible`
        (OpenAI-style /models endpoint) list via local_list_models(profile.base_url,
        dec_key(profile.api_key_enc)); every OTHER kind (deepl/microsoft/amazon/lara/
        cohere/google) returns [] (no listable /models endpoint — the UI falls back to
        a manual input).
      * "" / unknown                   -> [].

    SECURITY: this seam NEVER accepts a caller-supplied base_url or api_key — only
    stored/config values — so it can never be coerced into sending a stored/config
    key to an attacker host (see cloud_list_models' base_url note). `transport` is a
    TEST-ONLY httpx injection for the cloud path (hermetic tests), NOT a network
    override — it does not carry a URL, only intercepts the SDK's official host."""
    ref = (provider_ref or "").strip().lower()
    try:
        if ref in _LIST_MODELS_CLOUD_REFS:
            return cloud_list_models(ref, transport=transport)
        if ref == "local":
            from app.services.credential_origin import slot_key_for

            # Read the slot URL ONCE (gate round 2, LOW-1): slot_key_for re-reads it and
            # answers the placeholder if it moved in between.
            slot = config.LOCAL_BASE_URL
            return local_list_models(slot, slot_key_for(slot))
        if ref.startswith("engine:"):
            try:
                pid = int(ref.split(":", 1)[1])
            except (ValueError, IndexError):
                return []
            from app.services import engines

            prof = engines.get_profile(pid)
            if prof is None or not prof.enabled:
                return []
            # Org-ownership gate: a foreign-org engine ref is never listed (so the
            # workflow route can't enumerate another org's engine models).
            if org_id is not None and prof.org_id != org_id:
                return []
            if prof.kind in ("local", "openai_compatible"):
                from app.services.credential_origin import slot_key_for

                base = prof.base_url or config.LOCAL_BASE_URL
                # Gate round 1 HIGH-1: a keyless profile gets the slot key only when it
                # points at the slot's own origin; anywhere else, the placeholder.
                key = engines.dec_key(prof.api_key_enc) or slot_key_for(prof.base_url)
                return local_list_models(base, key)
            return []  # deepl/microsoft/amazon/lara/cohere/google — no /models endpoint
        return []
    except Exception:
        return []


def resolve_default_model(provider_ref: str, model=None, org_id=None) -> str:
    """The model id a DIRECT/authoring call should use when the caller left it blank.

    A caller-supplied model (a UI dropdown pick, a CLI --model, a step model) ALWAYS
    wins. When it is blank:
      * a BUILT-IN cloud provider (claude/gemini/openai) falls back to
        config.DEFAULT_MODELS[<p>], and if THAT is empty too (the exact hole behind
        the "no model configured for provider 'claude'" failure) to the first live
        model id list_models_for_ref returns -- which needs a configured key, so it
        resolves precisely when a real call is about to be made anyway. This mirrors
        how the translate/QE specs resolve a cloud default.
      * an engine:<id> / local / mock ref keeps a BLANK model: the engine profile's
        own model (or the local/mock default) is the right value, and forcing a
        built-in default onto them would be wrong.
    A cloud ref with no default AND no listable model returns "" so the provider's own
    empty-model guard fires with its clean, actionable message (the acceptable final
    fallback -- rare once a model dropdown is present). NEVER raises: a model resolver
    must not be able to break an authoring call or a page render."""
    m = (model or "").strip()
    if m:
        return m
    ref = (provider_ref or "").strip().lower()
    if ref in _LIST_MODELS_CLOUD_REFS:
        default = (config.DEFAULT_MODELS.get(ref) or "").strip()
        if default:
            return default
        try:
            listed = list_models_for_ref(ref, org_id)
        except Exception:
            listed = []
        if listed:
            return listed[0]
    return ""


# #342: the run_completion `task` values that JUDGE/SCORE rather than generate
# (LQA, QE, the Jev/structural judges and the verification calls). They sample at
# local_judge_temperature; every other task (translate, post-edit, authoring,
# context, image) is NOT here and gets local_temperature (the safe default for
# an unknown task: generation quality, not verdict stability).
_LOCAL_JUDGE_TASKS = frozenset(
    {
        "lqa",
        "qe",
        "jev_refine",
        "quality_judge",
        "adjudicate",
        "verify_rules",
        "rules_verify",
        "tag_verify",
    }
)


def _local_sampling(task=None):
    """#342: the (temperature, seed) a local-engine call with `task` must send.

    Read LIVE from the install settings (so a Settings save changes the NEXT call,
    no restart; mirrors how every other Setting takes effect). Temperature is
    always sent; seed is None (send no seed key) when local_seed is blank.
    NEVER raises: a settings read/parse failure (hand-edited row, a process with
    no settings table yet) falls back to the DEFAULTS value, never to a server
    default and never to a broken translation."""
    from app.services import settings

    is_judge = task in _LOCAL_JUDGE_TASKS
    temp_key = "local_judge_temperature" if is_judge else "local_temperature"
    fallback = "0.0" if is_judge else "0.2"
    try:
        temp = float(settings.get(temp_key))
        if not math.isfinite(temp):  # a hand-edited "nan"/"inf" row must not reach the wire
            temp = float(fallback)
    except Exception:
        temp = float(fallback)
    try:
        seed_raw = str(settings.get("local_seed") or "").strip()
    except Exception:
        seed_raw = "42"
    try:
        seed = int(seed_raw) if seed_raw else None
    except ValueError:
        seed = None
    if seed is not None and seed < 0:  # validators refuse it; a hand-edited row sends no seed
        seed = None
    return temp, seed


def local_sampling_kwargs(task=None) -> dict:
    """#342: the OpenAI-SDK sampling kwargs ({temperature[, seed]}) for a local-engine call
    that does NOT go through LocalProvider.complete (the OCR and image-localization vision
    calls). Same settings, same judge/generate split, same blank-seed rule."""
    temperature, seed = _local_sampling(task)
    return {"temperature": temperature, **({"seed": seed} if seed is not None else {})}


class LocalProvider(Provider):
    name = "local"
    # run-190 M-1 (round 3): the chat path forwards `reasoning_effort` (extra_body), so the
    # reactive thinking-burn cure reaches the server here.
    SUPPORTS_REASONING_EFFORT = True
    # supports_vision documents the CHAT path's capability (the runtime gate,
    # is_vision_capable, keys off the resolved model so a ChatML-adapter model like
    # qwen3.5 is correctly excluded even though this class-level flag is True).
    CAPS = {  # noqa: RUF012
        "supports_prompts": True,
        "supports_formality": False,
        "supports_glossary": False,
        "supports_vision": True,
    }

    # An EngineProfile-backed local engine binds these per-instance (see
    # _provider_for_profile) so ONE self-hosted card can point at its own
    # ip/key/model; unset (the legacy single-slot use) -> config globals, unchanged.
    _profile_base = None
    _profile_key = None
    _profile_model = None
    # None in production; tests set an httpx.MockTransport (mirrors _ProfileProvider's
    # `_transport`) so the local request/response shape — including the qwen3.5 / qwen3.8
    # raw completions path — is verifiable offline with no live server.
    _transport = None
    # run-190 Item B/C observer fields (last completion's wire finish reason + reasoning-
    # token count). Class-level None so run_completion can getattr them race-tolerantly on
    # a shared instance exactly as it does last_usage; set per call on the chat path.
    last_finish = None
    last_reasoning_tokens = None
    # #342: the sampling params the last call SENT (temperature always; seed None when
    # local_seed is blank). Same observer contract as last_finish — metadata for the
    # prompt-tap / LLM Ledger record, local calls only (None for every other provider).
    last_temperature = None
    last_seed = None

    def _client(self):
        if self._profile_base is not None:
            return _local_client(self._profile_base, self._profile_key, transport=self._transport)
        return _local_client(transport=self._transport)

    def _model(self, kw):
        return _effective_model(self, kw.get("model"))

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw) -> str:
        # #342 review: clear the observer first so a call that fails BEFORE the wire (client
        # build, token budget) never records the previous call's sampling on its tap row.
        self.last_temperature = None
        self.last_seed = None
        # #437: the SDK never closes a caller-supplied http_client and _local_client builds
        # one per call, so THIS call site closes it: one pool per call, none abandoned.
        # No caching: the guarded transport pins the resolved address (#354), so a cached
        # client would keep a stale pin. The streams below are fully drained in-call.
        client = self._client()
        try:
            return self._complete(client, prompt, system, task, images, **kw)
        finally:
            client.close()

    def _complete(self, client, prompt, system, task, images=None, **kw) -> str:
        model = self._model(kw)
        mt = _resolve_max_tokens(prompt, system, model, kw)
        # #342: resolve the sampling params ONCE (judge tasks get the judge temperature),
        # and stamp the observer fields before either path's wire call so an errored
        # attempt's tap record still names what WAS sent. Stamped here (not after the
        # drain) on purpose: last_finish is stamped after the wire call, but a seed/
        # temperature decision is made before it, and both paths share the decision.
        temperature, seed = _local_sampling(task)
        self.last_temperature = temperature
        self.last_seed = seed
        seed_kw = {"seed": seed} if seed is not None else {}
        adapter = _local_prompt_adapter(model)
        if adapter is not None:
            # RAW TEXT-COMPLETIONS PATH: no messages array, no content-block/image
            # channel exists here, so images are STRUCTURALLY excluded (SPEC §2.4/§6.1).
            # is_vision_capable already reports an adapter model as non-vision, so
            # run_completion never threads images to this path; the `images` param is
            # ignored defensively regardless.
            # RAW TEXT-COMPLETIONS PATH (see _local_prompt_adapter): this family burns
            # all output into reasoning_content on /v1/chat/completions and returns an
            # empty message.content, so hand-render ChatML with an empty-think prefill
            # and hit /v1/completions instead. Same stream/stall/usage semantics as the
            # chat path below — only the endpoint, prompt shape, stop list, and drain
            # differ. `model` is the RESOLVED id (self._model), so detection is correct
            # for both the legacy single-slot config and EngineProfile-bound instances.
            stream = client.completions.create(
                model=model,
                prompt=adapter.render(prompt, system),
                max_tokens=mt,
                stop=list(adapter.stop),
                stream=True,
                stream_options={"include_usage": True},
                # #342: pin the sampling (temperature always, seed only when set) so
                # every run on this server draws from the same distribution.
                temperature=temperature,
                **seed_kw,
            )
            # #720: a translation-shaped call is watched while it streams (loop / length).
            text, usage, finish = _drain_openai_text_stream(
                stream, watch=runaway_guard.watch_for(task, prompt, system)
            )
            # L3 (gate round 1): stamp last_finish on the RAW-completions path too (not just
            # the chat path), so the qwen3.5/3.8 ChatML adapter family reports an honest
            # finish reason ("stop") on a clean success instead of NULL. Observer only.
            self.last_finish = finish
            self.last_usage = (
                {
                    "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                }
                if usage
                else None
            )
            if is_length_finish(finish):
                # #720: a truncated loop is a runaway, never escalated.
                runaway_guard.raise_if_loop(text, task, prompt, system)
                _raise_truncated(
                    "local finish_reason=length - output truncated (raw completions path)",
                    budget=mt,
                    model=model,
                    finish_reason=finish,  # M-1 (round 5): the live wire reason ("length")
                )
            if finish == "content_filter":  # #633
                return _provider_refusal_text()
            # F2/F3/F4: strip hygiene + fail LOUD on an empty completion or a
            # reasoning-only (<think>) output — never store a blank/CoT translation.
            return _finalize_local_adapter_output(prompt, text, adapter.name)
        # DEFAULT chat-completions path for every model WITHOUT a ChatML adapter (neither
        # qwen3.5 nor qwen3.8) — unchanged EXCEPT
        # for the sanctioned F2/R5 empty-output guard on the return (see below): the
        # request shape and streaming are identical to before. Stream internally (stall
        # timeout, no wall-clock kill). Local engines have no cross-request caching
        # semantics, so no cache markers/fields here.
        # Batch 14 T2: the DEFAULT chat path can carry image content blocks (SPEC §6.1);
        # `images` is None for a non-vision call so the messages array is byte-identical.
        # run-190 Item B: the no-think cure. A `reasoning_effort` kwarg (the REACTIVE
        # ThinkingBurn retry, threaded by run_completion) overrides the PROACTIVE per-family
        # default (_local_chat_reasoning_effort — gemma-4 only). When neither applies the
        # request is byte-identical to pre-190 (no extra_body).
        effort = kw.get("reasoning_effort") or _local_chat_reasoning_effort(model)
        extra_body = {"reasoning_effort": effort} if effort else None
        create_kw = {
            "model": model,
            "messages": _chat_messages(prompt, system, images),
            "max_tokens": mt,
            "stream": True,
            "stream_options": {"include_usage": True},
            # #342: pin the sampling (temperature always, seed only when set) so every
            # run on this server draws from the same distribution.
            "temperature": temperature,
        }
        if seed is not None:
            create_kw["seed"] = seed
        if extra_body is not None:
            create_kw["extra_body"] = extra_body
        stream = client.chat.completions.create(**create_kw)
        # #720: a translation-shaped call is watched while it streams (loop / length).
        text, usage, finish = _drain_openai_stream(
            stream, watch=runaway_guard.watch_for(task, prompt, system)
        )
        reasoning_tokens = _reasoning_tokens(usage)
        self.last_finish = finish
        self.last_reasoning_tokens = reasoning_tokens
        self.last_usage = (
            {
                "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                **({"reasoning_tokens": reasoning_tokens} if reasoning_tokens is not None else {}),
            }
            if usage
            else None
        )
        if is_length_finish(finish):
            # run-190 Item B: EMPTY content + finish=length is a THINKING BURN, not partial-
            # content truncation. Distinguish them so run_completion does NOT escalate
            # max_tokens (which only feeds the reasoning) and instead retries ONCE with the
            # no-think cure, then fails fast. `text` is drained above and in scope here — the
            # exact seam the pre-190 code raised _raise_truncated at WITHOUT inspecting it.
            if not (text or "").strip().strip(_ZERO_WIDTH).strip():
                _raise_thinking_burn(
                    "local finish_reason=length with empty content — the model's reasoning "
                    "consumed the output budget (thinking burn)",
                    budget=mt,
                    model=model,
                    finish_reason=finish,
                )
            # #720: a truncated loop is a runaway, never escalated.
            runaway_guard.raise_if_loop(text, task, prompt, system)
            _raise_truncated(
                "local finish_reason=length - output truncated",
                budget=mt,
                model=model,
                finish_reason=finish,  # M-1 (round 5): the live wire reason ("length")
            )
        if finish == "content_filter":  # #633: the server's own refusal signal
            return _provider_refusal_text()
        # F2: fail LOUD on an empty completion for a non-empty prompt (the silent-blank
        # trap) rather than committing "" as a SegStatus.MT success.
        return _require_local_output(prompt, text)


_OVERRIDES: dict[str, Provider] = {}


_KEYS = {
    "claude": lambda: config.ANTHROPIC_API_KEY,
    "gemini": lambda: config.GEMINI_API_KEY,
    "openai": lambda: config.OPENAI_API_KEY,
    "local": lambda: config.LOCAL_BASE_URL,
}


_REAL = {
    "claude": ClaudeProvider,
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "local": LocalProvider,
}


_ENGINE_CLASSES = {
    "openai_compatible": OpenAICompatibleProvider,
    "deepl": DeepLProvider,
    "google": GoogleTranslateProvider,
    "microsoft": MicrosoftTranslatorProvider,
    "cohere": CohereProvider,
    "lara": LaraProvider,
    "amazon": AmazonTranslateProvider,
}


# --- provider registry + resilience ------------------------------------------
# EngineProfile-backed cloud engines, keyed by EngineProfile.kind. `local` is
# NOT here — it resolves to the existing LocalProvider (base_url/key/model swapped
# in from the profile) so the migrated slot behaves exactly like the legacy one.
def register_provider(name: str, provider: Provider) -> None:
    """Register/override a provider by name (used by tests + hot-swap)."""
    _OVERRIDES[name.lower()] = provider


def _provider_for_profile(prof, transport=None) -> Provider:
    """Construct the right Provider subclass for an EngineProfile row, wired to its
    base_url / decrypted key / model / per-kind config. A `local` engine reuses the
    existing LocalProvider so the migrated slot is byte-behaviour-identical."""
    if prof.kind == "local":
        from app.services import engines
        from app.services.credential_origin import slot_key_for

        base = prof.base_url or config.LOCAL_BASE_URL
        # Gate round 1 HIGH-1: never the slot key at the profile's OWN url unless that
        # url has the slot's origin (a blank base_url IS the slot).
        key = engines.dec_key(prof.api_key_enc) or slot_key_for(prof.base_url)
        p = LocalProvider()
        # Bind this instance to the profile's server (LocalProvider reads config
        # globals by default; a profile overrides them per-call via a bound client).
        p._profile_base = base
        p._profile_key = key
        p._profile_model = prof.model or None
        return p
    cls = _ENGINE_CLASSES.get(prof.kind)
    if cls is None:
        from app.services import engines

        ref = f"engine:{getattr(prof, 'id', '?')}"
        if prof.kind in engines.JUDGE_KINDS:
            # A quality judge scores; it cannot translate or answer a prompt. The only
            # path to it is qe_jev.score_segment (a QE-gated step or the Lab scorer).
            raise JudgeEngineNotAProvider(
                f"provider '{ref}' is a {engines.KIND_LABELS.get(prof.kind, prof.kind)} "
                f"quality judge, not a translation engine -- it can only be used as a "
                f"step's QE judge engine. Re-point this project/pair/step/connector at a "
                f"translation engine."
            )
        # An unknown kind used to fall back to MockProvider SILENTLY (placeholder output
        # recorded as a real translation). Every shipped kind is handled above, so a row
        # reaching here is corrupt or from a newer build: fail loud instead.
        raise DeadEngineError(
            f"provider '{ref}' has an unknown engine kind {prof.kind!r} -- refusing to "
            f"silently fall back to the offline mock."
        )
    return cls(profile=prof, transport=transport)


def get_provider(name: str | None = None) -> Provider:
    """Return a provider by name. A CONFIGURED provider that cannot be constructed
    fails LOUD; only 'mock' (explicit or the zero-config default via config) is mock.

    A name may be:
      * "mock" (explicit, or the resolved default) — the MockProvider, always;
      * a real built-in (claude/gemini/openai/local) — the real client when its key/
        base URL is set (it 'goes live' the moment a key is added, no code change
        above this layer). Keyless, it FAILS LOUD (ConfiguredProviderUnavailable)
        rather than silently degrading to the mock and faking output — UNLESS the
        offline-by-design escape hatch config.MOCK_FALLBACK_OK is set (the hermetic
        suite / an offline box), which restores the historical keyless -> mock;
      * "engine:<id>" — an EngineProfile-backed engine (P3), constructed from that
        row's base_url/key/model/config via _provider_for_profile; a dead/foreign ref
        raises DeadEngineError (the flag never rescues an engine ref).

    A test override (register_provider / _OVERRIDES) always wins, keyed by the exact
    name (including an "engine:<id>" name), so the hot-swap seam is unchanged.
    """
    name = (name or config.DEFAULT_PROVIDER or "mock").lower()
    if name in _OVERRIDES:
        return _OVERRIDES[name]
    if name.startswith("engine:"):
        return _get_engine_provider(name)
    # The explicit offline provider, and the zero-config default: always the mock.
    if name == "mock":
        return MockProvider()
    if name in _REAL:
        # A known built-in: real client when its key/base URL is set.
        if _KEYS[name]():
            return _REAL[name]()
        # Keyless built-in. A process that is OFFLINE BY DESIGN (the hermetic suite,
        # an offline demo) opts into the historical keyless -> mock fallback via
        # config.MOCK_FALLBACK_OK. On a CONFIGURED install the flag is unset, so we
        # REFUSE: a configured provider that cannot be provided must never fake output
        # with the mock (the 2026-08-07 prod incident). Read the flag LIVE so a test
        # can toggle it per-case.
        if config.MOCK_FALLBACK_OK:
            return MockProvider()
        setting = _PROVIDER_MISSING_SETTING[name]
        # CLI-native remedy (issue #55): the CLI now applies STORED Settings at bootstrap,
        # so a stored-blank OVERRIDES the env — "set LOCAL_BASE_URL" alone is dead advice
        # for a CLI operator whose stored local_base_url is blank. `local_base_url` is a
        # plain Setting, so it gets a `config set` hint; the cloud keys are ENCRYPTED
        # (Setup AI / `providers key`), never a plain `config set`, so only local adds it.
        cli_hint = {"local": " or `config set local_base_url <url>` (CLI)"}.get(name, "")
        raise ConfiguredProviderUnavailable(
            f"provider '{name}' is configured but {setting} is not set -- refusing to "
            f"translate with offline mock output on a configured install. Add the key "
            f"on the Setup AI page{cli_hint} (or set {setting} in the env for a pure-env "
            f"install), or set default_provider=mock for offline mock output."
        )
    # A NAME was given but matches nothing known. The old code silently returned the
    # mock here, masking typos/misconfig (a real footgun for local-engine names).
    # FAIL LOUD instead — distinct from the intended no-config-Mock paths above.
    raise UnknownProviderError(
        f"unknown provider '{name}' — not a built-in (claude/gemini/openai/local/"
        f"mock) or an 'engine:<id>' reference. Check the project/step/connector "
        f"provider setting for a typo; refusing to silently fall back to the mock."
    )


def _get_engine_provider(name: str) -> Provider:
    """Resolve an "engine:<id>" name to a profile-backed provider, or FAIL LOUD.

    An `engine:<id>` ref is always explicit user configuration (a project default,
    a per-pair provider, a workflow step, or a connector). If that engine no longer
    resolves to a LIVE, enabled profile — deleted, disabled, keyless, or with an
    undecryptable secret (a rotated/lost data/.enc_key) — the OLD behavior silently
    substituted MockProvider and wrote `[xx] ...` placeholder output as a "success".
    That is the repo's cardinal sin (silent degradation to mock), so we now raise
    DeadEngineError naming the dead ref; every run site surfaces it (job FAILED,
    CLI non-zero exit, SSE error event).

    This does NOT touch the keyless BUILT-IN offline path: a keyless `claude` (etc.)
    never reaches here — get_provider falls through to `return MockProvider()` for
    built-ins — so the sanctioned offline mode the hermetic suite runs on is
    unchanged. `provider_is_live` remains the single liveness authority: a cloud
    engine is live only with a key; a local engine is live with a base URL."""
    from app.services import engines

    try:
        pid = int(name.split(":", 1)[1])
    except (ValueError, IndexError):
        prof = None
    else:
        # ONE profile read for both the liveness verdict and the Provider construction
        # (GitHub #100). This used to call provider_is_live(name) — which fetches the row
        # — and then fetch it AGAIN, i.e. two DB round trips per segment on the hottest
        # path in the app. The liveness PREDICATE is unchanged; it is just applied to the
        # row we already have (`_profile_is_live` is the same three lines provider_is_live
        # runs, extracted so the two callers cannot drift).
        prof = engines.get_profile(pid)
    if not _profile_is_live(prof):
        raise DeadEngineError(
            f"provider '{name}' references an engine profile that is not live "
            f"(deleted, disabled, or keyless) — refusing to silently fall back to "
            f"the offline mock. Re-point the project/pair/step/connector at a "
            f"configured engine, or add its API key."
        )
    # Undecryptable-secret guard: provider_is_live only checks api_key_enc is
    # PRESENT, not that it still decrypts. A cloud engine whose ciphertext no longer
    # decrypts (rotated/lost data/.enc_key) would otherwise fire a real request with
    # an empty auth header (401) or, worse, resolve to nothing useful — treat a
    # present-but-undecryptable key the same as a dead ref. Local engines are exempt
    # (they can legitimately run keyless on a base URL).
    if prof is not None and prof.kind != "local" and prof.api_key_enc:  # noqa: SIM102
        if not engines.dec_key(prof.api_key_enc):
            raise DeadEngineError(
                f"provider '{name}' ({prof.kind}) has a stored API key that no "
                f"longer decrypts (rotated/lost data/.enc_key) — refusing to run "
                f"with an empty credential or fall back to the offline mock. "
                f"Re-enter the engine's API key."
            )
    return _provider_for_profile(prof)


#: Sentinel for `provider_is_live(profile=...)`: "I did not read the row, you read it".
_UNREAD_PROFILE = object()


def provider_name_is_unknown(name: str) -> bool:
    """True when `name` cannot be any provider at all: not a test override, not `mock`, not a
    built-in (claude/gemini/openai/local) and not an `engine:<id>` ref. This is the typo case
    `get_provider` raises UnknownProviderError for; a KNOWN name that merely has no key is a
    different condition (provider_is_live False), so a caller can tell "that provider does
    not exist" (a 400) from "that provider is not set up" (a 409). #580."""
    name = (name or "").strip().lower()
    if not name or name == "mock" or name in _OVERRIDES or name in _REAL:
        return False
    return not name.startswith("engine:")


def provider_is_live(name: str, *, profile=_UNREAD_PROFILE) -> bool:
    """Whether a provider ref can actually answer a call -- the SINGLE liveness authority
    (`_get_engine_provider` raises `DeadEngineError` for exactly the set this returns False for).

    `profile` (stress-0906 #97 round-2 MED-2) lets a caller that ALREADY holds the EngineProfile
    row for an `engine:<id>` ref hand it in, so the liveness read costs NO extra DB Session.
    `engines.get_profile` opens its own Session per call, which is the amplifier behind the #100
    QueuePool exhaustion, and the advanced mission view resolves the same handful of refs several
    times per render. Pass `None` to say "that id has NO row" (deleted); omit the argument to have
    this function read the row itself, exactly as before. The predicate applied to the row is
    IDENTICAL either way -- it is `_profile_is_live` in both branches (#100 extracted it so
    `_get_engine_provider`, which already holds the row, cannot drift from this one), it just
    stops re-reading."""
    name = (name or "").lower()
    if name in _OVERRIDES:
        return True
    if name.startswith("engine:"):
        try:
            pid = int(name.split(":", 1)[1])
        except (ValueError, IndexError):
            return False
        if profile is _UNREAD_PROFILE:
            from app.services import engines

            prof = engines.get_profile(pid)
        else:
            prof = profile
        # An enabled engine with a key (or a local engine with a base URL) is live.
        return _profile_is_live(prof)
    return name in _KEYS and bool(_KEYS[name]())
