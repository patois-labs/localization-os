"""Security & compliance verification (offline).

- PII detect/redact/restore round-trip
- uploads encryption at rest (magic header, transparent read; toggle off = plaintext)
- legacy DB vault recovery
- Argon2id password hash/verify
- full auth flow: first-run setup, login/logout, wrong-password 401
- RBAC: viewer can read but not write, admin-only pages gated
- audit log records mutations with the acting user
"""

import hashlib
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_sec_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.services import audit, pii, security, settings, storage, vault
from tests._csrf import csrf_for, csrf_value, sign_out

_FAULT_PW = "any typed password"  # pregate: allow-secret never checked, the check raises
_VIEWER_PW = "viewerpass-12345"  # the viewer fixture's password, set earlier in this suite


def proj(name):
    return {
        "name": name,
        "source_locale": "en-US",
        "target_locales": "fr-FR",
        "default_provider": "claude",
    }


# A wrong credential for an account that does not exist (#252 probe below).
_UNKNOWN_LOGIN = "nope-9"


def _no_full_user_scan(c) -> None:
    """Security review 2026-09 (#252): the sign-in wall and the sign-in page only ask
    whether ANY account exists. They must never load every account row: a stranger can
    repeat a failed sign-in freely, and every signed-in request pays the wall."""
    import re

    from sqlalchemy import event

    from app.db import engine

    statements: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(" ".join(str(statement).split()).lower())

    anon = TestClient(app)
    tok = csrf_value(anon)
    event.listen(engine, "before_cursor_execute", _record)
    try:
        assert c.get("/", follow_redirects=False).status_code == 200, "signed in, walled page"
        assert anon.get("/auth/login").status_code == 200
        r = anon.post(
            "/auth/login",
            data={
                "email": "nobody@x.com",
                "password": _UNKNOWN_LOGIN,
                "csrf_token": tok,
                "next": "/",
            },
        )
        assert r.status_code == 401, r.status_code
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    scans = [
        s
        for s in statements
        if re.search(r'\bfrom "?user"?(?:\s|$)', s)
        and not re.search(r"\bwhere\b|\blimit\b|count\(", s)
    ]
    assert not scans, f"#252: an unbounded read of every account row: {scans[:2]}"
    assert security.has_users() and security.user_count() >= 2, security.user_count()
    print("  #252: the wall and the sign-in page never load every account (count / exists)")


def main() -> None:
    init_db()

    # --- PII ---
    red, m = pii.redact("Email me@example.com, SSN 123-45-6789, call +1 (801) 875-1938.")
    assert "me@example.com" not in red and "123-45-6789" not in red, red
    assert pii.restore(red, m) == "Email me@example.com, SSN 123-45-6789, call +1 (801) 875-1938."
    assert any(d["type"] == "EMAIL" for d in pii.detect("a@b.com")), "detect failed"
    print("  PII redact/restore round-trip")

    # --- PII forced-ON for the API path (Phase B chunk 3a) ---
    # The global pii_redaction toggle governs the interactive/UI path. The
    # document API sets force_pii=True so inbound submissions are ALWAYS scrubbed,
    # even on an install that leaves the toggle OFF for local, air-gapped use.
    settings.set_many({"pii_redaction": "0"})
    assert not pii.enabled(), "pii_redaction should be off for this check"
    assert pii.should_redact(force=False) is False, "toggle OFF + no force -> no redaction"
    assert pii.should_redact(force=True) is True, (
        "API path (force=True) must redact even with toggle OFF"
    )
    settings.set_many({"pii_redaction": "1"})
    assert pii.should_redact(force=False) is True, "toggle ON -> redaction regardless of force"
    settings.set_many({"pii_redaction": "0"})
    print("  PII forced-ON: should_redact(force=True) redacts even when the toggle is OFF")

    # --- uploads encryption ---
    settings.set_many({"encrypt_uploads": "1"})
    p = Path(_TMP) / "u1.bin"
    storage.write_upload(p, b"confidential client document")
    raw = p.read_bytes()
    assert raw.startswith(b"LSENC1:") and b"confidential" not in raw, "not encrypted on disk"
    assert storage.read_upload(p) == b"confidential client document", "decrypt failed"
    settings.set_many({"encrypt_uploads": "0"})
    p2 = Path(_TMP) / "u2.bin"
    storage.write_upload(p2, b"plain")
    assert p2.read_bytes() == b"plain" and storage.read_upload(p2) == b"plain"
    print("  uploads encrypted at rest (toggle on); transparent read; plaintext when off")

    # --- DB vault ---
    dbf = Path(_TMP) / "x.db"
    Path(str(dbf) + ".vault").write_bytes(vault._fernet().encrypt(b"SQLITE-DATA-HERE"))
    assert b"SQLITE-DATA-HERE" not in Path(str(dbf) + ".vault").read_bytes(), "vault not encrypted"
    vault.unlock_if_needed(dbf)
    assert dbf.read_bytes() == b"SQLITE-DATA-HERE", "vault unlock failed"
    print("  legacy DB vault recovery")

    # --- password hashing ---
    h = security.hash_password("hunter2!")
    assert security.verify_password(h, "hunter2!") and not security.verify_password(h, "nope")
    print("  Argon2id password hash/verify")

    # --- auth flow ---
    c = TestClient(app)
    assert c.get("/").status_code == 200  # auth OFF: open
    assert c.post("/projects", data=proj("Pre"), follow_redirects=False).status_code == 303

    settings.set_many({"security_auth_required": "1"})  # turn auth ON
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/setup", r.headers

    # --- CSRF enforcement (auth on): the pre-session setup POST needs the token ---
    # A GET on the setup wizard mints session["csrf"]; csrf_for decodes it and
    # installs the X-CSRF-Token header on the client for all subsequent POSTs.
    # A plain HTML form post with no CSRF token (and no X-CSRF-Token header) is
    # blocked, and — since it is a full-page navigation, not a fetch — cleanly
    # redirected to re-login rather than dead-ending on a 403 (Finding 2).
    r0 = c.post(
        "/auth/setup",
        data={"email": "admin@x.com", "name": "Admin", "password": "adminpass-12345"},
        follow_redirects=False,
    )
    assert r0.status_code == 303 and r0.headers["location"].startswith("/auth/login?"), (
        f"auth on: a setup POST with no CSRF token must be blocked (redirect); got {r0.status_code}"
    )
    tok = csrf_for(c)
    assert tok, "csrf_for should mint and return a token"
    # A wrong token supplied via the header (fetch path) stays a plain 403.
    assert (
        c.post(
            "/auth/setup",
            data={"email": "admin@x.com", "name": "Admin", "password": "adminpass-12345"},
            headers={"X-CSRF-Token": "wrong-token"},
            follow_redirects=False,
        ).status_code
        == 403
    ), "a bad CSRF token (header) must be blocked"
    print("  CSRF: auth-on POST no-token form -> re-login redirect; bad header token -> 403")

    r = c.post(
        "/auth/setup",
        data={"email": "admin@x.com", "name": "Admin", "password": "short"},
        follow_redirects=False,
    )
    assert r.status_code == 400, "short setup password should be rejected"
    r = c.post(
        "/auth/setup",
        data={"email": "admin@x.com", "name": "Admin", "password": "adminpass-12345"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    csrf_for(c)  # session rotated on login -> refresh token
    assert c.get("/").status_code == 200  # authed as admin
    assert c.post("/projects", data=proj("AdminProj"), follow_redirects=False).status_code == 303
    assert any(
        "POST /projects" in row.action and row.actor == "admin@x.com" for row in audit.recent()
    ), "audit did not record the admin's project create"
    print("  first-run setup + admin authed + audited write")

    # CSRF: a cross-origin form POST is blocked; same-origin passes
    assert (
        c.post(
            "/projects",
            data=proj("Evil"),
            headers={"Origin": "http://evil.example"},
            follow_redirects=False,
        ).status_code
        == 403
    ), "cross-origin POST must be blocked"
    assert (
        c.post(
            "/projects",
            data=proj("SameOrigin"),
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    print("  CSRF: cross-origin POST 403, same-origin 303")

    # create a viewer
    assert (
        c.post(
            "/users",
            data={
                "email": "v@x.com",
                "name": "V",
                "role": "viewer",
                "password": "viewerpass-12345",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )

    sign_out(c, follow_redirects=False)
    csrf_for(c)  # logout cleared session -> refresh token
    assert c.get("/", follow_redirects=False).status_code == 303  # logged out -> login
    assert c.get("/health").json()["status"] == "ok"  # liveness, no auth
    assert c.get("/ready").json()["status"] == "ok"  # readiness (DB check)
    assert (
        c.post(
            "/auth/login", data={"email": "v@x.com", "password": "WRONG", "next": "/"}
        ).status_code
        == 401
    )
    r = c.post(
        "/auth/login",
        data={"email": "v@x.com", "password": "viewerpass-12345", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    csrf_for(c)  # login rotated session -> refresh token
    print("  login/logout + wrong-password 401 + health/ready open")

    # login lockout: 5 failures within the window lock the (email, ip) pair
    for i in range(4):
        rr = c.post("/auth/login", data={"email": "brute@x.com", "password": "nope", "next": "/"})
        assert rr.status_code == 401, (i, rr.status_code)
    rr = c.post("/auth/login", data={"email": "brute@x.com", "password": "nope", "next": "/"})
    assert rr.status_code == 429, "5th failure should trip the lockout"
    assert "from this address" in rr.text, "#222: do not promise an account-wide lock"
    rr = c.post("/auth/login", data={"email": "brute@x.com", "password": "nope", "next": "/"})
    assert rr.status_code == 429, "locked account must stay locked"
    security.clear_login_failures("brute@x.com", "testclient")
    print("  login lockout after 5 failures (429), cleared for other tests")

    # #222 reviewer probe: the second address retains a separate budget.
    email = "claims-probe@example.org"
    for _ in range(5):
        assert security.reserve_login_attempt(email, "198.51.100.1")
        security.settle_login_failure(email, "198.51.100.1")
    assert security.login_locked_seconds(email, "198.51.100.1") > 0
    assert security.login_locked_seconds(email, "198.51.100.2") == 0
    assert security.reserve_login_attempt(email, "198.51.100.2")
    security.clear_login_failures(email, "198.51.100.1")
    security.clear_login_failures(email, "198.51.100.2")

    # #292: rotating IPv6 interface identifiers must not reset the sign-in budget.
    ipv6_email = "ipv6-brute@x.com"
    addresses = [f"2001:db8:abcd:1234::{i}" for i in range(1, 7)]
    other_prefix = "2001:db8:abcd:1235::1"
    try:
        for i, address in enumerate(addresses[:5]):
            ip_client = TestClient(app, client=(address, 50000))
            csrf_for(ip_client)
            rr = ip_client.post(
                "/auth/login",
                data={"email": ipv6_email, "password": _UNKNOWN_LOGIN, "next": "/"},
                follow_redirects=False,
            )
            assert rr.status_code == (429 if i == 4 else 401), (address, rr.status_code)
        assert security.login_locked_seconds(ipv6_email, addresses[5]) > 0
        ip_client = TestClient(app, client=(addresses[5], 50000))
        csrf_for(ip_client)
        with patch.object(security, "authenticate") as authenticate:
            rr = ip_client.post(
                "/auth/login",
                data={"email": ipv6_email, "password": _UNKNOWN_LOGIN, "next": "/"},
                follow_redirects=False,
            )
            assert rr.status_code == 429, "sixth IPv6 address must be refused"
            authenticate.assert_not_called()
        assert security.login_locked_seconds(ipv6_email, other_prefix) == 0
        ip_client = TestClient(app, client=(other_prefix, 50000))
        csrf_for(ip_client)
        rr = ip_client.post(
            "/auth/login",
            data={"email": ipv6_email, "password": _UNKNOWN_LOGIN, "next": "/"},
            follow_redirects=False,
        )
        assert rr.status_code == 401, "a different /64 keeps its own budget"
    finally:
        for address in [*addresses, other_prefix]:
            security.clear_login_failures(ipv6_email, address)

    key = security._lockout_key
    # #384: the name half of the key is a sha256 digest of the normalized name, so an
    # attacker-sized typed name is never retained as dict-key text; the address half
    # keeps every #292 equivalence below.
    hk_u = hashlib.sha256(b"u").hexdigest()
    hk_probe = hashlib.sha256(b"user@x.com").hexdigest()
    assert key(" User@X.com ", "2001:db8:abcd:1234::1") == (hk_probe + "|2001:db8:abcd:1234::/64")
    assert key("u", "2001:0DB8:ABCD:1234:0000:0000:0000:0001") == key("u", "2001:db8:abcd:1234::1")
    assert key("u", "fe80::1%eth0") == key("u", "fe80::1")
    for mapped in ("::ffff:203.0.113.7", "0000:0000:0000:0000:0000:FFFF:CB00:7107"):
        assert key("u", mapped) == key("u", "203.0.113.7") == hk_u + "|203.0.113.7"
    assert key("u", "203.0.113.8") == hk_u + "|203.0.113.8"
    assert key("v", "203.0.113.7") != key("u", "203.0.113.7")
    for value in (
        "reauth-account",
        "testclient",
        "",
        "not an IP",
        "203.0.113.999",
        "fe80::1%",
        "fe80::1%eth0%bad",
        "2001:db8::/64",
        " 203.0.113.7 ",
        "\ud800",
    ):
        assert key(" User@X.com ", value) == f"{hk_probe}|{value}"
    print("  #292: IPv6 /64 lockout, equivalent addresses, IPv4 and placeholder keys")

    # TOTP gate round 3, LOW-7: a password check that RAISES (a database fault) gives its
    # up-front reservation back exactly once, so it never counts as a failed login: after
    # more faults than the threshold the pair holds only the one attempt it held before
    # (released once each, never below), is not locked, and the right password still works.
    boom = TestClient(app, raise_server_exceptions=False)
    csrf_for(boom)
    key = security._lockout_key("v@x.com", "testclient")
    security._failures[key] = [time.time()]  # one earlier wrong password stays counted
    with patch.object(security, "authenticate", side_effect=RuntimeError("database fault")):
        for i in range(security.LOCKOUT_THRESHOLD + 2):
            rr = boom.post(
                "/auth/login",
                data={"email": "v@x.com", "password": _FAULT_PW, "next": "/"},
                follow_redirects=False,
            )
            assert rr.status_code == 500, (i, rr.status_code)
    assert len(security._failures.get(key, [])) == 1, security._failures.get(key)
    assert not security.login_locked_seconds("v@x.com", "testclient")
    rr = boom.post(
        "/auth/login",
        data={"email": "v@x.com", "password": _VIEWER_PW, "next": "/"},
        follow_redirects=False,
    )
    assert rr.status_code == 303, rr.status_code
    security.clear_login_failures("v@x.com", "testclient")
    print("  LOW-7: a raising password check gives its reservation back (no lock, sign-in ok)")

    # session idle timeout logic
    assert security.session_idle_expired(last_active=0, now=10_000, idle_minutes=1)
    assert not security.session_idle_expired(last_active=0, now=30, idle_minutes=1)
    assert not security.session_idle_expired(last_active=0, now=10**9, idle_minutes=0)  # 0 = off
    print("  session idle-timeout expiry logic")

    # RBAC as viewer
    assert c.get("/projects").status_code == 200  # read OK
    assert (
        c.post("/projects", data=proj("Nope"), follow_redirects=False).status_code == 403
    )  # write blocked
    assert c.get("/users").status_code == 403  # admin-only
    print("  RBAC: viewer reads, write 403, admin pages 403")

    # back to admin -> admin pages reachable
    sign_out(c, follow_redirects=False)
    csrf_for(c)  # logout cleared session; mint a fresh anon token for the login POST
    c.post(
        "/auth/login",
        data={"email": "admin@x.com", "password": "adminpass-12345", "next": "/"},
        follow_redirects=False,
    )
    csrf_for(c)  # re-login rotated session -> refresh token
    for path in ["/users", "/security", "/audit"]:
        assert c.get(path).status_code == 200, path
    print("  admin can reach Users / Security / Audit")
    page = c.get("/security").text
    assert "Database encryption requires SQLCipher" in page
    assert 'name="encrypt_db"' not in page
    r = c.post("/security", data={"encrypt_db": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "SQLCipher" in r.headers["location"]
    assert settings.get("encrypt_db") == "0"

    # #395: the retired checkbox is absent on every normal security-page save.
    # Preserve the SQLCipher setting, including the export plaintext-staging guard.
    with patch("app.services.dbcrypt.enabled", return_value=True):
        settings.set_many({"encrypt_db": "1"})
        try:
            r = c.post(
                "/security",
                data={"security_auth_required": "1", "pii_redaction": "1"},
                follow_redirects=False,
            )
            assert r.status_code == 303 and "saved" in r.headers["location"]
            assert settings.get("pii_redaction") == "1"
            assert settings.get("encrypt_db") == "1", (
                "omitting the retired checkbox must preserve SQLCipher encryption settings"
            )
        finally:
            settings.set_many({"encrypt_db": "0", "pii_redaction": "0"})

    # --- CSRF form-field path (auth on, NO X-CSRF-Token header) ---
    # Regression guard for the bug where _csrf_ok consumed the request body via
    # form(), leaving the route's own Form(...) params empty (422). A real browser
    # HTML form has no X-CSRF-Token header; it carries the token as a hidden
    # csrf_token field. These exercise that path end-to-end on a fresh client so
    # no default header masks it. Reverting the _csrf_ok body-read fix makes them
    # FAIL (urlencoded/multipart -> 422; the route never sees its fields).
    fc = TestClient(app)  # no default CSRF header
    r = fc.post(
        "/auth/login",
        data={
            "email": "admin@x.com",
            "password": "adminpass-12345",
            "next": "/",
            "csrf_token": csrf_value(fc),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, (
        f"urlencoded form-field CSRF login must succeed (route must receive fields); got {r.status_code}"  # noqa: E501
    )
    assert r.headers["location"] == "/", "login should land on next=/"
    print("  CSRF form-field (urlencoded, no header): authed login -> 303")

    # multipart: a real document upload via the project.html flow, token as a
    # multipart field, no header. Proves the raw body is replayed to the route so
    # both the file AND the mode/csrf fields parse, and the document is created.
    fc2 = TestClient(app)
    fc2.post(
        "/auth/login",
        data={
            "email": "admin@x.com",
            "password": "adminpass-12345",
            "next": "/",
            "csrf_token": csrf_value(fc2),
        },
        follow_redirects=False,
    )
    pr = fc2.post(
        "/projects",
        data={**proj("CSRFUpload"), "csrf_token": csrf_value(fc2)},
        follow_redirects=False,
    )
    assert pr.status_code == 303, pr.status_code
    pid = int(pr.headers["location"].rstrip("/").split("/")[-1])
    up = fc2.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented", "csrf_token": csrf_value(fc2)},
        files={"file": ("hello.txt", b"Hello world. Second sentence.", "text/plain")},
        follow_redirects=False,
    )
    assert up.status_code == 303 and up.headers["location"].startswith("/documents/"), (
        f"multipart form-field CSRF upload must create a document; got {up.status_code} {up.headers.get('location')}"  # noqa: E501
    )
    print("  CSRF form-field (multipart, no header): document upload -> 303, doc created")

    # a bad token in the form field with no header must be rejected. Since this
    # is a form post (no X-CSRF-Token header), the failure path is the friendly
    # redirect to re-login, NOT a dead-end 403 (Finding 2). It must NOT be a 303
    # to the project (i.e. the write must not have happened).
    fc3 = TestClient(app)
    fc3.post(
        "/auth/login",
        data={
            "email": "admin@x.com",
            "password": "adminpass-12345",
            "next": "/",
            "csrf_token": csrf_value(fc3),
        },
        follow_redirects=False,
    )
    bad = fc3.post(
        "/projects",
        data={**proj("BadTok"), "csrf_token": "not-the-real-token"},
        follow_redirects=False,
    )
    assert (
        bad.status_code == 303 and bad.headers["location"] == "/auth/login?next=/projects&expired=1"
    ), (
        f"bad form-field token (no header) must redirect to re-login; got {bad.status_code} {bad.headers.get('location')}"  # noqa: E501
    )
    print("  CSRF form-field: bad token (no header) -> 303 re-login redirect")

    # the fetch/XHR path (X-CSRF-Token header present) keeps the plain 403 so a
    # JS error handler can react — it is NOT redirected (Finding 2 split).
    badh = fc3.post(
        "/projects",
        data=proj("BadHdr"),
        headers={"X-CSRF-Token": "not-the-real-token"},
        follow_redirects=False,
    )
    assert badh.status_code == 403, (
        f"bad token via header (fetch) must stay 403; got {badh.status_code}"
    )
    print("  CSRF header path: bad token -> 403 (no redirect, for JS handlers)")

    # --- sso_client_secret: masked input, placeholder-if-set, never blanked by
    #     an empty submission (same precedent as smtp_password) ---
    page = c.get("/security").text
    assert 'name="sso_client_secret"' in page and 'type="password"' in page
    assert "(optional" in page, (
        "unset secret should show the optional placeholder, not a stored value"
    )
    assert settings.get("sso_client_secret") == "" or settings.get("sso_client_secret") is None

    r = c.post(
        "/security",
        data={"sso_client_id": "abc123", "sso_client_secret": "top-secret-value"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # R3 hardening ride-along: sso_client_secret is now stored Fernet-ENCRYPTED at rest
    # (settings.set_secret), no longer a plaintext Setting row. read_secret_setting
    # decrypts it; the raw Setting value is ciphertext, never the plaintext.
    assert settings.read_secret_setting("sso_client_secret") == "top-secret-value"
    assert settings.get("sso_client_secret") != "top-secret-value", (
        "the client secret must be encrypted at rest, not stored in cleartext"
    )
    page = c.get("/security").text
    assert "top-secret-value" not in page, "secret value must never be echoed back to the browser"
    assert "(unchanged)" in page, "a stored secret should show the (unchanged) placeholder"
    print("  sso_client_secret: set via /security, masked input, never echoed")

    # empty submission must NOT blank the already-stored secret
    r = c.post(
        "/security",
        data={"sso_client_id": "abc123", "sso_client_secret": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert settings.read_secret_setting("sso_client_secret") == "top-secret-value", (
        "empty submission must not blank an existing stored secret"
    )
    print("  sso_client_secret: empty re-submission preserves the stored secret")

    _no_full_user_scan(c)

    settings.set_many(
        {"security_auth_required": "0", "sso_client_secret": "", "sso_client_id": ""}
    )  # reset
    print("SECURITY OK")


if __name__ == "__main__":
    main()
