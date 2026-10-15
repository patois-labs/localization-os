"""Global search: Workflows and Connections groups (#621) -- hermetic + offline.

The acceptance rule: a person never sees, or gets a count for, a workflow or a connection they
cannot open from its own page. The strongest proof is a comparison against the pages
themselves: for every user in a matrix, the ids global search returns for a query that matches
every fixture row must EQUAL the ids whose own page (`/workflows/{id}`,
`/connectors/connection/{id}`) answers 200 for that same user (archived workflows, which the
page still opens but every list leaves out, are the one declared exception).

  1. Single-tenant, sign-in on, project scoping on: admin, a translator who is a member of one
     project, a customer-bound linguist and a manager; global / customer / project-bound rows,
     an archived workflow, a disabled connector kind and a connection bound to a project that
     does not exist.
  2. Counts are taken AFTER the visibility filter: a query that only a hidden row matches has
     count 0 and no hits (so no count can leak a hidden item), and paging works.
  3. Multi-org: org 1 never sees org 2's rows and a row with no org is quarantined, exactly like
     the pages.
  4. Credentials and settings are never searched or shown: a marker planted in a connection's
     secret and config finds nothing and appears in no answer.
  5. The shared helpers (app/services/resource_visibility.py) give the web wrappers' answer.
  6. Four-surface parity: the same six groups on the service, /api/search, /search,
     /search/suggest, the CLI (local + remote), the MCP tool, the Ctrl+K palette, and en/es/fr/de
     catalogs.

Final line: "SEARCH WORKFLOWS CONNECTIONS 621 OK".
"""
# ruff: noqa: E501

import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_search621_")

from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli import _remote
from app.db import engine, init_db
from app.main import app
from app.models import Connection, Customer, Project, Role, Workflow
from app.services import access, apikeys, global_search, resource_visibility, security, settings
from tests._cli_runner import run_cli
from tests._csrf import csrf_for

PASSWORD = "Atlas meadow copper 2026!"  # pregate: allow-secret disposable fixture
TAG = "zzq"  # every fixture name carries it, so one query matches every row
SECRET_MARKER = "sekrit-marker-621"  # planted in a connection's credentials
CONFIG_MARKER = "cfg-marker-621"  # planted in a connection's settings


def _add(session, row):
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _wf(s, name, **kw):
    return _add(s, Workflow(name=f"{TAG} {name}", **kw)).id


def _conn(s, name, **kw):
    kw.setdefault("kind", "airtable")
    kw.setdefault("config", {"base_id": "app-fixture"})
    return _add(s, Connection(name=f"{TAG} {name}", **kw)).id


def build():
    ids = {}
    with Session(engine) as s:
        c1 = _add(s, Customer(name="Northstar Labs"))
        c2 = _add(s, Customer(name="Orchard Foods"))
        pa = _add(
            s, Project(name="Atlas launch", source_locale="en", customer_id=c1.id, created_by="x")
        )
        pb = _add(
            s, Project(name="Zebra rollout", source_locale="en", customer_id=c2.id, created_by="x")
        )
        ids.update(c1=c1.id, c2=c2.id, pa=pa.id, pb=pb.id)
        ids["wf"] = {
            "global": _wf(s, "global"),
            "c1": _wf(s, "customer one", customer_id=c1.id),
            "c2": _wf(s, "customer two", customer_id=c2.id),
            "pa": _wf(s, "project A", customer_id=c1.id, project_id=pa.id),
            "pb": _wf(s, "project B", customer_id=c2.id, project_id=pb.id),
            "pa_global": _wf(s, "project A no customer", project_id=pa.id),
        }
        ids["wf_archived"] = _wf(s, "archived", archived_at=datetime.now(UTC).replace(tzinfo=None))
        ids["conn"] = {
            "global": _conn(s, "global"),
            "c1": _conn(s, "customer one", customer_id=c1.id),
            "c2": _conn(s, "customer two", customer_id=c2.id),
            "pa": _conn(s, "project A", project_id=pa.id),
            "pb": _conn(s, "project B", project_id=pb.id),
            "jira": _conn(s, "jira disabled", kind="jira"),
        }
        # The planted credentials: found by no query, shown by no answer.
        cid = _conn(
            s,
            "creds holder",
            config={"base_id": CONFIG_MARKER},
            secret_enc=SECRET_MARKER,
        )
        ids["conn_creds"] = cid
    return ids


def make_users(ids):
    security.seed_initial()
    admin = security.create_user("adm@example.test", "Admin Two", Role.ADMIN, PASSWORD)
    translator = security.create_user(
        "tr1@example.test", "Translator One", Role.TRANSLATOR, PASSWORD
    )
    bound = security.create_user(
        "bound@example.test", "Bound", Role.LINGUIST, PASSWORD, customer_id=ids["c1"]
    )
    manager = security.create_user("mgr@example.test", "Manager", Role.MANAGER, PASSWORD)
    with Session(engine) as s:
        assert access.add_member(s, ids["pa"], translator.email)[0]
    return {"admin": admin, "translator": translator, "bound": bound, "manager": manager}


def login(email):
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, (email, r.status_code, r.text[:200])
    csrf_for(c)
    return c


def search_as(user, q, **kw):
    with Session(engine) as s:
        return global_search.search(s, global_search.scope_for_user(s, user), q, **kw)


def group(result, key):
    return next(g for g in result["groups"] if g["key"] == key)


def hit_ids(result, key):
    return {h["id"] for h in group(result, key)["hits"]}


def openable(client, template, all_ids):
    """The ids whose own page answers 200 for this client."""
    out = set()
    for i in all_ids:
        r = client.get(template.format(i), follow_redirects=False)
        assert r.status_code in (200, 404), (template, i, r.status_code)
        if r.status_code == 200:
            out.add(i)
    return out


def all_ids_of(model):
    with Session(engine) as s:
        return list(s.exec(select(model.id)).all())


def test_matrix(ids, users):
    """Search == the pages, for every user, in both groups."""
    settings.set_many(
        {
            "security_auth_required": "1",
            "project_scoping": "1",
            "connector_disabled_kinds": "jira",
        }
    )
    wf_ids, conn_ids = all_ids_of(Workflow), all_ids_of(Connection)
    archived = {ids["wf_archived"]}
    expect = {
        # name -> (workflow keys it may open, connection keys it may open)
        "admin": (set(ids["wf"]), {"global", "c1", "c2", "pa", "pb"}),
        "manager": (set(ids["wf"]), {"global", "c1", "c2", "pa", "pb"}),
        "translator": ({"global", "c1", "c2", "pa", "pa_global"}, {"global", "c1", "c2", "pa"}),
        "bound": ({"global", "c1", "pa", "pa_global"}, {"global", "c1", "pa"}),
    }
    for name, user in users.items():
        client = login(user.email)
        res = search_as(user, TAG, limit=25)
        wf_open = openable(client, "/workflows/{}", wf_ids) - archived
        conn_open = openable(client, "/connectors/connection/{}", conn_ids)
        got_wf, got_conn = hit_ids(res, "workflows"), hit_ids(res, "connections")
        assert got_wf == wf_open, (name, "workflows", sorted(got_wf), sorted(wf_open))
        assert got_conn == conn_open, (name, "connections", sorted(got_conn), sorted(conn_open))
        # The count is the visible count (after the filter), not the table size.
        assert group(res, "workflows")["count"] == len(got_wf), name
        assert group(res, "connections")["count"] == len(got_conn), name
        # And the hand-written expectation (so "both empty" can never pass).
        want_wf = {ids["wf"][k] for k in expect[name][0]}
        want_conn = {ids["conn"][k] for k in expect[name][1]} | {ids["conn_creds"]}
        assert got_wf == want_wf, (name, "workflows", sorted(got_wf), sorted(want_wf))
        assert got_conn == want_conn, (name, "connections", sorted(got_conn), sorted(want_conn))
        # The archived workflow and the disabled-kind connection are never listed or counted.
        assert ids["wf_archived"] not in got_wf and ids["conn"]["jira"] not in got_conn
    print(
        "  matrix: workflows and connections equal what each user can open, for admin, manager, member and bound users OK"
    )


def test_counts_after_filter(ids, users):
    tr = users["translator"]
    # A query only hidden rows match: no hits, count 0 (so a count cannot leak a hidden item).
    hidden_only = search_as(tr, f"{TAG} project B", groups=["workflows", "connections"])
    assert (
        group(hidden_only, "workflows")["count"] == 0
        and group(hidden_only, "workflows")["hits"] == []
    )
    assert group(hidden_only, "connections")["count"] == 0
    bound = users["bound"]
    other_customer = search_as(bound, f"{TAG} customer two", groups=["workflows", "connections"])
    assert other_customer["total"] == 0, other_customer
    # Paging: limit 2 over the 5 visible workflows of the translator, no repeats, no drops.
    seen = []
    for offset in (0, 2, 4, 6):
        page = search_as(tr, TAG, groups=["workflows"], limit=2, offset=offset)
        block = group(page, "workflows")
        assert block["count"] == 5, block["count"]
        seen += [h["id"] for h in block["hits"]]
    assert len(seen) == len(set(seen)) == 5, seen
    # A starts-with hit sorts first (rank), and the hit shape is the documented one.
    with Session(engine) as s:
        _add(s, Workflow(name="has needle inside"))
        _add(s, Workflow(name="needle start"))
    res = search_as(users["admin"], "needle", groups=["workflows"])
    titles = [h["title"] for h in group(res, "workflows")["hits"]]
    assert titles == ["needle start", "has needle inside"], titles
    h = group(
        search_as(users["admin"], f"{TAG} global", groups=["workflows", "connections"]), "workflows"
    )["hits"][0]
    assert h["kind"] == "workflow" and h["href"] == f"/workflows/{h['id']}" and h["subtitle"], h
    c = group(search_as(users["admin"], f"{TAG} global", groups=["connections"]), "connections")[
        "hits"
    ][0]
    assert c["kind"] == "connection" and c["href"] == f"/connectors/connection/{c['id']}", c
    assert c["subtitle"] == "Airtable", c
    # The connector kind is searchable too ("airtable" finds the Airtable connections).
    kind = search_as(users["admin"], "airtable", groups=["connections"])
    assert group(kind, "connections")["count"] >= 5, kind
    print(
        "  counts: taken after the visibility filter; hidden-only queries find nothing; paging is exact OK"
    )


def test_secrets_never_surface(ids, users):
    for marker in (SECRET_MARKER, CONFIG_MARKER):
        res = search_as(users["admin"], marker)
        assert res["total"] == 0, (marker, res["total"])
    everything = json.dumps(search_as(users["admin"], TAG, limit=25))
    assert SECRET_MARKER not in everything and CONFIG_MARKER not in everything
    print("  credentials and settings: never searched, never in an answer OK")


def test_org_ring(ids):
    """Multi-org: org 1 never sees org 2; a row with no org is quarantined, like the pages."""
    settings.set_many({"project_scoping": "0", "connector_disabled_kinds": ""})
    with Session(engine) as s:
        o1 = access.create_org(s, "Org One")
        o2 = access.create_org(s, "Org Two")
        oids = (o1.id, o2.id)
    u1 = security.create_user("o1@example.test", "O1", Role.MANAGER, PASSWORD, org_id=oids[0])
    u2 = security.create_user("o2@example.test", "O2", Role.MANAGER, PASSWORD, org_id=oids[1])
    with Session(engine) as s:
        mine = {
            "wf": _wf(s, "org one flow", org_id=oids[0]),
            "conn": _conn(s, "org one conn", org_id=oids[0]),
        }
        theirs = {
            "wf": _wf(s, "org two flow", org_id=oids[1]),
            "conn": _conn(s, "org two conn", org_id=oids[1]),
        }
    settings.set_many({"multi_org_enabled": "1"})
    try:
        wf_ids, conn_ids = all_ids_of(Workflow), all_ids_of(Connection)
        for user, own, foreign in ((u1, mine, theirs), (u2, theirs, mine)):
            client = login(user.email)
            res = search_as(user, TAG, limit=25)
            wf_open = openable(client, "/workflows/{}", wf_ids)
            conn_open = openable(client, "/connectors/connection/{}", conn_ids)
            assert hit_ids(res, "workflows") == wf_open - {ids["wf_archived"]}, user.email
            assert hit_ids(res, "connections") == conn_open, user.email
            assert hit_ids(res, "workflows") == {own["wf"]}, (user.email, hit_ids(res, "workflows"))
            assert hit_ids(res, "connections") == {own["conn"]}, (
                user.email,
                hit_ids(res, "connections"),
            )
            assert foreign["wf"] not in hit_ids(res, "workflows")
            assert foreign["conn"] not in hit_ids(res, "connections")
            assert group(res, "workflows")["count"] == 1 and group(res, "connections")["count"] == 1
        # A bare scope under the gate (a broken principal) fails closed; the authorized
        # operator door (UNSCOPED) sees every org.
        with Session(engine) as s:
            bare = global_search.Scope(project_ids=None, org_id=None)
            assert (
                global_search.search(s, bare, TAG, groups=["workflows", "connections"])["total"]
                == 0
            )
            allowed = global_search.Scope(project_ids=None, org_id=access.UNSCOPED)
            full = global_search.search(s, allowed, TAG, groups=["workflows"], limit=25)
            assert {mine["wf"], theirs["wf"]} <= hit_ids(full, "workflows")
            only1 = global_search.search(
                s, global_search.scope_for_org(s, oids[0]), TAG, groups=["workflows", "connections"]
            )
            assert hit_ids(only1, "workflows") == {mine["wf"]}
            assert hit_ids(only1, "connections") == {mine["conn"]}
    finally:
        settings.set_many({"multi_org_enabled": "0"})
    print(
        "  org ring: org 1 never sees org 2; no-org rows and bare scopes fail closed; pages agree OK"
    )


def test_shared_helpers(ids, users):
    """The web wrappers and the service return the same rows (the move changed nothing)."""
    from app.web import connectors_common, workflows_common

    with Session(engine) as s:
        rows = list(s.exec(select(Workflow)).all())
        conns = list(s.exec(select(Connection)).all())
        for user in users.values():
            via_service = resource_visibility.visible_workflows(s, user, rows)
            assert {w.id for w in via_service} <= {w.id for w in rows}
            conn_service = resource_visibility.visible_connections(s, user, conns)
            assert {c.id for c in conn_service} <= {c.id for c in conns}
    # Both wrappers delegate (no private copy of the rule is left behind).
    for rel, needle in (
        ("workflows_common.py", "resource_visibility.visible_workflows"),
        ("connectors_common.py", "resource_visibility.visible_connections"),
    ):
        with open(os.path.join(_ROOT, "app", "web", rel), encoding="utf-8") as f:
            assert needle in f.read(), rel
    assert callable(workflows_common._visible_workflows)
    assert callable(connectors_common._project_visible_connections)
    # Behavior pin on the service itself: the translator (member of A only) keeps A's
    # workflow and loses B's; the bound user loses the other customer's.
    with Session(engine) as s:
        tr = users["translator"]
        keep = resource_visibility.visible_workflows(
            s, tr, [s.get(Workflow, ids["wf"]["pa"]), s.get(Workflow, ids["wf"]["pb"])]
        )
        assert [w.id for w in keep] == [ids["wf"]["pa"]]
        b = users["bound"]
        keep = resource_visibility.visible_workflows(
            s, b, [s.get(Workflow, ids["wf"]["c1"]), s.get(Workflow, ids["wf"]["c2"])]
        )
        assert [w.id for w in keep] == [ids["wf"]["c1"]]
    print("  shared helpers: web wrappers delegate to app/services/resource_visibility.py OK")


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
    settings.set_many(
        {"security_auth_required": "1", "project_scoping": "1", "connector_disabled_kinds": "jira"}
    )
    tr = users["translator"]
    want_wf = {ids["wf"][k] for k in ("global", "c1", "c2", "pa", "pa_global")}
    api = TestClient(app)
    key = apikeys.create_key(tr.id, "search", scopes="api docs")[1]
    h = {"X-Loc-Token": key}
    r = api.get(f"/api/search?q={TAG}&group=workflows,connections&limit=25", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    body = r.json()
    assert [g["key"] for g in body["groups"]] == ["workflows", "connections"], body
    assert {x["id"] for x in body["groups"][0]["hits"]} >= want_wf
    assert ids["wf"]["pb"] not in {x["id"] for x in body["groups"][0]["hits"]}
    assert ids["conn"]["pb"] not in {x["id"] for x in body["groups"][1]["hits"]}
    full = api.get(f"/api/search?q={TAG}", headers=h).json()
    assert (
        [g["key"] for g in full["groups"]]
        == list(global_search.GROUPS)
        == [
            "projects",
            "documents",
            "tm",
            "glossary",
            "workflows",
            "connections",
        ]
    )
    assert api.get(f"/api/search?q={TAG}&group=nope", headers=h).status_code == 400
    # A hidden-only query over /api: counts are zero, the hidden name is not in the answer.
    hidden = api.get(
        f"/api/search?q={TAG}%20project%20B&group=workflows,connections", headers=h
    ).json()
    assert [g["count"] for g in hidden["groups"]] == [0, 0], hidden
    # Web page: grouped results with the new headings, only what this user can open.
    web = login(tr.email)
    page = web.get(f"/search?q={TAG}")
    assert page.status_code == 200 and f"{TAG} project A" in page.text, page.status_code
    assert (
        f"{TAG} project B" not in page.text
        and "Workflows" in page.text
        and "Connections" in page.text
    )
    one = web.get(f"/search?q={TAG}&group=connections")
    assert one.status_code == 200 and "/connectors/connection/" in one.text
    assert f"{TAG} project B" not in one.text
    sg = web.get(f"/search/suggest?q={TAG}%20project").json()
    assert "workflows" in [g["key"] for g in sg["groups"]]
    assert all(f"{TAG} project B" not in json.dumps(g["hits"]) for g in sg["groups"]), sg
    # CLI: in-process (operator) and remote (the token's rules).
    rc, out, err = run_cli(
        ["--json", "search", TAG, "--group", "workflows", "--group", "connections", "--limit", "25"]
    )
    body = json.loads(out)
    assert rc == 0 and [g["key"] for g in body["groups"]] == ["workflows", "connections"], (
        rc,
        out,
        err,
    )
    assert group(body, "workflows")["count"] == 6 and group(body, "connections")["count"] == 6, body
    rc, out, err = run_cli(["search", TAG, "--group", "workflows", "--limit", "25"])
    assert rc == 0 and f"{TAG} global" in out, (rc, out, err)
    assert run_cli(["search", TAG, "--group", "nope"])[0] == 2
    _remote._set_transport(_transport(api))
    try:
        base = ["--remote", "http://127.0.0.1", "--token", key]
        rc, out, err = run_cli(
            ["--json", *base, "search", TAG, "--group", "workflows", "--limit", "25"]
        )
        body = json.loads(out)
        assert rc == 0 and hit_ids(body, "workflows") >= want_wf, out
        assert ids["wf"]["pb"] not in hit_ids(body, "workflows"), out
        rc, out, _ = run_cli([*base, "search", TAG, "--group", "connections"])
        assert rc == 0 and f"{TAG} project B" not in out, out
    finally:
        _remote._set_transport(None)
    print(
        "  surfaces: /api/search, /search, /search/suggest and the CLI (local + remote) agree and hide B OK"
    )


def test_parity_and_copy():
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import registry

    from app.cli import cmd_search

    groups = list(global_search.GROUPS)
    assert list(cmd_search._GROUPS) == groups
    tool = next(t for t in registry.TOOLS if t.name == "search_workspace")
    assert tool.input_schema["properties"]["groups"]["items"]["enum"] == groups
    assert "workflow" in tool.summary and "connection" in tool.summary
    with open(os.path.join(_ROOT, "app", "static", "command_palette.js"), encoding="utf-8") as f:
        palette = f.read()
    assert (
        re.search(r"var GROUPS = \[(.*?)\];", palette)
        .group(1)
        .replace("'", "")
        .replace(" ", "")
        .split(",")
        == groups
    )
    with open(
        os.path.join(_ROOT, "app", "templates", "_command_palette.html"), encoding="utf-8"
    ) as f:
        tpl = f.read()
    for g in ("workflows", "connections"):
        assert f"data-group-{g}=" in tpl and f"page.search.group_{g}" in tpl
    cats = {}
    for loc in ("en", "es", "fr", "de"):
        with open(os.path.join(_ROOT, "app", "i18n", f"{loc}.json"), encoding="utf-8") as f:
            cats[loc] = json.load(f)
    keys = set(cats["en"]["page"]["search"])
    for loc, d in cats.items():
        assert set(d["page"]["search"]) == keys, loc
        for g in ("group_workflows", "group_connections"):
            assert d["page"]["search"][g].strip(), (loc, g)
        # Every catalog's copy says the new groups are searched (the text differs by language).
        assert (
            d["chrome"]["search_placeholder"] != cats["en"]["chrome"]["search_placeholder"]
            or loc == "en"
        )
    for rel in ("app/templates/help.html", "app/capabilities/llm.md", "docs/CLI.md"):
        with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
            text = f.read()
        assert "connections" in text and "workflows" in text, rel
    assert "workflow" in global_search.SEARCHED and "connection" in global_search.SEARCHED
    assert cats["en"]["page"]["search"]["searched"].startswith(
        "Searches project and customer names"
    )
    assert "workflow names and connection names" in cats["en"]["page"]["search"]["searched"]
    print(
        "  parity: six groups on service, CLI, MCP, palette, help, llms descriptor and four catalogs OK"
    )


def main():
    init_db()
    ids = build()
    users = make_users(ids)
    test_matrix(ids, users)
    test_counts_after_filter(ids, users)
    test_secrets_never_surface(ids, users)
    test_shared_helpers(ids, users)
    test_surfaces(ids, users)
    test_parity_and_copy()
    test_org_ring(ids)
    print("SEARCH WORKFLOWS CONNECTIONS 621 OK")


if __name__ == "__main__":
    main()
