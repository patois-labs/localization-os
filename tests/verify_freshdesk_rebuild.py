"""Freshdesk rebuild (un-archived): the defects the first build could not see.

A STRICT fake tenant: the translation PUT is a FULL replace that needs both title and
description (400 otherwise), language codes are case-sensitive as the portal lists them
('ru-RU'), a numeric-only article route 404s anything else, POST on an existing translation
answers 409, and folders are listed per folder with a page size. Covers:
  - taxonomy A: hostile article ids / folder ids never reach the wire; an article outside the
    configured folder answers like a missing one
  - taxonomy E: article discovery is spread across folders (the first folder cannot take the
    whole cap)
  - vendor language-code casing is written ('ru-RU'), not the lowercased internal form
  - taxonomy B/F (#640): a held body or title keeps the STORED translation on an update (never
    the source text), an all-held pair writes nothing, a held new language is not created, no
    content hash for any of them
  - draft mode never writes into a PUBLISHED translation (Review gate 2026-10-10): no MT is
    spent, nothing is written, no hash, the pair is reported as live_kept; unpublishing it
    or turning on publish lets it through; an explicit retry is refused at write time
  - steady state: run two writes nothing
  - the API key never appears in a message or log
  - un-archived: in the active set

ASCII-only prints; final line is 'verify_freshdesk_rebuild OK'.
"""

import base64
import json
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_fdr_")

import httpx
from sqlmodel import select

from app.db import init_db
from app.models import Segment, SegStatus
from app.services import connectors as reg
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.freshdesk import FreshdeskConnector

KEY = "fdkey-SECRET-77"


class StrictFreshdesk:
    def __init__(self, articles, folders, supported=("fr", "ru-RU"), page=2):
        # articles: {id: {...master..., folder_id}}, folders: {folder_id: [article ids]}
        self.articles = {str(a["id"]): a for a in articles}
        self.folders = folders
        self.supported = list(supported)
        self.translations = {}
        self.writes = []
        self.paths = []
        self.page = page

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _paged(self, req, rows):
        q = dict(req.url.params)
        per = min(int(q.get("per_page", 30)), self.page)
        pg = int(q.get("page", 1))
        return httpx.Response(200, json=rows[(pg - 1) * per : pg * per])

    def _handle(self, req):
        path = req.url.path
        self.paths.append(req.url.raw_path.decode())
        want = "Basic " + base64.b64encode(f"{KEY}:X".encode()).decode()
        assert req.headers["authorization"] == want
        pfx = "/api/v2"
        if path == f"{pfx}/settings/helpdesk":
            return httpx.Response(
                200, json={"primary_language": "en", "supported_languages": self.supported}
            )
        if path == f"{pfx}/solutions/categories":
            return httpx.Response(200, json=[{"id": "1", "name": "Cat"}])
        if path == f"{pfx}/solutions/categories/1/folders":
            return self._paged(req, [{"id": f} for f in self.folders])
        if path.startswith(f"{pfx}/solutions/folders/") and path.endswith("/subfolders"):
            return httpx.Response(200, json=[])
        if path.startswith(f"{pfx}/solutions/folders/") and path.endswith("/articles"):
            fid = path.split("/folders/")[1].split("/")[0]
            return self._paged(req, [{"id": a} for a in self.folders.get(fid, [])])
        parts = path[len(f"{pfx}/solutions/articles/") :].split("/")
        if path.startswith(f"{pfx}/solutions/articles/"):
            aid = parts[0]
            if not aid.isdigit():
                return httpx.Response(404, json={})
            if len(parts) == 1 and req.method == "GET":
                art = self.articles.get(aid)
                return httpx.Response(200, json=art) if art else httpx.Response(404, json={})
            if len(parts) == 2:
                lang = parts[1]
                if lang not in self.supported:
                    return httpx.Response(400, json={"code": "invalid_lang"})
                cur = self.translations.get((aid, lang))
                if req.method == "GET":
                    return httpx.Response(200, json=cur) if cur else httpx.Response(404, json={})
                body = json.loads(req.content)
                if not body.get("title") and body.get("title") != "":
                    return httpx.Response(400, json={"code": "missing_title"})
                if "description" not in body or body["description"] is None:
                    return httpx.Response(400, json={"code": "missing_description"})
                if req.method == "POST":
                    if cur:
                        return httpx.Response(409, json={})
                    self.writes.append(("POST", aid, lang, body))
                    self.translations[(aid, lang)] = {"id": aid, **body}
                    return httpx.Response(201, json=self.translations[(aid, lang)])
                if req.method == "PUT":
                    if not cur:
                        return httpx.Response(404, json={})
                    self.writes.append(("PUT", aid, lang, body))
                    self.translations[(aid, lang)] = {"id": aid, **body}  # FULL replace
                    return httpx.Response(200, json=self.translations[(aid, lang)])
        return httpx.Response(404, json={})


def _art(aid, folder="10", title="Reset password", body="<h1>Reset</h1><p>Click the link.</p>"):
    return {
        "id": str(aid),
        "title": title,
        "description": body,
        "status": 2,
        "folder_id": folder,
        "category_id": "1",
        "updated_at": "2026-10-01T00:00:00Z",
    }


def _conn(name, extra=None):
    cfg = {"domain": "acme", "target_locales": "fr"}
    cfg.update(extra or {})
    return cstore.create("freshdesk", name, cfg, {"api_key": KEY}, actor="admin")


def _flip(only_last=True):
    import app.services.translate as tm

    real = tm.pretranslate_document

    def patched(session, doc_id, locale, spec=None, force_pii=False):
        stats = real(session, doc_id, locale, spec, force_pii=force_pii)
        q = (
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == locale)
            .order_by(Segment.order_index.desc())
        )
        for seg in session.exec(q.limit(1) if only_last else q).all():
            seg.status = SegStatus.NEEDS_REVIEW
            session.add(seg)
        session.commit()
        return stats

    set_pieces("app.services.translate", "pretranslate_document", patched)
    return real


def _unflip(real):
    set_pieces("app.services.translate", "pretranslate_document", real)


def _hashes(cid):
    return (cstore.get(cid).config or {}).get("fd_hashes") or {}


def _tr(title, desc, status=1):
    return {"title": title, "description": desc, "status": status}


def main():
    init_db()

    assert not reg.is_archived("freshdesk")
    assert "freshdesk" in {s.kind for s in reg.active_specs()}
    print("  registry: freshdesk is in the active set")

    # ---- A: hostile ids never reach the wire; out-of-scope article skipped ----
    fx = StrictFreshdesk([_art("100")], {"10": ["100"]})
    fc = FreshdeskConnector({"domain": "acme"}, {"api_key": KEY}, transport=fx.transport())
    for bad in ["../x", "%2e%2e%2f", "1?a=b", "1#f", "1\r\n2", "1\x002", "１", "", "1/fr"]:
        try:
            fc.discover_articles(article_ids=[bad])
        except ConnectorError:
            continue
        raise AssertionError(("hostile id accepted", bad))
    for bad in ["../x", "1/2", "1?x"]:
        try:
            FreshdeskConnector(
                {"domain": "acme", "folder_id": bad}, {"api_key": KEY}, transport=fx.transport()
            ).discover_articles()
        except ConnectorError:
            continue
        raise AssertionError(("hostile folder id accepted", bad))
    assert [p for p in fx.paths if "solutions" in p] == [], fx.paths
    fy = StrictFreshdesk([_art("100", "10"), _art("101", "11")], {"10": ["100"], "11": ["101"]})
    scoped = FreshdeskConnector(
        {"domain": "acme", "folder_id": "10"}, {"api_key": KEY}, transport=fy.transport()
    )
    assert [a["id"] for a in scoped.discover_articles(article_ids=["100", "101"])] == ["100"]
    print("  taxonomy A: hostile ids refused before any request; other-folder id skipped")

    # ---- E: round-robin across folders under the cap ----
    arts = [_art(str(i), "10") for i in range(200, 205)] + [_art("300", "11")]
    fz = StrictFreshdesk(arts, {"10": [str(i) for i in range(200, 205)], "11": ["300"]})
    cap_c = FreshdeskConnector(
        {"domain": "acme", "max_articles": "3"}, {"api_key": KEY}, transport=fz.transport()
    )
    got = [a["id"] for a in cap_c.discover_articles()]
    assert "300" in got and len(got) == 3, ("second folder gets a share of the cap", got)
    print("  taxonomy E: discovery spreads the cap across folders", got)

    # ---- vendor casing, create, steady state ----
    f1 = StrictFreshdesk([_art("100")], {"10": ["100"]})
    c1 = _conn("FD casing", {"target_locales": "ru-RU"})
    ok, msg, s = cstore.freshdesk_translate(c1.id, actor="auto", transport=f1.transport())
    assert ok and s["created"] == 1, (msg, s)
    assert f1.writes[0][2] == "ru-RU", ("vendor casing on the wire", f1.writes)
    assert f1.writes[0][3]["status"] == 1
    f1.writes.clear()
    ok, msg, s = cstore.freshdesk_translate(c1.id, actor="auto", transport=f1.transport())
    assert ok and s["skipped"] == 1 and not f1.writes, ("steady state", s, f1.writes)
    print("  vendor language casing written (ru-RU); run two is a no-op")

    # ---- draft mode never writes into a PUBLISHED translation (Review gate) ----
    import app.services.translate as tm

    real_ptd = tm.pretranslate_document
    mt_calls = []

    def counting(session, doc_id, locale, spec=None, force_pii=False):
        mt_calls.append(locale)
        return real_ptd(session, doc_id, locale, spec, force_pii=force_pii)

    f2 = StrictFreshdesk([_art("100")], {"10": ["100"]})
    live = {"id": "100", **_tr("Titre humain", "<p>Texte publie</p>", 2)}
    f2.translations[("100", "fr")] = dict(live)
    c2 = _conn("FD live kept")
    set_pieces("app.services.translate", "pretranslate_document", counting)
    try:
        ok, msg, s = cstore.freshdesk_translate(c2.id, actor="auto", transport=f2.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert not f2.writes and f2.translations[("100", "fr")] == live, ("live untouched", f2.writes)
    assert not mt_calls, ("no MT is spent on a pair that cannot be written", mt_calls)
    assert s["updated"] == 0 and s["created"] == 0, s
    assert [(f["article_id"], f["locale"]) for f in s["live_kept"]] == [("100", "fr")], s
    assert "LIVE" in msg and "100/fr" in msg and "Publish after write" in msg, msg
    assert "100:fr" not in _hashes(c2.id), "a live-kept pair is not done: no hash"
    print("  draft mode: a published translation is not written, no MT, reported live_kept")
    # an explicit retry bypasses the pre-MT hook; the write-time guard still refuses it
    ok, msg, sr = cstore.retry_failed(
        c2.id, [{"article_id": "100", "locale": "fr", "error": "x"}], transport=f2.transport()
    )
    assert not f2.writes and f2.translations[("100", "fr")] == live, f2.writes
    assert sr["live_kept"] and sr["live_kept"][0]["error"].startswith("live-kept:"), sr
    assert "100:fr" not in _hashes(c2.id), "no hash after a refused retry"
    print("  explicit retry: refused at write time (live text kept), reported live_kept")
    # unpublished in Freshdesk -> the next run writes it, as a draft
    f2.translations[("100", "fr")]["status"] = 1
    ok, msg, s = cstore.freshdesk_translate(c2.id, actor="auto", transport=f2.transport())
    assert ok and s["updated"] == 1 and "live_kept" not in s, (msg, s)
    assert f2.translations[("100", "fr")]["status"] == 1, "an existing draft stays a draft"
    assert "100:fr" in _hashes(c2.id)
    # publish ON updates the live translation (the operator chose to publish)
    f2b = StrictFreshdesk([_art("100")], {"10": ["100"]})
    f2b.translations[("100", "fr")] = dict(live)
    c2b = _conn("FD live publish", {"publish": "1"})
    ok, msg, s = cstore.freshdesk_translate(c2b.id, actor="auto", transport=f2b.transport())
    assert ok and s["updated"] == 1 and "live_kept" not in s, (msg, s)
    assert f2b.translations[("100", "fr")]["status"] == 2
    assert f2b.translations[("100", "fr")]["description"] != "<p>Texte publie</p>"
    print("  unpublished -> written as a draft next run; publish=1 updates the live translation")

    # ---- B: held body on an existing translation keeps the stored translation ----
    f3 = StrictFreshdesk([_art("100")], {"10": ["100"]})
    f3.translations[("100", "fr")] = {"id": "100", **_tr("Alt Titel", "<p>Ancien texte</p>", 1)}
    c3 = _conn("FD held body")
    real = _flip(only_last=True)
    try:
        ok, msg, s3 = cstore.freshdesk_translate(c3.id, actor="auto", transport=f3.transport())
    finally:
        _unflip(real)
    cur = f3.translations[("100", "fr")]
    assert cur["description"] == "<p>Ancien texte</p>", ("held body keeps stored", cur)
    assert cur["title"] != "Alt Titel" and cur["status"] == 1, cur
    assert "100:fr" not in _hashes(c3.id), "no hash for a partly held pair"
    assert s3["held_back"] == 1 and ok and "re-attempted" in msg, (s3, msg)
    print("  taxonomy B: held body keeps the stored description (never source text), no hash")

    # ---- B: all held on existing -> nothing written; on missing -> not created ----
    f4 = StrictFreshdesk([_art("100")], {"10": ["100"]})
    f4.translations[("100", "fr")] = {"id": "100", **_tr("Alt", "<p>alt</p>", 1)}
    c4 = _conn("FD all held")
    real = _flip(only_last=False)
    try:
        cstore.freshdesk_translate(c4.id, actor="auto", transport=f4.transport())
    finally:
        _unflip(real)
    assert not f4.writes, ("all-held pair writes nothing", f4.writes)
    assert "100:fr" not in _hashes(c4.id)
    f5 = StrictFreshdesk([_art("100")], {"10": ["100"]})
    c5 = _conn("FD held new")
    real = _flip(only_last=True)
    try:
        ok, msg, s5 = cstore.freshdesk_translate(c5.id, actor="auto", transport=f5.transport())
    finally:
        _unflip(real)
    assert not f5.writes and ("100", "fr") not in f5.translations, f5.writes
    assert "100:fr" not in _hashes(c5.id) and s5["held_back"] == 1, s5
    print("  taxonomy F: all-held writes nothing; a held new language is not created; no hash")

    # ---- secrets ----
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = H()
    logging.getLogger().addHandler(h)
    try:

        def deny(req):
            return httpx.Response(401, json={"message": "bad key " + KEY})

        d = FreshdeskConnector(
            {"domain": "acme"}, {"api_key": KEY}, transport=httpx.MockTransport(deny)
        )
        ok, m = d.test()
        assert not ok and "401" in m and "SECRET" not in m, m
        assert all("SECRET" not in r for r in records), records
    finally:
        logging.getLogger().removeHandler(h)
    print("  secrets: no API key in messages or logs")

    print("verify_freshdesk_rebuild OK")


if __name__ == "__main__":
    main()
