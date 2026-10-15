"""Output-token BUDGET + escalate-on-truncation verification (production bug).

The owner hit, on a real install, a FULL_TEXT translation with `claude` that failed
EVERY step with "ProviderTruncated: claude hit max_tokens (670) - output truncated",
where 670 was ~ the request's real input-token count. Root cause:

  * providers._sized_max_tokens sized max_tokens from a chars/4 estimate with only a
    1.5x pad and a 256 floor. On punctuation-/markup-heavy text the chars/4 estimate
    UNDERCOUNTS the real tokenizer count, so the "1.5x headroom" collapsed to ~1.0x of
    the real input — leaving no room for the target-language expansion, so the model
    truncated.
  * the retry path in run_completion re-derived the SAME budget every attempt, so
    every retry truncated identically and the step died after retry_max.

This suite proves the fix, all OFFLINE (no network, throwaway DATA_DIR, blank keys):

  (a) the new budget = min(ceiling, max(FLOOR>=2048, input*MULT>=3)) — generous, so a
      markup-heavy input whose char-estimate undercounts is NOT starved; clamped to
      the model ceiling.
  (b) escalate-on-truncation: a first call that truncates is RETRIED with a LARGER
      budget (not the identical one) and succeeds.
  (c) an explicit-budget caller (the authoring contract) is byte-identical: its budget
      is honored verbatim and a DIRECT complete() call never escalates.
  (d) a truncation that persists even AT the model ceiling fails HONESTLY with a clear
      "exceeds model ceiling" message (the guard is never weakened).
  (e) per-provider semantics unchanged: openai_compatible sends NO max_tokens absent an
      explicit one (host default), and honors an explicit one.
  (f) FULL_TEXT translate e2e (the reported path) flows the new generous budget to the
      provider.
  (g) ceiling-table audit: the corrected, current, never-over-stated ceilings resolve.

Hermetic like the rest of tests/verify_*.py.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

init_env("loctms_tokbudget_")

from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus, TransMode
from app.services import providers, translate
from app.services.providers import (
    Provider,
    ProviderSpec,
    ProviderTruncated,
    run_completion,
)


# A fake provider that mimics a real one's budget resolution + truncation guard so the
# tests exercise the REAL _resolve_max_tokens seam and the REAL escalation wrapper.
class _RecordingProvider(Provider):
    name = "recorder"

    def __init__(self, truncate_until=None):
        # Raise ProviderTruncated while the RESOLVED budget < truncate_until; succeed
        # once it is >= truncate_until. None => never truncate.
        self.truncate_until = truncate_until
        self.budgets = []  # every max_tokens the provider resolved, in call order
        self.last_usage = None

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        model = kw.get("model")
        budget = providers._resolve_max_tokens(prompt, system, model, kw)
        self.budgets.append(budget)
        if self.truncate_until is not None and budget < self.truncate_until:
            # Mimic the real guard exactly: attach the budget that truncated + the
            # model ceiling so run_completion can escalate. (We do NOT call the private
            # _raise_truncated helper here on purpose — attaching plain attributes is
            # what makes this test also run cleanly against the OLD code for the
            # fail-before proof, where run_completion simply ignores the attributes.)
            e = ProviderTruncated(f"recorder hit max_tokens ({budget}) - output truncated")
            e.budget = budget
            e.ceiling = config.model_output_ceiling(model)
            raise e
        self.last_usage = {
            "input_tokens": 10,
            "output_tokens": 10,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "ok-translation"


# --- (a) budget floor / multiplier / ceiling math ----------------------------
def test_budget_floor_multiplier_ceiling():
    model = "claude-sonnet-4"  # ceiling 64000
    ceiling = config.model_output_ceiling(model)

    # (a1) FLOOR dominates for a small, markup-/punctuation-heavy input whose chars/4
    # estimate undercounts the real token count. OLD: min(max(256, tiny*1.5), ceiling)
    # == 256 (far below any real need); NEW: 2048 floor.
    markup = '<b>{0}</b> &mdash; 100%! "x" | [a](b) `c`'
    b1 = providers._sized_max_tokens(markup, None, model)
    assert b1 >= 2048, ("floor not applied", b1)  # OLD gives 256 -> fails
    assert b1 == providers._OUTPUT_BUDGET_FLOOR, b1

    # (a2) MULT applies for a mid-size input. est_tokens("x"*4000)=1000 -> 1000*4=4000.
    # OLD: est_output_tokens(1000)=1500 (< 3000) -> fails the >=3x assertion.
    mid = "x" * 4000
    b2 = providers._sized_max_tokens(mid, None, model)
    assert b2 >= 1000 * 3, ("multiplier below 3x", b2)  # OLD 1500 -> fails
    assert b2 == min(ceiling, max(2048, 1000 * providers._OUTPUT_BUDGET_MULT)), b2
    assert b2 == 4000, b2

    # (a3) ceiling CLAMP: est_tokens("x"*12000)=3000 -> 3000*4=12000 > 8192 ceiling.
    # OLD: min(max(256, 3000*1.5=4500), 8192)=4500 (!= ceiling) -> fails.
    big = "x" * 12000
    b3 = providers._sized_max_tokens(big, None, "claude-3-5")  # ceiling 8192
    assert b3 == config.model_output_ceiling("claude-3-5"), ("not clamped", b3)  # 8192
    assert b3 == 8192, b3

    # And the budget comfortably covers a real-world-shaped ~670-real-token input even when
    # the char estimate undercounts: the resolved budget is many times the estimate.
    real_like = (
        "Translate the following, preserving %(placeholders)s and <tags>markup</tags>: "
    ) + ("word, " * 120)
    bb = providers._sized_max_tokens(real_like, "You are a translator.", model)
    assert bb >= 2048, bb
    print("  (a) budget floor(2048)/mult(4)/ceiling-clamp math OK")


# --- (b) escalate-on-truncation retries with a LARGER budget -----------------
def test_escalation_grows_budget_on_truncation():
    prov = _RecordingProvider(truncate_until=3000)
    providers.register_provider("recorder", prov)
    try:
        spec = ProviderSpec(
            provider="recorder", model="claude-sonnet-4", retry_max=3, backoff_base=0
        )
        res = run_completion(spec, "Translate this.", system=None, task="translate")
    finally:
        providers._OVERRIDES.pop("recorder", None)
    # SUCCESS after escalation (OLD code: never escalates -> error stays set -> fails).
    assert res["error"] is None, res
    assert res["text"] == "ok-translation", res
    # The retry arrived with a STRICTLY LARGER budget (OLD: identical budget -> fails).
    assert len(prov.budgets) >= 2, prov.budgets
    assert prov.budgets[-1] > prov.budgets[0], prov.budgets
    assert prov.budgets[0] == 2048 and prov.budgets[1] == 4096, prov.budgets
    print("  (b) truncation escalates budget (2048 -> 4096) then succeeds OK")


# --- (c) explicit-budget caller (authoring) is byte-identical, never escalated -
def test_explicit_budget_caller_byte_identical():
    # A DIRECT complete() call (the setup-authoring / ocr / image-loc contract) with an
    # explicit max_tokens: the budget is honored verbatim (clamped to the ceiling) and
    # a truncation fails honestly with NO escalation — it bypasses run_completion.
    prov = _RecordingProvider(truncate_until=10**9)  # always truncates
    raised = False
    try:
        prov.complete("draft a big json", system="s", model="claude-sonnet-4", max_tokens=4096)
    except ProviderTruncated as e:
        raised = True
        assert getattr(e, "budget", None) == 4096, e  # carries the explicit budget
    assert raised, "explicit-budget truncation must still raise"
    assert prov.budgets == [4096], ("explicit budget not verbatim / escalated", prov.budgets)
    # And the resolver itself is unchanged for explicit budgets (clamped to ceiling).
    assert providers._resolve_max_tokens("p", "s", "claude-3-5", {"max_tokens": 999999}) == 8192
    assert providers._resolve_max_tokens("p", "s", "claude-sonnet-4", {"max_tokens": 4096}) == 4096
    print("  (c) explicit-budget caller byte-identical, no escalation OK")


# --- (d) ceiling-saturated truncation fails honestly -------------------------
def test_ceiling_saturated_truncation_fails_honestly():
    prov = _RecordingProvider(truncate_until=10**9)  # never satisfiable
    providers.register_provider("recorder", prov)
    try:
        spec = ProviderSpec(
            provider="recorder",
            model="claude-3-5",  # ceiling 8192
            retry_max=5,
            backoff_base=0,
        )
        res = run_completion(spec, "Translate this.", system=None, task="translate")
    finally:
        providers._OVERRIDES.pop("recorder", None)
    assert res["text"] == "", res
    assert res["error"] and "ceiling" in res["error"].lower(), res  # OLD: no such msg
    # Escalation grew to the ceiling and stopped there (no overshoot beyond 8192).
    assert prov.budgets[-1] == config.model_output_ceiling("claude-3-5"), prov.budgets
    assert prov.budgets == [2048, 4096, 8192], prov.budgets
    print("  (d) ceiling-saturated truncation -> honest 'exceeds ceiling' error OK")


# --- (e) per-provider: openai_compatible no max_tokens absent explicit -------
def _oc_capture_transport(captured, finish="stop"):
    import httpx

    def h(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hola"}, "finish_reason": finish}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    return httpx.MockTransport(h)


def test_openai_compatible_max_tokens_semantics_unchanged():
    from app.services import engines
    from app.services.providers import OpenAICompatibleProvider

    prof = engines.create_profile(
        "oc-budget",
        "openai_compatible",
        base_url="https://api.example.com",
        api_key="sk-x",
        model="some-model",
    )
    # (1) ABSENT explicit max_tokens -> body carries NONE (host default), unchanged.
    cap = {}
    OpenAICompatibleProvider(
        profile=engines.get_profile(prof.id), transport=_oc_capture_transport(cap)
    ).complete("Translate: hi", system="You translate.", task="translate", payload={"text": "hi"})
    assert "max_tokens" not in cap["body"], cap["body"]
    # (2) EXPLICIT max_tokens -> body carries it (clamped to the model ceiling).
    cap2 = {}
    OpenAICompatibleProvider(
        profile=engines.get_profile(prof.id), transport=_oc_capture_transport(cap2)
    ).complete(
        "Translate: hi",
        system="You translate.",
        max_tokens=1234,
        task="translate",
        payload={"text": "hi"},
    )
    assert cap2["body"].get("max_tokens") == 1234, cap2["body"]
    print("  (e) openai_compatible: no max_tokens absent explicit; explicit honored OK")


# --- (f) FULL_TEXT translate e2e flows the new budget ------------------------
def test_full_text_translate_gets_new_budget():
    prov = _RecordingProvider()  # never truncates; just records the budget
    providers.register_provider("recorder", prov)
    try:
        with Session(engine) as session:
            p = Project(name="ftbudget", source_locale="en", target_locales="es")
            session.add(p)
            session.commit()
            session.refresh(p)
            doc = Document(project_id=p.id, filename="d.txt", mode=TransMode.FULL_TEXT, fmt="txt")
            session.add(doc)
            session.commit()
            session.refresh(doc)
            body = "Hello there.\n\nThis is a short two-paragraph document."
            session.add(
                Segment(
                    document_id=doc.id,
                    project_id=p.id,
                    order_index=0,
                    source_text=body,
                    target_locale="es",
                    status=SegStatus.NEW,
                )
            )
            session.commit()
            spec = ProviderSpec(provider="recorder")  # model None -> DEFAULT ceiling
            for _ev in translate.pretranslate_stream(session, doc.id, "es", spec=spec):
                pass
    finally:
        providers._OVERRIDES.pop("recorder", None)
    assert prov.budgets, "FULL_TEXT translate never called the provider"
    # The translate path flowed the NEW generous budget (OLD: a small doc gets < 2048).
    assert prov.budgets[0] >= 2048, prov.budgets
    print("  (f) FULL_TEXT translate path flows the new generous budget OK")


# --- (g) ceiling-table audit -------------------------------------------------
def test_ceiling_table_audit():
    moc = config.model_output_ceiling
    # Corrected Claude 4 caps (were over-stated at 128000 -> would 400 large requests).
    assert moc("claude-opus-4-1-20250805") == 32000
    assert moc("claude-sonnet-4-5-20250929") == 64000
    assert moc("claude-haiku-4-5") == 64000
    # Substring ordering: specific ids win over the generic legacy "claude-3" key.
    assert moc("claude-3-7-sonnet-20250219") == 64000
    assert moc("claude-3-5-sonnet-latest") == 8192
    assert moc("claude-3-opus-20240229") == 4096  # legacy, not the 8192 DEFAULT
    # OpenAI + Google
    assert moc("gpt-4.1-2025-04-14") == 32768
    assert moc("gpt-4o-mini") == 16384
    assert moc("gpt-4-turbo") == 4096
    assert moc("gemini-2.5-pro") == 65536
    assert moc("gemini-1.5-pro") == 8192
    assert moc("something-unknown") == 8192  # DEFAULT
    # No entry is absurd/over-stated (a sanity bound well under any real 1M-ctx cap).
    for key, cap in config.MODEL_OUTPUT_CEILING.items():
        assert 4096 <= cap <= 200000, (key, cap)
    print("  (g) ceiling table audited (current, never over-stated) OK")


if __name__ == "__main__":
    init_db()
    test_budget_floor_multiplier_ceiling()
    test_escalation_grows_budget_on_truncation()
    test_explicit_budget_caller_byte_identical()
    test_ceiling_saturated_truncation_fails_honestly()
    test_openai_compatible_max_tokens_semantics_unchanged()
    test_full_text_translate_gets_new_budget()
    test_ceiling_table_audit()
    print("OUTPUT TOKEN BUDGET OK")
