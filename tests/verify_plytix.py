"""Plytix connector: offline verify suite over httpx.MockTransport.

FakePlytix is a STRICT fake (taxonomy G, the Strapi fixture-dodge lesson). It models what the
real API does and what the connector must respect:
  - POST /auth/api/get-token (api_key + api_password -> data[0].access_token); tokens expire, a
    stale token answers 401;
  - POST /api/v1/attributes/product/search (paged, label + type_class);
  - POST /api/v2/products/search (page_size > 100 -> 422, more than 50 requested attributes -> 422,
    an unknown attributes.<label> column -> 422, sku `in` filter, only the requested attribute
    columns come back);
  - GET /api/v2/products/{id} (every attribute);
  - PATCH /api/v2/products/{id}: only `label|status|attributes` keys, every attribute label must
    exist, a TextAttribute takes a string; a PARTIAL merge, so an attribute that is not named keeps
    its stored value (the "held attribute survives" assertions are therefore meaningful).
  - 429 injection (rate limit) and 5xx on PATCH.

Taxonomy A-H (the connector build blueprint section 3):
  A  hostile article_ids and a hostile attribute_map are refused with zero external writes;
     a foreign id (outside the configured SKUs) behaves like a missing one.
  B  a held-back attribute never overwrites a stored translation; the source attribute is never
     written (map validation + build_values defence).
  C  the existence probe reads the live record; run 2 over unchanged source is fully skipped.
  D  selection is schema driven: a non-text source is skipped and REPORTED; a map that names a
     missing / non-text target fails loud before any write.
  E  the record cap is honoured and pagination walks every page.
  F  an all-held pair records no content hash, so a healthy re-run re-attempts it; a pair is never
     created while a unit is held (#640).
  G  the fake enforces real validation.
  H  WriteBudget ceiling, egress guard, token never logged, rate-limit backoff via base._request,
     four parity surfaces.

ASCII-only prints; final line is 'verify_plytix OK'.
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

init_env("loctms_plytix_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import Connection, Segment, SegStatus, SyncLog
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.plytix import PlytixConnector
from app.services.connectors.strategy import ABSENT

API_KEY = "fixture-key-AAAA"  # pregate: allow-secret offline fixture
API_PASSWORD = "fixture-password-BBBB"  # pregate: allow-secret offline fixture
SECRET = {"api_key": API_KEY, "api_password": API_PASSWORD}
MAP = {
    "name": {"fr": "name_fr", "de": "name_de"},
    "description": {"fr": "description_fr", "de": "description_de"},
}
_SCHEMA = {
    "name": "TextAttribute",
    "description": "TextAttribute",
    "name_fr": "TextAttribute",
    "name_de": "TextAttribute",
    "description_fr": "TextAttribute",
    "description_de": "TextAttribute",
    "notes_html": "HtmlAttribute",
    "weight": "DecimalAttribute",
    "color": "DropdownAttribute",
}


def pid(n: int) -> str:
    return f"5f00000000000000000{n:05d}"


class FakePlytix:
    def __init__(
        self,
        schema=None,
        page_cap=100,
        rate_limit_first=0,
        expire_after=None,
        patch_mode="merge",
        names=None,
        ids=None,
    ):
        self.schema = copy.deepcopy(_SCHEMA if schema is None else schema)
        # Display names and ids of attributes (aliases a lenient API might accept).
        self.names = dict(names or {})
        self.ids = dict(ids or {})
        # "merge": PATCH merges the named attributes (what the community notes say).
        # "replace": PATCH REPLACES the whole attribute set (the unverified risk).
        self.patch_mode = patch_mode
        self.get_fails_after_patch = False  # every GET of a product 500s after the first PATCH
        self.get_without_attributes = False  # GET omits the attributes block entirely
        self.patch_fail_after: int | None = None  # PATCH number N+1 onward answers 422
        self.on_patch = None  # hook(product) run after a successful PATCH (concurrent edit)
        self.products: dict = {}
        self.patches: list = []  # (id, body) for SUCCESSFUL writes only
        self.requests: list = []  # (method, path)
        self.tokens_minted = 0
        self.valid_tokens: set = set()
        self.rate_limit_first = rate_limit_first  # first N API calls answer 429
        self.expire_after = expire_after  # after N authenticated calls the token goes stale
        self._authed_calls = 0
        self.search_pages: list = []
        self.patch_fail_status: int | None = None

    def add(self, n, sku, attributes, label="P"):
        self.products[pid(n)] = {
            "id": pid(n),
            "sku": sku,
            "label": label,
            "modified": "2026-10-01T10:00:00Z",
            "attributes": copy.deepcopy(attributes),
        }
        return pid(n)

    def attrs_of(self, n):
        return self.products[pid(n)]["attributes"]

    def transport(self):
        return httpx.MockTransport(self._handle)

    # ---- helpers -----------------------------------------------------------
    def _err(self, status, msg):
        return httpx.Response(status, json={"error": {"msg": msg, "errors": []}})

    def _auth_ok(self, req):
        h = req.headers.get("authorization", "")
        return h.startswith("Bearer ") and h[7:] in self.valid_tokens

    def _handle(self, req):
        path, method = req.url.path, req.method
        self.requests.append((method, path))
        if req.url.host == "auth.plytix.com":
            body = json.loads(req.content or b"{}")
            if path != "/auth/api/get-token" or method != "POST":
                return self._err(404, "no such auth route")
            if body.get("api_key") != API_KEY or body.get("api_password") != API_PASSWORD:
                return self._err(401, "bad credentials")
            self.tokens_minted += 1
            tok = f"tok-{self.tokens_minted}"
            self.valid_tokens = {tok}
            return httpx.Response(200, json={"data": [{"access_token": tok}]})
        if req.url.host != "pim.plytix.com":
            return self._err(404, "unexpected host " + req.url.host)
        if self.rate_limit_first > 0:
            self.rate_limit_first -= 1
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"message": "API rate limit exceeded", "limit": 50, "window_size": 10},
            )
        if not self._auth_ok(req):
            return self._err(401, "token expired or invalid")
        self._authed_calls += 1
        if self.expire_after is not None and self._authed_calls == self.expire_after:
            self.valid_tokens = set()  # the NEXT call 401s
        if method == "POST" and path == "/api/v1/attributes/product/search":
            body = json.loads(req.content or b"{}")
            pg = body.get("pagination") or {}
            page, size = int(pg.get("page", 1)), int(pg.get("page_size", 25))
            if size > 100:
                return self._err(422, "page_size must be <= 100")
            rows = [
                {
                    "id": self.ids.get(k, f"{i + 1:024x}"),
                    "label": k,
                    "name": self.names.get(k, k),
                    "type_class": v,
                }
                for i, (k, v) in enumerate(sorted(self.schema.items()))
            ]
            return httpx.Response(
                200, json={"data": rows[(page - 1) * size : page * size], "pagination": {}}
            )
        if method == "POST" and path == "/api/v2/products/search":
            body = json.loads(req.content or b"{}")
            cols = body.get("attributes") or []
            if len(cols) > 50:
                return self._err(422, "at most 50 attributes")
            for c in cols:
                if c.startswith("attributes.") and c[len("attributes.") :] not in self.schema:
                    return self._err(422, f"unknown attribute column {c}")
            pg = body.get("pagination") or {}
            page, size = int(pg.get("page", 1)), int(pg.get("page_size", 25))
            if size > 100:
                return self._err(422, "page_size must be <= 100")
            self.search_pages.append(page)
            items = list(self.products.values())
            for group in body.get("filters") or []:
                for f in group:
                    if f.get("field") == "sku" and f.get("operator") == "in":
                        items = [p for p in items if p["sku"] in f["value"]]
            rows = []
            for p in items[(page - 1) * size : page * size]:
                row = {"id": p["id"], "sku": p["sku"], "modified": p["modified"], "attributes": {}}
                for c in cols:
                    if c.startswith("attributes."):
                        lb = c[len("attributes.") :]
                        row["attributes"][lb] = p["attributes"].get(lb)
                rows.append(row)
            return httpx.Response(200, json={"data": rows, "pagination": {"page": page}})
        if path.startswith("/api/v2/products/"):
            ident = path[len("/api/v2/products/") :]
            p = self.products.get(ident)
            if method == "GET":
                if p is None:
                    return self._err(404, "not found")
                if self.get_fails_after_patch and self.patches:
                    return self._err(500, "read failed")
                row = copy.deepcopy(p)
                if self.get_without_attributes:
                    row.pop("attributes", None)
                return httpx.Response(200, json={"data": [row]})
            if method == "PATCH":
                if p is None:
                    return self._err(404, "not found")
                if self.patch_fail_status:
                    return self._err(self.patch_fail_status, "boom")
                if self.patch_fail_after is not None and len(self.patches) >= self.patch_fail_after:
                    return self._err(422, "write refused")
                body = json.loads(req.content or b"{}")
                if set(body) - {"label", "status", "attributes"}:
                    return self._err(422, "unknown field in body")
                for lb, val in (body.get("attributes") or {}).items():
                    t = self.schema.get(lb)
                    if t is None:
                        return self._err(422, f"attribute {lb} does not exist")
                    if t == "TextAttribute" and not (isinstance(val, str) or val is None):
                        return self._err(422, f"attribute {lb} expects text")
                if self.patch_mode == "replace":
                    p["attributes"] = copy.deepcopy(body.get("attributes") or {})  # WIPES the rest
                else:
                    p["attributes"].update(body.get("attributes") or {})  # PARTIAL merge
                if "label" in body:
                    p["label"] = body["label"]
                self.patches.append((ident, body))
                if self.on_patch is not None:
                    self.on_patch(p)
                return httpx.Response(200, json={"data": [{"id": ident}]})
        return self._err(404, f"unexpected {method} {path}")


def _conn(name, cfg_extra=None, mp=None):
    cfg = {
        "attribute_map": json.dumps(MAP if mp is None else mp),
        "source_locale": "en",
        "target_locales": "fr, de",
    }
    cfg.update(cfg_extra or {})
    return cstore.create("plytix", name, cfg, dict(SECRET), actor="admin")


def _pc(cfg=None, transport=None, secret=None):
    base = {"attribute_map": json.dumps(MAP), "source_locale": "en"}
    base.update(cfg or {})
    return PlytixConnector(base, secret or dict(SECRET), transport=transport)


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


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _raises(fn):
    try:
        fn()
    except cbase.ConnectorError:
        return True
    return False


def main() -> None:
    from app.web._routes import walk_routes

    init_db()
    cbase._sleep = lambda s: None  # backoff runs instantly
    cap = _Capture()
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.DEBUG)

    # ---- (1) capability, auth, test(), no secret leakage -----------------------
    assert PlytixConnector.capability.topology == "T1"
    fs = FakePlytix()
    ok, msg = _pc(transport=fs.transport()).test()
    assert ok, msg
    assert "Attribute map checked" in msg, msg
    bad_pw = {"api_key": API_KEY, "api_password": "wrong-password-ZZZ"}  # pregate: allow-secret
    ok, msg = _pc(transport=fs.transport(), secret=bad_pw).test()
    assert not ok and "rejected" in msg and "wrong-password-ZZZ" not in msg, msg
    ok, msg = _pc(transport=fs.transport(), secret={"api_key": "", "api_password": ""}).test()
    assert not ok and "required" in msg, msg
    print("  auth: token minted from key+password, bad creds refused without echoing them")

    # ---- (2) the attribute map is validated fail-closed (taxonomy A, operator config)
    bad_maps = [
        "not json",
        "[]",
        "{}",
        json.dumps({"name": "fr"}),
        json.dumps({"name": {}}),
        json.dumps({"../x": {"fr": "a_fr"}}),
        json.dumps({"name": {"fr": "../x"}}),
        json.dumps({"name": {"fr": "a/b"}}),
        json.dumps({"name": {"fr": "a?x=1"}}),
        json.dumps({"name": {"fr": "a#f"}}),
        json.dumps({"name": {"fr": "a\r\nb"}}),
        json.dumps({"name": {"fr": "a b"}}),
        json.dumps({"name": {"fr": "naiveé"}}),
        json.dumps({"name": {"fr": "sku"}}),  # system field as target
        json.dumps({"sku": {"fr": "x_fr"}}),  # system field as source
        json.dumps({"name": {"fr": "name"}}),  # target == its own source
        json.dumps(
            {"name": {"fr": "description"}, "description": {"fr": "d_fr"}}
        ),  # source as target
        json.dumps({"name": {"fr": "same"}, "description": {"fr": "same"}}),  # target used twice
        json.dumps({"name": {"fr": "n_fr", "de": "n_de"}, "description": {"fr": "d_fr"}}),  # ragged
        json.dumps({"name": {"..": "n_fr"}}),
        json.dumps({"name": {"fr FR": "n_fr"}}),
        json.dumps({"name": {"fr": 5}}),
        json.dumps({"name": {"fr": "Name"}}),  # target == source, different case
        json.dumps({"name": {"fr": "NAME"}}),
        json.dumps({"name": {"fr": "x_fr"}, "Name": {"fr": "y_fr"}}),  # source twice (case)
        json.dumps({"name": {"fr": "description"}, "Description": {"fr": "d_fr"}}),
        json.dumps({"name": {"fr": "D_FR"}, "description": {"fr": "d_fr"}}),  # target twice (case)
        json.dumps({"name": {"fr": "SKU"}}),  # system field, different case
        json.dumps({"name": {"fr": "x_fr", "FR": "x_frb"}}),  # locale repeated (case)
        json.dumps({"name": {"fr": "x_fr"}, " name": {"fr": "y_fr"}}),  # whitespace variant
        json.dumps({"name": {"fr": "x_fr "}}),
    ]
    for bm in bad_maps:
        assert _raises(lambda bm=bm: _pc({"attribute_map": bm}, fs.transport()).attribute_map()), bm
    too_many = {f"s{i}": {"fr": f"t{i}"} for i in range(25)}
    assert _raises(lambda: _pc({"attribute_map": json.dumps(too_many)}).attribute_map())
    ok_map = _pc({"attribute_map": MAP}).attribute_map()  # a dict is accepted too
    assert ok_map["name"]["fr"] == "name_fr"
    print(f"  A: {len(bad_maps) + 1} hostile/ragged/overlapping attribute maps refused")

    # ---- (3) G: the fake enforces real validation (payload tests bite) ----------
    fsg = FakePlytix()
    p1 = fsg.add(1, "SKU-1", {"name": "Hi"})
    pcg = _pc(transport=fsg.transport())
    assert pcg.patch_record(p1, {"name_fr": "Salut"}) == (True, "")
    ok, m = pcg.patch_record(p1, {"nope_fr": "x"})
    assert not ok and m.startswith("422"), m
    ok, m = pcg.patch_record(p1, {"name_fr": 5})
    assert not ok and m.startswith("422"), m
    print("  G: fake 422s an unknown attribute and a non-text value")

    # ---- (4) discovery: pagination, cap, SKU scope, requested columns -----------
    fs4 = FakePlytix()
    for i in range(1, 251):
        fs4.add(i, f"SKU-{i}", {"name": f"n{i}", "description": f"d{i}", "weight": 3})
    got = _pc({"max_entries": "250"}, fs4.transport()).discover_articles()
    assert len(got) == 250 and fs4.search_pages == [1, 2, 3], (len(got), fs4.search_pages)
    # only the mapped attribute columns are read: no 'weight' leaks into the record values
    assert "weight" not in got[0]["values"] and set(got[0]["values"]) == set(_pc()._all_labels())
    fs4.search_pages.clear()
    got = _pc({"max_entries": "7"}, fs4.transport()).discover_articles()
    assert len(got) == 7, len(got)
    scoped = _pc({"skus": "SKU-3, SKU-9"}, fs4.transport()).discover_articles()
    assert sorted(a["sku"] for a in scoped) == ["SKU-3", "SKU-9"], scoped
    print("  E: pagination walks every page, cap honoured, SKU scope applied, mapped columns only")

    # ---- (5) full flow CREATE: partial PATCH of targets only, source untouched --
    fs5 = FakePlytix()
    fs5.add(5, "SKU-5", {"name": "Hello", "description": "World", "weight": 2})
    c5 = _conn("create", {"target_locales": "fr"})
    ok, msg, s5 = cstore.plytix_translate(c5.id, actor="auto", transport=fs5.transport())
    assert ok, msg
    assert s5["created"] == 1 and s5["updated"] == 0, s5
    assert len(fs5.patches) == 1, fs5.patches
    _id, body = fs5.patches[0]
    assert set(body) == {"attributes"} and set(body["attributes"]) == {
        "name_fr",
        "description_fr",
    }, body
    stored = fs5.attrs_of(5)
    assert stored["name"] == "Hello" and stored["description"] == "World", (
        "source survives",
        stored,
    )
    assert stored["weight"] == 2, "unrelated attribute survives the partial merge"
    assert stored["name_fr"] and stored["name_fr"] != "Hello"
    print("  flow CREATE: only the two fr targets PATCHed; source + other attributes untouched")

    # ---- (6) A: hostile / foreign ids, zero writes -------------------------------
    fs6 = FakePlytix()
    fs6.add(6, "SKU-IN", {"name": "Hello"})
    fs6.add(7, "SKU-OUT", {"name": "Secret"})
    c6 = _conn("scope", {"target_locales": "fr", "skus": "SKU-IN"})
    hostile = [
        "..",
        "../x",
        "%2e%2e%2f",
        "a/b",
        "a?x=1",
        "a#f",
        "a\x00b",
        "a\r\nb",
        "a b",
        "5f000000é",
        "",
        "x" * 65,
        pid(6) + "/..",
        pid(6)[:10],  # a PREFIX of a real id
    ]
    for bad in hostile:
        fs6.patches.clear()
        fs6.requests.clear()
        ok, msg, _s = cstore.plytix_translate(
            c6.id, article_ids=[bad], actor="attacker", transport=fs6.transport()
        )
        assert not fs6.patches, (repr(bad), "ZERO external writes", fs6.patches)
        assert not any(m == "PATCH" for m, _p in fs6.requests), repr(bad)
        assert not any(".." in p for _m, p in fs6.requests), (repr(bad), fs6.requests)
    # a real product OUTSIDE the configured SKUs answers like a missing one
    fs6.patches.clear()
    ok, msg, s6 = cstore.plytix_translate(
        c6.id, article_ids=[pid(7)], actor="a", transport=fs6.transport()
    )
    assert not fs6.patches and fs6.attrs_of(7) == {"name": "Secret"}, "foreign product untouched"
    ok2, msg2, _ = cstore.plytix_translate(
        c6.id, article_ids=[pid(99)], actor="a", transport=fs6.transport()
    )
    assert (ok, msg) == (ok2, msg2), ("foreign id == missing id", msg, msg2)
    # the in-scope id works
    ok, msg, s6 = cstore.plytix_translate(
        c6.id, article_ids=[pid(6)], actor="a", transport=fs6.transport()
    )
    assert ok and s6["created"] == 1, (msg, s6)
    print(
        f"  A: {len(hostile)} hostile ids refused, zero writes; foreign id acts like a missing one"
    )

    # ---- (7) B/F: all held back -> nothing written, stored translation survives --
    import app.services.translate as T

    fs7 = FakePlytix()
    fs7.add(
        1,
        "SKU-1",
        {
            "name": "Hello",
            "description": "World",
            "name_fr": "Bonjour(humain)",
            "description_fr": "Monde(humain)",
        },
    )
    c7 = _conn("held", {"target_locales": "fr"})
    orig, patched = _needs_review(T)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s7 = cstore.plytix_translate(c7.id, actor="auto", transport=fs7.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert not fs7.patches, ("all held back -> NO write", fs7.patches)
    assert fs7.attrs_of(1)["name_fr"] == "Bonjour(humain)", "B: the human translation survives"
    assert (
        s7["held_back"] == 1 and s7["created"] == 0 and s7["updated"] == 0 and not s7["failures"]
    ), s7
    ok2, _m2, s72 = cstore.plytix_translate(c7.id, actor="auto", transport=fs7.transport())
    assert ok2 and s72["updated"] == 1 and s72["skipped"] == 0, ("F: no hash was recorded", s72)
    assert fs7.patches, "the healthy re-run wrote"
    print(
        "  B/F: NEEDS_REVIEW -> nothing written, translation kept, no hash, healthy re-run writes"
    )

    # ---- (8) B partial: one attribute held, the held target keeps its value ------
    fs8 = FakePlytix()
    fs8.add(1, "SKU-1", {"name": "Hello", "description": "World", "name_fr": "NomHumain"})
    c8 = _conn("partial", {"target_locales": "fr"})
    orig, patched = _needs_review(T, only_sources={"Hello"})
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s8 = cstore.plytix_translate(c8.id, actor="auto", transport=fs8.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    for _i, body in fs8.patches:
        assert set(body["attributes"]) == {"description_fr"}, ("held name_fr omitted", body)
    assert fs8.attrs_of(1)["name_fr"] == "NomHumain", "held target untouched"
    assert s8["incomplete"] and s8["incomplete"][0]["held_fields"] == ["name"], s8["incomplete"]
    assert s8.get("partial_held") == 1, s8
    # not hashed: next healthy run re-attempts
    ok, _m, s82 = cstore.plytix_translate(c8.id, actor="auto", transport=fs8.transport())
    assert s82["updated"] == 1 and s82["skipped"] == 0, s82
    print("  B: partial hold-back writes description_fr only, keeps name_fr, reports incomplete")

    # ---- (9) #640: never CREATE a language while a unit is held ------------------
    fs9 = FakePlytix()
    fs9.add(1, "SKU-1", {"name": "Hello", "description": "World"})
    c9 = _conn("nocreate", {"target_locales": "fr"})
    orig, patched = _needs_review(T, only_sources={"Hello"})
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s9 = cstore.plytix_translate(c9.id, actor="auto", transport=fs9.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert not fs9.patches, ("no half-filled language is created", fs9.patches)
    assert s9["held_back"] == 1 and s9["created"] == 0, s9
    print("  #640: a missing language is not created while one of its attributes is held")

    # ---- (10) C: second-run steady state; edited source re-translates ------------
    fs10 = FakePlytix()
    fs10.add(1, "SKU-1", {"name": "Hello", "description": "World"})
    c10 = _conn("steady", {"target_locales": "fr"})
    ok, _m, r1 = cstore.plytix_translate(c10.id, actor="auto", transport=fs10.transport())
    assert ok and r1["created"] == 1, r1
    fs10.patches.clear()
    ok, _m, r2 = cstore.plytix_translate(c10.id, actor="auto", transport=fs10.transport())
    assert ok and r2["skipped"] == 1 and r2["created"] == 0 and r2["updated"] == 0, r2
    assert not fs10.patches, "run 2 wrote nothing"
    fs10.products[pid(1)]["attributes"]["name"] = "Hello EDITED"
    ok, _m, r3 = cstore.plytix_translate(c10.id, actor="auto", transport=fs10.transport())
    assert ok and r3["updated"] == 1 and r3["skipped"] == 0, ("edited source re-translates", r3)
    # a half-filled language (target cleared) is re-attempted even with an unchanged hash
    fs10.products[pid(1)]["attributes"]["description_fr"] = None
    fs10.patches.clear()
    ok, _m, r4 = cstore.plytix_translate(c10.id, actor="auto", transport=fs10.transport())
    assert r4["skipped"] == 0 and fs10.patches, ("a cleared target is re-filled", r4)
    print("  C: run 2 skipped; edited source re-translated; a cleared target is re-filled")

    # ---- (11) created vs updated -------------------------------------------------
    fs11 = FakePlytix()
    fs11.add(1, "SKU-1", {"name": "Hello", "name_fr": "Vieux"})
    c11 = _conn("cvu", {"target_locales": "fr"})
    ok, _m, s11 = cstore.plytix_translate(c11.id, actor="auto", transport=fs11.transport())
    assert ok and s11["updated"] == 1 and s11["created"] == 0, s11
    print("  created/updated: a pre-existing target is reported as UPDATED")

    # ---- (12) D: schema-driven selection, both directions, loud config failures --
    fs12 = FakePlytix()
    fs12.add(1, "SKU-1", {"name": "Hello", "description": "", "notes_html": "<p>x</p>"})
    mp12 = {
        "name": {"fr": "name_fr"},
        "description": {"fr": "description_fr"},
        "notes_html": {"fr": "name_de"},
    }
    c12 = _conn("select", {"target_locales": "fr"}, mp=mp12)
    ok, msg, s12 = cstore.plytix_translate(c12.id, actor="auto", transport=fs12.transport())
    assert ok and s12["created"] == 1, (msg, s12)
    assert s12["fields_selected"] == ["name"], s12["fields_selected"]
    assert "not plain text" in s12["fields_skipped"]["notes_html"], s12["fields_skipped"]
    assert "no non-empty source" in s12["fields_skipped"]["description"], s12["fields_skipped"]
    assert set(fs12.patches[0][1]["attributes"]) == {"name_fr"}, fs12.patches
    for bad_map, why in [
        ({"nonexistent": {"fr": "name_fr"}}, "does not exist"),
        ({"name": {"fr": "nonexistent_fr"}}, "does not exist"),
        ({"name": {"fr": "weight"}}, "not TextAttribute"),
        ({"name": {"fr": "notes_html"}}, "not TextAttribute"),
    ]:
        fsb = FakePlytix()
        fsb.add(1, "SKU-1", {"name": "Hello"})
        cb = _conn("badcfg", {"target_locales": "fr"}, mp=bad_map)
        ok, msg, _s = cstore.plytix_translate(cb.id, actor="auto", transport=fsb.transport())
        assert not ok and why in msg and not fsb.patches, (bad_map, ok, msg)
        with Session(engine) as s:
            assert s.get(Connection, cb.id).status == "error"
    fse = FakePlytix(schema={})
    fse.add(1, "SKU-1", {"name": "Hello"})
    ok, msg, _s = cstore.plytix_translate(
        _conn("noschema", {"target_locales": "fr"}).id, actor="a", transport=fse.transport()
    )
    assert not ok and "no product attributes" in msg and not fse.patches, msg
    with Session(engine) as s:
        assert s.exec(select(SyncLog)).all(), "failures leave a SyncLog row"
    print("  D: html/empty sources skipped + REPORTED; missing/mistyped attributes fail loud")

    # ---- (13) source attribute is never written (defence in depth) ---------------
    pc13 = _pc()
    built = pc13.build_values({"name": "Bonjour", "description": "Monde", "weight": "x"}, "fr")
    assert built == {"name_fr": "Bonjour", "description_fr": "Monde"}, built
    assert pc13.build_values({"name": "x"}, "xx") == {}, "unmapped language writes nothing"
    assert pc13.build_values({"name": "   "}, "fr") == {}, "blank text writes nothing"
    pc13._map_cache = {
        "name": {"fr": "name"}
    }  # a corrupted in-memory map still cannot hit a source
    assert pc13.build_values({"name": "x"}, "fr") == {}, "target that is also a source refused"
    print("  B: build_values never emits a source attribute, an unmapped language or blank text")

    # ---- (14) language partition: rejected + dropped-as-source -------------------
    fs14 = FakePlytix()
    fs14.add(1, "SKU-1", {"name": "Hello", "description": "World"})
    c14 = _conn(
        "reject", {"target_locales": "fr, es, en"}, mp={"name": {"fr": "name_fr", "en": "name_de"}}
    )
    ok, msg, s14 = cstore.plytix_translate(c14.id, actor="auto", transport=fs14.transport())
    assert ok and s14["locales"] == ["fr"] and s14["rejected"] == ["es"], s14
    assert s14["dropped_source"] == ["en"] and "equal to the source language" in msg, (s14, msg)
    assert set(fs14.patches[0][1]["attributes"]) == {"name_fr"}, fs14.patches
    print("  partition: 'es' rejected (not in map), 'en' dropped as the source language")

    # ---- (15) failures: 422 recorded, no hash; 5xx on PATCH retried safely -------
    fs15 = FakePlytix()
    fs15.add(1, "SKU-1", {"name": "Hello", "description": "World"})
    fs15.patch_fail_status = 422
    c15 = _conn("fail", {"target_locales": "fr"})
    ok, msg, s15 = cstore.plytix_translate(c15.id, actor="auto", transport=fs15.transport())
    assert not ok and s15["failures"] and s15["created"] == 0, (ok, s15)
    fs15.patch_fail_status = None
    ok, _m, s152 = cstore.plytix_translate(c15.id, actor="auto", transport=fs15.transport())
    assert ok and s152["created"] == 1, ("a failure records no hash; the next run writes", s152)
    print("  failure: a PATCH 422 is a real failure with no hash; the next run writes")

    # ---- (16) H: rate limit via base._request, token expiry mid-run ---------------
    fs16 = FakePlytix(rate_limit_first=2)
    fs16.add(1, "SKU-1", {"name": "Hello", "description": "World"})
    c16 = _conn("ratelimit", {"target_locales": "fr"})
    ok, msg, s16 = cstore.plytix_translate(c16.id, actor="auto", transport=fs16.transport())
    assert ok and s16["created"] == 1, ("429s retried by base._request", msg, s16)
    fs16b = FakePlytix(expire_after=2)
    for n in range(1, 4):
        fs16b.add(n, f"SKU-{n}", {"name": f"n{n}", "description": f"d{n}"})
    c16b = _conn("expiry", {"target_locales": "fr"})
    ok, msg, s16b = cstore.plytix_translate(c16b.id, actor="auto", transport=fs16b.transport())
    assert ok and s16b["created"] == 3 and fs16b.tokens_minted == 2, (s16b, fs16b.tokens_minted)
    print("  H: 429 retried via base._request; an expired token is re-minted once")

    # ---- (17) H: WriteBudget ceiling ---------------------------------------------
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fs17 = FakePlytix()
        fs17.add(1, "SKU-1", {"name": "Hello"})
        c17 = _conn("budget", {"target_locales": "fr, de"})
        ok, _m, s17 = cstore.plytix_translate(c17.id, actor="auto", transport=fs17.transport())
        assert ok and s17["ceiling_hit"] and (s17["created"] + s17["updated"]) == 1, s17
        assert len(fs17.patches) == 1, fs17.patches
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    print("  H: WriteBudget MAX_WRITES_PER_RUN=1 stops after exactly one external write")

    # ---- (18) H: egress guard ----------------------------------------------------
    from app.services.connectors import egress_guard

    fs18 = FakePlytix()
    fs18.add(1, "SKU-1", {"name": "Hello"})
    c18 = _conn("egress", {"target_locales": "fr"})
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s = cstore.plytix_translate(c18.id, actor="a", transport=fs18.transport())
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fs18.patches, msg
    print("  H: mock MT + real egress refused, no external writes")

    # ---- (19) probe on a missing product -----------------------------------------
    pc19 = _pc(transport=FakePlytix().transport())
    assert pc19.strategy.exists_target(pc19, pid(404), "fr") is False
    assert pc19.live_values(pid(404)) is ABSENT
    print("  probe: a missing product -> exists_target False, live_values ABSENT")

    # ---- (19b) MERGE CANARY: a REPLACE-semantics PATCH can never wipe a product ------
    from app.services.connectors import plytix as _ply

    canary_calls: list = []
    real_canary = _ply.PlytixConnector._canary_patch

    def counting_canary(self, record_id, values):
        canary_calls.append(record_id)
        return real_canary(self, record_id, values)

    _ply.PlytixConnector._canary_patch = counting_canary

    def _sem(cid):
        with Session(engine) as s:
            return (s.get(Connection, cid).config or {}).get("plytix_patch_semantics") or {}

    full = {
        "name": "Hello",
        "description": "World",
        "weight": 2,
        "color": "red",
        "notes_html": "<p>keep</p>",
        "name_de": "Hallo(human)",
    }
    try:
        # MERGE fake: the first write of run 1 is the canary; it proves merge semantics, the
        # rest of the run and every later run write without it.
        fm = FakePlytix()
        for n in (1, 2, 3):
            fm.add(n, f"SKU-{n}", dict(full))
        cm = _conn("canary-merge", {"target_locales": "fr"})
        canary_calls.clear()
        ok, msg, sm = cstore.plytix_translate(cm.id, actor="auto", transport=fm.transport())
        assert ok and sm["created"] == 3 and sm["safety_stop"] is None, (msg, sm)
        assert canary_calls == [pid(1)], ("exactly one canary, on the first write", canary_calls)
        assert _sem(cm.id).get("merge_proven") and not _sem(cm.id).get("replace_detected")
        for n in (1, 2, 3):
            got = fm.attrs_of(n)
            assert all(got[k] == v for k, v in full.items()), ("merge keeps everything", got)
        for n in (1, 2, 3):
            fm.products[pid(n)]["attributes"]["name"] = f"Hello {n} EDITED"
        canary_calls.clear()
        ok, msg, sm2 = cstore.plytix_translate(cm.id, actor="auto", transport=fm.transport())
        assert ok and sm2["updated"] == 3 and canary_calls == [], ("proven: no canary", sm2)
        print("  canary MERGE: one canary on run 1 proves merge; later writes and runs skip it")

        # REPLACE fake: the canary write wipes product 1, the run stops at once, product 1 is
        # restored from the pre-write snapshot, products 2 and 3 are never touched, and the
        # next run refuses to write at all.
        fr = FakePlytix(patch_mode="replace")
        for n in (1, 2, 3):
            fr.add(n, f"SKU-{n}", dict(full))
        cr = _conn("canary-replace", {"target_locales": "fr, de"})
        ok, msg, sr = cstore.plytix_translate(cr.id, actor="auto", transport=fr.transport())
        assert not ok and "SAFETY STOP" in msg and "REPLACES" in msg, msg
        stop = sr["safety_stop"]
        assert stop and stop["product"] == pid(1), sr
        assert set(stop["lost"]) == set(full) - set(fr.patches[0][1]["attributes"]), stop
        assert stop["not_restored"] == {} and stop["restored"] == stop["lost"], stop
        assert [i for i, _b in fr.patches] == [pid(1), pid(1)], (
            "canary + restore only",
            fr.patches,
        )
        restored = fr.attrs_of(1)
        assert all(restored[k] == v for k, v in full.items() if k != "name_de"), restored
        assert restored["name"] == "Hello" and restored["description"] == "World", restored
        assert fr.attrs_of(2) == full and fr.attrs_of(3) == full, "later products untouched"
        assert sr["halted_skipped"] == 2 and sr["created"] == 0, sr
        assert _sem(cr.id).get("replace_detected") and not _sem(cr.id).get("merge_proven")
        with Session(engine) as s:
            assert s.get(Connection, cr.id).status == "error"
        assert any("SAFETY STOP" in ln for ln in cap.lines), "logged loudly"
        fr.patches.clear()
        ok, msg, _s = cstore.plytix_translate(cr.id, actor="auto", transport=fr.transport())
        assert not ok and "SAFETY STOP" in msg and not fr.patches, ("refused after", msg)
        print(
            "  canary REPLACE: stops at write 1, product restored, others untouched, then refused"
        )

        # REPLACE + the restore itself refused: the loss is reported with the old values.
        fx = FakePlytix(patch_mode="replace")
        fx.add(1, "SKU-1", dict(full))
        fx.patch_fail_after = 1
        cx = _conn("canary-norestore", {"target_locales": "fr"})
        ok, msg, sx = cstore.plytix_translate(cx.id, actor="auto", transport=fx.transport())
        assert not ok and "NOT restored" in msg, msg
        assert sx["safety_stop"]["not_restored"]["weight"] == 2, sx["safety_stop"]
        assert _sem(cx.id).get("replace_detected")
        print("  canary REPLACE + failed restore: loss reported with the old values to restore")

        # The re-read after the canary write fails: fail closed (stop, check by hand).
        fg = FakePlytix()
        for n in (1, 2):
            fg.add(n, f"SKU-{n}", dict(full))
        fg.get_fails_after_patch = True
        cg = _conn("canary-noread", {"target_locales": "fr"})
        ok, msg, _sg = cstore.plytix_translate(cg.id, actor="auto", transport=fg.transport())
        assert not ok and "SAFETY STOP" in msg and "by hand" in msg, msg
        assert len(fg.patches) == 1 and not _sem(cg.id).get("merge_proven"), fg.patches
        print("  canary: an unreadable product after the write stops the run (fail closed)")

        # Nothing to watch (GET carries no attributes): the canary refuses to write at all.
        fw = FakePlytix()
        fw.add(1, "SKU-1", dict(full))
        fw.get_without_attributes = True
        cw = _conn("canary-blind", {"target_locales": "fr"})
        ok, msg, _sw = cstore.plytix_translate(cw.id, actor="auto", transport=fw.transport())
        assert not ok and not fw.patches and "could not be verified" in msg, msg
        print("  canary: no attribute to watch -> nothing written")

        # A value the write did not name CHANGES during it (a concurrent edit): stop, no
        # restore (it may be a person's edit), merge not recorded as proven.
        fc = FakePlytix()
        for n in (1, 2):
            fc.add(n, f"SKU-{n}", dict(full))
        fc.on_patch = lambda p: p["attributes"].__setitem__("color", "blue")
        cc = _conn("canary-changed", {"target_locales": "fr"})
        ok, msg, sc = cstore.plytix_translate(cc.id, actor="auto", transport=fc.transport())
        assert not ok and "SAFETY STOP" in msg and sc["safety_stop"]["changed"] == ["color"], msg
        assert len(fc.patches) == 1 and fc.attrs_of(1)["color"] == "blue", fc.patches
        assert not _sem(cc.id).get("merge_proven") and not _sem(cc.id).get("replace_detected")
        print("  canary: a value changed under the write stops the run without a restore")
    finally:
        _ply.PlytixConnector._canary_patch = real_canary

    # ---- (19c) A: every alias Plytix might accept for an attribute is checked ---------
    alias_schema = dict(_SCHEMA)
    alias_schema.update(
        {"desc_fr": "TextAttribute", "name-fr": "TextAttribute", "Name_DE": "TextAttribute"}
    )
    for names, ids, amap, why in [
        # target label == the SOURCE attribute's display name
        ({"description": "desc_fr"}, {}, {"description": {"fr": "desc_fr"}}, "SOURCE"),
        # same, display name with spaces and capitals ('Desc FR' ~ desc_fr)
        ({"description": "Desc FR"}, {}, {"description": {"fr": "desc_fr"}}, "SOURCE"),
        # target label == the SOURCE attribute's id
        ({}, {"description": "desc_fr"}, {"description": {"fr": "desc_fr"}}, "SOURCE"),
        # target label also reads as another attribute's label (hyphen vs underscore)
        ({}, {}, {"name": {"fr": "name-fr"}}, "another attribute"),
        # target label differs from another attribute's label only by case
        ({}, {}, {"name": {"de": "name_de"}}, "another attribute"),
    ]:
        fa = FakePlytix(schema=alias_schema, names=names, ids=ids)
        fa.add(1, "SKU-1", {"name": "Hello", "description": "World"})
        ca = _conn("alias", {"target_locales": "fr, de"}, mp=amap)
        ok, msg, _s = cstore.plytix_translate(ca.id, actor="auto", transport=fa.transport())
        assert not ok and why in msg and not fa.patches, (names, ids, amap, msg)
        ok, msg = _pc({"attribute_map": json.dumps(amap)}, fa.transport()).test()
        assert not ok and why in msg, msg
    print("  A: a target that aliases a source (name, id, case, spacing) is refused before writing")

    # ---- (20) H: credentials and token never logged or returned -------------------
    blob = "\n".join(cap.lines)
    for secret in (API_KEY, API_PASSWORD, "tok-1", "tok-2"):
        assert secret not in blob, f"secret leaked into a log line: {secret}"
    assert API_KEY not in json.dumps(s5, default=str) and "tok-" not in json.dumps(s5, default=str)
    print("  H: api key, password and bearer token never appear in logs or results")

    # ---- (21) H: parity surfaces + harvest mapper ---------------------------------
    from app.services.connectors import REGISTRY
    from app.services.connectors import harvest as _hv

    assert "plytix" in REGISTRY
    c = TestClient(app)
    assert "Plytix" in c.get("/connectors").text
    from app.cli.__main__ import build_parser

    assert "connector plytix-translate" in _leaf_paths(build_parser())
    from starlette.routing import Route

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/plytix/translate" in api_paths
    assert _hv._map_plytix({"title": "Mug"})["source"].get("title") == "Mug"
    print("  H parity: REGISTRY + /connectors tile + /api route + CLI leaf + harvest mapper")

    print("verify_plytix OK")


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
