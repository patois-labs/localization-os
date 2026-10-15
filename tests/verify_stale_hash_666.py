"""#666: a (record, locale) pair written INCOMPLETELY drops its older stored content hash.

When a run writes a pair with some unit held back (NEEDS_REVIEW, #640) or with a field the
write left out (`write_incomplete`, #665), the target is MIXED: some fields carry the new
translation, some keep their older value. The pair's OLDER stored hash no longer describes
that target, so it must be removed: otherwise a later edit of the source back to exactly that
older text would hash-match and the pair would be treated as done while fields stay stale.

Covers the shared flow (flow.py stage 9, one place for every connector on it):
  1. revert-to-old-text after a held partial write re-writes the pair (fails before #666)
  2. the same after a `write_incomplete` partial write
  3. an aborted run still persists the drop (the finally-block persist)
  4. a fully written, unchanged pair spends no MT and writes nothing (no extra spend)
  5. a held pair still records no hash (#640)
  6. patch_config_merge removal: delta wins, casefold drops legacy aliases only on request
Plus the hand-rolled hash bookkeeping that is NOT on the shared flow:
  7. Jira (store_part4): a held pair writes NOTHING (all-or-nothing per pair), so the stored
     hash still describes the target and a revert is correctly suppressed (no write).
  8. Confluence uses the casefold drop (legacy mixed-case keys alias the pair).
(GitHub/GitLab/Bitbucket share git_core: checked in verify_github; Confluence end to end in
verify_confluence_harden; Contentful in verify_contentful_richtext; ServiceNow in its suite.)

Fully offline. ASCII-only prints; final line 'verify_stale_hash_666 OK'.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_stale666_")

import json
from types import SimpleNamespace
from unittest import mock

import httpx
from sqlmodel import select

import app.services.translate as translate_mod
from app.db import init_db
from app.models import Segment, SegStatus
from app.services.connectors import base as cbase
from app.services.connectors import flow
from app.services.connectors import store as cstore
from app.services.connectors.strategy import WriteResult

cbase._sleep = lambda s: None


class _Tenant:
    """One record "A" whose title/body texts the test edits between runs. The variant for
    locale "de" exists after the first write; `is_suppressed` answers True for an existing
    variant, so a stale hash-match WOULD suppress the pair (the #666 failure mode)."""

    def __init__(self):
        self.title = "Title v1"
        self.body = "Body v1"
        self.exists = False
        self.writes = []
        self.incomplete = False  # the next write leaves a field out (write_incomplete)
        self.mt_calls = 0
        self.hold_body = False

    def chash(self):
        return cbase.content_hash(self.title, self.body)


def _make(t: _Tenant, name: str):
    conn = cstore.create(
        "zendesk",
        name,
        {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de"},
        {"email": "a@acme.com", "api_token": "tok123"},  # pregate: allow-secret throwaway
        actor="admin",
    )

    def stub(session, doc_id, locale, *a, **kw):
        t.mt_calls += 1
        for sg in session.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if sg.target_locale != locale:
                continue
            sg.target_text = f"T:{sg.source_text}"
            sg.status = (
                SegStatus.NEEDS_REVIEW if (t.hold_body and sg.order_index == 1) else SegStatus.MT
            )
            session.add(sg)
        session.commit()
        return {"translated": 1}

    class _Strategy:
        def exists_target(self, connector, aid, loc):
            return t.exists

        def write_target(self, connector, aid, loc, payload, draft=True):
            t.writes.append(dict(payload))
            created = not t.exists
            t.exists = True
            return WriteResult(True, created, target_id="t-A")

    class _Connector:
        strategy = _Strategy()

        def discover_articles(self, article_ids=None):
            return [{"id": "A"}]

    def prepare(ctx, art):
        return flow.ArticlePrep(
            aid="A",
            chash=t.chash(),
            title_text=t.title,
            body_units=[SimpleNamespace(text=t.body, p_index="body")],
            extra_skeleton={},
            doc_title="doc A",
        )

    spec = flow.TranslateFlowSpec(
        kind="zendesk",
        no_conn_msg="no conn",
        hash_key="zendesk_hashes",
        probe_label="Zendesk",
        preflight=lambda ctx: ({"de"}, {}),
        prepare=prepare,
        is_suppressed=lambda connector, aid, loc: t.exists,
        build_payload=lambda prep, loc, seg0, mapping: {"title": seg0, **mapping},
        compute_draft=lambda cfg: True,
        compute_ok=lambda ctx: True,
        build_message=lambda ctx: "msg",
        build_result=lambda ctx: {
            "created": ctx.created,
            "updated": ctx.updated,
            "skipped": ctx.skipped,
        },
        held_prefix="held-back:",
        write_incomplete=lambda connector, aid, loc: t.incomplete,
    )

    def go(**kw):
        with (
            mock.patch.object(cstore, "build_fresh", return_value=_Connector()),
            mock.patch.object(translate_mod, "pretranslate_document", stub),
        ):
            return flow.run_translate_flow(
                spec, conn.id, transport=httpx.MockTransport(lambda r: httpx.Response(404)), **kw
            )

    def hashes():
        return dict((cstore.get(conn.id).config or {}).get("zendesk_hashes") or {})

    return conn, go, hashes


def test_revert_after_held_partial_rewrites():
    t = _Tenant()
    _c, go, hashes = _make(t, "666 held revert")
    ok, _m, res = go()  # v1, complete
    h1 = t.chash()
    assert ok and res["created"] == 1 and hashes() == {"A:de": h1}, (res, hashes())
    # v2: both fields change, the body is held for review -> title written, body kept (v1)
    t.title, t.body, t.hold_body = "Title v2", "Body v2", True
    ok, _m, res = go()
    assert res["updated"] == 1 and t.writes[-1] == {"title": "T:Title v2"}, t.writes
    assert "A:de" not in hashes(), ("#666: the stale v1 hash must be dropped", hashes())
    # the source is edited back to exactly v1: the target title is still v2, so the pair
    # must be re-translated and re-written (before #666 it hash-matched and was skipped)
    t.title, t.body, t.hold_body = "Title v1", "Body v1", False
    n_writes, n_mt = len(t.writes), t.mt_calls
    ok, _m, res = go()
    assert res["skipped"] == 0 and res["updated"] == 1, ("revert re-writes the pair", res)
    assert len(t.writes) == n_writes + 1 and t.mt_calls == n_mt + 1, (t.writes, t.mt_calls)
    assert t.writes[-1] == {"title": "T:Title v1", "body": "T:Body v1"}, t.writes[-1]
    assert hashes() == {"A:de": h1}, hashes()
    # 4. now fully written and unchanged: no MT, no write
    n_writes, n_mt = len(t.writes), t.mt_calls
    ok, _m, res = go()
    assert res["skipped"] == 1 and len(t.writes) == n_writes and t.mt_calls == n_mt, (res, t)
    print("  1. revert to the old text after a held partial write re-writes the pair")
    print("  4. a fully written, unchanged pair spends no MT and writes nothing")


def test_revert_after_write_incomplete_rewrites():
    t = _Tenant()
    _c, go, hashes = _make(t, "666 incomplete revert")
    go()
    h1 = t.chash()
    assert hashes() == {"A:de": h1}, hashes()
    t.title, t.incomplete = "Title v2", True
    _ok, _m, res = go()
    assert res["updated"] == 1 and "A:de" not in hashes(), ("stale hash dropped", hashes())
    t.title, t.incomplete = "Title v1", False
    _ok, _m, res = go()
    assert res["skipped"] == 0 and res["updated"] == 1 and hashes() == {"A:de": h1}, res
    print("  2. revert after a write_incomplete partial write re-writes the pair")


def test_abort_still_drops():
    t = _Tenant()
    c, go, hashes = _make(t, "666 abort")
    go()
    assert "A:de" in hashes(), hashes()
    # a run that writes A partly (held body) and then crashes on the next record
    t.title, t.body, t.hold_body = "Title v2", "Body v2", True
    try:
        _run_on(c.id, t, abort=True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the simulated crash must propagate")
    assert t.writes[-1] == {"title": "T:Title v2"}, t.writes
    assert "A:de" not in hashes(), ("an aborted run still drops the stale hash", hashes())
    print("  3. an aborted run still persists the drop")


def _run_on(cid, t, abort=False):
    """Drive one flow run on an EXISTING connection (used by the abort case)."""

    def stub(session, doc_id, locale, *a, **kw):
        for sg in session.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if sg.target_locale != locale:
                continue
            sg.target_text = f"T:{sg.source_text}"
            sg.status = (
                SegStatus.NEEDS_REVIEW if (t.hold_body and sg.order_index == 1) else SegStatus.MT
            )
            session.add(sg)
        session.commit()
        return {"translated": 1}

    class _Strategy:
        def exists_target(self, connector, aid, loc):
            return t.exists

        def write_target(self, connector, aid, loc, payload, draft=True):
            t.writes.append(dict(payload))
            return WriteResult(True, False, target_id="t-A")

    class _Connector:
        strategy = _Strategy()

        def discover_articles(self, article_ids=None):
            return [{"id": "A"}, {"id": "BOOM"}] if abort else [{"id": "A"}]

    def prepare(ctx, art):
        if art["id"] == "BOOM":
            raise RuntimeError("simulated crash")
        return flow.ArticlePrep(
            aid="A",
            chash=t.chash(),
            title_text=t.title,
            body_units=[SimpleNamespace(text=t.body, p_index="body")],
            extra_skeleton={},
            doc_title="doc A",
        )

    spec = flow.TranslateFlowSpec(
        kind="zendesk",
        no_conn_msg="no conn",
        hash_key="zendesk_hashes",
        probe_label="Zendesk",
        preflight=lambda ctx: ({"de"}, {}),
        prepare=prepare,
        is_suppressed=lambda connector, aid, loc: t.exists,
        build_payload=lambda prep, loc, seg0, mapping: {"title": seg0, **mapping},
        compute_draft=lambda cfg: True,
        compute_ok=lambda ctx: True,
        build_message=lambda ctx: "msg",
        build_result=lambda ctx: {"updated": ctx.updated},
        held_prefix="held-back:",
    )
    with (
        mock.patch.object(cstore, "build_fresh", return_value=_Connector()),
        mock.patch.object(translate_mod, "pretranslate_document", stub),
    ):
        return flow.run_translate_flow(
            spec, cid, transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )


def test_held_pair_records_no_hash():
    t = _Tenant()
    t.exists = True  # an existing variant: the held pair is updated (title only)
    t.hold_body = True
    _c, go, hashes = _make(t, "666 held no hash")
    _ok, _m, res = go()
    assert res["updated"] == 1 and not hashes(), ("#640: held pair records no hash", hashes())
    t.hold_body = False
    _ok, _m, res = go()
    assert hashes() == {"A:de": t.chash()}, hashes()
    print("  5. a held pair still records no hash (#640)")


def test_patch_config_merge_remove():
    conn = cstore.create(
        "zendesk",
        "666 merge",
        {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de"},
        {"email": "a@acme.com", "api_token": "tok123"},  # pregate: allow-secret throwaway
        actor="admin",
    )
    cstore.patch_config_merge(
        conn.id, "confluence_hashes", {"1:de-DE": "old", "1:de-de": "old", "2:de-de": "keep"}
    )

    def hs():
        return dict((cstore.get(conn.id).config or {}).get("confluence_hashes") or {})

    # exact removal leaves a case alias alone
    cstore.patch_config_merge(conn.id, "confluence_hashes", {}, hashes_remove=["1:de-de"])
    assert hs() == {"1:de-DE": "old", "2:de-de": "keep"}, hs()
    # casefold removal takes the alias too; a key in the delta wins over its removal
    cstore.patch_config_merge(
        conn.id,
        "confluence_hashes",
        {"2:de-de": "new"},
        hashes_remove=["1:de-de", "2:de-de"],
        remove_casefold=True,
    )
    assert hs() == {"2:de-de": "new"}, hs()
    print("  6. patch_config_merge: removal, casefold aliases on request, delta wins")


def test_unchanged_retry_keeps_hash():
    """Review gate: an explicit retry of an UNCHANGED, complete pair whose unit is now held
    keeps the stored hash (it equals this source's hash and was recorded by a complete write
    of the same text, which the held field still carries). Dropping it would re-translate the
    whole pair at full MT cost on every scheduled run while the unit stays held."""
    t = _Tenant()
    _c, go, hashes = _make(t, "666 unchanged retry")
    go()
    h1 = t.chash()
    assert hashes() == {"A:de": h1}, hashes()
    t.hold_body = True  # the retry's fresh MT of the body is held (a deterministic refusal)
    _ok, _m, res = go(only_pairs={("A", "de")})
    assert res["updated"] == 1 and t.writes[-1] == {"title": "T:Title v1"}, (res, t.writes)
    assert hashes() == {"A:de": h1}, ("an equal stored hash is kept", hashes())
    n_writes, n_mt = len(t.writes), t.mt_calls
    for _ in range(2):  # scheduled runs: suppressed, no MT spend loop
        _ok, _m, res = go()
        assert res["skipped"] == 1, res
    assert len(t.writes) == n_writes and t.mt_calls == n_mt, (t.writes, t.mt_calls)
    print("  9. an unchanged held retry keeps its hash: no MT re-spend on later runs")


def test_abort_keeps_complete_pairs():
    """An aborted run persists the hash of a pair it wrote COMPLETELY (#459) and drops
    nothing for it (#666 removes only incompletely written pairs)."""
    t = _Tenant()
    c, go, hashes = _make(t, "666 abort complete")
    go()
    t.title, t.body = "Title v2", "Body v2"
    try:
        _run_on(c.id, t, abort=True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the simulated crash must propagate")
    assert t.writes[-1] == {"title": "T:Title v2", "body": "T:Body v2"}, t.writes
    assert hashes() == {"A:de": t.chash()}, ("a complete pair keeps its new hash", hashes())
    print("  10. an aborted run keeps the new hash of a completely written pair")


_ADF_DESC = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Long description"}]}],
}


def test_jira_held_pair_writes_nothing():
    """Jira keeps its own hash map (store_part4). A pair with a held field writes NOTHING
    (field mode: one combined write; comment mode: one comment), so the stored hash keeps
    describing the target and a revert to that text is correctly suppressed (no write)."""
    src = {"summary": "Ticket v1"}
    puts = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        fields = {"summary": src["summary"], "description": _ADF_DESC}
        if path == "/rest/api/3/search/jql":
            return httpx.Response(
                200, json={"isLast": True, "issues": [{"key": "LOC-1", "fields": fields}]}
            )
        if path.startswith("/rest/api/3/issue/") and req.method == "GET":
            fld = req.url.params.get("fields", "summary")
            return httpx.Response(200, json={"key": "LOC-1", "fields": {fld: fields.get(fld)}})
        if path.startswith("/rest/api/3/issue/") and req.method == "PUT":
            puts.append(json.loads(req.content)["fields"])
            return httpx.Response(204)
        return httpx.Response(404, json={"path": path})

    conn = cstore.create(
        "jira",
        "666 jira",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary,description",
            "target_mode": "field",
            "target_field": "customfield_10050",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},  # pregate: allow-secret throwaway fixture
        actor="admin",
    )

    def run():
        return cstore.jira_translate(conn.id, transport=httpx.MockTransport(handler))

    def hashes():
        return dict((cstore.get(conn.id).config or {}).get("jira_hashes") or {})

    run()
    h1 = hashes().get("LOC-1:es-MX") or next(iter(hashes().values()), None)
    assert h1 and len(puts) == 1, (hashes(), puts)
    real = translate_mod.pretranslate_document

    def hold_description(session, doc_id, locale, *a, **kw):
        stats = real(session, doc_id, locale, *a, **kw)
        segs = session.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        segs.sort(key=lambda sg: sg.order_index)
        segs[-1].status = SegStatus.NEEDS_REVIEW
        session.add(segs[-1])
        session.commit()
        return stats

    src["summary"] = "Ticket v2"
    with mock.patch.object(translate_mod, "pretranslate_document", hold_description):
        run()
    assert len(puts) == 1, ("a held pair writes nothing to Jira", puts)
    assert h1 in hashes().values(), ("the stored hash still describes the target", hashes())
    src["summary"] = "Ticket v1"
    _ok, _msg, summ = run()
    assert len(puts) == 1 and summ["suppressed"] == 1, (puts, summ)
    print("  7. Jira: a held pair writes nothing, so a revert is rightly suppressed")


def test_confluence_spec_casefold():
    from app.services.connectors import confluence

    seen = {}

    def capture(spec, cid, **kw):
        seen["spec"] = spec
        return True, "", {}

    conn = cstore.create(
        "confluence",
        "666 confluence",
        {
            "base_url": "https://team.atlassian.net",
            "space_key": "HELP",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok-x"},  # pregate: allow-secret throwaway fixture
        actor="admin",
    )
    with mock.patch.object(confluence.flow, "run_translate_flow", capture):
        confluence.confluence_translate(conn.id)
    assert seen["spec"].hash_drop_casefold is True, seen
    print("  8. Confluence drops its legacy mixed-case hash aliases with the pair")


def main():
    init_db()
    test_revert_after_held_partial_rewrites()
    test_revert_after_write_incomplete_rewrites()
    test_abort_still_drops()
    test_held_pair_records_no_hash()
    test_patch_config_merge_remove()
    test_unchanged_retry_keeps_hash()
    test_abort_keeps_complete_pairs()
    test_jira_held_pair_writes_nothing()
    test_confluence_spec_casefold()
    print("verify_stale_hash_666 OK")


if __name__ == "__main__":
    main()
