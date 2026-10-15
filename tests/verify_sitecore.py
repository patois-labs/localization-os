"""Sitecore XM Cloud connector + connector-framework seams - fully offline via httpx.MockTransport.

FakeSitecore is a STATEFUL fake of the Authoring GraphQL API that enforces the platform's real
write rules (taxonomy G, the Strapi fixture-dodge lesson): updateItem 400-style errors on an
unknown field, an unknown language or a version that does not exist; addItemVersion starts an
EMPTY first version in a language; a write to a SHARED field changes the one value every
language sees (so a run that touched a shared field would visibly corrupt the source); and
updateItem is a partial merge (omitted fields keep their stored value). The token endpoint
checks the client credentials and can expire a token. Because the fake models those rules,
removing the behavior under test (the shared-field guard, the held-back omission, the version
probe) makes an assertion below fail.

Covers the taxonomy A-H checklist (the connector build blueprint section 3):
  A  caller article_ids are strict GUIDs sent only as GraphQL variables; hostile ids, a valid
     GUID outside the content root and operator root_path tricks are refused with ZERO writes;
     a hostile root path never appears in any query text.
  B  a held-back field never overwrites a stored language version; a shared field is never
     written (source language survives, even when the operator allowlists it).
  C  the existence probe reads the same (item, language, latest version) the write lands on, so
     a second run over unchanged source is fully SKIPPED.
  D  schema-driven selection: text types selected; images, links, shared, sharing-unknown,
     machine-shaped values skipped and REPORTED (both directions).
  E  pagination across pages and the cap.
  F  an all-held run records NO content hash; a healthy re-run re-attempts.
  G  the fake rejects unknown fields, missing versions and unknown languages.
  H  WriteBudget ceiling, egress guard, token never logged, rate-limit backoff, the four
     parity surfaces.

ASCII-only prints; final line is 'verify_sitecore OK'.
"""

# ruff: noqa: E501, UP031, RUF005
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

init_env("loctms_sitecore_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import Connection, Segment, SegStatus
from app.services.connectors import base as cbase
from app.services.connectors import sitecore as scmod
from app.services.connectors import store as cstore
from app.services.connectors.sitecore import SitecoreConnector
from app.services.connectors.strategy import ABSENT

CM = "https://xmc-acme-site-dev.sitecorecloud.io"
CM_HOST = "xmc-acme-site-dev.sitecorecloud.io"
ROOT_PATH = "/sitecore/content/Acme/Home"
CLIENT_ID = "cid-abc"
CLIENT_SECRET = "csecret-SUPER-xyz"  # pregate: allow-secret test fixture, not a credential

WRONG_SECRET = "nope"  # pregate: allow-secret test fixture, not a credential
WRONG_CREDS = {"client_id": CLIENT_ID, "client_secret": WRONG_SECRET}

ROOT_ID = "AAAAAAAA" + "0000" + "0000" + "0000" + "000000000001"


def gid(n: int) -> str:
    return "%08X%04X%04X%04X%012X" % (0x1000, 0x2000, 0x3000, 0x4000, n)


ST = "Single-Line Text"
ML = "Multi-Line Text"
RT = "Rich Text"


# ============================================================================
# Fake Sitecore XM Cloud (token endpoint + Authoring GraphQL).
# ============================================================================
class FakeSitecore:
    def __init__(self, languages=None):
        self.languages = languages or ["en", "fr-FR", "de-DE"]
        self.items: dict = {}  # id -> item dict
        self.requests: list = []  # (host, path, operationName, variables, query)
        self.writes: list = []  # (kind, id, lang, version, fields) for SUCCESSFUL writes
        self.add_calls: list = []  # (id, lang)
        self.token_mints = 0  # pregate: allow-secret test fixture, not a credential
        self.valid_token = None
        self.expire_next = False
        self.rate_limit_next = 0  # answer this many requests with 429 first
        self.fail_update_with: str | None = None  # GraphQL error text for the next updateItem
        self.deny_languages = False
        self.fail_delete = False  # deleteItemVersion answers a GraphQL error
        self.delete_calls: list = []  # (id, lang, version) actually deleted
        self.schema_patch = None  # callable(schema dict) -> None, to model a divergent tenant
        self.add_root(ROOT_ID, ROOT_PATH)

    # ---- seeding ------------------------------------------------------------------
    def add_root(self, iid, path):
        self.items[iid] = {
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "template": {},
            "shared_values": {},
            "versions": {},
        }

    def add_item(self, iid, path, fields, *, lang="en", under_root=True):
        """fields: {name: (type, value[, opts])}; opts: shared (True/False/None), unversioned."""
        tmpl, shared_values, vers = {}, {}, {}
        for name, spec in fields.items():
            ftype, value = spec[0], spec[1]
            opts = spec[2] if len(spec) > 2 else {}
            shared = opts.get("shared", False)
            tmpl[name] = {
                "type": ftype,
                "shared": shared,
                "unversioned": bool(opts.get("unversioned", False)),
            }
            if shared:
                shared_values[name] = value
            else:
                vers[name] = value
        self.items[iid] = {
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "template": tmpl,
            "shared_values": shared_values,
            "versions": {lang: [vers]},
        }

    def put_version(self, iid, lang, values):
        """Seed an existing human version (appended)."""
        self.items[iid]["versions"].setdefault(lang, []).append(dict(values))

    def value(self, iid, lang, name, version=None):
        it = self.items[iid]
        if it["template"][name]["shared"] is not False:
            return it["shared_values"].get(name, "")
        vs = it["versions"].get(lang) or []
        if not vs:
            return None
        v = vs[(version or len(vs)) - 1]
        return v.get(name)

    def n_versions(self, iid, lang):
        return len(self.items[iid]["versions"].get(lang) or [])

    def transport(self):
        return httpx.MockTransport(self._handle)

    # ---- helpers ------------------------------------------------------------------
    @staticmethod
    def _norm(i):
        return re.sub(r"[{}\-]", "", str(i)).upper()

    def _err(self, msg):
        return httpx.Response(200, json={"errors": [{"message": msg}], "data": None})

    def _ok(self, data):
        return httpx.Response(200, json={"data": data})

    def _item_json(self, iid, lang, *, fields):
        it = self.items[iid]
        vs = it["versions"].get(lang) or []
        out = {
            "itemId": iid,
            "name": it["name"],
            "path": it["path"],
            "version": len(vs),
            "language": {"name": lang},
        }
        if fields:
            nodes = []
            for name, t in it["template"].items():
                nodes.append(
                    {
                        "name": name,
                        "value": self.value(iid, lang, name) or "",
                        "templateField": {
                            "type": t["type"],
                            # the live schema states sharing as ONE enum (no boolean pair);
                            # None models a tenant that does not state it
                            "versioning": (
                                None
                                if t["shared"] is None
                                else "SHARED"
                                if t["shared"]
                                else "UNVERSIONED"
                                if t["unversioned"]
                                else "VERSIONED"
                            ),
                        },
                    }
                )
            out["fields"] = {"totalCount": len(nodes), "nodes": nodes}
        return out

    # ---- transport ----------------------------------------------------------------
    def _handle(self, req):
        host, path = req.url.host, req.url.path
        if host == "auth.sitecorecloud.io" and path == "/oauth/token":
            body = json.loads(req.content or b"{}")
            self.requests.append((host, path, "token", {"keys": sorted(body)}, ""))
            if (
                body.get("grant_type") != "client_credentials"
                or body.get("audience") != "https://api.sitecorecloud.io"
                or body.get("client_id") != CLIENT_ID
                or body.get("client_secret") != CLIENT_SECRET
            ):
                return httpx.Response(
                    401, json={"error": "access_denied", "error_description": CLIENT_SECRET}
                )
            self.token_mints += 1
            self.valid_token = f"tok-{self.token_mints}"
            return httpx.Response(200, json={"access_token": self.valid_token, "expires_in": 86400})
        if host != CM_HOST or path != "/sitecore/api/authoring/graphql/v1":
            return httpx.Response(404, json={"message": f"unexpected {host}{path}"})
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        auth = req.headers.get("authorization", "")
        if self.expire_next:
            self.expire_next = False
            self.valid_token = (
                "expired-elsewhere"  # pregate: allow-secret test fixture, not a credential
            )
        if auth != f"Bearer {self.valid_token}":
            return httpx.Response(401, json={"message": "unauthorized"})
        body = json.loads(req.content or b"{}")
        op, variables, query = body.get("operationName"), body.get("variables") or {}, body["query"]
        self.requests.append((host, path, op, variables, query))
        # schema validation: ItemTemplateField has `type` and `versioning`, NOT shared/unversioned/typeKey
        m = re.search(r"templateField \{([^}]*)\}", query)
        if m and set(m.group(1).split()) - {"type", "versioning"}:
            return self._err(f"Cannot query field on ItemTemplateField: {m.group(1)}")
        return getattr(self, "_op_" + str(op), self._op_unknown)(variables, query)

    def _op_unknown(self, v, q):
        return self._err("unknown operation")

    @staticmethod
    def _inp(*names):
        return {
            "inputFields": [
                {"name": n, "defaultValue": None, "type": {"kind": "NON_NULL"}} for n in names
            ]
        }

    def _op_SitecoreSchemaCheck(self, v, q):
        assert "__schema" in q and "ItemTemplateField" in q, q
        enum = {
            "kind": "ENUM",
            "name": "ItemFieldVersioning",
            "enumValues": [{"name": "VERSIONED"}, {"name": "UNVERSIONED"}, {"name": "SHARED"}],
        }
        data = {
            "__schema": {
                "mutationType": {
                    "fields": [
                        {"name": n}
                        for n in (
                            "createItem",
                            "updateItem",
                            "deleteItem",
                            "addItemVersion",
                            "deleteItemVersion",
                        )
                    ]
                }
            },
            "tf": {
                "fields": [
                    {"name": "name", "type": {"kind": "SCALAR", "name": "String"}},
                    {"name": "type", "type": {"kind": "SCALAR", "name": "String"}},
                    {
                        "name": "versioning",
                        "type": {"kind": "NON_NULL", "name": None, "ofType": enum},
                    },
                ]
            },
            "addv": {
                "inputFields": self._inp("itemId", "language")["inputFields"]
                + [{"name": "versionNumber", "defaultValue": None, "type": {"kind": "SCALAR"}}]
            },
            "delv": self._inp("itemId", "language", "version"),
            "upd": self._inp("itemId", "language", "version", "fields"),
        }
        if self.schema_patch:
            self.schema_patch(data)
        return self._ok(data)

    def _op_SitecoreDeleteVersion(self, v, q):
        inp = v.get("input") or {}
        iid, lang, ver = self._norm(inp.get("itemId", "")), inp.get("language"), inp.get("version")
        if set(inp) - {"itemId", "language", "version"}:
            return self._err("unknown key in DeleteItemVersionInput")
        if self.fail_delete:
            return self._err("Delete is not allowed: the item is locked")
        vs = (self.items.get(iid) or {}).get("versions", {}).get(lang) or []
        if not isinstance(ver, int) or ver < 1 or ver > len(vs):
            return self._err(f"Version {ver} does not exist in language '{lang}'")
        del vs[ver - 1]
        self.delete_calls.append((iid, lang, ver))
        return self._ok({"deleteItemVersion": {"__typename": "DeleteItemVersionPayload"}})

    def _op_SitecoreLanguages(self, v, q):
        if self.deny_languages:
            return httpx.Response(403, json={"message": "forbidden"})
        return self._ok(
            {
                "languages": {
                    "nodes": [{"name": n, "iso": n[:2], "displayName": n} for n in self.languages]
                }
            }
        )

    def _op_SitecoreItemByPath(self, v, q):
        want = str(v.get("path", "")).lower()
        for iid, it in self.items.items():
            if it["path"].lower() == want:
                return self._ok({"item": {"itemId": iid, "name": it["name"], "path": it["path"]}})
        return self._ok({"item": None})

    def _lookup(self, v):
        iid = self._norm(v.get("id", ""))
        return iid if iid in self.items else None

    def _op_SitecoreItemState(self, v, q):
        if v.get("lang") not in self.languages:
            return self._err(f"Language '{v.get('lang')}' does not exist")
        iid = self._lookup(v)
        if iid is None:
            return self._ok({"item": None})
        return self._ok({"item": self._item_json(iid, v["lang"], fields=False)})

    def _op_SitecoreItem(self, v, q):
        if v.get("lang") not in self.languages:
            return self._err(f"Language '{v.get('lang')}' does not exist")
        iid = self._lookup(v)
        if iid is None:
            return self._ok({"item": None})
        return self._ok({"item": self._item_json(iid, v["lang"], fields=True)})

    def _op_SitecoreSearch(self, v, q):
        m = re.search(r'value: "([0-9A-F]+)"', q)
        root = self.items.get(m.group(1)) if m else None
        if root is None:
            return self._err("bad _path criteria")
        lang = v["lang"]
        hits = [
            iid
            for iid, it in self.items.items()
            if iid != m.group(1)
            and (it["path"] == root["path"] or it["path"].startswith(root["path"] + "/"))
            and it["versions"].get(lang)
        ]
        hits.sort(key=lambda i: self.items[i]["path"])
        size, idx = int(v["pageSize"]), int(v["pageIndex"])
        page = hits[idx * size : (idx + 1) * size]
        return self._ok(
            {
                "search": {
                    "totalCount": len(hits),
                    "results": [
                        {
                            "itemId": i,
                            "path": self.items[i]["path"],
                            "version": self.n_versions(i, lang),
                            "innerItem": self._item_json(i, lang, fields=True),
                        }
                        for i in page
                    ],
                }
            }
        )

    def _op_SitecoreAddVersion(self, v, q):
        inp = v.get("input") or {}
        iid, lang = self._norm(inp.get("itemId", "")), inp.get("language")
        if iid not in self.items:
            return self._err("Item not found")
        if lang not in self.languages:
            return self._err(f"Language '{lang}' does not exist")
        vs = self.items[iid]["versions"].setdefault(lang, [])
        vs.append(dict(vs[-1]) if vs else {})
        self.add_calls.append((iid, lang))
        return self._ok({"addItemVersion": {"item": {"itemId": iid, "version": len(vs)}}})

    def _op_SitecoreUpdateItem(self, v, q):
        inp = v.get("input") or {}
        iid, lang, ver = self._norm(inp.get("itemId", "")), inp.get("language"), inp.get("version")
        if self.fail_update_with:
            msg, self.fail_update_with = self.fail_update_with, None
            return self._err(msg)
        if iid not in self.items:
            return self._err("Item not found")
        if lang not in self.languages:
            return self._err(f"Language '{lang}' does not exist")
        it = self.items[iid]
        vs = it["versions"].get(lang) or []
        if not isinstance(ver, int) or ver < 1 or ver > len(vs):
            return self._err(f"Version {ver} does not exist in language '{lang}'")
        written = {}
        for f in inp.get("fields") or []:
            if set(f) - {"name", "value", "reset"}:
                return self._err("unknown key in FieldValueInput")
            if f.get("name") not in it["template"]:
                return self._err(f"Field '{f.get('name')}' does not exist on the item template")
            if not isinstance(f.get("value"), str):
                return self._err("Field value must be a string")
            written[f["name"]] = f["value"]
        for name, val in written.items():
            if it["template"][name]["shared"] is not False:
                it["shared_values"][name] = val  # ONE value for every language
            else:
                vs[ver - 1][name] = val
        self.writes.append(("update", iid, lang, ver, dict(written)))
        return self._ok({"updateItem": {"item": {"itemId": iid, "version": ver}}})


# ============================================================================
# helpers
# ============================================================================
def _conn(name, cfg_extra=None):
    cfg = {
        "base_url": CM,
        "root_path": ROOT_PATH,
        "source_locale": "en",
        "target_locales": "fr-FR, de-DE",
    }
    cfg.update(cfg_extra or {})
    return cstore.create(
        "sitecore",
        name,
        cfg,
        {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        actor="admin",
    )


def _sc(cfg, transport, secret=None):
    base = {"base_url": CM, "root_path": ROOT_PATH, "source_locale": "en"}
    base.update(cfg or {})
    sec = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET} if secret is None else secret
    return SitecoreConnector(base, sec, transport=transport)


def _needs_review(mod, only_sources=None):
    orig = mod.pretranslate_document

    def patched(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if only_sources is None or sg.source_text in only_sources:
                sg.status = SegStatus.NEEDS_REVIEW
                s.add(sg)
        s.commit()
        return out

    return orig, patched


def _run_held(T, conn_id, fs, only_sources=None, **kw):
    orig, patched = _needs_review(T, only_sources)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        return cstore.sitecore_translate(conn_id, actor="auto", transport=fs.transport(), **kw)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)


def _basic_fs():
    fs = FakeSitecore()
    fs.add_item(
        gid(1),
        ROOT_PATH + "/About",
        {
            "Title": (ST, "About us"),
            "Text": (RT, "<p>Hello world.</p><p>Second paragraph.</p>"),
            "Summary": (ML, "A short summary."),
        },
    )
    return fs


def main() -> None:
    from app.web._routes import walk_routes

    init_db()
    import app.services.translate as T

    # ---- (1) capability, test(), language resolve, containers -------------------------
    assert SitecoreConnector.capability.topology == "T2"
    fs = _basic_fs()
    sc = _sc({}, fs.transport())
    ok, msg = sc.test()
    assert ok, msg
    assert "3 language" in msg and ROOT_PATH in msg, msg
    assert set(sc.enabled_locales()) == {"en", "fr-fr", "de-de"}
    assert sc.resolve_locale("fr-fr") == "fr-FR"
    assert sc.source_locale() == "en"
    assert sc.containers()[0]["id"] == ROOT_PATH
    print("  connector: T2, test(), case-preserving language resolve (fr-fr -> fr-FR)")

    # ---- (2) base_url + root_path guards --------------------------------------------
    for bad in (
        "http://" + CM_HOST,
        CM + "/sitecore/api/authoring/graphql/v1",
        "https://user:pw@" + CM_HOST,
        # gate: only Sitecore-hosted CM hosts; the bearer token never goes elsewhere
        "https://cm.sitecore.test",
        "https://attacker.example",
        "https://sitecorecloud.io",
        "https://auth.sitecorecloud.io",
        "https://api.sitecorecloud.io",
        "https://edge.sitecorecloud.io",
        "https://evilsitecorecloud.io",
        "https://xmc.sitecorecloud.io.attacker.example",
        CM + ":8443",
        "https://xmc_bad.sitecorecloud.io",
        CM + "?x=1",
        "https://169.254.169.254",
        "",
    ):
        r = False
        try:
            _sc({"base_url": bad}, fs.transport())._base()
        except cbase.ConnectorError:
            r = True
        assert r, ("base_url must be refused", bad)
    assert _sc({"base_url": CM + "/"}, fs.transport())._base() == CM
    assert _sc({"base_url": CM + ":443"}, fs.transport())._base() == CM
    assert _sc({"base_url": CM.upper().replace("HTTPS", "https")}, fs.transport())._base() == CM
    for bad in (
        "",
        "/sitecore",
        "/sitecore/content",
        "/sitecore/content/",
        "/sitecore/system/Settings",
        "/sitecore/content/../system",
        "/sitecore/content/a//b",
        "/sitecore/content/a/..",
        "/sitecore/content/a?x",
        "/sitecore/content/a#x",
        "/sitecore/content/a\\b",
        "/sitecore/content/a\r\nb",
        "/sitecore/content/a\x00b",
        "/sitecore/content/{GUID}",
        "sitecore/content/a",
    ):
        r = False
        try:
            _sc({"root_path": bad}, fs.transport())._root_path_cfg()
        except cbase.ConnectorError:
            r = True
        assert r, ("root_path must be refused", repr(bad))
    print("  guards: https-origin-only base_url; metadata IP blocked; hostile root_path refused")

    # ---- (3) auth: client credentials, token reuse, 401 re-mint, no secret leaks ------
    fs3 = _basic_fs()
    logs = []

    class _Cap(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    cap = _Cap(level=logging.DEBUG)
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.addHandler(cap)
    root_logger.setLevel(logging.DEBUG)
    try:
        sc3 = _sc({}, fs3.transport())
        assert sc3.test()[0]
        assert fs3.token_mints == 1, ("one token per connector instance", fs3.token_mints)
        sc3.enabled_locales()
        sc3.discover_articles()
        assert fs3.token_mints == 1
        fs3.expire_next = True
        sc3._lang_cache = None
        assert sc3.enabled_locales(), "a 401 mid-run re-mints once and retries"
        assert fs3.token_mints == 2, ("re-minted exactly once", fs3.token_mints)
        bad = _sc({}, fs3.transport(), secret=WRONG_CREDS)
        ok, msg = bad.test()
        assert not ok and "401" in msg, msg
        assert CLIENT_SECRET not in msg and "access_denied" not in msg, ("body not echoed", msg)
        missing = _sc({}, fs3.transport(), secret={})
        assert not missing.test()[0]
    finally:
        root_logger.removeHandler(cap)
        root_logger.setLevel(prev_level)
    blob = "\n".join(logs)
    assert CLIENT_SECRET not in blob and "tok-1" not in blob and "tok-2" not in blob, (
        "token and secret never reach the logs"
    )
    print("  auth: client-credentials mint, reuse, single 401 re-mint, secret/token never logged")

    # ---- (4) D: schema-driven selection, both directions, reported ---------------------
    fs4 = FakeSitecore()
    fs4.add_item(
        gid(2),
        ROOT_PATH + "/Page",
        {
            "Title": (ST, "Welcome"),
            "Body": (RT, "<p>Rich body.</p>"),
            "Intro": (ML, "Intro text here."),
            "Hero": ("Image", '<image mediaid="{X}" />'),
            "Link": ("General Link", '<link linktype="external" url="http://x" />'),
            "Show": ("Checkbox", "1"),
            "CssClass": (ST, "hero-wide"),
            "SharedHead": (ST, "Shared heading", {"shared": True}),
            "UnknownShare": (ST, "Maybe shared", {"shared": None}),
            "SiteUrl": (ST, "https://example.com/x"),
            "Code": (ST, "12345"),
            "Ident": (ST, "{110D559F-DEA5-42EA-9C1C-8A5DF7E70EF9}"),
            "Empty": (ST, ""),
            "Unver": (ST, "Unversioned text", {"unversioned": True}),
        },
    )
    sc4 = _sc({"exclude_fields": "CssClass"}, fs4.transport())
    art = sc4.discover_articles()[0]
    specs, skipped = sc4._field_selection(art["fields"])
    names = {s["name"]: s["type"] for s in specs}
    assert names == {
        "Title": "string",
        "Body": "html",
        "Intro": "textarea",
        "Unver": "string",
    }, names
    for bad_name, why in [
        ("Hero", "not translatable text"),
        ("Link", "not translatable text"),
        ("Show", "not translatable text"),
        ("CssClass", "exclude_fields"),
        ("SharedHead", "shared across all languages"),
        ("UnknownShare", "sharing unknown"),
        ("SiteUrl", "URL"),
        ("Code", "number"),
        ("Ident", "id"),
    ]:
        assert bad_name in skipped and why in skipped[bad_name], (bad_name, skipped.get(bad_name))
    assert "Empty" not in names, "an empty field is not selected"
    # an operator allowlist cannot override the shared-field guard
    sc4b = _sc({"fields": "Title, SharedHead, UnknownShare, Hero, Nope"}, fs4.transport())
    specs_b, skipped_b = sc4b._field_selection(art["fields"])
    assert [s["name"] for s in specs_b] == ["Title"], specs_b
    assert "shared across all languages" in skipped_b["SharedHead"], skipped_b
    assert "sharing unknown" in skipped_b["UnknownShare"]
    assert "not a field on this item" in skipped_b["Nope"], skipped_b
    # the run report names selected + skipped
    sc4.extract(art)
    summ = sc4.selection_summary()
    assert "Translatable field(s):" in summ and "Skipped:" in summ and "SharedHead" in summ, summ
    print(
        "  D: text types selected; image/link/checkbox/shared/unknown-share/machine values skipped + reported"
    )

    # ---- (5) full flow: CREATE (addItemVersion then updateItem), source untouched -------
    fs5 = _basic_fs()
    c5 = _conn("create", {"target_locales": "fr-FR"})
    ok, msg, s5 = cstore.sitecore_translate(c5.id, actor="auto", transport=fs5.transport())
    assert ok, msg
    assert s5["created"] == 1 and s5["updated"] == 0 and not s5["failures"], s5
    assert fs5.add_calls == [(gid(1), "fr-FR")], (
        "one version added in the exact-case language",
        fs5.add_calls,
    )
    assert len(fs5.writes) == 1, fs5.writes
    _k, wid, wlang, wver, wfields = fs5.writes[0]
    assert (wid, wlang, wver) == (gid(1), "fr-FR", 1)
    assert set(wfields) == {"Title", "Text", "Summary"}, wfields
    assert fs5.value(gid(1), "en", "Title") == "About us", "source language survives"
    assert fs5.value(gid(1), "en", "Text") == "<p>Hello world.</p><p>Second paragraph.</p>"
    assert fs5.n_versions(gid(1), "en") == 1
    assert "<p>" in fs5.value(gid(1), "fr-FR", "Text") and fs5.value(
        gid(1), "fr-FR", "Text"
    ) != fs5.value(gid(1), "en", "Text")
    print(
        "  flow CREATE: addItemVersion then one updateItem; source en version untouched; HTML kept"
    )

    # ---- (6) C: second run is fully SKIPPED (probe = version >= 1), edit re-translates --
    probe = _sc({}, fs5.transport())
    assert probe.strategy.exists_target(probe, gid(1), "fr-fr") is True
    assert probe.get_translation(gid(1), "de-DE") is ABSENT
    fs5.writes.clear()
    fs5.add_calls.clear()
    ok, _m, r2 = cstore.sitecore_translate(c5.id, actor="auto", transport=fs5.transport())
    assert ok and r2["created"] == 0 and r2["updated"] == 0 and r2["skipped"] == 1, r2
    assert not fs5.writes and not fs5.add_calls, "run 2 wrote nothing"
    fs5.items[gid(1)]["versions"]["en"][0]["Title"] = "About us EDITED"
    ok, _m, r3 = cstore.sitecore_translate(c5.id, actor="auto", transport=fs5.transport())
    assert ok and r3["updated"] == 1 and r3["created"] == 0, ("edited source re-translates", r3)
    assert not fs5.add_calls, "an existing language version is updated, never re-added"
    assert fs5.n_versions(gid(1), "fr-FR") == 1
    print("  C: run 2 SKIPPED; edited source -> updated in place (no new version)")

    # ---- (7) update targets the LATEST version; partial merge keeps unsent fields ------
    fs7 = FakeSitecore()
    fs7.add_item(gid(3), ROOT_PATH + "/P", {"Title": (ST, "Hello"), "Summary": (ML, "Sum")})
    fs7.put_version(gid(3), "fr-FR", {"Title": "Ancien", "Summary": "Resume humain"})
    fs7.put_version(gid(3), "fr-FR", {"Title": "Ancien v2", "Summary": "Resume humain v2"})
    c7 = _conn("latest", {"target_locales": "fr-FR"})
    orig, patched = _needs_review(T, only_sources={"Sum"})
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s7 = cstore.sitecore_translate(c7.id, actor="auto", transport=fs7.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert [w[3] for w in fs7.writes] == [2], ("written to the latest version 2", fs7.writes)
    assert set(fs7.writes[0][4]) == {"Title"}, ("held Summary omitted from the write", fs7.writes)
    assert fs7.value(gid(3), "fr-FR", "Summary") == "Resume humain v2", "B: held field keeps stored"
    assert fs7.value(gid(3), "fr-FR", "Summary", version=1) == "Resume humain", (
        "older version untouched"
    )
    assert s7["updated"] == 1 and s7.get("partial_held") == 1, s7
    assert any("awaiting review" in f["error"] for f in s7["failures"]) or s7.get("partial_held"), (
        s7
    )
    print(
        "  update: latest version written, held field omitted (stored value survives), partial_held reported"
    )

    # ---- (8) B/F + #640: all-held writes nothing and records no hash -------------------
    fs8 = _basic_fs()
    fs8.put_version(
        gid(1), "fr-FR", {"Title": "Humain", "Text": "<p>Humain</p>", "Summary": "Humain"}
    )
    c8 = _conn("held", {"target_locales": "fr-FR"})
    ok, msg, s8 = _run_held(T, c8.id, fs8)
    assert ok, msg
    assert not fs8.writes and not fs8.add_calls, ("all held back -> NO write", fs8.writes)
    assert fs8.value(gid(1), "fr-FR", "Title") == "Humain"
    assert (
        s8["held_back"] == 1 and s8["created"] == 0 and s8["updated"] == 0 and not s8["failures"]
    ), s8
    ok2, _m2, s82 = cstore.sitecore_translate(c8.id, actor="auto", transport=fs8.transport())
    assert ok2 and s82["updated"] == 1 and s82["skipped"] == 0, ("F: hash not poisoned", s82)
    assert fs8.value(gid(1), "fr-FR", "Title") != "Humain"
    print("  B/F: all-held run writes nothing, human fr survives, no hash; healthy re-run writes")

    # ---- (9) #640: a NEW language version is not created while a field is held ---------
    fs9 = _basic_fs()
    c9 = _conn("nocreate", {"target_locales": "fr-FR"})
    ok, msg, s9 = _run_held(T, c9.id, fs9, only_sources={"A short summary."})
    assert ok, msg
    assert not fs9.add_calls and not fs9.writes, (
        "no version created while a unit is held",
        fs9.add_calls,
    )
    assert fs9.n_versions(gid(1), "fr-FR") == 0
    assert s9["held_back"] == 1 and s9["created"] == 0, s9
    ok2, _m2, s92 = cstore.sitecore_translate(c9.id, actor="auto", transport=fs9.transport())
    assert ok2 and s92["created"] == 1 and fs9.n_versions(gid(1), "fr-FR") == 1, s92
    print("  #640: held field -> no new language version, no hash; healthy re-run creates it")

    # ---- (10) Rich Text is all-or-nothing --------------------------------------------
    fs10 = FakeSitecore()
    fs10.add_item(
        gid(4), ROOT_PATH + "/R", {"Title": (ST, "T"), "Text": (RT, "<p>One.</p><p>Two.</p>")}
    )
    fs10.put_version(
        gid(4), "fr-FR", {"Title": "Titre humain", "Text": "<p>Un humain.</p><p>Deux humain.</p>"}
    )
    c10 = _conn("rt", {"target_locales": "fr-FR"})
    ok, msg, s10 = _run_held(T, c10.id, fs10, only_sources={"Two."})
    assert ok, msg
    assert "Text" not in fs10.writes[0][4] if fs10.writes else True
    assert fs10.value(gid(4), "fr-FR", "Text") == "<p>Un humain.</p><p>Deux humain.</p>", (
        "a partly held Rich Text keeps its stored value, no mixed-language body"
    )
    assert s10.get("partial_held") == 1, s10
    print("  Rich Text: one held sentence -> whole field left alone (no mixed-language body)")

    # ---- (11) shared fields are never written (source survives), even if allowlisted ----
    fs11 = FakeSitecore()
    fs11.add_item(
        gid(5),
        ROOT_PATH + "/S",
        {"Title": (ST, "Local title"), "Heading": (ST, "Shared heading", {"shared": True})},
    )
    c11 = _conn("shared", {"target_locales": "fr-FR", "fields": "Title, Heading"})
    ok, msg, s11 = cstore.sitecore_translate(c11.id, actor="auto", transport=fs11.transport())
    assert ok and s11["created"] == 1, (msg, s11)
    assert set(fs11.writes[0][4]) == {"Title"}, fs11.writes
    assert fs11.items[gid(5)]["shared_values"]["Heading"] == "Shared heading", (
        "shared source intact"
    )
    assert "Heading" in s11["fields_skipped"], s11["fields_skipped"]
    # non-vacuity: the fake really would have changed the source language through a shared field
    direct = _sc({}, fs11.transport())
    direct._gql(
        "SitecoreUpdateItem",
        "mutation SitecoreUpdateItem($input: UpdateItemInput!) { updateItem(input: $input) { item { itemId } } }",
        {
            "input": {
                "itemId": gid(5),
                "language": "fr-FR",
                "version": 1,
                "fields": [{"name": "Heading", "value": "CORRUPT", "reset": False}],
            }
        },
    )
    assert fs11.value(gid(5), "en", "Heading") == "CORRUPT", "fake models shared-field semantics"
    print("  B: shared field never written (source language survives); fake models the corruption")

    # ---- (11b) schema shape + paged fields (review fix) ------------------------------
    # The Authoring schema states sharing as ONE enum `versioning` on ItemTemplateField (no
    # shared/unversioned booleans, no typeKey) and pages Item.fields at 50. A query that selects
    # the boolean pair fails validation on a real tenant; a short field page must be reported.
    for q in (scmod.Q_ITEM, scmod.Q_SEARCH):
        assert "versioning" in q and "shared" not in q and "typeKey" not in q, q
        assert "totalCount" in q and "first:" in q, q
    fs11b = FakeSitecore()
    fs11b.add_item(gid(6), ROOT_PATH + "/T", {"Title": (ST, "Short"), "Body": (ML, "Long text")})
    sc11b = _sc({}, fs11b.transport())
    art11b = sc11b.discover_articles()[0]
    art11b["fields_total"] = 120  # the API said there are more fields than were returned
    sc11b.extract(art11b)
    assert "(other fields)" in sc11b.sel.skipped_dict(), sc11b.sel.skipped_dict()
    print("  schema: sharing read from the versioning enum; short field page reported")

    # ---- (12) G: the fake enforces real validation -----------------------------------
    fsg = _basic_fs()
    scg = _sc({}, fsg.transport())
    mut = "mutation SitecoreUpdateItem($input: UpdateItemInput!) { updateItem(input: $input) { item { itemId } } }"
    for bad_input, why in [
        (
            {
                "itemId": gid(1),
                "language": "fr-FR",
                "version": 1,
                "fields": [{"name": "Title", "value": "x"}],
            },
            "version",
        ),
        (
            {
                "itemId": gid(1),
                "language": "en",
                "version": 1,
                "fields": [{"name": "Nope", "value": "x"}],
            },
            "does not exist",
        ),
        ({"itemId": gid(1), "language": "xx", "version": 1, "fields": []}, "Language"),
    ]:
        r = None
        try:
            scg._gql("SitecoreUpdateItem", mut, {"input": bad_input})
        except cbase.ConnectorError as e:
            r = str(e)
        assert r and why.lower() in r.lower(), (bad_input, r)
    print("  G: fake rejects missing version, unknown field, unknown language")

    # ---- (13) A: ids are strict GUIDs; foreign-scope GUID refused; zero writes ---------
    fsa = _basic_fs()
    fsa.add_item(gid(9), "/sitecore/content/Other/Secret", {"Title": (ST, "Secret")})
    sca = _sc({}, fsa.transport())
    must_raise = [
        "..",
        "../x",
        "%2e%2e%2f",
        "1" * 31,
        gid(1) + "0",
        gid(1)[:-1],
        gid(1) + "?x=1",
        gid(1) + "#f",
        gid(1) + "/../" + gid(9),
        gid(1) + "\x00",
        gid(1) + "\r\n",
        " " + gid(1),
        gid(1) + " ",
        gid(1) + "," + gid(9),
        "{" + gid(1),
        "Z" * 32,
        "١" * 32,
        "",
        "item:" + gid(1),
        "urn:uuid:" + gid(1),
    ]
    for bad in must_raise:
        r = False
        try:
            sca.discover_articles(article_ids=[bad])
        except cbase.ConnectorError:
            r = True
        assert r, (repr(bad), "must raise before any request")
    sca2 = _sc({}, fsa.transport())
    try:
        sca2.discover_articles(article_ids=[gid(1), "../x"])
        raise AssertionError("one hostile id refuses the whole request")
    except cbase.ConnectorError:
        pass
    assert not [q for q in fsa.requests if q[2] in ("SitecoreItem", "SitecoreItemState")], (
        "validated before ANY item request",
        fsa.requests,
    )
    # a valid GUID outside the content root is refused
    r = False
    try:
        _sc({}, fsa.transport()).discover_articles(article_ids=[gid(9)])
    except cbase.ConnectorError as e:
        r = "outside" in str(e)
    assert r, "valid GUID outside the root must be refused"
    # end to end: zero external writes for every hostile id and the foreign one
    ca = _conn("scope", {"target_locales": "fr-FR"})
    for bad in must_raise + [gid(9)]:
        fsa.writes.clear()
        fsa.add_calls.clear()
        ok, msg, _s = cstore.sitecore_translate(
            ca.id, article_ids=[bad], actor="x", transport=fsa.transport()
        )
        assert not fsa.writes and not fsa.add_calls, (repr(bad), "ZERO external writes")
    # forms of a valid id canonicalize (braces, hyphens, lower case) and translate
    fsa.writes.clear()
    ok, msg, sa = cstore.sitecore_translate(
        ca.id,
        article_ids=[
            "{%s-%s-%s-%s-%s}"
            % (gid(1)[:8].lower(), gid(1)[8:12], gid(1)[12:16], gid(1)[16:20], gid(1)[20:])
        ],
        actor="x",
        transport=fsa.transport(),
    )
    assert ok and sa["created"] == 1 and fsa.writes[0][1] == gid(1), (msg, sa)
    # a well-formed but missing id is skipped, not an error
    fsa.writes.clear()
    ok, msg, _s = cstore.sitecore_translate(
        ca.id, article_ids=[gid(777)], actor="x", transport=fsa.transport()
    )
    assert not fsa.writes
    # the write seam re-checks scope too (retry pairs come from stored failures)
    r = False
    try:
        _sc({}, fsa.transport()).create_translation(gid(9), "fr-FR", {"\x00field:Title": "x"})
    except cbase.ConnectorError:
        r = True
    assert r, "create_translation refuses an out-of-scope item"
    r = False
    try:
        _sc({}, fsa.transport()).get_translation("../x", "fr-FR")
    except cbase.ConnectorError:
        r = True
    assert r
    print(
        "  A: hostile ids refused before any request; foreign-root GUID refused; zero writes; id forms canonicalize"
    )

    # ---- (14) A: hostile values travel as variables, never in query text ---------------
    fsq = _basic_fs()
    evil = 'a"b) x'
    fsq.add_root(gid(50), "/sitecore/content/Acme/" + evil)
    scq = _sc({"root_path": "/sitecore/content/Acme/" + evil}, fsq.transport())
    scq.root()
    scq.discover_articles()
    assert not any(evil in q[4] for q in fsq.requests), "hostile text never reaches a query string"
    assert any(q[3].get("path", "").endswith(evil) for q in fsq.requests), "it rode as a variable"
    # operator root that does not exist -> clean error
    ok, msg = _sc({"root_path": "/sitecore/content/Nowhere"}, fsq.transport()).test()
    assert not ok and "not found" in msg, msg
    print("  A: hostile root path rides as a GraphQL variable only; missing root -> clean error")

    # ---- (15) E: discovery, pagination, cap, scope filter -------------------------------
    fse = FakeSitecore()
    for i in range(120):
        fse.add_item(gid(100 + i), f"{ROOT_PATH}/Section/Page{i:03d}", {"Title": (ST, f"Page {i}")})
    fse.add_item(gid(900), "/sitecore/content/Elsewhere/X", {"Title": (ST, "Out of scope")})
    # an item that exists only in French must not be listed for the English source
    fse.add_item(gid(901), f"{ROOT_PATH}/FrOnly", {"Title": (ST, "Seulement")}, lang="fr-FR")
    sce = _sc({"max_items": "500"}, fse.transport())
    got = sce.discover_articles()
    assert len(got) == 120, len(got)
    assert {a["id"] for a in got} == {gid(100 + i) for i in range(120)}, (
        "scoped, source-language only"
    )
    pages = sorted(q[3]["pageIndex"] for q in fse.requests if q[2] == "SitecoreSearch")
    assert pages == [0, 1, 2], ("three pages of 50 walked", pages)
    capped = _sc({"max_items": "7"}, fse.transport()).discover_articles()
    assert len(capped) == 7, len(capped)
    print(
        "  E: 120 items across 3 pages, cap honoured, out-of-scope and other-language items excluded"
    )

    # ---- (16) H: WriteBudget ceiling --------------------------------------------------
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fs16 = _basic_fs()
        c16 = _conn("budget", {"target_locales": "fr-FR, de-DE"})
        ok, _m, s16 = cstore.sitecore_translate(c16.id, actor="auto", transport=fs16.transport())
        assert ok and s16["ceiling_hit"] and (s16["created"] + s16["updated"]) == 1, s16
        assert len(fs16.writes) == 1, fs16.writes
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    print("  H: WriteBudget MAX_WRITES_PER_RUN=1 stops after exactly one external write")

    # ---- (17) H: egress guard fires ----------------------------------------------------
    from app.services.connectors import egress_guard

    fs17 = _basic_fs()
    c17 = _conn("egress", {"target_locales": "fr-FR"})
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s = cstore.sitecore_translate(c17.id, actor="a", transport=fs17.transport())
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fs17.writes and not fs17.add_calls, msg
    print("  H: egress guard - mock MT + real egress refused, no external writes")

    # ---- (18) rate limit backoff via base._request ------------------------------------
    fs18 = _basic_fs()
    slept = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        sc18 = _sc({}, fs18.transport())
        sc18.enabled_locales()  # mints the token first
        fs18.rate_limit_next = 2
        sc18._lang_cache = None
        assert sc18.enabled_locales(), "429 then 429 then success"
    finally:
        cbase._sleep = real_sleep
    assert slept and all(s >= 2 for s in slept[:2]), ("Retry-After honoured", slept)
    print("  H: 429 + Retry-After is retried by the shared backoff")

    # ---- (19) vendor errors: per-pair failure; version added but fields failed ----------
    # Gate: an empty language version switches off Sitecore's language fallback, so the version
    # this run added is deleted again when its fields fail to write.
    fs19 = _basic_fs()
    c19 = _conn("vendor", {"target_locales": "fr-FR"})
    fs19.fail_update_with = "Item is locked by another user"
    ok, msg, s19 = cstore.sitecore_translate(c19.id, actor="auto", transport=fs19.transport())
    assert not ok and s19["failures"], (msg, s19)
    err19 = s19["failures"][0]["error"]
    assert "locked" in err19 and "removed again" in err19 and "fallback" in err19, err19
    assert fs19.add_calls == [(gid(1), "fr-FR")]
    assert fs19.delete_calls == [(gid(1), "fr-FR", 1)], fs19.delete_calls
    assert fs19.n_versions(gid(1), "fr-FR") == 0, "no empty language version is left behind"
    assert fs19.n_versions(gid(1), "en") == 1 and fs19.value(gid(1), "en", "Title") == "About us"
    fs19.add_calls.clear()
    ok, msg, s19b = cstore.sitecore_translate(c19.id, actor="auto", transport=fs19.transport())
    assert ok and s19b["created"] == 1 and fs19.add_calls == [(gid(1), "fr-FR")], s19b
    assert fs19.n_versions(gid(1), "fr-FR") == 1 and fs19.value(gid(1), "fr-FR", "Title"), (
        "the retry creates one filled version"
    )
    print("  vendor error: fields failed -> the added version is deleted again, retry creates it")

    # the cleanup itself fails: loud EMPTY-version message, and the retry fills that version
    fs19d = _basic_fs()
    c19d = _conn("vendor-nodelete", {"target_locales": "fr-FR"})
    fs19d.fail_update_with = "Item is locked by another user"
    fs19d.fail_delete = True
    ok, msg, s19d = cstore.sitecore_translate(c19d.id, actor="auto", transport=fs19d.transport())
    err19d = s19d["failures"][0]["error"]
    assert not ok and "EMPTY language version 1" in err19d and "locked" in err19d, err19d
    assert fs19d.n_versions(gid(1), "fr-FR") == 1 and not fs19d.delete_calls
    fs19d.fail_delete = False
    fs19d.add_calls.clear()
    ok, msg, s19e = cstore.sitecore_translate(c19d.id, actor="auto", transport=fs19d.transport())
    assert ok and s19e["updated"] == 1 and not fs19d.add_calls, (
        "retry updates the empty version, no second add",
        s19e,
    )
    assert fs19d.n_versions(gid(1), "fr-FR") == 1 and fs19d.value(gid(1), "fr-FR", "Title")
    print("  vendor error: undeletable empty version is reported loudly; the retry fills it")

    # a version this run did not provably create (number > 1) is never deleted
    fs19f = _basic_fs()
    sc19f = _sc({}, fs19f.transport())
    r19f = sc19f._undo_new_version(gid(1), "en", 2, "boom")
    assert not r19f.ok and "left in place" in r19f.message and not fs19f.delete_calls, r19f
    assert fs19f.n_versions(gid(1), "en") == 1

    # addItemVersion lands but the connection drops: reported (never deleted, never resent)
    fs19g = _basic_fs()
    real_add = fs19g._op_SitecoreAddVersion

    def _add_then_drop(v, q):
        real_add(v, q)
        raise httpx.ReadTimeout("connection dropped after the add landed")

    fs19g._op_SitecoreAddVersion = _add_then_drop
    sc19g = _sc({}, fs19g.transport())
    r19g = sc19g.create_translation(gid(1), "fr-FR", {scmod.FIELD_PREFIX + "Title": "A propos"})
    assert not r19g.ok and "EMPTY language version" in r19g.message, r19g
    assert not fs19g.delete_calls and len(fs19g.add_calls) == 1, "not retried, not deleted"
    print("  vendor error: a non-idempotent add is never resent; a possibly-landed one is reported")

    # 403 on the language list -> clean failure, status error
    fs19c = _basic_fs()
    fs19c.deny_languages = True
    c19c = _conn("deny", {"target_locales": "fr-FR"})
    ok, msg, _s = cstore.sitecore_translate(c19c.id, actor="auto", transport=fs19c.transport())
    assert not ok and "403" in msg and not fs19c.writes, msg
    with Session(engine) as s:
        assert s.get(Connection, c19c.id).status == "error"
    print("  vendor error: 403 -> (False, msg) + ERROR status, no writes")

    # ---- (19b) gate: live schema is introspected first; a mismatch writes nothing -------
    def _mismatch(patch):
        f = _basic_fs()
        f.schema_patch = patch
        return f

    def _drop_mut(name):
        def p(d):
            d["__schema"]["mutationType"]["fields"] = [
                x for x in d["__schema"]["mutationType"]["fields"] if x["name"] != name
            ]

        return p

    def _drop_tf(name):
        def p(d):
            d["tf"]["fields"] = [x for x in d["tf"]["fields"] if x["name"] != name]

        return p

    def _versioning_bool(d):
        for x in d["tf"]["fields"]:
            if x["name"] == "versioning":
                x["type"] = {"kind": "SCALAR", "name": "Boolean", "enumValues": None}

    def _enum_missing_shared(d):
        for x in d["tf"]["fields"]:
            if x["name"] == "versioning":
                x["type"]["ofType"]["enumValues"] = [{"name": "VERSIONED"}, {"name": "UNVERSIONED"}]

    def _add_required(d):
        d["addv"]["inputFields"].append(
            {"name": "database", "defaultValue": None, "type": {"kind": "NON_NULL"}}
        )

    def _no_type(alias):
        def p(d):
            d[alias] = None

        return p

    def _drop_input(alias, name):
        def p(d):
            d[alias]["inputFields"] = [x for x in d[alias]["inputFields"] if x["name"] != name]

        return p

    cases = [
        (_drop_mut("addItemVersion"), "addItemVersion is missing"),
        (_drop_mut("deleteItemVersion"), "deleteItemVersion is missing"),
        (_drop_tf("versioning"), "versioning is missing"),
        (_drop_tf("type"), "ItemTemplateField.type is missing"),
        (_versioning_bool, "not the SHARED"),
        (_enum_missing_shared, "not the SHARED"),
        (_add_required, "AddItemVersionInput.database is required"),
        (_no_type("addv"), "AddItemVersionInput is missing"),
        (_no_type("tf"), "ItemTemplateField is missing"),
        (_drop_input("upd", "version"), "UpdateItemInput.version is missing"),
        (_drop_input("delv", "language"), "DeleteItemVersionInput.language is missing"),
    ]
    for i, (patch, want) in enumerate(cases):
        f = _mismatch(patch)
        ok_t, msg_t = _sc({}, f.transport()).test()
        assert not ok_t and "schema check failed" in msg_t and want in msg_t, (want, msg_t)
        cx = _conn(f"schema-{i}", {"target_locales": "fr-FR"})
        ok, msg, _s = cstore.sitecore_translate(cx.id, actor="auto", transport=f.transport())
        assert not ok and "schema check failed" in msg and want in msg, (want, msg)
        assert not f.writes and not f.add_calls and not f.delete_calls, (want, "nothing written")
        ops = [r[2] for r in f.requests]
        assert "SitecoreItem" not in ops and "SitecoreSearch" not in ops, (want, ops)
        r = False
        try:
            _sc({}, f.transport()).create_translation(
                gid(1), "fr-FR", {scmod.FIELD_PREFIX + "Title": "x"}
            )
        except cbase.ConnectorError as e:
            r = "schema check failed" in str(e)
        assert r and not f.add_calls, (want, "a direct write is refused too")
    # introspection refused (disabled or blocked) also fails closed
    f = _basic_fs()
    f._op_SitecoreSchemaCheck = lambda v, q: f._err("Introspection is not allowed")
    ok_t, msg_t = _sc({}, f.transport()).test()
    assert not ok_t and "did not answer schema introspection" in msg_t, msg_t
    # an extra enum value or an extra OPTIONAL input field is tolerated; the check runs once
    f = _basic_fs()

    def _tolerated(d):
        for x in d["tf"]["fields"]:
            if x["name"] == "versioning":
                x["type"]["ofType"]["enumValues"].append({"name": "FUTURE"})
        d["upd"]["inputFields"].append(
            {"name": "database", "defaultValue": '"master"', "type": {"kind": "NON_NULL"}}
        )

    f.schema_patch = _tolerated
    sct = _sc({}, f.transport())
    assert sct.test()[0] and sct.discover_articles()
    assert [r[2] for r in f.requests].count("SitecoreSchemaCheck") == 1
    print(
        "  gate: schema introspection fails closed on 11 mismatch shapes; nothing read or written"
    )

    # ---- (20) language handling -------------------------------------------------------
    fs20 = _basic_fs()
    c20 = _conn("langs", {"target_locales": "fr-FR, ja-JP, en"})
    ok, msg, s20 = cstore.sitecore_translate(c20.id, actor="auto", transport=fs20.transport())
    assert ok and s20["locales"] == ["fr-fr"] and s20["rejected"] == ["ja-jp"], s20
    assert s20["dropped_source"] == ["en"] and "equal to the source language" in msg, (s20, msg)
    assert {w[2] for w in fs20.writes} == {"fr-FR"}, (
        "exact-case language on the wire",
        fs20.writes,
    )
    c20b = _conn("badsrc", {"source_locale": "zz", "target_locales": "fr-FR"})
    ok, msg, _s = cstore.sitecore_translate(
        c20b.id, actor="auto", transport=_basic_fs().transport()
    )
    assert not ok and "does not exist" in msg, msg
    print(
        "  languages: unknown target rejected, source dropped and reported, unknown source fails loud"
    )

    # ---- (21) credential pin: moving base_url drops the stored client id/secret ----------
    from app.services.connectors import store_part2

    assert store_part2._credential_host(
        "sitecore", {"base_url": CM}
    ) == store_part2._credential_host("sitecore", {"base_url": CM + "/"})
    assert store_part2._credential_host(
        "sitecore", {"base_url": CM}
    ) != store_part2._credential_host("sitecore", {"base_url": "https://attacker.example"})
    print("  pin: sitecore credential destination follows base_url")

    # ---- (22) harvest + generic pull -----------------------------------------------------
    from app.services.connectors import harvest as _hv

    assert _hv._map_sitecore({"title": "About us"})["source"].get("title") == "About us"
    fs22 = _basic_fs()
    pulled = _sc({}, fs22.transport()).pull()
    assert {p.external_id for p in pulled} == {gid(1)} and any(p.text == "About us" for p in pulled)
    r = False
    try:
        _sc({}, fs22.transport()).push(pulled)
    except cbase.ConnectorError:
        r = True
    assert r
    print("  harvest title mapper + generic pull; push refuses (use Translate now)")

    # ---- (23) retry_failed re-attempts exactly the failed pair ----------------------------
    fs23 = _basic_fs()
    c23 = _conn("retry", {"target_locales": "fr-FR"})
    fs23.fail_update_with = "boom"
    ok, _m, _s = cstore.sitecore_translate(c23.id, actor="auto", transport=fs23.transport())
    assert not ok
    ok, msg, s23 = cstore.sitecore_translate(
        c23.id, actor="auto", transport=fs23.transport(), only_pairs={(gid(1), "fr-fr")}
    )
    assert ok and s23["updated"] + s23["created"] == 1, (msg, s23)
    print("  retry: only_pairs re-attempts the failed (item, language) pair")

    # ---- (24) H: parity surfaces --------------------------------------------------------
    from app.services.connectors import REGISTRY

    assert "sitecore" in REGISTRY
    c = TestClient(app)
    assert "Sitecore" in c.get("/connectors").text
    typ = c.get("/connectors/sitecore").text
    assert "CM host URL" in typ and "Content root path" in typ, "type form renders"
    from app.cli.__main__ import build_parser

    assert "connector sitecore-translate" in _leaf_paths(build_parser())
    from starlette.routing import Route

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/sitecore/translate" in api_paths
    print("  H parity: REGISTRY + /connectors tile + type form + /api route + CLI leaf")

    print("verify_sitecore OK")


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
    _ = copy  # kept for fixture edits
    main()
