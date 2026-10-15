"""WordPress (Polylang) connector - the THIRD T3 (document-per-locale) build - fully
offline via httpx.MockTransport (a fake WordPress + Polylang site).

WordPress is T3: no field-level localization, so a "translation" is a SEPARATE post the
connector creates in the target language and tracks in ConnectorIdMap via
strategy.DocPerLocaleStrategy (reserve-then-fill + the 7-case reconciliation state table -
the SAME machinery Notion/Help Scout built). Write-back rides the shared flow engine with
ZERO engine edits. Raw block content routes through the existing `html` filter.

The fake enforces WP/Polylang semantics so the tests are NON-VACUOUS (the "does the fake
model the real system" lesson):
  * FREE mode: post responses carry NO `translations`/`lang` fields, and `?lang=`/
    `?translations[xx]=` write params are ignored; discovery is confined by category/tag
    (WordPress free exposes no per-post language signal - B1).
  * PRO mode: post responses carry `translations`+`lang`; the write params tag + link.
  * status-scoped, category/tag-scoped, and search queries are all honored; `X-WP-TotalPages`
    is set; `?context=edit` returns `raw` (a rendered-only site returns only `rendered`).

Covers the four digest gotchas AND the fix-round findings: B1 (free-tier source scope /
fail-closed discovery), H1 (orphan reclamation - crash + delete-and-re-add), H2 (external
deletion / blank detection), H3 (shortcode masking, mutation-proven with a REWRITING engine),
M1 (update omits status), M2 (superseded-human-edit honesty), M3 (fresh Polylang mode both
stale directions), M4 (explicit own-target refused), M5 (excerpt translated + img alt loud
skip), L1 (explicit discovery status), L2 (all-raw-unavailable attention state), plus
taxonomy A/E, reconciliation rules, NEEDS_REVIEW exclusion, no-watermark, GDPR, and the
web/CLI parity surfaces.

Live QA is DEFERRED (Polylang Pro cost) - ships flagged "live QA pending"
(RUNBOOK-wordpress-live-qa.md). ASCII-only prints; final line is 'verify_wordpress OK'.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_wordpress_")

import json
import math

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.services.translate as translate_mod
from app.db import engine, init_db
from app.main import app
from app.models import ConnectorIdMap, Segment, SegStatus
from app.services import connectors as _reg
from app.services.connectors import idmap
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.wordpress import WordPressConnector

SHORTCODE = '[contact-form-7 id="7" title="Contact form"]'
GUTENBERG = (
    "<!-- wp:paragraph --><p>Hello world</p><!-- /wp:paragraph -->\n"
    '<!-- wp:heading {"level":2} --><h2>Second step</h2><!-- /wp:heading -->\n' + SHORTCODE
)


def _slugify(title):
    import re

    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-") or "post"


# --------------------------------------------------------------------------- #
# Fake WordPress + Polylang site.
# --------------------------------------------------------------------------- #
class FakeWordPress:
    CAT = 5  # the default source category id

    def __init__(self, mode="pro", polylang_absent=False, rendered_only=False):
        self.mode = mode
        self.polylang_absent = polylang_absent
        self.rendered_only = rendered_only
        self.posts: dict = {}
        self._seq = 1000
        self.creates: list = []
        self.puts: list = []
        self.list_calls: list = []  # L1: params of every GET-list call
        self.languages = [
            {"slug": "en", "locale": "en_US"},
            {"slug": "fr", "locale": "fr_FR"},
            {"slug": "de", "locale": "de_DE"},
        ]

    def transport(self):
        return httpx.MockTransport(self._handle)

    def add_source(
        self,
        title,
        content,
        *,
        ptype="posts",
        lang="en",
        status="publish",
        translations=None,
        pid=None,
        excerpt="",
        categories=None,
        tags=None,
        slug=None,
    ):
        self._seq += 1
        pid = str(pid if pid is not None else self._seq)
        self.posts[pid] = {
            "id": pid,
            "type": ptype,
            "title": title,
            "content": content,
            "excerpt": excerpt,
            "status": status,
            "lang": lang,
            "slug": slug if slug is not None else _slugify(title),
            "translations": dict(translations or {}),
            "categories": list(categories if categories is not None else [self.CAT]),
            "tags": list(tags or []),
            "modified_gmt": "2026-02-01T00:00:00",
        }
        return f"{ptype}:{pid}"

    def get(self, ref):
        return self.posts.get(ref.split(":")[-1])

    def content_of(self, ref):
        return (self.get(ref) or {}).get("content", "")

    def _post_json(self, p, edit):
        out = {
            "id": int(p["id"]),
            "status": p["status"],
            "type": p["type"],
            "slug": p.get("slug", ""),
            "modified_gmt": p["modified_gmt"],
        }
        if edit and not self.rendered_only:
            out["title"] = {"raw": p["title"], "rendered": p["title"]}
            out["content"] = {"raw": p["content"], "rendered": "<p>RENDERED-FLATTENED</p>"}
            out["excerpt"] = {"raw": p["excerpt"], "rendered": p["excerpt"]}
        else:
            out["title"] = {"rendered": p["title"]}
            out["content"] = {"rendered": "<p>RENDERED-FLATTENED</p>"}
            out["excerpt"] = {"rendered": p["excerpt"]}
        if self.mode == "pro":
            out["lang"] = p["lang"]
            out["translations"] = dict(p["translations"])
        return out

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        params = req.url.params
        seg = [s for s in path.split("/") if s]

        if "pll" in seg and seg[-1] == "languages":
            if self.polylang_absent:
                return httpx.Response(404, json={"code": "rest_no_route"})
            return httpx.Response(200, json=list(self.languages))

        if "v2" in seg:
            rest = seg[seg.index("v2") + 1 :]
            if not rest:
                return httpx.Response(404, json={"code": "rest_no_route"})
            ptype = rest[0]
            edit = params.get("context") == "edit"

            if len(rest) == 2:  # single: /wp/v2/<type>/<id>
                pid = rest[1]
                p = self.posts.get(pid)
                if method == "GET":
                    if p is None or p["type"] != ptype:
                        return httpx.Response(404, json={"code": "rest_post_invalid_id"})
                    return httpx.Response(200, json=self._post_json(p, edit))
                if method == "PUT":
                    if p is None:
                        return httpx.Response(404, json={"code": "rest_post_invalid_id"})
                    body = json.loads(req.content)
                    p["title"] = body.get("title", p["title"])
                    p["content"] = body.get("content", p["content"])
                    if "excerpt" in body:
                        p["excerpt"] = body["excerpt"]
                    if body.get("status"):  # M1: absent -> status UNCHANGED (partial)
                        p["status"] = body["status"]
                    self.puts.append((ptype, pid, p["status"]))
                    return httpx.Response(200, json=self._post_json(p, edit))
                return httpx.Response(405, json={"code": "rest_no_route"})

            if len(rest) == 1:  # collection: /wp/v2/<type>
                if method == "GET":
                    status = params.get("status")
                    lang = params.get("lang")
                    cats = params.get("categories")
                    tags = params.get("tags")
                    slug = params.get("slug")
                    search = (params.get("search") or "").lower()
                    self.list_calls.append(
                        {
                            "type": ptype,
                            "status": status,
                            "lang": lang,
                            "categories": cats,
                            "tags": tags,
                            "search": search,
                            "slug": slug,
                        }
                    )
                    items = [p for p in self.posts.values() if p["type"] == ptype]
                    if status and status != "any":
                        items = [p for p in items if p["status"] == status]
                    if lang and self.mode == "pro":
                        items = [p for p in items if p["lang"] == lang]
                    if slug:
                        items = [p for p in items if p.get("slug") == slug]
                    if cats:
                        want = {c.strip() for c in cats.split(",")}
                        items = [p for p in items if {str(x) for x in p["categories"]} & want]
                    if tags:
                        want = {c.strip() for c in tags.split(",")}
                        items = [p for p in items if {str(x) for x in p["tags"]} & want]
                    if search:
                        items = [
                            p
                            for p in items
                            if search in p["title"].lower() or search in p["content"].lower()
                        ]
                    items.sort(key=lambda p: int(p["id"]))
                    try:
                        page = int(params.get("page", 1))
                        per = int(params.get("per_page", 10))
                    except (TypeError, ValueError):
                        page, per = 1, 10
                    total = len(items)
                    total_pages = max(1, math.ceil(total / per)) if total else 0
                    if page > total_pages and total_pages:
                        return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})
                    chunk = items[(page - 1) * per : (page - 1) * per + per]
                    return httpx.Response(
                        200,
                        headers={"X-WP-TotalPages": str(total_pages), "X-WP-Total": str(total)},
                        json=[self._post_json(p, edit) for p in chunk],
                    )
                if method == "POST":
                    body = json.loads(req.content)
                    self._seq += 1
                    nid = str(self._seq)
                    lang = params.get("lang") or ""
                    links = {}
                    for k, v in params.multi_items():
                        if k.startswith("translations[") and k.endswith("]"):
                            links[k[len("translations[") : -1]] = str(v)
                    post = {
                        "id": nid,
                        "type": ptype,
                        "title": body.get("title") or "",
                        "content": body.get("content") or "",
                        "excerpt": body.get("excerpt") or "",
                        "slug": body.get("slug") or _slugify(body.get("title") or ""),
                        "status": body.get("status") or "draft",
                        "lang": lang,
                        "translations": {},
                        "categories": [],
                        "tags": [],
                        "modified_gmt": "2026-03-01T00:00:00",
                    }
                    if self.mode == "pro":
                        group = dict(links)
                        if lang:
                            group[lang] = nid
                        for loc, sid in links.items():
                            sib = self.posts.get(str(sid))
                            if sib is not None:
                                if lang:
                                    sib["translations"][lang] = nid
                                sib["translations"][loc] = str(sid)
                                group.update(sib["translations"])
                        post["translations"] = group
                    self.posts[nid] = post
                    self.creates.append((ptype, nid, lang, dict(links)))
                    return httpx.Response(201, json=self._post_json(post, edit))
        return httpx.Response(404, json={"code": "rest_no_route", "path": path})


def _conn(name, fake, *, targets="fr, de", extra=None, scope=True):
    cfg = {
        "base_url": "https://blog.acme.com",
        "post_types": "posts",
        "source_locale": "en",
        "target_locales": targets,
    }
    if scope:
        cfg["source_category"] = str(FakeWordPress.CAT)  # B1: free-tier discovery scope
    cfg.update(extra or {})
    return cstore.create(
        "wordpress",
        name,
        cfg,
        {"username": "editor", "application_password": "pw pw pw"},
        actor="admin",
    )


def _rewrite_ptd(real):
    """A REWRITING pretranslate: mutate EVERY segment it sees (uppercase + markers) so a
    fidelity assertion is mutation-proof - anything that survives unchanged provably never
    reached the engine (taxonomy G)."""

    def rw(session, doc_id, locale, spec=None, force_pii=False):
        stats = real(session, doc_id, locale, spec, force_pii=force_pii)
        for sg in session.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).all():
            sg.target_text = "X" + (sg.source_text or "").upper() + "X"
            session.add(sg)
        session.commit()
        return stats

    return rw


def main() -> None:
    init_db()
    import app.services.connectors.base as _base

    _base._sleep = lambda *_a, **_k: None
    mock_ptd = translate_mod.pretranslate_document

    def real_ptd(session, doc_id, locale, *args, **kwargs):
        stats = mock_ptd(session, doc_id, locale, *args, **kwargs)
        for seg in session.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            # The mock's [fr] prefix itself matches WordPress shortcode syntax.
            if seg.target_text:
                seg.target_text = seg.target_text.replace(f"[{locale}]", f"({locale})", 1)
                session.add(seg)
        session.commit()
        return stats

    set_pieces("app.services.translate", "pretranslate_document", real_ptd)

    # ---- (0) taxonomy A: ref scope/id validation + config validation ----
    cv = WordPressConnector(
        {"base_url": "https://blog.acme.com", "post_types": "posts, pages"},
        {"username": "u", "application_password": "p"},
    )
    assert cv._split_ref("posts:12") == ("posts", "12")
    assert cv._split_ref("post:5") == ("posts", "5")
    for bad in (
        "posts:../wp/v2/users",
        "posts:1/2",
        "posts:",
        "widgets:1",
        "posts:abc",
        "posts:-1",
        "posts:12?x=1",
        "12",
    ):
        try:
            cv._split_ref(bad)
            assert False, ("must reject", bad)
        except ConnectorError:
            pass
    assert WordPressConnector({"base_url": "https://x.io", "post_types": "posts"}, {})._split_ref(
        "12"
    ) == ("posts", "12")
    for bad_cfg in (
        {"post_types": "widgets"},
        {"source_status": "bogus"},
        {"source_category": "x"},
    ):
        try:
            wc = WordPressConnector({"base_url": "https://x.io", **bad_cfg}, {})
            wc._post_types()
            wc._source_status()
            wc._scope_params()
            assert False, ("bad config rejected", bad_cfg)
        except ConnectorError:
            pass
    print(
        "  taxonomy A: ref + config validation (path-escape/out-of-scope/bad ids, post "
        "types, source_status, scope ids) all fail-closed"
    )

    # ---- (1) PRO full flow: 2 targets, done rows, translated, assisted link ----
    f1 = FakeWordPress(mode="pro")
    src1 = f1.add_source("Reset password", "<p>reset here</p>")
    c1 = _conn("WP pro basic", f1, targets="fr, de")
    ok, msg, s1 = cstore.wordpress_translate(c1.id, actor="auto", transport=f1.transport())
    assert ok, msg
    assert s1["created"] == 2 and s1["polylang_mode"] == "pro", s1
    rows = idmap.list_for_connection(c1.id)
    assert len(rows) == 2 and all(r.status == "done" for r in rows), rows
    fr_row = next(r for r in rows if r.target_locale == "fr")
    fr_post = f1.get(fr_row.target_id)
    assert fr_post["status"] == "draft" and fr_post["lang"] == "fr", fr_post
    assert f1.get(src1)["translations"].get("fr") == fr_post["id"], "assisted link spliced"
    print(
        "  PRO flow: 2 draft target posts, 2 done rows, source spliced into the "
        "translation group (assisted link)"
    )

    # ---- (2) H3 + gotcha #1 MUTATION-PROVEN: shortcodes masked (never sent to engine),
    #          Gutenberg blocks survive, raw not rendered - proven with a REWRITING engine --
    f2 = FakeWordPress(mode="pro")
    s2 = f2.add_source("Doc", GUTENBERG)
    c2 = _conn("WP fidelity", f2, targets="fr")
    set_pieces("app.services.translate", "pretranslate_document", _rewrite_ptd(real_ptd))
    try:
        ok2, m2, _s2 = cstore.wordpress_translate(c2.id, actor="auto", transport=f2.transport())
        assert ok2, m2
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    body2 = f2.content_of(idmap.get(c2.id, s2, "fr").target_id)
    assert SHORTCODE in body2, (
        "shortcode survived byte-identical - it was MASKED, never sent to the mutating engine",
        body2,
    )
    assert "<!-- wp:paragraph -->" in body2 and '<!-- wp:heading {"level":2} -->' in body2, (
        "block comments + attrs intact",
        body2,
    )
    assert "XHELLO WORLDX" in body2, ("prose WAS mutated by the engine (test is live)", body2)
    assert "RENDERED-FLATTENED" not in body2, "wrote raw, never rendered"
    print(
        "  H3 + gotcha #1 (mutation-proven): shortcodes masked (survive a REWRITING engine "
        "byte-identical); Gutenberg blocks intact; prose mutated; raw not rendered"
    )

    # ---- (3) FREE mode: needs a scope, no NPE on null translations, mode surfaced ----
    f3 = FakeWordPress(mode="free")
    src3 = f3.add_source("Billing", "<p>Pay here</p>")
    c3 = _conn("WP free", f3, targets="fr")
    ok3, msg3, s3 = cstore.wordpress_translate(c3.id, actor="auto", transport=f3.transport())
    assert ok3 and s3["created"] == 1 and s3["polylang_mode"] == "free", s3
    assert "NOT detected" in msg3, msg3
    r3 = idmap.get(c3.id, src3, "fr")
    assert r3 is not None and r3.status == "done", r3
    conn3 = cstore.build_fresh(c3.id, f3.transport())
    assert conn3.find_preexisting(src3, "de", {"title": "no-match-xyz"}) is None, "free: no orphan"
    assert (cstore.get(c3.id).config or {}).get("wordpress_polylang_mode") == "free", "persisted"
    print(
        "  gotcha #2: FREE self-detected (no NPE on null translations); scoped discovery "
        "creates + tracks the target; mode persisted + surfaced"
    )

    # ---- (B1 BLOCKER) FREE + NO scope + auto-discovery -> REFUSED fail-closed ----
    fb = FakeWordPress(mode="free")
    fb.add_source("EN post", "<p>english</p>", lang="en")
    fb.add_source("FR post", "<p>francais</p>", lang="fr")
    fb.add_source("DE post", "<p>deutsch</p>", lang="de")
    cb = _conn("WP free noscope", fb, targets="fr", scope=False)  # NO source_category/tag
    okb, msgb, sb = cstore.wordpress_translate(cb.id, actor="auto", transport=fb.transport())
    assert not okb, (
        "free whole-site discovery must be REFUSED (would treat FR/DE as sources)",
        msgb,
        sb,
    )
    assert "no per-post language signal" in msgb and "Source category" in msgb, (
        "refusal names the remedy",
        msgb,
    )
    assert not fb.creates and not idmap.list_for_connection(cb.id), "refused run wrote nothing"
    print(
        "  B1: FREE whole-site auto-discovery is REFUSED fail-closed (no scope) - existing "
        "other-language content is never treated as a source; the refusal names the remedy"
    )

    # ---- (4) Pro/free detection via test() ----
    okt, msgt = cstore.build_fresh(c1.id, f1.transport()).test()
    assert okt and "Polylang Pro detected" in msgt, msgt
    _okf, msgtf = cstore.build_fresh(c3.id, f3.transport()).test()
    assert "NOT detected" in msgtf, msgtf
    print("  gotcha #2c: test() names the detected Polylang tier")

    # ---- (5) DRAFT posture BOTH posts+pages + steady state (with H2 corroboration) ----
    f5 = FakeWordPress(mode="pro")
    p5 = f5.add_source("A post", "<p>post body</p>", ptype="posts")
    g5 = f5.add_source("A page", "<p>page body</p>", ptype="pages")
    c5 = _conn("WP posture", f5, targets="fr", extra={"post_types": "posts, pages"})
    ok5, _m5, s5 = cstore.wordpress_translate(c5.id, actor="auto", transport=f5.transport())
    assert ok5 and s5["created"] == 2, s5
    assert f5.get(idmap.get(c5.id, p5, "fr").target_id)["status"] == "draft"
    assert f5.get(idmap.get(c5.id, g5, "fr").target_id)["status"] == "draft", "PAGE draft too"
    creates5 = len(f5.creates)
    ok5b, _m5b, s5b = cstore.wordpress_translate(c5.id, actor="auto", transport=f5.transport())
    assert ok5b and s5b["created"] == 0 and s5b["skipped"] == 2, s5b
    assert len(f5.creates) == creates5, "steady state: no re-creates"
    print(
        "  gotcha #3: posts AND pages get explicit DRAFT status; second run steady-state "
        "no-op for both (with a live target-health corroboration GET)"
    )

    # ---- (H2) external DELETION + BLANK detection make the recreate promise true ----
    f_h2 = FakeWordPress(mode="pro")
    sh = f_h2.add_source("Del me", "<p>content</p>")
    c_h2 = _conn("WP h2-del", f_h2, targets="fr")
    cstore.wordpress_translate(c_h2.id, actor="auto", transport=f_h2.transport())
    old_tid = idmap.get(c_h2.id, sh, "fr").target_id
    del f_h2.posts[old_tid.split(":")[-1]]  # human deleted it
    okd, _md, sd = cstore.wordpress_translate(c_h2.id, actor="auto", transport=f_h2.transport())
    new_tid = idmap.get(c_h2.id, sh, "fr").target_id
    assert okd and new_tid != old_tid and idmap.get(c_h2.id, sh, "fr").status == "done", (
        "deleted target detected + recreated with a new id (rule 3)",
        sd,
        old_tid,
        new_tid,
    )
    f_bl = FakeWordPress(mode="pro")
    sb2 = f_bl.add_source("Blank me", "<p>real content</p>")
    c_bl = _conn("WP h2-blank", f_bl, targets="fr")
    cstore.wordpress_translate(c_bl.id, actor="auto", transport=f_bl.transport())
    btid = idmap.get(c_bl.id, sb2, "fr").target_id
    f_bl.posts[btid.split(":")[-1]]["title"] = ""
    f_bl.posts[btid.split(":")[-1]]["content"] = ""  # human blanked it
    okbl, _mbl, sbl = cstore.wordpress_translate(c_bl.id, actor="auto", transport=f_bl.transport())
    assert okbl and sbl["updated"] == 1, ("blanked target refilled (rule 2)", sbl)
    assert "(fr)" in f_bl.content_of(btid), ("machine content restored", f_bl.content_of(btid))
    print(
        "  H2: an externally DELETED target is detected + recreated; a human-BLANKED target "
        "is refilled - the 'deleted post is recreated' promise is now true"
    )

    # ---- (H1) orphan reclamation: crash-orphan (2a) + delete-and-re-add-connection (2b),
    #          now via the SOURCE SLUG (round-2 blocker fix) ----
    f_h1 = FakeWordPress(mode="free")
    sh1 = f_h1.add_source("Orphan src", "<p>orphan</p>")  # slug 'orphan-src'
    c_h1 = _conn("WP h1-crash", f_h1, targets="fr")
    # a crash orphan carries the SOURCE slug (create sets slug=source_slug) + minted title.
    f_h1.add_source(
        "(fr) Orphan src", "<p>(fr) orphan</p>", lang="fr", categories=[], slug="orphan-src"
    )
    n_before = len(f_h1.posts)
    _ok_h1, _m_h1, s_h1 = cstore.wordpress_translate(
        c_h1.id, actor="auto", transport=f_h1.transport()
    )
    row_h1 = idmap.get(c_h1.id, sh1, "fr")
    assert row_h1 is not None and row_h1.status == "needs_link", (
        "the crash orphan is reclaimed as needs_link, not duplicated",
        row_h1,
        s_h1,
    )
    assert len(f_h1.posts) == n_before, ("NO duplicate post minted", len(f_h1.posts), n_before)
    f_h1b = FakeWordPress(mode="free")
    sh1b = f_h1b.add_source("Readd src", "<p>readd</p>")
    c_first = _conn("WP h1-readd-1", f_h1b, targets="fr")
    cstore.wordpress_translate(c_first.id, actor="auto", transport=f_h1b.transport())
    tid_b = idmap.get(c_first.id, sh1b, "fr").target_id
    cstore.delete(c_first.id)  # wipes the id map (GDPR)
    c_second = _conn("WP h1-readd-2", f_h1b, targets="fr")
    posts_before = len(f_h1b.posts)
    _ok2b, _m2b, s2b = cstore.wordpress_translate(
        c_second.id, actor="auto", transport=f_h1b.transport()
    )
    row2b = idmap.get(c_second.id, sh1b, "fr")
    assert row2b is not None and row2b.status == "needs_link" and row2b.target_id == tid_b, (
        "re-add reclaims the existing target by slug (needs_link), not duplicate",
        row2b,
        s2b,
    )
    assert len(f_h1b.posts) == posts_before, ("no duplicate on re-add", len(f_h1b.posts))
    print(
        "  H1: a crash-orphan AND a delete-and-re-add-connection both RECLAIM the existing "
        "target by SOURCE SLUG (needs_link), never duplicate + relay-translate"
    )

    # ---- (ROUND-2 BLOCKER a) two SAME-TITLE sources never needs_link EACH OTHER ----
    f_dup = FakeWordPress(mode="free")
    a_dup = f_dup.add_source("FAQ", "<p>faq one</p>", slug="faq-1")
    b_dup = f_dup.add_source("FAQ", "<p>faq two</p>", slug="faq-2")  # SAME title, diff slug
    c_dup = _conn("WP dup-title", f_dup, targets="fr")
    okd2, _md2, sd2 = cstore.wordpress_translate(
        c_dup.id, actor="auto", transport=f_dup.transport()
    )
    ra = idmap.get(c_dup.id, a_dup, "fr")
    rb = idmap.get(c_dup.id, b_dup, "fr")
    assert okd2 and ra and rb and ra.status == "done" and rb.status == "done", (
        "both same-titled sources create normally (non-unique title -> no title fallback)",
        sd2,
    )
    assert ra.target_id != a_dup and ra.target_id != b_dup, "A did not bind to a SOURCE post"
    assert rb.target_id != a_dup and rb.target_id != b_dup, "B did not bind to a SOURCE post"
    assert idmap.status_counts(c_dup.id).get("needs_link", 0) == 0, "no false needs_link"
    print(
        "  ROUND-2 blocker (a): two same-titled sources create normally - neither is "
        "needs_link'd to the other source (non-unique title disables title fallback)"
    )

    # ---- (ROUND-2 BLOCKER b) cross-locale: a short brand title never binds later locales
    #          to the SOURCE (or the first locale's target) ----
    f_x = FakeWordPress(mode="pro")
    sx = f_x.add_source("OK", "<p>brand</p>", slug="ok")  # short brand title
    c_x = _conn("WP cross-locale", f_x, targets="fr, de")
    okx, _mx, sx2 = cstore.wordpress_translate(c_x.id, actor="auto", transport=f_x.transport())
    rfr = idmap.get(c_x.id, sx, "fr")
    rde = idmap.get(c_x.id, sx, "de")
    assert okx and rfr and rde and rfr.status == "done" and rde.status == "done", sx2
    assert rfr.target_id != sx and rde.target_id != sx, "neither locale bound to the SOURCE"
    assert rfr.target_id != rde.target_id, "fr and de are DISTINCT targets (no ping-pong)"
    assert idmap.status_counts(c_x.id).get("needs_link", 0) == 0, "no cross-locale needs_link"
    print(
        "  ROUND-2 blocker (b): a short/brand title never binds a later locale to the "
        "source or the first locale's target (source + own-targets excluded, lang-scoped)"
    )

    # ---- (9) RULE 6 (Pro link) + adopt-in-place ----
    f9 = FakeWordPress(mode="pro")
    human9 = f9.add_source("Passwort", "<p>human</p>", lang="fr")
    src9 = f9.add_source(
        "Reset", "<p>reset</p>", lang="en", translations={"fr": human9.split(":")[-1]}
    )
    human_before = f9.content_of(human9)
    c9 = _conn("WP rule6", f9, targets="fr")
    ok9, m9, s9 = cstore.wordpress_translate(c9.id, actor="auto", transport=f9.transport())
    assert not ok9 and idmap.get(c9.id, src9, "fr").status == "needs_link", (m9, s9)
    assert f9.content_of(human9) == human_before, "human translation not overwritten"
    oka9, _ma9, _sa9 = cstore.wordpress_resolve_link(c9.id, src9, "fr", "adopt", actor="admin")
    assert oka9 and idmap.get(c9.id, src9, "fr").status == "done"
    f9.posts[src9.split(":")[-1]]["content"] = "<p>reset v2</p>"
    ok9b, _m9b, s9b = cstore.wordpress_translate(c9.id, actor="auto", transport=f9.transport())
    assert ok9b and s9b["updated"] == 1 and idmap.get(c9.id, src9, "fr").target_id == human9
    print(
        "  RULE 6 (Pro link): pre-existing linked human translation -> needs_link, never "
        "overwritten; adopt -> update-in-place"
    )

    # ---- (M1) update OMITS status: a human-published draft target stays published ----
    f_m1 = FakeWordPress(mode="pro")
    sm1 = f_m1.add_source("Pub me", "<p>v1</p>")
    c_m1 = _conn("WP m1", f_m1, targets="fr")  # publish=0 -> draft
    cstore.wordpress_translate(c_m1.id, actor="auto", transport=f_m1.transport())
    tid_m1 = idmap.get(c_m1.id, sm1, "fr").target_id
    f_m1.posts[tid_m1.split(":")[-1]]["status"] = "publish"  # human publishes it
    f_m1.posts[sm1.split(":")[-1]]["content"] = "<p>v2</p>"  # source edit -> update
    okm1, _mm1, sm1r = cstore.wordpress_translate(c_m1.id, actor="auto", transport=f_m1.transport())
    assert okm1 and sm1r["updated"] == 1
    assert f_m1.get(tid_m1)["status"] == "publish", (
        "update must OMIT status so a human-published target stays published",
        f_m1.get(tid_m1),
    )
    print(
        "  M1: an UPDATE omits status entirely - a human-published target is never "
        "un-published on the next source edit"
    )

    # ---- (M2) superseded human edits are reported per pair ----
    f_m2 = FakeWordPress(mode="pro")
    sm2 = f_m2.add_source("Polish me", "<p>v1</p>")
    c_m2 = _conn("WP m2", f_m2, targets="fr")
    cstore.wordpress_translate(c_m2.id, actor="auto", transport=f_m2.transport())
    tid_m2 = idmap.get(c_m2.id, sm2, "fr").target_id
    f_m2.posts[tid_m2.split(":")[-1]]["content"] = "<p>a human polished this</p>"  # human edit
    f_m2.posts[sm2.split(":")[-1]]["content"] = "<p>v2</p>"  # source edit
    okm2, mm2, sm2r = cstore.wordpress_translate(c_m2.id, actor="auto", transport=f_m2.transport())
    assert okm2 and sm2r["updated"] == 1
    assert tid_m2 in sm2r["superseded"] and "Superseded human edits" in mm2, (
        "the overwritten human edit is reported per pair (visible, not silent)",
        sm2r,
        mm2,
    )
    print(
        "  M2: overwriting a human-edited target is reported as SUPERSEDED per pair "
        "(message + result), not silent"
    )

    # ---- (M3) FRESH Polylang mode both stale directions ----
    f_5a = FakeWordPress(mode="pro")
    f_5a.add_source("x", "<p>x</p>")
    c_5a = _conn("WP m3-a", f_5a, targets="fr")
    cstore.patch_config(c_5a.id, {"wordpress_polylang_mode": "free"})  # STALE
    ok5a, m5a, s5a = cstore.wordpress_translate(c_5a.id, actor="auto", transport=f_5a.transport())
    assert ok5a and s5a["polylang_mode"] == "pro" and "Pro detected" in m5a, (
        "stale 'free' cfg -> fresh probe reports PRO",
        s5a,
        m5a,
    )
    f_5b = FakeWordPress(mode="free")
    f_5b.add_source("y", "<p>y</p>")
    c_5b = _conn("WP m3-b", f_5b, targets="fr")
    cstore.patch_config(c_5b.id, {"wordpress_polylang_mode": "pro"})  # STALE
    ok5bb, m5bb, s5bb = cstore.wordpress_translate(
        c_5b.id, actor="auto", transport=f_5b.transport()
    )
    assert ok5bb and s5bb["polylang_mode"] == "free" and "NOT detected" in m5bb, (
        "stale 'pro' cfg -> fresh probe reports FREE",
        s5bb,
        m5bb,
    )
    print(
        "  M3: the Polylang mode is the FRESH per-run probe, correct in BOTH stale "
        "directions (stale-free->pro, stale-pro->free)"
    )

    # ---- (M4) explicit article_ids: a named OWN-target id is refused (not re-translated) --
    f_m4 = FakeWordPress(mode="free")
    sm4 = f_m4.add_source("Source m4", "<p>src</p>")
    c_m4 = _conn("WP m4", f_m4, targets="fr")
    cstore.wordpress_translate(c_m4.id, actor="auto", transport=f_m4.transport())
    own_tid = idmap.get(c_m4.id, sm4, "fr").target_id
    f_m4.posts[sm4.split(":")[-1]]["content"] = "<p>src v2</p>"
    _okm4, mm4, sm4r = cstore.wordpress_translate(
        c_m4.id, article_ids=[own_tid, sm4], actor="auto", transport=f_m4.transport()
    )
    assert own_tid in sm4r["ignored_own_ids"] and "own translation posts" in mm4, (
        "a named own-target id is refused with a report line",
        sm4r,
        mm4,
    )
    # round-2 M3: explicit ids on the FREE tier bypass the language fence -> per-run note.
    assert sm4r["explicit_ids_on_free"] and "language cannot be verified on this tier" in mm4, (
        "explicit ids on free are flagged (language cannot be verified)",
        sm4r,
        mm4,
    )
    print(
        "  M4 + M3: an explicitly-named own-target id is refused (report line); explicit "
        "ids on the free tier are flagged (language cannot be verified)"
    )

    # ---- (M5) excerpt translated + <img alt> translated, cover-block alt counted ----
    f_m5 = FakeWordPress(mode="pro")
    sm5 = f_m5.add_source(
        "With excerpt",
        '<p>Body text</p><img src="a.png" alt="A red cat">'
        '<!-- wp:cover {"url":"b.png","alt":"A blue sky"} -->'
        '<div class="wp-block-cover"><img src="b.png" alt="A blue sky">'
        "<p>Cover text</p></div><!-- /wp:cover -->",
        excerpt="A short summary",
    )
    c_m5 = _conn("WP m5", f_m5, targets="fr")
    okm5, mm5, sm5r = cstore.wordpress_translate(c_m5.id, actor="auto", transport=f_m5.transport())
    assert okm5, mm5
    tgt5 = f_m5.get(idmap.get(c_m5.id, sm5, "fr").target_id)
    assert "(fr)" in tgt5["excerpt"], ("excerpt is translated", tgt5["excerpt"])
    body5 = tgt5["content"]
    assert 'alt="(fr) A red cat"' in body5, ("image alt is translated", body5)
    # The cover block's <img alt> stays in sync with its JSON alt: both untranslated.
    assert '"alt":"A blue sky"' in body5 and 'alt="A blue sky"' in body5, (
        "cover-block image alt left untranslated (JSON copy stays in sync)",
        body5,
    )
    assert "(fr) Cover text" in body5, ("cover-block TEXT is still translated", body5)
    assert sm5r["img_alt_untranslated"] == 1 and "cover blocks were NOT translated" in mm5, (
        "img_alt_untranslated counts only the cover-block alt, and the note says so",
        sm5r,
        mm5,
    )
    print(
        "  M5: excerpt and image alt are translated; a cover-block image alt is left "
        "untranslated (its JSON copy stays in sync) and counted truthfully"
    )

    # ---- (ROUND-2 H1) shortcode inside an HTML ATTRIBUTE is left as-is (tag not destroyed) --
    from app.services.connectors.wordpress import _mask_shortcodes, _unmask_shortcodes

    attr_html = '<a href="[site_url]/x">click here</a> and [gallery ids="1"] text'
    masked, codes = _mask_shortcodes(attr_html)
    assert "[site_url]" in masked, (
        "a shortcode inside an attribute is NOT masked (would break the tag)",
        masked,
    )
    assert '<a href="[site_url]/x">' in masked, "the <a> tag is intact"
    assert codes == ['[gallery ids="1"]'], ("only the TEXT-node shortcode was masked", codes)
    assert _unmask_shortcodes(masked, codes) == attr_html, "round-trips byte-identical"
    print(
        "  ROUND-2 H1: a shortcode inside an HTML attribute is left as-is (tag preserved); "
        "only text-node shortcodes are masked"
    )

    # ---- (ROUND-2 M1) prose brackets are NOT masked; residual shortcode masks are counted --
    for prose in ("[12]", "[Note: see below]", "[citation needed]", "see [1] and [2]"):
        m, cs = _mask_shortcodes(f"text {prose} more")
        assert not cs, (f"prose bracket must NOT be masked: {prose!r}", m, cs)
    for real in ("[gallery]", '[contact-form-7 id="7"]', "[/gallery]", "[foo /]"):
        _m, cs = _mask_shortcodes(f"x {real} y")
        assert len(cs) == 1, (f"a real shortcode must be masked: {real!r}", cs)
    print(
        "  ROUND-2 M1: prose brackets ([12], [Note:...], [citation needed], [1]) are NOT "
        "masked; real shortcodes are; residual masks are reported"
    )

    # ---- (ROUND-2 H2) a lost mask sentinel HOLDS the pair (never writes broken) ----
    import app.filters as _filters

    f_int = FakeWordPress(mode="pro")
    f_int.add_source("Integrity", "<p>Hello</p> " + SHORTCODE)
    c_int = _conn("WP integrity", f_int, targets="fr")
    _html = _filters.get_filter("html")
    _orig_build = _html.build
    import re as _re

    _html.build = lambda orig, texts: _re.sub(
        rb"<!--wpsc:\d+-->", b"", _orig_build(orig, texts)
    )  # STRIP sentinels
    try:
        oki2, mi2, si2 = cstore.wordpress_translate(
            c_int.id, actor="auto", transport=f_int.transport()
        )
    finally:
        _html.build = _orig_build
    assert not oki2, ("a lost shortcode HOLDS the pair (run not clean)", si2)
    assert si2["shortcode_lost_held"] == 1 and not f_int.creates, (
        "the pair is held, NOT written broken",
        si2,
        f_int.creates,
    )
    assert "masked shortcode was lost" in mi2, ("reported", mi2)
    # #307: additions are held on both create and update, with no remote write.
    for existing in (False, True):
        f_inj = FakeWordPress(mode="pro")
        f_inj.add_source("Injection", "<p>Hello</p>")
        c_inj = _conn("WP injected " + str(existing), f_inj, targets="fr")
        if existing:
            assert cstore.wordpress_translate(c_inj.id, actor="auto", transport=f_inj.transport())[
                0
            ]
        before = json.dumps(f_inj.posts, sort_keys=True)

        def _inject(session, doc_id, locale, *args, **kwargs):
            result = real_ptd(session, doc_id, locale, *args, **kwargs)
            for sg in session.exec(select(Segment).where(Segment.document_id == doc_id)).all():
                if sg.target_text:
                    sg.target_text += " [gallery]"
                    session.add(sg)
            session.commit()
            return result

        set_pieces("app.services.translate", "pretranslate_document", _inject)
        try:
            ok_inj, msg_inj, stats_inj = cstore.wordpress_translate(
                c_inj.id,
                actor="auto",
                transport=f_inj.transport(),
                only_pairs=[("posts:1001", "fr")],
            )
        finally:
            set_pieces("app.services.translate", "pretranslate_document", real_ptd)
        assert not ok_inj and stats_inj["shortcode_lost_held"] == 1, stats_inj
        assert "added, changed, or duplicated" in msg_inj, msg_inj
        assert json.dumps(f_inj.posts, sort_keys=True) == before
    print(
        "  ROUND-2 H2: a mask sentinel stripped by the build/engine HOLDS the pair "
        "(reported, never written with a lost shortcode)"
    )

    # ---- (ROUND-2 H3) server-side normalization does NOT flag a false 'superseded' ----
    class _NormalizingWP(FakeWordPress):
        """A site that NORMALIZES stored content on write (e.g. collapses spaces) - the PUT
        RESPONSE differs from the sent bytes. Persisting the SENT bytes would flag every
        later update as a human edit; persisting the RESPONSE hash does not."""

        def _handle(self, req):
            if req.method == "PUT" and "/wp/v2/" in req.url.path:
                import json as _j

                body = _j.loads(req.content)
                if "content" in body:
                    body["content"] = body["content"].replace("  ", " ") + "<!--kses-->"
                req = httpx.Request(
                    "PUT", req.url, headers=req.headers, content=_j.dumps(body).encode()
                )
            return super()._handle(req)

    f_norm = _NormalizingWP(mode="pro")
    s_norm = f_norm.add_source("Norm", "<p>v1</p>")
    c_norm = _conn("WP norm", f_norm, targets="fr")
    cstore.wordpress_translate(c_norm.id, actor="auto", transport=f_norm.transport())
    f_norm.posts[s_norm.split(":")[-1]]["content"] = "<p>v2</p>"  # source edit, NO human edit
    okn, _mn, sn = cstore.wordpress_translate(c_norm.id, actor="auto", transport=f_norm.transport())
    assert okn and sn["updated"] == 1 and not sn["superseded"], (
        "server normalization must NOT flag a false 'superseded' (compare RESPONSE hashes)",
        sn,
    )
    print(
        "  ROUND-2 H3: server-side normalization does not cause a false 'superseded' "
        "(we persist the PUT RESPONSE hash, not the sent bytes)"
    )

    # ---- (ROUND-2 M4) a TRASHED target (context=edit -> 200 status=trash) -> recreate ----
    f_tr = FakeWordPress(mode="pro")
    s_tr = f_tr.add_source("Trash me", "<p>content</p>")
    c_tr = _conn("WP trash", f_tr, targets="fr")
    cstore.wordpress_translate(c_tr.id, actor="auto", transport=f_tr.transport())
    old_tr = idmap.get(c_tr.id, s_tr, "fr").target_id
    f_tr.posts[old_tr.split(":")[-1]]["status"] = "trash"  # human trashed it
    oktr, _mtr, str_ = cstore.wordpress_translate(c_tr.id, actor="auto", transport=f_tr.transport())
    new_tr = idmap.get(c_tr.id, s_tr, "fr").target_id
    assert oktr and new_tr != old_tr and idmap.get(c_tr.id, s_tr, "fr").status == "done", (
        "a trashed target is treated as missing -> recreated (M4)",
        str_,
        old_tr,
        new_tr,
    )
    print(
        "  ROUND-2 M4: a TRASHED target (context=edit returns 200/status=trash) is treated "
        "as missing and recreated, not suppressed forever"
    )

    # ---- (ROUND-2 L3) a garbage (non-JSON) health response SUPPRESSES (safe), never writes --
    class _GarbageWP(FakeWordPress):
        def _handle(self, req):
            # after the first run, return non-JSON on the single-post GET (the health probe)
            if (
                req.method == "GET"
                and "/wp/v2/posts/" in req.url.path
                and getattr(self, "_armed", False)
            ):
                return httpx.Response(200, content=b"<html>not json</html>")
            return super()._handle(req)

    f_g = _GarbageWP(mode="free")
    f_g.add_source("Garbage", "<p>x</p>")
    c_g = _conn("WP garbage", f_g, targets="fr")
    cstore.wordpress_translate(c_g.id, actor="auto", transport=f_g.transport())
    creates_g = len(f_g.creates)
    f_g._armed = True
    okg, _mg, sg = cstore.wordpress_translate(c_g.id, actor="auto", transport=f_g.transport())
    assert okg and len(f_g.creates) == creates_g, (
        "a garbage health response SUPPRESSES (safe), never triggers a write",
        sg,
        f_g.creates,
    )
    print(
        "  ROUND-2 L3: a garbage (non-JSON) target-health response suppresses safely "
        "(no destructive recreate)"
    )

    # ---- (L1) discovery sends an EXPLICIT status (kill the surviving mutant) ----
    f_l1 = FakeWordPress(mode="free")
    f_l1.add_source("l1", "<p>x</p>")
    c_l1 = _conn("WP l1", f_l1, targets="fr")
    cstore.build_fresh(c_l1.id, f_l1.transport()).discover_articles()
    # the scoped discovery call carries categories=<scope>; the Pro-detection probe (status
    # =any, per_page=1) does not - isolate the discovery call and assert its explicit status.
    disc_calls = [lc for lc in f_l1.list_calls if lc.get("categories")]
    assert disc_calls and all(lc["status"] == "publish" for lc in disc_calls), (
        "discovery sends explicit status=publish (never the REST default)",
        f_l1.list_calls,
    )
    print("  L1: discovery sends an explicit status=<source_status> (never the REST default)")

    # ---- (L2) all-raw_unavailable run -> ATTENTION (ok=False), not plain connected ----
    f_l2 = FakeWordPress(mode="free", rendered_only=True)
    f_l2.add_source("rendered only", "<p>x</p>")
    c_l2 = _conn("WP l2", f_l2, targets="fr")
    okl2, ml2, sl2 = cstore.wordpress_translate(c_l2.id, actor="auto", transport=f_l2.transport())
    assert not okl2, ("an all-raw-unavailable run is an ATTENTION state (ok=False)", sl2)
    assert sl2["raw_unavailable_skipped"] == 1 and not f_l2.creates and "ATTENTION" in ml2
    print(
        "  L2: a run whose only outcome is raw-unavailable skips is an ATTENTION state "
        "(ok=False), never plain connected; nothing translated as rendered HTML"
    )

    # ---- (gotcha #4) pagination boundaries via _paginate ----
    def _pag(n):
        fp = FakeWordPress(mode="free")
        for i in range(n):
            fp.add_source(f"p{i}", "<p>x</p>", pid=200000 + i)
        conn = cstore.build_fresh(_conn(f"WP pag {n}", fp).id, fp.transport())
        with conn._client() as cc:
            return len(conn._paginate(cc, "posts", {"context": "edit", "status": "publish"}, 1000))

    assert _pag(1) == 1 and _pag(100) == 100 and _pag(200) == 200 and _pag(201) == 201, "boundaries"
    print("  gotcha #4: pagination returns every item at 1/100/200/201 (no last-page drop)")

    # ---- (E) taxonomy E: round-robin across post types ----
    f_e = FakeWordPress(mode="free")
    for i in range(5):
        f_e.add_source(f"post {i}", "<p>x</p>", ptype="posts", pid=300000 + i)
    for i in range(5):
        f_e.add_source(f"page {i}", "<p>x</p>", ptype="pages", pid=310000 + i)
    c_e = _conn(
        "WP rr", f_e, targets="fr", extra={"post_types": "posts, pages", "max_articles": "4"}
    )
    got = cstore.build_fresh(c_e.id, f_e.transport()).discover_articles()
    assert len(got) == 4 and {"posts", "pages"} <= {r["_type"] for r in got}, (
        "both types represented under the cap",
        [r["_type"] for r in got],
    )
    print("  taxonomy E: a shared cap is filled round-robin across post types")

    # ---- NEEDS_REVIEW exclusion + fully-refused not created ----
    def _flip(session, doc_id, locale, spec=None, force_pii=False, which="one"):
        stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == "fr":
            for sg in session.exec(
                select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "fr")
            ).all():
                if which == "all" or sg.source_text == "Hello world":
                    sg.status = SegStatus.NEEDS_REVIEW
                    session.add(sg)
            session.commit()
        return stats

    f_nr = FakeWordPress(mode="free")
    snr = f_nr.add_source("NR", GUTENBERG)
    c_nr = _conn("WP nr", f_nr, targets="fr")
    set_pieces(
        "app.services.translate",
        "pretranslate_document",
        lambda *a, **k: _flip(*a, **k, which="one"),
    )
    try:
        oknr, mnr, _snr = cstore.wordpress_translate(
            c_nr.id, actor="auto", transport=f_nr.transport()
        )
        assert oknr, mnr
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    # #640: a pair with a held unit is NOT created (the held spot would carry SOURCE text: a
    # mixed-language target); nothing reaches the wire and the next run retries it.
    assert idmap.get(c_nr.id, snr, "fr") is None and not f_nr.creates, (
        "NR held pair: no post created, no id-map row",
        f_nr.creates,
    )
    # #640 gate: the UPDATE path of an EXISTING target while a unit is held. The held unit's
    # NEEDS_REVIEW text never reaches the wire, the other units are written, the hash is
    # withheld, and the next run (unit approved) completes the pair and records the hash.
    oknr, mnr, _snr = cstore.wordpress_translate(c_nr.id, actor="auto", transport=f_nr.transport())
    assert oknr and len(f_nr.creates) == 1, (mnr, f_nr.creates)
    tid_nr = idmap.get(c_nr.id, snr, "fr").target_id
    assert "(fr) Hello world" in f_nr.content_of(tid_nr), f_nr.content_of(tid_nr)
    assert (cstore.get(c_nr.id).config or {}).get("wordpress_hashes"), "clean create: hash"
    cstore.patch_config(c_nr.id, remove=("wordpress_hashes",))  # force a re-run (update)
    puts_before = len(f_nr.puts)
    set_pieces(
        "app.services.translate",
        "pretranslate_document",
        lambda *a, **k: _flip(*a, **k, which="one"),
    )
    try:
        oknr, mnr, rnr = cstore.wordpress_translate(
            c_nr.id, actor="auto", transport=f_nr.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    bnr = f_nr.content_of(tid_nr)
    assert oknr and len(f_nr.puts) > puts_before and len(f_nr.creates) == 1, (mnr, f_nr.puts)
    assert "(fr) Hello world" not in bnr, ("held NEEDS_REVIEW text never written", bnr)
    assert "(fr) Second step" in bnr and SHORTCODE in bnr, ("others written", bnr)
    assert rnr.get("partial_held") == 1, rnr
    assert not (cstore.get(c_nr.id).config or {}).get("wordpress_hashes"), "held: no hash"
    oknr, mnr, _snr = cstore.wordpress_translate(c_nr.id, actor="auto", transport=f_nr.transport())
    assert oknr and "(fr) Hello world" in f_nr.content_of(tid_nr), f_nr.content_of(tid_nr)
    assert (cstore.get(c_nr.id).config or {}).get("wordpress_hashes"), "approved: hash recorded"
    f_ar = FakeWordPress(mode="free")
    sar = f_ar.add_source("AR", GUTENBERG)
    c_ar = _conn("WP ar", f_ar, targets="fr")
    set_pieces(
        "app.services.translate",
        "pretranslate_document",
        lambda *a, **k: _flip(*a, **k, which="all"),
    )
    try:
        okar, _mar, sar_r = cstore.wordpress_translate(
            c_ar.id, actor="auto", transport=f_ar.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    assert not okar and not f_ar.creates and idmap.get(c_ar.id, sar, "fr") is None, (
        "fully-refused -> not created, no row",
        sar_r,
    )
    print("  refusal guard: NR unit holds the pair (#640); fully-refused not created")

    # ---- regression: no watermark; retry only failed pairs; GDPR delete ----
    cfg1 = cstore.get(c1.id).config or {}
    assert "wordpress_hashes" in cfg1 and not any("watermark" in k for k in cfg1), list(cfg1)
    f_rt = FakeWordPress(mode="free")
    x = f_rt.add_source("X", "<p>x</p>", pid=400001)
    y = f_rt.add_source("Y", "<p>y</p>", pid=400002)
    c_rt = _conn("WP rt", f_rt, targets="fr, de")
    fails = [
        {"article_id": x, "locale": "fr", "error": "500"},
        {"article_id": y, "locale": "de", "error": "500"},
    ]
    okr, _mr, _sr = cstore.retry_failed(c_rt.id, fails, actor="auto", transport=f_rt.transport())
    assert okr and {(r.source_id, r.target_locale) for r in idmap.list_for_connection(c_rt.id)} == {
        (x, "fr"),
        (y, "de"),
    }, "retry only the failed pairs"
    cstore.delete(c_rt.id)
    with Session(engine) as s:
        assert not s.exec(
            select(ConnectorIdMap).where(ConnectorIdMap.connection_id == c_rt.id)
        ).all(), "delete swept id-map rows"
    print("  regression: no watermark; retry only failed pairs; GDPR delete sweeps id-map")

    # ---- parity: org isolation + web surfaces + CLI ----
    c = TestClient(app)
    from tests._api_fixture import user_api_token

    hdr = {"X-Loc-Token": user_api_token()}
    fapi = FakeWordPress(mode="free")
    fapi.add_source("api", "<p>x</p>")
    capi = _conn("WP api", fapi, targets="fr")
    assert (
        c.post("/api/connectors/99999999/wordpress/translate", json={}, headers=hdr).status_code
        == 404
    )
    assert (
        c.post("/api/connectors/99999999/wordpress/resolve-link", json={}, headers=hdr).status_code
        == 404
    )
    assert "WordPress" in c.get("/connectors").text
    typ = c.get("/connectors/wordpress").text
    assert "Create connection" in typ and "New connections are closed" not in typ, "ACTIVE add form"
    assert "live qa pending" in typ.lower(), "L3: live-QA-pending badge on the add page"
    _fn = {fs.name for fs in _reg.spec("wordpress").config_fields} | {
        fs.name for fs in _reg.spec("wordpress").secret_fields
    }
    assert {"base_url", "post_types", "application_password", "source_category"} <= _fn, _fn
    page = c.get(f"/connectors/connection/{capi.id}").text
    assert "/wordpress/translate" in page and "id map" in page.lower() and "Polylang" in page
    assert _reg.is_archived("wordpress") is False and "wordpress" in {
        s.kind for s in _reg.active_specs()
    }
    print(
        "  parity: org isolation; WordPress tile + ACTIVE add form + live-QA badge + "
        "id-map/Polylang panel + source_category field; active-tier spec"
    )

    from app.cli.cmd_connector import _wordpress_resolve_link, _wordpress_translate

    fcli = FakeWordPress(mode="free")
    scli = fcli.add_source("cli", "<p>x</p>")
    conncli = _conn("WP cli", fcli, targets="fr")

    class _Args:
        connection_id = conncli.id
        article_ids = ""
        locales = ""
        actor = "cli"
        local = True
        org = None
        json = False
        source_id = scli
        target_locale = "fr"
        action = "ignore"

    class _Emit:
        def __init__(self):
            self.res = None

        def result(self, msg, data):
            self.res = (msg, data)

    real_wp = cstore.wordpress_translate

    def _cap_wp(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        return real_wp(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=fcli.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "wordpress_translate", _cap_wp)
    try:
        emit = _Emit()
        assert _wordpress_translate(_Args(), emit) == 0 and emit.res[1]["ok"], emit.res
    finally:
        set_pieces("app.services.connectors.store", "wordpress_translate", real_wp)
    emit2 = _Emit()
    assert _wordpress_resolve_link(_Args(), emit2) in (0, 1) and emit2.res is not None
    print("  CLI: wordpress-translate + wordpress-resolve-link parse args and run")

    print("verify_wordpress OK")


if __name__ == "__main__":
    main()
