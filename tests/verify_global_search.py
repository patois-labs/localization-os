"""Global search (#595) -- hermetic + offline.

  1. Groups: projects (name + customer name), documents (file name), TM (source/target) and
     glossary terms each return their own hits and counts; archived projects and a key
     project's internal `keys` documents are left out; a hit that starts with the query sorts
     ahead of one that merely contains it.
  2. RBAC isolation (sign-in on, project scoping on): a translator who is a member of project A
     sees NOTHING from project B in any group, and the counts do not include it; a
     customer-bound user sees only its own customer's projects, documents, TM and glossaries;
     glossaries with no customer are staff-only; another org's rows never appear.
  3. Bounds: a query shorter than 2 or longer than 100 characters, an unknown group and a bad
     limit are refused with a sentence; limit is clamped to 25; offset pages one group; counts
     stop at COUNT_CAP and say `capped`; the TM scan is limited to TM_SCAN_CAP rows and says
     `truncated`; LIKE wildcards in the query match literally.
  4. Surfaces: GET /api/search (token), GET /search and GET /search/suggest (session), the
     `search` CLI verb (in-process and --remote) and the MCP tool all answer from the same
     service; a hidden project's name never appears in any of them.
  5. Honest copy: the placeholder and the palette row name what is searched; every catalog has
     the same page.search keys.

Final line: "GLOBAL SEARCH OK".
"""
# ruff: noqa: E501, RUF059

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_gsearch_")

from datetime import datetime

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli import _remote
from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Glossary,
    GlossaryEntry,
    Project,
    Role,
    SegmentTM,
)
from app.services import access, apikeys, global_search, security, settings
from tests._cli_runner import run_cli
from tests._csrf import csrf_for

PASSWORD = "Atlas meadow copper 2026!"  # pregate: allow-secret disposable fixture


def _add(session, row):
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def build():
    """Two customers, each with a project, document, TM entry and glossary term; one hidden
    project's text carries the marker 'zebra' so a leak is easy to spot."""
    ids = {}
    with Session(engine) as s:
        c1 = _add(s, Customer(name="Northstar Labs"))
        c2 = _add(s, Customer(name="Orchard Foods"))
        ids["c1"], ids["c2"] = c1.id, c2.id
        pa = _add(
            s, Project(name="Atlas launch", source_locale="en", customer_id=c1.id, created_by="x")
        )
        pb = _add(
            s, Project(name="Zebra rollout", source_locale="en", customer_id=c2.id, created_by="x")
        )
        pc = _add(
            s,
            Project(
                name="Atlas archive",
                source_locale="en",
                customer_id=c1.id,
                created_by="x",
                archived_at=datetime.utcnow(),
            ),
        )
        ids["pa"], ids["pb"], ids["pc"] = pa.id, pb.id, pc.id
        _add(s, Document(project_id=pa.id, filename="atlas-guide.docx"))
        _add(s, Document(project_id=pb.id, filename="zebra-manual.docx"))
        _add(s, Document(project_id=pa.id, filename="atlas-keys-internal.json", fmt="keys"))
        for i, (pid, word) in enumerate(((pa.id, "atlas"), (pb.id, "zebra"))):
            _add(
                s,
                SegmentTM(
                    project_id=pid,
                    source_locale="en",
                    target_locale="de",
                    source_text=f"Welcome to {word}",
                    target_text=f"Willkommen bei {word}",
                    source_hash=f"h{i}",
                ),
            )
        # A TM row with no project belongs to no customer and is never searched.
        _add(
            s,
            SegmentTM(
                project_id=None,
                source_locale="en",
                target_locale="de",
                source_text="Orphan atlas",
                target_text="Waise",
                source_hash="h-null",
            ),
        )
        g1 = _add(s, Glossary(name="Atlas terms", customer_id=c1.id))
        g2 = _add(s, Glossary(name="Zebra terms", customer_id=c2.id))
        g3 = _add(s, Glossary(name="Unassigned terms", customer_id=None))
        for g, term in ((g1, "atlas cloud"), (g2, "zebra cloud"), (g3, "legacy cloud")):
            _add(
                s,
                GlossaryEntry(
                    glossary_id=g.id,
                    source_locale="en",
                    target_locale="de",
                    source_term=term,
                    target_term=term.upper(),
                ),
            )
        ids["g1"], ids["g2"], ids["g3"] = g1.id, g2.id, g3.id
    return ids


#: The four groups #595 shipped. Workflows and connections (#621) have their own suite
#: (verify_search_workflows_connections_621), and this fixture world holds none of either.
LEGACY = ("projects", "documents", "tm", "glossary")


def counts(result):
    return {g["key"]: g["count"] for g in result["groups"] if g["key"] in LEGACY}


def titles(result):
    return {
        g["key"]: [h["title"] for h in g["hits"]] for g in result["groups"] if g["key"] in LEGACY
    }


def test_groups(ids):
    with Session(engine) as s:
        res = global_search.search(s, global_search.scope_for_org(s, None), "atlas")
        assert counts(res) == {"projects": 1, "documents": 1, "tm": 1, "glossary": 1}, counts(res)
        t = titles(res)
        assert t["projects"] == ["Atlas launch"], t  # the archived one is left out
        assert t["documents"] == ["atlas-guide.docx"], t  # the internal keys document is left out
        assert t["tm"] == ["Welcome to atlas"], t  # the project-less TM row is never searched
        assert t["glossary"] == ["atlas cloud"], t
        assert (
            res["total"] == 4 and "inside documents" in res["searched"]
        ) or "Text inside documents" in res["searched"]
        # Customer name and target-side text match too.
        assert (
            counts(global_search.search(s, global_search.scope_for_org(s, None), "northstar"))[
                "projects"
            ]
            == 1
        )
        assert (
            counts(global_search.search(s, global_search.scope_for_org(s, None), "willkommen"))[
                "tm"
            ]
            == 2
        )
        # One group only, hits carry a link into the app.
        one = global_search.search(
            s, global_search.scope_for_org(s, None), "zebra", groups=["projects"]
        )
        assert [g["key"] for g in one["groups"]] == ["projects"]
        assert one["groups"][0]["hits"][0]["href"] == f"/projects/{ids['pb']}"
        # Starts-with sorts ahead of contains.
        _add_project = Project(
            name="The atlas guide", source_locale="en", customer_id=ids["c1"], created_by="x"
        )
        s.add(_add_project)
        s.commit()
        ranked = titles(
            global_search.search(
                s, global_search.scope_for_org(s, None), "atlas", groups=["projects"]
            )
        )
        assert ranked["projects"] == ["Atlas launch", "The atlas guide"], ranked
        s.delete(_add_project)
        s.commit()
    print(
        "  groups: projects, documents, TM and glossary terms; archived, keys docs and orphan TM left out OK"
    )


def test_bounds(ids):
    with Session(engine) as s:
        scope = global_search.scope_for_org(s, None)
        for bad, needle in (
            ("a", "at least 2"),
            ("  ", "at least 2"),
            ("x" * 101, "limited to 100"),
        ):
            try:
                global_search.search(s, scope, bad)
                raise AssertionError(f"accepted {bad[:5]!r}")
            except global_search.SearchQueryError as exc:
                assert needle in str(exc), str(exc)
        for kwargs in ({"groups": ["nope"]}, {"limit": "many"}, {"offset": "x"}):
            try:
                global_search.search(s, scope, "atlas", **kwargs)
                raise AssertionError(f"accepted {kwargs}")
            except global_search.SearchQueryError:
                pass
        # Wildcards are literal: '%' and '_' match nothing here, they are not "match anything".
        assert global_search.search(s, scope, "%%")["total"] == 0
        assert global_search.search(s, scope, "__")["total"] == 0
        # limit is clamped to 25, offset pages.
        for i in range(30):
            s.add(
                Project(
                    name=f"Bulk {i:02d}", source_locale="en", customer_id=ids["c1"], created_by="x"
                )
            )
        s.commit()
        big = global_search.search(s, scope, "bulk", groups=["projects"], limit=500)
        assert (
            big["limit"] == 25
            and len(big["groups"][0]["hits"]) == 25
            and big["groups"][0]["count"] == 30
        )
        rest = global_search.search(s, scope, "bulk", groups=["projects"], limit=25, offset=25)
        assert len(rest["groups"][0]["hits"]) == 5
        seen = {h["id"] for h in big["groups"][0]["hits"]} | {
            h["id"] for h in rest["groups"][0]["hits"]
        }
        assert len(seen) == 30, "paging must not repeat or drop a hit"
        # Count cap.
        old = global_search.COUNT_CAP
        global_search.COUNT_CAP = 10
        try:
            capped = global_search.search(s, scope, "bulk", groups=["projects"])
            assert capped["groups"][0]["count"] == 10 and capped["groups"][0]["capped"] is True
        finally:
            global_search.COUNT_CAP = old
        # TM scan window: only the newest rows are scanned and the answer says so.
        for i in range(12):
            s.add(
                SegmentTM(
                    project_id=ids["pa"],
                    source_locale="en",
                    target_locale="de",
                    source_text=f"Needle {i}",
                    target_text="x",
                    source_hash=f"n{i}",
                )
            )
        s.commit()
        old = global_search.TM_SCAN_CAP
        global_search.TM_SCAN_CAP = 5
        try:
            tm = global_search.search(s, scope, "needle", groups=["tm"])["groups"][0]
            assert tm["count"] == 5 and tm["truncated"] is True, tm
            assert "Needle 11" in [h["title"] for h in tm["hits"]]
        finally:
            global_search.TM_SCAN_CAP = old
        tm = global_search.search(s, scope, "needle", groups=["tm"])["groups"][0]
        assert tm["count"] == 12 and tm["truncated"] is False
    print(
        "  bounds: length, group, limit clamp, paging, count cap, TM scan window, literal wildcards OK"
    )


def make_users(ids):
    security.seed_initial()
    admin = security.get_by_email("admin")
    translator = security.create_user(
        "tr1@example.test", "Translator One", Role.TRANSLATOR, PASSWORD
    )
    bound = security.create_user(
        "bound@example.test", "Bound", Role.LINGUIST, PASSWORD, customer_id=ids["c1"]
    )
    manager = security.create_user("mgr@example.test", "Manager", Role.MANAGER, PASSWORD)
    with Session(engine) as s:
        assert access.add_member(s, ids["pa"], translator.email)[0]
    return admin, translator, bound, manager


def search_as(user, q, **kw):
    with Session(engine) as s:
        return global_search.search(s, global_search.scope_for_user(s, user), q, **kw)


def test_rbac(ids, users):
    admin, translator, bound, manager = users
    settings.set_many({"project_scoping": "1"})
    # Staff with project scoping on: the translator is only in project A.
    t = search_as(translator, "zebra")
    assert t["total"] == 0 or counts(t)["glossary"] == 1, counts(t)
    assert counts(t)["projects"] == 0 and counts(t)["documents"] == 0 and counts(t)["tm"] == 0, (
        counts(t)
    )
    for g in t["groups"]:
        assert "zebra" not in json.dumps(g["hits"]).lower() or g["key"] == "glossary", g
    a = search_as(translator, "atlas")
    assert counts(a)["projects"] == 1 and counts(a)["documents"] == 1 and counts(a)["tm"] == 1, (
        counts(a)
    )
    # Admin sees both.
    z = search_as(admin, "zebra")
    assert counts(z)["projects"] == 1 and counts(z)["documents"] == 1 and counts(z)["tm"] == 1
    # Membership revoked: the same user now sees nothing from project A.
    with Session(engine) as s:
        access.remove_member(s, ids["pa"], translator.id)
    gone = search_as(translator, "atlas")
    assert counts(gone)["projects"] == counts(gone)["documents"] == counts(gone)["tm"] == 0, counts(
        gone
    )
    with Session(engine) as s:
        access.add_member(s, ids["pa"], translator.email)
    # Customer-bound user: only customer 1, glossaries of customer 1 only, no unassigned ones.
    for word in ("zebra", "legacy"):
        r = search_as(bound, word)
        assert r["total"] == 0, (word, counts(r))
    r = search_as(bound, "atlas")
    assert counts(r) == {"projects": 1, "documents": 1, "tm": 1, "glossary": 1}, counts(r)
    # Glossary with no customer: staff (manager+) only; a translator never sees it.
    assert counts(search_as(manager, "legacy"))["glossary"] == 1
    assert counts(search_as(translator, "legacy"))["glossary"] == 0
    # Unrestricted sign-in-off operator scope sees everything.
    with Session(engine) as s:
        full = global_search.search(
            s, global_search.scope_for_org(s, None), "cloud", groups=["glossary"]
        )
        assert full["groups"][0]["count"] == 3
    print(
        "  RBAC: membership, revoked membership, customer binding and staff-only glossaries hold in every group OK"
    )


def test_org_ring(ids):
    """Multi-org: a caller in org 1 never sees org 2's rows, in any group."""
    with Session(engine) as s:
        o1 = access.create_org(s, "Org One")
        o2 = access.create_org(s, "Org Two")
        c_a = _add(s, Customer(name="Org1 customer", org_id=o1.id))
        c_b = _add(s, Customer(name="Org2 customer", org_id=o2.id))
        _add(
            s,
            Project(
                name="Quasar alpha",
                source_locale="en",
                customer_id=c_a.id,
                org_id=o1.id,
                created_by="x",
            ),
        )
        p_b = _add(
            s,
            Project(
                name="Quasar beta",
                source_locale="en",
                customer_id=c_b.id,
                org_id=o2.id,
                created_by="x",
            ),
        )
        _add(s, Document(project_id=p_b.id, filename="quasar-b.docx"))
        g_b = _add(s, Glossary(name="Quasar glossary B", customer_id=c_b.id, org_id=o2.id))
        _add(
            s,
            GlossaryEntry(
                glossary_id=g_b.id,
                source_locale="en",
                target_locale="de",
                source_term="quasar term",
                target_term="q",
            ),
        )
        _add(
            s,
            SegmentTM(
                project_id=p_b.id,
                source_locale="en",
                target_locale="de",
                source_text="Quasar text",
                target_text="q",
                source_hash="qq",
            ),
        )
        scope1 = global_search.scope_for_org(s, o1.id)
        scope2 = global_search.scope_for_org(s, o2.id)
        r1 = global_search.search(s, scope1, "quasar")
        assert counts(r1) == {"projects": 1, "documents": 0, "tm": 0, "glossary": 0}, counts(r1)
        r2 = global_search.search(s, scope2, "quasar")
        assert counts(r2) == {"projects": 1, "documents": 1, "tm": 1, "glossary": 1}, counts(r2)
        # An unscoped principal under an active gate fails closed for glossaries.
        blocked = global_search.Scope(project_ids=None, org_id=None, unassigned_glossaries=True)
        prev = settings.get("multi_org_enabled")
        settings.set_many({"multi_org_enabled": "1"})
        try:
            assert global_search.search(s, blocked, "quasar", groups=["glossary"])["total"] == 0
        finally:
            settings.set_many({"multi_org_enabled": prev})
    print(
        "  org ring: org 1 never sees org 2 in any group; a bare scope under the gate fails closed OK"
    )


def _login(c, who):
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": who, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, (who, r.status_code, r.text[:200])
    csrf_for(c)
    return c


def _transport(client):
    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        resp = client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            resp.status_code,
            content=resp.content,
            headers={"content-type": resp.headers.get("content-type", "")},
        )

    return httpx.MockTransport(handler)


def test_surfaces(ids, users):
    admin, translator, bound, manager = users
    settings.set_many({"project_scoping": "1", "security_auth_required": "1"})
    api = TestClient(app)
    key = apikeys.create_key(translator.id, "search", scopes="api docs")[1]
    h = {"X-Loc-Token": key}
    r = api.get("/api/search?q=atlas", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert {g["key"]: g["count"] for g in r.json()["groups"]} == {
        "projects": 1,
        "documents": 1,
        "tm": 1,
        "glossary": 0 + counts(search_as(translator, "atlas"))["glossary"],
        "workflows": 0,
        "connections": 0,
    }, r.text
    hidden = api.get("/api/search?q=zebra", headers=h).json()
    assert [g["count"] for g in hidden["groups"] if g["key"] != "glossary"] == [0] * 5, hidden
    assert "Zebra rollout" not in json.dumps(hidden) and "zebra-manual" not in json.dumps(hidden)
    r = api.get("/api/search?q=atlas&group=tm,documents&limit=1", headers=h).json()
    assert [g["key"] for g in r["groups"]] == ["tm", "documents"] and r["limit"] == 1
    assert api.get("/api/search?q=a", headers=h).status_code == 400
    assert api.get("/api/search?q=atlas&group=nope", headers=h).status_code == 400
    assert api.get("/api/search?q=atlas").status_code in (401, 403)
    # Web: the translator's page lists A, never B; the page says what is searched.
    web = _login(TestClient(app), "tr1@example.test")
    page = web.get("/search?q=atlas")
    assert (
        page.status_code == 200 and "Atlas launch" in page.text and "atlas-guide.docx" in page.text
    )
    assert (
        "Text inside documents is not searched" in page.text
        or "does not search inside documents" in page.text
    )
    assert "Zebra rollout" not in web.get("/search?q=zebra").text
    none = web.get("/search?q=zebra").text
    # (Gate: the old "No matches" check was satisfied by the palette's count label in the page
    # chrome, not by the results; the org's glossary terms stay visible to a translator, as on
    # the Glossaries page, so check the hidden project's own rows instead.)
    assert "zebra-manual" not in none and "Welcome to zebra" not in none, none
    assert "No matches for" in web.get("/search?q=qqzzxx").text
    assert "Type at least 2 characters" in web.get("/search?q=a").text
    assert "Search projects" in web.get("/search").text
    grp = web.get("/search?q=atlas&group=documents")
    assert (
        grp.status_code == 200
        and "atlas-guide.docx" in grp.text
        and 'id="sg-projects"' not in grp.text
    )
    assert web.get("/search?q=atlas&group=nope").status_code == 200
    # Review: a hostile `page` (a superscript digit, a 5000-digit number) is page 1, never a 500.
    for bad in ("%C2%B2", "9" * 5000, "-3", "1.5"):
        pg = web.get(f"/search?q=atlas&group=documents&page={bad}")
        assert pg.status_code == 200 and "atlas-guide.docx" in pg.text, (bad[:8], pg.status_code)
    s = web.get("/search/suggest?q=atlas")
    assert s.status_code == 200 and s.json()["groups"][0]["key"] == "projects", s.text
    assert len(s.json()["groups"][0]["hits"]) <= 3
    assert web.get("/search/suggest?q=zebra").json()["total"] == 0 or all(
        g["count"] == 0
        for g in web.get("/search/suggest?q=zebra").json()["groups"]
        if g["key"] != "glossary"
    )
    assert web.get("/search/suggest?q=a").status_code == 400
    assert TestClient(app).get("/search/suggest?q=atlas", follow_redirects=False).status_code in (
        303,
        401,
        403,
    )
    # CLI in-process (operator, no per-user rules) and remote (the token's rules).
    rc, out, err = run_cli(["--json", "search", "zebra"])
    assert rc == 0 and json.loads(out)["total"] >= 3, (rc, out, err)
    rc, out, err = run_cli(["search", "atlas", "--group", "projects"])
    assert rc == 0 and "Atlas launch" in out, (rc, out, err)
    assert run_cli(["search", "a"])[0] == 2
    _remote._set_transport(_transport(api))
    try:
        base = ["--remote", "http://127.0.0.1", "--token", key]
        rc, out, err = run_cli(["--json", *base, "search", "zebra"])
        body = json.loads(out)
        assert (
            rc == 0 and [g["count"] for g in body["groups"] if g["key"] != "glossary"] == [0] * 5
        ), out
        rc, out, _ = run_cli([*base, "search", "atlas"])
        assert rc == 0 and "Atlas launch" in out and "Zebra rollout" not in out, out
        assert run_cli([*base, "search", "a"])[0] != 0
    finally:
        _remote._set_transport(None)
    print(
        "  surfaces: /api/search, /search, /search/suggest and the CLI (local + remote) agree and hide project B OK"
    )


def test_review_fixes(ids):
    """Review fixes: MCP limit 0 matches the API (clamped, not silently 5); a TM "open list"
    link is only offered for an ACTIVE customer (the TM page browses no archived customer);
    the docs name the MCP tool by its real name."""
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import asyncio

    import server

    seen = {}

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    async def fake_get(client, path, **kw):
        seen.update(kw.get("params") or {})
        return {"ok": True}

    saved = (server._client, server._get_json)
    server._client, server._get_json = lambda: _Ctx(), fake_get
    try:
        asyncio.run(server.search_workspace("atlas", limit=0, offset=0))
    finally:
        server._client, server._get_json = saved
    assert seen["limit"] == 0 and seen["offset"] == 0, seen
    with Session(engine) as s:
        cust = _add(s, Customer(name="Retired Co", archived_at=datetime.utcnow()))
        pr = _add(
            s, Project(name="Retired job", source_locale="en", customer_id=cust.id, created_by="x")
        )
        _add(
            s,
            SegmentTM(
                project_id=pr.id,
                source_locale="en",
                target_locale="de",
                source_text="retiredword here",
                target_text="x",
                source_hash="hret",
            ),
        )
        r = global_search.search(s, global_search.scope_for_org(s, None), "retiredword")
        tm = next(g for g in r["groups"] if g["key"] == "tm")
        assert tm["count"] == 1 and "list_href" not in tm, tm
        live = global_search.search(s, global_search.scope_for_org(s, None), "atlas")
        assert "list_href" in next(g for g in live["groups"] if g["key"] == "tm")
    for rel in ("docs/CLI.md", "app/services/global_search.py"):
        with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
            assert "MCP `search` tool" not in f.read(), rel
    print("  review fixes: bad page, MCP limit 0, archived-customer TM link, tool name OK")


def test_mcp_and_copy():
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import registry

    tool = next(t for t in registry.TOOLS if t.name == "search_workspace")
    assert tool.tier == "read" and tool.routes == (("GET", "/api/search"),)
    assert "inside documents" in tool.summary
    cats = {}
    for loc in ("en", "es", "fr", "de"):
        with open(os.path.join(_ROOT, "app", "i18n", f"{loc}.json"), encoding="utf-8") as f:
            d = json.load(f)
        cats[loc] = d
    keys = set(cats["en"]["page"]["search"])
    for loc, d in cats.items():
        assert set(d["page"]["search"]) == keys, loc
        assert {"palette_pages", "palette_wait", "palette_error", "palette_short"} <= set(
            d["page"]["app_shell"]
        ), loc
    en = cats["en"]
    assert "glossary" in en["chrome"]["search_placeholder"].lower()
    assert "everything" in en["page"]["app_shell"]["palette_search"].lower()
    with open(os.path.join(_ROOT, "app", "templates", "base.html"), encoding="utf-8") as f:
        base = f.read()
    assert (
        'class="topsearch-form"' in base
        and 'action="/search"' in base
        and 'action="/projects" class="topsearch-form"' not in base
    )
    print("  MCP tool + four catalogs + honest placeholder and form target OK")


def test_tm_window_scoped(ids):
    """Gate (#595): the TM window is the newest TM_SCAN_CAP rows of the CALLER'S projects, and
    finding it never sorts the caller's whole TM (that took 5 to 10 s per call on a one-million
    row project) nor scans the table past the window for the count."""
    from sqlalchemy import event

    with Session(engine) as s:
        for i in range(8):
            for pid, tag in ((ids["pa"], "pa"), (ids["pb"], "pb")):
                s.add(
                    SegmentTM(
                        project_id=pid,
                        source_locale="en",
                        target_locale="de",
                        source_text=f"Haystack {tag} {i}",
                        target_text="x",
                        source_hash=f"hay-{tag}-{i}",
                    )
                )
        s.commit()
        scope = global_search.Scope(project_ids=[ids["pa"]])
        seen = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if "segmenttm" in statement.lower():
                seen.append((statement, parameters))

        old = global_search.TM_SCAN_CAP
        global_search.TM_SCAN_CAP = 5
        event.listen(engine, "before_cursor_execute", capture)
        try:
            tm = global_search.search(s, scope, "haystack", groups=["tm"], limit=25)["groups"][0]
        finally:
            event.remove(engine, "before_cursor_execute", capture)
            global_search.TM_SCAN_CAP = old
        titles_ = sorted(h["title"] for h in tm["hits"])
        assert tm["truncated"] is True and tm["count"] == 5, tm
        assert titles_ == [f"Haystack pa {i}" for i in range(3, 8)], titles_
        if engine.dialect.name == "sqlite":
            plans = {}
            with engine.connect() as conn:
                for statement, params in seen:
                    rows = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, params).all()
                    plans[statement] = " | ".join(str(r[-1]) for r in rows)
            # The edge query: the only OFFSET statement with no LIKE (the hits query also pages,
            # but its final rank sort covers at most COUNT_CAP + 1 rows of the window).
            edge = [
                p for st, p in plans.items() if "OFFSET" in st.upper() and "LIKE" not in st.upper()
            ]
            assert edge and all("TEMP B-TREE" not in p for p in edge), edge
            # #608: the count and the page now come from ONE scan (newest COUNT_CAP + 1 matching
            # ids with their rank), so the counting statement is the LIKE scan itself, and it
            # must still be an id range over the window.
            counted = [p for st, p in plans.items() if "LIKE" in st.upper()]
            assert counted and all("rowid>" in p.replace(" ", "") for p in counted), counted
    print("  TM window: newest rows of the caller's own projects, no full sort, id-range count OK")


def main():
    init_db()
    ids = build()
    test_groups(ids)
    test_bounds(ids)
    users = make_users(ids)
    test_rbac(ids, users)
    test_surfaces(ids, users)
    test_org_ring(ids)
    test_review_fixes(ids)
    test_mcp_and_copy()
    test_tm_window_scoped(ids)
    print("GLOBAL SEARCH OK")


if __name__ == "__main__":
    main()
