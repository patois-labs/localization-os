"""Local engine outage handling verification (offline, no real server). Issue #606.

The 2026-10-07 night pressure test restarted llama-router behind mt-gate (engine 14):
  * every in-flight streamed response was cut AFTER the 200 header: the body just ended
    with no finish_reason and no [DONE]. The client must treat that as a FAILURE, never
    as a complete translation.
  * for ~38 s mt-gate answered instant 502s (first good answer at +44.5 s). The retry
    ladder (2 retries, 0.5 s base) failed whole runs in seconds. A local engine with no
    failover target must ride out about 45 s of 502s.

Covers (all via httpx.MockTransport against a LocalProvider, time.sleep recorded):
  - a stream with text but no finish_reason raises StreamIncomplete from the drain, with
    the partial text/usage attached for spend accounting
  - run_completion never returns a cut stream's text as a success
  - a cut stream followed by a whole one succeeds on the retry
  - a run of 502s longer than the old ladder (7 in a row) is survived and the recorded
    waits cover at least 45 s
  - a persistent outage still ends in a clear engine error (bounded attempts)
  - the extension is local-only and off when a failover target exists or retries are 0
  - non-outage errors (a 400) keep the old behavior: one attempt, no sleeps
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

init_env("loctms_outage_")

import httpx

from app import config
from app.services import providers
from app.services.providers import (
    LocalProvider,
    ProviderSpec,
    run_completion,
)

# getattr so a pre-fix tree fails on the behavior below, not at import
StreamIncomplete = getattr(providers, "StreamIncomplete", ())

BASE = "http://127.0.0.1:1234"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _sse(chunks, done=True):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def _whole(text="Bonjour le monde"):
    return _sse(
        [
            {"choices": [{"delta": {"content": text}, "index": 0, "finish_reason": None}]},
            {"choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}},
        ]
    )


def _cut(text="Bonjour le"):
    """200 + some text, then the body just ends: no finish_reason, no [DONE]."""
    return _sse(
        [{"choices": [{"delta": {"content": text}, "index": 0, "finish_reason": None}]}],
        done=False,
    )


def _bad_gateway():
    return httpx.Response(502, json={"error": "bad gateway"})


def _scripted(responses):
    """A transport that plays `responses` (callables) in order, then repeats the last."""
    calls = {"n": 0}

    def h(_req):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i]()

    return httpx.MockTransport(h), calls


def _local(transport):
    config.LOCAL_BASE_URL = BASE
    getattr(providers, "_OUTAGE_DEAD_UNTIL", {}).clear()  # each test starts with a live engine
    p = LocalProvider()
    p._transport = transport
    return p


class _Sleeps:
    """Record time.sleep inside the providers module instead of sleeping."""

    def __enter__(self):
        self.waits = []
        self._real = providers.time.sleep
        providers.time.sleep = self.waits.append
        return self

    def __exit__(self, *a):
        providers.time.sleep = self._real


def _run(provider, spec):
    orig = providers.get_provider
    providers.get_provider = lambda name: provider
    try:
        return run_completion(spec, "Translate to French: Hello world")
    finally:
        providers.get_provider = orig


def test_drain_rejects_a_stream_without_finish_reason():
    p = _local(_scripted([_cut])[0])
    try:
        p.complete("Translate: hi")
    except StreamIncomplete as e:
        if getattr(e, "partial_output", None) != "Bonjour le":
            _fail(f"the cut text must ride on the error for accounting: {e!r}")
        print("  a stream with no finish_reason raises StreamIncomplete (partial text kept)")
        return
    _fail("a cut stream was accepted as a complete answer")


def test_cut_stream_is_never_a_success():
    tr, calls = _scripted([_cut])
    with _Sleeps() as sl:
        res = _run(_local(tr), ProviderSpec("local", retry_max=1, backoff_base=0.5))
    if res.get("text"):
        _fail(f"a cut stream returned text as a success: {res!r}")
    if "finish_reason" not in (res.get("error") or ""):
        _fail(f"the failure must name the cut stream: {res.get('error')!r}")
    if res.get("error_class") != "StreamIncomplete":
        _fail(f"error_class: {res.get('error_class')!r}")
    if calls["n"] < 2:
        _fail(f"a cut stream must be retried, got {calls['n']} call(s)")
    print(f"  run_completion: persistent cut stream -> error after {calls['n']} attempts, no text")
    del sl


def test_cut_then_whole_succeeds_on_retry():
    tr, calls = _scripted([_cut, _whole])
    with _Sleeps():
        res = _run(_local(tr), ProviderSpec("local", backoff_base=0.5))
    if res.get("text") != "Bonjour le monde" or res.get("error"):
        _fail(f"the retry after a cut stream must succeed with the WHOLE text: {res!r}")
    if calls["n"] != 2:
        _fail(f"expected 2 calls, got {calls['n']}")
    print("  a cut stream followed by a whole one: the retry delivers the whole answer")


def test_502_window_is_survived():
    # 6 attempts' worth of instant 502s (the OpenAI SDK makes 3 HTTP calls per attempt),
    # well past the old 3-attempt ladder, then a good answer.
    tr, calls = _scripted([_bad_gateway] * 18 + [_whole])
    with _Sleeps() as sl:
        res = _run(_local(tr), ProviderSpec("local", backoff_base=0.5))
    if res.get("text") != "Bonjour le monde":
        _fail(f"a 502 window must be survived, got {res.get('error')!r} after {calls['n']} calls")
    total = sum(w for w in sl.waits if w >= 1.5)  # run_completion's ladder, not the SDK's
    if total < 45:
        _fail(f"the waits must cover about 45 s of 502s, they cover {total} s: {sl.waits}")
    print(
        f"  502 window: survived {calls['n'] - 1} bad-gateway calls, waited {total:.0f} s in total"
    )


def test_persistent_outage_ends_with_a_clear_error():
    tr, _calls = _scripted([_bad_gateway])
    with _Sleeps() as sl:
        res = _run(_local(tr), ProviderSpec("local", backoff_base=0.5))
    if res.get("text") or "502" not in (res.get("error") or ""):
        _fail(f"a persistent outage must end in the engine error: {res!r}")
    # SDK-level retries (max_retries) multiply calls; the run_completion attempts are bounded.
    if res.get("attempts") != 2 + 1 + config.LOCAL_OUTAGE_RETRIES:
        _fail(f"attempts must be bounded at retry_max+1+extra: {res.get('attempts')}")
    if sum(w for w in sl.waits if w >= 1.5) < 45:
        _fail(f"the ladder must cover 45 s before giving up: {sl.waits}")
    print(f"  persistent 502: gives up after {res['attempts']} attempts with the engine error")


def test_extension_is_local_only_and_off_with_failover():
    # a failover target: fail over fast, as before (no extended ladder)
    tr, _calls = _scripted([_bad_gateway])

    class Backup(providers.MockProvider):
        name = "backup"

    orig = providers.get_provider
    providers.get_provider = lambda name: _local(tr) if name == "local" else Backup()
    try:
        with _Sleeps() as sl:
            res = run_completion(
                ProviderSpec("local", fallback_provider="backup", backoff_base=0.5), "Hello"
            )
    finally:
        providers.get_provider = orig
    if res.get("attempts") is None or sum(w for w in sl.waits if w >= 1.5) > 5:
        _fail(f"with a failover target the ladder must stay short: waits {sl.waits}")
    # retries disabled stays disabled
    tr, _calls = _scripted([_bad_gateway])
    with _Sleeps() as sl:
        res = _run(_local(tr), ProviderSpec("local", retry_max=0, backoff_base=0.5))
    if res.get("attempts") != 1 or any(w >= 1.5 for w in sl.waits):
        _fail(f"retry_max=0 must stay a single attempt: {res.get('attempts')} {sl.waits}")
    # the switch
    old = config.LOCAL_OUTAGE_RETRIES
    config.LOCAL_OUTAGE_RETRIES = 0
    try:
        tr, _calls = _scripted([_bad_gateway])
        with _Sleeps() as sl:
            res = _run(_local(tr), ProviderSpec("local", backoff_base=0.5))
        if res.get("attempts") != 3:
            _fail(f"LOCAL_OUTAGE_RETRIES=0 restores the old ladder: {res.get('attempts')}")
    finally:
        config.LOCAL_OUTAGE_RETRIES = old
    print("  extension is off with a failover target, retry_max=0 and LOCAL_OUTAGE_RETRIES=0")


def test_non_outage_error_is_not_retried_longer():
    tr, _calls = _scripted([lambda: httpx.Response(400, json={"error": "bad"})])
    with _Sleeps() as sl:
        res = _run(_local(tr), ProviderSpec("local", backoff_base=0.5))
    if res.get("attempts") != 1 or any(w >= 1.5 for w in sl.waits):
        _fail(f"a 400 is terminal, one attempt, no sleeps: {res.get('attempts')} {sl.waits}")
    print("  a 400 stays terminal: one attempt, no waits")


def test_dead_engine_does_not_stall_every_segment():
    """Review: after one call spent the whole ~78 s ladder, the next calls to the same dead
    engine use the short ladder, so a big document fails in seconds, not hours."""
    tr, _calls = _scripted([_bad_gateway])
    p = _local(tr)
    with _Sleeps() as sl:
        first = _run(p, ProviderSpec("local", backoff_base=0.5))
    if first.get("attempts") != 3 + config.LOCAL_OUTAGE_RETRIES:
        _fail(f"the first call must use the whole ladder: {first.get('attempts')}")
    with _Sleeps() as sl2:
        second = _run(p, ProviderSpec("local", backoff_base=0.5))
    if second.get("attempts") != 3 or sum(w for w in sl2.waits if w >= 1.5) > 5:
        _fail(
            f"a dead engine must fail fast for the next calls: {second.get('attempts')} "
            f"attempts, waits {sl2.waits}"
        )
    # a success clears the flag: the next outage gets the whole ladder again
    tr2, _c = _scripted([_whole])
    _run(_local(tr2), ProviderSpec("local", backoff_base=0.5))
    tr3, _c = _scripted([_bad_gateway])
    with _Sleeps():
        third = _run(_local(tr3), ProviderSpec("local", backoff_base=0.5))
    if third.get("attempts") != 3 + config.LOCAL_OUTAGE_RETRIES:
        _fail(f"after a recovery the ladder is back: {third.get('attempts')}")
    del sl
    print("  a dead engine fails fast after one exhausted ladder; recovery restores the ladder")


def test_marked_down_is_per_engine_and_named_in_the_error():
    """Gate: the 60 s marked-down state is keyed on the engine's server, never poisons a
    different local engine, and both the call that sets it and a call it shortens say so in
    the error text (the job's first_error keeps 300 chars, so the note leads)."""
    tr, _calls = _scripted([_bad_gateway])
    with _Sleeps():
        first = _run(_local(tr), ProviderSpec("local", backoff_base=0.5))
    if not (first.get("error") or "").startswith("engine marked down for 60 s"):
        _fail(f"the call that marks the engine down must say so: {first.get('error')!r}")
    if "502" not in first["error"]:
        _fail(f"the engine's own error must stay in the text: {first['error']!r}")
    p = LocalProvider()
    p._transport = _scripted([_bad_gateway])[0]
    with _Sleeps():
        second = _run(p, ProviderSpec("local", backoff_base=0.5))
    if second.get("attempts") != 3 or not (second.get("error") or "").startswith(
        "engine marked down ("
    ):
        _fail(f"a shortened call must name the marked-down state: {second!r}")
    # a DIFFERENT local engine (another server) keeps its whole ladder
    other = LocalProvider()
    other._profile_base = "http://127.0.0.1:5678/v1"
    other._profile_key = None
    other._transport = _scripted([_bad_gateway])[0]
    with _Sleeps():
        third = _run(other, ProviderSpec("local", backoff_base=0.5))
    if third.get("attempts") != 3 + config.LOCAL_OUTAGE_RETRIES:
        _fail(f"another engine must not be marked down: {third.get('attempts')}")
    # a healthy call to the marked engine is not refused and clears only its own mark
    key = providers._outage_key(p)
    if key not in providers._OUTAGE_DEAD_UNTIL:
        _fail(f"precondition: the first engine is still marked: {providers._OUTAGE_DEAD_UNTIL}")
    ok = _run(_ok_provider(), ProviderSpec("local", backoff_base=0.5))
    if ok.get("error") or not ok.get("text"):
        _fail(f"a marked-down engine that answers must succeed: {ok!r}")
    if key in providers._OUTAGE_DEAD_UNTIL:
        _fail(f"a success must clear the mark: {providers._OUTAGE_DEAD_UNTIL}")
    providers._OUTAGE_DEAD_UNTIL.clear()
    print("  marked-down: per engine server, named in the error, cleared by a success")


def _ok_provider():
    p = LocalProvider()
    p._transport = _scripted([_whole])[0]
    return p


def test_read_timeout_keeps_the_short_ladder():
    """Gate: a READ timeout means the engine is up but slow or hung; each attempt already
    waited PROVIDER_TIMEOUT_S, so it keeps the old ladder. A CONNECT timeout (host gone, for
    example the tailnet peer down) is an outage and gets the long ladder."""

    def _raise(exc):
        def h(req):
            raise exc("timed out", request=req)

        return httpx.MockTransport(h)

    with _Sleeps():
        res = _run(_local(_raise(httpx.ReadTimeout)), ProviderSpec("local", backoff_base=0.5))
    if res.get("attempts") != 3:
        _fail(f"a read timeout must keep the short ladder: {res.get('attempts')} {res}")
    with _Sleeps():
        res = _run(_local(_raise(httpx.ConnectTimeout)), ProviderSpec("local", backoff_base=0.5))
    if res.get("attempts") != 3 + config.LOCAL_OUTAGE_RETRIES:
        _fail(f"a connect timeout is an outage: {res.get('attempts')} {res}")
    print("  read timeout keeps the short ladder; connect timeout gets the outage ladder")


def test_claude_and_gemini_cut_streams_are_failures():
    """Review: every streaming adapter treats a body that ends early as a failure."""
    from types import SimpleNamespace
    from unittest.mock import patch

    import anthropic

    class _Ctx:
        text_stream = ["hola "]  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return SimpleNamespace(usage=None, stop_reason=None, content=[])

    class _Anth:
        def __init__(self, *a, **kw):
            self.messages = SimpleNamespace(stream=lambda **k: _Ctx())

    orig, key = anthropic.Anthropic, config.ANTHROPIC_API_KEY
    anthropic.Anthropic, config.ANTHROPIC_API_KEY = _Anth, "sk-test"
    try:
        providers.ClaudeProvider().complete("p", model="claude-sonnet-4-6")
        _fail("a Claude stream with no stop_reason was accepted as a complete answer")
    except StreamIncomplete:
        pass
    finally:
        anthropic.Anthropic, config.ANTHROPIC_API_KEY = orig, key

    from google.genai import types

    cut = types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(parts=[types.Part(text="hola")]))]
    )
    with patch("google.genai.Client") as client:
        client.return_value.models.generate_content_stream.return_value = iter([cut])
        try:
            providers.GeminiProvider().complete("p", model="gemini-2.5-pro")
            _fail("a Gemini stream whose last chunk has no finish_reason was accepted")
        except StreamIncomplete:
            pass
    print("  Claude (no stop_reason) and Gemini (no finish_reason) cut streams: StreamIncomplete")


def main():
    test_drain_rejects_a_stream_without_finish_reason()
    test_cut_stream_is_never_a_success()
    test_cut_then_whole_succeeds_on_retry()
    test_502_window_is_survived()
    test_persistent_outage_ends_with_a_clear_error()
    test_extension_is_local_only_and_off_with_failover()
    test_non_outage_error_is_not_retried_longer()
    test_dead_engine_does_not_stall_every_segment()
    test_marked_down_is_per_engine_and_named_in_the_error()
    test_read_timeout_keeps_the_short_ladder()
    test_claude_and_gemini_cut_streams_are_failures()
    print("LOCAL OUTAGE OK")


if __name__ == "__main__":
    main()
