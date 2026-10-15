"""security: client ip (one authority for allowlists and audit)

Documented in: docs/architecture/identity-and-access.md
"""

import hashlib
import ipaddress
import re
import secrets
import threading
import time
import unicodedata
import weakref
from datetime import datetime, timedelta
from types import SimpleNamespace

from argon2 import PasswordHasher
from sqlalchemy import text
from sqlmodel import Session, select

from app import db as _db_relief
from app.db import engine
from app.models import Invite, Role, User
from app.services import audit, settings
from app.services.admin_invariant import lock_user_mutations

_ph = PasswordHasher()


SYSTEM = SimpleNamespace(id=0, email="system", name="System", role=Role.ADMIN, active=True)


def _engine():
    """The engine this scope must use.

    Normally the module's pooled `engine`, byte-identically. Inside a RELIEF scope — a
    cancel or `/settings` request the capacity doorman admitted at a fully checked-out pool
    (gate A round 3, HIGH-4) — the POOL-LESS engine, so the identity/audit reads on the way
    to the one action that ENDS in-flight work cannot themselves wait on the pool that
    in-flight work has exhausted. The pooled engine is passed IN (not read from app.db) so a
    suite that rebound this module's `engine` keeps its own."""
    return _db_relief.relief_engine_or(engine)


def request_path(request) -> str:
    """Decoded external ASGI path, independent of Host and URL reconstruction.

    This deliberately reattaches root_path for SAML's script_name; it does NOT
    equal app.web.route_path.route_path(scope) under a non-empty root_path.
    Mounts keep the full path in scope; some ASGI hosts supply a relative path.
    Reattach the root exactly once. Never unquote again: scope['path'] is already
    decoded by the server.
    """
    path = request.scope["path"]
    root = request.scope.get("root_path", "")
    if not root or path == root or path.startswith(root + "/"):
        return path
    return root + path


def host_name(value: str) -> str:
    """Validate an authority, returning its lowercase hostname without the port."""
    if not value or any(c.isspace() or ord(c) < 33 or ord(c) >= 127 for c in value):
        raise ValueError("Invalid Host")
    if value.startswith("["):
        match = re.fullmatch(r"\[([0-9a-fA-F:.]+)\](?::([0-9]+))?", value)
        if not match:
            raise ValueError("Invalid Host")
        name = str(ipaddress.IPv6Address(match[1]))
    else:
        match = re.fullmatch(r"([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*\.?)(?::([0-9]+))?", value)
        if not match:
            raise ValueError("Invalid Host")
        name = match[1].lower().rstrip(".")
    if match[2] is not None and not 0 < int(match[2]) <= 65535:
        raise ValueError("Invalid Host")
    return name


def constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string equality that is SAFE for latin-1-decoded wire headers.

    Starlette decodes request headers as latin-1, so a header carrying non-ASCII BYTES
    (which httpx and any raw client can send even though httpx rejects non-ASCII str
    values) becomes a Python str with code points > 0x7f. A raw
    `secrets.compare_digest(str, str)` RAISES `TypeError: comparing strings with non-ASCII
    characters is not supported` on any such input -- and on an auth/CSRF hot path that
    surfaces as an UNAUTHENTICATED 500 (plus an is-set enumeration oracle). Hashing both
    sides to equal-length SHA-256 digests first makes every input comparable in constant
    time with no length side-channel, so a non-ASCII value is a clean False (a mismatch),
    NEVER a raise. `surrogatepass` keeps the encode total for any lone surrogate too.

    This is the same technique `trust_auth.source_permitted` uses for the (b) trust secret;
    factored here so the connector-token compare (`deps.api_identity`) and the CSRF compare
    (`main._csrf_ok`) -- both formerly non-ASCII-header 500s (task_3cc0e09a) -- share it.

    NOT an unset-guard: this is pure equality, so `constant_time_eq("", "")` returns True (two
    empty digests match). Callers that must reject an empty/absent secret MUST guard emptiness
    themselves before calling (e.g. `bool(sent) and constant_time_eq(sent, exp)`); every
    current caller does (deps.api_identity via verify_key's own miss, main/_csrf_ok and
    backoffice/_csrf_ok via explicit `bool(sent)`/`bool(expected)` guards)."""
    return secrets.compare_digest(
        hashlib.sha256((a or "").encode("utf-8", "surrogatepass")).digest(),
        hashlib.sha256((b or "").encode("utf-8", "surrogatepass")).digest(),
    )


# --- client IP (one authority for allowlists and audit) ---
def ingress_entries(value):
    """Comma lists tolerate whitespace and empty separators on every ingress door."""
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _forwarded_address(token):
    token = token.strip()
    try:
        return str(ipaddress.ip_address(token))
    except ValueError:
        match = re.fullmatch(r"(?:\[([0-9a-fA-F:.]+)\]|([0-9.]+)):([0-9]+)", token)
        if not match or not 0 < int(match[3]) <= 65535:
            raise ValueError("Malformed X-Forwarded-For") from None
        address = ipaddress.ip_address(match[1] or match[2])
        if address.version != (6 if match[1] else 4):
            raise ValueError("Malformed X-Forwarded-For") from None
        return str(address)


#: The DEFAULT minimum length (password_policy.DEFAULT_MIN_LENGTH). The minimum in force
#: is the admin-configurable `password_min_length` setting; read it via password_policy.
MIN_PASSWORD_LEN = 12


# --- password policy ---
def password_issue(pw: str, *, user=None, admin_reset: bool = False) -> str | None:
    """Why a proposed password is unacceptable, as one plain sentence, or None if it's
    fine. THE one authority every door calls (setup, invite-accept, self-service change,
    the forced change at sign-in, admin create and reset over web / CLI / /api). The
    rules are the admin-configurable password policy (app/services/password_policy.py:
    length, character classes, common / personal terms, reuse history). `user` is the
    account the password is for when the door knows it: a User row (adds the reuse
    rule and the always-on "not your current password" rule), or an email string (adds
    the personal-term rule). `admin_reset=True` (admin_set_password only) skips the rules
    against the account's stored passwords, so a reset is never a guessing oracle. Seeded
    dev accounts bypass this by calling create_user(enforce_policy=False)."""
    from app.services import password_policy

    return password_policy.issue(pw, user=user, admin_reset=admin_reset)


def password_change_required(user) -> str | None:
    """Returns "reset" (an admin reset the password) or "expired" (older than
    password_max_age_days) when this LOCAL account must choose a new password at sign-in
    before anything else; None otherwise (and always None for SSO/LDAP/SCIM/SAML)."""
    from app.services import password_policy

    return password_policy.change_required(user)


def password_rules() -> dict:
    """The password policy in force, for form hints: {"min_length", "summary"}."""
    from app.services import password_policy

    p = password_policy.current()
    return {"min_length": p.min_length, "summary": password_policy.summary(p)}


LOCKOUT_THRESHOLD = 5


LOCKOUT_WINDOW_S = 15 * 60


LOCKOUT_DURATION_S = 15 * 60


#: #384 (deploy-01): the per-source aggregate admission budget. A source address with
#: more than this many failed checks (any names) inside one LOCKOUT_WINDOW_S is refused
#: before any verifier runs, so rotating usernames cannot keep buying fresh per-name
#: budgets and their Argon2 work. Generous far above one NAT window's legitimate typo
#: traffic; the per-(name, ip) LOCKOUT_THRESHOLD is unchanged.
SOURCE_FAIL_LIMIT = 200


#: #384: hard global cap on each limiter dict below. Keys are fixed-length digests, so
#: this caps the address layer's worst-case retained bytes whatever the traffic sends.
LOGIN_TRACK_MAX_KEYS = 10000


_failures: dict[str, list[float]] = {}


_locked_until: dict[str, float] = {}


_source_failures: dict[str, list[float]] = {}
#: Pending (reserved, unsettled) checks as the epoch each was reserved (#411): a
#: reservation leaked by a server fault expires after PENDING_TTL_S instead of pinning
#: its per-key and per-source budget slot until the worker restarts.
_pending_attempts: dict[str, list[float]] = {}
_source_pending: dict[str, list[float]] = {}

#: #411: how long an unsettled reservation counts. Far longer than any one sign-in check
#: (an Argon2 verify plus a directory bind takes seconds), far shorter than the window.
PENDING_TTL_S = 5 * 60

# This is an account-wide reauthentication key, never a client source address.
_REAUTH_ACCOUNT_BUCKET = "reauth-account"


# --- login rate limiting / lockout (in-memory, per process) ---
def _source_bucket(ip: str) -> str:
    """The address half of a limiter key."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        pass  # Preserve non-IP budgets such as "reauth-account" and "testclient".
    else:
        if isinstance(address, ipaddress.IPv6Address):
            # Mapped IPv4 shares its IPv4 budget; native IPv6 shares a /64.
            # The integer form also drops any interface zone identifier.
            ip = str(address.ipv4_mapped or ipaddress.IPv6Network((int(address), 64), strict=False))
    return ip or ""


# A limiter key is "<name-digest>|<source-bucket>". The dicts it indexes:
# _failures -> recent failure timestamps (within LOCKOUT_WINDOW_S);
# _locked_until -> epoch when the lock lifts;
# _source_failures -> recent failed-check timestamps (aggregate, #384).
def _lockout_key(email: str, ip: str) -> str:
    # #384: the normalized name enters the key only as a fixed-length digest, so an
    # attacker-sized typed name can never be retained as dict-key text in process
    # memory (the durable throttle table already hashes its names the same way).
    name = hashlib.sha256(
        (email or "").lower().strip().encode("utf-8", "surrogatepass")
    ).hexdigest()
    return f"{name}|{_source_bucket(ip)}"


def _prune_address_state(now: float) -> None:
    """Expire idle state in O(cap) under _attempt_lock. A pending check pins its entries
    until it is settled or released or PENDING_TTL_S passes (#411); _address_room bounds
    new insertions."""
    for pending in (_pending_attempts, _source_pending):
        for key, stamps in list(pending.items()):
            live = [t for t in stamps if now - t < PENDING_TTL_S]
            if live:
                pending[key] = live
            else:
                del pending[key]
    for table, pending in ((_failures, _pending_attempts), (_source_failures, _source_pending)):
        for key, seen in list(table.items()):
            if key not in pending and not any(now - t < LOCKOUT_WINDOW_S for t in seen):
                del table[key]
    for key, lifted_at in list(_locked_until.items()):
        if lifted_at <= now:
            del _locked_until[key]


def _address_room(table: dict, key: str, pending: dict) -> bool:
    """Make room BEFORE insertion, never evicting an outstanding reservation."""
    target = LOGIN_TRACK_MAX_KEYS - (key not in table)
    for candidate in list(table):
        if len(table) <= target:
            break
        if candidate != key and candidate not in pending:
            del table[candidate]
    return len(table) <= target


def _finish_pending(key: str, src: str) -> bool:
    """Release exactly one pending check under _attempt_lock."""
    if not _pending_attempts.get(key):
        return False
    for table, bucket in ((_pending_attempts, key), (_source_pending, src)):
        stamps = table.get(bucket)
        if stamps is None:
            continue  # never counted on this table (trusted source, or expired)
        stamps.pop(0)  # the oldest reservation is the one nearest to expiring
        if not stamps:
            del table[bucket]
    return True


def login_trusted_networks(value):
    """Parse `login_trusted_networks` (#411) into networks; a list broader than IPv4 /8 or
    IPv6 /32 raises ValueError (a wide list would switch the source budget off for everyone)."""
    networks = [ipaddress.ip_network(p, strict=False) for p in ingress_entries(value)]
    if any(n.prefixlen < (8 if n.version == 4 else 32) for n in networks):
        raise ValueError(
            "trusted login networks require IPv4 /8 or narrower and IPv6 /32 or narrower"
        )
    # A mapped source (::ffff:a.b.c.d) is matched as its IPv4 address, so an IPv4-mapped
    # IPv6 entry could never match anything: refuse it rather than silently trust nobody.
    if any(n.version == 6 and n.subnet_of(_IPV4_MAPPED_NET) for n in networks):
        raise ValueError("write IPv4-mapped IPv6 networks as IPv4 (203.0.113.0/24)")
    return networks


_IPV4_MAPPED_NET = ipaddress.ip_network("::ffff:0:0/96")


def _source_trusted_for_login(ip: str) -> bool:
    """True when this source is on the admin `login_trusted_networks` list (#411).

    Fails CLOSED: an empty, malformed or too-broad stored value, or a source that is not
    an address (a placeholder bucket such as "reauth-account"), is never trusted."""
    raw = settings.get("login_trusted_networks")
    if not (raw or "").strip():
        return False
    try:
        networks = login_trusted_networks(raw)
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in net for net in networks if net.version == address.version)


def login_locked_seconds(email: str, ip: str) -> int:
    """Seconds remaining on this (email, ip)'s lockout; 0 when not locked."""
    remaining = _locked_until.get(_lockout_key(email, ip), 0) - time.time()
    return max(0, int(remaining))


#: Serializes reserve/settle so concurrent requests cannot all read "not locked" first.
_attempt_lock = threading.Lock()


def reserve_login_attempt(email: str, ip: str) -> bool:
    """RESERVE one attempt for (email, ip) BEFORE checking anything (TOTP gate round 2,
    MEDIUM-2): refused while locked or once LOCKOUT_THRESHOLD attempts are already counted
    in the window, else the attempt is counted up front. So concurrent requests can never
    check more than LOCKOUT_THRESHOLD codes per window. Pair with settle_login_failure
    (a wrong answer) or clear_login_failures (a right one); release_login_attempt undoes a
    reservation that ended up checking nothing. #384 adds the two admission guards: stale
    limiter entries expire and dicts stay globally capped, and a source that failed
    SOURCE_FAIL_LIMIT checks (any names) in the window is refused before any verifier.
    Pending checks count on both budgets until settled, even past the time window."""
    key = _lockout_key(email, ip)
    src = _source_bucket(ip)
    # #411: a trusted shared NAT skips ONLY the per-source aggregate budget; the per-(name,
    # source) lockout (and so the MFA code budget) still applies to it.
    trusted = _source_trusted_for_login(ip)
    now = time.time()
    with _attempt_lock:
        _prune_address_state(now)
        if _locked_until.get(key, 0) > now:
            return False
        src_recent = [t for t in _source_failures.get(src, []) if now - t < LOCKOUT_WINDOW_S]
        aggregate = src != _REAUTH_ACCOUNT_BUCKET and not trusted
        if aggregate and len(src_recent) + len(_source_pending.get(src, ())) >= SOURCE_FAIL_LIMIT:
            return False
        recent = [t for t in _failures.get(key, []) if now - t < LOCKOUT_WINDOW_S]
        if len(recent) + len(_pending_attempts.get(key, ())) >= LOCKOUT_THRESHOLD:
            _failures[key] = recent
            return False
        if key not in _pending_attempts and len(_pending_attempts) >= LOGIN_TRACK_MAX_KEYS:
            return False
        if (
            aggregate
            and src not in _source_pending
            and len(_source_pending) >= LOGIN_TRACK_MAX_KEYS
        ):
            return False
        if not _address_room(_failures, key, _pending_attempts):
            return False
        if aggregate and not _address_room(_source_failures, src, _source_pending):
            return False
        _failures[key] = recent
        _pending_attempts.setdefault(key, []).append(now)
        if aggregate:
            _source_failures[src] = src_recent
            _source_pending.setdefault(src, []).append(now)
        return True


def release_login_attempt(email: str, ip: str) -> None:
    """Give back a reservation that did not check a code (the other key refused)."""
    key = _lockout_key(email, ip)
    with _attempt_lock:
        _finish_pending(key, _source_bucket(ip))


def settle_login_failure(email: str, ip: str) -> bool:
    """A reserved attempt was wrong: it stays counted; lock the pair once the window holds
    LOCKOUT_THRESHOLD of them. Returns True for the failure that locked it. The failure
    also counts toward the source's aggregate budget that reserve_login_attempt weighs
    before admitting (#384)."""
    key = _lockout_key(email, ip)
    trusted = _source_trusted_for_login(ip)
    now = time.time()
    with _attempt_lock:
        _prune_address_state(now)
        recent = [t for t in _failures.get(key, []) if now - t < LOCKOUT_WINDOW_S]
        src = _source_bucket(ip)
        if not _finish_pending(key, src):
            return False
        recent.append(now)
        if src != _REAUTH_ACCOUNT_BUCKET and not trusted:
            src_recent = [t for t in _source_failures.get(src, []) if now - t < LOCKOUT_WINDOW_S]
            src_recent.append(now)
            # Admission pinned this entry; settlement cannot recreate an evicted key.
            _source_failures[src] = src_recent
        if (
            len(recent) + len(_pending_attempts.get(key, ())) >= LOCKOUT_THRESHOLD
            and _locked_until.get(key, 0) <= now
        ):
            _address_room(_locked_until, key, {})
            _locked_until[key] = now + LOCKOUT_DURATION_S
            if key not in _pending_attempts:
                _failures.pop(key, None)
            else:
                _failures[key] = recent
            return True
        _failures[key] = recent
        return False


def clear_login_failures(email: str, ip: str, *, reserved: bool = True) -> None:
    """Clear completed failures, releasing this check only if it still holds a slot."""
    key = _lockout_key(email, ip)
    with _attempt_lock:
        if reserved:
            _finish_pending(key, _source_bucket(ip))
        if key in _pending_attempts:
            _failures[key] = []
        else:
            _failures.pop(key, None)
        _locked_until.pop(key, None)


# --- session idle timeout ---
def session_idle_expired(last_active: int, now: int, idle_minutes: int) -> bool:
    return idle_minutes > 0 and (now - last_active) > idle_minutes * 60


def _now() -> int:
    """The ONE session clock (a11y T9). login() and every expiry decision read it, so a
    suite can drive session time by patching this function instead of sleeping."""
    return int(time.time())


# Absolute limit read-side fallback: a stored value that is not a whole number (the write
# doors validate, so only a hand-edited row gets here) keeps the DEFAULT limit rather than
# silently turning the limit off.
_SESSION_MAX_HOURS_FALLBACK = 24
# An `iat` further than this in the FUTURE cannot be an honest stamp from this install (a
# server clock that stepped back, or skew between machines sharing the cookie secret); it
# is treated like a missing `iat` (expired), never as extra session time.
_IAT_FUTURE_SKEW_S = 300


def session_limits() -> tuple[int, int]:
    """(idle_seconds, max_seconds) from settings; 0 means that limit is off.

    `session_idle_minutes` keeps its historical read (garbage -> off). `session_max_hours`
    (a11y T9, maintainer decision 2) is the absolute limit counted from sign-in and never renewed
    by activity."""
    try:
        idle_minutes = int(settings.get("session_idle_minutes") or 0)
    except ValueError:
        idle_minutes = 0
    raw_max = settings.get("session_max_hours")
    try:
        max_hours = int(raw_max if raw_max not in (None, "") else _SESSION_MAX_HOURS_FALLBACK)
    except ValueError:
        max_hours = _SESSION_MAX_HOURS_FALLBACK
    return max(0, idle_minutes) * 60, max(0, max_hours) * 3600


def _stamp(value) -> int | None:
    """A session time stamp as an int, or None when absent or not a plain integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def session_clock(sess, now: int | None = None) -> dict:
    """PURE evaluation of a session's idle and absolute expiry. Never writes `sess`.

    Returns {"now", "expired": "" | "idle" | "max", "idle_seconds", "max_seconds",
    "idle_remaining", "absolute_remaining", "remaining_seconds", "ending"}; a *_remaining
    is None when that limit is off, and remaining_seconds is the smaller of the two
    (clamped at 0), so nothing (keepalive, typing) can report more time than the absolute
    limit allows. A session with no `iat` (minted before the absolute limit existed) has
    no absolute time left: it is expired whenever the absolute limit is on (a11y T9 I3,
    the conservative choice: sign in again once)."""
    now = _now() if now is None else now
    idle_s, max_s = session_limits()
    idle_left = abs_left = None
    if idle_s > 0:
        la = _stamp(sess.get("la"))
        idle_left = idle_s - (now - (now if la is None else la))
    if max_s > 0:
        iat = _stamp(sess.get("iat"))
        if iat is not None and iat > now + _IAT_FUTURE_SKEW_S:
            iat = None
        abs_left = 0 if iat is None else min(max_s, iat + max_s - now)
    expired = ""
    if abs_left is not None and abs_left <= 0:
        expired = "max"
    elif idle_left is not None and idle_left < 0:  # strict, exactly session_idle_expired
        expired = "idle"
    lefts = [v for v in (idle_left, abs_left) if v is not None]
    remaining = max(0, min(lefts)) if lefts else None
    ending = ""
    if lefts:
        ending = "max" if abs_left is not None and abs_left == min(lefts) else "idle"
    return {
        "now": now,
        "expired": expired,
        "idle_seconds": idle_s,
        "max_seconds": max_s,
        "idle_remaining": None if idle_left is None else max(0, idle_left),
        "absolute_remaining": None if abs_left is None else max(0, abs_left),
        "remaining_seconds": remaining,
        "ending": ending,
    }


def enforce_session(request) -> str:
    """Enforce the idle AND absolute limits on a logged-in session and refresh ONLY its
    last-active stamp (`la`; `iat` is never rewritten here). Returns "" when the session
    is fine (or anonymous), else the reason it expired ("idle" / "max") after clearing
    the session exactly like idle expiry always has."""
    if not request.session.get("uid"):
        return ""
    state = session_clock(request.session)
    if state["expired"]:
        request.session.clear()
        return state["expired"]
    if getattr(request, "headers", {}).get("x-loc-background") != "1":
        request.session["la"] = state["now"]
    return ""


def touch_session(request) -> bool:
    """Enforce the idle and absolute limits and refresh the last-active stamp. Returns
    False (after clearing the session) if it expired."""
    return enforce_session(request) == ""


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except Exception:
        return False


#: A fixed Argon2id hash of a random value nobody knows, made with the SAME hasher (so the
#: same cost parameters) as every real password hash. Built once, on first use.
_DUMMY_HASH: str | None = None


def dummy_verify(pw) -> None:
    """Spend exactly the work of one real password check, and always fail.

    The sign-in timing leak (HECVAT MFA train): authenticate() used to return at once for
    an unknown email, but ran a full Argon2 verify (about 84 ms against 18 ms) for a real
    one, so the response time told a stranger which addresses have accounts. Every path
    that decides "no" WITHOUT a real verify calls this instead, so both answers cost the
    same. The two-step sign-in step uses it the same way when there is nothing to check."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _ph.hash(secrets.token_urlsafe(24))
    verify_password(_DUMMY_HASH, str(pw or ""))


def user_count() -> int:
    """How many accounts exist: one SELECT count(*) (#252: this used to hydrate every User
    row, on every walled request and every sign-in page render)."""
    from sqlalchemy import func

    with Session(_engine()) as s:
        return int(s.exec(select(func.count()).select_from(User)).one() or 0)


def has_users() -> bool:
    """True once any account exists: a single-row EXISTS-style probe (LIMIT 1), the cheap
    question the sign-in wall and the sign-in page actually ask (#252). Not cached: an
    erase or a test reset can take an install back to zero accounts, and the probe is an
    indexed primary-key read."""
    with Session(_engine()) as s:
        return s.exec(select(User.id).limit(1)).first() is not None


def list_users(q: str = "", limit: int | None = None, offset: int = 0, *, org_id=None):
    """List users within org_id, applying search before pagination.

    Missing scope fails closed in multi-org mode. The verified local operator may
    explicitly pass access.UNSCOPED; gate-off callers retain the unfiltered default.
    """
    from app.services import access

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    with Session(_engine()) as s:
        stmt = select(User).order_by(User.created_at)
        if org_id is not None:
            stmt = stmt.where(User.org_id == org_id)
        q = (q or "").strip()
        if q:
            from sqlalchemy import func, or_

            needle = f"%{q.lower()}%"
            stmt = stmt.where(
                or_(func.lower(User.email).like(needle), func.lower(User.name).like(needle))
            )
        if limit is not None:
            stmt = stmt.offset(offset).limit(limit)
        return s.exec(stmt).all()


def count_users(q: str = "", *, org_id=None) -> int:
    """Count users matching the same optional `q` filter as list_users()."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return 0
    org_id = access.org_scope_value(org_id)
    with Session(_engine()) as s:
        from sqlalchemy import func, or_

        stmt = select(func.count()).select_from(User)
        if org_id is not None:
            stmt = stmt.where(User.org_id == org_id)
        q = (q or "").strip()
        if q:
            needle = f"%{q.lower()}%"
            stmt = stmt.where(
                or_(func.lower(User.email).like(needle), func.lower(User.name).like(needle))
            )
        return s.exec(stmt).one()


def normalize_email(email) -> str:
    """THE identity normalization law, in one named place (#74).

    An account IS its lower/stripped address: get_by_email has always lowercased its
    needle, so 'Bob@x.test' and 'bob@x.test' were never two identities to any reader —
    but nothing stopped a WRITE from storing the mixed-case form, which then could not
    be found by any lookup and, worse, could sit alongside the lowercase twin as a
    second row that by-email `.first()` picked between arbitrarily. Every User.email
    write goes through this, and DB-level index ux_user_email_lower (alembic
    a1f4c8b2d6e9) enforces the same normalization one layer down."""
    return (email or "").lower().strip()


#: Domains the app itself writes into User.email as a STAND-IN, never a person's address:
#: `dedup.invalid` (alembic a1f4c8b2d6e9: a retired duplicate's tombstone and the placeholder
#: an emailless account gets so the unique index can build) and `erased.invalid` (the gdpr
#: erasure tombstone). RFC 2606 `.invalid`, so mail can never deliver to them.
PLACEHOLDER_EMAIL_DOMAINS = frozenset({"dedup.invalid", "erased.invalid"})
PLACEHOLDER_EMAIL_MESSAGE = (
    "addresses at dedup.invalid and erased.invalid are reserved placeholders, not real "
    "email addresses."
)


def is_placeholder_email(email) -> bool:
    """True when `email` is an app-written stand-in address (see PLACEHOLDER_EMAIL_DOMAINS).

    Such an address is NOT an identity (#520 gate): nobody may sign in with it, reset or
    accept an invite with it, link an SSO/SAML/LDAP/SCIM login by it, or create an account
    carrying it. Otherwise an emailless account that has a password could sign in by typing
    the placeholder the migration gave it."""
    norm = normalize_email(email)
    _local, at, domain = norm.rpartition("@")
    return bool(at) and domain in PLACEHOLDER_EMAIL_DOMAINS


def get_by_email(email: str):
    """The account an address IDENTIFIES, or None. A placeholder address identifies nobody
    (is_placeholder_email), so every by-email door (password sign-in, invite acceptance,
    OIDC/SAML/LDAP adoption, SCIM create, trusted-device checks) treats it as unknown.
    Admin tooling that manages such a row reaches it by id or by its own direct query."""
    if is_placeholder_email(email):
        return None
    with Session(_engine()) as s:
        return s.exec(select(User).where(User.email == normalize_email(email))).first()


#: The audit/egress actor word for a request no identity was resolved for (the maintainer
#: 2026-09-25). The written actor is "unauthenticated <client ip>" or
#: "unauthenticated (unknown address)"; see security.unauthenticated_actor.
UNAUTHENTICATED_ACTOR = "unauthenticated"

# "anonymous" stays reserved: older installs carry audit rows written with it (the
# pre-2026-09-25 label for an unidentified /api caller), and those rows are never
# rewritten, so no account may ever claim that name either.
RESERVED_ACTORS = {"scheduler", "system", "companion-app", "anonymous", UNAUTHENTICATED_ACTOR}


#: Characters that render as nothing (or as a blank) yet are letters/symbols to Python, so
#: neither `isspace()` nor the format-character category catches them: the Hangul fillers
#: and the braille blank. Stripped before any reserved-name comparison.
_INVISIBLE_LETTERS = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800)))

#: The common Cyrillic/Greek letters that are drawn exactly like a Latin one. NFKC does not
#: fold these, so "anonymous" spelled with a Cyrillic a (U+0430) would otherwise pass as a
#: new name. Not a full confusables table: enough for the reserved words, all this guards.
_HOMOGLYPHS = str.maketrans(
    {
        0x0430: "a", 0x0435: "e", 0x043E: "o", 0x0440: "p", 0x0441: "c",
        0x0443: "y", 0x0445: "x", 0x0456: "i", 0x0458: "j", 0x0455: "s",
        0x0501: "d", 0x043D: "h", 0x0442: "t", 0x043C: "m", 0x03BF: "o",
        0x03B1: "a", 0x03B5: "e", 0x03B9: "i", 0x03BA: "k", 0x03BD: "v",
        0x03C1: "p", 0x03C4: "t", 0x03C5: "u", 0x0261: "g", 0x0251: "a",
    }
)  # fmt: skip


def _is_invisible(c: str) -> bool:
    """Format (Cf), control (Cc), combining-mark (Mn/Me) or blank-rendering characters."""
    return c in _INVISIBLE_LETTERS or unicodedata.category(c) in (
        "Cf",
        "Cc",
        "Mn",
        "Me",
        "Co",
        "Cn",
        "Cs",
    )


_ZWNJ, _ZWJ = "\u200c", "\u200d"
_VIRAMA_CCC = 9

#: Unicode Joining_Type (ArabicShaping.txt, Unicode 15.1) for the letters of the joining
#: scripts an address plausibly uses: Arabic (0600-06FF, the 0750-077F supplement), Syriac,
#: NKo and Mongolian. (first, last, type): D dual-joining, R right-joining, L left-joining.
#: Anything not listed here and not a combining mark is non-joining ("U"), which is the
#: conservative direction: a ZWNJ next to an unlisted letter (Arabic Extended-A/B, Manichaean,
#: Psalter Pahlavi, Hanifi Rohingya, Sogdian, Adlam, ...) is refused, never wrongly admitted.
_JOINING_RANGES = (
    (0x0620, 0x0620, "D"), (0x0622, 0x0625, "R"), (0x0626, 0x0626, "D"),
    (0x0627, 0x0627, "R"), (0x0628, 0x0628, "D"), (0x0629, 0x0629, "R"),
    (0x062A, 0x062E, "D"), (0x062F, 0x0632, "R"), (0x0633, 0x063F, "D"),
    (0x0641, 0x0647, "D"), (0x0648, 0x0648, "R"), (0x0649, 0x064A, "D"),
    (0x066E, 0x066F, "D"), (0x0671, 0x0673, "R"), (0x0675, 0x0677, "R"),
    (0x0678, 0x0687, "D"), (0x0688, 0x0699, "R"), (0x069A, 0x06BF, "D"),
    (0x06C0, 0x06C0, "R"), (0x06C1, 0x06C2, "D"), (0x06C3, 0x06CB, "R"),
    (0x06CC, 0x06CC, "D"), (0x06CD, 0x06CD, "R"), (0x06CE, 0x06CE, "D"),
    (0x06CF, 0x06CF, "R"), (0x06D0, 0x06D1, "D"), (0x06D2, 0x06D3, "R"),
    (0x06D5, 0x06D5, "R"), (0x06EE, 0x06EF, "R"), (0x06FA, 0x06FC, "D"),
    (0x06FF, 0x06FF, "D"),
    (0x0710, 0x0710, "R"), (0x0712, 0x0714, "D"), (0x0715, 0x0719, "R"),
    (0x071A, 0x071D, "D"), (0x071E, 0x071E, "R"), (0x071F, 0x0727, "D"),
    (0x0728, 0x0728, "R"), (0x0729, 0x0729, "D"), (0x072A, 0x072A, "R"),
    (0x072B, 0x072B, "D"), (0x072C, 0x072C, "R"), (0x072D, 0x072E, "D"),
    (0x072F, 0x072F, "R"), (0x074D, 0x074D, "R"), (0x074E, 0x074F, "D"),
    (0x0750, 0x0758, "D"), (0x0759, 0x075B, "R"), (0x075C, 0x076A, "D"),
    (0x076B, 0x076C, "R"), (0x076D, 0x0770, "D"), (0x0771, 0x0771, "R"),
    (0x0772, 0x0772, "D"), (0x0773, 0x0774, "R"), (0x0775, 0x0777, "D"),
    (0x0778, 0x0779, "R"), (0x077A, 0x077F, "D"),
    (0x07CA, 0x07EA, "D"),
    (0x1807, 0x1807, "D"), (0x1820, 0x1878, "D"), (0x1887, 0x18A8, "D"),
    (0x18AA, 0x18AA, "D"),
)  # fmt: skip


def _joining_type(c: str) -> str:
    """Joining_Type of `c` per _JOINING_RANGES; combining marks are transparent ("T")."""
    if unicodedata.category(c) in ("Mn", "Me"):
        return "T"
    cp = ord(c)
    for first, last, jt in _JOINING_RANGES:
        if first <= cp <= last:
            return jt
    return "U"


def _indic_block(c: str) -> int | None:
    """The Indic script block of `c` (Devanagari .. Sinhala, U+0900-0DFF), else None."""
    cp = ord(c)
    return cp >> 7 if 0x0900 <= cp <= 0x0DFF else None


def _indic_joiner_ok(s: str, i: int) -> bool:
    """s[i] is a joiner right after the virama s[i - 1]: the virama's base (skipping marks
    such as a nukta) and the character after the joiner must be letters of its block."""
    block = _indic_block(s[i - 1])
    if block is None or i + 1 >= len(s):
        return False
    b = i - 2
    while b >= 0 and unicodedata.category(s[b]) == "Mn" and _indic_block(s[b]) == block:
        b -= 1
    nxt = s[i + 1]
    return (
        b >= 0
        and unicodedata.category(s[b]).startswith("L")
        and _indic_block(s[b]) == block
        and unicodedata.category(nxt).startswith("L")
        and _indic_block(nxt) == block
    )


def _joiner_in_context(s: str, i: int) -> bool:
    """IDNA2008 CONTEXTJ (RFC 5892 Appendix A.1 / A.2) for the ZWNJ or ZWJ at s[i].

    Both are allowed directly after a virama (canonical combining class 9: Devanagari,
    Bengali, and the other Indic scripts). ZWNJ is also allowed between a left- or
    dual-joining letter and a right- or dual-joining one, with only transparent marks in
    between (Persian "mi" + ZWNJ + "khaham"). Latin letters, digits, "@" and "." are never a
    joining context, so a joiner inside "system" or an ordinary address stays refused.

    The virama branch is narrower than RFC 5892 (gate email-intl round 1, MEDIUM-1): the
    virama must sit on a letter of the SAME Indic block (U+0900-0DFF, one 128-code-point
    block per script) and a letter of that block must follow the joiner. So a joiner at the
    end of a word, before "@" or ".", or on a Latin letter carrying a borrowed virama, where
    it would render as nothing, is refused. Twins that still render alike (Tamil pulli +
    ZWNJ + consonant) are caught by the uniqueness skeleton (email_skeleton)."""
    if i and unicodedata.combining(s[i - 1]) == _VIRAMA_CCC:
        return _indic_joiner_ok(s, i)
    if s[i] != _ZWNJ:
        return False
    j = i - 1
    while j >= 0 and _joining_type(s[j]) == "T":
        j -= 1
    k = i + 1
    while k < len(s) and _joining_type(s[k]) == "T":
        k += 1
    return (
        j >= 0
        and k < len(s)
        and _joining_type(s[j]) in ("L", "D")
        and _joining_type(s[k]) in ("R", "D")
    )


#: Default-ignorable combining marks (category Mn, so the Cf test misses them) that render
#: as nothing: COMBINING GRAPHEME JOINER, the Khmer inherent vowels, the Mongolian free
#: variation selectors, and the variation selectors VS1-16 and VS17-256 (gate email-intl
#: round 1, LOW-1). The Hangul fillers are already in _INVISIBLE_LETTERS.
_IGNORABLE_MARKS = (
    (0x034F, 0x034F), (0x17B4, 0x17B5), (0x180B, 0x180F), (0xFE00, 0xFE0F), (0xE0100, 0xE01EF),
)  # fmt: skip


def _is_ignorable_mark(c: str) -> bool:
    cp = ord(c)
    return any(first <= cp <= last for first, last in _IGNORABLE_MARKS)


def has_hidden_email_char(s: str) -> bool:
    """True when `s` carries a character that makes one address display as another: a
    format (Cf), private-use, unassigned or surrogate code point, a blank-rendering letter,
    or a default-ignorable combining mark. U+200C / U+200D count only OUTSIDE a joining
    context (_joiner_in_context)."""
    for i, c in enumerate(s):
        if c in _INVISIBLE_LETTERS or _is_ignorable_mark(c):
            return True
        if unicodedata.category(c) in ("Cf", "Co", "Cn", "Cs"):
            if c in (_ZWNJ, _ZWJ) and _joiner_in_context(s, i):
                continue
            return True
    return False


def _strip_joiners(s: str) -> str:
    return s.replace(_ZWNJ, "").replace(_ZWJ, "")


def email_skeleton(email) -> str:
    """The rendered identity of an address for uniqueness: NFKC, casefold, joiners removed.
    Two addresses with the same skeleton display alike (gate email-intl round 1, MEDIUM-1)."""
    return _strip_joiners(unicodedata.normalize("NFKC", str(email or "")).casefold()).strip()


LOOKALIKE_EMAIL_MESSAGE = (
    "an account with an address that looks the same already exists; "
    "use that account or a clearly different address."
)


def lookalike_account_id(email) -> int | None:
    """Id of an EXISTING account whose address differs from `email` only by U+200C/U+200D
    (or whose stored form equals this address's skeleton), else None. An exact match is
    not a lookalike: the callers' own duplicate checks own that case.

    One query, joiner-stripped on both sides in SQL (replace() exists in SQLite and
    Postgres; the joiners are bound parameters). The stored side is not NFKC-folded (SQL
    cannot), so an existing address in fullwidth or another compatibility form is not
    matched against a later plain one; the other direction is."""
    from sqlalchemy import and_, func, or_

    norm = normalize_email(email)
    needles = sorted({_strip_joiners(norm), email_skeleton(norm)})
    stored = func.replace(func.replace(User.email, _ZWNJ, ""), _ZWJ, "")
    # Only a stored address that carries a joiner can differ from its stripped form, so the
    # replace() runs on those rows alone; the rest compare by the indexed email column.
    carries_joiner = or_(User.email.contains(_ZWNJ), User.email.contains(_ZWJ))
    match = or_(User.email.in_(needles), and_(carries_joiner, stored.in_(needles)))
    with Session(_engine()) as s:
        return s.exec(select(User.id).where(match, User.email != norm)).first()


def refuse_lookalike_email(email) -> None:
    """Raise ValueError when `email` would be an invisible twin of an existing account."""
    if lookalike_account_id(email) is not None:
        raise ValueError(LOOKALIKE_EMAIL_MESSAGE)


def is_reserved_actor(value) -> bool:
    """True when `value` names, or merely LOOKS like, a synthetic audit actor: one of
    RESERVED_ACTORS, or any "unauthenticated <...>" / "unauthenticated(...)" label. No user,
    trust principal or on-behalf-of attribution may carry one, so a real identity can never
    pass itself off as a refused unauthenticated caller (or the reverse) in the audit trail
    or in the org-scoped audit filter, which matches the actor column against member emails.

    Compared on the rendered shape (gate audit-actor round 1, MEDIUM-1 / LOW-1): zero-width
    and format characters, NBSP and every other blank, fullwidth forms and the common
    Cyrillic/Greek lookalikes are all normalized away first, so "anonymous" + U+200B,
    "system" in fullwidth, or "unauthenticated" + NBSP + an address are reserved too."""
    s = unicodedata.normalize("NFKC", str(value or "")).casefold().translate(_HOMOGLYPHS)
    # An invisible character is a SEPARATOR for the prefix test ("unauthenticated" + a braille
    # blank + an address renders exactly like the label) and vanishes in the skeleton.
    s = "".join(" " if _is_invisible(c) else c for c in s).strip()
    skeleton = "".join(c for c in s if not c.isspace())
    if skeleton in RESERVED_ACTORS:
        return True
    if not s.startswith(UNAUTHENTICATED_ACTOR):
        return False
    # "unauthenticated" followed by anything that is not part of an ordinary address
    # ("unauthenticated.person@example.com" stays a normal email) is the reserved label.
    nxt = s[len(UNAUTHENTICATED_ACTOR) :][:1]
    return not (nxt.isascii() and (nxt.isalnum() or nxt in "._-+@"))


EMAIL_MAX_LEN = 254


# Identities the app uses as synthetic/system actors in the audit trail; a real
# account with one of these emails would let a login impersonate the scheduler /
# system in the log, so creating one is rejected like a duplicate.
def validate_email(email) -> str:
    """Normalize + format-validate an email at the shared identity seam (BUG-A3-1).

    Returns the normalized (lower/stripped) address, or raises ValueError — surfaced
    identically on the web Users page, the CLI, and /api. This is a SECURITY + sanity
    gate, deliberately NOT an RFC-5322 validator. It rejects:

      * whitespace anywhere (a space/TAB/CR/LF is never valid in an address, and CR/LF
        is the header-injection vector A3 flagged);
      * any C0/C1/DEL control character (so an embedded NUL cannot enter a stored
        identity — isspace() misses NUL);
      * a zero-width, format or blank-rendering character (has_hidden_email_char), except
        ZWNJ / ZWJ in an IDNA2008 joining context;
      * an over-long address (> 254);
      * a MALFORMED "@" — an empty local or empty domain ("scheduler@", "@x.com") or a
        second "@".

    It deliberately TOLERATES a bare-word local with no "@" (e.g. an internal
    `local@host` without a dotted TLD, or a plain login name), because the app's own
    convention — the seeded `admin`/`linguist` accounts and many internal-login callers —
    uses non-dotted / no-"@" identities; demanding a dotted domain would reject
    established valid usage. Called only on the PUBLIC path (enforce_policy=True); the
    internal dev seeders skip it, exactly like the password policy."""
    norm = normalize_email(email)
    if not norm:
        raise ValueError("an email address is required.")
    if len(norm) > EMAIL_MAX_LEN:
        raise ValueError("email address is too long (max %d characters)." % EMAIL_MAX_LEN)  # noqa: UP031
    if any(c.isspace() for c in norm):
        raise ValueError("email address may not contain spaces or line breaks.")
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in norm):
        raise ValueError("email address contains invalid control characters.")
    # Gate audit-actor round 1 (LOW-1): zero-width / format characters and blank-rendering
    # letters (Hangul fillers, braille blank) make one address display as another, so an
    # identity carrying one could pass as someone else, or as a system actor, in the trail.
    # Round 2 (LOW-8): ZWNJ / ZWJ pass only in an IDNA2008 joining context (Persian, Indic).
    if has_hidden_email_char(norm):
        raise ValueError("email address contains invisible or formatting characters.")
    if "@" in norm:
        local, _, domain = norm.partition("@")
        if not local or not domain or "@" in domain:
            raise ValueError(f"{email!r} is not a valid email address.")
    if is_placeholder_email(norm):
        raise ValueError(PLACEHOLDER_EMAIL_MESSAGE)
    return norm


_SQLITE_INT64_MAX = 2**63 - 1


# SQLite's binder rejects an int outside signed-64-bit with a raw OverflowError, so
# a huge digit-string (typo / forged POST) must be coerced to None BEFORE any
# session.get — mirrors audit._as_id / customers._coerce_id.
def _coerce_customer_id(v):
    """'' / None / '0' / 0 / non-digit / out-of-range -> None (internal staff); a
    valid positive in-range id -> that int. ONE normalization so create_user +
    set_customer agree (0 is never a phantom binding) and neither overflows the
    SQLite binder on a huge id."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 0 < v <= _SQLITE_INT64_MAX else None
    # H2: str.isdigit() is True for Unicode digits int() rejects (e.g. the
    # superscript '2', U+00B2), which used to slip past the gate into int() and
    # raise ValueError -> 500 in create_user / set_customer. try/except closes it.
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n <= _SQLITE_INT64_MAX else None


def _validate_customer_binding(role: str, customer_id) -> None:
    """R2-3: enforce the customer-bound-user ROLE restriction at the shared seam —
    a customer-bound account may hold only translator/linguist/viewer, NEVER
    admin/manager (access.CUSTOMER_BINDABLE_ROLES). Raises ValueError (surfaced
    identically on the web Users page + the CLI) when a non-bindable role would be
    bound. `customer_id` here is the ALREADY-COERCED value (None = internal staff,
    always allowed). Does NOT check the customer EXISTS — that is the caller's job
    (it has the session); this seam owns only the role<->binding invariant so BOTH
    surfaces inherit it."""
    from app.services import access

    if customer_id is None:
        return
    if role not in access.CUSTOMER_BINDABLE_ROLES:
        raise ValueError(
            "A customer-bound account may only be a translator, linguist or "
            "viewer (not admin or manager)."
        )


class OrglessAccountError(ValueError):
    """#181: under an active multi-org gate an account (or an invitation that will mint
    one) must name an organization. A ValueError subclass, so every existing door's
    `except ValueError` still surfaces it cleanly; a door that has its own operator
    message catches this class first and says it in its own words."""


def refuse_orgless_account(
    org_id, *, allow_orgless_boot: bool = False, invite: bool = False
) -> None:
    """#181: THE one place "an account must belong to an organization" is enforced.

    create_user and create_invite call this near their start, so no surface can forget
    it. The value is judged AFTER stampable_org_id, so the BLOCKED_ORG_ID read-poison and
    the UNSCOPED operator sentinel count as "no org", exactly as the stamp would store
    them. A strict no-op with the gate off.

    `allow_orgless_boot` is the explicit, on-purpose exemption for the boot paths that run
    before any org context exists (seed_initial, ensure_linguist, seed_admin and the
    first-run setup wizard). It is keyword-only so nothing gets it by accident of
    argument order, and verify_multiorg_inv's call-site ratchet allowlists every caller
    that passes it."""
    if allow_orgless_boot:
        return
    from app.services import access

    if access.stampable_org_id(org_id) is None and access.multi_org_active():
        raise OrglessAccountError(
            "Multi-org is enabled, so "
            + ("an invitation" if invite else "a new account")
            + " must belong to an organization."
        )


class FirstAccountExists(Exception):
    """create_user(first_account_only=True) found an account already present (#414):
    the install was claimed first, by another setup request or by `init`."""


def create_user(
    email,
    name,
    role,
    password,
    auth_source="local",
    enforce_policy: bool = True,
    customer_id=None,
    org_id=None,
    *,
    allow_orgless_boot: bool = False,
    first_account_only: bool = False,
) -> User:
    """Create a user. Raises ValueError if the (normalized) email is reserved for
    a system actor, already belongs to an account, or (for a local password) does
    not meet the MIN_PASSWORD_LEN policy — all surface to the admin Users page /
    CLI the same way.

    The password policy is enforced HERE (the shared seam) so BOTH public callers
    — the CLI `user create --password` and the web admin create-user form — are
    covered (#4; previously only admin_set_password checked it). `enforce_policy`
    is set False ONLY by the internal dev seeders (seed_initial/ensure_linguist),
    which mint the short-password admin/linguist/user1 dev accounts on a fresh
    install; every public caller keeps the default (True). A blank password (an
    SSO/LDAP-provisioned account with no local password) is never policy-checked.

    `customer_id` (R2-3) binds the account to one customer at creation. The
    role<->binding invariant is enforced here (a bound account may only be
    translator/linguist/viewer), so both the web create-user form and CLI
    `user create --customer` inherit the guard. NULL = internal staff (default).

    `org_id` (multi-org #61) STAMPS the new account into an organization. The CLI
    `user create` passes its resolved --org scope (op_org_id) so a merely-scoped,
    non-operator caller cannot mint a user with org_id=None — which access.py treats
    as a SEE-ALL super-scope on the read surfaces (browsable_customers / llmemory_view
    / org_id_clause), re-opening the cross-org read leak. None (operator / single-
    tenant / gate off) keeps today's global account, byte-identical.

    SPEC-customer-archive (fix 2): a NEW binding to an ARCHIVED customer is
    refused here too, mirroring content.project_create's ArchivedCustomerError
    seam — archive retires a customer from every "choose a customer" surface,
    including the create-user binding, not just the picker. Existence of the
    id is still the caller's job (see the note above); only an EXISTING-but-
    archived customer is rejected. DOC (reviewed + accepted edge): once a
    customer is archived, a user already bound to it keeps logging in, keeps
    seeing their existing project list, and keeps PT visibility — archived
    only blocks NEW associations (new users bound to it, new projects/PTs
    created for it), never existing ones.

    #181: under an active multi-org gate an org-less create is refused here
    (OrglessAccountError) unless the caller is a boot path that passes
    `allow_orgless_boot=True` on purpose. See refuse_orgless_account.

    `first_account_only` (the /auth/setup wizard and seed_admin) makes "create the first
    account" one atomic step: the install-wide user-mutation lock (SQLite writer
    reservation, PostgreSQL advisory lock) is taken first, the "no account yet" check runs
    under it in the same transaction as the insert, and FirstAccountExists is raised when
    any account is already there. Without it, concurrent setup posts each passed the
    separate has_users() probe and each created an admin."""
    refuse_orgless_account(org_id, allow_orgless_boot=allow_orgless_boot)
    norm = normalize_email(email)
    if is_reserved_actor(norm):
        raise ValueError(f"{norm!r} is a reserved system identity.")
    # #520 gate: refused even for the internal seeders (enforce_policy=False): no path may
    # mint an account whose address is an app-written placeholder.
    if is_placeholder_email(norm):
        raise ValueError(PLACEHOLDER_EMAIL_MESSAGE)
    # BUG-A3-1: format-validate the email on the public path (control chars incl.
    # CR/LF/NUL, length, plausible local@domain). Skipped for the internal dev seeders
    # (enforce_policy=False), which mint synthetic non-email logins. After the reserved
    # + before the duplicate check so those keep their specific messages.
    if enforce_policy:
        validate_email(norm)
    # First-account callers check for ANY account under the database lock below.
    # A separate duplicate probe could see a concurrent winner here and raise
    # ValueError instead of FirstAccountExists, breaking setup/init idempotency.
    if not first_account_only:
        if get_by_email(norm):
            raise ValueError(f"A user with email {norm!r} already exists.")
        # Gate email-intl round 1 (MEDIUM-1): nor may it differ only by a joiner.
        refuse_lookalike_email(norm)
    if enforce_policy and password:
        issue = password_issue(password, user=norm)
        if issue:
            raise ValueError(issue)
    # R2-3: normalize the binding (0/blank/huge -> None) then reject an admin/
    # manager binding BEFORE creating anything.
    cid = _coerce_customer_id(customer_id)
    _validate_customer_binding(role, cid)
    # #73: callers pass a PRINCIPAL-derived org (SCIM's key org, the /api caller's
    # org, the CLI --org scope). access.current_org_id now poisons a NULL-org
    # principal to BLOCKED_ORG_ID so reads fail closed; a stamp must never persist
    # that sentinel, so it collapses back to None (a global account, exactly as
    # before #73). A real id is untouched.
    from app.services import access as _access

    org_id = _access.stampable_org_id(org_id)
    with Session(_engine()) as s:
        if first_account_only:
            lock_user_mutations(s)
            if s.exec(select(User.id).limit(1)).first() is not None:
                raise FirstAccountExists("an account already exists")
        if cid is not None:
            from app.models import Customer

            cust = s.get(Customer, cid)
            # A dangling/nonexistent id is left to the caller (unchanged); only an
            # EXISTING-but-archived customer is refused here.
            if cust is not None and getattr(cust, "archived_at", None) is not None:
                raise ValueError(
                    f"customer {cid} is archived — cannot bind a new user to it. "
                    "Unarchive it, or pick an active customer."
                )
        u = User(
            email=norm,
            name=name or "",
            role=role,
            password_hash="",
            auth_source=auth_source,
            customer_id=cid,
            org_id=org_id,
        )
        s.add(u)
        if password:
            from app.services import password_policy

            # The one local-password writer: hash + max-age clock + reuse history.
            password_policy.record_password(s, u, password)
        s.commit()
        s.refresh(u)
        return u


def auth_source_of(user) -> str:
    """`user.auth_source` normalized ONCE for every sign-in decision: stripped, lowercased,
    and blank read as "local" (a legacy row, as mfa.applies_to reads it). Gate sso-localpw
    round 1, LOW-1: the readers used to disagree, so a hand-edited "Local" row lost sign-in."""
    return (str(getattr(user, "auth_source", "") or "").strip().lower()) or "local"


#: The one sentence for the trust-host-auth principal (and any trust row): it never holds a
#: local password, and switching it to local would stop the next embedded boot
#: (trust_auth.configure_at_boot refuses a principal whose row is not "trust").
TRUST_IDENTITY_REFUSAL = (
    "The host service identity has no password; it signs in through the host application."
)


def signs_in_locally(user) -> bool:
    """True when `user`'s sign-in method is a password stored in this app (auth_source
    "local", normalized by auth_source_of). SSO, SAML, SCIM, LDAP and trust accounts sign
    in through their identity provider, directory or host, so a local password is never
    their credential (train sso-localpw)."""
    return bool(user) and auth_source_of(user) == "local"


def password_change_refusal(user) -> str | None:
    """The one plain sentence refusing a SELF-SERVICE password change, or None when the
    account may change its password here (a local account that has a local password).

    An SSO/SAML/SCIM account changing "its" password here used to skip the current-password
    check (it has no hash) and write a brand-new local credential that `authenticate` then
    accepted: a person disabled at the identity provider kept a working login, and the
    provider's MFA and password rules were sidestepped. An LDAP account types its DIRECTORY
    password into this app; a local change would plant a second, local credential beside it,
    so it is refused the same way (the directory owns that password)."""
    if not user:
        return "User not found"
    source = auth_source_of(user)
    if source == "trust":
        return TRUST_IDENTITY_REFUSAL
    if source == "ldap":
        return (
            "Your account signs in with your organization's directory password; change it "
            "through your organization's directory."
        )
    if source != "local" or signs_in_through_idp(user):
        return (
            "Your account signs in through single sign-on; change your password with your "
            "organization's sign-in provider."
        )
    if not getattr(user, "password_hash", ""):
        return (
            "This account has no password on this site. Ask an administrator to send you "
            "an invitation or reset your password."
        )
    return None


def signs_in_through_idp(user) -> bool:
    """True when every signal this app has says `user` signs in through an identity
    provider: a non-local auth_source (SSO, SAML, SCIM, LDAP, trust), OR a SCIM link
    (`external_id`) on a row with no local password. The second is a local row the IdP
    adopted by SCIM (adoption keeps auth_source) that then signs in through SAML (which keeps
    it too): "local" by column, IdP-only in fact (gate sso-localpw round 2, MEDIUM-1). A
    SCIM-adopted local row WITH a password still signs in locally by design."""
    if not user:
        return False
    if auth_source_of(user) != "local":
        return True
    return bool(getattr(user, "external_id", None)) and not getattr(user, "password_hash", "")


_IDP_INVITE_REFUSAL = (
    "This account signs in through your organization's single sign-on or directory, "
    "so an invitation cannot set a password for it. Sign in with your organization's "
    "sign-in page, or ask an administrator."
)


_INVITE_ORG_NOT_CHECKED = object()
_CROSS_ORG_INVITE_REFUSAL = (
    "An invitation cannot be issued for this address. Contact an administrator."
)


def invite_refusal(row, invite_org_id=_INVITE_ORG_NOT_CHECKED) -> str | None:
    """Why an invitation may not (re)password and activate the EXISTING account `row`, or
    None when it may. create_invite and accept_invite both ask (an invitation issued before
    the account joined the IdP is refused at accept). Allowed: a LOCAL account that is
    inactive with no password and no SCIM link (the row an admin pre-creates and
    then invites; activation is the point of that invitation). Refused:

      * a trust service identity (TRUST_IDENTITY_REFUSAL);
      * any account that signs in through an identity provider (signs_in_through_idp: a
        non-local auth_source, or a SCIM-linked row with no local password): an invitation
        must not give it a second, local way in;
      * an inactive account that has a password or a SCIM link. The app records a
        deactivation only as `active = False`, so an admin disable and a SCIM deactivation
        cannot be told apart; neither may be undone by a link that may predate it. An
        admin re-enables the account on the Users page instead.

    #536: under multi-org an invitation never reaches an account that belongs to ANOTHER
    organization (or to none), whatever its state: an org-A admin must not be able to set
    the password of, and activate, the inactive passwordless row org B pre-created. The
    refusal is the same non-enumerating sentence the active-account case already uses.
    `invite_org_id` is the org the invitation is stamped with; callers that cannot name one
    (the default) skip this check. Strict no-op while the multi-org gate is off."""
    if invite_org_id is not _INVITE_ORG_NOT_CHECKED:
        from app.services import access

        if access.multi_org_active() and getattr(row, "org_id", None) != invite_org_id:
            return _CROSS_ORG_INVITE_REFUSAL
    if auth_source_of(row) == "trust":
        return TRUST_IDENTITY_REFUSAL
    if signs_in_through_idp(row):
        return _IDP_INVITE_REFUSAL
    if row.active:
        from app.services import access

        if access.multi_org_active():
            return "An invitation cannot be issued for this address. Contact an administrator."
        return "This account is already active. Use Reset password on the Users page instead."
    if not row.active and (row.password_hash or getattr(row, "external_id", None)):
        return (
            "This account has been deactivated, so an invitation cannot turn it back on. "
            "Ask an administrator."
        )
    return None


def _invite_refusal_detail(row) -> str:
    """The audit detail of a refused invitation: the sign-in method, and why."""
    parts = [f"refused: {auth_source_of(row)} account"]
    if getattr(row, "external_id", None):
        parts.append("SCIM-linked")
    if not row.active:
        parts.append("inactive")
    return ", ".join(parts)


def _switch_to_local(row: User) -> str:
    """Make `row` a LOCAL account on purpose (an admin reset, with a named admin actor): the
    admin just issued it a local password, so its sign-in method becomes that password.
    Returns the previous (normalized) auth_source when the sign-in method changed, else "".
    A non-canonical spelling of local ("Local", " local") is rewritten to "local" without an
    audit row, since the method did not change. The caller commits, then writes the audit
    row with _audit_switch."""
    previous = auth_source_of(row)
    if row.auth_source != "local":
        row.auth_source = "local"
    return "" if previous == "local" else previous


def _audit_switch(email: str, previous: str, actor: str, why: str) -> None:
    if previous:
        audit.log(
            actor=actor or email,
            action="user.auth_source_changed",
            target=email,
            detail=f"{previous} -> local ({why})",
        )


def authenticate(email: str, password: str):
    """The user for a correct local password, else None.

    Constant work (timing leak fix): an unknown email, a disabled account and an account
    with no local password (SSO/LDAP) all run one dummy Argon2 verify, so the time taken
    no longer tells a caller whether the address has an account. A correct password
    proves ONLY the first factor: the web sign-in (app/web/auth_mfa.py) decides whether a
    second step follows before any session exists.

    Train sso-localpw: only a LOCAL account signs in with a local password. A hash on an
    SSO/SAML/SCIM/LDAP/trust row (left by the old self-service bug, or by a pre-F5 IdP
    adoption of a password account) is ignored, so an identity-provider account can never
    bypass the provider here. Only an admin reset switches such an account to local, on
    purpose and audited, so that path still signs in; an invitation never does (see
    invite_refusal)."""
    u = get_by_email(email)
    if not (u and u.active and u.password_hash and signs_in_locally(u)):
        dummy_verify(password)
        return None
    if verify_password(u.password_hash, password):
        # Multi-org P7: a SUSPENDED org's users cannot log in. A strict no-op with
        # the sacred gate off (org_active -> True), so single-tenant is unchanged.
        from app.services import access

        if not access.user_org_active(u):
            return None
        with Session(_engine()) as s:
            row = s.get(User, u.id)
            row.last_login = datetime.utcnow()
            s.add(row)
            s.commit()
        return u
    return None


class PasswordChangeResult(tuple):
    """`(ok, msg)` for every existing caller, plus the epoch the revocation produced.

    A tuple subclass, so `ok, msg = change_password(...)` keeps working everywhere while
    `.session_epoch` carries the one extra fact the web seam needs: the EXACT generation
    this change committed. The seam must stamp THAT value into the session it is serving
    and never re-read the column afterwards, or a revocation committed in between (an
    admin reset, another device's logout) would be read and handed to the very session
    that revocation killed. `.session_epoch` is 0 on failure (nothing was revoked)."""

    # No __slots__: a variable-length tuple subtype cannot have one.
    def __new__(cls, ok: bool, msg: str, session_epoch: int = 0):
        self = super().__new__(cls, (bool(ok), str(msg)))
        self.session_epoch = int(session_epoch or 0)
        return self


# ONE atomic increment, never a read-modify-write. Two revocations that overlap must
# BOTH count: if each read N in Python and wrote back N+1, the second logout / reset
# would be a silent no-op and a cookie stamped N+1 between them would survive it.
# COALESCE keeps a legacy NULL (a row written before the column existed) at 0.
_BUMP_SESSION_EPOCH_SQL = text(
    'UPDATE "user" SET session_epoch = COALESCE(session_epoch, 0) + 1 WHERE id = :uid'
)
_READ_SESSION_EPOCH_SQL = text('SELECT session_epoch FROM "user" WHERE id = :uid')


def bump_session_epoch(user_id: int, session: Session | None = None) -> int:
    """Invalidate EVERY outstanding session cookie for one user (#138). THE one seam.

    The web session is a stateless signed cookie, so there is nothing server-side to
    delete: instead each user carries a generation counter, every session records the
    epoch it was minted under (`session["se"]`), and `security.session_user` refuses a
    session whose epoch does not match. Raising the counter therefore kills every
    cookie minted before this moment, on every device, at once — no session table and
    no blacklist. The ONLY write on this path; ordinary requests never touch it.

    The increment is a SINGLE atomic SQL statement and the resulting value is read back
    inside the SAME transaction, so the number returned is exactly the generation this
    call produced even when revocations overlap. Every revoking path (logout,
    change_password, admin_set_password) goes through here: there is no second copy of
    this arithmetic to drift.

    Pass `session` to enlist in a caller's open transaction — that is how a password
    change lands the new hash and the revocation together or not at all. The caller
    then owns the commit. Without it, the bump opens and commits its own transaction.

    Returns the new epoch, or 0 when the user is gone (already a dead session).
    """
    if session is not None:
        return _bump_session_epoch_in(session, user_id)
    with Session(_engine()) as s:
        new_epoch = _bump_session_epoch_in(s, user_id)
        s.commit()
        return new_epoch


def _bump_session_epoch_in(session: Session, user_id: int) -> int:
    uid = int(user_id or 0)
    if not uid:
        return 0
    # Land any pending ORM work (e.g. the new password hash) BEFORE the raw UPDATE, so
    # the two writes cannot be reordered inside a later flush.
    session.flush()
    if not session.execute(_BUMP_SESSION_EPOCH_SQL, {"uid": uid}).rowcount:
        return 0
    row = session.execute(_READ_SESSION_EPOCH_SQL, {"uid": uid}).first()
    # The raw UPDATE went around the identity map: force any loaded copy to re-read
    # rather than serve (or write back) a value this statement has just superseded.
    session.expire_all()
    return int(row[0]) if row and row[0] is not None else 0


def login(request, user) -> None:
    # Reset the session across the privilege boundary (fixation hygiene): no
    # pre-auth state — stale oidc_* keys, anything a pre-login page stashed —
    # may survive into the authenticated session.
    request.session.clear()
    request.session["uid"] = user.id
    # #138: stamp the generation this session is minted under. Every login seam
    # (local, LDAP, SSO/OIDC, SAML) comes through here, so all of them are covered.
    request.session["se"] = int(getattr(user, "session_epoch", 0) or 0)
    # a11y T9: `iat` (issued-at) is written HERE and nowhere else, so the absolute
    # session limit counts from sign-in and no later request can renew it.
    now = _now()
    request.session["la"] = now
    request.session["iat"] = now


def logout(request) -> None:
    """Sign out: revoke the account's generation, THEN clear the client copy.

    The order is the fail-safe. If the revocation does not report success this RAISES
    and the client session is left INTACT. Whether it committed is UNKNOWABLE from here
    (a write can land and then fail to be acknowledged), so the direction is chosen on
    which unknown is safer to show: never clear the browser's cookie for a revocation we
    cannot vouch for, because that looks like a logout while the captured copy may still
    be live everywhere else. The web seam turns the raise into a clean error.
    """
    uid = request.session.get("uid")
    if uid:
        bump_session_epoch(uid)
    request.session.clear()


# --- RBAC predicates ---
def _rank(role) -> int:
    return Role.RANK.get(role, 0)


def is_admin(u) -> bool:
    return bool(u) and u.role == Role.ADMIN


def can_write(u) -> bool:
    return bool(u) and _rank(u.role) >= Role.RANK[Role.TRANSLATOR]


def can_manage(u) -> bool:
    return bool(u) and _rank(u.role) >= Role.RANK[Role.MANAGER]


def can_tune_ai(u) -> bool:
    """Who may edit AI setup, prompts, models, TM/glossary tuning — the linguist's
    home turf — in addition to admins. NOT tied to the privilege ladder."""
    return bool(u) and u.role in (Role.ADMIN, Role.LINGUIST)


def can_configure_customer(u) -> bool:
    """Who may configure a customer: preferences/settings, scoped defaults, context
    cards (save/delete) and active language pairs (add/remove/import). Admin, manager
    or linguist (2026-09-26: managers keep customer configuration, linguists
    keep it as AI/language tuning). Translators, reviewers and viewers are refused
    (#284). The ONE predicate every customer-config door uses, web and /api alike."""
    return can_manage(u) or can_tune_ai(u)


def can_review(u) -> bool:
    """Who may clear a TM lock — the EXPLICIT, audited unlock — plus other reviewer-
    gated actions: reviewer RANK or higher (reviewer/manager/admin, and linguist,
    which shares reviewer's RANK 2 by design — see models.Role). A translator (rank 1)
    or viewer (rank 0) is refused. This is a STRICTLY HIGHER bar than can_write (>=
    translator): editing a segment is a write, but overwriting/clearing a hard TM lock
    is reserved for a human reviewer+ (the recorded policy — models.Segment lock_hard
    note; services.tm_lock.unlock_segment). Both the web and /api unlock routes enforce
    this same predicate so the surfaces refuse identically."""
    return bool(u) and _rank(u.role) >= Role.RANK[Role.REVIEWER]


# --- seeding (real deployments only; see main._startup) ---
def enable_login_wall() -> None:
    """Turn the login wall on WITHOUT seeding any accounts. Used by container /
    declared-server installs (config.IS_SERVER): the first visitor is redirected
    to /auth/setup to create the admin (see the security middleware, which routes
    there while user_count() == 0). Idempotent."""
    settings.set_many({"security_auth_required": "1"})


# --- admin user administration (shared by the Users page + the CLI) ---
# Each returns a small dict the caller uses to build its own audit line and to
# know whether anything changed. The route keeps ownership of the audit call
# (so it can attach ip=) and of any self-lockout policy — these functions are
# the pure state transition, matching what the Users page did inline before.
def is_last_active_admin(session: Session, uid: int, org_id=None) -> bool:
    """True when `uid` is an active admin AND no OTHER active admin exists — so
    disabling or demoting it would leave the tenancy with ZERO administrators.
    SCIM uses this explicit-scope predicate under lock_user_mutations; other
    mutation seams use is_last_admin_of_own_org under the same transaction lock.
    Calling this predicate alone does not serialize a later mutation.

    `org_id` (adversarial review): an /api key is ALWAYS org-pinned, so an org-A admin
    disabling org-A's last admin must be refused even when ANOTHER org still has an
    admin (org A would otherwise self-lock its own administration). When org_id is
    given the "other admins?" count is confined to that org; None (the local CLI /
    unbound operator, single-tenant) keeps the historic INSTALL-GLOBAL count so
    gdpr.py + cmd_user.py's existing callers are byte-unchanged."""
    u = session.get(User, uid)
    if not u or not u.active or u.role != Role.ADMIN:
        return False
    stmt = select(User).where(
        User.role == Role.ADMIN,
        User.active == True,  # noqa: E712
        User.id != uid,
    )
    if org_id is not None:
        stmt = stmt.where(User.org_id == org_id)
    return not session.exec(stmt).all()


def is_last_admin_of_own_org(session: Session, uid: int) -> bool:
    """#178-1: the last-admin invariant judged against the TARGET's own org, resolved
    here rather than handed in by the caller. True when `uid` is an active admin and no
    other active admin shares its org, so demoting, disabling or erasing it would leave
    that org with zero administrators.

    The caller-supplied scope of is_last_active_admin was the hole: the local CLI passes
    no org (or the UNSCOPED operator sentinel), which counted admins install-wide, so org
    A's last admin could be disabled because org B still had one. With the gate off the
    install is the tenant and the count stays install-global, byte-identical to before.
    set_role and set_active call this, so every surface inherits it; gdpr.erase_user uses
    it for its in-transaction recheck. This predicate alone does not lock: mutation
    callers must acquire lock_user_mutations BEFORE reading the target and hold it
    through the commit."""
    u = session.get(User, uid)
    if not u or not u.active or u.role != Role.ADMIN:
        return False
    stmt = select(User.id).where(
        User.role == Role.ADMIN,
        User.active == True,  # noqa: E712
        User.id != uid,
    )
    from app.services import access

    if access.multi_org_active():
        stmt = stmt.where(User.org_id.is_(None) if u.org_id is None else User.org_id == u.org_id)
    return session.exec(stmt).first() is None


def _scoped_user(session: Session, uid: int, org_id):
    """Resolve a target within the required caller scope; missing scope fails closed."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    stmt = select(User).where(User.id == uid)
    if org_id is not None:
        stmt = stmt.where(User.org_id == org_id)
    return session.exec(stmt.execution_options(populate_existing=True)).first()


def set_role(session: Session, uid: int, role: str, actor: str = "", *, org_id):
    """Set a user's role. No-op (ok=False) when the user is missing or the role
    is not a known Role. Returns {"ok", "email", "old", "new"}. Mirrors the old
    users_role handler exactly (guard `u and role in Role.ALL`).

    R2-3: a CUSTOMER-BOUND user may not be promoted to a non-bindable role
    (admin/manager) — that would be an isolation breach (a customer account with
    admin power). Rejected (ok=False, reason="role_bound") so the caller surfaces
    a clear error instead of silently creating a privileged customer account. To
    make such a user an admin, clear their customer binding first."""
    lock_user_mutations(session)
    u = _scoped_user(session, uid, org_id)
    if not u or role not in Role.ALL:
        result = {
            "ok": False,
            "email": getattr(u, "email", None),
            "old": getattr(u, "role", None),
            "new": role,
        }
        session.rollback()
        return result
    # #178-1: never demote the last active admin of the account's own org. Enforced here
    # so the CLI, the Users page and /api cannot disagree about which org is counted.
    if role != Role.ADMIN and is_last_admin_of_own_org(session, u.id):
        result = {"ok": False, "email": u.email, "old": u.role, "new": role, "reason": "last_admin"}
        session.rollback()
        return result
    # A bound user promoted to admin/manager would break customer isolation.
    if getattr(u, "customer_id", None) is not None:
        from app.services import access

        if role not in access.CUSTOMER_BINDABLE_ROLES:
            result = {
                "ok": False,
                "email": u.email,
                "old": u.role,
                "new": role,
                "reason": "role_bound",
            }
            session.rollback()
            return result
    old_role = u.role
    u.role = role
    session.add(u)
    session.commit()
    return {"ok": True, "email": u.email, "old": old_role, "new": role}


def set_customer(session: Session, uid: int, customer_id, actor: str = "", *, org_id):
    """Bind or clear a user's customer under the install-wide user-mutation lock (#546).

    Without the lock this raced `set_role`: one request read an admin-to-be as bindable
    while the other promoted it, leaving a customer-bound admin. The lock is taken BEFORE
    the target is read (same order as `set_role`); every refusal rolls back so it is
    released, and a success commits inside `_set_customer_locked`."""
    lock_user_mutations(session)
    result = _set_customer_locked(session, uid, customer_id, actor, org_id=org_id)
    if not result.get("ok"):
        session.rollback()
    return result


def _set_customer_locked(session: Session, uid: int, customer_id, actor: str = "", *, org_id):
    """R2-3: bind (or clear, when customer_id is ''/None) a user to a customer —
    the "customer account" concept. Returns {"ok", "email", "old", "new"} (new is
    the resolved customer_id or None).

    Guards (fail closed, ok=False + a reason):
      - user missing                         -> reason="not_found"
      - a non-existent customer id           -> reason="no_customer"
      - customer is ARCHIVED (SPEC-customer-archive, fix 2) -> reason="archived_customer"
      - binding an admin/manager role        -> reason="role_bound"
    Clearing (blank) is always allowed and returns the user to internal staff.
    The customer's ORG match (multi-org) is the CALLER's responsibility (it has the
    org context); this seam owns existence + the role invariant so BOTH the web
    Users page and CLI inherit them identically.

    SPEC-customer-archive (fix 2): a NEW binding (a non-blank customer_id here) to
    an ARCHIVED customer is refused — archive retires a customer from every
    "choose a customer" surface, so a forged same-org POST can't bind a user to
    one just because the picker already filters it out. This only gates NEW
    bindings/changes: clearing (customer_id None/blank) a user OUT of an archived
    customer is always allowed (never blocked here), and a user already bound to a
    customer that gets archived later keeps that binding — and everything it
    grants (login, project list, PT visibility) — untouched; they just can't be
    freshly bound (here) or create new projects/PTs for it (content.project_create)
    while it stays archived."""
    from app.models import Customer

    u = _scoped_user(session, uid, org_id)
    if not u:
        return {"ok": False, "email": None, "old": None, "new": None, "reason": "not_found"}
    old = u.customer_id
    # A non-blank value that isn't a valid positive in-range id is a bad ref:
    # distinguish "clear" (blank / '0' / None) from "garbage id" so a typo doesn't
    # silently clear the binding. Coercion also guards the SQLite binder overflow.
    raw = "" if customer_id is None else str(customer_id).strip()
    new_cid = _coerce_customer_id(customer_id)
    if new_cid is None and raw not in ("", "0"):
        # a non-empty, non-'0' value that failed to coerce = a bad customer ref.
        return {"ok": False, "email": u.email, "old": old, "new": old, "reason": "no_customer"}
    if new_cid is not None:
        # The customer must exist (a dangling binding would confine the user to a
        # phantom customer = they'd see NOTHING, a silent footgun — reject instead).
        cust = session.get(Customer, new_cid)
        if cust is None:
            return {"ok": False, "email": u.email, "old": old, "new": old, "reason": "no_customer"}
        # SPEC-customer-archive (fix 2): a NEW binding to an ARCHIVED customer is
        # refused here too — only reached when new_cid is non-None (an actual
        # binding attempt), never on clear/unbind, so unbinding FROM an archived
        # customer is unaffected.
        if getattr(cust, "archived_at", None) is not None:
            return {
                "ok": False,
                "email": u.email,
                "old": old,
                "new": old,
                "reason": "archived_customer",
            }
        # A bound account may only be translator/linguist/viewer.
        from app.services import access

        if u.role not in access.CUSTOMER_BINDABLE_ROLES:
            return {"ok": False, "email": u.email, "old": old, "new": old, "reason": "role_bound"}
    u.customer_id = new_cid
    session.add(u)
    session.commit()
    return {"ok": True, "email": u.email, "old": old, "new": new_cid}


def set_department(session: Session, uid: int, department: str, actor: str = "", *, org_id):
    """Set a user's department, stored lower-cased+stripped so it is the ONE
    canonical form (matching, inbox identity, budget keys all compare
    case-insensitively — Finding 6). No-op (ok=False) when the user is missing.
    Returns {"ok", "email", "old", "new"}."""
    u = _scoped_user(session, uid, org_id)
    if not u:
        return {"ok": False, "email": None, "old": None, "new": None}
    old = u.department or ""
    u.department = (department or "").strip().lower()
    session.add(u)
    session.commit()
    return {"ok": True, "email": u.email, "old": old, "new": u.department}


def set_active(session: Session, uid: int, active: bool, actor: str = "", *, org_id):
    """Set a user's active flag. No-op (ok=False) when the user is missing.
    Returns {"ok", "email", "active", "keys_revoked"}. The Users page toggles (active =
    not active); callers that toggle pass the already-flipped value.

    #422 (identity-03): deactivating ENDS the account's credentials instead of suspending
    them. In the same transaction as the flag, the session epoch is raised (every session
    and known-device cookie minted before is dead for good) and every active API key is
    revoked, so re-enabling starts clean: the person signs in again and creates new keys."""
    lock_user_mutations(session)
    u = _scoped_user(session, uid, org_id)
    if not u:
        session.rollback()
        return {"ok": False, "email": None, "active": None, "keys_revoked": 0}
    # #178-1: never disable the last active admin of the account's own org.
    if not active and is_last_admin_of_own_org(session, u.id):
        result = {
            "ok": False,
            "email": u.email,
            "active": u.active,
            "reason": "last_admin",
            "keys_revoked": 0,
        }
        session.rollback()
        return result
    u.active = bool(active)
    session.add(u)
    scim_tokens = scim_token_prefixes(session, u.id) if not active else []
    keys_revoked = end_credentials(session, u.id, org_id=org_id) if not active else 0
    session.commit()
    email = u.email
    audit_scim_tokens_ended(actor, email, scim_tokens)
    return {
        "ok": True,
        "email": email,
        "active": u.active,
        "keys_revoked": keys_revoked,
        "scim_tokens_revoked": scim_tokens,
    }


def end_credentials(session: Session, uid: int, *, org_id) -> int:
    """#422: end every credential `uid` holds, inside the caller's transaction (the caller
    commits): raise the session epoch and revoke every active API key. Returns the number of
    keys revoked. The one seam for every deactivation path (set_active, SCIM PUT/PATCH)."""
    from app.services import apikeys

    session.flush()
    bump_session_epoch(uid, session=session)
    return apikeys.revoke_all_for_user(uid, org_id=org_id, session=session)


#: #422 gate: what an operator is told when deactivating an account revokes a SCIM token.
SCIM_SYNC_STOPS_WARNING = (
    "This account owns a SCIM token. Disabling it revokes that token for good, so your "
    "identity provider's user sync stops until an administrator creates a new SCIM token "
    "for another active administrator and enters it in the identity provider."
)


def scim_token_prefixes(session: Session, uid: int) -> list[str]:
    """#422 gate: the display prefixes of `uid`'s active SCIM-scoped API keys (the keys
    an identity provider authenticates with). Read BEFORE end_credentials revokes them."""
    from app.models import ApiKey

    rows = session.exec(
        select(ApiKey).where(ApiKey.user_id == uid, ApiKey.active == True)  # noqa: E712
    ).all()
    return sorted(k.prefix for k in rows if is_scim_key(k))


def is_scim_key(key) -> bool:
    """#422 gate: True when an API key carries the SCIM grant (an identity provider's key)."""
    from app.services import apikeys

    return "scim" in apikeys.scope_set(getattr(key, "scopes", "") or "")


def audit_scim_tokens_ended(actor: str, email: str, prefixes: list[str]) -> None:
    """#422 gate: one audit row per SCIM token a deactivation revoked, written AFTER the
    caller's commit (a separate writer). SCIM provisioning with that token stops, so the
    row says what to do."""
    for prefix in prefixes:
        audit.log(
            actor=actor or "system",
            action="scim.token_revoked",
            target=email,
            detail=(
                f"SCIM token {prefix} revoked because its owner was deactivated; identity "
                "provider sync with it stops. Create a new SCIM token for another active "
                "administrator and enter it in the identity provider."
            ),
        )


def admin_set_password(session: Session, uid: int, new_password: str, actor: str = ""):
    """Headless / admin password RESET — sets a new password WITHOUT knowing the
    current one (change_password requires the current password, which an admin
    doing a reset does not have). Enforces the password policy (password_issue, with
    this account's reuse history) and marks the account "change at next sign-in", so
    the person replaces the admin-chosen password before doing anything else. Never
    logs or returns the password. Returns (ok, msg): (False, reason) when the
    user is missing or the policy is not met, else (True, "Password updated")."""
    u = session.get(User, uid)
    if not u:
        return PasswordChangeResult(False, "User not found")
    # Gate sso-localpw round 1, MEDIUM-2: the trust-host-auth principal is a service
    # identity. A local password would be a second way in, and the switch to local below
    # would make the next embedded boot refuse to start (trust_auth.configure_at_boot).
    if auth_source_of(u) == "trust":
        audit.log(
            actor=actor or "system",
            action="user.password_reset",
            target=u.email,
            detail="refused: trust service identity",
        )
        return PasswordChangeResult(False, TRUST_IDENTITY_REFUSAL)
    # admin_reset: no current-password / reuse check against the target (an oracle for
    # the admin); must_change below forces the person to pick a fresh password anyway.
    problem = password_issue(new_password, user=u, admin_reset=True)
    if problem:
        return PasswordChangeResult(False, problem)
    from app.services import password_policy

    password_policy.record_password(session, u, new_password, must_change=True)
    # Train sso-localpw: authenticate accepts a local password only from a LOCAL account,
    # so an admin reset of an SSO/SAML/SCIM/LDAP account switches it to local ON PURPOSE
    # (audited below). This keeps the reset's old outcome (the person can sign in with the
    # admin-set password) and makes the must-change rule apply to it; the identity provider
    # no longer signs this account in (the F5 rule refuses adopting a local password
    # account), so SCIM deactivation (`active`) is the lifecycle control that remains.
    switched_from = _switch_to_local(u)
    email = u.email
    # #138: an admin RESET is a password change too - kill every outstanding session for
    # the account (that is usually the whole point of the reset). The SAME atomic bump,
    # in THIS transaction, so the new hash and the revocation land together or not at
    # all. NOTE: an admin who resets their OWN password signs themselves out with it.
    new_epoch = bump_session_epoch(uid, session=session)
    session.commit()
    _audit_switch(email, switched_from, actor, "admin password reset")
    from app.services import login_throttle

    login_throttle.clear(email, actor=actor or "system")
    return PasswordChangeResult(True, "Password updated", new_epoch)


# --- self-service account ---
def update_name(user_id: int, name: str) -> None:
    with Session(_engine()) as s:
        u = s.get(User, user_id)
        if u:
            u.name = name
            s.add(u)
            s.commit()


def update_locale(user_id: int, locale: str) -> None:
    """Persist a signed-in user's preferred UI language."""
    with Session(_engine()) as s:
        u = s.get(User, user_id)
        if u:
            u.ui_locale = locale
            s.add(u)
            s.commit()


#: Appearance choices (ADR 0063): light, dark, or follow the operating system. Stored on
#: User.ui_theme as "light" / "dark"; "" (the default) means "system". One validation seam
#: for the web form, POST /api/account/theme and `account set-theme`.
UI_THEMES = ("light", "dark", "system")


def normalize_theme(value) -> str:
    """Resolve a stored or submitted value to light / dark / system ("" and unknown -> system)."""
    v = (value or "").strip().lower() if isinstance(value, str) else ""
    return v if v in ("light", "dark") else "system"


def update_theme(user_id: int, theme: str) -> str:
    """Persist a signed-in user's appearance choice. The caller validates against UI_THEMES;
    "system" is stored as "". Returns the normalized choice."""
    choice = normalize_theme(theme)
    with Session(_engine()) as s:
        u = s.get(User, user_id)
        if u:
            u.ui_theme = "" if choice == "system" else choice
            s.add(u)
            s.commit()
    return choice


_reauth_lock = threading.Lock()
_reauth_accounts = weakref.WeakValueDictionary()
REAUTH_LOCKED = "Too many attempts. Wait 15 minutes, then sign in again."


def _reauth_account_lock(user_id: int):
    # Retain a strong reference while waiting/working; idle account locks disappear.
    with _reauth_lock:
        lock = _reauth_accounts.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _reauth_accounts[user_id] = lock
        return lock


def check_current_password(
    user_id: int,
    current: str,
    *,
    ip: str = "",
    allow_ldap: bool = False,
    clear_on_success: bool = True,
) -> str | None:
    """Shared re-authentication budget, across forms, sessions and client addresses.

    Uses the sign-in reservation machinery for both the address and an account key.
    Per-account serialization keeps a success from clearing pending failures. Other
    accounts can verify concurrently; the process lock only protects the lock registry.
    Returns a plain refusal, or None after a successful check.

    `clear_on_success=False` (#546): a correct password only gives its reservations back and
    leaves earlier failures standing. A caller that goes on to check a second factor under
    the same (email, address) budget (MFA turn-off) must not let each correct password wipe
    that factor's failed-code count.
    """
    with _reauth_account_lock(user_id), Session(_engine()) as s:
        user = s.get(User, user_id)
        if not user:
            return "User not found"
        directory = allow_ldap and auth_source_of(user) == "ldap"
        refusal = None if directory else password_change_refusal(user)
        if refusal:
            return refusal
        if not current:
            return "Enter your current password"
        account_ip = _REAUTH_ACCOUNT_BUCKET
        reserved = []
        for address in dict.fromkeys((ip, account_ip)):
            if not reserve_login_attempt(user.email, address):
                for held in reserved:
                    release_login_attempt(user.email, held)
                audit.log(
                    actor=user.email,
                    action="login.lockout",
                    ip=ip,
                    detail="current password refused while locked",
                )
                return REAUTH_LOCKED
            reserved.append(address)
        try:
            if directory:
                from app.services import ldap_auth

                # Reuse sign-in's bind without provisioning or changing the account.
                proof = (
                    ldap_auth._search_then_bind(user.email, current)
                    if ldap_auth.enabled()
                    else None
                )
                correct = bool(proof and normalize_email(proof[1]) == user.email and user.active)
            else:
                correct = verify_password(user.password_hash, current)
        except Exception:
            for address in reserved:
                release_login_attempt(user.email, address)
            raise
        if correct:
            for address in reserved:
                if clear_on_success:
                    clear_login_failures(user.email, address)
                else:
                    release_login_attempt(user.email, address)
            return None
        locked = False
        for address in reserved:
            locked = settle_login_failure(user.email, address) or locked
        if locked:
            bump_session_epoch(user.id, session=s)
            s.commit()
        audit.log(
            actor=user.email,
            action="login.lockout" if locked else "account.reauth.failed",
            ip=ip,
            detail="current password refused",
        )
        return REAUTH_LOCKED if locked else "Current password is incorrect"


def change_password(user_id: int, current: str, new: str, *, ip: str = ""):
    refusal = check_current_password(user_id, current, ip=ip)
    if refusal:
        return PasswordChangeResult(False, refusal)
    with Session(_engine()) as s:
        u = s.get(User, user_id)
        if not u:
            return PasswordChangeResult(False, "User not found")
        # Train sso-localpw: an account without a local password (SSO/SAML/SCIM/LDAP/
        # trust, or a local row with no hash) is refused BEFORE anything else. The old
        # `if u.password_hash and ...` skipped the current-password check for exactly
        # those accounts and planted a local credential.
        refusal = password_change_refusal(u)
        if refusal:
            return PasswordChangeResult(False, refusal)
        issue = password_issue(new, user=u)
        if issue:
            return PasswordChangeResult(False, issue)
        from app.services import password_policy

        # Clears "change at next sign-in" and restarts the max-age clock.
        password_policy.record_password(s, u, new)
        # #138: a password change must kill every OTHER outstanding session for this
        # account (the classic "I think someone has my password" move). The SAME atomic
        # bump as logout, in this one transaction, and the epoch it produces is returned
        # so the web seam can stamp the acting session without ever re-reading it.
        new_epoch = bump_session_epoch(u.id, session=s)
        s.commit()
        return PasswordChangeResult(True, "Password updated", new_epoch)


_INVITE_ORG_UNSET = object()


# --- email invitations (the account-creation handshake) ---
def _invite_scope(query, org_id):
    """#129: NULL is its own scope under the gate, never a scope widener."""
    from app.services import access

    return query.where(Invite.org_id == org_id) if access.multi_org_active() else query


def create_invite(
    email: str,
    role: str,
    created_by: str,
    days: int = 7,
    *,
    org_id=_INVITE_ORG_UNSET,
    ip: str = "",
) -> Invite:
    # Legacy service callers resolve once at creation; every public door supplies
    # its explicit scope, including None for an unscoped operator.
    if org_id is _INVITE_ORG_UNSET:
        inviter = get_by_email(created_by) if created_by else None
        org_id = getattr(inviter, "org_id", None)
    # #181: an invitation mints an account on acceptance, so it must name an org under
    # an active gate. Refused here, at issue time, where the inviter can still fix it.
    refuse_orgless_account(org_id, invite=True)
    # BUG-A3-1: same format gate as create_user — an invite is always a public/admin
    # action (never a seeder), so validation is unconditional. A malformed address is a
    # clean ValueError (surfaced identically on the web Users page + CLI + /api).
    email = validate_email(email)
    # An invite can only ever be accepted through create_user, which refuses a reserved
    # (or reserved-looking) identity; refuse it here too so no dead invite is ever issued.
    if is_reserved_actor(email):
        raise ValueError(f"{email!r} is a reserved system identity.")
    # Gate sso-localpw rounds 1+2: no invitation is issued for an existing account it may
    # not touch (a trust identity, an IdP account, a deactivated one); accept_invite asks
    # the same question for an invitation that predates the account's current state.
    target = get_by_email(email)
    refusal = invite_refusal(target, org_id) if target is not None else None
    if refusal:
        audit.log(
            actor=created_by or "system",
            action="user.invite.refused",
            target=email,
            detail=(
                "refused: trust service identity"
                if auth_source_of(target) == "trust"
                else (
                    "refused: other organization"
                    if refusal == _CROSS_ORG_INVITE_REFUSAL
                    and getattr(target, "org_id", None) != org_id
                    else _invite_refusal_detail(target)
                )
            ),
            ip=ip,
        )
        raise ValueError(refusal)
    # email-intl: refuse a joiner / normalization twin of another account. Runs after the
    # sso-localpw refusal: that one is about the exact account this address names (and
    # audits a refused invitation for it), this one about a DIFFERENT account that merely
    # looks the same, which excludes the exact match.
    refuse_lookalike_email(email)
    token = secrets.token_urlsafe(32)
    superseded = []
    now = datetime.utcnow()
    with Session(_engine()) as s:
        for previous in s.exec(
            _invite_scope(
                select(Invite).where(
                    Invite.email == email,
                    Invite.accepted == False,  # noqa: E712
                    Invite.revoked_at == None,  # noqa: E711
                    Invite.expires_at >= now,
                ),
                org_id,
            )
        ).all():
            previous.revoked_at = now
            s.add(previous)
            superseded.append(previous.email)
        inv = Invite(
            email=email,
            role=role if role in Role.ALL else Role.TRANSLATOR,
            token=token,
            created_by=created_by,
            org_id=org_id,
            expires_at=datetime.utcnow() + timedelta(days=days),
        )
        s.add(inv)
        s.commit()
        s.refresh(inv)
    for target in superseded:
        audit.log(actor=created_by, action="user.invite.supersede", target=target, ip=ip)
    return inv


def revoke_invite(
    invite_id: int,
    actor: str,
    *,
    org_id: int | None = None,
    ip: str = "",
    allow_expired: bool = False,
) -> Invite | None:
    """Revoke a live invite; consumed, expired and unknown IDs are a no-op.

    With ``allow_expired`` an expired invite is revoked too (Resend, and "Delete" on the
    Users page). This is always a SOFT delete: the row stays, ``revoked_at`` is set, and
    the audit detail says ``expired`` when the invite had already lapsed."""
    with Session(_engine()) as s:
        inv = s.exec(_invite_scope(select(Invite).where(Invite.id == invite_id), org_id)).first()
        now = datetime.utcnow()
        if (
            not inv
            or inv.accepted
            or inv.revoked_at
            or (not allow_expired and inv.expires_at < now)
        ):
            return None
        was_expired = inv.expires_at < now
        inv.revoked_at = now
        s.add(inv)
        s.commit()
        s.refresh(inv)
    audit.log(
        actor=actor,
        action="user.invite.revoke",
        target=inv.email,
        detail="expired" if was_expired else "",
        ip=ip,
    )
    return inv


def invite_metadata(inv: Invite) -> dict:
    """Public invitation metadata: never include the bearer token."""
    return {
        "id": inv.id,
        "email": inv.email,
        "role": inv.role,
        "created_at": inv.created_at.isoformat(),
        "expires_at": inv.expires_at.isoformat(),
    }


def _invites(*, expired: bool, org_id: int | None):
    with Session(_engine()) as s:
        query = select(Invite).where(
            Invite.accepted == False,  # noqa: E712
            Invite.revoked_at == None,  # noqa: E711
        )
        now = datetime.utcnow()
        query = query.where(Invite.expires_at < now if expired else Invite.expires_at >= now)
        query = _invite_scope(query, org_id)
        return s.exec(query.order_by(Invite.created_at)).all()


def expired_invites(org_id: int | None = None):
    """Unconsumed expired invitations, for the admin's history."""
    return _invites(expired=True, org_id=org_id)


def pending_invites(org_id: int | None = None):
    return _invites(expired=False, org_id=org_id)


def get_invite(token: str):
    with Session(_engine()) as s:
        inv = s.exec(select(Invite).where(Invite.token == token)).first()
        if not inv or inv.accepted or inv.revoked_at or inv.expires_at < datetime.utcnow():
            return None
        # GATE R5 MED-2: an invitation issued BEFORE the gate was turned on carries
        # org_id=NULL. After the flip, acceptance fails closed further down the path, but
        # the accept GET still rendered 200 with the invitee's own address on it, while
        # _invite_scope hid the row from BOTH the pending and the expired list, so no
        # admin could see it to revoke it. Live-looking, un-acceptable and invisible, at
        # exactly the pre-enablement moment this train exists for.
        #
        # Treat it as not-found here, which is the one place GET and POST share, so the
        # page and the submission finally agree and the door stops being an oracle for
        # who was invited. A strict no-op with the gate off.
        from app.services import access

        if inv.org_id is None and access.multi_org_active():
            return None
        return inv
