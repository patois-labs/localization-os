"""Jira connector Phase 1 verification — fully offline via httpx.MockTransport.

Covers the Phase-1 (zero-tunnel) Jira items from the planning notes §4:

  1. Real ADF AST walker (app.filters.adf): leaf-text-only translation with
     marks/links/mentions/emoji/tables/code preserved byte-identical; deep-clone +
     leaf-replace reinsert; structure-integrity guard (DOCX/XLIFF convention).
       - nested lists round-trip
       - table with mixed marks round-trip
       - mention + emoji mid-sentence preserved verbatim
       - code block content NOT translated
       - link text translated, href intact
       - deliberately-mangling reinsert -> structure guard fires
  2. Changelog-gated re-translation: per-(issue,locale) content-hash suppression
     (jira_hashes, merge-persisted); unchanged source => suppressed; only_pairs
     retry bypasses the gate.
  3. Per-field-type config: text cf accepted, non-text cf skipped (editmeta gate).
  4. Comment translation (opt-in): existing comments translated + written back.

Run standalone (one process): python tests/verify_jira_p1.py  ->  prints "... OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jira_p1_")

import httpx

from app.db import init_db
from app.filters import adf
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.jira_part1 import _LABEL_PREFIX
from app.services.connectors.jira_part2 import JiraConnector

# ---------------------------------------------------------------------------
# ADF fixtures
# ---------------------------------------------------------------------------
TEXTAREA_CF = "com.atlassian.jira.plugin.system.customfieldtypes:textarea"


def _text(t, marks=None):
    n = {"type": "text", "text": t}
    if marks:
        n["marks"] = marks
    return n


NESTED_LISTS = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [_text("First item")]},
                        {
                            "type": "orderedList",
                            "content": [
                                {
                                    "type": "listItem",
                                    "content": [
                                        {"type": "paragraph", "content": [_text("Nested one")]}
                                    ],
                                },
                                {
                                    "type": "listItem",
                                    "content": [
                                        {"type": "paragraph", "content": [_text("Nested two")]}
                                    ],
                                },
                            ],
                        },
                    ],
                },
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [_text("Second item")]}],
                },
            ],
        },
    ],
}

TABLE_MIXED_MARKS = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {
                            "type": "tableHeader",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [_text("Head", [{"type": "strong"}])],
                                }
                            ],
                        },
                        {
                            "type": "tableCell",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        _text("bold", [{"type": "strong"}]),
                                        _text(" and "),
                                        _text("italic", [{"type": "em"}]),
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ],
        },
    ],
}

MENTION_EMOJI = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [
                _text("Hey "),
                {"type": "mention", "attrs": {"id": "557058:abc", "text": "@Alex"}},
                _text(" check "),
                {"type": "emoji", "attrs": {"shortName": ":tada:", "id": "1f389", "text": "🎉"}},
                _text(" now"),
            ],
        },
    ],
}

CODE_BLOCK = {
    "type": "doc",
    "version": 1,
    "content": [
        {"type": "paragraph", "content": [_text("Run this:")]},
        {
            "type": "codeBlock",
            "attrs": {"language": "python"},
            "content": [_text("print('hello world')")],
        },
    ],
}

LINK_DOC = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [
                _text("See "),
                _text(
                    "the docs", [{"type": "link", "attrs": {"href": "https://example.com/docs"}}]
                ),
                _text(" here"),
            ],
        },
    ],
}


def _reverse(tree):
    """Fake 'translation' of every leaf: reverse the text (deterministic, offline)."""
    return {u.p_index: u.text[::-1] for u in adf.extract(tree).units}


def _d6_comment_loop_guard_author_based() -> None:
    """D6 (audit): the comment loop-guard is AUTHORSHIP-based, not a spoofable
    body-marker check.

    Fails on the pre-fix code (a bare `body.startswith(🌐)` drop): there a human
    comment that merely starts with 🌐 is silently dropped (data loss), and any
    author can spoof our marker. After the fix, a comment is excluded iff its
    author.accountId matches OUR OWN accountId (from /rest/api/3/myself); the
    marker no longer decides.
    """
    OUR_ACCT = "our-token-accountid"

    # A doc body whose first prose leaf begins with the label prefix.
    def _doc(text):
        return {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        }

    comments = [
        # (1) a LEGIT human comment that happens to start with 🌐 — a DIFFERENT
        #     author. Pre-fix this is dropped by the prefix check (data loss);
        #     post-fix it survives (author != us).
        {
            "id": "901",
            "author": {"accountId": "human-alice"},
            "body": _doc(f"{_LABEL_PREFIX} please translate this important note"),
        },
        # (2) a SPOOFER: another account posts a 🌐-prefixed comment. Must still be
        #     treated as a translation source (author != us), never silently ours.
        {
            "id": "902",
            "author": {"accountId": "human-mallory"},
            "body": _doc(f"{_LABEL_PREFIX} spoofed marker from someone else"),
        },
        # (3) our OWN labelled reply (author == us). Must be excluded by AUTHORSHIP.
        {
            "id": "903",
            "author": {"accountId": OUR_ACCT},
            "body": _doc(f"{_LABEL_PREFIX} es-MX: our own machine reply"),
        },
        # (4) an ordinary human comment (no marker). Always a source.
        {"id": "904", "author": {"accountId": "human-bob"}, "body": _doc("just a normal comment")},
    ]

    def handler(req):
        path = req.url.path
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": OUR_ACCT, "displayName": "Loc Bot"})
        if path == "/rest/api/3/issue/LOC-99/comment" and req.method == "GET":
            return httpx.Response(200, json={"comments": comments})
        return httpx.Response(404, json={"path": path})

    conn = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=httpx.MockTransport(handler),
    )

    got = conn.fetch_comments("LOC-99")
    got_ids = {c["id"] for c in got}

    # The human marker-prefixed comment is NO LONGER dropped (pre-fix data-loss defect).
    assert "901" in got_ids, (
        "a legit human comment starting with the marker must NOT be dropped (author != us)",
        got_ids,
    )
    # A spoofed marker from another author is a genuine source, never treated as ours.
    assert "902" in got_ids, (
        "a spoofed marker from another account must still translate (not silently treated as ours)",
        got_ids,
    )
    # Our OWN reply is excluded by authorship — the anti-repost loop still holds.
    assert "903" not in got_ids, (
        "our own reply (author == us) must be excluded by authorship",
        got_ids,
    )
    # An ordinary human comment is always a source.
    assert "904" in got_ids, ("an ordinary human comment must be a source", got_ids)

    # Degraded fallback: if /myself is unreachable, we fall back to the marker so the
    # anti-repost loop still has protection (author-id resolution best-effort).
    def handler_no_myself(req):
        path = req.url.path
        if path == "/rest/api/3/myself":
            return httpx.Response(503, text="unavailable")
        if path == "/rest/api/3/issue/LOC-99/comment" and req.method == "GET":
            return httpx.Response(200, json={"comments": comments})
        return httpx.Response(404, json={"path": path})

    conn2 = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=httpx.MockTransport(handler_no_myself),
    )
    got2_ids = {c["id"] for c in conn2.fetch_comments("LOC-99")}
    # With no self-identity, every 🌐-prefixed comment (901/902/903) is dropped
    # (fallback marker), and only the unmarked human comment (904) survives — the
    # conservative degraded behaviour.
    assert got2_ids == {"904"}, ("degraded fallback keeps the marker guard", got2_ids)
    print("  D6: comment loop-guard is author-based; human marker comment no longer dropped")


def _d6a_transient_myself_failure_not_cached_on_reused_connector() -> None:
    """D6-A (review, CONFIRMED defect + fix): a TRANSIENT /myself failure must
    NEVER be cached as our identity on a REUSED connector instance.

    store.py's comment-translation loop reuses ONE JiraConnector across every
    issue in a run. Pre-fix, `_self_account_id` memoized whatever the FIRST
    resolution attempt produced -- success OR failure -- via
    `if cached is not None: return cached`. So a single transient /myself
    503/timeout at the very start of a run poisoned that connector instance for
    its ENTIRE remaining lifetime: every later fetch_comments call fell back to
    the spoofable marker check forever (even once Jira recovered), silently
    dropping legitimate human marker-prefixed comments for the rest of the run
    -- and /myself was never retried.

    This drives ONE connector instance through two fetch_comments calls: the
    first sees /myself failing (503), the second (same instance) sees it
    healthy. Fails on the pre-fix code: the second call would still use the
    degraded marker fallback and drop comment "801" (a legit human comment that
    happens to start with the marker), because "" got cached on the first call
    and was never retried.
    """
    real_sleep = cbase._sleep
    cbase._sleep = lambda s: None  # instant retries -- offline test, no wall-clock wait
    OUR_ACCT = "our-token-accountid"
    myself_calls = {"n": 0}

    def _doc(text):
        return {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        }

    comments = [
        # A legit human comment that happens to start with the marker. Must
        # eventually be resolved as a genuine source (author != us) once
        # /myself recovers -- never permanently stuck behind the degraded
        # fallback just because /myself failed ONCE, earlier in the run.
        {
            "id": "801",
            "author": {"accountId": "human-carol"},
            "body": _doc(f"{_LABEL_PREFIX} translate this please, it's important"),
        },
        # Our own labelled reply -- must be excluded once authorship resolves.
        {
            "id": "802",
            "author": {"accountId": OUR_ACCT},
            "body": _doc(f"{_LABEL_PREFIX} es-MX: our own machine reply"),
        },
    ]

    def handler(req):
        path = req.url.path
        if path == "/rest/api/3/myself":
            myself_calls["n"] += 1
            # Transient outage for the FIRST fetch_comments call: base._request
            # retries an idempotent GET up to RETRY_MAX=3 attempts internally,
            # all 503, before giving up and returning the last response. Jira
            # then recovers for every attempt after that.
            if myself_calls["n"] <= 3:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, json={"accountId": OUR_ACCT, "displayName": "Loc Bot"})
        if path == "/rest/api/3/issue/LOC-77/comment" and req.method == "GET":
            return httpx.Response(200, json={"comments": comments})
        return httpx.Response(404, json={"path": path})

    conn = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=httpx.MockTransport(handler),
    )

    try:
        # Call 1 (same instance): /myself down for all 3 internal retry
        # attempts -> degraded marker fallback -> both marker-prefixed
        # comments (801 human, 802 ours) are dropped. Expected for THIS one
        # call in isolation; the defect is about what happens on the NEXT call.
        got1_ids = {c["id"] for c in conn.fetch_comments("LOC-77")}
        assert got1_ids == set(), (
            "first call (myself down): degraded marker fallback drops both "
            "marker-prefixed comments",
            got1_ids,
        )
        calls_after_1 = myself_calls["n"]
        assert calls_after_1 == 3, (
            "expected 3 internal retry attempts against the down /myself",
            calls_after_1,
        )

        # Call 2, SAME connector instance: /myself now healthy.
        got2_ids = {c["id"] for c in conn.fetch_comments("LOC-77")}

        # /myself MUST be re-attempted -- proves the earlier failure was never
        # memoized (the core of D6-A).
        assert myself_calls["n"] > calls_after_1, (
            "/myself must be RE-ATTEMPTED on the second call -- a transient "
            "failure must never be cached",
            myself_calls["n"],
        )
        # Authorship now resolves: the legit human marker comment survives...
        assert "801" in got2_ids, (
            "once /myself recovers, the legit human marker-prefixed comment "
            "must be resolved as a genuine source (author != us) -- it must "
            "not stay stuck behind a stale degraded fallback from an earlier, "
            "now-resolved /myself failure",
            got2_ids,
        )
        # ...and our own reply is excluded now that authorship can be checked.
        assert "802" not in got2_ids, (
            "our own reply must be excluded once authorship resolves",
            got2_ids,
        )
    finally:
        cbase._sleep = real_sleep

    print(
        "  D6-A: transient /myself failure is never cached on a reused "
        "connector instance; retried and resolved on the next call"
    )


def main() -> None:
    init_db()
    _d6_comment_loop_guard_author_based()
    _d6a_transient_myself_failure_not_cached_on_reused_connector()

    # ============================================================ ADF walker ==
    # nested lists: 4 leaves, structure identical after reinsert
    ex = adf.extract(NESTED_LISTS)
    assert [u.text for u in ex.units] == [
        "First item",
        "Nested one",
        "Nested two",
        "Second item",
    ], ex.units
    rebuilt = adf.reinsert(NESTED_LISTS, {u.p_index: f"[T]{u.text}" for u in ex.units})
    assert adf.structure_ok(NESTED_LISTS, rebuilt), "nested-list structure must survive"
    # the orderedList inside the first listItem is still there and typed
    li0 = rebuilt["content"][0]["content"][0]  # bulletList > listItem
    ol = li0["content"][1]  # the nested orderedList
    assert ol["type"] == "orderedList", rebuilt
    # orderedList > listItem > paragraph > text
    assert ol["content"][0]["content"][0]["content"][0]["text"] == "[T]Nested one"
    print("  adf: nested lists round-trip (4 leaves, ordered-in-bullet structure kept)")

    # table with mixed marks: marks preserved byte-identical, only text changed
    ex = adf.extract(TABLE_MIXED_MARKS)
    assert [u.text for u in ex.units] == ["Head", "bold", " and ", "italic"], ex.units
    rebuilt = adf.reinsert(TABLE_MIXED_MARKS, _reverse(TABLE_MIXED_MARKS))
    cell = rebuilt["content"][0]["content"][0]["content"][1]["content"][0]["content"]
    assert cell[0]["text"] == "dlob" and cell[0]["marks"] == [{"type": "strong"}], cell
    assert cell[2]["text"] == "cilati" and cell[2]["marks"] == [{"type": "em"}], cell
    assert adf.structure_ok(TABLE_MIXED_MARKS, rebuilt)
    print("  adf: table with mixed marks round-trip (strong/em marks preserved)")

    # mention + emoji mid-sentence: text leaves translate, mention/emoji byte-identical
    ex = adf.extract(MENTION_EMOJI)
    assert [u.text for u in ex.units] == ["Hey ", " check ", " now"], (
        ex.units
    )  # no mention/emoji unit
    rebuilt = adf.reinsert(MENTION_EMOJI, _reverse(MENTION_EMOJI))
    inline = rebuilt["content"][0]["content"]
    assert inline[1] == MENTION_EMOJI["content"][0]["content"][1], "mention must be byte-identical"
    assert inline[3] == MENTION_EMOJI["content"][0]["content"][3], "emoji must be byte-identical"
    assert (
        inline[0]["text"] == " yeH" and inline[2]["text"] == " kcehc "
    )  # "Hey "[::-1], " check "[::-1]
    print("  adf: mention + emoji mid-sentence preserved verbatim (no unit emitted)")

    # code block: code text NOT a unit, preserved verbatim
    ex = adf.extract(CODE_BLOCK)
    assert [u.text for u in ex.units] == ["Run this:"], ex.units  # code line excluded
    rebuilt = adf.reinsert(CODE_BLOCK, _reverse(CODE_BLOCK))
    assert rebuilt["content"][1]["content"][0]["text"] == "print('hello world')", "code untouched"
    assert rebuilt["content"][0]["content"][0]["text"] == ":siht nuR"
    print("  adf: code block content NOT translated (only prose leaf)")

    # link: child text translates, href unchanged
    ex = adf.extract(LINK_DOC)
    assert [u.text for u in ex.units] == ["See ", "the docs", " here"], ex.units
    rebuilt = adf.reinsert(LINK_DOC, _reverse(LINK_DOC))
    link_leaf = rebuilt["content"][0]["content"][1]
    assert link_leaf["text"] == "scod eht", link_leaf
    assert link_leaf["marks"][0]["attrs"]["href"] == "https://example.com/docs", "href intact"
    print("  adf: link text translated, href preserved")

    # structure guard fires on a deliberately-mangled target (extra node injected)
    mangled = adf.reinsert(LINK_DOC, {})
    mangled["content"].append({"type": "paragraph", "content": [_text("EXTRA")]})
    assert not adf.structure_ok(LINK_DOC, mangled), (
        "integrity guard must fire on node-count mismatch"
    )
    # and a dropped leaf trips it too
    mangled2 = adf.reinsert(LINK_DOC, {})
    mangled2["content"][0]["content"].pop()  # drop the " here" leaf
    assert not adf.structure_ok(LINK_DOC, mangled2), (
        "integrity guard must fire on leaf-count mismatch"
    )
    print("  adf: structure-integrity guard fires on node/leaf mismatch (mangling MT)")

    # ================================================== store.jira_translate ==
    # A Jira transport serving an issue with an ADF description containing a link
    # and a mention, capturing the description PUT payload.
    def field_transport(captured):
        desc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        _text("Reset via "),
                        _text(
                            "this link",
                            [{"type": "link", "attrs": {"href": "https://acme.co/reset"}}],
                        ),
                        _text(" now "),
                        {"type": "mention", "attrs": {"id": "u1", "text": "@Sam"}},
                    ],
                }
            ],
        }

        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/field":
                # Issue #641: the target is a rich-text (ADF) custom field
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "customfield_10050",
                            "schema": {
                                "type": "string",
                                "custom": TEXTAREA_CF,
                            },
                        }
                    ],
                )
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [
                            {
                                "key": "LOC-9",
                                "fields": {"summary": "Reset password", "description": desc},
                            }
                        ],
                    },
                )
            if path == "/rest/api/3/issue/LOC-9" and req.method == "PUT":
                captured.append(json.loads(req.content))
                return httpx.Response(204)
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler), desc

    captured = []
    tr, _src_desc = field_transport(captured)
    jconn = cstore.create(
        "jira",
        "Jira ADF field",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "description",
            "target_mode": "field",
            "target_field": "customfield_10050",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    ok, msg, summary = cstore.jira_translate(jconn.id, actor="auto", transport=tr)
    assert ok, msg
    assert summary["written"] == 1, summary
    # Issue #641: field mode writes the TARGET custom field only, never the source description
    assert list(captured[0]["fields"]) == ["customfield_10050"], captured[0]
    # the written target is ADF (reinserted), NOT flattened; link href + mention survive
    put_desc = captured[0]["fields"]["customfield_10050"]
    assert put_desc["type"] == "doc", put_desc
    para = put_desc["content"][0]["content"]
    assert para[1]["marks"][0]["attrs"]["href"] == "https://acme.co/reset", (
        "href must survive field write"
    )
    assert para[3]["type"] == "mention" and para[3]["attrs"]["id"] == "u1", (
        "mention must survive field write"
    )
    # and the source leaves were actually translated (MockProvider is non-empty)
    assert para[0]["text"] and para[0]["text"] != "Reset via ", "leaf text should be translated"
    print("  jira_translate: ADF description written back as a real ADF tree (href+mention intact)")

    # -- changelog gate: a second run with unchanged source is suppressed ----
    captured.clear()
    tr2, _ = field_transport(captured)
    ok2, msg2, sum2 = cstore.jira_translate(jconn.id, actor="auto", transport=tr2)
    assert ok2, msg2
    assert sum2["written"] == 0 and sum2["suppressed"] == 1, (
        "unchanged source must suppress",
        sum2,
    )
    assert captured == [], "no field PUT on a suppressed re-run"
    print("  jira_translate: changelog gate suppresses unchanged (issue,locale) on re-run")

    # -- only_pairs retry bypasses the hash gate (re-attempts the failed pair) -
    captured.clear()
    tr3, _ = field_transport(captured)
    ok3, _m3, sum3 = cstore.jira_translate(
        jconn.id, actor="auto", transport=tr3, only_pairs={("LOC-9", "es-MX")}
    )
    assert ok3 and sum3["written"] == 1 and sum3["suppressed"] == 0, ("retry bypasses gate", sum3)
    print("  jira_translate: only_pairs retry bypasses the hash gate (re-writes the pair)")

    # -- the OPERATOR retry path (store.retry_failed) re-attempts a hash-suppressed pair --
    # Jira is a STANDALONE flow (NOT run_translate_flow), so the framework-level only_pairs
    # bypass consolidation (which removed the per-connector hash-clear branches from
    # retry_failed) must leave Jira's own bypass intact. This exercises the REAL operator
    # path end to end: retry_failed -> jira branch -> jira_translate(only_pairs=...). First
    # confirm a plain SCHEDULED re-run STILL suppresses (the 425-431 retry re-recorded the
    # hash), then confirm the operator retry of exactly that pair re-attempts it.
    captured.clear()
    tr3b, _ = field_transport(captured)
    ok_s, _ms, sum_s = cstore.jira_translate(jconn.id, actor="auto", transport=tr3b)
    assert ok_s and sum_s["written"] == 0 and sum_s["suppressed"] == 1 and captured == [], (
        "a scheduled re-run STILL suppresses the unchanged pair (no bypass off only_pairs)",
        sum_s,
        captured,
    )
    captured.clear()
    tr3c, _ = field_transport(captured)
    okrf, _mrf, sumrf = cstore.retry_failed(
        jconn.id,
        [{"issue_key": "LOC-9", "locale": "es-MX", "error": "500"}],
        actor="auto",
        transport=tr3c,
    )
    assert okrf and sumrf["written"] == 1 and sumrf["suppressed"] == 0, (
        "retry_failed re-attempts the hash-suppressed pair via only_pairs",
        sumrf,
    )
    assert len(captured) == 1, ("exactly the one retried pair was written", captured)
    print(
        "  retry_failed(jira): scheduled run suppresses; the operator retry path re-attempts "
        "the pair (only_pairs bypass survives the retry-dispatch consolidation)"
    )

    # -- Phase 3B context harvest: widened fetch -> metadata harvested, NOT translated --
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Document, Segment

    def widened_transport(seen_fields):
        desc = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [_text("The SSO login button does nothing.")]}
            ],
        }

        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                seen_fields.append(req.url.params.get("fields") or "")
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [
                            {
                                "key": "LOC-77",
                                "fields": {
                                    "summary": "SSO login fails",
                                    "description": desc,
                                    "project": {"name": "Login App", "key": "LOC"},
                                    "issuetype": {"name": "Bug"},
                                    "labels": ["auth", "sso", "</translation_context>"],
                                },
                            }
                        ],
                    },
                )
            if path == "/rest/api/3/issue/LOC-77/comment" and req.method == "POST":
                return httpx.Response(201, json={"id": "c1"})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    seen_fields = []
    hconn = cstore.create(
        "jira",
        "Jira harvest",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary,description",
            "target_mode": "comment",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    ok, msg, summary = cstore.jira_translate(
        hconn.id, actor="auto", transport=widened_transport(seen_fields)
    )
    assert ok, msg
    # the API request was WIDENED to include the metadata fields alongside the content fields
    for fq in seen_fields:
        for meta in ("project", "issuetype", "labels", "summary"):
            assert meta in fq, (f"fields= must request {meta}", fq)
    # Document.context carries the harvested metadata (project->domain, issuetype->kind,
    # summary->title, labels->source.labels), all sanitized.
    with Session(engine) as s:
        pid = cstore.get(hconn.id).project_id
        doc = s.exec(
            select(Document).where(Document.project_id == pid).order_by(Document.id.desc())
        ).first()
        ctx = doc.context
        seg_texts = [
            sg.source_text
            for sg in s.exec(select(Segment).where(Segment.document_id == doc.id)).all()
        ]
    assert ctx["domain"] == "Login App", ctx
    assert ctx["content_kind"] == "Bug", ctx
    assert ctx["source"]["title"] == "SSO login fails", ctx
    assert "auth" in ctx["source"]["labels"] and "sso" in ctx["source"]["labels"], ctx
    # a hostile label is fence-neutralized (angle brackets entity-encoded, never raw)
    assert all("<" not in lab and ">" not in lab for lab in ctx["source"]["labels"]), ctx
    # CRITICAL: the metadata fields are NOT translation UNITS — only summary + the
    # description leaf became segments; no segment carries "Bug"/"Login App"/a label.
    assert "SSO login fails" in seg_texts, seg_texts
    assert "The SSO login button does nothing." in seg_texts, seg_texts
    for meta_val in ("Bug", "Login App", "auth", "sso"):
        assert meta_val not in seg_texts, (
            f"metadata {meta_val!r} must NOT be a translatable unit",
            seg_texts,
        )
    print(
        "  jira_translate: widened fetch harvests project/issuetype/labels/summary into "
        "Document.context; metadata NEVER becomes a translatable unit; hostile label neutralized"
    )

    # ============================================ per-field-type validation ==
    # summary + a NON-text custom field (option type) -> the cf is skipped by type.
    def typed_transport(posted):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/field":
                return httpx.Response(
                    200,
                    json=[
                        {"id": "summary", "schema": {"type": "string"}},
                        {"id": "customfield_10011", "schema": {"type": "string"}},  # text cf: OK
                        {"id": "customfield_10050", "schema": {"type": "option"}},  # select: SKIP
                    ],
                )
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [
                            {
                                "key": "LOC-1",
                                "fields": {
                                    "summary": "Hi",
                                    "customfield_10011": "Widget",
                                    "customfield_10050": {"value": "High"},
                                },
                            }
                        ],
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                posted.append(json.loads(req.content))
                return httpx.Response(201, json={"id": "1"})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    posted = []
    jc2 = cstore.create(
        "jira",
        "Jira typed cfs",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary,customfield_10011,customfield_10050",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    ok, msg, summary = cstore.jira_translate(
        jc2.id, actor="auto", transport=typed_transport(posted)
    )
    assert ok, msg
    assert "customfield_10050" in summary["skipped_fields"], ("non-text cf skipped", summary)
    assert "customfield_10011" not in summary["skipped_fields"], "text cf must be usable"
    print("  jira_translate: per-field-type gate skips a select cf, keeps a text cf")

    # ================================================= comment translation ==
    def comment_transport(posted):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-7", "fields": {"summary": "A ticket"}}],
                    },
                )
            if path == "/rest/api/3/issue/LOC-7/comment" and req.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "comments": [
                            {
                                "id": "500",
                                "body": {
                                    "type": "doc",
                                    "version": 1,
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [_text("Please advise on scope.")],
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                posted.append(json.loads(req.content))
                return httpx.Response(201, json={"id": "9"})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    posted = []
    jc3 = cstore.create(
        "jira",
        "Jira comments",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "translate_comments": "1",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    ok, msg, summary = cstore.jira_translate(
        jc3.id, actor="auto", transport=comment_transport(posted)
    )
    assert ok, msg
    blob = json.dumps(posted)
    assert "re: comment 500" in blob, ("existing comment translated + labelled", posted)
    # summary comment + comment-translation reply = 2 posts
    assert len(posted) == 2, posted
    print("  jira_translate: opt-in translates existing comment, writes labelled reply")

    # ============================================ F1: anti-repost + self-reply ==
    # A transport whose GET /comment ALSO serves back everything ever POSTed (so a
    # run's own labelled replies are visible to the NEXT run's fetch) — this is
    # what makes the self-feedback loop reproducible offline: if fetch_comments
    # didn't filter our own 🌐-labelled replies, they'd come back as new
    # translatable "existing comments" and multiply every run.
    def f1_transport(store_state):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-20", "fields": {"summary": "F1 ticket"}}],
                    },
                )
            if path == "/rest/api/3/issue/LOC-20/comment" and req.method == "GET":
                return httpx.Response(200, json={"comments": list(store_state["comments"])})
            if path == "/rest/api/3/issue/LOC-20/comment" and req.method == "POST":
                body = json.loads(req.content)
                new_id = str(store_state["next_id"])
                store_state["next_id"] += 1
                store_state["posted"].append(body)
                store_state["comments"].append({"id": new_id, "body": body["body"]})
                return httpx.Response(201, json={"id": new_id})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    state = {
        "comments": [
            {
                "id": "700",
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [_text("Please advise on scope.")]}
                    ],
                },
            }
        ],
        "posted": [],
        "next_id": 1000,
    }
    jc4 = cstore.create(
        "jira",
        "Jira F1 anti-repost",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "translate_comments": "1",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )

    # Run 1: summary (as a comment) + the one human comment translated = 2 posts.
    ok, msg, _sum1 = cstore.jira_translate(jc4.id, actor="auto", transport=f1_transport(state))
    assert ok, msg
    assert len(state["posted"]) == 2, (
        "run 1: summary comment + 1 translated comment",
        state["posted"],
    )
    posted_after_run1 = len(state["posted"])

    # Run 2, SAME state (our run-1 replies are now in store_state["comments"]):
    #   - the summary hash is unchanged -> field/summary-derived comment suppressed
    #   - the human comment (id 700) hash is unchanged -> suppressed
    #   - our OWN run-1 replies must be filtered by fetch_comments (label prefix) —
    #     never picked up as new translation sources
    # => ZERO new posts.
    ok2, msg2, sum2 = cstore.jira_translate(jc4.id, actor="auto", transport=f1_transport(state))
    assert ok2, msg2
    assert len(state["posted"]) == posted_after_run1, (
        "run 2 must post ZERO new comments (anti-repost + self-reply filter)",
        state["posted"],
    )
    assert sum2["suppressed"] >= 1 and sum2.get("comments_suppressed", 0) >= 1, sum2
    print(
        "  F1: second run with comments on posts ZERO new comments (fields+comments suppress independently)"  # noqa: E501
    )

    # Our own labelled replies must never surface as translatable "existing
    # comments" — confirm none of the stored comments that originated from OUR
    # posts (id >= 1000) were ever re-translated into a THIRD-generation reply
    # (i.e. no reply-to-a-reply exists in what we've posted).
    for body in state["posted"]:
        blob = json.dumps(body)
        assert "re: comment 1" not in blob and "re: comment 10" not in blob, (
            "a reply must never be re-translated as if it were a source comment",
            body,
        )
    print("  F1: connector's own labelled replies never become translation units")

    # An EDITED source comment (id 700's text changes) re-translates: hash differs
    # -> not suppressed -> a new labelled reply is posted even though the pair's
    # summary/field hash is still unchanged.
    state["comments"][0] = {
        "id": "700",
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [_text("Please advise on scope AND budget.")]}
            ],
        },
    }
    ok3, msg3, sum3 = cstore.jira_translate(jc4.id, actor="auto", transport=f1_transport(state))
    assert ok3, msg3
    assert len(state["posted"]) == posted_after_run1 + 1, (
        "an edited source comment must re-translate (hash changed)",
        state["posted"],
    )
    assert sum3["suppressed"] >= 1, ("the summary/field portion is still unchanged", sum3)
    print(
        "  F1: an EDITED source comment re-translates (hash changed); unedited ones stay suppressed"
    )

    # F3 (over-long translated summary skipped) was a field-mode-on-the-source-summary
    # guard. Issue #641: field mode never writes the source summary any more (it writes one
    # target custom field), so there is no summary cap to test here.

    # ============================================= F4: per-run write ceiling ==
    def f4_transport(captured):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [
                            {"key": f"LOC-4{i}", "fields": {"summary": f"Ticket {i}"}}
                            for i in range(5)
                        ],
                    },
                )
            if path.endswith("/comment") and req.method == "POST":
                captured.append(json.loads(req.content))
                return httpx.Response(201, json={"id": str(len(captured))})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    posted_f4 = []
    jc6 = cstore.create(
        "jira",
        "Jira F4 ceiling",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    import app.config as _cfg

    _orig_ceiling = _cfg.MAX_WRITES_PER_RUN
    _cfg.MAX_WRITES_PER_RUN = 3
    try:
        ok, msg, sum_f4 = cstore.jira_translate(
            jc6.id, actor="auto", transport=f4_transport(posted_f4)
        )
    finally:
        _cfg.MAX_WRITES_PER_RUN = _orig_ceiling
    assert ok, msg
    assert len(posted_f4) == 3, ("exactly 3 writes when the ceiling is 3 with 5 pending", posted_f4)
    assert sum_f4["written"] == 3, sum_f4
    assert sum_f4.get("ceiling_hit") is True, sum_f4
    assert "ceiling" in msg.lower() and "3" in msg, (
        "clear per-run message naming the ceiling",
        msg,
    )
    print("  F4: write ceiling of 3 with 5 pending -> exactly 3 writes + clear per-run message")

    # ===================================== F2: field-mode concurrent-edit guard ==
    # Issue #641: field mode writes only the TARGET custom field, so a human edit of the
    # SOURCE summary between our fetch and our write can no longer be clobbered. The
    # source is never PUT; the target field is written; the edit is picked up next run
    # (the source hash changes).
    def f2_transport(captured):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-50", "fields": {"summary": "Original summary"}}],
                    },
                )
            if path == "/rest/api/3/issue/LOC-50" and req.method == "GET":
                # Simulates a human having changed the field since our fetch.
                return httpx.Response(200, json={"fields": {"summary": "Human edited this!"}})
            if path == "/rest/api/3/issue/LOC-50" and req.method == "PUT":
                captured.append(json.loads(req.content))
                return httpx.Response(204)
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    captured = []
    jc7 = cstore.create(
        "jira",
        "Jira F2 concurrent edit",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "target_mode": "field",
            "target_field": "customfield_10050",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    ok, msg, _sum_f2 = cstore.jira_translate(jc7.id, actor="auto", transport=f2_transport(captured))
    assert ok, msg
    assert [list(c["fields"]) for c in captured] == [["customfield_10050"]], (
        "only the target field is written; the human-edited source summary is never PUT",
        captured,
    )
    print("  F2 (#641): source edit in the window cannot be clobbered; only the target is written")

    # ---- REGRESSION (transport-catch parity): a field PUT that hits a PERSISTENT
    #    transport error must be a per-pair failure, never an escaping crash. post_comment
    #    already caught httpx.TransportError; _put_field (set_field / set_field_adf) did
    #    NOT -- the same create-vs-update asymmetry fixed in freshdesk's client. base._request
    #    re-raises a persistent transport error even for an idempotent PUT, so without the
    #    catch it escaped straight out of jira_translate and crashed the whole run.
    def transport_err_transport():
        desc = {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [_text("Reset password now")]}],
        }

        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [
                            {"key": "LOC-77", "fields": {"summary": "Reset", "description": desc}}
                        ],
                    },
                )
            if path == "/rest/api/3/issue/LOC-77" and req.method == "PUT":
                # every attempt fails at the transport layer -> base._request exhausts
                # its idempotent retries and re-raises; _put_field must catch it.
                raise httpx.ConnectError("connection reset by peer")
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    jc8 = cstore.create(
        "jira",
        "Jira transport-error field write",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary,description",
            "target_mode": "field",
            "target_field": "customfield_10050",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    # must NOT raise -- a persistent transport error is a per-pair failure, run not green.
    ok, msg, sum_te = cstore.jira_translate(
        jc8.id, actor="auto", transport=transport_err_transport()
    )
    assert not ok, ("a persistent transport error on the field PUT -> run not green", msg)
    assert sum_te["written"] == 0, sum_te
    assert sum_te["failures"], ("the failed field write is recorded per-pair", sum_te)
    assert any(
        "ConnectError" in f["error"] or "connection reset" in f["error"].lower()
        for f in sum_te["failures"]
    ), sum_te["failures"]
    print(
        "  transport-parity: a persistent field-PUT transport error -> per-pair failure, no crash"
    )

    print("JIRA P1 OK")


if __name__ == "__main__":
    main()
