"""Under-translation guard on the FULL_TEXT chunk loop (honest-failure, 2026-08-13).

THE BUG (big-doc flood): on the sentence-tuned local model qwen3.5-9b-loc-esfr-v1 a
FULL_TEXT run consumed 9,211 input tokens and emitted 74 output tokens (~one sentence
per chunk; char ratio ~0.008-0.016). The nonzero output slipped past the
`LocalEmptyOutput` / blank guards (`step_failed = not output`), the step committed
COMPLETED, and the run reported completed GREEN over a mostly-untranslated body.

THE FIX (orchestrator.py FULL_TEXT chunk loop): after `out_text` is computed for a
chunk, a char-length ratio guard `len(out_text.strip()) / max(1, len(eng_text))` trips
when the ratio falls below config.UNDER_TRANSLATION_MIN_RATIO (default 0.05) AND the
input is at least UNDER_TRANSLATION_MIN_INPUT_CHARS (default 500 - short chunks are
exempt). On a trip a dedicated UnderTranslationError is raised, caught alongside
DeadEngineError, any partial stitched body is discarded, and the whole step FAILS LOUD
-> the run is FAILED honestly (no green success-lie, no target write-back).

What this suite pins:
  1. A large FULL_TEXT chunk whose engine emits a tiny output -> run FAILED, StepRun
     FAILED with error starting `under-translation:`, NO target write-back, and the
     accrued spend stamps (cost_usd/tokens_in/tokens_out) preserved on the FAILED row.
  2. Floor exemption: a SHORT doc's tiny output is left untouched (legitimate).
  3. Kill switch: UNDER_TRANSLATION_MIN_RATIO=0 restores the old (pre-guard) behavior.
  4. A normal MockProvider run of a LARGE doc is unaffected (no false positive).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked). ASCII-only.
Final line: "UNDER TRANSLATION GUARD OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_undertrans_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, prompts, refusal

init_db()

_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


# A real, multi-sentence paragraph well over the 500-char floor - the shape the flood
# fed the model. The whole thing is ONE FULL_TEXT unit / one chunk (chunk_policy=none).
BIG_SRC = (
    "Our localization platform helps global studios ship their games in dozens of "
    "languages without slowing down the release train. Every string, every menu, and "
    "every line of dialogue flows through an automated pipeline that translates, "
    "reviews, and delivers finished text back to engineering in minutes rather than "
    "weeks. Native-speaking reviewers approve the exceptions, translation memory "
    "eliminates repeated work, and the glossary keeps product names consistent across "
    "the entire catalog. This paragraph is deliberately long so that the chunk it "
    "forms clears the under-translation input-character floor by a wide margin."
)
assert len(BIG_SRC) >= 500, len(BIG_SRC)
SMALL_SRC = "Ship the build today."  # well under the 500-char floor


class _TinyEngine:
    """Swap orchestrator.run_completion for a fake that returns a FIXED tiny output
    regardless of input (the sentence-tuned model's failure mode), plus a real usage +
    cost so the FAILED StepRun's spend stamps can be asserted preserved."""

    OUT = "Hola."  # 5 chars -> ratio ~0.005 against BIG_SRC
    COST = 0.123
    USAGE = {"input_tokens": 2000, "output_tokens": 5}  # noqa: RUF012

    def __enter__(self):
        self._orig = orchestrator.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            return {
                "text": self.OUT,
                "provider_used": "mock",
                "model_used": "mock",
                "attempts": 1,
                "error": None,
                "usage": dict(self.USAGE),
                "cost_usd": self.COST,
            }

        set_pieces("app.services.orchestrator", "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces("app.services.orchestrator", "run_completion", self._orig)
        return False


# A canned conversational refusal that is_refusal catches (leads with the hedge). Short,
# so it ALSO trips the ratio floor against BIG_SRC -> exercises the F2 seam where the
# refusal guard, NOT the ratio guard, must claim it.
HEDGE = (
    "I'm ready to translate. However, I don't see any source text. "
    "Please provide the actual source text you'd like me to translate."
)
assert refusal.is_refusal(HEDGE), "test fixture must itself be a detected refusal"


class _FixedEngine:
    """Swap run_completion for a fake returning a FIXED output (a hedge, or arbitrary
    text) regardless of input."""

    def __init__(self, out):
        self.out = out

    def __enter__(self):
        self._orig = orchestrator.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            return {
                "text": self.out,
                "provider_used": "mock",
                "model_used": "mock",
                "attempts": 1,
                "error": None,
                "usage": {},
                "cost_usd": 0.0,
            }

        set_pieces("app.services.orchestrator", "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces("app.services.orchestrator", "run_completion", self._orig)
        return False


def _mk_ft(source_text):
    with Session(engine) as s:
        p = Project(name="ut-%d" % _seq(), org_id=1, source_locale="en-US", default_provider="mock")  # noqa: UP031
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="es-ES"))
        s.commit()
        doc = Document(
            project_id=p.id,
            filename="d.docx",
            fmt="docx",
            mode=TransMode.FULL_TEXT,
            skeleton={"units": [{"p_index": 0, "text": source_text}], "seg_locale": "en-US"},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id, target_locale="es-ES", order_index=0, source_text=source_text
            )
        )
        s.commit()
        return doc.id


def _mk_wf(prompt="Translate: {text}", chunk_policy="none"):
    return _mk_wf_steps([("llm", prompt)], chunk_policy=chunk_policy)


def _mk_wf_steps(specs, chunk_policy="none"):
    """specs: a list of ("llm", prompt) / ("human", None) in order."""
    with Session(engine) as s:
        wf = Workflow(name="ut-wf-%d" % _seq(), scope="global")  # noqa: UP031
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for i, (kind, prompt) in enumerate(specs):
            if kind == "llm":
                s.add(
                    WorkflowStep(
                        workflow_id=wf.id,
                        order_index=i,
                        name="MT%d" % i,  # noqa: UP031
                        step_type=StepType.LLM_AGENT,
                        provider="mock",
                        prompt_text=prompt,
                        chunk_policy=chunk_policy,
                        retry_max=1,
                        backoff_base=0.0,
                    )
                )
            else:
                s.add(
                    WorkflowStep(
                        workflow_id=wf.id,
                        order_index=i,
                        name="Review%d" % i,  # noqa: UP031
                        step_type=StepType.HUMAN_REVIEW,
                    )
                )
        s.commit()
        return wf.id


# A multi-paragraph body large enough to split into >1 chunk (MAX_CHUNK_TOKENS=4000,
# est_tokens ~ chars/4, so >16000 chars per chunk). 40 x ~1000-char paragraphs.
_PARA = (
    "This is a full sentence of ordinary product prose repeated enough times to make "
    "the paragraph about one thousand characters long so the whole document splits "
    "across more than one translation chunk. " * 6
)
BIG_MULTI = "\n\n".join("Paragraph %02d. %s" % (i, _PARA) for i in range(40))  # noqa: UP031
assert len(BIG_MULTI) > 32000, len(BIG_MULTI)


class _ChunkSeqEngine:
    """First run_completion call returns a valid long translation (no trip); every LATER
    call returns the HEDGE (short -> ratio trips -> the refusal guard claims it). Each call
    stamps cost + usage so the ACCUMULATED spend across calls can be asserted."""

    COST = 0.10
    USAGE = {"input_tokens": 1000, "output_tokens": 500}  # noqa: RUF012

    def __init__(self):
        self.calls = 0

    def __enter__(self):
        self._orig = orchestrator.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            self.calls += 1
            text = ("Traduccion valida y completa. " * 100) if self.calls == 1 else HEDGE
            return {
                "text": text,
                "provider_used": "mock",
                "model_used": "mock",
                "attempts": 1,
                "error": None,
                "usage": dict(self.USAGE),
                "cost_usd": self.COST,
            }

        set_pieces("app.services.orchestrator", "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces("app.services.orchestrator", "run_completion", self._orig)
        return False


def _steprun(doc_id):
    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id == doc_id)
            .order_by(WorkflowRun.id.desc())
        ).first()
        srs = s.exec(select(StepRun).where(StepRun.workflow_run_id == run.id)).all()
        return run, srs


# --- (1) the trip: big chunk, tiny output -> FAILED LOUD ---------------------------
def test_under_translation_trips():
    doc_id, wf_id = _mk_ft(BIG_SRC), _mk_wf()
    with Session(engine) as s, _TinyEngine():
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "failed", summary
    assert (summary.get("error") or "").startswith("under-translation:"), summary
    # names step / pair / chunk / char counts / ratio, and is ASCII (CLI-safe).
    msg = summary["error"]
    assert "(en-US->es-ES)" in msg, msg
    assert "chunk 1/1" in msg, msg
    assert "UNDER_TRANSLATION_MIN_RATIO" in msg, msg
    assert msg.isascii(), msg

    run, srs = _steprun(doc_id)
    assert run.status == RunStatus.FAILED, run.status
    assert len(srs) == 1, srs
    sr = srs[0]
    assert sr.status == RunStatus.FAILED, sr.status
    assert (sr.error or "").startswith("under-translation:"), sr.error
    # spend stamps PRESERVED on the FAILED row (accrued BEFORE the guard raised).
    assert sr.cost_usd == _TinyEngine.COST, sr.cost_usd
    assert sr.tokens_in == _TinyEngine.USAGE["input_tokens"], sr.tokens_in
    assert sr.tokens_out == _TinyEngine.USAGE["output_tokens"], sr.tokens_out
    # F9: provider/model/attempts are hoisted ABOVE the guard, so a chunk-1 trip stamps
    # them on the FAILED row too (not left NULL).
    assert sr.provider_used == "mock" and sr.model_used == "mock", (sr.provider_used, sr.model_used)
    assert sr.attempts == 1, sr.attempts
    # NO target write-back on a FAILED run: the body stays untranslated, never the tiny
    # stitched output (and never a green deliverable).
    with Session(engine) as s:
        seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
        assert not seg.target_text, seg.target_text
    print("  (1) big chunk + tiny output -> run+step FAILED, under-translation: error,")
    print("      no target write-back, spend stamps preserved OK")


# --- (2) floor exemption: a SHORT doc's tiny output is legitimate ------------------
def test_short_doc_exempt():
    doc_id, wf_id = _mk_ft(SMALL_SRC), _mk_wf()
    with Session(engine) as s, _TinyEngine():
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
        assert (seg.target_text or "").strip(), seg.target_text
    print("  (2) short doc (input < floor): tiny output is legitimate, run COMPLETED OK")


# --- (3) kill switch: MIN_RATIO=0 restores the pre-guard behavior ------------------
def test_kill_switch():
    # config.UNDER_TRANSLATION_MIN_RATIO is read LIVE off config by the chunk loop, so
    # setting it to 0.0 here is exactly what the UNDER_TRANSLATION_MIN_RATIO=0 env seed
    # produces at import. The same big chunk that FAILED in (1) now completes.
    doc_id, wf_id = _mk_ft(BIG_SRC), _mk_wf()
    _orig = config.UNDER_TRANSLATION_MIN_RATIO
    config.UNDER_TRANSLATION_MIN_RATIO = 0.0
    try:
        with Session(engine) as s, _TinyEngine():
            summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    finally:
        config.UNDER_TRANSLATION_MIN_RATIO = _orig
    assert summary["status"] == "completed", summary
    print("  (3) kill switch (UNDER_TRANSLATION_MIN_RATIO=0): old behavior, run COMPLETED OK")


# --- (4) a normal MockProvider run of a LARGE doc is unaffected --------------------
def test_normal_mock_run_unaffected():
    # No engine swap: the real MockProvider echoes "[es-ES] <source>", so the output is
    # ~as long as the input (ratio ~1.0) -> no false positive on a real translation.
    doc_id, wf_id = _mk_ft(BIG_SRC), _mk_wf()
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "completed", summary
    with Session(engine) as s:
        seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
        assert "[es-ES]" in (seg.target_text or ""), seg.target_text
    print("  (4) normal MockProvider run of a large doc: no false positive, COMPLETED OK")


# --- (5) F2: a tiny output that IS a refusal is claimed by the REFUSAL guard, NOT
#         reclassified as under-translation -> withheld + NEEDS_REVIEW (main behavior). --
def test_hedge_routes_to_review_not_under_translation():
    doc_id, wf_id = _mk_ft(BIG_SRC), _mk_wf()
    with Session(engine) as s, _FixedEngine(HEDGE):
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    # NOT failed: the refusal guard withholds + routes to review; the run COMPLETES.
    assert summary["status"] == "completed", summary
    run, srs = _steprun(doc_id)
    assert run.status == RunStatus.COMPLETED, run.status
    assert len(srs) == 1 and srs[0].status == RunStatus.COMPLETED, srs
    # the hedge was NOT reclassified as under-translation.
    assert not (srs[0].error or "").startswith("under-translation:"), srs[0].error
    with Session(engine) as s:
        seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert not (seg.target_text or "").strip(), seg.target_text  # withheld
        assert [f for f in (seg.qa_flags or []) if f.get("type") == "refusal"], seg.qa_flags
    print("  (5) F2: a tiny HEDGE is claimed by the refusal guard (withheld + needs_review),")
    print("      NOT reclassified as under-translation OK")


# --- (6) F2: a prompt missing {text} + a hedge output -> the T1-01 MISSING_SOURCE_HINT
#         still surfaces (the refusal path is reached, not pre-empted by the ratio guard). -
def test_missing_source_hint_survives():
    doc_id = _mk_ft(BIG_SRC)
    wf_id = _mk_wf(prompt="Translate the document to Spanish now.")  # NO {text}
    with Session(engine) as s, _FixedEngine(HEDGE):
        orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    _run, srs = _steprun(doc_id)
    assert len(srs) == 1, srs
    assert srs[0].error == prompts.MISSING_SOURCE_HINT, srs[0].error
    print("  (6) F2: prompt missing {text} -> StepRun.error carries MISSING_SOURCE_HINT OK")


# --- (7) F3: a FAILED step never reaches a later human_review pause; the run fails at
#         once (no resume hole). ---------------------------------------------------------
def test_fail_fast_never_pauses():
    doc_id = _mk_ft(BIG_SRC)
    wf_id = _mk_wf_steps([("llm", "Translate: {text}"), ("human", None)])
    with Session(engine) as s, _TinyEngine():
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert summary["status"] == "failed", summary  # NOT "paused"
    _run, srs = _steprun(doc_id)
    assert len(srs) == 1, ("the human_review step must never run", srs)
    assert srs[0].status == RunStatus.FAILED, srs[0].status
    print(
        "  (7) F3: a failed step never reaches the later human_review pause; run FAILED at once OK"
    )


# --- (8) F3: a step that fails AFTER a resume still lands FAILED (terminal logic). ------
def test_failure_after_resume_is_failed():
    # [human_review -> llm_agent]: the run pauses at step 0; the resume seeds a BIG edited
    # body, step 1 translates it, the engine under-translates -> the run FAILS terminally.
    big_edit = (
        "Human edited body that is deliberately long, well over the five hundred "
        "character input floor, so that the translation step which runs after the "
        "resume clears the exemption and the under-translation ratio guard applies "
        "to it exactly as it would on a fresh run. It repeats enough real sentences "
        "to be unambiguous filler for the length requirement without tripping any "
        "other guard, and it is plain prose with no refusal markers anywhere. The "
        "reviewer expanded the draft with several more sentences of ordinary "
        "product prose so the character count clears the floor comfortably and the "
        "guard has no reason to exempt this resumed translation step at all."
    )
    assert len(big_edit) >= 500, len(big_edit)
    doc_id = _mk_ft(BIG_SRC)
    wf_id = _mk_wf_steps([("human", None), ("llm", "Translate: {text}")])
    with Session(engine) as s:
        paused = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert paused["status"] == "paused", paused
    run_id = paused["run_id"]
    with Session(engine) as s, _TinyEngine():
        resumed = orchestrator.resume_workflow(s, run_id, "es-ES", big_edit)
    assert resumed["status"] == "failed", resumed
    assert (resumed.get("error") or "").startswith("under-translation:"), resumed
    print("  (8) F3: a step failing AFTER a resume still lands FAILED terminally OK")


# --- (9b) F7-tests: a MULTI-CHUNK hedge -> earlier chunks discarded, withheld
#          disposition, and spend STILL summed across every call made. ----------------
def test_multi_chunk_hedge_discards_earlier_but_sums_spend():
    doc_id = _mk_ft(BIG_MULTI)
    wf_id = _mk_wf(chunk_policy="auto")  # chunking ON -> the body splits into >1 chunk
    eng = _ChunkSeqEngine()
    with Session(engine) as s, eng:
        summary = orchestrator.run_workflow(s, wf_id, doc_id, "es-ES")
    assert eng.calls >= 2, ("the body must split into >1 chunk", eng.calls)
    # chunk 0 translated fine, chunk 1 hedged -> the refusal guard claims the STEP: the run
    # COMPLETES with the withheld disposition (not an under-translation FAILED).
    assert summary["status"] == "completed", summary
    _run, srs = _steprun(doc_id)
    assert len(srs) == 1, srs
    sr = srs[0]
    assert sr.status == RunStatus.COMPLETED, sr.status
    assert not (sr.error or "").startswith("under-translation:"), sr.error
    # earlier chunk(s) discarded -> the persisted output is the withheld marker, and the
    # target is withheld + routed to review (never the chunk-0 translation).
    assert sr.output_text == refusal.WITHHELD_OUTPUT, sr.output_text
    with Session(engine) as s:
        seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert not (seg.target_text or "").strip(), seg.target_text
    # spend is SUMMED across EVERY call made (both chunks), even though the loop broke.
    assert abs(sr.cost_usd - _ChunkSeqEngine.COST * eng.calls) < 1e-9, (sr.cost_usd, eng.calls)
    assert sr.tokens_in == _ChunkSeqEngine.USAGE["input_tokens"] * eng.calls, sr.tokens_in
    assert sr.tokens_out == _ChunkSeqEngine.USAGE["output_tokens"] * eng.calls, sr.tokens_out
    print("  (9b) F7: multi-chunk hedge -> earlier chunk discarded, withheld+needs_review,")
    print("       spend summed across all %d calls OK" % eng.calls)  # noqa: UP031


# --- (9) F7: tolerant config parsing (junk -> default; off-words -> 0; clamp + floor). --
def test_config_parse_is_tolerant():
    import os as _os

    cases_ratio = {
        "0.2": 0.2,
        "0": 0.0,
        "off": 0.0,
        "false": 0.0,
        "no": 0.0,
        "garbage": 0.05,
        "1.5": 0.05,
        "-0.1": 0.05,
        "": 0.05,
    }
    for raw, want in cases_ratio.items():
        _os.environ["UNDER_TRANSLATION_MIN_RATIO"] = raw
        got = config._under_translation_min_ratio()
        assert got == want, (raw, got, want)
    _os.environ.pop("UNDER_TRANSLATION_MIN_RATIO", None)
    cases_chars = {"800": 800, "0": 0, "-5": 0, "garbage": 500, "": 500}
    for raw, want in cases_chars.items():
        _os.environ["UNDER_TRANSLATION_MIN_INPUT_CHARS"] = raw
        got = config._under_translation_min_input_chars()
        assert got == want, (raw, got, want)
    _os.environ.pop("UNDER_TRANSLATION_MIN_INPUT_CHARS", None)
    print(
        "  (9) F7: config parse tolerates junk (ratio->0.05, off-words->0, clamp; chars->500, floor 0) OK"  # noqa: E501
    )


def test_over_translation_config_parse_and_shape():
    """run-190 Item D: the OVER-TRANSLATION ceiling knob is the symmetric twin of the
    UNDER-TRANSLATION floor — junk-tolerant parse, "0"/off-words are the kill switch — and
    the classic shape helper rejects a page dump while passing a real translation."""
    import os as _os

    from app.services import orchestrator

    cases = {
        "8.0": 8.0,
        "10": 10.0,
        "0": 0.0,
        "0.0": 0.0,
        "off": 0.0,
        "false": 0.0,
        "no": 0.0,
        "garbage": 8.0,
        "-3": 8.0,
        "": 8.0,
    }
    for raw, want in cases.items():
        _os.environ["OVER_TRANSLATION_MAX_RATIO"] = raw
        got = config._over_translation_max_ratio()
        assert got == want, (raw, got, want)
    _os.environ.pop("OVER_TRANSLATION_MAX_RATIO", None)

    # shape helper: a page dump (fence / cross-language table / multi-line-from-single-line /
    # far-too-long) is rejected; a plausible single-unit target passes.
    _saved = config.OVER_TRANSLATION_MAX_RATIO
    config.OVER_TRANSLATION_MAX_RATIO = 8.0
    try:
        ok, _ = orchestrator._looks_like_unit_translation("Reservar", "Book")
        assert ok, "a clean single-unit target must pass"
        ok, why = orchestrator._looks_like_unit_translation("```\nReservar\n```", "Book")
        assert not ok and why == "code_fence", why
        ok, why = orchestrator._looks_like_unit_translation(
            "| en | es |\n|---|---|\n| a | b |\n| c | d |", "Book"
        )
        assert not ok and why == "markdown_table", why
        ok, why = orchestrator._looks_like_unit_translation("line one\nline two", "Book")
        assert not ok and why == "multi_line", why
        ok, why = orchestrator._looks_like_unit_translation("x" * 500, "Book")
        assert not ok and why == "too_long", why
        # a multi-line SOURCE legitimately yields multi-line output (not a dump)
        ok, _ = orchestrator._looks_like_unit_translation("uno\ndos", "one\ntwo")
        assert ok, "multi-line source -> multi-line output is not a page dump"
        # kill switch: ratio 0 -> the length ceiling never trips (caller also skips entirely)
        config.OVER_TRANSLATION_MAX_RATIO = 0.0
        ok, _ = orchestrator._looks_like_unit_translation("x" * 5000, "Book")
        assert ok, "ratio 0 disables the length ceiling"
    finally:
        config.OVER_TRANSLATION_MAX_RATIO = _saved
    print("  (10) Item D: OVER_TRANSLATION_MAX_RATIO parse + shape helper OK")


if __name__ == "__main__":
    test_under_translation_trips()
    test_short_doc_exempt()
    test_kill_switch()
    test_normal_mock_run_unaffected()
    test_hedge_routes_to_review_not_under_translation()
    test_missing_source_hint_survives()
    test_fail_fast_never_pauses()
    test_failure_after_resume_is_failed()
    test_multi_chunk_hedge_discards_earlier_but_sums_spend()
    test_config_parse_is_tolerant()
    test_over_translation_config_parse_and_shape()
    print("UNDER TRANSLATION GUARD OK")
