"""Alembic-is-schema-authority verification (SPEC-alembic-authority).

Proves the four properties the authority change relies on, all hermetically (throwaway
LOC_TMS_DATA_DIR, never the real data/ dir):

  (a) alembic-head vs create_all(models) has ZERO column-set AND nullability diffs.
      Before the SegmentEmbedding.embedding reconcile there was exactly ONE nullability
      diff (embedding: NOT NULL under alembic, nullable under create_all); after it, zero.
  (b) A REAL install (config.IS_TEST False, via LOC_TMS_SERVER=1) with a FRESH DB goes
      through init_db()'s alembic path: alembic_version is stamped AT HEAD, every model
      table exists, and segmentembedding.embedding is NOT NULL.
  (c) The LEGACY fallback: a real install whose DB has tables but NO alembic_version does
      NOT crash init_db(), logs the rebuild WARNING, and still yields a working schema.
  (d) The IS_TEST path is UNCHANGED: init_db() on the hermetic test engine uses create_all
      and does NOT create an alembic_version table (so the ~30 fast suites keep their path).

Sub-paths (b) and (c) require config.IS_TEST to be False, which is import-time; this suite's
own process is IS_TEST True. So they run in CHILD subprocesses with LOC_TMS_SERVER=1 + a
throwaway LOC_TMS_DATA_DIR (the honest way to flip the import-time flag), mirroring
verify_migrations.test_config_override.

ASCII-only output.
"""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# Throwaway data dir so we never touch the real loc_tms.db; blank keys so no live egress.
from tests._bootstrap import init_env

init_env("loctms_authority_")

from sqlalchemy import create_engine, inspect

from app import config, db, models


def _run_child(code: str, data_dir: str) -> subprocess.CompletedProcess:
    """Run `code` in a child interpreter as a REAL install (LOC_TMS_SERVER=1 =>
    config.IS_TEST False) against a fresh throwaway data dir. cwd = repo root so
    `from app import ...` resolves to THIS worktree."""
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    env["LOC_TMS_SERVER"] = "1"  # flips IS_TEST False -> the real-install path
    env.pop("DATABASE_URL", None)  # stay on SQLite
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_BASE_URL"):
        env[k] = ""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def test_a_alembic_vs_create_all_zero_diff() -> None:
    """(a) alembic-head and create_all(models) agree on both column set and nullability
    for every table -- ZERO diffs after the model reconcile."""
    from alembic.config import Config

    from alembic import command

    # alembic-head on a fresh DB
    ad = tempfile.mkdtemp(prefix="authority_al_")
    al_url = "sqlite:///" + os.path.join(ad, "al.db").replace("\\", "/")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", al_url)
    command.upgrade(cfg, "head")
    al_eng = create_engine(al_url)
    al_insp = inspect(al_eng)

    # create_all on a separate fresh DB
    cd = tempfile.mkdtemp(prefix="authority_ca_")
    ca_url = "sqlite:///" + os.path.join(cd, "ca.db").replace("\\", "/")
    ca_eng = create_engine(ca_url)
    models.SQLModel.metadata.create_all(ca_eng)
    ca_insp = inspect(ca_eng)

    col_diffs = []
    null_diffs = []
    for t in sorted(models.SQLModel.metadata.tables):
        al_cols = {c["name"]: bool(c["nullable"]) for c in al_insp.get_columns(t)}
        ca_cols = {c["name"]: bool(c["nullable"]) for c in ca_insp.get_columns(t)}
        if set(al_cols) != set(ca_cols):
            col_diffs.append(
                f"{t}: alembic-only={sorted(set(al_cols) - set(ca_cols))} create_all-only={sorted(set(ca_cols) - set(al_cols))}"  # noqa: E501
            )
        for col in set(al_cols) & set(ca_cols):
            if al_cols[col] != ca_cols[col]:
                null_diffs.append(
                    f"{t}.{col}: alembic nullable={al_cols[col]} create_all nullable={ca_cols[col]}"
                )
    al_eng.dispose()
    ca_eng.dispose()

    assert not col_diffs, "column-set diffs:\n  " + "\n  ".join(col_diffs)
    assert not null_diffs, "nullability diffs:\n  " + "\n  ".join(sorted(null_diffs))
    # Sanity: the reconciled column is NOT NULL on BOTH paths.
    assert "embedding" in {c["name"] for c in al_insp.get_columns("segmentembedding")}
    print(
        "  (a) alembic-head == create_all: ZERO column + nullability diffs "
        "(segmentembedding.embedding NOT NULL on both)"
    )


_CHILD_B = r"""
import sys
from sqlalchemy import inspect, text
from alembic.config import Config
from alembic.script import ScriptDirectory
from app import config, db, models
assert not config.IS_TEST, "child must be a REAL install (IS_TEST False)"
assert db.engine.dialect.name == "sqlite"
# Fresh DB -> init_db must take the alembic authority path.
db.init_db()
insp = inspect(db.engine)
tables = set(insp.get_table_names())
assert "alembic_version" in tables, "alembic did not stamp a version on fresh real install"
expected = set(models.SQLModel.metadata.tables.keys())
missing = expected - tables
assert not missing, "missing model tables after alembic build: %s" % sorted(missing)
# Stamped AT HEAD.
cfg = Config("alembic.ini"); cfg.set_main_option("script_location", "alembic")
head = ScriptDirectory.from_config(cfg).get_current_head()
with db.engine.connect() as c:
    stamped = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
assert stamped == head, "alembic_version %r not at head %r" % (stamped, head)
# The reconciled column is NOT NULL under the real alembic build.
emb = {c["name"]: c["nullable"] for c in insp.get_columns("segmentembedding")}
assert emb["embedding"] is False, "segmentembedding.embedding should be NOT NULL"
# Default org present (migration seeds it; _ensure_default_org is a no-op).
with db.engine.connect() as c:
    orgs = [r[0] for r in c.execute(text("SELECT name FROM organization")).fetchall()]
assert orgs == ["Default"], "expected one Default org, got %r" % (orgs,)
# Idempotent: a second init_db at head is a clean no-op.
db.init_db()
print("CHILD_B_OK")
"""


def test_b_real_install_fresh_uses_alembic() -> None:
    """(b) A fresh real install builds under alembic, stamped at head, embedding NOT NULL."""
    d = tempfile.mkdtemp(prefix="authority_real_")
    proc = _run_child(_CHILD_B, d)
    tail = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    assert proc.returncode == 0, f"child_b exited {proc.returncode}:\n{tail}"
    assert "CHILD_B_OK" in (proc.stdout or ""), f"child_b did not confirm:\n{tail}"
    print(
        "  (b) fresh real install: alembic_version stamped at head, all tables, "
        "embedding NOT NULL, Default org seeded, init_db idempotent"
    )


_CHILD_C = r"""
import logging, sys
logging.basicConfig(level=logging.WARNING)   # ensure the warning reaches stderr
from sqlalchemy import inspect, text
from app import config, db, models
assert not config.IS_TEST, "child must be a REAL install (IS_TEST False)"
# Simulate a LEGACY pre-alembic DB: tables via create_all, NO alembic_version.
models.SQLModel.metadata.create_all(db.engine)
insp = inspect(db.engine)
assert insp.get_table_names(), "precondition: DB should have tables"
assert not insp.has_table("alembic_version"), "precondition: no alembic_version (legacy)"
# Must NOT crash on the legacy fallback path.
db.init_db()
insp2 = inspect(db.engine)
tables = set(insp2.get_table_names())
for t in ("user", "project", "segment", "segmentembedding"):
    assert t in tables, "legacy fallback lost table %s" % t
# Fallback path does not stamp alembic_version (it stays legacy until rebuilt).
assert not insp2.has_table("alembic_version"), "legacy fallback should NOT stamp alembic"
# Default org still seeded by _ensure_default_org on the fallback path.
with db.engine.connect() as c:
    orgs = [r[0] for r in c.execute(text("SELECT name FROM organization")).fetchall()]
assert orgs == ["Default"], "expected one Default org, got %r" % (orgs,)
print("CHILD_C_OK")
"""


def test_c_legacy_fallback_no_crash_warns() -> None:
    """(c) A legacy (tables, no alembic_version) real install falls back to create_all,
    logs the rebuild warning, and does not crash."""
    d = tempfile.mkdtemp(prefix="authority_legacy_")
    proc = _run_child(_CHILD_C, d)
    tail = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    assert proc.returncode == 0, f"child_c exited {proc.returncode}:\n{tail}"
    assert "CHILD_C_OK" in (proc.stdout or ""), f"child_c did not confirm:\n{tail}"
    assert "legacy pre-alembic SQLite DB detected" in (proc.stderr or ""), (
        f"legacy fallback did not log the rebuild WARNING:\n{tail}"
    )
    print("  (c) legacy pre-alembic DB: fallback works, WARNING logged, no crash")


def test_d_is_test_path_unchanged() -> None:
    """(d) The hermetic IS_TEST path still uses create_all -- NO alembic_version table is
    created, so the fast suites keep their path (no alembic overhead)."""
    assert config.IS_TEST, "this suite's own process must be IS_TEST"
    assert db.engine.dialect.name == "sqlite"
    db.init_db()
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())
    for t in ("user", "project", "segment", "segmentembedding"):
        assert t in tables, f"IS_TEST init_db did not create {t}"
    assert "alembic_version" not in tables, (
        "IS_TEST path must NOT run alembic (found alembic_version table) -- the fast "
        "path regressed onto the migration path"
    )
    # And the model reconcile holds on the create_all test path too.
    emb = {c["name"]: c["nullable"] for c in insp.get_columns("segmentembedding")}
    assert emb["embedding"] is False, "segmentembedding.embedding should be NOT NULL"
    print("  (d) IS_TEST path unchanged: create_all used, no alembic_version stamped")


# --- Postgres-portability of the alembic chain (GitHub #520) -----------------------
#
# Three Postgres/typing defects found in review of PR 473. Items (e) and (f) are reasoned
# from Postgres typing rules (the finder could not start Postgres in its sandbox), so they
# are checked by COMPILING the real column / reading the real SQL, not by running against a
# live Postgres; item (g) was reproduced on SQLite and is checked by a real round-trip.


def _load_migration(filename: str):
    """Import a single alembic revision module by file path (the versions dir is not a
    package), so the test drives the migration's own upgrade()/downgrade() source."""
    path = os.path.join(_ROOT, "alembic", "versions", filename)
    spec = importlib.util.spec_from_file_location("_mig_" + filename[:12], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CaptureOp:
    """Stand-in for the alembic `op` proxy that records sa.Column objects handed to
    batch_alter_table(...).add_column(), so a column can be compiled in isolation."""

    def __init__(self) -> None:
        self.columns: list = []

    def execute(self, *args, **kwargs) -> None:
        pass

    class _Batch:
        def __init__(self, sink: list) -> None:
            self._sink = sink

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def add_column(self, column) -> None:
            self._sink.append(column)

        def drop_column(self, name: str) -> None:
            pass

    def batch_alter_table(self, name, schema=None, **kwargs):
        return self._Batch(self.columns)


def test_e_requires_quote_not_integer_default() -> None:
    """(e) c3d4e5f6a7b8's requires_quote BOOLEAN must NOT carry an integer server default.

    sa.text('0') renders `DEFAULT 0` on Postgres, which rejects an integer default on a
    BOOLEAN column ("default expression is of type integer"), aborting every Postgres
    install past this revision. Compiling the real column with the Postgres dialect proves
    the emitted default is a boolean literal, not a bare integer."""
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    mig = _load_migration("c3d4e5f6a7b8_vendor_wave_e_quote_rework.py")
    capture = _CaptureOp()
    mig.op = capture  # the migration uses the module-level `op` proxy
    mig.upgrade()

    by_name = {c.name: c for c in capture.columns}
    assert "requires_quote" in by_name, "requires_quote column not found in the migration"
    rq = by_name["requires_quote"]
    assert rq.nullable is False, "requires_quote must stay NOT NULL"
    assert isinstance(rq.type, sa.Boolean), f"requires_quote is not a Boolean: {rq.type!r}"

    table = sa.Table("vendordispatch", sa.MetaData(), rq)
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    match = re.search(r"requires_quote\s+BOOLEAN\s+DEFAULT\s+(\S+)", ddl)
    assert match, f"no DEFAULT rendered for requires_quote in:\n{ddl}"
    default = match.group(1)
    assert not re.fullmatch(r"\d+", default), (
        "requires_quote emits an INTEGER default on Postgres "
        f"(DEFAULT {default}); use sa.false() (or a quoted string). DDL:\n{ddl}"
    )
    print(f"  (e) requires_quote Postgres DDL default is a boolean literal (DEFAULT {default})")


def test_f_dedupe_update_assigns_boolean_literal() -> None:
    """(f) a1f4c8b2d6e9's dedupe UPDATE must assign a boolean literal (false), not 0.

    Postgres has no implicit int-to-boolean cast, so `active = 0` fails on exactly the
    Postgres installs that have duplicate emails; SQLite accepts both, keeping tests green.
    Reading the real SQL and asserting the `active` assignment is `false` catches the
    integer without needing a live Postgres."""
    path = os.path.join(_ROOT, "alembic", "versions", "a1f4c8b2d6e9_user_email_unique_lower.py")
    src = Path(path).read_text(encoding="utf-8")
    upgrade_body = src.split("def upgrade", 1)[1].split("def downgrade")[0]
    literals = re.findall(r'"([^"\n]*)"', upgrade_body)
    dedupe = [s for s in literals if "SET email" in s and "active" in s]
    assert len(dedupe) == 1, f"expected exactly one dedupe UPDATE, found {len(dedupe)}: {dedupe}"
    match = re.search(r"active\s*=\s*([^\s,]+)", dedupe[0])
    assert match, f"no `active =` assignment in the dedupe UPDATE: {dedupe[0]!r}"
    value = match.group(1)
    assert value.lower() == "false", (
        "the dedupe UPDATE must assign a boolean literal (false); an integer aborts the "
        f"migration on Postgres. Found active = {value!r}"
    )
    print("  (f) dedupe UPDATE assigns a boolean literal (active = false)")


def test_g_full_chain_round_trip_sqlite() -> None:
    """(g) On a throwaway SQLite DB, upgrade head -> downgrade base -> upgrade head all pass.

    The a3f7c1e9d5b2 downgrade dropped ux_project_inbox_key unconditionally, but the later
    b6f1d0a7c34e downgrade rebuilds `project` and silently loses that expression index, so
    the unconditional drop raised 'no such index' and aborted the whole downgrade. DROP
    INDEX IF EXISTS makes the full round trip pass. FAILS on the base tree at a3f7c1e9d5b2."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text as sa_text

    from alembic import command

    d = tempfile.mkdtemp(prefix="authority_rt_")
    url = "sqlite:///" + os.path.join(d, "rt.db").replace("\\", "/")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    eng = create_engine(url)
    insp = inspect(eng)
    assert "alembic_version" in insp.get_table_names(), "re-upgrade did not stamp a version"
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with eng.connect() as conn:
        stamped = conn.execute(sa_text("SELECT version_num FROM alembic_version")).scalar()
    eng.dispose()
    assert stamped == head, f"after re-upgrade version is {stamped!r}, not head {head!r}"
    print("  (g) full chain round trip: upgrade head -> downgrade base -> upgrade head OK")


def main() -> None:
    test_a_alembic_vs_create_all_zero_diff()
    test_b_real_install_fresh_uses_alembic()
    test_c_legacy_fallback_no_crash_warns()
    test_d_is_test_path_unchanged()
    test_e_requires_quote_not_integer_default()
    test_f_dedupe_update_assigns_boolean_literal()
    test_g_full_chain_round_trip_sqlite()
    print("ALEMBIC AUTHORITY OK")


if __name__ == "__main__":
    main()
