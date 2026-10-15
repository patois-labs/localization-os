"""Freshdesk Solutions connector + connector-framework seams — fully offline via
httpx.MockTransport (a fake Freshdesk tenant).

Covers (per the connector brief's four headline constraints + the test list):
  - connector: test(), primary_language() read from the API, enabled_locales()
    (supported_languages), full-tree discover walk (categories -> folders ->
    articles), extract (title + HTML body via the html filter)
  - write-back roundtrip: CREATE (404 -> POST) AND UPDATE (GET 200 -> PUT) via the
    T2 SubResourceStrategy, per-language versions
  - POLL-ONLY change detection: updated_at bump + changed hash -> re-enqueue;
    updated_at bump + same hash -> suppressed
  - IMMUTABLE PRIMARY LANGUAGE: a non-en portal works (source != assumed en);
    translating INTO the primary is dropped
  - UNPUBLISHED-MASTER guard: a draft master (status 1) is skipped with a clear
    error, run not green
  - PLAN-GATE 4xx surfaces cleanly (not a crash, run not green)
  - 429 backoff (Retry-After honored via the shared machinery)
  - all-rejected targets -> NOT green + status=error
  - org scoping: the connector-created project inherits the connection's org
  - CLI freshdesk-translate round-trip
  - NEEDS_REVIEW exclusion from write-back (QE gate defense-in-depth)
  - framework seams: SubResourceStrategy 404 -> create; capability block poll-only

ASCII-only prints; final line is 'verify_freshdesk OK'.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_fd_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Connection, Project, Segment, SegStatus, TargetLocale
from app.services import connectors as _reg  # archive-tier spec introspection
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.freshdesk import FreshdeskConnector, _is_published
from app.services.connectors.strategy import ABSENT, SubResourceStrategy


# ============================================================================
# A fake Freshdesk tenant. Holds a category -> folders -> articles tree +
# per-language translations in memory; the MockTransport serves the
# /api/v2/solutions + /api/v2/settings endpoints and records writes.
# ============================================================================
class FakeFreshdesk:
    def __init__(
        self,
        primary="en",
        supported=("fr", "de", "es"),
        articles=None,
        plan_gate=False,
        settings_404=False,
        probe_403_langs=(),
        probe_400_langs=(),
        folders=None,
        subfolders=None,
    ):
        self.primary = primary
        self.supported = list(supported)
        # article_id -> master article dict
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        # (article_id, lang) -> translation dict (existing translations)
        self.translations: dict = {}
        self.writes: list = []  # (article_id, lang, method, payload)
        self.plan_gate = plan_gate  # when True, translation writes 4xx (plan-gated)
        self.settings_404 = settings_404  # settings/helpdesk route unavailable
        # langs whose translation GET probe 403s / 400s (plan-gate on the READ side,
        # or an unsupported-locale rejection) rather than the default 404-means-absent.
        self.probe_403_langs = set(probe_403_langs)
        self.probe_400_langs = set(probe_400_langs)
        self._rate_state = {"n": 0}
        self.rate_limit_once_on = None
        # tree: one category (1) -> N folders (default just folder 10) -> articles.
        # folders: list of folder-id strings under self.cat_id (top level).
        # subfolders: {parent_folder_id: [child_folder_id, ...]} for BFS tests.
        self.cat_id = "1"
        self.folder_id = "10"
        self.folders = list(folders) if folders is not None else [self.folder_id]
        self.subfolders = dict(subfolders or {})
        # folder_id -> [article_id, ...]; default: every article lives in folder_id.
        self.folder_articles = {self.folder_id: list(self.articles)}

    def add_translation(self, article_id, lang, title="t", description="<p>x</p>", status=1):
        self.translations[(str(article_id), lang)] = {
            "id": str(article_id),
            "title": title,
            "description": description,
            "status": status,
        }

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        pfx = "/api/v2"

        if self.rate_limit_once_on and self.rate_limit_once_on in path:
            self._rate_state["n"] += 1
            if self._rate_state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "rate"})

        # settings/helpdesk -> primary + supported languages
        if method == "GET" and path == f"{pfx}/settings/helpdesk":
            if self.settings_404:
                return httpx.Response(404, json={"error": "no settings route"})
            return httpx.Response(
                200,
                json={
                    "primary_language": self.primary,
                    "supported_languages": list(self.supported),
                },
            )

        # list categories
        if method == "GET" and path == f"{pfx}/solutions/categories":
            return httpx.Response(200, json=[{"id": self.cat_id, "name": "Cat"}])

        # folders in a category (paginated: page/per_page honored)
        if method == "GET" and path == f"{pfx}/solutions/categories/{self.cat_id}/folders":
            return self._paged_json(
                req, [{"id": fid, "name": f"Folder {fid}"} for fid in self.folders]
            )

        # subfolders of a given folder id (paginated)
        if (
            method == "GET"
            and path.endswith("/subfolders")
            and path.startswith(f"{pfx}/solutions/folders/")
        ):
            fid = path.split("/solutions/folders/")[1].split("/")[0]
            kids = self.subfolders.get(fid, [])
            return self._paged_json(req, [{"id": k, "name": f"Folder {k}"} for k in kids])

        # articles in a folder (paginated)
        if (
            method == "GET"
            and path.startswith(f"{pfx}/solutions/folders/")
            and path.endswith("/articles")
        ):
            fid = path.split("/solutions/folders/")[1].split("/")[0]
            aids = self.folder_articles.get(fid, [])
            return self._paged_json(req, [{"id": aid} for aid in aids])

        # GET master article: /solutions/articles/{id}
        if (
            method == "GET"
            and path.startswith(f"{pfx}/solutions/articles/")
            and path.count("/") == 5
        ):
            aid = path.rsplit("/", 1)[-1]
            art = self.articles.get(aid)
            if art is not None:
                return httpx.Response(200, json=dict(art))
            return httpx.Response(404, json={"error": "not found"})

        # translation routes: /solutions/articles/{id}/{lang}
        if path.startswith(f"{pfx}/solutions/articles/") and path.count("/") == 6:
            parts = path.split("/solutions/articles/")[1].split("/")
            aid, lang = parts[0], parts[1]
            if method == "GET":
                if lang in self.probe_403_langs:
                    return httpx.Response(
                        403,
                        json={
                            "code": "access_denied",
                            "message": "multilingual requires a higher plan",
                        },
                    )
                if lang in self.probe_400_langs:
                    return httpx.Response(
                        400,
                        json={"code": "invalid_lang", "message": f"unsupported language {lang}"},
                    )
                tr = self.translations.get((aid, lang))
                if tr is not None:
                    return httpx.Response(200, json=dict(tr))
                return httpx.Response(404, json={"error": "no translation"})
            if method in ("POST", "PUT"):
                if self.plan_gate:
                    # multilingual not available on this plan
                    return httpx.Response(
                        403,
                        json={
                            "code": "access_denied",
                            "message": "multilingual requires a higher plan",
                        },
                    )
                body = json.loads(req.content)
                self.writes.append((aid, lang, method, body))
                self.translations[(aid, lang)] = {"id": aid, **body}
                return httpx.Response(200 if method == "PUT" else 201, json={"id": aid, **body})

        return httpx.Response(404, json={"error": "unexpected", "path": path, "method": method})

    @staticmethod
    def _paged_json(req: httpx.Request, items: list) -> httpx.Response:
        """Serve `items` honoring ?page=n (1-based) & ?per_page — mirrors a real
        Freshdesk list endpoint so pagination-cap tests can exercise > 1 page."""
        page = int(req.url.params.get("page") or 1)
        per_page = int(req.url.params.get("per_page") or 30)
        start = (page - 1) * per_page
        return httpx.Response(200, json=items[start : start + per_page])


def _article(
    aid,
    title="Reset password",
    description="<h1>Reset</h1><p>Click the link to reset your password.</p>",
    status=2,
    updated_at="2026-07-01T00:00:00Z",
):
    return {
        "id": aid,
        "title": title,
        "description": description,
        "status": status,
        "updated_at": updated_at,
        "folder_id": 10,
        "category_id": 1,
    }


def _fconn(name, extra_cfg=None, secret=None):
    cfg = {"domain": "acme", "target_locales": "fr, de"}
    cfg.update(extra_cfg or {})
    sec = {"api_key": "key-abc"}
    sec.update(secret or {})
    return cstore.create("freshdesk", name, cfg, sec, actor="admin")


def main() -> None:
    init_db()

    # ---- (0) framework seam: SubResourceStrategy + capability (poll-only) ----
    strat = SubResourceStrategy()
    assert strat.topology == "T2"
    assert FreshdeskConnector.capability.topology == "T2"
    assert FreshdeskConnector.capability.change_signals.webhook_sig is None, (
        "Freshdesk Solutions have NO webhook (poll-only)"
    )
    print("  framework: T2 strategy + capability declares poll-only (no webhook)")

    # ---- (1) connector: test + primary_language + enabled_locales + discover ----
    ff = FakeFreshdesk(articles=[_article("100"), _article("101", title="Billing")])
    fc = FreshdeskConnector({"domain": "acme"}, {"api_key": "k"}, transport=ff.transport())
    ok, msg = fc.test()
    assert ok, msg
    assert "primary language en" in msg, msg
    assert fc.primary_language() == "en", fc.primary_language()
    assert set(fc.enabled_locales()) == {"fr", "de", "es"}, fc.enabled_locales()
    arts = fc.discover_articles()
    assert sorted(a["id"] for a in arts) == ["100", "101"], [a["id"] for a in arts]
    print("  connector: test + primary_language(en) + enabled_locales(3) + walk(2 articles)")

    # ---- (2) extract: title + HTML body via the html filter ----
    from app.filters import get_filter

    units, chash = fc.extract_units(_article("100"), get_filter)
    assert units[0]["field"] == "title" and units[0]["text"] == "Reset password", units[0]
    assert any(u["field"] == "body" for u in units), units
    assert len(chash) == 32, chash
    print("  extract: title (plain) + HTML body routed through the html filter")

    # ---- (3) write-back CREATE roundtrip (404 -> POST), status draft by default ----
    ff1 = FakeFreshdesk(articles=[_article("100")])
    conn = _fconn("FD create")
    ok, msg, s = cstore.freshdesk_translate(conn.id, actor="auto", transport=ff1.transport())
    assert ok, msg
    assert s["created"] == 2 and s["updated"] == 0, s
    assert {lang for _a, lang, _m, _b in ff1.writes} == {"fr", "de"}, ff1.writes
    for _a, lang, method, body in ff1.writes:  # noqa: B007
        assert method == "POST", ("missing translation -> POST create", method)
        assert body["status"] == 1, ("draft default (status 1)", body)
        assert "title" in body and "description" in body, body
        assert "seo_data" not in body, ("seo_data deferred in v1", body)
    print("  write-back CREATE: 404->POST for fr+de, status=1 (draft), no seo_data")

    # ---- (4) write-back UPDATE path (existing translation -> PUT) ----
    ff2 = FakeFreshdesk(articles=[_article("100")])
    ff2.add_translation("100", "de", title="Alt")  # de already exists
    conn2 = _fconn("FD update")
    ok, msg, s2 = cstore.freshdesk_translate(conn2.id, actor="auto", transport=ff2.transport())
    assert ok, msg
    assert s2["created"] == 1 and s2["updated"] == 1, s2  # fr created, de updated
    methods = {lang: method for _a, lang, method, _b in ff2.writes}
    assert methods == {"fr": "POST", "de": "PUT"}, methods
    print("  write-back UPDATE: existing de -> GET 200 -> PUT; missing fr -> POST")

    # ---- (5) publish toggle: publish=1 writes status 2 ----
    ff3 = FakeFreshdesk(articles=[_article("100")])
    conn3 = _fconn("FD publish", {"publish": "1", "target_locales": "fr"})
    ok, msg, _s3 = cstore.freshdesk_translate(conn3.id, actor="auto", transport=ff3.transport())
    assert ok, msg
    assert ff3.writes[0][3]["status"] == 2, ("publish=1 -> status 2", ff3.writes[0][3])
    print("  publish toggle: publish=1 -> status 2 (published); default -> status 1 (draft)")

    # ---- (6) POLL-ONLY suppression: same hash suppressed, changed hash re-enqueued -
    art_v1 = _article("200", updated_at="2026-07-01T00:00:00Z")
    ff4 = FakeFreshdesk(articles=[art_v1])
    conn4 = _fconn("FD suppress")
    ok, _m, s4a = cstore.freshdesk_translate(conn4.id, actor="auto", transport=ff4.transport())
    assert s4a["created"] == 2, s4a
    # Second run: updated_at BUMPED but content UNCHANGED + translations exist -> suppressed.
    art_v1b = _article("200", updated_at="2026-07-02T00:00:00Z")  # updated_at bump, same content
    ff5 = FakeFreshdesk(articles=[art_v1b])
    ff5.add_translation("200", "fr")
    ff5.add_translation("200", "de")
    ok, _m, s4b = cstore.freshdesk_translate(conn4.id, actor="auto", transport=ff5.transport())
    assert ok and s4b["skipped"] == 2 and s4b["created"] == 0 and s4b["updated"] == 0, s4b
    assert not ff5.writes, ("no writes on unchanged content (updated_at bump ignored)", ff5.writes)
    # Third run: updated_at bump AND content changed -> re-translated (PUT).
    art_v2 = _article(
        "200", description="<p>Brand new steps here.</p>", updated_at="2026-07-03T00:00:00Z"
    )
    ff6 = FakeFreshdesk(articles=[art_v2])
    ff6.add_translation("200", "fr")
    ff6.add_translation("200", "de")
    ok, _m, s4c = cstore.freshdesk_translate(conn4.id, actor="auto", transport=ff6.transport())
    assert ok and s4c["updated"] == 2 and s4c["skipped"] == 0, (
        "changed content -> re-translate",
        s4c,
    )
    print(
        "  poll-only: updated_at bump + same hash -> 2 skipped/0 writes; "
        "bump + changed hash -> 2 updated"
    )

    # ---- (7) IMMUTABLE PRIMARY LANGUAGE: non-en portal, source != assumed en ----
    # Portal primary is 'fr'; supported de/es. Source is fr (read from API, not en).
    ff7 = FakeFreshdesk(primary="fr", supported=("de", "es"), articles=[_article("100")])
    conn7 = _fconn("FD non-en", {"target_locales": "de, es"})
    ok, msg, s7 = cstore.freshdesk_translate(conn7.id, actor="auto", transport=ff7.transport())
    assert ok, msg
    assert s7["primary"] == "fr", ("source read from API != assumed en", s7)
    assert {lang for _a, lang, _m, _b in ff7.writes} == {"de", "es"}, ff7.writes
    print("  immutable primary: non-en portal (primary=fr) -> source is fr, de+es written")

    # ---- (7b) translating INTO the primary language is dropped ----
    ff7b = FakeFreshdesk(primary="en", supported=("fr",), articles=[_article("100")])
    conn7b = _fconn("FD into-primary", {"target_locales": "en, fr"})
    ok, msg, s7b = cstore.freshdesk_translate(conn7b.id, actor="auto", transport=ff7b.transport())
    assert ok, msg
    assert s7b["locales"] == ["fr"] and "en" in s7b["rejected"], s7b
    assert {lang for _a, lang, _m, _b in ff7b.writes} == {"fr"}, ff7b.writes
    print("  immutable primary: target 'en' == primary dropped; only fr written")

    # ---- (8) UNPUBLISHED-MASTER guard: draft master (status 1) skipped, not green --
    assert _is_published({"status": 2}) and not _is_published({"status": 1}), "status helper"
    ff8 = FakeFreshdesk(articles=[_article("100", status=1)])  # draft master
    conn8 = _fconn("FD draft-master", {"target_locales": "fr"})
    ok, msg, s8 = cstore.freshdesk_translate(conn8.id, actor="auto", transport=ff8.transport())
    assert not ok, ("draft master -> run not green", msg)
    assert s8["unpublished_skipped"] == 1 and s8["created"] == 0, s8
    assert not ff8.writes, ("no translation write for a draft master", ff8.writes)
    assert "not published" in msg, msg
    with Session(engine) as sess:
        assert sess.get(Connection, conn8.id).status == "error", "draft-master -> error status"
    print("  unpublished-master: draft master (status 1) skipped, run not green, error status")

    # ---- (8b) a published master alongside a draft: published one still writes ----
    ff8b = FakeFreshdesk(articles=[_article("100", status=2), _article("101", status=1)])
    conn8b = _fconn("FD mixed-status", {"target_locales": "fr"})
    ok, msg, s8b = cstore.freshdesk_translate(conn8b.id, actor="auto", transport=ff8b.transport())
    # published article wrote, but the draft-master error keeps the run not-green.
    assert s8b["created"] == 1 and s8b["unpublished_skipped"] == 1, s8b
    assert {a for a, _l, _m, _b in ff8b.writes} == {"100"}, (
        "only published 100 written",
        ff8b.writes,
    )
    assert not ok, ("a draft master present keeps the run not green", msg)
    print("  mixed status: published master writes; a draft master alongside keeps run not green")

    # ---- (9) PLAN-GATE 4xx surfaces cleanly (not a crash, not green) ----
    ff9 = FakeFreshdesk(articles=[_article("100")], plan_gate=True)
    conn9 = _fconn("FD plan-gate", {"target_locales": "fr"})
    ok, msg, s9 = cstore.freshdesk_translate(conn9.id, actor="auto", transport=ff9.transport())
    assert not ok, ("plan-gated multilingual 4xx -> run not green", msg)
    assert s9["created"] == 0 and s9["updated"] == 0, s9
    assert s9["failures"] and "403" in s9["failures"][0]["error"], s9["failures"]
    with Session(engine) as sess:
        assert sess.get(Connection, conn9.id).status == "error", "plan-gate -> error status"
    print(
        "  plan-gate: 403 on the translation write surfaces cleanly (failure, not crash), not green"
    )

    # ---- (10) supported-language rejection + ALL rejected -> NOT green ----
    ff10 = FakeFreshdesk(supported=("fr",), articles=[_article("100")])
    conn10 = _fconn("FD reject", {"target_locales": "fr, de, ja"})
    ok, msg, s10 = cstore.freshdesk_translate(conn10.id, actor="auto", transport=ff10.transport())
    assert ok, msg
    assert s10["locales"] == ["fr"] and set(s10["rejected"]) == {"de", "ja"}, s10
    assert {lang for _a, lang, _m, _b in ff10.writes} == {"fr"}, ff10.writes
    # all rejected -> not green + error
    ff10b = FakeFreshdesk(supported=("es",), articles=[_article("100")])
    conn10b = _fconn("FD all-reject", {"target_locales": "de, fr"})
    ok, msg, s10b = cstore.freshdesk_translate(
        conn10b.id, actor="auto", transport=ff10b.transport()
    )
    assert not ok and s10b["locales"] == [] and set(s10b["rejected"]) == {"de", "fr"}, (msg, s10b)
    assert not ff10b.writes, ff10b.writes
    with Session(engine) as sess:
        assert sess.get(Connection, conn10b.id).status == "error", "all-rejected -> error"
    print("  rejection: unsupported langs dropped; ALL-rejected -> ok=False + status=error")

    # ---- (11) 429 backoff honored (Retry-After) on a GET ----
    slept = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        ff11 = FakeFreshdesk(articles=[_article("100")])
        ff11.rate_limit_once_on = "/settings/helpdesk"
        fc11 = FreshdeskConnector({"domain": "acme"}, {"api_key": "k"}, transport=ff11.transport())
        assert fc11.primary_language() == "en"
        assert slept == [5.0], ("honored Retry-After: 5", slept)
    finally:
        cbase._sleep = real_sleep
    print("  backoff: 429 + Retry-After on a GET retried once (honored 5s)")

    # ---- (12) org scoping: project inherits the connection's org ----
    ff12 = FakeFreshdesk(articles=[_article("100")])
    conn12 = _fconn("FD org")
    cstore.freshdesk_translate(conn12.id, actor="auto", transport=ff12.transport())
    row12 = cstore.get(conn12.id)
    with Session(engine) as sess:
        proj = sess.get(Project, row12.project_id)
    assert proj is not None
    assert getattr(proj, "org_id", None) == getattr(row12, "org_id", None), (
        "connector project inherits connection org",
        proj.org_id,
        row12.org_id,
    )
    print("  org scoping: connector-created project inherits the connection's org")

    # ---- (13) SubResourceStrategy probe: 404 -> ABSENT -> create; present -> update -
    ff13 = FakeFreshdesk(articles=[_article("100")])
    fc13 = FreshdeskConnector({"domain": "acme"}, {"api_key": "k"}, transport=ff13.transport())
    assert fc13.get_translation("100", "fr") is ABSENT, "missing translation -> ABSENT"
    ff13.add_translation("100", "fr", title="Exists")
    got = fc13.get_translation("100", "fr")
    assert got is not ABSENT and got.get("title") == "Exists", got
    print("  strategy probe: 404 -> ABSENT (create); populated -> present (update)")

    # ---- (14) routes: Freshdesk appears in the add-connector flow ----
    c = TestClient(app)
    idx = c.get("/connectors").text
    assert "Freshdesk" in idx, "Freshdesk tile missing from /connectors"
    typ = c.get("/connectors/freshdesk").text
    # Rebuilt and un-archived (rebuild): the type page offers the add form again.
    assert "Create connection" in typ and "New connections are closed" not in typ, (
        "un-archived Freshdesk type page must render the add form"
    )
    assert not _reg.is_archived("freshdesk")
    _fnames = {f.name for f in _reg.spec("freshdesk").config_fields} | {
        f.name for f in _reg.spec("freshdesk").secret_fields
    }
    assert "api_key" in _fnames, ("Freshdesk spec must still declare 'api_key'", _fnames)
    print("  routes: Freshdesk tile present (active) + add form + spec fields pinned")

    # ---- (15) CLI freshdesk-translate round-trip ----
    ff15 = FakeFreshdesk(articles=[_article("100")])
    connc = _fconn("FD cli", {"target_locales": "fr"})
    real_ft = cstore.freshdesk_translate
    seen = {}

    def _capturing_ft(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        seen["called"] = True
        return real_ft(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=ff15.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "freshdesk_translate", _capturing_ft)
    try:
        from app.cli.cmd_connector import _freshdesk_translate

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

        rc = _freshdesk_translate(_Args(), _Emit())
        assert rc == 0 and emitted.get("ok") and seen.get("called"), (rc, emitted)
        assert {lang for _a, lang, _m, _b in ff15.writes} == {"fr"}, ff15.writes
    finally:
        set_pieces("app.services.connectors.store", "freshdesk_translate", real_ft)
    print("  CLI: freshdesk-translate ran the flow, wrote fr version")

    # ---- (16) retry_failed re-runs ONLY the failed (article, lang) pairs ----
    ff16 = FakeFreshdesk(articles=[_article("100"), _article("101", title="Other")])
    connret = _fconn("FD retry")
    failures = [
        {"article_id": "100", "locale": "de", "error": "500"},
        {"article_id": "101", "locale": "fr", "error": "400"},
    ]
    okr, _m, sumr = cstore.retry_failed(
        connret.id, failures, actor="auto", transport=ff16.transport()
    )
    assert okr, sumr
    written = {(a, lang) for a, lang, _m, _b in ff16.writes}
    assert written == {("100", "de"), ("101", "fr")}, ("only the 2 failed pairs", written)
    print("  retry: re-ran ONLY the 2 failed (article, lang) pairs")

    # ---- (17) TargetLocale rows stored catalog-canonical; wire calls stay lowercase -
    ff17 = FakeFreshdesk(supported=("pt-br",), articles=[_article("100")])
    conn17 = _fconn("FD canonical", {"target_locales": "pt-br"})
    ok, _m, s17 = cstore.freshdesk_translate(conn17.id, actor="auto", transport=ff17.transport())
    assert ok and s17["locales"] == ["pt-br"], s17
    assert {lang for _a, lang, _m, _b in ff17.writes} == {"pt-br"}, (
        "wire write-back stays lowercase",
        ff17.writes,
    )
    proj17 = cstore.get(conn17.id).project_id
    with Session(engine) as sess:
        stored = {
            tl.locale
            for tl in sess.exec(select(TargetLocale).where(TargetLocale.project_id == proj17)).all()
        }
    assert "pt-BR" in stored, ("TargetLocale row canonicalized", stored)
    print(
        "  regression: TargetLocale stored canonical (pt-BR) while write-back stays lowercase (pt-br)"  # noqa: E501
    )

    # ---- (18) DEFENSE-IN-DEPTH: a NEEDS_REVIEW segment is excluded from write-back --
    import app.services.translate as translate_mod

    real_ptd = translate_mod.pretranslate_document

    def _ptd_then_flip_de(session, doc_id, locale, spec=None, force_pii=False):
        stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == "de":
            # order_index 1 == the first BODY unit (0 is the title).
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
        ff18 = FakeFreshdesk(articles=[_article("600")])
        conn18 = _fconn("FD needs-review guard")
        ok, msg, _s18 = cstore.freshdesk_translate(
            conn18.id, actor="auto", transport=ff18.transport()
        )
        assert ok, msg
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    de_writes = [w for w in ff18.writes if w[1] == "de"]
    fr_writes = [w for w in ff18.writes if w[1] == "fr"]
    # #640: the held pair is NOT created at all (a created target would carry the source text
    # in the held unit); the flagged text therefore cannot reach the wire. fr is untouched.
    assert fr_writes and not de_writes, (fr_writes, de_writes)
    print("  defense-in-depth: a NEEDS_REVIEW segment is excluded from Freshdesk write-back")

    # ---- (19) pagination tolerates a 4xx past the last page; 4xx on page 1 raises --
    from app.services.connectors.base import ConnectorError

    class _PagedTransport:
        def __init__(self):
            self.calls = []

        def handler(self, req: httpx.Request) -> httpx.Response:
            page = req.url.params.get("page")
            self.calls.append(page)
            if page == "1":
                return httpx.Response(200, json=[{"id": str(i)} for i in range(100)])
            return httpx.Response(400, json={"error": "no more pages"})

    pt = _PagedTransport()
    fc19 = FreshdeskConnector(
        {"domain": "acme"}, {"api_key": "k"}, transport=httpx.MockTransport(pt.handler)
    )
    with fc19._client() as cc:
        got = fc19._paginate(cc, fc19._sol("/folders/10/articles"), cap=1000)
    assert len(got) == 100, ("full first page returned despite 400 on page 2", len(got))
    assert pt.calls == ["1", "2"], pt.calls

    def _always_400(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    fc19b = FreshdeskConnector(
        {"domain": "acme"}, {"api_key": "k"}, transport=httpx.MockTransport(_always_400)
    )
    raised = False
    try:
        with fc19b._client() as cc:
            fc19b._paginate(cc, fc19b._sol("/folders/10/articles"), cap=1000)
    except ConnectorError:
        raised = True
    assert raised, "a 4xx on page 1 (no prior results) must still raise"
    print("  pagination: 400 past-end tolerated (100 items); 400 on page 1 raises")

    # ---- (20) REGRESSION: plan-gated tenant 403s the translation GET probe ----
    # write_target's exists_target -> get_translation probe hits a 403 (not the usual
    # 404-absent), which used to raise ConnectorError straight out of the write loop
    # and crash the whole run. Must now land as a per-pair failure, run ERROR not a
    # crash, and NO exception escapes cstore.freshdesk_translate.
    ff20 = FakeFreshdesk(articles=[_article("100")], probe_403_langs={"fr", "de"})
    conn20 = _fconn("FD probe-403")
    ok, msg, s20 = cstore.freshdesk_translate(conn20.id, actor="auto", transport=ff20.transport())
    assert not ok, ("GET-probe 403 -> run not green", msg)
    assert s20["created"] == 0 and s20["updated"] == 0, s20
    assert len(s20["failures"]) == 2, s20["failures"]
    assert all("403" in f["error"] for f in s20["failures"]), s20["failures"]
    assert not ff20.writes, ("no write reached — the probe itself failed", ff20.writes)
    with Session(engine) as sess:
        assert sess.get(Connection, conn20.id).status == "error", (
            "GET-probe 403 -> connection status error"
        )
    print(
        "  regression: plan-gated GET probe 403 -> per-pair failure recorded, no crash, run ERROR"
    )

    # ---- (21) REGRESSION: settings 404 fallback + unsupported locale 400s the probe --
    # settings/helpdesk 404s (some tenants lack the route) -> enabled_locales() falls
    # back to configured target_locales + primary, which can let an actually-
    # unsupported locale through. Its translation GET then 400s. Must be a clean
    # per-pair failure, never a crash, run ERROR.
    ff21 = FakeFreshdesk(articles=[_article("100")], settings_404=True, probe_400_langs={"zz"})
    conn21 = _fconn("FD settings-404-unsupported", {"target_locales": "zz"})
    ok, msg, s21 = cstore.freshdesk_translate(conn21.id, actor="auto", transport=ff21.transport())
    assert not ok, ("settings-404 fallback + unsupported-locale 400 -> run not green", msg)
    assert s21["created"] == 0 and s21["updated"] == 0, s21
    assert len(s21["failures"]) == 1 and "400" in s21["failures"][0]["error"], s21["failures"]
    assert not ff21.writes, ff21.writes
    with Session(engine) as sess:
        assert sess.get(Connection, conn21.id).status == "error", (
            "settings-404 fallback + unsupported-locale 400 -> status error"
        )
    print(
        "  regression: settings-404 fallback + unsupported-locale GET 400 -> "
        "per-pair failure, no crash, run ERROR"
    )

    # ---- (22) folder pagination decoupled from max_articles: 60 folders discovered --
    # even with max_articles=1 (article accumulation cap), ALL 60 folders must be
    # walked for discovery — folder listing uses FOLDER_LIST_CAP, not max_articles.
    many_folders = [str(100 + i) for i in range(60)]
    ff22 = FakeFreshdesk(folders=many_folders)
    ff22.folder_articles = {many_folders[-1]: ["900"]}
    ff22.articles = {"900": _article("900")}
    fc22 = FreshdeskConnector(
        {"domain": "acme", "category_id": "1", "max_articles": "1"},
        {"api_key": "k"},
        transport=ff22.transport(),
    )
    with fc22._client() as cc:
        found = fc22._folder_ids_in_category(cc, "1")
    assert sorted(found) == sorted(many_folders), (
        "all 60 folders discovered regardless of max_articles=1",
        len(found),
    )
    # end-to-end: the one article living in the LAST folder is still reachable.
    arts22 = fc22.discover_articles(cap=1)
    assert [a["id"] for a in arts22] == ["900"], (
        "article in the 60th folder is still discovered",
        arts22,
    )
    print("  regression: 60 folders discovered with max_articles=1 (folder cap decoupled)")

    # ---- (23) real subfolder BFS: a sub-sub-folder's article is discovered ----
    # 10 (top) -> 20 (subfolder) -> 30 (sub-subfolder), article lives only in 30.
    ff23 = FakeFreshdesk(folders=["10"], subfolders={"10": ["20"], "20": ["30"]})
    ff23.folder_articles = {"10": [], "20": [], "30": ["500"]}
    ff23.articles = {"500": _article("500", title="Deeply nested")}
    fc23 = FreshdeskConnector(
        {"domain": "acme", "category_id": "1"}, {"api_key": "k"}, transport=ff23.transport()
    )
    with fc23._client() as cc:
        found23 = fc23._folder_ids_in_category(cc, "1")
    assert set(found23) == {"10", "20", "30"}, ("BFS reaches the sub-sub-folder", found23)
    arts23 = fc23.discover_articles()
    assert [a["id"] for a in arts23] == ["500"], (
        "article in the sub-sub-folder is discovered via real BFS",
        arts23,
    )
    print("  regression: real subfolder BFS discovers a sub-sub-folder's article")

    print("verify_freshdesk OK")


if __name__ == "__main__":
    main()
