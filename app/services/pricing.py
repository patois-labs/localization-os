"""Static per-model USD pricing, used ONLY by the spend-cap ledger (spend.py).

Numbers are published list prices per 1,000,000 tokens (input / output), current
as of 2026-07 (Anthropic rows re-checked 2026-09-22; Gemini, OpenAI and Cohere rows
re-checked 2026-10-01 against the cited pages, see the per-row comments for the few
unverified ones). They are DELIBERATELY a
plain in-code table, not a setting: the cap is a safety brake, and a brake whose
calibration lived in the same DB an operator is racing to edit under pressure is
a footgun. If a provider changes a price, edit this file (it ships with the code
that reads it).

Model matching is by longest-substring-of-the-model-id (the longest matching key
wins, whatever the table order), so `claude-opus-4-8`, `claude-opus-4-8[1m]`, and
a dated snapshot all map to the opus rate. An UNKNOWN model falls back to a
CONSERVATIVE default (opus-tier 5/25) rather than 0 — over-counting an unknown
model's spend trips the cap early, which
is the safe direction for a kill switch. Local/self-hosted and the mock are
priced at 0 (no real money leaves the building), so offline/local installs never
trip the cap. #360: that zero is an EXACT-id allowlist (`mock`, `local`, `local-model`),
never a substring, so a paid model id that happens to contain "local" or "mock" is priced
normally. NOTE (QA-4): a live self-hosted model reports its REAL id (e.g.
`meta-llama-3-8b-instruct`), which this table can't recognize. The self-hosted-is-
free invariant is therefore enforced at the seam that knows the engine KIND —
spend.is_exempt exempts a LocalProvider on a proven private endpoint, so record_usage
is never reached for that call. Public local endpoints use the conservative default.
This table stays keyed on the model id for the CLOUD
providers (where the id is the only pricing signal available).

Cache tokens (the fields P0 added to the usage dict) are priced off the model's
INPUT rate: a cache READ is ~0.1x input, a cache WRITE ~1.25x input — Anthropic's
published multipliers, which OpenAI/Gemini's implicit caching is close enough to
for a conservative brake (the pre-gpt-5 OpenAI families, whose cached discount is only
0.25x to 0.5x, take 0.5x: see cache_read_mult).

Two price shapes the flat table cannot hold live beside it (#360 gate): PROMPT-SIZE
tiers (_TIERS: Gemini Pro bills a higher rate once one call's prompt passes 200k
tokens; cost_usd picks the tier from that call's own prompt tokens, and a caller with
no token count gets the higher tier) and DATED list prices (_DATED: Gemini 3.6-3.8
Flash list one rate through 2026-12-31 and a higher one after; the current rate is
used until the date passes, then the later one, with no code edit).

Documented in: docs/architecture/cost-and-budgets.md
"""

import re
from datetime import date, datetime, timezone

# (input_per_1m, output_per_1m) USD. The LONGEST key found as a substring of the
# (lower-cased) model id wins (see rates_for), so a more-specific id beats its
# prefix regardless of order; keep more-specific ids listed first anyway for reading.
_PRICES: dict[str, tuple[float, float]] = {
    # --- Anthropic (the spec's named models) ---
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku": (1.0, 5.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (2.0, 10.0),  # Sonnet 5 list is 2/10 (not the 4.x 3/15)
    "claude-sonnet": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
    # opus-5 and opus-5-5 overlap as substrings; the longest-match rule in rates_for
    # sends `claude-opus-5-5` (and `claude-opus-5-5[1m]`) to the 4/20 key and plain
    # `claude-opus-5` (and `claude-opus-5[1m]`) to 5/25.
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus": (5.0, 25.0),  # 4-5/4-6/4-7/4-8 all 5/25
    "claude-fable-5": (10.0, 50.0),  # also covers claude-fable-5-1 (10/50)
    "claude": (5.0, 25.0),  # any other claude -> opus-tier (conservative)
    # --- OpenAI ---
    # Source: https://developers.openai.com/api/docs/pricing (standard tier), fetched
    # 2026-10-01 (#360 gate). Every listed text model has its own row. The family keys
    # ("gpt-6", "gpt-5", "gpt-4", "gpt-3.5", "gpt") are ALSO the substring every unlisted
    # id of that family falls to, so each carries the HIGHEST non-pro rate of its family
    # rather than the bare model's own list price (plain `gpt-5` lists 1.25/10 and is
    # over-counted 4x here; that is the safe direction for a brake).
    "gpt-6-astra": (10.0, 50.0),
    "gpt-6.1-sol": (2.0, 10.0),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6": (10.0, 50.0),
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4-pro": (30.0, 180.0),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.2-pro": (21.0, 168.0),
    "gpt-5.2": (1.75, 14.0),
    "gpt-5.1": (1.25, 10.0),
    "gpt-5-pro": (15.0, 120.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5": (5.0, 30.0),  # family catch-all (gpt-5.5 rate); bare gpt-5 lists 1.25/10
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o-2024-05-13": (5.0, 15.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.5, 10.0),
    "gpt-4-turbo": (10.0, 30.0),
    "gpt-4": (30.0, 60.0),  # gpt-4-0613 list; family catch-all
    "gpt-3.5-turbo-instruct": (1.5, 2.0),
    "gpt-3.5-turbo-1106": (1.0, 2.0),
    "gpt-3.5-turbo": (0.5, 1.5),
    "gpt-3.5": (1.5, 2.0),  # family catch-all (highest listed gpt-3.5 rate)
    "o1-pro": (150.0, 600.0),
    "o1": (15.0, 60.0),
    "o3-pro": (20.0, 80.0),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    # (bare `o3`, list 2/8, deliberately has NO key: a two-character key would match
    # unrelated ids below the default rate. It takes the 5/25 default, an over-count.)
    "gpt": (10.0, 50.0),  # unknown gpt -> highest listed non-pro rate (gpt-6-astra)
    "text-embedding-3-small": (0.02, 0.0),
    # --- Google Gemini ---
    # Source: https://ai.google.dev/gemini-api/docs/pricing (paid tier, standard, text
    # input), fetched 2026-10-01. Pro rows here are the <=200k-prompt rate; the >200k
    # rate is in _TIERS. The 3.6/3.7/3.8 Flash rows here are the rate FROM 2027-01-01;
    # the lower rate in force through 2026-12-31 is in _DATED.
    "gemini-3.8-flash-lite-tts": (1.00, 12.0),
    "gemini-3.8-flash-tts": (1.00, 18.0),
    "gemini-3.8-flash": (1.50, 7.50),
    "gemini-3.7-flash": (1.50, 7.50),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.5-flash": (1.50, 9.0),
    "gemini-3.5-live-translate": (3.50, 21.0),
    "gemini-3.5-transcribe-live": (3.50, 21.0),
    "gemini-3.5-transcribe": (2.00, 12.0),
    "gemini-3.1-pro": (2.0, 12.0),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-flash-tts": (1.00, 20.0),
    "gemini-3-flash": (0.50, 3.00),
    "gemini-3-pro-image": (2.0, 120.0),  # output priced at the image-output rate
    # `gemini-3-pro-preview` was shut down 2026-03-09 (ai.google.dev/gemini-api/docs/
    # deprecations; replacement gemini-3.1-pro-preview) and is no longer on the pricing
    # page; priced like its replacement, which matches its last published list rate.
    "gemini-3-pro": (2.0, 12.0),
    "gemini-2.5-pro-preview-tts": (1.00, 20.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash-preview-tts": (0.50, 10.0),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    # UNVERIFIED 2026-10-01: shut down 2026-06-01 (deprecations page) and gone from the
    # pricing page; kept at its last published list rate (calls to it now fail anyway).
    "gemini-2.0-flash": (0.10, 0.40),
    # UNVERIFIED 2026-10-01: retired, on neither page. Priced at their former >128k-prompt
    # tier, the conservative choice for an unverifiable row.
    "gemini-1.5-pro": (2.50, 10.0),
    "gemini-1.5-flash": (0.15, 0.60),
    # Any other / unknown gemini id -> the Pro >200k rate, NOT the cheapest flash rate:
    # an unknown id over-counts instead of slipping through.
    "gemini": (4.0, 18.0),
    # --- Cohere command (LLM, P3) ---
    # Source: https://cohere.com/pricing, fetched 2026-10-01. The page lists per-token
    # rates only for these models ("existing customers"); current models such as Command A
    # are custom-priced, so unknown command-* / cohere ids take the HIGHEST listed rate.
    "command-r-plus-04-2024": (3.0, 15.0),
    "command-r-plus-08-2024": (2.5, 10.0),
    "command-r-plus": (3.0, 15.0),  # undated alias -> the higher dated rate
    "command-r-03-2024": (0.50, 1.50),
    "command-r": (0.50, 1.50),  # the only listed Command R rate
    "command-light": (0.30, 0.60),
    "command": (3.0, 15.0),  # bare `command` lists 1/2; catch-all for every other command-*
    "cohere": (3.0, 15.0),
    # --- openai_compatible hosts (OpenRouter/DeepSeek/Mistral/Groq/…): each host
    # has its own free-text model-id namespace and PROVIDER_EXPANSION.md cites NO
    # confirmed per-Mtoken rates for them, so NO entries are added here — an unknown
    # model id falls through to _DEFAULT (opus-tier 5/25). That is intentionally
    # CONSERVATIVE for a safety-brake spend cap: an unrecognized model over-counts
    # (trips the cap early) rather than slipping through free. Add a specific entry
    # here (keyed on the model-id substring, e.g. "deepseek-chat") once a host's
    # published rate is confirmed. NOTE: a model id already containing a known
    # substring (e.g. "gpt", "claude") maps to THAT rate — good enough for the brake.
}

# NMT engines (P3) are priced per CHARACTER, not per token — a different unit, so a
# separate table keyed on the engine kind. USD per 1,000,000 source characters,
# published list prices (gapfill Gap-3 / plan): DeepL ~$25/M (Pro list),
# Google Basic v2 $20/M, Microsoft $10/M. An NMT usage dict carries `chars` +
# `nmt_kind`; cost_usd routes to this table. Unknown kind -> a conservative $25/M.
_NMT_PER_MCHAR: dict[str, float] = {
    "deepl": 25.0,
    "google": 20.0,
    "microsoft": 10.0,
    # Lara (Translated): list price is €20/Mchar. This table is USD (the cap is USD),
    # so convert at ~1.08 USD/EUR (approx 2026-07) -> ~$21.60/Mchar. The rate is a
    # safety-brake calibration, not a billing figure, so a rounded assumption is fine;
    # if the FX or list price moves materially, edit here. (PROVIDER_EXPANSION §5.)
    "lara": 21.6,
    # Amazon Translate standard tier: $15/Mchar (already USD). Active Custom
    # Translation ($60/M) is not wired. (PROVIDER_EXPANSION §2, mt_providers.md #3.)
    "amazon": 15.0,
}
_NMT_DEFAULT_PER_MCHAR = 25.0

# Fallback for a model id that matches NO key above. Conservative (opus-tier) so an
# unrecognized real model over-counts (trips the cap early) rather than slipping
# through free.
_DEFAULT: tuple[float, float] = (5.0, 25.0)

# Cache-token multipliers on the INPUT rate (Anthropic's published shape).
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25
# Now that the adapters report the NON-cached remainder as input_tokens (#506), a cache-read
# multiplier BELOW the provider's real cached rate would UNDER-count spend, so 0.1x is used
# only where the published cached rate was verified at or below 0.1x of input (pricing
# pages fetched 2026-10-07): Claude (Anthropic: cache read 0.1x); OpenAI gpt-5* / gpt-6*
# non-pro (0.1x, gpt-6.1-sol 0.05x); Gemini 2.5 / 3.x text models (0.1x, also >200k).
# OpenAI's pre-gpt-5 families discount less (gpt-4o, o1, o3-mini 0.5x; gpt-4.1, o3,
# o4-mini 0.25x) and take the 0.5x upper bound. Every other id (pro models with no cached
# price, Gemini TTS at 0.25x, retired Gemini 1.5/2.0, fine-tunes, openai_compatible hosts,
# unknown or blank ids) is billed the FULL input rate on its cached slice: an over-count,
# never under, until a verified rate is added here.
_CACHE_READ_MULT_LEGACY_OPENAI = 0.5
_CACHE_READ_MULT_UNVERIFIED = 1.0
_LEGACY_OPENAI_RE = re.compile(r"(?:^|/)(?:gpt-4|gpt-3\.5|o[134](?:-|$))")
_NO_CACHE_DISCOUNT_RE = re.compile(
    r"(?:^|/)(?:gpt-[\d.]+|o\d)-pro(?:-|$)|gpt-4o-2024-05-13|tts|image|live|transcribe"
)
_VERIFIED_TENTH_RE = re.compile(r"claude|(?:^|/)gpt-[56](?:[.-]|$)|gemini-(?:2\.5|3)(?:[.-]|$)")


def cache_read_mult(model: str | None) -> float:
    """Cache-READ multiplier on the input rate for ``model`` (see the constants above)."""
    m = str(model or "").strip().lower()
    if not m:
        return _CACHE_READ_MULT_UNVERIFIED
    if "claude" in m:
        return CACHE_READ_MULT
    if _NO_CACHE_DISCOUNT_RE.search(m):
        return _CACHE_READ_MULT_UNVERIFIED
    if _LEGACY_OPENAI_RE.search(m):
        return _CACHE_READ_MULT_LEGACY_OPENAI
    if _VERIFIED_TENTH_RE.search(m):
        return CACHE_READ_MULT
    return _CACHE_READ_MULT_UNVERIFIED


# Model ids that price at 0, matched EXACTLY (never as a substring, #360): the test mock
# and the placeholder ids the self-hosted `local` engine slot falls back to. A paid model id
# that merely CONTAINS "local" or "mock" (e.g. `acme-local-pro`) is NOT free and takes the
# normal table / conservative default. The real self-hosted-is-free rule lives at
# spend.is_exempt (engine KIND, proven private endpoint); this list only covers the
# report/estimate seams that have a provider name or fallback id but no provider instance.
_FREE_MODEL_IDS = frozenset({"mock", "local", "local-model"})


# Prompt-size tiers, keyed on the SAME _PRICES key the model id matched:
# key -> (threshold prompt tokens, (input, output) for a prompt ABOVE the threshold).
# _PRICES holds the at-or-below rate. Source: the Gemini pricing page cited above.
_TIERS: dict[str, tuple[int, tuple[float, float]]] = {
    "gemini-3.1-pro": (200_000, (4.0, 18.0)),
    "gemini-3-pro": (200_000, (4.0, 18.0)),
    "gemini-2.5-pro": (200_000, (2.50, 15.0)),
}

# Dated list prices, keyed on the matched _PRICES key: key -> (last day the rate below
# applies, (input, output) through that day). _PRICES holds the rate after it. The day is
# compared in UTC, so the switch lands a few hours early for a Pacific-time bill (the
# over-count direction). Source: the Gemini pricing page cited above.
_GEMINI_FLASH_UNTIL = date(2026, 12, 31)
_DATED: dict[str, tuple[date, tuple[float, float]]] = {
    "gemini-3.8-flash": (_GEMINI_FLASH_UNTIL, (0.75, 3.75)),
    "gemini-3.7-flash": (_GEMINI_FLASH_UNTIL, (0.75, 3.75)),
    "gemini-3.6-flash": (_GEMINI_FLASH_UNTIL, (0.75, 3.75)),
    "gemini-3.8-flash-tts": (_GEMINI_FLASH_UNTIL, (0.50, 9.0)),
    "gemini-3.8-flash-lite-tts": (_GEMINI_FLASH_UNTIL, (0.50, 6.0)),
}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def rates_for(
    model: str | None, prompt_tokens: int | None = None, on: date | None = None
) -> tuple[float, float]:
    """(input_per_1m, output_per_1m) USD for a model id. An exact _FREE_MODEL_IDS id is
    (0, 0); otherwise longest-substring match; unknown -> conservative default.
    None/blank -> default too.

    `prompt_tokens` picks a _TIERS tier (None = unknown size -> the HIGHER tier, the
    conservative answer). `on` is the day the call happened, for a _DATED row
    (None = today, UTC)."""
    m = (model or "").lower()
    if not m:
        return _DEFAULT
    if m in _FREE_MODEL_IDS:
        return (0.0, 0.0)
    # longest matching key wins (most specific), so "claude-opus-4-8" beats "claude"
    best_key = None
    for key in _PRICES:
        if key in m and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is None:
        return _DEFAULT
    rate = _PRICES[best_key]
    dated = _DATED.get(best_key)
    if dated is not None and (on or _today()) <= dated[0]:
        rate = dated[1]
    tier = _TIERS.get(best_key)
    if tier is not None and (prompt_tokens is None or prompt_tokens > tier[0]):
        rate = tier[1]
    return rate


def cost_usd(
    provider: str | None, model: str | None, usage: dict | None, on: date | None = None
) -> float:
    """USD cost of one provider call, from its usage dict.

    usage keys (all optional, default 0): input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens. Cache tokens are billed off the input
    rate (read 0.1x, write 1.25x); plain input tokens are the NON-cached remainder
    the provider ADAPTER normalizes to: Anthropic reports it uncached natively, while
    OpenAI prompt_tokens and Gemini prompt_token_count include the cached slice, so
    providers_hub / ocr subtract it before building the usage dict (#506).

    `provider` is accepted for future per-provider overrides but pricing is keyed
    on the model today; a blank model falls back via rates_for().

    NMT engines (P3) are priced per CHARACTER, not per token: a usage dict carrying
    `chars` + `nmt_kind` is billed off _NMT_PER_MCHAR (DeepL/Google/Microsoft) and
    returns early — its token fields are 0, so the token math below is a no-op for
    it, but returning here keeps the two units cleanly separate.

    A _TIERS model is priced on THIS usage's prompt size (input + cache read + cache
    write tokens). A caller that sums many calls into one usage dict (a report over a
    whole job) therefore lands on the higher tier sooner: an over-count, never under.
    `on` is the day of the call for a _DATED row (None = today, UTC)."""
    if not usage:
        return 0.0
    chars = int(usage.get("chars", 0) or 0)
    if chars > 0:
        rate = _NMT_PER_MCHAR.get(str(usage.get("nmt_kind") or "").lower(), _NMT_DEFAULT_PER_MCHAR)
        return chars * rate / 1_000_000.0
    try:
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        cr = int(usage.get("cache_read_tokens", 0) or 0)
        cw = int(usage.get("cache_write_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    in_rate, out_rate = rates_for(model, prompt_tokens=inp + cr + cw, on=on)
    per = 1_000_000.0
    return (
        inp * in_rate
        + out * out_rate
        + cr * in_rate * cache_read_mult(model)
        + cw * in_rate * CACHE_WRITE_MULT
    ) / per
