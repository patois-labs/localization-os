"""Notion connector verification (rebuild: 2026-03-11 API, T3 child pages, #640).
Fully offline: a stateful fake Notion workspace (tests/_notion_fake.py) behind
httpx.MockTransport, plus a capturing provider.

Groups (the number printed with each line):
  1   API contract: every request carries Notion-Version 2026-03-11 + Bearer, a page create
      sends only a page parent and a title, nothing outside the root is touched
  2   block fidelity: types, annotations, links, mentions, equations, code, tables, columns;
      response-only fields never sent; unsupported / hosted blocks reported; synced duplicate
  3   idempotent re-run, update in place, in_trash target orphaned and recreated
  4   #640 held-back: never a created page, never source text on an update, no hash
  5   id map: a human page with the target title is never overwritten (needs_link)
  6   scope / path escape (taxonomy A): bad ids never reach the wire, foreign pages answer
      like missing ones, depth, our own targets are never sources, title pattern is inert
  7   database root: data-source query with cursors, rows as sources, child-page targets
  8   failures: 429 backoff, 5xx never resent, a 503 that committed is adopted, a refused
      append rolls back and the retry resumes, token never in output
  9   paging (250 blocks) and the write budget
  10  surfaces: SPEC un-archived, T3 capability, ids, test() messages
  11  hand-edit guard, request limits, reported degradations
  12  live-page safety: a kill or a 500 at every write of a body swap and of a first write
      (never empty, never partial-only, converges); the fingerprint sees formatting-only
      edits but not a renamed mention; an edited translation is parked (no MT loop)
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_notionh_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import Connection, Segment, SegStatus, SyncLog
from app.services import connectors as reg
from app.services import translate as tr_mod
from app.services.connectors import base as cbase
from app.services.connectors import idmap
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.notion import (
    NOTION_VERSION,
    NotionConnector,
    _writeable_block,
    canon_id,
)
from tests._notion_fake import (
    TOKEN,
    FakeNotion,
    Killed,
    block,
    equation,
    mention_page,
    mention_user,
    run,
    uid,
)


def _hold(pred):
    orig = tr_mod.pretranslate_document

    def patched(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if pred(sg):
                sg.status = SegStatus.NEEDS_REVIEW
                s.add(sg)
        s.commit()
        return out

    tr_mod.pretranslate_document = patched
    return lambda: setattr(tr_mod, "pretranslate_document", orig)


def _mk(name, root, extra=None, token=TOKEN):
    cfg = {"parent_page_id": root, "source_locale": "en", "target_locales": "de-DE"}
    cfg.update(extra or {})
    return cstore.create("notion", name, cfg, {"token": token}, actor="admin")


def _tr(c, f, **kw):
    return cstore.notion_translate(c.id, actor="auto", transport=f.transport(), **kw)


def _cfg(cid):
    with Session(engine) as s:
        return dict(s.get(Connection, cid).config or {})


def _hashes(cid):
    return dict(_cfg(cid).get("notion_hashes") or {})


def _unseal(cid):
    """Forget the stored target fingerprints (the test then plays a hand edit it wants kept)."""
    with Session(engine) as s:
        conn = s.get(Connection, cid)
        cfg = dict(conn.config or {})
        cfg["notion_target_prints"] = {}
        conn.config = cfg
        s.add(conn)
        s.commit()


def _synclogs(cid):
    with Session(engine) as s:
        return s.exec(select(SyncLog).where(SyncLog.connection_id == cid)).all()


def _conn_obj(f, **cfg):
    base = {"parent_page_id": f.root, "source_locale": "en", "target_locales": "de-DE"}
    base.update(cfg)
    return NotionConnector(base, {"token": TOKEN}, transport=f.transport())


def _raises(fn, needle):
    try:
        fn()
    except ConnectorError as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return
    raise AssertionError(f"expected ConnectorError containing {needle!r}")


def T(title="Reset password", loc="de-de"):
    """Target title under the offline mock MT: the pattern tag plus the mock-translated title."""
    return f"[de-de] [de-de] {title}"


def _simple_source(f, title="Reset password", parent="__root__", text="Click the link."):
    return f.add_page(title, parent=parent, blocks=[block("paragraph", [run(text)])])


def main() -> None:
    init_db()
    cbase._sleep = lambda s: None  # backoff runs instantly

    # ===================================================================== 1
    f = FakeNotion()
    src = _simple_source(f)
    deep = f.add_page("Nested under source", parent=src, blocks=[block("paragraph", [run("deep")])])
    outside = f.add_page(
        "Outside the root", parent=None, blocks=[block("paragraph", [run("secret")])]
    )
    c = _mk("N main", f.root, {"target_locales": "de-DE, fr"})
    ok, msg, r = _tr(c, f)
    assert ok and r["created"] == 2 and r["updated"] == 0, (msg, r)
    kids = f.children_pages(src)
    assert deep in kids and len(kids) == 3, "two target children next to the existing sub-page"
    assert all(e["version"] == NOTION_VERSION for e in f.log), {e["version"] for e in f.log}
    assert all(e["auth"] == f"Bearer {TOKEN}" for e in f.log)
    posts = [e for e in f.log if e["method"] == "POST" and e["path"].endswith("/pages")]
    assert len(posts) == 2 and all(
        set(e["body"]) == {"parent", "properties"}
        and set(e["body"]["parent"]) == {"page_id"}
        and e["body"]["parent"]["page_id"] == src
        and set(e["body"]["properties"]) == {"title"}
        for e in posts
    ), posts
    assert not any(
        e["path"].endswith(("/pages/" + outside, "/blocks/" + outside + "/children")) for e in f.log
    ), "a page outside the root is never read"
    assert f.pages[outside]["title"] == "Outside the root" and not f.children_pages(outside)
    de = f.by_title(T())
    assert len(de) == 1 and f.texts(de[0]) == ["[de-de] Click the link."], f.texts(de[0])
    rows = idmap.list_for_connection(c.id)
    assert len(rows) == 2 and all(x.status == idmap.DONE for x in rows)
    assert f"{src}:de-de" in _hashes(c.id) and f"{deep}:de-de" not in _hashes(c.id), _hashes(c.id)
    print("  1. create: child pages under the source, 2026-03-11 header, page parent only")

    # ===================================================================== 2
    f2 = FakeNotion()
    link = "https://example.com/doc"
    pg_target = f2.add_page("Linked page", parent=None)
    user_id = uid(7)
    body = [
        block("heading_2", [run("Setup", {"bold": True})], is_toggleable=False),
        block(
            "paragraph",
            [
                run("Read "),
                run("this guide", {"bold": True, "italic": True, "color": "red"}, link),
                run(" and see "),
                mention_page(pg_target, "Linked page"),
                run(" by "),
                mention_user(user_id, "Ann"),
                run(" with "),
                equation("E=mc^2"),
                run(" now."),
            ],
        ),
        block("code", [run("print('keep me')")], language="python", caption=[run("Run it")]),
        block("bulleted_list_item", [run("Parent bullet")]),
        block(
            "callout",
            [run("Careful here")],
            icon={"type": "emoji", "emoji": "!"},
        ),
        {
            "type": "table",
            "table": {
                "table_width": 2,
                "has_column_header": True,
                "has_row_header": False,
                "children": [
                    {"type": "table_row", "table_row": {"cells": [[run("Name")], [run("Value")]]}},
                    {"type": "table_row", "table_row": {"cells": [[run("A")], [run("1")]]}},
                ],
            },
        },
        {
            "type": "column_list",
            "column_list": {
                "children": [
                    {
                        "type": "column",
                        "column": {"children": [block("paragraph", [run("Left col")])]},
                    },
                    {
                        "type": "column",
                        "column": {"children": [block("paragraph", [run("Right col")])]},
                    },
                ]
            },
        },
        block("image", [], type="file", file={"url": "https://s3.signed/x.png"}, caption=[]),
        block(
            "image",
            [],
            type="external",
            external={"url": "https://img.example/a.png"},
            caption=[run("Alt text")],
        ),
        {"type": "link_preview", "link_preview": {"url": "https://example.com/preview"}},
        {"type": "unsupported", "unsupported": {"block_type": "ai_block"}},
        block("divider"),
    ]
    s2 = f2.add_page("Guide", parent=f2.root, blocks=body)
    nested = f2.blocks[[i for k, i in f2.kids[s2] if k == "block"][3]]["id"]
    f2.put_block(nested, block("paragraph", [run("Child of bullet")]))
    f2.blocks[nested]["has_children"] = True
    c2 = _mk("N fidelity", f2.root)
    ok, msg, r = _tr(c2, f2)
    assert ok and r["created"] == 1, (msg, r)
    tgt = f2.by_title(T("Guide"))[0]
    tree = f2.body(tgt)
    types = [t for t, _x, _k in tree]
    assert types == [
        "heading_2", "paragraph", "code", "bulleted_list_item", "callout", "table",
        "column_list", "image", "bookmark", "divider",
    ], types  # fmt: skip
    para = [i for k, i in f2.kids[tgt] if k == "block"][1]
    runs = f2.blocks[para]["paragraph"]["rich_text"]
    assert runs[1]["text"]["link"] == {"url": link}, runs[1]
    assert runs[1]["annotations"]["bold"] and runs[1]["annotations"]["italic"], runs[1]
    assert runs[1]["annotations"]["color"] == "red"
    assert runs[1]["text"]["content"] == "[de-de] this guide"
    assert runs[3]["mention"]["page"] == {"id": pg_target}, runs[3]
    assert runs[5]["mention"]["user"] == {"id": user_id}, runs[5]
    assert runs[7]["equation"] == {"expression": "E=mc^2"}, runs[7]
    assert next(x["text"]["content"] for x in runs if x["type"] == "text") == "[de-de] Read "
    code = [i for k, i in f2.kids[tgt] if k == "block"][2]
    assert f2.blocks[code]["code"]["rich_text"][0]["text"]["content"] == "print('keep me')"
    assert f2.blocks[code]["code"]["caption"][0]["text"]["content"] == "[de-de] Run it"
    assert f2.blocks[code]["code"]["language"] == "python"
    nested_t = [i for k, i in f2.kids[tgt] if k == "block"][3]
    assert f2.texts(tgt).count("[de-de] Child of bullet") == 1 and nested_t
    table = f2.body(tgt)[5]
    assert [x[0] for x in table[2]] == ["table_row", "table_row"], table
    cols = f2.body(tgt)[6][2]
    assert [col[2][0][1] for col in cols] == [["[de-de] Left col"], ["[de-de] Right col"]], cols
    skipped = []
    for b in body:
        _writeable_block(b, skipped=skipped)
    assert skipped == ["image", "unsupported"], ("hosted image + unsupported are reported", skipped)
    ext_img = [i for k, i in f2.kids[tgt] if k == "block"][7]
    assert f2.blocks[ext_img]["image"]["external"] == {"url": "https://img.example/a.png"}
    bm = [i for k, i in f2.kids[tgt] if k == "block"][8]
    assert f2.blocks[bm]["bookmark"] == {"url": "https://example.com/preview"}, f2.blocks[bm]
    # the fake refuses response-only fields: the create above would have failed otherwise
    assert not any(e["method"] == "PATCH" and "plain_text" in str(e["body"]) for e in f2.log)
    # the source page is untouched
    assert [t for t, _x, _k in f2.body(s2)][:2] == ["heading_2", "paragraph"]
    assert f2.texts(s2)[0] == "Setup", "source body unchanged"
    conn2 = _conn_obj(f2)
    res = conn2.fill_target(
        tgt,
        {
            "title": "t",
            "locale": "de",
            "blocks": [
                block(
                    "synced_block",
                    [],
                    synced_from={"block_id": uid(9)},
                    children=[block("paragraph", [run("Synced")])],
                ),
                block("meeting_notes", [run("x")]),
            ],
        },
    )
    assert res.ok and "meeting_notes" in res.message, res
    sync = [
        i
        for k, i in f2.kids[tgt]
        if k == "block" and f2.blocks[i]["type"] == "synced_block" and not f2.blocks[i]["in_trash"]
    ]
    assert len(sync) == 1 and f2.blocks[sync[0]]["synced_block"]["synced_from"] is None
    print(
        "  2. block fidelity: types, annotations, links, mentions, equation, code, table, columns"
    )

    # ===================================================================== 3
    n_pages, n_writes = len(f.pages), len(f.writes())
    ok, msg, r = _tr(c, f)
    assert ok and r["skipped"] == 2 * 1 and r["created"] == 0 and r["updated"] == 0, (msg, r)
    assert len(f.pages) == n_pages and len(f.writes()) == n_writes, "re-run must not write"
    f.blocks[next(i for k, i in f.kids[src] if k == "block")]["paragraph"]["rich_text"] = [
        run("Click the NEW link.")
    ]
    ok, msg, r = _tr(c, f, article_ids=[src])
    assert ok and r["updated"] == 2 and r["created"] == 0, (msg, r)
    assert len(f.pages) == n_pages, "update in place, no duplicate child"
    de = f.by_title(T())[0]
    assert f.texts(de) == ["[de-de] Click the NEW link."], f.texts(de)
    live_blocks = [i for k, i in f.kids[de] if k == "block" and not f.blocks[i]["in_trash"]]
    assert len(live_blocks) == 1, "the old body is trashed, exactly one body remains"
    # a trashed target is orphaned and recreated, never resurrected
    f.pages[de]["in_trash"] = True
    f.blocks[next(i for k, i in f.kids[src] if k == "block")]["paragraph"]["rich_text"] = [
        run("Third text.")
    ]
    ok, msg, r = _tr(c, f, article_ids=[src])
    assert ok, (msg, r)
    de2 = f.by_title(T())
    assert len(de2) == 1 and de2[0] != de, "a new page replaces the trashed one"
    print("  3. re-run skips, edit updates in place (one body), a trashed target is recreated")

    # ===================================================================== 4
    # 4a: everything held on a new pair
    f4 = FakeNotion()
    s4 = _simple_source(f4)
    c4 = _mk("N held all", f4.root)
    undo = _hold(lambda sg: True)
    try:
        ok, msg, r = _tr(c4, f4)
    finally:
        undo()
    assert not f4.by_title(T()) and not _hashes(c4.id) and not f4.writes(), f4.writes()
    assert idmap.get(c4.id, s4, "de-de") is None
    print("  4a. all units held: no page, no hash, no write")
    # 4b: body held on a NEW pair -> no page with source text
    f4b = FakeNotion()
    s4b = _simple_source(f4b)
    c4b = _mk("N held body new", f4b.root)
    undo = _hold(lambda sg: sg.order_index >= 1)
    try:
        ok, msg, r = _tr(c4b, f4b)
    finally:
        undo()
    assert not f4b.children_pages(s4b) and not f4b.writes() and not _hashes(c4b.id)
    assert "Held back" in msg, msg
    cn = _conn_obj(f4b)
    for held in ({"_held_body": True}, {"_held_title": True}):
        tid, err = cn.create_target(
            s4b, "de-de", {"title": "x", "blocks": [], "locale": "de-de", **held}
        )
        assert tid is None and err.startswith("held-back"), (held, err)
    print("  4b. body held on a NEW pair: nothing created, create_target refuses on its own")
    # 4c: existing target, source edited, TITLE held: stored title kept
    f4c = FakeNotion()
    s4c = _simple_source(f4c)
    c4c = _mk("N held title", f4c.root)
    _tr(c4c, f4c)
    tgt = f4c.by_title(T())[0]
    f4c.pages[tgt]["title"] = "[de-de] Menschlicher Titel"
    _unseal(c4c.id)  # this test needs the stored title to differ; 11 covers the guard itself
    f4c.blocks[next(i for k, i in f4c.kids[s4c] if k == "block")]["paragraph"]["rich_text"] = [
        run("Second text.")
    ]
    old_hash = _hashes(c4c.id)[f"{s4c}:de-de"]
    undo = _hold(lambda sg: sg.order_index == 0)
    try:
        ok, msg, r = _tr(c4c, f4c)
    finally:
        undo()
    assert f4c.pages[tgt]["title"] == "[de-de] Menschlicher Titel", f4c.pages[tgt]
    assert f4c.texts(tgt) == ["[de-de] Second text."], f4c.texts(tgt)
    # #666: no new hash, and the older one is dropped (the page is now mixed)
    assert old_hash and f"{s4c}:de-de" not in _hashes(c4c.id), (
        "a partial pair records no new hash and drops its stale one",
        _hashes(c4c.id),
    )
    assert r.get("partial_held") == 1, r
    print("  4c. title held on an existing page: stored title kept, body written, no new hash")
    # 4d: existing target, one body unit held: the stored body stays as is (no source text)
    f4d = FakeNotion()
    s4d = f4d.add_page(
        "Two",
        parent=f4d.root,
        blocks=[block("paragraph", [run("First.")]), block("paragraph", [run("Second.")])],
    )
    c4d = _mk("N held body upd", f4d.root)
    _tr(c4d, f4d)
    tgt = f4d.by_title(T("Two"))[0]
    before = f4d.texts(tgt)
    first = next(i for k, i in f4d.kids[s4d] if k == "block")
    f4d.blocks[first]["paragraph"]["rich_text"] = [run("First, edited.")]
    undo = _hold(lambda sg: sg.order_index == 2)
    try:
        ok, msg, r = _tr(c4d, f4d)
    finally:
        undo()
    after = f4d.texts(tgt)
    assert after == before, ("the stored body is kept whole", before, after)
    assert not any(t in ("First, edited.", "Second.") for t in after), "no source text ever lands"
    # #666: a pair written with a held unit drops its older hash (the engine cannot tell the
    # page from a mixed one); a source revert to that text then costs one re-translation.
    assert f"{s4d}:de-de" not in _hashes(c4d.id), ("stale hash dropped", _hashes(c4d.id))
    ok, msg, r = _tr(c4d, f4d)  # approved (no hold): retried and written in full
    assert f4d.texts(tgt) == ["[de-de] First, edited.", "[de-de] Second."], f4d.texts(tgt)
    print("  4d. body held on an existing page: stored body kept whole, retried after approval")

    # ===================================================================== 5
    f5 = FakeNotion()
    s5 = _simple_source(f5)
    human = f5.seed_preexisting_target(s5, T())
    f5.put_block(human, block("paragraph", [run("Handgeschrieben")]))
    c5 = _mk("N needs link", f5.root)
    ok, msg, r = _tr(c5, f5)
    row = idmap.get(c5.id, s5, "de-de")
    assert row is not None and row.status == "needs_link" and row.target_id == human, row
    assert f5.texts(human) == ["Handgeschrieben"], "a human page is never overwritten"
    assert not [e for e in f5.writes() if human in e["path"]], f5.writes()
    print("  5. a same-title human page is never overwritten (needs_link)")

    # ===================================================================== 6
    f6 = FakeNotion()
    s6 = _simple_source(f6)
    other_tree = f6.add_page("Other tree", parent=None)
    foreign = f6.add_page("Foreign", parent=other_tree, blocks=[block("paragraph", [run("x")])])
    trashed = _simple_source(f6, "Gone")
    f6.pages[trashed]["in_trash"] = True
    cn = _conn_obj(f6)
    n_before = len(f6.log)
    for bad in (
        "../v1/users/me",
        "abc",
        "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6٣",
        "x" * 40,
        "",
        "https://evil.example/" + "a" * 32,
    ):
        _raises(lambda b=bad: cn.discover_articles(article_ids=[b]), "valid Notion page id")
    assert len(f6.log) == n_before, "invalid ids never reach the wire"
    assert [p["id"] for p in cn.discover_articles(article_ids=[s6])] == [s6]
    assert cn.discover_articles(article_ids=[foreign]) == [], (
        "foreign page answers like a missing one"
    )
    assert cn.discover_articles(article_ids=[trashed]) == []
    assert cn.discover_articles(article_ids=[other_tree]) == []
    assert cn.discover_articles(article_ids=[f6.root]) == [], "the root itself is not a source"
    assert cn.show_article(foreign) is None and cn.show_article("nope") is None
    # explicit id of a page in the subtree but deeper than source_depth is out of scope
    deep6 = f6.add_page("Deep", parent=s6, blocks=[block("paragraph", [run("deep")])])
    assert cn.discover_articles(article_ids=[deep6]) == []
    cd = _conn_obj(f6, source_depth="2")
    assert [p["id"] for p in cd.discover_articles(article_ids=[deep6])] == [deep6]
    srcs = {p["id"] for p in cd.discover_articles()}
    assert srcs == {s6, deep6}, srcs
    # our own targets are never sources at depth 2, even with a custom title pattern
    c6 = _mk("N depth2", f6.root, {"source_depth": "2", "title_pattern": "{title} ({loc})"})
    ok, msg, r = _tr(c6, f6)
    assert ok, (msg, r)
    ok, msg, r = _tr(c6, f6)
    assert ok and r["created"] == 0 and r["pages"] == 2, ("targets are not re-discovered", msg, r)
    # a source whose title merely looks like a tag is still a source
    f6b = FakeNotion()
    faq = _simple_source(f6b, "[FAQ] How to")
    assert [p["id"] for p in _conn_obj(f6b).discover_articles()] == [faq]
    # ids: URL / dashed / hex all canonical; Arabic-Indic digits are not ids
    h = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
    d = "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"
    assert canon_id(h) == d and canon_id(d.upper()) == d
    assert canon_id(f"https://www.notion.so/ws/My-Page-{h}?pvs=4") == d
    assert canon_id(f"https://evil.example/My-Page-{h}") is None
    assert canon_id("1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6٣") is None and canon_id(None) is None
    # the title pattern is plain substitution
    cp = _conn_obj(f6, title_pattern="{title.__class__} {loc}")
    assert cp._target_title("fr", "T") == "{title.__class__} fr"
    cp2 = _conn_obj(f6, title_pattern="no loc here")
    assert cp2._target_title("fr", "T") == "[fr] T", "a pattern without {loc} falls back"
    # api_version is validated before it reaches a header
    assert _conn_obj(f6, api_version="2025-09-03\r\nX: y")._api_version() == NOTION_VERSION
    # a root that is not shared / not valid fails with an operator message
    f6.hidden.add(f6.root)
    ok, msg = _conn_obj(f6).test()
    assert not ok and "shared" in msg.lower(), msg
    f6.hidden.clear()
    ok, msg = NotionConnector(
        {"parent_page_id": "not-an-id"}, {"token": TOKEN}, transport=f6.transport()
    ).test()
    assert not ok and "not valid" in msg, msg
    print("  6. scope: bad ids never hit the wire, foreign/trashed read as missing, depth, targets")

    # ===================================================================== 7
    f7 = FakeNotion()
    db, (ds1, ds2) = f7.add_database(n_sources=2)
    rows7 = [
        f7.add_page(
            f"Row {i}",
            parent=None,
            db_row_of=(db, ds1 if i % 2 else ds2),
            blocks=[block("paragraph", [run(f"Text {i}")])],
        )
        for i in range(5)
    ]
    other_db, (ods,) = f7.add_database()
    foreign_row = f7.add_page(
        "Foreign row",
        parent=None,
        db_row_of=(other_db, ods),
        blocks=[block("paragraph", [run("x")])],
    )
    cdb = _conn_obj(f7)
    cdb.config["parent_page_id"] = db
    got = {p["id"] for p in cdb.discover_articles()}
    assert got == set(rows7), got
    assert cdb.discover_articles(article_ids=[foreign_row]) == []
    q = [e for e in f7.log if e["path"].endswith("/query")]
    assert q and all(e["method"] == "POST" and e["version"] == NOTION_VERSION for e in q)
    assert {e["path"] for e in q} == {
        f"/v1/data_sources/{ds1}/query",
        f"/v1/data_sources/{ds2}/query",
    }
    assert any((e["body"] or {}).get("start_cursor") for e in q), "follows the cursor"
    ok, msg = cdb.test()
    assert ok and "root database" in msg and "5 source" in msg, msg
    cdbo = NotionConnector(
        {"parent_page_id": db, "api_version": "2022-06-28"},
        {"token": TOKEN},
        transport=f7.transport(),
    )
    _raises(lambda: cdbo.discover_articles(), "data sources")
    cdbc = _mk("N database", db, {"target_locales": "de-DE"})
    ok, msg, r = _tr(cdbc, f7)
    assert ok and r["created"] == 5, (msg, r)
    for row in rows7:
        assert len(f7.children_pages(row)) == 1, "target is a child page of the row"
    posts = [e for e in f7.log if e["method"] == "POST" and e["path"].endswith("/pages")]
    assert all(
        "page_id" in e["body"]["parent"] and "data_source_id" not in e["body"]["parent"]
        for e in posts
    )
    ok, msg, r = _tr(cdbc, f7)
    assert ok and r["created"] == 0 and r["skipped"] == 5, (msg, r)
    print("  7. database root: data-source query with cursors, rows as sources, child-page targets")

    # ===================================================================== 8
    f8 = FakeNotion()
    _simple_source(f8)
    c8 = _mk("N failures", f8.root)
    sleeps = []
    cbase._sleep = sleeps.append
    f8.rate_limit_next = 2
    ok, msg, r = _tr(c8, f8)
    cbase._sleep = lambda s: None
    assert ok and r["created"] == 1 and sleeps and all(s == 1 for s in sleeps), (msg, sleeps)
    print("  8a. 429 honours Retry-After through base._request, the run still completes")
    f8b = FakeNotion()
    s8b = _simple_source(f8b)
    c8b = _mk("N 503", f8b.root)
    f8b.fail_post_pages = 1
    ok, msg, r = _tr(c8b, f8b)
    posts = [e for e in f8b.log if e["method"] == "POST" and e["path"].endswith("/pages")]
    assert len(posts) == 1 and not f8b.children_pages(s8b) and r["failures"], (
        "503 not resent",
        posts,
    )
    ok, msg, r = _tr(c8b, f8b)
    assert ok and len(f8b.children_pages(s8b)) == 1
    f8c = FakeNotion()
    s8c = _simple_source(f8c)
    c8c = _mk("N 503 committed", f8c.root)
    f8c.commit_post_pages = 1
    ok, msg, r = _tr(c8c, f8c)
    assert ok and len(f8c.children_pages(s8c)) == 1, ("the committed page is adopted", msg)
    row = idmap.get(c8c.id, s8c, "de-de")
    assert row.status == "done" and row.target_id == f8c.children_pages(s8c)[0]
    print("  8b. a 5xx create is never resent; a 503 that committed is adopted, not duplicated")
    f8d = FakeNotion()
    s8d = _simple_source(f8d)
    c8d = _mk("N append fail", f8d.root)
    # make the first create succeed, then fail the first append
    orig_create = NotionConnector.create_target

    def create_then_arm(self, source_id, locale, fields):
        tid, err = orig_create(self, source_id, locale, fields)
        if tid:
            f8d.fail_append_once.add(tid)
        return tid, err

    NotionConnector.create_target = create_then_arm
    try:
        ok, msg, r = _tr(c8d, f8d)
    finally:
        NotionConnector.create_target = orig_create
    row = idmap.get(c8d.id, s8d, "de-de")
    assert not ok and row.status == "building", (msg, row)
    # the new page never stays live and empty: it went to the trash with the refused body
    assert f8d.children_pages(s8d) == [] and f8d.pages[row.target_id]["in_trash"], msg
    ok, msg, r = _tr(c8d, f8d)
    live = f8d.children_pages(s8d)
    assert ok and len(live) == 1, "the retry recreates exactly one page"
    assert f8d.texts(live[0]) == ["[de-de] Click the link."]
    assert idmap.get(c8d.id, s8d, "de-de").status == "done"
    f8e = FakeNotion()
    s8e = _simple_source(f8e)
    c8e = _mk("N append commit", f8e.root)
    orig_create2 = NotionConnector.create_target

    def create_then_arm2(self, source_id, locale, fields):
        tid, err = orig_create2(self, source_id, locale, fields)
        if tid:
            f8e.commit_append_once.add(tid)
        return tid, err

    NotionConnector.create_target = create_then_arm2
    try:
        ok, msg, r = _tr(c8e, f8e)
    finally:
        NotionConnector.create_target = orig_create2
    row = idmap.get(c8e.id, s8e, "de-de")
    assert f8e.texts(row.target_id) == [], (
        "blocks a 503 committed are rolled back",
        f8e.texts(row.target_id),
    )
    assert f8e.children_pages(s8e) == [], "the empty new page is not left live"
    ok, msg, r = _tr(c8e, f8e)
    live = f8e.children_pages(s8e)
    assert ok and len(live) == 1 and f8e.texts(live[0]) == ["[de-de] Click the link."]
    print("  8c. a refused or 503'd append is rolled back; the retry resumes with no duplicate")
    bad = _mk("N bad token", f8.root, token="ntn_WRONG_token_value_123")
    ok, msg = NotionConnector(
        _cfg(bad.id), {"token": "ntn_WRONG_token_value_123"}, transport=f8.transport()
    ).test()
    assert not ok and "rejected" in msg.lower() and "WRONG" not in msg, msg
    blob = " ".join(lg.message or "" for c_ in (c, c8, c8b, c8c, c8d) for lg in _synclogs(c_.id))
    assert TOKEN not in blob and TOKEN not in msg
    print("  8d. a wrong token is reported without echoing it; the token is in no run log")

    # ===================================================================== 9
    f9 = FakeNotion()
    f9.add_page(
        "Long page", parent=f9.root, blocks=[block("paragraph", [run(f"P{i}")]) for i in range(250)]
    )
    c9 = _mk("N long", f9.root)
    ok, msg, r = _tr(c9, f9)
    assert ok, msg
    tgt = f9.by_title(T("Long page"))[0]
    assert len(f9.texts(tgt)) == 250 and f9.texts(tgt)[249] == "[de-de] P249"
    appends = [
        e
        for e in f9.log
        if e["method"] == "PATCH" and e["path"].endswith("/children") and tgt in e["path"]
    ]
    assert [len(e["body"]["children"]) for e in appends] == [100, 100, 50], [
        len(e["body"]["children"]) for e in appends
    ]
    f9b = FakeNotion()
    for i in range(4):
        _simple_source(f9b, f"Page {i}")
    c9b = _mk("N budget", f9b.root, {"target_locales": "de-DE, fr"})
    old = config.MAX_WRITES_PER_RUN
    config.MAX_WRITES_PER_RUN = 3
    try:
        ok, msg, r = _tr(c9b, f9b)
    finally:
        config.MAX_WRITES_PER_RUN = old
    assert (
        r["created"] == 3
        and len([p for p in f9b.pages.values() if p["title"].startswith("[")]) == 3
    ), (msg, r)
    print("  9. 250 blocks read and appended in 100/100/50, the write budget stops a run")

    # ===================================================================== 11
    # 11a: a hand-edited translation is detected and NOT overwritten (body, then title)
    f11 = FakeNotion()
    s11 = _simple_source(f11)
    c11 = _mk("N handedit", f11.root)
    ok, msg, r = _tr(c11, f11)
    assert ok and r["created"] == 1 and "no draft" in msg.lower(), (msg, r)
    t11 = f11.by_title(T())[0]
    assert _cfg(c11.id).get("notion_target_prints", {}).get(t11), "fingerprint sealed after write"
    ok, msg, r = _tr(c11, f11)
    assert r["skipped"] == 1, (msg, r)
    human = next(i for k, i in f11.kids[t11] if k == "block")
    f11.blocks[human]["paragraph"]["rich_text"] = [run("Human fixed this sentence.")]
    f11.blocks[next(i for k, i in f11.kids[s11] if k == "block")]["paragraph"]["rich_text"] = [
        run("Source changed.")
    ]
    n_w = len(f11.writes())
    ok, msg, r = _tr(c11, f11, article_ids=[s11])
    assert r["updated"] == 0 and len(f11.writes()) == n_w, (msg, r, "nothing may be written")
    assert f11.texts(t11) == ["Human fixed this sentence."], f11.texts(t11)
    assert any("edited-in-notion" in str(x["error"]) for x in r["failures"]), r
    assert "NOT overwritten" in msg, msg
    assert _hashes(c11.id)[f"{s11}:de-de"] != "", "hash exists from the first run"
    # trashing the edited copy makes the next run create a fresh translation
    f11.pages[t11]["in_trash"] = True
    ok, msg, r = _tr(c11, f11, article_ids=[s11])
    assert ok and r["created"] == 1, (msg, r)
    t11b = f11.by_title(T())[0]
    assert t11b != t11 and f11.texts(t11b) == ["[de-de] Source changed."], f11.texts(t11b)
    # title edited by hand: also detected
    f11.pages[t11b]["title"] = "Mein Titel"
    f11.blocks[next(i for k, i in f11.kids[s11] if k == "block")]["paragraph"]["rich_text"] = [
        run("Source changed again.")
    ]
    ok, msg, r = _tr(c11, f11, article_ids=[s11])
    assert r["updated"] == 0 and f11.pages[t11b]["title"] == "Mein Titel", (msg, r)
    print("  11a. a translation edited in Notion is detected, not overwritten, reported")

    # 11b: a refused write that changed nothing visible keeps the seal (a hand edit made later
    #      is still caught); the retry rewrites the page and seals it. A write that leaves the
    #      page mixed (old + new) is unsealed so the next run converges: group 12a.
    f11b = FakeNotion()
    s11b = _simple_source(f11b)
    c11b = _mk("N midfail", f11b.root)
    _tr(c11b, f11b)
    t11c = f11b.by_title(T())[0]
    f11b.blocks[next(i for k, i in f11b.kids[s11b] if k == "block")]["paragraph"]["rich_text"] = [
        run("Edit two.")
    ]
    f11b.fail_append_once.add(t11c)
    ok, msg, r = _tr(c11b, f11b, article_ids=[s11b])
    assert r["updated"] == 0 and r["failures"], (msg, r)
    assert f11b.texts(t11c) == ["[de-de] Click the link."], "the old body is untouched"
    sealed = _cfg(c11b.id)["notion_target_prints"].get(t11c)
    assert sealed, "nothing visible changed: the seal is kept"
    ok, msg, r = _tr(c11b, f11b, article_ids=[s11b])
    assert ok and r["updated"] == 1 and f11b.texts(t11c) == ["[de-de] Edit two."], (msg, r)
    assert _cfg(c11b.id)["notion_target_prints"].get(t11c) not in ("", sealed), "sealed again"
    f11b.fail_append_once.add(t11c)
    f11b.blocks[next(i for k, i in f11b.kids[s11b] if k == "block")]["paragraph"]["rich_text"] = [
        run("Edit three.")
    ]
    _tr(c11b, f11b, article_ids=[s11b])  # refused again, nothing landed
    live_b = next(i for k, i in f11b.kids[t11c] if k == "block" and not f11b.blocks[i]["in_trash"])
    f11b.blocks[live_b]["paragraph"]["rich_text"] = [run("Hand edit after a failed run.")]
    ok, msg, r = _tr(c11b, f11b, article_ids=[s11b])
    assert "edited-in-notion" in str(r["failures"]), (msg, r)
    assert f11b.texts(t11c) == ["Hand edit after a failed run."]
    print("  11b. a refused write keeps the seal; a later hand edit is still caught")

    # 11c: limits: 150-row table (100 inline + 50 appended), >100 runs refused with no write,
    #      a mention that cannot be written back is reported, many tables batch under 1000 blocks
    f11c = FakeNotion()
    rows = [
        {"type": "table_row", "table_row": {"cells": [[run(f"r{n}")], [run("v")]]}}
        for n in range(150)
    ]
    big = {
        "type": "table",
        "table": {"table_width": 2, "has_column_header": False, "has_row_header": False,
                  "children": rows},
    }  # fmt: skip
    link_mention = {
        "type": "mention",
        "mention": {"type": "link_mention", "link_mention": {"href": "https://x.example"}},
        "annotations": run("")["annotations"],
        "plain_text": "Example site",
        "href": "https://x.example",
    }
    _ = f11c.add_page(
        "Big",
        parent=f11c.root,
        blocks=[big, block("paragraph", [run("See "), link_mention, run(" now")])],
    )
    c11c = _mk("N limits", f11c.root)
    ok, msg, r = _tr(c11c, f11c)
    assert ok and r["created"] == 1, (msg, r)
    t11d = f11c.by_title(T("Big"))[0]
    table = f11c.body(t11d)[0]
    assert len(table[2]) == 150, len(table[2])
    assert any("mention" in n for n in r["not_carried_over"]), r["not_carried_over"]
    assert "Not carried over" in msg, msg
    f11d = FakeNotion()
    many = [block("paragraph", [run("w ")] * 120)] + [big] * 8
    _ = f11d.add_page("Wide", parent=f11d.root, blocks=many)
    c11d = _mk("N wide", f11d.root)
    ok, msg, r = _tr(c11d, f11d)
    t11e = f11d.by_title(T("Wide"))
    assert not t11e and r["failures"] and "rich-text runs" in str(r["failures"]), (msg, r)
    assert not [w for w in f11d.writes() if w["path"].endswith("/children")], "no body written"
    f11e = FakeNotion()
    _ = f11e.add_page("Tables", parent=f11e.root, blocks=[big] * 8)
    c11e = _mk("N batch", f11e.root)
    ok, msg, r = _tr(c11e, f11e)
    assert ok and r["created"] == 1, (msg, r)
    assert len(f11e.body(f11e.by_title(T("Tables"))[0])) == 8
    print("  11c. 150-row table, 120-run block refused unwritten, mention reported, batching")

    # ===================================================================== 12
    # 12a: the live page is never empty or partial-only while its body is replaced, whether
    #      the process dies (Killed) or the API goes down (500 from then on, so even the
    #      rollback fails) at ANY write, and the next run
    #      converges to exactly one copy of the new body (no duplicates, sealed again).
    def _swap_case(name):
        f = FakeNotion()
        nested = block("bulleted_list_item", [run("A3")])
        nested["bulleted_list_item"]["children"] = [block("bulleted_list_item", [run("A4")])]
        src = f.add_page(
            "Swap",
            parent=f.root,
            blocks=[block("paragraph", [run("A1")]), block("paragraph", [run("A2")]), nested],
        )
        c = _mk(name, f.root)
        ok, msg, r = _tr(c, f)
        assert ok and r["created"] == 1, (msg, r)
        t = f.by_title(T("Swap"))[0]

        def _src_blocks(parent):
            for k, i in f.kids[parent]:
                if k == "block":
                    yield i
                    yield from _src_blocks(i)

        for i in _src_blocks(src):
            b = f.blocks[i]
            txt = b[b["type"]]["rich_text"][0]["text"]["content"]
            b[b["type"]]["rich_text"] = [run("B" + txt[1:])]
        return f, c, src, t

    old = [f"[de-de] A{n}" for n in range(1, 5)]
    new = [f"[de-de] B{n}" for n in range(1, 5)]

    def _reader_ok(f, t, where):
        seen = f.texts(t)
        assert seen, (where, "the live page is EMPTY")
        assert set(old) <= set(seen) or set(new) <= set(seen), (where, "partial-only", seen)

    def _converges(f, c, src, t, where):
        ok, msg, r = _tr(c, f, article_ids=[src])
        assert ok and f.texts(t) == new, (where, msg, r, f.texts(t))
        assert f.by_title(T("Swap")) == [t], (where, "one target page")
        assert _cfg(c.id)["notion_target_prints"].get(t), (where, "sealed again")
        ok, msg, r = _tr(c, f)
        assert r["skipped"] == 1 and f.texts(t) == new, (where, msg, r)

    for mode in ("kill", "fail"):
        k, points = 0, 0
        while True:
            k += 1
            f, c, src, t = _swap_case(f"N swap {mode} {k}")
            f.n_writes = 0
            hit = False
            if mode == "kill":
                f.kill_at_write = k
                try:
                    _tr(c, f, article_ids=[src])
                except Killed:
                    hit = True
                f.kill_at_write = 0
            else:
                f.fail_at_write = k
                ok, msg, r = _tr(c, f, article_ids=[src])
                hit = bool(r["failures"])
                f.fail_at_write = 0
            if not hit:
                break
            points += 1
            _reader_ok(f, t, f"{mode} at write {k}")
            _converges(f, c, src, t, f"{mode} at write {k}")
        assert points >= 6, (mode, points)
    print("  12a. replace body: killed or failed at every write, never empty, converges")

    # 12b: first creation: a refused body never leaves an empty live page (during a full
    #      outage the trash call fails too; the run message says the page is still empty);
    #      a killed run resumes into exactly one complete page
    for mode in ("kill", "fail"):
        k = 0
        while True:
            k += 1
            f = FakeNotion()
            src = f.add_page(
                "Fresh",
                parent=f.root,
                blocks=[block("paragraph", [run("A1")]), block("paragraph", [run("A2")])],
            )
            c = _mk(f"N fresh {mode} {k}", f.root)
            f.n_writes = 0
            hit = False
            if mode == "kill":
                f.kill_at_write = k
                try:
                    _tr(c, f)
                except Killed:
                    hit = True
                f.kill_at_write = 0
            else:
                f.fail_once_at_write = k
                ok, msg, r = _tr(c, f)
                hit = bool(r["failures"])
                f.fail_once_at_write = 0
                for p in f.by_title(T("Fresh")):
                    assert f.texts(p), (mode, k, "a failed first write left an EMPTY live page")
            if not hit:
                break
            ok, msg, r = _tr(c, f)
            live = f.by_title(T("Fresh"))
            assert ok and len(live) == 1, (mode, k, msg, r, live)
            assert f.texts(live[0]) == ["[de-de] A1", "[de-de] A2"], (mode, k, f.texts(live[0]))
        assert k >= 3, (mode, k)
    print("  12b. first write: a refused body leaves no empty page; a killed run resumes")

    # 12c: the fingerprint sees formatting-only edits (bold, link, block colour, order, code
    #      language, a to-do tick) and a renamed mention target is NOT a hand edit
    edits = {
        "bold": lambda b: b["paragraph"]["rich_text"][0]["annotations"].update(bold=True),
        "link": lambda b: b["paragraph"]["rich_text"][0]["text"].update(
            link={"url": "https://example.com/x"}
        ),
        "colour": lambda b: b["paragraph"].update(color="red_background"),
    }
    for what, edit in edits.items():
        f = FakeNotion()
        src = _simple_source(f)
        c = _mk(f"N fp {what}", f.root)
        _tr(c, f)
        t = f.by_title(T())[0]
        tb = next(i for k, i in f.kids[t] if k == "block")
        edit(f.blocks[tb])
        f.blocks[next(i for k, i in f.kids[src] if k == "block")]["paragraph"]["rich_text"] = [
            run("Source changed.")
        ]
        ok, msg, r = _tr(c, f, article_ids=[src])
        assert r["updated"] == 0 and "edited-in-notion" in str(r["failures"]), (what, msg, r)
    f = FakeNotion()
    src = f.add_page(
        "Order",
        parent=f.root,
        blocks=[
            block("paragraph", [run("One.")]),
            block("paragraph", [run("Two.")]),
            block("code", [run("x = 1")], language="python"),
            block("to_do", [run("Task")], checked=False),
        ],
    )
    c = _mk("N fp order", f.root)
    _tr(c, f)
    t = f.by_title(T("Order"))[0]
    tk = [i for k, i in f.kids[t] if k == "block"]
    snap = (list(f.kids[t]), json.dumps([f.blocks[i] for i in tk], sort_keys=True))
    for n, (what, edit) in enumerate(
        (
            ("order", lambda: f.kids[t].reverse()),
            ("language", lambda: f.blocks[tk[2]]["code"].update(language="javascript")),
            ("checked", lambda: f.blocks[tk[3]]["to_do"].update(checked=True)),
        )
    ):
        edit()
        sb = next(i for k, i in f.kids[src] if k == "block")
        f.blocks[sb]["paragraph"]["rich_text"] = [run(f"One {n}.")]
        ok, msg, r = _tr(c, f, article_ids=[src])
        assert r["updated"] == 0 and "edited-in-notion" in str(r["failures"]), (what, msg, r)
        f.kids[t] = list(snap[0])  # undo the edit: the page matches its seal again
        for i, b in zip(tk, json.loads(snap[1]), strict=True):
            f.blocks[i] = b
    f = FakeNotion()
    other = f.add_page("Elsewhere", parent=None)
    src = f.add_page(
        "Mentions",
        parent=f.root,
        blocks=[block("paragraph", [run("See "), mention_page(other, "Elsewhere"), run(".")])],
    )
    c = _mk("N fp mention", f.root)
    _tr(c, f)
    t = f.by_title(T("Mentions"))[0]
    tb = next(i for k, i in f.kids[t] if k == "block")
    for el in f.blocks[tb]["paragraph"]["rich_text"]:
        if el.get("type") == "mention":
            el["plain_text"] = "Renamed page"  # Notion renders the mention's CURRENT title
    sb = next(i for k, i in f.kids[src] if k == "block")
    f.blocks[sb]["paragraph"]["rich_text"][0] = run("Look at ")
    ok, msg, r = _tr(c, f, article_ids=[src])
    assert ok and r["updated"] == 1, (msg, r, "a renamed mention target is not a hand edit")
    print("  12c. fingerprint: formatting-only edits caught, a renamed mention is not an edit")

    # 12d: an edited translation is PARKED: later scheduled runs over the same source spend no
    #      MT and write nothing, but still report it; trashing the page lets it be re-created
    f = FakeNotion()
    src = _simple_source(f)
    c = _mk("N park", f.root)
    _tr(c, f)
    t = f.by_title(T())[0]
    f.blocks[next(i for k, i in f.kids[t] if k == "block")]["paragraph"]["rich_text"] = [
        run("Human fixed this sentence.")
    ]
    f.blocks[next(i for k, i in f.kids[src] if k == "block")]["paragraph"]["rich_text"] = [
        run("Source changed.")
    ]
    ok, msg, r = _tr(c, f)
    assert "edited-in-notion" in str(r["failures"]), (msg, r)
    calls = []
    orig_pt = tr_mod.pretranslate_document

    def _counting(*a, **kw):
        calls.append(1)
        return orig_pt(*a, **kw)

    tr_mod.pretranslate_document = _counting
    try:
        n_w = len(f.writes())
        for _ in range(2):
            ok, msg, r = _tr(c, f)
            assert not calls and len(f.writes()) == n_w, (msg, r, "no MT, no write")
            assert r["edited_in_notion"] == 1 and "edited in Notion" in msg, (msg, r)
        f.pages[t]["in_trash"] = True
        ok, msg, r = _tr(c, f)
        assert ok and r["created"] == 1 and calls, (msg, r)
    finally:
        tr_mod.pretranslate_document = orig_pt
    assert f.texts(f.by_title(T())[0]) == ["[de-de] Source changed."]
    print("  12d. an edited translation is parked: reported every run, no MT, no write")

    # ===================================================================== 10
    spec = reg.spec("notion")
    assert spec.archived is False and str(NotionConnector.capability.topology) in (
        "T3",
        "Topology.T3",
    )
    assert reg.REGISTRY["notion"] is NotionConnector
    assert (
        NOTION_VERSION == "2026-03-11" and NotionConnector.capability.api_version == NOTION_VERSION
    )
    fn = {x.name for x in spec.config_fields}
    assert {"parent_page_id", "source_depth", "api_version", "title_pattern"} <= fn, fn
    ok, msg = _conn_obj(f).test()
    assert ok and "Connected to Notion" in msg and "root page" in msg and NOTION_VERSION in msg, msg
    # a non-admin may not move a credentialed connection to another root (writes are live)
    rc = _mk("N scope", f.root)
    with Session(engine) as s_:
        rconn = s_.get(Connection, rc.id)
    moved = dict(_cfg(rc.id), parent_page_id=uid(999))
    err = cstore.scope_admin_error("notion", rconn, moved, caller_is_admin=False, actor="u")
    assert "Only an admin" in err, err
    assert not cstore.scope_admin_error("notion", rconn, moved, caller_is_admin=True)
    assert not cstore.scope_admin_error("notion", rconn, _cfg(rc.id), caller_is_admin=False)
    print("  10. surfaces: un-archived, T3, 2026-03-11, test() reports root kind and version")
    print("verify_notion_harden OK")


if __name__ == "__main__":
    main()
