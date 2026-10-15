"""Translation Memory MANAGEMENT module verification (offline, hermetic).

Covers the /tm module (browse / usage / lifecycle over the EXISTING SegmentTM rows):
  (1) schema — use_count + last_used_at columns exist and default correctly
  (2) usage tracking wired at the REAL apply sites, with realistic job payloads:
        - translate.pretranslate_stream segmented 100% apply -> bump
        - orchestrator.run_workflow step-0 100% apply -> bump
        - content.apply_tm_to_document 100% apply -> bump
        - a fuzzy (<100%) match is NOT applied and NOT counted
        - one entry applying to MANY identical segments in a run counts each apply
  (3) browse + filters (pair / project / text / date / usage / unused) + pagination
  (4) stats summary (totals, per-pair, used-vs-never, top-used, oldest/newest)
  (5) lifecycle delete: single + bulk-by-filter dry-run vs real + EMBEDDING-ROW
      erase (the core has no semantic search, but the SegmentEmbedding table stays and
      a delete must still clear any vector row that points at the entry: FK-safe)
  (5b) permission gates (web + api): delete (single + bulk) is can_tune_ai (admin
      or linguist), not can_manage -- per the maintainer, a translator (below linguist's
      rank) is denied, a linguist is ALLOWED (previously would have been 403'd
      under the old can_manage/manager-only gate)
  (6) scoped visibility (org + customer) incl. the F2 CLI cross-org rejection pattern
  (7) FK-valid writes on a PRAGMA foreign_keys=ON engine (the glossary F1 lesson —
      SegmentTM.project_id is a real FK; a NULL-project global row is FK-legal)

HERMETICITY: clears ANTHROPIC/GEMINI/OPENAI keys AND LOCAL_BASE_URL before importing
app (LOCAL_BASE_URL would otherwise leak the maintainer's live LM Studio in). ASCII-only.
"""

import io
import json as _json
import os
import sys
import tempfile
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tmmod_")

from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    Segment,
    SegmentEmbedding,
    SegmentTM,
    SegStatus,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import (
    apikeys,
    content,
    orchestrator,
    security,
    settings,
    tm,
    tm_module,
    translate,
)


def _fail(msg):
    raise AssertionError(msg)


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _mk_project(s, name="TM Proj", src="en-US", org_id=None):
    p = Project(name=name, source_locale=src, org_id=org_id)
    s.add(p)
    s.commit()
    s.refresh(p)
    return p.id


def _mk_doc_with_segments(s, project_id, locale, sources):
    """A segmented Document + one NEW Segment per source string (the shape the
    translate path expects: empty target, status NEW)."""
    doc = Document(
        project_id=project_id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
    )
    s.add(doc)
    s.commit()
    s.refresh(doc)
    for i, src in enumerate(sources):
        s.add(
            Segment(
                document_id=doc.id,
                target_locale=locale,
                order_index=i,
                source_text=src,
                target_text="",
                status=SegStatus.NEW,
            )
        )
    s.add(TargetLocale(project_id=project_id, locale=locale))
    s.commit()
    return doc


# ---------------------------------------------------------------------------
def test_schema():
    cols = {c["name"] for c in inspect(engine).get_columns("segmenttm")}
    assert "use_count" in cols, cols
    assert "last_used_at" in cols, cols
    with Session(engine) as s:
        pid = _mk_project(s)
        e = tm.write_back(s, pid, "Hello.", "Bonjour.", "en-US", "fr-FR")
        s.refresh(e)
        assert e.use_count == 0, e.use_count
        assert e.last_used_at is None, "creation must not count as translation usage"
        assert e.activity_at is not None, "creation stamps retention activity"
    print("  schema: creation stamps activity_at and leaves use_count at 0: OK")


def test_usage_tracking_translate_segmented():
    """A 100% segment-TM hit applied by pretranslate_stream bumps use_count; a fuzzy
    (<100%) match applies nothing and counts nothing."""
    with Session(engine) as s:
        pid = _mk_project(s)
        e = tm.write_back(s, pid, "Adventure awaits.", "L'aventure vous attend.", "en-US", "fr-FR")
        eid = e.id
        # Two segments: one EXACT (100% -> applied), one only fuzzy-similar (not 100).
        doc = _mk_doc_with_segments(
            s, pid, "fr-FR", ["Adventure awaits.", "Adventure awaits now!!"]
        )
        list(translate.pretranslate_stream(s, doc.id, "fr-FR"))
        s.expire_all()
        applied = tm_module.get_entry(s, eid)
        assert applied.use_count == 1, ("exact 100% apply must bump once", applied.use_count)
        assert applied.last_used_at is not None, "last_used_at must be stamped"
        # No OTHER SegmentTM row was created for the fuzzy source -> nothing else bumped.
        total_used = tm_module.stats(s)["used"]
        assert total_used == 1, ("only the exact hit counts as used", total_used)
    print("  usage: translate segmented 100% apply bumps; fuzzy does not: OK")


def test_usage_tracking_many_applies_one_run():
    """One TM entry applied to MANY identical segments in a single run counts each
    application (the increment must not collapse to 1, and must not break on repeats)."""
    with Session(engine) as s:
        pid = _mk_project(s, name="ManyApply")
        e = tm.write_back(s, pid, "Repeat me.", "Repete-moi.", "en-US", "fr-FR")
        eid = e.id
        doc = _mk_doc_with_segments(s, pid, "fr-FR", ["Repeat me."] * 4)
        list(translate.pretranslate_stream(s, doc.id, "fr-FR"))
        s.expire_all()
        applied = tm_module.get_entry(s, eid)
        assert applied.use_count == 4, ("4 identical applies -> use_count 4", applied.use_count)
    print("  usage: one entry applied to 4 identical segments counts 4: OK")


def test_usage_tracking_apply_tm_to_document():
    """content.apply_tm_to_document (the manual apply-tm action) bumps applied hits."""
    with Session(engine) as s:
        pid = _mk_project(s, name="ApplyTM")
        project = s.get(Project, pid)
        e = tm.write_back(s, pid, "Click here.", "Cliquez ici.", "en-US", "fr-FR")
        eid = e.id
        doc = _mk_doc_with_segments(s, pid, "fr-FR", ["Click here.", "Click here."])
        n = content.apply_tm_to_document(s, doc, project, "fr-FR")
        assert n == 2, ("both empty segments filled from TM", n)
        s.expire_all()
        assert tm_module.get_entry(s, eid).use_count == 2, "apply-tm must bump per apply"
    print("  usage: apply_tm_to_document bumps per applied segment: OK")


def test_usage_tracking_workflow_step0():
    """orchestrator.run_workflow step-0 100% TM short-circuit bumps use_count."""
    with Session(engine) as s:
        pid = _mk_project(s, name="WF")
        e = tm.write_back(s, pid, "Good morning.", "Bonjour matin.", "en-US", "fr-FR")
        eid = e.id
        doc = _mk_doc_with_segments(s, pid, "fr-FR", ["Good morning.", "New text here."])
        wf = Workflow(name="W")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(WorkflowStep(workflow_id=wf.id, order_index=0, name="MT", step_type="llm_agent"))
        s.commit()
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        assert tm_module.get_entry(s, eid).use_count == 1, "step-0 100% apply must bump"
    print("  usage: workflow step-0 100% apply bumps: OK")


# A ~209-character source whose only difference from the query is ONE DIGIT of the amount.
# fuzz.ratio scores that 99.52 (one differing char out of 209), which `round` pushed up to
# 100 while the hashes differ (so the exact-hash path correctly misses).
_MONEY_SRC = (
    "Please transfer 100 dollars to the account numbered 4471 8823 0092 before the end of "
    "the business day so the vendor invoice for March can be settled on time and without "
    "any further delay or extra bank charges."
)
_MONEY_ALT = _MONEY_SRC.replace("100 dollars", "900 dollars")
_MONEY_TGT = "Veuillez virer 100 dollars au compte 4471 8823 0092."


def test_fuzzy_99_5_floors_to_99_never_100():
    """#491: a fuzzy score in 99.5..99.9 must report 99, never a rounded-up 100.

    The bug: `int(round(best_score))` turned a 99.52 fuzzy hit into match_pct 100, and every
    auto-apply gate keys on `match_pct == 100` (orchestrator step-0 TM fill, the tm_lock
    "100" band, the tm_match offer list). A segment differing by ONE DIGIT of an amount was
    therefore filled with the stored target and locked as a 100% match, so a wrong number
    shipped with no MT and no review, even at the default fill_threshold=100.
    """
    assert _MONEY_SRC != _MONEY_ALT and tm.hash_text(_MONEY_SRC, "en-US", "fr-FR") != tm.hash_text(
        _MONEY_ALT, "en-US", "fr-FR"
    ), "the two sources must differ, so the exact-hash path cannot answer the query"
    with Session(engine) as s:
        pid = _mk_project(s, name="Fuzz99")
        e = tm.write_back(s, pid, _MONEY_SRC, _MONEY_TGT, "en-US", "fr-FR")
        eid = e.id

        # A hash-exact hit still reports 100 (the fix only floors NON-hash scores).
        exact = tm.lookup(s, pid, _MONEY_SRC, "en-US", "fr-FR")
        assert exact and exact["match_pct"] == 100, exact

        # The one-digit variant is a FUZZY hit and must report 99 on both lookup paths.
        hit = tm.lookup(s, pid, _MONEY_ALT, "en-US", "fr-FR")
        assert hit and hit["match_pct"] == 99, ("a 99.52 fuzzy hit must floor to 99", hit)
        assert hit["target_text"] == _MONEY_TGT, hit
        batch = tm.lookup_batch(s, pid, [_MONEY_ALT, _MONEY_SRC], "en-US", "fr-FR")
        assert batch[_MONEY_ALT]["match_pct"] == 99, batch[_MONEY_ALT]
        assert batch[_MONEY_SRC]["match_pct"] == 100, batch[_MONEY_SRC]

        # The orchestrator step-0 gate (`match_pct == 100`) must NOT fire for the variant:
        # no TM fill, no usage bump; the MT step translates it instead.
        doc = _mk_doc_with_segments(s, pid, "fr-FR", [_MONEY_ALT])
        wf = Workflow(name="Fuzz99WF")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(WorkflowStep(workflow_id=wf.id, order_index=0, name="MT", step_type="llm_agent"))
        s.commit()
        orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        s.expire_all()
        seg = s.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == "fr-FR")
        ).first()
        assert seg.target_text != _MONEY_TGT, (
            "#491: a 99.5 fuzzy hit must never be auto-applied as a 100% match",
            seg.target_text,
        )
        assert not seg.tm_match_pct or seg.tm_match_pct < 100, (
            "the segment must not carry 100% TM provenance",
            seg.tm_match_pct,
        )
        assert tm_module.get_entry(s, eid).use_count == 0, (
            "an un-applied fuzzy hit must not bump use_count",
            tm_module.get_entry(s, eid).use_count,
        )
    print("  fuzzy: a 99.5 one-digit-difference hit reports 99 and is never auto-applied: OK")


def test_browse_filters_and_pagination():
    with Session(engine) as s:
        pid = _mk_project(s, name="Browse")
        # 60 entries across two pairs so pagination + pair filter both bite.
        for i in range(40):
            tm.write_back(s, pid, f"src-en-{i}", f"tgt-fr-{i}", "en-US", "fr-FR")
        for i in range(20):
            tm.write_back(s, pid, f"src-en-{i}", f"tgt-es-{i}", "en-US", "es-ES")
        # pagination: 60 entries scoped to THIS project, per_page 25 -> 3 pages.
        r1 = tm_module.list_entries(s, page=1, per_page=25, project_id=pid)
        assert r1["total"] == 60, r1["total"]
        assert len(r1["entries"]) == 25, len(r1["entries"])
        assert r1["pages"] == 3, r1["pages"]
        r3 = tm_module.list_entries(s, page=3, per_page=25, project_id=pid)
        assert len(r3["entries"]) == 10, len(r3["entries"])
        # pair filter
        fr = tm_module.count_entries(s, project_id=pid, target_locale="fr-FR")
        assert fr == 40, fr
        es = tm_module.count_entries(s, project_id=pid, target_locale="es-ES")
        assert es == 20, es
        # text search on source OR target
        hit = tm_module.count_entries(s, project_id=pid, q="tgt-es-5")
        assert hit == 1, hit
        # min/max use + unused (nothing used yet in THIS project)
        assert tm_module.count_entries(s, project_id=pid, unused=True) == 60
        assert tm_module.count_entries(s, project_id=pid, min_use=1) == 0
    print("  browse: pair/project/text filters + pagination: OK")


def test_stats_summary():
    with Session(engine) as s:
        pid = _mk_project(s, name="Stats")
        a = tm.write_back(s, pid, "one", "un", "en-US", "fr-FR")
        tm.write_back(s, pid, "two", "deux", "en-US", "fr-FR")
        tm.write_back(s, pid, "three", "tres", "en-US", "es-ES")
        # apply `a` twice
        tm_module.bump_usage(s, [a.id, a.id])
        st = tm_module.stats(s, visible_pids=[pid])
        assert st["total"] == 3, st
        assert st["used"] == 1 and st["never_used"] == 2, st
        assert st["total_applications"] == 2, st
        pairs = {(p["source_locale"], p["target_locale"]): p["count"] for p in st["pairs"]}
        assert pairs[("en-US", "fr-FR")] == 2, pairs
        assert pairs[("en-US", "es-ES")] == 1, pairs
        assert st["top_used"] and st["top_used"][0]["id"] == a.id, st["top_used"]
        assert st["oldest_created_at"] and st["newest_created_at"], st
    print("  stats: totals/per-pair/used-split/top-used/date-range: OK")


def _plant_embedding(s, entry):
    """The core never writes embeddings (semantic TM is a paid add-on), but the table stays and
    rows can exist (an add-on, an older install): plant one the way the add-on would."""
    s.add(
        SegmentEmbedding(
            segment_tm_id=entry.id,
            project_id=entry.project_id,
            source_locale=entry.source_locale,
            target_locale=entry.target_locale,
            model="planted",
            dim=2,
            embedding=bytes.fromhex("0000803f00000040"),
        )
    )
    s.commit()


def test_delete_single_and_embedding_invalidation():
    """Deleting an entry removes it AND any embedding row that points at it (FK-safe)."""
    with Session(engine) as s:
        pid = _mk_project(s, name="DelEmb")
        e = tm.write_back(s, pid, "Ephemeral entry.", "Entree ephemere.", "en-US", "fr-FR")
        eid = e.id
        _plant_embedding(s, e)
        emb = s.exec(select(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id == eid)).all()
        assert emb, "the planted embedding row should exist for the entry"
        # delete via the module
        assert tm_module.delete_entry(s, eid) is True
        assert s.get(SegmentTM, eid) is None, "entry row must be gone"
        leftover = s.exec(
            select(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id == eid)
        ).all()
        assert leftover == [], ("embedding row must be erased on delete", leftover)
    print("  delete: single delete erases the entry's embedding row: OK")


def test_bulk_delete_dry_run_vs_real():
    with Session(engine) as s:
        pid = _mk_project(s, name="Bulk")
        for i in range(10):
            tm.write_back(s, pid, f"bulk-src-{i}", f"bulk-tgt-{i}", "en-US", "fr-FR")
        # mark 3 as used so "unused" filter selects the other 7
        used = tm_module.list_entries(s, project_id=pid, per_page=3)["entries"]
        tm_module.bump_usage(s, [used[0]["id"], used[1]["id"], used[2]["id"]])
        # dry-run: count only, delete nothing
        n = tm_module.bulk_delete(s, dry_run=True, project_id=pid, unused=True)
        assert n == 7, ("dry-run counts the 7 unused", n)
        assert tm_module.count_entries(s, project_id=pid) == 10, "dry-run deleted nothing"
        # real delete of the unused 7
        d = tm_module.bulk_delete(s, dry_run=False, project_id=pid, unused=True)
        assert d == 7, d
        assert tm_module.count_entries(s, project_id=pid) == 3, "3 used entries remain"
        assert tm_module.count_entries(s, project_id=pid, unused=True) == 0
    print("  bulk-delete: dry-run count vs real delete (unused filter): OK")


def test_bulk_delete_created_before_and_embeddings():
    """created_before filter + bulk delete also erases embedding rows for the matched set."""
    with Session(engine) as s:
        pid = _mk_project(s, name="BulkEmb")
        olds = [tm.write_back(s, pid, f"old-{i}", f"vieux-{i}", "en-US", "fr-FR") for i in range(3)]
        old_ids = [e.id for e in olds]
        # backdate the 3 rows so created_before selects them
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
        for e in olds:
            e.created_at = past
            s.add(e)
        s.commit()
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        for e in olds:
            _plant_embedding(s, e)
        # embeddings exist for the old rows
        assert s.exec(
            select(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id.in_(old_ids))
        ).all()
        d = tm_module.bulk_delete(s, created_before=cutoff, project_id=pid)
        assert d == 3, d
        assert (
            s.exec(
                select(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id.in_(old_ids))
            ).all()
            == []
        ), "bulk delete must invalidate embeddings for the matched set"
    print("  bulk-delete: created_before + embedding invalidation: OK")


_UN = 0


def _mk_user(role):
    """A throwaway user with the given role (unique email per call)."""
    global _UN
    _UN += 1
    return security.create_user(
        f"tmuser{role}{_UN}@x.com", role.title(), role, "tm-module-test-pw-123"
    )


def _key(user):
    _row, raw = apikeys.create_key(user.id, "tm-test", scopes="api docs")
    return raw


def test_permission_gates_delete_is_can_tune_ai():
    """TM delete (single + bulk) is gated on can_tune_ai (admin or linguist), NOT
    can_manage -- per the maintainer, TM lifecycle management (like glossaries) is the
    linguist's home turf and should not require manager sign-off.

    translator (rank 1, below linguist's rank 2) must be DENIED (403) on both web
    and /api. linguist (rank 2, same rank as reviewer) must be ALLOWED on both --
    this is the behavior change: previously linguist was 403'd under the old
    can_manage (>= manager, rank 3) gate."""
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            pid = _mk_project(s, name="PermGate")
            e1 = tm.write_back(s, pid, "perm one", "un perm", "en-US", "fr-FR")
            e2 = tm.write_back(s, pid, "perm two", "deux perm", "en-US", "fr-FR")
            e1id, e2id = e1.id, e2.id

        translator = _mk_user("translator")
        linguist = _mk_user("linguist")
        th = {"X-Loc-Token": _key(translator)}
        lh = {"X-Loc-Token": _key(linguist)}
        api = TestClient(app)

        # --- /api/tm/{id}/delete --------------------------------------------
        r = api.post(f"/api/tm/{e1id}/delete", headers=th)
        assert r.status_code == 403, ("translator must not delete TM via /api", r.text)
        with Session(engine) as s:
            assert s.get(SegmentTM, e1id) is not None, "denied delete must be a no-op"

        r = api.post(f"/api/tm/{e1id}/delete", headers=lh)
        assert r.status_code == 200, ("linguist CAN delete a TM entry via /api", r.text)
        with Session(engine) as s:
            assert s.get(SegmentTM, e1id) is None, "linguist's delete must take effect"

        # --- /api/tm/bulk-delete ---------------------------------------------
        r = api.post("/api/tm/bulk-delete", json={"project_id": pid}, headers=th)
        assert r.status_code == 403, ("translator must not bulk-delete TM via /api", r.text)
        with Session(engine) as s:
            assert s.get(SegmentTM, e2id) is not None, "denied bulk-delete must be a no-op"

        r = api.post("/api/tm/bulk-delete", json={"project_id": pid}, headers=lh)
        assert r.status_code == 200, ("linguist CAN bulk-delete TM via /api", r.text)
        with Session(engine) as s:
            assert s.get(SegmentTM, e2id) is None, "linguist's bulk-delete must take effect"

        # --- web /tm/{id}/delete (session-cookie login, same gate) ----------
        from tests._csrf import csrf_for

        with Session(engine) as s:
            e3 = tm.write_back(s, pid, "perm three", "trois perm", "en-US", "fr-FR")
            e3id = e3.id
        web_t = TestClient(app)
        csrf_for(web_t)  # login POST itself needs a CSRF token
        r = web_t.post(
            "/auth/login",
            data={"email": translator.email, "password": "tm-module-test-pw-123", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, ("translator login must succeed", r.status_code)
        csrf_for(web_t)  # login rotates the session -> refresh the token
        r = web_t.post(f"/tm/{e3id}/delete", follow_redirects=False)
        assert r.status_code == 403, ("translator must not delete TM via web", r.status_code)
        with Session(engine) as s:
            assert s.get(SegmentTM, e3id) is not None, "denied web delete must be a no-op"

        web_l = TestClient(app)
        csrf_for(web_l)
        r = web_l.post(
            "/auth/login",
            data={"email": linguist.email, "password": "tm-module-test-pw-123", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, ("linguist login must succeed", r.status_code)
        csrf_for(web_l)
        r = web_l.post(f"/tm/{e3id}/delete", follow_redirects=False)
        assert r.status_code == 303, ("linguist CAN delete TM entry via web", r.status_code)
        with Session(engine) as s:
            assert s.get(SegmentTM, e3id) is None, "linguist's web delete must take effect"
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  permission gates: delete (single + bulk) is can_tune_ai on web + /api; "
        "translator denied, linguist ALLOWED: OK"
    )


def test_scoped_visibility():
    """visible_pids confines list/stats/get to the given project set; a NULL-project
    global row is visible only to an unrestricted (None) caller."""
    with Session(engine) as s:
        pa = _mk_project(s, name="ScopeA")
        pb = _mk_project(s, name="ScopeB")
        tm.write_back(s, pa, "a-src", "a-tgt", "en-US", "fr-FR")
        tm.write_back(s, pb, "b-src", "b-tgt", "en-US", "fr-FR")
        # a scoped caller for project A sees only A's entries
        ra = tm_module.list_entries(s, visible_pids=[pa])
        assert ra["total"] == 1, ra["total"]
        assert ra["entries"][0]["project_id"] == pa, ra["entries"][0]
        # get_entry across scope -> None (the B entry is invisible to an A-scoped caller)
        b_entry = tm_module.list_entries(s, visible_pids=[pb])["entries"][0]
        assert tm_module.get_entry(s, b_entry["id"], visible_pids=[pa]) is None
        assert tm_module.get_entry(s, b_entry["id"], visible_pids=[pb]) is not None
        # a scoped caller cannot delete an out-of-scope entry
        assert tm_module.delete_entry(s, b_entry["id"], visible_pids=[pa]) is False
        assert s.get(SegmentTM, b_entry["id"]) is not None, "out-of-scope delete is a no-op"
    print("  scoping: visible_pids confines list/get/delete: OK")


def test_cli_round_trip():
    with Session(engine) as s:
        pid = _mk_project(s, name="CLITM")
        e = tm.write_back(s, pid, "cli source", "cli cible", "en-US", "fr-FR")
        eid = e.id
    # list (json)
    rc, out = _run_cli(["--json", "tm", "list", "--project", str(pid)])
    assert rc == 0, out
    body = _json.loads(out)
    assert body["total"] >= 1, body
    # show
    rc, out = _run_cli(["--json", "tm", "show", str(eid)])
    assert rc == 0, out
    assert _json.loads(out)["id"] == eid
    # search
    rc, out = _run_cli(["--json", "tm", "search", "cli source"])
    assert rc == 0, out
    assert _json.loads(out)["total"] >= 1
    # stats
    rc, out = _run_cli(["--json", "tm", "stats"])
    assert rc == 0, out
    assert _json.loads(out)["total"] >= 1
    # delete dry-run (single)
    rc, out = _run_cli(["--json", "tm", "delete", str(eid), "--dry-run"])
    assert rc == 0, out
    assert _json.loads(out)["dry_run"] is True
    with Session(engine) as s:
        assert s.get(SegmentTM, eid) is not None, "dry-run must not delete"
    # delete real (single, --yes to skip prompt)
    rc, out = _run_cli(["--json", "tm", "delete", str(eid), "--yes"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(SegmentTM, eid) is None, "real delete removed the entry"
    print("  CLI: list/search/show/stats/delete (dry-run + real) round-trip: OK")


def test_cli_bulk_delete_dry_run():
    with Session(engine) as s:
        pid = _mk_project(s, name="CLIBulk")
        for i in range(5):
            tm.write_back(s, pid, f"bd-{i}", f"bd-t-{i}", "en-US", "de-DE")
    rc, out = _run_cli(
        ["--json", "tm", "delete", "--project", str(pid), "--target", "de-DE", "--dry-run"]
    )
    assert rc == 0, out
    assert _json.loads(out)["count"] == 5, out
    with Session(engine) as s:
        assert tm_module.count_entries(s, project_id=pid) == 5, "dry-run kept all"
    rc, out = _run_cli(
        ["--json", "tm", "delete", "--project", str(pid), "--target", "de-DE", "--yes"]
    )
    assert rc == 0, out
    assert _json.loads(out)["count"] == 5, out
    with Session(engine) as s:
        assert tm_module.count_entries(s, project_id=pid) == 0, "real bulk delete cleared"
    print("  CLI: bulk delete dry-run vs real (by filter): OK")


def test_filterless_bulk_delete_requires_confirm_all():
    """MEDIUM finding fix: POST /api/tm/bulk-delete with an empty body {} (no filter
    fields at all) used to perform an UNCONFIRMED mass delete of the caller's entire
    visible TM. It must now be refused with a 400 unless the body carries
    confirm_all: true. Filtered deletes and dry-runs (filtered or not) are
    unaffected. Also covers the web route (POST /tm/bulk-delete): a forged POST
    with confirm=1 and no filters must NOT be able to filterless-delete without the
    same confirm_all opt-in (the real UI form always goes through the count-first
    preview, which now stamps confirm_all itself for the filterless case)."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import User
    from app.services import apikeys

    with Session(engine) as s:
        pid = _mk_project(s, name="FilterlessGuard")
        pid_other = _mk_project(s, name="FilterlessGuardFiltered")
        for i in range(4):
            tm.write_back(s, pid, f"fl-src-{i}", f"fl-tgt-{i}", "en-US", "fr-FR")
        for i in range(3):
            tm.write_back(s, pid_other, f"flf-src-{i}", f"flf-tgt-{i}", "en-US", "de-DE")
        u = User(email="tm-guard@example.com", role="admin", active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "tm-guard", scopes="api docs")
    hdr = {"X-Loc-Token": raw}
    api = TestClient(app)

    # --- JSON API: /api/tm/bulk-delete -------------------------------------
    total_before = None
    with Session(engine) as s:
        total_before = tm_module.count_entries(s)

    # (1) filterless POST {} -> 400, nothing deleted.
    r = api.post("/api/tm/bulk-delete", json={}, headers=hdr)
    assert r.status_code == 400, (r.status_code, r.text)
    body = r.json()
    assert body.get("ok") is False, body
    with Session(engine) as s:
        assert tm_module.count_entries(s) == total_before, (
            "a refused filterless bulk-delete must not delete anything"
        )

    # (2) filterless + confirm_all: true -> deletes everything visible.
    r = api.post("/api/tm/bulk-delete", json={"confirm_all": True}, headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["ok"] is True and body["count"] == total_before, body
    with Session(engine) as s:
        assert tm_module.count_entries(s) == 0, "confirm_all:true must wipe the visible TM"

    # reseed for the filtered + dry-run checks (previous step wiped everything).
    with Session(engine) as s:
        pid2 = _mk_project(s, name="FilterlessGuard2")
        for i in range(5):
            tm.write_back(s, pid2, f"fl2-src-{i}", f"fl2-tgt-{i}", "en-US", "fr-FR")

    # (3) filtered bulk-delete (project_id set) is unaffected by the guard, even
    # with no confirm_all and no dry_run.
    r = api.post("/api/tm/bulk-delete", json={"project_id": pid2}, headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["ok"] is True and body["count"] == 5, body
    with Session(engine) as s:
        assert tm_module.count_entries(s, project_id=pid2) == 0, (
            "a filtered bulk-delete must still work without confirm_all"
        )

    # (4) filterless dry_run is unaffected by the guard (dry-run never deletes).
    with Session(engine) as s:
        pid3 = _mk_project(s, name="FilterlessGuard3")
        for i in range(2):
            tm.write_back(s, pid3, f"fl3-src-{i}", f"fl3-tgt-{i}", "en-US", "fr-FR")
        expect = tm_module.count_entries(s)
    r = api.post("/api/tm/bulk-delete", json={"dry_run": True}, headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["ok"] is True and body["dry_run"] is True and body["count"] == expect, body
    with Session(engine) as s:
        assert tm_module.count_entries(s) == expect, "dry-run must not delete anything"

    # --- web route: POST /tm/bulk-delete (forged confirm=1, no filters) ----
    web = TestClient(app)
    before_web = None
    with Session(engine) as s:
        before_web = tm_module.count_entries(s)
    # A forged POST straight to confirm=1 with no filters and no confirm_all must
    # be refused (this is the exact request a legit user's browser would never
    # send -- the real form always routes through the count-first preview first).
    r = web.post("/tm/bulk-delete", data={"confirm": "1"})
    assert r.status_code == 400, (r.status_code, r.text)
    with Session(engine) as s:
        assert tm_module.count_entries(s) == before_web, (
            "a forged filterless confirm=1 POST must not delete anything"
        )
    # The real UI flow still works end to end: preview (no confirm) -> redirect
    # with confirm_n + confirm_all -> confirm=1&confirm_all=1 actually deletes.
    r = web.post("/tm/bulk-delete", data={}, follow_redirects=False)
    assert r.status_code == 303, (r.status_code, r.text)
    loc = r.headers["location"]
    assert f"confirm_n={before_web}" in loc, loc
    assert "confirm_all=1" in loc, ("filterless preview must flag confirm_all", loc)
    r = web.post(
        "/tm/bulk-delete", data={"confirm": "1", "confirm_all": "1"}, follow_redirects=False
    )
    assert r.status_code == 303, (r.status_code, r.text)
    assert f"bulk_deleted={before_web}" in r.headers["location"], r.headers["location"]
    with Session(engine) as s:
        assert tm_module.count_entries(s) == 0, (
            "the real preview-then-confirm flow must still delete everything"
        )
    print(
        "  filterless bulk-delete: /api 400 without confirm_all, deletes with it, "
        "filtered + dry-run unaffected; web route rejects forged confirm=1, "
        "real preview->confirm flow still works: OK"
    )


def test_cli_item_commands_org_scoped():
    """F2 pattern (CRITICAL, R2-3): under multi-org, --org A must NOT show/delete an
    entry that belongs to org B's project. SegmentTM is scoped THROUGH project->org,
    so an --org A operator's visible_pids never includes org B's projects."""
    from app.models import Organization

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            oa = Organization(name="TM Org A")
            ob = Organization(name="TM Org B")
            s.add(oa)
            s.add(ob)
            s.commit()
            s.refresh(oa)
            s.refresh(ob)
            oaid, obid = oa.id, ob.id
            pb = _mk_project(s, name="OrgB Proj", org_id=obid)
            e = tm.write_back(s, pb, "orgb src", "orgb cible", "en-US", "fr-FR")
            eid = e.id
        a, b, es = str(oaid), str(obid), str(eid)
        # show / delete under --org A against org-B's entry -> exit 2 (404-as-usage)
        for argv in (
            ["--org", a, "tm", "show", es],
            ["--org", a, "tm", "delete", es, "--yes"],
            ["--org", a, "tm", "delete", es, "--dry-run"],
        ):
            rc, out = _run_cli(argv)
            assert rc == 2, ("F2: cross-org must be refused (exit 2)", argv, rc, out)
        # org A's LIST/STATS never surface org B's entry either
        rc, out = _run_cli(["--json", "--org", a, "tm", "list"])
        assert rc == 0, out
        assert _json.loads(out)["total"] == 0, ("org A sees none of org B's TM", out)
        # org B (the owner) can show + delete
        assert _run_cli(["--json", "--org", b, "tm", "show", es])[0] == 0
        rc, out = _run_cli(["--json", "--org", b, "tm", "delete", es, "--yes"])
        assert rc == 0, out
        with Session(engine) as s:
            assert s.get(SegmentTM, eid) is None
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  F2 CLI tm show/delete/list org-scoped (cross-org 404 + no leak): OK")


def test_fk_valid_writes_pragma_on():
    """Glossary F1 lesson: on an FK-enforcing engine, a module write must be FK-valid.
    SegmentTM.project_id is a real FK -> project.id; a NULL-project global row is
    FK-legal, and usage bump + delete both work under PRAGMA foreign_keys=ON."""
    from sqlalchemy import create_engine, event
    from sqlmodel import SQLModel

    fk_dir = tempfile.mkdtemp(prefix="loctms_tmfk_")
    fk_engine = create_engine(f"sqlite:///{fk_dir}/fk.db")

    @event.listens_for(fk_engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    SQLModel.metadata.create_all(fk_engine)
    with Session(fk_engine) as s:
        # prove enforcement is live: a bad project_id FK must be rejected
        s.add(
            SegmentTM(
                project_id=999999,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="x",
                target_text="y",
                source_hash="h1",
            )
        )
        try:
            s.commit()
            _fail("FK enforcement not active on the test engine")
        except Exception:
            s.rollback()
        # a NULL-project (global) row is FK-legal; usage bump + delete both work
        e = SegmentTM(
            project_id=None,
            source_locale="en-US",
            target_locale="fr-FR",
            source_text="global",
            target_text="mondial",
            source_hash="h2",
        )
        s.add(e)
        s.commit()
        s.refresh(e)
        assert tm_module.bump_usage(s, [e.id]) == 1
        s.refresh(e)
        assert e.use_count == 1
        assert tm_module.delete_entry(s, e.id) is True
        assert s.get(SegmentTM, e.id) is None
    print("  FK: SegmentTM writes/bump/delete FK-valid under PRAGMA foreign_keys=ON: OK")


def main():
    init_db()
    test_schema()
    test_usage_tracking_translate_segmented()
    test_usage_tracking_many_applies_one_run()
    test_usage_tracking_apply_tm_to_document()
    test_usage_tracking_workflow_step0()
    test_fuzzy_99_5_floors_to_99_never_100()
    test_browse_filters_and_pagination()
    test_stats_summary()
    test_delete_single_and_embedding_invalidation()
    test_bulk_delete_dry_run_vs_real()
    test_bulk_delete_created_before_and_embeddings()
    test_permission_gates_delete_is_can_tune_ai()
    test_scoped_visibility()
    test_cli_round_trip()
    test_cli_bulk_delete_dry_run()
    test_filterless_bulk_delete_requires_confirm_all()
    test_cli_item_commands_org_scoped()
    test_fk_valid_writes_pragma_on()
    print("TM MODULE OK")


if __name__ == "__main__":
    main()
