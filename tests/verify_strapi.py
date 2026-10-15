"""Strapi connector + connector-framework seams — fully offline via httpx.MockTransport
(a fake Strapi instance, v5 AND v4, with a draft/published split and a strict `data`
validator).

Covers the original build AND the adversarial-review fix round. Fail-pre-fix proofs
for every finding are the reviewer's scratchpad probes (probe_a/b/c) which encode the
HEAD behavior; the assertions below LOCK IN the fixed behavior as regressions:

  H1  caller article_ids are scope-validated (unknown/`..`/`?` content types + ids are
      refused) end-to-end through store.strapi_translate and at the client seam; L1 the
      v4 write path rejects a query-injecting id too
  H2  a held-back (NEEDS_REVIEW) TITLE does not overwrite an existing translation with
      the English source (omitted on UPDATE; source-seeded only on CREATE)
  H3  type-aware auto-discovery excludes slug/uid/email/url/date/time/enum values and
      reports rich (blocks/json) skips VISIBLY; the slug-as-translatable fixture is gone
  H4  v5 reads/probes pin status=draft so a draft-only variant is found (no re-translate
      loop); the fake models draft-vs-published and a strict `data` validator
  M1  UPDATE writes ONLY the fields translated this run (no stale all-field re-send, no
      relations in the body); M3 there is no silent read-current at all
  M2  a real content field named `draft` survives (the strategy's bool sentinel is
      stripped without eating it)
  L2  multi-content-type cap is a shared budget (no floor division); L3 all-source-equal
      targets are reported honestly; L4 a content type containing ':' is rejected

Plus: content-hash suppression, locale-case round-trip, enabled-locale rejection,
WriteBudget ceiling, pagination, empty skip, v4 create/update, egress guard, parity.

ASCII-only prints; final line is 'verify_strapi OK'.
"""

import json
import math
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_strapi_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import Connection, Segment, SegStatus
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.strapi_part2 import StrapiConnector
from app.services.connectors.strategy import ABSENT

_SYS = {"id", "documentId", "locale", "localizations", "createdAt", "updatedAt", "publishedAt"}


# ============================================================================
# Fake Strapi. Models the v5 DRAFT vs PUBLISHED split (a write lands on the draft;
# a read must pass status=draft to see it) and enforces Strapi's strict `data`
# validator (an unknown / system key in a write body -> 400), plus v4 localizations.
# ============================================================================
class FakeStrapi:
    def __init__(self, version="v5", locales=None, page_size=100, schema=None):
        self.version = version
        self.locales = locales or [
            {"code": "en", "name": "English (en)", "isDefault": True},
            {"code": "fr", "name": "French (fr)"},
            {"code": "de", "name": "German (de)"},
        ]
        self.page_size = page_size
        self.schema = schema  # {ct: set(writable field names)} or None (permissive)
        self._nid = 1000
        # v5: draft[ct][docid][locale] = fields ; published[ct][docid][locale] = fields
        self.draft: dict = {}
        self.published: dict = {}
        # v4: v4docs[ct][id] = {"locale":.., "fields":{..}, "localizations": {locale: id}}
        self.v4docs: dict = {}
        self.creates: list = []  # (ct, ident, locale, data)
        self.updates: list = []  # (ct, ident, locale, data)
        # content-type-builder schema (#638): None -> answers 403 (token without access)
        self.builder: list | None = None
        self.builder_gets = 0

    # ---- seeding (v5) ------------------------------------------------------
    def add_v5(self, ct, docid, locale, fields, updatedAt="2026-07-05T10:00:00Z", published=True):
        f = dict(fields)
        f["updatedAt"] = updatedAt
        self.draft.setdefault(ct, {}).setdefault(docid, {})[locale] = dict(f)
        if published:
            self.published.setdefault(ct, {}).setdefault(docid, {})[locale] = dict(f)

    def draft_of(self, ct, docid, locale):
        return self.draft.get(ct, {}).get(docid, {}).get(locale)

    def published_of(self, ct, docid, locale):
        return self.published.get(ct, {}).get(docid, {}).get(locale)

    # ---- seeding (v4) ------------------------------------------------------
    def _next_id(self):
        self._nid += 1
        return self._nid

    def add_v4_master(self, ct, mid, locale, fields, updatedAt="2026-07-05T10:00:00Z"):
        self.v4docs.setdefault(ct, {})[mid] = {
            "locale": locale,
            "fields": {**fields, "updatedAt": updatedAt},
            "localizations": {},
        }

    def add_v4_localization(
        self, ct, master_id, loc_id, locale, fields, updatedAt="2026-07-05T10:00:00Z"
    ):
        self.v4docs.setdefault(ct, {})[loc_id] = {
            "locale": locale,
            "fields": {**fields, "updatedAt": updatedAt},
            "localizations": {},
        }
        self.v4docs[ct][master_id]["localizations"][locale] = loc_id

    def transport(self):
        return httpx.MockTransport(self._handle)

    # ---- strict data validator --------------------------------------------
    def _reject_data(self, ct, data):
        if self.schema is None:
            return None
        allowed = self.schema.get(ct, set())
        bad = [k for k in data if k in _SYS or k not in allowed]
        if bad:
            return httpx.Response(
                400,
                json={"error": {"name": "ValidationError", "message": f"Invalid key(s): {bad}"}},
            )
        return None

    def _flat5(self, ct, docid, locale, store):
        f = store[ct][docid][locale]
        return {
            "id": abs(hash((docid, locale))) % 100000,
            "documentId": docid,
            "locale": locale,
            "publishedAt": None,
            **f,
        }

    def _handle(self, req):
        path = req.url.path
        if not path.startswith("/api/"):
            return httpx.Response(404, json={"error": {"message": "not /api"}})
        rest = path[len("/api") :]
        method = req.method
        params = req.url.params
        if method == "GET" and rest == "/i18n/locales":
            return httpx.Response(200, json=self.locales)
        if method == "GET" and rest == "/content-type-builder/content-types":
            self.builder_gets += 1
            if self.builder is None:
                return httpx.Response(403, json={"error": {"message": "Forbidden"}})
            return httpx.Response(200, json={"data": self.builder})
        return (
            self._v5(method, rest, params, req)
            if self.version == "v5"
            else self._v4(method, rest, params, req)
        )

    # ---- v5 ----------------------------------------------------------------
    def _v5(self, method, rest, params, req):
        parts = [p for p in rest.split("/") if p]
        ct = parts[0] if parts else ""
        locale = params.get("locale")
        status = params.get("status") or "published"  # Strapi default read = published
        store = self.draft if status == "draft" else self.published

        if method == "GET" and len(parts) == 1:
            page = int(params.get("pagination[page]", "1"))
            variants = []
            for docid, locs in store.get(ct, {}).items():
                keys = [locale] if (locale and locale != "*") else list(locs)
                for lc in keys:
                    if lc in locs:
                        variants.append(self._flat5(ct, docid, lc, store))
            variants.sort(key=lambda e: e.get("updatedAt") or "", reverse=True)
            total = len(variants)
            page_count = max(1, math.ceil(total / self.page_size))
            start = (page - 1) * self.page_size
            data = variants[start : start + self.page_size]
            return httpx.Response(
                200,
                json={
                    "data": data,
                    "meta": {
                        "pagination": {
                            "page": page,
                            "pageSize": self.page_size,
                            "pageCount": page_count,
                            "total": total,
                        }
                    },
                },
            )

        if len(parts) == 2:
            ct, docid = parts
            locs = store.get(ct, {}).get(docid)
            if method == "GET":
                if locs is None or (locale and locale not in locs):
                    return httpx.Response(404, json={"data": None})
                return httpx.Response(200, json={"data": self._flat5(ct, docid, locale, store)})
            if method == "PUT":
                data = json.loads(req.content or b"{}").get("data") or {}
                bad = self._reject_data(ct, data)
                if bad is not None:
                    return bad
                existed = self.draft.get(ct, {}).get(docid, {}).get(locale) is not None
                self.draft.setdefault(ct, {}).setdefault(docid, {}).setdefault(locale, {})
                self.draft[ct][docid][locale].update(data)
                self.draft[ct][docid][locale].setdefault("updatedAt", "2026-07-09T00:00:00Z")
                (self.updates if existed else self.creates).append((ct, docid, locale, data))
                return httpx.Response(
                    200 if existed else 201,
                    json={"data": self._flat5(ct, docid, locale, self.draft)},
                )
        return httpx.Response(
            404,
            json={"error": {"message": f"v5 unexpected {method} {rest}"}},
        )

    # ---- v4 ----------------------------------------------------------------
    def _v4_item(self, ct, nid, populate=False):
        row = self.v4docs[ct][nid]
        attrs = {"locale": row["locale"], **row["fields"]}
        if populate:
            attrs["localizations"] = {
                "data": [
                    {"id": lid, "attributes": {"locale": lc}}
                    for lc, lid in row["localizations"].items()
                ]
            }
        return {"id": nid, "attributes": attrs}

    def _v4(self, method, rest, params, req):
        parts = [p for p in rest.split("/") if p]
        ct = parts[0] if parts else ""
        locale = params.get("locale")
        if method == "GET" and len(parts) == 1:
            page = int(params.get("pagination[page]", "1"))
            items = [
                self._v4_item(ct, nid)
                for nid, row in self.v4docs.get(ct, {}).items()
                if not locale or row["locale"] == locale
            ]
            items.sort(key=lambda e: e["attributes"].get("updatedAt") or "", reverse=True)
            total = len(items)
            page_count = max(1, math.ceil(total / self.page_size))
            start = (page - 1) * self.page_size
            return httpx.Response(
                200,
                json={
                    "data": items[start : start + self.page_size],
                    "meta": {"pagination": {"page": page, "pageCount": page_count, "total": total}},
                },
            )
        if method == "POST" and len(parts) == 3 and parts[2] == "localizations":
            ct = parts[0]
            try:
                mid = int(parts[1])
            except ValueError:
                return httpx.Response(404, json={"error": {"message": "bad master id"}})
            body = json.loads(req.content or b"{}")
            loc = body.get("locale")
            fields = {k: v for k, v in body.items() if k != "locale"}
            bad = self._reject_data(ct, fields)
            if bad is not None:
                return bad
            new_id = self._next_id()
            self.add_v4_localization(ct, mid, new_id, loc, fields)
            self.creates.append((ct, mid, loc, fields))
            return httpx.Response(200, json={"data": self._v4_item(ct, new_id)})
        if len(parts) == 2:
            ct, nid_s = parts
            try:
                nid = int(nid_s)
            except ValueError:
                return httpx.Response(404, json={"error": {"message": "bad id"}})
            row = self.v4docs.get(ct, {}).get(nid)
            if method == "GET":
                if row is None:
                    return httpx.Response(404, json={"data": None})
                populate = any(k.startswith("populate[localizations]") for k in params.keys())  # noqa: SIM118
                return httpx.Response(200, json={"data": self._v4_item(ct, nid, populate)})
            if method == "PUT":
                if row is None:
                    return httpx.Response(404, json={"data": None})
                data = json.loads(req.content or b"{}").get("data") or {}
                bad = self._reject_data(ct, data)
                if bad is not None:
                    return bad
                row["fields"].update(data)
                self.updates.append((ct, nid, row["locale"], data))
                return httpx.Response(200, json={"data": self._v4_item(ct, nid)})
        return httpx.Response(
            404,
            json={"error": {"message": f"v4 unexpected {method} {rest}"}},
        )


def _conn(name, cfg_extra=None, version="v5"):
    cfg = {
        "base_url": "http://strapi.test",
        "content_types": "articles",
        "source_locale": "en",
        "target_locales": "fr, de",
        "strapi_version": version,
    }
    cfg.update(cfg_extra or {})
    return cstore.create("strapi", name, cfg, {"api_token": "tok"}, actor="admin")


def _sc(cfg, transport, version="v5"):
    base = {
        "base_url": "http://strapi.test",
        "content_types": "articles",
        "source_locale": "en",
        "strapi_version": version,
    }
    base.update(cfg or {})
    return StrapiConnector(base, {"api_token": "tok"}, transport=transport)


def _all_needs_review(mod):
    """A pretranslate_document wrapper flagging every produced segment NEEDS_REVIEW
    (simulates a refusal / QE hold-back of the WHOLE entry)."""
    orig = mod.pretranslate_document

    def patched(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            sg.status = SegStatus.NEEDS_REVIEW
            s.add(sg)
        s.commit()
        return out

    return orig, patched


def _page_schema(localized_flags, type_localized=True, plural="articles"):
    """A content-type-builder list entry for `plural` (Strapi 5 shape)."""
    attrs = {
        n: {"type": "string", "pluginOptions": {"i18n": {"localized": v}}}
        for n, v in localized_flags.items()
    }
    return {
        "uid": f"api::{plural[:-1]}.{plural[:-1]}",
        "apiID": plural[:-1],
        "schema": {
            "kind": "collectionType",
            "singularName": plural[:-1],
            "pluralName": plural,
            "pluginOptions": {"i18n": {"localized": type_localized}},
            "attributes": attrs,
        },
    }


def _body_needs_review(mod):
    """Like _all_needs_review, but flags only the BODY segments (order_index >= 1): the
    title translates fine and one field stays held (the #640 partial-hold shape)."""
    orig = mod.pretranslate_document

    def patched(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if sg.order_index >= 1:
                sg.status = SegStatus.NEEDS_REVIEW
                s.add(sg)
        s.commit()
        return out

    return orig, patched


def main() -> None:
    from app.web._routes import walk_routes

    init_db()

    # ---- (1) capability + test + case-preserving resolve + status=draft reads ----
    assert StrapiConnector.capability.topology == "T2"
    fs = FakeStrapi(locales=[{"code": "en", "isDefault": True}, {"code": "fr"}, {"code": "pt-BR"}])
    sc = _sc({}, fs.transport())
    ok, msg = sc.test()
    assert ok, msg
    assert set(sc.enabled_locales()) == {"en", "fr", "pt-br"}
    assert sc._resolve_locale("pt-br") == "pt-BR"
    assert sc._read_params("fr") == {"locale": "fr", "status": "draft"}, sc._read_params("fr")
    print("  connector: T2, test(), case-preserving resolve, v5 reads pin status=draft (H4)")

    # ---- (2) base_url normalization + SSRF guard --------------------------------
    assert (
        _sc({"base_url": "http://strapi.test/api"}, fs.transport())._base()
        == "http://strapi.test/api"
    )
    assert (
        _sc({"base_url": "http://strapi.example:1337"}, fs.transport())._base()
        == "http://strapi.example:1337/api"
    )
    # P-F1: the shared SSRF guard now refuses the internal-name conventions too (a
    # multi-tenant host can't reach a tenant's loopback/internal box anyway), alongside
    # the metadata IP. `localhost` is refused by literal (no lookup needed).
    for bad in ("http://169.254.169.254", "http://localhost:1337", "http://2130706433"):
        raised = False
        try:
            _sc({"base_url": bad}, fs.transport())._base()
        except cbase.ConnectorError:
            raised = True
        assert raised, f"SSRF target must be blocked: {bad}"
    print("  base_url: /api normalized; localhost/metadata/numeric-loopback blocked (P-F1)")

    # ---- (3) H3: type-aware auto-discovery + VISIBLE skips -----------------------
    entry = {
        "id": 12,
        "documentId": "abc",
        "locale": "en",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "title": "Hello world",
        "body": "# Heading\n\nProse.",
        "slug": "hello-world",
        "status": "published",
        "contactEmail": "s@acme.com",
        "eventDate": "2026-03-04",
        "startTime": "09:30:00",
        "publishOn": "2026-03-04T09:30:00Z",
        "externalUrl": "https://acme.com/x",
        "content": [{"type": "paragraph"}],
        "meta": {"seo": {"d": "x"}},
        "views": 42,
    }
    sc3 = _sc({}, fs.transport())
    sel, skipped = sc3._field_selection(entry)
    assert sel == ["title", "body"], ("only real text fields selected", sel)
    for bad in (
        "slug",
        "status",
        "contactEmail",
        "eventDate",
        "startTime",
        "publishOn",
        "externalUrl",
    ):
        assert bad not in sel and bad in skipped, (bad, "must be excluded + reported")
    assert "content" in skipped and "rich content" in skipped["content"], skipped.get("content")
    assert "meta" in skipped and "rich content" in skipped["meta"], skipped.get("meta")
    print("  H3: keeps title+body; excludes slug/enum/email/date/url; reports blocks/json VISIBLY")

    scA = _sc({"fields": "title, slug"}, fs.transport())
    selA, _sk = scA._field_selection(entry)
    assert selA == ["title", "slug"], ("allowlist wins (operator override)", selA)
    print("  H3: `fields` allowlist overrides the heuristics")

    # N6: an allowlisted SYSTEM field is refused (reported), never written (else 400)
    selN6, skipN6 = _sc(
        {"fields": "title, updatedAt, documentId"}, fs.transport()
    )._field_selection(entry)
    assert selN6 == ["title"] and "updatedAt" in skipN6 and "documentId" in skipN6, (selN6, skipN6)
    print("  N6: allowlisted system fields (updatedAt/documentId) refused + reported, not written")

    # N2: realistic translatable copy is SELECTED (heuristics not over-eager)
    realistic = {
        "guide": "A getting started guide",
        "guidelines": "Community guidelines",
        "guidance": "Helpful guidance",
        "liquidCopy": "Pour it on",
        "fluidText": "Flows nicely",
        "emailSubject": "Welcome aboard!",
        "emailBody": "Thanks for joining.",
        "ctaLabel": "sign-up",
        "tagline": "made-to-order excellence",
        "philosophy": "state-of-the-art",
        "term": "internationalization",
        "heading": "Big News",
        "subheading": "Read all about it",
        "buttonText": "Buy now",
    }
    selR, skipR = _sc({}, fs.transport())._field_selection(realistic)
    missed = [k for k in realistic if k not in selR]
    assert not missed, ("realistic CMS copy must be SELECTED, not skipped (N2)", missed, skipR)
    mach = {
        "slug": "hello-world",
        "metaSlug": "x-y",
        "productUid": "u-1",
        "email": "a@b.com",
        "eventDate": "2026-01-01",
        "homepageUrl": "https://x.io",
        "documentId": "abc",
        # N2 swing-back: slug/uid-ROLE names whose value can change a URL / clash on unique
        "path": "about-us",
        "alias": "my-page",
        "identifier": "ref-000-1",
        "reference": "ref-000-1",
        "ref": "abc123def456",
    }
    selM, _skM = _sc({}, fs.transport())._field_selection(mach)
    assert selM == [], ("machine/id/role/format fields still excluded (incl path/alias/ref)", selM)
    print(
        "  N2: realistic copy SELECTED; slug/uid/email/url/date + path/alias/identifier/"
        "reference/ref still skipped"
    )

    # ---- (4) M1: UPDATE writes ONLY translated fields (no stale re-send/relation) ----
    fs4 = FakeStrapi()
    fs4.add_v5("articles", "d1", "en", {"title": "Hi", "body": "B", "summary": "S"})
    fs4.add_v5(
        "articles",
        "d1",
        "fr",
        {"title": "Bonjour(h)", "body": "Corps(h)", "summary": "Resume(h)", "author": {"id": 7}},
    )
    sc4 = _sc({}, fs4.transport())
    res = sc4.update_translation("articles:d1", "fr", {"title": "NouveauTitre", "draft": True})
    assert res.ok and not res.created, res
    assert fs4.updates and fs4.updates[0][3] == {"title": "NouveauTitre"}, (
        "only the translated field is written; no stale body/summary/relation",
        fs4.updates,
    )
    print("  M1: UPDATE writes ONLY the translated field(s) (no all-field re-send, no relation)")

    # ---- (5) M3: UPDATE performs NO read-current GET (no silent-swallow read) ----
    gets = {"n": 0}

    def h_count(req):
        if req.url.path == "/api/i18n/locales":
            return httpx.Response(200, json=[{"code": "en", "isDefault": True}, {"code": "fr"}])
        if req.method == "GET":
            gets["n"] += 1
            return httpx.Response(403, text="must not be read")
        return httpx.Response(200, json={"data": {"documentId": "d1", "locale": "fr"}})

    sc5 = _sc({}, httpx.MockTransport(h_count))
    r5 = sc5.update_translation("articles:d1", "fr", {"title": "T"})
    assert r5.ok and gets["n"] == 0, ("v5 UPDATE must not read-current (M1/M3)", gets, r5)
    print("  M3: v5 UPDATE does no read-current GET (removes the silent-swallow path)")

    # ---- (6) M2: a real content field named 'draft' survives ---------------------
    fs6 = FakeStrapi(schema={"articles": {"title", "draft"}})  # strict: only these keys
    fs6.add_v5("articles", "d1", "en", {"title": "Hi", "draft": "note"})
    fs6.add_v5("articles", "d1", "fr", {"title": "Salut", "draft": "vieux"})
    sc6 = _sc({}, fs6.transport())
    r6 = sc6.update_translation("articles:d1", "fr", {"title": "T", "draft": "traduit"})
    assert r6.ok, ("strict validator accepted the write (no leaked sentinel)", r6)
    assert fs6.updates[-1][3] == {"title": "T", "draft": "traduit"}, (
        "the real 'draft' field survives; the bool sentinel is gone",
        fs6.updates,
    )
    print("  M2: a real content field named 'draft' survives (strategy bool sentinel stripped)")

    # ---- (7) H2 + N3: held-back content preserves the translation, records NO hash --
    import app.services.translate as T

    fs7 = FakeStrapi()
    fs7.add_v5("articles", "d1", "en", {"title": "Hello", "body": "World"})
    fs7.add_v5(
        "articles",
        "d1",
        "fr",
        {"title": "Bonjour(humain)", "body": "Corps(humain)"},
        published=False,
    )
    c7 = _conn("H2 title", {"target_locales": "fr"})
    orig, patched = _all_needs_review(T)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s7 = cstore.strapi_translate(c7.id, actor="auto", transport=fs7.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert not fs7.updates and not fs7.creates, (
        "all fields held back -> NO write",
        fs7.updates,
        fs7.creates,
    )
    assert fs7.draft_of("articles", "d1", "fr")["title"] == "Bonjour(humain)", (
        "H2: existing translated title survives; NOT overwritten with the source",
        fs7.draft_of("articles", "d1", "fr"),
    )
    # N3: it is a HELD_BACK, not an 'updated' write, and NOT a reported failure.
    assert (
        s7["held_back"] == 1 and s7["updated"] == 0 and s7["created"] == 0 and not s7["failures"]
    ), ("N3: held-back is its own counter, not counted as updated, not a failure", s7)
    # N3 core: the run recorded NO suppression hash, so a later HEALTHY run (unchanged
    # source) RE-ATTEMPTS instead of suppressing the held-back content forever.
    ok2, _msg2, s72 = cstore.strapi_translate(c7.id, actor="auto", transport=fs7.transport())
    assert ok2 and s72["updated"] == 1 and s72["skipped"] == 0, (
        "N3: held-back content is re-attempted next run (hash was NOT poisoned)",
        s72,
    )
    assert fs7.updates, "run 2 actually wrote the translation"
    print(
        "  H2/N3: NEEDS_REVIEW held back -> translation preserved, counted as held_back not "
        "updated, no hash poisoning (healthy re-run re-attempts)"
    )

    # ---- (8) H1: caller article_ids are scope-validated end-to-end ---------------
    fs8 = FakeStrapi()
    fs8.add_v5("articles", "d1", "en", {"title": "Hi"})
    c8 = _conn("H1 scope", {"target_locales": "fr"})
    for bad_id in ["employee-salaries:99", "../../admin/users:1", "articles:1?populate=*"]:
        fs8.creates.clear()
        fs8.updates.clear()
        ok, msg, _s8 = cstore.strapi_translate(
            c8.id, article_ids=[bad_id], actor="attacker", transport=fs8.transport()
        )
        assert not ok, (bad_id, "must be refused", msg)
        assert not fs8.creates and not fs8.updates, (bad_id, "NO external write", fs8.creates)
    # client-seam refusals
    scH1 = _sc({"content_types": "articles"}, fs8.transport())
    for rid, viaresolve in [
        ("employee-salaries:1", True),
        ("articles:1?x=1", False),
        ("articles:../x", False),
    ]:
        r = False
        try:
            if viaresolve:
                scH1._resolve_explicit(rid, ["articles"])
            else:
                scH1._split_aid(rid)
        except cbase.ConnectorError:
            r = True
        assert r, (rid, "must raise at the client seam")
    print(
        "  H1: unknown/`..`/`?`-bearing article_ids refused end-to-end + at the client seam, no writes"  # noqa: E501
    )

    # ---- (9) L1: v4 write path rejects a query-injecting id ----------------------
    scv4 = _sc({"strapi_version": "v4"}, FakeStrapi(version="v4").transport(), version="v4")
    raised = False
    try:
        scv4.create_translation("articles:1?publicationState=live&x=", "fr", {"title": "T"})
    except cbase.ConnectorError:
        raised = True
    assert raised, "v4 create must reject an id containing '?'"
    print("  L1: v4 create_translation rejects a query-injecting entry id")

    # ---- (10) H4: status=draft finds a draft-only variant (no re-translate loop) --
    fs10 = FakeStrapi()
    fs10.add_v5("articles", "d1", "en", {"title": "Hello", "body": "World"})
    c10 = _conn("H4 loop", {"target_locales": "fr"})
    ok, msg, r1 = cstore.strapi_translate(c10.id, actor="auto", transport=fs10.transport())
    assert ok and r1["created"] == 1, ("run 1 creates the fr draft", r1)
    assert fs10.draft_of("articles", "d1", "fr") is not None
    assert fs10.published_of("articles", "d1", "fr") is None, "created variant is draft-only"
    assert _sc({}, fs10.transport()).get_translation("articles:d1", "fr") is not ABSENT, (
        "status=draft probe must find the draft-only variant"
    )
    ok, msg, r2 = cstore.strapi_translate(c10.id, actor="auto", transport=fs10.transport())
    assert ok and r2["created"] == 0 and r2["skipped"] == 1, (
        "run 2 suppresses (no re-translate loop) because the draft probe found it",
        r2,
    )
    print("  H4: draft-only variant found via status=draft -> run 2 suppresses (no re-write loop)")

    # ---- (11) full flow CREATE + draft strip + strict validator ------------------
    fs11 = FakeStrapi(schema={"articles": {"title", "body"}})  # strict: reject any stray key
    fs11.add_v5("articles", "d1", "en", {"title": "Hello", "body": "World"})
    c11 = _conn("create strict")
    ok, msg, s11 = cstore.strapi_translate(c11.id, actor="auto", transport=fs11.transport())
    assert ok, msg
    assert s11["created"] == 2, s11
    for _ct, _id, _loc, data in fs11.creates:
        assert "draft" not in data and set(data) <= {"title", "body"}, (
            "strict validator would 400 a leaked sentinel; none leaked",
            data,
        )
    assert "title" in s11["fields_selected"], (
        "selection reported in result",
        s11["fields_selected"],
    )
    print(
        "  flow CREATE: 2 draft variants; strict `data` validator passes (no sentinel leak); "
        "selection reported"
    )

    # ---- (12) content-hash suppression (changed -> re-translate) -----------------
    fs12 = FakeStrapi(schema={"articles": {"title", "body"}})
    fs12.add_v5("articles", "d1", "en", {"title": "Hello EDITED", "body": "World"})
    fs12.add_v5("articles", "d1", "fr", {"title": "x"}, published=False)
    fs12.add_v5("articles", "d1", "de", {"title": "y"}, published=False)
    ok, msg, s12 = cstore.strapi_translate(c11.id, actor="auto", transport=fs12.transport())
    assert ok and s12["updated"] == 2 and s12["skipped"] == 0, (
        "edited source -> re-translate",
        s12,
    )
    print("  suppression: edited source content -> 2 updated")

    # ---- (13) locale case round-trip on the wire (pt-BR) -------------------------
    fs13 = FakeStrapi(locales=[{"code": "en", "isDefault": True}, {"code": "pt-BR"}])
    fs13.add_v5("articles", "d1", "en", {"title": "Hello"})
    c13 = _conn("case", {"target_locales": "pt-BR"})
    ok, msg, _s13 = cstore.strapi_translate(c13.id, actor="auto", transport=fs13.transport())
    assert ok and {loc for _c, _i, loc, _d in fs13.creates} == {"pt-BR"}, fs13.creates
    print("  locale case: flow lowercases to pt-br; the wire write uses exact pt-BR")

    # ---- (14) enabled-locale rejection + L3 honest source-drop message -----------
    fs14 = FakeStrapi(locales=[{"code": "en", "isDefault": True}, {"code": "fr"}])
    fs14.add_v5("articles", "d1", "en", {"title": "Hello"})
    c14 = _conn("reject", {"target_locales": "fr, de, en"})
    ok, msg, s14 = cstore.strapi_translate(c14.id, actor="auto", transport=fs14.transport())
    assert ok and s14["locales"] == ["fr"] and s14["rejected"] == ["de"], s14
    assert s14["dropped_source"] == ["en"], ("source dropped surfaced in result (L3)", s14)
    assert "equal to the source locale" in msg, ("honest source-drop message (L3)", msg)
    print("  rejection: 'de' rejected, 'en' dropped-as-source and REPORTED honestly (L3)")

    fs14b = FakeStrapi(locales=[{"code": "en", "isDefault": True}])
    fs14b.add_v5("articles", "d1", "en", {"title": "Hello"})
    c14b = _conn("allreject", {"target_locales": "fr, de"})
    ok, msg, s14b = cstore.strapi_translate(c14b.id, actor="auto", transport=fs14b.transport())
    assert not ok and s14b["locales"] == [], s14b
    with Session(engine) as s:
        assert s.get(Connection, c14b.id).status == "error"
    print("  rejection: all targets unenabled -> ok=False + status error")

    # ---- (15) WriteBudget ceiling ------------------------------------------------
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fs15 = FakeStrapi()
        fs15.add_v5("articles", "d1", "en", {"title": "Hello"})
        c15 = _conn("budget")
        ok, msg, s15 = cstore.strapi_translate(c15.id, actor="auto", transport=fs15.transport())
        assert ok and s15["ceiling_hit"] and (s15["created"] + s15["updated"]) == 1, s15
        assert len(fs15.creates) == 1
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    print("  WriteBudget: MAX_WRITES_PER_RUN=1 stops after 1 external write")

    # ---- (16) L2: multi-content-type cap is a SHARED budget (no floor division) --
    fs16 = FakeStrapi()
    for ct in ("posts", "pages", "guides"):
        for i in range(40):
            fs16.add_v5(
                ct,
                f"{ct}-{i}",
                "en",
                {"title": f"t{i}"},
                updatedAt=f"2026-07-{(i % 28) + 1:02d}T00:00:00Z",
            )
    sc16 = _sc({"content_types": "posts, pages, guides", "max_entries": "50"}, fs16.transport())
    got = sc16.discover_articles()
    counts = {}
    for e in got:
        counts[e["_ct"]] = counts.get(e["_ct"], 0) + 1
    assert len(got) == 50, ("shared cap fills to 50", len(got))
    assert set(counts) == {"posts", "pages", "guides"} and min(counts.values()) >= 16, (
        "N1: round-robin distributes the shared cap; NO type starved (was Counter({'posts':50}))",
        counts,
    )
    print(f"  L2/N1: multi-content-type cap round-robins fairly (no starvation): {counts}")

    # ---- (17) pagination walk ----------------------------------------------------
    fs17 = FakeStrapi(page_size=2)
    for i in range(5):
        fs17.add_v5(
            "articles", f"p{i}", "en", {"title": f"T{i}"}, updatedAt=f"2026-07-0{i + 1}T00:00:00Z"
        )
    got17 = _sc({"max_entries": "50"}, fs17.transport()).discover_articles()
    assert sorted(e["documentId"] for e in got17) == [f"p{i}" for i in range(5)], got17
    print("  pagination: pageCount walk collected all 5 across 3 pages")

    # ---- (18) empty entry skipped ------------------------------------------------
    fs18 = FakeStrapi()
    fs18.add_v5("articles", "d2", "en", {})
    fs18.add_v5("articles", "d1", "en", {"title": "Has text"})
    c18 = _conn("empty")
    ok, msg, s18 = cstore.strapi_translate(c18.id, actor="auto", transport=fs18.transport())
    assert ok and s18["empty_skipped"] >= 1, s18
    print("  empty entry: an entry with no translatable fields is skipped")

    # ---- (19) L4: a content type containing ':' is rejected ----------------------
    raised = False
    try:
        _sc({"content_types": "post, post:x"}, fs.transport())._content_types()
    except cbase.ConnectorError:
        raised = True
    assert raised, "a content type containing ':' must be rejected (hash-namespace collision)"
    print("  L4: content type containing ':' rejected (record-id/hash namespace safe)")

    # ---- (20) v4 create localization + update by loc id --------------------------
    fs20 = FakeStrapi(
        version="v4", locales=[{"code": "en", "isDefault": True}, {"code": "fr"}, {"code": "de"}]
    )
    fs20.add_v4_master("articles", 1, "en", {"title": "Hello", "body": "World"})
    c20 = _conn("v4 create", version="v4")
    ok, msg, s20 = cstore.strapi_translate(c20.id, actor="auto", transport=fs20.transport())
    assert ok and s20["created"] == 2, s20
    assert {loc for _c, mid, loc, _d in fs20.creates} == {"fr", "de"} and all(
        mid == 1 for _c, mid, _l, _d in fs20.creates
    ), fs20.creates

    fs20b = FakeStrapi(version="v4", locales=[{"code": "en", "isDefault": True}, {"code": "fr"}])
    fs20b.add_v4_master("articles", 1, "en", {"title": "Hello"})
    fs20b.add_v4_localization("articles", 1, 500, "fr", {"title": "Salut-old"})
    c20b = _conn("v4 update", {"target_locales": "fr"}, version="v4")
    ok, msg, s20b = cstore.strapi_translate(c20b.id, actor="auto", transport=fs20b.transport())
    assert ok and s20b["updated"] == 1 and fs20b.updates[0][1] == 500, (s20b, fs20b.updates)
    scv4b = _sc({"target_locales": "fr", "strapi_version": "v4"}, fs20b.transport(), version="v4")
    assert scv4b.get_translation("articles:1", "fr") is not ABSENT
    assert scv4b.get_translation("articles:1", "de") is ABSENT
    print("  v4: create localizations (fr+de) + update existing fr by its own id (500)")

    # ---- (21) egress guard fires for strapi --------------------------------------
    from app.services.connectors import egress_guard

    fs21 = FakeStrapi()
    fs21.add_v5("articles", "d1", "en", {"title": "Hi"})
    c21 = _conn("egress")
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s21 = cstore.strapi_translate(c21.id, actor="a", transport=fs21.transport())
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fs21.creates, ("mock MT + real egress -> blocked, no writes", msg)
    print("  egress guard: mock MT + real egress refused, no external writes")

    # ---- (22) parity surfaces ----------------------------------------------------
    from app.services.connectors import REGISTRY

    assert "strapi" in REGISTRY
    c = TestClient(app)
    assert "Strapi" in c.get("/connectors").text
    typ = c.get("/connectors/strapi").text
    assert "Base URL" in typ and "Create" in typ
    from app.cli.__main__ import build_parser

    assert "connector strapi-translate" in _leaf_paths(build_parser())
    from starlette.routing import Route

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/strapi/translate" in api_paths
    print("  parity: REGISTRY + /connectors tile + type form + /api route + CLI leaf")

    # ---- (23) N7: _RESERVED_WRITE_KEYS stays coupled to what the strategy injects ----
    from app.services.connectors import strategy as _S
    from app.services.connectors.strapi import _RESERVED_WRITE_KEYS

    injected: dict = {}

    class _Spy:
        connection_id = None

        def get_translation(self, r, l):  # noqa: E741
            return _S.ABSENT

        def create_translation(self, r, l, fields):  # noqa: E741
            injected.update(fields)
            return _S.WriteResult(True, True)

        def update_translation(self, r, l, fields):  # noqa: E741
            injected.update(fields)
            return _S.WriteResult(True, False)

    _S.SubResourceStrategy().write_target(_Spy(), "1", "fr", {}, draft=True)
    assert set(injected) <= _RESERVED_WRITE_KEYS, (
        "SubResourceStrategy injects a key the Strapi connector does not alias/strip — a "
        "real content field of that name would be clobbered. Add it to _RESERVED_WRITE_KEYS "
        "+ _build_data.",
        set(injected),
        _RESERVED_WRITE_KEYS,
    )
    assert "draft" in injected, "sanity: the strategy still injects draft"
    print("  N7: _RESERVED_WRITE_KEYS covers every key SubResourceStrategy injects")

    # ---- (24) #640: a held-back BODY unit must not create an empty-body variant ----
    def _hashes(cid):
        return dict((cstore.get(cid).config or {}).get("strapi_hashes") or {})

    fs24 = FakeStrapi()
    fs24.add_v5("articles", "d1", "en", {"title": "Hello", "body": "World"})
    c24 = _conn("640 create", {"target_locales": "fr"})
    orig, patched = _body_needs_review(T)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s24 = cstore.strapi_translate(c24.id, actor="auto", transport=fs24.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert not fs24.creates and fs24.draft_of("articles", "d1", "fr") is None, (
        "#640: no locale variant is created while a field is held back",
        fs24.creates,
    )
    assert s24["created"] == 0 and s24["held_back"] == 1 and not s24["failures"], s24
    assert "Held back 1" in msg, msg
    assert "articles:d1:fr" not in _hashes(c24.id), (
        "#640: no content hash for a held pair, so it is retried",
        _hashes(c24.id),
    )
    # after approval (a healthy run) the variant is created with EVERY field present
    ok, msg, s24b = cstore.strapi_translate(c24.id, actor="auto", transport=fs24.transport())
    assert ok and s24b["created"] == 1 and s24b["held_back"] == 0, (msg, s24b)
    made = fs24.draft_of("articles", "d1", "fr")
    assert made and made.get("title") and made.get("body"), ("both fields written", made)
    assert "articles:d1:fr" in _hashes(c24.id)
    print("  #640: held body unit -> variant NOT created, held_back counted, no hash, retried")

    # UPDATE: the held field keeps its stored value, the rest is written, no hash recorded
    fs24u = FakeStrapi()
    fs24u.add_v5("articles", "d1", "en", {"title": "Hello", "body": "World"})
    fs24u.add_v5(
        "articles", "d1", "fr", {"title": "Bonjour(h)", "body": "Corps(h)"}, published=False
    )
    c24u = _conn("640 update", {"target_locales": "fr"})
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s24u = cstore.strapi_translate(c24u.id, actor="auto", transport=fs24u.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert fs24u.updates and set(fs24u.updates[-1][3]) == {"title"}, fs24u.updates
    assert fs24u.draft_of("articles", "d1", "fr")["body"] == "Corps(h)", "held field kept"
    assert s24u["updated"] == 1 and s24u["held_back"] == 1 and not s24u["failures"], s24u
    assert "articles:d1:fr" not in _hashes(c24u.id), "#640: no hash on a partial update"
    ok, msg, s24v = cstore.strapi_translate(c24u.id, actor="auto", transport=fs24u.transport())
    assert ok and s24v["updated"] == 1 and s24v["skipped"] == 0 and s24v["held_back"] == 0, s24v
    assert fs24u.draft_of("articles", "d1", "fr")["body"] != "Corps(h)", "retry wrote the body"
    print("  #640: held body unit on UPDATE -> stored value kept, no hash, retry writes it")

    # ---- (25) #638: fields Strapi marks `localized: false` are never translated ----
    def _ent(fs):
        fs.add_v5(
            "articles",
            "d1",
            "en",
            {"title": "Hello", "campaign": "Fall 2026", "tagline": "Bold ideas"},
        )

    fs25 = FakeStrapi()
    _ent(fs25)
    fs25.builder = [
        _page_schema({"title": True, "campaign": False, "tagline": True}),
        _page_schema({"name": False}, plural="people"),
    ]
    c25 = _conn("638 schema", {"target_locales": "fr"})
    ok, msg, s25 = cstore.strapi_translate(c25.id, actor="auto", transport=fs25.transport())
    assert ok, msg
    fr25 = fs25.draft_of("articles", "d1", "fr")
    assert fr25 and "campaign" not in fr25 and fr25.get("tagline") and fr25.get("title"), fr25
    assert "campaign" not in s25["fields_selected"], s25
    assert s25["fields_skipped"].get("campaign") == "not localized in Strapi", s25
    assert "campaign (not localized in Strapi)" in msg and "Could not read" not in msg, msg
    assert fs25.builder_gets == 1, ("the schema is read once per run", fs25.builder_gets)
    print("  #638: localized:false -> field skipped 'not localized in Strapi', not written")

    # an allowlist cannot smuggle a non-localized field back in
    fs25b = FakeStrapi()
    _ent(fs25b)
    fs25b.builder = fs25.builder
    c25b = _conn("638 allow", {"target_locales": "fr", "fields": "title, campaign"})
    ok, msg, s25b = cstore.strapi_translate(c25b.id, actor="auto", transport=fs25b.transport())
    assert ok, msg
    assert "campaign" not in (fs25b.draft_of("articles", "d1", "fr") or {}), "allowlist"
    assert s25b["fields_skipped"].get("campaign") == "not localized in Strapi", s25b

    # a content type with localization switched off: nothing in it is translatable
    fs25c = FakeStrapi()
    _ent(fs25c)
    fs25c.builder = [_page_schema({"title": True, "campaign": True}, type_localized=False)]
    c25c = _conn("638 off", {"target_locales": "fr"})
    ok, msg, s25c = cstore.strapi_translate(c25c.id, actor="auto", transport=fs25c.transport())
    assert not fs25c.creates and not s25c["fields_selected"], (s25c, fs25c.creates)

    # schema unreadable (403): today's heuristic stays, and the run message says so
    fs25d = FakeStrapi()
    _ent(fs25d)
    c25d = _conn("638 unreadable", {"target_locales": "fr"})
    ok, msg, _s25d = cstore.strapi_translate(c25d.id, actor="auto", transport=fs25d.transport())
    assert ok, msg
    assert "campaign" in (fs25d.draft_of("articles", "d1", "fr") or {}), "heuristic unchanged"
    assert "Could not read Strapi's content-type schema" in msg and "`fields` allowlist" in msg, msg
    # ...but not when the operator already set an allowlist
    fs25e = FakeStrapi()
    _ent(fs25e)
    c25e = _conn("638 unreadable+allow", {"target_locales": "fr", "fields": "title, tagline"})
    ok, msg, _s25e = cstore.strapi_translate(c25e.id, actor="auto", transport=fs25e.transport())
    assert ok and "Could not read" not in msg, msg
    print(
        "  #638: type-level off skips all; unreadable schema keeps heuristic + says so in the run"
    )

    print("verify_strapi OK")


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
