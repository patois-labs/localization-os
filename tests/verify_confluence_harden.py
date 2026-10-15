"""Confluence connector verification (rebuild: v2 API, T3 id map, shared flow).
Fully offline: a stateful fake Confluence tenant (tests/_confluence_fake.py) behind
httpx.MockTransport, plus a capturing provider.

Groups (the number printed with each line):
  1   discovery + create: a child page per (page, locale), right v2 payloads, id map `done`,
      scope space respected, hash keys lower-case, our own children never re-discovered
  2   idempotent re-run (0 writes), changed source -> UPDATE in place (PUT version+1), no dup
  3   source-context harvest (space name, ancestors, labels) in every prompt; both OFF flags
  4   #640 held-back: all-held / body-held on a NEW pair never creates; held title or body on an
      EXISTING child keeps the stored value (never the source), no hash, retried after approval
  5   id map fail-closed: a human page with the target title (child, tag or elsewhere in the
      space) is never overwritten (needs_link); a trashed target is orphaned and recreated; an
      unverifiable target holds the pair
  6   existing connections: legacy confluence_children/confluence_hashes keep working
  7   scope / path escape (taxonomy A): CQL AND-ing, paren injection, cql-only scope + ids,
      out-of-scope ids, bad ids and space keys never reach the wire
  8   pagination by cursor (only the cursor is followed); write budget ceiling
  9   failures: title clash, 5xx not resent, retry resumes the reserved page (no duplicate),
      429 backoff through base._request, token never in messages
  10  OAuth gateway paths (v2 under /wiki, v1 search with NO /wiki) + Bearer
  11  storage-format output stays text (no injected markup)
  12  webhook intake + versionless event key + harvested context caps (unchanged behaviour)
  13  surfaces: SPEC un-archived, T3 capability, REGISTRY
"""

import hashlib
import hmac
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_confl_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import Connection, Project, Segment, SegStatus, SyncLog, WebhookEvent
from app.services.connectors import base as cbase
from app.services.connectors import idmap, webhooks
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.confluence import ConfluenceConnector, _confluence_event_key
from app.services.providers import Provider, register_provider
from tests._confluence_fake import FakeConfluence

TOKEN = "tok-SECRET-confl-123456"  # pregate: allow-secret offline fake tenant value
SRC_BODY = "<h1>Reset password</h1><p>Click the link.</p>"


class CaptureProvider(Provider):
    name = "capture"
    calls = []  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        CaptureProvider.calls.append({"prompt": prompt, "task": task, "payload": payload})
        self.last_usage = {
            "input_tokens": max(1, len(prompt or "") // 4),
            "output_tokens": 4,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        payload = payload or {}
        if task == "translate":
            return f"[{payload.get('target', 'xx')}] {payload.get('text', prompt or '')}"
        return (prompt or "").strip()

    @classmethod
    def reset(cls):
        cls.calls = []


def _use_capture_provider(cid: int) -> None:
    with Session(engine) as s:
        conn = s.get(Connection, cid)
        proj = s.get(Project, conn.project_id) if conn and conn.project_id else None
        if proj:
            proj.default_provider = "capture"
            s.add(proj)
            s.commit()


def _translate_calls():
    return [c for c in CaptureProvider.calls if c["task"] == "translate"]


def T(loc, base="Reset password"):
    """The child title under the offline mock MT: it prefixes the translated title with the
    lower-case locale, so '[es-MX] [es-mx] Reset password'."""
    return f"[{loc}] [{loc.lower()}] {base}"


def _tenant(**kw):
    """HELP space with a parent chain Account > Security > page 'Reset password'."""
    f = FakeConfluence(**kw)
    f.add_space("HELP", "Customer Help Center")
    f.add_space("OTHER", "Other Space")
    acct = f.add_page("HELP", "Account", "<p>root</p>")
    sec = f.add_page("HELP", "Security", "<p>sec</p>", parent=acct)
    src = f.add_page("HELP", "Reset password", SRC_BODY, parent=sec, labels=["kb", "public"])
    return f, src


def _mk_conn(name, extra_cfg=None, secret_extra=None):
    cfg = {
        "base_url": "https://team.atlassian.net",
        "space_key": "HELP",
        "target_locales": "es-MX, de-DE",
        "source_locale": "en-US",
        "title_pattern": "[{loc}] {title}",
    }
    cfg.update(extra_cfg or {})
    sec = {"email": "b@x.com", "api_token": TOKEN}
    sec.update(secret_extra or {})
    return cstore.create("confluence", name, cfg, sec, actor="admin")


def _cfg(cid):
    with Session(engine) as s:
        return dict(s.get(Connection, cid).config or {})


def _hashes(cid):
    return dict(_cfg(cid).get("confluence_hashes") or {})


def _hold(predicate):
    from app.services import translate as tr

    orig = tr.pretranslate_document

    def patched(s, doc_id, loc, *a, **kw):
        out = orig(s, doc_id, loc, *a, **kw)
        for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if predicate(sg):
                sg.status = SegStatus.NEEDS_REVIEW
                s.add(sg)
        s.commit()
        return out

    tr.pretranslate_document = patched
    return lambda: setattr(tr, "pretranslate_document", orig)


def _synclogs(cid):
    with Session(engine) as s:
        return s.exec(select(SyncLog).where(SyncLog.connection_id == cid)).all()


def _raises(fn, needle):
    try:
        fn()
    except ConnectorError as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return
    raise AssertionError(f"expected ConnectorError containing {needle!r}")


def _tr(cid, fake, **kw):
    return cstore.confluence_translate(cid, actor="auto", transport=fake.transport(), **kw)


def main() -> None:
    init_db()
    register_provider("capture", CaptureProvider())
    cbase._sleep = lambda s: None  # backoff runs instantly

    # =====================================================================
    # 1. discovery + create
    # =====================================================================
    fake, src = _tenant()
    other = fake.add_page("OTHER", "Out of scope", "<p>Secret.</p>")
    conn = _mk_conn("Confl main")
    ok, msg, r = _tr(conn.id, fake)
    # the space holds 4 pages (Account, Security, Reset password) = 3 sources x 2 locales
    assert ok and r["created"] == 6 and r["updated"] == 0, (msg, r)
    assert not [
        p
        for p in fake.pages.values()
        if p["spaceId"] != fake.pages[src]["spaceId"] and p["id"] != other
    ], "nothing may be created outside the scope space"
    es = fake.by_title(T("es-MX"))
    de = fake.by_title(T("de-DE"))
    assert len(es) == 1 and len(de) == 1, (msg, [p["title"] for p in fake.pages.values()])
    assert es[0]["parentId"] == src and es[0]["spaceId"] == fake.pages[src]["spaceId"]
    assert "<h1>" in es[0]["body"] and "</p>" in es[0]["body"], es[0]["body"]
    assert es[0]["body"] != SRC_BODY, "the child body must be translated"
    # v2 request shapes (what the real API accepts)
    posts = [e for e in fake.log if e["method"] == "POST"]
    assert posts and all(
        set(e["body"]) <= {"spaceId", "status", "title", "parentId", "body"}
        and e["body"]["body"]["representation"] == "storage"
        and e["api"] == "v2"
        for e in posts
    ), posts
    assert not any(e["api"] == "v1" for e in fake.log), "no CQL given: v1 must not be touched"
    # Review gate: reserve AND fill in one create. No published page is ever empty: every POST
    # already carries the translated body, and no follow-up PUT is needed on a fresh create.
    assert all(
        (e["body"]["body"]["value"] or "").strip() and e["body"]["body"]["value"] != SRC_BODY
        for e in posts
    ), [e["body"]["body"] for e in posts]
    assert not fake.writes("PUT"), "a fresh create must not need a second (fill) version"
    assert all(p["version"]["number"] == 1 for p in fake.pages.values()), "one version per child"
    cf1 = ConfluenceConnector(_cfg(conn.id), {"email": "b@x.com", "api_token": TOKEN})
    for held in ({"title": None, "body": "<p>x</p>"}, {"title": "x", "body": None}):
        tid, err = cf1.create_target(src, "es-mx", {**held, "space_id": "1", "locale": "es-mx"})
        assert tid is None and err.startswith("held-back"), ("create refuses a partial", held, err)
    rows = idmap.list_for_connection(conn.id)
    assert len(rows) == 6 and all(x.status == idmap.DONE for x in rows), [x.status for x in rows]
    assert f"{src}:es-mx" in _hashes(conn.id), "hash keys are lower-case like every flow connector"
    assert other not in [p["parentId"] for p in fake.pages.values()]
    print("  1. create: child page per (page, locale) via v2, id map done, scope space only")

    # =====================================================================
    # 2. idempotent re-run, then update in place
    # =====================================================================
    n_pages, n_writes = len(fake.pages), len(fake.writes())
    ok, msg, r = _tr(conn.id, fake)
    assert ok and r["created"] == 0 and r["updated"] == 0 and r["skipped"] == 6, (msg, r)
    assert len(fake.pages) == n_pages and len(fake.writes()) == n_writes, "re-run must not write"
    print("  2a. re-run: 6 skipped, 0 writes, our own children are not re-discovered as sources")
    fake.pages[src]["body"] = "<h1>Reset password</h1><p>Click the NEW link.</p>"
    fake.pages[src]["version"]["number"] += 1
    v_before = fake.by_title(T("es-MX"))[0]["version"]["number"]
    ok, msg, r = _tr(conn.id, fake, page_ids=[src])
    assert ok and r["updated"] == 2 and r["created"] == 0, (msg, r)
    assert len(fake.pages) == n_pages, "update in place, no duplicate child"
    es = fake.by_title(T("es-MX"))[0]
    assert es["version"]["number"] == v_before + 1 and "NEW" in es["body"], es
    puts = fake.writes("PUT")[-2:]
    assert all(p["body"]["version"]["number"] == v_before + 1 for p in puts), puts
    print("  2b. changed source -> PUT version+1 on the same child, no duplicate")

    # =====================================================================
    # 3. context harvest
    # =====================================================================
    f3, s3 = _tenant()
    c3 = _mk_conn("Confl ctx")
    _tr(c3.id, f3, page_ids=[s3])
    _use_capture_provider(c3.id)
    CaptureProvider.reset()
    f3.pages[s3]["body"] = "<h1>Reset password</h1><p>Click the NEWER link.</p>"
    f3.pages[s3]["version"]["number"] += 1
    ok, msg, r = _tr(c3.id, f3, page_ids=[s3])
    assert ok, msg
    calls = _translate_calls()
    assert calls and not any(c["task"] == "doc_brief" for c in CaptureProvider.calls)
    for c in calls:
        p = c["prompt"]
        assert '<translation_context source="confluence"' in p, p
        assert "Customer Help Center" in p and "Account / Security" in p, p
        assert "kb" in p and "public" in p, p
    print("  3a. space name + ancestor chain + labels in every prompt, no LLM pre-pass")
    for flag, name in (({"context_brief": "0"}, "alias"), ({"harvest_context": "0"}, "canonical")):
        fo, so = _tenant()
        co = _mk_conn(f"Confl ctx off {name}", flag)
        _tr(co.id, fo, page_ids=[so])
        _use_capture_provider(co.id)
        CaptureProvider.reset()
        fo.pages[so]["body"] = "<h1>Reset password</h1><p>different</p>"
        fo.pages[so]["version"]["number"] += 1
        _tr(co.id, fo, page_ids=[so])
        for c in _translate_calls():
            assert "Customer Help Center" not in c["prompt"], (name, c["prompt"])
    print("  3b. context_brief=0 and harvest_context=0 both disable the harvest")

    # =====================================================================
    # 4. #640 held-back
    # =====================================================================
    f4, s4 = _tenant()
    c4 = _mk_conn("Confl held all", {"target_locales": "es-MX"})
    undo = _hold(lambda sg: True)
    try:
        ok, msg, r = _tr(c4.id, f4, page_ids=[s4])
    finally:
        undo()
    assert r["created"] == 0 and r["held_back"] == 1, (msg, r)
    assert not f4.by_title(T("es-MX")) and not _hashes(c4.id), "no page, no hash"
    assert not f4.writes(), f4.writes()
    assert "Held back" in msg, msg
    print("  4a. all units held: nothing written, no hash, run says held back")

    f4b, s4b = _tenant()
    c4b = _mk_conn("Confl held body new", {"target_locales": "es-MX"})
    undo = _hold(lambda sg: sg.order_index >= 1)
    try:
        ok, msg, r = _tr(c4b.id, f4b, page_ids=[s4b])
    finally:
        undo()
    assert not f4b.by_title(T("es-MX")) and r["held_back"] == 1, (msg, r)
    assert not _hashes(c4b.id) and not f4b.writes()
    print("  4b. body held on a NEW pair: no child page is created with a source-language body")

    # existing child, then source edit with the TITLE held: stored title kept, body updated
    f4c, s4c = _tenant()
    c4c = _mk_conn("Confl held title upd", {"target_locales": "es-MX"})
    _tr(c4c.id, f4c, page_ids=[s4c])
    child = f4c.by_title(T("es-MX"))[0]
    child["title"] = "[es-MX] Titulo humano"
    child["version"]["number"] += 1
    f4c.pages[s4c]["body"] = "<h1>Reset password</h1><p>Second text.</p>"
    f4c.pages[s4c]["version"]["number"] += 1
    undo = _hold(lambda sg: sg.order_index == 0)
    try:
        ok, msg, r = _tr(c4c.id, f4c, page_ids=[s4c])
    finally:
        undo()
    child = f4c.pages[child["id"]]
    assert child["title"] == "[es-MX] Titulo humano", ("held title keeps the stored one", child)
    assert "Second text" in child["body"] or "[es-mx]" in child["body"].lower(), child["body"]
    assert r["updated"] == 1 and r.get("partial_held") == 1, (msg, r)
    # #666: no new hash, and the OLDER one is dropped too (the child is now mixed)
    assert f"{s4c}:es-mx" not in _hashes(c4c.id), (
        "a partial pair records no new hash and drops its stale one",
        _hashes(c4c.id),
    )
    print("  4c. title held on an existing child: stored title kept, body written, no hash")

    # body unit held on an existing child: the stored body is kept (never mixed language)
    f4d, s4d = _tenant()
    c4d = _mk_conn("Confl held body upd", {"target_locales": "es-MX"})
    _tr(c4d.id, f4d, page_ids=[s4d])
    child = f4d.by_title(T("es-MX"))[0]
    stored_body = child["body"]
    f4d.pages[s4d]["body"] = "<h1>Reset password</h1><p>Third text.</p>"
    f4d.pages[s4d]["version"]["number"] += 1
    undo = _hold(lambda sg: sg.order_index == 1)
    try:
        ok, msg, r = _tr(c4d.id, f4d, page_ids=[s4d])
    finally:
        undo()
    child = f4d.pages[child["id"]]
    assert child["body"] == stored_body, ("held body keeps the stored body", child["body"])
    assert "Third text" not in child["body"], "source text must never ship"
    assert r.get("partial_held") == 1, (msg, r)
    # approved now: a normal run completes the pair and records the hash
    ok, msg, r = _tr(c4d.id, f4d, page_ids=[s4d])
    assert (
        r["updated"] == 1 and "Third text" in f4d.pages[child["id"]]["body"]
    ) or "[es-mx]" in f4d.pages[child["id"]]["body"].lower()
    assert _hashes(c4d.id)[f"{s4d}:es-mx"] == cbase.content_hash(
        "Reset password", f4d.pages[s4d]["body"]
    )
    print("  4d. body held on an existing child keeps the stored body; completes after approval")

    # =====================================================================
    # 5. id map fail-closed + orphan + unverifiable
    # =====================================================================
    f5, s5 = _tenant()
    human = f5.add_page("HELP", T("es-MX"), "<p>Hecho por una persona.</p>", parent=s5)
    sib = f5.add_page("HELP", "[de-DE] Something else", "<p>Humano.</p>", parent=s5)
    elsewhere = f5.add_page("HELP", T("fr-FR"), "<p>Humain.</p>")  # not a child
    c5 = _mk_conn("Confl collide", {"target_locales": "es-MX, de-DE, fr-FR"})
    ok, msg, r = _tr(c5.id, f5, page_ids=[s5])
    assert r["created"] == 0, (msg, r)
    for pid, body in (
        (human, "<p>Hecho por una persona.</p>"),
        (sib, "<p>Humano.</p>"),
        (elsewhere, "<p>Humain.</p>"),
    ):
        assert f5.pages[pid]["body"] == body and f5.pages[pid]["version"]["number"] == 1, pid
    assert not f5.writes("PUT") and not f5.writes("POST"), "a pre-existing page is never touched"
    st = idmap.status_counts(c5.id)
    assert st.get("needs_link") == 3, st
    assert "need a link decision" in msg, msg
    print(
        "  5a. human pages (exact title, locale tag, same title elsewhere) -> needs_link, untouched"
    )

    # trashed target -> orphaned and recreated
    f5b, s5b = _tenant()
    c5b = _mk_conn("Confl orphan", {"target_locales": "es-MX"})
    _tr(c5b.id, f5b, page_ids=[s5b])
    dead = f5b.by_title(T("es-MX"))[0]["id"]
    f5b.pages[dead]["status"] = "trashed"
    f5b.pages[s5b]["body"] = "<h1>Reset password</h1><p>After trash.</p>"
    f5b.pages[s5b]["version"]["number"] += 1
    ok, msg, r = _tr(c5b.id, f5b, page_ids=[s5b])
    new = f5b.by_title(T("es-MX"))
    assert r["created"] == 1 and len(new) == 1 and new[0]["id"] != dead, (msg, r)
    assert idmap.get(c5b.id, s5b, "es-mx").target_id == new[0]["id"]
    print("  5b. trashed target -> orphaned, a fresh page is created, map points at it")

    # unverifiable (503 on the target) -> hold, change nothing
    f5c, s5c = _tenant()
    c5c = _mk_conn("Confl unverifiable", {"target_locales": "es-MX"})
    _tr(c5c.id, f5c, page_ids=[s5c])
    kid = f5c.by_title(T("es-MX"))[0]["id"]
    f5c.pages[s5c]["body"] = "<h1>Reset password</h1><p>Edit.</p>"
    f5c.pages[s5c]["version"]["number"] += 1
    f5c.statuses[f"/pages/{kid}"] = 503
    nw = len(f5c.writes())
    ok, msg, r = _tr(c5c.id, f5c, page_ids=[s5c])
    del f5c.statuses[f"/pages/{kid}"]
    assert len(f5c.writes()) == nw and not ok, (msg, r)
    assert idmap.get(c5c.id, s5c, "es-mx").status == idmap.DONE, "row untouched on a 503"
    print("  5c. 503 while verifying the target: pair held, no orphan, no duplicate")

    # =====================================================================
    # 6. existing connections keep working (legacy state)
    # =====================================================================
    f6, s6 = _tenant()
    legacy_kid = f6.add_page("HELP", T("es-MX"), "<p>Antiguo.</p>", parent=s6)
    chash = cbase.content_hash("Reset password", SRC_BODY)
    c6 = _mk_conn("Confl legacy", {"target_locales": "es-MX"})
    cstore.patch_config_merge(c6.id, "confluence_children", {f"{s6}:es-MX": legacy_kid})
    cstore.patch_config_merge(c6.id, "confluence_hashes", {f"{s6}:es-MX": chash})
    # Review gate: the old loop could leave SOURCE text in a child it created while a unit was
    # held, and still recorded the hash. An adopted legacy hash is therefore invalidated: one
    # corrective rewrite (visible as legacy_resync), and a FAILED rewrite stays pending.
    f6.statuses[f"/pages/{legacy_kid}"] = 503
    ok, msg, r = _tr(c6.id, f6, page_ids=[s6])
    assert r["legacy_resync"] == 1 and r["skipped"] == 0 and not f6.writes(), (msg, r)
    assert "previous Confluence connector" in msg, msg
    assert idmap.get(c6.id, s6, "es-mx").target_id == legacy_kid
    assert _hashes(c6.id).get(f"{s6}:es-mx") == "legacy-resync", "failed rewrite stays pending"
    del f6.statuses[f"/pages/{legacy_kid}"]
    ok, msg, r = _tr(c6.id, f6, page_ids=[s6])
    assert ok and r["updated"] == 1 and r["created"] == 0 and r["legacy_resync"] == 0, (msg, r)
    assert f6.pages[legacy_kid]["version"]["number"] == 2, "rewrote the RECORDED legacy child"
    assert "Antiguo" not in f6.pages[legacy_kid]["body"], f6.pages[legacy_kid]["body"]
    assert _hashes(c6.id).get(f"{s6}:es-mx") == chash, "real hash recorded after the rewrite"
    ok, msg, r = _tr(c6.id, f6, page_ids=[s6])
    assert ok and r["skipped"] == 1 and r["updated"] == 0, ("then suppressed as usual", msg, r)
    f6.pages[s6]["body"] = "<h1>Reset password</h1><p>Changed.</p>"
    f6.pages[s6]["version"]["number"] += 1
    ok, msg, r = _tr(c6.id, f6, page_ids=[s6])
    assert r["updated"] == 1 and r["created"] == 0, (msg, r)
    assert f6.pages[legacy_kid]["version"]["number"] == 3, (
        "updated the RECORDED legacy child in place"
    )
    print("  6. legacy children adopted: one corrective rewrite (retried if it fails), then normal")

    # #666: a pair written PARTLY (a held unit) drops its stale hash AND the legacy
    # mixed-case alias of it ("<id>:es-MX", still holding the v1 hash), so a source edit back
    # to exactly v1 rewrites the page instead of being skipped as done.
    assert _hashes(c6.id).get(f"{s6}:es-MX") == chash, "legacy alias still present"
    f6.pages[s6]["body"] = SRC_BODY
    f6.pages[s6]["version"]["number"] += 1
    ok, msg, r = _tr(c6.id, f6, page_ids=[s6])
    assert r["updated"] == 1 and _hashes(c6.id).get(f"{s6}:es-mx") == chash, (msg, r)
    f6.pages[s6]["body"] = "<h1>Reset password</h1><p>Totally new text.</p>"
    f6.pages[s6]["version"]["number"] += 1
    undo = _hold(lambda sg: sg.order_index == 2)
    try:
        ok, msg, r = _tr(c6.id, f6, page_ids=[s6])
    finally:
        undo()
    assert r["updated"] == 1, (msg, r)
    hs6 = _hashes(c6.id)
    assert f"{s6}:es-mx" not in hs6 and f"{s6}:es-MX" not in hs6, ("#666 stale hash", hs6)
    f6.pages[s6]["body"] = SRC_BODY
    f6.pages[s6]["version"]["number"] += 1
    ok, msg, r = _tr(c6.id, f6, page_ids=[s6])
    assert r["updated"] == 1 and r["skipped"] == 0, ("#666: revert rewrites the page", msg, r)
    assert _hashes(c6.id).get(f"{s6}:es-mx") == chash
    print("  6b. #666: a partly written pair drops its stale hash and its legacy alias")

    # =====================================================================
    # 7. scope / path escape
    # =====================================================================
    f7, s7 = _tenant()
    sec7 = f7.add_page("OTHER", "Secret page", "<p>Do not translate.</p>", labels=["secret"])
    cf = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net", "cql": 'space = "HELP" AND label = "kb"'},
        {"email": "b@x.com", "api_token": TOKEN},
        transport=f7.transport(),
    )
    got = cf.discover_articles(cql="title ~ 'Reset'")
    assert f7.cql_seen[-1] == '(title ~ \'Reset\') AND (space = "HELP" AND label = "kb")', (
        f7.cql_seen
    )
    assert [p["id"] for p in got] == [s7], got
    _raises(lambda: cf.discover_articles(cql="a) OR (space = OTHER"), "parenthes")
    _raises(lambda: cf.discover_articles(cql="title ~ 'oops"), "quote")
    n = len(f7.log)
    _raises(lambda: cf.discover_articles(article_ids=[s7]), "cql filter with no space key")
    assert len(f7.log) == n, "refused before any request"
    # space scope: out-of-scope page ids are dropped, caller space_key is ignored
    cs = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net", "space_key": "HELP"},
        {"email": "b@x.com", "api_token": TOKEN},
        transport=f7.transport(),
    )
    assert [p["id"] for p in cs.discover_articles(article_ids=[sec7, s7])] == [s7]
    ids = [p["id"] for p in cs.discover_articles(space_key="OTHER")]
    assert sec7 not in ids and s7 in ids, ("scope space wins over a caller space_key", ids)
    # no scope at all: the caller space_key stands; neither -> refused
    cn = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": TOKEN},
        transport=f7.transport(),
    )
    assert [p["id"] for p in cn.discover_articles(space_key="OTHER")] == [sec7]
    _raises(lambda: cn.discover_articles(), "Provide a space key")
    # path escape: ids and keys are validated BEFORE any request
    n = len(f7.log)
    for bad in (
        "../../admin",
        "12/../34",
        "%2e%2e%2f1",
        "1?x=1",
        "1#f",
        "12\n34",
        "",
        "1" * 40,
        "٣",
    ):
        _raises(lambda b=bad: cs.discover_articles(article_ids=[b]), "page id")
    for bad in ("HELP/../X", "A B", "K\x00", "a" * 300, "k?x"):
        _raises(lambda b=bad: cn.discover_articles(space_key=b), "space key")
    assert len(f7.log) == n, "invalid ids/keys never reach the wire"
    # the configured scope space itself is validated too
    cb = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net", "space_key": "../x"},
        {"email": "b@x.com", "api_token": TOKEN},
        transport=f7.transport(),
    )
    _raises(lambda: cb.discover_articles(), "space key")
    # review: BOTH a scope space and a scope CQL declared -> both hold (the scope CQL wins inside
    # the query, so the space is re-checked on each fetched page)
    f7.add_page("OTHER", "Other kb page", "<p>x</p>", labels=["kb"])
    cboth = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net", "space_key": "HELP", "cql": 'label = "kb"'},
        {"email": "b@x.com", "api_token": TOKEN},
        transport=f7.transport(),
    )
    got = cboth.discover_articles()
    assert [p["id"] for p in got] == [s7], ("space + cql scope: nothing from OTHER", got)
    # a translate run with an out-of-scope id writes nothing
    c7 = _mk_conn("Confl scope run")
    ok, msg, r = _tr(c7.id, f7, page_ids=[sec7])
    assert not f7.writes(), "out-of-scope page id must not be translated"
    print("  7. scope: CQL ANDed, parens refused, cql-only + ids refused, bad ids never sent")

    # =====================================================================
    # 8. pagination + write budget
    # =====================================================================
    f8 = FakeConfluence(page_size=40)
    f8.add_space("HELP", "Help")
    for i in range(130):
        f8.add_page("HELP", f"Doc {i:03d}", f"<p>Body {i}.</p>")
    c8 = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net", "space_key": "HELP", "max_pages": "200"},
        {"email": "b@x.com", "api_token": TOKEN},
        transport=f8.transport(),
    )
    got = c8.discover_articles()
    assert len(got) == 130, len(got)
    cursors = [
        e["q"].get("cursor")
        for e in f8.log
        if e["path"].endswith("/pages") and "spaces" in e["path"]
    ]
    assert len([c for c in cursors if c]) >= 3, ("followed the cursor", cursors)
    assert all(
        e["raw"].startswith("/wiki/api/v2") or e["raw"].startswith("/wiki/rest") for e in f8.log
    )
    # children pagination in the collision probe: tag collision on child #75
    f8b, s8b = _tenant(page_size=30)
    for i in range(80):
        f8b.add_page("HELP", f"Unrelated child {i}", "<p>x</p>", parent=s8b)
    f8b.add_page("HELP", "[es-MX] Human sibling", "<p>h</p>", parent=s8b)
    c8b = _mk_conn("Confl paged children", {"target_locales": "es-MX"})
    ok, msg, r = _tr(c8b.id, f8b, page_ids=[s8b])
    assert idmap.status_counts(c8b.id).get("needs_link") == 1 and r["created"] == 0, (msg, r)
    print("  8a. cursor pagination followed for the space listing and the children probe")
    f8c, _s8c = _tenant()
    f8c.add_page("HELP", "Second doc", "<p>Another.</p>")
    c8c = _mk_conn("Confl budget", {"target_locales": "es-MX, de-DE"})
    old = config.MAX_WRITES_PER_RUN
    config.MAX_WRITES_PER_RUN = 2
    try:
        ok, msg, r = _tr(c8c.id, f8c)
    finally:
        config.MAX_WRITES_PER_RUN = old
    assert r["created"] == 2 and r.get("write_ceiling_hit", True), (msg, r)
    ok, msg, r = _tr(c8c.id, f8c)
    assert r["created"] >= 1, "the next run picks up what the ceiling cut"
    print("  8b. write budget: ceiling stops cleanly, next run continues")

    # =====================================================================
    # 9. failures
    # =====================================================================
    f9, s9 = _tenant()
    f9.add_page("HELP", T("es-MX"), "<p>colide</p>", parent=None)  # title clash in space
    c9 = _mk_conn("Confl clash", {"target_locales": "es-MX"})
    ok, msg, r = _tr(c9.id, f9, page_ids=[s9])
    assert not ok and r["created"] == 0 and not _hashes(c9.id), (msg, r)
    f9b, s9b = _tenant()
    c9b = _mk_conn("Confl 5xx", {"target_locales": "es-MX"})
    ok, msg, r = _tr(c9b.id, f9b, page_ids=[s9b])
    assert ok and r["created"] == 1, (msg, r)
    kid9 = idmap.get(c9b.id, s9b, "es-mx").target_id
    full9 = f9b.pages[kid9]["body"]
    assert full9.strip(), "the created child already holds the full translation"
    # A `building` row whose page differs from the payload (e.g. Confluence normalised the
    # storage XHTML) resumes with a PUT; a 5xx there is NOT resent and the page that readers
    # see is still the complete translation from the create, never an empty page.
    idmap.set_status(c9b.id, s9b, "es-mx", idmap.BUILDING)
    f9b.pages[kid9]["body"] = full9 + "<p></p>"
    old_h = _hashes(c9b.id).get(f"{s9b}:es-mx")
    f9b.pages[s9b]["body"] = "<h1>Reset password</h1><p>Click the NEW link.</p>"
    f9b.pages[s9b]["version"]["number"] += 1
    f9b.fail_put = 1
    n_puts = len(f9b.writes("PUT"))
    ok, msg, r = _tr(c9b.id, f9b, page_ids=[s9b])
    puts = f9b.writes("PUT")[n_puts:]
    assert len(puts) == 1, ("a 5xx on the fill is NOT resent", len(puts))
    assert r["failures"] and _hashes(c9b.id).get(f"{s9b}:es-mx") == old_h, (msg, r)
    assert f9b.pages[kid9]["body"].startswith(full9), "failed fill leaves the full page"
    row = idmap.get(c9b.id, s9b, "es-mx")
    assert row.status == idmap.BUILDING, "the reserved page stays resumable"
    n_pages = len(f9b.pages)
    ok, msg, r = cstore.retry_failed(
        c9b.id, r["failures"], actor="admin", transport=f9b.transport()
    )
    assert ok and len(f9b.pages) == n_pages, "retry resumes the reserved page, no duplicate"
    assert idmap.get(c9b.id, s9b, "es-mx").status == idmap.DONE
    assert _hashes(c9b.id).get(f"{s9b}:es-mx") != old_h and "NEW" in f9b.pages[kid9]["body"]
    print("  9a. title clash fails closed; a 5xx fill is not resent; retry resumes, no duplicate")
    f9c, s9c = _tenant()
    c9c = _mk_conn("Confl 429", {"target_locales": "es-MX"})
    f9c.throttle = 2
    ok, msg, r = _tr(c9c.id, f9c, page_ids=[s9c])
    assert ok and r["created"] == 1, (msg, r)
    f9c.fail_put = 0
    f9d, s9d = _tenant()
    c9d = _mk_conn("Confl 409", {"target_locales": "es-MX"})
    ok, msg, r = _tr(c9d.id, f9d, page_ids=[s9d])
    assert ok and r["created"] == 1, (msg, r)
    f9d.pages[s9d]["body"] = "<h1>Reset password</h1><p>Click the NEW link.</p>"
    f9d.pages[s9d]["version"]["number"] += 1
    f9d.conflict_once = True
    ok, msg, r = _tr(c9d.id, f9d, page_ids=[s9d])
    assert ok and r["updated"] == 1, ("a 409 refetches the version and retries once", msg, r)
    # token never in any message, summary, log
    blob = json.dumps([msg, r]) + json.dumps([(x.message or "") for x in _synclogs(c9b.id)])
    assert TOKEN not in blob
    print("  9b. 429 backoff and a 409 version retry succeed; the token appears nowhere")

    # =====================================================================
    # 10. OAuth gateway
    # =====================================================================
    f10, _s10 = _tenant()
    sec = {"auth_mode": "oauth", "oauth": {"cloudid": "cloud-MATCH", "access_token": "AT-1"}}
    c10 = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net", "space_key": "HELP", "cql": "type = page"},
        sec,
        transport=f10.transport(),
    )
    ok, m = c10.test()
    assert ok, m
    c10.discover_articles()
    e0 = f10.log[0]
    assert e0["gateway"] and e0["raw"] == "/ex/confluence/cloud-MATCH/wiki/api/v2/spaces", e0
    assert e0["auth"] == "bearer", e0
    c10.discover_articles(cql="title ~ 'Reset'")
    v1 = [e for e in f10.log if e["api"] == "v1"]
    assert v1 and v1[-1]["raw"] == "/ex/confluence/cloud-MATCH/rest/api/search", v1
    assert "/wiki/" not in v1[-1]["raw"], "the v1 gateway path has NO /wiki"
    bad = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net"},
        {"auth_mode": "oauth", "oauth": {"cloudid": "../evil"}},
        transport=f10.transport(),
    )
    assert bad.test()[0] is False
    # Review gate: a pre-rebuild OAuth token (classic scopes only) gets 401 from v2. Every
    # surface says "reconnect", never a bare 401; token mode and a plain 403 are unaffected.
    f10.statuses["/spaces"] = 401
    ok, m = c10.test()
    assert not ok and "Reconnect with OAuth" in m and "(401)" in m, m
    c10b = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net", "space_key": "HELP"},
        sec,
        transport=f10.transport(),
    )
    _raises(lambda: c10b.discover_articles(), "Reconnect with OAuth")
    ctok = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net", "space_key": "HELP"},
        {"email": "b@x.com", "api_token": TOKEN},
        transport=f10.transport(),
    )
    ok, m = ctok.test()
    assert not ok and "Reconnect with OAuth" not in m, m
    del f10.statuses["/spaces"]
    f10.statuses[f"/pages/{_s10}"] = 403
    try:
        c10.discover_articles(article_ids=[_s10])
    except ConnectorError as e:
        assert "Reconnect with OAuth" not in str(e), e
    del f10.statuses[f"/pages/{_s10}"]
    print("  10. OAuth: v2 under /ex/confluence/{cloudid}/wiki/api/v2, v1 search no /wiki, Bearer")

    # =====================================================================
    # 11. storage-format output stays text
    # =====================================================================
    from unittest.mock import patch

    def malicious_translation(session, doc_id, loc):
        for segment in session.exec(select(Segment).where(Segment.document_id == doc_id)):
            if segment.target_locale == loc:
                segment.target_text = 'Hola <img src=x onerror="alert(1)"> mundo'
                segment.status = SegStatus.APPROVED
                session.add(segment)
        session.commit()

    f11 = FakeConfluence()
    f11.add_space("HELP", "Help")
    s11 = f11.add_page("HELP", "Page", '<div title="a > b">Hello world</div>')
    c11 = _mk_conn("Confl escape", {"target_locales": "es-MX"})
    with patch("app.services.translate.pretranslate_document", malicious_translation):
        ok, msg, r = _tr(c11.id, f11, page_ids=[s11])
    assert ok, msg
    child = next(p for p in f11.pages.values() if p["parentId"] == s11)["body"]
    assert "<img" not in child and "&lt;img" in child, child
    assert child.startswith('<div title="a > b">Hola &lt;img'), child
    print("  11. approved text stays text in the storage body")

    # =====================================================================
    # 3. WEBHOOK INTAKE — signed page-updated -> targeted re-fetch/enqueue
    # =====================================================================
    assert webhooks.supports_webhook("confluence"), "confluence webhook verifier not registered"
    secret = "conf-webhook-secret"
    connw = _mk_conn("Harden webhook", secret_extra={"webhook_secret": secret})

    def sign(body: bytes) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    payload = {"page": {"id": "100", "version": {"number": 9}}, "eventType": "page_updated"}
    body = json.dumps(payload).encode()
    enq = []

    def _fake_enqueue(cid, kind, record_ids):
        enq.append((cid, kind, list(record_ids)))
        return 909

    # (a) valid signature -> accepted + enqueued once, THIN payload -> page id only
    hdrs = {"X-Hub-Signature-256": sign(body), "X-Hub-Delivery": "d-1"}
    res = webhooks.handle_intake(connw.id, body, hdrs, enqueue=_fake_enqueue)
    assert res.status == 200 and res.enqueued and res.job_id == 909, res.as_dict()
    assert res.record_ids == ["100"], ("thin payload -> page id only", res.record_ids)
    assert enq == [(connw.id, "confluence", ["100"])], enq
    print("  6. webhook: valid signature -> accepted, enqueued once, page id from thin payload")

    # (b) provider retry of the SAME delivery id -> ACK 200, enqueue nothing
    res2 = webhooks.handle_intake(connw.id, body, hdrs, enqueue=_fake_enqueue)
    assert res2.status == 200 and not res2.enqueued, ("retry deduped", res2.as_dict())
    assert len(enq) == 1, ("no second enqueue on retry", enq)
    with Session(engine) as s:
        rows = s.exec(select(WebhookEvent).where(WebhookEvent.connection_id == connw.id)).all()
    assert len(rows) == 1, ("exactly one ledger row", rows)
    print("  7. webhook: retry of same delivery -> 200 ACK, deduped (1 ledger row)")

    # (c) BAD signature -> 401, nothing enqueued
    bad = webhooks.handle_intake(
        connw.id,
        body,
        {"X-Hub-Signature-256": "sha256=deadbeef", "X-Hub-Delivery": "d-2"},
        enqueue=_fake_enqueue,
    )
    assert bad.status == 401 and not bad.enqueued, ("bad signature -> 401", bad.as_dict())
    assert len(enq) == 1, ("bad-sig delivery never enqueues", enq)
    print("  8. webhook: bad signature -> 401, nothing enqueued")

    # (d) a DIFFERENT edit of the same page (new version) is NOT deduped against (a)
    payload_v2 = {"page": {"id": "100", "version": {"number": 10}}, "eventType": "page_updated"}
    body_v2 = json.dumps(payload_v2).encode()
    res3 = webhooks.handle_intake(
        connw.id, body_v2, {"X-Hub-Signature-256": sign(body_v2)}, enqueue=_fake_enqueue
    )
    assert res3.status == 200 and res3.enqueued, ("distinct edit re-enqueues", res3.as_dict())
    assert enq[-1] == (connw.id, "confluence", ["100"]), enq
    print(
        "  9. webhook: a DIFFERENT edit (new version, no delivery header) re-enqueues (not deduped)"
    )

    # =====================================================================
    # F2: versionless webhook payload -> event key falls through to body-SHA
    # =====================================================================
    # No version.number anywhere and no delivery header -> None (was the bare page
    # id pre-fix, which collapsed two distinct edits into one dedup key).
    assert _confluence_event_key({"page": {"id": "100"}, "eventType": "page_updated"}) is None, (
        "versionless payload must return None so dedup_key() falls to body-SHA"
    )
    assert _confluence_event_key({}) is None
    # a version-bearing payload is UNCHANGED (still a strong page:version key)
    assert _confluence_event_key({"page": {"id": "100", "version": {"number": 3}}}) == "100:3"
    print("  10. _confluence_event_key: versionless payload -> None (not bare page id)")

    connv = _mk_conn("Harden versionless webhook", secret_extra={"webhook_secret": secret})
    body_a = json.dumps(
        {"page": {"id": "100"}, "eventType": "page_updated", "body": "first edit"}
    ).encode()
    body_b = json.dumps(
        {"page": {"id": "100"}, "eventType": "page_updated", "body": "second, DIFFERENT edit"}
    ).encode()
    res_a = webhooks.handle_intake(
        connv.id, body_a, {"X-Hub-Signature-256": sign(body_a)}, enqueue=_fake_enqueue
    )
    res_b = webhooks.handle_intake(
        connv.id, body_b, {"X-Hub-Signature-256": sign(body_b)}, enqueue=_fake_enqueue
    )
    assert res_a.status == 200 and res_a.enqueued, res_a.as_dict()
    assert res_b.status == 200 and res_b.enqueued, (
        "two DISTINCT versionless bodies must BOTH enqueue (F2 fix)",
        res_b.as_dict(),
    )
    # an identical-body REPLAY of (a) still dedups (true retry, same SHA)
    res_a_replay = webhooks.handle_intake(
        connv.id, body_a, {"X-Hub-Signature-256": sign(body_a)}, enqueue=_fake_enqueue
    )
    assert res_a_replay.status == 200 and not res_a_replay.enqueued, (
        "identical body replay must still dedup",
        res_a_replay.as_dict(),
    )
    print("  11. webhook: two distinct versionless bodies -> two enqueues; identical replay -> one")

    # =====================================================================
    # F3 (Phase 3B): harvested Confluence context is capped (deep ancestor chain,
    # many labels) by _CONF_MAX_ANCESTORS + validate_and_sanitize's SHORT_CAP/LABELS_MAX
    # =====================================================================
    from app.services import context_pkg
    from app.services.connectors import harvest

    deep_page = {
        "title": "Deep page",
        "space": "Customer Help Center",
        "ancestors": [f"Ancestor {i}" for i in range(500)],
        "labels": [f"label{i}" for i in range(50)],
    }
    ctx = harvest.build("confluence", deep_page, {})
    assert ctx is not None
    section = ctx["source"]["section"]
    # section is hard-capped at SHORT_CAP; only the LAST _CONF_MAX_ANCESTORS ancestors
    # (closest to the page) survive — the far ones are dropped before the cap.
    assert len(section) <= context_pkg.SHORT_CAP, (len(section), context_pkg.SHORT_CAP)
    assert "Ancestor 499" in section and "Ancestor 495" in section, section
    assert "Ancestor 0" not in section, ("far ancestor must be dropped", section)
    # labels are count-capped by the same ingestion contract.
    assert len(ctx["source"]["labels"]) == context_pkg.LABELS_MAX, ctx["source"]["labels"]
    print(
        f"  12. harvested context: 500-ancestor chain -> section capped to {len(section)} "
        f"chars (<= {context_pkg.SHORT_CAP}), labels -> {context_pkg.LABELS_MAX}"
    )

    # =====================================================================
    # 13. surfaces
    # =====================================================================
    from app.services.connectors import REGISTRY
    from app.services.connectors import confluence as cmod

    assert cmod.SPEC.archived is False and REGISTRY["confluence"] is ConfluenceConnector
    assert ConfluenceConnector.capability.topology == "T3"
    print("  13. un-archived, T3 capability, registered")
    print("verify_confluence_harden OK")


if __name__ == "__main__":
    main()
