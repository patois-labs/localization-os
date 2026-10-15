"""Zoho Desk connector (rebuilt 2026-10-10) - fully offline via httpx.MockTransport (a fake Zoho
Desk tenant that speaks the shapes in the vendor's OpenAPI spec: PATCH to edit a translation,
POST to create one with a required authorId, `answer` as the body key, OAuth refresh on the
accounts host of the same data center).

Covers: test/discover/enabled locales, multi-DC host pin and the host allowlist, orgId on every
call, create (POST) and update (PATCH, partial), suppression by hash and translationState, locale
rejection, publish toggle, 429 backoff, pagination, scope (category/department) on listed and
by-id articles, id/locale validation before any request, the #640 held-unit rules (create refused,
update omits the held field, no hash, OUTDATED stays), draft-mode live-kept (no MT spent, no
write, reported), the refresh-token mint (accounts host, cache, 401 retry, error text without
secrets, token never logged), CLI, retry, canonical locales, NEEDS_REVIEW exclusion.

ASCII-only prints; final line is 'verify_zoho OK'.
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

init_env("loctms_zoho_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Connection, Project, Segment, SegStatus, TargetLocale
from app.services import connectors as _reg
from app.services import languages
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors import zoho as zmod
from app.services.connectors.base import ConnectorError
from app.services.connectors.strategy import ABSENT, SubResourceStrategy
from app.services.connectors.zoho import ZohoConnector, _is_stale_state

DC_HOST = "desk.zoho.eu"  # a NON-.com DC on purpose (multi-DC gotcha)
ACCOUNTS_HOST = "accounts.zoho.eu"
ORG_ID = "600000000042"
STATIC_TOKEN = "1000.static-token-aaa"  # pregate: allow-secret offline fixture
CLIENT_ID = "1000.CLIENTIDXYZ"  # pregate: allow-secret offline fixture
CLIENT_SECRET = "client-secret-qqq"  # pregate: allow-secret offline fixture
REFRESH_TOKEN = "1000.refresh-token-rrr"  # pregate: allow-secret offline fixture
NEEDS_REVIEW = SegStatus.NEEDS_REVIEW


class FakeZoho:
    """A fake Zoho Desk tenant plus its accounts server."""

    def __init__(self, articles=None, api_host=DC_HOST, org_id=ORG_ID, accept=None):
        self.api_host = api_host
        self.org_id = org_id
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        self.translations: dict = {}
        self.writes: list = []  # (article_id, locale, method, payload)
        self.hosts_seen: set = set()
        self.paths_seen: list = []
        self.orgids_seen: list = []
        self.auth_seen: list = []
        self.paged_limits: list = []
        self.paged_froms: list = []
        self.token_posts: list = []  # form dicts posted to the accounts host
        self.token_n = 0
        self.token_mode = "ok"  # ok | error200 | http400
        self.accept = accept  # None = any token; else a set of accepted API tokens
        self.revoke_next = False  # answer 401 once, whatever the token
        self._rate_state = {"n": 0}
        self.rate_limit_once_on = None

    def add_translation(self, article_id, locale, title="", answer="", status="Draft", **extra):
        d = {
            "id": str(article_id),
            "locale": locale,
            "title": title,
            "answer": answer,
            "status": status,
        }
        d.update(extra)
        self.translations[(str(article_id), locale)] = d

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        pfx = "/api/v1"
        self.hosts_seen.add(req.url.host)
        self.paths_seen.append((method, path))

        if req.url.host.startswith("accounts."):
            form = dict(httpx.QueryParams(req.content.decode()))
            self.token_posts.append({"host": req.url.host, "path": path, **form})
            if self.token_mode == "http400":
                return httpx.Response(400, json={"error": "invalid_client"})
            if self.token_mode == "error200":
                return httpx.Response(200, json={"error": "invalid_code"})
            self.token_n += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"1000.minted-{self.token_n}",
                    "expires_in": 3600,
                    "api_domain": "https://www.zohoapis.eu",
                    "token_type": "Bearer",
                },
            )

        self.orgids_seen.append(req.headers.get("orgId"))
        self.auth_seen.append(req.headers.get("Authorization"))
        tok = (req.headers.get("Authorization") or "").replace("Zoho-oauthtoken ", "")
        if self.revoke_next:
            self.revoke_next = False
            return httpx.Response(401, json={"errorCode": "INVALID_OAUTH"})
        if self.accept is not None and tok not in self.accept:
            return httpx.Response(401, json={"errorCode": "INVALID_OAUTH"})

        if self.rate_limit_once_on and self.rate_limit_once_on in path:
            self._rate_state["n"] += 1
            if self._rate_state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "rate"})

        if method == "GET" and path == f"{pfx}/kbRootCategories":
            return httpx.Response(200, json={"data": [{"id": "800", "name": "Getting Started"}]})

        if method == "GET" and "/translations/" in path:
            parts = path.split("/articles/")[1].split("/")
            aid, locale = parts[0], parts[-1]
            tr = self.translations.get((aid, locale))
            if tr is not None:
                return httpx.Response(200, json=dict(tr))
            return httpx.Response(404, json={"errorCode": "URL_NOT_FOUND"})

        if method == "POST" and path.endswith("/translations"):
            aid = path.split("/articles/")[1].split("/")[0]
            body = json.loads(req.content)
            locale = body.get("locale")
            self.writes.append((aid, locale, "POST", body))
            self.translations[(aid, locale)] = {"id": aid, **body}
            return httpx.Response(201, json={"id": aid, **body})

        if method == "PATCH" and "/translations/" in path:
            parts = path.split("/articles/")[1].split("/")
            aid, locale = parts[0], parts[-1]
            body = json.loads(req.content)
            self.writes.append((aid, locale, "PATCH", body))
            cur = self.translations.get((aid, locale), {"id": aid, "locale": locale})
            self.translations[(aid, locale)] = {**cur, **body}
            return httpx.Response(200, json={"id": aid, **body})

        if method == "GET" and path.startswith(f"{pfx}/articles/") and path.count("/") == 4:
            aid = path.rsplit("/", 1)[-1]
            art = self.articles.get(aid)
            if art is not None:
                return httpx.Response(200, json=dict(art))
            return httpx.Response(404, json={"errorCode": "URL_NOT_FOUND"})

        if method == "GET" and path == f"{pfx}/articles":
            self.paged_limits.append(req.url.params.get("limit"))
            self.paged_froms.append(req.url.params.get("from"))
            cat = req.url.params.get("categoryId")
            rows = [
                {"id": aid, "categoryId": a.get("categoryId")}
                for aid, a in self.articles.items()
                if not cat or a.get("categoryId") == cat
            ]
            return httpx.Response(200, json={"data": rows})

        return httpx.Response(404, json={"error": "unexpected", "path": path, "method": method})


def _article(
    aid,
    title="Reset password",
    answer="<h1>Reset</h1><p>Click the reset link to reset.</p>",
    status="Published",
    **extra,
):
    a = {
        "id": aid,
        "title": title,
        "status": status,
        "modifiedTime": "2026-07-05T12:00:00.000Z",
        "answer": answer,
        "authorId": "4000000000059",
    }
    a.update(extra)
    return a


def _zconn(name, extra_cfg=None, secret=None):
    cfg = {
        "api_domain": DC_HOST,
        "org_id": ORG_ID,
        "source_locale": "en",
        "target_locales": "de, fr",
    }
    cfg.update(extra_cfg or {})
    sec = {"access_token": STATIC_TOKEN}
    if secret is not None:
        sec = secret
    return cstore.create("zoho", name, cfg, sec, actor="admin")


def _bare(cfg=None, secret=None, transport=None):
    c = {"api_domain": DC_HOST, "org_id": ORG_ID, "source_locale": "en"}
    c.update(cfg or {})
    return ZohoConnector(c, secret or {"access_token": STATIC_TOKEN}, transport=transport)


def _flip(locale_to_hold, order_index):
    """pretranslate wrapper that marks one segment of one locale NEEDS_REVIEW."""
    import app.services.translate as translate_mod

    real = translate_mod.pretranslate_document

    def _ptd(session, doc_id, locale, spec=None, force_pii=False):
        stats = real(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == locale_to_hold:
            seg = session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id,
                    Segment.target_locale == locale,
                    Segment.order_index == order_index,
                )
            ).first()
            if seg:
                seg.status = NEEDS_REVIEW
                session.add(seg)
                session.commit()
        return stats

    return real, _ptd


def main() -> None:
    init_db()

    # ---- (0) framework seam ----
    assert SubResourceStrategy().topology == "T2"
    assert ZohoConnector.capability.topology == "T2"
    assert ZohoConnector.capability.change_signals.webhook_sig is None
    assert ZohoConnector.capability.change_signals.stale_flag == "translationState"
    assert ZohoConnector.capability.regions == "dynamic-from-token"
    assert zmod.SPEC.archived is False, "Zoho Desk is un-archived by the rebuild"
    print("  framework: T2 strategy, poll-first, translationState stale flag, un-archived")

    # ---- (1) stale-state classifier (spec enum: OUTDATED, UP-TO-DATE, AUTO-TRANSLATE FAILED) ----
    assert _is_stale_state("OUTDATED") and _is_stale_state("Outdated")
    assert _is_stale_state("AUTO-TRANSLATE FAILED")
    assert not _is_stale_state(None) and not _is_stale_state("UP-TO-DATE")
    assert not _is_stale_state("Translated")
    print("  stale-state: OUTDATED / AUTO-TRANSLATE FAILED stale; missing/up-to-date/unknown not")

    # ---- (2) connector: test + enabled_locales + discover ----
    fz = FakeZoho(articles=[_article("100"), _article("101", title="Billing")])
    zc = _bare({"target_locales": "de, fr"}, transport=fz.transport())
    ok, msg = zc.test()
    assert ok, msg
    assert set(zc.enabled_locales()) == {"en", "de", "fr"}, zc.enabled_locales()
    arts = zc.discover_articles()
    assert sorted(a["id"] for a in arts) == ["100", "101"]
    assert fz.hosts_seen == {DC_HOST}, ("all requests go to the configured DC", fz.hosts_seen)
    assert fz.orgids_seen and all(o == ORG_ID for o in fz.orgids_seen), set(fz.orgids_seen)
    assert set(fz.auth_seen) == {f"Zoho-oauthtoken {STATIC_TOKEN}"}
    assert ("GET", "/api/v1/kbRootCategories") not in fz.paths_seen
    assert zc.containers() == [{"id": "800", "name": "Getting Started (KB 800)", "tables": []}]
    print("  connector: test + enabled_locales + discover; DC host, orgId and auth on every call")

    # ---- (3) host allowlist: the credential only ever goes to a real Zoho host ----
    for bad in (
        "evil.example",
        "desk.zoho.com.evil.example",
        "desk.zoho.com:8443",
        "https://user:pw@desk.zoho.com",
        "desk.zoho.com/api/v1/articles",
        "127.0.0.1",
        "desk.zoho.eu.attacker.net",
    ):
        try:
            _bare({"api_domain": bad})._base()
            raise AssertionError(f"host {bad!r} must be refused")
        except ConnectorError:
            pass
    assert _bare({"api_domain": "http://desk.zoho.eu"})._base() == "https://desk.zoho.eu"
    assert _bare({"api_domain": "DESK.ZOHO.COM.AU/"})._base() == "https://desk.zoho.com.au"
    assert _bare({"api_domain": "desk.zoho.eu"})._accounts_base() == "https://accounts.zoho.eu"
    assert _bare({"api_domain": "desk.zohocloud.ca"})._accounts_base().endswith("zohocloud.ca")
    print("  host pin: only Zoho Desk hosts allowed; accounts host derived; http upgraded")

    # ---- (4) extract: `answer` is the body key ----
    from app.filters import get_filter

    units, _h, bk = zc.extract_units(_article("100"), get_filter("html"))
    assert bk == "answer" and units[0]["field"] == "title" and units[1]["field"] == "body"
    print("  extract: title + html answer")

    # ---- (5) CREATE: POST with the spec's required fields ----
    fz1 = FakeZoho(articles=[_article("100")])
    conn = _zconn("Zoho create")
    ok, msg, summary = cstore.zoho_translate(conn.id, actor="auto", transport=fz1.transport())
    assert ok, msg
    assert summary["created"] == 2 and summary["updated"] == 0, summary
    for _a, loc, method, body in fz1.writes:
        assert method == "POST"
        assert body["locale"] == loc and body["status"] == "Draft", body
        assert body["authorId"] == "4000000000059", ("author from the source article", body)
        assert body["title"] and "answer" in body and "content" not in body, body
        assert body["translationState"] == "UP-TO-DATE", body
        assert set(body) <= {"locale", "title", "answer", "authorId", "status", "translationState"}
    print("  CREATE: 2 locales POSTed with locale/title/answer/authorId/status, Draft")

    # config author_id wins over the source author
    fz1b = FakeZoho(articles=[_article("100")])
    c1b = _zconn("Zoho author", {"author_id": "4000000000777", "target_locales": "de"})
    cstore.zoho_translate(c1b.id, actor="auto", transport=fz1b.transport())
    assert fz1b.writes[0][3]["authorId"] == "4000000000777", fz1b.writes
    # no author anywhere -> clean per-pair failure, nothing written
    noauth = _article("100")
    noauth.pop("authorId")
    fz1c = FakeZoho(articles=[noauth])
    c1c = _zconn("Zoho noauthor", {"target_locales": "de"})
    ok, msg, _s1c = cstore.zoho_translate(c1c.id, actor="auto", transport=fz1c.transport())
    assert not ok and not fz1c.writes and "Author agent id" in msg, (ok, msg, fz1c.writes)
    print("  CREATE: author_id config wins; no author at all -> clear failure, no write")

    # ---- (6) UPDATE: PATCH, partial body, no `locale`, existing draft ----
    fz2 = FakeZoho(articles=[_article("100")])
    fz2.add_translation("100", "de", title="Alt", answer="<p>alt</p>")
    conn2 = _zconn("Zoho update")
    ok, msg, s2 = cstore.zoho_translate(conn2.id, actor="auto", transport=fz2.transport())
    assert ok and s2["created"] == 1 and s2["updated"] == 1, (msg, s2)
    methods = {(loc, m) for _a, loc, m, _b in fz2.writes}
    assert methods == {("de", "PATCH"), ("fr", "POST")}, methods
    patch = next(b for _a, loc, m, b in fz2.writes if m == "PATCH")
    assert "locale" not in patch and set(patch) == {"title", "answer", "translationState"}, patch
    assert "PUT" not in {m for m, _p in fz2.paths_seen}, "the API has no PUT for a translation"
    print("  UPDATE: existing de PATCHed (title, answer, translationState), fr POSTed, no PUT")

    # ---- (7) suppression by hash; changed source re-translates; translationState stale ----
    art_v1 = _article("200")
    fz3 = FakeZoho(articles=[art_v1])
    conn3 = _zconn("Zoho suppress")
    ok, _m, s3a = cstore.zoho_translate(conn3.id, actor="auto", transport=fz3.transport())
    assert s3a["created"] == 2, s3a
    fz4 = FakeZoho(articles=[art_v1])
    fz4.add_translation("200", "de", translationState="UP-TO-DATE")
    fz4.add_translation("200", "fr")
    ok, _m, s3b = cstore.zoho_translate(conn3.id, actor="auto", transport=fz4.transport())
    assert ok and s3b["skipped"] == 2 and not fz4.writes, (s3b, fz4.writes)
    art_v2 = _article("200", answer="<h1>Reset</h1><p>Brand new steps here.</p>")
    fz5 = FakeZoho(articles=[art_v2])
    fz5.add_translation("200", "de")
    fz5.add_translation("200", "fr")
    ok, _m, s3c = cstore.zoho_translate(conn3.id, actor="auto", transport=fz5.transport())
    assert ok and s3c["updated"] == 2 and s3c["skipped"] == 0, s3c
    fz9 = FakeZoho(articles=[art_v2])
    fz9.add_translation("200", "de", translationState="OUTDATED")
    fz9.add_translation("200", "fr", translationState="UP-TO-DATE")
    ok, _m, s9 = cstore.zoho_translate(conn3.id, actor="auto", transport=fz9.transport())
    assert ok and s9["skipped"] == 1 and s9["updated"] == 1, s9
    assert {loc for _a, loc, _m2, _b in fz9.writes} == {"de"}, fz9.writes
    print("  suppression: unchanged -> skipped; changed -> PATCH; OUTDATED re-enqueues only de")

    # ---- (8) locale availability ----
    fz10 = FakeZoho(articles=[_article("100")])
    conn10 = _zconn("Zoho reject", {"target_locales": "de"})
    ok, msg, s10 = cstore.zoho_translate(
        conn10.id, target_locales="de, fr, ja", actor="auto", transport=fz10.transport()
    )
    assert ok and s10["locales"] == ["de"] and set(s10["rejected"]) == {"fr", "ja"}, s10
    ok, msg, s10b = cstore.zoho_translate(
        conn10.id,
        target_locales="fr, ja",
        actor="auto",
        transport=FakeZoho(articles=[_article("100")]).transport(),
    )
    assert not ok and s10b["locales"] == [], (ok, s10b)
    with Session(engine) as s:
        assert s.get(Connection, conn10.id).status == "error"
    # malformed locale codes never become a URL path
    fz10c = FakeZoho(articles=[_article("100")])
    conn10c = _zconn("Zoho badloc", {"target_locales": "de, ../x, d%65, fr/1"})
    ok, msg, s10c = cstore.zoho_translate(conn10c.id, actor="auto", transport=fz10c.transport())
    assert s10c["locales"] == ["de"] and len(s10c["rejected"]) == 3, s10c
    assert all(".." not in p and "%" not in p for _m, p in fz10c.paths_seen), fz10c.paths_seen
    print("  locales: not-enabled dropped; all-rejected -> error; malformed codes rejected")

    # ---- (9) publish toggle (only a Published source goes live) ----
    fz11 = FakeZoho(articles=[_article("100"), _article("101", status="Draft")])
    conn11 = _zconn("Zoho publish", {"publish": "1", "target_locales": "de"})
    ok, msg, _s = cstore.zoho_translate(conn11.id, actor="auto", transport=fz11.transport())
    by_art = {a: b["status"] for a, _l, _m, b in fz11.writes}
    assert by_art == {"100": "Published", "101": "Draft"}, by_art
    fz11b = FakeZoho(articles=[_article("100")])
    conn11b = _zconn("Zoho draft", {"target_locales": "de"})
    cstore.zoho_translate(conn11b.id, actor="auto", transport=fz11b.transport())
    assert fz11b.writes[0][3]["status"] == "Draft"
    print("  publish toggle: publish=1 -> Published only for a Published source; default Draft")

    # ---- (10) 429 backoff ----
    slept = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        fz12 = FakeZoho(articles=[_article("100")])
        fz12.rate_limit_once_on = "/articles"
        ok, _m = _bare(transport=fz12.transport()).test()
        assert ok and slept == [5.0], (_m, slept)
    finally:
        cbase._sleep = real_sleep
    print("  backoff: 429 + Retry-After retried once (5s)")

    # ---- (11) pagination at the 50 cap ----
    class _Paged:
        def __init__(self, total=120, ignore_from=False):
            self.limits, self.froms, self.total, self.ignore = [], [], total, ignore_from
            self.calls = 0

        def handler(self, req: httpx.Request) -> httpx.Response:
            if not req.url.path.endswith("/articles") or req.method != "GET":
                aid = req.url.path.rsplit("/", 1)[-1]
                return httpx.Response(200, json={"id": aid, "title": "x", "answer": "<p>x</p>"})
            self.calls += 1
            lim, frm = int(req.url.params.get("limit")), int(req.url.params.get("from"))
            self.limits.append(lim)
            self.froms.append(frm)
            if self.ignore:
                frm = 1
            end = min(frm - 1 + lim, self.total)
            return httpx.Response(200, json={"data": [{"id": str(i)} for i in range(frm, end + 1)]})

    pt = _Paged()
    got = _bare({"max_articles": "120"}, transport=httpx.MockTransport(pt.handler))
    assert len(got.discover_articles(cap=120)) == 120
    assert all(x <= 50 for x in pt.limits) and pt.froms[:3] == [1, 51, 101], (pt.limits, pt.froms)
    fit = _Paged(total=50, ignore_from=True)
    got2 = _bare({"max_articles": "500"}, transport=httpx.MockTransport(fit.handler))
    assert len(got2.discover_articles(cap=500)) == 50 and fit.calls == 2, fit.calls
    print("  pagination: limit<=50, from 1->51->101; a from-ignoring server stops after one page")

    # ---- (12) org scoping ----
    fz14 = FakeZoho(articles=[_article("100")])
    conn14 = _zconn("Zoho org")
    cstore.zoho_translate(conn14.id, actor="auto", transport=fz14.transport())
    row14 = cstore.get(conn14.id)
    with Session(engine) as s:
        proj = s.get(Project, row14.project_id)
    assert proj is not None and getattr(proj, "org_id", None) == getattr(row14, "org_id", None)
    print("  org scoping: project inherits the connection's org")

    # ---- (13) strategy probe ----
    fz15 = FakeZoho(articles=[_article("100")])
    zc15 = _bare(transport=fz15.transport())
    assert zc15.get_translation("100", "de") is ABSENT
    fz15.add_translation("100", "de", title="Exists")
    assert zc15.get_translation("100", "de").get("title") == "Exists"
    print("  strategy probe: 404 -> ABSENT; populated -> present")

    # ---- (14) routes: un-archived, add form shown ----
    c = TestClient(app)
    assert "Zoho Desk" in c.get("/connectors").text
    typ = c.get("/connectors/zoho").text
    assert "New connections are closed" not in typ, "Zoho Desk is un-archived"
    names = {f.name for f in _reg.spec("zoho").config_fields} | {
        f.name for f in _reg.spec("zoho").secret_fields
    }
    assert {"api_domain", "org_id", "client_id", "client_secret", "refresh_token"} <= names
    print("  routes: type page open for new connections; spec fields present")

    # ---- (15) CLI ----
    fz17 = FakeZoho(articles=[_article("100")])
    connc = _zconn("Zoho cli", {"target_locales": "de"})
    real_zt = cstore.zoho_translate
    seen = {}

    def _capturing_zt(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        seen["called"] = True
        return real_zt(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=fz17.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "zoho_translate", _capturing_zt)
    try:
        from app.cli.cmd_connector import _zoho_translate

        class _Args:
            connection_id = connc.id
            article_ids = ""
            locales = ""
            actor = "cli"
            local = True
            org = None
            as_json = True

        emitted = {}

        class _Emit:
            as_json = True

            def result(self, text, data):
                emitted.update(data)

            def json(self, data):
                emitted.update(data)

        rc = _zoho_translate(_Args(), _Emit())
        assert rc == 0 and emitted.get("ok") and seen.get("called"), (rc, emitted)
        assert {loc for _a, loc, _m, _b in fz17.writes} == {"de"}
    finally:
        set_pieces("app.services.connectors.store", "zoho_translate", real_zt)
    print("  CLI: zoho-translate ran the flow")

    # ---- (16) retry_failed re-runs ONLY the failed pairs ----
    fz18 = FakeZoho(articles=[_article("100"), _article("101", title="Other")])
    connret = _zconn("Zoho retry")
    failures = [
        {"article_id": "100", "locale": "de", "error": "500"},
        {"article_id": "101", "locale": "fr", "error": "400"},
    ]
    okr, _m, sumr = cstore.retry_failed(
        connret.id, failures, actor="auto", transport=fz18.transport()
    )
    assert okr, sumr
    assert {(a, loc) for a, loc, _m2, _b in fz18.writes} == {("100", "de"), ("101", "fr")}
    print("  retry: re-ran ONLY the 2 failed pairs")

    # ---- (17) canonical TargetLocale rows; lowercase wire ----
    fz19 = FakeZoho(articles=[_article("100")])
    conn19 = _zconn("Zoho canonical", {"target_locales": "pt-br"})
    ok, _m, _s19 = cstore.zoho_translate(conn19.id, actor="auto", transport=fz19.transport())
    assert ok and {loc for _a, loc, _m2, _b in fz19.writes} == {"pt-br"}
    with Session(engine) as s:
        stored = {
            tl.locale
            for tl in s.exec(
                select(TargetLocale).where(
                    TargetLocale.project_id == cstore.get(conn19.id).project_id
                )
            ).all()
        }
    assert "pt-BR" in stored, stored
    print("  canonical: TargetLocale pt-BR, wire pt-br")

    # ---- (18) empty body skipped; other-language article skipped ----
    fz20 = FakeZoho(
        articles=[
            _article("400", answer=""),
            _article("401", locale="de"),
            _article("402", locale="en"),
        ]
    )
    conn20 = _zconn("Zoho skips", {"target_locales": "fr"})
    ok, msg, s20 = cstore.zoho_translate(conn20.id, actor="auto", transport=fz20.transport())
    assert ok and s20["empty_skipped"] == 1 and s20["other_locale_skipped"] == 1, s20
    assert {a for a, _l, _m, _b in fz20.writes} == {"402"}, fz20.writes
    assert "empty-body" in msg and "not written in the source locale" in msg, msg
    print("  skips: empty body and a German source article are skipped, the English one written")

    # ---- (19) #640 held units ----
    # (a) held BODY unit, CREATE: de is not created at all, fr is.
    real, wrapped = _flip("de", 1)

    set_pieces("app.services.translate", "pretranslate_document", wrapped)
    try:
        fz21 = FakeZoho(articles=[_article("600")])
        conn21 = _zconn("Zoho held create")
        ok, msg, s21 = cstore.zoho_translate(conn21.id, actor="auto", transport=fz21.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real)
    assert ok, msg
    assert not [w for w in fz21.writes if w[1] == "de"], "no de created while a unit is held"
    assert [w for w in fz21.writes if w[1] == "fr"], "fr still written"
    assert s21.get("held_back") == 1, s21
    assert "600:de" not in (cstore.get(conn21.id).config.get("zoho_hashes") or {}), "no hash"

    # (b) held BODY unit, UPDATE: title written, answer omitted (stored body kept), still OUTDATED
    set_pieces("app.services.translate", "pretranslate_document", wrapped)
    try:
        fz22 = FakeZoho(articles=[_article("601")])
        fz22.add_translation("601", "de", title="Alt", answer="<p>stored human body</p>")
        conn22 = _zconn("Zoho held update body", {"target_locales": "de"})
        ok, msg, s22 = cstore.zoho_translate(conn22.id, actor="auto", transport=fz22.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real)
    (w,) = fz22.writes
    assert w[2] == "PATCH" and "answer" not in w[3] and w[3]["title"] != "Alt", w
    assert "translationState" not in w[3], ("a partly held pair is not marked up to date", w[3])
    assert fz22.translations[("601", "de")]["answer"] == "<p>stored human body</p>"
    assert s22.get("partial_held") == 1, s22
    assert "601:de" not in (cstore.get(conn22.id).config.get("zoho_hashes") or {})

    # (c) held TITLE, UPDATE: answer written, title omitted (never the source title)
    real2, wrapped0 = _flip("de", 0)
    set_pieces("app.services.translate", "pretranslate_document", wrapped0)
    try:
        fz23 = FakeZoho(articles=[_article("602", title="Source Title")])
        fz23.add_translation("602", "de", title="Gespeicherter Titel", answer="<p>alt</p>")
        conn23 = _zconn("Zoho held update title", {"target_locales": "de"})
        ok, msg, _s23 = cstore.zoho_translate(conn23.id, actor="auto", transport=fz23.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real2)
    (w,) = fz23.writes
    assert "title" not in w[3] and "answer" in w[3], w
    assert fz23.translations[("602", "de")]["title"] == "Gespeicherter Titel"
    assert "Source Title" not in json.dumps(w[3])

    # (d) held BODY unit, UPDATE, publish=1 + Published source: the Draft translation is NOT
    # promoted to Published (a part-stale translation never goes live; Review gate 10-10).
    real3, wrapped3 = _flip("de", 1)
    set_pieces("app.services.translate", "pretranslate_document", wrapped3)
    try:
        fz24 = FakeZoho(articles=[_article("603")])
        fz24.add_translation("603", "de", title="Alt", answer="<p>stored body</p>", status="Draft")
        conn24 = _zconn("Zoho held publish", {"publish": "1", "target_locales": "de"})
        ok, msg, _s24 = cstore.zoho_translate(conn24.id, actor="auto", transport=fz24.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real3)
    (w,) = fz24.writes
    assert w[2] == "PATCH" and "status" not in w[3], ("held pair must not be published", w)
    assert fz24.translations[("603", "de")]["status"] == "Draft", fz24.translations
    print("  #640: held create refused; held body/title omitted on update; no hash; stays OUTDATED")
    print("  #640: publish=1 never promotes a held (partly translated) translation to Published")

    # ---- (20) id and scope validation before any request ----
    fzv = FakeZoho(
        articles=[
            _article("100", categoryId="800", departmentId="900"),
            _article("101", categoryId="801", departmentId="900"),
            _article("102", categoryId="800", departmentId="901"),
        ]
    )
    for bad in ("../x", "1/../2", "12 34", "1?x=1", "١٢", "1\r\n2", "%2e%2e", ""):
        zcv = _bare(transport=fzv.transport())
        try:
            if bad == "":
                # an empty id in a list is simply skipped by the caller; direct fetch refuses
                zcv.show_article(bad)
            else:
                zcv.discover_articles(article_ids=[bad])
            raise AssertionError(f"article id {bad!r} must be refused")
        except ConnectorError:
            pass
    assert fzv.paths_seen == [], ("refused before any request", fzv.paths_seen)
    try:
        _bare(transport=fzv.transport()).get_translation("100", "../de")
        raise AssertionError("bad locale must be refused")
    except ConnectorError:
        pass
    zcs = _bare({"category_id": "800"}, transport=fzv.transport())
    assert sorted(a["id"] for a in zcs.discover_articles()) == ["100", "102"]
    got_ids = zcs.discover_articles(article_ids=["100", "101", "999"])
    assert [a["id"] for a in got_ids] == ["100"], ("foreign category/missing dropped", got_ids)
    zds = _bare({"department_id": "900", "category_id": "800"}, transport=fzv.transport())
    assert [a["id"] for a in zds.discover_articles()] == ["100"]
    assert [a["id"] for a in zds.discover_articles(article_ids=["102"])] == []
    n_before = len(fzv.paths_seen)
    ok_org, _m = _bare({"org_id": "6000/../1"}, transport=fzv.transport()).test()
    assert not ok_org and len(fzv.paths_seen) == n_before, "a bad orgId makes no request"
    print("  validation: bad ids/locales refused before any request; category + department scope")

    # ---- (21) DRAFT MODE never changes a live translation ----
    calls = {"n": 0}
    import app.services.translate as tm

    real_ptd = tm.pretranslate_document

    def _count_ptd(session, doc_id, locale, spec=None, force_pii=False):
        calls["n"] += 1
        return real_ptd(session, doc_id, locale, spec, force_pii=force_pii)

    set_pieces("app.services.translate", "pretranslate_document", _count_ptd)
    try:
        fzl = FakeZoho(articles=[_article("700")])
        fzl.add_translation("700", "de", title="Live", answer="<p>live</p>", status="Published")
        connl = _zconn("Zoho live kept")
        ok, msg, sl = cstore.zoho_translate(connl.id, actor="auto", transport=fzl.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert ok, msg
    assert [w[1] for w in fzl.writes] == ["fr"] and fzl.writes[0][2] == "POST", fzl.writes
    assert fzl.translations[("700", "de")]["answer"] == "<p>live</p>", "live text untouched"
    assert [r["locale"] for r in sl["live_kept"]] == ["de"], sl
    assert "LIVE" in msg and "Publish after write = 1" in msg, msg
    assert calls["n"] == 1, ("MT spent for fr only, not for the live de", calls)
    assert "700:de" not in (cstore.get(connl.id).config.get("zoho_hashes") or {})
    # an explicit retry bypasses the pre-MT hook, the write-time guard still refuses
    okr, _m, _sr = cstore.retry_failed(
        connl.id,
        [{"article_id": "700", "locale": "de", "error": "x"}],
        actor="auto",
        transport=fzl.transport(),
    )
    assert fzl.translations[("700", "de")]["answer"] == "<p>live</p>", "retry kept live text"
    assert not [w for w in fzl.writes if w[1] == "de"], fzl.writes
    # publish mode DOES update it
    fzp = FakeZoho(articles=[_article("701")])
    fzp.add_translation("701", "de", title="Live", answer="<p>live</p>", status="Published")
    connp = _zconn("Zoho publish live", {"publish": "1", "target_locales": "de"})
    ok, msg, sp = cstore.zoho_translate(connp.id, actor="auto", transport=fzp.transport())
    assert ok and sp["updated"] == 1 and fzp.writes[0][3]["status"] == "Published", sp
    print("  draft mode: a Published translation is kept live, no MT spent, reported; publish=1 ok")

    # ---- (22) Language Set locales are not pre-rejected ----
    ls = languages.create("Zoho EU", "en", "de-DE, fr-FR")
    fz24 = FakeZoho(articles=[_article("500")])
    conn24 = _zconn("Zoho langset", {"target_locales": "", "language_set_id": str(ls.id)})
    ok, msg, s24 = cstore.zoho_translate(conn24.id, actor="auto", transport=fz24.transport())
    assert ok and set(s24["locales"]) == {"de-de", "fr-fr"} and not s24["rejected"], s24
    print("  language set: blank target_locales + set -> locales usable")

    # ---- (23) GET probe 403 -> per-pair failure ----
    class _Probe403(FakeZoho):
        def _handle(self, req):
            if req.method == "GET" and "/translations/" in req.url.path:
                return httpx.Response(403, json={"error": "restricted"})
            return super()._handle(req)

    fzp3 = _Probe403(articles=[_article("950")])
    connp3 = _zconn("Zoho probe-403")
    ok, msg, sp3 = cstore.zoho_translate(connp3.id, actor="auto", transport=fzp3.transport())
    assert not ok and sp3["created"] == 0 and len(sp3["failures"]) == 2 and not fzp3.writes
    print("  probe 403: per-pair failures, no crash, no write")

    # ---- (24) AUTH: refresh token -> access token on the accounts host ----
    zmod._TOKEN_CACHE.clear()
    sec = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "refresh_token": REFRESH_TOKEN}
    fza = FakeZoho(articles=[_article("100")], accept={"1000.minted-1", "1000.minted-2"})
    conna = _zconn("Zoho refresh", {"target_locales": "de"}, secret=sec)
    log_records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            log_records.append(record.getMessage())

    h = _Capture()
    logging.getLogger().addHandler(h)
    old_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        ok, msg, sa = cstore.zoho_translate(conna.id, actor="auto", transport=fza.transport())
    finally:
        logging.getLogger().removeHandler(h)
        logging.getLogger().setLevel(old_level)
    assert ok, msg
    assert len(fza.token_posts) == 1, ("one mint for the whole run", fza.token_posts)
    tp = fza.token_posts[0]
    assert tp["host"] == ACCOUNTS_HOST and tp["path"] == "/oauth/v2/token", tp
    assert tp["grant_type"] == "refresh_token" and tp["refresh_token"] == REFRESH_TOKEN
    assert tp["client_id"] == CLIENT_ID and tp["client_secret"] == CLIENT_SECRET
    assert set(fza.auth_seen) == {"Zoho-oauthtoken 1000.minted-1"}, fza.auth_seen
    blob = " ".join(log_records) + msg + json.dumps(sa)
    for secret in (CLIENT_SECRET, REFRESH_TOKEN, "1000.minted-1", CLIENT_ID):
        assert secret not in blob, "a credential leaked into a log line or a result"
    assert fza.hosts_seen <= {DC_HOST, ACCOUNTS_HOST}
    # a second connector in the same process reuses the cached token (10 tokens / 10 min cap)
    fza2 = FakeZoho(articles=[_article("100")])
    zc2 = _bare(secret=sec, transport=fza2.transport())
    assert zc2.test()[0] and not fza2.token_posts, "cached token reused"
    # the cache expires
    real_clock = zmod._clock
    zmod._clock = lambda: real_clock() + 4000
    try:
        assert zc2.test()[0] and len(fza2.token_posts) == 1, "expired token re-minted"
    finally:
        zmod._clock = real_clock
    print("  auth: refresh token minted on the DC accounts host, cached, never logged or returned")

    # 401 on a minted token -> forget, re-mint once, retry
    zmod._TOKEN_CACHE.clear()
    fzr = FakeZoho(articles=[_article("100")])
    zcr = _bare(secret=sec, transport=fzr.transport())
    assert zcr.test()[0]
    # a 401 inside the re-mint window must NOT mint again (a refused call cannot burn the 10 per
    # 10 minutes budget); the 401 is then reported, not looped
    fzr.revoke_next = True
    okw, _mw = zcr.test()
    assert not okw and fzr.token_n == 1, ("no re-mint inside the window", fzr.token_n)
    real_clock2 = zmod._clock
    zmod._clock = lambda: real_clock2() + 120
    try:
        fzr.revoke_next = True
        assert zcr.test()[0] and fzr.token_n == 2, ("re-minted after a 401", fzr.token_n)
        # a 401 for a token that a concurrent run already replaced: retry with the new one, no mint
        zmod._TOKEN_CACHE[zcr._token_key()] = (
            "1000.minted-other",
            real_clock2() + 9999,
            real_clock2(),
        )
        zcr._used_token = "1000.minted-stale"
        assert zcr._forget_token("1000.minted-stale") is True
        assert zmod._TOKEN_CACHE[zcr._token_key()][0] == "1000.minted-other"
        # an orgId complaint is reported as such
        r403 = httpx.Response(
            422, json={"errorCode": "INVALID_DATA", "message": "orgId is invalid"}
        )
        assert "organization id" in zmod._http_err(r403) and "orgId" in zmod._http_err(r403)
        assert zmod._DESK_TO_ACCOUNTS["desk.zohohq.in"] == "accounts.zohohq.in"
    finally:
        zmod._clock = real_clock2
    # error answered with HTTP 200 + {"error": ...}, and an HTTP 400: clean message, no secrets
    for mode in ("error200", "http400"):
        zmod._TOKEN_CACHE.clear()
        fze = FakeZoho(articles=[_article("100")])
        fze.token_mode = mode
        okc, mmsg = _bare(secret=sec, transport=fze.transport()).test()
        assert not okc and "refresh token" in mmsg, mmsg
        for secret in (CLIENT_SECRET, REFRESH_TOKEN, CLIENT_ID):
            assert secret not in mmsg, mmsg
        assert not fze.orgids_seen, "no API call is made without a token"
    # missing credentials
    okm, mm = _bare(secret={"client_id": CLIENT_ID}).test()
    assert not okm and "client id, client secret and refresh token" in mm, mm
    # a data center mismatch never sends the secret anywhere but the derived accounts host
    zmod._TOKEN_CACHE.clear()
    fzd = FakeZoho(articles=[_article("100")])
    _bare({"api_domain": "desk.zoho.com.au"}, secret=sec, transport=fzd.transport()).test()
    assert fzd.token_posts[0]["host"] == "accounts.zoho.com.au", fzd.token_posts
    print("  auth: 401 re-mints once; Zoho error answers are clean; DC-derived accounts host")

    # legacy pasted access token still works and never touches the accounts host
    fzs = FakeZoho(articles=[_article("100")], accept={STATIC_TOKEN})
    assert _bare(transport=fzs.transport()).test()[0] and not fzs.token_posts
    print("  auth: a pasted access token still works (no mint)")

    print("verify_zoho OK")


if __name__ == "__main__":
    main()
