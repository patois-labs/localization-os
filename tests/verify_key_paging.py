"""F18 SQL paging regression and Python semantic oracle."""

import os
import random
import sys
from contextlib import contextmanager
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_keypaging_")
from sqlalchemy import event, insert
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import ProjectType, Segment, StringKey
from app.services import content, keys, settings


@contextmanager
def measured(session):
    stats = {"queries": 0, "keys": 0, "sql": []}

    def before(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            stats["queries"] += 1
            stats["sql"].append(statement)

    def loaded(session, obj):
        if isinstance(obj, StringKey):
            stats["keys"] += 1

    event.listen(session.bind, "before_cursor_execute", before)
    event.listen(session, "loaded_as_persistent", loaded)
    try:
        yield stats
    finally:
        event.remove(session.bind, "before_cursor_execute", before)
        event.remove(session, "loaded_as_persistent", loaded)


# SELECTs for one keys-grid page (GET /projects/{pid}/keys), layout included. 18 since the
# release of 2026-10-13: the New project dialog in base.html (new_project_ctx) reads the
# custom field definitions (customfielddef) once per page, like it reads content types.
# A change here must name the new query; the failure message lists every table read.
GRID_QUERIES = 18


def _sql_tables(stats):
    import re

    return [
        stats["queries"],
        [
            (re.findall(r"\bFROM\s+(\w+)", sql) or re.findall(r"SELECT\s+(\w+)\.", sql) or ["?"])[0]
            for sql in stats["sql"]
        ],
    ]


def oracle(rows, values, **f):
    out = []
    for k in rows:
        if not f.get("include_deleted", False) and k.deleted_at:
            continue
        if f.get("namespace") is not None and k.namespace != f["namespace"]:
            continue
        if not f.get("include_archived", True) and k.is_archived:
            continue
        if not f.get("include_hidden", True) and k.is_hidden:
            continue
        if f.get("tag") and f["tag"] not in (k.tags or []):
            continue
        if f.get("q") and not any(
            str(f["q"]).lower() in (v or "").lower() for v in (k.name, k.description, k.context)
        ):
            continue
        segs = values.get(k.id, [])
        if f.get("status") and not any(s.status == f["status"] for s in segs):
            continue
        if f.get("untranslated_in") and not any(
            s.target_locale == f["untranslated_in"] and not (s.target_text or "").strip()
            for s in segs
        ):
            continue
        out.append(k)
    return sorted(out, key=lambda k: (k.namespace or "", k.name or "", k.context or "", k.id))


def fixture():
    init_db()
    settings.set_many({"key_projects_enabled": "1", "security_auth_required": "0"})
    rng = random.Random(18)
    with Session(engine) as s:
        p = content.project_create(
            s,
            "F18",
            source_locale="en-US",
            target_locales="de-DE,fr-FR",
            project_type=ProjectType.KEY_BASED,
        )
        pid = p.id
        doc = keys.internal_document(s, p, "")
        did = doc.id
        s.execute(
            insert(StringKey),
            [
                {
                    "project_id": pid,
                    "name": rng.choice(["Z", "a", "É", "é", "中", "100%_\\"]) + str(i),
                    "namespace": rng.choice(["", "A", "z"]),
                    "context": rng.choice(["", "note", "MATCH"]),
                    "description": rng.choice(["", "match", "%_"]),
                    "tags": rng.choice([None, [], ["red"], ["redder", "blue"]]),
                    "is_hidden": i % 7 == 0,
                    "is_archived": i % 11 == 0,
                    "deleted_at": datetime(2020, 1, 1) if i % 17 == 0 else None,
                }
                for i in range(5000)
            ],
        )
        ids = s.exec(
            select(StringKey.id).where(StringKey.project_id == pid).order_by(StringKey.id)
        ).all()
        s.execute(
            insert(Segment),
            [
                {
                    "document_id": did,
                    "key_id": kid,
                    "order_index": i,
                    "target_locale": loc,
                    "source_text": "Source",
                    "target_text": " \t\n\r\v\f" if i % 3 == 0 else "Text",
                    "status": "new"
                    if (i % 2 == 0 and (i % 4 != 0 or loc == "fr-FR"))
                    else "approved",
                }
                for i, kid in enumerate(ids)
                for loc in ("de-DE", "fr-FR")
                if i != 4999
            ],
        )
        s.commit()
    return pid


def check_list(pid):
    with Session(engine) as s:
        original = s.exec(select(StringKey).where(StringKey.project_id == pid)).all()
    combinations = [
        {},
        {"namespace": ""},
        {"tag": "red"},
        {"q": "%_"},
        {"q": "MATCH"},
        {"include_archived": False, "include_hidden": False},
        {
            "namespace": "A",
            "tag": "blue",
            "q": "note",
            "include_deleted": True,
            "include_archived": False,
            "include_hidden": False,
        },
    ]
    from itertools import product

    combinations.extend(
        dict(
            zip(
                ("namespace", "tag", "q", "include_deleted", "include_archived", "include_hidden"),
                values,
                strict=True,
            )
        )
        for values in product(
            (None, "", "A"),
            (None, "red"),
            (None, "MATCH", "%_"),
            (False, True),
            (False, True),
            (False, True),
        )
    )
    for f in combinations:
        expected = oracle(original, {}, **f)[3:13]
        with Session(engine) as s, measured(s) as stats:
            actual = keys.list_keys(s, pid, offset=3, limit=10, **f)
            assert [k.id for k in actual] == [k.id for k in expected], f
            assert stats["queries"] == 1, stats
            assert stats["keys"] == len(actual), stats
            assert "LIMIT" in stats["sql"][0] and "ORDER BY" in stats["sql"][0], stats
    print("item 1 list: 1 query; bounded row loads; oracle parity")


FILTERS = [
    {"status": "approved"},
    {"untranslated_in": "de-DE"},
    {
        "status": "new",
        "untranslated_in": "fr-FR",
        "namespace": "",
        "tag": "red",
        "q": "note",
        "include_archived": False,
        "include_hidden": False,
    },
    {"status": "absent"},
]


def expected_search(pid, filters):
    with Session(engine) as s:
        rows = s.exec(select(StringKey).where(StringKey.project_id == pid)).all()
        values = {}
        for seg in s.exec(select(Segment)).all():
            values.setdefault(seg.key_id, []).append(seg)
        return oracle(rows, values, **filters)


def check_search(pid):
    for filters in FILTERS:
        expected = expected_search(pid, filters)
        with Session(engine) as s, measured(s) as stats:
            actual = keys.search_keys(s, pid, **filters)
            assert [k.id for k in actual] == [k.id for k in expected]
            assert stats["queries"] == 1, stats["queries"]
    print("item 2 search: 1 query; correlated filters match oracle")


def check_pages(pid):
    for filters in [{}, *FILTERS]:
        expected = sorted(k.id for k in expected_search(pid, filters))
        actual = []
        cursor = 0
        while True:
            with Session(engine) as s, measured(s) as stats:
                page = keys.page_keys(s, pid, cursor=cursor, limit=100, **filters)
                ids = [k.id for k in page["keys"]]
                assert stats["queries"] == 1, stats
                assert stats["keys"] == len(ids) + int(page["has_more"]), stats
                assert ids == expected[len(actual) : len(actual) + 100]
                actual.extend(ids)
                assert page["has_more"] == (len(actual) < len(expected))
                cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        assert actual == expected
    print("item 3 pages: 1 query, <=101 key loads, complete filtered sequence")


def legacy_view(session: Session, key, locales=None) -> dict:
    """ONE key rendered as a plain dict -- THE shared payload behind the web grid,
    the `/api` key routes and `keys list --json`, so the three surfaces cannot drift
    into describing the same key differently (the parity law, mechanized).

    `values` is keyed by target locale; each entry lists one row per plural form with
    its text, status and qa_flags. `base` is the source-locale value(s)."""
    rows = keys.values_for(session, key)
    if locales is not None:
        wanted = set(locales)
        rows = [s for s in rows if s.target_locale in wanted]
    # K8 read-time derived check (spec Â§9): write-time gating only judges a value when it
    # is NEXT written, so an existing over-limit row would stay invisible until touched.
    # `key_view` already loads every value row, so ONE len() per row yields a DERIVED
    # `over_limit` the grid, /api and `keys list --json` render on the first page load
    # after deploy â€” no rows modified, no statuses changed, no delta entries emitted. A
    # pure len() comparison (not a heuristic), so the false-positive risk is near zero.
    from app.services import quality

    lim = quality.coerce_char_limit(key.char_limit)
    values: dict[str, list] = {}
    for seg in rows:
        entry = {
            "segment_id": seg.id,
            "plural_category": seg.plural_category or "",
            "text": seg.target_text or "",
            "status": seg.status,
            "qa_flags": list(seg.qa_flags or []),
            # TM-matching lock (PROPOSAL-tm-matching D-1): surface the lock in the key
            # grid. None on every unlocked value = byte-identical.
            "lock_band": seg.lock_band,
            "lock_hard": bool(seg.lock_hard),
        }
        if lim is not None:
            n = quality.char_count(seg.target_text or "")
            if n > lim:
                entry["over_limit"] = {"length": n, "limit": lim}
        values.setdefault(seg.target_locale, []).append(entry)
    return {
        "id": key.id,
        "project_id": key.project_id,
        "namespace": key.namespace or "",
        "name": key.name,
        "context": key.context or "",
        "description": key.description or "",
        "char_limit": key.char_limit,
        "is_plural": bool(key.is_plural),
        "plural_arg_name": key.plural_arg_name,
        "tags": list(key.tags or []),
        "source_file": key.source_file or "",
        "is_hidden": bool(key.is_hidden),
        "is_archived": bool(key.is_archived),
        "custom": dict(key.custom or {}),
        "deleted": key.deleted_at is not None,
        "created_at": key.created_at.isoformat() if key.created_at else "",
        "updated_at": key.updated_at.isoformat() if key.updated_at else "",
        "translations_modified_at": (
            key.translations_modified_at.isoformat() if key.translations_modified_at else ""
        ),
        "base": keys.base_values(session, key),
        "values": values,
    }


def check_views(pid):
    with Session(engine) as s:
        page = keys.page_keys(s, pid, limit=10, include_deleted=True)["keys"]
        page[0].is_plural = True
        page[0].char_limit = 1
        seg = keys.values_for(s, page[0])[0]
        seg.plural_category = "other"
        seg.lock_band = "100"
        seg.lock_hard = True
        seg.qa_flags = ["source_changed"]
        for loc in ("de-DE", "fr-FR"):
            s.add(
                Segment(
                    document_id=seg.document_id,
                    key_id=page[0].id,
                    order_index=6000,
                    target_locale=loc,
                    plural_category="one",
                    source_text="One",
                    target_text="One translated",
                    status="approved",
                )
            )
        s.commit()
        expected = [legacy_view(s, k) for k in page]
        with measured(s) as stats:
            actual = keys.key_views(s, page, project_id=pid)
            assert actual == expected
            assert stats["queries"] == 2, stats
        for k in page:
            assert (
                keys.key_views(s, [k], ["fr-FR"], project_id=pid)
                == [keys.key_view(s, k, ["fr-FR"])]
                == [legacy_view(s, k, ["fr-FR"])]
            )
    print("item 4 views: 2 queries; legacy payload parity")


def check_grid(pid):
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlparse

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import settings as _settings

    # The settings cache probes `__settings_version__` once CHECK_INTERVAL_SECONDS (2 s)
    # have passed, so on a loaded machine the probe landed inside the measured request
    # and counted an 18th query. Hold the interval open for the measurement, the same
    # way verify_glossary_paging_scale does; the count is about the grid, not the clock.
    with (
        TestClient(app) as client,
        patch.object(_settings, "CHECK_INTERVAL_SECONDS", 3600),
    ):
        client.get(f"/projects/{pid}/keys?limit=1")
        with Session(engine) as s, measured(s) as stats:
            response = client.get(f"/projects/{pid}/keys")
        assert response.status_code == 200, response.text[:200]
        assert response.context["shown"] == 200, response.context["shown"]
        assert response.context["has_more"]
        assert "showing 200 of 4,277, more available" in response.text
        assert stats["queries"] == GRID_QUERIES, _sql_tables(stats)
        if os.environ.get("F18_GRID_RECORD"):
            from pathlib import Path

            Path(os.environ["F18_GRID_RECORD"]).write_text(
                str(stats["queries"]) + "\n", encoding="utf-8"
            )
        print(f"item 5 grid: 200 rows; {stats['queries']} SELECTs")
        params = {
            "limit": "2",
            "namespace": "",
            "tag": "red",
            "q": "note",
            "status": "new",
            "untranslated_in": "de-DE",
            "archived": "1",
            "deleted": "1",
        }
        response = client.get(f"/projects/{pid}/keys", params=params)
        assert response.context["has_more"]
        link = response.context["next_url"]
        parsed = parse_qs(urlparse(str(link)).query, keep_blank_values=True)
        for k, v in params.items():
            assert parsed[k] == [v], (k, parsed)
        following = client.get(str(link))
        assert following.status_code == 200
        assert (
            response.context["groups"][0]["rows"][0]["id"]
            != following.context["groups"][0]["rows"][0]["id"]
        )
        assert f'action="/projects/{pid}/keys/export"' in response.text
        assert 'name="fmt"' in response.text and 'name="empty_as"' in response.text
        for bad in ("0", "1001", "oops"):
            assert client.get(f"/projects/{pid}/keys?limit={bad}").status_code == 422


def check_iterator(pid):
    filters = {"include_deleted": True, "include_archived": False, "tag": "red"}
    expected = sorted(k.id for k in expected_search(pid, filters))
    with Session(engine) as s:
        assert [k.id for k in keys.iter_keys(s, pid, batch=37, **filters)] == expected
    for filename, count in [("content.py", 1), ("key_files.py", 2), ("tm_delivery.py", 2)]:
        from pathlib import Path

        # 2026-09-14 SRP splits: the caller may live in a sibling piece of the module
        stem = filename[:-3]
        source = "".join(
            f.read_text(encoding="utf-8")
            for f in sorted((Path(_ROOT) / "app" / "services").glob(f"{stem}*.py"))
            if f.name == filename or f.name.startswith(stem + "_")
        )
        assert source.count(".iter_keys(") == count, filename
    print("item 6 iterator: complete filtered id sequence in batches of 37")


def check_iterator_delivery_parity(pid):

    from app.services import tm_delivery

    for function in (tm_delivery._key_empty_ids, tm_delivery.key_empties):
        with Session(engine) as s:
            actual = function(s, pid, "de-DE")
            with patch_pieces("app.services.keys", "iter_keys", keys.list_keys):
                expected = function(s, pid, "de-DE")
            assert actual == expected, function.__name__
    print("iterator delivery outputs retain original list order")


def check_iterator_lifecycle_parity():

    from app.services import key_files

    with Session(engine) as s:
        project = content.project_create(
            s,
            "loop parity",
            source_locale="en-US",
            target_locales="de-DE",
            project_type=ProjectType.KEY_BASED,
        )
        for name in ("a", "b", "c"):
            keys.create_key(s, project, name, namespace="loop", base_value=name)
        s.commit()
        # This fixture's metadata and creation orders agree. Both full-project
        # writers must visit the same sequence through the old and new readers.
        sequences = []
        for reader in (keys.list_keys, keys.iter_keys):
            visited = []

            def observe(session, key, visited=visited, **kwargs):
                visited.append(key.name)

            with (
                patch_pieces("app.services.keys", "iter_keys", reader),
                patch_pieces("app.services.keys", "ensure_value_rows", observe),
            ):
                assert content._materialize_key_values(s, project.id) == 3
            sequences.append(visited)
        assert sequences == [["a", "b", "c"], ["a", "b", "c"]]
        sequences = []
        for reader in (keys.list_keys, keys.iter_keys):
            visited = []

            def observe_delete(session, key, visited=visited, **kwargs):
                visited.append(key.name)

            with (
                patch_pieces("app.services.keys", "iter_keys", reader),
                patch_pieces("app.services.keys", "delete_key", observe_delete),
            ):
                report = key_files.import_keys(
                    s, project, "loop.json", b"{}", locale="en-US", mode=key_files.MODE_CLEANUP
                )
            assert report["deleted"] == 3
            sequences.append(visited)
        assert sequences == [["a", "b", "c"], ["a", "b", "c"]]
    print("iterator lifecycle/cleanup visit sequences match legacy on ordered fixture")


def check_guard_and_postgres_sql(pid):
    from types import SimpleNamespace

    from sqlalchemy.dialects import postgresql

    class Refused(Exception):
        pass

    for fn in (keys.list_keys, keys.search_keys, keys.page_keys):
        with Session(engine) as s, measured(s) as stats:
            with patch_pieces("app.services.keys", "_guard", side_effect=Refused):
                try:
                    fn(s, pid, org_id=-1, customer_id=-1)
                except Refused:
                    pass
                else:
                    raise AssertionError("guard was bypassed")
            assert stats["queries"] == 0
    fake = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()))
    stmt = keys._key_statement(
        fake, pid, tag="red", q="%_", status="new", untranslated_in="de-DE"
    ).order_by(*keys._key_order(fake))
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "@>" in sql and "JSONB" in sql and "ILIKE" in sql
    assert sql.count('COLLATE "C"') == 3 and "json_each" not in sql


def check_round2(pid):
    """Run each final-gate regression even when another finding is still red."""
    import sqlite3
    from pathlib import Path
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from sqlalchemy.dialects import postgresql, sqlite

    from app.main import app

    failures = []

    def check(label, fn):
        try:
            fn()
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            print(f"ROUND2 RED {failures[-1]}")
        else:
            print(f"ROUND2 GREEN {label}")

    def views(s, page, project_id):
        return keys.key_views(s, page, project_id=project_id)

    with Session(engine) as s:
        p = content.project_create(
            s,
            "Round 2",
            source_locale="en-US",
            target_locales="de-DE",
            project_type=ProjectType.KEY_BASED,
        )
        special_pid = p.id
        for name, value in (("Äpfel", "\u00a0"), ("wide", "\u3000"), ("bom", "\ufeff")):
            k = keys.create_key(s, p, name, base_value="Source")
            seg = keys.values_for(s, k)[0]
            seg.target_text = value
            s.add(seg)
        s.commit()

    def unicode_search():
        with Session(engine) as s:
            assert [k.name for k in keys.search_keys(s, special_pid, q="äpfel")] == ["Äpfel"], (
                "äpfel must find Äpfel"
            )
        fake = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()))
        sql = str(keys._key_statement(fake, pid, q="äpfel").compile(dialect=postgresql.dialect()))
        assert sql.count(" ILIKE ") == 3 and "loc_lower" not in sql

    def unicode_empty():
        with Session(engine) as s:
            names = {k.name for k in keys.search_keys(s, special_pid, untranslated_in="de-DE")}
            assert names == {"Äpfel", "wide", "bom"}, names
        chars = (
            " \t\n\r\v\f\u00a0\u1680"
            + "".join(chr(n) for n in range(0x2000, 0x200B))
            + "\u2028\u2029\u202f\u205f\u3000\ufeff"
        )
        for dialect in (sqlite.dialect(), postgresql.dialect()):
            fake = SimpleNamespace(
                get_bind=lambda dialect=dialect: SimpleNamespace(dialect=dialect)
            )
            compiled = keys._key_statement(fake, pid, untranslated_in="de-DE").compile(
                dialect=dialect
            )
            assert chars in compiled.params.values(), compiled.params
            assert ("btrim(" if dialect.name == "postgresql" else "trim(") in str(compiled)

    def docs():
        for filename in (
            "app/capabilities/keys.md",
            "app/capabilities/llm.md",
            "app/templates/help.html",
        ):
            text = (Path(_ROOT) / filename).read_text(encoding="utf-8")
            assert "?namespace=" in text and "root namespace" in text, filename
        text = (Path(_ROOT) / "docs/CLI.md").read_text(encoding="utf-8")
        assert 'keys list --namespace ""' in text and "root namespace" in text

    def grid_count():
        with TestClient(app) as client:
            client.get(f"/projects/{pid}/keys?limit=1")
            with Session(engine) as s, measured(s) as stats:
                response = client.get(f"/projects/{pid}/keys")
            expected = len(expected_search(pid, {"include_archived": False}))
            assert response.context["total"] == expected, (response.context["total"], expected)
            assert f"showing 200 of {expected:,}" in response.text
            assert (
                "grouped within this page" in response.text
                and "pages follow key id" in response.text
            )
            assert stats["queries"] == GRID_QUERIES, _sql_tables(stats)
            count_sql = [
                sql
                for sql in stats["sql"]
                if "count(" in sql.lower() and "stringkey" in sql.lower()
            ]
            assert len(count_sql) == 1 and "LIMIT" in count_sql[0], count_sql
            following = client.get(str(response.context["next_url"]))
            assert following.context["total"] == expected
            with Session(engine) as s:
                p = content.project_create(
                    s,
                    "Count cap",
                    source_locale="en-US",
                    target_locales="de-DE",
                    project_type=ProjectType.KEY_BASED,
                )
                large_pid = p.id
                s.execute(
                    insert(StringKey),
                    [
                        {
                            "project_id": large_pid,
                            "name": f"count-{i}",
                            "namespace": "half" if i < 10000 else "",
                        }
                        for i in range(20000)
                    ],
                )
                s.commit()
            response = client.get(f"/projects/{large_pid}/keys")
            assert response.context["total"] == 10001
            assert "showing 200 of 10,000+" in response.text
            response = client.get(f"/projects/{large_pid}/keys?namespace=half")
            assert response.context["total"] == 10000
            assert "showing 200 of 10,000" in response.text and "10,000+" not in response.text

    def scoped_values():
        with Session(engine) as s:
            own = keys.page_keys(s, pid, limit=1)["keys"][0]
            foreign = keys.page_keys(s, special_pid, limit=1)["keys"][0]
            payload = views(s, [own, foreign], pid)
            assert payload[0]["values"] and payload[0]["base"]
            assert payload[1]["values"] == {} and payload[1]["base"] == {}, (
                "cross-project values leaked"
            )

    def no_sqlite_collation():
        with Session(engine) as s:
            sql = str(
                keys._key_statement(s, pid)
                .order_by(*keys._key_order(s))
                .compile(dialect=sqlite.dialect())
            )
        assert "COLLATE" not in sql, sql

    def chunked_values():
        with Session(engine) as s:
            raw = s.connection().connection.driver_connection
            previous = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            try:
                for limit, queries in ((100, 3), (1000, 5)):
                    with measured(s) as stats:
                        page = keys.page_keys(s, pid, limit=limit)["keys"]
                        payload = views(s, page, pid)
                    assert len(payload) == limit and all(
                        row["values"] and row["base"] for row in payload
                    )
                    assert stats["queries"] == queries, stats["queries"]
                    print(
                        f"ROUND2 D5 limit={limit}: {queries} SELECTs "
                        f"(1 key + {queries - 1} value/base)"
                    )
            finally:
                raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)

    def iterator_bound(batch):
        with Session(engine) as s:
            try:
                next(keys.iter_keys(s, pid, batch=batch))
            except ValueError:
                return
            raise AssertionError(f"batch={batch} was silently clamped")

    check("M1 Unicode search / Postgres ILIKE", unicode_search)
    check("M2 Unicode empty / dialect trim", unicode_empty)
    check("M3 four namespace surfaces", docs)
    check("M4 bounded grid count / M5 page grouping", grid_count)
    check("L1 project-scoped values", scoped_values)
    check("L2 no SQLite COLLATE", no_sqlite_collation)
    check("L3 1000-key values / query counts", chunked_values)
    for batch in (0, 1001):
        check(f"L4 batch={batch}", lambda batch=batch: iterator_bound(batch))
    assert not failures, "\n".join(failures)


def check_docs():
    from pathlib import Path

    architecture = (Path(_ROOT) / "docs/architecture/string-keys.md").read_text(encoding="utf-8")
    for phrase in ("ASCII whitespace", "ILIKE", "101", "200", "1000", "id > cursor"):
        assert phrase in architecture, phrase
    for filename in ("app/templates/help.html", "app/capabilities/llm.md"):
        text = (Path(_ROOT) / filename).read_text(encoding="utf-8")
        assert "200 keys" in text and "cursor" in text
    print("item 8 documented paging contract")


if __name__ == "__main__":
    pid = fixture()
    check_list(pid)
    check_search(pid)
    check_pages(pid)
    check_views(pid)
    check_grid(pid)
    check_iterator(pid)
    check_iterator_delivery_parity(pid)
    check_iterator_lifecycle_parity()
    check_guard_and_postgres_sql(pid)
    check_docs()
    check_round2(pid)
    print("KEY PAGING OK")
