"""#384 sign-in memory retention regression (offline).

Anonymous /auth/login requests must not retain attacker-sized usernames in process
memory: the length seam refuses them before any lookup or reservation, the address
limiter keys are digests of the normalized name, its dicts expire and are capped
globally, and one source address cannot rotate names past every rate limit.

Documented in: docs/architecture/identity-and-access.md
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_login_memory_")

import app.services.security_client_ip_one_authority_ as core
from app.db import init_db
from app.main import app
from app.models import LoginThrottle
from app.services import login_throttle as throttle
from app.services import security, settings
from app.web import auth

BIG = "a" * (64 * 1024)  # the review probe's attacker-sized username
GOOD = "Test-only-strong-pass-2026"  # pregate: allow-secret offline fixture
WRONG = "Test-only-wrong-pass-2026"  # pregate: allow-secret offline fixture


def _retained_key_chars():
    """Every character the process-global address-limiter dicts keep as a key."""
    return sum(
        len(key)
        for table in (
            security._failures,
            security._locked_until,
            getattr(core, "_source_failures", {}),
        )
        for key in table
    )


def long_username_checks():
    """The probe: eight distinct 64 KiB usernames return 401 and retain no name text."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine
    from tests._csrf import csrf_for

    names = [f"probe-{i}@" + BIG for i in range(8)]
    client = TestClient(app)
    csrf_for(client)
    before = _retained_key_chars()
    with patch.object(auth, "_ip", return_value="198.51.100.140"):
        for name in names:
            r = client.post(
                "/auth/login", data={"email": name, "password": WRONG}, follow_redirects=False
            )
            assert r.status_code == 401, (name[:12], r.status_code)
            # The oversized-name answer must mark both fields invalid like any other
            # wrong-credentials answer, or the error is invisible to assistive tech.
            for field in ("login-username", "login-password"):
                tag = r.text.split(f'id="{field}"', 1)[1].split(">", 1)[0]
                assert 'aria-invalid="true"' in tag, f"{field} not aria-invalid on oversized name"
    retained = _retained_key_chars() - before
    assert retained <= 512, f"#384: the limiter dicts retained {retained} chars of name text"
    with Session(engine) as s:
        for name in names:
            assert s.get(LoginThrottle, throttle._key(name)) is None, (
                "#384: a too-long name must reach no lookup and no reservation"
            )


def hashed_key_checks():
    """An attempt on any name is tracked under a digest of the normalized name."""
    ip = "198.51.100.154"
    key = security._lockout_key(BIG, ip)
    assert "a" * 64 not in key and len(key) <= 128, "the key must be a digest, not the name"
    assert security._lockout_key(" User@X.com ", ip) == security._lockout_key("user@x.com", ip)
    assert security._lockout_key("u", ip) != security._lockout_key("v", ip)
    assert security.reserve_login_attempt(BIG, ip)
    assert not any("a" * 64 in tracked for tracked in security._failures), (
        "#384: the raw typed name must never become dict-key text"
    )
    security.clear_login_failures(BIG, ip)


def expiry_cap_checks():
    """Address state expires with its window (the probe's keys_after_24h case) and the
    dicts are capped globally, whatever the traffic."""
    stale = time.time() - security.LOCKOUT_WINDOW_S - 1
    for i in range(3):
        security._failures[f"stale-f-{i}"] = [stale]
        security._locked_until[f"stale-l-{i}"] = stale
    core._source_failures["stale-src"] = [stale]
    assert security.reserve_login_attempt("fresh-a@example.test", "198.51.100.155")
    assert not [k for k in security._failures if k.startswith("stale-f-")]
    assert not [k for k in security._locked_until if k.startswith("stale-l-")]
    assert "stale-src" not in core._source_failures

    held = [f"probe-{i}" for i in range(8)]
    for k in held:
        security._failures[k] = [time.time()]
    shifted = time.time() + 24 * 3600
    with patch.object(core, "time", SimpleNamespace(time=lambda: shifted)):
        assert security.reserve_login_attempt("fresh-b@example.test", "198.51.100.156")
    assert not [k for k in security._failures if k.startswith("probe-")], (
        "#384: every retained key must be gone once the clock passes its window"
    )

    cap = core.LOGIN_TRACK_MAX_KEYS
    for i in range(cap + 50):
        security._failures[f"fill-{i}"] = [time.time()]
    assert security.reserve_login_attempt("fresh-c@example.test", "198.51.100.157")
    assert len(security._failures) <= cap, "#384: the failures dict must stay bounded"


def source_budget_checks():
    """Rotating fresh names from one source cannot buy checks past the aggregate budget."""
    ip = "198.51.100.160"
    with patch.object(core, "SOURCE_FAIL_LIMIT", 7):
        for i in range(7):
            name = f"rot-{i}@rot.example.test"
            assert security.reserve_login_attempt(name, ip)
            security.settle_login_failure(name, ip)
        assert not security.reserve_login_attempt("rot-8@rot.example.test", ip), (
            "#384: the per-source aggregate budget must refuse the eighth rotating name"
        )
        assert security.reserve_login_attempt("rot-8@rot.example.test", "198.51.100.161")
    security.clear_login_failures("rot-8@rot.example.test", "198.51.100.161")


def rotation_web_checks():
    """The same over the real sign-in path: past the budget the next fresh name is
    refused with 429 before any verifier runs; another source is untouched."""
    from fastapi.testclient import TestClient

    from tests._csrf import csrf_for

    client = TestClient(app)
    csrf_for(client)
    with (
        patch.object(core, "SOURCE_FAIL_LIMIT", 12),
        patch.object(auth, "_ip", return_value="198.51.100.162"),
    ):
        for i in range(12):
            r = client.post(
                "/auth/login",
                data={"email": f"web-rot-{i}@rot.example.test", "password": WRONG},
                follow_redirects=False,
            )
            assert r.status_code == 401, (i, r.status_code)
        r = client.post(
            "/auth/login",
            data={"email": "web-rot-13@rot.example.test", "password": WRONG},
            follow_redirects=False,
        )
        assert r.status_code == 429, (
            f"#384: rotating names from one source bypassed every rate limit ({r.status_code})"
        )


def overlapping_source_checks():
    from app.web import auth_mfa

    ip = "198.51.100.180"
    for i in range(core.SOURCE_FAIL_LIMIT - 1):
        name = f"overlap-failed-{i}"
        assert core.reserve_login_attempt(name, ip)
        core.settle_login_failure(name, ip)
    names = [f"overlap-pending-{i}" for i in range(40)]
    with ThreadPoolExecutor(max_workers=40) as pool:
        admitted = list(pool.map(lambda name: core.reserve_login_attempt(name, ip), names))
    assert sum(admitted) == 1, f"only one source slot remained, admitted {sum(admitted)}"
    held = names[admitted.index(True)]
    core.release_login_attempt(held, ip)
    assert core.reserve_login_attempt("after-release", ip)
    core.clear_login_failures("after-release", ip)
    assert core.reserve_login_attempt("after-success", ip)
    core.settle_login_failure("after-success", ip)
    assert not core.reserve_login_attempt("after-failure", ip)
    # A successful overlapping check releases only its own reservation. Neither
    # pruning nor success may erase another verifier's pending source capacity.
    ip = "198.51.100.181"
    with patch.object(core, "SOURCE_FAIL_LIMIT", 2):
        assert core.reserve_login_attempt("same", ip)
        assert core.reserve_login_attempt("same", ip)
        core.clear_login_failures("same", ip, reserved=False)
        assert not core.reserve_login_attempt("blocked", ip)
        core.clear_login_failures("same", ip)
        # #411: a pending check is counted until PENDING_TTL_S, so stay just inside it.
        shifted = time.time() + core.PENDING_TTL_S - 5
        with patch.object(core, "time", SimpleNamespace(time=lambda: shifted)):
            assert core.reserve_login_attempt("new", ip)
            assert not core.reserve_login_attempt("blocked", ip)
            core.settle_login_failure("same", ip)
            core.release_login_attempt("new", ip)
            assert core.reserve_login_attempt("last", ip)
            core.release_login_attempt("last", ip)
    # Password success releases in auth.py, then clears failures in auth_mfa.py.
    # That second step must not release the slot held by an overlapping request.
    ip = "198.51.100.182"
    with (
        patch.object(core, "SOURCE_FAIL_LIMIT", 1),
        patch.object(auth_mfa, "_ip", return_value=ip),
        patch.object(auth_mfa.mfa, "applies_to", return_value=False),
        patch.object(auth_mfa, "complete_sign_in", return_value="/"),
        patch.object(auth_mfa.login_throttle, "clear"),
        patch.object(auth_mfa.login_throttle, "remember_device"),
    ):
        assert core.reserve_login_attempt("password-success", ip)
        auth_mfa.finish_password_step(None, None, "/", via="password", login_key="password-success")
        assert not core.reserve_login_attempt("still-blocked", ip)
        core.release_login_attempt("password-success", ip)


def reauth_isolation_checks():
    from sqlmodel import Session

    from app.db import engine
    from app.models import User

    hashed = security.hash_password(GOOD)
    with Session(engine) as session:
        users = [
            User(email=f"reauth-isolation-{i}@example.test", name="Isolation", password_hash=hashed)
            for i in range(41)
        ]
        session.add_all(users)
        session.commit()
        ids = [user.id for user in users]
    # Exercise the real account/address reservation loop without 200 Argon2 checks.
    with patch.object(core, "verify_password", return_value=False):
        for i, uid in enumerate(ids[:-1]):
            for _ in range(core.LOCKOUT_THRESHOLD):
                assert core.check_current_password(uid, WRONG, ip=f"198.51.100.{i + 1}")
    assert core.check_current_password(ids[-1], GOOD, ip="203.0.113.199") is None, (
        "unrelated accounts exhausted the synthetic reauth-account source budget"
    )
    with patch.object(core, "verify_password", side_effect=RuntimeError("verifier fault")):
        try:
            core.check_current_password(ids[-1], GOOD, ip="203.0.113.199")
        except RuntimeError:
            pass
        else:
            raise AssertionError("verifier fault was swallowed")
    assert "203.0.113.199" not in core._source_pending
    assert core._lockout_key(users[-1].email, "reauth-account") not in core._pending_attempts


def interleaved_cap_checks():
    tables = [core._failures, core._locked_until, core._source_failures]
    tables += [getattr(core, name, {}) for name in ("_pending_attempts", "_source_pending")]
    from contextlib import ExitStack

    with ExitStack() as stack:
        for table in tables:
            stack.enter_context(patch.dict(table, {}, clear=True))
        stack.enter_context(patch.object(core, "LOGIN_TRACK_MAX_KEYS", 3))
        held = []
        for i in range(8):
            pair = (f"cap-{i}", f"203.0.113.{i + 1}")
            if core.reserve_login_attempt(*pair):
                held.append(pair)
            assert all(len(table) <= 3 for table in tables), "admission exceeded cap"
        for pair in held:
            core.settle_login_failure(*pair)
            assert all(len(table) <= 3 for table in tables), "settlement exceeded cap"
        # Repeated lock insertion and churn while a reservation remains outstanding.
        held_pair = ("pinned", "203.0.113.100")
        assert core.reserve_login_attempt(*held_pair)
        for i in range(8):
            pair = (f"lock-{i}", f"192.0.2.{i + 1}")
            for _ in range(core.LOCKOUT_THRESHOLD):
                assert core.reserve_login_attempt(*pair)
                core.settle_login_failure(*pair)
                assert all(len(table) <= 3 for table in tables), "lock insertion exceeded cap"
        core.settle_login_failure(*held_pair)
        assert len(core._failures[core._lockout_key(*held_pair)]) == 1
        assert core._source_failures[held_pair[1]], "pending source was lost during churn"
        assert all(len(table) <= 3 for table in tables)


def trusted_network_checks():
    """#411: a trusted shared NAT skips ONLY the per-source budget; every other limit holds."""
    nat = "203.0.113.77"
    outside = "198.51.100.77"

    def rotate(ip, count, prefix):
        admitted = 0
        for i in range(count):
            name = f"{prefix}-{i}@nat.example.test"
            if security.reserve_login_attempt(name, ip):
                admitted += 1
                security.settle_login_failure(name, ip)
        return admitted

    try:
        with patch.object(core, "SOURCE_FAIL_LIMIT", 7):
            # control: with no list, the source budget refuses past its limit
            assert rotate(nat, 20, "ctl") == 7, "control: the source budget caps the NAT"
            settings.set_many({"login_trusted_networks": "203.0.113.0/24, 2001:db8:1::/48"})
            for ip in (nat, "::ffff:203.0.113.78", "2001:db8:1:0:5::9"):
                assert rotate(ip, 20, "tr") == 20, f"trusted source {ip} must not be capped"
            assert not {core._source_bucket(ip) for ip in (nat, "2001:db8:1:0:5::9")} & set(
                core._source_pending
            ), "a trusted source is never counted as pending"
            assert rotate(outside, 20, "out") == 7, "an untrusted source is still capped"
            # the per-(name, source) lockout still applies inside a trusted network
            for _ in range(security.LOCKOUT_THRESHOLD):
                assert security.reserve_login_attempt("same@nat.example.test", nat)
                security.settle_login_failure("same@nat.example.test", nat)
            assert security.login_locked_seconds("same@nat.example.test", nat) > 0
            assert not security.reserve_login_attempt("same@nat.example.test", nat)
            # hostile or broken stored values trust nothing (fail closed)
            security.clear_login_failures("same@nat.example.test", nat)
            for bad in (
                "0.0.0.0/0",
                "10.0.0.0/7",
                "::/0",
                "2001::/16",
                "not-an-ip",
                "203.0.113.0/99",
            ):
                settings.set_many({"login_trusted_networks": bad})
                assert not core._source_trusted_for_login(nat), bad
            settings.set_many({"login_trusted_networks": " , ,"})
            assert not core._source_trusted_for_login(nat), "an empty list trusts nothing"
            settings.set_many({"login_trusted_networks": "203.0.113.0/24"})
            for odd in ("", "reauth-account", "testclient", "203.0.113.0/24", "203.0.113.77 "):
                assert not core._source_trusted_for_login(odd), repr(odd)
            assert not core._source_trusted_for_login("203.0.114.1")
            # the write door refuses a broad or malformed list
            for bad in ("0.0.0.0/0", "10.0.0.0/7", "2001::/16", "nope"):
                try:
                    settings.validate_value("login_trusted_networks", bad)
                except settings.InvalidSettingValue:
                    continue
                raise AssertionError(f"write door accepted {bad!r}")
            settings.validate_value("login_trusted_networks", "")
            settings.validate_value("login_trusted_networks", "203.0.113.0/24,10.0.0.0/8")
    finally:
        settings.set_many({"login_trusted_networks": ""})
        for ip in (nat, outside):
            security.clear_login_failures("same@nat.example.test", ip)


def reservation_ttl_checks():
    """#411: a leaked reservation expires on its own, on both the per-key and source budget."""
    ip = "198.51.100.210"
    t0 = time.time()
    clock = SimpleNamespace(time=lambda: t0)
    with patch.object(core, "time", clock):
        # leak LOCKOUT_THRESHOLD reservations on one pair, never settled or released
        for _ in range(security.LOCKOUT_THRESHOLD):
            assert security.reserve_login_attempt("leak@ttl.example.test", ip)
        assert not security.reserve_login_attempt("leak@ttl.example.test", ip), "pair is pinned"
        clock.time = lambda: t0 + core.PENDING_TTL_S - 1
        assert not security.reserve_login_attempt("leak@ttl.example.test", ip), "not yet expired"
        clock.time = lambda: t0 + core.PENDING_TTL_S + 1
        assert security.reserve_login_attempt("leak@ttl.example.test", ip), "per-key slot freed"
        security.release_login_attempt("leak@ttl.example.test", ip)
        # leaked reservations from different names pin the source budget, then free it
        security.clear_login_failures("leak@ttl.example.test", ip, reserved=False)
        base = t0 + core.PENDING_TTL_S + 1
        clock.time = lambda: base
        with patch.object(core, "SOURCE_FAIL_LIMIT", 3):
            for i in range(3):
                assert security.reserve_login_attempt(f"src-{i}@ttl.example.test", ip)
            assert not security.reserve_login_attempt("src-9@ttl.example.test", ip), "source pinned"
            clock.time = lambda: base + core.PENDING_TTL_S + 1
            assert security.reserve_login_attempt("src-9@ttl.example.test", ip), "source freed"
        # a late settle of an expired reservation is a no-op, never an error
        assert not security.settle_login_failure("src-0@ttl.example.test", ip)
        # a live reservation inside the TTL is still counted
        clock.time = lambda: base + core.PENDING_TTL_S + 2
        assert security.settle_login_failure("src-9@ttl.example.test", ip) is False
    for name in ("leak", "src-0", "src-1", "src-2", "src-9"):
        security.clear_login_failures(f"{name}@ttl.example.test", ip, reserved=False)
    core._pending_attempts.clear()
    core._source_pending.clear()


def trusted_network_parse_checks():
    """#411 gate: an IPv4-mapped IPv6 entry could never match (mapped sources are matched
    as IPv4), so the write door refuses it, and a stored bad list is named at startup."""
    for bad in ("::ffff:203.0.113.0/120", "::ffff:0:0/96"):
        try:
            settings.validate_value("login_trusted_networks", bad)
        except settings.InvalidSettingValue as exc:
            assert "IPv4" in str(exc), str(exc)
            continue
        raise AssertionError(f"write door accepted the never-matching {bad!r}")
    try:
        settings.set_many({"login_trusted_networks": "0.0.0.0/0"})
        message = security.check_login_trusted_networks_at_boot()
        assert "login_trusted_networks is invalid" in message, message
        settings.set_many({"login_trusted_networks": "203.0.113.0/24"})
        assert security.check_login_trusted_networks_at_boot() == ""
    finally:
        settings.set_many({"login_trusted_networks": ""})
    assert security.check_login_trusted_networks_at_boot() == ""


def trusted_network_spoof_checks():
    """#411 gate: the exemption keys on the ONE client-IP authority (security.client_ip),
    never on a raw forwarded header. Over the real sign-in route: a direct caller that
    claims a trusted address in X-Forwarded-For or Fly-Client-IP stays capped; the same
    address forwarded by a configured trusted proxy is exempt."""
    from fastapi.testclient import TestClient

    from tests._csrf import csrf_for

    def post(client, name, headers):
        return client.post(
            "/auth/login",
            data={"email": name, "password": WRONG},
            headers=headers,
            follow_redirects=False,
        ).status_code

    spoof = {"X-Forwarded-For": "203.0.113.5", "Fly-Client-IP": "203.0.113.5"}
    try:
        settings.set_many({"login_trusted_networks": "203.0.113.0/24"})
        with patch.object(core, "SOURCE_FAIL_LIMIT", 6):
            direct = TestClient(app, client=("198.51.100.240", 50000))
            csrf_for(direct)
            codes = [post(direct, f"spoof-{i}@nat.example.test", spoof) for i in range(8)]
            assert codes[:6] == [401] * 6 and codes[6:] == [429, 429], (
                f"a forged forwarded header bought the trusted-network exemption: {codes}"
            )
            settings.set_many({"trusted_proxy_ips": "198.51.100.241/32"})
            proxied = TestClient(app, client=("198.51.100.241", 50000))
            csrf_for(proxied)
            codes = [
                post(proxied, f"proxied-{i}@nat.example.test", {"X-Forwarded-For": "203.0.113.5"})
                for i in range(8)
            ]
            assert codes == [401] * 8, f"a trusted proxy's client was still capped: {codes}"
    finally:
        settings.set_many({"login_trusted_networks": "", "trusted_proxy_ips": ""})


def trusted_network_web_checks():
    """#411 gate: the Security page sets, refuses and clears the list through the same
    validator as `config set` and /api config."""
    from fastapi.testclient import TestClient

    from tests._csrf import csrf_for

    client = TestClient(app)
    client.get("/security")
    csrf_for(client)
    page = client.get("/security").text
    assert 'name="login_trusted_networks"' in page, "Security page has no networks field"
    base = {"audit_enabled": "1", "login_networks_sent": "1"}
    try:
        r = client.post(
            "/security",
            data={**base, "login_trusted_networks": " 203.0.113.0/24, 2001:db8:1::/48 "},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/security?saved=1", r.headers["location"]
        assert settings.get("login_trusted_networks") == "203.0.113.0/24, 2001:db8:1::/48"
        assert "203.0.113.0/24, 2001:db8:1::/48" in client.get("/security").text
        for bad in ("0.0.0.0/0", "nope", "::ffff:203.0.113.0/120"):
            r = client.post(
                "/security",
                data={**base, "login_trusted_networks": bad},
                follow_redirects=False,
            )
            assert r.headers["location"] == "/security?err=login_networks", (bad, r.headers)
            assert settings.get("login_trusted_networks") == "203.0.113.0/24, 2001:db8:1::/48"
        err = client.get("/security?err=login_networks").text
        assert "Trusted sign-in networks must be" in err
        # a post without the field leaves the list unchanged; the blank field clears it
        r = client.post("/security", data={"audit_enabled": "1"}, follow_redirects=False)
        assert settings.get("login_trusted_networks") == "203.0.113.0/24, 2001:db8:1::/48"
        r = client.post(
            "/security", data={**base, "login_trusted_networks": ""}, follow_redirects=False
        )
        assert r.headers["location"] == "/security?saved=1", r.headers["location"]
        assert settings.get("login_trusted_networks") == ""
    finally:
        settings.set_many({"login_trusted_networks": ""})


def main():
    init_db()
    errors = []
    for check in (overlapping_source_checks, reauth_isolation_checks, interleaved_cap_checks):
        try:
            check()
        except AssertionError as exc:
            errors.append(f"{check.__name__}: {exc}")
    assert not errors, errors
    long_username_checks()
    print("  8 rotating 64 KiB usernames: 401, no retained name text, no throttle row")
    hashed_key_checks()
    print("  limiter keys are digests of the normalized name")
    expiry_cap_checks()
    print("  address state expires with its window and stays capped")
    source_budget_checks()
    trusted_network_checks()
    print("  #411: trusted networks skip only the per-source budget")
    trusted_network_parse_checks()
    print("  #411: IPv4-mapped entries refused; a stored bad list is named at startup")
    trusted_network_spoof_checks()
    print("  #411: forged forwarded headers never reach the exemption")
    trusted_network_web_checks()
    print("  #411: Security page sets, refuses and clears the list")
    reservation_ttl_checks()
    print("  #411: a leaked reservation expires after PENDING_TTL_S")
    rotation_web_checks()
    print("  per-source budget: rotating names cannot buy checks past the limit")
    print("verify_login_memory OK")


if __name__ == "__main__":
    main()
