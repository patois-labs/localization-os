"""#576: the #571 caps reach every ingest path that is not a file upload.

Hermetic and offline: throwaway LOC_TMS_DATA_DIR, provider keys cleared, mock connector
transports (httpx.MockTransport), ASCII-only prints. The caps are lowered in-process
(config.MAX_DOCUMENT_SEGMENTS and friends) so the files stay tiny.

Section A, connector pulls and seeders: Airtable pull, Jira translate, Confluence translate
and a flow.py connector (Zendesk). Over a cap the run stops cleanly with a clear message
recorded on the run, NO Document or Segment row is stored, and the engine is never called
(no money spent on rows that would be refused). Under the cap everything still works.
Final line: "INGEST CAPS 576 OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_caps576_")

import httpx
from sqlmodel import Session, func, select

from app import config
from app.db import engine, init_db
from app.models import Document, Segment
from app.services import translate
from app.services.connectors import store as cstore

init_db()

CAP = 5
config.MAX_DOCUMENT_SEGMENTS = CAP
config.MAX_DOCUMENT_ROWS = 100
config.MAX_SEGMENT_CHARS = 1000

NL = chr(10)
ENGINE_CALLS: list = []
_real_pretranslate = translate.pretranslate_document


def _counting_pretranslate(*a, **kw):
    ENGINE_CALLS.append(a[1:3])
    return _real_pretranslate(*a, **kw)


translate.pretranslate_document = _counting_pretranslate


def counts():
    with Session(engine) as s:
        return (
            s.exec(select(func.count()).select_from(Document)).one(),
            s.exec(select(func.count()).select_from(Segment)).one(),
        )


def conn_status(cid):
    from app.models import Connection

    with Session(engine) as s:
        row = s.get(Connection, cid)
        return str(getattr(row.status, "value", row.status)), (row.last_message or "")


# ---------------------------------------------------------------------------
# A1. Airtable pull
# ---------------------------------------------------------------------------
def airtable_transport(n_records: int):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.startswith("/v0/appCAP/"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {"id": f"rec{i}", "fields": {"Body (EN)": f"Hello number {i}"}}
                        for i in range(n_records)
                    ]
                },
            )
        return httpx.Response(404, json={"error": "unexpected"})

    return httpx.MockTransport(handler)


def test_airtable_pull():
    cfg = {
        "base_id": "appCAP",
        "table": "Content",
        "source_field": "Body (EN)",
        "target_field": "Body (FR)",
        "target_locale": "fr-FR",
        "max_records": "100000",
    }
    conn = cstore.create("airtable", "Cap base", cfg, {"api_key": "patTEST"}, actor="admin")
    before = counts()
    ok, msg, doc_id = cstore.pull_connection(
        conn.id, actor="admin", transport=airtable_transport(CAP + 7)
    )
    assert not ok and not doc_id, (ok, msg, doc_id)
    assert "MAX_DOCUMENT_SEGMENTS" in msg, msg
    assert counts() == before, ("rows stored for a refused pull", before, counts())
    status, last = conn_status(conn.id)
    assert status.lower().endswith("error") and "MAX_DOCUMENT_SEGMENTS" in last, (status, last)
    # under the cap: unchanged behavior, rows land (chunk width forced tiny)
    from app.services import seed_caps

    real_chunk = seed_caps.SEED_CHUNK
    seed_caps.SEED_CHUNK = 2
    try:
        ok, msg, doc_id = cstore.pull_connection(
            conn.id, actor="admin", transport=airtable_transport(CAP)
        )
    finally:
        seed_caps.SEED_CHUNK = real_chunk
    assert ok and doc_id, msg
    with Session(engine) as s:
        n = s.exec(
            select(func.count()).select_from(Segment).where(Segment.document_id == doc_id)
        ).one()
    assert n == CAP, n
    print("ok airtable pull: over-cap refused with a clear message, nothing stored; at-cap works")


# ---------------------------------------------------------------------------
# A2. Jira translate
# ---------------------------------------------------------------------------
def jira_transport(n_issues: int, writes: list, text_len: int = 20):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/rest/api/3/search/jql":
            issues = [
                {
                    "key": f"LOC-{i}",
                    "fields": {"summary": "S" * text_len + f" {i}", "description": None},
                }
                for i in range(1, n_issues + 1)
            ]
            return httpx.Response(200, json={"isLast": True, "issues": issues})
        if req.method in ("POST", "PUT") or "/comment" in path:
            writes.append(path)
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def _jira_conn(name, locales="es-MX"):
    cfg = {
        "base_url": "https://team.atlassian.net",
        "jql": "project = LOC",
        "source_field": "summary",
        "target_mode": "comment",
        "target_locales": locales,
        "source_locale": "en-US",
    }
    return cstore.create("jira", name, cfg, {"email": "b@x.com", "api_token": "tok"}, actor="admin")


def test_airtable_webhook():
    """The webhook door (dynamic_translate) shares the caps: refused before the Document
    row and before any engine call, reason on the connection and the run log."""
    n = CAP + 3
    pushed: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/v0/meta/bases/appCAPW/tables":
            return httpx.Response(
                200,
                json={
                    "tables": [
                        {
                            "id": "tbl1",
                            "name": "Content",
                            "fields": [
                                {"name": "Notes", "type": "multilineText"},
                                {"name": "Notes (es-MX)", "type": "multilineText"},
                            ],
                        }
                    ]
                },
            )
        if req.method == "GET" and path.startswith("/v0/appCAPW/"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {"id": f"rec{i}", "fields": {"Notes": f"Note number {i}"}} for i in range(n)
                    ]
                },
            )
        if req.method == "PATCH":
            pushed.append(1)
            return httpx.Response(200, json={"records": []})
        return httpx.Response(404, json={"path": path})

    conn = cstore.create(
        "airtable",
        "Cap webhook",
        {
            "source_field": "Notes",
            "naming_pattern": "{src} ({loc})",
            "source_locale": "en-US",
            "base_id": "appCAPW",
            "table": "Content",
        },
        {"api_key": "patCAPW"},
        actor="admin",
    )
    real_writes = config.MAX_WRITES_PER_RUN
    config.MAX_WRITES_PER_RUN = 1000
    try:
        before, calls = counts(), len(ENGINE_CALLS)
        ok, msg, _s = cstore.dynamic_translate(
            conn.id, base_id="appCAPW", table="Content", transport=httpx.MockTransport(handler)
        )
    finally:
        config.MAX_WRITES_PER_RUN = real_writes
    assert not ok and "MAX_DOCUMENT_SEGMENTS" in msg, (ok, msg)
    assert counts() == before and len(ENGINE_CALLS) == calls and not pushed, (
        "refused webhook run stored rows, called the engine or wrote to Airtable"
    )
    status, last = conn_status(conn.id)
    assert status.lower().endswith("error") and "MAX_DOCUMENT_SEGMENTS" in last, (status, last)
    print("ok airtable webhook: over-cap run refused before rows/engine/write-back")


def test_jira_translate():
    # too many segments
    conn = _jira_conn("Jira cap")
    writes: list = []
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _summary = cstore.jira_translate(
        conn.id, actor="auto", transport=jira_transport(CAP + 3, writes)
    )
    assert not ok and "MAX_DOCUMENT_SEGMENTS" in msg, (ok, msg)
    assert counts() == before and len(ENGINE_CALLS) == calls and not writes, (
        "refused run stored rows, called the engine or wrote to Jira",
        counts(),
        len(ENGINE_CALLS) - calls,
        writes,
    )
    status, last = conn_status(conn.id)
    assert status.lower().endswith("error") and "MAX_DOCUMENT_SEGMENTS" in last, (status, last)
    # segments x locales over MAX_DOCUMENT_ROWS (segments alone are fine)
    config.MAX_DOCUMENT_ROWS = 6
    try:
        conn2 = _jira_conn("Jira rows", locales="es-MX, de-DE, fr-FR, it-IT")
        before, calls = counts(), len(ENGINE_CALLS)
        ok, msg, _s = cstore.jira_translate(
            conn2.id, actor="auto", transport=jira_transport(3, writes)
        )
        assert not ok and "MAX_DOCUMENT_ROWS" in msg, (ok, msg)
        assert counts() == before and len(ENGINE_CALLS) == calls and not writes, msg
    finally:
        config.MAX_DOCUMENT_ROWS = 100
    # one huge text
    conn3 = _jira_conn("Jira chars")
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _s = cstore.jira_translate(
        conn3.id,
        actor="auto",
        transport=jira_transport(1, writes, text_len=config.MAX_SEGMENT_CHARS + 50),
    )
    assert not ok and "MAX_SEGMENT_CHARS" in msg, (ok, msg)
    assert counts() == before and len(ENGINE_CALLS) == calls and not writes, msg
    # the search itself never pages past cap + 1 issues, whatever max_results says (#576
    # review): a huge JQL cannot grow memory without bound before the cap check runs
    from app.services.connectors import jira_part2

    seen = {"asked": 0}

    class _C:
        def __init__(self):
            pass

    jc = jira_part2.JiraConnector.__new__(jira_part2.JiraConnector)

    def fake_get(c, path, params=None):
        seen["asked"] += int(params["maxResults"])
        n = int(params["maxResults"])
        return httpx.Response(
            200,
            json={
                "issues": [{"key": f"K-{i}", "fields": {}} for i in range(n)],
                "nextPageToken": "t",
            },
        )

    jc._get = fake_get
    got = jc._search(None, "project = X", "summary", 10**9)
    assert len(got) == CAP + 1 and seen["asked"] == CAP + 1, (len(got), seen)
    # under the cap still translates
    conn4 = _jira_conn("Jira ok")
    ok, msg, _s = cstore.jira_translate(conn4.id, actor="auto", transport=jira_transport(2, writes))
    assert ok, msg
    assert counts()[0] == before[0] + 1 and len(ENGINE_CALLS) > calls, "at-cap run should translate"
    print("ok jira translate: segments, rows, chars refused before rows/engine; small run ok")


# ---------------------------------------------------------------------------
# A3. Confluence translate (per page)
# ---------------------------------------------------------------------------
def confluence_transport(created: list, big_paragraphs: int):
    """Fake Confluence tenant (REST v2); `created` captures the child-page POSTs."""
    from tests._confluence_fake import FakeConfluence

    fake = FakeConfluence()
    fake.add_space("HELP", "Help")
    for pid, paragraphs in (("1", big_paragraphs), ("2", 1)):
        body = f"<h1>Doc {pid}</h1>" + "".join(
            f"<p>Para {j} of {pid}.</p>" for j in range(paragraphs)
        )
        fake.add_page("HELP", f"Doc {pid}", body, pid=pid)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/pages"):
            created.append(json.loads(req.content))
        return fake._handle(req)

    return httpx.MockTransport(handler)


def test_confluence_translate():
    conn = cstore.create(
        "confluence",
        "Conf cap",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "HELP",
            "target_locales": "es-MX",
            "source_locale": "en-US",
            "title_pattern": "[{loc}] {title}",
            "max_pages": "25",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    created: list = []
    before_docs, calls = counts()[0], len(ENGINE_CALLS)
    _ok, msg, summary = cstore.confluence_translate(
        conn.id, actor="auto", transport=confluence_transport(created, big_paragraphs=CAP + 4)
    )
    # page 1 is over the cap: refused, recorded; page 2 (small) still translated
    assert "MAX_DOCUMENT_SEGMENTS" in msg, msg
    assert any("MAX_DOCUMENT_SEGMENTS" in f["error"] for f in summary["failures"]), summary
    assert counts()[0] == before_docs + 1, ("only the small page may create a document", counts())
    assert len(created) == 1 and created[0].get("title", "").endswith("Doc 2"), created
    assert len(ENGINE_CALLS) - calls == 1, ("engine only for the small page", ENGINE_CALLS[calls:])
    print("ok confluence: over-cap page refused and recorded, small page still translated")


# ---------------------------------------------------------------------------
# A4. flow.py connector (Zendesk)
# ---------------------------------------------------------------------------
def zendesk_transport(created: list, big_paragraphs: int):
    locales = ["es"]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        m = req.method
        if path.endswith("/locales.json") or "locales/active" in path or path.endswith("/locales"):
            return httpx.Response(200, json={"locales": locales})
        if "/api/v2/help_center/locales" in path:
            return httpx.Response(200, json={"locales": locales})
        if "/articles" in path and m == "GET" and "/translations" not in path:
            arts = [
                {
                    "id": 1,
                    "title": "Big article",
                    "body": "".join(f"<p>Para {j}.</p>" for j in range(big_paragraphs)),
                    "locale": "en-us",
                    "edited_at": "2026-01-02T00:00:00Z",
                    "section_id": 1,
                },
                {
                    "id": 2,
                    "title": "Small article",
                    "body": "<p>One.</p>",
                    "locale": "en-us",
                    "edited_at": "2026-01-01T00:00:00Z",
                    "section_id": 1,
                },
            ]
            return httpx.Response(200, json={"articles": arts, "count": len(arts)})
        if "/translations/" in path and m == "GET":
            return httpx.Response(404, json={})
        if "/translations" in path and m in ("POST", "PUT"):
            created.append({"path": path, **json.loads(req.content or b"{}")})
            return httpx.Response(201 if m == "POST" else 200, json={"translation": {"id": 1}})
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def test_flow_connector():
    conn = cstore.create(
        "zendesk",
        "Zen cap",
        {"subdomain": "acme", "target_locales": "es", "source_locale": "en-us"},
        {"api_token": "tok", "email": "b@x.com"},
        actor="admin",
    )
    created: list = []
    before_docs, calls = counts()[0], len(ENGINE_CALLS)
    _ok, msg, summary = cstore.zendesk_translate(
        conn.id, actor="auto", transport=zendesk_transport(created, big_paragraphs=CAP + 4)
    )
    assert "MAX_DOCUMENT_SEGMENTS" in msg, msg
    assert any(
        "MAX_DOCUMENT_SEGMENTS" in (f.get("error") or "") for f in summary.get("failures", [])
    ), summary
    assert counts()[0] == before_docs + 1, (
        "only the small article may create a document",
        counts(),
    )
    assert len(created) == 1, ("only the small article is written back", created)
    assert len(ENGINE_CALLS) - calls == 1, (
        "engine only for the small article",
        ENGINE_CALLS[calls:],
    )
    # the big article stays pending: the watermark must not run past it
    from app.services.connectors.store import _epoch

    assert summary["watermark"] < _epoch("2026-01-02T00:00:00Z"), summary
    print("ok flow.py (zendesk): over-cap article refused and recorded, held behind the watermark")


def test_connector_row_cap_before_engine():
    """The row cap (segments x locales) is the second guard: a record under the segment
    cap whose rows still break MAX_DOCUMENT_ROWS is refused by the shared seeder check
    (seed_caps), not by the HTML parser, before any row or engine call."""
    config.MAX_DOCUMENT_ROWS = 3
    try:
        conn = cstore.create(
            "zendesk",
            "Zen rows",
            {"subdomain": "acme2", "target_locales": "es", "source_locale": "en-us"},
            {"api_token": "tok", "email": "b@x.com"},
            actor="admin",
        )
        created: list = []
        before_docs, calls = counts()[0], len(ENGINE_CALLS)
        _ok, msg, summary = cstore.zendesk_translate(
            conn.id, actor="auto", transport=zendesk_transport(created, big_paragraphs=3)
        )
        assert "MAX_DOCUMENT_ROWS" in msg, msg
        assert counts()[0] == before_docs + 1 and len(created) == 1, (counts(), created)
        assert len(ENGINE_CALLS) - calls == 1, ENGINE_CALLS[calls:]

        conn = cstore.create(
            "confluence",
            "Conf rows",
            {
                "base_url": "https://team.atlassian.net",
                "space_key": "HELP",
                "target_locales": "es-MX",
                "source_locale": "en-US",
                "title_pattern": "[{loc}] {title}",
                "max_pages": "25",
            },
            {"email": "b@x.com", "api_token": "tok"},
            actor="admin",
        )
        created = []
        before_docs, calls = counts()[0], len(ENGINE_CALLS)
        _ok, msg, summary = cstore.confluence_translate(  # noqa: RUF059
            conn.id, actor="auto", transport=confluence_transport(created, big_paragraphs=3)
        )
        assert "MAX_DOCUMENT_ROWS" in msg, msg
        assert counts()[0] == before_docs + 1 and len(created) == 1, (counts(), created)
        assert len(ENGINE_CALLS) - calls == 1, ENGINE_CALLS[calls:]
    finally:
        config.MAX_DOCUMENT_ROWS = 100
    print(
        "ok flow.py and confluence: segments x locales over the row cap refused before rows/engine"
    )


# ---------------------------------------------------------------------------
# B. add-a-locale seeding
# ---------------------------------------------------------------------------
def _mk_doc(n_rows: int, src_locale: str = "fr-FR"):
    from app.models import DocStatus, Project

    with Session(engine) as s:
        p = Project(name=f"seed{n_rows}", source_locale="en", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(
            project_id=p.id,
            upload_blob_key=f"u{n_rows}.txt",
            filename=f"seed{n_rows}.txt",
            fmt="txt",
            status=DocStatus.NEW,
            skeleton={"units": []},
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        for i in range(n_rows):
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale=src_locale,
                    order_index=i,
                    source_text=f"Row {i}.",
                )
            )
        s.commit()
        return d.id


def _locale_rows(doc_id, locale):
    with Session(engine) as s:
        return sorted(
            s.exec(
                select(Segment.order_index).where(
                    Segment.document_id == doc_id, Segment.target_locale == locale
                )
            ).all()
        )


def test_add_locale_seeding():
    from app.filters.base import InputLimitError
    from app.services import content, locale_seed, seed_caps
    from app.web import api_discovery

    # small document: one atomic transaction, same result as before
    d = _mk_doc(4)
    with Session(engine) as s:
        assert content._seed_locale_segments(s, d, "de-DE") == 4
        assert content._seed_locale_segments(s, d, "de-DE") == 0  # has rows: seeded
    assert _locale_rows(d, "de-DE") == [0, 1, 2, 3]

    # row cap: 4 existing + 4 more = 8 rows passes MAX_DOCUMENT_ROWS = 6
    config.MAX_DOCUMENT_ROWS = 6
    try:
        d = _mk_doc(4)
        # add-a-language door: skipped quietly, nothing written, nothing raised
        with Session(engine) as s:
            assert content._seed_locale_segments(s, d, "de-DE") == 0
        assert _locale_rows(d, "de-DE") == []
        # ...but the user is told: the document page and `doc show` read file_warnings
        with Session(engine) as s:
            warns = (s.get(Document, d).skeleton or {}).get("file_warnings") or []
        assert warns and "MAX_DOCUMENT_ROWS" in warns[0]["message"], warns
        with Session(engine) as s:
            content._seed_locale_segments(s, d, "de-DE")  # again: no duplicate notice
            assert len((s.get(Document, d).skeleton or {}).get("file_warnings")) == 1
        # translate door: refused with a 413 before any engine call
        calls = len(ENGINE_CALLS)
        with Session(engine) as s:
            doc = s.get(Document, d)
            resp = api_discovery.prepare_translate_locale(s, doc, "de-DE")
        assert resp is not None and resp.status_code == 413, resp
        assert b"MAX_DOCUMENT_ROWS" in resp.body, resp.body
        assert _locale_rows(d, "de-DE") == [] and len(ENGINE_CALLS) == calls
        with Session(engine) as s:
            try:
                locale_seed.seed_locale(s, d, "de-DE", strict=True)
            except InputLimitError:
                pass
            else:
                raise AssertionError("strict seeding ignored MAX_DOCUMENT_ROWS")
    finally:
        config.MAX_DOCUMENT_ROWS = 100
    print("ok add-locale: row cap skips the add door, refuses the translate door with 413")

    # chunked and resumable: 10 source rows, chunks of 3, crash after the second chunk
    d = _mk_doc(10)
    real_chunk, real_add = seed_caps.SEED_CHUNK, seed_caps.add_in_chunks
    seed_caps.SEED_CHUNK = 3
    state = {"calls": 0, "crash": True}

    def flaky_add(session, objs, *, chunk=3):
        state["calls"] += 1
        if state["crash"] and state["calls"] == 3:
            raise RuntimeError("simulated crash between chunks")
        return real_add(session, objs, chunk=chunk)

    seed_caps.add_in_chunks = flaky_add
    try:
        with Session(engine) as s:
            try:
                content._seed_locale_segments(s, d, "es-ES")
            except RuntimeError:
                pass
            else:
                raise AssertionError("the simulated crash did not fire")
        part = _locale_rows(d, "es-ES")
        assert 0 < len(part) < 10, ("a crash must leave a partial clone", part)
        from app.services import export_gate

        with Session(engine) as s:
            held = export_gate.gate_held_reason(s, s.get(Document, d), "es-ES")
            assert held and "still being added" in held, held  # never delivered half-cloned
            assert export_gate.gate_held_reason(s, s.get(Document, d), "xx-XX") is None
        with Session(engine) as s:
            doc = s.get(Document, d)
            assert (doc.skeleton or {}).get("_seeding", {}).get("es-ES") == 10, doc.skeleton
        # next call resumes: only the missing rows are written, no duplicates
        state["crash"] = False
        with Session(engine) as s:
            written = content._seed_locale_segments(s, d, "es-ES")
        assert written == 10 - len(part), (written, part)
        assert _locale_rows(d, "es-ES") == list(range(10)), _locale_rows(d, "es-ES")
        with Session(engine) as s:
            doc = s.get(Document, d)
            assert "_seeding" not in (doc.skeleton or {}), doc.skeleton
            assert content._seed_locale_segments(s, d, "es-ES") == 0
            assert export_gate.gate_held_reason(s, doc, "es-ES") is None
        assert state["calls"] >= 3

        # two callers at once (add-a-language clone still running, translate clicked): the
        # second must wait, not "resume" beside the live clone and duplicate rows
        import threading
        import time

        d2 = _mk_doc(10)
        state["crash"] = False
        slow_calls = {"n": 0}

        def slow_add(session, objs, *, chunk=3):
            slow_calls["n"] += 1
            time.sleep(0.15)
            return real_add(session, objs, chunk=chunk)

        seed_caps.add_in_chunks = slow_add
        errs: list = []

        def go(strict):
            try:
                with Session(engine) as s:
                    locale_seed.seed_locale(s, d2, "pt-BR", strict=strict)
            except Exception as exc:
                errs.append(exc)

        t1 = threading.Thread(target=go, args=(False,))
        t2 = threading.Thread(target=go, args=(True,))
        t1.start()
        time.sleep(0.2)  # t1 is mid-clone: marker set, some rows written
        t2.start()
        t1.join()
        t2.join()
        assert not errs, errs
        assert _locale_rows(d2, "pt-BR") == list(range(10)), _locale_rows(d2, "pt-BR")
        # deterministic form: while a seed holds the (document, locale) lock a second caller
        # blocks (and writes nothing) until it is released, then finds the work done
        d3 = _mk_doc(10)
        lock = locale_seed._lock_for(d3, "ja-JP")
        lock.acquire()
        t3 = threading.Thread(target=lambda: go_d3())

        def go_d3():
            with Session(engine) as s:
                locale_seed.seed_locale(s, d3, "ja-JP", strict=True)

        t3.start()
        time.sleep(0.3)
        assert t3.is_alive() and _locale_rows(d3, "ja-JP") == [], "second seed did not wait"
        lock.release()
        t3.join()
        assert _locale_rows(d3, "ja-JP") == list(range(10))
    finally:
        seed_caps.SEED_CHUNK, seed_caps.add_in_chunks = real_chunk, real_add
    print("ok add-locale: big clone is chunked, a crash is resumed without duplicates")


# ---------------------------------------------------------------------------
# C. key-file imports (JSON and PO are the only key-file formats)
# ---------------------------------------------------------------------------
def _key_project(locales="es-ES"):
    from app.models import ProjectType
    from app.services import content

    with Session(engine) as s:
        p = content.project_create(
            s,
            f"KC-{len(locales)}-{os.urandom(3).hex()}",
            source_locale="en-US",
            target_locales=locales,
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _key_count(pid):
    from app.models import StringKey

    with Session(engine) as s:
        return s.exec(
            select(func.count()).select_from(StringKey).where(StringKey.project_id == pid)
        ).one()


def test_key_file_imports():
    from app.models import Project
    from app.services import key_files as kf

    pid = _key_project()
    n_over = CAP + 3

    def run(name, data):
        with Session(engine) as s:
            p = s.get(Project, pid)
            try:
                kf.import_keys(s, p, name, data, locale="en-US")
            except kf.KeyFileError as exc:
                return str(exc)
        return None

    # JSON: more keys than the cap (flat and nested)
    flat = json.dumps({f"k{i}": f"Value {i}" for i in range(n_over)}).encode()
    nested = json.dumps({"a": {f"k{i}": f"Value {i}" for i in range(n_over)}}).encode()
    for nm, data in (("flat.json", flat), ("nested.json", nested)):
        err = run(nm, data)
        assert err and "MAX_DOCUMENT_SEGMENTS" in err, (nm, err)
        assert _key_count(pid) == 0, ("keys stored for a refused file", nm)
    # JSON: far more strings than the cap is refused before json.loads builds a tree
    err = run("many.json", json.dumps({f"k{i}": "v" for i in range(CAP * 25)}).encode())
    assert err and "MAX_DOCUMENT_SEGMENTS" in err, err
    # JSON: one huge value
    err = run("huge.json", json.dumps({"k": "w" * (config.MAX_SEGMENT_CHARS + 10)}).encode())
    assert err and "MAX_SEGMENT_CHARS" in err, err
    # PO: more entries than the cap
    nl = "\n"
    head = (
        'msgid ""'
        + nl
        + 'msgstr ""'
        + nl
        + '"Content-Type: text/plain; charset=UTF-8\\n"'
        + nl
        + nl
    )
    po = "".join(f'msgid "key{i}"' + nl + f'msgstr "Value {i}"' + nl + nl for i in range(n_over))
    err = run("many.po", (head + po).encode())
    assert err and "MAX_DOCUMENT_SEGMENTS" in err, err
    # PO: far more entries than the cap refused before babel builds a catalog
    po_big = "".join(f'msgid "k{i}"' + nl + 'msgstr "v"' + nl + nl for i in range(CAP * 3))
    err = run("big.po", po_big.encode())
    assert err and "MAX_DOCUMENT_SEGMENTS" in err and "far more" in err, err
    assert _key_count(pid) == 0
    # keys x target languages over MAX_DOCUMENT_ROWS
    pid2 = _key_project("es-ES, de-DE, fr-FR")
    config.MAX_DOCUMENT_ROWS = 6
    try:
        with Session(engine) as s:
            p = s.get(Project, pid2)
            try:
                kf.import_keys(
                    s,
                    p,
                    "rows.json",
                    json.dumps({f"k{i}": "v" for i in range(3)}).encode(),
                    locale="en-US",
                )
            except kf.KeyFileError as exc:
                assert "MAX_DOCUMENT_ROWS" in str(exc), exc
            else:
                raise AssertionError("key import ignored MAX_DOCUMENT_ROWS")
    finally:
        config.MAX_DOCUMENT_ROWS = 100
    assert _key_count(pid2) == 0
    # at the cap still imports
    ok_data = json.dumps({f"k{i}": f"Value {i}" for i in range(CAP)}).encode()
    assert run("ok.json", ok_data) is None
    assert _key_count(pid) == CAP, _key_count(pid)
    print("ok key-file imports: JSON and PO over the key, string, row and value caps refused")


# ---------------------------------------------------------------------------
# D. TMX, TBX and glossary CSV imports (entry cap = MAX_DOCUMENT_ROWS)
# ---------------------------------------------------------------------------
def test_tm_and_glossary_imports():
    from app.models import GlossaryEntry, ProjectType, SegmentTM
    from app.services import content, glossary, glossary_tbx, tm

    with Session(engine) as s:
        p = content.project_create(
            s,
            "TM cap",
            source_locale="en-US",
            target_locales="fr-FR",
            project_type=ProjectType.DOCUMENT,
        )
        pid = p.id

    def n_tm():
        with Session(engine) as s:
            return s.exec(select(func.count()).select_from(SegmentTM)).one()

    def n_gl():
        with Session(engine) as s:
            return s.exec(select(func.count()).select_from(GlossaryEntry)).one()

    def tmx(n):
        tus = "".join(
            f'<tu><tuv xml:lang="en-US"><seg>Source {i}</seg></tuv>'
            f'<tuv xml:lang="fr-FR"><seg>Cible {i}</seg></tuv></tu>'
            for i in range(n)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="en-US"/>'
            f"<body>{tus}</body></tmx>"
        ).encode()

    config.MAX_DOCUMENT_ROWS = 5
    try:
        before = n_tm()
        with Session(engine) as s:
            try:
                tm.import_tmx(s, pid, tmx(8), "en-US", "fr-FR")
            except ValueError as exc:
                assert "MAX_DOCUMENT_ROWS" in str(exc), exc
            else:
                raise AssertionError("TMX import ignored MAX_DOCUMENT_ROWS")
        assert n_tm() == before, "rows stored for a refused TMX"
        with Session(engine) as s:
            res = tm.import_tmx(s, pid, tmx(5), "en-US", "fr-FR")
        assert res["imported"] == 5, res  # at the cap is inclusive

        # TBX: caught in the stream (8 entries) and by the cheap count (30 entries)
        def tbx(n):
            ents = "".join(
                f'<termEntry><langSet xml:lang="en"><tig><term>t{i}</term></tig></langSet>'
                f'<langSet xml:lang="fr"><tig><term>u{i}</term></tig></langSet></termEntry>'
                for i in range(n)
            )
            return f"<martif><text><body>{ents}</body></text></martif>".encode()

        for n in (8, 30):
            before = n_gl()
            with Session(engine) as s:
                try:
                    glossary.import_bytes(s, pid, tbx(n), "en", "fr", filename="g.tbx")
                except glossary_tbx.TbxError as exc:
                    assert "MAX_DOCUMENT_ROWS" in str(exc), exc
                else:
                    raise AssertionError("TBX import ignored MAX_DOCUMENT_ROWS")
            assert n_gl() == before

        # glossary CSV: rolled back as a whole
        csv_text = "source_term,target_term" + NL + "".join(f"t{i},u{i}" + NL for i in range(8))
        before = n_gl()
        with Session(engine) as s:
            try:
                glossary.import_bytes(s, pid, csv_text.encode(), "en", "fr", filename="g.csv")
            except Exception as exc:
                assert "MAX_DOCUMENT_ROWS" in str(exc), exc
            else:
                raise AssertionError("glossary CSV import ignored MAX_DOCUMENT_ROWS")
        assert n_gl() == before, "rows stored for a refused glossary CSV"
        ok_csv = "source_term,target_term" + NL + "".join(f"t{i},u{i}" + NL for i in range(5))
        with Session(engine) as s:
            res = glossary.import_bytes(s, pid, ok_csv.encode(), "en", "fr", filename="g.csv")
        assert res["entries"] == 5, res
    finally:
        config.MAX_DOCUMENT_ROWS = 100
    print("ok TMX, TBX and glossary CSV imports: over the entry cap refused and rolled back")


# ---------------------------------------------------------------------------
# F. Gate: vendor XLIFF returns and the shared JSON budget
# ---------------------------------------------------------------------------
def test_vendor_xliff_return_budget():
    """#576 gate: a vendor XLIFF return gets the XLIFF upload's markup budget and the unit
    cap before lxml builds the tree; a key file gets the upload's non-string JSON budget."""
    from app.services import vendor_return_common, vendor_xliff

    def ret(units: int, notes: int = 0) -> bytes:
        body = "".join(
            f'<trans-unit id="s{i}"><target>Cible {i}</target>'
            + "".join(f"<note>n{j}</note>" for j in range(notes))
            + "</trans-unit>"
            for i in range(units)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?><xliff version="1.2"><file '
            f'target-language="fr-FR"><body>{body}</body></file></xliff>'
        ).encode()

    ok = vendor_xliff.parse_return_xliff(ret(CAP))
    assert len(ok["segments"]) == CAP, ok
    # markup flood: few units, far more than 40 x cap markup characters
    flood = ret(2, notes=30 * CAP)
    for data in (flood, ret(CAP + 1)):
        try:
            vendor_xliff.parse_return_xliff(data)
        except vendor_xliff.XliffParseError as exc:
            assert "MAX_DOCUMENT_SEGMENTS" in str(exc), exc
        else:
            raise AssertionError("vendor XLIFF return ignored the #571 budget")
        try:
            vendor_return_common.parse_payload(data)
        except vendor_return_common.ReturnContractError as exc:
            assert "MAX_DOCUMENT_SEGMENTS" in str(exc), exc
        else:
            raise AssertionError("vendor return intake ignored the #571 budget")

    # key file: values that are not strings carry no quotes; same budget as an upload
    from app.models import Project
    from app.services import key_files as kf

    pid = _key_project()
    zeros = json.dumps({"k": [0] * (25 * CAP)}).encode()
    with Session(engine) as s:
        try:
            kf.import_keys(s, s.get(Project, pid), "zeros.json", zeros, locale="en-US")
        except kf.KeyFileError as exc:
            assert "far more values" in str(exc) and "MAX_DOCUMENT_SEGMENTS" in str(exc), exc
        else:
            raise AssertionError("key file skipped the non-string JSON budget")
    assert _key_count(pid) == 0
    print("ok gate: vendor XLIFF return and key-file JSON share the upload parse budgets")


def test_half_cloned_locale_counts_583():
    """#583: while a chunked add-a-language clone is unfinished (Document.skeleton marker),
    the dashboard and project counts leave that locale out and the editor says so."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import locale_seed
    from app.web import queries

    doc_id = _mk_doc(4, src_locale="fr-FR")
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        for i in range(2):  # a half-cloned locale: 2 of 4 rows
            s.add(
                Segment(
                    document_id=doc_id,
                    target_locale="es-ES",
                    order_index=i,
                    source_text=f"Row {i}.",
                )
            )
        s.commit()
        project_id = doc.project_id

        def totals():
            dash = queries._dashboard_data(s, visible_ids=[project_id])["kpis"]["segments"]
            proj = queries._project_counts(s, [doc_id])["segments"]
            return dash, proj

        from app.web import home

        def reports_total():  # gate: /reports figure (whole-install scope, auth off)
            return home._dashboard_reports_data(None, s)["segments"]

        assert totals() == (6, 6), totals()  # no marker: unchanged behavior
        rep_before = reports_total()
        locale_seed._set_marker(s, doc, "es-ES", 4)
        assert totals() == (4, 4), ("marker must exclude the half-cloned locale", totals())
        assert reports_total() == rep_before - 2, ("/reports must skip it too", rep_before)
        # review: the project DETAIL stats and the language count agree with the list.
        det = queries._project_stats(s, [doc_id])
        assert det["segments"] == 4 and det["locale_count"] == 1, det
        lst = queries._project_counts(s, [doc_id])
        assert lst["locale_count"] == 1 and lst["segments"] == det["segments"], lst
    client = TestClient(app)
    r = client.get(f"/documents/{doc_id}/editor?locale=es-ES")
    assert r.status_code == 200 and "localeSeedingNotice" in r.text, r.status_code
    r = client.get(f"/documents/{doc_id}/editor?locale=fr-FR")
    assert r.status_code == 200 and "localeSeedingNotice" not in r.text
    # gate: the document page (per-locale segment count + grid) carries the same notice.
    r = client.get(f"/documents/{doc_id}?locale=es-ES")
    assert r.status_code == 200 and "localeSeedingNotice" in r.text, r.status_code
    r = client.get(f"/documents/{doc_id}?locale=fr-FR")
    assert r.status_code == 200 and "localeSeedingNotice" not in r.text
    with Session(engine) as s:
        locale_seed._set_marker(s, s.get(Document, doc_id), "es-ES", None)
        assert queries._project_counts(s, [doc_id])["segments"] == 6
    print("ok #583: half-cloned locale left out of dashboard/project counts, editor notice shown")


def main():
    test_half_cloned_locale_counts_583()
    test_airtable_pull()
    test_airtable_webhook()
    test_jira_translate()
    test_confluence_translate()
    test_flow_connector()
    test_connector_row_cap_before_engine()
    test_add_locale_seeding()
    test_key_file_imports()
    test_tm_and_glossary_imports()
    test_vendor_xliff_return_budget()
    print("INGEST CAPS 576 OK")


if __name__ == "__main__":
    main()
