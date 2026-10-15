"""Connector data-loss / duplication fixes, batch connectors-a (issues #443-#446, #459, #460).

Fully offline: httpx.MockTransport fakes and the built-in MockProvider only. No real
Airtable, Jira, WordPress or other service is ever contacted.

Final line: "CONNECTOR BATCH 1005 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_conn1005_")

import httpx

from app.db import init_db
from app.services.connectors import base as cbase
from app.services.connectors.airtable import AirtableConnector

cbase._sleep = lambda s: None


def test_443_create_records_not_retried():
    """A batch Airtable committed but answered 502 / timed out must NOT be resent."""
    posts = []

    def handler_502(req: httpx.Request) -> httpx.Response:
        posts.append(req)
        return httpx.Response(502, text="bad gateway after commit")

    at = AirtableConnector(
        {"base_id": "appX", "table": "T"},
        {"api_key": "patX"},
        transport=httpx.MockTransport(handler_502),
    )
    created, failed, _msg = at.create_records("appX", "T", [{"A": "1"}, {"A": "2"}])
    assert not created and len(failed) == 2, (created, failed)
    assert len(posts) == 1, ("502 on a create POST must not be resent", len(posts))

    posts.clear()

    def handler_timeout(req: httpx.Request) -> httpx.Response:
        posts.append(req)
        raise httpx.ReadTimeout("read timed out after commit", request=req)

    at = AirtableConnector(
        {"base_id": "appX", "table": "T"},
        {"api_key": "patX"},
        transport=httpx.MockTransport(handler_timeout),
    )
    created, failed, _msg = at.create_records("appX", "T", [{"A": "1"}])
    assert not created and len(failed) == 1, (created, failed)
    assert len(posts) == 1, ("timeout on a create POST must not be resent", len(posts))

    # 429 (server refused without processing) is still retried safely.
    posts.clear()

    def handler_429(req: httpx.Request) -> httpx.Response:
        posts.append(req)
        if len(posts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json={"records": [{"id": "rec1", "fields": {"A": "1"}}]})

    at = AirtableConnector(
        {"base_id": "appX", "table": "T"},
        {"api_key": "patX"},
        transport=httpx.MockTransport(handler_429),
    )
    created, failed, _msg = at.create_records("appX", "T", [{"A": "1"}])
    assert len(created) == 1 and not failed and len(posts) == 2, (created, failed, len(posts))
    print("  1. #443: create_records never resends after a 5xx/timeout; 429 still retried")


class _FakeT3Client:
    """Minimal T3DocClient: verify_target behavior is scripted per test."""

    def __init__(self, verify, connection_id):
        self.connection_id = connection_id
        self.verify = verify
        self.creates = 0
        self.fills = 0

    def create_target(self, source_id, locale, fields):
        self.creates += 1
        return f"posts:new{self.creates}", ""

    def fill_target(self, target_id, fields):
        from app.services.connectors.strategy import WriteResult

        self.fills += 1
        return WriteResult(True, False, target_id=target_id)

    def verify_target(self, target_id):
        return self.verify()

    def find_preexisting(self, source_id, locale, fields):
        return None


def test_444_verify_transient_failure_holds_pair():
    from app.services.connectors import idmap
    from app.services.connectors.strategy import DocPerLocaleStrategy, TargetUnverifiable
    from app.services.connectors.wordpress import WordPressConnector

    # --- connector level: only a definite 404 (or trash) is "absent" ---
    def wp(status_or_exc, body=None):
        def handler(req: httpx.Request) -> httpx.Response:
            if isinstance(status_or_exc, Exception):
                raise status_or_exc
            return httpx.Response(status_or_exc, json=body if body is not None else {})

        return WordPressConnector(
            {"base_url": "https://blog.acme.com", "post_types": "posts"},
            {"username": "u", "application_password": "p"},
            transport=httpx.MockTransport(handler),
        )

    assert wp(200, {"status": "publish"}).verify_target("posts:5") is True
    assert wp(200, {"status": "trash"}).verify_target("posts:5") is False
    assert wp(404, {"code": "rest_post_invalid_id"}).verify_target("posts:5") is False
    for bad in (503, 429, 401, 500):
        try:
            wp(bad).verify_target("posts:5")
        except TargetUnverifiable:
            pass
        else:
            raise AssertionError(("verify_target must not answer for status", bad))
    try:
        wp(httpx.ReadTimeout("slow", request=httpx.Request("GET", "https://x"))).verify_target(
            "posts:5"
        )
    except TargetUnverifiable:
        pass
    else:
        raise AssertionError("a timeout must raise TargetUnverifiable")

    # --- strategy level: unverifiable DONE row is held, never orphaned + recreated ---
    strat = DocPerLocaleStrategy()
    from app.services.connectors import store as cstore

    cid = cstore.create(
        "wordpress",
        "WP 444",
        {"base_url": "https://blog.acme.com", "post_types": "posts", "source_locale": "en"},
        {"username": "u", "application_password": "p"},  # pregate: allow-secret throwaway fixture
        actor="admin",
    ).id
    idmap.reserve(cid, "posts:1", "fr", "posts:77", status=idmap.DONE)

    def boom():
        raise TargetUnverifiable("503")

    client = _FakeT3Client(boom, cid)
    res = strat.write_target(client, "posts:1", "fr", {"title": "T"})
    row = idmap.get(cid, "posts:1", "fr")
    assert not res.ok and client.creates == 0, ("no second post on 503", res, client.creates)
    assert row.status == idmap.DONE and row.target_id == "posts:77", ("row untouched", row)

    # a definite absence still recreates (rule 3 unchanged)
    client = _FakeT3Client(lambda: False, cid)
    res = strat.write_target(client, "posts:1", "fr", {"title": "T"})
    assert client.creates == 1, ("a real 404 still recreates", client.creates)
    print("  2. #444: a 503/429/401/timeout on verify holds the pair; only a 404 recreates")
    # --- same class on Notion + Help Scout: a transient status holds, 404/410 = gone ---
    from app.services.connectors.helpscout import HelpScoutConnector
    from app.services.connectors.notion import NotionConnector

    def other(cls, cfg, secret, status, body=None):
        return cls(
            cfg,
            secret,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(status, json=body if body is not None else {})
            ),
        )

    mk = {
        "notion": lambda st, b=None: other(
            NotionConnector,
            {},
            {"token": "t"},
            st,
            b,  # pregate: allow-secret throwaway fixture
        ),
        "helpscout": lambda st, b=None: other(
            HelpScoutConnector,
            {},
            {"api_key": "k"},  # pregate: allow-secret throwaway fixture
            st,
            b,
        ),
    }
    for name, make in mk.items():
        assert make(404).verify_target("abc") is False, name
        assert make(410).verify_target("abc") is False, name
        for bad in (503, 429, 401, 500):
            try:
                make(bad).verify_target("abc")
            except TargetUnverifiable:
                pass
            else:
                raise AssertionError((name, "transient status must not read as deleted", bad))
    assert mk["notion"](200, {"archived": False}).verify_target("abc") is True
    assert mk["notion"](200, {"archived": True}).verify_target("abc") is False
    assert mk["helpscout"](200, {"id": "abc"}).verify_target("abc") is True
    print("  2b. #444 (review): Notion and Help Scout hold on a transient verify failure")


def _at_transport(state):
    """Compact fake Airtable: webhook register + list-payloads only (no fetch/PATCH is
    ever reached because dynamic_translate is stubbed to the outcome under test)."""
    import json

    def handler(req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        if m == "POST" and path == "/v0/bases/appX/webhooks":
            return httpx.Response(
                200,
                json={
                    "id": "achNEW",
                    "macSecretBase64": "azU=",
                    "expirationTime": "2026-07-13T00:00:00.000Z",
                },
            )
        if m == "GET" and path == "/v0/meta/bases/appX/tables":
            return httpx.Response(
                200,
                json={
                    "tables": [
                        {
                            "id": "tblC",
                            "name": "Content",
                            "fields": [{"id": "fldSRC", "name": "Notes", "type": "richText"}],
                        }
                    ]
                },
            )
        if m == "GET" and "/payloads" in path:
            pages = state.get("pages") or []
            resp = pages.pop(0) if pages else {"payloads": [], "cursor": 1, "mightHaveMore": False}
            return httpx.Response(200, json=resp)
        return httpx.Response(404, json={"path": path, "method": m, "b": json.dumps({})})

    return httpx.MockTransport(handler)


def _payload_page(rids, cursor):
    recs = {rid: {"current": {"cellValuesByFieldId": {"fldSRC": "x"}}} for rid in rids}
    return {
        "payloads": [{"changedTablesById": {"tblC": {"changedRecordsById": recs}}}],
        "cursor": cursor,
        "mightHaveMore": False,
    }


def test_445_whole_run_failure_keeps_backlog():
    from unittest import mock

    from app.services import settings as app_settings
    from app.services.connectors import airtable_webhooks as aw
    from app.services.connectors import store as cstore

    app_settings.set_many({"app_base_url": "https://tms.example.com"})

    def mk(name, extra=None):
        cfg = {
            "source_field": "Notes",
            "table": "Content",
            "base_id": "appX",
            "naming_pattern": "{src} ({loc})",
            "source_locale": "en-US",
        }
        cfg.update(extra or {})
        c = cstore.create("airtable", name, cfg, {"api_key": "patX"}, actor="admin")
        state = {}
        assert cstore.register_airtable_webhook(c.id, transport=_at_transport(state))[0]
        return c, state

    def pending(cid):
        return sorted(cstore.airtable_webhook_status(cid)["pending_record_ids"])

    # (a) forced drain of a 3-record backlog; the run fails outright -> all stay pending
    c, state = mk("Whole fail")
    cstore._add_pending_records(c.id, ["recA", "recB", "recC"])
    state["pages"] = [_payload_page([], 5)]
    with mock.patch.object(aw, "dynamic_translate", return_value=(False, "boom", {})):
        ok, _m, _s = cstore.consume_airtable_payloads(
            c.id, transport=_at_transport(state), force=True
        )
    assert not ok and pending(c.id) == ["recA", "recB", "recC"], ("backlog kept", pending(c.id))

    # (b) auto mode: ids fresh off the cursor were never pending; a whole-run failure
    # must QUEUE them (the cursor already advanced past them)
    c2, state2 = mk("Auto fail", {"auto_translate_on_change": "1"})
    state2["pages"] = [_payload_page(["recX", "recY"], 9)]
    with mock.patch.object(aw, "dynamic_translate", return_value=(False, "boom", {})):
        cstore.consume_airtable_payloads(c2.id, transport=_at_transport(state2))
    assert pending(c2.id) == ["recX", "recY"], ("auto-mode failure queued", pending(c2.id))

    # (c) archived project: ok=True but nothing translated -> nothing cleared
    c3, state3 = mk("Archived")
    cstore._add_pending_records(c3.id, ["recA", "recB"])
    state3["pages"] = [_payload_page([], 5)]
    skipped = (True, "Sync skipped (archived).", {"skipped_archived": True})
    with mock.patch.object(aw, "dynamic_translate", return_value=skipped):
        cstore.consume_airtable_payloads(c3.id, transport=_at_transport(state3), force=True)
    assert pending(c3.id) == ["recA", "recB"], ("archived keeps queue", pending(c3.id))

    # (d) regression: a normal success still clears what it handled
    c4, state4 = mk("Success")
    cstore._add_pending_records(c4.id, ["recA", "recB"])
    state4["pages"] = [_payload_page([], 5)]
    with mock.patch.object(aw, "dynamic_translate", return_value=(True, "ok", {"translated": 2})):
        cstore.consume_airtable_payloads(c4.id, transport=_at_transport(state4), force=True)
    assert pending(c4.id) == [], ("handled records are cleared", pending(c4.id))
    print("  3. #445: whole-run failure / archived skip keep the pending backlog; success clears")


_ADF_DESC = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Long description"}]}],
}
_BOTH = ("summary", "description")


def _jira_transport(n_issues, posts, puts, fields=("summary",)):
    """Fake Jira: N issues LOC-1..LOC-N (summary, optional ADF description), comment POST
    and field PUT recorders, per-issue re-GET for the concurrent-edit guard."""
    import json

    def issue_fields(i):
        f = {"summary": f"Ticket {i}"}
        if "description" in fields:
            f["description"] = _ADF_DESC
        return f

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/rest/api/3/search/jql":
            return httpx.Response(
                200,
                json={
                    "isLast": True,
                    "issues": [
                        {"key": f"LOC-{i}", "fields": issue_fields(i)}
                        for i in range(1, n_issues + 1)
                    ],
                },
            )
        if path.startswith("/rest/api/3/issue/") and req.method == "GET":
            key = path.rsplit("/", 1)[-1]
            fld = req.url.params.get("fields", "summary")
            i = int(key.split("-")[1])
            return httpx.Response(200, json={"key": key, "fields": {fld: issue_fields(i).get(fld)}})
        if path.endswith("/comment") and req.method == "POST":
            posts.append(path.split("/")[-2])
            return httpx.Response(201, json={"id": str(len(posts))})
        if path.startswith("/rest/api/3/issue/") and req.method == "PUT":
            puts.append((path.rsplit("/", 1)[-1], sorted(json.loads(req.content)["fields"])))
            return httpx.Response(204)
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def _jira_conn(name, **extra):
    from app.services.connectors import store as cstore

    cfg = {
        "base_url": "https://team.atlassian.net",
        "jql": "project = LOC",
        "source_field": "summary",
        "target_mode": "comment",
        "target_locales": "es-MX",
        "source_locale": "en-US",
    }
    cfg.update(extra)
    return cstore.create(
        "jira",
        name,
        cfg,
        {"email": "b@x.com", "api_token": "tok"},  # pregate: allow-secret throwaway fixture
        actor="admin",
    )


def test_446_pending_cleared_only_for_handled_keys():
    from unittest import mock

    import app.config as app_config
    from app.services.connectors import store as cstore

    keys = ["LOC-1", "LOC-2", "LOC-3", "LOC-4", "LOC-5"]

    # (a) fetch cap: 5 pending, max_results 2 -> only the 2 fetched leave the queue
    c = _jira_conn("cap", max_results="2")
    cstore.jira_record_pending(c.id, keys)
    posts, puts = [], []
    ok, msg, _s = cstore.jira_translate_pending(c.id, transport=_jira_transport(5, posts, puts))
    assert ok, msg
    assert sorted(posts) == ["LOC-1", "LOC-2"], posts
    assert cstore.jira_pending(c.id) == ["LOC-3", "LOC-4", "LOC-5"], cstore.jira_pending(c.id)

    # (b) write ceiling: 4 pending, ceiling 2 -> the cut-off keys stay
    c2 = _jira_conn("budget")
    cstore.jira_record_pending(c2.id, keys[:4])
    posts, puts = [], []
    real = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 2
    try:
        ok, msg, _s = cstore.jira_translate_pending(
            c2.id, transport=_jira_transport(4, posts, puts)
        )
    finally:
        app_config.MAX_WRITES_PER_RUN = real
    assert ok and len(posts) == 2, (msg, posts)
    assert cstore.jira_pending(c2.id) == ["LOC-3", "LOC-4"], cstore.jira_pending(c2.id)

    # (c) archived sync project: skipped with ok=True -> NOTHING leaves the queue
    c3 = _jira_conn("archived")
    cstore.jira_record_pending(c3.id, keys[:3])
    with mock.patch(
        "app.services.connectors.store_part4._refuse_archived_sync",
        return_value="Sync skipped: archived.",
    ):
        ok, msg, summ = cstore.jira_translate_pending(c3.id)
    assert ok and summ.get("skipped_archived"), (ok, msg, summ)
    assert cstore.jira_pending(c3.id) == keys[:3], cstore.jira_pending(c3.id)

    # (d) regression: everything handled -> queue emptied
    c4 = _jira_conn("all done")
    cstore.jira_record_pending(c4.id, keys[:2])
    posts, puts = [], []
    ok, msg, _s = cstore.jira_translate_pending(c4.id, transport=_jira_transport(2, posts, puts))
    assert ok and cstore.jira_pending(c4.id) == [], cstore.jira_pending(c4.id)
    print("  4. #446: pending keys cleared only when handled (cap, ceiling, archived)")


def test_460_ceiling_cut_pair_does_not_record_hash():
    from unittest import mock

    import app.config as app_config
    import app.services.translate as translate_mod
    from app.models import SegStatus
    from app.services.connectors import store as cstore

    def run(conn, posts, puts, n_issues=1):
        return cstore.jira_translate(
            conn.id, transport=_jira_transport(n_issues, posts, puts, _BOTH)
        )

    # (a) field mode (Issue #641: one combined write into the target custom field per
    # issue), TWO issues, ONE write left in the run: the second issue is cut off, must
    # not record its hash, and is written next run.
    c = _jira_conn(
        "field cut",
        source_field="summary,description",
        target_mode="field",
        target_field="customfield_10050",
    )
    posts, puts = [], []
    real = app_config.MAX_WRITES_PER_RUN
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        ok, _msg, _s = run(c, posts, puts, 2)
    finally:
        app_config.MAX_WRITES_PER_RUN = real
    assert ok and puts == [("LOC-1", ["customfield_10050"])], ("only LOC-1 landed", puts)
    posts2, puts2 = [], []
    ok, _msg, summ = run(c, posts2, puts2, 2)
    assert puts2 == [("LOC-2", ["customfield_10050"])], (
        "the cut-off issue must be retried next run, not suppressed",
        puts2,
        summ,
    )
    # now fully written -> the third run is suppressed
    posts3, puts3 = [], []
    ok, _msg, summ = run(c, posts3, puts3, 2)
    assert not puts3 and summ["suppressed"] == 2, ("converged", puts3, summ)

    # (b) a field held for NEEDS_REVIEW keeps the pair retryable
    c2 = _jira_conn(
        "held",
        source_field="summary,description",
        target_mode="field",
        target_field="customfield_10050",
    )
    real_ptd = translate_mod.pretranslate_document

    def hold_description(session, doc_id, locale, *a, **kw):
        from sqlmodel import select

        from app.models import Segment

        stats = real_ptd(session, doc_id, locale, *a, **kw)
        segs = session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        segs.sort(key=lambda sg: sg.order_index)
        segs[-1].status = SegStatus.NEEDS_REVIEW  # the description leaf
        session.add(segs[-1])
        session.commit()
        return stats

    posts, puts = [], []
    with mock.patch.object(translate_mod, "pretranslate_document", hold_description):
        run(c2, posts, puts)
    hashes = (cstore.get(c2.id).config or {}).get("jira_hashes") or {}
    assert not hashes, ("a pair with a held field must not record its hash", hashes)
    assert puts == [], ("a held pair must not half-write the target field", puts)
    print("  5. #460: a ceiling-cut or review-held pair does not record the suppression hash")

    # (c) gate finding: COMMENT mode (the default) with a held field must not post a
    # partial comment, because the pair records no hash and every later run would post
    # another one (append-only duplicates). Nothing posts while held; the key stays
    # pending; once the hold clears it posts exactly once and then converges.
    c3 = _jira_conn("held comment", source_field="summary,description")
    for _ in range(2):
        cstore.jira_record_pending(c3.id, ["LOC-1"])
        posts, puts = [], []
        with mock.patch.object(translate_mod, "pretranslate_document", hold_description):
            cstore.jira_translate_pending(c3.id, transport=_jira_transport(1, posts, puts, _BOTH))
        assert posts == [], ("no partial comment while a field is held", posts)
        assert cstore.jira_pending(c3.id) == ["LOC-1"], cstore.jira_pending(c3.id)
    posts, puts = [], []
    cstore.jira_translate_pending(c3.id, transport=_jira_transport(1, posts, puts, _BOTH))
    assert posts == ["LOC-1"] and cstore.jira_pending(c3.id) == [], (posts, c3.id)
    posts, puts = [], []
    run(c3, posts, puts)
    assert posts == [], ("converged after the full comment posted", posts)

    # (d) a held field on a pair whose hash is ALREADY recorded (unchanged source, e.g.
    # recorded before #460) is suppressed: nothing to write, so it must leave the queue.
    cstore.jira_record_pending(c3.id, ["LOC-1"])
    posts, puts = [], []
    with mock.patch.object(translate_mod, "pretranslate_document", hold_description):
        cstore.jira_translate_pending(c3.id, transport=_jira_transport(1, posts, puts, _BOTH))
    assert posts == [] and cstore.jira_pending(c3.id) == [], (posts, cstore.jira_pending(c3.id))
    print("  5b. #460 (gate): a held pair in comment mode posts nothing until it is whole")


class _DeadOnThird:
    """Stand-in for translate.pretranslate_document: fine twice, then the engine dies."""

    def __init__(self):
        self.calls = 0

    def __call__(self, session, doc_id, locale, *a, **kw):
        from app.services import providers

        self.calls += 1
        if self.calls >= 3:
            raise providers.DeadEngineError("engine:99 is gone")
        return {"translated": 0}


def test_459_aborted_flow_run_keeps_hashes_of_written_pairs():
    from unittest import mock

    import app.services.translate as translate_mod
    from app.services.connectors import flow
    from app.services.connectors import store as cstore
    from app.services.connectors.strategy import WriteResult

    written = []

    class _Strategy:
        def write_target(self, connector, aid, loc, payload, draft=True):
            written.append(aid)
            return WriteResult(True, True, target_id=f"t-{aid}")

    class _Connector:
        strategy = _Strategy()

        def discover_articles(self, article_ids=None):
            return [{"id": "A"}, {"id": "B"}, {"id": "C"}]

    def prepare(ctx, art):
        aid = art["id"]
        return flow.ArticlePrep(
            aid=aid,
            chash=f"hash-{aid}",
            # empty source title: the stubbed pretranslate makes no targets, and a non-empty
            # title with no target would count as HELD (#640), which withholds the hash.
            title_text="",
            body_units=[],
            extra_skeleton={},
            doc_title=f"doc {aid}",
        )

    spec = flow.TranslateFlowSpec(
        kind="zendesk",
        no_conn_msg="no conn",
        hash_key="zendesk_hashes",
        probe_label="Zendesk",
        preflight=lambda ctx: ({"de"}, {}),
        prepare=prepare,
        is_suppressed=lambda connector, aid, loc: False,
        build_payload=lambda prep, loc, seg0, mapping: {},
        compute_draft=lambda cfg: True,
        compute_ok=lambda ctx: bool(ctx.created or ctx.updated),
        build_message=lambda ctx: "msg",
        build_result=lambda ctx: {"created": ctx.created},
    )
    conn = cstore.create(
        "zendesk",
        "flow abort",
        {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de"},
        {"email": "a@acme.com", "api_token": "tok123"},  # pregate: allow-secret throwaway fixture
        actor="admin",
    )
    dead = _DeadOnThird()
    with (
        mock.patch.object(cstore, "build_fresh", return_value=_Connector()),
        mock.patch.object(translate_mod, "pretranslate_document", dead),
    ):
        ok, msg, _res = flow.run_translate_flow(
            spec, conn.id, transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )
    assert not ok and written == ["A", "B"], (ok, msg, written)
    hashes = (cstore.get(conn.id).config or {}).get("zendesk_hashes") or {}
    assert hashes == {"A:de": "hash-A", "B:de": "hash-B"}, (
        "the hashes of the articles already written must survive the abort",
        hashes,
    )
    print("  6. #459: a flow run aborted by a dead engine keeps the hashes of written pairs")


def test_459_confluence_abort_keeps_children_and_hashes():
    from unittest import mock

    import app.services.translate as translate_mod
    from app.services.connectors import idmap
    from app.services.connectors import store as cstore
    from tests._confluence_fake import FakeConfluence

    fake = FakeConfluence()
    fake.add_space("ENG", "Engineering")
    for pid in ("1", "2", "3"):
        fake.add_page("ENG", f"Page {pid}", f"<p>Body {pid}</p>", pid=pid)
    conn = cstore.create(
        "confluence",
        "cf abort",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "ENG",
            "target_locales": "de",
            "source_locale": "en",
        },
        {"email": "b@x.com", "api_token": "tok"},  # pregate: allow-secret throwaway fixture
        actor="admin",
    )
    real = translate_mod.pretranslate_document
    state = {"n": 0}

    def dead_on_third(session, doc_id, locale, *a, **kw):
        # the first two pages really translate (a held, untranslated unit would never be
        # written under #640), then the engine dies
        from app.services import providers

        state["n"] += 1
        if state["n"] >= 3:
            raise providers.DeadEngineError("engine:99 is gone")
        return real(session, doc_id, locale, *a, **kw)

    with mock.patch.object(translate_mod, "pretranslate_document", dead_on_third):
        ok, msg, _res = cstore.confluence_translate(conn.id, transport=fake.transport())
    made = sorted(p["parentId"] for p in fake.pages.values() if p["parentId"])
    assert not ok and made == ["1", "2"], (ok, msg, made)
    cfg = cstore.get(conn.id).config or {}
    assert set(cfg.get("confluence_hashes") or {}) == {"1:de", "2:de"}, cfg
    rows = {r.source_id: r for r in idmap.list_for_connection(conn.id)}
    assert set(rows) == {"1", "2"} and all(r.status == idmap.DONE for r in rows.values()), rows
    print("  7. #459: a Confluence run aborted by a dead engine keeps its id map and hashes")


def test_640_held_unit_pair_records_no_hash_and_create_is_held():
    """#640: a (record, locale) pair with ANY unit held back (NEEDS_REVIEW) records no content
    hash, so the next run retries it. A spec with held_prefix (field-mapped connectors whose
    create would leave the field empty) also never CREATES the variant while a unit is held,
    but still updates an existing one."""
    from types import SimpleNamespace
    from unittest import mock

    from sqlmodel import select

    import app.services.translate as translate_mod
    from app.models import Segment, SegStatus
    from app.services.connectors import flow
    from app.services.connectors import store as cstore
    from app.services.connectors.strategy import WriteResult

    hold = {"on": True}
    writes = []
    exists = {"v": False}

    def stub(session, doc_id, locale, *a, **kw):
        for sg in session.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if sg.target_locale != locale:
                continue
            sg.target_text = f"T:{sg.source_text}"
            sg.status = (
                SegStatus.NEEDS_REVIEW if (hold["on"] and sg.order_index == 1) else SegStatus.MT
            )
            session.add(sg)
        session.commit()
        return {"translated": 1}

    class _Strategy:
        def exists_target(self, connector, aid, loc):
            return exists["v"]

        def write_target(self, connector, aid, loc, payload, draft=True):
            writes.append((aid, loc, dict(payload)))
            return WriteResult(True, not exists["v"], target_id=f"t-{aid}")

    class _Connector:
        strategy = _Strategy()

        def discover_articles(self, article_ids=None):
            return [{"id": "A"}]

    def prepare(ctx, art):
        return flow.ArticlePrep(
            aid="A",
            chash="hash-A",
            title_text="Title",
            body_units=[SimpleNamespace(text="Body text", p_index="body")],
            extra_skeleton={},
            doc_title="doc A",
        )

    def make_spec(held_prefix):
        return flow.TranslateFlowSpec(
            kind="zendesk",
            no_conn_msg="no conn",
            hash_key="zendesk_hashes",
            probe_label="Zendesk",
            preflight=lambda ctx: ({"de"}, {}),
            prepare=prepare,
            is_suppressed=lambda connector, aid, loc: False,
            build_payload=lambda prep, loc, seg0, mapping: {"title": seg0, **mapping},
            compute_draft=lambda cfg: True,
            compute_ok=lambda ctx: True,
            build_message=lambda ctx: "msg",
            build_result=lambda ctx: {"created": ctx.created, "failures": ctx.failures},
            held_prefix=held_prefix,
        )

    def run(held_prefix):
        conn = cstore.create(
            "zendesk",
            f"flow held {held_prefix}",
            {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de"},
            {"email": "a@acme.com", "api_token": "tok123"},  # pregate: allow-secret throwaway
            actor="admin",
        )

        def go():
            with (
                mock.patch.object(cstore, "build_fresh", return_value=_Connector()),
                mock.patch.object(translate_mod, "pretranslate_document", stub),
            ):
                return flow.run_translate_flow(
                    make_spec(held_prefix),
                    conn.id,
                    transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                )

        def hashes():
            return (cstore.get(conn.id).config or {}).get("zendesk_hashes") or {}

        return go, hashes

    # (a0) article / doc-per-locale connector (no held_prefix), variant absent: NOT created
    # (it would hold the SOURCE text in the held paragraph), reported held, no hash
    go, hashes = run(None)
    ok, msg, res = go()
    assert ok and not writes and not hashes(), ("no create while a unit is held", writes)
    assert res["held_back"] == 1 and "Held back 1" in msg, (res, msg)
    # (a) variant present: written (partial), no hash; retried after approval
    exists["v"] = True
    ok, msg, res = go()
    assert ok and len(writes) == 1 and not hashes(), (writes, hashes())
    # the held unit's NEEDS_REVIEW text never reaches the update payload
    assert "body" not in writes[0][2] and writes[0][2].get("title") == "T:Title", writes
    assert "T:Body text" not in repr(writes[0][2]), writes
    assert res["partial_held"] == 1 and "held for review" in msg, (res, msg)
    hold["on"] = False
    ok, msg, res = go()
    assert ok and len(writes) == 2 and hashes() == {"A:de": "hash-A"}, (writes, hashes())
    assert "partial_held" not in res
    exists["v"] = False

    # (b) field-mapped connector (held_prefix), variant absent: NOT created, reported held
    writes.clear()
    hold["on"] = True
    go, hashes = run("held-back:")
    ok, msg, res = go()
    assert ok and not writes and not hashes(), ("no create while a unit is held", writes)
    assert len(res["failures"]) == 1 and res["failures"][0]["error"].startswith("held-back:"), res
    assert "body" in res["failures"][0]["error"], res
    # (c) variant present: updated, the held field is left out of the payload, no hash
    exists["v"] = True
    ok, msg, res = go()
    assert ok and len(writes) == 1 and writes[0][2].get("title") == "T:Title", writes
    assert "body" not in writes[0][2] and not hashes(), (writes, hashes())
    assert any(f["error"].startswith("held-back:") for f in res["failures"]), res
    # (d) approved: created/updated whole and the hash is recorded
    hold["on"] = False
    ok, msg, res = go()
    assert ok and writes[-1][2].get("body") == "T:Body text" and hashes() == {"A:de": "hash-A"}
    print("  7. #640: a pair with a held unit records no hash; field-mapped create is held")


def main():
    init_db()
    test_443_create_records_not_retried()
    test_444_verify_transient_failure_holds_pair()
    test_445_whole_run_failure_keeps_backlog()
    test_446_pending_cleared_only_for_handled_keys()
    test_460_ceiling_cut_pair_does_not_record_hash()
    test_459_aborted_flow_run_keeps_hashes_of_written_pairs()
    test_459_confluence_abort_keeps_children_and_hashes()
    test_640_held_unit_pair_records_no_hash_and_create_is_held()
    print("CONNECTOR BATCH 1005 OK")


if __name__ == "__main__":
    main()
