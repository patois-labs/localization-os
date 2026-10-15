"""#579: every connector fetch is bounded BEFORE the #576 cap check.

Hermetic and offline: throwaway LOC_TMS_DATA_DIR, provider keys cleared, mock transports
(httpx.MockTransport), ASCII-only prints. The document cap is lowered in-process
(config.MAX_DOCUMENT_SEGMENTS = 5) so the files stay tiny.

Every mock is a firehose: it answers any list request with a full page of unique records and
says "there is more", for 40 pages, whatever page size the client asked for. A connection is
set to a record limit of 1000000. After the fix the client asks for at most cap + 1 records
and STOPS REQUESTING pages; before it, the client followed the firehose to its own page
bound. Each section asserts the number of requests the client made.

Final line: "FETCH BOUND 579 OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_fetch579_")

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
# #587: this suite's firehose pages are 100 rows whatever page size is asked, which at the
# lowered caps would trip the per-run character budget; that budget has its own suite
# (verify_run_budget_587).
from app.services.connectors import base as _cbase

_cbase.run_char_budget = lambda: 10**12
HUGE = "1000000"  # the admin-set record limit on every connection under test
PAGES = 40  # the firehose ends after this many pages so an unbounded client still terminates
PAGE_ROWS = 100  # rows per firehose page, whatever page size the client asks for
MAX_REQUESTS = 3  # a bounded client needs one list request (a spare for a probe)

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


class Firehose:
    """Counts list requests. `page(n)` returns the JSON for the n-th list page (1-based)."""

    def __init__(self, handler):
        self._handler = handler
        self.list_requests = 0
        self.all_requests = []

    def transport(self):
        def run(req: httpx.Request) -> httpx.Response:
            self.all_requests.append(f"{req.method} {req.url.path}")
            return self._handler(self, req)

        return httpx.MockTransport(run)

    def list_page(self):
        """Call from a handler for a LIST request; returns the 1-based page number."""
        self.list_requests += 1
        return self.list_requests


def rows(page: int, n: int = PAGE_ROWS, **extra) -> list:
    return [
        {"id": f"{page}{i:04d}", "title": f"T {page} {i}", "body": "<p>x</p>", **extra}
        for i in range(n)
    ]


def assert_bounded(name: str, fh: Firehose, got: list) -> None:
    assert len(got) <= CAP + 1, (name, "buffered more than cap + 1 records", len(got))
    assert fh.list_requests <= MAX_REQUESTS, (
        name,
        "client kept requesting pages",
        fh.list_requests,
        fh.all_requests[:6],
    )
    print(f"ok {name}: {fh.list_requests} list request(s), {len(got)} record(s) kept")


# ---------------------------------------------------------------------------
# Family 0: the flow engine refuses a run over the record cap, before any engine call
# (Zendesk stands in for every connector that runs through flow.py)
# ---------------------------------------------------------------------------
def zendesk_firehose(oversize_chars: int = 0):
    def handler(fh: Firehose, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "locales" in path:
            return httpx.Response(200, json={"locales": ["es"]})
        if path.endswith("/articles") and req.method == "GET":
            page = fh.list_page()
            data = rows(page, edited_at="2026-01-01T00:00:00Z", section_id=1, locale="en-us")
            if oversize_chars:
                data[0]["body"] = "<p>" + "z" * oversize_chars + "</p>"
            more = page < PAGES
            return httpx.Response(
                200,
                json={
                    "articles": data,
                    "meta": {"has_more": more, "after_cursor": f"c{page}"},
                },
            )
        return httpx.Response(404, json={"path": path})

    return Firehose(handler)


def _zendesk_conn(name: str, **cfg):
    base = {"subdomain": name, "target_locales": "es", "source_locale": "en-us"}
    return cstore.create(
        "zendesk",
        name,
        {**base, **cfg},
        {"api_token": "tok", "email": "b@x.com"},
        actor="admin",
    )


def test_flow_refuses_over_cap_run():
    conn = _zendesk_conn("flowcap", max_articles=HUGE)
    fh = zendesk_firehose()
    before_docs, before_calls = counts()[0], len(ENGINE_CALLS)
    ok, msg, _summary = cstore.zendesk_translate(conn.id, actor="auto", transport=fh.transport())
    assert not ok, msg
    assert "MAX_DOCUMENT_SEGMENTS" in msg and "more than" in msg, msg
    assert fh.list_requests <= MAX_REQUESTS, ("flow kept paging", fh.list_requests)
    assert counts()[0] == before_docs, "a refused run stored a Document"
    assert len(ENGINE_CALLS) == before_calls, "a refused run called the engine"
    from app.models import Connection

    with Session(engine) as s:
        row = s.get(Connection, conn.id)
        status, last = str(getattr(row.status, "value", row.status)), row.last_message or ""
    assert status.lower().endswith("error") and "MAX_DOCUMENT_SEGMENTS" in last, (status, last)
    print(f"ok flow.py (zendesk): over-cap run refused after {fh.list_requests} list request")


def test_flow_under_cap_still_runs():
    """A connection whose own limit is below the cap behaves as before."""
    conn = _zendesk_conn("flowok", max_articles="2")
    created: list = []

    def handler(fh: Firehose, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "locales" in path:
            return httpx.Response(200, json={"locales": ["es"]})
        if path.endswith("/articles") and req.method == "GET":
            page = fh.list_page()
            data = rows(page, 2, edited_at="2026-01-01T00:00:00Z", section_id=1, locale="en-us")
            return httpx.Response(200, json={"articles": data, "meta": {"has_more": False}})
        if "/translations/" in path and req.method == "GET":
            return httpx.Response(404, json={})
        if "/translations" in path and req.method in ("POST", "PUT"):
            created.append(path)
            return httpx.Response(201, json={"translation": {"id": 1}})
        return httpx.Response(404, json={"path": path})

    fh = Firehose(handler)
    before_calls = len(ENGINE_CALLS)
    ok, msg, _summary = cstore.zendesk_translate(conn.id, actor="auto", transport=fh.transport())
    assert ok, msg
    assert len(created) == 2 and len(ENGINE_CALLS) - before_calls == 2, (created, ENGINE_CALLS)
    print("ok flow.py (zendesk): a run under the cap is unchanged")


def test_flow_oversize_record_refused_before_buffer():
    """A listed record heavier than 10 x MAX_SEGMENT_CHARS is dropped from the buffer, the
    reason lands on the run, and no engine call is made for it."""
    conn = _zendesk_conn("flowbig", max_articles="3")
    created: list = []

    def handler(fh: Firehose, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "locales" in path:
            return httpx.Response(200, json={"locales": ["es"]})
        if path.endswith("/articles") and req.method == "GET":
            page = fh.list_page()
            data = rows(page, 3, edited_at="2026-01-01T00:00:00Z", section_id=1, locale="en-us")
            data[0]["body"] = "<p>" + "z" * (config.MAX_SEGMENT_CHARS * 10 + 50) + "</p>"
            return httpx.Response(200, json={"articles": data, "meta": {"has_more": False}})
        if "/translations/" in path and req.method == "GET":
            return httpx.Response(404, json={})
        if "/translations" in path and req.method in ("POST", "PUT"):
            created.append(path)
            return httpx.Response(201, json={"translation": {"id": 1}})
        return httpx.Response(404, json={"path": path})

    fh = Firehose(handler)
    before_calls = len(ENGINE_CALLS)
    _ok, _msg, summary = cstore.zendesk_translate(conn.id, actor="auto", transport=fh.transport())
    errs = " ".join(f.get("error") or "" for f in summary.get("failures", []))
    assert "fetch limit" in errs and "10000" in " ".join(
        str(f.get("article_id")) for f in summary.get("failures", [])
    ), summary
    assert len(created) == 2, ("the two small records are still written", created)
    assert len(ENGINE_CALLS) - before_calls == 2, ENGINE_CALLS[before_calls:]
    print("ok flow.py (zendesk): an oversize listed record is refused before it is buffered")


def test_flow_oversize_record_held_behind_watermark():
    """Review: a record dropped for size must not be skipped for good once the watermark
    moves on. The watermark stays below the dropped record's edit time, so the next run
    reports it again."""
    from app.models import Connection
    from app.services.connectors.store import _epoch

    conn = _zendesk_conn("flowhold", max_articles="3")

    def handler(fh: Firehose, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "locales" in path:
            return httpx.Response(200, json={"locales": ["es"]})
        if path.endswith("/articles") and req.method == "GET":
            page = fh.list_page()
            data = rows(page, 3, edited_at="2026-01-05T00:00:00Z", section_id=1, locale="en-us")
            data[0]["edited_at"] = "2026-01-01T00:00:00Z"
            data[0]["body"] = "<p>" + "z" * (config.MAX_SEGMENT_CHARS * 10 + 50) + "</p>"
            return httpx.Response(200, json={"articles": data, "meta": {"has_more": False}})
        if "/translations/" in path and req.method == "GET":
            return httpx.Response(404, json={})
        if "/translations" in path and req.method in ("POST", "PUT"):
            return httpx.Response(201, json={"translation": {"id": 1}})
        return httpx.Response(404, json={"path": path})

    fh = Firehose(handler)
    cstore.zendesk_translate(conn.id, actor="auto", transport=fh.transport())
    with Session(engine) as s:
        wm = int((s.get(Connection, conn.id).config or {}).get("zendesk_watermark") or 0)
    dropped = _epoch("2026-01-01T00:00:00Z")
    assert wm < dropped, ("the watermark moved past a dropped record", wm, dropped)
    print("ok flow.py (zendesk): a dropped oversize record is held behind the watermark")


# ---------------------------------------------------------------------------
# Family A: help-center and article connectors
# ---------------------------------------------------------------------------
def _detail(rid: str) -> dict:
    return {"id": rid, "title": f"T {rid}", "body": "<p>x</p>", "content": "<p>x</p>"}


def _discover(name: str, fh: Firehose, connector) -> list:
    got = connector.discover_articles()
    assert_bounded(name, fh, got)
    return got


def test_document360():
    from app.services.connectors.document360 import Document360Connector

    def handler(fh, req):
        path = req.url.path
        if path.endswith("/articles") and req.method == "GET":
            page = fh.list_page()
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": rows(page),
                    "pagination": {"page": page - 1, "has_more": True},
                },
            )
        if "/Articles/" in path:
            detail = {**_detail(path.split("/")[-2]), "project_version_id": "pv"}
            return httpx.Response(200, json={"data": detail})
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = Document360Connector(
        {"source_lang": "en", "max_articles": HUGE, "project_version_id": "pv"},
        {"api_token": "t"},
        transport=fh.transport(),
    )
    _discover("document360", fh, c)


def test_freshdesk():
    from app.services.connectors.freshdesk import FreshdeskConnector

    def handler(fh, req):
        path = req.url.path
        if path.endswith("/solutions/categories"):
            return httpx.Response(200, json=[{"id": 1}])
        if path.endswith("/categories/1/folders"):
            return httpx.Response(200, json=[{"id": 11}])
        if path.endswith("/subfolders"):
            return httpx.Response(200, json=[])
        if "/folders/" in path and path.endswith("/articles"):
            page = fh.list_page()
            return httpx.Response(200, json=rows(page))
        if "/solutions/articles/" in path:
            return httpx.Response(200, json=_detail(path.rsplit("/", 1)[-1]))
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = FreshdeskConnector(
        {"domain": "acme", "max_articles": HUGE}, {"api_key": "k"}, transport=fh.transport()
    )
    _discover("freshdesk", fh, c)


def test_intercom():
    from app.services.connectors.intercom import IntercomConnector

    def handler(fh, req):
        path = req.url.path
        if path == "/articles" and req.method == "GET":
            page = fh.list_page()
            return httpx.Response(
                200, json={"data": rows(page), "pages": {"next": {"starting_after": f"c{page}"}}}
            )
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = IntercomConnector(
        {"region": "US", "author_id": "55", "max_articles": HUGE},
        {"access_token": "t"},
        transport=fh.transport(),
    )
    _discover("intercom", fh, c)


def test_helpscout():
    from app.services.connectors.helpscout import HelpScoutConnector

    def handler(fh, req):
        path = req.url.path
        if path.endswith("/collections/SRC/articles"):
            page = fh.list_page()
            items = [{"id": f"{page}-{i}", "name": "n", "collectionId": "SRC"} for i in range(100)]
            return httpx.Response(
                200, json={"articles": {"items": items, "page": page, "pages": 1000}}
            )
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = HelpScoutConnector(
        {"collection_map": json.dumps({"SRC": {"de": "DST"}}), "max_articles": HUGE},
        {"api_key": "k"},
        transport=fh.transport(),
    )
    _discover("helpscout", fh, c)


def test_salesforce():
    from app.services.connectors.salesforce import SalesforceConnector

    inst = "https://acme.my.salesforce.com"

    def handler(fh, req):
        path = req.url.path
        if "/query" in path:
            page = fh.list_page()
            recs = [{"Id": f"ka{page}{i}", "Title": "t", "Language": "en_US"} for i in range(100)]
            return httpx.Response(
                200,
                json={
                    "records": recs,
                    "done": False,
                    "nextRecordsUrl": f"/services/data/v60.0/query/01g{page}-2000",
                },
            )
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = SalesforceConnector(
        {"login_url": "test.salesforce.com", "username": "svc@x.com", "max_articles": HUGE},
        {"client_id": "cid", "private_key": "unused"},
        transport=fh.transport(),
    )
    c._tok = ("tok", inst)
    _discover("salesforce", fh, c)


def test_zoho():
    from app.services.connectors.zoho import ZohoConnector

    def handler(fh, req):
        path = req.url.path
        if path.endswith("/api/v1/articles") and req.method == "GET":
            page = fh.list_page()
            return httpx.Response(200, json={"data": rows(page)})
        if "/api/v1/articles/" in path:
            return httpx.Response(200, json=_detail(path.rsplit("/", 1)[-1]))
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = ZohoConnector(
        {
            "api_domain": "desk.zoho.eu",
            "org_id": "600000000042",
            "source_locale": "en",
            "max_articles": HUGE,
        },
        {"access_token": "t"},
        transport=fh.transport(),
    )
    _discover("zoho", fh, c)


def test_wordpress():
    from app.services.connectors.wordpress import WordPressConnector

    def handler(fh, req):
        path = req.url.path
        if "/pll/" in path:
            return httpx.Response(404, json={})
        if path.endswith("/wp/v2/posts") and req.method == "GET":
            page = fh.list_page()
            posts = [
                {"id": page * 1000 + i, "title": {"raw": "t"}, "content": {"raw": "<p>x</p>"}}
                for i in range(100)
            ]
            return httpx.Response(200, json=posts, headers={"X-WP-TotalPages": "100000"})
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = WordPressConnector(
        {
            "base_url": "https://blog.acme.com",
            "post_types": "posts",
            "source_category": "3",
            "max_articles": HUGE,
        },
        {"username": "u", "application_password": "p"},
        transport=fh.transport(),
    )
    _discover("wordpress", fh, c)


# ---------------------------------------------------------------------------
# Family B: CMS and commerce connectors
# ---------------------------------------------------------------------------
def test_contentful():
    from app.services.connectors.contentful import ContentfulConnector

    def handler(fh, req):
        path = req.url.path
        if path.endswith("/entries") and req.method == "GET":
            page = fh.list_page()
            items = [
                {
                    "sys": {"id": f"e{page}-{i}", "version": 1, "updatedAt": "2026-01-01"},
                    "fields": {"title": {"en-US": "t"}},
                }
                for i in range(100)
            ]
            return httpx.Response(200, json={"items": items, "total": 10**9})
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = ContentfulConnector(
        {
            "space_id": "sp",
            "environment": "master",
            "region": "US",
            "content_types": "blogPost",
            "source_locale": "en-US",
            "max_entries": HUGE,
        },
        {"cma_token": "CFPAT-x"},
        transport=fh.transport(),
    )
    _discover("contentful", fh, c)


def test_strapi():
    from app.services.connectors.strapi import StrapiConnector

    def handler(fh, req):
        path = req.url.path
        if "i18n/locales" in path:
            return httpx.Response(200, json=[{"code": "en"}])
        if path.endswith("/articles") and req.method == "GET":
            page = fh.list_page()
            data = [
                {"id": page * 1000 + i, "documentId": f"d{page}-{i}", "title": "t"}
                for i in range(100)
            ]
            return httpx.Response(
                200, json={"data": data, "meta": {"pagination": {"pageCount": 100000}}}
            )
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = StrapiConnector(
        {
            "base_url": "http://strapi.test",
            "content_types": "articles",
            "source_locale": "en",
            "strapi_version": "v5",
            "max_entries": HUGE,
        },
        {"api_token": "tok"},
        transport=fh.transport(),
    )
    _discover("strapi", fh, c)


def test_shopify():
    from app.services.connectors.shopify import ShopifyConnector

    def handler(fh, req):
        page = fh.list_page()
        edges = [
            {
                "node": {
                    "resourceId": f"gid://shopify/Product/{page * 1000 + i}",
                    "translatableContent": [
                        {"key": "title", "value": "t", "digest": "d", "locale": "en"}
                    ],
                    "translations": [],
                }
            }
            for i in range(100)
        ]
        block = {"edges": edges, "pageInfo": {"hasNextPage": True, "endCursor": f"c{page}"}}
        return httpx.Response(200, json={"data": {"translatableResources": block}})

    fh = Firehose(handler)
    c = ShopifyConnector(
        {
            "store_name": "acme",
            "resource_types": "PRODUCT",
            "source_locale": "en",
            "max_entries": HUGE,
        },
        {"access_token": "shpat_x"},
        transport=fh.transport(),
    )
    _discover("shopify", fh, c)


def test_akeneo():
    from app.services.connectors.akeneo import AkeneoConnector

    def handler(fh, req):
        path = req.url.path
        if path.endswith("/products-uuid") and req.method == "GET":
            page = fh.list_page()
            items = [
                {"uuid": f"u{page}-{i}", "identifier": f"p{page}-{i}", "values": {}}
                for i in range(100)
            ]
            return httpx.Response(200, json={"_embedded": {"items": items}})
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = AkeneoConnector(
        {
            "base_url": "http://akeneo.test",
            "content_types": "product",
            "source_locale": "en_US",
            "max_entries": HUGE,
        },
        {"access_token": "tok"},
        transport=fh.transport(),
    )
    _discover("akeneo", fh, c)


def test_hubspot():
    from app.services.connectors.hubspot import HubSpotConnector

    def handler(fh, req):
        path = req.url.path
        if path.endswith("/site-pages") and req.method == "GET":
            page = fh.list_page()
            results = [{"id": str(page * 1000 + i)} for i in range(100)]
            return httpx.Response(
                200, json={"results": results, "paging": {"next": {"after": f"c{page}"}}}
            )
        return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "name": "n"})

    fh = Firehose(handler)
    c = HubSpotConnector(
        {"content_types": "site-pages", "source_language": "en", "max_entries": HUGE},
        {"access_token": "pat-na1-x"},
        transport=fh.transport(),
    )
    _discover("hubspot", fh, c)


# ---------------------------------------------------------------------------
# Family C: Notion (child-page listing and the per-page block tree)
# ---------------------------------------------------------------------------
PARENT_ID = "11111111-1111-1111-1111-111111111111"
BIG_ID = "22222222-2222-2222-2222-222222222222"
LONG_ID = "33333333-3333-3333-3333-333333333333"


def _notion(fh, **cfg):
    from app.services.connectors.notion import NotionConnector

    base = {"parent_page_id": PARENT_ID, "source_locale": "en", "max_pages": HUGE}
    base.update(cfg)
    return NotionConnector(base, {"token": "secret_x"}, transport=fh.transport())


def test_notion_child_listing():
    def handler(fh, req):
        path = req.url.path
        if path.endswith("/pages/" + PARENT_ID):
            return httpx.Response(200, json={"id": PARENT_ID, "properties": {}})
        if path.endswith(f"/blocks/{PARENT_ID}/children"):
            page = fh.list_page()
            results = [
                {
                    "id": f"pg{page}-{i}",
                    "type": "child_page",
                    "child_page": {"title": f"P {page} {i}"},
                    "last_edited_time": "2026-01-01T00:00:00.000Z",
                }
                for i in range(100)
            ]
            return httpx.Response(
                200, json={"results": results, "has_more": True, "next_cursor": f"c{page}"}
            )
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    _discover("notion child pages", fh, _notion(fh))


def test_notion_oversize_page_refused():
    """A page whose blocks weigh more than 10 x MAX_SEGMENT_CHARS is refused while it is
    read: the tree is not buffered and no further block pages are requested."""
    from app.filters.base import InputLimitError

    def handler(fh, req):
        path = req.url.path
        if path.endswith("/pages/" + BIG_ID):
            return httpx.Response(200, json={"id": BIG_ID, "properties": {}})
        if path.endswith(f"/blocks/{BIG_ID}/children"):
            page = fh.list_page()
            blocks = [
                {
                    "id": f"b{page}-{i}",
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": "w" * 900}}],
                    },
                }
                for i in range(100)
            ]
            return httpx.Response(
                200, json={"results": blocks, "has_more": True, "next_cursor": f"c{page}"}
            )
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    c = _notion(fh)
    try:
        c.fetch_page_content(BIG_ID)
    except InputLimitError as e:
        assert "10 x MAX_SEGMENT_CHARS" in str(e), e
    else:
        raise AssertionError("an oversize Notion page was read in full")
    assert fh.list_requests <= MAX_REQUESTS, ("kept reading blocks", fh.list_requests)
    print(f"ok notion page tree: refused after {fh.list_requests} block page(s)")


# ---------------------------------------------------------------------------
# Family D: Airtable webhook door, Jira and Confluence (one-error connectors)
# ---------------------------------------------------------------------------
def conn_status(cid):
    from app.models import Connection

    with Session(engine) as s:
        row = s.get(Connection, cid)
        return str(getattr(row.status, "value", row.status)), (row.last_message or "")


def test_airtable_webhook_fetch_bounded():
    """max_records of 1000000 on the webhook door: the fetch asks for cap + 1 records and
    stops; the run is then refused by the #576 check, before rows and engine calls."""

    def handler(fh, req):
        path = req.url.path
        if path == "/v0/meta/bases/appFB579/tables":
            fields = [
                {"name": "Notes", "type": "multilineText"},
                {"name": "Notes (es-MX)", "type": "multilineText"},
            ]
            return httpx.Response(
                200, json={"tables": [{"id": "tbl1", "name": "Content", "fields": fields}]}
            )
        if req.method == "GET" and path.startswith("/v0/appFB579/"):
            page = fh.list_page()
            recs = [{"id": f"r{page}-{i}", "fields": {"Notes": f"Note {i}"}} for i in range(100)]
            body = {"records": recs}
            if page < PAGES:
                body["offset"] = f"o{page}"
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"path": path})

    fh = Firehose(handler)
    conn = cstore.create(
        "airtable",
        "FB579 webhook",
        {
            "source_field": "Notes",
            "naming_pattern": "{src} ({loc})",
            "source_locale": "en-US",
            "base_id": "appFB579",
            "table": "Content",
            "max_records": HUGE,
        },
        {"api_key": "patFB579"},
        actor="admin",
    )
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _s = cstore.dynamic_translate(
        conn.id, base_id="appFB579", table="Content", transport=fh.transport()
    )
    assert not ok and "MAX_DOCUMENT_SEGMENTS" in msg, (ok, msg)
    assert fh.list_requests <= MAX_REQUESTS, ("kept paging", fh.list_requests)
    assert counts() == before and len(ENGINE_CALLS) == calls, "refused run stored rows or spent MT"
    print(f"ok airtable webhook: fetch stopped after {fh.list_requests} list request")


def test_jira_oversize_issue_refused():
    conn = cstore.create(
        "jira",
        "FB579 jira",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "target_mode": "comment",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    writes: list = []

    def handler(fh, req):
        if req.url.path == "/rest/api/3/search/jql":
            fh.list_page()
            big = "S" * (config.MAX_SEGMENT_CHARS * 10 + 50)
            issues = [{"key": "LOC-1", "fields": {"summary": big, "description": None}}]
            return httpx.Response(200, json={"isLast": True, "issues": issues})
        if req.method in ("POST", "PUT"):
            writes.append(req.url.path)
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _s = cstore.jira_translate(conn.id, actor="auto", transport=fh.transport())
    assert not ok and "fetch limit" in msg and "LOC-1" in msg, (ok, msg)
    assert counts() == before and len(ENGINE_CALLS) == calls and not writes, "refused run did work"
    status, last = conn_status(conn.id)
    assert status.lower().endswith("error") and "fetch limit" in last, (status, last)
    print("ok jira: an oversize issue stops the run before it is buffered, reason on the run")


def test_confluence_oversize_page_refused():
    conn = cstore.create(
        "confluence",
        "FB579 confluence",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "HELP",
            "target_locales": "es-MX",
            "source_locale": "en-US",
            "title_pattern": "[{loc}] {title}",
            "max_pages": HUGE,
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    created: list = []

    def handler(fh, req):
        path = req.url.path
        if path == "/wiki/api/v2/spaces":
            return httpx.Response(
                200, json={"results": [{"id": "1", "key": "HELP", "name": "Help"}]}
            )
        if path == "/wiki/api/v2/spaces/1/pages" and req.method == "GET":
            fh.list_page()
            body = "<p>" + "w" * (config.MAX_SEGMENT_CHARS * 10 + 50) + "</p>"
            page = {
                "id": "9",
                "title": "Big",
                "spaceId": "1",
                "status": "current",
                "version": {"number": 1},
                "body": {"storage": {"value": body, "representation": "storage"}},
            }
            return httpx.Response(200, json={"results": [page]})
        if req.method == "POST":
            created.append(path)
            return httpx.Response(200, json={"id": "c1"})
        return httpx.Response(404, json={})

    fh = Firehose(handler)
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _s = cstore.confluence_translate(conn.id, actor="auto", transport=fh.transport())
    assert not ok and "fetch limit" in msg, (ok, msg)
    assert counts() == before and len(ENGINE_CALLS) == calls and not created, "refused run did work"
    print("ok confluence: an oversize page stops the run before it is buffered")


# ---------------------------------------------------------------------------
# Family E (Review gate): no silent partial document, no unbounded export buffer
# ---------------------------------------------------------------------------
def _three_articles_one_heavy(fh: Firehose, req: httpx.Request, export=None):
    path = req.url.path
    if "locales" in path:
        return httpx.Response(200, json={"locales": ["es"]})
    if path.endswith("/incremental/articles") and export is not None:
        return httpx.Response(200, json=export)
    if path.endswith("/articles") and req.method == "GET":
        page = fh.list_page()
        data = rows(page, 3, edited_at="2026-01-05T00:00:00Z", section_id=1, locale="en-us")
        data[0]["body"] = "<p>" + "z" * (config.MAX_SEGMENT_CHARS * 10 + 50) + "</p>"
        return httpx.Response(200, json={"articles": data, "meta": {"has_more": False}})
    if "/translations/" in path and req.method == "GET":
        return httpx.Response(404, json={})
    if "/translations" in path and req.method in ("POST", "PUT"):
        return httpx.Response(201, json={"translation": {"id": 1}})
    return httpx.Response(404, json={"path": path})


def test_gate_pull_door_refuses_dropped_record():
    """The generic pull stores ONE Document for the whole list. A record dropped for size
    must refuse the pull with the reason, never store the Document without it."""
    conn = _zendesk_conn("pullbig", max_articles="3")
    fh = Firehose(_three_articles_one_heavy)
    before = counts()
    ok, msg, _n = cstore.pull_connection(conn.id, actor="admin", transport=fh.transport())
    assert not ok and "fetch limit" in msg and "10000" in msg, (ok, msg)
    assert counts() == before, "a pull missing a dropped record stored a partial Document"
    status, last = conn_status(conn.id)
    assert status.lower().endswith("error") and "fetch limit" in last, (status, last)
    print("ok pull door: a size-dropped record refuses the pull instead of a partial Document")


def test_gate_notion_block_budget_refused():
    """A Notion page with more blocks than the per-page block budget used to be read up to
    the budget and translated half read. It is now refused whole."""
    from app.filters.base import InputLimitError

    def handler(fh, req):
        path = req.url.path
        if path.endswith("/pages/" + LONG_ID):
            return httpx.Response(200, json={"id": LONG_ID, "properties": {}})
        if path.endswith(f"/blocks/{LONG_ID}/children"):
            page = fh.list_page()
            blocks = [
                {
                    "id": f"b{page}-{i}",
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": "w"}}]},
                }
                for i in range(100)
            ]
            return httpx.Response(
                200, json={"results": blocks, "has_more": True, "next_cursor": f"c{page}"}
            )
        return httpx.Response(404, json={})

    saved = config.MAX_SEGMENT_CHARS
    config.MAX_SEGMENT_CHARS = 1_000_000  # the size budget must not be what trips here
    try:
        fh = Firehose(handler)
        try:
            _title, blocks, _h = _notion(fh).fetch_page_content(LONG_ID)
        except InputLimitError as e:
            assert "blocks" in str(e) and "not translated" in str(e), e
        else:
            raise AssertionError(f"a long Notion page was read half: {len(blocks)} blocks kept")
    finally:
        config.MAX_SEGMENT_CHARS = saved
    print(f"ok notion page tree: over the block budget refused after {fh.list_requests} pages")


def test_gate_zendesk_export_oversize_not_buffered():
    """The incremental-export backstop buffered full article bodies with no size check. An
    oversize changed article is now a small stub, refused per record on the run and held
    behind the watermark."""
    from app.models import Connection
    from app.services.connectors.base import item_chars
    from app.services.connectors.store import _epoch
    from app.services.connectors.zendesk import ZendeskConnector

    heavy = {
        "id": 777,
        "title": "Heavy",
        "body": "<p>" + "q" * (config.MAX_SEGMENT_CHARS * 10 + 50) + "</p>",
        "edited_at": "2026-02-01T00:00:00Z",
        "section_id": 1,
        "locale": "en-us",
    }
    export = {"articles": [heavy], "end_time": 1, "next_page": None}

    def handler(fh, req):
        return _three_articles_one_heavy(fh, req, export=export)

    c = ZendeskConnector(
        {"subdomain": "exp", "max_articles": "3"},
        {"api_token": "t", "email": "b@x.com"},
        transport=Firehose(handler).transport(),
    )
    got, _end = c.incremental_articles(start_time=0)
    assert got and item_chars(got[0]) < 1000, ("export buffered the heavy body", item_chars(got))

    conn = _zendesk_conn("exphold", max_articles="3")
    with Session(engine) as s:
        row = s.get(Connection, conn.id)
        row.config = {**(row.config or {}), "zendesk_watermark": _epoch("2026-01-01T00:00:00Z")}
        s.add(row)
        s.commit()
    fh = Firehose(handler)
    _ok, _msg, summary = cstore.zendesk_translate(conn.id, actor="auto", transport=fh.transport())
    fails = {str(f.get("article_id")): f.get("error") or "" for f in summary.get("failures", [])}
    assert "fetch limit" in fails.get("777", ""), summary
    with Session(engine) as s:
        wm = int((s.get(Connection, conn.id).config or {}).get("zendesk_watermark") or 0)
    assert wm < _epoch(heavy["edited_at"]), ("watermark moved past the heavy article", wm)
    print("ok zendesk export: an oversize changed article is a stub, refused and held")


def main():
    """No argument: run every section. Names as arguments: run only those (fail-before runs)."""
    wanted = sys.argv[1:]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not wanted or name in wanted):
            fn()
    print("FETCH BOUND 579 OK")


if __name__ == "__main__":
    main()
