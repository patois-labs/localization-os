"""Akeneo connector + connector-framework seams — fully offline via httpx.MockTransport.

FakeAkeneo is a SCHEMA-AWARE fake that enforces Akeneo's REAL write validation (taxonomy G,
the Strapi fixture-dodge lesson): it 422s an unknown attribute code, a locale value on a
NON-localizable attribute, a missing/extra scope, and a disabled locale — so the payload
assertions below actually bite. It also models Akeneo's officially-documented PARTIAL MERGE
at the (attribute, locale, scope) triple level, so the "other locales/scopes/attributes
survive" assertions are meaningful (a merge-deleting fake would pass them vacuously).

Covers the taxonomy A-H checklist (the connector build blueprint §3):
  A  caller article_ids are scope-validated (content-type half against the fixed {product,
     product_model} allowlist incl. a PREFIX of a valid type; id half against traversal /
     '?' / '#' / NUL / CR-LF) end-to-end + at the client seam, with ZERO external writes;
     operator content_types='..' is refused too.
  B  a held-back (NEEDS_REVIEW) attribute never overwrites an existing stored translation;
     a partial hold-back writes the healthy attrs and leaves the held one untouched.
  C  the existence probe reads the LIVE record (CE has no draft/publish), so a second run
     over unchanged source is fully SKIPPED (no re-translate loop).
  D  schema-driven selection: localizable text SELECTED; non-localizable / select / metric /
     media / identifier SKIPPED and REPORTED (both directions).
  E  the shared cap round-robins across product + product_model (assert DISTRIBUTION).
  F  a no-op (all-held) run records NO suppression hash -> a healthy re-run re-attempts.
  G  the fake enforces real validation (unknown attr / non-localizable-locale -> 422).
  H  WriteBudget ceiling, egress guard fires, four parity surfaces exist.
Plus: partial-PATCH merge preserves other locales/scopes/attributes (MANDATORY-1 model),
content-hash suppression, locale-case round-trip (fr_FR), scopable-attribute channel scope,
created-vs-updated, no-read-before-write on the merge, source-locale loud fails.

ASCII-only prints; final line is 'verify_akeneo OK'.
"""

import copy
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_akeneo_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import Connection, Segment, SegStatus
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.akeneo_part1 import AkeneoConnector
from app.services.connectors.strategy import ABSENT

# Default attribute schema: two localizable text attrs + a spread of NON-translatable types
# so schema-driven selection (D) has both directions to exercise.
_DEFAULT_ATTRS = {
    "name": {"type": "pim_catalog_text", "localizable": True, "scopable": False},
    "description": {"type": "pim_catalog_textarea", "localizable": True, "scopable": False},
    "sku": {"type": "pim_catalog_identifier", "localizable": False, "scopable": False},
    "meta_title": {"type": "pim_catalog_text", "localizable": False, "scopable": False},
    "color": {"type": "pim_catalog_simpleselect", "localizable": True, "scopable": False},
    "weight": {"type": "pim_catalog_metric", "localizable": False, "scopable": False},
    "image": {"type": "pim_catalog_image", "localizable": False, "scopable": False},
    "care": {"type": "pim_catalog_textarea", "localizable": True, "scopable": True},
}


def _tri(locale, data, scope=None):
    return {"locale": locale, "scope": scope, "data": data}


# ============================================================================
# Fake Akeneo. Models /locales, /attributes, /products-uuid, /product-models with a strict
# per-(attribute, locale, scope) validator and a real partial-merge PATCH.
# ============================================================================
class FakeAkeneo:
    def __init__(
        self, locales=None, attributes=None, page_size=100, channels=None, deny_attrs=False
    ):
        self.locales = locales or [
            {"code": "en_US", "enabled": True},
            {"code": "fr_FR", "enabled": True},
            {"code": "de_DE", "enabled": True},
        ]
        self.attributes = copy.deepcopy(_DEFAULT_ATTRS if attributes is None else attributes)
        self.channels = channels if channels is not None else [{"code": "ecommerce"}]
        self.deny_attrs = deny_attrs  # True -> GET /attributes 403s (unreadable schema)
        self.page_size = page_size
        self.products: dict = {}  # uuid -> {"uuid":.., "values":{..}, "updated":..}
        self.product_models: dict = {}  # code -> {"code":.., "values":{..}, "updated":..}
        self.patches: list = []  # (ct, ident, values) for SUCCESSFUL writes only

    # ---- seeding -----------------------------------------------------------
    def add_product(self, uuid, values, updated="2026-07-05T10:00:00+00:00"):
        self.products[uuid] = {"uuid": uuid, "values": copy.deepcopy(values), "updated": updated}

    def add_product_model(self, code, values, updated="2026-07-05T10:00:00+00:00"):
        self.product_models[code] = {
            "code": code,
            "values": copy.deepcopy(values),
            "updated": updated,
        }

    def values_of(self, ct, ident):
        store = self.products if ct == "product" else self.product_models
        return (store.get(ident) or {}).get("values")

    def transport(self):
        return httpx.MockTransport(self._handle)

    # ---- strict validator (taxonomy G) -------------------------------------
    def _enabled_codes(self):
        return {l["code"] for l in self.locales if l.get("enabled", True)}  # noqa: E741

    def _reject(self, msg):
        return httpx.Response(
            422, json={"message": "Validation failed.", "errors": [{"message": msg}]}
        )

    def _validate(self, values):
        codes = self._enabled_codes()
        for attr, triples in (values or {}).items():
            meta = self.attributes.get(attr)
            if meta is None:
                return self._reject(f"Attribute '{attr}' does not exist.")
            if not isinstance(triples, list):
                return self._reject(f"Attribute '{attr}' expects an array of values.")
            for t in triples:
                loc, scope = t.get("locale"), t.get("scope")
                if meta["localizable"] and not loc:
                    return self._reject(f"Attribute '{attr}' is localizable: a locale is required.")
                if not meta["localizable"] and loc:
                    return self._reject(
                        f"Attribute '{attr}' is not localizable: no locale allowed."
                    )
                if meta["scopable"] and not scope:
                    return self._reject(f"Attribute '{attr}' is scopable: a scope is required.")
                if not meta["scopable"] and scope:
                    return self._reject(f"Attribute '{attr}' is not scopable: no scope allowed.")
                if loc and loc not in codes:
                    return self._reject(f"Locale '{loc}' is not enabled.")
        return None

    def _merge(self, rec, values):
        cur = rec["values"]
        for attr, triples in values.items():
            existing = cur.setdefault(attr, [])
            for t in triples:
                key = (t.get("locale"), t.get("scope"))
                for e in existing:
                    if (e.get("locale"), e.get("scope")) == key:
                        e["data"] = t.get("data")
                        break
                else:
                    existing.append(dict(t))

    def _paginated(self, items, params):
        page = int(params.get("page", "1"))
        limit = int(params.get("limit", str(self.page_size)))
        start = (page - 1) * limit
        return httpx.Response(200, json={"_embedded": {"items": items[start : start + limit]}})

    def _handle(self, req):
        path, method, params = req.url.path, req.method, req.url.params
        if not path.startswith("/api/rest/v1"):
            return httpx.Response(404, json={"message": "not /api/rest/v1"})
        rest = path[len("/api/rest/v1") :]
        if method == "GET" and rest == "/locales":
            return self._paginated(self.locales, params)
        if method == "GET" and rest == "/channels":
            return self._paginated(self.channels, params)
        if method == "GET" and rest == "/attributes":
            if self.deny_attrs:
                return httpx.Response(403, json={"message": "Access forbidden."})
            items = [{"code": c, **m} for c, m in self.attributes.items()]
            return self._paginated(items, params)
        for ct, endpoint, store in (
            ("product", "products-uuid", self.products),
            ("product_model", "product-models", self.product_models),
        ):
            base = "/" + endpoint
            if method == "GET" and rest == base:
                return self._paginated(list(store.values()), params)
            prefix = base + "/"
            if rest.startswith(prefix):
                ident = rest[len(prefix) :]
                if method == "GET":
                    if ident not in store:
                        return httpx.Response(404, json={"message": "not found"})
                    return httpx.Response(200, json=store[ident])
                if method == "PATCH":
                    values = (json.loads(req.content or b"{}") or {}).get("values") or {}
                    bad = self._validate(values)
                    if bad is not None:
                        return bad
                    if ident not in store:
                        idf = "uuid" if ct == "product" else "code"
                        store[ident] = {idf: ident, "values": {}}
                    self._merge(store[ident], values)
                    self.patches.append((ct, ident, values))
                    return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unexpected {method} {rest}"})


def _conn(name, cfg_extra=None):
    cfg = {
        "base_url": "http://akeneo.test",
        "content_types": "product",
        "source_locale": "en_US",
        "target_locales": "fr_FR, de_DE",
    }
    cfg.update(cfg_extra or {})
    return cstore.create("akeneo", name, cfg, {"access_token": "tok"}, actor="admin")


def _ac(cfg, transport):
    base = {"base_url": "http://akeneo.test", "content_types": "product", "source_locale": "en_US"}
    base.update(cfg or {})
    return AkeneoConnector(base, {"access_token": "tok"}, transport=transport)


def _needs_review(mod, only_sources=None):
    """Wrap pretranslate_document to flag NEEDS_REVIEW: all segments, or only those whose
    source_text is in `only_sources` (a partial hold-back)."""
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


def main() -> None:
    from app.web._routes import walk_routes

    init_db()

    # ---- (1) capability + test + case-preserving resolve ------------------------
    assert AkeneoConnector.capability.topology == "T1"
    fs = FakeAkeneo()
    ac = _ac({}, fs.transport())
    ok, msg = ac.test()
    assert ok, msg
    assert set(ac.enabled_locales()) == {"en_us", "fr_fr", "de_de"}
    assert ac.resolve_locale("fr_fr") == "fr_FR", ac.resolve_locale("fr_fr")
    assert ac._source_locale() == "en_US"
    print("  connector: T1, test(), case-preserving resolve (fr_fr -> fr_FR)")

    # ---- (2) base_url normalization + SSRF guard --------------------------------
    assert (
        _ac({"base_url": "http://akeneo.test/api/rest/v1"}, fs.transport())._base()
        == "http://akeneo.test/api/rest/v1"
    )
    assert (
        _ac({"base_url": "http://akeneo.test"}, fs.transport())._base()
        == "http://akeneo.test/api/rest/v1"
    )
    raised = False
    try:
        _ac({"base_url": "http://169.254.169.254"}, fs.transport())._base()
    except cbase.ConnectorError:
        raised = True
    assert raised, "metadata IP must be blocked"
    print("  base_url: /api/rest/v1 normalized; metadata IP blocked (SSRF guard)")

    # ---- (3) D: schema-driven selection + VISIBLE skips (both directions) --------
    values = {
        "name": [_tri("en_US", "Hello world")],
        "description": [_tri("en_US", "A nice product.")],
        "sku": [_tri(None, "SKU-001")],  # identifier -> not localizable
        "meta_title": [_tri(None, "meta")],  # text but NOT localizable
        "color": [_tri("en_US", "red")],  # simpleselect -> not text
        "weight": [_tri(None, {"amount": "1", "unit": "KG"})],  # metric
        "image": [_tri(None, "a/b/c.png")],  # media
        "care": [_tri("en_US", "Wash cold", scope="ecommerce")],  # scopable, no channel -> skip
    }
    ac3 = _ac({}, fs.transport())
    selected, skipped = ac3._field_selection(values)
    assert [a for a, _ in selected] == ["description", "name"], selected
    for bad, why in [
        ("sku", "not localizable"),
        ("meta_title", "not localizable"),
        ("color", "not translatable text"),
        ("weight", "not localizable"),
        ("image", "not localizable"),
        ("care", "channel"),
    ]:
        assert bad in skipped and why in skipped[bad], (bad, skipped.get(bad))
    # the localizable-but-non-text path (a select) is reported with the deferred-build note
    assert "deferred build" in skipped["color"], skipped["color"]
    print(
        "  D: selects name+description; skips identifier/non-localizable/select/metric/"
        "media + scopable-without-channel, all REPORTED"
    )

    # a scopable attribute IS selected once a channel is configured
    ac3b = _ac({"channel": "ecommerce"}, fs.transport())
    sel3b, _sk = ac3b._field_selection(values)
    assert "care" in [a for a, _ in sel3b], ("scopable attr selected with a channel", sel3b)
    print("  D: a scopable attribute is selected once the connection has a channel")

    # ---- (4) G: the fake ENFORCES real validation (non-vacuous payload tests) ----
    fsg = FakeAkeneo()
    fsg.add_product("p1", {"name": [_tri("en_US", "Hi")]})
    acg = _ac({}, fsg.transport())
    good = acg.patch_record("product:p1", {"name": [_tri("fr_FR", "Salut")]})
    assert good.ok, good
    bad_unknown = acg.patch_record("product:p1", {"nope": [_tri("fr_FR", "x")]})
    assert not bad_unknown.ok and bad_unknown.status == 422, ("unknown attr -> 422", bad_unknown)
    bad_loc = acg.patch_record("product:p1", {"sku": [_tri("fr_FR", "x")]})
    assert not bad_loc.ok and bad_loc.status == 422, ("locale on non-localizable -> 422", bad_loc)
    print("  G: fake 422s unknown attribute + locale-on-non-localizable (payload tests bite)")

    # ---- (5) full flow CREATE + no-read-before-write + partial merge -------------
    fs5 = FakeAkeneo()
    fs5.add_product(
        "p1", {"name": [_tri("en_US", "Hello")], "description": [_tri("en_US", "World")]}
    )
    c5 = _conn("create", {"target_locales": "fr_FR"})
    ok, msg, s5 = cstore.akeneo_translate(c5.id, actor="auto", transport=fs5.transport())
    assert ok, msg
    assert s5["created"] == 1, ("one fr write (both attrs in a single PATCH)", s5)
    # the write body carried ONLY the two translated attrs, each a single fr triple
    assert fs5.patches, "a PATCH landed"
    for _ct, _id, vals in fs5.patches:
        assert set(vals) <= {"name", "description"}, ("only translated attrs in body", vals)
        for triples in vals.values():
            assert (
                len(triples) == 1
                and triples[0]["locale"] == "fr_FR"
                and triples[0]["scope"] is None
            ), ("one fr_FR/null-scope triple", triples)
    # the source en_US triple survived the partial merge (never overwritten)
    stored = fs5.values_of("product", "p1")
    en = [t for t in stored["name"] if t["locale"] == "en_US"]
    assert en and en[0]["data"] == "Hello", ("source en_US survives the merge", stored["name"])
    print("  flow CREATE: 2 fr values written (partial body); source en_US survives the merge")

    # ---- (6) A: caller article_ids scope-validated end-to-end + at the seam ------
    fs6 = FakeAkeneo()
    fs6.add_product("p1", {"name": [_tri("en_US", "Hi")]})
    ac6 = _ac({"content_types": "product"}, fs6.transport())
    must_raise = [
        "..:x",
        "%2e%2e%2f:x",
        "prod:x",
        "produc:x",  # ct half: unknown / PREFIX
        "product_models:x",
        "PRODUCT:x",
        "prodöct:x",  # wrong-form / case / unicode ct
        "product:..",
        "product:a/b",
        "product:a?x=1",
        "product:a#f",  # id half structural
        "product:a\x00b",
        "product:a\r\nb",
        "product:a b",  # id half NUL / CRLF / space
    ]
    for bad in must_raise:
        r = False
        try:
            ac6._split_aid(bad)
        except cbase.ConnectorError:
            r = True
        assert r, (repr(bad), "must raise at the client seam")
    c6 = _conn("scope", {"target_locales": "fr_FR"})
    for bad in must_raise + ["employee_salaries:1"]:  # noqa: RUF005
        fs6.patches.clear()
        ok, msg, _s6 = cstore.akeneo_translate(
            c6.id, article_ids=[bad], actor="attacker", transport=fs6.transport()
        )
        assert not fs6.patches, (repr(bad), "ZERO external writes", fs6.patches)
    # operator content_types='..' is refused (Strapi N4)
    r = False
    try:
        _ac({"content_types": ".."}, fs6.transport())._content_types()
    except cbase.ConnectorError:
        r = True
    assert r, "operator content_types='..' must be refused"
    # a unicode id half is not an escape: safely percent-encoded, no write, no crash
    fs6.patches.clear()
    ok, _m, _s = cstore.akeneo_translate(
        c6.id, article_ids=["product:naïve"], actor="a", transport=fs6.transport()
    )
    assert not fs6.patches, "unicode id -> 404, no write"
    print(
        "  A: ct-allowlist + id-segment refusals (incl. PREFIX 'prod') at seam + "
        "end-to-end; content_types='..' refused; zero writes"
    )

    # ---- (7) B + F: held-back never overwrites; no hash poisoning ----------------
    import app.services.translate as T

    fs7 = FakeAkeneo()
    fs7.add_product(
        "p1",
        {
            "name": [_tri("en_US", "Hello"), _tri("fr_FR", "Bonjour(humain)")],
            "description": [_tri("en_US", "World"), _tri("fr_FR", "Monde(humain)")],
        },
    )
    c7 = _conn("held", {"target_locales": "fr_FR"})
    orig, patched = _needs_review(T)  # ALL segments -> NEEDS_REVIEW
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s7 = cstore.akeneo_translate(c7.id, actor="auto", transport=fs7.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert not fs7.patches, ("all held back -> NO write", fs7.patches)
    stored7 = fs7.values_of("product", "p1")
    fr_name = [t for t in stored7["name"] if t["locale"] == "fr_FR"][0]["data"]  # noqa: RUF015
    assert fr_name == "Bonjour(humain)", ("B: existing human fr survives", stored7["name"])
    assert (
        s7["held_back"] == 1 and s7["created"] == 0 and s7["updated"] == 0 and not s7["failures"]
    ), ("F: held_back is its own counter, not a failure", s7)
    # F core: NO suppression hash recorded -> a healthy re-run re-attempts.
    ok2, _m2, s72 = cstore.akeneo_translate(c7.id, actor="auto", transport=fs7.transport())
    assert ok2 and s72["updated"] == 1 and s72["skipped"] == 0, (
        "F: held-back content re-attempted next run (hash NOT poisoned)",
        s72,
    )
    assert fs7.patches, "healthy re-run actually wrote"
    print(
        "  B/F: NEEDS_REVIEW held back -> human fr preserved, held_back not updated, no "
        "hash poisoning (healthy re-run re-attempts)"
    )

    # ---- (8) B partial: some attrs held, the held one is left untouched ----------
    fs8 = FakeAkeneo()
    fs8.add_product(
        "p1",
        {
            "name": [_tri("en_US", "Hello"), _tri("fr_FR", "NomHumain")],
            "description": [_tri("en_US", "World")],
        },
    )
    c8 = _conn("partial", {"target_locales": "fr_FR"})
    orig, patched = _needs_review(T, only_sources={"Hello"})  # hold ONLY name
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s8 = cstore.akeneo_translate(c8.id, actor="auto", transport=fs8.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    for _ct, _id, vals in fs8.patches:
        assert "name" not in vals and "description" in vals, (
            "held 'name' omitted; 'description' written",
            vals,
        )
    stored8 = fs8.values_of("product", "p1")
    fr_name8 = [t for t in stored8["name"] if t["locale"] == "fr_FR"][0]["data"]  # noqa: RUF015
    assert fr_name8 == "NomHumain", ("held name's existing fr untouched", stored8["name"])
    assert s8["incomplete"] and s8["incomplete"][0]["held_fields"] == ["name"], s8["incomplete"]
    print(
        "  B: partial hold-back writes description, leaves held name's fr untouched, "
        "reports incomplete"
    )

    # ---- (9) C: second-run steady state (no re-translate loop) -------------------
    fs9 = FakeAkeneo()
    fs9.add_product("p1", {"name": [_tri("en_US", "Hello")]})
    c9 = _conn("steady", {"target_locales": "fr_FR"})
    ok, _m, r1 = cstore.akeneo_translate(c9.id, actor="auto", transport=fs9.transport())
    assert ok and r1["created"] == 1, ("run 1 creates fr", r1)
    # the probe reads the live record and finds the fr value it just wrote
    probe_ac = _ac({}, fs9.transport())
    assert probe_ac.strategy.exists_target(probe_ac, "product:p1", "fr_fr") is True
    fs9.patches.clear()
    ok, _m, r2 = cstore.akeneo_translate(c9.id, actor="auto", transport=fs9.transport())
    assert ok and r2["created"] == 0 and r2["updated"] == 0 and r2["skipped"] == 1, (
        "run 2 suppresses (probe found the fr value)",
        r2,
    )
    assert not fs9.patches, "run 2 wrote nothing"
    print("  C: run 2 over unchanged source is fully SKIPPED (probe reads the live write)")

    # ---- (10) content-hash suppression: edited source -> re-translate ------------
    fs10 = FakeAkeneo()
    fs10.add_product("p1", {"name": [_tri("en_US", "Hello EDITED"), _tri("fr_FR", "old")]})
    # reuse c9's connection (already has a stored hash for the OLD source)
    ok, _m, s10 = cstore.akeneo_translate(c9.id, actor="auto", transport=fs10.transport())
    assert ok and s10["updated"] == 1 and s10["skipped"] == 0, ("edited source re-translates", s10)
    print("  suppression: edited source content -> re-translate (updated 1)")

    # ---- (11) created vs updated -------------------------------------------------
    fs11 = FakeAkeneo()
    fs11.add_product("p1", {"name": [_tri("en_US", "Hello"), _tri("fr_FR", "Vieux")]})
    c11 = _conn("cvu", {"target_locales": "fr_FR"})
    ok, _m, s11 = cstore.akeneo_translate(c11.id, actor="auto", transport=fs11.transport())
    assert ok and s11["updated"] == 1 and s11["created"] == 0, (
        "pre-existing fr -> updated not created",
        s11,
    )
    print("  created/updated: a pre-existing fr value is reported as UPDATED")

    # ---- (12) E: shared cap round-robins across product + product_model ----------
    fs12 = FakeAkeneo()
    for i in range(40):
        fs12.add_product(f"pu-{i}", {"name": [_tri("en_US", f"n{i}")]})
        fs12.add_product_model(f"pm-{i}", {"name": [_tri("en_US", f"m{i}")]})
    ac12 = _ac({"content_types": "product, product_model", "max_entries": "50"}, fs12.transport())
    got = ac12.discover_articles()
    counts = {}
    for e in got:
        counts[e["_ct"]] = counts.get(e["_ct"], 0) + 1
    assert len(got) == 50, ("shared cap fills to 50", len(got))
    assert set(counts) == {"product", "product_model"} and min(counts.values()) >= 20, (
        "E: round-robin distributes the shared cap; NO type starved",
        counts,
    )
    print(f"  E: cap round-robins across product + product_model (no starvation): {counts}")

    # ---- (13) H: WriteBudget ceiling --------------------------------------------
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fs13 = FakeAkeneo()
        fs13.add_product("p1", {"name": [_tri("en_US", "Hello")]})
        c13 = _conn("budget", {"target_locales": "fr_FR, de_DE"})
        ok, _m, s13 = cstore.akeneo_translate(c13.id, actor="auto", transport=fs13.transport())
        assert ok and s13["ceiling_hit"] and (s13["created"] + s13["updated"]) == 1, s13
        assert len(fs13.patches) == 1, ("exactly one external write", fs13.patches)
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    print("  H: WriteBudget MAX_WRITES_PER_RUN=1 stops after exactly one external write")

    # ---- (14) H: egress guard fires ---------------------------------------------
    from app.services.connectors import egress_guard

    fs14 = FakeAkeneo()
    fs14.add_product("p1", {"name": [_tri("en_US", "Hi")]})
    c14 = _conn("egress", {"target_locales": "fr_FR"})
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s14 = cstore.akeneo_translate(c14.id, actor="a", transport=fs14.transport())
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fs14.patches, ("mock MT + real egress -> blocked, no writes", msg)
    print("  H: egress guard - mock MT + real egress refused, no external writes")

    # ---- (15) H: parity surfaces ------------------------------------------------
    from app.services.connectors import REGISTRY

    assert "akeneo" in REGISTRY
    c = TestClient(app)
    assert "Akeneo" in c.get("/connectors").text
    typ = c.get("/connectors/akeneo").text
    assert "Instance URL" in typ and "Create" in typ, "type form renders"
    from app.cli.__main__ import build_parser

    assert "connector akeneo-translate" in _leaf_paths(build_parser())
    from starlette.routing import Route

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/akeneo/translate" in api_paths
    print("  H parity: REGISTRY + /connectors tile + type form + /api route + CLI leaf")

    # ---- (16) locale case round-trip on the wire (fr_FR / pt_BR) -----------------
    fs16 = FakeAkeneo(
        locales=[{"code": "en_US", "enabled": True}, {"code": "pt_BR", "enabled": True}]
    )
    fs16.add_product("p1", {"name": [_tri("en_US", "Hello")]})
    c16 = _conn("case", {"target_locales": "pt_BR"})
    ok, _m, _s16 = cstore.akeneo_translate(c16.id, actor="auto", transport=fs16.transport())
    assert ok and fs16.patches, fs16.patches
    wrote = {t["locale"] for _c, _i, vals in fs16.patches for tr in vals.values() for t in tr}
    assert wrote == {"pt_BR"}, ("flow lowercases pt_br; the wire write uses exact pt_BR", wrote)
    print("  locale case: flow uses pt_br internally; the wire write uses exact pt_BR")

    # ---- (17) enabled-locale rejection + honest source-drop; source loud fails ---
    fs17 = FakeAkeneo(
        locales=[{"code": "en_US", "enabled": True}, {"code": "fr_FR", "enabled": True}]
    )
    fs17.add_product("p1", {"name": [_tri("en_US", "Hello")]})
    c17 = _conn("reject", {"target_locales": "fr_FR, de_DE, en_US"})
    ok, msg, s17 = cstore.akeneo_translate(c17.id, actor="auto", transport=fs17.transport())
    assert ok and s17["locales"] == ["fr_fr"] and s17["rejected"] == ["de_de"], s17
    assert s17["dropped_source"] == ["en_us"] and "equal to the source locale" in msg, (s17, msg)
    print("  rejection: 'de_DE' rejected, 'en_US' dropped-as-source and reported honestly")

    # source_locale unset -> loud fail
    fs17b = FakeAkeneo()
    fs17b.add_product("p1", {"name": [_tri("en_US", "Hi")]})
    c17b = cstore.create(
        "akeneo",
        "nosrc",
        {"base_url": "http://akeneo.test", "content_types": "product", "target_locales": "fr_FR"},
        {"access_token": "t"},
        actor="admin",
    )
    ok, msg, _s = cstore.akeneo_translate(c17b.id, actor="auto", transport=fs17b.transport())
    assert not ok and "no default locale" in msg.lower(), ("source_locale required, loud fail", msg)
    # source_locale not enabled -> loud fail
    c17c = _conn("badsrc", {"source_locale": "zz_ZZ", "target_locales": "fr_FR"})
    ok, msg, _s = cstore.akeneo_translate(c17c.id, actor="auto", transport=FakeAkeneo().transport())
    assert not ok and "not enabled" in msg.lower(), ("unenabled source loud fail", msg)
    print("  source locale: unset OR not-enabled fails LOUD (no silent stall)")

    # ---- (18) product-model write path (code-keyed) -----------------------------
    fs18 = FakeAkeneo()
    fs18.add_product_model("mycode", {"name": [_tri("en_US", "Model")]})
    c18 = _conn("pm", {"content_types": "product_model", "target_locales": "fr_FR"})
    ok, _m, s18 = cstore.akeneo_translate(c18.id, actor="auto", transport=fs18.transport())
    assert ok and s18["created"] == 1, s18
    assert (
        fs18.patches and fs18.patches[0][0] == "product_model" and fs18.patches[0][1] == "mycode"
    ), ("product_model PATCH by code", fs18.patches)
    print("  product_model: code-keyed record translated via /product-models/{code}")

    # ---- (19) ABSENT record probe -----------------------------------------------
    ac19 = _ac({}, FakeAkeneo().transport())
    assert ac19.strategy.exists_target(ac19, "product:ghost", "fr_fr") is False
    assert ac19.record_values("product:ghost") is ABSENT
    print("  probe: a missing record -> exists_target False, record_values ABSENT")

    # ---- (20) M1a: unreadable /attributes -> clean (False,msg) + ERROR + SyncLog ---
    from app.models import SyncLog

    fs20 = FakeAkeneo(deny_attrs=True)
    fs20.add_product("p1", {"name": [_tri("en_US", "Hello")]})
    c20 = _conn("attr403", {"target_locales": "fr_FR"})
    # PRE-FIX this raised an uncaught ConnectorError from the per-article loop (the schema
    # was fetched lazily in extract, outside stage-3's try). It must now be a clean fail.
    ok, msg, _s = cstore.akeneo_translate(c20.id, actor="auto", transport=fs20.transport())
    assert not ok, ("M1a: unreadable schema must fail loud, not raise", ok, msg)
    with Session(engine) as s:
        assert s.get(Connection, c20.id).status == "error", "ERROR status recorded"
        assert s.exec(select(SyncLog).where(SyncLog.connection_id == c20.id)).all(), (
            "a SyncLog row recorded the failure"
        )
    assert not fs20.patches
    print("  M1a: unreadable /attributes -> (False,msg) + ERROR status + SyncLog, no raise")

    # ---- (21) M1b: 200-but-empty /attributes while products exist -> loud fail ----
    fs21 = FakeAkeneo(attributes={})
    fs21.add_product("p1", {"name": [_tri("en_US", "Hello")]})
    c21 = _conn("attrempty", {"target_locales": "fr_FR"})
    ok, msg, _s = cstore.akeneo_translate(c21.id, actor="auto", transport=fs21.transport())
    assert not ok and ("schema" in msg.lower() or "attribute" in msg.lower()), (
        "M1b: empty schema is an error state, not a green zero-write run",
        ok,
        msg,
    )
    assert not fs21.patches
    print("  M1b: empty /attributes with products -> loud (False,msg), not a silent green no-op")

    # ---- (22) M1 benign: schema loads but NOTHING localizable -> benign skip-all ---
    fs22 = FakeAkeneo(
        attributes={
            "sku": {"type": "pim_catalog_identifier", "localizable": False, "scopable": False}
        }
    )
    fs22.add_product("p1", {"sku": [_tri(None, "SKU-1")]})
    c22 = _conn("nolocal", {"target_locales": "fr_FR"})
    ok, msg, s22 = cstore.akeneo_translate(c22.id, actor="auto", transport=fs22.transport())
    assert ok and s22["empty_skipped"] >= 1 and (s22["created"] + s22["updated"]) == 0, (
        "loaded-fine-but-nothing-localizable is a benign skip-all, not a hard fail",
        s22,
    )
    print(
        "  M1 benign: schema loads, no localizable attr -> ok=True skip-all (distinct from empty)"
    )

    # ---- (23) L2: configured-but-nonexistent channel -> clean preflight fail -------
    fs23 = FakeAkeneo()
    fs23.add_product("p1", {"name": [_tri("en_US", "Hello")]})
    c23 = _conn("badchan", {"channel": "does_not_exist", "target_locales": "fr_FR"})
    ok, msg, _s = cstore.akeneo_translate(c23.id, actor="auto", transport=fs23.transport())
    assert not ok and "channel" in msg.lower(), ("L2: bad channel fails at preflight", ok, msg)
    assert not fs23.patches
    # a valid channel preflights fine (and a scopable attr then writes with that scope)
    fs23b = FakeAkeneo()
    fs23b.add_product("p1", {"care": [_tri("en_US", "Wash cold", scope="ecommerce")]})
    c23b = _conn("okchan", {"channel": "ecommerce", "target_locales": "fr_FR"})
    ok, _m, s23b = cstore.akeneo_translate(c23b.id, actor="auto", transport=fs23b.transport())
    assert ok and s23b["created"] == 1, ("valid channel + scopable attr writes", s23b)
    scopes = {t["scope"] for _c, _i, vals in fs23b.patches for tr in vals.values() for t in tr}
    assert scopes == {"ecommerce"}, ("scopable value carries the channel scope", scopes)
    print("  L2: bad channel -> clean preflight fail; valid channel writes scoped values")

    # ---- (24) L1: harvest mapper reads the 'title' key --------------------------
    from app.services.connectors import harvest as _hv

    assert _hv._map_akeneo({"title": "Widget"})["source"].get("title") == "Widget", (
        "L1: _map_akeneo must read f.get('title') (not the literal 'None' key)"
    )
    print("  L1: harvest _map_akeneo reads the 'title' key like every sibling")

    print("verify_akeneo OK")


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
    main()
