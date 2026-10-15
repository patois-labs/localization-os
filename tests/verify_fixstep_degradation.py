"""GitHub #45 - the correction-loop FIX STEP: a bounded per-segment findings block,
and per-segment (not whole-run) failure on a provider error.

Prod run #74 failed at the fix step with "Context size has been exceeded": the
whole-document LQA findings block was injected into EVERY segment's fix prompt, and
the resulting APIError on ONE segment failed the entire run, discarding every locale
value that had already been produced.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked).

  [1] the per-segment findings block respects the cap, says so when it truncated, and
      PREFERS findings whose quoted text overlaps the segment. Under the cap it is
      byte-identical (no reordering, no marker). Every truncation path keeps the closing
      </lqa_findings> fence, and relevance matching is WORD-BOUNDED.
  [2] the cap is enforced at the real per-segment prompt seam (_run_step_on_segment).
  [3] a provider error on ONE segment's fix call: the run COMPLETES, that segment keeps
      its pre-fix draft + NEEDS_REVIEW + a step_error qa_flag, and its siblings commit
      their fixed text untouched.
  [4] a provider error on EVERY segment fails the run wholesale ONLY when the error CLASS
      is structural (connection/engine level) - the all-or-nothing write-back stands
      there, because nothing was reachable. The same failure with a REQUEST-shaped error
      completes and delivers every pre-step draft flagged, at any document size: there is
      no "enough segments failed" size arm (re-review N3). The classifier is pinned by a
      two-directional message corpus (N4).
  [5] ...but a ONE- or TWO-segment pending set failing on a REQUEST-shaped error does NOT
      kill the run (adversarial review P3). `pending_idx` is a POST-FILTER set, so a
      correction loop routinely offers the fix step a single segment - and "every pending
      segment failed" then meant "the only one failed", re-creating prod #74 inside its
      own fix. Two shapes: a single-segment document, and a TM-protected correction loop
      whose one unprotected segment errors (the 100% TM match must survive intact).
  [6] a chunked segment whose SECOND chunk errors (output non-empty AND error set)
      degrades too - the partial output is discarded, the pre-step text kept (P10).
  [7] step 0 has no pre-step translation to keep, so a provider error there still FAILS
      the run and writes NO target - never the source as a target (P7, mutation M2).
  [8] the degradation record SURVIVES a human_review pause/resume (P1). It is run-local
      state and the write-back runs after the resume, so it used to be silently dropped
      exactly when a human looked at the document.
  [9] a CLEAN re-run CLEARS a stale step_error badge (P4, the stale-flag class).
 [10] degradation is reported as STRUCTURED telemetry, not only as English prose: the
      step event's lqa_safety slot and the run-level `done` rollup (P8).
 [11] a degraded segment is never sent to the QE judge (P9) - there is no output to
      score, and a bogus gate verdict would badge a string that was never committed.
 [12] the step_error flag is VISIBLE on every review surface: the editor grid, the
      headless per-segment detail row, and the review summary line (P2).
 [13] TWO pauses: a segment degraded before pause 1 and REPAIRED before pause 2 lands
      clean - the persisted record is written THROUGH, not merely written once (N1).
 [14] a later step succeeding on a degraded segment clears its record INSIDE the run, on
      both the concurrent and the serial dispatch branch (mutation M6).

Final line: "FIXSTEP DEGRADATION OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_fixstep_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    RunStatus,
    Segment,
    SegStatus,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import content, orchestrator, quality
from app.services import workflows as wf

init_db()

SRC, TGT = "en-US", "fr-FR"
SENTENCES = [
    "The user opens the settings page.",
    "The export finished with two warnings.",
    "Delete the archive when you are done.",
]


def _big_findings(n=40):
    """A whole-document findings block in lqa.render_findings' shape, big enough to
    blow a small local context window when injected into every segment prompt."""
    lines = ['<lqa_findings score="61" gate="failed">']
    for i in range(1, n + 1):
        lines.append(
            '%d. [Accuracy/major] the phrase "widget number %d" was rendered '  # noqa: UP031
            "literally and loses the intended sense entirely" % (i, i)
        )
    lines.append("</lqa_findings>")
    return "\n".join(lines)


# --- [1] the bounding seam ----------------------------------------------------
def test_bound_findings():
    cap = orchestrator.LQA_FINDINGS_SEGMENT_CAP
    block = _big_findings()
    assert len(block) > cap, len(block)

    out = orchestrator._bound_findings_block(block, "an unrelated sentence", "")
    assert len(out) <= cap, (len(out), cap)
    assert out.startswith("<lqa_findings") and out.rstrip().endswith("</lqa_findings>")
    assert "omitted" in out, out  # honest: the model is TOLD findings were elided

    # RELEVANCE: a finding whose quoted text is in THIS segment survives even though
    # it sits far past the cap in document order.
    seg = "Check widget number 39 before shipping."
    out2 = orchestrator._bound_findings_block(block, seg, "")
    assert len(out2) <= cap, len(out2)
    assert "widget number 39" in out2, out2

    # ...and the current TARGET counts as overlap too, not just the source.
    out3 = orchestrator._bound_findings_block(block, "unrelated", "verifiez widget number 37 svp")
    assert "widget number 37" in out3, out3

    # BYTE-IDENTICAL under the cap: no reordering, no marker, nothing.
    small = _big_findings(2)
    assert len(small) <= cap
    assert orchestrator._bound_findings_block(small, "anything", "") == small
    assert orchestrator._bound_findings_block("", "a", "b") == ""

    # P12a: EVERY truncation path keeps the closing fence. The block is injection-fenced
    # and the prompt puts trusted instructions AFTER it, so an unterminated fence is a
    # structural break, not a cosmetic one. These are the degenerate shapes that used to
    # fall through to a bare `[:cap]`.
    degenerate = [
        ("two lines", '<lqa_findings score="1">\n' + "x" * 4000 + "\n</lqa_findings>"),
        ("huge head", "<lqa_findings " + "a" * 3000 + ">\n1. [A/major] hi\n</lqa_findings>"),
        (
            "huge meta",
            '<lqa_findings score="1">\n(note ' + "m" * 3000 + ")\n1. [A/major] hi\n</lqa_findings>",
        ),
    ]
    for label, blk in degenerate:
        got = orchestrator._bound_findings_block(blk, "s", "t")
        assert len(got) <= cap, (label, len(got))
        assert got.rstrip().endswith("</lqa_findings>"), (label, got[-80:])
    # ...and a fence-less blob is still just truncated (nothing to preserve).
    assert len(orchestrator._bound_findings_block("y" * 4000, "s", "t")) <= cap

    # P12b: relevance is WORD-BOUNDED - a finding about "widget number 3" is not about a
    # segment containing "widget number 39". A false match evicts a finding that really
    # was about this segment, since relevance decides what survives the cap.
    line3 = '3. [Accuracy/major] the phrase "widget number 3" was rendered literally'
    assert orchestrator._findings_relevant(line3, "Check widget number 3 now.", "")
    assert not orchestrator._findings_relevant(line3, "Check widget number 39.", "")
    print(
        "  [1] findings block bounded to %d chars, truncation announced, quoted-text "  # noqa: UP031
        "overlap preferred, byte-identical under the cap; every truncation path keeps "
        "the closing fence; relevance is word-bounded" % cap
    )


# --- [2] the cap at the real per-segment prompt seam --------------------------
def test_cap_at_the_prompt_seam():
    seen = []
    real = orchestrator.run_completion

    def _capture(spec, prompt, **kw):
        seen.append(prompt)
        return {
            "text": "corrige",
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    block = _big_findings()
    step = WorkflowStep(
        workflow_id=0, name="Fix", step_type=StepType.LLM_AGENT, order_index=1, chunk_policy="none"
    )
    set_pieces("app.services.orchestrator", "run_completion", _capture)
    try:
        orchestrator._run_step_on_segment(
            _spec(),
            "Fix this: {text}",
            {
                "source_locale": SRC,
                "target_locale": TGT,
                "context": "",
                "style_guide": "",
                "lqa_findings": block,
            },
            step,
            SENTENCES[0],
            "Le user ouvre la page.",
            TGT,
            [],
            False,
            findings_suffix=orchestrator._LQA_FINDINGS_PREAMBLE + block,
        )
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    assert seen, "no prompt composed"
    body = seen[0]
    assert "<lqa_findings" in body, body
    inner = body[body.index("<lqa_findings") :]
    assert len(inner) <= orchestrator.LQA_FINDINGS_SEGMENT_CAP + 64, len(inner)
    assert len(inner) < len(block), (len(inner), len(block))
    print(
        "  [2] the per-segment fix prompt carries a bounded block (%d chars, not %d)"  # noqa: UP031
        % (len(inner), len(block))
    )


def _spec():
    from app.services.providers import ProviderSpec

    return ProviderSpec(provider="mock", model="mock-1")


# --- [3]/[4] graceful per-segment degradation ---------------------------------
def _doc(name):
    with Session(engine) as s:
        p = content.project_create(
            s, name, source_locale=SRC, target_locales=TGT, default_provider="mock"
        )
        doc = Document(
            project_id=p.id, filename=name + ".txt", mode=TransMode.SEGMENTED, source_locale=SRC
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, text in enumerate(SENTENCES):
            s.add(
                Segment(
                    document_id=doc.id,
                    order_index=i,
                    source_text=text,
                    target_locale=TGT,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return doc.id


def _two_step_wf(name):
    with Session(engine) as s:
        w = Workflow(name=name, scope="global", source_locale=SRC, target_locale=TGT)
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(
            s,
            w.id,
            name="Fix",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Fix into {target_locale}: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        return w.id


def _run(doc_id, wf_id, timeout=120):
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wf_id, doc_id, TGT, created_by="test")
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            r = s.get(WorkflowRun, rid)
            if r is not None and r.status in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.PAUSED,
            ):
                return r.status
        time.sleep(0.05)
    raise AssertionError("run never finished")


def _rows(doc_id):
    with Session(engine) as s:
        rows = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == TGT)
        ).all()
        return sorted(rows, key=lambda x: x.order_index)


def _erroring_completion(fail_texts):
    """A run_completion stub that ERRORS (as providers.run_completion does on an
    exhausted retry chain: text="" + a populated `error`) for the listed inputs."""

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if any(t in text for t in fail_texts):
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": (
                    "APIError: Engine protocol predict stream returned an "
                    "error: Context size has been exceeded."
                ),
            }
        return {
            "text": "FIXED " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    return _stub


def test_one_segment_fails():
    doc_id, wf_id = _doc("degrade-one"), _two_step_wf("WF-degrade-one")
    real = orchestrator.run_completion
    calls = {"n": 0}

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        calls["n"] += 1
        # step 0 translates everything; step 1 (the fix) errors on ONE segment only.
        if text.startswith("DRAFT ") and "export finished" in text:
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": (
                    "APIError: Engine protocol predict stream returned an "
                    "error: Context size has been exceeded."
                ),
            }
        if text.startswith("DRAFT "):
            return {
                "text": "FIXED " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    try:
        status = _run(doc_id, wf_id)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    assert status == RunStatus.COMPLETED, status
    rows = _rows(doc_id)
    hurt = [r for r in rows if "export finished" in (r.source_text or "")]
    ok = [r for r in rows if "export finished" not in (r.source_text or "")]
    assert len(hurt) == 1 and len(ok) == 2, [r.source_text for r in rows]
    h = hurt[0]
    # the PRE-FIX draft is kept (not blanked, not the source)
    assert h.target_text == "DRAFT " + h.source_text, h.target_text
    assert h.status == SegStatus.NEEDS_REVIEW, h.status
    flags = [f.get("type") for f in (h.qa_flags or []) if isinstance(f, dict)]
    assert quality.STEP_ERROR_FLAG_TYPE in flags, flags
    msg = [  # noqa: RUF015
        f.get("message")
        for f in (h.qa_flags or [])
        if isinstance(f, dict) and f.get("type") == quality.STEP_ERROR_FLAG_TYPE
    ][0]
    assert "Fix" in msg, msg  # the flag NAMES the step that failed
    # ...and the siblings are entirely unaffected.
    for r in ok:
        assert r.target_text == "FIXED DRAFT " + r.source_text, r.target_text
        assert r.status == SegStatus.MT, r.status
        f = [x.get("type") for x in (r.qa_flags or []) if isinstance(x, dict)]
        assert quality.STEP_ERROR_FLAG_TYPE not in f, f
    print(
        "  [3] one failing fix call -> run COMPLETED, that segment keeps its draft + "
        "NEEDS_REVIEW + step_error; 2 siblings committed clean"
    )


def test_every_segment_fails():
    """The CLASS arm at scale: every segment of a 3-segment doc fails with an engine-
    level message. Nothing was reachable, so there is no completed work to keep and the
    all-or-nothing write-back stands."""
    doc_id, wf_id = _doc("degrade-all"), _two_step_wf("WF-degrade-all")
    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if text.startswith("DRAFT "):
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: engine unreachable",
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    try:
        status = _run(doc_id, wf_id)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    assert status == RunStatus.FAILED, status
    # all-or-nothing write-back stands: a wholesale failure writes NO targets.
    for r in _rows(doc_id):
        assert not (r.target_text or "").strip(), r.target_text
    print(
        "  [4a] EVERY fix call failing with an ENGINE-level error (3-segment doc) -> "
        "run FAILED, no partial write-back (all-or-nothing preserved)"
    )


def test_every_segment_fails_on_a_request_error_still_completes():
    """re-review N3, and the whole reason the SIZE arm is gone.

    The verbatim prod #74 message, failing on EVERY segment of a 3-segment document. The
    old size arm (>=3 pending all failed -> escalate) made this identical failure behave
    two different ways depending on document length: 2 segments completed with both
    drafts kept, 3 segments failed the run and discarded every draft - prod #74 exactly,
    reachable by adding one segment, and untested (a threshold mutation to 999 survived).

    A context overflow that hits every segment IS a shared cause - the injected block is
    too big for this engine - but it is a REQUEST-shaped one, and delivering every
    pre-step draft flagged for review is strictly more than delivering nothing."""
    PROD_74 = (
        "APIError: Engine protocol predict stream returned an error: "
        "Context size has been exceeded."
    )
    for label, doc_id, wf_id in (
        ("3-segment", _doc("all-req-3"), _two_step_wf("WF-all-req-3")),
        ("1-segment", _one_seg_doc("all-req-1"), _two_step_wf("WF-all-req-1")),
    ):
        real = orchestrator.run_completion

        def _stub(spec, prompt, **kw):
            text = (kw.get("payload") or {}).get("text") or ""
            if text.startswith("DRAFT "):
                return {
                    "text": "",
                    "provider_used": None,
                    "model_used": None,
                    "attempts": 1,
                    "cost_usd": 0.0,
                    "usage": None,
                    "error": PROD_74,
                }
            return {
                "text": "DRAFT " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }

        set_pieces("app.services.orchestrator", "run_completion", _stub)
        try:
            status = _run(doc_id, wf_id)
        finally:
            set_pieces("app.services.orchestrator", "run_completion", real)
        assert status == RunStatus.COMPLETED, (label, status)
        rows = _rows(doc_id)
        assert rows, label
        for r in rows:
            # EVERY draft is delivered - nothing discarded...
            assert r.target_text == "DRAFT " + r.source_text, (label, r.target_text)
            # ...and every one of them says why it never got its fix.
            assert r.status == SegStatus.NEEDS_REVIEW, (label, r.status)
            f = [x.get("type") for x in (r.qa_flags or []) if isinstance(x, dict)]
            assert quality.STEP_ERROR_FLAG_TYPE in f, (label, f)
    print(
        "  [4c] EVERY segment failing on the verbatim prod #74 request error -> run "
        "COMPLETES with every draft kept + flagged, at BOTH document sizes (the size "
        "arm is gone; document length no longer changes the outcome)"
    )


def test_structural_error_escalates_on_one_segment():
    """The CLASS arm of the narrowed escalation: a connection-level failure says "the
    engine is down" on a sample of ONE, so a single-segment doc still fails wholesale."""
    doc_id, wf_id = _one_seg_doc("struct-1"), _two_step_wf("WF-struct-1")
    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if text.startswith("DRAFT "):
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": (
                    "ConnectionError: HTTPConnectionPool(host='127.0.0.1', "
                    "port=1234): Max retries exceeded - connection refused"
                ),
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    try:
        status = _run(doc_id, wf_id)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    assert status == RunStatus.FAILED, status
    for r in _rows(doc_id):
        assert not (r.target_text or "").strip(), r.target_text
    # ...and the predicate itself, as a two-directional CORPUS. Every false POSITIVE here
    # discards a whole locale, so the two directions are not equally costly and the
    # negative half is the load-bearing one (re-review N4).
    STRUCTURAL = [
        "ConnectionError: connection refused",
        "dead engine ref engine:7",
        (
            "ConnectionError: HTTPConnectionPool(host='127.0.0.1', port=1234): "
            "Max retries exceeded with url: /v1/chat (Caused by "
            "NewConnectionError('Connection refused'))"
        ),
        "OSError: [Errno 101] Network is unreachable",
        "the engine is unreachable from this host",
        "socket.gaierror: [Errno -2] Name or service not known",
        "ConnectTimeout: connection timed out after 30s",
        "http.client.RemoteDisconnected: Remote end closed connection",
        "ssl.SSLError: certificate verify failed",
    ]
    REQUEST_SHAPED = [
        "APIError: Context size has been exceeded.",
        "429 rate limit exceeded",
        # N4 (a): urllib3's phrasing for retry exhaustion on 429/503. This is rate
        # limiting / transient load - REQUEST-shaped - and the old bare `max retries
        # exceeded` alternative classified it as a dead engine.
        (
            "HTTPSConnectionPool(host='api.example.com', port=443): Max retries exceeded "
            "with url: /v1/messages (Caused by ResponseError('too many 429 error "
            "responses'))"
        ),
        (
            "HTTPSConnectionPool(host='api.example.com', port=443): Max retries exceeded "
            "with url: /v1/messages (Caused by ResponseError('too many 503 error "
            "responses'))"
        ),
        # N4 (b): a failing step's error echoes the SEGMENT it choked on, and a document
        # can be about network troubleshooting. Bare `unreachable` discarded its locale.
        (
            "APIError: invalid request for input 'If the printer is unreachable, restart "
            "the router and try again.'"
        ),
        "400 Bad Request: prompt too long",
        "AuthenticationError: invalid api key",
        "",
        None,
    ]
    for msg in STRUCTURAL:
        assert orchestrator._is_structural_error(msg), ("must escalate", msg)
    for msg in REQUEST_SHAPED:
        assert not orchestrator._is_structural_error(msg), ("must DEGRADE", msg)
    print(
        "  [4b] a STRUCTURAL (connection-level) error escalates on a sample of one -> "  # noqa: UP031
        "run FAILED; corpus of %d structural / %d request-shaped messages classified "
        "correctly in both directions" % (len(STRUCTURAL), len(REQUEST_SHAPED))
    )


# --- [5] THE NARROWED ESCALATION (adversarial review P3) ----------------------
def _one_seg_doc(name, text=None):
    with Session(engine) as s:
        p = content.project_create(
            s, name, source_locale=SRC, target_locales=TGT, default_provider="mock"
        )
        doc = Document(
            project_id=p.id, filename=name + ".txt", mode=TransMode.SEGMENTED, source_locale=SRC
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                order_index=0,
                source_text=text or SENTENCES[0],
                target_locale=TGT,
                status=SegStatus.NEW,
            )
        )
        s.commit()
        return doc.id


def test_single_pending_segment_degrades():
    """P3, the reviewer's case F. One segment, the fix call for it errors. Before the
    narrowing this was 1 degraded == 1 pending -> escalation -> run FAILED -> the
    document delivered NOTHING, over one bad fix prompt."""
    doc_id, wf_id = _one_seg_doc("degrade-solo"), _two_step_wf("WF-degrade-solo")
    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if text.startswith("DRAFT "):
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: Context size has been exceeded.",
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    try:
        status = _run(doc_id, wf_id)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    assert status == RunStatus.COMPLETED, status
    row = _rows(doc_id)[0]
    assert row.target_text == "DRAFT " + row.source_text, row.target_text
    assert row.status == SegStatus.NEEDS_REVIEW, row.status
    flags = [f.get("type") for f in (row.qa_flags or []) if isinstance(f, dict)]
    assert quality.STEP_ERROR_FLAG_TYPE in flags, flags
    print(
        "  [5a] a SINGLE pending segment failing on a request-shaped error degrades "
        "(run COMPLETED, draft kept) instead of killing the run"
    )


def _lqa_stub(passed=False):
    """A context manager over lqa.score_text so a correction loop's guard fires."""
    from app.services import lqa

    class _Ctx:
        def __enter__(self):
            self._orig = lqa.score_text

            def _fake(*a, **kw):
                return {
                    "score": 95.0 if passed else 40.0,
                    "passed": passed,
                    "errors": [
                        {"dimension": "Terminology", "severity": "major", "note": "term issue"}
                    ],
                    "gate": "auto_approved" if passed else "needs_review",
                    "cost_usd": 0.0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                }

            lqa.score_text = _fake
            return self

        def __exit__(self, *e):
            lqa.score_text = self._orig
            return False

    return _Ctx()


def test_tm_protected_loop_keeps_the_tm_segment():
    """P3, the reviewer's case A - the shape prod #74 actually had.

    A correction loop: translate -> LQA (fails) -> fix (guarded on the LQA). The doc's
    first segment is a 100% TM match, so the TM PROVENANCE GUARD drops it from the fix
    step's pending set; the ONE segment left errors. Pre-narrowing that read as "every
    pending segment failed", failed the run, and the all-or-nothing write-back discarded
    the customer-approved TM target along with everything else."""
    from app.models import GlossaryEntry, SegmentTM
    from app.services import tm

    TM_SRC = "Welcome to the Widget store."
    TM_TGT = "Bienvenue dans la boutique Aparato."
    OTHER = "Buy the Widget now."
    with Session(engine) as s:
        p = content.project_create(
            s, "tm-loop", source_locale=SRC, target_locales=TGT, default_provider="mock"
        )
        s.add(
            GlossaryEntry(
                project_id=p.id,
                source_term="Widget",
                target_term="Aparato",
                source_locale=SRC,
                target_locale=TGT,
            )
        )
        s.add(
            SegmentTM(
                project_id=p.id,
                source_locale=SRC,
                target_locale=TGT,
                source_text=TM_SRC,
                target_text=TM_TGT,
                source_hash=tm.hash_text(TM_SRC, SRC, TGT),
            )
        )
        s.commit()
        doc = Document(
            project_id=p.id, filename="tm-loop.txt", mode=TransMode.SEGMENTED, source_locale=SRC
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, text in enumerate([TM_SRC, OTHER]):
            s.add(
                Segment(
                    document_id=doc.id,
                    order_index=i,
                    source_text=text,
                    target_locale=TGT,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        doc_id = doc.id
    with Session(engine) as s:
        w = Workflow(name="WF-tm-loop", scope="global", source_locale=SRC, target_locale=TGT)
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(s, w.id, name="LQA", step_type=StepType.LQA, provider="mock")
        wf.add_step(
            s,
            w.id,
            name="Fix",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="FIX into {target_locale}: {text}",
            chunk_policy="none",
            tm_examples=False,
            run_condition={"ref": "prev_lqa", "op": "failed", "value": None},
        )
        wf_id = w.id

    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        if "FIX into" in prompt:
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: Context size has been exceeded.",
            }
        text = (kw.get("payload") or {}).get("text") or ""
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    try:
        with _lqa_stub(passed=False):
            status = _run(doc_id, wf_id)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    assert status == RunStatus.COMPLETED, status
    rows = _rows(doc_id)
    # THE POINT: the customer-approved 100% TM target is still there, untouched.
    assert rows[0].target_text == TM_TGT, rows[0].target_text
    assert rows[0].tm_match_pct == 100, rows[0].tm_match_pct
    # ...and the one segment whose fix call failed kept its draft and is flagged.
    assert rows[1].target_text == "DRAFT " + OTHER, rows[1].target_text
    assert rows[1].status == SegStatus.NEEDS_REVIEW, rows[1].status
    f = [x.get("type") for x in (rows[1].qa_flags or []) if isinstance(x, dict)]
    assert quality.STEP_ERROR_FLAG_TYPE in f, f
    print(
        "  [5b] TM-protected correction loop, the fix step's ONLY pending segment "
        "fails -> run COMPLETED and the 100% TM target survives intact"
    )


# --- [6] the CHUNKED partial-output case (P10) --------------------------------
def test_chunked_partial_output_degrades():
    """A segment whose chunk 1 succeeded and chunk 2 errored comes back with output
    NON-EMPTY and error SET. That output is a truncated translation no caller can tell
    from a complete one - and folding it into the step's error failed the whole run. It
    now degrades: the partial text is discarded and the pre-step text kept."""
    from app import config

    PARA = "First paragraph here.\n\nSecond paragraph here."
    with Session(engine) as s:
        p = content.project_create(
            s, "chunked", source_locale=SRC, target_locales=TGT, default_provider="mock"
        )
        doc = Document(
            project_id=p.id, filename="chunked.txt", mode=TransMode.SEGMENTED, source_locale=SRC
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, text in enumerate([PARA, "A single short sentence."]):
            s.add(
                Segment(
                    document_id=doc.id,
                    order_index=i,
                    source_text=text,
                    target_locale=TGT,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        doc_id = doc.id
    with Session(engine) as s:
        w = Workflow(name="WF-chunked", scope="global", source_locale=SRC, target_locale=TGT)
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        # chunk_policy "auto" + a tiny token ceiling -> the two-paragraph segment really
        # is split into TWO chunk calls by the production seam (_seg_chunks).
        wf.add_step(
            s,
            w.id,
            name="Fix",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Fix into {target_locale}: {text}",
            chunk_policy="auto",
            tm_examples=False,
        )
        wf_id = w.id

    real, real_cap = orchestrator.run_completion, config.MAX_CHUNK_TOKENS
    seen = {"fix_calls": 0}

    def _stub(spec, prompt, **kw):
        # Branch on the PROMPT, not the payload text: the second chunk of a chunked
        # segment carries only its own paragraph, with no marker from the prior step.
        text = (kw.get("payload") or {}).get("text") or ""
        if "Fix into" in prompt:
            seen["fix_calls"] += 1
            if "Second paragraph" in text:  # the SECOND chunk fails
                return {
                    "text": "",
                    "provider_used": None,
                    "model_used": None,
                    "attempts": 1,
                    "cost_usd": 0.0,
                    "usage": None,
                    "error": "APIError: Context size has been exceeded.",
                }
            return {
                "text": "FIXED " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        return {
            "text": "D:" + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    config.MAX_CHUNK_TOKENS = 3
    try:
        status = _run(doc_id, wf_id)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
        config.MAX_CHUNK_TOKENS = real_cap
    assert status == RunStatus.COMPLETED, status
    assert seen["fix_calls"] >= 3, seen  # 2 chunks + the sibling's single chunk
    rows = _rows(doc_id)
    hurt = rows[0]
    # the PARTIAL chunked output is DISCARDED - the pre-step draft stands.
    assert hurt.target_text == "D:" + PARA, hurt.target_text
    assert "FIXED" not in (hurt.target_text or ""), hurt.target_text
    assert hurt.status == SegStatus.NEEDS_REVIEW, hurt.status
    f = [x.get("type") for x in (hurt.qa_flags or []) if isinstance(x, dict)]
    assert quality.STEP_ERROR_FLAG_TYPE in f, f
    # ...and the single-chunk sibling committed its fix normally.
    assert rows[1].target_text.startswith("FIXED "), rows[1].target_text
    assert rows[1].status == SegStatus.MT, rows[1].status
    print(
        "  [6] a two-chunk segment whose 2nd chunk errors degrades (partial output "
        "discarded, pre-step text kept); its sibling commits clean"
    )


# --- [7] step 0 has nothing to keep (P7, mutation M2) -------------------------
def test_step_zero_failure_still_fails_the_run():
    """The guard the degradation seam is built ON: a segment that fails BEFORE anything
    ever translated it holds only its SOURCE, and committing an untranslated source as a
    delivered target is exactly what the all-or-nothing write-back exists to prevent. A
    ONE-STEP workflow makes this unambiguous - there is no later step to rescue it."""
    doc_id = _doc("step0-fail")
    with Session(engine) as s:
        w = Workflow(name="WF-step0-fail", scope="global", source_locale=SRC, target_locale=TGT)
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf_id = w.id
    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if "export finished" in text:  # ONE segment fails, at step 0
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: Context size has been exceeded.",
            }
        return {
            "text": "TRADUIT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    try:
        status = _run(doc_id, wf_id)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    assert status == RunStatus.FAILED, status
    for r in _rows(doc_id):
        assert not (r.target_text or "").strip(), (r.source_text, r.target_text)
        # ...and above all NEVER the source echoed into the target slot.
        assert (r.target_text or "") != r.source_text, r.source_text
    print(
        "  [7] a step-0 provider error on ONE segment -> run FAILED, NO targets "
        "written, never source-as-target"
    )


# --- [8] the record survives a human_review pause/resume (P1) -----------------
def _wf_with_pause(name):
    with Session(engine) as s:
        w = Workflow(name=name, scope="global", source_locale=SRC, target_locale=TGT)
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(
            s,
            w.id,
            name="Fix",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Fix into {target_locale}: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(s, w.id, name="Review", step_type=StepType.HUMAN_REVIEW)
        return w.id


def test_degradation_survives_pause_resume():
    doc_id, wf_id = _doc("pause-resume"), _wf_with_pause("WF-pause-resume")
    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if text.startswith("DRAFT ") and "export finished" in text:
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: Context size has been exceeded.",
            }
        if text.startswith("DRAFT "):
            return {
                "text": "FIXED " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    try:
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, TGT, created_by="t")
        assert summary["status"] == "paused", summary
        run_id = summary["run_id"]
        # resume with NO edits (accept as-is) - the ordinary reviewer action.
        with Session(engine) as s:
            done = orchestrator.resume_workflow(s, run_id, TGT, "")
        assert done["status"] == "completed", done
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)

    rows = _rows(doc_id)
    hurt = [r for r in rows if "export finished" in (r.source_text or "")][0]  # noqa: RUF015
    flags = [f.get("type") for f in (hurt.qa_flags or []) if isinstance(f, dict)]
    assert quality.STEP_ERROR_FLAG_TYPE in flags, (hurt.status, flags)
    assert hurt.status == SegStatus.NEEDS_REVIEW, (hurt.status, flags)
    assert hurt.target_text == "DRAFT " + hurt.source_text, hurt.target_text
    for r in rows:
        if r is hurt:
            continue
        assert r.target_text == "FIXED DRAFT " + r.source_text, r.target_text
        assert r.status == SegStatus.MT, r.status
    print(
        "  [8] the degradation record SURVIVES a human_review pause/resume "
        "(needs_review + step_error still there after the resume)"
    )


# --- [9] a clean re-run clears the stale badge (P4) ---------------------------
def test_clean_rerun_clears_the_stale_flag():
    doc_id, wf_id = _doc("stale-badge"), _two_step_wf("WF-stale-badge")
    real = orchestrator.run_completion

    def _degrading(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if text.startswith("DRAFT ") and "export finished" in text:
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: Context size has been exceeded.",
            }
        if text.startswith("DRAFT "):
            return {
                "text": "FIXED " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    def _clean(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if text.startswith("DRAFT "):
            return {
                "text": "FIXED " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _degrading)
    try:
        assert _run(doc_id, wf_id) == RunStatus.COMPLETED
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    hurt = [r for r in _rows(doc_id) if "export finished" in (r.source_text or "")][0]  # noqa: RUF015
    before = [f.get("type") for f in (hurt.qa_flags or []) if isinstance(f, dict)]
    assert quality.STEP_ERROR_FLAG_TYPE in before, before

    set_pieces("app.services.orchestrator", "run_completion", _clean)
    try:
        assert _run(doc_id, wf_id) == RunStatus.COMPLETED
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    hurt = [r for r in _rows(doc_id) if "export finished" in (r.source_text or "")][0]  # noqa: RUF015
    after = [f.get("type") for f in (hurt.qa_flags or []) if isinstance(f, dict)]
    assert quality.STEP_ERROR_FLAG_TYPE not in after, after
    assert hurt.status == SegStatus.MT, (hurt.status, after)
    assert hurt.target_text == "FIXED DRAFT " + hurt.source_text, hurt.target_text
    # ...and the helper itself: every OTHER family survives the clear.
    keep = {"type": "dnt_violation", "severity": "major", "message": "m"}
    mixed = [keep, quality.step_error_flag("Fix", "boom")]
    assert quality.clear_step_error_flags(mixed) == [keep]
    assert quality.clear_step_error_flags([quality.step_error_flag("Fix", "")]) is None
    assert quality.clear_step_error_flags(None) is None
    print(
        "  [9] a CLEAN re-run clears the stale step_error badge and un-routes the "
        "segment; other flag families survive the clear"
    )


# --- [10] structured degradation telemetry (P8) -------------------------------
def test_degradation_telemetry():
    doc_id, wf_id = _doc("telemetry"), _two_step_wf("WF-telemetry")
    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if text.startswith("DRAFT ") and "export finished" in text:
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: Context size has been exceeded.",
            }
        if text.startswith("DRAFT "):
            return {
                "text": "FIXED " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    events = []
    set_pieces("app.services.orchestrator", "run_completion", _stub)
    try:
        with Session(engine) as s:
            for ev in orchestrator.run_workflow_stream(s, wf_id, doc_id, TGT):
                events.append(ev)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    steps = [e for e in events if e["type"] == "step"]
    done = [e for e in events if e["type"] == "done"]
    assert done, [e["type"] for e in events]
    fix_ev = [e for e in steps if e.get("step_name") == "Fix"][0]  # noqa: RUF015
    assert fix_ev.get("lqa_safety", {}).get("degraded") == 1, fix_ev.get("lqa_safety")
    assert done[0].get("lqa_safety", {}).get("degraded") == 1, done[0].get("lqa_safety")
    # ...and a step that degraded NOTHING still carries no lqa_safety slot at all, so an
    # ordinary run's event shape is unchanged.
    t_ev = [e for e in steps if e.get("step_name") == "Translate"][0]  # noqa: RUF015
    assert "lqa_safety" not in t_ev, t_ev.get("lqa_safety")
    # The prose note stays too - the trace column a human reads.
    assert "kept pre-step text" in (fix_ev.get("error") or ""), fix_ev.get("error")
    print(
        "  [10] degradation is structured telemetry: lqa_safety.degraded on the step "
        "event AND the run-level done rollup (clean steps unchanged)"
    )


# --- [11] a degraded segment is never QE-judged (P9) --------------------------
def test_degraded_segment_is_not_qe_judged():
    from app.services import qe

    doc_id = _doc("qe-skip")
    with Session(engine) as s:
        w = Workflow(name="WF-qe-skip", scope="global", source_locale=SRC, target_locale=TGT)
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(
            s,
            w.id,
            name="Fix",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Fix into {target_locale}: {text}",
            chunk_policy="none",
            tm_examples=False,
            qe_gate=True,
            qe_threshold=70,
        )
        wf_id = w.id

    real, real_score = orchestrator.run_completion, qe.score_segment
    judged = []

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if text.startswith("DRAFT ") and "export finished" in text:
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: Context size has been exceeded.",
            }
        if text.startswith("DRAFT "):
            return {
                "text": "FIXED " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    def _fake_score(spec, source, target, src_loc, tgt_loc, **kw):
        judged.append(target)
        return {
            "score": 20.0,
            "rationale": "bad",
            "skipped": False,
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    qe.score_segment = _fake_score
    try:
        status = _run(doc_id, wf_id)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
        qe.score_segment = real_score
    assert status == RunStatus.COMPLETED, status
    # The judge saw the two SIBLINGS' fixed output and nothing else. Above all it never
    # saw the degraded segment's pre-step draft (which the fix step never produced).
    assert len(judged) == 2, judged
    assert not any("export finished" in t for t in judged), judged
    hurt = [r for r in _rows(doc_id) if "export finished" in (r.source_text or "")][0]  # noqa: RUF015
    flags = [f.get("type") for f in (hurt.qa_flags or []) if isinstance(f, dict)]
    assert "qe" not in flags, flags  # no bogus gate verdict on a degraded segment
    assert quality.STEP_ERROR_FLAG_TYPE in flags, flags
    print(
        "  [11] a degraded segment is never sent to the QE judge and carries no gate "
        "verdict (2 siblings scored, it was not)"
    )


# --- [12] the flag is visible on every review surface (P2) --------------------
def test_step_error_is_surfaced():
    from app.web import api_cli, documents

    class _Seg:
        id, order_index, status = 7, 0, SegStatus.NEEDS_REVIEW
        target_text = "Le brouillon"
        qa_flags = [quality.step_error_flag("Fix", "APIError: context exceeded")]  # noqa: RUF012

    row = api_cli._segment_detail_row(_Seg())
    types = [f["type"] for f in row["qa_flags"]]
    assert quality.STEP_ERROR_FLAG_TYPE in types, row
    assert "Fix" in row["qa_flags"][0]["message"], row

    line = api_cli.review_summary_line([row])
    assert line and "step-error 1" in line, line
    assert line.startswith("1 flagged for review"), line

    # the assert inside api_cli keeps the summary breakdown and the surfaced set in
    # lockstep; re-state it here so a future family cannot be added to one only.
    assert {ft for ft, _ in api_cli._SUMMARY_FAMILIES} == set(api_cli._SEGMENT_SURFACED)
    assert quality.STEP_ERROR_FLAG_TYPE in api_cli._SEGMENT_SURFACED

    # ...and the editor grid: _SURFACED is a local of the route, so read it off the
    # source rather than duplicating the list (the chip renders from a generic loop).
    import inspect

    src = inspect.getsource(documents)
    assert "quality.STEP_ERROR_FLAG_TYPE" in src, "editor grid _SURFACED omits step_error"
    print(
        "  [12] step_error renders as an editor chip, in the headless detail row, and "
        "in the review summary line"
    )


# --- [13] TWO pauses: a repaired segment must not stay quarantined (N1) -------
def test_double_pause_clears_a_repaired_record():
    """re-review N1. Persisting the degradation record at a pause was only half of it:
    the write also has to be able to say NOTHING IS WRONG ANY MORE.

    Translate -> Fix1 -> Review -> Fix2 -> Review2. Fix1 degrades seg 1; Fix2 REPAIRS it,
    which pops its live entry - but the record written at pause 1 is still sitting in
    run.context. A write-only-when-non-empty condition leaves it there, pause 2
    republishes nothing, and the final resume restores a verdict about text that two
    steps have since replaced: a permanently quarantined, perfectly good segment."""
    doc_id = _doc("double-pause")
    with Session(engine) as s:
        w = Workflow(name="WF-double-pause", scope="global", source_locale=SRC, target_locale=TGT)
        s.add(w)
        s.commit()
        s.refresh(w)
        wf.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(
            s,
            w.id,
            name="Fix1",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="FIX1 into {target_locale}: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(s, w.id, name="Review", step_type=StepType.HUMAN_REVIEW)
        wf.add_step(
            s,
            w.id,
            name="Fix2",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="FIX2 into {target_locale}: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        wf.add_step(s, w.id, name="Review2", step_type=StepType.HUMAN_REVIEW)
        wf_id = w.id
    real = orchestrator.run_completion

    def _stub(spec, prompt, **kw):
        text = (kw.get("payload") or {}).get("text") or ""
        if "FIX1 into" in prompt and "export finished" in text:
            return {
                "text": "",
                "provider_used": None,
                "model_used": None,
                "attempts": 1,
                "cost_usd": 0.0,
                "usage": None,
                "error": "APIError: Context size has been exceeded.",
            }
        if "FIX1 into" in prompt:
            return {
                "text": "FA " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        if "FIX2 into" in prompt:  # Fix2 succeeds on EVERYTHING
            return {
                "text": "FB " + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }
        return {
            "text": "DRAFT " + text,
            "provider_used": "mock",
            "model_used": "m",
            "attempts": 1,
            "error": None,
            "cost_usd": 0.0,
            "usage": {},
        }

    set_pieces("app.services.orchestrator", "run_completion", _stub)
    try:
        with Session(engine) as s:
            summary = orchestrator.run_workflow(s, wf_id, doc_id, TGT, created_by="t")
        assert summary["status"] == "paused", summary
        run_id = summary["run_id"]
        # PAUSE 1: the degradation really was recorded (this is the N1 precondition).
        with Session(engine) as s:
            ctx = s.get(WorkflowRun, run_id).context or {}
        assert ctx.get("seg_step_error"), ctx
        with Session(engine) as s:
            mid = orchestrator.resume_workflow(s, run_id, TGT, "")
        assert mid["status"] == "paused", mid
        # PAUSE 2: Fix2 repaired the segment, so the record must have been CLEARED.
        with Session(engine) as s:
            ctx2 = s.get(WorkflowRun, run_id).context or {}
        assert ctx2.get("seg_step_error") == {}, ctx2.get("seg_step_error")
        with Session(engine) as s:
            done = orchestrator.resume_workflow(s, run_id, TGT, "")
        assert done["status"] == "completed", done
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real)
    for r in _rows(doc_id):
        # every segment went through Fix2, so every one of them is clean.
        assert r.target_text.startswith("FB "), r.target_text
        flags = [f.get("type") for f in (r.qa_flags or []) if isinstance(f, dict)]
        assert quality.STEP_ERROR_FLAG_TYPE not in flags, (r.source_text, flags)
        assert r.status == SegStatus.MT, (r.source_text, r.status, flags)
    print(
        "  [13] TWO pauses: a segment degraded before pause 1 and REPAIRED before "
        "pause 2 lands clean - the persisted record is written through, not just "
        "written once"
    )


# --- [14] the IN-RUN clear on success (mutation M6) ---------------------------
def test_in_run_success_clears_the_record():
    """A later step that SUCCEEDS on a degraded segment holds text the failure verdict is
    no longer about, so its entry is popped THERE, inside the run - not left for the
    write-back to sort out. Deleting those pops leaves a segment that was fixed within a
    single run still wearing the badge (mutation M6 survived without this).

    Both dispatch branches are covered: the CONCURRENT fan-out (default) and the SERIAL
    adaptive-feedback pass, which own separate copies of the pop."""
    for label, adaptive in (("concurrent", False), ("serial/adaptive", True)):
        doc_id = _doc("inrun-clear-%s" % ("c" if not adaptive else "s"))
        with Session(engine) as s:
            w = Workflow(
                name="WF-inrun-%s" % ("c" if not adaptive else "s"),
                scope="global",
                source_locale=SRC,
                target_locale=TGT,
            )
            s.add(w)
            s.commit()
            s.refresh(w)
            wf.add_step(
                s,
                w.id,
                name="Translate",
                step_type=StepType.LLM_AGENT,
                provider="mock",
                prompt_text="Translate: {text}",
                chunk_policy="none",
                tm_examples=False,
            )
            wf.add_step(
                s,
                w.id,
                name="Fix1",
                step_type=StepType.LLM_AGENT,
                provider="mock",
                prompt_text="FIX1 into {target_locale}: {text}",
                chunk_policy="none",
                tm_examples=False,
                adaptive_feedback=adaptive,
            )
            wf.add_step(
                s,
                w.id,
                name="Fix2",
                step_type=StepType.LLM_AGENT,
                provider="mock",
                prompt_text="FIX2 into {target_locale}: {text}",
                chunk_policy="none",
                tm_examples=False,
                adaptive_feedback=adaptive,
            )
            wf_id = w.id
        real = orchestrator.run_completion

        def _stub(spec, prompt, **kw):
            text = (kw.get("payload") or {}).get("text") or ""
            if "FIX1 into" in prompt and "export finished" in text:
                return {
                    "text": "",
                    "provider_used": None,
                    "model_used": None,
                    "attempts": 1,
                    "cost_usd": 0.0,
                    "usage": None,
                    "error": "APIError: Context size has been exceeded.",
                }
            marker = (
                "FA " if "FIX1 into" in prompt else ("FB " if "FIX2 into" in prompt else "DRAFT ")
            )
            return {
                "text": marker + text,
                "provider_used": "mock",
                "model_used": "m",
                "attempts": 1,
                "error": None,
                "cost_usd": 0.0,
                "usage": {},
            }

        set_pieces("app.services.orchestrator", "run_completion", _stub)
        try:
            status = _run(doc_id, wf_id)
        finally:
            set_pieces("app.services.orchestrator", "run_completion", real)
        assert status == RunStatus.COMPLETED, (label, status)
        hurt = [r for r in _rows(doc_id) if "export finished" in (r.source_text or "")][0]  # noqa: RUF015
        # Fix2 produced this segment's committed text, so Fix1's verdict is stale AND
        # was cleared within the run - no flag, no review routing, no stale badge.
        assert hurt.target_text.startswith("FB "), (label, hurt.target_text)
        flags = [f.get("type") for f in (hurt.qa_flags or []) if isinstance(f, dict)]
        assert quality.STEP_ERROR_FLAG_TYPE not in flags, (label, flags)
        assert hurt.status == SegStatus.MT, (label, hurt.status, flags)
    print(
        "  [14] a later step SUCCEEDING on a degraded segment clears its record inside "
        "the run (both the concurrent fan-out and the serial adaptive pass)"
    )


def main():
    print("FIXSTEP DEGRADATION (GitHub #45)")
    test_bound_findings()
    test_cap_at_the_prompt_seam()
    test_one_segment_fails()
    test_every_segment_fails()
    test_every_segment_fails_on_a_request_error_still_completes()
    test_structural_error_escalates_on_one_segment()
    test_single_pending_segment_degrades()
    test_tm_protected_loop_keeps_the_tm_segment()
    test_chunked_partial_output_degrades()
    test_step_zero_failure_still_fails_the_run()
    test_degradation_survives_pause_resume()
    test_clean_rerun_clears_the_stale_flag()
    test_degradation_telemetry()
    test_degraded_segment_is_not_qe_judged()
    test_step_error_is_surfaced()
    test_double_pause_clears_a_repaired_record()
    test_in_run_success_clears_the_record()
    print("FIXSTEP DEGRADATION OK")


if __name__ == "__main__":
    main()
