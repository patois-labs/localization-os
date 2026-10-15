"""Offline UX seed: real auth responses captured before the shared auth-off fixture.

Documented in: DESIGN.md. Application modules are imported inside the functions, after
fresh_fixture() has pointed the app at its disposable data directory.
"""


def seed_auth_pages(client):
    """Capture real responses before restoring the shared auth-off fixture."""
    from app.services import mfa, security, settings
    from tests._csrf import csrf_for

    pages = {}
    password = "Atlas meadow copper 2026!"  # pregate: allow-secret disposable fixture

    def capture(name, response, status=200):
        assert response.status_code == status, (name, response.status_code, response.text[:200])
        pages[name] = response.text
        print("UX auth fixture: " + name, flush=True)
        return response

    def get(name, path):
        return capture(name, client.get(path, follow_redirects=False))

    def post(path, data):
        csrf_for(client)
        return client.post(path, data=data, follow_redirects=False)

    def login(email):
        client.cookies.clear()
        client.headers.pop("X-CSRF-Token", None)
        response = post("/auth/login", {"email": email, "password": password, "next": "/"})
        assert response.status_code == 303, response.text[:200]
        csrf_for(client)

    # First-run setup is only reachable while no account exists, so capture it first.
    get("setup", "/auth/setup")
    capture(
        "setup-error",
        post("/auth/setup", {"name": "Jane Admin", "email": "jane@example.test", "password": "x"}),
        400,
    )
    get("account-off", "/account")
    admin = security.create_user("atlas-admin@example.test", "Alex Northstar", "admin", password)
    security.create_user("atlas-viewer@example.test", "Atlas reviewer", "viewer", password)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        get("login", "/auth/login")
        capture(
            "login-error",
            post("/auth/login", {"email": admin.email, "password": "incorrect", "next": "/"}),
            401,
        )
        login(admin.email)
        get("account", "/account")
        get("logout", "/auth/logout")
        get("password-change", "/auth/password-change")
        capture(
            "password-error",
            post(
                "/auth/password-change",
                {"current": "incorrect", "new": password, "confirm": password},
            ),
            400,
        )
        response = post(
            "/account/apikeys", {"name": "Atlas CI", "docs": "1", "expires_in_days": "30"}
        )
        assert response.status_code in (200, 303), response.text[:200]
        get("account-keys", "/account")
        inv = security.create_invite("invitee@example.test", "viewer", admin.email)
        get("accept", f"/auth/accept/{inv.token}")
        capture(
            "accept-error",
            post(
                f"/auth/accept/{inv.token}",
                {"name": "Atlas invitee", "password": password, "confirm": "different"},
            ),
            400,
        )
        get("accept-invalid", "/auth/accept/invalid-fixture")
        get("users", "/users")
        get("users-empty", "/users?q=does-not-exist")
        get("account-mfa-reauth", "/account/mfa/setup")
        capture("account-mfa-setup", post("/account/mfa/setup", {"current": password}))
        capture("account-mfa-error", post("/account/mfa/setup", {"code": "bad"}), 400)
        code = mfa.totp(mfa.pending_secret(admin.id), mfa._now())
        capture("account-mfa-codes", post("/account/mfa/setup", {"code": code}))
        get("account-mfa-on", "/account")
        login(admin.email)
        get("mfa-verify", "/auth/mfa")
        capture("mfa-recovery-error", post("/auth/mfa", {"recovery": "bad"}), 401)
        login("atlas-viewer@example.test")
        get("account-viewer", "/account")
        assert client.get("/users", follow_redirects=False).status_code == 403
        settings.set_many({"mfa_required": "all"})
        settings.invalidate_cache()
        login("atlas-viewer@example.test")
        get("mfa-enroll", "/auth/mfa/enroll")
        capture("mfa-enroll-error", post("/auth/mfa/enroll", {"code": "bad"}), 401)
        viewer = security.get_by_email("atlas-viewer@example.test")
        code = mfa.totp(mfa.pending_secret(viewer.id), mfa._now())
        capture("mfa-codes", post("/auth/mfa/enroll", {"code": code}))
    finally:
        settings.set_many({"security_auth_required": "0", "mfa_required": "off"})
        settings.invalidate_cache()
        client.cookies.clear()
        client.headers.pop("X-CSRF-Token", None)
    return pages
