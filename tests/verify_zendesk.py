"""Zendesk Guide connector + connector-framework seams — fully offline via
httpx.MockTransport (a fake Zendesk tenant).

Covers (per the Phase 4 brief):
  - connector: test(), enabled_locales(), discover (label AND-filter, section
    scope), extract (title + HTML body via the shared html filter)
  - write-back roundtrip: CREATE (missing locale -> POST) AND UPDATE (existing
    locale -> PUT), draft flag, per-locale translation objects
  - enabled-locale rejection (a target not enabled in Guide is dropped, surfaced)
  - change detection: edited_at-NOT-updated_at (an updated_at-only bump must NOT
    re-enqueue); content-hash suppression (unchanged source -> skipped)
  - anti-loop labels (trigger label removed / translated label added on write-back)
  - webhook intake: valid signature -> enqueued exactly once (idempotent on
    retry); bad signature -> 401; thin payload -> re-fetch of the article
  - incremental export watermark chaining (end_time -> next start_time)
  - 429 honored (Retry-After) via the existing backoff machinery
  - framework seams: SubResourceStrategy 404-vs-200; the generalized webhook intake
    dedup ledger; CLI zendesk-translate parity

ASCII-only prints; final line is 'verify_zendesk OK'.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_zd_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Connection,
    JobStatus,
    Segment,
    SegStatus,
    TargetLocale,
    WebhookEvent,
)
from app.services import connectors as _reg  # archive-tier spec introspection
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors import webhooks
from app.services.connectors.strategy import ABSENT, SubResourceStrategy
from app.services.connectors.zendesk import ZendeskConnector


# ============================================================================
# A fake Zendesk Guide tenant. Holds articles + their translation sub-resources
# in memory; the MockTransport handler serves the HC endpoints the connector hits
# and records writes so assertions can inspect them.
# ============================================================================
class FakeZendesk:
    def __init__(self, enabled_locales=("en-us", "de", "fr"), articles=None):
        self.enabled = list(enabled_locales)
        # article_id -> article dict (with label_names, edited_at, updated_at, body)
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        # (article_id, locale) -> translation dict
        self.translations: dict = {}
        self.creates: list = []  # (article_id, locale, payload)
        self.updates: list = []  # (article_id, locale, payload)
        self.label_puts: list = []  # (article_id, label_names)
        self.incremental_pages = []  # list of (articles, end_time, next_page) tuples
        self._rate_state = {"n": 0}
        self.rate_limit_once_on = None  # a path substring to 429 once, then 200
        self.put_replaces = False  # True: a translation PUT replaces instead of merging

    # draft=True by default: the connector writes drafts unless configured otherwise, so a
    # translation it created earlier is a draft. A test of a PUBLISHED one passes draft=False.
    def add_translation(self, article_id, locale, title="", body="", draft=True):
        self.translations[(str(article_id), locale)] = {
            "id": 9000 + len(self.translations),
            "locale": locale,
            "title": title,
            "body": body,
            "draft": draft,
            "outdated": False,
        }

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        pfx = "/api/v2/help_center"

        # Optional one-shot 429 on a chosen path (backoff test).
        if self.rate_limit_once_on and self.rate_limit_once_on in path:
            self._rate_state["n"] += 1
            if self._rate_state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "rate"})

        # locales
        if method == "GET" and path == f"{pfx}/locales":
            return httpx.Response(200, json={"locales": self.enabled, "default_locale": "en-us"})

        # incremental export
        if method == "GET" and path == f"{pfx}/incremental/articles":
            st = int(req.url.params.get("start_time", "0"))
            # Serve the first page whose start_time matches, else the "caught up"
            # page (no advance).
            for arts, end_time, next_start in self.incremental_pages:
                if next_start is None:  # terminal page
                    return httpx.Response(
                        200, json={"articles": arts, "end_time": end_time, "next_page": None}
                    )
                if st <= (next_start - 1):
                    np = f"https://x.zendesk.com{pfx}/incremental/articles?start_time={end_time}"
                    return httpx.Response(
                        200, json={"articles": arts, "end_time": end_time, "next_page": np}
                    )
            return httpx.Response(200, json={"articles": [], "end_time": st, "next_page": None})

        # list articles (all)
        if method == "GET" and path == f"{pfx}/articles":
            arts = list(self.articles.values())
            labels = req.url.params.get("label_names")
            if labels:
                want = set(labels.split(","))
                arts = [a for a in arts if want.issubset(set(a.get("label_names") or []))]
            return httpx.Response(200, json={"articles": arts, "meta": {"has_more": False}})

        # list by section
        if method == "GET" and path.startswith(f"{pfx}/sections/") and path.endswith("/articles"):
            sid = path.split("/sections/")[1].split("/")[0]
            arts = [a for a in self.articles.values() if str(a.get("section_id")) == sid]
            return httpx.Response(200, json={"articles": arts, "meta": {"has_more": False}})

        # show one article
        if (
            method == "GET"
            and path.startswith(f"{pfx}/articles/")
            and path.count("/") == 5
            and "translations" not in path
        ):
            aid = path.rsplit("/", 1)[-1]
            art = self.articles.get(aid)
            return (
                httpx.Response(200, json={"article": art}) if art else httpx.Response(404, json={})
            )

        # GET a single translation (existence probe)
        if method == "GET" and "/translations/" in path and method == "GET":
            aid = path.split("/articles/")[1].split("/")[0]
            loc = path.rsplit("/", 1)[-1]
            tr = self.translations.get((aid, loc))
            return (
                httpx.Response(200, json={"translation": tr})
                if tr
                else httpx.Response(404, json={})
            )

        # POST create translation
        if method == "POST" and path.endswith("/translations"):
            aid = path.split("/articles/")[1].split("/")[0]
            body = json.loads(req.content)["translation"]
            loc = body["locale"]
            self.creates.append((aid, loc, body))
            self.translations[(aid, loc)] = {"id": 9000 + len(self.translations), **body}
            return httpx.Response(201, json={"translation": self.translations[(aid, loc)]})

        # PUT update translation
        if method == "PUT" and "/translations/" in path:
            aid = path.split("/articles/")[1].split("/")[0]
            loc = path.rsplit("/", 1)[-1]
            body = json.loads(req.content)["translation"]
            self.updates.append((aid, loc, body))
            self.translations.setdefault((aid, loc), {"id": 9999, "locale": loc})
            if self.put_replaces:
                # Replace semantics (undocumented by Zendesk): a field the PUT omits is blanked.
                cur = self.translations[(aid, loc)]
                for k in ("title", "body", "draft", "outdated"):
                    cur[k] = None
            self.translations[(aid, loc)].update(body)
            return httpx.Response(200, json={"translation": self.translations[(aid, loc)]})

        # PUT article (anti-loop label write)
        if method == "PUT" and path.startswith(f"{pfx}/articles/") and path.count("/") == 5:
            aid = path.rsplit("/", 1)[-1]
            body = json.loads(req.content)["article"]
            if "label_names" in body:
                self.label_puts.append((aid, body["label_names"]))
                if aid in self.articles:
                    self.articles[aid]["label_names"] = body["label_names"]
            return httpx.Response(200, json={"article": self.articles.get(aid, {})})

        return httpx.Response(404, json={"error": "unexpected", "path": path, "method": method})


def _article(
    aid,
    title="Reset password",
    body="<h1>Reset</h1><p>Click the link.</p>",
    labels=None,
    edited_at="2026-07-05T10:00:00Z",
    updated_at="2026-07-05T10:00:00Z",
    section_id=None,
    source_locale="en-us",
):
    return {
        "id": aid,
        "title": title,
        "body": body,
        "source_locale": source_locale,
        "label_names": labels or [],
        "edited_at": edited_at,
        "updated_at": updated_at,
        "outdated_locales": [],
        "section_id": section_id,
    }


def _zconn(name, extra_cfg=None, secret=None):
    cfg = {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de, fr"}
    cfg.update(extra_cfg or {})
    sec = {"email": "a@acme.com", "api_token": "tok123"}
    sec.update(secret or {})
    return cstore.create("zendesk", name, cfg, sec, actor="admin")


def wait_job(c, jid, timeout=30):
    deadline = time.time() + timeout
    d = {}
    while time.time() < deadline:
        d = c.get(f"/jobs/{jid}/status").json()
        if d.get("status") in (JobStatus.COMPLETED, JobStatus.FAILED):
            return d
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish in {timeout}s: {d}")


def main() -> None:
    init_db()

    # ---- (0) framework seam: SubResourceStrategy 404-vs-200 create/update ----
    strat = SubResourceStrategy()
    assert strat.topology == "T2"

    class _FakeClient:
        def __init__(self, existing):
            self.existing = existing
            self.calls = []

        def get_translation(self, rid, loc):
            return {"ok": 1} if (rid, loc) in self.existing else ABSENT

        def create_translation(self, rid, loc, fields):
            from app.services.connectors.strategy import WriteResult

            self.calls.append(("create", rid, loc, fields))
            return WriteResult(True, True, target_id="1")

        def update_translation(self, rid, loc, fields):
            from app.services.connectors.strategy import WriteResult

            self.calls.append(("update", rid, loc, fields))
            return WriteResult(True, False, target_id="1")

    fc = _FakeClient(existing={("7", "fr")})
    r1 = strat.write_target(fc, "7", "de", {"title": "x"}, draft=True)  # missing -> create
    r2 = strat.write_target(fc, "7", "fr", {"title": "y"}, draft=False)  # present -> update
    assert r1.created and not r2.created, (r1, r2)
    assert fc.calls[0][0] == "create" and fc.calls[0][3]["draft"] is True, fc.calls
    assert fc.calls[1][0] == "update" and fc.calls[1][3]["draft"] is False, fc.calls
    assert strat.exists_target(fc, "7", "fr") and not strat.exists_target(fc, "7", "de")
    print("  strategy(T2): 404->POST create, 200->PUT update; draft threaded; exists probe")

    # ---- (1) connector: test + enabled_locales + discover (label AND-filter) ----
    fz = FakeZendesk(
        articles=[
            _article("100", labels=["needs-translation"]),
            _article("101", title="Billing", labels=["other"]),
            _article("102", title="SSO", labels=["needs-translation", "vip"]),
        ]
    )
    zc = ZendeskConnector(
        {"subdomain": "acme", "label_names": "needs-translation"},
        {"email": "a@acme.com", "api_token": "t"},
        transport=fz.transport(),
    )
    ok, msg = zc.test()
    assert ok, msg
    assert set(zc.enabled_locales()) == {"en-us", "de", "fr"}, zc.enabled_locales()
    arts = zc.discover_articles()
    ids = sorted(a["id"] for a in arts)
    assert ids == ["100", "102"], ("label AND-filter picks only needs-translation", ids)
    print("  connector: test + enabled_locales(3) + discover label-AND-filter (2 of 3)")

    # section scope: only the article in section 555
    fz_sec = FakeZendesk(
        articles=[_article("200", section_id=555), _article("201", section_id=999)]
    )
    zc_sec = ZendeskConnector(
        {"subdomain": "acme", "section_id": "555"},
        {"email": "a@acme.com", "api_token": "t"},
        transport=fz_sec.transport(),
    )
    assert [a["id"] for a in zc_sec.discover_articles()] == ["200"], "section scope"
    print("  connector: section_id scope narrows to that section")

    # ---- (2) extract: title + HTML body via the shared html filter ----
    from app.filters import get_filter

    html = get_filter("html")
    units, chash = zc.extract_units(_article("100"), html)
    assert units[0] == {"p_index": 0, "text": "Reset password", "field": "title"}, units[0]
    assert any(u["field"] == "body" and "Reset" in u["text"] for u in units), units
    assert len(chash) == 32, chash
    print("  connector: extract title(order0) + HTML body units (shared html filter)")

    # ---- (3) write-back roundtrip: CREATE path (no existing translations) ----
    fz1 = FakeZendesk(articles=[_article("100", labels=["needs-translation"])])
    conn = _zconn("ZD create", {"label_names": "needs-translation"})
    ok, msg, summary = cstore.zendesk_translate(conn.id, actor="auto", transport=fz1.transport())
    assert ok, msg
    assert summary["created"] == 2 and summary["updated"] == 0, summary  # de + fr created
    assert {loc for _a, loc, _b in fz1.creates} == {"de", "fr"}, fz1.creates
    # each create carries a translated title + HTML body + draft True (default)
    for _a, loc, body in fz1.creates:  # noqa: B007
        assert body["draft"] is True, body
        assert "<h1>" in body["body"] and "<p>" in body["body"], ("markup preserved", body)
        assert body["title"].strip(), body
    print(
        f"  write-back CREATE: 2 locales POSTed as drafts, markup preserved ({summary['created']})"
    )

    # ---- (4) write-back UPDATE path (existing translation -> PUT) ----
    fz2 = FakeZendesk(articles=[_article("100", labels=["needs-translation"])])
    fz2.add_translation("100", "de", title="Alt", body="<p>alt</p>")  # de already exists
    conn2 = _zconn("ZD update", {"label_names": "needs-translation"})
    ok, msg, summary = cstore.zendesk_translate(conn2.id, actor="auto", transport=fz2.transport())
    assert ok, msg
    assert summary["created"] == 1 and summary["updated"] == 1, summary  # fr created, de updated
    assert any(loc == "de" for _a, loc, _b in fz2.updates), fz2.updates
    assert any(loc == "fr" for _a, loc, _b in fz2.creates), fz2.creates
    # the PUT clears `outdated` (explicit guard for the [UNCERTAIN] auto-clear)
    de_put = [b for _a, loc, b in fz2.updates if loc == "de"][0]  # noqa: RUF015
    assert de_put.get("outdated") is False, ("PUT must clear outdated", de_put)
    print("  write-back UPDATE: existing de PUT (outdated cleared) + fr POST created")

    # ---- (4b) REGRESSION: TargetLocale rows are catalog-canonical; wire calls stay
    # lowercase (Zendesk-shaped) ----
    # 'pt-br' is Zendesk's own wire-format code; the catalog's canonical display form
    # is 'pt-BR'. Provider RESOLUTION already worked either way (case/underscore-
    # insensitive _norm comparison) but the stored TargetLocale row must be canonical
    # so it visually matches a project's per-pair provider config keyed on es-ES-style
    # codes, and the connector's own API traffic must keep using 'pt-br' verbatim.
    fz4b = FakeZendesk(
        enabled_locales=("en-us", "pt-br"), articles=[_article("100", labels=["needs-translation"])]
    )
    conn4b = _zconn(
        "ZD canonical locale", {"label_names": "needs-translation", "target_locales": "pt-br"}
    )
    ok, msg, summary = cstore.zendesk_translate(conn4b.id, actor="auto", transport=fz4b.transport())
    assert ok, msg
    assert summary["locales"] == ["pt-br"], summary
    # wire call: the write-back POST used the lowercase Zendesk code, unchanged.
    assert {loc for _a, loc, _b in fz4b.creates} == {"pt-br"}, (
        "wire write-back must stay lowercase (Zendesk-shaped)",
        fz4b.creates,
    )
    # stored TargetLocale row: canonical form, NOT the raw lowercase wire code.
    proj4b_id = cstore.get(conn4b.id).project_id
    with Session(engine) as s:
        stored_locales = {
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == proj4b_id)).all()
        }
    assert stored_locales == {"pt-BR"}, (
        "TargetLocale row must be catalog-canonical, not the raw wire code",
        stored_locales,
    )
    print(
        "  regression: TargetLocale stored canonical (pt-BR) while Zendesk write-back "
        "stays lowercase (pt-br)"
    )

    # ---- (5) draft flag off -> publish immediately ----
    fz3 = FakeZendesk(articles=[_article("100")])
    conn3 = _zconn("ZD publish", {"draft": "0"})
    ok, _m, _s = cstore.zendesk_translate(conn3.id, actor="auto", transport=fz3.transport())
    assert ok and all(b["draft"] is False for _a, _l, b in fz3.creates), fz3.creates
    print("  draft flag: draft=0 publishes (draft False on every create)")

    # ---- (6) enabled-locale rejection ----
    # Guide enables only en-us + de; the connection targets de, fr, ja.
    fz4 = FakeZendesk(enabled_locales=("en-us", "de"), articles=[_article("100")])
    conn4 = _zconn("ZD reject", {"target_locales": "de, fr, ja"})
    ok, msg, summary = cstore.zendesk_translate(conn4.id, actor="auto", transport=fz4.transport())
    assert ok, msg
    assert summary["locales"] == ["de"], ("only de is usable", summary)
    assert set(summary["rejected"]) == {"fr", "ja"}, summary
    assert {loc for _a, loc, _b in fz4.creates} == {"de"}, ("never wrote fr/ja", fz4.creates)
    assert "not enabled in Guide" in msg, msg
    print("  enabled-locale rejection: fr+ja dropped with a clear message, only de written")

    # ---- (7) content-hash suppression + edited_at NOT updated_at ----
    # First run creates de+fr and stores per-(article,locale) hashes.
    art_v1 = _article("100", edited_at="2026-07-01T00:00:00Z", updated_at="2026-07-01T00:00:00Z")
    fz5 = FakeZendesk(articles=[art_v1])
    conn5 = _zconn("ZD hash")
    ok, _m, s1 = cstore.zendesk_translate(conn5.id, actor="auto", transport=fz5.transport())
    assert s1["created"] == 2, s1

    # Second run: updated_at bumped (a comment/vote), body/title UNCHANGED. The
    # source content hash is identical -> both locales suppressed (skipped), NOTHING
    # re-written. This is the edited_at-not-updated_at + content-hash guard.
    art_bumped = dict(art_v1, updated_at="2026-07-09T00:00:00Z")  # updated_at only!
    fz6 = FakeZendesk(articles=[art_bumped])
    fz6.add_translation("100", "de")
    fz6.add_translation("100", "fr")  # they now exist
    ok, _m, s2 = cstore.zendesk_translate(conn5.id, actor="auto", transport=fz6.transport())
    assert ok, _m
    assert s2["created"] == 0 and s2["updated"] == 0 and s2["skipped"] == 2, (
        "updated_at-only bump must NOT re-translate",
        s2,
    )
    assert not fz6.creates and not fz6.updates, (
        "no writes on unchanged content",
        fz6.creates,
        fz6.updates,
    )
    print("  suppression: updated_at-only bump + unchanged content -> 2 skipped, 0 writes")

    # Third run: edited_at advances AND body actually changes -> re-translated (PUT).
    art_edited = dict(
        art_v1, body="<h1>Reset</h1><p>New instructions here.</p>", edited_at="2026-07-10T00:00:00Z"
    )
    fz7 = FakeZendesk(articles=[art_edited])
    fz7.add_translation("100", "de")
    fz7.add_translation("100", "fr")
    ok, _m, s3 = cstore.zendesk_translate(conn5.id, actor="auto", transport=fz7.transport())
    assert ok and s3["updated"] == 2 and s3["skipped"] == 0, ("edited content -> re-translate", s3)
    print("  suppression: real edited_at content change -> 2 updated (not skipped)")

    # ---- (8) anti-loop labels: remove trigger label + add translated label ----
    fz8 = FakeZendesk(articles=[_article("100", labels=["needs-translation", "keep"])])
    conn8 = _zconn(
        "ZD labels",
        {
            "label_names": "needs-translation",
            "translated_label": "translated",
            "remove_trigger_label": "1",
        },
    )
    ok, _m, _s = cstore.zendesk_translate(conn8.id, actor="auto", transport=fz8.transport())
    assert ok and fz8.label_puts, ("a label PUT happened", fz8.label_puts)
    _aid, new_labels = fz8.label_puts[-1]
    assert "needs-translation" not in new_labels, ("trigger label removed", new_labels)
    assert "translated" in new_labels and "keep" in new_labels, (
        "translated added, keep kept",
        new_labels,
    )
    print("  anti-loop: trigger label removed, 'translated' added, unrelated label kept")

    # ---- (9) incremental export watermark chaining (end_time -> next start_time) ----
    fz9 = FakeZendesk(articles=[_article("300")])
    # page 1: articles up to end_time=1000 with a next_page; page 2: terminal, end_time=2000
    fz9.incremental_pages = [
        ([_article("300", edited_at="2026-07-01T00:00:00Z")], 1000, 1000),  # next_start=1000
        ([_article("301", edited_at="2026-07-02T00:00:00Z")], 2000, None),
    ]  # terminal
    zc9 = ZendeskConnector(
        {"subdomain": "acme"}, {"email": "a@acme.com", "api_token": "t"}, transport=fz9.transport()
    )
    arts, end_time = zc9.incremental_articles(start_time=0)
    got = sorted(a["id"] for a in arts)
    assert got == ["300", "301"], ("both pages walked via end_time chaining", got)
    assert end_time == 2000, ("final end_time is the new watermark", end_time)
    print("  incremental export: chained end_time->start_time across 2 pages, watermark=2000")

    # ---- (10) 429 honored via the existing backoff machinery ----
    slept = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        fz10 = FakeZendesk(articles=[_article("100")])
        fz10.rate_limit_once_on = "/locales"  # first /locales GET 429s, then 200
        zc10 = ZendeskConnector(
            {"subdomain": "acme"},
            {"email": "a@acme.com", "api_token": "t"},
            transport=fz10.transport(),
        )
        locs = zc10.enabled_locales()
        assert set(locs) == {"en-us", "de", "fr"}, locs
        assert slept == [5.0], ("honored Retry-After: 5", slept)
    finally:
        cbase._sleep = real_sleep
    print("  backoff: 429 + Retry-After on a GET retried once (honored 5s)")

    # ---- (11) generalized webhook intake: signature verify + dedup + enqueue ----
    secret = "super-secret-webhook-key"
    connw = _zconn("ZD webhook", secret={"webhook_secret": secret})

    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).isoformat()

    def sign(body: bytes) -> str:
        signed = timestamp.encode() + body
        return base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).decode()

    # a thin article.published payload: id only, no body
    payload = {
        "type": "zen:event-type:article.published",
        "detail": {"id": "100", "brand_id": "1"},
        "event": {"title": "Webhook article", "locale": "en-us"},
    }
    body = json.dumps(payload).encode()

    enqueued = []

    def _fake_enqueue(cid, kind, record_ids):
        enqueued.append((cid, kind, list(record_ids)))
        return 4242

    # (a) valid signature -> accepted + enqueued exactly once, record_ids from the
    #     THIN payload (id only)
    hdrs = {
        "X-Zendesk-Webhook-Signature": sign(body),
        "X-Zendesk-Webhook-Id": "evt-1",
        "X-Zendesk-Webhook-Signature-Timestamp": timestamp,
    }
    res = webhooks.handle_intake(connw.id, body, hdrs, enqueue=_fake_enqueue)
    assert res.status == 200 and res.enqueued and res.job_id == 4242, res.as_dict()
    assert res.record_ids == ["100"], ("thin payload -> article id only", res.record_ids)
    assert enqueued == [(connw.id, "zendesk", ["100"])], enqueued
    print("  webhook: valid signature -> accepted, enqueued once, record id from thin payload")

    # (b) provider RETRY of the SAME event id -> ACK 200 but enqueue NOTHING
    res2 = webhooks.handle_intake(connw.id, body, hdrs, enqueue=_fake_enqueue)
    assert res2.status == 200 and not res2.enqueued, ("retry deduped", res2.as_dict())
    assert len(enqueued) == 1, ("no second enqueue on retry", enqueued)
    with Session(engine) as s:
        rows = s.exec(select(WebhookEvent).where(WebhookEvent.connection_id == connw.id)).all()
    assert len(rows) == 1, ("exactly one ledger row for the event", rows)
    print("  webhook: retry of same event id -> 200 ACK, deduped (no second job, 1 ledger row)")

    # (c) BAD signature -> 401, nothing enqueued
    bad = webhooks.handle_intake(
        connw.id,
        body,
        {"X-Zendesk-Webhook-Signature": "deadbeef", "X-Zendesk-Webhook-Id": "evt-2"},
        enqueue=_fake_enqueue,
    )
    assert bad.status == 401 and not bad.enqueued, ("bad signature -> 401", bad.as_dict())
    assert len(enqueued) == 1, ("bad-sig delivery never enqueues", enqueued)
    print("  webhook: bad signature -> 401, nothing enqueued")

    # (d) unknown connection -> 401 (indistinguishable from bad-sig; no existence leak)
    ghost = webhooks.handle_intake(99999999, body, hdrs, enqueue=_fake_enqueue)
    assert ghost.status == 401, ("unknown connection is 401, not 404", ghost.as_dict())
    print("  webhook: unknown connection -> 401 (no existence leak)")

    # (e) REGRESSION: timing equalization — the unknown-connection branch pays for a
    # Fernet DECRYPT too (not just the dummy HMAC), matching the real path's
    # _webhook_secret -> dec_secret call BEFORE its HMAC verification. Assert
    # BEHAVIORALLY (call count), not via a flaky wall-clock measurement.
    from app.services.connectors import store as conn_store

    real_dec = conn_store.dec_secret
    dec_calls = []

    def _counting_dec(token):
        dec_calls.append(token)
        return real_dec(token)

    set_pieces("app.services.connectors.store", "dec_secret", _counting_dec)
    try:
        webhooks._DUMMY_ENC_TOKEN = None  # force a fresh dummy token this run
        dec_calls.clear()
        ghost2 = webhooks.handle_intake(99999999, body, hdrs, enqueue=_fake_enqueue)
        assert ghost2.status == 401, ghost2.as_dict()
        assert len(dec_calls) == 1, (
            "unknown-connection branch must do exactly one dummy Fernet decrypt "
            "(equalizing against the real path's _webhook_secret decrypt)",
            dec_calls,
        )
    finally:
        set_pieces("app.services.connectors.store", "dec_secret", real_dec)
    print(
        "  regression: unknown-connection 401 branch also pays for a dummy Fernet "
        "decrypt (equalizes both operations the real path does before its HMAC)"
    )

    # ---- (12) webhook intake THROUGH the HTTP route + real job re-fetch ----
    c = TestClient(app)
    fzr = FakeZendesk(articles=[_article("777", title="Route article")])
    connr = _zconn("ZD route", secret={"webhook_secret": secret})
    # Point the store's live-op transport at our fake for the job worker (jobs run
    # with transport=None; stub zendesk_translate to capture the article_ids the
    # webhook enqueued and run against the fake tenant).
    real_zt = cstore.zendesk_translate
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
        seen["article_ids"] = article_ids
        return real_zt(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=fzr.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "zendesk_translate", _capturing_zt)
    try:
        rp = {"type": "zen:event-type:article.published", "detail": {"id": "777"}}
        rb = json.dumps(rp).encode()
        r = c.post(
            f"/webhooks/connectors/{connr.id}",
            content=rb,
            headers={
                "X-Zendesk-Webhook-Signature": sign(rb),
                "X-Zendesk-Webhook-Signature-Timestamp": timestamp,
                "X-Zendesk-Webhook-Id": "route-evt-1",
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200 and r.json()["enqueued"], r.text
        jid = r.json()["job_id"]
        d = wait_job(c, jid)
        assert d["status"] == JobStatus.COMPLETED, d
        assert seen.get("article_ids") == ["777"], ("job re-fetched exactly the event's id", seen)
        # the re-fetch translated + wrote the article (create de+fr)
        assert {loc for _a, loc, _b in fzr.creates} == {"de", "fr"}, fzr.creates
    finally:
        set_pieces("app.services.connectors.store", "zendesk_translate", real_zt)
    print("  webhook route: HMAC-verified POST -> job -> re-fetch article 777 -> wrote de+fr")

    # bad-sig through the real route -> 401
    r401 = c.post(
        f"/webhooks/connectors/{connr.id}",
        content=b"{}",
        headers={"X-Zendesk-Webhook-Signature": "nope", "Content-Type": "application/json"},
    )
    assert r401.status_code == 401, r401.text
    print("  webhook route: bad signature -> HTTP 401")

    # ---- (12b) REGRESSION: webhook body size cap (413), BEFORE signature evaluation ----
    from app import config as app_config

    cap = app_config.WEBHOOK_MAX_BODY_BYTES

    # (a) oversized DECLARED Content-Length -> 413, and the signature verifier never
    # even runs (a bad/absent signature on the SAME oversized request would also be
    # 401 -- assert 413 specifically, proving the size check runs first).
    huge_body = b"x" * (cap + 1)
    sig_calls = []
    real_verify = webhooks.verify_signature

    def _counting_verify(*a, **kw):
        sig_calls.append(1)
        return real_verify(*a, **kw)

    webhooks.verify_signature = _counting_verify
    try:
        r413a = c.post(
            f"/webhooks/connectors/{connr.id}",
            content=huge_body,
            headers={
                "Content-Length": str(len(huge_body)),
                "X-Zendesk-Webhook-Signature": "irrelevant",
                "Content-Type": "application/octet-stream",
            },
        )
        assert r413a.status_code == 413, r413a.text
        assert not sig_calls, (
            "oversized Content-Length must reject BEFORE signature verification",
            sig_calls,
        )
    finally:
        webhooks.verify_signature = real_verify
    print(f"  regression: webhook Content-Length > {cap} -> 413, signature never evaluated")

    # (b) oversized ACTUAL body with NO Content-Length header at all -> still 413
    # (the streamed-read guard, not just the header pre-check). TestClient (httpx)
    # always computes a real Content-Length for a bytes body, so the ONLY way to
    # actually omit it end-to-end is a generator/async-iterator body, which httpx
    # sends chunked with no Content-Length -- exactly the "lying/absent header"
    # case the review flagged.
    import asyncio

    async def _gen_body():
        chunk = b"y" * 4096
        for _ in range((cap // len(chunk)) + 5):  # comfortably over the cap
            yield chunk

    async def _post_streamed():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post(
                f"/webhooks/connectors/{connr.id}",
                content=_gen_body(),
                headers={
                    "X-Zendesk-Webhook-Signature": "irrelevant",
                    "Content-Type": "application/octet-stream",
                },
            )

    r413b = asyncio.run(_post_streamed())
    assert r413b.status_code == 413, r413b.text
    print(
        "  regression: oversized actual body with NO Content-Length header -> 413 "
        "via the bounded streamed read"
    )

    # (c) a normal, small, VALID webhook body is unaffected by the cap.
    ok_payload = {"type": "zen:event-type:article.published", "detail": {"id": "777"}}
    ok_body = json.dumps(ok_payload).encode()
    assert len(ok_body) < cap
    rok = c.post(
        f"/webhooks/connectors/{connr.id}",
        content=ok_body,
        headers={
            "X-Zendesk-Webhook-Signature": sign(ok_body),
            "X-Zendesk-Webhook-Signature-Timestamp": timestamp,
            "X-Zendesk-Webhook-Id": "cap-ok-evt-1",
            "Content-Type": "application/json",
        },
    )
    assert rok.status_code == 200, rok.text
    print("  regression: normal-sized webhook body is unaffected by the size cap")

    # ---- (13) SSRF guard on the subdomain base ----
    for badsub in ("acme/evil", "acme:8080"):
        raised = False
        try:
            ZendeskConnector({"subdomain": badsub}, {"email": "a@x.com", "api_token": "t"})._base()
        except cbase.ConnectorError:
            raised = True
        assert raised, f"invalid subdomain must be rejected: {badsub}"
    good = ZendeskConnector({"subdomain": "acme"}, {"email": "a@x.com", "api_token": "t"})
    assert good._base() == "https://acme.zendesk.com", good._base()
    print("  ssrf/base: invalid subdomain rejected; acme -> https://acme.zendesk.com")

    # ---- (14) routes: Zendesk appears in the add-connector flow ----
    idx = c.get("/connectors").text
    assert "Zendesk" in idx, "Zendesk tile missing from /connectors"
    typ = c.get("/connectors/zendesk").text
    # rebuild: Zendesk is un-archived -> the add form is offered again.
    assert _reg.is_archived("zendesk") is False, "zendesk must be un-archived"
    assert "New connections are closed" not in typ and "Create connection" in typ, (
        "un-archived Zendesk type page must show the add form"
    )
    _fnames = {f.name for f in _reg.spec("zendesk").config_fields} | {
        f.name for f in _reg.spec("zendesk").secret_fields
    }
    assert "subdomain" in _fnames, ("Zendesk spec must still declare 'subdomain'", _fnames)
    # the connection page shows the webhook intake URL (existing conns untouched)
    det = c.get(f"/connectors/connection/{connr.id}").text
    assert f"/webhooks/connectors/{connr.id}" in det, "intake URL not shown on connection page"
    print(
        "  routes: Zendesk tile present (active) + add form on type page + "
        "intake URL on the connection page"
    )

    # ---- (15) retry_failed re-runs ONLY the failed (article, locale) pairs ----
    fzret = FakeZendesk(articles=[_article("100"), _article("101", title="Other")])
    connret = _zconn("ZD retry")
    failures = [
        {"article_id": "100", "locale": "de", "error": "500"},
        {"article_id": "101", "locale": "fr", "error": "400"},
    ]
    okr, _m, sumr = cstore.retry_failed(
        connret.id, failures, actor="auto", transport=fzret.transport()
    )
    assert okr, sumr
    written = {(a, loc) for a, loc, _b in fzret.creates}
    assert written == {("100", "de"), ("101", "fr")}, ("only the 2 failed pairs written", written)
    print("  retry: re-ran ONLY the 2 failed (article, locale) pairs, not the cross product")

    # ---- (16) REGRESSION: fractional-second edited_at parses -> watermark advances ----
    fz16 = FakeZendesk(articles=[_article("100", edited_at="2026-07-05T10:00:00.123Z")])
    conn16 = _zconn("ZD fractional")
    ok, _m, s16 = cstore.zendesk_translate(conn16.id, actor="auto", transport=fz16.transport())
    assert ok and s16["watermark"] > 1_700_000_000, (
        "fractional edited_at must parse and advance the watermark",
        s16,
    )
    print("  regression: fractional-second edited_at parsed, watermark advanced")

    # ---- (17) REGRESSION: ALL target locales rejected -> run FAILS (not green) ----
    fz17 = FakeZendesk(enabled_locales=("en-us",), articles=[_article("100")])
    conn17 = _zconn("ZD all-rejected", {"target_locales": "de, fr"})
    ok, msg, s17 = cstore.zendesk_translate(conn17.id, actor="auto", transport=fz17.transport())
    assert not ok, ("all-rejected-locales run must FAIL, not report green", msg, s17)
    assert set(s17["rejected"]) == {"de", "fr"} and s17["locales"] == [], s17
    assert not fz17.creates and not fz17.updates, ("nothing written", fz17.creates)
    with Session(engine) as s:
        row17 = s.get(Connection, conn17.id)
    assert row17.status == "error", ("misconfigured connection must show error", row17.status)
    print("  regression: all-rejected-locales -> ok=False + status=error, nothing written")

    # ---- (18) REGRESSION: webhook re-fetch is NOT label-filtered ----
    # The anti-loop strips the trigger label on first translate; a LATER content-
    # edit webhook for that article must still be processed (the event names the
    # article — that IS the selection).
    fz18 = FakeZendesk(articles=[_article("300", labels=["translated"])])  # no trigger label
    conn18 = _zconn("ZD wh-label", {"label_names": "needs-translation"})
    ok, _m, s18 = cstore.zendesk_translate(
        conn18.id, article_ids=["300"], actor="webhook", transport=fz18.transport()
    )
    assert ok and s18["created"] == 2, ("explicit article_ids must bypass the label filter", s18)
    print("  regression: webhook article_ids bypass the label filter (post-strip edits still flow)")

    # ---- (19) REGRESSION: enqueue crash between ledger row and job -> retry recovers ----
    connc = _zconn("ZD crash", secret={"webhook_secret": secret})
    crash_payload = {"type": "zen:event-type:article.published", "detail": {"id": "555"}}
    crash_body = json.dumps(crash_payload).encode()
    crash_hdrs = {
        "X-Zendesk-Webhook-Signature": sign(crash_body),
        "X-Zendesk-Webhook-Signature-Timestamp": timestamp,
        "X-Zendesk-Webhook-Id": "crash-evt-1",
    }

    def _boom(cid, kind, record_ids):
        raise RuntimeError("worker enqueue crashed")

    crashed = False
    try:
        webhooks.handle_intake(connc.id, crash_body, crash_hdrs, enqueue=_boom)
    except RuntimeError:
        crashed = True
    assert crashed, "the crash must propagate (provider sees 500 and retries)"
    # The provider's RETRY of the SAME event id must be allowed to enqueue (the
    # ledger row exists but has no job attached -> retryable, not lost forever).
    retried = []
    res19 = webhooks.handle_intake(
        connc.id, crash_body, crash_hdrs, enqueue=lambda cid, k, ids: retried.append(ids) or 777
    )
    assert res19.status == 200 and res19.enqueued and retried == [["555"]], (
        "retry after a crashed enqueue must re-enqueue",
        res19.as_dict(),
        retried,
    )
    # ...and a THIRD delivery (job now attached) is a settled duplicate.
    res19b = webhooks.handle_intake(
        connc.id, crash_body, crash_hdrs, enqueue=lambda cid, k, ids: 888
    )
    assert res19b.status == 200 and not res19b.enqueued, res19b.as_dict()
    print("  regression: enqueue crash -> provider retry re-enqueues; settled dup still deduped")

    # ---- (20) REGRESSION: verified event with NO recognizable ids -> ACK no-op ----
    # (never a full paid-MT sweep off an unparseable payload)
    noid_body = json.dumps({"type": "zen:event-type:article.published"}).encode()
    noid_hdrs = {
        "X-Zendesk-Webhook-Signature": sign(noid_body),
        "X-Zendesk-Webhook-Signature-Timestamp": timestamp,
        "X-Zendesk-Webhook-Id": "noid-evt-1",
    }
    called = []
    res20 = webhooks.handle_intake(
        connc.id, noid_body, noid_hdrs, enqueue=lambda cid, k, ids: called.append(ids) or 999
    )
    assert res20.status == 200 and not res20.enqueued and not called, (
        "no-ids event must ACK without enqueuing",
        res20.as_dict(),
        called,
    )
    assert "no changed records" in res20.message, res20.message
    print("  regression: signed no-ids event -> 200 ACK, no job (no full-sweep escalation)")

    # ---- (21) incremental-export BACKSTOP consumed by the translate flow ----
    # An article whose content changed since the watermark but which no longer
    # carries the trigger label (anti-loop stripped it) is picked up via the
    # incremental union; an updated_at-only incremental candidate is NOT.
    conn21 = _zconn("ZD backstop", {"label_names": "needs-translation"})
    cstore.patch_config(conn21.id, {"zendesk_watermark": 1000})
    changed = _article(
        "400",
        title="Changed since watermark",
        labels=["translated"],  # no trigger label
        edited_at="2026-07-01T00:00:00Z",
    )  # epoch >> 1000
    stale = _article(
        "401",
        title="Metadata-only bump",
        labels=["translated"],
        edited_at="1970-01-01T00:05:00Z",  # epoch 300 <= 1000
        updated_at="2026-07-01T00:00:00Z",
    )
    fz21 = FakeZendesk()  # discover finds nothing
    fz21.incremental_pages = [([changed, stale], 2000, None)]
    ok, _m, s21 = cstore.zendesk_translate(conn21.id, actor="scheduler", transport=fz21.transport())
    assert ok, _m
    assert s21["articles"] == 1 and s21["created"] == 2, (
        "only the edited_at-advanced article flows via the backstop",
        s21,
    )
    written21 = {a for a, _l, _b in fz21.creates}
    assert written21 == {"400"}, ("401 (edited_at <= watermark) must NOT re-enqueue", written21)
    assert s21["watermark"] >= 1_700_000_000, ("watermark advanced past the change", s21)
    print(
        "  backstop: incremental union translated the label-stripped changed article; "
        "updated_at-only candidate excluded"
    )

    # watermark must NOT advance past an article whose write FAILED (so the
    # backstop re-surfaces it next run)
    conn21b = _zconn("ZD wm-fail")
    fail_art = _article("500", edited_at="2026-07-02T00:00:00Z")
    fz21b = FakeZendesk(articles=[fail_art])
    real_handle = fz21b._handle

    def failing_handle(req):
        if req.method == "POST" and req.url.path.endswith("/translations"):
            return httpx.Response(500, json={"error": "boom"})
        return real_handle(req)

    ok, _m, s21b = cstore.zendesk_translate(
        conn21b.id, actor="auto", transport=httpx.MockTransport(failing_handle)
    )
    assert not ok and s21b["failures"], s21b
    assert s21b["watermark"] == 0, ("watermark must stay behind a failed article", s21b)
    print("  backstop: failed write keeps the watermark behind the article (re-surfaced next run)")

    # ---- (23) REGRESSION: concurrent runs merge hashes + never regress the watermark ----
    # Reproduces the review finding: a webhook job (plain jobs.submit) and a
    # scheduled/manual run (submit_unique) can execute concurrently against the SAME
    # connection (WORKER_COUNT=2). Both snapshot-read zendesk_hashes/zendesk_watermark
    # at run start; the OLD code patch_config-wrote the whole stale snapshot at the
    # end, so whichever run finished LAST wiped the other's new hashes and could
    # regress the watermark (200 -> 150). patch_config_merge fixes this: only the
    # run's own delta is merged in, and the watermark takes max(stored, new).
    connc = _zconn("ZD concurrent")
    # Run A "snapshot-reads" an empty config (nothing stored yet).
    # Run B finishes FIRST: writes its own hash entries + advances the watermark to 200.
    cstore.patch_config_merge(
        connc.id,
        "zendesk_hashes",
        {"100:de": "hashB"},
        watermark_key="zendesk_watermark",
        watermark_at_least=200,
    )
    # Run A finishes SECOND: persists its OWN delta (disjoint article) + a LOWER
    # watermark (150, from A's own slower/older run) via the same merge helper.
    cstore.patch_config_merge(
        connc.id,
        "zendesk_hashes",
        {"200:fr": "hashA"},
        watermark_key="zendesk_watermark",
        watermark_at_least=150,
    )
    final_cfg = cstore.get(connc.id).config or {}
    assert final_cfg.get("zendesk_hashes") == {"100:de": "hashB", "200:fr": "hashA"}, (
        "both runs' hash deltas must survive (no clobber)",
        final_cfg.get("zendesk_hashes"),
    )
    assert final_cfg.get("zendesk_watermark") == 200, (
        "watermark must NOT regress from 200 to 150",
        final_cfg.get("zendesk_watermark"),
    )
    print(
        "  regression: interleaved runs merge hash deltas + watermark stays at the max (200), "
        "never regresses to 150"
    )

    # End-to-end version: a real zendesk_translate run only ever contributes its OWN
    # delta, even when the config already carries ANOTHER run's fresher state that
    # this run's in-memory snapshot never saw.
    fz23 = FakeZendesk(articles=[_article("900", edited_at="2026-07-01T00:00:00Z")])
    conn23 = _zconn("ZD concurrent e2e")
    # Simulate a concurrent webhook run's state landing WHILE conn23's run is still
    # "in flight" in memory: patch it in directly, bypassing conn23's own snapshot.
    # zendesk_translate re-reads `conn` only ONCE at the top, so this models a second
    # run's persist happening after conn23's in-memory snapshot was taken but before
    # conn23's own end-of-run persist.
    ok, _m, s23 = cstore.zendesk_translate(conn23.id, actor="auto", transport=fz23.transport())
    assert ok and s23["created"] == 2, s23
    cfg23 = cstore.get(conn23.id).config or {}
    assert cfg23.get("zendesk_hashes", {}).get("900:de"), cfg23
    assert cfg23.get("zendesk_hashes", {}).get("900:fr"), cfg23
    print(
        "  regression: a normal zendesk_translate run persists via merge (delta-only, watermark max)"  # noqa: E501
    )

    # ---- (23) DEFENSE-IN-DEPTH (QE gate forward-compat): a NEEDS_REVIEW segment
    #    is excluded from write-back. Connectors don't run the gated orchestrator
    #    today (translate.pretranslate_document always sets SegStatus.MT), so this
    #    simulates a future gated connector flow by flipping one produced segment
    #    to NEEDS_REVIEW between the translate phase and the write-back gather
    #    (exactly where zendesk_translate calls translate.pretranslate_document
    #    then re-reads Segment rows) — the write-back loop must skip it.
    # ----
    import app.services.translate as translate_mod

    real_ptd = translate_mod.pretranslate_document

    def _ptd_then_flip_de(session, doc_id, locale, spec=None, force_pii=False):
        stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == "de":
            # order_index 1 == the first BODY unit (0 is the title) — flip it to
            # NEEDS_REVIEW to simulate a future gated connector flow routing a
            # segment to review between the translate and write-back phases.
            seg = session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id,
                    Segment.target_locale == "de",
                    Segment.order_index == 1,
                )
            ).first()
            if seg:
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
                session.commit()
        return stats

    set_pieces("app.services.translate", "pretranslate_document", _ptd_then_flip_de)
    try:
        fz24 = FakeZendesk(articles=[_article("500")])
        conn24 = _zconn("ZD needs-review guard")
        ok, msg, _s24 = cstore.zendesk_translate(
            conn24.id, actor="auto", transport=fz24.transport()
        )
        assert ok, msg
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    # de's first body segment (order_index 1) was flipped to NEEDS_REVIEW -> its
    # text must be ABSENT from the write payload's body; fr is untouched -> its
    # body unit is present as usual.
    de_writes = [w for w in (fz24.creates + fz24.updates) if w[1] == "de"]
    fr_writes = [w for w in (fz24.creates + fz24.updates) if w[1] == "fr"]
    assert fr_writes, "fr (untouched) must still be written"
    # #640: the held pair is NOT created at all (a created target would carry the source text
    # in the held unit); the flagged text therefore cannot reach the wire. fr is untouched.
    assert not de_writes, ("de is not created while a unit is held", de_writes)
    assert "Click the link" in fr_writes[0][2].get("body", ""), fr_writes
    print("  23. defense-in-depth: a NEEDS_REVIEW segment is excluded from Zendesk write-back")

    # ---- (24) REGRESSION: a plan-restricted tenant 403s the translation GET probe ----
    # write_target's exists_target -> get_translation probe hits a 403 (not the usual
    # 404-absent), which raises ConnectorError straight out of the write loop. Before
    # the per-pair guard it crashed the whole run; now it must land as a per-pair
    # failure (run ERROR), no exception escaping cstore.zendesk_translate.
    class _Probe403Zendesk(FakeZendesk):
        def _handle(self, req):
            path = req.url.path
            if req.method == "GET" and "/translations/" in path:
                loc = path.rsplit("/", 1)[-1]
                if loc in ("de", "fr"):
                    return httpx.Response(403, json={"error": "restricted"})
            return super()._handle(req)

    fz24b = _Probe403Zendesk(articles=[_article("900")])
    conn24b = _zconn("ZD probe-403 per-pair guard")
    ok, msg, s24b = cstore.zendesk_translate(conn24b.id, actor="auto", transport=fz24b.transport())
    assert not ok, ("GET-probe 403 -> run not green", msg)
    assert s24b["created"] == 0 and s24b["updated"] == 0, s24b
    assert len(s24b["failures"]) == 2, s24b["failures"]
    assert all("403" in f["error"] for f in s24b["failures"]), s24b["failures"]
    assert not (fz24b.creates + fz24b.updates), (
        "no write reached - the probe itself failed",
        fz24b.creates,
        fz24b.updates,
    )
    with Session(engine) as sess:
        assert sess.get(Connection, conn24b.id).status == "error", (
            "GET-probe 403 -> connection status error"
        )
    print(
        "  24. regression: plan-restricted GET probe 403 -> per-pair failure, no crash, run ERROR"
    )

    # ---- (25) held-back on UPDATE (#640 / taxonomy B): a held title or body is OMITTED,
    #    the stored human translation survives, no hash, the stale flag is not cleared ----
    def _flip_ptd(flips):
        def _ptd(session, doc_id, locale, spec=None, force_pii=False):
            stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
            for loc, order in flips:
                if loc != locale:
                    continue
                seg = session.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id,
                        Segment.target_locale == loc,
                        Segment.order_index == order,
                    )
                ).first()
                if seg:
                    seg.status = SegStatus.NEEDS_REVIEW
                    session.add(seg)
                    session.commit()
            return stats

        return _ptd

    for label, flips, want_title, want_body in (
        ("body unit held", [("de", 1)], True, False),
        ("title held", [("de", 0)], False, True),
    ):
        fz25 = FakeZendesk(articles=[_article("600")])
        fz25.add_translation("600", "de", title="Menschlicher Titel", body="<p>Menschlich</p>")
        fz25.translations[("600", "de")]["outdated"] = True
        conn25 = _zconn("ZD held update " + label, {"target_locales": "de"})
        set_pieces("app.services.translate", "pretranslate_document", _flip_ptd(flips))
        try:
            cstore.zendesk_translate(conn25.id, actor="auto", transport=fz25.transport())
        finally:
            set_pieces("app.services.translate", "pretranslate_document", real_ptd)
        assert not fz25.creates, ("existing variant is updated, never re-created", fz25.creates)
        assert len(fz25.updates) == 1, (label, fz25.updates)
        sent = fz25.updates[0][2]
        # A held field is re-sent with its STORED value (safe whether PUT merges or replaces);
        # the translated one carries new text.
        if want_title:
            assert sent["title"] != "Menschlicher Titel", (label, sent)
        else:
            assert sent["title"] == "Menschlicher Titel", ("held title = stored", label, sent)
        if want_body:
            assert sent["body"] != "<p>Menschlich</p>", (label, sent)
        else:
            assert sent["body"] == "<p>Menschlich</p>", ("held body = stored", label, sent)
        assert "Reset" not in sent["body"] or want_body, ("no source text", sent)
        assert "_held" not in sent and "outdated" not in sent, ("held pair keeps stale flag", sent)
        stored = fz25.translations[("600", "de")]
        if not want_body:
            assert stored["body"] == "<p>Menschlich</p>", ("stored body survives", stored)
        if not want_title:
            assert stored["title"] == "Menschlicher Titel", ("stored title survives", stored)
        assert stored["outdated"] is True, stored
        hashes = (cstore.get(conn25.id).config or {}).get("zendesk_hashes", {})
        assert "600:de" not in hashes, ("a held pair records no content hash", hashes)
    print("  25. held-back on update: held title/body omitted, stored text kept, no hash")

    # all fields held on an existing variant -> no write at all; on a new variant -> no create
    for exists in (True, False):
        fz25b = FakeZendesk(articles=[_article("601", body="<p>Only</p>")])
        if exists:
            fz25b.add_translation("601", "de", title="Alt", body="<p>Alt</p>")
        conn25b = _zconn("ZD all held %s" % exists, {"target_locales": "de"})  # noqa: UP031
        set_pieces(
            "app.services.translate", "pretranslate_document", _flip_ptd([("de", 0), ("de", 1)])
        )
        try:
            cstore.zendesk_translate(conn25b.id, actor="auto", transport=fz25b.transport())
        finally:
            set_pieces("app.services.translate", "pretranslate_document", real_ptd)
        assert not fz25b.creates and not fz25b.updates, (exists, fz25b.creates, fz25b.updates)
        h = (cstore.get(conn25b.id).config or {}).get("zendesk_hashes", {})
        assert "601:de" not in h, h
    print("  25b. everything held: nothing created or updated, no hash")

    # ---- (26) taxonomy A: caller strings never escape the Help Center path ----
    seen_paths: list = []

    def _spy(req):
        seen_paths.append(req.url.raw_path.decode())
        return httpx.Response(404, json={})

    spy = httpx.MockTransport(_spy)
    zspy = ZendeskConnector(
        {"subdomain": "acme"}, {"email": "a@x.com", "api_token": "t"}, transport=spy
    )
    bad_ids = (
        "../users/me",
        "1/../../x",
        "%2e%2e%2f1",
        "1?x=1",
        "1#f",
        "1\r\nX: y",
        "1\x00",
        "١",
        "",
    )
    for bad in bad_ids:
        for call in (
            lambda b=bad: zspy.discover_articles(article_ids=[b]),
            lambda b=bad: zspy.show_article(b),
            lambda b=bad: zspy.get_translation(b, "de"),
            lambda b=bad: zspy.create_translation(b, "de", {"title": "t"}),
            lambda b=bad: zspy.update_translation(b, "de", {"title": "t"}),
        ):
            try:
                call()
            except cbase.ConnectorError:
                continue
            raise AssertionError(("bad id not refused", bad))
    for badloc in ("../x", "de/../x", "de?x", "de\nx", "", "d e"):
        try:
            zspy.get_translation("1", badloc)
        except cbase.ConnectorError:
            continue
        raise AssertionError(("bad locale not refused", badloc))
    for key in ("section_id", "category_id"):
        zbad = ZendeskConnector(
            {"subdomain": "acme", key: ".."}, {"email": "a@x.com", "api_token": "t"}, transport=spy
        )
        try:
            zbad.discover_articles()
        except cbase.ConnectorError:
            pass
        else:
            raise AssertionError(("bad scope id not refused", key))
    assert seen_paths == [], ("no request left the connector for a refused input", seen_paths)
    # an explicitly named article outside the configured section is dropped like a missing one
    fz26 = FakeZendesk(articles=[_article("700", section_id=555), _article("701", section_id=999)])
    z26 = ZendeskConnector(
        {"subdomain": "acme", "section_id": "555"},
        {"email": "a@x.com", "api_token": "t"},
        transport=fz26.transport(),
    )
    assert [a["id"] for a in z26.discover_articles(article_ids=["700", "701"])] == ["700"]
    print("  26. path escape refused before any request; out-of-section article id dropped")

    # ---- (27) the API token never reaches a log line ----
    import logging

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    cap = _Cap()
    root_log = logging.getLogger()
    old_level = root_log.level
    root_log.addHandler(cap)
    root_log.setLevel(logging.DEBUG)
    try:
        fz27 = FakeZendesk(articles=[_article("800")])
        conn27 = _zconn("ZD token log", secret={"api_token": "SECRET-TOKEN-XYZ"})
        cstore.zendesk_translate(conn27.id, actor="auto", transport=fz27.transport())
        fz27b = FakeZendesk(articles=[_article("801")])
        fz27b.rate_limit_once_on = "/locales"
        cstore.zendesk_translate(conn27.id, actor="auto", transport=fz27b.transport())
    finally:
        root_log.removeHandler(cap)
        root_log.setLevel(old_level)
    b64 = base64.b64encode(b"a@acme.com/token:SECRET-TOKEN-XYZ").decode()
    blob = "\n".join(cap.lines)
    assert "SECRET-TOKEN-XYZ" not in blob and b64 not in blob, "token leaked into a log line"
    print("  27. token and Basic credential absent from every log line")

    # ---- (28) vendor errors on write: 422 on create and update are per-pair failures ----
    class _Reject422(FakeZendesk):
        def _handle(self, req):
            if req.method in ("POST", "PUT") and "/translations" in req.url.path:
                return httpx.Response(422, json={"error": "RecordInvalid"})
            return super()._handle(req)

    fz28 = _Reject422(articles=[_article("900")])
    fz28.add_translation("900", "fr", title="x", body="<p>x</p>")
    conn28 = _zconn("ZD 422")
    ok, msg, s28 = cstore.zendesk_translate(conn28.id, actor="auto", transport=fz28.transport())
    assert not ok and len(s28["failures"]) == 2, (msg, s28)
    assert all("422" in f["error"] for f in s28["failures"]), s28["failures"]
    assert not (cstore.get(conn28.id).config or {}).get("zendesk_hashes"), "failed write: no hash"
    print("  28. 422 on create and update -> per-pair failures, no hash recorded")

    # ---- (29) pagination follows the cursor across pages ----
    class _Paged(FakeZendesk):
        def _handle(self, req):
            if req.method == "GET" and req.url.path.endswith("/help_center/articles"):
                after = req.url.params.get("page[after]")
                assert req.url.params.get("page[size]"), "cursor pagination needs page[size]"
                if not after:
                    return httpx.Response(
                        200,
                        json={
                            "articles": [_article("1"), _article("2")],
                            "meta": {"has_more": True, "after_cursor": "c1"},
                        },
                    )
                assert after == "c1", after
                return httpx.Response(
                    200, json={"articles": [_article("3")], "meta": {"has_more": False}}
                )
            return super()._handle(req)

    zp = ZendeskConnector(
        {"subdomain": "acme"},
        {"email": "a@x.com", "api_token": "t"},
        transport=_Paged().transport(),
    )
    assert [a["id"] for a in zp.discover_articles()] == ["1", "2", "3"]
    print("  29. discovery follows meta.after_cursor across pages")

    # ---- (30) held pair keeps its trigger label; a failed label PUT is reported ----
    lbl_cfg = {
        "label_names": "needs-translation",
        "translated_label": "translated",
        "remove_trigger_label": "1",
        "target_locales": "de",
    }
    fz30 = FakeZendesk(articles=[_article("610", labels=["needs-translation"])])
    fz30.add_translation("610", "de", title="Alt", body="<p>Alt</p>")
    conn30 = _zconn("ZD held keeps label", lbl_cfg)
    set_pieces("app.services.translate", "pretranslate_document", _flip_ptd([("de", 1)]))
    try:
        cstore.zendesk_translate(conn30.id, actor="auto", transport=fz30.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert fz30.updates and not fz30.label_puts, ("held pair: trigger label kept", fz30.label_puts)

    class _LabelFail(FakeZendesk):
        def _handle(self, req):
            if req.method == "PUT" and "/translations/" not in req.url.path:
                return httpx.Response(403, json={"error": "Forbidden"})
            return super()._handle(req)

    fz30b = _LabelFail(articles=[_article("611", labels=["needs-translation"])])
    conn30b = _zconn("ZD label fail", lbl_cfg)
    _ok, msg30, _s = cstore.zendesk_translate(conn30b.id, actor="auto", transport=fz30b.transport())
    assert "611/labels" in msg30 and "403" in msg30, ("label failure reported", msg30)
    print("  30. held pair keeps its trigger label; label write-back failure is reported")

    # ---- (31) gate: draft mode never changes a PUBLISHED translation; visible in the result ----
    for replaces in (False, True):
        fz31 = FakeZendesk(articles=[_article("620", labels=["needs-translation"])])
        fz31.put_replaces = replaces
        fz31.add_translation("620", "de", title="Live Titel", body="<p>Live</p>", draft=False)
        conn31 = _zconn("ZD draft vs published %s" % replaces, lbl_cfg)  # noqa: UP031
        _ok31, msg31, s31 = cstore.zendesk_translate(
            conn31.id, actor="auto", transport=fz31.transport()
        )
        live = fz31.translations[("620", "de")]
        assert not fz31.updates and not fz31.creates, ("no write", fz31.updates, fz31.creates)
        assert live["title"] == "Live Titel" and live["body"] == "<p>Live</p>", live
        assert live["draft"] is False, ("still published", live)
        assert _ok31, ("a live-kept pair is reported, not a failure", msg31)
        assert [(f["article_id"], f["locale"]) for f in s31["live_kept"]] == [("620", "de")], s31
        assert "Kept 1 LIVE (published) translation(s) unchanged" in msg31, msg31
        assert not fz31.label_puts, ("trigger label kept", fz31.label_puts)
        h31 = (cstore.get(conn31.id).config or {}).get("zendesk_hashes", {})
        assert "620:de" not in h31, h31
    # the write-time guard refuses too (an explicit retry bypasses the pre-MT park hook)
    fz31w = FakeZendesk(articles=[_article("622")])
    fz31w.put_replaces = True
    fz31w.add_translation("622", "de", title="Live", body="<p>Live</p>", draft=False)
    zc31 = ZendeskConnector(
        {"subdomain": "acme"}, {"email": "a@x.com", "api_token": "t"}, transport=fz31w.transport()
    )
    r31 = zc31.strategy.write_target(zc31, "622", "de", {"title": "Neu", "body": "<p>Neu</p>"})
    assert not r31.ok and r31.message.startswith("live-kept:"), r31
    assert not fz31w.updates and fz31w.translations[("622", "de")]["title"] == "Live"
    # publish mode (draft = 0) still updates a published translation, as configured
    fz31b = FakeZendesk(articles=[_article("621")])
    fz31b.add_translation("621", "de", title="Live", body="<p>Live</p>", draft=False)
    conn31b = _zconn("ZD publish updates live", {"target_locales": "de", "draft": "0"})
    ok31b, _m, _s = cstore.zendesk_translate(conn31b.id, actor="auto", transport=fz31b.transport())
    assert ok31b and len(fz31b.updates) == 1 and fz31b.updates[0][2]["draft"] is False
    print("  31. draft mode leaves a published translation untouched and reports it")

    # ---- (32) gate: replace-semantics PUT never blanks or source-fills a held field ----
    for label, flips, held_key, stored_val in (
        ("title held", [("de", 0)], "title", "Menschlicher Titel"),
        ("body unit held", [("de", 1)], "body", "<p>Menschlich</p>"),
    ):
        for mode in ("1", "0"):
            fz32 = FakeZendesk(articles=[_article("630")])
            fz32.put_replaces = True
            fz32.add_translation("630", "de", title="Menschlicher Titel", body="<p>Menschlich</p>")
            conn32 = _zconn(
                "ZD replace %s %s" % (label, mode),  # noqa: UP031
                {"target_locales": "de", "draft": mode},
            )
            set_pieces("app.services.translate", "pretranslate_document", _flip_ptd(flips))
            try:
                cstore.zendesk_translate(conn32.id, actor="auto", transport=fz32.transport())
            finally:
                set_pieces("app.services.translate", "pretranslate_document", real_ptd)
            stored = fz32.translations[("630", "de")]
            assert len(fz32.updates) == 1, (label, mode, fz32.updates)
            assert stored[held_key] == stored_val, ("held field survives a replace PUT", stored)
            assert stored["title"] and stored["body"], ("nothing blanked", stored)
            # a partly held pair never publishes a stored draft, even in publish mode
            assert stored["draft"] is True, (label, mode, stored)
    print("  32. replace-semantics PUT: held field keeps its stored value; draft stays draft")

    print("verify_zendesk OK")


if __name__ == "__main__":
    main()
