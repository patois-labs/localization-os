"""Provider resilience + prompt-caching verification — Features 0 & 7.

Covers the Chunk-0/7 provider-layer changes end to end, all OFFLINE (no real
provider calls, no network, throwaway DATA_DIR, blanked keys):

  (a) Anthropic translation calls carry cache_control=ephemeral on the system
      block, AND the system prefix is byte-identical across two consecutive
      segment calls of the same doc/locale (the thing that makes the cache hit).
  (b) Provider cache-usage fields (cache_read/cache_write) flow into the
      translation stats (and therefore Job.result).
  (c) Warm-then-fan-out ordering: the FIRST pending segment is translated
      synchronously (writing the shared prefix cache) BEFORE the pool fans the
      rest out.
  (d) A streaming provider that goes silent trips ProviderStalled, which
      run_completion treats as retryable -> failover to the backup provider.
  (e) A truncated provider response (ProviderTruncated) is NEVER accepted as a
      success: no partial target is written; run_completion surfaces the error.
  (f) estimate_translation() unit behavior (segment count, token math, duration).
  (g) An oversized FULL_TEXT body is routed through the paragraph chunker
      (multiple provider calls, rejoined) instead of one giant truncating call.

Hermetic like the rest: throwaway LOC_TMS_DATA_DIR + cleared provider keys at
import (forces MockProvider), ASCII-only prints.
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

init_env("loctms_resil_")
# Keep the stall timeout tiny so test (d) fails fast instead of waiting 2 min.
os.environ["PROVIDER_TIMEOUT_S"] = "1"

from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    Segment,
    SegStatus,
    TransMode,
)
from app.services import providers, translate
from app.services.providers import (
    Provider,
    ProviderSpec,
    ProviderStalled,
    ProviderTruncated,
    est_output_tokens,
    run_completion,
)


# --- (a) Anthropic cache_control + byte-identical prefix ---------------------
class _FakeUsage:
    input_tokens = 100
    output_tokens = 20
    cache_creation_input_tokens = 100
    cache_read_input_tokens = 0


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessage:
    stop_reason = "end_turn"
    usage = _FakeUsage()

    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeStreamCtx:
    """Mimics anthropic client.messages.stream(...) context manager."""

    def __init__(self, recorder, args):
        self._recorder = recorder
        self._args = args
        self.text_stream = ["hola ", "mundo"]

    def __enter__(self):
        self._recorder.append(self._args)
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return _FakeMessage("hola mundo")


class _FakeMessages:
    def __init__(self, recorder):
        self._recorder = recorder

    def stream(self, **args):
        return _FakeStreamCtx(self._recorder, args)


class _FakeAnthropic:
    """Records every call's args so we can assert on the system blocks."""

    recorder = []  # noqa: RUF012

    def __init__(self, *a, **kw):
        self.messages = _FakeMessages(_FakeAnthropic.recorder)


def test_anthropic_cache_control_and_stable_prefix():
    import anthropic

    _FakeAnthropic.recorder.clear()
    orig_cls, orig_key = anthropic.Anthropic, config.ANTHROPIC_API_KEY
    anthropic.Anthropic = _FakeAnthropic
    config.ANTHROPIC_API_KEY = "sk-test"
    config.PROMPT_CACHING = True
    try:
        prov = providers.ClaudeProvider()
        system = "You are a professional en->es translator. " + ("x" * 200)
        # Two consecutive segment calls sharing the same system prefix.
        prov.complete("Segment one.", system=system, model="claude-sonnet-4-6")
        prov.complete("Segment two, different user text.", system=system, model="claude-sonnet-4-6")
    finally:
        anthropic.Anthropic = orig_cls
        config.ANTHROPIC_API_KEY = orig_key

    calls = _FakeAnthropic.recorder
    assert len(calls) == 2, len(calls)
    for c in calls:
        blocks = c["system"]
        assert isinstance(blocks, list) and blocks, blocks
        assert blocks[0]["type"] == "text"
        assert blocks[0].get("cache_control") == {"type": "ephemeral"}, blocks[0]
    # Byte-identical system prefix across the two segment calls (cache-hit key).
    assert calls[0]["system"] == calls[1]["system"], "system prefix drifted"
    # max_tokens was sized from the input, clamped to the sonnet ceiling.
    assert calls[0]["max_tokens"] <= config.model_output_ceiling("claude-sonnet-4-6")
    print("  (a) Anthropic cache_control ephemeral + byte-identical prefix OK")


def test_prompt_caching_off_drops_marker():
    import anthropic

    _FakeAnthropic.recorder.clear()
    orig_cls, orig_key, orig_pc = (
        anthropic.Anthropic,
        config.ANTHROPIC_API_KEY,
        config.PROMPT_CACHING,
    )
    anthropic.Anthropic = _FakeAnthropic
    config.ANTHROPIC_API_KEY = "sk-test"
    config.PROMPT_CACHING = False
    try:
        providers.ClaudeProvider().complete("Hi.", system="Some system.", model="claude-sonnet-4-6")
    finally:
        anthropic.Anthropic = orig_cls
        config.ANTHROPIC_API_KEY = orig_key
        config.PROMPT_CACHING = orig_pc
    block = _FakeAnthropic.recorder[0]["system"][0]
    assert "cache_control" not in block, block
    print("  (a') PROMPT_CACHING=0 omits cache_control marker OK")


# --- (b) cache-usage fields flow into stats ----------------------------------
def _make_full_text_project(session, body):
    p = Project(name="resil", source_locale="en", target_locales="es")
    session.add(p)
    session.commit()
    session.refresh(p)
    doc = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED, fmt="txt")
    session.add(doc)
    session.commit()
    session.refresh(doc)
    for i, line in enumerate(body):
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


def test_cache_usage_flows_into_stats():
    providers.MockProvider.emit_cache_usage = True
    providers.MockProvider._seen_prefixes = set()
    try:
        with Session(engine) as session:
            _, doc = _make_full_text_project(
                session, ["First sentence here.", "Second sentence here.", "Third sentence here."]
            )
            stats = {}
            for ev in translate.pretranslate_stream(session, doc.id, "es"):
                if ev["type"] == "summary":
                    stats = ev
        assert stats["translated"] == 3, stats
        # write on the first (cache-miss) call, reads on the warm ones.
        assert stats["tokens_cache_write"] > 0, stats
        assert stats["tokens_cache_read"] > 0, stats
    finally:
        providers.MockProvider.emit_cache_usage = False
        providers.MockProvider._seen_prefixes = set()
    print("  (b) cache_read/cache_write tokens flow into stats OK")


# --- (c) warm-then-fan-out ordering ------------------------------------------
class _OrderProvider(Provider):
    """Records the wall-clock order + thread of each complete() so we can assert
    the first (warm) call fully precedes the fanned-out rest."""

    name = "order"

    def __init__(self):
        self.events = []

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        import threading

        self.events.append((time.time(), threading.get_ident()))
        time.sleep(0.02)
        self.last_usage = {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "t"


def test_warm_then_fanout_ordering():
    prov = _OrderProvider()
    providers.register_provider("order", prov)
    try:
        with Session(engine) as session:
            _, doc = _make_full_text_project(session, ["a a a", "b b b", "c c c", "d d d"])
            spec = ProviderSpec(provider="order")
            list(translate.pretranslate_stream(session, doc.id, "es", spec=spec))
        assert len(prov.events) == 4, prov.events
        warm_ts, warm_tid = prov.events[0]
        rest = prov.events[1:]
        # every fanned-out call STARTS after the warm call started, and the warm
        # call ran on its own (this thread) before the pool threads.
        assert all(ts >= warm_ts for ts, _ in rest), prov.events
        assert any(tid != warm_tid for _, tid in rest), "rest never used a pool thread"
    finally:
        providers._OVERRIDES.pop("order", None)
    print("  (c) warm-first-then-fan-out ordering OK")


# --- (d) stall -> ProviderStalled -> failover --------------------------------
class _StallingProvider(Provider):
    name = "staller"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        # Emulate a stream that goes silent: watchdog over an iterator that hangs.
        def _silent():
            time.sleep(config.PROVIDER_TIMEOUT_S + 1)
            yield "too late"

        for _ in providers._stall_watchdog(_silent()):
            pass
        return "unreachable"


class _BackupProvider(Provider):
    name = "backup"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.last_usage = {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "backup-translation"


def test_stall_trips_failover():
    providers.register_provider("staller", _StallingProvider())
    providers.register_provider("backup", _BackupProvider())
    try:
        spec = ProviderSpec(provider="staller", fallback_provider="backup", retry_max=0)
        res = run_completion(spec, "hello", system="sys")
        assert res["error"] is None, res
        assert res["text"] == "backup-translation", res
        assert res["provider_used"] == "backup" and res["failover"], res
    finally:
        providers._OVERRIDES.pop("staller", None)
        providers._OVERRIDES.pop("backup", None)
    print("  (d) stall -> ProviderStalled -> failover to backup OK")


def test_stall_watchdog_raises():
    def _silent():
        time.sleep(config.PROVIDER_TIMEOUT_S + 1)
        yield "x"

    raised = False
    try:
        list(providers._stall_watchdog(_silent()))
    except ProviderStalled:
        raised = True
    assert raised, "watchdog did not raise ProviderStalled on a silent stream"
    print("  (d') _stall_watchdog raises ProviderStalled on silence OK")


# --- (e) truncation is never a silent success --------------------------------
class _TruncatingProvider(Provider):
    name = "truncator"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        _TruncatingProvider.calls += 1
        raise ProviderTruncated("hit max_tokens — partial output")


def test_truncation_not_accepted_no_partial_written():
    _TruncatingProvider.calls = 0
    providers.register_provider("truncator", _TruncatingProvider())
    try:
        spec = ProviderSpec(provider="truncator", retry_max=1)
        res = run_completion(spec, "hello", system="sys")
        assert res["text"] == "", res
        assert res["error"] and "max_tokens" in res["error"], res
        # retried (not terminal), never wrote a partial.
        assert _TruncatingProvider.calls >= 2, _TruncatingProvider.calls

        # And end-to-end: a truncated segment leaves the target UNwritten.
        with Session(engine) as session:
            _, doc = _make_full_text_project(session, ["Only sentence."])
            list(translate.pretranslate_stream(session, doc.id, "es", spec=spec))
            seg = session.exec(
                translate.select(Segment).where(Segment.document_id == doc.id)
            ).first()
            assert not (seg.target_text or "").strip(), seg.target_text
    finally:
        providers._OVERRIDES.pop("truncator", None)
    print("  (e) truncation surfaced as error; no partial target written OK")


# --- (f) estimate_translation unit -------------------------------------------
def test_estimate_translation():
    assert est_output_tokens(1000) == 1500  # 1.5x padding above the floor
    assert est_output_tokens(0) == 256  # small-source floor
    assert est_output_tokens(100) == 256  # 1.5x below floor -> floored
    with Session(engine) as session:
        _p, doc = _make_full_text_project(session, ["word word word word", "more more more"])
        est = translate.estimate_translation(session, doc.id, "es")
        assert est["segments"] == 2, est
        assert est["input_tokens"] > 0, est
        assert est["output_tokens"] == est_output_tokens(est["input_tokens"]), est
        assert est["seconds"] >= 0 and est["tokens_per_sec"] > 0, est
        assert est["provider"], est
        # a nonexistent doc estimates zero, not a crash.
        z = translate.estimate_translation(session, 999999, "es")
        assert z["segments"] == 0, z
    print("  (f) estimate_translation math + empty-doc guard OK")


# --- (g) oversized full-text routes through chunking -------------------------
class _CountingProvider(Provider):
    name = "counter"

    def __init__(self):
        self.calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        self.calls += 1
        self.last_usage = {
            "input_tokens": 10,
            "output_tokens": 10,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "translated chunk"


def test_oversized_full_text_chunks():
    prov = _CountingProvider()
    providers.register_provider("counter", prov)
    try:
        # Build a FULL_TEXT body well over MAX_CHUNK_TOKENS across paragraphs.
        # Each paragraph is ~1500 tokens (6000 chars); several of them force the
        # paragraph chunker to emit multiple chunks (MAX_CHUNK_TOKENS=4000).
        para = ("word " * 1500).strip()  # ~7500 chars ~ 1875 tokens
        body = "\n\n".join([para] * 6)  # ~11k tokens >> 4000
        with Session(engine) as session:
            p = Project(name="ft", source_locale="en", target_locales="es")
            session.add(p)
            session.commit()
            session.refresh(p)
            doc = Document(project_id=p.id, filename="big.txt", mode=TransMode.FULL_TEXT, fmt="txt")
            session.add(doc)
            session.commit()
            session.refresh(doc)
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
            spec = ProviderSpec(provider="counter")
            stats = {}
            for ev in translate.pretranslate_stream(session, doc.id, "es", spec=spec):
                if ev["type"] == "summary":
                    stats = ev
            seg = session.exec(
                translate.select(Segment).where(Segment.document_id == doc.id)
            ).first()
        assert prov.calls > 1, ("expected chunked (multi-call) full-text, got", prov.calls)
        assert stats["provider_calls"] == prov.calls, stats
        assert (seg.target_text or "").strip(), "chunked full-text produced no target"
    finally:
        providers._OVERRIDES.pop("counter", None)
    print("  (g) oversized FULL_TEXT routed through chunking OK")


if __name__ == "__main__":
    init_db()
    test_anthropic_cache_control_and_stable_prefix()
    test_prompt_caching_off_drops_marker()
    test_cache_usage_flows_into_stats()
    test_warm_then_fanout_ordering()
    test_stall_trips_failover()
    test_stall_watchdog_raises()
    test_truncation_not_accepted_no_partial_written()
    test_estimate_translation()
    test_oversized_full_text_chunks()
    print("PROVIDER RESILIENCE OK")
