"""Jira connector Phase 2 verification — fully offline (no network).

Covers the Phase-2 push-intake items from the planning notes §4 (J5/J6)
and the connectors-first plan, all hermetic (the generalized intake never does
network I/O — it verifies, dedups, and hands changed issue keys to an injected
enqueue callback; jira_translate runs against an httpx.MockTransport):

  1. INTAKE AUTH — a correct X-Loc-Webhook-Secret header (Automation path) is
     accepted; the WebSub X-Hub-Signature (raw-webhook path) is accepted; a
     wrong/missing secret -> a timing-equalized 401 (never 500, never leaks whether
     the connection exists).
  2. POSTURE — auto_translate_on_change OFF (default): a verified push records the
     changed issue(s) as PENDING and enqueues NOTHING; ON: it enqueues a targeted
     jira_translate with EXACTLY the payload's issues.
  3. DEDUP — a provider RETRY of the same delivery ACKs 200 and does not re-enqueue
     / re-record.
  4. PAYLOAD SHAPES — several plausible Automation/webhook body shapes all parse to
     the right issue key(s).
  5. CHANGELOG GATE — a status-only / comment-only changelog -> the intake ACKs a
     no-op (no pending, no enqueue); a description-change changelog -> accepted.
  6. PENDING RELEASE — jira_translate_pending translates exactly the pending set and
     clears it; a push landing DURING the run is preserved.
  7. LOCALHOST GUARD — _public_base_url() returns '' for unset/localhost and the
     real value otherwise (the connection page shows the tunnel guidance vs the URL).
  8. ORG SCOPING — an intake for a connection id that doesn't exist is an
     indistinguishable 401 (no cross-connection reach).
  9. EVENT-ID + FK-valid fixtures throughout (real Connection rows via cstore.create).

Run standalone (one process): python tests/verify_jira_p2.py  ->  prints "... OK".
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
from tests._piece_patch import set_pieces

init_env("loctms_jira_p2_")

import httpx
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import WebhookEvent
from app.services import settings
from app.services.connectors import jira as jira_mod
from app.services.connectors import store as cstore
from app.services.connectors import webhooks

SECRET = "a-very-long-random-shared-secret-value"


def _mk_conn(name, cfg_extra=None, with_secret=True):
    cfg = {
        "base_url": "https://team.atlassian.net",
        "jql": "project = LOC",
        "source_field": "summary,description",
        "target_mode": "field",
        "target_locales": "es-MX",
        "source_locale": "en-US",
    }
    cfg.update(cfg_extra or {})
    sec = {"email": "b@x.com", "api_token": "tok"}
    if with_secret:
        sec["webhook_secret"] = SECRET
    return cstore.create("jira", name, cfg, sec, actor="admin")


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _fake_route_enqueue(recorder):
    """Mirror web/api.py's _enqueue posture gate exactly: for a Jira connection
    without auto_translate_on_change, record pending + return None; else submit a
    (fake) job and return an id. `recorder` collects ('enqueued'|'pending', cid, ids)."""

    def _enqueue(cid, kind, record_ids):
        if kind == "jira":
            conn = cstore.get(cid)
            if conn is not None and not cstore.jira_auto_translate_on(conn):
                cstore.jira_record_pending(cid, record_ids)
                recorder.append(("pending", cid, list(record_ids)))
                return None
        recorder.append(("enqueued", cid, list(record_ids)))
        return 4242  # a fake job id

    return _enqueue


# ---------------------------------------------------------------------------
def test_intake_auth_and_posture_off():
    conn = _mk_conn("intake off")
    rec = []
    enq = _fake_route_enqueue(rec)

    # Correct shared-secret header (Automation path) -> accepted, PENDING recorded,
    # nothing enqueued (auto off by default).
    body = json.dumps({"issue_key": "LOC-1", "event": "updated"}).encode()
    res = webhooks.handle_intake(conn.id, body, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res.status == 200 and res.ok, (res.status, res.message)
    assert not res.enqueued and res.job_id is None, (
        "must NOT enqueue when auto off",
        res.as_dict(),
    )
    assert rec == [("pending", conn.id, ["LOC-1"])], rec
    assert cstore.jira_pending(conn.id) == ["LOC-1"], cstore.jira_pending(conn.id)
    print(
        "  auth+posture: correct header secret accepted -> PENDING recorded, nothing enqueued (auto OFF) OK"  # noqa: E501
    )

    # WebSub signature path (raw webhook) accepted too — new issue, still pending.
    body2 = json.dumps({"issue": {"key": "LOC-2"}}).encode()
    res2 = webhooks.handle_intake(conn.id, body2, {"X-Hub-Signature": _sign(body2)}, enqueue=enq)
    assert res2.status == 200 and res2.ok, (res2.status, res2.message)
    assert cstore.jira_pending(conn.id) == ["LOC-1", "LOC-2"], cstore.jira_pending(conn.id)
    print("  auth: WebSub X-Hub-Signature (raw-webhook path) also accepted OK")

    # Wrong secret -> 401, timing-equalized, nothing recorded/enqueued.
    before = list(rec)
    bad = webhooks.handle_intake(conn.id, body, {"X-Loc-Webhook-Secret": "nope"}, enqueue=enq)
    assert bad.status == 401 and not bad.ok, bad.as_dict()
    # Missing entirely -> 401.
    missing = webhooks.handle_intake(conn.id, body, {}, enqueue=enq)
    assert missing.status == 401 and not missing.ok, missing.as_dict()
    # Bad HMAC signature -> 401.
    badsig = webhooks.handle_intake(
        conn.id, body2, {"X-Hub-Signature": "sha256=deadbeef"}, enqueue=enq
    )
    assert badsig.status == 401, badsig.as_dict()
    assert rec == before, "a rejected delivery must not record or enqueue anything"
    print("  auth: wrong / missing / bad-signature -> 401, nothing recorded OK")


def test_posture_auto_on_enqueues_exact_issues():
    conn = _mk_conn("intake auto on", {"auto_translate_on_change": "on"})
    rec = []
    enq = _fake_route_enqueue(rec)
    body = json.dumps({"issue_keys": ["LOC-10", "LOC-11"], "event": "updated"}).encode()
    res = webhooks.handle_intake(conn.id, body, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res.status == 200 and res.enqueued and res.job_id == 4242, res.as_dict()
    assert rec == [("enqueued", conn.id, ["LOC-10", "LOC-11"])], rec
    assert cstore.jira_pending(conn.id) == [], "auto-on must not record pending"
    assert res.record_ids == ["LOC-10", "LOC-11"], res.record_ids
    print("  posture: auto ON -> targeted enqueue with EXACTLY the payload's issues, no pending OK")


def test_dedup_retry():
    conn = _mk_conn("intake dedup", {"auto_translate_on_change": "on"})
    rec = []
    enq = _fake_route_enqueue(rec)
    body = json.dumps({"issue_key": "LOC-20"}).encode()
    hdrs = {"X-Loc-Webhook-Secret": SECRET, "X-Atlassian-Webhook-Identifier": "evt-abc-123"}
    r1 = webhooks.handle_intake(conn.id, body, hdrs, enqueue=enq)
    assert r1.enqueued, r1.as_dict()
    # A retry of the SAME delivery id -> 200 ACK, but no second enqueue.
    r2 = webhooks.handle_intake(conn.id, body, hdrs, enqueue=enq)
    assert r2.status == 200 and not r2.enqueued, ("retry must ACK-but-not-enqueue", r2.as_dict())
    assert len(rec) == 1, ("exactly one enqueue across the retry", rec)
    with Session(engine) as s:
        rows = s.exec(select(WebhookEvent).where(WebhookEvent.connection_id == conn.id)).all()
    assert len(rows) == 1, ("one ledger row for the deduped event", len(rows))
    print(
        "  dedup: a provider retry (same X-Atlassian-Webhook-Identifier) ACKs, no double enqueue OK"
    )


def test_payload_shapes_parse():
    shapes = [
        ({"issue_key": "AA-1"}, ["AA-1"]),
        ({"issueKey": "AA-2"}, ["AA-2"]),
        ({"key": "AA-3"}, ["AA-3"]),
        ({"issue": {"key": "AA-4"}}, ["AA-4"]),
        ({"issue_keys": ["AA-5", "AA-6"]}, ["AA-5", "AA-6"]),
        ({"keys": ["AA-7"]}, ["AA-7"]),
        ({"issues": [{"key": "AA-8"}, "AA-9"]}, ["AA-8", "AA-9"]),
    ]
    for body_json, expected in shapes:
        got = jira_mod._jira_issue_keys(body_json)
        assert got == expected, (body_json, got, expected)
    # A body naming NO recognizable key parses empty (the intake ACKs a no-op).
    assert jira_mod._jira_issue_keys({"event": "updated"}) == []
    # A smart-value that failed to expand (still literal braces) is NOT treated as a key.
    assert jira_mod._jira_issue_keys({"issue_key": "{{issue.key}}"}) == []
    print("  shapes: 7 plausible Automation/webhook body shapes parse to the right keys OK")


def test_changelog_gate_status_only():
    conn = _mk_conn("intake changelog")
    rec = []
    enq = _fake_route_enqueue(rec)
    # status-only changelog -> the changed-ids hook returns [] -> intake ACKs no-op:
    # no pending, no enqueue.
    body = json.dumps(
        {
            "issue_key": "LOC-30",
            "changelog": {
                "items": [{"field": "status", "fromString": "To Do", "toString": "Done"}]
            },
        }
    ).encode()
    res = webhooks.handle_intake(conn.id, body, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res.status == 200 and res.ok and not res.enqueued, res.as_dict()
    assert rec == [], ("status-only change must not enqueue or record pending", rec)
    assert cstore.jira_pending(conn.id) == [], cstore.jira_pending(conn.id)

    # comment-only changelog -> same no-op.
    body_c = json.dumps(
        {"issue_key": "LOC-30", "changelog": {"items": [{"field": "Comment"}]}}
    ).encode()
    res_c = webhooks.handle_intake(conn.id, body_c, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res_c.status == 200 and not res_c.enqueued and rec == [], res_c.as_dict()

    # F5 (review): "Fix Version/s"-only changelog (Jira's real changelog field
    # display name for the fix-version field) -> also a no-op, same as status/comment.
    body_fv = json.dumps(
        {
            "issue_key": "LOC-30",
            "changelog": {
                "items": [{"field": "Fix Version/s", "fromString": None, "toString": "1.0"}]
            },
        }
    ).encode()
    res_fv = webhooks.handle_intake(conn.id, body_fv, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res_fv.status == 200 and not res_fv.enqueued and rec == [], res_fv.as_dict()
    assert cstore.jira_pending(conn.id) == [], cstore.jira_pending(conn.id)

    # description-change changelog -> accepted, pending recorded.
    body_d = json.dumps(
        {
            "issue_key": "LOC-31",
            "changelog": {"items": [{"field": "status"}, {"field": "description"}]},
        }
    ).encode()
    res_d = webhooks.handle_intake(conn.id, body_d, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res_d.status == 200 and res_d.ok, res_d.as_dict()
    assert cstore.jira_pending(conn.id) == ["LOC-31"], cstore.jira_pending(conn.id)
    print(
        "  changelog: status-only/comment-only -> no re-translate; description-change -> accepted OK"  # noqa: E501
    )


def test_pending_release_translates_exact_and_clears():
    # A Jira transport that serves the two pending issues and captures field PUTs.
    def transport(captured):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                # jira_translate passes issue_keys -> fetch_issues builds a key IN (...)
                # JQL; return exactly the two issues.
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [
                            {"key": "LOC-40", "fields": {"summary": "Alpha", "description": None}},
                            {"key": "LOC-41", "fields": {"summary": "Beta", "description": None}},
                        ],
                    },
                )
            if req.method == "PUT" and path.startswith("/rest/api/3/issue/"):
                captured.append(path.rsplit("/", 1)[-1])
                return httpx.Response(204)
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    conn = _mk_conn(
        "pending release",
        {"source_field": "summary", "target_mode": "field", "target_field": "customfield_10050"},
    )
    # Seed pending directly (as a verified push would).
    cstore.jira_record_pending(conn.id, ["LOC-40", "LOC-41"])
    assert cstore.jira_pending(conn.id) == ["LOC-40", "LOC-41"]
    captured = []
    ok, msg, summary = cstore.jira_translate_pending(
        conn.id, actor="auto", transport=transport(captured)
    )
    assert ok, msg
    assert summary.get("pending") == 2, summary
    assert set(captured) == {"LOC-40", "LOC-41"}, (
        "translated exactly the pending issues",
        captured,
    )
    assert cstore.jira_pending(conn.id) == [], (
        "pending cleared on success",
        cstore.jira_pending(conn.id),
    )
    print("  pending release: translates exactly the pending set, then clears it OK")

    # Empty pending -> ok no-op.
    ok2, msg2, sum2 = cstore.jira_translate_pending(conn.id, actor="auto")
    assert ok2 and sum2.get("pending") == 0 and "No pending" in msg2, (ok2, msg2, sum2)
    print("  pending release: empty set -> ok no-op OK")


def test_pending_release_preserves_concurrent_push():
    # jira_record_pending after the read-of-pending but the clear removes only the
    # keys we processed — so a key added mid-run survives. Simulate by monkeypatching
    # jira_translate to add a new pending key while "running".
    conn = _mk_conn("pending concurrent", {"source_field": "summary", "target_mode": "comment"})
    cstore.jira_record_pending(conn.id, ["LOC-50"])

    real = cstore.jira_translate

    def fake_translate(cid, issue_keys=None, **kw):
        # a push lands DURING the run, adding LOC-99 to pending
        cstore.jira_record_pending(cid, ["LOC-99"])
        return True, "ok", {"written": 1}

    set_pieces("app.services.connectors.store", "jira_translate", fake_translate)
    try:
        ok, msg, _summary = cstore.jira_translate_pending(conn.id, actor="auto")
    finally:
        set_pieces("app.services.connectors.store", "jira_translate", real)
    assert ok, msg
    # LOC-50 was processed and cleared; LOC-99 (added mid-run) is preserved.
    assert cstore.jira_pending(conn.id) == ["LOC-99"], cstore.jira_pending(conn.id)
    print(
        "  pending release: a push landing during the run is preserved (only processed keys cleared) OK"  # noqa: E501
    )


def test_localhost_guard():
    from app.web.connectors import _public_base_url

    # unset -> ''
    settings.set_many({"app_base_url": ""})
    assert _public_base_url() == "", "unset app_base_url -> no public URL"
    # localhost variants -> '' (Atlassian's cloud can't reach them)
    for bad in ("http://localhost:8000", "http://127.0.0.1:8000", "https://myapp.local"):
        settings.set_many({"app_base_url": bad})
        assert _public_base_url() == "", ("localhost-ish must be treated as no public URL", bad)
    # a real tunnel URL -> returned without trailing slash
    settings.set_many({"app_base_url": "https://abc123.trycloudflare.com/"})
    assert _public_base_url() == "https://abc123.trycloudflare.com", _public_base_url()
    settings.set_many({"app_base_url": ""})  # reset
    print(
        "  localhost guard: unset/localhost -> '' (tunnel guidance); real URL returned trimmed OK"
    )


def test_org_scoping_unknown_connection_is_401():
    rec = []
    enq = _fake_route_enqueue(rec)
    # A connection id that does not exist -> indistinguishable 401 (no existence leak,
    # no reach into another connection). Use a large id no fixture created.
    body = json.dumps({"issue_key": "X-1"}).encode()
    res = webhooks.handle_intake(999999, body, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res.status == 401 and not res.ok, res.as_dict()
    assert rec == [], "unknown connection must not enqueue"
    # A connection with NO webhook_secret set -> intake fails closed (401) even with a
    # header, because verify_signature rejects a blank secret.
    nosec = _mk_conn("no secret", with_secret=False)
    res2 = webhooks.handle_intake(nosec.id, body, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res2.status == 401, ("no vaulted secret -> 401", res2.as_dict())
    print("  org scoping: unknown connection + secret-less connection both -> 401, no reach OK")


def test_issue_key_injection_and_comma_smuggling_rejected():
    # F1(a) (adversarial review — JQL injection / scope escape). The old gate only
    # rejected '{' and ' ', so a JQL-injection payload or a comma-smuggled pair of
    # keys passed straight through _jira_issue_keys and would have interpolated raw
    # into fetch_issues' `key in (...)` clause. The strict ABC-123 fullmatch (F1(a)
    # fix) must reject both, never reaching a search.
    injection = "LOC-1)OR(project=SECRET"
    assert jira_mod._jira_issue_keys({"issue_key": injection}) == [], (
        "a JQL-injection-shaped value must never be accepted as an issue key"
    )
    comma_smuggle = "A-1,B-2"
    assert jira_mod._jira_issue_keys({"issue_key": comma_smuggle}) == [], (
        "a comma-smuggled value must never silently become two keys"
    )
    # A genuinely well-formed key alongside a hostile one: only the good one survives.
    got = jira_mod._jira_issue_keys({"issue_keys": [injection, "LOC-2", comma_smuggle]})
    assert got == ["LOC-2"], got
    # Also via the intake end-to-end: a verified push naming only an injection string
    # parses to NO keys, so the intake ACKs a no-op (no pending, no enqueue, no search).
    conn = _mk_conn("intake injection")
    rec = []
    enq = _fake_route_enqueue(rec)
    body = json.dumps({"issue_key": injection}).encode()
    res = webhooks.handle_intake(conn.id, body, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res.status == 200 and res.record_ids == [], res.as_dict()
    assert rec == [], ("an injection-only payload must record/enqueue nothing", rec)
    print("  injection/comma-smuggling: rejected by _jira_issue_keys, never reach a search OK")


def test_fetch_issues_scope_ands_connection_jql():
    # F1(b) (adversarial review — scope escape). fetch_issues on the issue_keys path
    # must AND the connection's configured jql onto `key in (...)` — a signed push
    # naming an issue outside that scope must not translate it, even though the
    # token itself could read it.
    sent_jql = {}

    def transport(in_scope_keys):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                jql = req.url.params.get("jql") or ""
                sent_jql["jql"] = jql
                # Emulate the Jira SERVER applying the AND: only keys in
                # in_scope_keys are ever returned, no matter what the client asked for.
                issues = [
                    {"key": k, "fields": {"summary": f"S-{k}", "description": None}}
                    for k in in_scope_keys
                ]
                return httpx.Response(200, json={"isLast": True, "issues": issues})
            if req.method == "POST" and "/comment" in path:
                return httpx.Response(201)
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    conn = _mk_conn(
        "scope and", {"jql": "project = LOC", "source_field": "summary", "target_mode": "comment"}
    )

    # In-scope key: the connection's jql "project = LOC" is ANDed in, the (fake)
    # server still returns it (it's really in LOC) -> translates.
    ok, msg, summary = cstore.jira_translate(
        conn.id, issue_keys=["LOC-1"], actor="auto", transport=transport(["LOC-1"])
    )
    assert ok, msg
    assert summary.get("written", 0) >= 1, summary
    assert (
        "key in (LOC-1)" in sent_jql["jql"]
        and "AND" in sent_jql["jql"]
        and "project = LOC" in sent_jql["jql"]
    ), sent_jql
    print("  scope AND: in-scope key -> clause ANDs connection jql, translates OK")

    # Out-of-scope key: the client asks for a key outside the connection's project;
    # the ANDed clause means the (fake) server matching that AND returns NOTHING for
    # it -> clean per-key no-result, not an error, not a translate of the wrong issue.
    ok2, msg2, summary2 = cstore.jira_translate(
        conn.id, issue_keys=["SECRET-9"], actor="auto", transport=transport([])
    )
    assert ok2, msg2
    assert summary2.get("written", 0) == 0 and summary2.get("translated", 0) == 0, summary2
    assert "key in (SECRET-9)" in sent_jql["jql"] and "project = LOC" in sent_jql["jql"], sent_jql
    print(
        "  scope AND: out-of-scope key -> clean no-result under the ANDed jql, never translated OK"
    )

    # No jql configured on the connection -> keys-only clause, unchanged (documented
    # posture: token visibility is the declared scope when the operator sets none).
    conn2 = _mk_conn(
        "scope and no jql", {"jql": "", "source_field": "summary", "target_mode": "comment"}
    )
    ok3, msg3, _summary3 = cstore.jira_translate(
        conn2.id, issue_keys=["LOC-5"], actor="auto", transport=transport(["LOC-5"])
    )
    assert ok3, msg3
    assert sent_jql["jql"] == "key in (LOC-5)", sent_jql
    print("  scope AND: no connection jql configured -> keys-only clause (documented) OK")


def test_free_query_jql_scope_ands_connection_jql():
    # B1 (adversarial audit — scope escape, free-query path). A caller-supplied `jql`
    # with NO issue_keys (an /api caller's `jql`, the CLI `--jql`) previously BECAME
    # the whole clause and REPLACED the connection's declared scope entirely, so a
    # scoped-but-authorized caller (e.g. a linguist key) could search/translate/
    # comment on ANY issue the credential can read. The free-query branch must now AND
    # the connection's configured jql onto the caller jql, exactly like the issue_keys
    # branch — proven confined here.
    sent_jql = {}

    def transport(in_scope_keys):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                jql = req.url.params.get("jql") or ""
                sent_jql["jql"] = jql
                # The (fake) server returns only in-scope keys, emulating Jira applying
                # the ANDed clause: whatever the client asked for, scope wins.
                issues = [
                    {"key": k, "fields": {"summary": f"S-{k}", "description": None}}
                    for k in in_scope_keys
                ]
                return httpx.Response(200, json={"isLast": True, "issues": issues})
            if req.method == "POST" and "/comment" in path:
                return httpx.Response(201)
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    conn = _mk_conn(
        "free-query scope",
        {"jql": "project = LOC", "source_field": "summary", "target_mode": "comment"},
    )

    # A HOSTILE free-query jql aimed at another project. The connection's scope is
    # ANDed in, so the clause is `(project = SECRET) AND (project = LOC)` -> the server
    # matches NOTHING -> nothing translated, and critically the SECRET clause never
    # ran unscoped.
    ok, msg, summary = cstore.jira_translate(
        conn.id, jql="project = SECRET", actor="linguist", transport=transport([])
    )
    assert ok, msg
    assert summary.get("written", 0) == 0 and summary.get("translated", 0) == 0, summary
    assert (
        "(project = SECRET)" in sent_jql["jql"]
        and "AND" in sent_jql["jql"]
        and "(project = LOC)" in sent_jql["jql"]
    ), ("caller jql must be ANDed with the connection scope, both parenthesized", sent_jql)
    print(
        "  B1 free-query: hostile caller jql -> ANDed under connection scope, translates nothing OK"
    )

    # A LEGITIMATE in-scope free-query (a narrower filter WITHIN the scope) still works:
    # the clause intersects and the server returns the in-scope issue -> it translates.
    ok2, msg2, summary2 = cstore.jira_translate(
        conn.id, jql="status = 'To Localize'", actor="linguist", transport=transport(["LOC-7"])
    )
    assert ok2, msg2
    assert summary2.get("written", 0) >= 1, summary2
    assert "(status = 'To Localize')" in sent_jql["jql"] and "(project = LOC)" in sent_jql["jql"], (
        sent_jql
    )
    print("  B1 free-query: in-scope narrower jql -> intersected with scope, translates OK")

    # An OR-injection attempt: a bare OR in the caller jql must NOT re-associate past
    # the AND and widen the match — the parens around the caller clause contain it.
    ok3, msg3, _summary3 = cstore.jira_translate(
        conn.id, jql="project = A OR project = SECRET", actor="linguist", transport=transport([])
    )
    assert ok3, msg3
    assert "(project = A OR project = SECRET) AND (project = LOC)" in sent_jql["jql"], (
        "caller OR must stay parenthesized inside the AND, never widening the scope",
        sent_jql,
    )
    print("  B1 free-query: caller `OR` stays parenthesized, cannot widen the ANDed scope OK")

    # No caller jql AND a scope configured -> the scope IS the query (never an unscoped
    # full pull). A bare `order by` fallback is only used when NO scope is set.
    ok4, msg4, _summary4 = cstore.jira_translate(
        conn.id, jql=None, actor="auto", transport=transport(["LOC-9"])
    )
    assert ok4, msg4
    assert sent_jql["jql"] == "project = LOC", (
        "no caller jql -> the configured scope is the whole query, not order-by-created",
        sent_jql,
    )
    print("  B1 free-query: no caller jql -> scope alone is the query (no unscoped full pull) OK")

    # No scope configured -> the caller jql stands as-is (documented posture: the
    # token's own visibility is the declared scope). No AND, no parens.
    conn2 = _mk_conn(
        "free-query no scope", {"jql": "", "source_field": "summary", "target_mode": "comment"}
    )
    ok5, msg5, _summary5 = cstore.jira_translate(
        conn2.id, jql="project = ANY", actor="auto", transport=transport(["ANY-1"])
    )
    assert ok5, msg5
    assert sent_jql["jql"] == "project = ANY", (
        "no scope configured -> caller jql unchanged (documented posture)",
        sent_jql,
    )
    print("  B1 free-query: no connection scope -> caller jql unchanged (documented) OK")


# --- a JQL-FAITHFUL mock: actually evaluates the effective clause against a fixture
# of in-scope AND out-of-scope issues, with JQL's real precedence (AND binds tighter
# than OR) and parentheses. This is what makes the paren-breaking attack observable:
# a broken `(caller) AND (scope)` re-associates to `caller OR scoped` and this evaluator
# will RETURN the out-of-scope issue, and the run will POST a comment on it. ---
def _eval_jql(clause: str, issue: dict) -> bool:
    """Evaluate a tiny JQL subset over one issue dict {'key','project'}: the operators
    `project = X`, `key in (K1,K2)`, `status = '...'` (always True — a pure narrower),
    grouped by parens, joined by AND/OR with AND binding tighter than OR (JQL's real
    precedence). Enough to make paren-breaking re-association observable."""
    import re as _re

    s = clause.strip()
    # strip a trailing `order by ...`
    s = _re.split(r"\border\s+by\b", s, flags=_re.I)[0].strip()
    pos = 0

    def peek():
        nonlocal pos
        while pos < len(s) and s[pos].isspace():
            pos += 1
        return s[pos] if pos < len(s) else ""

    def parse_or():
        val = parse_and()
        while True:
            save = pos
            tok = _word()
            if tok.upper() == "OR":
                val = parse_and() or val
            else:
                _restore(save)
                return val

    def parse_and():
        val = parse_not()
        while True:
            save = pos
            tok = _word()
            if tok.upper() == "AND":
                rhs = parse_not()
                val = val and rhs
            else:
                _restore(save)
                return val

    def parse_not():
        nonlocal pos
        if peek() == "(":
            pos += 1  # consume '('
            v = parse_or()
            if peek() == ")":
                pos += 1
            return v
        return parse_pred()

    def _word():
        nonlocal pos
        peek()
        start = pos
        while pos < len(s) and (s[pos].isalnum() or s[pos] in "_-"):
            pos += 1
        return s[start:pos]

    def _restore(p):
        nonlocal pos
        pos = p

    def parse_pred():
        nonlocal pos
        peek()
        # read up to the next boolean/paren as the predicate text
        start = pos
        while pos < len(s):
            ch = s[pos]
            if ch in "()":
                break
            # stop at a top-level AND/OR keyword
            rest = s[pos:]
            um = rest[:4].upper()
            if um.startswith("AND") and (len(rest) <= 3 or not rest[3].isalnum()):
                break
            if um.startswith("OR") and (len(rest) <= 2 or not rest[2].isalnum()):
                break
            pos += 1
        pred = s[start:pos].strip()
        return _match_pred(pred, issue)

    def _match_pred(pred: str, iss: dict) -> bool:
        pl = pred.lower()
        m = _re.match(r"project\s*=\s*['\"]?([A-Za-z0-9_-]+)", pred, _re.I)
        if m:
            return iss.get("project", "").upper() == m.group(1).upper()
        m = _re.match(r"key\s+in\s*\(([^)]*)\)", pred, _re.I)
        if m:
            keys = [k.strip().strip("'\"") for k in m.group(1).split(",")]
            return iss.get("key") in keys
        if pl.startswith("status"):  # noqa: SIM103
            return True  # a pure narrower — never widens scope
        return False

    try:
        result = parse_or()
    except Exception:
        return False
    return bool(result)


def test_free_query_jql_paren_break_confined_against_faithful_mock():
    # HIGH (adversarial review — paren-breaking JQL injection). The prior fix wrapped
    # the caller jql in parens and ANDed the scope, but the CALLER can supply its OWN
    # parens: `project = SECRET) OR (project = LOC` makes `(caller) AND (scope)` ==
    # `project = SECRET OR (scoped)` (AND binds tighter than OR), so SECRET runs
    # UNSCOPED. This test runs that exact payload against a JQL-FAITHFUL mock that
    # evaluates the effective clause and would RETURN + let us POST on SECRET-9 if the
    # escape were open. It must be REFUSED before any search runs.
    FIXTURE = [
        {"key": "LOC-1", "project": "LOC"},  # in scope
        {"key": "SECRET-9", "project": "SECRET"},  # OUT of scope
    ]
    reads, writes = [], []

    def faithful_transport():
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                clause = req.url.params.get("jql") or ""
                matched = [i for i in FIXTURE if _eval_jql(clause, i)]
                reads.extend(i["key"] for i in matched)
                issues = [
                    {"key": i["key"], "fields": {"summary": f"S-{i['key']}", "description": None}}
                    for i in matched
                ]
                return httpx.Response(200, json={"isLast": True, "issues": issues})
            if req.method == "POST" and "/comment" in path:
                writes.append(path.split("/issue/")[1].split("/")[0])
                return httpx.Response(201)
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    # sanity: the faithful evaluator DOES model the vulnerability — the broken clause
    # the OLD code would have built returns the out-of-scope issue.
    broken = "(project = SECRET) OR (project = LOC) AND (project = LOC)"
    assert _eval_jql(broken, FIXTURE[1]) is True, (
        "faithful mock must reproduce the escape (else the test proves nothing)"
    )

    conn = _mk_conn(
        "paren-break jql",
        {"jql": "project = LOC", "source_field": "summary", "target_mode": "comment"},
    )

    # THE ATTACK: a paren-breaking caller jql. Must be refused -> ok False, and CRUCIALLY
    # no search ran against SECRET and no comment was posted on SECRET-9.
    ok, msg, summary = cstore.jira_translate(
        conn.id,
        jql="project = SECRET) OR (project = LOC",
        actor="attacker",
        transport=faithful_transport(),
    )
    assert not ok, ("paren-breaking caller jql must be REFUSED", msg, summary)
    assert "parenthes" in msg.lower(), ("error must name the parenthesis rejection", msg)
    assert reads == [], ("NO issue may be read under a refused query", reads)
    assert writes == [], ("NO out-of-scope comment may be posted", writes)
    assert "SECRET-9" not in reads and "SECRET-9" not in writes
    print(
        "  B1 attack: paren-breaking caller jql REFUSED -> zero reads, zero writes, no SECRET-9 OK"
    )

    # A legitimate paren-free narrower still translates the in-scope issue and never
    # the out-of-scope one (the faithful mock confirms the AND actually confines).
    reads.clear()
    writes.clear()
    ok2, msg2, _summary2 = cstore.jira_translate(
        conn.id, jql="status = 'To Localize'", actor="linguist", transport=faithful_transport()
    )
    assert ok2, msg2
    assert "LOC-1" in reads and "SECRET-9" not in reads, ("scope confines the faithful mock", reads)
    assert writes == ["LOC-1"], ("only the in-scope issue is written", writes)
    print("  B1 attack: paren-free narrower -> only in-scope LOC-1 read+written, SECRET-9 never OK")

    # The reject-parens validation raises the expected ConnectorError directly at the
    # connector seam (so /api surfaces a 400 and the CLI a usage error), independent of
    # the store wiring.
    from app.services.connectors.base import ConnectorError as _CE

    jr = jira_mod.JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=faithful_transport(),
    )
    raised = False
    try:
        jr.fetch_issues(jql="a) OR (b", scope_jql="project = LOC")
    except _CE as e:
        raised = "parenthes" in str(e).lower()
    assert raised, "fetch_issues must raise ConnectorError on a paren-containing scoped caller jql"
    # an unbalanced quote is likewise refused
    raised_q = False
    try:
        jr.fetch_issues(jql="summary ~ 'oops", scope_jql="project = LOC")
    except _CE as e:
        raised_q = "quote" in str(e).lower()
    assert raised_q, (
        "fetch_issues must raise ConnectorError on an unbalanced-quote scoped caller jql"
    )
    print("  B1 attack: fetch_issues raises ConnectorError on parens AND on unbalanced quotes OK")


def test_pending_release_double_click_coalesces():
    # F2 (adversarial review — double-post). Two rapid "Translate pending now"
    # submissions must coalesce into ONE enqueued job, keyed on the COMPOUND
    # (connection_id, jira_translate_pending) — not connection_id alone (which
    # would also coalesce onto an unrelated in-flight job for the same connection).
    from app.models import Job, JobStatus
    from app.services import jobs as jobs_mod

    conn = _mk_conn("pending dedup")
    cstore.jira_record_pending(conn.id, ["LOC-70"])

    payload = {"connection_id": conn.id, "actor": "web", "jira_translate_pending": True}
    with Session(engine) as s:
        # Seed a QUEUED job directly (as the first click's submit_unique would have
        # left behind) so the second click's dedup check has something live to hit.
        first = Job(
            kind="connector_translate", payload=payload, status=JobStatus.QUEUED, created_by="web"
        )
        s.add(first)
        s.commit()
        s.refresh(first)
        first_id = first.id

    jid2, created2 = jobs_mod.submit_unique(
        "connector_translate",
        dict(payload),
        actor="web",
        dedupe_key=("connection_id", "jira_translate_pending"),
    )
    assert not created2 and jid2 == first_id, (jid2, created2, first_id)

    # A DIFFERENT job kind-of-work for the SAME connection (a plain full sync, no
    # jira_translate_pending flag) must NOT be coalesced away by the compound key —
    # proves the fix doesn't just recreate the old connection_id-only behavior.
    sync_payload = {"connection_id": conn.id, "actor": "web"}
    jid3, created3 = jobs_mod.submit_unique(
        "connector_translate",
        sync_payload,
        actor="web",
        dedupe_key=("connection_id", "jira_translate_pending"),
    )
    assert created3 and jid3 != first_id, (jid3, created3, first_id)
    print(
        "  pending dedup: two rapid releases -> one job; an unrelated sync for the same connection still runs OK"  # noqa: E501
    )


def test_jobs_dispatch_unmapped_kind_fails_loud():
    # F3 (adversarial review — durable dispatch shape). A kind with no explicit
    # webhook-dispatch mapping (e.g. Airtable, whose verifier exists but isn't wired
    # into this dispatch yet) must FAIL the job loud instead of silently misrouting
    # into zendesk_translate.
    from app.services import jobs as jobs_mod

    conn = cstore.create(
        "airtable",
        "unmapped dispatch kind",
        {"base_id": "appXXXX", "table": "Strings"},
        {"api_key": "tok"},
        actor="admin",
    )
    called = {"zendesk": False}
    real_zendesk = cstore.zendesk_translate

    def spy_zendesk(*a, **kw):
        called["zendesk"] = True
        return real_zendesk(*a, **kw)

    set_pieces("app.services.connectors.store", "zendesk_translate", spy_zendesk)
    try:
        payload = {"connection_id": conn.id, "actor": "webhook", "webhook_record_ids": ["rec123"]}
        try:
            jobs_mod._run_connector_translate(payload, lambda *a, **k: None)
            raised = False
        except RuntimeError as e:
            raised = True
            assert "no webhook dispatch for kind" in str(e) and "airtable" in str(e), str(e)
    finally:
        set_pieces("app.services.connectors.store", "zendesk_translate", real_zendesk)
    assert raised, "an unmapped kind must fail loud, not silently succeed"
    assert not called["zendesk"], "an unmapped kind must never fall through to zendesk_translate"
    print(
        "  jobs dispatch: unmapped kind (airtable) fails loud with a clear message, no zendesk call OK"  # noqa: E501
    )


def test_jobs_dispatch_routes_jira():
    # The connector_translate job handler must route a Jira webhook payload to
    # jira_translate (not zendesk_translate). Assert by monkeypatching the store.
    from app.services import jobs as jobs_mod

    conn = _mk_conn("jobs dispatch", {"auto_translate_on_change": "on"})
    called = {}

    def fake_jira_translate(cid, issue_keys=None, **kw):
        called["cid"] = cid
        called["ids"] = list(issue_keys or [])
        return True, "ok", {"written": len(issue_keys or [])}

    real = cstore.jira_translate
    set_pieces("app.services.connectors.store", "jira_translate", fake_jira_translate)
    try:
        payload = {
            "connection_id": conn.id,
            "actor": "webhook",
            "webhook_record_ids": ["LOC-60", "LOC-61"],
            "webhook_article_ids": ["LOC-60", "LOC-61"],
        }
        out = jobs_mod._run_connector_translate(payload, lambda *a, **k: None)
    finally:
        set_pieces("app.services.connectors.store", "jira_translate", real)
    assert called.get("cid") == conn.id, called
    assert called.get("ids") == ["LOC-60", "LOC-61"], called
    assert out.get("ok"), out
    print(
        "  jobs: a Jira webhook payload dispatches to jira_translate with the payload's issues OK"
    )


def test_d4_button_trigger_replay_deduped():
    # D4 (audit) — the DEFAULT Jira posture is button-trigger (auto_translate OFF):
    # a verified push records PENDING and enqueues NO job, so the ledger row's job_id
    # stays NULL. Pre-fix, record_event keyed retryability on `job_id IS NULL`, so a
    # button-trigger event NEVER settled and every replay of one captured, validly-
    # signed POST was re-accepted as fresh (unbounded replay). The fix settles on an
    # explicit processed_at marker. This test FAILS on pre-fix code: the replay
    # re-records PENDING so `len(rec) == 2` (verified against the pre-fix tree).
    conn = _mk_conn("d4 button-trigger dedup")  # auto OFF (default posture)
    rec = []
    enq = _fake_route_enqueue(rec)
    body = json.dumps({"issue_key": "LOC-4001", "changelog": {"id": "4001"}}).encode()
    hdrs = {"X-Loc-Webhook-Secret": SECRET, "X-Atlassian-Webhook-Identifier": "evt-d4-replay"}
    r1 = webhooks.handle_intake(conn.id, body, hdrs, enqueue=enq)
    assert r1.status == 200 and not r1.enqueued and r1.job_id is None, r1.as_dict()
    assert rec == [("pending", conn.id, ["LOC-4001"])], rec

    # REPLAY the exact captured signed POST -> must be a SETTLED duplicate: ACK 200,
    # enqueue nothing more, record no second pending.
    r2 = webhooks.handle_intake(conn.id, body, hdrs, enqueue=enq)
    assert r2.status == 200 and "duplicate" in r2.message.lower(), (
        "a replay under button-trigger must be a settled duplicate, not re-accepted",
        r2.as_dict(),
    )
    assert len(rec) == 1, ("the replay must NOT re-enqueue / re-record pending", rec)

    with Session(engine) as s:
        rows = s.exec(select(WebhookEvent).where(WebhookEvent.connection_id == conn.id)).all()
    assert len(rows) == 1, ("exactly one ledger row for the deduped event", len(rows))
    assert rows[0].processed_at is not None, (
        "the event must be SETTLED (processed_at set) even though NO job was enqueued",
        rows[0],
    )
    assert rows[0].job_id is None, ("button-trigger settles WITHOUT a job id", rows[0].job_id)
    print(
        "  D4 dedup: a button-trigger event settles (processed_at) -> replay deduped, no re-enqueue OK"  # noqa: E501
    )


def test_d4_crash_window_recovery_preserved():
    # D4 (audit) — the settle marker must NOT regress the crash-window recovery: an
    # event recorded but NEVER settled (the prior attempt died between record_event
    # and settle_event) stays retryable, so a provider retry re-processes it and no
    # event is lost. Once settled, further replays dedup.
    conn = _mk_conn("d4 crash window")
    assert webhooks.record_event(conn.id, "jira", "evt-crash") is True, (
        "first delivery of a fresh event must be processable"
    )
    # NO settle_event() here -> processed_at stays NULL (simulates a crash before settle).
    assert webhooks.record_event(conn.id, "jira", "evt-crash") is True, (
        "an unsettled prior (crash before settle) must be RETRYABLE, not deduped"
    )
    # Now settle it; any further replay is a duplicate.
    webhooks.settle_event(conn.id, "evt-crash", job_id=None)
    assert webhooks.record_event(conn.id, "jira", "evt-crash") is False, (
        "a SETTLED event must dedup"
    )
    with Session(engine) as s:
        rows = s.exec(select(WebhookEvent).where(WebhookEvent.connection_id == conn.id)).all()
    assert len(rows) == 1 and rows[0].processed_at is not None, rows
    print("  D4 crash-window: unsettled prior stays retryable; settled prior dedups OK")


def test_weak_key_button_trigger_reedit_not_lost():
    # HIGH regression (adversarial review, LIVE REPRO). The maintainer's PRIMARY path: a Jira
    # Automation "Send web request" with a THIN body ({"issue_key":"LOC-1"}) + the
    # X-Loc-Webhook-Secret header, NO X-Atlassian-Webhook-Identifier and NO changelog.
    # dedup_key then falls back to sha256(body) — a WEAK key that is a pure function of
    # the body with no time component. Under the default button-trigger posture the
    # first edit settles the ledger row with job_id NULL; pre-fix, EVERY later genuine
    # re-edit produced a byte-identical body -> the same weak event_id -> a settled
    # duplicate -> silently DROPPED FOREVER. The fix: a settled WEAK-keyed event on the
    # no-job path does NOT suppress a new delivery, so a genuine re-edit is re-accepted
    # (jira_record_pending is an idempotent set-merge). Reviewer repro exactly: two
    # identical thin-body signed POSTs separated by a release of the pending set -> the
    # SECOND is accepted and the issue is pending again. FAILS on the pre-fix tree (the
    # second POST is "duplicate event ignored" and the issue never returns to pending).
    conn = _mk_conn("weak-key reedit")  # auto OFF (default button-trigger posture)
    rec = []
    enq = _fake_route_enqueue(rec)
    body = json.dumps({"issue_key": "LOC-1"}).encode()
    hdrs = {"X-Loc-Webhook-Secret": SECRET}  # NO Atlassian id, NO changelog -> weak key
    # Guard: this repro MUST exercise the weak body-hash key (else it proves nothing).
    ev = webhooks.dedup_key("jira", body, json.loads(body.decode()), hdrs)
    assert webhooks.is_weak_key(ev), ("repro must exercise the weak body-hash key", ev)

    # First edit -> accepted, LOC-1 pending.
    r1 = webhooks.handle_intake(conn.id, body, hdrs, enqueue=enq)
    assert r1.status == 200 and r1.ok, r1.as_dict()
    assert cstore.jira_pending(conn.id) == ["LOC-1"], cstore.jira_pending(conn.id)

    # The operator RELEASES the pending set (clicks "Translate pending now") -> drained.
    cstore.patch_config(conn.id, {"jira_pending": []})
    assert cstore.jira_pending(conn.id) == [], "pending drained after release"

    # A genuine LATER re-edit of the SAME issue produces a byte-identical body -> the
    # same weak key. It MUST be accepted (not a settled duplicate) and LOC-1 pending
    # again — otherwise the edit is lost forever.
    r2 = webhooks.handle_intake(conn.id, body, hdrs, enqueue=enq)
    assert r2.status == 200 and r2.ok, r2.as_dict()
    assert "duplicate" not in r2.message.lower(), (
        "a genuine weak-key re-edit on the no-job path must NOT be a duplicate",
        r2.as_dict(),
    )
    assert "accepted" in r2.message.lower(), r2.as_dict()
    assert cstore.jira_pending(conn.id) == ["LOC-1"], (
        "the re-edit must return the issue to pending, not be dropped",
        cstore.jira_pending(conn.id),
    )
    assert rec == [("pending", conn.id, ["LOC-1"]), ("pending", conn.id, ["LOC-1"])], (
        "both deliveries re-record pending (the second is not suppressed)",
        rec,
    )
    # The ledger stays a single, consistent row (settle updates it in place, job_id NULL).
    with Session(engine) as s:
        rows = s.exec(select(WebhookEvent).where(WebhookEvent.connection_id == conn.id)).all()
    assert len(rows) == 1 and rows[0].processed_at is not None and rows[0].job_id is None, (
        "one settled row, no job",
        [(r.processed_at, r.job_id) for r in rows],
    )
    print(
        "  weak-key reedit: identical thin-body re-edit on button-trigger is RE-ACCEPTED, not lost OK"  # noqa: E501
    )


def test_weak_vs_strong_key_button_trigger_distinction():
    # Guard against OVER-reverting (task test 2). A STRONG provider-identity key
    # (signed issue key + changelog id) on the SAME button-trigger posture must STILL
    # dedup on replay after settle — the fix only relaxes dedup for the WEAK body-hash
    # fallback, never for provider-supplied identity. Same thin body as the weak repro,
    # but WITH the Atlassian identifier -> strong key -> a replay is a settled duplicate.
    conn = _mk_conn("strong-key dedup guard")  # auto OFF (button-trigger)
    rec = []
    enq = _fake_route_enqueue(rec)
    body = json.dumps({"issue_key": "LOC-2", "changelog": {"id": "strong-1"}}).encode()
    hdrs = {"X-Loc-Webhook-Secret": SECRET, "X-Atlassian-Webhook-Identifier": "evt-strong-1"}
    ev = webhooks.dedup_key("jira", body, json.loads(body.decode()), hdrs)
    assert not webhooks.is_weak_key(ev) and ev == "LOC-2:strong-1", (
        "must be the strong provider key",
        ev,
    )

    r1 = webhooks.handle_intake(conn.id, body, hdrs, enqueue=enq)
    assert r1.status == 200 and r1.ok, r1.as_dict()
    assert cstore.jira_pending(conn.id) == ["LOC-2"], cstore.jira_pending(conn.id)

    # Drain, then REPLAY the exact same signed POST (same Atlassian id) -> settled
    # duplicate: 200 ACK, NO re-record, pending stays drained.
    cstore.patch_config(conn.id, {"jira_pending": []})
    r2 = webhooks.handle_intake(conn.id, body, hdrs, enqueue=enq)
    assert r2.status == 200 and "duplicate" in r2.message.lower(), (
        "a strong-key replay must remain a settled duplicate",
        r2.as_dict(),
    )
    assert cstore.jira_pending(conn.id) == [], (
        "a strong-key replay must NOT re-record pending",
        cstore.jira_pending(conn.id),
    )
    assert rec == [("pending", conn.id, ["LOC-2"])], ("exactly one accept for the strong key", rec)
    print(
        "  strong-key guard: a provider-identity replay on button-trigger still dedups (no over-revert) OK"  # noqa: E501
    )


def test_d5_changed_ids_capped_during_extraction():
    # D5 (audit) — a signed-but-hostile body can pack tens of thousands of issue keys;
    # the intake must cap DURING extraction (set-based O(1) dedup + early break), not
    # parse+regex+log every candidate then slice. A payload of 5000 DISTINCT valid
    # keys must yield at most the cap. Pre-fix _jira_issue_keys returned all 5000
    # (verified against the pre-fix tree), so `len(got) <= cap` FAILS on pre-fix code.
    cap = getattr(webhooks, "_MAX_CHANGED_IDS", 100)
    keys = [f"LOC-{i}" for i in range(1, 5001)]
    got = jira_mod._jira_issue_keys({"issue_keys": keys})
    assert len(got) <= cap, (
        "the extractor must cap during extraction, not process all 5000",
        len(got),
    )
    assert got == keys[:cap], (
        "capped to the first N in order (dedup preserved)",
        got[:2],
        got[-1:],
    )

    # The generalized wrapper caps too (defense in depth for ANY connector's extractor),
    # and dedups duplicates via a set.
    capped = webhooks.changed_record_ids("jira", {"issue_keys": keys + keys})
    assert len(capped) <= cap, ("the wrapper must also cap the id list", len(capped))

    # End-to-end: a verified push carrying 5000 keys enqueues at most `cap` ids.
    conn = _mk_conn("d5 cap", {"auto_translate_on_change": "on"})
    rec = []
    enq = _fake_route_enqueue(rec)
    body = json.dumps({"issue_keys": keys}).encode()
    res = webhooks.handle_intake(conn.id, body, {"X-Loc-Webhook-Secret": SECRET}, enqueue=enq)
    assert res.status == 200 and len(res.record_ids) <= cap, (res.status, len(res.record_ids))
    print(f"  D5 cap: {len(keys)} candidate keys -> capped to {len(got)} during extraction OK")


def main():
    init_db()
    test_intake_auth_and_posture_off()
    test_posture_auto_on_enqueues_exact_issues()
    test_dedup_retry()
    test_payload_shapes_parse()
    test_changelog_gate_status_only()
    test_pending_release_translates_exact_and_clears()
    test_pending_release_preserves_concurrent_push()
    test_localhost_guard()
    test_org_scoping_unknown_connection_is_401()
    test_issue_key_injection_and_comma_smuggling_rejected()
    test_fetch_issues_scope_ands_connection_jql()
    test_free_query_jql_scope_ands_connection_jql()
    test_free_query_jql_paren_break_confined_against_faithful_mock()
    test_pending_release_double_click_coalesces()
    test_jobs_dispatch_unmapped_kind_fails_loud()
    test_jobs_dispatch_routes_jira()
    test_d4_button_trigger_replay_deduped()
    test_d4_crash_window_recovery_preserved()
    test_weak_key_button_trigger_reedit_not_lost()
    test_weak_vs_strong_key_button_trigger_distinction()
    test_d5_changed_ids_capped_during_extraction()
    print("verify_jira_p2 OK")


if __name__ == "__main__":
    main()
