"""Nullability self-heal verification (real-DB drift incident).

FOLLOW-UP (C1 availability fix): a review found _rebuild_table_nullable's post-rebuild
PRAGMA foreign_key_check ran with NO table argument, scanning the WHOLE database. On a
real DB that was never FK-validated, an unrelated pre-existing orphan ANYWHERE would make
that whole-DB check non-empty, so _rebuild_table_nullable would raise -> _ensure_nullability
would raise -> init_db() would raise -> the app fails to boot, even though the rebuild
itself (scoped to glossaryentry) was perfectly fine. Two fixes, both covered below:
  (1) the post-rebuild check is now scoped: PRAGMA foreign_key_check("<table>") — only the
      rebuilt table's own FK rows are in the blast radius.
  (2) _ensure_nullability now catches a per-table heal failure, logs a warning, and lets
      init_db() continue (non-fatal to boot) instead of propagating — defense in depth for
      any OTHER way a rebuild could fail (e.g. a genuine glossaryentry-local orphan).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider).
ZERO live calls.

BACKGROUND — the incident this closes: the maintainer's real SQLite DB was created by an OLD
create_all() and has NEVER been under Alembic (no alembic_version). It is kept in sync
ONLY by app/db.py's shim (_ensure_columns = ALTER TABLE ADD COLUMN, _ensure_indexes).
The shim can ADD a column but CANNOT change an existing column's nullability, so the
Alembic revision that made glossaryentry.project_id NULLABLE for module-owned entries
(d2e6b8c4a1f7) NEVER reached that DB. There, project_id is still NOT NULL, so the FIRST
module-owned glossary term add (glossary_module.add_term -> project_id=None) dies with
`IntegrityError: NOT NULL constraint failed: glossaryentry.project_id` -- even though the
model declares it Optional. app/db.py's new _ensure_nullability() self-heals this via a
faithful SQLite table rebuild.

A fresh create_all() DB CANNOT reproduce the bug (the model's Optional already makes the
column nullable there), so this suite CONSTRUCTS the drifted NOT NULL table explicitly to
model the real DB, proves the module add fails WITHOUT the fix, then proves
_ensure_nullability() heals it while preserving all data/indexes/FKs.

Covers (the task's a-g):
  (a) project_id becomes nullable (PRAGMA table_info notnull=0) after the fix
  (b) a pre-existing legacy row (project_id set, glossary_id NULL) survives intact
  (c) all pre-existing indexes still exist
  (d) a NEW module-owned entry (project_id NULL, glossary_id set) now INSERTs -- the
      exact operation that failed in prod, end-to-end via gm.create + gm.add_term
  (e) the FK is still enforced after the rebuild (a bogus project_id is rejected)
  (f) idempotent: a second run is a no-op and leaves the data unchanged
  (g) the benign reverse-direction columns (other NOT NULL columns) are NOT modified
  plus: the pre-fix failure proof, and a fresh-DB no-op check.

Final line: "NULLABILITY SELFHEAL OK".
"""

import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_nullheal_")

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import db
from app.db import engine, init_db
from app.models import GlossaryEntry
from app.services import content
from app.services import glossary_module as gm


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# The exact drifted shape of the real DB: glossaryentry with project_id NOT NULL, the
# shim-added glossary_id column present, both FKs, matching the create_all() DDL byte for
# byte EXCEPT for the NOT NULL on project_id (the sole schema divergence the incident
# found where the real DB is STRICTER than the model).
_NOTNULL_DDL = """
CREATE TABLE glossaryentry (
    id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    glossary_id INTEGER,
    source_locale VARCHAR NOT NULL,
    target_locale VARCHAR NOT NULL,
    source_term VARCHAR NOT NULL,
    target_term VARCHAR NOT NULL,
    dnt BOOLEAN NOT NULL,
    case_sensitive BOOLEAN NOT NULL,
    meta JSON,
    concept_id INTEGER,
    term_id INTEGER,
    target_term_id INTEGER,
    meta_overrides JSON,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES project (id),
    FOREIGN KEY(glossary_id) REFERENCES glossary (id)
)
"""
# NOTE: concept_id / term_id are the SPEC-terminology-backend T1 link columns; target_term_id /
# meta_overrides are the T1b LAW-TERM-18 per-pair binding columns (§A.1). On the real
# shim-maintained DB they are all added FK-LESS by _ensure_columns (ALTER ADD COLUMN cannot add a
# FK), so the drift shape carries them as plain INTEGER / JSON columns -- present so ORM
# reads/writes and the nullability rebuild preserve them, matching app/db._ADDED_COLUMNS
# (which must list target_term_id/meta_overrides for glossaryentry, r7-11).


def _table_info(table):
    """{column_name: notnull_bool} from PRAGMA table_info."""
    with engine.connect() as c:
        rows = c.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
    return {r[1]: bool(r[3]) for r in rows}


def _index_names(table):
    with engine.connect() as c:
        rows = c.exec_driver_sql(f'PRAGMA index_list("{table}")').fetchall()
    return {r[1] for r in rows}


def _install_drifted_db():
    """Build the real-DB drift: a project + a module glossary + a NOT NULL glossaryentry
    holding a pre-existing LEGACY row (project_id set, glossary_id NULL) and its index.

    Returns (project_id, glossary_id). The project/glossary live in their own tables
    (untouched by the glossaryentry swap), so we create them FIRST, then drop+recreate
    glossaryentry in the strict shape and seed the legacy row against the real project."""
    init_db()  # modern schema (project_id nullable) + Default org
    with Session(engine) as s:
        proj = content.project_create(s, "Drift P", source_locale="en-US", target_locales="es-ES")
        pid = proj.id
        g = gm.create(s, "Drift GL", created_by="lin@x.com")
        gid = g.id

    # Swap glossaryentry for the strict (NOT NULL) shape + its index, then seed a legacy
    # row. glossaryentry is empty at this point (nothing has written it yet), so the DROP
    # is clean even with FK enforcement on.
    with engine.begin() as c:
        c.exec_driver_sql("DROP TABLE glossaryentry")
        c.exec_driver_sql(_NOTNULL_DDL)
        c.exec_driver_sql(
            "CREATE INDEX ix_glossaryentry_glossary_id ON glossaryentry (glossary_id)"
        )
        c.exec_driver_sql(
            "INSERT INTO glossaryentry "
            "(project_id, glossary_id, source_locale, target_locale, "
            " source_term, target_term, dnt, case_sensitive) "
            f"VALUES ({pid}, NULL, 'en-US', 'es-ES', "
            "'legacyterm', 'legacytrad', 0, 0)"
        )
    return pid, gid


def _insert_with_fk_off(sql):
    """Insert a row that violates FK enforcement (models a real DB's pre-existing drift,
    accumulated before the app ever ran with foreign_keys=ON). Mirrors the exact
    autocommit-PRAGMA + explicit BEGIN/COMMIT pattern app/db.py's own rebuild uses (PRAGMA
    foreign_keys is a no-op inside a transaction), and restores FK ON + isolation_level in
    a finally so this test helper leaves the connection pool in the same clean state the
    app itself requires."""
    raw = engine.raw_connection()
    dbapi_conn = getattr(raw, "driver_connection", None) or raw.dbapi_connection
    prev_iso = getattr(dbapi_conn, "isolation_level", "")
    cur = raw.cursor()
    try:
        dbapi_conn.isolation_level = None
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN")
        cur.execute(sql)
        cur.execute("COMMIT")
    finally:
        try:  # noqa: SIM105
            cur.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        try:  # noqa: SIM105
            dbapi_conn.isolation_level = prev_iso
        except Exception:
            pass
        cur.close()
        raw.close()


class _ListHandler(logging.Handler):
    """Captures log records in-process (no pytest caplog fixture here — this suite is a
    standalone script run as its own subprocess, see test_verify_suites.py)."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_unrelated_table_orphan_does_not_block_heal():
    """(a) An orphan in an UNRELATED table (segment.document_id dangling) must NOT abort
    the glossaryentry heal or crash boot. This is the exact C1 scenario: a real DB that
    was never FK-validated plausibly has drift somewhere unrelated to the table being
    healed; only that table's own FK rows are the rebuild's blast radius."""
    _pid, _gid = _install_drifted_db()

    # Plant a genuine dangling FK in `segment` (document_id -> a document.id that does not
    # exist). Inserted with FK enforcement off, exactly how such drift would have
    # accumulated on a real DB before every connection enforced foreign_keys=ON.
    _insert_with_fk_off(
        "INSERT INTO segment (document_id, target_locale, order_index, source_text, "
        "target_text, status) VALUES (999999, 'es-ES', 0, 'src', 'tgt', 'new')"
    )

    # --- PROVE PRE-FIX WOULD FAIL: an unscoped (whole-DB) foreign_key_check on THIS exact
    # DB state is non-empty -- that is precisely what the old table-less
    # `PRAGMA foreign_key_check` in _rebuild_table_nullable would have seen mid-rebuild,
    # which would have raised and aborted the (unrelated) glossaryentry heal. -----------
    with engine.connect() as c:
        whole_db_violations = c.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    assert whole_db_violations, (
        "expected the unrelated segment orphan to trip an UNSCOPED foreign_key_check "
        "(this proves the pre-fix table-less check would have aborted the heal)"
    )
    print(
        "  pre-fix proof: unscoped PRAGMA foreign_key_check flags the unrelated orphan "
        f"({len(whole_db_violations)} row(s)) -- old code would have raised here"
    )

    # --- POST-FIX: the actual (scoped) self-heal succeeds despite that orphan. ----------
    db._ensure_nullability()
    assert _table_info("glossaryentry")["project_id"] is False, (
        "post-fix: glossaryentry.project_id must still heal to nullable"
    )
    print(
        "  post-fix: _ensure_nullability succeeds; glossaryentry healed "
        "despite the unrelated segment orphan"
    )

    # The unrelated orphan is untouched (still there, still dangling) -- it was never in
    # scope and the fix must not silently "fix" or delete it either.
    with engine.connect() as c:
        seg_rows = c.exec_driver_sql(
            "SELECT document_id FROM segment WHERE document_id=999999"
        ).fetchall()
    assert seg_rows and seg_rows[0][0] == 999999, (
        "unrelated orphan segment row should be left exactly as-is"
    )
    print("  unrelated segment orphan left untouched (irrelevant to this heal)")

    # init_db() itself (the actual boot path) must not crash either.
    init_db()
    print("  init_db() completes without raising: boot does not crash")


def test_glossaryentry_local_orphan_is_non_fatal():
    """(b) A dangling FK INSIDE glossaryentry itself (project_id pointing at a project
    that does not exist) makes the scoped post-rebuild check legitimately fail. That
    failure must be caught by _ensure_nullability, logged, and non-fatal: the DB is left
    intact (the in-rebuild ROLLBACK already guarantees this) and unhealed (still NOT
    NULL), and init_db() must still return instead of crashing the app."""
    _pid, _gid = _install_drifted_db()

    # Plant a glossaryentry-LOCAL orphan: project_id references a project that does not
    # exist. Inserted with FK enforcement off (models drift already present in the table
    # being rebuilt, the case the scoped check IS supposed to still catch).
    _insert_with_fk_off(
        "INSERT INTO glossaryentry (project_id, glossary_id, source_locale, "
        "target_locale, source_term, target_term, dnt, case_sensitive) "
        "VALUES (888888, NULL, 'en-US', 'es-ES', 'orphanterm', 'orphantrad', 0, 0)"
    )

    with engine.connect() as c:
        before_rows = sorted(
            c.exec_driver_sql(
                "SELECT id, project_id, glossary_id, source_term FROM glossaryentry"
            ).fetchall()
        )

    handler = _ListHandler()
    handler.setLevel(logging.WARNING)
    db.log.addHandler(handler)
    try:
        db._ensure_nullability()  # must NOT raise
    finally:
        db.log.removeHandler(handler)
    print("  _ensure_nullability did not raise despite a glossaryentry-local orphan")

    # A warning was logged (not swallowed silently).
    assert handler.records, "a heal failure must be logged, not swallowed silently"
    msg = handler.records[0].getMessage()
    assert "glossaryentry" in msg and handler.records[0].levelno == logging.WARNING, (
        "expected a WARNING mentioning glossaryentry",
        msg,
    )
    print(f"  warning logged: {msg!r}")

    # DB left intact + UNHEALED: project_id is still NOT NULL (rebuild rolled back).
    assert _table_info("glossaryentry")["project_id"] is True, (
        "a failed heal must leave the column unhealed (rollback), not half-applied"
    )
    print("  glossaryentry.project_id still NOT NULL: rollback left the schema unhealed")

    # Data unchanged (both the legacy row and the orphan row survive byte-for-byte).
    with engine.connect() as c:
        after_rows = sorted(
            c.exec_driver_sql(
                "SELECT id, project_id, glossary_id, source_term FROM glossaryentry"
            ).fetchall()
        )
    assert before_rows == after_rows, (
        "data changed by a failed/rolled-back heal",
        before_rows,
        after_rows,
    )
    print("  data intact: legacy row + orphan row both byte-for-byte unchanged")

    # FK enforcement is restored ON for the next connection out of the pool (the
    # existing finally-restore this fix must not weaken).
    with engine.connect() as c:
        fk_state = c.exec_driver_sql("PRAGMA foreign_keys").fetchone()[0]
    assert fk_state == 1, "foreign_keys must be restored ON after a failed heal"
    print("  foreign_keys restored ON after the failed heal")

    # init_db() itself must still return (non-fatal to boot), not propagate.
    init_db()
    print(
        "  init_db() completes without raising: boot does not crash "
        "(feature stays degraded, not fatal)"
    )


def test_fresh_db_is_noop():
    """A fresh create_all() DB already has project_id NULLABLE (the model is Optional),
    so _ensure_nullability must be a pure no-op — run it twice, no error, still nullable.
    This is the fresh-create / CI path: byte-identical to before the fix existed."""
    init_db()
    assert _table_info("glossaryentry")["project_id"] is False, (
        "fresh create_all should already make project_id nullable"
    )
    db._ensure_nullability()
    db._ensure_nullability()
    assert _table_info("glossaryentry")["project_id"] is False
    print("  fresh DB: project_id already nullable, _ensure_nullability is a no-op")


def test_selfheal_end_to_end():
    pid, gid = _install_drifted_db()

    # Precondition: the drift is real — project_id is NOT NULL.
    before = _table_info("glossaryentry")
    assert before["project_id"] is True, (
        "precondition: constructed table must have project_id NOT NULL"
    )
    print("  drift installed: glossaryentry.project_id is NOT NULL (real-DB shape)")

    # --- PRE-FIX PROOF: the exact prod operation fails on the strict table -----------
    with Session(engine) as s:
        raised = False
        try:
            gm.add_term(
                s, gid, "hola", target_term="hello", source_locale="en-US", target_locale="es-ES"
            )
        except IntegrityError as e:
            raised = True
            assert "NOT NULL" in str(e), ("expected a NOT NULL failure", str(e))
            s.rollback()
        if not raised:
            _fail(
                "PRE-FIX: module add_term should raise IntegrityError on NOT NULL "
                "project_id, but it succeeded"
            )
    print("  pre-fix: module add_term raises NOT NULL IntegrityError (the prod bug)")

    # --- APPLY THE FIX ---------------------------------------------------------------
    db._ensure_nullability()

    # (a) project_id is now nullable.
    after = _table_info("glossaryentry")
    assert after["project_id"] is False, "post-fix: project_id must be nullable"
    # (g) benign reverse-direction columns are UNCHANGED (still NOT NULL) — the fix only
    #     widened the one targeted column, nothing else.
    for col in (
        "id",
        "source_locale",
        "target_locale",
        "source_term",
        "target_term",
        "dnt",
        "case_sensitive",
    ):
        assert after[col] is True, f"post-fix: {col} must stay NOT NULL (not widened)"
    assert set(after) == set(before), "column set changed during rebuild"
    print("  post-fix: project_id nullable; every other column's nullability preserved")

    # (b) the pre-existing legacy row survived intact.
    with Session(engine) as s:
        legacy = s.exec(
            select(GlossaryEntry).where(GlossaryEntry.source_term == "legacyterm")
        ).one()
        assert (
            legacy.project_id == pid
            and legacy.glossary_id is None
            and legacy.target_term == "legacytrad"
            and legacy.source_locale == "en-US"
            and legacy.target_locale == "es-ES"
        ), ("legacy row not preserved", legacy)
    print("  post-fix: pre-existing legacy row preserved byte-for-byte")

    # (c) the pre-existing index survived the rebuild.
    assert "ix_glossaryentry_glossary_id" in _index_names("glossaryentry"), (
        "pre-existing index dropped by the rebuild"
    )
    print("  post-fix: ix_glossaryentry_glossary_id preserved")

    # (d) the exact prod operation now succeeds end-to-end (module-owned entry, NULL
    #     project_id, glossary_id set).
    with Session(engine) as s:
        entry = gm.add_term(
            s, gid, "hola", target_term="hello", source_locale="en-US", target_locale="es-ES"
        )
        assert entry is not None and entry.project_id is None and entry.glossary_id == gid, (
            "module add_term should now succeed with project_id NULL",
            entry,
        )
    print("  post-fix: module add_term inserts (project_id NULL, glossary_id set): OK")

    # (e) the FK is STILL enforced after the rebuild — a dangling project_id is rejected.
    with Session(engine) as s:
        raised = False
        try:
            s.add(
                GlossaryEntry(
                    project_id=999999,
                    glossary_id=None,
                    source_locale="en-US",
                    target_locale="es-ES",
                    source_term="bogus",
                    target_term="z",
                )
            )
            s.commit()
        except IntegrityError:
            raised = True
            s.rollback()
        if not raised:
            _fail("post-fix: FK not enforced — a dangling project_id was accepted")
    print("  post-fix: FK to project(id) still enforced (bogus project_id rejected)")

    # (f) idempotent: a second run rebuilds NOTHING and leaves data untouched.
    with Session(engine) as s:
        rows_before = sorted(
            (r.id, r.project_id, r.glossary_id, r.source_term)
            for r in s.exec(select(GlossaryEntry)).all()
        )
    db._ensure_nullability()
    assert _table_info("glossaryentry")["project_id"] is False
    with Session(engine) as s:
        rows_after = sorted(
            (r.id, r.project_id, r.glossary_id, r.source_term)
            for r in s.exec(select(GlossaryEntry)).all()
        )
    assert rows_before == rows_after, ("idempotent re-run changed data", rows_before, rows_after)
    assert "ix_glossaryentry_glossary_id" in _index_names("glossaryentry")
    print("  idempotent: second _ensure_nullability run is a no-op, data unchanged")


def main():
    test_fresh_db_is_noop()
    test_selfheal_end_to_end()
    test_unrelated_table_orphan_does_not_block_heal()
    test_glossaryentry_local_orphan_is_non_fatal()
    print("NULLABILITY SELFHEAL OK")


if __name__ == "__main__":
    main()
