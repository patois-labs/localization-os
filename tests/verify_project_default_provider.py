"""A project created WITHOUT a provider gets the install's CONFIGURED default (offline).

Every door must agree with the new-project form and .env (mock out of the box):

  [1] the service (`content.project_create(s, name)`, what scripts call) -> config.DEFAULT_PROVIDER
  [2] POST /api/projects with no provider -> the configured default
  [3] the CLI `project create` with no --provider -> the configured default
  [4] the web POST /projects with no default_provider field -> the configured default
  [5] an EXPLICIT provider is respected, and a customer default is still inherited when omitted
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_defprov_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import Customer, Project, Role
from app.services import apikeys, content, security, settings
from tests._cli_runner import run_cli


def _provider(name):
    with Session(engine) as s:
        p = s.exec(select(Project).where(Project.name == name)).first()
        assert p is not None, f"project {name!r} was not created"
        return p.default_provider


def main():
    init_db()
    settings.set_many(
        {"security_auth_required": "0", "multi_org_enabled": "0", "default_provider": "mock"}
    )
    settings.invalidate_cache()
    assert config.DEFAULT_PROVIDER == "mock", config.DEFAULT_PROVIDER
    c = TestClient(app)
    user = security.create_user(
        "defprov@example.test",
        "Defprov",
        Role.ADMIN,
        "offline-fixture-password-123",  # pregate: allow-secret offline fixture
    )
    hdr = {"X-Loc-Token": apikeys.create_key(user.id, "defprov", scopes="api docs")[1]}

    with Session(engine) as s:
        content.project_create(s, "dp-service", target_locales="fr-FR")
    assert _provider("dp-service") == "mock", _provider("dp-service")
    print("  [1] service default follows config.DEFAULT_PROVIDER")

    r = c.post("/api/projects", json={"name": "dp-api", "target_locales": ["fr-FR"]}, headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert _provider("dp-api") == "mock", _provider("dp-api")
    print("  [2] /api/projects default follows the configured default")

    code, out, err = run_cli(["project", "create", "dp-cli", "--targets", "fr-FR"])
    assert code == 0, (code, out, err)
    assert _provider("dp-cli") == "mock", _provider("dp-cli")
    print("  [3] CLI default follows the configured default")

    c.get("/projects")
    r = c.post(
        "/projects",
        data={"name": "dp-web", "target_locales": "fr-FR"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), (r.status_code, r.text[:200])
    assert _provider("dp-web") == "mock", _provider("dp-web")
    print("  [4] web POST without a provider follows the configured default")

    r = c.post(
        "/api/projects",
        json={"name": "dp-explicit", "target_locales": ["fr-FR"], "provider": "local"},
        headers=hdr,
    )
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert _provider("dp-explicit") == "local"
    with Session(engine) as s:
        cust = Customer(name="dp-cust", default_provider="gemini")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        content.project_create(s, "dp-inherit", target_locales="fr-FR", customer_id=cust.id)
    assert _provider("dp-inherit") == "gemini", _provider("dp-inherit")
    print("  [5] explicit provider respected; customer default still inherited when omitted")
    print("verify_project_default_provider OK")


if __name__ == "__main__":
    main()
