"""Help Scout Docs connector — the SECOND T3 (document-per-locale) build — fully
offline via httpx.MockTransport (a fake Help Scout Docs account).

Help Scout is T3: no native localization, so a "translation" is a SEPARATE article
the connector creates in a mapped target-language COLLECTION and tracks in
ConnectorIdMap via strategy.DocPerLocaleStrategy (reserve-then-fill + the 7-case
reconciliation state table — the SAME machinery Notion built). Write-back rides the
shared flow engine with ZERO engine edits. Body is HTML -> the existing `html` filter
(no walker, unlike Notion).

Covers (SPEC-t3-notion-helpscout Tests list + the orchestrator brief):
  - full helpscout_translate flow: one article per locale in the mapped collection,
    done id-map rows, translated HTML body + name written to the target
  - resumable build (THE critical one): a crash after reserve (fill/PUT fails) -> retry
    PUTs the SAME target, never double-creates (one ConnectorIdMap row, one target id)
  - reconciliation rules 1-7 (inherited via DocPerLocaleStrategy), rule 6 = fail-closed
    needs_link (a pre-existing article unknown to the map is never overwritten)
  - related[] cross-link remap: mapped ids remapped to target-locale ids; an UNMAPPED
    id stays pointing at source (documented choice, never invented)
  - collection_map rejection: a (source collection, locale) with no mapping fails
    LOUDLY (per-pair), never guessed; the mapped locale still succeeds
  - exists_target reads the MAP (done), never content; content-hash suppression
  - NEEDS_REVIEW exclusion + fully-refused article NOT created (FINDING 2 parity)
  - no-watermark regression (poll-first); org isolation (foreign cid -> 404)
  - GDPR/connection-delete sweeps the id-map rows; retry_failed only the failed pairs
  - CLI helpscout-translate + helpscout-resolve-link; web tile/type/panel/endpoint

ASCII-only prints; final line is 'verify_helpscout OK'.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_helpscout_")

import json

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import ConnectorIdMap, Segment, SegStatus
from app.services import connectors as _reg  # archive-tier spec introspection
from app.services.connectors import idmap
from app.services.connectors import store as cstore
from app.services.connectors.helpscout import HelpScoutConnector

# --------------------------------------------------------------------------- #
# Fake Help Scout Docs account (httpx.MockTransport).
# --------------------------------------------------------------------------- #
SRC_COL = "col-source-en"
DE_COL = "col-target-de"
FR_COL = "col-target-fr"


class FakeHelpScout:
    def __init__(self):
        # article id -> dict(id, collectionId, name, text, slug, related[], status, updatedAt)
        self.articles: dict = {}
        self.collections = [SRC_COL, DE_COL, FR_COL]
        self._seq = 0
        self.creates: list = []  # (collectionId, name) for each POST /articles
        self.puts: list = []  # (article_id) for each PUT
        self.put_bodies: list = []  # (article_id, body) for each PUT
        # Number of upcoming PUT *attempts* to 500 (>= RETRY_MAX exhausts one fill's
        # idempotent retries, simulating a crash-after-reserve that a retry can't swallow).
        self.put_fail_remaining = 0
        self.get_fail: dict = {}  # article id -> number of upcoming GET attempts to 500
        self.hosts_seen: set = set()
        self.requests: list = []  # (method, path) of every request
        self.auth_seen: set = set()
        self.page_size = 0  # 0 = one page; else the list endpoint pages by this size
        self.rate_limit_once = False
        self._rate_n = 0
        self.create_status = 0  # non-zero: POST /articles answers this status

    def transport(self):
        return httpx.MockTransport(self._handle)

    # -- source setup --
    def add_source(self, aid, name, html, slug="", related=None, collection=SRC_COL):
        self.articles[aid] = {
            "id": aid,
            "collectionId": collection,
            "name": name,
            "text": html,
            "slug": slug,
            "related": list(related or []),
            "status": "published",
            "updatedAt": "2026-02-01T00:00:00Z",
        }
        return aid

    def seed_preexisting(self, collection, name, slug=""):
        """An article in a TARGET collection that OUR map never created (rule 6)."""
        self._seq += 1
        tid = f"pre-{self._seq}"
        self.articles[tid] = {
            "id": tid,
            "collectionId": collection,
            "name": name,
            "text": "<p>human wrote this</p>",
            "slug": slug,
            "related": [],
            "status": "published",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
        return tid

    def ids_in(self, collection):
        return [a["id"] for a in self.articles.values() if a["collectionId"] == collection]

    def body_of(self, aid):
        return (self.articles.get(aid) or {}).get("text", "")

    def _list_env(self, items):
        return {"articles": {"page": 1, "pages": 1, "count": len(items), "items": items}}

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        seg = [p for p in path.split("/") if p]
        self.hosts_seen.add(req.url.host)
        self.requests.append((method, path))
        self.auth_seen.add(req.headers.get("Authorization"))
        if self.rate_limit_once and method == "GET" and path.endswith("/collections"):
            self._rate_n += 1
            if self._rate_n == 1:
                return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "rate"})
        # GET /v1/collections
        if method == "GET" and path.endswith("/collections"):
            items = [{"id": c, "name": c} for c in self.collections]
            return httpx.Response(
                200, json={"collections": {"page": 1, "pages": 1, "items": items}}
            )
        # GET /v1/collections/{id}/articles
        if method == "GET" and "/collections/" in path and path.endswith("/articles"):
            col = seg[-2]
            items = [
                {
                    "id": a["id"],
                    "collectionId": a["collectionId"],
                    "name": a["name"],
                    "slug": a["slug"],
                    "updatedAt": a["updatedAt"],
                }
                for a in self.articles.values()
                if a["collectionId"] == col
            ]
            if self.page_size:
                page = int(req.url.params.get("page") or 1)
                pages = max(1, -(-len(items) // self.page_size))
                lo = (page - 1) * self.page_size
                env = self._list_env(items[lo : lo + self.page_size])
                env["articles"].update({"page": page, "pages": pages})
                return httpx.Response(200, json=env)
            return httpx.Response(200, json=self._list_env(items))
        # GET /v1/articles/{id}
        if method == "GET" and "/articles/" in path:
            aid = seg[-1]
            if self.get_fail.get(aid, 0) > 0:
                self.get_fail[aid] -= 1
                return httpx.Response(500, json={"error": "internal"})
            a = self.articles.get(aid)
            if a is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"article": dict(a)})
        # POST /v1/articles (create)
        if method == "POST" and path.endswith("/articles"):
            body = json.loads(req.content)
            if self.create_status:
                return httpx.Response(self.create_status, json={"error": "refused"})
            self._seq += 1
            tid = f"art-{self._seq}"
            self.articles[tid] = {
                "id": tid,
                "collectionId": str(body.get("collectionId") or ""),
                "name": body.get("name") or "",
                "text": body.get("text") or "",
                "slug": body.get("slug") or "",
                "related": [str(x) for x in (body.get("related") or [])],
                "status": body.get("status") or "notpublished",
                "updatedAt": "2026-03-01T00:00:00Z",
            }
            self.creates.append((self.articles[tid]["collectionId"], self.articles[tid]["name"]))
            return httpx.Response(
                201, headers={"Location": f"/v1/articles/{tid}"}, json={"article": {"id": tid}}
            )
        # PUT /v1/articles/{id} (update)
        if method == "PUT" and "/articles/" in path:
            aid = seg[-1]
            if self.put_fail_remaining > 0:
                self.put_fail_remaining -= 1
                return httpx.Response(500, json={"error": "internal"})
            body = json.loads(req.content)
            a = self.articles.get(aid)
            if a is None:
                return httpx.Response(404, json={"error": "not found"})
            self.put_bodies.append((aid, body))
            a["name"] = body.get("name") or a["name"]
            a["text"] = body.get("text") or a["text"]
            if "related" in body:
                a["related"] = [str(x) for x in (body.get("related") or [])]
            if body.get("status"):
                a["status"] = body["status"]
            self.puts.append(aid)
            return httpx.Response(200, json={"article": dict(a)})
        return httpx.Response(404, json={"error": "unhandled", "path": path})


def _cmap(**per_locale):
    """collection_map: {SRC_COL: {locale: target_collection}}."""
    return {SRC_COL: dict(per_locale)}


def _sconn(name, fake, cmap=None, extra=None, targets="de-DE"):
    cfg = {
        "collection_map": cmap if cmap is not None else _cmap(**{"de-DE": DE_COL}),
        "source_locale": "en",
        "target_locales": targets,
    }
    cfg.update(extra or {})
    sec = {"api_key": "hs_testkey"}
    return cstore.create("helpscout", name, cfg, sec, actor="admin")


def _seed_one(
    fake,
    aid="art-src-A",
    name="Reset password",
    html="<p>Hello world</p><p>Second step.</p>",
    slug="reset-password",
    related=None,
):
    return fake.add_source(aid, name, html, slug=slug, related=related)


def main() -> None:
    init_db()
    # Make the shared HTTP retry/backoff instant (the resumable test exhausts a fill's
    # idempotent PUT retries) so the suite doesn't pay real backoff sleeps.
    import app.services.connectors.base as _base

    _base._sleep = lambda *_a, **_k: None

    # ---- (0) collection_map JSON-string parsing + case-insensitive locale match ----
    conn0 = HelpScoutConnector(
        {"collection_map": json.dumps({SRC_COL: {"de-DE": DE_COL}})}, {"api_key": "k"}
    )
    assert conn0._target_collection(SRC_COL, "de-de") == DE_COL, "JSON string + case-insensitive"
    assert conn0._target_collection(SRC_COL, "fr") is None, "unmapped locale -> None"
    assert conn0._target_collection("other-col", "de-de") is None, "unmapped collection -> None"
    print(
        "  config: collection_map parses JSON string; locale match is case-insensitive; "
        "unmapped -> None"
    )

    # ---- (1) full flow: one article per locale in the mapped collection ----
    f1 = FakeHelpScout()
    a1 = _seed_one(f1)
    c1 = _sconn("HS basic", f1, cmap=_cmap(**{"de-DE": DE_COL, "fr": FR_COL}), targets="de-DE, fr")
    ok, msg, s1 = cstore.helpscout_translate(c1.id, actor="auto", transport=f1.transport())
    assert ok, msg
    assert s1["created"] == 2 and s1["updated"] == 0, s1
    rows = idmap.list_for_connection(c1.id)
    assert len(rows) == 2 and all(r.status == "done" for r in rows), [
        (r.target_locale, r.status) for r in rows
    ]
    assert {r.target_locale for r in rows} == {"de-de", "fr"}, rows
    assert not f1.puts, ("the create already carries the content: no redundant PUT", f1.puts)
    assert f1.hosts_seen == {"docsapi.helpscout.net"}, f1.hosts_seen
    import base64

    assert f1.auth_seen == {"Basic " + base64.b64encode(b"hs_testkey:X").decode()}, f1.auth_seen
    de_row = next(r for r in rows if r.target_locale == "de-de")
    de_art = f1.articles[de_row.target_id]
    assert de_art["collectionId"] == DE_COL, ("de article in the DE collection", de_art)
    assert de_art["name"].startswith("[de-de]"), ("translated name", de_art["name"])
    assert "[de-de]" in de_art["text"] and "Hello world" in de_art["text"], de_art["text"]
    fr_row = next(r for r in rows if r.target_locale == "fr")
    assert f1.articles[fr_row.target_id]["collectionId"] == FR_COL, "fr article in FR collection"
    print(
        "  flow: 2 articles created in the mapped DE/FR collections; 2 done rows; "
        "HTML body + name translated"
    )

    # ---- (2) content-hash suppression: unchanged source -> skipped, reads the MAP ----
    ok, _m, s2 = cstore.helpscout_translate(c1.id, actor="auto", transport=f1.transport())
    assert ok and s2["created"] == 0 and s2["updated"] == 0 and s2["skipped"] == 2, s2
    creates_after_suppress = len(f1.creates)
    assert creates_after_suppress == 2, ("no new POSTs on a suppressed run", f1.creates)
    # exists_target reads the MAP (done), never content
    connector = cstore.build_fresh(c1.id, f1.transport())
    assert connector.strategy.exists_target(connector, a1, "de-de") is True, "done row -> exists"
    idmap.reserve(c1.id, a1, "es", "ghost", status="building")
    assert connector.strategy.exists_target(connector, a1, "es") is False, (
        "building row -> not exists"
    )
    idmap.delete(c1.id, a1, "es")
    print(
        "  suppression: unchanged -> 2 skipped, no new articles; exists_target reads MAP "
        "(done), building != exists"
    )

    # ---- (3) RULE 2: done + present -> update in place (PUT, same id) ----
    f3 = FakeHelpScout()
    a3 = _seed_one(f3)
    c3 = _sconn("HS rule2", f3)
    cstore.helpscout_translate(c3.id, actor="auto", transport=f3.transport())
    tid3 = idmap.get(c3.id, a3, "de-de").target_id
    creates_before = len(f3.creates)
    f3.articles[a3]["text"] += "<p>Brand new line.</p>"  # bust the hash
    ok, msg, s3 = cstore.helpscout_translate(c3.id, actor="auto", transport=f3.transport())
    assert ok, msg
    assert s3["updated"] == 1 and s3["created"] == 0, ("update in place", s3)
    assert len(f3.creates) == creates_before, ("no new article on update", f3.creates)
    assert idmap.get(c3.id, a3, "de-de").target_id == tid3, "same target id kept"
    assert "Brand new line" in f3.body_of(tid3), "new content written to same article"
    print("  RULE 2: done+present -> PUT in place (same id, no new article)")

    # ---- (4) RULE 3: done + target GONE -> orphaned + recreate (new id) ----
    f4 = FakeHelpScout()
    a4 = _seed_one(f4)
    c4 = _sconn("HS rule3", f4)
    cstore.helpscout_translate(c4.id, actor="auto", transport=f4.transport())
    old_tid = idmap.get(c4.id, a4, "de-de").target_id
    del f4.articles[old_tid]  # a human deleted our article (404s now)
    f4.articles[a4]["text"] += "<p>Another line.</p>"
    ok, msg, _s4b = cstore.helpscout_translate(c4.id, actor="auto", transport=f4.transport())
    assert ok, msg
    new_tid = idmap.get(c4.id, a4, "de-de").target_id
    assert new_tid and new_tid != old_tid, ("recreated with a NEW id", old_tid, new_tid)
    assert idmap.get(c4.id, a4, "de-de").status == "done", "recreated row is done"
    r4 = [r for r in idmap.list_for_connection(c4.id) if r.source_id == a4]
    assert len(r4) == 1, ("one row per (source, locale) — recreate reused it", r4)
    print("  RULE 3: done+GONE -> orphan the dead id + recreate with a new id (never resurrect)")

    # ---- (5) RULE 4 / resumable build: crash after reserve -> retry PUTs, no double-create --
    f5 = FakeHelpScout()
    a5 = _seed_one(f5)
    c5 = _sconn("HS resume", f5)
    # exhaust the idempotent retries of the fill's read of the target so the first fill fails
    # AFTER the article is created + reserved (a genuine crash-after-reserve, not a retry hiccup).
    f5.get_fail = {"art-1": _base.RETRY_MAX}
    ok, msg, s5a = cstore.helpscout_translate(c5.id, actor="auto", transport=f5.transport())
    assert not ok, ("the fill failure FAILS the run", msg, s5a)
    row5 = idmap.get(c5.id, a5, "de-de")
    assert row5 is not None and row5.status == "building", (
        "reserved as building after crash",
        row5,
    )
    reserved_tid = row5.target_id
    assert len(f5.creates) == 1, ("exactly one article created (the reserve)", f5.creates)
    # retry: must PUT the SAME reserved target, never POST a second article
    ok, msg, _s5b = cstore.helpscout_translate(c5.id, actor="auto", transport=f5.transport())
    assert ok, msg
    assert len(f5.creates) == 1, ("retry did NOT create a second article", f5.creates)
    row5b = idmap.get(c5.id, a5, "de-de")
    assert row5b.status == "done" and row5b.target_id == reserved_tid, (
        "resumed the SAME target to done",
        row5b.status,
        row5b.target_id,
        reserved_tid,
    )
    all5 = [r for r in idmap.list_for_connection(c5.id) if r.source_id == a5]
    assert len(all5) == 1, ("exactly ONE ConnectorIdMap row for the pair", all5)
    print(
        "  RULE 4 / resumable: crash after reserve -> retry fills the SAME target "
        "(one row, one id, no double-create)"
    )

    # ---- (6) RULE 5: building row + target absent -> drop stale row + recreate ----
    f6 = FakeHelpScout()
    a6 = _seed_one(f6)
    c6 = _sconn("HS rule5", f6)
    idmap.reserve(c6.id, a6, "de-de", "ghost-never-existed", status="building")
    ok, msg, _s6 = cstore.helpscout_translate(c6.id, actor="auto", transport=f6.transport())
    assert ok, msg
    row6 = idmap.get(c6.id, a6, "de-de")
    assert row6.status == "done" and row6.target_id != "ghost-never-existed", (
        "stale building row dropped + recreated",
        row6.status,
        row6.target_id,
    )
    assert len(f6.creates) == 1, ("recreated exactly once", f6.creates)
    print("  RULE 5: building+absent -> drop the stale row + recreate (never PUT a 404 id)")

    # ---- (7) RULE 6 (LOAD-BEARING): pre-existing article unknown to the map -> needs_link --
    f7 = FakeHelpScout()
    a7 = _seed_one(f7)
    c7 = _sconn("HS rule6", f7)
    # a human hand-wrote a translation in the DE collection with the SAME slug as source.
    pre_tid = f7.seed_preexisting(DE_COL, name="Passwort zuruecksetzen", slug="reset-password")
    pre_body_before = f7.body_of(pre_tid)
    ok, msg, s7 = cstore.helpscout_translate(c7.id, actor="auto", transport=f7.transport())
    assert not ok, ("a needs_link collision FAILS the run (loud)", msg, s7)
    row7 = idmap.get(c7.id, a7, "de-de")
    assert row7 is not None and row7.status == "needs_link", ("flagged needs_link", row7)
    assert row7.target_id == pre_tid, (
        "points at the pre-existing article (slug match)",
        row7.target_id,
        pre_tid,
    )
    assert f7.body_of(pre_tid) == pre_body_before, "the human's article was NOT overwritten"
    assert not any(col == DE_COL for (col, _n) in f7.creates), (
        "NO new article created for the collided pair",
        f7.creates,
    )
    # a SECOND run keeps failing closed
    ok2, _m, _s = cstore.helpscout_translate(c7.id, actor="auto", transport=f7.transport())
    assert not ok2 and idmap.get(c7.id, a7, "de-de").status == "needs_link", (
        "re-run stays fail-closed until an operator resolves it"
    )
    print(
        "  RULE 6: pre-existing human translation (slug collision) -> needs_link, FAIL "
        "CLOSED, never overwritten"
    )

    # ---- (7b) resolve needs_link: ADOPT -> next run updates in place; IGNORE -> drops row --
    okr, msgr, _sr = cstore.helpscout_resolve_link(c7.id, a7, "de-de", "adopt", actor="admin")
    assert okr, msgr
    assert idmap.get(c7.id, a7, "de-de").status == "done", "adopt -> done"
    f7.articles[a7]["text"] += "<p>Post-adopt change.</p>"
    ok3, msg3, s7c = cstore.helpscout_translate(c7.id, actor="auto", transport=f7.transport())
    assert ok3, msg3
    # the adopted human article is PUBLISHED: draft mode keeps it live and unchanged
    assert s7c["updated"] == 0 and s7c["live_kept"], ("adopted live article kept", s7c)
    assert f7.body_of(pre_tid) == pre_body_before
    cstore.patch_config(c7.id, {"publish": "1"})
    ok3, msg3, s7c = cstore.helpscout_translate(c7.id, actor="auto", transport=f7.transport())
    assert ok3, msg3
    assert s7c["updated"] == 1 and s7c["created"] == 0, ("adopted target updated in place", s7c)
    assert idmap.get(c7.id, a7, "de-de").target_id == pre_tid, "adopted the human's id"
    assert "Post-adopt change" in f7.body_of(pre_tid) or "[de-de]" in f7.body_of(pre_tid), (
        "content now written to the adopted article"
    )
    # ignore path (fresh collision)
    f7b = FakeHelpScout()
    a7b = _seed_one(f7b)
    c7b = _sconn("HS rule6-ignore", f7b)
    f7b.seed_preexisting(DE_COL, name="[de-de] Reset password", slug="reset-password")
    cstore.helpscout_translate(c7b.id, actor="auto", transport=f7b.transport())
    oki, _m, _s = cstore.helpscout_resolve_link(c7b.id, a7b, "de-de", "ignore", actor="admin")
    assert oki and idmap.get(c7b.id, a7b, "de-de") is None, "ignore -> row dropped"
    print("  RULE 6 resolve: adopt -> update-in-place same id; ignore -> row dropped")

    # ---- (8) RULE 7: a locale removed from scope leaves its row + target untouched ----
    f8 = FakeHelpScout()
    a8 = _seed_one(f8)
    c8 = _sconn("HS rule7", f8, cmap=_cmap(**{"de-DE": DE_COL, "fr": FR_COL}), targets="de-DE, fr")
    cstore.helpscout_translate(c8.id, actor="auto", transport=f8.transport())
    fr_row = idmap.get(c8.id, a8, "fr")
    fr_tid = fr_row.target_id
    assert fr_row.status == "done"
    cstore.patch_config(c8.id, {"target_locales": "de-DE"})  # narrow scope
    f8.articles[a8]["text"] += "<p>Scope change.</p>"
    ok, msg, _s8 = cstore.helpscout_translate(c8.id, actor="auto", transport=f8.transport())
    assert ok, msg
    fr_after = idmap.get(c8.id, a8, "fr")
    assert fr_after is not None and fr_after.status == "done" and fr_after.target_id == fr_tid, (
        "the out-of-scope fr row + target are untouched",
        fr_after,
    )
    assert fr_tid in f8.articles, "the fr target article is not deleted"
    print("  RULE 7: a locale narrowed out of scope keeps its row + target (no content deletion)")

    # ---- (9) related[] remap SELF-HEALS when a referent later becomes done (MED-1) ----
    # Seed the REFERRER (A) BEFORE the referent (B) so discovery lists A first: in run 1
    # A is written while B has no `done` row, so A's link starts pointing at SOURCE art-B.
    # A LATER run with NO source change must heal it, because the resolved-reference state
    # is folded into A's suppression hash. This replaces the old test-9 crutches (seeding
    # B first + hand-busting A's hash) with an honest cross-run heal.
    f9 = FakeHelpScout()
    _seed_one(
        f9,
        aid="art-A",
        name="Reset password",
        html="<p>See related.</p>",
        slug="reset-password",
        related=["art-B", "art-does-not-exist"],
    )
    _seed_one(f9, aid="art-B", name="Billing", html="<p>Billing help.</p>", slug="billing")
    c9 = _sconn("HS related", f9)  # de-DE only
    ok, msg, _s = cstore.helpscout_translate(c9.id, actor="auto", transport=f9.transport())
    assert ok, msg
    a_tid = idmap.get(c9.id, "art-A", "de-de").target_id
    b_tid = idmap.get(c9.id, "art-B", "de-de").target_id
    assert idmap.get(c9.id, "art-B", "de-de").status == "done", "B is done after run 1"
    # run 1 left A pointing at the SOURCE art-B (B was not yet done when A was written)
    assert f9.articles[a_tid]["related"] == ["art-B", "art-does-not-exist"], (
        "run 1: A links at source art-B",
        f9.articles[a_tid]["related"],
    )
    # run 2: NO source change. The heal fires because B's `done` state changed A's hash.
    ok2, msg2, s9 = cstore.helpscout_translate(c9.id, actor="auto", transport=f9.transport())
    assert ok2, msg2
    assert s9["updated"] == 1, ("A re-filled to heal its link with no source change", s9)
    a_related = f9.articles[a_tid]["related"]
    assert b_tid in a_related and "art-B" not in a_related, (
        "healed: source art-B replaced by B's target id",
        a_related,
        b_tid,
    )
    assert "art-does-not-exist" in a_related, (
        "an UNMAPPED related id stays pointing at source (never invented)",
        a_related,
    )
    # run 3: fully settled -> A is suppressed again (the heal is STABLE, no re-write churn)
    ok3, _m3, s9c = cstore.helpscout_translate(c9.id, actor="auto", transport=f9.transport())
    assert ok3 and s9c["updated"] == 0 and s9c["skipped"] == 2, (
        "healed state is stable: A no longer re-fills",
        s9c,
    )
    print(
        "  related[] remap SELF-HEALS: a referrer written before its referent heals on "
        "the next run with NO source change; unmapped stays source; heal is stable"
    )

    # ---- (10) collection_map rejection: an unmapped (collection, locale) fails loudly ----
    f10 = FakeHelpScout()
    a10 = _seed_one(f10)
    # de-DE mapped, fr NOT mapped -> fr must be rejected loudly, de-DE must succeed
    c10 = _sconn("HS colreject", f10, cmap=_cmap(**{"de-DE": DE_COL}), targets="de-DE, fr")
    ok, msg, s10 = cstore.helpscout_translate(c10.id, actor="auto", transport=f10.transport())
    # de-DE landed; fr failed -> not a clean run, but de-DE work happened
    assert (
        idmap.get(c10.id, a10, "de-de") is not None
        and idmap.get(c10.id, a10, "de-de").status == "done"
    ), "de-DE (mapped) succeeded"
    assert idmap.get(c10.id, a10, "fr") is None, ("no id-map row for the unmapped fr pair", s10)
    fr_fail = [ff for ff in s10.get("failures", []) if ff.get("locale") == "fr"]
    assert fr_fail and (
        "collection" in fr_fail[0]["error"].lower() and "mapped" in fr_fail[0]["error"].lower()
    ), ("fr rejected loudly with a collection-mapping error", s10.get("failures"))
    assert not any(col == FR_COL for (col, _n) in f10.creates), "no fr article created"
    print(
        "  collection_map: an unmapped (collection, locale) fails LOUDLY per-pair "
        "(never guessed); the mapped locale still succeeds"
    )

    # ---- (11) no watermark key ever written (poll-first) ----
    cfg11 = cstore.get(c1.id).config or {}
    assert "helpscout_hashes" in cfg11, ("content-hash map persisted", list(cfg11))
    assert not any("watermark" in k for k in cfg11), (
        "poll-first flow must NOT introduce a watermark",
        list(cfg11),
    )
    print("  regression: helpscout_hashes persisted, NO watermark key (poll-first)")

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
                    Segment.source_text == "Hello world",
                )
            ).first()
            if seg:
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
                session.commit()
        return stats

    f12 = FakeHelpScout()
    a12 = _seed_one(f12)
    c12 = _sconn("HS needs-review", f12)
    set_pieces("app.services.translate", "pretranslate_document", _ptd_flip)
    try:
        ok, msg, _s12 = cstore.helpscout_translate(c12.id, actor="auto", transport=f12.transport())
        assert ok, msg
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    # #640: a pair with a held unit is NOT created (the held spot would carry SOURCE text: a
    # mixed-language target); nothing reaches the wire and the next run retries it.
    assert idmap.get(c12.id, a12, "de-de") is None, "held pair: no target created, no id-map row"
    print("  defense-in-depth: a NEEDS_REVIEW unit holds the pair (no target created)")

    # ---- (13) FINDING 2: a fully-refused translation does not create a clone ----
    def _ptd_all_refused(session, doc_id, locale, spec=None, force_pii=False):
        stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == "de-de":
            for seg in session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "de-de"
                )
            ).all():
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
            session.commit()
        return stats

    f13 = FakeHelpScout()
    a13 = _seed_one(f13)
    c13 = _sconn("HS refused", f13)
    set_pieces("app.services.translate", "pretranslate_document", _ptd_all_refused)
    try:
        ok13, msg13, s13 = cstore.helpscout_translate(
            c13.id, actor="auto", transport=f13.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert ok13 and s13["held_back"] == 1 and s13["failures"] == [], (msg13, s13)
    assert "Held back 1" in msg13, msg13
    assert not f13.creates, ("NO POST /articles for a fully-refused article", f13.creates)
    assert idmap.get(c13.id, a13, "de-de") is None, "no id-map row for a refused pair"
    cfg13 = cstore.get(c13.id).config or {}
    assert f"{a13}:de-de" not in (cfg13.get("helpscout_hashes") or {}), (
        "hash NOT recorded for a refused pair (so a retry re-attempts it)"
    )
    print("  FINDING 2: a fully-refused article is NOT created; reported held back; no hash")

    # ---- (14) retry_failed re-runs ONLY the failed (article, locale) pairs ----
    f14 = FakeHelpScout()
    x = _seed_one(f14, aid="art-X", name="Xray")
    y = _seed_one(f14, aid="art-Y", name="Yankee")
    c14 = _sconn(
        "HS retry", f14, cmap=_cmap(**{"de-DE": DE_COL, "fr": FR_COL}), targets="de-DE, fr"
    )
    failures = [
        {"article_id": x, "locale": "de-de", "error": "500"},
        {"article_id": y, "locale": "fr", "error": "500"},
    ]
    okr, _m, _sr = cstore.retry_failed(c14.id, failures, actor="auto", transport=f14.transport())
    assert okr, _sr
    made = {(r.source_id, r.target_locale) for r in idmap.list_for_connection(c14.id)}
    assert made == {(x, "de-de"), (y, "fr")}, ("only the 2 failed pairs built", made)
    print("  retry: re-ran ONLY the 2 failed (article, locale) pairs, not the cross product")

    # ---- (15) GDPR / connection delete sweeps the id-map rows ----
    with Session(engine) as s:
        before = s.exec(select(ConnectorIdMap).where(ConnectorIdMap.connection_id == c14.id)).all()
    assert before, "precondition: id-map rows exist"
    cstore.delete(c14.id)
    with Session(engine) as s:
        after = s.exec(select(ConnectorIdMap).where(ConnectorIdMap.connection_id == c14.id)).all()
    assert not after, ("connection delete swept the ConnectorIdMap rows (GDPR + FK)", len(after))
    print("  GDPR: deleting a Help Scout connection sweeps its ConnectorIdMap rows")

    # ---- (16) org isolation: foreign/missing cid -> 404 on the /api routes ----
    c = TestClient(app)
    from tests._api_fixture import user_api_token

    hdr = {"X-Loc-Token": user_api_token()}
    fapi = FakeHelpScout()
    _seed_one(fapi)
    capi = _sconn("HS api", fapi)
    assert (
        c.post("/api/connectors/99999999/helpscout/translate", json={}, headers=hdr).status_code
        == 404
    )
    assert (
        c.post("/api/connectors/99999999/helpscout/resolve-link", json={}, headers=hdr).status_code
        == 404
    )
    r_own = c.post(
        f"/api/connectors/{capi.id}/helpscout/translate", json={"article_ids": []}, headers=hdr
    )
    assert r_own.status_code in (200, 400), (r_own.status_code, r_own.text)
    print("  org isolation: foreign/missing cid -> 404 on both /api helpscout routes")

    # ---- (17) web routes: tile + type page + endpoint + id-map panel ----
    assert "Help Scout" in c.get("/connectors").text, "Help Scout tile missing from /connectors"
    typ = c.get("/connectors/helpscout").text
    # Rebuilt and un-archived 2026-10-10: the add form is open. Pin the spec field names too.
    assert "New connections are closed" not in typ, "Help Scout is un-archived"
    _fnames = {f.name for f in _reg.spec("helpscout").config_fields} | {
        f.name for f in _reg.spec("helpscout").secret_fields
    }
    assert "collection_map" in _fnames, (
        "Help Scout spec must still declare 'collection_map'",
        _fnames,
    )
    page = c.get(f"/connectors/connection/{capi.id}").text
    assert "/helpscout/translate" in page, "inbound-API endpoint must render"
    assert "id map" in page.lower(), "the id-map status panel must render"
    print("  routes: Help Scout tile + type page; inbound endpoint + id-map panel present")

    # ---- (18) CLI helpscout-translate + helpscout-resolve-link smoke ----
    from app.cli.cmd_connector import (
        _helpscout_resolve_link,
        _helpscout_translate,
        _parse_list,  # noqa: F401
    )

    fcli = FakeHelpScout()
    scli = _seed_one(fcli)
    conncli = _sconn("HS cli", fcli)

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

    real_hs = cstore.helpscout_translate
    seen = {}

    def _cap_hs(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        seen["called"] = True
        return real_hs(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=fcli.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "helpscout_translate", _cap_hs)
    try:
        emit = _Emit()
        assert _helpscout_translate(_Args(), emit) == 0, emit.res
        assert seen.get("called") and emit.res[1]["ok"], emit.res
    finally:
        set_pieces("app.services.connectors.store", "helpscout_translate", real_hs)
    emit2 = _Emit()
    rc = _helpscout_resolve_link(_Args(), emit2)
    assert rc in (0, 1) and emit2.res is not None, emit2.res
    print("  CLI: helpscout-translate + helpscout-resolve-link parse args and run")

    from app.services.connectors.base import ConnectorError

    # ---- (19) MED-2: rule 6 detects a SAME-slug human article even when the LIST
    #           summary OMITS slug (fetch-by-id fallback; the list shape is [UNCERTAIN]) --
    class _SlugOmittingHelpScout(FakeHelpScout):
        """The suite's fake, but the collection-articles LIST omits `slug` (Help Scout's
        real list summary shape is [UNCERTAIN]). The by-id GET still returns slug."""

        def _handle(self, req):
            path = req.url.path
            if req.method == "GET" and "/collections/" in path and path.endswith("/articles"):
                col = [p for p in path.split("/") if p][-2]
                items = [
                    {
                        "id": a["id"],
                        "collectionId": a["collectionId"],
                        "name": a["name"],
                        "updatedAt": a["updatedAt"],
                    }  # slug omitted
                    for a in self.articles.values()
                    if a["collectionId"] == col
                ]
                return httpx.Response(200, json=self._list_env(items))
            return super()._handle(req)

    f19 = _SlugOmittingHelpScout()
    a19 = _seed_one(f19)  # source slug 'reset-password'
    c19 = _sconn("HS slug-absent", f19)
    # a human hand-wrote the DE translation using the SAME source slug but a localized name
    f19.seed_preexisting(DE_COL, name="Passwort zuruecksetzen", slug="reset-password")
    de_before19 = set(f19.ids_in(DE_COL))
    ok19, _m19, _s19 = cstore.helpscout_translate(c19.id, actor="auto", transport=f19.transport())
    row19 = idmap.get(c19.id, a19, "de-de")
    assert not ok19, "same-slug human article -> fail closed"
    assert row19 is not None and row19.status == "needs_link", (
        "detected via fetch-by-id even though the list summary omits slug",
        row19,
    )
    assert set(f19.ids_in(DE_COL)) == de_before19, ("no duplicate minted", f19.ids_in(DE_COL))
    print(
        "  MED-2: a SAME-slug human translation is detected via fetch-by-id when the "
        "list omits slug (rule-6 fail-closed, no duplicate)"
    )

    # ---- (20) MED-2: rule 6 detects a human article that kept the SOURCE title ----
    f20 = FakeHelpScout()
    a20 = _seed_one(f20, name="Reset password", slug="reset-password")
    c20 = _sconn("HS src-title", f20)
    # localized slug (no shared slug key) but the SOURCE-language title was kept
    f20.seed_preexisting(DE_COL, name="Reset password", slug="passwort-zuruecksetzen")
    de_before20 = set(f20.ids_in(DE_COL))
    ok20, _m20, _s20 = cstore.helpscout_translate(c20.id, actor="auto", transport=f20.transport())
    row20 = idmap.get(c20.id, a20, "de-de")
    assert not ok20 and row20 is not None and row20.status == "needs_link", (
        "source-title match catches the human article",
        row20,
    )
    assert set(f20.ids_in(DE_COL)) == de_before20, ("no duplicate minted", f20.ids_in(DE_COL))
    print(
        "  MED-2: a human translation that kept the SOURCE title is detected (source-"
        "title match), fail-closed, no duplicate"
    )

    # ---- (21) MED-2 RESIDUAL (documented): a fully-localized slug+title human
    #           translation is UNDETECTABLE -> a separate duplicate is minted, but the
    #           human's article is NEVER clobbered ----
    f21 = FakeHelpScout()
    a21 = _seed_one(f21, name="Reset password", slug="reset-password")
    c21 = _sconn("HS residual", f21)
    human21 = f21.seed_preexisting(
        DE_COL, name="Passwort zuruecksetzen", slug="passwort-zuruecksetzen"
    )  # localized slug AND title
    human_body21 = f21.body_of(human21)
    ok21, _m21, _s21 = cstore.helpscout_translate(c21.id, actor="auto", transport=f21.transport())
    row21 = idmap.get(c21.id, a21, "de-de")
    assert ok21 and row21 is not None and row21.status == "done" and row21.target_id != human21, (
        "undetectable residual: a separate target is minted",
        row21,
        human21,
    )
    assert f21.body_of(human21) == human_body21, (
        "the human's article is NEVER overwritten (a duplicate, not a clobber)",
        human21,
    )
    print(
        "  MED-2 residual (documented): a fully-localized slug+title human translation "
        "is undetectable -> a separate duplicate is minted; the human's article is "
        "never clobbered"
    )

    # ---- (22) MED-3: an invalid collection_map partition is rejected LOUDLY at the
    #           store CREATE seam (both violation shapes, offending ids named) ----
    f22 = FakeHelpScout()
    try:  # shape (a): two locales share one target collection
        _sconn(
            "HS bad-shared",
            f22,
            cmap={SRC_COL: {"de-DE": DE_COL, "fr": DE_COL}},
            targets="de-DE, fr",
        )
        assert False, "shape (a) should be rejected at create"
    except ConnectorError as e:
        assert "two locales" in str(e) and DE_COL in str(e), ("names the offending ids", str(e))
    try:  # shape (b): a target collection equals a source collection key
        _sconn("HS bad-loop", f22, cmap={SRC_COL: {"de-DE": SRC_COL}})
        assert False, "shape (b) should be rejected at create"
    except ConnectorError as e:
        assert "SOURCE" in str(e) and SRC_COL in str(e), ("names the offending ids", str(e))
    print(
        "  MED-3: an invalid collection_map (shared target / target==source) is "
        "rejected LOUDLY at the store create seam, naming the offending ids"
    )

    # ---- (23) MED-3: the UPDATE seam rejects the same (no persist), and a pre-existing
    #           config is caught DEFENSIVELY at run start ----
    f23 = FakeHelpScout()
    a23 = _seed_one(f23)
    c23 = _sconn("HS valid-then-bad", f23)  # created with a VALID map
    try:
        cstore.update(
            c23.id,
            "HS valid-then-bad",
            {"collection_map": {SRC_COL: {"de-DE": DE_COL, "fr": DE_COL}}},
            None,
        )
        assert False, "update should reject an invalid partition"
    except ConnectorError:
        pass
    assert (cstore.get(c23.id).config or {}).get("collection_map") == _cmap(**{"de-DE": DE_COL}), (
        "the rejected update did NOT persist the invalid map"
    )
    # inject the bad map bypassing the write guards (patch_config), then run -> defensive
    cstore.patch_config(c23.id, {"collection_map": {SRC_COL: {"de-DE": DE_COL, "fr": DE_COL}}})
    ok23, msg23, _s23 = cstore.helpscout_translate(c23.id, actor="auto", transport=f23.transport())
    assert not ok23 and "collection_map" in msg23 and "invalid" in msg23, (
        "run start rejects the pre-existing invalid config loudly",
        msg23,
    )
    assert idmap.get(c23.id, a23, "de-de") is None, "no id-map rows written for a rejected run"
    print(
        "  MED-3: the update seam rejects an invalid partition (no persist) and a "
        "pre-existing invalid config is caught defensively at run start"
    )

    # ---- (24) LOW-4: retry_failed with a MIXED-CASE locale ('de-DE') actually retries
    #           (the flow lowercases usable, so a raw-case only_pairs used to no-op) ----
    f24 = FakeHelpScout()
    z24 = _seed_one(f24, aid="art-Z", name="Zulu")
    c24 = _sconn("HS retry-case", f24)  # de-DE mapped
    failures24 = [{"article_id": z24, "locale": "de-DE", "error": "500"}]  # MIXED CASE
    okr24, _m24, _sr24 = cstore.retry_failed(
        c24.id, failures24, actor="auto", transport=f24.transport()
    )
    assert okr24, _sr24
    row24 = idmap.get(c24.id, z24, "de-de")
    assert row24 is not None and row24.status == "done", (
        "a mixed-case retry locale is normalized and actually re-run, not a silent no-op",
        row24,
    )
    print(
        "  LOW-4: retry_failed normalizes a mixed-case locale ('de-DE') so the pair is "
        "actually re-run instead of silently no-opping"
    )

    # ---- (25) NEW MED: a NON-UNIQUE source title must NOT false-collide with a human
    #           target that shares it — both same-titled sources create via slug fallback --
    f25 = FakeHelpScout()
    a25 = _seed_one(f25, aid="art-A25", name="Reset password", slug="reset-a")
    b25 = _seed_one(f25, aid="art-B25", name="Reset password", slug="reset-b")  # SAME title
    c25 = _sconn("HS nonunique-title", f25)
    human25 = f25.seed_preexisting(DE_COL, name="Reset password", slug="human-de")  # shares title
    human_body25 = f25.body_of(human25)
    ok25, msg25, _s25 = cstore.helpscout_translate(c25.id, actor="auto", transport=f25.transport())
    ra25 = idmap.get(c25.id, a25, "de-de")
    rb25 = idmap.get(c25.id, b25, "de-de")
    assert ok25, msg25
    assert ra25 is not None and ra25.status == "done", ("first same-titled source created", ra25)
    assert rb25 is not None and rb25.status == "done", ("second same-titled source created", rb25)
    assert ra25.target_id != human25 and rb25.target_id != human25, (
        "neither same-titled source adopted the human article"
    )
    assert f25.body_of(human25) == human_body25, "the human's article was not touched"
    print(
        "  NEW MED: a non-unique source title falls back to slug-only detection, so two "
        "same-titled sources both create instead of false-colliding with a human target"
    )

    # ---- (26) NEW LOW: the rule-6 by-id slug fetch is HARD-BOUNDED to max_articles ----
    class _CapProbeHelpScout(FakeHelpScout):
        """Slug-omitting list (forces the by-id fetch path) + counts by-id GETs to
        DE_COL, so the test can assert the fetch count is bounded by max_articles."""

        def __init__(self):
            super().__init__()
            self.byid_de_gets = []

        def _handle(self, req):
            path = req.url.path
            if req.method == "GET" and "/articles/" in path and "/collections/" not in path:
                aid = [p for p in path.split("/") if p][-1]
                art = self.articles.get(aid)
                # only the human (pre-existing) articles: the probe's fetches, not the read of the
                # target this run just created
                if art and art["collectionId"] == DE_COL and aid.startswith("pre-"):
                    self.byid_de_gets.append(aid)
            if req.method == "GET" and "/collections/" in path and path.endswith("/articles"):
                col = [p for p in path.split("/") if p][-2]
                items = [
                    {
                        "id": art["id"],
                        "collectionId": art["collectionId"],
                        "name": art["name"],
                        "updatedAt": art["updatedAt"],
                    }  # slug omitted
                    for art in self.articles.values()
                    if art["collectionId"] == col
                ]
                return httpx.Response(200, json=self._list_env(items))
            return super()._handle(req)

    f26 = _CapProbeHelpScout()
    _seed_one(f26, name="Unique source title 26", slug="uniq-src-26")  # source, no match
    for i in range(10):  # a large in-page target collection, none matching
        f26.seed_preexisting(DE_COL, name=f"German article {i}", slug=f"de-art-{i}")
    c26 = _sconn("HS cap-bound", f26, extra={"max_articles": "3"})
    ok26, m26, _s26 = cstore.helpscout_translate(c26.id, actor="auto", transport=f26.transport())
    assert ok26, m26
    assert len(f26.byid_de_gets) <= 3, (
        "the rule-6 by-id fetch is bounded to max_articles (cap=3), not one per in-page article",
        len(f26.byid_de_gets),
    )
    print(
        "  NEW LOW: the rule-6 by-id slug fetch is hard-bounded to max_articles (cap=3 "
        f"-> {len(f26.byid_de_gets)} by-id fetch(es), not the whole collection)"
    )

    # ---- (27) HELD UNITS on UPDATE (#640): the stored text is re-sent, never the source ----
    def _flip_unit(locale_to_hold, source_text):
        def _ptd(session, doc_id, locale, spec=None, force_pii=False):
            stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
            if locale == locale_to_hold:
                seg = session.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id,
                        Segment.target_locale == locale,
                        Segment.source_text == source_text,
                    )
                ).first()
                if seg:
                    seg.status = SegStatus.NEEDS_REVIEW
                    session.add(seg)
                    session.commit()
            return stats

        return _ptd

    f27 = FakeHelpScout()
    a27 = _seed_one(f27, name="Reset password")
    c27 = _sconn("HS held-update", f27)
    ok, msg, _s = cstore.helpscout_translate(c27.id, actor="auto", transport=f27.transport())
    assert ok, msg
    t27 = idmap.get(c27.id, a27, "de-de").target_id
    stored_name = f27.articles[t27]["name"]
    stored_text = f27.articles[t27]["text"]

    def _hashes(cid):
        return dict(cstore.get(cid).config.get("helpscout_hashes") or {})

    h27_before = _hashes(c27.id)
    f27.articles[a27]["text"] = "<p>Hello world</p><p>Second step changed.</p>"
    set_pieces(
        "app.services.translate", "pretranslate_document", _flip_unit("de-de", "Hello world")
    )
    try:
        ok, msg, s27 = cstore.helpscout_translate(c27.id, actor="auto", transport=f27.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert f27.articles[t27]["text"] == stored_text, "a body with a held unit keeps its stored text"
    assert f27.articles[t27]["name"] == stored_name
    assert not f27.put_bodies or all(
        "Hello world" not in b.get("text", "") for _a, b in f27.put_bodies
    )
    assert s27["held_back"] == 1 and s27["failures"] == [], s27
    # #666: no new hash, and the pair's OLDER hash is dropped (a later source revert to that
    # text must re-write the pair, not hash-match it as done)
    assert f"{a27}:de-de" in h27_before, h27_before
    assert _hashes(c27.id) == {k: v for k, v in h27_before.items() if k != f"{a27}:de-de"}, (
        "a partly held pair records no new hash and drops its stale one",
        _hashes(c27.id),
    )
    # held TITLE only: the translated body lands, the stored title stays (never the source title)
    f27b = FakeHelpScout()
    a27b = _seed_one(f27b, name="Reset password")
    c27b = _sconn("HS held-title", f27b)
    cstore.helpscout_translate(c27b.id, actor="auto", transport=f27b.transport())
    t27b = idmap.get(c27b.id, a27b, "de-de").target_id
    old_name = f27b.articles[t27b]["name"]
    f27b.articles[a27b]["text"] = "<p>Hello world</p><p>Another new step.</p>"
    set_pieces(
        "app.services.translate", "pretranslate_document", _flip_unit("de-de", "Reset password")
    )
    try:
        ok, msg, s27b = cstore.helpscout_translate(
            c27b.id, actor="auto", transport=f27b.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert f27b.articles[t27b]["name"] == old_name, "held title: the stored name is re-sent"
    assert "Another new step" in f27b.articles[t27b]["text"], "the translated body landed"
    assert s27b["updated"] == 1 and s27b["failures"] == [], s27b
    assert s27b["partial_held"] == 1 and s27b["held_back"] == 1, s27b
    # held TITLE with publish=1 and a published source: the draft target is NOT promoted to
    # published (a partly translated article never goes live; Review gate 10-10).
    f27c = FakeHelpScout()
    a27c = _seed_one(f27c, name="Reset password")
    c27c = _sconn("HS held-publish", f27c)
    cstore.helpscout_translate(c27c.id, actor="auto", transport=f27c.transport())
    t27c = idmap.get(c27c.id, a27c, "de-de").target_id
    assert f27c.articles[t27c]["status"] == "notpublished"
    cstore.patch_config(c27c.id, {"publish": "1"})
    f27c.articles[a27c]["text"] = "<p>Hello world</p><p>Third new step.</p>"
    set_pieces(
        "app.services.translate", "pretranslate_document", _flip_unit("de-de", "Reset password")
    )
    try:
        ok, msg, _s27c = cstore.helpscout_translate(
            c27c.id, actor="auto", transport=f27c.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert "Third new step" in f27c.articles[t27c]["text"], "the translated body landed"
    assert f27c.articles[t27c]["status"] == "notpublished", "a held pair is never published"
    assert all("status" not in b for a, b in f27c.put_bodies if str(a) == str(t27c)), (
        f27c.put_bodies
    )
    print("  #640: a held body keeps its stored text; a held title keeps its stored name")
    print("  #640: publish=1 never promotes a held (partly translated) target to published")

    # ---- (28) publish toggle: only a PUBLISHED source produces a published target ----
    f28 = FakeHelpScout()
    p28 = _seed_one(f28, aid="art-pub", name="Published one")
    u28 = _seed_one(f28, aid="art-unpub", name="Unpublished one")
    f28.articles[u28]["status"] = "notpublished"
    c28 = _sconn("HS publish", f28, extra={"publish": "1"})
    ok, msg, _s = cstore.helpscout_translate(c28.id, actor="auto", transport=f28.transport())
    assert ok, msg
    st = {a: f28.articles[idmap.get(c28.id, a, "de-de").target_id]["status"] for a in (p28, u28)}
    assert st == {p28: "published", u28: "notpublished"}, st
    f28b = FakeHelpScout()
    _seed_one(f28b, aid="art-d", name="Draft mode")
    c28b = _sconn("HS draft", f28b)
    cstore.helpscout_translate(c28b.id, actor="auto", transport=f28b.transport())
    only = [a for a in f28b.articles.values() if a["collectionId"] == DE_COL]
    assert [a["status"] for a in only] == ["notpublished"], only
    print("  publish: published source -> published target; unpublished / draft mode stay drafts")

    # ---- (29) DRAFT MODE never changes a live (published) target ----
    seen_mt = {"n": 0}

    def _count_ptd(session, doc_id, locale, spec=None, force_pii=False):
        seen_mt["n"] += 1
        return real_ptd(session, doc_id, locale, spec, force_pii=force_pii)

    f29 = FakeHelpScout()
    a29 = _seed_one(f29, name="Live one")
    c29 = _sconn("HS live-kept", f29)
    cstore.helpscout_translate(c29.id, actor="auto", transport=f29.transport())
    t29 = idmap.get(c29.id, a29, "de-de").target_id
    f29.articles[t29]["status"] = "published"  # a human published the German article
    live_text = f29.articles[t29]["text"]
    f29.articles[a29]["text"] = "<p>Hello world</p><p>Edited after publishing.</p>"
    set_pieces("app.services.translate", "pretranslate_document", _count_ptd)
    try:
        ok, msg, s29 = cstore.helpscout_translate(c29.id, actor="auto", transport=f29.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert ok, msg
    assert seen_mt["n"] == 0, ("no MT spent for a live target", seen_mt)
    assert f29.articles[t29]["text"] == live_text and f29.articles[t29]["status"] == "published"
    assert [r["locale"] for r in s29["live_kept"]] == ["de-de"] and s29["failures"] == [], s29
    assert "LIVE" in msg and "Publish translations = 1" in msg, msg
    hash29 = cstore.get(c29.id).config["helpscout_hashes"].get(f"{a29}:de-de")
    # an explicit retry bypasses the pre-MT hook; the write-time guard still refuses
    okr, _m, _sr = cstore.retry_failed(
        c29.id,
        [{"article_id": a29, "locale": "de-de", "error": "x"}],
        actor="auto",
        transport=f29.transport(),
    )
    assert f29.articles[t29]["text"] == live_text, "retry kept the live text"
    # publish mode updates it live
    cstore.patch_config(c29.id, {"publish": "1"})
    ok, msg, s29b = cstore.helpscout_translate(c29.id, actor="auto", transport=f29.transport())
    assert ok and s29b["updated"] == 1, (msg, s29b)
    assert "Edited after publishing" in f29.articles[t29]["text"], "publish mode updates it"
    assert hash29 != cstore.get(c29.id).config["helpscout_hashes"].get(f"{a29}:de-de")
    print("  draft mode: a published target is kept live, no MT spent, reported; publish=1 updates")

    # ---- (30) ids and scope: refused or dropped before they matter ----
    f30 = FakeHelpScout()
    a30 = _seed_one(f30, name="In scope")
    other = f30.add_source("art-other", "Other collection", "<p>x</p>", collection="col-other")
    c30 = _sconn("HS scope", f30)
    conn30 = cstore.build_fresh(c30.id, f30.transport())
    n_start = len(f30.requests)
    for bad in ("../x", "a/b", "a b", "a?b", "a%2eb", "\u0661", "x" * 80, ""):
        try:
            conn30.discover_articles(article_ids=[bad])
            raise AssertionError(f"article id {bad!r} must be refused")
        except ConnectorError:
            pass
    assert len(f30.requests) == n_start, ("refused before any request", f30.requests[n_start:])
    got = conn30.discover_articles(article_ids=[a30, other, "missing-1"])
    assert [a["id"] for a in got] == [a30], ("only the mapped source collection", got)
    tgt = f30.seed_preexisting(DE_COL, name="A target article", slug="t")
    assert conn30.discover_articles(article_ids=[tgt]) == [], "a target article is never a source"
    for bad_map in (
        {"../x": {"de-DE": DE_COL}},
        {SRC_COL: {"de-DE": "a/b"}},
        {SRC_COL: {"de-DE": "a%2fb"}},
    ):
        try:
            _sconn("HS badmap", f30, cmap=bad_map)
            raise AssertionError("bad collection id must be refused at create")
        except ConnectorError as e:
            assert "Invalid Help Scout" in str(e), e
    print("  ids/scope: bad ids refused before any request; named articles outside the map dropped")

    # ---- (31) credentials never logged or returned; vendor errors; pagination; 429 ----
    import logging

    records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _Cap()
    logging.getLogger().addHandler(h)
    old_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        f31 = FakeHelpScout()
        _seed_one(f31)
        f31.create_status = 400  # e.g. the article name is not unique in the collection
        c31 = _sconn("HS vendor-error", f31)
        ok31, msg31, s31 = cstore.helpscout_translate(
            c31.id, actor="auto", transport=f31.transport()
        )
    finally:
        logging.getLogger().removeHandler(h)
        logging.getLogger().setLevel(old_level)
    assert not ok31 and "400" in json.dumps(s31["failures"]), (msg31, s31)
    assert not idmap.list_for_connection(c31.id), "a refused create leaves no id-map row"
    blob = " ".join(records) + msg31 + json.dumps(s31)
    assert "hs_testkey" not in blob and "aHNfdGVzdGtleTpY" not in blob, "key leaked"
    bad_key = HelpScoutConnector({"collection_map": {}}, {"api_key": "hs_secretvalue"})
    okk, mk = bad_key.test()
    assert not okk and "hs_secretvalue" not in mk, mk

    f32 = FakeHelpScout()
    for i in range(7):
        f32.add_source(f"art-p{i}", f"Paged {i}", "<p>x</p>")
    f32.page_size = 3
    conn32 = HelpScoutConnector(
        {"collection_map": {SRC_COL: {"de-DE": DE_COL}}, "max_articles": "50"},
        {"api_key": "k"},
        transport=f32.transport(),
    )
    assert len(conn32.discover_articles()) == 7, "three pages of 3,3,1 are all read"

    slept = []
    real_sleep = _base._sleep
    _base._sleep = lambda s: slept.append(s)
    try:
        f33 = FakeHelpScout()
        f33.rate_limit_once = True
        okt, mt = HelpScoutConnector(
            {"collection_map": {SRC_COL: {"de-DE": DE_COL}}},
            {"api_key": "k"},
            transport=f33.transport(),
        ).test()
        assert okt and slept == [7.0], (mt, slept)
    finally:
        _base._sleep = real_sleep
    print(
        "  credentials: key never logged/returned; vendor 4xx per pair; 3 pages read; 429 honoured"
    )

    print("verify_helpscout OK")


if __name__ == "__main__":
    main()
