"""#587 (part 2): the Notion pull reports the pages it could not read, and counts page text
against the per-run character budget.

Hermetic and offline: throwaway LOC_TMS_DATA_DIR, mock transports (httpx.MockTransport),
ASCII-only prints. The caps are lowered in-process so the files stay tiny:
MAX_DOCUMENT_SEGMENTS = 5 and MAX_SEGMENT_CHARS = 1000 give a per-record limit of 10000
characters and a per-run budget of 10000 characters (the larger of the derived 500 and one
maximum record). Every record below is about 3000 characters: each is far under the per-record
limit and the record count is under the document cap, so only the run budget can refuse a
run of four of them (about 12000 characters).

Final line: "NOTION SKIPPED 587 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_notionskip587_")

import httpx
from sqlmodel import Session, func, select

from app import config
from app.db import engine, init_db
from app.models import Connection, Document, Segment
from app.services import translate
from app.services.connectors import store as cstore
from app.services.connectors.base import run_char_budget

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
    try:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = 50000, 100000
        assert run_char_budget() == 50_000_000, run_char_budget()  # the fixed ceiling
    finally:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = saved
    print("ok budget: min(derived, ceiling), never below one record")


# ---------------------------------------------------------------------------
# Notion: pages the pull could not read are reported; page text counts against the budget
# ---------------------------------------------------------------------------
PARENT_ID = "11111111-1111-1111-1111-111111111111"
OK1 = "aaaaaaaa-0000-0000-0000-000000000001"
BAD1 = "bbbbbbbb-0000-0000-0000-000000000001"
BAD2 = "bbbbbbbb-0000-0000-0000-000000000002"


def _notion_handler(pages: dict, text_chars: int = 20):
    """`pages` maps page id -> title; an id starting with 'bad' answers 403 on its blocks."""

    def handler(mock, req):
        path = req.url.path
        if path.endswith("/pages/" + PARENT_ID):
            return httpx.Response(200, json={"id": PARENT_ID, "properties": {}})
        if path.endswith(f"/blocks/{PARENT_ID}/children"):
            results = [
                {
                    "id": pid,
                    "type": "child_page",
                    "child_page": {"title": title},
                    "last_edited_time": "2026-01-01T00:00:00.000Z",
                }
                for pid, title in pages.items()
            ]
            return httpx.Response(200, json={"results": results, "has_more": False})
        for pid in pages:
            if path.endswith(f"/pages/{pid}"):
                return httpx.Response(200, json={"id": pid, "properties": {}})
            if path.endswith(f"/blocks/{pid}/children"):
                if pid.startswith("bad") or pid.startswith("bbbbbbbb"):
                    return httpx.Response(403, json={"message": "no access"})
                blocks = [
                    {
                        "id": f"{pid}-b{i}",
                        "type": "paragraph",
                        "has_children": False,
                        "paragraph": {
                            "rich_text": [{"type": "text", "text": {"content": "w" * text_chars}}]
                        },
                    }
                    for i in range(2)
                ]
                return httpx.Response(200, json={"results": blocks, "has_more": False})
        return httpx.Response(404, json={})

    return handler


def _notion_conn(name: str):
    return cstore.create(
        "notion",
        name,
        {"parent_page_id": PARENT_ID, "source_locale": "en", "target_locales": "es"},
        {"token": "secret_x"},
        actor="admin",
    )


def test_notion_pull_reports_skipped_pages():
    pages = {OK1: "Good one", BAD1: "Locked page", BAD2: "Another locked"}
    mock = Mock(_notion_handler(pages))
    conn = _notion_conn("rbnotion")
    ok, msg, doc_id = cstore.pull_connection(conn.id, actor="admin", transport=mock.transport())
    assert ok and doc_id, (ok, msg)
    assert "Skipped 2 page(s)" in msg, msg
    assert f"{BAD1} (Locked page)" in msg and f"{BAD2} (Another locked)" in msg, msg
    status, last = conn_status(conn.id)
    assert "Skipped 2 page(s)" in last, (status, last)
    print("ok notion pull: unreadable pages counted and named on the run")


def test_notion_pull_over_budget():
    pages = {OK1: "One", BAD1.replace("b", "c"): "Two", BAD2.replace("b", "d"): "Three"}
    mock = Mock(_notion_handler(pages, text_chars=3000))
    conn = _notion_conn("rbnotionbig")
    before, calls = counts(), len(ENGINE_CALLS)
    ok, msg, _n = cstore.pull_connection(conn.id, actor="admin", transport=mock.transport())
    assert not ok and "per-run limit" in msg, (ok, msg)
    assert counts() == before and len(ENGINE_CALLS) == calls, "refused pull stored a Document"
    print("ok notion pull: page text over the run budget refuses the pull whole")


def main():
    """No argument: run every section. Names as arguments: run only those (fail-before runs)."""
    wanted = sys.argv[1:]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not wanted or name in wanted):
            fn()
    print("NOTION SKIPPED 587 OK")


if __name__ == "__main__":
    main()
