"""TM-matching LOCK core verification (offline, hermetic).

PROPOSAL-tm-matching-workflow-step, the SHARED engine helpers + write-seam enforcement.
The tm_match engine itself lives in the orchestrator (verify_tm_match_step drives it end
to end); this suite covers the pure helpers it calls + the seam:
  (1) plan_hit: a 100% hit -> ("100", soft) by default; a fuzzy hit -> None
  (2) plan_hit HARD when hundreds_enter_lqa is off; candidate_indices excludes a stored
      target / produced / already-locked segment (B4 -- never clobber an existing value)
  (3) the write seam REFUSES a machine overwrite of a hard-locked segment (target +
      status preserved); a HUMAN edit clears the lock at its own site
      (clear_lock_on_human_edit); TM_LOCKS_ENABLED off = inert
  (4) the seam CLEARS a soft lock on a real overwrite, keeps it on a no-op re-commit (7a)
  (5) unlock_segment clears the lock + writes an audit row (reviewer+ action)
  (6) a soft lock does NOT refuse at the seam (it enters LQA / round-2)
  (7) the hard-lock gate precedes the refusal gate (mutant catcher)
  (8) the audited unlock is REVIEWER+ on BOTH HTTP surfaces (web + /api refuse
      identically): viewer/translator 403 + lock intact + no tm.unlock row;
      reviewer/linguist/manager/admin 200 + cleared + one audit row
  (9) the in-process CLI runs as the trusted operator -> unlock allowed with the
      login wall on

HERMETIC: throwaway LOC_TMS_DATA_DIR; MockProvider offline; ASCII-only.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tmlock_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    AuditLog,
    Document,
    Project,
    Segment,
    SegmentTM,
    SegStatus,
    TargetLocale,
    TransMode,
)
from app.services import quality, tm, tm_lock

init_db()

HEDGE = (
    "I'm ready to translate from English to French. However, I don't see any "
    "source text provided. Please provide the text you would like translated."
)


def _mk_project(s, name="LockProj", src="en-US"):
    p = Project(name=name, source_locale=src)
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _mk_doc(s, project_id, locale, sources):
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


def _poison(s, project_id, source, target, src="en-US", tgt="fr-FR"):
    row = SegmentTM(
        project_id=project_id,
        source_locale=src,
        target_locale=tgt,
        source_text=source,
        target_text=target,
        source_hash=tm.hash_text(source, src, tgt),
    )
    s.add(row)
    s.commit()
    return row


def _seg(s, doc_id, order=0):
    return s.exec(
        select(Segment).where(Segment.document_id == doc_id, Segment.order_index == order)
    ).one()


# --- (1) fill + SOFT lock by default -----------------------------------------
def test_match_and_lock_soft_default():
    with Session(engine) as s:
        p = _mk_project(s)
        tm.write_back(s, p.id, "Save changes.", "Enregistrer.", "en-US", "fr-FR")
        doc = _mk_doc(s, p.id, "fr-FR", ["Save changes.", "Untranslated line."])
        # The SINGLE engine lives in the orchestrator (verify_tm_match_step drives the
        # end-to-end fill+lock); here we test the SHARED decision helpers it calls.
        # plan_hit: a 100% hit under the default (soft) policy -> ("100", hard=False).
        hit = {"match_pct": 100, "target_text": "Enregistrer.", "id": 7}
        assert tm_lock.plan_hit(hit, tm_lock.TmPolicy(hundreds_enter_lqa=True)) == ("100", False)
        # a sub-100 (fuzzy) hit is NOT fillable
        assert tm_lock.plan_hit({"match_pct": 92}, tm_lock.TmPolicy()) is None
        # _stamp_lock persists band/hard + provenance; lock_summary reflects it
        seg = _seg(s, doc.id, 0)
        seg.target_text = "Enregistrer."
        tm_lock._stamp_lock(seg, band="100", hard=False, tm_id=7, entry_id=7)
        summ = tm_lock.lock_summary(seg)
        assert summ and summ["band"] == "100" and summ["hard"] is False, summ
        assert (seg.meta or {}).get("tm_lock"), "provenance must ride meta"
    print("  helpers: plan_hit soft 100 / fuzzy None; _stamp_lock+summary: OK")


# --- (2) HARD lock plan + B4 candidacy (never clobber a stored translation) --
def test_plan_hard_and_candidacy():
    with Session(engine) as s:
        p = _mk_project(s, name="LockHard")
        doc = _mk_doc(s, p.id, "fr-FR", ["a", "b", "c"])
        # plan_hit hard when hundreds_enter_lqa is off (D3)
        hit = {"match_pct": 100, "target_text": "X", "id": 1}
        assert tm_lock.plan_hit(hit, tm_lock.TmPolicy(hundreds_enter_lqa=False)) == ("100", True)
        # B4: candidate_indices excludes a segment whose STORED target is non-empty
        segs = [_seg(s, doc.id, i) for i in range(3)]
        segs[1].target_text = "deja traduit"  # an existing (human/MT) translation
        s.add(segs[1])
        s.commit()
        segs = [_seg(s, doc.id, i) for i in range(3)]
        cands = tm_lock.candidate_indices(segs, produced=set(), already_locked=set())
        assert cands == [0, 2], ("segment 1 has a stored target -> never a candidate", cands)
        # produced + already_locked also exclude
        assert tm_lock.candidate_indices(segs, {0}, {2}) == [], "produced/locked excluded"
    print("  helpers: plan_hit hard; candidacy excludes stored-target/produced/locked: OK")


# --- (2b) Q9: fill_or_relock -- fill empty / re-lock a byte-equal TM fill / skip other ---
def test_fill_or_relock_predicate():
    """Q9 (core-QA fix train 2). The per-segment decision that lets tm_match RE-LOCK a
    prior TM-exact fill (pct=100 stamped, but never locked -- e.g. the plain-translate
    exact-TM short-circuit) while still honouring B4 for a genuinely different existing
    translation."""
    pol = tm_lock.TmPolicy(hundreds_enter_lqa=True)  # soft
    hit = {"match_pct": 100, "target_text": "Enregistrer.", "id": 3}
    # Batch 15 T1 (LAW-TM-12): fill_or_relock now returns a 4-tuple
    # (action, band, hard, lock_mode). A 100-band fill under a soft policy is
    # ("fill", "100", False, "soft"); the lock_mode is what the orchestrator routes on.
    # empty stored target + 100% hit -> FILL (soft)
    assert tm_lock.fill_or_relock("", hit, pol) == ("fill", "100", False, "soft")
    assert tm_lock.fill_or_relock("   ", hit, pol) == ("fill", "100", False, "soft")
    # a stored target that ALREADY byte-equals the hit -> RE-LOCK (the Q9 case)
    assert tm_lock.fill_or_relock("Enregistrer.", hit, pol) == ("relock", "100", False, "soft")
    # a DIFFERENT existing translation -> None (B4: never clobbered, never locked)
    assert tm_lock.fill_or_relock("Sauvegarder.", hit, pol) is None
    # even a one-byte divergence (trailing space) is NOT a re-lock -- byte-exact only
    assert tm_lock.fill_or_relock("Enregistrer. ", hit, pol) is None
    # F10: a NORMALIZATION-ONLY divergence (NFD stored vs NFC entry, same glyphs) is NOT a
    # re-lock -- the equality is DELIBERATELY raw byte-equality, never normalize()d, because
    # a form mismatch means the stored bytes came from a different write path (out of scope).
    import unicodedata

    _nfc = "caf\u00e9"  # "caf" + e-acute as ONE code point (NFC)
    _nfd = "cafe\u0301"  # "cafe" + combining acute (NFD): other bytes
    assert _nfc != _nfd and unicodedata.normalize("NFC", _nfd) == _nfc  # canonical-equal...
    _acc_hit = {"match_pct": 100, "target_text": _nfc, "id": 9}
    assert tm_lock.fill_or_relock(_nfd, _acc_hit, pol) is None  # ...but NOT byte-equal -> skip
    assert tm_lock.fill_or_relock(_nfc, _acc_hit, pol) == ("relock", "100", False, "soft")
    # no 100% hit (fuzzy / None) -> None regardless of the stored target
    assert (
        tm_lock.fill_or_relock(
            "Enregistrer.", {"match_pct": 92, "target_text": "Enregistrer."}, pol
        )
        is None
    )
    assert tm_lock.fill_or_relock("Enregistrer.", None, pol) is None
    # HARD policy carries through both fill and re-lock
    hpol = tm_lock.TmPolicy(hundreds_enter_lqa=False)
    assert tm_lock.fill_or_relock("", hit, hpol) == ("fill", "100", True, "hard")
    assert tm_lock.fill_or_relock("Enregistrer.", hit, hpol) == ("relock", "100", True, "hard")
    print("  helpers: fill_or_relock fill/relock/skip (Q9 re-lock, B4-safe): OK")


# --- (3) the seam refuses a machine overwrite of a HARD lock -----------------
def test_seam_refuses_hard_lock_overwrite():
    with Session(engine) as s:
        p = _mk_project(s, name="SeamRefuse")
        doc = _mk_doc(s, p.id, "fr-FR", ["Locked source."])
        seg = _seg(s, doc.id, 0)
        # hard-lock it with a known good target
        seg.target_text = "Verrouille."
        seg.status = SegStatus.MT
        tm_lock._stamp_lock(seg, band="100", hard=True, tm_id=1, entry_id=1)
        s.add(seg)
        s.commit()
        # a machine (MT) write is REFUSED; target + status preserved
        out = quality.commit_mt_output(s, seg, "Overwritten by MT", source="mt")
        assert out.lock_refused is True and out.committed is False, out
        assert seg.target_text == "Verrouille." and seg.status == SegStatus.MT, seg
        # a HUMAN edit does NOT go through the machine seam -- it clears the lock at its
        # own save site (item 5a). clear_lock_on_human_edit is that mechanism.
        cleared = tm_lock.clear_lock_on_human_edit(s, seg, actor="rev@x.com")
        assert cleared is True, "a human edit of a locked segment clears the lock"
        s.commit()
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert not seg.lock_band and not (seg.meta or {}).get("tm_lock"), seg.lock_band
    print("  seam: hard lock refuses machine write; human edit clears the lock: OK")


def test_kill_switch_disables_lock():
    with Session(engine) as s:
        p = _mk_project(s, name="KillSwitch")
        doc = _mk_doc(s, p.id, "fr-FR", ["Kill source."])
        seg = _seg(s, doc.id, 0)
        seg.target_text = "Verrou."
        tm_lock._stamp_lock(seg, band="100", hard=True, tm_id=1, entry_id=1)
        s.add(seg)
        s.commit()
        orig = config.TM_LOCKS_ENABLED
        try:
            config.TM_LOCKS_ENABLED = False
            out = quality.commit_mt_output(s, seg, "MT wins now.", source="mt")
            assert out.committed is True and out.lock_refused is False, out
            assert seg.target_text == "MT wins now.", seg.target_text
        finally:
            config.TM_LOCKS_ENABLED = orig
    print("  seam: TM_LOCKS_ENABLED off makes locks inert: OK")


# --- (4) seam clears a SOFT lock on a real overwrite, keeps it on a no-op ----
def test_seam_clears_soft_lock_on_change_only():
    with Session(engine) as s:
        p = _mk_project(s, name="SoftClear")
        doc = _mk_doc(s, p.id, "fr-FR", ["Soft src.", "Soft src2."])
        # segment 0: soft-locked; a fix that CHANGES the text clears the lock (item 7a)
        seg0 = _seg(s, doc.id, 0)
        seg0.target_text = "Doux."
        tm_lock._stamp_lock(seg0, band="100", hard=False, tm_id=1, entry_id=1)
        s.add(seg0)
        s.commit()
        out = quality.commit_mt_output(s, seg0, "Corrige.", source="workflow")
        assert out.committed is True and out.lock_refused is False, out
        s.commit()  # commit_mt_output adds but the caller owns the transaction
        s.expire_all()
        seg0 = _seg(s, doc.id, 0)
        assert seg0.target_text == "Corrige.", seg0.target_text
        assert not seg0.lock_band and not (seg0.meta or {}).get("tm_lock"), (
            "a real overwrite of a soft lock clears band + provenance",
            seg0.lock_band,
        )
        # segment 1: soft-locked; a no-op re-commit of the SAME text KEEPS the lock
        seg1 = _seg(s, doc.id, 1)
        seg1.target_text = "Meme."
        tm_lock._stamp_lock(seg1, band="100", hard=False, tm_id=2, entry_id=2)
        s.add(seg1)
        s.commit()
        quality.commit_mt_output(s, seg1, "Meme.", source="workflow")
        s.commit()
        s.expire_all()
        seg1 = _seg(s, doc.id, 1)
        assert seg1.lock_band == "100", (
            "a no-op re-commit must keep the soft lock",
            seg1.lock_band,
        )
    print("  seam: soft lock cleared on a real overwrite, kept on a no-op re-commit: OK")


# --- (5) reviewer+ unlock clears the lock + audits ---------------------------
def test_unlock_clears_and_audits():
    with Session(engine) as s:
        p = _mk_project(s, name="Unlock")
        doc = _mk_doc(s, p.id, "fr-FR", ["Unlock source."])
        seg = _seg(s, doc.id, 0)
        seg.target_text = "Deverrouiller."
        tm_lock._stamp_lock(seg, band="100", hard=True, tm_id=1, entry_id=1)
        s.add(seg)
        s.commit()
        n_before = len(s.exec(select(AuditLog)).all())
        assert tm_lock.unlock_segment(s, seg, actor="rev@x.com") is True
        s.expire_all()
        seg = _seg(s, doc.id, 0)
        assert not seg.lock_band and seg.lock_hard is False, seg
        assert not (seg.meta or {}).get("tm_lock"), "provenance cleared on unlock"
        assert len(s.exec(select(AuditLog)).all()) == n_before + 1, "unlock must audit"
        # unlocking an already-unlocked segment is a no-op (no audit)
        assert tm_lock.unlock_segment(s, seg, actor="rev@x.com") is False
    print("  unlock: clears lock + writes one audit row; no-op when unlocked: OK")


# --- (6) a SOFT lock does NOT refuse at the seam -----------------------------
def test_soft_lock_does_not_refuse_seam():
    with Session(engine) as s:
        p = _mk_project(s, name="SoftSeam")
        doc = _mk_doc(s, p.id, "fr-FR", ["Soft source."])
        seg = _seg(s, doc.id, 0)
        seg.target_text = "Doux."
        tm_lock._stamp_lock(seg, band="100", hard=False, tm_id=1, entry_id=1)
        s.add(seg)
        s.commit()
        # soft lock: the seam still writes (round-2/LQA may edit); the pending-set
        # exclusion is what keeps normal MT off it, not the seam.
        out = quality.commit_mt_output(s, seg, "LQA fix.", source="workflow")
        assert out.committed is True and out.lock_refused is False, out
    print("  seam: SOFT lock does not refuse (enters LQA/round-2): OK")


# --- (7) the lock gate is ABOVE the refusal gate (L12 mutant catcher) --------
def test_hard_lock_precedes_refusal_gate():
    """A HEDGE-shaped write to a HARD-locked segment must be refused BY THE LOCK
    (lock_refused=True), not by the refusal gate. This pins the ORDER: the hard-lock
    check runs FIRST, so a mutant that moves it below the refusal gate flips the outcome
    (flagged as a refusal, lock_refused=False) and this test goes red."""
    with Session(engine) as s:
        p = _mk_project(s, name="LockOrder")
        doc = _mk_doc(s, p.id, "fr-FR", ["Order src."])
        seg = _seg(s, doc.id, 0)
        seg.target_text = "Bon."
        tm_lock._stamp_lock(seg, band="100", hard=True, tm_id=1, entry_id=1)
        s.add(seg)
        s.commit()
        out = quality.commit_mt_output(s, seg, HEDGE, source="mt")
        assert out.lock_refused is True, ("the LOCK must refuse first, above the refusal gate", out)
        s.expire_all()
        assert _seg(s, doc.id, 0).target_text == "Bon.", "good target preserved"
    print("  seam: hard-lock gate precedes the refusal gate (mutant catcher): OK")


# --- (8) RBAC: clearing a TM lock is a REVIEWER+ action on BOTH HTTP surfaces --
def test_unlock_rbac_reviewer_plus_dual_surface():
    """The audited unlock is REVIEWER+ (security.can_review) on the web route AND the
    /api route, refusing IDENTICALLY (task_223abc28 / task_938f801f). Before this the
    web route ran only the middleware's can_write gate (so a TRANSLATOR could clear a
    hard TM lock) and the /api route ran NO role gate at all (so a viewer's docs key
    could). The matrix: viewer/translator -> 403 + lock intact + NO tm.unlock audit
    row; reviewer/linguist/manager/admin -> 200 + lock cleared + exactly one tm.unlock
    row. linguist shares reviewer's RANK (2) and is allowed by design."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Role
    from app.services import apikeys, settings
    from app.services import security as sec
    from tests._csrf import csrf_for

    matrix = [
        (Role.VIEWER, False),
        (Role.TRANSLATOR, False),
        (Role.REVIEWER, True),
        (Role.LINGUIST, True),
        (Role.MANAGER, True),
        (Role.ADMIN, True),
    ]
    PW = "unlock-rbac-pw-123"

    with Session(engine) as s:
        p = _mk_project(s, name="UnlockRBAC")
        pid = p.id
        doc = Document(
            project_id=pid, filename="rbac.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(TargetLocale(project_id=pid, locale="fr-FR"))
        s.commit()
        did = doc.id

    def _lock_new_seg() -> int:
        # A fresh segment per attempt (an ALLOWED unlock clears the lock, so each row
        # must be independent). order_index is unique per (doc, locale) -- take the
        # next free slot so the many attempts do not collide on the UNIQUE key.
        with Session(engine) as s:
            n = len(s.exec(select(Segment).where(Segment.document_id == did)).all())
            seg = Segment(
                document_id=did,
                target_locale="fr-FR",
                order_index=n,
                source_text="Lock me.",
                target_text="Verrou.",
                status=SegStatus.MT,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            tm_lock._stamp_lock(seg, band="100", hard=True, tm_id=1, entry_id=1)
            s.add(seg)
            s.commit()
            return seg.id

    def _locked(seg_id: int) -> bool:
        with Session(engine) as s:
            return bool(s.get(Segment, seg_id).lock_band)

    def _n_unlock_audit() -> int:
        with Session(engine) as s:
            return len(s.exec(select(AuditLog).where(AuditLog.action == "tm.unlock")).all())

    from app.services import access

    creds = {}
    for role, _allowed in matrix:
        email = f"{role}@unlock.test"
        u = sec.create_user(email, email, role, PW)
        _row, raw = apikeys.create_key(u.id, "unlock-rbac", scopes="docs")
        creds[role] = (email, raw)
        # Grant PROJECT VISIBILITY to every role (project_scoping defaults ON, so
        # reviewer/translator/viewer are membership-scoped) — harmless for the
        # see-all roles. This ISOLATES the role gate: a refused role must 403 on the
        # RBAC check, not on a visibility miss, and an allowed role must actually
        # reach the unlock.
        with Session(engine) as s:
            access.add_member(s, pid, email, added_by="test")

    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        # --- /api surface (docs-scoped per-user key) ---
        for role, allowed in matrix:
            _email, raw = creds[role]
            sid = _lock_new_seg()
            before = _n_unlock_audit()
            c = TestClient(app)
            r = c.post("/api/segments/%d/unlock" % sid, headers={"X-Loc-Token": raw}, json={})  # noqa: UP031
            if allowed:
                assert r.status_code == 200 and r.json()["unlocked"] is True, (
                    role,
                    r.status_code,
                    r.text,
                )
                assert not _locked(sid), ("api: allowed unlock must clear the lock", role)
                assert _n_unlock_audit() == before + 1, (
                    "api: allowed unlock writes one tm.unlock row",
                    role,
                )
            else:
                assert r.status_code == 403, (role, r.status_code, r.text)
                assert "reviewer" in r.json().get("error", ""), (role, r.text)
                assert _locked(sid), ("api: refused unlock must NOT clear the lock", role)
                assert _n_unlock_audit() == before, (
                    "api: refused unlock writes NO tm.unlock row",
                    role,
                )
        print(
            "  api unlock RBAC: viewer/translator 403 (locked, no audit); "
            "reviewer/linguist/manager/admin 200 (cleared, audited): OK"
        )

        # --- web surface (session login per role) ---
        for role, allowed in matrix:
            email, _raw = creds[role]
            sid = _lock_new_seg()
            before = _n_unlock_audit()
            c = TestClient(app)
            csrf_for(c)  # anon token for the login POST
            lr = c.post(
                "/auth/login",
                data={"email": email, "password": PW, "next": "/"},
                follow_redirects=False,
            )
            assert lr.status_code == 303, (role, "login", lr.status_code)
            csrf_for(c)  # session rotated on login -> refresh token
            r = c.post("/segments/%d/unlock" % sid)  # noqa: UP031
            if allowed:
                assert r.status_code == 200 and r.json()["unlocked"] is True, (
                    role,
                    r.status_code,
                    r.text,
                )
                assert not _locked(sid), ("web: allowed unlock must clear the lock", role)
                assert _n_unlock_audit() == before + 1, (
                    "web: allowed unlock writes one tm.unlock row",
                    role,
                )
            else:
                # viewer is stopped by the middleware's can_write gate, translator by
                # the route's can_review gate -- both 403, both leave the lock + no row.
                assert r.status_code == 403, (role, r.status_code, r.text[:200])
                assert _locked(sid), ("web: refused unlock must NOT clear the lock", role)
                assert _n_unlock_audit() == before, (
                    "web: refused unlock writes NO tm.unlock row",
                    role,
                )
        print(
            "  web unlock RBAC: viewer/translator 403 (locked, no audit); "
            "reviewer/linguist/manager/admin 200 (cleared, audited): OK"
        )
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()


# --- (9) the in-process CLI runs as the trusted operator -> always allowed -----
def test_unlock_cli_operator_allowed():
    """`doc unlock-segment` runs in-process as the local operator (user None), so it
    clears a lock even with the login wall ON and regardless of the reviewer+ gate the
    HTTP surfaces enforce. Pins that the RBAC train did NOT break the operator path."""
    from types import SimpleNamespace

    from app.cli import cmd_doc
    from app.services import settings

    with Session(engine) as s:
        p = _mk_project(s, name="UnlockCLI")
        doc = _mk_doc(s, p.id, "fr-FR", ["CLI unlock."])
        seg = _seg(s, doc.id, 0)
        seg.target_text = "Op."
        tm_lock._stamp_lock(seg, band="100", hard=True, tm_id=1, entry_id=1)
        s.add(seg)
        s.commit()
        sid = seg.id

    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:

        class _Emit:
            def __init__(self):
                self.data = None

            def result(self, text, data=None):
                self.data = data

        emit = _Emit()
        args = SimpleNamespace(segment_id=sid, remote=False, actor="operator")
        rc = cmd_doc._doc_unlock_segment(args, emit)
        assert rc == 0 and emit.data and emit.data["unlocked"] is True, emit.data
        with Session(engine) as s:
            assert not s.get(Segment, sid).lock_band, "operator CLI must clear the lock"
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  cli unlock: in-process operator allowed even with the login wall on: OK")


if __name__ == "__main__":
    test_match_and_lock_soft_default()
    test_plan_hard_and_candidacy()
    test_fill_or_relock_predicate()
    test_seam_refuses_hard_lock_overwrite()
    test_kill_switch_disables_lock()
    test_seam_clears_soft_lock_on_change_only()
    test_unlock_clears_and_audits()
    test_soft_lock_does_not_refuse_seam()
    test_hard_lock_precedes_refusal_gate()
    test_unlock_rbac_reviewer_plus_dual_surface()
    test_unlock_cli_operator_allowed()
    print("verify_tm_lock: all OK")
