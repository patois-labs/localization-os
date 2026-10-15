"""Connected projects (T1: create, setup menu, Source card) verification (offline).

A connected project is a third kind of project next to Documents and Keys: its content comes
from a connected system, it is created FIRST (empty), and its page offers a setup menu that picks
a connection. Proves:

- the stored marker `Project.intake` (default upload) and creating one on every surface: the
  service, the web dialog's POST, POST /api/projects, `project create --connected` (in-process and
  the --remote request body) and the MCP `create_project(intake=...)`
- the intake switch rule (only while no documents, no keys, no binding) on the service, the API
  route, the CLI verb and the MCP configure_project action
- the upload routes refuse a connected project (web, /api, CLI, images) so the hidden page controls
  cannot be bypassed
- the Projects list: the Connected filter (web, /api, CLI, MCP) and the source badge ("Not connected
  yet" until bound)
- the project page: the setup menu unbound, the Source card bound, upload hidden, unbinding
  returns to the setup menu and keeps the project connected, `return_to` is an allow-list
- "Connect a new system" returns to the project with the new connection selected
- a template applied to a connected project never binds and never schedules
- a customer-bound user never sees (or binds) another customer's connection (404 semantics)
"""

import argparse
import asyncio
import io
import json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env

init_env("loctms_connected_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    ProjectAutomation,
    ProjectIntake,
    Role,
)
from app.services import (
    apikeys,
    content,
    content_projects,
    security,
    settings,
)
from app.services import (
    connected_projects as cp,
)
from app.services import (
    project_automation as pa,
)
from app.services import (
    templates as tpl,
)
from app.services.connectors import store as cstore

PW = "pw-fixture-7741"


def mk_customer(name):
    with Session(engine) as s:
        return content.customer_create(s, name).id


def mk_conn(name, customer_id=None, kind="zendesk"):
    cfg = {"subdomain": "acme", "source_locale": "en-us", "target_locales": "de, fr"}
    return cstore.create(
        kind,
        name,
        cfg,
        {"email": "a@acme.com", "api_token": "tok-fixture"},
        actor="t",
        customer_id=customer_id,
    )


def mk_project(name, intake=None, customer_id=None, targets="de, fr"):
    with Session(engine) as s:
        p = content_projects.project_create(
            s, name, "en-US", targets, customer_id=customer_id, created_by="t@x.co", intake=intake
        )
        return p.id


def add_doc(pid):
    with Session(engine) as s:
        s.add(Document(project_id=pid, filename="a.txt"))
        s.commit()


def row(pid):
    with Session(engine) as s:
        return s.get(Project, pid)


def expect(fn, exc, needle=""):
    try:
        fn()
    except exc as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return e
    raise AssertionError(f"expected {exc.__name__}")


def cli(*argv):
    from app.cli.__main__ import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            code = main(["--json", *argv])
        except SystemExit as e:  # argparse usage errors
            code = e.code
    out = buf.getvalue().strip()
    try:
        return code, (json.loads(out.splitlines()[-1]) if out else {})
    except ValueError:
        return code, {"raw": out}


# ---------------------------------------------------------------------------------------
def test_model_and_service():
    pid = mk_project("CP default")
    assert row(pid).intake == ProjectIntake.UPLOAD == "upload", "every existing project is upload"
    cid = mk_project("CP connected", intake="connected")
    r = row(cid)
    assert r.intake == "connected" and r.project_type == "document", (r.intake, r.project_type)
    # blank and None mean upload
    assert row(mk_project("CP blank", intake="")).intake == "upload"
    expect(lambda: mk_project("CP bad", intake="sideways"), cp.IntakeError, "not available")
    # a connected project is a Documents project: the strings shape is a later bind-time rule
    settings.set_many({"key_projects_enabled": "1"})
    with Session(engine) as s:
        expect(
            lambda: content_projects.project_create(
                s, "CP keys conn", "en-US", "de", project_type="key_based", intake="connected"
            ),
            cp.IntakeError,
            "documents project",
        )
        assert s.exec(select(Project).where(Project.name == "CP keys conn")).first() is None
    # the module kill switch
    settings.set_many({"project_automation_enabled": "0"})
    try:
        expect(lambda: mk_project("CP off", intake="connected"), cp.IntakeError, "switched off")
    finally:
        settings.set_many({"project_automation_enabled": "1"})
    print(
        "  model: intake defaults to upload; connected is a Documents project; bad values refused"
    )


def test_intake_switch():
    pid = mk_project("CP switch")
    with Session(engine) as s:
        p = cp.set_intake(s, s.get(Project, pid), "connected", actor="m@x.co")
        assert p.intake == "connected"
        p = cp.set_intake(s, p, "upload", actor="m@x.co")
        assert p.intake == "upload"
        p = cp.set_intake(s, p, "connected", actor="m@x.co")
    # content refuses the switch, with the reason named
    add_doc(pid)
    with Session(engine) as s:
        e = expect(
            lambda: cp.set_intake(s, s.get(Project, pid), "upload"), cp.IntakeError, "no longer"
        )
        assert "documents" in str(e)
    # a binding refuses it too
    pb = mk_project("CP switch bound", intake="connected")
    conn = mk_conn("ZD switch")
    pa.bind(pb, conn.id, actor="m@x.co", is_admin=True)
    with Session(engine) as s:
        e = expect(
            lambda: cp.set_intake(s, s.get(Project, pb), "upload"), cp.IntakeError, "no longer"
        )
        assert "bound to a source" in str(e)
    # unbinding keeps the project connected (maintainer decision 6)
    pa.unbind(pb, actor="m@x.co")
    assert row(pb).intake == "connected"
    with Session(engine) as s:
        assert pa.get(s, pb) is None
    print(
        "  intake switch: allowed while empty, refused with documents or a binding, unbind keeps it"
    )


def test_return_to_allowlist():
    ok = [
        "/projects/5",
        "/projects/5#source",
        "/projects/5/automation",
        "/projects/12?preview=1#source",
    ]
    for v in ok:
        assert cp.safe_return_to(v) == v, v
    bad = [
        "",
        None,
        "//evil.example/x",
        "https://evil.example/",
        "/\\evil.example",
        "/projects/5\r\nSet-Cookie: a=b",
        "/projects/5/../../etc",
        "javascript:alert(1)",
        "/admin",
        "/projects/x",
        "/projects/5?next=http://evil.example",
        "/" + "a" * 400,
        # Review gate: Unicode digits are not an id, and no newline survives inside the value.
        "/projects/١٢",
        "/projects/5\n#source",
    ]
    for v in bad:
        assert cp.safe_return_to(v) == "", repr(v)
    assert cp.with_params("/projects/5", connection=9) == "/projects/5?connection=9"
    assert (
        cp.with_params("/projects/5?preview=1", connection=9)
        == "/projects/5?preview=1&connection=9"
    )
    print("  return_to: strict in-site allow-list; hostile values dropped")


def test_api_create_list_intake():
    settings.set_many({"security_auth_required": "1", "audit_enabled": "1", "project_scoping": "0"})
    try:
        admin = security.create_user("cp-admin@x.co", "CP Admin", Role.ADMIN, PW)
        mgr = security.create_user("cp-mgr@x.co", "CP Mgr", Role.MANAGER, PW)
        viewer = security.create_user("cp-view@x.co", "CP View", Role.VIEWER, PW)
        ca = mk_customer("CP API A")
        cb = mk_customer("CP API B")
        bound = security.create_user(
            "cp-bound@x.co", "CP Bound", Role.TRANSLATOR, PW, customer_id=ca
        )
        _k, ak = apikeys.create_key(admin.id, "cp admin", scopes="api docs")
        _k, mk = apikeys.create_key(mgr.id, "cp mgr", scopes="api docs")
        _k, vk = apikeys.create_key(viewer.id, "cp view", scopes="api docs")
        _k, bk = apikeys.create_key(bound.id, "cp bound", scopes="api docs")
        A, M, V, B = ({"X-Loc-Token": t} for t in (ak, mk, vk, bk))
        c = TestClient(app)

        r = c.post(
            "/api/projects",
            headers=A,
            json={"name": "CP api conn", "customer_id": ca, "targets": "de", "intake": "connected"},
        )
        assert r.status_code in (200, 201), r.text
        body = r.json()
        assert body["intake"] == "connected" and body["project_type"] == "document", body
        assert body["setup_url"] == f"/projects/{body['id']}#source", body
        conn_pid = body["id"]
        r = c.post("/api/projects", headers=A, json={"name": "CP api up", "customer_id": ca})
        assert r.status_code in (200, 201) and r.json()["intake"] == "upload", r.text
        assert "setup_url" not in r.json()
        up_pid = r.json()["id"]
        r = c.post(
            "/api/projects",
            headers=A,
            json={"name": "CP api bad", "customer_id": ca, "intake": "sideways"},
        )
        assert r.status_code == 400 and "intake" in r.text, r.text
        r = c.post(
            "/api/projects",
            headers=A,
            json={
                "name": "CP api keys",
                "customer_id": ca,
                "intake": "connected",
                "project_type": "key_based",
                "targets": "de",
            },
        )
        assert r.status_code == 400, r.text
        # the detail shows the intake
        r = c.get(f"/api/projects/{conn_pid}", headers=A)
        assert r.status_code == 200 and r.json()["intake"] == "connected", r.text
        # the automation view of an unbound connected project points at the setup page
        r = c.get(f"/api/projects/{conn_pid}/automation", headers=A)
        assert r.status_code == 200, r.text
        assert r.json()["bound"] is False and r.json()["setup_url"].endswith("#source"), r.json()

        # list filter + source label
        r = c.get("/api/projects?intake=connected", headers=A)
        assert r.status_code == 200, r.text
        rows = r.json()["projects"]
        assert conn_pid in {p["id"] for p in rows} and up_pid not in {p["id"] for p in rows}, rows
        assert all(p["intake"] == "connected" for p in rows)
        mine = next(p for p in rows if p["id"] == conn_pid)
        assert mine["source"]["label"] == "Not connected yet" and mine["source"]["bound"] is False
        r = c.get("/api/projects?intake=upload", headers=A)
        assert conn_pid not in {p["id"] for p in r.json()["projects"]}
        assert up_pid in {p["id"] for p in r.json()["projects"]}
        r = c.get("/api/projects?intake=whatever", headers=A)
        assert r.status_code == 400, r.text
        r = c.get("/api/projects", headers=A)
        assert {conn_pid, up_pid} <= {p["id"] for p in r.json()["projects"]}
        assert all(p["intake"] in ("upload", "connected") for p in r.json()["projects"])

        # bind -> the badge becomes the connector kind
        conn = mk_conn("ZD list")
        r = c.post(f"/api/projects/{conn_pid}/source", headers=M, json={"connection_id": conn.id})
        assert r.status_code in (200, 201), r.text
        rows = c.get("/api/projects?intake=connected", headers=A).json()["projects"]
        mine = next(p for p in rows if p["id"] == conn_pid)
        assert mine["source"] == {
            "intake": "connected",
            "kind": "zendesk",
            "label": "Zendesk Guide",
            "bound": True,
        }, mine["source"]

        # the intake route: ring first, then manager; refused with content or a binding
        empty = mk_project("CP api switch", customer_id=ca)
        r = c.post(f"/api/projects/{empty}/intake", headers=V, json={"intake": "connected"})
        assert r.status_code == 403, r.text
        r = c.post(f"/api/projects/{empty}/intake", headers=M, json={"intake": "connected"})
        assert r.status_code == 200 and r.json()["intake"] == "connected", r.text
        assert r.json()["setup_url"].endswith("#source")
        r = c.post(f"/api/projects/{empty}/intake", headers=M, json={"intake": "nope"})
        assert r.status_code == 400, r.text
        r = c.post(f"/api/projects/{conn_pid}/intake", headers=M, json={"intake": "upload"})
        assert r.status_code == 400 and "bound to a source" in r.text, r.text
        # a foreign customer's project answers like a missing one, before the role gate
        other = mk_project("CP api other", customer_id=cb)
        r = c.post(f"/api/projects/{other}/intake", headers=B, json={"intake": "connected"})
        missing = c.post("/api/projects/999999/intake", headers=B, json={"intake": "connected"})
        assert r.status_code == 404 and r.json() == missing.json(), (r.text, missing.text)
        assert row(other).intake == "upload"
        print("  api: create, list filter, source label, intake route (ring, role, refusals)")
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "1"})


def test_upload_routes_refuse():
    settings.set_many({"security_auth_required": "1"})
    try:
        admin = security.create_user("cp-up-admin@x.co", "CP Up", Role.ADMIN, PW)
        _k, ak = apikeys.create_key(admin.id, "cp up", scopes="api docs")
        A = {"X-Loc-Token": ak}
        pid = mk_project("CP uploads", intake="connected")
        up = mk_project("CP uploads ok")
        c = TestClient(app)
        files = {"file": ("note.txt", b"Hello world. Second sentence.", "text/plain")}
        r = c.post("/api/documents", headers=A, data={"project_id": str(pid)}, files=files)
        assert r.status_code == 400 and "connected project" in r.text, (r.status_code, r.text)
        with Session(engine) as s:
            assert not s.exec(select(Document).where(Document.project_id == pid)).all()
        # the same call on an upload project still works (not a blanket refusal)
        files = {"file": ("note.txt", b"Hello world. Second sentence.", "text/plain")}
        r = c.post("/api/documents", headers=A, data={"project_id": str(up)}, files=files)
        assert r.status_code == 200, r.text

        # web: the form route
        from tests._csrf import csrf_for

        cl = TestClient(app, follow_redirects=False)
        csrf_for(cl)
        r = cl.post("/auth/login", data={"email": admin.email, "password": PW, "next": "/"})
        assert r.status_code == 303, r.status_code
        csrf_for(cl)
        files = {"file": ("note.txt", b"Hello world.", "text/plain")}
        r = cl.post(f"/projects/{pid}/documents", files=files)
        assert r.status_code == 400 and "connected project" in r.text, (r.status_code, r.text[:200])
        r = cl.post(
            "/images/upload",
            data={"project_id": str(pid)},
            files={"file": ("a.png", b"\x89PNG\r\n", "image/png")},
        )
        assert r.status_code in (400, 404), r.status_code  # 404 when the images module is off
        r = c.post(
            "/api/images",
            headers=A,
            data={"project_id": str(pid)},
            files={"file": ("a.png", b"\x89PNG\r\n", "image/png")},
        )
        assert r.status_code in (400, 404), r.status_code
        with Session(engine) as s:
            assert not s.exec(select(Document).where(Document.project_id == pid)).all()

        # CLI (in-process): doc upload refused with a usage exit
        path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "cp_note.txt")
        with open(path, "wb") as fh:
            fh.write(b"Hello world.")
        code, out = cli("doc", "upload", str(pid), path)
        assert code != 0, (code, out)
        code, out = cli("doc", "upload", str(up), path)
        assert code == 0, (code, out)
        print(
            "  uploads: web, /api, images and CLI refuse a connected project; upload projects fine"
        )
    finally:
        settings.set_many({"security_auth_required": "0"})


def test_web_create_and_pages():
    settings.set_many({"security_auth_required": "1", "project_scoping": "0"})
    try:
        from tests._csrf import csrf_for

        admin = security.create_user("cp-web-admin@x.co", "CP Web", Role.ADMIN, PW)
        mgr = security.create_user("cp-web-mgr@x.co", "CP WebM", Role.MANAGER, PW)
        viewer = security.create_user("cp-web-view@x.co", "CP WebV", Role.VIEWER, PW)
        ca = mk_customer("CP Web A")

        def login(user):
            cl = TestClient(app, follow_redirects=False)
            csrf_for(cl)
            r = cl.post("/auth/login", data={"email": user.email, "password": PW, "next": "/"})
            assert r.status_code == 303, r.status_code
            csrf_for(cl)
            return cl

        a = login(admin)
        # the dialog offers the three types
        page = a.get("/projects")
        assert page.status_code == 200
        assert 'value="connected"' in page.text and "<strong>Connected</strong>" in page.text
        assert "<strong>Documents</strong>" in page.text
        assert "data-np-upload-only" in page.text
        # create: project_type=connected -> a connected Documents project, lands on the setup menu
        r = a.post(
            "/projects",
            data={
                "name": "CP web conn",
                "customer_id": str(ca),
                "project_type": "connected",
                "source_locale": "en-US",
                "target_locales": ["de-DE"],
            },
        )
        assert r.status_code == 303, (r.status_code, r.text[:200])
        m = r.headers["location"]
        assert m.endswith("#source") and m.startswith("/projects/"), m
        pid = int(m.split("/")[2].split("#")[0])
        p = row(pid)
        assert p.intake == "connected" and p.project_type == "document", (p.intake, p.project_type)
        # a Trados package with Connected is refused (no upload step)
        r = a.post(
            "/projects",
            data={"name": "CP web conn pkg", "customer_id": str(ca), "project_type": "connected"},
            files={"sdl_package": ("x.sdlppx", b"PK", "application/octet-stream")},
        )
        assert r.status_code == 303 and "err=" in r.headers["location"], r.headers["location"]
        # a normal create is untouched
        r = a.post("/projects", data={"name": "CP web doc", "customer_id": str(ca)})
        assert r.status_code == 303 and "#source" not in r.headers["location"]
        doc_pid = int(r.headers["location"].split("/")[2])
        assert row(doc_pid).intake == "upload"

        # the project page: setup menu, no upload area
        pg = a.get(f"/projects/{pid}")
        assert pg.status_code == 200
        t = pg.text
        assert 'id="source"' in t and "Connect a source" in t, "the setup menu"
        assert 'id="doc-upload-form"' not in t and "Upload a document" not in t, "uploads hidden"
        assert "No documents yet - they appear after the first run" in t
        assert "source-step-1" in t and "Connect a new system" in t
        assert f'href="/connectors/zendesk?return_to=/projects/{pid}"' in t
        # a normal project page keeps its upload area and has no source card
        pg2 = a.get(f"/projects/{doc_pid}")
        assert 'id="doc-upload-form"' in pg2.text and 'id="source"' not in pg2.text

        # step 1 lists the visible connections with "already feeds N projects"
        conn = mk_conn("ZD web")
        other = mk_project("CP web other", intake="connected", customer_id=ca)
        pa.bind(other, conn.id, actor="m@x.co", is_admin=True)
        pg = a.get(f"/projects/{pid}")
        assert "ZD web" in pg.text and "already feeds 1 project" in pg.text, "feeds count"
        for hostile in ("%C2%B2", "99999999999999999999", "-1", "abc", "0"):
            assert a.get(f"/projects/{pid}?connection={hostile}").status_code == 200, hostile
        pg = a.get(f"/projects/{pid}?connection={conn.id}")
        assert f'value="{conn.id}" selected' in pg.text, "new connection preselected"
        # the projects list: filter and badge
        lst = a.get("/projects?intake=connected")
        assert lst.status_code == 200
        assert "CP web conn" in lst.text and "CP web doc" not in lst.text
        assert "Not connected yet" in lst.text and "Zendesk" in lst.text
        assert 'id="intake-filter"' in lst.text

        # bind through the project page form (return_to keeps us on the project)
        mg = login(mgr)
        r = mg.post(
            f"/projects/{pid}/automation/bind",
            data={"connection_id": str(conn.id), "return_to": f"/projects/{pid}"},
        )
        assert r.status_code == 303, r.status_code
        loc = r.headers["location"]
        assert (
            loc.startswith(f"/projects/{pid}?") and "src_ok=1" in loc and loc.endswith("#source")
        ), loc
        pg = mg.get(loc.split("#")[0])
        t = pg.text
        assert "Source" in t and "Connect a source" not in t, "the Source card replaces the menu"
        assert "ZD web" in t and "Run now" in t and "Change source" in t and "Pause" in t
        assert "Next run:" in t and "Last run:" in t
        assert "Content source set." in t, "the flash shows"
        assert 'id="project-notifications"' in t, "notifications folded into the card"
        assert t.count('id="project-notifications"') == 1
        assert 'id="doc-upload-form"' not in t
        # the old Automation link is replaced by the card's own link
        assert f'href="/projects/{pid}/automation">Automation</a>' not in t
        # preview: a dry run that reads and writes nothing
        pv = mg.get(f"/projects/{pid}?preview=1")
        assert 'id="source-preview"' in pv.text and "reads nothing" in pv.text, "preview panel"
        with Session(engine) as s:
            assert not s.exec(select(Document).where(Document.project_id == pid)).all()
        # Change source -> the setup menu again, still connected
        r = mg.post(f"/projects/{pid}/automation/unbind", data={"return_to": f"/projects/{pid}"})
        assert r.status_code == 303 and r.headers["location"].endswith("#source")
        pg = mg.get(f"/projects/{pid}")
        assert "Connect a source" in pg.text and "source-step-1" in pg.text
        assert row(pid).intake == "connected"
        # a hostile return_to falls back to the Automation page, never an off-site redirect
        r = mg.post(
            f"/projects/{pid}/automation/bind",
            data={"connection_id": str(conn.id), "return_to": "//evil.example/x"},
        )
        assert r.status_code == 303 and r.headers["location"].startswith(
            f"/projects/{pid}/automation"
        ), r.headers["location"]
        mg.post(f"/projects/{pid}/automation/unbind", data={})
        # "Use uploads instead" on the setup menu, and the rule behind it
        sw = mk_project("CP web switch", intake="connected", customer_id=ca)
        assert 'id="source-use-uploads"' in mg.get(f"/projects/{sw}").text
        r = mg.post(f"/projects/{sw}/intake", data={"intake": "upload"})
        assert r.status_code == 303 and r.headers["location"] == f"/projects/{sw}"
        assert row(sw).intake == "upload"
        assert 'id="doc-upload-form"' in mg.get(f"/projects/{sw}").text
        add_doc(sw)
        r = mg.post(
            f"/projects/{sw}/intake", data={"intake": "connected", "return_to": f"/projects/{sw}"}
        )
        assert r.status_code == 303 and "src_ok=0" in r.headers["location"], r.headers["location"]
        assert row(sw).intake == "upload", "a project with documents cannot switch"
        # a viewer sees the page but not the controls
        vw = login(viewer)
        pg = vw.get(f"/projects/{pid}")
        assert pg.status_code == 200 and "A manager or admin sets up the source" in pg.text
        assert 'id="source-bind-form"' not in pg.text
        r = vw.post(f"/projects/{pid}/automation/bind", data={"connection_id": str(conn.id)})
        assert r.status_code == 403

        # Connect a new system: the connector page carries return_to into its form, and the
        # created connection sends us back with it selected
        cpg = a.get(f"/connectors/zendesk?return_to=/projects/{pid}")
        assert cpg.status_code == 200
        assert f'name="return_to" value="/projects/{pid}"' in cpg.text, "hidden return_to"
        assert "connector-return-note" in cpg.text
        bad = a.get("/connectors/zendesk?return_to=https://evil.example/")
        assert 'name="return_to"' not in bad.text and "connector-return-note" not in bad.text
        r = a.post(
            "/connectors/zendesk",
            data={
                "name": "ZD created here",
                "return_to": f"/projects/{pid}",
                "subdomain": "acme",
                "email": "a@acme.com",
                "api_token": "tok-fixture-2",
                "source_locale": "en-us",
                "target_locales": "de",
            },
        )
        assert r.status_code == 303, (r.status_code, r.text[:300])
        loc = r.headers["location"]
        new = [cn for cn in cstore.list_connections() if cn.name == "ZD created here"]
        assert new, "the connection was created"
        assert loc == f"/projects/{pid}?connection={new[0].id}#source", loc
        r = a.post(
            "/connectors/zendesk",
            data={
                "name": "ZD evil",
                "return_to": "https://evil.example/",
                "subdomain": "acme",
                "email": "a@acme.com",
                "api_token": "tok-fixture-3",
                "source_locale": "en-us",
                "target_locales": "de",
            },
        )
        assert r.status_code == 303 and r.headers["location"].startswith(
            "/connectors/connection/"
        ), r.headers["location"]
        print(
            "  web: dialog types, create lands on setup, setup menu, Source card, unbind, return_to"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "1"})


def test_card_state_unbound_and_bound():
    """The read model behind the card (no HTTP): steps, choices, preview."""
    pid = mk_project("CP card", intake="connected")
    conn = mk_conn("ZD card")
    with Session(engine) as s:
        proj = s.get(Project, pid)
        st = cp.card_state(s, proj, None, {"bound": False}, can_manage=True)
        mine = [c for c in st["choices"] if c["name"] == "ZD card"]
        assert st["bound"] is False and len(mine) == 1, st["choices"]
        assert mine[0]["feeds"] == 0 and mine[0]["kind_label"] == "Zendesk Guide"
        assert st["new_kinds"] and st["preview"] is None
        st = cp.card_state(s, proj, None, {"bound": False}, can_manage=False)
        assert st["choices"] == [] and st["new_kinds"] == [], "a non-manager gets no picker"
    pa.bind(pid, conn.id, actor="m@x.co", is_admin=True)
    with Session(engine) as s:
        proj = s.get(Project, pid)
        arow = pa.get(s, pid)
        auto = pa.view(s, arow)
        st = cp.card_state(s, proj, arow, auto, can_manage=True, want_preview=True)
        assert st["bound"] and st["steps"][1] and st["steps"][3] and not st["steps"][6]
        pv = st["preview"]
        assert pv["ready"] is True and pv["languages"] == ["de", "fr"] and pv["documents"] == 0, pv
        assert "reads nothing" in pv["note"]
        # no languages -> the preview says the run cannot start
        from app.models import TargetLocale

        for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == pid)).all():
            s.delete(tl)
        s.commit()
        pv = cp.preview(s, s.get(Project, pid), arow)
        assert pv["ready"] is False and any("target languages" in p for p in pv["problems"]), pv
    print("  card state: unbound choices, bound steps, preview readiness (no egress)")


def _langs(pid):
    from app.models import TargetLocale

    with Session(engine) as s:
        return sorted(
            t.locale for t in s.exec(select(TargetLocale).where(TargetLocale.project_id == pid))
        )


def test_gate_bind_defaults_languages_and_stopped():
    """Review gate: spec step 3 (a connected project bound with NO language takes the connection's
    target locales through the normal add path; an upload project or one that has a language is
    untouched), and the Preview / step 3 count only ACTIVE languages (T2 soft stop)."""
    from datetime import UTC, datetime

    from app.models import TargetLocale

    conn = mk_conn("ZD langs")
    empty = mk_project("CP langs empty", intake="connected", targets="")
    assert _langs(empty) == []
    pa.bind(empty, conn.id, actor="m@x.co", is_admin=True)
    assert _langs(empty) == ["de", "fr"], _langs(empty)

    conn2 = mk_conn("ZD langs 2")
    plain = mk_project("CP langs upload", targets="")
    pa.bind(plain, conn2.id, actor="m@x.co", is_admin=True)
    assert _langs(plain) == [], "an upload project's languages are never filled"

    conn3 = mk_conn("ZD langs 3")
    own = mk_project("CP langs own", intake="connected", targets="it")
    pa.bind(own, conn3.id, actor="m@x.co", is_admin=True)
    assert _langs(own) == ["it"], "a project that has a language keeps exactly its list"

    with Session(engine) as s:
        tl = s.exec(
            select(TargetLocale).where(
                TargetLocale.project_id == empty, TargetLocale.locale == "fr"
            )
        ).first()
        tl.stopped_at = datetime.now(UTC).replace(tzinfo=None)
        s.add(tl)
        s.commit()
        proj = s.get(Project, empty)
        arow = pa.get(s, empty)
        pv = cp.preview(s, proj, arow)
        assert pv["languages"] == ["de"], pv["languages"]
        st = cp.card_state(s, proj, arow, pa.view(s, arow), can_manage=True)
        assert st["languages"] == 1, st["languages"]
    print("  gate: bind fills an empty connected project's languages; preview skips stopped ones")


def test_template_never_binds():
    cust = mk_customer("CP Tpl")
    pid = mk_project("CP tpl source", customer_id=cust)
    with Session(engine) as s:
        t = tpl.save_as_template(s, pid, "CP tpl", scope="customer")
        tid = t.id
    with Session(engine) as s:
        proj, _warn = tpl.apply_template(s, tid, "CP from tpl", intake="connected")
        new_id = proj.id
        assert proj.intake == "connected"
        assert pa.get(s, new_id) is None, "a template never binds"
        assert not s.exec(
            select(ProjectAutomation).where(ProjectAutomation.project_id == new_id)
        ).all()
        plain, _ = tpl.apply_template(s, tid, "CP from tpl plain")
        assert plain.intake == "upload"
        try:
            tpl.apply_template(s, tid, "CP from tpl bad", intake="x")
        except tpl.TemplateError:
            pass
        else:
            raise AssertionError("a bad intake must be a TemplateError")
    print("  template: a connected project from a template has no binding and no schedule")


def test_customer_isolation_step1():
    """Step 1 never lists another customer's connection, and forcing its id answers exactly like
    a missing id (404 semantics), for a manager of the project and for a customer-bound account."""
    settings.set_many({"security_auth_required": "1", "project_scoping": "0"})
    try:
        from urllib.parse import parse_qs, urlparse

        from tests._csrf import csrf_for

        ca = mk_customer("CP Iso A")
        cb = mk_customer("CP Iso B")
        mk_conn("ZD iso global")
        mk_conn("ZD iso A", customer_id=ca)
        conn_b = mk_conn("ZD iso B", customer_id=cb)
        pid_a = mk_project("CP iso A", intake="connected", customer_id=ca)
        pid_b = mk_project("CP iso B", intake="connected", customer_id=cb)
        mgr = security.create_user("cp-iso-m@x.co", "Iso M", Role.MANAGER, PW)
        bound = security.create_user("cp-iso-a@x.co", "Iso A", Role.TRANSLATOR, PW, customer_id=ca)
        with Session(engine) as s:
            for who in (mgr, bound):
                names = [c["name"] for c in cp.source_choices(s, s.get(Project, pid_a), who)]
                assert "ZD iso A" in names and "ZD iso global" in names, names
                assert "ZD iso B" not in names, ("another customer's connection listed", who.email)

        def login(user):
            cl = TestClient(app, follow_redirects=False)
            csrf_for(cl)
            r = cl.post("/auth/login", data={"email": user.email, "password": PW, "next": "/"})
            assert r.status_code == 303
            csrf_for(cl)
            return cl

        mg = login(mgr)
        pg = mg.get(f"/projects/{pid_a}")
        assert pg.status_code == 200
        assert "ZD iso A" in pg.text and "ZD iso global" in pg.text
        assert "ZD iso B" not in pg.text, "another customer's connection is not listed"
        rt = {"return_to": f"/projects/{pid_a}"}
        r1 = mg.post(
            f"/projects/{pid_a}/automation/bind", data={"connection_id": str(conn_b.id), **rt}
        )
        r2 = mg.post(f"/projects/{pid_a}/automation/bind", data={"connection_id": "999999", **rt})
        q1 = parse_qs(urlparse(r1.headers["location"]).query)
        q2 = parse_qs(urlparse(r2.headers["location"]).query)
        assert q1["src_ok"] == ["0"] and q1 == q2, (
            "a foreign id must read like a missing one",
            q1,
            q2,
        )
        with Session(engine) as s:
            assert pa.get(s, pid_a) is None, "nothing was bound"

        # a customer-bound account: no picker at all, and the other customer's project is invisible
        bc = login(bound)
        pg = bc.get(f"/projects/{pid_a}")
        assert pg.status_code == 200 and "ZD iso B" not in pg.text
        assert 'id="source-bind-form"' not in pg.text, "a translator has no setup controls"
        gone = bc.get(f"/projects/{pid_b}")
        missing = bc.get("/projects/999999")
        assert gone.status_code == missing.status_code, (gone.status_code, missing.status_code)
        print(
            "  isolation: step 1 never lists another customer's connection; forced id = missing id"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "1"})


def test_cli():
    from app.cli.__main__ import build_parser

    cust = mk_customer("CP CLI")
    code, out = cli(
        "project",
        "create",
        "CP cli conn",
        "--customer-id",
        str(cust),
        "--targets",
        "de",
        "--connected",
    )
    assert code == 0 and out["intake"] == "connected" and out["project_type"] == "document", out
    assert out["setup_url"].endswith("#source")
    pid = out["id"]
    code, out = cli("project", "create", "CP cli up", "--customer-id", str(cust), "--targets", "de")
    assert code == 0 and out["intake"] == "upload" and "setup_url" not in out, out
    up = out["id"]
    code, out = cli(
        "project",
        "create",
        "CP cli bad",
        "--customer-id",
        str(cust),
        "--connected",
        "--type",
        "key_based",
        "--targets",
        "de",
    )
    assert code == 2, (code, out)
    code, out = cli("project", "list", "--connected")
    assert code == 0, out
    ids = {p["id"] for p in out["projects"]}
    assert pid in ids and up not in ids, ids
    assert (
        next(p for p in out["projects"] if p["id"] == pid)["source"]["label"] == "Not connected yet"
    )
    # the intake verb
    code, out = cli("project", "set-intake", str(up), "connected")
    assert code == 0 and out["intake"] == "connected", out
    code, out = cli("project", "set-intake", str(up), "upload")
    assert code == 0 and out["intake"] == "upload", out
    add_doc(up)
    code, out = cli("project", "set-intake", str(up), "connected")
    assert code == 2, (code, out)
    leaves = set()

    def walk(parser, prefix):
        for act in parser._actions:
            if hasattr(act, "choices") and isinstance(act.choices, dict) and act.dest != "help":
                for name, sub in act.choices.items():
                    walk(sub, [*prefix, name])
        if prefix:
            leaves.add(" ".join(prefix))

    walk(build_parser(), [])
    assert "project set-intake" in leaves

    # --remote: the request body carries the intake, the list asks for the filter
    from app.cli import _remote_impl_common as ric

    sent = {}

    class FakeClient:
        def post(self, path, json=None):
            sent["post"] = (path, json)
            return {
                "ok": True,
                "id": 77,
                "name": "x",
                "source_locale": "en-US",
                "target_locales": [],
                "project_type": "document",
                "intake": "connected",
            }

        def get(self, path, params=None):
            sent["get"] = (path, params)
            return {
                "projects": [
                    {"id": 1, "name": "n", "source_locale": "en-US", "source": {"label": "Zendesk"}}
                ],
                "truncated": False,
                "hidden": {},
            }

    real = ric._client
    ric._client = lambda args: FakeClient()
    try:
        Args = argparse.Namespace(
            name="x",
            source="en-US",
            targets="",
            customer_id=1,
            provider=None,
            connected=True,
            project_type="",
            per_pair=False,
            pair_provider=[],
            due="",
            content_type="",
            all_targets=True,
            field=[],
            seg_profile="",
            archived="active",
            status="",
            limit=None,
            offset=None,
            columns="",
            sort_field="",
        )

        class Emit:
            as_json = True

            def result(self, msg, payload):
                sent["result"] = (msg, payload)

            def json(self, payload):
                sent["json"] = payload

            def warn(self, m):
                pass

            def say(self, m):
                pass

            def table(self, *a, **k):
                sent["table"] = (a, k)

        ric.project_create(Args, Emit())
        assert sent["post"][0] == "/api/projects" and sent["post"][1]["intake"] == "connected", sent
        assert "connected" in sent["result"][0]
        Emit.as_json = False
        ric.project_list(Args, Emit())
        assert sent["get"][0] == "/api/projects" and sent["get"][1].get("intake") == "connected", (
            sent
        )
        assert sent["table"][1]["headers"][5] == "connected source", sent["table"]
    finally:
        ric._client = real
    print("  cli: create --connected (in-process and --remote body), list --connected, set-intake")


def test_mcp():
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import registry
    import server

    calls = []

    class Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    async def fake_post(client, path, body=None):
        calls.append(("POST", path, body))
        if path == "/api/projects":
            return {
                "ok": True,
                "id": 41,
                "intake": body.get("intake", "upload"),
                "setup_url": "/projects/41#source" if body.get("intake") == "connected" else None,
            }
        return {"ok": True, "intake": (body or {}).get("intake")}

    async def fake_get(client, path, params=None):
        calls.append(("GET", path, params))
        return {"ok": True, "projects": [], "customers": [], "hidden": {}}

    real = (server._client, server._post_json, server._get_json)
    server._client, server._post_json, server._get_json = lambda: Ctx(), fake_post, fake_get
    try:
        out = json.loads(
            asyncio.run(server.create_project("MCP conn", customer_id=3, intake="connected"))
        )
        post = next(c for c in calls if c[1] == "/api/projects")
        assert post[2]["intake"] == "connected", post
        assert "project_automation" in out["next"] and "/projects/41#source" in out["next"], out
        out = json.loads(asyncio.run(server.create_project("MCP up", customer_id=3)))
        assert "intake" not in [c for c in calls if c[1] == "/api/projects"][1][2]
        assert "next" not in out
        for bad in (
            {"intake": "sideways"},
            {"intake": "connected", "project_type": "key_based"},
        ):
            try:
                asyncio.run(server.create_project("MCP bad", customer_id=3, **bad))
            except server.ToolError:
                pass
            else:
                raise AssertionError(f"accepted {bad}")
        calls.clear()
        asyncio.run(server.list_projects(intake="connected"))
        assert calls[0][2]["intake"] == "connected", calls
        try:
            asyncio.run(server.list_projects(intake="x"))
        except server.ToolError:
            pass
        else:
            raise AssertionError("a bad intake filter must be refused")
        calls.clear()
        asyncio.run(server.configure_project(5, "intake", intake="connected"))
        assert calls[0][1] == "/api/projects/5/intake" and calls[0][2] == {"intake": "connected"}
        try:
            asyncio.run(server.project_automation(5, "bind"))
        except server.ToolError as exc:
            assert "/projects/5#source" in str(exc) and "never creates a connection" in str(exc), (
                str(exc)
            )
        else:
            raise AssertionError("bind without a connection must point at the setup page")
    finally:
        server._client, server._post_json, server._get_json = real
    tool = next(t for t in registry.TOOLS if t.name == "create_project")
    assert "intake" in tool.input_schema["properties"]
    assert (
        "intake"
        in next(t for t in registry.TOOLS if t.name == "list_projects").input_schema["properties"]
    )
    cfg = next(t for t in registry.TOOLS if t.name == "configure_project")
    assert "intake" in cfg.input_schema["properties"]["action"]["enum"]
    print(
        "  mcp: create_project, list_projects, configure_project carry intake; bind names the page"
    )


def main() -> None:
    init_db()
    test_model_and_service()
    test_intake_switch()
    test_return_to_allowlist()
    test_card_state_unbound_and_bound()
    test_gate_bind_defaults_languages_and_stopped()
    test_template_never_binds()
    test_api_create_list_intake()
    test_upload_routes_refuse()
    test_web_create_and_pages()
    test_customer_isolation_step1()
    test_cli()
    test_mcp()
    print("verify_connected_projects OK")


if __name__ == "__main__":
    main()
