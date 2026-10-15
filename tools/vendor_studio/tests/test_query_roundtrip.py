"""Wave 3 Train E — the vendor-side half of the buyer query round trip (§7.2/§7.5).

Covers the two pre-existing vendor-side bugs' fixes (HIGH-5's guard split), the inbound
buyer-query storage (direction='in'), the outbound shared-comment carry, and the VS
additive-column shim (MED-6). The shared/unshared payload proof + locked-commentable proof
live beside the existing contract round-trip in test_contract_roundtrip.py.
"""

from __future__ import annotations

from conftest import sample_contract
from vstudio import delivery, editor, intake
from vstudio.models import VComment, VProjectStatus


def _with_query(claim="q-claim", ref="42", subject="Why literal tag?"):
    c = sample_contract(claim=claim)
    c["segments"][0]["queries"] = [
        {
            "ref": ref,
            "subject": subject,
            "status": "open",
            "posts": [
                {
                    "body": "The vendor should keep the ⟦x1⟧ token.",
                    "author_role": "buyer",
                    "at": "2026-08-19T10:00:00",
                }
            ],
        }
    ]
    return c


# --- inbound: buyer queries land as direction='in' VComments (§7.2) -----------
def test_inbound_query_stored_as_in_comment(session):
    proj = intake.store_dispatch(session, _with_query())
    seg0 = delivery.project_segments(session, proj)[0]
    ins = editor.inbound_queries_for(session, seg0)
    assert len(ins) == 1
    assert ins[0].direction == "in" and ins[0].external_ref == "42"
    assert "Why literal tag?" in ins[0].body
    # a segment with no queries has no inbound rows.
    seg1 = delivery.project_segments(session, proj)[1]
    assert editor.inbound_queries_for(session, seg1) == []


def test_inbound_query_dedups_on_replay(session):
    """store_dispatch is idempotent on the claim token, so a re-POST returns the existing
    project without re-storing; the per-(segment, ref) de-dup is belt-and-braces."""
    proj = intake.store_dispatch(session, _with_query())
    intake._store_inbound_queries(session, proj, _with_query()["segments"])
    seg0 = delivery.project_segments(session, proj)[0]
    assert len(editor.inbound_queries_for(session, seg0)) == 1


# --- HIGH-5: the guard split (locked commentable, project-closed not) ----------
def test_guard_commentable_blocks_closed_project(session):
    proj = intake.store_dispatch(session, sample_contract())
    seg = delivery.project_segments(session, proj)[0]
    for closed in (VProjectStatus.CANCELLED, VProjectStatus.DELIVERED):
        proj.status = closed
        session.add(proj)
        session.commit()
        try:
            editor.add_comment(session, proj, seg, author="Ana", role="Translator", body="hello")
            assert False, f"add_comment must raise on a {closed} project"
        except editor.EditError:
            pass


def test_locked_add_comment_succeeds_but_patch_raises(session):
    proj = intake.store_dispatch(session, sample_contract(locked_last=True))
    locked = delivery.project_segments(session, proj)[-1]
    assert locked.locked is True
    c = editor.add_comment(
        session,
        proj,
        locked,
        author="Ana",
        role="Translator",
        body="returned as dispatched -- why?",
    )
    assert c.id is not None
    try:
        editor.patch_target(session, proj, locked, "x")
        assert False, "patch_target must still raise on a locked segment"
    except editor.EditError:
        pass


# --- outbound: only shared, non-inbound comments cross back (§7.5) -------------
def test_shared_vs_unshared_and_reply_ref(session):
    proj = intake.store_dispatch(session, _with_query())
    seg = delivery.project_segments(session, proj)[0]
    editor.add_comment(
        session,
        proj,
        seg,
        author="Ana",
        role="Translator",
        body="Kept the token.",
        share=True,
        external_ref="42",
    )
    editor.add_comment(
        session, proj, seg, author="Ana", role="Translator", body="internal note", share=False
    )
    payload = delivery.build_return_payload(session, proj, [seg.contract_id])
    comments = payload["segments"][0].get("comments") or []
    assert len(comments) == 1
    assert comments[0]["reply_to"] == 42 and comments[0]["body"] == "Kept the token."
    # a standalone shared comment (no external_ref) -> reply_to null (opens a new thread).
    seg2 = delivery.project_segments(session, proj)[1]
    editor.add_comment(
        session, proj, seg2, author="Ana", role="Translator", body="new question", share=True
    )
    payload2 = delivery.build_return_payload(session, proj, [seg2.contract_id])
    assert payload2["segments"][0]["comments"][0]["reply_to"] is None


# --- M6/L5: consented drops are disclosed, malformed replies are guarded --------
def test_over_cap_shared_comments_are_disclosed_not_dropped(session):
    """Gate M6: >10 consented shared comments on a segment ship the first 10 (the buyer's
    return cap) but DECLARE comments_omitted so the drop is never silent."""
    proj = intake.store_dispatch(session, sample_contract())
    seg = delivery.project_segments(session, proj)[0]
    for k in range(13):
        editor.add_comment(
            session, proj, seg, author="Ana", role="Translator", body=f"shared {k}", share=True
        )
    payload = delivery.build_return_payload(session, proj, [seg.contract_id])
    entry = payload["segments"][0]
    assert len(entry["comments"]) == 10, entry
    assert entry.get("comments_truncated") is True and entry.get("comments_omitted") == 3, entry


def test_malformed_reply_ref_is_not_silently_new_threaded(session):
    """Gate L5: a shared comment whose external_ref is set but NON-NUMERIC is a malformed
    reply -- it is DROPPED (and disclosed via comments_omitted), never shipped as a new thread
    with reply_to=null."""
    from vstudio.models import VComment

    proj = intake.store_dispatch(session, sample_contract())
    seg = delivery.project_segments(session, proj)[0]
    # a good reply, and a malformed one (non-numeric external_ref).
    editor.add_comment(
        session,
        proj,
        seg,
        author="Ana",
        role="Translator",
        body="good reply",
        share=True,
        external_ref="42",
    )
    session.add(
        VComment(
            segment_id=seg.id,
            author="Ana",
            role="Translator",
            body="malformed reply",
            direction="local",
            share=True,
            external_ref="not-a-number",
        )
    )
    session.commit()
    payload = delivery.build_return_payload(session, proj, [seg.contract_id])
    entry = payload["segments"][0]
    bodies = [c["body"] for c in entry["comments"]]
    assert "good reply" in bodies and "malformed reply" not in bodies, entry
    assert entry.get("comments_omitted") == 1, (
        "the malformed reply must be disclosed, not silently new-threaded",
        entry,
    )
    assert entry["comments"][0]["reply_to"] == 42


# --- MED-6: the additive-column shim adds the three columns on boot ------------
def test_vs_shim_adds_new_columns(env):
    """A VS DB built from the pre-change model set gains direction / external_ref / share on
    boot via db._ensure_columns. Simulated by DROPPING the columns from the live table, then
    re-running init_db (the generic shim must re-ADD them)."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text
    from sqlmodel import select
    from vstudio import db as vdb

    eng = vdb.engine()
    # Rebuild vcomment WITHOUT the three Wave-3 columns (pre-change shape), with a row.
    with eng.begin() as con:
        con.execute(text("DROP TABLE IF EXISTS vcomment"))
        con.execute(
            text(
                "CREATE TABLE vcomment (id INTEGER PRIMARY KEY, segment_id INTEGER, "
                "author VARCHAR, role VARCHAR, body VARCHAR, created_at DATETIME)"
            )
        )
        con.execute(
            text(
                "INSERT INTO vcomment (id, segment_id, author, role, body) "
                "VALUES (1, 1, 'Ana', 'Translator', 'old row')"
            )
        )
    cols = {c["name"] for c in sa_inspect(eng).get_columns("vcomment")}
    assert "direction" not in cols and "share" not in cols and "external_ref" not in cols
    # Boot the shim.
    vdb._ensure_columns(eng)
    cols2 = {c["name"] for c in sa_inspect(eng).get_columns("vcomment")}
    assert {"direction", "external_ref", "share"} <= cols2
    # The pre-existing row now reads the shim's type-generic NOT NULL defaults (empty string /
    # 0 / NULL), never a 500. `direction` comes back "" (a non-'in' value), which every
    # predicate here treats identically to the model default "local" -- only 'in' is special-
    # cased, so a pre-Wave-3 comment correctly reads as a local, unshared note.
    with eng.begin() as con:
        row = con.execute(
            text("SELECT direction, external_ref, share FROM vcomment WHERE id=1")
        ).fetchone()
    assert row[0] in ("", "local") and row[0] != "in"
    assert row[2] in (0, False) and row[1] is None
    # A NEW row inserted through the ORM gets the MODEL default "local".
    with vdb.get_session() as s:
        s.add(VComment(segment_id=1, author="Ana", role="Translator", body="fresh"))
        s.commit()
        fresh = s.exec(select(VComment).where(VComment.body == "fresh")).first()
        assert fresh.direction == "local" and fresh.share is False
