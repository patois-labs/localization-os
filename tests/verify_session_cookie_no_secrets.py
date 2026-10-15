"""#476 (apiauth-05): a new API key and an invite link never ride in the session cookie.

THE BUG: POST /account/apikeys put the full new key into request.session and redirected;
the Starlette session cookie is signed, not encrypted, so the key sat in plain base64 in
the Set-Cookie header (proxy logs, HAR files, cookie stores) and stayed recoverable from
any copy of that cookie. The admin invite link (it carries the invite token) was stashed
the same way when no mail server is configured.

Pinned (auth ON):
  1. After POST /account/apikeys the session cookie holds no key material (no "lsu_"),
     the next GET /account shows the key exactly once, the key works, a second GET does
     not show it again.
  2. A reference whose value is gone (expired / restart) renders the "can no longer be
     shown" notice instead of a key.
  3. After an admin invite with no SMTP, the session cookie holds no invite token, and
     the /users page still shows the link once.

Final line: "SESSION COOKIE NO SECRETS OK".
"""

import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cookienosecret_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.models import Role
from app.services import security, settings
from tests._csrf import csrf_for, session_data

init_db()

_PW = "cookie-fixture-pass-1"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _login(c, who):
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": who, "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    if r.status_code != 303:
        _fail(f"login {who} failed: {r.status_code}")
    csrf_for(c)


def _cookie_text(c):
    return json.dumps(session_data(c)) + " " + (c.cookies.get("session") or "")


def main():
    security.create_user("cookie-t@x.com", "T", Role.TRANSLATOR, _PW)
    security.create_user("cookie-a@x.com", "A", Role.ADMIN, _PW)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        _login(c, "cookie-t@x.com")
        r = c.post("/account/apikeys", data={"name": "cookie", "docs": "1"}, follow_redirects=False)
        if r.status_code != 303:
            _fail(f"key create must 303, got {r.status_code}")
        if "lsu_" in _cookie_text(c):
            _fail("the session cookie carries the new API key in plaintext")
        page = c.get("/account").text
        m = re.search(r'aria-label="New API key" value="([^"]+)"', page)
        if not m or not m.group(1).startswith("lsu_"):
            _fail("GET /account must show the new key once")
        key = m.group(1)
        r = c.get("/api/account", headers={"X-Loc-Token": key})
        if r.status_code != 200:
            _fail(f"the shown key must work, got {r.status_code}")
        if key in c.get("/account").text:
            _fail("the key must not be shown a second time")
        print("  1. new key: no key material in the cookie, shown once, works: OK")

        # 2. the reference survives but its value is gone (restart / expiry)
        c.post("/account/apikeys", data={"name": "lost"}, follow_redirects=False)
        from app.web import one_time_stash

        one_time_stash._entries.clear()
        page = c.get("/account").text
        if "can no longer be shown" not in page or 'aria-label="New API key"' in page:
            _fail("a lost reference must render the notice, not a key")
        print("  2. lost reference renders the notice: OK")

        a = TestClient(app)
        _login(a, "cookie-a@x.com")
        r = a.post(
            "/users/invite",
            data={"email": "newbie@x.com", "role": Role.TRANSLATOR},
            follow_redirects=False,
        )
        if r.status_code != 303:
            _fail(f"invite must 303, got {r.status_code}")
        if "/auth/accept/" in _cookie_text(a):
            _fail("the session cookie carries the invite link (and its token)")
        if "/auth/accept/" not in a.get(r.headers["location"]).text:
            _fail("the /users page must still show the invite link once")
        print("  3. invite link: not in the cookie, shown once on /users: OK")
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("SESSION COOKIE NO SECRETS OK")


if __name__ == "__main__":
    main()
