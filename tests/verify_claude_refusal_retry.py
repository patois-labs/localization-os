"""Claude provider: API refusal signal (#633) and Retry-After on 429/529 (#634).

OFFLINE: the REAL anthropic SDK runs against an httpx2 MockTransport (no network, throwaway
DATA_DIR). Needs anthropic >= 1.x (httpx2 transport).

  #633  stop_reason "refusal" is never a translation: ClaudeProvider hands back the refusal
        sentinel, run_completion books the usage and returns it, the refusal guard flags it,
        a plain-translate segment is NOT written (NEEDS_REVIEW), /api/translate/text's
        seam reports an error with empty text, and the LQA judge fails closed. An
        OpenAI-compatible finish_reason "content_filter" is treated the same.
  #634  a 429/529 Retry-After (seconds, fractional, HTTP date, retry-after-ms) or the
        anthropic-ratelimit-*-reset timestamps set the retry delay, capped by
        PROVIDER_RETRY_AFTER_CAP_S and never shorter than the computed backoff; absent
        headers keep the plain backoff; the wait is on the caller's worker thread.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

init_env("loctms_claude633_")

import anthropic
import httpx2
from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus, TransMode
from app.services import lqa, providers, refusal, translate
from app.services.providers import ProviderSpec, run_completion

MODEL = "claude-haiku-4-5"


def _sse(stop_reason, text):
    def ev(name, data):
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    msg = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 1},
    }
    return (
        ev("message_start", {"type": "message_start", "message": msg})
        + ev(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        + ev(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )
        + ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        + ev(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        )
        + ev("message_stop", {"type": "message_stop"})
    )


def _ok(text="Hola mundo", stop="end_turn"):
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=_sse(stop, text).encode()
    )


def _err(status, headers=None):
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    return httpx2.Response(status, headers=headers or {}, json=body)


class _Wire:
    """Scripted Anthropic wire: one response per request, in order; the last one repeats."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request):
        r = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return r(request) if callable(r) else r


class _patched:
    """Route ClaudeProvider's anthropic.Anthropic(...) at the scripted wire (no SDK retries)."""

    def __init__(self, wire):
        self.wire = wire

    def __enter__(self):
        self._orig = anthropic.Anthropic
        self._key = config.ANTHROPIC_API_KEY
        wire, orig = self.wire, self._orig

        def factory(*a, **kw):
            kw["http_client"] = httpx2.Client(transport=httpx2.MockTransport(wire))
            kw["max_retries"] = 0
            return orig(*a, **kw)

        anthropic.Anthropic = factory
        config.ANTHROPIC_API_KEY = "sk-test"
        return self

    def __exit__(self, *a):
        anthropic.Anthropic = self._orig
        config.ANTHROPIC_API_KEY = self._key


REFUSED_BODY = "Here is the translation you asked for, partially: Hola"


def _project(session, lines):
    p = Project(name="c633", source_locale="en", target_locales="es")
    session.add(p)
    session.commit()
    session.refresh(p)
    doc = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED, fmt="txt")
    session.add(doc)
    session.commit()
    session.refresh(doc)
    for i, line in enumerate(lines):
        session.add(
            Segment(
                document_id=doc.id,
                project_id=p.id,
                order_index=i,
                source_text=line,
                target_locale="es",
                status=SegStatus.NEW,
            )
        )
    session.commit()
    return p, doc


def test_provider_returns_refusal_sentinel():
    with _patched(_Wire(lambda r: _ok(REFUSED_BODY, "refusal"))):
        prov = providers.ClaudeProvider()
        out = prov.complete("Translate: Hello world.", system="sys", model=MODEL)
    assert refusal.is_refusal(out), out
    assert REFUSED_BODY not in out, out
    # the billed call is still accounted
    assert prov.last_usage and prov.last_usage["output_tokens"] == 7, prov.last_usage
    # a normal end_turn is untouched
    with _patched(_Wire(lambda r: _ok("Hola mundo"))):
        assert providers.ClaudeProvider().complete("Hi", model=MODEL) == "Hola mundo"
    print("  633a. stop_reason refusal -> refusal sentinel, usage kept, end_turn untouched OK")


def test_run_completion_and_translate_text():
    spec = ProviderSpec(provider="claude", model=MODEL, retry_max=0)
    with _patched(_Wire(lambda r: _ok(REFUSED_BODY, "refusal"))):
        res = run_completion(spec, "Translate: Hello.", system="sys")
        assert res["error"] is None and refusal.is_refusal(res["text"]), res
        assert res["usage"]["output_tokens"] == 7, res
        # /api/translate/text seam: error + empty text, never the sentinel as a translation
        out = translate.translate_text_once("Hello world.", "en", "es", spec=spec)
    assert out["text"] == "" and out["error"], out
    print("  633b. run_completion + translate_text_once quarantine the refusal OK")


def test_plain_translate_does_not_write():
    spec = ProviderSpec(provider="claude", model=MODEL, retry_max=0)
    with _patched(_Wire(lambda r: _ok(REFUSED_BODY, "refusal"))), Session(engine) as session:
        _, doc = _project(session, ["Hello world, this is a sentence."])
        events = list(translate.pretranslate_stream(session, doc.id, "es", spec=spec))
        seg = session.exec(translate.select(Segment).where(Segment.document_id == doc.id)).first()
    assert not (seg.target_text or "").strip(), seg.target_text
    assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
    summary = [e for e in events if e.get("type") == "summary"][-1]
    assert not summary.get("translated"), summary
    print("  633c. plain translate: target unwritten, NEEDS_REVIEW, not counted translated OK")


def test_lqa_judge_fails_closed():
    with _patched(_Wire(lambda r: _ok('{"errors":[]}', "refusal"))):
        errors, _cost, _tin, _tout, status, _shape = lqa._llm_errors(
            "Hello", "Hola", "en", "es", "", "claude"
        )
    assert status == lqa.JUDGE_UNAVAILABLE and errors == [], (status, errors)
    print("  633d. LQA judge: refusal is JUDGE_UNAVAILABLE, never a clean pass OK")


def test_openai_content_filter():
    class _Delta:
        content = "partial"

    class _Choice:
        def __init__(self, fr):
            self.delta = _Delta()
            self.finish_reason = fr

    class _Chunk:
        def __init__(self, fr):
            self.choices = [_Choice(fr)]
            self.usage = None

    class _Client:
        def __init__(self, *a, **kw):
            self.chat = self
            self.completions = self

        def create(self, **kw):
            return iter([_Chunk(None), _Chunk("content_filter")])

    import openai

    orig, key = openai.OpenAI, config.OPENAI_API_KEY
    openai.OpenAI = _Client
    config.OPENAI_API_KEY = "sk-test"
    try:
        out = providers.OpenAIProvider().complete("Hi", model="gpt-5.2")
    finally:
        openai.OpenAI, config.OPENAI_API_KEY = orig, key
    assert refusal.is_refusal(out) and "partial" not in out, out
    print("  633e. OpenAI finish_reason content_filter -> refusal sentinel OK")


def test_guard_off_never_writes_sentinel():
    """Review: REFUSAL_GUARD_ENABLED=0 must not turn the sentinel into a stored translation."""
    spec = ProviderSpec(provider="claude", model=MODEL, retry_max=2, backoff_base=0)
    orig = config.REFUSAL_GUARD_ENABLED
    config.REFUSAL_GUARD_ENABLED = False
    try:
        wire = _Wire(lambda r: _ok(REFUSED_BODY, "refusal"))
        with _patched(wire), Session(engine) as session:
            res = run_completion(spec, "Translate: Hello.", system="sys")
            assert res["text"] == "" and res["error"], res
            assert wire.calls == 1, wire.calls  # terminal: not retried
            _, doc = _project(session, ["Hello world, this is a sentence."])
            list(translate.pretranslate_stream(session, doc.id, "es", spec=spec))
            seg = session.exec(
                translate.select(Segment).where(Segment.document_id == doc.id)
            ).first()
        assert "provider refusal" not in (seg.target_text or "").lower(), seg.target_text
        assert not (seg.target_text or "").strip(), seg.target_text
        with _patched(_Wire(lambda r: _ok('{"errors":[]}', "refusal"))):
            _e, _c, _i, _o, status, _sh = lqa._llm_errors("Hello", "Hola", "en", "es", "", "claude")
        assert status == lqa.JUDGE_UNAVAILABLE, status
    finally:
        config.REFUSAL_GUARD_ENABLED = orig
    print("  633f. guard off: sentinel never returned/written, judge still unavailable OK")


def test_gemini_safety_is_refusal():
    from types import SimpleNamespace as NS

    from google import genai

    def run(finish, block=None):
        chunk = NS(
            text="partial",
            candidates=[NS(finish_reason=finish)] if finish else [],
            usage_metadata=None,
            prompt_feedback=NS(block_reason=block) if block else None,
        )

        class _Models:
            def generate_content_stream(self, **kw):
                return iter([chunk])

        class _Client:
            def __init__(self, *a, **kw):
                self.models = _Models()

        orig, key = genai.Client, config.GEMINI_API_KEY
        genai.Client, config.GEMINI_API_KEY = _Client, "k"
        try:
            return providers.GeminiProvider().complete("Hi", model="gemini-2.5-flash")
        finally:
            genai.Client, config.GEMINI_API_KEY = orig, key

    out = run("FinishReason.SAFETY")
    assert refusal.is_refusal(out) and "partial" not in out, out
    assert refusal.is_refusal(run(None, block="SAFETY"))
    assert run("FinishReason.STOP") == "partial"
    print("  633g. Gemini SAFETY / prompt block -> refusal sentinel, STOP untouched OK")


def test_retry_after_garbage_forms():
    err = lambda h, st=429: NS_ERR(st, h)  # noqa: E731
    for bad in ("-5", "0", "nan", "inf", "1e999", "garbage", "", "Wed, 01 Jan 2020 00:00:00 GMT"):
        assert providers._claude_retry_after(err({"retry-after": bad})) is None, bad
        assert providers._claude_retry_after(err({"retry-after-ms": bad})) is None, bad
    assert providers._claude_retry_after(err({"retry-after": "5"}, 500)) is None
    assert providers._claude_retry_after(err({"retry-after-ms": "-1", "retry-after": "4"})) == 4.0
    print("  634d. negative/zero/nan/inf/garbage/past-date headers -> None OK")


# ---- #634 -----------------------------------------------------------------------------
class NS_ERR:
    def __init__(self, status, headers):
        self.status_code = status
        self.response = type("R", (), {"headers": headers})()


def _delays(first_headers, status=429, retry_max=2, base=0.01):
    """Run one call whose first response is `status` with `first_headers`, then a success;
    return the sleeps the retry loop asked for."""
    sleeps = []
    wire = _Wire(lambda r: _err(status, first_headers), lambda r: _ok("Hola"))
    spec = ProviderSpec(provider="claude", model=MODEL, retry_max=retry_max, backoff_base=base)
    with _patched(wire), mock.patch.object(providers.time, "sleep", sleeps.append):
        res = run_completion(spec, "Translate: Hello.", system="sys")
    assert res["error"] is None and res["text"] == "Hola", res
    assert wire.calls == 2, wire.calls
    return sleeps


def test_retry_after_seconds_and_variants():
    assert _delays({"retry-after": "3"}) == [3.0]
    assert _delays({"retry-after": "1.5"}, status=529) == [1.5]
    assert _delays({"retry-after-ms": "2500"}, status=529) == [2.5]
    when = formatdate(time.time() + 6.5, usegmt=True)
    (d,) = _delays({"retry-after": when})
    assert 4.0 <= d <= 6.6, d
    reset = (datetime.now(timezone.utc) + timedelta(seconds=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d,) = _delays({"anthropic-ratelimit-requests-reset": reset})
    assert 6.5 <= d <= 9.0, d
    print("  634a. Retry-After seconds / fractional / HTTP date / ms / ratelimit-reset honored OK")


def test_retry_after_caps_and_floor():
    cap = float(config.PROVIDER_RETRY_AFTER_CAP_S)
    assert _delays({"retry-after": "86400"}) == [cap]  # hostile header is clamped
    # never shorter than our own backoff
    assert _delays({"retry-after": "1"}, base=5.0) == [5.0]
    print("  634b. capped by PROVIDER_RETRY_AFTER_CAP_S and floored at the computed backoff OK")


def test_no_header_keeps_backoff_and_other_statuses_ignored():
    assert _delays({}) == [0.01]
    assert _delays({"retry-after": "garbage"}) == [0.01]
    # only 429/529 carry the header meaning; a 500 keeps the plain backoff
    assert _delays({"retry-after": "30"}, status=500) == [0.01]
    print("  634c. no/garbage header and non-429/529 statuses keep the plain backoff OK")


if __name__ == "__main__":
    init_db()
    failed = []
    for fn in (
        test_provider_returns_refusal_sentinel,
        test_run_completion_and_translate_text,
        test_plain_translate_does_not_write,
        test_lqa_judge_fails_closed,
        test_openai_content_filter,
        test_guard_off_never_writes_sentinel,
        test_gemini_safety_is_refusal,
        test_retry_after_garbage_forms,
        test_retry_after_seconds_and_variants,
        test_retry_after_caps_and_floor,
        test_no_header_keeps_backoff_and_other_statuses_ignored,
    ):
        try:
            fn()
        except Exception as exc:  # run every check so one report shows all failures
            failed.append(fn.__name__)
            print(f"  FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    if failed:
        print("FAILED: " + ", ".join(failed))
        sys.exit(1)
    print("CLAUDE REFUSAL RETRY OK")
