"""Editable page layouts (train L1, SPEC-project-layout-editor-2026-09) -- hermetic + offline.

Covers the storage, validation, owner isolation, every surface and the rendering of the
per-user page layouts:

  1. Validation (app/services/page_layouts.py::validate_rows): a good layout passes; too
     many cards in a row, an unknown card, a duplicate, an oversized body, a non-list,
     an empty row, a non-string card, prototype keys (__proto__ / constructor / toString),
     a wrong version or page and an extra field are all REFUSED with a plain sentence.
  2. Read-side repair (resolve_rows): a stored layout with a now-unregistered card, a
     duplicate and an over-cap row is repaired, and a card it does not mention is
     appended in its own row; corrupt stored JSON degrades to the default.
  3. Web routes (sign-in on): GET/PUT/DELETE /account/layouts/<page> round-trip, CSRF
     required (no token -> 403, cross-origin -> 403), a viewer may save their OWN layout,
     the carve-out is exact (no other path or verb), DELETE resets to the default.
  4. Owner isolation: user A's layout never shows for user B on any route (web, /api,
     CLI), and no route takes the owner from input (an `owner` body field is refused, an
     owner query parameter is ignored, and no route signature has a user parameter).
  5. /api routes: list / get / put / delete with a docs-scoped key; unknown page -> 404.
  6. Sign-in OFF: every surface (web, /api, CLI) writes the single "local" owner.
  7. CLI: in-process (--user) and --remote round trips; missing --user, a bad layout and
     a remote --user are refused (exit 2) and store nothing.
  8. Rendering: the project page embeds the resolved layout as inert JSON with the
     engine script; the markup keeps the default order; a hostile stored key or card
     title cannot inject markup (|tojson escapes it).
  9. Migration a7e3c9d2b5f1: upgrade creates userlayout with its unique constraint,
     downgrade drops it, upgrade again works, on a throwaway SQLite file.
 10. The page_layout.js model under node: normalize, merge, cap 3, empty rows dropped,
     hostile keys, and every keyboard move.
 11. Train L2, every REGISTRY page (Dashboard, Connectors, Setup AI, Users and the other
     hub pages): the #page-layout wrapper holds only data-card cards (a saved layout
     rebuilds it), in the registered default order, with every default pair side by side
     and every card named; one hidden Edit layout button above the cards; the default
     render has the SAME h2 sequence and ids as the template rendered with its layout
     wiring stripped (= the pre-change template); a saved layout is accepted and embedded,
     hostile layouts are refused and store nothing, a hostile stored key is dropped and
     escaped; the CLI list and the help page name every page.

Final line: "PAGE LAYOUTS OK".
"""
# ruff: noqa: E501
# (The node probe script and the expected layouts are verbatim data; wrapping hurts review.)

import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_pagelayouts_")

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli import _remote
from app.db import engine, init_db
from app.main import app
from app.models import Project, Role, TargetLocale, UserLayout
from app.services import apikeys, page_layouts, security, settings
from tests._cli_runner import run_cli
from tests._csrf import csrf_for

_JS = os.path.join(_ROOT, "app", "static", "page_layout.js")
_PROJECT_HTML = os.path.join(_ROOT, "app", "templates", "project.html")
_PARTIAL = os.path.join(_ROOT, "app", "templates", "_page_layout.html")

GOOD = [["overview", "budget"], ["tm", "glossary", "langs"], ["members"]]


def _refused(payload, needle, page="project"):
    try:
        page_layouts.validate_rows(page, payload)
    except page_layouts.LayoutError as exc:
        assert needle.lower() in str(exc).lower(), (payload, str(exc))
        return str(exc)
    raise AssertionError(f"accepted an invalid layout: {payload!r}")


# --------------------------------------------------------------------------- #
# 1-2. validation and repair
# --------------------------------------------------------------------------- #
def test_validation():
    assert page_layouts.validate_rows("project", GOOD) == GOOD
    assert page_layouts.validate_rows("project", {"v": 1, "rows": GOOD}) == GOOD
    assert page_layouts.validate_rows("project", {"rows": [["members"]]}) == [["members"]]
    _refused([["overview", "budget", "upload", "documents"]], "at most 3")
    _refused([["overview"], ["nope"]], "does not have")
    _refused([["overview"], ["overview"]], "more than once")
    _refused([["overview", "overview"]], "more than once")
    _refused({"rows": "overview"}, "list of rows")
    _refused("overview", "list of rows")
    _refused(None, "list of rows")
    _refused({"rows": {"0": ["overview"]}}, "list of rows")
    _refused([], "no rows")
    _refused([[]], "empty")
    _refused(["overview"], "not a list")
    _refused([[1]], "does not have")
    _refused([[["overview"]]], "does not have")
    for hostile in ("__proto__", "constructor", "toString", "hasOwnProperty", "</script>"):
        _refused([[hostile]], "does not have")
    _refused({"v": 2, "rows": GOOD}, "version")
    _refused({"v": True, "rows": GOOD}, "version")
    _refused({"v": "1", "rows": GOOD}, "version")
    _refused({"page": "dashboard", "rows": GOOD}, "different page")
    _refused({"rows": GOOD, "owner": "user:1"}, "unexpected field")
    # ("dashboard" was the unregistered example until train L2 registered it.)
    try:
        page_layouts.validate_rows("nonexistent_page", GOOD)
        raise AssertionError("an unregistered page must be refused")
    except page_layouts.UnknownPage as exc:
        assert "Known pages: " in str(exc) and "project" in str(exc), str(exc)
    # A card of ANOTHER page is not a card of this one.
    _refused([["running"]], "does not have")
    _refused([["overview"]], "does not have", page="dashboard")
    # Oversized: the body bound is checked BEFORE parsing.
    big = json.dumps({"rows": GOOD, "pad": "x" * (page_layouts.MAX_BODY_BYTES + 10)}).encode()
    try:
        page_layouts.parse_body(big)
        raise AssertionError("an oversized body must be refused")
    except page_layouts.LayoutError as exc:
        assert "too large" in str(exc)
    try:
        page_layouts.parse_body(b"{not json")
        raise AssertionError("malformed JSON must be refused")
    except page_layouts.LayoutError as exc:
        assert "not valid JSON" in str(exc)
    # The stored document bound holds for the largest VALID layout (8 single rows).
    flat = [[k] for k in page_layouts.REGISTRY["project"].movable]
    assert len(page_layouts._dump(flat)) < page_layouts.MAX_LAYOUT_BYTES
    print("  validation: good layouts pass; 17 invalid shapes + hostile keys refused OK")


def test_resolve():
    spec = page_layouts.REGISTRY["project"]
    got = page_layouts.resolve_rows(
        "project",
        [["glossary", "retired_card", "tm"], ["tm", "overview"], "junk", [], ["a", "b"]],
    )
    assert got[0] == ["glossary", "tm"], got
    assert got[1] == ["overview"], got
    rest = [r[0] for r in got[2:]]
    assert all(len(r) == 1 for r in got[2:]), got
    assert rest == [k for k in spec.movable if k not in ("glossary", "tm", "overview")], got
    # Over-cap stored rows are split (a later lower cap never breaks a page).
    split = page_layouts.resolve_rows("project", [list(spec.movable)])
    assert [len(r) for r in split] == [3, 3, 2], split
    # Corrupt stored JSON reads as the default order.
    assert page_layouts._load("{bad") is None
    assert page_layouts.resolve_rows("project", None) == [[k] for k in spec.movable]
    print(
        "  resolve: dropped card removed, missing cards appended, cap split, corrupt -> default OK"
    )


# --------------------------------------------------------------------------- #
# helpers for the route / CLI tests
# --------------------------------------------------------------------------- #
def _login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (who, r.status_code)
    csrf_for(c)
    return c


def _rows_in_db(owner):
    with Session(engine) as s:
        row = s.exec(
            select(UserLayout).where(UserLayout.owner == owner, UserLayout.page_key == "project")
        ).first()
        return None if row is None else json.loads(row.layout_json)["rows"]


def _project():
    with Session(engine) as s:
        p = Project(name="Layout project", source_locale="en", created_by="user1")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de"))
        s.commit()
        return p.id


def _mock_transport(client):
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


def _data_cards(html):
    return re.findall(r'<section class="card(?: flat)?" data-card="([a-z_]+)"', html)


def _listed(out, page):
    """The entry for `page` in a `--json layout list` output (every registered page)."""
    rows = [x for x in json.loads(out)["layouts"] if x["page"] == page]
    assert len(rows) == 1, out[:300]
    return rows[0]


def _blob(html):
    m = re.search(
        r'<script nonce="[^"]+" type="application/json" id="page-layout-data">(.*?)</script>',
        html,
        re.S,
    )
    assert m, "the page must embed #page-layout-data"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------- #
# 3-5, 7-8. sign-in ON: web, /api, CLI, rendering
# --------------------------------------------------------------------------- #
def test_auth_on_surfaces():
    assert security.seed_initial() is True  # admin / linguist / user1, login wall ON
    security.create_user("viewer1", "Viewer", Role.VIEWER, "viewer1", enforce_policy=False)
    ua = security.get_by_email("user1")
    ub = security.get_by_email("linguist")
    uv = security.get_by_email("viewer1")
    owner_a, owner_b, owner_v = f"user:{ua.id}", f"user:{ub.id}", f"user:{uv.id}"
    pid = _project()

    a = _login(TestClient(app), "user1", "user1")
    b = _login(TestClient(app), "linguist", "linguist")

    # Default when nothing is saved.
    r = a.get("/account/layouts/project")
    assert r.status_code == 200 and r.json()["saved"] is False, r.text
    assert r.json()["rows"] == page_layouts.default_rows("project")

    # CSRF: the web PUT needs the synchronizer token; a foreign Origin is refused.
    tok = a.headers.pop("X-CSRF-Token")
    r = a.put("/account/layouts/project", json={"rows": GOOD}, follow_redirects=False)
    assert r.status_code in (303, 403), ("a web PUT without a CSRF token must fail", r.status_code)
    assert _rows_in_db(owner_a) is None, "a CSRF-less PUT must store nothing"
    r = a.put(
        "/account/layouts/project",
        json={"rows": GOOD},
        headers={"X-CSRF-Token": "wrong-token-value"},  # pregate: allow-secret fixture
    )
    assert r.status_code == 403, r.status_code
    a.headers["X-CSRF-Token"] = tok
    r = a.put(
        "/account/layouts/project", json={"rows": GOOD}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403, ("cross-origin PUT must be blocked", r.status_code)
    assert _rows_in_db(owner_a) is None
    bad_token = {"X-CSRF-Token": "nope"}  # pregate: allow-secret wrong on purpose
    r = a.delete("/account/layouts/project", headers=bad_token)
    assert r.status_code == 403, r.status_code

    # Round trip with the token.
    r = a.put("/account/layouts/project", json={"v": 1, "rows": GOOD})
    assert r.status_code == 200 and r.json()["saved"] is True, r.text
    assert r.json()["rows"][:3] == GOOD, r.json()
    assert _rows_in_db(owner_a) == GOOD
    # Invalid input: 400 with a sentence, nothing stored over the good layout.
    for bad in ({"rows": [["overview", "budget", "upload", "tm"]]}, {"rows": [["nope"]]}):
        r = a.put("/account/layouts/project", json=bad)
        assert r.status_code == 400 and r.json()["ok"] is False, r.text
        assert r.json()["error"].endswith("."), r.json()
    r = a.put(
        "/account/layouts/project",
        content=b"x" * (page_layouts.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400 and "too large" in r.json()["error"], r.text
    assert _rows_in_db(owner_a) == GOOD, "a refused write must leave the saved layout alone"
    assert a.put("/account/layouts/nonexistent_page", json={"rows": GOOD}).status_code == 404
    print("  web: default read, CSRF token + same-origin required, round trip, 400/404 refusals OK")

    # Owner isolation: B sees the default and B's write never touches A's row.
    assert b.get("/account/layouts/project").json()["saved"] is False
    r = b.put(
        f"/account/layouts/project?owner={owner_a}&user_id={ua.id}", json={"rows": [["members"]]}
    )
    assert r.status_code == 200, r.text
    assert _rows_in_db(owner_b) == [["members"]], "B's write lands on B's own row"
    assert _rows_in_db(owner_a) == GOOD, "an owner query parameter must be ignored"
    r = b.put("/account/layouts/project", json={"rows": [["members"]], "owner": owner_a})
    assert r.status_code == 400, "an owner body field must be refused"
    assert _rows_in_db(owner_a) == GOOD
    assert b.delete(f"/account/layouts/project?owner={owner_a}").status_code == 200
    assert _rows_in_db(owner_a) == GOOD and _rows_in_db(owner_b) is None
    # No route has a parameter that could name another user.
    from app.web import page_layouts as web_layouts

    for fn in (
        web_layouts.web_layout_get,
        web_layouts.web_layout_put,
        web_layouts.web_layout_delete,
        web_layouts.api_layouts_list,
        web_layouts.api_layout_get,
        web_layouts.api_layout_put,
        web_layouts.api_layout_delete,
    ):
        params = set(inspect.signature(fn).parameters) - {"request", "page_key"}
        assert not params, (fn.__name__, params)
    print(
        "  isolation: B reads the default, writes only its own row; owner query/body ignored/refused OK"
    )

    # A viewer may save their OWN layout (the exact carve-out) and nothing else.
    v = _login(TestClient(app), "viewer1", "viewer1")
    r = v.put("/account/layouts/project", json={"rows": [["langs", "tm"]]})
    assert r.status_code == 200, ("a viewer saves their own layout", r.status_code, r.text[:200])
    assert _rows_in_db(owner_v) == [["langs", "tm"]]
    assert v.delete("/account/layouts/project").status_code == 200
    assert _rows_in_db(owner_v) is None
    r = v.post("/account/layouts/project", json={"rows": [["langs"]]})
    assert r.status_code in (403, 405), ("POST is not in the carve-out", r.status_code)
    from app import main as app_main

    # Mutation proof: without the carve-out the viewer's PUT is refused by the rank gate.
    real = app_main._self_service_writable
    app_main._self_service_writable = lambda method, path: False
    try:
        r = v.put("/account/layouts/project", json={"rows": [["langs"]]})
        assert r.status_code == 403, ("the carve-out is what admits the viewer", r.status_code)
    finally:
        app_main._self_service_writable = real
    assert _rows_in_db(owner_v) is None
    assert app_main._self_service_writable("PUT", "/account/layouts/project")
    assert app_main._self_service_writable("DELETE", "/account/layouts/project")
    for method, path in (
        ("POST", "/account/layouts/project"),
        ("PATCH", "/account/layouts/project"),
        ("PUT", "/account/layouts/project/x"),
        ("PUT", "/account/layouts/"),
        ("PUT", "/account/layoutsx/project"),
        ("PUT", "/projects/1"),
        ("DELETE", "/account/layouts/PROJECT"),
        # Gate L1 LOW-2: `$` let a trailing newline through; fullmatch does not.
        ("PUT", "/account/layouts/project\n"),
        ("DELETE", "/account/layouts/project\n"),
        ("PUT", "/account/layouts/project\r\n"),
    ):
        assert not app_main._self_service_writable(method, path), (method, path)
    print("  viewer: PUT/DELETE of their own layout pass the rank gate; the carve-out is exact OK")

    # DELETE resets A.
    r = a.delete("/account/layouts/project")
    assert r.status_code == 200 and r.json()["reset"] is True and r.json()["saved"] is False
    assert r.json()["rows"] == page_layouts.default_rows("project")
    assert _rows_in_db(owner_a) is None
    assert a.delete("/account/layouts/project").json()["reset"] is False
    print("  web: DELETE resets to the default (reset flag true once, then false) OK")

    # --- /api (token) --------------------------------------------------------
    _ra, key_a = apikeys.create_key(ua.id, "layout-a", scopes="api docs")
    _rb, key_b = apikeys.create_key(ub.id, "layout-b", scopes="api docs")
    _rn, key_n = apikeys.create_key(ua.id, "layout-nodocs", scopes="api")
    api = TestClient(app)
    ha, hb = {"X-Loc-Token": key_a}, {"X-Loc-Token": key_b}
    assert api.get("/api/account/layouts").status_code == 401
    assert api.get("/api/account/layouts", headers={"X-Loc-Token": key_n}).status_code == 403
    r = api.put("/api/account/layouts/project", json={"rows": GOOD}, headers=ha)
    assert r.status_code == 200 and r.json()["saved"] is True, r.text
    assert _rows_in_db(owner_a) == GOOD
    lst = api.get("/api/account/layouts", headers=ha).json()["layouts"]
    # Train L2: the list names every registered page (sorted); only project is saved.
    assert [x["page"] for x in lst] == sorted(page_layouts.REGISTRY), lst
    assert [x["page"] for x in lst if x["saved"]] == ["project"], lst
    assert api.get("/api/account/layouts/project", headers=hb).json()["saved"] is False
    assert api.get("/api/account/layouts/nope", headers=ha).status_code == 404
    assert api.put("/api/account/layouts/project", json=[["nope"]], headers=ha).status_code == 400
    assert api.delete("/api/account/layouts/project", headers=hb).json()["reset"] is False
    assert _rows_in_db(owner_a) == GOOD, "B's DELETE never touches A's layout"
    # The web session sees the layout the token wrote (same owner, same row).
    assert a.get("/account/layouts/project").json()["rows"][:3] == GOOD
    print("  /api: 401 no key, 403 no docs scope, token-owned round trip, 400/404, isolation OK")

    # --- rendering ------------------------------------------------------------
    page = a.get(f"/projects/{pid}", follow_redirects=False)
    assert page.status_code == 200, (page.status_code, page.headers.get("location"))
    html = page.text
    blob = _blob(html)
    assert blob["saved"] is True and blob["rows"][:3] == GOOD, blob
    assert blob["url"] == "/account/layouts/project"
    assert '<script src="/static/page_layout.js' in html
    assert html.index('id="page-layout-data"') < html.index("/static/page_layout.js")
    assert html.index('id="page-layout"') < html.index('id="page-layout-data"')
    assert re.search(r'<p id="layoutAnnounce"[^>]*role="status"[^>]*aria-live="polite"', html)
    assert re.search(r'<p id="layoutAlert"[^>]*role="alert"', html)
    # The server markup keeps the DEFAULT order (the saved layout is applied by JS).
    cards = _data_cards(html)
    default_flat = [k for row in page_layouts.default_rows("project") for k in row]
    assert cards == [k for k in default_flat if k in cards], cards
    assert "data-layout-edit hidden" in html, "the Edit button is hidden until JS shows it"
    # B renders the default.
    assert _blob(b.get(f"/projects/{pid}").text)["saved"] is False
    # A hostile stored key cannot inject markup (dropped on read; |tojson escapes).
    with Session(engine) as s:
        row = s.exec(select(UserLayout).where(UserLayout.owner == owner_a)).first()
        row.layout_json = json.dumps({"v": 1, "rows": [["</script><b id=pwn>", "tm"]]})
        s.add(row)
        s.commit()
    html = a.get(f"/projects/{pid}").text
    assert "<b id=pwn>" not in html and _blob(html)["rows"][0] == ["tm"], _blob(html)
    # The blob names no card titles (a card not rendered here never leaks its title),
    # and whatever it carries is escaped by |tojson: a hostile value stays inert.
    assert "cards" not in _blob(html), _blob(html)
    real_blob = page_layouts.render_blob

    def evil_blob(owner, page_key):
        out = real_blob(owner, page_key)
        out["url"] = "</script><img src=x onerror=alert(1)>"
        return out

    page_layouts.render_blob = evil_blob
    try:
        html = a.get(f"/projects/{pid}").text
    finally:
        page_layouts.render_blob = real_blob
    blob_src = html.split('id="page-layout-data">', 1)[1].split("</script>", 1)[0]
    assert "<img" not in blob_src and "u003c/script" in blob_src, blob_src[:300]
    assert _blob(html)["url"].startswith("</script>")
    print(
        "  render: blob + engine script after the movable area, default markup order, hostile key/title inert OK"
    )

    # --- CLI in-process -------------------------------------------------------
    rows_arg = json.dumps([["glossary", "tm"], ["overview"]])
    rc, out, err = run_cli(["layout", "set", "project", "--rows", rows_arg, "--user", "linguist"])
    assert rc == 0, (rc, out, err)
    assert _rows_in_db(owner_b) == [["glossary", "tm"], ["overview"]]
    rc, out, _ = run_cli(["--json", "layout", "show", "project", "--user", str(ub.id)])
    assert rc == 0 and json.loads(out)["saved"] is True, out
    rc, out, _ = run_cli(["layout", "show", "project", "--user", "linguist"])
    assert rc == 0 and "row 1: Glossary [glossary], Translation memory [tm]" in out, out
    rc, out, _ = run_cli(["--json", "layout", "list", "--user", "linguist"])
    assert rc == 0 and _listed(out, "project")["saved"] is True, out
    rc, _, err = run_cli(["layout", "show", "project"])
    assert rc == 2 and "--user" in err, (rc, err)
    for bad in ('[["tm","tm"]]', '[["x"]]', "not json", '[["a","b","c","d"]]'):
        rc, _, err = run_cli(["layout", "set", "project", "--rows", bad, "--user", "linguist"])
        assert rc == 2, (bad, rc, err)
    rc, _, err = run_cli(["layout", "show", "nope", "--user", "linguist"])
    assert rc == 2 and "Known pages" in err, err
    assert _rows_in_db(owner_b) == [["glossary", "tm"], ["overview"]]
    rc, out, _ = run_cli(["layout", "reset", "project", "--user", "linguist"])
    assert rc == 0 and _rows_in_db(owner_b) is None, out
    print(
        "  CLI (in-process): set/show/list/reset by email-or-id; missing --user + bad rows refused OK"
    )

    # --- CLI --remote (the token IS the account) --------------------------------
    _remote._set_transport(_mock_transport(api))
    try:
        base = ["--remote", "http://127.0.0.1", "--token", key_b]
        rc, out, err = run_cli([*base, "layout", "set", "project", "--rows", rows_arg])
        assert rc == 0, (rc, out, err)
        assert _rows_in_db(owner_b) == [["glossary", "tm"], ["overview"]]
        rc, out, _ = run_cli(["--json", *base, "layout", "show", "project"])
        assert rc == 0 and json.loads(out)["rows"][0] == ["glossary", "tm"], out
        rc, out, _ = run_cli(["--json", *base, "layout", "list"])
        assert rc == 0 and _listed(out, "project")["saved"] is True, out
        rc, _, err = run_cli([*base, "layout", "show", "project", "--user", "user1"])
        assert rc == 2 and "in-process only" in err, (rc, err)
        rc, _, err = run_cli([*base, "layout", "set", "project", "--rows", '[["x"]]'])
        assert rc != 0 and _rows_in_db(owner_b) == [["glossary", "tm"], ["overview"]], (rc, err)
        rc, out, _ = run_cli([*base, "layout", "reset", "project"])
        assert rc == 0 and _rows_in_db(owner_b) is None, out
        assert _rows_in_db(owner_a) is not None, "B's remote reset never touches A"
    finally:
        _remote._set_transport(None)
    print("  CLI (--remote): token-owned set/show/list/reset; remote --user + bad rows refused OK")


# --------------------------------------------------------------------------- #
# 6. sign-in OFF: one shared "local" owner on every surface
# --------------------------------------------------------------------------- #
def test_auth_off_local_owner():
    settings.set_many({"security_auth_required": "0"})
    try:
        c = TestClient(app)
        r = c.put("/account/layouts/project", json={"rows": [["tm", "glossary"]]})
        assert r.status_code == 200, (r.status_code, r.text[:200])
        with Session(engine) as s:
            row = s.exec(select(UserLayout).where(UserLayout.owner == "local")).first()
            assert row is not None and row.user_id is None, row
        assert _rows_in_db("local") == [["tm", "glossary"]]
        # /api with any key edits the same local layout (sign-in off: one layout).
        ua = security.get_by_email("user1")
        _r, key = apikeys.create_key(ua.id, "layout-off", scopes="api docs")
        g = c.get("/api/account/layouts/project", headers={"X-Loc-Token": key}).json()
        assert g["saved"] is True and g["rows"][0] == ["tm", "glossary"], g
        # CLI in-process: no --user needed; --user refused (no page would read it).
        rc, out, _ = run_cli(["--json", "layout", "show", "project"])
        assert rc == 0 and json.loads(out)["rows"][0] == ["tm", "glossary"], out
        rc, _, err = run_cli(["layout", "reset", "project", "--user", "user1"])
        assert rc == 2 and "sign-in is off" in err, err
        rc, _, _ = run_cli(["layout", "reset", "project"])
        assert rc == 0 and _rows_in_db("local") is None
    finally:
        settings.set_many({"security_auth_required": "1"})
    print("  sign-in off: web, /api and CLI share the single 'local' owner (user_id NULL) OK")


# --------------------------------------------------------------------------- #
# 8b. template structure
# --------------------------------------------------------------------------- #
def test_template_structure():
    with open(_PROJECT_HTML, encoding="utf-8") as fh:
        src = fh.read().replace("\r\n", "\n")
    spec = page_layouts.REGISTRY["project"]
    keys = re.findall(r'data-card="([a-z_]+)"', src)
    assert keys == [k for row in spec.default_rows for k in row], keys
    assert set(keys) == set(spec.movable)
    for pinned in ("project-header", "banner-warn", "download-hero"):
        opening = re.search(r'<section class="card[^"]*' + pinned + r"[^>]*>", src)
        assert opening and "data-card" not in opening.group(0), pinned
    wrap = src.index('<div id="page-layout" class="page-layout" data-layout-page="project">')
    assert src.index('class="card download-hero"') < wrap < src.index('data-card="overview"')
    assert re.search(
        r"\{% with layout_page = 'project' %\}\{% include \"_page_layout.html\" %\}", src
    )
    with open(_PARTIAL, encoding="utf-8") as fh:
        partial = fh.read()
    assert (
        '<script nonce="{{ csp_nonce() }}" type="application/json" id="page-layout-data">'
        "{{ page_layout(request, layout_page)|tojson }}</script>" in partial
    )
    assert re.search(r'<script src="/static/page_layout\.js\?v=[^"]*"></script>', partial)
    assert " defer" not in partial and " async" not in partial, "the engine must run synchronously"
    # The layout CSS block must survive merges intact: its rules must sit OUTSIDE comments
    # (a broken merge once left a conflict marker and split the block, so the Edit button
    # lost its positioned parent and slid under the sticky topbar).
    with open(os.path.join(_ROOT, "app", "static", "style.css"), encoding="utf-8") as fh:
        css = fh.read().replace("\r\n", "\n")
    assert not re.search(r"^(<<<<<<<|=======|>>>>>>>)( |$)", css, re.M), "conflict marker"
    live = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "*/" not in live, "a CSS comment closes early"
    for rule in (
        # Train L2 generalized the L1 .project-header rule to every page header.
        ".has-layout-edit { position: relative; }",
        ".layout-edit-btn[hidden] { display: none; }",
        '.layout-row[data-cols="3"] { grid-template-columns: repeat(3, minmax(0, 1fr)); }',
    ):
        assert rule in live, f"layout CSS rule missing or commented out: {rule}"
    # #564 follow-up: the Edit layout control is a compact, quiet secondary button (small type,
    # tight padding, muted), never a prominent primary one, and keeps the 24x24 px minimum
    # target (WCAG 2.2 SC 2.5.8). Pinned on the base rule and on the Users and Settings markup.
    # The rule must be `button.layout-edit-btn`: `button.ghost` (0,1,1) would beat a bare
    # `.layout-edit-btn` (0,1,0) and silently keep the full-strength ghost colors.
    m = re.search(r"^button\.layout-edit-btn \{([^}]*)\}", live, re.M)
    assert m, "base .layout-edit-btn rule missing"
    decl = m.group(1)
    fs = re.search(r"font-size:\s*([\d.]+)rem", decl)
    assert fs and float(fs.group(1)) <= 0.8, f"Edit layout must be compact (font-size): {decl}"
    pad = re.search(r"padding:\s*(\d+)px\s+(\d+)px", decl)
    assert pad and int(pad.group(1)) <= 4 and int(pad.group(2)) <= 10, (
        f"padding not compact: {decl}"
    )
    assert "min-height: 24px" in decl and "min-width: 24px" in decl, "24x24 minimum target lost"
    assert "var(--primary)" not in decl and "background: transparent" in decl, decl
    for tpl in ("users.html", "settings.html"):
        with open(os.path.join(_ROOT, "app", "templates", tpl), encoding="utf-8") as fh:
            btn = re.search(r"<button[^>]*data-layout-edit[^>]*>", fh.read())
        assert btn, f"{tpl}: Edit layout button missing"
        assert 'class="ghost layout-edit-btn' in btn.group(0), f"{tpl}: must be a ghost button"
        assert "primary" not in btn.group(0), f"{tpl}: must not be a primary button"
    print(
        "  template: data-card keys == registry default order, pinned cards untouched, partial wired OK"
    )


# --------------------------------------------------------------------------- #
# 9. migration on a throwaway DB
# --------------------------------------------------------------------------- #
def test_migration():
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect

    from alembic import command

    tmp = tempfile.mkdtemp(prefix="loctms_pagelayouts_mig_")
    try:
        cfg = Config(os.path.join(_ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
        url = "sqlite:///" + os.path.join(tmp, "loc_tms.db").replace("\\", "/")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        insp = sa_inspect(eng)
        assert "userlayout" in insp.get_table_names()
        cols = {c["name"]: c["nullable"] for c in insp.get_columns("userlayout")}
        assert cols == {
            "id": False,
            "owner": False,
            "user_id": True,
            "page_key": False,
            "layout_json": False,
            "updated_at": False,
        }, cols
        uqs = {u["name"]: u["column_names"] for u in insp.get_unique_constraints("userlayout")}
        assert uqs.get("ux_userlayout_owner_page") == ["owner", "page_key"], uqs
        idx = {i["name"] for i in insp.get_indexes("userlayout")}
        assert {"ix_userlayout_owner", "ix_userlayout_user_id"} <= idx, idx
        eng.dispose()
        # Downgrade to the revision BEFORE a7e3c9d2b5f1 (not "-1": later revisions, such as
        # the password policy's e5a1c8d3f7b2, now sit on top of it).
        command.downgrade(cfg, "d8c2a6f1e4b9")
        eng = create_engine(url)
        assert "userlayout" not in sa_inspect(eng).get_table_names()
        eng.dispose()
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        assert "userlayout" in sa_inspect(eng).get_table_names()
        eng.dispose()
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        assert len(heads) == 1, heads
        chain = {r.revision for r in script.walk_revisions("base", heads[0])}
        assert "a7e3c9d2b5f1" in chain, heads
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(
        "  migration a7e3c9d2b5f1: upgrade -> table + unique + indexes, downgrade drops, re-upgrade, one head OK"
    )


# --------------------------------------------------------------------------- #
# 10. the JS model under node
# --------------------------------------------------------------------------- #
_NODE_TEST = r"""
const M = require(process.argv[1]);
const out = {};
const allowed = ["overview","budget","upload","documents","langs","tm","glossary","members"];
out.norm = M.normalize([["tm","tm","x"],"bad",[],["a","overview","budget","upload","documents"],
  [7,null,{}],["__proto__","constructor","toString","glossary"]], allowed, 3);
out.hostileAllowed = M.normalize([["__proto__","constructor"],["hasOwnProperty"]],
  ["__proto__","constructor","hasOwnProperty"], 3);
out.forRendered = M.forRendered([["glossary","ghost"],["overview"]], ["overview","tm","glossary"], 3);
out.garbage = M.forRendered("nope", ["overview","tm"], 3);
const base = [["overview"],["budget"],["tm","glossary"],["members"]];
out.upAlone = M.moveUp(base, "budget", 3);
out.upShared = M.moveUp(base, "glossary", 3);
out.upTop = M.moveUp(base, "overview", 3);
out.downLast = M.moveDown(base, "members", 3);
out.downShared = M.moveDown(base, "tm", 3);
out.downJoin = M.moveDown(base, "budget", 3);
out.upFull = M.moveUp([["a","b","c"],["d"]], "d", 3);
out.downFull = M.moveDown([["d"],["a","b","c"]], "d", 3);
out.left = M.moveLeft(base, "glossary");
out.leftEdge = M.moveLeft(base, "tm");
out.right = M.moveRight(base, "tm");
out.own = M.ownRow(base, "tm");
out.ownAlone = M.ownRow(base, "overview");
out.w3 = M.setWidth(base, 0, 3, 3);
out.w1 = M.setWidth([["a","b","c"],["d"]], 0, 1, 3);
out.w2 = M.setWidth([["a","b","c"],["d"]], 0, 2, 3);
out.placeFull = M.placeAt([["a","b","c"],["d"]], "d", 0, 1, 3);
out.placeRow = M.placeAt([["a","b"],["d"]], "d", 0, 1, 3);
out.placeSame = M.placeAt([["a","b","c"]], "a", 0, 3, 3);
out.newRow = M.placeNewRow([["a","b"],["c"]], "b", 0);
out.moveRow = M.moveRow([["a"],["b"],["c"]], 2, 0);
out.merge = M.mergeHidden([["overview"],["tm"]], [["overview","budget"],["members"],["tm"]],
  ["overview","tm"], 3);
out.mergeFull = M.mergeHidden([["overview","tm","glossary"]], [["overview","budget"],["tm"],["glossary"]],
  ["overview","tm","glossary"], 3);
out.same = [M.same([["a"]],[["a"]]), M.same([["a"]],[["b"]])];
out.max = M.MAX;
console.log(JSON.stringify(out));
"""


def test_js_model():
    # Gate L1 LOW-4: a missing node must never read as a pass. It FAILS the suite unless
    # the box explicitly opts out (LOC_TMS_NODE_OPTIONAL=1), and even then it prints a
    # loud NOT RUN line instead of an OK.
    node = shutil.which("node")
    if not node:
        if os.environ.get("LOC_TMS_NODE_OPTIONAL", "").strip() == "1":
            print("  NOT RUN: node is not installed; the page_layout.js model test DID NOT RUN")
            return
        raise AssertionError(
            "node is not installed, so the page_layout.js model test cannot run; install "
            "Node.js or set LOC_TMS_NODE_OPTIONAL=1 to record it as not run"
        )
    r = subprocess.run(
        [node, "-e", _NODE_TEST, _JS], capture_output=True, text=True, check=False, timeout=60
    )
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["norm"] == [["tm"], ["overview", "budget", "upload"], ["documents"], ["glossary"]], o[
        "norm"
    ]
    assert o["hostileAllowed"] == [["__proto__", "constructor"], ["hasOwnProperty"]], o[
        "hostileAllowed"
    ]
    assert o["forRendered"] == [["glossary"], ["overview"], ["tm"]], o["forRendered"]
    assert o["garbage"] == [["overview"], ["tm"]], o["garbage"]
    assert o["upAlone"] == [["overview", "budget"], ["tm", "glossary"], ["members"]], o["upAlone"]
    assert o["upShared"] == [["overview"], ["budget"], ["glossary"], ["tm"], ["members"]], o[
        "upShared"
    ]
    assert o["upTop"] == [["overview"], ["budget"], ["tm", "glossary"], ["members"]]
    assert o["downLast"] == [["overview"], ["budget"], ["tm", "glossary"], ["members"]]
    assert o["downShared"] == [["overview"], ["budget"], ["glossary"], ["tm"], ["members"]]
    assert o["downJoin"] == [["overview"], ["budget", "tm", "glossary"], ["members"]], o["downJoin"]
    assert o["upFull"] == [["d"], ["a", "b", "c"]], o["upFull"]
    assert o["downFull"] == [["a", "b", "c"], ["d"]], o["downFull"]
    assert o["left"] == [["overview"], ["budget"], ["glossary", "tm"], ["members"]]
    assert o["leftEdge"] == [["overview"], ["budget"], ["tm", "glossary"], ["members"]]
    assert o["right"] == [["overview"], ["budget"], ["glossary", "tm"], ["members"]]
    assert o["own"] == [["overview"], ["budget"], ["glossary"], ["tm"], ["members"]]
    assert o["ownAlone"] == [["overview"], ["budget"], ["tm", "glossary"], ["members"]]
    assert o["w3"] == [["overview", "budget", "tm"], ["glossary"], ["members"]], o["w3"]
    assert o["w1"] == [["a"], ["b", "c"], ["d"]], o["w1"]
    assert o["w2"] == [["a", "b"], ["c"], ["d"]], o["w2"]
    assert o["placeFull"] == [["a", "b", "c"], ["d"]], o["placeFull"]
    assert o["placeRow"] == [["a", "d", "b"]], o["placeRow"]
    assert o["placeSame"] == [["b", "c", "a"]], o["placeSame"]
    assert o["newRow"] == [["b"], ["a"], ["c"]], o["newRow"]
    assert o["moveRow"] == [["c"], ["a"], ["b"]], o["moveRow"]
    assert o["merge"] == [["overview", "budget"], ["members"], ["tm"]], o["merge"]
    assert o["mergeFull"] == [["overview", "tm", "glossary"], ["budget"]], o["mergeFull"]
    assert o["same"] == [True, False] and o["max"] == 3
    with open(_JS, encoding="utf-8") as fh:
        js = fh.read()
    assert ".innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js
    assert "Object.create(null)" in js
    print("  node: normalize/merge/cap 3/empty rows/hostile keys and every keyboard move OK")


# --------------------------------------------------------------------------- #
# 11. train L2: every registered hub page
# --------------------------------------------------------------------------- #
#: page key -> template. Every REGISTRY page must be here (and vice versa).
_L2_TEMPLATES = {
    "project": "project.html",
    "dashboard": "dashboard.html",
    "connectors": "connectors.html",
    "connectors_catalog": "connectors_catalog.html",
    "connector_type": "connector_type.html",
    "connection": "connection.html",
    "setup_ai": "setup_ai.html",
    "users": "users.html",
    "settings": "settings.html",
    "security": "security.html",
    "account": "account.html",
    "routing": "routing.html",
    "document": "document.html",
    "reports": "reports.html",
    "my_work": "my_work.html",
    "customer": "customer.html",
    "glossary": "glossary_detail.html",
    "style_guide": "style_guide_detail.html",
    "vendor": "vendor_detail.html",
    "workflow_group": "workflow_group.html",
    "building_blocks": "building_blocks.html",
    "queries": "queries.html",
}

_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children: list = []  # _Node or str

    def elements(self):
        return [c for c in self.children if isinstance(c, _Node)]

    def text(self):
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def walk(self):
        for c in self.elements():
            yield c
            yield from c.walk()


def _tree(html):
    """A tolerant element tree of a rendered page (stdlib HTMLParser): void elements
    never nest, and an end tag closes back to its nearest open match."""
    from html.parser import HTMLParser

    root = _Node("#root", (), None)

    class P(HTMLParser):
        cur = root

        def handle_starttag(self, tag, attrs):
            node = _Node(tag, attrs, self.cur)
            self.cur.children.append(node)
            if tag not in _VOID:
                self.cur = node

        def handle_startendtag(self, tag, attrs):
            self.cur.children.append(_Node(tag, attrs, self.cur))

        def handle_endtag(self, tag):
            n = self.cur
            while n is not root and n.tag != tag:
                n = n.parent
            if n is not root:
                self.cur = n.parent

        def handle_data(self, data):
            self.cur.children.append(data)

    P(convert_charrefs=True).feed(html)
    return root


def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def _h2s(root):
    return [_norm(n.text()) for n in root.walk() if n.tag == "h2"]


def _ids(root):
    return [n.attrs["id"] for n in root.walk() if n.attrs.get("id")]


# The L1/L2 wiring, exactly as the templates add it. Removing it from a template gives
# back the template as it was before the layout train touched it (checked once against
# git while building L2), so rendering the stripped source is the pre-change render.
_STRIP = (
    (
        re.compile(
            r'^[ \t]*<div id="page-layout" class="page-layout" data-layout-page="[a-z_]+">\n', re.M
        ),
        "",
    ),
    (
        re.compile(
            r"^[ \t]*</div>\n[ \t]*\{% with layout_page = '[a-z_]+' %\}\{% include \"_page_layout\.html\" %\}\{% endwith %\}\n",
            re.M,
        ),
        "",
    ),
    (re.compile(r"^[ \t]*<button [^\n]*data-layout-edit hidden>[^\n]*</button>\n", re.M), ""),
    # ux/admin-auth-account: the button is passed to m.page_header(secondary=...) via a set block.
    (
        re.compile(
            r"^[ \t]*\{% set layout_action %\}<button [^\n]*data-layout-edit hidden>[^\n]*</button>\{% endset %\}\n",
            re.M,
        ),
        "",
    ),
    (re.compile(r" has-layout-edit"), ""),
    (re.compile(r' data-card="[a-z_]+"'), ""),
    (re.compile(r' data-card-title="[^"]*"'), ""),
    (
        re.compile(r"m\.card\(layout_key='[a-z_]+'(?:, layout_title='[^']*')?(?:, flat=true)?\)"),
        "m.card()",
    ),
    (re.compile(r", layout_key='[a-z_]+'\)"), ")"),
    (re.compile(r", layout_key=none\)"), ")"),
    (re.compile(r'\{% if layout_key %\} data-card="\{\{ layout_key \}\}"\{% endif %\}'), ""),
)


def _strip_wiring(src):
    src = src.replace("\r\n", "\n")
    # Branch 05 captures the same layout container inside shared tabs. Its closing
    # div and engine include are no longer adjacent. Strip the actual balanced
    # wrapper, not whichever div happens to precede the engine include.
    wrapper = re.search(
        r'<div id="page-layout" class="page-layout" data-layout-page="[a-z_]+">', src
    )
    if wrapper:
        depth = 1
        for tag in re.finditer(r"</?div\b[^>]*>", src[wrapper.end() :]):
            depth += -1 if tag.group().startswith("</") else 1
            if depth == 0:
                start, end = wrapper.end() + tag.start(), wrapper.end() + tag.end()
                src = src[:start] + src[end:]
                src = src[: wrapper.start()] + src[wrapper.end() :]
                break
        else:
            raise AssertionError("Unclosed page-layout wrapper")
    src = re.sub(
        r"\{% with layout_page = '[a-z_]+' %\}\{% include \"_page_layout\.html\" %\}\{% endwith %\}",
        "",
        src,
    )
    for pat, rep in _STRIP:
        src = pat.sub(rep, src)
    return src


def _render_stripped(client, url, template):
    """GET `url` with `template` served WITHOUT its layout wiring (the pre-change
    template), through the app's own Jinja environment."""
    from jinja2 import BaseLoader

    from app.web.deps import templates as tpl

    env = tpl.env
    orig = env.loader

    class Stripped(BaseLoader):
        def get_source(self, environment, name):
            src, filename, uptodate = orig.get_source(environment, name)
            if name == template:
                return _strip_wiring(src), None, lambda: False
            return src, filename, uptodate

    env.loader = Stripped()
    if env.cache is not None:
        env.cache.clear()
    try:
        r = client.get(url)
    finally:
        env.loader = orig
        if env.cache is not None:
            env.cache.clear()
    assert r.status_code == 200, (url, r.status_code)
    return r.text


def _seed_l2(ad):
    """One of everything an L2 page shows, created through the models and services the
    routes read (offline; nothing is translated)."""
    from app.models import (
        Connection,
        Customer,
        EvalDataset,
        EvalDatasetItem,
        Glossary,
        MemoryEntry,
        MemorySuggestion,
        Workflow,
    )
    from app.services import style_guides, vendors
    from app.services import workflows as wf_svc
    from app.services.connectors import store as conn_store

    settings.set_many(
        {
            "routing_enabled": "1",
            "reports_enabled": "1",
            "worklist_enabled": "1",
            "queries_enabled": "1",
            "vendors_enabled": "1",
        }
    )
    settings.invalidate_cache()
    ids = {}
    with Session(engine) as s:
        cust = Customer(name="L2 Outdoor")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        ids["cust"] = cust.id
        p = Project(
            name="L2 catalog", source_locale="en-US", customer_id=cust.id, created_by="admin"
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        ids["pid"] = p.id
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        conn = Connection(
            kind="airtable", name="L2 base", config={}, secret_enc=conn_store.enc_secret({})
        )
        s.add(conn)
        g = Glossary(name="L2 terms", customer_id=cust.id)
        s.add(g)
        ds = EvalDataset(name="L2 set", target_locale="fr-FR", item_count=1)
        s.add(ds)
        mem = MemoryEntry(customer_id=cust.id, body="Use concise product copy.", status="active")
        s.add(mem)
        wf = Workflow(name="L2 flow")
        s.add(wf)
        s.commit()
        for obj in (conn, g, ds, mem, wf):
            s.refresh(obj)
        s.add(EvalDatasetItem(dataset_id=ds.id, source_text="Hello"))
        sug = MemorySuggestion(kind="tune_prompt", customer_id=cust.id, workflow_id=wf.id)
        s.add(sug)
        s.commit()
        s.refresh(sug)
        ids.update(conn=conn.id, gid=g.id, ds=ds.id, mem=mem.id, sug=sug.id)
        ids["vendor"] = vendors.create(s, "L2 Vendor").id
        ids["sg"] = style_guides.create(s, "L2 voice", customer_id=cust.id).id
        ids["grp"] = wf_svc.create_group(s, "L2 group").id
    r = ad.post(
        f"/projects/{ids['pid']}/documents",
        data={"mode": "segmented"},
        files={"file": ("l2.txt", b"Light tents.\n\nWarm bags.", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    ids["doc"] = int(r.headers["location"].split("?")[0].rstrip("/").split("/")[-1])
    return ids


def _l2_urls(ids):
    return {
        "project": f"/projects/{ids['pid']}",
        "dashboard": "/",
        "connectors": "/connectors",
        "connectors_catalog": "/connectors/catalog",
        "connector_type": "/connectors/airtable",
        "connection": f"/connectors/connection/{ids['conn']}",
        "setup_ai": "/setup-ai",
        "users": "/users",
        "settings": "/settings",
        "security": "/security",
        "account": "/account",
        "routing": "/routing",
        "document": f"/documents/{ids['doc']}",
        "reports": "/reports",
        "my_work": "/my-work",
        "customer": f"/customers/{ids['cust']}",
        "glossary": f"/glossaries/{ids['gid']}",
        "style_guide": f"/style-guides/{ids['sg']}",
        "vendor": f"/vendors/{ids['vendor']}",
        "workflow_group": f"/workflow-groups/{ids['grp']}",
        "building_blocks": "/workflows/building-blocks",
        "queries": "/queries",
    }


def _check_wrapper(key, root):
    """The #page-layout wrapper holds ONLY movable cards (a saved layout rebuilds it and
    drops anything else), each at most one grid level deep; a grid groups exactly one
    default row. Returns the rendered card keys in DOM order."""
    spec = page_layouts.REGISTRY[key]
    wraps = [n for n in root.walk() if n.attrs.get("id") == "page-layout"]
    assert len(wraps) == 1, (key, len(wraps))
    wrap = wraps[0]
    assert wrap.tag == "div" and wrap.attrs.get("data-layout-page") == key, (key, wrap.attrs)
    assert not _norm("".join(c for c in wrap.children if isinstance(c, str))), (
        key,
        "loose text inside the wrapper",
    )
    rendered, groups = [], []
    for child in wrap.elements():
        if child.tag == "section" and child.attrs.get("data-card"):
            rendered.append(child.attrs["data-card"])
            continue
        inner = child.elements()
        assert child.tag == "div" and inner, (
            key,
            "non-card inside the wrapper",
            child.tag,
            child.attrs,
        )
        assert not _norm("".join(c for c in child.children if isinstance(c, str))), key
        for sec in inner:
            assert sec.tag == "section" and sec.attrs.get("data-card"), (key, sec.tag, sec.attrs)
            rendered.append(sec.attrs["data-card"])
        groups.append([sec.attrs["data-card"] for sec in inner])
    # every data-card on the page is inside the wrapper, once
    everywhere = [n.attrs["data-card"] for n in root.walk() if n.attrs.get("data-card")]
    assert sorted(everywhere) == sorted(rendered), (key, everywhere, rendered)
    assert len(set(rendered)) == len(rendered), (key, rendered)
    assert set(rendered) <= set(spec.movable), (key, set(rendered) - set(spec.movable))
    flat = [k for row in spec.default_rows for k in row]
    assert rendered == [k for k in flat if k in rendered], (key, "not the default order", rendered)
    # A grid (.grid2) is one or more consecutive default rows (Reports shows two rows of
    # two in one grid); every default row of 2+ sits inside one grid.
    shown_rows = [[k for k in row if k in rendered] for row in spec.default_rows]
    shown_rows = [r for r in shown_rows if r]
    for grp in groups:
        spans = [
            [k for row in shown_rows[i:j] for k in row]
            for i in range(len(shown_rows))
            for j in range(i + 1, len(shown_rows) + 1)
        ]
        assert grp in spans, (key, "a side-by-side group is not whole default rows", grp)
    for row in spec.default_rows:
        if len(row) > 1 and all(k in rendered for k in row):
            assert any(set(row) <= set(g) for g in groups), (
                key,
                "a default pair is not side by side",
                row,
            )
    # every card can be named: an h2 inside it, or data-card-title
    for sec in (n for n in wrap.walk() if n.attrs.get("data-card")):
        named = any(n.tag == "h2" for n in sec.walk()) or sec.attrs.get("data-card-title")
        assert named, (key, "card without a name", sec.attrs["data-card"])
    return rendered


def test_rollout_pages():
    """Every registered page: wiring, default order == the pre-change render, a saved
    layout accepted and applied, a hostile one refused, the blob escaped."""
    assert set(_L2_TEMPLATES) == set(page_layouts.REGISTRY), set(_L2_TEMPLATES) ^ set(
        page_layouts.REGISTRY
    )
    ad = _login(TestClient(app), "admin", "admin")
    admin = security.get_by_email("admin")
    owner = f"user:{admin.id}"
    ids = _seed_l2(ad)
    urls = _l2_urls(ids)
    assert set(urls) == set(page_layouts.REGISTRY)
    tdir = os.path.join(_ROOT, "app", "templates")
    for key in sorted(page_layouts.REGISTRY):
        url, template = urls[key], _L2_TEMPLATES[key]
        spec = page_layouts.REGISTRY[key]
        r = ad.get(url)
        assert r.status_code == 200, (key, url, r.status_code, r.text[:200])
        html = r.text
        root = _tree(html)
        rendered = _check_wrapper(key, root)
        assert len(rendered) >= 2, (key, "fewer than 2 movable cards render", rendered)
        blob = _blob(html)
        assert blob["page"] == key and blob["saved"] is False, (key, blob)
        assert blob["rows"] == page_layouts.default_rows(key), (key, blob["rows"])
        # One Edit layout button, hidden until the engine shows it, ABOVE the cards.
        assert html.count("data-layout-edit") == 1, key
        assert html.index("data-layout-edit") < html.index('id="page-layout"'), key
        assert html.index('id="page-layout"') < html.index('id="page-layout-data"'), key
        # Pre-change render: the same headings and the same ids, in the same order.
        with open(os.path.join(tdir, template), encoding="utf-8") as fh:
            src = fh.read()
        stripped = _strip_wiring(src)
        assert "data-card" not in stripped and "layout_key" not in stripped, key
        assert "page-layout" not in stripped and "layout-edit" not in stripped, key
        before = _tree(_render_stripped(ad, url, template))
        assert _h2s(before) == _h2s(root), (key, _h2s(before), _h2s(root))
        old_ids = _ids(before)
        added = ["page-layout", "layoutAnnounce", "layoutAlert", "page-layout-data"]
        assert sorted(_ids(root)) == sorted(old_ids + added), (key, set(old_ids) ^ set(_ids(root)))
        assert [i for i in _ids(root) if i not in added] == old_ids, (key, "ids reordered")
        # A saved layout: reversed order, the first three side by side.
        flat = list(reversed(spec.movable))
        custom = [flat[:3]] + [[k] for k in flat[3:]]
        r = ad.put(f"/account/layouts/{key}", json={"v": 1, "rows": custom})
        assert r.status_code == 200 and r.json()["saved"] is True, (
            key,
            r.status_code,
            r.text[:200],
        )
        blob = _blob(ad.get(url).text)
        assert blob["saved"] is True and blob["rows"] == custom, (key, blob)
        # Hostile layouts are refused and store nothing.
        hostile = (
            [["__proto__"]],
            [[flat[0], flat[0]]],
            [["x" * 40]],
            [[*flat[:3], "overview_x"]],
            [["</script><b>"]],
            [[flat[0]], "nope"],
        )
        for bad in hostile:
            r = ad.put(f"/account/layouts/{key}", json={"rows": bad})
            assert r.status_code == 400, (key, bad, r.status_code)
        assert page_layouts.get_layout(owner, key)["rows"] == custom, key
        # A hostile STORED key is dropped on read and can never break out of the blob.
        with Session(engine) as s:
            row = s.exec(
                select(UserLayout).where(UserLayout.owner == owner, UserLayout.page_key == key)
            ).first()
            row.layout_json = json.dumps({"v": 1, "rows": [["</script><b id=pwn>", flat[0]]]})
            s.add(row)
            s.commit()
        html = ad.get(url).text
        assert "<b id=pwn>" not in html, key
        assert _blob(html)["rows"][0] == [flat[0]], (key, _blob(html)["rows"][:2])
        r = ad.delete(f"/account/layouts/{key}")
        assert r.status_code == 200, (key, r.status_code)
        assert _blob(ad.get(url).text)["saved"] is False, key
        print(
            f"  {key}: {len(rendered)} cards, default order == pre-change render, save/refuse/escape OK"
        )
    # The CLI list reads the registry: every page, text and JSON.
    rc, out, _ = run_cli(["--json", "layout", "list", "--user", "admin"])
    assert rc == 0 and [x["page"] for x in json.loads(out)["layouts"]] == sorted(
        page_layouts.REGISTRY
    ), out[:300]
    rc, out, _ = run_cli(["layout", "list", "--user", "admin"])
    assert rc == 0 and all(k in out for k in page_layouts.REGISTRY), out
    # Help names every page that can be arranged (by its registry title).
    with open(os.path.join(tdir, "help.html"), encoding="utf-8") as fh:
        help_src = _norm(fh.read())
    listed = help_src.split("Pages you can arrange:", 1)[1].split("</p>", 1)[0]
    missing = [s.title for s in page_layouts.REGISTRY.values() if s.title not in listed]
    assert not missing, ("help does not list", missing)
    print(
        f"  L2: {len(page_layouts.REGISTRY)} registered pages wired and checked; CLI list + help name them all"
    )


_DEEP_BODIES = (
    ("8000 levels of [", ("[" * 8000 + "]" * 8000).encode()),
    ('{"rows": 3000 levels', ('{"rows": ' + "[" * 3000 + "]" * 3000 + "}").encode()),
)


def test_deep_nesting():
    """Gate L1 MEDIUM: a deeply nested body under the 16 KB cap used to raise
    RecursionError inside json.loads (HTTP 500). It is refused with a plain 400 on the
    service seam, the web route and the /api route, and nothing is stored."""
    for label, body in _DEEP_BODIES:
        assert len(body) <= page_layouts.MAX_BODY_BYTES, label
        try:
            page_layouts.parse_body(body)
            raise AssertionError(f"{label}: accepted")
        except page_layouts.LayoutError as exc:
            assert "nested too deeply" in str(exc), (label, str(exc))
    # The depth counter ignores brackets inside strings and honours escapes.
    assert page_layouts._nesting_depth('{"rows": [["[[[[[[[[[[\\"[[[["]]}') == 3
    assert page_layouts.parse_body(b'{"rows": [["tm"]]}') == {"rows": [["tm"]]}
    # A hostile shallow body that still makes the decoder raise is a 400, never a 500.
    real_loads = page_layouts.json.loads

    def boom(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded")

    page_layouts.json.loads = boom
    try:
        try:
            page_layouts.parse_body(b'{"rows": [["tm"]]}')
            raise AssertionError("a decoder RecursionError must become LayoutError")
        except page_layouts.LayoutError as exc:
            assert "not valid JSON" in str(exc), str(exc)
    finally:
        page_layouts.json.loads = real_loads

    settings.set_many({"security_auth_required": "1"})
    web = _login(TestClient(app, raise_server_exceptions=False), "user1", "user1")
    ua = security.get_by_email("user1")
    _r, key = apikeys.create_key(ua.id, "layout-deep", scopes="api docs")
    api = TestClient(app, raise_server_exceptions=False)
    before = _rows_in_db(f"user:{ua.id}")
    for label, body in _DEEP_BODIES:
        for surface, client, url, hdr in (
            ("web", web, "/account/layouts/project", {}),
            ("api", api, "/api/account/layouts/project", {"X-Loc-Token": key}),
        ):
            r = client.put(url, content=body, headers={"Content-Type": "application/json", **hdr})
            assert r.status_code == 400, (surface, label, r.status_code, r.text[:200])
            assert "nested too deeply" in r.json()["error"], (surface, label, r.text[:200])
    assert _rows_in_db(f"user:{ua.id}") == before, "a refused deep body must store nothing"
    print(
        "  deep nesting: both hostile shapes -> 400 on the service, web and /api; decoder errors -> 400 OK"
    )


def test_gdpr_and_race():
    """Gate L1 LOW-3: GDPR export carries the subject's saved layouts and erase deletes
    them. Gate L1 LOW-7: two concurrent first saves by one owner never 500 (the loser's
    unique-constraint IntegrityError is retried as an update)."""
    from app.services import gdpr

    settings.set_many({"security_auth_required": "1"})
    security.create_user("gone1", "Gone", Role.TRANSLATOR, "gone1-pass", enforce_policy=False)
    gone = security.get_by_email("gone1")
    owner = f"user:{gone.id}"
    page_layouts.save_layout(owner, gone.id, "project", {"rows": [["tm", "glossary"]]})
    sections: dict = {}
    for section, rec in gdpr.export_user(gone.id):
        sections.setdefault(section, []).append(rec)
    got = sections.get("page_layouts") or []
    assert len(got) == 1 and got[0]["page_key"] == "project", sections.keys()
    assert got[0]["rows"] == [["tm", "glossary"]], got
    ok, msg = gdpr.erase_user(gone.id, actor="admin")
    assert ok, msg
    assert _rows_in_db(owner) is None, "erase must delete the subject's saved layouts"
    with Session(engine) as s:
        left = s.exec(select(UserLayout).where(UserLayout.user_id == gone.id)).all()
    assert not left, left
    assert "page_layouts_removed" in msg, msg
    print("  GDPR: export carries page_layouts; erase deletes the subject's layout rows OK")

    # Race: two sessions of one user make their FIRST save at the same moment. Both
    # read "no row"; the first INSERT commits; the second hits ux_userlayout_owner_page.
    security.create_user("race1", "Race", Role.TRANSLATOR, "race1-pass", enforce_policy=False)
    ur = security.get_by_email("race1")
    owner_r = f"user:{ur.id}"
    tab1 = _login(TestClient(app, raise_server_exceptions=False), "race1", "race1-pass")
    tab2 = _login(TestClient(app, raise_server_exceptions=False), "race1", "race1-pass")
    real_row = page_layouts._row
    state = {"n": 0}

    def racing_row(s, owner_arg, page_key):
        # Tab 2's first lookup runs before tab 1 has committed (it sees no row); tab 1
        # then commits inside that window, so tab 2's INSERT collides.
        state["n"] += 1
        if state["n"] == 1:
            r1 = tab1.put("/account/layouts/project", json={"rows": [["members"]]})
            assert r1.status_code == 200, r1.text
            return None
        return real_row(s, owner_arg, page_key)

    page_layouts._row = racing_row
    try:
        r2 = tab2.put("/account/layouts/project", json={"rows": [["langs", "tm"]]})
    finally:
        page_layouts._row = real_row
    assert state["n"] >= 2, "the retry must look the row up again"
    assert r2.status_code == 200, ("a racing first save must upsert, never 500", r2.status_code)
    assert _rows_in_db(owner_r) == [["langs", "tm"]], "the later save wins as an update"
    with Session(engine) as s:
        n = len(s.exec(select(UserLayout).where(UserLayout.owner == owner_r)).all())
    assert n == 1, n
    print("  race: concurrent first saves from two sessions -> 200, one row, last write wins OK")


def main():
    init_db()
    test_validation()
    test_resolve()
    test_template_structure()
    test_js_model()
    test_migration()
    test_auth_on_surfaces()
    test_auth_off_local_owner()
    test_deep_nesting()
    test_gdpr_and_race()
    test_rollout_pages()
    print("PAGE LAYOUTS OK")


if __name__ == "__main__":
    main()
