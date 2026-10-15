"""Authentication + RBAC.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _ph, SYSTEM, _engine, request_path, host_name,
constant_time_eq, ingress_entries, _forwarded_address, MIN_PASSWORD_LEN, password_issue,
password_change_required, password_rules,
LOCKOUT_THRESHOLD, LOCKOUT_WINDOW_S, LOCKOUT_DURATION_S, _failures, _locked_until, _lockout_key,
login_locked_seconds, reserve_login_attempt, release_login_attempt,
settle_login_failure, clear_login_failures, session_idle_expired,
session_limits, session_clock, enforce_session, touch_session, hash_password, verify_password,
dummy_verify, user_count, has_users, list_users, count_users, normalize_email,
get_by_email, is_placeholder_email, PLACEHOLDER_EMAIL_DOMAINS, PLACEHOLDER_EMAIL_MESSAGE,
RESERVED_ACTORS, UNAUTHENTICATED_ACTOR, is_reserved_actor, EMAIL_MAX_LEN,
validate_email, _SQLITE_INT64_MAX,
_coerce_customer_id, _validate_customer_binding, create_user, authenticate, login, logout,
bump_session_epoch, _rank,
is_admin, can_write, can_manage, can_tune_ai, can_configure_customer, can_review,
enable_login_wall,
is_last_active_admin, is_last_admin_of_own_org, OrglessAccountError,
refuse_orgless_account, set_role, set_customer, set_department, set_active, admin_set_password,
update_name, update_locale, UI_THEMES, normalize_theme, update_theme, change_password,
_INVITE_ORG_UNSET, _invite_scope,
create_invite, revoke_invite, invite_metadata, _invites, expired_invites, pending_invites,
get_invite, signs_in_locally, password_change_refusal, _switch_to_local, _audit_switch,
auth_source_of, TRUST_IDENTITY_REFUSAL, invite_refusal, signs_in_through_idp,
_invite_refusal_detail from
app.services.security_client_ip_one_authority_.

Argon2id password hashing, signed-cookie sessions, and role-based access. All
enforcement is gated by the `security_auth_required` setting: when OFF (default)
the app behaves as a single trusted operator (a synthetic SYSTEM admin); when ON,
login is required and roles are enforced.

Roles (ascending privilege): viewer < translator < reviewer < manager < admin.

Documented in: docs/architecture/identity-and-access.md
"""

import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta
from functools import lru_cache
from types import SimpleNamespace

from argon2 import PasswordHasher
from sqlmodel import Session, select

from app import config
from app import db as _db_relief
from app.db import engine
from app.models import Invite, Role, User
from app.services import audit, settings
from app.services.security_client_ip_one_authority_ import (
    _INVITE_ORG_UNSET,
    _SQLITE_INT64_MAX,
    EMAIL_MAX_LEN,
    LOCKOUT_DURATION_S,
    LOCKOUT_THRESHOLD,
    LOCKOUT_WINDOW_S,
    LOOKALIKE_EMAIL_MESSAGE,
    MIN_PASSWORD_LEN,
    PLACEHOLDER_EMAIL_DOMAINS,
    PLACEHOLDER_EMAIL_MESSAGE,
    RESERVED_ACTORS,
    SCIM_SYNC_STOPS_WARNING,
    SYSTEM,
    TRUST_IDENTITY_REFUSAL,
    UI_THEMES,
    UNAUTHENTICATED_ACTOR,
    FirstAccountExists,
    OrglessAccountError,
    _audit_switch,
    _coerce_customer_id,
    _engine,
    _failures,
    _forwarded_address,
    _invite_refusal_detail,
    _invite_scope,
    _invites,
    _locked_until,
    _lockout_key,
    _ph,
    _rank,
    _switch_to_local,
    _validate_customer_binding,
    admin_set_password,
    audit_scim_tokens_ended,
    auth_source_of,
    authenticate,
    bump_session_epoch,
    can_configure_customer,
    can_manage,
    can_review,
    can_tune_ai,
    can_write,
    change_password,
    check_current_password,
    clear_login_failures,
    constant_time_eq,
    count_users,
    create_invite,
    create_user,
    dummy_verify,
    enable_login_wall,
    end_credentials,
    enforce_session,
    expired_invites,
    get_by_email,
    get_invite,
    has_hidden_email_char,
    has_users,
    hash_password,
    host_name,
    ingress_entries,
    invite_metadata,
    invite_refusal,
    is_admin,
    is_last_active_admin,
    is_last_admin_of_own_org,
    is_placeholder_email,
    is_reserved_actor,
    is_scim_key,
    list_users,
    login,
    login_locked_seconds,
    login_trusted_networks,
    logout,
    lookalike_account_id,
    normalize_email,
    normalize_theme,
    password_change_refusal,
    password_change_required,
    password_issue,
    password_rules,
    pending_invites,
    refuse_orgless_account,
    release_login_attempt,
    request_path,
    reserve_login_attempt,
    revoke_invite,
    scim_token_prefixes,
    session_clock,
    session_idle_expired,
    session_limits,
    set_active,
    set_customer,
    set_department,
    set_role,
    settle_login_failure,
    signs_in_locally,
    signs_in_through_idp,
    touch_session,
    update_locale,
    update_name,
    update_theme,
    user_count,
    validate_email,
    verify_password,
)

__all__ = [
    "EMAIL_MAX_LEN",
    "LOCKOUT_DURATION_S",
    "LOCKOUT_THRESHOLD",
    "LOCKOUT_WINDOW_S",
    "LOOKALIKE_EMAIL_MESSAGE",
    "MIN_PASSWORD_LEN",
    "PLACEHOLDER_EMAIL_DOMAINS",
    "PLACEHOLDER_EMAIL_MESSAGE",
    "RESERVED_ACTORS",
    "SCIM_SYNC_STOPS_WARNING",
    "SYSTEM",
    "TRUST_IDENTITY_REFUSAL",
    "UI_THEMES",
    "UNAUTHENTICATED_ACTOR",
    "_INVITE_ORG_UNSET",
    "_SQLITE_INT64_MAX",
    "FirstAccountExists",
    "OrglessAccountError",
    "PasswordHasher",
    "SimpleNamespace",
    "_audit_switch",
    "_coerce_customer_id",
    "_db_relief",
    "_engine",
    "_failures",
    "_forwarded_address",
    "_invite_refusal_detail",
    "_invite_scope",
    "_invites",
    "_locked_until",
    "_lockout_key",
    "_ph",
    "_rank",
    "_switch_to_local",
    "_validate_customer_binding",
    "admin_set_password",
    "audit",
    "audit_scim_tokens_ended",
    "auth_source_of",
    "authenticate",
    "bump_session_epoch",
    "can_configure_customer",
    "can_manage",
    "can_review",
    "can_tune_ai",
    "can_write",
    "change_password",
    "check_current_password",
    "clear_login_failures",
    "constant_time_eq",
    "count_users",
    "create_invite",
    "create_user",
    "dummy_verify",
    "enable_login_wall",
    "end_credentials",
    "enforce_session",
    "engine",
    "expired_invites",
    "get_by_email",
    "get_invite",
    "has_users",
    "hash_password",
    "hashlib",
    "host_name",
    "ingress_entries",
    "invite_metadata",
    "invite_refusal",
    "is_admin",
    "is_last_active_admin",
    "is_last_admin_of_own_org",
    "is_placeholder_email",
    "is_reserved_actor",
    "is_scim_key",
    "list_users",
    "login",
    "login_locked_seconds",
    "login_trusted_networks",
    "logout",
    "lookalike_account_id",
    "normalize_email",
    "normalize_theme",
    "password_change_refusal",
    "password_change_required",
    "password_issue",
    "password_rules",
    "pending_invites",
    "re",
    "refuse_orgless_account",
    "release_login_attempt",
    "request_path",
    "reserve_login_attempt",
    "revoke_invite",
    "scim_token_prefixes",
    "secrets",
    "session_clock",
    "session_idle_expired",
    "session_limits",
    "set_active",
    "set_customer",
    "set_department",
    "set_role",
    "settle_login_failure",
    "signs_in_locally",
    "signs_in_through_idp",
    "time",
    "timedelta",
    "touch_session",
    "update_locale",
    "update_name",
    "update_theme",
    "user_count",
    "validate_email",
    "verify_password",
]

_logger = logging.getLogger(__name__)


def proxy_networks(value):
    """Reject broad trust: default routes recreate wildcard, caller-controlled identity."""
    networks = [ipaddress.ip_network(p, strict=False) for p in ingress_entries(value)]
    if any(n.prefixlen < (8 if n.version == 4 else 32) for n in networks):
        raise ValueError(
            "trusted proxies require IPv4 /8 or narrower and IPv6 /32 or narrower; "
            "broad networks permit forged client identity"
        )
    return networks


def client_ip_detail(request):
    """Bounded JSON-escaped evidence; never attribute an invalid chain to the proxy.

    An over-long chain keeps its RIGHT end (#289): that is where the trusted proxies
    and the real client sit, so the left is what gets cut. Still bounded to 128
    characters, JSON-escaped, and marked truncated."""
    if client_ip(request) == "" and "x-forwarded-for" in request.headers:
        raw = ", ".join(request.headers.getlist("x-forwarded-for"))
        truncated = len(raw) > 128
        kept = raw[-128:] if truncated else raw
        return (
            "malformed-x-forwarded-for="
            + json.dumps(kept, ensure_ascii=True)
            + (" (truncated)" if truncated else "")
        )
    return ""


# --- the unauthenticated audit actor ---
# The audit log never says "anonymous": every row names who or what acted. A request that
# resolved no identity (no session, no valid API key, no trust principal) is written as
# "unauthenticated <client ip>", with the refusal status, the user agent and, when a wrong
# key or bearer token was presented, only its first few characters in the detail column.
# ONE helper pair so the middleware, the feature gate and the egress attribution agree.
_UA_MAX = 120
#: Typed sign-in names are recorded in detail, never as the actor, and bounded like this.
_TYPED_NAME_MAX = 80
#: apikeys.create_key stores full_key[:12] as the display prefix; never show more than that,
#: and never more than half of what was sent, so a short token is never shown whole.
_WRONG_KEY_START_MAX = 12
#: A wrong value in the shared connector token's own format shows less: its random part is
#: shorter than a per-user key's, and a near-miss may be an old rotated token.
_WRONG_SHARED_START_MAX = 8


def unauthenticated_actor(request) -> str:
    """The actor for a request with no identity: `unauthenticated <ip>` from the one
    client-IP authority, or `unauthenticated (unknown address)` when there is none."""
    try:
        ip = client_ip(request)
    except Exception:
        ip = ""
    return f"{UNAUTHENTICATED_ACTOR} {ip}" if ip else f"{UNAUTHENTICATED_ACTOR} (unknown address)"


def _printable(value: str) -> str:
    # Header values arrive latin-1 decoded; drop every C0/C1/DEL control (and any other
    # non-printable) so nothing can split or disguise a line in the audit view or a SIEM feed.
    return "".join(c for c in value if c.isprintable())


def quoted_field(value, cap: int) -> str:
    """Caller-supplied text made safe for one detail field: NFKC-normalized, every control,
    format (zero-width) and other non-printable character removed, bounded to `cap` characters
    (then "..."), JSON-quoted with non-ASCII escaped, and `;` written as its JSON escape so a naive
    "; " field splitter can never see a forged field inside it (gate round 1, LOW-3)."""
    text = _printable(unicodedata.normalize("NFKC", str(value or "")))
    if len(text) > cap:
        text = text[:cap] + "..."
    return json.dumps(text, ensure_ascii=True).replace(";", "\\u003b")


def _typed_name_is_email(text: str) -> bool:
    """True when `text` is an email address by the app's own seam (`validate_email`) AND
    has the "@" plus dotted domain the GDPR masker recognises as an address. The seam alone
    tolerates a bare word (the seeded `admin` login), which is exactly the shape of a
    password typed into the wrong box, so a bare word is never "an email" here."""
    if "@" not in text:
        return False
    try:
        validate_email(text)
    except ValueError:
        return False
    from app.services.gdpr_common import _EMAIL_RE

    return _EMAIL_RE.fullmatch(text) is not None


def _typed_name_mac(text: str) -> str:
    """First 8 hex of HMAC-SHA256 over the normalized typed text, keyed off the session
    secret under its own label (never an unkeyed hash: a dictionary password would fall
    out of one). Rotating the secret only stops ids correlating across the rotation;
    nothing verifies them, unlike the audit chain MAC."""
    key = hashlib.sha256(("loc-typed-name:" + config.SESSION_SECRET).encode("utf-8")).digest()
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:8]


def typed_name_field(value) -> str:
    """A kept typed sign-in name as one bounded, quoted field (see quoted_field)."""
    return quoted_field(value, _TYPED_NAME_MAX)


def _known_email_domain(domain: str) -> bool:
    """True when some account's address is at `domain` (lowercase). Organizations carry no
    domain of their own, so the account table is the only record of which domains this
    install serves. One LIMIT 1 query on the email column (a suffix match, so a scan of
    the user table rather than an index seek; the table is small and this runs only on a
    failed sign-in). Any error answers False: the caller then masks, the safe direction."""
    try:
        with Session(_engine()) as s:
            hit = s.exec(
                select(User.id).where(User.email.endswith("@" + domain, autoescape=True)).limit(1)
            ).first()
    except Exception:
        return False
    return hit is not None


def typed_name_detail(value, *, directory_verified: bool = False) -> str:
    """Detail evidence for a sign-in name that matched no account (MEDIUM-1): the typed
    text is evidence, never an identity, so it lives in detail and never in the actor.

    Kept as `typed-name="..."` (see quoted_field) only for a directory username the LDAP
    bind just proved (`directory_verified`), or an email address whose domain belongs to
    an existing account. Anything else may be a password typed into the wrong box (gate
    audit-actor round 2, LOW-6; gate audit-typedname round 1, LOW-1: `Welcome@2024.Fall`
    is a valid address), so it is recorded as `typed-name-masked id=<8 hex>`: enough to
    correlate repeated attempts, nothing that reveals the value (not even its length).
    An email-shaped name at an unknown domain keeps nothing else either: its domain part
    may be half of a password (`2024.Fall`). Normalization and zero-width stripping come
    first, so lookalike variants of one name share an id."""
    text = _printable(unicodedata.normalize("NFKC", str(value or "")))
    if directory_verified:
        return "typed-name=" + typed_name_field(text)
    if _typed_name_is_email(text) and _known_email_domain(text.rpartition("@")[2].lower()):
        return "typed-name=" + typed_name_field(text)
    return f"typed-name-masked id={_typed_name_mac(text)}"


def _presented_token(request) -> str:
    raw = (request.headers.get("x-loc-token") or "").strip()
    if not raw:
        scheme, _, cred = (request.headers.get("authorization") or "").strip().partition(" ")
        if scheme.lower() == "bearer":
            raw = cred.strip()
    return raw


def _genuine_display_prefix(raw: str) -> str:
    """The stored display prefix when `raw` is a REAL credential that was refused anyway
    (its org is suspended, or the shared token is unbound under multi-org), else "". Pure
    reads, no last-used stamp: this only labels the audit row."""
    try:
        from app.services import apikeys

        prefix = apikeys.display_prefix_for(raw)
        if prefix:
            return prefix
        shared = settings.read_secret_setting("connector_api_token") or ""
        if shared and constant_time_eq(raw, shared):
            return shared[:4]  # the scheme only ("lsk_"): no character of the secret part
    except Exception:
        return ""
    return ""


def presented_token_prefix(request) -> str:
    """The first few characters of a presented X-Loc-Token or Bearer token, or "" when none
    was sent. At most _WRONG_KEY_START_MAX (8 for a value in the shared token's "lsk_"
    format) and at most half the token (so a short guess is never shown in full), limited to
    key-alphabet characters, always followed by "..."."""
    raw = _presented_token(request)
    if not raw:
        return ""
    cap = _WRONG_SHARED_START_MAX if raw.startswith("lsk_") else _WRONG_KEY_START_MAX
    shown = raw[: min(cap, len(raw) // 2)]
    return "".join(c if c.isascii() and (c.isalnum() or c in "_-.") else "?" for c in shown) + "..."


def unauthenticated_detail(request, status: int | None = None) -> str:
    """The audit detail for an unauthenticated request: "refused <status>" (or "status <n>"
    for a non-error answer), the user agent (see quoted_field), and, when a credential was
    sent, either `suspended-key=<stored display prefix>` for a genuine key that was refused
    (gate round 1, LOW-2: never more of the secret than the product already displays) or
    `wrong-key-start=<first chars>...` for a value that is no key at all."""
    parts = []
    if status is not None:
        parts.append(f"refused {status}" if status >= 400 else f"status {status}")
    ua = request.headers.get("user-agent") or ""
    parts.append("user-agent=" + (quoted_field(ua, _UA_MAX) if _printable(ua) else "(none)"))
    raw = _presented_token(request)
    if raw:
        genuine = _genuine_display_prefix(raw)
        if genuine:
            parts.append(f"suspended-key={genuine}")
        else:
            parts.append(f"wrong-key-start={presented_token_prefix(request)}")
    return "; ".join(parts)


FLY_PROXY_FIX = (
    "Set LOC_TMS_TRUSTED_PROXY_IPS to the measured addresses of your reverse proxy only."
)
_fly_peer_logged = False


@lru_cache(maxsize=16)
def _fly_proxy_trust_status(fly_app, value):
    if not fly_app:
        return ""
    try:
        networks = [ipaddress.ip_network(p, strict=False) for p in ingress_entries(value)]
        excluded = (
            "127.0.0.0/8",
            "::1/128",
            "100.64.0.0/10",
            "fd7a:115c:a1e0::/48",
            "fdaa::/16",
            "::ffff:127.0.0.0/104",
            "::ffff:100.64.0.0/106",
        )
        if any(net.overlaps(ipaddress.ip_network(bad)) for net in networks for bad in excluded):
            return "fly proxy trust unsafe"
        if not proxy_networks(value):
            return "fly proxy trust unset"
    except ValueError:
        return "fly proxy trust unset"
    return ""


def fly_proxy_trust_status():
    # Config is the live effective view; health must never read the settings store.
    return _fly_proxy_trust_status(config.fly_app_name, config.TRUSTED_PROXY_IPS)


def check_fly_proxy_at_boot():
    status = fly_proxy_trust_status()
    if status:
        logging.error(
            "%s: client identity and forwarded HTTPS are unreliable. %s", status, FLY_PROXY_FIX
        )


def check_login_trusted_networks_at_boot() -> str:
    """#411: a stored `login_trusted_networks` the write door would refuse (malformed, or
    broader than IPv4 /8 or IPv6 /32) trusts no source at all. Say so at startup, so the
    operator learns why a shared NAT is still capped. Returns the message (empty when fine)."""
    raw = settings.get("login_trusted_networks") or ""
    if not raw.strip():
        return ""
    try:
        login_trusted_networks(raw)
    except ValueError as exc:
        message = (
            "login_trusted_networks is invalid and is IGNORED (no sign-in source is exempt "
            f"from the per-source budget): {exc}. Fix it with `config set login_trusted_networks`."
        )
        logging.error(message)
        return message
    return ""


def client_ip(request) -> str:
    """Trust forwarding only from configured socket peers, including on Fly.

    Off Fly the socket peer must be trusted first. Walk from right to left, one hop
    at a time, skipping trusted proxies; the first untrusted, well-formed hop is the
    client. An absent chain returns the peer; a present empty chain, or a malformed
    hop in a position only a trusted proxy writes (at or right of the client), has no
    identity. Tokens left of the client are client-controlled and never parsed, so a
    malformed one there cannot blank the real address (#289).
    """
    global _fly_peer_logged
    peer = request.client.host if request.client else ""
    try:
        networks = proxy_networks(settings.get("trusted_proxy_ips"))

        def trusted(address):
            ip = ipaddress.ip_address(address)
            return any(ip in net for net in networks)

        if not trusted(peer):
            if config.fly_app_name and "fly-client-ip" in request.headers and not _fly_peer_logged:
                _fly_peer_logged = True
                logging.error(
                    "Fly-Client-IP header ignored: the edge peer %s is not a trusted proxy. %s",
                    peer,
                    FLY_PROXY_FIX,
                )
            return peer
    except ValueError:
        return peer
    if config.fly_app_name:
        # FLY_APP_NAME selects the edge header, never the socket trust boundary.
        # Loopback tailnet forwarders and 6PN neighbours must not choose audit IPs.
        lines = request.headers.getlist("fly-client-ip")
        try:
            return str(ipaddress.ip_address(lines[0].strip())) if len(lines) == 1 else peer
        except ValueError:
            return peer
    lines = request.headers.getlist("x-forwarded-for")
    if not lines:
        return peer
    raw = ", ".join(lines)
    address = ""
    for token in reversed(raw.split(",")):
        try:
            address = _forwarded_address(token)
        except ValueError:
            return ""
        if not trusted(address):
            return address
    return address


def auth_required() -> bool:
    return not config.loopback_bind() or settings.get("security_auth_required") == "1"


def _mfa_takeover_refusal(existing, via: str, ip: str = "") -> str | None:
    """TOTP train, gate round 1 LOW-1: an account that has two-step sign-in ON here is never
    adopted by an OIDC/SAML sign-in (which would re-mark it as an SSO account and so skip
    the code, and drop it out of mfa_required). Same shape as the local-password refusal:
    the person signs in with their password and code instead. Audited, never silent."""
    from app.services import mfa

    if not mfa.is_enrolled(getattr(existing, "id", 0)):
        return None
    if not mfa.applies_to(existing):
        # #423 gate: a factor on an account that has no local sign-in (a SCIM-linked row
        # with no password, set up before #423 closed that door) guards nothing here, so it
        # must not lock the owner out of their identity provider sign-in.
        return None
    audit.log(
        actor=existing.email,
        action="login.sso_refused",
        target=existing.email,
        detail=f"{via} sign-in refused: two-step sign-in is on for this account",
        ip=ip,
    )
    # Gate round 2, LOW-6: say how to get out of it. If this site no longer offers the
    # password form (LDAP turned off), only an admin reset of two-step sign-in lets the
    # identity provider adopt the account.
    return (
        "This account uses two-step sign-in on this site, so single sign-on cannot be used "
        "for it yet. Sign in with your password and authentication code, or ask an "
        "administrator to reset your two-step sign-in so you can use single sign-on."
    )


def _sso_account_refusal(existing, ip: str = "") -> str | None:
    """The refusals every OIDC sign-in applies to an existing account, whether it was
    found by its subject binding or by email: a local password account is never taken
    over (finding F5), a trust-host-auth row is a service identity, an account with
    two-step sign-in on here is not adopted, and a disabled account is refused."""
    if existing.password_hash and auth_source_of(existing) == "local":
        return "This email belongs to a local password account - sign in with your local password."
    # Train 0b: a trust-host-auth principal is a DEDICATED service identity, not a
    # login. Refuse SSO adoption exactly like a local-password collision, so an IdP
    # login as that email can never take over the persistent trust (admin) row.
    if auth_source_of(existing) == "trust":
        return (
            "This email is a trust-host-auth service identity, not a "
            "login account - it cannot be used to sign in."
        )
    refusal = _mfa_takeover_refusal(existing, "sso", ip)
    if refusal:
        return refusal
    if not existing.active:
        return "This account has been disabled."
    return None


def _sso_refused(existing, detail: str, ip: str) -> None:
    audit.log(
        actor=existing.email,
        action="login.sso_refused",
        target=existing.email,
        detail=f"sso sign-in refused: {detail}",
        ip=ip,
    )


def _claim_true(value) -> bool:
    """email_verified as the IdP sent it: JSON true, or the string "true" (some IdPs)."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


_SSO_OTHER_PROVIDER = (
    "This email belongs to an account that signs in through {via} on this site - "
    "sign in that way, or ask an administrator."
)
_SSO_OTHER_SUBJECT = (
    "This email is linked to a different single sign-on identity - ask an administrator."
)


def get_or_create_sso_user(claims: dict, ip: str = ""):
    """Map verified OIDC claims to a local account. Returns (user, None) or
    (None, error).

    Rules (identity-01: the account is bound to the OIDC subject, not to an email):
      - the id_token must carry `iss` and `sub`. An account already BOUND to that
        (issuer, sub) is the account, whatever the email claim says now;
      - otherwise the account is found by the `email` claim ONLY (never
        `preferred_username`), and only when the IdP vouches for it:
        `email_verified` true, or the admin switch `sso_trust_unverified_email` on for
        an IdP whose addresses are admin-assigned. Every first link (JIT creation or
        adopting an existing account) records the binding;
      - an existing LOCAL account that has a PASSWORD (any role, not just admin)
        is never taken over by SSO (finding F5, kept consistent with
        get_or_create_ldap_user); the person still signs in with the password;
      - an account bound to a DIFFERENT subject at this issuer is never re-linked by
        email (audited login.sso_refused). An account bound at another issuer (the
        admin switched identity providers) re-binds on a verified email;
      - an LDAP or SAML account is never adopted (another provider owns it). A
        SCIM-provisioned account is bound but keeps auth_source scim (SCIM owns its
        lifecycle, as on the SAML path). Other passwordless accounts (sso, or a local
        row an admin created without a password) are re-marked auth_source="sso";
      - unknown emails get a fresh account with the `sso_default_role` setting
        (validated against Role.ALL, falling back to translator)."""
    iss = str(claims.get("iss") or "").strip()
    sub = str(claims.get("sub") or "").strip()

    if iss and sub:
        with Session(_engine()) as s:
            bound = s.exec(
                select(User).where(User.sso_issuer == iss, User.sso_subject == sub)
            ).all()
        if len(bound) > 1:
            return None, _SSO_OTHER_SUBJECT
        if bound:
            refusal = _sso_account_refusal(bound[0], ip)
            if refusal:
                return None, refusal
            # Gate #420: a bound account that LDAP or SAML has since taken over belongs to
            # that provider; OIDC never signs into it (the same rule as the email path).
            source = auth_source_of(bound[0])
            if source in ("ldap", "saml"):
                _sso_refused(bound[0], f"account belongs to {source}", ip)
                return None, _SSO_OTHER_PROVIDER.format(via=source.upper())
            with Session(_engine()) as s:
                row = s.get(User, bound[0].id)
                row.last_login = datetime.utcnow()
                s.add(row)
                s.commit()
                s.refresh(row)
                return row, None

    email = normalize_email(claims.get("email"))
    if not email or "@" not in email:
        return None, "The identity provider did not supply an email address."

    existing = get_by_email(email)
    if existing:
        refusal = _sso_account_refusal(existing, ip)
        if refusal:
            return None, refusal
    if not iss or not sub:
        return None, "The identity provider did not supply a subject identifier."
    verified = _claim_true(claims.get("email_verified"))
    if not verified and settings.get("sso_trust_unverified_email") != "1":
        if existing:
            _sso_refused(existing, "email not verified by the identity provider", ip)
        return None, (
            "The identity provider did not confirm this email address, so it cannot be "
            "used to sign in here. Ask an administrator."
        )

    if existing:
        source = auth_source_of(existing)
        if source in ("ldap", "saml"):
            _sso_refused(existing, f"account belongs to {source}", ip)
            return None, _SSO_OTHER_PROVIDER.format(via=source.upper())
        if existing.sso_subject and existing.sso_issuer == iss and existing.sso_subject != sub:
            _sso_refused(existing, "email is linked to another subject", ip)
            return None, _SSO_OTHER_SUBJECT
        with Session(_engine()) as s:
            row = s.get(User, existing.id)
            if not (source == "scim" or getattr(row, "external_id", None)):
                row.auth_source = "sso"
            row.sso_issuer = iss
            row.sso_subject = sub
            row.last_login = datetime.utcnow()
            s.add(row)
            s.commit()
            s.refresh(row)
            linked_email = row.email
        audit.log(
            actor=linked_email,
            action="login.sso_linked",
            target=linked_email,
            detail="sso identity linked to this account",
            ip=ip,
        )
        return row, None

    # GATE R2 MED-1: a brand-new account. Under the sacred multi-org gate there is no org
    # to assign, so refuse JIT provisioning rather than mint a NULL-org user. Its SAML
    # sibling sixty lines below already did exactly this; OIDC and LDAP did not, and this
    # train's correctly scoped /users listing is what turns such an orphan from
    # visible-and-repairable into invisible, unsearchable and unable to authenticate.
    # #181: the refusal itself now lives in create_user (refuse_orgless_account); this
    # door only keeps its own wording for the person at the IdP callback.
    role = settings.get("sso_default_role") or Role.TRANSLATOR
    if role not in Role.ALL:
        role = Role.TRANSLATOR
    # BUG-A3-1: create_user now format-validates the email — a malformed IdP claim is a
    # clean SSO refusal (this function's (user, error) contract), never a 500 in the
    # callback.
    try:
        user = create_user(email, claims.get("name") or "", role, "", auth_source="sso")
    except OrglessAccountError:
        return None, (
            "This account is not provisioned. Under multi-org, accounts are provisioned "
            "by your administrator (e.g. via SCIM) before single sign-on."
        )
    except ValueError as exc:
        return None, str(exc)
    # identity-01: bind the new account to the subject that created it.
    with Session(_engine()) as s:
        row = s.get(User, user.id)
        row.sso_issuer = iss
        row.sso_subject = sub
        s.add(row)
        s.commit()
        s.refresh(row)
        new_email = row.email
    # Gate #420: a JIT first link is audited like an adoption (help and docs promise it).
    audit.log(
        actor=new_email,
        action="login.sso_linked",
        target=new_email,
        detail="sso identity linked to a new account",
        ip=ip,
    )
    return row, None


def get_or_create_saml_user(email: str, name: str = "", ip: str = ""):
    """Map a verified SAML assertion (email + display name) to a local account (Train I).
    Returns (user, None) or (None, error). Mirrors get_or_create_sso_user, with two SAML
    additions:

      * SCIM-MANAGED accounts keep their SCIM lifecycle: a user whose auth_source is
        'scim' (or that carries an external_id) is logged in WITHOUT re-marking its
        auth_source or touching `active` — SCIM owns provisioning, SAML only authenticates
        (an active=false SCIM user therefore cannot log in via SAML — see the disabled
        branch below).
      * MULTI-ORG: a brand-new JIT account has no org context on the SAML path (unlike a
        SCIM key, which names the org), so under the sacred gate NEW JIT provisioning is
        REFUSED (the account must be provisioned first, e.g. via SCIM) rather than minted
        with a NULL org (the #73 see-all leak). Single-tenant does full JIT."""
    email = normalize_email(email)
    if not email or "@" not in email:
        return None, "The identity provider did not supply an email address."

    existing = get_by_email(email)
    if existing:
        # A local-password account is never taken over by an IdP login (finding F5).
        if existing.password_hash and auth_source_of(existing) == "local":
            return None, (
                "This email belongs to a local password account - sign in with your local password."
            )
        # A trust-host-auth service identity is not a login (Train 0b).
        if auth_source_of(existing) == "trust":
            return None, ("This email is a trust-host-auth service identity, not a login account.")
        refusal = _mfa_takeover_refusal(existing, "saml", ip)
        if refusal:
            return None, refusal
        if not existing.active:
            # Honors a SCIM deactivation (active=false) as well as any manual disable.
            return None, "This account has been disabled."
        # SCIM-managed: authenticate WITHOUT mutating SCIM-owned lifecycle fields.
        if auth_source_of(existing) == "scim" or getattr(existing, "external_id", None):
            with Session(_engine()) as s:
                row = s.get(User, existing.id)
                row.last_login = datetime.utcnow()
                s.add(row)
                s.commit()
                s.refresh(row)
                return row, None
        # An SSO/LDAP/SAML-provisioned account (no local password): re-mark saml + reuse.
        with Session(_engine()) as s:
            row = s.get(User, existing.id)
            row.auth_source = "saml"
            row.last_login = datetime.utcnow()
            s.add(row)
            s.commit()
            s.refresh(row)
            return row, None

    # A brand-new account. Under the sacred multi-org gate there is no org to assign, so
    # refuse JIT provisioning (provision via SCIM first) rather than mint a NULL-org user.
    # #181: create_user owns that refusal; this door keeps its own wording.
    role = settings.get("saml_default_role") or Role.TRANSLATOR
    if role not in Role.ALL:
        role = Role.TRANSLATOR
    try:
        user = create_user(email, name or "", role, "", auth_source="saml")
    except OrglessAccountError:
        return None, (
            "This account is not provisioned. Under multi-org, accounts are provisioned "
            "by your administrator (e.g. via SCIM) before SAML sign-in."
        )
    except ValueError as exc:
        return None, str(exc)
    return user, None


# --- session helpers ---
def session_user(request):
    """The current user, or None. When auth is OFF, returns the SYSTEM admin."""
    if not auth_required():
        return SYSTEM
    uid = request.session.get("uid")
    if not uid:
        return None
    # #247: the idle and absolute limits (a11y T9) hold at THIS seam, not only in the
    # sign-in wall. The wall skips every /auth path, so /auth/password-change used to
    # accept a session the product had already ended, for as long as the signed cookie
    # stayed valid. A session past either limit is cleared and treated as signed out
    # here, for every caller. Pure read of the clock: the last-active stamp is refreshed
    # only by enforce_session (the wall and keepalive), never by resolving the user.
    if session_clock(request.session)["expired"]:
        request.session.clear()
        return None
    with Session(_engine()) as s:
        u = s.get(User, uid)
        if not (u and u.active):
            return None
        # #138: server-side session invalidation. The cookie is a stateless signed
        # blob, so logout could only clear the CLIENT copy — a captured or cached
        # pre-logout cookie stayed usable until it expired. Every session records the
        # generation it was minted under; logout and change_password bump the user's
        # `session_epoch`, so a cookie from before either event no longer matches and
        # is treated exactly like a logged-out session (here, at the ONE seam every
        # web request goes through). A session with no `se` at all is a cookie minted
        # before this change: refused once, at deploy, and never again.
        if request.session.get("se") != int(u.session_epoch or 0):
            request.session.clear()
            return None
        # Multi-org P7: if the user's org was suspended after they logged in, drop
        # the session immediately (no-op with the sacred gate off).
        from app.services import access

        if not access.user_org_active(u):
            return None
        return u


def session_status(request) -> dict:
    """READ-ONLY view of the caller's session lifetime (a11y T9, `GET /auth/session`).

    Never writes the session: the user is resolved against a detached COPY of it (so even
    session_user's own clear-on-revoked-epoch lands on the copy), and the clock is the pure
    session_clock. Polling this therefore can never extend a session (invariant I1).
    Returns {"signed_in": False} with auth off, no user, a revoked cookie, or an expired
    session; otherwise the limits and the remaining seconds (min of idle and absolute)."""
    if not auth_required():
        return {"signed_in": False, "auth": "off"}
    snapshot = dict(request.session)
    if not snapshot.get("uid"):
        return {"signed_in": False}
    state = session_clock(snapshot)
    if state["expired"]:
        return {"signed_in": False, "expired": state["expired"]}
    if session_user(SimpleNamespace(session=snapshot)) is None:
        return {"signed_in": False}
    return {
        "signed_in": True,
        "idle_seconds": state["idle_seconds"],
        "max_seconds": state["max_seconds"],
        "idle_remaining": state["idle_remaining"],
        "absolute_remaining": state["absolute_remaining"],
        "remaining_seconds": state["remaining_seconds"],
        "ending": state["ending"],
    }


def warn_lookalike_accounts() -> list[int]:
    """Boot-time scan for accounts created before the lookalike rules (gate audit-actor
    round 2, LOW-7): an email carrying a hidden character outside a joining context, or one
    that renders as a reserved system actor, or one that differs from another account's only
    by U+200C/U+200D. ACTIVE accounts only, so deactivating one (the advice) silences it.
    Logs ONE warning naming the account ids (never the addresses) and returns them. Changes
    nothing and refuses no sign-in. Never raises, so a DB problem cannot fail startup.

    Cheap at any size (gate email-intl round 1, LOW-3): SQL returns only the rows that could
    match, those holding a character outside printable ASCII ("!".."~", which also catches
    spaces and controls) or an ASCII reserved-name shape, so the Python checks run on a
    handful of rows rather than every account. The prefilter is exact: a printable-ASCII
    address can only be reserved by being a reserved word or starting "unauthenticated"."""
    from sqlalchemy import or_

    try:
        with Session(_engine()) as s:
            dialect = s.get_bind().dialect.name
            shapes = [User.email.in_(sorted(RESERVED_ACTORS)), User.email.like("unauthenticated%")]
            if dialect == "sqlite":
                shapes.append(User.email.op("GLOB")("*[^!-~]*"))
            elif dialect == "postgresql":
                shapes.append(User.email.op("~")("[^!-~]"))
            else:  # unknown dialect: no portable filter, scan every row
                shapes = [User.id.is_not(None)]
            active = User.active == True  # noqa: E712
            rows = s.exec(select(User.id, User.email).where(active, or_(*shapes))).all()
            flagged = [
                uid
                for uid, email in rows
                if email and (has_hidden_email_char(email) or is_reserved_actor(email))
            ]
            # Joiner twins: an address whose joiner-stripped form is another active account.
            stripped = {
                email.replace(chr(0x200C), "").replace(chr(0x200D), ""): uid
                for uid, email in rows
                if email and uid not in flagged and (chr(0x200C) in email or chr(0x200D) in email)
            }
            if stripped:
                twins = s.exec(select(User.email).where(active, User.email.in_(stripped))).all()
                flagged += [stripped[e] for e in twins]
    except Exception:
        _logger.exception("Lookalike-account scan failed at startup (skipped)")
        return []
    if flagged:
        _logger.warning(
            "%d active account(s) carry an email with invisible characters, one that looks "
            "like a reserved system identity, or one that looks the same as another "
            "account's (user ids: %s). New accounts can no longer be "
            "created this way. An admin should deactivate these accounts on the Users page "
            "and, if the person is real, invite them again under a clean address.",
            len(flagged),
            ", ".join(str(u) for u in flagged),
        )
    return flagged


def seed_initial() -> bool:
    """On a fresh real install, create the default accounts and turn on the login
    wall. admin/admin = super admin; linguist/linguist = AI/CAT specialist;
    user1/user1 = standard user."""
    if user_count() > 0:
        return False
    # Dev seed accounts intentionally use short passwords — bypass the policy
    # (enforce_policy=False) that every PUBLIC caller now gets (#4). #181: a boot path on
    # an empty install, before any org context exists, so it opts out of the org-less
    # refusal on purpose; the Default-org backfill (_ensure_default_org) adopts them.
    for email, name, role in (
        ("admin", "Administrator", Role.ADMIN),
        ("linguist", "Staff Linguist", Role.LINGUIST),
        ("user1", "Standard User", Role.TRANSLATOR),
    ):
        user = create_user(email, name, role, email, enforce_policy=False, allow_orgless_boot=True)
        with Session(_engine()) as session:
            seeded = session.get(User, user.id)
            # Explicit test mode keeps the established fixture sign-in flow.
            seeded.password_must_change = not config.IS_TEST
            session.add(seeded)
            session.commit()
    settings.set_many({"security_auth_required": "1"})
    return True


# --- First-run setup token (#414) ---------------------------------------------------------
# A server install (LOC_TMS_SERVER=1, or run.py bound to a non-loopback HOST) used to hand
# the administrator account to whoever posted /auth/setup first. The wizard now also asks
# for a one-time setup token that only the operator can read: LOC_TMS_SETUP_TOKEN when set,
# otherwise a random token generated into DATA_DIR/.setup_token (0600) and printed once to
# the server log at startup. The file is removed as soon as an account exists. Loopback
# single-user installs and test mode never ask for it.
SETUP_ENV_VAR = "LOC_TMS_SETUP_TOKEN"
SETUP_MIN_LEN = 16
_SETUP_FILE_NAME = ".setup_token"


def setup_token_required() -> bool:
    """True when /auth/setup must be given the setup token (server posture, not test mode)."""
    if config.IS_TEST:
        return False
    return config.IS_SERVER or not config.loopback_bind()


def _setup_token_path():
    return config.DATA_DIR / _SETUP_FILE_NAME


def _env_setup_token() -> str:
    value = (config._get(SETUP_ENV_VAR) or "").strip()
    return value if len(value) >= SETUP_MIN_LEN else ""


def _read_setup_token_file() -> str:
    path = _setup_token_path()
    # Another worker may have created the file a moment ago and not written it yet.
    for _ in range(20):
        try:
            value = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return ""
        if value:
            return value
        time.sleep(0.05)
    return ""


def _setup_token(create: bool = False) -> tuple[str, bool]:
    """(token, created_now). The env value wins; otherwise the file, created when asked."""
    value = _env_setup_token()
    if value:
        return value, False
    value = _read_setup_token_file()
    if value or not create:
        return value, False
    path = _setup_token_path()
    config.private_directory(path.parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = _read_setup_token_file()
        if existing:
            return existing, False
        # An empty file is the leftover of a crash between create and write: it would
        # lock setup out for good (and be announced as an empty token). Replace it.
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return _read_setup_token_file(), False
    value = secrets.token_urlsafe(24)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(value + "\n")
    return value, True


def clear_setup_token() -> None:
    """Remove the generated setup token: an account exists, so it can never be used again."""
    try:
        _setup_token_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logging.getLogger("loc_tms").warning("could not remove the used setup token file")


def prepare_setup_token_at_boot() -> None:
    """Startup hook for server installs: print the setup token while no account exists,
    remove a leftover token file once one does."""
    if has_users():
        clear_setup_token()
        return
    if not setup_token_required():
        return
    log = logging.getLogger("loc_tms")
    if _env_setup_token():
        log.warning("No accounts yet. /auth/setup asks for the setup token from %s.", SETUP_ENV_VAR)
        return
    if (config._get(SETUP_ENV_VAR) or "").strip():
        log.error(
            "%s is shorter than %d characters and is ignored; using a generated token.",
            SETUP_ENV_VAR,
            SETUP_MIN_LEN,
        )
    token, created = _setup_token(create=True)
    if created:
        _announce_setup_token(token)
    elif token:
        # Printed in full once, when it was generated. A later restart (or another worker)
        # must not copy it into the log again: say where it is instead.
        log.warning(
            "No accounts yet. /auth/setup asks for the one-time setup token printed at the "
            "first start; it is in %s (or set %s).",
            _setup_token_path(),
            SETUP_ENV_VAR,
        )


def _announce_setup_token(token: str) -> None:
    logging.getLogger("loc_tms").warning(
        "No accounts yet. To create the administrator at /auth/setup, enter this one-time "
        "setup token: %s (also in %s; removed once an account exists).",
        token,
        _setup_token_path(),
    )


def setup_token_ok(submitted) -> bool:
    """True when /auth/setup may create the first account for this submitted token."""
    if not setup_token_required():
        return True
    # Only startup creates the token. A concurrent setup may already have consumed
    # and removed it; validation must never recreate a used setup credential.
    expected, _created = _setup_token()
    given = str(submitted or "").strip()
    return bool(expected and given) and hmac.compare_digest(
        given.encode("utf-8"), expected.encode("utf-8")
    )


def seed_admin(
    email: str,
    password: str,
    name: str = "Administrator",
    issue_key: bool = False,
    key_scopes: str = "docs",
):
    """Idempotently seed the FIRST admin on a fresh install and turn on the login
    wall. Shared by the /auth/setup wizard, the `init` CLI, and env-seeding, so
    they converge on one code path.

    Enforces the password policy (password_issue). No-ops (returns
    (None, None, "exists")) when any user already exists — so it is safe to run on
    every boot / re-run of `init`. On success returns (user, full_key_or_None,
    "created"); the API key is minted ONCE and its plaintext is returned so the
    caller can display it (never stored, never recoverable).

    `issue_key` mints a per-user API key with `key_scopes` (default "docs" — the
    document/MCP scope; "api" is always implied by apikeys.normalize_scopes)."""
    if user_count() > 0:
        return None, None, "exists"
    issue = password_issue(password, user=email)
    if issue:
        raise ValueError(issue)
    # Q1/F1: validate the requested key scopes BEFORE any state is created. Otherwise an
    # invalid `--key-scopes` raised from create_key() at the end of this function would
    # leave a half-seeded install (admin created + login wall on, but no key), and a
    # corrected retry no-ops because user_count() > 0 — so --issue-key silently never
    # happens. Validating up front makes an invalid scope a clean refusal with zero state.
    if issue_key:
        from app.services import apikeys

        apikeys.validate_scopes(key_scopes)
    # #181: the FIRST account on an empty install (user_count() == 0 above), so there is
    # no org context to stamp yet; it opts out of the org-less refusal on purpose.
    # #414: first_account_only re-checks under the user-mutation lock, so a concurrent
    # /auth/setup post and this call can never both create an admin.
    try:
        user = create_user(
            email, name, Role.ADMIN, password, allow_orgless_boot=True, first_account_only=True
        )
    except FirstAccountExists:
        return None, None, "exists"
    clear_setup_token()
    enable_login_wall()
    full_key = None
    if issue_key:
        from app.services import apikeys

        # #270 gate fix: when apikey_max_age_days is already set (config set/apply can run
        # before the first account exists), a no-expiry mint would be refused AFTER the
        # admin was created, leaving the half-seeded install the Q1/F1 note above forbids.
        # Mint the init key at the install's maximum lifetime instead.
        max_days = apikeys.max_key_age_days()
        init_expiry = (
            apikeys.expiry_from_days(min(max_days, apikeys.MAX_LIFETIME_DAYS)) if max_days else None
        )
        _, full_key = apikeys.create_key(
            user.id, name="init", scopes=key_scopes, expires_at=init_expiry
        )
    return user, full_key, "created"


def ensure_linguist() -> bool:
    """Add the default linguist persona to installs that were seeded before the
    role existed. Idempotent: only acts if no linguist account is present and the
    email isn't taken (so a deleted linguist isn't resurrected on every boot)."""
    if user_count() == 0:
        return False  # fresh install handled by seed_initial()
    with Session(_engine()) as s:
        if s.exec(select(User).where(User.role == Role.LINGUIST)).first():
            return False
        if s.exec(select(User).where(User.email == "linguist")).first():
            return False
    # #181: a boot-time persona backfill, not a public create, so it opts out of the
    # org-less refusal on purpose (the next boot's _ensure_default_org adopts the row).
    user = create_user(
        "linguist",
        "Staff Linguist",
        Role.LINGUIST,
        "linguist",
        enforce_policy=False,
        allow_orgless_boot=True,
    )
    # #290 (gate round 1 MEDIUM-1): the backfilled persona has a well-known password, so a
    # real install forces a change at first sign-in, exactly like the seeded admin.
    if user is not None and not config.IS_TEST:
        with Session(_engine()) as s:
            row = s.get(User, user.id)
            if row is not None:
                row.password_must_change = True
                s.add(row)
                s.commit()
    return True


class InviteRefused(ValueError):
    """An invitation that may not set a password on the existing account it names; the
    message is the one plain sentence for the accept page (gate sso-localpw round 1)."""


def accept_invite(
    token: str,  # pregate: allow-secret parameter name, not a value
    name: str,
    password: str,  # pregate: allow-secret parameter name, not a value
    ip: str = "",
):
    """Consume an invite: a NEW account is minted through the SHARED create_user seam
    (Train I I-fix — email validation, reserved-actor + customer-binding invariants, and
    ORG STAMPING that the old inline `User(...)` bypassed), stamped into the INVITER's
    org so an invitee joins the org that invited them (a no-op with the sacred gate off;
    it closes the NULL-org leak #73 under multi-org). An EXISTING account (an admin
    pre-created the row, then invited) is activated + (re)passworded in place — never a
    new creation, so create_user does not apply there. External behavior is preserved:
    the sole caller (web/auth.py accept_post) already policy-checks the password before
    calling here, so create_user's identical check is redundant, not a new rejection.

    Gate sso-localpw round 1: an existing account the invitation may not touch (see
    invite_refusal: non-local, trust, or deactivated with a password or SCIM link) raises
    InviteRefused with the plain sentence; the invitation is revoked and the refusal
    audited as `user.invite.refused`. Every other failure still returns None."""
    with Session(_engine()) as s:
        inv = s.exec(select(Invite).where(Invite.token == token)).first()
        if not inv or inv.accepted or inv.revoked_at or inv.expires_at < datetime.utcnow():
            return None
        inv_id, inv_email, inv_role = inv.id, inv.email, inv.role
        existing = s.exec(select(User).where(User.email == inv_email)).first()
        existing_id = existing.id if existing else None
        # #129: ownership survives CLI/system actors and an erased or moved inviter.
        inviter_org = inv.org_id

    # GATE R6 MED-1: this check used to sit AFTER the existing-account split below, so it
    # only ever guarded the new-account path. An org-less invitation addressed to an email
    # an admin had pre-created therefore completed end to end under an active gate, setting
    # that account's password. It belongs before the split, because it is a statement about
    # the INVITATION, not about which branch consumes it.
    #
    # Keeping it here also stops `get_invite` and `accept_invite` disagreeing. Today the
    # only caller checks `get_invite` four lines earlier, so the guard is really enforced
    # by the order of two calls in one route; a future /api accept twin or CLI accept verb
    # would bypass it by default. The service is the right place for an invariant.
    #
    # GATE R7 LOW-3: with the gate OFF this is a behavioural no-op, but it is no longer a
    # LEXICAL one on the existing-account path, which now evaluates multi_org_active() and
    # imports access where it previously did neither. Recorded so "strict no-op" is not
    # later read as "executes nothing"; measured identical to main across all 20 gate-off
    # acceptance cells.
    #
    # NOT closed here, and #177 owns it: this asks whether the INVITATION has an org. It
    # does not compare or stamp the TARGET's org, so an org-stamped invitation aimed at a
    # legacy NULL-org account still activates that account and leaves its org NULL, which
    # strands the person at the login page. See the #177 scope note.
    from app.services import access

    if access.multi_org_active() and inviter_org is None:
        return None

    if existing_id is not None:
        # Activate the pre-existing account in place (unchanged behavior).
        with Session(_engine()) as s:
            row = s.get(User, existing_id)
            # Gate sso-localpw round 1, MEDIUM-1: an invitation never changes HOW an account
            # signs in and never undoes a deactivation. The link may predate the account
            # becoming an IdP account, and whoever holds it picks the password, so it is
            # not an admin's decision to take the account off the IdP (an admin reset is,
            # and stays available). Refused, the invitation revoked, and audited.
            refusal = invite_refusal(row, inviter_org)  # #536: cross-org refused
            if refusal:
                invrow = s.get(Invite, inv_id)
                invrow.revoked_at = datetime.utcnow()
                s.add(invrow)
                s.commit()
                audit.log(
                    actor=row.email,
                    action="user.invite.refused",
                    target=row.email,
                    detail=_invite_refusal_detail(row),
                    ip=ip,
                )
                raise InviteRefused(refusal)
            from app.services import password_policy

            password_policy.record_password(s, row, password)
            row.name = name or row.name
            row.active = True
            invrow = s.get(Invite, inv_id)
            invrow.accepted = True
            s.add(row)
            s.add(invrow)
            s.commit()
            s.refresh(row)
        return row

    # FAIL CLOSED (#73-class leak): under the sacred multi-org gate an account MUST carry
    # an org. Legacy/operator invites with NULL ownership cannot mint a NULL-org account
    # under the gate; an admin must issue an org-owned invitation. The check itself now
    # runs above, before the existing-account split, so it covers both consumers.
    #
    # A NEW account: funnel through the shared seam. Create the user FIRST so a refusal
    # (reserved actor / malformed identity) never consumes the invite; only then mark it
    # accepted.
    try:
        user = create_user(
            inv_email, name, inv_role, password, auth_source="local", org_id=inviter_org
        )
    except ValueError:
        return None
    with Session(_engine()) as s:
        invrow = s.get(Invite, inv_id)
        if invrow is not None:
            invrow.accepted = True
            s.add(invrow)
            s.commit()
    return user
