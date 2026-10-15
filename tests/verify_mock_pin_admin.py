"""Choosing the offline Mock engine for a project is admin-only (issue #577), sign-in ON.

The install default was already admin-only (#561). Any writer could still pin ONE project to
Mock (placeholder text) through POST /projects, the API, a per-pair matrix, a customer default
or a template. Every one of those now answers 403 below admin; an admin is unaffected; a project
already pinned to Mock keeps working (a linguist re-saving the same matrix is not refused).

  [1] web      POST /projects with default_provider=mock: linguist 403, no row; admin 303.
  [2] api      POST /api/projects provider=mock and a mock pair mapping: linguist 403; admin 200.
  [3] pairs    POST /api/projects/{id}/pair-providers and the web twin: linguist 403 for a NEW mock
               pair; admin 200; the linguist re-saving the already-Mock pair is allowed.
  [4] customer POST /api/customers/{id}/defaults provider=mock: linguist 403; admin 200; the
               linguist re-saving an existing Mock default is allowed.
  [5] template applying a template that pins Mock: linguist 403 (web and API); admin 200.
  [6] seam     project_create(creator=linguist) raises MockEngineAdminOnlyError; creator=None (the
               trusted local CLI operator) and sign-in off are not refused; a non-mock provider is
               never refused; while Mock is the install default a writer may carry it along.

Hermetic + offline. ASCII-only prints. Final line: "verify_mock_pin_admin OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mockpin_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import Customer, Project, ProjectTemplate, Role
from app.services import apikeys, content, security, settings
from app.services import pair_providers as pp
from tests._csrf import csrf_for

init_db()

PW = "mockpin577-pw-123456"


def _fail(msg):
    raise AssertionError(msg)


def _login(role):
    email = f"mockpin-{role}@x.com"
    user = security.create_user(email, email, role, PW)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (role, r.status_code)
    csrf_for(c)
    _, raw = apikeys.create_key(user.id, f"mockpin-{role}", scopes="api docs")
    return c, user, {"X-Loc-Token": raw}


def _project_names():
    with Session(engine) as s:
        return {p.name for p in s.exec(select(Project)).all()}


def main():
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    with Session(engine) as s:
        cust = Customer(name="MockPin Co")
        s.add(cust)
        s.commit()
        cid = cust.id
        pinned = content.project_create(
            s,
            "Already pinned",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=str(cid),
            default_provider="mock",
        )
        pinned_id = pinned.id
        plain = content.project_create(
            s,
            "Plain project",
            source_locale="en-US",
            target_locales="de-DE, fr-FR",
            customer_id=str(cid),
        )
        plain_id = plain.id
        tpl = ProjectTemplate(
            name="Mock template",
            customer_id=cid,
            source_locale="en-US",
            target_locales=["de-DE"],
            default_provider="mock",
        )
        s.add(tpl)
        s.commit()
        tpl_id = tpl.id

    admin_user = security.create_user("mockpin-admin0@x.com", "a", Role.ADMIN, PW)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    ca, _ua, ha = _login(Role.ADMIN)
    cl, ul, hl = _login(Role.LINGUIST)

    # [1] web create
    form = {
        "name": "Web mock pin",
        "source_locale": "en-US",
        "target_locales": "de-DE",
        "customer_id": str(cid),
        "default_provider": "mock",
    }
    r = cl.post("/projects", data={**form, "name": "Linguist web pin"}, follow_redirects=False)
    assert r.status_code == 403, f"linguist web mock create: {r.status_code}"
    assert "Linguist web pin" not in _project_names(), "linguist web create left a project row"
    r = cl.post(
        "/projects",
        data={**form, "name": "Linguist web plain", "default_provider": "claude"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers.get("location", ""), (
        f"linguist non-mock web create: {r.status_code} {r.headers.get('location')}"
    )
    r = ca.post("/projects", data={**form, "name": "Admin web pin"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers.get("location", ""), (
        f"admin web mock create: {r.status_code} {r.headers.get('location')}"
    )
    print("  [1] web: linguist 403 (no row), admin and a non-mock linguist create succeed OK")

    # [2] api create
    body = {"name": "Api mock pin", "customer_id": cid, "targets": ["de-DE"], "provider": "mock"}
    r = cl.post("/api/projects", json={**body, "name": "Linguist api pin"}, headers=hl)
    assert r.status_code == 403, f"linguist api mock create: {r.status_code} {r.text}"
    assert "Linguist api pin" not in _project_names(), "linguist api create left a project row"
    pair_body = {
        "name": "Linguist api pair pin",
        "customer_id": cid,
        "targets": ["de-DE"],
        "provider": "claude",
        "provider_mode": "per_pair",
        "pair_providers": {"en-US:de-DE": "mock"},
    }
    r = cl.post("/api/projects", json=pair_body, headers=hl)
    assert r.status_code == 403, f"linguist api mock pair create: {r.status_code} {r.text}"
    assert "Linguist api pair pin" not in _project_names(), "pair create left a project row"
    r = ca.post("/api/projects", json={**body, "name": "Admin api pin"}, headers=ha)
    assert r.status_code == 200, f"admin api mock create: {r.status_code} {r.text}"
    r = cl.post(
        "/api/projects",
        json={**body, "name": "Linguist api plain", "provider": "claude"},
        headers=hl,
    )
    assert r.status_code == 200, f"linguist non-mock api create: {r.status_code} {r.text}"
    print("  [2] api: linguist 403 for provider and pair mock (no row), admin 200 OK")

    # [3] pair-providers matrix
    mock_pair = {"provider_mode": "per_pair", "pair_providers": {"en-US:de-DE": "mock"}}
    r = cl.post(f"/api/projects/{plain_id}/pair-providers", json=mock_pair, headers=hl)
    assert r.status_code == 403, f"linguist new mock pair: {r.status_code} {r.text}"
    web = cl.post(
        f"/projects/{plain_id}/pair-providers",
        data={"provider_mode": "per_pair", "pair_target": ["de-DE"], "pair_provider": ["mock"]},
        follow_redirects=False,
    )
    assert web.status_code == 403, f"linguist web new mock pair: {web.status_code}"
    r = ca.post(f"/api/projects/{plain_id}/pair-providers", json=mock_pair, headers=ha)
    assert r.status_code == 200, f"admin mock pair: {r.status_code} {r.text}"
    r = cl.post(f"/api/projects/{plain_id}/pair-providers", json=mock_pair, headers=hl)
    assert r.status_code == 200, (
        f"linguist re-save of an existing mock pair: {r.status_code} {r.text}"
    )
    other = {
        "provider_mode": "per_pair",
        "pair_providers": {"en-US:de-DE": "mock", "en-US:fr-FR": "mock"},
    }
    r = cl.post(f"/api/projects/{plain_id}/pair-providers", json=other, headers=hl)
    assert r.status_code == 403, f"linguist adding a second mock pair: {r.status_code} {r.text}"
    with Session(engine) as s:
        resolved = pp.resolve_provider(s, s.get(Project, plain_id), "en-US", "de-DE")
        assert resolved == "mock", f"existing mock pair stopped resolving: {resolved}"
        assert pp.resolve_provider(s, s.get(Project, pinned_id), "en-US", "de-DE") == "mock"
    print("  [3] pairs: new mock pair 403 (api and web); existing mock pair re-save and resolve OK")

    # [4] customer default
    r = cl.post(f"/api/customers/{cid}/defaults", json={"provider": "mock"}, headers=hl)
    assert r.status_code == 403, f"linguist customer mock default: {r.status_code} {r.text}"
    r = ca.post(f"/api/customers/{cid}/defaults", json={"provider": "mock"}, headers=ha)
    assert r.status_code == 200, f"admin customer mock default: {r.status_code} {r.text}"
    r = cl.post(f"/api/customers/{cid}/defaults", json={"provider": "mock"}, headers=hl)
    assert r.status_code == 200, f"linguist re-save of an existing mock default: {r.status_code}"
    r = ca.post(f"/api/customers/{cid}/defaults", json={"provider": "claude"}, headers=ha)
    assert r.status_code == 200, r.text
    print("  [4] customer default: new mock 403, admin 200, existing mock re-save allowed OK")

    # [5] template that pins mock
    r = cl.post(
        f"/api/templates/{tpl_id}/apply",
        json={"name": "Linguist from tpl", "customer_id": cid},
        headers=hl,
    )
    assert r.status_code == 403, f"linguist apply mock template: {r.status_code} {r.text}"
    web = cl.post(
        "/projects/from-template",
        data={"template_id": str(tpl_id), "name": "Linguist web tpl", "customer_id": str(cid)},
        follow_redirects=False,
    )
    assert web.status_code == 403, f"linguist web apply mock template: {web.status_code}"
    assert not ({"Linguist from tpl", "Linguist web tpl"} & _project_names()), "template left a row"
    r = ca.post(
        f"/api/templates/{tpl_id}/apply",
        json={"name": "Admin from tpl", "customer_id": cid},
        headers=ha,
    )
    assert r.status_code == 200, f"admin apply mock template: {r.status_code} {r.text}"
    print("  [5] template: linguist 403 (api and web, no row), admin 200 OK")

    # [6] the service seam
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    with Session(engine) as s:
        try:
            content.project_create(
                s,
                "Seam linguist",
                target_locales="de-DE",
                customer_id=str(cid),
                default_provider="mock",
                creator=ul,
            )
        except content.MockEngineAdminOnlyError:
            pass
        else:
            _fail("project_create let a linguist pin mock")
        assert "Seam linguist" not in _project_names()
        for label, kwargs in (
            ("trusted caller (creator None)", {"creator": None}),
            ("admin", {"creator": admin_user}),
        ):
            content.project_create(
                s,
                f"Seam {label}",
                target_locales="de-DE",
                customer_id=str(cid),
                default_provider="mock",
                **kwargs,
            )
        content.project_create(
            s,
            "Seam non-mock",
            target_locales="de-DE",
            customer_id=str(cid),
            default_provider="claude",
            creator=ul,
        )
    real_default = config.DEFAULT_PROVIDER
    config.DEFAULT_PROVIDER = "mock"
    try:
        with Session(engine) as s:
            content.project_create(
                s,
                "Seam carried default",
                target_locales="de-DE",
                customer_id=str(cid),
                default_provider="mock",
                creator=ul,
            )
    finally:
        config.DEFAULT_PROVIDER = real_default
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    with Session(engine) as s:
        content.project_create(
            s,
            "Seam auth off",
            target_locales="de-DE",
            customer_id=str(cid),
            default_provider="mock",
            creator=ul,
        )
    print("  [6] seam: linguist refused; None, admin, non-mock, default-mock, auth-off pass OK")


if __name__ == "__main__":
    main()
    print("verify_mock_pin_admin OK")
