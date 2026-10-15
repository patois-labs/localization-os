"""D5 glossary and TM bounded-page acceptance, SQLite and optional migrated Postgres."""

import ast
import asyncio
import csv
import io
import json
import os
import sys
import time
from contextlib import (
    asynccontextmanager,
    closing,
    contextmanager,
    redirect_stderr,
    redirect_stdout,
)
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._api_cli_patch import patch_api_cli
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_glossscale_")
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, insert
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, get_session, init_db
from app.main import app
from app.models import Glossary, GlossaryEntry, GlossarySense, SegmentTM
from app.services import apikeys, content, glossary, security, settings, tm_module

_measuring = ContextVar("glossary_paging_measured_call", default=False)


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
    stats = {"queries": 0, "page_queries": 0, "entry_rows": 0, "tm_rows": 0, "other_rows": 0}

    def before(conn, cursor, statement, parameters, context, many):
        if not _measuring.get():
            return
        if statement.lstrip().upper().startswith("SELECT"):
            stats["queries"] += 1
            sql = " ".join(statement.lower().split())
            if "count(" not in sql and any(
                table in sql for table in (" from glossaryentry ", " from glossarysense ")
            ):
                stats["page_queries"] += 1

    def after(conn, cursor, statement, parameters, context, many):
        if not _measuring.get():
            return
        sql = " ".join(statement.lower().split())
        if sql.startswith("select"):
            kind = (
                "entry_rows"
                if " from glossaryentry " in sql and "count(" not in sql
                else "tm_rows"
                if " from segmenttm " in sql and "count(" not in sql
                else "other_rows"
            )
            context.cursor = CountingCursor(cursor, stats, kind)

    binds = [bind] if bind is engine else [bind, engine]
    for target in binds:
        event.listen(target, "before_cursor_execute", before)
        event.listen(target, "after_cursor_execute", after)
    # TestClient/AnyIO propagates this context into the real request worker.
    # Unrelated background jobs must not add SELECTs to a request measurement.
    measuring_token = _measuring.set(True)
    try:
        yield stats
    finally:
        _measuring.reset(measuring_token)
        for target in binds:
            event.remove(target, "before_cursor_execute", before)
            event.remove(target, "after_cursor_execute", after)


def build(session, count):
    p = content.project_create(
        session, "D5 glossary", source_locale="en-US", target_locales="es-ES"
    )
    pid = p.id
    first = (session.exec(select(func.max(GlossaryEntry.id))).one() or 0) + 1
    for start in range(0, count, 2000):
        indexes = range(start, min(start + 2000, count))
        session.execute(
            insert(GlossaryEntry),
            [
                {
                    "id": first + i,
                    "project_id": pid,
                    "source_locale": "en-US",
                    "target_locale": "es-ES",
                    "source_term": f"scale-term-{i:09d}",
                    "target_term": "target",
                }
                for i in indexes
            ],
        )
        session.execute(
            insert(GlossarySense),
            [
                {
                    "glossary_entry_id": first + i,
                    "sense_label": "sense",
                    "target_term": f"sense-{i}",
                }
                for i in indexes
            ],
        )
        session.execute(
            insert(SegmentTM),
            [
                {
                    "project_id": pid,
                    "source_locale": "en-US",
                    "target_locale": "es-ES",
                    "source_text": f"source-{i}",
                    "target_text": "target",
                    "source_hash": f"hash-{pid}-{i}",
                }
                for i in indexes
            ],
        )
    session.commit()
    return pid, first


def build_module(session, count):
    g = Glossary(name="D5 module")
    session.add(g)
    session.commit()
    gid = g.id
    session.execute(
        insert(GlossaryEntry),
        [
            {
                "glossary_id": gid,
                "source_locale": "en-US",
                "target_locale": "es-ES",
                "source_term": f"module-term-{i:09d}",
                "target_term": "target",
            }
            for i in range(count)
        ],
    )
    session.commit()
    return gid


def drill(bind, count, token):
    records = []
    with bind.connect() as connection:
        transaction = connection.begin()
        if bind.dialect.name == "sqlite":
            # SQLite legacy transaction mode otherwise commits the first SAVEPOINT.
            connection.exec_driver_sql("BEGIN")
        try:

            def session_factory(*args, **kwargs):
                return Session(connection, join_transaction_mode="create_savepoint")

            with session_factory() as session:
                pid, first = build(session, count)
                gid = build_module(session, count)
            for surface in ("glossary", "tm"):
                with session_factory() as session, measured(bind) as stats:
                    started = time.perf_counter()
                    if surface == "glossary":
                        page = glossary.page_entries(
                            session, project_id=pid, cursor=first - 1 or None, limit=100
                        )
                        assert len(page["entries"]) == 100 and page["has_more"]
                    else:
                        page = tm_module.list_entries(session, project_id=pid, page=2, per_page=100)
                        assert len(page["entries"]) == 100 and page["total"] == count
                    elapsed = time.perf_counter() - started
                record = dict(
                    dialect=bind.dialect.name, K=count, surface=surface, wall_s=elapsed, **stats
                )
                records.append(record)
                print("D5 " + json.dumps(record, sort_keys=True))

            def override_session():
                with session_factory() as session:
                    yield session

            app.dependency_overrides[get_session] = override_session
            try:
                with (
                    patch_api_cli("Session", session_factory),
                    # Keep real SQLite key reads/stamps inside the rollback fixture.
                    patch(
                        "app.services.apikeys.Session",
                        session_factory if bind is engine else Session,
                    ),
                    # Hold the warmed settings cache steady across measured requests.
                    patch.object(settings, "CHECK_INTERVAL_SECONDS", 3600),
                    closing(TestClient(app, headers={"X-Loc-Token": token})) as client,
                ):
                    for surface, url, params, bound in (
                        ("api_project", f"/api/projects/{pid}/glossary", {"limit": 100}, 101),
                        ("api_module", f"/api/glossaries/{gid}", {"limit": 100}, 101),
                        ("web_project", f"/projects/{pid}", {}, 201),
                        ("web_module", f"/glossaries/{gid}", {}, 201),
                        ("api_module_default", f"/api/glossaries/{gid}", {}, 1001),
                    ):
                        assert client.get(url, params=params).status_code == 200
                        with measured(bind) as stats:
                            started = time.perf_counter()
                            response = client.get(url, params=params)
                            elapsed = time.perf_counter() - started
                        assert response.status_code == 200, response.text[:300]
                        assert stats["entry_rows"] <= bound and stats["page_queries"] == 2
                        if surface == "api_module_default":
                            body = response.json()
                            assert len(body["terms"]) == 1000
                            assert bool(body["next_cursor"]) == (count > 1000)
                            assert body["entry_count"] == count
                            assert client.get(url, params={"limit": 1001}).status_code == 400
                        record = dict(
                            dialect=bind.dialect.name,
                            K=count,
                            surface=surface,
                            wall_s=elapsed,
                            **stats,
                        )
                        records.append(record)
                        print("D5 " + json.dumps(record, sort_keys=True))
            finally:
                app.dependency_overrides.pop(get_session, None)
        finally:
            transaction.rollback()
    return records


def surfaces(token):
    with Session(engine) as session:
        pid, first = build(session, 505)
        gid = build_module(session, 505)
    failures = []
    with closing(TestClient(app, headers={"X-Loc-Token": token})) as client:

        def api():
            r = client.get(
                f"/api/projects/{pid}/glossary", params={"limit": 2, "cursor": first + 1}
            )
            assert r.status_code == 200, r.text
            b = r.json()
            assert [e["id"] for e in b["entries"]] == [first + 2, first + 3] and b.get(
                "next_cursor"
            ) == first + 3, "project API cursor ignored"

        def module_api():
            r = client.get(f"/api/glossaries/{gid}", params={"limit": 2, "cursor": ""})
            assert r.status_code == 200, r.text
            assert len(r.json()["terms"]) == 2 and r.json().get("next_cursor"), (
                "module API page ignored"
            )

        def cli():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(
                    [
                        "--json",
                        "glossary",
                        "list",
                        str(pid),
                        "--limit",
                        "2",
                        "--cursor",
                        str(first + 1),
                    ]
                )
            assert rc == 0, "CLI cursor rejected"
            b = json.loads(buf.getvalue())
            assert [e["id"] for e in b["entries"]] == [first + 2, first + 3] and b.get(
                "next_cursor"
            ) == first + 3, "CLI cursor ignored"

        def compatibility():
            b = client.get(f"/api/projects/{pid}/glossary", params={"offset": 2, "limit": 2}).json()
            assert [e["id"] for e in b["entries"]] == [first + 2, first + 3]
            assert (b["limit"], b["offset"], b["truncated"]) == (2, 2, True)
            b = client.get(f"/api/projects/{pid}/glossary").json()
            assert len(b["entries"]) == 505 and (b["limit"], b["offset"], b["truncated"]) == (
                1000,
                0,
                False,
            )
            assert len(client.get(f"/api/glossaries/{gid}").json()["terms"]) == 505
            for url in (f"/api/projects/{pid}/glossary", f"/api/glossaries/{gid}"):
                over_cap = client.get(url, params={"limit": 1001})
                assert over_cap.status_code == 400
                assert over_cap.json() == {"ok": False, "error": "limit must be <= 1000."}
                assert client.get(url, params={"limit": 0}).status_code == 400
                assert client.get(url, params={"offset": -1}).status_code == 400
                b = client.get(url, params={"limit": 2, "cursor": first + 1, "offset": 1}).json()
                assert b["offset"] == 1
            with Session(engine) as session:
                entries = glossary.list_entries(session, pid)
                assert len(entries) == 505
                assert len(list(glossary.iter_entries(session, project_id=pid, chunk=2))) == 505
                with measured(engine) as stats:
                    assert glossary.count(session, pid) == 505
                assert stats["queries"] == 1 and stats["entry_rows"] == 0
                from app.services import glossary_module as gm

                with measured(engine) as stats:
                    assert gm.entry_count(session, gid) == 505
                assert stats["queries"] == 1 and stats["entry_rows"] == 0
                rows = list(csv.DictReader(io.StringIO(glossary.export_csv(session, pid).decode())))
                assert len(rows) == 1010 and rows[-1]["source_term"] == "scale-term-000000504"
                from lxml import etree

                from app.services import glossary_tbx

                doc = etree.fromstring(glossary_tbx.export_tbx(session, pid))
                assert len(doc.findall(".//{*}conceptEntry")) == 505

        def invalid_cursor():
            for url, key in (
                (f"/api/projects/{pid}/glossary", "cursor"),
                (f"/api/glossaries/{gid}", "cursor"),
                (f"/projects/{pid}", "glossary_cursor"),
                (f"/glossaries/{gid}", "glossary_cursor"),
            ):
                for bad in ("-5", "abc", "1e9", "0"):
                    response = client.get(url, params={key: bad})
                    assert response.status_code == 400
                    assert response.json() == {
                        "ok": False,
                        "error": "cursor must be a positive integer",
                    }
                assert client.get(url, params={key: ""}).status_code == 200
            for command in (
                ["glossary", "list", str(pid)],
                ["glossary", "module", "show", str(gid)],
            ):
                for bad in ("-5", "abc", "1e9", "0"):
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        try:
                            rc = cli_main([*command, "--cursor", bad])
                        except SystemExit as exc:
                            rc = exc.code
                    assert rc == 2
                    assert "cursor must be a positive integer" in stderr.getvalue()

        def large_position():
            for param in ("offset", "cursor"):
                b = client.get(f"/api/projects/{pid}/glossary", params={param: 10**30}).json()
                assert b["entries"] == [] and b["truncated"] is False
                if param == "offset":
                    assert b["offset"] == 10**30

        def remote_cli():
            class Remote:
                def get(self, url, params=None):
                    r = client.get(url, params=params)
                    assert r.status_code == 200, r.text
                    return r.json()

            for command, field in [
                (["glossary", "list", str(pid)], "entries"),
                (["glossary", "module", "show", str(gid)], "terms"),
            ]:
                results = []
                for remote in (False, True):
                    argv = (
                        ["--json"]
                        + (["--remote", "http://127.0.0.1"] if remote else [])
                        + command
                        + ["--limit", "2"]
                    )
                    buf = io.StringIO()
                    with (
                        patch_pieces("app.cli._remote_impl", "_client", return_value=Remote()),
                        redirect_stdout(buf),
                    ):
                        assert cli_main(argv) == 0
                    results.append(json.loads(buf.getvalue()))
                assert results[0] == results[1] and len(results[0][field]) == 2
                assert results[0]["next_cursor"] == results[0][field][-1]["id"]

        def mcp():
            # Execute the real read handler without importing the optional MCP SDK.
            source = (_ROOT / "integrations/mcp-server/server.py").read_text(encoding="utf-8")
            node = next(
                n
                for n in ast.parse(source).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "list_assets"
            )

            @asynccontextmanager
            async def local_client():
                yield client

            async def get_json(c, url, params=None):
                response = c.get(url, params=params)
                assert response.status_code == 200, response.text
                return response.json()

            namespace = {"json": json, "_client": local_client, "_get_json": get_json}
            exec(
                compile(ast.Module(body=[node], type_ignores=[]), "list_assets", "exec"), namespace
            )
            fn = namespace["list_assets"]
            first_page = json.loads(
                asyncio.run(fn(view="glossary", glossary_id=gid, limit=2, cursor=None))
            )["glossary"]
            assert len(first_page["terms"]) == 2 and first_page["next_cursor"]
            second = json.loads(
                asyncio.run(
                    fn(
                        view="glossary",
                        glossary_id=gid,
                        limit=2,
                        cursor=first_page["next_cursor"],
                        offset=1,
                    )
                )
            )["glossary"]
            assert (
                len(second["terms"]) == 2
                and second["terms"][0]["id"] == first_page["next_cursor"] + 2
            )
            assert (
                len(
                    json.loads(asyncio.run(fn(view="glossary", glossary_id=gid)))["glossary"][
                        "terms"
                    ]
                )
                == 505
            )

        def web(path, prefix):
            banner = {"gl_entries": "505", "gl_senses": "2", "gl_changed": "2", "gl_skipped": "3"}
            r = client.get(path, params=banner)
            if prefix == "module-term-":
                assert "Terms (200 of 505)" in r.text
            assert r.status_code == 200, r.text[:300]
            term_html = (
                r.text.split("<h2>Terms (", 1)[-1].split("</table>", 1)[0]
                if prefix == "module-term-"
                else r.text
            )
            assert (
                prefix + "000000199" in term_html
                and prefix + "000000200" not in term_html
                and "glossary_cursor=" in r.text
            ), "web glossary page unbounded or next link missing"
            import re

            link = re.search(r'href="([^"]*glossary_cursor=[^"]*)"', r.text)
            assert link, "next link missing"
            from urllib.parse import parse_qs, urlparse

            next_url = link.group(1).replace("&amp;", "&")
            assert all(parse_qs(urlparse(next_url).query)[k] == [v] for k, v in banner.items())
            assert r.text.index("Next glossary terms") > r.text.index(prefix + "000000199")
            r = client.get(next_url)
            assert "First page</a>" in r.text
            banner_text = (
                "Imported 505 entries" if prefix == "module-term-" else "added 505 term(s)"
            )
            assert banner_text in r.text
            if prefix == "module-term-":
                assert "Terms (200 of 505)" in r.text
            first_link = re.search(r'href="([^"]*)">First page</a>', r.text)
            first_url = first_link.group(1).replace("&amp;", "&")
            assert all(parse_qs(urlparse(first_url).query)[k] == [v] for k, v in banner.items())
            assert prefix + "000000000" in client.get(first_url).text
            term_html = (
                r.text.split("<h2>Terms (", 1)[-1].split("</table>", 1)[0]
                if prefix == "module-term-"
                else r.text
            )
            assert prefix + "000000200" in term_html and prefix + "000000199" not in term_html, (
                "web next page wrong"
            )
            if prefix == "module-term-":
                final_link = re.search(r'href="([^"]*)">Next glossary terms</a>', r.text)
                final = client.get(final_link.group(1).replace("&amp;", "&"))
                assert "Terms (105 of 505)" in final.text
                assert "Next glossary terms</a>" not in final.text
                assert "First page</a>" in final.text

        for name, fn in [
            ("project API", api),
            ("module API", module_api),
            ("CLI", cli),
            ("compatibility and exports", compatibility),
            ("remote CLI", remote_cli),
            ("large position", large_position),
            ("invalid cursor", invalid_cursor),
            ("MCP", mcp),
            ("project web", lambda: web(f"/projects/{pid}", "scale-term-")),
            ("module web", lambda: web(f"/glossaries/{gid}", "module-term-")),
        ]:
            try:
                fn()
            except (AssertionError, SystemExit) as exc:
                failures.append(f"RED {name}: {exc}")
    return failures


def main():
    registration = (_ROOT / "tests/test_verify_suites.py").read_text(encoding="utf-8")
    assert 'assert "verify_glossary_paging_scale" in' in registration, (
        "D5 suite needs explicit registration guard"
    )
    init_db()
    # Start the real app before inserting scale fixtures; repeated startup runs
    # upgrade backfills, which are outside the steady-state paging measurement.
    with TestClient(app):
        pass
    settings.set_many({"security_auth_required": "0"})
    test_password = "glossary-test-password-123"  # pregate: allow-secret fixture
    user = security.create_user("glossscale@example.test", "D5", "admin", test_password)
    _, token = apikeys.create_key(user.id, "D5 scale", scopes="api docs")
    records = []
    sizes = (1000, max(1001, int(os.environ.get("GLOSSARY_PAGING_SCALE", "20000"))))
    for size in sizes:
        records.extend(drill(engine, size, token))
    url = os.environ.get("LOC_TEST_DATABASE_URL", "")
    if url:
        assert url.startswith("postgres"), "LOC_TEST_DATABASE_URL must be Postgres"
        pg = create_engine(url)
        try:
            for size in sizes:
                records.extend(drill(pg, size, token))
        finally:
            pg.dispose()
    else:
        print(
            "POSTGRES SKIPPED: LOC_TEST_DATABASE_URL unset; "
            "orchestrator runs the Postgres drill later"
        )
    # Include a precise 3000-term cap pin before the independent surface fixtures.
    records.extend(drill(engine, 3000, token))
    failures = surfaces(token)
    for r in records:
        if r["surface"] not in ("glossary", "tm"):
            peers = [
                p for p in records if (p["dialect"], p["surface"]) == (r["dialect"], r["surface"])
            ]
            assert len({p["queries"] for p in peers}) == 1, peers
            continue
        field, bound = ("entry_rows", 101) if r["surface"] == "glossary" else ("tm_rows", 100)
        if r["queries"] != 2 or r[field] > bound:
            failures.append(
                f"RED {r['surface']} K={r['K']}: {r['queries']} SELECTs / {r[field]} rows; "
                f"expected 2 SELECTs / <= {bound} rows"
            )
    for failure in failures:
        print(failure)
    assert not failures, "glossary paging acceptance failed"
    evidence = os.environ.get("GLOSSARY_PAGING_EVIDENCE")
    if evidence:
        Path(evidence).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    print("GLOSSARY PAGING SCALE OK")


if __name__ == "__main__":
    main()
