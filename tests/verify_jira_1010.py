"""Jira connector batch jira-1010 (issues #641, #642, #643, #645; #644 if built).

Fully offline: httpx.MockTransport fakes and the built-in MockProvider only. No real Jira
tenant is ever contacted.

  #641  field mode writes ONLY the configured target field (never the source summary or
        description); no target field -> refused before any MT.
  #642  a scope JQL with ORDER BY keeps ORDER BY only at the very end, outside parentheses,
        on every keyed path.
  #643  comment mode posts the rebuilt ADF tree (lists, paragraphs), not one flat paragraph.
  #645  a scoped Atlassian API token's 401 names the scoped-token cause.

Final line: "JIRA 1010 OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jira_1010_")

import httpx

from app.db import init_db
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore

cbase._sleep = lambda s: None

SECRET = {"email": "b@x.com", "api_token": "tok"}  # pregate: allow-secret throwaway fixture


def _t(text, marks=None):
    n = {"type": "text", "text": text}
    if marks:
        n["marks"] = marks
    return n


def _para(*kids):
    return {"type": "paragraph", "content": list(kids)}


def _item(text):
    return {"type": "listItem", "content": [_para(_t(text))]}


DESC = {
    "type": "doc",
    "version": 1,
    "content": [
        _para(_t("Intro paragraph here")),
        {
            "type": "bulletList",
            "content": [_item("First point"), _item("Second point"), _item("Third point")],
        },
        _para(_t("Closing paragraph here")),
    ],
}


def fake_jira(log, *, summary="Reset password", description=DESC, catalogue=None, key="LOC-1"):
    """A fake Jira. `log` collects (method, path, query, body)."""

    def handler(req):
        path = req.url.path
        body = None
        if req.content:
            try:
                body = json.loads(req.content)
            except ValueError:
                body = None
        log.append((req.method, path, dict(req.url.params), body))
        if path == "/rest/api/3/search/jql":
            return httpx.Response(
                200,
                json={
                    "isLast": True,
                    "issues": [
                        {"key": key, "fields": {"summary": summary, "description": description}}
                    ],
                },
            )
        if path == "/rest/api/3/field":
            return httpx.Response(200, json=catalogue or [])
        if path == f"/rest/api/3/issue/{key}" and req.method == "GET":
            fld = req.url.params.get("fields") or "summary"
            return httpx.Response(
                200,
                json={"fields": {fld: {"summary": summary, "description": description}.get(fld)}},
            )
        if path == f"/rest/api/3/issue/{key}" and req.method == "PUT":
            return httpx.Response(204)
        if path == f"/rest/api/3/issue/{key}/comment" and req.method == "POST":
            return httpx.Response(201, json={"id": "9001"})
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


def _conn(name, **extra):
    cfg = {
        "base_url": "https://team.atlassian.net",
        "jql": "project = LOC",
        "source_field": "summary,description",
        "target_mode": "comment",
        "target_locales": "es-MX",
        "source_locale": "en-US",
    }
    cfg.update(extra)
    return cstore.create("jira", name, cfg, SECRET, actor="admin")


def _puts(log):
    return [e for e in log if e[0] == "PUT"]


def _posts(log):
    return [e for e in log if e[0] == "POST"]


# ---------------------------------------------------------------------------
def test_641_field_mode_writes_only_target_field():
    log = []
    c = _conn("f641", target_mode="field", target_field="customfield_10050")
    ok, msg, _summ = cstore.jira_translate(c.id, actor="auto", transport=fake_jira(log))
    assert ok, msg
    puts = _puts(log)
    assert puts, ("the target field must be written", log)
    for _m, _p, _q, body in puts:
        written = set(body["fields"])
        assert written == {"customfield_10050"}, ("only the target field may be written", written)
    # a second target locale cannot silently overwrite the first: refused up front
    log2 = []
    c2 = _conn(
        "f641 two locales",
        target_mode="field",
        target_field="customfield_10050",
        target_locales="es-MX, ja-JP",
    )
    ok2, msg2, _s = cstore.jira_translate(c2.id, actor="auto", transport=fake_jira(log2))
    assert not ok2 and "one language" in msg2, (ok2, msg2)
    assert not _puts(log2) and not any(e[1] == "/rest/api/3/search/jql" for e in log2), log2
    print("  641: field mode writes only the target field; several locales refused up front")


def test_641_field_mode_without_target_refused_before_mt():
    for tf in ("", None):
        log = []
        extra = {"target_mode": "field"}
        if tf is not None:
            extra["target_field"] = tf
        c = _conn(f"f641 none {tf!r}", **extra)
        ok, msg, _s = cstore.jira_translate(c.id, actor="auto", transport=fake_jira(log))
        assert not ok and "target custom field" in msg, (ok, msg)
        assert not _puts(log) and not _posts(log), log
        assert not any(e[1] == "/rest/api/3/search/jql" for e in log), ("no fetch, no MT", log)
    # a target that is a source field / built-in is refused too (it would overwrite the source)
    for bad in ("summary", "description", "customfield_20000"):
        log = []
        src = (
            "summary,description,customfield_20000"
            if bad == "customfield_20000"
            else "summary,description"
        )
        c = _conn(f"f641 bad {bad}", target_mode="field", target_field=bad, source_field=src)
        ok, msg, _s = cstore.jira_translate(c.id, actor="auto", transport=fake_jira(log))
        assert not ok and "never overwritten" in msg, (bad, ok, msg)
        assert not _puts(log), log
    print("  641: field mode with no/overlapping target refused before any fetch or MT")


def test_641_rich_target_gets_adf_and_source_untouched():
    log = []
    cat = [
        {
            "id": "customfield_10050",
            "schema": {
                "type": "string",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textarea",
            },
        }
    ]
    c = _conn("f641 rich", target_mode="field", target_field="customfield_10050")
    ok, msg, _s = cstore.jira_translate(c.id, actor="auto", transport=fake_jira(log, catalogue=cat))
    assert ok, msg
    puts = _puts(log)
    assert len(puts) == 1, puts
    val = puts[0][3]["fields"]["customfield_10050"]
    assert val["type"] == "doc", val
    kinds = [n["type"] for n in val["content"]]
    assert "bulletList" in kinds, ("description structure survives in the target", kinds)
    # a non-text target (number field) is refused
    log2 = []
    cat2 = [{"id": "customfield_10050", "schema": {"type": "number"}}]
    c2 = _conn("f641 num", target_mode="field", target_field="customfield_10050")
    ok2, msg2, _s = cstore.jira_translate(
        c2.id, actor="auto", transport=fake_jira(log2, catalogue=cat2)
    )
    assert not ok2 and "not a text field" in msg2 and not _puts(log2), (ok2, msg2)
    print("  641: rich-text target receives the ADF tree; a numeric target is refused")


# ---------------------------------------------------------------------------
def _jql_seen(log):
    out = []
    for _method, path, query, body in log:
        if path == "/rest/api/3/search/jql":
            out.append((body or {}).get("jql") or query.get("jql"))
    return out


def _valid_order_by(jql):
    """ORDER BY must be at the very end, once, and never inside parentheses."""
    low = jql.lower()
    if "order by" not in low:
        return True
    idx = low.rindex("order by")
    if low.count("order by") != 1:
        return False
    return jql[:idx].count("(") == jql[:idx].count(")") and ")" not in jql[idx:]


def test_642_scope_order_by_stays_outside_parentheses():
    scope = "project = LOC ORDER BY created DESC"
    # keyed path (webhook push, MCP item_ids, retries): keys AND scope
    log = []
    c = _conn("f642 keys", jql=scope)
    ok, msg, _s = cstore.jira_translate(
        c.id, actor="auto", issue_keys=["LOC-1"], transport=fake_jira(log)
    )
    assert ok, msg
    (jql,) = _jql_seen(log)
    assert _valid_order_by(jql), jql
    assert jql.rstrip().lower().endswith("order by created desc"), jql
    assert "key in (LOC-1)" in jql and "project = LOC" in jql, jql
    # free-query path with a caller jql: caller AND scope, scope's ORDER BY kept at the end
    log = []
    c = _conn("f642 caller", jql=scope)
    ok, msg, _s = cstore.jira_translate(
        c.id, actor="auto", jql="labels = loc", transport=fake_jira(log)
    )
    assert ok, msg
    (jql,) = _jql_seen(log)
    assert _valid_order_by(jql), jql
    assert "labels = loc" in jql and "project = LOC" in jql, jql
    # a caller jql that carries its own ORDER BY: still a single trailing ORDER BY
    log = []
    c = _conn("f642 both", jql=scope)
    ok, msg, _s = cstore.jira_translate(
        c.id, actor="auto", jql="labels = loc ORDER BY updated ASC", transport=fake_jira(log)
    )
    assert ok, msg
    (jql,) = _jql_seen(log)
    assert _valid_order_by(jql), jql
    # full pull with the scope alone, and a scope without ORDER BY, are unchanged
    log = []
    c = _conn("f642 full", jql=scope)
    cstore.jira_translate(c.id, actor="auto", transport=fake_jira(log))
    assert _jql_seen(log) == [scope], _jql_seen(log)
    log = []
    c = _conn("f642 plain", jql="project = LOC")
    cstore.jira_translate(c.id, actor="auto", issue_keys=["LOC-1"], transport=fake_jira(log))
    assert _jql_seen(log) == ["(key in (LOC-1)) AND (project = LOC)"], _jql_seen(log)
    # the approximate-count preview shares the same clause
    from app.services.connectors.jira_part2 import JiraConnector

    jc = JiraConnector({"base_url": "https://team.atlassian.net"}, SECRET)
    clause = jc._scoped_clause(None, ["NW-1"], "project = NW ORDER BY created DESC")
    assert _valid_order_by(clause), clause
    print("  642: ORDER BY in the scope JQL stays at the end, outside the parentheses")


# ---------------------------------------------------------------------------
def test_643_comment_mode_keeps_description_structure():
    log = []
    c = _conn("f643", target_mode="comment")
    ok, msg, _s = cstore.jira_translate(c.id, actor="auto", transport=fake_jira(log))
    assert ok, msg
    posts = _posts(log)
    assert len(posts) == 1, posts
    body = posts[0][3]["body"]
    assert body["type"] == "doc", body
    kinds = [n["type"] for n in body["content"]]
    # label paragraph + summary paragraph + intro paragraph + list + closing paragraph
    assert kinds == ["paragraph", "paragraph", "paragraph", "bulletList", "paragraph"], kinds
    lst = body["content"][3]
    assert [i["type"] for i in lst["content"]] == ["listItem"] * 3, lst
    items = [i["content"][0]["content"][0]["text"] for i in lst["content"]]
    assert all(
        t.endswith(w)
        for t, w in zip(items, ("First point", "Second point", "Third point"), strict=True)
    ), items
    assert body["content"][0]["content"][0]["text"].startswith("🌐"), body["content"][0]
    print("  643: comment mode posts the rebuilt ADF (bulletList with 3 items, paragraphs kept)")


# ---------------------------------------------------------------------------
def test_645_scoped_token_401_is_explained():
    from app.services.connectors.jira_part1 import SPEC
    from app.services.connectors.jira_part2 import JiraConnector

    def handler(req):
        return httpx.Response(401, text="Unauthorized; scope does not match")

    jc = JiraConnector(
        {"base_url": "https://team.atlassian.net"}, SECRET, transport=httpx.MockTransport(handler)
    )
    ok, msg = jc.test()
    assert not ok and "401" in msg and "scoped API token" in msg, msg
    assert "api.atlassian.com" in msg and "Create API token" in msg, msg
    # a plain 403 does not get the scoped-token advice
    jc403 = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        SECRET,
        transport=httpx.MockTransport(lambda req: httpx.Response(403, text="nope")),
    )
    ok, msg = jc403.test()
    assert not ok and "scoped" not in msg, msg
    assert any("with scopes" in s for s in SPEC.setup_steps), SPEC.setup_steps
    print("  645: a 401 from test() names the scoped-token cause; setup steps warn about it")


# ---------------------------------------------------------------------------
INLINE_DESC = {
    "type": "doc",
    "version": 1,
    "content": [
        _para(
            _t("Click "),
            _t("here", [{"type": "link", "attrs": {"href": "https://acme.co/reset"}}]),
            _t(" to reset, "),
            _t("now", [{"type": "strong"}]),
            {"type": "mention", "attrs": {"id": "u1", "text": "@Sam"}},
            _t(" thanks"),
        ),
        {"type": "codeBlock", "content": [_t("print('hi')")]},
    ],
}


def test_644_inline_marks_one_segment_per_paragraph():
    from app.filters import adf

    units = adf.extract_inline(INLINE_DESC)
    assert len(units) == 1, ("one unit for the whole sentence, code block untouched", units)
    u = units[0]
    assert u.text == ("Click ⟦g1⟧here⟦/g1⟧ to reset, ⟦g2⟧now⟦/g2⟧⟦x3⟧ thanks"), u.text
    # a translation that REORDERS the spans (Japanese-style): marks follow the words
    ja = "⟦g2⟧今⟦/g2⟧⟦x3⟧ ここを⟦g1⟧クリック⟦/g1⟧"
    tree, bad = adf.reinsert_inline(INLINE_DESC, {0: ja})
    assert not bad, bad
    kids = tree["content"][0]["content"]
    by_text = {k.get("text"): k for k in kids if k["type"] == "text"}
    assert by_text["今"]["marks"] == [{"type": "strong"}], kids
    assert by_text["クリック"]["marks"][0]["attrs"]["href"] == "https://acme.co/reset"
    assert any(k["type"] == "mention" and k["attrs"]["id"] == "u1" for k in kids), kids
    assert tree["content"][1] == INLINE_DESC["content"][1], "code block untouched"
    assert adf.structure_ok_inline(INLINE_DESC, tree)
    # a dropped or invented token is reported, and the source content is kept
    for broken in ("Haga clic aqui", "⟦g1⟧a⟦/g1⟧ ⟦g2⟧b ⟦x3⟧"):
        tree2, bad2 = adf.reinsert_inline(INLINE_DESC, {0: broken})
        assert bad2 == [0], (broken, bad2)
        assert tree2 == INLINE_DESC, "a bad unit keeps the source content"
    # a paragraph with no marks stays plain text (no tokens)
    plain = {"type": "doc", "version": 1, "content": [_para(_t("Just words"))]}
    assert adf.extract_inline(plain)[0].text == "Just words"

    # end to end: field mode into a rich-text target keeps the marks on the translated words
    log = []
    cat = [
        {
            "id": "customfield_10050",
            "schema": {
                "type": "string",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textarea",
            },
        }
    ]
    c = _conn(
        "f644",
        source_field="description",
        target_mode="field",
        target_field="customfield_10050",
    )
    ok, msg, _s = cstore.jira_translate(
        c.id, actor="auto", transport=fake_jira(log, description=INLINE_DESC, catalogue=cat)
    )
    assert ok, msg
    (put,) = _puts(log)
    para = put[3]["fields"]["customfield_10050"]["content"][0]["content"]
    assert [k["type"] for k in para].count("mention") == 1, para
    link = [
        k
        for k in para
        if k["type"] == "text" and k.get("marks") and k["marks"][0]["type"] == "link"
    ]
    assert link and link[0]["text"] == "here", para
    assert not any("⟦" in (k.get("text") or "") for k in para), ("no token leaks", para)
    # review: nested marked spans would silently drop the outer marks -> reported, source kept
    nested = "⟦g1⟧Haga ⟦g2⟧clic⟦/g2⟧⟦/g1⟧, ⟦g2⟧x⟦/g2⟧"
    t3, bad3 = adf.reinsert_inline(INLINE_DESC, {0: "⟦g1⟧a ⟦g2⟧b⟦/g2⟧⟦/g1⟧ ⟦x3⟧"})
    assert bad3 == [0] and t3 == INLINE_DESC, (bad3, nested)

    # review: token loss in a REAL run (MT drops the placeholders) -> nothing is written to
    # the target, the error says the original was kept, no text silently lost
    import unittest.mock as mock

    from sqlmodel import select

    from app.models import Segment
    from app.services import translate as translate_mod

    real_ptd = translate_mod.pretranslate_document

    def drop_tokens(session, doc_id, locale, *a, **kw):
        stats = real_ptd(session, doc_id, locale, *a, **kw)
        for sg in session.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            sg.target_text = "Haga clic aqui"
            session.add(sg)
        session.commit()
        return stats

    log2 = []
    c2 = _conn(
        "f644 loss",
        source_field="description",
        target_mode="field",
        target_field="customfield_10050",
    )
    with mock.patch.object(translate_mod, "pretranslate_document", drop_tokens):
        _ok2, msg2, _s2 = cstore.jira_translate(
            c2.id, actor="auto", transport=fake_jira(log2, description=INLINE_DESC, catalogue=cat)
        )
    assert not _puts(log2), ("token loss must not write the target", _puts(log2))
    assert "kept original" in msg2, msg2
    print("  644: one segment per paragraph with tokens; marks follow the reordered words")


def test_gate_comment_mode_guard_failure_posts_nothing():
    """Gate (jira-1010): in comment mode a description that fails the ADF guard (MT
    dropped a placeholder) must not post a partial summary-only comment, because the
    pair keeps no hash and every later run would post another duplicate."""
    import unittest.mock as mock

    from sqlmodel import select

    from app.models import Segment
    from app.services import translate as translate_mod

    real_ptd = translate_mod.pretranslate_document

    def drop_tokens(session, doc_id, locale, *a, **kw):
        stats = real_ptd(session, doc_id, locale, *a, **kw)
        for sg in session.exec(select(Segment).where(Segment.document_id == doc_id)).all():
            if "⟦" in (sg.source_text or ""):
                sg.target_text = "Haga clic aqui"
                session.add(sg)
        session.commit()
        return stats

    c = _conn("gate comment loss", target_mode="comment")
    for _run in range(2):
        log = []
        with mock.patch.object(translate_mod, "pretranslate_document", drop_tokens):
            _ok, msg, _s = cstore.jira_translate(
                c.id, actor="auto", transport=fake_jira(log, description=INLINE_DESC)
            )
        assert not _posts(log), ("no partial comment on a guard failure", _posts(log))
        assert "kept original" in msg, msg
    # once MT keeps the tokens, the pair posts exactly one whole comment
    log = []
    ok, msg, _s = cstore.jira_translate(
        c.id, actor="auto", transport=fake_jira(log, description=INLINE_DESC)
    )
    assert ok, msg
    assert len(_posts(log)) == 1, _posts(log)
    print("  gate: comment mode posts nothing while the ADF guard fails, then one comment")


def main():
    init_db()
    test_641_field_mode_writes_only_target_field()
    test_641_field_mode_without_target_refused_before_mt()
    test_641_rich_target_gets_adf_and_source_untouched()
    test_642_scope_order_by_stays_outside_parentheses()
    test_643_comment_mode_keeps_description_structure()
    test_645_scoped_token_401_is_explained()
    test_644_inline_marks_one_segment_per_paragraph()
    test_gate_comment_mode_guard_failure_posts_nothing()
    print("JIRA 1010 OK")


if __name__ == "__main__":
    main()
