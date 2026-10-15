"""Password policy for local accounts (HECVAT AAAI-03). Offline, hermetic.

Proves, with real requests through the app and the in-process CLI:

  R  every rule of app/services/password_policy.py::issue: minimum and maximum length,
     spaces and Unicode allowed, required character classes (a caseless script counts as
     lowercase), the bundled common list (case-insensitive), the full email, the username
     with only digits/symbols added, the product/company name, reuse history, and one
     plain sentence naming the broken rule;
  S  the five settings: defaults (NIST), one validator each, refused on every door (the
     registry, CLI config set, POST /api/admin/config, the web Security page's Passwords
     card with its field-linked error), accepted values audit-logged with before/after,
     never masked as secrets, and the NOT_SECRET_KEYS copies (settings, CLI, MCP server)
     identical so MCP manage_install set_config can carry them;
  D  every door calls the one authority with the account context it has: web setup,
     invite accept, account change (error linked to the new-password field, escaped),
     admin create (web /users, CLI user create, POST /api/users), admin reset (CLI
     user reset-password, POST /api/users/{uid}/reset-password) and CLI init;
  F  the forced change: an admin reset or password_max_age_days sends a password sign-in
     to /auth/password-change, every other page redirects there until the change
     succeeds, the change refuses reuse, clears the flag and restarts the clock;
  G  GDPR export carries no password hashes; erase deletes the history rows;
  X  SSO/LDAP accounts (no local password) are unaffected, and the dev seeders bypass;
  M  the alembic revision e5a1c8d3f7b2 upgrades (with the backfill) and downgrades.

Prints ASCII only; ends with PASSWORD POLICY OK.
"""

import ast
import html
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_pwpolicy_")

import logging

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, PasswordHistory, Role, User
from app.services import apikeys, gdpr, password_policy, security, settings
from tests._cli_runner import run_cli
from tests._csrf import record_message, session_data, sign_out

ADMIN = "admin@pwpolicy.test"
ADMIN_PW = "orchard lantern river 2026"  # pregate: allow-secret throwaway fixture login
GOOD = "violet harbor thunder 77"  # pregate: allow-secret throwaway fixture password
# Fixture passwords the rules must refuse (and one admin-reset value), named once.
PW_SHORT = "short"  # pregate: allow-secret test fixture
PW_USERNAME = "Zorblax 2026 !!!!"  # pregate: allow-secret test fixture
PW_EMAIL = "invitee@pwpolicy.test 1"  # pregate: allow-secret test fixture
PW_RESET = "cobalt summit 808"  # pregate: allow-secret test fixture
# The five policy setting keys, named once (password_policy.KEYS is their order).
MIN_LEN, CLASSES, BLOCK, HISTORY, MAX_AGE = password_policy.KEYS


def _common_entry() -> str:
    """A real list entry of at least 12 characters (so only the common rule refuses it)."""
    for line in password_policy._COMMON_FILE.read_bytes().decode("utf-8").splitlines():
        if len(line) >= 12:
            return line
    raise AssertionError("the common list has no entry of 12+ characters")


def last_audit(action: str):
    with Session(engine) as s:
        return s.exec(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc())
        ).first()


def user_row(email: str) -> User:
    with Session(engine) as s:
        return s.exec(select(User).where(User.email == email)).first()


def csrf(c) -> str:
    tok = session_data(c).get("csrf")
    if not tok:
        c.get("/account")
        tok = session_data(c).get("csrf") or ""
    assert tok, "no CSRF token"
    return tok


def login(email: str, pw: str, nxt: str = "/"):
    c = TestClient(app)
    c.get("/auth/login")
    pre = session_data(c).get("csrf")
    r = c.post(
        "/auth/login",
        data={"email": email, "password": pw, "next": nxt, "csrf_token": pre},
        follow_redirects=False,
    )
    return c, r


# --------------------------------------------------------------------------------------- R
def check_rules():
    pol = password_policy.Policy()
    assert pol == password_policy.current(), "defaults must be the NIST defaults"
    assert (pol.min_length, pol.require_classes, pol.block_common) == (12, 0, True)
    assert (pol.history, pol.max_age_days) == (5, 0)
    issue = password_policy.issue
    assert issue("short") == "Password must be at least 12 characters."
    assert issue("x" * 1025).startswith("Password must be at most 1024")
    assert issue(" " * 20) == "Password cannot be only spaces."
    assert issue("correct horse battery staple") is None, "spaces must be allowed"
    assert issue("水火風雷山川花鳥風月雪晴") is None
    common = _common_entry()
    for variant in (common, common.upper(), common.title()):
        assert "too common" in (issue(variant) or ""), variant
    assert "email address" in issue("x jane.doe@example.org x", user="jane.doe@example.org")
    assert "username" in issue("JaneDoe 2026!!!!", user="jane.doe@example.org")
    # the username inside a longer passphrase is not the username "with digits added"
    assert issue("jane loves violet harbors", user="jane@example.org") is None
    brand = str(settings.get("brand_name") or "Localization OS")
    assert "product or company name" in issue(f"my {brand} pass 1"), "brand name"
    settings.set_many({"org_name": "Acme University"})
    # only the FULL organization name (spaces and case ignored), never one of its words
    assert "product or company name" in issue("I love AcmeUniversity 26")
    assert "product or company name" in issue("acme university rocks!")
    assert issue("go acme go state 123456") is None, "a single org word must not be refused"
    assert issue("localization team 2026") is None, "a single product word must not be refused"
    settings.set_many({"org_name": ""})
    # the common rule also matches a list word with digits/symbols added, repeats and runs
    weak = [
        "password1234",
        "qwertyuiop12",
        "iloveyou2024!",
        "Password123!",
        "monkey123456",
        "trustno1trustno1",
        "123456789012",
        "aaaaaaaaaaaa",
        "asdfghjkl123",
    ]
    for w in weak:
        assert "too common" in (issue(w) or ""), w
    settings.set_many({CLASSES: "3"})
    msg = issue("lowercase only words")
    assert msg.startswith("Password must use at least 3 of these") and "It uses 2" in msg, msg
    assert issue("Upper lower 12 words") is None
    # a caseless script counts as lowercase: kanji + digit + symbol = 3 classes
    assert issue("水火風雷山川 2026 !!") is None
    settings.set_many({CLASSES: "0", BLOCK: "0"})
    assert issue(common) is None, "block_common off must allow a common password"
    settings.set_many({BLOCK: "1", MIN_LEN: "20"})
    assert issue("seventeen chars!!") == "Password must be at least 20 characters."
    settings.set_many({MIN_LEN: "12"})
    # garbage stored value reads as the default (the write doors refuse garbage anyway)
    with Session(engine) as s:
        from app.models import Setting

        row = s.get(Setting, "password_history")
        if row is None:
            row = Setting(key="password_history", value="abc")
        row.value = "abc"
        s.add(row)
        s.commit()
    settings.invalidate_cache()
    assert password_policy.current().history == 5
    settings.set_many({HISTORY: "5"})
    summary = password_policy.summary()
    assert "At least 12 characters" in summary and "last 5 passwords" in summary
    print(
        "  rules: length, spaces/Unicode, classes, common list, email/username/brand, one sentence"
    )


# --------------------------------------------------------------------------------------- S
def _mcp_not_secret() -> frozenset:
    with open("integrations/mcp-server/server.py", encoding="utf-8") as fh:
        src = fh.read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_NOT_SECRET_KEYS" for t in node.targets
        ):
            return frozenset(ast.literal_eval(node.value.args[0]))
    raise AssertionError("_NOT_SECRET_KEYS missing from the MCP server")


BAD = {
    MIN_LEN: ["7", "129", "abc", "-1", "", "12.5"],
    CLASSES: ["5", "-1", "x"],
    BLOCK: ["2", "yes", ""],
    HISTORY: ["25", "-1", "a"],
    MAX_AGE: ["3651", "-5", "ten"],
}
GOODV = {
    MIN_LEN: ["8", "128", "14"],
    CLASSES: ["0", "4"],
    BLOCK: ["0", "1"],
    HISTORY: ["0", "24"],
    MAX_AGE: ["0", "90", "3650"],
}


def check_settings(admin_client):
    from app.cli import _common as cli_common

    keys = set(password_policy.KEYS)
    assert keys == set(settings.NOT_SECRET_KEYS) == set(cli_common.NOT_SECRET_KEYS)
    assert keys == _mcp_not_secret(), "MCP server copy drifted"
    for k in keys:
        assert k in settings.DEFAULTS and k in settings.VALUE_VALIDATORS, k
        assert not settings.is_secret_key(k) and not cli_common.is_secret_key(k), k
        assert settings.display_value(k) == settings.DEFAULTS[k], k
    # the broad masking rule still covers real secrets
    assert settings.is_secret_key("smtp_password") and cli_common.is_secret_key("smtp_password")
    # registry
    for k, bads in BAD.items():
        for v in bads:
            try:
                settings.validate_value(k, v)
            except settings.InvalidSettingValue:
                continue
            raise AssertionError(f"{k} accepted {v!r}")
        for v in GOODV[k]:
            settings.validate_value(k, v)
    # CLI door
    for k, bads in BAD.items():
        before = settings.get(k)
        code, _o, _e = run_cli(["config", "set", k, bads[0]])
        assert code != 0 and settings.get(k) == before, (k, code)
    # #280: the password policy keys are security-sensitive, so a valid write confirms.
    code, out, _e = run_cli(["config", "set", "password_history", "7", "--yes"])
    assert code == 0 and settings.get("password_history") == "7", (code, _e)
    assert "7" in out, "a policy value is not a secret and is shown"
    row = last_audit("config.set")
    assert row.target == "password_history" and "= 7" in row.detail, row.detail
    code, out, _e = run_cli(["config", "get", "password_history"])
    assert code == 0 and "7" in out and "(set)" not in out, out
    run_cli(["config", "set", "password_history", "5", "--yes"])
    # /api door (remote admin on, an admin-scoped key)
    settings.set_many({"remote_admin_enabled": "1"})
    settings.invalidate_cache()
    admin = user_row(ADMIN)
    _row, raw = apikeys.create_key(admin.id, "pwpolicy", scopes="api docs admin")
    h = {"X-Loc-Token": raw}
    api = TestClient(app)
    for k, bads in BAD.items():
        before = settings.get(k)
        r = api.post("/api/admin/config", json={"settings": {k: bads[0]}}, headers=h)
        assert r.status_code == 400 and settings.get(k) == before, (k, r.status_code, r.text)
    r = api.post("/api/admin/config", json={"settings": {MAX_AGE: "30"}}, headers=h)
    assert r.status_code == 400 and "confirm" in r.text, "#280: the policy keys confirm"
    r = api.post(
        "/api/admin/config", json={"settings": {MAX_AGE: "30"}, "confirm": [MAX_AGE]}, headers=h
    )
    assert r.status_code == 200 and settings.get("password_max_age_days") == "30", r.text
    got = api.get("/api/admin/config", headers=h).json()["settings"]
    assert got.get("password_max_age_days") == "30", "the /api read must not mask a policy key"
    api.post("/api/admin/config", json={"settings": {MAX_AGE: "0"}, "confirm": True}, headers=h)
    # web door: the Passwords card
    tok = csrf(admin_client)
    page = admin_client.get("/security").text
    assert 'data-card="passwords"' in page and 'id="passwords"' in page
    for fid in (
        "password-min-length",
        "password-require-classes",
        "password-history",
        "password-max-age-days",
        "password-block-common",
    ):
        assert f'id="{fid}"' in page, fid
    base = {
        "security_auth_required": "1",
        "audit_enabled": "1",
        "csrf_token": tok,
        "pwpolicy_card_sent": "1",
        BLOCK: "1",
    }
    codes = {
        MIN_LEN: "pw_min_length",
        CLASSES: "pw_require_classes",
        HISTORY: "pw_history",
        MAX_AGE: "pw_max_age",
    }
    for k, code in codes.items():
        before = settings.get(k)
        r = admin_client.post("/security", data={**base, k: BAD[k][0]}, follow_redirects=False)
        assert r.headers["location"] == f"/security?err={code}", r.headers["location"]
        assert settings.get(k) == before, f"a refused save wrote {k}"
    r = admin_client.post("/security", data={**base, BLOCK: "maybe"}, follow_redirects=False)
    assert r.headers["location"] == "/security?err=pw_block_common"
    err_page = admin_client.get("/security?err=pw_min_length").text
    assert 'id="password-min-length"' in err_page
    frag = err_page.split('id="password-min-length"', 1)[1].split("/>", 1)[0]
    assert 'aria-invalid="true"' in frag, "the refused field is not marked invalid"
    assert 'data-error-field="password-min-length"' in err_page, "summary does not link the field"
    r = admin_client.post(
        "/security",
        data={**base, MIN_LEN: "14", CLASSES: "2"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/security?saved=1", r.headers["location"]
    assert settings.get("password_min_length") == "14"
    row = last_audit("security.update")
    assert "password_min_length old=12 new=14" in row.detail, row.detail
    assert "password_require_classes old=0 new=2" in row.detail, row.detail
    # unchecking the box with the card present turns the rule off; an omitted card changes nothing
    base_off = {k: v for k, v in base.items() if k != "password_block_common"}
    admin_client.post("/security", data=base_off, follow_redirects=False)
    assert settings.get("password_block_common") == "0"
    admin_client.post(
        "/security",
        data={"security_auth_required": "1", "audit_enabled": "1", "csrf_token": tok},
        follow_redirects=False,
    )
    assert settings.get("password_min_length") == "14", "an omitted card must not reset"
    settings.set_many({MIN_LEN: "12", CLASSES: "0", BLOCK: "1"})
    print(
        "  settings: 5 keys, one validator each, refused on registry/CLI/api/web; audited;"
        " not secret"
    )


# --------------------------------------------------------------------------------------- D
def check_setup_door():
    c = TestClient(app)
    assert security.user_count() == 0
    r = c.post("/auth/setup", data={"email": ADMIN, "name": "Admin", "password": PW_SHORT})
    assert r.status_code == 400 and "at least 12 characters" in r.text
    assert 'id="setup-password-rules"' in c.get("/auth/setup").text
    # "admin" alone is a common-list word, so the username case uses an uncommon name
    r = c.post(
        "/auth/setup",
        data={"email": "zorblax@pwpolicy.test", "name": "Z", "password": PW_USERNAME},
    )
    assert r.status_code == 400 and "your username" in r.text, r.text[:300]
    assert 'aria-invalid="true"' in r.text.split('id="setup-password"', 1)[1].split(">", 1)[0]
    assert security.user_count() == 0
    r = c.post(
        "/auth/setup",
        data={"email": ADMIN, "name": "Admin", "password": ADMIN_PW},
        follow_redirects=False,
    )
    assert r.status_code == 303 and security.user_count() == 1, r.status_code
    u = user_row(ADMIN)
    assert u.password_changed_at is not None and not u.password_must_change
    print("  door: web setup (short + username refused with the field marked, then created)")


def check_invite_door():
    inv = security.create_invite("invitee@pwpolicy.test", Role.TRANSLATOR, ADMIN)
    c = TestClient(app)
    page = c.get(f"/auth/accept/{inv.token}").text
    assert 'id="accept-password-rules"' in page and 'minlength="12"' in page
    tok = session_data(c).get("csrf")
    common = _common_entry()
    r = c.post(
        f"/auth/accept/{inv.token}",
        data={"name": "<b>Inv</b>", "password": common, "confirm": common, "csrf_token": tok},
    )
    assert r.status_code == 400 and "too common" in r.text, r.text[:300]
    assert "<b>Inv</b>" not in r.text and html.escape("<b>Inv</b>") in r.text, "name not escaped"
    r = c.post(
        f"/auth/accept/{inv.token}",
        data={
            "name": "Inv",
            "password": PW_EMAIL,
            "confirm": PW_EMAIL,
            "csrf_token": tok,
        },
    )
    assert r.status_code == 400 and "email address" in r.text
    r = c.post(
        f"/auth/accept/{inv.token}",
        data={"name": "Inv", "password": GOOD, "confirm": GOOD, "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "invitee@pwpolicy.test")).first()
        hist = s.exec(select(PasswordHistory).where(PasswordHistory.user_id == u.id)).all()
    assert len(hist) == 1 and hist[0].password_hash.startswith("$argon2id$")
    assert GOOD not in hist[0].password_hash
    print("  door: invite accept (common + email refused, escaped echo, history row as argon2id)")


def check_account_door():
    c, r = login("invitee@pwpolicy.test", GOOD)
    assert r.status_code == 303 and r.headers["location"] == "/", r.headers["location"]
    tok = csrf(c)
    page = c.get("/account").text
    assert 'id="account-password-rules"' in page
    r = c.post(
        "/account/password",
        data={"current": GOOD, "new": GOOD, "confirm": GOOD, "csrf_token": tok},
        follow_redirects=False,
    )
    loc = r.headers["location"]
    assert loc.startswith("/account?err=") and "different%20from%20your%20current" in loc, loc
    page = c.get(loc).text
    assert "Choose a password different from your current one." in page
    frag = page.split('id="account-new-password"', 1)[1].split("/>", 1)[0]
    assert 'aria-invalid="true"' in frag, "the new-password field is not marked invalid"
    r = c.post(
        "/account/password",
        data={
            "current": "wrong wrong wrong",
            "new": "x" * 14,
            "confirm": "x" * 14,
            "csrf_token": tok,
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/account?saved=pwfail"
    # free text reaching the page is escaped, never markup
    # (security review 2026-09, #248: free text shows only once recorded for the session)
    record_message(c, "<script>alert(1)</script>")
    page = c.get("/account?err=%3Cscript%3Ealert(1)%3C/script%3E").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    new = "amber meadow falcon 31"
    r = c.post(
        "/account/password",
        data={"current": GOOD, "new": new, "confirm": new, "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/account?saved=pw", r.headers["location"]
    row = last_audit("account.change_password")
    assert row.detail == "ok", row.detail
    # history: back to the first password is refused, at every depth up to the setting
    ok, msg = security.change_password(user_row("invitee@pwpolicy.test").id, new, GOOD)
    assert not ok and "last 5 passwords" in msg
    settings.set_many({HISTORY: "1"})
    ok, msg = security.change_password(user_row("invitee@pwpolicy.test").id, new, GOOD)
    assert ok, msg
    with Session(engine) as s:
        n = len(
            s.exec(
                select(PasswordHistory).where(
                    PasswordHistory.user_id == user_row("invitee@pwpolicy.test").id
                )
            ).all()
        )
    assert n == 1, f"history not pruned to the setting ({n})"
    settings.set_many({HISTORY: "5"})
    print("  door: account change (reuse refused + field marked, escaped, pruned history)")


def check_admin_doors(admin_client):
    # web /users create
    tok = csrf(admin_client)
    r = admin_client.post(
        "/users",
        data={
            "email": "webmade@pwpolicy.test",
            "name": "W",
            "role": "translator",
            "password": PW_SHORT,
            "csrf_token": tok,
        },
        follow_redirects=False,
    )
    assert "error=" in r.headers["location"] and user_row("webmade@pwpolicy.test") is None
    # CLI user create
    code, _o, err = run_cli(
        [
            "user",
            "create",
            "--email",
            "climade@pwpolicy.test",
            "--role",
            "translator",
            "--password",
            "tiny",
        ]
    )
    assert code != 0 and "at least 12" in err and user_row("climade@pwpolicy.test") is None
    code, _o, err = run_cli(
        [
            "user",
            "create",
            "--email",
            "climade@pwpolicy.test",
            "--role",
            "translator",
            "--password",
            GOOD,
        ]
    )
    assert code == 0, err
    # CLI reset-password. An admin reset never checks the target's current or previous
    # passwords (that answer would be a guessing oracle): resetting to the CURRENT password
    # succeeds and still forces a change at the next sign-in.
    code, out, err = run_cli(
        ["user", "reset-password", "climade@pwpolicy.test", "--password", GOOD]
    )
    assert code == 0, (out, err)
    assert "current" not in err and "last 5" not in err
    assert user_row("climade@pwpolicy.test").password_must_change
    # the other rules still apply to an admin reset
    code, _o, err = run_cli(
        ["user", "reset-password", "climade@pwpolicy.test", "--password", "password1234"]
    )
    assert code != 0 and "too common" in err, err
    code, out, err = run_cli(
        ["user", "reset-password", "climade@pwpolicy.test", "--password", "temporary granite 55"]
    )
    assert code == 0 and "next sign-in" in out, (out, err)
    assert user_row("climade@pwpolicy.test").password_must_change
    row = last_audit("user.password_reset")
    assert row.target == "climade@pwpolicy.test" and "next sign-in" in row.detail
    # /api create + reset
    admin = user_row(ADMIN)
    _row, raw = apikeys.create_key(admin.id, "pwpolicy-users", scopes="api docs admin")
    h = {"X-Loc-Token": raw}
    api = TestClient(app)
    r = api.post(
        "/api/users",
        json={"email": "apimade@pwpolicy.test", "role": "translator", "password": _common_entry()},
        headers=h,
    )
    assert r.status_code == 400 and "too common" in r.json()["error"], r.text
    r = api.post(
        "/api/users",
        json={"email": "apimade@pwpolicy.test", "role": "translator", "password": GOOD},
        headers=h,
    )
    assert r.status_code in (200, 201), r.text
    uid = user_row("apimade@pwpolicy.test").id
    r = api.post(f"/api/users/{uid}/reset-password", json={"password": PW_SHORT}, headers=h)
    assert r.status_code == 400 and "at least 12" in r.json()["error"], r.text
    r = api.post(f"/api/users/{uid}/reset-password", json={"password": PW_RESET}, headers=h)
    assert r.status_code == 200 and set(r.json()) == {"ok", "email"}, r.text
    assert user_row("apimade@pwpolicy.test").password_must_change, "reset must force a change"
    # /api reset to the account's current (and to a previous) password: no oracle
    for guess in (PW_RESET, GOOD):
        r = api.post(f"/api/users/{uid}/reset-password", json={"password": guess}, headers=h)
        assert r.status_code == 200, (guess, r.text)
    # CLI init / env seeding: seed_admin no-ops once a user exists, so pin that it runs the
    # same authority (with the admin email as context) before creating anything.
    import inspect

    seed_src = inspect.getsource(security.seed_admin)
    assert "password_issue(password, user=email)" in seed_src, "seed_admin skips the policy"
    print("  door: admin create (web, CLI, /api) and admin reset (CLI, /api) refused + must_change")


# --------------------------------------------------------------------------------------- F
def check_forced_change():
    c, r = login("climade@pwpolicy.test", "temporary granite 55", nxt="/projects")
    assert r.status_code == 303, r.status_code
    assert r.headers["location"].startswith("/auth/password-change?"), r.headers["location"]
    row = last_audit("login.password_change_required")
    assert row.actor == "climade@pwpolicy.test" and row.detail == "reset"
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/password-change")
    r = c.get("/projects", follow_redirects=False)
    assert "next=/projects" in r.headers["location"], r.headers["location"]
    page = c.get("/auth/password-change?next=/projects").text
    assert "An administrator set a temporary password" in page
    assert 'id="pwc-rules"' in page and 'minlength="12"' in page
    tok = csrf_from(c)
    r = c.post(
        "/auth/password-change",
        data={
            "current": "temporary granite 55",
            "new": "temporary granite 55",
            "confirm": "temporary granite 55",
            "next": "/projects",
            "csrf_token": tok,
        },
    )
    assert r.status_code == 400 and "different from your current one" in r.text
    assert 'aria-invalid="true"' in r.text.split('id="pwc-new"', 1)[1].split(">", 1)[0]
    r = c.post(
        "/auth/password-change",
        data={
            "current": "temporary granite 55",
            "new": "a",
            "confirm": "b",
            "next": "/projects",
            "csrf_token": tok,
        },
    )
    assert r.status_code == 400 and "did not match" in r.text
    r = c.post(
        "/auth/password-change",
        data={
            "current": "temporary granite 55",
            "new": "saffron glacier 12",
            "confirm": "saffron glacier 12",
            "next": "https://evil.example/",
            "csrf_token": tok,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and not r.headers["location"].startswith("http"), r.headers
    u = user_row("climade@pwpolicy.test")
    assert not u.password_must_change and not security.password_change_required(u)
    r = c.get("/", follow_redirects=False)
    assert not (r.headers.get("location") or "").startswith("/auth/password-change")
    # max age: expired password -> forced change; the clock restarts after the change
    settings.set_many({MAX_AGE: "30"})
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.password_changed_at = datetime.utcnow() - timedelta(days=31)
        s.add(row)
        s.commit()
    c2, r = login("climade@pwpolicy.test", "saffron glacier 12")
    assert r.headers["location"].startswith("/auth/password-change"), r.headers["location"]
    assert "has expired" in c2.get("/auth/password-change").text
    # signing out clears the flag
    sign_out(c2)
    assert "pwc" not in session_data(c2)
    settings.set_many({MAX_AGE: "0"})
    _c3, r = login("climade@pwpolicy.test", "saffron glacier 12")
    assert r.headers["location"] == "/", "max age 0 must never expire"
    print("  forced change: reset + max age -> change page wall; reuse refused; flag + clock reset")


def _post_change(c, current, new):
    return c.post(
        "/auth/password-change",
        data={
            "current": current,
            "new": new,
            "confirm": new,
            "next": "/",
            "csrf_token": csrf_from(c),
        },
        follow_redirects=False,
    )


def check_walls_history_zero():
    """Gate round 1 MEDIUM: with a password history of 0 neither wall may be cleared by typing
    the current password again (the always-on current-password rule), on the web page."""
    settings.set_many({HISTORY: "0"})
    email, temp = "wall0@pwpolicy.test", "temporary basalt 64"
    security.create_user(email, "W0", Role.TRANSLATOR, "first basalt choice 1")
    code, _o, err = run_cli(["user", "reset-password", email, "--password", temp])
    assert code == 0, err
    # admin reset wall
    c, r = login(email, temp)
    assert r.headers["location"].startswith("/auth/password-change"), r.headers["location"]
    r = _post_change(c, temp, temp)
    assert r.status_code == 400 and "different from your current one" in r.text, r.status_code
    assert user_row(email).password_must_change, "the reset wall was cleared without a change"
    r = c.get("/", follow_redirects=False)
    assert r.headers["location"].startswith("/auth/password-change"), "wall gone"
    fresh = "granite orchid mirror 9"
    r = _post_change(c, temp, fresh)
    assert r.status_code == 303 and not user_row(email).password_must_change
    # max-age wall
    settings.set_many({MAX_AGE: "30"})
    old = datetime.utcnow() - timedelta(days=40)
    with Session(engine) as s:
        row = s.get(User, user_row(email).id)
        row.password_changed_at = old
        s.add(row)
        s.commit()
    c2, r = login(email, fresh)
    assert r.headers["location"].startswith("/auth/password-change"), r.headers["location"]
    r = _post_change(c2, fresh, fresh)
    assert r.status_code == 400 and "different from your current one" in r.text
    assert user_row(email).password_changed_at == old, "the max-age clock was reset"
    assert security.password_change_required(user_row(email)) == "expired"
    r = c2.get("/", follow_redirects=False)
    assert r.headers["location"].startswith("/auth/password-change"), "wall gone"
    # with history 0 an OLDER password is allowed again (only the current one is refused)
    r = _post_change(c2, fresh, temp)
    assert r.status_code == 303, r.status_code
    assert security.password_change_required(user_row(email)) is None
    settings.set_many({HISTORY: "5", MAX_AGE: "0"})
    print("  history 0: reset + max-age walls refuse the current password on the web page")


def csrf_from(c) -> str:
    tok = session_data(c).get("csrf")
    if not tok:
        c.get("/auth/password-change")
        tok = session_data(c).get("csrf") or ""
    assert tok
    return tok


# --------------------------------------------------------------------------------------- G
def check_gdpr():
    u = user_row("invitee@pwpolicy.test")
    blob = "".join(str(rec) for _sec, rec in gdpr.export_user(u.id))
    assert "$argon2" not in blob, "a password hash reached the GDPR export"
    with Session(engine) as s:
        n = len(s.exec(select(PasswordHistory).where(PasswordHistory.user_id == u.id)).all())
    assert n >= 1
    ok, _msg = gdpr.erase_user(u.id, actor=ADMIN)
    assert ok, _msg
    with Session(engine) as s:
        n = len(s.exec(select(PasswordHistory).where(PasswordHistory.user_id == u.id)).all())
        row = s.get(User, u.id)
    assert n == 0 and row.password_hash == "" and row.password_changed_at is None
    # a hard delete (no product path does one; fixtures and operators may) cascades
    tmp = security.create_user("gone@pwpolicy.test", "G", Role.TRANSLATOR, GOOD)
    with Session(engine) as s:
        s.delete(s.get(User, tmp.id))
        s.commit()
        left = s.exec(select(PasswordHistory).where(PasswordHistory.user_id == tmp.id)).all()
    assert left == [], "history rows outlived a hard-deleted user"
    print("  gdpr: export carries no hash; erase deletes every history row; hard delete cascades")


# --------------------------------------------------------------------------------------- X
def check_unaffected():
    settings.set_many({MIN_LEN: "128", MAX_AGE: "1"})
    sso = security.create_user("sso@pwpolicy.test", "S", Role.TRANSLATOR, "", auth_source="sso")
    ldap = security.create_user("ldap@pwpolicy.test", "L", Role.TRANSLATOR, "", auth_source="ldap")
    for u in (sso, ldap):
        assert security.password_change_required(u) is None
    with Session(engine) as s:
        row = s.get(User, ldap.id)
        row.password_must_change = True  # even a stray flag never gates a directory login
        s.add(row)
        s.commit()
        row = s.get(User, ldap.id)
    assert security.password_change_required(row) is None
    dev = security.create_user(
        "dev@pwpolicy.test", "D", Role.TRANSLATOR, "admin", enforce_policy=False
    )
    assert dev.password_hash, "the dev seeders must still bypass the policy"
    settings.set_many({MIN_LEN: "12", MAX_AGE: "0"})
    with open("app/web/auth.py", encoding="utf-8") as fh:
        src = fh.read()
    ldap_branch = src.split("if ldap_auth.enabled():", 1)[1].split("u = security.authenticate", 1)[
        0
    ]
    assert "password_change_required" not in ldap_branch, "LDAP sign-in must not be gated"
    print("  unaffected: SSO/LDAP accounts never checked or gated; dev seeders bypass")


# --------------------------------------------------------------------------------------- M
def check_migration():
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy import inspect as sa_inspect

    from alembic import command

    tmp = tempfile.mkdtemp(prefix="loctms_pwpolicy_mig_")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    url = "sqlite:///" + os.path.join(tmp, "m.db").replace("\\", "/")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "a7e3c9d2b5f1")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(
            text(
                'INSERT INTO "user" (email, name, role, password_hash, auth_source, active, '
                "is_superadmin, created_at, session_epoch) VALUES "
                "('a@x.test', '', 'admin', '$argon2id$x', 'local', 1, 0, CURRENT_TIMESTAMP, 0), "
                "('b@x.test', '', 'viewer', '', 'sso', 1, 0, CURRENT_TIMESTAMP, 0)"
            )
        )
    eng.dispose()
    command.upgrade(cfg, "e5a1c8d3f7b2")
    eng = create_engine(url)
    insp = sa_inspect(eng)
    cols = {c["name"]: c["nullable"] for c in insp.get_columns("user")}
    assert cols["password_changed_at"] is True and cols["password_must_change"] is False
    hcols = {c["name"]: c["nullable"] for c in insp.get_columns("passwordhistory")}
    assert hcols == {"id": False, "user_id": False, "password_hash": False, "created_at": False}, (
        hcols
    )
    assert "ix_passwordhistory_user_id" in {i["name"] for i in insp.get_indexes("passwordhistory")}
    with eng.begin() as conn:
        rows = dict(
            conn.execute(text('SELECT email, password_changed_at IS NOT NULL FROM "user"')).all()
        )
        flags = {r[0] for r in conn.execute(text('SELECT password_must_change FROM "user"'))}
    assert rows == {"a@x.test": 1, "b@x.test": 0}, rows
    assert flags == {0}, flags
    eng.dispose()
    command.downgrade(cfg, "a7e3c9d2b5f1")
    eng = create_engine(url)
    insp = sa_inspect(eng)
    assert "passwordhistory" not in insp.get_table_names()
    assert "password_changed_at" not in {c["name"] for c in insp.get_columns("user")}
    eng.dispose()
    print("  migration e5a1c8d3f7b2: upgrade + backfill (local only), downgrade clean")


def main():
    init_db()
    check_migration()
    check_setup_door()  # creates the admin (fresh install, wall off)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    admin_client, r = login(ADMIN, ADMIN_PW)
    assert r.status_code == 303 and r.headers["location"] == "/", r.headers
    check_rules()
    check_settings(admin_client)
    check_invite_door()
    check_account_door()
    check_admin_doors(admin_client)
    check_forced_change()
    check_walls_history_zero()
    check_gdpr()
    check_unaffected()
    print("PASSWORD POLICY OK")


if __name__ == "__main__":
    main()
