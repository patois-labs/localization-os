"""Migrations + configurable-DB verification (Phase 4-A).

Proves the Alembic baseline is complete and the SQLite default path is unchanged,
WITHOUT needing a real Postgres server:

  1. baseline completeness: run `alembic upgrade head` programmatically against a
     FRESH temp SQLite DATA_DIR, then assert every table in SQLModel.metadata
     exists AND (rigorously) that every table's DB column set exactly equals its
     model column set (both model-only and db-only diffs are failures), AND that
     the four hot-path indexes from app/db.py exist — including the UNIQUE
     ux_segment_doc_locale_order. The 'Default' org seed is also checked.
  2. config override: a subprocess with DATABASE_URL set must resolve
     config.DB_URL to that URL and config.IS_SQLITE to False (module-level config
     is import-time, so a clean subprocess is the honest check).
  3. dialect-guard sanity: init_db() on the default SQLite path still creates
     tables and runs _ensure_columns (a dropped-then-re-added column reappears),
     i.e. the SQLite path is behaviourally unchanged.

ASCII-only output.
"""

import glob
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# Run against a throwaway data dir so we never touch the real loc_tms.db.
from tests._bootstrap import init_env

init_env("loctms_mig_")

from sqlalchemy import create_engine, inspect, text

from app import config, models


def _alembic_upgrade_head(data_dir: str) -> None:
    """Run `alembic upgrade head` in-process against a fresh SQLite data dir."""
    from alembic.config import Config

    from alembic import command

    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    # env.py reads app.config.DB_URL, which derives from LOC_TMS_DATA_DIR; point
    # both at the fresh dir. (set_main_option gives env.py the URL directly too.)
    url = "sqlite:///" + os.path.join(data_dir, "loc_tms.db").replace("\\", "/")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return url


def _alembic_upgrade(data_dir: str, target: str) -> str:
    """Run ``alembic upgrade <target>`` in-process against a SQLite data dir (any revision,
    not only head), returning the URL. Mirrors :func:`_alembic_upgrade_head`."""
    from alembic.config import Config

    from alembic import command

    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    url = "sqlite:///" + os.path.join(data_dir, "loc_tms.db").replace("\\", "/")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, target)
    return url


def test_t1b_a5_data_step_backfills_bindings() -> None:
    """SPEC-terminology-backend T1b §A.5 (round-2 M-1, MUT-A5): the c4d8e2a1f7b6 migration's
    ``upgrade()`` must run its A.5 DATA STEP, not only add the two binding columns. Stamp a DB at
    the PARENT revision (b7e3f1a9c2d4 -- before the binding columns exist), seed a concept-linked
    module entry via Core, then ``alembic upgrade head`` and assert the data step populated
    ``target_term_id`` (+ the ``meta_overrides`` tombstone) with NO app boot. Deleting the
    ``_backfill_pair_bindings_core(op.get_bind())`` call from ``upgrade()`` (MUT-A5) leaves both
    columns NULL -> this fails."""
    import datetime as _dt

    from sqlalchemy import MetaData, Table
    from sqlalchemy import create_engine as _ce
    from sqlalchemy import insert as _ins
    from sqlalchemy import select as _sel

    fresh = tempfile.mkdtemp(prefix="loctms_mig_a5_")
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = fresh
    try:
        # 1) build the schema up to the PARENT of the T1b revision (no binding columns yet).
        url = _alembic_upgrade(fresh, "b7e3f1a9c2d4")
        eng = _ce(url)
        md = MetaData()
        md.reflect(bind=eng)
        gcols = {c["name"] for c in inspect(eng).get_columns("glossaryentry")}
        assert "target_term_id" not in gcols and "meta_overrides" not in gcols, (
            "precondition: the parent revision must NOT carry the binding columns yet"
        )

        def _fill(table: Table, known: dict) -> dict:
            """Return a values dict with every NOT NULL column supplied (a type default for the
            ones the seed does not name), so a Core insert at this schema never trips a NOT NULL."""
            row = dict(known)
            for col in table.columns:
                if col.name in row or col.primary_key:
                    continue
                if col.nullable or col.default is not None or col.server_default is not None:
                    continue
                t = col.type.__class__.__name__.lower()
                if "int" in t:
                    row[col.name] = 0
                elif "bool" in t:
                    row[col.name] = False
                elif "float" in t or "numeric" in t:
                    row[col.name] = 0.0
                elif "date" in t or "time" in t:
                    row[col.name] = _dt.datetime(2026, 1, 1)
                else:
                    row[col.name] = ""
            return row

        gl = md.tables["glossary"]
        gc_t = md.tables["glossaryconcept"]
        gt = md.tables["glossaryterm"]
        ge = md.tables["glossaryentry"]
        with eng.begin() as conn:
            gid = conn.execute(
                _ins(gl), _fill(gl, {"name": "a5-mig", "status": "draft"})
            ).inserted_primary_key[0]
            cid = conn.execute(
                _ins(gc_t), _fill(gc_t, {"glossary_id": gid, "definition": "a billing document"})
            ).inserted_primary_key[0]
            src_id = conn.execute(
                _ins(gt),
                _fill(gt, {"concept_id": cid, "locale": "en-US", "term": "invoice"}),
            ).inserted_primary_key[0]
            conn.execute(
                _ins(gt),
                _fill(gt, {"concept_id": cid, "locale": "es-ES", "term": "factura"}),
            )
            eid = conn.execute(
                _ins(ge),
                _fill(
                    ge,
                    {
                        "glossary_id": gid,
                        "source_locale": "en-US",
                        "target_locale": "es-ES",
                        "source_term": "invoice",
                        "target_term": "factura",
                        "concept_id": cid,
                        "term_id": src_id,
                    },
                ),
            ).inserted_primary_key[0]
        eng.dispose()

        # 2) upgrade to head: c4d8e2a1f7b6 adds the columns AND runs the A.5 data step.
        _alembic_upgrade(fresh, "head")

        eng2 = _ce(url)
        md2 = MetaData()
        md2.reflect(bind=eng2)
        ge2 = md2.tables["glossaryentry"]
        gt2 = md2.tables["glossaryterm"]
        with eng2.connect() as conn:
            row = conn.execute(
                _sel(ge2.c.target_term_id, ge2.c.meta_overrides).where(ge2.c.id == eid)
            ).first()
            factura_id = conn.execute(
                _sel(gt2.c.id).where(gt2.c.concept_id == cid, gt2.c.term == "factura")
            ).scalar_one()
        eng2.dispose()
        assert row is not None and row[0] == factura_id, (
            f"A.5 data step did not resolve target_term_id (got {row and row[0]!r}, "
            f"want {factura_id}) -- the migration's backfill call is missing"
        )
        # the concept carries a definition the entry does not override -> a tombstone override.
        assert row[1] is not None, (
            "A.5 data step did not pin meta_overrides -- the migration's backfill call is missing"
        )
    finally:
        if prev is not None:
            os.environ["LOC_TMS_DATA_DIR"] = prev
        else:
            os.environ.pop("LOC_TMS_DATA_DIR", None)
    print("  T1b A.5: alembic upgrade head backfills target_term_id/meta_overrides (no app boot)")


def test_baseline_completeness() -> None:
    fresh = tempfile.mkdtemp(prefix="loctms_mig_fresh_")
    # IMPORTANT: do NOT call init_db()/create_all first — the migration must build
    # the whole schema itself. Point LOC_TMS_DATA_DIR at the fresh dir so env.py
    # (which reads app.config.DB_URL) targets it.
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = fresh
    try:
        url = _alembic_upgrade_head(fresh)
    finally:
        if prev is not None:
            os.environ["LOC_TMS_DATA_DIR"] = prev

    engine = create_engine(url)
    insp = inspect(engine)
    present = set(insp.get_table_names())

    expected = set(models.SQLModel.metadata.tables.keys())
    missing = expected - present
    assert not missing, f"migration did not create tables: {sorted(missing)}"
    assert "alembic_version" in present, "alembic did not stamp a version"
    print("  baseline: %d model tables all present after upgrade head" % len(expected))  # noqa: UP031

    # RIGOROUS column completeness: for EVERY table in the model, the DB's column
    # name set after `upgrade head` must exactly equal the model's column name set.
    # This makes the migration the real guarantee that the baseline stays in sync
    # with the models (it catches BOTH a column the migration forgot to create AND
    # a stray column the migration created that the model no longer has).
    total_cols = 0
    for table_name, table in sorted(models.SQLModel.metadata.tables.items()):
        model_cols = {c.name for c in table.columns}
        db_cols = {c["name"] for c in insp.get_columns(table_name)}
        model_only = model_cols - db_cols
        db_only = db_cols - model_cols
        assert not model_only and not db_only, (
            f"{table_name} column mismatch: model-only={sorted(model_only)} db-only={sorted(db_only)}"  # noqa: E501
        )
        total_cols += len(model_cols)
    print(
        "  baseline: all %d columns across %d tables match the models exactly"  # noqa: UP031
        % (total_cols, len(expected))
    )

    # The four hot-path indexes (must match app/db.py) exist after upgrade head,
    # INCLUDING the unique segment index. Uses inspect().get_indexes(), which on
    # SQLite reports unique indexes with their unique flag set.
    def _index_names(table):
        return {ix["name"] for ix in insp.get_indexes(table)}

    def _unique_index_names(table):
        return {ix["name"] for ix in insp.get_indexes(table) if ix.get("unique")}

    seg_ix = _index_names("segment")
    assert "ix_segment_doc_locale" in seg_ix, (
        f"missing ix_segment_doc_locale on segment: {sorted(seg_ix)}"
    )
    assert "ux_segment_doc_locale_order" in seg_ix, (
        f"missing ux_segment_doc_locale_order on segment: {sorted(seg_ix)}"
    )
    assert "ux_segment_doc_locale_order" in _unique_index_names("segment"), (
        "ux_segment_doc_locale_order exists but is not UNIQUE"
    )
    assert "ix_segmenttm_lookup" in _index_names("segmenttm"), (
        "missing ix_segmenttm_lookup on segmenttm"
    )
    assert "ix_fulltexttm_lookup" in _index_names("fulltexttm"), (
        "missing ix_fulltexttm_lookup on fulltexttm"
    )
    # #158 RATCHET: the EXPRESSION-based unique index over lower(email) -- the #74
    # guarantee that one normalized address means one account. It needs its own assertion
    # because it fails SILENTLY: SQLAlchemy cannot reflect an expression index, so any
    # `batch_alter_table('user')` (a full table REBUILD on SQLite, and the house pattern
    # for adding a column) drops it and does not put it back, emitting only
    #   SAWarning: Skipped unsupported reflection of expression-based index
    # Nothing else would notice; duplicate normalized emails simply become insertable
    # again, and db.py's own recreate path is skipped once duplicates exist. Reflection is
    # no use for the same reason the rebuild loses it, so read sqlite_master directly.
    with engine.connect() as _c:
        user_ix = {
            r[0]
            for r in _c.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='user'"
            ).fetchall()
        }
    assert "ux_user_email_lower" in user_ix, (
        "#158: ux_user_email_lower is MISSING after `upgrade head`. An expression index "
        "cannot be reflected, so a batch_alter_table('user') anywhere in the chain "
        "silently destroyed it -- and with it the #74 one-account-per-normalized-email "
        "guarantee. Recreate it in that revision. Present: " + repr(sorted(user_ix))
    )
    print("  baseline: 4 hot-path indexes present (incl. UNIQUE ux_segment_doc_locale_order)")
    print("  #158: ux_user_email_lower survives upgrade head (expression index, silent loss)")

    # The amended segmentembedding migration (revision c3d9e7f1a2b8) must build the
    # UNIQUE (segment_tm_id, model) index that backs the spam-proof upsert (finding
    # 5a). Assert it exists AND is unique on the Alembic/Postgres authority path.
    emb_ix = _index_names("segmentembedding")
    assert "ux_segmentembedding_segtm_model" in emb_ix, (
        f"missing ux_segmentembedding_segtm_model on segmentembedding: {sorted(emb_ix)}"
    )
    assert "ux_segmentembedding_segtm_model" in _unique_index_names("segmentembedding"), (
        "ux_segmentembedding_segtm_model exists but is not UNIQUE"
    )
    assert "ix_segmentembedding_lookup" in emb_ix, (
        "missing ix_segmentembedding_lookup on segmentembedding"
    )
    print("  baseline: segmentembedding UNIQUE (segment_tm_id, model) index present")

    # The 'Default' organization seed folded into the baseline upgrade().
    with engine.connect() as c:
        orgs = c.execute(text("SELECT name FROM organization")).fetchall()
    assert orgs == [("Default",)], f"expected exactly one 'Default' org, got {orgs!r}"
    print("  baseline: 'Default' organization seeded by migration")

    # SPEC-terminology-backend T1 (§4.1/§4.2, M5): the concept/term store's NAMED indexes
    # must exist after `upgrade head` — NEW guard code, not a loop extension. The composite
    # ix_glossaryconcept_extid + the single-column ix_glossaryconcept_glossary_id (the
    # auto-name of glossary_id's Field(index=True), declared in the migration too) + the
    # single named ix_glossaryterm_concept. create_all emits the same set (the models
    # declare them), so column-set parity above and these named-index checks together pin
    # the two engines to one schema.
    concept_ix = _index_names("glossaryconcept")
    # ix_glossaryconcept_hgroup (T1b, §A.1): the homonym-group closure index the projector's
    # group-closed scope query uses; declared in the model + the c4d8e2a1f7b6 revision.
    for name in (
        "ix_glossaryconcept_glossary_id",
        "ix_glossaryconcept_extid",
        "ix_glossaryconcept_hgroup",
    ):
        assert name in concept_ix, f"missing {name} on glossaryconcept: {sorted(concept_ix)}"
    term_ix = _index_names("glossaryterm")
    assert "ix_glossaryterm_concept" in term_ix, (
        f"missing ix_glossaryterm_concept on glossaryterm: {sorted(term_ix)}"
    )
    # exactly one named index on glossaryterm.concept_id (no stray auto-named duplicate):
    assert "ix_glossaryterm_concept_id" not in term_ix, (
        "glossaryterm has BOTH the named and an auto-named concept index (drop Field(index=True))"
    )
    print("  baseline: glossary concept/term named indexes present (2 + 1), no stray dup")
    engine.dispose()


def test_baseline_nullability_parity() -> None:
    """For EVERY table+column, alembic-head nullability must equal the model's
    nullability. This is the guard that closes the gap the column-set + index checks
    left open: it would have caught BOTH the segmentembedding.embedding drift (NOT NULL
    under alembic, nullable under the model before the reconcile) AND the glossaryentry
    class (a migration that widened a column the create_all path never applied). After
    the model reconcile (SegmentEmbedding.embedding pinned NOT NULL) this is GREEN with
    ZERO diffs."""
    fresh = tempfile.mkdtemp(prefix="loctms_mig_null_")
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = fresh
    try:
        url = _alembic_upgrade_head(fresh)
    finally:
        if prev is not None:
            os.environ["LOC_TMS_DATA_DIR"] = prev

    engine = create_engine(url)
    insp = inspect(engine)

    mismatches = []
    checked = 0
    for table_name, table in sorted(models.SQLModel.metadata.tables.items()):
        model_nullable = {c.name: bool(c.nullable) for c in table.columns}
        db_nullable = {c["name"]: bool(c["nullable"]) for c in insp.get_columns(table_name)}
        for col, m_nul in model_nullable.items():
            if col not in db_nullable:
                continue  # column-set parity is asserted by test_baseline_completeness
            checked += 1
            if db_nullable[col] != m_nul:
                mismatches.append(
                    f"{table_name}.{col}: alembic nullable={db_nullable[col]} model nullable={m_nul}"  # noqa: E501
                )
    engine.dispose()
    assert not mismatches, "alembic-head vs model NULLABILITY drift (%d):\n  %s" % (  # noqa: UP031
        len(mismatches),
        "\n  ".join(sorted(mismatches)),
    )
    print("  baseline: nullability parity across %d columns (alembic-head == models)" % checked)  # noqa: UP031


def test_config_override() -> None:
    """A clean subprocess with DATABASE_URL set resolves config to Postgres."""
    pg_url = "postgresql+psycopg://u:p@localhost:5432/loc_tms"
    env = dict(os.environ)
    env["DATABASE_URL"] = pg_url
    # A distinct data dir so the keyfiles the config module writes don't collide.
    env["LOC_TMS_DATA_DIR"] = tempfile.mkdtemp(prefix="loctms_mig_cfg_")
    code = (
        "from app import config; "
        "print(config.DB_URL); "
        "print('SQLITE' if config.IS_SQLITE else 'NOTSQLITE')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, f"subprocess failed: {out.stderr}"
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    assert pg_url in lines, f"DB_URL not overridden by DATABASE_URL: {lines!r}"
    assert "NOTSQLITE" in lines, f"IS_SQLITE should be False for a postgres URL: {lines!r}"
    print("  config: DATABASE_URL overrides DB_URL and flips IS_SQLITE off")

    # And the default (no DATABASE_URL) stays SQLite.
    env2 = dict(os.environ)
    env2.pop("DATABASE_URL", None)
    env2["LOC_TMS_DATA_DIR"] = tempfile.mkdtemp(prefix="loctms_mig_cfg2_")
    out2 = subprocess.run(
        [sys.executable, "-c", code],
        env=env2,
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert out2.returncode == 0, f"subprocess failed: {out2.stderr}"
    lines2 = [ln.strip() for ln in out2.stdout.splitlines() if ln.strip()]
    assert any(ln.startswith("sqlite:///") for ln in lines2), (
        f"default should be sqlite: {lines2!r}"
    )
    assert "SQLITE" in lines2, f"IS_SQLITE should be True by default: {lines2!r}"
    print("  config: default (no DATABASE_URL) stays sqlite (zero-config)")


def test_dialect_guard_sanity() -> None:
    """init_db() on the default SQLite path still creates tables + backfills
    columns via _ensure_columns (the SQLite path is unchanged)."""
    from app import db

    assert config.IS_SQLITE, "this suite runs on the SQLite default engine"
    assert db.engine.dialect.name == "sqlite"

    db.init_db()
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())
    for t in ("user", "project", "auditlog", "segment"):
        assert t in tables, f"init_db did not create {t}"

    # Drop a column that _ensure_columns is responsible for backfilling, then
    # re-run init_db and confirm the additive migration path re-adds it. This
    # exercises the PRAGMA->inspect refactor on the live SQLite path.
    with db.engine.connect() as c:
        # SQLite supports DROP COLUMN since 3.35; the venv's sqlite is new enough.
        c.execute(text("ALTER TABLE user DROP COLUMN ui_locale"))
        c.commit()
    after_drop = {col["name"] for col in inspect(db.engine).get_columns("user")}
    assert "ui_locale" not in after_drop, "precondition: ui_locale should be dropped"

    db.init_db()  # _ensure_columns should re-add it
    after_fix = {col["name"] for col in inspect(db.engine).get_columns("user")}
    assert "ui_locale" in after_fix, "_ensure_columns did not re-add ui_locale"
    print("  dialect-guard: sqlite init_db creates tables + _ensure_columns backfills")


def test_added_columns_registry_covers_revisions() -> None:
    """THE TWIN GUARD: every column an alembic revision adds to an EXISTING table must ALSO be
    in app/db.py::_ADDED_COLUMNS.

    The two paths are not alternatives, they are twins. A DB built by alembic gets the column
    from the revision; a REAL legacy SQLite install (tables present, no alembic_version) is
    brought forward by create_all() + `_ensure_columns`, which knows only this registry. Ship a
    revision without its shim entry and that second population is missing the column, so every
    `select()` on the table dies with "no such column" -- the whole feature, not just the new
    field. (Found on this branch: the advancedstagedtarget vision/adjudicated pair had the
    revision and no shim entry.) Scanning the revisions makes the invariant self-enforcing
    instead of a convention in a comment."""
    import re

    from app.db import _ADDED_COLUMNS

    # Both spellings the revisions use: the direct `op.add_column("t", sa.Column("c", ...))` and
    # the `with op.batch_alter_table("t") as batch_op: batch_op.add_column(sa.Column("c", ...))`
    # block (the majority). Missing the batch form would make this guard near-vacuous.
    direct = re.compile(
        r'op\.add_column\(\s*["\'](?P<table>\w+)["\']\s*,\s*sa\.Column\(\s*["\'](?P<col>\w+)["\']'
    )
    batch_table = re.compile(r'op\.batch_alter_table\(\s*["\'](?P<table>\w+)["\']')
    batch_add = re.compile(r'batch_op\.add_column\(\s*sa\.Column\(\s*["\'](?P<col>\w+)["\']')

    # NO ALLOWLIST. The two pre-existing gaps this guard found when it was written
    # (user.ui_skin, workflow.created_via) have their shim entries now, so the guard is
    # strict: every add_column a revision performs must be in the registry, no exceptions.
    # If a new gap appears, fix the registry -- do not re-introduce an allowlist here.
    scanned = 0
    missing: list[str] = []
    for path in sorted(glob.glob(os.path.join(_ROOT, "alembic", "versions", "*.py"))):
        # WHOLE FILE, not line by line: a formatter wraps a long call across lines
        # (`op.add_column(\n    "t",\n    sa.Column("c", ...),\n)`) and a per-line scan sees
        # neither half. That blind spot hid the very column this guard was written for
        # (advancedstagedtarget.adjudicated) plus webhookevent.processed_at.
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        found: list[tuple[str, str]] = [
            (m.group("table"), m.group("col")) for m in direct.finditer(src)
        ]
        # A batch_op.add_column belongs to the batch_alter_table block it sits inside, so
        # attribute each one to the nearest PRECEDING batch_alter_table by position.
        blocks = [(m.start(), m.group("table")) for m in batch_table.finditer(src)]
        for m in batch_add.finditer(src):
            table = next((t for pos, t in reversed(blocks) if pos < m.start()), None)
            if table:
                found.append((table, m.group("col")))
        for tbl, col in found:
            scanned += 1
            known = {name for name, _ddl in _ADDED_COLUMNS.get(tbl, [])}
            if col not in known:
                missing.append(f"{os.path.basename(path)}: {tbl}.{col}")
    assert scanned >= 100, f"the revision scan matched only {scanned} add_column calls (regex rot?)"
    assert not missing, (
        "alembic revisions add columns that _ensure_columns would never backfill on a legacy "
        "SQLite install (add them to app/db.py::_ADDED_COLUMNS with matching DDL): "
        + "; ".join(missing)
    )
    print(
        f"  registry: {scanned} literal revision add_column(s) scanned, ALL in _ADDED_COLUMNS "
        "(no gaps; loop-driven add_column calls with a variable column name are not scanned)"
    )


def test_revises_docstring_matches_down_revision() -> None:
    """Every revision file's docstring `Revises:` line must name the SAME parent as its
    `down_revision` assignment.

    Alembic reads only `down_revision`, so a docstring that disagrees breaks nothing TODAY, but it
    is a FALSE PIN inside a schema-authority file, invisible to every other guard (the Batch-11
    standing lesson: false provenance on a resolving pin). It bites when the next person re-chains a
    revision (a merge re-parents `down_revision` but leaves the header naming the OLD parent), reads
    the header, and re-roots the graph wrong. This is exactly how b9e3d7c1f5a2 shipped its P3 T2
    round-2 MEDIUM. Scanning both the header line and the code makes the invariant
    self-enforcing."""
    import re

    header_re = re.compile(r"^Revises:\s*(\S+)?\s*$", re.MULTILINE)
    # down_revision may be single- or double-quoted, or literal None.
    code_re = re.compile(r"^down_revision[^\n=]*=\s*(None|['\"]([^'\"]+)['\"])", re.MULTILINE)

    scanned = 0
    mismatches: list[str] = []
    for path in sorted(glob.glob(os.path.join(_ROOT, "alembic", "versions", "*.py"))):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        hm = header_re.search(src)
        cm = code_re.search(src)
        if hm is None or cm is None:
            mismatches.append(
                f"{os.path.basename(path)}: could not parse "
                f"{'Revises header' if hm is None else 'down_revision'}"
            )
            continue
        header_parent = hm.group(1)  # None when the header line is empty (a base revision)
        code_parent = None if cm.group(1) == "None" else cm.group(2)
        scanned += 1
        if header_parent != code_parent:
            mismatches.append(
                f"{os.path.basename(path)}: docstring Revises={header_parent!r} != "
                f"down_revision={code_parent!r}"
            )
    assert scanned >= 80, f"the revision scan matched only {scanned} files (regex rot?)"
    assert not mismatches, (
        "revision file docstring `Revises:` lines disagree with their `down_revision` (a false pin "
        "in a schema-authority file; fix the docstring to match the code): " + "; ".join(mismatches)
    )
    print(f"  Revises/down_revision agreement: {scanned} revision files, ALL agree")


def test_legacy_shape_backfill_advanced_staged_target() -> None:
    """The legacy-shape path for the P1 provenance columns, end to end: an advancedstagedtarget
    table WITHOUT vision/adjudicated (an install created before this build) must gain both from
    `_ensure_columns`, and an ORM select over the model must then work -- the exact failure a
    missing shim entry produces ("no such column: advancedstagedtarget.vision" on every
    target.propose and deliver, i.e. a dead conductor tier)."""
    from sqlmodel import Session, select

    from app import db
    from app.models import AdvancedStagedTarget

    db.init_db()
    with db.engine.connect() as c:
        for col in ("vision", "adjudicated"):
            c.execute(text(f"ALTER TABLE advancedstagedtarget DROP COLUMN {col}"))
        c.commit()
    dropped = {col["name"] for col in inspect(db.engine).get_columns("advancedstagedtarget")}
    assert not ({"vision", "adjudicated"} & dropped), "precondition: both columns dropped"

    db.init_db()  # create_all() + _ensure_columns: the legacy-install path
    restored = {col["name"] for col in inspect(db.engine).get_columns("advancedstagedtarget")}
    assert {"vision", "adjudicated"} <= restored, (
        f"_ensure_columns did not backfill the advanced provenance columns: {sorted(restored)}"
    )
    with Session(db.engine) as s:
        s.exec(select(AdvancedStagedTarget)).all()  # would raise OperationalError pre-fix
    print("  legacy-shape: advancedstagedtarget vision/adjudicated backfilled + selectable")


def test_legacy_shape_backfill_user_ui_skin() -> None:
    """The legacy-shape path for User.ui_skin (alembic c1d5e8f3a9b4): a `user` table WITHOUT
    ui_skin (an install created before that build, never alembic-managed) must gain it from
    `_ensure_columns`, and an ORM select over User must then work -- the exact failure the
    missing shim entry produced ("no such column: user.ui_skin" on EVERY user lookup, i.e. a
    dead login/session path, not merely a skin preference that falls back)."""
    from sqlmodel import Session, select

    from app import db
    from app.models import User

    db.init_db()
    with db.engine.connect() as c:
        c.execute(text("ALTER TABLE user DROP COLUMN ui_skin"))
        c.commit()
    dropped = {col["name"] for col in inspect(db.engine).get_columns("user")}
    assert "ui_skin" not in dropped, "precondition: ui_skin should be dropped"

    db.init_db()  # create_all() + _ensure_columns: the legacy-install path
    restored = {col["name"] for col in inspect(db.engine).get_columns("user")}
    assert "ui_skin" in restored, (
        f"_ensure_columns did not backfill user.ui_skin: {sorted(restored)}"
    )
    with Session(db.engine) as s:
        s.exec(select(User)).all()  # would raise OperationalError pre-fix
    print("  legacy-shape: user.ui_skin backfilled + selectable")


def test_legacy_shape_backfill_workflow_created_via() -> None:
    """The legacy-shape path for Workflow.created_via (alembic c3e9a1f7b2d5): a `workflow` table
    WITHOUT created_via must gain it from `_ensure_columns`, and an ORM select over Workflow must
    then work -- the exact failure the missing shim entry produced ("no such column:
    workflow.created_via" on every workflow list/dispatch/run, i.e. a dead classic engine)."""
    from sqlmodel import Session, select

    from app import db
    from app.models import Workflow

    db.init_db()
    with db.engine.connect() as c:
        c.execute(text("ALTER TABLE workflow DROP COLUMN created_via"))
        c.commit()
    dropped = {col["name"] for col in inspect(db.engine).get_columns("workflow")}
    assert "created_via" not in dropped, "precondition: created_via should be dropped"

    db.init_db()  # create_all() + _ensure_columns: the legacy-install path
    restored = {col["name"] for col in inspect(db.engine).get_columns("workflow")}
    assert "created_via" in restored, (
        f"_ensure_columns did not backfill workflow.created_via: {sorted(restored)}"
    )
    with Session(db.engine) as s:
        s.exec(select(Workflow)).all()  # would raise OperationalError pre-fix
    print("  legacy-shape: workflow.created_via backfilled + selectable")


def test_invite_revocation_upgrade() -> None:
    """Upgrade a populated pre-129 database without consuming its existing invite."""
    import secrets

    fresh = tempfile.mkdtemp(prefix="loctms_mig_invite_")
    url = _alembic_upgrade(fresh, "e2b6c9f4a713")
    eng = create_engine(url)
    token = secrets.token_urlsafe(32)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invite "
                "(email, role, token, created_by, created_at, expires_at, accepted) "
                "VALUES (:email, 'translator', :token, 'system', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"email": "migration@example.test", "token": token},
        )
    assert "revoked_at" not in {c["name"] for c in inspect(eng).get_columns("invite")}
    _alembic_upgrade_head(fresh)
    column = next(c for c in inspect(eng).get_columns("invite") if c["name"] == "revoked_at")
    assert column["nullable"]
    org_column = next(c for c in inspect(eng).get_columns("invite") if c["name"] == "org_id")
    assert org_column["nullable"]
    with eng.connect() as conn:
        row = conn.execute(text("SELECT token, accepted, revoked_at, org_id FROM invite")).one()
        assert row.token == token and not row.accepted and row.revoked_at is None
        assert row.org_id is None
    eng.dispose()
    print("  invite revoked_at: populated predecessor upgrades, existing token preserved")


def test_legacy_shape_backfill_evaldataset_source() -> None:
    """AI opt-out (gate round 4): an evaldataset table WITHOUT source_project_id /
    source_customer_id (an install from before this build, booting through the
    create_all + _ensure_columns self-heal path that a legacy pre-alembic DB uses) must gain
    BOTH columns and the one-time backfill must stamp them, without crashing. The backfill
    writes both columns, so running it after only the first one was added crashed init_db
    ("no such column: source_customer_id") on every boot of such an install.

    Rows: a genuine source document (older than the dataset) is stamped; a REUSED document
    id (created after the dataset) and a missing document stay NULL (the opt-out check then
    fails closed on them); an upload dataset (no source document) is untouched."""
    from datetime import datetime, timedelta

    from app import db

    db.init_db()
    now = datetime.utcnow()
    old, new = now - timedelta(days=2), now + timedelta(days=2)
    with db.engine.connect() as c:
        c.execute(
            text(
                "INSERT INTO customer (name, created_at, ai_opt_out) VALUES ('mig-ds-cust', :t, 0)"
            ),
            {"t": old},
        )
        cid = c.execute(text("SELECT max(id) FROM customer")).scalar()
        c.execute(
            text(
                "INSERT INTO project (name, customer_id, source_locale, default_provider, "
                "provider_mode, project_type, status, created_by, created_at, ai_opt_out) "
                "VALUES ('mig-ds-proj', :c, 'en-US', 'mock', 'uniform', 'document', "
                "'in_translation', '', :t, 0)"
            ),
            {"c": cid, "t": old},
        )
        pid = c.execute(text("SELECT max(id) FROM project")).scalar()

        def _doc(created):
            c.execute(
                text(
                    "INSERT INTO document (project_id, filename, fmt, mode, status, "
                    "created_at, created_by) VALUES (:p, 'd.txt', 'txt', 'segmented', "
                    "'ready', :t, '')"
                ),
                {"p": pid, "t": created},
            )
            return c.execute(text("SELECT max(id) FROM document")).scalar()

        genuine, reused = _doc(old), _doc(new)

        def _ds(name, src):
            c.execute(
                text(
                    "INSERT INTO evaldataset (name, description, source_locale, target_locale, "
                    "origin, source_document_id, item_count, created_at, created_by) "
                    "VALUES (:n, '', 'en-US', 'es-ES', 'document', :d, 0, :t, '')"
                ),
                {"n": name, "d": src, "t": now},
            )
            return c.execute(text("SELECT max(id) FROM evaldataset")).scalar()

        ds_ok, ds_reused, ds_missing, ds_upload = (
            _ds("ok", genuine),
            _ds("reused", reused),
            _ds("missing", 987654),
            _ds("upload", None),
        )
        for col in ("source_customer_id", "source_project_id"):
            c.execute(text(f"ALTER TABLE evaldataset DROP COLUMN {col}"))
        c.commit()
    have = {col["name"] for col in inspect(db.engine).get_columns("evaldataset")}
    assert not ({"source_project_id", "source_customer_id"} & have), "precondition"

    db.init_db()  # create_all() + _ensure_columns: must NOT crash, must stamp
    have = {col["name"] for col in inspect(db.engine).get_columns("evaldataset")}
    assert {"source_project_id", "source_customer_id"} <= have, sorted(have)
    with db.engine.connect() as c:
        rows = {
            r[0]: (r[1], r[2])
            for r in c.execute(
                text("SELECT id, source_project_id, source_customer_id FROM evaldataset")
            )
        }
    assert rows[ds_ok] == (pid, cid), rows[ds_ok]
    assert rows[ds_reused] == (None, None), rows[ds_reused]
    assert rows[ds_missing] == (None, None), rows[ds_missing]
    assert rows[ds_upload] == (None, None), rows[ds_upload]
    db.init_db()  # a second boot is a clean no-op
    print("  legacy-shape: evaldataset source columns added + backfilled (no boot crash)")


def _index_sql(url: str) -> dict:
    """{(table, index name): normalized CREATE INDEX sql} for every named index, read straight
    from sqlite_master so expression indexes (which SQLAlchemy cannot reflect) are included."""
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tbl_name, name, sql FROM sqlite_master WHERE type='index' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ).fetchall()
    finally:
        eng.dispose()
    return {(t, n): " ".join((s or "").split()) for t, n, s in rows}


# Revisions whose downgrade drops a column from a table that has (or may grow) an index a SQLite
# batch rebuild cannot carry over. Each must step down WITHOUT a table rebuild, leaving the exact
# index set of its down_revision. f2c8a6d4b1e7 once used batch drop_column on `project` and lost
# the expression index ux_project_inbox_key (connected projects sweep 2026-10-13).
_DOWNGRADE_INDEX_GUARDED = (
    ("f2c8a6d4b1e7", "c2a8e4f6b1d3", "project"),
    ("c2a8e4f6b1d3", "e1b7c3a9d5f2", "targetlocale"),
)


def test_downgrade_keeps_every_index() -> None:
    from alembic.config import Config

    from alembic import command

    for rev, down, table in _DOWNGRADE_INDEX_GUARDED:
        ref_dir = tempfile.mkdtemp(prefix="loctms_mig_dnref_")
        reference = _index_sql(_alembic_upgrade(ref_dir, down))

        work_dir = tempfile.mkdtemp(prefix="loctms_mig_dn_")
        url = _alembic_upgrade(work_dir, rev)
        cfg = Config(os.path.join(_ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.downgrade(cfg, down)
        got = _index_sql(url)

        changed = sorted(set(got.items()) ^ set(reference.items()))
        assert got == reference, f"downgrade {rev} -> {down} changed indexes: {changed}"
        # The source must not route the guarded table through a batch rebuild on downgrade.
        path = glob.glob(os.path.join(_ROOT, "alembic", "versions", rev + "_*.py"))[0]
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        down_src = src.split("def downgrade", 1)[1]
        assert "batch_alter_table" not in down_src, (
            f"{rev} downgrade uses batch_alter_table on {table} (SQLite rebuild drops indexes)"
        )
    count = len(_DOWNGRADE_INDEX_GUARDED)
    print(f"  downgrade: {count} guarded revisions step down with every index intact")


def main() -> None:
    test_invite_revocation_upgrade()
    test_downgrade_keeps_every_index()
    test_t1b_a5_data_step_backfills_bindings()
    test_baseline_completeness()
    test_baseline_nullability_parity()
    test_config_override()
    test_dialect_guard_sanity()
    test_added_columns_registry_covers_revisions()
    test_revises_docstring_matches_down_revision()
    test_legacy_shape_backfill_advanced_staged_target()
    test_legacy_shape_backfill_user_ui_skin()
    test_legacy_shape_backfill_workflow_created_via()
    test_legacy_shape_backfill_evaldataset_source()
    print("MIGRATIONS OK")


if __name__ == "__main__":
    main()
