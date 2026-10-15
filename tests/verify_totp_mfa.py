"""Two-step sign-in (TOTP, RFC 6238) for local and LDAP accounts (HECVAT AAAI-17 / DCTR-14).
Offline, hermetic (throwaway LOC_TMS_DATA_DIR). Real requests through the app, the
in-process CLI and the token /api.

Proves:

  T  the TOTP arithmetic against the RFC 6238 appendix B vectors; the secret is stored as a
     Fernet ciphertext (never the plaintext); the otpauth URI and the QR SVG;
  M  the alembic revision c3d9a6f1e2b7 upgrades (both tables, unique user index) and
     downgrades;
  S  setup with mfa_required=admins: the first admin sets up two-step sign-in before any
     session exists, then sees ten recovery codes once;
  N  NO SESSION BEFORE THE CODE: after a correct password the session carries only the
     pending state (no uid); walled pages, /auth/password-change, /auth/keepalive and
     /auth/session all treat the person as signed out; the code signs them in;
  R  replay refused: the same code (same step) and an older step inside the drift window;
  L  wrong codes count toward the SAME lockout as wrong passwords (5 in the window), and the
     lock then refuses the password step too; a pending step past 5 minutes is refused and
     the sign-in page announces why;
  C  recovery codes: each works once, recovery_used is audited with the count left;
  A  admin reset (web Users page, DELETE /api/users/{uid}/mfa, CLI user mfa-reset) deletes
     the factor AND bumps the session epoch (live sessions end); GET status on /api + CLI;
  P  mfa_required=admins forces set-up for admins only; =all for every password account;
     invite acceptance then sign-in lands on the set-up page; the forced password change
     comes AFTER the second factor;
  X  SSO (OIDC) sign-in is unaffected even with mfa_required=all; LDAP sign-in requires it;
     an API key keeps working for an account with two-step sign-in on;
  U  self-service on the Account page (a viewer too): set up, new recovery codes (needs a
     code), turn off (needs a code; refused while the policy requires it);
  K  the setting: Security page (validated, audited before/after), CLI config set, a
     sensitive key; audit rows never contain a code or the secret; GDPR erase deletes the
     factor;
  G  gate round 1: a per-ACCOUNT wrong-code budget (2 IPs lock it; LDAP name variants
     share it; persisted; mfa.locked; guards the Account-page checks); OIDC/SAML never
     adopt an account with two-step sign-in on; an unverified session ends once the policy
     applies (promotion, raised policy); no-store on every code/secret/recovery page;
     concurrent first set-ups never 500 (one secret stored); round 3 LOW-7: a code check
     that raises gives both reservations back exactly once, and a code refused unchecked is
     shown the locked message and counts no per-address failure;
  F  identity-02: a set-up secret belongs to the one flow that showed it; a new password
     step or self-service grant gets a fresh secret; an abandoned secret confirms and
     signs in nothing;
  I  #423: a SCIM-linked local row with no password is an identity-provider account (no
     local factor, set-up refused, not ended by mfa_required=all, SAML still works); a
     no-password local row is refused set-up instead of skipping re-authentication;
  Z  timing: an unknown email, a disabled account and an account with no local password
     all run one Argon2 verify in authenticate(); the code step runs one when there is
     nothing to compare against.

Prints ASCII only; ends with TOTP MFA OK.
"""

import base64
import json
import os
import re
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_totpmfa_")

import logging

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, MfaRecoveryCode, Role, User, UserMfa
from app.services import apikeys, gdpr, ldap_auth, mfa, oidc, security, settings
from app.services import security_client_ip_one_authority_ as sec_core
from tests._cli_runner import run_cli
from tests._csrf import session_data

ADMIN = "admin@totp.test"
ADMIN_PW = "granite meadow falcon 2026"  # pregate: allow-secret throwaway fixture login
USER_PW = "copper lagoon willow 88"  # pregate: allow-secret throwaway fixture password
NEW_PW = "saffron canyon ember 51"  # pregate: allow-secret throwaway fixture password
BURST_BAD_PW = "wrong horse"  # pregate: allow-secret throwaway fixture password
RECO = "recovery-page@totp.test"
RECO_PW = "harbor willow quartz 77"  # pregate: allow-secret throwaway fixture password
IP = "testclient"

# One controllable clock for codes and the 5-minute pending window (mfa._now is the ONE
# clock both read). Every check that needs a fresh code advances it by a step.
_CLOCK = [time.time()]
mfa._now = lambda: _CLOCK[0]


def tick(seconds: float = mfa.STEP_SECONDS) -> None:
    _CLOCK[0] += seconds


def user_row(email: str) -> User:
    with Session(engine) as s:
        return s.exec(select(User).where(User.email == email)).first()


def mfa_row(uid: int):
    with Session(engine) as s:
        return s.exec(select(UserMfa).where(UserMfa.user_id == uid)).first()


def secret_of(uid: int) -> str:
    row = mfa_row(uid)
    assert row is not None, "no UserMfa row"
    return mfa._decrypt(row.secret_enc)


#: Every code and recovery code this suite handled, so the audit trail can be searched for
#: them at the end (none may appear there).
_SEEN: set = set()


def code_for(uid: int, step_offset: int = 0) -> str:
    code = mfa.totp(secret_of(uid), _CLOCK[0] + step_offset * mfa.STEP_SECONDS)
    _SEEN.add(code)
    return code


def audits(action: str):
    with Session(engine) as s:
        return s.exec(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc())
        ).all()


def csrf(c) -> str:
    tok = session_data(c).get("csrf")
    if not tok:
        c.get("/auth/login")
        tok = session_data(c).get("csrf") or ""
    assert tok, "no CSRF token"
    return tok


def reset_lockout() -> None:
    security._failures.clear()
    security._locked_until.clear()


def password_step(email: str, pw: str, nxt: str = "/", client=None):
    c = client or TestClient(app)
    c.get("/auth/login")
    r = c.post(
        "/auth/login",
        data={"email": email, "password": pw, "next": nxt, "csrf_token": csrf(c)},
        follow_redirects=False,
    )
    return c, r


def post_code(c, code: str = "", recovery: str = "", path: str = "/auth/mfa"):
    c.get(path)  # renders the page (mints the CSRF token of the pending session)
    data = {"csrf_token": csrf(c)}
    if recovery:
        data["recovery"] = recovery
    else:
        data["code"] = code
    return c.post(path, data=data, follow_redirects=False)


def signed_in(c) -> bool:
    return bool(session_data(c).get("uid"))


def full_sign_in(email: str, pw: str, uid: int):
    c, r = password_step(email, pw)
    assert r.status_code == 303 and r.headers["location"] == "/auth/mfa", r.headers
    tick()
    r = post_code(c, code_for(uid))
    assert r.status_code == 303 and signed_in(c), (r.status_code, r.text[:300])
    return c


def enroll_forced(c, uid: int):
    """On the forced set-up page (pending): confirm with a correct code, return the codes."""
    page = c.get("/auth/mfa/enroll")
    assert page.status_code == 200, page.status_code
    tick()
    r = c.post(
        "/auth/mfa/enroll",
        data={"code": code_for(uid), "csrf_token": csrf(c)},
        follow_redirects=False,
    )
    assert r.status_code == 200 and "Save your recovery codes" in r.text, r.text[:400]
    codes = re.findall(r"<code>([a-z2-9]{5}-[a-z2-9]{5})</code>", r.text)
    assert len(codes) == mfa.RECOVERY_CODE_COUNT, codes
    _SEEN.update(codes)
    return codes, r


# ------------------------------------------------------------------------------------- T
def check_totp_unit():
    sec = base64.b32encode(b"12345678901234567890").decode()
    for t, exp in (
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ):
        assert mfa.hotp(mfa._key_bytes(sec), t // 30, digits=8) == exp, t
    s = mfa.new_secret()
    assert len(s) == 32 and re.fullmatch(r"[A-Z2-7]{32}", s)
    enc = mfa._encrypt(s)
    assert s not in enc and mfa._decrypt(enc) == s
    assert mfa._decrypt("not-a-token") is None
    uri = mfa.otpauth_uri(s, "a@b.test")
    assert uri.startswith("otpauth://totp/") and "period=30" in uri
    assert "=".join(("secret", s)) in uri
    svg = mfa.qr_svg(uri)
    assert svg.startswith("<svg") and "a@b.test" not in svg and s not in svg
    # the drift window: previous and next step accepted, two away refused
    now = _CLOCK[0]
    for off, ok in ((-1, True), (0, True), (1, True), (-2, False), (2, False)):
        got = mfa._match_step(s, mfa.totp(s, now + off * 30), now)
        assert (got is not None) == ok, (off, got)
    print("  T: RFC 6238 vectors, drift window, Fernet at rest, otpauth URI, QR SVG")


# ------------------------------------------------------------------------------------- M
def check_migration():
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect

    from alembic import command

    tmp = tempfile.mkdtemp(prefix="loctms_totpmfa_mig_")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    url = "sqlite:///" + os.path.join(tmp, "m.db").replace("\\", "/")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "e4b7c1a9d2f6")
    command.upgrade(cfg, "c3d9a6f1e2b7")
    eng = create_engine(url)
    insp = sa_inspect(eng)
    cols = {c["name"]: c["nullable"] for c in insp.get_columns("usermfa")}
    assert cols == {
        "id": False,
        "user_id": False,
        "secret_enc": False,
        "enabled_at": True,
        "last_step": False,
        "last_used_at": True,
        "fail_count": False,
        "fail_window_start": True,
        "locked_until": True,
        "created_at": False,
    }, cols
    idx = {i["name"]: i for i in insp.get_indexes("usermfa")}
    assert idx["ix_usermfa_user_id"]["unique"], idx
    rcols = {c["name"]: c["nullable"] for c in insp.get_columns("mfarecoverycode")}
    assert rcols == {
        "id": False,
        "user_id": False,
        "code_hash": False,
        "used_at": True,
        "created_at": False,
    }, rcols
    eng.dispose()
    command.downgrade(cfg, "e4b7c1a9d2f6")
    eng = create_engine(url)
    names = sa_inspect(eng).get_table_names()
    assert "usermfa" not in names and "mfarecoverycode" not in names
    eng.dispose()
    print("  M: migration c3d9a6f1e2b7 upgrade (unique user index) + downgrade clean")


# ------------------------------------------------------------------------------------- S
def check_setup_forced():
    assert security.user_count() == 0
    settings.set_many({"mfa_required": "admins"})
    c = TestClient(app)
    r = c.post(
        "/auth/setup",
        data={"email": ADMIN, "name": "Admin", "password": ADMIN_PW},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/auth/mfa/enroll", r.headers
    sess = session_data(c)
    assert "uid" not in sess and "mfa_p" in sess, sess
    uid = user_row(ADMIN).id
    page = c.get("/auth/mfa/enroll").text
    secret = secret_of(uid)
    assert mfa.format_secret(secret) in page, "the key must be shown as text"
    assert "<svg" in page and 'role="img"' in page
    assert 'autocomplete="one-time-code"' in page and 'id="mfa-enroll-code"' in page
    assert "within 5 minutes" in page, "the time limit must be announced"
    row = mfa_row(uid)
    assert row.enabled_at is None and secret not in row.secret_enc
    # a reload keeps the same key (what was scanned stays valid)
    c.get("/auth/mfa/enroll")
    assert secret_of(uid) == secret
    # a wrong code: refused, still not signed in, field marked
    r = c.post(
        "/auth/mfa/enroll", data={"code": "000000", "csrf_token": csrf(c)}, follow_redirects=False
    )
    assert r.status_code == 401 and not signed_in(c)
    assert 'aria-invalid="true"' in r.text.split('id="mfa-enroll-code"', 1)[1].split(">", 1)[0]
    codes, r = enroll_forced(c, uid)
    assert signed_in(c), "a correct code signs the first admin in"
    assert 'href="/"' in r.text
    assert mfa.is_enrolled(uid) and mfa.recovery_codes_left(uid) == 10
    assert audits("mfa.enroll") and audits("setup.create_admin")
    reset_lockout()
    print("  S: setup with mfa_required=admins: set-up before any session, codes shown once")
    return codes


# --------------------------------------------------------------------------------- #408
def check_recovery_page_timeout():
    """#408 (webui-02): the forced-enrollment recovery page signs the person in and shows
    all ten codes, so it must carry the shared session-timeout metadata, dialog and script
    (base.html's #260 fix) or an unattended machine reads the codes after the idle timeout.
    Reproduces a real forced enrollment and checks the served response, no-store preserved.
    """
    before = settings.get("mfa_required")
    settings.set_many({"security_auth_required": "1", "mfa_required": "all"})
    settings.invalidate_cache()
    try:
        security.create_user(RECO, "Reco", Role.TRANSLATOR, RECO_PW)
        uid = user_row(RECO).id
        c, r = password_step(RECO, RECO_PW)
        assert r.status_code == 303 and r.headers["location"] == "/auth/mfa/enroll", r.headers
        # The anonymous set-up door (no session yet) never carries the signed-in warning.
        page = c.get("/auth/mfa/enroll").text
        assert "session_timeout.js" not in page and 'name="loc-session-idle"' not in page
        _codes, r = enroll_forced(c, uid)  # 200, signed in, the one time codes are shown
        assert r.status_code == 200 and "Save your recovery codes" in r.text
        assert r.headers.get("cache-control") == "no-store", r.headers.get("cache-control")
        for marker in (
            "/static/session_timeout.js",
            'id="session-dialog"',
            'name="loc-session-idle"',
            'name="loc-session-uid"',
        ):
            assert marker in r.text, f"#408: the recovery page is missing {marker}"
        assert f'<meta name="loc-session-uid" content="{uid}" />' in r.text
        assert 'role="alertdialog"' in r.text
    finally:
        settings.set_many({"mfa_required": before or "off"})
        settings.invalidate_cache()
    print("  #408: recovery page loads the timeout warning; codes hide at the deadline")


# ------------------------------------------------------------------------------------- N
def check_no_session_before_code(admin_uid: int):
    c, r = password_step(ADMIN, ADMIN_PW, nxt="/projects")
    assert r.status_code == 303 and r.headers["location"] == "/auth/mfa", r.headers
    sess = session_data(c)
    assert "uid" not in sess and sess.get("mfa_p", {}).get("u") == admin_uid, sess
    for path in ("/", "/account", "/users", "/security"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login"), (
            path,
            r.status_code,
            r.headers.get("location"),
        )
    r = c.get("/auth/password-change", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    assert c.get("/auth/session").json().get("signed_in") is False
    r = c.post("/auth/keepalive", headers={"X-CSRF-Token": csrf(c)})
    assert r.status_code == 401
    # a walled POST is refused too (no session to act as)
    r = c.post("/account/name", data={"name": "x", "csrf_token": csrf(c)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    page = c.get("/auth/mfa")
    assert page.status_code == 200 and 'autocomplete="one-time-code"' in page.text
    assert "within 5 minutes" in page.text
    tick()
    r = post_code(c, code_for(admin_uid))
    assert r.status_code == 303 and r.headers["location"] == "/projects", r.headers
    assert signed_in(c) and "mfa_p" not in session_data(c)
    assert c.get("/account", follow_redirects=False).status_code == 200
    ok = audits("login.success")[0]
    assert ok.actor == ADMIN and ok.detail == "mfa=totp", (ok.actor, ok.detail)
    # Issue #546: "Start over" must not be a GET that changes state (a cross-site link could
    # abort a sign-in in progress); it is a CSRF-checked POST.
    c2, r2 = password_step(ADMIN, ADMIN_PW)
    assert r2.headers["location"] == "/auth/mfa", r2.headers
    page2 = c2.get("/auth/mfa").text
    assert 'action="/auth/mfa/cancel"' in page2 and 'href="/auth/mfa/cancel"' not in page2
    g = c2.get("/auth/mfa/cancel", follow_redirects=False)
    assert g.status_code in (404, 405), g.status_code
    assert "mfa_p" in session_data(c2), "a GET must not clear the pending sign-in"
    bad = c2.post("/auth/mfa/cancel", data={}, follow_redirects=False)
    assert "expired=1" in bad.headers.get("location", "") and "mfa_p" in session_data(c2), (
        bad.status_code,
        bad.headers.get("location"),
    )
    ok_post = c2.post("/auth/mfa/cancel", data={"csrf_token": csrf(c2)}, follow_redirects=False)
    assert ok_post.status_code == 303 and ok_post.headers["location"] == "/auth/login"
    assert "mfa_p" not in session_data(c2)
    print("  N: after the password only a pending state exists (no uid); the code signs in")
    print("     'Start over' is a CSRF-checked POST; a GET leaves the pending sign-in alone")
    return c


# ------------------------------------------------------------------------------------- R
def check_replay(admin_uid: int):
    c, r = password_step(ADMIN, ADMIN_PW)
    tick()
    code = code_for(admin_uid)
    r = post_code(c, code)
    assert r.status_code == 303 and signed_in(c)
    # the same code again (same step) is refused
    c2, _ = password_step(ADMIN, ADMIN_PW)
    r = post_code(c2, code)
    assert r.status_code == 401 and not signed_in(c2), r.status_code
    assert "already been used" in r.text
    # an OLDER step inside the drift window is refused too
    older = code_for(admin_uid, -1)
    r = post_code(c2, older)
    assert r.status_code == 401 and not signed_in(c2)
    # the next step works
    tick()
    r = post_code(c2, code_for(admin_uid))
    assert r.status_code == 303 and signed_in(c2)
    reset_lockout()
    print("  R: the same code and an older step are refused (replay); the next step works")


# ------------------------------------------------------------------------------------- L
def check_lockout_and_expiry(admin_uid: int):
    reset_lockout()
    c, r = password_step(ADMIN, ADMIN_PW)
    for i in range(security.LOCKOUT_THRESHOLD - 1):
        r = post_code(c, "123456" if code_for(admin_uid) != "123456" else "654321")
        assert r.status_code == 401, (i, r.status_code)
    r = post_code(c, "000001" if code_for(admin_uid) != "000001" else "000002")
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?err=mfa_locked"
    assert "mfa_p" not in session_data(c) and not signed_in(c)
    # the lock is the SAME one the password step checks: even the right password is refused
    _, r = password_step(ADMIN, ADMIN_PW)
    assert r.status_code == 429, r.status_code
    assert audits("mfa.failed") and audits("login.lockout")[0].detail == "two-step sign-in"
    page = TestClient(app).get("/auth/login?err=mfa_locked").text
    assert "locked for 15 minutes" in page
    reset_lockout()
    # mixed: wrong passwords and wrong codes add up in one window
    for _ in range(3):
        _, r = password_step(ADMIN, "wrong password entirely")
        assert r.status_code == 401
    c, r = password_step(ADMIN, ADMIN_PW)
    assert r.headers["location"] == "/auth/mfa"
    r = post_code(c, "111111" if code_for(admin_uid) != "111111" else "222222")
    assert r.status_code == 401
    r = post_code(c, "333333" if code_for(admin_uid) != "333333" else "444444")
    assert r.headers.get("location") == "/auth/login?err=mfa_locked", r.status_code
    reset_lockout()
    # the 5-minute window: past it the step is refused and the sign-in page says why
    c, r = password_step(ADMIN, ADMIN_PW)
    tick(mfa.PENDING_MAX_SECONDS + 1)
    r = c.get("/auth/mfa", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?err=mfa_expired"
    assert "mfa_p" not in session_data(c)
    page = c.get(r.headers["location"]).text
    assert "timed out" in page and 'role="alert"' in page
    # a POST after expiry does not sign in either
    c, r = password_step(ADMIN, ADMIN_PW)
    code_then = code_for(admin_uid, 1)
    tick(mfa.PENDING_MAX_SECONDS + 1)
    r = c.post("/auth/mfa", data={"code": code_then, "csrf_token": csrf(c)}, follow_redirects=False)
    assert r.status_code == 303 and not signed_in(c)
    tick()
    print("  L: wrong codes share the password lockout (5), the lock blocks the password too;")
    print("     a step older than 5 minutes is refused and announced on the sign-in page")


# ------------------------------------------------------------------------------------- C
def check_recovery_codes(admin_uid: int, codes: list):
    reset_lockout()
    c, r = password_step(ADMIN, ADMIN_PW)
    r = post_code(c, recovery=codes[0].upper().replace("-", " "))
    assert r.status_code == 303 and signed_in(c), (r.status_code, r.text[:300])
    assert mfa.recovery_codes_left(admin_uid) == 9
    used = audits("mfa.recovery_used")[0]
    assert used.detail == "9 code(s) left", used.detail
    c2, _ = password_step(ADMIN, ADMIN_PW)
    r = post_code(c2, recovery=codes[0])
    assert r.status_code == 401 and not signed_in(c2), "a recovery code works only once"
    assert 'aria-invalid="true"' in r.text.split('id="mfa-recovery"', 1)[1].split(">", 1)[0]
    reset_lockout()
    print("  C: a recovery code signs in once (spaces/case ignored), then is refused")


# ------------------------------------------------------------------------------------- P
def make_user(email: str, role: str, source: str = "local", pw: str = USER_PW) -> int:
    u = security.create_user(email, "", role, pw if source == "local" else "", auth_source=source)
    return u.id


def check_policy_and_invite(admin_client):
    settings.set_many({"mfa_required": "admins"})
    make_user("admin2@totp.test", Role.ADMIN)
    make_user("trans@totp.test", Role.TRANSLATOR)
    c, r = password_step("admin2@totp.test", USER_PW)
    assert r.headers["location"] == "/auth/mfa/enroll" and not signed_in(c)
    c, r = password_step("trans@totp.test", USER_PW)
    assert r.headers["location"] == "/" and signed_in(c), "admins only"
    settings.set_many({"mfa_required": "all"})
    c, r = password_step("trans@totp.test", USER_PW)
    assert r.headers["location"] == "/auth/mfa/enroll" and not signed_in(c)
    tuid = user_row("trans@totp.test").id
    enroll_forced(c, tuid)
    assert signed_in(c)
    # invite acceptance never signs in; the sign-in that follows lands on the set-up page
    inv = security.create_invite("invitee@totp.test", Role.VIEWER, ADMIN)
    ic = TestClient(app)
    ic.get(f"/auth/accept/{inv.token}")
    r = ic.post(
        f"/auth/accept/{inv.token}",
        data={"name": "I", "password": USER_PW, "confirm": USER_PW, "csrf_token": csrf(ic)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?accepted=1"
    assert not signed_in(ic)
    _, r = password_step("invitee@totp.test", USER_PW, client=ic)
    assert r.headers["location"] == "/auth/mfa/enroll" and not signed_in(ic)
    # forced password change comes AFTER the second factor
    reset_lockout()
    with Session(engine) as s:
        ok, _ = security.admin_set_password(s, tuid, NEW_PW)
    assert ok
    c, r = password_step("trans@totp.test", NEW_PW, nxt="/projects")
    assert r.headers["location"] == "/auth/mfa", "the second factor first"
    assert "pwc" not in session_data(c)
    tick()
    r = post_code(c, code_for(tuid))
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/password-change")
    assert signed_in(c) and session_data(c).get("pwc") == "reset"
    settings.set_many({"mfa_required": "admins"})
    reset_lockout()
    print("  P: admins-only vs all; invite accept then sign-in -> set-up; the forced password")
    print("     change follows the second factor")
    return tuid


# ------------------------------------------------------------------------------------- X
def check_sso_ldap_apikey(admin_uid: int):
    settings.set_many({"mfa_required": "all"})
    # OIDC: patched IdP answer (the callback trusts oidc.complete_login's verified claims)
    real = (oidc.enabled, oidc.complete_login, oidc.stored_next)
    try:
        oidc.enabled = lambda: True
        oidc.complete_login = lambda request, code, state: (
            {
                "iss": "https://idp.test",
                "sub": "s-totp",
                "email": "sso.person@totp.test",
                "email_verified": True,
                "name": "S",
            },
            None,
        )
        oidc.stored_next = lambda request: "/"
        c = TestClient(app)
        r = c.get("/auth/oidc/callback?code=x&state=y", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", r.headers
        assert signed_in(c), "SSO sign-in is not asked for a code"
        sso = user_row("sso.person@totp.test")
        assert sso.auth_source == "sso" and not mfa.required_for(sso)
        assert mfa.status(sso)["applies"] is False
    finally:
        oidc.enabled, oidc.complete_login, oidc.stored_next = real
    # LDAP: a directory-proven password is the first factor only
    luid = make_user("ldap.person@totp.test", Role.TRANSLATOR, source="ldap")
    real_l = (ldap_auth.enabled, ldap_auth.try_login)
    try:
        ldap_auth.enabled = lambda: True

        def _try(username, password):
            if username == "ldap.person" and password == USER_PW:
                return user_row("ldap.person@totp.test"), None
            return None, None

        ldap_auth.try_login = _try
        c, r = password_step("ldap.person", USER_PW)
        assert r.headers["location"] == "/auth/mfa/enroll" and not signed_in(c), r.headers
        enroll_forced(c, luid)
        assert audits("auth.ldap_login")[0].detail == "mfa=totp"
        c, r = password_step("ldap.person", USER_PW)
        assert r.headers["location"] == "/auth/mfa" and not signed_in(c)
        tick()
        r = post_code(c, code_for(luid))
        assert r.status_code == 303 and signed_in(c)
    finally:
        ldap_auth.enabled, ldap_auth.try_login = real_l
    settings.set_many({"mfa_required": "admins"})
    # an API key is a separate credential: unaffected by two-step sign-in
    _, key = apikeys.create_key(admin_uid, name="t", scopes="docs admin")
    r = TestClient(app).get("/api/users", headers={"X-Loc-Token": key})
    assert r.status_code == 200, r.status_code
    reset_lockout()
    print("  X: OIDC unaffected under mfa_required=all; LDAP needs the code; API key unaffected")
    return key


# ------------------------------------------------------------------ gate round 1 fixes
def _wrong(uid: int) -> str:
    live = {mfa.totp(secret_of(uid), _CLOCK[0] + k * mfa.STEP_SECONDS) for k in (-1, 0, 1)}
    return next(c for c in ("111111", "222222", "333333", "444444") if c not in live)


def _unlock(uid: int) -> None:
    with Session(engine) as s:
        row = s.exec(select(UserMfa).where(UserMfa.user_id == uid)).first()
        row.locked_until = None
        row.fail_count = 0
        row.fail_window_start = None
        s.add(row)
        s.commit()


def _ip_client(ip: str):
    return TestClient(app, client=(ip, 50000))


def check_account_budget(tuid: int, luid: int):
    """MEDIUM-1 / LOW-2: wrong codes count per ACCOUNT from any address and any spelling
    of the sign-in name; the lock covers the code step AND the Account-page code checks,
    is persisted on the UserMfa row, and writes mfa.locked."""
    reset_lockout()
    # (a) two addresses, 5 wrong codes each: each address trips only its own per-IP
    #     lockout at its 5th, but together they lock the ACCOUNT for every address
    for ip in ("10.1.0.1", "10.2.0.2"):
        c, r = password_step("trans@totp.test", NEW_PW, client=_ip_client(ip))
        assert r.headers["location"] == "/auth/mfa", (ip, r.headers)
        for _ in range(security.LOCKOUT_THRESHOLD):
            r = post_code(c, _wrong(tuid))
        assert r.headers.get("location") == "/auth/login?err=mfa_locked", (ip, r.status_code)
    assert mfa.account_locked_seconds(tuid) > 0, "10 wrong codes from 2 IPs lock the account"
    assert mfa_row(tuid).locked_until is not None, "the lock is persisted (survives restart)"
    locked = audits("mfa.locked")[0]
    assert locked.actor == "trans@totp.test" and locked.ip == "10.2.0.2", (locked.actor, locked.ip)
    # a third, fresh address with the RIGHT code is refused while the account is locked
    c, r = password_step("trans@totp.test", NEW_PW, client=_ip_client("10.3.0.3"))
    tick()
    r = post_code(c, code_for(tuid))
    assert r.headers.get("location") == "/auth/login?err=mfa_locked" and not signed_in(c)
    assert mfa.verify(tuid, code_for(tuid, 1)) is mfa.REFUSED, "refused unchecked while locked"
    # the lock expires (clock) and a right code then works and clears the budget
    tick(mfa.ACCOUNT_LOCK_S + 1)
    reset_lockout()
    full_sign_in("trans@totp.test", NEW_PW, tuid)
    assert mfa_row(tuid).fail_count == 0 and mfa.account_locked_seconds(tuid) == 0
    # (b) LDAP name variants share the budget: jdoe and jdoe@corp, three addresses
    real_l = (ldap_auth.enabled, ldap_auth.try_login)
    try:
        ldap_auth.enabled = lambda: True

        def _try(username, password):
            if username in ("ldap.person", "ldap.person@totp.test") and password == USER_PW:
                return user_row("ldap.person@totp.test"), None
            return None, None

        ldap_auth.try_login = _try
        reset_lockout()
        lc, r = password_step("ldap.person", USER_PW, client=_ip_client("10.9.9.9"))
        tick()
        r = post_code(lc, code_for(luid))
        assert r.status_code == 303 and signed_in(lc), "a signed-in LDAP session for (c)"
        for name, ip, n in (
            ("ldap.person", "10.4.0.4", 4),
            ("ldap.person@totp.test", "10.5.0.5", 4),
            ("ldap.person", "10.6.0.6", 2),
        ):
            c, r = password_step(name, USER_PW, client=_ip_client(ip))
            assert r.headers["location"] == "/auth/mfa", (name, r.headers)
            for _ in range(n):
                r = post_code(c, _wrong(luid))
        assert mfa.account_locked_seconds(luid) > 0, "name variants share one account budget"
        # (c) the Account-page code checks are guarded by the same lock
        tick()
        r = lc.post(
            "/account/mfa/recovery-codes",
            data={"code": code_for(luid), "csrf_token": csrf(lc)},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/account?err=mfa_wait", r.headers
        tick(mfa.ACCOUNT_LOCK_S + 1)
        _unlock(luid)
    finally:
        ldap_auth.enabled, ldap_auth.try_login = real_l
    # (d) concurrent first set-ups never 500 on the unique index: all get ONE secret
    import threading

    ruid = make_user("race@totp.test", Role.TRANSLATOR)
    got, errs = [], []

    def _go():
        try:
            got.append(mfa.begin_enrollment(ruid, USER_PW))
        except Exception as exc:
            errs.append(exc)

    threads = [threading.Thread(target=_go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # identity-02: each set-up is its own flow with its own secret; the race never 500s
    # and exactly one of them is the stored secret.
    assert not errs and len(got) == 6 and secret_of(ruid) in got, (errs, got)
    reset_lockout()
    print("  G1: per-ACCOUNT budget: 2 IPs x 5 wrong codes lock the account for every IP")
    print("      (persisted, mfa.locked); LDAP name variants share it; Account-page checks")
    print("      guarded; concurrent first set-ups never 500, one secret stored")


def check_sso_takeover_refused(luid: int):
    """LOW-1: an account with two-step sign-in ON is never adopted by OIDC or SAML."""
    before = user_row("ldap.person@totp.test").auth_source
    assert mfa.is_enrolled(luid) and before == "ldap"
    real = (oidc.enabled, oidc.complete_login, oidc.stored_next)
    try:
        oidc.enabled = lambda: True
        oidc.complete_login = lambda request, code, state: (
            {
                "iss": "https://idp.test",
                "sub": "s-ldap",
                "email": "ldap.person@totp.test",
                "email_verified": True,
            },
            None,
        )
        oidc.stored_next = lambda request: "/"
        c = TestClient(app)
        r = c.get("/auth/oidc/callback?code=x&state=y", follow_redirects=False)
        assert r.status_code == 401 and not signed_in(c), r.status_code
        assert "two-step sign-in" in r.text
    finally:
        oidc.enabled, oidc.complete_login, oidc.stored_next = real
    user, err = security.get_or_create_saml_user("ldap.person@totp.test", "L")
    assert user is None and "two-step sign-in" in (err or "")
    assert user_row("ldap.person@totp.test").auth_source == "ldap", "not re-marked"
    rows = audits("login.sso_refused")
    assert len(rows) >= 2 and rows[0].target == "ldap.person@totp.test"
    # round 2 LOW-6: the message says how to recover; the OIDC row carries the client IP
    assert "administrator to reset your two-step sign-in" in (err or "")
    assert rows[1].ip == IP, rows[1].ip
    print("  G2: OIDC and SAML refuse to adopt an account with two-step sign-in on (audited)")


def check_concurrency_and_recovery_bypass():
    """Round 2 MEDIUM-2: attempts are RESERVED before any code is checked, so a burst of
    concurrent wrong codes checks at most ACCOUNT_FAIL_LIMIT per account (and at most
    LOCKOUT_THRESHOLD per address), the account ends locked, and a right code after the
    limit is refused. LOW-5: a recovery code still works while the account is locked, and
    the owner is emailed when it locks."""
    import threading

    from app.services import mailer

    reset_lockout()
    buid = make_user("burst@totp.test", Role.TRANSLATOR)
    mfa.begin_enrollment(buid, USER_PW)
    tick()
    rcodes = mfa.confirm_enrollment(buid, code_for(buid))
    assert rcodes

    checked = []
    real_inner = mfa._verify_inner

    def counting_inner(uid, code):
        checked.append(code)
        return real_inner(uid, code)

    sent = []
    real_send = mailer.send
    mfa._verify_inner = counting_inner
    mailer.send = lambda to, subject, body: sent.append((to, subject)) or True
    try:
        # (a) service level: 40 threads, all wrong codes, released together
        bad = _wrong(buid)
        barrier = threading.Barrier(40)

        def _go():
            barrier.wait()
            mfa.verify(buid, bad, ip="10.7.0.1")

        threads = [threading.Thread(target=_go) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(checked) <= mfa.ACCOUNT_FAIL_LIMIT, len(checked)
        assert mfa.account_locked_seconds(buid) > 0, "the burst ends with the account locked"
        assert (
            len(audits("mfa.locked")) >= 1 and audits("mfa.locked")[0].target == "burst@totp.test"
        )
        assert sent and sent[-1][0] == "burst@totp.test", "the owner is emailed on lock"
        # a RIGHT authenticator code after the limit is refused, and not even checked
        n = len(checked)
        tick()
        assert mfa.verify(buid, code_for(buid)) is mfa.REFUSED and len(checked) == n
        # (b) LOW-5: a recovery code bypasses the account lock (and is spent)
        c, r = password_step("burst@totp.test", USER_PW, client=_ip_client("10.7.0.9"))
        assert r.headers["location"] == "/auth/mfa"
        r = post_code(c, recovery=rcodes[0])
        assert r.status_code == 303 and signed_in(c), (r.status_code, r.headers)
        _SEEN.update(rcodes)
        # (c) per address: 12 concurrent posts from ONE address and ONE pending cookie
        #     check at most LOCKOUT_THRESHOLD codes
        _unlock(buid)
        reset_lockout()
        checked.clear()
        c, r = password_step("burst@totp.test", USER_PW, client=_ip_client("10.7.0.2"))
        c.get("/auth/mfa")
        tok = csrf(c)
        cookie = c.cookies.get("session")
        clients = []
        for _ in range(12):
            cc = _ip_client("10.7.0.2")
            cc.cookies.set("session", cookie)
            clients.append(cc)
        barrier = threading.Barrier(12)
        bad = _wrong(buid)

        def _post(cc):
            barrier.wait()
            cc.post("/auth/mfa", data={"code": bad, "csrf_token": tok}, follow_redirects=False)

        threads = [threading.Thread(target=_post, args=(cc,)) for cc in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(checked) <= security.LOCKOUT_THRESHOLD, len(checked)
        assert security.login_locked_seconds("burst@totp.test", "10.7.0.2") > 0
        # (c2) the PASSWORD step reserves too: 20 concurrent wrong passwords from one address
        #     check at most LOCKOUT_THRESHOLD passwords, and the pair ends locked
        reset_lockout()
        pw_checked = []
        real_auth = security.authenticate

        def counting_auth(email, password):
            pw_checked.append(email)
            return real_auth(email, password)

        pclients = []
        for _ in range(20):
            pc = _ip_client("10.7.0.3")
            pc.get("/auth/login")
            pclients.append((pc, csrf(pc)))
        barrier = threading.Barrier(20)
        codes_seen = []

        def _pw(pc, tok):
            barrier.wait()
            r = pc.post(
                "/auth/login",
                data={"email": "burst@totp.test", "password": BURST_BAD_PW, "csrf_token": tok},
                follow_redirects=False,
            )
            codes_seen.append(r.status_code)

        security.authenticate = counting_auth
        try:
            threads = [threading.Thread(target=_pw, args=pt) for pt in pclients]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            security.authenticate = real_auth
        assert len(pw_checked) <= security.LOCKOUT_THRESHOLD, len(pw_checked)
        assert codes_seen.count(429) >= 20 - security.LOCKOUT_THRESHOLD, codes_seen
        assert security.login_locked_seconds("burst@totp.test", "10.7.0.3") > 0
        # a correct password does not use up the budget (the reservation is given back)
        reset_lockout()
        for _ in range(security.LOCKOUT_THRESHOLD + 2):
            _, r = password_step("burst@totp.test", USER_PW, client=_ip_client("10.7.0.4"))
            assert r.headers.get("location") == "/auth/mfa", r.status_code
        # (d) the per-address reservation itself, 30 threads: exactly LOCKOUT_THRESHOLD win
        reset_lockout()
        wins = []
        barrier = threading.Barrier(30)

        def _res():
            barrier.wait()
            wins.append(security.reserve_login_attempt("race@x.test", "10.8.0.1"))

        threads = [threading.Thread(target=_res) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert wins.count(True) == security.LOCKOUT_THRESHOLD, wins.count(True)
    finally:
        mfa._verify_inner = real_inner
        mailer.send = real_send
    _unlock(buid)
    reset_lockout()
    print("  G5: 40 concurrent wrong codes check <= 10 per account and lock it; a right code")
    print("      after the limit is refused unchecked; 12 concurrent posts from one address")
    print("      check <= 5; recovery codes bypass the account lock; owner emailed on lock")


def _set_budget(uid: int, count: int) -> None:
    """Put `count` attempts in the account's CURRENT window (as in-flight or earlier ones)."""
    with Session(engine) as s:
        row = s.exec(select(UserMfa).where(UserMfa.user_id == uid)).first()
        row.locked_until = None
        row.fail_count = count
        row.fail_window_start = mfa._utc_now()
        s.add(row)
        s.commit()


def _raised(fn, *args) -> bool:
    """True when fn(*args) raised the injected RuntimeError (it must reach the caller)."""
    try:
        fn(*args)
    except RuntimeError:
        return True
    return False


def _ip_entries(name: str, ip: str) -> int:
    return len(security._failures.get(security._lockout_key(name, ip), []))


def check_fault_release_and_refused():
    """Gate round 3, LOW-7. (a) A code check that RAISES gives back both reservations (the
    account budget and the per-address count) exactly once: after more faults than either
    limit, fail_count and the per-address entries are what they were before, nothing is
    locked, and a right code then signs in. (b) The same for the set-up confirm. (c) The
    give-back never goes below zero and never eats a reservation another request holds.
    (d) A right code REFUSED UNCHECKED (the budget held by in-flight attempts) gets the
    locked message and the login.locked row, is not checked, and counts no per-address
    failure."""
    reset_lockout()
    fuid = make_user("fault@totp.test", Role.TRANSLATOR)
    mfa.begin_enrollment(fuid, USER_PW)
    tick()
    assert mfa.confirm_enrollment(fuid, code_for(fuid))
    ip = "10.11.0.1"
    client = TestClient(app, client=(ip, 50000), raise_server_exceptions=False)
    c, r = password_step("fault@totp.test", USER_PW, client=client)
    assert r.headers["location"] == "/auth/mfa", r.headers
    # one earlier wrong code stays counted on both budgets (the "prior" to return to)
    r = post_code(c, _wrong(fuid))
    assert r.status_code == 401, r.status_code
    prior = (mfa_row(fuid).fail_count, _ip_entries("fault@totp.test", ip))
    assert prior == (1, 1), prior

    def _boom(*_a, **_k):
        raise RuntimeError("database fault")

    real_v, real_c = mfa._verify_inner, mfa._confirm_inner
    mfa._verify_inner = _boom
    try:
        # (a) more faults than the account limit AND the per-address threshold
        for i in range(mfa.ACCOUNT_FAIL_LIMIT + 3):
            r = post_code(c, _wrong(fuid))
            assert r.status_code == 500, (i, r.status_code)
    finally:
        mfa._verify_inner = real_v
    now = (mfa_row(fuid).fail_count, _ip_entries("fault@totp.test", ip))
    assert now == prior, (now, prior)
    assert mfa_row(fuid).locked_until is None and not mfa.account_locked_seconds(fuid)
    assert not security.login_locked_seconds("fault@totp.test", ip)
    assert not audits("mfa.locked") or audits("mfa.locked")[0].target != "fault@totp.test"
    tick()
    r = post_code(c, code_for(fuid))
    assert r.status_code == 303 and signed_in(c), (r.status_code, r.headers)
    assert mfa_row(fuid).fail_count == 0
    # (b) the set-up confirm: a raising check gives the account reservation back
    euid = make_user("fault.enroll@totp.test", Role.TRANSLATOR)
    mfa.begin_enrollment(euid, USER_PW)
    _set_budget(euid, 2)
    mfa._confirm_inner = _boom
    try:
        for _ in range(mfa.ACCOUNT_FAIL_LIMIT + 3):
            assert _raised(mfa.confirm_enrollment, euid, "123456"), "the fault was swallowed"
    finally:
        mfa._confirm_inner = real_c
    assert mfa_row(euid).fail_count == 2 and mfa_row(euid).locked_until is None
    tick()
    assert mfa.confirm_enrollment(euid, code_for(euid)), "a right code sets it up afterwards"
    # (c) double-release guard: one fault releases exactly one attempt (3 held -> 3), and a
    #     count another request already reset (a success) is never pushed below zero
    _set_budget(fuid, 3)
    mfa._verify_inner = _boom
    try:
        assert _raised(mfa.verify, fuid, _wrong(fuid)), "the fault reaches the caller"
        assert mfa_row(fuid).fail_count == 3, mfa_row(fuid).fail_count

        def _reset_then_boom(uid, code):
            mfa._clear_failures(uid)  # a concurrent right code restarted the budget
            raise RuntimeError("database fault")

        mfa._verify_inner = _reset_then_boom
        assert _raised(mfa.verify, fuid, _wrong(fuid))
        assert mfa_row(fuid).fail_count == 0, mfa_row(fuid).fail_count
    finally:
        mfa._verify_inner = real_v
    # per address: a raising check from a pair that already holds 2 attempts gives back 1
    security._failures[security._lockout_key("fault@totp.test", "10.11.0.2")] = [time.time()] * 2
    c2, r = password_step(
        "fault@totp.test",
        USER_PW,
        client=TestClient(app, client=("10.11.0.2", 50000), raise_server_exceptions=False),
    )
    assert r.headers["location"] == "/auth/mfa" and _ip_entries("fault@totp.test", "10.11.0.2") == 2
    mfa._verify_inner = _boom
    try:
        assert post_code(c2, _wrong(fuid)).status_code == 500
    finally:
        mfa._verify_inner = real_v
    assert _ip_entries("fault@totp.test", "10.11.0.2") == 2, "released once, not twice"
    # (d) refused unchecked: in-flight attempts hold the whole account budget
    _set_budget(fuid, mfa.ACCOUNT_FAIL_LIMIT)
    checked = []

    def counting(uid, code):
        checked.append(code)
        return real_v(uid, code)

    rip = "10.11.0.3"
    c3, r = password_step("fault@totp.test", USER_PW, client=_ip_client(rip))
    assert r.headers["location"] == "/auth/mfa", r.headers
    n_locked, n_failed = len(audits("login.locked")), len(audits("mfa.failed"))
    mfa._verify_inner = counting
    try:
        tick()
        r = post_code(c3, code_for(fuid))
    finally:
        mfa._verify_inner = real_v
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?err=mfa_locked", (
        r.status_code,
        r.headers,
    )
    assert not signed_in(c3) and not checked, "refused without a check, not signed in"
    assert _ip_entries("fault@totp.test", rip) == 0, "no per-address failure counted"
    assert not security.login_locked_seconds("fault@totp.test", rip)
    assert len(audits("mfa.failed")) == n_failed, "not audited as a wrong code"
    assert len(audits("login.locked")) == n_locked + 1
    row = audits("login.locked")[0]
    assert row.actor == "fault@totp.test" and row.detail == "two-step sign-in", (
        row.actor,
        row.detail,
    )
    page = c3.get("/auth/login?err=mfa_locked")
    assert "locked for 15 minutes" in page.text, "the locked message, not 'not correct'"
    assert "That code is not correct" not in page.text
    assert mfa_row(fuid).fail_count == mfa.ACCOUNT_FAIL_LIMIT, "the refusal settled nothing"
    # (e) gate LOW-4: the Account-page REFUSED branch (budget still held): the wait message,
    #     no per-address failure, the row names the refusal, the account count untouched
    assert signed_in(c), "the session from (a)"
    reset_lockout()
    n_failed = len(audits("mfa.failed"))
    tick()
    r = c.post(
        "/account/mfa/recovery-codes",
        data={"code": code_for(fuid), "csrf_token": csrf(c)},
        follow_redirects=False,
    )
    assert r.headers.get("location") == "/account?err=mfa_wait", (r.status_code, r.headers)
    assert _ip_entries("fault@totp.test", ip) == 0, "no per-address failure on the Account page"
    assert len(audits("mfa.failed")) == n_failed + 1
    assert audits("mfa.failed")[0].detail == "code to replace recovery codes; locked"
    assert mfa_row(fuid).fail_count == mfa.ACCOUNT_FAIL_LIMIT, "the refusal settled nothing"
    # (f) gate LOW-4: forced set-up while the account budget is held: the locked path
    #     (login.locked), no per-address failure, no mfa.failed row, nothing set up
    before_policy = settings.get("mfa_required")
    settings.set_many({"mfa_required": "all"})
    try:
        nuid = make_user("fault.forced@totp.test", Role.TRANSLATOR)
        fip = "10.11.0.4"
        c4, r = password_step("fault.forced@totp.test", USER_PW, client=_ip_client(fip))
        assert r.headers["location"] == "/auth/mfa/enroll", r.headers
        assert c4.get("/auth/mfa/enroll").status_code == 200  # starts the enrollment row
        _set_budget(nuid, mfa.ACCOUNT_FAIL_LIMIT)
        n_locked, n_failed = len(audits("login.locked")), len(audits("mfa.failed"))
        tick()
        r = c4.post(
            "/auth/mfa/enroll",
            data={"code": code_for(nuid), "csrf_token": csrf(c4)},
            follow_redirects=False,
        )
        assert r.headers.get("location") == "/auth/login?err=mfa_locked", (
            r.status_code,
            r.headers,
        )
        assert _ip_entries("fault.forced@totp.test", fip) == 0, "no per-address failure"
        assert len(audits("login.locked")) == n_locked + 1
        assert len(audits("mfa.failed")) == n_failed, "not audited as a wrong code"
        assert not mfa.is_enrolled(nuid) and not signed_in(c4)
        # (g) gate LOW-1: NO enrollment row (never started, or an admin reset removed it)
        #     is "nothing to confirm" (None), never the locked answer
        with Session(engine) as s:
            s.delete(s.exec(select(UserMfa).where(UserMfa.user_id == nuid)).first())
            s.commit()
        assert mfa.confirm_enrollment(nuid, "123456") is None
        c5, r = password_step("fault.forced@totp.test", USER_PW, client=_ip_client("10.11.0.5"))
        assert r.headers["location"] == "/auth/mfa/enroll", r.headers
        with Session(engine) as s:  # the set-up page was never loaded: still no row
            assert s.exec(select(UserMfa).where(UserMfa.user_id == nuid)).first() is None
        n_locked = len(audits("login.locked"))
        r = c5.post(
            "/auth/mfa/enroll",
            data={"code": "123456", "csrf_token": csrf(c5)},
            follow_redirects=False,
        )
        assert r.status_code == 401 and "That code is not correct" in r.text, r.status_code
        assert len(audits("login.locked")) == n_locked, "no false lock row"
    finally:
        settings.set_many({"mfa_required": before_policy or "off"})
    # (h) gate LOW-3: Unicode digits (Arabic-Indic, superscript, one mixed in) pass
    #     str.isdigit(); they are now a plain wrong code, counted, never a 500
    _unlock(fuid)
    reset_lockout()
    uip = "10.11.0.6"
    c6, r = password_step("fault@totp.test", USER_PW, client=_ip_client(uip))
    assert r.headers["location"] == "/auth/mfa", r.headers
    odd = (
        "١٢٣٤٥٦",
        "¹²³⁴⁵⁶",
        "12345٦",
    )
    for i, bad in enumerate(odd, start=1):
        assert mfa.verify(fuid, bad) is None, bad.encode("unicode_escape")
        r = post_code(c6, bad)
        assert r.status_code == 401, (bad.encode("unicode_escape"), r.status_code)
        assert _ip_entries("fault@totp.test", uip) == i, "counted like any wrong code"
    assert mfa_row(fuid).fail_count == 2 * len(odd), "service and route both counted"
    mfa.begin_enrollment(nuid, USER_PW)
    assert mfa.confirm_enrollment(nuid, odd[0]) is None
    _unlock(fuid)
    reset_lockout()
    full_sign_in("fault@totp.test", USER_PW, fuid)
    reset_lockout()
    print("  G6: a raising code check gives both reservations back once (13 faults leave")
    print("      the counts as they were, no lock, a right code then signs in; set-up too;")
    print("      never below zero); a right code refused unchecked gets the locked message,")
    print("      login.locked, no per-address failure")


def check_policy_every_request(admin_uid: int):
    """LOW-3: a session without a proved second factor ends once the policy applies."""
    reset_lockout()
    settings.set_many({"mfa_required": "admins"})
    puid = make_user("promote@totp.test", Role.TRANSLATOR)
    c, r = password_step("promote@totp.test", USER_PW)
    assert r.headers["location"] == "/" and signed_in(c)
    assert c.get("/account", follow_redirects=False).status_code == 200
    with Session(engine) as s:
        security.set_role(s, puid, Role.ADMIN, org_id=None)
    r = c.get("/users", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert r.headers["location"].startswith("/auth/login?err=mfa_signin_again"), r.headers
    assert not signed_in(c) and audits("mfa.session_ended")[0].actor == "promote@totp.test"
    assert "signed out" in c.get(r.headers["location"]).text
    # signing in again goes through set-up
    c, r = password_step("promote@totp.test", USER_PW)
    assert r.headers["location"] == "/auth/mfa/enroll" and not signed_in(c)
    # a session that DID prove a code survives the policy being raised to all
    ac = full_sign_in(ADMIN, ADMIN_PW, admin_uid)
    settings.set_many({"mfa_required": "all"})
    assert ac.get("/account", follow_redirects=False).status_code == 200
    settings.set_many({"mfa_required": "admins"})
    print("  G3: promotion under admins (or a raised policy) ends an unverified session;")
    print("      a session that proved a code survives")


def check_no_store(admin_uid: int, tuid: int):
    """LOW-4: every page with a code form, the setup key or the recovery codes is no-store."""

    def _ok(r, what):
        cc = r.headers.get("cache-control", "")
        assert "no-store" in cc and r.headers.get("pragma") == "no-cache", (what, dict(r.headers))

    reset_lockout()
    c, _ = password_step("trans@totp.test", NEW_PW)
    _ok(c.get("/auth/mfa"), "code page")
    r = c.post("/auth/mfa", data={"code": _wrong(tuid), "csrf_token": csrf(c)})
    _ok(r, "code page error")
    settings.set_many({"mfa_required": "all"})
    nuid = make_user("nostore@totp.test", Role.TRANSLATOR)
    c, r = password_step("nostore@totp.test", USER_PW)
    assert r.headers["location"] == "/auth/mfa/enroll"
    _ok(c.get("/auth/mfa/enroll"), "forced set-up page")
    _, r = enroll_forced(c, nuid)
    _ok(r, "recovery codes (standalone)")
    settings.set_many({"mfa_required": "off"})
    sc, _ = password_step("promote@totp.test", USER_PW)
    assert signed_in(sc)
    _ok(sc.get("/account/mfa/setup"), "account set-up page")
    settings.set_many({"mfa_required": "admins"})
    reset_lockout()
    print("  G4: code, set-up and recovery-code pages send Cache-Control no-store + Pragma")


# ------------------------------------------------------------------------------------- A
def check_admin_reset(admin_client, admin_key, tuid):
    # the translator signs in (enrolled); the admin resets them from the Users page
    reset_lockout()
    tc = full_sign_in("trans@totp.test", NEW_PW, tuid)
    epoch0 = user_row("trans@totp.test").session_epoch
    page = admin_client.get("/users").text
    assert f'action="/users/{tuid}/mfa-reset"' in page and "Two-step sign-in" in page
    r = admin_client.post(
        f"/users/{tuid}/mfa-reset", data={"csrf_token": csrf(admin_client)}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/users?mfa_reset=1"
    assert user_row("trans@totp.test").session_epoch == epoch0 + 1, "reset bumps the epoch"
    assert mfa_row(tuid) is None and mfa.recovery_codes_left(tuid) == 0
    r = tc.get("/account", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login"), (
        "the translator's live session ended"
    )
    assert audits("mfa.reset")[0].target == "trans@totp.test"
    # a pending step dies with a reset too (epoch mismatch)
    settings.set_many({"mfa_required": "all"})
    c, r = password_step("trans@totp.test", NEW_PW)
    assert r.headers["location"] == "/auth/mfa/enroll"
    enroll_forced(c, tuid)
    c, r = password_step("trans@totp.test", NEW_PW)
    assert r.headers["location"] == "/auth/mfa"
    mfa.reset(tuid)
    r = c.get("/auth/mfa", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    settings.set_many({"mfa_required": "admins"})
    # /api: status then reset (org-ringed admin routes; the reset needs the admin scope, #270)
    api = TestClient(app)
    h = {"X-Loc-Token": admin_key}
    st = api.get(f"/api/users/{user_row(ADMIN).id}/mfa", headers=h).json()
    assert st["ok"] and st["enrolled"] and st["required"] and st["recovery_codes_left"] >= 1
    assert not any("secret" in k for k in st), st
    assert api.get("/api/users/999999/mfa", headers=h).status_code == 404
    luid = user_row("ldap.person@totp.test").id
    e0 = user_row("ldap.person@totp.test").session_epoch
    r = api.delete(f"/api/users/{luid}/mfa", headers=h)
    assert r.status_code == 200 and r.json() == {
        "ok": True,
        "email": "ldap.person@totp.test",
        "was_enrolled": True,
    }, r.text
    assert user_row("ldap.person@totp.test").session_epoch == e0 + 1 and mfa_row(luid) is None
    # a non-admin key is refused
    _, tkey = apikeys.create_key(tuid, name="t", scopes="docs admin")
    assert api.get(f"/api/users/{luid}/mfa", headers={"X-Loc-Token": tkey}).status_code == 403
    # CLI: status + reset (the host break-glass)
    code, out, err = run_cli(["--json", "user", "mfa-status", ADMIN])
    assert code == 0 and '"enrolled": true' in out, (code, out, err)
    code, out, _ = run_cli(["user", "mfa-status", "sso.person@totp.test"])
    assert code == 0 and "not applicable" in out, out
    e0 = user_row(ADMIN).session_epoch
    code, out, err = run_cli(["user", "mfa-reset", ADMIN, "--yes"])
    assert code == 0 and "reset for" in out, (code, out, err)
    assert user_row(ADMIN).session_epoch == e0 + 1 and not mfa.is_enrolled(user_row(ADMIN).id)
    cli_row = audits("mfa.reset")[0]
    assert cli_row.actor == "cli" and cli_row.target == ADMIN
    code, _, err = run_cli(["user", "mfa-reset", "nobody@totp.test", "--yes"])
    assert code == 2, (code, err)
    print("  A: reset on the Users page, DELETE /api and CLI deletes the factor and bumps the")
    print("     session epoch (live and pending sessions end); status on /api and CLI")


# ------------------------------------------------------------------------------------- U
def check_self_service():
    reset_lockout()
    settings.set_many({"mfa_required": "off"})
    vuid = make_user("viewer@totp.test", Role.VIEWER)
    c, r = password_step("viewer@totp.test", USER_PW)
    assert signed_in(c)
    acct = c.get("/account").text
    assert 'data-card="mfa"' in acct and "/account/mfa/setup" in acct
    page = c.get("/account/mfa/setup")
    assert page.status_code == 200 and "Setup key" not in page.text
    assert mfa.pending_secret(vuid) is None
    denied = c.post("/account/mfa/setup", data={"csrf_token": csrf(c)})
    assert denied.status_code == 400 and mfa.pending_secret(vuid) is None
    page = c.post("/account/mfa/setup", data={"current": USER_PW, "csrf_token": csrf(c)})
    assert page.status_code == 200 and "Setup key" in page.text
    assert mfa.format_secret(secret_of(vuid)) in page.text
    r = c.post("/account/mfa/setup", data={"code": "000000", "csrf_token": csrf(c)})
    assert r.status_code == 400 and not mfa.is_enrolled(vuid)
    tick()
    r = c.post("/account/mfa/setup", data={"code": code_for(vuid), "csrf_token": csrf(c)})
    assert r.status_code == 200 and "Save your recovery codes" in r.text, r.text[:300]
    assert mfa.is_enrolled(vuid) and audits("mfa.enroll")[0].detail == "self-service"
    # new recovery codes need a current code
    r = c.post(
        "/account/mfa/recovery-codes",
        data={"code": "999999", "csrf_token": csrf(c)},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/account?err=mfa_regen_code"
    err_page = c.get("/account?err=mfa_regen_code").text
    assert 'aria-invalid="true"' in err_page.split('id="mfa-regen-code"', 1)[1].split(">", 1)[0]
    old = [x.id for x in _codes(vuid)]
    tick()
    r = c.post("/account/mfa/recovery-codes", data={"code": code_for(vuid), "csrf_token": csrf(c)})
    assert (
        r.status_code == 200
        and len(re.findall(r"<code>[a-z2-9]{5}-[a-z2-9]{5}</code>", r.text)) == 10
    )
    assert not set(old) & {x.id for x in _codes(vuid)}, "the old set is gone"
    # turning off: refused while the policy requires it, then needs a code
    settings.set_many({"mfa_required": "all"})
    tick()
    r = c.post(
        "/account/mfa/disable",
        data={"code": code_for(vuid), "current": USER_PW, "csrf_token": csrf(c)},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/account?err=mfa_disable_required" and mfa.is_enrolled(vuid)
    settings.set_many({"mfa_required": "off"})
    r = c.post(
        "/account/mfa/disable",
        data={"code": "000000", "current": USER_PW, "csrf_token": csrf(c)},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/account?err=mfa_disable_code" and mfa.is_enrolled(vuid)
    # Issue #546: the correct current password must not clear the failed-code count, or the
    # per-address code budget never trips on this route. Wrong codes with a right password
    # are locked out within LOCKOUT_THRESHOLD tries.
    reset_lockout()
    seen = []
    for _ in range(security.LOCKOUT_THRESHOLD + 2):
        r = c.post(
            "/account/mfa/disable",
            data={"code": "000000", "current": USER_PW, "csrf_token": csrf(c)},
            follow_redirects=False,
        )
        seen.append(r.headers["location"])
    assert "/account?err=mfa_wait" in seen, ("disable code budget never tripped", seen)
    assert mfa.is_enrolled(vuid)
    reset_lockout()
    _unlock(vuid)
    tick()
    r = c.post(
        "/account/mfa/disable",
        data={"code": code_for(vuid), "current": USER_PW, "csrf_token": csrf(c)},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/account?saved=mfa_off#mfa"
    assert not mfa.is_enrolled(vuid) and not _codes(vuid) and audits("mfa.disable")
    # an SSO account sees no set-up
    sso_c = TestClient(app)
    real = (oidc.enabled, oidc.complete_login, oidc.stored_next)
    try:
        oidc.enabled = lambda: True
        # identity-01: the account was bound to (iss, sub) in check X; this sign-in matches it.
        oidc.complete_login = lambda request, code, state: (
            {"iss": "https://idp.test", "sub": "s-totp", "email": "sso.person@totp.test"},
            None,
        )
        oidc.stored_next = lambda request: "/"
        sso_c.get("/auth/oidc/callback?code=x&state=y")
    finally:
        oidc.enabled, oidc.complete_login, oidc.stored_next = real
    assert "identity provider" in sso_c.get("/account").text
    assert sso_c.get("/account/mfa/setup").status_code == 400
    settings.set_many({"mfa_required": "admins"})
    reset_lockout()
    print("  U: a viewer sets up, replaces codes (code needed), turns off (code needed;")
    print("     refused while required); SSO accounts get nothing to set up")


def _codes(uid):
    with Session(engine) as s:
        return s.exec(select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == uid)).all()


def _shown_secret(page) -> str:
    """The set-up key a page showed, without spaces ("" when none)."""
    found = re.search(r"([A-Z2-7]{4}(?: [A-Z2-7]{4}){7})", page.text)
    return found.group(1).replace(" ", "") if found else ""


def check_setup_secret_per_flow():
    """identity-02: an unconfirmed set-up secret belongs to the ONE set-up flow that showed
    it. A person who knows the password and opens forced set-up, then leaves, must not have
    planted the secret the real owner later scans: every new password step (and every new
    self-service grant) gets a fresh secret, a reload in the same flow keeps its own, and a
    code from an abandoned flow neither confirms another flow nor signs in afterwards."""
    reset_lockout()
    settings.set_many({"mfa_required": "all"})
    puid = make_user("plant@totp.test", Role.TRANSLATOR)
    first, r = password_step("plant@totp.test", USER_PW)
    assert r.headers["location"] == "/auth/mfa/enroll", r.headers
    s1 = _shown_secret(first.get("/auth/mfa/enroll"))
    assert s1 and s1 == secret_of(puid)
    assert _shown_secret(first.get("/auth/mfa/enroll")) == s1, "a reload keeps its own secret"
    # The first person leaves. The owner signs in: a NEW secret, the old one never shown.
    owner, r = password_step("plant@totp.test", USER_PW)
    assert r.headers["location"] == "/auth/mfa/enroll", r.headers
    page = owner.get("/auth/mfa/enroll")
    s2 = _shown_secret(page)
    assert s2 and s2 != s1, "the second set-up flow was shown the first flow's secret"
    assert mfa.format_secret(s1) not in page.text
    # A code from the abandoned secret confirms nothing (from either flow).
    tick()
    stale = mfa.totp(s1, _CLOCK[0])
    r = first.post(
        "/auth/mfa/enroll", data={"code": stale, "csrf_token": csrf(first)}, follow_redirects=False
    )
    assert r.status_code == 401 and not mfa.is_enrolled(puid), r.status_code
    reset_lockout()
    _unlock(puid)
    # The first flow's own page after the owner's flow began does not show the owner's secret.
    assert mfa.format_secret(s2) not in r.text
    # Its error page started a newer flow; the owner's page re-renders a fresh one too, so
    # sign the owner in once more and confirm in a single, uninterrupted flow.
    owner, r = password_step("plant@totp.test", USER_PW)
    s3 = _shown_secret(owner.get("/auth/mfa/enroll"))
    assert s3 and s3 not in (s1, s2)
    tick()
    r = owner.post(
        "/auth/mfa/enroll",
        data={"code": mfa.totp(s3, _CLOCK[0]), "csrf_token": csrf(owner)},
        follow_redirects=False,
    )
    assert r.status_code == 200 and mfa.is_enrolled(puid), (r.status_code, r.text[:300])
    # The abandoned secret does not sign in.
    reset_lockout()
    later, r = password_step("plant@totp.test", USER_PW)
    assert r.headers["location"] == "/auth/mfa"
    tick()
    r = post_code(later, mfa.totp(s1, _CLOCK[0]))
    assert r.status_code == 401 and not signed_in(later), r.status_code
    reset_lockout()
    _unlock(puid)
    # Self-service: two sessions of one account each re-authenticate; the second gets a
    # different secret and the first one's code does not confirm.
    settings.set_many({"mfa_required": "off"})
    suid = make_user("plant.self@totp.test", Role.VIEWER)
    a, _ = password_step("plant.self@totp.test", USER_PW)
    b, _ = password_step("plant.self@totp.test", USER_PW)
    assert signed_in(a) and signed_in(b)
    sa = _shown_secret(
        a.post("/account/mfa/setup", data={"current": USER_PW, "csrf_token": csrf(a)})
    )
    sb = _shown_secret(
        b.post("/account/mfa/setup", data={"current": USER_PW, "csrf_token": csrf(b)})
    )
    assert sa and sb and sa != sb, "two set-up grants shared one secret"
    tick()
    r = a.post("/account/mfa/setup", data={"code": mfa.totp(sa, _CLOCK[0]), "csrf_token": csrf(a)})
    assert not mfa.is_enrolled(suid), "a code from a superseded set-up turned the factor on"
    assert mfa.format_secret(sb) not in r.text
    reset_lockout()
    _unlock(suid)
    # The superseded session's attempt did not disturb the newer flow: it confirms.
    assert secret_of(suid) == sb
    tick()
    r = b.post("/account/mfa/setup", data={"code": mfa.totp(sb, _CLOCK[0]), "csrf_token": csrf(b)})
    assert r.status_code == 200 and mfa.is_enrolled(suid), (r.status_code, r.text[:300])
    settings.set_many({"mfa_required": "admins"})
    reset_lockout()
    print("  F: identity-02: a set-up secret belongs to one flow; a new password step or")
    print("     grant gets a fresh one; an abandoned secret confirms and signs in nothing")


def _minted_session(uid: int, *, verified: bool = False):
    """A client holding a server-signed session for `uid`, minted the way a SAML sign-in
    mints it (security.login: no second-factor mark unless `verified`)."""
    import itsdangerous

    from app import config

    row = user_row_by_id(uid)
    now = int(time.time())
    payload = {"uid": uid, "se": row.session_epoch, "la": now, "iat": now}
    if verified:
        payload["mf"] = 1
    signer = itsdangerous.TimestampSigner(str(config.SESSION_SECRET))
    raw = base64.b64encode(json.dumps(payload).encode("utf-8"))
    c = TestClient(app)
    c.cookies.set("session", signer.sign(raw).decode("utf-8"), domain="testserver.local", path="/")
    return c


def user_row_by_id(uid: int) -> User:
    with Session(engine) as s:
        return s.get(User, uid)


def check_idp_linked_account_has_no_local_factor():
    """#423 (identity-04): a local row the IdP adopted by SCIM (a SCIM link, no password)
    signs in only through SAML. It is an identity-provider account in fact: two-step sign-in
    here does not apply, set-up is refused (a session thief used to enroll a factor with no
    proof, which then locked the owner out of SAML), and mfa_required=all does not end its
    SAML sessions. A local row with no password and no SCIM link is refused set-up too."""
    reset_lockout()
    settings.set_many({"mfa_required": "off"})
    u = security.create_user("idp.linked@totp.test", "Idp", Role.TRANSLATOR, "")
    with Session(engine) as s:  # what a SCIM adoption of the pre-created row writes
        row = s.get(User, u.id)
        row.external_id = "idp-0423"
        s.add(row)
        s.commit()
    linked = user_row("idp.linked@totp.test")
    assert security.auth_source_of(linked) == "local" and not linked.password_hash
    assert not mfa.applies_to(linked) and mfa.status(linked)["applies"] is False
    # SAML still signs it in (and keeps it "local")
    got, err = security.get_or_create_saml_user(linked.email, "Idp")
    assert got is not None and err is None, err
    c = _minted_session(u.id)
    page = c.get("/account/mfa/setup", follow_redirects=False)
    assert page.status_code == 400 and not _shown_secret(page), page.status_code
    assert mfa.pending_secret(u.id) is None, "a set-up secret was stored with no proof"
    r = c.post("/account/mfa/setup", data={"code": "123456", "csrf_token": csrf(c)})
    assert r.status_code == 400 and not mfa.is_enrolled(u.id), r.status_code
    assert mfa.pending_secret(u.id) is None
    # mfa_required=all: its SAML session is not ended on the first page (no wall loop)
    settings.set_many({"mfa_required": "all"})
    assert not mfa.required_for(user_row(linked.email))
    r = _minted_session(u.id).get("/account", follow_redirects=False)
    assert r.status_code == 200, (r.status_code, r.headers.get("location"))
    got, err = security.get_or_create_saml_user(linked.email, "Idp")
    assert got is not None and err is None, err
    # #423 gate: a factor set up on such a row BEFORE the fix (planted by a session thief)
    # guards nothing (no local sign-in) and must not lock the owner out of SAML.
    mfa._begin_enrollment(u.id)
    tick()
    assert mfa.confirm_enrollment(u.id, code_for(u.id)) and mfa.is_enrolled(u.id)
    got, err = security.get_or_create_saml_user(linked.email, "Idp")
    assert got is not None and err is None, f"a stale factor locked the owner out: {err}"
    assert not mfa.required_for(user_row(linked.email))
    mfa.reset(u.id)
    # A SCIM-linked row that DOES have a local password keeps local two-step sign-in.
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.password_hash = security.hash_password(USER_PW)
        s.add(row)
        s.commit()
    assert mfa.applies_to(user_row(linked.email)) and mfa.required_for(user_row(linked.email))
    # A local row with no password and no SCIM link: set-up is refused, not skipped
    settings.set_many({"mfa_required": "off"})
    bare = security.create_user("no.password@totp.test", "", Role.VIEWER, "")
    assert mfa.applies_to(user_row(bare.email))
    c = _minted_session(bare.id, verified=True)
    page = c.get("/account/mfa/setup", follow_redirects=False)
    assert page.status_code == 400 and not _shown_secret(page), page.status_code
    assert mfa.pending_secret(bare.id) is None
    r = c.post("/account/mfa/setup", data={"current": "x", "csrf_token": csrf(c)})
    assert r.status_code == 400 and mfa.pending_secret(bare.id) is None, r.status_code
    try:
        mfa.begin_enrollment(bare.id)
        raise AssertionError("set-up started with no password to prove")
    except mfa.MfaError as exc:
        assert "no password on this site" in str(exc)
    settings.set_many({"mfa_required": "admins"})
    reset_lockout()
    print("  I: #423: a SCIM-linked no-password row is an IdP account (no local factor, no")
    print("     wall loop, SAML works); no-password set-up is refused, not skipped")


# ------------------------------------------------------------------------------------- K
def check_setting_audit_gdpr(admin_client):
    before = settings.get("mfa_required")
    r = admin_client.post(
        "/security",
        data={"mfa_required": "sometimes", "csrf_token": csrf(admin_client)},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/security?err=mfa_required_value"
    assert settings.get("mfa_required") == before
    page = admin_client.get("/security?err=mfa_required_value").text
    assert 'aria-invalid="true"' in page.split('id="mfa-required"', 1)[1].split(">", 1)[0]
    # a full Security form post (every card) that changes only mfa_required
    settings.set_many({"mfa_required": "off"})
    r = admin_client.post(
        "/security",
        data={
            "mfa_required": "all",
            "security_auth_required": "1",
            "audit_enabled": "1",
            "csrf_token": csrf(admin_client),
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/security?saved=1", r.headers
    assert settings.get("mfa_required") == "all"
    row = audits("security.update")[0]
    assert "mfa_required old=off new=all" in row.detail, row.detail
    code, out, err = run_cli(["config", "set", "mfa_required", "bogus", "--yes"])
    assert code != 0 and settings.get("mfa_required") == "all", (code, out, err)
    code, out, err = run_cli(["config", "set", "mfa_required", "admins", "--yes"])
    assert code == 0 and settings.get("mfa_required") == "admins", (code, out, err)
    assert "mfa_required" in settings.SENSITIVE_KEYS
    assert settings.DEFAULTS["mfa_required"] == "off"
    # never a code or a secret in the audit trail
    with Session(engine) as s:
        keys_now = [mfa._decrypt(r.secret_enc) for r in s.exec(select(UserMfa)).all()]
        trail = " ".join(
            f"{a.actor} {a.action} {a.target} {a.detail}" for a in s.exec(select(AuditLog))
        )
    for sec in filter(None, keys_now):
        assert sec not in trail
    for handled in _SEEN:
        assert handled not in trail, f"a code reached the audit trail: {handled}"
        assert handled.replace("-", "") not in trail
    # GDPR erase deletes the factor
    euid = make_user("erase.me@totp.test", Role.TRANSLATOR)
    mfa.begin_enrollment(euid, USER_PW)
    tick()
    assert mfa.confirm_enrollment(euid, code_for(euid))
    gdpr.erase_user(euid, actor=ADMIN)
    assert mfa_row(euid) is None and not _codes(euid)
    print("  K: setting validated + audited on the Security page and CLI (sensitive);")
    print("     no code or secret in the audit trail; GDPR erase deletes the factor")


# ------------------------------------------------------------------------------------- Z
def check_timing():
    calls = []
    real = sec_core.verify_password

    def counting(h, pw):
        calls.append(h)
        return real(h, pw)

    sec_core.verify_password = counting
    try:
        assert security.authenticate("nobody-at-all@totp.test", "whatever pw") is None
        assert len(calls) == 1, "an unknown email still runs one Argon2 verify"
        calls.clear()
        assert security.authenticate("sso.person@totp.test", "whatever pw") is None
        assert len(calls) == 1, "an account without a local password runs one too"
        calls.clear()
        with Session(engine) as s:
            u = s.exec(select(User).where(User.email == "viewer@totp.test")).first()
            u.active = False
            s.add(u)
            s.commit()
        assert security.authenticate("viewer@totp.test", USER_PW) is None
        assert len(calls) == 1, "a disabled account runs one too"
        calls.clear()
        assert security.authenticate(ADMIN, ADMIN_PW) is not None and len(calls) == 1
        calls.clear()
        assert mfa.verify(user_row("viewer@totp.test").id, "123456") is None
        assert len(calls) == 1, "the code step with nothing to compare runs one verify"
    finally:
        sec_core.verify_password = real
    # and it is measurably not the fast path any more (loose bound, CI-safe)
    t0 = time.perf_counter()
    security.authenticate("nobody-else@totp.test", "x")
    miss = time.perf_counter() - t0
    t0 = time.perf_counter()
    security.authenticate(ADMIN, "not the password")
    hit = time.perf_counter() - t0
    assert miss > hit * 0.3, (miss, hit)
    print(
        f"  Z: unknown/disabled/no-password accounts run a dummy verify (miss {miss * 1000:.0f} ms,"
    )
    print(f"     known {hit * 1000:.0f} ms); the code step does too when nothing is stored")


def check_forced_enroll_continue_link():
    """Security review 2026-10 web-01: the forced set-up page renders the sanitized `next`
    into its "continue" link. Browsers drop TAB/LF inside a URL, so a `next` of
    '/<TAB>/evil.example' became the off-site '//evil.example' after a real sign-in. The
    sanitizer must answer '/' for any control character, so the link is a same-site path."""
    before = settings.get("mfa_required")
    settings.set_many({"mfa_required": "all"})
    try:
        for i, bad in enumerate(("/\t/evil.example/landing", "/\n/evil.example/landing")):
            email = f"webnext{i}@totp.test"
            uid = make_user(email, Role.LINGUIST)
            reset_lockout()
            c, r = password_step(email, USER_PW, nxt=bad)
            assert r.headers["location"] == "/auth/mfa/enroll", r.headers
            _codes, page = enroll_forced(c, uid)
            hrefs = re.findall(r'<a class="button" href="([^"]*)"', page.text)
            assert hrefs, page.text[:400]
            for href in hrefs:
                assert "evil.example" not in href, ("continue link left the site", href)
                assert not any(ord(ch) < 0x20 for ch in href), ("control char in href", href)
    finally:
        settings.set_many({"mfa_required": before or "off"})
        reset_lockout()
    print("  W: forced set-up 'continue' link stays same-site for a TAB/LF next (web-01)")


def main():
    init_db()
    check_totp_unit()
    check_migration()
    codes = check_setup_forced()
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    admin_uid = user_row(ADMIN).id
    admin_client = check_no_session_before_code(admin_uid)
    check_replay(admin_uid)
    check_lockout_and_expiry(admin_uid)
    check_recovery_codes(admin_uid, codes)
    tuid = check_policy_and_invite(admin_client)
    admin_key = check_sso_ldap_apikey(admin_uid)
    luid = user_row("ldap.person@totp.test").id
    check_account_budget(tuid, luid)
    check_sso_takeover_refused(luid)
    check_policy_every_request(admin_uid)
    check_no_store(admin_uid, tuid)
    check_concurrency_and_recovery_bypass()
    check_fault_release_and_refused()
    admin_client = full_sign_in(ADMIN, ADMIN_PW, admin_uid)
    check_admin_reset(admin_client, admin_key, tuid)
    check_self_service()
    check_setup_secret_per_flow()
    check_idp_linked_account_has_no_local_factor()
    # the CLI reset signed the admin out; sign in again (not enrolled now, admins required)
    reset_lockout()
    c, r = password_step(ADMIN, ADMIN_PW)
    assert r.headers["location"] == "/auth/mfa/enroll"
    enroll_forced(c, admin_uid)
    check_setting_audit_gdpr(c)
    check_recovery_page_timeout()
    check_forced_enroll_continue_link()
    check_timing()
    print("TOTP MFA OK")


if __name__ == "__main__":
    main()
