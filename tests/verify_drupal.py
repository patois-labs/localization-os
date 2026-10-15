"""Drupal connector + connector-framework seams, fully offline via httpx.MockTransport.

The fake is a Drupal site with core JSON:API and (optionally) the contrib JSON:API Multilingual
module. It models the parts of the platform the connector defect taxonomy attacks, so a payload
assertion cannot pass vacuously (taxonomy G):

  * per-language translations of ONE entity, a translatable-vs-shared field split, and the
    "new translation is seeded with the SOURCE values of every field not sent" behaviour;
  * a strict validator: a non-translatable field, an unknown attribute, the read-only
    `processed` key or a bad text format is a 422, as the module answers;
  * WITHOUT the module, `?langCode=` is silently ignored and a PATCH lands on the DEFAULT
    translation (the data-loss hazard the connector's probe exists to prevent);
  * a 50-row page cap, a 404 for an absent translation, a 403 for an unpublished one the API
    user may not view, content moderation that hides a new translation from the default
    revision, 429 / 5xx injection, three auth schemes (Simple OAuth client credentials, Basic,
    Bearer), and a hostile `links.next` pointing at another host.

Covers: A scope and path escape; B held-back never overwrites a stored translation (create AND
update, title AND body, formatted field whole-or-nothing); C the existence probe reads what
the write writes (second run is a no-op); D field selection both directions, reported; E the
shared cap round-robins; F a no-op records no hash; G the fake itself; H WriteBudget, egress
guard, parity surfaces, token never logged, error bodies never echoed, backoff via
base._request.

ASCII-only prints; final line is 'verify_drupal OK'.
"""

import copy
import json
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_drupal_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import Connection, Segment, SegStatus
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.drupal import DrupalConnector

# Offline fixtures only; none of these is a real credential.
BEARER = {"access_token": "bearer-fixture"}  # pregate: allow-secret offline fixture
OAUTH = {  # pregate: allow-secret offline fixture
    "client_id": "cid-fixture",
    "client_secret": "csec-fixture",
}
BASIC = {"password": "pw-fixture"}  # pregate: allow-secret offline fixture
BASIC_USER = "locos-bot"

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"

ART = "node--article"
PAGE = "node--page"
TERM = "taxonomy_term--tags"

# Translatable attributes per resource type (the site's field configuration). Everything else
# on the entity is SHARED by all languages.
TRANSLATABLE = {
    ART: {"title", "body", "field_tagline"},
    PAGE: {"title", "body"},
    TERM: {"name", "description"},
}
_FORMATS = {"basic_html", "full_html", "plain_text"}

BODY_HTML = '<p>Welcome to <a href="/about">our campus</a>.</p><p>Apply now.</p>'


def _body(html=BODY_HTML, fmt="full_html", summary="A short welcome"):
    return {"value": html, "format": fmt, "summary": summary, "processed": html}


def _err(status, title, detail=""):
    return httpx.Response(
        status,
        headers={"Content-Type": "application/vnd.api+json"},
        json={"errors": [{"status": str(status), "title": title, "detail": detail}]},
    )


# ============================================================================
# Fake Drupal site.
# ============================================================================
class FakeDrupal:
    def __init__(
        self,
        *,
        multilingual=True,
        languages=("en", "fr", "de"),
        site_lang="en",
        page_cap=50,
        lang_list_ok=True,
        can_see_unpublished=True,
        hide_new=False,
    ):
        self.multilingual = multilingual
        self.languages = list(languages)
        self.site_lang = site_lang
        self.page_cap = page_cap
        self.lang_list_ok = lang_list_ok
        self.can_see_unpublished = can_see_unpublished
        self.hide_new = hide_new
        self.ents: dict = {}  # rtype -> {uuid: {"default", "shared", "tr": {lang: attrs}}}
        self.hidden: set = set()  # (uuid, lang) hidden from the default revision
        self.requests: list = []  # (method, path, params)
        self.writes: list = []  # (method, rtype, uuid, lang, attrs) -- every landed write
        self.throttle = 0
        self.fail_next: dict = {}  # method -> [status, ...]
        self.fail_body = "<html><title>Oops</title>SECRET-MARKER-9</html>"
        self.evil_next = False
        self.runaway = False
        self.valid_bearer = {BEARER["access_token"]}
        self.valid_basic = (BASIC_USER, BASIC["password"])
        self.oauth_client = (OAUTH["client_id"], OAUTH["client_secret"])
        self.tokens: set = set()
        self.minted = 0
        self.auth_required = True
        self._seq = 0
        # --- adversarial knobs (review round) ---
        self.before_write = None  # callable run just before a POST/PATCH member write is handled
        self.mutate_get = None  # callable(json_dict) -> json_dict on a 200 member GET
        self.get_age = None  # Age header on a 200 member GET (a shared cache answered)
        self.patch_204 = False  # PATCH answers 204 with no body
        self.force_publish = False  # a created translation comes back PUBLISHED
        self.req_headers: list = []  # (method, headers dict) of every request

    # ---- seeding -----------------------------------------------------------
    def add(self, rtype, uuid, default_lang, attrs, shared=None, changed=1):
        """attrs = the DEFAULT translation's translatable attributes."""
        a = dict(attrs)
        a.setdefault("status", True)
        a["changed"] = changed
        self.ents.setdefault(rtype, {})[uuid] = {
            "default": default_lang,
            "shared": dict(shared or {}),
            "tr": {default_lang: a},
        }

    def add_translation(self, rtype, uuid, lang, attrs, status=False):
        a = dict(attrs)
        a["status"] = status
        a["changed"] = 5
        self.ents[rtype][uuid]["tr"][lang] = a

    def tr(self, rtype, uuid, lang):
        return self.ents.get(rtype, {}).get(uuid, {}).get("tr", {}).get(lang)

    def transport(self):
        return httpx.MockTransport(self._handle)

    def nwrites(self):
        return len(self.writes)

    def revoke_tokens(self):
        self.tokens.clear()

    # ---- views ---------------------------------------------------------------
    def _view(self, rtype, uuid, lang):
        ent = self.ents[rtype][uuid]
        tr = ent["tr"][lang]
        attrs = dict(ent["shared"])
        attrs.update(copy.deepcopy(tr))
        attrs["langcode"] = lang
        attrs["default_langcode"] = lang == ent["default"]
        attrs["drupal_internal__nid"] = abs(hash(uuid)) % 1000
        return {"type": rtype, "id": uuid, "attributes": attrs, "relationships": {}}

    def _meta_404(self, ent):
        return httpx.Response(
            404,
            headers={"Content-Type": "application/vnd.api+json"},
            json={
                "errors": [
                    {
                        "status": "404",
                        "title": "Not Found",
                        "detail": "No translation in that language.",
                        "meta": {"availableTranslations": sorted(ent["tr"])},
                    }
                ]
            },
        )

    # ---- auth ----------------------------------------------------------------
    def _authed(self, req) -> bool:
        if not self.auth_required:
            return True
        h = req.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            tok = h[7:]
            return tok in self.valid_bearer or tok in self.tokens
        if h.startswith("Basic "):
            import base64

            try:
                user, _s, pw = base64.b64decode(h[6:]).decode().partition(":")
            except Exception:
                return False
            return (user, pw) == self.valid_basic
        return False

    def _token(self, req):
        form = dict(x.split("=", 1) for x in req.content.decode().split("&") if "=" in x)
        if form.get("grant_type") != "client_credentials":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if (form.get("client_id"), form.get("client_secret")) != self.oauth_client:
            return httpx.Response(
                401, json={"error": "invalid_client", "echo": req.content.decode()}
            )
        self.minted += 1
        tok = f"oauth-token-{self.minted}"
        self.tokens.add(tok)
        return httpx.Response(
            200, json={"token_type": "Bearer", "expires_in": 300, "access_token": tok}
        )

    # ---- request handling ------------------------------------------------------
    def _handle(self, req):
        self.req_headers.append((req.method, dict(req.headers)))
        is_member = (
            req.url.path.startswith("/jsonapi/")
            and len([p for p in req.url.path[len("/jsonapi/") :].split("/") if p]) == 3
        )
        if is_member and req.method in ("POST", "PATCH") and self.before_write:
            hook, self.before_write = self.before_write, None
            hook()
        resp = self._handle_inner(req)
        if is_member and req.method == "GET" and resp.status_code == 200:
            if self.mutate_get:
                resp = httpx.Response(200, json=self.mutate_get(json.loads(resp.content)))
            if self.get_age:
                resp.headers["Age"] = str(self.get_age)
        if is_member and req.method == "PATCH" and resp.status_code == 200 and self.patch_204:
            return httpx.Response(204)
        return resp

    def _handle_inner(self, req):
        params = dict(req.url.params)
        self.requests.append((req.method, req.url.path, params))
        if self.throttle > 0:
            self.throttle -= 1
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        queued = self.fail_next.get(req.method)
        if queued:
            code = queued.pop(0)
            return httpx.Response(code, headers={"Content-Type": "text/html"}, text=self.fail_body)
        path = req.url.path
        if path == "/oauth/token" and req.method == "POST":
            return self._token(req)
        if not path.startswith("/jsonapi/"):
            return httpx.Response(404, text="not jsonapi")
        if not self._authed(req):
            return _err(401, "Unauthorized", "Authentication required.")
        parts = [p for p in path[len("/jsonapi/") :].split("/") if p]
        if parts[:2] == ["configurable_language", "configurable_language"]:
            if not self.lang_list_ok:
                return _err(403, "Forbidden", "The 'administer languages' permission is required.")
            data = [
                {
                    "type": "configurable_language--configurable_language",
                    "id": f"aaaaaaaa-0000-4000-8000-{i:012d}",
                    "attributes": {"drupal_internal__id": lg, "locked": False},
                }
                for i, lg in enumerate(self.languages)
            ] + [
                {
                    "type": "configurable_language--configurable_language",
                    "id": "bbbbbbbb-0000-4000-8000-000000000000",
                    "attributes": {"drupal_internal__id": "und", "locked": True},
                }
            ]
            return httpx.Response(200, json={"data": data})
        if len(parts) not in (2, 3):
            return _err(404, "Not Found")
        rtype = f"{parts[0]}--{parts[1]}"
        if rtype not in self.ents:
            return _err(404, "Not Found", f"Unknown resource type {rtype}.")
        if len(parts) == 2:
            return self._collection(req, rtype, params)
        return self._member(req, rtype, parts[2], params)

    def _collection(self, req, rtype, params):
        if req.method != "GET":
            return _err(405, "Method Not Allowed")
        lang = params.get("langCode")
        rows = []
        order = sorted(
            self.ents[rtype],
            key=lambda u: -max(a.get("changed", 0) for a in self.ents[rtype][u]["tr"].values()),
        )
        for uuid in order:
            ent = self.ents[rtype][uuid]
            if self.multilingual and lang:
                if lang not in ent["tr"] or (uuid, lang) in self.hidden:
                    continue
                rows.append(self._view(rtype, uuid, lang))
            else:
                shown = self.site_lang if self.site_lang in ent["tr"] else ent["default"]
                rows.append(self._view(rtype, uuid, shown))
        limit = min(int(params.get("page[limit]", "50")), self.page_cap)
        offset = 0 if self.runaway else int(params.get("page[offset]", "0"))
        page = rows[offset : offset + limit]
        links = {}
        if self.runaway or offset + limit < len(rows):
            host = "https://evil.example" if self.evil_next else "http://drupal.test"
            links["next"] = {"href": f"{host}/jsonapi/x?page[offset]={offset + limit}"}
        return httpx.Response(200, json={"data": page, "links": links})

    def _validate(self, rtype, attrs, *, create):
        ok = TRANSLATABLE.get(rtype, set()) | {"langcode", "status"}
        for k, v in attrs.items():
            if k == "processed":
                return _err(422, "Unprocessable Entity", "processed is read-only")
            if k not in ok:
                return _err(
                    422,
                    "Unprocessable Entity",
                    f"Field {k} is not translatable or does not exist.",
                )
            if isinstance(v, dict) and "value" in v:
                if set(v) - {"value", "format", "summary"}:
                    return _err(422, "Unprocessable Entity", f"Bad keys in {k}.")
                if v.get("format") not in _FORMATS:
                    return _err(422, "Unprocessable Entity", f"Unknown text format on {k}.")
        return None

    def _member(self, req, rtype, uuid, params):
        ent = self.ents[rtype].get(uuid)
        if ent is None:
            return _err(404, "Not Found", "No such entity.")
        lang = params.get("langCode")
        if req.method == "GET":
            if self.multilingual and lang:
                tr = ent["tr"].get(lang)
                if tr is None or (uuid, lang) in self.hidden:
                    return self._meta_404(ent)
                if tr.get("status") is False and not self.can_see_unpublished:
                    return _err(403, "Forbidden", "The 'view unpublished' permission is required.")
                return httpx.Response(200, json={"data": self._view(rtype, uuid, lang)})
            shown = self.site_lang if self.site_lang in ent["tr"] else ent["default"]
            return httpx.Response(200, json={"data": self._view(rtype, uuid, shown)})
        if req.method == "DELETE":
            if not self.multilingual or not lang:
                return _err(405, "Method Not Allowed")
            if lang == ent["default"]:
                return _err(400, "Bad Request", "The default translation cannot be deleted alone.")
            if lang not in ent["tr"]:
                return self._meta_404(ent)
            del ent["tr"][lang]
            self.hidden.discard((uuid, lang))
            self.writes.append(("DELETE", rtype, uuid, lang, {}))
            return httpx.Response(204)
        try:
            body = json.loads(req.content.decode() or "{}")
        except Exception:
            return _err(400, "Bad Request")
        data = body.get("data") or {}
        attrs = data.get("attributes") or {}
        if data.get("type") != rtype or data.get("id") != uuid:
            return _err(409, "Conflict", "type/id mismatch")
        if req.headers.get("Content-Type") != "application/vnd.api+json":
            return _err(415, "Unsupported Media Type")
        if req.method == "POST":
            if not self.multilingual or not lang:
                return _err(405, "Method Not Allowed")
            if lang not in self.languages:
                return _err(400, "Bad Request", f"Unknown language {lang}.")
            if lang in ent["tr"]:
                return _err(409, "Conflict", "That translation already exists.")
            if attrs.get("langcode") != lang:
                return _err(400, "Bad Request", "langcode attribute must match langCode.")
            bad = self._validate(rtype, attrs, create=True)
            if bad:
                return bad
            seeded = {
                k: copy.deepcopy(v)
                for k, v in ent["tr"][ent["default"]].items()
                if k in TRANSLATABLE.get(rtype, set()) | {"status"}
            }
            for k, v in attrs.items():
                if k != "langcode":
                    seeded[k] = copy.deepcopy(v)
            seeded["changed"] = 9
            if self.force_publish:
                seeded["status"] = True
            ent["tr"][lang] = seeded
            if self.hide_new:
                self.hidden.add((uuid, lang))
            self.writes.append(("POST", rtype, uuid, lang, copy.deepcopy(attrs)))
            return httpx.Response(201, json={"data": self._view(rtype, uuid, lang)})
        if req.method == "PATCH":
            bad = self._validate(rtype, attrs, create=False)
            if bad:
                return bad
            if not self.multilingual or not lang:
                # core JSON:API ignores langCode: the DEFAULT translation is patched. Setting
                # the default translation's langcode to a language the entity already has
                # throws in ContentEntityBase::onChange before save() (a 500, nothing saved);
                # to a language it does not have, core RELABELS the source and saves.
                target = ent["default"]
                new = attrs.get("langcode")
                if new and new != target:
                    if new in ent["tr"]:
                        return _err(500, "Internal Server Error", "A translation already exists.")
                    ent["tr"][new] = ent["tr"].pop(target)
                    ent["default"] = target = new
                for k, v in attrs.items():
                    if k != "langcode":
                        ent["tr"][target][k] = copy.deepcopy(v)
                self.writes.append(("PATCH", rtype, uuid, target, copy.deepcopy(attrs)))
                return httpx.Response(200, json={"data": self._view(rtype, uuid, target)})
            if lang not in ent["tr"]:
                return self._meta_404(ent)
            if "langcode" in attrs and attrs["langcode"] != lang:
                return _err(422, "Unprocessable Entity", "Translation resource language mismatch.")
            for k, v in attrs.items():  # partial merge: only the named attributes change
                ent["tr"][lang][k] = copy.deepcopy(v)
            self.writes.append(("PATCH", rtype, uuid, lang, copy.deepcopy(attrs)))
            return httpx.Response(200, json={"data": self._view(rtype, uuid, lang)})
        return _err(405, "Method Not Allowed")


# ============================================================================
# helpers
# ============================================================================
def _conn(name, cfg_extra=None, secret=None):
    cfg = {
        "base_url": "http://drupal.test",
        "resource_types": ART,
        "source_locale": "en",
        "target_locales": "fr, de",
        "exclude_fields": "field_ref_code",
    }
    cfg.update(cfg_extra or {})
    return cstore.create("drupal", name, cfg, secret or BEARER, actor="admin")


def _dc(cfg, transport, secret=None):
    base = {
        "base_url": "http://drupal.test",
        "resource_types": ART,
        "source_locale": "en",
        "target_locales": "fr, de",
    }
    base.update(cfg or {})
    return DrupalConnector(base, secret or BEARER, transport=transport)


def _hashes(cid):
    return dict((cstore.get(cid).config or {}).get("drupal_hashes") or {})


def _seed(fd, uuid=U1, **over):
    attrs = {
        "title": "Hello Campus",
        "body": _body(),
        "field_tagline": "Learn anywhere",
    }
    attrs.update(over)
    shared = {"field_ref_code": "ABC-1", "field_slug": "hello-campus"}
    fd.add(ART, uuid, "en", attrs, shared=shared)


def _hold(mod, pred):
    """A pretranslate_document wrapper flagging the segments `pred(order_index)` NEEDS_REVIEW."""
    orig = mod.pretranslate_document

    def patched(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if pred(sg.order_index):
                sg.status = SegStatus.NEEDS_REVIEW
                s.add(sg)
        s.commit()
        return out

    return orig, patched


def _run_held(cid, fd, pred, **kw):
    import app.services.translate as T

    orig, patched = _hold(T, pred)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        return cstore.drupal_translate(cid, actor="auto", transport=fd.transport(), **kw)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)


def _gets_only(fd):
    return all(m == "GET" for m, _p, _q in fd.requests)


class _LogCatcher(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append(str(record.msg))


def _leaf_paths(parser, prefix="") -> set:
    import argparse

    out: set = set()
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        return {prefix} if prefix else set()
    for sp in subs:
        for name, subparser in sp.choices.items():
            out |= _leaf_paths(subparser, f"{prefix} {name}".strip())
    return out


# ============================================================================
# the tests
# ============================================================================
def _test_connector_basics():
    assert DrupalConnector.capability.topology == "T2"
    fd = FakeDrupal()
    _seed(fd)
    dc = _dc({}, fd.transport())
    ok, msg = dc.test()
    assert ok and "JSON:API Multilingual detected" in msg, msg
    assert dc.enabled_locales() == ["en", "fr", "de"], dc.enabled_locales()
    assert dc._resolve_locale("FR") == "fr"
    # a resource type that does not exist answers a clear failure
    ok, msg = _dc({"resource_types": "node--nothing"}, fd.transport()).test()
    assert not ok and "not found" in msg, msg
    # an unreadable language list falls back to the configured languages and says so
    fd2 = FakeDrupal(lang_list_ok=False)
    _seed(fd2)
    dc2 = _dc({}, fd2.transport())
    ok, msg = dc2.test()
    assert ok and "not readable" in msg, msg
    assert set(dc2.enabled_locales()) == {"en", "fr", "de"}
    # a missing module fails the test button loudly
    fd3 = FakeDrupal(multilingual=False)
    _seed(fd3)
    ok, msg = _dc({}, fd3.transport()).test()
    assert not ok and "JSON:API Multilingual" in msg, msg
    # no sample content yet: connected, but says the module check is deferred
    fd4 = FakeDrupal()
    fd4.ents[ART] = {}
    ok, msg = _dc({}, fd4.transport()).test()
    assert ok and "Could not verify" in msg, msg
    print("  basics: T2, test() (module detected / missing / unverifiable), language fallback")


def _test_auth():
    cap = _LogCatcher()
    lg = logging.getLogger()
    lg.addHandler(cap)
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        # Simple OAuth client credentials: one mint, reused across calls
        fd = FakeDrupal()
        _seed(fd)
        dc = _dc({}, fd.transport(), OAUTH)
        ok, msg = dc.test()
        assert ok, msg
        assert dc.discover_articles()
        assert fd.minted == 1, ("the token is minted once and cached", fd.minted)
        sent = [r for r in fd.requests if r[1] == "/oauth/token"]
        assert len(sent) == 1
        # a revoked token mid-run: one 401, one re-mint, the call succeeds
        fd.revoke_tokens()
        assert _dc({}, fd.transport(), OAUTH).discover_articles()
        assert fd.minted == 2, ("exactly one re-mint after a 401", fd.minted)
        # wrong client secret: a short error code, no echo of the form (the fake echoes it)
        bad = {"client_id": OAUTH["client_id"], "client_secret": "wrong-secret-fixture"}
        try:
            _dc({}, fd.transport(), bad).discover_articles()
        except ConnectorError as e:
            assert "refused the OAuth" in str(e) and "wrong-secret-fixture" not in str(e), str(e)
        else:
            raise AssertionError("a wrong OAuth secret must fail")
        # Basic and Bearer
        fb = FakeDrupal()
        _seed(fb)
        assert _dc({"username": BASIC_USER}, fb.transport(), BASIC).discover_articles()
        assert _dc({}, fb.transport(), BEARER).discover_articles()
        # partial or missing credentials are refused before any request
        for cfg, sec in [
            ({}, {"client_id": "only-id"}),
            ({"username": "u"}, {"password": ""}),
            ({}, {"access_token": " "}),
        ]:
            f0 = FakeDrupal()
            _seed(f0)
            try:
                _dc(cfg, f0.transport(), sec).discover_articles()
            except ConnectorError:
                assert not f0.requests, "no request goes out without usable credentials"
            else:
                raise AssertionError(("incomplete credentials must be refused", cfg, sec))
        # a wrong Bearer token: 401 -> clear failure, token not echoed
        try:
            _dc({}, fb.transport(), {"access_token": "nope-fixture"}).discover_articles()
        except ConnectorError as e:
            assert "401" in str(e) and "nope-fixture" not in str(e), str(e)
        else:
            raise AssertionError("a wrong token must fail")
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old_level)
    blob = "\n".join(cap.lines)
    for secret in (
        "csec-fixture",
        "bearer-fixture",
        "pw-fixture",
        "oauth-token-1",
        "oauth-token-2",
    ):
        assert secret not in blob, ("a credential reached the logs", secret)
    print(
        "  auth: OAuth mint once + one re-mint on 401, Basic, Bearer, partial creds refused, "
        "no log leak"
    )


def _test_discovery():
    # D/E: pagination past the 50 cap, round-robin across types, other-language entities
    fd = FakeDrupal()
    for i in range(120):
        u = f"00000000-0000-4000-8000-{i:012d}"
        fd.add(ART, u, "en", {"title": f"T{i}"}, changed=i + 1)
    got = _dc({"max_entries": "500"}, fd.transport()).discover_articles()
    assert len(got) == 120 and len({e["uuid"] for e in got}) == 120, len(got)
    assert max(int(p.get("page[limit]", 0)) for _m, _p, p in fd.requests) <= 50
    print("  pagination: 120 entities across 3 pages of 50 (offset walk)")

    # a hostile links.next to another host is never followed
    fe = FakeDrupal(
        page_cap=10,
    )
    fe.evil_next = True
    for i in range(25):
        fe.add(ART, f"00000000-0000-4000-8000-{i:012d}", "en", {"title": f"T{i}"}, changed=i + 1)
    got = _dc({"max_entries": "100"}, fe.transport()).discover_articles()
    assert len(got) == 25, len(got)
    assert all(p.startswith("/jsonapi/") for _m, p, _q in fe.requests)
    print("  SSRF: a links.next pointing at another host is ignored; pages walked by offset")

    # a server that always says 'next' with the same page cannot hold the run
    fr = FakeDrupal()
    fr.runaway = True
    for i in range(5):
        fr.add(ART, f"00000000-0000-4000-8000-{i:012d}", "en", {"title": f"T{i}"})
    got = _dc({"max_entries": "100"}, fr.transport()).discover_articles()
    assert len(got) == 5 and len(fr.requests) <= 8, (len(got), len(fr.requests))
    print("  runaway 'next' links terminate (no new rows -> stop)")

    # E: three types, shared cap 50, round-robin (assert DISTRIBUTION)
    fm = FakeDrupal()
    for rt in (ART, PAGE, TERM):
        for i in range(40):
            attr = {"name": f"n{i}"} if rt == TERM else {"title": f"t{i}"}
            fm.add(rt, f"{abs(hash(rt)) % 10**4:04d}0000-0000-4000-8000-{i:012d}", "en", attr)
    got = _dc(
        {"resource_types": f"{ART}, {PAGE}, {TERM}", "max_entries": "50"}, fm.transport()
    ).discover_articles()
    counts: dict = {}
    for e in got:
        counts[e["_type"]] = counts.get(e["_type"], 0) + 1
    assert len(got) == 50 and set(counts) == {ART, PAGE, TERM} and min(counts.values()) >= 16, (
        counts
    )
    print(f"  E: shared cap round-robins across types, none starved: {counts}")

    # an entity whose ORIGINAL language is not the source is skipped and counted
    fo = FakeDrupal()
    _seed(fo)
    fo.add(ART, U2, "fr", {"title": "Bonjour"})
    fo.add_translation(ART, U2, "en", {"title": "Hello from fr"})
    dc = _dc({}, fo.transport())
    got = dc.discover_articles()
    assert [e["uuid"] for e in got] == [U1] and dc.other_language_skipped == 1, (got, dc)
    print("  entities not authored in the source language are skipped and counted")

    # an oversize record is dropped with a reason (never buffered)
    fz = FakeDrupal()
    _seed(fz)
    fz.add(ART, U3, "en", {"title": "Big", "body": _body("<p>" + "x" * 5000 + "</p>")})
    saved = app_config.MAX_SEGMENT_CHARS
    app_config.MAX_SEGMENT_CHARS = 100
    try:
        dc = _dc({}, fz.transport())
        got = dc.discover_articles()
    finally:
        app_config.MAX_SEGMENT_CHARS = saved
    assert [e["uuid"] for e in got] == [U1] and dc.fetch_notes, (got, dc.fetch_notes)
    print("  #579: an over-limit record is dropped with a fetch note")


def _test_scope_escape_A():
    fd = FakeDrupal()
    _seed(fd)
    c = _conn("A scope", {"resource_types": f"{ART}"})
    hostile = [
        f"node--secret:{U1}",
        f"../../user/user:{U1}",
        f"{ART}:../../user/user",
        f"{ART}:{U1}?include=uid",
        f"{ART}:{U1}%2f..%2fuser",
        f"{ART}:{U1}#x",
        f"{ART}:{U1}\r\nX-Evil: 1",
        f"{ART}:{U1}\x00",
        "node--art:" + U1,  # a PREFIX of a configured type
        f"{ART}x:{U1}",
        f"user--user:{U1}",
        f"{ART}:",
        f":{U1}",
        f"{ART}:١٢",
        "not-a-uuid",
        f"{ART}:{U1.upper()[:-1]}",
    ]
    for bad in hostile:
        fd.requests.clear()
        ok, msg, _r = cstore.drupal_translate(
            c.id, article_ids=[bad], actor="x", transport=fd.transport()
        )
        assert not ok, (bad, "must be refused", msg)
        assert not fd.writes, (bad, "NO external write")
        assert not any(p.startswith("/jsonapi/") and "/user" in p for _m, p, _q in fd.requests), (
            bad,
            "no request reached another endpoint",
            fd.requests,
        )
    # operator-config strings are validated too (taxonomy A applies to config, not just input)
    for cfg in (
        {"resource_types": "node--article/../.."},
        {"resource_types": "user--user"},
        {"resource_types": "node--Article"},
        {"resource_types": "config_pages--x"},
        {"resource_types": ""},
        {"api_prefix": "../x"},
        {"api_prefix": "a b"},
        {"api_prefix": "jsonapi?x=1"},
    ):
        f1 = FakeDrupal()
        _seed(f1)
        cn = _conn("A cfg " + json.dumps(cfg), cfg)
        ok, msg, _r = cstore.drupal_translate(cn.id, actor="x", transport=f1.transport())
        assert not ok and not f1.writes, (cfg, msg)
        assert not any("/user" in p or ".." in p for _m, p, _q in f1.requests), (cfg, f1.requests)
    # language codes never reach a URL unvalidated
    dc = _dc({}, fd.transport())
    for loc in ("fr&include=uid", "../x", "fr\r\nX-Evil: 1", "f r", ""):
        try:
            dc.get_translation(f"{ART}:{U1}", loc)
        except ConnectorError:
            continue
        raise AssertionError(("bad language code accepted", loc))
    # a bare uuid is only allowed on a single-type connection
    try:
        _dc({"resource_types": f"{ART}, {PAGE}"}, fd.transport())._resolve_explicit(U1, [ART, PAGE])
    except ConnectorError as e:
        assert "Ambiguous" in str(e)
    else:
        raise AssertionError("ambiguous bare id must be refused")
    print("  A: hostile ids, types, prefixes and language codes refused end to end, zero writes")


def _test_module_guard():
    # WITHOUT the module the site ignores langCode and a PATCH would hit the SOURCE entity.
    fd = FakeDrupal(multilingual=False)
    _seed(fd)
    before = copy.deepcopy(fd.ents)
    c = _conn("no module")
    ok, msg, res = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert not ok and "JSON:API Multilingual" in msg, msg
    assert res == {}, ("refused at discovery, before any MT or per-pair work", res)
    assert fd.ents == before and not fd.writes, "the source entity must be untouched"
    assert _gets_only(fd), "a missing module is caught by reads alone"
    # even a direct strategy write (a code path that skipped discovery) cannot land
    dc = _dc({}, fd.transport())
    try:
        dc.strategy.write_target(dc, f"{ART}:{U1}", "fr", {"title": "Bonjour"}, draft=True)
    except ConnectorError as e:
        assert "JSON:API Multilingual" in str(e)
    else:
        raise AssertionError("write_target must refuse when the module is absent")
    assert fd.ents == before and not fd.writes
    print(
        "  module guard: no module -> run refused before MT, direct write refused, source untouched"
    )

    # with the module, a target equal to the entity's original language is refused
    fm = FakeDrupal()
    _seed(fm)
    dcm = _dc({}, fm.transport())
    try:
        dcm.get_translation(f"{ART}:{U1}", "en")
    except ConnectorError as e:
        assert "original language" in str(e)
    else:
        raise AssertionError("the default translation must never be a write target")
    print("  module guard: the entity's own default language is never a write target")


def _test_create_and_steady_state():
    fd = FakeDrupal()
    _seed(fd)
    src_before = copy.deepcopy(fd.ents[ART][U1]["tr"]["en"])
    c = _conn("create")
    ok, msg, r1 = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert ok and r1["created"] == 2 and r1["updated"] == 0, (msg, r1)
    posts = [w for w in fd.writes if w[0] == "POST"]
    assert {w[3] for w in posts} == {"fr", "de"}, posts
    for _m, _rt, _uuid, lang, attrs in posts:
        assert set(attrs) == {"langcode", "title", "body", "field_tagline", "status"}, attrs
        assert attrs["status"] is False and attrs["langcode"] == lang, "created UNPUBLISHED"
        assert set(attrs["body"]) == {"value", "format", "summary"}, (
            "no `processed`",
            attrs["body"],
        )
        assert attrs["body"]["format"] == "full_html", "text format carried verbatim"
        assert (
            '<a href="/about">' in attrs["body"]["value"] and f"[{lang}]" in attrs["body"]["value"]
        )
        assert attrs["title"].startswith(f"[{lang}]") and "draft" not in attrs
    assert fd.ents[ART][U1]["tr"]["en"] == src_before, "the source translation is untouched"
    assert fd.tr(ART, U1, "fr")["field_tagline"].startswith("[fr]")
    assert not any(fd.tr(ART, U1, "fr")[k] == src_before[k] for k in ("title", "field_tagline")), (
        "no source text left in the new translation (it was seeded, then overwritten)"
    )
    assert set(r1["fields_selected"]) == {"title", "body", "field_tagline"}, r1["fields_selected"]
    assert "field_slug" in r1["fields_skipped"], r1["fields_skipped"]
    print(
        "  C/create: 2 unpublished translations, whole formatted field, format verbatim, "
        "no `processed`"
    )

    # C: the second run over unchanged source is a full no-op
    n = fd.nwrites()
    ok, msg, r2 = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert ok and r2["created"] == 0 and r2["updated"] == 0 and r2["skipped"] == 2, r2
    assert fd.nwrites() == n, "steady state writes nothing"
    # an edit re-translates as an UPDATE; PATCH never carries status, and ALWAYS carries the
    # target langcode (the module validates it; core without the module refuses it, see guard)
    fd.ents[ART][U1]["tr"]["en"]["title"] = "Hello Campus EDITED"
    ok, msg, r3 = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert ok and r3["updated"] == 2 and r3["created"] == 0, r3
    patches = [w for w in fd.writes if w[0] == "PATCH"]
    assert patches and all(
        "status" not in w[4] and w[4].get("langcode") == w[3] != "en" for w in patches
    ), patches
    print(
        "  C: run 2 skipped both (probe + hash), an edit PATCHes both; PATCH never touches status"
    )

    # a PUBLISHED (live) translation is not rewritten by a draft-mode run, and that is VISIBLE
    fd2 = FakeDrupal()
    _seed(fd2)
    human = {"title": "Bonjour(h)", "body": _body("<p>Vieux</p>"), "field_tagline": "h"}
    fd2.add_translation(ART, U1, "fr", human, status=True)
    c2 = _conn("published", {"target_locales": "fr"})
    import app.services.translate as T

    calls = []
    orig_pre = T.pretranslate_document

    def counting(*a, **kw):
        calls.append(1)
        return orig_pre(*a, **kw)

    set_pieces("app.services.translate", "pretranslate_document", counting)
    try:
        ok, msg, r = cstore.drupal_translate(c2.id, actor="auto", transport=fd2.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig_pre)
    assert ok and r["updated"] == 0 and r["live_left_alone"] == 1 and not r["failures"], r
    assert not calls, "a live translation is parked BEFORE any machine translation is spent"
    assert not fd2.writes and fd2.tr(ART, U1, "fr")["title"] == "Bonjour(h)", fd2.writes
    assert f"Kept 1 LIVE (published) translation(s) unchanged: {ART}:{U1}/fr" in msg, msg
    assert [(k["article_id"], k["locale"]) for k in r["live_kept"]] == [(f"{ART}:{U1}", "fr")], r
    assert f"{ART}:{U1}:fr" not in _hashes(c2.id), "left alone is not 'done'"
    # an explicit retry skips the pre-MT park but the write-time guard still refuses it
    ok, msg, r = cstore.drupal_translate(
        c2.id, actor="auto", transport=fd2.transport(), only_pairs={(f"{ART}:{U1}", "fr")}
    )
    assert not fd2.writes and r["live_left_alone"] == 1 and not r["failures"], (msg, r)
    # opt in: the live translation is rewritten and stays published
    c3 = _conn("published opt-in", {"target_locales": "fr", "edit_published": "1"})
    ok, msg, r = cstore.drupal_translate(c3.id, actor="auto", transport=fd2.transport())
    assert ok and r["updated"] == 1 and r["live_left_alone"] == 0, r
    assert fd2.tr(ART, U1, "fr")["status"] is True, "update leaves the published state alone"
    print("  draft mode: a published translation is left alone and reported; opt-in rewrites it")


def _test_held_back_B():
    # all fields held, existing translation: nothing written, stored survives, no hash
    fd = FakeDrupal()
    _seed(fd)
    fd.add_translation(
        ART,
        U1,
        "fr",
        {"title": "Bonjour(h)", "body": _body("<p>Corps(h)</p>"), "field_tagline": "Astuce(h)"},
    )
    c = _conn("all held", {"target_locales": "fr"})
    ok, msg, s = _run_held(c.id, fd, lambda i: True)
    assert ok, msg
    assert not fd.writes and fd.tr(ART, U1, "fr")["title"] == "Bonjour(h)", fd.writes
    assert s["held_back"] == 1 and s["updated"] == 0 and s["created"] == 0 and not s["failures"], s
    assert f"{ART}:{U1}:fr" not in _hashes(c.id), "N3/F: no hash for an all-held no-op"
    ok, msg, s2 = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert ok and s2["updated"] == 1 and s2["held_back"] == 0, s2
    print("  B/F: all fields held -> nothing written, stored translation kept, no hash, retried")

    # title held on UPDATE: the title is omitted; the rest is written; no hash
    fd = FakeDrupal()
    _seed(fd)
    fd.add_translation(
        ART,
        U1,
        "fr",
        {"title": "Bonjour(h)", "body": _body("<p>Corps(h)</p>"), "field_tagline": "Astuce(h)"},
    )
    c = _conn("title held", {"target_locales": "fr"})
    ok, msg, s = _run_held(c.id, fd, lambda i: i == 0)
    assert ok and s["updated"] == 1 and s["held_back"] == 1, s
    assert fd.tr(ART, U1, "fr")["title"] == "Bonjour(h)", "held title keeps its stored value"
    assert fd.tr(ART, U1, "fr")["field_tagline"].startswith("[fr]")
    assert set(fd.writes[-1][4]) == {"body", "field_tagline", "langcode"}, fd.writes[-1]
    assert fd.writes[-1][4]["langcode"] == "fr", fd.writes[-1]
    assert f"{ART}:{U1}:fr" not in _hashes(c.id), "partial update records no hash"
    print("  B: held title omitted on UPDATE (never the source), other fields written, no hash")

    # ONE paragraph of the formatted body held: the whole body field is omitted (whole-or-nothing)
    fd = FakeDrupal()
    _seed(fd)
    stored = _body("<p>Corps(h)</p><p>Suite(h)</p>", summary="Resume(h)")
    fd.add_translation(
        ART, U1, "fr", {"title": "Bonjour(h)", "body": stored, "field_tagline": "Astuce(h)"}
    )
    c = _conn("para held", {"target_locales": "fr"})
    ok, msg, s = _run_held(c.id, fd, lambda i: i == 2)  # body.value#1 ("Apply now.")
    assert ok and s["updated"] == 1, s
    assert fd.tr(ART, U1, "fr")["body"] == stored, "a half-translated formatted field is never sent"
    assert "body" not in fd.writes[-1][4] and "title" in fd.writes[-1][4], fd.writes[-1]
    assert f"{ART}:{U1}:fr" not in _hashes(c.id)
    ok, msg, s2 = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert ok and s2["updated"] == 1, s2
    assert (
        fd.tr(ART, U1, "fr")["body"] != stored
        and fd.tr(ART, U1, "fr")["body"]["format"] == "full_html"
    )
    print("  B: one held paragraph -> the whole formatted field is left alone; the retry writes it")

    # #640 on CREATE: a held unit blocks the create entirely (Drupal would seed the source)
    fd = FakeDrupal()
    _seed(fd)
    c = _conn("640 create", {"target_locales": "fr"})
    ok, msg, s = _run_held(c.id, fd, lambda i: i == 3)  # field_tagline
    assert ok and s["created"] == 0 and s["held_back"] == 1 and not s["failures"], s
    assert not fd.writes and fd.tr(ART, U1, "fr") is None, (
        "no translation is created while a unit is held"
    )
    assert "Held back 1" in msg, msg
    assert f"{ART}:{U1}:fr" not in _hashes(c.id)
    ok, msg, s2 = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert ok and s2["created"] == 1 and s2["held_back"] == 0, s2
    made = fd.tr(ART, U1, "fr")
    assert made["field_tagline"].startswith("[fr]") and made["title"].startswith("[fr]")
    assert f"{ART}:{U1}:fr" in _hashes(c.id)
    print(
        "  #640: a held unit blocks the CREATE (no source-seeded translation), "
        "retried after approval"
    )


def _test_probe_and_failures():
    # C: content moderation hides the new translation -> reported failed, never done
    fd = FakeDrupal(hide_new=True)
    _seed(fd)
    c = _conn("moderation", {"target_locales": "fr"})
    ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert not ok and s["created"] == 0 and s["failures"], (msg, s)
    assert "not readable" in s["failures"][0]["error"], s["failures"]
    assert f"{ART}:{U1}:fr" not in _hashes(c.id), "not marked done"
    print("  C: a created translation invisible on the default revision is a failure, no hash")

    # a 403 on the probe (unpublished translation the user may not view) is NOT 'absent'
    fd = FakeDrupal(can_see_unpublished=False)
    _seed(fd)
    fd.add_translation(ART, U1, "fr", {"title": "Bonjour(h)"}, status=False)
    c = _conn("403 probe", {"target_locales": "fr"})
    ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert not ok and not fd.writes, (msg, fd.writes)
    assert any("403" in f["error"] for f in s["failures"]), s["failures"]
    print("  probe: 403 is an error, never 'absent' (no duplicate-create attempt)")

    # G: a wrongly selected SHARED field is refused by the site, loudly, and changes nothing
    fd = FakeDrupal()
    _seed(fd)
    c = _conn("shared field", {"target_locales": "fr", "exclude_fields": ""})
    ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert not ok and not fd.writes and fd.tr(ART, U1, "fr") is None, (msg, s)
    assert any("422" in f["error"] for f in s["failures"]), s["failures"]
    assert fd.ents[ART][U1]["shared"]["field_ref_code"] == "ABC-1"
    assert f"{ART}:{U1}:fr" not in _hashes(c.id)
    print("  G: a shared field picked by the heuristic is a loud 422 failure, nothing changes")

    # H: backoff via base._request (429 x2 then fine), PATCH retried on 5xx, POST not
    cbase._sleep = lambda *_a, **_k: None
    fd = FakeDrupal()
    _seed(fd)
    fd.throttle = 2
    assert _dc({}, fd.transport()).discover_articles(), "429 with Retry-After is retried"
    fd = FakeDrupal()
    _seed(fd)
    fd.add_translation(ART, U1, "fr", {"title": "Bonjour(h)"})
    fd.fail_next["PATCH"] = [500, 500]
    c = _conn("patch retry", {"target_locales": "fr"})
    ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert ok and s["updated"] == 1, (msg, s)
    fd = FakeDrupal()
    _seed(fd)
    fd.fail_next["POST"] = [500, 500, 500]
    c = _conn("post once", {"target_locales": "fr"})
    ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    posts = [r for r in fd.requests if r[0] == "POST"]
    assert not ok and len(posts) == 1, ("a create POST is never resent after a 5xx", len(posts))
    assert "SECRET-MARKER-9" not in msg and all(
        "SECRET-MARKER-9" not in f["error"] for f in s["failures"]
    ), "the raw upstream body is never echoed"
    print(
        "  H: 429 retried via base._request; PATCH retried on 5xx; POST create sent once; "
        "body not echoed"
    )


def _test_write_guard_review():
    """Review round: the source-overwrite guard cannot be fooled, and a write that lands in the
    wrong language halts the run."""
    src_title = "Hello Campus"

    # (1) the module vanishes BETWEEN the final probe and the PATCH. JSON:API has no
    # precondition header, so the PATCH itself must be unable to land on the source: it carries
    # the target `langcode`, and core (no module) refuses to relabel the default translation to
    # a language the entity already has (ContentEntityBase::onChange throws before save()).
    for variant in ("body", "204"):
        fd = FakeDrupal()
        _seed(fd)
        fd.add_translation(ART, U1, "fr", {"title": "Bonjour(h)"})
        fd.add_translation(ART, U1, "de", {"title": "Hallo(h)"})
        fd.patch_204 = variant == "204"
        fd.before_write = lambda fd=fd: setattr(fd, "multilingual", False)
        c = _conn(f"vanish {variant}")
        ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
        assert not ok and not fd.writes, (variant, "nothing lands", fd.writes)
        assert fd.tr(ART, U1, "en")["title"] == src_title and fd.ents[ART][U1]["default"] == "en"
        assert len(s["failures"]) == 2 and not _hashes(c.id), (variant, s)
    print("  guard: module vanishes before the PATCH (200 and 204) -> core refuses, nothing lands")

    # (1b) the residual a client cannot close: the module vanishes AND the target translation
    # is deleted in the same round trip, so core relabels the SOURCE. The answer is checked,
    # the run HALTS after that one write (no further MT spend or writes, later entities are
    # not attempted) and the message names the recovery.
    for variant in ("body", "204"):
        fd = FakeDrupal()
        _seed(fd)
        _seed(fd, uuid=U2)
        for u in (U1, U2):
            fd.add_translation(ART, u, "fr", {"title": "Bonjour(h)"})
            fd.add_translation(ART, u, "de", {"title": "Hallo(h)"})
        fd.patch_204 = variant == "204"

        def vanish(fd=fd):
            fd.multilingual = False
            fd.ents[ART][U1]["tr"].pop("de", None)
            fd.ents[ART][U1]["tr"].pop("fr", None)

        fd.before_write = vanish
        c = _conn(f"double race {variant}")
        ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
        patches = [w for w in fd.writes if w[0] == "PATCH"]
        assert not ok and len(patches) == 1, (variant, "exactly one write ever lands", fd.writes)
        errs = " | ".join(f["error"] for f in s["failures"])
        assert errs.count("SOURCE OVERWRITE SUSPECTED") == 2, (variant, errs)
        assert msg.startswith("RUN HALTED: SOURCE OVERWRITE SUSPECTED") and "Revisions tab" in msg
        assert s["halted_skipped"] == 1, (variant, "the later entity is not attempted", s)
        assert not _hashes(c.id), "nothing is marked done"
    print("  guard: module AND translation vanish (double race) -> ONE write, run halted, loud")

    # (2) the same on a CREATE: core answers 405 to a POST on an existing resource, nothing lands
    fd = FakeDrupal()
    _seed(fd)
    fd.before_write = lambda: setattr(fd, "multilingual", False)
    c = _conn("vanish create", {"target_locales": "fr"})
    ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert not ok and not fd.writes and fd.tr(ART, U1, "en")["title"] == src_title, (msg, fd.writes)
    print("  guard: module vanishes before the POST -> 405, nothing written")

    # (3) forged or ambiguous probe answers are never a proof
    def src_lang(j):
        j["data"]["attributes"]["langcode"] = "en"
        return j

    def no_lang(j):
        j["data"]["attributes"].pop("langcode", None)
        return j

    def no_default_flag(j):
        j["data"]["attributes"].pop("default_langcode", None)
        return j

    def is_default(j):
        j["data"]["attributes"]["default_langcode"] = True
        return j

    def wrong_type(j):
        j["data"]["type"] = "node--page"
        return j

    def no_type(j):
        j["data"].pop("type", None)
        return j

    def wrong_id(j):
        j["data"]["id"] = U2
        return j

    def as_list(j):
        j["data"] = [j["data"]]
        return j

    forgeries = [
        src_lang,
        no_lang,
        no_default_flag,
        is_default,
        wrong_type,
        no_type,
        wrong_id,
        as_list,
    ]
    for forge in forgeries:
        fd = FakeDrupal()
        _seed(fd)
        fd.add_translation(ART, U1, "fr", {"title": "Bonjour(h)"})
        fd.mutate_get = forge
        dc = _dc({}, fd.transport())
        rid = f"{ART}:{U1}"
        for probe in ("get", "write"):
            try:
                if probe == "get":
                    dc.get_translation(rid, "fr")
                else:
                    dc.strategy.write_target(dc, rid, "fr", {"title": "Salut"}, draft=True)
            except ConnectorError:
                pass
            else:
                raise AssertionError(f"{forge.__name__}: a forged probe answer was accepted")
        assert not fd.writes, (forge.__name__, fd.writes)
    print(
        "  guard: 8 forged probe answers (wrong lang, missing fields, wrong type/id, list) refused"
    )

    # (4) a shared cache in the path: reads ask for no-cache and an Age header is not a proof
    fd = FakeDrupal()
    _seed(fd)
    fd.add_translation(ART, U1, "fr", {"title": "Bonjour(h)"})
    dc = _dc({}, fd.transport())
    assert dc.get_translation(f"{ART}:{U1}", "fr")
    gets = [h for m, h in fd.req_headers if m == "GET"]
    assert gets and all(h.get("cache-control") == "no-cache" for h in gets), "reads bypass caches"
    fd.get_age = 30
    try:
        dc.get_translation(f"{ART}:{U1}", "fr")
    except ConnectorError as e:
        assert "shared cache" in str(e)
    else:
        raise AssertionError("a cache-served probe must not prove the module")
    c = _conn("cached", {"target_locales": "fr"})
    ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert not fd.writes, fd.writes
    print("  guard: probes send no-cache; an Age header (shared cache) fails closed")

    # (5) a bare 404 does not claim the module is present (only the module's own 404 does)
    fd = FakeDrupal()
    _seed(fd)
    fd._meta_404 = lambda ent: _err(404, "Not Found")
    dc = _dc({}, fd.transport())
    sample = dc.discover_articles()[0]
    assert dc.multilingual_state(sample) == "unknown"
    ok, msg = dc.test()
    assert ok and "Could not verify" in msg, msg
    print("  guard: a bare 404 leaves the module 'unverified' (writes still prove it themselves)")

    # (6) a created translation the site PUBLISHED despite status=false is a loud failure
    fd = FakeDrupal()
    _seed(fd)
    fd.force_publish = True
    c = _conn("forced publish", {"target_locales": "fr"})
    ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert not ok and any("PUBLISHED" in f["error"] for f in s["failures"]), (msg, s)
    assert f"{ART}:{U1}:fr" not in _hashes(c.id)
    print("  create: a translation the site published anyway is reported, not marked done")


def _test_selection_D():
    fd = FakeDrupal()
    fd.add(
        ART,
        U1,
        "en",
        {
            "title": "Hello",
            "body": _body(),
            "field_tagline": "sign-up now",
            "field_cta": "Buy now",
            "field_tags": ["a", "b"],
            "path": {"alias": "/hello", "pid": 3, "langcode": "en"},
            "field_link": {"uri": "https://x.io", "title": "Go"},
            "field_email": "a@b.com",
            "field_site": "https://x.io",
            "field_start": "2026-01-01",
            "field_count": 4,
            "field_flag": True,
            "field_slug": "hello-world",
            "field_empty": "",
            "revision_log": "edit",
            "content_translation_source": "und",
        },
    )
    dc = _dc({}, fd.transport())
    ent = dc.discover_articles()[0]
    dc.extract(ent)
    sel, skip = set(dc.sel.selected), dc.sel.skipped
    assert {"title", "body", "field_tagline", "field_cta"} <= sel, ("real copy selected", sel)
    for junk in (
        "field_email",
        "field_site",
        "field_start",
        "field_slug",
        "field_count",
        "field_flag",
    ):
        assert junk in skip, ("junk skipped with a reason", junk, skip)
    assert "path" not in sel and "field_tags" in skip and "field_link" in skip, skip
    assert "revision_log" not in sel and "content_translation_source" not in sel
    summary = dc.sel.selection_summary()
    assert "multi-value" in summary and "structured value" in summary, ("loud skips first", summary)
    # allowlist overrides; a system attribute in it is refused with a visible reason
    dca = _dc({"fields": "title, field_cta, status, langcode, field_missing"}, fd.transport())
    names, sk = dca._field_selection(ent["attrs"])
    assert names == ["title", "field_cta"], names
    assert "status" in sk and "langcode" in sk and "field_missing" in sk, sk
    names, sk = _dc({"exclude_fields": "field_cta"}, fd.transport())._field_selection(ent["attrs"])
    assert "field_cta" not in names and sk["field_cta"] == "operator exclude_fields"
    print(
        "  D: real copy selected, junk/structured/multi-value skipped WITH reasons; allowlist wins"
    )

    # empty entity skipped
    fe = FakeDrupal()
    fe.add(ART, U2, "en", {})
    _seed(fe)
    c = _conn("empty", {"target_locales": "fr"})
    ok, _msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fe.transport())
    assert ok and s["empty_skipped"] == 1 and s["created"] == 1, s
    print("  an entity with no translatable fields is skipped")


def _test_flow_edges():
    # enabled-language rejection + honest source-drop message
    fd = FakeDrupal(languages=("en", "fr"))
    _seed(fd)
    c = _conn("reject", {"target_locales": "fr, de, en"})
    ok, msg, s = cstore.drupal_translate(c.id, actor="auto", transport=fd.transport())
    assert (
        ok and s["locales"] == ["fr"] and s["rejected"] == ["de"] and s["dropped_source"] == ["en"]
    ), s
    assert "equal to the source language" in msg, msg
    fd2 = FakeDrupal(languages=("en",))
    _seed(fd2)
    c2 = _conn("allreject", {"target_locales": "fr"})
    ok, msg, s2 = cstore.drupal_translate(c2.id, actor="auto", transport=fd2.transport())
    assert not ok and s2["locales"] == []
    with Session(engine) as ses:
        assert ses.get(Connection, c2.id).status == "error"
    print("  languages: not-enabled rejected, source dropped and reported, all-rejected -> error")

    # locale case round trip (zh-hans etc. are lowercase in Drupal; mixed case maps to exact)
    fc = FakeDrupal(languages=("en", "pt-BR"))
    _seed(fc)
    c3 = _conn("case", {"target_locales": "pt-BR"})
    ok, _msg, _s3 = cstore.drupal_translate(c3.id, actor="auto", transport=fc.transport())
    assert ok and {w[3] for w in fc.writes} == {"pt-BR"}, fc.writes
    print("  language case: flow lowercases, the wire write uses the site's exact code")

    # WriteBudget
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fb = FakeDrupal()
        _seed(fb)
        c4 = _conn("budget")
        ok, msg, s4 = cstore.drupal_translate(c4.id, actor="auto", transport=fb.transport())
        assert ok and s4["ceiling_hit"] and (s4["created"] + s4["updated"]) == 1, s4
        assert len([w for w in fb.writes if w[0] == "POST"]) == 1
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    print("  H: MAX_WRITES_PER_RUN=1 stops after one external write")

    # egress guard
    from app.services.connectors import egress_guard

    fg = FakeDrupal()
    _seed(fg)
    c5 = _conn("egress")
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s = cstore.drupal_translate(c5.id, actor="a", transport=fg.transport())
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fg.writes, msg
    print("  H: mock MT + real egress refused, no external writes")

    # pull / push
    fp = FakeDrupal()
    _seed(fp)
    dcp = _dc({"exclude_fields": "field_ref_code"}, fp.transport())
    items = dcp.pull()
    assert any(i.field == "title" for i in items) and any(
        i.field.startswith("body.value#") for i in items
    )
    try:
        dcp.push(items)
    except ConnectorError:
        pass
    else:
        raise AssertionError("push must refuse")
    print("  pull() lists title + units; push() refuses (use Translate now)")


def _test_parity_surfaces_H():
    from app.services.connectors import REGISTRY

    assert "drupal" in REGISTRY, "drupal missing from REGISTRY"
    c = TestClient(app)
    assert "Drupal" in c.get("/connectors").text, "tile missing on /connectors"
    form = c.get("/connectors/drupal").text
    assert "Site URL" in form and "Resource types" in form, "type form missing"
    from app.cli.__main__ import build_parser

    assert "connector drupal-translate" in _leaf_paths(build_parser()), "CLI leaf missing"
    from starlette.routing import Route

    from app.web._routes import walk_routes

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/drupal/translate" in api_paths, "/api route missing"
    print("  parity: REGISTRY + /connectors tile + type form + /api route + CLI leaf")


def main() -> None:
    init_db()
    cbase._sleep = lambda *_a, **_k: None
    _test_parity_surfaces_H()
    _test_connector_basics()
    _test_auth()
    _test_discovery()
    _test_scope_escape_A()
    _test_module_guard()
    _test_create_and_steady_state()
    _test_held_back_B()
    _test_probe_and_failures()
    _test_write_guard_review()
    _test_selection_D()
    _test_flow_edges()
    print("verify_drupal OK")


if __name__ == "__main__":
    main()
