"""Persistent password budget regression for security review #222 (offline)."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_account_lockout_")

from app.db import init_db
from app.models import LoginThrottle
from app.services import login_throttle as throttle
from app.services import security

GOOD = "Test-only-strong-pass-2026"  # pregate: allow-secret offline fixture
WRONG = "Test-only-wrong-pass-2026"  # pregate: allow-secret offline fixture


def lock(name):
    throttle.clear(name)
    for i in range(20):
        held = throttle.reserve(name, f"203.0.113.{i}")
        assert held
        throttle.settle(held, failed=True)
    assert throttle.locked_seconds(name) > 0


def web_checks():
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine
    from app.main import app
    from app.models import AuditLog, Role
    from app.services import apikeys, ldap_auth, mfa, settings
    from app.web import auth
    from tests._cli_runner import run_cli
    from tests._csrf import csrf_for

    owner = security.create_user("owner@example.test", "Owner", Role.ADMIN, GOOD)
    other = security.create_user("other@example.test", "Other", Role.VIEWER, GOOD)
    settings.set_many({"security_auth_required": "1"})

    def login(client, name, value=WRONG, ip="192.0.2.100"):
        client.get("/auth/login")
        csrf_for(client)
        with patch.object(auth, "_ip", return_value=ip):
            return client.post(
                "/auth/login", data={"email": name, "password": value}, follow_redirects=False
            )

    browser = TestClient(app, base_url="https://testserver")
    response = login(browser, owner.email, GOOD)
    assert response.status_code == 303
    cookie = browser.cookies.get(throttle.DEVICE_COOKIE)
    assert cookie and throttle.known_device(cookie, owner)
    cookie_header = response.headers.get("set-cookie", "").lower()
    assert all(x in cookie_header for x in ("httponly", "secure", "samesite=lax", "2592000"))
    assert not throttle.known_device(cookie, other)
    assert not throttle.known_device(cookie[:-1] + ("1" if cookie[-1] != "1" else "2"), owner)
    with patch.object(throttle, "_now", return_value=throttle._now() + throttle.DEVICE_AGE + 1):
        assert not throttle.known_device(cookie, owner)

    # Failures distributed over 25 real request attempts lock at the twentieth failure.
    anonymous = TestClient(app)
    with patch.object(mfa, "_notify_owner_locked") as mail:
        for i in range(25):
            response = login(anonymous, owner.email, ip=f"198.51.100.{i}")
            assert response.status_code == (401 if i < 19 else 429), (i, response.status_code)
        assert not mail.called, "SMTP must not affect the refusal response timing"
        throttle.notify_owners()
        assert any(c.args[0] == owner.email for c in mail.call_args_list)
    assert throttle.locked_seconds(owner.email) > 0
    # Simulated process restart clears only the old address layer.
    security._failures.clear()
    security._locked_until.clear()
    assert login(anonymous, owner.email, GOOD).status_code == 429
    forged = TestClient(app)
    forged.cookies.set(throttle.DEVICE_COOKIE, "forged")
    assert login(forged, owner.email, GOOD).status_code == 429

    # A name that was once an LDAP alias can later become a distinct local account.
    # A cookie for the old directory target must never bypass this account's lock.
    forged.cookies.set(throttle.DEVICE_COOKIE, throttle.device_value(other))
    throttle._change(
        throttle._key(owner.email), lambda row: ({"canonical_name": other.email}, True)
    )
    assert login(forged, owner.email, GOOD).status_code == 429
    assert throttle.account_name(owner.email) == owner.email
    assert login(forged, owner.email, GOOD).status_code == 429

    unknown = "absent@example.test"
    lock(unknown)
    with patch.object(security, "authenticate") as verifier:
        a = login(anonymous, owner.email)
        b = login(anonymous, unknown)
        assert a.status_code == b.status_code == 429
        assert throttle.LOCK_MESSAGE in a.text and throttle.LOCK_MESSAGE in b.text
        assert not verifier.called, "Known and unknown locks take the same no-verification path"

    # Device holders still face the unchanged address layer.
    for _ in range(5):
        assert security.reserve_login_attempt(owner.email, "192.0.2.77")
        security.settle_login_failure(owner.email, "192.0.2.77")
    assert login(browser, owner.email, GOOD, "192.0.2.77").status_code == 429
    assert login(browser, owner.email, GOOD).status_code == 303
    assert throttle.locked_seconds(owner.email) == 0
    # Success resets escalation, so the next lock is 15 minutes again.
    lock(owner.email)
    assert 898 <= throttle.locked_seconds(owner.email) <= 900

    browser.get("/users")
    csrf_for(browser)
    response = browser.post(f"/users/{owner.id}/unlock", follow_redirects=False)
    assert response.status_code == 303
    assert "Account sign-in lock cleared." in browser.get(response.headers["location"]).text
    assert throttle.locked_seconds(owner.email) == 0
    lock(owner.email)
    row, raw = apikeys.create_key(owner.id, "unlock test", "api docs")
    _, viewer_key = apikeys.create_key(other.id, "refused unlock", "api docs")
    denied = anonymous.post(f"/api/users/{owner.id}/unlock", headers={"X-Loc-Token": viewer_key})
    assert denied.status_code == 403 and throttle.locked_seconds(owner.email) > 0
    viewer = TestClient(app)
    assert login(viewer, other.email, GOOD).status_code == 303
    viewer.get("/")
    csrf_for(viewer)
    assert viewer.post(f"/users/{owner.id}/unlock").status_code == 403
    response = anonymous.post(f"/api/users/{owner.id}/unlock", headers={"X-Loc-Token": raw})
    assert response.status_code == 200, response.text
    assert throttle.locked_seconds(owner.email) == 0
    lock(owner.email)
    code, out, err = run_cli(["--json", "user", "unlock", owner.email])
    assert code == 0, (out, err)
    assert throttle.locked_seconds(owner.email) == 0
    with Session(engine) as s:
        events = s.exec(select(AuditLog).where(AuditLog.action == "login.account_unlocked")).all()
        assert len(events) >= 3 and all(e.actor for e in events)

    lock(owner.email)
    with Session(engine) as s:
        assert security.admin_set_password(s, owner.id, GOOD + "-reset", actor=other.email)[0]
    assert throttle.locked_seconds(owner.email) == 0

    # The account reservation exists before the LDAP entry point is called.
    ldap_name = "directory@example.test"
    binds = []

    def bind(name, value):
        with Session(engine) as s:
            row = s.get(LoginThrottle, throttle._key(name))
            assert row and len(__import__("json").loads(row.attempts)) >= 1
        binds.append(name)
        return None, None

    with (
        patch.object(ldap_auth, "enabled", return_value=True),
        patch.object(ldap_auth, "try_login", side_effect=bind),
    ):
        for i in range(25):
            response = login(anonymous, ldap_name, ip=f"192.0.2.{i}")
            assert response.status_code == (401 if i < 19 else 429)
    assert len(binds) == 20

    # An exception gives back the account reservation without consuming a failure.
    with patch.object(security, "authenticate", side_effect=RuntimeError("offline fault")):
        try:
            login(anonymous, "fault@example.test")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected verifier fault")
    with Session(engine) as s:
        row = s.get(LoginThrottle, throttle._key("fault@example.test"))
        assert row.attempts == "[]"


def storage_checks():
    from sqlmodel import Session

    from app.db import engine

    # Rolling, not fixed windows: an hour-old failure expires independently.
    name = "rolling@example.test"
    with patch.object(throttle, "_now", return_value=200000):
        throttle.settle(throttle.reserve(name, "old"), failed=True)
    with patch.object(throttle, "_now", return_value=203599):
        for i in range(18):
            throttle.settle(throttle.reserve(name, str(i)), failed=True)
    with patch.object(throttle, "_now", return_value=203601):
        held = throttle.reserve(name, "new")
        assert held
        assert not throttle.settle(held, failed=True)
        assert throttle.settle(throttle.reserve(name, "last"), failed=True)

    # A delayed result after unlock cannot consume a newer reservation.
    held = throttle.reserve("late@example.test", "one")
    throttle.clear("late@example.test")
    fresh = throttle.reserve("late@example.test", "two")
    throttle.settle(held, failed=True)
    with Session(engine) as s:
        row = s.get(LoginThrottle, throttle._key("late@example.test"))
        assert [a[0] for a in json.loads(row.attempts)] == [fresh[1]]
    throttle.settle(fresh, failed=False)

    with patch.object(throttle, "_now", return_value=300000):
        for i in range(100):
            assert throttle.reserve(f"quota-{i}", "quota-address")
        assert throttle.reserve("quota-over", "quota-address") == ()
    with patch.object(throttle, "_now", return_value=400000):
        throttle.purge_expired()
        with Session(engine) as s:
            assert s.get(LoginThrottle, throttle._key("quota-0")) is None


def ldap_alias_checks():
    from app.services import ldap_auth

    email = "canonical-directory@example.test"
    entry = SimpleNamespace(entry_dn="uid=directory,dc=test")
    search = SimpleNamespace(entries=[entry], search=lambda *a, **k: None, unbind=lambda: None)
    binds = []

    def connect(uri, dn, value):
        if dn == entry.entry_dn:
            binds.append(dn)
            raise ValueError("rejected bind")
        return search

    with (
        patch.object(ldap_auth, "_connect", side_effect=connect),
        patch.object(ldap_auth, "_escape_filter", side_effect=lambda value: value),
        patch.object(
            ldap_auth, "_entry_get", side_effect=lambda obj, key: email if key == "mail" else ""
        ),
    ):
        for i in range(25):
            alias = f"directory-alias-{i}"
            held = [throttle.reserve(alias, f"192.0.2.{i}")]
            context = throttle.start_directory_context(alias, f"192.0.2.{i}", "", held)
            try:
                result = ldap_auth._search_then_bind(alias, WRONG)
                assert i < 20 and result is None
                for reservation in held:
                    throttle.settle(reservation, failed=True)
            except throttle.AccountLocked:
                assert i >= 20
                for reservation in held:
                    throttle.settle(reservation, failed=False)
            finally:
                throttle.end_directory_context(context)
    assert len(binds) == 20, "Directory aliases must share the account budget before bind"
    assert throttle.account_name("directory-alias-0") == email
    throttle.clear(email, actor="host operator")
    assert throttle.locked_seconds("directory-alias-0") == 0


def round_one_checks():
    """Run every gate regression even on the broken tip, recording each red case."""
    import re

    from fastapi.testclient import TestClient
    from sqlmodel import Session, select
    from starlette.responses import Response

    from app import config
    from app.db import engine
    from app.main import app
    from app.models import AuditLog, Role, User
    from app.services import apikeys, settings
    from app.web import auth
    from tests._cli_runner import run_cli
    from tests._csrf import csrf_for

    settings.set_many({"security_auth_required": "1"})
    failures = []

    def check(label, fn):
        try:
            fn()
        except AssertionError as exc:
            failures.append(label)
            print(f"RED {label}: {exc}", flush=True)
        else:
            print(f"GREEN {label}", flush=True)

    def user(label):
        return security.create_user(f"fix1-{label}@example.test", label, Role.ADMIN, GOOD)

    def login(name, value=GOOD, ip="198.18.90.1", client=None):
        client = client or TestClient(app)
        client.get("/auth/login")
        csrf_for(client)
        with patch.object(auth, "_ip", return_value=ip):
            return client.post(
                "/auth/login", data={"email": name, "password": value}, follow_redirects=False
            )

    def assert_clear(name):
        with Session(engine) as s:
            row = s.get(LoginThrottle, throttle._key(name))
            assert row is not None, "unlock must create the missing row"
            assert row.attempts == "[]" and row.level == 0 and row.locked_until == 0

    # Real request traffic exhausts the NAT quota, without mocking admission or auth.
    for i in range(100):
        assert login(f"fix1-typo-{i}", WRONG).status_code == 401

    def nat():
        target = user("nat")
        assert login(target.email).status_code == 303

    check("NAT rowless real user", nat)

    def oracle():
        target = user("oracle")
        throttle._insert(throttle._key(target.email))
        client = TestClient(app)
        # Spy on the real verifier: both paths must pay the Argon2 cost.
        from app.services import security_client_ip_one_authority_ as core

        with patch.object(core, "verify_password", wraps=core.verify_password) as verify:
            a = login(target.email, WRONG, client=client)
            b = login("fix1-over-quota", WRONG, client=client)
            assert a.status_code == b.status_code == 401
            assert verify.call_count == 2

        def norm(body, name):
            # Mask only the per-request random values: the CSRF token and (since the #259
            # script CSP, 5e04b2be) each <script nonce>. Everything else must be byte-equal,
            # so a known-but-wrong and an over-quota unknown name stay indistinguishable.
            body = re.sub(r"(\snonce=\")[^\"]+", r"\1NONCE", body.replace(name, "NAME"))
            return re.sub(r"(csrf[^\"]*\"\s*(value|content)=\")[^\"]+", r"\1TOKEN", body)

        assert norm(a.text, target.email) == norm(b.text, "fix1-over-quota")
        with Session(engine) as s:
            assert s.get(LoginThrottle, throttle._key("fix1-over-quota")) is None

    check("quota oracle and password timing work", oracle)
    # Seed the exact durable exhausted state, avoiding 10,000 slow password checks.
    throttle._change(
        "creation:global",
        lambda row: ({"level": 10000, "locked_until": throttle._now() + 86400}, True),
    )

    def global_quota():
        target = user("global")
        assert login(target.email, ip="198.18.91.1").status_code == 303

    check("global rowless admin", global_quota)

    def directory():
        email = "fix1-directory@example.test"
        held = [throttle.reserve("fix1-alias", "198.18.92.1")]
        token = throttle.start_directory_context("fix1-alias", "198.18.92.1", "", held)
        try:
            try:
                throttle.reserve_directory_account(email)
            except throttle.AccountLocked:
                raise AssertionError("resolved directory identity was quota refused") from None
            with Session(engine) as s:
                row = s.get(LoginThrottle, throttle._key(email))
                assert row and len(json.loads(row.attempts)) == 1
        finally:
            throttle.end_directory_context(token)

    check("resolved LDAP canonical bypasses quota before bind", directory)

    operator = user("operator")
    throttle._insert(throttle._key(operator.email))
    browser = TestClient(app)
    assert login(operator.email, client=browser).status_code == 303
    _, raw = apikeys.create_key(operator.id, "fix1 unlock", "api docs")

    def recovery(door):
        target = user(door)
        if door == "cli":
            code, out, err = run_cli(["--json", "user", "unlock", target.email])
            assert code == 0, (out, err)
        elif door == "api":
            response = browser.post(f"/api/users/{target.id}/unlock", headers={"X-Loc-Token": raw})
            assert response.status_code == 200
        elif door == "web":
            browser.get("/users")
            csrf_for(browser)
            assert (
                browser.post(f"/users/{target.id}/unlock", follow_redirects=False).status_code
                == 303
            )
        else:
            with Session(engine) as s:
                assert security.admin_set_password(s, target.id, GOOD + "-reset")[0]
        assert_clear(target.email)
        value = GOOD + "-reset" if door == "reset" else GOOD
        assert login(target.email, value, ip="198.18.93.1").status_code == 303

    for door in ("web", "api", "cli", "reset"):
        check(f"rowless recovery {door}", lambda door=door: recovery(door))

    def revoke(kind):
        target = user(kind)
        cookie = throttle.device_value(target)
        assert throttle.known_device(cookie, target)
        if kind == "self-change":
            assert security.change_password(target.id, GOOD, GOOD + "-changed")[0]
        elif kind == "admin-change":
            with Session(engine) as s:
                assert security.admin_set_password(s, target.id, GOOD + "-changed")[0]
        elif kind == "epoch":
            security.bump_session_epoch(target.id)
        else:
            with Session(engine) as s:
                s.delete(s.get(User, target.id))
                s.commit()
            replacement = user(kind)
            assert replacement.id == target.id, "exercise SQLite ID reuse"
        assert not throttle.known_device(cookie, security.get_by_email(target.email))
        assert not throttle.known_device(cookie, target), "stale user objects cannot revive cookies"
        if kind == "delete":
            assert not throttle.known_device(cookie, replacement)

    for kind in ("self-change", "admin-change", "epoch", "delete"):
        check(f"device revocation {kind}", lambda kind=kind: revoke(kind))

    def attribution():
        target = user("audit")
        # Existing row keeps this check independent of the quota defect.
        throttle._insert(throttle._key(target.email))
        lock(target.email)
        with Session(engine) as s:
            event = s.exec(
                select(AuditLog).where(
                    AuditLog.action == "login.account_locked", AuditLog.target == target.email
                )
            ).one()
            assert event.actor == "unauthenticated 203.0.113.19"

    check("lock audit actor", attribution)

    def secure_cookie():
        for configured, scheme in ((True, "http"), (False, "https"), (False, "http")):
            with patch.object(config, "SECURE_COOKIES", configured):
                response = throttle.remember_device(
                    Response(), SimpleNamespace(url=SimpleNamespace(scheme=scheme)), operator
                )
            assert ("Secure" in response.headers["set-cookie"]) == (configured or scheme == "https")

    check("device Secure policy", secure_cookie)
    # Isolate the existing suite from this deliberately exhausted creation window.
    with Session(engine) as s:
        for row in s.exec(select(LoginThrottle).where(LoginThrottle.key.startswith("creation:"))):
            s.delete(row)
        s.commit()
    assert not failures, failures


def main():
    init_db()
    round_one_checks()
    assert LoginThrottle.__tablename__ == "loginthrottle"
    name = "distributed@example.test"
    with patch.object(throttle, "_now", return_value=100000):
        for i in range(25):
            held = throttle.reserve(name, f"198.51.100.{i}")
            assert bool(held) == (i < 20), i
            if held:
                throttle.settle(held, failed=True)
        assert throttle.locked_seconds(name) == 900
        security._failures.clear()
        security._locked_until.clear()
        assert throttle.locked_seconds(name) == 900
    for now, duration in ((100901, 1800), (102702, 3600), (106303, 3600)):
        with patch.object(throttle, "_now", return_value=now):
            for i in range(20):
                held = throttle.reserve(name, f"198.51.100.{i}")
                assert held
                throttle.settle(held, failed=True)
            assert throttle.locked_seconds(name) == duration
    throttle.clear(name)
    assert throttle.locked_seconds(name) == 0
    with ThreadPoolExecutor(max_workers=25) as pool:
        held = list(pool.map(lambda i: throttle.reserve(name, str(i)), range(25)))
    assert sum(bool(x) for x in held) == 20
    for reservation in held:
        if reservation:
            throttle.settle(reservation, failed=False)
    assert throttle.reserve(name, "fresh")
    throttle.clear(name)
    with ThreadPoolExecutor(max_workers=25) as pool:
        held = list(pool.map(lambda i: throttle.reserve(name, str(i)), range(25)))
    with ThreadPoolExecutor(max_workers=20) as pool:
        tripped = list(pool.map(lambda h: throttle.settle(h, failed=True), filter(None, held)))
    assert sum(tripped) == 1 and throttle.locked_seconds(name) > 0
    storage_checks()
    ldap_alias_checks()
    web_checks()
    print("verify_account_lockout OK")


if __name__ == "__main__":
    main()
