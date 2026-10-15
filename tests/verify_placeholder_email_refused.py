"""#520 gate: an app-written placeholder address is never an identity (offline).

Migration a1f4c8b2d6e9 gives every emailless account after the first a unique
'no-email-user-<id>@dedup.invalid' placeholder (so the unique index can build) and keeps it
ACTIVE. Before the gate fix, an emailless account that HAD a password could then sign in by
typing that placeholder. A placeholder (dedup.invalid, and gdpr's erased.invalid) must be
refused at every by-email door:

  sign-in    web /auth/login and security.authenticate, also after an admin password reset
  invite     create_invite refuses the address (so no invite can be accepted for it)
  sso        OIDC (get_or_create_sso_user) and SAML never adopt or link the row by email
  ldap       get_or_create_ldap_user never adopts it
  scim       SCIM create never adopts it (a 400, the row is untouched)
  create     create_user refuses it on the public path and for the internal seeders
  mail       mailer.send never hands it to the SMTP relay

The control: the same password signs a normal account in, and an admin still reaches the
placeholdered row by id (it stays manageable).

Final line: "PLACEHOLDER EMAIL REFUSED OK".
"""

import os
import secrets
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_placeholder_")

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import ApiKey, User
from app.services import ldap_auth, mailer, scim, security, settings
from tests._csrf import csrf_for, session_data

FAILS = []


def check(cond, label):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def refuses(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except ValueError:
        return True
    return False


def web_login(email, pw):
    c = TestClient(app)
    csrf_for(c)
    c.post(
        "/auth/login", data={"email": email, "password": pw, "next": "/"}, follow_redirects=False
    )
    return session_data(c).get("uid")


def set_email(uid, email):
    with engine.begin() as conn:
        conn.execute(text('UPDATE "user" SET email = :e WHERE id = :i'), {"e": email, "i": uid})


init_db()
assert security.seed_initial() is True
admin = security.get_by_email("admin")

pw = secrets.token_urlsafe(18)  # pregate: allow-secret random per-run fixture
control = security.create_user("control-520@x.com", "Control", "translator", pw)
victim = security.create_user("emailless-520@x.com", "Emailless", "translator", pw)
erased = security.create_user("erased-520@x.com", "Erased", "translator", pw)
placeholder = f"no-email-user-{victim.id}@dedup.invalid"
tomb = f"deleted-user-{erased.id}@erased.invalid"
# What the migration leaves behind: an ACTIVE account carrying a placeholder, with a password.
set_email(victim.id, placeholder)
set_email(erased.id, tomb)

# --- control: the door works for a real address ---
check(web_login(control.email, pw) == control.id, "control: a real address signs in on the web")
check(security.authenticate(control.email, pw) is not None, "control: authenticate accepts it")

# --- sign-in ---
for addr in (placeholder, placeholder.upper(), "  " + placeholder + " ", tomb):
    check(security.is_placeholder_email(addr), f"is_placeholder_email({addr!r})")
    check(security.get_by_email(addr) is None, f"get_by_email({addr!r}) resolves nobody")
    check(
        security.authenticate(addr, pw) is None,
        f"authenticate({addr!r}) refuses the right password",
    )
    check(web_login(addr, pw) is None, f"web /auth/login({addr!r}) mints no session")
check(
    not security.is_placeholder_email("person@dedup.invalid.example.com"),
    "a real lookalike domain is not a placeholder",
)
check(
    not security.is_placeholder_email("fx1@test.invalid"),
    "other .invalid fixtures are not placeholders",
)

# an admin password reset (the only reset path) does not make the placeholder a login
with Session(engine) as s:
    security.admin_set_password(s, victim.id, pw + "x", actor=admin.email)
check(
    security.authenticate(placeholder, pw + "x") is None,
    "after an admin reset the placeholder still cannot sign in",
)
check(
    web_login(placeholder, pw + "x") is None,
    "after an admin reset the web sign-in still refuses it",
)

# --- create / invite ---
check(refuses(security.validate_email, placeholder), "validate_email refuses a placeholder")
check(
    refuses(security.create_user, "no-email-user-999@dedup.invalid", "", "translator", pw),
    "create_user refuses a placeholder",
)
check(
    refuses(security.create_user, "x@erased.invalid", "", "translator", pw, enforce_policy=False),
    "create_user refuses a placeholder for the internal seeders too",
)
check(
    refuses(security.create_invite, placeholder, "translator", admin.email, org_id=None),
    "create_invite refuses a placeholder",
)

# --- SSO / SAML / LDAP never link the row by its placeholder ---
settings.set_many({"sso_trust_unverified_email": "1"})
claims = {
    "iss": "https://idp.test",
    "sub": "attacker-sub",
    "email": placeholder,
    "email_verified": True,
}
user, err = security.get_or_create_sso_user(claims)
check(user is None and err, f"OIDC refuses the placeholder ({err!r})")
user, err = security.get_or_create_saml_user(placeholder, "x")
check(user is None and err, f"SAML refuses the placeholder ({err!r})")
user, err = ldap_auth.get_or_create_ldap_user(placeholder, "x")
check(user is None and err, f"LDAP refuses the placeholder ({err!r})")
try:
    scim.create_user(None, "scim-test", {"userName": placeholder, "externalId": "ext-520"})
    scim_refused = False
except scim.ScimError as exc:
    scim_refused = exc.status == 400
check(scim_refused, "SCIM create refuses the placeholder with a 400")
with Session(engine) as s:
    row = s.get(User, victim.id)
    check(
        row.sso_subject in (None, "") and row.external_id in (None, ""),
        "the placeholdered row was not linked",
    )
    check(
        row.email == placeholder and row.active,
        "the placeholdered row is untouched and still active",
    )
    keys = s.exec(select(ApiKey).where(ApiKey.user_id == victim.id)).all()
    check(keys == [], "no key was minted for it")

# --- mail is never handed to the relay ---
sent = []


class _FakeSMTP:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **kw):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg):
        sent.append(msg["To"])


settings.set_many({"smtp_host": "smtp.test"})
real_smtp = mailer.smtplib.SMTP
mailer.smtplib.SMTP = _FakeSMTP
try:
    check(mailer.send(placeholder, "s", "b") is False, "mailer.send refuses a dedup placeholder")
    check(mailer.send(tomb, "s", "b") is False, "mailer.send refuses an erasure tombstone")
    check(
        mailer.send(control.email, "s", "b") is True,
        "control: mailer.send delivers to a real address",
    )
finally:
    mailer.smtplib.SMTP = real_smtp
    settings.set_many({"smtp_host": ""})
check(sent == [control.email], f"only the real address reached the relay {sent}")

# --- the row stays manageable by id ---
c = TestClient(app)
csrf_for(c)
r = c.post(
    "/auth/login", data={"email": "admin", "password": "admin", "next": "/"}, follow_redirects=False
)
check(r.status_code == 303 and session_data(c).get("uid") == admin.id, "admin signs in")

if FAILS:
    print("FAILED:", len(FAILS))
    sys.exit(1)
print("PLACEHOLDER EMAIL REFUSED OK")
