"""OIDC SSO verification (fully offline).

Builds a fake IdP with a real RSA keypair served through httpx.MockTransport:
  /.well-known/openid-configuration, /jwks, /token (RS256-signed id_token).
Then exercises the whole relying-party flow end to end:

  - GET /auth/oidc/login -> 303 to the IdP authorize URL (state/nonce/PKCE S256)
  - callback with code+state -> user auto-created (sso_default_role), session
    logged in, audit row "login.sso"
  - PKCE verifier is checked server-side against the challenge
  - wrong state -> 401; tampered id_token signature -> 401
  - SSO can NOT take over a local password admin account
  - disabled toggles: GET /auth/oidc/login and POST /auth/sso show not configured (501)
"""

import base64
import hashlib
import os
import sys
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_oidc_")

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.models import Role
from app.services import audit, oidc, security, settings
from tests._csrf import csrf_for

ISSUER = "https://idp.test"
CLIENT_ID = "loc-studio-client"
KID = "test-key-1"


class LoginControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.forms = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))
        elif tag == "form":
            self.forms.append(dict(attrs))


def check_login_navigation(client):
    next_path = "/projects?view=all&sort=name"
    for page in (
        client.get("/auth/login", params={"next": next_path}),
        client.get("/auth/oidc/callback?error=access_denied"),
    ):
        controls = LoginControls()
        controls.feed(page.text)
        assert not any(f.get("action") == "/auth/sso" for f in controls.forms), (
            "OIDC must not start a form redirect chain blocked by form-action 'self'"
        )
        links = [a for a in controls.links if a.get("href", "").startswith("/auth/oidc/login?")]
        assert len(links) == 1, "OIDC needs a keyboard-accessible navigation link"
        assert {"ghost", "button"} <= set(links[0].get("class", "").split())
        assert "form-action 'self';" in page.headers["content-security-policy"]
        if page.status_code == 200:
            target = urlparse(links[0]["href"])
            assert dict(parse_qsl(target.query))["next"] == next_path
            redirect = client.get(links[0]["href"], follow_redirects=False)
            assert redirect.status_code == 303
            destination = urlparse(redirect.headers["location"])
            assert destination.scheme + "://" + destination.netloc == ISSUER
            assert dict(parse_qsl(destination.query))["state"]
    print("  login and error pages: styled OIDC GET link, encoded next, IdP redirect with state")


_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_uint(n: int) -> str:
    return _b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


_PUB = _PRIVATE_KEY.public_key().public_numbers()
JWK = {
    "kty": "RSA",
    "use": "sig",
    "alg": "RS256",
    "kid": KID,
    "n": _b64url_uint(_PUB.n),
    "e": _b64url_uint(_PUB.e),
}

# what the NEXT /token call should mint (the test sets these per scenario)
PENDING = {
    "email": "sso.user@corp.test",
    "nonce": "",
    "challenge": "",
    "tamper": False,
    # identity-01: per-scenario claim overrides. `sub` defaults to one stable subject per
    # email (a different person per address); a value of None drops the claim.
    "claims": {},
}
SEEN = {"token_calls": 0}


def _idp_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/.well-known/openid-configuration":
        return httpx.Response(
            200,
            json={
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "jwks_uri": ISSUER + "/jwks",
            },
        )
    if path == "/jwks":
        return httpx.Response(200, json={"keys": [JWK]})
    if path == "/token":
        SEEN["token_calls"] += 1
        form = dict(parse_qsl(request.content.decode("utf-8")))
        assert form.get("grant_type") == "authorization_code", form
        assert form.get("client_id") == CLIENT_ID, form
        assert form.get("client_secret") == "s3cret-value", "client_secret not sent"
        # PKCE: the verifier must hash (S256) to the challenge from /authorize
        verifier = form.get("code_verifier") or ""
        assert verifier, "PKCE code_verifier missing from token exchange"
        if PENDING["challenge"]:
            got = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
            assert got == PENDING["challenge"], "PKCE verifier does not match challenge"
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "sub-" + PENDING["email"],
            "email": PENDING["email"],
            "email_verified": True,
            "name": "Sso User",
            "nonce": PENDING["nonce"],
            "iat": now,
            "exp": now + 300,
        }
        for k, v in PENDING["claims"].items():
            if v is None:
                claims.pop(k, None)
            else:
                claims[k] = v
        id_token = jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})
        if PENDING["tamper"]:  # corrupt the signature bytes
            id_token = id_token[:-6] + ("AAAAAA" if not id_token.endswith("AAAAAA") else "BBBBBB")
        return httpx.Response(
            200, json={"access_token": "at-123", "token_type": "Bearer", "id_token": id_token}
        )
    return httpx.Response(404, json={"error": "unknown path " + path})


def _begin(c: TestClient, next_path: str = "/"):
    """Navigate to the login link's GET route and parse the IdP authorize query."""
    r = c.get("/auth/oidc/login", params={"next": next_path}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    loc = r.headers["location"]
    assert loc.startswith(ISSUER + "/authorize?"), loc
    q = dict(parse_qsl(urlparse(loc).query))
    PENDING["nonce"] = q["nonce"]
    PENDING["challenge"] = q["code_challenge"]
    return q


def _callback(c: TestClient, addr: str, code: str, **claims):
    """One full OIDC round trip whose id_token names `addr`, with claim overrides (an
    `email` override, including None to drop it, wins over `addr`)."""
    PENDING["email"] = addr
    PENDING["claims"] = claims
    try:
        q = _begin(c)
        return c.get(f"/auth/oidc/callback?code={code}&state={q['state']}", follow_redirects=False)
    finally:
        PENDING["claims"] = {}


def _refused_audit(target: str) -> int:
    return sum(1 for r in audit.recent() if r.action == "login.sso_refused" and r.target == target)


def check_subject_binding(c: TestClient) -> None:
    """identity-01: OIDC links by a VERIFIED email once, then by (issuer, sub) only. Every
    refusal is a 401 that changes nothing on the account."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import User

    boss = security.create_user("boss2@corp.test", "Boss Two", Role.ADMIN, "", auth_source="sso")
    assert not boss.sso_subject, "precondition: a pre-upgrade SSO admin is unbound"

    def unchanged_boss():
        row = security.get_by_email("boss2@corp.test")
        return row.auth_source == "sso" and not row.sso_subject and row.role == Role.ADMIN

    # email_verified false / absent / preferred_username only: no first link by email.
    for i, (label, email, claims) in enumerate(
        (
            ("email_verified=false", "boss2@corp.test", {"email_verified": False}),
            ("email_verified absent", "boss2@corp.test", {"email_verified": None}),
            ("email_verified='false'", "boss2@corp.test", {"email_verified": "false"}),
            (
                "preferred_username only",
                "boss2@corp.test",
                {"email": None, "preferred_username": "boss2@corp.test"},
            ),
        )
    ):
        before = _refused_audit("boss2@corp.test")
        r = _callback(c, email, f"bind-{i}", sub="attacker-sub", **claims)
        assert r.status_code == 401, (label, r.status_code)
        assert unchanged_boss(), f"{label}: the SSO admin account was changed"
        if label != "preferred_username only":
            assert _refused_audit("boss2@corp.test") == before + 1, f"{label}: refusal not audited"
    print("  unverified email, absent email_verified, preferred_username: 401, admin untouched")

    # An unverified unknown email creates nothing.
    before = security.user_count()
    r = _callback(c, "stranger@corp.test", "bind-stranger", email_verified=False)
    assert r.status_code == 401 and security.user_count() == before
    assert security.get_by_email("stranger@corp.test") is None
    print("  unverified unknown email: 401, no account created")

    # A missing `sub` fails id_token validation outright.
    r = _callback(c, "nosub@corp.test", "bind-nosub", sub=None)
    assert r.status_code == 401 and security.get_by_email("nosub@corp.test") is None
    print("  id_token without sub: 401")

    # The real owner (verified) links once; the binding is recorded and audited.
    r = _callback(c, "boss2@corp.test", "bind-owner", sub="boss2-real-sub")
    assert r.status_code == 303, r.status_code
    row = security.get_by_email("boss2@corp.test")
    assert (row.sso_issuer, row.sso_subject) == (ISSUER, "boss2-real-sub")
    assert any(
        a.action == "login.sso_linked" and a.target == "boss2@corp.test" for a in audit.recent()
    ), "first link not audited"
    print("  verified first link binds (issuer, sub), audited login.sso_linked")

    # Another subject with the same VERIFIED email is refused: the account is bound.
    before = _refused_audit("boss2@corp.test")
    r = _callback(c, "boss2@corp.test", "bind-other", sub="someone-else")
    assert r.status_code == 401 and "different single sign-on identity" in r.text, r.status_code
    row = security.get_by_email("boss2@corp.test")
    assert row.sso_subject == "boss2-real-sub", "binding moved to the other subject"
    assert _refused_audit("boss2@corp.test") == before + 1
    print("  second subject for a bound address: 401, binding kept, audited")

    # The bound subject signs in even after its email changed at the IdP (no new account).
    before = security.user_count()
    r = _callback(
        c, "boss2.renamed@corp.test", "bind-renamed", sub="boss2-real-sub", email_verified=False
    )
    assert r.status_code == 303 and security.user_count() == before
    assert security.get_by_email("boss2.renamed@corp.test") is None
    print("  bound subject signs in by sub (email claim change ignored, no new account)")

    # LDAP and SAML accounts are never adopted by OIDC, even with a verified email.
    for email, source in (("dir.user@corp.test", "ldap"), ("saml.user@corp.test", "saml")):
        security.create_user(email, source, Role.MANAGER, "", auth_source=source)
        r = _callback(c, email, "bind-" + source)
        assert r.status_code == 401 and source.upper() in r.text, (source, r.status_code)
        row = security.get_by_email(email)
        assert row.auth_source == source and not row.sso_subject, f"{source} account re-marked"
        assert _refused_audit(email) == 1
    print("  LDAP and SAML accounts: 401, auth_source unchanged, audited")

    # A SCIM-provisioned account is bound but keeps its SCIM lifecycle marker.
    scim = security.create_user("scim.user@corp.test", "Scim", Role.TRANSLATOR, "")
    with Session(engine) as s:
        srow = s.get(User, scim.id)
        srow.auth_source = "scim"
        srow.external_id = "okta-123"
        s.add(srow)
        s.commit()
    r = _callback(c, "scim.user@corp.test", "bind-scim")
    assert r.status_code == 303, r.status_code
    row = security.get_by_email("scim.user@corp.test")
    assert row.auth_source == "scim" and row.sso_subject == "sub-scim.user@corp.test"
    print("  SCIM account: linked by verified email, auth_source stays scim")

    # email_verified as the string "true" is accepted (some IdPs send strings).
    r = _callback(c, "strtrue@corp.test", "bind-strtrue", email_verified="true")
    assert r.status_code == 303 and security.get_by_email("strtrue@corp.test").sso_subject
    assert any(
        a.action == "login.sso_linked" and a.target == "strtrue@corp.test" for a in audit.recent()
    ), "JIT first link not audited"
    print("  email_verified='true' (string) accepted; JIT account bound, audited")

    # Gate #420: a BOUND account that LDAP then took over is not signed into by its old
    # OIDC subject either (the bound path applies the same provider rule as the email path).
    with Session(engine) as s:
        lrow = s.exec(select(User).where(User.email == "strtrue@corp.test")).one()
        lrow.auth_source = "ldap"
        s.add(lrow)
        s.commit()
    before = _refused_audit("strtrue@corp.test")
    r = _callback(c, "strtrue@corp.test", "bind-strtrue-ldap")
    assert r.status_code == 401 and "LDAP" in r.text, r.status_code
    assert security.get_by_email("strtrue@corp.test").auth_source == "ldap"
    assert _refused_audit("strtrue@corp.test") == before + 1
    print("  bound account later moved to LDAP: its OIDC subject is refused, audited")

    # Admin override for an IdP that never sends email_verified (default off).
    assert settings.get("sso_trust_unverified_email") == "0"
    try:
        settings.validate_value("sso_trust_unverified_email", "yes")
        raise AssertionError("a non 0/1 value must be refused")
    except settings.InvalidSettingValue:
        pass
    settings.set_many({"sso_trust_unverified_email": "1"})
    try:
        r = _callback(c, "entra.user@corp.test", "bind-trust", email_verified=None)
        assert r.status_code == 303, r.status_code
        assert security.get_by_email("entra.user@corp.test").sso_subject
    finally:
        settings.set_many({"sso_trust_unverified_email": "0"})
    print("  sso_trust_unverified_email=1 links without email_verified; validator is 0/1 only")

    # After the admin switches identity providers, a verified email re-binds the account.
    user, err = security.get_or_create_sso_user(
        {
            "iss": "https://idp2.test",
            "sub": "boss2-at-idp2",
            "email": "boss2@corp.test",
            "email_verified": True,
        }
    )
    assert user is not None and err is None, err
    assert (user.sso_issuer, user.sso_subject) == ("https://idp2.test", "boss2-at-idp2")
    print("  new issuer: verified email re-binds the account to the new provider")


def main() -> None:
    init_db()
    transport = httpx.MockTransport(_idp_handler)
    oidc.set_default_transport(transport)
    oidc.clear_cache()

    c = TestClient(app)

    # --- disabled: routes fall back gracefully ---
    assert settings.get("sso_enabled") != "1"
    r = c.get("/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 501 and "SSO is not configured." in r.text
    assert c.post("/auth/sso", data={"next": "/"}).status_code == 501
    print("  SSO disabled: GET /auth/oidc/login and POST /auth/sso show not configured")

    # --- enable SSO + the login wall (no local users yet) ---
    settings.set_many(
        {
            "sso_enabled": "1",
            "sso_provider": "oidc",
            "sso_issuer": ISSUER,
            "sso_client_id": CLIENT_ID,
            "sso_client_secret": "s3cret-value",
            "security_auth_required": "1",
        }
    )
    assert oidc.enabled()
    check_login_navigation(c)
    csrf_for(c)
    legacy = c.post("/auth/sso", data={"next": "/projects"}, follow_redirects=False)
    assert legacy.status_code == 303
    assert legacy.headers["location"] == "/auth/oidc/login?next=%2Fprojects"

    # --- begin: authorize redirect carries state/nonce/PKCE ---
    q = _begin(c, next_path="/projects")
    assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID
    assert q["scope"] == "openid email profile"
    assert q["redirect_uri"] == "http://testserver/auth/oidc/callback", q["redirect_uri"]
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) >= 40
    assert q["state"] and q["nonce"]
    print("  authorize redirect: code+PKCE(S256)+state+nonce+redirect_uri correct")

    # --- callback: token exchange, id_token verified, user auto-created ---
    r = c.get(f"/auth/oidc/callback?code=authcode-1&state={q['state']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/projects", (
        r.status_code,
        r.headers.get("location"),
    )
    u = security.get_by_email("sso.user@corp.test")
    assert u is not None, "SSO user was not created"
    assert u.role == settings.get("sso_default_role") == "translator", u.role
    assert u.auth_source == "sso" and u.active
    assert c.get("/", follow_redirects=False).status_code == 200, "session not logged in"
    assert any(
        row.action == "login.sso" and row.actor == "sso.user@corp.test" for row in audit.recent()
    ), "audit row login.sso missing"
    assert SEEN["token_calls"] == 1
    print(
        "  callback: user auto-created (translator, auth_source=sso), "
        "logged in, audited, next honored"
    )

    # --- replay: the one-shot state is gone, same callback again fails ---
    r = c.get(f"/auth/oidc/callback?code=authcode-1&state={q['state']}", follow_redirects=False)
    assert r.status_code == 401, "replayed callback must fail (state single-use)"
    print("  replayed callback rejected (session state is single-use)")

    # --- wrong state -> 401 ---
    q = _begin(c)
    r = c.get("/auth/oidc/callback?code=authcode-2&state=WRONG-STATE", follow_redirects=False)
    assert r.status_code == 401, r.status_code
    print("  wrong state -> 401")

    # --- NON-ASCII state -> 401, not a 500 (task_3cc0e09a class) ---
    # `state` is a callback QUERY param; Starlette utf-8-decodes it, so a non-ASCII value
    # (%C3%A9 -> é) reached a raw secrets.compare_digest(str, str) and raised TypeError -> an
    # unauthenticated 500. Now via security.constant_time_eq -> a clean state MISMATCH -> 401.
    q = _begin(c)
    r = c.get(
        "/auth/oidc/callback?code=authcode-2b&state=caf%C3%A9-" + "z" * 20, follow_redirects=False
    )
    assert r.status_code == 401, (
        f"a non-ASCII OIDC state must be 401, never a compare_digest 500: {r.status_code}"
    )
    print("  non-ASCII state -> 401 (not a compare_digest 500)")

    # --- tampered id_token signature -> 401 ---
    q = _begin(c)
    PENDING["tamper"] = True
    before = security.user_count()
    r = c.get(f"/auth/oidc/callback?code=authcode-3&state={q['state']}", follow_redirects=False)
    PENDING["tamper"] = False
    assert r.status_code == 401, "tampered signature must be rejected"
    assert "validation failed" in r.text
    assert security.user_count() == before, "no user may be created from a bad token"
    print("  tampered id_token signature -> 401, no user created")

    # --- SSO must NOT take over a local password admin ---
    security.create_user("boss@corp.test", "Boss", Role.ADMIN, "boss-password-123")
    PENDING["email"] = "boss@corp.test"
    q = _begin(c)
    r = c.get(f"/auth/oidc/callback?code=authcode-4&state={q['state']}", follow_redirects=False)
    assert r.status_code == 401, "SSO must refuse a local admin takeover"
    assert "local password" in r.text, r.text[:200]
    boss = security.get_by_email("boss@corp.test")
    assert boss.auth_source == "local" and boss.password_hash, "admin account mutated"
    print("  local password admin takeover refused (401), account untouched")

    # --- a NON-admin local PASSWORD user is NOT shadowed by SSO (finding F5:
    #     takeover protection widened from admin-only to ANY password account) ---
    security.create_user("mixed@corp.test", "Mixed", Role.REVIEWER, "mixed-password-123")
    PENDING["email"] = "mixed@corp.test"
    q = _begin(c)
    r = c.get(f"/auth/oidc/callback?code=authcode-5&state={q['state']}", follow_redirects=False)
    assert r.status_code == 401, "SSO must not take over a non-admin local password account"
    assert "local password" in r.text, r.text[:200]
    mixed = security.get_by_email("mixed@corp.test")
    assert mixed.auth_source == "local" and mixed.role == Role.REVIEWER and mixed.password_hash, (
        "non-admin local password account mutated by SSO"
    )
    print("  non-admin local password account NOT shadowed by SSO (F5), account untouched")

    # --- an existing SSO-provisioned user (no local password) is still reused,
    #     so widening did not break the normal returning-SSO-user path ---
    sso_only = security.create_user(
        "ssoonly@corp.test", "SSO Only", Role.REVIEWER, "", auth_source="sso"
    )
    assert not sso_only.password_hash, "precondition: SSO-provisioned user has no password"
    PENDING["email"] = "ssoonly@corp.test"
    q = _begin(c)
    r = c.get(f"/auth/oidc/callback?code=authcode-6&state={q['state']}", follow_redirects=False)
    assert r.status_code == 303, "returning passwordless SSO user must still log in"
    reused = security.get_by_email("ssoonly@corp.test")
    assert reused.auth_source == "sso" and reused.role == Role.REVIEWER, (
        "passwordless SSO user should be reused (role kept)"
    )
    print("  passwordless SSO-provisioned user still reused/adopted (normal path intact)")
    # identity-01 upgrade path: that pre-existing, unbound account bound itself to the
    # subject on this verified sign-in.
    assert (reused.sso_issuer, reused.sso_subject) == (ISSUER, "sub-ssoonly@corp.test"), (
        reused.sso_issuer,
        reused.sso_subject,
    )
    print("  legacy unbound SSO account binds to (issuer, sub) on its next verified sign-in")

    check_subject_binding(c)

    # --- claims mapping guard: no usable email -> refusal ---
    user, err = security.get_or_create_sso_user(
        {"iss": ISSUER, "sub": "x", "preferred_username": "justaname"}
    )
    assert user is None and "email" in (err or ""), (user, err)
    print("  claims mapping: email required")

    settings.set_many({"security_auth_required": "0", "sso_enabled": "0"})
    oidc.set_default_transport(None)
    print("OIDC OK")


if __name__ == "__main__":
    main()
