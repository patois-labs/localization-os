"""Durable account password lockout, separate from address and MFA code budgets.

Documented in: docs/architecture/identity-and-access.md
"""

import hashlib
import hmac
import json
import secrets
import time
from contextvars import ContextVar

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import config
from app.models import LoginThrottle
from app.services import audit, security

LIMIT = 20
WINDOW = 3600
DEVICE_COOKIE = "loc_known_device"
DEVICE_AGE = 30 * 86400
LOCK_MESSAGE = "Too many sign-in attempts. Try again later or use a known device."
_directory_context = ContextVar("password_login_budget", default=None)


class AccountLocked(Exception):
    """A directory alias resolved to a locked canonical account before the bind."""


def account_name(name):
    with Session(security._engine()) as s:
        row = s.get(LoginThrottle, _key(name))
        canonical = row.canonical_name if row else ""
    if canonical and security.get_by_email(name) is not None:
        # A newly created local account owns its name, even if it used to be an alias.
        _change(_key(name), lambda row: ({"canonical_name": ""}, True))
        return name
    return canonical or name


def start_directory_context(name, ip, cookie, reservations):
    return _directory_context.set((name, ip, cookie, reservations))


def end_directory_context(token):
    _directory_context.reset(token)


def reserve_directory_account(email):
    """Directory search resolves aliases; reserve the account BEFORE the user bind.

    The password form has already reserved the typed name. Reauthentication has its
    own budget and does not install this context. Never remap a local login name.
    """
    context = _directory_context.get()
    if not context or not email:
        return
    name, ip, cookie, reservations = context
    email = security.normalize_email(email)
    # A trusted directory search has established a real identity, even before its
    # first successful bind provisions a User. Unknown-name quotas cannot block it.
    _insert(_key(email))
    if _key(name) == _key(email) and any(reservations):
        return
    if security.get_by_email(name) is None:
        _change(_key(name), lambda row: ({"canonical_name": email}, True))
    user = security.get_by_email(email)
    if known_device(cookie, user):
        return
    held = reserve(email, ip)
    if held is None:
        raise AccountLocked()
    reservations.append(held)


def _now():
    return time.time()


def _key(name):
    # Hash normalized typed names: a password accidentally typed here is not stored.
    normalized = (name or "").strip().lower()
    return "name:" + hashlib.sha256(normalized.encode()).hexdigest()


def _insert(key):
    with Session(security._engine()) as s:
        if s.get(LoginThrottle, key) is not None:
            return
        s.add(LoginThrottle(key=key, expires_at=_now() + 86400))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()  # Another process created the same row.


def _change(key, change):
    """Optimistic CAS on a bounded row, portable across SQLite and Postgres.

    Every settle is one conditional UPDATE. A losing writer retries against fresh
    state. Unique reservation IDs prevent late completions from changing a reset row.
    No process lock is part of correctness and no read transaction spans the UPDATE.
    """
    while True:
        with Session(security._engine()) as s:
            row = s.get(LoginThrottle, key)
            if row is None:
                return None
            s.expunge(row)
        values, result = change(row)
        if values is None:
            return result
        with Session(security._engine()) as s:
            res = s.execute(
                update(LoginThrottle)
                .where(LoginThrottle.key == key, LoginThrottle.version == row.version)
                .values(**values, version=row.version + 1)
            )
            s.commit()
            if res.rowcount == 1:
                return result


def _admit(key, limit):
    """Fixed daily creation quota; global quota bounds rotating-address growth too."""

    def change(row):
        count = row.level if row.locked_until > _now() else 0
        if count >= limit:
            return None, False
        return {
            "level": count + 1,
            "locked_until": row.locked_until if count else _now() + 86400,
            "expires_at": _now() + 2 * 86400,
        }, True

    return _change(key, change)


def _ensure(name, ip):
    key = _key(name)
    with Session(security._engine()) as s:
        if s.get(LoginThrottle, key):
            return True
        canonical = s.exec(
            select(LoginThrottle.key)
            .where(LoginThrottle.canonical_name == security.normalize_email(name))
            .limit(1)
        ).first()
    if canonical or security.get_by_email(name) is not None:
        _insert(key)
        return True
    _insert("creation:global")
    if not _admit("creation:global", 10000):
        return False
    address = security._lockout_key("", ip)
    bucket = "creation:" + hashlib.sha256(address.encode()).hexdigest()
    _insert(bucket)
    if not _admit(bucket, 100):
        return False
    _insert(key)
    return True


def locked_seconds(name):
    with Session(security._engine()) as s:
        row = s.get(LoginThrottle, _key(name))
        return max(0, int(row.locked_until - _now())) if row else 0


def reserve(name, ip):
    """None means locked; () means untracked unknown, which still pays verification."""
    key = _key(name)
    if not _ensure(name, ip):
        return ()
    nonce = secrets.token_hex(16)
    address = hashlib.sha256(security._lockout_key("", ip).encode()).hexdigest()

    def change(row):
        now = _now()
        # Pending checks expire after the same hour as failures, even after a crash.
        attempts = [a for a in json.loads(row.attempts) if a[1] > now - WINDOW]
        if row.locked_until > now or len(attempts) >= LIMIT:
            return None, None
        attempts.append([nonce, now, address, False])
        return {"attempts": json.dumps(attempts), "expires_at": now + 86400}, (key, nonce, name, ip)

    return _change(key, change)


def settle(reservation, *, failed):
    if not reservation:
        return False
    key, nonce, name, ip = reservation
    user = security.get_by_email(name)

    def change(row):
        now = _now()
        attempts = [a for a in json.loads(row.attempts) if a[1] > now - WINDOW]
        attempt = next((a for a in attempts if a[0] == nonce and not a[3]), None)
        if attempt is None:
            return None, None
        if failed:
            attempt[3] = True
        else:
            attempts.remove(attempt)
        failures = [a for a in attempts if a[3]]
        values = {"attempts": json.dumps(attempts)}
        event = None
        if len(failures) >= LIMIT and row.locked_until <= now:
            level = min(row.level + 1, 3)
            seconds = 900 * 2 ** (level - 1)
            values.update(attempts="[]", level=level, locked_until=now + seconds)
            values["notification"] = json.dumps([user.email if user else "", seconds])
            event = (len(failures), len({a[2] for a in failures}), seconds)
        return values, event

    event = _change(key, change)
    if event:
        # Notify and audit only the writer that actually committed the lock.
        count, addresses, seconds = event
        audit.log(
            actor=f"unauthenticated {ip}",
            action="login.account_locked",
            target=user.email if user else key,
            detail=f"failures {count}; distinct addresses {addresses}; lock seconds {seconds}",
            ip=ip,
            force=True,
        )
    return bool(event)


def clear(name, *, actor=""):
    from sqlmodel import select

    with Session(security._engine()) as s:
        aliases = s.exec(
            select(LoginThrottle.key).where(
                LoginThrottle.canonical_name == security.normalize_email(name)
            )
        ).all()
    cleared = True
    for key in {*aliases, _key(name)}:
        cleared = _clear_key(key) and cleared
    if actor and cleared:
        audit.log(actor=actor, action="login.account_unlocked", target=name, force=True)
    return cleared


def _clear_key(key):
    _insert(key)
    return bool(
        _change(
            key,
            lambda row: (
                {
                    "attempts": "[]",
                    "level": 0,
                    "locked_until": 0,
                    "expires_at": _now() + 86400,
                },
                True,
            ),
        )
    )


def unlock(user_id, *, actor):
    from app.models import User

    with Session(security._engine()) as s:
        user = s.get(User, user_id)
        if user is None:
            return {"ok": False}
        email = user.email
    return {"ok": clear(email, actor=actor), "email": email}


def purge_expired():
    """Janitor removes idle probes and creation buckets, retaining real escalation."""
    from sqlmodel import select

    from app.models import User

    with Session(security._engine()) as s:
        known = [_key(email) for email in s.exec(select(User.email)).all()]
        s.execute(
            delete(LoginThrottle).where(
                LoginThrottle.expires_at < _now(),
                ~LoginThrottle.key.in_(known),
            )
        )
        s.commit()


def notify_owners():
    """Drain the persisted mail notices off the login response's timing path."""
    from sqlmodel import select

    from app.services import mfa

    with Session(security._engine()) as s:
        rows = s.exec(select(LoginThrottle).where(LoginThrottle.notification != "").limit(100))
        pending = [(row.key, row.notification) for row in rows]
    for key, notice in pending:
        claimed = _change(
            key,
            lambda row, notice=notice: (
                ({"notification": ""}, True) if row.notification == notice else (None, False)
            ),
        )
        if claimed:
            email, seconds = json.loads(notice)
            mfa._notify_owner_locked(email, password_seconds=seconds)


def _device_mac(body, user):
    # Bind to revocation state and account incarnation, including SQLite ID reuse.
    binding = json.dumps(
        [body, user.email, int(user.session_epoch or 0), user.created_at.isoformat()],
        separators=(",", ":"),
    )
    return hmac.new(config.SESSION_SECRET.encode(), binding.encode(), hashlib.sha256).hexdigest()


def device_value(user):
    body = f"login-device-v2:{user.id}:{int(_now())}"
    mac = _device_mac(body, user)
    return body + ":" + mac


def known_device(value, user):
    if not user or not value or len(value) > 200:
        return False
    # Do not trust a detached User from before a reset or deletion.
    current = security.get_by_email(user.email)
    if not current or current.id != user.id or not current.active:
        return False
    try:
        version, uid, issued, mac = value.split(":")
        age = _now() - int(issued)
        body = ":".join((version, uid, issued))
        expected = _device_mac(body, current)
        return (
            version == "login-device-v2"
            and int(uid) == user.id
            and 0 <= age <= DEVICE_AGE
            and hmac.compare_digest(mac, expected)
        )
    except (ValueError, TypeError):
        return False


def remember_device(response, request, user):
    response.set_cookie(
        DEVICE_COOKIE,
        device_value(user),
        max_age=DEVICE_AGE,
        httponly=True,
        secure=config.SECURE_COOKIES or request.url.scheme == "https",
        samesite="lax",
    )
    return response
