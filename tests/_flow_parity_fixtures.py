"""Shared fixtures + scenarios for the ConnectorTranslateFlow parity harness.

NOT a pytest module and NOT a ``verify_*`` suite (leading underscore keeps it out
of both collectors). It is imported by:

  * ``capture_flow_goldens.py``  — run ONCE against the PRE-refactor store.py to
    freeze golden outputs into ``flow_engine_goldens.json``.
  * ``verify_flow_engine_parity.py`` — the repeatable suite that re-runs the SAME
    scenarios against the POST-refactor engine and asserts byte-equality.

Each scenario builds a fresh connection against an in-memory Fake tenant
(httpx.MockTransport), drives one of the four T2 translate flows (possibly after
priming runs, for suppression), and returns a fully JSON-serializable record:
(ok, msg, result-dict, the connection's SyncLog rows, the connection's persisted
hash/watermark config state). Old vs new must match byte-for-byte.

The Fake tenant classes + article/connection builders are copied faithfully from
tests/verify_{zendesk,document360,freshdesk,zoho}.py so the transports behave
exactly as the flows expect; only write-failure hooks are added where a scenario
needs to exercise the per-pair failure / watermark-hold path.
"""

import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
os.environ.setdefault("LOC_TMS_DATA_DIR", tempfile.mkdtemp(prefix="loctms_flowparity_"))
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["LOCAL_BASE_URL"] = ""

import httpx
from sqlmodel import Session, select

import app.config as _cfgmod
from app.db import engine, init_db
from app.models import Connection, SyncLog
from app.services.connectors import store as cstore


# ===========================================================================
# Fake Zendesk Guide tenant (from verify_zendesk.FakeZendesk) + a write-fail hook
# ===========================================================================
class FakeZendesk:
    def __init__(
        self,
        enabled_locales=("en-us", "de", "fr"),
        articles=None,
        fail_write_locales=(),
        sections=None,
    ):
        self.enabled = list(enabled_locales)
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        self.translations: dict = {}
        self.creates: list = []
        self.updates: list = []
        self.label_puts: list = []
        self.incremental_pages = []
        self.fail_write_locales = set(fail_write_locales)  # locales whose write 400s
        # Phase 3B: section id -> name, served at GET /help_center/sections/{id} for
        # the context-harvest section resolution. `section_gets` counts real fetches so
        # a test can prove the connector caches (one GET per distinct section per pull).
        self.sections = {str(k): v for k, v in (sections or {}).items()}
        self.section_gets: list = []

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

        if method == "GET" and path == f"{pfx}/locales":
            return httpx.Response(200, json={"locales": self.enabled, "default_locale": "en-us"})

        if method == "GET" and path == f"{pfx}/incremental/articles":
            st = int(req.url.params.get("start_time", "0"))
            for arts, end_time, next_start in self.incremental_pages:
                if next_start is None:
                    return httpx.Response(
                        200, json={"articles": arts, "end_time": end_time, "next_page": None}
                    )
                if st <= (next_start - 1):
                    np = f"https://x.zendesk.com{pfx}/incremental/articles?start_time={end_time}"
                    return httpx.Response(
                        200, json={"articles": arts, "end_time": end_time, "next_page": np}
                    )
            return httpx.Response(200, json={"articles": [], "end_time": st, "next_page": None})

        if method == "GET" and path == f"{pfx}/articles":
            arts = list(self.articles.values())
            labels = req.url.params.get("label_names")
            if labels:
                want = set(labels.split(","))
                arts = [a for a in arts if want.issubset(set(a.get("label_names") or []))]
            return httpx.Response(200, json={"articles": arts, "meta": {"has_more": False}})

        if method == "GET" and path.startswith(f"{pfx}/sections/") and path.endswith("/articles"):
            sid = path.split("/sections/")[1].split("/")[0]
            arts = [a for a in self.articles.values() if str(a.get("section_id")) == sid]
            return httpx.Response(200, json={"articles": arts, "meta": {"has_more": False}})

        # Phase 3B: section id -> name (context harvest). Distinct from the
        # /sections/{id}/articles list route above (this one does NOT end in /articles).
        if (
            method == "GET"
            and path.startswith(f"{pfx}/sections/")
            and not path.endswith("/articles")
            and path.count("/") == 5
        ):
            sid = path.rsplit("/", 1)[-1]
            self.section_gets.append(sid)
            name = self.sections.get(sid)
            if name is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"section": {"id": sid, "name": name}})

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

        if method == "GET" and "/translations/" in path:
            aid = path.split("/articles/")[1].split("/")[0]
            loc = path.rsplit("/", 1)[-1]
            tr = self.translations.get((aid, loc))
            return (
                httpx.Response(200, json={"translation": tr})
                if tr
                else httpx.Response(404, json={})
            )

        if method == "POST" and path.endswith("/translations"):
            aid = path.split("/articles/")[1].split("/")[0]
            body = json.loads(req.content)["translation"]
            loc = body["locale"]
            if loc in self.fail_write_locales:
                return httpx.Response(400, json={"error": "write rejected"})
            self.creates.append((aid, loc, body))
            self.translations[(aid, loc)] = {"id": 9000 + len(self.translations), **body}
            return httpx.Response(201, json={"translation": self.translations[(aid, loc)]})

        if method == "PUT" and "/translations/" in path:
            aid = path.split("/articles/")[1].split("/")[0]
            loc = path.rsplit("/", 1)[-1]
            if loc in self.fail_write_locales:
                return httpx.Response(400, json={"error": "write rejected"})
            body = json.loads(req.content)["translation"]
            self.updates.append((aid, loc, body))
            self.translations.setdefault((aid, loc), {"id": 9999, "locale": loc})
            self.translations[(aid, loc)].update(body)
            return httpx.Response(200, json={"translation": self.translations[(aid, loc)]})

        if method == "PUT" and path.startswith(f"{pfx}/articles/") and path.count("/") == 5:
            aid = path.rsplit("/", 1)[-1]
            body = json.loads(req.content)["article"]
            if "label_names" in body:
                self.label_puts.append((aid, body["label_names"]))
                if aid in self.articles:
                    self.articles[aid]["label_names"] = body["label_names"]
            return httpx.Response(200, json={"article": self.articles.get(aid, {})})

        return httpx.Response(404, json={"error": "unexpected", "path": path, "method": method})


def _zd_article(
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


def _zd_conn(name, extra_cfg=None, secret=None):
    cfg = {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de, fr"}
    cfg.update(extra_cfg or {})
    sec = {"email": "a@acme.com", "api_token": "tok123"}
    sec.update(secret or {})
    return cstore.create("zendesk", name, cfg, sec, actor="admin")


# ===========================================================================
# Fake Document360 tenant (from verify_document360.FakeD360)
# ===========================================================================
D360_PV = "pv-main"
D360_USER = "user-1"
_D360_ALLOWED_PUT = {
    "title",
    "content",
    "html_content",
    "category_id",
    "hidden",
    "version_number",
    "translation_option",
    "source",
    "order",
}


class FakeD360:
    """A fake Document360 project that follows the CURRENT v2 docs (apidocs.document360.com):
    workspaces, `/Language/{pv}`, flat article list with 0-based pages, category tree, per-language
    article versions (draft = status 0, published = status 3), a PARTIAL `PUT` whose body rejects
    unknown properties (so a `publish` key is a 400), `PUT .../fork` and `POST .../publish`
    (both need user_id and version_number).

    `missing_mode` picks how a language with no version answers a GET: "400" (article not found,
    the default), "404", "null" (200 with success false) or "fallback" (200 with the default
    language's content and is_fall_back_content true).

    Test hooks: `live_edits` records every content PUT that hit a PUBLISHED version in place
    (a connector must never do that), `writes` every content PUT, `forks`, `publishes`."""

    def __init__(
        self, enabled_langs=("en", "de", "fr"), articles=None, pv=D360_PV, missing_mode="400"
    ):
        self.enabled = list(enabled_langs)
        self.pv = pv
        self.missing_mode = missing_mode
        # article_id -> source (en) article dict
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        # (article_id, lang) -> {"versions": {n: {...}}, "public": n | None}
        self.versions: dict = {}
        self.writes: list = []  # (article_id, lang, body) - every content PUT
        self.forks: list = []  # (article_id, body)
        self.publishes: list = []  # (article_id, lang) - every publish POST
        self.publish_bodies: list = []
        self.live_edits: list = []  # (article_id, lang, version) edited while published
        self.gets: list = []  # (path, params) of every GET
        self.categories = None  # None = one root category holding every article
        self.page_size_cap = 100
        self._rate_state = {"n": 0}
        self.rate_limit_once_on = None
        self.put_error_for = {}  # lang -> status code to fail the content PUT with

    # -- seeding ---------------------------------------------------------------
    def add_version(
        self,
        article_id,
        lang,
        title="",
        content="",
        html_content="",
        translation_option=None,
        published=False,
    ):
        """Seed an existing language version: a draft (default) or a published one."""
        rec = {
            "title": title,
            "content": content or html_content,
            "status": 3 if published else 0,
        }
        if translation_option is not None:
            rec["translation_option"] = translation_option
        self.versions[(str(article_id), lang)] = {
            "versions": {1: rec},
            "public": 1 if published else None,
        }

    def latest(self, article_id, lang):
        """The newest version dict of a language (or None)."""
        slot = self.versions.get((str(article_id), lang))
        if not slot:
            return None
        return slot["versions"][max(slot["versions"])]

    def transport(self):
        return httpx.MockTransport(self._handle)

    # -- wire helpers ----------------------------------------------------------
    @staticmethod
    def _ok(data, **extra):
        return httpx.Response(200, json={"success": True, "data": data, **extra})

    @staticmethod
    def _err(status, text):
        return httpx.Response(
            status, json={"success": False, "data": None, "errors": [{"description": text}]}
        )

    def _view(self, aid, lang):
        slot = self.versions[(aid, lang)]
        latest = max(slot["versions"])
        rec = slot["versions"][latest]
        out = {
            "id": aid,
            "title": rec.get("title", ""),
            "content": rec.get("content", ""),
            "html_content": "",
            "version_number": latest,
            "latest_version": latest,
            "public_version": slot["public"],
            "status": rec.get("status", 0),
            "project_version_id": self.pv,
            "content_type": "markdown",
            "is_fall_back_content": False,
        }
        if "translation_option" in rec:
            out["translation_option"] = rec["translation_option"]
        return out

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        low = path.lower()
        pfx = "/v2"

        if self.rate_limit_once_on and self.rate_limit_once_on.lower() in low:
            self._rate_state["n"] += 1
            if self._rate_state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "rate"})
        if method == "GET":
            self.gets.append((path, dict(req.url.params)))

        if method == "GET" and low == f"{pfx}/projectversions":
            return self._ok([{"id": self.pv, "version_code_name": "v1", "is_main_version": True}])
        if method == "GET" and low == f"{pfx}/language/{self.pv.lower()}":
            return self._ok(
                [
                    {
                        "language_id": f"l-{c}",
                        "language_code": c,
                        "language_name": c,
                        "is_set_as_default": c == "en",
                    }
                    for c in self.enabled
                ]
            )
        if method == "GET" and low == f"{pfx}/projectversions/{self.pv.lower()}/articles":
            return self._list(req)
        if method == "GET" and low == f"{pfx}/projectversions/{self.pv.lower()}/categories":
            tree = self.categories
            if tree is None:
                tree = [
                    {
                        "id": "cat-root",
                        "name": "Root",
                        "articles": [{"id": a} for a in self.articles],
                        "child_categories": [],
                    }
                ]
            return self._ok(tree)

        parts = [p for p in path.split("/") if p]  # ["v2", "Articles", id, ...]
        if len(parts) >= 3 and parts[0] == "v2" and parts[1].lower() == "articles":
            aid = parts[2]
            if method == "PUT" and len(parts) == 4 and parts[3] == "fork":
                return self._fork(aid, req)
            if len(parts) == 4:
                lang = parts[3]
                if method == "GET":
                    return self._get_version(aid, lang)
                if method == "PUT":
                    return self._put_version(aid, lang, req)
            if method == "POST" and len(parts) == 5 and parts[4] == "publish":
                return self._publish(aid, parts[3], req)
        return httpx.Response(404, json={"error": "unexpected", "path": path, "method": method})

    def _list(self, req):
        q = req.url.params
        rows = [
            {
                "id": aid,
                "title": a.get("title"),
                "public_version": a.get("public_version"),
                "latest_version": a.get("latest_version", a.get("version_number")),
                "status": a.get("status", 3),
            }
            for aid, a in self.articles.items()
        ]
        hits = int(q.get("hitsPerPage") or 0)
        if hits <= 0:
            return self._ok(rows, pagination=None)
        hits = min(hits, self.page_size_cap)
        page = int(q.get("page") or 0)
        chunk = rows[page * hits : (page + 1) * hits]
        pag = {
            "page": page,
            "hits_per_page": hits,
            "has_more": (page + 1) * hits < len(rows),
            "result_count": len(chunk),
        }
        return self._ok(chunk, pagination=pag)

    def _get_version(self, aid, lang):
        if lang not in self.enabled:
            return self._err(400, f"Language '{lang}' is not enabled for this workspace.")
        if lang == "en" and aid in self.articles:
            src = dict(self.articles[aid])
            src.setdefault("project_version_id", self.pv)
            src.setdefault("status", 3)
            src.setdefault("latest_version", src.get("version_number"))
            src.setdefault("is_fall_back_content", False)
            return self._ok(src)
        if (aid, lang) in self.versions:
            return self._ok(self._view(aid, lang))
        if aid not in self.articles:
            return self._err(400, "Article not found")
        if self.missing_mode == "404":
            return httpx.Response(404, json={"success": False})
        if self.missing_mode == "null":
            return httpx.Response(200, json={"success": False, "data": None})
        if self.missing_mode == "fallback":
            src = dict(self.articles[aid])
            src["is_fall_back_content"] = True
            src.setdefault("project_version_id", self.pv)
            return self._ok(src)
        return self._err(400, "Article not found")

    def _put_version(self, aid, lang, req):
        body = json.loads(req.content)
        unknown = set(body) - _D360_ALLOWED_PUT
        if unknown:
            return self._err(400, f"Unknown properties: {sorted(unknown)}")
        if lang not in self.enabled:
            return self._err(400, f"Language '{lang}' is not enabled for this workspace.")
        if aid not in self.articles and (aid, lang) not in self.versions:
            return self._err(400, "Article not found")
        if lang in self.put_error_for:
            return httpx.Response(self.put_error_for[lang], json={"success": False})
        slot = self.versions.setdefault((aid, lang), {"versions": {}, "public": None})
        if not slot["versions"]:
            src = self.articles.get(aid, {})
            slot["versions"][1] = {
                "title": src.get("title", ""),
                "content": src.get("content") or src.get("html_content") or "",
                "status": 0,
            }
        n = body.get("version_number") or max(slot["versions"])
        if n not in slot["versions"]:
            return self._err(400, f"Version {n} does not exist")
        if slot["public"] == n:
            self.live_edits.append((aid, lang, n))
        rec = slot["versions"][n]
        for k in ("title", "content", "translation_option", "html_content"):
            if k in body:
                rec["content" if k == "html_content" else k] = body[k]
        self.writes.append((aid, lang, body))
        return self._ok(
            {
                "id": aid,
                "latest_version": max(slot["versions"]),
                "public_version": slot["public"],
            }
        )

    def _fork(self, aid, req):
        body = json.loads(req.content)
        self.forks.append((aid, body))
        for need in ("version_number", "user_id", "lang_code"):
            if need not in body:
                return self._err(400, f"The {need} field is required.")
        slot = self.versions.get((aid, body["lang_code"]))
        if not slot or body["version_number"] not in slot["versions"]:
            return self._err(400, f"Cannot fork version number {body['version_number']}")
        new = max(slot["versions"]) + 1
        slot["versions"][new] = {**slot["versions"][body["version_number"]], "status": 0}
        return self._ok({"version_number": new, "status": 0})

    def _publish(self, aid, lang, req):
        body = json.loads(req.content)
        if not body.get("user_id") or "version_number" not in body:
            return self._err(400, "user_id and version_number are required")
        slot = self.versions.get((aid, lang))
        n = body["version_number"]
        if not slot or n not in slot["versions"]:
            return self._err(400, "Article not found")
        if slot["public"] == n:
            return self._err(400, "Article has already been published.")
        slot["public"] = n
        slot["versions"][n]["status"] = 3
        self.publishes.append((aid, lang))
        self.publish_bodies.append(body)
        return self._ok({"id": aid, "public_version": n})


def _d360_md_article(
    aid,
    title="Reset password",
    content="# Reset {{glossary:pw}}\n\nClick the {{snippet:reset}} link to reset.",
    version_number=1,
):
    return {
        "id": aid,
        "title": title,
        "content": content,
        "html_content": "",
        "content_type": "markdown",
        "project_version_id": D360_PV,
        "version_number": version_number,
        "latest_version": version_number,
        "public_version": version_number,
        "status": 3,
        "source_lang": "en",
        "translation_option": 1,
    }


def _d360_html_article(
    aid,
    title="Billing",
    html_content="<h1>Billing {{var:plan}}</h1><p>Manage your plan.</p>",
    version_number=1,
):
    return {
        "id": aid,
        "title": title,
        "content": "",
        "html_content": html_content,
        "content_type": "wysiwyg",
        "project_version_id": D360_PV,
        "version_number": version_number,
        "latest_version": version_number,
        "public_version": version_number,
        "status": 3,
        "source_lang": "en",
        "translation_option": 1,
    }


def _d360_conn(name, extra_cfg=None, secret=None):
    cfg = {"source_lang": "en", "target_locales": "de, fr", "project_version_id": D360_PV}
    if str((extra_cfg or {}).get("publish", "0")) == "1":
        cfg["user_id"] = D360_USER  # publishing needs a team account id
    cfg.update(extra_cfg or {})
    sec = {"api_token": "tok-abc"}
    sec.update(secret or {})
    return cstore.create("document360", name, cfg, sec, actor="admin")


# ===========================================================================
# Fake Freshdesk tenant (from verify_freshdesk.FakeFreshdesk)
# ===========================================================================
class FakeFreshdesk:
    def __init__(
        self,
        primary="en",
        supported=("fr", "de", "es"),
        articles=None,
        plan_gate=False,
        folders=None,
        subfolders=None,
    ):
        self.primary = primary
        self.supported = list(supported)
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        self.translations: dict = {}
        self.writes: list = []
        self.plan_gate = plan_gate
        self.cat_id = "1"
        self.folder_id = "10"
        self.folders = list(folders) if folders is not None else [self.folder_id]
        self.subfolders = dict(subfolders or {})
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

        if method == "GET" and path == f"{pfx}/settings/helpdesk":
            return httpx.Response(
                200,
                json={
                    "primary_language": self.primary,
                    "supported_languages": list(self.supported),
                },
            )
        if method == "GET" and path == f"{pfx}/solutions/categories":
            return httpx.Response(200, json=[{"id": self.cat_id, "name": "Cat"}])
        if method == "GET" and path == f"{pfx}/solutions/categories/{self.cat_id}/folders":
            return self._paged(req, [{"id": fid, "name": f"Folder {fid}"} for fid in self.folders])
        if (
            method == "GET"
            and path.endswith("/subfolders")
            and path.startswith(f"{pfx}/solutions/folders/")
        ):
            fid = path.split("/solutions/folders/")[1].split("/")[0]
            kids = self.subfolders.get(fid, [])
            return self._paged(req, [{"id": k, "name": f"Folder {k}"} for k in kids])
        if (
            method == "GET"
            and path.startswith(f"{pfx}/solutions/folders/")
            and path.endswith("/articles")
        ):
            fid = path.split("/solutions/folders/")[1].split("/")[0]
            aids = self.folder_articles.get(fid, [])
            return self._paged(req, [{"id": aid} for aid in aids])
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
        if path.startswith(f"{pfx}/solutions/articles/") and path.count("/") == 6:
            parts = path.split("/solutions/articles/")[1].split("/")
            aid, lang = parts[0], parts[1]
            if method == "GET":
                tr = self.translations.get((aid, lang))
                if tr is not None:
                    return httpx.Response(200, json=dict(tr))
                return httpx.Response(404, json={"error": "no translation"})
            if method in ("POST", "PUT"):
                if self.plan_gate:
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
    def _paged(req, items):
        page = int(req.url.params.get("page") or 1)
        per_page = int(req.url.params.get("per_page") or 30)
        start = (page - 1) * per_page
        return httpx.Response(200, json=items[start : start + per_page])


def _fd_article(
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


def _fd_conn(name, extra_cfg=None, secret=None):
    cfg = {"domain": "acme", "target_locales": "fr, de"}
    cfg.update(extra_cfg or {})
    sec = {"api_key": "key-abc"}
    sec.update(secret or {})
    return cstore.create("freshdesk", name, cfg, sec, actor="admin")


# ===========================================================================
# Fake Zoho Desk tenant (from verify_zoho.FakeZoho)
# ===========================================================================
_ZO_HOST = "desk.zoho.eu"
_ZO_ORG = "600000000042"


class FakeZoho:
    def __init__(self, articles=None):
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        self.translations: dict = {}
        self.writes: list = []

    # status="Draft" by default: the connector writes drafts unless configured otherwise, so a
    # translation it created earlier is a draft. A test of a PUBLISHED one passes status.
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

        if method == "GET" and path == f"{pfx}/kbRootCategories":
            return httpx.Response(200, json={"data": [{"id": "800", "name": "Getting Started"}]})
        if method == "GET" and "/translations/" in path:
            parts = path.split("/articles/")[1].split("/")
            aid, locale = parts[0], parts[-1]
            tr = self.translations.get((aid, locale))
            if tr is not None:
                return httpx.Response(200, json=dict(tr))
            return httpx.Response(404, json={"error": "no such translation"})
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
            self.translations[(aid, locale)] = {
                **self.translations.get((aid, locale), {"id": aid}),
                **body,
            }
            return httpx.Response(200, json={"id": aid, **body})
        if method == "GET" and path.startswith(f"{pfx}/articles/") and path.count("/") == 4:
            aid = path.rsplit("/", 1)[-1]
            art = self.articles.get(aid)
            if art is not None:
                return httpx.Response(200, json=dict(art))
            return httpx.Response(404, json={"error": "no such article"})
        if method == "GET" and path == f"{pfx}/articles":
            rows = [{"id": aid} for aid in self.articles]
            return httpx.Response(200, json={"data": rows})
        return httpx.Response(404, json={"error": "unexpected", "path": path, "method": method})


def _zo_article(
    aid,
    title="Reset password",
    answer="<h1>Reset</h1><p>Click the reset link to reset.</p>",
    modifiedTime="2026-07-05T12:00:00.000Z",
):
    return {
        "id": aid,
        "title": title,
        "status": "Published",
        "modifiedTime": modifiedTime,
        "answer": answer,
        "authorId": "4000000000059",
    }


def _zo_conn(name, extra_cfg=None, secret=None):
    cfg = {
        "api_domain": _ZO_HOST,
        "org_id": _ZO_ORG,
        "source_locale": "en",
        "target_locales": "de, fr",
    }
    cfg.update(extra_cfg or {})
    sec = {"access_token": "1000.tok-abc"}
    sec.update(secret or {})
    return cstore.create("zoho", name, cfg, sec, actor="admin")


# ===========================================================================
# Capture helpers
# ===========================================================================
_HASH_KEYS = ("zendesk_hashes", "zendesk_watermark", "d360_hashes", "fd_hashes", "zoho_hashes")


def _capture(cid, ok, msg, result):
    """Freeze the run's public outputs + the connection's persisted state."""
    with Session(engine) as s:
        rows = list(
            s.exec(select(SyncLog).where(SyncLog.connection_id == cid).order_by(SyncLog.id)).all()
        )
        synclog = [
            {
                "direction": r.direction,
                "ok": r.ok,
                "count": r.count,
                "message": r.message,
                "actor": r.actor,
            }
            for r in rows
        ]
        conn = s.get(Connection, cid)
        cfg = dict(conn.config or {})
    state = {k: cfg[k] for k in _HASH_KEYS if k in cfg}
    return {"ok": ok, "msg": msg, "result": result, "synclog": synclog, "config": state}


class _CeilingPatch:
    """Temporarily set config.MAX_WRITES_PER_RUN (WriteBudget reads it at build)."""

    def __init__(self, n):
        self.n = n
        self._old = getattr(_cfgmod, "MAX_WRITES_PER_RUN", None)

    def __enter__(self):
        _cfgmod.MAX_WRITES_PER_RUN = self.n

    def __exit__(self, *a):
        _cfgmod.MAX_WRITES_PER_RUN = self._old


# ===========================================================================
# Scenarios: each returns (name, record). Deterministic, one fresh conn each.
# ===========================================================================
def _scn_zendesk():
    out = {}

    # ZD-A: create 2 locales, anti-loop labels, draft default.
    fz = FakeZendesk(articles=[_zd_article("100", labels=["needs-translation", "keep"])])
    conn = _zd_conn(
        "PZD create+labels",
        {
            "label_names": "needs-translation",
            "translated_label": "translated",
            "remove_trigger_label": "1",
        },
    )
    ok, msg, res = cstore.zendesk_translate(conn.id, actor="auto", transport=fz.transport())
    out["zd_create_labels"] = _capture(conn.id, ok, msg, res)

    # ZD-B: partial rejection (only de enabled), then a no-op suppression second run.
    fz1 = FakeZendesk(enabled_locales=("en-us", "de"), articles=[_zd_article("100")])
    conn2 = _zd_conn("PZD reject", {"target_locales": "de, fr, ja"})
    cstore.zendesk_translate(conn2.id, actor="auto", transport=fz1.transport())
    fz1b = FakeZendesk(enabled_locales=("en-us", "de"), articles=[_zd_article("100")])
    fz1b.add_translation("100", "de")
    ok, msg, res = cstore.zendesk_translate(conn2.id, actor="auto", transport=fz1b.transport())
    out["zd_reject_then_suppress"] = _capture(conn2.id, ok, msg, res)

    # ZD-C: ceiling + FINDING-1 watermark clamp. 3 articles x 2 locales, ceiling=3.
    arts = [
        _zd_article("10", edited_at="2026-07-01T00:00:00Z"),
        _zd_article("11", edited_at="2026-07-02T00:00:00Z"),
        _zd_article("12", edited_at="2026-07-03T00:00:00Z"),
    ]
    fz2 = FakeZendesk(articles=arts)
    conn3 = _zd_conn("PZD ceiling")
    with _CeilingPatch(3):
        ok, msg, res = cstore.zendesk_translate(
            conn3.id, actor="scheduler", source="scheduled", transport=fz2.transport()
        )
    out["zd_ceiling_clamp"] = _capture(conn3.id, ok, msg, res)

    # ZD-D: per-pair write failure keeps the watermark behind that article.
    fz3 = FakeZendesk(
        articles=[_zd_article("20", edited_at="2026-07-04T00:00:00Z")], fail_write_locales={"fr"}
    )
    conn4 = _zd_conn("PZD writefail")
    ok, msg, res = cstore.zendesk_translate(conn4.id, actor="auto", transport=fz3.transport())
    out["zd_write_failure"] = _capture(conn4.id, ok, msg, res)

    # ZD-E: incremental-export union (watermark set by run 1, run 2 unions a changed art).
    fz4 = FakeZendesk(articles=[_zd_article("300", edited_at="2026-07-01T00:00:00Z")])
    conn5 = _zd_conn("PZD incremental")
    cstore.zendesk_translate(conn5.id, actor="auto", transport=fz4.transport())
    fz5 = FakeZendesk(articles=[])
    fz5.incremental_pages = [
        ([_zd_article("301", body="<p>new body</p>", edited_at="2026-07-05T00:00:00Z")], 2000, None)
    ]
    ok, msg, res = cstore.zendesk_translate(conn5.id, actor="auto", transport=fz5.transport())
    out["zd_incremental_union"] = _capture(conn5.id, ok, msg, res)
    return out


def _scn_document360():
    out = {}

    # D360-A: markdown + html create (editor branching, shielding) + publish toggle.
    fz = FakeD360(articles=[_d360_md_article("100"), _d360_html_article("101")])
    conn = _d360_conn("PD360 create", {"publish": "1"})
    ok, msg, res = cstore.document360_translate(conn.id, actor="auto", transport=fz.transport())
    out["d360_create_publish"] = _capture(conn.id, ok, msg, res)

    # D360-B: empty-body skip + stale-flag re-enqueue + suppression.
    fz1 = FakeD360(
        articles=[_d360_md_article("100"), _d360_md_article("200", content="")]
    )  # empty body
    conn2 = _d360_conn("PD360 stale")
    cstore.document360_translate(conn2.id, actor="auto", transport=fz1.transport())
    # Second run: 100/de flagged needs-update (option 1) -> re-enqueue; 100/fr translated -> suppress.  # noqa: E501
    fz2 = FakeD360(articles=[_d360_md_article("100"), _d360_md_article("200", content="")])
    fz2.add_version("100", "de", translation_option=1)
    fz2.add_version("100", "fr", translation_option=2)
    ok, msg, res = cstore.document360_translate(conn2.id, actor="auto", transport=fz2.transport())
    out["d360_empty_stale_suppress"] = _capture(conn2.id, ok, msg, res)

    # D360-C: ceiling (2 articles x 2 locales, ceiling=2).
    fz3 = FakeD360(articles=[_d360_md_article("100"), _d360_html_article("101")])
    conn3 = _d360_conn("PD360 ceiling")
    with _CeilingPatch(2):
        ok, msg, res = cstore.document360_translate(
            conn3.id, actor="auto", transport=fz3.transport()
        )
    out["d360_ceiling"] = _capture(conn3.id, ok, msg, res)
    return out


def _scn_freshdesk():
    out = {}

    # FD-A: primary drop + create + unpublished skip + empty skip.
    fz = FakeFreshdesk(
        articles=[
            _fd_article("100"),
            _fd_article("101", status=1),  # unpublished master -> skipped
            _fd_article("102", title="", description=""),  # empty -> skipped
        ]
    )
    conn = _fd_conn("PFD create", {"target_locales": "fr, en"})  # en == primary -> rejected
    ok, msg, res = cstore.freshdesk_translate(conn.id, actor="auto", transport=fz.transport())
    out["fd_primary_unpub_empty"] = _capture(conn.id, ok, msg, res)

    # FD-B: plan-gate failure (special ok-semantics path).
    fz1 = FakeFreshdesk(articles=[_fd_article("100")], plan_gate=True)
    conn2 = _fd_conn("PFD plangate")
    ok, msg, res = cstore.freshdesk_translate(
        conn2.id, actor="webhook", source="webhook", transport=fz1.transport()
    )
    out["fd_plan_gate"] = _capture(conn2.id, ok, msg, res)

    # FD-C: suppression second run.
    fz2 = FakeFreshdesk(articles=[_fd_article("100")])
    conn3 = _fd_conn("PFD suppress")
    cstore.freshdesk_translate(conn3.id, actor="auto", transport=fz2.transport())
    fz3 = FakeFreshdesk(articles=[_fd_article("100")])
    fz3.add_translation("100", "fr")
    fz3.add_translation("100", "de")
    ok, msg, res = cstore.freshdesk_translate(conn3.id, actor="auto", transport=fz3.transport())
    out["fd_suppress"] = _capture(conn3.id, ok, msg, res)

    # FD-D: ceiling.
    fz4 = FakeFreshdesk(articles=[_fd_article("100"), _fd_article("101")])
    conn4 = _fd_conn("PFD ceiling")
    with _CeilingPatch(2):
        ok, msg, res = cstore.freshdesk_translate(conn4.id, actor="auto", transport=fz4.transport())
    out["fd_ceiling"] = _capture(conn4.id, ok, msg, res)
    return out


def _scn_zoho():
    out = {}

    # ZO-A: create (two articles).
    fz = FakeZoho(
        articles=[
            _zo_article("100"),
            _zo_article("101", answer="<p>content body</p>"),
        ]
    )
    conn = _zo_conn("PZO create")
    ok, msg, res = cstore.zoho_translate(conn.id, actor="auto", transport=fz.transport())
    out["zo_create_bodykey"] = _capture(conn.id, ok, msg, res)

    # ZO-B: empty-body skip + translationState stale re-enqueue + suppression.
    fz1 = FakeZoho(articles=[_zo_article("100"), _zo_article("200", answer="")])
    conn2 = _zo_conn("PZO stale")
    cstore.zoho_translate(conn2.id, actor="auto", transport=fz1.transport())
    fz2 = FakeZoho(articles=[_zo_article("100"), _zo_article("200", answer="")])
    fz2.add_translation("100", "de", translationState="Outdated")  # stale -> re-enqueue
    fz2.add_translation("100", "fr", translationState="Translated")  # fresh -> suppress
    ok, msg, res = cstore.zoho_translate(conn2.id, actor="auto", transport=fz2.transport())
    out["zo_empty_stale_suppress"] = _capture(conn2.id, ok, msg, res)

    # ZO-C: ceiling.
    fz3 = FakeZoho(articles=[_zo_article("100"), _zo_article("101")])
    conn3 = _zo_conn("PZO ceiling")
    with _CeilingPatch(2):
        ok, msg, res = cstore.zoho_translate(conn3.id, actor="auto", transport=fz3.transport())
    out["zo_ceiling"] = _capture(conn3.id, ok, msg, res)
    return out


def run_all() -> dict:
    """Run every scenario against whatever store.py is imported; return the records."""
    init_db()
    records = {}
    records.update({f"zendesk::{k}": v for k, v in _scn_zendesk().items()})
    records.update({f"document360::{k}": v for k, v in _scn_document360().items()})
    records.update({f"freshdesk::{k}": v for k, v in _scn_freshdesk().items()})
    records.update({f"zoho::{k}": v for k, v in _scn_zoho().items()})
    return records
