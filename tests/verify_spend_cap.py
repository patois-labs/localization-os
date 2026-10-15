"""Global spend-cap kill switch (offline). Spend-cap standalone.

Hermetic: throwaway LOC_TMS_DATA_DIR, provider keys cleared (forces MockProvider),
ASCII-only prints, FastAPI TestClient.

Covers:
  * pricing math: per-model input/output rates (haiku 1/5, sonnet 3/15, opus 5/25),
    cache read (0.1x input) / write (1.25x input) rates, unknown-model conservative
    fallback, local/mock priced at 0.
  * record_usage accumulates real spend per period; zero-cost (mock/local) is a no-op.
  * enforcement: with the cap on and spend >= cap, run_completion BLOCKS a REAL
    provider (returns the terminal 'Spend cap reached' error, does NOT call it) and
    fails over rather than retrying; under the cap it calls + records.
  * MockProvider is EXEMPT (offline install never trips, even with the cap set tiny).
  * would_block / app_meta banner flag set only when tripped; audit event once on trip.
  * settings round-trip: web /setup-ai/spend save + CLI `config get/set spend_cap_usd`.
  * CLI `health` prints a spend status line.
  * task_cca6bb89 (shared-parse hardening): unlike DepartmentBudget.
    monthly_token_limit (a raw sqlite3 OverflowError -> 500 on a huge int),
    spend_cap_usd is a TEXT setting parsed with float(), so a huge/long numeric
    string doesn't crash -- it silently saturates to `inf` and used to be
    accepted verbatim as a nonsensical "$inf" cap. /setup-ai/spend now rejects
    that (and anything past MAX_SPEND_CAP_USD) with a clean redirect banner;
    cap_usd() also defensively treats any non-finite/out-of-range stored value
    as OFF (belt-and-suspenders for the generic `config set` bypass, which has
    no per-key validation).
"""

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_spend_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import SpendLedger
from app.services import apikeys, audit, pricing, security, settings
from app.services import providers as _prov
from app.services import spend as spend_svc
from app.services.providers import (
    DeepLProvider,
    LocalProvider,
    MockProvider,
    OpenAICompatibleProvider,
    Provider,
    ProviderSpec,
    register_provider,
    run_completion,
)
from tests._csrf import csrf_for


class LiveStub(Provider):
    """A 'configured' engine (so it is NOT a MockProvider). Counts its calls so we
    can prove a blocked call never reaches the provider."""

    name = "claude"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        LiveStub.calls += 1
        # A real provider sets last_usage after a call; mimic it so the direct-call
        # recorder (spend.record_provider) can accrue for author-prompt/glossary.
        self.last_usage = {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "[fr] hi"


def _reset_ledger():
    """Zero the current period's ledger row so a test starts from $0 regardless of
    order (record_usage is additive across the whole suite)."""
    key = spend_svc.period_key()
    with Session(engine) as s:
        row = s.get(SpendLedger, key)
        if row:
            row.accumulated_usd = 0.0
            s.add(row)
            s.commit()
    spend_svc._tripped_audited.clear()


def test_pricing_math():
    """Per-model rates + cache multipliers + conservative unknown fallback."""
    assert pricing.rates_for("claude-haiku-4-5") == (1.0, 5.0)
    assert pricing.rates_for("claude-sonnet-4-6") == (3.0, 15.0)
    assert pricing.rates_for("claude-sonnet-4-5") == (3.0, 15.0)
    # longest-substring match: opus id must beat the bare "claude" key
    assert pricing.rates_for("claude-opus-4-8") == (5.0, 25.0)
    assert pricing.rates_for("claude-opus-4-8[1m]") == (5.0, 25.0)
    # 2026-09-22 list: Sonnet 5 is 2/10 (not the 4.x 3/15); the generic sonnet
    # fallback stays 3/15.
    assert pricing.rates_for("claude-sonnet-5") == (2.0, 10.0)
    assert pricing.rates_for("claude-sonnet-5[1m]") == (2.0, 10.0)
    assert pricing.rates_for("claude-sonnet-unknown") == (3.0, 15.0)
    # Opus 5.5 is 4/20; the longer key must beat the overlapping claude-opus-5 key,
    # and plain Opus 5 must stay 5/25 (non-regression).
    assert pricing.rates_for("claude-opus-5-5") == (4.0, 20.0)
    assert pricing.rates_for("claude-opus-5-5[1m]") == (4.0, 20.0)
    assert pricing.rates_for("claude-opus-5") == (5.0, 25.0)
    assert pricing.rates_for("claude-opus-5[1m]") == (5.0, 25.0)
    # Longest-key-wins must not depend on table order: with the table reversed
    # (claude-opus-5 now listed BEFORE claude-opus-5-5), a first-match lookup would
    # send claude-opus-5-5 to 5/25. Swap the module dict, restore it after.
    original_prices = pricing._PRICES
    pricing._PRICES = dict(reversed(list(original_prices.items())))
    try:
        keys = list(pricing._PRICES)
        assert keys.index("claude-opus-5") < keys.index("claude-opus-5-5")
        assert pricing.rates_for("claude-opus-5-5") == (4.0, 20.0)
        assert pricing.rates_for("claude-opus-5-5[1m]") == (4.0, 20.0)
        assert pricing.rates_for("claude-opus-5") == (5.0, 25.0)
        assert pricing.rates_for("claude-sonnet-5") == (2.0, 10.0)
    finally:
        pricing._PRICES = original_prices
    assert pricing.rates_for("claude-fable-5") == (10.0, 50.0)
    assert pricing.rates_for("claude-fable-5-1") == (10.0, 50.0)
    one_m_each = {"input_tokens": 1000000, "output_tokens": 1000000}
    assert abs(pricing.cost_usd("claude", "claude-sonnet-5", one_m_each) - 12.0) < 1e-9
    assert abs(pricing.cost_usd("claude", "claude-opus-5-5", one_m_each) - 24.0) < 1e-9
    assert abs(pricing.cost_usd("claude", "claude-opus-5", one_m_each) - 30.0) < 1e-9
    assert pricing.rates_for("gpt-4o-mini") == (0.15, 0.60)
    assert pricing.rates_for("gemini-2.0-flash") == (0.10, 0.40)
    assert pricing.rates_for("local-model") == (0.0, 0.0)
    assert pricing.rates_for("mock") == (0.0, 0.0)
    # unknown -> conservative opus-tier default (over-count, safe direction)
    assert pricing.rates_for("totally-unknown-model") == (5.0, 25.0)
    assert pricing.rates_for(None) == (5.0, 25.0)
    # #360: Gemini Pro rows are NOT priced at flash rates. Pro is tiered by ONE call's
    # prompt size (input + cache tokens): <=200k the lower rate, >200k (or unknown) higher.
    import datetime as _dt

    assert pricing.rates_for("gemini-2.5-pro") == (2.5, 15.0), "no size known -> high tier"
    assert pricing.rates_for("gemini-2.5-pro", prompt_tokens=200_000) == (1.25, 10.0)
    assert pricing.rates_for("gemini-2.5-pro", prompt_tokens=200_001) == (2.5, 15.0)
    assert pricing.rates_for("gemini-3.1-pro-preview", prompt_tokens=5_000) == (2.0, 12.0)
    assert pricing.rates_for("gemini-3.1-pro-preview") == (4.0, 18.0)
    assert pricing.rates_for("gemini-3-pro-preview", prompt_tokens=5_000) == (2.0, 12.0)
    assert pricing.rates_for("gemini-3-pro-preview") == (4.0, 18.0)
    short = {"input_tokens": 100_000, "output_tokens": 100_000}
    assert abs(pricing.cost_usd("gemini", "gemini-3.1-pro-preview", short) - 1.4) < 1e-9
    big = {"input_tokens": 150_000, "cache_read_tokens": 60_000, "output_tokens": 0}
    # 210k prompt tokens -> >200k tier: 150k*4 + 60k*4*0.1 = 0.6 + 0.024
    assert abs(pricing.cost_usd("gemini", "gemini-3.1-pro-preview", big) - 0.624) < 1e-9
    assert pricing.rates_for("gemini-2.5-flash-lite") == (0.10, 0.40)
    assert pricing.rates_for("gemini-2.5-flash") == (0.30, 2.50)
    assert pricing.rates_for("gemini-3.5-flash") == (1.5, 9.0)
    assert pricing.rates_for("gemini-3.1-flash-lite") == (0.25, 1.50)
    assert pricing.rates_for("gemini-3-flash-preview") == (0.50, 3.00)
    # 3.6/3.7/3.8 Flash: the list rate in force ON THE DAY (0.75/3.75 through 2026-12-31)
    in_promo, after = _dt.date(2026, 12, 31), _dt.date(2027, 1, 1)
    for flash in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash-preview"):
        assert pricing.rates_for(flash, on=in_promo) == (0.75, 3.75), flash
        assert pricing.rates_for(flash, on=after) == (1.5, 7.5), flash
    one_in = {"input_tokens": 1_000_000}
    assert pricing.cost_usd("gemini", "gemini-3.8-flash", one_in, on=in_promo) == 0.75
    assert pricing.cost_usd("gemini", "gemini-3.8-flash", one_in, on=after) == 1.5
    # longest-match: a TTS sibling is NOT priced at its cheaper text parent's output rate
    assert pricing.rates_for("gemini-3.8-flash-tts", on=after) == (1.0, 18.0)
    assert pricing.rates_for("gemini-unheard-of") == (4.0, 18.0), "unknown gemini = Pro >200k"
    # every gemini id the code / settings / tests actually use matches its OWN row, never
    # the bare "gemini" fallback (prefix-order safety for the 4/18 fallback)
    for gid in (
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-1.5-pro",
        "gemini-3.1-pro",
        "gemini-3.1-pro-high",
        "gemini-3.1-pro-medium",
        "gemini-3.8-flash",
        "gemini-3.8-flash-preview",
        "gemini-3.5-flash-lite",
        "models/gemini-2.5-flash",
    ):
        assert pricing.rates_for(gid, prompt_tokens=1, on=in_promo)[0] < 4.0, gid
    for pro in ("gemini-2.5-pro", "gemini-3-pro-preview", "gemini-3.1-pro-preview"):
        assert pricing.rates_for(pro)[0] > pricing.rates_for("gemini-2.0-flash")[0]
    # #360 gate: OpenAI / Cohere rows from their published lists; catch-alls not below list
    assert pricing.rates_for("gpt-5.5-pro") == (30.0, 180.0)
    assert pricing.rates_for("gpt-4.1") == (2.0, 8.0) and pricing.rates_for("gpt-4o") == (2.5, 10.0)
    assert pricing.rates_for("gpt-4-0613") == (30.0, 60.0)
    assert pricing.rates_for("gpt-6-astra")[1] <= pricing.rates_for("gpt-7-unknown")[1]
    assert pricing.rates_for("o1-pro") == (150.0, 600.0)
    assert pricing.rates_for("command-r-plus-04-2024") == (3.0, 15.0)
    assert pricing.rates_for("command-a-03-2025") == (3.0, 15.0), "unlisted command = highest"
    # #360: free is an EXACT-id allowlist, never a substring
    for fake in ("acme-local-pro", "gpt-4o-local", "mock-gpt-5", "my-mockingbird-xl", "LOCAL-70b"):
        assert pricing.rates_for(fake) > (0.0, 0.0), f"{fake} must not price at 0"
        assert pricing.cost_usd("engine:3", fake, {"input_tokens": 1_000_000}) > 0.0, fake
    assert pricing.rates_for("claude-local-test") == (5.0, 25.0)
    assert pricing.rates_for("MOCK") == (0.0, 0.0) and pricing.rates_for("local") == (0.0, 0.0)
    # real local engines: exempt instance (record_provider) + report fallback ids stay $0
    assert pricing.cost_usd("local", "local-model", {"input_tokens": 9_999_999}) == 0.0
    # a public Local endpoint passes model None -> conservative default, not free
    assert pricing.cost_usd("local", None, {"input_tokens": 1_000_000}) > 0.0
    print("  pricing: per-model rates + unknown -> conservative default; local/mock=0")

    # cost: 1M input on opus = $5.00; 1M output = $25.00
    assert (
        abs(pricing.cost_usd("claude", "claude-opus-4-8", {"input_tokens": 1_000_000}) - 5.0) < 1e-9
    )
    assert (
        abs(pricing.cost_usd("claude", "claude-opus-4-8", {"output_tokens": 1_000_000}) - 25.0)
        < 1e-9
    )
    # cache read is 0.1x input; write is 1.25x input
    assert (
        abs(pricing.cost_usd("claude", "claude-opus-4-8", {"cache_read_tokens": 1_000_000}) - 0.5)
        < 1e-9
    )
    assert (
        abs(pricing.cost_usd("claude", "claude-opus-4-8", {"cache_write_tokens": 1_000_000}) - 6.25)
        < 1e-9
    )
    # mixed
    mixed = pricing.cost_usd(
        "claude",
        "claude-haiku-4-5",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
            "cache_write_tokens": 1_000_000,
        },
    )
    # haiku 1/5: 1 + 5 + 1*0.1 + 1*1.25 = 7.35
    assert abs(mixed - 7.35) < 1e-9, mixed
    # no usage / mock model -> 0
    assert pricing.cost_usd("mock", "mock", None) == 0.0
    assert pricing.cost_usd("local", "local-model", {"input_tokens": 9_999_999}) == 0.0
    print("  pricing: cache read 0.1x / write 1.25x of input; mixed sums correctly")


def test_record_usage_accumulates():
    """record_usage adds real cost per period; zero-cost is a no-op."""
    _reset_ledger()
    assert spend_svc.spent_this_period() == 0.0
    added = spend_svc.record_usage("claude", "claude-opus-4-8", {"input_tokens": 1_000_000})
    assert abs(added - 5.0) < 1e-9, added
    assert abs(spend_svc.spent_this_period() - 5.0) < 1e-9
    # a second call accrues on top
    spend_svc.record_usage("claude", "claude-haiku-4-5", {"output_tokens": 1_000_000})
    assert abs(spend_svc.spent_this_period() - 10.0) < 1e-9  # 5 + 5
    # mock/local + no-usage never move the ledger
    assert spend_svc.record_usage("mock", "mock", {"input_tokens": 9_999_999}) == 0.0
    assert spend_svc.record_usage("local", "local-model", {"input_tokens": 9_999_999}) == 0.0
    assert spend_svc.record_usage("claude", "claude-opus-4-8", None) == 0.0
    assert abs(spend_svc.spent_this_period() - 10.0) < 1e-9, "zero-cost must not accrue"
    print("  ledger: record_usage accumulates per period; mock/local/no-usage are no-ops")


def test_would_block_and_cap_state():
    """would_block reflects (cap on AND spent >= cap); off => never blocked."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})
    spend_svc.record_usage("claude", "claude-opus-4-8", {"input_tokens": 2_000_000})  # $10
    assert spend_svc.spent_this_period() >= 10.0
    blocked, spent, cap = spend_svc.would_block()
    assert blocked is False and cap == 0.0, (blocked, cap)  # off -> never blocked
    settings.set_many({"spend_cap_usd": "20"})
    blocked, spent, cap = spend_svc.would_block()
    assert blocked is False and cap == 20.0, (blocked, spent, cap)  # 10 < 20
    settings.set_many({"spend_cap_usd": "5"})
    blocked, spent, cap = spend_svc.would_block()
    assert blocked is True and abs(spent - 10.0) < 1e-9 and cap == 5.0, (blocked, spent, cap)
    assert "Spend cap reached" in spend_svc.cap_message(spent, cap)
    print("  would_block: off=never; under cap allowed; at/over cap blocked")


def test_spend_meter_states(client):
    """The DESIGN.md §5 spend-cap meter (viz quick-win): honest states across
    uncapped / under-cap / at-cap, on setup_ai.html (the full module: title,
    figure, 9px track + amber cap tick, legend) and the compact always-danger
    meter in the base.html app-shell banner (which only ever renders once
    tripped)."""
    _reset_ledger()
    # --- uncapped (0 = off): figure alone, no meter track, existing "cap off" badge ---
    settings.set_many({"spend_cap_usd": "0"})
    r = client.get("/setup-ai")
    assert r.status_code == 200
    assert "AI translation spend" in r.text
    assert 'class="spend-meter-track"' not in r.text, "no meter track when uncapped"
    assert '<span class="badge muted-badge">cap off</span>' in r.text
    home = client.get("/")
    assert 'class="warnbar spendbar"' not in home.text, "never tripped while uncapped"

    # --- under cap: proportional fill, tick at the cap threshold, 'under cap' badge ---
    settings.set_many({"spend_cap_usd": "20"})
    spend_svc.record_usage("claude", "claude-opus-4-8", {"input_tokens": 1_000_000})  # $5
    state = spend_svc.meter_state()
    assert state["cap"] == 20.0 and abs(state["spent"] - 5.0) < 1e-9
    assert 0 < state["fill_pct"] < 100 and state["tick_pct"] == 100, state
    r = client.get("/setup-ai")
    assert 'class="spend-meter-track"' in r.text
    assert f'style="width:{state["fill_pct"]}%"' in r.text, r.text
    assert '<span class="spend-meter-tick" style="left:100%"' in r.text
    assert '<span class="badge exact">under cap</span>' in r.text
    assert "spend-danger" not in r.text, "under cap must not use the danger fill"
    home = client.get("/")
    assert 'class="warnbar spendbar"' not in home.text, "not tripped -> no banner"

    # --- at/over cap: fill flips to the existing danger convention, 'cap reached'
    # badge, and the app-shell banner appears with its compact always-danger meter ---
    settings.set_many({"spend_cap_usd": "5"})
    state = spend_svc.meter_state()
    assert state["tripped"] is True and state["fill_pct"] == 100, state
    r = client.get("/setup-ai")
    assert 'class="prog-bar spend-danger"' in r.text
    assert '<span class="badge warn">cap reached</span>' in r.text
    home = client.get("/")
    assert 'class="warnbar spendbar"' in home.text
    banner = home.text[home.text.index('class="warnbar spendbar"') :]
    assert 'class="prog"' in banner[:400] and 'style="width:100%"' in banner[:400], banner[:400]
    settings.set_many({"spend_cap_usd": "0"})
    print(
        "  meter: uncapped/under-cap/at-cap states render the correct markup + "
        "numbers on setup_ai.html; the app-shell banner meter appears only once "
        "tripped OK"
    )


def test_meter_fill_never_full_unless_tripped(client):
    """REGRESSION GUARD (reviewer nit): meter_state() used round() for fill_pct,
    so cap=$100/spent=$99.60 rendered a FULL (100%) bar right next to an "under
    cap" badge — a full bar must mean exactly one thing (tripped), never "close
    enough that round() tipped it over". Fix floors fill_pct and clamps it to 99
    while under cap; pct_used (the NUMBER, not the bar) is still allowed to read
    "100%" from honest rounding — the reviewer's issue was the BAR, not the text."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "100"})
    with Session(engine) as s:
        row = s.get(SpendLedger, spend_svc.period_key())
        if row is None:
            row = SpendLedger(period=spend_svc.period_key(), accumulated_usd=0.0)
        row.accumulated_usd = 99.6
        s.add(row)
        s.commit()
    state = spend_svc.meter_state()
    assert state["tripped"] is False, state  # $99.60 < $100 cap
    assert state["pct_used"] == 100, state  # honest text rounding is fine
    assert state["fill_pct"] == 99, (
        "a near-but-under-cap spend must floor/clamp the BAR below 100%, "
        "not round it up to a full bar",
        state,
    )
    r = client.get("/setup-ai")
    assert r.status_code == 200
    assert 'style="width:99%"' in r.text, r.text
    assert 'style="width:100%"' not in r.text, (
        "the fill must never render as a full bar while under cap"
    )
    assert '<span class="badge exact">under cap</span>' in r.text
    assert "spend-danger" not in r.text
    # exactly AT the cap (spent==cap): tripped, fill IS legitimately full (100%,
    # no clamp) — the full bar means exactly this state and nothing else.
    with Session(engine) as s:
        row = s.get(SpendLedger, spend_svc.period_key())
        row.accumulated_usd = 100.0
        s.add(row)
        s.commit()
    state = spend_svc.meter_state()
    assert state["tripped"] is True and state["fill_pct"] == 100, state
    settings.set_many({"spend_cap_usd": "0"})
    print(
        "  meter: fill_pct floors+clamps below 100 while under cap (a $99.60/"
        "$100 spend shows a 99% bar, never a misleading full bar next to "
        "'under cap'); exactly at the cap the full bar is legitimate (tripped) OK"
    )


def test_enforcement_blocks_real_provider_not_mock():
    """Cap on + over: a REAL provider is blocked (terminal error, not called);
    the MockProvider is EXEMPT and still runs."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0.0001"})  # tiny cap
    spend_svc.record_usage("claude", "claude-opus-4-8", {"input_tokens": 1_000_000})  # $5 >> cap
    assert spend_svc.would_block()[0] is True

    # 'claude' has no key -> get_provider() returns MockProvider -> EXEMPT -> succeeds.
    res = run_completion(
        ProviderSpec(provider="claude"),
        "Translate: hi",
        task="translate",
        payload={"text": "hi", "target": "fr"},
    )
    assert res["error"] is None and res["text"], ("mock must be exempt", res)
    print("  enforce: MockProvider exempt (offline install never trips)")

    # A LIVE provider IS blocked: terminal error, provider never called, no failover.
    LiveStub.calls = 0
    register_provider("claude", LiveStub())
    try:
        res = run_completion(
            ProviderSpec(provider="claude"),
            "Translate: hi",
            task="translate",
            payload={"text": "hi", "target": "fr"},
        )
        assert res["error"] and "Spend cap reached" in res["error"], res
        assert res["text"] == "" and res["provider_used"] is None, res
        assert LiveStub.calls == 0, ("blocked call must not reach the provider", LiveStub.calls)
        print("  enforce: live provider BLOCKED at cap -> terminal error, never called")

        # Failover target is also blocked (a real backup can't sneak past the cap).
        LiveStub.calls = 0
        register_provider("gemini", LiveStub())
        spec = ProviderSpec(provider="claude", fallback_provider="gemini")
        res = run_completion(
            spec, "Translate: hi", task="translate", payload={"text": "hi", "target": "fr"}
        )
        assert res["error"] and "Spend cap reached" in res["error"], res
        assert LiveStub.calls == 0, ("failover must also be blocked", LiveStub.calls)
        _prov._OVERRIDES.pop("gemini", None)
        print("  enforce: failover target also blocked when over cap")

        # Raise the cap well above spend -> the live provider runs + records.
        settings.set_many({"spend_cap_usd": "1000000"})
        LiveStub.calls = 0
        before = spend_svc.spent_this_period()
        res = run_completion(
            ProviderSpec(provider="claude"),
            "Translate: hi",
            task="translate",
            payload={"text": "hi", "target": "fr"},
        )
        assert res["error"] is None and res["text"] == "[fr] hi", res
        assert LiveStub.calls == 1, LiveStub.calls
        assert spend_svc.spent_this_period() > before, "under-cap live call must accrue spend"
        print("  enforce: under cap -> live provider runs and spend is recorded")
    finally:
        _prov._OVERRIDES.pop("claude", None)
        _prov._OVERRIDES.pop("gemini", None)
        settings.set_many({"spend_cap_usd": "0"})


def test_direct_call_sites_enforced_and_recorded():
    """The direct provider.complete() sites (glossary sense-select / term-mine, the
    workflow author-prompt) get the same brake via spend.blocked_for/record_provider:
    a REAL engine over cap is blocked WITHOUT calling; the mock is exempt; an
    under-cap real call accrues."""
    from app.services import orchestrator

    _reset_ledger()

    # is_exempt is decided on the INSTANCE: mock exempt, LiveStub not.
    assert spend_svc.is_exempt(_prov.MockProvider()) is True
    assert spend_svc.is_exempt(LiveStub()) is False

    # Over cap: blocked_for a REAL provider returns the terminal message; the mock
    # returns None (exempt) even at a tiny cap.
    settings.set_many({"spend_cap_usd": "0.0001"})
    spend_svc.record_usage("claude", "claude-opus-4-8", {"input_tokens": 1_000_000})  # $5
    assert spend_svc.would_block()[0] is True
    assert spend_svc.blocked_for(_prov.MockProvider()) is None
    msg = spend_svc.blocked_for(LiveStub())
    assert msg and "Spend cap reached" in msg, msg

    # author_prompt: over cap on a REAL engine returns the terminal msg, never calls.
    LiveStub.calls = 0
    register_provider("claude", LiveStub())
    try:
        out = orchestrator.author_prompt("translate politely", provider="claude")
        assert "Spend cap reached" in out and LiveStub.calls == 0, (out, LiveStub.calls)

        # Raise the cap: author_prompt now calls the live provider and records spend.
        settings.set_many({"spend_cap_usd": "1000000"})
        LiveStub.calls = 0
        before = spend_svc.spent_this_period()
        out = orchestrator.author_prompt("translate politely", provider="claude")
        assert out == "[fr] hi" and LiveStub.calls == 1, (out, LiveStub.calls)
        assert spend_svc.spent_this_period() > before, "author_prompt spend must accrue"
    finally:
        _prov._OVERRIDES.pop("claude", None)
        settings.set_many({"spend_cap_usd": "0"})
    print("  bypass: glossary/author-prompt direct calls enforced + recorded (mock exempt)")


def test_local_provider_exempt():
    """QA-4: exemption is decided on the resolved provider INSTANCE KIND. A self-hosted
    LocalProvider spends no real money and must be EXEMPT (price 0 / never accrue) even
    when it reports its REAL model id (e.g. `meta-llama-3-8b-instruct`, which matches no
    _PRICES key and would otherwise fall to the opus-tier default and accrue phantom
    spend -- the direct trigger for the QA-1 ledger-write deadlock). MockProvider stays
    exempt. CLOUD engines (openai_compatible / deepl / a plain keyed provider) stay
    NON-exempt and keep their real/conservative pricing -- no over-exemption."""
    _reset_ledger()

    # Exemption on the INSTANCE KIND: mock + self-hosted local are free. A self-hosted slot needs a
    # configured private URL: the shipped default is EMPTY (2026-10-06), and an unconfigured slot
    # stays non-free (fail closed; it cannot make a call anyway).
    from app import config as _cfg_spend

    _saved_local = _cfg_spend.LOCAL_BASE_URL
    try:
        _cfg_spend.LOCAL_BASE_URL = ""
        unconfigured_free = spend_svc.is_exempt(LocalProvider())
        assert unconfigured_free is False, "unconfigured local is not proven free"
        _cfg_spend.LOCAL_BASE_URL = "http://192.168.0.5:1234"
        _exempt_local()
    finally:
        _cfg_spend.LOCAL_BASE_URL = _saved_local


def _exempt_local():
    assert spend_svc.is_exempt(LocalProvider()) is True, "self-hosted local must be exempt"
    assert spend_svc.is_exempt(MockProvider()) is True, "mock stays exempt"
    # NOT over-exempted: a cloud openai_compatible host (BYO bearer key -> OpenRouter/
    # DeepSeek/etc.) and the cloud NMT engines keep their pricing.
    assert spend_svc.is_exempt(OpenAICompatibleProvider()) is False, (
        "openai_compatible is a CLOUD host (real money) -> must NOT be exempt"
    )
    assert spend_svc.is_exempt(DeepLProvider()) is False, "cloud NMT keeps pricing"
    assert spend_svc.is_exempt(LiveStub()) is False, "a plain keyed provider is not exempt"

    # A LocalProvider reporting a REAL local model id accrues NOTHING (record_provider
    # short-circuits on the exempt instance -- pre-fix it priced 30.0 at the opus default).
    lp = LocalProvider()
    lp.last_usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    before = spend_svc.spent_this_period()
    added = spend_svc.record_provider(lp, "local", "meta-llama-3-8b-instruct")
    assert added == 0.0, ("a local engine must accrue 0 regardless of model id", added)
    assert spend_svc.spent_this_period() == before, "local must not move the ledger"

    # A CLOUD model id still prices correctly (the conservative table is untouched)...
    assert (
        abs(pricing.cost_usd("claude", "claude-opus-4-8", {"input_tokens": 1_000_000}) - 5.0) < 1e-9
    )
    # ...and an unknown openai_compatible cloud model id still OVER-counts (safe
    # direction for a kill switch), NOT silently zeroed by an over-broad exemption.
    assert pricing.cost_usd("engine:9", "deepseek-chat", {"input_tokens": 1_000_000}) > 0.0, (
        "a cloud openai_compatible model must NOT be priced 0 (no over-exemption)"
    )
    print(
        "  QA-4: LocalProvider exempt (0 accrual on a real model id); mock exempt; "
        "cloud (openai_compatible/deepl/keyed) NOT exempt -> pricing preserved"
    )


def test_audit_on_trip():
    """A trip emits exactly ONE 'spend.cap_tripped' audit row per period."""
    _reset_ledger()
    settings.set_many({"audit_enabled": "1", "spend_cap_usd": "0.0001"})
    spend_svc.record_usage("claude", "claude-opus-4-8", {"input_tokens": 1_000_000})
    before = audit.count(action="spend.cap_tripped")
    # note_trip is idempotent per period; call twice
    spend_svc.note_trip()
    spend_svc.note_trip()
    after = audit.count(action="spend.cap_tripped")
    assert after == before + 1, ("exactly one trip audit per period", before, after)
    settings.set_many({"spend_cap_usd": "0"})
    print("  audit: cap trip logs exactly once per period (forced, gate-independent)")


def test_settings_roundtrip_web_and_cli():
    """/setup-ai/spend saves the cap (web); CLI config get/set round-trips it."""
    _reset_ledger()
    # web: log in as admin (auth on), post the spend form
    security.seed_initial()
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": "admin", "password": "admin", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    csrf_for(c)
    r = c.post("/setup-ai/spend", data={"spend_cap_usd": "42.5"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    assert settings.get("spend_cap_usd") == "42.5", settings.get("spend_cap_usd")
    assert spend_svc.cap_usd() == 42.5
    # T11 (HECVAT APPL-09): a negative or non-numeric value is REFUSED with the error
    # summary and the prior cap is kept (it used to be saved as 0, turning the cap off).
    for bad in ("not-a-number", "-5", "-0.01", "nan", "1e-3", "12,5", "$50", "5 0"):
        csrf_for(c)
        r = c.post("/setup-ai/spend", data={"spend_cap_usd": bad}, follow_redirects=False)
        assert r.status_code == 303 and "spend_error=" in r.headers["location"], (bad, r.headers)
        assert settings.get("spend_cap_usd") == "42.5", (bad, settings.get("spend_cap_usd"))
    page = c.get(r.headers["location"]).text
    assert 'role="alert"' in page and "The spend cap must be an amount" in page
    # blank still means "no cap" (the documented contract), stored as "0"
    csrf_for(c)
    r = c.post("/setup-ai/spend", data={"spend_cap_usd": "  "}, follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    assert settings.get("spend_cap_usd") == "0", settings.get("spend_cap_usd")
    # the one parser: clean forms in, refusals out
    assert settings.spend_cap_value("") == "0" and settings.spend_cap_value("50.00") == "50"
    assert settings.spend_cap_value("0.0001") == "0.0001"
    # T11 gate round 1 LOW 4: an exponent is its plain amount (the docstring says so)
    assert settings.spend_cap_value("1e3") == "1000"
    assert settings.spend_cap_value("1e+06") == "1000000"
    # ...and ONE normalization: set_many (every door's chokepoint) stores the canonical form
    settings.set_many({"spend_cap_usd": " 50 "})
    assert settings.get("spend_cap_usd") == "50", settings.get("spend_cap_usd")
    settings.set_many({"spend_cap_usd": "1e3"})
    assert settings.get("spend_cap_usd") == "1000", settings.get("spend_cap_usd")
    settings.set_many({"spend_cap_usd": ""})
    assert settings.get("spend_cap_usd") == "0", settings.get("spend_cap_usd")
    for bad in ("-1", "abc", "inf", "1" + "0" * 12, ".", "1.2.3", "²"):
        try:
            settings.spend_cap_value(bad)
        except settings.InvalidSettingValue:
            continue
        raise AssertionError(f"spend_cap_value accepted {bad!r}")
    print("  web: /setup-ai/spend saves the cap; negative / non-numeric refused; blank = off")

    # setup-ai page renders the spend card with the current value + spend-to-date
    r = c.get("/setup-ai")
    assert r.status_code == 200 and "Spend cap" in r.text, r.status_code
    print("  web: Setup AI page shows the spend card")

    # CLI: config set/get spend_cap_usd (generic config surface, no new command).
    # Run as subprocesses so the CLI's in-process require_local()/arg wiring is
    # exercised exactly as a real invocation, against the SAME data dir.
    env = dict(os.environ)
    p = subprocess.run(
        [sys.executable, "-m", "app.cli", "config", "set", "spend_cap_usd", "99"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, (p.returncode, p.stderr)
    settings.invalidate_cache()
    assert settings.get("spend_cap_usd") == "99", settings.get("spend_cap_usd")
    p = subprocess.run(
        [sys.executable, "-m", "app.cli", "config", "get", "spend_cap_usd"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0 and "99" in p.stdout, (p.returncode, p.stdout, p.stderr)
    # T11: the CLI door refuses a negative / non-numeric cap and keeps the prior value.
    for bad in ("-5", "abc"):
        p = subprocess.run(
            [sys.executable, "-m", "app.cli", "config", "set", "spend_cap_usd", bad],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert p.returncode != 0 and "spend cap must be" in (p.stdout + p.stderr), (
            bad,
            p.returncode,
            p.stdout,
            p.stderr,
        )
        settings.invalidate_cache()
        assert settings.get("spend_cap_usd") == "99", (bad, settings.get("spend_cap_usd"))
    # T11 gate round 1 LOW 4: the CLI door stores the same canonical form as the web door.
    p = subprocess.run(
        [sys.executable, "-m", "app.cli", "config", "set", "spend_cap_usd", "1e2"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    settings.invalidate_cache()
    assert settings.get("spend_cap_usd") == "100", settings.get("spend_cap_usd")
    settings.set_many({"spend_cap_usd": "99"})
    print("  cli: config set refuses a negative / non-numeric spend cap; stores 1e2 as 100")
    # T11: the API door (POST /api/admin/config, which MCP set_config forwards to) too.
    admin = security.get_by_email("admin")
    _row, raw = apikeys.create_key(admin.id, "spend-cap", scopes="api docs admin")
    settings.set_many({"remote_admin_enabled": "1"})
    try:
        h = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway key minted by this suite
        for bad in ("-5", "abc"):
            r = TestClient(app).post(
                "/api/admin/config", headers=h, json={"settings": {"spend_cap_usd": bad}}
            )
            assert r.status_code == 400, (bad, r.status_code, r.text[:300])
            assert settings.get("spend_cap_usd") == "99", settings.get("spend_cap_usd")
        r = TestClient(app).post(
            "/api/admin/config", headers=h, json={"settings": {"spend_cap_usd": "12.5"}}
        )
        assert r.status_code == 200 and settings.get("spend_cap_usd") == "12.5", r.text[:300]
        # T11 gate round 1 LOW 4: the API door stores the canonical form too.
        r = TestClient(app).post(
            "/api/admin/config", headers=h, json={"settings": {"spend_cap_usd": " 12.50 "}}
        )
        assert r.status_code == 200 and settings.get("spend_cap_usd") == "12.5", (
            r.text[:300],
            settings.get("spend_cap_usd"),
        )
    finally:
        settings.set_many({"remote_admin_enabled": "0"})
    print("  api: /api/admin/config refuses a negative / non-numeric spend cap")
    settings.set_many({"spend_cap_usd": "0"})
    print("  cli: config get/set spend_cap_usd round-trips (generic surface, no new command)")

    settings.set_many({"security_auth_required": "0"})


def test_web_spend_huge_value_rejected():
    """A huge/absurdly-long spend_cap_usd string saturates to `inf` at float()
    (no exception) -- /setup-ai/spend must reject that with a clean redirect
    banner instead of silently storing "inf". A within-bound huge value still
    saves normally (no over-reject); cap_usd() also never returns a non-finite
    or out-of-range value even if one somehow reaches storage."""
    _reset_ledger()
    security.seed_initial()
    settings.set_many({"spend_cap_usd": "0", "security_auth_required": "1"})
    try:
        c = TestClient(app)
        csrf_for(c)
        r = c.post(
            "/auth/login",
            data={"email": "admin", "password": "admin", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        csrf_for(c)

        # A 400-digit string parses to +inf, not a ValueError -- must be rejected.
        huge_digits = "1" + "0" * 400
        r = c.post("/setup-ai/spend", data={"spend_cap_usd": huge_digits}, follow_redirects=False)
        assert r.status_code == 303, r.status_code
        assert "spend_error=" in r.headers["location"], r.headers
        assert settings.get("spend_cap_usd") == "0", (
            f"a rejected huge value must not overwrite the prior cap: {settings.get('spend_cap_usd')}"  # noqa: E501
        )

        # Literal "inf" (a plausible copy-paste) is equally rejected.
        csrf_for(c)
        r = c.post("/setup-ai/spend", data={"spend_cap_usd": "inf"}, follow_redirects=False)
        assert r.status_code == 303 and "spend_error=" in r.headers["location"], r.headers
        assert settings.get("spend_cap_usd") == "0"

        # A within-bound huge cap (e.g. $1,000,000) still saves normally.
        csrf_for(c)
        r = c.post("/setup-ai/spend", data={"spend_cap_usd": "1000000"}, follow_redirects=False)
        assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
        assert settings.get("spend_cap_usd") == "1000000", settings.get("spend_cap_usd")
        assert spend_svc.cap_usd() == 1000000.0

        # T11 gate round 1 LOW 4: set_many itself (every door's chokepoint) now refuses
        # it, so nothing reaches storage that way.
        try:
            settings.set_many({"spend_cap_usd": "inf"})
        except settings.InvalidSettingValue:
            pass
        else:
            raise AssertionError("set_many stored a non-finite spend cap")
        assert settings.get("spend_cap_usd") == "1000000", settings.get("spend_cap_usd")
        # Defense-in-depth: if a non-finite value ever reaches storage another way (an
        # older install's row, a hand-edited database), cap_usd() must not arm it as a
        # live cap. Simulated by the stored value the reader sees.
        real_get = settings.get
        settings.get = lambda k, *a, **kw: "inf" if k == "spend_cap_usd" else real_get(k, *a, **kw)
        try:
            assert spend_svc.cap_usd() == 0.0, "cap_usd() must treat a stored inf as OFF"
        finally:
            settings.get = real_get
        settings.set_many({"spend_cap_usd": "0"})
        print(
            "  spend cap: huge/inf value -> clean redirect error, prior cap preserved; "
            "a within-bound huge cap still saves; cap_usd() never returns non-finite"
        )
    finally:
        settings.set_many({"spend_cap_usd": "0", "security_auth_required": "0"})


def test_cli_health_spend_line():
    """`health` prints a spend status line (no new EXPECTED_COMMANDS entry)."""
    _reset_ledger()
    settings.set_many({"spend_cap_usd": "0"})
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "health"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert "spend" in proc.stdout.lower(), proc.stdout
    print("  cli: `health` includes a spend status line")


def test_estimate_resolver_is_shared():
    from unittest.mock import patch

    from app.services import providers_common

    usage = {"input_tokens": 1000, "output_tokens": 1000}
    # Simulate adding a built-in at the resolver alone. Estimates must discover it
    # without a second provider-name list being edited.
    with patch.object(providers_common, "_effective_model", return_value="gpt-5.2-pro") as resolve:
        cost = spend_svc.cost_for_ref("future_builtin", None, usage)
    resolve.assert_called_once()
    assert cost == pricing.cost_usd("future_builtin", "gpt-5.2-pro", usage)
    assert (
        providers_common._effective_model(type("Unknown", (), {"name": "unknown"})(), None) is None
    )


def main():
    init_db()
    settings.set_many({"spend_cap_usd": "0", "audit_enabled": "1"})
    test_pricing_math()
    test_estimate_resolver_is_shared()
    test_record_usage_accumulates()
    test_would_block_and_cap_state()
    test_spend_meter_states(TestClient(app))
    test_meter_fill_never_full_unless_tripped(TestClient(app))
    test_enforcement_blocks_real_provider_not_mock()
    test_direct_call_sites_enforced_and_recorded()
    test_local_provider_exempt()
    test_audit_on_trip()
    test_settings_roundtrip_web_and_cli()
    test_web_spend_huge_value_rejected()
    test_cli_health_spend_line()
    from tests.spend_accounting_cases import run

    run()
    from tests.free_engine_pricing_cases import run as run_free_engine_pricing

    run_free_engine_pricing()
    from tests.job_exact_cost_cases import run as run_job_exact_cost

    run_job_exact_cost()
    from tests.inherited_model_pricing_cases import run as run_inherited_model_pricing

    run_inherited_model_pricing()
    settings.set_many({"spend_cap_usd": "0", "security_auth_required": "0"})
    print("SPEND CAP OK")


if __name__ == "__main__":
    main()
