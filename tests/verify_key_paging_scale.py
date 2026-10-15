"""D5: identical bounded-page drill on SQLite and optional migrated Postgres.

Run through test_verify_suites.py; F18_SCALE_KEYS=1000000 enables the full drill.
LOC_TEST_DATABASE_URL must name a disposable, migrated Postgres test database.
The Postgres fixture is rolled back; no schema is installed by this suite.
"""

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

init_env("loctms_keyscale_")
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, insert
from sqlmodel import Session, select

from app.db import engine, get_session, init_db
from app.main import app
from app.models import ProjectType, Segment, StringKey
from app.services import apikeys, content, keys, security, settings


class CountingCursor:
    """Count actual DBAPI fetches, not rowcount (-1 for SQLite SELECTs)."""

    def __init__(self, cursor, stats, kind):
        self.cursor, self.stats, self.kind = cursor, stats, kind

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def fetchone(self):
        row = self.cursor.fetchone()
        self.stats[self.kind] += int(row is not None)
        return row

    def fetchmany(self, *args):
        rows = self.cursor.fetchmany(*args)
        self.stats[self.kind] += len(rows)
        return rows

    def fetchall(self):
        rows = self.cursor.fetchall()
        self.stats[self.kind] += len(rows)
        return rows


@contextmanager
def measured(bind):
    stats = {"queries": 0, "key_rows": 0, "value_rows": 0, "other_rows": 0}

    def before(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            stats["queries"] += 1

    def after(conn, cursor, statement, parameters, context, many):
        sql = " ".join(statement.lower().split())
        if sql.startswith("select"):
            kind = (
                "key_rows"
                if " from stringkey " in sql
                else "value_rows"
                if " from segment " in sql
                else "other_rows"
            )
            context.cursor = CountingCursor(cursor, stats, kind)

    # Keep the settings version probe OUT of the counted window. This suite already says
    # unrelated settings SELECTs "do not belong to the page query contract" and warms the
    # cache once up front, but `settings.CHECK_INTERVAL_SECONDS` is 2.0 and `_probe_version`
    # opens its OWN session, so any counted window that straddles the interval costs exactly
    # one extra SELECT. That is wall-clock dependent, not code dependent: this suite passes
    # alone in 13s and fails inside a 47-minute sweep with queries=N+1 and every other
    # counter identical. Diagnosed 2026-09-20 by forcing the probe due, which reproduces the
    # sweep failure byte for byte.
    #
    # Marking the version freshly checked here makes the suite's existing intent actually
    # hold, rather than holding only when the machine is idle. A suite that false-reds under
    # load is one the next person learns to wave away, which is the same failure mode as a
    # ratchet that cries wolf.
    settings._checked_at = time.monotonic()
    settings._probing = False

    binds = [bind] if bind is engine else [bind, engine]
    for target in binds:
        event.listen(target, "before_cursor_execute", before)
        event.listen(target, "after_cursor_execute", after)
    try:
        yield stats
    finally:
        for target in binds:
            event.remove(target, "before_cursor_execute", before)
            event.remove(target, "after_cursor_execute", after)


SPECIAL_NAMES = ["z", "A", "\u00e9", "\u00c9", "\u4e2d", "literal%_", "literalXY"]


def dialect_semantics(session, pid):
    rows = keys.list_keys(session, pid, namespace="special")
    assert [k.name for k in rows] == sorted(SPECIAL_NAMES)
    assert [k.name for k in keys.list_keys(session, pid, q="literal%_")] == ["literal%_"]
    names = [k.name for k in keys.list_keys(session, pid, q="\u00e9")]
    assert names == ["\u00c9", "\u00e9"]
    assert keys.search_keys(session, pid, q="z", untranslated_in="de-DE")
    assert keys.search_keys(session, pid, q="A", untranslated_in="de-DE")


def build(session, count):
    started = time.perf_counter()
    p = content.project_create(
        session,
        "F18 scale",
        source_locale="en-US",
        target_locales="de-DE,fr-FR",
        project_type=ProjectType.KEY_BASED,
    )
    pid = p.id
    did = keys.internal_document(session, p, "").id
    first = (session.exec(select(func.max(StringKey.id))).one() or 0) + 1
    for start in range(0, count, 2000):
        stop = min(start + 2000, count)
        session.execute(
            insert(StringKey),
            [
                {
                    "id": first + i,
                    "project_id": pid,
                    "name": SPECIAL_NAMES[i] if i < len(SPECIAL_NAMES) else f"key-{i:09d}",
                    "namespace": "special" if i < len(SPECIAL_NAMES) else "",
                    "tags": ["scale"],
                }
                for i in range(start, stop)
            ],
        )
        session.execute(
            insert(Segment),
            [
                {
                    "document_id": did,
                    "key_id": first + i,
                    "order_index": i,
                    "target_locale": loc,
                    "source_text": "Source",
                    "target_text": "\u00a0" if i == 0 else " \t\n\r\v\f",
                    "status": "new",
                }
                for i in range(start, stop)
                for loc in ("de-DE", "fr-FR")
            ],
        )
    session.commit()
    return pid, first, time.perf_counter() - started


def drill(bind, count, token):
    # The same fixture and real route body run on both dialects. Savepoints keep
    # service commits inside the outer rollback, like pg_smoke's functional slice.
    with bind.connect() as connection:
        transaction = connection.begin()

        def session_factory():
            return Session(connection, join_transaction_mode="create_savepoint")

        def dependency():
            with session_factory() as session:
                yield session

        app.dependency_overrides[get_session] = dependency
        try:
            with session_factory() as session:
                pid, first, build_s = build(session, count)
                dialect_semantics(session, pid)
            records = []
            with TestClient(app, headers={"X-Loc-Token": token}) as client:
                # Warm global settings caches, as unrelated settings SELECTs do not
                # belong to the page query contract. Project/payload still load fresh.
                warm = client.get(f"/api/projects/{pid}/keys?limit=1")
                assert warm.status_code == 200, warm.text[:300]
                for cursor in (first - 1, first + count // 2):
                    with session_factory() as session, measured(bind) as stats:
                        start = time.perf_counter()
                        page = keys.page_keys(
                            session,
                            pid,
                            cursor=cursor,
                            limit=100,
                            tag="scale",
                            status="new",
                            untranslated_in="de-DE",
                        )
                        elapsed = time.perf_counter() - start
                    assert len(page["keys"]) == 100 and page["has_more"]
                    assert stats["queries"] == 1 and stats["key_rows"] == 101, stats
                    records.append(
                        dict(
                            dialect=bind.dialect.name,
                            K=count,
                            surface="service",
                            cursor=cursor,
                            build_s=build_s,
                            wall_s=elapsed,
                            **stats,
                        )
                    )
                    with measured(bind) as stats:
                        start = time.perf_counter()
                        response = client.get(
                            f"/api/projects/{pid}/keys",
                            params={
                                "limit": 100,
                                "cursor": cursor,
                                "tag": "scale",
                                "status": "new",
                                "untranslated_in": "de-DE",
                            },
                        )
                        elapsed = time.perf_counter() - start
                    assert response.status_code == 200, response.text[:300]
                    payload = response.json()
                    assert len(payload["keys"]) == 100 and payload["has_more"]
                    assert [k["id"] for k in payload["keys"]] == [k.id for k in page["keys"]]
                    assert stats["key_rows"] == 101 and stats["value_rows"] == 400, stats
                    assert stats["queries"] == 9, stats
                    records.append(
                        dict(
                            dialect=bind.dialect.name,
                            K=count,
                            surface="api",
                            cursor=cursor,
                            build_s=build_s,
                            wall_s=elapsed,
                            **stats,
                        )
                    )
            for record in records:
                print("D5 " + json.dumps(record, sort_keys=True))
            return records
        finally:
            app.dependency_overrides.pop(get_session, None)
            transaction.rollback()


if __name__ == "__main__":
    registration = (_ROOT / "tests/test_verify_suites.py").read_text(encoding="utf-8")
    assert 'assert "verify_key_paging_scale" in' in registration, (
        "D5 suite needs explicit registration guard"
    )
    init_db()
    settings.set_many({"key_projects_enabled": "1", "security_auth_required": "0"})
    count = int(os.environ.get("F18_SCALE_KEYS", "20000"))
    test_password = "f18-test-password-123"  # pregate: allow-secret offline fixture
    user = security.create_user("f18@example.test", "F18", "admin", test_password)
    _, token = apikeys.create_key(user.id, "F18 scale", scopes="api keys")
    records = []
    # Independent K values make query independence an assertion in every run.
    for size in (1000, count):
        records.extend(drill(engine, size, token))
    url = os.environ.get("LOC_TEST_DATABASE_URL", "")
    if not url:
        print(
            "POSTGRES SKIPPED: LOC_TEST_DATABASE_URL unset; "
            "orchestrator runs the Postgres drill later"
        )
    else:
        assert url.startswith("postgres"), "LOC_TEST_DATABASE_URL must be Postgres"
        pg = create_engine(url)
        try:
            for size in (1000, count):
                records.extend(drill(pg, size, token))
        finally:
            pg.dispose()
    if os.environ.get("F18_SCALE_RECORD"):
        Path(os.environ["F18_SCALE_RECORD"]).write_text(
            json.dumps(records, indent=2) + "\n", encoding="utf-8"
        )
    print("KEY PAGING SCALE OK")
