"""Contentful Rich Text translation (#648) and the validation-hold hash rule (#665).

Fully offline: the Rich Text walker (app/filters/contentful_rich_text.py) on realistic
Contentful documents, then the whole connector flow against the fake CMA from
verify_contentful. Fail-before evidence: before this change a Rich Text field was skipped
("rich text ... not translated"), and an UPDATE whose translated field broke a CMA
validation still recorded the content hash.

Node types and JSON shape follow Contentful's docs
(https://www.contentful.com/developers/docs/concepts/rich-text/) and the node enums in
https://github.com/contentful/rich-text (rich-text-types: blocks.ts, inlines.ts, marks.ts).

ASCII-only prints; final line is 'verify_contentful_richtext OK'.
"""

import copy
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_contentful_rt_")

from sqlmodel import select

from app.db import init_db
from app.filters import contentful_rich_text as rt
from app.models import Segment
from app.services import translate as translate_mod
from app.services.connectors import store as cstore

# verify_contentful owns the fake CMA.
from tests.verify_contentful import FakeContentful, _conn


def _t(value, marks=None):
    return {
        "nodeType": "text",
        "value": value,
        "marks": [{"type": m} for m in marks or []],
        "data": {},
    }


def _blk(kind, content, data=None):
    return {"nodeType": kind, "data": data or {}, "content": content}


LINK = {
    "nodeType": "hyperlink",
    "data": {"uri": "https://example.com/pricing"},
    "content": [_t("our "), _t("pricing page", ["italic"])],
}
ENTRY_LINK = {
    "nodeType": "entry-hyperlink",
    "data": {"target": {"sys": {"id": "e9", "type": "Link", "linkType": "Entry"}}},
    "content": [_t("related post")],
}
EMBED_INLINE = {
    "nodeType": "embedded-entry-inline",
    "data": {"target": {"sys": {"id": "e7", "type": "Link", "linkType": "Entry"}}},
    "content": [],
}
EMBED_BLOCK = _blk(
    "embedded-entry-block",
    [],
    {"target": {"sys": {"id": "e8", "type": "Link", "linkType": "Entry"}}},
)
EMBED_ASSET = _blk(
    "embedded-asset-block",
    [],
    {"target": {"sys": {"id": "a1", "type": "Link", "linkType": "Asset"}}},
)
UNKNOWN = {"nodeType": "x-future-block", "data": {"keep": [1, 2, {"a": None}]}, "content": []}


def rich_doc():
    return _blk(
        "document",
        [
            _blk("heading-2", [_t("Getting started")]),
            _blk(
                "paragraph",
                [
                    _t("Read the "),
                    _t("quick guide", ["bold"]),
                    _t(" and visit "),
                    copy.deepcopy(LINK),
                    _t(" now. "),
                    copy.deepcopy(EMBED_INLINE),
                    _t(" See also "),
                    copy.deepcopy(ENTRY_LINK),
                    _t("."),
                ],
            ),
            _blk(
                "unordered-list",
                [
                    _blk("list-item", [_blk("paragraph", [_t("First step")])]),
                    _blk(
                        "list-item",
                        [
                            _blk("paragraph", [_t("Second step ", ["bold", "italic"]), _t("here")]),
                            _blk(
                                "ordered-list",
                                [_blk("list-item", [_blk("paragraph", [_t("Nested step")])])],
                            ),
                        ],
                    ),
                ],
            ),
            _blk("blockquote", [_blk("paragraph", [_t("Quality is a habit.")])]),
            _blk("paragraph", [_t("Run "), _t("npm install", ["code"]), _t(" first.")]),
            copy.deepcopy(EMBED_BLOCK),
            copy.deepcopy(EMBED_ASSET),
            _blk("hr", []),
            copy.deepcopy(UNKNOWN),
            _blk(
                "table",
                [
                    _blk(
                        "table-row",
                        [
                            _blk("table-header-cell", [_blk("paragraph", [_t("Plan")])]),
                            _blk("table-header-cell", [_blk("paragraph", [_t("Price")])]),
                        ],
                    ),
                    _blk(
                        "table-row",
                        [
                            _blk("table-cell", [_blk("paragraph", [_t("Starter")])]),
                            _blk("table-cell", [_blk("paragraph", [_t("12 EUR")])]),
                        ],
                    ),
                ],
            ),
            _blk("paragraph", [_t("")]),
        ],
    )


def _nodes(doc, kind):
    out = []

    def go(n):
        if isinstance(n, dict):
            if n.get("nodeType") == kind:
                out.append(n)
            for c in n.get("content") or []:
                go(c)

    go(doc)
    return out


def _all_text(doc):
    return [n["value"] for n in _nodes(doc, "text")]


def _identity(units, prefix="[xx] "):
    return {u.p_index: prefix + u.text for u in units}


def test_walker():
    doc = rich_doc()
    src = copy.deepcopy(doc)
    units = rt.extract(doc)
    texts = [u.text for u in units]
    assert doc == src, "extract must not mutate the source"
    # one unit per text block: heading, paragraph, 3 list paragraphs (+ nested), quote,
    # code paragraph, 4 table cells; the empty paragraph and void blocks have none
    assert len(units) == 11, (len(units), texts)
    para = texts[1]
    assert para.startswith("Read the ⟦g1⟧quick guide⟦/g1⟧ and visit "), para
    assert "⟦g2⟧our ⟦g3⟧pricing page⟦/g3⟧⟦/g2⟧" in para, para
    assert "⟦x4⟧" in para and "⟦g5⟧related post⟦/g5⟧" in para, para
    assert any("⟦x1⟧" in t and "install" not in t for t in texts if "Run " in t), texts
    assert "npm install" not in " ".join(texts), "code spans are never sent to MT"

    # identity-with-prefix rebuild keeps every non-text node and every mark / data payload
    out, bad = rt.rebuild(doc, _identity(units))
    assert out is not None and not bad, bad
    assert doc == src, "rebuild must not mutate the source"
    assert _nodes(out, "hyperlink")[0]["data"] == {"uri": "https://example.com/pricing"}
    assert _nodes(out, "embedded-entry-inline")[0] == EMBED_INLINE
    assert _nodes(out, "embedded-entry-block")[0] == EMBED_BLOCK
    assert _nodes(out, "embedded-asset-block")[0] == EMBED_ASSET
    assert _nodes(out, "x-future-block")[0] == UNKNOWN
    assert _nodes(out, "entry-hyperlink")[0]["data"] == ENTRY_LINK["data"]
    assert "npm install" in _all_text(out)
    bolds = [n for n in _nodes(out, "text") if n["marks"] == [{"type": "bold"}]]
    assert [n["value"] for n in bolds] == ["quick guide"], bolds
    link_children = _nodes(out, "hyperlink")[0]["content"]
    assert [(c["value"], c["marks"]) for c in link_children] == [
        ("our ", []),
        ("pricing page", [{"type": "italic"}]),
    ], link_children
    assert rt._skeleton(doc) == rt._skeleton(out)
    for n in _nodes(out, "text"):
        assert set(n) == {"nodeType", "value", "marks", "data"}, n
    print("  walker: 11 block units, marks/links/embeds/unknown nodes round-trip byte-identical")

    # CJK target with reordered words and tokens moved
    u1 = units[1]
    cjk = (
        "⟦g2⟧料金ページ⟦/g2⟧を見て、⟦g1⟧クイックガイド⟦/g1⟧を読んでください。⟦x4⟧⟦g5⟧関連記事⟦/g5⟧"
    )
    trans = _identity(units)
    trans[u1.p_index] = cjk
    ja, bad = rt.rebuild(doc, trans)
    assert ja is None and u1.p_index in bad, "a dropped nested italic token must be refused"
    ok_cjk = cjk.replace("⟦g2⟧", "⟦g2⟧⟦g3⟧").replace("⟦/g2⟧", "⟦/g3⟧⟦/g2⟧")
    ja, bad = rt.rebuild(doc, {**trans, u1.p_index: ok_cjk})
    assert ja is not None and not bad, bad
    jp = _nodes(ja, "paragraph")[0]["content"]
    assert [c["nodeType"] for c in jp][:3] == ["hyperlink", "text", "text"], jp
    assert jp[0]["data"] == {"uri": "https://example.com/pricing"}
    assert jp[0]["content"][0]["marks"] == [{"type": "italic"}]
    assert jp[2]["marks"] == [{"type": "bold"}], jp[2]
    print("  walker: CJK reordered paragraph rebuilds with link, nested italic and bold intact")

    # token loss / duplication / crossing / stray bracket / missing / blank -> held (None)
    base = _identity(units)
    cases = {
        "dropped open+close": base[1].replace("⟦g1⟧quick guide⟦/g1⟧", "quick guide"),
        "dropped point token": base[1].replace("⟦x4⟧", ""),
        "duplicated token": base[1] + "⟦x4⟧",
        "unopened close": base[1].replace("⟦g1⟧", ""),
        "crossed": base[1].replace("⟦/g1⟧", "⟦/g2⟧", 1).replace("⟦/g2⟧⟦/g2⟧", "⟦/g2⟧"),
        "stray bracket": base[1] + " ⟦",
        "blank": "   ",
    }
    for name, bad_text in cases.items():
        out, bad = rt.rebuild(doc, {**base, 1: bad_text})
        assert out is None and 1 in bad, (name, bad)
    missing = dict(base)
    del missing[4]
    out, bad = rt.rebuild(doc, missing)
    assert out is None and bad == [4], bad
    print("  walker: token loss, duplicates, crossing, stray bracket, blank, missing all refused")

    # text that contains the placeholder brackets is left verbatim (never sent to MT)
    odd = _blk("document", [_blk("paragraph", [_t("a ⟦g1⟧ b")])])
    assert rt.extract(odd) == []
    assert rt.extract({"nodeType": "paragraph"}) == [] and rt.extract(None) == []
    print("  walker: bracket-bearing text and non-documents are not units")


CT_FIELDS = [
    {"id": "title", "type": "Symbol", "localized": True},
    {"id": "body", "type": "RichText", "localized": True},
    {"id": "legal", "type": "RichText", "localized": False},
]


def _fs(extra=None, de=None):
    fs = FakeContentful()
    fs.add_ct("blogPost", CT_FIELDS, display="title")
    fields = {
        "title": {"en-US": "Hello world"},
        "body": {"en-US": rich_doc()},
        "legal": {"en-US": rich_doc()},
    }
    for fid, v in (de or {}).items():
        fields.setdefault(fid, {})["de-DE"] = v
    fields.update(extra or {})
    fs.add_entry("e1", "blogPost", fields)
    return fs


def _hashes(cid):
    return (cstore.get(cid).config or {}).get("contentful_hashes") or {}


def test_flow_create():
    fs = _fs()
    c = _conn("rt create", {"target_locales": "de-DE, fr"})
    ok, msg, s = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
    assert ok, msg
    assert s["created"] == 2, s
    assert "body" in s["fields_selected"], s["fields_selected"]
    assert "rich text" not in str(s["fields_skipped"]).lower(), s["fields_skipped"]
    assert "legal" in s["fields_skipped"] and "not localized" in s["fields_skipped"]["legal"]
    assert "rich text" not in msg.lower(), msg
    f = fs.entries["e1"]["fields"]
    src = rich_doc()
    assert f["body"]["en-US"] == src, "source locale untouched"
    assert f["legal"] == {"en-US": src}, "localized:false Rich Text gets no target key"
    for loc in ("de-DE", "fr"):
        d = f["body"][loc]
        assert d["nodeType"] == "document" and d["data"] == {}, d
        assert rt._skeleton(d) == rt._skeleton(src)
        assert _nodes(d, "hyperlink")[0]["data"] == {"uri": "https://example.com/pricing"}
        assert _nodes(d, "embedded-entry-block")[0] == EMBED_BLOCK
        assert _nodes(d, "embedded-entry-inline")[0] == EMBED_INLINE
        assert _nodes(d, "x-future-block")[0] == UNKNOWN
        joined = " ".join(_all_text(d))
        assert "[" + loc.lower() + "]" in joined, joined
        assert "⟦" not in joined and "⟧" not in joined, joined
        assert [n["value"] for n in _nodes(d, "text") if n["marks"] == [{"type": "bold"}]], d
        assert "npm install" in _all_text(d)
        assert "Plan" not in joined.replace("[" + loc.lower() + "] Plan", ""), joined
    assert _hashes(c.id).get("e1:de-de") and _hashes(c.id).get("e1:fr"), _hashes(c.id)
    ok2, _msg2, s2 = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
    assert ok2 and s2["skipped"] == 2 and s2["created"] == 0, s2
    # an edit inside the Rich Text changes the source hash and re-translates
    fs.entries["e1"]["fields"]["body"]["en-US"]["content"][0]["content"][0]["value"] = "Changed"
    fs.entries["e1"]["sys"]["version"] += 1
    ok3, _msg3, s3 = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
    assert ok3 and s3["skipped"] == 0 and s3["updated"] == 2, s3
    assert "Changed" in " ".join(_all_text(fs.entries["e1"]["fields"]["body"]["de-DE"]))
    print(
        "  flow: Rich Text translated per block into de-DE/fr; embeds/unknown/links kept; "
        "localized:false untouched; hash gate re-translates on an edit"
    )


def _drop_tokens(only_fragment=None):
    real = translate_mod.pretranslate_document

    def patched(session, doc_id, locale, *a, **kw):
        stats = real(session, doc_id, locale, *a, **kw)
        for sg in session.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if "⟦" in (sg.source_text or ""):
                sg.target_text = "tokens lost here"
                session.add(sg)
        session.commit()
        return stats

    return real, patched


def test_flow_token_loss_holds_pair():
    # CREATE: nothing written (no mangled tree, no partial locale), no hash, reported held
    fs = _fs()
    c = _conn("rt loss create", {"target_locales": "de-DE"})
    real, patched = _drop_tokens()
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        _ok, msg, s = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real)
    f = fs.entries["e1"]["fields"]
    assert not fs.puts, ("no PUT on a guard failure at create", fs.puts)
    assert "de-DE" not in f["body"] and "de-DE" not in f["title"], f
    assert not _hashes(c.id), _hashes(c.id)
    assert s["held_back"] == 1 and "Held back" in msg, (s, msg)
    # healthy retry creates it
    ok2, msg2, s2 = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
    assert ok2 and s2["created"] == 1, (msg2, s2)
    assert fs.entries["e1"]["fields"]["body"]["de-DE"]["nodeType"] == "document"
    assert _hashes(c.id).get("e1:de-de")

    # UPDATE: stored Rich Text value kept, plain title still updates, no hash, incomplete
    old = {"nodeType": "document", "data": {}, "content": [_blk("paragraph", [_t("Alter Text")])]}
    fs = _fs(de={"title": "Alter Titel", "body": old})
    c = _conn("rt loss update", {"target_locales": "de-DE"})
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        _ok, _msg, s = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real)
    f = fs.entries["e1"]["fields"]
    assert f["body"]["de-DE"] == old, ("the stored Rich Text is kept", f["body"]["de-DE"])
    assert f["title"]["de-DE"] != "Alter Titel", f["title"]
    assert not _hashes(c.id), _hashes(c.id)
    inc = s["incomplete"]
    assert inc and inc[0]["held_fields"] == ["body"], inc
    assert "placeholders" in inc[0]["reasons"]["body"], inc
    ok2, msg2, s2 = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
    assert ok2 and s2["skipped"] == 0 and not s2["incomplete"], (msg2, s2)
    assert fs.entries["e1"]["fields"]["body"]["de-DE"] != old
    print(
        "  flow: token loss at create writes nothing; at update keeps the stored tree; "
        "neither records a hash; a healthy retry completes"
    )


def test_665_validation_hold_records_no_hash():
    fs = FakeContentful()
    fs.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"size": {"max": 12}}],
            },
            {"id": "body", "type": "Text", "localized": True},
        ],
        display="title",
    )
    fs.add_entry(
        "e1",
        "blogPost",
        {
            "title": {"en-US": "Hello there", "de-DE": "Alter Titel"},
            "body": {"en-US": "Some prose.", "de-DE": "Alter Text"},
        },
    )
    c = _conn("665", {"target_locales": "de-DE"})
    calls = {"n": 0}
    real = translate_mod.pretranslate_document

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    set_pieces("app.services.translate", "pretranslate_document", counting)
    try:
        ok, msg, s = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
        f = fs.entries["e1"]["fields"]
        assert ok and s["updated"] == 1, (msg, s)
        assert f["body"]["de-DE"] != "Alter Text", ("the valid field is written", f["body"])
        assert f["title"]["de-DE"] == "Alter Titel", ("the invalid field is held", f["title"])
        assert s["incomplete"] and s["incomplete"][0]["held_fields"] == ["title"], s["incomplete"]
        assert not _hashes(c.id), ("#665: no content hash after a validation hold", _hashes(c.id))
        cfg = cstore.get(c.id).config or {}
        assert "e1:de-de" in (cfg.get("contentful_blocked") or {}), ("parked + listed", cfg)
        assert "e1:de-de" in (cfg.get("contentful_blocked_src") or {}), (
            "parked for this hash",
            cfg,
        )
        # Owner decision 2026-10-10: still not done, but the deterministic hold must not
        # re-spend MT on every scheduled run while the source is unchanged.
        ok2, msg2, s2 = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
        assert ok2 and s2["parked"] == 1 and calls["n"] == 1, ("parked, no new MT", calls, s2)
        assert "NOT DONE" in msg2, msg2
        assert not _hashes(c.id), ("#665: still no hash after the parked run", _hashes(c.id))
        # a source edit changes the hash, so the pair is re-attempted (and still held)
        fs.entries["e1"]["fields"]["body"]["en-US"] = "Some other prose."
        _ok2b, _m, s2b = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
        assert s2b["parked"] == 0 and calls["n"] == 2, ("source edit re-attempts", calls, s2b)
        assert not _hashes(c.id), _hashes(c.id)
        # once the field fits, an explicit retry completes it, records the hash, clears the park
        fs.content_types["blogPost"]["fields"][0]["validations"] = []
        cstore.retry_failed(c.id, s2b["incomplete"], actor="auto", transport=fs.transport())
        assert calls["n"] == 3, ("the retry re-attempts the parked pair", calls)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real)
    assert fs.entries["e1"]["fields"]["title"]["de-DE"] != "Alter Titel", fs.entries["e1"]
    assert _hashes(c.id).get("e1:de-de"), _hashes(c.id)
    cfg = cstore.get(c.id).config or {}
    assert not cfg.get("contentful_blocked") and not cfg.get("contentful_blocked_src"), cfg
    print(
        "  665: update with one over-limit field writes the rest, keeps the old value, records "
        "no hash, is parked without MT re-spend, retry completes and records the hash"
    )


def test_rich_unknown_nodes_untouched():
    """Unknown block types are never entered; an unknown inline rides along verbatim."""
    unknown_block = {
        "nodeType": "x-callout",
        "data": {"tone": "warn"},
        "content": [_blk("paragraph", [_t("Do not translate me")])],
    }
    unknown_inline = {"nodeType": "x-mention", "data": {"id": 7}, "content": [_t("@zed")]}
    doc = _blk(
        "document",
        [
            unknown_block,
            _blk("paragraph", [_t("Hello "), copy.deepcopy(unknown_inline), _t(" world")]),
        ],
    )
    src = copy.deepcopy(doc)
    units = rt.extract(doc)
    assert [u.text for u in units] == ["Hello ⟦x1⟧ world"], [u.text for u in units]
    out, bad = rt.rebuild(doc, {0: "Hallo ⟦x1⟧ Welt"})
    assert out is not None and not bad, bad
    assert out["content"][0] == src["content"][0], "unknown block must be byte-identical"
    assert _nodes(out, "x-mention")[0] == unknown_inline
    assert rt.rebuild(doc, {0: "Hallo Welt"})[0] is None, "a dropped unknown inline is refused"
    print("  walker: unknown block types untouched, unknown inline kept verbatim")

    # nothing translatable (empty paragraph, embeds, hr, digits): no units, and the connector
    # never writes the field, so the stored document stays byte-identical
    nothing = _blk(
        "document",
        [
            _blk("paragraph", [_t("")]),
            copy.deepcopy(EMBED_BLOCK),
            copy.deepcopy(EMBED_ASSET),
            _blk("hr", []),
            _blk("paragraph", [_t("2026")]),
            _blk("paragraph", [copy.deepcopy(EMBED_INLINE)]),
        ],
    )
    assert rt.extract(nothing) == []
    stored = rich_doc()
    fs = _fs(extra={"body": {"en-US": nothing, "de-DE": stored}})
    c = _conn("rt-nothing", {"target_locales": "de-DE"})
    ok, msg, s = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
    assert ok and "body" not in s["fields_selected"], (msg, s)
    assert fs.entries["e1"]["fields"]["body"]["de-DE"] == stored, "stored value untouched"
    print("  flow: a Rich Text field with nothing translatable is skipped, target untouched")


def test_gate_findings():
    """Review gate (rt-1010): sibling inline reorder is legal, a malformed child never
    crashes the walker, and an incomplete write drops an older stored hash."""
    # 1. A target language may put an inline entry before a link (CJK word order).
    doc = _blk(
        "document",
        [_blk("paragraph", [_t("See "), copy.deepcopy(LINK), _t(" and "), EMBED_INLINE, _t(".")])],
    )
    units = rt.extract(doc)
    assert len(units) == 1 and "x3" in units[0].text, [u.text for u in units]
    out, bad = rt.rebuild(doc, {0: "⟦x3⟧と⟦g1⟧⟦g2⟧料金⟦/g2⟧ページ⟦/g1⟧を参照。"})
    assert out is not None, ("reordered siblings rebuild", bad)
    para = out["content"][0]["content"]
    assert para[0] == EMBED_INLINE and para[2]["data"] == LINK["data"], para
    # ... but a duplicated inline entry still holds the field
    out2, _ = rt.rebuild(doc, {0: "⟦x3⟧⟦x3⟧⟦g1⟧⟦g2⟧a⟦/g2⟧b⟦/g1⟧"})
    assert out2 is None, out2

    # 2. A malformed (non-object) child inside a paragraph rides along verbatim.
    bad_doc = _blk("document", [_blk("paragraph", [_t("Hello world"), "junk", None])])
    units = rt.extract(bad_doc)
    assert [u.text for u in units] == ["Hello world⟦x1⟧⟦x2⟧"], [u.text for u in units]
    out3, _ = rt.rebuild(bad_doc, {0: "Hallo Welt⟦x1⟧⟦x2⟧"})
    assert out3["content"][0]["content"][1:] == ["junk", None], out3

    # 3. An incomplete write drops an older stored hash (no stale "done").
    fs = FakeContentful()
    fs.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"size": {"max": 12}}],
            },
            {"id": "body", "type": "Text", "localized": True},
        ],
        display="title",
    )
    fs.add_entry(
        "e1",
        "blogPost",
        {
            "title": {"en-US": "Hello there", "de-DE": "Alter Titel"},
            "body": {"en-US": "Some prose.", "de-DE": "Alter Text"},
        },
    )
    c = _conn("gate-stale", {"target_locales": "de-DE"})
    cstore.patch_config_nested(c.id, {"contentful_hashes": {"add": {"e1:de-de": "older"}}})
    ok, msg, s = cstore.contentful_translate(c.id, actor="auto", transport=fs.transport())
    assert ok and s["updated"] == 1 and s["incomplete"], (msg, s)
    assert "e1:de-de" not in _hashes(c.id), ("stale hash dropped", _hashes(c.id))
    print("  gate: sibling reorder rebuilds, malformed child verbatim, stale hash dropped")


def main():
    init_db()
    test_walker()
    test_flow_create()
    test_flow_token_loss_holds_pair()
    test_665_validation_hold_records_no_hash()
    test_rich_unknown_nodes_untouched()
    test_gate_findings()
    print("verify_contentful_richtext OK")


if __name__ == "__main__":
    main()
