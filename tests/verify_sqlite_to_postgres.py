"""SQLite -> Postgres data copy (`loc-tms db copy-to-postgres`, scripts/sqlite_to_postgres.py).

Engine redesign S3b. The Postgres half runs ONLY when LOC_TMS_TARGET_PG_URL names a server
where that role may CREATE DATABASE (it creates and drops its own scratch databases and never
touches the one the URL names); otherwise it is skipped cleanly and says so.

Covers:
  offline   -- URL normalization and redaction, the CLI verb's usage errors (no env var, missing
               file, non-Postgres URL), the verb is host-only (--remote refused), value converters.
  copy      -- a seeded SQLite install (current head) is copied into a freshly migrated Postgres
               database: every table's rows equal the source (booleans, timestamps, JSON, NUL
               stripped from text), per-table counts match, serial sequences continue after the
               highest copied id, dropped-and-re-added foreign keys are all back.
  refusals  -- a second run is refused (target no longer empty); a revision mismatch is refused;
               a source with foreign-key orphans is refused BEFORE anything is written; --check
               writes nothing.
  rollback  -- a failure part way through leaves the target exactly as it was (still only the
               migration-seeded rows, every foreign key still present).
"""

import contextlib
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_s2p_")

import sqlalchemy as sa

from scripts import sqlite_to_postgres as s2p

PG_URL = os.environ.get("LOC_TMS_TARGET_PG_URL", "")
_FAILS = []


def _check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILS.append(name)


def _raises(fn, *needles):
    try:
        fn()
    except s2p.CopyError as exc:
        return all(n in str(exc) for n in needles), str(exc)[:200]
    return False, "no CopyError"


# --------------------------------------------------------------------------- offline
def test_offline():
    n = s2p.normalize_url
    _check("postgres:// -> psycopg", n("postgres://u@h/d") == "postgresql+psycopg://u@h/d")
    _check("postgresql:// -> psycopg", n("postgresql://u@h/d") == "postgresql+psycopg://u@h/d")
    keep = "postgresql+psycopg://u@h/d"
    _check("explicit driver kept", n(keep) == keep)
    ok, d = _raises(lambda: n("sqlite:///x.db"), "Postgres")
    _check("sqlite URL refused", ok, d)
    ok, d = _raises(lambda: n(""), "no target")
    _check("empty URL refused", ok, d)
    pw_marker = "pw" + "-s2p-fixture"
    red = s2p.redact(f"postgresql+psycopg://user:{pw_marker}@host/db")
    _check("password redacted from the report", pw_marker not in red and "***" in red, red)

    stats = {}
    t = sa.table(
        "t", sa.column("b", sa.Boolean), sa.column("s", sa.String), sa.column("d", sa.DateTime)
    )
    conv = {c.name: s2p._converter(c, stats) for c in t.columns}
    _check("bool 0/1", conv["b"](1) is True and conv["b"](0) is False and conv["b"](None) is None)
    _check("NUL stripped and counted", conv["s"]("a\x00b") == "ab" and stats["nul_stripped"] == 1)
    ts = conv["d"]("2026-10-23 08:01:02.123456")
    _check("timestamp parsed", ts == datetime.datetime(2026, 10, 23, 8, 1, 2, 123456), repr(ts))

    cli = [sys.executable, "-m", "app.cli", "db", "copy-to-postgres"]
    env = {k: v for k, v in os.environ.items() if k != "LOC_TMS_TARGET_PG_URL"}
    missing = os.path.join(_TMP, "nope.db")
    p = subprocess.run([*cli, missing], capture_output=True, text=True, env=env, timeout=120)
    _check("missing source file -> exit 1", p.returncode == 1, p.stderr[:120])
    here = os.path.join(_TMP, "empty.db")
    sqlite3.connect(here).close()
    p = subprocess.run([*cli, here], capture_output=True, text=True, env=env, timeout=120)
    _check(
        "no target env var -> usage error (exit 2) naming the variable",
        p.returncode == 2 and "LOC_TMS_TARGET_PG_URL" in p.stderr,
        p.stderr[:160],
    )
    env["LOC_TMS_TARGET_PG_URL"] = "sqlite:///elsewhere.db"
    p = subprocess.run([*cli, here], capture_output=True, text=True, env=env, timeout=120)
    _check(
        "non-Postgres target -> exit 1",
        p.returncode == 1 and "Postgres" in p.stderr,
        p.stderr[:160],
    )
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli",
            "--remote",
            "https://example.test",
            "db",
            "copy-to-postgres",
            here,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    _check("host-only: --remote is refused (exit 2)", p.returncode == 2, p.stderr[:160])


# --------------------------------------------------------------------------- seeded source
def seed_sqlite() -> str:
    """A current-head SQLite install with rows covering the awkward types; returns its path."""
    from sqlmodel import Session

    from app import db
    from app.db import init_db
    from app.models import (
        Document,
        Project,
        Segment,
        TargetLocale,
        Workflow,
        WorkflowStep,
    )

    init_db()
    with Session(db.engine) as s:
        projects = [Project(name=f"P{i}", source_locale="en-US") for i in range(3)]
        s.add_all(projects)
        s.commit()
        for p in projects:
            s.refresh(p)
        for p in projects:
            s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        wf = Workflow(name="wf")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(WorkflowStep(workflow_id=wf.id, order_index=0, name="translate"))
        docs = [Document(project_id=projects[0].id, filename=f"d{i}.docx") for i in range(4)]
        s.add_all(docs)
        s.commit()
        for d in docs:
            s.refresh(d)
        for n, d in enumerate(docs):
            for k in range(30):
                s.add(
                    Segment(
                        document_id=d.id,
                        target_locale="de-DE",
                        order_index=k,
                        source_text=f"source {n}-{k} é中",
                        target_text="with\x00nul" if (n, k) == (1, 2) else f"target {n}-{k}",
                        status="approved" if k % 2 else "mt",
                    )
                )
        s.commit()
    db.engine.dispose()
    # Test mode builds the schema with create_all and never stamps Alembic; a real install is
    # stamped at head, which is what the copy requires.
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(
        Config(os.path.join(_ROOT, "alembic.ini"))
    ).get_current_head()
    raw = sqlite3.connect(os.path.join(_TMP, "loc_tms.db"))
    raw.execute("create table if not exists alembic_version (version_num varchar(32) not null)")
    raw.execute("delete from alembic_version")
    raw.execute("insert into alembic_version values (?)", (head,))
    raw.commit()
    raw.close()
    out = os.path.join(_TMP, "seeded.db")
    src = sqlite3.connect(os.path.join(_TMP, "loc_tms.db"))
    dst = sqlite3.connect(out)
    src.backup(dst)
    src.close()
    dst.close()
    return out


def _admin_engine():
    return sa.create_engine(s2p.normalize_url(PG_URL), isolation_level="AUTOCOMMIT")


@contextlib.contextmanager
def scratch_db():
    name = "locos_s2p_" + uuid.uuid4().hex[:10]
    admin = _admin_engine()
    with admin.connect() as c:
        c.execute(sa.text(f'create database "{name}"'))
    url = sa.engine.make_url(s2p.normalize_url(PG_URL)).set(database=name)
    url_s = url.render_as_string(hide_password=False)
    env = dict(os.environ, DATABASE_URL=url_s)
    p = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        cwd=_ROOT,
        timeout=600,
    )
    try:
        assert p.returncode == 0, p.stderr[-500:]
        yield url_s
    finally:
        with admin.connect() as c:
            c.execute(sa.text(f'drop database if exists "{name}" with (force)'))
        admin.dispose()


def _rows(conn_exec, table, cols):
    return conn_exec(table, cols)


def _norm(v):
    if isinstance(v, datetime.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True)
    if isinstance(v, memoryview):
        return bytes(v)
    return v


def _norm_src(v, col):
    if isinstance(col.type, sa.DateTime) and isinstance(v, str):
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat(sep=" ")
    if isinstance(col.type, sa.JSON) and isinstance(v, str):
        try:
            j = json.loads(v)
        except ValueError:
            return v
        return None if j is None else json.dumps(j, sort_keys=True)
    if isinstance(v, str):
        return v.replace("\x00", "")
    return v


def tables_equal(sqlite_path, url) -> tuple[bool, str, int]:
    sq = sqlite3.connect(sqlite_path)
    e = sa.create_engine(url)
    md = sa.MetaData()
    md.reflect(e)
    total = 0
    with e.connect() as c:
        for name, t in sorted(md.tables.items()):
            if name == "alembic_version":
                continue
            cols = list(t.columns)
            names = [col.name for col in cols]
            order = ", ".join(f'"{n}"' for n in ([c.name for c in t.primary_key.columns] or names))
            sel = ", ".join(f'"{n}"' for n in names)
            a_rows = sq.execute(f'select {sel} from "{name}" order by {order}').fetchall()
            b_rows = c.execute(sa.text(f'select {sel} from "{name}" order by {order}')).fetchall()
            if len(a_rows) != len(b_rows):
                return False, f"{name}: {len(a_rows)} vs {len(b_rows)} rows", total
            for a, b in zip(a_rows, b_rows, strict=True):
                x = [_norm(_norm_src(v, col)) for v, col in zip(a, cols, strict=True)]
                y = [_norm(v) for v in b]
                if x != y:
                    diff = [(n, p, q) for n, p, q in zip(names, x, y, strict=True) if p != q][:2]
                    return False, f"{name}: {diff}", total
            total += len(a_rows)
    sq.close()
    e.dispose()
    return True, "", total


def fk_count(url) -> int:
    e = sa.create_engine(url)
    with e.connect() as c:
        n = c.execute(sa.text("select count(*) from pg_constraint where contype = 'f'")).scalar()
    e.dispose()
    return n


def seeded_rows(url) -> dict:
    e = sa.create_engine(url)
    md = sa.MetaData()
    md.reflect(e)
    out = {}
    with e.connect() as c:
        for name, t in md.tables.items():
            k = c.execute(sa.select(sa.func.count()).select_from(t)).scalar()
            if k:
                out[name] = k
    e.dispose()
    return out


# --------------------------------------------------------------------------- postgres
def test_postgres():
    seeded = seed_sqlite()
    with scratch_db() as url:
        fks_before = fk_count(url)
        base = seeded_rows(url)
        _check(
            "fresh migrated target holds only seeded rows",
            set(base) <= {"alembic_version", "organization"},
            str(base),
        )

        rep = s2p.run(seeded, url, check_only=True)
        _check("--check passes and writes nothing", rep["ok"] and seeded_rows(url) == base)

        rep = s2p.run(seeded, url)
        _check("copy ok", rep["ok"], str({k: v for k, v in rep.items() if k != "tables"})[:300])
        _check(
            "per-table counts match",
            all(t["source"] == t["target"] for t in rep["tables"]) and rep["source_rows"] > 120,
        )
        ok, detail, n = tables_equal(seeded, url)
        _check(f"every row equals the source ({n} rows)", ok, detail)
        _check("NUL character stripped and counted", rep["converted"].get("nul_stripped") == 1)
        _check(
            "foreign keys all back",
            fk_count(url) == fks_before and rep["foreign_keys_restored"] > 0,
        )

        e = sa.create_engine(url)
        with e.begin() as c:
            top = c.execute(sa.text("select max(id) from segment")).scalar()
            new = c.execute(
                sa.text(
                    "insert into segment (document_id, target_locale, order_index, source_text, "
                    "target_text, status) select document_id, 'fr-FR', 1, 'x', 'y', 'mt' "
                    "from segment limit 1 returning id"
                )
            ).scalar()
        e.dispose()
        _check("serial sequence continues after the copied ids", new == top + 1, f"{top} -> {new}")

        ok, d = _raises(lambda: s2p.run(seeded, url), "not empty")
        _check("second run refused (target not empty)", ok, d)

    with scratch_db() as url:
        base = seeded_rows(url)
        stale = os.path.join(_TMP, "stale.db")
        sq = sqlite3.connect(seeded)
        sq2 = sqlite3.connect(stale)
        sq.backup(sq2)
        sq.close()
        sq2.execute("update alembic_version set version_num = 'deadbeef0000'")
        sq2.commit()
        sq2.close()
        ok, d = _raises(lambda: s2p.run(stale, url), "revisions differ")
        _check("revision mismatch refused", ok, d)

        orphan = os.path.join(_TMP, "orphan.db")
        sq = sqlite3.connect(seeded)
        sq2 = sqlite3.connect(orphan)
        sq.backup(sq2)
        sq.close()
        sq2.execute("pragma foreign_keys = off")
        sq2.execute(
            "update segment set document_id = 999999 where id = (select min(id) from segment)"
        )
        sq2.commit()
        sq2.close()
        ok, d = _raises(lambda: s2p.run(orphan, url), "foreign key", "segment")
        _check("foreign-key orphans refused before any write", ok and seeded_rows(url) == base, d)

    with scratch_db() as url:
        base = seeded_rows(url)
        fks_before = fk_count(url)
        real = s2p._copy_table
        calls = {"n": 0}

        def boom(src, conn, table, *a, **k):
            calls["n"] += 1
            if calls["n"] == 5:
                raise RuntimeError("simulated failure part way through")
            return real(src, conn, table, *a, **k)

        s2p._copy_table = boom
        try:
            ok, d = _raises(lambda: s2p.run(seeded, url), "rolled back")
        finally:
            s2p._copy_table = real
        _check("failure part way is reported as rolled back", ok, d)
        _check(
            "target unchanged after the failure (rows)",
            seeded_rows(url) == base,
            str(seeded_rows(url)),
        )
        _check("target unchanged after the failure (foreign keys)", fk_count(url) == fks_before)
        rep = s2p.run(seeded, url)
        _check("the same command succeeds after a failed run", rep["ok"])


def main():
    test_offline()
    if not PG_URL:
        print(
            "SKIP postgres half: set LOC_TMS_TARGET_PG_URL to a server "
            "where the role can CREATE DATABASE"
        )
    else:
        test_postgres()
    if _FAILS:
        print("FAILED:", ", ".join(_FAILS))
        sys.exit(1)
    print("SQLITE TO POSTGRES OK")


if __name__ == "__main__":
    main()
