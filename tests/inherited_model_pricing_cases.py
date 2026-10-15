"""Offline security regressions for GitHub #373.

A workflow/authoring call may leave its model BLANK to inherit the engine profile's
model. The adapter resolves the real model for the HTTP request, but the OLD spend
accounting booked the BLANK model -- which prices at the conservative fallback rate
(_DEFAULT 5/25 -> $0.030 per 1000 in + 1000 out) -- so an expensive inherited profile
(a gpt-5.2-pro profile at 21/168 -> $0.189) out-ran the install/project cap while the
ledger under-reported. Every case here drives the REAL openai_compatible provider over
an httpx.MockTransport (no mock substitution, no weakened guard) and asserts the
inherited-model charge equals the explicit-model charge and equals the table rate.
"""

import json
from unittest.mock import patch

import httpx

from app import config
from app.services import engines, settings, spend
from app.services.providers import (
    _OVERRIDES,
    LocalProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderSpec,
    ProviderTruncated,
    register_provider,
    run_completion,
)

_MODEL = "gpt-5.2-pro"  # configured (21.0, 168.0): $0.189 per 1000 in + 1000 out
_EXPECTED = 0.189
_BASE = "https://price373.test/v1"


def _transport(captured, *, finish="stop", content="ok"):
    """Capture the WIRE model; return 1000-in/1000-out usage. `finish`/`content` let a
    case reproduce the thinking-burn shape (finish=length + empty content) that raises
    AFTER last_usage was recorded, so a FAILED attempt's spend is what is asserted."""

    def h(req):
        captured["model"] = json.loads(req.content).get("model")
        body = {
            "choices": [
                {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
            ],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
        }
        return httpx.Response(200, json=body)

    return httpx.MockTransport(h)


def _profile(name):
    return engines.create_profile(
        name, "openai_compatible", base_url=_BASE, api_key="sk-price373", model=_MODEL
    )


def _provider(pid, captured, **kw):
    transport = _transport(captured, **kw)
    return OpenAICompatibleProvider(profile=engines.get_profile(pid), transport=transport)


def _book(runner):
    """Run `runner()` and return (result, ledger_delta)."""
    before = spend.spent_this_period()
    res = runner()
    return res, spend.spent_this_period() - before


def adapter_default_parity():
    """Blank adapter defaults match explicit models on both funnels, even on failure."""
    from openai import OpenAI

    prof = engines.create_profile("price373-blank", "openai_compatible", base_url=_BASE, model="")
    try:
        for builtin in (True, False):
            expected_model, expected_cost = (
                (_MODEL, _EXPECTED) if builtin else ("gpt-3.5-turbo", 0.002)
            )
            for direct in (True, False):
                for failed in (False, True):
                    charges = []
                    for explicit in (False, True):
                        captured = {}
                        finish = "length" if failed else "stop"
                        transport = _transport(captured, finish=finish)

                        def sse(req, captured=captured, model=expected_model, finish=finish):
                            captured["model"] = json.loads(req.content)["model"]
                            chunk = {
                                "id": "offline",
                                "object": "chat.completion.chunk",
                                "created": 0,
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": "ok"},
                                        "finish_reason": finish,
                                    }
                                ],
                                "usage": {
                                    "prompt_tokens": 1000,
                                    "completion_tokens": 1000,
                                    "total_tokens": 2000,
                                },
                            }
                            return httpx.Response(
                                200,
                                headers={"content-type": "text/event-stream"},
                                text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                            )

                        with httpx.Client(transport=httpx.MockTransport(sse)) as client:
                            sdk = OpenAI(api_key="offline", http_client=client)
                            prov = (
                                OpenAIProvider()
                                if builtin
                                else OpenAICompatibleProvider(profile=prof, transport=transport)
                            )
                            ref = "openai" if builtin else f"engine:{prof.id}"
                            register_provider(ref, prov)
                            try:
                                with (
                                    patch.dict(config.DEFAULT_MODELS, {"openai": _MODEL}),
                                    patch("openai.OpenAI", return_value=sdk),
                                ):
                                    before = spend.spent_this_period()
                                    kwargs = {"model": expected_model} if explicit else {}
                                    if direct:
                                        try:
                                            spend.complete_direct(prov, "Translate: hi", **kwargs)
                                        except ProviderTruncated:
                                            assert failed
                                        else:
                                            assert not failed
                                    else:
                                        res = run_completion(
                                            ProviderSpec(ref, retry_max=0, **kwargs),
                                            "Translate: hi",
                                        )
                                        assert bool(res["error"]) == failed, res
                                        assert res["model_used"] == expected_model, res
                                    delta = spend.spent_this_period() - before
                                    assert captured["model"] == expected_model, captured
                                    assert abs(delta - expected_cost) < 1e-9, (
                                        builtin,
                                        direct,
                                        failed,
                                        explicit,
                                        delta,
                                    )
                                    charges.append(delta)
                            finally:
                                _OVERRIDES.pop(ref, None)
                    assert abs(charges[0] - charges[1]) < 1e-9, charges
    finally:
        engines.delete_profile(prof.id)


def inherited_matches_explicit():
    """The core fix + parity: a blank (inherited) spec model and the SAME explicitly-named
    model send the same wire model and book the identical, table-correct charge."""
    settings.set_many({"spend_cap_usd": "0"})
    prof = _profile("price373-parity")
    ref = f"engine:{prof.id}"
    try:
        cap_in, cap_ex = {}, {}
        register_provider(ref, _provider(prof.id, cap_in))
        res_in, delta_in = _book(
            lambda: run_completion(ProviderSpec(ref, model=None), "Translate: hi")
        )
        _OVERRIDES.pop(ref, None)

        register_provider(ref, _provider(prof.id, cap_ex))
        res_ex, delta_ex = _book(
            lambda: run_completion(ProviderSpec(ref, model=_MODEL), "Translate: hi")
        )
        _OVERRIDES.pop(ref, None)

        assert res_in["error"] is None and res_ex["error"] is None, (res_in, res_ex)
        # Wire identity and the returned metadata first, so a build that fixes the ledger but
        # not model_used fails HERE (the charge asserts below would mask it).
        assert cap_in["model"] == _MODEL == cap_ex["model"], ("wire model drift", cap_in, cap_ex)
        assert res_in["model_used"] == _MODEL, ("model_used must name the resolved model", res_in)
        assert res_ex["model_used"] == _MODEL, res_ex
        assert abs(delta_in - _EXPECTED) < 1e-9, ("inherited priced on the blank model", delta_in)
        assert abs(delta_ex - _EXPECTED) < 1e-9, ("explicit priced wrong", delta_ex)
        assert abs(delta_in - delta_ex) < 1e-9, ("inherited != explicit charge", delta_in, delta_ex)
    finally:
        _OVERRIDES.pop(ref, None)
        engines.delete_profile(prof.id)


def cap_admits_fewer_at_true_rate():
    """Two inherited calls under $0.10 of cap headroom: at the true $0.189 rate the first
    trips the cap and the second is BLOCKED (not silently admitted at the cheap blank rate)."""
    settings.set_many({"spend_cap_usd": "0"})
    prof = _profile("price373-cap")
    ref = f"engine:{prof.id}"
    try:
        headroom = spend.spent_this_period() + 0.10
        settings.set_many({"spend_cap_usd": str(headroom)})
        cap = {}
        register_provider(ref, _provider(prof.id, cap))
        try:
            res1, delta1 = _book(
                lambda: run_completion(ProviderSpec(ref, model=None), "Translate: hi")
            )
            assert res1["error"] is None, res1
            assert abs(delta1 - _EXPECTED) < 1e-9, ("first call", delta1)
            res2, delta2 = _book(
                lambda: run_completion(ProviderSpec(ref, model=None), "Translate: hi")
            )
            assert res2["error"] and "Spend cap" in res2["error"], ("second call blocked", res2)
            assert delta2 == 0.0, ("a blocked call must not book", delta2)
        finally:
            _OVERRIDES.pop(ref, None)
            settings.set_many({"spend_cap_usd": "0"})
    finally:
        engines.delete_profile(prof.id)


def failed_attempt_prices_resolved_model():
    """A FAILED attempt (a thinking-burn: finish=length + empty content, raised AFTER
    last_usage was recorded) is booked on the resolved model, not the blank one."""
    settings.set_many({"spend_cap_usd": "0"})
    prof = _profile("price373-fail")
    ref = f"engine:{prof.id}"
    try:
        cap = {}
        register_provider(ref, _provider(prof.id, cap, finish="length", content=""))
        try:
            spec = ProviderSpec(ref, model=None, retry_max=0, backoff_base=0)
            res, delta = _book(lambda: run_completion(spec, "Translate: hi"))
            assert res["error"], ("expected the burn to fail the call", res)
            assert cap["model"] == _MODEL, cap
            assert abs(delta - _EXPECTED) < 1e-9, ("failed attempt priced blank", delta)
        finally:
            _OVERRIDES.pop(ref, None)
    finally:
        engines.delete_profile(prof.id)


def direct_call_prices_resolved_model():
    """complete_direct (glossary/author-prompt bypass) books an inherited model at the
    resolved rate, same as run_completion."""
    settings.set_many({"spend_cap_usd": "0"})
    prof = _profile("price373-direct")
    try:
        cap = {}
        prov = _provider(prof.id, cap)
        before = spend.spent_this_period()
        spend.complete_direct(prov, "Translate: hi", model=None)
        delta = spend.spent_this_period() - before
        assert cap["model"] == _MODEL, cap
        assert abs(delta - _EXPECTED) < 1e-9, ("direct call priced on the blank model", delta)
    finally:
        engines.delete_profile(prof.id)


def local_exemption_preserved():
    """A proven-private LocalProvider still accrues 0 regardless of the model id it
    reports, and billable_model still short-circuits it to the blank rate (the private-local
    exemption is preserved by the fix)."""
    settings.set_many({"spend_cap_usd": "0"})
    lp = LocalProvider()
    lp._profile_base = "http://127.0.0.1/v1"  # proven private -> exempt
    lp.last_usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert spend.billable_model(lp, _MODEL) is None, "local must bill on the blank model"
    before = spend.spent_this_period()
    added = spend.record_provider(lp, "local", _MODEL)
    assert added == 0.0, "a private local engine must accrue 0"
    assert spend.spent_this_period() == before, "local must not move the ledger"


def workflow_step_blank_model_prices_profile_rate():
    """The shape the issue actually reproduces: a workflow step that names the ENGINE and
    leaves its model blank. `_spec_for_step` yields model=None (DEFAULT_MODELS has no
    engine:<id> entry), so the completion that step drives must still book the profile's
    table rate, not the blank-model fallback."""
    from app.models import WorkflowStep
    from app.services.orchestrator_part2 import _spec_for_step

    settings.set_many({"spend_cap_usd": "0"})
    prof = _profile("price373-step")
    ref = f"engine:{prof.id}"
    try:
        spec = _spec_for_step(WorkflowStep(workflow_id=1, provider=ref, model=None))
        assert spec.model is None, ("the step shape must yield a BLANK spec model", spec)
        cap = {}
        register_provider(ref, _provider(prof.id, cap))
        try:
            res, delta = _book(lambda: run_completion(spec, "Translate: hi"))
            assert res["error"] is None, res
            assert cap["model"] == _MODEL, ("wire model drift", cap)
            assert res["model_used"] == _MODEL, res
            assert abs(delta - _EXPECTED) < 1e-9, ("workflow step priced on the blank", delta)
        finally:
            _OVERRIDES.pop(ref, None)
    finally:
        engines.delete_profile(prof.id)


def explicit_cheaper_model_beats_pricey_profile():
    """Resolution ORDER guard: the profile model is a FALLBACK, never an override. A step
    that explicitly names a cheap model on an expensive profile must send and book the
    CHEAP one (gpt-5-nano: 0.05/0.40 per 1M -> 0.00045 for this usage)."""
    settings.set_many({"spend_cap_usd": "0"})
    cheap, cheap_cost = "gpt-5-nano", 0.00045
    prof = _profile("price373-cheaper")
    ref = f"engine:{prof.id}"
    try:
        cap = {}
        register_provider(ref, _provider(prof.id, cap))
        try:
            res, delta = _book(
                lambda: run_completion(ProviderSpec(ref, model=cheap), "Translate: hi")
            )
            assert res["error"] is None, res
            assert cap["model"] == cheap, ("the profile model overrode the explicit one", cap)
            assert res["model_used"] == cheap, res
            assert abs(delta - cheap_cost) < 1e-6, ("booked the profile rate", delta)
        finally:
            _OVERRIDES.pop(ref, None)
    finally:
        engines.delete_profile(prof.id)


CASES = (
    adapter_default_parity,
    inherited_matches_explicit,
    cap_admits_fewer_at_true_rate,
    failed_attempt_prices_resolved_model,
    direct_call_prices_resolved_model,
    workflow_step_blank_model_prices_profile_rate,
    explicit_cheaper_model_beats_pricey_profile,
    local_exemption_preserved,
)


def run():
    failures = []
    for case in CASES:
        settings.set_many({"spend_cap_usd": "0"})
        try:
            case()
            print(f"  #373 inherited-model pricing {case.__name__}: PASS")
        except Exception as exc:
            import traceback

            failures.append(case.__name__)
            print(f"  #373 inherited-model pricing {case.__name__}: FAIL {exc}")
            traceback.print_exc()
    settings.set_many({"spend_cap_usd": "0"})
    assert not failures, failures
