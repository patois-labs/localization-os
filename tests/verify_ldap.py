"""LDAP authentication verification (#40) — fully offline via ldap3 MOCK_SYNC.

Builds a fake directory with ldap3's built-in MOCK_SYNC strategy (no sockets, no
real server) and drives the whole /auth/login flow end to end:

  - provision-on-first-login: an LDAP user with no local account gets one, with
    ldap_default_role and auth_source='ldap', logged in, audit 'auth.ldap_login';
  - a repeat login refreshes the display name;
  - wrong password -> LDAP declines -> local auth also fails -> counts toward the
    existing lockout (5 strikes -> 429);
  - a local password ADMIN with the same email as an LDAP identity is NEVER taken
    over (falls through to local; the local password still logs in as admin);
  - ldap_enabled=0 -> ZERO LDAP calls (the injected factory would raise);
  - the LDAP server unreachable (factory raises) -> local auth still works, no 500.
"""

import os
import ssl
import sys
from contextlib import suppress
from pathlib import Path
from unittest.mock import Mock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_ldap_")

# ldap3 is an OPT-IN (LGPL) extra kept out of the default zero-copyleft install
# (requirements-ldap.txt). When it is absent this whole suite has nothing to exercise
# — skip cleanly with exit 0 rather than erroring, so the base-only default install
# still passes the test run. When present, run fully.
try:
    from ldap3 import MOCK_SYNC, Connection, Server
except ImportError:
    print(
        "SKIP: ldap3 not installed (opt-in extra; pip install -r requirements-ldap.txt) — LDAP OK"
    )
    raise SystemExit(0) from None

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.models import Role
from app.services import audit, ldap_auth, security, settings
from tests._csrf import csrf_for

BASE_DN = "dc=corp,dc=test"
BIND_DN = "cn=svc-loc,ou=service,dc=corp,dc=test"
BIND_PW = "service-secret-pw"

# a directory: dn -> attributes (userPassword drives the bind check in MOCK_SYNC)
_DIRECTORY = {
    BIND_DN: {"userPassword": BIND_PW, "objectClass": "person"},
    "uid=alice,ou=people,dc=corp,dc=test": {
        "userPassword": "alice-dir-pw",
        "mail": "alice@corp.test",
        "cn": "Alice",
        "displayName": "Alice Anderson",
        "uid": "alice",
        "objectClass": "inetOrgPerson",
    },
    # collides (by email) with a LOCAL admin created below — takeover must be refused
    "uid=boss,ou=people,dc=corp,dc=test": {
        "userPassword": "boss-dir-pw",
        "mail": "boss@corp.test",
        "cn": "Boss",
        "displayName": "The Boss",
        "uid": "boss",
        "objectClass": "inetOrgPerson",
    },
    # collides (by email) with a NON-admin LOCAL password account (a linguist) —
    # F5 widened the takeover protection to ALL password accounts, so this must
    # also be refused (fall through to local).
    "uid=ling,ou=people,dc=corp,dc=test": {
        "userPassword": "ling-dir-pw",
        "mail": "ling@corp.test",
        "cn": "Ling",
        "displayName": "The Linguist",
        "uid": "ling",
        "objectClass": "inetOrgPerson",
    },
}

CALLS = {"n": 0, "raise": False}


def _make_conn(uri, user, password):
    """Factory injected into ldap_auth: returns a MOCK_SYNC Connection already
    'bound', raising on bad credentials to mimic ldap3 auto_bind=True. Counts
    calls so a suite can assert LDAP was (or was NOT) contacted, and can be told
    to raise to simulate an unreachable server.

    A FRESH Server per call: MOCK_SYNC keys its in-memory DIT on the Server
    object, so a shared server would ignore later add_entry() calls (and never
    reflect a directory change mid-suite, e.g. a renamed displayName)."""
    CALLS["n"] += 1
    if CALLS["raise"]:
        raise ConnectionError("LDAP server is unreachable (simulated)")
    conn = Connection(
        Server("fake-corp-dc"),
        user=user or None,
        password=password or None,
        client_strategy=MOCK_SYNC,
    )
    for dn, attrs in _DIRECTORY.items():
        conn.strategy.add_entry(dn, attrs)
    if not conn.bind():
        raise ConnectionError("simulated bind failure (bad credentials)")
    return conn


def _login(c, email, password):
    csrf_for(c)
    return c.post(
        "/auth/login",
        data={"email": email, "password": password, "next": "/"},
        follow_redirects=False,
    )


def _transport_tests():
    """Collect every transport regression even when the implementation is missing."""

    def required():
        assert ldap_auth._build_server("ldaps://dc.example").tls.validate == ssl.CERT_REQUIRED

    def custom_ca():
        ca = Path(_TMP) / "directory-ca.pem"
        ca.write_text("offline construction fixture", encoding="ascii")
        with patch.object(settings, "get", return_value=str(ca)):
            assert ldap_auth._build_server("ldaps://dc.example").tls.ca_certs_file == str(ca)
        assert settings.DEFAULTS["ldap_ca_cert"] == ""
        from app.cli.cmd_admin_helpers import _LDAP_KEYS as cli_keys
        from app.web.api_cli_admin_2 import _LDAP_KEYS as api_keys

        assert "ldap_ca_cert" in cli_keys and "ldap_ca_cert" in api_keys

    def system_ca():
        with patch.object(settings, "get", return_value=""):
            tls = ldap_auth._build_server("ldaps://dc.example").tls
        assert tls.validate == ssl.CERT_REQUIRED and tls.ca_certs_file is None
        assert tls.version is None  # ldap3 uses create_default_context with system roots
        context = Mock()
        connection = Mock()
        connection.server.host = "dc.example"
        with (
            patch("ldap3.core.tls.create_default_context", return_value=context) as create,
            patch("ldap3.core.tls.check_hostname") as hostname,
        ):
            tls.wrap_socket(connection, do_handshake=True)
        create.assert_called_once_with(
            purpose=ssl.Purpose.SERVER_AUTH, cafile=None, capath=None, cadata=None
        )
        assert context.verify_mode == ssl.CERT_REQUIRED
        hostname.assert_called_once_with(context.wrap_socket.return_value, "dc.example", None)

    def bare_host():
        server = ldap_auth._build_server("dc.example")
        assert server.ssl and server.port == 636 and server.host == "dc.example"

    def starttls_failure():
        conn = Mock()
        conn.closed = False
        conn.start_tls.return_value = False
        with (
            patch.object(ldap_auth, "_server_factory", None),
            patch("ldap3.Connection", return_value=conn) as constructor,
            suppress(Exception),
        ):
            ldap_auth._connect("ldap://dc.example", BIND_DN, BIND_PW)
        conn.start_tls.assert_called_once()
        conn.bind.assert_not_called()
        conn.rebind.assert_not_called()
        assert not constructor.call_args.kwargs.get("auto_bind")
        assert not constructor.call_args.kwargs.get("password")
        assert not constructor.call_args.kwargs.get("user")
        conn.unbind.assert_called_once()

        # Certificate failures on either credential exchange must reach the
        # rate-limited outage path, including the normally quiet user-bind leg.
        for user_leg in (False, True):
            factory = Mock(side_effect=ssl.SSLCertVerificationError("untrusted certificate"))
            secure_connect = ldap_auth._connect
            discovery = _make_conn("ldaps://dc.example", BIND_DN, BIND_PW)
            calls = 0

            def connect(uri, dn, pw, user_leg=user_leg, discovery=discovery, secure=secure_connect):
                nonlocal calls
                calls += 1
                if user_leg and calls == 1:
                    return discovery
                return secure("ldaps://dc.example", dn, pw)

            with (
                patch.object(ldap_auth, "_server_factory", None),
                patch("ldap3.Connection", factory),
                patch.object(ldap_auth, "_connect", connect),
                patch.object(ldap_auth, "_log_outage") as outage,
            ):
                assert ldap_auth._search_then_bind("alice", "alice-dir-pw") is None
            outage.assert_called_once()
            assert "SSLCertVerificationError" in outage.call_args.args[0]
            assert not factory.call_args.kwargs.get("password")
            discovery.unbind()

        # A successful upgrade may bind, but only after open and StartTLS.
        conn = Mock()
        conn.closed = False
        with (
            patch.object(ldap_auth, "_server_factory", None),
            patch("ldap3.Connection", return_value=conn),
        ):
            assert ldap_auth._connect("ldap://dc.example", BIND_DN, BIND_PW) is conn
        assert [call[0] for call in conn.mock_calls] == ["open", "start_tls", "rebind"]
        conn.rebind.assert_called_once_with(user=BIND_DN, password=BIND_PW)

    def invalid_scheme():
        original = settings.get("ldap_host")
        for host in ("http://dc.example", "bogus://dc.example", "ldap+plain://dc.example"):
            try:
                settings.set_many({"ldap_host": host})
            except ValueError:
                assert settings.get("ldap_host") == original
            else:
                raise AssertionError(f"accepted insecure host: {host}")

    def no_referral_chasing():
        """GATE HIGH-1. ldap3 defaults allowed_referral_hosts to [("*", True)] (any host,
        WITH our credentials) and auto_referrals to True. On a referral it builds a NEW
        connection to a directory-named host: for an ldap:// referral use_ssl=False,
        tls=None, our user and password copied in, then open and bind. Its StartTLS rescue
        is gated on tls_started, which is False for an ldaps:// primary, and even then it
        manufactures a default Tls() -- validate=CERT_NONE, the exact #169 defect. AD emits
        referrals routinely, so this needs no MITM of the primary channel."""
        server = ldap_auth._build_server("ldaps://dc.example")
        assert server.allowed_referral_hosts == [], (
            f"referrals may be chased with our credentials: {server.allowed_referral_hosts!r}"
        )
        conn = Mock()
        conn.closed = False
        with (
            patch.object(ldap_auth, "_server_factory", None),
            patch("ldap3.Connection", return_value=conn) as factory,
        ):
            ldap_auth._connect("ldaps://dc.example", BIND_DN, BIND_PW)
        assert factory.call_args.kwargs.get("auto_referrals") is False, (
            "auto_referrals must be off: a referral chase is a second, unhardened socket "
            "carrying the service bind password"
        )

    def unmatchable_hosts_refused():
        """GATE LOW-1. Forms that parse but can never pass ldap3's hostname check (plain
        lowercase equality), so they must fail at CONFIG time, not as a silent lockout."""
        original = settings.get("ldap_host")
        for host in ("ldaps://dc.example.", "ldaps://☃.example", "ldaps://dc.example:"):
            try:
                settings.set_many({"ldap_host": host})
            except ValueError:
                assert settings.get("ldap_host") == original
            else:
                raise AssertionError(f"accepted an unmatchable host: {host}")

    def refuse_hosts(hosts):
        original = settings.get("ldap_host")
        for host in hosts:
            try:
                settings.set_many({"ldap_host": host})
            except ValueError as exc:
                assert len(str(exc)) < 150
                assert settings.get("ldap_host") == original
            else:
                raise AssertionError(f"accepted invalid host: {host!r}")

    def wildcard_hostname_checker():
        from ldap3.core.exceptions import LDAPCertificateError
        from ldap3.core.tls import check_hostname

        sock = Mock()
        sock.getpeercert.return_value = {"subjectAltName": [("DNS", "attacker.example")]}
        check_hostname(sock, "*", None)  # Real ldap3 sentinel accepts the wrong certificate.
        try:
            check_hostname(sock, ldap_auth._build_server("dc.example").host, None)
        except LDAPCertificateError:
            pass
        else:
            raise AssertionError("real hostname checker accepted an unrelated certificate")
        refuse_hosts(("*", "ldaps://*", "ldap://*:389", "*.example", "dc*.example"))

    def sni_socket_hostname():
        # Only DNS hosts are exercised here: _build_server normalises first, and an IP
        # literal is now refused outright (see ip_literals_refused), because ldap3's
        # hostname check reads only DNS SANs so an IP can never validate. ldap_auth keeps
        # its "omit SNI for an IP literal" branch as defence if normalisation is ever
        # bypassed, but it is not reachable through this seam any more.
        for uri, expected in (
            ("ldaps://DC.Example:636", "dc.example"),
            ("ldap://DC.Example:389", "dc.example"),
        ):
            server = ldap_auth._build_server(uri)
            conn = Mock(server=server)
            context = Mock()
            with patch("ldap3.core.tls.create_default_context", return_value=context):
                server.tls.wrap_socket(conn, do_handshake=False)
            kwargs = context.wrap_socket.call_args.kwargs
            assert context.verify_mode == ssl.CERT_REQUIRED
            if expected is None:
                assert "server_hostname" not in kwargs, uri
            else:
                assert kwargs.get("server_hostname") == expected, (uri, kwargs)

    def invalid_dns_labels():
        refuse_hosts(("a" * 64 + ".example", "dc..example", ".example"))

    def control_characters():
        refuse_hosts(("dc\x00.example", "dc\x7f.example", "dc\n.example", "\tdc.example"))

    def scoped_ipv6():
        refuse_hosts(("ldap://[fe80::1%25eth0]:389", "ldaps://[fe80::1%eth0]"))

    def valid_host_boundaries():
        for host in ("a" * 63 + ".example", "dc", "xn--n3h.example", "dc.example:65534"):
            assert ldap_auth._build_server(host).tls.validate == ssl.CERT_REQUIRED

    def starttls_belt_before_bind():
        """GATE R3 LOW-2. The belt asserting tls_started before rebind was unpinned: the
        existing starttls_failure case uses a bare Mock(), whose .tls_started is a truthy
        Mock attribute, so the guard was never exercised there and deleting it changed no
        outcome. This drives the case the belt exists for: StartTLS reported success, then
        the flag is False at bind time, which is what Connection.rebind re-opening a closed
        socket in PLAINTEXT for an ldap:// server looks like."""
        conn = Mock()
        conn.closed = False
        conn.start_tls.return_value = True
        conn.tls_started = False
        with (
            patch.object(ldap_auth, "_server_factory", None),
            patch("ldap3.Connection", return_value=conn),
            suppress(Exception),
        ):
            ldap_auth._connect("ldap://dc.example", BIND_DN, BIND_PW)
        conn.rebind.assert_not_called()
        conn.bind.assert_not_called()
        conn.unbind.assert_called()
        # And the healthy ldaps:// path must NOT trip it: tls_started is False there by
        # design, because TLS was negotiated on connect rather than via StartTLS.
        healthy = Mock()
        healthy.closed = False
        healthy.tls_started = False
        with (
            patch.object(ldap_auth, "_server_factory", None),
            patch("ldap3.Connection", return_value=healthy),
        ):
            ldap_auth._connect("ldaps://dc.example", BIND_DN, BIND_PW)
        healthy.rebind.assert_called_once()
        healthy.start_tls.assert_not_called()

    def refused_host_reason_is_specific():
        """GATE R3 LOW-1. A refused stored host must report WHY, not a generic
        "TLS setup failed (ValueError)" that also implies a socket was opened."""
        with patch.object(ldap_auth, "_server_factory", None):
            try:
                ldap_auth._connect("ldaps://10.0.0.5", BIND_DN, BIND_PW)
            except ldap_auth.LDAPTransportError as exc:
                assert "ldap_host" in str(exc) and "IP literal" in str(exc), str(exc)
            else:
                raise AssertionError("an IP-literal host must be refused")

    def ip_literals_refused():
        """GATE R2 MED-1. An IP literal can never pass the hostname check: ldap3's
        match_hostname reads only DNS SANs and never an `IP Address` SAN, so `ldaps://
        10.0.0.5` raises LDAPCertificateError even against a certificate issued FOR that
        IP. Refuse it at the door rather than storing a config that fails at every login.
        This is part of the breaking upgrade: it worked before only because certificates
        were not validated at all."""
        refuse_hosts(
            (
                "192.0.2.1",
                "ldaps://192.0.2.1",
                "ldap://[2001:db8::1]:389",
                "ldaps://[2001:db8::1]",
                "ldaps://[v1.fe80::1]",
            )
        )

    def port_and_label_boundaries():
        """GATE R2 LOW-1/LOW-3. ldap3's port validator is range(0, 65535), which EXCLUDES
        65535, so accepting it stores a value that raises at every login. And a backslash
        is not a label separator, so dc.example\\evil.example would otherwise pass the
        per-label length check as one oversized-but-split label."""
        refuse_hosts(("dc.example:65535", "dc.example\\evil.example"))

    cases = [
        required,
        custom_ca,
        system_ca,
        bare_host,
        starttls_failure,
        invalid_scheme,
        no_referral_chasing,
        unmatchable_hosts_refused,
        wildcard_hostname_checker,
        sni_socket_hostname,
        invalid_dns_labels,
        control_characters,
        scoped_ipv6,
        valid_host_boundaries,
        ip_literals_refused,
        port_and_label_boundaries,
        starttls_belt_before_bind,
        refused_host_reason_is_specific,
    ]
    print("LDAP transport collected ids: " + ", ".join(case.__name__ for case in cases))
    failures = []
    for case in cases:
        try:
            case()
        except Exception as exc:
            failures.append(case.__name__)
            print(f"  FAIL {case.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  PASS {case.__name__}")
    assert not failures, f"transport regressions failed: {failures}"


# A value that looks like a password typed into the username box (audit-typedname LOW-2).
_TYPED_IN_NAME_BOX = "Tr0ub4dor&3-typed-in-the-name-box"


def _outage_log_hides_typed_name() -> None:
    """audit-typedname LOW-2. During an outage the rate-limited warning must still fire once
    per window, but the typed sign-in name (which may be a password typed into the wrong
    box) must appear nowhere in the captured records: not the message, not the args, not
    the operator-facing last_outage_reason. The simulated library errors QUOTE the typed
    value in their message, as ldap3 does when it echoes a search filter or a DN."""
    import logging

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    def _leaky_factory(uri, user, password):
        raise RuntimeError(f"invalid filter (|(uid={_TYPED_IN_NAME_BOX}))")

    def _leaky_search(username, password):
        raise RuntimeError(f"unexpected failure for {username}")

    def _ambiguous_factory(uri, user, bind_pw):
        server = Server("fake-corp-dc")
        conn = Connection(server, user, bind_pw, client_strategy=MOCK_SYNC)
        conn.strategy.add_entry(BIND_DN, {"userPassword": BIND_PW, "objectClass": "person"})
        for n in (1, 2):
            conn.strategy.add_entry(
                f"cn=dup{n},ou=people,dc=corp,dc=test",
                {"uid": _TYPED_IN_NAME_BOX, "cn": f"dup{n}", "objectClass": "inetOrgPerson"},
            )
        assert conn.bind()
        return conn

    logger = logging.getLogger("loc_tms")
    handler = _Capture(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    settings.set_many({"ldap_enabled": "1"})
    try:
        ldap_auth._last_outage_log = 0.0
        ldap_auth.set_server_factory(_leaky_factory)
        for _ in range(3):
            assert ldap_auth.try_login(_TYPED_IN_NAME_BOX, "anything") == (None, None)
        with patch.object(ldap_auth, "_search_then_bind", _leaky_search):
            ldap_auth._last_outage_log = 0.0  # a new window, so this path logs too
            assert ldap_auth.try_login(_TYPED_IN_NAME_BOX, "anything") == (None, None)
        ldap_auth.set_server_factory(_ambiguous_factory)
        assert ldap_auth.try_login(_TYPED_IN_NAME_BOX, "anything") == (None, None)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        ldap_auth.set_server_factory(_make_conn)
        ldap_auth._last_outage_log = 0.0

    outages = [r for r in records if "falling back to local auth" in r.getMessage()]
    assert len(outages) == 2, ("one outage warning per window expected", len(outages))
    assert "search-then-bind failed: RuntimeError" in outages[0].getMessage()
    assert "login raised unexpectedly" in outages[1].getMessage()
    assert any("refusing ambiguous login" in r.getMessage() for r in records), (
        "the ambiguous-match warning must still be logged"
    )
    for r in records:
        seen = " ".join([str(r.msg), repr(r.args), r.getMessage()])
        assert _TYPED_IN_NAME_BOX not in seen, ("typed sign-in name leaked to the log", seen)
    assert _TYPED_IN_NAME_BOX not in ldap_auth.last_outage_reason()
    print("  outage + ambiguous-match logs never carry the typed name; outage logged once/window")


def main() -> None:
    init_db()
    ldap_auth.set_server_factory(_make_conn)

    # login wall ON (real auth). Seed ONE local admin who also exists in LDAP.
    security.create_user("boss@corp.test", "Local Boss", Role.ADMIN, "boss-local-pw-1234")
    settings.set_many(
        {
            "security_auth_required": "1",
            "ldap_enabled": "1",
            "ldap_host": "ldap://fake-corp-dc",
            "ldap_base_dn": BASE_DN,
            "ldap_bind_dn": BIND_DN,
            "ldap_user_filter": "(|(uid={username})(mail={username}))",
            "ldap_default_role": "reviewer",
        }
    )
    settings.set_secret("ldap_bind_password_enc", BIND_PW)
    assert ldap_auth.enabled()
    assert settings.get_secret("ldap_bind_password_enc") == BIND_PW, "bind pw round-trip"
    print("  bind password stored encrypted (round-trips via get_secret)")

    # --- 1. provision-on-first-login (default role, auth_source, audit) ---
    c = TestClient(app)
    assert security.get_by_email("alice@corp.test") is None
    r = _login(c, "alice@corp.test", "alice-dir-pw")
    assert r.status_code == 303 and r.headers["location"] == "/", (r.status_code, r.text[:200])
    alice = security.get_by_email("alice@corp.test")
    assert alice is not None, "LDAP user was not provisioned"
    assert alice.auth_source == "ldap", alice.auth_source
    assert alice.role == "reviewer", ("default role not applied", alice.role)
    assert alice.name == "Alice Anderson", alice.name
    assert c.get("/", follow_redirects=False).status_code == 200, "session not logged in"
    assert any(
        row.action == "auth.ldap_login" and row.actor == "alice@corp.test" for row in audit.recent()
    ), "audit auth.ldap_login missing"
    print("  first login provisions user (reviewer, auth_source=ldap), logged in, audited")

    # --- 2. repeat login refreshes the display name ---
    _DIRECTORY["uid=alice,ou=people,dc=corp,dc=test"]["displayName"] = "Alice B. Anderson"
    c2 = TestClient(app)
    r = _login(c2, "alice@corp.test", "alice-dir-pw")
    assert r.status_code == 303, r.status_code
    alice = security.get_by_email("alice@corp.test")
    assert alice.name == "Alice B. Anderson", ("name not refreshed", alice.name)
    print("  repeat login refreshes the display name from the directory")

    # --- 3. wrong password -> LDAP declines, local fails, lockout counts ---
    security.clear_login_failures("alice@corp.test", "testclient")
    c3 = TestClient(app)
    for i in range(security.LOCKOUT_THRESHOLD - 1):
        r = _login(c3, "alice@corp.test", "wrong-pw")
        assert r.status_code == 401, (i, r.status_code)
    r = _login(c3, "alice@corp.test", "wrong-pw")  # 5th strike trips the lock
    assert r.status_code == 429, ("lockout should trip on the 5th failure", r.status_code)
    print("  wrong LDAP password falls to local, failures count toward lockout (429)")

    # --- 4. local admin takeover protection ---
    # Sign in with the LDAP directory password for boss@ — LDAP proves it, but the
    # local admin must NOT be shadowed: it falls through to local, where the DIR
    # password is not the LOCAL password, so login fails (admin identity intact).
    security.clear_login_failures("boss@corp.test", "testclient")
    c4 = TestClient(app)
    r = _login(c4, "boss@corp.test", "boss-dir-pw")
    assert r.status_code == 401, ("LDAP must not take over a local admin", r.status_code)
    boss = security.get_by_email("boss@corp.test")
    assert boss.auth_source == "local" and boss.role == Role.ADMIN, "admin account mutated"
    assert boss.password_hash, "admin password wiped"
    # and the local admin still logs in with the LOCAL password
    security.clear_login_failures("boss@corp.test", "testclient")
    c4b = TestClient(app)
    r = _login(c4b, "boss@corp.test", "boss-local-pw-1234")
    assert r.status_code == 303, ("local admin must still log in locally", r.status_code)
    assert security.get_by_email("boss@corp.test").auth_source == "local"
    print("  local password admin NOT taken over by LDAP; local login still works")

    # --- 4b. NON-admin local password account takeover protection (finding F5) ---
    # A linguist with a LOCAL password also exists in LDAP with a different dir
    # password. F5 widened the takeover refusal from admin-only to ALL password
    # accounts, so the LDAP identity must NOT shadow this account: the dir password
    # falls through to local (where it is wrong) and the LOCAL password still works.
    security.create_user("ling@corp.test", "Local Linguist", Role.LINGUIST, "ling-local-pw-1234")
    security.clear_login_failures("ling@corp.test", "testclient")
    c4c = TestClient(app)
    r = _login(c4c, "ling@corp.test", "ling-dir-pw")  # LDAP proves it, but must not take over
    assert r.status_code == 401, (
        "LDAP must not take over a non-admin local password account",
        r.status_code,
    )
    ling = security.get_by_email("ling@corp.test")
    assert ling.auth_source == "local" and ling.role == Role.LINGUIST, "linguist account mutated"
    assert ling.password_hash, "linguist password wiped"
    # and the local linguist still logs in with the LOCAL password
    security.clear_login_failures("ling@corp.test", "testclient")
    c4d = TestClient(app)
    r = _login(c4d, "ling@corp.test", "ling-local-pw-1234")
    assert r.status_code == 303, ("local linguist must still log in locally", r.status_code)
    assert security.get_by_email("ling@corp.test").auth_source == "local"
    print("  non-admin local password account (linguist) NOT taken over by LDAP; local works")

    # --- 5. ldap_enabled=0 -> zero LDAP calls ---
    settings.set_many({"ldap_enabled": "0"})
    assert not ldap_auth.enabled()
    before = CALLS["n"]
    security.clear_login_failures("alice@corp.test", "testclient")
    c5 = TestClient(app)
    r = _login(c5, "alice@corp.test", "alice-dir-pw")  # LDAP off: local-only, no acct match
    assert r.status_code == 401, r.status_code  # alice has no local password
    assert CALLS["n"] == before, ("LDAP was contacted while disabled", CALLS["n"], before)
    print("  ldap_enabled=0 makes ZERO LDAP calls (local-only auth)")

    # --- 6. LDAP server unreachable -> no 500, local auth still works ---
    settings.set_many({"ldap_enabled": "1"})
    CALLS["raise"] = True
    security.clear_login_failures("boss@corp.test", "testclient")
    c6 = TestClient(app)
    # a login that would need LDAP: the server raises, we degrade to local — and
    # the local admin's LOCAL password still authenticates. No 500.
    r = _login(c6, "boss@corp.test", "boss-local-pw-1234")
    assert r.status_code == 303, ("outage must degrade to local, not 500", r.status_code)
    CALLS["raise"] = False
    print("  LDAP outage degrades to local auth (no 500 on the login page)")

    # --- 6b. the outage log never carries the typed sign-in name (audit-typedname LOW-2) ---
    _outage_log_hides_typed_name()

    # --- LOW-1 (gate round 1): the LDAP-toggle refusal surfaces its ACTIONABLE message
    # on BOTH write surfaces (4xx with the pip-install hint), never a generic 500.
    # Simulate ldap3 absent by making require_installed() raise (ldap3 IS installed in
    # this suite's venv, so we can't uninstall it mid-run). security_auth_required is
    # still "1" here (set at the top), so the admin session is enforced.
    from app.services import apikeys

    _HINT = "pip install -r requirements-ldap.txt"
    orig_req = ldap_auth.require_installed

    def _raise_missing():
        raise RuntimeError("LDAP is enabled but the 'ldap3' package is not installed: " + _HINT)

    ldap_auth.require_installed = _raise_missing
    settings.set_many({"ldap_enabled": "0"})  # known-off baseline
    try:
        # WEB (surface 1): an admin saving the Security form with LDAP on is redirected to
        # /security?err=..., and the RENDERED page shows the actionable message (not a 500,
        # not a silent no-op); nothing is written.
        security.clear_login_failures("boss@corp.test", "testclient")
        cw = TestClient(app)
        assert _login(cw, "boss@corp.test", "boss-local-pw-1234").status_code == 303
        csrf_for(cw)
        r = cw.post("/security", data={"ldap_enabled": "1"}, follow_redirects=True)
        assert r.status_code == 200, ("web LDAP refusal must not 500", r.status_code)
        assert _HINT in r.text, "the actionable message must render on the Security page"
        assert settings.get("ldap_enabled") == "0", "web refusal must write nothing"
        print("  LOW-1 web: Security save (LDAP on, no ldap3) -> message RENDERED on page, no 500")

        # API setup: an admin-scope key + remote_admin_enabled for the two /api doors.
        _row, raw = apikeys.create_key(boss.id, "ldap-low1", scopes="admin")
        hdr = {"X-Loc-Token": raw}
        settings.set_many({"remote_admin_enabled": "1"})
        ca = TestClient(app)

        # API (surface 2): POST /api/admin/ldap — the endpoint named for this job.
        # #280: every LDAP key is security-sensitive, so the body confirms it.
        r = ca.post(
            "/api/admin/ldap",
            headers=hdr,
            json={"ldap_enabled": "1", "confirm": ["ldap_enabled"]},
        )
        assert r.status_code == 400, (
            "/api/admin/ldap must be 400, not 500",
            r.status_code,
            r.text[:200],
        )
        assert r.json().get("ok") is False and _HINT in r.json().get("error", ""), r.text
        assert settings.get("ldap_enabled") == "0", "/api/admin/ldap refusal must write nothing"
        print("  LOW-1 api: POST /api/admin/ldap (LDAP on, no ldap3) -> 400 with the message")

        # API (surface 3): POST /api/admin/config — the generic settings door.
        r = ca.post(
            "/api/admin/config",
            headers=hdr,
            json={"settings": {"ldap_enabled": "1"}, "confirm": ["ldap_enabled"]},
        )
        assert r.status_code == 400, ("/api/admin/config must be 400", r.status_code, r.text[:200])
        assert r.json().get("ok") is False and _HINT in r.json().get("error", ""), r.text
        assert settings.get("ldap_enabled") == "0", "/api/admin/config refusal must write nothing"
        print("  LOW-1 api: POST /api/admin/config (LDAP on, no ldap3) -> 400 with the message")
    finally:
        ldap_auth.require_installed = orig_req
        settings.set_many({"remote_admin_enabled": "0", "ldap_enabled": "0"})

    settings.set_many({"security_auth_required": "0", "ldap_enabled": "0"})
    ldap_auth.set_server_factory(None)
    _transport_tests()
    print("LDAP OK")


if __name__ == "__main__":
    main()
