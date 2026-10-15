"""Runaway generation guard verification (offline, no real server). Issue #720.

The format soak (round 8, rst/3 into tr-TR on the local engine) saw the model repeat itself on
one paragraph. Nothing looked at the streamed text, `finish_reason=length` was read as "budget too
small" and the escalate-on-truncation ladder doubled max_tokens to the model ceiling (2048 -> 4096
-> 8192: about 14,000 tokens of the same loop, 13 minutes) before the segment failed with "output
exceeds model ceiling ... consider segmented mode / smaller chunks".

Covers (httpx.MockTransport against a LocalProvider / OpenAICompatibleProvider, no network):
  - a streamed loop is cut off mid-stream (the rest of the stream is never read) with
    GenerationRunaway naming the loop
  - run_completion: ONE attempt, no budget escalation, the segment error says the engine repeated
    itself (old code: three attempts 2048 -> 4096 -> 8192 and "exceeds model ceiling")
  - the raw text-completions path (qwen ChatML adapter) is watched the same way
  - a non-loop answer far longer than the request is stopped by the length cap
  - a truncated (non-streamed) openai_compatible loop is not escalated either
  - no false positives: a long real translation, a translation that mirrors a source table rule
    or dot leader, whitespace padding, and a non-translation task (setup authoring) whose output
    dwarfs its input are all left alone; an ordinary truncation still escalates
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

init_env("loctms_runaway_")

import httpx

from app import config
from app.db import init_db
from app.services import providers
from app.services.providers import LocalProvider, ProviderSpec, run_completion

# getattr so a pre-fix tree fails on the behavior below, not at import
GenerationRunaway = getattr(providers, "GenerationRunaway", None)

BASE = "http://127.0.0.1:1234"
MODEL = "gemma-4-12b-q4km"
SOURCE = (
    "Translate the following text from English to Turkish.\n\n"
    "Sphinx uses reStructuredText as its markup language, and many of its strengths come "
    "from the power and straightforwardness of reStructuredText and its parsing and "
    "translating suite, the Docutils."
)
LOOP_UNIT = "ve Docutils'in ayrıştırma ve çeviri paketi, "


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _chunk(text, finish=None):
    return {"choices": [{"delta": {"content": text}, "index": 0, "finish_reason": finish}]}


class _CountingStream(httpx.SyncByteStream):
    """An SSE body that records how many events the client actually pulled."""

    def __init__(self, events):
        self.events = events
        self.read = 0

    def __iter__(self):
        for ev in self.events:
            self.read += 1
            yield f"data: {json.dumps(ev)}\n\n".encode()
        yield b"data: [DONE]\n\n"


def _sse_transport(events, seen=None):
    """A transport answering every request with `events`; returns (transport, streams, bodies)."""
    streams, bodies = [], []

    def h(req):
        bodies.append(json.loads(req.content or b"{}"))
        s = _CountingStream(events)
        streams.append(s)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=s)

    return httpx.MockTransport(h), streams, bodies


def _loop_events(n_loop=600, finish="length"):
    """A real start, then the same clause forever (the shape the engine produced)."""
    ev = [_chunk("Sphinx, işaretleme dili olarak reStructuredText kullanır ve ")]
    ev += [_chunk(LOOP_UNIT) for _ in range(n_loop)]
    ev.append(_chunk("", finish))
    ev.append({"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 8192}})
    return ev


def _local(transport):
    config.LOCAL_BASE_URL = BASE
    getattr(providers, "_OUTAGE_DEAD_UNTIL", {}).clear()
    p = LocalProvider()
    p._transport = transport
    return p


def _run(provider, prompt=SOURCE, task="translate", system=None):
    orig = providers.get_provider
    providers.get_provider = lambda name: provider
    try:
        spec = ProviderSpec(provider="local", model=MODEL, retry_max=2, backoff_base=0)
        return run_completion(spec, prompt, system=system, task=task)
    finally:
        providers.get_provider = orig


def test_stream_loop_is_cut_mid_stream():
    if GenerationRunaway is None:
        _fail("no GenerationRunaway: a repetition loop is never recognised (old code)")
    tr, streams, _ = _sse_transport(_loop_events())
    p = _local(tr)
    try:
        p.complete(SOURCE, task="translate", model=MODEL)
        _fail("a 600-times repeated clause was accepted / truncated, not stopped as a loop")
    except GenerationRunaway as e:
        msg = str(e)
        if "repeated itself" not in msg:
            _fail(f"the reason must say the engine repeated itself: {msg}")
        if not (getattr(e, "partial_output", "") or "").startswith("Sphinx"):
            _fail("the partial text must ride on the error for accounting")
    pulled = streams[0].read
    if pulled > 60:
        _fail(f"the loop was not cut early: {pulled} of {len(_loop_events())} events read")
    print(f"  streamed loop cut after {pulled} of {len(_loop_events())} events (GenerationRunaway)")


def test_run_completion_fails_fast_without_escalation():
    tr, _streams, bodies = _sse_transport(_loop_events())
    res = _run(_local(tr))
    if res.get("text"):
        _fail(f"a loop must never be returned as a translation: {res!r}")
    err = res.get("error") or ""
    if "repeated itself" not in err:
        _fail(f"the segment error must name the loop, got: {err!r}")
    if "ceiling" in err:
        _fail(f"a loop is not an output-ceiling problem: {err!r}")
    if res.get("attempts") != 1 or len(bodies) != 1:
        budgets = [b.get("max_tokens") for b in bodies]
        _fail(f"a loop must fail on the first attempt, no escalation; budgets sent {budgets}")
    if res.get("truncated"):
        _fail("a loop must not be reported as a truncation (it would advise a bigger model)")
    print(f"  run_completion: one attempt, no escalation, error {err[:70]!r}...")


def test_truncated_loop_without_stream_check_is_not_escalated():
    """Short loop that only shows at the very end of a truncated answer (below the streamed
    check's next window) is still caught at the finish=length seam, not escalated."""
    from app.services import providers_part5 as runaway_guard

    text = "Sphinx " + LOOP_UNIT * 12
    if not runaway_guard.looks_like_loop(text, SOURCE):
        _fail("12 back-to-back copies of a clause must read as a loop")
    try:
        runaway_guard.raise_if_loop(text, "translate", SOURCE, None)
        _fail("raise_if_loop let a truncated loop through")
    except GenerationRunaway:
        pass
    runaway_guard.raise_if_loop(text, "setup_author", SOURCE, None)  # not watched: no raise
    print("  finish=length seam: a truncated loop raises, a non-translation task is untouched")


def test_raw_completions_path_is_watched():
    """The qwen ChatML adapter path streams /v1/completions (choices[].text)."""
    events = [{"choices": [{"text": "Sphinx ", "index": 0, "finish_reason": None}]}]
    events += [
        {"choices": [{"text": LOOP_UNIT, "index": 0, "finish_reason": None}]} for _ in range(600)
    ]
    events.append({"choices": [{"text": "", "index": 0, "finish_reason": "length"}]})
    tr, streams, _ = _sse_transport(events)
    p = _local(tr)
    try:
        p.complete(SOURCE, task="translate", model="qwen3.5-27b")
        _fail("the raw completions path let a loop run to the end")
    except GenerationRunaway:
        pass
    if streams[0].read > 60:
        _fail(f"raw path loop not cut early: {streams[0].read} events read")
    print(f"  raw completions (ChatML adapter) path: loop cut after {streams[0].read} events")


def test_overlong_answer_is_capped():
    """No loop, just far more text than the request: stopped by the length cap."""
    words = [f"kelime{i} " for i in range(4000)]  # all distinct: never periodic
    events = [_chunk(w) for w in words] + [_chunk("", "stop")]
    tr, streams, _ = _sse_transport(events)
    p = _local(tr)
    try:
        p.complete("Translate: Hello.", task="translate", model=MODEL)
        _fail("an answer thousands of times longer than its request was accepted")
    except GenerationRunaway as e:
        if "far longer than the source" not in str(e):
            _fail(f"cap reason unclear: {e}")
    if streams[0].read >= len(events):
        _fail("the overlong stream was read to the end")
    print(f"  overlong (non-loop) answer stopped by the length cap after {streams[0].read} events")


def test_openai_compatible_truncated_loop_not_escalated():
    from app.services import engines
    from app.services.providers import OpenAICompatibleProvider

    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Sphinx " + LOOP_UNIT * 200},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 80, "completion_tokens": 4000},
            },
        )

    prof = engines.create_profile(
        "oc-runaway",
        "openai_compatible",
        base_url="https://api.example.com",
        api_key="sk-x",
        model="some-model",
    )
    prov = OpenAICompatibleProvider(
        profile=engines.get_profile(prof.id), transport=httpx.MockTransport(h)
    )
    try:
        prov.complete(SOURCE, task="translate", payload={"text": SOURCE})
        _fail("openai_compatible: a truncated loop was accepted")
    except Exception as e:
        if GenerationRunaway is None or not isinstance(e, GenerationRunaway):
            _fail(f"openai_compatible: a truncated loop raised {type(e).__name__} (escalates)")
    res = _run(prov)
    if res.get("attempts") != 1 or "repeated itself" not in (res.get("error") or ""):
        _fail(f"openai_compatible loop must fail on the first attempt: {res!r}")
    print("  openai_compatible: a truncated loop raises GenerationRunaway, one attempt")


def test_no_false_positives():
    from app.services import providers_part5 as g

    # A long real translation (well over the loop window) streams through untouched.
    real = (
        "Sphinx, işaretleme dili olarak reStructuredText kullanır ve güçlü yanlarının çoğu "
        "reStructuredText'in ve onun ayrıştırma ve çeviri paketi Docutils'in gücünden ve "
        "sadeliğinden gelir. "
    ) * 3
    real += " ".join(f"Bölüm {i}: ayrıntılar burada anlatılır." for i in range(60))
    events = [_chunk(real[i : i + 20]) for i in range(0, len(real), 20)] + [_chunk("", "stop")]
    tr, _, _ = _sse_transport(events)
    out = _local(tr).complete(SOURCE + " " + "x" * len(real), task="translate", model=MODEL)
    if out != real:
        _fail("a long legitimate translation was altered or stopped")

    # The source has a reStructuredText table rule / dot leader: the translation mirrors it.
    src = "Contents\n" + "=" * 600 + "\nChapter one " + "." * 500 + " 1"
    tgt = "İçindekiler\n" + "=" * 600 + "\nBirinci bölüm " + "." * 500 + " 1"
    if g.looks_like_loop(tgt[: tgt.index("\nBirinci")], src) or g.looks_like_loop(tgt[:-2], src):
        _fail("a table rule / dot leader copied from the source was called a loop")
    # Whitespace padding is bounded by the length cap, never called a loop.
    if g.looks_like_loop("Ad" + " " * 800, "Name"):
        _fail("whitespace padding was called a loop")
    # Real repetition in a short translation (under the window) is fine.
    if g.looks_like_loop("evet evet evet evet", "yes yes yes yes"):
        _fail("a short repeated word was called a loop")

    # A non-translation task whose output dwarfs its input is never watched (setup authoring).
    big = json.dumps({"steps": [{"name": "step", "prompt": "same text " * 20}] * 80})
    events = [_chunk(big[i : i + 50]) for i in range(0, len(big), 50)] + [_chunk("", "stop")]
    tr, _, _ = _sse_transport(events)
    out = _local(tr).complete("Make a workflow.", task="setup_author", model=MODEL)
    if out != big:
        _fail("setup authoring output (repetitive JSON, 100x its input) was stopped")
    print("  no false positives: long translation, mirrored rules/leaders, padding, authoring")


def test_ordinary_truncation_still_escalates():
    """A real (non-loop) truncation keeps the #683 escalate-on-truncation ladder."""
    calls = {"n": 0}

    def h(req):
        body = json.loads(req.content)
        calls["n"] += 1
        if body.get("max_tokens", 0) < 4096:
            text, finish = "Sphinx, işaretleme dili olarak", "length"
        else:
            text, finish = "Sphinx, işaretleme dili olarak reStructuredText kullanır.", "stop"
        ev = [_chunk(text), _chunk("", finish)]
        ev.append({"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 10}})
        payload = "".join(f"data: {json.dumps(e)}\n\n" for e in ev) + "data: [DONE]\n\n"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=payload.encode()
        )

    res = _run(_local(httpx.MockTransport(h)))
    if res.get("error") or "kullanır" not in (res.get("text") or ""):
        _fail(f"an ordinary truncation no longer escalates: {res!r}")
    if calls["n"] != 2:
        _fail(f"expected 2 attempts (2048 then 4096), got {calls['n']}")
    print("  ordinary truncation: still escalates 2048 -> 4096 and succeeds")


def test_watch_keeps_only_the_tail():
    """Gate: the watch held and re-joined the WHOLE answer at every 200-character check. It
    keeps only the tail the loop test reads (bounded memory and join cost); a loop that starts
    after a long clean answer is still caught."""
    import time

    from app.services import providers_part5 as rg
    from app.services.providers_common import GenerationRunaway

    words = [f"word{i} " for i in range(60_000)]  # ~600k chars, no repeating unit
    reference = "".join(words)  # a request as long as the answer (cap 6x)
    w = rg.RunawayWatch(reference)
    t0 = time.perf_counter()
    for d in words:
        w.feed(d)
    dt = time.perf_counter() - t0
    if dt > 5.0:
        _fail(f"feeding a 600k-character answer took {dt:.2f}s")
    held = sum(len(x) for x in w._parts)
    if held > rg._TAIL_CHARS + rg._CHECK_EVERY + 64:
        _fail(f"the watch holds {held} characters of a 600k answer, not just the tail")
    try:
        for _ in range(200):
            w.feed(" ham")
    except GenerationRunaway as e:
        if "ham" not in str(e):
            _fail(f"the late loop is named: {e}")
    else:
        _fail("a loop that starts after 600k characters must still be caught")
    print(f"  [gate] the watch keeps only the tail (600k chars in {dt:.2f}s); late loop caught")


def main():
    init_db()
    test_watch_keeps_only_the_tail()
    test_stream_loop_is_cut_mid_stream()
    test_run_completion_fails_fast_without_escalation()
    test_truncated_loop_without_stream_check_is_not_escalated()
    test_raw_completions_path_is_watched()
    test_overlong_answer_is_capped()
    test_openai_compatible_truncated_loop_not_escalated()
    test_no_false_positives()
    test_ordinary_truncation_still_escalates()
    print("RUNAWAY GENERATION OK")


if __name__ == "__main__":
    main()
