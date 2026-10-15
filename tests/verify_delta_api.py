"""SPEC-key-based-projects section 10.5 — THE DELTA API CONTRACT (Batch 4, Wave B).

`GET /api/projects/{pid}/changes` is the batch's differentiator: no surveyed competitor
ships a change feed with first-class deletions (research 01 section 6, Lokalise's own CI
uses git tags as a cursor). A feed that is merely *usually* right is worse than none,
because a consumer cannot tell a missed entry from a quiet week — so this suite attacks
the contract rather than demonstrating it.

Groups:
  [A] the shape: ordered by cursor, bootstrap from 0, next_cursor advances, has_more.
  [B] TOMBSTONES: a delete arrives as an ENTRY, never as an absence — including for
      the consumer whose cursor was set BEFORE the delete, which is the exact case the
      spec asked to construct.
  [C] limit: default, explicit, and the 1000 cap (a caller cannot ask for the world).
  [D] the pruned floor: a cursor below it answers resync_required with an honest
      message and no entries, while since=0 never does.
  [E] cursor stability under INTERLEAVED writes: paging through a window while new
      writes land never skips and never repeats an entry.
  [F] the scope matrix: 401 with no token, 403 for the wrong key kind / a missing
      "keys" scope, and 404 (non-enumerable) for another customer's project.
  [G] the deleted-project rule: "gone, re-bootstrap" (a 404), not a stream of
      tombstones — while the bulk deletes that leave the project ALIVE do tombstone.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). Final line:
"DELTA API OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_delta_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Project, ProjectType, Role, StringKey
from app.services import apikeys, content, security, settings
from app.services import keys as keys_svc

PW = "deltaapi-pw-123456"
_N = [0]


def _uniq() -> int:
    _N[0] += 1
    return _N[0]


def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _token(email, scopes="api keys", role=Role.ADMIN, customer_id=None):
    user = security.create_user(email, email, role, PW, customer_id=customer_id)
    _row, raw = apikeys.create_key(user.id, "delta", scopes=scopes)
    return raw


def _customer(name=None):
    with Session(engine) as s:
        c = Customer(name=name or "DeltaCust %d" % _uniq())  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _project(cust_id, targets="de-DE"):
    with Session(engine) as s:
        p = content.project_create(
            s,
            "DeltaProj %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales=targets,
            customer_id=str(cust_id),
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _add(pid, name, base="Hello"):
    with Session(engine) as s:
        return keys_svc.create_key(
            s, s.get(Project, pid), name, base_value=base, actor="delta-fixture"
        ).id


def _set(pid, key_id, locale, text):
    with Session(engine) as s:
        keys_svc.set_value(
            s,
            s.get(StringKey, key_id),
            locale,
            text,
            origin=keys_svc.ORIGIN_HUMAN,
            actor="delta-fixture",
        )


def _delete(key_id):
    with Session(engine) as s:
        keys_svc.delete_key(s, s.get(StringKey, key_id), actor="delta-fixture")


def _changes(client, tok, pid, **params):
    r = client.get("/api/projects/%d/changes" % pid, params=params, headers={"X-Loc-Token": tok})  # noqa: UP031
    return r


# ============================================================================
# [A] shape
# ============================================================================
def test_shape_and_bootstrap():
    client = TestClient(app)
    tok = _token("delta-shape@x.com")
    pid = _project(_customer())
    k1 = _add(pid, "shape::one", "One")
    _set(pid, k1, "de-DE", "Eins")

    r = _changes(client, tok, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    for field in ("entries", "next_cursor", "has_more", "resync_required"):
        assert field in body, (field, body)
    assert body["resync_required"] is False, body
    entries = body["entries"]
    assert entries, body
    assert [e["cursor"] for e in entries] == sorted(e["cursor"] for e in entries), (
        "entries must arrive in cursor order"
    )
    _BASE = {
        "cursor",
        "entity",
        "op",
        "key_id",
        "namespace",
        "name",
        "target_locale",
        "plural_category",
        "changed_at",
        "actor",
    }
    import hashlib as _hl

    for e in entries:
        # AP-1: the base field set is always present; VALUE ops carry two ADDITIVE
        # fields (text + content_hash), key ops and deleted tombstones do not.
        assert set(e) >= _BASE, e
        if e["entity"] == "value" and e["op"] != "deleted":
            assert set(e) == _BASE | {"text", "content_hash"}, e
            assert e["content_hash"] == _hl.sha256(e["text"].encode("utf-8")).hexdigest(), e
        else:
            assert set(e) == _BASE, ("key/deleted entries stay tombstone-shaped", e)
    assert entries[0]["entity"] == "key" and entries[0]["op"] == "created", entries[0]
    assert body["next_cursor"] == entries[-1]["cursor"], body
    assert any(e["entity"] == "value" and e["op"] == "updated" for e in entries), entries

    # since=<last> is empty until something else happens, then carries only the new
    after = _changes(client, tok, pid, since=body["next_cursor"]).json()
    assert after["entries"] == [] and after["resync_required"] is False, after
    _add(pid, "shape::two", "Two")
    after2 = _changes(client, tok, pid, since=body["next_cursor"]).json()
    assert {e["name"] for e in after2["entries"]} == {"shape::two"}, after2
    print(
        "  [A] the envelope, cursor ordering, the entry field set, next_cursor and "
        "an empty tail after a caught-up cursor OK"
    )


# ============================================================================
# [B] tombstones
# ============================================================================
def test_deletions_are_entries_not_absences():
    """The spec's own challenge: construct a delete that a since-cursor consumer
    misses. It cannot be constructed -- that is the claim."""
    client = TestClient(app)
    tok = _token("delta-tomb@x.com")
    pid = _project(_customer())
    doomed = _add(pid, "tomb::doomed", "Doomed")
    _set(pid, doomed, "de-DE", "Verurteilt")

    # a consumer catches up BEFORE the delete...
    cursor = _changes(client, tok, pid).json()["next_cursor"]
    _delete(doomed)
    # ...and its very next poll learns about it.
    body = _changes(client, tok, pid, since=cursor).json()
    tombs = [e for e in body["entries"] if e["op"] == "deleted"]
    assert tombs, ("the delete must arrive as an ENTRY", body)
    t = tombs[0]
    assert t["entity"] == "key" and t["key_id"] == doomed, t
    assert t["name"] == "tomb::doomed", (
        "a tombstone must be self-describing after the key row is gone",
        t,
    )
    assert t["actor"] == "delta-fixture", t

    # a tombstoned key emits NO further value entries (review A9)
    end = body["next_cursor"]
    with Session(engine) as s:
        key = s.get(StringKey, doomed)
        for seg in keys_svc.values_for(s, key):
            seg.target_text = "zombie"
            s.add(seg)
        s.commit()
    after = _changes(client, tok, pid, since=end).json()
    assert after["entries"] == [], ("a tombstoned key must emit no further value entries", after)

    # ...and RESTORE re-announces the key AND its surviving values (re-review R10),
    # so a feed-only consumer can repopulate it.
    with Session(engine) as s:
        keys_svc.restore_key(s, s.get(StringKey, doomed), actor="delta-fixture")
    back = _changes(client, tok, pid, since=end).json()["entries"]
    assert any(e["entity"] == "key" and e["op"] == "created" for e in back), back
    assert any(e["entity"] == "value" and e["op"] == "created" for e in back), (
        "restore must re-announce the values, or the consumer has a live key it can never populate",
        back,
    )
    print(
        "  [B] a delete arrives as a first-class entry for a cursor set BEFORE it; "
        "a tombstoned key goes silent; restore re-announces key AND values OK"
    )


def test_bulk_locale_removal_tombstones():
    """A bulk delete that leaves the PROJECT alive must still tombstone (review A3).
    Removing a target locale destroys every key's value rows for it with a bulk
    sa_delete the ORM listener structurally cannot see."""
    client = TestClient(app)
    tok = _token("delta-bulk@x.com")
    pid = _project(_customer(), targets="de-DE, fr-FR")
    # UNtranslated on purpose: a locale with translated content is refused by
    # `remove_target_locale` outright, and the claim under test is that the bulk
    # sa_delete which DOES run emits tombstones for the value rows it destroys.
    _add(pid, "bulk::probe", "Probe")
    cursor = _changes(client, tok, pid).json()["next_cursor"]
    with Session(engine) as s:
        content.remove_target_locale(s, pid, "fr-FR")
    body = _changes(client, tok, pid, since=cursor).json()
    gone = [e for e in body["entries"] if e["op"] == "deleted" and e["target_locale"] == "fr-FR"]
    assert gone, (
        "removing a locale must tombstone its key values, or a consumer keeps them forever",
        body,
    )
    print(
        "  [B] removing a target locale emits real value tombstones (the bulk "
        "delete the ORM listener cannot see) OK"
    )


# ============================================================================
# [C] limit
# ============================================================================
def test_limit_is_capped_and_paged():
    client = TestClient(app)
    tok = _token("delta-limit@x.com")
    pid = _project(_customer())
    for i in range(6):
        _add(pid, "limit::k%d" % i, "V%d" % i)  # noqa: UP031

    body = _changes(client, tok, pid, limit=3).json()
    assert len(body["entries"]) == 3 and body["has_more"] is True, body
    second = _changes(client, tok, pid, since=body["next_cursor"], limit=3).json()
    assert len(second["entries"]) == 3, second
    assert min(e["cursor"] for e in second["entries"]) > body["next_cursor"], second

    huge = _changes(client, tok, pid, limit=999999).json()
    assert huge["has_more"] is False and len(huge["entries"]) <= keys_svc.MAX_DELTA_LIMIT
    # a garbage limit is a default, not a 500
    assert _changes(client, tok, pid, limit=0).status_code == 200
    assert _changes(client, tok, pid, since=-5).status_code == 200
    print(
        "  [C] limit pages cleanly, has_more is honest, and an absurd limit is "  # noqa: UP031
        "capped at %d rather than served OK" % keys_svc.MAX_DELTA_LIMIT
    )


# ============================================================================
# [D] the pruned floor
# ============================================================================
def test_pruned_floor_signals_resync():
    client = TestClient(app)
    tok = _token("delta-floor@x.com")
    pid = _project(_customer())
    _add(pid, "floor::old", "Old")
    # A cursor STRICTLY BELOW the floor is the case that must resync. A consumer
    # sitting exactly ON the floor has provably received everything that was pruned,
    # so it is caught up, not stale -- pinning the first entry's cursor keeps this
    # test on the interesting side of that boundary rather than its edge.
    stale = _changes(client, tok, pid).json()["entries"][0]["cursor"]

    # age the existing rows past the retention window, then prune
    from datetime import datetime, timedelta

    from sqlmodel import select

    from app.models import KeyChangeLog

    with Session(engine) as s:
        for row in s.exec(select(KeyChangeLog).where(KeyChangeLog.project_id == pid)).all():
            row.changed_at = datetime.utcnow() - timedelta(days=400)
            s.add(row)
        s.commit()
        keys_svc.prune_changelog(s, pid)
    _add(pid, "floor::new", "New")

    body = _changes(client, tok, pid, since=stale).json()
    assert body["resync_required"] is True, (
        "a cursor below the pruned floor must say so, not report 'up to date'",
        body,
    )
    assert body["entries"] == [], body
    assert "re-bootstrap" in (body.get("message") or ""), body

    # since=0 is a bootstrap and NEVER reports resync
    boot = _changes(client, tok, pid, since=0).json()
    assert boot["resync_required"] is False, boot
    assert {e["name"] for e in boot["entries"]} == {"floor::new"}, boot
    print(
        "  [D] a cursor under the pruned floor answers resync_required + an "
        "honest message; since=0 bootstraps without one OK"
    )


# ============================================================================
# [E] cursor stability under interleaved writes
# ============================================================================
def test_cursor_stability_under_interleaved_writes():
    """Page through the feed while NEW writes land between pages. The union of the
    pages must contain every entry from the original window exactly once -- a cursor
    that skipped or repeated would silently corrupt a consumer's copy."""
    client = TestClient(app)
    tok = _token("delta-stable@x.com")
    pid = _project(_customer())
    keys = [_add(pid, "stab::k%d" % i, "V%d" % i) for i in range(4)]  # noqa: UP031

    seen, cursor, rounds = [], 0, 0
    while rounds < 20:
        rounds += 1
        body = _changes(client, tok, pid, since=cursor, limit=2).json()
        seen.extend(body["entries"])
        cursor = body["next_cursor"]
        if not body["has_more"]:
            break
        # an interleaved write between pages -- the racy shape a timestamp cursor
        # or a per-project counter would lose an entry to
        _set(pid, keys[rounds % len(keys)], "de-DE", "round %d" % rounds)  # noqa: UP031

    cursors = [e["cursor"] for e in seen]
    assert cursors == sorted(cursors), ("pages must stay globally ordered", cursors)
    assert len(cursors) == len(set(cursors)), ("no entry may be served twice", cursors)
    with Session(engine) as s:
        from sqlmodel import select

        from app.models import KeyChangeLog

        all_rows = s.exec(
            select(KeyChangeLog).where(KeyChangeLog.project_id == pid).order_by(KeyChangeLog.id)
        ).all()
        highest = max(cursors)
        expected = [r.id for r in all_rows if r.id <= highest]
    assert cursors == expected, (
        "the pages must cover EVERY entry up to the last cursor served, with no gap",
        set(expected) - set(cursors),
    )
    print(
        "  [E] paging through %d entries while writes interleave skips nothing and "  # noqa: UP031
        "repeats nothing OK" % len(cursors)
    )


# ============================================================================
# [F] the scope + isolation matrix
# ============================================================================
def test_scope_and_isolation_matrix():
    _auth(True)
    try:
        client = TestClient(app)
        cust_a, cust_b = _customer("DeltaA %d" % _uniq()), _customer("DeltaB %d" % _uniq())  # noqa: UP031
        pid_a, pid_b = _project(cust_a), _project(cust_b)
        _add(pid_a, "matrix::a", "A")
        _add(pid_b, "matrix::bSECRET", "B")

        # no identity -> 401
        r = client.get("/api/projects/%d/changes" % pid_a)  # noqa: UP031
        assert r.status_code == 401, (r.status_code, r.text)

        # a token with api+docs but NOT keys -> 403
        docs_tok = _token("delta-docs@x.com", scopes="api docs")
        r = _changes(client, docs_tok, pid_a)
        assert r.status_code == 403, (r.status_code, r.text)
        assert "keys-scoped" in r.text, r.text

        # "api" alone -> 403
        api_tok = _token("delta-apionly@x.com", scopes="api")
        assert _changes(client, api_tok, pid_a).status_code == 403

        # the shared CONNECTOR token (not a per-user key) -> 403
        from app.web.deps import connector_api_token

        r = _changes(client, connector_api_token(), pid_a)
        assert r.status_code == 403, (r.status_code, r.text)

        # a keys-scoped token bound to customer A: its own project reads...
        bound = _token("delta-bound@x.com", customer_id=cust_a, role=Role.LINGUIST)
        assert _changes(client, bound, pid_a).status_code == 200
        # ...and customer B's is a 404, NOT a 403 (non-enumerable)
        r = _changes(client, bound, pid_b)
        assert r.status_code == 404, (r.status_code, r.text)
        assert "matrix::bSECRET" not in r.text, ("the refusal must not leak content", r.text)
        # a project id that never existed answers IDENTICALLY -- that is the point
        missing = client.get("/api/projects/9999999/changes", headers={"X-Loc-Token": bound})
        assert missing.status_code == 404 and missing.json() == r.json(), (
            "a cross-customer project must be indistinguishable from a missing one",
            r.json(),
            missing.json(),
        )

        # a DOCUMENT project is the same 404 through this endpoint
        with Session(engine) as s:
            doc_pid = content.project_create(
                s,
                "DeltaDoc %d" % _uniq(),  # noqa: UP031
                source_locale="en-US",
                target_locales="de-DE",
                customer_id=str(cust_a),
            ).id
        admin = _token("delta-admin@x.com")
        assert _changes(client, admin, doc_pid).status_code == 404
    finally:
        _auth(False)
    print(
        "  [F] 401 no token / 403 wrong kind + missing scope / 404 cross-customer, "
        "byte-identical to a missing id OK"
    )


# ============================================================================
# [G] a deleted project is 'gone, re-bootstrap'
# ============================================================================
def test_deleted_project_is_a_404_not_a_tombstone_stream():
    client = TestClient(app)
    tok = _token("delta-projdel@x.com")
    pid = _project(_customer())
    _add(pid, "projdel::probe", "Probe")
    cursor = _changes(client, tok, pid).json()["next_cursor"]
    with Session(engine) as s:
        content.delete_project(s, pid)
    r = _changes(client, tok, pid, since=cursor)
    assert r.status_code == 404, (
        "a deleted project must answer 404 (re-bootstrap), not a tombstone stream",
        r.status_code,
        r.text,
    )
    print(
        "  [G] a deleted PROJECT is a 404 for its consumers -- spec section 5's "
        "'gone, re-bootstrap', not a stream of tombstones OK"
    )


# ============================================================================
# [H] AP-1 — modified_after mode + refuse-loudly + enrichment (gate H1/H2/M1/M2/L)
# ============================================================================
def test_modified_after_and_refuse_loudly():
    import hashlib
    from datetime import datetime, timedelta

    client = TestClient(app)
    tok = _token("delta-modafter@x.com")
    pid = _project(_customer(), targets="de-DE, fr-FR")
    # L1: a key whose values are ONLY ever CREATED (never re-edited) must still be
    # visible to modified_after -- the `fresh` branch bumps translations_modified_at.
    k1 = _add(pid, "ma::one", "One")
    _set(pid, k1, "de-DE", "Eins")
    _set(pid, k1, "fr-FR", "Un")

    t0 = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    body = _changes(client, tok, pid, modified_after=t0).json()
    assert body["mode"] == "modified_after", body
    assert body["resync_required"] is False and "next_cursor" not in body, body
    assert "next_after_id" in body, ("keyset cursor id must ride back", body)
    got = {(e["target_locale"], e["text"]) for e in body["entries"]}
    assert ("de-DE", "Eins") in got and ("fr-FR", "Un") in got, body
    for e in body["entries"]:
        assert e["content_hash"] == hashlib.sha256(e["text"].encode("utf-8")).hexdigest(), e
        assert "cursor" not in e, ("modified_after entries are not cursor-addressed", e)

    # locale filter narrows the feed (a KNOWN locale)
    only_de = _changes(client, tok, pid, modified_after=t0, locale="de-DE").json()
    assert {e["target_locale"] for e in only_de["entries"]} == {"de-DE"}, only_de

    # L3: a materialized-but-never-translated locale is NOT emitted. ja-JP is added
    # with no value written, so it must never appear as an empty-text entry.
    with Session(engine) as s:
        content.add_target_locale(s, pid, "ja-JP")
    _set(pid, k1, "de-DE", "Eins-2")  # touch the key so it's fresh past t0 again
    fresh = _changes(client, tok, pid, modified_after=t0).json()
    assert all(e["target_locale"] != "ja-JP" for e in fresh["entries"]), (
        "a never-translated locale (empty value) must not be emitted",
        fresh,
    )
    assert all(e["text"] != "" for e in fresh["entries"]), fresh

    # a timestamp AFTER every write is empty, and its cursor holds (no silent advance)
    t2 = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    empty = _changes(client, tok, pid, modified_after=t2).json()
    assert empty["entries"] == [] and empty["has_more"] is False, empty
    assert empty["next_after_id"] is None, empty

    # --- H2: locale is validated + is modified_after-only ------------------------
    # (a) an UNKNOWN locale is a 400, not an empty feed with an advancing cursor
    bad_loc = _changes(client, tok, pid, modified_after=t0, locale="zz-ZZ")
    assert bad_loc.status_code == 400 and bad_loc.json()["ok"] is False, bad_loc.text
    # (b) locale / after_id OUTSIDE modified_after mode is a 400 (no accept-and-ignore)
    assert _changes(client, tok, pid, since=0, locale="de-DE").status_code == 400
    assert _changes(client, tok, pid, since=0, after_id=5).status_code == 400

    # REFUSE LOUDLY: an unparseable modified_after is a 400; both modes at once is 400
    assert _changes(client, tok, pid, modified_after="not-a-date").status_code == 400
    assert _changes(client, tok, pid, since=0, modified_after=t0).status_code == 400
    # F-4: a malformed cursor token -- bad id, or an OUT-OF-RANGE (negative) id -- is a
    # 400, not a silent 'match everything' (a negative id makes `id > after_id` admit all).
    assert _changes(client, tok, pid, modified_after=t0 + "~notanint").status_code == 400
    assert _changes(client, tok, pid, modified_after=t0 + "~-5").status_code == 400

    # modified_after NEVER reports deletions: delete a key, it just stops appearing
    before_del = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    _delete(k1)
    after = _changes(client, tok, pid, modified_after=before_del).json()
    assert all(e["key_id"] != k1 for e in after["entries"]), (
        "a deleted key must vanish from modified_after (no tombstones here)",
        after,
    )
    print(
        "  [H] modified_after: enriched current-state rows, keyset cursor, validated "
        "locale, L1 fresh-visible, L3 empty-locale skip, loud refusals OK"
    )


def test_modified_after_keyset_tie_safety():
    """[H] gate M1: keyset pagination over a MICROSECOND-TIE group must page every key
    exactly once with no skip and no repeat, and never overrun the limit. Ten keys are
    forced to share one translations_modified_at; paging at limit=1 must still walk all
    ten via (next_modified_after, next_after_id)."""
    from datetime import datetime

    from app.models import StringKey

    client = TestClient(app)
    tok = _token("delta-tie@x.com")
    pid = _project(_customer())
    ids = [_add(pid, "tie::k%02d" % i, "V%d" % i) for i in range(10)]  # noqa: UP031
    for kid in ids:
        _set(pid, kid, "de-DE", "t%d" % kid)  # noqa: UP031
    tie = datetime(2030, 1, 1, 0, 0, 0)
    with Session(engine) as s:
        for kid in ids:
            k = s.get(StringKey, kid)
            k.translations_modified_at = tie
            s.add(k)
        s.commit()

    floor = "2029-01-01T00:00:00"
    seen, ma, aid, rounds = [], floor, None, 0
    while rounds < 50:
        rounds += 1
        params = {"modified_after": ma, "limit": 1}
        if aid is not None:
            params["after_id"] = aid
        b = _changes(client, tok, pid, **params).json()
        assert len(b["entries"]) <= 1 * 6, ("a limit=1 page must not overrun", b)
        seen.extend(e["key_id"] for e in b["entries"])
        ma, aid = b["next_modified_after"], b["next_after_id"]
        if not b["has_more"]:
            break
    assert set(seen) == set(ids), ("every tied key must be paged", set(ids) - set(seen))
    assert len(seen) == len(set(seen)), ("no key may repeat across pages", seen)
    print(
        "  [H] keyset pagination walks a 10-key microsecond tie at limit=1 with no "
        "skip and no repeat (gate M1) OK"
    )


def test_modified_after_single_field_token_resume():
    """[H] gate NEW-3: feeding back ONLY the advertised `next_modified_after` (the opaque
    ISO~id token), with NO after_id param, must page a microsecond-tie group with no
    loss. This is the half-cursor bug: next_modified_after alone used to drop every
    same-timestamp key past the first."""
    from datetime import datetime

    from app.models import StringKey

    client = TestClient(app)
    tok = _token("delta-token@x.com")
    pid = _project(_customer())
    ids = [_add(pid, "tok::k%02d" % i, "V%d" % i) for i in range(10)]  # noqa: UP031
    for kid in ids:
        _set(pid, kid, "de-DE", "t%d" % kid)  # noqa: UP031
    tie = datetime(2031, 1, 1, 0, 0, 0)
    with Session(engine) as s:
        for kid in ids:
            k = s.get(StringKey, kid)
            k.translations_modified_at = tie
            s.add(k)
        s.commit()

    seen, ma, rounds = [], "2030-01-01T00:00:00", 0
    while rounds < 50:
        rounds += 1
        # ONLY modified_after is threaded -- no after_id. The token must carry the id.
        b = _changes(client, tok, pid, modified_after=ma, limit=1).json()
        seen.extend(e["key_id"] for e in b["entries"])
        ma = b["next_modified_after"]
        if not b["has_more"]:
            break
        assert "~" in ma, ("next_modified_after must be a resumable token while paging", b)
    assert set(seen) == set(ids), (
        "the single advertised field must lose nothing",
        set(ids) - set(seen),
    )
    assert len(seen) == len(set(seen)), seen
    print(
        "  [H] single-field token resume (next_modified_after only) walks a tie with "
        "no loss (gate NEW-3) OK"
    )


def test_modified_after_predating_floor_resyncs():
    """[H] gate F-3: a `modified_after` older than the change-history window
    (now - retention_days) answers `resync_required` (a value cleared before that horizon
    has had its changelog op pruned, so cleared-value detection can no longer be trusted),
    mirroring the since-mode floor -- not a silent, incomplete feed. A T inside the window
    still serves normally."""
    from datetime import datetime, timedelta

    client = TestClient(app)
    tok = _token("delta-mafloor@x.com")
    pid = _project(_customer())
    k = _add(pid, "mafloor::k", "V")
    _set(pid, k, "de-DE", "Alt")

    # a T older than the 90-day retention window -> resync_required, no entries
    body = _changes(client, tok, pid, modified_after="2000-01-01T00:00:00").json()
    assert body["resync_required"] is True and body["entries"] == [], body
    assert "re-bootstrap" in (body.get("message") or ""), body
    # a recent T (well inside the window) still serves normally
    recent = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    ok = _changes(client, tok, pid, modified_after=recent).json()
    assert ok["resync_required"] is False, ok
    # and disabling retention (window 0) never resyncs, even for an ancient T
    settings.set_many({"key_changelog_retention_days": "0"})
    settings.invalidate_cache()
    try:
        unbounded = _changes(client, tok, pid, modified_after="2000-01-01T00:00:00").json()
        assert unbounded["resync_required"] is False, unbounded
    finally:
        settings.set_many({"key_changelog_retention_days": "90"})
        settings.invalidate_cache()
    print(
        "  [H] a modified_after older than the retention window answers "
        "resync_required; a recent T serves; window=0 never resyncs (gate F-3) OK"
    )


def test_modified_after_cleared_vs_placeholder():
    """[H] gate NEW-1: a CLEARED value must emit (text:"", empty-string hash) so a
    consumer retires the string; a never-translated PLACEHOLDER must NOT emit. Both are
    empty target_text with status NEW, so the distinguisher is the changelog."""
    import hashlib
    from datetime import datetime, timedelta

    client = TestClient(app)
    tok = _token("delta-clr@x.com")
    pid = _project(_customer(), targets="de-DE, fr-FR")
    k = _add(pid, "clr::k", "Base")
    _set(pid, k, "de-DE", "Eins")
    _set(pid, k, "fr-FR", "Un")
    with Session(engine) as s:  # a never-translated placeholder locale
        content.add_target_locale(s, pid, "ja-JP")

    t0 = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    _set(pid, k, "de-DE", "")  # CLEAR de-DE (was "Eins")

    # locale-scoped de-DE consumer must SEE the clear (text:"") -- not 0 entries
    de = _changes(client, tok, pid, modified_after=t0, locale="de-DE").json()
    de_e = [e for e in de["entries"] if e["key_id"] == k]
    assert de_e, ("a cleared value must not vanish from a locale-scoped feed", de)
    assert de_e[0]["text"] == "" and de_e[0]["content_hash"] == hashlib.sha256(b"").hexdigest(), (
        de_e
    )

    # unscoped: cleared de-DE PRESENT, pristine ja-JP placeholder ABSENT
    allm = _changes(client, tok, pid, modified_after=t0).json()
    locs = {e["target_locale"] for e in allm["entries"] if e["key_id"] == k}
    assert "de-DE" in locs, ("cleared value present", allm)
    assert "ja-JP" not in locs, ("pristine placeholder must be excluded", allm)

    # clear ALL translated locales -> the key must NOT vanish (since-mode reports it)
    t1 = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    _set(pid, k, "fr-FR", "")
    allm2 = _changes(client, tok, pid, modified_after=t1).json()
    assert k in {e["key_id"] for e in allm2["entries"]}, (
        "a fully-cleared key must not silently vanish from modified_after",
        allm2,
    )
    assert "ja-JP" not in {e["target_locale"] for e in allm2["entries"] if e["key_id"] == k}, allm2
    print(
        "  [H] cleared value emits (text:''), pristine placeholder excluded, "
        "fully-cleared key does not vanish (gate NEW-1) OK"
    )


def test_modified_after_entry_ceiling():
    """[H] gate M2: the per-response entry ceiling truncates at a KEY boundary with
    has_more=true, and keyset resume covers the rest. Exercised with a temporarily
    lowered MAX_DELTA_ENTRIES so the test needs only a handful of keys."""
    from datetime import datetime, timedelta

    client = TestClient(app)
    tok = _token("delta-ceil@x.com")
    pid = _project(_customer(), targets="de-DE, fr-FR")
    ids = [_add(pid, "ceil::k%d" % i, "V%d" % i) for i in range(4)]  # noqa: UP031
    for kid in ids:
        _set(pid, kid, "de-DE", "d%d" % kid)  # noqa: UP031
        _set(pid, kid, "fr-FR", "f%d" % kid)  # 2 entries per key  # noqa: UP031

    orig = keys_svc.MAX_DELTA_ENTRIES
    set_pieces(
        "app.services.keys", "MAX_DELTA_ENTRIES", 3
    )  # forces a break after the first key (2 <= 3, 4 > 3)
    try:
        # before the writes (which land at 'now') but INSIDE the retention window,
        # so F-3 does not resync this out.
        floor = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        seen, ma, aid, rounds = [], floor, None, 0
        first = _changes(client, tok, pid, modified_after=floor, limit=1000).json()
        assert first["has_more"] is True, ("the ceiling must force has_more", first)
        assert len(first["entries"]) <= 4, first
        while rounds < 50:
            rounds += 1
            params = {"modified_after": ma, "limit": 1000}
            if aid is not None:
                params["after_id"] = aid
            b = _changes(client, tok, pid, **params).json()
            seen.extend((e["key_id"], e["target_locale"]) for e in b["entries"])
            ma, aid = b["next_modified_after"], b["next_after_id"]
            if not b["has_more"]:
                break
        assert len(seen) == len(set(seen)), ("ceiling paging must not repeat", seen)
        assert {kid for kid, _ in seen} == set(ids), (set(ids), seen)
    finally:
        set_pieces("app.services.keys", "MAX_DELTA_ENTRIES", orig)
    print("  [H] entry-ceiling truncates at a key boundary + keyset resume covers all (gate M2) OK")


def test_changelog_soft_deleted_key_not_enriched():
    """[H] gate L4: a since-cursor VALUE entry whose key is now soft-deleted must NOT be
    enriched with the parked trash text (base tombstone-less shape only)."""
    client = TestClient(app)
    tok = _token("delta-l4@x.com")
    pid = _project(_customer())
    k = _add(pid, "l4::secret", "Secret")
    _set(pid, k, "de-DE", "Geheim")
    _delete(k)
    boot = _changes(client, tok, pid, since=0).json()
    val_entries = [e for e in boot["entries"] if e["entity"] == "value" and e["key_id"] == k]
    assert val_entries, boot
    for e in val_entries:
        assert "text" not in e and "content_hash" not in e, (
            "a soft-deleted key's value entries must not leak live/parked text",
            e,
        )
    print("  [H] changelog value entries for a soft-deleted key are not enriched (gate L4) OK")


def main():
    init_db()
    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    test_shape_and_bootstrap()
    test_modified_after_and_refuse_loudly()
    test_modified_after_keyset_tie_safety()
    test_modified_after_single_field_token_resume()
    test_modified_after_predating_floor_resyncs()
    test_modified_after_cleared_vs_placeholder()
    test_modified_after_entry_ceiling()
    test_changelog_soft_deleted_key_not_enriched()
    test_deletions_are_entries_not_absences()
    test_bulk_locale_removal_tombstones()
    test_limit_is_capped_and_paged()
    test_pruned_floor_signals_resync()
    test_cursor_stability_under_interleaved_writes()
    test_scope_and_isolation_matrix()
    test_deleted_project_is_a_404_not_a_tombstone_stream()
    print("DELTA API OK")


if __name__ == "__main__":
    main()
