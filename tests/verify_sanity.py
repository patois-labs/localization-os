"""Sanity connector - offline verify suite (httpx.MockTransport fake of the Sanity HTTP API).

The connector supports BOTH Sanity localization models, so the fake models both:
  field model    sanity-plugin-internationalized-array: language items inside ONE document
  document model @sanity/document-internationalization: a document per language + a
                 translation.metadata document

TAXONOMY G - the fake is STRICT so the payload assertions are non-vacuous:
  * only the exact GROQ shapes the connector emits are answered (any other shape is a 400), so a
    query that drifts or a filter that leaks `versions.*` documents fails loudly
  * the host, API version path, Bearer token and `perspective=raw` are required
  * a create body that carries `_rev` / `_createdAt` / `_updatedAt` is rejected
  * a patch on a missing document, a `set` on a `_key` selector that matches nothing, and an
    insert that duplicates an array `_key` are errors
  * `ifRevisionID` mismatch -> 409 transactionConsistencyError; only the FIRST patch of a
    transaction may carry it for a document (a later one would see the bumped revision)
  * mutations are atomic (a failing one leaves the dataset untouched)

Covers the defect taxonomy (the connector build blueprint section 3): A scope/path escape,
B held-back never overwrites a stored translation, C draft/published steady state, D selection
visible in both directions, E round-robin across types, F no hash on a no-op, G strict fake,
H budget / egress / token never logged / rate-limit backoff / vendor error codes.

ASCII-only prints; final line is 'verify_sanity OK'.
"""

import contextlib
import copy
import json
import logging
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_sanity_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.filters import sanity_portable_text as pt
from app.main import app
from app.models import Connection, Segment, SegStatus
from app.services.connectors import base as cbase
from app.services.connectors import idmap
from app.services.connectors import store as cstore
from app.services.connectors.sanity import SanityConnector
from app.services.connectors.sanity_strategy import (
    SanityDocStrategy,
    SanityFieldStrategy,
    item_lang,
    plan_field_write,
    target_doc_id,
)

TOKEN = "skTESTTOKENsanity0123456789"  # pregate: allow-secret test fixture token
VER = "v2025-02-19"


# ============================================================================
# Fake Sanity: query + mutate endpoints over an in-memory dataset.
# ============================================================================
class FakeSanity:
    def __init__(self, project="proj1", dataset="production"):
        self.project = project
        self.dataset = dataset
        self.docs: dict = {}
        self.n = 0
        self.applied: list = []  # every ACCEPTED mutations list
        self.queries: list = []  # every query string
        self.conflict_next = 0  # bump the target doc's _rev before the next N guarded patches
        self.on_conflict = None  # callable(doc): the concurrent edit's content change
        self.rate_limit_once = False
        self.fail_status = None  # force this status on every mutate
        self.fail_replace = False  # 500 on any transaction holding createOrReplace (crash mid-fill)
        self.seen_auth: set = set()

    # ---- seeding
    def put(self, doc, *, bump=True):
        self.n += 1
        d = copy.deepcopy(doc)
        d["_rev"] = f"r{self.n}"
        d.setdefault("_createdAt", "2026-10-01T00:00:00Z")
        d["_updatedAt"] = f"2026-10-{(self.n % 28) + 1:02d}T10:00:00Z"
        self.docs[d["_id"]] = d
        return d

    def transport(self):
        return httpx.MockTransport(self._handle)

    # ---- helpers
    def _err(self, status, desc, typ="error"):
        return httpx.Response(status, json={"error": {"description": desc, "type": typ}})

    def _handle(self, req):
        if req.url.host != f"{self.project}.api.sanity.io":
            return httpx.Response(404, json={"error": {"description": "wrong host"}})
        self.seen_auth.add(req.headers.get("authorization", ""))
        if req.headers.get("authorization") != f"Bearer {TOKEN}":
            return self._err(401, "Unauthorized - Session not found")
        m = re.fullmatch(rf"/{VER}/data/(query|mutate)/{self.dataset}", req.url.path)
        if not m or req.method != "POST":
            return self._err(404, f"unexpected {req.method} {req.url.path}")
        if self.rate_limit_once and m.group(1) == "query":
            self.rate_limit_once = False
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        body = json.loads(req.content or b"{}")
        if m.group(1) == "query":
            if req.url.params.get("perspective") != "raw":
                return self._err(400, "this fake requires perspective=raw")
            return self._query(body.get("query", ""), body.get("params") or {})
        return self._mutate(body.get("mutations"))

    def _query(self, q, p):
        self.queries.append(q)
        docs = self.docs
        if q == "*[_id in [$p, $d]]":
            res = [d for i, d in docs.items() if i in (p["p"], p["d"])]
        elif q == "*[_id in $ids]":
            res = [d for i, d in docs.items() if i in p["ids"]]
        elif q == "count(*[_id in [$p, $d]])":
            res = len([i for i in docs if i in (p["p"], p["d"])])
        elif q == "count(*[_type in $t && !(_id in path('versions.**'))])":
            res = len(
                [
                    d
                    for i, d in docs.items()
                    if d["_type"] in p["t"] and not i.startswith("versions.")
                ]
            )
        elif q.startswith("*[_type == 'translation.metadata'"):
            if q != (
                "*[_type == 'translation.metadata' && $ref in translations[].value._ref]"
                "{_id, _rev, translations}"
            ):
                return self._err(400, "bad metadata query")
            res = []
            for d in docs.values():
                if d["_type"] == "translation.metadata" and any(
                    (e.get("value") or {}).get("_ref") == p["ref"]
                    for e in d.get("translations") or []
                ):
                    res.append({k: d[k] for k in ("_id", "_rev", "translations")})
        else:
            mm = re.fullmatch(
                r"\*\[_type == \$t && !\(_id in path\('versions\.\*\*'\)\)"
                r"(?: && (\w+) == \$src)?\] \| order\(_updatedAt desc, _id\) "
                r"\[(\d+)\.\.\.(\d+)\]\._id",
                q,
            )
            if not mm:
                return self._err(400, f"fake does not understand this GROQ: {q}")
            rows = [
                d
                for i, d in docs.items()
                if d["_type"] == p["t"]
                and not i.startswith("versions.")
                and (not mm.group(1) or d.get(mm.group(1)) == p.get("src"))
            ]
            rows.sort(key=lambda d: (d["_updatedAt"], d["_id"]), reverse=False)
            rows.sort(key=lambda d: d["_updatedAt"], reverse=True)
            res = [d["_id"] for d in rows][int(mm.group(2)) : int(mm.group(3))]
        return httpx.Response(200, json={"ms": 1, "query": q, "result": copy.deepcopy(res)})

    def _mutate(self, muts):
        if self.fail_status:
            return self._err(self.fail_status, "forced failure")
        if not isinstance(muts, list) or not muts:
            return self._err(400, "mutations required")
        if self.fail_replace and any("createOrReplace" in m for m in muts):
            return self._err(500, "internal error")
        work = copy.deepcopy(self.docs)
        n0 = self.n
        guarded = 0
        for m in muts:
            if len(m) != 1:
                return self._fail(work, n0, 400, "one operation per mutation")
            ((kind, pl),) = m.items()
            if kind in ("create", "createIfNotExists", "createOrReplace"):
                if not isinstance(pl, dict) or not pl.get("_id") or not pl.get("_type"):
                    return self._fail(work, n0, 400, "document needs _id and _type")
                bad = [k for k in ("_rev", "_createdAt", "_updatedAt") if k in pl]
                if bad:
                    return self._fail(work, n0, 400, f"system field(s) not allowed: {bad}")
                exists = pl["_id"] in work
                if kind == "createIfNotExists" and exists:
                    continue
                if kind == "create" and exists:
                    return self._fail(work, n0, 409, "Document by ID already exists")
                self.n += 1
                d = copy.deepcopy(pl)
                d["_rev"] = f"r{self.n}"
                d["_createdAt"] = work.get(pl["_id"], {}).get("_createdAt", "2026-10-09T00:00:00Z")
                d["_updatedAt"] = f"2026-10-{(self.n % 28) + 1:02d}T11:00:00Z"
                work[pl["_id"]] = d
            elif kind == "patch":
                did = pl.get("id")
                doc = work.get(did)
                if doc is None:
                    return self._fail(work, n0, 400, f"Document not found: {did}")
                if "ifRevisionID" in pl:
                    guarded += 1
                    if self.conflict_next > 0:
                        self.conflict_next -= 1
                        self.n += 1
                        work[did]["_rev"] = f"r{self.n}"  # an external edit landed first
                        if self.on_conflict:
                            self.on_conflict(work[did])  # ... and changed the document
                        self.docs[did] = copy.deepcopy(work[did])
                        doc = work[did]
                    if pl["ifRevisionID"] != doc["_rev"]:
                        return self._fail(
                            work,
                            n0,
                            409,
                            "Mutation(s) failed: Document revision ID mismatch",
                            "transactionConsistencyError",
                        )
                err = self._patch(doc, pl)
                if err:
                    return self._fail(work, n0, 400, err)
                self.n += 1
                doc["_rev"] = f"r{self.n}"
                doc["_updatedAt"] = f"2026-10-{(self.n % 28) + 1:02d}T12:00:00Z"
            else:
                return self._fail(work, n0, 400, f"unknown mutation {kind}")
        self.docs = work
        self.applied.append(copy.deepcopy(muts))
        return httpx.Response(200, json={"transactionId": "t", "results": []})

    def _fail(self, work, n0, status, desc, typ="error"):
        return self._err(status, desc, typ)

    def _patch(self, doc, pl):
        known = {"id", "ifRevisionID", "set", "insert"}
        if set(pl) - known:
            return f"unknown patch op(s): {sorted(set(pl) - known)}"
        for path, val in (pl.get("set") or {}).items():
            m = re.fullmatch(r'(\w+)\[_key=="([A-Za-z0-9_\-]+)"\]\.value', path)
            if m:
                arr = doc.get(m.group(1))
                item = next((i for i in arr or [] if i.get("_key") == m.group(2)), None)
                if item is None:
                    return f"set path matches nothing: {path}"
                item["value"] = val
            elif re.fullmatch(r"\w+", path):
                doc[path] = val
            else:
                return f"bad set path {path!r}"
        ins = pl.get("insert")
        if ins:
            m = re.fullmatch(r"(\w+)\[-1\]", ins.get("after", ""))
            if not m or not isinstance(doc.get(m.group(1)), list):
                return f"bad insert target {ins.get('after')!r}"
            arr = doc[m.group(1)]
            arr.extend(copy.deepcopy(ins["items"]))
            keys = [i.get("_key") for i in arr]
            if len(keys) != len(set(keys)):
                return "duplicate _key in array"
        return None


# ============================================================================
# Seed helpers
# ============================================================================
def _item(typ, lang, val, *, v5=True):
    if v5:
        return {"_key": f"k{lang}", "_type": typ, "language": lang, "value": val}
    return {"_key": lang, "_type": typ, "value": val}


def _pt(text="Read the docs now.", link=True):
    spans = [{"_type": "span", "_key": "s1", "text": "Read ", "marks": []}]
    if link:
        spans.append(
            {"_type": "span", "_key": "s2", "text": "the docs", "marks": ["strong", "lk1"]}
        )
    spans.append({"_type": "span", "_key": "s3", "text": " now.", "marks": []})
    return [
        {
            "_type": "block",
            "_key": "b1",
            "style": "normal",
            "markDefs": [{"_key": "lk1", "_type": "link", "href": "https://acme.com/docs"}],
            "children": spans,
        },
        {"_type": "image", "_key": "img1", "asset": {"_ref": "image-abc-100x100-png"}},
    ]


def _field_doc(did="post1", typ="post", draft=False, v5=True, extra=None):
    d = {
        "_id": ("drafts." if draft else "") + did,
        "_type": typ,
        "title": [_item("internationalizedArrayStringValue", "en", "Hello world", v5=v5)],
        "body": [_item("internationalizedArrayBodyValue", "en", _pt(), v5=v5)],
        "slug": [_item("internationalizedArrayStringValue", "en", "hello-world", v5=v5)],
        "summary": [_item("internationalizedArrayStringValue", "en", "", v5=v5)],
        "views": [_item("internationalizedArrayNumberValue", "en", 42, v5=v5)],
        "subtitle": "Plain unlocalised subtitle",
        "internalCount": 7,
    }
    d.update(extra or {})
    return d


def _doc_model_doc(did="art1", typ="article", lang="en", draft=False, extra=None):
    d = {
        "_id": ("drafts." if draft else "") + did,
        "_type": typ,
        "language": lang,
        "title": "Doc level title",
        "body": _pt("Doc body copy."),
        "slug": {"_type": "slug", "current": "doc-level-title"},
        "category": "news",
        "rank": 3,
    }
    d.update(extra or {})
    return d


CFG_FIELD = {
    "project_id": "proj1",
    "dataset": "production",
    "i18n_model": "field",
    "document_types": "post",
    "languages": "en, fr, de",
    "source_locale": "en",
    "target_locales": "fr, de",
}
CFG_DOC = {
    "project_id": "proj1",
    "dataset": "production",
    "i18n_model": "document",
    "document_types": "article",
    "languages": "en, fr, de",
    "source_locale": "en",
    "target_locales": "fr",
}


def _conn(name, cfg=None, base=None):
    c = dict(base or CFG_FIELD)
    c.update(cfg or {})
    return cstore.create("sanity", name, c, {"api_token": TOKEN}, actor="admin")


def _cc(fs, cfg=None, base=None):
    c = dict(base or CFG_FIELD)
    c.update(cfg or {})
    return SanityConnector(c, {"api_token": TOKEN}, transport=fs.transport())


def _run(cid, fs, **kw):
    return cstore.sanity_translate(cid, actor="auto", transport=fs.transport(), **kw)


def _flip(mod, predicate):
    orig = mod.pretranslate_document

    def patched(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if predicate(sg):
                sg.status = SegStatus.NEEDS_REVIEW
                s.add(sg)
        s.commit()
        return out

    return orig, patched


def _lang_items(doc, fld):
    return {(i.get("language") or i["_key"]): i["value"] for i in doc[fld]}


def _raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except cbase.ConnectorError:
        return True
    return False


class _LogCap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.msgs = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.msgs.append(record.getMessage())


def main() -> None:
    import app.services.translate as T
    from app.services.connectors import egress_guard
    from app.web._routes import walk_routes

    init_db()
    cap = _LogCap()
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.DEBUG)

    # ---- (1) capability, strategies, test(), locale resolve ----------------------------
    assert SanityConnector.capability.topology == "T1"
    assert SanityConnector.capability.rich_text == "portable_text"
    fs = FakeSanity()
    fs.put(_field_doc())
    cc = _cc(fs)
    assert isinstance(cc.strategy, SanityFieldStrategy) and cc.strategy.topology == "T1"
    assert isinstance(_cc(fs, base=CFG_DOC).strategy, SanityDocStrategy)
    ok, msg = cc.test()
    assert ok and "field model" in msg and "1 document" in msg, msg
    assert TOKEN not in msg
    assert cc.enabled_locales() == ["en", "fr", "de"]
    assert _cc(fs, {"languages": "en, pt-BR"}).resolve_locale("pt-br") == "pt-BR"
    assert cc._source_locale() == "en"
    bad_tok = SanityConnector(dict(CFG_FIELD), {"api_token": "wrong"}, transport=fs.transport())
    ok2, msg2 = bad_tok.test()
    assert not ok2 and "401" in msg2 and "wrong" not in msg2, msg2
    print("  connector: T1/T3 strategies by model, test() ok + 401 message, locale resolve")

    # ---- (2) A: scope / path escape ---------------------------------------------------
    for key, bad in (
        ("project_id", "../evil"),
        ("project_id", "p.q"),
        ("project_id", "Proj"),
        ("project_id", "p\r\nHost: x"),
        ("dataset", "production/../x"),
        ("dataset", "a b"),
        ("api_version", "v1/../x"),
        ("document_types", "post, translation.metadata"),
        ("document_types", "post, drafts.post"),
        ("document_types", "post, .."),
        ("document_types", "post/x"),
        ("languages", "en, fr/../x"),
        ("language_field", "lang]; drop"),
        ("i18n_model", "magic"),
    ):
        cx = _cc(fs, {key: bad}, base=CFG_DOC)
        raised = False
        try:
            cx._host()
            cx._dataset()
            cx._version()
            cx._types()
            cx._languages()
            cx._lang_field()
            cx._model()
        except cbase.ConnectorError:
            raised = True
        assert raised, ("unsafe operator config must be refused", key, bad)
    fsA = FakeSanity()
    fsA.put(_field_doc())
    fsA.put(_field_doc(did="secret1", typ="secretType"))
    fsA.put({"_id": "post2x", "_type": "post2", "title": "prefix"})  # type PREFIXED by 'post'
    ca = _cc(fsA)
    for hostile in (
        "..",
        "../x",
        "%2e%2e%2f",
        "post1/../secret1",
        "post1?x=1",
        "post1#f",
        "post1\x00",
        "post1\r\nX: y",
        "pöst1",
        "",
        "a b",
        "versions.rel.post1",
    ):
        assert _raises(ca.discover_articles, article_ids=[hostile]) or hostile == "", hostile
    assert _raises(ca.discover_articles, article_ids=["secret1"]), "type out of scope refused"
    assert _raises(ca.discover_articles, article_ids=["post2x"]), "prefix type refused"
    assert not fsA.applied, "A: zero external writes for any refused id"
    assert [a["id"] for a in ca.discover_articles(article_ids=["drafts.post1"])] == ["post1"]
    # remote-supplied _key cannot enter a patch path
    evil = _field_doc(did="evil1")
    evil["title"].append(_item("internationalizedArrayStringValue", "fr", "old", v5=True))
    evil["title"][1]["_key"] = 'k"]; unset(title'
    ops, dropped, _u = plan_field_write(evil, "fr", {"title": "neu"})
    assert not ops and "title" in dropped and "unsafe _key" in dropped["title"], (ops, dropped)
    # review fix: a dropped field fails the whole pair closed (no silent partial write + hash)
    fsE = FakeSanity()
    evil2 = _field_doc(did="evil2")
    evil2["title"].append(_item("internationalizedArrayStringValue", "fr", "old", v5=True))
    evil2["title"][1]["_key"] = 'k"]; unset(title'
    fsE.put(evil2)
    ce = _cc(fsE)
    wr = ce.strategy.write_target(ce, "evil2", "fr", {"title": "neu", "summary": "Zusammen"})
    assert not wr.ok and "cannot write safely" in wr.message, wr
    assert not any("patch" in m for muts in fsE.applied for m in muts), "no patch applied"
    # review fix: a field name that is not a plain identifier never reaches a `set` path
    odd = _doc_model_doc(did="odd1", extra={"a.b": "text here", "x[0]": "more text"})
    _p, _r, sk = _cc(fsE, base=CFG_DOC)._select(odd)
    assert "a.b" in sk and "x[0]" in sk and "a.b" not in _p and "x[0]" not in _p, (_p, sk)
    print(
        "  A: unsafe project/dataset/version/type/language config refused; hostile ids, wrong type,"
        " prefix type refused with no writes; remote _key never reaches a path"
    )

    # ---- (3) D: selection both directions, reported ---------------------------------
    fsD = FakeSanity()
    fsD.put(_field_doc())
    cd = _cc(fsD)
    doc = cd.snapshot("post1")["doc"]
    plain, rich, skipped = cd._select(doc)
    assert set(plain) == {"title"} and set(rich) == {"body"}, (plain, rich)
    assert "slug" in skipped and "looks non-translatable" in skipped["slug"], skipped
    assert "summary" in skipped and "no non-empty" in skipped["summary"], skipped
    assert "views" in skipped and "unsupported value" in skipped["views"], skipped
    assert "subtitle" in skipped and "not an internationalized array" in skipped["subtitle"], (
        skipped
    )
    assert "internalCount" not in skipped, "an unrelated number is not noise"
    ca2 = _cc(fsD, {"fields": "title, slug, subtitle, nope"})
    p2, r2, s2 = ca2._select(doc)
    assert set(p2) == {"title", "slug"} and not r2, (p2, r2)
    assert "subtitle" in s2 and "nope" in s2, s2
    assert set(_cc(fsD, {"exclude_fields": "title"})._select(doc)[0]) == set()
    assert cd.translated_field_ids(doc) == {"title", "body", "summary"}, cd.translated_field_ids(
        doc
    )
    # document model heuristics
    dm = _doc_model_doc()
    cdm = _cc(fsD, base=CFG_DOC)
    p3, r3, s3 = cdm._select(dm)
    assert set(p3) == {"title"} and set(r3) == {"body"}, (p3, r3)
    assert "category" in s3 and "enum-like" in s3["category"], s3
    assert "slug" in s3 and "unsupported" in s3["slug"] and "rank" in s3, s3
    assert "language" not in s3 and "language" not in p3
    p4, _r4, _s4 = _cc(fsD, {"fields": "title, category"}, base=CFG_DOC)._select(dm)
    assert set(p4) == {"title", "category"}, "allowlist bypasses the enum heuristic"
    print(
        "  D: junk skipped AND real copy kept, both models; every skip reported with a reason;"
        " allowlist overrides the heuristics"
    )

    # ---- (4) field model CREATE: draft made, language items inserted, source untouched ----
    fs4 = FakeSanity()
    fs4.put(_field_doc())  # published only
    pub_before = copy.deepcopy(fs4.docs["post1"])
    c4 = _conn("create")
    ok, msg, s4 = _run(c4.id, fs4)
    assert ok, msg
    assert s4["created"] == 2 and s4["updated"] == 0, s4  # fr + de
    assert "drafts.post1" in fs4.docs, "C: write lands on the draft"
    assert fs4.docs["post1"] == pub_before, "published document byte-identical (never published)"
    dr = fs4.docs["drafts.post1"]
    for loc in ("fr", "de"):
        t = _lang_items(dr, "title")
        assert t[loc].strip() and t[loc] != "Hello world", (loc, t)
        b = _lang_items(dr, "body")[loc]
        assert pt.is_portable_text(b) and any(x.get("_type") == "image" for x in b), b
        link_span = [c for blk in b if blk.get("children") for c in blk["children"] if c["marks"]]
        assert link_span and link_span[0]["marks"] == ["strong", "lk1"], "marks kept verbatim"
        assert b[0]["markDefs"] == _pt()[0]["markDefs"], "link annotation untouched"
    assert _lang_items(dr, "title")["en"] == "Hello world", "source item preserved"
    assert _lang_items(dr, "slug") == {"en": "hello-world"}, "skipped slug untouched"
    assert dr["subtitle"] == "Plain unlocalised subtitle" and dr["internalCount"] == 7
    assert "summary" in s4["fields_skipped"] and "title" in s4["fields_selected"], s4
    for muts in fs4.applied:
        for m in muts:
            if "patch" in m:
                assert m["patch"]["id"].startswith("drafts."), "never patches published"
    # every item has the plugin v5 shape (language field, typed)
    for it in dr["title"]:
        assert it["_type"] == "internationalizedArrayStringValue" and "language" in it
    n_applied = len(fs4.applied)
    print(
        f"  field CREATE: draft from published, fr+de items inserted (typed, v5 shape), source/"
        f"slug/others untouched, published byte-identical ({n_applied} transactions)"
    )

    # ---- (5) C: second run steady state, then update on source change ------------------
    q_before = len(fs4.applied)
    calls = {"n": 0}
    real_pt = T.pretranslate_document

    def _counting(s, doc_id, loc, *a, **k):
        calls["n"] += 1
        return real_pt(s, doc_id, loc, *a, **k)

    set_pieces("app.services.translate", "pretranslate_document", _counting)
    try:
        ok, msg, s5 = _run(c4.id, fs4)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_pt)
    assert ok and s5["skipped"] == 2 and s5["created"] == 0 and s5["updated"] == 0, (msg, s5)
    assert len(fs4.applied) == q_before and calls["n"] == 0, "C: no write, no MT on run 2"
    # source edit on the draft -> hash changes -> the existing fr/de items are UPDATED by _key
    d4 = fs4.docs["drafts.post1"]
    d4["title"][0]["value"] = "Hello brave world"
    fs4.put(d4)
    ok, msg, s5b = _run(c4.id, fs4)
    assert ok and s5b["updated"] == 2 and s5b["created"] == 0, (msg, s5b)
    t = _lang_items(fs4.docs["drafts.post1"], "title")
    assert len(fs4.docs["drafts.post1"]["title"]) == 3, "no duplicate item minted"
    assert t["fr"] != "Hello world" and t["en"] == "Hello brave world", t
    print(
        "  C: run 2 fully suppressed (no write, no MT); a source edit UPDATES the existing items"
        " by _key, no duplicates"
    )

    # ---- (6) legacy plugin v4 (_key == language) ---------------------------------------
    fs6 = FakeSanity()
    fs6.put(_field_doc(v5=False))
    c6 = _conn("legacy", {"target_locales": "fr"})
    ok, msg, s6 = _run(c6.id, fs6)
    assert ok and s6["created"] == 1, (msg, s6)
    items = fs6.docs["drafts.post1"]["title"]
    assert [i["_key"] for i in items] == ["en", "fr"] and "language" not in items[1], items
    print("  field model: plugin v4 shape (language in _key) read and written in kind")

    # ---- (7) B/F: held-back never overwrites; #640 no create with a hole; no hash -----------
    fs7 = FakeSanity()
    d7 = _field_doc(draft=True)
    d7["title"].append(_item("internationalizedArrayStringValue", "fr", "Menschentitel", v5=True))
    d7["body"].append(_item("internationalizedArrayBodyValue", "fr", _pt("Alte Prosa", link=False)))
    fs7.put(d7)
    c7 = _conn("held-title", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: sg.order_index == 0)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s7 = _run(c7.id, fs7)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    dr7 = fs7.docs["drafts.post1"]
    assert _lang_items(dr7, "title")["fr"] == "Menschentitel", "B: held title keeps stored value"
    assert _lang_items(dr7, "body")["fr"] != _pt("Alte Prosa", link=False), "body still updates"
    assert s7["updated"] == 1 and s7["held_back"] == 1, s7
    assert "post1:fr" not in ((cstore.get(c7.id).config or {}).get("sanity_hashes") or {}), (
        "#640: partial write records no hash"
    )
    ok, msg, s7b = _run(c7.id, fs7)  # healthy run retries and writes the title
    assert ok and s7b["updated"] == 1, (msg, s7b)
    assert _lang_items(fs7.docs["drafts.post1"], "title")["fr"] != "Menschentitel"
    # F: everything held back -> no write, no hash, re-attempted later
    fs7f = FakeSanity()
    fs7f.put(_field_doc(draft=True))
    c7f = _conn("all-held", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: True)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, _s7f = _run(c7f.id, fs7f)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert not fs7f.applied, ("F: all held -> nothing written", fs7f.applied)
    assert not ((cstore.get(c7f.id).config or {}).get("sanity_hashes") or {}), "F: no hash"
    ok, msg, s7g = _run(c7f.id, fs7f)
    assert ok and s7g["created"] == 1, (msg, s7g)
    # #640: a NEW language is not created while one field is held
    fs7h = FakeSanity()
    fs7h.put(_field_doc(draft=True))
    c7h = _conn("hole", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: sg.order_index == 0)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s7h = _run(c7h.id, fs7h)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert not fs7h.applied and s7h["held_back"] == 1 and s7h["created"] == 0, (msg, s7h)
    assert "fr" not in _lang_items(fs7h.docs["drafts.post1"], "title"), "no half-built language"
    print(
        "  B/F/#640: held title keeps the stored value, body updates, no hash on partial; all-held"
        " writes nothing; a new language is not created with a hole"
    )

    # ---- (8) revision conflict: refetch + re-plan; exhaustion aborts without a hash --------
    fs8 = FakeSanity()
    fs8.put(_field_doc(draft=True))
    fs8.conflict_next = 1
    c8 = _conn("conflict", {"target_locales": "fr"})
    slept = []
    rs = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        ok, msg, s8 = _run(c8.id, fs8)
        assert ok and s8["created"] == 1, (msg, s8)
        fs8b = FakeSanity()
        fs8b.put(_field_doc(draft=True))
        fs8b.conflict_next = 99
        c8b = _conn("conflict-always", {"target_locales": "fr"})
        ok, msg, _s8b = _run(c8b.id, fs8b)
    finally:
        cbase._sleep = rs
    assert not ok and "revision conflict" in msg and not fs8b.applied, (ok, msg)
    assert not ((cstore.get(c8b.id).config or {}).get("sanity_hashes") or {}), "no hash"
    assert sum(slept) <= 40, ("backoff bounded", slept)
    print(
        "  conflict: stale revision -> refetch and retry succeeds; endless conflict aborts, no hash"
    )

    # ---- (8b) Review gate: a concurrent edit that ADDS and EDITS items between plan and write ----
    # The retry must re-plan against the fresh document: the source entry, the concurrently added
    # language and the edited item survive in their stored order; only fr is added, at the end.
    fs8c = FakeSanity()
    d8c = _field_doc(draft=True)
    d8c["title"].append(_item("internationalizedArrayStringValue", "de", "Hallo Welt"))
    fs8c.put(d8c)

    def _concurrent(doc):
        doc["title"].append(_item("internationalizedArrayStringValue", "es", "Hola mundo"))
        doc["title"][1]["value"] = "Hallo Welt (Redaktion)"
        doc["title"][0]["value"] = "Hello world"  # source untouched by the other editor

    fs8c.conflict_next = 1
    fs8c.on_conflict = _concurrent
    c8c = _cc(fs8c, {"languages": "en, fr, de, es", "target_locales": "fr"})
    rs = cbase._sleep
    cbase._sleep = lambda s: None
    try:
        wr8c = c8c.strategy.write_target(c8c, "post1", "fr", {"title": "Bonjour le monde"})
    finally:
        cbase._sleep = rs
    assert wr8c.ok, wr8c
    got = [(item_lang(i), i["value"], i["_key"]) for i in fs8c.docs["drafts.post1"]["title"]]
    assert [g[:2] for g in got[:3]] == [
        ("en", "Hello world"),
        ("de", "Hallo Welt (Redaktion)"),
        ("es", "Hola mundo"),
    ], got
    assert got[3][:2] == ("fr", "Bonjour le monde") and len(got) == 4, got
    assert [g[2] for g in got[:3]] == ["ken", "kde", "kes"], "stored _keys unchanged"
    print(
        "  conflict retry re-plans on the fresh revision: source + concurrently added/edited items"
        " kept in order, fr appended once"
    )

    # ---- (8c) Review gate: a language that is already PUBLISHED gets a draft overlay only ------
    fs8d = FakeSanity()
    pub8 = _field_doc()
    pub8["title"].append(_item("internationalizedArrayStringValue", "fr", "Bonjour (publie)"))
    fs8d.put(pub8)
    pub8_before = copy.deepcopy(fs8d.docs["post1"])
    c8d = _conn("live-field", {"target_locales": "fr"})
    ok, msg, s8d = _run(c8d.id, fs8d)
    assert ok and s8d["updated"] == 1, (msg, s8d)
    assert fs8d.docs["post1"] == pub8_before, "published document byte-identical"
    assert _lang_items(fs8d.docs["drafts.post1"], "title")["fr"] != "Bonjour (publie)"
    assert s8d.get("live_kept") == [{"article_id": "post1", "locale": "fr"}], s8d
    assert "already published" in msg and "draft only" in msg, msg
    assert "live_kept" not in s4 and "live_kept" not in s5b, "unpublished languages: no note"
    print("  field model: a published language is written to the draft only, reported live_kept")

    # ---- (9) Portable Text filter + rich failure ------------------------------------------
    blocks = _pt()
    units = pt.extract(blocks)
    assert len(units) == 1 and "⟦g1⟧the docs⟦/g1⟧" in units[0].text, units
    ok_doc, bad = pt.rebuild(blocks, {0: "Lisez ⟦g1⟧la doc⟦/g1⟧ maintenant."})
    assert ok_doc and not bad
    assert ok_doc[1] == blocks[1], "image block verbatim"
    marked = [c for c in ok_doc[0]["children"] if c["marks"]]
    assert marked and marked[0]["text"] == "la doc" and marked[0]["_key"] == "s2"
    keys = [c["_key"] for c in ok_doc[0]["children"]]
    assert len(keys) == len(set(keys)), "span keys unique"
    for broken in (
        "Lisez la doc maintenant.",  # tokens dropped
        "⟦g1⟧a⟦g1⟧b⟦/g1⟧",  # duplicated
        "⟦/g1⟧x⟦g1⟧",  # crossed
        "⟦g1⟧⟦g2⟧z⟦/g2⟧⟦/g1⟧",  # added / nested
    ):
        nd, b2 = pt.rebuild(blocks, {0: broken})
        assert nd is None and b2, broken
    assert pt.rebuild(blocks, {})[0] is None, "a missing translation holds the field"
    assert blocks == _pt(), "source never mutated"
    code = [
        {
            "_type": "block",
            "_key": "c",
            "children": [{"_type": "span", "_key": "x", "text": "a⟦b", "marks": []}],
        }
    ]
    assert pt.extract(code) == [], "text containing the placeholder brackets is left verbatim"
    # flow level: placeholders lost -> field held, stored value kept, pair retried
    fs9 = FakeSanity()
    d9 = _field_doc(draft=True)
    d9["body"].append(
        _item("internationalizedArrayBodyValue", "fr", _pt("Texte stocke", link=False))
    )
    d9["title"].append(_item("internationalizedArrayStringValue", "fr", "Titre stocke", v5=True))
    fs9.put(d9)
    c9 = _conn("guard", {"target_locales": "fr", "fields": "title, body"})
    real_rebuild = pt.rebuild
    pt.rebuild = lambda b, t: (None, [0])
    try:
        import app.services.connectors.sanity as _san

        _san.pt.rebuild = pt.rebuild
        ok, msg, _s9 = _run(c9.id, fs9)
    finally:
        pt.rebuild = real_rebuild
        _san.pt.rebuild = real_rebuild
    assert ok, msg
    assert _lang_items(fs9.docs["drafts.post1"], "body")["fr"] == _pt("Texte stocke", link=False), (
        "guard failure keeps the stored Portable Text"
    )
    assert "post1:fr" not in ((cstore.get(c9.id).config or {}).get("sanity_hashes") or {})
    print(
        "  Portable Text: block walker keeps marks/links/images, rejects broken placeholders;"
        " a guard failure holds the field and keeps the stored value"
    )

    # ---- (10) document model: create, link, steady state, update, rule 6, orphan -------------
    fs10 = FakeSanity()
    fs10.put(_doc_model_doc())
    fs10.put(_doc_model_doc(did="art1", draft=True, extra={"title": "Doc level title"}))
    fs10.put(
        {"_id": "versions.rel1.art1", "_type": "article", "language": "en", "title": "release copy"}
    )
    c10 = _conn("docmodel", base=CFG_DOC)
    ok, msg, s10 = _run(c10.id, fs10)
    assert ok and s10["created"] == 1 and s10["entries"] == 1, (msg, s10)
    tpid = target_doc_id("art1", "fr")
    tgt = fs10.docs["drafts." + tpid]
    assert tgt["language"] == "fr" and tgt["_type"] == "article", tgt
    assert tgt["title"] != "Doc level title" and tgt["title"].strip()
    assert tgt["category"] == "news" and tgt["slug"] == {
        "_type": "slug",
        "current": "doc-level-title",
    }
    assert pt.is_portable_text(tgt["body"]) and tgt["body"][0]["children"]
    assert tpid not in fs10.docs, "the translation is a DRAFT only"
    metas = [d for d in fs10.docs.values() if d["_type"] == "translation.metadata"]
    assert len(metas) == 1, metas
    ent = {e["language"]: e["value"] for e in metas[0]["translations"]}
    assert ent["en"]["_ref"] == "art1" and ent["fr"]["_ref"] == tpid, ent
    assert ent["fr"]["_weak"] is True and ent["fr"]["_strengthenOnPublish"] == {"type": "article"}
    row = idmap.get(c10.id, "art1", "fr")
    assert row and row.status == idmap.DONE and row.target_id == tpid, row
    for muts in fs10.applied:
        for m in muts:
            for body in m.values():
                did = body.get("id") or body.get("_id")
                assert did.startswith(("drafts.", "l10n-meta-")), ("only drafts + metadata", did)
    # no 'versions.' document is ever a source
    assert all(
        "release copy" not in json.dumps(d) or d["_id"].startswith("versions.")
        for d in fs10.docs.values()
        if d["_id"] != "versions.rel1.art1"
    )
    n_before = len(fs10.applied)
    set_pieces("app.services.translate", "pretranslate_document", _counting)
    calls["n"] = 0
    try:
        ok, msg, s10b = _run(c10.id, fs10)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_pt)
    assert ok and s10b["skipped"] == 1 and len(fs10.applied) == n_before and calls["n"] == 0, s10b
    # human edits the translation's category; then the source changes: only translated fields move
    td = fs10.docs["drafts." + tpid]
    td["category"] = "human-edited"
    fs10.put(td)
    sd = fs10.docs["drafts.art1"]
    sd["title"] = "Doc level title v2"
    fs10.put(sd)
    ok, msg, s10c = _run(c10.id, fs10)
    assert ok and s10c["updated"] == 1 and s10c["created"] == 0, (msg, s10c)
    td2 = fs10.docs["drafts." + tpid]
    assert td2["category"] == "human-edited" and td2["title"] != tgt["title"], td2
    assert len([d for d in fs10.docs.values() if d["_type"] == "translation.metadata"]) == 1
    # rule 3: translation deleted in the Studio -> recreated under the same id, no dup link
    del fs10.docs["drafts." + tpid]
    sd = fs10.docs["drafts.art1"]
    sd["title"] = "Doc level title v3"
    fs10.put(sd)
    ok, msg, s10d = _run(c10.id, fs10)
    assert ok and s10d["created"] == 1, (msg, s10d)
    assert "drafts." + tpid in fs10.docs
    metas = [d for d in fs10.docs.values() if d["_type"] == "translation.metadata"]
    assert len(metas) == 1 and len(metas[0]["translations"]) == 2, metas
    print(
        "  document model: translation draft + metadata link + id map; versions.* ignored; run 2"
        " suppressed; update touches only translated fields; deleted target recreated"
    )

    # ---- (11) rule 6: a translation we do not own is left alone, no MT spent -----------------
    fs11 = FakeSanity()
    fs11.put(_doc_model_doc())
    fs11.put(
        _doc_model_doc(did="human-fr", lang="fr", extra={"title": "Titre ecrit par un humain"})
    )
    fs11.put(
        {
            "_id": "meta-human",
            "_type": "translation.metadata",
            "schemaTypes": ["article"],
            "translations": [
                {
                    "_key": "ken",
                    "_type": "internationalizedArrayReferenceValue",
                    "language": "en",
                    "value": {"_type": "reference", "_ref": "art1", "_weak": True},
                },
                {
                    "_key": "kfr",
                    "_type": "internationalizedArrayReferenceValue",
                    "language": "fr",
                    "value": {"_type": "reference", "_ref": "human-fr", "_weak": True},
                },
            ],
        }
    )
    c11 = _conn("rule6", base=CFG_DOC)
    set_pieces("app.services.translate", "pretranslate_document", _counting)
    calls["n"] = 0
    try:
        ok, msg, s11 = _run(c11.id, fs11)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_pt)
    assert ok and s11["existing_untouched"] == 1 and s11["created"] == 0, (msg, s11)
    assert calls["n"] == 0, "no machine translation spent on a translation we do not own"
    assert not fs11.applied and fs11.docs["human-fr"]["title"] == "Titre ecrit par un humain"
    assert "untouched" in msg and idmap.get(c11.id, "art1", "fr") is None, "no needs_link row"
    assert not ((cstore.get(c11.id).config or {}).get("sanity_hashes") or {})
    # the human deletes theirs and unlinks it: the next run proceeds
    del fs11.docs["meta-human"]
    ok, msg, s11b = _run(c11.id, fs11)
    assert ok and s11b["created"] == 1, (msg, s11b)
    print(
        "  rule 6: a linked translation we did not create is untouched, zero MT, no needs_link row;"
        " it proceeds once the foreign link is gone"
    )

    # write-time backstop of the same rule (a race between the pre-check and the write)
    from app.services.connectors.sanity_strategy import EXISTING_PREFIX

    fs11 = FakeSanity()
    fs11.put(_doc_model_doc())
    fs11.put(
        {
            "_id": "meta-human2",
            "_type": "translation.metadata",
            "translations": [
                {
                    "_key": "a",
                    "_type": "internationalizedArrayReferenceValue",
                    "language": "en",
                    "value": {"_type": "reference", "_ref": "art1"},
                },
                {
                    "_key": "b",
                    "_type": "internationalizedArrayReferenceValue",
                    "language": "de",
                    "value": {"_type": "reference", "_ref": "human-de"},
                },
            ],
        }
    )
    ccw = _cc(fs11, base=CFG_DOC)
    ccw.connection_id = c11.id
    fs11.put(_doc_model_doc(did="human-de", lang="de", extra={"title": "Deutscher Titel"}))
    n_app = len(fs11.applied)
    wr = ccw.strategy.write_target(ccw, "art1", "de", {"title": "x", "_source": "art1"})
    assert not wr.ok and wr.message.startswith(EXISTING_PREFIX), wr
    assert len(fs11.applied) == n_app, "the backstop writes nothing"
    # crash mid-fill: the reserved row stays 'building'; the next run RESUMES (no second document)
    fs11b = FakeSanity()
    fs11b.put(_doc_model_doc())
    c11b = _conn("resume", base=CFG_DOC)
    fs11b.fail_replace = True
    rs = cbase._sleep
    cbase._sleep = lambda s: None
    try:
        ok, msg, _s11c = _run(c11b.id, fs11b)
    finally:
        cbase._sleep = rs
    assert not ok, msg
    rowb = idmap.get(c11b.id, "art1", "fr")
    assert rowb and rowb.status == idmap.BUILDING, rowb
    fs11b.fail_replace = False
    ok, msg, s11d = _run(c11b.id, fs11b)
    assert ok and s11d["created"] == 1, (msg, s11d)
    assert idmap.get(c11b.id, "art1", "fr").status == idmap.DONE
    l10n = [i for i in fs11b.docs if i.startswith(("drafts.l10n-", "l10n-")) and "meta" not in i]
    assert l10n == ["drafts." + target_doc_id("art1", "fr")], ("exactly one translation doc", l10n)
    print("  rule 6 backstop at write time; crash mid-fill resumes the reserved row, one document")

    # ---- (11c) Review gate: document model with a PUBLISHED translation --------------------------
    # (a) owned + published (editor published it): a source change updates the DRAFT only.
    fs11p = FakeSanity()
    fs11p.put(_doc_model_doc())
    c11p = _conn("doc-live", base=CFG_DOC)
    ok, msg, _ = _run(c11p.id, fs11p)
    assert ok
    tp = target_doc_id("art1", "fr")
    published = dict(fs11p.docs.pop("drafts." + tp))
    published["_id"] = tp
    fs11p.put(published)  # the editor published the translation
    live_before = copy.deepcopy(fs11p.docs[tp])
    src = fs11p.docs["art1"]
    src["title"] = "Doc level title v2"
    fs11p.put(src)
    ok, msg, s11p = _run(c11p.id, fs11p)
    assert ok and s11p["updated"] == 1, (msg, s11p)
    assert fs11p.docs[tp] == live_before, "the live translation is byte-identical"
    assert fs11p.docs["drafts." + tp]["title"] != live_before["title"]
    assert s11p.get("live_kept") == [{"article_id": "art1", "locale": "fr"}], s11p
    assert "already published" in msg, msg
    # (b) lost id map + published translation carrying an editor's own non-translated fields:
    # the draft is built from the PUBLISHED translation, never from the source document.
    fs11q = FakeSanity()
    fs11q.put(_doc_model_doc())
    fs11q.put(
        _doc_model_doc(
            did=tp,
            lang="fr",
            extra={
                "title": "Titre publie",
                "category": "actualites",
                "slug": {"_type": "slug", "current": "titre-publie"},
            },
        )
    )
    live_q = copy.deepcopy(fs11q.docs[tp])
    c11q = _conn("doc-lost-map", base=CFG_DOC)
    ok, msg, s11q = _run(c11q.id, fs11q)
    assert ok and s11q["created"] == 1, (msg, s11q)
    assert fs11q.docs[tp] == live_q, "the live translation is byte-identical"
    dq = fs11q.docs["drafts." + tp]
    assert dq["category"] == "actualites" and dq["slug"]["current"] == "titre-publie", (
        "no source-language field copied over a published translation",
        dq,
    )
    assert dq["title"] != "Titre publie" and dq["language"] == "fr", dq
    assert s11q.get("live_kept") == [{"article_id": "art1", "locale": "fr"}], s11q
    for muts in fs11q.applied:
        for m in muts:
            for body in m.values():
                did = body.get("id") or body.get("_id")
                assert did.startswith(("drafts.", "l10n-meta-")), ("only drafts + metadata", did)
    print(
        "  document model: a published translation is never changed; the draft overlay is built"
        " from it (not the source) and reported live_kept"
    )

    # ---- (12) document model: held title -> no create (#640); explicit id of a non-source -----
    fs12 = FakeSanity()
    fs12.put(_doc_model_doc())
    c12 = _conn("docheld", base=CFG_DOC)
    orig, patched = _flip(T, lambda sg: sg.order_index == 0)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s12 = _run(c12.id, fs12)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert not fs12.applied and s12["held_back"] == 1, (msg, s12)
    ok, msg, s12b = _run(c12.id, fs12)
    assert ok and s12b["created"] == 1
    fs12.put({"_id": "de1", "_type": "article", "language": "de", "title": "Deutsch"})
    assert _raises(_cc(fs12, base=CFG_DOC).discover_articles, article_ids=["de1"]), (
        "only source-language documents are translated"
    )
    print("  document model: held field -> no translation document created; non-source id refused")

    # ---- (13) E: shared cap round-robin across types ----------------------------------------
    fs13 = FakeSanity()
    for i in range(6):
        fs13.put(_field_doc(did=f"a{i}", typ="post"))
    for i in range(6):
        fs13.put(_field_doc(did=f"b{i}", typ="page"))
    cc13 = _cc(fs13, {"document_types": "post, page", "max_entries": "4"})
    arts = cc13.discover_articles()
    kinds = [a["_type"] for a in arts]
    assert len(arts) == 4 and kinds.count("post") == 2 and kinds.count("page") == 2, kinds
    print("  E: cap of 4 across 2 types -> 2 each (distribution asserted, not just the total)")

    # ---- (14) H: WriteBudget, egress guard, token never logged, vendor errors, rate limit --------
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fs14 = FakeSanity()
        fs14.put(_field_doc(draft=True))
        c14 = _conn("budget")
        ok, msg, s14 = _run(c14.id, fs14)
        assert ok and s14["ceiling_hit"] and (s14["created"] + s14["updated"]) == 1, s14
        assert len(fs14.applied) == 1
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    fs15 = FakeSanity()
    fs15.put(_field_doc(draft=True))
    c15 = _conn("egress")
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s15 = _run(c15.id, fs15)
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fs15.applied, ("mock MT + real egress -> blocked, no writes", msg)
    # vendor error codes surface as per-pair failures, not crashes
    for code in (403, 400, 500):
        fsE = FakeSanity()
        fsE.put(_field_doc(draft=True))
        fsE.fail_status = code
        cE = _conn(f"err{code}", {"target_locales": "fr"})
        rs = cbase._sleep
        cbase._sleep = lambda s: None
        try:
            ok, msg, _sE = _run(cE.id, fsE)
        finally:
            cbase._sleep = rs
        assert not ok and str(code) in msg, (code, msg)
        assert not ((cstore.get(cE.id).config or {}).get("sanity_hashes") or {})
    # 429 honours Retry-After through base._request
    fs16 = FakeSanity()
    fs16.put(_field_doc(draft=True))
    fs16.rate_limit_once = True
    slept = []
    rs = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        ok, msg, s16 = _run(_conn("ratelimit", {"target_locales": "fr"}).id, fs16)
    finally:
        cbase._sleep = rs
    assert ok and s16["created"] == 1 and slept and slept[0] == 2.0, (msg, slept)
    # the token never reaches a log line, a run message, a failure row or a result
    blob = " | ".join(cap.msgs)
    assert TOKEN not in blob, "token leaked into a log message"
    assert TOKEN not in json.dumps(s14) + json.dumps(s16) + msg
    assert fs16.seen_auth == {f"Bearer {TOKEN}"}
    print(
        "  H: WriteBudget stops at 1; mock-egress guard blocks; 403/400/500 become failures with"
        " no hash; 429 honours Retry-After; token never logged"
    )

    # ---- (15) parity surfaces + org isolation + CLI ----------------------------------------
    from app.services.connectors import REGISTRY

    assert "sanity" in REGISTRY
    c = TestClient(app)
    assert "Sanity" in c.get("/connectors").text
    typ = c.get("/connectors/sanity").text
    assert "Project ID" in typ and "Create" in typ
    from app.cli.__main__ import build_parser

    assert "connector sanity-translate" in _leaf_paths(build_parser())
    from starlette.routing import Route

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/sanity/translate" in api_paths
    from tests._api_fixture import user_api_token

    hdr = {"X-Loc-Token": user_api_token()}
    r_ghost = c.post("/api/connectors/99999999/sanity/translate", json={}, headers=hdr)
    assert r_ghost.status_code == 404, r_ghost.status_code
    own = _conn("api")
    r_own = c.post(
        f"/api/connectors/{own.id}/sanity/translate", json={"article_ids": []}, headers=hdr
    )
    assert r_own.status_code in (200, 400), r_own.status_code
    with Session(engine) as s:
        assert s.get(Connection, own.id).kind == "sanity"
    print("  parity: REGISTRY + tile + type form + /api route + CLI leaf; foreign cid -> 404")

    logging.getLogger().removeHandler(cap)
    print("verify_sanity OK")


def _leaf_paths(parser, prefix=""):
    import argparse

    out = set()
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        return {prefix} if prefix else set()
    for sp in subs:
        for name, subparser in sp.choices.items():
            out |= _leaf_paths(subparser, f"{prefix} {name}".strip())
    return out


if __name__ == "__main__":
    main()
