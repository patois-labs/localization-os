"""Intercom rebuild (un-archived): the defects the first build could not see.

A STRICT fake workspace (the Strapi fixture-dodge lesson): the article PUT REPLACES the
whole translated_content map, answers 400 for a read-only block key or a non-integer
author_id, 404 for a path that is not a numeric article id, and the list endpoint pages with
a cursor across two Help Centers. Covers:
  - taxonomy A: caller-supplied ids (.., %2e%2e, ?, #, CR/LF, NUL, unicode digits) never
    reach the wire; an id of another Help Center answers like a missing one
  - taxonomy B/F (#640): a held body keeps the stored translation, an all-held pair writes
    and records nothing, a held new locale is not created
  - taxonomy C/E: steady state on run two writes nothing; help_center_id is applied while
    paging (not after the cap); cursor pages walk
  - the whole-map PUT strips read-only keys and sends an integer author_id
  - the token never appears in a message, log or error; hostile api_version refused
  - un-archived: in the active set

ASCII-only prints; final line is 'verify_intercom_rebuild OK'.
"""

import json
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_icr_")

import httpx
from sqlmodel import select

from app.db import init_db
from app.models import Segment, SegStatus
from app.services import connectors as reg
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.intercom import IntercomConnector

TOKEN = "tok-SECRET-9f3a"
READ_ONLY = {
    "created_at",
    "updated_at",
    "url",
    "has_unpublished_changes",
    "draft_updated_at",
    "created_by_id",
    "updated_by_id",
}


class StrictIntercom:
    def __init__(self, articles, per_page_cap=2):
        self.articles = {str(a["id"]): a for a in articles}
        self.order = [str(a["id"]) for a in articles]
        self.puts = []
        self.paths = []
        self.per_page_cap = per_page_cap
        self.disabled = set()  # languages not enabled on the Help Center: silently dropped

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, req):
        path = req.url.path
        self.paths.append(req.url.raw_path.decode())
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        if req.method == "GET" and path == "/articles":
            q = dict(req.url.params)
            pp = min(int(q.get("per_page", 25)), self.per_page_cap)
            start = int(q.get("starting_after") or 0)
            ids = self.order[start : start + pp]
            nxt = start + pp
            pages = {"type": "pages", "next": None}
            if nxt < len(self.order):
                pages["next"] = {"page": nxt, "starting_after": str(nxt)}
            return httpx.Response(
                200,
                json={
                    "type": "list",
                    "total_count": len(self.order),
                    "pages": pages,
                    "data": [self.articles[i] for i in ids],
                },
            )
        parts = path.split("/")
        if len(parts) == 3 and parts[1] == "articles":
            aid = parts[2]
            if not aid.isdigit():
                return httpx.Response(404, json={"errors": [{"code": "not_found"}]})
            art = self.articles.get(aid)
            if req.method == "GET":
                return httpx.Response(200, json=art) if art else httpx.Response(404, json={})
            if req.method == "PUT":
                body = json.loads(req.content)
                tc = body.get("translated_content") or {}
                for _key, blk in tc.items():
                    bad = READ_ONLY & set(blk)
                    if bad:
                        return httpx.Response(400, json={"errors": [{"code": "read_only"}]})
                    if "author_id" in blk and not isinstance(blk["author_id"], int):
                        return httpx.Response(400, json={"errors": [{"code": "author_type"}]})
                self.puts.append((aid, json.loads(json.dumps(tc))))
                # REPLACE, like the strictest reading; a disabled language is dropped silently
                art["translated_content"] = {k: v for k, v in tc.items() if k not in self.disabled}
                return httpx.Response(200, json=art)
        return httpx.Response(404, json={})


def _art(aid, hc="1", tc=None, title="Reset password", body="<h1>Reset</h1><p>Click the link.</p>"):
    return {
        "id": str(aid),
        "type": "article",
        "title": title,
        "description": "How to reset",
        "body": body,
        "author_id": "55",
        "state": "published",
        "default_locale": "en",
        "updated_at": 1_700_000_000,
        "help_center_id": hc,
        "translated_content": tc or {"type": "article_translated_content"},
    }


def _conn(name, extra=None):
    cfg = {"region": "US", "source_locale": "en", "target_locales": "de", "author_id": "55"}
    cfg.update(extra or {})
    return cstore.create("intercom", name, cfg, {"access_token": TOKEN}, actor="admin")


def _flip(only_locale=None, only_last=True):
    """Patch pretranslate so the chosen segments of `only_locale` become NEEDS_REVIEW."""
    import app.services.translate as tm

    real = tm.pretranslate_document

    def patched(session, doc_id, locale, spec=None, force_pii=False):
        stats = real(session, doc_id, locale, spec, force_pii=force_pii)
        if only_locale in (None, locale):
            q = (
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == locale)
                .order_by(Segment.order_index.desc())
            )
            segs = q.limit(1) if only_last else q
            for seg in session.exec(segs).all():
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
            session.commit()
        return stats

    set_pieces("app.services.translate", "pretranslate_document", patched)
    return real


def _unflip(real):
    set_pieces("app.services.translate", "pretranslate_document", real)


def main():
    init_db()

    # ---- un-archived ----
    assert not reg.is_archived("intercom")
    assert "intercom" in {s.kind for s in reg.active_specs()}
    print("  registry: intercom is in the active set")

    # ---- A: hostile ids never reach the wire ----
    fi = StrictIntercom([_art("100")])
    ic = IntercomConnector({"region": "US"}, {"access_token": TOKEN}, transport=fi.transport())
    for bad in ["../me", "%2e%2e%2fadmin", "1?x=1", "1#f", "1\r\n2", "1\x002", "１２", "", "1/2"]:
        try:
            ic.discover_articles(article_ids=[bad])
        except ConnectorError:
            continue
        raise AssertionError(("hostile id accepted", bad))
    assert fi.paths == [], ("no request may leave for a hostile id", fi.paths)
    # an id of another Help Center answers like a missing one
    fi2 = StrictIntercom([_art("100", hc="1"), _art("101", hc="2")])
    ic2 = IntercomConnector(
        {"region": "US", "help_center_id": "1"}, {"access_token": TOKEN}, transport=fi2.transport()
    )
    got = ic2.discover_articles(article_ids=["100", "101"])
    assert [a["id"] for a in got] == ["100"], got
    print("  taxonomy A: hostile ids refused before any request; foreign Help Center id skipped")

    # ---- E: help_center_id applied while paging, cursor walk ----
    arts = [_art(str(i), hc="2") for i in range(200, 205)] + [_art("300", hc="1")]
    fi3 = StrictIntercom(arts, per_page_cap=2)
    ic3 = IntercomConnector(
        {"region": "US", "help_center_id": "1", "max_articles": "2"},
        {"access_token": TOKEN},
        transport=fi3.transport(),
    )
    got3 = ic3.discover_articles()
    assert [a["id"] for a in got3] == ["300"], ("scoped before the cap", got3)
    ic3b = IntercomConnector({"region": "US"}, {"access_token": TOKEN}, transport=fi3.transport())
    assert len(ic3b.discover_articles(cap=50)) == 6
    print("  taxonomy E: help_center_id filters while paging (cap cannot starve it); cursor walk")

    # ---- create + strict PUT shape + steady state (C) ----
    fi4 = StrictIntercom([_art("100")])
    c4 = _conn("IC strict")
    ok, msg, s = cstore.intercom_translate(c4.id, actor="auto", transport=fi4.transport())
    assert ok and s["created"] == 1, (msg, s)
    blk = fi4.articles["100"]["translated_content"]["de"]
    assert blk["author_id"] == 55 and "created_at" not in blk, blk
    fi4.puts.clear()
    ok, msg, s = cstore.intercom_translate(c4.id, actor="auto", transport=fi4.transport())
    assert ok and s["skipped"] == 1 and not fi4.puts, ("steady state writes nothing", s, fi4.puts)
    print("  taxonomy C: create sends integer author_id; run two is a no-op")

    # ---- whole-map PUT strips read-only keys of OTHER stored blocks and keeps them ----
    tc = {
        "type": "article_translated_content",
        "fr": {
            "type": "article_content",
            "title": "Salut",
            "body": "<p>Bonjour</p>",
            "author_id": "55",
            "state": "published",
            "created_at": 1,
            "updated_at": 2,
            "url": "http://x/fr",
            "has_unpublished_changes": False,
            "created_by_id": 9,
        },
    }
    fi5 = StrictIntercom([_art("100", tc=tc)])
    c5 = _conn("IC readonly")
    ok, msg, s = cstore.intercom_translate(c5.id, actor="auto", transport=fi5.transport())
    assert ok and s["created"] == 1, (msg, s)
    after = fi5.articles["100"]["translated_content"]
    assert after["fr"]["title"] == "Salut" and after["fr"]["state"] == "published", after
    print("  whole-map PUT: other locale preserved, read-only keys stripped, author coerced")

    # ---- B/F: a held body on an EXISTING variant keeps the stored body, no hash ----
    old = {
        "type": "article_translated_content",
        "de": {
            "type": "article_content",
            "title": "Alt Titel",
            "description": "Alte Beschreibung",
            "body": "<p>Alter Text</p>",
            "author_id": "55",
            "state": "draft",
        },
    }
    fi6 = StrictIntercom([_art("100", tc=old)])
    c6 = _conn("IC held update")
    real = _flip("de")
    try:
        ok, msg, s6 = cstore.intercom_translate(c6.id, actor="auto", transport=fi6.transport())
    finally:
        _unflip(real)
    de = fi6.articles["100"]["translated_content"]["de"]
    assert de["body"] == "<p>Alter Text</p>", ("held body keeps the stored translation", de)
    assert de["title"] != "Alt Titel", ("translated title still lands", de)
    assert de["state"] == "draft", ("existing state untouched", de)
    assert "100:de" not in ((cstore.get(c6.id).config or {}).get("intercom_hashes") or {}), (
        "no content hash for a partly held pair"
    )
    assert s6["held_back"] == 1 and ok, s6
    assert "re-attempted" in msg, msg
    print("  taxonomy B: held body omitted (stored body survives), no hash, reported held-back")

    # ---- B/F: ALL units held on an existing variant writes nothing ----
    fi7 = StrictIntercom([_art("100", tc=json.loads(json.dumps(old)))])
    c7 = _conn("IC all held")
    real = _flip("de", only_last=False)
    try:
        ok, msg, s7 = cstore.intercom_translate(c7.id, actor="auto", transport=fi7.transport())
    finally:
        _unflip(real)
    assert not fi7.puts, ("nothing translated -> nothing written", fi7.puts)
    assert fi7.articles["100"]["translated_content"]["de"]["body"] == "<p>Alter Text</p>"
    assert "100:de" not in ((cstore.get(c7.id).config or {}).get("intercom_hashes") or {})
    assert s7["held_back"] == 1, s7
    # ... and on a MISSING locale no empty block is created
    fi8 = StrictIntercom([_art("100")])
    c8 = _conn("IC all held new")
    real = _flip("de", only_last=False)
    try:
        cstore.intercom_translate(c8.id, actor="auto", transport=fi8.transport())
    finally:
        _unflip(real)
    assert not fi8.puts and "de" not in fi8.articles["100"]["translated_content"], fi8.puts
    print("  taxonomy F: an all-held pair writes nothing and records no hash (new or existing)")

    # ---- draft mode never writes into a PUBLISHED locale (Review gate 2026-10-10) ----
    import app.services.translate as tm

    real_ptd = tm.pretranslate_document
    mt_calls = []

    def counting(session, doc_id, locale, spec=None, force_pii=False):
        mt_calls.append(locale)
        return real_ptd(session, doc_id, locale, spec, force_pii=force_pii)

    live_tc = json.loads(json.dumps(old))
    live_tc["de"]["state"] = "published"
    live_de = json.loads(json.dumps(live_tc["de"]))
    fl = StrictIntercom([_art("100", tc=live_tc)])
    cl = _conn("IC live kept")
    set_pieces("app.services.translate", "pretranslate_document", counting)
    try:
        ok, msg, sl = cstore.intercom_translate(cl.id, actor="auto", transport=fl.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert not fl.puts, ("a published locale is never written in draft mode", fl.puts)
    assert fl.articles["100"]["translated_content"]["de"] == live_de
    assert not mt_calls, ("no MT is spent on a pair that cannot be written", mt_calls)
    assert [(f["article_id"], f["locale"]) for f in sl["live_kept"]] == [("100", "de")], sl
    assert "LIVE" in msg and "100/de" in msg and "Publish after write" in msg, msg
    assert "100:de" not in ((cstore.get(cl.id).config or {}).get("intercom_hashes") or {})
    ok, msg, sr = cstore.retry_failed(
        cl.id, [{"article_id": "100", "locale": "de", "error": "x"}], transport=fl.transport()
    )
    assert not fl.puts and sr["live_kept"][0]["error"].startswith("live-kept:"), (fl.puts, sr)
    print("  draft mode: a published locale is not written, no MT, reported live_kept (retry too)")
    fl.articles["100"]["translated_content"]["de"]["state"] = "draft"
    ok, msg, sl2 = cstore.intercom_translate(cl.id, actor="auto", transport=fl.transport())
    assert ok and sl2["updated"] == 1 and "live_kept" not in sl2, (msg, sl2)
    assert fl.articles["100"]["translated_content"]["de"]["state"] == "draft"
    flp = StrictIntercom([_art("100", tc=json.loads(json.dumps(live_tc)))])
    clp = _conn("IC live publish", {"publish": "1"})
    ok, msg, slp = cstore.intercom_translate(clp.id, actor="auto", transport=flp.transport())
    assert ok and slp["updated"] == 1 and "live_kept" not in slp, (msg, slp)
    assert flp.articles["100"]["translated_content"]["de"]["state"] == "published"
    print("  unpublished -> written as a draft next run; publish=1 updates the live locale")

    # ---- F: a locale Intercom silently ignored is a failure, never a recorded success ----
    fi9 = StrictIntercom([_art("100")])
    fi9.disabled = {"de"}
    c9 = _conn("IC ignored locale")
    ok, msg, s9 = cstore.intercom_translate(c9.id, actor="auto", transport=fi9.transport())
    assert s9["created"] == 0 and s9["failures"], s9
    assert "did not store" in s9["failures"][0]["error"], s9
    assert "100:de" not in ((cstore.get(c9.id).config or {}).get("intercom_hashes") or {})
    print("  taxonomy F: a silently ignored locale key is read back as a failure (no hash)")

    # ---- secrets: token never in messages, logs or errors ----
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = H()
    logging.getLogger().addHandler(h)
    try:
        bad = IntercomConnector(
            {"region": "US"}, {"access_token": TOKEN + "\r\nX: y"}, transport=fi.transport()
        )
        ok, m = bad.test()
        assert not ok and "SECRET" not in m, m
        v = IntercomConnector(
            {"region": "US", "api_version": "2.16\r\nX: y"},
            {"access_token": TOKEN},
            transport=fi.transport(),
        )
        ok, m = v.test()
        assert not ok and "version" in m.lower(), m

        def deny(req):
            return httpx.Response(401, json={"errors": [{"message": "bad token " + TOKEN}]})

        d = IntercomConnector(
            {"region": "US"}, {"access_token": TOKEN}, transport=httpx.MockTransport(deny)
        )
        ok, m = d.test()
        assert not ok and "401" in m, m
        assert all("SECRET" not in r for r in records), records
    finally:
        logging.getLogger().removeHandler(h)
    print("  secrets: bad token chars / hostile version refused, no token in messages or logs")

    print("verify_intercom_rebuild OK")


if __name__ == "__main__":
    main()
