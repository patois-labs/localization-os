"""Remove-a-target-locale-from-a-project verification (offline, hermetic).

The inverse of content.add_target_locale (the add-locale flow). Covers the SERVICE
(content.remove_target_locale / remove_locale_preview / translated_target_locales)
+ all three surfaces (web, CLI, /api):

  (1) cascade: the removed (UNTRANSLATED) locale's Segment rows (+ editor state) and
      its LQAResult rows and its per-pair provider override are DELETED; the other
      locale's segments are untouched
  (2) PRESERVED: SegmentTM / FullTextTM / glossary rows for the pair survive (durable
      assets — re-adding the locale must restore its TM)
  (3) last-locale rule: removing the FINAL target locale fails closed
      (LocaleRemovalBlocked) and deletes NOTHING
  (4) re-add after remove works, and re-add restores 100% TM matches from the kept TM
  (5) PERMANENCE rule (2026-07-13): a locale with ANY translated content is
      never removable -- covers a segmented-mode Segment with target text, a
      FULL_TEXT-mode Segment with target text (the same field), and a WorkflowRun of
      its own that survives even after its segment text is later hand-cleared
  (6) chip affordance: the project page offers the remove x only for a still-
      removable (untranslated, non-last) locale
  (7) typed-confirmation: the web confirm page requires typing the locale; a wrong /
      missing confirm deletes nothing
  (8) permission gates: web + /api removal is a WRITE action (viewer denied,
      translator allowed)
  (9) CLI `project remove-locale` (--yes required; last-locale + unknown-locale +
      translated-content errors, ASCII message)
 (10) /api parity (POST /api/projects/{id}/remove-locale) incl. confirm_locale match
      + the translated-content block
 (11) audit row (project.remove_locale) written
 (12) run_view tolerates an orphaned run (a WorkflowRun whose TargetLocale row is
      gone) without a 500 -- a defensive property kept even though
      remove_target_locale itself can no longer produce this state going forward
      (a locale with a run of its own is now permanent, never reaches deletion)

HERMETIC: own LOC_TMS_DATA_DIR, all provider keys blanked before importing app. ASCII.
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_locrm_")

from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    AuditLog,
    Document,
    FullTextTM,
    GlossaryEntry,
    LQAResult,
    Project,
    ProjectPairProvider,
    Segment,
    SegmentTM,
    SegStatus,
    StepRun,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, content, security, settings, tm


def _fail(msg):
    raise AssertionError(msg)


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


_UN = 0


def _mk_user(role):
    global _UN
    _UN += 1
    return security.create_user(f"locrm{role}{_UN}@x.com", role.title(), role, "locrm-test-pw-123")


def _key(user):
    _row, raw = apikeys.create_key(user.id, "locrm-test", scopes="api docs")
    return raw


def _scenario(
    s, name="RM", src="en-US", first="fr-FR", second="es-ES", org_id=None, attach_run=False
):
    """A project with `first` locale + one document (2 segments), then `second`
    added via add_target_locale (seeds segments, UNTRANSLATED -- target_text stays
    "" on every segment). TM + glossary + a pair-provider override + an LQA result
    are attached to `second` WITHOUT going through an actual translation (TM/
    glossary/pair-provider rows never touch Segment.target_text; an LQA note can sit
    on an untranslated segment too), so `second` stays REMOVABLE under the
    permanence rule (2026-07-13: only an UNTRANSLATED locale can be removed) —
    removing it exercises both the delete and the preserve paths.

    `attach_run=True` additionally attaches a WorkflowRun (+ a Workflow/WorkflowStep/
    StepRun to hang it off) to the SECOND locale -- this makes `second` PERMANENT
    (condition (b) of translated_target_locales), so a scenario built this way is
    for the BLOCKED tests only; remove_target_locale on `second` must then raise.

    Returns (pid, doc_id, run_id_or_None)."""
    p = Project(name=name, source_locale=src, org_id=org_id)
    s.add(p)
    s.commit()
    s.refresh(p)
    doc = Document(
        project_id=p.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
    )
    s.add(doc)
    s.commit()
    s.refresh(doc)
    for i, txt in enumerate(["Hello world.", "Goodbye now."]):
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=first,
                order_index=i,
                source_text=txt,
                target_text="",
                status=SegStatus.NEW,
            )
        )
    s.add(TargetLocale(project_id=p.id, locale=first))
    s.commit()
    # add the second locale via the real add path (seeds `second` segments, empty
    # target_text -- add_target_locale never translates, only clones source text)
    content.add_target_locale(s, p.id, second)
    # TM (segment + full-text) + glossary for the SECOND pair -> must be preserved.
    # tm.write_back / the direct FullTextTM/GlossaryEntry inserts below are the
    # DURABLE-asset write path -- none of them touch Segment.target_text, so seeding
    # them does NOT make `second` translated under the permanence rule.
    tm.write_back(s, p.id, "Hello world.", "Hola mundo.", src, second)
    s.add(
        FullTextTM(
            project_id=p.id,
            source_locale=src,
            target_locale=second,
            source_hash="ftrmhash",
            source_text="Hello world.",
            target_text="Hola mundo.",
        )
    )
    s.add(
        GlossaryEntry(
            project_id=p.id,
            source_locale=src,
            target_locale=second,
            source_term="world",
            target_term="mundo",
        )
    )
    # a per-pair provider override for the SECOND target -> must be deleted
    s.add(
        ProjectPairProvider(
            project_id=p.id, source_locale=src, target_locale=second, provider="claude"
        )
    )
    s.commit()
    # an LQA result on one of the SECOND locale's (still-untranslated) segments ->
    # must be deleted. A reviewer note can exist on a segment before it's translated.
    seg2 = s.exec(
        select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == second)
    ).first()
    s.add(
        LQAResult(
            document_id=doc.id, segment_id=seg2.id, dimension="Accuracy", severity="Minor", note="x"
        )
    )
    s.commit()
    run_id = None
    if attach_run:
        # a workflow + a run for the SECOND locale -> makes `second` PERMANENT.
        wf = Workflow(name="wf", project_id=None)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        step = WorkflowStep(workflow_id=wf.id, order_index=0)
        s.add(step)
        s.commit()
        s.refresh(step)
        run = WorkflowRun(
            workflow_id=wf.id, document_id=doc.id, target_locale=second, status="completed"
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(StepRun(workflow_run_id=run.id, step_id=step.id, status="completed"))
        s.commit()
        run_id = run.id
    return p.id, doc.id, run_id


# ---------------------------------------------------------------------------
def test_cascade_deletes_and_preserves():
    """Removal of an UNTRANSLATED locale still cascades exactly as before: segments
    + LQA + pair-provider override deleted; TM + glossary preserved. (The OLD
    "workflow-run HISTORY survives a removal" case for THIS SAME locale is now
    impossible under the permanence rule -- a locale with a run of its own can never
    reach this deletion path at all; see test_workflow_run_blocks_removal_*.)"""
    with Session(engine) as s:
        pid, doc_id, run_id = _scenario(s, name="Cascade", attach_run=False)
        assert run_id is None
        preview = content.remove_locale_preview(s, pid, "es-ES")
        assert preview["deletes"]["segments"] == 2, preview
        assert preview["deletes"]["pair_provider_overrides"] == 1, preview
        assert preview["deletes"]["lqa_results"] == 1, preview
        assert preview["preserves"]["tm_segments"] == 1, preview
        assert preview["preserves"]["glossary_entries"] == 1, preview
        assert preview["is_translated"] is False, preview
        assert preview["blocked"] is False, preview

        deleted = content.remove_target_locale(s, pid, "es-ES", actor="tester")
        assert deleted["segments"] == 2, deleted

        # DELETED: the es-ES TargetLocale row, its segments, LQA, pair provider
        assert not content._project_locale_rows(s, pid, "es-ES"), "locale row gone"
        es_segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "es-ES")
        ).all()
        assert es_segs == [], "es-ES segments deleted"
        assert s.exec(select(LQAResult).where(LQAResult.document_id == doc_id)).all() == [], (
            "es-ES LQA deleted"
        )
        assert (
            s.exec(
                select(ProjectPairProvider).where(
                    ProjectPairProvider.project_id == pid,
                    ProjectPairProvider.target_locale == "es-ES",
                )
            ).all()
            == []
        ), "override gone"

        # UNTOUCHED: fr-FR segments remain
        fr_segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "fr-FR")
        ).all()
        assert len(fr_segs) == 2, "the other locale's segments are untouched"

        # PRESERVED: TM (segment + full-text) + glossary durable assets
        assert s.exec(
            select(SegmentTM).where(SegmentTM.project_id == pid, SegmentTM.target_locale == "es-ES")
        ).all(), "segment TM for the pair is PRESERVED"
        assert s.exec(
            select(FullTextTM).where(
                FullTextTM.project_id == pid, FullTextTM.target_locale == "es-ES"
            )
        ).all(), "full-text TM is PRESERVED"
        assert s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.project_id == pid, GlossaryEntry.target_locale == "es-ES"
            )
        ).all(), "glossary PRESERVED"
    print(
        "  cascade: segments/LQA/override deleted; TM/glossary preserved; "
        "other locale untouched: OK"
    )


def test_last_locale_blocked():
    with Session(engine) as s:
        p = Project(name="OneLoc", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        preview = content.remove_locale_preview(s, p.id, "fr-FR")
        assert preview["blocked"] is True, preview
        try:
            content.remove_target_locale(s, p.id, "fr-FR")
            _fail("removing the last locale must raise LocaleRemovalBlocked")
        except content.LocaleRemovalBlocked:
            pass
        assert content._project_locale_rows(s, p.id, "fr-FR"), "nothing deleted"
    print("  last-locale rule: removing the only locale fails closed, no delete: OK")


def test_unknown_locale_is_none():
    with Session(engine) as s:
        pid, _doc, _run = _scenario(s, name="Unknown")
        assert content.remove_locale_preview(s, pid, "de-DE") is None
        assert content.remove_target_locale(s, pid, "de-DE") is None
    print("  unknown locale: preview + remove both None (route 404/redirect): OK")


def test_re_add_after_remove_restores_tm():
    with Session(engine) as s:
        pid, doc_id, _run = _scenario(s, name="ReAdd")
        content.remove_target_locale(s, pid, "es-ES")
        assert not content._project_locale_rows(s, pid, "es-ES")
        # re-add: seeds fresh es-ES segments again
        assert content.add_target_locale(s, pid, "es-ES") is True
        seg = s.exec(
            select(Segment).where(
                Segment.document_id == doc_id,
                Segment.target_locale == "es-ES",
                Segment.source_text == "Hello world.",
            )
        ).first()
        assert seg is not None, "re-add re-seeds the locale's segments"
        # the PRESERVED TM still matches -> apply restores the target
        proj = s.get(Project, pid)
        applied = content.apply_tm_to_document(s, s.get(Document, doc_id), proj, "es-ES")
        assert applied >= 1, "kept TM produces a 100% match after re-add"
        s.refresh(seg)
        assert seg.target_text == "Hola mundo.", seg.target_text
    print("  re-add after remove: re-seeds segments + kept TM restores matches: OK")


def test_audit_row_written():
    with Session(engine) as s:
        pid, _doc, _run = _scenario(s, name="Audit")
        content.remove_target_locale(s, pid, "es-ES", actor="auditor@x.com")
        row = s.exec(
            select(AuditLog)
            .where(AuditLog.action == "project.remove_locale")
            .order_by(AuditLog.id.desc())
        ).first()
        assert row is not None, "an audit row is written"
        assert "es-ES" in (row.target or ""), row.target
        assert "preserved" in (row.detail or ""), row.detail
    print("  audit: project.remove_locale row written with preserve note: OK")


# --- PERMANENCE rule (2026-07-13): translated content blocks removal -------
def test_translated_segment_blocks_removal():
    """SEGMENTED mode: a locale with even ONE segment holding target text is
    permanent -- fails closed, deletes nothing."""
    with Session(engine) as s:
        pid, doc_id, _run = _scenario(s, name="SegBlock", attach_run=False)
        seg = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "es-ES")
        ).first()
        seg.target_text = "Hola."
        seg.status = SegStatus.MT
        s.add(seg)
        s.commit()

        preview = content.remove_locale_preview(s, pid, "es-ES")
        assert preview["is_translated"] is True, preview
        assert preview["blocked"] is True, preview
        assert "translated content" in preview["blocked_reason"], preview
        try:
            content.remove_target_locale(s, pid, "es-ES")
            _fail("a locale with a translated segment must not be removable")
        except content.LocaleRemovalBlocked as exc:
            assert "translated content" in str(exc), str(exc)

        # nothing deleted: locale row + all its segments (translated AND not) remain
        assert content._project_locale_rows(s, pid, "es-ES"), "locale row kept"
        es_segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "es-ES")
        ).all()
        assert len(es_segs) == 2, "segments untouched"
    print(
        "  permanence: a segmented-mode segment with target text blocks removal, "
        "nothing deleted: OK"
    )


def test_full_text_mode_blocks_removal():
    """FULL_TEXT mode: translate.py writes the whole-document result into the SAME
    Segment.target_text field as segmented mode (see _translate_full_text /
    _apply_result_ft) -- one predicate, one query shape, covers both modes."""
    with Session(engine) as s:
        p = Project(name="FTBlock", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        doc = Document(
            project_id=p.id, filename="d.txt", fmt="txt", mode=TransMode.FULL_TEXT, status="new"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale="fr-FR",
                order_index=0,
                source_text="Whole document text.",
                target_text="",
            )
        )
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        # seeds a single es-ES segment cloned from the fr-FR one (add_target_locale's
        # living-projects seeding), still untranslated at this point
        content.add_target_locale(s, p.id, "es-ES")
        seg = s.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == "es-ES")
        ).first()
        assert seg is not None and seg.target_text == "", "seeded untranslated"

        preview = content.remove_locale_preview(s, p.id, "es-ES")
        assert preview["blocked"] is False, preview  # not translated YET

        # now translate it (mirrors translate.py's full-text write path)
        seg.target_text = "Todo el documento traducido."
        seg.status = SegStatus.MT
        s.add(seg)
        s.commit()

        preview = content.remove_locale_preview(s, p.id, "es-ES")
        assert preview["is_translated"] is True, preview
        assert preview["blocked"] is True, preview
        try:
            content.remove_target_locale(s, p.id, "es-ES")
            _fail("a translated FULL_TEXT locale must not be removable")
        except content.LocaleRemovalBlocked:
            pass
        assert content._project_locale_rows(s, p.id, "es-ES"), "nothing deleted"
    print(
        "  permanence: FULL_TEXT-mode translated content blocks removal too "
        "(same Segment.target_text field as segmented mode): OK"
    )


def test_workflow_run_blocks_removal_even_after_target_cleared():
    """A locale's OWN WorkflowRun blocks removal even if every segment's target
    text is later hand-cleared -- a run's mere existence is a translation attempt
    whose output shape isn't reliably visible from segment state alone."""
    with Session(engine) as s:
        pid, doc_id, run_id = _scenario(s, name="RunBlock", attach_run=True)
        assert run_id is not None
        for seg in s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "es-ES")
        ).all():
            seg.target_text = ""
            seg.status = SegStatus.NEW
            s.add(seg)
        s.commit()

        preview = content.remove_locale_preview(s, pid, "es-ES")
        assert preview["is_translated"] is True, preview
        assert preview["blocked"] is True, preview
        try:
            content.remove_target_locale(s, pid, "es-ES")
            _fail(
                "a locale with a run of its own must stay unremovable even after "
                "its segment text was cleared"
            )
        except content.LocaleRemovalBlocked:
            pass
        assert content._project_locale_rows(s, pid, "es-ES"), "nothing deleted"
        assert s.get(WorkflowRun, run_id) is not None, "run itself untouched"
    print(
        "  permanence: a locale's own WorkflowRun blocks removal even after its "
        "segment text is cleared: OK"
    )


def test_chip_affordance_reflects_removability():
    """project.html only renders the chip x for a locale that is still removable:
    untranslated AND not the project's last locale. A translated locale shows no x."""
    from fastapi.testclient import TestClient

    from app.main import app

    with Session(engine) as s:
        p = Project(name="ChipTest", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        doc = Document(
            project_id=p.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale="fr-FR",
                order_index=0,
                source_text="Hi.",
                target_text="",
            )
        )
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        content.add_target_locale(s, p.id, "es-ES")
        content.add_target_locale(s, p.id, "de-DE")
        seg_es = s.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == "es-ES")
        ).first()
        seg_es.target_text = "Hola."
        seg_es.status = SegStatus.MT
        s.add(seg_es)
        s.commit()
        pid = p.id
    web = TestClient(app)
    r = web.get(f"/projects/{pid}")
    assert r.status_code == 200, r.status_code
    html = r.text
    assert 'remove-locale/fr-FR"' in html, "untranslated fr-FR: chip x present"
    assert 'remove-locale/de-DE"' in html, "untranslated de-DE: chip x present"
    assert 'remove-locale/es-ES"' not in html, "translated es-ES: chip x ABSENT"
    print(
        "  chip affordance: remove x shown for untranslated locales only, "
        "absent for the translated one: OK"
    )


def test_web_typed_confirmation_and_gates():
    from fastapi.testclient import TestClient

    from app.main import app
    from tests._csrf import csrf_for

    # project_scoping off so can_see_all is True for every role: this isolates the
    # WRITE gate (the thing under test) from the orthogonal membership-visibility gate.
    settings.set_many(
        {"security_auth_required": "1", "multi_org_enabled": "0", "project_scoping": "0"}
    )
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            pid, _doc_id, _run = _scenario(s, name="WebConfirm")

        translator = _mk_user("translator")
        viewer = _mk_user("viewer")

        # viewer (no write) is DENIED on the confirm page and the POST
        vc = TestClient(app)
        csrf_for(vc)
        r = vc.post(
            "/auth/login",
            data={"email": viewer.email, "password": "locrm-test-pw-123", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        csrf_for(vc)
        r = vc.get(f"/projects/{pid}/remove-locale/es-ES")
        assert r.status_code == 403, ("viewer must not see the confirm page", r.status_code)
        r = vc.post(
            f"/projects/{pid}/remove-locale",
            data={"locale": "es-ES", "confirm_locale": "es-ES"},
            follow_redirects=False,
        )
        assert r.status_code == 403, ("viewer must not remove a locale", r.status_code)
        with Session(engine) as s:
            assert content._project_locale_rows(s, pid, "es-ES"), "viewer POST did nothing"

        # translator (write) can see the page; a WRONG typed confirm deletes nothing
        tc = TestClient(app)
        csrf_for(tc)
        r = tc.post(
            "/auth/login",
            data={"email": translator.email, "password": "locrm-test-pw-123", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        csrf_for(tc)
        r = tc.get(f"/projects/{pid}/remove-locale/es-ES")
        assert r.status_code == 200 and "es-ES" in r.text, "confirm page renders"
        r = tc.post(
            f"/projects/{pid}/remove-locale",
            data={"locale": "es-ES", "confirm_locale": "WRONG"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        assert "/remove-locale/" in r.headers["location"], "wrong confirm bounces back"
        with Session(engine) as s:
            assert content._project_locale_rows(s, pid, "es-ES"), (
                "a wrong typed confirm must delete nothing"
            )
        # correct typed confirm removes it -> redirect carries ?locale_removed=
        r = tc.post(
            f"/projects/{pid}/remove-locale",
            data={"locale": "es-ES", "confirm_locale": "es-ES"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        assert "locale_removed=es-ES" in r.headers["location"], r.headers["location"]
        with Session(engine) as s:
            assert not content._project_locale_rows(s, pid, "es-ES"), "removed"
            assert s.exec(
                select(SegmentTM).where(
                    SegmentTM.project_id == pid, SegmentTM.target_locale == "es-ES"
                )
            ).all(), "TM still preserved"
    finally:
        settings.set_many(
            {"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"}
        )
        settings.invalidate_cache()
    print(
        "  web: confirm page renders, wrong/no confirm no-ops, viewer denied, "
        "correct confirm removes + preserves TM: OK"
    )


def test_api_parity_and_confirm_match():
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            pid, _doc, _run = _scenario(s, name="ApiParity")
        translator = _mk_user("translator")
        viewer = _mk_user("viewer")
        api = TestClient(app)

        # viewer denied (write gate)
        r = api.post(
            f"/api/projects/{pid}/remove-locale",
            json={"locale": "es-ES", "confirm_locale": "es-ES"},
            headers={"X-Loc-Token": _key(viewer)},
        )
        assert r.status_code == 403, ("viewer denied on /api", r.text)

        th = {"X-Loc-Token": _key(translator)}
        # confirm_locale mismatch -> 400, nothing deleted
        r = api.post(
            f"/api/projects/{pid}/remove-locale",
            json={"locale": "es-ES", "confirm_locale": "nope"},
            headers=th,
        )
        assert r.status_code == 400, ("mismatched confirm rejected", r.text)
        with Session(engine) as s:
            assert content._project_locale_rows(s, pid, "es-ES"), "no delete on mismatch"
        # matched confirm -> 200, deletes the locale, preserves TM
        r = api.post(
            f"/api/projects/{pid}/remove-locale",
            json={"locale": "es-ES", "confirm_locale": "es-ES"},
            headers=th,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["deleted"]["segments"] == 2, body
        with Session(engine) as s:
            assert not content._project_locale_rows(s, pid, "es-ES")
            assert s.exec(
                select(SegmentTM).where(
                    SegmentTM.project_id == pid, SegmentTM.target_locale == "es-ES"
                )
            ).all(), "TM preserved via /api"
        # unknown locale -> 404
        r = api.post(
            f"/api/projects/{pid}/remove-locale",
            json={"locale": "de-DE", "confirm_locale": "de-DE"},
            headers=th,
        )
        assert r.status_code == 404, r.text
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  /api: write-gated, confirm_locale must match, deletes + preserves TM, unknown 404: OK")


def test_cli_remove_locale():
    with Session(engine) as s:
        pid, _doc, _run = _scenario(s, name="CliRemove")
    # without --yes a non-interactive run refuses (confirm returns False)
    rc, out = _run_cli(["--json", "project", "remove-locale", str(pid), "es-ES"])
    assert rc != 0, ("no --yes must refuse", out)
    with Session(engine) as s:
        assert content._project_locale_rows(s, pid, "es-ES"), "refused, nothing deleted"
    # with --yes it removes and reports the deleted counts
    rc, out = _run_cli(["--json", "project", "remove-locale", str(pid), "es-ES", "--yes"])
    assert rc == 0, out
    body = _json.loads(out)
    assert body["ok"] and body["deleted"]["segments"] == 2, body
    with Session(engine) as s:
        assert not content._project_locale_rows(s, pid, "es-ES"), "removed via CLI"
        assert s.exec(
            select(SegmentTM).where(SegmentTM.project_id == pid, SegmentTM.target_locale == "es-ES")
        ).all(), "TM preserved via CLI"
    # unknown locale -> usage error
    rc, out = _run_cli(["project", "remove-locale", str(pid), "de-DE", "--yes"])
    assert rc != 0, ("unknown locale errors", out)
    # last-locale (fr-FR remains alone) -> blocked
    rc, out = _run_cli(["project", "remove-locale", str(pid), "fr-FR", "--yes"])
    assert rc != 0, ("last-locale blocked on CLI", out)
    with Session(engine) as s:
        assert content._project_locale_rows(s, pid, "fr-FR"), "last locale kept"
    print(
        "  CLI: remove-locale requires --yes, removes + preserves TM, unknown + "
        "last-locale error: OK"
    )


def test_cli_blocked_by_translated_content():
    """CLI surface: a translated locale is blocked with the SAME clear message the
    service raises, ASCII-only (CLI-reachable strings are ASCII-only, repo law)."""
    with Session(engine) as s:
        pid, doc_id, _run = _scenario(s, name="CliBlock", attach_run=False)
        seg = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "es-ES")
        ).first()
        seg.target_text = "Hola."
        seg.status = SegStatus.MT
        s.add(seg)
        s.commit()
    rc, out = _run_cli(["--json", "project", "remove-locale", str(pid), "es-ES", "--yes"])
    assert rc != 0, ("translated locale must be blocked on the CLI", out)
    assert "translated content" in out, out
    assert out.isascii(), ("CLI-reachable error text must stay ASCII-only", out)
    with Session(engine) as s:
        assert content._project_locale_rows(s, pid, "es-ES"), "nothing deleted"
    print("  CLI: a translated locale is blocked with a clear ASCII message, nothing deleted: OK")


def test_api_blocked_by_translated_content():
    """/api surface: same permanence block, clean 400 + message (no independent
    pre-check to disagree with the service -- api_cli.py calls remove_target_locale
    directly and surfaces its LocaleRemovalBlocked)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with Session(engine) as s:
        pid, doc_id, _run = _scenario(s, name="ApiBlock", attach_run=False)
        seg = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "es-ES")
        ).first()
        seg.target_text = "Hola."
        seg.status = SegStatus.MT
        s.add(seg)
        s.commit()
    translator = _mk_user("translator")
    api = TestClient(app)
    r = api.post(
        f"/api/projects/{pid}/remove-locale",
        json={"locale": "es-ES", "confirm_locale": "es-ES"},
        headers={"X-Loc-Token": _key(translator)},
    )
    assert r.status_code == 400, ("translated locale must be blocked on /api", r.text)
    assert "translated content" in r.json().get("error", ""), r.text
    with Session(engine) as s:
        assert content._project_locale_rows(s, pid, "es-ES"), "nothing deleted"
    print("  /api: a translated locale is blocked with a clear message, nothing deleted: OK")


def test_run_view_tolerates_orphaned_run():
    """run_view must not 500 on a WorkflowRun whose TargetLocale row is gone. This
    used to arise from removing a TRANSLATED locale (the old behavior); the
    permanence rule means remove_target_locale can no longer produce that state
    going forward (a locale with a run of its own is now unremovable) -- so this
    test constructs the orphan directly, keeping run_view's defensive tolerance
    covered (historical rows / any other path that drops a TargetLocale row)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with Session(engine) as s:
        pid, _doc, run_id = _scenario(s, name="RunView", attach_run=True)
        assert run_id is not None
        for tl in content._project_locale_rows(s, pid, "es-ES"):
            s.delete(tl)
        s.commit()
    web = TestClient(app)
    r = web.get(f"/runs/{run_id}")
    assert r.status_code == 200, ("run view must not crash on an orphaned run", r.status_code)
    print("  run_view: GET /runs/{id} for a run whose locale is gone renders (no orphan crash): OK")


def main():
    init_db()
    test_cascade_deletes_and_preserves()
    test_last_locale_blocked()
    test_unknown_locale_is_none()
    test_re_add_after_remove_restores_tm()
    test_audit_row_written()
    test_translated_segment_blocks_removal()
    test_full_text_mode_blocks_removal()
    test_workflow_run_blocks_removal_even_after_target_cleared()
    test_chip_affordance_reflects_removability()
    test_web_typed_confirmation_and_gates()
    test_api_parity_and_confirm_match()
    test_cli_remove_locale()
    test_cli_blocked_by_translated_content()
    test_api_blocked_by_translated_content()
    test_run_view_tolerates_orphaned_run()
    print("LOCALE REMOVE OK")


if __name__ == "__main__":
    main()
