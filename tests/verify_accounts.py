"""Multi-user accounts verification (offline).

- seed_initial() creates admin/admin (super admin) + user1/user1 (standard) and
  turns on the login wall
- login wall: anonymous -> redirect to login
- admin reaches admin areas; standard user is blocked from them but can use the app
- self-service password change, then re-login with the new password
- viewer self-service: the four /account settings POSTs work for a viewer, own row only
- email-invite handshake: admin invites -> token link -> accept sets password ->
  new account can log in and is role-restricted
"""

import json
import logging
import os
import re
import secrets
import sys
import time
from base64 import b64encode

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_acct_")

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.orm.attributes import set_committed_value

from app import config
from app.db import init_db
from app.main import app
from app.models import User
from app.services import audit as audit_mod
from app.services import security, settings
from app.services import security_client_ip_one_authority_ as sec_mod
from tests._csrf import csrf_for, session_data, sign_out

# The exact codec starlette's SessionMiddleware uses (see tests/_csrf), so the
# #138 section can MINT a cookie the server accepts as authentic - the only way
# to forge the "legacy cookie with uid but no epoch" case.
_signer = itsdangerous.TimestampSigner(str(config.SESSION_SECRET))


def sign_session(payload: dict) -> str:
    return _signer.sign(b64encode(json.dumps(payload).encode("utf-8"))).decode("utf-8")


def replay(cookie: str, path: str = "/projects"):
    """Replay a captured/forged cookie on a FRESH client (a stolen-cookie attacker:
    no other state, just the bytes)."""
    rc = TestClient(app)
    rc.cookies.set("session", cookie)
    return rc.get(path, follow_redirects=False)


_REAL_SESSION = sec_mod.Session


class StaleSnapshotSession(_REAL_SESSION):
    """Force the classic read-modify-write interleaving DETERMINISTICALLY (#138 HIGH-1).

    Two racing revocations both READ epoch N, both WRITE N+1: the second logout or reset
    is then a silent no-op, so a cookie stamped N+1 between them survives it. No threads
    and no timing here - this Session pins whatever the ORM hands back for a User to the
    snapshot taken before the FIRST revocation, which is exactly what the losing racer
    sees. A bump that increments in SQL never reads the row into Python at all, so the
    pin is inert for it: that difference IS the test."""

    pinned = None

    def get(self, entity, ident, *args, **kwargs):
        row = super().get(entity, ident, *args, **kwargs)
        if row is not None and entity is User and StaleSnapshotSession.pinned is not None:
            # set_committed_value, NOT an assignment: the row must look as though it was
            # READ at the older epoch, without becoming dirty (an assignment would make
            # the ORM write the stale value back, which is the harness cheating).
            set_committed_value(row, "session_epoch", StaleSnapshotSession.pinned)
        return row


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def login(c, who, pw):
    # Auth is on here: the login POST itself needs a CSRF token (mint an anon one
    # if the session was just cleared by a logout), and a successful login rotates
    # the session, so refresh the client's token afterwards.
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def _self_service_round(c, cur_pw: str, new_pw: str, target) -> list:
    """The five /account settings POSTs, each also naming ANOTHER account (``target``)
    in the form and the query. Returns the redirect locations in order."""
    from app.i18n.loader import available_locales

    other = {"user_id": str(target.id), "uid": str(target.id), "email": target.email}
    q = f"?user_id={target.id}&email={target.email}"
    locale = next(code for code in available_locales() if code != "en")
    posts = [
        ("/account/name", {"name": "Self Named"}),
        ("/account/locale", {"ui_locale": locale}),
        ("/account/get-started", {}),
        ("/account/theme", {"theme": "light"}),
        ("/account/password", {"current": "not-my-password", "new": new_pw, "confirm": new_pw}),
        ("/account/password", {"current": cur_pw, "new": new_pw, "confirm": new_pw}),
    ]
    return [
        c.post(path + q, data={**form, **other}, follow_redirects=False).headers.get("location")
        for path, form in posts
    ]


def _viewer_self_service() -> None:
    """Viewer self-service train (09-26): a viewer (rank 0, below can_write) may
    POST exactly the five /account settings forms, each acting on their OWN row only;
    every real write, /account/apikeys and any near-miss path stays refused."""
    from sqlmodel import Session, select

    from app import main as app_main
    from app.db import engine
    from app.models import AuditLog

    vpw, tpw = (
        secrets.token_urlsafe(18),
        secrets.token_urlsafe(18),
    )  # pregate: allow-secret random per-run fixture
    viewer = security.create_user("viewer-ss@x.com", "Viewer Before", "viewer", vpw)
    trans = security.create_user("trans-ss@x.com", "Trans Before", "translator", tpw)

    def row(uid):
        with Session(engine) as s:
            u = s.get(User, uid)
            return (
                u.name,
                u.ui_locale,
                u.onboarding_dismissed_at,
                u.password_hash,
                u.session_epoch,
                u.ui_theme,
            )

    trans_before = row(trans.id)
    v = TestClient(app)
    assert login(v, viewer.email, vpw).status_code == 303
    assert v.get("/account").status_code == 200

    # Control first: without the new allowlist the rank gate refuses the viewer (the
    # defect this train fixes), so the passes below are the allowlist's doing.
    real = app_main._ACCOUNT_SELF_SERVICE_POSTS
    app_main._ACCOUNT_SELF_SERVICE_POSTS = frozenset()
    try:
        for path in (
            "/account/password",
            "/account/name",
            "/account/locale",
            "/account/get-started",
            "/account/theme",
        ):
            r = v.post(path, data={"name": "x", "theme": "dark"}, follow_redirects=False)
            assert r.status_code == 403, (
                "control: the rank gate refuses a viewer",
                path,
                r.status_code,
            )
    finally:
        app_main._ACCOUNT_SELF_SERVICE_POSTS = real
    assert row(viewer.id)[0] == "Viewer Before"

    # CSRF is unchanged on the newly admitted routes and on /auth/password-change: a POST
    # with no token is sent to sign in again and changes nothing.
    tok = v.headers.pop("X-CSRF-Token")
    before = row(viewer.id)
    for path in ("/account/password", "/auth/password-change"):
        r = v.post(
            path,
            data={"current": vpw, "new": "no-token-pass-123", "confirm": "no-token-pass-123"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "expired=1" in r.headers["location"], (path, r.status_code)
    r = v.post("/account/name", data={"name": "No Token"}, follow_redirects=False)
    assert "expired=1" in r.headers["location"]
    assert row(viewer.id) == before, "a CSRF-less POST must change nothing"
    v.headers["X-CSRF-Token"] = tok
    print("  viewer self-service: CSRF still required on /account/* and /auth/password-change OK")

    new_vpw = secrets.token_urlsafe(18)
    locs = _self_service_round(v, vpw, new_vpw, trans)
    assert locs[:4] == [
        "/account?saved=name",
        "/account?saved=locale",
        "/account?saved=getstarted",
        "/account?saved=theme",
    ], locs
    assert locs[4] == "/account?saved=pwfail", ("the current password is still checked", locs)
    assert locs[5] == "/account?saved=pw", locs
    name, locale, flag, pw_hash, epoch, theme = row(viewer.id)
    # A viewer is not eligible for Get started: the POST is admitted but changes nothing.
    assert (name, flag) == ("Self Named", None) and locale and locale != "en"
    assert theme == "light", "a viewer sets their OWN appearance (ADR 0063)"
    assert pw_hash != before[3] and epoch > before[4]
    assert row(trans.id) == trans_before, "a form/query naming another account must not touch it"
    # The session that changed the password stays signed in; the old password is dead.
    assert v.get("/account").status_code == 200
    r = v.post(
        "/account/password",
        data={"current": new_vpw, "new": "short", "confirm": "short"},
        follow_redirects=False,
    )
    assert "err=Password%20must%20be%20at%20least%2012" in r.headers["location"], (
        "policy still applies"
    )
    sign_out(v, follow_redirects=False)
    assert login(v, viewer.email, vpw).status_code == 401
    assert login(v, viewer.email, new_vpw).status_code == 303
    print(
        "  viewer self-service: name/locale/get-started/theme/password change their OWN row only OK"
    )

    # Still refused: a real write, API keys, and near-miss paths (exact allowlist).
    assert v.post("/projects", data={"name": "Nope"}, follow_redirects=False).status_code == 403
    assert v.post("/account/apikeys", data={"name": "k"}, follow_redirects=False).status_code == 403
    for path in (
        "/account/password/x",
        "/account/passwordx",
        "/account/password/",
        "/account/names",
    ):
        assert v.post(path, data={"name": "x"}, follow_redirects=False).status_code == 403, path
    for method, path in (
        ("POST", "/account/password/x"),
        ("POST", "/account/passwordx"),
        ("POST", "/account/password/"),
        ("POST", "/account/password\n"),
        ("POST", "/account/apikeys"),
        ("POST", "/account"),
        ("PUT", "/account/password"),
        ("DELETE", "/account/name"),
        ("PATCH", "/account/get-started"),
        ("PUT", "/account/theme"),
        ("POST", "/account/themes"),
    ):
        assert not app_main._self_service_writable(method, path), (method, path)
    for path in (
        "/account/password",
        "/account/name",
        "/account/locale",
        "/account/get-started",
        "/account/theme",
    ):
        assert app_main._self_service_writable("POST", path), path
    assert row(viewer.id)[0] == "Self Named"
    print("  viewer self-service: /projects, /account/apikeys and near-miss paths still 403 OK")

    # The audit trail a viewer leaves is the same shape as a translator's for the same
    # round: actor = the user, same actions, targets and details.
    t = TestClient(app)
    assert login(t, trans.email, tpw).status_code == 303
    assert _self_service_round(t, tpw, secrets.token_urlsafe(18), viewer) == locs

    def trail(email):
        with Session(engine) as s:
            rows = s.exec(
                select(AuditLog).where(AuditLog.actor == email).order_by(AuditLog.id)
            ).all()
        wanted = [r for r in rows if r.action.startswith("account.") or "/account/" in r.action]
        return [(r.action, r.target, r.detail) for r in wanted]

    vt = trail(viewer.email)
    names = [a for a, _t, _d in vt]
    for action in ("account.update_name", "account.update_locale", "account.update_theme"):
        assert action in names, (action, vt)
    assert ("account.change_password", "", "ok") in vt and (
        "account.change_password",
        "",
        "failed",
    ) in vt
    tt = trail(trans.email)
    # The viewer's trail also holds the control 403s, the CSRF refusals and the policy
    # refusal above; compare the matching round, from its first name change on.
    assert tt, "the translator round must leave audit rows"
    first = next(i for i, e in enumerate(vt) if e[0] == "account.update_name")
    assert vt[first : first + len(tt)] == tt, ("viewer and translator trails differ", vt, tt)
    print("  viewer self-service: audit rows match a translator's (actor = the user) OK")


def _expired_invite_delete(c) -> None:
    """SPEC-expired-invite-delete (09-24): Delete on an Expired invitations row is a
    SOFT delete through the existing revoke route, behind the shared confirm prompt.
    ``c`` is the signed-in admin client with its CSRF header. Expiry is set directly
    (days=-1, or expires_at moved into the past), never by sleeping."""
    import io
    from contextlib import redirect_stdout
    from datetime import datetime, timedelta

    from sqlmodel import Session, select

    from app.cli.__main__ import main as cli_main
    from app.db import engine
    from app.models import AuditLog, Invite

    password = secrets.token_urlsafe(24)  # pregate: allow-secret random per-run fixture
    # An address that needs escaping in the confirm attribute (#179: no raw markup).
    gone = security.create_invite("o'lapsed\"<i>@x.com", "translator", "admin", days=-1)
    page = c.get("/users").text
    escaped = "o&#39;lapsed&#34;&lt;i&gt;@x.com"
    assert f'aria-label="Delete invitation for {escaped}">Delete</button>' in page, (
        "Delete button must carry the escaped address in its accessible name"
    )
    assert (
        f'data-action-confirm="Delete the expired invitation for {escaped}?'
        ' The old link stays dead. You can send a new invite later."' in page
    ), "the confirm message must use the shared data-action-confirm pattern, escaped"
    assert "<i>@x.com" not in page and 'onclick="return confirm' not in page
    assert f"/users/invite/{gone.id}/revoke" in page

    # CSRF is required: without the token nothing is revoked and nothing is audited.
    csrf_header = c.headers.pop("X-CSRF-Token")
    r = c.post(f"/users/invite/{gone.id}/revoke", follow_redirects=False)
    assert r.status_code == 303 and "expired=1" in r.headers["location"], r.headers
    c.headers["X-CSRF-Token"] = csrf_header  # pregate: allow-secret restores the test header
    assert gone.id in {i.id for i in security.expired_invites()}

    r = c.post(f"/users/invite/{gone.id}/revoke", follow_redirects=False)
    assert r.status_code == 303 and "deleted=" in r.headers["location"], r.headers
    assert "revoked=" not in r.headers["location"]
    page = c.get(r.headers["location"]).text
    assert f'role="status">Invitation deleted for <strong>{escaped}</strong>.' in page
    assert f"/users/invite/{gone.id}/revoke" not in page and f'value="{gone.id}"' not in page
    assert gone.id not in {i.id for i in security.expired_invites()}
    assert gone.id not in {i.id for i in security.pending_invites()}
    # The old email link: GET and POST both render the invalid/expired page, no account.
    anon = TestClient(app)
    assert "Invitation invalid" in anon.get(f"/auth/accept/{gone.token}").text
    csrf_for(anon)
    r = anon.post(
        f"/auth/accept/{gone.token}",
        data={
            "name": "Lapsed",
            "password": password,  # pregate: allow-secret random per-run fixture
            "confirm": password,
        },
        follow_redirects=False,
    )
    assert r.status_code == 200 and "Invitation invalid" in r.text
    assert security.get_by_email(gone.email) is None, "a deleted invite created an account"
    assert security.accept_invite(gone.token, "Lapsed", password) is None
    # A second Delete, and Resend of the deleted row, are clean 404s.
    assert c.post(f"/users/invite/{gone.id}/revoke", follow_redirects=False).status_code == 404
    r = c.post(
        "/users/invite",
        data={"email": gone.email, "role": gone.role, "resend_id": gone.id},
        follow_redirects=False,
    )
    assert r.status_code == 404
    with Session(engine) as session:
        row = session.get(Invite, gone.id)
        assert row is not None and row.revoked_at is not None, "must be a SOFT delete"
        assert not row.accepted
        events = session.exec(
            select(AuditLog).where(
                AuditLog.action == "user.invite.revoke", AuditLog.target == gone.email
            )
        ).all()
        assert len(events) == 1, events
        assert events[0].detail == "expired" and events[0].actor == "admin" and events[0].ip

    # A pending invite that lapsed later (expires_at moved into the past) is deletable
    # too, and a still-pending Revoke keeps its plain audit detail and ?revoked= line.
    lapsed = security.create_invite("lapsed-later@x.com", "viewer", "admin")
    live = security.create_invite("still-live@x.com", "viewer", "admin")
    with Session(engine) as session:
        row = session.get(Invite, lapsed.id)
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        session.add(row)
        session.commit()
    r = c.post(f"/users/invite/{lapsed.id}/revoke", follow_redirects=False)
    assert r.status_code == 303 and "deleted=" in r.headers["location"]
    r = c.post(f"/users/invite/{live.id}/revoke", follow_redirects=False)
    assert r.status_code == 303 and "revoked=" in r.headers["location"]
    with Session(engine) as session:
        details = {
            e.target: e.detail
            for e in session.exec(
                select(AuditLog).where(AuditLog.action == "user.invite.revoke")
            ).all()
        }
    assert details[lapsed.email] == "expired" and details[live.email] == "", details

    # An accepted invite cannot be deleted or revived, even once it is past expiry.
    taken = security.create_invite("taken@x.com", "translator", "admin")
    assert security.accept_invite(taken.token, "Taken", password) is not None
    with Session(engine) as session:
        row = session.get(Invite, taken.id)
        row.expires_at = datetime.utcnow() - timedelta(days=1)
        session.add(row)
        session.commit()
    assert c.post(f"/users/invite/{taken.id}/revoke", follow_redirects=False).status_code == 404
    assert security.revoke_invite(taken.id, "admin", allow_expired=True) is None
    with Session(engine) as session:
        row = session.get(Invite, taken.id)
        assert row.accepted and row.revoked_at is None
    # Unknown id: a no-op with the not-found page.
    r = c.post("/users/invite/987654/revoke", follow_redirects=False)
    assert r.status_code == 404 and "Invitation not found" in r.text

    # Admin only: a manager and a translator are refused and the invite survives.
    guarded = security.create_invite("guarded@x.com", "translator", "admin", days=-1)
    for role in ("manager", "translator"):
        who = f"del-{role}@x.com"
        security.create_user(who, f"Del {role}", role, password)
        other = TestClient(app)
        assert login(other, who, password).status_code == 303
        r = other.post(f"/users/invite/{guarded.id}/revoke", follow_redirects=False)
        assert r.status_code in (403, 303) and "deleted=" not in r.headers.get("location", "")
        assert guarded.id in {i.id for i in security.expired_invites()}, role

    # CLI round trip: invite-revoke deletes an expired invite (no prompt, soft delete).
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli_main(["--json", "user", "invite-revoke", "--id", str(guarded.id)]) == 0
    payload = json.loads(output.getvalue())
    assert (
        payload["expired"] is True and payload["revoked_at"] and payload["email"] == guarded.email
    )
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli_main(["--json", "user", "invites", "--expired"]) == 0
    assert guarded.id not in {i["id"] for i in json.loads(output.getvalue())["invites"]}
    text_case = security.create_invite("cli-lapsed@x.com", "viewer", "admin", days=-1)
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli_main(["user", "invite-revoke", "--id", str(text_case.id)]) == 0
    assert "Deleted expired invitation for cli-lapsed@x.com." in output.getvalue()
    assert cli_main(["user", "invite-revoke", "--id", str(taken.id)]) == 2
    assert cli_main(["user", "invite-revoke", "--id", str(guarded.id)]) == 2
    with Session(engine) as session:
        assert session.get(Invite, guarded.id).revoked_at is not None
        assert any(
            e.detail == "expired"
            for e in session.exec(
                select(AuditLog).where(
                    AuditLog.action == "user.invite.revoke", AuditLog.target == guarded.email
                )
            ).all()
        )

    # Resend on an expired invite still works (regression guard beside Delete).
    again = security.create_invite("resend-lapsed@x.com", "viewer", "admin", days=-1)
    r = c.post(
        "/users/invite",
        data={"email": again.email, "role": again.role, "resend_id": again.id},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "invited=" in r.headers["location"]
    assert any(i.email == again.email for i in security.pending_invites())
    print("  expired invite Delete: soft, confirmed, CSRF + admin only, link dead, CLI parity")


def _sso_session_client(user) -> TestClient:
    """A client holding the session an SSO/SAML sign-in leaves behind (security.login
    writes exactly uid/se/la/iat), plus a CSRF token, minted directly: this suite has no
    identity provider, and the probe only needs a signed-in identity-provider account."""
    now = int(time.time())
    tok = secrets.token_urlsafe(16)
    c = TestClient(app)
    c.cookies.set(
        "session",
        sign_session(
            {"uid": user.id, "se": int(user.session_epoch or 0), "la": now, "iat": now, "csrf": tok}
        ),
    )
    c.headers["X-CSRF-Token"] = tok  # pregate: allow-secret per-run CSRF test header
    return c


def _audit_rows(action: str, target: str = "", actor: str = ""):
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import AuditLog

    with Session(engine) as s:
        q = select(AuditLog).where(AuditLog.action == action)
        if target:
            q = q.where(AuditLog.target == target)
        if actor:
            q = q.where(AuditLog.actor == actor)
        return s.exec(q).all()


def _sso_local_password() -> None:
    """Train sso-localpw (security): an account that signs in through an identity provider
    can neither plant a local password nor sign in with one. On base accc9661 the probe
    below got 303 saved=pw from /account/password with current="" and then signed in with
    the planted password; every CONTROL assertion here fails on that base."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import Role, User

    new_pw = secrets.token_urlsafe(18)  # pregate: allow-secret random per-run fixture
    local_pw = secrets.token_urlsafe(18)  # pregate: allow-secret random per-run fixture
    reset_pw = secrets.token_urlsafe(18)  # pregate: allow-secret random per-run fixture

    def hash_of(uid):
        with Session(engine) as s:
            return s.get(User, uid).password_hash

    # --- the probe: an OIDC account, no password, signed in the SSO way ---
    sso, err = security.get_or_create_sso_user(
        {
            "iss": "https://idp.test",
            "sub": "s-probe",
            "email": "sso.probe@acct.test",
            "email_verified": True,
            "name": "S",
        }
    )
    assert sso is not None and err is None, err
    assert sso.auth_source == "sso" and not sso.password_hash, "precondition"
    c = _sso_session_client(sso)
    page = c.get("/account")
    assert page.status_code == 200, page.status_code
    assert 'action="/account/password"' not in page.text, "an SSO account must get no password form"
    assert "single sign-on; change your password" in page.text, "the card must say where instead"
    # CONTROL 1: /account/password with an empty current password writes nothing.
    r = c.post(
        "/account/password",
        data={"current": "", "new": new_pw, "confirm": new_pw},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=pw" not in r.headers["location"], r.headers
    assert hash_of(sso.id) == "", "CONTROL: /account/password planted a local password"
    shown = c.get(r.headers["location"]).text
    assert "sign-in provider" in shown, shown[:400]
    # CONTROL 2: the forced-change page is refused the same way (no form, no write).
    assert 'action="/auth/password-change"' not in c.get("/auth/password-change").text
    r = c.post(
        "/auth/password-change",
        data={"current": "", "new": new_pw, "confirm": new_pw, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 403, r.status_code
    assert hash_of(sso.id) == "", "CONTROL: /auth/password-change planted a local password"
    refused = [
        a
        for a in _audit_rows("account.change_password", actor=sso.email)
        if a.detail == "refused: signs in through sso"
    ]
    assert len(refused) == 2, [a.detail for a in _audit_rows("account.change_password")]
    # The refusal audit reads the source through auth_source_of: a whitespace-only value
    # reads as local (it used to write "refused: signs in through " with no name).
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.web import auth as web_auth

    req = Request({"type": "http", "method": "POST", "path": "/account/password", "headers": []})
    for raw, want in (
        ("   ", "refused: no local password"),
        (" SAML ", "refused: signs in through saml"),
    ):
        who = f"ws{len(raw)}.refusal@acct.test"
        web_auth._audit_password_change_refused(req, SimpleNamespace(email=who, auth_source=raw))
        details = [a.detail for a in _audit_rows("account.change_password", actor=who)]
        assert details == [want], (raw, details)
    # CONTROL 3: even a hash already in the DB (the old bug's aftermath, or a pre-F5
    # adoption) never signs an identity-provider account in, on the service or the form.
    with Session(engine) as s:
        row = s.get(User, sso.id)
        row.password_hash = security.hash_password(new_pw)  # pregate: allow-secret planted hash
        s.add(row)
        s.commit()
    assert security.authenticate(sso.email, new_pw) is None, "CONTROL: SSO + hash signed in"
    assert login(TestClient(app), sso.email, new_pw).status_code == 401
    # ...and a stray hash does not reopen self-service either (no current-password oracle).
    ok, msg = security.change_password(sso.id, new_pw, local_pw)
    assert not ok and "single sign-on" in msg, msg
    for source in ("saml", "scim", "ldap", "trust"):
        u = security.create_user(
            f"{source}.probe@acct.test", "", Role.TRANSLATOR, "", auth_source=source
        )
        ok, msg = security.change_password(u.id, "", new_pw)
        assert not ok and hash_of(u.id) == "", (source, msg)
        with Session(engine) as s:
            row = s.get(User, u.id)
            row.password_hash = security.hash_password(new_pw)  # pregate: allow-secret planted hash
            s.add(row)
            s.commit()
        assert security.authenticate(u.email, new_pw) is None, f"{source} + hash signed in"
        if source == "ldap":
            assert "directory" in msg, msg
    print("  sso-localpw: SSO/SAML/SCIM/LDAP/trust cannot set or use a local password")

    # --- legitimate paths still sign in ---
    loc = security.create_user("local.person@acct.test", "L", Role.TRANSLATOR, local_pw)
    assert security.authenticate(loc.email, local_pw) is not None
    lc = TestClient(app)
    assert login(lc, loc.email, local_pw).status_code == 303
    assert 'action="/account/password"' in lc.get("/account").text, "local keeps the form"
    r = lc.post(
        "/account/password",
        data={"current": "not it", "new": new_pw, "confirm": new_pw},
        follow_redirects=False,
    )
    assert "saved=pwfail" in r.headers["location"], "a local change still needs the current one"
    r = lc.post(
        "/account/password",
        data={"current": local_pw, "new": new_pw, "confirm": new_pw},
        follow_redirects=False,
    )
    assert "saved=pw" in r.headers["location"] and "pwfail" not in r.headers["location"]
    assert security.authenticate(loc.email, new_pw) is not None
    # a legacy row with a blank auth_source reads as local, exactly as before
    with Session(engine) as s:
        row = s.get(User, loc.id)
        row.auth_source = ""
        s.add(row)
        s.commit()
    assert security.authenticate(loc.email, new_pw) is not None, "blank auth_source = local"
    # admin reset of an SSO account switches it to local ON PURPOSE (audited) and signs in
    with Session(engine) as s:
        ok, msg = security.admin_set_password(s, sso.id, reset_pw, actor="admin")
    assert ok, msg
    with Session(engine) as s:
        row = s.get(User, sso.id)
        assert row.auth_source == "local" and row.password_must_change, "reset must switch"
    assert security.authenticate(sso.email, reset_pw) is not None, "reset must sign in"
    switched = _audit_rows("user.auth_source_changed", target=sso.email)
    assert [a.detail for a in switched] == ["sso -> local (admin password reset)"], switched
    # from then on the IdP no longer adopts it (finding F5), so the switch is real
    again, err = security.get_or_create_sso_user({"email": sso.email})
    assert again is None and "local password" in err, err
    print("  sso-localpw: local change, blank source and admin reset still sign in")
    _sso_localpw_round1(new_pw, reset_pw)
    _sso_localpw_round2(new_pw)


def _sso_localpw_round1(new_pw: str, reset_pw: str) -> None:
    """Gate sso-localpw round 1. MEDIUM-1: an invitation never moves an existing account off
    its identity provider and never undoes a deactivation (on round 0 both probes below
    ended ('local', active) and signed in). MEDIUM-2: the trust principal is never reset or
    invited (round 0 rewrote it to 'local', which stops the next embedded boot). LOW-1:
    a non-canonical 'Local' row still signs in (round 0 locked it out)."""
    from datetime import datetime, timedelta

    from sqlmodel import Session

    from app.db import engine
    from app.models import Invite, Role, User

    def row_of(email):
        with Session(engine) as s:
            u = security.get_by_email(email)
            return s.get(User, u.id)

    def refused_accept(token, email):
        try:
            security.accept_invite(token, "X", new_pw)
        except security.InviteRefused as exc:
            return str(exc)
        raise AssertionError(f"CONTROL: the invitation for {email} was accepted")

    # A: invited, then the person signs in through OIDC instead; the old link is refused.
    inv_a = security.create_invite("a.person@acct.test", Role.TRANSLATOR, "admin")
    sso, err = security.get_or_create_sso_user(
        {
            "iss": "https://idp.test",
            "sub": "s-a",
            "email": "a.person@acct.test",
            "email_verified": True,
        }
    )
    assert sso is not None, err
    msg = refused_accept(inv_a.token, sso.email)
    assert "single sign-on" in msg, msg
    row = row_of(sso.email)
    assert (row.auth_source, row.password_hash) == ("sso", ""), "CONTROL: invite switched it"
    assert security.authenticate(sso.email, new_pw) is None
    assert security.get_invite(inv_a.token) is None, "the refused invitation must be dead"
    assert [a.detail for a in _audit_rows("user.invite.refused", target=sso.email)] == [
        "refused: sso account"
    ]
    # The public accept door uses a generic invalid page without account-state details.
    # Round 2: a new
    # invitation for an IdP account is refused at creation, so this one predates the row.
    inv_a2 = security.create_invite("a2.person@acct.test", Role.TRANSLATOR, "admin")
    sso, err = security.get_or_create_sso_user(
        {
            "iss": "https://idp.test",
            "sub": "s-a2",
            "email": "a2.person@acct.test",
            "email_verified": True,
        }
    )
    assert sso is not None, err
    anon = TestClient(app)
    csrf_for(anon)
    form = {"name": "A", "password": new_pw, "confirm": new_pw}  # pregate: allow-secret no literal
    r = anon.post(f"/auth/accept/{inv_a2.token}", data=form, follow_redirects=False)
    assert r.status_code == 400 and "single sign-on" not in r.text, r.status_code
    assert row_of(sso.email).password_hash == ""
    # A2: SCIM-provisioned, then SCIM deactivates it; the pending invite must not revive it.
    inv_b = security.create_invite("scim.gone@acct.test", Role.TRANSLATOR, "admin")
    sc = security.create_user("scim.gone@acct.test", "", Role.TRANSLATOR, "", auth_source="scim")
    with Session(engine) as s:
        r2 = s.get(User, sc.id)
        r2.external_id, r2.active = "ext-gone", False
        s.add(r2)
        s.commit()
    refused_accept(inv_b.token, sc.email)
    row = row_of(sc.email)
    assert (row.auth_source, row.active, row.password_hash) == ("scim", False, ""), "CONTROL"
    # an admin-disabled LOCAL account (it has a password) is not revived by an old link either
    dis_pw = secrets.token_urlsafe(18)  # pregate: allow-secret random per-run fixture
    inv_c = security.create_invite("disabled.local@acct.test", Role.TRANSLATOR, "admin")
    dis = security.create_user("disabled.local@acct.test", "", Role.TRANSLATOR, dis_pw)
    with Session(engine) as s:
        r3 = s.get(User, dis.id)
        r3.active = False
        s.add(r3)
        s.commit()
    assert "deactivated" in refused_accept(inv_c.token, dis.email)
    assert not row_of(dis.email).active
    # legitimate: a PRE-CREATED inactive local row (no password, no SCIM link) is activated
    with Session(engine) as s:
        s.add(User(email="pre.created@acct.test", active=False))
        s.commit()
    inv_d = security.create_invite("pre.created@acct.test", Role.TRANSLATOR, "admin")
    got = security.accept_invite(inv_d.token, "Pre", new_pw)
    assert got is not None and got.active and got.auth_source == "local"
    assert security.authenticate("pre.created@acct.test", new_pw) is not None
    print("  sso-localpw r1: invites never switch an IdP account or revive a deactivated one")

    # MEDIUM-2: the trust principal (any trust row) is never reset or invited.
    tr = security.create_user("host-svc@acct.test", "", Role.ADMIN, "", auth_source="trust")
    with Session(engine) as s:
        ok, msg = security.admin_set_password(s, tr.id, reset_pw, actor="admin")
    assert not ok and "host service identity" in msg, msg
    row = row_of(tr.email)
    assert (row.auth_source, row.password_hash) == ("trust", ""), (
        "CONTROL: a reset rewrote the trust principal (trust_auth.configure_at_boot then refuses)"
    )
    assert [a.detail for a in _audit_rows("user.password_reset", target=tr.email)] == [
        "refused: trust service identity"
    ]
    try:
        security.create_invite(tr.email, Role.TRANSLATOR, "admin")
        raise AssertionError("CONTROL: an invitation was issued for the trust principal")
    except ValueError as exc:
        assert "host service identity" in str(exc), exc
    with Session(engine) as s:  # an invitation that predates the trust row
        s.add(
            Invite(
                email=tr.email,
                role=Role.TRANSLATOR,
                token="sso-localpw-trust-fixture",  # pregate: allow-secret offline fixture
                created_by="admin",
                expires_at=datetime.utcnow() + timedelta(days=7),
            )
        )
        s.commit()
    assert "host service identity" in refused_accept("sso-localpw-trust-fixture", tr.email)
    assert row_of(tr.email).auth_source == "trust"
    print("  sso-localpw r1: the trust principal cannot be reset or invited (audited)")

    # LOW-1: non-canonical spellings of local sign in, and a reset rewrites them to "local".
    for i, spelling in enumerate(("Local", " local", "LOCAL ")):
        pw = secrets.token_urlsafe(18)  # pregate: allow-secret random per-run fixture
        u = security.create_user(f"odd{i}@acct.test", "", Role.TRANSLATOR, pw)
        with Session(engine) as s:
            r4 = s.get(User, u.id)
            r4.auth_source = spelling
            s.add(r4)
            s.commit()
        assert security.authenticate(u.email, pw) is not None, f"CONTROL: {spelling!r} locked out"
        assert security.password_change_refusal(row_of(u.email)) is None
        with Session(engine) as s:
            ok, msg = security.admin_set_password(s, u.id, reset_pw, actor="admin")
        assert ok, msg
        assert row_of(u.email).auth_source == "local"
        assert security.authenticate(u.email, reset_pw) is not None
        assert _audit_rows("user.auth_source_changed", target=u.email) == [], "not a switch"
    print("  sso-localpw r1: 'Local' / ' local' rows sign in; reset writes 'local'")


def _sso_localpw_round2(new_pw: str) -> None:
    """Gate sso-localpw round 2. MEDIUM-1 (probe D): a pre-created row the IdP then adopts
    by SCIM and signs in by SAML stays auth_source 'local' with no password, but signs in
    only through the IdP; a pending invitation must not give it a local password (on the
    round-1 tip it was accepted and signed in). LOW-1: the F5 adoption rule, mfa.applies_to
    and password_policy.change_required read the normalized auth_source."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import Role, User
    from app.services import mfa, password_policy, scim

    # --- probe D ---
    with Session(engine) as s:
        s.add(User(email="d.person@acct.test", active=False))
        s.commit()
    inv_d = security.create_invite("d.person@acct.test", Role.TRANSLATOR, "admin")
    scim.create_user(
        None, "scim", {"userName": "d.person@acct.test", "externalId": "ext-d", "active": True}
    )
    saml_user, err = security.get_or_create_saml_user("d.person@acct.test")
    assert saml_user is not None and saml_user.auth_source == "local", err
    try:
        security.accept_invite(inv_d.token, "D", new_pw, ip="203.0.113.9")
        raise AssertionError("CONTROL: the invitation passworded a SCIM-adopted SAML account")
    except security.InviteRefused as exc:
        assert "single sign-on" in str(exc), exc
    d = security.get_by_email("d.person@acct.test")
    assert d.password_hash == "" and security.authenticate(d.email, new_pw) is None
    rows = _audit_rows("user.invite.refused", target=d.email)
    assert [(a.detail, a.ip) for a in rows] == [
        ("refused: local account, SCIM-linked", "203.0.113.9")
    ], [(a.detail, a.ip) for a in rows]
    # a NEW invitation for it is refused at creation, and self-service names the IdP
    try:
        security.create_invite(d.email, Role.TRANSLATOR, "admin")
        raise AssertionError("CONTROL: an invitation was issued for a SCIM-adopted SAML row")
    except ValueError as exc:
        assert "single sign-on" in str(exc), exc
    assert "single sign-on" in security.password_change_refusal(d)
    # a SCIM-adopted local row WITH a password still signs in locally and may be re-invited
    kept_pw = secrets.token_urlsafe(18)  # pregate: allow-secret random per-run fixture
    kept = security.create_user("kept.local@acct.test", "", Role.TRANSLATOR, kept_pw)
    scim.create_user(None, "scim", {"userName": kept.email, "externalId": "ext-k"})
    assert security.authenticate(kept.email, kept_pw) is not None
    try:
        security.create_invite(kept.email, Role.TRANSLATOR, "admin")
    except ValueError as exc:
        assert "Reset password" in str(exc)
    else:
        raise AssertionError("active local account must use reset, not invitation")
    print("  sso-localpw r2: a SCIM-adopted SAML account cannot be invited to a password")

    # --- LOW-1: the neighbours read the normalized auth_source ---
    odd_pw = secrets.token_urlsafe(18)  # pregate: allow-secret random per-run fixture
    odd = security.create_user("odd.f5@acct.test", "", Role.TRANSLATOR, odd_pw)
    with Session(engine) as s:
        r = s.get(User, odd.id)
        r.auth_source = "Local"
        r.password_must_change = True  # pregate: allow-secret boolean flag
        s.add(r)
        s.commit()
        odd = s.get(User, odd.id)
    assert mfa.applies_to(odd), "a 'Local' row signs in with a password, so MFA applies"
    assert password_policy.change_required(odd) == "reset", "must-change applies to 'Local'"
    adopted, err = security.get_or_create_sso_user({"email": odd.email})
    assert adopted is None and "local password" in err, "F5: OIDC adopted a 'Local' row"
    adopted, err = security.get_or_create_saml_user(odd.email)
    assert adopted is None and "local password" in err, "F5: SAML adopted a 'Local' row"
    from app.services import ldap_auth

    adopted, err = ldap_auth.get_or_create_ldap_user(odd.email, "")
    assert adopted is None and "local password" in err, "F5: LDAP adopted a 'Local' row"
    assert security.get_by_email(odd.email).auth_source == "Local", "row must be untouched"
    print("  sso-localpw r2: F5, MFA and must-change read the normalized auth_source")


def check_unhandled_error_log_redacts_invite_token() -> None:
    """#537 gate: the catch-all 500 handler logged `Unhandled error on <method> <path>`,
    and for /auth/accept/<token> the path IS the bearer invite token. Neither the log
    nor the error page may carry it."""
    import app.web.auth as web_auth

    inv = security.create_invite("err-log@x.com", "viewer", "admin")
    lines: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    grab = _Grab(level=logging.DEBUG)
    log = logging.getLogger("loc_tms")
    log.addHandler(grab)
    orig = web_auth.security.get_invite

    def _boom(_token):
        raise RuntimeError("forced failure for the log check")

    web_auth.security.get_invite = _boom
    try:
        r = TestClient(app, raise_server_exceptions=False).get(f"/auth/accept/{inv.token}")
    finally:
        web_auth.security.get_invite = orig
        log.removeHandler(grab)
    assert r.status_code == 500, r.status_code
    assert inv.token not in r.text, "invite token on the error page"
    hits = [ln for ln in lines if "Unhandled error on" in ln]
    assert hits, ("the 500 must still be logged", lines)
    assert all(inv.token not in ln for ln in lines), ("invite token in the log", hits)
    assert any("auth/accept/[redacted]" in ln for ln in hits), hits
    print("  invite token: the unhandled-error log line and the 500 page omit it")


def main() -> None:
    init_db()

    # --- seeding ---
    assert security.seed_initial() is True
    assert security.user_count() == 3
    assert settings.get("security_auth_required") == "1"
    roles = {u.email: u.role for u in security.list_users()}
    assert roles.get("linguist") == "linguist", roles
    print("  seeded admin + linguist + user1, login wall ON")

    c = TestClient(app)
    # login wall
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.get("/", follow_redirects=False).headers["location"].startswith("/auth/login")

    # --- admin ---
    assert login(c, "admin", "admin").status_code == 303
    assert c.get("/").status_code == 200
    for path in ["/users", "/security", "/audit", "/setup-ai", "/settings"]:
        assert c.get(path).status_code == 200, ("admin", path)
    print("  admin signs in and reaches all admin areas")

    # #129: only live invitations remain pending; every invalidation kills accept.
    password = secrets.token_urlsafe(24)
    expired = security.create_invite("expired@x.com", "translator", "admin", days=-1)
    assert expired.id not in {i.id for i in security.pending_invites()}
    assert security.get_invite(expired.token) is None
    assert security.accept_invite(expired.token, "Expired", password) is None
    old = security.create_invite("repeat@x.com", "translator", "admin")
    new = security.create_invite(" REPEAT@x.com ", "translator", "admin")
    assert security.get_invite(old.token) is None
    assert security.accept_invite(old.token, "Old", password) is None
    assert security.accept_invite(new.token, "New", password) is not None
    assert security.revoke_invite(new.id, "admin") is None
    revoked = security.create_invite("revoked@x.com", "translator", "admin")
    page = c.get("/users").text
    assert "Expired invitations" in page and expired.email in page
    assert revoked.token not in page and expired.token not in page
    csrf_header = c.headers.pop("X-CSRF-Token")
    missing_csrf = c.post(f"/users/invite/{revoked.id}/revoke", follow_redirects=False)
    assert missing_csrf.status_code == 303 and "expired=1" in missing_csrf.headers["location"]
    assert security.get_invite(revoked.token) is not None
    c.headers["X-CSRF-Token"] = csrf_header
    r = c.post(f"/users/invite/{revoked.id}/revoke", follow_redirects=False)
    assert r.status_code == 303 and "revoked=" in r.headers["location"]
    assert security.get_invite(revoked.token) is None
    assert security.accept_invite(revoked.token, "Revoked", password) is None
    assert security.revoke_invite(revoked.id, "admin") is None
    assert security.revoke_invite(999999, "admin") is None
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import AuditLog, Invite

    with Session(engine) as session:
        assert session.get(Invite, old.id).revoked_at is not None
        events = session.exec(select(AuditLog)).all()
        assert any(e.action == "user.invite.revoke" and e.target == revoked.email for e in events)
        assert any(e.action == "user.invite.supersede" and e.target == old.email for e in events)
    import io
    import json
    from contextlib import redirect_stdout

    from app.cli.__main__ import main as cli_main

    cli_invite = security.create_invite("cli-invite@x.com", "translator", "admin")
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli_main(["--json", "user", "invites"]) == 0
    assert cli_invite.token not in output.getvalue()
    assert '"token"' not in output.getvalue()
    assert any(i["id"] == cli_invite.id for i in json.loads(output.getvalue())["invites"])
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli_main(["--json", "user", "invite-revoke", "--id", str(cli_invite.id)]) == 0
    assert json.loads(output.getvalue())["revoked_at"]
    assert security.get_invite(cli_invite.token) is None
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli_main(["--json", "user", "invites", "--expired"]) == 0
    assert {i["id"] for i in json.loads(output.getvalue())["invites"]} == {expired.id}
    assert expired.token not in output.getvalue()
    for source in (expired, security.create_invite("resend@x.com", "translator", "admin")):
        assert f'value="{source.id}"' in c.get("/users").text
        r = c.post(
            "/users/invite",
            data={"email": source.email, "role": source.role, "resend_id": source.id},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert security.get_invite(source.token) is None
        fresh = next(i for i in security.pending_invites() if i.email == source.email)
        assert fresh.token != source.token
        assert fresh.token in c.get("/users").text
        assert fresh.token not in c.get("/users").text
        with Session(engine) as session:
            events = session.exec(select(AuditLog).where(AuditLog.target == source.email)).all()
            assert any(e.action == "user.invite.revoke" and e.ip for e in events)
            assert any(e.action == "user.invite" and e.ip for e in events)
    c.post("/users/invite", data={"email": "web-repeat@x.com", "role": "translator"})
    c.post("/users/invite", data={"email": "web-repeat@x.com", "role": "translator"})
    with Session(engine) as session:
        assert any(
            e.ip
            for e in session.exec(
                select(AuditLog).where(
                    AuditLog.action == "user.invite.supersede",
                    AuditLog.target == "web-repeat@x.com",
                )
            ).all()
        )
    print("  invite expiry, supersession, revoke, audit and secret-free web list")
    _expired_invite_delete(c)

    # #4: admin create-user form rejects an 11-char password cleanly (the policy
    # now lives in security.create_user, so the form is covered too). Redirects
    # back to /users?error=... and creates NO account; a 12+ char one succeeds.
    r = c.post(
        "/users",
        data={
            "email": "weakform@x.com",
            "name": "Weak",
            "role": "translator",
            "password": "shortpw1234",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" in r.headers["location"], r.headers
    assert security.get_by_email("weakform@x.com") is None, "weak-pw user was created"
    r = c.post(
        "/users",
        data={
            "email": "goodform@x.com",
            "name": "Good",
            "role": "translator",
            "password": "good-pass-12345",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" not in r.headers["location"], r.headers
    assert security.get_by_email("goodform@x.com") is not None, "valid-pw user not created"
    print("  admin create-user form enforces the 12-char password policy")

    # admin invites a user
    assert (
        c.post(
            "/users/invite",
            data={"email": "newbie@x.com", "role": "translator"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    pend = [i for i in security.pending_invites() if i.email == "newbie@x.com"]
    assert pend, "invite not created"
    token = pend[0].token
    assert token in c.get("/users").text, "manual link must be shown once after creation"
    assert token not in c.get("/users").text, "manual link must not persist in the list"

    # accept the invite (unauthenticated link)
    cc = TestClient(app)
    assert cc.get(f"/auth/accept/{token}").status_code == 200
    csrf_for(cc)  # accept page render minted the session csrf; send it on the POSTs
    # too-short password is rejected (12-char minimum), then a good one succeeds
    r = cc.post(
        f"/auth/accept/{token}",
        data={"name": "Newbie", "password": "short", "confirm": "short"},
        follow_redirects=False,
    )
    assert r.status_code == 400, "short password should be rejected"
    r = cc.post(
        f"/auth/accept/{token}",
        data={"name": "Newbie", "password": "newbie-pass-123", "confirm": "newbie-pass-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "accepted=1" in r.headers["location"]
    assert security.get_invite(token) is None, "token should be consumed"
    # #537: the bearer invite token never lands in an audit row (the rejected-password
    # and the accepted POST were both audited by path before the fix).
    from sqlmodel import Session as _S
    from sqlmodel import select as _sel

    from app.db import engine as _eng
    from app.models import AuditLog as _AL

    with _S(_eng) as _s:
        for _row in _s.exec(_sel(_AL)).all():
            assert token not in (_row.action or "") + (_row.detail or "") + (_row.target or ""), (
                "invite token leaked into the audit log",
                _row.action,
            )
        assert any(
            (_r.action or "").startswith("POST /auth/accept/") for _r in _s.exec(_sel(_AL)).all()
        ), "the accept POST must still be audited (with the token redacted)"
    print("  email-invite handshake: invite -> accept -> account created")
    check_unhandled_error_log_redacts_invite_token()

    # --- standard user RBAC ---
    sign_out(c, follow_redirects=False)
    assert login(c, "user1", "user1").status_code == 303
    assert c.get("/").status_code == 200
    assert c.get("/projects").status_code == 200  # can use the app
    for path in ["/users", "/security", "/audit", "/setup-ai", "/settings"]:
        assert c.get(path).status_code == 403, ("user1", path)
    assert (
        c.post("/setup-ai", data={"default_provider": "claude"}, follow_redirects=False).status_code
        == 403
    )
    protected = security.create_invite("protected@x.com", "translator", "admin")
    assert c.post(f"/users/invite/{protected.id}/revoke").status_code == 403
    assert security.get_invite(protected.token) is not None
    print("  standard user uses the app but is blocked from admin areas")

    # --- linguist RBAC: AI setup yes, admin areas no ---
    lc = TestClient(app)
    assert login(lc, "linguist", "linguist").status_code == 303
    assert lc.get("/setup-ai").status_code == 200, "linguist should reach Setup AI"
    assert lc.get("/templates").status_code == 200
    assert (
        lc.post(
            "/setup-ai", data={"default_provider": "claude"}, follow_redirects=False
        ).status_code
        == 303
    ), "linguist should be able to save AI setup"
    for path in ["/users", "/security", "/audit", "/settings"]:
        assert lc.get(path).status_code == 403, ("linguist", path)
    home = lc.get("/").text
    assert "/setup-ai" in home, "linguist should see the Setup AI nav/menu link"
    print("  linguist reaches Setup AI + templates, blocked from admin areas")

    # --- self-service password change ---
    assert c.get("/account").status_code == 200
    r = c.post(
        "/account/password",
        data={"current": "user1", "new": "short", "confirm": "short"},
        follow_redirects=False,
    )
    # Password policy train: a policy refusal now carries its one-sentence reason in the
    # page error summary (?err=), instead of the generic pwfail alias.
    assert "err=Password%20must%20be%20at%20least%2012" in r.headers["location"], (
        "short password should be rejected with the rule it broke"
    )
    assert (
        c.post(
            "/account/password",
            data={"current": "user1", "new": "user1-new-pass99", "confirm": "user1-new-pass99"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    # #138: the change kills the account's OTHER sessions, never the acting one.
    assert c.get("/account").status_code == 200, "changing your own password must not sign you out"
    sign_out(c, follow_redirects=False)
    assert login(c, "user1", "user1").status_code == 401  # old password rejected
    assert login(c, "user1", "user1-new-pass99").status_code == 303  # new password works
    print("  self-service password change + re-login")

    # invited user can log in and is restricted
    sign_out(c, follow_redirects=False)
    assert login(c, "newbie@x.com", "newbie-pass-123").status_code == 303
    assert c.get("/security").status_code == 403
    print("  invited account logs in and is role-restricted")

    _viewer_self_service()

    # --- #138: logout invalidates the session SERVER-side (per-user epoch) ---
    # The session is a stateless itsdangerous-signed cookie, so clearing it client
    # side left a captured/cached copy usable until expiry. Every cookie carries the
    # user's session_epoch; logout and password change bump it, killing every
    # outstanding cookie for that user on every device.
    admin_id = security.get_by_email("admin").id

    a = TestClient(app)
    assert login(a, "admin", "admin").status_code == 303
    stolen = a.cookies.get("session")
    assert stolen, "login must set a session cookie"
    # control: the captured cookie IS a working session before the logout
    assert replay(stolen).status_code == 200, "control: a live cookie reaches /projects"
    # (a) THE DEFECT: replaying the pre-logout cookie after logout
    sign_out(a, follow_redirects=False)
    r = replay(stolen)
    assert r.status_code == 303, (
        f"#138: a replayed pre-logout cookie must be refused, got {r.status_code}"
    )
    assert r.headers["location"].startswith("/auth/login"), r.headers.get("location")
    assert session_data(a).get("se") is None, "logout must clear the client session too"
    # (b) a byte-flipped (unauthentic) cookie is still refused - unchanged control
    flipped = stolen[:-2] + ("A" if stolen[-2] != "A" else "B") + stolen[-1]
    assert replay(flipped).status_code == 303, "a tampered cookie must stay refused"
    # (c) the SAME user logging in again gets a fresh, working session
    b = TestClient(app)
    assert login(b, "admin", "admin").status_code == 303
    assert b.get("/projects").status_code == 200, "a fresh login after logout must work"
    assert session_data(b).get("se") is not None, "login must stamp the session epoch"
    live = b.cookies.get("session")
    # (d) two live sessions for one user: logging out of ONE kills the other
    d2 = TestClient(app)
    assert login(d2, "admin", "admin").status_code == 303
    assert d2.get("/projects").status_code == 200
    sign_out(b, follow_redirects=False)
    assert d2.get("/projects", follow_redirects=False).status_code == 303, (
        "#138: logout on one device must kill the user's other sessions"
    )
    assert replay(live).status_code == 303
    # (e) a password change kills every outstanding session
    e1 = TestClient(app)
    assert login(e1, "admin", "admin").status_code == 303
    assert e1.get("/projects").status_code == 200
    ok, msg = security.change_password(admin_id, "admin", "admin-new-pass99")
    assert ok, msg
    assert e1.get("/projects", follow_redirects=False).status_code == 303, (
        "#138: a password change must kill outstanding sessions"
    )
    # (f) a pre-upgrade cookie (authentic, carries uid but no epoch) is refused once
    legacy = sign_session({"uid": admin_id, "la": int(time.time())})
    assert replay(legacy).status_code == 303, "a cookie with no epoch must be refused"
    print("  #138: logout/password change invalidate every outstanding cookie")

    # --- #138 fix round: the revocation must be ATOMIC and never re-resolved ---
    pw = "admin-new-pass99"  # (e) above changed it

    # (h) HIGH-1: two revocations that both start from the SAME snapshot must BOTH
    # count. Deterministic, no threads: StaleSnapshotSession pins what the ORM read
    # hands back, which is exactly what the losing racer of a read-modify-write sees.
    base = security.get_by_email("admin").session_epoch
    sec_mod.Session = StaleSnapshotSession
    try:
        StaleSnapshotSession.pinned = base
        first = security.bump_session_epoch(admin_id)  # racer A: one device logs out
        StaleSnapshotSession.pinned = None
        mid = TestClient(app)  # a login lands BETWEEN the two revocations
        assert login(mid, "admin", pw).status_code == 303
        assert mid.get("/projects").status_code == 200, "the in-between login must work"
        StaleSnapshotSession.pinned = base  # racer B still sees the pre-A snapshot
        second = security.bump_session_epoch(admin_id)
    finally:
        StaleSnapshotSession.pinned = None
        sec_mod.Session = _REAL_SESSION
    assert second > first, (
        f"#138 HIGH-1: revocations must increment atomically; racers produced {first} then {second}"
    )
    assert mid.get("/projects", follow_redirects=False).status_code == 303, (
        "#138 HIGH-1: the second revocation must kill the cookie stamped between the two"
    )

    # (i) the SAME race with change_password as the losing racer.
    base2 = security.get_by_email("admin").session_epoch
    sec_mod.Session = StaleSnapshotSession
    try:
        StaleSnapshotSession.pinned = base2
        security.bump_session_epoch(admin_id)  # racer A: another device logs out
        StaleSnapshotSession.pinned = None
        mid2 = TestClient(app)
        assert login(mid2, "admin", pw).status_code == 303
        assert mid2.get("/projects").status_code == 200
        StaleSnapshotSession.pinned = base2  # racer B reads the pre-A snapshot
        res = security.change_password(admin_id, pw, "admin-third-pass99")
    finally:
        StaleSnapshotSession.pinned = None
        sec_mod.Session = _REAL_SESSION
    ok, msg = res
    assert ok, msg
    pw = "admin-third-pass99"
    assert mid2.get("/projects", follow_redirects=False).status_code == 303, (
        "#138 HIGH-1: a password change racing a logout must still revoke"
    )
    print("  #138 HIGH-1: concurrent revocations each increment the epoch atomically")

    # (j) HIGH-2: the acting session gets the epoch the change PRODUCED, never one
    # re-read afterwards. Forced: an extra revocation commits between the change and
    # the re-stamp, so a re-read would hand the session the value that killed it.
    actor = TestClient(app)
    assert login(actor, "admin", pw).status_code == 303
    real_change = security.change_password

    def change_then_interleaved_bump(uid, current, new, **kwargs):
        result = real_change(uid, current, new, **kwargs)
        if result[0]:
            security.bump_session_epoch(uid)  # an admin reset / another device, right here
        return result

    security.change_password = change_then_interleaved_bump
    try:
        csrf_for(actor)
        r = actor.post(
            "/account/password",
            data={
                "current": pw,
                "new": "admin-fourth-pass99",
                "confirm": "admin-fourth-pass99",
            },
            follow_redirects=False,
        )
    finally:
        security.change_password = real_change
    assert r.status_code == 303 and "saved=pw" in r.headers["location"], r.headers.get("location")
    pw = "admin-fourth-pass99"
    assert actor.get("/projects", follow_redirects=False).status_code == 303, (
        "#138 HIGH-2: a revocation committed after the change must not be re-read and "
        "handed to the session it revoked"
    )
    print("  #138 HIGH-2: the acting session is stamped with the epoch the change produced")

    # (k) MED-1: a revocation that does not commit must answer cleanly and leave the
    # session SIGNED IN (never pretend a cookie is dead when it is not).
    signed_in = TestClient(app)
    assert login(signed_in, "admin", pw).status_code == 303
    handler = RecordingHandler()
    logging.getLogger("loc_tms").addHandler(handler)
    real_bump = sec_mod.bump_session_epoch

    def boom(*args, **kwargs):
        raise RuntimeError("revocation storage is down")

    sec_mod.bump_session_epoch = boom
    try:
        r = sign_out(signed_in, follow_redirects=False)
    finally:
        sec_mod.bump_session_epoch = real_bump
        logging.getLogger("loc_tms").removeHandler(handler)
    assert r.status_code == 500, f"a failed revocation must answer cleanly, got {r.status_code}"
    assert "Traceback" not in r.text, "the failure must not leak a traceback"
    assert handler.records, "the failed revocation must be logged"
    assert signed_in.get("/projects").status_code == 200, (
        "#138 MED-1: a failed revocation must leave the session signed in, not half-dead"
    )
    print("  #138 MED-1: a failed revocation answers cleanly and keeps the session")

    # (l) MED-1 round 2: when the revocation failed BECAUSE storage is down, the
    # failure-audit write goes to that same storage and can fail too. It must not
    # escape and hand the operator a raw 500 in place of the clean page.
    still_in = TestClient(app)
    assert login(still_in, "admin", pw).status_code == 303
    handler2 = RecordingHandler()
    logging.getLogger("loc_tms").addHandler(handler2)
    real_audit_log = audit_mod.log

    def audit_boom(*args, **kwargs):
        raise RuntimeError("audit storage is down")

    sec_mod.bump_session_epoch = boom
    audit_mod.log = audit_boom
    try:
        r = sign_out(still_in, follow_redirects=False)
    finally:
        sec_mod.bump_session_epoch = real_bump
        audit_mod.log = real_audit_log
        logging.getLogger("loc_tms").removeHandler(handler2)
    assert r.status_code == 500, (
        f"a failed revocation whose audit also fails must answer cleanly, got {r.status_code}"
    )
    assert "Traceback" not in r.text, "the failure must not leak a traceback"
    logged = " | ".join(rec.getMessage() for rec in handler2.records)
    assert "revocation failed" in logged, f"the revocation failure must be logged: {logged}"
    assert "audit" in logged, f"the audit failure must be logged too: {logged}"
    assert still_in.get("/projects").status_code == 200, (
        "#138 MED-1: a failed revocation must leave the session usable, not half-dead"
    )
    print("  #138 MED-1: a failed revocation survives its own audit failing")

    _sso_local_password()

    settings.set_many({"security_auth_required": "0"})
    # (g) auth OFF is byte-identical: session_user short-circuits to SYSTEM before it
    # reads the session at all, so neither a dead nor a nonsense cookie changes it.
    assert replay(stolen).status_code == 200, "auth OFF must ignore the session entirely"
    assert replay(sign_session({"uid": 10**9})).status_code == 200
    print("  #138: the auth-OFF path is unchanged (SYSTEM, no session read)")
    print("ACCOUNTS OK")


def credential_hardening_regressions():
    """Review probe B/C/D, using throwaway accounts and the actual public forms."""
    from unittest.mock import patch

    from sqlmodel import Session

    from app.db import engine
    from app.models import Role
    from app.services import apikeys, mfa
    from tests._cli_runner import run_cli

    init_db()
    settings.set_many({"security_auth_required": "1", "mfa_required": "off"})
    original = secrets.token_urlsafe(24)
    replacement = secrets.token_urlsafe(24)
    errors = []

    def client(email):
        c = TestClient(app)
        assert login(c, email, original).status_code == 303
        return c

    try:
        u = security.create_user("budget@hardening.test", "", Role.VIEWER, original)
        c = client(u.email)
        owner = client(u.email)
        for index in range(security.LOCKOUT_THRESHOLD):
            door = "/account/password" if index % 2 else "/auth/password-change"
            c.post(
                door,
                data={
                    "current": "incorrect",
                    "new": replacement,
                    "confirm": replacement,
                    "csrf_token": csrf_for(c),
                },
                follow_redirects=False,
            )
        assert security.login_locked_seconds(u.email, "testclient") > 0
        assert owner.get("/account", follow_redirects=False).status_code == 303
        assert not security.change_password(u.id, original, replacement)[0]
        assert not security.change_password(u.id, original, replacement, ip="another-ip")[0]
        assert len(_audit_rows("login.lockout", actor=u.email)) >= 3
        rotating = security.create_user("rotating@hardening.test", "", Role.VIEWER, original)
        for index in range(security.LOCKOUT_THRESHOLD):
            try:
                mfa.begin_enrollment(rotating.id, "incorrect", ip=f"address-{index}")
            except mfa.MfaError:
                pass
            else:
                raise AssertionError("MFA setup accepted an incorrect password")
        assert not security.change_password(rotating.id, original, replacement, ip="fresh")[0]
        # Expiration restores access, and a successful check starts a fresh budget.
        key = security._lockout_key(rotating.email, "reauth-account")
        security._locked_until[key] = time.time() - 1
        assert security.change_password(rotating.id, original, replacement, ip="fresh")[0]
        print("  #225: shared form budget, session revocation, locked correct guess refused")
    except AssertionError as exc:
        errors.append(f"#225 {exc}")

    try:
        email = "active@hardening.test"
        inv = security.create_invite(email, Role.VIEWER, "system")
        u = security.create_user(email, "", Role.VIEWER, original)
        before = u.password_hash
        try:
            security.create_invite(email, Role.VIEWER, "system")
        except ValueError as exc:
            assert "Reset password" in str(exc)
        else:
            raise AssertionError("active account received a second invitation")
        admin = security.create_user("inviter@hardening.test", "", Role.ADMIN, original)
        ac = client(admin.email)
        r = ac.post(
            "/users/invite", data={"email": email, "role": Role.VIEWER, "csrf_token": csrf_for(ac)}
        )
        assert "Reset password" in r.text, "web invite refusal message"
        _, bearer = apikeys.create_key(admin.id, "hardening", "docs admin")
        api = TestClient(app)
        r = api.post(
            "/api/users/invite",
            json={"email": email, "role": Role.VIEWER},
            headers=[("X-Loc-Token", bearer)],
        )
        assert r.status_code == 400 and set(r.json()) == {"ok", "error"}, "API refusal shape"
        assert "Reset password" in r.json()["error"]
        code, out, err = run_cli(
            ["--json", "user", "invite", "--email", email, "--role", Role.VIEWER]
        )
        assert code != 0 and "Reset password" in out + err, "CLI refusal message"
        _, viewer_bearer = apikeys.create_key(u.id, "hardening", "docs admin")
        denied = [
            api.post(
                "/api/users/invite",
                json={"email": target},
                headers=[("X-Loc-Token", viewer_bearer)],
            )
            for target in (email, "unknown@hardening.test")
        ]
        assert denied[0].status_code == denied[1].status_code == 403, "non-admin denial status"
        assert denied[0].json() == denied[1].json(), "non-admin denial shape"
        c = TestClient(app)
        c.get(f"/auth/accept/{inv.token}")
        responses = []
        for guess in (original, replacement):
            r = c.post(
                f"/auth/accept/{inv.token}",
                data={"password": guess, "confirm": guess, "csrf_token": csrf_for(c)},
                follow_redirects=False,
            )
            responses.append((r.status_code, r.text))
        assert all("different from" not in body for _, body in responses)
        assert security.get_invite(inv.token) is None
        assert security.accept_invite(inv.token, "", replacement) is None
        with Session(engine) as s:
            assert s.get(User, u.id).password_hash == before
        print("  #249: create and stale accept refused, no current-password oracle")
    except AssertionError as exc:
        errors.append(f"#249 {exc}")

    try:
        u = security.create_user("factor@hardening.test", "", Role.VIEWER, original)
        c = client(u.email)
        owner = client(u.email)
        page = c.get("/account/mfa/setup")
        assert "Setup key" not in page.text and mfa.pending_secret(u.id) is None
        r = c.post("/account/mfa/setup", data={"csrf_token": csrf_for(c)})
        assert r.status_code == 400 and mfa.pending_secret(u.id) is None
        r = c.post("/account/mfa/setup", data={"current": original, "csrf_token": csrf_for(c)})
        assert "Setup key" in r.text
        with patch("app.services.mailer.send", return_value=True) as mail:
            r = c.post(
                "/account/mfa/setup",
                data={"code": mfa.totp(mfa.pending_secret(u.id)), "csrf_token": csrf_for(c)},
            )
            assert mail.call_count == 1 and mail.call_args.args[0] == u.email
        codes = re.findall(r"<code>([a-z2-9]{5}-[a-z2-9]{5})</code>", r.text)
        assert len(codes) == 10
        assert r.status_code == 200 and mfa.is_enrolled(u.id)
        assert c.get("/account", follow_redirects=False).status_code == 200
        assert owner.get("/account", follow_redirects=False).status_code == 303
        try:
            mfa.disable(u.id)
        except mfa.MfaError:
            pass
        else:
            raise AssertionError("service disabled MFA without current password")
        assert mfa.is_enrolled(u.id)
        other = TestClient(app)
        other.cookies.update(c.cookies)
        denied = c.post("/account/mfa/disable", data={"code": codes[0], "csrf_token": csrf_for(c)})
        assert denied.status_code == 200 and mfa.is_enrolled(u.id)
        with patch("app.services.mailer.send", return_value=True) as mail:
            r = c.post(
                "/account/mfa/recovery-codes", data={"code": codes[1], "csrf_token": csrf_for(c)}
            )
            assert mail.call_count == 1 and "replacement" in mail.call_args.args[2]
        codes = re.findall(r"<code>([a-z2-9]{5}-[a-z2-9]{5})</code>", r.text)
        with patch("app.services.mailer.send", return_value=True) as mail:
            r = c.post(
                "/account/mfa/disable",
                data={"code": codes[0], "current": original, "csrf_token": csrf_for(c)},
                follow_redirects=False,
            )
            assert mail.call_count == 1 and "turned off" in mail.call_args.args[2]
        assert r.status_code == 303 and not mfa.is_enrolled(u.id)
        assert c.get("/account", follow_redirects=False).status_code == 200
        assert other.get("/account", follow_redirects=False).status_code == 303
        print("  #250: secret withheld, re-auth required, enrollment revokes other sessions")
    except AssertionError as exc:
        errors.append(f"#250 {exc}")
    assert not errors, errors
    print("CREDENTIAL HARDENING OK")


def gate_round_one_regressions():
    """Gate reproductions: LDAP proof, blank input, concurrency, code order, invites."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from unittest.mock import patch

    from sqlmodel import Session

    from app.db import engine
    from app.models import Invite, Role
    from app.services import access, ldap_auth, mfa

    init_db()
    settings.set_many({"security_auth_required": "1", "mfa_required": "off"})
    original = secrets.token_urlsafe(24)
    errors = []

    def user(label, source="local"):
        return security.create_user(
            f"{label}@gate-round.test",
            "",
            Role.VIEWER,
            original if source == "local" else "",
            auth_source=source,
        )

    def client(u):
        c = TestClient(app)
        c.cookies.set(
            "session",
            sign_session(
                {
                    "uid": u.id,
                    "se": u.session_epoch,
                    "mf": 1,
                    "la": int(time.time()),
                    "iat": int(time.time()),
                }
            ),
            domain="testserver.local",
            path="/",
        )
        csrf_for(c)
        return c

    def ldap_proof():
        u = user("directory", "ldap")
        c = client(u)
        page = c.get("/account/mfa/setup")
        assert "Setup key" not in page.text and mfa.pending_secret(u.id) is None
        with (
            patch.object(ldap_auth, "enabled", return_value=True),
            patch.object(ldap_auth, "_search_then_bind", return_value=None) as bind,
        ):
            for index in range(security.LOCKOUT_THRESHOLD):
                try:
                    mfa.begin_enrollment(u.id, original, ip=f"ldap-{index}")
                except mfa.MfaError:
                    pass
                else:
                    raise AssertionError("LDAP enrollment skipped directory proof")
            assert bind.call_count == security.LOCKOUT_THRESHOLD
            bind.return_value = ("dn", u.email, "Directory")
            try:
                mfa.disable(u.id, original, ip="fresh")
            except mfa.MfaError as exc:
                assert "Too many" in str(exc)
            else:
                raise AssertionError("LDAP disable bypassed shared lockout")
            assert bind.call_count == security.LOCKOUT_THRESHOLD
        assert security.get_by_email(u.email).session_epoch == u.session_epoch + 1
        good = user("directory-good", "ldap")
        with (
            patch.object(ldap_auth, "enabled", return_value=True),
            patch.object(
                ldap_auth, "_search_then_bind", return_value=("dn", good.email, "Directory")
            ) as bind,
        ):
            c = client(good)
            page = c.post("/account/mfa/setup", data={"current": original})
            assert "Setup key" in page.text, (page.status_code, page.url, page.text[-1800:])
            bind.assert_called_once_with(good.email, original)
            bind.return_value = ("other-dn", u.email, "Other")
            try:
                mfa.disable(good.id, original)
            except mfa.MfaError:
                pass
            else:
                raise AssertionError("directory proof accepted a different identity")
            # A leftover local hash must never substitute for the directory factor.
            with Session(engine) as s:
                row = s.get(User, good.id)
                row.password_hash = security.hash_password(original)
                s.add(row)
                s.commit()
            bind.return_value = None
            try:
                mfa.disable(good.id, original)
            except mfa.MfaError:
                pass
            else:
                raise AssertionError("LDAP outage fell back to a local hash")
            bind.return_value = ("dn", good.email, "Directory")
            codes = mfa.confirm_enrollment(good.id, mfa.totp(mfa.pending_secret(good.id)))
            c = client(security.get_by_email(good.email))
            assert 'id="mfa-disable-current"' in c.get("/account").text
            page = c.post(
                "/account/mfa/disable",
                data={"current": original, "code": codes[0]},
                follow_redirects=False,
            )
            assert page.status_code == 303 and not mfa.is_enrolled(good.id)

    def blank_input():
        u = user("blank")
        for _ in range(6):
            assert security.check_current_password(u.id, "") == "Enter your current password"
        assert security.get_by_email(u.email).session_epoch == u.session_epoch
        assert security.check_current_password(u.id, original) is None
        c = client(u)
        payload = session_data(c)
        payload["mfa_setup_reauth"] = time.time() - 301
        c.cookies.clear()
        c.cookies.set("session", sign_session(payload), domain="testserver.local", path="/")
        with patch.object(mfa, "begin_enrollment") as begin:
            page = c.post("/account/mfa/setup", data={"code": "123456"})
            assert page.status_code == 400 and 'name="current"' in page.text, (
                page.status_code,
                page.url,
                page.text[-1800:],
            )
            begin.assert_not_called()

    def independent_checks():
        users = [user("parallel-a"), user("parallel-b")]
        barrier = Barrier(2)

        def verify(*args):
            barrier.wait(timeout=5)
            return True

        with (
            patch.object(sec_mod, "verify_password", side_effect=verify),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(
                pool.map(lambda u: security.check_current_password(u.id, original), users)
            )
        assert results == [None, None]

        # Same-account checks stay serialized so success cannot erase a pending failure.
        from threading import Event

        entered, release = Event(), Event()
        calls = []

        def ordered_verify(_hash, typed):
            calls.append(typed)
            if typed == original:
                entered.set()
                assert release.wait(5)
                return True
            return False

        with (
            patch.object(sec_mod, "verify_password", side_effect=ordered_verify),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            success = pool.submit(security.check_current_password, users[0].id, original)
            assert entered.wait(5)
            failure = pool.submit(security.check_current_password, users[0].id, "wrong")
            release.set()
            assert success.result() is None
            assert failure.result() == "Current password is incorrect"
        assert calls == [original, "wrong"]
        key = security._lockout_key(users[0].email, "reauth-account")
        assert len(security._failures[key]) == 1

    def disable_order():
        u = user("disable")
        raw = mfa.begin_enrollment(u.id, original)
        codes = mfa.confirm_enrollment(u.id, mfa.totp(raw))
        c = client(security.get_by_email(u.email))
        before = mfa.status(u)["recovery_codes_left"]
        page = c.post(
            "/account/mfa/disable",
            data={"current": "wrong", "code": codes[0]},
            follow_redirects=False,
        )
        assert mfa.status(u)["recovery_codes_left"] == before, "recovery code was spent"
        assert page.status_code == 303 and "err=" in page.headers["location"], (
            page.status_code,
            page.headers,
            page.text[-1800:],
        )
        assert c.get(page.headers["location"]).status_code == 200
        page = c.post(
            "/account/mfa/disable",
            data={"current": original, "code": codes[0]},
            follow_redirects=False,
        )
        assert page.status_code == 303 and not mfa.is_enrolled(u.id)

    def neutral_invite():
        u = user("neutral")
        with patch.object(access, "multi_org_active", return_value=True):
            refusal = security.invite_refusal(u)
        assert "active" not in refusal.lower() and "Reset password" not in refusal

    def revoke_stale():
        inv = security.create_invite("stale@gate-round.test", Role.VIEWER, "system")
        user("stale")
        c = TestClient(app)
        csrf_for(c)
        c.post(f"/auth/accept/{inv.token}", data={"password": original, "confirm": original})
        with Session(engine) as s:
            assert s.get(Invite, inv.id).revoked_at is not None

    for probe in (
        ldap_proof,
        blank_input,
        independent_checks,
        disable_order,
        neutral_invite,
        revoke_stale,
    ):
        try:
            probe()
            print(f"  gate round 1 PASS {probe.__name__}")
        except Exception as exc:
            import traceback

            traceback.print_exc()
            errors.append(f"{probe.__name__}: {type(exc).__name__} {exc}")
    assert not errors, errors


if __name__ == "__main__":
    if not os.environ.get("CREDENTIAL_REGRESSIONS_ONLY"):
        main()
    gate_round_one_regressions()
    credential_hardening_regressions()
