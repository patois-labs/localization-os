"""Boot the copied independent host offline, through the house subprocess runner."""

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

# No inherited install settings, secrets, backend or provider endpoints.
for key in list(os.environ):
    if key.startswith("LOC_TMS_") or key == "DATABASE_URL":
        os.environ.pop(key)
_TMP = Path(init_env("loctms_embedded_host_"))
os.environ.update(
    LOC_TMS_EMBEDDED="1",
    LOC_TMS_SERVER="1",
    DATABASE_URL="",
    LOC_TMS_ADMIN_EMAIL="host-admin@example.com",
    LOC_TMS_ADMIN_PASSWORD=(
        "host-example-"  # pregate: allow-secret hermetic example fixture, not a credential
        "password-123"
    ),
    LOC_TMS_TRUST_PRINCIPAL="host-principal@example.com",
    LOC_TMS_TRUST_HEADER_SECRET=(
        "host-example-"  # pregate: allow-secret hermetic example fixture, not a credential
        "trust-secret-1234567890"
    ),
    HOST_AUTH_TOKEN=(
        "host-example-"  # pregate: allow-secret hermetic example fixture, not a credential
        "authentication-1234567890"
    ),
    HOST_USER="host-user@example.com",
    PYTHONDONTWRITEBYTECODE="1",
    PYTHONPATH=str(_ROOT),
)
_HOST = _TMP / "independent-host"
shutil.copytree(_ROOT / "examples/embedded-host", _HOST)
os.chdir(_HOST)
sys.path.insert(0, str(_HOST))


def command(*args):
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, encoding="utf-8", timeout=90
    )


def test_host_lifecycle_bootstrap_auth_and_cron():
    bootstrap = command(
        "-m",
        "app.cli",
        "--json",
        "init",
        "--from-env",
        "--issue-key",
        "--key-scopes",
        "docs keys admin",
    )
    assert bootstrap.returncode == 0, bootstrap.stderr
    key = json.loads(bootstrap.stdout)["api_key"]
    assert key.startswith("lsu_")
    again = command("-m", "app.cli", "--json", "init", "--from-env")
    assert again.returncode == 0 and json.loads(again.stdout)["action"] == "noop"

    from fastapi.testclient import TestClient

    from app import config
    from app.services import audit, scheduler, settings, trust_auth

    host = importlib.import_module("host")
    assert Path(host.__file__).parent == _HOST
    assert not _HOST.is_relative_to(_ROOT)
    with TestClient(host.app) as client:
        headers = {"X-Loc-Token": key}
        assert (_ROOT / "app/static/style.css").is_file(), "probe an actual normal-UI asset"
        for path in ("/static/style.css",):
            assert client.get(path, headers=headers, follow_redirects=False).status_code == 404
        assert client.get("/api/projects").status_code == 401
        assert client.get("/api/projects", headers=headers).status_code == 200
        # Real host routing and real scheduler tick, with an observing spy only.
        with patch.object(scheduler, "tick", wraps=scheduler.tick) as tick:
            response = client.post("/host/tick", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["ran"] is True
            assert tick.call_count == 1
        assert client.post("/host/tick").status_code == 401
        purge = client.post("/host/retention/purge", headers=headers, json={"confirm": "purge"})
        assert purge.status_code == 404, purge.text  # remote-admin gate remains off

        settings.set_many(
            {"remote_admin_enabled": "1", "retention_enabled": "1", "retention_days": "30"}
        )
        purge = client.post("/host/retention/purge", headers=headers, json={"confirm": "purge"})
        assert purge.status_code == 200, purge.text
        settings.set_many({"remote_admin_enabled": "0", "retention_enabled": "0"})

        host_auth = {"Authorization": "Bearer " + os.environ["HOST_AUTH_TOKEN"]}
        assert client.get("/host/projects", headers=host_auth).status_code == 401
        forged = {
            "X-Loc-Trust-Secret": os.environ["LOC_TMS_TRUST_HEADER_SECRET"],
            "X-Loc-Trust-User": "forged@example.com",
        }
        assert client.get("/api/projects", headers=forged).status_code == 401
        settings.set_many({"embedded_trust_host_auth": "1", "audit_enabled": "1"})
        trust_auth.configure_at_boot()
        assert client.get("/host/projects").status_code == 401
        assert client.get("/api/projects", headers=forged).status_code == 401
        with patch.object(config, "IS_EMBEDDED", False):
            assert client.get("/host/projects", headers=host_auth).status_code == 401
        response = client.get("/host/projects", headers={**host_auth, **forged})
        assert response.status_code == 200, response.text
        assert (
            client.post("/host/projects", headers={**host_auth, **forged}, json={}).status_code
            == 403
        )
        rows = list(audit.iter_export())
        assert any(
            row.get("actor") == os.environ["LOC_TMS_TRUST_PRINCIPAL"]
            and row.get("detail") == "on-behalf-of=host-user@example.com"
            for row in rows
        )
        assert not any("forged@example.com" in row.get("detail", "") for row in rows)
        settings.set_many({"embedded_trust_host_auth": "0"})
    print("copied host: lifecycle, bootstrap key, UI 404, cron pair and trust boundary passed")


def test_upgrade_schema_and_profile():
    from sqlalchemy import text

    from app.db import engine

    good = command("upgrade_check.py", "--profile", "base")
    assert good.returncode == 0, good.stdout + good.stderr
    report = json.loads(good.stdout)
    assert "LOC_TMS_TRUST_HEADER_SECRETS" in report["environment_keys"]
    mismatch = command("upgrade_check.py", "--profile", "postgres")
    assert mismatch.returncode != 0, mismatch.stdout
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'stale_host_schema'"))
    stale = command("upgrade_check.py", "--profile", "base")
    assert stale.returncode != 0 and "Installed schema does not match" in stale.stdout
    print("upgrade check: matching schema exits 0; stale schema and wrong profile exit 1")


def test_websocket_trust_headers_scrubbed():
    host = importlib.import_module("host")
    recorded = []

    async def downstream(scope, receive, send):
        recorded.append(scope)

    async def receive():
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        raise AssertionError(message)

    scope = {
        "type": "websocket",
        "path": "/api/projects",
        "headers": [
            (b"X-Loc-Trust-Secret", b"caller-supplied"),
            (b"x-loc-trust-user", b"forged@example.com"),
            (b"host", b"example.com"),
        ],
    }
    with patch.object(host, "product", downstream):
        asyncio.run(host.app(scope, receive, send))
    assert recorded[0]["headers"] == [(b"host", b"example.com")]
    assert len(scope["headers"]) == 3, "caller scope remains intact"


def test_non_ascii_host_configuration_refuses_boot():
    for name in ("HOST_AUTH_TOKEN", "HOST_USER", "LOC_TMS_TRUST_HEADER_SECRET"):
        with patch.dict(
            os.environ,
            {name: "fixture-\u00e9"},  # pregate: allow-secret hermetic non-ASCII boot fixture
        ):
            result = command("-c", "import host")
        assert result.returncode != 0, f"{name} must refuse at import"
        assert f"{name} must contain only ASCII characters before boot" in result.stderr
        assert "UnicodeEncodeError" not in result.stderr


def _origin_main_available():
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "origin/main"], cwd=_ROOT, capture_output=True
        ).returncode
        == 0
    )


def test_readme_copyable_environment_and_secret_scan():
    from scripts.pregate import secret_hits

    path = "examples/embedded-host/README.md"
    source = (_ROOT / path).read_text(encoding="utf-8")
    fence = source.split("```text", 1)[1].split("```", 1)[0]
    assert "pregate:" not in fence
    assert (
        "LOC_TMS_ADMIN_PASSWORD="  # pregate: allow-secret negative assertion on the env key name, no value  # noqa: E501
        not in fence
    )
    assert "12 characters" in source.split("```text", 1)[0]
    if not _origin_main_available():
        print("SKIP README diff secret scan: origin/main is unavailable")
        return
    diff = subprocess.check_output(
        ["git", "diff", "origin/main", "--", path], cwd=_ROOT, text=True, encoding="utf-8"
    )
    assert secret_hits(diff, path) == []


def test_egress_documentation_scope():
    if not _origin_main_available():
        print("SKIP egress documentation scope: origin/main is unavailable")
        return
    path = "app/capabilities/llm.md"
    source = (_ROOT / path).read_text(encoding="utf-8")
    base = subprocess.check_output(
        ["git", "show", f"origin/main:{path}"], cwd=_ROOT, text=True, encoding="utf-8"
    )
    sentence = next(
        line for line in base.splitlines() if line.startswith("writes one `egress.private_target`")
    )
    assert sentence in source.splitlines(), "preserve main's audit wording"
    assert "\n\n## Release identity" in source
    assert "\n\n\n## Release identity" not in source


def main():
    test_host_lifecycle_bootstrap_auth_and_cron()
    test_upgrade_schema_and_profile()
    test_websocket_trust_headers_scrubbed()
    test_non_ascii_host_configuration_refuses_boot()
    test_readme_copyable_environment_and_secret_scan()
    test_egress_documentation_scope()
    print("EMBEDDED HOST EXAMPLE OK")


if __name__ == "__main__":
    main()
