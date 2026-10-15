"""providers: part 2 (layered pure move)

Documented in: docs/architecture/engines-and-providers.md
"""

import re

import httpx

from app import config
from app.services.providers_common import (
    _CHATML_STOP,
    _CLOUD_LIST_TIMEOUT_S,
    _ZERO_WIDTH,
    LOCAL_CONNECT_TIMEOUT_S,
    DeadEngineError,
    NmtEmptyOutput,
    NmtUnsupported,
    NoModelConfigured,
    Provider,
    ProviderCallError,
    ThinkingBurn,
    _anthropic_list_models,
    _b64_png,
    _base_lang,
    _explicit_max_tokens,
    _http_date,
    _LocalPromptAdapter,
    _normalize_local_base,
    _parse_retry_after,
    _sanitize_chatml,
    _sized_max_tokens,
    _tokens,
    drop_credential_ids,
    local_engine_client_kwargs,
)


def _require_model(provider_name: str, model: str | None) -> str:
    """Pre-flight: a chat provider must resolve to a non-empty model before we hit
    the API. Return the stripped model, or raise NoModelConfigured with the exact
    env var to set."""
    m = (model or "").strip()
    if m:
        return m
    env = {
        "claude": "DEFAULT_CLAUDE_MODEL",
        "gemini": "DEFAULT_GEMINI_MODEL",
        "openai": "DEFAULT_OPENAI_MODEL",
    }.get(provider_name)
    where = (
        f"set a model on the step/project, or {env}" if env else "set a model on the step/project"
    )
    raise NoModelConfigured(
        f"no model configured for provider '{provider_name}' ({where}) — "
        f"refusing to call the API with an empty model"
    )


def _resolve_max_tokens(prompt: str, system: str | None, model: str | None, kw: dict) -> int:
    """The output-token budget for a prompt-based complete(): an explicit `max_tokens`
    kwarg (clamped to the model ceiling) OVERRIDES the input-sized heuristic; absent,
    the input-sized default (_sized_max_tokens) applies -- so the translation path,
    which never passes max_tokens, is unchanged. See _explicit_max_tokens (BUG 5)."""
    explicit = _explicit_max_tokens(kw, model)
    return explicit if explicit is not None else _sized_max_tokens(prompt, system, model)


def _raise_thinking_burn(msg: str, *, budget: int | None, model: str | None, finish_reason: str):
    """Raise ThinkingBurn (run-190 Item B): finish_reason=='length' with EMPTY content on
    the local chat path — the model burned its whole budget on reasoning. Carries the same
    budget/ceiling metadata as ProviderTruncated (so a reader still sees the cap), plus
    empty_output=True and the wire finish_reason. run_completion special-cases it: NO
    max_tokens escalation, ONE retry with reasoning_effort='none', then fail fast."""
    e = ThinkingBurn(msg, empty_output=True, finish_reason=finish_reason)
    e.budget = budget
    e.ceiling = config.model_output_ceiling(model)
    raise e


class UnknownProviderError(DeadEngineError):
    """A stored provider NAME does not match any known provider — not a built-in
    (claude/gemini/openai/local/mock), not an `engine:<id>` ref, not a test override.

    The old behavior silently returned MockProvider for ANY unrecognized name, so a
    typo (`cluade`, `local-lm`, a stale engine kind) produced deterministic `[xx] ...`
    placeholder output that LOOKED like a successful translation — the same
    silent-degradation-to-mock footgun DeadEngineError closed for `engine:<id>` refs.
    This closes it for built-in names too: get_provider now RAISES here.

    Subclasses DeadEngineError deliberately: every run site already catches
    DeadEngineError (job -> FAILED, CLI -> non-zero exit, SSE -> error event) and
    _is_terminal_error already treats it as terminal, so an unknown name surfaces
    cleanly everywhere with no new catch clauses.

    NOT triggered by the sanctioned offline default: when NO provider is configured
    (name resolves to a keyless built-in, or to the explicit `mock`), get_provider
    still returns MockProvider unchanged — that path never reaches this error."""


class ConfiguredProviderUnavailable(DeadEngineError):
    """A CONFIGURED built-in provider (claude/gemini/openai/local) cannot be
    constructed because its credential is absent — the project/step/pair/global
    default names e.g. 'claude' but ANTHROPIC_API_KEY is unset (a cloud key), or
    'local' but LOCAL_BASE_URL is blank.

    The OLD behavior silently substituted MockProvider and stored deterministic
    `[xx] <source>` placeholder output as if it were a real translation — the exact
    2026-08-07 prod incident (a claude-pinned install with no key filled two days of
    runs with mock garbage under green checkmarks). get_provider now RAISES this,
    naming the provider and the missing setting, so the run FAILS LOUD on every
    surface instead of faking output.

    Subclasses DeadEngineError deliberately: every MT run site already treats that as
    TERMINAL (job -> FAILED, CLI -> non-zero exit, SSE -> error event) and never
    fails it over to another keyless built-in (which would restore the silent-mock
    degradation), so this loud refusal surfaces cleanly with no new catch clauses and
    _is_terminal_error already classifies it terminal.

    NOT raised for the sanctioned OFFLINE posture: an unset/blank default and the
    explicit 'mock' provider both resolve to MockProvider unchanged (the zero-config
    default is now 'mock'), and a process that is offline BY DESIGN may set
    config.MOCK_FALLBACK_OK to keep the keyless-built-in -> mock behavior (the
    hermetic suite, an offline demo). See config.MOCK_FALLBACK_OK / DEFAULT_PROVIDER."""


def _require_nmt_output(source: str, out: str, provider: str) -> str:
    """Guard shared by every NMT provider's response-extraction step: a 200 response
    that maps to "" for a non-empty source is NOT a legitimate empty translation (no
    real NMT/adaptive-MT API returns a genuinely empty string for real input text) —
    it means extraction missed an unrecognized/malformed body. Raise NmtEmptyOutput
    (terminal, no usage recorded by the caller) instead of returning "" and letting
    it silently ride through as a phantom billed translation. A blank SOURCE (rare,
    e.g. an all-whitespace segment) legitimately maps to a blank target, so this only
    fires when the source has real content."""
    if (source or "").strip() and not (out or "").strip():
        raise NmtEmptyOutput(
            f"{provider}: HTTP 200 but no translated text could be extracted from "
            f"the response body (non-empty source) — refusing to treat this as a "
            f"successful empty translation."
        )
    return out


def _mock_choose_sense(payload: dict) -> int:
    segment = payload.get("segment", "")
    senses = payload.get("senses", [])
    seg_tokens = _tokens(segment)
    best_i, best_score = 0, -1
    for i, s in enumerate(senses):
        cue_tokens = _tokens(s.get("cue", ""))
        score = len(seg_tokens & cue_tokens)
        if score > best_score:
            best_score, best_i = score, i
    return best_i


def claude_image_content(prompt, images):
    """Anthropic user-message content: image block(s) THEN the text block (the
    _guarded_vision_call / ocr.py order)."""
    blocks = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _b64_png(b)},
        }
        for b in images
    ]
    blocks.append({"type": "text", "text": prompt})
    return blocks


def openai_image_content(prompt, images):
    """OpenAI-style chat user content: the text block THEN image_url data-URI block(s)
    (the _guarded_vision_call / ocr.py order). Shared by openai/local/openai_compatible."""
    content = [{"type": "text", "text": prompt}]
    for b in images:
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64_png(b)}"}}
        )
    return content


def _chat_messages(prompt, system, images=None):
    """The OpenAI-style chat `messages` array shared by every chat provider that
    speaks that wire format (OpenAI / local / openai_compatible / Cohere): an optional
    leading system message, then the user prompt.

    Batch 14 T2 (SPEC §6.1): when `images` is a non-empty list of PNG bytes, the user
    message content becomes a content-block LIST (`[{"type":"text",...}] +
    [{"type":"image_url",...}...]`); absent, the user content is the flat prompt STRING,
    byte-identical to before the param existed (LAW-IMG-7). run_completion only threads
    images to a vision-capable CHAT target, so the local ChatML-adapter path (which uses
    client.completions.create, not this messages array) and every NMT/cohere engine never
    reach this with images."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    user_content = openai_image_content(prompt, images) if images else prompt
    msgs.append({"role": "user", "content": user_content})
    return msgs


def _local_client(
    base_url=None,
    api_key=None,
    *,
    read_timeout=None,
    max_retries=None,
    transport=None,
    timeout=None,
):
    """OpenAI-SDK client for a local/self-hosted OpenAI-compatible server.

    The timeout is SPLIT: connect is always capped at LOCAL_CONNECT_TIMEOUT_S (so an
    unreachable host fails fast, not after ~2 min), while read/write/pool default to
    PROVIDER_TIMEOUT_S so a healthy long generation on the translation path is not
    truncated. `read_timeout` overrides the read budget (the Test button passes a
    short one); `max_retries` limits SDK-level retries (the Test button passes 0 so a
    dead server errors once, not 3x); `transport` injects an httpx transport for
    hermetic tests (mirrors engines._client / OpenAICompatibleProvider).
    `timeout` preserves an existing caller's full timeout policy (vision uses the
    SDK default); when omitted, the split translation timeout above is unchanged."""
    from openai import OpenAI

    from app.services.credential_origin import slot_key_for

    base = _normalize_local_base(base_url if base_url is not None else config.LOCAL_BASE_URL)
    read = config.PROVIDER_TIMEOUT_S if read_timeout is None else read_timeout
    if timeout is None:
        timeout = httpx.Timeout(read, connect=LOCAL_CONNECT_TIMEOUT_S)
    kwargs = {
        "base_url": base,
        # Gate round 1 HIGH-1: the slot key only ever goes to the slot's own origin.
        "api_key": api_key or slot_key_for(base),
        "timeout": timeout,
    }
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    client_kw = local_engine_client_kwargs(base, timeout=timeout, transport=transport)
    # #354: always hand the SDK the door's client (the pinned guarded transport, an injected
    # test transport, or the tailnet SOCKS route), never let it build its own unpinned one.
    kwargs["http_client"] = httpx.Client(**client_kw)
    return OpenAI(**kwargs)


def local_list_models_result(
    base_url=None, api_key=None, *, transport=None, quick: bool = False
) -> dict:
    """Discovery result with an explicit policy-refusal signal; no exception escapes.

    #564 m11: ``quick`` is the Setup AI page's automatic on-load probe. A configured but
    unreachable server used to hold the card at "checking" for the full 6 s budget (about
    8 s end to end); the quick probe gives up after 2 s of connecting and says the server
    did not answer (``unreachable``) instead of the misleading "no models found"."""
    from app.services.egress import EgressBlocked

    base = _normalize_local_base(base_url if base_url is not None else config.LOCAL_BASE_URL)
    if not base:
        return {"models": [], "blocked": False}
    from app.services.credential_origin import slot_key_for

    # Gate round 1 HIGH-1: the slot key only ever goes to the slot's own origin.
    sent_key = api_key or slot_key_for(base)
    headers = {"Authorization": f"Bearer {sent_key}"}
    try:
        probe_timeout = httpx.Timeout(4.0, connect=2.0) if quick else 6
        client_kw = local_engine_client_kwargs(base, timeout=probe_timeout, transport=transport)
        # #354: the door's client always (pinned guarded transport / test double / SOCKS).
        with httpx.Client(**client_kw) as c:
            r = c.get(f"{base}/models", headers=headers)
        if r.status_code in (401, 403):
            # #346: say the server refused the key, without echoing anything it sent.
            return {"models": [], "blocked": False, "auth_failed": True}
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        # #390 (ai-02): a server that echoes the bearer it received back as a model id
        # must not hand the saved key to a discovery caller that may not read it.
        return {"models": drop_credential_ids(ids, sent_key), "blocked": False}
    except EgressBlocked as exc:
        return {"models": [], "blocked": True, "error": str(exc)}
    except httpx.TransportError:
        return {"models": [], "blocked": False, "unreachable": True}
    except Exception:
        return {"models": [], "blocked": False}


def _openai_list_models(api_key: str, transport=None) -> list[str]:
    """OpenAI GET /v1/models via the SDK (client.models.list()). Returns ALL model
    ids the key can access, INCLUDING fine-tunes (ft:.../custom) — chat-capability is
    not cleanly derivable from this endpoint, so we return everything and let the user
    pick rather than silently drop a model they need.

    SECURITY: host is not caller-configurable — always the SDK's official
    api.openai.com. The configured OPENAI key never goes to an operator-supplied host.
    #390 (ai-02): an id echoing the key just sent is dropped (drop_credential_ids)."""
    from openai import OpenAI

    kwargs = {"api_key": api_key, "timeout": _CLOUD_LIST_TIMEOUT_S}
    if transport is not None:
        kwargs["http_client"] = httpx.Client(transport=transport, timeout=_CLOUD_LIST_TIMEOUT_S)
    client = OpenAI(**kwargs)
    # First page's .data (OpenAI's /models is not cursor-paginated — one page holds
    # every model + fine-tune the key can access); avoid iterating the pager for the
    # same anti-infinite-loop reason as the Anthropic helper.
    page = client.models.list()
    ids = [getattr(m, "id", None) for m in (page.data or [])]
    return drop_credential_ids(list(dict.fromkeys(i for i in ids if i)), api_key)


def _gemini_list_models(api_key: str, transport=None) -> list[str]:
    """Gemini/Google GET /v1beta/models (raw httpx — easy to mock; the SDK would work
    too). Strips the 'models/' prefix and keeps only generateContent-capable models
    when the response reports supportedGenerationMethods.

    SECURITY: the host is hard-coded to the official generativelanguage.googleapis.com
    — NOT caller-configurable — so the configured GEMINI key can never be sent to an
    operator-supplied host. The key rides the x-goog-api-key header, never the query
    string, so no request URL carries it into a log line (#302). #390 (ai-02): a name
    echoing the key just sent is dropped (drop_credential_ids)."""
    base = "https://generativelanguage.googleapis.com"
    with httpx.Client(transport=transport, timeout=_CLOUD_LIST_TIMEOUT_S) as c:
        r = c.get(
            f"{base}/v1beta/models",
            params={"pageSize": 1000},
            headers={"x-goog-api-key": api_key or ""},
        )
    r.raise_for_status()
    out: list[str] = []
    for m in r.json().get("models", []):
        methods = m.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        name = m.get("name") or ""
        if name.startswith("models/"):
            name = name[len("models/") :]
        if name:
            out.append(name)
    return drop_credential_ids(list(dict.fromkeys(out)), api_key)


def cloud_list_models(
    provider: str, api_key: str | None = None, base_url: str | None = None, *, transport=None
) -> list[str]:
    """List the models a cloud provider's key can access (claude / gemini / openai).

    Dispatches by provider name; uses the configured key from config.* when api_key is
    None (never reads .env directly). Returns [] on ANY error — no key, auth failure,
    network error, missing SDK — and NEVER raises, mirroring local_list_models so the
    Setup AI route can render a clean empty dropdown. `transport` is for hermetic tests.

    SECURITY — `base_url` is accepted for signature symmetry with the local route but
    is DELIBERATELY IGNORED for these cloud providers: the configured cloud key is
    real-money credential and must ONLY ever be sent to each provider's official host.
    Honoring a caller-supplied base_url would let a trusted can_tune_ai user (or a
    request forged through one) exfiltrate the configured ANTHROPIC/OPENAI/GEMINI key
    to an arbitrary host (SSRF-style key theft). A custom host is legitimate ONLY for
    the LOCAL engine (worthless 'lm-studio' key), which uses local_list_models instead."""
    p = (provider or "").strip().lower()
    key_src = {
        "claude": lambda: config.ANTHROPIC_API_KEY,
        "openai": lambda: config.OPENAI_API_KEY,
        "gemini": lambda: config.GEMINI_API_KEY,
    }
    if p not in key_src:
        return []
    key = api_key if api_key else key_src[p]()
    if not key:
        return []
    # base_url intentionally NOT forwarded — the official provider host is always used.
    try:
        if p == "claude":
            return _anthropic_list_models(key, transport)
        if p == "openai":
            return _openai_list_models(key, transport)
        if p == "gemini":
            return _gemini_list_models(key, transport)
    except Exception:
        return []
    return []


def _render_qwen35_chatml(prompt: str, system: str | None) -> str:
    """Render a ChatML text-completions prompt for the qwen3.5 family: an optional
    system turn, the user turn, then the assistant turn PREFILLED with an empty think
    block so the model resumes AFTER </think> and never spends the completion on
    reasoning. When there is no system message the system block is omitted entirely
    (a `<|im_start|>system\\n<|im_end|>` empty turn confuses the template).

    F1: both `prompt` and `system` are UNTRUSTED and are sanitized (_sanitize_chatml)
    before interpolation so a source segment / user system prompt carrying a literal
    ChatML sentinel can neither forge a role turn nor collide with the string stop
    list (silent mid-translation truncation). See _sanitize_chatml."""
    prompt = _sanitize_chatml(prompt)
    system = _sanitize_chatml(system) if system else system
    parts = []
    if system:
        parts.append(f"<|im_start|>system\n{system}<|im_end|>\n")
    parts.append(f"<|im_start|>user\n{prompt}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    return "".join(parts)


_QWEN35_ADAPTER = _LocalPromptAdapter(
    name="qwen3.5", render=_render_qwen35_chatml, stop=tuple(_CHATML_STOP)
)


_QWEN38_ADAPTER = _LocalPromptAdapter(
    name="qwen3.8", render=_render_qwen35_chatml, stop=tuple(_CHATML_STOP)
)


_QWEN35_RE = re.compile(r"qwen[\s._-]*3[\s._-]*5")


_QWEN38_RE = re.compile(r"qwen[\s._-]*3[\s._-]*8(?![\s._-]*b)")


# qwen3.8 shares qwen3.5's renderer + stop list VERBATIM (the ChatML empty-think prefill
# was replayed on qwen3.8-27b unchanged — see the SECOND FAMILY note in the block above);
# only the diagnostic `name` differs so logs/errors name the actual family rather than
# reading "qwen3.5" for a qwen3.8 model.
# F5: match qwen3.5 across the separator spellings seen in the wild — a dot, dash,
# underscore, space, or nothing between the 3 and 5 (`qwen3.5-9b`, `qwen-3.5-9b`,
# `qwen3_5-9b`, `qwen35-9b`). A FALSE NEGATIVE reproduces the original silent-empty
# bug (the model routes to the chat path, burns the answer into reasoning_content, and
# returns ""); an over-match only sends a non-qwen model down the raw path, which is
# benign. So the detector is deliberately generous.
# qwen3.8: the SAME separator tolerance as F5 above (`qwen3.8-27b`, `qwen-3.8`,
# `qwen3_8`, `qwen 3.8`, `qwen38`) — but the SAFE/DANGEROUS asymmetry is INVERTED vs
# qwen3.5. A qwen3.8 FALSE NEGATIVE fails LOUDLY, not silently: an unadapted qwen3.8
# hits the chat path where its UNBOUNDED reasoning stream (not capped by max_tokens)
# blows the context (mid-stream "Context size has been exceeded" at 16k) or burns the
# whole budget (finish=length at 50k -> _raise_truncated) — never a stored blank. The
# dangerous direction here is the FALSE POSITIVE: the real Qwen3-8B family (`qwen3-8b`,
# `Qwen3-8B-Q4_K_M`, `qwen3_8b`, `qwen3 8b`, `qwen3-8b-loc-es-v1`) is a DIFFERENT model
# family whose chat-path behavior has NOT been characterized here — there is no evidence
# it needs this treatment, and rerouting it through the raw empty-think prefill would
# silently CHANGE its behavior with NO error. So the detector refuses a trailing `b`
# right after the 8 via a negative lookahead. DECIDED: Qwen3-8B is
# deliberately EXCLUDED.
#
# What the lookahead sacrifices (accepted, because the mitigation is loud): ANY 3.8 id
# whose first non-separator suffix char is `b` — not just the bare `qwen3.8b`, but also
# `qwen3.8-base`, `qwen3.8-bf16`, `qwen3.8-bnb-4bit`, `qwen38-base` (all wrongly
# excluded). Size-suffixed canonical forms are SAFE (`qwen3.8-27b`, `qwen3.8-27b-bf16` —
# the size digits sit between the separator and the `b`, so the lookahead does not fire).
# A wrongly-excluded 3.8 id fails LOUDLY on the chat path (truncation/context error),
# never silently blank, so this ceiling is tolerable rather than a silent trap.
#
# ACCEPTED OVER-MATCHES (documented, NOT excluded): `qwen3-8` and `qwen3-80b` still
# MATCH (the `b` in `qwen3-80b` follows the `0`, not the `8`). Both are implausible as
# real local model ids, and if one ever appears its worst case is the benign loud-
# failure ceiling above — NOT a silent miss. Left as-is deliberately: a future regex
# change that flips them must be a conscious act, pinned by a test, not an accident.
# Still does NOT match plain `qwen3`, `qwen3.6`, or `qwen4`.
def _local_prompt_adapter(model: str | None) -> "_LocalPromptAdapter | None":
    """The prompt adapter for a resolved LOCAL model id, or None for the default
    chat-completions path. Detection is case-insensitive and separator-tolerant —
    qwen3.5 via _QWEN35_RE (`qwen3.5-9b`, `qwen3.5-9b-loc-esfr-v1`,
    `qwen3.5-9b-loc-es-v2`, `qwen3.5-9b-loc-fr-v1`, near-misses `qwen-3.5-9b` /
    `qwen3_5-9b` / `qwen35-9b`) and qwen3.8 via _QWEN38_RE (`qwen3.8-27b`, `qwen-3.8`,
    `qwen3_8`, `qwen 3.8`, `qwen38`); both families share one renderer + stop list and
    return their OWN honestly-named adapter. Order matters: qwen3.5 is checked FIRST, so
    an id that somehow names BOTH versions (two `qwen` tokens, e.g.
    `qwen3.5-...-qwen3.8-...`) resolves to the qwen3.5 adapter; realistic single-family
    ids match only one detector. Returning None here routes a model to the UNCHANGED
    chat-completions path — identical to before EXCEPT for the one sanctioned behavior
    change: that path now runs the F2/R5 empty-output guard (_require_local_output), so
    a 200-with-blank-content for a non-empty prompt raises LocalEmptyOutput instead of
    silently committing "" as a translation. Applies to every model that is neither
    qwen3.5 nor qwen3.8 (any future thinking family that blanks message.content on the
    chat endpoint now fails loud rather than storing a phantom empty target)."""
    if not model:
        return None
    m = model.lower()
    if _QWEN35_RE.search(m):
        return _QWEN35_ADAPTER
    if _QWEN38_RE.search(m):
        return _QWEN38_ADAPTER
    return None


class LocalEmptyOutput(ProviderCallError):
    """A LOCAL provider call (LocalProvider.complete, either the raw /v1/completions
    ChatML-adapter path (qwen3.5 / qwen3.8) OR the default /v1/chat/completions path)
    returned HTTP 200 with NO usable translation for a NON-EMPTY prompt — i.e. one of:
      * an empty / whitespace-only completion (the exact silent-blank trap: without
        this guard translate._apply_result commits "" as SegStatus.MT at
        mt_confidence=0.9 — a plausible 'success' with no translation). This is the
        LOCAL twin of NmtEmptyOutput, and the durable backstop that makes a near-miss
        model id (F5) or a future thinking family that BLANKS message.content on the
        chat path (as qwen3.5 does) fail LOUD instead of silently blank. NOTE it is NOT
        the backstop for qwen3.8: that family returns NON-empty content on the chat
        endpoint, so an unadapted qwen3.8 is caught by TRUNCATION instead (its unbounded
        reasoning triggers context-exceeded / finish=length -> _raise_truncated), never
        by this empty guard.
      * a completion that is nothing but a reasoning block — an UNCLOSED `<think>...`
        (chain-of-thought that never produced a translation), or a `<think>...</think>`
        with no real text after it (F3/F4). Returning that would store chain-of-thought
        as the translation.

    Mirrors NmtEmptyOutput's contract: it fires ONLY for a non-blank prompt (a blank
    prompt legitimately maps to a blank output — no false positive), the provider
    raises BEFORE any success is recorded, and it is TERMINAL — checked by name in
    _is_terminal_error alongside NmtEmptyOutput, so run_completion goes straight to the
    failover chain (retrying the SAME misconfigured local model produces the same empty
    body). A ProviderCallError subclass so translate.py surfaces it as a segment error
    (run_completion returns {text:"", error:<this>}; _apply_result's `if error and not
    text` counts it an error and leaves the segment untranslated) — never a stored
    empty/CoT translation."""


def _require_local_output(prompt: str, out: str) -> str:
    """F2 empty-output guard for the LOCAL path (the twin of _require_nmt_output): a
    200 that maps to "" for a non-empty prompt is never a legitimate empty translation
    on this path — raise LocalEmptyOutput instead of returning "" and letting it ride
    through as a phantom 'translated' segment. Conditioned on a non-blank PROMPT so a
    genuinely-empty input (rare) does not false-positive.

    R5: output that is only whitespace and/or zero-width characters
    (U+200B/200C/200D/FEFF) is treated as empty too — a model can emit invisibles that
    would otherwise ride through as a phantom non-blank 'translation'."""
    if (prompt or "").strip() and not (out or "").strip().strip(_ZERO_WIDTH).strip():
        raise LocalEmptyOutput(
            "local provider returned an empty completion for a non-empty prompt — "
            "refusing to store a blank translation as a successful result."
        )
    return out


def _nmt_source_text(prompt, payload) -> str:
    """The source text an NMT engine translates: payload['text'] is the segment
    source (the deterministic channel translation_prompt fills), which is what a
    text-in/text-out engine needs. A prompt-only call (no translate payload) has no
    single source string, so we reject it — NMT engines can't follow instructions."""
    text = (payload or {}).get("text")
    if not text:
        raise NmtUnsupported(
            "this engine is a neural MT engine (text-in/text-out) and cannot run "
            "prompt-based tasks (workflow steps / full-text prompts). Use it on the "
            "per-segment translate path, or pick an LLM engine for prompt steps."
        )
    return text


class _ProfileProvider(Provider):
    """Shared base for the EngineProfile-backed engines (openai_compatible / deepl /
    google / microsoft / cohere / lara / amazon). Each is constructed from an
    EngineProfile row plus a test transport (None in production; an httpx.MockTransport
    in the hermetic suite); the decrypted API key comes from the profile lazily.
    Concrete subclasses set `name`/`CAPS` and implement complete()."""

    def __init__(self, profile=None, transport=None):
        self.profile = profile
        self._transport = transport
        self.last_usage = None

    def _profile_api_key(self, kw):
        """The engine's API key: a `api_key` kw override wins, else the profile's
        stored ciphertext decrypted via engines.dec_key ('' when there is no profile).
        Lazy-imports engines to avoid an import cycle (providers <- engines)."""
        return kw.get("api_key") or (
            __import__("app.services.engines", fromlist=["dec_key"]).dec_key(
                self.profile.api_key_enc
            )
            if self.profile
            else ""
        )


def _lara_signed_headers(
    method: str, path: str, access_key_id: str, secret: str, body: bytes = b""
) -> dict:
    """Build the Lara/ModernMT HMAC auth headers — EXPERIMENTAL (scheme [UNCERTAIN],
    needs live verification). The documented scheme signs a canonical string
    `METHOD\\nContent-MD5\\nContent-Type\\nDate\\npath` with HMAC-SHA256(secret) and
    sends `Authorization: Lara <access_key_id>:<base64 signature>`. Content-MD5 is the
    base64 MD5 of the body ("" for an empty body). Deterministic given a fixed Date, so
    request construction is fully unit-testable offline.

    NOTE: MD5 here is a REQUEST-INTEGRITY checksum in the vendor's signing scheme, NOT
    a security primitive — it is the documented Content-MD5 header, not password/secret
    hashing."""
    import base64
    import hashlib
    import hmac

    date = _http_date()
    content_type = "application/json"
    content_md5 = ""
    if body:
        # Content-MD5 is an integrity header the signing scheme requires, not a security hash.
        content_md5 = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
    canonical = "\n".join([method.upper(), content_md5, content_type, date, path])
    sig = base64.b64encode(
        hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "Authorization": f"Lara {access_key_id}:{sig}",
        "Date": date,
        "Content-Type": content_type,
    }
    if content_md5:
        headers["Content-MD5"] = content_md5
    return headers


# Request headers whose value is a credential (compared lower-case).
_SECRET_HEADERS = frozenset(
    {
        "authorization",
        "x-goog-api-key",
        "ocp-apim-subscription-key",
        "x-api-key",
        "api-key",
        "x-subscription-token",
    }
)


def _scrub_request_secrets(r, text: str) -> str:
    """Replace any credential the request carried in a header (and its bare token
    after a scheme word such as 'Bearer') with [redacted] in ``text``. Best-effort:
    a response built without a request returns ``text`` unchanged."""
    try:
        headers = r.request.headers
    except Exception:
        return text
    for name, value in headers.items():
        if name.lower() not in _SECRET_HEADERS or not value:
            continue
        spellings = {value}
        parts = value.split(None, 1)
        if len(parts) == 2 and len(parts[1]) >= 6:
            spellings.add(parts[1])
        for s in sorted(spellings, key=len, reverse=True):
            if len(s) >= 6:
                text = text.replace(s, "[redacted]")
    return text


def _raise_for_nmt_status(r, provider: str) -> None:
    """Turn an HTTP error into a provider exception carrying `status_code`, so
    run_completion._is_terminal_error can route a 4xx (bad key/request) to failover
    and retry a 5xx/timeout. A 200 is a no-op.

    On a 429 (or 503) that carries a `Retry-After` header, the parsed delay (seconds)
    is attached as `err.retry_after` so run_completion's retry loop can honor it
    (max(header, computed backoff), capped) instead of ignoring it and hammering
    straight back into the rate limit."""
    if r.status_code == 200:
        return
    # #302: the error text lands in run notes and in the job log's traceback. If the
    # provider echoes the credential back in its error body, blank it first.
    text = _scrub_request_secrets(r, r.text)
    err = RuntimeError(f"{provider} HTTP {r.status_code}: {text[:200]}")
    err.status_code = r.status_code
    # LOW-3 (gate round 5): carry the FULL, untruncated response body on the exception. The
    # message above caps at 200 chars for readability, but the cured-retry burn fold reads
    # `err.body` so a 400 whose JSON names `reasoning_effort` PAST char 200 still folds to the
    # honest burn instead of leaking the raw HTTP error. Best-effort; never influences a call.
    err.body = text
    if r.status_code in (429, 503):
        ra = _parse_retry_after(r.headers.get("Retry-After"))
        if ra is not None:
            err.retry_after = ra
    raise err


def _zh_is_traditional(loc: str) -> bool:
    """True when a Chinese BCP-47 tag names the Traditional script: an explicit
    ``zh-Hant``/``zh-Hant-*`` script subtag, or a Traditional region (zh-TW/zh-HK/
    zh-MO) with no ``Hans`` override. Every other zh tag (``zh``, ``zh-Hans``,
    ``zh-CN``, ``zh-SG``) is treated as Simplified. Case-insensitive. One classifier
    shared by every NMT target mapper so the three engines never drift (#500)."""
    parts = (loc or "").lower().split("-")
    if "hant" in parts:
        return True
    if "hans" in parts:
        return False
    return len(parts) >= 2 and parts[1] in ("tw", "hk", "mo")


def _deepl_lang(loc: str) -> str:
    """DeepL uppercases language codes and uses a few region variants
    (EN-GB/EN-US, PT-BR/PT-PT); pass those through, else the base subtag upper.
    Chinese is script-split so a zh-TW project is not billed Simplified (#500):
    Traditional variants -> ZH-HANT, everything else (incl. zh/zh-CN) -> ZH."""
    l = loc or ""  # noqa: E741
    special = {"en-us": "EN-US", "en-gb": "EN-GB", "pt-br": "PT-BR", "pt-pt": "PT-PT"}
    hit = special.get(l.lower())
    if hit:
        return hit
    if _base_lang(l) == "zh":
        return "ZH-HANT" if _zh_is_traditional(l) else "ZH"
    return _base_lang(l).upper()


def _google_lang(loc: str) -> str:
    """Google Basic v2 uses ISO-639-1 mostly; split Chinese by script so a zh-TW/zh-HK
    project targets Traditional zh-TW rather than defaulting to Simplified (#500),
    and keep the pt variants."""
    l = (loc or "").lower()  # noqa: E741
    special = {"pt-br": "pt", "pt-pt": "pt-PT"}
    if l in special:
        return special[l]
    if _base_lang(l) == "zh":
        return "zh-TW" if _zh_is_traditional(l) else "zh-CN"
    return _base_lang(l)


def _ms_lang(loc: str) -> str:
    """Azure uses zh-Hans/zh-Hant + pt-br/pt-pt; else the base subtag. Every zh tag
    maps to an explicit script code (#500): zh-TW/zh-HK/zh-MO/zh-Hant-* -> zh-Hant,
    any other zh (zh, zh-CN, zh-SG, zh-Hans-*) -> zh-Hans, never a bare "zh"."""
    l = (loc or "").lower()  # noqa: E741
    special = {"pt-br": "pt", "pt-pt": "pt-pt"}
    if l in special:
        return special[l]
    if _base_lang(l) == "zh":
        return "zh-Hant" if _zh_is_traditional(l) else "zh-Hans"
    return _base_lang(l)


def _amazon_lang(loc: str) -> str:
    """Amazon Translate uses ISO-639-1 base codes for most languages, with a handful
    of region/script variants (zh / zh-TW, pt / pt-PT, es-MX, fr-CA). We pass through
    the base subtag and keep the few variants Amazon distinguishes, following the same
    remap-only convention as the other NMT kinds (no bespoke full table). Chinese is
    script-split so a zh-TW/zh-HK/zh-MO project hits Amazon's native Traditional code
    instead of being billed Simplified (#500): Traditional variants -> zh-TW, else zh.
    An unsupported code surfaces a clean 4xx from Amazon, never silent garbage."""
    l = (loc or "").lower()  # noqa: E741
    special = {
        "pt-br": "pt",
        "pt-pt": "pt-PT",
        "es-mx": "es-MX",
        "fr-ca": "fr-CA",
    }
    if l in special:
        return special[l]
    if _base_lang(l) == "zh":
        return "zh-TW" if _zh_is_traditional(l) else "zh"
    return _base_lang(l)


def assert_provider_visible(name: str | None, org_id) -> None:
    """Guard an authoring provider ref against CROSS-ORG engine reach (MED-2).

    An `engine:<id>` ref naming a profile owned by ANOTHER org must be
    indistinguishable from a nonexistent/dead one — never enumerable (the
    resolve-then-404 house rule; cf. list_models_for_ref's `prof.org_id !=
    org_id` guard). Raises DeadEngineError with the SAME not-live message
    `_get_engine_provider` uses, so the authoring seams that already catch
    DeadEngineError (setup_author._author_json -> clean 400, orchestrator.
    author_prompt -> surfaced by both callers) show the identical clean error
    and cannot tell a foreign engine from a missing one.

    A NO-OP when the multi-org gate is off (org_id None), for a built-in name,
    or for a non-engine ref — byte-identical to today in single-tenant. Call it
    BEFORE get_provider at each authoring seam."""
    if org_id is None:
        return
    ref = (name or "").strip().lower()
    if not ref.startswith("engine:"):
        return
    try:
        pid = int(ref.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    from app.services import engines

    prof = engines.get_profile(pid)
    if prof is not None and getattr(prof, "org_id", None) != org_id:
        raise DeadEngineError(
            f"provider '{name}' references an engine profile that is not live "
            f"(deleted, disabled, or keyless) — refusing to silently fall back to "
            f"the offline mock. Re-point the project/pair/step/connector at a "
            f"configured engine, or add its API key."
        )
