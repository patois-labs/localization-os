"""FX-5 regressions. Offline fixtures shared with the auto-fix suite."""

import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

from verify_qe_autofix import (
    JUDGE_CALLS,
    AFProvider,
    ProviderSpec,
    Session,
    TestClient,
    api_headers,
    app,
    db_engine,
    engines,
    fake_post,
    init_db,
    make_wf,
    new_doc,
    orchestrator,
    qe,
    qe_autofix,
    qe_jev,
    register_provider,
    run,
    segs_for,
    select,
)

from app.models import RunStatus, Segment, SegStatus, WorkflowRun, WorkflowStep
from app.services import qe_jev_loop, quality
from app.web.api_cli import review_summary_line
from app.web.api_cli_misc_1 import _segment_detail_row


def test_deterministic():
    terms = [
        {"source_term": "Nimbus", "target_term": "Nimbus", "dnt": True},
        {"source_term": "dashboard", "target_term": "tableau", "dnt": False},
    ]
    human = {"type": "reviewer", "message": "Keep my note", "severity": "major"}
    with Session(db_engine) as s:
        seg = Segment(source_text="Nimbus dashboard 42", target_locale="fr-FR", qa_flags=[human])
        quality.commit_mt_output(s, seg, "Autre ecran 43", source="workflow", glossary_terms=terms)
        assert {"dnt_violation", "glossary_violation", "number_integrity"} <= {
            f["type"] for f in seg.qa_flags
        }
        before = list(seg.qa_flags)
        quality.commit_mt_output(s, seg, "Autre ecran 43", source="workflow", glossary_terms=terms)
        assert seg.qa_flags == before
        quality.commit_mt_output(s, seg, "Autre vue 43", source="workflow")
        assert seg.qa_flags == before, "changed text without terms must preserve terminology"
        quality.commit_mt_output(
            s, seg, "Nimbus tableau 42", source="workflow", glossary_terms=terms
        )
        assert seg.qa_flags == [human], seg.qa_flags
        seg.qa_flags = before
        quality.commit_mt_output(s, seg, "Nimbus tableau 42", source="workflow", glossary_terms=[])
        assert seg.qa_flags == [human], "an explicit empty term set is still a re-check"


def test_no_terms_callers(client, pid):
    from app.models import Document, Project, ProjectType, TransMode
    from app.services import content, fulltext_paragraphs, keys, tm, translate

    flags = [
        {"type": "dnt_violation", "severity": "major", "message": "keep DNT"},
        {"type": "glossary_violation", "severity": "major", "message": "keep glossary"},
        {"type": "reviewer", "severity": "major", "message": "keep human"},
    ]
    target = "Autre ecran"
    failures = []

    def seed(s, seg, text):
        seg.target_text = text
        seg.status = SegStatus.NEEDS_REVIEW
        seg.qa_flags = list(flags)
        s.add(seg)
        s.commit()

    def check(s, seg, name):
        s.commit()
        s.refresh(seg)
        assert seg.target_text == target, (name, seg.target_text)
        if seg.qa_flags != flags:
            failures.append((name, seg.qa_flags))
        else:
            print(f"  no-terms {name} PASS")

    for path in ("manual TM", "segmented TM", "full-text TM", "full-text save"):
        did = new_doc(client, pid, ["Nimbus dashboard"])
        with Session(db_engine) as s:
            seg = segs_for(s, did)[0]
            doc = s.get(Document, did)
            seed(s, seg, target if path == "full-text save" else "")
            if path == "manual TM":
                with patch.object(
                    tm,
                    "lookup_batch",
                    return_value={seg.source_text: {"match_pct": 100, "target_text": target}},
                ):
                    assert content.apply_tm_to_document(s, doc, s.get(Project, pid), "fr-FR") == 1
            elif path == "full-text save":
                fulltext_paragraphs._commit_body(s, seg, [target], provenance="human")
            else:
                if path == "full-text TM":
                    doc.mode = TransMode.FULL_TEXT
                    s.add(doc)
                    s.commit()
                with (
                    patch.object(tm, "ft_lookup", return_value=target),
                    patch.object(
                        tm,
                        "lookup_batch",
                        return_value={seg.source_text: {"match_pct": 100, "target_text": target}},
                    ),
                ):
                    list(translate.pretranslate_stream(s, did, "fr-FR"))
            check(s, seg, path)

    with Session(db_engine) as s:
        project = content.project_create(
            s,
            "FX5 keys",
            source_locale="en-US",
            target_locales="fr-FR",
            project_type=ProjectType.KEY_BASED,
        )
        key = keys.create_key(s, project, "dashboard")
        keys.ensure_value_rows(s, key)
        seg = keys.values_for(s, key, "fr-FR")[0]
        seg.source_text = "Nimbus dashboard"
        seed(s, seg, "Ancien ecran")
        keys.set_value(s, key, "fr-FR", target, origin="key-import")
        check(s, seg, "key import")

    assert not failures, failures


def test_old_verdicts():
    flags = [qe.qe_flag(60, "old", 70, False, False), qe_autofix.record(outcome="fixed")]
    human = {"type": "reviewer", "message": "Keep my note"}
    with Session(db_engine) as s:
        for changed in (False, True):
            seg = Segment(
                source_text="Hello",
                target_text="Bonjour",
                target_locale="fr-FR",
                qa_flags=[*flags, human],
            )
            quality.commit_mt_output(s, seg, "Salut" if changed else "Bonjour", source="workflow")
            assert seg.qa_flags == ([human] if changed else [*flags, human]), seg.qa_flags
        seg = Segment(
            source_text="Hello",
            target_text="Bonjour",
            target_locale="fr-FR",
            qa_flags=flags,
            status=SegStatus.APPROVED,
        )
        outcome = quality.commit_mt_output(s, seg, "Salut", source="workflow")
        assert not outcome.committed and seg.qa_flags == flags


def test_review_counts():
    def row(flags):
        return _segment_detail_row(
            SimpleNamespace(
                id=1,
                order_index=0,
                status=SegStatus.MT,
                target_text="Bonjour",
                qa_flags=flags,
            )
        )

    passed = qe.qe_flag(100, "clean", 70, True, False)
    fixed = qe_autofix.record(outcome="fixed")
    failed = qe.qe_flag(60, "bad", 70, False, False)
    skipped = qe.qe_flag(None, "", 70, False, True)
    undecided = qe.qe_flag(90, "unsure", 70, False, False)
    tried = qe_autofix.record(outcome="needs_review")
    rows = [row([passed, fixed]), row([failed, tried]), row([skipped]), row([undecided])]
    assert review_summary_line([rows[0]]) is None
    # Skipped QE does not route to review in the product; undecided still asks a person.
    expected = "2 flagged for review (qe 2, auto-fix 1)"
    assert review_summary_line(rows) == expected, review_summary_line(rows)
    assert review_summary_line(json.loads(json.dumps(rows))) == expected
    assert len(rows[0]["qa_flags"]) == 2, "informational badges remain visible"


def test_rerun(client, pid, judge):
    did = new_doc(client, pid, ["BAD one."])
    wid = make_wf("rerun", judge, qe_autofix=True)
    AFProvider.reset()
    run(wid, did)
    with Session(db_engine) as s:
        seg = segs_for(s, did)[0]
        assert {"qe", "qe_autofix"} <= {f["type"] for f in seg.qa_flags}
        local_rows = [_segment_detail_row(seg)]
        step = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid)).first()
        step.qe_gate = False
        s.add(step)
        s.commit()
    response = client.get(f"/api/documents/{did}/segments?detail=segments", headers=api_headers())
    assert response.status_code == 200, response.text
    remote_rows = response.json()["locales"]["fr-FR"]["segment_details"]
    assert remote_rows == local_rows
    assert review_summary_line(remote_rows) is None
    # The same producer now replaces GOOD with BAD, without running a judge.
    AFProvider.reset()
    run(wid, did)
    with Session(db_engine) as s:
        seg = segs_for(s, did)[0]
        assert seg.target_text == "BAD one.", seg.target_text
        assert not any(f["type"] in ("qe", "qe_autofix") for f in seg.qa_flags or [])
        flags = [
            qe.qe_flag(60, "same text", 70, False, False),
            qe_autofix.record(outcome="needs_review"),
        ]
        seg.qa_flags = flags
        seg.status = SegStatus.NEEDS_REVIEW
        s.add(seg)
        s.commit()
    run(wid, did)
    with Session(db_engine) as s:
        assert segs_for(s, did)[0].qa_flags == flags


def test_cancel(client, pid, judge):
    for adaptive in (False, True):
        entered, released = threading.Event(), threading.Event()
        errors, records = [], []
        did = new_doc(client, pid, ["BAD2 one.", "BAD2 two."])
        wid = make_wf(
            "cancel",
            judge,
            qe_autofix=True,
            qe_autofix_cross_check=True,
            adaptive_feedback=adaptive,
        )

        def slow_fix(text, entered=entered, released=released):
            entered.set()
            assert released.wait(20), "canceller did not release fixer"
            return text.replace("BAD2", "BAD")

        def cancel(entered=entered, released=released, did=did, errors=errors):
            try:
                assert entered.wait(20), "fixer never started"
                with Session(db_engine) as s:
                    run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == did)).first()
                    orchestrator.cancel_run(s, run.id)
            except Exception as exc:
                errors.append(exc)
            finally:
                released.set()

        original_record = qe_autofix.record

        def capture(original_record=original_record, records=records, **kw):
            rec = original_record(**kw)
            records.append(rec)
            return rec

        AFProvider.reset(slow_fix)
        JUDGE_CALLS.clear()
        thread = threading.Thread(target=cancel)
        thread.start()
        with (
            patch.object(qe_jev, "_post", side_effect=fake_post),
            patch.object(qe_autofix, "record", side_effect=capture),
            Session(db_engine) as s,
        ):
            list(orchestrator.run_workflow_stream_segmented(s, wid, did, "fr-FR"))
        thread.join(25)
        assert not thread.is_alive() and not errors, errors
        assert JUDGE_CALLS == ["BAD2 one."], JUDGE_CALLS
        assert [c["task"] for c in AFProvider.calls].count("jev_refine") == 1
        assert not any(c["task"] == "qe" for c in AFProvider.calls)
        if adaptive:
            assert [c["task"] for c in AFProvider.calls].count("translate") == 1
        assert records and records[0]["stop_reason"] == "cancelled", records
        with Session(db_engine) as s:
            run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == did)).first()
            assert run.status == RunStatus.CANCELLED
            assert all(not g.target_text for g in segs_for(s, did))


def test_cancel_final_judge():
    cancelled = [False]
    base = {"score": 60, "skipped": False, "categories": {"accuracy": 2}, "unsure": []}

    def final_score(*args, **kwargs):
        cancelled[0] = True
        return {**base, "score": 100, "categories": {"accuracy": 0}}

    AFProvider.reset()
    with patch.object(qe_jev, "score_segment", side_effect=final_score):
        result = qe_autofix.run_loop(
            "Hello",
            "BAD",
            "en-US",
            "fr-FR",
            spec=ProviderSpec(provider="afprov", model=None),
            judge=None,
            baseline_card=base,
            threshold=70,
            max_rounds=3,
            cross_check=False,
            accept=None,
            tap_meta=None,
            cancelled=lambda: cancelled[0],
        )
    assert result["stop_reason"] == "cancelled", result
    assert result["totals"]["jev_calls"] == 1


def test_cap_completed_round(client, pid, judge):
    for adaptive in (False, True):
        did = new_doc(client, pid, ["BAD one."])
        wid = make_wf(
            "cap last round",
            judge,
            qe_autofix=True,
            qe_autofix_max_rounds=1,
            adaptive_feedback=adaptive,
        )
        tripped = [False]

        def fix(text, tripped=tripped):
            tripped[0] = True
            return text.replace("BAD", "GOOD")

        AFProvider.reset(fix)
        with patch.object(
            qe_autofix, "cap_blocked", side_effect=lambda tripped=tripped: tripped[0]
        ):
            run(wid, did)
        with Session(db_engine) as s:
            seg = segs_for(s, did)[0]
            assert seg.target_text == "GOOD one.", seg.target_text
            assert seg.status == SegStatus.MT, seg.status
            rec = next(f for f in seg.qa_flags if f["type"] == "qe_autofix")
            assert rec["outcome"] == "fixed", rec
            assert rec["stop_reason"] == "target_reached" and rec["rounds_used"] == 1, rec
            assert rec["cost_usd"] > 0, "the completed paid fixer round must retain its spend"


def test_cancelled_error():
    base = {"score": 60, "skipped": False, "categories": {"accuracy": 2}, "unsure": []}
    with patch.object(qe_jev_loop, "run_completion", side_effect=RuntimeError("cancelled")):
        result = qe_autofix.run_loop(
            "Hello",
            "BAD",
            "en-US",
            "fr-FR",
            spec=ProviderSpec(provider="afprov", model=None),
            judge=None,
            baseline_card=base,
            threshold=70,
            max_rounds=1,
            cross_check=False,
            accept=None,
            tap_meta=None,
            cancelled=lambda: False,
        )
    assert result["stop_reason"] == "fixer_failed", result


def test_cancel_guards(client, pid, judge):
    # Cancel on entry to _score_seg: the loop check has already passed. Conversely,
    # count entries after a judge cancels: a no-op scorer cannot hide a missing break.
    for adaptive in (False, True):
        for at_entry in (False, True):
            did = new_doc(client, pid, ["BAD one.", "BAD two."])
            wid = make_wf("independent cancel guards", judge, adaptive_feedback=adaptive)
            entries = []

            def cancel(did=did):
                with Session(db_engine) as s:
                    row = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == did)).first()
                    orchestrator.cancel_run(s, row.id)

            def trace(frame, event, arg, entries=entries, at_entry=at_entry, cancel=cancel):
                if event == "call" and frame.f_code.co_name == "_score_seg":
                    entries.append(frame.f_locals["j"])
                    if at_entry and len(entries) == 1:
                        cancel()
                return None

            def score(body, *args, cancel=cancel):
                result = fake_post(body, *args)
                cancel()
                return result

            AFProvider.reset()
            JUDGE_CALLS.clear()
            previous = sys.gettrace()
            try:
                sys.settrace(trace)
                with patch.object(qe_jev, "_post", side_effect=score), Session(db_engine) as s:
                    list(orchestrator.run_workflow_stream_segmented(s, wid, did, "fr-FR"))
            finally:
                sys.settrace(previous)
            assert entries == [0], (adaptive, at_entry, entries)
            assert len(JUDGE_CALLS) == (0 if at_entry else 1), JUDGE_CALLS
            if adaptive:
                assert [c["task"] for c in AFProvider.calls].count("translate") == 1


def test_cancel_winner(client, pid, judge):
    for adaptive in (False, True):
        did = new_doc(client, pid, ["BAD one."])
        wid = make_wf(
            "discard cancelled winner", judge, qe_autofix=True, adaptive_feedback=adaptive
        )
        original = qe_autofix.run_loop
        records = []
        original_record = qe_autofix.record

        def loop(*args, original=original, **kwargs):
            result = original(*args, **kwargs)
            winner, fixed = qe_autofix.pick_winner(result, 60, 70)
            assert winner is not None and fixed
            # Isolate winner discard from the later run-level write-back guard.
            result["stop_reason"] = "cancelled"
            return result

        def record(records=records, original_record=original_record, **kwargs):
            records.append(kwargs)
            return original_record(**kwargs)

        AFProvider.reset()
        with (
            patch.object(qe_autofix, "run_loop", side_effect=loop),
            patch.object(qe_autofix, "record", side_effect=record),
        ):
            run(wid, did)
        assert records[0]["kept"] == "original", records
        assert records[0]["outcome"] == "needs_review", records
        with Session(db_engine) as s:
            assert segs_for(s, did)[0].target_text == "BAD one."


def main():
    init_db()
    register_provider("afprov", AFProvider())
    client = TestClient(app)
    response = client.post(
        "/projects",
        data={
            "name": "FX5",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "afprov",
        },
        follow_redirects=False,
    )
    pid = int(response.headers["location"].rsplit("/", 1)[-1])
    judge = engines.create_profile("FX5 judge", "jev_local", base_url="http://127.0.0.1:8077")
    failures = []
    for name, test in (
        ("196", test_deterministic),
        ("M1 callers", lambda: test_no_terms_callers(client, pid)),
        ("212", test_old_verdicts),
        ("213", test_review_counts),
        ("212 rerun", lambda: test_rerun(client, pid, judge)),
        ("194", lambda: test_cancel(client, pid, judge)),
        ("194 final judge", test_cancel_final_judge),
        ("M2 completed round", lambda: test_cap_completed_round(client, pid, judge)),
        ("L5 provider error", test_cancelled_error),
        ("L2 independent guards", lambda: test_cancel_guards(client, pid, judge)),
        ("L2 cancelled winner", lambda: test_cancel_winner(client, pid, judge)),
    ):
        try:
            test()
            print(f"#{name} PASS")
        except Exception as exc:
            import traceback

            traceback.print_exc()
            failures.append(f"#{name}: {exc}")
    assert not failures, failures
    print("verify_fx5_run_flags OK")


if __name__ == "__main__":
    main()
