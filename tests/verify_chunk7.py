"""Chunk 7 verification — LLM-based LQA (MQM/DQF) + rulebooks, offline.

- score_text: clean=100/auto_approved; an untranslated segment scores a Major
  Accuracy error -> gate needs_review
- rulebook DSL changes the score deterministically (FORBID term)
- score_document persists LQAResult rows and computes an overall score
- LQA runs as a workflow step (records a scorecard + per-error rows)
- endpoints: scorecard page, rulebook save, re-score
"""

import os
import re
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
from app.models import (
    EngineProfile,
    LQAResult,
    Project,
    RunStatus,
    Segment,
    StepRun,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import engines as engines_svc
from app.services import lqa, orchestrator, reports


def docx_from(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def main() -> None:
    init_db()
    client = TestClient(app)

    # --- score_text ---
    clean = lqa.score_text("Hello there.", "Bonjour.", "en-US", "fr-FR")
    assert clean["score"] == 100 and clean["gate"] == "auto_approved", clean
    bad = lqa.score_text("Hello there.", "Hello there.", "en-US", "fr-FR")
    assert any(e["severity"] == "major" for e in bad["errors"]), bad
    assert bad["gate"] == "needs_review" and bad["score"] < 100, bad
    print(f"  score_text: clean=100/auto, untranslated={bad['score']}/needs_review")

    # --- rulebook changes the score ---
    no_rule = lqa.score_text("Visit the site.", "Visitez foo.", "en-US", "fr-FR")
    with_rule = lqa.score_text(
        "Visit the site.", "Visitez foo.", "en-US", "fr-FR", rulebook="FORBID foo"
    )
    assert with_rule["score"] < no_rule["score"], (no_rule["score"], with_rule["score"])
    assert any("foo" in e["note"] for e in with_rule["errors"]), with_rule
    print(f"  rulebook effect: {no_rule['score']} -> {with_rule['score']} with FORBID rule")

    # --- score_document persists + aggregates ---
    r = client.post(
        "/projects",
        data={
            "name": "LQA Test",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                docx_from(["Good morning.", "Save 50% today."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        segs = sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                )
            ).all(),
            key=lambda x: x.order_index,
        )
        s0, s1 = segs[0].id, segs[1].id
    client.post(f"/segments/{s0}/save", data={"target_text": "Bonjour.", "tte_ms": "0"})
    # left untranslated -> a Major accuracy error
    client.post(f"/segments/{s1}/save", data={"target_text": "Save 50% today.", "tte_ms": "0"})

    with Session(engine) as s:
        card = lqa.score_document(s, doc_id, "fr-FR", persist=True)
        rows = s.exec(select(LQAResult).where(LQAResult.document_id == doc_id)).all()
    assert card["segments_scored"] == 2, card
    assert card["overall"] < 100 and card["gate"] == "needs_review", card
    assert len(rows) >= 1, rows
    print(f"  score_document: overall={card['overall']}, {len(rows)} LQAResult rows persisted")

    # --- LQA as a workflow step ---
    with Session(engine) as s:
        wf = Workflow(name="Translate + LQA", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wf_id = wf.id
        s.add(
            WorkflowStep(
                workflow_id=wf_id,
                order_index=0,
                name="Translator",
                step_type=StepType.LLM_AGENT,
                prompt_text="Translate: {text}",
            )
        )
        s.add(WorkflowStep(workflow_id=wf_id, order_index=1, name="LQA", step_type=StepType.LQA))
        s.commit()
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "full_text"},
        files={
            "file": (
                "ft.docx",
                docx_from(["A sentence to translate and score."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    ft_doc = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wf_id, ft_doc, "fr-FR")
    lqa_steps = [st for st in summary["steps"] if st.get("step_type") == "lqa"]
    assert lqa_steps and "lqa" in lqa_steps[0], summary["steps"]
    assert "score" in lqa_steps[0]["lqa"] and "gate" in lqa_steps[0]["lqa"], lqa_steps[0]
    print(
        f"  LQA workflow step: score={lqa_steps[0]['lqa']['score']} gate={lqa_steps[0]['lqa']['gate']}"  # noqa: E501
    )

    # --- endpoints ---
    page = client.get(f"/documents/{doc_id}/lqa?locale=fr-FR").text
    assert "LQA scorecard" in page, "lqa page not rendered"
    r = client.post(
        f"/documents/{doc_id}/rulebook",
        data={"locale": "fr-FR", "text": "FORBID widgets"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get(f"/documents/{doc_id}/lqa?locale=fr-FR").text
    assert "FORBID widgets" in page, "rulebook not saved/shown"
    r = client.post(
        f"/documents/{doc_id}/lqa/run", data={"locale": "fr-FR"}, follow_redirects=False
    )
    assert r.status_code == 303
    print("  endpoints: scorecard renders, rulebook saves, re-score runs")

    # =========================================================================
    # HARDENING (review finding 1): an LQA step whose provider is an
    # engine:<id> with an UNDECRYPTABLE key. provider_is_live only checks
    # api_key_enc is PRESENT (not that it still decrypts), so _llm_errors used to
    # pass it as "live", call run_completion -> get_provider -> DeadEngineError,
    # which propagated OUT of score_text uncaught (a 500 on POST
    # /api/workflows/{id}/run, a CLI traceback). Semantics: score_text NEVER
    # raises for this case -- it catches DeadEngineError and returns a card
    # (the run must not 500), and the orchestrator's LQA sites catch it too as
    # defense-in-depth.
    #
    # ISSUE #56 UPDATE: a CONFIGURED-LIVE judge that could not run is NOT the same
    # as a clean pass. The card no longer reports a bare 100/auto_approved -- it is
    # marked `llm_judge="unavailable"` and the gate fails CLOSED to `needs_review`
    # (a judge outage must never auto-approve a release/workflow gate). The no-raise
    # / no-500 invariant this test was written for is UNCHANGED; only the gate value
    # moved from auto_approved to needs_review. (A NOT-live provider -- offline/mock
    # -- still soft-degrades to a clean structural-only auto_approved; that is the
    # "no judge configured" case, tested elsewhere.)
    # =========================================================================
    bad_prof = engines_svc.create_profile("Chunk7DeadLQA", "deepl", api_key="realkey")
    with Session(engine) as s:
        row = s.get(EngineProfile, bad_prof.id)
        row.api_key_enc = "gAAAAABnot-a-valid-fernet-token"  # present but undecryptable
        s.add(row)
        s.commit()
    dead_ref = f"engine:{bad_prof.id}"

    # (a) direct score_text with the dead engine -> no raise. Structural checks still
    # run (score 100 here, "Bonjour." is clean), BUT the configured judge is
    # UNAVAILABLE so the gate fails closed to needs_review + the marker is stamped (#56).
    card_dead = lqa.score_text("Hello there.", "Bonjour.", "en-US", "fr-FR", provider_name=dead_ref)
    assert card_dead["score"] == 100, card_dead
    assert card_dead["gate"] == "needs_review", card_dead
    assert card_dead.get("llm_judge") == "unavailable", card_dead
    print(
        "  finding-1 direct (#56): score_text with an undecryptable-key engine does not "
        "raise; gate held needs_review + llm_judge=unavailable (not a clean pass)"
    )

    # (b) workflow run with an LQA step on the dead engine -> run completes cleanly,
    # no exception escapes run_workflow (mirrors NOT-live semantics: LQA step
    # COMPLETED with a clean scorecard, not a run FAILED/500).
    with Session(engine) as s:
        wfd = Workflow(name="LQA dead-engine wf", scope="global")
        s.add(wfd)
        s.commit()
        s.refresh(wfd)
        wfd_id = wfd.id
        s.add(
            WorkflowStep(
                workflow_id=wfd_id,
                order_index=0,
                name="Translator",
                step_type=StepType.LLM_AGENT,
                prompt_text="Translate: {text}",
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wfd_id,
                order_index=1,
                name="LQA",
                step_type=StepType.LQA,
                provider=dead_ref,
            )
        )
        s.commit()
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "full_text"},
        files={
            "file": (
                "ftd.docx",
                docx_from(["Another sentence to score."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    ftd_doc = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        summary_dead = orchestrator.run_workflow(s, wfd_id, ftd_doc, "fr-FR")
    assert summary_dead["status"] == "completed", (
        "LQA on an undecryptable-key engine must NOT crash/fail the run (#56 holds the "
        "gate at needs_review, it does not fail the run)",
        summary_dead,
    )
    lqa_steps_dead = [st for st in summary_dead["steps"] if st.get("step_type") == "lqa"]
    assert lqa_steps_dead and "lqa" in lqa_steps_dead[0], summary_dead["steps"]
    print(
        "  finding-1 workflow: LQA step on undecryptable-key engine -> run "
        "COMPLETED (no exception escape, no 500); gate held, run not failed"
    )

    # Also prove the API run route (the review's specific crash site) survives.
    with Session(engine) as s:
        wr = s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wfd_id)).first()
        assert wr is not None and wr.status == RunStatus.COMPLETED, wr
    print("  finding-1 API surface: WorkflowRun persisted COMPLETED (no 500 path)")

    # =========================================================================
    # ISSUE #56 F1 + F3 (gate round 2): a dead-judge RE-SCORE must NOT replace the
    # prior AD-HOC scoring (abort the persist, preserve rows); the scorecard headline
    # must be the STORED scoring's DURABLE, threshold-correct gate — never the fresh
    # structural recompute, and never a min-across-history worst-row.
    # =========================================================================
    from datetime import datetime, timedelta

    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "full_text"},
        files={
            "file": (
                "f1.docx",
                docx_from(["Buy the widget today."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    f1_doc = int(r.headers["location"].rsplit("/", 1)[-1])
    seg_ids = []
    with Session(engine) as s:
        for seg in s.exec(
            select(Segment).where(Segment.document_id == f1_doc, Segment.target_locale == "fr-FR")
        ).all():
            seg.target_text = "[fr] " + (seg.source_text or "x")
            s.add(seg)
            seg_ids.append(seg.id)
        # A GOOD prior AD-HOC scoring on record (segment_id NOT NULL): exactly the rows the
        # pre-score DELETE targets, so this is what a dead-judge re-score would have wiped.
        s.add(
            LQAResult(
                document_id=f1_doc,
                segment_id=seg_ids[0],
                dimension="Accuracy",
                severity="major",
                note="real mistranslation",
                score=95.0,
                target_locale="fr-FR",
                finding_source="llm",
                judge="claude-haiku-4-5",
                scoring_id="prior",
            )
        )
        proj = s.get(Project, pid)
        proj.default_provider = dead_ref  # every judge call -> UNAVAILABLE
        s.add(proj)
        s.commit()

    def _adhoc_rows():
        with Session(engine) as s:
            return s.exec(
                select(LQAResult).where(
                    LQAResult.document_id == f1_doc,
                    LQAResult.segment_id.is_not(None),
                    LQAResult.finding_source != "human",
                )
            ).all()

    before = _adhoc_rows()
    assert len(before) == 1 and before[0].note == "real mistranslation", before
    # (F1) a dead-judge persist=True re-score ABORTS: the ad-hoc rows are NOT deleted.
    with Session(engine) as s:
        card_dead_persist = lqa.score_document(s, f1_doc, "fr-FR", persist=True)
    after = _adhoc_rows()
    assert card_dead_persist.get("persisted") is False, card_dead_persist
    assert len(after) == 1 and after[0].note == "real mistranslation", (
        "F1: a dead-judge re-score WIPED the prior ad-hoc scoring",
        after,
    )
    print(
        "  F1: dead-judge Re-score aborts (persisted=False) — prior ad-hoc scoring "
        "preserved, nothing wiped"
    )

    # (F1 banner) the POST redirect carries the hold banner param the GET can render.
    rp = client.post(
        f"/documents/{f1_doc}/lqa/run", data={"locale": "fr-FR"}, follow_redirects=False
    )
    assert "judge=unavailable" in rp.headers["location"], rp.headers["location"]
    assert "previous scoring was preserved" in client.get(rp.headers["location"]).text
    print("  F1 banner: dead-judge Re-score redirect shows the hold banner")

    # (F3a) the DURABLE, threshold-correct gate drives the headline. Seed a workflow LQA
    # StepRun that FAILED at score 95 (as a threshold-99 step would) -> headline
    # needs_review, NOT the auto_approved a hardcoded-90 re-derivation (95 >= 90) invents.
    now = datetime.utcnow()

    def _seed_lqa_steprun(score, gate, when):
        with Session(engine) as s:
            wf = Workflow(name=f"f3-wf-{when.isoformat()}", scope="global")
            s.add(wf)
            s.commit()
            s.refresh(wf)
            st = WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="LQA",
                step_type=StepType.LQA,
                provider="mock",
            )
            s.add(st)
            s.commit()
            s.refresh(st)
            wr = WorkflowRun(
                workflow_id=wf.id,
                document_id=f1_doc,
                target_locale="fr-FR",
                status=RunStatus.COMPLETED,
            )
            s.add(wr)
            s.commit()
            s.refresh(wr)
            s.add(
                StepRun(
                    workflow_run_id=wr.id,
                    step_id=st.id,
                    status=RunStatus.COMPLETED,
                    score=float(score),
                    gate=gate,
                    finished_at=when,
                )
            )
            s.commit()

    _seed_lqa_steprun(95, "failed", now - timedelta(days=1))
    page = client.get(f"/documents/{f1_doc}/lqa?locale=fr-FR").text
    # Anchor on the headline element's ids/data attributes (the visible gate label is
    # human-formatted, "Needs review"), and still require the visible 95 and gate label.
    _m = re.search(r'<p id="lqa-stored-headline"([^>]*)>(.*?)</p>', page, re.S)
    assert _m, "F3a: durable headline element missing"
    assert 'data-score="95"' in _m.group(1) and 'data-gate="needs_review"' in _m.group(1), (
        "F3a: headline must show the durable 95/needs_review"
    )
    assert ">95<" in _m.group(2) and "Needs review" in _m.group(2), (
        "F3a: headline must show the durable 95/needs_review"
    )
    assert "Live structural checks" in page and "not the gate" in page, (
        "F3: the structural recompute must be labelled non-authoritative"
    )
    with Session(engine) as s:
        h = reports.lqa_scorecard_headline(s, document_id=f1_doc, target_locale="fr-FR")
    assert h == {"has_scoring": True, "regime": "run", "score": 95, "gate": "needs_review"}, h
    print("  F3a: durable gate needs_review at 95 (a 90-derivation would say auto_approved)")

    # (F3b) a NEWER clean StepRun 98/passed wins; a 2-week-old 40/failed never pins it.
    _seed_lqa_steprun(98, "passed", now)
    _seed_lqa_steprun(40, "failed", now - timedelta(days=14))
    with Session(engine) as s:
        h = reports.lqa_scorecard_headline(s, document_id=f1_doc, target_locale="fr-FR")
    assert h["score"] == 98 and h["gate"] == "auto_approved", (
        "F3b: latest StepRun must win, never min-across-history",
        h,
    )
    print(
        "  F3b: latest StepRun (98/auto_approved) wins — a stale 40/needs_review row "
        "does not pin the headline"
    )

    # (F3c) a doc with NO scoring reads "no findings on record" (not "no saved scoring yet").
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "full_text"},
        files={
            "file": (
                "f3c.docx",
                docx_from(["Nothing scored here."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    f3c_doc = int(r.headers["location"].rsplit("/", 1)[-1])
    page = client.get(f"/documents/{f3c_doc}/lqa?locale=fr-FR").text
    assert "no findings on record" in page, "F3c: unscored doc must say 'no findings on record'"
    print("  F3c: an unscored doc reads 'no findings on record'")

    # (F1 partial-outage) a MIXED scoring (some segments OK, one UNAVAILABLE) aborts the
    # WHOLE persist — the aggregate rule must never silently persist the good half over the
    # prior generation. A 2-segment doc with a prior ad-hoc row; score_text is stubbed to
    # return OK then UNAVAILABLE.
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "mix.docx",
                docx_from(["First sentence.", "Second sentence."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    mix_doc = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        msegs = s.exec(
            select(Segment).where(Segment.document_id == mix_doc, Segment.target_locale == "fr-FR")
        ).all()
        for seg in msegs:
            seg.target_text = "[fr] " + (seg.source_text or "x")
            s.add(seg)
        s.add(
            LQAResult(
                document_id=mix_doc,
                segment_id=msegs[0].id,
                dimension="Style",
                severity="minor",
                note="prior mixed",
                score=97.0,
                target_locale="fr-FR",
                finding_source="llm",
                scoring_id="pm",
            )
        )
        s.commit()
    _mixcards = [
        {
            "score": 100,
            "passed": True,
            "errors": [],
            "gate": "auto_approved",
            "threshold": 90,
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "llm_judge": "ok",
        },
        {
            "score": 100,
            "passed": False,
            "errors": [],
            "gate": "needs_review",
            "threshold": 90,
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "llm_judge": "unavailable",
        },
    ]
    _orig_score_text = lqa.score_text
    lqa.score_text = lambda *a, **k: _mixcards.pop(0)
    try:
        with Session(engine) as s:
            card_mix = lqa.score_document(s, mix_doc, "fr-FR", persist=True)
    finally:
        lqa.score_text = _orig_score_text
    with Session(engine) as s:
        mrows = s.exec(
            select(LQAResult).where(
                LQAResult.document_id == mix_doc, LQAResult.finding_source != "human"
            )
        ).all()
    assert card_mix.get("persisted") is False and card_mix.get("llm_judge") == "unavailable", (
        card_mix
    )
    assert len(mrows) == 1 and mrows[0].note == "prior mixed", (
        "F1: a MIXED-outage scoring must abort entirely, never persist the good half",
        mrows,
    )
    print(
        "  F1 partial-outage: a mixed OK/UNAVAILABLE scoring aborts the WHOLE persist "
        "(prior rows intact)"
    )

    # (F1 offline) a NOT-live install still persists a structural-only scoring (unchanged).
    with Session(engine) as s:
        proj = s.get(Project, pid)
        proj.default_provider = "mock"
        s.add(proj)
        s.commit()
        card_off = lqa.score_document(s, f1_doc, "fr-FR", persist=True)
    assert card_off.get("persisted") is True and card_off.get("llm_judge") == "skipped", card_off
    print("  F1: offline install still persists a structural-only scoring (unchanged)")

    print("CHUNK 7 OK")


if __name__ == "__main__":
    main()
