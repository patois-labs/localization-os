"""Intercom Help Center connector + the T1 FieldMapStrategy seam — fully offline via
httpx.MockTransport (a fake Intercom workspace).

Intercom is the FIRST T1 (field-level map) connector and the FIRST NEW consumer of
the shared translate-flow engine (app/services/connectors/flow.py).

Covers:
  - locale-key mapping: internal BCP-47 -> the EXACT Intercom key, and rejection of
    an unmappable locale (gotcha #2)
  - T1 FieldMapStrategy unit cases: key-presence create-vs-update, the GET-merge-PUT
    of the WHOLE map (other locales preserved), the source-locale write guard
    (gotcha #1), and the record lock keyed on (connection, record)
  - connector: test(), containers (collections), discover (list + article_ids
    re-fetch), extract (title + description + HTML body)
  - full intercom_translate flow: CREATE (missing locale key), UPDATE (existing
    locale key), content-hash suppression, empty-article skip, NEEDS_REVIEW
    exclusion, WriteBudget ceiling trim, all-rejected FAIL, source-locale never
    written
  - engine-consumer regression: the flow declares NO watermark key (updated_at is
    metadata-tainted) — no watermark/incremental config is ever written
  - org isolation: a foreign cid -> 404 on the /api translate route
  - CLI intercom-translate subcommand smoke

ASCII-only prints; final line is 'verify_intercom OK'.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_ic_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Connection, Segment, SegStatus, TargetLocale
from app.services import connectors as _reg  # archive-tier spec introspection
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.intercom import (
    IntercomConnector,
    _to_intercom_key,
)
from app.services.connectors.strategy import (
    ABSENT,
    FieldMapStrategy,
    WriteResult,
    _record_write_lock_for,
    _write_lock_for,
)


# ============================================================================
# A fake Intercom workspace. Holds Articles (each with top-level default-locale
# fields + a translated_content map) in memory; the MockTransport serves the
# Articles + Help Center endpoints and records PUTs for assertions.
# ============================================================================
class FakeIntercom:
    def __init__(self, articles=None, collections=None):
        self.articles = {str(a["id"]): dict(a) for a in (articles or [])}
        self.collections = list(collections or [])
        self.puts: list = []  # (article_id, translated_content payload)
        self._rate_state = {"n": 0}
        self.rate_limit_once_on = None

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method

        if self.rate_limit_once_on and self.rate_limit_once_on in path:
            self._rate_state["n"] += 1
            if self._rate_state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "5"}, json={"errors": []})

        # list articles (cursor paginated; single page for the fake)
        if method == "GET" and path == "/articles":
            data = list(self.articles.values())
            return httpx.Response(
                200,
                json={
                    "type": "list",
                    "total_count": len(data),
                    "pages": {"type": "pages", "next": None},
                    "data": data,
                },
            )

        # retrieve / update one article
        if path.startswith("/articles/") and path.count("/") == 2:
            aid = path.rsplit("/", 1)[-1]
            if method == "GET":
                art = self.articles.get(aid)
                return httpx.Response(200, json=art) if art else httpx.Response(404, json={})
            if method == "PUT":
                body = json.loads(req.content)
                tc = body.get("translated_content") or {}
                self.puts.append((aid, tc))
                if aid in self.articles:
                    self.articles[aid]["translated_content"] = dict(tc)
                return httpx.Response(200, json=self.articles.get(aid, {"id": aid}))

        # collections (containers)
        if method == "GET" and path == "/help_center/collections":
            return httpx.Response(
                200, json={"type": "list", "data": self.collections, "pages": {"next": None}}
            )

        return httpx.Response(404, json={"errors": [{"code": "not_found", "path": path}]})


def _article(
    aid,
    title="Reset password",
    description="How to reset",
    body="<h1>Reset</h1><p>Click the link.</p>",
    default_locale="en",
    translated_content=None,
    author_id="55",
    help_center_id=None,
    updated_at=1_700_000_000,
):
    art = {
        "id": str(aid),
        "type": "article",
        "title": title,
        "description": description,
        "body": body,
        "author_id": author_id,
        "state": "published",
        "default_locale": default_locale,
        "updated_at": updated_at,
        "help_center_id": help_center_id,
        "translated_content": translated_content or {"type": "article_translated_content"},
    }
    return art


def _iconn(name, extra_cfg=None, secret=None):
    cfg = {"region": "US", "source_locale": "en", "target_locales": "de, fr", "author_id": "55"}
    cfg.update(extra_cfg or {})
    sec = {"access_token": "tok-123"}
    sec.update(secret or {})
    return cstore.create("intercom", name, cfg, sec, actor="admin")


def main() -> None:
    init_db()

    # ---- (0) locale-key mapping: internal BCP-47 -> exact Intercom key ----
    cases = {
        "de-DE": "de",
        "pt-br": "pt-BR",
        "zh-cn": "zh-CN",
        "zh-tw": "zh-TW",
        "es-MX": "es",
        "de-form": "de",  # 2026-10 docs no longer list de-form
        "nb-NO": "nb",
        "en": "en",
        "zh-Hans": "zh-CN",
        "pt-PT": "pt",
        "xx-YY": None,
        "": None,
    }
    for src, want in cases.items():
        assert _to_intercom_key(src) == want, (src, _to_intercom_key(src), want)
    print("  locale-key: BCP-47 -> exact Intercom key (pt-BR/zh-CN/de-form/nb); unmappable -> None")

    # ---- (1) T1 FieldMapStrategy: key-presence create/update + GET-merge-PUT ----
    strat = FieldMapStrategy(
        locale_key=_to_intercom_key,
        source_locale_of=lambda conn, rec: (rec or {}).get("default_locale"),
        finalize_block=lambda b, *, draft: {
            **b,
            "state": ("draft" if draft else "published"),
            "type": "article_content",
        },
    )
    assert strat.topology == "T1"

    class _FakeRec:
        def __init__(self, record):
            self.record = record
            self.connection_id = 1
            self.puts = []

        def get_record(self, rid):
            return dict(self.record)

        def put_record(self, rid, container, *, created):
            self.puts.append((rid, container, created))
            return WriteResult(True, created, target_id=rid)

    # article whose default_locale is en; already has an fr translation.
    rec = {
        "id": "7",
        "default_locale": "en",
        "title": "src",
        "translated_content": {
            "type": "article_translated_content",
            "fr": {"type": "article_content", "title": "Bonjour"},
        },
    }
    fc = _FakeRec(rec)
    r_new = strat.write_target(
        fc, "7", "de-DE", {"title": "Hallo"}, draft=True
    )  # de absent -> create
    r_upd = strat.write_target(
        fc, "7", "fr-FR", {"title": "Salut"}, draft=False
    )  # fr present -> update
    assert r_new.created and not r_upd.created, (r_new, r_upd)
    # first PUT: de added, fr preserved (GET-merge-PUT keeps other locales)
    _rid, cont_new, _c = fc.puts[0]
    assert cont_new["de"]["title"] == "Hallo" and cont_new["de"]["state"] == "draft", cont_new
    assert cont_new["fr"]["title"] == "Bonjour", ("other locale preserved in merge", cont_new)
    assert "type" not in cont_new, ("container discriminator dropped on write", cont_new)
    # second PUT: fr updated as published
    _rid, cont_upd, _c = fc.puts[1]
    assert cont_upd["fr"]["title"] == "Salut" and cont_upd["fr"]["state"] == "published", cont_upd
    assert strat.exists_target(fc, "7", "fr") and not strat.exists_target(fc, "7", "ja")
    print(
        "  strategy(T1): key-absent->create, key-present->update; GET-merge-PUT preserves "
        "other locales; discriminator dropped"
    )

    # ---- (1b) T1 source-locale write guard (gotcha #1): never write default_locale ----
    fc2 = _FakeRec(rec)
    r_src = strat.write_target(
        fc2, "7", "en-US", {"title": "X"}, draft=True
    )  # en == default_locale
    assert r_src.ok and not r_src.created and not fc2.puts, (
        "writing the record default locale must be a benign no-op (no PUT)",
        r_src,
        fc2.puts,
    )
    print(
        "  strategy(T1): writing a target == record default_locale is a no-op (source never "
        "corrupted)"
    )

    # ---- (1c) the T1 lock is keyed on (connection, record), NOT locale ----
    l_de = _record_write_lock_for(1, "7")
    l_fr = _record_write_lock_for(1, "7")
    assert l_de is l_fr, "all locales of one record must map to the SAME stripe"
    # ...and a T2-style locale-keyed lock would NOT have that property in general:
    assert _write_lock_for(1, "7", "de") is _write_lock_for(1, "7", "de")
    print(
        "  strategy(T1): record lock collapses all locales of a record to one stripe "
        "(serializes cross-locale writes)"
    )

    # ---- (1d) missing record -> clean failure, not a crash ----
    class _Absent(_FakeRec):
        def get_record(self, rid):
            return ABSENT

    r_miss = strat.write_target(_Absent(rec), "9", "de", {"title": "x"}, draft=True)
    assert not r_miss.ok and "not found" in r_miss.message, r_miss
    print("  strategy(T1): a vanished record -> clean WriteResult failure (no exception)")

    # ---- (2) connector: test + containers + discover + extract ----
    fi = FakeIntercom(
        articles=[_article("100"), _article("101", title="Billing")],
        collections=[{"id": "9", "name": "Getting started"}],
    )
    ic = IntercomConnector(
        {"region": "US", "author_id": "55"}, {"access_token": "t"}, transport=fi.transport()
    )
    ok, msg = ic.test()
    assert ok, msg
    assert [c["id"] for c in ic.containers()] == ["9"], ic.containers()
    arts = ic.discover_articles()
    assert sorted(a["id"] for a in arts) == ["100", "101"], arts
    # article_ids path re-fetches by id
    one = ic.discover_articles(article_ids=["100"])
    assert [a["id"] for a in one] == ["100"], one
    from app.filters import get_filter

    html = get_filter("html")
    units, chash = ic.extract_units(_article("100"), html)
    assert units[0] == {"p_index": 0, "text": "Reset password", "field": "title"}, units[0]
    assert any(u["field"] == "description" for u in units), units
    assert any(u["field"] == "body" and "Reset" in u["text"] for u in units), units
    assert len(chash) == 32, chash
    print(
        "  connector: test + containers(1) + discover(list + article_ids) + extract "
        "(title/description/HTML body)"
    )

    # ---- (3) full flow CREATE: missing locale keys -> two blocks written ----
    fi1 = FakeIntercom(articles=[_article("100")])
    conn = _iconn("IC create")
    ok, msg, s = cstore.intercom_translate(conn.id, actor="auto", transport=fi1.transport())
    assert ok, msg
    assert s["created"] == 2 and s["updated"] == 0, s  # de + fr created
    # both writes landed on article 100; final translated_content has de + fr blocks
    final_tc = fi1.articles["100"]["translated_content"]
    assert "de" in final_tc and "fr" in final_tc, final_tc
    for loc in ("de", "fr"):
        blk = final_tc[loc]
        assert blk["state"] == "draft", ("draft by default", blk)
        assert blk["type"] == "article_content", blk
        assert blk["author_id"] == 55, ("configured localization author stamped", blk)
        assert "<h1>" in blk["body"] and "<p>" in blk["body"], ("markup preserved", blk)
        assert blk["title"].strip() and blk["description"].strip(), blk
    print(
        f"  flow CREATE: 2 locale blocks written (draft, author stamped, markup + description) "
        f"({s['created']})"
    )

    # ---- (3b) source locale is NEVER a target, foreign locale UNMAPPABLE rejected ----
    fi3b = FakeIntercom(articles=[_article("100")])
    conn3b = _iconn("IC reject", {"target_locales": "en, de, xx"})  # en == source, xx unmappable
    ok, msg, s3b = cstore.intercom_translate(conn3b.id, actor="auto", transport=fi3b.transport())
    assert ok, msg
    assert s3b["locales"] == ["de"], ("only de usable", s3b)
    assert set(s3b["rejected"]) == {"en", "xx"}, s3b
    assert set(fi3b.articles["100"]["translated_content"]) - {"type"} == {"de"}, (
        "only de written; never en/xx",
        fi3b.articles["100"]["translated_content"],
    )
    print("  flow reject: source locale (en) + unmappable (xx) dropped; only de written")

    # ---- (3c) TargetLocale rows stored canonical; write key is the exact Intercom key ----
    fi3c = FakeIntercom(articles=[_article("100")])
    conn3c = _iconn("IC ptbr", {"target_locales": "pt-BR"})
    ok, msg, _s3c = cstore.intercom_translate(conn3c.id, actor="auto", transport=fi3c.transport())
    assert ok, msg
    # write key is Intercom's exact 'pt-BR'
    assert "pt-BR" in fi3c.articles["100"]["translated_content"], (
        "write key must be the exact Intercom key",
        fi3c.articles["100"]["translated_content"],
    )
    proj3c = cstore.get(conn3c.id).project_id
    with Session(engine) as sdb:
        stored = {
            tl.locale
            for tl in sdb.exec(select(TargetLocale).where(TargetLocale.project_id == proj3c)).all()
        }
    assert stored == {"pt-BR"}, ("TargetLocale stored canonical", stored)
    print("  flow key: write uses exact 'pt-BR'; TargetLocale row canonical pt-BR")

    # ---- (4) UPDATE path: an existing locale key -> merged/updated, other kept ----
    existing = {
        "type": "article_translated_content",
        "de": {
            "type": "article_content",
            "title": "Alt",
            "body": "<p>alt</p>",
            "author_id": "55",
            "state": "published",
        },
        "es": {"type": "article_content", "title": "Hola"},
    }  # unrelated locale
    fi4 = FakeIntercom(articles=[_article("100", translated_content=existing)])
    conn4 = _iconn("IC update", {"target_locales": "de, fr"})
    ok, msg, s4 = cstore.intercom_translate(conn4.id, actor="auto", transport=fi4.transport())
    assert ok, msg
    # Review gate 2026-10-10 (supersedes the #465 keep-published update): draft mode never
    # writes into an already PUBLISHED locale, neither unpublishing it (#465) nor putting
    # unreviewed machine text live. de is left exactly as it was and reported live_kept;
    # fr is created as a draft.
    assert s4["created"] == 1 and s4["updated"] == 0, s4
    assert [(f["article_id"], f["locale"]) for f in s4["live_kept"]] == [("100", "de")], s4
    tc4 = fi4.articles["100"]["translated_content"]
    assert "es" in tc4, ("the unrelated es locale must survive the GET-merge-PUT", tc4)
    assert tc4["de"]["title"] == "Alt" and "fr" in tc4, tc4
    assert tc4["de"]["state"] == "published", ("published de flipped to draft (#465)", tc4["de"])
    assert tc4["fr"]["state"] == "draft", ("new block is a draft", tc4["fr"])
    # publish ON still promotes a draft block to published.
    fi4b = FakeIntercom(
        articles=[
            _article(
                "100",
                translated_content={
                    "type": "article_translated_content",
                    "de": {"type": "article_content", "title": "Alt", "state": "draft"},
                },
            )
        ]
    )
    conn4b = _iconn("IC update publish", {"target_locales": "de", "publish": "1"})
    ok, msg, _s4b = cstore.intercom_translate(conn4b.id, actor="auto", transport=fi4b.transport())
    assert ok, msg
    assert fi4b.articles["100"]["translated_content"]["de"]["state"] == "published", fi4b.articles[
        "100"
    ]["translated_content"]
    print("  flow UPDATE: published de kept live (draft mode), fr created, es preserved")

    # ---- (5) content-hash suppression: unchanged source -> skipped, updated_at bump ignored ----
    fi5 = FakeIntercom(articles=[_article("100", updated_at=1_700_000_000)])
    conn5 = _iconn("IC hash")
    ok, _m, s5a = cstore.intercom_translate(conn5.id, actor="auto", transport=fi5.transport())
    assert s5a["created"] == 2, s5a
    # second run: updated_at bumped, but title/description/body UNCHANGED, and the two
    # locales now exist -> both suppressed, no writes.
    fi5.articles["100"]["updated_at"] = 1_799_999_999  # metadata-only bump
    fi5.puts.clear()
    ok, _m, s5b = cstore.intercom_translate(conn5.id, actor="auto", transport=fi5.transport())
    assert ok and s5b["created"] == 0 and s5b["updated"] == 0 and s5b["skipped"] == 2, (
        "updated_at-only bump + unchanged content -> suppressed",
        s5b,
    )
    assert not fi5.puts, ("no PUT on unchanged content", fi5.puts)
    print("  flow suppression: updated_at-only bump + unchanged content -> 2 skipped, 0 writes")

    # third run: body actually changes -> re-translated (both locales updated)
    fi5.articles["100"]["body"] = "<h1>Reset</h1><p>New steps here.</p>"
    fi5.puts.clear()
    ok, _m, s5c = cstore.intercom_translate(conn5.id, actor="auto", transport=fi5.transport())
    assert ok and s5c["updated"] == 2 and s5c["skipped"] == 0, ("real edit -> re-translate", s5c)
    print("  flow suppression: real body change -> 2 updated (content-hash is the gate)")

    # ---- (6) empty article -> skipped, no write ----
    fi6 = FakeIntercom(articles=[_article("100", title="", description="", body="")])
    conn6 = _iconn("IC empty")
    ok, msg, s6 = cstore.intercom_translate(conn6.id, actor="auto", transport=fi6.transport())
    assert ok, msg
    assert s6["empty_skipped"] == 1 and s6["created"] == 0 and not fi6.puts, s6
    print("  flow empty: an all-empty article is skipped (no write)")

    # ---- (7) engine-consumer regression: NO watermark key is ever written ----
    cfg7 = cstore.get(conn5.id).config or {}
    assert "intercom_hashes" in cfg7, ("the content-hash map IS persisted", list(cfg7))
    assert not any("watermark" in k for k in cfg7), (
        "the T1 poll-first flow must NOT introduce a watermark key (updated_at is "
        "metadata-tainted)",
        list(cfg7),
    )
    print("  regression: flow persists intercom_hashes but NO watermark key (updated_at gate off)")

    # ---- (8) all-rejected -> run FAILS (not green) + status error ----
    fi8 = FakeIntercom(articles=[_article("100")])
    conn8 = _iconn("IC all-rejected", {"target_locales": "en, xx"})  # en=source, xx unmappable
    ok, msg, s8 = cstore.intercom_translate(conn8.id, actor="auto", transport=fi8.transport())
    assert not ok, ("all-rejected run must FAIL", msg, s8)
    assert s8["locales"] == [] and set(s8["rejected"]) == {"en", "xx"} and not fi8.puts, s8
    with Session(engine) as sdb:
        assert sdb.get(Connection, conn8.id).status == "error", "misconfig -> status error"
    print("  regression: all-rejected -> ok=False + status=error, nothing written")

    # ---- (9) WriteBudget ceiling trims the run (partial, reported) ----
    from app import config as app_config

    real_ceiling = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        fi9 = FakeIntercom(articles=[_article("100")])
        conn9 = _iconn("IC budget", {"target_locales": "de, fr"})
        ok, msg, s9 = cstore.intercom_translate(conn9.id, actor="auto", transport=fi9.transport())
        assert ok, msg
        assert s9["created"] == 1 and s9["ceiling_hit"] is True and s9["write_ceiling"] == 1, s9
        assert len(fi9.puts) == 1, ("only one write under the ceiling", fi9.puts)
        assert "write ceiling" in msg, msg
    finally:
        app_config.MAX_WRITES_PER_RUN = real_ceiling
    print("  regression: WriteBudget ceiling=1 stops after 1 write (ceiling_hit reported)")

    # ---- (10) NEEDS_REVIEW segment excluded from write-back ----
    import app.services.translate as translate_mod

    real_ptd = translate_mod.pretranslate_document

    def _ptd_then_flip_de(session, doc_id, locale, spec=None, force_pii=False):
        stats = real_ptd(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == "de":
            # the LAST body segment (a real HTML body unit) flipped to NEEDS_REVIEW
            seg = session.exec(
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == "de")
                .order_by(Segment.order_index.desc())
            ).first()
            if seg:
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
                session.commit()
        return stats

    set_pieces("app.services.translate", "pretranslate_document", _ptd_then_flip_de)
    try:
        fi10 = FakeIntercom(articles=[_article("500")])
        conn10 = _iconn("IC needs-review")
        ok, msg, _s10 = cstore.intercom_translate(
            conn10.id, actor="auto", transport=fi10.transport()
        )
        assert ok, msg
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd)
    tc10 = fi10.articles["500"]["translated_content"]
    # #640: a pair with a held unit is NOT created (the held spot would carry SOURCE text: a
    # mixed-language target); nothing reaches the wire and the next run retries it.
    assert "de" not in tc10, ("held pair: no de translation created", tc10)
    assert "Click the link" in tc10["fr"]["body"], ("fr's translated body unit IS included", tc10)
    print("  defense-in-depth: a NEEDS_REVIEW segment is excluded from Intercom write-back")

    # ---- (11) 429 honored via the existing backoff machinery ----
    slept = []
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: slept.append(s)
    try:
        fi11 = FakeIntercom(articles=[_article("100")])
        fi11.rate_limit_once_on = "/articles"
        ic11 = IntercomConnector(
            {"region": "US"}, {"access_token": "t"}, transport=fi11.transport()
        )
        assert ic11.test()[0]
        assert slept == [5.0], ("honored Retry-After: 5", slept)
    finally:
        cbase._sleep = real_sleep
    print("  backoff: 429 + Retry-After on a GET retried once (honored 5s)")

    # ---- (12) region base URL + bad region rejected ----
    assert (
        IntercomConnector({"region": "EU"}, {"access_token": "t"})._base()
        == "https://api.eu.intercom.io"
    )
    assert (
        IntercomConnector({"region": "au"}, {"access_token": "t"})._base()
        == "https://api.au.intercom.io"
    )
    raised = False
    try:
        IntercomConnector({"region": "MARS"}, {"access_token": "t"})._base()
    except cbase.ConnectorError:
        raised = True
    assert raised, "an invalid region must be rejected"
    print("  region: US/EU/AU base URLs pinned; invalid region rejected")

    # ---- (13) org isolation: a foreign cid -> 404 on the /api translate route ----
    c = TestClient(app)
    from tests._api_fixture import user_api_token

    connapi = _iconn("IC api")
    # single-tenant: the shared connector token reaches its own connection, a
    # non-existent cid is 404 (indistinguishable from a foreign-org one — _conn_in_org).
    hdr = {"X-Loc-Token": user_api_token()}
    r_ghost = c.post("/api/connectors/99999999/intercom/translate", json={}, headers=hdr)
    assert r_ghost.status_code == 404, (
        "foreign/missing cid -> 404",
        r_ghost.status_code,
        r_ghost.text,
    )
    r_own = c.post(
        f"/api/connectors/{connapi.id}/intercom/translate", json={"article_ids": []}, headers=hdr
    )
    assert r_own.status_code in (200, 400), (
        "own connection is reachable",
        r_own.status_code,
        r_own.text,
    )
    print("  org isolation: foreign/missing cid -> 404; own connection reachable on /api route")

    # ---- (14) routes: Intercom in the add-connector flow ----
    idx = c.get("/connectors").text
    assert "Intercom" in idx, "Intercom tile missing from /connectors"
    typ = c.get("/connectors/intercom").text
    # Rebuilt and un-archived (rebuild): the type page offers the add form again and
    # the connector is in the active set.
    assert "Create connection" in typ and "New connections are closed" not in typ, (
        "un-archived Intercom type page must render the add form"
    )
    assert not _reg.is_archived("intercom")
    _fnames = {f.name for f in _reg.spec("intercom").config_fields} | {
        f.name for f in _reg.spec("intercom").secret_fields
    }
    assert "region" in _fnames, ("Intercom spec must still declare 'region'", _fnames)
    # P1: the inbound-API card renders for an intercom connection (endpoint mapped) —
    # the connection page must expose the /intercom/translate inbound endpoint path.
    page = c.get(f"/connectors/connection/{connapi.id}").text
    assert "/intercom/translate" in page, (
        "inbound-API endpoint path must render on the intercom connection page (P1)",
        "/intercom/translate" in page,
    )
    print(
        "  routes: Intercom tile + type page render generically; inbound-API endpoint card present"
    )

    # ---- (15) CLI intercom-translate subcommand smoke ----
    from app.cli.cmd_connector import _intercom_translate, _parse_list  # noqa: F401

    ficli = FakeIntercom(articles=[_article("100")])
    conncli = _iconn("IC cli")
    real_it = cstore.intercom_translate
    seen = {}

    def _capturing_it(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        seen["article_ids"] = article_ids
        return real_it(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=ficli.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "intercom_translate", _capturing_it)

    class _Args:
        connection_id = conncli.id
        article_ids = "100"
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
        rc = _intercom_translate(_Args(), emit)
        assert rc == 0, emit.res
        assert seen.get("article_ids") == ["100"], seen
        assert emit.res[1]["ok"], emit.res
    finally:
        set_pieces("app.services.connectors.store", "intercom_translate", real_it)
    print("  CLI: intercom-translate parses --article-ids and runs the flow (OK)")

    # ---- (16) retry_failed re-runs ONLY the failed (article, locale) pairs ----
    fir = FakeIntercom(articles=[_article("100"), _article("101", title="Other")])
    connr = _iconn("IC retry")
    failures = [
        {"article_id": "100", "locale": "de", "error": "500"},
        {"article_id": "101", "locale": "fr", "error": "400"},
    ]
    okr, _m, sumr = cstore.retry_failed(connr.id, failures, actor="auto", transport=fir.transport())
    assert okr, sumr
    written = {(aid, loc) for aid, tc in fir.puts for loc in tc if loc != "type"}
    assert written == {("100", "de"), ("101", "fr")}, ("only the 2 failed pairs written", written)
    print("  retry: re-ran ONLY the 2 failed (article, locale) pairs, not the cross product")

    # ---- (17) D1: two locales that FOLD onto one Intercom key -> ONE writer ----
    # nn and nb both resolve to the Intercom key 'nb'. Without the dedup guard both
    # would write the 'nb' block (last-writer wins, clobbering the other) while BOTH
    # record a success hash -> the loser is suppressed next run and lost forever.
    fi17 = FakeIntercom(articles=[_article("100")])
    conn17 = _iconn("IC fold", {"target_locales": "nn, nb"})
    ok, msg, s17 = cstore.intercom_translate(conn17.id, actor="auto", transport=fi17.transport())
    assert ok, msg
    # exactly ONE write, to the 'nb' key, carrying nb's own translated content
    assert len(fi17.puts) == 1, ("exactly one write, not two (no clobber)", fi17.puts)
    tc17 = fi17.articles["100"]["translated_content"]
    assert set(tc17) - {"type"} == {"nb"}, ("only the 'nb' key written", tc17)
    assert tc17["nb"]["title"].strip(), tc17["nb"]
    # nn reported rejected (as a collision), nb the sole usable locale
    assert s17["locales"] == ["nb"], ("only nb usable after the fold", s17)
    assert any("nn" in r and "nb" in r for r in s17["rejected"]), (
        "nn must be reported rejected as a collision",
        s17["rejected"],
    )
    # exactly ONE hash entry recorded: aid:nb — and NO aid:nn (the silent-loss guard)
    hkeys17 = set((cstore.get(conn17.id).config or {}).get("intercom_hashes") or {})
    assert "100:nb" in hkeys17 and "100:nn" not in hkeys17, (
        "one hash entry, aid:nb only (NO aid:nn)",
        hkeys17,
    )
    print("  D1 fold: nn+nb -> one write to 'nb'; nn rejected; only aid:nb hash (no silent loss)")

    # ---- (18) D2: a NEEDS_REVIEW description must NOT clobber a good target value ----
    # An existing 'de' block carries a good human description. This run's description
    # segment is forced NEEDS_REVIEW (excluded from write-back). The whole-map PUT must
    # OMIT description so the existing target value survives; title/body still update.
    existing18 = {
        "type": "article_translated_content",
        "de": {
            "type": "article_content",
            "title": "Alt Titel",
            "description": "Gute alte Beschreibung",
            "body": "<p>alt</p>",
            "author_id": "55",
            "state": "draft",  # a published block is never written in draft mode (Review gate)
        },
    }
    real_ptd18 = translate_mod.pretranslate_document

    def _ptd_flip_desc(session, doc_id, locale, spec=None, force_pii=False):
        stats = real_ptd18(session, doc_id, locale, spec, force_pii=force_pii)
        if locale == "de":
            # the description rides as the unit whose source_text is the article
            # description ("How to reset"); flip exactly that segment to NEEDS_REVIEW.
            seg = session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id,
                    Segment.target_locale == "de",
                    Segment.source_text == "How to reset",
                )
            ).first()
            if seg:
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
                session.commit()
        return stats

    set_pieces("app.services.translate", "pretranslate_document", _ptd_flip_desc)
    try:
        fi18 = FakeIntercom(articles=[_article("100", translated_content=existing18)])
        conn18 = _iconn("IC desc-guard", {"target_locales": "de"})
        ok, msg, _s18 = cstore.intercom_translate(
            conn18.id, actor="auto", transport=fi18.transport()
        )
        assert ok, msg
    finally:
        set_pieces("app.services.translate", "pretranslate_document", real_ptd18)
    de18 = fi18.articles["100"]["translated_content"]["de"]
    assert de18["description"] == "Gute alte Beschreibung", (
        "a NEEDS_REVIEW description must NOT clobber the existing target value",
        de18,
    )
    assert de18["title"] != "Alt Titel" and de18["title"].strip(), ("title still updated", de18)
    assert "alt" not in de18["body"], ("body still updated from source, not left stale", de18)
    print("  D2 desc-guard: a NEEDS_REVIEW description is OMITTED (existing target preserved)")

    # ---- (19) retry BYPASSES content-hash suppression (framework-level only_pairs) ----
    # A pair that already WROTE records a content hash, so a SCHEDULED run correctly
    # suppresses it (no re-spend). An operator retry via retry_failed is an EXPLICIT
    # instruction to re-attempt, so flow.py stage 6 bypasses suppression for those exact
    # (article, locale) pairs. Intercom is an article-style flow connector that had NO
    # per-connector hash-clear branch in retry_failed (only contentful/shopify/hubspot
    # did), so before the framework-level bypass an Intercom retry of a hash-suppressed
    # pair silently no-op'd. This proves (a) scheduled suppression still holds AND (b) the
    # explicit retry now re-attempts the pair.
    fi19 = FakeIntercom(articles=[_article("100")])
    conn19 = _iconn("IC retry-bypass", {"target_locales": "de"})
    ok, msg, s19a = cstore.intercom_translate(conn19.id, actor="auto", transport=fi19.transport())
    assert ok and s19a["created"] == 1, ("run 1 writes the de block", msg, s19a)
    assert "100:de" in ((cstore.get(conn19.id).config or {}).get("intercom_hashes") or {}), (
        "run 1 recorded the de content hash",
        cstore.get(conn19.id).config,
    )
    # scheduled run 2 (only_pairs=None): hash matches + de exists -> suppressed, no write
    fi19.puts.clear()
    ok, _m, s19b = cstore.intercom_translate(conn19.id, actor="auto", transport=fi19.transport())
    assert (
        ok and s19b["skipped"] == 1 and (s19b["created"] + s19b["updated"]) == 0 and not fi19.puts
    ), ("scheduled run 2 STILL suppresses the hash-matching pair (no re-spend)", s19b, fi19.puts)
    # explicit operator retry of exactly (100, de): the flow BYPASSES suppression and re-writes
    fi19.puts.clear()
    okr, _mr, s19c = cstore.retry_failed(
        conn19.id, [{"article_id": "100", "locale": "de"}], actor="auto", transport=fi19.transport()
    )
    assert okr, s19c
    assert s19c["updated"] == 1 and s19c["skipped"] == 0, (
        "explicit retry re-attempts the hash-suppressed pair (framework-level bypass)",
        s19c,
    )
    written19 = {(aid, loc) for aid, tc in fi19.puts for loc in tc if loc != "type"}
    assert written19 == {("100", "de")}, (
        "retry re-wrote exactly the de pair",
        written19,
        fi19.puts,
    )
    print(
        "  retry-bypass: scheduled run suppresses the hashed pair; an explicit retry "
        "re-attempts it (framework-level only_pairs bypass, no per-connector hash-clear)"
    )

    print("verify_intercom OK")


if __name__ == "__main__":
    main()
