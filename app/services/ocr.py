"""Vision-LLM OCR — transcribe a page image with ANY vision-capable engine.

Modern multimodal LLMs are excellent OCR engines and need no system binaries
(no Tesseract/poppler), which keeps air-gapped installs simple. The same image
goes to whichever provider the project uses:
  - Claude / Gemini / OpenAI  (cloud, vision-capable)
  - local (LM Studio / Ollama) running a vision model like Qwen3-VL

If no vision-capable engine is configured, a deterministic placeholder is
returned so ingestion still works offline (and tests stay deterministic).

SPEND GUARD (issue #29): every real-provider branch below is gated by the same
brake run_completion uses (providers.py:1961,1979) BEFORE the call, and records
real usage to the ledger AFTER it (spend.py:100-194). This module builds raw SDK
clients directly rather than going through the app's Provider abstraction
(Provider.complete() takes a text prompt only, no image parameter — vision calls
can't route through it), so it can't reuse spend.blocked_for(prov)/record_provider
(prov, ...) verbatim; those are shaped around a resolved Provider instance's
`.last_usage`, set only by Provider.complete(). Instead `_guarded_vision_call`
(below) uses spend's lower-level primitives (would_block/record_usage) directly.
Every branch that reaches a real SDK call already implies a REAL, keyed engine —
ocr_image only enters a branch when the matching key/base URL is configured — so
there is no Mock/offline case to exempt here; that path is the separate
placeholder return at the bottom of ocr_image, which never touches spend at all.

Documented in: docs/architecture/image-localization.md
"""

import base64

from app import config
from app.services.credential_origin import slot_key_for
from app.services.providers import _claude_usage, _local_client

OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text in this document image exactly as it "
    "appears. Preserve reading order and line/paragraph breaks. Do not translate, "
    "summarize, or add commentary — output only the transcribed text."
)
_MAX = 4000


def _openai_usage(u) -> dict | None:
    """Normalize an OpenAI (or OpenAI-compatible local server) usage object into
    our usage dict. Mirrors providers.OpenAIProvider._usage exactly; duplicated
    (not imported) because that normalizer lives on the Provider class and this
    module builds its own SDK client (see module docstring)."""
    if not u:
        return None
    details = getattr(u, "prompt_tokens_details", None)
    cached = int((getattr(details, "cached_tokens", 0) if details else 0) or 0)
    # prompt_tokens includes the cached slice; input_tokens is the non-cached remainder.
    return {
        "input_tokens": max(0, int(getattr(u, "prompt_tokens", 0) or 0) - cached),
        "output_tokens": getattr(u, "completion_tokens", 0) or 0,
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
    }


def _gemini_usage(u) -> dict | None:
    """Normalize a Gemini usage_metadata object. Mirrors the inline usage dict
    providers.GeminiProvider.complete builds after its stream completes."""
    if not u:
        return None
    cached = int(getattr(u, "cached_content_token_count", 0) or 0)
    # prompt_token_count includes the cached slice; input_tokens is the non-cached remainder.
    return {
        "input_tokens": max(0, int(getattr(u, "prompt_token_count", 0) or 0) - cached),
        "output_tokens": getattr(u, "candidates_token_count", 0) or 0,
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
    }


def _estimated_usage(input_chars: int, output_text: str) -> dict:
    """Conservative fallback usage when a real provider's response carries no
    usage block. All three SDKs return one on today's non-streaming OCR calls,
    so this is defense-in-depth, not the common case — same idea as
    run_completion's own fallback (providers.py:1971-1974): an untracked call
    still moves the ledger by estimate rather than silently costing $0.
    ~4 chars/token; `input_chars` is measured over the base64 image payload +
    prompt (not just the short text prompt) so a vision call isn't wildly
    undercounted against a text-only estimate."""
    return {
        "input_tokens": max(1, input_chars // 4),
        "output_tokens": max(0, len(output_text or "") // 4),
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated": True,
    }


def _guarded_vision_call(provider_name: str, model: str, input_chars: int, call_fn):
    """Spend-guard wrapper around ONE real-provider vision call.

    Factored out (rather than inlined in ocr_image) so a second caller — the
    upcoming image-localization module's region/context analysis calls
    (issue #29) can reuse
    the identical brake instead of re-deriving it.

    `call_fn()` performs the actual SDK call and returns (text, usage_dict_or_
    None); it is invoked ONLY after the spend check clears, so a blocked call
    never constructs a client or reaches the network. Returns (text, block_msg):
    block_msg is None on success, or the terminal 'Spend cap reached' message
    when the cap is on and tripped (mirroring spend.cap_message/blocked_for).
    """
    from app.services import ai_optout, spend

    # AI opt-out choke point (DPAI-08): an opted-out project's page image never reaches
    # a vision model; the caller renders the refusal as its "[OCR blocked: ...]" text.
    try:
        ai_optout.guard("image OCR")
    except ai_optout.AIOptOutRefused as exc:
        return None, str(exc)
    blocked, spent, cap = spend.would_block()
    if blocked:
        spend.note_trip()
        return None, spend.cap_message(spent, cap)
    text, usage = call_fn()
    # #361: the ledger's free-engine rule: a self-hosted local vision model books nothing even
    # though DEFAULT_MODELS["local"] names a real model id (qwen3-vl, ...); a public `local`
    # endpoint and every cloud provider book as before.
    spend.record_ref_usage(provider_name, model, usage or _estimated_usage(input_chars, text))
    # #359: OCR text that carries a global credential is rejected (callers render the
    # "[OCR error: ...]" placeholder); billed above either way.
    from app.services.secrets_scrub import guard_answer

    guard_answer(text)
    return text, None


def ocr_image(png_bytes: bytes, provider: str | None = None) -> str:
    """Transcribe a PNG page image. `provider` defaults to the configured default.

    When the spend cap blocks a real-provider call, this returns a clean
    placeholder string — SAME shape/contract as the "no engine configured"
    placeholder below — rather than raising. The sole caller today, PdfFilter.
    parse (app/filters/pdf_filter.py), takes this return value as literal
    paragraph text with no try/except around the call; raising a spend
    exception here would crash the whole document ingest instead of degrading
    gracefully the way the existing offline path already does.
    """
    provider = (provider or config.DEFAULT_PROVIDER or "").lower()
    b64 = base64.b64encode(png_bytes).decode()
    input_chars = len(OCR_PROMPT) + len(b64)
    try:
        if provider == "local" and config.LOCAL_BASE_URL:
            model = config.DEFAULT_MODELS.get("local") or "local-model"

            def _call():
                from openai import DEFAULT_TIMEOUT

                from app.services.providers_hub import local_sampling_kwargs

                client = _local_client(
                    base_url=config.LOCAL_BASE_URL,
                    api_key=slot_key_for(config.LOCAL_BASE_URL),
                    timeout=DEFAULT_TIMEOUT,
                )
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        max_tokens=_MAX,
                        # #342: the pinned local sampling (OCR is a generate call).
                        **local_sampling_kwargs(),
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": OCR_PROMPT},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                                    },
                                ],
                            }
                        ],
                    )
                finally:
                    # #437: the SDK never closes a caller-supplied http_client; this
                    # per-image client is closed on every path.
                    client.close()
                return resp.choices[0].message.content or "", _openai_usage(
                    getattr(resp, "usage", None)
                )

            text, block_msg = _guarded_vision_call("local", model, input_chars, _call)
            return text if block_msg is None else f"[OCR blocked: {block_msg}]"
        if provider == "openai" and config.OPENAI_API_KEY:
            model = config.DEFAULT_MODELS["openai"]

            def _call():
                from openai import OpenAI

                client = OpenAI(api_key=config.OPENAI_API_KEY)
                resp = client.chat.completions.create(
                    model=model,
                    max_tokens=_MAX,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": OCR_PROMPT},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                                },
                            ],
                        }
                    ],
                )
                return resp.choices[0].message.content or "", _openai_usage(
                    getattr(resp, "usage", None)
                )

            text, block_msg = _guarded_vision_call("openai", model, input_chars, _call)
            return text if block_msg is None else f"[OCR blocked: {block_msg}]"
        if provider == "claude" and config.ANTHROPIC_API_KEY:
            model = config.DEFAULT_MODELS["claude"]

            def _call():
                import anthropic

                client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
                msg = client.messages.create(
                    model=model,
                    max_tokens=_MAX,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": b64,
                                    },
                                },
                                {"type": "text", "text": OCR_PROMPT},
                            ],
                        }
                    ],
                )
                text = "".join(b.text for b in msg.content if b.type == "text")
                return text, _claude_usage(getattr(msg, "usage", None))

            text, block_msg = _guarded_vision_call("claude", model, input_chars, _call)
            return text if block_msg is None else f"[OCR blocked: {block_msg}]"
        if provider == "gemini" and config.GEMINI_API_KEY:
            model = config.DEFAULT_MODELS["gemini"]

            def _call():
                from google import genai
                from google.genai import types

                client = genai.Client(api_key=config.GEMINI_API_KEY)
                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                        OCR_PROMPT,
                    ],
                )
                return resp.text or "", _gemini_usage(getattr(resp, "usage_metadata", None))

            text, block_msg = _guarded_vision_call("gemini", model, input_chars, _call)
            return text if block_msg is None else f"[OCR blocked: {block_msg}]"
    except Exception as e:
        return f"[OCR error: {type(e).__name__}: {e}]"

    return "[scanned page - configure a vision-capable engine in Setup AI to enable OCR]"
