"""#580 + #582: /api input handling and TM customer-list scoping.

Issue #582 (check): GET /api/tm and /api/tm/stats answer a `customers` picker list while no
customer is chosen. It must hold only the caller's own org's active customers (multi-org on),
only the bound customer for a customer-bound key, and nothing from another org.

Issue #580 items (each a section below, run in order):
  [A] unknown customer_id on POST /api/projects is a 404 (never a 500), on /api, web and CLI
  [B] duplicate customer / project names are refused with 409 on /api, web and CLI
  [C] project status moves only along the allowed graph (400 lists the allowed next states)
  [D] an unknown provider NAME on translate is a 400, not the misleading 409 "no engine"
  [E] /api request-validation errors use the {ok:false,error} shape
  [F] a non-JSON customer / project body says so instead of "name is blank"
  [G] a no-project upload names how it was routed (the inbox fallback itself is by design)

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). Run one section with
`python tests/verify_api_input_580.py A`; no argument runs all. Final line: "API INPUT 580 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_apiinput580_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import Customer, Organization, Project, ProjectStatus, Role, User
from app.services import apikeys, content, lifecycle, security, settings

PW = "apiinput580-pw-123456"


def _gate_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()


def _gate_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _auth_off():
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()


def _auth_on():
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()


def _h(raw):
    return {"X-Loc-Token": raw}


def _key(user_id, scopes="api docs"):
    _row, raw = apikeys.create_key(user_id, "t580", scopes=scopes)
    return raw


def _user(email, role, org_id=None, customer_id=None):
    u = security.create_user(email, email, role, PW, customer_id=customer_id)
    if org_id is not None:
        with Session(engine) as s:
            row = s.get(User, u.id)
            row.org_id = org_id
            s.add(row)
            s.commit()
    return u.id


def _client():
    from app.main import app

    return TestClient(app)


def _two_org_world():
    """Org A: customers A1, A2 (+ a project each). Org B: customers B1, B2. Users: admin A,
    admin B, a linguist bound to A1."""
    with Session(engine) as s:
        oa = Organization(name="Org A")
        ob = Organization(name="Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
    settings.set_many({"multi_org_enabled": "1"})
    settings.invalidate_cache()
    admin_a = _user("admina580@x.com", Role.ADMIN, oa_id)
    admin_b = _user("adminb580@x.com", Role.ADMIN, ob_id)
    with Session(engine) as s:
        adm_a, adm_b = s.get(User, admin_a), s.get(User, admin_b)
        a1 = content.customer_create(s, "TM Alpha One", creator=adm_a).id
        a2 = content.customer_create(s, "TM Alpha Two", creator=adm_a).id
        b1 = content.customer_create(s, "TM Beta One", creator=adm_b).id
        b2 = content.customer_create(s, "TM Beta Two", creator=adm_b).id
        # Pin every row's org directly: the stamp-by-creator path depends on the multi_org
        # cache being live at create time, which is brittle in a seed.
        for name, cid, who, org in (
            ("A1 proj", a1, adm_a, oa_id),
            ("A2 proj", a2, adm_a, oa_id),
            ("B1 proj", b1, adm_b, ob_id),
            ("B2 proj", b2, adm_b, ob_id),
        ):
            proj = content.project_create(
                s,
                name,
                source_locale="en-US",
                target_locales="de-DE",
                customer_id=cid,
                creator=who,
                default_provider="mock",
            )
            proj.org_id = org
            s.add(proj)
        for cid, org in ((a1, oa_id), (a2, oa_id), (b1, ob_id), (b2, ob_id)):
            cust = s.get(Customer, cid)
            cust.org_id = org
            s.add(cust)
        s.commit()
    bound_a1 = _user("bound580@x.com", Role.LINGUIST, oa_id, customer_id=a1)
    return {
        "oa": oa_id,
        "ob": ob_id,
        "a1": a1,
        "a2": a2,
        "b1": b1,
        "b2": b2,
        "admin_a": _key(admin_a),
        "admin_b": _key(admin_b),
        "bound_a1": _key(bound_a1),
    }


def _names(body):
    return sorted(c["name"] for c in body.get("customers", []))


# --------------------------------------------------------------------------- #
# #582
# --------------------------------------------------------------------------- #
def test_tm_customer_list_scoped(w, api):
    own_a = ["TM Alpha One", "TM Alpha Two"]
    for path in ("/api/tm", "/api/tm/stats"):
        got = api.get(path, headers=_h(w["admin_a"]))
        assert got.status_code == 200, (path, got.text)
        assert _names(got.json()) == own_a, ("org A admin sees only org A", path, got.json())

        got = api.get(path, headers=_h(w["admin_b"]))
        assert _names(got.json()) == ["TM Beta One", "TM Beta Two"], (
            "org B admin sees only org B",
            path,
            got.json(),
        )

        # A customer-bound key: its own customer is forced, so no picker list at all.
        got = api.get(path, headers=_h(w["bound_a1"]))
        body = got.json()
        assert got.status_code == 200 and body.get("customer_id") == w["a1"], (path, body)
        assert not body.get("customers"), ("a bound key gets no picker list", path, body)
        for other in ("TM Alpha Two", "TM Beta One", "TM Beta Two"):
            assert other not in got.text, ("bound key must not see", other, path)

        # Asking for a foreign org's customer id must not select it nor name it.
        got = api.get(path + "?customer=%d" % w["b1"], headers=_h(w["admin_a"]))  # noqa: UP031
        assert "TM Beta" not in got.text, ("org A key asking for org B customer", path, got.text)
        assert _names(got.json()) == own_a, got.json()
        # ... and a bound key asking for a sibling customer stays on its own.
        got = api.get(path + "?customer=%d" % w["a2"], headers=_h(w["bound_a1"]))  # noqa: UP031
        assert got.json().get("customer_id") == w["a1"], got.json()
    # Multi-org OFF (single tenant, auth on): the bound key still sees only its own customer,
    # and an unbound key's picker is the same roster /api/customers gives it, nothing more.
    settings.set_many({"multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        for path in ("/api/tm", "/api/tm/stats"):
            got = api.get(path, headers=_h(w["bound_a1"]))
            body = got.json()
            assert body.get("customer_id") == w["a1"] and not body.get("customers"), (path, body)
            for other in ("TM Alpha Two", "TM Beta One", "TM Beta Two"):
                assert other not in got.text, ("single-tenant bound key must not see", other)
        roster = sorted(
            c["name"]
            for c in api.get("/api/customers", headers=_h(w["admin_a"])).json()["customers"]
        )
        for path in ("/api/tm", "/api/tm/stats"):
            assert _names(api.get(path, headers=_h(w["admin_a"])).json()) == roster, path
    finally:
        settings.set_many({"multi_org_enabled": "1"})
        settings.invalidate_cache()
    print("  [#582] /api/tm + /api/tm/stats customer lists: org A, org B, bound key all scoped OK")


# --------------------------------------------------------------------------- #
# #580
# --------------------------------------------------------------------------- #
def _run_cli(argv):
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue() + err.getvalue()


def _new_customer(name):
    with Session(engine) as s:
        return content.customer_create(s, name).id


def _new_project(name, cust_id, status=None):
    with Session(engine) as s:
        p = content.project_create(
            s, name, source_locale="en-US", target_locales="de-DE", customer_id=cust_id
        )
        if status:
            p.status = status
            s.add(p)
            s.commit()
        return p.id


def _status_of(pid):
    with Session(engine) as s:
        return s.get(Project, pid).status


def test_A_unknown_customer(w, api):
    h = _h(w["admin_a"])
    r = api.post(
        "/api/projects",
        headers=h,
        json={"name": "Ghost", "customer_id": 999999, "source_locale": "en-US", "targets": "de-DE"},
    )
    assert r.status_code == 404, ("unknown customer_id is a 404, not a 500", r.status_code, r.text)
    assert r.json() == {"ok": False, "error": "not found"}, r.json()
    # A customer in ANOTHER org answers byte-identically (multi-org on): no existence oracle.
    settings.set_many({"multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        r2 = api.post(
            "/api/projects",
            headers=h,
            json={"name": "Foreign", "customer_id": w["b1"], "targets": "de-DE"},
        )
        assert r2.status_code == 404 and r2.json() == r.json(), (r2.status_code, r2.text)
    finally:
        settings.set_many({"multi_org_enabled": "0"})
        settings.invalidate_cache()
    # Service seam: a typed error for every other caller (web, CLI).
    with Session(engine) as s:
        try:
            content.project_create(s, "Svc ghost", target_locales="de-DE", customer_id=999999)
        except content.UnknownCustomerError:
            pass
        else:
            raise AssertionError("project_create must raise UnknownCustomerError")
    rc, text = _run_cli(
        ["project", "create", "CLI ghost", "--customer-id", "999999", "--targets", "de-DE"]
    )
    assert rc == 2 and "not found" in text.lower(), ("CLI exit 2", rc, text)
    _auth_off()
    try:
        wr = api.post(
            "/projects",
            data={"name": "Web ghost", "customer_id": "999999", "target_locales": "de-DE"},
            follow_redirects=False,
        )
        assert wr.status_code == 303 and "err=" in wr.headers["location"], (
            "web flashes an error, never a 500",
            wr.status_code,
            wr.text[:200],
        )
    finally:
        _auth_on()
    print("  [A] unknown customer_id: /api 404 (foreign org identical), service error, CLI, web OK")


def test_B_duplicate_names(w, api):
    h = _h(w["admin_a"])
    # Existing duplicates (created before this change, through the bare service) stay put.
    old1 = _new_customer("Legacy Dup Co")
    old2 = _new_customer("Legacy Dup Co")
    assert old1 != old2
    r = api.post("/api/customers", headers=h, json={"name": "Fresh Co"})
    assert r.status_code == 200, r.text
    first = r.json()["id"]
    for variant in ("Fresh Co", "  fresh co  ", "FRESH CO"):
        r = api.post("/api/customers", headers=h, json={"name": variant})
        assert r.status_code == 409, ("duplicate customer name is a 409", variant, r.text)
        body = r.json()
        assert body["ok"] is False and body.get("existing_id") == first, body
        assert str(first) in body["error"], body
    got = api.get("/api/customers", headers=h, params={"name": "Fresh Co"}).json()["customers"]
    assert [c["id"] for c in got] == [first], got
    # A legacy duplicate name still refuses a THIRD copy.
    r = api.post("/api/customers", headers=h, json={"name": "Legacy Dup Co"})
    assert r.status_code == 409, r.text

    # Projects: same customer + same name = 409; another customer may reuse the name.
    other = _new_customer("Other Co")
    body = {"name": "Release Notes", "customer_id": first, "targets": "de-DE"}
    r = api.post("/api/projects", headers=h, json=body)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    r = api.post("/api/projects", headers=h, json={**body, "name": " release notes "})
    assert r.status_code == 409 and r.json().get("existing_id") == pid, (r.status_code, r.text)
    r = api.post("/api/projects", headers=h, json={**body, "customer_id": other})
    assert r.status_code == 200, ("same name under another customer is fine", r.text)

    # Multi-org: org B may reuse org A's customer name.
    settings.set_many({"multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        r = api.post("/api/customers", headers=_h(w["admin_a"]), json={"name": "Org Scoped Co"})
        assert r.status_code == 200, r.text
        r = api.post("/api/customers", headers=_h(w["admin_a"]), json={"name": "Org Scoped Co"})
        assert r.status_code == 409, r.text
        r = api.post("/api/customers", headers=_h(w["admin_b"]), json={"name": "Org Scoped Co"})
        assert r.status_code == 200, ("another org may reuse the name", r.text)
    finally:
        settings.set_many({"multi_org_enabled": "0"})
        settings.invalidate_cache()

    # CLI + service
    rc, text = _run_cli(["customer", "create", "Fresh Co"])
    assert rc == 2 and "already" in text.lower(), ("CLI exit 2", rc, text)
    rc, text = _run_cli(
        ["project", "create", "Release Notes", "--customer-id", str(first), "--targets", "de-DE"]
    )
    assert rc == 2 and "already" in text.lower(), ("CLI exit 2", rc, text)
    with Session(engine) as s:
        # The bare service stays permissive (hermetic fixtures + internal callers).
        assert content.customer_create(s, "Fresh Co") is not None
        try:
            content.customer_create(s, "Fresh Co", reject_duplicate=True)
        except content.DuplicateNameError:
            pass
        else:
            raise AssertionError("reject_duplicate=True must raise DuplicateNameError")

    # Web (auth off so the form posts need no session): refused with a 409, no new row.
    _auth_off()
    try:
        before = len(
            api.get("/api/customers", headers=h, params={"archived": "all"}).json()["customers"]
        )
        wr = api.post("/customers", data={"name": "Fresh Co"}, follow_redirects=False)
        assert wr.status_code == 409, ("web duplicate customer", wr.status_code, wr.text[:200])
        after = len(
            api.get("/api/customers", headers=h, params={"archived": "all"}).json()["customers"]
        )
        assert before == after, "a refused duplicate must not create a row"
        wr = api.post(
            "/projects",
            data={"name": "Release Notes", "customer_id": str(first), "target_locales": "de-DE"},
            follow_redirects=False,
        )
        assert wr.status_code == 303 and "err=" in wr.headers["location"], (
            "web duplicate project flashes an error",
            wr.status_code,
            wr.headers.get("location"),
        )
    finally:
        _auth_on()
    print(
        "  [B] duplicate names: 409 on /api (customer per org, project per customer), web, CLI OK"
    )


def test_C_status_transitions(w, api):
    h = _h(w["admin_a"])
    cid = _new_customer("Status Co")

    def move(pid, to):
        return api.post("/api/projects/%d/status" % pid, headers=h, json={"status": to})  # noqa: UP031

    pid = _new_project("S closed", cid, status="closed")
    r = move(pid, "draft")
    assert r.status_code == 400, ("closed -> draft refused", r.status_code, r.text)
    err = r.json()["error"]
    assert "in_translation" in err and "allowed" in err.lower(), err
    assert _status_of(pid) == "closed"

    pid2 = _new_project("S cancelled", cid, status="cancelled")
    r = move(pid2, "cancelled")
    assert r.status_code == 400 and "already" in r.json()["error"].lower(), r.text
    assert move(pid2, "completed").status_code == 400
    assert _status_of(pid2) == "cancelled"
    assert move(pid2, "in_translation").status_code == 200, "cancelled -> in_translation reopens"

    pid3 = _new_project("S flow", cid)  # starts in_translation
    for step in (
        "in_review",
        "completed",
        "closed",
        "in_translation",
        "cancelled",
        "in_translation",
    ):
        r = move(pid3, step)
        assert r.status_code == 200, (step, r.text)
    # The app's own automatic transitions are not subject to the manual graph.
    with Session(engine) as s:
        lifecycle.set_status(s, pid, ProjectStatus.DRAFT)  # internal write: unguarded
    assert _status_of(pid) == "draft"
    # CLI mirrors it
    pid4 = _new_project("S cli", cid, status="closed")
    rc, text = _run_cli(["project", "status", str(pid4), "draft"])
    assert rc == 2 and "in_translation" in text, ("CLI refuses closed -> draft", rc, text)
    rc, _text = _run_cli(["project", "status", str(pid4), "in_translation"])
    assert rc == 0 and _status_of(pid4) == "in_translation", rc
    # Web: refused with the lifecycle error flash, status unchanged
    pid5 = _new_project("S web", cid, status="closed")
    _auth_off()
    try:
        wr = api.post(
            "/projects/%d/status" % pid5,  # noqa: UP031
            data={"status": "draft"},
            follow_redirects=False,
        )
        assert wr.status_code == 303 and "err=status" in wr.headers["location"], wr.headers
    finally:
        _auth_on()
    assert _status_of(pid5) == "closed"
    # The page never offers a button the graph refuses: a completed project has no
    # "Mark completed" (a repeat), but still offers Reopen / Close / Cancel.
    pid6 = _new_project("S page", cid, status="completed")
    _auth_off()
    try:
        page = api.get("/projects/%d" % pid6).text  # noqa: UP031
    finally:
        _auth_on()
    assert 'value="in_translation"' in page and 'value="closed"' in page, "reopen/close offered"
    assert 'value="completed"' not in page, "a completed project must not offer Mark completed"
    print(
        "  [C] status graph: closed->draft + cancelled->cancelled refused (400 lists allowed), "
        "reopen/complete/close allowed, internal writes untouched, CLI + web match OK"
    )


def _upload(api, h, pid=None, name="a.txt", data=b"Hello world.\n\nSecond paragraph.\n"):
    form = {"project_id": str(pid)} if pid is not None else {}
    return api.post(
        "/api/documents", headers=h, files={"file": (name, data, "text/plain")}, data=form
    )


def test_D_unknown_provider(w, api):
    h = _h(w["admin_a"])
    cid = _new_customer("Provider Co")
    pid = _new_project("P doc", cid)
    r = _upload(api, h, pid)
    assert r.status_code == 200, r.text
    doc = r.json()["document_id"]
    r = api.post(
        "/api/documents/%d/translate" % doc,  # noqa: UP031
        headers=h,
        json={"targets": ["de-DE"], "provider": "nonexistent"},
    )
    assert r.status_code == 400, ("unknown provider name is a 400", r.status_code, r.text)
    assert "nonexistent" in r.json()["error"] and "unknown provider" in r.json()["error"].lower()
    # A KNOWN built-in with no key keeps the honest 409.
    r = api.post(
        "/api/documents/%d/translate" % doc,  # noqa: UP031
        headers=h,
        json={"targets": ["de-DE"], "provider": "claude"},
    )
    assert r.status_code == 409, ("keyless built-in stays 409", r.status_code, r.text)
    print("  [D] unknown provider name -> 400 naming it; keyless built-in still 409 OK")


def test_E_validation_shape(w, api):
    h = _h(w["admin_a"])
    r = api.get("/api/projects/abc", headers=h)
    assert r.status_code == 422, r.status_code
    body = r.json()
    assert body.get("ok") is False and isinstance(body.get("error"), str) and body["error"], body
    assert "detail" in body, "the FastAPI detail list stays for existing callers"
    r = api.get("/api/customers/xyz/detail", headers=h)
    assert r.status_code == 422 and r.json().get("ok") is False, r.text
    # Outside /api the framework default is untouched.
    r = api.get("/api/../projects/abc", headers=h)
    assert r.status_code != 500, r.status_code
    assert '"ok"' not in r.text, "the web error shape is not the /api one"
    print("  [E] /api path-id validation errors carry {ok:false,error} (+detail), web unchanged OK")


def test_F_non_json_body(w, api):
    h = {**_h(w["admin_a"]), "Content-Type": "application/json"}
    for path in ("/api/customers", "/api/projects"):
        r = api.post(path, headers=h, content=b"name=Acme&x=1")
        assert r.status_code == 400, (path, r.status_code, r.text)
        assert "json" in r.json()["error"].lower() and "blank" not in r.json()["error"], r.json()
        r = api.post(path, headers=h, content=b"[1, 2]")
        assert r.status_code == 400 and "object" in r.json()["error"].lower(), (path, r.text)
        # A real JSON object with a blank name keeps its friendly message.
        r = api.post(path, headers=h, json={"name": "   "})
        assert r.status_code == 400 and "blank" in r.json()["error"], (path, r.text)
    print("  [F] non-JSON / non-object body says so; blank name keeps 'blank' OK")


def test_G_upload_routing(w, api):
    h = _h(w["admin_a"])
    r = _upload(api, h, None, name="loose.txt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "inbox" in str(body.get("routing", "")).lower(), ("no-project upload says how", body)
    cid = _new_customer("Routing Co")
    pid = _new_project("R doc", cid)
    r = _upload(api, h, pid)
    assert "routing" not in r.json(), ("an explicit project is unchanged", r.json())
    print("  [G] no-project upload reports its routing; explicit project response unchanged OK")


SECTIONS = {
    "A": test_A_unknown_customer,
    "B": test_B_duplicate_names,
    "C": test_C_status_transitions,
    "D": test_D_unknown_provider,
    "E": test_E_validation_shape,
    "F": test_F_non_json_body,
    "G": test_G_upload_routing,
}


def main():
    which = set(sys.argv[1:])
    init_db()
    w = _two_org_world()
    api = _client()
    _gate_on()
    try:
        if not which or "582" in which:
            test_tm_customer_list_scoped(w, api)
        # Single-tenant from here on (multi-org is switched on only inside a check).
        settings.set_many({"multi_org_enabled": "0"})
        settings.invalidate_cache()
        for key, fn in SECTIONS.items():
            if not which or key in which:
                fn(w, api)
    finally:
        _gate_off()
    print("API INPUT 580 OK")


if __name__ == "__main__":
    main()
