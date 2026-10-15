"""Notion connector — the FIRST T3 (document-per-locale) build — fully offline via
httpx.MockTransport (a fake Notion workspace).

Notion is T3: no native localization, so a "translation" is a CHILD page the
connector creates and tracks in ConnectorIdMap via strategy.DocPerLocaleStrategy
(reserve-then-fill + the 7-case reconciliation state table). Write-back rides the
shared flow engine with ZERO engine edits.

Covers (SPEC-t3-notion-helpscout Tests list + the orchestrator brief):
  - notion_blocks walker: nested block round-trip preserving annotations + link;
    2000-char leaf split across runs; structure_ok
  - DocPerLocaleStrategy.exists_target reads the MAP, never content (a blank/partial
    translation with a `done` row still counts as existing; a building row does not)
  - full notion_translate flow: creates one child page per locale, done id-map rows,
    translated body written to the target
  - resumable build (THE critical one): a crash after reserve (fill fails) -> retry
    PATCHes the SAME target, never double-creates (exactly one ConnectorIdMap row,
    one target id)
  - reconciliation rules 1-7, one test each (rule 6 = fail-closed needs_link, the
    load-bearing one: a pre-existing target unknown to the map is never overwritten)
  - content-hash suppression; org isolation (foreign cid -> 404); retry_failed;
    no watermark (poll-first); GDPR/connection-delete sweeps the id-map rows
  - CLI notion-translate + notion-resolve-link; web tile/type/panel/endpoint

ASCII-only prints; final line is 'verify_notion OK'.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_notion_")

import json

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import ConnectorIdMap, Segment, SegStatus
from app.services import connectors as _reg  # archive-tier spec introspection
from app.services.connectors import idmap, notion_blocks
from app.services.connectors import store as cstore


# --------------------------------------------------------------------------- #
# Fake Notion workspace (httpx.MockTransport).
# --------------------------------------------------------------------------- #
def _run(content, annotations=None, link=None):
    ann = {
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "code": False,
        "color": "default",
    }
    ann.update(annotations or {})
    return {
        "type": "text",
        "text": {"content": content, "link": link},
        "annotations": ann,
        "plain_text": content,
        "href": (link or {}).get("url") if link else None,
    }


def _block(bid, btype, runs, has_children=False):
    return {
        "object": "block",
        "id": bid,
        "type": btype,
        btype: {"rich_text": runs, "color": "default"},
        "has_children": has_children,
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": "2026-01-01T00:00:00.000Z",
    }


def _code_block(bid, code_runs, caption_runs, language="python"):
    """A Notion `code` block: `rich_text` is the code text (never translated),
    `caption` is translatable prose (FINDING 3)."""
    return {
        "object": "block",
        "id": bid,
        "type": "code",
        "code": {"rich_text": code_runs, "caption": caption_runs, "language": language},
        "has_children": False,
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": "2026-01-01T00:00:00.000Z",
    }


class FakeNotion:
    def __init__(self):
        self.parent = "0a0a0a0a-0000-4000-8000-000000000001"
        # page_id -> {"title", "archived", "parent_id"}
        self.pages: dict = {}
        # id -> list of child block dicts served by GET /blocks/{id}/children
        self.children: dict = {self.parent: []}
        self.pages[self.parent] = {"title": "Root", "archived": False, "parent_id": None}
        self._seq = 0
        self.creates: list = []  # (source_id, title)
        self.appended: list = []  # (parent_id, n_blocks)
        self.deleted: list = []  # block ids archived
        self.fail_fill_once_for: set = set()  # target ids whose NEXT append 500s once

    def transport(self):
        return httpx.MockTransport(self._handle)

    # -- source setup --
    def add_source(self, sid, title, body_blocks):
        self.pages[sid] = {"title": title, "archived": False, "parent_id": self.parent}
        self.children[self.parent].append(
            {
                "object": "block",
                "id": sid,
                "type": "child_page",
                "child_page": {"title": title},
                "has_children": True,
                "last_edited_time": "2026-02-01T00:00:00.000Z",
            }
        )
        self.children[sid] = list(body_blocks)

    def add_child_block_children(self, block_id, children):
        self.children[block_id] = list(children)

    def seed_preexisting_target(self, sid, title):
        """A child page under `sid` that OUR map never created (rule 6)."""
        self._seq += 1
        tid = f"pre{self._seq:028d}"
        self.pages[tid] = {"title": title, "archived": False, "parent_id": sid}
        self.children.setdefault(sid, []).append(
            {
                "object": "block",
                "id": tid,
                "type": "child_page",
                "child_page": {"title": title},
                "has_children": True,
            }
        )
        self.children[tid] = []
        return tid

    def target_ids_under(self, sid):
        return [b["id"] for b in self.children.get(sid, []) if b.get("type") == "child_page"]

    def body_texts(self, page_id):
        out = []

        def walk(pid):
            for b in self.children.get(pid, []):
                if b.get("type") == "child_page":
                    continue
                typ = b.get("type")
                for r in (b.get(typ) or {}).get("rich_text", []):
                    out.append((r.get("text") or {}).get("content", ""))
                walk(b["id"])

        walk(page_id)
        return out

    # -- helpers --
    @staticmethod
    def _title_of(properties):
        t = (properties or {}).get("title")
        if isinstance(t, dict):
            arr = t.get("title") or []
        elif isinstance(t, list):
            arr = t
        else:
            arr = []
        return "".join((r.get("text") or {}).get("content", "") for r in arr if isinstance(r, dict))

    def _page_json(self, pid):
        p = self.pages[pid]
        return {
            "object": "page",
            "id": pid,
            "in_trash": p["archived"],
            "parent": (
                {"type": "page_id", "page_id": p["parent_id"]}
                if p.get("parent_id")
                else {"type": "workspace", "workspace": True}
            ),
            "last_edited_time": "2026-02-01T00:00:00.000Z",
            "properties": {"Name": {"type": "title", "title": [_run(p["title"])]}},
        }

    def _remove_block(self, bid):
        for pid, lst in self.children.items():  # noqa: B007
            for i, b in enumerate(lst):
                if b.get("id") == bid:
                    lst.pop(i)
                    return

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        seg = [p for p in path.split("/") if p]
        # /v1/users/me
        if method == "GET" and path.endswith("/users/me"):
            return httpx.Response(200, json={"object": "user", "type": "person", "id": "u1"})
        # /v1/pages/{id}
        if method == "GET" and "/pages/" in path:
            pid = seg[-1]
            if pid in self.pages and not self.pages[pid]["archived"]:
                return httpx.Response(200, json=self._page_json(pid))
            return httpx.Response(404, json={"object": "error", "code": "object_not_found"})
        # /v1/blocks/{id}/children
        if "/blocks/" in path and path.endswith("/children"):
            bid = seg[-2]
            if method == "GET":
                # Real Notion does NOT list a child_page whose page is archived (it
                # is in the trash) — filter those so a deleted target isn't re-found.
                results = [
                    b
                    for b in self.children.get(bid, [])
                    if not (
                        b.get("type") == "child_page"
                        and self.pages.get(b.get("id"), {}).get("archived")
                    )
                ]
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "results": results,
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
            if method == "PATCH":
                if bid in self.fail_fill_once_for:
                    self.fail_fill_once_for.discard(bid)
                    return httpx.Response(500, json={"object": "error", "code": "internal"})
                body = json.loads(req.content)
                created = []
                for ch in body.get("children", []):
                    self._seq += 1
                    nbid = f"blk{self._seq:029d}"
                    stored = dict(ch)
                    stored["id"] = nbid
                    stored["object"] = "block"
                    stored.setdefault("has_children", False)
                    self.children.setdefault(bid, []).append(stored)
                    created.append(stored)
                self.appended.append((bid, len(created)))
                return httpx.Response(200, json={"object": "list", "results": created})
        # /v1/blocks/{id}  (DELETE = archive; PATCH = update block, unused)
        if "/blocks/" in path and method == "DELETE":
            bid = seg[-1]
            self._remove_block(bid)
            self.deleted.append(bid)
            return httpx.Response(200, json={"object": "block", "id": bid, "archived": True})
        # /v1/pages  (POST create; PATCH update title)
        if method == "POST" and path.endswith("/pages"):
            body = json.loads(req.content)
            parent = (body.get("parent") or {}).get("page_id")
            title = self._title_of(body.get("properties"))
            self._seq += 1
            tid = f"tgt{self._seq:028d}"
            self.pages[tid] = {"title": title, "archived": False, "parent_id": parent}
            self.children.setdefault(parent, []).append(
                {
                    "object": "block",
                    "id": tid,
                    "type": "child_page",
                    "child_page": {"title": title},
                    "has_children": True,
                }
            )
            self.children[tid] = []
            self.creates.append((parent, title))
            return httpx.Response(200, json={"object": "page", "id": tid})
        if method == "PATCH" and "/pages/" in path:
            pid = seg[-1]
            body = json.loads(req.content)
            title = self._title_of(body.get("properties"))
            if pid in self.pages and title:
                self.pages[pid]["title"] = title
                for lst in self.children.values():
                    for b in lst:
                        if b.get("id") == pid and b.get("type") == "child_page":
                            b["child_page"]["title"] = title
            return httpx.Response(200, json={"object": "page", "id": pid})
        return httpx.Response(404, json={"object": "error", "path": path})


def _basic_body():
    """A small nested body: a paragraph with a bold run + a link run, and a toggle
    with one nested paragraph child."""
    p1 = _block(
        "src-b1",
        "paragraph",
        [
            _run("Hello "),
            _run("world", {"bold": True}),
            _run(" site", link={"url": "https://x.example"}),
        ],
    )
    toggle = _block("src-b2", "toggle", [_run("More")], has_children=True)
    return [p1, toggle]


def _sconn(name, fake, extra=None):
    cfg = {"parent_page_id": fake.parent, "source_locale": "en", "target_locales": "de-DE, fr"}
    cfg.update(extra or {})
    sec = {"token": "ntn_testtoken"}
    return cstore.create("notion", name, cfg, sec, actor="admin")


def _seed_one_source(fake, sid="5a5a5a5a-0000-4000-8000-00000000000a", title="Reset password"):
    body = _basic_body()
    fake.add_source(sid, title, body)
    # the toggle's nested child (fetched via GET /blocks/src-b2/children)
    fake.add_child_block_children(
        "src-b2", [_block("src-b2c1", "paragraph", [_run("Nested step.")])]
    )
    return sid


def main() -> None:
    init_db()

    # ---- (1) walker: nested round-trip preserves annotations + link ----
    body = _basic_body()
    ex = notion_blocks.extract(body)
    # units: "Hello ", "world"(bold), " site"(link), "More"  -> 4
    assert [u.text for u in ex.units] == ["Hello ", "world", " site", "More"], [
        u.text for u in ex.units
    ]
    assert ex.units[1].annotations.get("bold") is True, ex.units[1].annotations
    assert (ex.units[2].link or {}).get("url") == "https://x.example", ex.units[2].link
    trans = {0: "Hallo ", 1: "Welt", 2: " Seite", 3: "Mehr"}
    rebuilt = notion_blocks.reinsert(body, trans)
    r_runs = rebuilt[0]["paragraph"]["rich_text"]
    assert [r["text"]["content"] for r in r_runs] == ["Hallo ", "Welt", " Seite"], r_runs
    assert r_runs[1]["annotations"]["bold"] is True, ("bold preserved", r_runs[1])
    assert r_runs[2]["text"]["link"] == {"url": "https://x.example"}, ("link preserved", r_runs[2])
    assert rebuilt[1]["toggle"]["rich_text"][0]["text"]["content"] == "Mehr"
    assert notion_blocks.structure_ok(body, rebuilt), "structure preserved on round-trip"
    # source untouched (deep clone)
    assert body[0]["paragraph"]["rich_text"][0]["text"]["content"] == "Hello ", "source not mutated"
    print("  walker: nested round-trip preserves annotations + link; source not mutated")

    # ---- (1b) 2000-char leaf splits across runs preserving annotations ----
    big = _block("b", "paragraph", [_run("x", {"italic": True})])
    long_tr = "A" * 4500  # -> 3 runs (2000 + 2000 + 500)
    split = notion_blocks.reinsert([big], {0: long_tr})
    runs = split[0]["paragraph"]["rich_text"]
    assert len(runs) == 3, ("4500 chars -> 3 runs", [len(r["text"]["content"]) for r in runs])
    assert [len(r["text"]["content"]) for r in runs] == [2000, 2000, 500]
    assert all(r["annotations"]["italic"] is True for r in runs), "annotations on every split run"
    assert "".join(r["text"]["content"] for r in runs) == long_tr, "concatenation is lossless"
    assert notion_blocks.structure_ok([big], split), "split does NOT change block count"
    print("  walker: a 2000+ char leaf splits into <=2000-char runs, annotations on each")

    # ---- (2) full flow: one child page per locale, done rows, translated body ----
    f2 = FakeNotion()
    sid = _seed_one_source(f2)
    c2 = _sconn("N basic", f2)
    ok, msg, s2 = cstore.notion_translate(c2.id, actor="auto", transport=f2.transport())
    assert ok, msg
    assert s2["created"] == 2 and s2["updated"] == 0, s2
    assert len(f2.creates) == 2, ("one create per locale", f2.creates)
    tids = f2.target_ids_under(sid)
    assert len(tids) == 2, ("two target child pages", tids)
    # id map has exactly two done rows for this source
    rows = idmap.list_for_connection(c2.id)
    assert len(rows) == 2 and all(r.status == "done" for r in rows), [
        (r.target_locale, r.status) for r in rows
    ]
    assert {r.target_locale for r in rows} == {"de-de", "fr"}, rows
    # target body carries translated (mock '[loc] ...') content, source untouched
    de_tid = next(r.target_id for r in rows if r.target_locale == "de-de")
    de_body = f2.body_texts(de_tid)
    assert any(t.startswith("[de-de]") for t in de_body), ("mock translation written", de_body)
    assert "Nested step." not in de_body, ("nested child was translated too", de_body)
    assert any("Nested step" in t and t.startswith("[de-de]") for t in de_body), de_body
    print("  flow: 2 child pages created; 2 done id-map rows (de-de, fr); nested body translated")

    # ---- (3) exists_target reads the MAP not content ----
    connector = cstore.build_fresh(c2.id, f2.transport())
    assert connector.strategy.exists_target(connector, sid, "de-de") is True, "done row -> exists"
    # a BUILDING row is NOT 'exists' (so it resumes, never suppresses)
    idmap.reserve(c2.id, sid, "es", "tgtXXXX", status="building")
    assert connector.strategy.exists_target(connector, sid, "es") is False, (
        "building row -> not exists"
    )
    # blank/partial target content is irrelevant: exists is the MAP's `done`, not body
    idmap.set_status(c2.id, sid, "es", "done")
    assert connector.strategy.exists_target(connector, sid, "es") is True, (
        "done row -> exists (map, not content)"
    )
    idmap.delete(c2.id, sid, "es")
    print("  strategy: exists_target reads the MAP (done), never content; building != exists")

    # ---- (4) content-hash suppression: unchanged source -> skipped ----
    ok, _m, s4 = cstore.notion_translate(c2.id, actor="auto", transport=f2.transport())
    assert ok and s4["created"] == 0 and s4["updated"] == 0 and s4["skipped"] == 2, (
        "unchanged -> suppressed",
        s4,
    )
    assert len(f2.creates) == 2, ("no new creates on suppressed run", f2.creates)
    print("  flow suppression: unchanged content -> 2 skipped, no new pages")

    # ---- (5) RULE 2: done row + target present -> update in place (PATCH, same id) ----
    f5 = FakeNotion()
    s5id = _seed_one_source(f5)
    c5 = _sconn("N rule2", f5, {"target_locales": "de-DE"})
    cstore.notion_translate(c5.id, actor="auto", transport=f5.transport())
    tid_before = idmap.get(c5.id, s5id, "de-de").target_id
    # edit the source so the hash changes -> re-translate the SAME target
    f5.children[s5id][1]["paragraph"] = (
        {"rich_text": [_run("Reset now.")], "color": "default"}
        if False
        else f5.children[s5id][1].get("paragraph")
    )
    f5.children[s5id].append(_block("src-new", "paragraph", [_run("Brand new line.")]))
    creates_before = len(f5.creates)
    ok, msg, s5b = cstore.notion_translate(c5.id, actor="auto", transport=f5.transport())
    assert ok, msg
    assert s5b["updated"] == 1 and s5b["created"] == 0, ("update in place", s5b)
    assert len(f5.creates) == creates_before, ("no new page created on update", f5.creates)
    assert idmap.get(c5.id, s5id, "de-de").target_id == tid_before, "same target id kept"
    assert any("Brand new line" in t for t in f5.body_texts(tid_before)), (
        "new content written to same page"
    )
    print("  RULE 2: done+present -> PATCH in place (same id, no new page)")

    # ---- (6) RULE 3: done row + target GONE -> orphaned + recreate (new id) ----
    f6 = FakeNotion()
    s6id = _seed_one_source(f6)
    c6 = _sconn("N rule3", f6, {"target_locales": "de-DE"})
    cstore.notion_translate(c6.id, actor="auto", transport=f6.transport())
    old_tid = idmap.get(c6.id, s6id, "de-de").target_id
    f6.pages[old_tid]["archived"] = True  # a human deleted our page
    # change source so it isn't suppressed
    f6.children[s6id].append(_block("src-x", "paragraph", [_run("Another line.")]))
    ok, msg, _s6b = cstore.notion_translate(c6.id, actor="auto", transport=f6.transport())
    assert ok, msg
    new_tid = idmap.get(c6.id, s6id, "de-de").target_id
    assert new_tid and new_tid != old_tid, (
        "recreated with a NEW id, dead id not resurrected",
        old_tid,
        new_tid,
    )
    assert idmap.get(c6.id, s6id, "de-de").status == "done", "recreated row is done"
    # exactly one live row for this (source, locale) (the orphaned dead id is not a 2nd row)
    r6 = [r for r in idmap.list_for_connection(c6.id) if r.source_id == s6id]
    assert len(r6) == 1, ("one row per (source, locale) — recreate reused it", r6)
    print("  RULE 3: done+GONE -> orphan the dead id + recreate with a new id (never resurrect)")

    # ---- (7) RULE 4 / resumable build: crash after reserve -> retry PATCHes, no double-create ----
    f7 = FakeNotion()
    s7id = _seed_one_source(f7)
    c7 = _sconn("N resume", f7, {"target_locales": "de-DE"})
    # make the FIRST fill (append) fail AFTER the target is created + reserved
    # We can't know the target id up front, so fail the FIRST append call regardless:
    orig_handle = f7._handle
    state = {"failed": False}

    def _handle_fail_first(req):
        if req.method == "PATCH" and req.url.path.endswith("/children") and not state["failed"]:
            state["failed"] = True
            return httpx.Response(500, json={"object": "error", "code": "internal"})
        return orig_handle(req)

    tr_fail = httpx.MockTransport(_handle_fail_first)
    ok, msg, s7a = cstore.notion_translate(c7.id, actor="auto", transport=tr_fail)
    assert not ok, ("the fill failure FAILS the run", msg, s7a)
    row7 = idmap.get(c7.id, s7id, "de-de")
    assert row7 is not None and row7.status == "building", (
        "reserved as building after crash",
        row7,
    )
    reserved_tid = row7.target_id
    creates_after_crash = len(f7.creates)
    assert creates_after_crash == 1, ("exactly one page created (the reserve)", f7.creates)
    # retry: must PATCH the SAME reserved target, never POST a second page
    ok, msg, _s7b = cstore.notion_translate(c7.id, actor="auto", transport=f7.transport())
    assert ok, msg
    assert len(f7.creates) == 1, ("retry did NOT create a second page", f7.creates)
    row7b = idmap.get(c7.id, s7id, "de-de")
    assert row7b.status == "done" and row7b.target_id == reserved_tid, (
        "resumed the SAME target to done",
        row7b.status,
        row7b.target_id,
        reserved_tid,
    )
    all7 = [r for r in idmap.list_for_connection(c7.id) if r.source_id == s7id]
    assert len(all7) == 1, ("exactly ONE ConnectorIdMap row for the pair", all7)
    print(
        "  RULE 4 / resumable: crash after reserve -> retry PATCHes the SAME target "
        "(one row, one id, no double-create)"
    )

    # ---- (8) RULE 5: building row + target absent -> drop stale row + recreate ----
    f8 = FakeNotion()
    s8id = _seed_one_source(f8)
    c8 = _sconn("N rule5", f8, {"target_locales": "de-DE"})
    # a building row whose target never existed (or was deleted mid-build)
    idmap.reserve(c8.id, s8id, "de-de", "ghosttarget00000000000000000000", status="building")
    ok, msg, _s8b = cstore.notion_translate(c8.id, actor="auto", transport=f8.transport())
    assert ok, msg
    row8 = idmap.get(c8.id, s8id, "de-de")
    assert row8.status == "done" and row8.target_id != "ghosttarget00000000000000000000", (
        "stale building row dropped + recreated",
        row8.status,
        row8.target_id,
    )
    assert len(f8.creates) == 1, ("recreated exactly once", f8.creates)
    print("  RULE 5: building+absent -> drop the stale row + recreate (never PATCH a 404 id)")

    # ---- (9) RULE 6 (LOAD-BEARING): pre-existing target unknown to the map -> needs_link ----
    f9 = FakeNotion()
    s9id = _seed_one_source(f9)
    c9 = _sconn("N rule6", f9, {"target_locales": "de-DE"})
    # a human has ALREADY hand-written a translation child page titled like ours.
    conn9 = cstore.build_fresh(c9.id, f9.transport())
    want_title = conn9._target_title(
        "de-de", "[de-de] Reset password"
    )  # matches build_payload's title
    pre_tid = f9.seed_preexisting_target(s9id, want_title)
    pre_body_before = list(f9.children[pre_tid])
    ok, msg, s9b = cstore.notion_translate(c9.id, actor="auto", transport=f9.transport())
    assert not ok, ("a needs_link collision FAILS the run (loud)", msg, s9b)
    row9 = idmap.get(c9.id, s9id, "de-de")
    assert row9 is not None and row9.status == "needs_link", ("flagged needs_link", row9)
    assert row9.target_id == pre_tid, ("points at the pre-existing target", row9.target_id, pre_tid)
    assert f9.children[pre_tid] == pre_body_before, (
        "the human's page was NOT overwritten",
        f9.children[pre_tid],
    )
    assert not any(pid == s9id and title == want_title for (pid, title) in f9.creates), (
        "NO new page created for the collided pair",
        f9.creates,
    )
    # a SECOND run keeps failing closed (never adopts silently)
    ok2, _m, _s = cstore.notion_translate(c9.id, actor="auto", transport=f9.transport())
    assert not ok2 and idmap.get(c9.id, s9id, "de-de").status == "needs_link", (
        "re-run stays fail-closed until an operator resolves it"
    )
    assert f9.children[pre_tid] == pre_body_before, "still not overwritten on re-run"
    print("  RULE 6: pre-existing human translation -> needs_link, FAIL CLOSED, never overwritten")

    # ---- (9b) resolve needs_link: ADOPT -> next run updates in place; IGNORE -> drops row ----
    okr, msgr, _sr = cstore.notion_resolve_link(c9.id, s9id, "de-de", "adopt", actor="admin")
    assert okr, msgr
    assert idmap.get(c9.id, s9id, "de-de").status == "done", "adopt -> done"
    # a NEW run now UPDATES the adopted (pre-existing) target in place, same id
    f9.children[s9id].append(_block("src-chg", "paragraph", [_run("Post-adopt change.")]))
    ok3, msg3, s9c = cstore.notion_translate(c9.id, actor="auto", transport=f9.transport())
    assert ok3, msg3
    assert s9c["updated"] == 1 and s9c["created"] == 0, ("adopted target updated in place", s9c)
    assert idmap.get(c9.id, s9id, "de-de").target_id == pre_tid, "adopted the human's id"
    assert any("Post-adopt change" in t for t in f9.body_texts(pre_tid)), (
        "content now written to it"
    )
    # ignore path (fresh collision)
    f9b = FakeNotion()
    s9bid = _seed_one_source(f9b)
    c9b = _sconn("N rule6-ignore", f9b, {"target_locales": "de-DE"})
    conn9b = cstore.build_fresh(c9b.id, f9b.transport())
    f9b.seed_preexisting_target(s9bid, conn9b._target_title("de-de", "[de-de] Reset password"))
    cstore.notion_translate(c9b.id, actor="auto", transport=f9b.transport())
    oki, _m, _s = cstore.notion_resolve_link(c9b.id, s9bid, "de-de", "ignore", actor="admin")
    assert oki and idmap.get(c9b.id, s9bid, "de-de") is None, "ignore -> row dropped"
    print("  RULE 6 resolve: adopt -> update-in-place same id; ignore -> row dropped")

    # ---- (10) RULE 7: a locale removed from scope leaves its row + target untouched ----
    f10 = FakeNotion()
    s10id = _seed_one_source(f10)
    c10 = _sconn("N rule7", f10, {"target_locales": "de-DE, fr"})
    cstore.notion_translate(c10.id, actor="auto", transport=f10.transport())
    fr_row = idmap.get(c10.id, s10id, "fr")
    fr_tid = fr_row.target_id
    assert fr_row.status == "done"
    # narrow scope to de-DE only, re-run (source changed so de is revisited)
    cstore.patch_config(c10.id, {"target_locales": "de-DE"})
    f10.children[s10id].append(_block("src-nn", "paragraph", [_run("Scope change.")]))
    ok, msg, _s10b = cstore.notion_translate(c10.id, actor="auto", transport=f10.transport())
    assert ok, msg
    fr_after = idmap.get(c10.id, s10id, "fr")
    assert fr_after is not None and fr_after.status == "done" and fr_after.target_id == fr_tid, (
        "the out-of-scope fr row + target are untouched",
        fr_after,
    )
    assert not f10.pages[fr_tid]["archived"], "the fr target page is not deleted"
    assert "fr" not in {
        r.target_locale
        for r in idmap.list_for_connection(c10.id)
        if r.source_id == s10id and r.updated_at > fr_after.updated_at
    }, "fr not re-touched"
    print("  RULE 7: a locale narrowed out of scope keeps its row + target (no content deletion)")

    # ---- (11) no watermark key ever written (poll-first) ----
    cfg11 = cstore.get(c2.id).config or {}
    assert "notion_hashes" in cfg11, ("content-hash map persisted", list(cfg11))
    assert not any("watermark" in k for k in cfg11), (
        "poll-first flow must NOT introduce a watermark",
        list(cfg11),
    )
    print("  regression: notion_hashes persisted, NO watermark key (poll-first)")

    # ---- (12) NEEDS_REVIEW body unit excluded from write-back (source kept, no hedge) ----
    import app.services.translate as translate_mod

    real_ptd = translate_mod.pretranslate_document

    def _ptd_flip(session, doc_id, locale, spec=None, force_pii=False):
        stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == "de-de":
            seg = session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id,
                    Segment.target_locale == "de-de",
                    Segment.source_text == "world",
                )
            ).first()
            if seg:
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
                session.commit()
        return stats

    f12 = FakeNotion()
    s12id = _seed_one_source(f12)
    c12 = _sconn("N needs-review", f12, {"target_locales": "de-DE"})
    set_pieces("app.services.translate", "pretranslate_document", _ptd_flip)
    try:
        ok, msg, _s12 = cstore.notion_translate(c12.id, actor="auto", transport=f12.transport())
        assert ok, msg
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    # #640: a pair with a held unit is NOT created (the held spot would carry SOURCE text: a
    # mixed-language target); nothing reaches the wire and the next run retries it.
    assert idmap.get(c12.id, s12id, "de-de") is None, "held pair: no target created, no id-map row"
    print("  defense-in-depth: a NEEDS_REVIEW run holds the pair (no target created, no hedge)")

    # ---- (13) retry_failed re-runs ONLY the failed (page, locale) pairs ----
    f13 = FakeNotion()
    a = _seed_one_source(f13, "aaaaaaaa-0000-4000-8000-00000000000a", "Alpha")
    b = _seed_one_source(f13, "bbbbbbbb-0000-4000-8000-00000000000b", "Beta")
    c13 = _sconn("N retry", f13, {"target_locales": "de-DE, fr"})
    failures = [
        {"article_id": a, "locale": "de-de", "error": "500"},
        {"article_id": b, "locale": "fr", "error": "500"},
    ]
    okr, _m, _sr = cstore.retry_failed(c13.id, failures, actor="auto", transport=f13.transport())
    assert okr, _sr
    made = {(r.source_id, r.target_locale) for r in idmap.list_for_connection(c13.id)}
    assert made == {(a, "de-de"), (b, "fr")}, ("only the 2 failed pairs built", made)
    print("  retry: re-ran ONLY the 2 failed (page, locale) pairs, not the cross product")

    # ---- (14) GDPR / connection delete sweeps the id-map rows ----
    with Session(engine) as s:
        before = s.exec(select(ConnectorIdMap).where(ConnectorIdMap.connection_id == c13.id)).all()
    assert before, "precondition: id-map rows exist for the connection"
    cstore.delete(c13.id)
    with Session(engine) as s:
        after = s.exec(select(ConnectorIdMap).where(ConnectorIdMap.connection_id == c13.id)).all()
    assert not after, ("connection delete swept the ConnectorIdMap rows (GDPR + FK)", len(after))
    print("  GDPR: deleting a Notion connection sweeps its ConnectorIdMap rows")

    # ---- (15) org isolation: foreign/missing cid -> 404 on the /api routes ----
    c = TestClient(app)
    from tests._api_fixture import user_api_token

    fapi = FakeNotion()
    _seed_one_source(fapi)
    capi = _sconn("N api", fapi)
    hdr = {"X-Loc-Token": user_api_token()}
    assert (
        c.post("/api/connectors/99999999/notion/translate", json={}, headers=hdr).status_code == 404
    )
    assert (
        c.post("/api/connectors/99999999/notion/resolve-link", json={}, headers=hdr).status_code
        == 404
    )
    r_own = c.post(
        f"/api/connectors/{capi.id}/notion/translate", json={"article_ids": []}, headers=hdr
    )
    assert r_own.status_code in (200, 400), (r_own.status_code, r_own.text)
    print("  org isolation: foreign/missing cid -> 404 on both /api notion routes")

    # ---- (16) web routes: tile + type page + endpoint + id-map panel ----
    assert "Notion" in c.get("/connectors").text, "Notion tile missing from /connectors"
    typ = c.get("/connectors/notion").text
    # Notion is rebuilt and un-archived -> the add form is offered again.
    assert "New connections are closed" not in typ and "Create connection" in typ, (
        "the rebuilt Notion type page must offer the add form"
    )
    _fnames = {f.name for f in _reg.spec("notion").config_fields} | {
        f.name for f in _reg.spec("notion").secret_fields
    }
    assert "parent_page_id" in _fnames, ("Notion spec must still declare 'parent_page_id'", _fnames)
    page = c.get(f"/connectors/connection/{capi.id}").text
    assert "/notion/translate" in page, "inbound-API endpoint must render"
    assert "id map" in page.lower(), "the id-map status panel must render"
    print("  routes: Notion tile + type page; inbound endpoint + id-map panel present")

    # ---- (17) CLI notion-translate + notion-resolve-link smoke ----
    from app.cli.cmd_connector import (
        _notion_resolve_link,
        _notion_translate,
        _parse_list,  # noqa: F401
    )

    fcli = FakeNotion()
    scli = _seed_one_source(fcli)
    conncli = _sconn("N cli", fcli, {"target_locales": "de-DE"})

    class _Args:
        connection_id = conncli.id
        article_ids = ""
        locales = ""
        actor = "cli"
        local = True
        org = None
        json = False
        source_id = scli
        target_locale = "de-de"
        action = "ignore"

    class _Emit:
        def __init__(self):
            self.res = None

        def result(self, msg, data):
            self.res = (msg, data)

    real_nt = cstore.notion_translate
    seen = {}

    def _cap_nt(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        seen["called"] = True
        return real_nt(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=fcli.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "notion_translate", _cap_nt)
    try:
        emit = _Emit()
        assert _notion_translate(_Args(), emit) == 0, emit.res
        assert seen.get("called") and emit.res[1]["ok"], emit.res
    finally:
        set_pieces("app.services.connectors.store", "notion_translate", real_nt)
    # resolve-link CLI (ignore a non-existent row -> non-zero, but parses + runs)
    emit2 = _Emit()
    rc = _notion_resolve_link(_Args(), emit2)
    assert rc in (0, 1) and emit2.res is not None, emit2.res
    print("  CLI: notion-translate + notion-resolve-link parse args and run")

    # ---- (18) FINDING 3: code-block CAPTION translates, code TEXT stays verbatim ----
    code_blk = _code_block("cb1", [_run("print('hi')")], [_run("Sample code")])
    ex18 = notion_blocks.extract([code_blk])
    assert [u.text for u in ex18.units] == ["Sample code"], (
        "only the caption is a unit, the code text is skipped",
        [u.text for u in ex18.units],
    )
    rebuilt18 = notion_blocks.reinsert([code_blk], {0: "Beispielcode"})
    assert rebuilt18[0]["code"]["caption"][0]["text"]["content"] == "Beispielcode", (
        "caption translated",
        rebuilt18[0]["code"]["caption"],
    )
    assert rebuilt18[0]["code"]["rich_text"][0]["text"]["content"] == "print('hi')", (
        "code text untouched",
        rebuilt18[0]["code"]["rich_text"],
    )
    assert notion_blocks.structure_ok([code_blk], rebuilt18), "structure preserved"
    print(
        "  FINDING 3: code-block caption is extracted + reinserted translated; code text untouched"
    )

    # ---- (19) FINDING 1: broadened rule-6 probe reclaims a title-drifted orphan; ----
    # ----      a DIFFERENT locale's tag is NOT a false collision                  ----
    f19 = FakeNotion()
    s19id = _seed_one_source(f19)
    c19 = _sconn("N rule1-drift", f19, {"target_locales": "de-DE"})
    # Orphan: a child page bearing OUR locale tag but a title SUFFIX that differs
    # from whatever this run computes (simulates a crash between POST /pages and
    # idmap.reserve() on a PRIOR run whose title drifted: a NEEDS_REVIEW hedge that
    # didn't fire this time, a source-title edit, non-deterministic MT).
    orphan_tid = f19.seed_preexisting_target(s19id, "[de-de] Some Other Title Entirely")
    ok19, msg19, s19 = cstore.notion_translate(c19.id, actor="auto", transport=f19.transport())
    assert not ok19, ("broadened rule-6 collision fails the run", msg19, s19)
    row19 = idmap.get(c19.id, s19id, "de-de")
    assert row19 is not None and row19.status == "needs_link", ("flagged needs_link", row19)
    assert row19.target_id == orphan_tid, (
        "linked to the drifted orphan",
        row19.target_id,
        orphan_tid,
    )
    tids19 = f19.target_ids_under(s19id)
    assert tids19 == [orphan_tid], ("no duplicate/second child page created", tids19)
    assert not f19.creates, ("no POST /pages for the collided pair", f19.creates)
    print(
        "  FINDING 1: a title-drifted orphan sharing our locale tag is reclaimed "
        "(needs_link, no duplicate page, nothing overwritten)"
    )

    f19b = FakeNotion()
    s19bid = _seed_one_source(f19b)
    c19b = _sconn("N rule1-diff-locale", f19b, {"target_locales": "de-DE"})
    # A page tagged for a DIFFERENT locale (fr) must NOT collide with a de-DE target.
    f19b.seed_preexisting_target(s19bid, "[fr] Something Unrelated")
    ok19b, msg19b, _s19b = cstore.notion_translate(
        c19b.id, actor="auto", transport=f19b.transport()
    )
    assert ok19b, msg19b
    row19b = idmap.get(c19b.id, s19bid, "de-de")
    assert row19b is not None and row19b.status == "done", (
        "a different locale's tag is NOT a collision -> normal create",
        row19b,
    )
    tids19b = f19b.target_ids_under(s19bid)
    assert len(tids19b) == 2, ("the pre-existing fr page + the new de-de page", tids19b)
    print(
        "  FINDING 1: a DIFFERENT locale's tag ('[fr]' vs a de-DE target) is NOT "
        "a false collision -> creates normally"
    )

    # ---- (20) FINDING 2: a fully-refused translation does not create/fill an ----
    # ----      untranslated English clone under a "[loc]" label                ----
    def _ptd_all_refused(session, doc_id, locale, spec=None, force_pii=False):
        stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == "de-de":
            segs = session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "de-de"
                )
            ).all()
            for seg in segs:
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
            session.commit()
        return stats

    # -- CREATE path: brand-new page, entire translation collapses --
    f20 = FakeNotion()
    s20id = _seed_one_source(f20)
    c20 = _sconn("N refused-create", f20, {"target_locales": "de-DE"})
    set_pieces("app.services.translate", "pretranslate_document", _ptd_all_refused)
    try:
        ok20, msg20, s20 = cstore.notion_translate(c20.id, actor="auto", transport=f20.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert not ok20, ("a fully-refused pair fails the run", msg20, s20)
    assert not f20.creates, ("NO POST /pages fired for a fully-refused page", f20.creates)
    assert len(s20["failures"]) == 1, s20["failures"]
    fail20 = s20["failures"][0]
    assert fail20["article_id"] == s20id and fail20["locale"] == "de-de", fail20
    assert "refused" in fail20["error"].lower() or "flagged" in fail20["error"].lower(), fail20
    assert "refused" in msg20.lower() or "flagged" in msg20.lower(), msg20
    assert idmap.get(c20.id, s20id, "de-de") is None, "no id-map row for a refused pair"
    cfg20 = cstore.get(c20.id).config or {}
    assert f"{s20id}:de-de" not in (cfg20.get("notion_hashes") or {}), (
        "hash NOT recorded for a refused pair (so a retry re-attempts it)",
        cfg20.get("notion_hashes"),
    )
    print(
        "  FINDING 2 (create): a fully-refused page is NOT created; recorded as a "
        "per-pair failure; no hash recorded"
    )

    # -- UPDATE path: existing DONE page, retranslation collapses to zero --
    f21 = FakeNotion()
    s21id = _seed_one_source(f21)
    c21 = _sconn("N refused-update", f21, {"target_locales": "de-DE"})
    ok21a, msg21a, _s21a = cstore.notion_translate(c21.id, actor="auto", transport=f21.transport())
    assert ok21a, msg21a
    row21_before = idmap.get(c21.id, s21id, "de-de")
    tid21 = row21_before.target_id
    body21_before = list(f21.body_texts(tid21))
    title21_before = f21.pages[tid21]["title"]
    appended_before = len(f21.appended)
    deleted_before = len(f21.deleted)
    hash21_before = (cstore.get(c21.id).config or {}).get("notion_hashes", {}).get(f"{s21id}:de-de")
    # change the source so it isn't hash-suppressed, then collapse the retranslation
    f21.children[s21id].append(_block("src-refused", "paragraph", [_run("New refused line.")]))
    set_pieces("app.services.translate", "pretranslate_document", _ptd_all_refused)
    try:
        ok21b, msg21b, s21b = cstore.notion_translate(
            c21.id, actor="auto", transport=f21.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert not ok21b, (msg21b, s21b)
    assert len(s21b["failures"]) == 1, s21b["failures"]
    assert (
        "refused" in s21b["failures"][0]["error"].lower()
        or "flagged" in s21b["failures"][0]["error"].lower()
    ), s21b["failures"]
    row21_after = idmap.get(c21.id, s21id, "de-de")
    assert row21_after.status == "done" and row21_after.target_id == tid21, (
        "existing done row untouched",
        row21_after,
    )
    assert f21.body_texts(tid21) == body21_before, (
        "existing page body untouched on a refused update",
        f21.body_texts(tid21),
    )
    assert f21.pages[tid21]["title"] == title21_before, "existing page title untouched"
    assert len(f21.appended) == appended_before, ("no append/fill call fired", f21.appended)
    assert len(f21.deleted) == deleted_before, ("no clear/delete call fired", f21.deleted)
    hash21_after = (cstore.get(c21.id).config or {}).get("notion_hashes", {}).get(f"{s21id}:de-de")
    assert hash21_after == hash21_before, (
        "hash NOT advanced on a refused update",
        hash21_before,
        hash21_after,
    )
    print(
        "  FINDING 2 (update): a fully-refused retranslation leaves the existing "
        "page's blocks/title untouched; recorded as a per-pair failure; hash not advanced"
    )

    print("verify_notion OK")


if __name__ == "__main__":
    main()
