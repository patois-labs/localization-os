"""MQM-aligned QE score + routing gate (roadmap gap #1 + #6).

Verifies the consensus HITL pattern (Phrase QPS / XTM Intelligent Score / Smartling
LQE): score every produced segment, route only low-confidence ones to a human.

  1. Gate OFF  -> byte-identical prompts (no 'qe' task call) + unchanged event shapes.
  2. Gate ON, high score  -> segment PROCEEDS (SegStatus.MT), qe flag stored, passed.
  3. Gate ON, low score    -> SegStatus.NEEDS_REVIEW + rationale in qa_flags + visible
                              in the CAT editor grid; run summary tally correct.
  4. Threshold boundary    -> score == threshold PROCEEDS (>=, not >).
  5. Judge junk JSON / provider error -> qe_skipped, segment still proceeds.
  6. PII redaction ON      -> the judge prompt receives REDACTED text (no raw PII).
  7. Dead qe_provider ref  -> follows lqa's convention: soft-skip (qe_skipped), the
                              run still completes (score degraded, never the run).
  8. Spend accounting      -> the judge's calls move the spend ledger (counted).
  9. Composition w/ adaptive_feedback -> a QE-FAILED segment does NOT seed the buffer.

Hermetic + FK enforcement ON (the app engine wires PRAGMA foreign_keys=ON). A
custom NON-mock capturing provider records every (system, prompt, task, payload) so
we can assert on the exact judge prompt, and — being non-mock — its calls are NOT
spend-exempt (real ledger movement). ASCII-only prints.
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Segment, SegStatus, WorkflowStep
from app.services import orchestrator, spend, workflows
from app.services.providers import (
    Provider,
    register_provider,
)


class QEProvider(Provider):
    """Records every call. For task='translate' returns a deterministic target; for
    task='qe' returns a canned MQM judge JSON whose category counts are DERIVED from
    the target text so a test can steer the score:
      * a target containing 'BAD' -> many accuracy errors -> low score;
      * otherwise -> zero errors -> score 100.
    NON-mock so is_exempt=False (spend accrues, ledger assertion is real)."""

    name = "qeprov"
    calls = []  # noqa: RUF012
    junk_qe = False  # flip the judge into returning non-JSON (junk) output
    error_qe = False  # flip the judge into raising (provider error path)

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        QEProvider.calls.append(
            {"system": system, "prompt": prompt, "task": task, "payload": payload}
        )
        self.last_usage = {
            "input_tokens": max(1, len(prompt or "") // 4),
            "output_tokens": 4,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        if task == "qe":
            if QEProvider.error_qe:
                raise RuntimeError("simulated QE judge outage")
            if QEProvider.junk_qe:
                return "not json at all {{{"
            tgt = (payload or {}).get("target", "")
            if "BAD" in tgt:
                return (
                    '{"accuracy":3,"fluency":1,"terminology":0,"style":0,'
                    '"rationale":"multiple mistranslations"}'
                )
            return (
                '{"accuracy":0,"fluency":0,"terminology":0,"style":0,'
                '"rationale":"clean translation"}'
            )
        if task == "translate":
            # echo the source verbatim so a source containing 'BAD' yields a 'BAD'
            # target the judge scores low (deterministic steering).
            return (payload or {}).get("text", prompt or "")
        return (prompt or "").strip()

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.junk_qe = False
        cls.error_qe = False


def docx_from(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def new_segmented_doc(client, pid, paragraphs, name="d.docx"):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                name,
                docx_from(paragraphs),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def qe_calls():
    return [c for c in QEProvider.calls if c["task"] == "qe"]


def segs_for(session, doc_id):
    return sorted(
        session.exec(select(Segment).where(Segment.document_id == doc_id)).all(),
        key=lambda x: x.order_index,
    )


def make_wf(session, name, **step_kw):
    wf = workflows.create_workflow(session, name)
    workflows.add_step(
        session,
        wf.id,
        name="T",
        step_type="llm_agent",
        provider="qeprov",
        prompt_text="Translate: {text}",
        **step_kw,
    )
    return wf.id


def main() -> None:
    init_db()
    register_provider("qeprov", QEProvider())
    client = TestClient(app)

    r = client.post(
        "/projects",
        data={
            "name": "QE",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "qeprov",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    GOOD = ["Alpha one.", "Beta two.", "Gamma three."]

    # ========================================================================
    # 1. Gate OFF -> no 'qe' task call; segment path byte-identical.
    # ========================================================================
    with Session(engine) as s:
        wf_off = make_wf(s, "OFF")  # qe_gate defaults False
    doc_id = new_segmented_doc(client, pid, GOOD, "off.docx")
    QEProvider.reset()
    with Session(engine) as s:
        events_off = list(orchestrator.run_workflow_stream_segmented(s, wf_off, doc_id, "fr-FR"))
    assert not qe_calls(), "gate OFF must make NO judge call"
    step_ev = [e for e in events_off if e.get("type") == "step"][0]  # noqa: RUF015
    assert "qe" not in step_ev, "gate OFF step event must not carry a qe key"
    done_ev = [e for e in events_off if e.get("type") == "done"][0]  # noqa: RUF015
    assert "qe" not in done_ev, "gate OFF done event must not carry a qe key"
    with Session(engine) as s:
        for seg in segs_for(s, doc_id):
            assert seg.status == SegStatus.MT, seg.status
            assert not (seg.qa_flags or []), "gate OFF must store no qe flag"
    print("  1. gate OFF -> no judge call, event shapes + segment state unchanged OK")

    # ========================================================================
    # 2. Gate ON, high score -> segment PROCEEDS, qe flag stored (passed).
    # ========================================================================
    with Session(engine) as s:
        wf_on = make_wf(s, "ON-good", qe_gate=True, qe_threshold=70)
    doc_id = new_segmented_doc(client, pid, GOOD, "good.docx")
    QEProvider.reset()
    with Session(engine) as s:
        ev = list(orchestrator.run_workflow_stream_segmented(s, wf_on, doc_id, "fr-FR"))
    assert len(qe_calls()) == len(GOOD), "one judge call per produced segment"
    with Session(engine) as s:
        for seg in segs_for(s, doc_id):
            assert seg.status == SegStatus.MT, "high-score segment must proceed as MT"
            qf = [f for f in (seg.qa_flags or []) if f.get("type") == "qe"][0]  # noqa: RUF015
            assert qf["score"] == 100 and qf["passed"] is True and not qf["skipped"], qf
            assert qf["rationale"] == "clean translation", qf
    done_ev = [e for e in ev if e.get("type") == "done"][0]  # noqa: RUF015
    assert done_ev["qe"] == {"scored": 3, "passed": 3, "failed": 0, "skipped": 0}, done_ev["qe"]
    print("  2. gate ON high score -> segment proceeds, qe flag stored (passed) OK")

    # ========================================================================
    # 3. Gate ON, low score -> NEEDS_REVIEW + rationale + editor visibility.
    # ========================================================================
    MIXED = ["Alpha one.", "BAD gamma.", "Delta four."]  # seg 1 scores low
    with Session(engine) as s:
        wf_low = make_wf(s, "ON-mixed", qe_gate=True, qe_threshold=70)
    doc_id = new_segmented_doc(client, pid, MIXED, "mixed.docx")
    QEProvider.reset()
    with Session(engine) as s:
        ev = list(orchestrator.run_workflow_stream_segmented(s, wf_low, doc_id, "fr-FR"))
    with Session(engine) as s:
        rows = segs_for(s, doc_id)
        assert rows[0].status == SegStatus.MT, "good segment proceeds"
        assert rows[1].status == SegStatus.NEEDS_REVIEW, "low-score segment routes to review"
        assert rows[2].status == SegStatus.MT, "good segment proceeds"
        qf = [f for f in (rows[1].qa_flags or []) if f.get("type") == "qe"][0]  # noqa: RUF015
        assert qf["passed"] is False and qf["score"] < 70, qf
        assert qf["rationale"] == "multiple mistranslations", qf
        assert qf["severity"] == "major", "routed segment badge must be 'major' (warn)"
        low_seg_id = rows[1].id
    done_ev = [e for e in ev if e.get("type") == "done"][0]  # noqa: RUF015
    assert done_ev["qe"] == {"scored": 3, "passed": 2, "failed": 1, "skipped": 0}, done_ev["qe"]
    # editor visibility: the QE badge shows in the CAT grid's qa cell for the doc
    r = client.get(f"/documents/{doc_id}/editor?locale=fr-FR")
    assert r.status_code == 200, r.status_code
    assert "routed to review" in r.text and "multiple mistranslations" in r.text, (
        "the QE rationale must surface in the editor grid"
    )
    print("  3. gate ON low score -> NEEDS_REVIEW + rationale + editor visibility OK")

    # ========================================================================
    # 4. Threshold boundary: score == threshold PROCEEDS (>=, not >).
    #    A clean target scores 100; set threshold 100 -> boundary passes.
    # ========================================================================
    with Session(engine) as s:
        wf_bound = make_wf(s, "boundary", qe_gate=True, qe_threshold=100)
    doc_id = new_segmented_doc(client, pid, ["Alpha one."], "bound.docx")
    QEProvider.reset()
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_bound, doc_id, "fr-FR"))
    with Session(engine) as s:
        seg = segs_for(s, doc_id)[0]
        assert seg.status == SegStatus.MT, "score == threshold must PROCEED (>=)"
        qf = [f for f in (seg.qa_flags or []) if f.get("type") == "qe"][0]  # noqa: RUF015
        assert qf["passed"] is True and qf["score"] == 100, qf
    print("  4. threshold boundary (score == threshold) proceeds OK")

    # ========================================================================
    # 5. Judge junk JSON / provider error -> qe_skipped, segment proceeds.
    # ========================================================================
    for label, flip in (("junk", "junk_qe"), ("error", "error_qe")):
        with Session(engine) as s:
            wf_skip = make_wf(s, f"skip-{label}", qe_gate=True, qe_threshold=70)
        doc_id = new_segmented_doc(client, pid, ["Alpha one."], f"skip_{label}.docx")
        QEProvider.reset()
        setattr(QEProvider, flip, True)
        with Session(engine) as s:
            ev = list(orchestrator.run_workflow_stream_segmented(s, wf_skip, doc_id, "fr-FR"))
        setattr(QEProvider, flip, False)
        with Session(engine) as s:
            seg = segs_for(s, doc_id)[0]
            assert seg.status == SegStatus.MT, f"{label}: skipped judge must let segment proceed"
            qf = [f for f in (seg.qa_flags or []) if f.get("type") == "qe"][0]  # noqa: RUF015
            assert qf["skipped"] is True and qf["score"] is None, qf
        done_ev = [e for e in ev if e.get("type") == "done"][0]  # noqa: RUF015
        assert done_ev["qe"] == {"scored": 0, "passed": 0, "failed": 0, "skipped": 1}, (
            label,
            done_ev["qe"],
        )
    print("  5. judge junk/error -> qe_skipped, segment proceeds OK")

    # ========================================================================
    # 6. PII redaction ON -> the judge prompt receives REDACTED text.
    # ========================================================================

    SSN = "123-45-6789"
    with Session(engine) as s:
        wf_pii = make_wf(s, "pii", qe_gate=True, qe_threshold=70, redact_pii=True)
    doc_id = new_segmented_doc(client, pid, [f"Contact SSN {SSN} now."], "pii.docx")
    QEProvider.reset()
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_pii, doc_id, "fr-FR"))
    jcall = qe_calls()[0]
    assert SSN not in jcall["prompt"], "raw SSN reached the QE judge prompt"
    assert "PII_" in jcall["prompt"] or "«PII_" in jcall["prompt"], (
        "the judge should see the redaction placeholder, not raw PII"
    )
    print("  6. PII redaction ON -> judge receives redacted text OK")

    # ========================================================================
    # 7. Dead qe_provider ref -> lqa convention: soft-skip; the run still completes.
    #    An unknown 'engine:99999' ref is not live -> run_completion resolves it to
    #    the mock for the JUDGE only... so use a genuinely dead engine ref shape.
    # ========================================================================
    with Session(engine) as s:
        wf_dead = make_wf(s, "dead-qe", qe_gate=True, qe_threshold=70)
        # Legacy stored reference, or an engine deleted after save: runtime still soft-skips.
        step = orchestrator.get_steps(s, wf_dead)[0]
        step.qe_provider = "engine:999999"
        s.add(step)
        s.commit()
    doc_id = new_segmented_doc(client, pid, ["Alpha one."], "deadqe.docx")
    QEProvider.reset()
    with Session(engine) as s:
        ev = list(orchestrator.run_workflow_stream_segmented(s, wf_dead, doc_id, "fr-FR"))
    # the RUN must still complete (the judge degraded, not the run)
    assert [e for e in ev if e.get("type") == "done"], "a dead qe judge must not fail the run"
    assert not [e for e in ev if e.get("type") == "error"], "no error event on a dead judge"
    with Session(engine) as s:
        seg = segs_for(s, doc_id)[0]
        assert seg.status == SegStatus.MT, "dead judge -> segment proceeds (soft-skip)"
        qf = [f for f in (seg.qa_flags or []) if f.get("type") == "qe"][0]  # noqa: RUF015
        assert qf["skipped"] is True, qf
    done_ev = [e for e in ev if e.get("type") == "done"][0]  # noqa: RUF015
    assert done_ev["qe"]["skipped"] == 1 and done_ev["qe"]["scored"] == 0, done_ev["qe"]
    with Session(engine) as s:
        from app.models import WorkflowRun

        warnings = s.get(WorkflowRun, done_ev["run_id"]).context["warnings"]
        assert any(w.get("code") == "qe_never_scored" for w in warnings), warnings
    assert "judge not configured" in qf["message"], qf
    print("  7. dead qe_provider ref -> soft-skip (lqa convention), run completes OK")

    # ========================================================================
    # 8. Spend accounting: the judge's calls move the spend ledger.
    # ========================================================================
    with Session(engine) as s:
        wf_spend = make_wf(s, "spend", qe_gate=True, qe_threshold=70)
    doc_id = new_segmented_doc(client, pid, GOOD, "spend.docx")
    QEProvider.reset()
    spent_before = spend.spent_this_period()
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_spend, doc_id, "fr-FR"))
    assert len(qe_calls()) == len(GOOD), "judge called per segment"
    assert spend.spent_this_period() > spent_before, (
        "the QE judge calls must move the spend ledger (counted)"
    )
    print("  8. spend accounting includes the judge calls OK")

    # ========================================================================
    # 9. Composition with adaptive_feedback: a QE-FAILED segment does NOT seed the
    #    buffer. Seg 0 is BAD (fails); seg 1+ must NOT carry seg 0's pair.
    # ========================================================================
    from app.services import adaptive

    COMP = ["BAD alpha.", "Beta two.", "Gamma three."]
    with Session(engine) as s:
        wf_comp = make_wf(
            s,
            "comp",
            qe_gate=True,
            qe_threshold=70,
            adaptive_feedback=True,
            adaptive_feedback_window=5,
        )
    doc_id = new_segmented_doc(client, pid, COMP, "comp.docx")
    QEProvider.reset()
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_comp, doc_id, "fr-FR"))
    tcalls = [c for c in QEProvider.calls if c["task"] == "translate"]
    assert len(tcalls) == len(COMP), tcalls
    # seg 1 & 2 prompts must NOT contain the failed seg-0 pair in a feedback block
    for p in (tcalls[1]["prompt"], tcalls[2]["prompt"]):
        if adaptive._FEEDBACK_HEADER in p:
            fb = p.split(adaptive._FEEDBACK_HEADER)[1]
            assert "BAD alpha." not in fb, (
                "a QE-FAILED segment must NOT seed the adaptive feedback buffer"
            )
    # sanity: seg 1 (which PASSES) SHOULD seed seg 2's buffer
    p2 = tcalls[2]["prompt"]
    assert adaptive._FEEDBACK_HEADER in p2 and "Beta two." in p2, (
        "a QE-PASSING segment must still seed the buffer for later segments"
    )
    print("  9. composition: QE-failed segment does not seed the feedback buffer OK")

    # ========================================================================
    # 9b. REGRESSION: the REAL (offline) MockProvider, no custom judge registered
    #    — this is the zero-key install the product promises works fully offline.
    #    Before the fix, MockProvider.complete had no task=='qe' branch, so every
    #    QE call fell through to the generic prompt-echo, json.loads failed, and
    #    EVERY segment soft-skipped ("junk_json") -> qe_skipped == len(segments),
    #    no scoring, no routing, ever. Assert real scores are produced (qe_skipped
    #    == 0) and the DETERMINISTIC-FAIL rule (app.services.providers._mock_qe:
    #    the whole-word, case-insensitive token "BAD" in the target -> 3 accuracy
    #    errors -> score 40) routes below threshold to NEEDS_REVIEW — so the demo
    #    shows BOTH a pass and a routed-to-review outcome with no API key at all.
    # ========================================================================
    with Session(engine) as s:
        wf_mock = workflows.create_workflow(s, "mock-offline")
        workflows.add_step(
            s,
            wf_mock.id,
            name="T",
            step_type="llm_agent",
            provider="mock",
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_threshold=70,
        )
        wf_mock_id = wf_mock.id
    MOCK_MIXED = ["Alpha one.", "This is a BAD translation.", "Gamma three."]
    doc_id = new_segmented_doc(client, pid, MOCK_MIXED, "mock_offline.docx")
    with Session(engine) as s:
        ev = list(orchestrator.run_workflow_stream_segmented(s, wf_mock_id, doc_id, "fr-FR"))
    with Session(engine) as s:
        rows = segs_for(s, doc_id)
        for r in rows:
            qf = [f for f in (r.qa_flags or []) if f.get("type") == "qe"]
            assert qf, "the real offline MockProvider must produce a real qe flag (no skip)"
            assert qf[0]["skipped"] is False, ("must not silently skip", qf[0])
        assert rows[1].status == SegStatus.NEEDS_REVIEW, (
            "a target containing the whole word BAD must route to review",
            rows[1].status,
        )
        assert rows[0].status == SegStatus.MT and rows[2].status == SegStatus.MT, (
            "clean targets must proceed"
        )
    done_ev = [e for e in ev if e.get("type") == "done"][0]  # noqa: RUF015
    assert done_ev["qe"]["skipped"] == 0, (
        "the offline MockProvider QE judge must never silently no-op",
        done_ev["qe"],
    )
    assert done_ev["qe"] == {"scored": 3, "passed": 2, "failed": 1, "skipped": 0}, done_ev["qe"]
    print(
        "  9b. offline MockProvider QE judge: real scores (qe_skipped=0), BAD target -> NEEDS_REVIEW OK"  # noqa: E501
    )

    # ========================================================================
    # 9c. Stale QE badge cleared on save/approve (review finding): the editor
    #    must stop showing "routed to review" once a human has fixed/approved the
    #    segment — segment_save/segment_approve now clear the qe entry via
    #    qe.clear_qe_flag (sibling of qe.merge_qe_flag).
    # ========================================================================
    with Session(engine) as s:
        low_qf = [f for f in (s.get(Segment, low_seg_id).qa_flags or []) if f.get("type") == "qe"]
        assert low_qf, "precondition: the routed segment carries a qe flag"
    r = client.post(
        f"/segments/{low_seg_id}/save", data={"target_text": "Delta four, fixed.", "tte_ms": "0"}
    )
    assert r.status_code == 200, r.status_code
    with Session(engine) as s:
        seg = s.get(Segment, low_seg_id)
        assert seg.status == SegStatus.EDITED, seg.status
        assert not [f for f in (seg.qa_flags or []) if f.get("type") == "qe"], (
            "stale QE badge must be cleared after save",
            seg.qa_flags,
        )
    print("  9c. save clears the stale QE badge (status EDITED, qe flag gone) OK")

    # approve path: seed a fresh routed segment, then approve it and assert the
    # flag clears there too (mirrors save).
    with Session(engine) as s:
        wf_appr = make_wf(s, "ON-approve", qe_gate=True, qe_threshold=70)
    doc_id = new_segmented_doc(client, pid, ["BAD approve me."], "approve.docx")
    QEProvider.reset()
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_appr, doc_id, "fr-FR"))
    with Session(engine) as s:
        seg2 = segs_for(s, doc_id)[0]
        assert seg2.status == SegStatus.NEEDS_REVIEW, seg2.status
        assert [f for f in (seg2.qa_flags or []) if f.get("type") == "qe"], (
            "precondition: routed segment carries a qe flag"
        )
        seg2_id = seg2.id
    r = client.post(f"/segments/{seg2_id}/approve")
    assert r.status_code == 200, r.status_code
    with Session(engine) as s:
        seg2 = s.get(Segment, seg2_id)
        assert seg2.status == SegStatus.APPROVED, seg2.status
        assert not [f for f in (seg2.qa_flags or []) if f.get("type") == "qe"], (
            "stale QE badge must be cleared after approve",
            seg2.qa_flags,
        )
    print("  9d. approve clears the stale QE badge (status APPROVED, qe flag gone) OK")

    # ========================================================================
    # 9e. REGRESSION: no judge spend after the step has already errored, on the
    #    SERIAL/adaptive path (orchestrator._score_seg call site). Before the fix,
    #    _score_seg(j, r) ran unconditionally even after `err` was set by an
    #    earlier segment in the same serial loop, burning judge calls on a run
    #    that gets discarded anyway (the concurrent path already guarded this with
    #    `if qe_on and not err:`). Segment 0 triggers a terminal provider error;
    #    segments 1+ must NOT get a QE judge call.
    # ========================================================================
    class ErrOnTrigger(Exception):
        """Name matches _is_terminal_error's 'badrequest' substring check, so
        run_completion fails fast (no retry/backoff sleep) straight to error."""

    class ErrProvider(Provider):
        name = "errprov"
        calls = []  # noqa: RUF012

        def complete(self, prompt, system=None, task=None, payload=None, **kw):
            ErrProvider.calls.append(task)
            if task == "translate" and "ERRTRIGGER" in (payload or {}).get("text", ""):
                raise ErrOnTrigger("simulated terminal provider error")
            if task == "qe":
                return '{"accuracy":0,"fluency":0,"terminology":0,"style":0,"rationale":"ok"}'
            return (payload or {}).get("text", prompt or "")

        @classmethod
        def reset(cls):
            cls.calls = []

    register_provider("errprov", ErrProvider())
    with Session(engine) as s:
        wf_err = workflows.create_workflow(s, "err-serial")
        workflows.add_step(
            s,
            wf_err.id,
            name="T",
            step_type="llm_agent",
            provider="errprov",
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_threshold=70,
            adaptive_feedback=True,
            adaptive_feedback_window=5,
        )
        wf_err_id = wf_err.id
    ERR_SEGS = ["ERRTRIGGER one.", "Beta two.", "Gamma three."]
    doc_id = new_segmented_doc(client, pid, ERR_SEGS, "err_serial.docx")
    ErrProvider.reset()
    with Session(engine) as s:
        ev = list(orchestrator.run_workflow_stream_segmented(s, wf_err_id, doc_id, "fr-FR"))
    qe_call_count = sum(1 for t in ErrProvider.calls if t == "qe")
    assert qe_call_count == 0, (
        "no QE judge call must fire once the serial step has errored",
        ErrProvider.calls,
    )
    assert [e for e in ev if e.get("type") == "error"], (
        "the induced provider error must surface as a failed run"
    )
    print("  9e. serial path: no judge spend after a mid-step error (0 qe calls) OK")

    # ========================================================================
    # web form + CLI parity: the step editor persists the three fields.
    # ========================================================================
    with Session(engine) as s:
        wf_form = workflows.create_workflow(s, "form-wf")
        wf_form_id = wf_form.id
    r = client.post(
        f"/workflows/{wf_form_id}/steps",
        data={
            "name": "Web QE",
            "step_type": "llm_agent",
            "provider": "qeprov",
            "prompt_text": "Do: {text}",
            "chunk_policy": "auto",
            "qe_gate": "1",
            "qe_threshold": "55",
            "qe_provider": "qeprov",
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.status_code
    with Session(engine) as s:
        st = sorted(
            s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf_form_id)).all(),
            key=lambda x: x.order_index,
        )[-1]
        assert st.qe_gate is True and st.qe_threshold == 55 and st.qe_provider == "qeprov", (
            st.qe_gate,
            st.qe_threshold,
            st.qe_provider,
        )
    # FX-1: invalid inbound thresholds are refused; stored runtime recovery stays unchanged.
    with Session(engine) as s:
        wf_c = workflows.create_workflow(s, "clamp-wf")
        for bad in ("nope", 9999):
            try:
                workflows.add_step(s, wf_c.id, provider="qeprov", qe_threshold=bad)
            except workflows.WorkflowError:
                pass
            else:
                raise AssertionError(f"invalid threshold accepted: {bad}")
        st_n = workflows.add_step(s, wf_c.id, provider="qeprov", qe_provider="  ")
        assert st_n.qe_provider is None, "blank qe_provider must normalise to None"
    print("  10. web form + service-layer validation persist the QE fields OK")

    print("verify_qe_gate OK")


if __name__ == "__main__":
    main()
