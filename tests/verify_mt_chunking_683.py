"""An overlong segment is translated in pieces, never failed on the engine's output limit (#683).

The format soak's stress file `ass/stress/longseg` is one 6,000-character subtitle line wrapped
in a single italic span, so the segmenter keeps it as ONE segment. Into Japanese on the local
model its translation needs more than the 8,192-token output ceiling: every retry hit the
ceiling and the job failed ("output exceeds model ceiling 8192 tokens"). The fix lives in the
shared MT pipeline (`app/services/mt_chunking.py`, used by the plain pretranslate path and the
workflow segmented path): a segment whose generously estimated output exceeds the engine's
budget is cut at safe boundaries, translated piece by piece and rejoined with the #668 seam
rules; a segment no safe cut can fit goes to review with a reason; and a truncated engine
answer (any output-limit finish reason) is never saved as a translation.

  job        the soak shape through the real upload door + translate_document job: two engine
             requests, joined target (no space between the Japanese pieces), tags intact,
             Segment.meta["mt_chunks"] == 2, the job COMPLETES, export keeps {\\i1}
  unfit      a 6,000-character Japanese run with no space or punctuation: no request is sent,
             the segment goes to review with the engine-limit reason, the job does not fail
  workflow   the same two shapes through orchestrator.run_workflow (segmented llm_agent step)
  trunc      a local engine stream that ends with finish_reason "max_tokens" (not "length")
             is a truncation: run_completion returns no text, and the MT job leaves the
             segment untranslated instead of saving the cut-off answer
  plan       the planner itself: fits -> one piece, protected tokens never cut, CJK sentence
             cuts, balanced pieces, the engine's ceiling decides (a 64k-output model is not
             chunked), MT_CHUNK_OUTPUT_FILL=0 is the kill switch

Every engine is a local fake (no network, no spend). Final line: "MT CHUNKING 683 OK".
"""

import importlib
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_longseg683_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    JobStatus,
    Segment,
    SegStatus,
    StepType,
    Workflow,
    WorkflowStep,
)
from app.services import export, jobs, orchestrator, providers, providers_hub, quality
from app.services.providers import LocalProvider, Provider, ProviderSpec, register_provider
from app.services.providers_common import _raise_truncated

init_db()
_client = TestClient(app)
_N = [0]

LIMIT_CHARS = 4000  # the fake engine "hits its 8192-token ceiling" on a longer source
_TOKEN_RE = re.compile(r"«TAG[0-9a-f]+_\d+»|⟦/?[a-z]?\d+⟧")
JA_SENT = "これはテストです。"

SENTENCES = [
    "The quarterly report shows that the Kestrel team shipped twelve features and fixed "
    "forty bugs.",
    "Customers in three regions asked for faster exports, clearer error messages and "
    "offline support.",
    "Before the next release, the support desk will review every open ticket older than "
    "thirty days.",
    "Storage costs fell because old build artifacts are now removed automatically after two weeks.",
]
LONG_EN = "Kestrel overview. " + " ".join(SENTENCES[i % 4] for i in range(64))
assert 5500 < len(LONG_EN) < 7000, len(LONG_EN)
LONG_JA_RUN = "あ" * 6000  # no space, no punctuation: nothing to cut at


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


class _CeilingEngine(Provider):
    """A configured engine with a hard output ceiling. A source longer than LIMIT_CHARS
    raises the same truncation the real local engine does at its ceiling (budget == ceiling,
    so run_completion gives up with "output exceeds model ceiling"). Shorter sources come
    back as Japanese of a plausible length, with every tag token kept in place."""

    name = "ceilingfake"

    def __init__(self):
        self.calls: list[str] = []

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        text = (payload or {}).get("text", "")
        self.calls.append(text)
        if len(text) > LIMIT_CHARS:
            _raise_truncated(
                "fake finish_reason=length - output truncated",
                budget=config.model_output_ceiling(None),
                model=None,
                finish_reason="length",
            )
        half = len(text) / 2
        pre = "".join(m.group() for m in _TOKEN_RE.finditer(text) if m.start() < half)
        post = "".join(m.group() for m in _TOKEN_RE.finditer(text) if m.start() >= half)
        body = JA_SENT * max(1, len(_TOKEN_RE.sub("", text)) // 18)
        return pre + body + post


def _install(eng):
    names = sorted({"mock", (config.DEFAULT_PROVIDER or "mock").lower()})
    saved = {n: providers_hub._OVERRIDES.get(n) for n in names}
    for n in names:
        register_provider(n, eng)
    return saved


def _restore(saved):
    for n, prev in saved.items():
        if prev is None:
            providers_hub._OVERRIDES.pop(n, None)
        else:
            providers_hub._OVERRIDES[n] = prev


def _project(src, tgt):
    _N[0] += 1
    r = _client.post(
        "/projects",
        data={
            "name": f"longseg-{_N[0]}",
            "source_locale": src,
            "target_locales": tgt,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _upload(src, tgt, name, data: bytes) -> int:
    pid = _project(src, tgt)
    r = _client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": (name, data, "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])


def _ass(long_line: str) -> bytes:
    return (
        "[Script Info]\nTitle: Sample\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 480\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
        "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,"
        "0,1,2,2,2,10,10,10,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
        "MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,{\\i1}"
        + long_line
        + "{\\i0}\\NSecond row\n"
        "Dialogue: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,Short line.\n"
    ).encode("utf-8")


def _segs(doc_id, locale):
    with Session(engine) as s:
        return s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == locale)
            .order_by(Segment.order_index)
        ).all()


def _long_seg(doc_id, locale):
    segs = _segs(doc_id, locale)
    longs = [g for g in segs if len(g.source_text) > LIMIT_CHARS]
    if len(longs) != 1:
        _fail(f"expected one overlong segment, got {[len(g.source_text) for g in segs]}")
    return longs[0]


def _flag_reasons(seg):
    return [str(f.get("reason") or "") for f in (seg.qa_flags or []) if isinstance(f, dict)]


def _check_chunked(seg, eng, label):
    if seg.status != SegStatus.MT or not seg.target_text:
        _fail(f"{label}: the long segment was not translated: {seg.status} {seg.qa_flags}")
    if (seg.meta or {}).get("mt_chunks") != 2:
        _fail(f"{label}: Segment.meta must record the 2 pieces: {seg.meta!r}")
    if "。 " in seg.target_text:
        _fail(f"{label}: a space was put between the Japanese pieces (#668 seam rules)")
    if quality.tag_damage(seg.source_text, seg.target_text):
        _fail(f"{label}: tags damaged: {quality.tag_damage(seg.source_text, seg.target_text)}")
    if not (seg.target_text.startswith("⟦") and seg.target_text.endswith("⟧")):
        _fail(f"{label}: the italic pair must wrap the joined text: {seg.target_text[:40]!r}")
    long_calls = [c for c in eng.calls if len(c) > 1000]
    if len(long_calls) != 2 or any(len(c) > LIMIT_CHARS for c in long_calls):
        _fail(f"{label}: expected 2 piece requests under the limit: {[len(c) for c in eng.calls]}")


def test_job_soak_shape():
    doc_id = _upload("en-US", "ja-JP", "longseg.ass", _ass(LONG_EN))
    eng = _CeilingEngine()
    saved = _install(eng)
    try:
        job = jobs.run_sync("translate_document", {"document_id": doc_id, "locale": "ja-JP"})
    finally:
        _restore(saved)
    if job.status != JobStatus.COMPLETED:
        _fail(f"[job] the translate job must complete, got {job.status}: {job.error}")
    seg = _long_seg(doc_id, "ja-JP")
    _check_chunked(seg, eng, "[job]")
    with Session(engine) as s:
        out = export.build_translated_file(s, s.get(Document, doc_id), "ja-JP").decode("utf-8")
    if "{\\i1}" + JA_SENT not in out or "。 こ" in out:
        _fail(f"[job] export must keep the italic code and the joined text: {out[-300:]!r}")
    print("  [job] 6,000-char ASS line en->ja: 2 pieces, joined, tags kept, job completed OK")


def test_job_unfit_goes_to_review():
    doc_id = _upload("ja-JP", "en-US", "run.txt", (LONG_JA_RUN + "\n").encode("utf-8"))
    eng = _CeilingEngine()
    saved = _install(eng)
    try:
        job = jobs.run_sync("translate_document", {"document_id": doc_id, "locale": "en-US"})
    finally:
        _restore(saved)
    if job.status == JobStatus.FAILED:
        _fail(f"[unfit] the job must not fail on an unsplittable segment: {job.error}")
    seg = _long_seg(doc_id, "en-US")
    if seg.status != SegStatus.NEEDS_REVIEW or seg.target_text:
        _fail(f"[unfit] expected NEEDS_REVIEW with no target: {seg.status} {seg.target_text!r}")
    if not any(r.startswith("too long for machine translation:") for r in _flag_reasons(seg)):
        _fail(f"[unfit] the review flag must name the engine limit: {seg.qa_flags!r}")
    if any("ああああ" in c for c in eng.calls):
        _fail("[unfit] nothing should be sent to the engine for an unsplittable segment")
    print("  [unfit] 6,000-char unbroken ja run: review with reason, no request, job ok OK")


def _workflow():
    with Session(engine) as s:
        _N[0] += 1
        wf = Workflow(name=f"longseg-wf-{_N[0]}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="Translate",
                step_type=StepType.LLM_AGENT,
                provider="mock",
                prompt_text="Translate into {target}: {text}",
                retry_max=0,
                backoff_base=0.0,
            )
        )
        s.commit()
        return wf.id


def test_workflow_paths():
    doc_id = _upload("en-US", "ja-JP", "longseg.ass", _ass(LONG_EN))
    wf_id = _workflow()
    eng = _CeilingEngine()
    saved = _install(eng)
    try:
        with Session(engine) as s:
            orchestrator.run_workflow(s, wf_id, doc_id, "ja-JP")
    finally:
        _restore(saved)
    _check_chunked(_long_seg(doc_id, "ja-JP"), eng, "[workflow]")
    print("  [workflow] segmented llm_agent step: 2 pieces, joined, mt_chunks stamped OK")

    doc_id = _upload("ja-JP", "en-US", "run.txt", (LONG_JA_RUN + "\n").encode("utf-8"))
    wf_id = _workflow()
    eng = _CeilingEngine()
    saved = _install(eng)
    try:
        with Session(engine) as s:
            orchestrator.run_workflow(s, wf_id, doc_id, "en-US")
    finally:
        _restore(saved)
    seg = _long_seg(doc_id, "en-US")
    if seg.status != SegStatus.NEEDS_REVIEW or seg.target_text:
        _fail(f"[workflow unfit] expected review, no target: {seg.status} {seg.target_text!r}")
    if not any(r.startswith("too long for machine translation:") for r in _flag_reasons(seg)):
        _fail(f"[workflow unfit] the flag must name the engine limit: {seg.qa_flags!r}")
    if any("ああああ" in c for c in eng.calls):
        _fail("[workflow unfit] nothing should be sent to the engine")
    print("  [workflow] unsplittable segment: withheld to review with the engine-limit reason OK")


# --- truncation: an output-limit finish reason is never a complete translation --------------
def _sse_cut(text, finish):
    chunks = [
        {"choices": [{"delta": {"content": text}, "index": 0, "finish_reason": None}]},
        {"choices": [{"delta": {}, "index": 0, "finish_reason": finish}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 8192}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def _truncating_local(finish):
    config.LOCAL_BASE_URL = "http://127.0.0.1:1234"
    getattr(providers, "_OUTAGE_DEAD_UNTIL", {}).clear()
    p = LocalProvider()
    p._transport = httpx.MockTransport(lambda _req: _sse_cut("Bonjour, voici le début", finish))
    return p


def test_truncated_answer_is_never_a_translation():
    real_sleep = providers.time.sleep
    providers.time.sleep = lambda *_a: None
    try:
        for finish in ("length", "max_tokens", "MAX_OUTPUT_TOKENS"):
            p = _truncating_local(finish)
            orig = providers.get_provider
            providers.get_provider = lambda _name, _p=p: _p
            try:
                res = providers.run_completion(ProviderSpec("local", retry_max=1), "Translate: hi")
            finally:
                providers.get_provider = orig
            if res.get("text") or not res.get("error"):
                _fail(f"[trunc] finish_reason={finish!r} returned text as a success: {res!r}")
        print("  [trunc] finish length / max_tokens / MAX_OUTPUT_TOKENS: no text, an error OK")

        doc_id = _upload("en-US", "fr-FR", "short.txt", b"Hello there, how are you today?\n")
        saved = _install(_truncating_local("max_tokens"))
        try:
            jobs.run_sync("translate_document", {"document_id": doc_id, "locale": "fr-FR"})
        finally:
            _restore(saved)
    finally:
        providers.time.sleep = real_sleep
    for g in _segs(doc_id, "fr-FR"):
        if g.target_text or g.status == SegStatus.MT:
            _fail(f"[trunc] a cut-off answer was saved as a translation: {g.target_text!r}")
    print("  [trunc] MT job: a max_tokens-truncated answer leaves the segment untranslated OK")


# --- the planner -----------------------------------------------------------------------------
def test_planner():
    mc = importlib.import_module("app.services.mt_chunking")
    mock = ProviderSpec(provider="mock")
    p = mc.plan("Hello world.", mock, "ja-JP")
    assert p.pieces == ["Hello world."] and not p.chunked and not p.unfit
    long_it = "⟦g1⟧" + LONG_EN + "⟦/g1⟧"
    p = mc.plan(long_it, mock, "ja-JP")
    assert p.chunked and len(p.pieces) == 2, [len(x) for x in p.pieces]
    rebuilt = "".join(a + b for a, b in zip(p.pieces, [*p.seps, ""], strict=True))
    assert rebuilt == long_it, "pieces + separators must rebuild the source exactly"
    a, b = (len(x) for x in p.pieces)
    assert 0.5 < a / b < 2.0, ("pieces should be balanced", a, b)
    assert all(x.rstrip().endswith((".", "⟧")) for x in p.pieces), "cut at sentence ends"
    assert mc.plan(long_it, mock, "fr-FR").pieces == [long_it], "fr fits one request"
    big = ProviderSpec(provider="claude", model="claude-sonnet-4-5")
    assert mc.plan(long_it, big, "ja-JP", ceiling=64000).pieces == [long_it]
    # protected tokens: sentinels, ICU placeholders with spaces, URLs are never cut
    words = "word " * 1500
    for tok in ("«TAGabcdef12_3»", "{count, plural, one {# file} other {# files}}"):
        p = mc.plan(words + tok + " " + words, mock, "ja-JP")
        assert p.chunked and any(tok in x for x in p.pieces), (tok, "must stay whole")
    # a cut can only fall inside a token when the token is at the cut point: put one there
    half = "word " * 1400
    icu = "{n, plural, one {# file} other {# files}}"
    p = mc.plan(half + icu + " tail " + half, mock, "ja-JP")
    assert any(icu in x for x in p.pieces), [x[-50:] for x in p.pieces]
    # the only spaces sit inside a nested ICU placeholder or a sentinel: no cut is offered
    assert mc._cuts("a{n, plural, one {# x} other {# y}}b", mc._SPACE_RE) == []
    assert mc._cuts("x«PII_EMAIL_1» y", mc._SPACE_RE) != []  # a space outside is fine
    # CJK source: cut after 。 with no separator, rejoined with none
    ja = ("あ" * 300 + "。") * 20
    p = mc.plan(ja, mock, "en-US")
    assert p.chunked and all(s == "" for s in p.seps), p.seps
    assert p.join(["これは文。", "次の文。"] + ["x。"] * (len(p.pieces) - 2)).startswith(
        "これは文。次の文。"
    )
    # en -> ja join adds no space after a full-width stop; en -> fr keeps the source space
    p = mc.plan(long_it, mock, "ja-JP")
    assert p.join([JA_SENT, JA_SENT]) == JA_SENT + JA_SENT
    assert p.join(["Bonjour.", "Salut."]) == "Bonjour. Salut."
    # gate: a WORD-space cut (inside a sentence) between two Thai pieces gets no space (a Thai
    # space reads as a sentence break); a sentence-level seam keeps the source space
    no_stop = "word " * 6000
    p = mc.plan(no_stop.strip(), mock, "th-TH")
    assert p.chunked and set(p.levels) == {2}, p.levels
    th = "สวัสดีครับ"
    assert p.join([th] * len(p.pieces)) == th * len(p.pieces), p.join([th] * len(p.pieces))
    assert p.join(["Bonjour"] * len(p.pieces)) == " ".join(["Bonjour"] * len(p.pieces))
    p = mc.plan(("Sentence here. " * 900).strip(), mock, "th-TH")
    assert p.chunked and set(p.levels) == {0}, p.levels
    assert p.join([th] * len(p.pieces)) == " ".join([th] * len(p.pieces))
    # unsplittable
    p = mc.plan(LONG_JA_RUN, mock, "en-US")
    assert p.unfit and p.unfit_reason.startswith("too long for machine translation:"), p
    # PII literal protection (the workflow path redacts per piece)
    phone = "+1 (555) 010 2233 4455"
    text = words + phone + " " + words
    p = mc.plan(text, mock, "ja-JP", ceiling=None)
    pp = mc.Planner(mock, "ja-JP").plan(text, protect=[phone])
    assert any(phone in x for x in pp.pieces), "a protected literal must stay whole"
    del p
    # kill switch
    old = config.MT_CHUNK_OUTPUT_FILL
    config.MT_CHUNK_OUTPUT_FILL = 0
    try:
        assert mc.plan(long_it, mock, "ja-JP").pieces == [long_it]
    finally:
        config.MT_CHUNK_OUTPUT_FILL = old
    print("  [plan] fit/split/balance/protect/CJK seams/ceiling/unfit/kill switch OK")


if __name__ == "__main__":
    test_job_soak_shape()
    test_job_unfit_goes_to_review()
    test_workflow_paths()
    test_truncated_answer_is_never_a_translation()
    test_planner()
    print("MT CHUNKING 683 OK")
