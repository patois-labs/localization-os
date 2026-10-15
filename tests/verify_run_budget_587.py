"""#587: one connector run may not buffer more than a per-run character budget, and the Notion
pull says which pages it could not read.

Hermetic and offline: throwaway LOC_TMS_DATA_DIR, mock transports (httpx.MockTransport),
ASCII-only prints. The caps are lowered in-process so the files stay tiny:
MAX_DOCUMENT_SEGMENTS = 5 and MAX_SEGMENT_CHARS = 1000 give a per-record limit of 10000
characters and a per-run budget of 10000 characters (the larger of the derived 500 and one
maximum record). Every record below is about 3000 characters: each is far under the per-record
limit and the record count is under the document cap, so only the run budget can refuse a
run of four of them (about 12000 characters).

Final line: "RUN BUDGET 587 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_runbudget587_")

import httpx
from sqlmodel import Session, func, select

from app import config
from app.db import engine, init_db
from app.models import Connection, Document, Segment
from app.services import translate
from app.services.connectors import store as cstore
from app.services.connectors.base import FetchBudgetError, run_char_budget

init_db()

CAP = 5
config.MAX_DOCUMENT_SEGMENTS = CAP
config.MAX_DOCUMENT_ROWS = 100
config.MAX_SEGMENT_CHARS = 1000
HEAVY = 3000  # characters in one record body: under the per-record limit, four blow the budget
MAX_REQUESTS = 4

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
    with Session(engine) as s:
        row = s.get(Connection, cid)
        return str(getattr(row.status, "value", row.status)), (row.last_message or "")


class Mock:
    def __init__(self, handler):
        self._handler = handler
        self.list_requests = 0
        self.writes: list = []
        self.body_chars = HEAVY

    def transport(self):
        return httpx.MockTransport(lambda req: self._handler(self, req))

    def list_page(self):
        self.list_requests += 1
        return self.list_requests


def assert_refused(name, ok, msg, mock, before, calls):
    assert not ok, (name, "run was not refused", msg)
    assert "per-run limit" in msg, (name, msg)
    assert counts() == before, (name, "a refused run stored a Document or Segments")
    assert len(ENGINE_CALLS) == calls, (name, "a refused run called the engine")
    assert not mock.writes, (name, "a refused run wrote to the source", mock.writes)
    assert mock.list_requests <= MAX_REQUESTS, (name, "kept fetching", mock.list_requests)


def test_budget_derivation():
    assert run_char_budget() == 10000, run_char_budget()
    saved = (config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS)
    saved_ceiling = config.CONNECTOR_RUN_CHAR_BUDGET
    try:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = 50000, 100000
        assert run_char_budget() == 50_000_000, run_char_budget()  # the fixed ceiling wins
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = 500, 10000
        assert run_char_budget() == 500_000, run_char_budget()  # derived is smaller here
        config.CONNECTOR_RUN_CHAR_BUDGET = 200_000
        assert run_char_budget() == 200_000, run_char_budget()  # env override lowers it
        config.CONNECTOR_RUN_CHAR_BUDGET = 1000
        assert run_char_budget() == 100_000, run_char_budget()  # never below one record
    finally:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = saved
        config.CONNECTOR_RUN_CHAR_BUDGET = saved_ceiling
    print("ok budget: min(derived, ceiling), never below one record")


# ---------------------------------------------------------------------------
# Zendesk: the flow engine, the generic pull door and the incremental export
# ---------------------------------------------------------------------------
def _zd_handler(mock: Mock, req: httpx.Request, export=None):
    path = req.url.path
    if "locales" in path:
        return httpx.Response(200, json={"locales": ["es"]})
    if path.endswith("/incremental/articles") and export is not None:
        return httpx.Response(200, json=export)
    if path.endswith("/articles") and req.method == "GET":
        page = mock.list_page()
        data = [
            {
                "id": f"{page}{i:04d}",
                "title": f"T {page} {i}",
                "body": "<p>" + "q" * mock.body_chars + "</p>",
                "edited_at": "2026-01-05T00:00:00Z",
                "section_id": 1,
                "locale": "en-us",
            }
            for i in range(2)
        ]
        return httpx.Response(
            200,
            json={"articles": data, "meta": {"has_more": page < 40, "after_cursor": f"c{page}"}},
        )
    if "/translations/" in path and req.method == "GET":
        return httpx.Response(404, json={})
    if "/translations" in path and req.method in ("POST", "PUT"):
        mock.writes.append(path)
        return httpx.Response(201, json={"translation": {"id": 1}})
    return httpx.Response(404, json={"path": path})


def _zd_conn(name: str):
    return cstore.create(
        "zendesk",
        name,
        {"subdomain": name, "target_locales": "es", "source_locale": "en-us", "max_articles": "4"},
        {"api_token": "tok", "email": "b@x.com"},
        actor="admin",
    )


def test_flow_refuses_over_budget_run():
    conn = _zd_conn("rbflow")
    mock = Mock(_zd_handler)
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _s = cstore.zendesk_translate(conn.id, actor="auto", transport=mock.transport())
    assert_refused("zendesk flow", ok, msg, mock, before, calls)
    status, last = conn_status(conn.id)
    assert status.lower().endswith("error") and "per-run limit" in last, (status, last)
    print(f"ok zendesk flow: over-budget run refused whole after {mock.list_requests} request(s)")


def test_pull_door_refuses_over_budget_run():
    conn = _zd_conn("rbpull")
    mock = Mock(_zd_handler)
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _n = cstore.pull_connection(conn.id, actor="admin", transport=mock.transport())
    assert_refused("zendesk pull", ok, msg, mock, before, calls)
    print("ok pull door: over-budget run refused, no Document")


def test_zendesk_export_counts():
    from app.services.connectors.zendesk import ZendeskConnector

    arts = [
        {
            "id": 900 + i,
            "title": "x",
            "body": "<p>" + "q" * HEAVY + "</p>",
            "edited_at": "2026-02-01T00:00:00Z",
            "section_id": 1,
            "locale": "en-us",
        }
        for i in range(4)
    ]
    export = {"articles": arts, "end_time": 1, "next_page": None}
    mock = Mock(lambda m, r: _zd_handler(m, r, export=export))
    c = ZendeskConnector(
        {"subdomain": "rbexp", "max_articles": "4"},
        {"api_token": "t", "email": "b@x.com"},
        transport=mock.transport(),
    )
    try:
        c.incremental_articles(start_time=0)
    except FetchBudgetError as e:
        assert "per-run limit" in str(e), e
    else:
        raise AssertionError("the incremental export buffered past the run budget")
    print("ok zendesk export: kept articles count against the run budget")


def test_under_budget_still_runs():
    conn = cstore.create(
        "zendesk",
        "rbok",
        {
            "subdomain": "rbok",
            "target_locales": "es",
            "source_locale": "en-us",
            "max_articles": "2",
        },
        {"api_token": "tok", "email": "b@x.com"},
        actor="admin",
    )
    mock = Mock(_zd_handler)
    mock.body_chars = 50
    calls = len(ENGINE_CALLS)
    ok, msg, _s = cstore.zendesk_translate(conn.id, actor="auto", transport=mock.transport())
    assert ok, msg
    assert len(ENGINE_CALLS) - calls == 2, ENGINE_CALLS[calls:]
    print("ok zendesk flow: a run under the budget is unchanged")


# ---------------------------------------------------------------------------
# Airtable, Jira, Confluence: one error stops the run
# ---------------------------------------------------------------------------
def test_airtable_over_budget():
    def handler(mock, req):
        path = req.url.path
        if path == "/v0/meta/bases/appRB587/tables":
            fields = [
                {"name": "Notes", "type": "multilineText"},
                {"name": "Notes (es-MX)", "type": "multilineText"},
            ]
            return httpx.Response(
                200, json={"tables": [{"id": "tbl1", "name": "Content", "fields": fields}]}
            )
        if req.method == "GET" and path.startswith("/v0/appRB587/"):
            page = mock.list_page()
            recs = [{"id": f"r{page}-{i}", "fields": {"Notes": "n" * HEAVY}} for i in range(2)]
            body = {"records": recs}
            if page < 40:
                body["offset"] = f"o{page}"
            return httpx.Response(200, json=body)
        if req.method in ("PATCH", "POST", "PUT"):
            mock.writes.append(path)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"path": path})

    mock = Mock(handler)
    conn = cstore.create(
        "airtable",
        "RB587 airtable",
        {
            "source_field": "Notes",
            "naming_pattern": "{src} ({loc})",
            "source_locale": "en-US",
            "base_id": "appRB587",
            "table": "Content",
            "max_records": "4",
        },
        {"api_key": "patRB587"},
        actor="admin",
    )
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _s = cstore.dynamic_translate(
        conn.id, base_id="appRB587", table="Content", transport=mock.transport()
    )
    assert_refused("airtable", ok, msg, mock, before, calls)
    print("ok airtable: over-budget run refused whole")


def test_jira_over_budget():
    conn = cstore.create(
        "jira",
        "RB587 jira",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "target_mode": "comment",
            "target_locales": "es-MX",
            "source_locale": "en-US",
            "max_results": "4",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )

    def handler(mock, req):
        if req.url.path == "/rest/api/3/search/jql":
            page = mock.list_page()
            issues = [
                {"key": f"LOC-{page}{i}", "fields": {"summary": "S" * HEAVY, "description": None}}
                for i in range(2)
            ]
            return httpx.Response(
                200, json={"isLast": False, "nextPageToken": f"t{page}", "issues": issues}
            )
        if req.method in ("POST", "PUT"):
            mock.writes.append(req.url.path)
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})

    mock = Mock(handler)
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _s = cstore.jira_translate(conn.id, actor="auto", transport=mock.transport())
    assert_refused("jira", ok, msg, mock, before, calls)
    print("ok jira: over-budget run refused whole")


def test_confluence_over_budget():
    conn = cstore.create(
        "confluence",
        "RB587 confluence",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "HELP",
            "target_locales": "es-MX",
            "source_locale": "en-US",
            "title_pattern": "[{loc}] {title}",
            "max_pages": "4",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )

    def handler(mock, req):
        path = req.url.path
        if path == "/wiki/api/v2/spaces":
            return httpx.Response(
                200, json={"results": [{"id": "1", "key": "HELP", "name": "Help"}]}
            )
        if path == "/wiki/api/v2/spaces/1/pages" and req.method == "GET":
            page = mock.list_page()
            pages = [
                {
                    "id": f"{page}{i}",
                    "title": f"P {page} {i}",
                    "spaceId": "1",
                    "status": "current",
                    "version": {"number": 1},
                    "body": {
                        "storage": {
                            "value": "<p>" + "w" * HEAVY + "</p>",
                            "representation": "storage",
                        }
                    },
                }
                for i in range(2)
            ]
            return httpx.Response(
                200,
                json={
                    "results": pages,
                    "_links": {"next": f"/wiki/api/v2/spaces/1/pages?cursor={page}"},
                },
            )
        if req.method == "POST":
            mock.writes.append(path)
            return httpx.Response(200, json={"id": "c1"})
        return httpx.Response(404, json={})

    mock = Mock(handler)
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _s = cstore.confluence_translate(conn.id, actor="auto", transport=mock.transport())
    assert_refused("confluence", ok, msg, mock, before, calls)
    print("ok confluence: over-budget run refused whole")


def test_helpscout_and_hubspot_full_fetches_are_charged():
    """The two connectors that list summaries still buffer FULL records on the by-id path
    (Help Scout retry ids, HubSpot objects); those are charged too (review of #587)."""
    from app.services.connectors.helpscout import HelpScoutConnector
    from app.services.connectors.hubspot import ABSENT, HubSpotConnector  # noqa: F401

    hs = HelpScoutConnector({"max_articles": "50"}, {"api_key": "k"})
    hs._fetch_article = lambda c, aid: {"id": aid, "text": "t" * HEAVY}
    hs._client = lambda: __import__("contextlib").nullcontext(None)
    try:
        hs.discover_articles(article_ids=[str(i) for i in range(10)])
    except FetchBudgetError as e:
        assert "per-run limit" in str(e), e
    else:
        raise AssertionError("help scout by-id fetch was not charged")

    hub = HubSpotConnector(
        {"content_types": "site-pages", "source_language": "en", "max_entries": "50"},
        {"access_token": "pat-na1-x"},
    )
    hub.fetch_object = lambda aid: {"id": aid.split(":")[-1], "html": "h" * HEAVY}
    hub._list_ids = lambda t, cap: [str(i) for i in range(10)]
    try:
        hub.discover_articles()
    except FetchBudgetError as e:
        assert "per-run limit" in str(e), e
    else:
        raise AssertionError("hubspot discovery was not charged")
    print("ok helpscout by-id and hubspot discovery: full fetched records are charged")


def _bare(cls, **stubs):
    """A connector object with no network: config only, every fetch helper stubbed."""
    import contextlib

    obj = object.__new__(cls)
    obj.config = {"max_articles": "50", "max_entries": "50"}
    obj.secrets = {}
    obj.fetch_notes = []
    obj._fetch_chars = 0
    obj._client = lambda *a, **k: contextlib.nullcontext(None)
    for k, v in stubs.items():
        setattr(obj, k, v)
    return obj


def _expect_refused(name, fn):
    try:
        fn()
    except FetchBudgetError as e:
        assert "per-run limit" in str(e), (name, e)
    else:
        raise AssertionError(f"{name}: full fetched records were not charged")


def test_full_refetch_paths_are_charged():
    """Gate of #587: discoveries that list summaries and then fetch each record in FULL
    (Document360, Freshdesk and Zoho on every run; every connector's by-id retry/webhook
    path; the Help Scout generic pull) keep those full records, so they are charged too."""
    from app.services.connectors.akeneo import AkeneoConnector
    from app.services.connectors.contentful import ContentfulConnector
    from app.services.connectors.document360 import Document360Connector
    from app.services.connectors.freshdesk import FreshdeskConnector
    from app.services.connectors.helpscout import HelpScoutConnector
    from app.services.connectors.intercom import IntercomConnector
    from app.services.connectors.shopify import ShopifyConnector
    from app.services.connectors.strapi import StrapiConnector
    from app.services.connectors.wordpress import WordPressConnector
    from app.services.connectors.zendesk import ZendeskConnector
    from app.services.connectors.zoho import ZohoConnector

    body = "b" * HEAVY
    ids = [str(i) for i in range(10)]
    summaries = [{"id": i} for i in ids]  # tiny list rows: _keep_rows charges almost nothing

    d360 = _bare(
        Document360Connector,
        _source_lang=lambda: "en",
        _list_articles=lambda c, cap: summaries,
        _fetch_article=lambda c, aid, lang: {
            "id": aid,
            "content": body,
            "project_version_id": "pv",
        },
    )
    d360.config = {"max_articles": "50", "project_version_id": "pv"}
    _expect_refused("document360 listing", d360.discover_articles)
    d360._fetch_chars = 0
    _expect_refused("document360 by-id", lambda: d360.discover_articles(article_ids=ids))

    fd = _bare(
        FreshdeskConnector,
        _paginate=lambda c, path, cap: summaries,
        _fetch_article=lambda c, aid: {"id": aid, "description": body},
    )
    fd.config["folder_id"] = "f1"
    _expect_refused("freshdesk listing", fd.discover_articles)
    fd._fetch_chars = 0
    fd.config.pop("folder_id")  # a configured folder scopes by-id fetches (taxonomy A)
    _expect_refused("freshdesk by-id", lambda: fd.discover_articles(article_ids=ids))

    zo = _bare(
        ZohoConnector,
        _paginate=lambda c, path, params, cap: summaries,
        _fetch_article=lambda c, aid: {"id": aid, "answer": body},
    )
    _expect_refused("zoho listing", zo.discover_articles)
    zo._fetch_chars = 0
    _expect_refused("zoho by-id", lambda: zo.discover_articles(article_ids=ids))

    ic = _bare(IntercomConnector, _fetch_article=lambda c, aid: {"id": aid, "body": body})
    _expect_refused("intercom by-id", lambda: ic.discover_articles(article_ids=ids))

    class _Resp:
        status_code = 200

        def __init__(self, aid):
            self._aid = aid

        def json(self):
            return {"article": {"id": self._aid, "body": body}}

    zd = _bare(
        ZendeskConnector,
        _hc=lambda p: p,
        _get=lambda c, path, **k: _Resp(path.rsplit("/", 1)[-1]),
        _label_filter=lambda: [],
    )
    _expect_refused("zendesk by-id", lambda: zd.discover_articles(article_ids=ids))

    wp = _bare(
        WordPressConnector,
        _post_types=lambda: ["posts"],
        _own_target_refs=lambda: set(),
        _assisted=lambda: True,
        _split_ref=lambda raw, types: ("posts", raw),
        _ref=lambda t, num: f"{t}:{num}",
        _fetch_post=lambda c, t, num: {"id": num},
        _normalize=lambda item, t: {"id": item["id"], "content": body},
    )
    _expect_refused("wordpress by-id", lambda: wp.discover_articles(article_ids=ids))

    sh = _bare(
        ShopifyConnector,
        _resource_types=lambda: ["PRODUCT"],
        _source_locale=lambda: "en",
        _validate_gid=lambda raw, types: f"gid://shopify/Product/{raw}",
        _fetch_resource=lambda gid, loc: {"resourceId": gid},
        _record=lambda node, rt: {"id": node["resourceId"], "body": body},
    )
    _expect_refused("shopify by-id", lambda: sh.discover_articles(article_ids=ids))

    st = _bare(
        StrapiConnector,
        _source_locale=lambda: "en",
        _content_types=lambda: ["api::article.article"],
        _resolve_explicit=lambda raw, cts: (cts[0], raw),
        _fetch_entry=lambda c, ct, docid, src: {"documentId": docid, "body": body},
    )
    _expect_refused("strapi by-id", lambda: st.discover_articles(article_ids=ids))

    cf = _bare(
        ContentfulConnector,
        _content_types=lambda: ["article"],
        _fetch_entry_scoped=lambda c, raw, cts: {"sys": {"id": raw}, "body": body},
    )
    _expect_refused("contentful by-id", lambda: cf.discover_articles(article_ids=ids))

    ak = _bare(
        AkeneoConnector,
        _content_types=lambda: ["product"],
        _split_aid=lambda raw: ("product", raw),
        _fetch_one=lambda c, ct, ident: {"identifier": ident, "description": body},
    )
    _expect_refused("akeneo by-id", lambda: ak.discover_articles(article_ids=ids))

    hs = _bare(
        HelpScoutConnector,
        discover_articles=lambda **k: summaries,
        fetch_article_content=lambda aid: {"id": aid, "name": "t", "text": "<p>" + body + "</p>"},
    )
    _expect_refused("help scout generic pull", hs.pull)
    print("ok full re-fetch paths: listed-then-fetched and by-id records are charged")


def main():
    """No argument: run every section. Names as arguments: run only those (fail-before runs)."""
    wanted = sys.argv[1:]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not wanted or name in wanted):
            fn()
    print("RUN BUDGET 587 OK")


if __name__ == "__main__":
    main()
