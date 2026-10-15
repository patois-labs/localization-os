"""#375: engine registration/configuration requires administrator approval.

Documented in: docs/architecture/engines-and-providers.md
Run only through tests/test_verify_suites.py. No provider calls.
"""

import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from tests._bootstrap import init_env

init_env("loctms_engine_admin_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.models import Role
from app.services import apikeys, engines, security, settings
from tests._csrf import csrf_for

PASSWORD = "engine-boundary-fixture-123"  # pregate: allow-secret offline fixture
FIXTURE_KEY = "fixture-provider-key"  # pregate: allow-secret offline fixture


def main():
    init_db()
    settings.set_many({"security_auth_required": "1", "remote_admin_enabled": "1"})
    profile = engines.create_profile("Approved", "deepl", api_key=FIXTURE_KEY, enabled=False)
    original = engines.get_profile(profile.id).model_dump()
    local_before = settings.get("local_base_url")
    for role in (
        Role.LINGUIST,
        Role.MANAGER,
        Role.TRANSLATOR,
        Role.REVIEWER,
        Role.VIEWER,
        Role.ADMIN,
    ):
        email = f"{role}@engine-boundary.test"
        user = security.create_user(email, email, role, PASSWORD)
        client = TestClient(app)
        csrf_for(client)
        assert (
            client.post(
                "/auth/login", data={"email": email, "password": PASSWORD}, follow_redirects=False
            ).status_code
            == 303
        )
        csrf_for(client)
        payload = {"name": f"Probe-{role}", "kind": "deepl", "api_key": FIXTURE_KEY}
        response = client.post("/setup-ai/engines", data=payload, follow_redirects=False)
        if role == Role.ADMIN:
            assert response.status_code == 303 and "engine_saved=1" in response.headers["location"]
            assert engines.get_by_name(f"Probe-{role}") is not None
            response = client.post(
                f"/setup-ai/engines/{profile.id}",
                data={"name": "Approved-updated", "api_key": FIXTURE_KEY, "enabled": "1"},
                follow_redirects=False,
            )
            assert response.status_code == 303 and "engine_saved=1" in response.headers["location"]
            assert engines.get_profile(profile.id).name == "Approved-updated"
            response = client.post(
                "/setup-ai/local",
                data={"base_url": "http://127.0.0.1:1234"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert settings.get("local_base_url") == "http://127.0.0.1:1234"
            continue
        assert response.status_code == 403, (role, response.status_code, response.headers)
        assert engines.get_by_name(f"Probe-{role}") is None
        for path, data in (
            (f"/setup-ai/engines/{profile.id}", {"name": "Hijacked", "api_key": FIXTURE_KEY}),
            (f"/setup-ai/engines/{profile.id}", {"base_url": "https://outside.example"}),
            (f"/setup-ai/engines/{profile.id}", {"enabled": "1"}),
            ("/setup-ai/local", {"base_url": "https://outside.example", "api_key": FIXTURE_KEY}),
            ("/setup-ai/local/models", {"base_url": "https://outside.example"}),
            ("/setup-ai/local/test", {"base_url": "https://outside.example"}),
            ("/setup-ai/cloud/models", {"provider": "openai", "api_key": FIXTURE_KEY}),
            (f"/setup-ai/engines/{profile.id}/models", {"base_url": "https://outside.example"}),
            ("/setup-ai/jev/import-env", {}),
            ("/setup-ai/keys/openai", {"api_key": FIXTURE_KEY}),
        ):
            response = client.post(path, data=data, follow_redirects=False)
            assert response.status_code == 403, (role, path, response.status_code)
        assert engines.get_profile(profile.id).model_dump() == original
        assert settings.get("local_base_url") == local_before
        # Remote CLI/MCP use this API. Even an admin-scoped token cannot lift
        # the account's role to administrator.
        _, raw = apikeys.create_key(user.id, "boundary", scopes="api admin")
        for path, body in (
            ("/api/admin/engines", payload),
            (f"/api/admin/engines/{profile.id}/update", {"enabled": True}),
            ("/api/admin/config", {"local_base_url": "https://outside.example"}),
        ):
            response = client.post(path, json=body, headers={"X-Loc-Token": raw})
            assert response.status_code == 403, (role, path, response.status_code)
        assert engines.get_profile(profile.id).model_dump() == original
        assert settings.get("local_base_url") == local_before
        if role == Role.LINGUIST:
            page = client.get("/setup-ai")
            assert page.status_code == 200
            assert '<form method="post" action="/setup-ai/engines"' not in page.text
            assert '<form method="post" action="/setup-ai/local"' not in page.text
            assert 'name="base_url"' not in page.text
            assert "Only administrators can register" in page.text
            # Reading configured models stays delegated; no real provider call.
            with patch("app.web.admin.cloud_list_models", return_value=["approved-model"]):
                response = client.post("/setup-ai/cloud/models", data={"provider": "openai"})
                assert response.status_code == 200
                assert response.json()["models"] == ["approved-model"]
            with patch.object(engines, "list_models", return_value={"models": ["approved-model"]}):
                response = client.post(f"/setup-ai/engines/{profile.id}/models")
                assert response.status_code == 200
                assert response.json()["models"] == ["approved-model"]
    print("verify_engine_admin_boundary OK")


if __name__ == "__main__":
    main()
