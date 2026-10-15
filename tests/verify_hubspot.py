"""HubSpot CMS connector + the T3-with-native-linkage HubSpotVariantStrategy — fully
offline via httpx.MockTransport (a fake HubSpot CMS v3 API that models the language-group
model, `create-language-variation`'s CLONE-into-DRAFT semantics, and — crucially — the
DRAFT and PUBLISHED representations as SEPARATE stored content).

HubSpot is the FOURTH hand-built connector-factory connector (Strapi #1 T2, Contentful #2
T1, Shopify #3 T2-flavored). It is T3 (a translation is a separately-addressable object)
whose CORRELATION is native: HubSpot's own `translations` dict + `translatedFromId`, so no
id map is persisted.

The fake is deliberately STRICT so the assertions are non-vacuous (taxonomy G):
  * draft and published content are SEPARATE dicts, so a connector whose read, probe and
    write did not agree on a state would FAIL here rather than pass silently (the Strapi
    fixture lesson);
  * a PATCH carrying an unknown top-level key -> 400, and `layoutSections` on a blog post
    (or `postBody` on a page) -> 400, so the payload assertions mean something;
  * create-language-variation REFUSES a non-primary id (proves resolve-to-primary,
    digest §6.5) and REFUSES a locale that already has a variation (proves the write path
    consults HubSpot's native linkage BEFORE cloning);
  * a clone really does copy the SOURCE content into the new object, so a phantom is a
    real 100%-source-text page.

Covers the connector-build defect taxonomy (the connector build blueprint §3):
  A  caller-supplied `<content-type>:<id>` scope + shape, fail-closed (traversal, encoded
     traversal, unicode, NUL, CR/LF, ?/#, a PREFIX of a configured type, ambiguity) +
     operator-config validation (a deferred content type is refused with its real reason)
  B  a held-back (NEEDS_REVIEW) title / meta / module property / paragraph NEVER writes
     source over a stored translation - per field, on both create and update
  C  read, probe and write target the SAME state -> the SECOND run suppresses (no loop),
     proven for BOTH content states
  D  translatable-property selection both directions: junk (ids/urls/css/machine values)
     not selected AND real copy in a custom module REPORTED as skipped
  E  the shared cap is distributed round-robin across content types (no starvation)
  F  an all-held-back run records NO content hash, so a later healthy run re-attempts
  G  the fake enforces HubSpot's own validation (see above)
  H  the standing repo-law sweep: WriteBudget ceiling, mock-egress guard, ASCII CLI

Plus the three digest gotchas:
  1. CONTENT FIDELITY - the whole-document `<body original="...">` encoding is NOT ported;
     the module-tree JSON round-trips byte-identical except the translated leaves
     (ints, bools, nulls, unicode, HTML entities, deep nesting all preserved).
  2. DRAFT-VS-PUBLISHED - decided as draft (default); read/probe/write agree, proven by
     emptying the OTHER state and showing the run is unaffected, in both modes.
  3. THE PHANTOM-CLONE WINDOW - a crash between the clone and our PATCH leaves a real
     variant full of SOURCE text; it is never counted as translated, and the next run
     COMPLETES it rather than skipping it or cloning a second one.

ASCII-only prints; final line is 'verify_hubspot OK'.
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

init_env("loctms_hubspot_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import select

from app import config as app_config
from app.db import init_db
from app.main import app
from app.models import Segment, SegStatus
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.hubspot import (
    _CONTENT_TYPES,
    API_BASE,
    HubSpotConnector,
    HubSpotVariantStrategy,
    _path_token,
)

_SEG_TO_TYPE = {v["segment"]: k for k, v in _CONTENT_TYPES.items()}
_CONTENT_KEYS = {"name", "htmlTitle", "metaDescription", "layoutSections", "postBody"}


# ============================================================================
# Fake HubSpot CMS v3 API.
# ============================================================================
class FakeHubSpot:
    def __init__(self):
        # oid -> {"ctype", "language", "translatedFromId", "translations",
        #         "currentState", "draft": {...}, "published": {...}}
        self.objects: dict = {}
        self._next = 1000
        self.patches: list = []  # (oid, state, body) per ACCEPTED patch
        self.variations: list = []  # (primary_oid, language, new_oid)
        self.fail_after_create = False  # the phantom window: clone lands, response 500s
        self.patch_500: set = set()  # oids whose PATCH returns 500
        # --- round-2 MEDIUM-2 hooks: make the group dict genuinely DISAGREE with the
        # object store between two reads, so both self-heal branches really execute.
        self.ghost_variants: dict = {}  # primary oid -> {lang: id-with-no-object}
        self.drop_group_on_next_read: set = set()  # (oid, lang) -> vanish on 2nd GET
        self.remap_group_on_next_read: dict = {}  # (oid, lang) -> new id on 2nd GET
        self._group_reads: dict = {}
        self.clone_no_published = False  # a fresh clone has NO published representation

    # ---- seeding --------------------------------------------------------
    def add(self, ctype, oid=None, *, language="en", content=None, state="PUBLISHED"):
        oid = str(oid if oid is not None else self._alloc())
        body = dict(content or {})
        self.objects[oid] = {
            "ctype": ctype,
            "language": language,
            "translatedFromId": None,
            "translations": {},
            "currentState": state,
            "draft": copy.deepcopy(body),
            "published": copy.deepcopy(body),
        }
        return oid

    def _alloc(self):
        self._next += 1
        return self._next

    def rep(self, oid, state):
        return self.objects[str(oid)][state]

    def transport(self):
        return httpx.MockTransport(self._handle)

    # ---- serialization --------------------------------------------------
    def _view(self, oid, state):
        o = self.objects[str(oid)]
        translations = copy.deepcopy(o["translations"])
        translations.update(self.ghost_variants.get(str(oid)) or {})
        out = {
            "id": str(oid),
            "language": o["language"],
            "translatedFromId": o["translatedFromId"] or "",
            "translations": translations,
            "currentState": o["currentState"],
            "updatedAt": "2026-08-07T00:00:00Z",
        }
        out.update(copy.deepcopy(o[state]))
        return out

    @staticmethod
    def _json(status, payload):
        return httpx.Response(status, json=payload)

    def _bad(self, msg, status=400):
        return self._json(status, {"status": "error", "message": msg})

    # ---- routing --------------------------------------------------------
    def _handle(self, req):
        url = str(req.url)
        if not url.startswith(API_BASE + "/"):
            return self._json(404, {"message": f"wrong host/path: {url}"})
        rest = url[len(API_BASE) + 1 :].split("?")[0].strip("/")
        parts = rest.split("/")
        body = {}
        if req.content:
            try:
                body = json.loads(req.content)
            except ValueError:
                return self._bad("body is not JSON")

        # segment is the first TWO path components for every supported type
        if len(parts) < 2:
            return self._json(404, {"message": f"unknown path {rest}"})
        segment = "/".join(parts[:2])
        ctype = _SEG_TO_TYPE.get(segment)
        if ctype is None:
            return self._json(404, {"message": f"unknown resource {segment}"})
        tail = parts[2:]

        if not tail:  # LIST
            if req.method != "GET":
                return self._bad("method not allowed", 405)
            return self._list(ctype, req)

        if tail[:2] == ["multi-language", "create-language-variation"]:
            if req.method != "POST":
                return self._bad("method not allowed", 405)
            return self._create_variation(ctype, body)

        oid = tail[0]
        state = "published"
        if len(tail) == 2 and tail[1] == "draft":
            state = "draft"
        elif len(tail) != 1:
            return self._json(404, {"message": f"unknown path {rest}"})
        if oid not in self.objects or self.objects[oid]["ctype"] != ctype:
            # A GHOST id (named in a group dict, no object behind it) 404s — and THAT is
            # the moment a human's delete/replace becomes visible on the group. Firing the
            # disagreement here (rather than on a read count) is deterministic no matter
            # how many times discovery happened to read the primary first, so both
            # self-heal branches are genuinely exercised.
            self._resolve_ghost(oid)
            return self._json(404, {"message": f"no such object {oid}"})
        if self.objects[oid][state] is None:
            # A representation that genuinely does not exist (a brand-new variation has no
            # published version until someone publishes it) -> 404, like HubSpot.
            return self._json(404, {"message": f"no {state} for {oid}"})
        if req.method == "GET":
            return self._json(200, self._view(oid, state))
        if req.method == "PATCH":
            return self._patch(ctype, oid, state, body)
        return self._bad("method not allowed", 405)

    def _resolve_ghost(self, ghost_id: str) -> None:
        """Apply any pending group disagreement for the primary that names `ghost_id`:
        the entry either VANISHES (a human deleted the variation) or RE-POINTS at a real
        object (a human replaced it). Idempotent — fires once."""
        for primary, langs in list(self.ghost_variants.items()):
            for lang, entry in list(langs.items()):
                if str((entry or {}).get("id")) != str(ghost_id):
                    continue
                key = (primary, lang)
                if key in self.drop_group_on_next_read:
                    langs.pop(lang, None)
                    self.drop_group_on_next_read.discard(key)
                elif key in self.remap_group_on_next_read:
                    new_id = self.remap_group_on_next_read.pop(key)
                    langs.pop(lang, None)
                    self.objects[primary]["translations"][lang] = {
                        "id": str(new_id),
                        "slug": f"{lang}/x",
                    }

    def _list(self, ctype, req):
        limit = int(req.url.params.get("limit") or 100)
        after = req.url.params.get("after")
        ids = sorted(
            (oid for oid, o in self.objects.items() if o["ctype"] == ctype), key=lambda x: int(x)
        )
        start = (ids.index(after) + 1) if (after in ids) else 0
        page = ids[start : start + limit]
        results = [self._view(oid, "published") for oid in page]
        payload = {"results": results, "total": len(ids)}
        if start + limit < len(ids) and page:
            payload["paging"] = {"next": {"after": page[-1]}}
        return self._json(200, payload)

    def _patch(self, ctype, oid, state, body):
        """STRICT (taxonomy G): an unknown top-level key, or the wrong body field for the
        content type, is a 400 — which is what makes the payload assertions non-vacuous."""
        if oid in self.patch_500:
            return self._json(500, {"message": "internal error"})
        if not isinstance(body, dict) or not body:
            return self._bad("empty patch body")
        unknown = [k for k in body if k not in _CONTENT_KEYS]
        if unknown:
            return self._bad(f"unknown propert(ies): {', '.join(sorted(unknown))}")
        wants = _CONTENT_TYPES[ctype]["body"]
        if wants == "layout" and "postBody" in body:
            return self._bad("postBody is not a property of a page")
        if wants == "html" and "layoutSections" in body:
            return self._bad("layoutSections is not a property of a blog post")
        rep = self.objects[oid][state]
        for k, v in body.items():
            rep[k] = copy.deepcopy(v)
        self.patches.append((oid, state, copy.deepcopy(body)))
        return self._json(200, self._view(oid, state))

    def _create_variation(self, ctype, body):
        oid = str((body or {}).get("id") or "")
        lang = str((body or {}).get("language") or "").strip().lower()
        if oid not in self.objects or self.objects[oid]["ctype"] != ctype:
            return self._bad(f"no such object {oid}", 404)
        src = self.objects[oid]
        if src["translatedFromId"]:
            # digest §6.5: HubSpot's group model has ONE primary. A connector that did
            # not resolve-to-primary would land here.
            return self._bad(
                "cannot create a language variation from a translation; "
                "use the primary language object"
            )
        if not lang:
            return self._bad("language is required")
        if lang == str(src["language"]).lower():
            return self._bad("language matches the primary language")
        if lang in src["translations"]:
            # A connector that did not consult the native `translations` dict first would
            # land here (and would be trying to clone a second variant).
            return self._bad(f"a {lang} variation already exists for {oid}")
        new_oid = str(self._alloc())
        # THE CLONE (digest §6.4): the full source content, in DRAFT.
        self.objects[new_oid] = {
            "ctype": ctype,
            "language": lang,
            "translatedFromId": oid,
            "translations": {},
            "currentState": "DRAFT",
            "draft": copy.deepcopy(src["draft"]),
            "published": (None if self.clone_no_published else copy.deepcopy(src["published"])),
        }
        src["translations"][lang] = {"id": new_oid, "slug": f"{lang}/page-{new_oid}"}
        self.variations.append((oid, lang, new_oid))
        if self.fail_after_create:
            # THE PHANTOM WINDOW: the clone HAS landed server-side; the client never
            # learns its id.
            return self._json(500, {"message": "gateway error after create"})
        return self._json(201, self._view(new_oid, "draft"))


# --- fixtures ---------------------------------------------------------------
def _layout():
    """A realistic HubSpot module tree. Selected leaves (allowlist order, DFS with sorted
    keys): main/widgets/w1/body/html, main/widgets/w2/body/button_text,
    main/widgets/w2/body/headline. Everything else must survive byte-identical."""
    return {
        "main": {
            "cells": {},
            "name": "main",
            "type": "cell",
            "widgets": {
                "w1": {
                    "body": {
                        "css_class": "span12 widget-span",
                        "html": "<p>Hello prose here.</p>",
                        "module_id": 12345,
                    },
                    "id": "w1",
                    "name": "rich_text_1",
                    "order": 0,
                    "type": "module",
                },
                "w2": {
                    "body": {
                        "button_text": "Buy now",
                        "count": 7,
                        "custom_blurb": "A custom module property holding copy.",
                        "enabled": True,
                        "headline": "Big Headline",
                        "link_url": "https://acme.example/x",
                        "nothing": None,
                        "data_path": "/café/&amp;—/« x »",
                        "value": "MACHINE-42",
                    },
                    "id": "w2",
                    "name": "custom_widget_1",
                    "order": 1,
                    "type": "module",
                },
            },
        },
    }


def _probe_layout():
    """The REVIEWER's HIGH-1 probe fixture, both directions in one tree.

    MUST NOT be selected (all four sit under ORDINARY text-property names, which is
    exactly why a name-only gate let them through to MT and to the wire):
      content -> a HubL module_block          title -> a {{ page.name }} expression
      text    -> a CDN URL                    label -> a contact email
    MUST be selected (the class the old prose heuristic silently dropped: short, some
    without a space):
      cta_label="Subscribe"   banner_copy="Learn more"   hero_sub="Fast"
    (cta_label / banner_copy / hero_sub are NOT in the default name allowlist, so they
    must at minimum be REPORTED; with `fields` set they must be SELECTED.)"""
    return {
        "main": {
            "widgets": {
                "m1": {
                    "body": {
                        "content": "{% module_block module 'hero' path='@hubspot/rich_text' %}",
                        "title": "{{ page.name }} | {{ content.name }}",
                        "text": "https://cdn2.hubspot.net/hubfs/123/asset.png",
                        "label": "sales@acme.example",
                        "cta_label": "Subscribe",
                        "banner_copy": "Learn more",
                        "hero_sub": "Fast",
                        # letter-free under an ALLOWED name -> the VALUE gate is what rejects it
                        "caption": "---- 42.00 ----",
                    },
                    "id": "m1",
                    "type": "module",
                },
            }
        },
    }


_SEL_PATHS = [
    ["main", "widgets", "w1", "body", "html"],
    ["main", "widgets", "w2", "body", "button_text"],
    ["main", "widgets", "w2", "body", "headline"],
]


def _page_content(name="Landing hero"):
    return {
        "name": name,
        "htmlTitle": "SEO title",
        "metaDescription": "SEO description",
        "layoutSections": _layout(),
    }


def _post_content(name="First post"):
    return {
        "name": name,
        "htmlTitle": "Post SEO",
        "metaDescription": "Post meta",
        "postBody": "<p>First para.</p><p>Second para.</p>",
    }


def _conn(name, cfg_extra=None):
    cfg = {"content_types": "site-pages", "source_language": "en", "target_locales": "fr, de"}
    cfg.update(cfg_extra or {})
    return cstore.create("hubspot", name, cfg, {"access_token": "pat-na1-x"}, actor="admin")


def _cc(fh, cfg=None):
    base = {"content_types": "site-pages", "source_language": "en"}
    base.update(cfg or {})
    return HubSpotConnector(base, {"access_token": "pat-na1-x"}, transport=fh.transport())


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


def _variant_of(fh, primary_oid, lang):
    return (fh.objects[str(primary_oid)]["translations"].get(lang) or {}).get("id")


def main() -> None:
    from app.web._routes import walk_routes

    init_db()
    import app.services.translate as T

    # ---- (1) capability, strategy, test(), content-state + deferred-type config ----
    assert HubSpotConnector.capability.topology == "T3"
    assert HubSpotVariantStrategy().topology == "T3"
    fh = FakeHubSpot()
    fh.add("site-pages", 1, content=_page_content())
    cc = _cc(fh)
    ok, msg = cc.test()
    assert ok, msg
    assert "draft" in msg, ("test() names the state it will write", msg)
    assert cc.content_state == "draft", "draft is the DEFAULT state"
    assert _cc(fh, {"content_state": "published"}).content_state == "published"
    for bad_state in ("live", "DRAFTS", "1", "publish"):
        raised = False
        try:
            _cc(fh, {"content_state": bad_state}).content_state  # noqa: B018
        except ConnectorError:
            raised = True
        assert raised, ("an unknown content_state is refused fail-closed", bad_state)
    # A DEFERRED content type is refused with its REAL reason, never silently ignored.
    for deferred in ("marketing-emails", "forms", "hubdb"):
        raised = ""
        try:
            _cc(fh, {"content_types": f"site-pages, {deferred}"}).content_types()
        except ConnectorError as e:
            raised = str(e)
        assert "not supported" in raised.lower() and "half-built" in raised.lower(), (
            "deferred type refused with the real reason",
            deferred,
            raised,
        )
    raised = ""
    try:
        _cc(fh, {"content_types": "products"}).content_types()
    except ConnectorError as e:
        raised = str(e)
    assert "Unsupported" in raised, raised
    print(
        "  connector: T3 capability + native-linkage strategy, test(), draft default, "
        "content_state fail-closed, emails/forms/HubDB refused with the real reason"
    )

    # ---- (2) taxonomy A: caller-supplied id scope + shape, fail-closed ----
    cc2 = _cc(fh, {"content_types": "site-pages"})
    assert cc2._validate_aid("site-pages:123") == ("site-pages", "123")
    assert cc2._validate_aid("77") == ("site-pages", "77"), "bare id ok for ONE type"
    for bad in (
        "blog-posts:1",  # type not configured
        "site-page:1",  # PREFIX of a configured type
        "site-pagess:1",  # superstring of a configured type
        "site-pages:../../x",  # traversal
        "site-pages:%2e%2e%2f1",  # encoded traversal
        "site-pages:1/../2",
        "site-pages:1?x=1",
        "site-pages:1#f",
        "site-pages:1\x00",
        "site-pages:1\r\n",
        "site-pages:١٢",
        "site-pages:",
        "site-pages:abc",
        "",
        "   ",
        "site-pages:" + "9" * 21,  # over the digit bound
        "../pages/site-pages/1",
    ):
        raised = False
        try:
            cc2._validate_aid(bad)
        except ConnectorError:
            raised = True
        assert raised, ("A: unsafe/out-of-scope object id refused", bad)
    # ambiguity is refused rather than guessed when >1 type is configured
    cc2b = _cc(fh, {"content_types": "site-pages, blog-posts"})
    raised = False
    try:
        cc2b._validate_aid("123")
    except ConnectorError:
        raised = True
    assert raised, "A: a bare id is ambiguous for a multi-type connection -> refused"
    assert cc2b._validate_aid("blog-posts:5") == ("blog-posts", "5")
    # language tags are allowlisted too (they reach a request body)
    for bad_lang in ("fr/../x", "fr\r\n", "fr de", "", "١", "fr;drop"):
        raised = False
        try:
            HubSpotConnector._validate_language(bad_lang)
        except ConnectorError:
            raised = True
        assert raised, ("A: unsafe language tag refused", bad_lang)
    assert HubSpotConnector._validate_language("es-mx") == "es-mx"
    # end-to-end: an out-of-scope / malformed id writes NOTHING
    fh2 = FakeHubSpot()
    fh2.add("site-pages", 1, content=_page_content())
    fh2.add("blog-posts", 2, content=_post_content())
    c2 = _conn("scope", {"target_locales": "fr"})
    for bad_id in ("blog-posts:2", "site-pages:../2", "nope", "site-page:1"):
        fh2.patches.clear()
        fh2.variations.clear()
        cstore.hubspot_translate(
            c2.id, article_ids=[bad_id], actor="attacker", transport=fh2.transport()
        )
        assert not fh2.patches and not fh2.variations, (
            bad_id,
            "no variation and no patch for an unsafe/out-of-scope id",
        )
    print(
        "  A: caller <content-type>:<id> scope+shape validated (traversal, encoded, "
        "unicode, NUL, CRLF, ?/#, prefix, bound, ambiguity); nothing written"
    )

    # ---- (3) taxonomy D: selection both directions, and the skip is REPORTED ----
    cc3 = _cc(fh)
    leaves = cc3._collect_layout_units(_layout())
    assert [p for p, _k, _t in leaves] == _SEL_PATHS, (
        "only text properties selected",
        [p for p, _k, _t in leaves],
    )
    assert cc3._sel_selected == {"html", "button_text", "headline"}, cc3._sel_selected
    # REAL COPY in a custom module is reported (the reference's silent-skip defect)
    assert "custom_blurb" in cc3._sel_skipped and "allowlist" in cc3._sel_skipped["custom_blurb"], (
        cc3._sel_skipped
    )
    # machine values are not selected at all
    for junk in ("link_url", "css_class", "module_id", "value", "id", "type", "order"):
        assert junk not in cc3._sel_selected, ("D: junk must not be selected", junk)
    # allowlist overrides the default name set, but NEVER unlocks machine names
    cc3b = _cc(fh, {"fields": "custom_blurb, link_url, css_class"})
    leavesB = cc3b._collect_layout_units(_layout())
    assert [k for _p, k, _t in leavesB] == ["custom_blurb"], (
        "allowlist selects the custom property; url/class stay refused",
        leavesB,
    )
    cc3c = _cc(fh, {"exclude_fields": "headline"})
    leavesC = cc3c._collect_layout_units(_layout())
    assert [k for _p, k, _t in leavesC] == ["html", "button_text"], leavesC
    print(
        "  D: allowlist selection; ids/urls/css/machine values never selected; real "
        "copy in a custom module REPORTED as skipped; fields/exclude_fields override"
    )

    # ---- (3b) HIGH-1: the VALUE gate + honest reporting (the reviewer's probe) ----
    ccP = _cc(fh)
    leavesP = ccP._collect_layout_units(_probe_layout())
    picked = {k for _p, k, _t in leavesP}
    # DIRECTION 1 - four machine values under ORDINARY text-property names must NOT be
    # selected (a name-only gate sent all four to MT and wrote them).
    for name in ("content", "title", "text", "label"):
        assert name not in picked, (
            "HIGH-1: a machine VALUE must not be selected on the "
            "strength of its property name alone",
            name,
            picked,
        )
    assert "HubSpot template expression" in ccP._sel_skipped["content"], ccP._sel_skipped
    assert "HubSpot template expression" in ccP._sel_skipped["title"], ccP._sel_skipped
    assert "URL-shaped value" in ccP._sel_skipped["text"], ccP._sel_skipped
    assert "email-shaped value" in ccP._sel_skipped["label"], ccP._sel_skipped
    from app.services.connectors.hubspot import _value_is_machine

    assert "caption" not in picked and "no letters" in (
        _value_is_machine("---- 42.00 ----") or ""
    ), ccP._sel_skipped
    # LOW-5 (round 2): a letter-free reject is REPORTED, not invisible - "24/7", "100%",
    # "9:00" and dates are genuinely localizable, so the operator must at least see them.
    assert "no letters" in ccP._sel_skipped.get("caption", ""), (
        "LOW-5: letter-free rejects are reported (the reason was unreachable before)",
        ccP._sel_skipped,
    )
    for real_copy in ("Subscribe", "Learn more", "Fast", "Cafe au lait"):
        assert _value_is_machine(real_copy) is None, (
            "value SHAPE alone must never reject real copy",
            real_copy,
        )
    # DIRECTION 2 - short real copy must be REPORTED (it was invisible: fields_skipped {})
    for name in ("cta_label", "banner_copy", "hero_sub"):
        assert name in ccP._sel_skipped and "allowlist" in ccP._sel_skipped[name], (
            "HIGH-1: short custom-module copy must be REPORTED, not silently dropped",
            name,
            ccP._sel_skipped,
        )
    # ... and allowlisting them SELECTS them (they are real copy, not machine values)
    ccP2 = _cc(fh, {"fields": "cta_label, banner_copy, hero_sub, content, text, label"})
    leavesP2 = ccP2._collect_layout_units(_probe_layout())
    picked2 = {k for _p, k, _t in leavesP2}
    assert picked2 == {"cta_label", "banner_copy", "hero_sub"}, (
        "HIGH-1: the allowlist selects real copy but NEVER bypasses the value gate - a "
        "mangled HubL expression is an unrenderable page, not a bad translation",
        picked2,
    )
    # end to end: none of the four machine values reaches the wire
    fhP = FakeHubSpot()
    pP = fhP.add(
        "site-pages",
        15,
        content={
            "name": "Probe",
            "htmlTitle": "",
            "metaDescription": "",
            "layoutSections": _probe_layout(),
        },
    )
    cP = _conn("probe", {"target_locales": "fr"})
    okP, msgP, sP = cstore.hubspot_translate(cP.id, actor="auto", transport=fhP.transport())
    assert okP, msgP
    wrote = json.dumps(fhP.rep(_variant_of(fhP, pP, "fr"), "draft")["layoutSections"])
    assert "[fr] {%" not in wrote and "[fr] {{" not in wrote, ("HubL never MT'd", wrote)
    assert "[fr] https://" not in wrote and "[fr] sales@" not in wrote, wrote
    assert set(sP["fields_skipped"]) >= {
        "content",
        "title",
        "text",
        "label",
        "cta_label",
        "banner_copy",
        "hero_sub",
    }, ("the run RESULT reports every unselected text leaf", sP["fields_skipped"])
    assert "Skipped text propert(ies)" in msgP, msgP
    print(
        "  HIGH-1: value gate rejects HubL/URL/email/letter-free under ordinary names "
        "(both modes); every unselected leaf with a letter is REPORTED; short copy "
        "('Subscribe', 'Fast') is reported and allowlistable"
    )

    # ---- (3c) round-2 HIGH-1: the gate covers EVERY outgoing string, not layoutSections ----
    # Probed by the reviewer: htmlTitle "{{ page.name }} | Acme" was written as
    # "[fr] {{ page.name }} | Acme" while the IDENTICAL string in a module property was
    # blocked. HubL SEO titles are a documented HubSpot feature -> the likelier carrier.
    fhG = FakeHubSpot()
    pG = fhG.add(
        "site-pages",
        16,
        content={
            "name": "{{ content.name }}",  # ENTIRELY expression -> dropped
            "htmlTitle": "{{ page.name }} | Acme",  # MIXED -> masked + translated
            "metaDescription": "{% if x %}{% endif %}",  # ENTIRELY expression -> dropped
            "layoutSections": {},
        },
    )
    cG = _conn("meta-gate", {"target_locales": "fr"})
    okG, msgG, sG = cstore.hubspot_translate(cG.id, actor="auto", transport=fhG.transport())
    assert okG, msgG
    vG = _variant_of(fhG, pG, "fr")
    repG = fhG.rep(vG, "draft")
    assert repG["name"] == "{{ content.name }}", (
        "round-2 HIGH-1: an entirely-expression title is never MT'd",
        repG["name"],
    )
    assert repG["metaDescription"] == "{% if x %}{% endif %}", (
        "round-2 HIGH-1: metaDescription is gated too",
        repG["metaDescription"],
    )
    assert repG["htmlTitle"] == "[fr] {{ page.name }} | Acme", (
        "MEDIUM-4: mixed HubL+prose IS translated with the token restored byte-exact",
        repG["htmlTitle"],
    )
    # The TITLE is special (Segment order 0, the Strapi-H2 source-fallback site), so its
    # drop is also a per-pair held field. A gated-out META value belongs in the SELECTION
    # report only: the decision is deterministic, so it must not enter the retry-bearing
    # `incomplete` channel.
    heldG = {f for p in (sG.get("incomplete") or []) for f in p["held_fields"]}
    assert "name" in heldG, ("the dropped title is held + reported", sG.get("incomplete"))
    assert {"name", "metaDescription"} <= set(sG["fields_skipped"]), (
        "round-2 HIGH-1: every gated meta/title value is REPORTED, not silent",
        sG["fields_skipped"],
    )

    # postBody paragraphs are gated + masked too (they went out ungated before)
    fhG2 = FakeHubSpot()
    bG2 = fhG2.add(
        "blog-posts",
        17,
        content={
            "name": "Post",
            "htmlTitle": "",
            "metaDescription": "",
            "postBody": (
                "<p>Welcome back, {{ contact.firstname }}! Save 20% today.</p>"
                "<p>{% module_block m 'x' %}{% end_module_block %}</p>"
            ),
        },
    )
    cG2 = _conn("post-gate", {"content_types": "blog-posts", "target_locales": "fr"})
    okG2, msgG2, _sG2 = cstore.hubspot_translate(cG2.id, actor="auto", transport=fhG2.transport())
    assert okG2, msgG2
    bodyG2 = fhG2.rep(_variant_of(fhG2, bG2, "fr"), "draft")["postBody"]
    assert "[fr] Welcome back, {{ contact.firstname }}! Save 20% today." in bodyG2, (
        "MEDIUM-4: a mixed HubL paragraph is translated with the token intact",
        bodyG2,
    )
    assert "[fr] {% module_block" not in bodyG2 and "{% module_block m 'x' %}" in bodyG2, (
        "round-2 HIGH-1: an entirely-expression paragraph is never MT'd",
        bodyG2,
    )
    print(
        "  round-2 HIGH-1: the value gate covers name/htmlTitle/metaDescription and "
        "every postBody paragraph, not just layoutSections"
    )

    # ---- (3d) MEDIUM-4: HubL masking round-trip + damaged-token refusal ----
    from app.services.connectors.hubspot import mask_hubl, unmask_hubl

    src4 = "Welcome back, {{ contact.firstname }}! Save {% if v %}20%{% endif %} today."
    masked, spans = mask_hubl(src4)
    assert "{{" not in masked and "{%" not in masked and len(spans) == 3, (masked, spans)
    assert "⟦h1⟧" in masked, (
        "the SHARED segment placeholder primitive is used, not a bespoke marker",
        masked,
    )
    back, ok4 = unmask_hubl(masked, spans)
    assert ok4 and back == src4, ("byte-exact round trip", back)
    back, ok4 = unmask_hubl("[fr] " + masked, spans)
    assert ok4 and back == "[fr] " + src4, back
    for damaged in (
        masked.replace("⟦h2⟧", ""),  # token DROPPED
        masked + "⟦h1⟧",  # token DUPLICATED
        masked.replace("⟦h1⟧", "[h1]"),  # token MANGLED
        masked + "⟦x9⟧",
    ):  # token INVENTED
        _b, ok_bad = unmask_hubl(damaged, spans)
        assert not ok_bad, ("a damaged token must never be silently written", damaged)
    # end to end: a damaged token HOLDS the field instead of writing it broken
    import app.services.translate as _T4

    fhD = FakeHubSpot()
    pD = fhD.add(
        "site-pages",
        18,
        content={
            "name": "Hello {{ contact.firstname }}",
            "htmlTitle": "Plain SEO title",
            "metaDescription": "",
            "layoutSections": {},
        },
    )
    cD = _conn("damaged", {"target_locales": "fr"})
    _orig4 = _T4.pretranslate_document

    def _mangling(s, doc_id, loc, *a, **kw):
        out = _orig4(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if sg.target_text and "⟦h1⟧" in sg.target_text:
                sg.target_text = sg.target_text.replace("⟦h1⟧", "")
                s.add(sg)
        s.commit()
        return out

    set_pieces("app.services.translate", "pretranslate_document", _mangling)
    try:
        _okD, _msgD, sD = cstore.hubspot_translate(cD.id, actor="auto", transport=fhD.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", _orig4)
    vD = _variant_of(fhD, pD, "fr")
    assert fhD.rep(vD, "draft")["name"] == "Hello {{ contact.firstname }}", (
        "MEDIUM-4: a damaged token is NEVER written (the clone's text stands)",
        fhD.rep(vD, "draft")["name"],
    )
    assert fhD.rep(vD, "draft")["htmlTitle"] == "[fr] Plain SEO title", (
        "the undamaged sibling field still lands",
        fhD.rep(vD, "draft")["htmlTitle"],
    )
    reasonsD = " ".join(r for p in (sD.get("incomplete") or []) for r in p["reasons"].values())
    assert "did not survive translation intact" in reasonsD, (
        "MEDIUM-4: the damaged token is reported",
        sD.get("incomplete"),
    )
    # #307: an expression added to a source with NO masks must also be held at delivery.
    fh_inj = FakeHubSpot()
    p_inj = fh_inj.add(
        "site-pages",
        19,
        content={
            "name": "Hello",
            "htmlTitle": "Safe sibling",
            "metaDescription": "",
            "layoutSections": {},
        },
    )
    c_inj = _conn("injected", {"target_locales": "fr"})

    def _inject(s, doc_id, loc, *a, **kw):
        result = _orig4(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if sg.source_text == "Hello":
                sg.target_text = "Bonjour {{ request.query_dict.q }}"
                s.add(sg)
        s.commit()
        return result

    set_pieces("app.services.translate", "pretranslate_document", _inject)
    try:
        _, _, report = cstore.hubspot_translate(
            c_inj.id, actor="auto", transport=fh_inj.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", _orig4)
    translated = fh_inj.rep(_variant_of(fh_inj, p_inj, "fr"), "draft")
    assert translated["name"] == "Hello", translated
    assert translated["htmlTitle"] == "[fr] Safe sibling", translated
    assert any("added" in r for p in report["incomplete"] for r in p["reasons"].values())
    print(
        "  MEDIUM-4: HubL spans are masked with the shared segment placeholder, restored "
        "byte-exact, and a dropped/duplicated/mangled/invented token holds the field"
    )

    # ---- (4) GOTCHA 1: module-tree fidelity (no <body original> encoding at all) ----
    fh4 = FakeHubSpot()
    p4 = fh4.add("site-pages", 10, content=_page_content())
    c4 = _conn("fidelity", {"target_locales": "fr"})
    ok, msg, _s4 = cstore.hubspot_translate(c4.id, actor="auto", transport=fh4.transport())
    assert ok, msg
    v4 = _variant_of(fh4, p4, "fr")
    assert v4, ("a language variation was created", fh4.variations)
    expected = copy.deepcopy(_layout())
    src_layout = _layout()
    for path in _SEL_PATHS:
        cur = src_layout
        for step in path[:-1]:
            cur = cur[step]
        src_text = cur[path[-1]]
        tgt = expected
        for step in path[:-1]:
            tgt = tgt[step]
        tgt[path[-1]] = f"[fr] {src_text}"
    got = fh4.rep(v4, "draft")["layoutSections"]
    assert got == expected, (
        "GOTCHA 1: everything except the translated leaves round-trips "
        "byte-identical (ints, bools, null, unicode, entities, nesting)",
        json.dumps(got, ensure_ascii=False)[:400],
    )
    # and the SOURCE page is never touched
    assert fh4.rep(p4, "draft")["layoutSections"] == _layout(), "source layout untouched"
    assert fh4.rep(p4, "published")["layoutSections"] == _layout(), "source untouched"
    print(
        "  GOTCHA 1: layoutSections merged path-by-path into a deep copy - unicode, "
        "entities, ints, bools, null and nesting survive byte-identical; source untouched"
    )

    # ---- (5) happy path across BOTH body models (pages + blog posts) ----
    fh5 = FakeHubSpot()
    p5 = fh5.add("site-pages", 20, content=_page_content("Hero"))
    b5 = fh5.add("blog-posts", 21, content=_post_content("Post one"))
    c5 = _conn("happy", {"content_types": "site-pages, blog-posts", "target_locales": "fr, de"})
    ok, msg, s5 = cstore.hubspot_translate(c5.id, actor="auto", transport=fh5.transport())
    assert ok, msg
    assert s5["created"] == 4 and s5["updated"] == 0, s5  # 2 objects x 2 languages
    for oid in (p5, b5):
        for lang in ("fr", "de"):
            vid = _variant_of(fh5, oid, lang)
            assert vid, (oid, lang, "variation created")
            rep = fh5.rep(vid, "draft")
            assert rep["name"].startswith(f"[{lang}] "), rep["name"]
            assert rep["htmlTitle"].startswith(f"[{lang}] "), rep["htmlTitle"]
            assert rep["metaDescription"].startswith(f"[{lang}] "), rep["metaDescription"]
    post_fr = fh5.rep(_variant_of(fh5, b5, "fr"), "draft")["postBody"]
    assert "[fr] First para." in post_fr and "[fr] Second para." in post_fr, post_fr
    assert fh5.rep(b5, "draft")["postBody"] == _post_content()["postBody"], "source post kept"
    pulled = _cc(fh5, {"content_types": "site-pages"}).pull()
    assert any(p.field == "name" for p in pulled) and any(
        str(p.field).startswith("b") for p in pulled
    ), pulled[:4]
    assert "Note: translations were written to each variation's DRAFT" in msg, msg
    print(
        f"  happy path: {s5['created']} variations across pages (module tree) and blog "
        f"posts (postBody via the shared html filter); sources untouched; draft note shown"
    )

    # ---- (6) GOTCHA 2 + taxonomy C: read/probe/write agree on DRAFT; run 2 suppresses ----
    fh6 = FakeHubSpot()
    p6 = fh6.add("site-pages", 30, content=_page_content())
    c6 = _conn("steady", {"target_locales": "fr"})
    ok, _m, r6a = cstore.hubspot_translate(c6.id, actor="auto", transport=fh6.transport())
    assert ok and r6a["created"] == 1, r6a
    v6 = _variant_of(fh6, p6, "fr")
    # the WRITE went to the DRAFT only: published still holds the cloned SOURCE text
    assert fh6.rep(v6, "draft")["name"].startswith("[fr] ")
    assert fh6.rep(v6, "published")["name"] == "Landing hero", (
        "GOTCHA 2: draft mode must not touch the published representation",
        fh6.rep(v6, "published"),
    )
    assert all(st == "draft" for _o, st, _b in fh6.patches), fh6.patches
    _probe = cstore.build_fresh(c6.id, fh6.transport())
    assert _probe.strategy.exists_target(_probe, f"site-pages:{p6}", "fr"), (
        "the tracked-path probe finds the draft fully covered"
    )
    ok, _m, r6b = cstore.hubspot_translate(c6.id, actor="auto", transport=fh6.transport())
    assert ok and r6b["created"] == 0 and r6b["updated"] == 0 and r6b["skipped"] == 1, (
        "C: run 2 suppresses via the tracked-set probe + content hash (no loop)",
        r6b,
    )
    # DECISIVE: wrecking the PUBLISHED representation must not change anything, because
    # read, probe and write all target the draft.
    fh6.rep(v6, "published")["name"] = ""
    fh6.rep(v6, "published")["layoutSections"] = {}
    ok, _m, r6c = cstore.hubspot_translate(c6.id, actor="auto", transport=fh6.transport())
    assert ok and r6c["skipped"] == 1 and r6c["created"] == 0 and r6c["updated"] == 0, (
        "GOTCHA 2: the probe reads the DRAFT, not the published rep",
        r6c,
    )
    # but wrecking the DRAFT (what we actually wrote) DOES surface for restore
    fh6.rep(v6, "draft")["layoutSections"]["main"]["widgets"]["w1"]["body"]["html"] = ""
    ok, _m, r6d = cstore.hubspot_translate(c6.id, actor="auto", transport=fh6.transport())
    assert ok and r6d["updated"] == 1 and r6d["skipped"] == 0, (
        "a tracked path emptied in the state we wrote surfaces for restore",
        r6d,
    )
    print(
        "  C + GOTCHA 2 (draft): write/read/probe all on the draft; run 2 skips; "
        "wrecking the PUBLISHED rep changes nothing; wrecking the DRAFT restores"
    )

    # ---- (6b) GOTCHA 2, the other mode: published reads/probes/writes published ----
    fh6b = FakeHubSpot()
    p6b = fh6b.add("site-pages", 31, content=_page_content())
    c6b = _conn("steady-pub", {"target_locales": "fr", "content_state": "published"})
    ok, msg, r6e = cstore.hubspot_translate(c6b.id, actor="auto", transport=fh6b.transport())
    assert ok and r6e["created"] == 1 and r6e["content_state"] == "published", r6e
    v6b = _variant_of(fh6b, p6b, "fr")
    assert fh6b.rep(v6b, "published")["name"].startswith("[fr] ")
    assert fh6b.rep(v6b, "draft")["name"] == "Landing hero", (
        "published mode must not touch the draft",
        fh6b.rep(v6b, "draft"),
    )
    assert all(st == "published" for _o, st, _b in fh6b.patches), fh6b.patches
    assert "set to 'published'" in msg, msg
    fh6b.rep(v6b, "draft")["name"] = ""  # wreck the OTHER state
    ok, _m, r6f = cstore.hubspot_translate(c6b.id, actor="auto", transport=fh6b.transport())
    assert ok and r6f["skipped"] == 1, ("published mode: run 2 steady state", r6f)
    print(
        "  GOTCHA 2 (published mode): the same three operations move together; run 2 "
        "still steady; the draft rep is never touched"
    )

    # ---- (6c) HIGH-2: a content_state FLIP must force a full re-write, both ways ----
    # The runbook's own MANDATORY-1 revert path. With state-less tracking keys the OTHER
    # state's representation is the CLONE (pure source text) and is non-empty at exactly
    # the tracked paths, so the presence-only probe said "covered" and the run reported
    # "Skipped 1 unchanged" + "written to the live object" over untranslated content.
    for first, second in (("draft", "published"), ("published", "draft")):
        fhF = FakeHubSpot()
        pF = fhF.add("site-pages", 32, content=_page_content())
        cF = _conn(f"flip-{first}", {"target_locales": "fr", "content_state": first})
        ok, _m, rF1 = cstore.hubspot_translate(cF.id, actor="auto", transport=fhF.transport())
        assert ok and rF1["created"] == 1, rF1
        vF = _variant_of(fhF, pF, "fr")
        assert fhF.rep(vF, second)["name"] == "Landing hero", (
            "setup: the OTHER state still holds the clone's SOURCE text",
            first,
        )
        cstore.patch_config(cF.id, {"content_state": second})  # the revert path
        ok, _msgF, rF2 = cstore.hubspot_translate(cF.id, actor="auto", transport=fhF.transport())
        assert ok and rF2["skipped"] == 0 and (rF2["created"] + rF2["updated"]) == 1, (
            f"HIGH-2: {first}->{second} must be a FULL re-write, not 'skipped unchanged'",
            rF2,
        )
        assert fhF.rep(vF, second)["name"].startswith("[fr] "), (
            f"HIGH-2: the {second} representation is now really translated",
            fhF.rep(vF, second),
        )
        # and the new state settles on its own next run (no re-write loop)
        ok, _m, rF3 = cstore.hubspot_translate(cF.id, actor="auto", transport=fhF.transport())
        assert ok and rF3["skipped"] == 1, (f"{second} settles", rF3)
        cfgF = cstore.get(cF.id).config or {}
        assert f"site-pages:{pF}:fr:{first}" in (
            cfgF.get("hubspot_written") or {}
        ) and f"site-pages:{pF}:fr:{second}" in (cfgF.get("hubspot_written") or {}), (
            "HIGH-2: tracking is keyed per state",
            cfgF.get("hubspot_written"),
        )
    print(
        "  HIGH-2: flipping content_state (the runbook's own revert path) forces a FULL "
        "re-write in the new state, both directions, then settles; tracking is "
        "state-keyed while the content hash stays state-less"
    )

    # ---- (7) GOTCHA 3: the phantom-clone window ----
    fh7 = FakeHubSpot()
    p7 = fh7.add("site-pages", 40, content=_page_content())
    c7 = _conn("phantom", {"target_locales": "fr"})
    fh7.fail_after_create = True
    ok, msg, r7a = cstore.hubspot_translate(c7.id, actor="auto", transport=fh7.transport())
    assert not ok and r7a["failures"], ("the create-window crash is a real failure", r7a)
    v7 = _variant_of(fh7, p7, "fr")
    assert v7, "the clone DID land server-side (this is the phantom)"
    assert fh7.rep(v7, "draft")["name"] == "Landing hero", (
        "the phantom is 100% SOURCE text",
        fh7.rep(v7, "draft"),
    )
    assert not fh7.patches, "nothing was written into the phantom"
    # a dict-presence probe would now call this pair 'translated'. Ours must NOT.
    _probe7 = cstore.build_fresh(c7.id, fh7.transport())
    assert not _probe7.strategy.exists_target(_probe7, f"site-pages:{p7}", "fr"), (
        "GOTCHA 3: a variation's mere EXISTENCE is never 'translated'"
    )
    assert not (cstore.get(c7.id).config or {}).get("hubspot_hashes"), (
        "no suppression hash was recorded for the failed pair",
        (cstore.get(c7.id).config or {}).get("hubspot_hashes"),
    )
    # the re-run must COMPLETE the phantom, not skip it and not clone a second one
    fh7.fail_after_create = False
    n_before = len(fh7.variations)
    ok, msg, r7b = cstore.hubspot_translate(c7.id, actor="auto", transport=fh7.transport())
    assert ok and (r7b["created"] + r7b["updated"]) == 1, ("the re-run completes it", r7b)
    assert len(fh7.variations) == n_before, (
        "GOTCHA 3: no SECOND variation was cloned",
        fh7.variations,
    )
    assert fh7.rep(v7, "draft")["name"].startswith("[fr] "), (
        "the phantom now carries the translation",
        fh7.rep(v7, "draft"),
    )
    ok, _m, r7c = cstore.hubspot_translate(c7.id, actor="auto", transport=fh7.transport())
    assert ok and r7c["skipped"] == 1, ("and it settles on the next run", r7c)
    print(
        "  GOTCHA 3: a crash in the clone window leaves a 100%-source phantom that is "
        "NOT counted as translated, records no hash, and is COMPLETED (never duplicated) "
        "by the next run"
    )

    # ---- (8) digest 6.5: resolve to the group PRIMARY before cloning ----
    fh8 = FakeHubSpot()
    p8 = fh8.add("site-pages", 50, content=_page_content())
    c8 = _conn("primary", {"target_locales": "fr"})
    cstore.hubspot_translate(c8.id, actor="auto", transport=fh8.transport())
    v8 = _variant_of(fh8, p8, "fr")
    cc8 = _cc(fh8)
    assert cc8.resolve_primary(f"site-pages:{v8}") == f"site-pages:{p8}", (
        "a variation resolves UP to the group primary",
        v8,
    )
    # translating the VARIANT's id must still land in the SAME group (the fake 400s a
    # create-language-variation aimed at a translation, so a shortcut would fail loudly)
    c8b = _conn("primary2", {"target_locales": "de"})
    ok, msg, _r8 = cstore.hubspot_translate(
        c8b.id, article_ids=[f"site-pages:{v8}"], actor="auto", transport=fh8.transport()
    )
    assert ok, ("resolve-to-primary keeps create-language-variation legal", msg)
    assert _variant_of(fh8, p8, "de"), (
        "the de variation joined the PRIMARY's group",
        fh8.objects[str(p8)]["translations"],
    )
    assert not fh8.objects[str(v8)]["translations"], (
        "no variation-of-a-variation was created",
        fh8.objects[str(v8)]["translations"],
    )
    print(
        "  digest 6.5: an id that is itself a translation resolves to the group primary; "
        "no variation-of-a-variation is ever created"
    )

    # ---- (8b) MEDIUM-3: a NAMED variant id must not relay-translate ----
    # Discovery already skipped translatedFromId objects; the CALLER path (/api, CLI
    # --article-ids, retry, the runbook's MANDATORY-2 steps) did not. Naming the fr
    # variation produced "[de] [fr] Landing hero" and filed tracking under the VARIANT's
    # aid, leaving the primary's pair untracked forever.
    fh8b = FakeHubSpot()
    p8b = fh8b.add("site-pages", 55, content=_page_content())
    c8c = _conn("relay", {"target_locales": "fr"})
    cstore.hubspot_translate(c8c.id, actor="auto", transport=fh8b.transport())
    v8b = _variant_of(fh8b, p8b, "fr")
    assert fh8b.rep(v8b, "draft")["name"] == "[fr] Landing hero"
    c8d = _conn("relay2", {"target_locales": "de"})
    ok, msg8b, s8b = cstore.hubspot_translate(
        c8d.id, article_ids=[f"site-pages:{v8b}"], actor="auto", transport=fh8b.transport()
    )
    assert ok, msg8b
    v8de = _variant_of(fh8b, p8b, "de")
    assert v8de, ("the de variation joined the PRIMARY's group", fh8b.objects[str(p8b)])
    assert fh8b.rep(v8de, "draft")["name"] == "[de] Landing hero", (
        "MEDIUM-3: translated FROM THE PRIMARY, never relayed through the fr translation",
        fh8b.rep(v8de, "draft")["name"],
    )
    cfg8b = cstore.get(c8d.id).config or {}
    assert f"site-pages:{p8b}:de:draft" in (cfg8b.get("hubspot_variants") or {}), (
        "MEDIUM-3: tracking is filed under the PRIMARY's aid",
        cfg8b.get("hubspot_variants"),
    )
    assert not any(
        k.startswith(f"site-pages:{v8b}:") for k in (cfg8b.get("hubspot_variants") or {})
    ), ("MEDIUM-3: nothing is tracked under the variant's aid", cfg8b.get("hubspot_variants"))
    assert s8b["substitutions"] and s8b["substitutions"][0]["translated"] == f"site-pages:{p8b}", (
        "the substitution is REPORTED, never silent",
        s8b["substitutions"],
    )
    assert "Re-resolved 1 named id" in msg8b, msg8b
    print(
        "  MEDIUM-3: naming a language variation re-resolves to the group primary "
        "(reported), translates FROM the primary, and tracks under the primary's aid"
    )

    # ---- (8c) MEDIUM-5: a foreign-language object is skipped and REPORTED ----
    fh8c = FakeHubSpot()
    fh8c.add("site-pages", 56, content=_page_content("Startseite"), language="de")
    fh8c.add("site-pages", 57, content=_page_content("Home"), language="en")
    c8e = _conn("lang", {"target_locales": "fr"})
    ok, msg8c, s8c = cstore.hubspot_translate(c8e.id, actor="auto", transport=fh8c.transport())
    assert ok, msg8c
    assert s8c["language_skipped"] == 1 and s8c["created"] == 1, (
        "MEDIUM-5: the de-authored primary is skipped, the en one is translated",
        s8c,
    )
    assert s8c["language_skipped_objects"] == {"site-pages:56": "de"}, s8c
    assert not fh8c.objects["56"]["translations"], (
        "MEDIUM-5: nothing was created for the de-authored object",
        fh8c.objects["56"]["translations"],
    )
    assert "NOT authored in this connection's source language" in msg8c, msg8c
    # and primaryLanguage on create_variation comes from the OBJECT, not the config
    cc8c = _cc(fh8c, {"source_language": "en"})
    seen_pl: dict = {}
    real_call = cc8c._call

    def _spy(method, url, *, params=None, json_body=None, idempotent=True):
        if "create-language-variation" in url:
            seen_pl["primaryLanguage"] = (json_body or {}).get("primaryLanguage")
        return real_call(method, url, params=params, json_body=json_body, idempotent=idempotent)

    cc8c._call = _spy
    vid8c, err8c = cc8c.create_variation("site-pages:56", "fr")  # a de-authored primary
    assert vid8c and not err8c, err8c
    assert seen_pl.get("primaryLanguage") == "de", (
        "MEDIUM-5: primaryLanguage is taken from the OBJECT, not the config",
        seen_pl,
    )
    print(
        "  MEDIUM-5: an object not authored in the source language is skipped + "
        "reported (counter + message); primaryLanguage comes from the object"
    )

    # ---- (8d) MEDIUM-6 + round-2 MEDIUM-2/3: the self-heal, all branches EXERCISED ----
    # Round-2 finding: the previous fixture never entered either heal branch (the group
    # read at the top of write_target is already use_cache=False, so it agreed with the
    # object read). These fixtures make the group dict genuinely DISAGREE between the two
    # reads - i.e. a human deleting or replacing a variation mid-run.
    #
    # (a) healed is None -> the group entry VANISHES on the second read: recreate.
    fh8d = FakeHubSpot()
    p8d = fh8d.add("site-pages", 58, content=_page_content())
    fh8d.ghost_variants[str(p8d)] = {"fr": {"id": "888801", "slug": "fr/x"}}
    fh8d.drop_group_on_next_read.add((str(p8d), "fr"))
    c8f = _conn("heal-none", {"target_locales": "fr"})
    n_before = len(fh8d.variations)
    ok, msg8d, s8d = cstore.hubspot_translate(c8f.id, actor="auto", transport=fh8d.transport())
    assert ok and (s8d["created"] + s8d["updated"]) == 1, (
        "MEDIUM-6(a): a vanished variation is recreated, not failed forever",
        s8d,
        msg8d,
    )
    assert len(fh8d.variations) == n_before + 1, (
        "exactly ONE replacement was created",
        fh8d.variations,
    )
    assert fh8d.rep(_variant_of(fh8d, p8d, "fr"), "draft")["name"].startswith("[fr] ")

    # (b) healed != variant -> the group entry RE-POINTS at a real object on the second
    # read: adopt it, do NOT create anything.
    fh8e = FakeHubSpot()
    p8e = fh8e.add("site-pages", 59, content=_page_content())
    real8e = fh8e.add("site-pages", 5900, content=_page_content("Replacement"))
    fh8e.objects[str(real8e)]["translatedFromId"] = str(p8e)
    fh8e.objects[str(real8e)]["language"] = "fr"
    fh8e.ghost_variants[str(p8e)] = {"fr": {"id": "888802", "slug": "fr/x"}}
    fh8e.remap_group_on_next_read[(str(p8e), "fr")] = real8e
    c8h = _conn("heal-remap", {"target_locales": "fr"})
    n_before_e = len(fh8e.variations)
    ok, msg8e, s8e = cstore.hubspot_translate(c8h.id, actor="auto", transport=fh8e.transport())
    assert ok and (s8e["created"] + s8e["updated"]) == 1, (s8e, msg8e)
    assert len(fh8e.variations) == n_before_e, (
        "MEDIUM-6(b): a re-pointed group entry is ADOPTED, never re-created",
        fh8e.variations,
    )
    assert fh8e.rep(real8e, "draft")["name"].startswith("[fr] "), (
        "the adopted object received the translation",
        fh8e.rep(real8e, "draft")["name"],
    )

    # (c) ROUND-2 MEDIUM-2: a variation we JUST created that has no representation in the
    # configured state must NOT re-enter the heal and clone a SECOND variation.
    fh8f = FakeHubSpot()
    fh8f.add("site-pages", 60, content=_page_content())
    fh8f.clone_no_published = True  # a fresh clone has no published version
    c8i = _conn("no-dup", {"target_locales": "fr", "content_state": "published"})
    ok, msg8f, s8f = cstore.hubspot_translate(c8i.id, actor="auto", transport=fh8f.transport())
    assert len(fh8f.variations) == 1, (
        "MEDIUM-2: exactly ONE variation is created; the heal must not run on a "
        "variation we created this instant",
        fh8f.variations,
    )
    err8f = " ".join(str(f.get("error")) for f in (s8f.get("failures") or []))
    assert not ok and "representation does not exist" in err8f, (s8f, msg8f)
    # ROUND-2 MEDIUM-3: in PUBLISHED mode the remedy must be the published-mode one
    assert (
        "only as an unpublished draft" in err8f
        and "set this connection's content_state to 'draft'" in err8f
    ), ("MEDIUM-3: the remedy is not backwards in published mode", err8f)
    assert "pending draft" not in err8f, ("no draft-mode advice in published mode", err8f)

    # (d) and in DRAFT mode the remedy is the draft-mode one
    fh8g = FakeHubSpot()
    p8g = fh8g.add("site-pages", 61, content=_page_content())
    fh8g.ghost_variants[str(p8g)] = {"de": {"id": "888803", "slug": "de/x"}}
    c8j = _conn("remedy-draft", {"target_locales": "de"})
    ok, _msg8g, s8g = cstore.hubspot_translate(c8j.id, actor="auto", transport=fh8g.transport())
    err_text = " ".join(str(f.get("error")) for f in (s8g.get("failures") or []))
    assert (
        not ok
        and "representation does not exist" in err_text
        and "pending draft" in err_text
        and "content_state to 'published'" in err_text
    ), ("MEDIUM-6/3: the draft-mode failure names cause + the right remedy", err_text)
    print(
        "  MEDIUM-6 + MEDIUM-2/3: both self-heal branches EXERCISED (vanished -> "
        "recreate, re-pointed -> adopt); a just-created variation never heals into a "
        "duplicate; the remedy string matches the mode in both directions"
    )

    # ---- (8e) ergonomic: en-us portal vs source_language 'en' ----
    fh8h = FakeHubSpot()
    p8h = fh8h.add("site-pages", 62, content=_page_content(), language="en-us")
    fh8h.add("site-pages", 63, content=_page_content("Nope"), language="fr-ca")
    c8k = _conn("regional", {"target_locales": "de"})  # source_language defaults 'en'
    ok, msg8h, s8h = cstore.hubspot_translate(c8k.id, actor="auto", transport=fh8h.transport())
    assert ok and s8h["created"] == 1, (
        "an en-us portal must NOT produce a green all-skip run against source 'en'",
        s8h,
    )
    assert _variant_of(fh8h, p8h, "de"), "the en-us page was translated"
    assert s8h["language_regional"] == {"site-pages:62": "en-us"}, (
        "the region difference is REPORTED so it can be tightened",
        s8h,
    )
    assert "matched on the base language" in msg8h, msg8h
    assert s8h["language_skipped"] == 1 and s8h["language_skipped_objects"] == {
        "site-pages:63": "fr-ca"
    }, ("a different BASE language is still skipped", s8h)
    print(
        "  ergonomics: en-us matches source 'en' (base-language match, reported) while "
        "fr-ca is still skipped - no silent green all-skip first run"
    )

    # ---- (9) taxonomy B: a held-back field never writes source over a translation ----
    fh9 = FakeHubSpot()
    p9 = fh9.add("site-pages", 60, content=_page_content())
    c9 = _conn("held", {"target_locales": "fr"})
    ok, _m, _r = cstore.hubspot_translate(c9.id, actor="auto", transport=fh9.transport())
    assert ok
    v9 = _variant_of(fh9, p9, "fr")
    # a human refines the machine output, then the source changes -> run 2, title held back
    fh9.rep(v9, "draft")["name"] = "Titre humain"
    fh9.rep(v9, "draft")["layoutSections"]["main"]["widgets"]["w2"]["body"]["headline"] = (
        "Titre humain 2"
    )
    fh9.rep(p9, "draft")["layoutSections"]["main"]["widgets"]["w1"]["body"]["html"] = (
        "<p>Edited prose.</p>"
    )
    fh9.rep(p9, "draft")["name"] = "Landing hero v2"
    # segments: 0=name, 1=htmlTitle, 2=metaDescription, 3=html, 4=button_text, 5=headline
    orig, patched = _flip(T, lambda sg: sg.order_index in (0, 5))  # title + headline
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s9 = cstore.hubspot_translate(c9.id, actor="auto", transport=fh9.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    rep9 = fh9.rep(v9, "draft")
    assert rep9["name"] == "Titre humain", (
        "B: a held-back title must NOT be source-filled over a stored translation",
        rep9["name"],
    )
    hl = rep9["layoutSections"]["main"]["widgets"]["w2"]["body"]["headline"]
    assert hl == "Titre humain 2", ("B: a held-back module property keeps its translation", hl)
    assert (
        rep9["layoutSections"]["main"]["widgets"]["w1"]["body"]["html"]
        == "[fr] <p>Edited prose.</p>"
    ), "B: the un-held property still updates"
    inc9 = s9.get("incomplete") or []
    assert len(inc9) == 1 and "name" in inc9[0]["held_fields"], ("B: incomplete surfaced", inc9)
    assert "INCOMPLETE" in msg, msg
    print(
        "  B: held-back title AND held-back module property are OMITTED (stored "
        "translations preserved), the rest updates, incomplete marker surfaced"
    )

    # ---- (9b) taxonomy B for the HTML body model ----
    fh9b = FakeHubSpot()
    b9 = fh9b.add("blog-posts", 61, content=_post_content())
    c9b = _conn("held-html", {"content_types": "blog-posts", "target_locales": "fr"})
    ok, _m, _r = cstore.hubspot_translate(c9b.id, actor="auto", transport=fh9b.transport())
    assert ok
    v9b = _variant_of(fh9b, b9, "fr")
    fh9b.rep(v9b, "draft")["postBody"] = "<p>Premier humain.</p><p>Second humain.</p>"
    fh9b.rep(b9, "draft")["postBody"] = "<p>First para EDITED.</p><p>Second para.</p>"
    orig, patched = _flip(T, lambda sg: sg.order_index == 4)  # hold the 2nd paragraph
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, _s9b = cstore.hubspot_translate(c9b.id, actor="auto", transport=fh9b.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    body9b = fh9b.rep(v9b, "draft")["postBody"]
    assert "[fr] First para EDITED." in body9b, ("the translated paragraph lands", body9b)
    assert "Second humain." in body9b, (
        "B: the held-back paragraph keeps its STORED translation (target skeleton)",
        body9b,
    )
    assert "Second para." not in body9b, ("B: source text is never written back", body9b)
    # now force real structure drift AND a held-back unit -> the body is omitted entirely
    fh9b.rep(v9b, "draft")["postBody"] = "<p>Seulement un.</p>"  # 1 unit vs 2
    fh9b.rep(b9, "draft")["postBody"] = "<p>First para AGAIN.</p><p>Second para.</p>"
    orig, patched = _flip(T, lambda sg: sg.order_index == 4)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s9c = cstore.hubspot_translate(c9b.id, actor="auto", transport=fh9b.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert fh9b.rep(v9b, "draft")["postBody"] == "<p>Seulement un.</p>", (
        "B: drift + a held-back unit -> the body is NOT written at all (nothing "
        "overwritten with source)",
        fh9b.rep(v9b, "draft")["postBody"],
    )
    assert any(
        "postBody" in p["held_fields"] or any("postBody" in f for f in p["held_fields"])
        for p in (s9c.get("incomplete") or [])
    ), ("the omitted body is surfaced as incomplete", s9c.get("incomplete"))
    print(
        "  B (html body): a held-back paragraph keeps its stored translation via the "
        "TARGET skeleton; on real structure drift the body is omitted, never source-filled"
    )

    # ---- (9c) LOW-7: a wholesale skeleton replacement is SURFACED, not silent ----
    # Drift + EVERYTHING translated is a safe full replace for taxonomy B (no source text
    # survives) — but it discards target-only human restructuring, so say so.
    fh9c = FakeHubSpot()
    b9c = fh9c.add("blog-posts", 62, content=_post_content())
    c9c = _conn("low7", {"content_types": "blog-posts", "target_locales": "fr"})
    cstore.hubspot_translate(c9c.id, actor="auto", transport=fh9c.transport())
    v9c = _variant_of(fh9c, b9c, "fr")
    # a human restructures the TARGET (added a third paragraph), then the source changes
    fh9c.rep(v9c, "draft")["postBody"] = "<p>Un.</p><p>Deux.</p><p>Ajout humain.</p>"
    fh9c.rep(b9c, "draft")["postBody"] = "<p>First NEW.</p><p>Second NEW.</p>"
    ok, msg9c, s9d = cstore.hubspot_translate(c9c.id, actor="auto", transport=fh9c.transport())
    assert ok, msg9c
    assert "Ajout humain" not in fh9c.rep(v9c, "draft")["postBody"], (
        "the wholesale replace really did discard the target-only paragraph"
    )
    # ROUND-2 LOW-7: its OWN channel. `held_fields` means "not written" to every machine
    # consumer (the retry path included); a field that WAS written must never appear there.
    assert any("postBody" in p["replaced_fields"] for p in (s9d.get("replaced") or [])), (
        "LOW-7: the replacement is surfaced in `replaced`",
        s9d.get("replaced"),
    )
    assert not any("postBody" in p["held_fields"] for p in (s9d.get("incomplete") or [])), (
        "LOW-7: a WRITTEN field must not be reported as held",
        s9d.get("incomplete"),
    )
    reasons9 = " ".join(r for p in (s9d.get("replaced") or []) for r in p["reasons"].values())
    assert "REPLACED" in reasons9 and "superseded" in reasons9, (
        "LOW-7: the reason says the target structure was replaced",
        reasons9,
    )
    assert "Replaced the body structure of 1 pair(s)" in msg9c or "Replaced the body structure" in (
        msg9c or ""
    ), msg9c
    print(
        "  LOW-7: a wholesale source-skeleton replace of a drifted target body is "
        "surfaced in its OWN `replaced` channel ('target-only edits superseded'), "
        "keeping held_fields single-meaning"
    )

    # ---- (9d) LOW-8: a held PATH renders as a path, not as a `fields` suggestion ----
    fh9e = FakeHubSpot()
    fh9e.add("site-pages", 63, content=_page_content())
    c9e = _conn("low8", {"target_locales": "fr"})
    # #640: a NEW variation is never created while a unit is held (it would be a clone of the
    # English source), so create it healthy first, then hold `headline` on an operator retry
    # (which bypasses the content-hash gate) to get the partial UPDATE this check is about.
    cstore.hubspot_translate(c9e.id, actor="auto", transport=fh9e.transport())
    orig, patched = _flip(T, lambda sg: sg.order_index == 5)  # hold `headline`
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg9e, s9e = cstore.retry_failed(
            c9e.id,
            [{"article_id": "site-pages:63", "locale": "fr"}],
            actor="auto",
            transport=fh9e.transport(),
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    held9 = (s9e.get("incomplete") or [{}])[0].get("held_fields") or []
    assert any(">" in h and "headline" in h for h in held9), (
        "LOW-8: a path token renders as a readable path",
        held9,
    )
    assert not any(h.startswith("body:[") for h in held9), (
        "LOW-8: no raw JSON token leaks into the operator surface",
        held9,
    )
    reasons9e = (s9e.get("incomplete") or [{}])[0].get("reasons") or {}
    assert all("add it to `fields`" not in r for r in reasons9e.values()), (
        "LOW-8: a held PATH is never phrased as something to add to `fields` (that "
        "advice belongs to the NAME-keyed selection report, not to a path)",
        reasons9e,
    )
    assert "main > widgets > w2 > body > headline" in msg9e, (
        "LOW-8: the readable path reaches the operator message",
        msg9e,
    )
    print(
        "  LOW-8: a held-back module PATH renders as 'main > widgets > ... > headline', "
        "never a raw JSON token or a `fields` suggestion"
    )

    # ---- (10) taxonomy F: all-held-back -> no write, no hash, healthy re-run retries ----
    fh10 = FakeHubSpot()
    p10 = fh10.add("site-pages", 70, content=_page_content())
    c10 = _conn("allheld", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: True)
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg, s10 = cstore.hubspot_translate(c10.id, actor="auto", transport=fh10.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg
    assert not fh10.patches, ("F: all held -> NO patch", fh10.patches)
    assert (
        s10["held_back"] == 1
        and s10["created"] == 0
        and s10["updated"] == 0
        and not s10["failures"]
    ), s10
    assert not (cstore.get(c10.id).config or {}).get("hubspot_hashes"), (
        "F: an all-held run records NO suppression hash",
        (cstore.get(c10.id).config or {}).get("hubspot_hashes"),
    )
    ok2, _m2, s10b = cstore.hubspot_translate(c10.id, actor="auto", transport=fh10.transport())
    assert ok2 and (s10b["created"] + s10b["updated"]) == 1 and s10b["skipped"] == 0, (
        "F: the held-back pair is re-attempted next run (hash not poisoned)",
        s10b,
    )
    assert _variant_of(fh10, p10, "fr"), s10b
    print(
        "  F: all-held-back -> nothing written, counted held_back, NO hash recorded, "
        "healthy re-run re-attempts"
    )

    # ---- (11) taxonomy E: shared cap round-robins across content types ----
    fh11 = FakeHubSpot()
    for i in range(40):
        fh11.add("site-pages", 100 + i, content=_page_content(f"p{i}"))
        fh11.add("landing-pages", 200 + i, content=_page_content(f"l{i}"))
        fh11.add("blog-posts", 300 + i, content=_post_content(f"b{i}"))
    cc11 = _cc(
        fh11, {"content_types": "site-pages, landing-pages, blog-posts", "max_entries": "30"}
    )
    got = cc11.discover_articles()
    counts: dict = {}
    for r in got:
        counts[r["_ctype"]] = counts.get(r["_ctype"], 0) + 1
    assert len(got) == 30, ("shared cap fills to 30", len(got))
    assert (
        set(counts) == {"site-pages", "landing-pages", "blog-posts"} and min(counts.values()) == 10
    ), ("E: round-robin distributes the shared cap; no type starved", counts)
    print(f"  E: multi-content-type cap round-robins fairly (no starvation): {counts}")

    # ---- (12) taxonomy G: the fake really does enforce HubSpot's validation ----
    fh12 = FakeHubSpot()
    p12 = fh12.add("site-pages", 400, content=_page_content())
    b12 = fh12.add("blog-posts", 401, content=_post_content())
    cc12 = _cc(fh12, {"content_types": "site-pages, blog-posts"})
    okp, errp = cc12.patch_object(f"site-pages:{p12}", {"nonsense": 1})
    assert not okp and "unknown" in errp, ("G: an unknown patch key is a 400", errp)
    okp, errp = cc12.patch_object(f"blog-posts:{b12}", {"layoutSections": {}})
    assert not okp and "not a property" in errp, ("G: wrong body field for the type", errp)
    okp, errp = cc12.patch_object("site-pages:99999", {"name": "x"})
    assert not okp and "404" in errp, ("G: patching a missing object 404s", errp)
    vid12, err12 = cc12.create_variation(f"site-pages:{p12}", "fr")
    assert vid12 and not err12, err12
    _v2, err12b = cc12.create_variation(f"site-pages:{p12}", "fr")
    assert not _v2 and "already exists" in err12b, (
        "G: a duplicate variation is refused (so reuse-first is load-bearing)",
        err12b,
    )
    _v3, err12c = cc12.create_variation(vid12, "de")
    assert not _v3 and "from a translation" in err12c, (
        "G: a variation-of-a-variation is refused",
        err12c,
    )
    # HubSpot's HTML-error-page edge case (digest 6.13) becomes a clear ConnectorError
    html_tp = httpx.MockTransport(
        lambda r: httpx.Response(
            502,
            text="<html><body><h1>HTTP ERROR 502</h1></body></html>",
            headers={"content-type": "text/html"},
        )
    )
    ccH = HubSpotConnector(
        {"content_types": "site-pages"}, {"access_token": "t"}, transport=html_tp
    )
    okH, msgH = ccH.test()
    assert not okH and "non-JSON" in msgH and "HTTP ERROR 502" in msgH, (
        "digest 6.13: an HTML error page is reported clearly, never a crash",
        msgH,
    )
    print(
        "  G: the fake enforces unknown-key/wrong-field/404/duplicate-variation/"
        "variation-of-a-variation; an HTML error page is reported clearly"
    )

    # ---- (13) GOTCHA 3 ordering: tracking is persisted BEFORE the hash merge ----
    fh13 = FakeHubSpot()
    p13 = fh13.add("site-pages", 500, content=_page_content())
    c13 = _conn("tracking", {"target_locales": "fr"})
    ok, _m, _r = cstore.hubspot_translate(c13.id, actor="auto", transport=fh13.transport())
    cfg13 = cstore.get(c13.id).config or {}
    key13 = f"site-pages:{p13}:fr:draft"  # state-keyed tracking (HIGH-2)
    assert cfg13.get("hubspot_variants", {}).get(key13), ("the variant id was tracked", cfg13)
    toks = cfg13.get("hubspot_written", {}).get(key13) or []
    assert _prop_ok(toks) and _path_token(_SEL_PATHS[0]) in toks, (
        "the written paths were tracked",
        toks,
    )
    # the content HASH stays state-less on purpose (source identity, not target state)
    assert cfg13.get("hubspot_hashes", {}).get(f"site-pages:{p13}:fr"), (
        "and the hash was recorded, state-less",
        cfg13.get("hubspot_hashes"),
    )
    # simulate a crash that lost the tracking but kept the hash: the probe must NOT
    # report 'covered' (no any-key fallback), so ONE re-write re-establishes tracking.
    cstore.patch_config(c13.id, remove=["hubspot_written"])
    ok, _m, r13 = cstore.hubspot_translate(c13.id, actor="auto", transport=fh13.transport())
    assert ok and r13["updated"] == 1 and r13["skipped"] == 0, (
        "untracked => NOT covered (no false-covered hole)",
        r13,
    )
    assert (cstore.get(c13.id).config or {}).get("hubspot_written", {}).get(key13), (
        "tracking re-established"
    )
    ok, _m, r13b = cstore.hubspot_translate(c13.id, actor="auto", transport=fh13.transport())
    assert ok and r13b["skipped"] == 1, ("and settles again", r13b)
    print(
        "  GOTCHA 3 (tracking): variant id + written paths persist per successful pair "
        "BEFORE the hash merge; untracked never counts as covered"
    )

    # ---- (14) taxonomy H: WriteBudget ceiling ----
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fh14 = FakeHubSpot()
        fh14.add("site-pages", 600, content=_page_content())
        c14 = _conn("budget")  # fr + de
        ok, msg, s14 = cstore.hubspot_translate(c14.id, actor="auto", transport=fh14.transport())
        assert ok and s14["ceiling_hit"] and (s14["created"] + s14["updated"]) == 1, s14
        assert len(fh14.variations) == 1, ("only ONE external write happened", fh14.variations)
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    print("  H: WriteBudget MAX_WRITES_PER_RUN=1 stops after 1 external variation write")

    # ---- (15) taxonomy H: the mock-egress guard fires ----
    from app.services.connectors import egress_guard

    fh15 = FakeHubSpot()
    fh15.add("site-pages", 610, content=_page_content())
    c15 = _conn("egress")
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, msg, _s15 = cstore.hubspot_translate(c15.id, actor="a", transport=fh15.transport())
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not fh15.variations and not fh15.patches, (
        "mock MT + real egress -> blocked, nothing written",
        msg,
    )
    print("  H: egress guard blocks mock MT + real egress, no external write")

    # ---- (16) suppression: an edited source re-translates; source language dropped ----
    fh16 = FakeHubSpot()
    p16 = fh16.add("site-pages", 620, content=_page_content())
    c16 = _conn("edit", {"target_locales": "fr, de, en"})
    ok, msg, s16 = cstore.hubspot_translate(c16.id, actor="auto", transport=fh16.transport())
    assert ok and s16["locales"] == ["fr", "de"] and s16["dropped_source"] == ["en"], s16
    assert "equal to the source language" in msg, msg
    fh16.rep(p16, "draft")["name"] = "Landing hero EDITED"
    ok, _m, s16b = cstore.hubspot_translate(c16.id, actor="auto", transport=fh16.transport())
    assert ok and s16b["updated"] == 2 and s16b["skipped"] == 0, (
        "an edited source re-translates every locale",
        s16b,
    )
    # an invalid language tag is rejected loudly, never sent to the wire
    c16c = _conn("badlang", {"target_locales": "fr, ../etc"})
    ok, msg, s16c = cstore.hubspot_translate(c16c.id, actor="auto", transport=fh16.transport())
    assert s16c["rejected"] == ["../etc"] and s16c["locales"] == ["fr"], s16c
    print(
        "  suppression: edited source -> re-translate; source language dropped; an "
        "invalid language tag is rejected before the wire"
    )

    # ---- (17) pagination + empty object skipped ----
    fh17 = FakeHubSpot()
    for i in range(5):
        fh17.add("site-pages", 700 + i, content=_page_content(f"P{i}"))
    assert len(_cc(fh17, {"max_entries": "3"}).discover_articles()) == 3
    assert len(_cc(fh17, {"max_entries": "50"}).discover_articles()) == 5
    fh17b = FakeHubSpot()
    fh17b.add(
        "site-pages",
        800,
        content={"name": "  ", "htmlTitle": "", "metaDescription": "", "layoutSections": {}},
    )
    c17 = _conn("empty")
    ok, msg, s17 = cstore.hubspot_translate(
        c17.id, article_ids=["site-pages:800"], actor="auto", transport=fh17b.transport()
    )
    assert ok and s17["empty_skipped"] >= 1, s17
    print("  pagination: cursor walk collects to the cap; an empty object is skipped")

    # ---- (18) parity surfaces ----
    from app.services.connectors import REGISTRY

    assert "hubspot" in REGISTRY
    c = TestClient(app)
    assert "HubSpot" in c.get("/connectors").text
    typ = c.get("/connectors/hubspot").text
    assert "Content types" in typ and "Content state" in typ, "add form renders"
    from app.cli.__main__ import build_parser

    assert "connector hubspot-translate" in _leaf_paths(build_parser())
    from starlette.routing import Route

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/hubspot/translate" in api_paths
    from app.services.connectors import harvest as _h

    assert "hubspot" in _h._HARVESTERS
    assert cstore.hubspot_translate is not None
    print(
        "  parity: REGISTRY + /connectors tile + type form + /api route + CLI leaf + "
        "harvest mapper + store delegator"
    )

    # ---- (19) org isolation on the /api route ----
    from tests._api_fixture import user_api_token

    connapi = _conn("api")
    hdr = {"X-Loc-Token": user_api_token()}
    r_ghost = c.post("/api/connectors/99999999/hubspot/translate", json={}, headers=hdr)
    assert r_ghost.status_code == 404, ("foreign/missing cid -> 404", r_ghost.status_code)
    r_own = c.post(
        f"/api/connectors/{connapi.id}/hubspot/translate", json={"article_ids": []}, headers=hdr
    )
    assert r_own.status_code in (200, 400), ("own connection reachable", r_own.status_code)
    print("  org isolation: foreign/missing cid -> 404; own connection reachable")

    # ---- (20) CLI hubspot-translate smoke ----
    from app.cli.cmd_connector import _hubspot_translate

    fhcli = FakeHubSpot()
    fhcli.add("site-pages", 900, content=_page_content())
    conncli = _conn("cli", {"target_locales": "fr"})
    real_ct = cstore.hubspot_translate
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
            transport=fhcli.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "hubspot_translate", _capturing)

    class _Args:
        connection_id = conncli.id
        article_ids = "site-pages:900"
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
        rc = _hubspot_translate(_Args(), emit)
        assert rc == 0, emit.res
        assert seen.get("article_ids") == ["site-pages:900"], seen
        assert emit.res[1]["ok"], emit.res
        assert emit.res[0].isascii(), ("ASCII-safe CLI output", emit.res[0])
    finally:
        set_pieces("app.services.connectors.store", "hubspot_translate", real_ct)
    print("  CLI: hubspot-translate parses --article-ids, runs the flow, ASCII output")

    # ---- (21) retry_failed re-attempts a failed pair ----
    fh21 = FakeHubSpot()
    p21 = fh21.add("site-pages", 950, content=_page_content())
    c21 = _conn("retry", {"target_locales": "fr"})
    ok, _m, _r = cstore.hubspot_translate(c21.id, actor="auto", transport=fh21.transport())
    v21 = _variant_of(fh21, p21, "fr")
    fh21.patch_500.add(str(v21))
    fh21.rep(p21, "draft")["name"] = "Landing hero v3"  # force a re-write
    _rs = cbase._sleep
    cbase._sleep = lambda s: None
    try:
        ok, _m, s21 = cstore.hubspot_translate(c21.id, actor="auto", transport=fh21.transport())
    finally:
        cbase._sleep = _rs
    assert not ok and s21["failures"], s21
    fh21.patch_500.clear()
    okr, _mr, s21r = cstore.retry_failed(
        c21.id, s21["failures"], actor="auto", transport=fh21.transport()
    )
    assert okr and fh21.rep(v21, "draft")["name"] == "[fr] Landing hero v3", (
        "retry_failed re-attempts the failed pair",
        s21r,
        fh21.rep(v21, "draft")["name"],
    )
    print("  retry: retry_failed re-writes a previously-failed (object, language) pair")

    # ---- (22) #659: the sync project honours the connector's own source_language ----
    from sqlmodel import Session

    from app.db import engine
    from app.services.connectors import store_part2 as _sp2

    c22 = _conn("srclang", {"source_language": "fr", "target_locales": "es"})
    with Session(engine) as s22:
        conn22 = s22.get(type(c22), c22.id)
        proj22 = _sp2._ensure_project(s22, conn22)
        assert proj22.source_locale == "fr", (
            "#659: project source follows source_language",
            proj22.source_locale,
        )
    c22b = _conn("srclang-default", {"target_locales": "es"})
    with Session(engine) as s22:
        proj22b = _sp2._ensure_project(s22, s22.get(type(c22b), c22b.id))
        assert proj22b.source_locale == "en", ("#659: en config gives en", proj22b.source_locale)
    c22c = _conn(
        "srclang-explicit",
        {"source_language": "fr", "source_locale": "fr-CA", "target_locales": "es"},
    )
    with Session(engine) as s22:
        proj22c = _sp2._ensure_project(s22, s22.get(type(c22c), c22c.id))
        assert proj22c.source_locale == "fr-CA", (
            "#659: explicit source_locale wins",
            proj22c.source_locale,
        )
    print("  #659: the sync project is created in the connector's source_language")

    # ---- (23) #660: blog-post create-language-variation sends no primaryLanguage ----
    fh23 = FakeHubSpot()
    fh23.add("blog-posts", 91, content=_post_content("Post"), language="en")
    cc23 = _cc(fh23, {"content_types": "blog-posts"})
    sent23: dict = {}
    real23 = cc23._call

    def _spy23(method, url, *, params=None, json_body=None, idempotent=True):
        if "create-language-variation" in url:
            sent23.update(json_body or {})
        return real23(method, url, params=params, json_body=json_body, idempotent=idempotent)

    cc23._call = _spy23
    v23, e23 = cc23.create_variation("blog-posts:91", "fr")
    assert v23 and not e23, e23
    assert "primaryLanguage" not in sent23 and sent23.get("language") == "fr", (
        "#660: HubSpot's blog-post schema is id, language, usePublished only",
        sent23,
    )
    print("  #660: blog-post create-language-variation omits primaryLanguage")

    # ---- (24) #658: setup text names Service Keys, not Private Apps ----
    from app.services.connectors.hubspot import SPEC as _hs_spec

    blob24 = json.dumps(
        [f.label + f.help for f in _hs_spec.secret_fields] + list(_hs_spec.setup_steps)
    ).lower()
    assert "settings -> integrations -> service keys" in blob24, ("#658", blob24)
    assert "integrations -> private apps" not in blob24, ("#658", blob24)
    assert _hs_spec.secret_fields[0].label == "Service Key", (
        "#658",
        _hs_spec.secret_fields[0].label,
    )
    print("  #658: setup copy names Service Keys")

    # ---- (25) #640: a held unit must not create a (English-clone) variation ----
    fh25 = FakeHubSpot()
    p25 = fh25.add("site-pages", 960, content=_page_content())
    c25 = _conn("held-create", {"target_locales": "fr"})
    orig, patched = _flip(T, lambda sg: sg.order_index == 5)  # hold `headline`
    set_pieces("app.services.translate", "pretranslate_document", patched)
    try:
        ok, msg25, s25 = cstore.hubspot_translate(c25.id, actor="auto", transport=fh25.transport())
    finally:
        set_pieces("app.services.translate", "pretranslate_document", orig)
    assert ok, msg25
    assert _variant_of(fh25, p25, "fr") is None and s25["created"] == 0, (
        "#640: no variation is created (it would be a clone of the English source)",
        s25,
    )
    assert s25["held_back"] == 1 and not s25["failures"], s25
    ok, msg25b, s25b = cstore.hubspot_translate(c25.id, actor="auto", transport=fh25.transport())
    assert ok and s25b["created"] == 1 and _variant_of(fh25, p25, "fr"), (msg25b, s25b)
    print("  #640: held unit on a NEW variation -> not created, held_back=1, created once approved")

    print("verify_hubspot OK")


def _prop_ok(tokens) -> bool:
    return any(t.startswith("prop:name") for t in tokens)


def _leaf_paths(parser, prefix="") -> set:
    import argparse

    out: set = set()
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        return {prefix.strip()} if prefix.strip() else set()
    for a in subs:
        for name, sub in a.choices.items():
            out |= _leaf_paths(sub, f"{prefix} {name}")
    return out


if __name__ == "__main__":
    main()
