"""Shopify connector + the T2-flavored ShopifyTranslationStrategy — fully offline via
httpx.MockTransport (a fake Shopify Admin GraphQL API that models the identity-less
translation triple (resourceId, key, locale), the server-computed source `digest`, the
`translationsRegister` upsert, cost-based THROTTLED throttling, and INVALID_DIGEST
rejection).

Shopify is the THIRD hand-built connector-factory connector (Strapi #1 T2, Contentful #2
T1). It is T2-flavored: a translation has NO independent identity — it is read from the
master resource's `translations(locale:)` overlay and written via the single upsert
`translationsRegister`, each write carrying a FRESHLY-READ source-content digest.

The fake is deliberately STRICT so the assertions are non-vacuous (taxonomy G): a register
whose per-key `translatableContentDigest` does not match the CURRENT source digest -> an
INVALID_DIGEST userError; an unknown key -> a userError; a cost-throttle mode returns the
real THROTTLED shape. Under those rules the happy-path writes still pass, which is what
makes the register assertions meaningful.

Covers the connector-build defect taxonomy (the connector build blueprint §3):
  A  the store_name host-injection allowlist (gotcha #1) + caller resourceId scope/shape
  B  a held-back (NEEDS_REVIEW) key never overwrites a stored translation
  C  read/probe/write target the same state -> the SECOND run suppresses (no loop)
  D  server-driven translatability: text keys selected; machine-typed keys + the URL
     `handle` skipped and REPORTED; allowlist/exclude overrides
  E  the write cap is distributed round-robin across resource types (no starvation)
  F  an all-held-back run records NO content-hash, so a later healthy run re-attempts
  G  the fake enforces Shopify semantics (INVALID_DIGEST / unknown key / THROTTLED)
  H  the standing repo-law sweep: WriteBudget ceiling, mock-egress guard, ASCII CLI

Plus the three digest gotchas: (1) store_name allowlist; (2) digest freshness — a source
edit between materialize and write still succeeds with the fresh digest, an INVALID_DIGEST
race is retried and then classified loudly+retryably; (3) NO silent no-op skip — an
identical-to-source translation is still REGISTERED and suppressed next run (not re-picked).
Plus cost-throttle backoff, content-hash suppression, locale-case round-trip, pagination,
empty skip, parity surfaces, org isolation.

ASCII-only prints; final line is 'verify_shopify OK'.
"""

import hashlib
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_shopify_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import Connection, Segment, SegStatus
from app.services.connectors import base as cbase
from app.services.connectors import shopify_part2 as _sp2
from app.services.connectors import store as cstore
from app.services.connectors.shopify import (
    API_VERSION,
    ShopifyConnector,
    ShopifyTranslationStrategy,
    _validate_store_handle,
)

# shopify.dev/docs/api/usage/versioning (checked 2026-10-10): the stable versions still
# accessible after 2025-10 sunsets on 2026-10-16 15:00 UTC. A newer version must be added
# here deliberately when the pin moves.
SUPPORTED_API_VERSIONS = frozenset({"2026-01", "2026-04", "2026-07", "2026-10"})


# TranslationErrorCode enum, shopify.dev 2026-10 (checked 2026-10-10):
# https://shopify.dev/docs/api/admin-graphql/2026-10/enums/TranslationErrorCode
# The fake may only emit codes from this list (#652). Shopify's docs do not name a code for a
# stale digest; the closest is INVALID_TRANSLATABLE_CONTENT, which the connector treats as
# the digest race. The live QA run must confirm it (runbook MANDATORY-1).
REAL_TRANSLATION_ERROR_CODES = frozenset(
    {
        "BLANK",
        "FAILS_RESOURCE_VALIDATION",
        "INVALID",
        "INVALID_CODE",
        "INVALID_FORMAT",
        "INVALID_KEY_FOR_MODEL",
        "INVALID_LOCALE_FOR_SHOP",
        "INVALID_MARKET_LOCALIZABLE_CONTENT",
        "INVALID_TRANSLATABLE_CONTENT",
        "INVALID_VALUE_FOR_HANDLE_TRANSLATION",
        "MARKET_CUSTOM_CONTENT_NOT_ALLOWED",
        "MARKET_DOES_NOT_EXIST",
        "MARKET_LOCALE_CREATION_FAILED",
        "RESOURCE_NOT_FOUND",
        "RESOURCE_NOT_MARKET_CUSTOMIZABLE",
        "RESOURCE_NOT_TRANSLATABLE",
        "TOO_MANY_KEYS_FOR_RESOURCE",
        "INVALID_LOCALE_FOR_MARKET",
    }
)
DIGEST_RACE_CODE = "INVALID_TRANSLATABLE_CONTENT"


def _digest(value: str) -> str:
    """The fake's server-computed source digest — a hash of the CURRENT source value, so
    mutating the source value changes the digest (exactly what makes INVALID_DIGEST
    non-vacuous and the freshness guarantee testable)."""
    return "d:" + hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


# ============================================================================
# Fake Shopify Admin GraphQL API (single endpoint). Dispatches on the operation
# text (there is one endpoint; a real fake keys off the query, not a parser).
# ============================================================================
ALL_FAKES: list = []


class FakeShopify:
    def __init__(self, store="acme", locales=None):
        self.store = store
        self.locales = locales or [
            {"locale": "en", "name": "English", "primary": True, "published": True},
            {"locale": "fr", "name": "French", "primary": False, "published": True},
            {"locale": "de", "name": "German", "primary": False, "published": True},
        ]
        # rid -> {"type": rt, "content": {key: {"value","type"}}}
        self.resources: dict = {}
        # rid -> {locale: {key: value}}   (the registered overlay)
        self.translations: dict = {}
        self.registers: list = []  # (rid, locale, inputs) on every ACCEPTED register
        self.throttle_n = 0  # throttle the next N graphql calls
        self.invalid_digest_once: set = set()  # rid: mutate 'title' source + INVALID_DIGEST once
        self.always_invalid: set = set()  # rid: every register -> INVALID_DIGEST
        self.poison_keys: dict = {}  # rid -> {key}: a non-digest userError per key
        self.poison_one_at_a_time = False  # return only the FIRST poison key's error
        # Access scopes the app was granted (currentAppInstallation.accessScopes). None = the
        # query errors (a token the API will not describe).
        self.scopes = [
            "read_locales",
            "read_translations",
            "write_translations",
            "read_products",
            "read_content",
        ]
        # Client-credentials grant (#653). client_creds = (id, secret) enables the mint
        # endpoint; valid_tokens (a set) makes GraphQL require a live token (else 401).
        self.client_creds: tuple | None = None
        self.mint_error: str | None = None  # e.g. "shop_not_permitted" -> 400
        self.valid_tokens: set | None = None
        self.mint_count = 0
        self.mint_ttl = 86399
        self.mint_bodies: list = []
        self.seen_tokens: list = []  # X-Shopify-Access-Token of every GraphQL call
        self.emitted_codes: set = set()  # every userError code the fake has returned
        ALL_FAKES.append(self)
        self.list_reverse_args: list = []  # the `reverse` variable of every list call
        self.bad_versions: list = []  # request versions outside SUPPORTED_API_VERSIONS

    # ---- seeding --------------------------------------------------------
    def add_resource(self, rid, rtype, content):
        """content = {key: (value, type)}."""
        self.resources[rid] = {
            "type": rtype,
            "content": {k: {"value": v, "type": t} for k, (v, t) in content.items()},
        }

    def seed_translation(self, rid, locale, key, value):
        self.translations.setdefault(rid, {}).setdefault(locale, {})[key] = value

    def transport(self):
        return httpx.MockTransport(self._handle)

    # ---- helpers --------------------------------------------------------
    def _content_nodes(self, rid, locale):
        c = self.resources[rid]["content"]
        return [
            {
                "key": k,
                "value": d["value"],
                "digest": _digest(d["value"]),
                "type": d["type"],
                "locale": locale,
            }
            for k, d in c.items()
        ]

    def _overlay(self, rid, locale):
        reg = (self.translations.get(rid) or {}).get(locale) or {}
        return [{"key": k, "value": v, "locale": locale} for k, v in reg.items()]

    def _throttled(self):
        return httpx.Response(
            200,
            json={
                "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                "extensions": {
                    "cost": {
                        "requestedQueryCost": 10,
                        "throttleStatus": {
                            "maximumAvailable": 1000,
                            "currentlyAvailable": 5,
                            "restoreRate": 50,
                        },
                    }
                },
            },
        )

    def _handle(self, req):
        if str(req.url) == f"https://{self.store}.myshopify.com/admin/oauth/access_token":
            return self._mint(req)
        expect = f"https://{self.store}.myshopify.com/admin/api/"
        if not str(req.url).startswith(expect):
            return httpx.Response(404, json={"errors": [{"message": "wrong host/path"}]})
        # #651: a version outside the supported window is refused (Shopify stops serving a
        # sunset version; 2025-10 is unsupported after 2026-10-16 3:00 PM UTC).
        ver = str(req.url)[len(expect) :].split("/", 1)[0]
        if ver not in SUPPORTED_API_VERSIONS:
            self.bad_versions.append(ver)
            return httpx.Response(404, json={"errors": [{"message": f"unsupported {ver}"}]})
        tok = req.headers.get("X-Shopify-Access-Token")
        self.seen_tokens.append(tok)
        if self.valid_tokens is not None and tok not in self.valid_tokens:
            return httpx.Response(401, json={"errors": "[API] Invalid API key or access token"})
        if self.throttle_n > 0:
            self.throttle_n -= 1
            return self._throttled()
        body = json.loads(req.content or b"{}")
        query = body.get("query") or ""
        var = body.get("variables") or {}

        if "currentAppInstallation" in query:
            if self.scopes is None:
                return httpx.Response(
                    200, json={"errors": [{"message": "Access denied for currentAppInstallation"}]}
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "currentAppInstallation": {
                            "accessScopes": [{"handle": s} for s in self.scopes]
                        }
                    }
                },
            )

        if "shopLocales" in query:
            return httpx.Response(200, json={"data": {"shopLocales": self.locales}})

        if "translatableResources(" in query:  # LIST (plural)
            rtype = var.get("type")
            first = int(var.get("first") or 100)
            after = var.get("after")
            ids = [rid for rid, r in self.resources.items() if r["type"] == rtype]
            # Shopify lists oldest-first (ascending id) unless `reverse: true` (#654).
            ids.sort(key=lambda x: int(x.rsplit("/", 1)[-1]))
            self.list_reverse_args.append(bool(var.get("reverse")))
            if var.get("reverse"):
                ids.reverse()
            start = (ids.index(after) + 1) if (after in ids) else 0
            page = ids[start : start + first]
            edges = [
                {
                    "cursor": rid,
                    "node": {
                        "resourceId": rid,
                        "translatableContent": self._content_nodes(rid, "en"),
                    },
                }
                for rid in page
            ]
            has_next = (start + first) < len(ids)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "translatableResources": {
                            "edges": edges,
                            "pageInfo": {
                                "hasNextPage": has_next,
                                "endCursor": page[-1] if page else None,
                            },
                        }
                    }
                },
            )

        if "translatableResource(" in query:  # SINGLE
            rid = var.get("id")
            loc = var.get("locale")
            if rid not in self.resources:
                return httpx.Response(200, json={"data": {"translatableResource": None}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "translatableResource": {
                            "resourceId": rid,
                            "translatableContent": self._content_nodes(rid, "en"),
                            "translations": self._overlay(rid, loc),
                        }
                    }
                },
            )

        if "translationsRegister" in query:  # MUTATION (upsert)
            rid = var.get("id")
            inputs = var.get("translations") or []
            if rid not in self.resources:
                return self._register_errors(
                    [{"code": "RESOURCE_NOT_FOUND", "message": f"no such resource {rid}"}]
                )
            if rid in self.always_invalid:
                return self._register_errors(
                    [
                        {
                            "code": DIGEST_RACE_CODE,
                            "message": "Digest does not match the resource's content.",
                        }
                    ]
                )
            if rid in self.invalid_digest_once:
                self.invalid_digest_once.discard(rid)
                # simulate a real race on ONE key only: 'title' source changes under the
                # write -> INVALID_DIGEST once. On retry the connector re-reads and now sees
                # the per-key drift, so it drops 'title' and registers the rest.
                tgt = (
                    "title"
                    if "title" in self.resources[rid]["content"]
                    else next(iter(self.resources[rid]["content"]))
                )
                self.resources[rid]["content"][tgt]["value"] += " (edited mid-write)"
                return self._register_errors(
                    [
                        {
                            "code": DIGEST_RACE_CODE,
                            "message": "Digest does not match the resource's content.",
                        }
                    ]
                )
            # MEDIUM-1: reject the poison key(s), attributing each to its input index via the
            # `field` path (['translations','<idx>','value']) — the connector must drop those
            # and register the rest. poison_one_at_a_time returns only the FIRST, to exercise
            # the per-key drop loop's independent budget (M2).
            poison = self.poison_keys.get(rid) or set()
            perr = [
                {
                    "code": "FAILS_RESOURCE_VALIDATION",
                    "field": ["translations", str(i), "value"],
                    "message": f"invalid value for {inp.get('key')}",
                }
                for i, inp in enumerate(inputs)
                if inp.get("key") in poison
            ]
            if perr:
                return self._register_errors(perr[:1] if self.poison_one_at_a_time else perr)
            content = self.resources[rid]["content"]
            for inp in inputs:
                key = inp.get("key")
                if key not in content:
                    return self._register_errors(
                        [
                            {
                                "code": "INVALID_KEY_FOR_MODEL",
                                "message": f"unknown key {key}",
                            }
                        ]
                    )
                if inp.get("translatableContentDigest") != _digest(content[key]["value"]):
                    return self._register_errors(
                        [
                            {
                                "code": DIGEST_RACE_CODE,
                                "message": "Digest does not match the resource's content.",
                            }
                        ]
                    )
            for inp in inputs:
                self.translations.setdefault(rid, {}).setdefault(inp["locale"], {})[inp["key"]] = (
                    inp["value"]
                )
            self.registers.append((rid, inputs[0]["locale"] if inputs else None, inputs))
            return httpx.Response(
                200,
                json={
                    "data": {
                        "translationsRegister": {
                            "userErrors": [],
                            "translations": [
                                {"key": i["key"], "value": i["value"], "locale": i["locale"]}
                                for i in inputs
                            ],
                        }
                    }
                },
            )

        return httpx.Response(
            200, json={"errors": [{"message": f"unexpected query: {query[:60]}"}]}
        )

    def _mint(self, req):
        from urllib.parse import parse_qs

        assert req.method == "POST"
        assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        self.mint_bodies.append(form)
        assert form.get("grant_type") == "client_credentials", form
        if self.mint_error:
            return httpx.Response(400, json={"error": self.mint_error})
        if (form.get("client_id"), form.get("client_secret")) != self.client_creds:
            return httpx.Response(
                400, json={"error": "invalid_request", "error_description": form["client_secret"]}
            )
        self.mint_count += 1
        token = f"shpat_minted_{self.mint_count}"
        if self.valid_tokens is not None:
            self.valid_tokens.add(token)
        return httpx.Response(
            200,
            json={"access_token": token, "scope": "read_products", "expires_in": self.mint_ttl},
        )

    def _register_errors(self, errs):
        for e in errs:
            self.emitted_codes.add(e.get("code"))
        return httpx.Response(
            200, json={"data": {"translationsRegister": {"userErrors": errs, "translations": []}}}
        )


# --- standard product content. Covers taxonomy-D both directions:
#   title (STRING)     -> selected
#   body_html (HTML)   -> selected
#   meta_title (STRING)-> selected
#   handle (STRING)    -> skipped by NAME (URL slug must not be MT'd)
#   spec_json (JSON)   -> skipped by TYPE (machine value)
#   ext_url (URI)      -> skipped by TYPE
def _std_content():
    return {
        "title": ("Hello world", "STRING"),
        "body_html": ("<p>Some prose.</p>", "HTML"),
        "meta_title": ("Hello - SEO", "STRING"),
        "handle": ("hello-world", "STRING"),
        "spec_json": ('{"w":1}', "JSON"),
        "ext_url": ("https://acme.com/x", "URI"),
    }


def _seed_std(fs, rid="gid://shopify/Product/1"):
    fs.add_resource(rid, "PRODUCT", _std_content())
    return rid


def _conn(name, cfg_extra=None):
    cfg = {
        "store_name": "acme",
        "resource_types": "PRODUCT",
        "source_locale": "en",
        "target_locales": "fr, de",
    }
    cfg.update(cfg_extra or {})
    return cstore.create("shopify", name, cfg, {"access_token": "shpat_x"}, actor="admin")


def _cc(fs, cfg=None):
    base = {"store_name": fs.store, "resource_types": "PRODUCT", "source_locale": "en"}
    base.update(cfg or {})
    return ShopifyConnector(base, {"access_token": "shpat_x"}, transport=fs.transport())


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


def main() -> None:
    from app.web._routes import walk_routes

    init_db()

    # ---- (1) capability + strategy + test + locale resolve + source default ----
    assert ShopifyConnector.capability.topology == "T2"
    assert ShopifyTranslationStrategy().topology == "T2"
    fs = FakeShopify(
        locales=[
            {"locale": "en", "primary": True, "published": True},
            {"locale": "fr", "published": True},
            {"locale": "pt-BR", "published": True},
        ]
    )
    _seed_std(fs)
    cc = _cc(fs)
    ok, msg = cc.test()
    assert ok, msg
    # #655: test() reads the granted scopes and names a missing one.
    fs_s = FakeShopify()
    fs_s.scopes = ["read_locales", "read_translations", "read_products"]
    ok_s, msg_s = _cc(fs_s).test()
    assert not ok_s and "write_translations" in msg_s, ("#655 names the missing write scope", msg_s)
    fs_s.scopes = ["read_locales", "write_translations", "read_products"]  # write implies read
    ok_s, msg_s = _cc(fs_s).test()
    assert ok_s and msg_s.startswith("Connected to Shopify"), (ok_s, msg_s)
    fs_s.scopes = ["read_locales", "read_translations", "write_translations"]
    ok_s, msg_s = _cc(fs_s).test()  # PRODUCT configured but read_products absent: warn only
    assert ok_s and "read_products" in msg_s, ("#655 resource scope warning", msg_s)
    fs_s.scopes = ["read_locales", "read_translations", "write_translations", "write_products"]
    ok_s, msg_s = _cc(fs_s).test()
    assert ok_s and "read_products" not in msg_s, ("write_products implies read_products", msg_s)
    fs_s.scopes = None  # the scope query errors: connect still works, says it could not check
    ok_s, msg_s = _cc(fs_s).test()
    assert ok_s and "could not" in msg_s.lower(), ("#655 scope read failure is non-fatal", msg_s)
    fs_s.scopes = ["read_locales", "read_translations", "write_translations", "read_products"]
    ok_s, msg_s = _cc(fs_s).test()
    assert ok_s and msg_s.startswith("Connected to Shopify"), msg_s
    # ---- #653: client-credentials auth (client id + secret mint a 24 h token) ----
    import logging

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.text = []

        def emit(self, record):
            self.text.append(record.getMessage())

    cap653 = _Cap()
    logging.getLogger().addHandler(cap653)
    _old_lvl = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    _sp2._reset_token_cache()
    SECRET653 = "shpss_" + "z" * 24
    fs_c = FakeShopify()
    fs_c.client_creds = ("cid-1", SECRET653)
    fs_c.valid_tokens = set()
    _seed_std(fs_c)

    def _ccc(fs, cid="cid-1", sec=SECRET653, **extra):
        sec_d = {"client_id": cid, "client_secret": sec}
        sec_d.update(extra)
        return ShopifyConnector(
            {"store_name": fs.store, "resource_types": "PRODUCT"}, sec_d, transport=fs.transport()
        )

    okc, msgc = _ccc(fs_c).test()
    assert okc, msgc
    assert fs_c.mint_count == 1, ("#653 one mint", fs_c.mint_count)
    assert fs_c.seen_tokens and set(fs_c.seen_tokens) == {"shpat_minted_1"}, fs_c.seen_tokens
    # a NEW connector instance (every web action builds one) reuses the cached token
    okc, msgc = _ccc(fs_c).test()
    assert okc and fs_c.mint_count == 1, ("#653 token cached across instances", fs_c.mint_count)
    # expiry: past the refresh margin of the 24 h token -> re-mint
    _real_now = _sp2._now
    _sp2._now = lambda: _real_now() + 86399 - 30
    try:
        okc, msgc = _ccc(fs_c).test()
        assert okc and fs_c.mint_count == 2, ("#653 re-mint before expiry", fs_c.mint_count)
    finally:
        _sp2._now = _real_now
    # revoked/expired mid-run: a 401 re-mints once and the call succeeds
    fs_c.valid_tokens.clear()
    before = fs_c.mint_count
    okc, msgc = _ccc(fs_c).test()
    assert okc and fs_c.mint_count == before + 1, ("#653 401 -> one re-mint", msgc)
    # a rotated secret never reuses the old cached token
    fs_c.client_creds = ("cid-1", SECRET653 + "2")
    n_before = fs_c.mint_count
    okc, msgc = _ccc(fs_c, sec=SECRET653 + "2").test()
    assert okc and fs_c.mint_count == n_before + 1, "#653 rotated secret mints fresh"
    # a wrong secret: clean error, the secret is not echoed even if upstream echoes it
    okc, msgc = _ccc(fs_c, sec="wrong-secret-value").test()
    assert not okc and "client id" in msgc.lower(), msgc
    assert "wrong-secret-value" not in msgc, ("#653 secret leaked into an error", msgc)
    # gate: an upstream that echoes the secret or client id as the short `error` code itself
    for echoed in (SECRET653, "cid-1"):
        fs_c.mint_error = echoed
        _sp2._reset_token_cache()
        okc, msgc = _ccc(fs_c).test()
        assert not okc and echoed not in msgc, ("#653 credential echoed in error code", msgc)
    fs_c.mint_error = None
    # shop_not_permitted is named with its fix
    fs_c.mint_error = "shop_not_permitted"
    _sp2._reset_token_cache()
    okc, msgc = _ccc(fs_c).test()
    assert not okc and "shop_not_permitted" in msgc and "same Shopify organization" in msgc, msgc
    fs_c.mint_error = None
    # half a pair is refused by name, with no network call
    n_mint = len(fs_c.mint_bodies)
    for half, word in (
        ({"cid": "cid-1", "sec": ""}, "client secret"),
        ({"cid": "", "sec": "s"}, "client id"),
    ):
        okc, msgc = _ccc(fs_c, cid=half["cid"], sec=half["sec"]).test()
        assert not okc and word in msgc, (word, msgc)
    assert len(fs_c.mint_bodies) == n_mint, "#653 half a pair must not call Shopify"
    # neither credential: the message names both options
    okc, msgc = ShopifyConnector({"store_name": "acme"}, {}, transport=fs_c.transport()).test()
    assert not okc and "client id" in msgc and "access token" in msgc, msgc
    # a static token still works and never mints
    fs_t = FakeShopify()
    fs_t.client_creds = ("cid-1", SECRET653)
    okc, msgc = _cc(fs_t).test()
    assert okc and fs_t.mint_count == 0 and not fs_t.mint_bodies, "static token must not mint"
    # a full flow run with client credentials only (no access_token anywhere)
    _sp2._reset_token_cache()
    fs_f = FakeShopify()
    fs_f.client_creds = ("cid-1", SECRET653)
    fs_f.valid_tokens = set()
    rid_f = _seed_std(fs_f)
    conn_f = cstore.create(
        "shopify",
        "cc-flow",
        {
            "store_name": "acme",
            "resource_types": "PRODUCT",
            "source_locale": "en",
            "target_locales": "fr",
        },
        {"client_id": "cid-1", "client_secret": SECRET653},
        actor="admin",
    )
    ok_f, msg_f, s_f = cstore.shopify_translate(conn_f.id, actor="auto", transport=fs_f.transport())
    assert ok_f and s_f["created"] == 1, (msg_f, s_f)
    assert fs_f.translations[rid_f].get("fr"), "#653 client-credentials run wrote translations"
    assert fs_f.mint_count == 1, ("#653 one mint for a whole run", fs_f.mint_count)
    # the credential fields are declared on the spec (web form, CLI --secret, API all read it)
    names = {f.name: f for f in ShopifyConnector.spec.secret_fields}
    assert {"client_id", "client_secret", "access_token"} <= set(names), names
    assert names["client_secret"].secret and not names["client_secret"].required
    assert not names["access_token"].required and not names["client_id"].required
    from app.cli.cmd_connector import _validate_secret_keys

    _validate_secret_keys("shopify", {"client_id": "a", "client_secret": "b"})
    # secrets and tokens never reach a log record
    joined = "\n".join(cap653.text)
    assert SECRET653 not in joined and "shpat_minted" not in joined, "#653 secret/token logged"
    logging.getLogger().removeHandler(cap653)
    logging.getLogger().setLevel(_old_lvl)
    _sp2._reset_token_cache()
    print(
        "  #653: client id + secret mint a cached 24 h token (re-mint on expiry / 401); safe errors"
    )

    # ---- #656: setup text matches the current Shopify flow ----
    steps656 = " ".join(ShopifyConnector.spec.setup_steps)
    assert "Dev Dashboard" in steps656 and "Client ID" in steps656, steps656
    assert "Develop apps" not in steps656 and "Partners" not in steps656, steps656
    assert set(cc.enabled_locales()) == {"en", "fr", "pt-br"}
    assert cc.resolve_locale("pt-br") == "pt-BR", cc.resolve_locale("pt-br")
    assert cc._source_locale() == "en", cc._source_locale()
    print(
        "  connector: T2 capability + strategy, test(), case-preserving locale resolve, "
        "primary source locale"
    )

    # ---- (2) GOTCHA #1 + taxonomy A: store_name host-injection allowlist ----
    assert _validate_store_handle("acme") == "acme"
    assert _validate_store_handle("Acme-EU") == "acme-eu"
    assert _validate_store_handle("acme.myshopify.com") == "acme", "full host reduced to handle"
    # a handle that is a PREFIX of another is still its own distinct valid handle
    assert (
        _validate_store_handle("shop") == "shop" and _validate_store_handle("shop-eu") == "shop-eu"
    )
    for bad in (
        "evil.com/",
        "x.evil.com/",
        "a/b",
        "a.b",
        "a@b",
        "ac me",
        "-acme",
        "acme-",
        "acmé",
        "",
        "   ",
        "acme\x00x",
        "acme\rx",
        "acme\nx",
    ):
        raised = False
        try:
            _validate_store_handle(bad)
        except cbase.ConnectorError:
            raised = True
        assert raised, ("gotcha #1: unsafe store handle must be refused", repr(bad))
    # the endpoint host is the store handle's myshopify domain, never attacker-controlled
    assert _cc(fs)._endpoint() == (
        f"https://acme.myshopify.com/admin/api/{API_VERSION}/graphql.json"
    )
    # #651: the pinned version is one Shopify still serves after the 2025-10 sunset, and
    # every request the connector sends carries it.
    assert API_VERSION in SUPPORTED_API_VERSIONS, ("#651 unsupported API_VERSION", API_VERSION)
    assert ShopifyConnector.capability.api_version == API_VERSION
    assert not fs.bad_versions, ("#651 request used an unsupported API version", fs.bad_versions)
    ccbad = ShopifyConnector(
        {"store_name": "x.evil.com/"}, {"access_token": "t"}, transport=fs.transport()
    )
    raised = False
    try:
        ccbad._endpoint()
    except cbase.ConnectorError:
        raised = True
    assert raised, "gotcha #1: a '/'-bearing store_name never reaches URL construction"
    print(
        "  A/gotcha#1: store handle allowlisted ('/','.','@',unicode,space,empty,ctrl "
        "refused); host is always <handle>.myshopify.com"
    )

    # ---- (2b) taxonomy A: caller resourceId scope + shape (GID) ----
    cc2 = _cc(fs, {"resource_types": "PRODUCT"})
    assert cc2._validate_gid("gid://shopify/Product/9") == "gid://shopify/Product/9"
    for bad_gid in (
        "gid://shopify/Collection/9",  # type not configured
        "gid://shopify/Product/9/../x",  # malformed
        "gid://evil/Product/9",
        "Product/9",
        "9",
        "gid://shopify/Product/x",
        "gid://shopify/Product/9?x=1",
    ):
        raised = False
        try:
            cc2._validate_gid(bad_gid)
        except cbase.ConnectorError:
            raised = True
        assert raised, ("A: out-of-scope / malformed resource id refused", bad_gid)
    # end-to-end: a bad/out-of-scope article_id writes NOTHING
    fs2 = FakeShopify()
    _seed_std(fs2)
    fs2.add_resource("gid://shopify/Collection/5", "COLLECTION", {"title": ("Secret", "STRING")})
    c2 = _conn("scope", {"target_locales": "fr"})
    for bad_id in ("gid://shopify/Collection/5", "gid://shopify/Product/1/../x", "nope"):
        fs2.registers.clear()
        cstore.shopify_translate(
            c2.id, article_ids=[bad_id], actor="attacker", transport=fs2.transport()
        )
        assert not fs2.registers, (bad_id, "no register for an unsafe/out-of-scope id")
    print("  A: caller resource GID scope+shape validated; out-of-scope id writes nothing")

    # ---- (3) taxonomy D: server-driven selection + type/name skips REPORTED ----
    cc3 = _cc(fs)
    rec3 = cc3.discover_articles()[0]
    sel, skipped = cc3._field_selection(rec3["content"])
    assert sel == ["title", "body_html", "meta_title"], ("only text keys selected", sel)
    assert "handle" in skipped and "looks non-translatable" in skipped["handle"], skipped.get(
        "handle"
    )
    assert "spec_json" in skipped and "not translatable text" in skipped["spec_json"], skipped.get(
        "spec_json"
    )
    assert "ext_url" in skipped and "not translatable text" in skipped["ext_url"], skipped.get(
        "ext_url"
    )
    # allowlist BYPASSES the name/type heuristic but only selects keys Shopify marks
    cc3b = _cc(fs, {"fields": "title, handle, spec_json, ghost"})
    selB, skipB = cc3b._field_selection(rec3["content"])
    assert selB == ["title", "handle"], ("allowlist selects the slug but still gates on type", selB)
    assert "spec_json" in skipB and "ghost" in skipB, (
        "JSON refused by type; unknown refused",
        skipB,
    )
    cc3c = _cc(fs, {"exclude_fields": "body_html"})
    selC, _skC = cc3c._field_selection(rec3["content"])
    assert selC == ["title", "meta_title"], ("exclude_fields drops body_html", selC)
    print(
        "  D: server-driven keys; machine types + URL handle skipped+reported; "
        "allowlist/exclude override"
    )

    # ---- (4) happy path pull -> translate -> register (source preserved) ----
    fs4 = FakeShopify()
    rid4 = _seed_std(fs4)
    c4 = _conn("create")
    pulled = ShopifyConnector(c4.config, {"access_token": "t"}, transport=fs4.transport()).pull()
    assert {p.field for p in pulled} == {"title", "body_html", "meta_title"}, pulled
    ok, msg, s4 = cstore.shopify_translate(c4.id, actor="auto", transport=fs4.transport())
    assert ok, msg
    assert s4["created"] == 2 and s4["updated"] == 0, s4  # fr + de
    for loc in ("fr", "de"):
        reg = fs4.translations[rid4][loc]
        assert reg.get("title", "").strip() and reg.get("body_html", "").strip(), (loc, reg)
        assert "handle" not in reg and "spec_json" not in reg, (
            "slug/machine keys not registered",
            reg,
        )
    # the SOURCE content is never overwritten by a register
    assert fs4.resources[rid4]["content"]["title"]["value"] == "Hello world"
    assert "title" in s4["fields_selected"] and "handle" in s4["fields_skipped"], s4
    assert not s4.get("incomplete"), s4.get("incomplete")
    print(
        f"  happy path: pull lists text keys; 2 locales registered on title+body_html+"
        f"meta_title; source untouched; slug/machine skipped ({s4['created']})"
    )

    # ---- (5) taxonomy B: a held-back TITLE never overwrites a stored translation ----
    import app.services.translate as T

    fs5 = FakeShopify()
    rid5 = _seed_std(fs5)
    fs5.seed_translation(rid5, "fr", "title", "Titre humain")
    fs5.seed_translation(rid5, "fr", "body_html", "<p>Vieux</p>")
    c5 = _conn("held-title", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: sg.order_index == 0)  # flip only the title
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s5 = cstore.shopify_translate(c5.id, actor="auto", transport=fs5.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert fs5.translations[rid5]["fr"]["title"] == "Titre humain", (
        "B: held-back title must NOT overwrite the stored translation",
        fs5.translations[rid5]["fr"],
    )
    assert fs5.translations[rid5]["fr"]["body_html"] != "<p>Vieux</p>", "B: body still updates"
    inc5 = s5.get("incomplete") or []
    assert (
        len(inc5) == 1 and inc5[0]["article_id"] == rid5 and inc5[0]["held_fields"] == ["title"]
    ), ("B: held title surfaced as incomplete", inc5)
    assert "INCOMPLETE" in msg, msg
    print(
        "  B: NEEDS_REVIEW title OMITTED (stored fr title preserved); body updates; "
        "incomplete marker surfaced"
    )

    # ---- (6) taxonomy F: all-held-back -> no register, no hash, healthy re-run re-attempts ----
    fs6 = FakeShopify()
    rid6 = _seed_std(fs6)
    fs6.seed_translation(rid6, "fr", "title", "Garde")
    c6 = _conn("all-held", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: True)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s6 = cstore.shopify_translate(c6.id, actor="auto", transport=fs6.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert not fs6.registers, ("F: all held -> NO register", fs6.registers)
    assert fs6.translations[rid6]["fr"]["title"] == "Garde", "existing translation kept"
    assert (
        s6["held_back"] == 1 and s6["created"] == 0 and s6["updated"] == 0 and not s6["failures"]
    ), s6
    ok2, _m2, s62 = cstore.shopify_translate(c6.id, actor="auto", transport=fs6.transport())
    assert ok2 and (s62["created"] + s62["updated"]) == 1 and s62["skipped"] == 0, (
        "F: held-back re-attempted next run (hash not poisoned)",
        s62,
    )
    print("  F: all-held-back -> no register, counted held_back, healthy re-run re-attempts")

    # ---- (7) taxonomy C: the SECOND run suppresses (no re-translate loop) ----
    fs7 = FakeShopify()
    _seed_std(fs7)
    c7 = _conn("steady")
    ok, _m, r7a = cstore.shopify_translate(c7.id, actor="auto", transport=fs7.transport())
    assert ok and r7a["created"] == 2, r7a
    # the probe reads the persisted registered-set (from the stored connection) against the
    # live overlay -> fully covered.
    _probe = cstore.build_fresh(c7.id, fs7.transport())
    assert _probe.strategy.exists_target(_probe, "gid://shopify/Product/1", "fr"), (
        "fr probe finds the full registered set present"
    )
    ok, _m, r7b = cstore.shopify_translate(c7.id, actor="auto", transport=fs7.transport())
    assert ok and r7b["created"] == 0 and r7b["updated"] == 0 and r7b["skipped"] == 2, (
        "C: run 2 suppresses via exists + content-hash (no loop)",
        r7b,
    )
    print("  C: run 2 finds the registered locales + hash match -> 2 skipped (steady state)")

    # ---- (8) GOTCHA #3: an identical-to-source translation is REGISTERED (not skipped) ----
    fs8 = FakeShopify()
    rid8 = (
        fs8.add_resource(
            "gid://shopify/Product/1",
            "PRODUCT",
            {"title": ("Hello", "STRING"), "sku": ("ACME-123", "STRING")},
        )
        or "gid://shopify/Product/1"
    )
    c8 = _conn("identical", {"target_locales": "fr"})
    # force the 'sku' body segment's translation to EQUAL the source (a real no-op MT)
    orig = T.pretranslate_document

    def _identity(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if sg.source_text == "ACME-123":
                sg.target_text = "ACME-123"  # identical to source
                sg.status = SegStatus.MT
                s.add(sg)
        s.commit()
        return out

    set_pieces("app.services.translate", "pretranslate_document", _identity)
    try:
        ok, msg, _s8 = cstore.shopify_translate(c8.id, actor="auto", transport=fs8.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert fs8.translations[rid8]["fr"].get("sku") == "ACME-123", (
        "gotcha #3: an identical-to-source translation is REGISTERED, never silently skipped",
        fs8.translations.get(rid8),
    )
    ok2, _m, s82 = cstore.shopify_translate(c8.id, actor="auto", transport=fs8.transport())
    assert ok2 and s82["skipped"] == 1 and (s82["created"] + s82["updated"]) == 0, (
        "gotcha #3: because it was registered, run 2 is SUPPRESSED (not perpetually re-picked)",
        s82,
    )
    print("  gotcha#3: identical-to-source value REGISTERED (not skipped) -> run 2 suppressed")

    # ---- (9a) HIGH-2: a source edit mid-flight drops ONLY the drifted key (no livelock) ----
    # The reviewer's livelock probe: whole-pair abort on any drift means one flapping field
    # (price/inventory/timestamp) blocks every OTHER key forever. translationsRegister is
    # per-key, so the drifted key is DROPPED and the un-drifted keys still register.
    fs9 = FakeShopify()
    rid9 = _seed_std(fs9)
    c9 = _conn("drift-drop", {"target_locales": "fr"})
    orig = T.pretranslate_document

    def _edit_title(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        # ONLY 'title' changes AFTER materialize (its translation was made from OLD text);
        # body_html + meta_title are stable.
        fs9.resources[rid9]["content"]["title"]["value"] += " v2"
        return out

    set_pieces("app.services.translate", "pretranslate_document", _edit_title)
    try:
        ok, msg, s9 = cstore.shopify_translate(c9.id, actor="auto", transport=fs9.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    reg9 = fs9.translations[rid9]["fr"]
    assert "body_html" in reg9 and "meta_title" in reg9 and "title" not in reg9, (
        "HIGH-2: the drifted 'title' is dropped; the un-drifted keys still register",
        reg9,
    )
    inc9 = s9.get("incomplete") or []
    assert (
        inc9
        and inc9[0]["held_fields"] == ["title"]
        and "source changed" in inc9[0]["reasons"]["title"]
    ), ("HIGH-2: drifted key reported incomplete", inc9)
    # the drifted key's source CHANGED, so the content hash changed -> it auto-returns next
    # run (no operator action), and now (stable) registers.
    ok2, _m, s92 = cstore.shopify_translate(c9.id, actor="auto", transport=fs9.transport())
    assert ok2 and (s92["created"] + s92["updated"]) >= 1, (
        "HIGH-2: drifted key auto-returns next run",
        s92,
    )
    assert fs9.translations[rid9]["fr"]["title"] == "[fr] Hello world v2", (
        "HIGH-2: the title is finally registered from its CURRENT source",
        fs9.translations[rid9]["fr"],
    )
    print(
        "  HIGH-2a: a source edit drops ONLY the drifted key (others register); the drifted "
        "key auto-returns next run via the hash change (no livelock)"
    )

    # ---- (9a2) HIGH-2: ALL keys drifted -> the whole pair aborts (nothing coherent) ----
    fs9x = FakeShopify()
    rid9x = _seed_std(fs9x)
    c9x = _conn("drift-all", {"target_locales": "fr"})

    def _edit_all(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for k in fs9x.resources[rid9x]["content"]:
            fs9x.resources[rid9x]["content"][k]["value"] += " v2"
        return out

    set_pieces("app.services.translate", "pretranslate_document", _edit_all)
    try:
        okx, _msgx, s9x = cstore.shopify_translate(c9x.id, actor="auto", transport=fs9x.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert not okx and not fs9x.registers and s9x["failures"], ("HIGH-2: all-drifted aborts", s9x)
    assert "every key drifted" in s9x["failures"][0]["error"], s9x["failures"]
    print("  HIGH-2a2: all keys drifted -> whole pair aborts (no register), retryable")

    # ---- (9b) HIGH-2: an INVALID_DIGEST race on one key -> per-key drop, rest register ----
    slept: list = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        fs9b = FakeShopify()
        rid9b = _seed_std(fs9b)
        fs9b.invalid_digest_once.add(rid9b)  # first register mutates ONLY 'title' + INVALID_DIGEST
        c9b = _conn("digest-race", {"target_locales": "fr"})
        ok, msg, _s9b = cstore.shopify_translate(c9b.id, actor="auto", transport=fs9b.transport())
        # attempt 1: drift-clean, register -> fake mutates 'title' + INVALID_DIGEST; the retry
        # re-reads, sees title drift, DROPS title, registers body_html + meta_title.
        assert ok, msg
        reg9b = fs9b.translations[rid9b]["fr"]
        assert "body_html" in reg9b and "title" not in reg9b, (
            "HIGH-2: the retry drops the drifted title and registers the rest",
            reg9b,
        )
        assert slept, ("the INVALID_DIGEST retry backs off before re-checking", slept)
    finally:
        cbase._sleep = real_sleep
    print("  HIGH-2b: an INVALID_DIGEST race on one key -> retry drops that key, others register")

    # ---- (9c) GOTCHA #2c: a PERSISTENT INVALID_DIGEST (source UNCHANGED) is retryable-fail ----
    # always_invalid rejects WITHOUT changing the source, so the drift check stays clean and
    # the retries exhaust on a genuine (non-drift) INVALID_DIGEST -> loud, no hash.
    fs9c = FakeShopify()
    rid9c = _seed_std(fs9c)
    fs9c.always_invalid.add(rid9c)
    c9c = _conn("digest-fail", {"target_locales": "fr"})
    _rs = cbase._sleep
    cbase._sleep = lambda s: None
    try:
        ok, msg, s9c = cstore.shopify_translate(c9c.id, actor="auto", transport=fs9c.transport())
    finally:
        cbase._sleep = _rs
    assert not ok and s9c["created"] == 0 and s9c["updated"] == 0, (
        "persistent INVALID_DIGEST -> fail",
        s9c,
    )
    assert s9c["failures"] and "digest mismatch" in s9c["failures"][0]["error"], s9c
    with Session(engine) as s:
        assert s.get(Connection, c9c.id).status == "error"
    fs9c.always_invalid.clear()
    ok2, _m, s9c2 = cstore.shopify_translate(c9c.id, actor="auto", transport=fs9c.transport())
    assert ok2 and s9c2["created"] == 1 and s9c2["skipped"] == 0, (
        "gotcha #2: a failed pair recorded no hash -> re-attempted",
        s9c2,
    )
    print(
        "  gotcha#2c: persistent INVALID_DIGEST (source unchanged) -> loud retryable failure "
        "(no hash) -> re-attempted next run"
    )

    # ---- (9d) HIGH-1: a partial write is re-attemptable (scheduled full-coverage + retry) ----
    fs1 = FakeShopify()
    rid1 = (
        fs1.add_resource(
            "gid://shopify/Product/1",
            "PRODUCT",
            {"title": ("Hello", "STRING"), "body_html": ("<p>Body</p>", "HTML")},
        )
        or "gid://shopify/Product/1"
    )
    # #640: the fr locale already holds a registered translation, so this run is an UPDATE of
    # an existing variant (a FIRST write with a unit held is not made at all: see 9d2 below).
    fs1.seed_translation(rid1, "fr", "body_html", "<p>[fr] old</p>")
    c1h = _conn("high1", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: sg.order_index == 0)  # hold ONLY the title
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s1 = cstore.shopify_translate(c1h.id, actor="auto", transport=fs1.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert (
        "body_html" in fs1.translations[rid1]["fr"] and "title" not in fs1.translations[rid1]["fr"]
    ), (
        "HIGH-1: body registered, held title genuinely absent from the overlay",
        fs1.translations[rid1],
    )
    inc1 = s1.get("incomplete") or []
    assert len(inc1) == 1 and inc1[0]["held_fields"] == ["title"], (
        "HIGH-1: title reported incomplete",
        inc1,
    )
    # #640: a pair written with a unit held back records NO content hash, so a plain HEALTHY
    # scheduled run 2 (the title now approved) re-attempts it and registers the title. (Before
    # #640 the partial write was hash-suppressed and only retry_failed could finish it.)
    ok2, _m, s1b = cstore.shopify_translate(c1h.id, actor="auto", transport=fs1.transport())
    assert ok2 and s1b["skipped"] == 0 and s1b["updated"] == 1, (
        "#640: run 2 re-attempts the partially-written pair (no hash was recorded)",
        s1b,
    )
    assert fs1.translations[rid1]["fr"].get("title") == "[fr] Hello", (
        "#640: run 2 registered the previously held title",
        fs1.translations[rid1]["fr"],
    )
    # retry_failed on the incomplete pair re-attempts via the flow-level only_pairs bypass
    # (suppression is skipped for an explicit operator retry) and now (title healthy)
    # registers the title.
    okr, _mr, s1r = cstore.retry_failed(c1h.id, inc1, actor="auto", transport=fs1.transport())
    assert okr, s1r
    assert fs1.translations[rid1]["fr"].get("title") == "[fr] Hello", (
        "HIGH-1: retry_failed re-attempts and registers the held title",
        fs1.translations[rid1]["fr"],
    )
    print(
        "  HIGH-1/#640: partial write records no hash -> run 2 re-attempts and registers the "
        "held key; retry_failed still works (flow-level only_pairs bypass)"
    )

    # ---- (9d2) #640: a held unit on a FIRST write (no key registered for the locale) ----
    # Shopify falls back to the source text for an unregistered key, so registering only the
    # other fields would leave a mixed-language product page. The pair is held back instead:
    # nothing registered, no hash, reported as held_back; once approved the next run writes it.
    fs640 = FakeShopify()
    rid640 = (
        fs640.add_resource(
            "gid://shopify/Product/640",
            "PRODUCT",
            {"title": ("Hello", "STRING"), "body_html": ("<p>Body</p>", "HTML")},
        )
        or "gid://shopify/Product/640"
    )
    c640 = _conn("held640", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: sg.order_index == 0)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s640 = cstore.shopify_translate(c640.id, actor="auto", transport=fs640.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert not fs640.translations.get(rid640, {}).get("fr"), (
        "#640: nothing registered on a first write with a held unit",
        fs640.translations,
    )
    assert s640["created"] == 0 and s640["updated"] == 0 and s640.get("held_back") == 1, s640
    assert "Held back 1" in msg, msg
    assert not (cstore.get(c640.id).config or {}).get("shopify_hashes"), "#640: no hash recorded"
    ok2, _m, s640b = cstore.shopify_translate(c640.id, actor="auto", transport=fs640.transport())
    assert ok2 and s640b["created"] == 1, ("#640: approved -> created on the next run", s640b)
    assert set(fs640.translations[rid640]["fr"]) == {"title", "body_html"}, fs640.translations
    print("  #640: first write with a held unit registers nothing, records no hash, then completes")

    # ---- (9e) HIGH-3: source_locale can never label primary text or make primary a target ----
    fsH3 = FakeShopify()  # en primary, fr/de
    _seed_std(fsH3)
    cH3 = _conn("srcoverride", {"source_locale": "fr", "target_locales": "de"})  # fr != primary en
    okH3, msgH3, _sH3 = cstore.shopify_translate(cH3.id, actor="auto", transport=fsH3.transport())
    assert not okH3 and "primary" in msgH3.lower(), (
        "HIGH-3: a source_locale that differs from the primary fails loudly",
        msgH3,
    )
    with Session(engine) as s:
        assert s.get(Connection, cH3.id).status == "error"
    # the primary ('en') is NEVER a writable target, even when configured as one
    fsH3b = FakeShopify()
    ridH3 = _seed_std(fsH3b)
    cH3b = _conn("primary-target", {"target_locales": "en, fr"})  # source_locale='en' matches
    okb, _mb, sH3b = cstore.shopify_translate(cH3b.id, actor="auto", transport=fsH3b.transport())
    assert okb and sH3b["locales"] == ["fr"] and sH3b["dropped_source"] == ["en"], (
        "HIGH-3: the primary 'en' is dropped from targets, only 'fr' is usable",
        sH3b,
    )
    assert "en" not in fsH3b.translations.get(ridH3, {}), (
        "HIGH-3: nothing is EVER registered onto the primary locale",
        fsH3b.translations.get(ridH3),
    )
    print("  HIGH-3: source_locale!=primary fails loud; the primary is never a writable target")

    # ---- (9f) MEDIUM-1: one poison key is dropped; the good keys still register ----
    fsM1 = FakeShopify()
    ridM1 = (
        fsM1.add_resource(
            "gid://shopify/Product/1",
            "PRODUCT",
            {"title": ("Hello", "STRING"), "meta_title": ("SEO", "STRING")},
        )
        or "gid://shopify/Product/1"
    )
    fsM1.poison_keys[ridM1] = {"meta_title"}  # Shopify rejects meta_title (non-digest userError)
    cM1 = _conn("poison", {"target_locales": "fr"})
    okM1, msgM1, sM1 = cstore.shopify_translate(cM1.id, actor="auto", transport=fsM1.transport())
    assert okM1, msgM1
    assert fsM1.translations[ridM1]["fr"].get("title") == "[fr] Hello", (
        "MEDIUM-1: the GOOD key still registers despite a poison sibling",
        fsM1.translations[ridM1],
    )
    assert "meta_title" not in fsM1.translations[ridM1]["fr"], (
        "MEDIUM-1: the poison key is not registered"
    )
    incM1 = sM1.get("incomplete") or []
    assert (
        incM1
        and incM1[0]["held_fields"] == ["meta_title"]
        and "rejected by Shopify" in incM1[0]["reasons"]["meta_title"]
    ), ("MEDIUM-1: the poison key is recorded incomplete + operator-visible", incM1)
    print("  MEDIUM-1: a single poison key is dropped (recorded incomplete); good keys still land")

    # ---- (9g) MEDIUM-3: an externally-deleted key's translation is RESTORED next run ----
    fsM3 = FakeShopify()
    ridM3 = _seed_std(fsM3)
    cM3 = _conn("restore", {"target_locales": "fr"})
    ok, _m, sM3a = cstore.shopify_translate(cM3.id, actor="auto", transport=fsM3.transport())
    assert ok and sM3a["created"] == 1, sM3a
    assert "body_html" in fsM3.translations[ridM3]["fr"], sM3a
    # a human DELETES one key's translation in Shopify; the content hash still matches
    del fsM3.translations[ridM3]["fr"]["body_html"]
    ok2, _m, sM3b = cstore.shopify_translate(cM3.id, actor="auto", transport=fsM3.transport())
    assert ok2 and (sM3b["created"] + sM3b["updated"]) == 1 and sM3b["skipped"] == 0, (
        "MEDIUM-3: a deleted registered key is NOT masked by the others -> re-attempt",
        sM3b,
    )
    assert "body_html" in fsM3.translations[ridM3]["fr"], (
        "MEDIUM-3: the deleted key's translation is restored (full-coverage probe)",
        fsM3.translations[ridM3],
    )
    print("  MEDIUM-3: full-coverage probe restores a single externally-deleted key's translation")

    # ---- (9h) M1: _attribute_bad_keys is conservative (the reviewer's three probe shapes) ----
    attr = ShopifyConnector._attribute_bad_keys
    ins = [{"key": "title"}, {"key": "body_html"}]
    assert attr([{"field": ["translations", "1", "value"]}], ins) == {"body_html"}, (
        "valid index attributes"
    )
    assert attr([{"field": ["input", "0", "id"]}], ins) == set(), (
        "M1 probe: a numeric token in a NON-translations path must NOT drop title",
        ins,
    )
    assert attr([{"field": ["translations", "0", "1"]}], ins) == {"title"}, (
        "M1 probe: two numeric tokens -> only the one AFTER 'translations'"
    )
    assert attr([{"field": "translations0value"}], ins) == set(), (
        "M1 probe: a STRING field is unattributable (never iterated char-wise)"
    )
    assert attr([{"field": ["translations", "9", "value"]}], ins) == set(), (
        "out-of-range -> unattributable"
    )
    assert attr([{"field": None, "message": "x"}], ins) == set(), "None field -> unattributable"
    print(
        "  M1: bad-key attribution requires a translations-index; the 3 misattribution "
        "probes (non-translations path / two indices / string field) drop nothing"
    )

    # ---- (9i) M2: one-at-a-time poison rejections use their OWN budget (not digest retries) ----
    fsM2 = FakeShopify()
    keysM2 = {"title": ("Hi", "STRING")}
    for i in range(5):  # 5 poison keys > _MAX_DIGEST_RETRIES(4): the old shared budget failed
        keysM2[f"p{i}"] = (f"v{i}", "STRING")
    ridM2 = (
        fsM2.add_resource("gid://shopify/Product/1", "PRODUCT", keysM2) or "gid://shopify/Product/1"
    )
    fsM2.poison_keys[ridM2] = {f"p{i}" for i in range(5)}
    fsM2.poison_one_at_a_time = True
    cM2 = _conn("poison-drip", {"target_locales": "fr", "fields": "title, p0, p1, p2, p3, p4"})
    okM2, msgM2, sM2 = cstore.shopify_translate(cM2.id, actor="auto", transport=fsM2.transport())
    assert okM2, msgM2
    assert fsM2.translations[ridM2]["fr"].get("title") == "[fr] Hi", (
        "M2: after 5 one-at-a-time drops the good key STILL registers (own budget)",
        fsM2.translations[ridM2],
    )
    incM2 = sM2.get("incomplete") or []
    assert incM2 and len(incM2[0]["held_fields"]) == 5, (
        "M2: all 5 poison keys recorded incomplete",
        incM2,
    )
    # and no misleading INVALID_DIGEST failure (there was never a digest error)
    assert not sM2.get("failures"), ("M2: not a failure — the pair succeeded partially", sM2)
    print(
        "  M2: 5 one-at-a-time poison drops don't exhaust the digest budget; good key lands, "
        "no misleading INVALID_DIGEST tail"
    )

    # ---- (9j) H1: crash (tracking dropped, hash kept) -> next run re-attempts + re-tracks ----
    fsH1 = FakeShopify()
    ridH1 = _seed_std(fsH1)
    cH1c = _conn("crash", {"target_locales": "fr"})
    okc, _mc, sH1a = cstore.shopify_translate(cH1c.id, actor="auto", transport=fsH1.transport())
    assert okc and sH1a["created"] == 1, sH1a
    assert (cstore.get(cH1c.id).config or {}).get("shopify_registered"), "tracking was persisted"
    # simulate a crash that dropped tracking but kept the hash. A bool(existing) fallback
    # would seal this pair 'covered' forever (self-sealing hole); untracked=>not-covered
    # must instead re-register once.
    cstore.patch_config(
        cH1c.id, remove=["shopify_registered"]
    )  # wipe tracking, keep shopify_hashes
    okr, _mr, sH1b = cstore.shopify_translate(cH1c.id, actor="auto", transport=fsH1.transport())
    assert okr and (sH1b["created"] + sH1b["updated"]) == 1 and sH1b["skipped"] == 0, (
        "H1: untracked+hash-present is NOT false-covered -> re-registers once",
        sH1b,
    )
    assert (cstore.get(cH1c.id).config or {}).get("shopify_registered", {}).get(f"{ridH1}:fr"), (
        "H1: the re-register RE-ESTABLISHES tracking",
        cstore.get(cH1c.id).config,
    )
    okr2, _m2, sH1c = cstore.shopify_translate(cH1c.id, actor="auto", transport=fsH1.transport())
    assert okr2 and sH1c["skipped"] == 1, ("H1: with tracking re-established, run 3 skips", sH1c)
    print(
        "  H1: a crash-dropped registered-set (hash kept) re-attempts ONCE + re-establishes "
        "tracking (no permanent false-covered hole)"
    )

    # ---- (10) taxonomy E: shared cap round-robins across resource types (no starvation) ----
    fs10 = FakeShopify()
    for rt in ("PRODUCT", "COLLECTION", "ARTICLE"):
        typ = rt.capitalize()
        for i in range(40):
            fs10.add_resource(f"gid://shopify/{typ}/{i}", rt, {"title": (f"t{i}", "STRING")})
    cc10 = _cc(fs10, {"resource_types": "PRODUCT, COLLECTION, ARTICLE", "max_entries": "50"})
    got = cc10.discover_articles()
    counts: dict = {}
    for r in got:
        counts[r["_rt"]] = counts.get(r["_rt"], 0) + 1
    assert len(got) == 50, ("shared cap fills to 50", len(got))
    assert set(counts) == {"PRODUCT", "COLLECTION", "ARTICLE"} and min(counts.values()) >= 16, (
        "E: round-robin distributes the shared cap; no type starved",
        counts,
    )
    print(f"  E: multi-resource-type cap round-robins fairly (no starvation): {counts}")

    # ---- (11) locale case round-trip on the wire (fr -> registered under fr) ----
    fs11 = FakeShopify(
        locales=[
            {"locale": "en", "primary": True, "published": True},
            {"locale": "pt-BR", "published": True},
        ]
    )
    rid11 = _seed_std(fs11)
    c11 = _conn("case", {"target_locales": "pt-BR"})
    ok, msg, _s11 = cstore.shopify_translate(c11.id, actor="auto", transport=fs11.transport())
    assert ok, msg
    assert "pt-BR" in fs11.translations[rid11], (
        "wire register uses exact 'pt-BR'",
        fs11.translations[rid11],
    )
    print("  locale case: flow lowercases to pt-br; the wire register uses exact pt-BR")

    # ---- (12) enabled-locale rejection + honest source-drop + all-rejected FAIL ----
    fs12 = FakeShopify(
        locales=[
            {"locale": "en", "primary": True, "published": True},
            {"locale": "fr", "published": True},
        ]
    )
    _seed_std(fs12)
    c12 = _conn("reject", {"target_locales": "fr, de, en"})
    ok, msg, s12 = cstore.shopify_translate(c12.id, actor="auto", transport=fs12.transport())
    assert ok and s12["locales"] == ["fr"] and s12["rejected"] == ["de"], s12
    assert s12["dropped_source"] == ["en"], ("source dropped surfaced", s12)
    assert "equal to the source locale" in msg, msg

    fs12b = FakeShopify(locales=[{"locale": "en", "primary": True, "published": True}])
    _seed_std(fs12b)
    c12b = _conn("allreject", {"target_locales": "fr, de"})
    ok, msg, s12b = cstore.shopify_translate(c12b.id, actor="auto", transport=fs12b.transport())
    assert not ok and s12b["locales"] == [], s12b
    with Session(engine) as s:
        assert s.get(Connection, c12b.id).status == "error"
    print("  rejection: 'de' rejected, 'en' dropped-as-source (reported); all-rejected -> FAIL")

    # ---- (13) unpublished-locale note (digest gotcha #6) ----
    fs13 = FakeShopify(
        locales=[
            {"locale": "en", "primary": True, "published": True},
            {"locale": "fr", "published": False},
        ]
    )  # fr enabled but UNPUBLISHED
    rid13 = _seed_std(fs13)
    c13 = _conn("unpub", {"target_locales": "fr"})
    ok, msg, s13 = cstore.shopify_translate(c13.id, actor="auto", transport=fs13.transport())
    assert ok and "fr" in fs13.translations[rid13], (
        "write to an unpublished locale still succeeds",
        s13,
    )
    assert "not published" in msg.lower(), ("operator-visible unpublished-locale note", msg)
    print(
        "  gotcha#6: a write to an enabled-but-unpublished locale succeeds + is noted "
        "(won't show on storefront until published)"
    )

    # ---- (14) WriteBudget ceiling (taxonomy H) ----
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fs14 = FakeShopify()
        _seed_std(fs14)
        c14 = _conn("budget")
        ok, msg, s14 = cstore.shopify_translate(c14.id, actor="auto", transport=fs14.transport())
        assert ok and s14["ceiling_hit"] and (s14["created"] + s14["updated"]) == 1, s14
        assert len(fs14.registers) == 1
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    print("  H: WriteBudget MAX_WRITES_PER_RUN=1 stops after 1 external register")

    # ---- (15) mock-egress guard fires (taxonomy H) ----
    from app.services.connectors import egress_guard

    fs15 = FakeShopify()
    _seed_std(fs15)
    c15 = _conn("egress")
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s15 = cstore.shopify_translate(c15.id, actor="a", transport=fs15.transport())
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fs15.registers, ("mock MT + real egress -> blocked, no register", msg)
    print("  H: egress guard blocks mock MT + real egress, no external register")

    # ---- (16) taxonomy G + cost throttle: the fake enforces THROTTLED backoff ----
    slept16: list = []
    _rs16 = cbase._sleep
    cbase._sleep = lambda s: slept16.append(s)
    try:
        fs16 = FakeShopify()
        _seed_std(fs16)
        fs16.throttle_n = 2  # first two GraphQL calls throttle, then settle
        c16 = _conn("throttle", {"target_locales": "fr"})
        ok, msg, s16 = cstore.shopify_translate(c16.id, actor="auto", transport=fs16.transport())
        assert ok and s16["created"] == 1, ("throttled calls are retried after backoff", s16, msg)
        assert slept16, ("G: a THROTTLED response backs off before retrying", slept16)
        assert max(slept16) <= cbase.MAX_SLEEP, ("throttle wait clamped at MAX_SLEEP", slept16)
    finally:
        cbase._sleep = _rs16
    # G: unknown key + INVALID_DIGEST are real userErrors at the register seam
    fs16b = FakeShopify()
    rid16b = _seed_std(fs16b)
    ccG = _cc(fs16b)
    good_digest = _digest(fs16b.resources[rid16b]["content"]["title"]["value"])
    outG = ccG.register_translations(
        rid16b,
        "fr",
        [{"locale": "fr", "key": "ghost", "value": "x", "translatableContentDigest": good_digest}],
    )
    assert not outG.ok and "unknown key" in outG.message, (
        "G: unknown key -> userError",
        outG.message,
    )
    outD = ccG.register_translations(
        rid16b,
        "fr",
        [{"locale": "fr", "key": "title", "value": "x", "translatableContentDigest": "d:stale"}],
    )
    assert not outD.ok and outD.invalid_digest, (
        "G: a stale digest -> the digest-race code",
        outD.message,
    )
    # #652: the invented INVALID_DIGEST code is NOT a Shopify TranslationErrorCode, so it
    # must not latch the digest-race path (it is an ordinary, unattributable userError).
    _orig_gql = ccG._graphql
    ccG._graphql = lambda q, v=None: {
        "translationsRegister": {
            "userErrors": [{"code": "INVALID_DIGEST", "field": None, "message": "legacy"}]
        }
    }
    outLegacy = ccG.register_translations(
        rid16b,
        "fr",
        [{"locale": "fr", "key": "title", "value": "x", "translatableContentDigest": good_digest}],
    )
    ccG._graphql = _orig_gql
    assert not outLegacy.ok and not outLegacy.invalid_digest, (
        "#652: INVALID_DIGEST is not a real code and must not be treated as a digest race",
        outLegacy.message,
    )
    print("  G: fake enforces THROTTLED backoff (clamped) + digest-race + unknown-key userErrors")

    # ---- (17) content-hash: an edited source re-translates ----
    fs17 = FakeShopify()
    rid17 = _seed_std(fs17)
    c17 = _conn("edit")
    cstore.shopify_translate(c17.id, actor="auto", transport=fs17.transport())
    fs17.resources[rid17]["content"]["title"]["value"] = "Hello EDITED world"
    ok, _m, s17 = cstore.shopify_translate(c17.id, actor="auto", transport=fs17.transport())
    assert ok and s17["updated"] == 2 and s17["skipped"] == 0, (
        "edited source -> re-translate",
        s17,
    )
    print("  suppression: edited source content -> 2 updated")

    # ---- (18) pagination walk across pages + empty resource skipped ----
    fs18 = FakeShopify()
    for i in range(5):
        fs18.add_resource(f"gid://shopify/Product/{i}", "PRODUCT", {"title": (f"T{i}", "STRING")})
    got18 = _cc(fs18, {"max_entries": "3"}).discover_articles()
    assert len(got18) == 3, ("cap honored across paginated fetch", len(got18))
    got18b = _cc(fs18, {"max_entries": "50"}).discover_articles()
    assert len(got18b) == 5, got18b
    # #654: with more resources than max_entries (default 50), the NEWEST are discovered, not
    # the oldest 50: a store of 60 products whose 60th is new must surface the 60th.
    fs18n = FakeShopify()
    for i in range(1, 61):
        fs18n.add_resource(f"gid://shopify/Product/{i}", "PRODUCT", {"title": (f"P{i}", "STRING")})
    got18n = _cc(fs18n).discover_articles()  # default cap 50
    ids18n = [r["resource_id"] for r in got18n]
    assert len(ids18n) == 50, ("#654 default cap still bounds the run", len(ids18n))
    assert "gid://shopify/Product/60" in ids18n, ("#654 newest product must be discovered", ids18n)
    assert ids18n[0] == "gid://shopify/Product/60", ("#654 newest first", ids18n[:3])
    assert "gid://shopify/Product/11" in ids18n and "gid://shopify/Product/10" not in ids18n, (
        "#654 the oldest fall off, not the newest",
        ids18n[-3:],
    )
    assert fs18n.list_reverse_args and all(fs18n.list_reverse_args), fs18n.list_reverse_args
    # raising max_entries reaches everything
    assert len(_cc(fs18n, {"max_entries": "100"}).discover_articles()) == 60
    # a multi-page walk (cap above one page of 100) still follows the cursor
    fs18p = FakeShopify()
    for i in range(1, 251):
        fs18p.add_resource(f"gid://shopify/Product/{i}", "PRODUCT", {"title": (f"P{i}", "STRING")})
    got18p = _cc(fs18p, {"max_entries": "230"}).discover_articles()
    assert len(got18p) == 230 and got18p[0]["resource_id"].endswith("/250"), len(got18p)
    fs18b = FakeShopify()
    fs18b.add_resource(
        "gid://shopify/Product/9",
        "PRODUCT",
        {"title": ("", "STRING"), "body_html": ("   ", "HTML")},
    )
    c18 = _conn("empty")
    ok, msg, s18 = cstore.shopify_translate(
        c18.id, article_ids=["gid://shopify/Product/9"], actor="auto", transport=fs18b.transport()
    )
    assert ok and s18["empty_skipped"] >= 1, s18
    print("  pagination: cursor walk collects to the cap; an empty resource is skipped")

    # ---- (19) parity surfaces ----
    from app.services.connectors import REGISTRY

    assert "shopify" in REGISTRY
    c = TestClient(app)
    assert "Shopify" in c.get("/connectors").text
    typ = c.get("/connectors/shopify").text
    assert "Store name" in typ and "Create" in typ
    from app.cli.__main__ import build_parser

    assert "connector shopify-translate" in _leaf_paths(build_parser())
    from starlette.routing import Route

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/shopify/translate" in api_paths
    # harvest mapper registered
    from app.services.connectors import harvest as _h

    assert "shopify" in _h._HARVESTERS
    print("  parity: REGISTRY + /connectors tile + type form + /api route + CLI leaf + harvest")

    # ---- (20) org isolation: a foreign/missing cid -> 404 on the /api translate route ----
    from tests._api_fixture import user_api_token

    connapi = _conn("api")
    hdr = {"X-Loc-Token": user_api_token()}
    r_ghost = c.post("/api/connectors/99999999/shopify/translate", json={}, headers=hdr)
    assert r_ghost.status_code == 404, ("foreign/missing cid -> 404", r_ghost.status_code)
    r_own = c.post(
        f"/api/connectors/{connapi.id}/shopify/translate", json={"article_ids": []}, headers=hdr
    )
    assert r_own.status_code in (200, 400), ("own connection reachable", r_own.status_code)
    print("  org isolation: foreign/missing cid -> 404; own connection reachable on /api route")

    # ---- (21) CLI shopify-translate subcommand smoke ----
    from app.cli.cmd_connector import _shopify_translate

    ficli = FakeShopify()
    _seed_std(ficli)
    conncli = _conn("cli")
    real_ct = cstore.shopify_translate
    seen: dict = {}

    def _capturing(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        seen["article_ids"] = article_ids
        return real_ct(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=ficli.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "shopify_translate", _capturing)

    class _Args:
        connection_id = conncli.id
        article_ids = "gid://shopify/Product/1"
        locales = ""
        actor = "cli"
        local = True
        org = None
        json = False

    class _Emit:
        def __init__(self):
            self.res = None

        def result(self, msg, data):
            self.res = (msg, data)

    try:
        emit = _Emit()
        rc = _shopify_translate(_Args(), emit)
        assert rc == 0, emit.res
        assert seen.get("article_ids") == ["gid://shopify/Product/1"], seen
        assert emit.res[1]["ok"], emit.res
    finally:
        set_pieces("app.services.connectors.store", "shopify_translate", real_ct)
    print("  CLI: shopify-translate parses --article-ids and runs the flow (OK)")

    # ---- (22) retry_failed re-attempts a failed pair ----
    fs22 = FakeShopify()
    rid22 = _seed_std(fs22)
    fs22.always_invalid.add(rid22)
    c22 = _conn("retry", {"target_locales": "fr"})
    _rs22 = cbase._sleep
    cbase._sleep = lambda s: None
    try:
        ok, _m, s22 = cstore.shopify_translate(c22.id, actor="auto", transport=fs22.transport())
    finally:
        cbase._sleep = _rs22
    assert not ok and s22["failures"], s22
    fs22.always_invalid.clear()
    okr, _mr, s22r = cstore.retry_failed(
        c22.id, s22["failures"], actor="auto", transport=fs22.transport()
    )
    assert okr and "fr" in fs22.translations[rid22], (
        "retry_failed re-attempts the failed pair",
        s22r,
    )
    print("  retry: retry_failed re-registers a previously-failed (resource, locale) pair")

    # ---- (23) M4: CLI `connector retry --from-json` reads the failures+incomplete UNION ----
    # A held/poison key's ONLY auto-visible recovery route is the incomplete list; piping a
    # run summary that has incomplete-but-no-failures must still retry it (parity with /api).
    import json as _json
    import tempfile as _tempfile

    from app.cli.cmd_connector import _retry as _cli_retry

    fsM4 = FakeShopify()
    ridM4 = _seed_std(fsM4)
    # #640: an existing registered translation makes this an UPDATE (a first write with the
    # title held is held back whole, not partially registered).
    fsM4.seed_translation(ridM4, "fr", "body_html", "<p>[fr] old</p>")
    cM4 = _conn("cli-union", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: sg.order_index == 0)  # hold the title -> incomplete
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        _ok, _m, sM4 = cstore.shopify_translate(cM4.id, actor="auto", transport=fsM4.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert sM4.get("incomplete") and not sM4.get("failures"), (
        "M4 setup: a summary with incomplete but no failures",
        sM4,
    )
    seenM4: dict = {}
    real_rf = cstore.retry_failed

    def _capture_rf(cid, failures, actor="", transport=None):
        seenM4["failures"] = failures
        return real_rf(cid, failures, actor=actor, transport=fsM4.transport())

    set_pieces("app.services.connectors.store", "retry_failed", _capture_rf)
    _tmp = _tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")  # noqa: SIM115
    _json.dump({"failures": [], "incomplete": sM4["incomplete"]}, _tmp)
    _tmp.close()

    class _ArgsR:
        connection_id = cM4.id
        from_json = _tmp.name
        actor = "cli"
        local = True
        org = None
        json = False

    class _EmitR:
        def result(self, msg, data):
            self.res = (msg, data)

    try:
        rcR = _cli_retry(_ArgsR(), _EmitR())
        assert rcR == 0, "CLI retry ran"
        # the CLI passed the INCOMPLETE pair through (not silently dropped)
        assert seenM4.get("failures") and any(
            f.get("article_id") == ridM4 for f in seenM4["failures"]
        ), ("M4: CLI retry --from-json includes incomplete pairs (union with /api)", seenM4)
        assert fsM4.translations[ridM4]["fr"].get("title") == "[fr] Hello world", (
            "M4: the incomplete title is re-attempted + registered via the CLI union",
            fsM4.translations[ridM4],
        )
    finally:
        set_pieces("app.services.connectors.store", "retry_failed", real_rf)
        os.unlink(_tmp.name)
    print(
        "  M4: CLI `connector retry --from-json` reads failures+incomplete+blocked (parity "
        "with POST /api .../retry) - incomplete pairs are not silently skipped"
    )

    # #652: every userError code any fake in this suite emitted is a real Shopify
    # TranslationErrorCode (2026-10 list), so the suite cannot defend an invented code.
    emitted = set().union(*(f.emitted_codes for f in ALL_FAKES))
    assert emitted, "no userError codes were emitted at all (vacuous check)"
    assert emitted <= REAL_TRANSLATION_ERROR_CODES, (
        "#652: fake emitted codes that are not real TranslationErrorCode values",
        sorted(emitted - REAL_TRANSLATION_ERROR_CODES),
    )
    assert DIGEST_RACE_CODE in emitted
    print("verify_shopify OK")


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


if __name__ == "__main__":
    main()
