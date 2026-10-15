"""Docker / server-mode auth verification (offline) — Option A.

Regression guard for the fixed "container boots with auth OFF" bug. A real
container legitimately sets LOC_TMS_DATA_DIR=/data for its volume; that used to
be misread as "test mode" (no login wall, no seeded admin, no scheduler), leaving
a deployed container wide open.

The fix: an explicit LOC_TMS_SERVER=1 signal (set by the Dockerfile + compose)
declares a real server install. This suite simulates that container env — a
THROWAWAY LOC_TMS_DATA_DIR plus LOC_TMS_SERVER=1 — and asserts Option A:

  * config classifies it as a server install, NOT a test run
  * startup turns the login wall ON but seeds NO dev accounts
  * an anonymous visit is redirected to /auth/setup (the first-run wizard)
  * /auth/setup is reachable and creates the admin
  * after setup the wall is enforced (admin authed; logout -> login)

#414 (first-run setup secret and race):
  * startup prints a one-time setup token to the log and DATA_DIR/.setup_token;
    a setup POST without it, or with a wrong one, is refused 403 and creates nothing
  * concurrent valid setup POSTs create exactly ONE admin; the rest go to /auth/login
  * the token file is removed once the admin exists
  * create_user(first_account_only=True) and seed_admin refuse once an account exists

Hermetic: temp data dir + cleared provider keys, never touches the real DB.
ASCII-only prints (the cp1252 console throws on non-ASCII).
"""

import logging
import os
import sys
import threading
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

# Simulate a container: throwaway data dir (the volume) PLUS the server signal.
init_env("loctms_docker_")
os.environ["LOC_TMS_SERVER"] = "1"

from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.services import security, settings
from tests._csrf import csrf_for, sign_out

SETUP_PW = "adminpass-12345"  # pregate: allow-secret throwaway fixture password
RACERS = 10


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _setup_post(client, email, setup_token=None, password=SETUP_PW):
    data = {"email": email, "name": "Admin", "password": password}
    if setup_token is not None:
        data["setup_token"] = setup_token
    return client.post("/auth/setup", data=data, follow_redirects=False)


def _race(setup_token):
    """#414: RACERS clients each load the wizard, wait on one barrier, then post a valid
    setup at the same moment. Returns [(client, email, response), ...]."""
    clients = [TestClient(app) for _ in range(RACERS)]
    for client in clients:
        assert client.get("/auth/setup").status_code == 200
        csrf_for(client)
    barrier = threading.Barrier(RACERS)
    results = [None] * RACERS

    def go(i):
        email = f"racer{i}@x.com"
        barrier.wait()
        results[i] = (clients[i], email, _setup_post(clients[i], email, setup_token))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(RACERS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(120)
    assert all(results), "a racer did not finish"
    return results


def main() -> None:
    # Config must read the container env as a server install, not a test run.
    assert config.IS_SERVER is True, "LOC_TMS_SERVER=1 should mark a server install"
    assert config.IS_TEST is False, "a server install must NOT be treated as test mode"
    print("  container env -> IS_SERVER, not IS_TEST")

    with patch.object(config, "IS_SERVER", False), patch.object(config, "HOST", "127.0.0.1"):
        assert not security.setup_token_required(), "local single-user mode stays unchanged"
        with patch.object(config, "HOST", "0.0.0.0"):
            assert security.setup_token_required(), "a public bind also needs the setup token"
        with patch.object(config, "IS_TEST", True):
            assert security.setup_token_ok(""), "throwaway verify installs need no token"

    capture = _Capture()
    logging.getLogger("loc_tms").addHandler(capture)
    token_file = config.DATA_DIR / ".setup_token"

    # TestClient as a context manager fires the app startup event (_startup),
    # which is where the login-wall / seeding / scheduler gate lives.
    with TestClient(app) as c:
        # Wall ON, but NO dev accounts seeded (unlike local dev's seed_initial()).
        assert security.auth_required() is True, "server install must enable the login wall"
        assert security.user_count() == 0, "server install must NOT seed dev accounts"
        print("  startup: login wall ON, zero seeded accounts")

        # An anonymous visit is walled off and pushed to the first-run wizard.
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/auth/setup", r.headers
        assert c.get("/auth/setup").status_code == 200, "setup wizard must be reachable"
        print("  anonymous visit -> redirected to reachable /auth/setup")

        # #414: startup generated a one-time setup token, printed it to the log and kept
        # it in DATA_DIR/.setup_token; the wizard asks for it.
        assert token_file.is_file(), "a server install with no account needs a setup token"
        setup_token = token_file.read_text(encoding="ascii").strip()
        assert len(setup_token) >= security.SETUP_MIN_LEN, "setup token too short"
        token_file.unlink()
        assert not security.setup_token_ok(setup_token), "missing token file must fail closed"
        assert not token_file.exists(), "only startup may generate a replacement"
        security.prepare_setup_token_at_boot()
        replacement = token_file.read_text(encoding="ascii").strip()
        assert len(replacement) >= security.SETUP_MIN_LEN and replacement != setup_token
        setup_token = replacement
        with patch.dict(os.environ, {security.SETUP_ENV_VAR: "e" * 32}):
            assert security.setup_token_ok("e" * 32), "the operator environment value wins"
            assert not security.setup_token_ok(setup_token), (
                "file must not override the environment"
            )
        assert any(setup_token in line for line in capture.lines), "token not in the log"
        # A later start (or a second worker) must not print the token in full again.
        capture.lines.clear()
        security.prepare_setup_token_at_boot()
        assert not any(setup_token in line for line in capture.lines), "token re-logged"
        assert any(str(token_file) in line for line in capture.lines), "no pointer to the file"
        assert token_file.read_text(encoding="ascii").strip() == setup_token
        # A crash between create and write leaves an empty file: boot must replace it.
        token_file.write_bytes(b"")
        security.prepare_setup_token_at_boot()
        setup_token = token_file.read_text(encoding="ascii").strip()
        assert len(setup_token) >= security.SETUP_MIN_LEN, "empty token file not replaced"
        assert any(setup_token in line for line in capture.lines), "new token not announced"
        assert 'name="setup_token"' in c.get("/auth/setup").text, "wizard lacks the token field"
        print("  startup: one-time setup token in the log and DATA_DIR/.setup_token")

        # Auth is on (server install), so the setup POST carries the CSRF token
        # minted by the wizard render (decoded from the session cookie).
        csrf_for(c)
        # #414: no token, or a wrong one, is refused and creates nothing.
        r = _setup_post(c, "admin@x.com")
        assert r.status_code == 403, ("setup without the token must be refused", r.status_code)
        r = _setup_post(c, "admin@x.com", setup_token="x" * len(setup_token))
        assert r.status_code == 403, ("setup with a wrong token must be refused", r.status_code)
        assert security.user_count() == 0, "a refused setup must not create an account"
        print("  setup POST without / with a wrong setup token -> 403, no account")

        # The wizard rejects weak passwords (with the token).
        r = _setup_post(c, "admin@x.com", setup_token, password="short")
        assert r.status_code == 400, "short setup password should be rejected"

        # #414: concurrent valid posts create exactly ONE admin.
        results = _race(setup_token)
        assert security.user_count() == 1, (
            f"concurrent setup created {security.user_count()} accounts, expected exactly 1"
        )
        won = [x for x in results if x[2].headers.get("location") == "/"]
        lost = [x for x in results if x[2].headers.get("location") == "/auth/login"]
        assert len(won) == 1 and len(lost) == RACERS - 1, [
            (x[2].status_code, x[2].headers.get("location")) for x in results
        ]
        winner, winner_email, _r = won[0]
        assert security.get_by_email(winner_email) is not None
        assert winner.get("/").status_code == 200, "the one admin should now be authed"
        loser = lost[0][0]
        assert loser.get("/", follow_redirects=False).status_code == 303, "a loser got in"
        assert not token_file.exists(), "the setup token must be removed once an account exists"
        assert not security.setup_token_ok(setup_token)
        assert not token_file.exists(), "a late validation must not recreate a consumed token"
        # A loser can pass the first empty-install probe just before the winner
        # consumes the token. It must reach sign-in, not report a bad credential.
        from starlette.requests import Request

        from app.web.auth import auth_setup_post

        with (
            patch.object(security, "has_users", side_effect=[False, True]),
            patch.object(security, "setup_token_ok", return_value=False),
        ):
            late = auth_setup_post(
                Request({"type": "http"}), "late@x.com", "Late", SETUP_PW, setup_token
            )
        assert late.status_code == 303 and late.headers["location"] == "/auth/login"
        print(f"  {RACERS} concurrent setup posts -> exactly 1 admin, the rest sent to sign-in")

        # #414: the atomic guard itself, for the other first-account caller (init).
        for email in ("late@x.com", winner_email):
            try:
                security.create_user(
                    email,
                    "Late",
                    security.Role.ADMIN,
                    SETUP_PW,
                    allow_orgless_boot=True,
                    first_account_only=True,
                )
            except security.FirstAccountExists:
                pass
            else:
                raise AssertionError("first_account_only must refuse once an account exists")
        assert security.seed_admin("late@x.com", SETUP_PW)[2] == "exists"
        assert security.user_count() == 1
        print("  create_user(first_account_only) and seed_admin refuse a second first account")

        # After setup the wall is still enforced: a fresh (logged-out) client
        # can no longer reach the app, and setup is now closed.
        sign_out(winner, follow_redirects=False)
        anon = TestClient(app)
        r = anon.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login"), r.headers
        r = anon.get("/auth/setup", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/auth/login", (
            "setup must close once an admin exists"
        )
        print("  wall enforced after setup; /auth/setup closed")

    # Leave global state clean for any other suite sharing the interpreter.
    logging.getLogger("loc_tms").removeHandler(capture)
    settings.set_many({"security_auth_required": "0"})
    print("DOCKER AUTH OK")


if __name__ == "__main__":
    main()
