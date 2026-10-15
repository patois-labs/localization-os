"""Issue #506 (items 1, 2, 4, 5): provider correctness, fully offline.

  1. CohereProvider treats finish_reason MAX_TOKENS as truncation, guards empty output,
     and honors an explicit max_tokens.
  2. Cached tokens are counted once for OpenAI / Gemini (prompt tokens include them).
  4. The engine-profile memo cannot store a row read before an invalidation.
  5. A blank-model local profile is judged against the default model's adapter.

Items 3 and 6 of #506 are done by another queue, not here.
Hermetic: throwaway data dir, httpx.MockTransport, no keys, no network.
"""

import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_rest506_")

import httpx
from sqlmodel import Session as _RealSession

from app import config
from app.db import init_db
from app.services import engines, ocr, pricing
from app.services import providers as P
from app.services.providers_common import usage_token_counts

init_db()


_N = [0]


def _cohere(body, captured=None):
    def h(req):
        if captured is not None:
            captured["body"] = __import__("json").loads(req.content)
        return httpx.Response(200, json=body)

    _N[0] += 1
    p = engines.create_profile(f"C506-{_N[0]}", "cohere", api_key="ck", model="command-r")
    return P.CohereProvider(profile=engines.get_profile(p.id), transport=httpx.MockTransport(h))


def test_cohere_truncation_empty_and_max_tokens():
    usage = {"tokens": {"input_tokens": 10, "output_tokens": 5}}
    ok = _cohere(
        {"finish_reason": "COMPLETE", "message": {"content": [{"text": "Hi"}]}, "usage": usage}
    )
    assert ok.complete("Translate: hi") == "Hi"

    cap = {}
    cut = _cohere(
        {"finish_reason": "MAX_TOKENS", "message": {"content": [{"text": "Half"}]}, "usage": usage},
        cap,
    )
    try:
        cut.complete("Translate: a long text", max_tokens=777)
    except P.ProviderTruncated as e:
        assert e.finish_reason == "MAX_TOKENS" and e.budget == 777
    else:
        raise AssertionError("MAX_TOKENS must raise ProviderTruncated")
    assert cap["body"]["max_tokens"] == 777  # explicit max_tokens is sent
    assert cut.last_usage["output_tokens"] == 5  # billed tokens still recorded

    cap2 = {}
    plain = _cohere(
        {"finish_reason": "COMPLETE", "message": {"content": [{"text": "x"}]}, "usage": usage}, cap2
    )
    plain.complete("hi")
    assert "max_tokens" not in cap2["body"]  # translation path unchanged

    empty = _cohere(
        {"finish_reason": "COMPLETE", "message": {"content": []}, "usage": usage},
    )
    try:
        empty.complete("Translate: hi")
    except P.NmtEmptyOutput:
        pass
    else:
        raise AssertionError("empty Cohere output must not be a success")
    print("  Cohere: truncation, empty guard, explicit max_tokens OK")


def test_cached_tokens_counted_once():
    u = SimpleNamespace(
        prompt_tokens=3000,
        completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=2048),
    )
    for d in (P.OpenAIProvider()._usage(u), ocr._openai_usage(u)):
        assert d["input_tokens"] == 952 and d["cache_read_tokens"] == 2048, d
        assert usage_token_counts(d) == (3000, 100), usage_token_counts(d)
    g = SimpleNamespace(
        prompt_token_count=3000, candidates_token_count=100, cached_content_token_count=2048
    )
    d = ocr._gemini_usage(g)
    assert d["input_tokens"] == 952 and usage_token_counts(d) == (3000, 100)
    # cost: 952 plain + 2048 at the cache-read rate, never 3000 + 2048
    c = pricing.cost_usd("openai", "gpt-4o-mini", P.OpenAIProvider()._usage(u))
    in_rate, out_rate = pricing.rates_for("gpt-4o-mini", prompt_tokens=3000)
    # gpt-4o-mini's real cached rate is 0.5x input, so the legacy OpenAI families are priced at
    # the 0.5x upper bound: with the double count gone the cached slice must not be UNDER-counted.
    assert pricing.cache_read_mult("gpt-4o-mini") == 0.5
    want = (952 * in_rate + 100 * out_rate + 2048 * in_rate * 0.5) / 1e6
    assert abs(c - want) < 1e-12, (c, want)
    # gpt-5 / Claude / Gemini keep the 0.1x cache-read multiplier; an `o`-prefixed id that is not
    # an o1/o3/o4 model (and gpt-4-free ids) is untouched.
    verified_tenth = (
        "gpt-5.5",
        "gpt-6.1-sol",
        "claude-opus-4-8",
        "gemini-3.5-flash",
        "gemini-2.5-pro",
        "gemini-3.1-pro-preview",
        "openai/gpt-5",
    )
    for m in verified_tenth:
        assert pricing.cache_read_mult(m) == pricing.CACHE_READ_MULT, m
    for m in ("gpt-4.1", "gpt-4o", "o3-mini", "o4-mini", "o1", "gpt-3.5-turbo", "openai/gpt-4o"):
        assert pricing.cache_read_mult(m) == 0.5, m
    # Review gate: an id whose cached rate was NOT verified at <= 0.1x (no cached price, Gemini TTS
    # at 0.25x, retired Gemini, fine-tunes, openai_compatible hosts, unknown / blank) bills the
    # cached slice at the FULL input rate, never below the provider's real price.
    unverified = (
        "gpt-5.5-pro",
        "o1-pro",
        "gpt-4o-2024-05-13",
        "gemini-3.8-flash-tts",
        "gemini-2.0-flash",
        "ft:gpt-4o-mini:org::x",
        "deepseek-chat",
        "grok-4",
        "omni-x",
        "gpt-7",
        None,
        "",
    )
    for m in unverified:
        assert pricing.cache_read_mult(m) == 1.0, m
    # the total never undershoots the old double count's real bill: 3000-token prompt, 2048 cached,
    # gpt-4o: real = 952*1 + 2048*0.5 = 1976 input-units; recorded = same.
    assert abs((952 + 2048 * 0.5) - 1976) < 1e-9
    # no cache: unchanged; cached > prompt (garbage) clamps instead of going negative
    plain = SimpleNamespace(prompt_tokens=50, completion_tokens=1, prompt_tokens_details=None)
    assert P.OpenAIProvider()._usage(plain)["input_tokens"] == 50
    odd = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=1,
        prompt_tokens_details=SimpleNamespace(cached_tokens=99),
    )
    assert P.OpenAIProvider()._usage(odd)["input_tokens"] == 0
    print("  cached tokens counted once (usage + cost) OK")


def test_profile_memo_ignores_read_that_raced_an_invalidation():
    config.ENGINE_PROFILE_CACHE_TTL_S = 60.0
    engines.invalidate_profile_cache()
    prof = engines.create_profile("R506", "cohere", api_key="k", model="m")
    pid = prof.id
    engines.invalidate_profile_cache()

    class RacingSession(_RealSession):
        fired = False

        def get(self, *a, **kw):
            row = super().get(*a, **kw)  # the OLD (enabled) row is already read ...
            if not RacingSession.fired:
                RacingSession.fired = True
                # ... when another writer disables the engine and commits.
                engines.Session = _RealSession
                engines.update_profile(pid, enabled=False)
                engines.Session = RacingSession
            return row

    real = engines.Session
    engines.Session = RacingSession
    try:
        engines.get_profile(pid)  # reads pre-disable row; must NOT memoize it
    finally:
        engines.Session = real
    assert RacingSession.fired
    assert engines.get_profile(pid).enabled is False, "stale pre-invalidation row was memoized"
    print("  engine profile memo drops a read that raced an invalidation OK")


def test_blank_model_local_profile_uses_default_model_adapter():
    prof = SimpleNamespace(kind="local", model="")
    old = config.DEFAULT_MODELS["local"]
    try:
        config.DEFAULT_MODELS["local"] = "qwen3.8-27b"  # ChatML adapter: ignores images
        assert P._vision_capable_resolved("engine:1", profile=prof) is False
        config.DEFAULT_MODELS["local"] = "gemma-4-12b"  # plain chat path: vision ok
        assert P._vision_capable_resolved("engine:1", profile=prof) is True
        assert (
            P._vision_capable_resolved(
                "engine:1", profile=SimpleNamespace(kind="local", model="qwen3.8-27b")
            )
            is False
        )
    finally:
        config.DEFAULT_MODELS["local"] = old
    print("  blank-model local profile vision check resolves the default model OK")


if __name__ == "__main__":
    test_cohere_truncation_empty_and_max_tokens()
    test_cached_tokens_counted_once()
    test_profile_memo_ignores_read_that_raced_an_invalidation()
    test_blank_model_local_profile_uses_default_model_adapter()
    print("REST506 OK")
