"""Notion fill_target never wipes the target before the replacement is accepted (#466).

Offline: httpx.MockTransport against a STRICT fake Notion that, like the real API, refuses

  - a `table` created without its table_row children inline,
  - a `column_list` created without columns that carry their own children inline,
  - a Notion-hosted file block (`image.type == "file"`),
  - any block of a made-up `bad_block` type.

  [A] an UPDATE whose source carries a table + a column_list lands (children inline), the
      old body is archived only after the new one was accepted.
  [B] an UPDATE the API refuses leaves the OLD body byte-for-byte and rolls back the blocks
      the failed append created (no half page, no duplicate), and fails the pair with the
      API error.
  [C] a hosted-file image is skipped with a note instead of failing the whole page; the
      surrounding text still lands.
  [D] a retry after [B] converges (one new body, no old body, no leftovers).
  [E] an unreadable current body fails the fill without appending beside it.
  [F] a network drop mid-append rolls back the partial new body; [G] a failed FIRST archive
      rolls back the new body; [H] a failed LATER archive keeps the new body and a retry
      converges to exactly one body.

ASCII-only prints; final line is 'verify_notion_fill_safety OK'.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# verify_notion owns the fake workspace + the throwaway data dir bootstrap.
import json

import httpx

from app.db import init_db
from app.services.connectors.notion import NotionConnector
from tests.verify_notion import FakeNotion, _block, _run


class StrictNotion(FakeNotion):
    """FakeNotion that enforces Notion's create-time rules on PATCH /blocks/{id}/children."""

    def __init__(self):
        super().__init__()
        self.refused: list = []
        self.fail_reads_for: set = set()
        self.drop_on_append_n: int = 0  # raise a transport error on the Nth append (1-based)
        self.fail_delete_of: set = set()  # block ids whose DELETE answers 500
        self._appends = 0

    @staticmethod
    def _problem(ch):
        t = ch.get("type")
        typ = ch.get(t) or {}
        if t == "bad_block":
            return "bad_block is not a valid block type"
        if t == "paragraph" and "REFUSE" in str(typ):
            return "body.children[1].paragraph validation failed"
        if t == "table" and not typ.get("children"):
            return "table.children should be defined"
        if t == "column_list":
            cols = typ.get("children") or []
            if len(cols) < 2:
                return "column_list needs at least 2 columns"
            for col in cols:
                if not (col.get("column") or {}).get("children"):
                    return "column.children should be defined"
        if t in ("image", "file", "pdf", "video", "audio") and typ.get("type") == "file":
            return "Cannot create a file block with a Notion-hosted file"
        for k in typ.get("children") or []:
            p = StrictNotion._problem(k)
            if p:
                return p
        return None

    def _handle(self, req):
        path = req.url.path
        seg = [p for p in path.split("/") if p]
        if (
            req.method == "GET"
            and "/blocks/" in path
            and path.endswith("/children")
            and seg[-2] in self.fail_reads_for
        ):
            return httpx.Response(500, json={"object": "error", "code": "internal"})
        if req.method == "DELETE" and seg and seg[-1] in self.fail_delete_of:
            return httpx.Response(500, json={"object": "error", "code": "internal"})
        if req.method == "PATCH" and "/blocks/" in path and path.endswith("/children"):
            self._appends += 1
            if self.drop_on_append_n and self._appends == self.drop_on_append_n:
                raise httpx.ConnectError("connection reset mid-sequence", request=req)
            body = json.loads(req.content)
            for ch in body.get("children", []):
                prob = self._problem(ch)
                if prob:
                    self.refused.append(prob)
                    return httpx.Response(
                        400, json={"object": "error", "code": "validation_error", "message": prob}
                    )
        return super()._handle(req)


def _para(bid, text):
    return _block(bid, "paragraph", [_run(text)])


def _table(bid, cells):
    rows = [
        {
            "object": "block",
            "id": f"{bid}-r{i}",
            "type": "table_row",
            "table_row": {"cells": [[_run(c)] for c in row]},
            "has_children": False,
        }
        for i, row in enumerate(cells)
    ]
    return {
        "object": "block",
        "id": bid,
        "type": "table",
        "table": {
            "table_width": len(cells[0]),
            "has_column_header": True,
            "has_row_header": False,
            "children": rows,
        },
        "has_children": True,
    }


def _columns(bid, left, right):
    def col(cid, text):
        return {
            "object": "block",
            "id": cid,
            "type": "column",
            "column": {"children": [_para(cid + "p", text)]},
            "has_children": True,
        }

    return {
        "object": "block",
        "id": bid,
        "type": "column_list",
        "column_list": {"children": [col(bid + "a", left), col(bid + "b", right)]},
        "has_children": True,
    }


def _hosted_image(bid):
    return {
        "object": "block",
        "id": bid,
        "type": "image",
        "image": {
            "type": "file",
            "file": {"url": "https://s3.example/x.png?sig=1", "expiry_time": "2026-01-01"},
            "caption": [],
        },
        "has_children": False,
    }


def _setup(old_texts=("OLD one", "OLD two")):
    f = StrictNotion()
    tid = "7a7a7a7a-0000-4000-8000-00000000000a"
    f.pages[tid] = {"title": "[de] t", "archived": False, "parent_id": f.parent}
    f.children[tid] = [_para("old%d" % i, t) for i, t in enumerate(old_texts)]  # noqa: UP031
    conn = NotionConnector(
        {"parent_page_id": f.parent, "source_locale": "en"},
        {"token": "ntn_testtoken"},
        transport=f.transport(),
    )
    return f, tid, conn


def _fields(blocks):
    return {"locale": "de", "title": "Titel", "blocks": blocks}


def _texts(f, tid):
    return f.body_texts(tid)


def test_a_table_and_columns_land():
    f, tid, conn = _setup()
    blocks = [
        _para("n1", "NEW intro"),
        _table("tb", [["A", "B"], ["C", "D"]]),
        _columns("cl", "left text", "right text"),
    ]
    res = conn.fill_target(tid, _fields(blocks))
    assert res.ok, ("fill refused", res.message, f.refused)
    kids = f.children[tid]
    assert [b["type"] for b in kids] == ["paragraph", "table", "column_list"], kids
    assert len(kids[1]["table"]["children"]) == 2, "table rows carried inline"
    assert "OLD one" not in str(kids) and "OLD two" not in str(kids), "old body archived"
    assert f.pages[tid]["title"].endswith("Titel"), f.pages[tid]
    print("  [A] table + column_list land with children inline; old body archived after")


def test_b_refused_update_keeps_old_body():
    f, tid, conn = _setup()
    before = [dict(b) for b in f.children[tid]]
    blocks = [_para("n1", "NEW intro"), _para("n2", "REFUSE ME")]
    res = conn.fill_target(tid, _fields(blocks))
    assert not res.ok and "400" in res.message, res
    assert f.children[tid] == before, ("old body must survive a refused append", f.children[tid])
    assert f.pages[tid]["title"] == "[de] t", "title untouched when the body was refused"
    print(
        "  [B] refused append: old body intact, new blocks rolled back, pair fails with the error"
    )
    return f, tid, conn


def test_c_hosted_image_skipped():
    f, tid, conn = _setup()
    blocks = [_para("n1", "Before"), _hosted_image("im"), _para("n2", "After")]
    res = conn.fill_target(tid, _fields(blocks))
    assert res.ok and "1 block(s) not copied (image)" in res.message, res
    assert _texts(f, tid) == ["Before", "After"], _texts(f, tid)
    print("  [C] hosted-file image skipped with a note; surrounding text lands")


def test_d_retry_converges(f, tid, conn):
    blocks = [_para("n1", "NEW intro"), _table("tb", [["A", "B"]])]
    res = conn.fill_target(tid, _fields(blocks))
    assert res.ok, res
    kids = f.children[tid]
    assert [b["type"] for b in kids] == ["paragraph", "table"], kids
    print("  [D] a retry after a refused fill converges to exactly the new body")


def test_e_unreadable_body_fails_closed():
    f, tid, conn = _setup()
    f.fail_reads_for.add(tid)
    before = list(f.children[tid])
    res = conn.fill_target(tid, _fields([_para("n1", "NEW")]))
    assert not res.ok and "read current body" in res.message, res
    assert f.children[tid] == before, "nothing appended beside an unreadable body"
    print("  [E] unreadable current body: fill fails, nothing written")


def _toggle(bid, text, kid_text):
    return {
        "object": "block",
        "id": bid,
        "type": "toggle",
        "toggle": {"rich_text": [_run(text)], "children": [_para(bid + "k", kid_text)]},
        "has_children": True,
    }


def test_f_network_drop_mid_append_keeps_old_body():
    f, tid, conn = _setup()
    before = [dict(b) for b in f.children[tid]]
    f.drop_on_append_n = 2  # 1st append (top level) lands, the nested one dies
    res = conn.fill_target(tid, _fields([_para("n1", "NEW"), _toggle("tg", "T", "inner")]))
    assert not res.ok, res
    assert f.children[tid] == before, ("partial new body must be rolled back", f.children[tid])
    # a clean retry converges
    f.drop_on_append_n = 0
    res = conn.fill_target(tid, _fields([_para("n1", "NEW"), _toggle("tg", "T", "inner")]))
    assert res.ok, res
    assert [b["type"] for b in f.children[tid]] == ["paragraph", "toggle"], f.children[tid]
    print("  [F] network drop mid-sequence: partial append rolled back, old body intact")


def test_g_archive_failure_leaves_one_body():
    f, tid, conn = _setup()
    before = [dict(b) for b in f.children[tid]]
    f.fail_delete_of.add(before[0]["id"])  # the first old block cannot be archived
    res = conn.fill_target(tid, _fields([_para("n1", "NEW")]))
    assert not res.ok and "trash old block" in res.message, res
    assert f.children[tid] == before, ("no duplicate body after a failed swap", f.children[tid])
    print("  [G] failed archive before anything was removed: new blocks rolled back")


def test_h_later_archive_failure_converges():
    f, tid, conn = _setup(("OLD one", "OLD two", "OLD three"))
    second = f.children[tid][1]["id"]
    f.fail_delete_of.add(second)  # the swap dies after the first old block is gone
    res = conn.fill_target(tid, _fields([_para("n1", "NEW")]))
    assert not res.ok and "trash old block" in res.message, res
    texts = _texts(f, tid)
    assert "NEW" in texts, ("the new body must stay once old blocks are gone", texts)
    assert "OLD one" not in texts, texts
    f.fail_delete_of.clear()
    res = conn.fill_target(tid, _fields([_para("n1", "NEW")]))
    assert res.ok, res
    assert _texts(f, tid) == ["NEW"], ("a retry converges to exactly the new body", _texts(f, tid))
    print("  [H] failed archive mid-swap: new body kept, retry converges to one body")


def main():
    init_db()
    print("verify_notion_fill_safety: write-then-swap (#466)")
    test_a_table_and_columns_land()
    f, tid, conn = test_b_refused_update_keeps_old_body()
    test_c_hosted_image_skipped()
    test_d_retry_converges(f, tid, conn)
    test_e_unreadable_body_fails_closed()
    test_f_network_drop_mid_append_keeps_old_body()
    test_g_archive_failure_leaves_one_body()
    test_h_later_archive_failure_converges()
    print("verify_notion_fill_safety OK")


if __name__ == "__main__":
    main()
