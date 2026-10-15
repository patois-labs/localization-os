"""KEY-BASED PROJECTS, Wave A -- THE ARCHITECTURE LAW (SPEC-key-based-projects 10.2).

    "Keys are an identity layer on top of the existing Segment spine -- never a
     parallel pipeline."

That sentence is either true or it is marketing, and the only way to tell is to run
the SAME content through BOTH shapes and compare. This suite builds a key project and
a document project holding IDENTICAL source strings, with identical TM, identical
glossary entries and the SAME workflow, runs a real MockProvider workflow over each
through the UNMODIFIED orchestrator entry point, and compares what came out.

GROUPS
  [A] PARITY. `orchestrator.run_workflow_stream` (imported, never patched) is driven
      over a key project's internal document. The prompts sent to the provider are
      captured BYTE-FOR-BYTE and compared with the document project's; so are the
      written targets, statuses and StepRun rows. A difference anywhere means key
      content is NOT on the shared pipeline.
  [B] TM LEVERAGE. A 100% segment-TM hit short-circuits step 0 for a KEY segment
      exactly as it does for a document segment (same provenance stamps, same skipped
      provider call) -- because TM is looked up by (project, locale pair, text) and a
      key value is a Segment.
  [C] GLOSSARY. A required term seeded per project reaches the prompt for the key
      project byte-identically to the document project.
  [D] THE SAFETY PERIMETER IS SHARED, NOT FORKED. `quality.commit_mt_output` -- the
      ONE sanctioned MT write path, carrying the refusal guard, NEEDS_REVIEW routing
      and TM-ineligibility -- produces identical outcomes on a key segment and a
      document segment given the same refusal text; an IMPORTED value routes through
      it (so a .po full of LLM hedges is quarantined on the way in) while a human edit
      is content and is not gated; and a flagged key value is refused by the same TM
      write-back guard. Nothing in the key layer re-implements or bypasses any of it.
  [E] THE EXPORT TAG GUARD fires on a damaged KEY value: the key exporter calls the
      SAME `export.segment_tag_damage` predicate, substitutes the source, and reports
      the substitution through the same {unit,index,kind} channel the document
      exporter fills.
  [F] THE WORKFLOW WRITE-BACK IS IN THE DELTA FEED. The run is launched THE WAY EVERY
      REAL SURFACE LAUNCHES IT -- `orchestrator.start_run`, which hands execution to a
      daemon thread -- and its translations appear as change entries attributed to the
      `actor_scope` the LAUNCHER entered (review A6). The thread crossing is asserted,
      not assumed, because a same-thread fixture would make the claim vacuous. Still
      NO orchestrator change.

WHAT IS DELIBERATELY NOT MOCKED: the orchestrator, translate.py, quality.py, tm.py,
glossary.py and export.py are all the real modules; `providers.run_completion` is
WRAPPED BY AN OBSERVER (it still runs, against the offline MockProvider) purely so
the prompts can be compared. A stub would have made the parity claim vacuous.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env; provider
keys blanked; provider "mock"). ASCII-only output. Final line: "KEYS PIPELINE OK".
"""

import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_keyspipe_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    GlossaryEntry,
    KeyChangeLog,
    KeyEntity,
    ProjectType,
    RunStatus,
    Segment,
    SegmentTM,
    SegStatus,
    StepRun,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
)
from app.services import (
    content,
    key_files,
    keys,
    orchestrator,
    quality,
    tm,
)
from app.services import export as export_svc
from app.services import workflows as wf

init_db()

SRC = "en-US"
TGT = "es-ES"
#: The same three strings, held as key values in one project and as document
#: paragraphs in the other. Deliberately ordinary sentences -- the point is parity,
#: not an edge case.
STRINGS = [
    ("app.title", "Welcome to the dashboard."),
    ("app.cta", "Save your changes."),
    ("app.hint", "Type a search term."),
]

_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- the provider observer (wraps, never replaces) ---------------------------
_PROMPTS: list = []
_REAL_COMPLETION = orchestrator.run_completion


#: Thread NAMES the provider was called on, per call. [F1] reads this to prove the
#: run really did cross a thread (a same-thread fixture would make the actor-scope
#: claim vacuous -- review A6).
_CALL_THREADS: list = []


def _observed_completion(spec, prompt, **kw):
    _PROMPTS.append(prompt)
    _CALL_THREADS.append(threading.current_thread().name)
    return _REAL_COMPLETION(spec, prompt, **kw)


set_pieces("app.services.orchestrator", "run_completion", _observed_completion)


# --- fixtures ----------------------------------------------------------------
def _key_project():
    with Session(engine) as s:
        p = content.project_create(
            s,
            "KeyP-%d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            target_locales=TGT,
            default_provider="mock",
            project_type=ProjectType.KEY_BASED,
        )
        for name, text in STRINGS:
            keys.create_key(s, p, name, base_value=text)
        doc = keys.internal_document(s, p, "", create=False)
        return p.id, doc.id


def _document_project():
    with Session(engine) as s:
        p = content.project_create(
            s,
            "DocP-%d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            target_locales=TGT,
            default_provider="mock",  # noqa: RUF100, UP031
        )
        doc = Document(
            project_id=p.id,
            filename="strings.md",
            fmt="md",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": t} for _n, t in STRINGS]},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, (_name, text) in enumerate(STRINGS):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=TGT,
                    order_index=i,
                    source_text=text,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return p.id, doc.id


def _workflow(name="WF"):
    with Session(engine) as s:
        w = Workflow(
            name="%s-%d" % (name, _seq()),  # noqa: UP031
            scope="global",
            source_locale=SRC,
            target_locale=TGT,  # noqa: RUF100, UP031
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
            prompt_text="Translate to {target_locale} ({glossary}): {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        return w.id


def _run(wid, doc_id, actor=""):
    _PROMPTS.clear()
    with Session(engine) as s:
        if actor:
            with keys.actor_scope(actor):
                events = list(orchestrator.run_workflow_stream(s, wid, doc_id, TGT))
        else:
            events = list(orchestrator.run_workflow_stream(s, wid, doc_id, TGT))
    return events, list(_PROMPTS)


def _run_through_the_real_launcher(wid, doc_id, actor="", timeout=180):
    """Launch the way EVERY web/batch surface does: `orchestrator.start_run` creates
    the WorkflowRun and hands execution to a DAEMON THREAD, returning immediately.

    This is the shape review A6 was about. The old [F1] fixture drained
    `run_workflow_stream` inline, so it proved actor attribution only in the CLI's
    same-thread case -- while the web launcher, the batch dispatcher and every real
    caller cross a thread boundary the thread-local actor did not survive. Nothing here
    touches orchestrator.py (the serialization law); the fixture just stops pretending
    the hand-off does not happen."""
    _PROMPTS.clear()
    _CALL_THREADS.clear()
    with Session(engine) as s:
        if actor:
            with keys.actor_scope(actor):
                run_id = orchestrator.start_run(s, wid, doc_id, TGT)
        else:
            run_id = orchestrator.start_run(s, wid, doc_id, TGT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with Session(engine) as s:
            run = s.get(WorkflowRun, run_id)
            if run is not None and run.status in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.PAUSED,
            ):
                return run_id, run.status
        time.sleep(0.05)
    _fail(f"the backgrounded run {run_id} never reached a terminal state")


def _segments(doc_id):
    with Session(engine) as s:
        rows = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == TGT)
        ).all()
        return [
            (r.order_index, r.source_text, r.target_text, r.status, r.tm_match_pct, r.mt_confidence)
            for r in sorted(rows, key=lambda x: x.order_index)
        ]


def _seed_tm(project_id, source, target):
    with Session(engine) as s:
        s.add(
            SegmentTM(
                project_id=project_id,
                source_locale=SRC,
                target_locale=TGT,
                source_text=source,
                target_text=target,
                source_hash=tm.hash_text(source, SRC, TGT),
            )
        )
        s.commit()


def _seed_glossary(project_id, source_term, target_term):
    with Session(engine) as s:
        s.add(
            GlossaryEntry(
                project_id=project_id,
                source_locale=SRC,
                target_locale=TGT,
                source_term=source_term,
                target_term=target_term,
            )
        )
        s.commit()


# ============================================================================
# [A] Parity
# ============================================================================
def test_a_prompts_and_results_are_identical():
    kp, kdoc = _key_project()
    dp, ddoc = _document_project()
    wid = _workflow()

    _events_k, prompts_k = _run(wid, kdoc)
    _events_d, prompts_d = _run(wid, ddoc)

    assert prompts_k, "the key project's run made no provider call at all"
    # MT calls run on a thread pool (orchestrator `_mt_workers`), so the fake provider
    # records them in COMPLETION order: compare the prompts as a multiset (b-qa2 gate:
    # the order flaked under load on the LLM server).
    assert sorted(prompts_k) == sorted(prompts_d), (
        f"the prompt a KEY value produces differs from the document one:\n  key: {prompts_k!r}\n"
        f"  doc: {prompts_d!r}"
    )

    seg_k, seg_d = _segments(kdoc), _segments(ddoc)
    assert [r[1:] for r in seg_k] == [r[1:] for r in seg_d], (seg_k, seg_d)
    assert all(r[2].strip() for r in seg_k), f"the key run wrote no targets: {seg_k}"
    assert all(r[3] == SegStatus.MT for r in seg_k), seg_k

    # The run itself is an ordinary WorkflowRun against an ordinary Document row --
    # no FK surgery, no polymorphic target, so run history/SSE work unchanged.
    with Session(engine) as s:
        run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == kdoc)).first()
        assert run is not None and run.target_locale == TGT
        steps = s.exec(select(StepRun).where(StepRun.workflow_run_id == run.id)).all()
        assert len(steps) == 1, steps
    print("  [A1] identical prompts, targets and statuses for key vs document content")
    return kp, kdoc, dp, ddoc, wid


def test_a_key_values_carry_the_key_identity():
    """Parity must not have been bought by making key segments indistinguishable: the
    rows the pipeline just wrote are still addressable AS KEY VALUES."""
    kp, kdoc = _key_project()
    wid = _workflow()
    _run(wid, kdoc)
    with Session(engine) as s:
        for name, source in STRINGS:
            key = keys.get_key(s, kp, name)
            vals = keys.values_for(s, key, TGT)
            assert len(vals) == 1 and vals[0].source_text == source, vals
            assert vals[0].target_text.strip(), f"no translation landed on {name}"
            assert vals[0].document_id == kdoc
    print("  [A2] the translated rows are still addressable by key identity")


# ============================================================================
# [B] TM leverage
# ============================================================================
def test_b_tm_short_circuit_is_identical():
    kp, kdoc = _key_project()
    dp, ddoc = _document_project()
    _seed_tm(kp, STRINGS[0][1], "Bienvenido al panel.")
    _seed_tm(dp, STRINGS[0][1], "Bienvenido al panel.")
    wid = _workflow()

    _e, prompts_k = _run(wid, kdoc)
    _e, prompts_d = _run(wid, ddoc)
    # The TM hit SKIPS a provider call -- for both shapes, by the same count.
    assert len(prompts_k) == len(STRINGS) - 1 == len(prompts_d), (len(prompts_k), len(prompts_d))
    assert sorted(prompts_k) == sorted(prompts_d)

    seg_k, seg_d = _segments(kdoc), _segments(ddoc)
    assert seg_k[0][2] == "Bienvenido al panel." == seg_d[0][2], (seg_k[0], seg_d[0])
    assert seg_k[0][4] == 100 == seg_d[0][4], f"TM provenance differs: {seg_k[0]} vs {seg_d[0]}"
    assert [r[1:] for r in seg_k] == [r[1:] for r in seg_d]
    print("  [B1] a 100% TM hit short-circuits a key segment exactly as a document one")


# ============================================================================
# [C] Glossary
# ============================================================================
def test_c_glossary_reaches_the_key_prompt():
    kp, kdoc = _key_project()
    dp, ddoc = _document_project()
    _seed_glossary(kp, "dashboard", "panel de control")
    _seed_glossary(dp, "dashboard", "panel de control")
    wid = _workflow()

    _e, prompts_k = _run(wid, kdoc)
    _e, prompts_d = _run(wid, ddoc)
    hits = [p for p in prompts_k if "panel de control" in p]
    assert hits, f"the glossary term never reached the key prompt: {prompts_k!r}"
    assert sorted(prompts_k) == sorted(prompts_d), (
        "glossary injection differs between the two shapes"
    )
    print("  [C1] glossary matching fires on key segments, byte-identically")


# ============================================================================
# [D] The shared safety perimeter
# ============================================================================
def test_d_refusal_guard_is_the_same_code():
    """`quality.commit_mt_output` is the ONE sanctioned MT write path. Feeding it the
    same refusal text with a key segment and a document segment must produce the same
    outcome -- if the key layer had forked the write, this is where it would show."""
    kp, kdoc = _key_project()
    _dp, ddoc = _document_project()
    refusal = "I'm sorry, but I can't help with that request."
    with Session(engine) as s:
        kseg = s.exec(select(Segment).where(Segment.document_id == kdoc)).first()
        dseg = s.exec(select(Segment).where(Segment.document_id == ddoc)).first()
        out_k = quality.commit_mt_output(s, kseg, refusal, source="mt")
        out_d = quality.commit_mt_output(s, dseg, refusal, source="mt")
        s.commit()
        assert out_k.flagged == out_d.flagged is True, (out_k, out_d)
        assert out_k.tm_eligible == out_d.tm_eligible is False, (out_k, out_d)
        assert kseg.status == dseg.status == SegStatus.NEEDS_REVIEW, (kseg.status, dseg.status)
        assert kseg.target_text == dseg.target_text == "", "a refusal was WRITTEN"
        assert [f.get("type") for f in (kseg.qa_flags or [])] == [
            f.get("type") for f in (dseg.qa_flags or [])
        ], (kseg.qa_flags, dseg.qa_flags)
        # ...and the key value is now visibly in review through the KEY api too.
        key = keys.get_key(s, kp, STRINGS[0][0])
        assert any(v.status == SegStatus.NEEDS_REVIEW for v in keys.values_for(s, key))
    print("  [D1] the refusal guard / NEEDS_REVIEW routing is the same code for keys")


def test_d_imported_refusal_is_quarantined():
    """`keys.set_value` splits on PROVENANCE: human text is content, but an IMPORTED
    value is foreign text and goes through the choke point. A .po whose msgstr is an
    LLM hedge must be quarantined on the way in -- not stored and shipped."""
    from app.services import refusal

    hedge = (
        "I'm sorry, but I can't help with that. Please provide the source text "
        "you'd like translated."
    )
    assert refusal.is_refusal(hedge), "the fixture must itself be a detected refusal"
    kp, _kdoc = _key_project()
    with Session(engine) as s:
        key = keys.get_key(s, kp, STRINGS[0][0])
        seg = keys.set_value(s, key, TGT, hedge, origin=keys.ORIGIN_IMPORT)
        assert seg.target_text == "", f"an imported refusal was WRITTEN: {seg.target_text!r}"
        assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
        assert any(f.get("type") == "refusal" for f in (seg.qa_flags or [])), seg.qa_flags
        # ...while the SAME text typed by a human is content and is stored.
        key2 = keys.get_key(s, kp, STRINGS[1][0])
        # ORIGIN_HUMAN is the explicitly-stated posture (the parameter has no default
        # any more -- review A7: forgetting it must be a TypeError, not a free pass).
        human = keys.set_value(s, key2, TGT, hedge, origin=keys.ORIGIN_HUMAN)
        assert human.target_text == hedge, "a human edit was gated"
        assert human.status == SegStatus.EDITED, human.status
    print("  [D2] an IMPORTED refusal is quarantined; the same text typed by a human is not")


def test_d_needs_review_value_is_tm_ineligible():
    """The NEEDS_REVIEW perimeter is not re-implemented for keys: a flagged key value
    is refused by the same `tm.write_back` guard that protects document content."""
    kp, _kdoc = _key_project()
    dp, ddoc = _document_project()
    # The flag shape is the REAL one the FULL_TEXT revert writes (quality.
    # source_fallback_indices reads `indices`); a flag without it is not a flag, which
    # is precisely the fixture-realism trap this repo has been bitten by before.
    flag = [
        {
            "type": quality.SOURCE_FALLBACK_FLAG_TYPE,
            "severity": "major",
            "message": "source substituted",
            "indices": [0],
        }
    ]
    with Session(engine) as s:
        key = keys.get_key(s, kp, STRINGS[0][0])
        kseg = keys.values_for(s, key, TGT)[0]
        dseg = s.exec(select(Segment).where(Segment.document_id == ddoc)).first()
        for seg, pid in ((kseg, kp), (dseg, dp)):  # noqa: B007
            seg.target_text, seg.status, seg.qa_flags = ("Bienvenido", SegStatus.NEEDS_REVIEW, flag)
            s.add(seg)
        s.commit()
        krow = tm.write_back(s, kp, kseg.source_text, kseg.target_text, SRC, TGT, segment=kseg)
        drow = tm.write_back(s, dp, dseg.source_text, dseg.target_text, SRC, TGT, segment=dseg)
        assert krow is None and drow is None, (
            f"a source_fallback-flagged value was written to TM (key={krow} doc={drow})"
        )
        # ...and with the flag CLEARED both are accepted -- so the refusal really is
        # the flag, not an accident of the key shape.
        kseg.qa_flags = dseg.qa_flags = []
        s.add(kseg)
        s.add(dseg)
        s.commit()
        assert (
            tm.write_back(s, kp, kseg.source_text, kseg.target_text, SRC, TGT, segment=kseg)
            is not None
        )
        assert (
            tm.write_back(s, dp, dseg.source_text, dseg.target_text, SRC, TGT, segment=dseg)
            is not None
        )
    print("  [D3] a flagged key value is refused by the shared TM write-back guard")


# ============================================================================
# [E] The export tag guard
# ============================================================================
def test_e_export_tag_guard_fires_on_a_key_value():
    kp, _kdoc = _key_project()
    with Session(engine) as s:
        key = keys.get_key(s, kp, STRINGS[0][0])
        seg = keys.values_for(s, key, TGT)[0]
        # A source with an inline-fidelity token and a target that DROPPED it: the
        # exact damage class the guard exists for.
        seg.source_text = "Click ⟦g1⟧Save⟦/g1⟧ now."
        seg.target_text = "Haz clic en Guardar ahora."
        seg.status = SegStatus.MT
        s.add(seg)
        s.commit()
        assert export_svc.segment_tag_damage(seg), (
            "the shared guard does not consider this damaged; the fixture is wrong"
        )
        # THE BUILD FILE (namespace-scoped, bare names) is what the guard protects:
        # damaged markup must never ship in a deliverable. Live-QA K1 scoped the
        # substitution to this shape; the transport half is asserted below.
        report = []
        data = key_files.export_locale(s, kp, TGT, namespace="", report=report)
        body = data.decode("utf-8")
        assert "Haz clic" not in body, "a tag-damaged key value shipped anyway"
        assert "Save" in body, f"the source substitute did not land: {body}"
        assert report and report[0]["unit"] == "key", report
        # `index` is the name AS EXPORTED -- a scoped export keeps BARE names. The
        # manifest has to name the string the way the FILE does, or a reader cannot
        # find it.
        assert report[0]["index"] == STRINGS[0][0], report
        assert report[0]["kind"] == export_svc.SUB_AUTOMATIC, report
        # `substitution_detail` -- the ONE ASCII rendering used by audit rows, the CLI
        # and manifests -- reads a key report unchanged.
        assert "substituted=1" in export_svc.substitution_detail(report)
        # THE TRANSPORT FILE carries the STORED text instead (live-QA K1): it exists to
        # be re-imported, and substituting there wrote English over the real
        # translation on the way back in. Still reported -- under its own kind, so the
        # substitution count stays honest.
        trep = []
        tbody = key_files.export_locale(s, kp, TGT, report=trep).decode("utf-8")
        assert "Haz clic" in tbody, "the transport file must carry the stored value"
        assert trep and trep[0]["kind"] == key_files.SUB_KEPT_FOR_TRANSPORT, trep
        assert trep[0]["index"] == "__default__::" + STRINGS[0][0], trep
        assert not key_files.substituted_keys(trep), trep
        assert "substituted=" not in export_svc.substitution_detail(
            [e for e in trep if e["kind"] == export_svc.SUB_AUTOMATIC]
        )
    print(
        "  [E1] the export tag guard substitutes a damaged key value in the BUILD "
        "file and reports it; the TRANSPORT file carries the stored text, also "
        "reported"
    )


# ============================================================================
# [F] The workflow write-back reaches the delta feed
# ============================================================================
def test_f_workflow_writeback_is_in_the_feed():
    """Review A6. The write-back is the entry the delta feed cares most about, and it
    is produced on a thread the launcher started -- so the launch goes through
    `orchestrator.start_run` here, not through an inline stream drain. That the run
    genuinely crossed a thread is asserted, not assumed."""
    kp, kdoc = _key_project()
    wid = _workflow()
    with Session(engine) as s:
        before = len(list(s.exec(select(KeyChangeLog).where(KeyChangeLog.project_id == kp)).all()))
    _run_id, status = _run_through_the_real_launcher(wid, kdoc, actor="workflow:test")
    assert status == RunStatus.COMPLETED, status
    assert _CALL_THREADS and all(t != "MainThread" for t in _CALL_THREADS), (
        "the launcher did NOT hand the run to a background thread, so this fixture "
        f"cannot say anything about actor attribution across threads: {_CALL_THREADS}"
    )
    with Session(engine) as s:
        d = keys.changes_since(s, kp, 0)
        fresh = d["entries"][before:]
        values = [e for e in fresh if e["entity"] == KeyEntity.VALUE]
        assert len(values) == len(STRINGS), (
            f"the workflow's writes did not reach the delta feed: {fresh}"
        )
        assert all(e["target_locale"] == TGT for e in values), values
        assert all(e["actor"] == "workflow:test" for e in values), (
            "actor_scope did not attribute the run: %s" % [e["actor"] for e in values]
        )
        names = sorted(e["name"] for e in values)
        assert names == sorted(n for n, _t in STRINGS), names
    print("  [F1] a workflow's write-back appears in the delta feed, correctly attributed")


def main():
    test_a_prompts_and_results_are_identical()
    test_a_key_values_carry_the_key_identity()
    test_b_tm_short_circuit_is_identical()
    test_c_glossary_reaches_the_key_prompt()
    test_d_refusal_guard_is_the_same_code()
    test_d_imported_refusal_is_quarantined()
    test_d_needs_review_value_is_tm_ineligible()
    test_e_export_tag_guard_fires_on_a_key_value()
    test_f_workflow_writeback_is_in_the_feed()
    print("KEYS PIPELINE OK")


if __name__ == "__main__":
    main()
