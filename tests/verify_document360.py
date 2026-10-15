"""Document360 connector + connector-framework seams - fully offline via
httpx.MockTransport (a fake Document360 project that follows the CURRENT v2 docs, see
tests/_flow_parity_fixtures.FakeD360).

Covers:
  - connector: test(), enabled_locales() (/Language/{pv}), discover (project version + category
    scope, 0-based pages), editor-type detection (content_type), scope and id validation
  - extract: title + body via the EDITOR-APPROPRIATE filter, merge-code shielding
  - write-back: CREATE (language has no real version: 400 / 404 / null / fallback all mean
    absent) and UPDATE (partial PUT), body always in `content`, never a `publish` key
  - merge-code shielding round-trip and the write-side debris strip
  - poll change detection: content-hash suppression, translation_option stale flag
  - language-availability handling, 429 backoff, org scoping, CLI, retry
  - HELD UNITS (#640): a held field is omitted (keeps its stored value), never a source stand-in,
    a held pair is never created and records no content hash
  - DRAFT SAFETY: a published translation is never edited in place (a draft is forked from it,
    or the pair is reported as kept-published), publish is a POST with user_id + version_number
  - regional hosts: a closed set, a hostile region is refused; the token never reaches a log

ASCII-only prints; final line is 'verify_document360 OK'.
"""

import json
import logging
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_d360_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Connection, Project, Segment, SegStatus, SyncLog, TargetLocale
from app.services import connectors as _reg  # archive-tier spec introspection
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.document360 import (
    _SHIELD_CLOSE,
    _SHIELD_OPEN,
    Document360Connector,
    _editor_of,
    shield_codes,
    strip_write_egress_debris,
    unshield_codes,
)
from app.services.connectors.strategy import ABSENT, SubResourceStrategy
from tests._flow_parity_fixtures import (
    D360_PV,
    D360_USER,
    FakeD360,
    _d360_conn,
    _d360_html_article,
    _d360_md_article,
)

_md_article = _d360_md_article
_html_article = _d360_html_article
_dconn = _d360_conn


def _connector(fz, **cfg):
    base = {"source_lang": "en", "project_version_id": D360_PV}
    base.update(cfg)
    return Document360Connector(base, {"api_token": "t"}, transport=fz.transport())


def _hashes(conn_id):
    return (cstore.get(conn_id).config or {}).get("d360_hashes") or {}


def _flip_review(orders, locale="de"):
    """Patch pretranslate_document so the given segment order_index values of `locale` come back
    NEEDS_REVIEW (a held unit). Returns (patch, restore)."""
    import app.services.translate as translate_mod

    real = translate_mod.pretranslate_document

    def _ptd(session, doc_id, loc, spec=None, force_pii=False):
        stats = real(session, doc_id, loc, spec, force_pii=force_pii)
        if loc == locale:
            for seg in session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id,
                    Segment.target_locale == loc,
                    Segment.order_index.in_(list(orders)),
                )
            ).all():
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
            session.commit()
        return stats

    set_pieces("app.services.translate", "pretranslate_document", _ptd)
    return lambda: set_pieces("app.services.translate", "pretranslate_document", real)


def main() -> None:
    init_db()

    # ---- (0) framework seam: SubResourceStrategy + capability block ----
    strat = SubResourceStrategy()
    assert strat.topology == "T2"
    assert Document360Connector.capability.topology == "T2"
    assert Document360Connector.capability.change_signals.webhook_sig is None, (
        "Document360 has NO webhook (poll-only)"
    )
    assert not _reg.is_archived("document360"), "rebuilt connector is un-archived"
    print("  framework: T2 strategy + capability declares poll-only (no webhook), un-archived")

    # ---- (1) shielding round-trip (byte-identical merge codes) ----
    src = "Hi {{user.name}}, see {{snippet:a}} and {{x}}{{y}}."
    sh, codes = shield_codes(src)
    assert "{{" not in sh, ("all codes shielded", sh)
    import re as _re

    mt = _re.sub(r"[A-Za-z]+", lambda m: m.group(0).upper(), sh)
    restored = unshield_codes(mt, codes)
    for c in codes:
        assert c in restored, ("merge code survived byte-identical", c, restored)
    assert restored.count("{{") == 4, restored
    print("  shielding: 4 merge codes round-trip byte-identical through a mangling MT")

    # ---- (2) editor detection: documented content_type wins, else the populated field ----
    assert _editor_of({"content_type": "markdown", "html_content": "<p>x</p>"}) == "markdown"
    assert _editor_of({"content_type": "wysiwyg", "content": "<p>x</p>"}) == "html"
    assert _editor_of({"content_type": 1, "content": "<p>x</p>"}) == "html"
    assert _editor_of({"content_type": "block", "content": ""}) == "block"
    assert _editor_of({"content": "# x"}) == "markdown"
    assert _editor_of({"html_content": "<p>x</p>"}) == "html"
    assert _editor_of({}) == "markdown"
    print("  editor detection: content_type (markdown/wysiwyg/block, 0/1/2) then field fallback")

    # ---- (3) connector: test + enabled_locales + discover ----
    fz = FakeD360(articles=[_md_article("100"), _html_article("101")])
    dc = _connector(fz)
    ok, msg = dc.test()
    assert ok, msg
    assert set(dc.enabled_locales()) == {"en", "de", "fr"}, dc.enabled_locales()
    arts = dc.discover_articles()
    assert sorted(a["id"] for a in arts) == ["100", "101"], [a["id"] for a in arts]
    src_gets = [p for p, q in fz.gets if p.startswith("/v2/Articles/")]
    assert src_gets and all(
        q.get("isForDisplay") == "false" and q.get("appendSASToken") == "false"
        for p, q in fz.gets
        if p.startswith("/v2/Articles/")
    ), "source read is raw (no merge codes resolved, no SAS tokens)"
    ok_bad, msg_bad = _connector(fz, project_version_id="not-there").test()
    assert not ok_bad and "not in this project" in msg_bad, msg_bad
    print("  connector: test + enabled_locales(3) + discover(2) raw source read; bad pv rejected")

    # ---- (3b) listing pages: 0-based pages of the flat list, has_more until done ----
    fzp = FakeD360(articles=[_md_article(str(i)) for i in range(300, 305)])
    fzp.page_size_cap = 2
    got = _connector(fzp).discover_articles()
    assert sorted(a["id"] for a in got) == [str(i) for i in range(300, 305)], got
    pages = [q.get("page") for p, q in fzp.gets if p.endswith("/articles")]
    assert pages == ["0", "1", "2"], ("0-based pages until has_more is false", pages)
    print("  pagination: 5 articles over pages 0,1,2 (hitsPerPage capped at 2 by the server)")

    # ---- (3c) scope: project version, category subtree, explicit ids, id validation ----
    other = _md_article("999")
    other["project_version_id"] = "pv-other"
    fzs = FakeD360(articles=[_md_article("100"), _md_article("101"), _md_article("102"), other])
    fzs.categories = [
        {
            "id": "catA",
            "articles": [{"id": "100"}],
            "child_categories": [{"id": "catA1", "articles": [{"id": "101"}]}],
        },
        {"id": "catB", "articles": [{"id": "102"}], "child_categories": []},
    ]
    listed = sorted(a["id"] for a in _connector(fzs).discover_articles())
    assert listed == ["100", "101", "102"], (
        "an article of another project version is dropped",
        listed,
    )
    in_cat = sorted(a["id"] for a in _connector(fzs, category_id="catA").discover_articles())
    assert in_cat == ["100", "101"], ("category + sub-category only", in_cat)
    by_id = _connector(fzs, category_id="catA").discover_articles(article_ids=["101", "102", "999"])
    assert [a["id"] for a in by_id] == ["101"], ("ids outside the category are dropped", by_id)
    by_id2 = _connector(fzs).discover_articles(article_ids=["999", "100"])
    assert [a["id"] for a in by_id2] == ["100"], ("id of another project version dropped", by_id2)
    try:
        _connector(fzs, category_id="catZ").discover_articles()
        raise AssertionError("an unknown category must fail the run")
    except ConnectorError:
        pass
    try:
        _connector(fzs, project_version_id="").discover_articles()
        raise AssertionError("a blank project version must fail the run")
    except ConnectorError:
        pass
    n_before = len(fzs.gets)
    for bad in ("../x", "a/b", "%2e%2e", "x?y=1", "a b", "id\r\nX: 1", "١٢", ""):
        for fn in (
            lambda b=bad: _connector(fzs).discover_articles(article_ids=[b]),
            lambda b=bad: _connector(fzs).get_translation(b, "de"),
            lambda b=bad: _connector(fzs).get_translation("100", b),
        ):
            try:
                fn()
                raise AssertionError(("unsafe id/langCode reached the wire", bad))
            except ConnectorError:
                pass
    assert len(fzs.gets) == n_before, "an invalid id or langCode never produced a request"
    print("  scope: pv + category subtree enforced, foreign ids dropped, unsafe ids refused")

    # ---- (3d) regional hosts: closed set, hostile region refused, token only to vendor hosts ----
    hosts = []

    def _record(req):
        hosts.append(req.url.host)
        return httpx.Response(200, json={"success": True, "data": []})

    for region, host in (
        ("", "apihub.document360.io"),
        ("global", "apihub.document360.io"),
        ("us", "apihub.us.document360.io"),
        ("ca", "apihub.ca.document360.io"),
    ):
        Document360Connector(
            {"region": region, "project_version_id": "pv"},
            {"api_token": "t"},
            transport=httpx.MockTransport(_record),
        ).test()
        assert hosts[-1] == host, (region, hosts[-1])
    nreq = len(hosts)
    ok_h, msg_h = Document360Connector(
        {"region": "attacker.example", "project_version_id": "pv"},
        {"api_token": "t"},
        transport=httpx.MockTransport(_record),
    ).test()
    assert not ok_h and len(hosts) == nreq, ("hostile region sent nothing", msg_h, hosts)
    print("  regions: global/us/ca map to the vendor hosts; a hostile region is refused")

    # ---- (4) extract: editor-appropriate filter + shielded units ----
    from app.filters import get_filter

    units_md, _h_md, ed_md = dc.extract_units(_md_article("100"), get_filter)
    assert ed_md == "markdown", ed_md
    assert units_md[0]["field"] == "title", units_md[0]
    assert "{{" not in units_md[0]["text"], units_md[0]
    _units_html, _h_html, ed_html = dc.extract_units(_html_article("101"), get_filter)
    assert ed_html == "html", ed_html
    print("  extract: markdown article -> md filter, wysiwyg article -> html filter (shielded)")

    # ---- (5) write-back CREATE (Markdown), every way a missing language can answer ----
    for mode in ("400", "404", "null", "fallback"):
        fz1 = FakeD360(articles=[_md_article("100")], missing_mode=mode)
        conn = _dconn(f"D360 create md {mode}")
        ok, msg, summary = cstore.document360_translate(
            conn.id, actor="auto", transport=fz1.transport()
        )
        assert ok, (mode, msg)
        assert summary["created"] == 2 and summary["updated"] == 0, (mode, summary)
        assert {lang for _a, lang, _b in fz1.writes} == {"de", "fr"}, fz1.writes
        for _a, lang, body in fz1.writes:  # noqa: B007
            assert "content" in body and "html_content" not in body, ("content field", body)
            assert "publish" not in body, ("`publish` is not a PUT field", body)
            assert body["translation_option"] == "2", ("marked Translated", body)
            assert (
                "{{glossary:pw}}" in body["content"] and "{{snippet:reset}}" in body["content"]
            ), (
                "merge codes byte-identical in write-back",
                body["content"],
            )
        assert not fz1.publishes and not fz1.forks and not fz1.live_edits
    print(
        "  write-back CREATE: 400/404/null/fallback = absent -> 2 langCodes PUT `content`, option 2"
    )

    # ---- (6) both editor types in ONE run: each writes the documented `content` field ----
    fz2 = FakeD360(articles=[_md_article("100"), _html_article("101")])
    conn2 = _dconn("D360 mixed", {"target_locales": "de"})
    ok, msg, _s2 = cstore.document360_translate(conn2.id, actor="auto", transport=fz2.transport())
    assert ok, msg
    by_art = {aid: body for aid, _lang, body in fz2.writes}
    for aid in ("100", "101"):
        assert "content" in by_art[aid] and "html_content" not in by_art[aid], by_art[aid]
    assert "{{var:plan}}" in by_art["101"]["content"], ("html merge code survived", by_art["101"])
    assert "<h1>" in by_art["101"]["content"], ("html structure kept", by_art["101"]["content"])
    print(
        "  branching: md and wysiwyg articles both write `content`, html structure and codes kept"
    )

    # ---- (7) write-back UPDATE of an existing DRAFT: PUT with its version, no fork ----
    fz3 = FakeD360(articles=[_md_article("100")])
    fz3.add_version("100", "de", title="Alt", content="# alt")  # de draft already exists
    conn3 = _dconn("D360 update")
    ok, msg, s3 = cstore.document360_translate(conn3.id, actor="auto", transport=fz3.transport())
    assert ok, msg
    assert s3["created"] == 1 and s3["updated"] == 1, s3  # fr created, de updated
    de_put = next(b for _a, lang, b in fz3.writes if lang == "de")
    assert de_put["version_number"] == 1, ("edits the draft it probed", de_put)
    assert not fz3.forks and not fz3.live_edits, "a draft is edited in place, nothing forked"
    print("  write-back UPDATE: existing draft de updated (its version_number) + fr created")

    # ---- (8) content-hash suppression (poll change detection) ----
    art_v1 = _md_article("200", version_number=1)
    fz4 = FakeD360(articles=[art_v1])
    conn4 = _dconn("D360 suppress")
    ok, _m, s4a = cstore.document360_translate(conn4.id, actor="auto", transport=fz4.transport())
    assert s4a["created"] == 2, s4a
    fz5 = FakeD360(articles=[art_v1])
    fz5.add_version("200", "de")
    fz5.add_version("200", "fr")
    ok, _m, s4b = cstore.document360_translate(conn4.id, actor="auto", transport=fz5.transport())
    assert ok and s4b["skipped"] == 2 and s4b["created"] == 0 and s4b["updated"] == 0, s4b
    assert not fz5.writes, ("no writes on unchanged content", fz5.writes)
    art_v2 = _md_article(
        "200", content="# Reset {{glossary:pw}}\n\nBrand new steps here.", version_number=2
    )
    fz6 = FakeD360(articles=[art_v2])
    fz6.add_version("200", "de")
    fz6.add_version("200", "fr")
    ok, _m, s4c = cstore.document360_translate(conn4.id, actor="auto", transport=fz6.transport())
    assert ok and s4c["updated"] == 2 and s4c["skipped"] == 0, ("changed -> re-translate", s4c)
    print("  poll suppression: unchanged source -> 2 skipped/0 writes; changed source -> 2 updated")

    # ---- (9) language-availability handling (target not enabled -> dropped) ----
    fz7 = FakeD360(enabled_langs=("en", "de"), articles=[_md_article("100")])
    conn7 = _dconn("D360 reject", {"target_locales": "de, fr, ja"})
    ok, msg, s7 = cstore.document360_translate(conn7.id, actor="auto", transport=fz7.transport())
    assert ok, msg
    assert s7["locales"] == ["de"] and set(s7["rejected"]) == {"fr", "ja"}, s7
    assert {lang for _a, lang, _b in fz7.writes} == {"de"}, ("only de written", fz7.writes)
    assert "not enabled in the project" in msg, msg
    fz7b = FakeD360(enabled_langs=("en",), articles=[_md_article("100")])
    conn7b = _dconn("D360 all-reject", {"target_locales": "de, fr"})
    ok, msg, s7b = cstore.document360_translate(conn7b.id, actor="auto", transport=fz7b.transport())
    assert not ok and s7b["locales"] == [] and set(s7b["rejected"]) == {"de", "fr"}, (msg, s7b)
    assert not fz7b.writes, fz7b.writes
    with Session(engine) as s:
        assert s.get(Connection, conn7b.id).status == "error", "misconfig -> error status"
    print("  language availability: unknown targets dropped; all-rejected -> ok=False + error")

    # ---- (10) publish: POST {user_id, version_number}; never a PUT field; needs a user id ----
    fz8 = FakeD360(articles=[_md_article("100")])
    conn8 = _dconn("D360 publish", {"publish": "1", "target_locales": "de"})
    ok, msg, _s8 = cstore.document360_translate(conn8.id, actor="auto", transport=fz8.transport())
    assert ok, msg
    assert fz8.publishes == [("100", "de")], ("publish call issued", fz8.publishes)
    assert (
        fz8.publish_bodies[0]["user_id"] == D360_USER
        and fz8.publish_bodies[0]["version_number"] == 1
    ), fz8.publish_bodies
    assert "publish" not in fz8.writes[0][2], fz8.writes[0][2]
    assert fz8.versions[("100", "de")]["public"] == 1, "the written version is now the live one"
    fz9 = FakeD360(articles=[_md_article("100")])
    conn9 = _dconn("D360 draft", {"target_locales": "de"})
    cstore.document360_translate(conn9.id, actor="auto", transport=fz9.transport())
    assert not fz9.publishes and fz9.versions[("100", "de")]["public"] is None, (
        "draft default -> no publish call, nothing live",
        fz9.publishes,
    )
    fz9b = FakeD360(articles=[_md_article("100")])
    conn9b = _dconn("D360 publish no user", {"publish": "1", "user_id": "", "target_locales": "de"})
    ok, msg, _s9b = cstore.document360_translate(
        conn9b.id, actor="auto", transport=fz9b.transport()
    )
    assert not ok and "Team account id" in msg, msg
    assert not fz9b.writes and not fz9b.publishes, (
        "publish without a user id stops before any MT/write"
    )
    print(
        "  publish: POST with user_id + version_number; draft default; no user id -> refused early"
    )

    # ---- (11) 429 backoff honored (Retry-After) ----
    slept = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        fz10 = FakeD360(articles=[_md_article("100")])
        fz10.rate_limit_once_on = "/Language/"
        dc10 = _connector(fz10)
        assert set(dc10.enabled_locales()) == {"en", "de", "fr"}
        assert slept == [5.0], ("honored Retry-After: 5", slept)
    finally:
        cbase._sleep = real_sleep
    print("  backoff: 429 + Retry-After on a GET retried once (honored 5s)")

    # ---- (11b) shared base: X-RateLimit-Reset honoured only as a sane seconds-until on a 429 ----
    import httpx as _hx

    def _ra(status, headers):
        return cbase._retry_after_seconds(_hx.Response(status, headers=headers))

    now = int(time.time())
    for status, headers, want, why in (
        (429, {"X-RateLimit-Reset": "7"}, 7.0, "Document360 seconds-until"),
        (429, {"Retry-After": "3", "X-RateLimit-Reset": "9"}, 3.0, "Retry-After wins"),
        (429, {"X-Contentful-RateLimit-Reset": "2", "X-RateLimit-Reset": "9"}, 2.0, "Contentful"),
        (429, {"X-RateLimit-Reset": str(now + 30)}, None, "epoch seconds -> backoff"),
        (429, {"X-RateLimit-Reset": str((now + 30) * 1000)}, None, "epoch ms -> backoff"),
        (429, {"X-RateLimit-Reset": "30000"}, None, "ms-until / absurd -> backoff"),
        (429, {"X-RateLimit-Reset": "0"}, None, "zero -> backoff"),
        (429, {"X-RateLimit-Reset": "-5"}, None, "past -> backoff"),
        (429, {"X-RateLimit-Reset": "nan"}, None, "NaN -> backoff"),
        (429, {"X-RateLimit-Reset": "inf"}, None, "inf -> backoff"),
        (429, {"X-RateLimit-Reset": "2026-10-10T12:00:00Z"}, None, "ISO date -> backoff"),
        (503, {"X-RateLimit-Reset": "7"}, None, "only a 429 uses the window reset"),
        (429, {}, None, "no header -> backoff"),
    ):
        got = _ra(status, headers)
        assert got == want, (why, headers, got)
    print("  backoff: X-RateLimit-Reset honoured only as 0<s<=MAX_SLEEP on a 429; else backoff")

    # ---- (12) org scoping: project inherits the connection's org ----
    fz11 = FakeD360(articles=[_md_article("100")])
    conn11 = _dconn("D360 org")
    cstore.document360_translate(conn11.id, actor="auto", transport=fz11.transport())
    row11 = cstore.get(conn11.id)
    with Session(engine) as s:
        proj = s.get(Project, row11.project_id)
    assert proj is not None
    assert getattr(proj, "org_id", None) == getattr(row11, "org_id", None), (
        proj.org_id,
        row11.org_id,
    )
    print("  org scoping: connector-created project inherits the connection's org")

    # ---- (13) existence probe: every "no real version" answer is ABSENT ----
    for mode in ("400", "404", "null", "fallback"):
        fz12 = FakeD360(articles=[_md_article("100")], missing_mode=mode)
        dc12 = _connector(fz12)
        assert dc12.get_translation("100", "de") is ABSENT, ("missing langCode -> ABSENT", mode)
    fz12.add_version("100", "de", title="Exists")
    got = dc12.get_translation("100", "de")
    assert got is not ABSENT and got.get("title") == "Exists", got
    last = [(p, q) for p, q in fz12.gets if p.endswith("/de")][-1]
    assert last[1].get("isPublished") == "false", "the probe reads the latest DRAFT-or-published"
    print("  strategy probe: 400/404/null/fallback -> ABSENT (create); a real version -> present")

    # ---- (14) routes: the type page offers the add form again (un-archived) ----
    c = TestClient(app)
    idx = c.get("/connectors").text
    assert "Document360" in idx, "Document360 tile missing from /connectors"
    typ = c.get("/connectors/document360").text
    assert "New connections are closed" not in typ and "Create connection" in typ, (
        "un-archived Document360 type page must offer the add form"
    )
    spec_fields = {f.name: f for f in _reg.spec("document360").config_fields}
    for need in ("project_version_id", "category_id", "region", "user_id", "publish"):
        assert need in spec_fields, ("config field missing", need)
    assert spec_fields["project_version_id"].required, "the project version is required"
    assert "api_token" in {f.name for f in _reg.spec("document360").secret_fields}
    print("  routes: Document360 type page offers Create connection; spec fields pinned")

    # ---- (15) CLI document360-translate round-trip ----
    fz13 = FakeD360(articles=[_md_article("100")])
    connc = _dconn("D360 cli", {"target_locales": "de"})
    real_dt = cstore.document360_translate
    seen = {}

    def _capturing_dt(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        seen["called"] = True
        return real_dt(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=fz13.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "document360_translate", _capturing_dt)
    try:
        from app.cli.cmd_connector import _document360_translate

        class _Args:
            connection_id = connc.id
            article_ids = ""
            locales = ""
            actor = "cli"
            local = True
            org = None
            as_json = True

        emitted = {}

        class _Emit:
            as_json = True

            def result(self, text, data):
                emitted.update(data)

            def json(self, data):
                emitted.update(data)

        rc = _document360_translate(_Args(), _Emit())
        assert rc == 0 and emitted.get("ok") and seen.get("called"), (rc, emitted)
        assert {lang for _a, lang, _b in fz13.writes} == {"de"}, fz13.writes
    finally:
        set_pieces("app.services.connectors.store", "document360_translate", real_dt)
    print("  CLI: document360-translate ran the flow, wrote de version")

    # ---- (16) retry_failed re-runs ONLY the failed (article, langCode) pairs ----
    fz14 = FakeD360(articles=[_md_article("100"), _md_article("101", title="Other")])
    connret = _dconn("D360 retry")
    failures = [
        {"article_id": "100", "locale": "de", "error": "500"},
        {"article_id": "101", "locale": "fr", "error": "400"},
    ]
    okr, _m, sumr = cstore.retry_failed(
        connret.id, failures, actor="auto", transport=fz14.transport()
    )
    assert okr, sumr
    written = {(a, lang) for a, lang, _b in fz14.writes}
    assert written == {("100", "de"), ("101", "fr")}, ("only the 2 failed pairs", written)
    print("  retry: re-ran ONLY the 2 failed (article, langCode) pairs")

    # ---- (17) TargetLocale rows canonical; the wire spells the code as the project does ----
    fz15 = FakeD360(enabled_langs=("en", "pt-BR"), articles=[_md_article("100")])
    conn15 = _dconn("D360 canonical", {"target_locales": "pt-br"})
    ok, _m, s15 = cstore.document360_translate(conn15.id, actor="auto", transport=fz15.transport())
    assert ok and s15["locales"] == ["pt-br"], s15
    assert {lang for _a, lang, _b in fz15.writes} == {"pt-BR"}, (
        "the wire uses the project's own spelling of the langCode",
        fz15.writes,
    )
    proj15 = cstore.get(conn15.id).project_id
    with Session(engine) as s:
        stored = {
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == proj15)).all()
        }
    assert "pt-BR" in stored, ("TargetLocale row canonicalized", stored)
    print("  regression: TargetLocale stored canonical (pt-BR); wire uses the project's spelling")

    # ---- (18) stale-flag (translation_option) honored on suppression ----
    art18 = _md_article("300", version_number=1)
    fz18a = FakeD360(articles=[art18])
    conn18 = _dconn("D360 stale-flag")
    ok, _m, s18a = cstore.document360_translate(
        conn18.id, actor="auto", transport=fz18a.transport()
    )
    assert s18a["created"] == 2, s18a
    fz18b = FakeD360(articles=[art18])
    fz18b.add_version("300", "de")  # translation_option field MISSING -> suppressed
    fz18b.add_version("300", "fr", title="x", content="x", translation_option=1)  # needs update
    ok, _m, s18b = cstore.document360_translate(
        conn18.id, actor="auto", transport=fz18b.transport()
    )
    assert ok, s18b
    assert s18b["skipped"] == 1 and s18b["updated"] == 1, s18b
    assert {lang for _a, lang, _b in fz18b.writes} == {"fr"}, fz18b.writes
    fz18c = FakeD360(articles=[art18])
    for lang in ("de", "fr"):
        fz18c.add_version("300", lang, title="x", content="x", translation_option=2)
    ok, _m, s18c = cstore.document360_translate(
        conn18.id, actor="auto", transport=fz18c.transport()
    )
    assert ok and s18c["skipped"] == 2 and not fz18c.writes, (s18c, fz18c.writes)
    fz18d = FakeD360(articles=[art18])
    for lang in ("de", "fr"):  # the response enum spells the options out
        fz18d.add_version("300", lang, title="x", content="x", translation_option="translated")
    ok, _m, s18d = cstore.document360_translate(
        conn18.id, actor="auto", transport=fz18d.transport()
    )
    assert ok and s18d["skipped"] == 2 and not fz18d.writes, (s18d, fz18d.writes)
    fz18e = FakeD360(articles=[art18])
    for lang in ("de", "fr"):
        fz18e.add_version("300", lang, title="x", content="x", translation_option="needTranslation")
    ok, _m, s18e = cstore.document360_translate(
        conn18.id, actor="auto", transport=fz18e.transport()
    )
    assert ok and s18e["updated"] == 2, ("needTranslation re-enqueues", s18e)
    print("  stale-flag: 0/1/needTranslation re-enqueue; 2/translated/missing suppress")

    # ---- (20) PUA sentinel defense (raw PUA char in source) ----
    raw_pua = f"pre{_SHIELD_OPEN}0{_SHIELD_CLOSE}post then {{{{glossary:pw}}}} stays intact."
    sh20, codes20 = shield_codes(raw_pua)
    restored20 = unshield_codes(sh20, codes20)
    assert "{{glossary:pw}}" in restored20, ("real merge code round-tripped", restored20)
    assert _SHIELD_OPEN not in restored20 and _SHIELD_CLOSE not in restored20, restored20
    assert restored20.startswith("pre0post then"), ("raw sentinels stripped", restored20)
    print(
        "  PUA defense: raw PUA sentinel char in source stripped; real {{code}} round-trips clean"
    )

    # ---- (21) skipped articles: empty body, never published, block editor ----
    empty_art = dict(_md_article("400"), content="")
    draft_art = dict(_md_article("401"), status=0, public_version=None)
    block_art = dict(_md_article("402"), content_type="block", content="")
    fz21 = FakeD360(articles=[empty_art, draft_art, block_art, _md_article("403")])
    conn21 = _dconn("D360 skips", {"target_locales": "de"})
    ok, msg, s21 = cstore.document360_translate(conn21.id, actor="auto", transport=fz21.transport())
    assert ok, msg
    assert s21["empty_skipped"] == 1 and s21["unpublished_skipped"] == 1, s21
    assert s21["unsupported_skipped"] == 1 and s21["created"] == 1, s21
    assert {aid for aid, _l, _b in fz21.writes} == {"403"}, ("only the real article", fz21.writes)
    assert "empty-body" in msg and "not published yet" in msg and "block" in msg, msg
    print(
        "  skips: empty-body, never-published and block-editor articles are reported, not written"
    )

    # ---- (22) HELD UNITS (#640): NEEDS_REVIEW is never written, never a source stand-in ----
    # (22a) a held pair is NOT created (a created language would carry source text) and, in the
    # same run, the unflagged language is created and hashed.
    restore = _flip_review([1], "de")
    try:
        fz22 = FakeD360(articles=[_md_article("600")])
        conn22 = _dconn("D360 needs-review create")
        ok, msg, s22 = cstore.document360_translate(
            conn22.id, actor="auto", transport=fz22.transport()
        )
        assert ok, msg
    finally:
        restore()
    assert not [w for w in fz22.writes if w[1] == "de"], (
        "de is not created while held",
        fz22.writes,
    )
    assert [w for w in fz22.writes if w[1] == "fr"], "fr (untouched) is still written"
    assert s22.get("held_back") == 1, s22
    assert "600:fr" in _hashes(conn22.id) and "600:de" not in _hashes(conn22.id), (
        "no content hash on a held pair",
        _hashes(conn22.id),
    )
    # (22b) held BODY unit on an existing draft: only the title is sent, content untouched.
    restore = _flip_review([1], "de")
    try:
        fz22b = FakeD360(articles=[_md_article("601")])
        fz22b.add_version("601", "de", title="Gespeichert", content="# gespeichert text")
        fz22b.add_version("601", "fr", title="Stocke", content="# texte stocke")
        conn22b = _dconn("D360 needs-review update")
        ok, msg, s22b = cstore.document360_translate(
            conn22b.id, actor="auto", transport=fz22b.transport()
        )
        assert ok, msg
    finally:
        restore()
    de_body = next(b for _a, lang, b in fz22b.writes if lang == "de")
    assert "content" not in de_body and "translation_option" not in de_body, (
        "a held body is omitted and the version is not marked Translated",
        de_body,
    )
    assert de_body["title"] and "Click the" not in json.dumps(de_body), de_body
    assert fz22b.latest("601", "de")["content"] == "# gespeichert text", "stored body untouched"
    assert s22b.get("partial_held") == 1, s22b
    assert "601:de" not in _hashes(conn22b.id) and "601:fr" in _hashes(conn22b.id), _hashes(
        conn22b.id
    )
    # (22c) held TITLE: the body is written, no title key (the stored title survives).
    restore = _flip_review([0], "de")
    try:
        fz22c = FakeD360(articles=[_md_article("602")])
        fz22c.add_version("602", "de", title="Gespeichert", content="# alt")
        conn22c = _dconn("D360 needs-review title", {"target_locales": "de"})
        ok, msg, _s22c = cstore.document360_translate(
            conn22c.id, actor="auto", transport=fz22c.transport()
        )
        assert ok, msg
    finally:
        restore()
    de_t = fz22c.writes[0][2]
    assert "title" not in de_t and "content" in de_t, de_t
    assert fz22c.latest("602", "de")["title"] == "Gespeichert", "stored title kept, not the source"
    # (22d) EVERYTHING held: nothing is written at all, no hash.
    restore = _flip_review([0, 1, 2], "de")
    try:
        fz22d = FakeD360(articles=[_md_article("603")])
        fz22d.add_version("603", "de", title="Gespeichert", content="# alt")
        conn22d = _dconn("D360 needs-review all", {"target_locales": "de"})
        cstore.document360_translate(conn22d.id, actor="auto", transport=fz22d.transport())
    finally:
        restore()
    assert not fz22d.writes and "603:de" not in _hashes(conn22d.id), (
        fz22d.writes,
        _hashes(conn22d.id),
    )
    # (22e) a held pair in PUBLISH mode is written to a draft but never published.
    restore = _flip_review([1], "de")
    try:
        fz22e = FakeD360(articles=[_md_article("604")])
        fz22e.add_version("604", "de", title="Gespeichert", content="# alt")
        conn22e = _dconn("D360 needs-review publish", {"publish": "1", "target_locales": "de"})
        ok, msg, _s22e = cstore.document360_translate(
            conn22e.id, actor="auto", transport=fz22e.transport()
        )
    finally:
        restore()
    assert not fz22e.publishes and fz22e.versions[("604", "de")]["public"] is None, (
        "a partly held pair is not published",
        fz22e.publishes,
    )
    print("  held units: never created, held fields omitted, no hash, never published")

    # ---- (23) egress helper: residual PUA sentinel debris is stripped ----
    assert strip_write_egress_debris("Hello {{name}} world") == "Hello {{name}} world"
    assert strip_write_egress_debris(f"a{_SHIELD_OPEN}9{_SHIELD_CLOSE}b") == "ab"
    assert strip_write_egress_debris(f"a{_SHIELD_OPEN}0b") == "ab"
    assert strip_write_egress_debris(f"a0{_SHIELD_CLOSE}b") == "ab"
    assert strip_write_egress_debris(f"a{_SHIELD_OPEN}b{_SHIELD_CLOSE}c") == "abc"
    print("  23. egress helper: residual PUA sentinel debris stripped; faithful text untouched")

    # ---- (24) a mangling engine that drops a merge-code sentinel holds the article ----
    import app.services.translate as translate_mod

    real_ptd24 = translate_mod.pretranslate_document

    def _ptd_mangle_sentinel(session, doc_id, locale, spec=None, force_pii=False):
        stats = real_ptd24(session, doc_id, locale, spec, force_pii=force_pii)
        for seg in session.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if seg.target_text:
                seg.target_text = (
                    seg.target_text.replace(_SHIELD_CLOSE, "") + f" tail{_SHIELD_OPEN}"
                )
                session.add(seg)
        session.commit()
        return stats

    set_pieces("app.services.translate", "pretranslate_document", _ptd_mangle_sentinel)
    try:
        fz24 = FakeD360(articles=[_md_article("700")])
        conn24 = _dconn("D360 PUA-mangling engine")
        ok, msg, _s24 = cstore.document360_translate(
            conn24.id, actor="auto", transport=fz24.transport()
        )
        assert not ok, msg
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd24)
    assert not fz24.writes, "missing merge codes must hold the article before any PUT"
    assert "template integrity" in msg, msg
    print("  24. regression: mangling engine's dropped merge code holds the article before PUT")

    # ---- (25) a plan-gated tenant 403s the translation GET probe: per-pair failure ----
    class _Probe403D360(FakeD360):
        def _handle(self, req):
            parts = [p for p in req.url.path.split("/") if p]
            is_probe = req.method == "GET" and len(parts) == 4 and parts[1] == "Articles"
            if is_probe and parts[3] in ("de", "fr"):
                return httpx.Response(403, json={"error": "plan gate"})
            return super()._handle(req)

    fz25 = _Probe403D360(articles=[_md_article("800")])
    fz25.add_version("800", "de", title="x", content="y")
    fz25.add_version("800", "fr", title="x", content="y")
    conn25 = _dconn("D360 probe-403 per-pair guard")
    ok, msg, s25 = cstore.document360_translate(conn25.id, actor="auto", transport=fz25.transport())
    assert not ok, ("GET-probe 403 -> run not green", msg)
    assert s25["created"] == 0 and s25["updated"] == 0, s25
    assert len(s25["failures"]) == 2 and all("403" in f["error"] for f in s25["failures"]), s25
    assert not fz25.writes, ("no PUT reached - the probe itself failed", fz25.writes)
    with Session(engine) as sess:
        assert sess.get(Connection, conn25.id).status == "error"
    print("  25. regression: plan-gated GET probe 403 -> per-pair failure, no crash, run ERROR")

    # ---- (26) DRAFT SAFETY: a published translation is never edited in place ----
    # (26a) draft mode + team account id: a draft is FORKED from the live version and the machine
    # text goes into it; the live text and the published pointer are untouched.
    fz26 = FakeD360(articles=[_md_article("900")])
    fz26.add_version("900", "de", title="Live Titel", content="# live text", published=True)
    conn26 = _dconn("D360 fork", {"target_locales": "de", "user_id": D360_USER})
    ok, msg, s26 = cstore.document360_translate(conn26.id, actor="auto", transport=fz26.transport())
    assert ok, msg
    assert not fz26.live_edits, ("the published version was edited in place", fz26.live_edits)
    assert fz26.forks and fz26.forks[0][1] == {
        "version_number": 1,
        "user_id": D360_USER,
        "lang_code": "de",
    }, fz26.forks
    slot = fz26.versions[("900", "de")]
    assert slot["public"] == 1 and slot["versions"][1]["content"] == "# live text", slot
    assert 2 in slot["versions"] and slot["versions"][2]["content"] != "# live text", slot
    assert fz26.writes[0][2]["version_number"] == 2, "the PUT targets the new draft"
    assert not fz26.publishes, "draft mode never publishes"
    assert s26["forked_drafts"] == 1 and s26["updated"] == 1, s26
    assert "published text is unchanged" in msg, ("outcome visible in the run message", msg)
    # (26b) draft mode WITHOUT a team account id: skipped BEFORE any MT is paid for, left
    # unchanged and reported as live-kept; nothing written, nothing hashed.
    fz26b = FakeD360(articles=[_md_article("901")])
    fz26b.add_version("901", "de", title="Live Titel", content="# live text", published=True)
    conn26b = _dconn("D360 kept published", {"target_locales": "de"})
    engine_calls = []
    import app.services.translate as _tr

    _real_pt = _tr.pretranslate_document

    def _count_pt(*a, **k):
        engine_calls.append(1)
        return _real_pt(*a, **k)

    set_pieces("app.services.translate", "pretranslate_document", _count_pt)
    try:
        ok, msg, s26b = cstore.document360_translate(
            conn26b.id, actor="auto", transport=fz26b.transport()
        )
    finally:
        set_pieces("app.services.translate", "pretranslate_document", _real_pt)
    assert ok and not engine_calls, ("skipped before any MT is spent", ok, msg, engine_calls)
    assert not fz26b.writes and not fz26b.forks and not fz26b.live_edits, fz26b.writes
    assert s26b["updated"] == 0 and len(s26b["live_kept"]) == 1, s26b
    assert s26b["live_kept"][0]["error"].startswith("live-kept:"), s26b["live_kept"]
    assert "LIVE (published) translation(s) unchanged" in msg and "901/de" in msg, msg
    assert "901:de" not in _hashes(conn26b.id), "a live-kept pair is retried, not hashed"
    # the write-time guard repeats the check (an explicit retry bypasses the park hook)
    okr, _mr, sr = cstore.retry_failed(
        conn26b.id,
        [{"article_id": "901", "locale": "de", "error": "x"}],
        actor="auto",
        transport=fz26b.transport(),
    )
    assert not fz26b.writes and not fz26b.live_edits, ("retry must not write live", fz26b.writes)
    assert any(f["error"].startswith("live-kept:") for f in sr["failures"]), sr
    # (26c) a newer DRAFT beside the published version is edited in place (live untouched).
    fz26c = FakeD360(articles=[_md_article("902")])
    fz26c.versions[("902", "de")] = {
        "versions": {
            1: {"title": "Live", "content": "# live", "status": 3},
            2: {"title": "Draft", "content": "# draft", "status": 0},
        },
        "public": 1,
    }
    conn26c = _dconn("D360 newer draft", {"target_locales": "de"})
    cstore.document360_translate(conn26c.id, actor="auto", transport=fz26c.transport())
    assert not fz26c.forks and not fz26c.live_edits, "the newer draft is simply updated"
    assert fz26c.writes[0][2]["version_number"] == 2
    assert fz26c.versions[("902", "de")]["versions"][1]["content"] == "# live"
    # (26d) publish mode on a published translation: fork, write the draft, publish it.
    fz26d = FakeD360(articles=[_md_article("903")])
    fz26d.add_version("903", "de", title="Live Titel", content="# live text", published=True)
    conn26d = _dconn("D360 publish over live", {"publish": "1", "target_locales": "de"})
    ok, msg, _s26d = cstore.document360_translate(
        conn26d.id, actor="auto", transport=fz26d.transport()
    )
    assert ok, msg
    assert not fz26d.live_edits and fz26d.forks, (
        "even when publishing, the live version is not edited"
    )
    assert (
        fz26d.versions[("903", "de")]["public"] == 2
        and fz26d.publish_bodies[0]["version_number"] == 2
    ), fz26d.publish_bodies
    # (26e) ONE open draft is reused: a second run after the source changed edits the forked
    # draft (version 2) and never forks again; the live version stays untouched.
    fz26e = FakeD360(articles=[_md_article("904", version_number=1)])
    fz26e.add_version("904", "de", title="Live Titel", content="# live text", published=True)
    conn26e = _dconn("D360 fork once", {"target_locales": "de", "user_id": D360_USER})
    ok, msg, _a = cstore.document360_translate(
        conn26e.id, actor="auto", transport=fz26e.transport()
    )
    assert ok and len(fz26e.forks) == 1, (msg, fz26e.forks)
    fz26e.articles["904"] = _md_article(
        "904", content="# Changed again\n\nNew words.", version_number=2
    )
    ok, msg, s26e = cstore.document360_translate(
        conn26e.id, actor="auto", transport=fz26e.transport()
    )
    assert ok and len(fz26e.forks) == 1, ("a second run must reuse the open draft", fz26e.forks)
    assert not fz26e.live_edits and len(fz26e.versions[("904", "de")]["versions"]) == 2
    assert fz26e.writes[-1][2]["version_number"] == 2 and not s26e.get("forked_drafts"), s26e
    # (26f) a version whose status cannot be read is treated as live (fail closed), never edited
    dc26f = _connector(FakeD360())
    assert dc26f._is_live({}) and dc26f._is_live({"title": "x"})
    assert not dc26f._is_live({"status": 0}) and not dc26f._is_live({"status": "draft"})
    assert dc26f._is_live({"status": 3}) and dc26f._is_live({"status": "Published"})
    print("  draft safety: published -> forked draft (live untouched) / live-kept / publish")

    # ---- (27) vendor errors surface as failures; a failed publish keeps the content write ----
    fz27 = FakeD360(articles=[_md_article("910")])
    fz27.put_error_for = {"fr": 400}
    conn27 = _dconn("D360 put 400")
    ok, msg, s27 = cstore.document360_translate(conn27.id, actor="auto", transport=fz27.transport())
    assert ok and s27["created"] == 1 and len(s27["failures"]) == 1, (msg, s27)
    assert s27["failures"][0]["locale"] == "fr" and "400" in s27["failures"][0]["error"], s27
    assert "910:de" in _hashes(conn27.id) and "910:fr" not in _hashes(conn27.id)

    class _Pub400(FakeD360):
        def _publish(self, aid, lang, req):
            return self._err(400, "Version is locked")

    fz27b = _Pub400(articles=[_md_article("911")])
    conn27b = _dconn("D360 publish 400", {"publish": "1", "target_locales": "de"})
    ok, msg, s27b = cstore.document360_translate(
        conn27b.id, actor="auto", transport=fz27b.transport()
    )
    assert ok and s27b["created"] == 1 and "publish" in msg, (ok, msg)
    assert fz27b.latest("911", "de") is not None, "the content write stands as a draft"

    def _status(code):
        return Document360Connector(
            {"project_version_id": "pv"},
            {"api_token": "t"},
            transport=httpx.MockTransport(lambda r: httpx.Response(code, json={})),
        ).test()

    ok401, m401 = _status(401)
    assert not ok401 and "rejected the API token" in m401 and "region" in m401, m401
    ok500, m500 = _status(404)
    assert not ok500 and "404" in m500, m500
    print("  vendor errors: PUT 400 -> failure row, failed publish keeps the draft, 401 hint")

    # ---- (28) the API token never reaches a log, a message, the result or the SyncLog ----
    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    cap = _Capture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    try:
        fz28 = FakeD360(articles=[_md_article("920")])
        conn28 = _dconn(
            "D360 token", {"publish": "1", "target_locales": "de"}, {"api_token": "tok-SECRET-7"}
        )
        ok, msg, s28 = cstore.document360_translate(
            conn28.id, actor="auto", transport=fz28.transport()
        )
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
    with Session(engine) as sess:
        logs = [
            r.message for r in sess.exec(select(SyncLog).where(SyncLog.connection_id == conn28.id))
        ]
    blob = "\n".join(cap.lines + logs + [msg, json.dumps(s28, default=str)])
    assert "tok-SECRET-7" not in blob, "the API token leaked"
    print("  token: not in logs, run message, result or SyncLog")

    print("verify_document360 OK")


if __name__ == "__main__":
    main()
