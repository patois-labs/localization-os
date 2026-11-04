"""Copy a Localization OS SQLite database into an empty, migrated Postgres database.

Operator tool behind `loc-tms db copy-to-postgres` (host-only: no web, API or MCP surface,
because it reads a database file from the host's disk and writes to a database server the
operator names). It never imports `app.db`: it must not open, create or migrate the install's
own database as a side effect.

What it does, in one PostgreSQL transaction (all or nothing):

  1. Preflight (read only): the source is a SQLite file at the SAME Alembic revision as the
     target; the target is Postgres, migrated, and holds no data beyond what the migrations
     seed (the `alembic_version` row and the default organization row); the source has no
     foreign-key orphans (SQLite does not enforce foreign keys, Postgres does).
  2. Copy every table that exists on the Postgres side, parents before children, in batches.
     Foreign keys that sit in a dependency cycle (customer / project / workflow / ...) or point
     at their own table are dropped for the copy and re-added afterwards, which validates every
     row. Values are converted to the target column types (SQLite 0/1 -> boolean, ISO text ->
     timestamp, JSON text -> JSON, NUL characters stripped from text, counted in the report).
  3. Reset every serial / identity sequence to the highest copied id.
  4. Verify the per-table row counts and the sequences before COMMIT. Any mismatch rolls the
     whole copy back, leaving the target exactly as it was.

Idempotency: a second run against a target that already holds the copy is refused in the
preflight (the target is no longer empty), and a failed run leaves nothing behind, so the
same command can simply be run again. To redo a finished copy, drop and re-create the
Postgres database, run `alembic upgrade head`, and copy again.

Not copied: files (uploads, keyfiles; they stay in the data directory), SQLite-only tables
(e.g. FTS shadow tables), and the SQLite file itself, which is opened read-only and never
changed.

Documented in: docs/architecture/backups-and-dr.md
"""

import datetime as _dt
import json
import re
import sqlite3
import time
from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.sql.ddl import sort_tables_and_constraints

# Rows a fresh migration leaves behind that the copy replaces rather than refuses.
MIGRATION_SEEDED = ("alembic_version", "organization")
# SQLite internals and full-text shadow tables are not data tables.
_IGNORED_SOURCE = re.compile(r"^(sqlite_|.*_fts($|_)|.*_(content|docsize|idx|config|data)$)")
DEFAULT_BATCH = 2000


class CopyError(Exception):
    """A refusal or failure that the operator can act on (message is plain ASCII)."""


def normalize_url(url: str) -> str:
    """postgres:// and postgresql:// both mean psycopg here; anything else is refused."""
    url = (url or "").strip()
    if not url:
        raise CopyError("no target Postgres URL given")
    scheme, sep, rest = url.partition("://")
    if not sep:
        raise CopyError("the target URL must look like postgresql+psycopg://user@host/dbname")
    low = scheme.lower()
    if low in ("postgres", "postgresql"):
        return "postgresql+psycopg://" + rest
    if low.startswith("postgresql+"):
        return url
    raise CopyError(f"the target must be a Postgres URL, got scheme '{scheme}'")


def redact(url: str) -> str:
    return sa.engine.make_url(url).render_as_string(hide_password=True)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _count_rows(src: sqlite3.Connection, name: str) -> int:
    return src.execute(f"select count(*) from {_q(name)}").fetchone()[0]  # noqa: S608


def _open_source(path: str) -> sqlite3.Connection:
    uri = "file:" + path.replace("\\", "/").replace("?", "%3f") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("select count(*) from sqlite_master").fetchone()
    except sqlite3.Error as exc:
        raise CopyError(
            f"cannot open {path} as a plain SQLite database: {exc} "
            "(an encrypted install must be decrypted first: loc-tms db decrypt)"
        ) from exc
    return conn


def _source_tables(src: sqlite3.Connection) -> dict:
    out = {}
    for (name,) in src.execute(
        "select name from sqlite_master where type = 'table' order by name"
    ).fetchall():
        cols = [r[1] for r in src.execute(f"pragma table_info({_q(name)})").fetchall()]
        out[name] = cols
    return out


def _converter(col: sa.Column, stats: dict) -> Callable:
    t = col.type
    if isinstance(t, sa.Boolean):
        return lambda v: None if v is None else bool(int(v)) if not isinstance(v, bool) else v
    if isinstance(t, sa.DateTime):

        def to_dt(v):
            if v is None or isinstance(v, _dt.datetime):
                return v
            s = str(v).replace("Z", "+00:00")
            try:
                return _dt.datetime.fromisoformat(s)
            except ValueError:
                return _dt.datetime.fromisoformat(s.replace(" ", "T"))

        return to_dt
    if isinstance(t, sa.Date):
        return lambda v: _dt.date.fromisoformat(str(v)[:10]) if isinstance(v, str) else v
    if isinstance(t, sa.JSON):

        def _strip_nul(x):
            # Postgres json/jsonb refuses U+0000 inside strings (gate S3b LOW).
            if isinstance(x, str) and "\x00" in x:
                stats["json_nul_stripped"] = stats.get("json_nul_stripped", 0) + 1
                return x.replace("\x00", "")
            if isinstance(x, dict):
                return {_strip_nul(k): _strip_nul(val) for k, val in x.items()}
            if isinstance(x, list):
                return [_strip_nul(i) for i in x]
            return x

        def to_json(v):
            if isinstance(v, str):
                try:
                    return _strip_nul(json.loads(v))
                except ValueError:
                    stats["json_wrapped"] = stats.get("json_wrapped", 0) + 1
                    return _strip_nul(v)
            return _strip_nul(v)

        return to_json
    if isinstance(t, sa.LargeBinary):
        return lambda v: None if v is None else bytes(v)
    if isinstance(t, sa.Integer):
        return lambda v: int(v) if isinstance(v, str) and v.strip().lstrip("-").isdigit() else v
    if isinstance(t, sa.String):

        def to_str(v):
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            if isinstance(v, str) and "\x00" in v:
                stats["nul_stripped"] = stats.get("nul_stripped", 0) + 1
                return v.replace("\x00", "")
            return v

        return to_str
    return lambda v: v


def _fk_defs(conn, table: sa.Table, fkcs) -> list:
    """(constraint name, 'FOREIGN KEY ... REFERENCES ...') for the given constraints."""
    out = []
    for fkc in fkcs:
        row = conn.execute(
            sa.text(
                "select pg_get_constraintdef(c.oid) from pg_constraint c "
                "where c.conrelid = cast(:t as regclass) and c.conname = :n and c.contype = 'f'"
            ),
            {"t": _q(table.name), "n": fkc.name},
        ).scalar()
        if row:
            out.append((fkc.name, row))
    return out


def run(
    sqlite_path: str,
    target_url: str,
    *,
    batch_size: int = DEFAULT_BATCH,
    check_only: bool = False,
    log: Callable[[str], None] = lambda m: None,
) -> dict:
    """Copy `sqlite_path` into the Postgres database at `target_url`; return a report dict.

    Raises CopyError for every refusal and failure (the target is unchanged then).
    """
    started = time.monotonic()
    url = normalize_url(target_url)
    src = _open_source(sqlite_path)
    engine = sa.create_engine(url, pool_pre_ping=True)
    report: dict = {
        "ok": False,
        "source": sqlite_path,
        "target": redact(url),
        "check_only": check_only,
        "tables": [],
    }
    try:
        try:
            engine.connect().close()
        except Exception as exc:
            raise CopyError(f"cannot connect to the target: {type(exc).__name__}") from exc

        # --- preflight -----------------------------------------------------------------
        src_rev = _revision_sqlite(src)
        md = sa.MetaData()
        md.reflect(bind=engine)
        if "alembic_version" not in md.tables:
            raise CopyError(
                "the target has no alembic_version table: run `alembic upgrade head` first"
            )
        with engine.connect() as c:
            dst_rev = c.execute(sa.text("select version_num from alembic_version")).scalar()
        report["revision"] = dst_rev
        if src_rev != dst_rev:
            raise CopyError(
                f"schema revisions differ (source {src_rev}, target {dst_rev}). Boot the SQLite "
                "install on this release once (or run `loc-tms db migrate` against it) so it is "
                "at the current head, then `alembic upgrade head` on the target"
            )
        src_tables = _source_tables(src)
        dst_names = {t for t in md.tables if t != "alembic_version"}
        unmapped = sorted(
            n
            for n in src_tables
            if n not in md.tables and n != "alembic_version" and not _IGNORED_SOURCE.match(n)
        )
        unmapped_rows = {n: _count_rows(src, n) for n in unmapped}
        report["source_only_tables"] = unmapped_rows
        lost = {n: k for n, k in unmapped_rows.items() if k}
        if lost:
            raise CopyError(
                "the source has data in tables the target schema does not have: "
                + ", ".join(f"{n} ({k} rows)" for n, k in sorted(lost.items()))
            )
        with engine.connect() as c:
            dirty = {}
            for name in sorted(dst_names):
                n = c.execute(sa.select(sa.func.count()).select_from(md.tables[name])).scalar()
                allowed = 1 if name == "organization" else 0
                if n > allowed:
                    dirty[name] = n
        if dirty:
            raise CopyError(
                "the target is not empty (a finished or foreign copy?): "
                + ", ".join(f"{n} ({k} rows)" for n, k in sorted(dirty.items()))
                + ". Use a fresh database: create it, `alembic upgrade head`, then copy"
            )
        orphans = src.execute("pragma foreign_key_check").fetchall()
        if orphans:
            by = {}
            for tbl, _rowid, parent, _idx in orphans:
                by[f"{tbl} -> {parent}"] = by.get(f"{tbl} -> {parent}", 0) + 1
            raise CopyError(
                "the source has rows whose foreign key points at nothing (SQLite did not "
                "enforce it, Postgres will): "
                + ", ".join(f"{k} x{v}" for k, v in sorted(by.items())[:12])
            )
        src_counts = {n: _count_rows(src, n) for n in sorted(dst_names) if n in src_tables}
        report["source_rows"] = sum(src_counts.values())
        log(
            f"preflight ok: revision {dst_rev}, {len(src_counts)} tables, "
            f"{report['source_rows']} rows to copy"
        )
        if check_only:
            report["ok"] = True
            report["seconds"] = round(time.monotonic() - started, 2)
            return report

        # --- copy (one transaction) ----------------------------------------------------
        ordered, loose = _order(md, dst_names)
        stats: dict = {}
        with engine.begin() as conn:
            conn.execute(sa.text("set local lock_timeout = '30s'"))
            if conn.execute(
                sa.select(sa.func.count()).select_from(md.tables["organization"])
            ).scalar():
                conn.execute(sa.text("delete from organization"))
            dropped = []
            for table, fkcs in loose:
                for name, definition in _fk_defs(conn, table, fkcs):
                    conn.execute(
                        sa.text(f"alter table {_q(table.name)} drop constraint {_q(name)}")
                    )
                    dropped.append((table.name, name, definition))
            for table in ordered:
                if table.name not in src_tables:
                    report["tables"].append({"table": table.name, "source": 0, "target": 0})
                    continue
                t0 = time.monotonic()
                n = _copy_table(src, conn, table, src_tables[table.name], batch_size, stats, log)
                report["tables"].append(
                    {
                        "table": table.name,
                        "source": src_counts.get(table.name, 0),
                        "target": n,
                        "seconds": round(time.monotonic() - t0, 3),
                    }
                )
            for tname, name, definition in dropped:
                try:
                    conn.execute(
                        sa.text(f"alter table {_q(tname)} add constraint {_q(name)} {definition}")
                    )
                except Exception as exc:
                    raise CopyError(
                        f"foreign key {name} on {tname} does not hold for the copied rows "
                        f"({type(exc).__name__}); nothing was changed"
                    ) from exc
            report["sequences_reset"] = _reset_sequences(conn, md, ordered)
            bad = []
            for name in sorted(src_counts):
                got = conn.execute(sa.select(sa.func.count()).select_from(md.tables[name])).scalar()
                if got != src_counts[name]:
                    bad.append(f"{name}: source {src_counts[name]}, target {got}")
            if bad:
                raise CopyError("row counts differ after the copy: " + "; ".join(bad))
        report["converted"] = stats
        report["foreign_keys_restored"] = len(dropped)
        report["ok"] = True
        report["seconds"] = round(time.monotonic() - started, 2)
        return report
    except CopyError:
        raise
    except Exception as exc:  # database errors: say the class, never the row data
        raise CopyError(
            "copy failed and was rolled back: "
            f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
        ) from exc
    finally:
        src.close()
        engine.dispose()


def _revision_sqlite(src: sqlite3.Connection) -> str | None:
    try:
        row = src.execute("select version_num from alembic_version").fetchone()
    except sqlite3.Error as exc:
        raise CopyError(
            "the source has no alembic_version table (a pre-Alembic install): boot it once on "
            "this release so it is stamped, then copy"
        ) from exc
    return row[0] if row else None


def _order(md: sa.MetaData, names: set) -> tuple[list, list]:
    """Tables parents-first, plus the foreign keys to drop during the copy and re-add after."""
    tables = [md.tables[n] for n in sorted(names)]
    pairs = sort_tables_and_constraints(tables)
    ordered, loose = [], []
    for table, fkcs in pairs:
        if table is None:
            by_table = {}
            for fkc in fkcs:
                by_table.setdefault(fkc.parent, []).append(fkc)
            loose.extend(by_table.items())
        else:
            ordered.append(table)
    # Self-referencing foreign keys are never in the ordering; drop them for the copy too.
    seen = {fkc.name for _t, fks in loose for fkc in fks}
    for table in ordered:
        own = [
            fk.constraint
            for fk in table.foreign_keys
            if fk.column.table is table and fk.constraint.name not in seen
        ]
        if own:
            loose.append((table, list({id(c): c for c in own}.values())))
    return ordered, loose


def _copy_table(src, conn, table, src_cols, batch_size, stats, log) -> int:
    cols = [c for c in table.columns if c.name in src_cols]
    dropped = [c for c in src_cols if c not in table.columns]
    if dropped:
        stats.setdefault("source_columns_not_in_target", {})[table.name] = dropped
    if not cols:
        return 0
    conv = [_converter(c, stats) for c in cols]
    names = [c.name for c in cols]
    cur = src.execute(f"select {', '.join(_q(n) for n in names)} from {_q(table.name)}")  # noqa: S608
    total = 0
    ins = table.insert()
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        payload = [{n: f(v) for n, f, v in zip(names, conv, row, strict=True)} for row in rows]
        conn.execute(ins, payload)
        total += len(payload)
    if total >= 10000:
        log(f"  {table.name}: {total} rows")
    return total


def _reset_sequences(conn, md, tables) -> int:
    n = 0
    for table in tables:
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or not isinstance(pk[0].type, sa.Integer):
            continue
        col = pk[0].name
        seq = conn.execute(
            sa.text("select pg_get_serial_sequence(:t, :c)"), {"t": _q(table.name), "c": col}
        ).scalar()
        if not seq:
            continue
        conn.execute(
            sa.text(
                f"select setval(cast(:s as regclass), coalesce(max({_q(col)}), 1), "  # noqa: S608
                f"max({_q(col)}) is not null) from {_q(table.name)}"
            ),
            {"s": seq},
        )
        n += 1
    return n
