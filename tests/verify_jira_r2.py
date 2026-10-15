"""Jira connector round-2 upgrades — fully offline via httpx.MockTransport.

Covers the five scoped round-2 items (the Jira upgrade digest), one test per item, plus a
confirmation that the existing
description/comment/scope-guard paths still hold (verify_jira_p1/p2 remain the full
regression proof; this suite adds the round-2 surface):

  G1  ADF rich-text CUSTOM fields are translatable — schema-driven selection
      (_cf_schema_kind) admits a rich-text/paragraph cf and routes it through the
      SAME ADF walker as `description` (marks/mentions/links survive); a numeric /
      select cf is skipped-AND-REPORTED (§3-D), never silently dropped; description
      still translates (no regression to the built-in ADF path).
  G7  Long scoped JQL uses a POST body on /rest/api/3/search/jql; the scope is still
      ANDed on the POST path; a hostile un-wrappable query is still refused.
  G8  Issue-count preview via POST /rest/api/3/search/approximate-count; scope applied.
  G2  read:jira-user is in the Atlassian OAuth scope set; the /myself authorship
      loop-guard works in OAUTH mode (bearer token, via the gateway).
  G3  OAuth-mode attachment download: a site-host /rest/... content URL is rewritten
      onto the gateway and fetched with the bearer token; a legacy /secure/... URL is
      cleanly REPORTED (ConnectorError), never a silent fail. Token mode unchanged.

The fake models CURRENT Jira semantics: /rest/api/3/search/jql GET+POST with
nextPageToken, /rest/api/3/field schema blocks (schema.custom textarea vs option),
ADF field payloads, /rest/api/3/search/approximate-count, /rest/api/3/myself, and the
OAuth gateway path prefix /ex/jira/{cloudid}. Run standalone: prints "JIRA R2 OK".

FAIL-BEFORE (pinned to the pre-fix fork point commit 9397f80, NOT HEAD): at 9397f80
validate_fields SKIPS a rich-text custom field whose schema.type is not literally
"string" ("custom field type 'any' is not translatable"), there is no
approximate_count / _scoped_clause / store.jira_count, _search is GET-only, the
Atlassian scope set lacks read:jira-user, and download_attachment fail-closes on an
OAuth-mode site-host content URL — so every assertion below fails against 9397f80.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jira_r2_")

import httpx

from app.db import init_db
from app.services.connectors import oauth
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.jira import (
    JQL_GET_MAXLEN,
    JiraConnector,
    _cf_schema_kind,
)


def _text(t, marks=None):
    n = {"type": "text", "text": t}
    if marks:
        n["marks"] = marks
    return n


def _doc(*inline):
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": list(inline)}],
    }


TEXTAREA = "com.atlassian.jira.plugin.system.customfieldtypes:textarea"
TEXTFIELD = "com.atlassian.jira.plugin.system.customfieldtypes:textfield"


# ===========================================================================
# G1 — rich-text custom fields: schema-driven classification + end-to-end
# ===========================================================================
def _g1_classifier_unit() -> None:
    """The SCHEMA-DRIVEN selection (§3-D). The old gate was an exact
    `schema.type == 'string'` frozenset, which SKIPPED every rich-text custom field
    whose type string was not literally 'string'. _cf_schema_kind widens that."""
    # a rich-text cf whose type is NOT 'string' (an app/Forge rich text field): the
    # old exact-string gate skipped it; the new one admits it as ADF (the widening).
    assert _cf_schema_kind({"type": "any", "custom": TEXTAREA}) == ("adf", ""), (
        "a :textarea cf with a non-string type must now be admitted as ADF"
    )
    # the classic Jira Paragraph field: type 'string' but :textarea -> ADF (its value
    # is an ADF doc, so it must route through the walker, not the plain path).
    assert _cf_schema_kind({"type": "string", "custom": TEXTAREA})[0] == "adf"
    # an ADF-typed rich text field with no :textarea custom key.
    assert _cf_schema_kind({"type": "doc"})[0] == "adf"
    # a single-line text field -> plain string.
    assert _cf_schema_kind({"type": "string", "custom": TEXTFIELD}) == ("plain", "")
    # non-text fields -> skipped, reason NAMES the type (reported, not dropped).
    k_num, r_num = _cf_schema_kind({"type": "number"})
    k_opt, r_opt = _cf_schema_kind({"type": "option"})
    k_usr, _ = _cf_schema_kind({"type": "user"})
    assert k_num == "" and "number" in r_num, (k_num, r_num)
    assert k_opt == "" and "option" in r_opt, (k_opt, r_opt)
    assert k_usr == "", "a user-picker cf must be skipped"
    # an unresolvable cf (no schema) is skipped with a reason, never admitted.
    assert _cf_schema_kind(None)[0] == "" and _cf_schema_kind({})[0] == ""
    print(
        "  G1(unit): _cf_schema_kind admits rich-text (incl. non-string type) as ADF, "
        "plain text as plain, non-text skipped-with-typed-reason"
    )


def _g1_end_to_end() -> None:
    """Full store.jira_translate flow (field mode): an ADF rich-text CUSTOM field is
    translated with structure preserved; a numeric + a select cf are skipped-AND-
    reported; the built-in `description` still translates (no regression)."""
    # customfield_20001: rich-text, type 'any' + :textarea (the OLD gate SKIPPED this).
    CF_ADF = "customfield_20001"
    CF_NUM = "customfield_20002"  # number  -> skipped + reported
    CF_SEL = "customfield_20003"  # option  -> skipped + reported
    desc = _doc(
        _text("Reset via "),
        _text("this link", [{"type": "link", "attrs": {"href": "https://acme.co/x"}}]),
        _text(" now "),
        {"type": "mention", "attrs": {"id": "u1", "text": "@Sam"}},
    )
    cf_val = _doc(
        _text("Acceptance: the "),
        _text("login flow", [{"type": "strong"}]),
        _text(" works "),
        {"type": "emoji", "attrs": {"shortName": ":tada:", "id": "1f389", "text": "🎉"}},
    )
    orig = {"description": desc, CF_ADF: cf_val, CF_NUM: 42, CF_SEL: {"value": "High"}}

    def transport(captured):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/field":
                return httpx.Response(
                    200,
                    json=[
                        {"id": "summary", "schema": {"type": "string"}},
                        {"id": "description", "schema": {"type": "doc", "system": "description"}},
                        {"id": CF_ADF, "schema": {"type": "any", "custom": TEXTAREA}},
                        {"id": CF_NUM, "schema": {"type": "number", "custom": "..:float"}},
                        {"id": CF_SEL, "schema": {"type": "option", "custom": "..:select"}},
                        {"id": "customfield_10099", "schema": {"type": "any", "custom": TEXTAREA}},
                    ],
                )
            if path == "/rest/api/3/search/jql":
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-1", "fields": {"summary": "Reset", **orig}}],
                    },
                )
            if path == "/rest/api/3/issue/LOC-1" and req.method == "GET":
                # F2 concurrent-edit re-check: return the field unchanged since fetch.
                fld = req.url.params.get("fields") or ""
                return httpx.Response(200, json={"fields": {fld: orig.get(fld)}})
            if path == "/rest/api/3/issue/LOC-1" and req.method == "PUT":
                captured.append(json.loads(req.content))
                return httpx.Response(204)
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    captured = []
    conn = cstore.create(
        "jira",
        "Jira R2 ADF cf",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": f"description,{CF_ADF},{CF_NUM},{CF_SEL}",
            "target_mode": "field",
            "target_field": "customfield_10099",
            "target_locales": "es-MX",
            "source_locale": "en-US",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    ok, msg, summary = cstore.jira_translate(conn.id, actor="auto", transport=transport(captured))
    assert ok, msg

    # the non-text cfs are skipped AND reported (typed reason surfaced in the message).
    assert CF_NUM in summary["skipped_fields"], ("numeric cf must be skipped", summary)
    assert CF_SEL in summary["skipped_fields"], ("select cf must be skipped", summary)
    assert CF_ADF not in summary["skipped_fields"], ("rich-text cf must be usable now", summary)
    assert "description" not in summary["skipped_fields"], "description must stay usable"
    assert "not translatable" in msg, ("skip reason must be reported in the message", msg)

    # Issue #641: field mode writes ONLY the target custom field. Both ADF sources are
    # rebuilt as real ADF trees (structure preserved) and combined into that one write.
    assert [list(c["fields"]) for c in captured] == [["customfield_10099"]], captured
    target = captured[0]["fields"]["customfield_10099"]
    assert target["type"] == "doc", target
    d_para, cf_para = (target["content"][0]["content"], target["content"][1]["content"])
    # description part: href + mention survive, leaf translated.
    assert d_para[1]["marks"][0]["attrs"]["href"] == "https://acme.co/x", ("desc href", d_para)
    assert d_para[3]["type"] == "mention", ("desc mention intact", d_para)
    assert d_para[0]["text"] and d_para[0]["text"] != "Reset via ", ("desc leaf", d_para)
    # rich-text CUSTOM source part: mark + emoji survive, leaf text translated.
    assert cf_para[1]["marks"] == [{"type": "strong"}], ("mark preserved on the cf", cf_para)
    assert cf_para[3]["type"] == "emoji" and cf_para[3]["attrs"]["id"] == "1f389", cf_para
    assert cf_para[0]["text"] and cf_para[0]["text"] != "Acceptance: the ", cf_para
    print(
        "  G1(e2e): rich-text custom field translated as ADF (marks/emoji intact), numeric "
        "+ select skipped-and-reported, description unregressed"
    )


# ===========================================================================
# G7 — long JQL POST fallback (scope preserved) + hostile rejected on POST path
# ===========================================================================
def _g7_post_fallback_and_scope_guard() -> None:
    # A scope long enough that ANY built clause exceeds JQL_GET_MAXLEN -> POST.
    LONG_SCOPE = "project = LOC AND labels in (" + ",".join(f"lbl{i}" for i in range(400)) + ")"
    assert len(LONG_SCOPE) > JQL_GET_MAXLEN, "fixture scope must exceed the GET threshold"

    def transport(seen):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/jql":
                if req.method == "POST":
                    seen.append(("POST", json.loads(req.content).get("jql", "")))
                else:
                    seen.append(("GET", req.url.params.get("jql", "")))
                return httpx.Response(
                    200,
                    json={
                        "isLast": True,
                        "issues": [{"key": "LOC-1", "fields": {"summary": "Hi"}}],
                    },
                )
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    seen = []
    jr = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=transport(seen),
    )
    issues = jr.fetch_issues(jql="status = Open", scope_jql=LONG_SCOPE, fields=("summary",), cap=10)
    assert issues and issues[0]["key"] == "LOC-1", issues
    methods = [m for m, _ in seen]
    assert methods and all(m == "POST" for m in methods), (
        "long JQL must go POST, not GET",
        methods,
    )
    body_jql = seen[0][1]
    # scope is STILL ANDed on the POST body — the POST path is not a scope bypass.
    assert "(status = Open)" in body_jql and LONG_SCOPE in body_jql and " AND " in body_jql, (
        "POST body must carry the scoped clause",
        body_jql[:120],
    )

    # A hostile un-wrappable caller jql is refused BEFORE any request, on the POST
    # path too (rejection is in _scoped_clause, upstream of transport selection).
    seen.clear()
    hostile = "project = SECRET) OR (project = LOC"
    try:
        jr.fetch_issues(jql=hostile, scope_jql=LONG_SCOPE, fields=("summary",))
        raise AssertionError("a paren-breaking caller jql must be rejected, even when long")
    except ConnectorError as e:
        assert "parenthes" in str(e).lower(), e
    assert seen == [], ("no search request may fire for a rejected hostile query", seen)
    print(
        "  G7: long scoped JQL uses POST and stays scope-ANDed; hostile un-wrappable "
        "query rejected on the POST path (no request fired)"
    )


# ===========================================================================
# G8 — approximate-count preview (scoped) + store.jira_count + hostile refused
# ===========================================================================
def _g8_count_preview() -> None:
    def transport(seen):
        def handler(req):
            path = req.url.path
            if path == "/rest/api/3/search/approximate-count" and req.method == "POST":
                seen.append(json.loads(req.content).get("jql", ""))
                return httpx.Response(200, json={"count": 42})
            return httpx.Response(404, json={"path": path})

        return httpx.MockTransport(handler)

    seen = []
    jr = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=transport(seen),
    )
    n = jr.approximate_count(jql="status = Open", scope_jql="project = LOC")
    assert n == 42, n
    assert seen == ["(status = Open) AND (project = LOC)"], ("count is scope-ANDed", seen)

    # store.jira_count end-to-end (the surface the /api + CLI + MCP accounting call).
    seen.clear()
    conn = cstore.create(
        "jira",
        "Jira R2 count",
        {
            "base_url": "https://team.atlassian.net",
            "jql": "project = LOC",
            "source_field": "summary",
            "target_locales": "es-MX",
        },
        {"email": "b@x.com", "api_token": "tok"},
        actor="admin",
    )
    ok, msg, out = cstore.jira_count(conn.id, jql="status = Open", transport=transport(seen))
    assert ok and out == {"count": 42}, (ok, msg, out)
    assert "42" in msg, msg
    assert seen == ["(status = Open) AND (project = LOC)"], ("store path scopes the count", seen)

    # the count preview inherits the SAME scope guard: a hostile query is refused.
    ok_h, msg_h, _out_h = cstore.jira_count(conn.id, jql="x) OR (y", transport=transport(seen))
    assert not ok_h and "parenthes" in msg_h.lower(), ("count must refuse a hostile jql", msg_h)
    print(
        "  G8: approximate-count preview scoped to the connection; store.jira_count "
        "returns the count and refuses a hostile jql"
    )


# ===========================================================================
# G2 — read:jira-user scope + OAuth-mode /myself authorship loop-guard
# ===========================================================================
def _g2_oauth_myself_loopguard() -> None:
    assert "read:jira-user" in oauth._ATLASSIAN_SCOPES, (
        "read:jira-user must be in the Atlassian scope set (/myself authorship loop-guard)"
    )
    # the descriptor the app actually requests carries it too.
    prov = oauth.get_provider("jira")
    assert prov and "read:jira-user" in prov.scopes, prov.scopes

    OUR = "our-oauth-accountid"
    CLOUD = "cloud-abcdef"
    seen = {"myself_auth": None, "myself_path": None}

    def handler(req):
        path = req.url.path
        if path.endswith("/rest/api/3/myself"):
            seen["myself_path"] = path
            seen["myself_auth"] = req.headers.get("authorization")
            return httpx.Response(200, json={"accountId": OUR, "displayName": "Loc Bot"})
        if path.endswith("/rest/api/3/issue/LOC-9/comment") and req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "comments": [
                        {
                            "id": "1",
                            "author": {"accountId": "human-alice"},
                            "body": _doc(_text("please translate")),
                        },
                        {
                            "id": "2",
                            "author": {"accountId": OUR},
                            "body": _doc(_text("our own machine reply")),
                        },
                    ]
                },
            )
        return httpx.Response(404, json={"path": path})

    jr = JiraConnector(
        {},
        {
            "auth_mode": "oauth",
            "oauth": {
                "cloudid": CLOUD,
                "access_token": "AT-123",
                "site_url": "https://team.atlassian.net",
            },
        },
        transport=httpx.MockTransport(handler),
    )
    got = {c["id"] for c in jr.fetch_comments("LOC-9")}
    # authorship resolved through the GATEWAY with a bearer token; our own comment excluded.
    assert seen["myself_auth"] == "Bearer AT-123", ("myself must use the bearer token", seen)
    assert seen["myself_path"] == f"/ex/jira/{CLOUD}/rest/api/3/myself", (
        "myself must go through the OAuth gateway path",
        seen["myself_path"],
    )
    assert got == {"1"}, ("our own reply excluded by authorship in oauth mode", got)
    print(
        "  G2: read:jira-user in the Atlassian scopes; OAuth-mode /myself authorship "
        "loop-guard works via the gateway (bearer), excludes our own reply"
    )


# ===========================================================================
# G3 — OAuth-mode attachment: site-host /rest URL rewritten to gateway; /secure reported
# ===========================================================================
def _g3_oauth_attachment() -> None:
    CLOUD = "cloud-abcdef"
    seen = {"path": None, "auth": None}

    def handler(req):
        path = req.url.path
        if path == f"/ex/jira/{CLOUD}/rest/api/3/attachment/content/100":
            seen["path"] = path
            seen["auth"] = req.headers.get("authorization")
            return httpx.Response(200, content=b"BINARYDATA")
        return httpx.Response(404, json={"path": path})

    jr = JiraConnector(
        {},
        {
            "auth_mode": "oauth",
            "oauth": {
                "cloudid": CLOUD,
                "access_token": "AT-9",
                "site_url": "https://team.atlassian.net",
            },
        },
        transport=httpx.MockTransport(handler),
    )

    # Jira returns the content URL on the SITE host; it must be rewritten onto the
    # gateway and fetched with the bearer token (was fail-closed before this fix).
    data = jr.download_attachment(
        "https://team.atlassian.net/rest/api/3/attachment/content/100", max_bytes=1_000_000
    )
    assert data == b"BINARYDATA", ("OAuth-mode site-host /rest attachment must download", data)
    assert seen["path"] == f"/ex/jira/{CLOUD}/rest/api/3/attachment/content/100", (
        "attachment must be fetched through the gateway",
        seen["path"],
    )
    assert seen["auth"] == "Bearer AT-9", ("attachment fetch uses the bearer token", seen)

    # a legacy /secure/ content URL is NOT gateway-addressable: reported, not silent.
    try:
        jr.download_attachment("https://team.atlassian.net/secure/attachment/100/f.docx")
        raise AssertionError(
            "a legacy /secure OAuth attachment must be reported, not silently fetched"
        )
    except ConnectorError as e:
        assert "legacy" in str(e).lower() and "secure" in str(e).lower(), e

    # token mode is unchanged: same-origin site URL passes; an off-site URL is refused.
    jr_tok = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "b@x.com", "api_token": "tok"},
        transport=httpx.MockTransport(handler),
    )
    try:
        jr_tok.download_attachment("https://evil.example.com/rest/api/3/attachment/content/100")
        raise AssertionError("an off-site content URL must be refused in token mode")
    except ConnectorError as e:
        assert "outside" in str(e).lower(), e
    print(
        "  G3: OAuth-mode site-host /rest attachment rewritten to the gateway + bearer; "
        "legacy /secure reported; token-mode off-site still refused"
    )


def main() -> None:
    init_db()
    _g1_classifier_unit()
    _g1_end_to_end()
    _g7_post_fallback_and_scope_guard()
    _g8_count_preview()
    _g2_oauth_myself_loopguard()
    _g3_oauth_attachment()
    print("JIRA R2 OK")


if __name__ == "__main__":
    main()
