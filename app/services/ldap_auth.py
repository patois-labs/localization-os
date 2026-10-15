"""LDAP / Active Directory authentication — real search-then-bind.

Wired into the /auth/login POST as a FIRST attempt for non-local accounts when
ldap_enabled=1; a failure (bad password, LDAP outage, no match) falls through to
LOCAL password auth, so the seeded admin always works and an LDAP outage never
500s the login page.

Design mirrors app/services/oidc.py:
  - the ldap3 library is imported LAZILY (guarded), so an install without it still
    boots and runs — LDAP is only a hard dependency when the toggle is on;
  - all directory access is testable offline: ldap3 ships a MOCK_SYNC strategy,
    and this module accepts an injectable Server/Connection factory (set via
    set_server_factory) so tests drive a mocked directory with zero real sockets;
  - a local password admin can NEVER be shadowed/taken over by an LDAP identity
    with the same email (identical rule to get_or_create_sso_user);
  - error text shown to the browser is generic; details go to the log.

Flow (search-then-bind):
  1. Bind to the directory as the configured service/bind DN (or anonymously when
     no bind DN is set), using the encrypted-at-rest bind password.
  2. Search base_dn with the configured user filter (e.g.
     (|(uid={username})(mail={username}))) to resolve the user's real DN + attrs.
  3. Re-bind as that DN with the password the user just submitted. Success proves
     the credential; failure means wrong password (fall through to local).
  4. Provision-or-update the local User (email/name from attributes, role =
     ldap_default_role) and return it.

Documented in: docs/architecture/sso-provisioning.md
"""

from __future__ import annotations

import logging
import ssl
import time
from contextlib import suppress
from datetime import datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

from sqlmodel import Session

from app.db import engine
from app.models import Role, User
from app.services import login_throttle, security, settings

_logger = logging.getLogger("loc_tms")

# Rate-limit the LDAP-outage log (finding F4). During a directory outage EVERY
# login attempt would otherwise emit a full traceback via _logger.exception,
# flooding the log. We keep an in-memory timestamp (module-level, like the login
# lockout state) and log the outage at most once per window, at WARNING, with a
# concise message instead of a per-attempt traceback.
_OUTAGE_LOG_WINDOW_S = 60.0
_last_outage_log = 0.0

# The most recent outage reason, kept so `admin ldap test-login` can TELL the operator why
# a bind failed. Without it the rate-limited log line is the only signal, and the #169
# hardening is exactly the kind of change that turns a working install into a silent
# lockout (an internal CA not in the system trust store, or a DC with no StartTLS on 389).
# Diagnostic only: never consulted by an auth decision, and it holds a reason string, never
# a credential.
_last_outage_reason = ""


def last_outage_reason() -> str:
    """The most recent LDAP outage reason, for operator diagnostics. May be stale."""
    return _last_outage_reason


def _log_outage(context: str) -> None:
    """Log an LDAP outage at most once per ``_OUTAGE_LOG_WINDOW_S`` (finding F4).

    The first occurrence in a window is informative (WARNING, includes the failing
    context); subsequent attempts within the window are suppressed so an outage
    does not spam the log with a traceback per login. No auth behavior changes.

    The typed sign-in name is deliberately NOT a parameter. An outage is a system event,
    and a person who types their password into the username box would otherwise have it
    written to the server log (audit-typedname LOW-2). ``context`` must be module-authored
    text only: never an exception message that could quote the filter, a DN or the name."""
    global _last_outage_log, _last_outage_reason
    _last_outage_reason = context
    now = time.monotonic()
    if now - _last_outage_log >= _OUTAGE_LOG_WINDOW_S:
        _last_outage_log = now
        _logger.warning(
            "LDAP %s; falling back to local auth (further LDAP errors suppressed for %ds).",
            context,
            int(_OUTAGE_LOG_WINDOW_S),
        )


# Injectable factory for the ldap3 Server/Connection pair. Production leaves it
# None (real sockets); tests set it to a callable returning a MOCK_SYNC-backed
# Connection so the whole flow runs offline. Signature:
#     factory(uri, bind_dn, bind_password) -> ldap3.Connection (already bound)
_server_factory = None


def set_server_factory(factory) -> None:
    """Inject a Connection factory for tests (ldap3 MOCK_SYNC). None = real."""
    global _server_factory
    _server_factory = factory


def enabled() -> bool:
    """LDAP is active only when the toggle is on AND the minimum config is set
    (a host and a base DN — without them there is nothing to search)."""
    return (
        settings.get("ldap_enabled") == "1"
        and bool((settings.get("ldap_host") or "").strip())
        and bool((settings.get("ldap_base_dn") or "").strip())
    )


def bind_password() -> str:
    """The service/bind DN password, decrypted from its encrypted-at-rest setting.
    Stored Fernet-encrypted under ldap_bind_password_enc (see settings.py /
    admin.security_save), mirroring how connector credentials are protected."""
    return settings.get_secret("ldap_bind_password_enc")


DEFAULT_USER_FILTER = "(|(uid={username})(mail={username}))"


def user_filter() -> str:
    """The configured search filter with a {username} placeholder, or the default.
    {username} is escaped by ldap3 at search time (assembled via a safe builder),
    so a submitted value can never inject filter syntax."""
    f = (settings.get("ldap_user_filter") or "").strip()
    return f or DEFAULT_USER_FILTER


def _ldap3():
    """Import ldap3 lazily so an install without it still boots. Raises a clear
    error only when LDAP is actually exercised without the library present. ldap3 is
    an OPT-IN (LGPL) extra kept out of the default zero-copyleft install — the message
    names the extras file so the fix is obvious."""
    try:
        import ldap3

        return ldap3
    except Exception as e:  # pragma: no cover - only when the dep is missing
        raise RuntimeError(
            "LDAP is enabled but the 'ldap3' package is not installed: "
            "pip install -r requirements-ldap.txt "
            "(container image: rebuild it with --build-arg WITH_LDAP=1)"
        ) from e


def require_installed() -> None:
    """Raise the clear install hint if ldap3 is absent. Called by the settings write
    path so turning the LDAP toggle ON is REFUSED (rather than saving a switch that
    would crash every login) when the opt-in extra is not installed."""
    _ldap3()


def _escape_filter(value: str) -> str:
    """RFC 4515 escape for a value interpolated into a search filter. ldap3
    provides its own escaper; use it so behavior matches the library exactly. Routed
    through _ldap3() first so a missing (opt-in) ldap3 surfaces the clear extras-file
    message, not an opaque ImportError."""
    _ldap3()
    from ldap3.utils.conv import escape_filter_chars

    return escape_filter_chars(value or "")


class LDAPTransportError(ConnectionError):
    """A failed secure transport, distinct from rejected credentials.

    Its message is logged verbatim by the outage path, so every raise site passes a fixed
    string, an exception CLASS name, or a refusal of the ADMIN-configured host (built only
    from settings, never from login input): never a DN, a filter or a typed value."""


def _build_server(uri: str):
    """Construct a validating server without opening a socket."""
    ldap3 = _ldap3()
    uri = settings.normalize_ldap_host(uri)
    if not uri:
        raise ValueError("LDAP host is empty")
    host = urlsplit(uri).hostname
    try:
        ip_address(host)
    except ValueError:
        sni = host
    else:
        sni = None  # SNI carries DNS names only, never IPv4 or IPv6 literals.
    # With version=None ldap3 uses create_default_context(SERVER_AUTH), loading
    # system roots when cafile is None. CERT_REQUIRED also enables its hostname check.
    tls = ldap3.Tls(
        validate=ssl.CERT_REQUIRED,
        ca_certs_file=(settings.get("ldap_ca_cert") or "").strip() or None,
        sni=sni,
    )
    # allowed_referral_hosts=[] closes the hole the rest of this hardening would otherwise
    # leave wide open. ldap3's default is [("*", True)] -- ANY host, WITH our credentials
    # (core/server.py:158). On a referral, create_referral_connection builds a BRAND NEW
    # connection to a host named by the directory: for an ldap:// referral it sets
    # use_ssl=False and tls=None, copies our user and password in, opens, and binds. Its
    # StartTLS rescue is gated on connection.tls_started, which is False for an ldaps://
    # primary, and even when it does fire it manufactures a default Tls() -- which is
    # validate=CERT_NONE, the exact defect #169 is about. AD emits referrals routinely, so
    # this needs no MITM of the primary channel at all.
    return ldap3.Server(
        uri, get_info=ldap3.NONE, connect_timeout=10, tls=tls, allowed_referral_hosts=[]
    )


def _connect(uri: str, bind_dn: str, bind_pw: str):
    """Open a directory connection bound as the service account. Uses the injected
    factory when present (tests: MOCK_SYNC), else a real ldap3 Server/Connection.
    The caller is responsible for closing (used as a context manager)."""
    if _server_factory is not None:
        return _server_factory(uri, bind_dn, bind_pw)
    ldap3 = _ldap3()
    conn = None
    # Outside the try below, so a REFUSED HOST keeps its own curated reason instead of
    # being flattened to "TLS setup failed (ValueError)". After the upgrade, seven
    # distinct stored-host breakages all reported that one opaque string, and it is
    # actively misleading because no socket was opened at all. normalize_ldap_host's
    # messages name the offending form and never contain a credential.
    try:
        server = _build_server(uri)
    except ValueError as exc:
        raise LDAPTransportError(f"invalid ldap_host setting: {exc}") from exc
    try:
        # Do not even give the connection credentials until TLS has succeeded.
        # auto_referrals=False belt-and-braces with allowed_referral_hosts=[] above:
        # ldap3 defaults it to True (core/connection.py:194), and a referral chase is a
        # second, unhardened socket carrying this bind password. We do not follow them.
        conn = ldap3.Connection(server, auto_bind=False, receive_timeout=15, auto_referrals=False)
        conn.open()
        if conn.closed:
            raise LDAPTransportError("TLS socket did not open; no bind attempted")
        if not server.ssl and not conn.start_tls():
            raise LDAPTransportError("StartTLS refused; no bind attempted")
    except Exception as exc:
        if conn is not None:
            with suppress(Exception):
                conn.unbind()
        if isinstance(exc, LDAPTransportError):
            raise
        # Exception types identify certificate/configuration failures without logging
        # arbitrary library messages that could contain credentials.
        raise LDAPTransportError(f"TLS setup failed ({type(exc).__name__})") from exc
    try:
        # Belt: assert the channel is still protected immediately before the credential
        # goes out. Not reachable today (the SYNC strategy performs no I/O between
        # start_tls and rebind), but Connection.rebind re-opens a CLOSED socket in
        # PLAINTEXT for an ldap:// server (connection.py:589-590) and BaseStrategy.close()
        # clears tls_started, so the one path that would silently downgrade is closed here
        # rather than left to a future refactor to notice.
        if not server.ssl and not getattr(conn, "tls_started", False):
            raise LDAPTransportError("StartTLS not active at bind time; no bind attempted")
        if not conn.rebind(user=bind_dn or None, password=bind_pw or None):
            raise ldap3.core.exceptions.LDAPBindError("LDAP bind rejected")
        return conn
    except Exception:
        with suppress(Exception):
            conn.unbind()
        raise


def _search_then_bind(username: str, password: str):
    """The core directory exchange. Returns (dn, email, name) on a proven bind, or
    None on ordinary failure (no match, wrong password, outage). AccountLocked is
    propagated to the password form as a uniform 429 before binding. LDAP outages
    degrade to local auth, never a 500 on the login page.

    A blank password is rejected up front: many directories permit an *unauthenticated
    bind* (a bind with a DN but empty password succeeds without checking the
    credential), which would be a silent auth bypass."""
    if not username or not password:
        return None

    try:
        uri = (settings.get("ldap_host") or "").strip()
        base_dn = (settings.get("ldap_base_dn") or "").strip()
        bind_dn = (settings.get("ldap_bind_dn") or "").strip()
        bind_pw = bind_password()
        flt = user_filter().replace("{username}", _escape_filter(username))
        # try/finally, not just `with`: Connection.__exit__ unbinds only when the
        # connection ENTERED unbound (connection.py:545-552), and _connect always returns
        # a bound one, so the context manager alone leaks an authenticated socket per
        # login. Pre-existing (auto_bind=True left the same state) but this train owns
        # the function, so close it here rather than inherit the leak.
        conn = _connect(uri, bind_dn, bind_pw)
        try:
            conn.search(base_dn, flt, attributes=["mail", "cn", "displayName", "uid"])
            entries = list(getattr(conn, "entries", []) or [])
            if len(entries) != 1:
                # 0 = no such user; >1 = ambiguous filter — never guess an identity
                if len(entries) > 1:
                    # No typed name here either: it is login input, not a proven identity.
                    _logger.warning(
                        "LDAP filter matched %d entries; refusing ambiguous login",
                        len(entries),
                    )
                return None
            entry = entries[0]
            user_dn = str(getattr(entry, "entry_dn", "") or "")
            if not user_dn:
                return None
            email = _entry_get(entry, "mail") or (username if "@" in username else "")
            name = _entry_get(entry, "displayName") or _entry_get(entry, "cn")
        finally:
            with suppress(Exception):
                conn.unbind()

        # Re-bind as the found DN with the SUBMITTED password (proves the credential).
        login_throttle.reserve_directory_account(email)
        proof = None
        try:
            proof = _connect(uri, user_dn, password)
            # _connect raised already on a bad password; reaching here is success
        except LDAPTransportError:
            raise
        except Exception:
            # wrong password (bind rejected) — a normal auth failure, not an outage
            return None
        finally:
            if proof is not None:
                with suppress(Exception):
                    proof.unbind()

        return user_dn, security.normalize_email(email), name
    except login_throttle.AccountLocked:
        raise
    except Exception as exc:
        # discovery/search/service-bind failure = LDAP unreachable/misconfigured.
        # Degrade to local auth (the caller falls through). Rate-limited so an
        # outage does not emit a traceback per login attempt (finding F4).
        # Only the class name for anything but our own transport error: ldap3 messages can
        # quote the search filter (which embeds the typed name), a DN or a server
        # diagnostic, so str(exc) is never logged for them.
        cause = str(exc) if isinstance(exc, LDAPTransportError) else type(exc).__name__
        _log_outage(f"search-then-bind failed: {cause}")
        return None


def _entry_get(entry, name: str) -> str:
    """Read a single attribute value from an ldap3 Entry (mock or real)."""
    try:
        val = entry[name].value if name in entry else None
    except Exception:
        val = None
    if isinstance(val, (list, tuple)):
        val = val[0] if val else None
    return str(val) if val else ""


def _default_role() -> str:
    role = settings.get("ldap_default_role") or Role.TRANSLATOR
    return role if role in Role.ALL else Role.TRANSLATOR


def get_or_create_ldap_user(email: str, name: str):
    """Map a proven LDAP identity to a local account. Returns (user, None) or
    (None, error). Rules mirror get_or_create_sso_user exactly:

      - an existing LOCAL account that has a PASSWORD (any role, not just admin)
        is NEVER taken over by LDAP — that would let anyone who controls the
        directory shadow a real local password account (finding F5, widened from
        admin-only). The refusal returns an error so the caller FALLS THROUGH to
        local password auth (the local password still works); it is not a hard
        deny of the person, just of the LDAP-over-local shadowing;
      - a disabled account is refused;
      - other existing users (SSO/LDAP-provisioned, no local password) are
        re-marked auth_source='ldap', name refreshed, and reused;
      - unknown emails get a fresh account with the ldap_default_role setting."""
    email = security.normalize_email(email)
    if not email or "@" not in email:
        return None, "The directory did not supply an email address."

    existing = security.get_by_email(email)
    if existing:
        if existing.password_hash and security.auth_source_of(existing) == "local":
            return None, (
                "This email belongs to a local password account - sign in with your local password."
            )
        # Train 0b: a trust-host-auth principal is a DEDICATED service identity, not a
        # login. Refuse LDAP adoption exactly like a local-password collision, so a
        # directory login as that email can never take over the persistent trust row.
        if security.auth_source_of(existing) == "trust":
            return None, (
                "This email is a trust-host-auth service identity, not a "
                "login account - it cannot be used to sign in."
            )
        if not existing.active:
            return None, "This account has been disabled."
        with Session(engine) as s:
            row = s.get(User, existing.id)
            row.auth_source = "ldap"
            if name and name != row.name:
                row.name = name  # keep the display name fresh
            row.last_login = datetime.utcnow()
            s.add(row)
            s.commit()
            s.refresh(row)
            return row, None

    # GATE R2 MED-1: a brand-new account. Under the sacred multi-org gate there is no org
    # to assign, so refuse JIT provisioning rather than mint a NULL-org user, matching the
    # SAML and SSO siblings. An org-less account is invisible in the (correctly scoped)
    # /users listing, refused by every management route, and cannot authenticate, so it is
    # worse than a clean refusal at the door. #181: create_user owns that refusal now
    # (refuse_orgless_account); this door maps it to its own NOT_PROVISIONED wording, which
    # the login route recognises (is_not_provisioned).
    # BUG-A3-1: create_user now format-validates the email — a malformed LDAP mail
    # attribute is a clean login refusal (this function's (user, error) contract),
    # never an uncaught 500 in the login flow (mirrors the SSO sibling).
    try:
        user = security.create_user(email, name or "", _default_role(), "", auth_source="ldap")
    except security.OrglessAccountError:
        return None, NOT_PROVISIONED
    except ValueError as exc:
        return None, str(exc)
    return user, None


NOT_PROVISIONED = (
    "This account is not provisioned. Under multi-org, accounts are provisioned "
    "by your administrator (e.g. via SCIM) before directory sign-in."
)


def is_not_provisioned(error) -> bool:
    """GATE R3 MED-1. The login route deliberately SWALLOWS a hard LDAP refusal and falls
    through to local password auth, which is right for the two pre-existing refusals (an
    LDAP identity shadowing a local password admin, a disabled account) because in both
    cases the person still has a local password to fall through TO.

    An unprovisioned directory user has none. The fall-through therefore reached
    `security.authenticate`, failed, and counted as a bad password: measured at five
    attempts with the CORRECT directory password, the fifth returned 429 with a 899-second
    lockout, and the operator-facing explanation was never shown once.

    So the route needs to tell these two refusals apart. This predicate is the seam, and it
    compares against the module constant rather than a literal so the two cannot drift."""
    return bool(error) and error == NOT_PROVISIONED


def try_login(username: str, password: str):
    """Attempt LDAP authentication for a login. Returns (user, None) on success,
    (None, None) to signal 'fall through to local auth' (no match / wrong password
    / outage), or (None, error) for a hard refusal that should be shown (a local
    admin takeover attempt / a disabled account).

    AccountLocked propagates a pre-bind refusal. Unexpected directory failures
    degrade to (None, None) so the login route continues to local password auth."""
    try:
        if not enabled():
            return None, None
        proof = _search_then_bind((username or "").strip(), password or "")
        if not proof:
            return None, None  # unknown / wrong pw / outage -> local
        _dn, email, name = proof
        return get_or_create_ldap_user(email, name)
    except login_throttle.AccountLocked:
        raise
    except Exception:
        # Unexpected failure -> degrade to local auth. Rate-limited (finding F4).
        _log_outage("login raised unexpectedly")
        return None, None
