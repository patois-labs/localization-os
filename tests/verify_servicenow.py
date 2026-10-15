"""ServiceNow Knowledge connector - T3 (one kb_knowledge row per language, linked by Parent)
- fully offline via httpx.MockTransport (a fake ServiceNow instance).

TAXONOMY G - THE FAKE MODELS THE REAL SYSTEM, so the payload assertions are not vacuous:
  * Table API: encoded queries (^ AND, ^OR on the preceding clause, ^ORDERBY), sysparm_fields,
    sysparm_limit / sysparm_offset, sysparm_exclude_reference_link (reference fields are
    {"link","value"} dicts without it), 404 for a missing row, 201 on insert, PATCH = PARTIAL
    update of only the sent keys.
  * UNKNOWN FIELDS ARE SILENTLY IGNORED on insert, in queries and in sysparm_fields (the real
    default), so an instance WITHOUT the I18N plugin has no `language` / `parent` columns and
    a write that sends them is quietly dropped. This is what makes the schema check and the
    client-side parent re-check bite.
  * Auth: Basic or an OAuth client-credentials bearer (`/oauth_token.do`); a wrong or expired
    credential answers 401; a scripted queue can inject 429 + Retry-After / 5xx / 403.
  * The fake does NOT enforce workflow on the API (the real Table API does not either): a
    PATCH to a published row lands. The connector, not the fake, must refuse to send it.

Covers taxonomy A (hostile ids + operator config), B (held title / held body on update),
C (second-run steady state), D (wiki skipped + reported), E (cap), F (held pair records no
hash; workflow-held pair is parked, not done), H (WriteBudget, egress guard, token never
logged, four surfaces), plus the workflow-state rules, rule-6 needs_link, deletion/blank
healing, pagination boundaries, OAuth re-mint, 429 backoff and the Language-field refusal.

Live QA is maintainer-gated (RUNBOOK-servicenow-live-qa.md). ASCII-only prints; final line is
'verify_servicenow OK'.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_servicenow_")

import itertools
import json
import logging
import re
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.services.translate as translate_mod
from app import config as app_config
from app.db import engine, init_db
from app.main import app
from app.models import ConnectorIdMap, Segment, SegStatus
from app.services.connectors import REGISTRY, idmap
from app.services.connectors import base as cbase
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError
from app.services.connectors.servicenow import ServiceNowConnector

KB = "a" * 32
KB2 = "b" * 32
CAT = "c" * 32
BASE = "https://dev12345.service-now.com"
USER, PW = "svc_locos", "pw-secret-9f3a"
CID, CSEC = "client-id-77", "client-secret-55e1"
BODY = (
    '<h2>Reset your password</h2><p>Open <a href="/kb?id=kb_article&sys_id=123">the portal</a> '
    'and press the button.</p><p><img src="/sys_attachment.do?sys_id='
    + "d"
    * 32
    + '" alt="Screenshot">'
    "</p>"
)


def _sid(n: int) -> str:
    return f"{n:032x}"


# --------------------------------------------------------------------------- #
# Fake ServiceNow instance.
# --------------------------------------------------------------------------- #
class FakeServiceNow:
    def __init__(self, *, i18n=True, parent_field=True, langs=("en", "fr", "de"), oauth=False):
        self.i18n = i18n  # False -> no `language` column at all
        self.parent_field = parent_field  # False -> `language` yes, `parent` no
        self.langs = list(langs)
        self.langs_readable = True
        self.oauth = oauth
        self.rows: dict = {}
        self._seq = itertools.count(1)
        self.inserts: list = []
        self.patches: list = []  # (sys_id, body)
        self.requests: list = []  # (method, path, query)
        self.fail_next: list = []  # [(status, headers, body)]
        self.valid_tokens: set = set()
        self.minted = 0
        self.seen_auth: set = set()
        self.reject_bearer_once = False
        self.kbs = {KB: "IT Knowledge", KB2: "HR Knowledge"}
        self.or_top_level = False  # True -> `^OR` binds at the TOP level (the feared reading)

    def transport(self):
        return httpx.MockTransport(self._handle)

    # ---- seeding
    def add(
        self,
        title,
        text=BODY,
        *,
        kb=KB,
        state="published",
        lang="en",
        parent="",
        category=CAT,
        article_type="html",
        sid=None,
        number=None,
    ):
        n = next(self._seq)
        sid = sid or _sid(1000 + n)
        self.rows[sid] = {
            "sys_id": sid,
            "number": number or f"KB{n:07d}",
            "short_description": title,
            "text": text,
            "language": lang,
            "parent": parent,
            "kb_knowledge_base": kb,
            "kb_category": category,
            "workflow_state": state,
            "article_type": article_type,
            "sys_updated_on": "2026-10-01 10:00:00",
        }
        return sid

    def targets_of(self, src, lang=None):
        return [
            r
            for r in self.rows.values()
            if r["parent"] == src and (lang is None or r["language"] == lang)
        ]

    # ---- rendering
    def _visible(self, row):
        out = dict(row)
        if not self.i18n:
            out.pop("language", None)
            out.pop("parent", None)
        elif not self.parent_field:
            out.pop("parent", None)
        return out

    def _render(self, row, fields, exclude_ref):
        full = self._visible(row)
        if not exclude_ref:
            for k in ("parent", "kb_knowledge_base", "kb_category"):
                if full.get(k):
                    full[k] = {"link": f"{BASE}/api/now/table/x/{full[k]}", "value": full[k]}
        if fields:
            full = {k: v for k, v in full.items() if k in fields}
        return full

    # ---- query engine (encoded query subset)
    def _match(self, row, query):
        vis = self._visible(row)
        groups, order = [], None
        toks = [t for t in query.split("^") if t]
        if self.or_top_level:
            real = [t for t in toks if not t.startswith("ORDERBY")]
            idx = next((i for i, t in enumerate(real) if t.startswith("OR")), None)
            if idx is not None:
                order = next((t[7:] for t in toks if t.startswith("ORDERBY")), None)
                left = real[:idx]
                right = [t[2:] for t in real[idx:]]
                return all(self._one(vis, c) for c in left) or any(
                    self._one(vis, c) for c in right
                ), order
        for tok in toks:
            if tok.startswith("ORDERBY"):
                order = tok[len("ORDERBY") :]
                continue
            if tok.startswith("OR") and groups:
                groups[-1].append(tok[2:])
            else:
                groups.append([tok])

        def one(cl):
            if cl.endswith("ISEMPTY"):
                f = cl[: -len("ISEMPTY")]
                return f in vis and not vis.get(f)
            if "=" in cl:
                f, v = cl.split("=", 1)
                if f not in vis:
                    return True  # unknown field in a query is IGNORED (real default)
                return str(vis.get(f)) == v
            return True

        return all(any(one(c) for c in g) for g in groups), order

    @staticmethod
    def _one(vis, cl):
        if cl.endswith("ISEMPTY"):
            f = cl[: -len("ISEMPTY")]
            return f in vis and not vis.get(f)
        if "=" in cl:
            f, v = cl.split("=", 1)
            if f not in vis:
                return True
            return str(vis.get(f)) == v
        return True

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        q = req.url.params
        self.requests.append((method, path, dict(q)))
        if self.fail_next and path.startswith("/api/"):
            st, hdr, body = self.fail_next.pop(0)
            return httpx.Response(st, headers=hdr, json=body or {"error": {"message": "scripted"}})
        if path == "/oauth_token.do":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            if (
                not self.oauth
                or form.get("grant_type") != "client_credentials"
                or form.get("client_id") != CID
                or form.get("client_secret") != CSEC
            ):
                return httpx.Response(401, json={"error": "access_denied"})
            self.minted += 1
            tok = f"tok-{self.minted}"
            self.valid_tokens.add(tok)
            return httpx.Response(200, json={"access_token": tok, "expires_in": 1799})
        if not path.startswith("/api/now/table/"):
            return httpx.Response(404, json={"error": {"message": "no route"}})
        if not self._auth_ok(req):
            return httpx.Response(401, json={"error": {"message": "User Not Authenticated"}})
        rest = path[len("/api/now/table/") :].split("/")
        table, rid = rest[0], (rest[1] if len(rest) > 1 else None)
        exclude = q.get("sysparm_exclude_reference_link") == "true"
        fields = set((q.get("sysparm_fields") or "").split(",")) - {""}
        if table == "sys_language":
            if not self.langs_readable:
                return httpx.Response(403, json={"error": {"message": "ACL"}})
            return httpx.Response(200, json={"result": [{"id": i, "name": i} for i in self.langs]})
        if table == "kb_knowledge_base":
            if rid in self.kbs:
                return httpx.Response(200, json={"result": {"sys_id": rid, "title": self.kbs[rid]}})
            return httpx.Response(404, json={"error": {"message": "No Record found"}})
        if table != "kb_knowledge":
            return httpx.Response(404, json={"error": {"message": "Invalid table"}})
        if rid is None and method == "GET":
            rows = []
            order = None
            for r in self.rows.values():
                ok, order = self._match(r, q.get("sysparm_query", ""))
                if ok:
                    rows.append(r)
            if order:
                rows.sort(key=lambda r: r.get(order, ""))
            limit = int(q.get("sysparm_limit", 10000))
            off = int(q.get("sysparm_offset", 0))
            chunk = rows[off : off + limit]
            return httpx.Response(
                200,
                headers={"X-Total-Count": str(len(rows))},
                json={"result": [self._render(r, fields, exclude) for r in chunk]},
            )
        if rid is None and method == "POST":
            body = json.loads(req.content)
            n = next(self._seq)
            sid = _sid(5000 + n)
            row = {
                "sys_id": sid,
                "number": f"KB{n:07d}",
                "short_description": "",
                "text": "",
                "language": "",
                "parent": "",
                "kb_knowledge_base": "",
                "kb_category": "",
                "workflow_state": "draft",
                "article_type": "html",
                "sys_updated_on": "2026-10-02 10:00:00",
            }
            vis_cols = set(self._visible(row))
            for k, v in body.items():
                if k in vis_cols:  # unknown keys are silently dropped
                    row[k] = v
            self.rows[sid] = row
            self.inserts.append(dict(body))
            return httpx.Response(201, json={"result": self._render(row, set(), exclude)})
        row = self.rows.get(rid)
        if row is None:
            return httpx.Response(404, json={"error": {"message": "No Record found"}})
        if method == "GET":
            return httpx.Response(200, json={"result": self._render(row, fields, exclude)})
        if method == "PATCH":
            body = json.loads(req.content)
            vis_cols = set(self._visible(row))
            for k, v in body.items():
                if k in vis_cols:
                    row[k] = v
            self.patches.append((rid, dict(body)))
            return httpx.Response(200, json={"result": self._render(row, fields, exclude)})
        return httpx.Response(405, json={"error": {"message": "method"}})

    def _auth_ok(self, req) -> bool:
        h = req.headers.get("Authorization", "")
        self.seen_auth.add(h.split(" ")[0])
        if h.startswith("Basic "):
            import base64

            return base64.b64decode(h[6:]).decode() == f"{USER}:{PW}"
        if h.startswith("Bearer "):
            tok = h[7:]
            if self.reject_bearer_once:
                self.reject_bearer_once = False
                self.valid_tokens.discard(tok)
            return tok in self.valid_tokens
        return False


def _conn(name, *, targets="fr, de", extra=None, oauth=False, kb=KB):
    cfg = {
        "base_url": BASE,
        "knowledge_base": kb,
        "source_locale": "en",
        "target_locales": targets,
    }
    cfg.update(extra or {})
    secret = (
        {"client_id": CID, "client_secret": CSEC} if oauth else {"username": USER, "password": PW}
    )
    return cstore.create("servicenow", name, cfg, secret, actor="admin")


def _run(c, fake, **kw):
    return cstore.servicenow_translate(c.id, actor="auto", transport=fake.transport(), **kw)


def _cfg(c):
    return cstore.get(c.id).config or {}


def main() -> None:
    init_db()
    cbase._sleep = lambda *_a, **_k: None
    real_ptd = translate_mod.pretranslate_document
    calls = {"n": 0}

    def counting_ptd(session, doc_id, locale, *a, **k):
        calls["n"] += 1
        return real_ptd(session, doc_id, locale, *a, **k)

    set_pieces("app.services.translate", "pretranslate_document", counting_ptd)

    def _rewrite(real):
        def rw(session, doc_id, locale, spec=None, force_pii=False):
            stats = real(session, doc_id, locale, spec, force_pii=force_pii)
            for sg in session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == locale
                )
            ).all():
                sg.target_text = (
                    "X"
                    + re.sub(
                        r"(⟦[^⟧]*⟧)|([^⟦]+)",
                        lambda m: m.group(1) or m.group(2).upper(),
                        sg.source_text or "",
                    )
                    + "X"
                )
                session.add(sg)
            session.commit()
            return stats

        return rw

    def _hold(which, locale="fr"):
        """A pretranslate that routes chosen segments to NEEDS_REVIEW ('title', 'body' or 'all')."""

        def f(session, doc_id, loc, spec=None, force_pii=False):
            stats = counting_ptd(session, doc_id, loc, spec, force_pii=force_pii)
            if loc == locale:
                for sg in session.exec(
                    select(Segment).where(
                        Segment.document_id == doc_id, Segment.target_locale == loc
                    )
                ).all():
                    if (
                        which == "all"
                        or (which == "title" and sg.order_index == 0)
                        or (which == "body" and sg.order_index == 1)
                    ):
                        sg.status = SegStatus.NEEDS_REVIEW
                        session.add(sg)
                session.commit()
            return stats

        return f

    # ---- (A) taxonomy A: hostile ids and operator config, ZERO external writes ----
    fa = FakeServiceNow()
    src_a = fa.add("Hostile test", "<p>x</p>")
    ca = _conn("A scope")
    conn_a = cstore.build_fresh(ca.id, fa.transport())
    hostile = [
        "../../../api/now/table/sys_user",
        "%2e%2e%2f%2e%2e%2fsys_user",
        src_a[:31],  # a PREFIX of a real id
        src_a + "0",
        "а" * 32,  # cyrillic lookalike
        "a" * 31 + "\x00",
        src_a + "\r\nX-Evil: 1",
        src_a + "?x=1",
        src_a + "#frag",
        "",
        "   ",
    ]
    for bad in hostile:
        try:
            conn_a.discover_articles(article_ids=[bad])
            raise AssertionError(("must reject", bad))
        except ConnectorError:
            pass
    assert not fa.inserts and not fa.patches, "A: hostile ids wrote nothing"
    assert not any("sys_user" in p for _m, p, _q in fa.requests), "A: no request escaped"
    assert conn_a._split_id(src_a.upper()) == src_a, (
        "uppercase hex normalizes, never reaches wire raw"
    )
    for key, bad, meth in (
        ("knowledge_base", KB + "^ORactive=true", "_kb"),
        ("knowledge_base", "abc", "_kb"),
        ("kb_category", CAT + "^NQ", "_category"),
        ("source_locale", "en^ORlanguageISNOTEMPTY", "_source_locale"),
    ):
        cfg = {"base_url": BASE, "knowledge_base": KB, key: bad}
        x = ServiceNowConnector(cfg, {"username": "u", "password": "p"}, transport=fa.transport())
        try:
            getattr(x, meth)()
            raise AssertionError(("config must reject", key, bad))
        except ConnectorError:
            pass
    try:
        conn_a._lang_code("fr^NQ")
        raise AssertionError("target language injection must be rejected")
    except ConnectorError:
        pass
    print("  A: hostile ids + operator config (kb/category/source/target) fail closed, 0 writes")

    # ---- (1) full flow: scope, workflow, translation exclusion, wiki, native link ----
    f1 = FakeServiceNow()
    s1 = f1.add("Reset password", BODY)
    s1b = f1.add("VPN setup", "<p>Install the client.</p>")
    f1.add("Draft article", "<p>draft</p>", state="draft")
    f1.add("Other KB", "<p>other</p>", kb=KB2)
    f1.add("French human", "<p>fr</p>", lang="fr", parent=s1b, state="published")
    f1.add("Odd child", "<p>child</p>", lang="en", parent=s1, state="published")
    f1.add("Wiki one", "[[wiki]]", article_type="wiki")
    c1 = _conn("full")
    ok, msg, r1 = _run(c1, f1)
    assert ok, msg
    assert r1["created"] == 3 and r1["updated"] == 0, r1
    # s1 -> fr, de ; s1b has a human FR translation -> needs_link for fr, de created
    rows = {(r.source_id, r.target_locale): r for r in idmap.list_for_connection(c1.id)}
    assert rows[(s1, "fr")].status == "done" and rows[(s1, "de")].status == "done", rows
    assert rows[(s1b, "fr")].status == "needs_link", "pre-existing human FR translation is flagged"
    assert rows[(s1b, "de")].status == "done"
    t = f1.rows[rows[(s1, "fr")].target_id]
    assert t["workflow_state"] == "draft" and t["language"] == "fr" and t["parent"] == s1, t
    assert t["kb_knowledge_base"] == KB and t["kb_category"] == CAT, t
    assert t["short_description"].startswith("[fr]"), t
    assert 'src="/sys_attachment.do?sys_id=' + "d" * 32 + '"' in t["text"], (
        "attachment ref survives"
    )
    assert '<a href="/kb?id=kb_article&sys_id=123">' in t["text"] or "sys_id=123" in t["text"], t[
        "text"
    ]
    assert f1.rows[s1]["short_description"] == "Reset password" and f1.rows[s1]["text"] == BODY, (
        "source untouched"
    )
    assert not [p for p in f1.patches if p[0] in (s1, s1b)], "never PATCHes a source"
    human = next(r for r in f1.rows.values() if r["short_description"] == "French human")
    assert human["text"] == "<p>fr</p>", "needs_link human translation not overwritten"
    assert r1["source_skips"].get("wiki_unsupported") == 1, ("D: wiki skipped + reported", r1)
    assert r1["source_skips"].get("is_translation", 0) >= 1, r1
    assert "wiki-format" in msg, msg
    assert r1["link_mode"] == "native" and "Parent link available" in msg, msg
    assert "need a link decision" in msg, msg
    assert not any(i.get("workflow_state") != "draft" for i in f1.inserts), (
        "every insert is explicit draft"
    )
    assert "Basic" in f1.seen_auth
    print(
        "  full flow: published sources only, other KB/draft/translation/wiki excluded+reported,"
        " Draft targets linked by Parent, human translation flagged needs_link"
    )

    # ---- (2) fidelity, mutation-proven (rewriting engine) ----
    f2 = FakeServiceNow()
    s2 = f2.add("Doc", BODY)
    c2 = _conn("fidelity", targets="fr")
    set_pieces("app.services.translate", "pretranslate_document", _rewrite(counting_ptd))
    try:
        ok2, m2, _r = _run(c2, f2)
        assert ok2, m2
    finally:
        set_pieces("app.services.translate", "pretranslate_document", counting_ptd)
    body2 = f2.rows[idmap.get(c2.id, s2, "fr").target_id]["text"]
    assert "XRESET YOUR PASSWORDX" in body2, ("engine mutated the prose (test is live)", body2)
    assert (
        'href="/kb?id=kb_article&sys_id=123"' in body2
        and "/sys_attachment.do?sys_id=" + "d" * 32 in body2
    ), body2
    assert "<h2>" in body2 and "</p>" in body2, body2
    print("  fidelity (mutation-proven): prose mutated, tags/href/attachment src byte-identical")

    # ---- (3) C: second run steady state, no loop ----
    n_ins, n_pat, n_pt = len(f1.inserts), len(f1.patches), calls["n"]
    ok, msg3, r3 = _run(c1, f1)
    assert ok and r3["created"] == 0 and r3["updated"] == 0 and r3["skipped"] >= 3, (msg3, r3)
    assert len(f1.inserts) == n_ins and len(f1.patches) == n_pat and calls["n"] == n_pt, (
        "C: no re-translate"
    )
    print("  C: second run over unchanged source is fully skipped (no MT, no writes)")

    # ---- (4) source edit -> UPDATE of the Draft; superseded human edit is reported ----
    tid = idmap.get(c1.id, s1, "fr").target_id
    f1.rows[tid]["text"] = "<p>HUMAN EDIT</p>"  # a human polished the draft
    f1.rows[s1]["text"] = BODY + "<p>New paragraph.</p>"
    ok, msg4, r4 = _run(c1, f1)
    assert ok and r4["updated"] == 2 and r4["created"] == 0, (msg4, r4)  # fr + de of s1
    assert "(fr) " not in f1.rows[tid]["text"] and "New paragraph" in f1.rows[tid]["text"], f1.rows[
        tid
    ]
    assert r4["superseded"], ("human edit overwritten is reported", r4)
    assert len(f1.targets_of(s1, "fr")) == 1, "update, never a duplicate"
    print("  update: changed source PATCHes the same Draft; overwritten human edit is reported")

    # ---- (5) workflow: a published translation is HELD, parked, never edited ----
    f5 = FakeServiceNow()
    s5 = f5.add("Policy", "<p>policy text</p>")
    c5 = _conn("workflow", targets="fr")
    _run(c5, f5)
    t5 = idmap.get(c5.id, s5, "fr").target_id
    f5.rows[t5]["workflow_state"] = "published"
    f5.rows[s5]["text"] = "<p>policy text v2</p>"
    n_pat = len(f5.patches)
    ok, m5, r5 = _run(c5, f5)
    assert len(f5.patches) == n_pat, "published translation is NEVER patched"
    assert "held" in m5 and "ATTENTION" in m5 and r5["held_by_workflow"], (m5, r5)
    assert (_cfg(c5).get("servicenow_parked") or {}).get(f"{s5}:fr"), "parked marker recorded"
    n_pt = calls["n"]
    ok, _m5b, r5b = _run(c5, f5)
    assert calls["n"] == n_pt and r5b["parked"] == 1, ("F: parked pair re-spends no MT", r5b)
    assert len(f5.patches) == n_pat
    f5.rows[t5]["workflow_state"] = "draft"  # human moved it back to draft
    ok, m5c, r5c = _run(c5, f5, only_pairs={(s5, "fr")})
    assert ok and r5c["updated"] == 1 and "v2" in f5.rows[t5]["text"], (m5c, r5c)
    assert not (_cfg(c5).get("servicenow_parked") or {}).get(f"{s5}:fr"), (
        "park cleared after success"
    )
    # retired is held even with update_published=1; published is edited only with the opt-in
    f5.rows[s5]["text"] = "<p>policy v3</p>"
    f5.rows[t5]["workflow_state"] = "published"
    cstore.patch_config(c5.id, {"update_published": "1"})
    ok, m5d, r5d = _run(c5, f5)
    assert ok and "v3" in f5.rows[t5]["text"] and r5d["updated"] == 1, (
        "opt-in edits published",
        m5d,
        r5d,
    )
    # GATE: a live edit is counted and named as LIVE, never folded into the Draft count
    assert r5d["live_edited"] == 1 and r5d["live_edited_articles"], r5d
    assert "LIVE: edited 1 PUBLISHED" in m5d and "updated 0 Draft" in m5d, m5d
    assert r5c["live_edited"] == 0 and "LIVE" not in m5c, (m5c, r5c)
    f5.rows[s5]["text"] = "<p>policy v4</p>"
    f5.rows[t5]["workflow_state"] = "retired"
    n_pat = len(f5.patches)
    ok, _m, r5e = _run(c5, f5)
    assert len(f5.patches) == n_pat and r5e["held_by_workflow"], "retired is never edited"
    f5.rows[t5]["workflow_state"] = "review"
    cstore.patch_config(c5.id, {"update_published": "0"})
    f5.rows[s5]["text"] = "<p>policy v5</p>"
    _run(c5, f5)
    assert len(f5.patches) == n_pat, "a translation in review is never edited"
    print(
        "  workflow: published/review/retired held + parked (no MT loop), draft healed on retry,"
        " update_published opt-in edits published, retired never"
    )

    # ---- (6) taxonomy B/F + #640: held units ----
    f6 = FakeServiceNow()
    s6 = f6.add("Held title", "<p>Held body text</p>")
    c6 = _conn("held", targets="fr")
    set_pieces("app.services.translate", "pretranslate_document", _hold("body"))
    try:
        ok, _m6, r6 = _run(c6, f6)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", counting_ptd)
    assert not f6.inserts and idmap.get(c6.id, s6, "fr") is None, (
        "#640: held body -> not created",
        r6,
    )
    assert not (_cfg(c6).get("servicenow_hashes") or {}), "no hash for a held pair"
    ok, m6b, _r = _run(c6, f6)  # approved: created complete
    t6 = idmap.get(c6.id, s6, "fr").target_id
    assert (
        ok
        and f6.rows[t6]["text"].startswith("<p>[fr]")
        and (_cfg(c6).get("servicenow_hashes") or {})
    ), m6b
    # update with a held BODY: stored translation survives byte-identical, title still updates
    good_text = f6.rows[t6]["text"]
    f6.rows[s6]["short_description"] = "Held title v2"
    f6.rows[s6]["text"] = "<p>Held body text v2</p>"
    set_pieces("app.services.translate", "pretranslate_document", _hold("body"))
    try:
        ok, _m6c, r6c = _run(c6, f6)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", counting_ptd)
    sent = f6.patches[-1][1]
    assert "text" not in sent and "short_description" in sent, (
        "B: held body omitted from PATCH",
        sent,
    )
    assert f6.rows[t6]["text"] == good_text, "B: stored translation survives the held run"
    assert r6c.get("partial_held") == 1, r6c
    assert not (_cfg(c6).get("servicenow_hashes") or {}).get(f"{s6}:fr"), (
        "F: held update records no hash"
    )
    ok, _m, _r6d = _run(c6, f6)  # approved
    assert f6.rows[t6]["text"] != good_text and "v2" in f6.rows[t6]["text"], f6.rows[t6]
    # update with a held TITLE: stored title survives
    f6.rows[s6]["short_description"] = "Held title v3"
    f6.rows[s6]["text"] = "<p>Held body text v3</p>"
    title_before = f6.rows[t6]["short_description"]
    set_pieces("app.services.translate", "pretranslate_document", _hold("title"))
    try:
        _run(c6, f6)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", counting_ptd)
    sent = f6.patches[-1][1]
    assert "short_description" not in sent and "text" in sent, ("B: held title omitted", sent)
    assert f6.rows[t6]["short_description"] == title_before, "B: stored title survives"
    # fully refused -> not created
    f6b = FakeServiceNow()
    s6b = f6b.add("All refused", "<p>x y z</p>")
    c6b = _conn("refused", targets="fr")
    set_pieces("app.services.translate", "pretranslate_document", _hold("all"))
    try:
        _okb, _mb, _rb = _run(c6b, f6b)
    finally:
        set_pieces("app.services.translate", "pretranslate_document", counting_ptd)
    assert not f6b.inserts and idmap.get(c6b.id, s6b, "fr") is None, (
        "fully refused is never created"
    )
    print(
        "  B/F/#640: held body or title omitted from PATCH (stored text survives),"
        " held create refused,"
        " no hash for a held pair, approval completes it"
    )

    # ---- (7) rule 6: needs_link adopt / ignore, and crash orphan reclaim ----
    f7 = FakeServiceNow()
    s7 = f7.add("Orphan src", "<p>orphan</p>")
    pre = f7.add("Manual FR", "<p>fr manuel</p>", lang="fr", parent=s7, state="draft")
    c7 = _conn("rule6", targets="fr")
    ok, _m7, r7 = _run(c7, f7)
    row7 = idmap.get(c7.id, s7, "fr")
    assert row7.status == "needs_link" and row7.target_id == pre and not f7.inserts, (row7, r7)
    assert f7.rows[pre]["text"] == "<p>fr manuel</p>", "not overwritten"
    okr, mr, _ = cstore.servicenow_resolve_link(c7.id, s7, "fr", "adopt", actor="t")
    assert okr, mr
    ok, _m, r7b = _run(c7, f7)
    assert (
        f7.rows[pre]["text"] != "<p>fr manuel</p>" and idmap.get(c7.id, s7, "fr").status == "done"
    ), r7b
    assert len(f7.targets_of(s7, "fr")) == 1, "adopted: no duplicate"
    okr, mr, _ = cstore.servicenow_resolve_link(c7.id, s7, "fr", "bogus", actor="t")
    assert not okr
    print(
        "  rule 6: a Parent-linked human translation is needs_link (never overwritten),"
        " adopt then updates it"
    )

    # ---- (8) instance without Parent / without Language ----
    f8 = FakeServiceNow(parent_field=False)
    s8 = f8.add("No parent field", "<p>text</p>")
    f8.add("Bystander", "<p>b</p>", lang="fr")
    c8 = _conn("noparent", targets="fr")
    ok, m8, r8 = _run(c8, f8)
    assert ok and r8["created"] == 1 and r8["link_mode"] == "absent", (m8, r8)
    assert "NOT linked" in m8, m8
    assert idmap.get(c8.id, s8, "fr").status == "done", "false parent match impossible"
    f8b = FakeServiceNow(i18n=False)
    f8b.add("No i18n", "<p>text</p>")
    c8b = _conn("noi18n", targets="fr")
    ok, m8b, _ = _run(c8b, f8b)
    assert not ok and "I18N" in m8b and not f8b.inserts, (
        "Language field missing -> refuse, 0 writes",
        m8b,
    )
    okt, mt = cstore.build_fresh(c8b.id, f8b.transport()).test()
    print(
        "  schema: no Parent -> runs unlinked and says so;"
        " no Language field -> refused with 0 writes"
    )

    # ---- (9) deletion + blank healing ----
    f9 = FakeServiceNow()
    s9 = f9.add("Heal", "<p>heal me</p>")
    c9 = _conn("heal", targets="fr")
    _run(c9, f9)
    old = idmap.get(c9.id, s9, "fr").target_id
    del f9.rows[old]
    ok, _m, r9 = _run(c9, f9)
    new = idmap.get(c9.id, s9, "fr").target_id
    assert ok and new != old and idmap.get(c9.id, s9, "fr").status == "done", r9
    f9.rows[new]["short_description"] = ""
    f9.rows[new]["text"] = ""
    ok, _m, r9b = _run(c9, f9)
    assert ok and r9b["updated"] == 1 and f9.rows[new]["text"], "blanked target refilled"
    print("  heal: an externally deleted target is recreated, a blanked one refilled")

    # ---- (10) E + pagination boundaries + cap ----
    def _disc(n, cap=None, extra=None):
        fp = FakeServiceNow()
        for i in range(n):
            fp.add(f"a{i}", "<p>x</p>", sid=_sid(90000 + i))
        cx = _conn(f"pag{n}", extra=extra)
        return cstore.build_fresh(cx.id, fp.transport()).discover_articles(cap=cap), fp

    for n in (1, 49, 50, 51, 100, 101):
        got, _fp = _disc(n, cap=500)
        assert len(got) == n, ("pagination returns every row", n, len(got))
    got, _fpx = _disc(60, extra={"max_articles": "7"})
    assert len(got) == 7, ("E: configured cap respected", len(got))
    assert len({r["ref"] for r in got}) == 7
    print("  E/pagination: every row returned at 1/49/50/51/100/101, the cap holds")

    # ---- (11) explicit ids: foreign KB == missing, own target refused, unpublished skipped ----
    f11 = FakeServiceNow()
    mine = f11.add("Mine", "<p>m</p>")
    foreign = f11.add("Foreign", "<p>f</p>", kb=KB2)
    draft = f11.add("Draft", "<p>d</p>", state="draft")
    c11 = _conn("explicit", targets="fr")
    cn = cstore.build_fresh(c11.id, f11.transport())
    got_f = cn.discover_articles(article_ids=[foreign])
    skips_f = dict(cn._skips)
    cn2 = cstore.build_fresh(c11.id, f11.transport())
    got_m = cn2.discover_articles(article_ids=[_sid(777)])  # nonexistent
    assert got_f == got_m == [] and skips_f == cn2._skips == {"not_found": 1}, (skips_f, cn2._skips)
    cn3 = cstore.build_fresh(c11.id, f11.transport())
    assert cn3.discover_articles(article_ids=[draft]) == [] and cn3._skips == {"unpublished": 1}
    ok, _m, _r11 = _run(c11, f11, article_ids=[mine])
    own = idmap.get(c11.id, mine, "fr").target_id
    cn4 = cstore.build_fresh(c11.id, f11.transport())
    assert cn4.discover_articles(article_ids=[own]) == [] and cn4._skips == {"own_translation": 1}
    print(
        "  A/isolation: a foreign-KB id answers exactly like a missing one;"
        " own target, draft refused"
    )

    # ---- (12) auth: OAuth mint, 401 re-mint once, no secret ever logged ----
    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, rec):
            self.lines.append(rec.getMessage())

    cap = _Cap()
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.DEBUG)
    f12 = FakeServiceNow(oauth=True)
    f12.add("OAuth", "<p>o</p>")
    c12 = _conn("oauth", targets="fr", oauth=True)
    f12.reject_bearer_once = True
    ok, m12, r12 = _run(c12, f12)
    assert ok and r12["created"] == 1, m12
    assert f12.minted == 2 and "Bearer" in f12.seen_auth and "Basic" not in f12.seen_auth, (
        "token minted, then re-minted exactly once after a 401",
        f12.minted,
    )
    bad = _conn("oauth-bad", targets="fr", oauth=True)
    cstore.update(bad.id, "oauth-bad", {}, {"client_id": CID, "client_secret": "WRONG-SECRET-123"})
    ok, mbad, _ = _run(bad, f12)
    assert not ok and "OAuth" in mbad and "WRONG-SECRET-123" not in mbad, mbad
    fbad = FakeServiceNow()
    cbasic = _conn("basic-bad", targets="fr")
    cstore.update(cbasic.id, "basic-bad", {}, {"username": USER, "password": "NOPE-pass-1"})
    ok, mb, _ = _run(cbasic, fbad)
    assert not ok and "credentials" in mb and "NOPE-pass-1" not in mb, mb
    for secret in (PW, CSEC, "tok-1", "tok-2", "NOPE-pass-1", "WRONG-SECRET-123"):
        assert secret not in "\n".join(cap.lines), ("secret in logs", secret)
    logging.getLogger().removeHandler(cap)
    okt, mt = cstore.build_fresh(c12.id, f12.transport()).test()
    assert okt and "IT Knowledge" in mt, mt
    cnone = cstore.create(
        "servicenow", "nocreds", {"base_url": BASE, "knowledge_base": KB}, {}, actor="admin"
    )
    okn, mn = cstore.build_fresh(cnone.id, f12.transport()).test()
    assert not okn and "credentials are required" in mn, mn
    print(
        "  auth: OAuth minted once and re-minted once on 401; bad creds explained;"
        " no secret in logs/messages"
    )

    # ---- (13) rate limit + vendor errors ----
    f13 = FakeServiceNow()
    f13.add("Rate", "<p>r</p>")
    c13 = _conn("rate", targets="fr")
    f13.fail_next = [(429, {"Retry-After": "1"}, None), (429, {"Retry-After": "1"}, None)]
    ok, m13, r13 = _run(c13, f13)
    assert ok and r13["created"] == 1, ("429 backed off via base._request", m13)
    f13b = FakeServiceNow()
    f13b.add("Err", "<p>e</p>")
    c13b = _conn("err", targets="fr")
    f13b.fail_next = [(403, {}, {"error": {"message": "Insufficient rights"}})]
    ok, m13b, _ = _run(c13b, f13b)
    assert not ok and "denied access" in m13b and not f13b.inserts, m13b
    f13c = FakeServiceNow()
    f13c.add("Err2", "<p>e</p>")
    c13c = _conn("err2", targets="fr")
    f13c.fail_next = [(500, {}, {"error": {"message": "boom"}})] * 3
    ok, m13c, _ = _run(c13c, f13c)
    assert not ok and "500" in m13c, m13c
    print(
        "  vendor: 429 Retry-After retried, 403 explained, persistent 5xx surfaces as a run error"
    )

    # ---- (14) languages: inactive rejected, source never a target, unreadable list noted ----
    f14 = FakeServiceNow(langs=("en", "fr"))
    f14.add("Lang", "<p>l</p>")
    c14 = _conn("langs", targets="fr, de, en")
    ok, m14, r14 = _run(c14, f14)
    assert ok and r14["created"] == 1 and set(r14["rejected"]) == {"de", "en"}, (m14, r14)
    f14b = FakeServiceNow()
    f14b.langs_readable = False
    f14b.add("Lang2", "<p>l</p>")
    c14b = _conn("langs2", targets="fr")
    ok, m14b, r14b = _run(c14b, f14b)
    assert ok and r14b["created"] == 1 and "could not be read" in m14b, m14b
    print(
        "  languages: inactive/source locales rejected, unreadable language list is noted not fatal"
    )

    # ---- (15) H: WriteBudget + egress guard ----
    saved = getattr(app_config, "MAX_WRITES_PER_RUN", None)
    app_config.MAX_WRITES_PER_RUN = 1
    try:
        f15 = FakeServiceNow()
        f15.add("Budget", "<p>b</p>")
        c15 = _conn("budget", targets="fr, de")
        ok, _m, r15 = _run(c15, f15)
        assert ok and r15["ceiling_hit"] and (r15["created"] + r15["updated"]) == 1, r15
        assert len(f15.inserts) == 1, ("exactly one external write", f15.inserts)
    finally:
        app_config.MAX_WRITES_PER_RUN = saved
    from app.services.connectors import egress_guard

    f16 = FakeServiceNow()
    f16.add("Egress", "<p>e</p>")
    c16 = _conn("egress", targets="fr")
    real = egress_guard.egress_is_real
    egress_guard.egress_is_real = lambda t: True
    try:
        ok, m16, _ = _run(c16, f16)
    finally:
        egress_guard.egress_is_real = real
    assert not ok and not f16.inserts and not f16.patches, ("mock MT + real egress refused", m16)
    print("  H: WriteBudget stops after exactly one write; egress guard refuses mock MT, 0 writes")

    # ---- (16) retry only failed pairs; GDPR delete; credential pin ----
    f17 = FakeServiceNow()
    x = f17.add("X", "<p>x</p>")
    y = f17.add("Y", "<p>y</p>")
    c17 = _conn("retry", targets="fr, de")
    okr, _m, _s = cstore.retry_failed(
        c17.id,
        [
            {"article_id": x, "locale": "fr", "error": "500"},
            {"article_id": y, "locale": "de", "error": "500"},
        ],
        actor="auto",
        transport=f17.transport(),
    )
    assert okr and {(r.source_id, r.target_locale) for r in idmap.list_for_connection(c17.id)} == {
        (x, "fr"),
        (y, "de"),
    }, "retry only the failed pairs"
    cfg17 = _cfg(c17)
    assert "servicenow_hashes" in cfg17 and not any("watermark" in k for k in cfg17), list(cfg17)
    cstore.update(c17.id, "retry", {"base_url": "https://attacker.example"}, None)
    assert not cstore.dec_secret(cstore.get(c17.id).secret_enc), (
        "moving base_url clears the credential"
    )
    cstore.delete(c17.id)
    with Session(engine) as s:
        assert not s.exec(
            select(ConnectorIdMap).where(ConnectorIdMap.connection_id == c17.id)
        ).all()
    print(
        "  regression: retry only failed pairs; no watermark;"
        " repointing base_url drops the credential; GDPR delete"
    )

    # ---- (18) REVIEW: a re-versioned source keeps its identity (number, not sys_id) ----
    f18 = FakeServiceNow()
    s18 = f18.add("Guide", "<p>v1 text</p>", number="KB0000042")
    c18 = _conn("versioned", targets="fr")
    _run(c18, f18)
    t18 = idmap.get(c18.id, s18, "fr").target_id
    # A human edits the live article: ServiceNow mints a NEW row (new sys_id, same number)
    # and the old one becomes outdated.
    f18.rows[s18]["workflow_state"] = "outdated"
    s18b = f18.add("Guide", "<p>v2 text</p>", number="KB0000042")
    n_ins = len(f18.inserts)
    ok, m18, _r18 = _run(c18, f18)
    assert ok and len(f18.inserts) == n_ins, ("no second Draft for a new version", m18)
    assert "v2" in f18.rows[t18]["text"], "the SAME Draft is updated from the new version"
    assert idmap.get(c18.id, s18b, "fr") is None and len(f18.targets_of(s18, "fr")) == 1
    assert (_cfg(c18).get("servicenow_numbers") or {}).get("KB0000042") == s18
    # an explicit id naming the superseded row resolves to the live version, same identity
    cn18 = cstore.build_fresh(c18.id, f18.transport())
    got18 = cn18.discover_articles(article_ids=[s18])
    assert [(g["ref"], g["id"]) for g in got18] == [(s18, s18b)], got18
    f18.rows[s18b]["text"] = "<p>v3 text</p>"
    ok, _m, r18b = _run(c18, f18, article_ids=[s18])
    assert ok and r18b["updated"] == 1 and len(f18.inserts) == n_ins
    # a Draft that was never published is still refused
    cn18b = cstore.build_fresh(c18.id, f18.transport())
    d18 = f18.add("Never live", "<p>d</p>", state="draft", number="KB0000099")
    assert cn18b.discover_articles(article_ids=[d18]) == [] and cn18b._skips == {"unpublished": 1}
    # an outdated (superseded) translation row is never an edit target, even with the opt-in
    f18.rows[t18]["workflow_state"] = "outdated"
    cstore.patch_config(c18.id, {"update_published": "1"})
    f18.rows[s18b]["text"] = "<p>v4 text</p>"
    n_pat = len(f18.patches)
    _run(c18, f18)
    assert len(f18.patches) == n_pat, "an outdated (superseded) row is never PATCHed"
    # GATE: a human translation whose Parent names the LIVE version (made after the source was
    # re-versioned) is found by the rule-6 probe: needs_link, never a duplicate Draft.
    f18c = FakeServiceNow()
    v1 = f18c.add("Doc", "<p>one</p>", number="KB0000077")
    c18c = _conn("versioned-probe", targets="fr")
    _run(c18c, f18c)
    f18c.rows[v1]["workflow_state"] = "outdated"
    v2 = f18c.add("Doc", "<p>two</p>", number="KB0000077")
    human = f18c.add("Doc DE", "<p>zwei</p>", lang="de", parent=v2, state="draft")
    cstore.patch_config(c18c.id, {"target_locales": "fr, de"})
    n_ins = len(f18c.inserts)
    _run(c18c, f18c)
    assert len(f18c.inserts) == n_ins, ("no duplicate de Draft beside the human one", f18c.inserts)
    row18c = idmap.get(c18c.id, v1, "de")
    assert row18c is not None and row18c.status == idmap.NEEDS_LINK, row18c
    assert f18c.rows[human]["text"] == "<p>zwei</p>", "the human translation is untouched"
    print("  review: re-versioned source keeps its identity, outdated never edited")

    # ---- (19) REVIEW: a top-level `^OR` cannot widen the source set; https only ----
    f19 = FakeServiceNow()
    f19.or_top_level = True
    live19 = f19.add("Live", "<p>l</p>")
    f19.add("Draft empty lang", "<p>d</p>", state="draft", lang="")
    f19.add("Other KB empty lang", "<p>o</p>", kb=KB2, lang="")
    c19 = _conn("or-binding", targets="fr")
    cn19 = cstore.build_fresh(c19.id, f19.transport())
    got19 = cn19.discover_articles()
    assert [g["ref"] for g in got19] == [live19], ("sources survive a wrong ^OR binding", got19)
    for bad_url in ("http://dev12345.service-now.com", "ftp://x.service-now.com"):
        x = ServiceNowConnector(
            {"base_url": bad_url, "knowledge_base": KB},
            {"username": "u", "password": "p"},
            transport=f19.transport(),
        )
        try:
            x._base()
            raise AssertionError(("must refuse non-https", bad_url))
        except ConnectorError:
            pass
    print("  review: a top-level ^OR cannot admit Drafts or other KBs; http:// refused")

    # ---- (20) GATE: scope fields and the live-edit opt-in are admin only ----
    from app.models import Role, User
    from app.services import apikeys, security, settings
    from tests._csrf import csrf_for

    def _user(email, role):
        with Session(engine) as s:
            u = User(email=email, name=email, role=role, active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
            return u.id

    c20 = _conn("scope-gate", targets="fr")
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        api = TestClient(app)
        _r, ltok = apikeys.create_key(_user("ling-sn@x.test", Role.LINGUIST), "sn-ling")
        _r, atok = apikeys.create_key(_user("admin-sn@x.test", Role.ADMIN), "sn-admin")
        lh, ah = {"X-Loc-Token": ltok}, {"X-Loc-Token": atok}
        for cfg in (
            {"update_published": "1"},
            {"update_published": "true"},
            {"knowledge_base": KB2},
            {"kb_category": "e" * 32},
            {"kb_category": CAT},
        ):
            r = api.post(f"/api/connectors/{c20.id}/update", headers=lh, json={"config": cfg})
            assert r.status_code == 403 and "Only an admin" in r.text, (cfg, r.status_code, r.text)
        cf = _cfg(c20)
        assert cf["knowledge_base"] == KB and not cf.get("update_published"), cf
        # a LINGUIST still edits everything else, may resend the same values, may turn it off
        r = api.post(
            f"/api/connectors/{c20.id}/update",
            headers=lh,
            json={"config": {"knowledge_base": KB.upper(), "max_articles": "7"}},
        )
        assert r.status_code == 200 and _cfg(c20)["max_articles"] == "7", r.text
        r = api.post(
            "/api/connectors",
            headers=lh,
            json={
                "kind": "servicenow",
                "name": "ling-live",
                "config": {"base_url": BASE, "knowledge_base": KB, "update_published": "1"},
            },
        )
        assert r.status_code == 403 and "Only an admin" in r.text, (r.status_code, r.text)
        # an admin may do all of it
        r = api.post(
            f"/api/connectors/{c20.id}/update",
            headers=ah,
            json={"config": {"update_published": "1", "kb_category": CAT}},
        )
        assert r.status_code == 200 and _cfg(c20)["update_published"] == "1", r.text
        r = api.post(
            f"/api/connectors/{c20.id}/update",
            headers=lh,
            json={"config": {"update_published": "1"}},
        )
        assert r.status_code == 200, ("resending an admin-set value is not a change", r.text)
        r = api.post(
            f"/api/connectors/{c20.id}/update",
            headers=lh,
            json={"config": {"update_published": "0"}},
        )
        assert r.status_code == 200 and _cfg(c20)["update_published"] == "0", r.text
        # web form, LINGUIST: same rules (the form re-posts every field)
        pw = "sn-scope-pass-123456"  # pregate: allow-secret throwaway fixture login
        security.create_user("ling-sn-web@x.test", "ling-sn-web@x.test", Role.LINGUIST, pw)
        web = TestClient(app)
        csrf_for(web)
        r = web.post(
            "/auth/login",
            data={"email": "ling-sn-web@x.test", "password": pw, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        csrf_for(web)
        form = {
            "name": "scope-gate",
            "base_url": BASE,
            "knowledge_base": KB,
            "source_locale": "en",
            "target_locales": "fr",
            "kb_category": CAT,
            "update_published": "0",
        }
        for k, v in (("update_published", "1"), ("knowledge_base", KB2)):
            r = web.post(
                f"/connectors/connection/{c20.id}", data={**form, k: v}, follow_redirects=False
            )
            assert r.status_code == 303 and "ok=0" in r.headers["location"], (k, r.headers)
        # own credentials do not unlock a scope move (they would merge over the stored one)
        own_creds = {"username": "mine", "password": "x"}  # pregate: allow-secret fixture
        r = web.post(
            f"/connectors/connection/{c20.id}",
            data={**form, "knowledge_base": KB2, **own_creds},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "ok=0" in r.headers["location"], r.headers
        assert _cfg(c20)["knowledge_base"].lower() == KB and _cfg(c20)["update_published"] == "0"
        r = web.post(f"/connectors/connection/{c20.id}", data=form, follow_redirects=False)
        assert r.status_code == 303 and "ok=1" in r.headers["location"], r.headers
        r = web.post(
            "/connectors/servicenow",
            data={**form, "name": "ling-web-live", "update_published": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "Only%20an%20admin" in r.headers["location"], r.headers
        assert not [x for x in cstore.list_connections() if x.name == "ling-web-live"]
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  gate: LINGUIST cannot move KB/category or turn on live edits (web + /api)")

    # ---- (17) parity surfaces ----
    assert "servicenow" in REGISTRY
    c = TestClient(app)
    from tests._api_fixture import user_api_token

    hdr = {"X-Loc-Token": user_api_token()}
    assert (
        c.post("/api/connectors/99999999/servicenow/translate", json={}, headers=hdr).status_code
        == 404
    )
    assert (
        c.post("/api/connectors/99999999/servicenow/resolve-link", json={}, headers=hdr).status_code
        == 404
    )
    assert "ServiceNow" in c.get("/connectors").text, "tile"
    typ = c.get("/connectors/servicenow").text
    assert (
        "Create connection" in typ
        and "Knowledge base sys_id" in typ
        and "live qa pending" in typ.lower()
    ), "add form"
    page = c.get(f"/connectors/connection/{c1.id}").text
    assert (
        "/servicenow/translate" in page
        and "Translated articles" in page
        and "Needs a link decision" in page
    ), "panel"
    assert "/servicenow/resolve-link" in page
    assert REGISTRY["servicenow"].spec.supports_oauth is False
    from app.cli.cmd_connector import _servicenow_resolve_link, _servicenow_translate

    fcli = FakeServiceNow()
    scli = fcli.add("cli", "<p>x</p>")
    conncli = _conn("cli", targets="fr")

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
        res = None

        def result(self, msg, data):
            self.res = (msg, data)

    real_t = cstore.servicenow_translate

    def _cap(
        cid,
        article_ids=None,
        target_locales=None,
        actor="",
        transport=None,
        only_pairs=None,
        source="manual",
    ):
        return real_t(
            cid,
            article_ids=article_ids,
            target_locales=target_locales,
            actor=actor,
            transport=fcli.transport(),
            only_pairs=only_pairs,
        )

    set_pieces("app.services.connectors.store", "servicenow_translate", _cap)
    try:
        emit = _Emit()
        assert _servicenow_translate(_Args(), emit) == 0 and emit.res[1]["ok"], emit.res
    finally:
        set_pieces("app.services.connectors.store", "servicenow_translate", real_t)
    emit2 = _Emit()
    assert _servicenow_resolve_link(_Args(), emit2) in (0, 1) and emit2.res is not None
    # the generic parity check the factory generated
    from app.cli.__main__ import build_parser

    leaves = _leaf_paths(build_parser())
    assert {"connector servicenow-translate", "connector servicenow-resolve-link"} <= leaves
    from starlette.routing import Route

    from app.web._routes import walk_routes

    api_paths = {r.path for r in walk_routes(app) if isinstance(r, Route)}
    assert "/api/connectors/{cid}/servicenow/translate" in api_paths
    assert "/api/connectors/{cid}/servicenow/resolve-link" in api_paths
    print("  parity: REGISTRY + tile + add form + id-map panel + /api routes + CLI verbs")
    print("verify_servicenow OK")


def _leaf_paths(parser, prefix=""):
    import argparse

    out = set()
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        return {prefix} if prefix else set()
    for sp in subs:
        for name, subparser in sp.choices.items():
            out |= _leaf_paths(subparser, f"{prefix} {name}".strip())
    return out


if __name__ == "__main__":
    main()
