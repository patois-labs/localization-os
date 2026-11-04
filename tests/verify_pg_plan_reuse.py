"""Postgres plan reuse across pooled checkouts (ADR 0109) -- hermetic.

Throwaway LOC_TMS_DATA_DIR, blanked provider keys, zero network.

psycopg 3 drops its prepared statements on every rollback(), and every pooled checkout ends
in one, so before this change Postgres planned every statement of every request.
app/db.py::_plan_reuse_connection_class keeps them across a rollback of a clean transaction.

Covers:
  words      -- app.db._plan_safe: reads, DML, CTEs, SET/LOCK/SAVEPOINT/RELEASE (any case, after
                comments and whitespace) are plain; DDL, ROLLBACK (TO SAVEPOINT), DISCARD,
                DEALLOCATE, TRUNCATE, COMMENT, an empty statement and unknown words are not.
  internals  -- the psycopg internals the fix rests on still exist: PrepareManager.get/clear,
                Connection._prepared, Connection.rollback -> _rollback_gen calling
                self._prepared.clear(), _pipeline, and info.transaction_status. A psycopg
                upgrade that moves them fails here instead of silently restoring re-planning.
  sqlite     -- the SQLite engine is untouched (no do_connect listener; plain sqlite3).
  postgres   -- (also: a rolled-back ALTER followed by SELECT * clears, gate r1 MEDIUM)
                ONLY when LOC_TMS_TEST_PG_URL names a scratch Postgres database (skipped
                otherwise): stock psycopg leaves 0 rows in pg_prepared_statements after pooled
                checkouts; with the fix the statement is prepared and stays prepared across
                checkouts (generic or custom plan reused); a rollback still undoes writes; a
                failed transaction, rolled-back DDL, a rollback to savepoint and an aborted
                executemany pipeline each clear the cache (server side too) and the next
                statements run fine; ALTER on a prepared table's schema stays correct.
"""

import contextlib
import inspect
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_planreuse_")

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import db

PG_URL = os.environ.get("LOC_TMS_TEST_PG_URL") or os.environ.get("LOC_TMS_TARGET_PG_URL") or ""


def test_words():
    plain = [
        b"SELECT 1",
        b"\n  select x from t",
        b"INSERT INTO t VALUES ($1)",
        b"UPDATE t SET a = $1",
        b"DELETE FROM t",
        b"WITH x AS (SELECT 1) SELECT * FROM x",
        b"-- note\nSELECT 1",
        b"/* a\nb */ UPDATE t SET a=1",
        b"SET LOCAL lock_timeout = '5s'",
        b"LOCK TABLE t",
        b"SAVEPOINT sa_savepoint_1",
        b"RELEASE SAVEPOINT sa_savepoint_1",
        b"MERGE INTO t USING s ON true WHEN MATCHED THEN DO NOTHING",
        b"SELECT 1;",
        b"SELECT 1 ;  \n",
    ]
    unsafe = [
        b"CREATE TABLE t (a int)",
        b"DROP TABLE t",
        b"ALTER TABLE t ADD COLUMN b int",
        b"ROLLBACK TO SAVEPOINT sa_savepoint_1",
        b"rollback",
        b"DISCARD ALL",
        b"DEALLOCATE ALL",
        b"TRUNCATE t",
        b"COMMENT ON TABLE t IS 'x'",
        b"",
        b"   ",
        b"-- only a comment",
        b"GRANT SELECT ON t TO x",
        b"(SELECT 1)",
        b"SELECT 1; DROP TABLE t",
        b"UPDATE t SET a = 1;ALTER TABLE t ADD COLUMN b int;",
    ]
    for q in plain:
        assert db._plan_safe(q), q
    for q in unsafe:
        assert not db._plan_safe(q), q
    print(f"  words: {len(plain)} plain and {len(unsafe)} unsafe statements classified OK")


def test_internals():
    import psycopg
    from psycopg import _connection_base
    from psycopg._preparing import PrepareManager

    for name in ("get", "clear", "maintain_gen"):
        assert callable(getattr(PrepareManager, name, None)), name
    assert list(inspect.signature(PrepareManager.get).parameters)[:3] == [
        "self",
        "query",
        "prepare",
    ]
    src = inspect.getsource(_connection_base.BaseConnection._rollback_gen)
    assert "self._prepared.clear()" in src, "rollback no longer clears through _prepared.clear"
    # psycopg also clears MID-transaction after ALTER/DROP/ROLLBACK: the fix must not let that
    # clear drop its unsafe mark (gate r1 MEDIUM), so pin that this path still exists.
    assert "self.clear()" in inspect.getsource(PrepareManager._should_discard)
    assert "transaction_status == IDLE" in src
    init_src = inspect.getsource(_connection_base.BaseConnection.__init__)
    assert "self._prepared" in init_src and "self._pipeline" in init_src
    assert "self._rollback_gen()" in inspect.getsource(psycopg.Connection.rollback)
    cls = db._plan_reuse_connection_class()
    assert issubclass(cls, psycopg.Connection)
    print(f"  internals: psycopg {psycopg.__version__} still has the hooks the fix uses OK")


def test_sqlite_untouched():
    assert db.engine.dialect.name == "sqlite"
    assert len(db.engine.dialect.dispatch.do_connect) == 0, "a do_connect listener on SQLite"
    with db.engine.connect() as cx:
        raw = cx.connection.dbapi_connection
        assert type(raw).__module__.startswith(("sqlite3", "pysqlcipher", "sqlcipher")), type(raw)
    print("  sqlite: no do_connect listener, plain DBAPI connection OK")


# ---------------------------------------------------------------------------------------------
# Postgres (LOC_TMS_TEST_PG_URL only)
# ---------------------------------------------------------------------------------------------
_Q = "SELECT a FROM plan_reuse_t WHERE a = :a"


def _engine(fixed):
    from sqlalchemy import create_engine

    eng = create_engine(PG_URL, pool_size=1, max_overflow=0)
    if fixed:
        db._install_plan_reuse(eng)
    return eng


def _prepared(eng):
    with eng.connect() as cx:
        return [r[0] for r in cx.execute(text("SELECT statement FROM pg_prepared_statements"))]


def _checkouts(eng, n):
    """n separate pooled checkouts, each a read-only transaction that ends in a rollback."""
    for i in range(n):
        with eng.connect() as cx:
            cx.execute(text(_Q), {"a": i}).all()


def _count_ours(eng):
    return sum("plan_reuse_t" in s for s in _prepared(eng))


def test_pg():
    from sqlalchemy import create_engine

    setup = create_engine(PG_URL)
    with setup.begin() as cx:
        cx.execute(text("DROP TABLE IF EXISTS plan_reuse_t"))
        cx.execute(text("CREATE TABLE plan_reuse_t (a int)"))
        cx.execute(text("INSERT INTO plan_reuse_t SELECT generate_series(1, 50)"))
    try:
        # Stock psycopg: re-planned forever.
        stock = _engine(False)
        _checkouts(stock, 12)
        assert _count_ours(stock) == 0, _prepared(stock)
        stock.dispose()

        eng = _engine(True)
        _checkouts(eng, 12)
        assert _count_ours(eng) == 1, _prepared(eng)
        with eng.connect() as cx:
            row = cx.execute(
                text(
                    "SELECT generic_plans + custom_plans FROM pg_prepared_statements"
                    " WHERE statement LIKE '%plan_reuse_t%'"
                )
            ).one()
            assert row[0] >= 7, row  # executed as a prepared statement on later checkouts

        # A rollback still undoes writes.
        with eng.connect() as cx:
            cx.execute(text("INSERT INTO plan_reuse_t VALUES (-1)"))
            cx.rollback()
        with eng.connect() as cx:
            assert cx.execute(text("SELECT count(*) FROM plan_reuse_t WHERE a = -1")).scalar() == 0
        assert _count_ours(eng) == 1, "a clean write rollback must keep the cache"

        # A failed transaction clears (a Parse after the error was never created).
        with eng.connect() as cx:
            with contextlib.suppress(DBAPIError):
                cx.execute(text("SELECT 1/0"))
            cx.rollback()
            assert cx.connection.dbapi_connection._prepared._names == {}
        assert _count_ours(eng) == 0, _prepared(eng)
        _checkouts(eng, 12)
        assert _count_ours(eng) == 1

        # Rolled-back DDL clears.
        with eng.connect() as cx:
            cx.execute(text("CREATE TABLE plan_reuse_tmp (b int)"))
            cx.rollback()
        assert _count_ours(eng) == 0
        _checkouts(eng, 12)

        # A rolled-back ALTER clears even though psycopg itself cleared at the ALTER and the
        # statements after it were prepared against the altered table (gate r1 MEDIUM).
        star = "SELECT * FROM plan_reuse_t WHERE a = :a"
        with eng.connect() as cx:
            cx.execute(text("ALTER TABLE plan_reuse_t ADD COLUMN c int"))
            for i in range(7):
                assert len(cx.execute(text(star), {"a": i}).one_or_none() or (0, 0)) == 2
            cx.rollback()
        assert not any("SELECT *" in st for st in _prepared(eng)), _prepared(eng)
        with eng.connect() as cx:
            assert cx.execute(text(star), {"a": 3}).one() == (3,)
        _checkouts(eng, 12)
        assert _count_ours(eng) == 1

        # A rollback to savepoint (an error swallowed) clears at the outer rollback.
        with eng.connect() as cx:
            cx.execute(text(_Q), {"a": 1}).all()
            sp = cx.begin_nested()
            try:
                cx.execute(text("SELECT 1/0"))
            except DBAPIError:
                sp.rollback()
            cx.execute(text(_Q), {"a": 2}).all()
            cx.rollback()
        assert _count_ours(eng) == 0
        _checkouts(eng, 12)

        # An aborted executemany pipeline clears, and the connection keeps working.
        with eng.connect() as cx:
            rows = [{"a": i} for i in range(6)]
            with contextlib.suppress(DBAPIError):
                cx.execute(text("INSERT INTO plan_reuse_t VALUES (1/(:a - 3))"), rows)
            cx.rollback()
        assert _count_ours(eng) == 0
        _checkouts(eng, 12)
        assert _count_ours(eng) == 1

        # A committed ALTER on the prepared table: results stay right.
        with eng.begin() as cx:
            cx.execute(text("ALTER TABLE plan_reuse_t ADD COLUMN b int"))
        with eng.connect() as cx:
            assert cx.execute(text(_Q), {"a": 5}).all() == [(5,)]
        _checkouts(eng, 6)
        eng.dispose()
    finally:
        with setup.begin() as cx:
            cx.execute(text("DROP TABLE IF EXISTS plan_reuse_t"))
            cx.execute(text("DROP TABLE IF EXISTS plan_reuse_tmp"))
        setup.dispose()
    print(
        "  postgres: plans kept across checkouts; failed/DDL/savepoint/pipeline rollbacks clear OK"
    )


if __name__ == "__main__":
    test_words()
    test_internals()
    test_sqlite_untouched()
    if PG_URL:
        test_pg()
    else:
        print("  postgres: SKIP (set LOC_TMS_TEST_PG_URL to a scratch database)")
    print("PG PLAN REUSE OK")
