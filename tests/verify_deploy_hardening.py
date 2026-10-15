"""Offline regressions for security review #290, #291, #293 and #294, plus the
red-team audit 2026-10-03 finding 03 (no dev-account seed on a non-loopback bind)."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

init_env("loctms_deploy_hardening_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from starlette.websockets import WebSocketDisconnect

from app import config
from app.db import engine, init_db
from app.main import app
from app.models import Role, User
from app.services import security, settings, vault
from tests._csrf import csrf_for

_SEED = "admin"  # pregate: allow-secret known seed fixture


def check_seed():
    with patch.object(config, "IS_TEST", False):
        assert security.seed_initial()
    admin = security.get_by_email("admin")
    assert admin.password_must_change, "fresh seeded admin must change password"
    with TestClient(app, base_url="http://localhost") as client:
        csrf_for(client)
        result = client.post(
            "/auth/login",
            data={"email": "admin", "password": _SEED},
            follow_redirects=False,
        )
        assert result.status_code == 303
        result = client.get("/users", follow_redirects=False)
        assert "/auth/password-change" in result.headers.get("location", "")
    # Model an existing admin that already completed setup. Upgrade must not reset it.
    with Session(engine) as session:
        row = session.get(User, admin.id)
        row.password_must_change = False
        session.add(row)
        session.commit()
    before = security.get_by_email("admin")
    assert not security.seed_initial()
    after = security.get_by_email("admin")
    assert not after.password_must_change
    assert before.password_hash == after.password_hash
    assert before.id == after.id and security.user_count() == 3
    client = TestClient(app, base_url="http://localhost")
    csrf_for(client)
    client.post("/auth/login", data={"email": "admin", "password": _SEED})
    assert client.get("/users").status_code == 200


def check_hosts():
    with patch.object(config, "IS_TEST", False), patch.object(config, "IS_SERVER", False):
        client = TestClient(app, base_url="http://localhost")
        for host in ("localhost:8000", "127.0.0.1", "[::1]:8000"):
            assert client.get("/auth/login", headers={"Host": host}).status_code == 200
        for path in ("/auth/login", "/users", "/api/projects", "/health", "/ready"):
            result = client.get(path, headers={"Host": "rebind.attacker.example"})
            assert result.status_code == 400, (path, result.status_code)
        result = client.post(
            "/auth/login",
            data={"email": "admin", "password": _SEED},
            headers={"Host": "rebind.attacker.example", "Origin": "http://rebind.attacker.example"},
        )
        assert result.status_code == 400
        try:
            with client.websocket_connect("/ws", headers={"Host": "rebind.attacker.example"}):
                raise AssertionError("foreign websocket Host accepted")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008
        with (
            patch.object(config, "SECURITY_ALLOWED_HOSTS", "machine.tailnet.test"),
            patch.object(config, "APP_BASE_URL", "http://base.tailnet.test"),
        ):
            for host in ("machine.tailnet.test", "base.tailnet.test", "localhost"):
                assert client.get("/auth/login", headers={"Host": host}).status_code == 200
        with patch.object(config, "IS_SERVER", True):
            assert client.get("/auth/login", headers={"Host": "server.test"}).status_code == 200


def check_mode():
    env = os.environ.copy()
    env.pop("LOC_TMS_SERVER", None)
    env.pop("LOC_TMS_TEST_MODE", None)
    env["HOST"] = "0.0.0.0"
    code = "from app import config; assert not config.IS_TEST"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    env["LOC_TMS_TEST_MODE"] = "1"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
    assert result.returncode != 0 and b"loopback HOST" in result.stderr
    with patch.object(config, "HOST", "0.0.0.0"):
        settings.set_many({"security_auth_required": "0"})
        assert security.auth_required(), "public binds always enforce authentication"


# A real (non-test-mode) boot in a throwaway data dir, run as a subprocess because
# only a fresh process can have IS_TEST False at app import. argv[1]: "seed"
# (loopback dev boot must create the 3 dev accounts) or "wall" (non-loopback bind
# must seed nothing and route the first visit to the setup wizard).
_BOOT_CHILD = """
import logging
import sys

msgs = []


class _Cap(logging.Handler):
    def emit(self, record):
        msgs.append(record.getMessage())


logging.getLogger("loc_tms").addHandler(_Cap())
from fastapi.testclient import TestClient

from app.main import app
from app.services import security

with TestClient(app, base_url="http://127.0.0.1:8000") as client:
    probe = client.get("/", follow_redirects=False)
loc = probe.headers.get("location", "")
if sys.argv[1] == "seed":
    assert security.user_count() == 3, security.user_count()
    assert "/auth/setup" not in loc, loc
else:
    assert security.user_count() == 0, security.user_count()
    assert probe.status_code == 303 and "/auth/setup" in loc, (probe.status_code, loc)
    assert security.auth_required(), "login wall must be ON on a public bind"
    assert any("Dev-account seeding skipped" in m for m in msgs), msgs
print("child ok")
"""


def check_dev_seed_bind():
    """Red-team finding 03: plain `run.py` on a NON-loopback HOST must not seed
    admin/linguist/user1 (wall ON + /auth/setup instead); loopback dev still seeds."""
    for host, mode in (("0.0.0.0", "wall"), ("127.0.0.1", "seed")):
        data_dir = tempfile.mkdtemp(prefix="loctms_devseed_")
        env = os.environ.copy()
        env.pop("LOC_TMS_SERVER", None)
        env.pop("LOC_TMS_TEST_MODE", None)
        env["LOC_TMS_DATA_DIR"] = data_dir
        env["HOST"] = host
        try:
            result = subprocess.run(
                [sys.executable, "-c", _BOOT_CHILD, mode],
                env=env,
                capture_output=True,
                timeout=300,
                cwd=_ROOT,
            )
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)
        assert result.returncode == 0, (
            f"boot with HOST={host} ({mode}) failed: "
            f"{result.stderr.decode(errors='replace')[-1500:]}"
        )


def check_storage():
    target = config.DATA_DIR / "private" / "nested"
    opened = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777, **kwargs):
        opened.append((Path(path), flags, mode))
        return real_open(path, flags, mode, **kwargs)

    old_mask = os.umask(0)
    try:
        with patch.object(config, "DATA_DIR", target), patch("os.open", recording_open):
            value, created = config._keyfile("fixture-key", lambda: "fixture-material")
            assert created and value == "fixture-material"
            assert opened and opened[-1][2] == 0o600, "key must be private at creation"
            assert opened[-1][1] & os.O_EXCL
            config.prepare_sqlite_file(f"sqlite:///{target / 'db.sqlite'}")
            assert opened[-1][2] == 0o600
            dbfile = target / "db.sqlite"
            dbfile.write_bytes(b"existing fixture database")
            config.prepare_sqlite_file(f"sqlite:///{dbfile}")
            assert dbfile.read_bytes() == b"existing fixture database"
            Path(str(dbfile) + ".vault").write_bytes(vault._fernet().encrypt(dbfile.read_bytes()))
            dbfile.unlink()
            assert vault.unlock(dbfile)
            assert opened[-1][2] == 0o600
            assert dbfile.read_bytes() == b"existing fixture database"
            if os.name == "posix":
                for path, mode in (
                    (target, 0o700),
                    (target.parent, 0o700),
                    (target / "fixture-key", 0o600),
                    (target / "db.sqlite", 0o600),
                ):
                    assert path.stat().st_mode & 0o777 == mode
    finally:
        os.umask(old_mask)


def check_compose():
    import ipaddress

    import yaml

    base = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    companion = yaml.safe_load((_ROOT / "deploy/compose-proxy.yml").read_text())
    services = base["services"]
    assert ":?" in services["db"]["environment"]["POSTGRES_PASSWORD"]
    env = companion["services"]["app"]["environment"]
    assert env["LOC_TMS_SECURE_COOKIES"] == "1"
    address = services["proxy"]["networks"]["default"]["ipv4_address"]
    assert env["LOC_TMS_TRUSTED_PROXY_IPS"] == address
    pool = base["networks"]["default"]["ipam"]["config"][0]
    assert ipaddress.ip_address(address) not in ipaddress.ip_network(pool["ip_range"])
    assert "LOC_TMS_PROXY_CONFIGURED" in services["proxy"]["command"][0]
    assert companion["services"]["proxy"]["environment"]["LOC_TMS_PROXY_CONFIGURED"] == "1"


def check_linguist_backfill():
    """Gate round 1 MEDIUM-1: the boot-time linguist backfill (well-known password) must
    force a change on a real install, like the seeded admin."""
    with Session(engine) as session:
        for row in session.exec(select(User).where(User.role == Role.LINGUIST)).all():
            session.delete(row)
        session.commit()
    with patch.object(config, "IS_TEST", False):
        assert security.ensure_linguist()
    linguist = security.get_by_email("linguist")
    assert linguist is not None and linguist.password_must_change, "backfilled linguist"


def main():
    init_db()
    failures = []
    for check in (
        check_seed,
        check_linguist_backfill,
        check_hosts,
        check_mode,
        check_dev_seed_bind,
        check_storage,
        check_compose,
    ):
        try:
            check()
            print(check.__name__, "PASS")
        except Exception as exc:
            failures.append(check.__name__)
            print(check.__name__, "FAIL", repr(exc))
    assert not failures, failures
    print("DEPLOY HARDENING OK")


if __name__ == "__main__":
    main()
