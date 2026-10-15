"""KEY-BASED PROJECTS -- THE INERTNESS INVARIANT (SPEC-key-based-projects 10.7).

Wave A shipped the MIGRATION + ENGINE half ([A]-[G]); Wave B added the BEHAVIOUR
half ([H]), which is the claim an existing install actually cares about: with the
surfaces PRESENT BUT UNUSED -- and separately with the kill switch OFF -- a
document project's pages, /api responses and CLI output are byte-identical.

The claim Batch 4 makes to every existing install:

    With the feature never exercised, a document-driven project behaves exactly as it
    did before this batch.

This suite proves the SCHEMA and ENGINE side of that claim -- the parts that exist
before a single surface does.

GROUPS
  [A] THE REVISION ADDS, AND ONLY ADDS. The schema is built twice from scratch --
      once to the PREVIOUS head (a4e7d2c8b91f) and once to the new head -- and the two
      are diffed column by column. The ONLY differences allowed are the additions this
      revision declares (two tables, one project column, two segment columns, five
      indexes). Any nullability change to a PRE-EXISTING column, any dropped index, any
      widened constraint fails here, naming it. This is what "adds columns without
      disturbing nullability parity" means, made mechanical.
  [B] ALL THREE BUILD PATHS AGREE. alembic-head, `create_all` (the hermetic test path)
      and the app/db.py legacy ADD-COLUMN shim must produce the same column names, the
      same nullability, the same COLUMN DEFAULTS (review A13/B15 -- a default present on
      one path only made a raw INSERT fail on a create_all DB and succeed on a real one)
      and the same PARTIAL WHERE clauses on both new unique indexes (review A10 -- the
      gap that let a mutation stripping them stay green everywhere). Otherwise a real
      install and a test install disagree about the schema, which is the exact class of
      bug SPEC-alembic-authority was written about.
  [C] THE REVISION IS REVERSIBLE. `downgrade` returns the schema to the previous head.
  [D] THE ENGINE IS UNREACHABLE WHEN UNUSED. The change-feed listener -- the one piece
      of Wave A that runs inside EVERY flush in the app -- keeps its real registration
      and its real guard, but its BODY is replaced with a function that RAISES; a full
      document-project lifecycle (create, ingest-shaped segment writes, edits, deletes)
      then still completes. Unreached code cannot change behaviour, so this is the
      strongest form of the inertness proof, and it stays live forever.
  [E] DOCUMENT PROJECTS ARE UNTOUCHED BY THE NEW DIMENSION. project_create called the
      pre-Batch-4 way yields project_type "document"; the new segment columns stay
      NULL on every document row; and the partial unique index does not constrain
      document segments (two document segments may share a locale + NULL key, which a
      non-partial index would have refused); and a `Project(...)` built in Python
      defaults to "document" (review A14 -- connectors/store.py and routing.py
      construct Project directly and rely on exactly that).
  [F] THE HOOK IS REGISTERED WHERE EVERY PROCESS SEES IT. A BARE subprocess that
      imports only `app.db` -- as a CLI verb, a worker or an /api process does --
      finds the before_flush hook in sqlalchemy.orm.Session's dispatch, writes a value
      row the orchestrator's way, and the parent then reads that change out of the
      feed. Registering the hook as an import side effect of app/services/keys.py
      (which nothing at boot imports) made "a changelog row no matter who wrote it"
      false in exactly the processes that matter (review A2).
  [G] A DOCUMENT DELETE LOADS NO KEY CODE. `retention.delete_document` runs on every
      document install, and it used to IMPORT app.services.keys unconditionally just to
      ask whether the document was an internal key one -- which armed a process-wide
      `threading.Thread.start` wrapper on installs that have never seen a key project.
      [D] structurally cannot see that (it replaces the listener's body, not the
      import), so this is a subprocess watching its own `sys.modules` and its own
      `Thread.start` (re-review R4) -- plus the opposite assertion, that an INTERNAL
      key document still gets its value tombstones.
  [H] THE BEHAVIOUR HALF (Wave B). A document project is rendered, listed and
      exported through the WEB pages, the /api routes and the CLI three times --
      with the key feature present but never exercised, with a key project alive
      next door, and with the kill switch OFF -- and the three answers are
      compared BYTE FOR BYTE. This is the invariant the whole batch rests on, and
      it is the one a reviewer can break by accident: every listing seam that
      learned to hide internal key documents is a place a document row could be
      hidden too.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env; provider
keys blanked). ASCII-only output. Final line: "KEYS INERTNESS OK".
"""

import os
import re
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_keysinert_")

from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from app import models
from app.db import engine, init_db
from app.models import (
    Document,
    ProjectType,
    Segment,
    SegStatus,
    TransMode,
)
from app.services import content, keys, retention, settings

init_db()

PREV_HEAD = "a4e7d2c8b91f"
NEW_HEAD = "b6f1d0a7c34e"

#: Exactly what revision b6f1d0a7c34e is allowed to add. Anything else in the diff is
#: a failure -- including a column this suite's author forgot to list.
EXPECTED_NEW_TABLES = {"stringkey", "keychangelog"}
EXPECTED_NEW_COLUMNS = {
    ("project", "project_type"),
    ("segment", "key_id"),
    ("segment", "plural_category"),
}
EXPECTED_NEW_INDEXES = {
    ("stringkey", "ix_stringkey_project_id"),
    ("stringkey", "ux_stringkey_identity"),
    ("keychangelog", "ix_keychangelog_project_id"),
    ("keychangelog", "ix_keychangelog_project_cursor"),
    ("segment", "ix_segment_key_id"),
    ("segment", "ux_segment_key_locale_form"),
}

_TMPDIRS: list = []
_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _upgrade(target: str) -> str:
    """`alembic upgrade <target>` against a FRESH temp data dir; returns the URL."""
    from alembic.config import Config

    from alembic import command

    fresh = tempfile.mkdtemp(prefix="loctms_keysinert_db_")
    _TMPDIRS.append(fresh)
    url = "sqlite:///" + os.path.join(fresh, "loc_tms.db").replace("\\", "/")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = fresh
    try:
        command.upgrade(cfg, target)
    finally:
        if prev is not None:
            os.environ["LOC_TMS_DATA_DIR"] = prev
    return url


def _snapshot(url: str) -> dict:
    """{tables, columns: {(table, col): nullable}, defaults: {(table, col): default},
    indexes: {(table, index)}, index_sql: {name: CREATE INDEX text}}."""
    eng = create_engine(url)
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    columns, defaults, indexes, index_sql = {}, {}, set(), {}
    for table in sorted(tables):
        for col in insp.get_columns(table):
            columns[(table, col["name"])] = bool(col["nullable"])
            # DEFAULTS are captured too (review A13/B15): a column default that exists
            # on one build path and not another is invisible to a names+nullability
            # diff, and is exactly the divergence that made a raw INSERT fail on a
            # create_all DB while succeeding on an alembic one.
            defaults[(table, col["name"])] = col.get("default")
    # Indexes come from sqlite_master, NOT inspect().get_indexes(): SQLAlchemy skips
    # reflecting EXPRESSION-based indexes, which is exactly the kind this revision adds
    # (both partial uniques use coalesce()). Reflection would have silently reported
    # them as absent -- a guard blind to the thing it guards.
    with eng.connect() as c:
        for name, tbl, sql in c.execute(
            text(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ).fetchall():
            indexes.add((tbl, name))
            index_sql[name] = sql or ""
    eng.dispose()
    return {
        "tables": tables,
        "columns": columns,
        "defaults": defaults,
        "indexes": indexes,
        "index_sql": index_sql,
    }


#: The PARTIAL predicate each new unique index must carry, normalized to lower case
#: with whitespace collapsed. Asserted on ALL THREE build paths (review A10): without
#: the WHERE clause, `ux_segment_key_locale_form` would forbid a second DOCUMENT
#: segment in the same locale, and `ux_stringkey_identity` would let a soft-deleted key
#: squat on its own name forever. [E2] could not notice either: NULLs are DISTINCT in a
#: SQL unique index, so a non-partial index still permits the document rows it inserts.
PARTIAL_INDEX_WHERE = {
    "ux_stringkey_identity": "where deleted_at is null",
    "ux_segment_key_locale_form": "where key_id is not null",
}


def _norm_sql(sql: str) -> str:
    return " ".join((sql or "").lower().split())


def _assert_partial_wheres(index_sql: dict, path: str) -> None:
    for name, where in PARTIAL_INDEX_WHERE.items():
        sql = _norm_sql(index_sql.get(name, ""))
        assert sql, f"{path}: index {name} does not exist at all"
        assert where in sql, (
            f"{path}: {name} is NOT partial -- its DDL carries no {where!r}:\n  {sql}"
        )


# ============================================================================
# [A] The revision adds, and only adds
# ============================================================================
def test_a_revision_only_adds():
    before = _snapshot(_upgrade(PREV_HEAD))
    # PINNED TO NEW_HEAD, NOT "head". This cell's claim is about ONE revision --
    # what b6f1d0a7c34e adds, and that it adds nothing else. "head" was the same
    # thing only while that revision happened to BE the head; the first revision
    # stacked on top of it (c4b7e1f92a3d, workflowrun.notes) turned this into an
    # assertion about the whole tail of the migration chain and failed it. The
    # later cells that legitimately mean "the current schema" keep using "head".
    after = _snapshot(_upgrade(NEW_HEAD))

    new_tables = after["tables"] - before["tables"]
    gone_tables = before["tables"] - after["tables"]
    assert not gone_tables, f"the revision DROPPED tables: {sorted(gone_tables)}"
    assert new_tables == EXPECTED_NEW_TABLES, f"unexpected table set change: {sorted(new_tables)}"

    new_cols = {
        k
        for k in after["columns"]
        if k not in before["columns"] and k[0] not in EXPECTED_NEW_TABLES
    }
    gone_cols = set(before["columns"]) - set(after["columns"])
    assert not gone_cols, f"the revision DROPPED columns: {sorted(gone_cols)}"
    assert new_cols == EXPECTED_NEW_COLUMNS, (
        f"unexpected new columns on existing tables: {sorted(new_cols)}"
    )

    # THE load-bearing assertion: not one PRE-EXISTING column changed nullability.
    drift = [
        "{}.{}: {} -> {}".format(t, c, before["columns"][(t, c)], after["columns"][(t, c)])
        for (t, c) in sorted(before["columns"])
        if after["columns"].get((t, c)) != before["columns"][(t, c)]
    ]
    assert not drift, "the revision changed EXISTING columns' nullability:\n  {}".format(
        "\n  ".join(drift)
    )

    gone_ix = before["indexes"] - after["indexes"]
    new_ix = {i for i in after["indexes"] - before["indexes"] if i[0] not in EXPECTED_NEW_TABLES}
    assert not gone_ix, f"the revision DROPPED indexes: {sorted(gone_ix)}"
    assert new_ix == {i for i in EXPECTED_NEW_INDEXES if i[0] not in EXPECTED_NEW_TABLES}, (
        f"unexpected new indexes on existing tables: {sorted(new_ix)}"
    )
    print(
        "  [A1] revision %s adds %d tables / %d columns / %d indexes and changes "  # noqa: UP031
        "NOTHING else" % (NEW_HEAD, len(new_tables), len(new_cols), len(new_ix))
    )


def test_a_new_columns_are_as_declared():
    after = _snapshot(_upgrade("head"))
    # project_type is NOT NULL (a value, never an absence); both segment columns are
    # nullable (NULL is what "this is document content" means).
    assert after["columns"][("project", "project_type")] is False
    assert after["columns"][("segment", "key_id")] is True
    assert after["columns"][("segment", "plural_category")] is True
    # ...and the server default backfills every existing row to "document".
    url = _upgrade(PREV_HEAD)
    eng = create_engine(url)
    with eng.connect() as c:
        c.execute(
            text(
                "INSERT INTO project (name, source_locale, default_provider, "
                "provider_mode, status, created_at) VALUES "
                "('legacy', 'en-US', 'claude', 'uniform', 'in_translation', "
                "CURRENT_TIMESTAMP)"
            )
        )
        c.commit()
    eng.dispose()
    print("  [A2] project_type is NOT NULL; both segment columns are nullable")


def test_a_backfill_is_document():
    """A row that existed BEFORE the migration must come out as 'document'. Proven on
    a real pre-migration row, not on a fresh insert."""
    from alembic.config import Config

    from alembic import command

    fresh = tempfile.mkdtemp(prefix="loctms_keysinert_bf_")
    _TMPDIRS.append(fresh)
    url = "sqlite:///" + os.path.join(fresh, "loc_tms.db").replace("\\", "/")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = fresh
    try:
        command.upgrade(cfg, PREV_HEAD)
        eng = create_engine(url)
        with eng.connect() as c:
            c.execute(
                text(
                    "INSERT INTO project (name, source_locale, default_provider,"
                    " provider_mode, status, created_at) VALUES "
                    "('pre-existing', 'en-US', 'claude', 'uniform', "
                    "'in_translation', CURRENT_TIMESTAMP)"
                )
            )
            c.commit()
        eng.dispose()
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        with eng.connect() as c:
            row = c.execute(
                text("SELECT project_type FROM project WHERE name='pre-existing'")
            ).fetchone()
        eng.dispose()
    finally:
        if prev is not None:
            os.environ["LOC_TMS_DATA_DIR"] = prev
    assert row is not None and row[0] == ProjectType.DOCUMENT, row
    print("  [A3] a project row that predates the migration backfills to 'document'")


# ============================================================================
# [B] The three build paths agree
# ============================================================================
def _live_indexes() -> set:
    """(table, index) for the engine this suite runs on -- straight from sqlite_master,
    for the same reason _snapshot does."""
    out = set()
    with engine.connect() as c:
        for name, tbl in c.execute(
            text(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ).fetchall():
            out.add((tbl, name))
    return out


def test_b_build_paths_agree():
    alembic_snap = _snapshot(_upgrade("head"))
    # The create_all path is the engine this suite is already running on (init_db ran
    # at import under config.IS_TEST).
    insp = inspect(engine)
    live_ix = _live_indexes()
    for table in sorted(EXPECTED_NEW_TABLES):
        assert table in insp.get_table_names(), f"create_all did not build {table}"
        create_all_cols = {c["name"]: bool(c["nullable"]) for c in insp.get_columns(table)}
        alembic_cols = {c: n for (t, c), n in alembic_snap["columns"].items() if t == table}
        assert create_all_cols == alembic_cols, (
            f"{table} differs between create_all and alembic:\n  create_all={sorted(create_all_cols.items())}\n  alembic={sorted(alembic_cols.items())}"  # noqa: E501
        )
        ix = {n for (t, n) in live_ix if t == table}
        expected_ix = {n for (t, n) in EXPECTED_NEW_INDEXES if t == table}
        assert expected_ix <= ix, (
            f"{table} missing indexes on create_all: {sorted(expected_ix - ix)}"
        )
    seg_cols = {c["name"]: bool(c["nullable"]) for c in insp.get_columns("segment")}
    assert seg_cols.get("key_id") is True and seg_cols.get("plural_category") is True
    assert {c["name"] for c in insp.get_columns("project")} >= {"project_type"}
    seg_ix = {n for (t, n) in live_ix if t == "segment"}
    assert {"ix_segment_key_id", "ux_segment_key_locale_form"} <= seg_ix, sorted(seg_ix)
    # The legacy ADD-COLUMN shim carries the SAME columns, so a non-alembic SQLite
    # boot lands in the same place.
    from app import db as _db

    assert ("project_type", "VARCHAR NOT NULL DEFAULT 'document'") in _db._ADDED_COLUMNS[
        "project"
    ], _db._ADDED_COLUMNS["project"]
    assert ("key_id", "INTEGER") in _db._ADDED_COLUMNS["segment"]
    assert ("plural_category", "VARCHAR") in _db._ADDED_COLUMNS["segment"]
    assert any(n == "ux_stringkey_identity" for n, _t, _c in _db._UNIQUE_INDEXES)
    assert any(n == "ux_segment_key_locale_form" for n, _t, _c in _db._UNIQUE_INDEXES)
    print("  [B1] alembic, create_all and the legacy shim agree on every new column")


def test_b_project_type_has_a_server_default_everywhere():
    """Review A13/B15. The three paths agreed on names and nullability and DISAGREED on
    the DEFAULT: alembic and the legacy shim wrote `DEFAULT 'document'`, create_all
    wrote nothing (a Python-side default only). A raw INSERT that omits project_type --
    a migration test, a fixture, a psql session -- therefore failed NOT NULL on a
    create_all DB and succeeded on a real one."""
    alembic_snap = _snapshot(_upgrade("head"))
    alembic_default = alembic_snap["defaults"][("project", "project_type")]
    assert alembic_default and "document" in str(alembic_default), alembic_default
    insp = inspect(engine)
    create_all_default = {c["name"]: c.get("default") for c in insp.get_columns("project")}[
        "project_type"
    ]
    assert create_all_default and "document" in str(create_all_default), (
        "create_all built project.project_type with NO server default (alembic has "
        f"{alembic_default!r}) -- the three build paths disagree"
    )
    # ...and the proof that the default is what it is FOR: a raw INSERT that never
    # mentions the column succeeds on the create_all engine.
    with engine.connect() as c:
        c.execute(
            text(
                "INSERT INTO project (name, source_locale, default_provider, "  # noqa: UP031
                "provider_mode, status, created_at, created_by) VALUES "
                "('raw-insert-%d', 'en-US', 'claude', 'uniform', "
                "'in_translation', CURRENT_TIMESTAMP, '')" % _seq()
            )
        )
        c.commit()
        row = c.execute(
            text(
                "SELECT project_type FROM project WHERE name LIKE "
                "'raw-insert-%' ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None and row[0] == ProjectType.DOCUMENT, row
    print("  [B3] project_type carries the same server default on all three paths")


def test_b_partial_index_where_clauses_are_real():
    """Review A10 -- the [E2] gap. NOTHING asserted that either new unique index is
    PARTIAL: deleting `sqlite_where`/`postgresql_where` from models.py and the WHERE
    string from db.py left every suite GREEN, because NULLs are distinct in a SQL
    unique index and so a non-partial index still permits the document rows [E2]
    inserts. This reads the DDL text itself, on ALL THREE build paths."""
    _assert_partial_wheres(_snapshot(_upgrade("head"))["index_sql"], "alembic head")
    with engine.connect() as c:
        live = {
            n: (s or "")
            for n, s in c.execute(
                text(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ).fetchall()
        }
    _assert_partial_wheres(live, "create_all")
    # The legacy ADD-COLUMN shim pastes its column string straight into
    # `CREATE UNIQUE INDEX ... ON {table} {cols}`, so the WHERE has to ride in there.
    from app import db as _db

    shim = {
        name: f"create unique index {name} on {tbl} {cols}"
        for name, tbl, cols in _db._UNIQUE_INDEXES
    }
    _assert_partial_wheres(shim, "legacy ADD-COLUMN shim")
    print("  [B4] both new unique indexes are PARTIAL on all three build paths")


def test_b_models_match_the_migration():
    """The model metadata is the third authority; verify_migrations compares the whole
    schema, this pins THIS revision's tables specifically so a future model edit that
    forgets the migration is caught with a message naming keys."""
    snap = _snapshot(_upgrade("head"))
    for table in sorted(EXPECTED_NEW_TABLES) + ["segment", "project"]:  # noqa: RUF005
        model_cols = {
            c.name: bool(c.nullable) for c in models.SQLModel.metadata.tables[table].columns
        }
        db_cols = {c: n for (t, c), n in snap["columns"].items() if t == table}
        assert model_cols == db_cols, (
            f"{table}: models vs migration mismatch\n  models={sorted(model_cols.items())}\n  alembic={sorted(db_cols.items())}"  # noqa: E501
        )
    print("  [B2] the models and the migration agree exactly on the touched tables")


# ============================================================================
# [C] Reversible
# ============================================================================
def test_c_downgrade_restores_the_previous_head():
    from alembic.config import Config

    from alembic import command

    fresh = tempfile.mkdtemp(prefix="loctms_keysinert_dn_")
    _TMPDIRS.append(fresh)
    url = "sqlite:///" + os.path.join(fresh, "loc_tms.db").replace("\\", "/")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = fresh
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, PREV_HEAD)
    finally:
        if prev is not None:
            os.environ["LOC_TMS_DATA_DIR"] = prev
    after = _snapshot(url)
    reference = _snapshot(_upgrade(PREV_HEAD))
    assert after["tables"] == reference["tables"], "downgrade left tables behind: {}".format(
        sorted(after["tables"] ^ reference["tables"])
    )
    assert set(after["columns"]) == set(reference["columns"]), (
        "downgrade left columns behind: {}".format(
            sorted(set(after["columns"]) ^ set(reference["columns"]))
        )
    )
    print(f"  [C1] the revision downgrades cleanly back to {PREV_HEAD}")


# ============================================================================
# [D] The engine is unreachable when unused
# ============================================================================
def test_d_change_feed_listener_is_unreachable():
    """THE strongest inertness proof: replace the one piece of Wave A that runs inside
    every flush with a function that RAISES, then run a document project's whole
    lifecycle. It completes, so no new code executed at all."""

    def _explode(session, touched, gone, fresh):
        raise AssertionError("the key change-feed listener ran on a document install")

    # The LISTENER stays registered and real -- only its BODY is replaced, so the
    # guard that decides "no key segments here" is the code actually under test.
    real_body = keys._append_segment_changes
    set_pieces("app.services.keys", "_append_segment_changes", _explode)
    try:
        with Session(engine) as s:
            p = content.project_create(s, "Inert-%d" % _seq(), target_locales="es-ES")  # noqa: UP031
            doc = Document(
                project_id=p.id,
                filename="d.md",
                fmt="md",
                mode=TransMode.SEGMENTED,
                skeleton={"units": []},
            )
            s.add(doc)
            s.commit()
            s.refresh(doc)
            for i, txt in enumerate(("One.", "Two.", "Three.")):
                s.add(
                    Segment(
                        document_id=doc.id,
                        target_locale="es-ES",
                        order_index=i,
                        source_text=txt,
                        status=SegStatus.NEW,
                    )
                )
            s.commit()
            rows = s.exec(select(Segment).where(Segment.document_id == doc.id)).all()
            for seg in rows:
                seg.target_text, seg.status = "Uno.", SegStatus.EDITED
                s.add(seg)
            s.commit()
            s.delete(rows[0])
            s.commit()
            assert len(s.exec(select(Segment).where(Segment.document_id == doc.id)).all()) == 2
    finally:
        set_pieces("app.services.keys", "_append_segment_changes", real_body)
    print("  [D1] the change-feed listener never runs on a document-only install")


def test_d_listener_is_back_on():
    """A live check that the previous test restored the world -- an inertness proof
    that silently disarmed the feature would be worse than no proof at all."""
    with Session(engine) as s:
        p = content.project_create(
            s,
            "Armed-%d" % _seq(),  # noqa: UP031
            target_locales="es-ES",
            project_type=ProjectType.KEY_BASED,  # noqa: RUF100, UP031
        )
        k = keys.create_key(s, p, "armed", base_value="Hello")
        keys.set_value(s, k, "es-ES", "Hola", origin=keys.ORIGIN_HUMAN)
        entries = keys.changes_since(s, p.id, 0)["entries"]
        assert any(e["entity"] == "value" and e["op"] == "updated" for e in entries), entries
    print("  [D2] ...and it is armed again afterwards")


# ============================================================================
# [E] Document projects are untouched by the new dimension
# ============================================================================
def test_e_document_projects_are_unchanged():
    with Session(engine) as s:
        # The pre-Batch-4 call, verbatim: no project_type argument at all.
        p = content.project_create(s, "Plain-%d" % _seq(), target_locales="es-ES, fr-FR")  # noqa: UP031
        assert p.project_type == ProjectType.DOCUMENT
        assert keys.is_key_project(p) is False
        doc = Document(
            project_id=p.id,
            filename="d.md",
            fmt="md",
            mode=TransMode.SEGMENTED,
            skeleton={"units": []},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale="es-ES",
                order_index=0,
                source_text="One.",
                status=SegStatus.NEW,
            )
        )
        s.commit()
        seg = s.exec(select(Segment).where(Segment.document_id == doc.id)).first()
        assert seg.key_id is None and seg.plural_category is None, (
            f"a document segment picked up key columns: {seg}"
        )
        assert keys.is_internal_key_document(doc) is False
    print("  [E1] a document project created the old way is 'document' with NULL key columns")


def test_e_partial_index_does_not_constrain_document_rows():
    """`ux_segment_key_locale_form` is (key_id, target_locale, coalesce(plural_category,''))
    -- WITHOUT the partial WHERE it would forbid two document segments in the same
    locale (both have key_id NULL and plural_category NULL). This inserts exactly that
    pair, which a non-partial index would reject."""
    with Session(engine) as s:
        p = content.project_create(s, "Partial-%d" % _seq(), target_locales="es-ES")  # noqa: UP031
        doc = Document(
            project_id=p.id,
            filename="d.md",
            fmt="md",
            mode=TransMode.SEGMENTED,
            skeleton={"units": []},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i in range(5):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text="S%d" % i,  # noqa: UP031
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        rows = s.exec(select(Segment).where(Segment.document_id == doc.id)).all()
        assert len(rows) == 5, rows
    print("  [E2] the partial unique index leaves document segments unconstrained")


def test_e_model_default_is_document():
    """Review A14. Nothing pinned the MODEL-level default: flipping it to KEY_BASED
    left every suite green, while `connectors/store.py` and `routing.py` construct
    `Project(...)` directly and rely on it. Those two callers would have started
    minting key-based projects silently."""
    from app.models import Project as _Project

    assert _Project(name="x").project_type == ProjectType.DOCUMENT, (
        "Project()'s default project_type is not 'document' -- every direct "
        "Project(...) construction (connectors/store.py, routing.py) is affected"
    )
    print("  [E3] a Project constructed in Python defaults to 'document'")


# ============================================================================
# [F] The change-feed hook is registered where EVERY process sees it
# ============================================================================
_PROBE = r"""
import sys
# The whole point: import ONLY app.db, as a CLI verb, a worker or an /api process does.
from app.db import engine
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session
from app.models import Segment, SegStatus

assert "app.services.keys" not in sys.modules, \
    "importing app.db dragged in app.services.keys -- this probe proves nothing"
dispatch = SASession.dispatch.before_flush
names = [getattr(fn, "__name__", "") for fn in dispatch._clslevel.get(SASession, [])]
assert "_key_change_feed" in names, \
    "no key change-feed hook in SASession's before_flush dispatch: %s" % names

seg_id = int(sys.argv[1])
with Session(engine) as s:
    seg = s.get(Segment, seg_id)
    seg.target_text, seg.status = "written by a bare process", SegStatus.MT
    s.add(seg)
    s.commit()
print("PROBE OK")
"""


def test_f_listener_is_registered_in_every_process():
    """Review A2 -- the HIGH one. The hook used to be an import side effect of
    app/services/keys.py, which nothing at app or CLI boot imports: a fresh process
    that imported app.db and wrote Segment.target_text the orchestrator way appended
    NOTHING to the feed. That falsifies "a changelog row no matter who wrote it"
    exactly where it matters most -- the workflow write-back.

    This spawns a BARE subprocess that imports only app.db, asserts the before_flush
    hook is present in sqlalchemy.orm.Session's dispatch (and that keys was NOT
    dragged in), writes a value row, and exits; the parent then reads the feed."""
    import subprocess

    with Session(engine) as s:
        p = content.project_create(
            s,
            "Probe-%d" % _seq(),  # noqa: UP031
            target_locales="es-ES",
            project_type=ProjectType.KEY_BASED,  # noqa: RUF100, UP031
        )
        k = keys.create_key(s, p, "probe.me", base_value="Hello")
        pid, seg_id = p.id, keys.values_for(s, k)[0].id
        before = keys.changes_since(s, pid, 0)["next_cursor"]

    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(seg_id)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    assert proc.returncode == 0, (
        f"the bare-process probe failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr}"
    )
    assert "PROBE OK" in proc.stdout, proc.stdout

    with Session(engine) as s:
        entries = keys.changes_since(s, pid, before)["entries"]
        assert any(
            e["entity"] == "value" and e["op"] == "updated" and e["name"] == "probe.me"
            for e in entries
        ), (
            "a write from a process that never imported app.services.keys produced "
            f"no feed entry: {entries}"
        )
        assert s.get(Segment, seg_id).target_text == "written by a bare process"
    print("  [F1] the change feed is armed in a process that only imports app.db")


# ============================================================================
# [G] A DOCUMENT DELETE DOES NOT INSTALL KEY MACHINERY (re-review R4)
# ============================================================================
_DELETE_PROBE = r"""
import sys, threading
# A pure document install: nothing here has any reason to know keys exist.
from app.db import engine
from app.services import retention

real_start = threading.Thread.start
retention.delete_document(__import__("sqlmodel").Session(engine), int(sys.argv[1]))

assert "app.services.keys" not in sys.modules, \
    "deleting an ORDINARY document imported app.services.keys"
assert not getattr(threading.Thread.start, "_loc_key_wrapped", False), \
    "deleting an ordinary document monkeypatched threading.Thread.start"
assert threading.Thread.start is real_start, \
    "threading.Thread.start was replaced during an ordinary document delete"
print("PROBE OK")
"""


def test_g_document_delete_stays_out_of_the_keys_module():
    """Re-review R4. `retention.delete_document` imported app.services.keys on EVERY
    delete, just to call `is_internal_key_document` -- and importing that module armed
    a process-wide `threading.Thread.start` wrapper. A document-only install therefore
    got key machinery installed behind its back, contradicting app/db.py's own comment
    that a document install never loads the keys module.

    [D] cannot see this: it replaces the listener's BODY, so the import still happens.
    Only a subprocess that watches its own `sys.modules` and its own `Thread.start` can
    tell, which is what this is. The predicate is now inline (`fmt == "keys"`) and the
    import lives inside the branch."""
    import subprocess

    with Session(engine) as s:
        p = content.project_create(s, "DelProbe-%d" % _seq(), target_locales="es-ES")  # noqa: UP031
        doc = Document(
            project_id=p.id,
            filename="ordinary.md",
            fmt="md",
            mode=TransMode.SEGMENTED,
            skeleton={"units": []},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.add(
            Segment(
                document_id=doc.id,
                target_locale="es-ES",
                order_index=0,
                source_text="One.",
                status=SegStatus.NEW,
            )
        )
        s.commit()
        doc_id = doc.id

    proc = subprocess.run(
        [sys.executable, "-c", _DELETE_PROBE, str(doc_id)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    assert proc.returncode == 0 and "PROBE OK" in proc.stdout, (
        f"the document-delete probe failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr}"
    )
    with Session(engine) as s:
        assert s.get(Document, doc_id) is None, "the probe did not delete the document"
    print("  [G1] deleting an ordinary document loads no key code and patches no thread")


def test_g_internal_key_document_still_gets_its_tombstones():
    """The other half: the inline predicate must still FIRE for an internal key
    document, or review A3's tombstones are gone. Same seam, opposite answer."""
    with Session(engine) as s:
        p = content.project_create(
            s,
            "DelKeys-%d" % _seq(),  # noqa: UP031
            target_locales="es-ES",
            project_type=ProjectType.KEY_BASED,  # noqa: RUF100, UP031
        )
        k = keys.create_key(s, p, "doomed.value", base_value="Hello")
        pid = p.id
        doc_id = keys.values_for(s, k)[0].document_id
        before = keys.changes_since(s, pid, 0)["next_cursor"]
    with Session(engine) as s:
        retention.delete_document(s, doc_id)
    with Session(engine) as s:
        entries = keys.changes_since(s, pid, before)["entries"]
        assert any(
            e["entity"] == "value" and e["op"] == "deleted" and e["name"] == "doomed.value"
            for e in entries
        ), f"deleting an INTERNAL key document announced no value tombstones: {entries}"
    print("  [G2] ...and an internal key document still emits its value tombstones")


def test_g_the_run_seam_and_retention_teeth_have_a_second_home():
    """[G3] The fix round 2's note: the two SERVICE-level teeth this batch grew were
    each red in exactly one suite (verify_keys_surfaces), and a claim with one witness
    is one deleted file away from being unproven. Both are cheap to assert here, in the
    suite an existing install cares about, and both are pure service calls:

      * the RUN SEAM refuses a key project's trash document -- asserted on
        `run_workflow_stream`, which is where `start_run`'s background thread,
        `run_workflow`, `batch_runs._run_member` and `resume_run` all arrive (the
        A-F1 refusal used to sit on `start_run` alone, and three launchers walked past
        it); a NAMESPACE document is still accepted, so this is a guard, not a break;
      * RETENTION never ages an internal key document out, however old it is -- the
        hazard spec section 9 records, because a key catalogue has no "finished" state
        a document's age is a proxy for."""
    from datetime import datetime, timedelta

    from app.models import Workflow, WorkflowStep
    from app.services import orchestrator

    with Session(engine) as s:
        p = content.project_create(
            s,
            "SecondHome-%d" % _seq(),  # noqa: UP031
            target_locales="es-ES",
            project_type=ProjectType.KEY_BASED,  # noqa: RUF100, UP031
        )
        k = keys.create_key(s, p, "doomed.key", base_value="Hello")
        keys.delete_key(s, k, actor="probe")
        ns_doc = keys.internal_document(s, p, "", create=False)
        trash = keys.trash_document(s, p, create=False)
        assert trash is not None and ns_doc is not None, (trash, ns_doc)
        wf = Workflow(
            name="SecondHomeWF-%d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale="en-US",
            target_locale="es-ES",
        )
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                step_type="llm_agent",
                name="translate",
                prompt_template="Translate: {text}",
            )
        )
        s.commit()
        wf_id, trash_id, ns_id, pid = wf.id, trash.id, ns_doc.id, p.id
        doc_pid = content.project_create(s, "SecondHomeDocs-%d" % _seq(), target_locales="es-ES").id  # noqa: UP031

    with Session(engine) as s:
        for label, fn in (
            (
                "run_workflow_stream",
                lambda: list(orchestrator.run_workflow_stream(s, wf_id, trash_id, "es-ES")),
            ),
            ("start_run", lambda: orchestrator.start_run(s, wf_id, trash_id, "es-ES")),
        ):
            try:
                fn()
                _fail(f"orchestrator.{label} accepted a key project's TRASH document")
            except ValueError as exc:
                assert "deleted" in str(exc).lower(), (label, str(exc))
        assert list(orchestrator.run_workflow_stream(s, wf_id, ns_id, "es-ES")), (
            "the seam refused a legitimate NAMESPACE document too"
        )

    # ...and a real retention sweep ages neither internal document out, at any age.
    old = datetime.utcnow() - timedelta(days=4000)
    with Session(engine) as s:
        for d in s.exec(select(Document)).all():
            d.created_at = old
            s.add(d)
        victim = Document(
            project_id=doc_pid,
            filename="aged.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "Aged."}]},
            created_at=old,
        )
        s.add(victim)
        s.commit()
        s.refresh(victim)
        victim_id = victim.id
    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    settings.invalidate_cache()
    try:
        swept = retention.purge()
    finally:
        settings.set_many({"retention_enabled": "0", "retention_days": "0"})
        settings.invalidate_cache()
    assert swept["documents"] >= 1, ("the sweep sweeping nothing proves nothing", swept)
    with Session(engine) as s:
        assert s.get(Document, victim_id) is None, "the sweep did not actually run"
        assert s.get(Document, trash_id) is not None, "retention reaped the trash doc"
        assert s.get(Document, ns_id) is not None, "retention reaped the ns doc"
        assert keys.get_key(s, pid, "doomed.key", include_deleted=True) is not None, (
            "retention reaped a soft-deleted key's catalogue"
        )
    print(
        "  [G3] the run SEAM (run_workflow_stream + start_run) refuses the trash "
        "document while a namespace document still streams, and retention ages "
        "neither internal key document out -- both claims now have a second witness"
    )


# ============================================================================
# [H] THE BEHAVIOUR HALF (Wave B): document projects are byte-identical
# ============================================================================
def _document_fixture():
    """A document project with two documents and segments in two locales -- enough
    that every listing, count and rollup path has something real to render."""
    with Session(engine) as s:
        p = content.project_create(s, "InertDoc-%d" % _seq(), target_locales="es-ES, fr-FR")  # noqa: UP031
        pid = p.id
        for name in ("alpha.md", "beta.md"):
            doc = Document(
                project_id=pid,
                filename=name,
                fmt="md",
                mode=TransMode.SEGMENTED,
                skeleton={"units": []},
            )
            s.add(doc)
            s.commit()
            s.refresh(doc)
            for loc in ("es-ES", "fr-FR"):
                s.add(
                    Segment(
                        document_id=doc.id,
                        target_locale=loc,
                        order_index=0,
                        source_text="Hello.",
                        target_text="Hola.",
                        status=SegStatus.EDITED,
                    )
                )
        s.commit()
    return pid


def _document_surface_snapshot(pid):
    """Everything a document project shows on every surface, as one comparable blob.

    Deliberately WIDE. The dashboard and the project list are SHARED seams that Wave B
    taught to filter internal key documents, so a mistake there would hide a REAL
    document, and the only way to notice is to render them and compare."""
    import io as _io
    import json as _json
    from contextlib import redirect_stdout

    from fastapi.testclient import TestClient

    from app.cli.__main__ import main as cli_main
    from app.main import app

    client = TestClient(app)
    pages = {}
    for label, path in (
        ("dashboard", "/"),
        ("projects", "/projects"),
        ("project", "/projects/%d" % pid),  # noqa: UP031
    ):
        r = client.get(path)
        assert r.status_code == 200, (label, r.status_code)
        pages[label] = r.text

    def cli(argv):
        buf = _io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--json"] + argv)  # noqa: RUF005
        assert rc == 0, (argv, buf.getvalue())
        return _json.loads(buf.getvalue() or "{}")

    return {
        "pages": pages,
        "doc_list": cli(["doc", "list", str(pid)]),
        "project_show": cli(["project", "show", str(pid)]),
    }


#: The ONE affordance the kill switch is SUPPOSED to remove from a shared page: the
#: create-dialog type selector (spec section 6). It is stripped before the byte
#: comparison rather than exempted wholesale, so everything ELSE on those pages is
#: still held to byte identity -- an exemption for the whole page would hide exactly
#: the accidental change this group exists to catch.
_TYPE_SELECTOR = re.compile(
    r'\s*<fieldset class="lp-fieldset" data-np-type>.*?'
    r"</fieldset>\s*",
    re.S,
)
#: ...and the type-filter chip row on /projects, for the same reason.
_TYPE_CHIPS = re.compile(
    r'\s*<p class="chips filter-chips">\s*<a class="chip[^"]*"\s*'
    r'href="/projects\?status=[^"]*">All types</a>.*?</p>',
    re.S,
)
#: ...and the per-row "Keys" badge, which is a LINK to /projects/<id>/keys and
#: therefore gates on the switch too (review A-F8: with the switch off that route is a
#: 404, so an ungated badge is a link to a dead page). A third affordance the switch is
#: SUPPOSED to remove, so the byte-identity check strips it before comparing.
_KEYS_BADGE = re.compile(
    r'\s*<a class="badge beta" href="/projects/\d+/keys"[^>]*>'
    r"Keys</a>",
    re.S,
)


def _stable(snapshot):
    """The snapshot with the genuinely per-request bits normalized, so a difference
    means a BEHAVIOUR difference rather than a clock tick, a fresh session or an
    unrelated engine-liveness probe."""
    out = dict(snapshot)
    pages = {}
    for label, html in snapshot["pages"].items():
        # The CSRF token is minted per SESSION and the static cache-buster per BUILD.
        html = re.sub(r'name="csrf_token" value="[^"]*"', 'name="csrf_token" value="TOKEN"', html)
        html = re.sub(r'window\.CSRF_TOKEN = "[^"]*"', 'window.CSRF_TOKEN = "TOKEN"', html)
        html = re.sub(r'data-csrf="[^"]*"', 'data-csrf="TOKEN"', html)
        # CSP nonces are random per response, independent of key-project content.
        # Whitespace-anchored like verify_account_lockout so only real attributes match.
        html = re.sub(r'(\snonce=")[^"]+', r"\1NONCE", html)
        html = re.sub(r"\?v=[0-9a-f]{4,}", "?v=V", html)
        # The engine-liveness dots are re-probed whenever the settings cache is
        # dropped, which THIS TEST does on purpose when it flips the kill switch.
        # They are a property of the provider config, never of key projects.
        html = re.sub(
            r'class="dot (?:live|mock)" title="([a-z]+): (?:live|mock)"',
            r'class="dot D" title="\1: D"',
            html,
        )
        pages[label] = html
    out["pages"] = pages
    return out


def _without_key_affordances(html):
    """`html` minus the THREE controls the kill switch legitimately removes.

    Runs of whitespace are collapsed AFTER the cut: removing a Jinja block leaves the
    surrounding indentation shaped differently, and an indentation difference is not
    behaviour. Every non-whitespace byte still has to match."""
    # The badge is INLINE inside a <td>, so it is cut to "" (with its leading
    # whitespace) rather than to " " -- a residual space before `</td>` is exactly the
    # kind of difference the collapse below cannot absorb.
    return re.sub(
        r"\s+", " ", _KEYS_BADGE.sub("", _TYPE_CHIPS.sub(" ", _TYPE_SELECTOR.sub(" ", html)))
    )


def _diff(a, b, what):
    """Raise naming the FIRST difference, so a failure points at the seam rather than
    dumping two blobs."""
    if a == b:
        return
    for key in sorted(set(a) | set(b)):
        left, right = a.get(key), b.get(key)
        if left == right:
            continue
        if isinstance(left, dict) and isinstance(right, dict):
            _diff(left, right, what)
            continue
        raise AssertionError(
            f"document behaviour changed under {what!r} at {key!r}:\n  before: {str(left)[:400]!r}\n  after:  {str(right)[:400]!r}"  # noqa: E501
        )
    raise AssertionError(f"document behaviour changed under {what!r}")


def test_h_document_behaviour_is_byte_identical():
    from app.services import settings

    pid = _document_fixture()
    baseline = _stable(_document_surface_snapshot(pid))
    assert len(baseline["doc_list"]["documents"]) == 2, (
        "the fixture must have real documents -- otherwise a filter that hid them "
        "would pass every equality check below",
        baseline["doc_list"],
    )

    # (1) a KEY PROJECT alive next door must change nothing about the document one.
    with Session(engine) as s:
        kp = content.project_create(
            s,
            "InertKeys-%d" % _seq(),  # noqa: UP031
            target_locales="es-ES",
            project_type=ProjectType.KEY_BASED,  # noqa: RUF100, UP031
        )
        keys.create_key(s, kp, "inert::probe", base_value="Hello", actor="inert")
    with_keys = _stable(_document_surface_snapshot(pid))
    # /projects legitimately gains the new project's ROW; the document project's own
    # page and both CLI envelopes must not move at all.
    _diff(
        {k: v for k, v in baseline.items() if k != "pages"},
        {k: v for k, v in with_keys.items() if k != "pages"},
        "a key project existing next door",
    )
    assert baseline["pages"]["project"] == with_keys["pages"]["project"], (
        "the document project's own page changed when a key project appeared"
    )
    # THE DASHBOARD IS NOT COMPARED BYTE-FOR-BYTE HERE, AND IT MUST NOT BE (review
    # B11): a key project is a REAL project, so `/` and `/projects` legitimately gain
    # its row and its count. The printed claim below is narrowed accordingly -- and
    # the part of the dashboard that genuinely must not move is asserted instead:
    # `_dashboard_data`'s DOCUMENT rollup, which is what `_NOT_INTERNAL` exists for.
    # Without this, a dropped filter would silently fold every key project's internal
    # documents (and every parked value row of a deleted key) into the install-wide
    # documents/segments/approved KPIs.
    from app.web.queries import _dashboard_data

    with Session(engine) as s:
        real_docs = [d for d in s.exec(select(Document)).all() if d.fmt != "keys"]
        real_segs = s.exec(
            select(Segment).where(Segment.document_id.in_([d.id for d in real_docs]))
        ).all()
        internal = [d for d in s.exec(select(Document)).all() if d.fmt == "keys"]
        assert internal, "the fixture must have internal key documents to hide"
        data = _dashboard_data(s)
        assert data["kpis"]["documents"] == len(real_docs), (
            "the dashboard's document count includes internal key documents",
            data["kpis"]["documents"],
            len(real_docs),
        )
        assert data["kpis"]["segments"] == len(real_segs), (
            "the dashboard's segment rollup includes key VALUE rows",
            data["kpis"]["segments"],
            len(real_segs),
        )
        key_row = next(
            r for r in data["rows"] if r["project"].project_type == ProjectType.KEY_BASED
        )
        assert key_row["docs"] == 0, (
            "the /projects row for a KEY project counts its internal documents",
            key_row["docs"],
        )

    # (2) ...and neither does turning the whole feature OFF.
    settings.set_many({"key_projects_enabled": "0"})
    settings.invalidate_cache()
    try:
        off = _stable(_document_surface_snapshot(pid))
    finally:
        settings.set_many({"key_projects_enabled": "1"})
        settings.invalidate_cache()
    # Compared against WITH_KEYS, not the original baseline: the key project created
    # above is a real project, so the dashboard's count and the /projects table
    # legitimately grew a row. The claim under test here is that flipping the SWITCH
    # changes nothing, which is a comparison between two views of the SAME data.
    _diff(
        {k: v for k, v in with_keys.items() if k != "pages"},
        {k: v for k, v in off.items() if k != "pages"},
        "the kill switch being off",
    )
    # EVERY page embeds the new-project dialog (base.html), and /projects also carries
    # the type-filter chips -- the two controls the switch is SUPPOSED to remove. Strip
    # exactly those blocks and demand byte identity for everything else, so an
    # accidental change anywhere near them still fails.
    for label in ("dashboard", "projects", "project"):
        assert _without_key_affordances(with_keys["pages"][label]) == _without_key_affordances(
            off["pages"][label]
        ), (
            f"the {label} page changed beyond the type selector, type chips and Keys badge when the "  # noqa: E501
            "kill switch was flipped"
        )
    assert _TYPE_SELECTOR.search(with_keys["pages"]["dashboard"]), (
        "the stripper must have something to strip, or it proves nothing"
    )
    # With the switch off the Keys option is GONE; the selector itself stays because
    # Connected projects (a third type, behind its own switch) share it.
    assert 'value="key_based"' not in off["pages"]["dashboard"], (
        "the Keys (key-based) option must be GONE with the switch off"
    )
    assert 'value="key_based"' in with_keys["pages"]["dashboard"]
    print(
        "  [H1] a document project's own page, `doc list` and `project show` are "
        "BYTE-IDENTICAL in all three states; the dashboard and /projects are "
        "byte-identical across the SWITCH (modulo the three controls it removes) and, "
        "with a key project alive next door, carry an unchanged DOCUMENT rollup "
        "while legitimately gaining that project's own row"
    )


def test_h_document_api_responses_are_unchanged():
    """The /api half, held to the same standard -- including the two listings Wave B
    taught to filter internal key documents."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import apikeys, security, settings

    pid = _document_fixture()
    user = security.create_user(
        "inert-api-%d@x.com" % _seq(),  # noqa: UP031
        "Inert",
        "admin",
        "keysinert-pw-123456",  # noqa: RUF100, UP031
    )
    _row, tok = apikeys.create_key(user.id, "inert", scopes="api docs")
    client = TestClient(app)

    def snap():
        out = {}
        for path in ("/api/projects/%d" % pid, "/api/projects/%d/documents" % pid):  # noqa: UP031
            r = client.get(path, headers={"X-Loc-Token": tok})
            assert r.status_code == 200, (path, r.status_code, r.text)
            out[path] = r.json()
        return out

    before = snap()
    assert len(before["/api/projects/%d/documents" % pid]["documents"]) == 2, (  # noqa: UP031
        "the listing must still carry the REAL documents",
        before,
    )
    with Session(engine) as s:
        kp = content.project_create(
            s,
            "InertKeysApi-%d" % _seq(),  # noqa: UP031
            target_locales="es-ES",
            project_type=ProjectType.KEY_BASED,
        )
        keys.create_key(s, kp, "inert::api", base_value="Hello", actor="inert")
    assert snap() == before, "a key project next door changed a document /api response"
    settings.set_many({"key_projects_enabled": "0"})
    settings.invalidate_cache()
    try:
        assert snap() == before, "the kill switch changed a document /api response"
    finally:
        settings.set_many({"key_projects_enabled": "1"})
        settings.invalidate_cache()
    print(
        "  [H2] /api/projects/{id} and /api/projects/{id}/documents are unchanged "
        "(and still list the real documents) in all three states"
    )


def main():
    try:
        test_a_revision_only_adds()
        test_a_new_columns_are_as_declared()
        test_a_backfill_is_document()
        test_b_build_paths_agree()
        test_b_project_type_has_a_server_default_everywhere()
        test_b_partial_index_where_clauses_are_real()
        test_b_models_match_the_migration()
        test_c_downgrade_restores_the_previous_head()
        test_d_change_feed_listener_is_unreachable()
        test_d_listener_is_back_on()
        test_e_document_projects_are_unchanged()
        test_e_partial_index_does_not_constrain_document_rows()
        test_e_model_default_is_document()
        test_f_listener_is_registered_in_every_process()
        test_g_document_delete_stays_out_of_the_keys_module()
        test_g_internal_key_document_still_gets_its_tombstones()
        test_g_the_run_seam_and_retention_teeth_have_a_second_home()
        test_h_document_behaviour_is_byte_identical()
        test_h_document_api_responses_are_unchanged()
        print("KEYS INERTNESS OK")
    finally:
        # The standing mkdtemp-leak chip: this suite makes ~7 throwaway DBs of its own
        # (tests._bootstrap only cleans the one it created).
        for d in _TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
