"""Contentful connector + the T1 field-level-map seam — fully offline via
httpx.MockTransport (a fake Contentful CMA that models the fields.{id}.{locale}
document, X-Contentful-Version optimistic concurrency, and the CMA validators).

Contentful is the SECOND hand-built connector-factory connector (Strapi was #1) and a
T1 (field-level map) connector whose write is a version-locked read-merge-write of the
ONE entry that carries all locales inline.

The fake is deliberately STRICT so the assertions are non-vacuous (taxonomy G): a PUT
with the wrong X-Contentful-Version -> 409; a PUT of an unknown field -> 422; a PUT
that sets a non-DEFAULT locale on a NON-localized field -> 422. Under those rules the
happy-path writes still pass, which is what makes the payload assertions meaningful.

Covers the connector-build defect taxonomy (the connector build blueprint §3):
  A  caller/operator strings can't escape the CMA URL or scope (bad entry ids + a
     non-configured content type refused end-to-end and at the client seam)
  B  a held-back (NEEDS_REVIEW) field never overwrites a stored translation, on create
     AND update (only this-run's translated delta is applied)
  C  read/probe/write target the same state -> the SECOND run suppresses (no
     re-translate loop)
  D  schema-driven translatability: localized Symbol/Text/RichText selected (Rich Text
     has its own suite, verify_contentful_richtext); Object / non-text / non-localized
     skipped and REPORTED
  E  the write cap is distributed round-robin across content types (no starvation)
  F  an all-held-back run records NO content-hash, so a later healthy run re-attempts
  G  the fake enforces Contentful's validation semantics (409 / 422)
  H  the standing repo-law sweep: WriteBudget ceiling, mock-egress guard, ASCII CLI

Plus: version-conflict refetch-remerge-retry (and abort-after-exhaustion, no hash),
content-hash suppression, locale-case round-trip, metadata (tags) preservation,
pagination, empty skip, parity surfaces, org isolation.

ASCII-only prints; final line is 'verify_contentful OK'.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_contentful_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import Connection, Segment, SegStatus
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.contentful import (
    ContentfulConnector,
    ContentfulEntryStrategy,
)


# ============================================================================
# Fake Contentful CMA. Models the entry document (fields.{id}.{locale}), the
# content-type schema (localized flag + type), X-Contentful-Version concurrency
# (wrong version -> 409), and CMA field validation (unknown field or a non-default
# locale on a non-localized field -> 422).
# ============================================================================
class FakeContentful:
    def __init__(self, space="sp", env="master", locales=None):
        self.space = space
        self.env = env
        self.locales = locales or [
            {"code": "en-US", "name": "English", "default": True},
            {"code": "de-DE", "name": "German"},
            {"code": "fr", "name": "French"},
        ]
        self.content_types: dict = {}  # ctid -> {"displayField", "fields":[{id,type,localized}]}
        self.entries: dict = {}  # eid -> {"sys", "fields", "metadata"?}
        self.puts: list = []  # (eid, fields, metadata) recorded on every accepted PUT
        self.always_conflict: set = set()  # eids that 409 on EVERY put (exhaustion test)
        self.conflict_once: set = set()  # eids that 409 exactly once (retry test)
        self.conflict_reset = None  # X-Contentful-RateLimit-Reset on 409s (F6)

    # ---- seeding --------------------------------------------------------
    def add_ct(self, ctid, fields, display=None):
        self.content_types[ctid] = {"displayField": display, "fields": fields}

    def add_entry(
        self, eid, ctid, fields, version=1, updatedAt="2026-07-05T10:00:00Z", metadata=None
    ):
        e = {
            "sys": {
                "id": eid,
                "version": version,
                "updatedAt": updatedAt,
                "contentType": {"sys": {"id": ctid}},
            },
            "fields": fields,
        }
        if metadata is not None:
            e["metadata"] = metadata
        self.entries[eid] = e

    def transport(self):
        return httpx.MockTransport(self._handle)

    # ---- helpers --------------------------------------------------------
    def _default_locale(self):
        for l in self.locales:  # noqa: E741
            if l.get("default"):
                return l["code"]
        return self.locales[0]["code"]

    def _fielddefs(self, ctid):
        return {f["id"]: f for f in self.content_types.get(ctid, {}).get("fields", [])}

    def _entry_ct(self, e):
        return e["sys"]["contentType"]["sys"]["id"]

    def _conflict(self, msg):
        headers = {}
        if self.conflict_reset is not None:
            headers["X-Contentful-RateLimit-Reset"] = str(self.conflict_reset)
        return httpx.Response(409, json={"message": msg}, headers=headers)

    def _validate_write_fields(self, ctid, fields):
        """CMA-shaped validation (makes the payload tests non-vacuous): unknown field ->
        422; a non-default locale on a non-localized field -> 422; a Symbol value over
        256 chars or over a field size.max (or under size.min) -> 422. Contentful rejects
        the WHOLE entry PUT on any of these, which is exactly why the connector must hold
        the offending field back per-field (review H2)."""
        defs = self._fielddefs(ctid)
        default = self._default_locale()
        for fid, locmap in (fields or {}).items():
            if fid not in defs:
                return httpx.Response(422, json={"message": f"Unknown field: {fid}"})
            if not isinstance(locmap, dict):
                return httpx.Response(422, json={"message": f"Field {fid} not a locale map"})
            d = defs[fid]
            if not d.get("localized"):
                for loc in locmap:
                    if loc != default:
                        return httpx.Response(
                            422,
                            json={
                                "message": f"Field {fid} is not localized; cannot set locale {loc}"
                            },
                        )
            for loc, val in locmap.items():
                if not isinstance(val, str):
                    continue
                if d.get("type") == "Symbol" and len(val) > 256:
                    return httpx.Response(
                        422,
                        json={
                            "message": f"Field {fid}/{loc} exceeds the Symbol 256 limit ({len(val)})"  # noqa: E501
                        },
                    )
                for v in d.get("validations") or []:
                    size = v.get("size") or {}
                    if isinstance(size.get("max"), int) and len(val) > size["max"]:
                        return httpx.Response(
                            422,
                            json={
                                "message": f"Field {fid}/{loc} exceeds size.max {size['max']} ({len(val)})"  # noqa: E501
                            },
                        )
                    if isinstance(size.get("min"), int) and len(val) < size["min"]:
                        return httpx.Response(
                            422,
                            json={
                                "message": f"Field {fid}/{loc} below size.min {size['min']} ({len(val)})"  # noqa: E501
                            },
                        )
        return None

    def _handle(self, req):
        path = req.url.path
        method = req.method
        params = req.url.params
        prefix = f"/spaces/{self.space}/environments/{self.env}"
        if not path.startswith(prefix):
            return httpx.Response(404, json={"message": "not this space/env"})
        rest = path[len(prefix) :]

        if method == "GET" and rest == "/locales":
            return httpx.Response(200, json={"items": self.locales})

        if method == "GET" and rest.startswith("/content_types/"):
            ctid = rest[len("/content_types/") :]
            ct = self.content_types.get(ctid)
            if not ct:
                return httpx.Response(404, json={"message": "no such content type"})
            return httpx.Response(
                200,
                json={
                    "sys": {"id": ctid},
                    "displayField": ct["displayField"],
                    "fields": ct["fields"],
                },
            )

        if method == "GET" and rest == "/entries":
            ctid = params.get("content_type")
            skip = int(params.get("skip") or 0)
            limit = int(params.get("limit") or 100)
            items = [e for e in self.entries.values() if self._entry_ct(e) == ctid]
            items.sort(key=lambda e: e["sys"].get("updatedAt") or "", reverse=True)
            total = len(items)
            return httpx.Response(
                200,
                json={
                    "items": items[skip : skip + limit],
                    "total": total,
                    "skip": skip,
                    "limit": limit,
                },
            )

        if rest.startswith("/entries/"):
            eid = rest[len("/entries/") :]
            e = self.entries.get(eid)
            if method == "GET":
                if not e:
                    return httpx.Response(404, json={"message": "no such entry"})
                return httpx.Response(200, json=e)
            if method == "PUT":
                if not e:
                    return httpx.Response(404, json={"message": "no such entry"})
                ver = req.headers.get("X-Contentful-Version")
                if ver is None or int(ver) != e["sys"]["version"]:
                    return self._conflict("VersionMismatch")
                if eid in self.always_conflict:
                    e["sys"]["version"] += 1  # simulate an external edit every time
                    return self._conflict("VersionMismatch (raced)")
                if eid in self.conflict_once:
                    self.conflict_once.discard(eid)
                    e["sys"]["version"] += 1  # one external edit, then let it settle
                    return self._conflict("VersionMismatch (raced once)")
                body = json.loads(req.content or b"{}")
                fields = body.get("fields") or {}
                bad = self._validate_write_fields(self._entry_ct(e), fields)
                if bad is not None:
                    return bad
                e["fields"] = fields
                # STRICT (review M2): store EXACTLY what the PUT carried. A PUT that omits
                # metadata CLEARS it — so the tags-preservation test only passes if the
                # connector actually echoes metadata back (no vacuous merge).
                e["metadata"] = body.get("metadata")
                e["sys"]["version"] += 1
                self.puts.append((eid, fields, body.get("metadata")))
                return httpx.Response(200, json=e)

        return httpx.Response(404, json={"message": f"unexpected {method} {rest}"})


# --- standard schema. Covers every skip class the taxonomy-D + H1 review demands:
#   title (Symbol, localized)         -> selected
#   body  (Text, localized)           -> selected
#   hero  (RichText, localized)       -> an EMPTY document: nothing to translate (#648)
#   views (Integer, localized)        -> skipped by TYPE
#   slug  (Symbol, LOCALIZED)         -> skipped by NAME heuristic (H1: localized slugs
#                                        are normal Contentful modelling, must NOT be MT'd)
#   canonicalUrl (Symbol, localized)  -> skipped by VALUE (URL-shaped)
#   internalCode (Symbol, NOT local.) -> skipped by the localized gate
def _std_fields():
    return [
        {"id": "title", "type": "Symbol", "localized": True},
        {"id": "body", "type": "Text", "localized": True},
        {"id": "hero", "type": "RichText", "localized": True},
        {"id": "views", "type": "Integer", "localized": True},
        {"id": "slug", "type": "Symbol", "localized": True},
        {"id": "canonicalUrl", "type": "Symbol", "localized": True},
        {"id": "internalCode", "type": "Symbol", "localized": False},
    ]


def _seed_std(fs, ctid="blogPost", eid="e1", version=1, extra_locales=None, metadata=None):
    fs.add_ct(ctid, _std_fields(), display="title")
    fields = {
        "title": {"en-US": "Hello world"},
        "body": {"en-US": "Some prose."},
        "hero": {"en-US": {"nodeType": "document", "content": []}},
        "views": {"en-US": 42},
        "slug": {"en-US": "hello-world"},
        "canonicalUrl": {"en-US": "https://acme.com/hello"},
        "internalCode": {"en-US": "PRD-1"},
    }
    for loc, vals in (extra_locales or {}).items():
        for fid, v in vals.items():
            fields.setdefault(fid, {})[loc] = v
    fs.add_entry(eid, ctid, fields, version=version, metadata=metadata)


def _conn(name, cfg_extra=None):
    cfg = {
        "space_id": "sp",
        "environment": "master",
        "region": "US",
        "content_types": "blogPost",
        "source_locale": "en-US",
        "target_locales": "de-DE, fr",
    }
    cfg.update(cfg_extra or {})
    return cstore.create("contentful", name, cfg, {"cma_token": "CFPAT-x"}, actor="admin")


def _cc(fs, cfg=None):
    base = {
        "space_id": "sp",
        "environment": "master",
        "region": "US",
        "content_types": "blogPost",
        "source_locale": "en-US",
    }
    base.update(cfg or {})
    return ContentfulConnector(base, {"cma_token": "CFPAT-x"}, transport=fs.transport())


def _flip(mod, predicate):
    """Wrap pretranslate_document to flip segments matching predicate(seg) to
    NEEDS_REVIEW (simulates a refusal / QE hold-back)."""
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
    assert ContentfulConnector.capability.topology == "T1"
    assert ContentfulEntryStrategy().topology == "T1"
    fs = FakeContentful(
        locales=[{"code": "en-US", "default": True}, {"code": "de-DE"}, {"code": "pt-BR"}]
    )
    _seed_std(fs)
    cc = _cc(fs)
    ok, msg = cc.test()
    assert ok, msg
    assert set(cc.enabled_locales()) == {"en-us", "de-de", "pt-br"}
    assert cc.resolve_locale("pt-br") == "pt-BR", cc.resolve_locale("pt-br")
    assert cc._source_locale() == "en-US", cc._source_locale()
    print(
        "  connector: T1 capability + strategy, test(), case-preserving locale resolve, "
        "default source locale"
    )

    # ---- (2) SSRF + region + path-segment validation (taxonomy A) ----
    assert _cc(fs)._host() == "https://api.contentful.com"
    assert _cc(fs, {"region": "EU"})._host() == "https://api.eu.contentful.com"
    for bad_region in ("MARS",):
        raised = False
        try:
            _cc(fs, {"region": bad_region})._host()
        except cbase.ConnectorError:
            raised = True
        assert raised, ("invalid region rejected", bad_region)
    for bad_space in ("../admin", "sp/x", "sp?x=1", "..", "a b", "sp\x00x", "sp\rx", "sp\nx"):
        raised = False
        try:
            _cc(fs, {"space_id": bad_space})._space()
        except cbase.ConnectorError:
            raised = True
        assert raised, ("unsafe space id (incl. control chars) must be rejected", repr(bad_space))
    for bad_ct in ("post/x", "post:x is fine? no", "..", "a b"):
        raised = False
        try:
            _cc(fs, {"content_types": f"blogPost, {bad_ct}"})._content_types()
        except cbase.ConnectorError:
            raised = True
        assert raised, ("unsafe content type must be rejected", bad_ct)
    print("  A: region validated; unsafe space id / content type ('..','/','?',space) rejected")

    # ---- (3) schema-driven selection + H1 name/value heuristic + VISIBLE skips (D) ----
    cc3 = _cc(fs)
    entry3 = cc3.discover_articles()[0]
    sel, skipped = cc3._field_selection(entry3)
    assert sel == ["title", "body"], ("only real localized text selected", sel)
    # #648: Rich Text is translated now; this fixture's hero is an EMPTY document, so it is
    # skipped for having nothing to translate (not for its type).
    assert "hero" in skipped and "no non-empty source" in skipped["hero"], skipped.get("hero")
    assert "views" in skipped and "Integer" in skipped["views"], skipped.get("views")
    # H1: a LOCALIZED slug must be SKIPPED (by name) and REPORTED — not blindly MT'd
    assert "slug" in skipped and "looks non-translatable" in skipped["slug"], (
        "H1: a localized slug must be skipped by the name heuristic",
        skipped.get("slug"),
    )
    assert "canonicalUrl" in skipped and "URL-shaped" in skipped["canonicalUrl"], (
        "H1: a localized URL-valued field skipped by value",
        skipped.get("canonicalUrl"),
    )
    assert "internalCode" in skipped and "not localized" in skipped["internalCode"], skipped.get(
        "internalCode"
    )
    print(
        "  D/H1: localized slug skipped by NAME + reported; URL-value skipped; "
        "empty-RichText/Integer/non-localized skipped + reported"
    )

    # allowlist BYPASSES the name/value heuristic but still gates on localized + text-type
    cc3b = _cc(fs, {"fields": "title, slug, hero, internalCode"})
    selB, skipB = cc3b._field_selection(entry3)
    assert selB == ["title", "slug"], ("allowlist override selects the localized slug", selB)
    assert "hero" in skipB and "internalCode" in skipB, (
        "but still refuses an empty RichText document + non-localized",
        skipB,
    )
    # exclude_fields (auto mode only)
    cc3c = _cc(fs, {"exclude_fields": "body"})
    selC, _skC = cc3c._field_selection(entry3)
    assert selC == ["title"], ("exclude_fields drops body", selC)
    print(
        "  D/H1: allowlist bypasses the heuristic (slug selectable) but keeps localized+"
        "type gates; exclude_fields honored"
    )

    # ---- (4) full flow CREATE: new locale keys, source + others preserved ----
    fs4 = FakeContentful()
    _seed_std(fs4)
    c4 = _conn("create")
    ok, msg, s4 = cstore.contentful_translate(c4.id, actor="auto", transport=fs4.transport())
    assert ok, msg
    assert s4["created"] == 2 and s4["updated"] == 0, s4  # de-DE + fr
    e4 = fs4.entries["e1"]["fields"]
    for loc in ("de-DE", "fr"):
        assert loc in e4["title"] and e4["title"][loc].strip(), (loc, e4["title"])
        assert loc in e4["body"] and e4["body"][loc].strip(), (loc, e4["body"])
    assert e4["title"]["en-US"] == "Hello world", ("source locale preserved", e4["title"])
    assert e4["slug"] == {"en-US": "hello-world"}, ("skipped localized slug untouched", e4["slug"])
    assert e4["internalCode"] == {"en-US": "PRD-1"}, (
        "non-localized field untouched",
        e4["internalCode"],
    )
    assert "de-DE" not in e4["hero"] and "fr" not in e4["hero"], (
        "an empty RichText document is never written to a target locale",
        e4["hero"],
    )
    assert "title" in s4["fields_selected"] and "hero" in s4["fields_skipped"], s4
    assert not s4.get("incomplete"), ("a clean run has no incomplete pairs", s4.get("incomplete"))
    print(
        f"  flow CREATE: 2 locales written to title+body; source/slug/internalCode/hero "
        f"preserved; selection reported; no incomplete ({s4['created']})"
    )

    # ---- (5) taxonomy B: a held-back TITLE never overwrites a stored translation ----
    import app.services.translate as T

    fs5 = FakeContentful()
    _seed_std(fs5, extra_locales={"de-DE": {"title": "Menschentitel", "body": "Alte Prosa"}})
    c5 = _conn("held-title", {"target_locales": "de-DE"})
    orig, patched = _flip(T, lambda sg: sg.order_index == 0)  # flip only the title segment
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s5 = cstore.contentful_translate(c5.id, actor="auto", transport=fs5.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    e5 = fs5.entries["e1"]["fields"]
    assert e5["title"]["de-DE"] == "Menschentitel", (
        "B: held-back title must NOT overwrite the stored translation",
        e5["title"],
    )
    assert e5["body"]["de-DE"] != "Alte Prosa" and e5["body"]["de-DE"].strip(), (
        "B: the translated body still updates",
        e5["body"],
    )
    assert s5["updated"] == 1, s5
    # M1: the held-back title is surfaced as an operator-visible INCOMPLETE marker
    inc5 = s5.get("incomplete") or []
    assert (
        len(inc5) == 1
        and inc5[0]["article_id"] == "e1"
        and inc5[0]["locale"] == "de-de"
        and inc5[0]["held_fields"] == ["title"]
    ), ("M1: partial write reported incomplete", inc5)
    assert "INCOMPLETE" in msg, ("M1: incomplete surfaced in the run message", msg)
    # #640: a pair written with a unit held records NO content hash, so the next (healthy)
    # run re-attempts it and finally writes the title (Contentful creates no entity, so a
    # missing field is simply absent for the locale; the hash rule is what stops the retry).
    assert "e1:de-de" not in ((cstore.get(c5.id).config or {}).get("contentful_hashes") or {}), (
        "#640: partial write must not record the content hash"
    )
    ok5b, msg5b, s5b = cstore.contentful_translate(c5.id, actor="auto", transport=fs5.transport())
    assert ok5b and s5b["updated"] == 1 and s5b["skipped"] == 0, (msg5b, s5b)
    assert fs5.entries["e1"]["fields"]["title"]["de-DE"] != "Menschentitel", (
        "#640: the held title is written once approved",
        fs5.entries["e1"]["fields"]["title"],
    )
    print(
        "  B/M1: NEEDS_REVIEW title OMITTED (stored de-DE title preserved); body updates; "
        "held title surfaced as an incomplete marker"
    )

    # ---- (6) taxonomy F: all-held-back -> no write, no hash, healthy re-run re-attempts ----
    fs6 = FakeContentful()
    _seed_std(fs6, extra_locales={"de-DE": {"title": "Mensch", "body": "AlteProse"}})
    c6 = _conn("all-held", {"target_locales": "de-DE"})
    orig, patched = _flip(T, lambda sg: True)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s6 = cstore.contentful_translate(c6.id, actor="auto", transport=fs6.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert not fs6.puts, ("F: all fields held back -> NO write", fs6.puts)
    assert fs6.entries["e1"]["fields"]["title"]["de-DE"] == "Mensch", "existing translation kept"
    assert (
        s6["held_back"] == 1 and s6["updated"] == 0 and s6["created"] == 0 and not s6["failures"]
    ), s6
    # no hash poisoned -> a later HEALTHY run re-attempts
    ok2, _m2, s62 = cstore.contentful_translate(c6.id, actor="auto", transport=fs6.transport())
    assert ok2 and s62["updated"] == 1 and s62["skipped"] == 0, (
        "F: held-back content re-attempted next run (hash not poisoned)",
        s62,
    )
    print(
        "  F: all-held-back -> no write, counted held_back not updated, healthy re-run re-attempts"
    )

    # ---- (7) taxonomy C: the SECOND run suppresses (no re-translate loop) ----
    fs7 = FakeContentful()
    _seed_std(fs7)
    c7 = _conn("steady")
    ok, _m, r7a = cstore.contentful_translate(c7.id, actor="auto", transport=fs7.transport())
    assert ok and r7a["created"] == 2, r7a
    # the write landed on the same entry state the read sees -> probe finds it
    assert _cc(fs7).strategy.exists_target(_cc(fs7), "e1", "de-de"), "de-DE probe finds the write"
    ok, _m, r7b = cstore.contentful_translate(c7.id, actor="auto", transport=fs7.transport())
    assert ok and r7b["created"] == 0 and r7b["updated"] == 0 and r7b["skipped"] == 2, (
        "C: run 2 suppresses via exists + content-hash (no loop)",
        r7b,
    )
    print("  C: run 2 finds the written locales + hash match -> 2 skipped (no re-translate loop)")

    # ---- (8) content-hash: an edited source re-translates ----
    fs8 = FakeContentful()
    _seed_std(fs8)
    c8 = _conn("edit")
    cstore.contentful_translate(c8.id, actor="auto", transport=fs8.transport())
    fs8.entries["e1"]["fields"]["title"]["en-US"] = "Hello EDITED world"
    ok, _m, s8 = cstore.contentful_translate(c8.id, actor="auto", transport=fs8.transport())
    assert ok and s8["updated"] == 2 and s8["skipped"] == 0, ("edited source -> re-translate", s8)
    print("  suppression: edited source content -> 2 updated")

    # ---- (9) version conflict: refetch-remerge-retry succeeds; exhaustion aborts (no hash) ----
    # Patch the shared backoff sleep so the 409 retry loop does not add real delay, and
    # prove it BACKS OFF between conflict retries (LOW).
    slept: list = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        fs9 = FakeContentful()
        _seed_std(fs9)
        fs9.conflict_once.add("e1")  # first PUT 409s once, then settles
        c9 = _conn("conflict-once", {"target_locales": "de-DE"})
        ok, msg, s9 = cstore.contentful_translate(c9.id, actor="auto", transport=fs9.transport())
        assert ok and s9["created"] == 1 and not s9["failures"], (
            "409-once -> refetch + re-apply the delta + succeed",
            s9,
            msg,
        )
        assert fs9.entries["e1"]["fields"]["title"]["de-DE"].strip()
        assert slept, ("the 409 retry backs off at least once (LOW)", slept)
    finally:
        cbase._sleep = real_sleep
    print(
        "  C(concurrency): a 409 version conflict is retried (refetch+remerge, backoff) and succeeds"  # noqa: E501
    )

    fs9b = FakeContentful()
    _seed_std(fs9b)
    fs9b.always_conflict.add("e1")  # every PUT 409s -> exhaust retries -> abort
    c9b = _conn("conflict-always", {"target_locales": "de-DE"})
    _rs = cbase._sleep
    cbase._sleep = lambda s: None
    try:
        ok, msg, s9b = cstore.contentful_translate(c9b.id, actor="auto", transport=fs9b.transport())
    finally:
        cbase._sleep = _rs
    assert not ok and s9b["created"] == 0 and s9b["updated"] == 0, ("persistent 409 -> abort", s9b)
    assert s9b["failures"] and "version conflict" in s9b["failures"][0]["error"], s9b
    # NO hash recorded for the aborted pair -> next run retries (proven: run again, no suppress)
    fs9b.always_conflict.clear()
    ok2, _m, s9b2 = cstore.contentful_translate(c9b.id, actor="auto", transport=fs9b.transport())
    assert ok2 and s9b2["created"] == 1 and s9b2["skipped"] == 0, (
        "aborted pair recorded no hash -> re-attempted",
        s9b2,
    )
    print("  C(concurrency): persistent 409 aborts the pair (no hash) -> re-attempted next run")

    # ---- (10) taxonomy A: article_ids scope-validated end-to-end + at the client seam ----
    fs10 = FakeContentful()
    _seed_std(fs10)  # blogPost e1 (configured)
    fs10.add_ct("secretType", _std_fields(), display="title")
    fs10.add_entry("secret1", "secretType", {"title": {"en-US": "Salaries"}})
    c10 = _conn("scope", {"target_locales": "de-DE"})
    for bad_id in ["../../users", "e1?x=1", "e1/child", "secret1"]:
        fs10.puts.clear()
        ok, msg, _s10 = cstore.contentful_translate(
            c10.id, article_ids=[bad_id], actor="attacker", transport=fs10.transport()
        )
        assert not fs10.puts, (bad_id, "NO external write for an unsafe/out-of-scope id", fs10.puts)
    # a non-configured content type entry is refused at the client seam too
    cc10 = _cc(fs10, {"content_types": "blogPost"})
    for rid in ["../x", "e1?populate=1", "e1/child"]:
        r = False
        try:
            cc10.entry_snapshot(rid)
        except cbase.ConnectorError:
            r = True
        assert r, (rid, "entry_snapshot must reject an unsafe id at the client seam")
    # a well-formed id whose content type is not configured -> refused at snapshot
    r = False
    try:
        cc10.entry_snapshot("secret1")
    except cbase.ConnectorError:
        r = True
    assert r, "entry_snapshot must refuse an entry whose content type is not configured"
    print("  A: unsafe / out-of-scope article_ids write nothing end-to-end + refused at the seam")

    # ---- (11) taxonomy E: the shared cap round-robins across content types (no starvation) ----
    fs11 = FakeContentful()
    for ct in ("posts", "pages", "guides"):
        fs11.add_ct(ct, _std_fields(), display="title")
        for i in range(40):
            fs11.add_entry(
                f"{ct}-{i}",
                ct,
                {"title": {"en-US": f"t{i}"}},
                updatedAt=f"2026-07-{(i % 28) + 1:02d}T00:00:00Z",
            )
    cc11 = _cc(fs11, {"content_types": "posts, pages, guides", "max_entries": "50"})
    got = cc11.discover_articles()
    counts: dict = {}
    for e in got:
        counts[e["_ct"]] = counts.get(e["_ct"], 0) + 1
    assert len(got) == 50, ("shared cap fills to 50", len(got))
    assert set(counts) == {"posts", "pages", "guides"} and min(counts.values()) >= 16, (
        "E: round-robin distributes the shared cap; no type starved",
        counts,
    )
    print(f"  E: multi-content-type cap round-robins fairly (no starvation): {counts}")

    # ---- (12) locale case round-trip on the wire (de-de flow -> de-DE key) ----
    fs12 = FakeContentful(locales=[{"code": "en-US", "default": True}, {"code": "pt-BR"}])
    _seed_std(fs12)
    c12 = _conn("case", {"target_locales": "pt-BR"})
    ok, msg, _s12 = cstore.contentful_translate(c12.id, actor="auto", transport=fs12.transport())
    assert ok, msg
    assert "pt-BR" in fs12.entries["e1"]["fields"]["title"], (
        "wire write uses the exact 'pt-BR' key",
        fs12.entries["e1"]["fields"]["title"],
    )
    print("  locale case: flow lowercases to pt-br; the wire write uses exact pt-BR")

    # ---- (13) enabled-locale rejection + honest source-drop + all-rejected FAIL ----
    fs13 = FakeContentful(locales=[{"code": "en-US", "default": True}, {"code": "de-DE"}])
    _seed_std(fs13)
    c13 = _conn("reject", {"target_locales": "de-DE, fr, en-US"})
    ok, msg, s13 = cstore.contentful_translate(c13.id, actor="auto", transport=fs13.transport())
    assert ok and s13["locales"] == ["de-de"] and s13["rejected"] == ["fr"], s13
    assert s13["dropped_source"] == ["en-us"], ("source dropped surfaced in result", s13)
    assert "equal to the source locale" in msg, ("honest source-drop message", msg)

    fs13b = FakeContentful(locales=[{"code": "en-US", "default": True}])
    _seed_std(fs13b)
    c13b = _conn("allreject", {"target_locales": "de-DE, fr"})
    ok, msg, s13b = cstore.contentful_translate(c13b.id, actor="auto", transport=fs13b.transport())
    assert not ok and s13b["locales"] == [], s13b
    with Session(engine) as s:
        assert s.get(Connection, c13b.id).status == "error"
    print("  rejection: 'fr' rejected, 'en-US' dropped-as-source (reported); all-rejected -> FAIL")

    # ---- (14) WriteBudget ceiling (taxonomy H) ----
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fs14 = FakeContentful()
        _seed_std(fs14)
        c14 = _conn("budget")
        ok, msg, s14 = cstore.contentful_translate(c14.id, actor="auto", transport=fs14.transport())
        assert ok and s14["ceiling_hit"] and (s14["created"] + s14["updated"]) == 1, s14
        assert len(fs14.puts) == 1
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    print("  H: WriteBudget MAX_WRITES_PER_RUN=1 stops after 1 external write")

    # ---- (15) mock-egress guard fires (taxonomy H) ----
    from app.services.connectors import egress_guard

    fs15 = FakeContentful()
    _seed_std(fs15)
    c15 = _conn("egress")
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s15 = cstore.contentful_translate(c15.id, actor="a", transport=fs15.transport())
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fs15.puts, ("mock MT + real egress -> blocked, no writes", msg)
    print("  H: egress guard blocks mock MT + real egress, no external writes")

    # ---- (16) M2: metadata (tags) ECHOED in the PUT (fake stores exactly what it sent) ----
    fs16 = FakeContentful()
    _seed_std(
        fs16, metadata={"tags": [{"sys": {"id": "campaignQ3", "type": "Link", "linkType": "Tag"}}]}
    )
    c16 = _conn("meta", {"target_locales": "de-DE"})
    ok, msg, _s16 = cstore.contentful_translate(c16.id, actor="auto", transport=fs16.transport())
    assert ok, msg
    # The fake now REPLACES metadata with whatever the PUT carried (no vacuous merge), so
    # tags survive ONLY because the connector echoes metadata. Assert the recorded PUT
    # payload itself carried the tag.
    assert fs16.puts, "a write happened"
    _eid, _flds, put_md = fs16.puts[-1]
    assert (put_md or {}).get("tags") and put_md["tags"][0]["sys"]["id"] == "campaignQ3", (
        "M2: the PUT payload must carry the entry's metadata tags",
        put_md,
    )
    md = fs16.entries["e1"].get("metadata") or {}
    assert md.get("tags") and md["tags"][0]["sys"]["id"] == "campaignQ3", (
        "metadata tags survive the read-merge-write",
        md,
    )
    print("  M2: metadata tags ECHOED in the PUT payload (strict fake) -> preserved")

    # ---- (17) taxonomy G: the fake ENFORCES CMA semantics (409 / 422) ----
    fs17 = FakeContentful()
    _seed_std(fs17)
    cc17 = _cc(fs17)
    # wrong version -> 409 conflict
    outv = cc17.write_entry("e1", {"title": {"en-US": "x", "de-DE": "y"}}, 999, None)
    assert (not outv.ok) and outv.conflict, ("G: wrong X-Contentful-Version -> 409", outv)
    # unknown field -> 422 (non-vacuous payload validation)
    out_unknown = cc17.write_entry("e1", {"ghost": {"en-US": "x"}}, 1, None)
    assert (not out_unknown.ok) and (not out_unknown.conflict) and "422" in out_unknown.message, (
        "G: unknown field -> 422",
        out_unknown,
    )
    # a non-default locale on a NON-localized field -> 422 (proves the write-guard matters)
    out_nonloc = cc17.write_entry("e1", {"internalCode": {"en-US": "s", "de-DE": "s2"}}, 1, None)
    assert (not out_nonloc.ok) and "422" in out_nonloc.message, (
        "G: non-localized field with a target locale -> 422",
        out_nonloc,
    )
    # an oversize Symbol value -> 422 (H2 non-vacuous: proves the whole PUT is rejected)
    out_long = cc17.write_entry(
        "e1", {"title": {"en-US": "Hello world", "de-DE": "z" * 300}}, 1, None
    )
    assert (not out_long.ok) and "422" in out_long.message, (
        "G/H2: an oversize Symbol value 422s the whole PUT",
        out_long,
    )
    print("  G/H2: fake enforces 409 (version) + 422 (unknown / non-localized / Symbol-256)")

    # ---- (17b) H2: validate_value catches the Symbol-256 + size validations ----
    cc17b = _cc(fs17)
    assert cc17b.validate_value("blogPost", "title", "x" * 256) is None, "256 is allowed"
    r256 = cc17b.validate_value("blogPost", "title", "x" * 257)
    assert r256 and "256" in r256, ("H2: 257 chars exceeds the Symbol limit", r256)
    fsV = FakeContentful()
    fsV.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"size": {"max": 5}}],
            }
        ],
        display="title",
    )
    fsV.add_entry("e1", "blogPost", {"title": {"en-US": "Hi"}})
    ccV = _cc(fsV)
    rv = ccV.validate_value("blogPost", "title", "toolong")
    assert rv and "max length 5" in rv, ("H2: size.max validation enforced", rv)
    assert ccV.validate_value("blogPost", "title", "ok") is None
    print("  H2: validate_value enforces the Symbol-256 hard cap + the field's size.max")

    # ---- (17c) H2 flow (changed deliberately by #640): an oversize translation on a NEW
    # locale is a per-field validation hold, so the locale is NOT created (it would stay
    # without the title); it is a durable validation block (no MT re-spend), nothing hashed.
    fsH = FakeContentful()
    fsH.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"size": {"max": 5}}],
            },
            {"id": "body", "type": "Text", "localized": True},
        ],
        display="title",
    )
    # source title "Hola" (4 <= 5, valid); its translation "[de-de] Hola" (>5) breaks size.max
    fsH.add_entry("e1", "blogPost", {"title": {"en-US": "Hola"}, "body": {"en-US": "Some prose."}})
    cH = _conn("h2flow", {"target_locales": "de-DE"})
    ok, msg, sH = cstore.contentful_translate(cH.id, actor="auto", transport=fsH.transport())
    assert ok, msg
    fH = fsH.entries["e1"]["fields"]
    assert "de-DE" not in fH["body"] and "de-DE" not in fH["title"], (
        "#640: a new locale is not created while a field is held by validation",
        fH,
    )
    assert sH["created"] == 0 and sH["updated"] == 0 and not sH["failures"], sH
    blkH = sH.get("blocked") or []
    assert len(blkH) == 1 and blkH[0]["article_id"] == "e1", ("#640: reported blocked", sH)
    ok2, _m, sH2 = cstore.contentful_translate(cH.id, actor="auto", transport=fsH.transport())
    assert sH2["created"] == 0 and sH2["parked"] == 1, ("block: no MT re-spend", sH2)
    print(
        "  H2/#640: oversize title on a NEW locale -> not created, validation-blocked, no re-spend"
    )

    # ---- (17d) M1: retry_failed re-attempts the blocked pair once the cap is raised ----
    fsH.content_types["blogPost"]["fields"][0]["validations"] = [{"size": {"max": 500}}]
    okr, _mr, sHr = cstore.retry_failed(cH.id, blkH, actor="auto", transport=fsH.transport())
    assert okr, sHr
    fH = fsH.entries["e1"]["fields"]
    assert "de-DE" in fH["title"] and "de-DE" in fH["body"], (
        "M1: retry_failed re-attempted the blocked pair and wrote BOTH fields",
        fH,
    )
    print("  M1: retry_failed re-attempts a blocked pair (flow-level only_pairs bypass)")

    # ---- (17c2) #640: a NEEDS_REVIEW unit on a NEW locale -> no create, held_back, no hash ----
    fsN = FakeContentful()
    _seed_std(fsN)
    cN = _conn("640-new", {"target_locales": "de-DE"})
    origN, patchedN = _flip(T, lambda sg: sg.order_index >= 1)  # hold the body unit(s)
    set_pieces("app.services.translate", "pretranslate_document", patchedN)
    try:
        okN, msgN, sN = cstore.contentful_translate(cN.id, actor="auto", transport=fsN.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", origN)
    assert okN, msgN
    fN = fsN.entries["e1"]["fields"]
    assert not any("de-DE" in (fN.get(f) or {}) for f in fN), ("#640: not created", fN)
    assert sN["created"] == 0 and sN["held_back"] == 1 and not sN["failures"], sN
    assert "e1:de-de" not in ((cstore.get(cN.id).config or {}).get("contentful_hashes") or {})
    okN2, _m, sN2 = cstore.contentful_translate(cN.id, actor="auto", transport=fsN.transport())
    assert okN2 and sN2["created"] + sN2["updated"] == 1 and sN2["held_back"] == 0, sN2
    fN = fsN.entries["e1"]["fields"]
    assert "de-DE" in fN["title"] and "de-DE" in fN["body"], ("both fields after approval", fN)
    print(
        "  #640: NEEDS_REVIEW on a NEW locale -> not created, held_back=1, retried after approval"
    )

    # ---- (17e) M3: an all-non-localizable payload FAILS (no false 'unchanged' success) ----
    fsM3 = FakeContentful()
    _seed_std(fsM3)
    ccM3 = _cc(fsM3)
    # neuter the write guard's allow-set: NOW every field is 'non-localizable'
    ccM3.localizable_field_ids = lambda ct: set()
    ccM3.connection_id = 12345
    rM3 = ccM3.strategy.write_target(ccM3, "e1", "de-DE", {"title": "T"}, draft=True)
    assert (not rM3.ok) and "no writable fields" in rM3.message, (
        "M3: an all-filtered payload is a FAILURE, never a false 'unchanged' success",
        rM3,
    )
    assert not fsM3.puts, ("M3: nothing was written", fsM3.puts)
    print("  M3: an all-non-localizable payload fails per-pair (no false success, no hash)")

    # ---- (17f) M4: a source_locale the space does not enable FAILS loudly ----
    fsM4 = FakeContentful()
    _seed_std(fsM4)
    c_m4 = _conn("m4", {"source_locale": "xx-XX", "target_locales": "de-DE"})
    ok, msg, _sM4 = cstore.contentful_translate(c_m4.id, actor="auto", transport=fsM4.transport())
    assert not ok and "not enabled" in msg, ("M4: an unenabled source_locale fails loud", msg)
    with Session(engine) as s:
        assert s.get(Connection, c_m4.id).status == "error"
    print("  M4: a source_locale not enabled on the space fails loudly (status error)")

    # ---- (17g) F1: an ALL-validation-blocked pair -> deterministic block, no MT re-spend ----
    import app.services.translate as _Tmod

    fsF1 = FakeContentful()
    fsF1.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"size": {"max": 3}}],
            }
        ],
        display="title",
    )
    fsF1.add_entry("e1", "blogPost", {"title": {"en-US": "Hi"}})  # source 2<=3 valid; MT expands >3
    cF1 = _conn("f1", {"target_locales": "de-DE"})
    calls = {"n": 0}
    real_pt = _Tmod.pretranslate_document

    def _counting(s, doc, loc, *a, **k):
        calls["n"] += 1
        return real_pt(s, doc, loc, *a, **k)

    set_pieces("app.services.translate", "pretranslate_document", _counting)
    try:
        ok1, msg1, sF1 = cstore.contentful_translate(
            cF1.id, actor="auto", transport=fsF1.transport()
        )
        _r2 = cstore.contentful_translate(cF1.id, actor="auto", transport=fsF1.transport())
        _r3 = cstore.contentful_translate(cF1.id, actor="auto", transport=fsF1.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_pt)
    assert ok1, ("F1: an all-validation-blocked run is NOT a hard failure (stays green)", msg1)
    assert sF1["blocked"] and sF1["blocked"][0]["article_id"] == "e1", ("F1: reported blocked", sF1)
    assert not sF1["failures"], ("F1: a block is NOT a real failure", sF1)
    assert not fsF1.puts, ("F1: nothing written", fsF1.puts)
    assert "BLOCKED" in msg1, msg1
    assert not ((cstore.get(cF1.id).config or {}).get("contentful_hashes") or {}), (
        "F1/#665: an all-blocked pair is not done, so no content hash is recorded",
        cstore.get(cF1.id).config,
    )
    assert _r3[2]["parked"] == 1, ("F1: later runs park the pair instead of re-translating", _r3)
    assert calls["n"] == 1, (
        "F1: exactly ONE MT materialization across THREE scheduled runs (no re-spend)",
        calls,
    )
    with Session(engine) as s:
        assert s.get(Connection, cF1.id).status != "error", "F1: connection not permanently red"
    # retry_failed still REACHES the blocked pair (flow-level only_pairs bypass -> re-attempts;
    # the validation block is consulted only on a hash-match, which the retry bypasses)
    calls["n"] = 0
    set_pieces("app.services.translate", "pretranslate_document", _counting)
    try:
        cstore.retry_failed(cF1.id, sF1["blocked"], actor="auto", transport=fsF1.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_pt)
    assert calls["n"] == 1, (
        "F1: retry_failed re-attempts the blocked pair (one materialization)",
        calls,
    )
    print("  F1: all-validation-blocked -> 1 MT across 3 runs, connection green, retriable")

    # ---- (17h) F2: a legacy value that 422s the whole PUT -> RUNTIME-422 block (round-3) ----
    # The client-side echoed-value pre-check was DELETED (round-3 finding 1); the block now
    # fires only on the REAL 422 the CMA returns for the full-entry PUT (the fake enforces
    # size.max, so the oversize echoed title 422s the whole PUT).
    fsF2 = FakeContentful()
    fsF2.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"size": {"max": 12}}],
            },
            {"id": "body", "type": "Text", "localized": True},
        ],
        display="title",
    )
    # e1 has a LEGACY oversize title (a max:5 added after the content) + a valid body; e2 is fine
    fsF2.add_entry(
        "e1", "blogPost", {"title": {"en-US": "Way too long title"}, "body": {"en-US": "Prose."}}
    )
    fsF2.add_entry("e2", "blogPost", {"title": {"en-US": "OK"}, "body": {"en-US": "Fine."}})
    cF2 = _conn("f2", {"target_locales": "de-DE"})
    calls["n"] = 0
    set_pieces("app.services.translate", "pretranslate_document", _counting)
    try:
        okF2, msgF2, sF2 = cstore.contentful_translate(
            cF2.id, actor="auto", transport=fsF2.transport()
        )
        cstore.contentful_translate(
            cF2.id, actor="auto", transport=fsF2.transport()
        )  # scheduled re-run
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_pt)
    e1blk = [b for b in sF2["blocked"] if b["article_id"] == "e1"]
    # #640: size.max is 12 so e2's translation ("[de-de] OK") fits; e1's translated title
    # also breaks size.max, so on a NEW locale the
    # per-field validation hold now blocks the create before the PUT ("new locale not
    # created"); the CMA-422 wording remains for a hold the pre-write validator cannot see.
    assert e1blk and (
        "CMA rejected" in e1blk[0]["error"] or "new locale not created" in e1blk[0]["error"]
    ), (
        "F2: e1 blocked (runtime 422 or the create-hold), keyed 'error'",
        sF2["blocked"],
    )
    assert "de-DE" not in fsF2.entries["e1"]["fields"].get("body", {}), (
        "F2: e1 is NOT written (the whole PUT 422'd on the echoed legacy title)",
        fsF2.entries["e1"]["fields"],
    )
    assert "de-DE" in fsF2.entries["e2"]["fields"]["body"], (
        "F2: e2 unaffected (body written)",
        fsF2.entries["e2"]["fields"],
    )
    assert okF2, (
        "F2: one blocked pair does not make the connection red when others succeed",
        msgF2,
    )
    # no re-spend: e1 materialized once across the two scheduled runs (e2 materializes once too)
    assert calls["n"] == 2, ("F2: e1+e2 materialize once each; the block does not re-spend", calls)
    print(
        "  F2: a legacy value 422s the PUT -> runtime block (no client pre-check); no re-spend; "
        "others unaffected"
    )

    # ---- (17i) F3: regexp flags are honoured (JS -> Python) ----
    fs3 = FakeContentful()
    fs3.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"regexp": {"pattern": "^[a-z ]+$", "flags": "i"}}],
            }
        ],
        display="title",
    )
    assert _cc(fs3).validate_value("blogPost", "title", "Hallo Welt") is None, (
        "F3: the 'i' flag accepts uppercase the CMA would accept",
        "Hallo Welt",
    )
    fs3b = FakeContentful()
    fs3b.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"regexp": {"pattern": "^[a-z ]+$"}}],
            }
        ],
        display="title",
    )
    assert _cc(fs3b).validate_value("blogPost", "title", "Hallo Welt") is not None, (
        "F3: without the flag, uppercase is correctly rejected"
    )
    assert _cc(fs3b).validate_value("blogPost", "title", "hallo welt") is None
    print("  F3: regexp JS flags mapped to Python (i->IGNORECASE); both directions correct")

    # ---- (17i-2) red-team audit 2026-10-03 finding 01: remote regexes are ReDoS-bounded ----
    # A field's regexp validation is remote-supplied text. "(a|aa)+b" backtracks
    # exponentially against a long run of 'a' under BOTH stdlib re and the regex
    # module; with the guard the match must stop at the per-match timeout and FAIL
    # OPEN (value accepted, validation skipped) with a warning naming the content
    # type + field, never the value. A Text field is used because a Symbol field
    # 422s over 256 chars before its regexp is consulted.
    import logging as _logging
    import time as _time

    from app.services.connectors import contentful_part1 as _cfp1

    class _LogCap(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    _cap = _LogCap()
    _logging.getLogger("loc_tms.connectors").addHandler(_cap)
    try:
        fsR = FakeContentful()
        fsR.add_ct(
            "blogPost",
            [
                {
                    "id": "body",
                    "type": "Text",
                    "localized": True,
                    "validations": [{"regexp": {"pattern": "(a|aa)+b"}}],
                }
            ],
            display="title",
        )
        ccR = _cc(fsR)
        bomb = "a" * 200  # never matches (a|aa)+b: the catastrophic-backtracking worst case
        t0 = _time.monotonic()
        r_bomb = ccR.validate_value("blogPost", "body", bomb)
        dt = _time.monotonic() - t0
        assert dt < 2.0, f"ReDoS guard failed: catastrophic pattern took {dt:.2f}s"
        assert r_bomb is None, "finding 01: a timed-out validation must FAIL OPEN (accept)"
        w = [m for m in _cap.msgs if "match limit" in m]
        assert w and "blogPost" in w[-1] and "body" in w[-1], ("timeout warning", w)
        assert bomb not in w[-1], "the warning must never echo the value"
        # an over-length pattern is refused BEFORE compiling, also fail-open + warned
        over_len_pattern = "a" * (_cfp1._MAX_REMOTE_REGEX_PATTERN_LEN + 1)
        fsO = FakeContentful()
        fsO.add_ct(
            "blogPost",
            [
                {
                    "id": "title",
                    "type": "Symbol",
                    "localized": True,
                    "validations": [{"regexp": {"pattern": over_len_pattern}}],
                }
            ],
            display="title",
        )
        assert _cc(fsO).validate_value("blogPost", "title", "x") is None, (
            "finding 01: an over-long pattern is skipped (accepted), not enforced"
        )
        w = [m for m in _cap.msgs if "pattern limit" in m]
        assert w and "blogPost" in w[-1] and "title" in w[-1], ("over-length warning", w)
        # an unparseable pattern keeps failing open (review F3) and now also warns
        fsB = FakeContentful()
        fsB.add_ct(
            "blogPost",
            [
                {
                    "id": "title",
                    "type": "Symbol",
                    "localized": True,
                    "validations": [{"regexp": {"pattern": "(unclosed"}}],
                }
            ],
            display="title",
        )
        assert _cc(fsB).validate_value("blogPost", "title", "anything") is None
        w = [m for m in _cap.msgs if "not a valid pattern" in m]
        assert w and "blogPost" in w[-1], ("compile-error warning", w)
    finally:
        _logging.getLogger("loc_tms.connectors").removeHandler(_cap)
    print(
        "  finding 01: catastrophic remote regexp returns < 2s and fails OPEN; over-length"
        " + unparseable patterns skipped; warnings name ct/field, never the value"
    )

    # ---- (17j) F4: unrecognized validation shapes are ignored (fail open, no crash) ----
    fs4 = FakeContentful()
    fs4.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [
                    {"size": 5},
                    {"regexp": "^x$"},
                    "not-a-dict",
                    42,
                    {"size": {"max": 10}},
                ],
            }
        ],
        display="title",
    )
    cc4 = _cc(fs4)
    assert cc4.validate_value("blogPost", "title", "short") is None, (
        "F4: malformed validation shapes are ignored (no crash, no false hold-back)",
        "short",
    )
    r10 = cc4.validate_value("blogPost", "title", "x" * 20)
    assert r10 and "max length 10" in r10, (
        "F4: the WELL-FORMED size.max after the bad shapes still applies",
        r10,
    )
    print(
        "  F4: {'size':5}/{'regexp':str}/non-dict validation shapes ignored; good ones still apply"
    )

    # ---- (17k) F6/#650: a 409 VersionMismatch uses plain bounded backoff; the rate-limit
    # header is ignored there (Contentful sends it on a 429, never on a 409) ----
    fs6 = FakeContentful()
    _seed_std(fs6)
    fs6.conflict_once.add("e1")
    fs6.conflict_reset = 3600  # hostile huge reset on a 409 must NOT be honoured
    c6 = _conn("f6clamp", {"target_locales": "de-DE"})
    slept6: list = []
    _rs6 = cbase._sleep
    cbase._sleep = lambda s: slept6.append(s)
    try:
        okc, _mc, _sc = cstore.contentful_translate(c6.id, actor="auto", transport=fs6.transport())
    finally:
        cbase._sleep = _rs6
    assert okc, _mc
    assert slept6 and max(slept6) <= cbase.BACKOFF_BASE, (
        "F6/#650: a 409 backs off exponentially, ignoring any rate-limit header",
        slept6,
    )
    print("  F6: a 409 version conflict backs off by plain exponential backoff (header ignored)")

    # ---- (17l) round-3 finding 6: the block map stores the REASON, not the redundant hash ----
    fsR6 = FakeContentful()
    fsR6.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"size": {"max": 3}}],
            }
        ],
        display="title",
    )
    fsR6.add_entry("e1", "blogPost", {"title": {"en-US": "Hi"}})
    cR6 = _conn("r6", {"target_locales": "de-DE"})
    cstore.contentful_translate(cR6.id, actor="auto", transport=fsR6.transport())
    cfgR6 = cstore.get(cR6.id).config or {}
    bval = (cfgR6.get("contentful_blocked") or {}).get("e1:de-de")
    hval = (cfgR6.get("contentful_hashes") or {}).get("e1:de-de")
    assert isinstance(bval, str) and "max length 3" in bval, (
        "finding 6: the block map value is the human REASON",
        bval,
    )
    sval = (cfgR6.get("contentful_blocked_src") or {}).get("e1:de-de")
    assert not hval and sval, (
        "#665 (owner 2026-10-10): a blocked pair records NO content hash (not done); the "
        "park marker holds the source hash so MT is not re-spent",
        bval,
        hval,
        sval,
    )
    print(
        "  finding 6 / #665: block map stores the reason; NO hash is recorded (not done); "
        "the source hash lives in contentful_blocked_src"
    )

    # ---- (17m) round-3 finding 3: a block is CLEARED on success -> delete-in-Contentful self-heals ----  # noqa: E501
    fsSH = FakeContentful()
    fsSH.add_ct(
        "blogPost",
        [
            {
                "id": "title",
                "type": "Symbol",
                "localized": True,
                "validations": [{"size": {"max": 3}}],
            }
        ],
        display="title",
    )
    fsSH.add_entry("e1", "blogPost", {"title": {"en-US": "Hi"}})  # blocked entry
    fsSH.add_entry("e2", "blogPost", {"title": {"en-US": "Yo"}})  # never-blocked control
    # give the control a schema that always fits by using a separate content type
    fsSH.add_ct("okType", [{"id": "title", "type": "Symbol", "localized": True}], display="title")
    fsSH.add_entry("e2", "okType", {"title": {"en-US": "Yo"}})
    cSH = _conn("selfheal", {"content_types": "blogPost, okType", "target_locales": "de-DE"})
    _okSH, _m, sSH1 = cstore.contentful_translate(cSH.id, actor="a", transport=fsSH.transport())
    assert any(b["article_id"] == "e1" for b in sSH1["blocked"]), ("e1 blocked", sSH1)
    assert "de-DE" in fsSH.entries["e2"]["fields"]["title"], (
        "e2 (control) written",
        fsSH.entries["e2"]["fields"],
    )
    assert "e1:de-de" in (cstore.get(cSH.id).config or {}).get("contentful_blocked", {}), (
        "e1 block persisted"
    )
    # operator fixes the validation, then RETRIES the blocked pair -> success -> block CLEARED
    fsSH.content_types["blogPost"]["fields"][0]["validations"] = [{"size": {"max": 500}}]
    cstore.retry_failed(cSH.id, sSH1["blocked"], actor="a", transport=fsSH.transport())
    assert "e1:de-de" not in (cstore.get(cSH.id).config or {}).get("contentful_blocked", {}), (
        "finding 3: a successful write CLEARS the block",
        (cstore.get(cSH.id).config or {}).get("contentful_blocked"),
    )
    assert "de-DE" in fsSH.entries["e1"]["fields"]["title"], "e1 written after retry"
    # a human DELETES both translations in Contentful; the next scheduled run RESTORES both
    del fsSH.entries["e1"]["fields"]["title"]["de-DE"]
    del fsSH.entries["e2"]["fields"]["title"]["de-DE"]
    cstore.contentful_translate(cSH.id, actor="a", transport=fsSH.transport())
    assert "de-DE" in fsSH.entries["e1"]["fields"]["title"], (
        "finding 3: the formerly-BLOCKED e1 self-heals (block was cleared, so not suppressed)",
    )
    assert "de-DE" in fsSH.entries["e2"]["fields"]["title"], ("control e2 self-heals too",)
    print(
        "  finding 3: block cleared on success -> a deleted translation self-heals (blocked + control)"  # noqa: E501
    )

    # ---- (17n) round-3 finding 4: _clean_failures keeps article_id -> API retry works ----
    from app.web.api import _clean_failures

    cleaned = _clean_failures([{"article_id": "e1", "locale": "de-DE", "error": "x"}])
    assert (
        cleaned and cleaned[0].get("article_id") == "e1" and cleaned[0].get("locale") == "de-DE"
    ), ("finding 4: a contentful article_id item survives _clean_failures", cleaned)
    assert _clean_failures([{"issue_key": "PROJ-1", "locale": "de", "error": "y"}]), (
        "finding 4: the jira issue_key shape still works"
    )
    assert _clean_failures([{"locale": "de"}]) == [], (
        "finding 4: an item with no id is still dropped"
    )
    capi = _conn("apiretry")
    from tests._api_fixture import user_api_token

    _capi_client = TestClient(app)
    hdrapi = {"X-Loc-Token": user_api_token()}
    rapi = _capi_client.post(
        f"/api/connectors/{capi.id}/retry",
        json={"failures": [{"article_id": "e1", "locale": "de-DE", "error": "x"}]},
        headers=hdrapi,
    )
    assert rapi.status_code == 202, (
        "finding 4: POST /api .../retry accepts a contentful failure list",
        rapi.status_code,
        rapi.text,
    )
    print("  finding 4: _clean_failures keeps article_id (jira still works); API retry returns 202")

    # ---- (18) pagination walk across pages ----
    fs18 = FakeContentful()
    fs18.add_ct("blogPost", _std_fields(), display="title")
    for i in range(5):
        fs18.add_entry(
            f"p{i}",
            "blogPost",
            {"title": {"en-US": f"T{i}"}},
            updatedAt=f"2026-07-0{i + 1}T00:00:00Z",
        )
    got18 = _cc(fs18, {"max_entries": "3"}).discover_articles()
    assert len(got18) == 3, ("cap honored across paginated fetch", len(got18))
    got18b = _cc(fs18, {"max_entries": "50"}).discover_articles()
    assert sorted(e["id"] for e in got18b) == [f"p{i}" for i in range(5)], got18b
    print("  pagination: skip/limit walk collects entries up to the cap")

    # ---- (19) empty entry skipped ----
    fs19 = FakeContentful()
    fs19.add_ct("blogPost", _std_fields(), display="title")
    fs19.add_entry("empty1", "blogPost", {"title": {"en-US": ""}, "body": {"en-US": "   "}})
    fs19.add_entry("full1", "blogPost", {"title": {"en-US": "Has text"}})
    c19 = _conn("empty")
    ok, msg, s19 = cstore.contentful_translate(c19.id, actor="auto", transport=fs19.transport())
    assert ok and s19["empty_skipped"] >= 1, s19
    print("  empty entry: an entry with no translatable source text is skipped")

    # ---- (20) parity surfaces ----
    from app.services.connectors import REGISTRY

    assert "contentful" in REGISTRY
    c = TestClient(app)
    assert "Contentful" in c.get("/connectors").text
    typ = c.get("/connectors/contentful").text
    assert "Space ID" in typ and "Create" in typ
    from app.cli.__main__ import build_parser

    assert "connector contentful-translate" in _leaf_paths(build_parser())
    from starlette.routing import Route

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/contentful/translate" in api_paths
    print("  parity: REGISTRY + /connectors tile + type form + /api route + CLI leaf")

    # ---- (21) org isolation: a foreign/missing cid -> 404 on the /api translate route ----
    from tests._api_fixture import user_api_token

    connapi = _conn("api")
    hdr = {"X-Loc-Token": user_api_token()}
    r_ghost = c.post("/api/connectors/99999999/contentful/translate", json={}, headers=hdr)
    assert r_ghost.status_code == 404, ("foreign/missing cid -> 404", r_ghost.status_code)
    r_own = c.post(
        f"/api/connectors/{connapi.id}/contentful/translate", json={"article_ids": []}, headers=hdr
    )
    assert r_own.status_code in (200, 400), ("own connection reachable", r_own.status_code)
    print("  org isolation: foreign/missing cid -> 404; own connection reachable on /api route")

    # ---- (22) CLI contentful-translate subcommand smoke ----
    from app.cli.cmd_connector import _contentful_translate

    ficli = FakeContentful()
    _seed_std(ficli)
    conncli = _conn("cli")
    real_ct = cstore.contentful_translate
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

    set_pieces("app.services.connectors.store", "contentful_translate", _capturing)

    class _Args:
        connection_id = conncli.id
        article_ids = "e1"
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
        rc = _contentful_translate(_Args(), emit)
        assert rc == 0, emit.res
        assert seen.get("article_ids") == ["e1"], seen
        assert emit.res[1]["ok"], emit.res
    finally:
        set_pieces("app.services.connectors.store", "contentful_translate", real_ct)
    print("  CLI: contentful-translate parses --article-ids and runs the flow (OK)")

    # ---- (18) #647: exists_target and existed look only at translated fields ----
    fs18 = FakeContentful()
    fs18.add_ct("blogPost", _std_fields(), display="title")
    # only the (name-heuristic-skipped) localized slug carries a de-DE value
    fs18.add_entry(
        "e18",
        "blogPost",
        {
            "title": {"en-US": "Hello world"},
            "body": {"en-US": "Some prose."},
            "slug": {"en-US": "hello-world", "de-DE": "hallo-welt"},
        },
    )
    cc18 = _cc(fs18)
    strat18 = ContentfulEntryStrategy()
    assert strat18.exists_target(cc18, "e18", "de-de") is False, (
        "#647: a de-DE value on an UNtranslated (slug) field must not count as existing"
    )
    cc18x = _cc(fs18, {"exclude_fields": "body"})
    fs18.entries["e18"]["fields"]["body"]["de-DE"] = "Prosa"
    assert strat18.exists_target(cc18x, "e18", "de-de") is False, (
        "#647: an exclude_fields field with a target value must not count as existing"
    )
    assert strat18.exists_target(cc18, "e18", "de-de") is True, (
        "#647: a translated field (body) with a target value still counts as existing"
    )
    # write_target: created vs updated is decided on translated fields only
    del fs18.entries["e18"]["fields"]["body"]["de-DE"]
    wr18 = strat18.write_target(cc18, "e18", "de-de", {"title": "Hallo Welt", "body": "Prosa"})
    assert wr18.ok and wr18.created is True and wr18.message == "created", (
        "#647: slug.de-DE alone must not turn a create into an update",
        wr18,
    )
    assert fs18.entries["e18"]["fields"]["slug"]["de-DE"] == "hallo-welt", "slug left untouched"
    # the flow rewrites a translated title an editor deleted even though slug.de-DE remains
    fs18b = FakeContentful()
    fs18b.add_ct("blogPost", _std_fields(), display="title")
    fs18b.add_entry(
        "e18b",
        "blogPost",
        {
            "title": {"en-US": "Hello world"},
            "body": {"en-US": "Some prose."},
            "slug": {"en-US": "hello-world", "de-DE": "hallo-welt"},
        },
    )
    c18b = _conn("f647", {"target_locales": "de-DE"})
    okb, mb, _sb = cstore.contentful_translate(c18b.id, actor="auto", transport=fs18b.transport())
    assert okb, mb
    assert (fs18b.entries["e18b"]["fields"].get("title") or {}).get("de-DE"), "title written"
    del fs18b.entries["e18b"]["fields"]["title"]["de-DE"]
    del fs18b.entries["e18b"]["fields"]["body"]["de-DE"]
    okb2, mb2, _sb2 = cstore.contentful_translate(
        c18b.id, actor="auto", transport=fs18b.transport()
    )
    assert okb2, mb2
    assert (fs18b.entries["e18b"]["fields"].get("title") or {}).get("de-DE"), (
        "#647: a translated title deleted by an editor is restored although slug.de-DE remains",
        mb2,
    )
    print("  #647: exists_target / existed scoped to translated fields (slug/excluded ignored)")

    # ---- (19) #650: a 429 honours Retry-After, then X-Contentful-RateLimit-Reset ----
    def _rl_transport(headers_list):
        calls = {"n": 0}

        def handler(request):
            i = calls["n"]
            calls["n"] += 1
            if i < len(headers_list):
                return httpx.Response(429, json={"message": "rate"}, headers=headers_list[i])
            return httpx.Response(200, json={"items": []})

        return httpx.MockTransport(handler)

    for hdrs, want, label in (
        ({"X-Contentful-RateLimit-Reset": "2"}, 2.0, "Reset alone"),
        ({"Retry-After": "3", "X-Contentful-RateLimit-Reset": "9"}, 3.0, "Retry-After wins"),
        ({"X-Contentful-RateLimit-Reset": "3600"}, cbase.MAX_SLEEP, "Reset clamped"),
        ({}, cbase.BACKOFF_BASE, "no header: exponential backoff"),
    ):
        cc19 = ContentfulConnector(
            {
                "space_id": "sp",
                "environment": "master",
                "region": "US",
                "content_types": "blogPost",
                "source_locale": "en-US",
            },
            {"cma_token": "CFPAT-x"},
            transport=_rl_transport([hdrs]),
        )
        slept19: list = []
        _rs19 = cbase._sleep
        cbase._sleep = lambda s, _l=slept19: _l.append(s)
        try:
            with cc19._client() as c19:
                r19 = cc19._get(c19, "/spaces/sp/environments/master/entries")
        finally:
            cbase._sleep = _rs19
        assert r19.status_code == 200 and slept19 == [want], (
            "#650",
            label,
            slept19,
            want,
        )
    # the 409 VersionMismatch path no longer reads rate-limit headers (they never ride a 409)
    fs19 = FakeContentful()
    _seed_std(fs19)
    fs19.conflict_once.add("e1")
    fs19.conflict_reset = 7
    c19 = _conn("f650-409", {"target_locales": "de-DE"})
    slept19b: list = []
    _rs19b = cbase._sleep
    cbase._sleep = lambda s: slept19b.append(s)
    try:
        ok19, m19, _s19 = cstore.contentful_translate(
            c19.id, actor="auto", transport=fs19.transport()
        )
    finally:
        cbase._sleep = _rs19b
    assert ok19, m19
    assert 7 not in slept19b and 7.0 not in slept19b, (
        "#650: the dead rate-limit header read on 409 is gone (plain backoff instead)",
        slept19b,
    )
    print(
        "  #650: 429 honours Retry-After then X-Contentful-RateLimit-Reset; 409 uses plain backoff"
    )

    print("verify_contentful OK")


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
