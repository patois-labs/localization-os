"""providers: part 3 (layered pure move)

Documented in: docs/architecture/engines-and-providers.md
"""

import json
from urllib.parse import urlsplit

import httpx

from app import config
from app.services import providers_part5 as runaway_guard
from app.services.providers_common import (
    _LEADING_THINK_OPEN_RE,
    _SOURCE_THINK_RE,
    _ZERO_WIDTH,
    LOCAL_TEST_READ_TIMEOUT_S,
    DeadEngineError,
    GenerationRunaway,
    NmtEmptyOutput,
    NmtUnsupported,
    NoModelConfigured,
    ProviderRefused,
    _amazon_endpoint,
    _base_lang,
    _csv_ids,
    _effective_model,
    _explicit_max_tokens,
    _lara_extract,
    _lara_lang,
    _nmt_usage,
    _normalize_local_base,
    _raise_truncated,
    _sigv4_headers,
    _strip_adapter_output,
    is_length_finish,
    local_engine_client_kwargs,
)
from app.services.providers_part2 import (
    LocalEmptyOutput,
    _amazon_lang,
    _chat_messages,
    _deepl_lang,
    _google_lang,
    _lara_signed_headers,
    _local_client,
    _local_prompt_adapter,
    _ms_lang,
    _nmt_source_text,
    _ProfileProvider,
    _raise_for_nmt_status,
    _raise_thinking_burn,
    _require_local_output,
    _require_nmt_output,
)


def _finalize_local_adapter_output(prompt: str, text: str, family: str = "") -> str:
    """The raw-adapter path's output hygiene + guards in one place: strip
    (_strip_adapter_output), then F3/F4 (a surviving leading `<think>` is reasoning,
    not a translation -> raise) and F2 (empty completion -> raise). Returns the clean
    translation only when there is real, non-reasoning text.

    `family` is the adapter's honest name (e.g. "qwen3.5" / "qwen3.8"), threaded in from
    the caller so the CoT raise below names the ACTUAL family in its user-visible message
    (segment error / Test-connection toast) rather than a hardcoded lie. Both callers
    pass adapter.name; the "" default (a bare unit call) falls back to "local adapter"
    in the message so it never reads "local local adapter".

    R2 (source-aware exception, and WHY the two skips travel together): the leading-
    think handling assumes any leading `<think>...</think>` in the OUTPUT is the model's
    own chain-of-thought — safe to delete when text follows, and a raise-worthy
    CoT-only completion when it does not. That assumption BREAKS when the SOURCE itself
    contains a `<think>` block: a faithful translation may then legitimately OPEN with a
    think block that echoes the source's think content, so a leading block is real
    target text, not reasoning. When _SOURCE_THINK_RE matches the prompt we therefore
    BYPASS _strip_adapter_output ENTIRELY (never delete the echoed block) AND skip the
    surviving-leading-`<think>` raise (a source-faithful echo — even a completion that
    is nothing but the echoed think block — is a valid translation, not CoT). The two
    must be skipped together: stripping-off leaves the raise as the only gate, and the
    raise on its own would reject exactly the faithful echoes the strip-skip is meant to
    preserve. Only the F2/R5 empty guard still applies (an empty output is empty
    regardless of the source). _strip_adapter_output stays deliberately SOURCE-UNAWARE;
    the prompt is consulted only here, at the finalize seam."""
    if _SOURCE_THINK_RE.search(prompt or ""):
        return _require_local_output(prompt, text)
    stripped = _strip_adapter_output(text)
    # F3/F4: a leading <think> that survived stripping means the completion is nothing
    # but a reasoning block — unclosed CoT, or a closed block with no translation after
    # it (_strip_adapter_output leaves such a block intact by design). Never a target.
    # R3: the check is case/whitespace-tolerant (symmetric with the strip regex) so
    # `<Think>` / `<think >` variants cannot slip through and be stored as translation.
    if _LEADING_THINK_OPEN_RE.match(stripped):
        label = f"local {family} adapter" if family else "local adapter"
        raise LocalEmptyOutput(
            f"{label} returned only a reasoning block (an unclosed or "
            "content-free <think>) with no translation — refusing to store "
            "chain-of-thought as a translation."
        )
    return _require_local_output(prompt, stripped)


_LOCAL_TEST_MAX_TOKENS = 256


# Modest output budget for the one-off "Test connection" raw-completions ping. The
# legacy /v1/completions endpoint defaults max_tokens to 16 (a classic gotcha that
# would truncate even the short test phrase), so we set an explicit small budget —
# the chat path relies on the server default, but the text-completions default is too
# small to trust for this family.
def local_complete(base_url, api_key, model, prompt, system=None, *, transport=None) -> str:
    """One-off completion against an explicit local server — the Setup AI "Test
    connection" button and nothing else. FAILS FAST: an ~8s connect budget, a short
    read budget (LOCAL_TEST_READ_TIMEOUT_S), and NO SDK retries, so an unreachable or
    silent server errors in seconds (like the 8s per-row probe) instead of the old
    ~2-min spin (120s x SDK retries). Actual translation runs are unaffected — they
    go through LocalProvider.complete with the full streamed read/stall budget."""
    from app.services import ai_optout

    ai_optout.guard("an AI engine call")  # AI opt-out choke point (DPAI-08)
    from app.services.pii_boundary import RequestRedaction

    redaction = RequestRedaction((prompt, system))
    prompt, system = redaction.redact((prompt, system))
    # #342: the ping is a GENERATE call (not a judge task), so it samples at
    # local_temperature. Lazy import: providers_hub imports this module, so a
    # module-scope import would cycle.
    from app.services.providers_hub import _local_sampling

    temperature, seed = _local_sampling()
    seed_kw = {"seed": seed} if seed is not None else {}
    # #389: the Test-connection ping must guard its SUCCESS output exactly like a run, or
    # an echoing server hands the outbound key back to the caller in the HTTP 200 body (the
    # blank-field route fills in the saved slot key here). Guard the ACTUAL outbound key
    # (what `_local_client` sends) plus the global keys; a blank/placeholder key is skipped
    # so an ordinary "lm-studio" round trip is never garbled.
    from app.services.secrets_scrub import ECHO_MIN_KEY_LEN, guard_answer, known_keys

    echo_keys = known_keys() + (
        [api_key] if isinstance(api_key, str) and len(api_key) >= ECHO_MIN_KEY_LEN else []
    )
    adapter = _local_prompt_adapter(model)
    # #437: build the per-call client only after every pre-call step that can raise, so the
    # try/finally around each wire call below covers it from creation to close.
    client = _local_client(
        base_url,
        api_key,
        read_timeout=LOCAL_TEST_READ_TIMEOUT_S,
        max_retries=0,
        transport=transport,
    )
    if adapter is not None:
        # This family burns all output into reasoning_content on the chat endpoint
        # (see _local_prompt_adapter) — even the Test-connection ping would come back
        # empty. Use the raw text-completions path with the empty-think prefill so the
        # button proves a REAL round trip. Non-streamed, mirroring the chat shape here.
        # `model` is necessarily truthy here — _local_prompt_adapter returns None for a
        # falsy model, so this branch is only reached with a real id (no `or` fallback).
        try:
            resp = client.completions.create(
                model=model,
                prompt=adapter.render(prompt, system),
                max_tokens=_LOCAL_TEST_MAX_TOKENS,
                stop=list(adapter.stop),
                # #342: same pinned sampling as the translation paths.
                temperature=temperature,
                **seed_kw,
            )
        finally:
            # #437: the SDK never closes a caller-supplied http_client; this one-off
            # probe closes it whether it succeeds or errors (no caching: the guarded
            # transport pins the resolved address, #354).
            client.close()
        # R1: same finalize as the translation path — an empty or CoT-only round trip
        # must RAISE, not return "✓-able" text. admin.py's Test-connection handler wraps
        # a raise into ok:false, so a thinking-family model that comes back blank or
        # reasoning-only reads as a failed probe instead of a bogus "✓ " in Setup-AI.
        # #389: guard the restored output so an echoing server cannot return the key.
        return guard_answer(
            redaction.restore(
                _finalize_local_adapter_output(prompt, resp.choices[0].text or "", adapter.name)
            ),
            keys=echo_keys,
        )
    msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    try:
        resp = client.chat.completions.create(
            model=model or "local-model", messages=msgs, temperature=temperature, **seed_kw
        )
    finally:
        client.close()  # #437: same close-on-every-path as the completions branch above
    # R1: same empty guard on the chat probe — any future thinking family that blanks
    # message.content on the chat endpoint fails the probe loudly rather than "✓"-ing.
    # #389: guard the restored output so an echoing server cannot return the key.
    return guard_answer(
        redaction.restore(_require_local_output(prompt, resp.choices[0].message.content or "")),
        keys=echo_keys,
    )


def _tap_local_path(prov, model):
    """Which LOCAL completion path a call took, for the Prompt Tap record's `local_path`
    field (chip task_6f7784ca). PURE + race-free: a function of the resolved model id
    only, so it is safe to compute on a SHARED provider instance across job-worker threads
    (unlike a per-instance stamped attribute, which a concurrent call could misread).

    Returns None for a NON-LOCAL provider (claude/gemini/openai/mock -- `local_path` is
    then never stamped / absent, the honest convention: the field only means something for
    a local engine). For a LocalProvider it returns the raw-adapter family name
    ("qwen3.5"/"qwen3.8") when the resolved model routes through _local_prompt_adapter, or
    "chat" for the default chat-completions path (adapter is None). This answers the
    qwen3.5-vs-3.8 question the chip exists for, better than a bare raw/chat enum. Observer
    only -- never influences a call; it is a small enum, kept OFF the redaction path.

    RESOLUTION mirrors LocalProvider._model's rungs (post-gate M1): a legacy single-slot
    LocalProvider run with a BLANK spec model (real + deliberate -- resolve_default_model
    keeps `model` blank for local refs, and orchestrator/setup_author/assist drive it that
    way) resolves its real model from config.DEFAULT_MODELS['local']. Without that rung the
    record would stamp "chat" for a call that actually took the raw qwen3.5/3.8 path -- an
    affirmatively WRONG answer in the chip's own field."""
    if getattr(prov, "name", None) != "local":
        return None
    resolved = _effective_model(prov, model) or config.DEFAULT_MODELS.get("local") or ""
    adapter = _local_prompt_adapter(resolved)
    return adapter.name if adapter is not None else "chat"


class OpenAICompatibleProvider(_ProfileProvider):
    """A hosted OpenAI-Chat-Completions endpoint reached with a BYO bearer key —
    OpenRouter, DeepSeek, Mistral, Groq, Together, Fireworks, xAI, Qwen (DashScope),
    Moonshot/Kimi all speak this wire format. It reuses the SAME request shape the
    `local` kind uses (POST /v1/chat/completions with a messages array), but against
    the profile's REQUIRED base_url + a real bearer key, and via a raw httpx.Client
    (not the openai SDK) so a MockTransport makes request-shape/auth verifiable
    offline — the EngineProfile-provider pattern the NMT/Cohere kinds already use.

    prompt-based (supports_prompts True), so it serves both the per-segment translate
    path and workflow LLM steps. `model` is free-text (each host has its own model-id
    namespace, e.g. `deepseek/deepseek-chat` on OpenRouter vs `deepseek-chat` direct)."""

    name = "openai_compatible"
    CAPS = {  # noqa: RUF012
        "supports_prompts": True,
        "supports_formality": False,
        "supports_glossary": False,
        "supports_vision": True,
    }
    # run-190 M-1 (gate round 3): this kind now FORWARDS a caller-supplied `reasoning_effort`
    # onto the /chat/completions body (below), so the reactive thinking-burn cure reaches the
    # server (LM Studio and other OpenAI-shaped self-hosted reasoning servers honour it). A
    # server that rejects the non-standard value 4xx's on the CURED RETRY only, which
    # run_completion folds back to the honest burn message.
    SUPPORTS_REASONING_EFFORT = True

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw) -> str:
        base = _normalize_local_base(
            (self.profile.base_url if self.profile else "") or kw.get("base_url") or ""
        )
        key = self._profile_api_key(kw)
        model = _effective_model(self, kw.get("model"))
        # Batch 14 T2: image content blocks ride the chat messages array (SPEC §6.1) when
        # run_completion threads them to this vision-capable target; None = byte-identical.
        body = {"model": model, "messages": _chat_messages(prompt, system, images)}
        # Honor an explicit max_tokens (setup authoring's large fixed budget, BUG 5).
        # Absent, send NO max_tokens (the host's own default) exactly as before, so the
        # per-segment translate path is unchanged. Setting it here is what lets an
        # openai_compatible engine draft a workflow without truncating.
        explicit_mt = _explicit_max_tokens(kw, model)
        if explicit_mt is not None:
            body["max_tokens"] = explicit_mt
        # run-190 M-1 (gate round 3): forward the REACTIVE no-think cure when run_completion
        # supplies it on the thinking-burn retry. Sent ONLY when the caller passes it (the
        # reactive retry) -- the PROACTIVE gemma-4 cure stays local-only, so a first/normal
        # call is byte-identical (no reasoning_effort on the wire). A server that rejects the
        # non-standard value 4xx's, which run_completion catches and folds back to the honest
        # thinking-burn message (never surfacing the raw HTTP error).
        _reasoning_effort = kw.get("reasoning_effort")
        if _reasoning_effort:
            body["reasoning_effort"] = _reasoning_effort
        client_kw = local_engine_client_kwargs(
            base, timeout=config.PROVIDER_TIMEOUT_S, transport=self._transport
        )
        with httpx.Client(**client_kw) as c:
            r = c.post("/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
        _raise_for_nmt_status(r, "openai_compatible")
        data = r.json()
        choices = data.get("choices") or []
        out = ""
        finish = None
        if choices:
            out = (choices[0].get("message") or {}).get("content") or ""
            finish = choices[0].get("finish_reason")
        # LOW-4 (gate round 5): stamp the wire finish reason as the last-seen observer field
        # (mirrors LocalProvider), so a CLEAN openai_compatible success stamps "stop" on the
        # tap/StepRun instead of NULL. Observer only; the error path reads the reason off the
        # raised exception (LOW-3), never this, so a stale value can never leak onto a failure.
        self.last_finish = finish
        # No usage block -> leave last_usage None so run_completion's length-based
        # estimate fallback engages. A truthy zero-token dict here would record a
        # permanent $0 cost and silently bypass the spend cap.
        u = data.get("usage")
        self.last_usage = (
            {
                "input_tokens": int(u.get("prompt_tokens", 0) or 0),
                "output_tokens": int(u.get("completion_tokens", 0) or 0),
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }
            if u
            else None
        )
        if is_length_finish(finish):
            # run-190 M3 (gate round 1): an OpenAI-shaped local/hosted reasoning server reached
            # through an openai_compatible EngineProfile can THINKING-BURN exactly like the
            # `local` kind — finish=length with EMPTY content means the reasoning ate the
            # output budget, NOT partial-content truncation. Raise ThinkingBurn so
            # run_completion special-cases it instead of climbing the escalate-on-truncation
            # ladder (which only feeds the burn). The PROACTIVE reasoning_effort=none cure stays
            # local/gemma-4 only (L7 -- a first call to a hosted server stays byte-identical).
            # M-1 (gate round 3): the REACTIVE cure, however, IS now sent to this kind --
            # complete() forwards a caller-supplied reasoning_effort (SUPPORTS_REASONING_EFFORT
            # True), so run_completion retries ONCE with reasoning_effort='none' (LM Studio
            # live-cures the burn with it). A server that rejects the value 4xx's on that retry,
            # which run_completion folds back to this honest burn message. Partial content still
            # truncates.
            if not (out or "").strip().strip(_ZERO_WIDTH).strip():
                _raise_thinking_burn(
                    "openai_compatible finish_reason=length with empty content — the model's "
                    "reasoning consumed the output budget (thinking burn)",
                    budget=explicit_mt,
                    model=model,
                    finish_reason=finish,
                )
            # #720: a truncated LOOP is not a too-small budget: fail fast, never escalate.
            runaway_guard.raise_if_loop(out, task, prompt, system)
            # budget is the explicit max_tokens if we set one, else None (host default);
            # run_completion escalates to the ceiling either way.
            _raise_truncated(
                "openai_compatible finish_reason=length - output truncated",
                budget=explicit_mt,
                model=model,
                finish_reason=finish,  # M-1 (round 5): the live wire reason ("length")
            )
        return out


class DeepLProvider(_ProfileProvider):
    """DeepL NMT. Auth header `DeepL-Auth-Key <key>`; POST /v2/translate with a
    `text` array (one segment/call here); `target_lang` required, `source_lang`
    optional. Optional profile-config params, sent only when set (so a blank profile
    keeps DeepL's classic default behavior): `formality` (target-dependent),
    `model_type` (latency_optimized | quality_optimized | prefer_quality_optimized —
    next-gen LLM translation), `context` (untranslated steering text), and
    `custom_instructions` (up to 10 strings, ≤300 chars each — prompt-style steering).
    Response: {"translations": [{"text": ...}]}. NMT — no prompt channel."""

    name = "deepl"
    CAPS = {"supports_prompts": False, "supports_formality": True, "supports_glossary": True}  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw) -> str:
        source = _nmt_source_text(prompt, payload)
        target = (payload or {}).get("target") or ""
        base = (self.profile.base_url if self.profile else "") or "https://api.deepl.com"
        key = self._profile_api_key(kw)
        body = {"text": [source], "target_lang": _deepl_lang(target)}
        src = (payload or {}).get("source")
        if src:
            # source_lang takes a bare ZH, never the Traditional TARGET code ZH-HANT (#500).
            body["source_lang"] = "ZH" if _base_lang(src) == "zh" else _deepl_lang(src)
        cfg = (self.profile.config or {}) if self.profile else {}
        formality = cfg.get("formality")
        if formality and formality != "default":
            body["formality"] = formality
        # Next-gen / expanded DeepL params — sent ONLY when set on the profile, so the
        # effective default (classic latency_optimized NMT) is unchanged for a profile
        # that leaves them blank. The quality_optimized billing/latency surcharge is
        # [UNCERTAIN] per PROVIDER_EXPANSION §4, hence NOT defaulted on here.
        context = cfg.get("context")
        if context:
            body["context"] = context
        # custom_instructions may reach here as a canonical list (form/CLI/normalized
        # config) OR as a bare scalar string (direct create_profile/update_profile with
        # custom_instructions="..."). A string must be treated as ONE instruction, never
        # iterated per-character into up to 10 one-char instructions.
        raw = cfg.get("custom_instructions")
        instrs = [raw] if isinstance(raw, str) else list(raw or [])
        if instrs:
            # DeepL accepts up to 10 instructions, 300 chars each; re-clamp defensively.
            body["custom_instructions"] = [str(i)[:300] for i in instrs][:10]
        model_type = cfg.get("model_type")
        if model_type:
            body["model_type"] = model_type
        elif context or instrs:
            # DeepL 400s if context/custom_instructions are set on a classic (non-
            # next-gen) model. When the operator left model_type blank but DID set a
            # steering param, fall back to the graceful next-gen variant so the request
            # succeeds instead of erroring. Neither set -> classic default is untouched.
            body["model_type"] = "prefer_quality_optimized"
        # Profile steering is added inside complete(), after the shared argument mask.
        from app.services.pii_boundary import RequestRedaction

        redaction = RequestRedaction(body)
        body = redaction.redact(body)
        with httpx.Client(
            **local_engine_client_kwargs(
                base, transport=self._transport, timeout=config.PROVIDER_TIMEOUT_S
            ),
            follow_redirects=False,
        ) as c:
            r = c.post(
                "/v2/translate", headers={"Authorization": f"DeepL-Auth-Key {key}"}, json=body
            )
        _raise_for_nmt_status(r, "deepl")
        data = r.json()
        out = "".join(t.get("text", "") for t in data.get("translations", []))
        out = _require_nmt_output(source, out, "deepl")
        # NMT is priced per source CHARACTER — record chars in the usage dict so the
        # spend ledger accrues (pricing maps char-based entries; see pricing.py).
        # (_require_nmt_output raises BEFORE this line on an empty-body response, so
        # no usage/spend is ever recorded for a call that produced no real output.)
        self.last_usage = _nmt_usage(source, "deepl")
        return redaction.restore(out)


class GoogleTranslateProvider(_ProfileProvider):
    """Google Cloud Translation Basic v2 (DECISIONS item 3: Basic v2 first, Advanced
    v3 deferred). The API key rides the X-Goog-Api-Key header and q/target/source
    ride a JSON POST body, never the query string, so neither the key nor the
    customer text is part of a request URL that a log line could print (#302). POST
    /language/translate/v2; response
    {"data": {"translations": [{"translatedText": ...}]}}. NMT — no prompt channel,
    no formality control found (CAPS.supports_formality False)."""

    name = "google"
    CAPS = {"supports_prompts": False, "supports_formality": False, "supports_glossary": False}  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw) -> str:
        source = _nmt_source_text(prompt, payload)
        target = (payload or {}).get("target") or ""
        base = (
            self.profile.base_url if self.profile else ""
        ) or "https://translation.googleapis.com"
        key = self._profile_api_key(kw)
        body = {"q": source, "target": _google_lang(target), "format": "text"}
        src = (payload or {}).get("source")
        if src:
            body["source"] = _google_lang(src)
        with httpx.Client(
            **local_engine_client_kwargs(
                base, transport=self._transport, timeout=config.PROVIDER_TIMEOUT_S
            ),
            follow_redirects=False,
        ) as c:
            r = c.post("/language/translate/v2", headers={"X-Goog-Api-Key": key or ""}, json=body)
        _raise_for_nmt_status(r, "google")
        data = r.json()
        out = "".join(
            t.get("translatedText", "") for t in data.get("data", {}).get("translations", [])
        )
        out = _require_nmt_output(source, out, "google")
        self.last_usage = _nmt_usage(source, "google")
        return out


class MicrosoftTranslatorProvider(_ProfileProvider):
    """Azure Translator Text v3.0 (classic). Header `Ocp-Apim-Subscription-Key` (+
    `Ocp-Apim-Subscription-Region` from the profile config for a regional resource);
    POST /translate?api-version=3.0&to=<lang> with a `[{"Text": ...}]` array;
    response `[{"translations": [{"text": ...}]}]`. NMT — no prompt channel; real-time
    glossary is inline-markup only, documented as unsupported (CAPS.supports_glossary
    False); tone/gender lives only on the new 2026-06-06 API (not built here)."""

    name = "microsoft"
    CAPS = {"supports_prompts": False, "supports_formality": False, "supports_glossary": False}  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw) -> str:
        source = _nmt_source_text(prompt, payload)
        target = (payload or {}).get("target") or ""
        base = (
            self.profile.base_url if self.profile else ""
        ) or "https://api.cognitive.microsofttranslator.com"
        key = self._profile_api_key(kw)
        headers = {"Ocp-Apim-Subscription-Key": key}
        region = (self.profile.config or {}).get("region") if self.profile else None
        if region:
            headers["Ocp-Apim-Subscription-Region"] = region
        params = {"api-version": "3.0", "to": _ms_lang(target)}
        src = (payload or {}).get("source")
        if src:
            params["from"] = _ms_lang(src)
        with httpx.Client(
            **local_engine_client_kwargs(
                base, transport=self._transport, timeout=config.PROVIDER_TIMEOUT_S
            ),
            follow_redirects=False,
        ) as c:
            r = c.post("/translate", params=params, headers=headers, json=[{"Text": source}])
        _raise_for_nmt_status(r, "microsoft")
        data = r.json()
        out = ""
        if data and isinstance(data, list):
            out = "".join(t.get("text", "") for t in (data[0].get("translations", []) or []))
        out = _require_nmt_output(source, out, "microsoft")
        self.last_usage = _nmt_usage(source, "microsoft")
        return out


class CohereProvider(_ProfileProvider):
    """Cohere command LLM. Bearer auth; POST /v2/chat with a messages array (like
    Claude/OpenAI — prompt-based). SSE streaming with the stall watchdog + usage
    capture; a plain non-stream response is also accepted (mock transports return
    one). Response (non-stream): {"message": {"content": [{"text": ...}]},
    "usage": {"tokens": {"input_tokens", "output_tokens"}}}. supports_prompts True."""

    name = "cohere"
    CAPS = {"supports_prompts": True, "supports_formality": False, "supports_glossary": False}  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw) -> str:
        base = (self.profile.base_url if self.profile else "") or "https://api.cohere.com"
        key = self._profile_api_key(kw)
        model = _effective_model(self, kw.get("model"))
        body = {"model": model, "messages": _chat_messages(prompt, system)}
        # Honor an explicit max_tokens (setup authoring's large budget); absent, send
        # none so the translation path is unchanged (#506 item 1).
        explicit_mt = _explicit_max_tokens(kw, model)
        if explicit_mt is not None:
            body["max_tokens"] = explicit_mt
        with httpx.Client(
            **local_engine_client_kwargs(
                base, transport=self._transport, timeout=config.PROVIDER_TIMEOUT_S
            ),
            follow_redirects=False,
        ) as c:
            r = c.post("/v2/chat", headers={"Authorization": f"Bearer {key}"}, json=body)
        _raise_for_nmt_status(r, "cohere")
        data = r.json()
        content = data.get("message", {}).get("content", [])
        out = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        # No usage block -> leave last_usage None so run_completion's length-based
        # estimate fallback engages (a truthy zero-token dict would record a
        # permanent $0 cost and silently bypass the spend cap — same trap as OAI).
        toks = (data.get("usage") or {}).get("tokens")
        self.last_usage = (
            {
                "input_tokens": int(toks.get("input_tokens", 0) or 0),
                "output_tokens": int(toks.get("output_tokens", 0) or 0),
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }
            if toks
            else None
        )
        # Truncation guard (Feature 7): Cohere reports finish_reason "MAX_TOKENS" when the
        # output cap cut the answer. Usage is already recorded above (the tokens were
        # billed); partial output must never be returned as a success.
        finish = str(data.get("finish_reason") or "")
        if is_length_finish(finish):
            _raise_truncated(
                "cohere finish_reason=MAX_TOKENS - output truncated",
                budget=explicit_mt,
                model=model,
                finish_reason=finish,
            )
        # Empty-output guard: a 200 that yields no text for a real prompt is not a
        # translation (same trap as NMT / local).
        return _require_nmt_output(prompt, out, "cohere")


class LaraProvider(_ProfileProvider):
    """Lara / Translated adaptive MT (kind `lara`) — EXPERIMENTAL.

    Rides the NMT text-in/text-out char-billed path (translate a segment from
    payload["text"], usage recorded as chars) but is LLM-based/adaptive underneath, so
    it exposes richer profile-config fields, sent ONLY when set: `instructions` (free
    text — a list, or a scalar coerced to one), `style` (fluid|faithful|creative),
    `adapt_to` (comma-separated TM ids), `glossaries` (comma-separated glossary ids).

    AUTH / SIGNING — [UNCERTAIN], hence the EXPERIMENTAL flag (PROVIDER_EXPANSION §5):
    Translated's docs steer to their SDK because it HMAC-signs requests; raw-REST
    signing is not verified against official docs. We implement the documented
    ModernMT/Lara HMAC scheme (Authorization: `Lara <access_key_id>:<b64 HMAC-SHA256>`
    over a canonical METHOD\\nContent-MD5\\nContent-Type\\nDate\\npath string) so the
    REQUEST CONSTRUCTION is fully unit-tested offline, but the scheme itself NEEDS LIVE
    VERIFICATION before this kind is trusted in production. `lara-sdk` is not a
    dependency (not installable in this hermetic env); if it is added later, prefer it.

    Auth is a PAIR: access-key-id in api_key_enc, access-key secret in the config blob
    (access_key_secret_enc, Fernet-encrypted). NMT — no prompt channel."""

    name = "lara"
    # supports_prompts stays False (NMT text-in/text-out dispatch); the `instructions`
    # field is a profile-config steering channel, not a per-call prompt. supports_glossary
    # True because the profile's `glossaries` field wires the glossary module.
    CAPS = {"supports_prompts": False, "supports_formality": False, "supports_glossary": True}  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw) -> str:
        source = _nmt_source_text(prompt, payload)
        target = (payload or {}).get("target") or ""
        base = (self.profile.base_url if self.profile else "") or "https://api.laratranslate.com"
        key = self._profile_api_key(kw)
        cfg = (self.profile.config or {}) if self.profile else {}
        eng = __import__("app.services.engines", fromlist=["dec_cfg_secret"])
        secret = kw.get("api_secret") or eng.dec_cfg_secret(cfg)
        body_obj = {"q": source, "target": _lara_lang(target)}
        src = (payload or {}).get("source")
        if src:
            body_obj["source"] = _lara_lang(src)
        # Adaptive fields — sent ONLY when set on the profile (a blank profile behaves
        # like a plain NMT call). instructions may be a list or a scalar string.
        raw_instr = cfg.get("instructions")
        if raw_instr:
            instrs = [raw_instr] if isinstance(raw_instr, str) else list(raw_instr)
            instrs = [str(i) for i in instrs if str(i).strip()]
            if instrs:
                body_obj["instructions"] = instrs
        style = cfg.get("style")
        if style:
            body_obj["style"] = style
        adapt_to = _csv_ids(cfg.get("adapt_to"))
        if adapt_to:
            body_obj["adapt_to"] = adapt_to
        glossaries = _csv_ids(cfg.get("glossaries"))
        if glossaries:
            body_obj["glossaries"] = glossaries
        # Profile instructions do not arrive through the shared content arguments.
        from app.services.pii_boundary import RequestRedaction

        redaction = RequestRedaction(body_obj)
        body_obj = redaction.redact(body_obj)
        body = json.dumps(body_obj).encode()
        headers = _lara_signed_headers("POST", "/translate", key, secret, body=body)
        with httpx.Client(
            **local_engine_client_kwargs(
                base, transport=self._transport, timeout=config.PROVIDER_TIMEOUT_S
            ),
            follow_redirects=False,
        ) as c:
            r = c.post("/translate", headers=headers, content=body)
        _raise_for_nmt_status(r, "lara")
        data = r.json()
        out = _lara_extract(data)
        out = _require_nmt_output(source, out, "lara")
        self.last_usage = _nmt_usage(source, "lara")
        return redaction.restore(out)


class AmazonTranslateProvider(_ProfileProvider):
    """Amazon Translate (kind `amazon`) — NMT, SigV4-authed.

    text-in/text-out `TranslateText` on the JSON-RPC endpoint
    (X-Amz-Target: AWSShineFrontendService_20170701.TranslateText,
    translate.{region}.amazonaws.com) — char-billed, rides the NMT path. Auth is AWS
    SigV4 (access key in api_key_enc, secret-access-key in the config blob
    access_key_secret_enc, region a REQUIRED config field). boto3 is NOT a dependency
    (kept out per the plan); SigV4 for this single action is hand-rolled against the
    stdlib (well-documented, stable algorithm; tested against the AWS SigV4 test-suite
    vectors). Custom Terminology is DEFERRED (not wired). NMT — no prompt channel."""

    name = "amazon"
    CAPS = {"supports_prompts": False, "supports_formality": True, "supports_glossary": False}  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw) -> str:
        source = _nmt_source_text(prompt, payload)
        target = (payload or {}).get("target") or ""
        cfg = (self.profile.config or {}) if self.profile else {}
        region = (kw.get("region") or cfg.get("region") or "").strip()
        if not region:
            # A missing region is a bad-request shape (terminal) — surface a 400-style
            # error so run_completion routes to failover rather than retry-hammering.
            err = RuntimeError("amazon: region is required (config: region=us-east-1)")
            err.status_code = 400
            raise err
        key = self._profile_api_key(kw)
        eng = __import__("app.services.engines", fromlist=["dec_cfg_secret"])
        secret = kw.get("api_secret") or eng.dec_cfg_secret(cfg)
        base = (self.profile.base_url if self.profile else "") or _amazon_endpoint(region)
        body_obj = {
            "Text": source,
            "SourceLanguageCode": _amazon_lang(src)
            if (src := (payload or {}).get("source"))
            else "auto",
            "TargetLanguageCode": _amazon_lang(target),
        }
        formality = cfg.get("formality")
        if formality:
            body_obj["Settings"] = {"Formality": formality}
        body = json.dumps(body_obj).encode()
        headers = _sigv4_headers(
            access_key=key,
            secret_key=secret,
            region=region,
            service="translate",
            target="AWSShineFrontendService_20170701.TranslateText",
            host=urlsplit(base).netloc,
            body=body,
        )
        with httpx.Client(
            **local_engine_client_kwargs(
                base, transport=self._transport, timeout=config.PROVIDER_TIMEOUT_S
            ),
            follow_redirects=False,
        ) as c:
            r = c.post("/", headers=headers, content=body)
        _raise_for_nmt_status(r, "amazon")
        data = r.json()
        out = data.get("TranslatedText", "")
        out = _require_nmt_output(source, out, "amazon")
        self.last_usage = _nmt_usage(source, "amazon")
        return out


def _vision_capable_resolved(name: str, *, model=None, profile=None) -> bool:
    """Capability-only predicate for a recognized ref/profile; it performs no admission read."""
    if name in ("claude", "openai", "gemini"):
        return True
    if name == "mock":
        return False
    if name == "local":
        m = model or config.DEFAULT_MODELS.get("local") or "local-model"
        return _local_prompt_adapter(m) is None
    if name.startswith("engine:") and profile is not None:
        kind = (getattr(profile, "kind", "") or "").lower()
        if kind in ("local", "openai_compatible"):
            # A blank model resolves to the DEFAULT model the call will really use
            # (the same rule as _effective_model); judging "" capable skipped the
            # default's ChatML adapter, which ignores images (#506 item 5).
            m = model or getattr(profile, "model", "") or ""
            if not m:
                m = (
                    (config.DEFAULT_MODELS.get("local") or "local-model")
                    if kind == "local"
                    else "gpt-3.5-turbo"
                )
            return _local_prompt_adapter(m) is None
    return False


def _is_terminal_error(e: Exception) -> bool:
    """Errors that retrying the SAME provider can never fix (bad key, bad model,
    malformed request) — skip straight to the fallback. Rate limits, timeouts,
    5xx and network errors stay retryable."""
    # A prompt-only task routed to an NMT engine (NmtUnsupported) is a shape error
    # retrying can never fix — treat it as terminal so run_completion goes straight
    # to the failover chain (e.g. NMT primary -> LLM fallback) instead of burning
    # the full retry/backoff budget on a request that will always fail. (Matches the
    # NmtUnsupported docstring's "TERMINAL error" contract, which the name-substring
    # check below would otherwise miss.) NmtEmptyOutput (a 200 body that maps to no
    # translated text for a non-empty source — the empty-translation billing trap;
    # see its docstring) is the same shape of problem and gets the same treatment.
    # LocalEmptyOutput (LocalProvider returned an empty completion / reasoning-only
    # output for a non-empty prompt — the local twin of NmtEmptyOutput) is the same
    # shape of problem: retrying the SAME local model reproduces the same empty body,
    # so it is terminal and run_completion goes straight to failover.
    from app.services.secrets_scrub import KeyEchoError

    # #359: a hostile endpoint that echoes our key will do it again on a retry.
    if isinstance(
        e,
        (
            NmtUnsupported,
            NmtEmptyOutput,
            LocalEmptyOutput,
            KeyEchoError,
            ProviderRefused,
            # #720: the same model + prompt + pinned sampling loops again; failover applies.
            GenerationRunaway,
        ),
    ):
        return True
    # An empty resolved model is a config error retrying the SAME provider can never
    # fix — treat it like the raw 400 it replaces (terminal), so run_completion goes
    # straight to any fallback instead of burning the retry budget.
    if isinstance(e, NoModelConfigured):
        return True
    # A dead engine:<id> ref is explicit misconfiguration retrying can never fix,
    # AND must not be quietly failed over to a keyless built-in (that would restore
    # the silent-mock degradation). It normally propagates OUT of run_completion
    # (get_provider raises before the retry loop), so this is defensive: any path
    # that does route it here treats it as terminal, not retryable.
    if isinstance(e, DeadEngineError):
        return True
    status = getattr(e, "status_code", None)
    if status is None:
        # Google GenAI APIError exposes the HTTP status as code.
        status = getattr(e, "code", None)
    if status in (400, 401, 403, 404, 422):
        return True
    name = type(e).__name__.lower()
    return any(
        k in name
        for k in (
            "authentication",
            "permissiondenied",
            "notfound",
            "badrequest",
            "invalidargument",
            "unprocessable",
        )
    )
