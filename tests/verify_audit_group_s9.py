"""S9 WP-AUDIT: the audit group append does less under the locks, and the chain is unchanged.

The group append (app/services/audit.py::_append_group_locked) holds the cross-process audit lock,
so every statement and every Python object it builds is paid by every appender of every process.
S9 cut that work without touching the chain format. This suite pins:

- the locked section builds NO SQLModel AuditLog object (the rows are plain column values, the
  per-event part is prepared in _Event before any lock);
- the statements per group are fixed whatever the group size: (the Postgres advisory lock,) ONE
  combined read, ONE INSERT for all rows, ONE anchor UPDATE; the org lookup is one statement;
- every stored column is exactly what the model would have stored: each row_hash recomputed from the
  stored columns with the canonical _compute_hash matches, prev_hash links event by event, the
  anchor and the witness name the group head, verify_chain passes;
- awkward values round trip (quotes, braces, backslashes, the word NULL, commas, unicode, a
  100 KB detail, empty strings, customer and org ids set and unset);
- the ids a statement returns are matched to their rows by row_hash, so a database that returns the
  rows in another order, too few rows, a duplicate or an unknown hash can never link a head to the
  wrong row (the group is refused and rewritten event by event);
- a SQLite without RETURNING (older than 3.35) takes the per-row path and writes the same chain;
- the audit-trail test seam: a stage leaves its rows in session.info["audit_group_rows"].
"""

import os
import sys
import types
from itertools import pairwise

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_audit_s9_")

from sqlalchemy import event
from sqlmodel import Session, select

from app import db
from app.db import engine, init_db
from app.models import AuditLog, Customer, Organization, Setting, User
from app.services import audit, audit_state, settings
from tests._stmt_counter import StatementCounter

init_db()
settings.set_many({"audit_enabled": "1"})
PG = engine.dialect.name == "postgresql"

_AWKWARD = [
    "",
    "plain",
    "quo\"te and 'single'",
    "{curly,braces}, comma",
    "back\\slash \\\\ double",
    "NULL",
    "null",
    "café 中文 \U0001f600",
    "line\nbreak\ttab",
    "x" * 100_000,
]


def _rows(after=0):
    with Session(engine) as s:
        return s.exec(select(AuditLog).where(AuditLog.id > after).order_by(AuditLog.id)).all()


def _head_id():
    with Session(engine) as s:
        return s.exec(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1)).first() or 0


def _events(n, prefix, **kw):
    return [
        audit._Event(
            kw.get("actor", "s9@example.com"), f"{prefix}.row", f"t{i}", "", "", None, None
        )
        for i in range(n)
    ]


def _assert_chain(rows, before_hash):
    """Every stored row hashes (with the canonical function) to its row_hash and links to its
    predecessor; the first links to `before_hash`."""
    prev = before_hash
    for r in rows:
        assert r.prev_hash == prev, (r.id, "prev_hash does not link")
        again = audit._compute_hash(
            r.prev_hash,
            r.ts.isoformat(),
            r.actor,
            r.action,
            r.target,
            r.detail,
            r.ip,
            customer_id=r.customer_id,
        )
        assert again == r.row_hash, (
            r.id,
            "row_hash is not the canonical hash of the stored columns",
        )
        prev = r.row_hash
    for a, b in pairwise(rows):
        assert b.id > a.id and b.ts >= a.ts, "ids ascend and ts never goes backwards in link order"


def test_1_no_model_objects_in_the_locked_section():
    built = []

    def seen(target, args, kwargs):
        built.append(1)

    event.listen(AuditLog, "init", seen)
    try:
        audit._write_group(_events(5, "nomodel"))
    finally:
        event.remove(AuditLog, "init", seen)
    assert built == [], f"{len(built)} AuditLog objects were built by a group write"
    print("  [1] a group write builds no SQLModel AuditLog object")


def test_2_statements_per_group():
    counter = StatementCounter(engine)
    for n in (1, 8, 64):
        group = _events(n, f"stmts{n}")
        with counter.measure() as m:
            audit._write_group(group)
        assert {e.outcome for e in group} == {"ok"}
        shape = list(m.by_statement)
        want = 4 if PG else 3
        assert m.total == want, (n, m.total, shape)
        starts = [s.split(" FROM ")[0][:40] for s in shape]
        assert any(s.startswith("INSERT INTO auditlog") for s in shape), shape
        assert sum(s.startswith("INSERT") for s in shape) == 1, ("one INSERT for the group", shape)
        assert sum(s.startswith("UPDATE setting") for s in shape) == 1, shape
        assert sum(s.startswith("SELECT (SELECT auditlog.id") for s in shape) == 1, starts
    # the per-row org lookup is a single statement
    with counter.measure() as m:
        assert audit._actor_org_id("someone@example.com") is None
    assert m.total == 1, m.by_statement
    print(f"  [2] a group of 1, 8 or 64 rows costs {want} statements; the org lookup costs 1")


def test_3_rows_are_what_the_model_would_store():
    with Session(engine) as s:
        org = Organization(name="S9 org")
        c1, c2 = Customer(name="S9 customer one"), Customer(name="S9 customer two")
        s.add_all([org, c1, c2])
        s.commit()
        org_id, customers = org.id, (None, c1.id, c2.id)
        s.add(User(email="member@example.com", name="M", org_id=org_id, password_hash="x"))
        s.commit()
    before = _head_id()
    with Session(engine) as s:
        before_hash = s.exec(select(AuditLog.row_hash).where(AuditLog.id == before)).first() or ""
    group = []
    for i, text in enumerate(_AWKWARD):
        group.append(
            audit._Event(
                "Member@Example.com" if i % 2 else "",
                f"act.{i}",
                text,
                text,
                f"10.0.0.{i}",
                customers[i % 3],
                (org_id, None)[i % 2],
            )
        )
    audit._write_group(group)
    rows = _rows(before)
    assert len(rows) == len(_AWKWARD) and {e.outcome for e in group} == {"ok"}
    _assert_chain(rows, before_hash)
    for e, r in zip(group, rows, strict=True):
        assert (r.actor, r.action, r.target, r.detail, r.ip) == (
            e.actor,
            e.action,
            e.target,
            e.detail,
            e.ip,
        )
        assert (r.customer_id, r.org_id) == (e.customer_id, e.org_id)
    assert rows[0].actor == "system" and rows[0].detail == "" and rows[0].customer_id is None
    assert rows[1].customer_id == customers[1] and rows[0].org_id == org_id
    with Session(engine) as s:
        anchor = s.get(Setting, audit._ANCHOR_KEY).value
    assert anchor.startswith(f"{rows[-1].id}:{rows[-1].row_hash}:"), "anchor names the group head"
    assert audit_state.read()["id"] == rows[-1].id, "the witness names the group head"
    assert audit.verify_chain()[0]
    # through log(): the org stamp comes from the actor's account, read per row
    audit.log(actor="member@example.com", action="stamp.org", customer_id=customers[2])
    last = _rows(rows[-1].id)[0]
    assert last.org_id == org_id and last.customer_id == customers[2]
    assert audit.verify_chain()[0]
    print("  [3] every column and hash equals the canonical row; anchor, witness and chain agree")


class _FakeConn:
    """Returns what a database could legally return for the INSERT, to drive _insert_rows."""

    def __init__(self, returned):
        self.dialect = types.SimpleNamespace(name="sqlite", insert_returning=True)
        self._returned = returned

    def execute(self, stmt, rows):
        return types.SimpleNamespace(all=lambda: self._returned(rows))

    exec_driver_sql = execute


def test_4_ids_pair_with_rows_by_hash():
    rows = [{"row_hash": f"h{i}"} for i in range(5)]

    ids = audit._insert_rows(
        _FakeConn(lambda rs: [(10 + i, r["row_hash"]) for i, r in reversed(list(enumerate(rs)))]),
        rows,
    )
    assert ids == [10, 11, 12, 13, 14], ids

    def refuse(returned, why):
        try:
            audit._insert_rows(_FakeConn(returned), rows)
        except audit._GroupOrderError:
            return
        raise AssertionError(f"{why}: the group was not refused")

    refuse(lambda rs: [(1, "h0"), (2, "h1")], "too few rows returned")
    refuse(lambda rs: [(i, "h0") for i in range(5)], "one hash returned five times")
    refuse(lambda rs: [(i, f"h{i}") for i in range(4)] + [(9, "other")], "an unknown hash returned")
    print("  [4] ids are paired with their rows by row_hash; a wrong answer refuses the group")


def test_5_group_refused_then_rewritten_alone():
    real = audit._insert_rows
    calls = {"n": 0}

    def one_bad_answer(conn, rows):
        calls["n"] += 1
        if calls["n"] == 1 and len(rows) > 1:
            raise audit._GroupOrderError("forced")
        return real(conn, rows)

    before = _head_id()
    audit._insert_rows = one_bad_answer
    try:
        group = _events(4, "refused")
        audit._write_group(group)
    finally:
        audit._insert_rows = real
    rows = _rows(before)
    assert len(rows) == 4 and {e.outcome for e in group} == {"ok"}, [e.outcome for e in group]
    assert calls["n"] == 5, calls
    assert audit.verify_chain()[0]
    print("  [5] a refused group is rolled back and every event is rewritten alone")


def test_6_sqlite_without_returning_uses_the_per_row_path():
    if PG:
        print("  [6] (skipped on Postgres)")
        return
    dialect = engine.dialect
    saved = dialect.insert_returning
    dialect.insert_returning = False
    before = _head_id()
    try:
        group = _events(6, "noreturning")
        audit._write_group(group)
    finally:
        dialect.insert_returning = saved
    rows = _rows(before)
    assert len(rows) == 6 and {e.outcome for e in group} == {"ok"}
    assert audit.verify_chain()[0]
    print("  [6] a SQLite without RETURNING writes the same chain one INSERT per row")


def test_7_stage_exposes_its_rows_to_commit_stand_ins():
    real = db.commit_or_shed
    seen = {}

    def peek(session, *, stage=None, **kw):
        if stage is not None:
            stage()
            seen["rows"] = list(session.info.get("audit_group_rows", ()))
            session.rollback()
            return False
        return real(session, stage=stage, **kw)

    group = _events(3, "seam")
    db.commit_or_shed = peek
    try:
        audit._write_group(group)
    finally:
        db.commit_or_shed = real
    assert [r["action"] for r in seen["rows"]] == ["seam.row"] * 3
    assert all({"ts", "prev_hash", "row_hash"} <= set(r) for r in seen["rows"])
    assert {e.outcome for e in group} == {"shed"}
    assert audit.verify_chain()[0]
    print(
        "  [7] a stage leaves its rows in session.info for commit stand-ins; a shed writes nothing"
    )


def main():
    test_1_no_model_objects_in_the_locked_section()
    test_2_statements_per_group()
    test_3_rows_are_what_the_model_would_store()
    test_4_ids_pair_with_rows_by_hash()
    test_5_group_refused_then_rewritten_alone()
    test_6_sqlite_without_returning_uses_the_per_row_path()
    test_7_stage_exposes_its_rows_to_commit_stand_ins()
    print("verify_audit_group_s9: OK")


if __name__ == "__main__":
    main()
