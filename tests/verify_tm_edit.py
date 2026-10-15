"""Edit-a-TM-entry-in-place verification (offline, hermetic).

Covers tm_module.edit_entry (target-only, in place) + all three surfaces (web, CLI,
/api):

  (1) target edit persists; source + source_hash are UNCHANGED (source is the match
      key / identity and is NOT editable)
  (2) refusal-gate enforcement: an edited target that reads as a refusal/hedge is
      REJECTED (TMEditRefused), never stored — an edit is a TM WRITE site and joins
      the same refusal choke point as commit_mt_output / import_tmx (not a bypass)
  (3) blank target rejected; an identical target is a no-op (no audit row)
  (4) EMBEDDING assumption: SegmentEmbedding keys on segment_tm_id and embeds the
      SOURCE, so a target-only edit leaves source_hash + the embedding row intact —
      no invalidation/re-embed (verified in code + here)
  (5) scope: an out-of-scope entry id edits nothing (route 404s)
  (6) audit row (tm.edit) written with old->new detail
  (7) permission gate (web + /api): edit is can_tune_ai (linguist/admin); a translator
      is DENIED, a linguist ALLOWED — same gate as delete (editing is TM management)
  (8) CLI `tm edit --target` (success + refusal rejection)
  (9) /api parity (POST /api/tm/{id}/edit) incl. refusal 400

HERMETIC: own LOC_TMS_DATA_DIR, provider keys + LOCAL_BASE_URL blanked before import
(the embedding assertion pins the offline MockEmbedder). ASCII only.
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

init_env("loctms_tmedit_")

from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import AuditLog, Project, SegmentEmbedding, SegmentTM
from app.services import apikeys, security, settings, tm, tm_module

_HEDGE = "I'm sorry, but I can't translate that."


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
    return security.create_user(
        f"tmedit{role}{_UN}@x.com", role.title(), role, "tmedit-test-pw-123"
    )


def _key(user):
    _row, raw = apikeys.create_key(user.id, "tmedit-test", scopes="api docs")
    return raw


def _mk_project(s, name="TMEdit"):
    p = Project(name=name, source_locale="en-US")
    s.add(p)
    s.commit()
    s.refresh(p)
    return p.id


def _mk_entry(s, pid=None, source="Hello world.", target="Bonjour le monde."):
    if pid is None:
        pid = _mk_project(s, name="TMEntry")
    e = tm.write_back(s, pid, source, target, "en-US", "fr-FR")
    s.refresh(e)
    return e


# ---------------------------------------------------------------------------
def test_target_edit_persists_source_unchanged():
    with Session(engine) as s:
        _mk_project(s)
        e = _mk_entry(s)
        eid, old_hash, old_src = e.id, e.source_hash, e.source_text
        out = tm_module.edit_entry(s, eid, "Salut le monde.", actor="tester")
        assert out is not None and out.id == eid
        s.refresh(e)
        assert e.target_text == "Salut le monde.", e.target_text
        assert e.source_text == old_src, "source text is untouched"
        assert e.source_hash == old_hash, "source_hash (match key) is unchanged"
    print("  edit: target persists; source + source_hash unchanged (identity kept): OK")


def test_refusal_gate_rejects_hedge():
    with Session(engine) as s:
        _mk_project(s)
        e = _mk_entry(s)
        eid, before = e.id, e.target_text
        try:
            tm_module.edit_entry(s, eid, _HEDGE, actor="tester")
            _fail("a refusal/hedge target must be rejected (TMEditRefused)")
        except tm_module.TMEditRefused:
            pass
        s.refresh(e)
        assert e.target_text == before, "a rejected hedge edit stores nothing"
    print("  refusal gate: an edited hedge target is rejected, never stored: OK")


def test_blank_and_noop():
    with Session(engine) as s:
        _mk_project(s)
        e = _mk_entry(s)
        eid = e.id
        try:
            tm_module.edit_entry(s, eid, "   ", actor="tester")
            _fail("a blank target must raise ValueError")
        except ValueError:
            pass
        # identical target = no-op, and writes NO audit row
        n_before = len(s.exec(select(AuditLog).where(AuditLog.action == "tm.edit")).all())
        out = tm_module.edit_entry(s, eid, e.target_text, actor="tester")
        assert out is not None and out.id == eid
        n_after = len(s.exec(select(AuditLog).where(AuditLog.action == "tm.edit")).all())
        assert n_after == n_before, "an identical (no-op) edit writes no audit row"
    print("  blank rejected; identical target is a no-op (no audit): OK")


def test_embedding_unaffected_by_target_edit():
    from array import array

    with Session(engine) as s:
        _mk_project(s)
        e = _mk_entry(s)
        eid = e.id
        # A SegmentEmbedding is keyed by segment_tm_id and stores the vector of the
        # NORMALIZED SOURCE (models.SegmentEmbedding / tm.upsert_embedding). Insert one
        # directly (no dependency on a live embedder) to prove the target-edit path
        # leaves it valid.
        blob = array("f", [0.1, 0.2, 0.3]).tobytes()
        s.add(
            SegmentEmbedding(
                segment_tm_id=eid,
                project_id=e.project_id,
                source_locale="en-US",
                target_locale="fr-FR",
                model="mock",
                dim=3,
                embedding=blob,
            )
        )
        s.commit()
        hash_before = e.source_hash
        # Edit the target only.
        tm_module.edit_entry(s, eid, "Coucou le monde.", actor="tester")
        s.refresh(e)
        emb2 = s.exec(select(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id == eid)).first()
        assert emb2 is not None, "the embedding row survives a target-only edit"
        assert e.source_hash == hash_before, "source_hash unchanged -> embedding valid"
        assert bytes(emb2.embedding) == blob, (
            "embedding vector (of the SOURCE) is unchanged by a target edit"
        )
    print(
        "  embedding: keyed by segment_tm_id on the SOURCE, unchanged by a target "
        "edit (no invalidation needed): OK"
    )


def test_scope_and_audit():
    with Session(engine) as s:
        pa = _mk_project(s, name="ScopeA")
        pb = _mk_project(s, name="ScopeB")
        ea = _mk_entry(s, pa)
        eb = _mk_entry(s, pb)
        # out-of-scope edit -> None, nothing written
        out = tm_module.edit_entry(s, eb.id, "leak?", visible_pids=[pa], actor="x")
        assert out is None, "an out-of-scope entry edits nothing"
        s.refresh(eb)
        assert eb.target_text != "leak?", "out-of-scope edit is a no-op"
        # in-scope edit -> audit row old->new
        tm_module.edit_entry(s, ea.id, "Nouvelle cible.", visible_pids=[pa], actor="auditor@x.com")
        row = s.exec(
            select(AuditLog).where(AuditLog.action == "tm.edit").order_by(AuditLog.id.desc())
        ).first()
        assert row is not None, "an audit row is written"
        assert "Nouvelle cible." in (row.detail or ""), row.detail
        assert "->" in (row.detail or ""), "audit detail records old->new"
    print("  scope: out-of-scope edit is a no-op; in-scope writes tm.edit old->new: OK")


def test_permission_gate_edit_is_can_tune_ai():
    from fastapi.testclient import TestClient

    from app.main import app
    from tests._csrf import csrf_for

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            _mk_project(s, name="PermEdit")
            e1 = _mk_entry(s)
            e2 = _mk_entry(s, source="Second one.", target="Deuxieme.")
            e1id, e2id = e1.id, e2.id

        translator = _mk_user("translator")
        linguist = _mk_user("linguist")
        api = TestClient(app)

        # --- /api/tm/{id}/edit ------------------------------------------------
        r = api.post(
            f"/api/tm/{e1id}/edit",
            json={"target": "Nouveau"},
            headers={"X-Loc-Token": _key(translator)},
        )
        assert r.status_code == 403, ("translator must not edit via /api", r.text)
        with Session(engine) as s:
            assert s.get(SegmentTM, e1id).target_text != "Nouveau", "denied = no-op"

        r = api.post(
            f"/api/tm/{e1id}/edit",
            json={"target": "Nouveau"},
            headers={"X-Loc-Token": _key(linguist)},
        )
        assert r.status_code == 200, ("linguist CAN edit via /api", r.text)
        with Session(engine) as s:
            assert s.get(SegmentTM, e1id).target_text == "Nouveau", "linguist edit took"

        # refusal via /api -> 400, nothing stored
        r = api.post(
            f"/api/tm/{e2id}/edit", json={"target": _HEDGE}, headers={"X-Loc-Token": _key(linguist)}
        )
        assert r.status_code == 400, ("a hedge target is 400 on /api", r.text)
        with Session(engine) as s:
            assert s.get(SegmentTM, e2id).target_text == "Deuxieme.", "hedge not stored"

        # --- web /tm/{id}/edit (session login) --------------------------------
        with Session(engine) as s:
            e3 = _mk_entry(s, source="Third one.", target="Troisieme.")
            e3id = e3.id
        web_t = TestClient(app)
        csrf_for(web_t)
        r = web_t.post(
            "/auth/login",
            data={"email": translator.email, "password": "tmedit-test-pw-123", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        csrf_for(web_t)
        r = web_t.post(f"/tm/{e3id}/edit", data={"target": "X"}, follow_redirects=False)
        assert r.status_code == 403, ("translator denied on web edit", r.status_code)

        web_l = TestClient(app)
        csrf_for(web_l)
        r = web_l.post(
            "/auth/login",
            data={"email": linguist.email, "password": "tmedit-test-pw-123", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        csrf_for(web_l)
        r = web_l.post(
            f"/tm/{e3id}/edit", data={"target": "Trois modifie."}, follow_redirects=False
        )
        assert r.status_code == 303, ("linguist CAN edit via web", r.status_code)
        assert "edited=1" in r.headers["location"], r.headers["location"]
        with Session(engine) as s:
            assert s.get(SegmentTM, e3id).target_text == "Trois modifie.", "web edit took"
        # web refusal -> redirect with ?err=, nothing stored
        r = web_l.post(f"/tm/{e3id}/edit", data={"target": _HEDGE}, follow_redirects=False)
        assert r.status_code == 303 and "err=" in r.headers["location"], r.headers["location"]
        with Session(engine) as s:
            assert s.get(SegmentTM, e3id).target_text == "Trois modifie.", (
                "a hedge edit via web stores nothing"
            )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  permission gate: edit is can_tune_ai (translator denied, linguist "
        "allowed) on web + /api; refusal rejected on both: OK"
    )


def test_cli_edit():
    with Session(engine) as s:
        _mk_project(s, name="CliEdit")
        e = _mk_entry(s, source="Cli source.", target="Cli cible.")
        eid = e.id
    rc, out = _run_cli(["--json", "tm", "edit", str(eid), "--target", "Cli nouvelle."])
    assert rc == 0, out
    body = _json.loads(out)
    assert body["ok"] and body["target_text"] == "Cli nouvelle.", body
    with Session(engine) as s:
        assert s.get(SegmentTM, eid).target_text == "Cli nouvelle.", "CLI edit persisted"
    # refusal rejected on the CLI (non-zero exit, nothing stored)
    rc, out = _run_cli(["tm", "edit", str(eid), "--target", _HEDGE])
    assert rc != 0, ("a hedge target must fail on the CLI", out)
    with Session(engine) as s:
        assert s.get(SegmentTM, eid).target_text == "Cli nouvelle.", "hedge not stored"
    # missing entry -> usage error
    rc, out = _run_cli(["tm", "edit", "999999", "--target", "x"])
    assert rc != 0, ("unknown entry errors", out)
    print("  CLI: tm edit --target persists; hedge rejected; unknown entry errors: OK")


def main():
    init_db()
    test_target_edit_persists_source_unchanged()
    test_refusal_gate_rejects_hedge()
    test_blank_and_noop()
    test_embedding_unaffected_by_target_edit()
    test_scope_and_audit()
    test_permission_gate_edit_is_can_tune_ai()
    test_cli_edit()
    print("TM EDIT OK")


if __name__ == "__main__":
    main()
