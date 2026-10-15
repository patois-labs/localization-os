"""Two-step sign-in: built-in TOTP (RFC 6238) for accounts that sign in with a password.

HECVAT AAAI-17 / DCTR-14. A person who turns it on proves, after the password, that they
hold an authenticator app (Google Authenticator, Microsoft Authenticator, 1Password,
Aegis, ...) by typing its current 6-digit code, or one of ten single-use recovery codes.

What this module owns (the web flow that uses it is app/web/auth_mfa.py):

  * the TOTP arithmetic itself: RFC 4226 HOTP over HMAC-SHA1, 30-second steps, 6 digits,
    and one step of clock drift either way. Standard library only (hmac, struct, base32);
  * the per-account secret, stored as a Fernet ciphertext under config.ENCRYPTION_KEY
    (the key connector credentials use), never in plaintext; the plaintext exists only in
    memory and on the enrollment page;
  * replay refusal: `UserMfa.last_step` is the newest step a code was accepted for, and
    an accepted code must be for a LATER step. The update is one conditional SQL
    statement, so two requests racing with the same code cannot both win;
  * ten recovery codes per set, stored as Argon2id hashes like passwords, each usable
    once;
  * the `mfa_required` policy (off / admins / all) and who it applies to.

Who can use it: accounts whose auth_source is "local" or "ldap" (they type a password
into this app). SSO (OIDC) and SAML accounts never reach a password step here; their
identity provider's own MFA applies. API keys are unaffected: a key is a separate,
revocable credential, not a sign-in. There is no "remember this device".

Never logged: codes, recovery codes and secrets. Audit rows name the account and the
kind of factor only.

Documented in: docs/architecture/identity-and-access.md
"""

import base64
import contextlib
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import case, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import config
from app.models import MfaRecoveryCode, Role, User, UserMfa
from app.services import audit, settings
from app.services.security_client_ip_one_authority_ import (
    _engine,
    auth_source_of,
    bump_session_epoch,
    check_current_password,
    dummy_verify,
    hash_password,
    signs_in_through_idp,
    verify_password,
)

#: RFC 6238 parameters. Every mainstream authenticator app defaults to exactly these.
STEP_SECONDS = 30
DIGITS = 6
#: Accept the previous and the next step as well (clock drift of up to 30 s either way).
WINDOW_STEPS = 1
#: 160-bit shared secret, the RFC 4226 recommendation for HMAC-SHA1.
TOTP_KEY_BYTES = 20
_KEY_PARAM = "secret"  # the otpauth URI parameter name (Key Uri Format)
#: Recovery codes per set, and their shape: 10 characters from an alphabet without
#: look-alikes (no 0/o, 1/l/i), shown as two groups of five.
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LEN = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
#: How long the half-signed-in state between the password and the code may last.
PENDING_MAX_SECONDS = 300
#: The PER-ACCOUNT wrong-code budget (gate round 1, MEDIUM-1). The sign-in lockout counts
#: per typed name + client IP, so someone who has the password could spread code guesses
#: over many addresses. This budget counts wrong codes against the ACCOUNT, from any
#: address and any spelling of the sign-in name, on every code check (sign-in, forced and
#: self-service set-up, new recovery codes, turning it off): ACCOUNT_FAIL_LIMIT wrong codes
#: inside ACCOUNT_FAIL_WINDOW_S lock every code check for ACCOUNT_LOCK_S. It is stored on
#: the UserMfa row, so a restart does not reset it.
ACCOUNT_FAIL_LIMIT = 10
ACCOUNT_FAIL_WINDOW_S = 15 * 60
ACCOUNT_LOCK_S = 15 * 60


class _Refused:
    """What verify / confirm_enrollment answer when the code was REFUSED UNCHECKED (the
    account is locked, or in-flight attempts hold the whole budget), as distinct from None
    for a code that was checked and wrong (gate round 3, LOW-7). Falsy, so a caller that
    only asks "did it pass?" still fails closed; the sign-in routes test `is REFUSED` to
    show the paused message and not count a per-address failure for a code never looked at."""

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "mfa.REFUSED"

    # One instance only: a copy or a pickle round trip stays `is REFUSED` (gate INFO-2).
    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return "REFUSED"


REFUSED = _Refused()

#: The `mfa_required` values, and the one used when the stored value is unreadable (a
#: hand-edited row; every write door validates): the default, like session_max_hours does.
POLICY_VALUES = ("off", "admins", "all")
DEFAULT_POLICY = "off"
#: auth_source values that sign in with a password typed into this app.
PASSWORD_SOURCES = frozenset({"local", "ldap"})
#: #423: a local account with no password here has nothing to prove before set-up.
NO_LOCAL_SIGNIN_REFUSAL = (
    "This account has no password on this site, so two-step sign-in cannot be set up or "
    "changed here. Ask an administrator to send you an invitation or reset your password."
)


class MfaError(ValueError):
    """A refused two-step sign-in change, with one plain sentence for the person."""


def _now() -> float:
    """The ONE clock for codes and the pending window, so a suite can drive time by
    patching this function instead of sleeping."""
    return time.time()


# --- RFC 4226 / 6238 -------------------------------------------------------------------
def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP: HMAC-SHA1 of the 8-byte counter, dynamic truncation, `digits` digits."""
    mac = hmac.new(key, struct.pack(">Q", int(counter)), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def time_step(now: float | None = None) -> int:
    """The RFC 6238 time step (T) for `now` (seconds since the epoch)."""
    return int((_now() if now is None else now) // STEP_SECONDS)


def totp(secret_b32: str, now: float | None = None) -> str:
    """The current code for a base32 secret (what the authenticator app shows)."""
    return hotp(_key_bytes(secret_b32), time_step(now))


def new_secret() -> str:
    """A fresh random secret as unpadded upper-case base32 (32 characters)."""
    return base64.b32encode(secrets.token_bytes(TOTP_KEY_BYTES)).decode("ascii").rstrip("=")


def _key_bytes(secret_b32: str) -> bytes:
    s = "".join(str(secret_b32 or "").split()).upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def format_secret(secret_b32: str) -> str:
    """The secret in groups of four, for typing into an app by hand."""
    s = str(secret_b32 or "")
    return " ".join(s[i : i + 4] for i in range(0, len(s), 4))


def otpauth_uri(secret_b32: str, account: str) -> str:
    """The Key URI Format every authenticator app reads from a QR code."""
    from app import branding

    issuer = branding.brand_name()
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?{_KEY_PARAM}={secret_b32}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


def qr_svg(uri: str) -> str:
    """An inline SVG QR code of `uri` (segno, pure Python, BSD-3-Clause). The markup is
    produced by segno from the encoded modules only (no text from the URI is written into
    it), so it is safe to render unescaped. The page always shows the key as text too."""
    import segno

    return segno.make(uri, error="m").svg_inline(scale=5, border=4, dark="#000", light="#fff")


def _normalize(code) -> str:
    """A typed code with spaces and hyphens removed, lower-cased (recovery codes are shown
    as xxxxx-xxxxx; people paste them with or without the hyphen)."""
    return "".join(c for c in str(code or "") if c not in " -\t\r\n").lower()[:64]


def _totp_shaped(norm: str) -> bool:
    """Six ASCII digits. str.isdigit() alone is true for Arabic-Indic, superscript and other
    Unicode digits, which then made the constant-time compare raise (a 500 on user input,
    mfa-low7 gate LOW-3); such a code is now simply a wrong code."""
    return len(norm) == DIGITS and norm.isascii() and norm.isdigit()


def _utc_now() -> datetime:
    """Naive UTC 'now' from the ONE clock (_now), for the stored counter timestamps."""
    return datetime.fromtimestamp(_now(), timezone.utc).replace(tzinfo=None)


def _match_step(secret_b32: str, code: str, now: float | None = None) -> int | None:
    """The time step `code` belongs to inside the drift window, or None. Every candidate
    is compared (constant-time), with no early exit."""
    try:
        key = _key_bytes(secret_b32)
    except Exception:
        return None
    base = time_step(now)
    found = None
    for step in range(base - WINDOW_STEPS, base + WINDOW_STEPS + 1):
        if hmac.compare_digest(hotp(key, step), code) and found is None:
            found = step
    return found


# --- secret at rest (Fernet, like connector credentials) -------------------------------
def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(config.ENCRYPTION_KEY.encode())


def _encrypt(secret_b32: str) -> str:
    return _fernet().encrypt(secret_b32.encode("ascii")).decode("ascii")


def _decrypt(token: str) -> str | None:
    """The plaintext secret, or None when it cannot be read (a lost or rotated
    ENCRYPTION_KEY). None fails CLOSED: the code is refused, and an admin reset (or the
    host CLI `user mfa-reset`) is the way back in."""
    try:
        return _fernet().decrypt(str(token or "").encode("ascii")).decode("ascii")
    except Exception:
        return None


# --- policy ------------------------------------------------------------------------------
def policy() -> str:
    """The `mfa_required` setting: "off", "admins" or "all"."""
    value = str(settings.get("mfa_required") or "").strip().lower()
    return value if value in POLICY_VALUES else DEFAULT_POLICY


def applies_to(user) -> bool:
    """True when `user` signs in with a password typed into this app (local or LDAP), so a
    second step here is possible. SSO/SAML/SCIM/trust identities and the auth-off SYSTEM
    operator are never asked.

    #423 (identity-04): a "local" row the IdP adopted by SCIM (a SCIM link, no password)
    signs in only through the identity provider (SAML keeps auth_source too), so it is an
    IdP account in fact (security.signs_in_through_idp): no local factor, never ended by the
    sign-in wall, and its set-up page says the provider's MFA applies."""
    if not (user and getattr(user, "id", 0)):
        return False
    source = auth_source_of(user)
    if source not in PASSWORD_SOURCES:
        return False
    return not (source == "local" and signs_in_through_idp(user))


def required_for(user, value: str | None = None) -> bool:
    """True when the policy makes two-step sign-in mandatory for `user`."""
    if not applies_to(user):
        return False
    value = policy() if value is None else value
    if value == "all":
        return True
    return value == "admins" and getattr(user, "role", "") == Role.ADMIN


def _row(session: Session, user_id: int) -> UserMfa | None:
    return session.exec(select(UserMfa).where(UserMfa.user_id == int(user_id or 0))).first()


def is_enrolled(user_id: int) -> bool:
    """Two-step sign-in is ON for this account (enrollment confirmed with a code)."""
    if not user_id:
        return False
    with Session(_engine()) as s:
        row = _row(s, user_id)
        return bool(row and row.enabled_at)


def enrolled_ids(user_ids) -> set[int]:
    """Which of `user_ids` have two-step sign-in ON (one query, for the Users table)."""
    ids = [int(i) for i in (user_ids or []) if i]
    if not ids:
        return set()
    with Session(_engine()) as s:
        rows = s.exec(
            select(UserMfa.user_id).where(
                UserMfa.user_id.in_(ids),
                UserMfa.enabled_at != None,  # noqa: E711
            )
        ).all()
        return {int(r) for r in rows}


def status(user) -> dict:
    """The facts every surface shows (web Account and Users pages, /api, CLI). Never the
    secret, never a code."""
    uid = int(getattr(user, "id", 0) or 0)
    enrolled = False
    enabled_at = last_used_at = None
    left = 0
    if uid:
        with Session(_engine()) as s:
            row = _row(s, uid)
            if row and row.enabled_at:
                enrolled = True
                enabled_at, last_used_at = row.enabled_at, row.last_used_at
                left = len(
                    s.exec(
                        select(MfaRecoveryCode.id).where(
                            MfaRecoveryCode.user_id == uid,
                            MfaRecoveryCode.used_at == None,  # noqa: E711
                        )
                    ).all()
                )
    return {
        "enrolled": enrolled,
        "enabled_at": enabled_at.isoformat() if enabled_at else None,
        "last_used_at": last_used_at.isoformat() if last_used_at else None,
        "recovery_codes_left": left,
        "applies": applies_to(user),
        "required": required_for(user),
        "policy": policy(),
    }


# --- enrollment --------------------------------------------------------------------------
def begin_enrollment(user_id: int, current: str = "", *, ip: str = "", flow: str = "") -> str:
    """Self-service entry point: prove the password before disclosing a secret."""
    require_current_password(user_id, current, ip=ip)
    return _begin_enrollment(user_id, flow)


def flow_tag(secret_b32: str) -> str:
    """The set-up FLOW a started secret belongs to (identity-02). The web keeps this tag in
    the one session (or pending sign-in state) that was shown the secret; only that flow
    may see the secret again or confirm it. A one-way hash of a 160-bit random secret, so
    the tag riding in the signed (readable) session cookie discloses nothing."""
    digest = hashlib.sha256(b"loc-tms mfa set-up flow|" + str(secret_b32).encode("ascii"))
    return digest.hexdigest()[:32]


def _flow_matches(secret_b32: str | None, flow) -> bool:
    return bool(secret_b32 and flow) and hmac.compare_digest(flow_tag(secret_b32), str(flow))


def require_current_password(
    user_id: int, current: str, *, ip: str = "", clear_on_success: bool = True
) -> None:
    with Session(_engine()) as s:
        user = s.get(User, user_id)
        if not user or not applies_to(user):
            raise MfaError("Two-step sign-in here is not available for this account.")
        if not user.password_hash and auth_source_of(user) != "ldap":
            # #423: there is nothing to prove, so set-up is REFUSED (it used to be skipped,
            # letting any session holder plant a factor with no proof).
            raise MfaError(NO_LOCAL_SIGNIN_REFUSAL)
    refusal = check_current_password(
        user_id, current, ip=ip, allow_ldap=True, clear_on_success=clear_on_success
    )
    if refusal:
        raise MfaError(refusal)


class EnrollmentCodes(list):
    """One-time codes plus the exact epoch produced by their enrollment transaction."""

    def __init__(self, codes, session_epoch):
        super().__init__(codes)
        self.session_epoch = session_epoch


def _notify_change(user_id: int, change: str) -> None:
    from app.services import mailer

    with contextlib.suppress(Exception), Session(_engine()) as s:
        user = s.get(User, user_id)
        if user:
            mailer.send(
                user.email,
                "Your two-step sign-in settings changed",
                f"Two-step sign-in was {change} for your account. "
                "If you did not do this, contact your administrator immediately.",
            )


def _begin_enrollment(user_id: int, flow: str = "") -> str:
    """Start (or resume) enrollment and return the base32 secret to show; the caller keeps
    `flow_tag(secret)` as its flow. Only the SAME flow resumes: a reload of a page that
    already showed the secret passes that tag and gets the same secret, so what the person
    scanned stays valid. Any other caller (a new password step, a new self-service grant,
    another session) gets a FRESH secret that replaces the unconfirmed one (identity-02: a
    password holder who opened set-up and left must not have planted the secret the owner
    later scans). The row is updated in place, so the per-account wrong-code budget
    survives. Refused when two-step sign-in is already on (turn it off first, or ask an
    admin to reset it)."""
    secret = new_secret()
    with Session(_engine()) as s:
        row = _row(s, user_id)
        if row and row.enabled_at:
            raise MfaError("Two-step sign-in is already on for this account.")
        if row:
            current = _decrypt(row.secret_enc)
            if _flow_matches(current, flow):
                return current
            if _replace_unconfirmed(s, row.id, secret):
                return secret
            raise MfaError("Two-step sign-in is already on for this account.")
        s.add(UserMfa(user_id=int(user_id), secret_enc=_encrypt(secret)))
        try:
            s.commit()
            return secret
        except IntegrityError:
            # Two first-time set-ups at the same moment (two tabs) raced to the unique
            # user_id index: the other one inserted first. This flow still gets its OWN
            # secret (it replaces the other's unconfirmed one) instead of a 500.
            s.rollback()
    with Session(_engine()) as s:
        row = _row(s, user_id)
        if row and not row.enabled_at and _replace_unconfirmed(s, row.id, secret):
            return secret
    raise MfaError("Two-step sign-in is already on for this account.")


def _replace_unconfirmed(session: Session, row_id: int, secret: str) -> bool:
    """Swap a still-unconfirmed row's secret (conditional: a row confirmed meanwhile is
    left alone). Commits. True when the swap landed."""
    res = session.execute(
        update(UserMfa)
        .where(UserMfa.id == row_id, UserMfa.enabled_at == None)  # noqa: E711
        .values(secret_enc=_encrypt(secret), last_step=0)
    )
    if res.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    return True


def pending_secret(user_id: int, flow: str | None = None) -> str | None:
    """The started-but-unconfirmed secret, or None. With `flow`, only when that flow is
    the one the secret was shown to (identity-02)."""
    with Session(_engine()) as s:
        row = _row(s, user_id)
        if not row or row.enabled_at:
            return None
        secret = _decrypt(row.secret_enc)
        if flow is not None and not _flow_matches(secret, flow):
            return None
        return secret


def _new_recovery_codes(session: Session, user_id: int) -> list[str]:
    """Replace the account's recovery codes with a fresh set; returns the plaintext codes
    (shown once, never stored)."""
    old_codes = session.exec(
        select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id)
    ).all()
    for old in old_codes:
        session.delete(old)
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LEN))
        codes.append(raw)
        session.add(MfaRecoveryCode(user_id=user_id, code_hash=hash_password(raw)))
    return [f"{c[:5]}-{c[5:]}" for c in codes]


def _confirm_inner(user_id: int, code, flow: str | None = None) -> list[str] | None:
    """Turn two-step sign-in on when `code` is right for the started secret. Returns the
    ten recovery codes (plaintext, to show once), or None when the code is wrong or no
    enrollment was started. With `flow`, the started secret must be the one shown to that
    flow (identity-02), else None. The accepted step becomes `last_step`, so the same code
    cannot then be used to sign in."""
    norm = _normalize(code)
    with Session(_engine()) as s:
        row = _row(s, user_id)
        secret = _decrypt(row.secret_enc) if (row and not row.enabled_at) else None
        if not secret or not _totp_shaped(norm):
            return None
        if flow is not None and not _flow_matches(secret, flow):
            return None
        step = _match_step(secret, norm)
        if step is None:
            return None
        now = datetime.utcnow()
        # Conditional: only a still-unconfirmed row flips, so two tabs confirming at once
        # cannot mint two recovery-code sets.
        res = s.execute(
            update(UserMfa)
            .where(UserMfa.id == row.id, UserMfa.enabled_at == None)  # noqa: E711
            .values(enabled_at=now, last_step=step, last_used_at=now)
        )
        if res.rowcount != 1:
            s.rollback()
            return None
        codes = _new_recovery_codes(s, int(user_id))
        epoch = bump_session_epoch(user_id, session=s)
        s.commit()
        return EnrollmentCodes(codes, epoch)


# --- verification ------------------------------------------------------------------------
def _verify_inner(user_id: int, code) -> str | None:
    """Check a sign-in code. Returns "totp" or "recovery" on success, else None.

    A TOTP code must be for a time step LATER than the last accepted one (replay refused,
    including an older step inside the drift window). A recovery code is spent on use.
    When there is nothing to compare against (no confirmed enrollment, an unreadable
    secret), one dummy Argon2 verify runs so the answer takes the same time."""
    norm = _normalize(code)
    uid = int(user_id or 0)
    with Session(_engine()) as s:
        row = _row(s, uid) if uid else None
        if not row or not row.enabled_at:
            dummy_verify(norm)
            return None
        if _totp_shaped(norm):
            secret = _decrypt(row.secret_enc)
            if not secret:
                dummy_verify(norm)
                return None
            step = _match_step(secret, norm)
            if step is None or step <= int(row.last_step or 0):
                return None
            res = s.execute(
                update(UserMfa)
                .where(UserMfa.id == row.id, UserMfa.last_step < step)
                .values(last_step=step, last_used_at=datetime.utcnow())
            )
            s.commit()
            return "totp" if res.rowcount == 1 else None
        if len(norm) == RECOVERY_CODE_LEN and all(c in _RECOVERY_ALPHABET for c in norm):
            unused = s.exec(
                select(MfaRecoveryCode).where(
                    MfaRecoveryCode.user_id == uid,
                    MfaRecoveryCode.used_at == None,  # noqa: E711
                )
            ).all()
            match = None
            # Always the same number of Argon2 verifies, whatever is left and wherever the
            # match is: the time taken says nothing about the set.
            for i in range(RECOVERY_CODE_COUNT):
                if i < len(unused):
                    if verify_password(unused[i].code_hash, norm) and match is None:
                        match = unused[i]
                else:
                    dummy_verify(norm)
            if match is None:
                return None
            now = datetime.utcnow()
            res = s.execute(
                update(MfaRecoveryCode)
                .where(MfaRecoveryCode.id == match.id, MfaRecoveryCode.used_at == None)  # noqa: E711
                .values(used_at=now)
            )
            if res.rowcount != 1:
                s.rollback()
                return None
            s.execute(update(UserMfa).where(UserMfa.id == row.id).values(last_used_at=now))
            s.commit()
            return "recovery"
        dummy_verify(norm)
        return None


# --- the per-account wrong-code budget (MEDIUM-1) -----------------------------------------
def account_locked_seconds(user_id: int) -> int:
    """Seconds left on this ACCOUNT's code lock (0 = not locked), from any address."""
    with Session(_engine()) as s:
        row = _row(s, user_id)
        until = getattr(row, "locked_until", None)
    if not until:
        return 0
    return max(0, int((until - _utc_now()).total_seconds()))


def _window_exprs(now: datetime):
    fresh = or_(
        UserMfa.fail_window_start == None,  # noqa: E711
        UserMfa.fail_window_start < now - timedelta(seconds=ACCOUNT_FAIL_WINDOW_S),
    )
    not_locked = or_(UserMfa.locked_until == None, UserMfa.locked_until <= now)  # noqa: E711
    return fresh, not_locked


def _reserve_attempt(user_id: int) -> str:
    """RESERVE one code check against the account budget BEFORE the code is looked at
    (gate round 2, MEDIUM-2). One atomic conditional UPDATE: it succeeds only when the
    account is not locked and fewer than ACCOUNT_FAIL_LIMIT attempts are counted in the
    current window (a stale window restarts at 1). So however many requests race, at most
    ACCOUNT_FAIL_LIMIT codes are ever checked per window: the rest are refused without a
    check. Returns "ok", "locked" (refused, nothing checked) or "none" (no UserMfa row)."""
    uid = int(user_id or 0)
    now = _utc_now()
    fresh, not_locked = _window_exprs(now)
    with Session(_engine()) as s:
        res = s.execute(
            update(UserMfa)
            .where(
                UserMfa.user_id == uid,
                not_locked,
                or_(fresh, UserMfa.fail_count < ACCOUNT_FAIL_LIMIT),
            )
            .values(
                fail_count=case((fresh, 1), else_=UserMfa.fail_count + 1),
                fail_window_start=case((fresh, now), else_=UserMfa.fail_window_start),
            )
        )
        s.commit()
        if res.rowcount == 1:
            return "ok"
        return "locked" if _row(s, uid) is not None else "none"


def _notify_owner_locked(email: str, *, password_seconds: int = 0) -> None:
    """Tell the account owner by email (when SMTP is configured) that code entry locked:
    someone who has their password may be guessing codes (gate round 2, LOW-5)."""
    if not email or "@" not in email:
        return
    try:
        from app.services import mailer

        if password_seconds:
            mailer.send(
                email,
                "Password sign-in locked on your account",
                "20 failed password sign-ins within 60 minutes locked sign-in from unknown "
                f"devices for {password_seconds // 60} minutes. A known device can still sign "
                "in. If this was not you, tell your administrator. They can unlock sign-in.",
            )
            return
        mailer.send(
            email,
            "Two-step sign-in locked on your account",
            (
                f"{ACCOUNT_FAIL_LIMIT} wrong two-step sign-in codes were entered for {email} "
                f"within {ACCOUNT_FAIL_WINDOW_S // 60} minutes, so code entry is locked for "
                f"{ACCOUNT_LOCK_S // 60} minutes.\n\nThe codes are only asked for after a "
                "correct password. If this was not you, someone may know your password: "
                "change it, and tell your administrator. You can still sign in with one of "
                "your recovery codes while code entry is locked.\n"
            ),
        )
    except Exception:  # never let a mail problem break the sign-in path
        return


def _settle_failure(user_id: int, ip: str = "") -> bool:
    """A reserved attempt turned out wrong: it stays counted. When the count has reached
    ACCOUNT_FAIL_LIMIT, lock every code check for ACCOUNT_LOCK_S (one conditional UPDATE,
    so exactly one of several racing failures trips it), audit `mfa.locked` and email the
    owner. Returns True for the failure that locked it. Never logs the code."""
    uid = int(user_id or 0)
    now = _utc_now()
    with Session(_engine()) as s:
        res = s.execute(
            update(UserMfa)
            .where(UserMfa.user_id == uid, UserMfa.fail_count >= ACCOUNT_FAIL_LIMIT)
            .values(
                locked_until=now + timedelta(seconds=ACCOUNT_LOCK_S),
                fail_count=0,
                fail_window_start=None,
            )
        )
        s.commit()
        tripped = res.rowcount == 1
        email = getattr(s.get(User, uid), "email", "") if tripped else ""
    if tripped:
        audit.log(
            actor=email or f"user #{uid}",
            action="mfa.locked",
            target=email,
            detail=(
                f"{ACCOUNT_FAIL_LIMIT} wrong codes within {ACCOUNT_FAIL_WINDOW_S // 60} minutes; "
                f"code checks locked for {ACCOUNT_LOCK_S // 60} minutes from every address "
                "(recovery codes still work)"
            ),
            ip=ip,
        )
        _notify_owner_locked(email)
    return tripped


def _release_attempt(user_id: int) -> None:
    """Give back ONE reserved attempt whose check never finished (it raised, gate round 3,
    LOW-7), so a server-side fault never counts against the account. A single decrement
    guarded by fail_count > 0: a success or a lock that already reset the count in the
    meantime is never pushed below zero. The callers run it once, only on the exception
    path of the check, never after a settle or a clear."""
    with Session(_engine()) as s:
        s.execute(
            update(UserMfa)
            .where(UserMfa.user_id == int(user_id or 0), UserMfa.fail_count > 0)
            .values(fail_count=UserMfa.fail_count - 1)
        )
        s.commit()


def _checked(user_id: int, inner, *args):
    """Run the code check for a reserved attempt: on ANY exception the reservation is given
    back (exactly once, here) and the exception re-raised. Settling or clearing happens
    only after this returns, outside it, so the two paths never both touch the count."""
    try:
        return inner(*args)
    except BaseException:
        # The original fault is the one to report; a failed give-back (the same database
        # being down) leaves the attempt counted, which fails closed.
        with contextlib.suppress(Exception):
            _release_attempt(user_id)
        raise


def _clear_failures(user_id: int) -> None:
    """A right code: the budget restarts (the attempt reserved for it is not left counted)."""
    with Session(_engine()) as s:
        s.execute(
            update(UserMfa)
            .where(UserMfa.user_id == int(user_id or 0))
            .values(fail_count=0, fail_window_start=None)
        )
        s.commit()


def _recovery_shaped(norm: str) -> bool:
    return len(norm) == RECOVERY_CODE_LEN and all(c in _RECOVERY_ALPHABET for c in norm)


def confirm_enrollment(
    user_id: int, code, *, ip: str = "", flow: str | None = None
) -> list[str] | _Refused | None:
    """Turn two-step sign-in on when `code` is right for the started secret. Returns the
    ten recovery codes (plaintext, to show once), None when the code is wrong or no
    enrollment was started, or REFUSED (falsy) when the account's code checks are locked
    or the budget is held, so nothing was checked. Every attempt is reserved against the
    per-account budget BEFORE the code is checked, and given back if the check raises. The
    accepted step becomes `last_step`, so the same code cannot then be used to sign in."""
    state = _reserve_attempt(user_id)
    if state == "none":
        return None  # no enrollment started (or an admin reset removed it): nothing to confirm
    if state != "ok":
        return REFUSED
    codes = _checked(user_id, _confirm_inner, user_id, code, flow)
    if codes:
        _clear_failures(user_id)
        _notify_change(user_id, "turned on")
    else:
        _settle_failure(user_id, ip)
    return codes


def verify(user_id: int, code, *, ip: str = "") -> str | _Refused | None:
    """Check a code (sign-in, new recovery codes, turning it off). Returns "totp" or
    "recovery" on success, REFUSED (falsy) when the code was refused unchecked, else None
    (checked and wrong).

    Authenticator codes: each attempt is RESERVED against the per-account budget before the
    code is checked (refused outright, unchecked, once the budget is spent or the account
    is locked: REFUSED); a wrong code stays counted and may lock the account; a right one
    restarts the budget; a check that raises gives its reservation back (LOW-7). Recovery
    codes (10 characters from a 31-letter alphabet, about 8 x 10^14 values, Argon2id-hashed)
    bypass the per-account lock and do not count toward it, so the
    real owner can always get in while someone with the password keeps the lock on
    (gate round 2, LOW-5); the per-address sign-in lockout still limits them.

    A TOTP code must be for a time step LATER than the last accepted one (replay refused,
    including an older step inside the drift window). A recovery code is spent on use.
    When there is nothing to compare against (no confirmed enrollment, an unreadable
    secret), one dummy Argon2 verify runs so the answer takes the same time."""
    uid = int(user_id or 0)
    if not uid or _recovery_shaped(_normalize(code)):
        return _verify_inner(uid, code)
    state = _reserve_attempt(uid)
    if state == "none":
        return _verify_inner(uid, code)  # no factor stored: the dummy-verify path
    if state != "ok":
        return REFUSED
    kind = _checked(uid, _verify_inner, uid, code)
    if kind:
        _clear_failures(uid)
    else:
        _settle_failure(uid, ip)
    return kind


def recovery_codes_left(user_id: int) -> int:
    with Session(_engine()) as s:
        return len(
            s.exec(
                select(MfaRecoveryCode.id).where(
                    MfaRecoveryCode.user_id == int(user_id or 0),
                    MfaRecoveryCode.used_at == None,  # noqa: E711
                )
            ).all()
        )


def regenerate_recovery_codes(user_id: int) -> list[str]:
    """A fresh set of ten codes (the old set stops working). The caller has already checked
    a current code. Refused when two-step sign-in is off."""
    with Session(_engine()) as s:
        row = _row(s, user_id)
        if not row or not row.enabled_at:
            raise MfaError("Two-step sign-in is not on for this account.")
        codes = _new_recovery_codes(s, int(user_id))
        s.commit()
        _notify_change(user_id, "updated with replacement recovery codes")
        return codes


def _delete_rows(session: Session, user_id: int) -> bool:
    found = False
    codes = session.exec(select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id)).all()
    for code in codes:
        session.delete(code)
    row = _row(session, user_id)
    if row is not None:
        found = bool(row.enabled_at)
        session.delete(row)
    session.flush()
    return found


def disable(user_id: int, current: str = "", *, ip: str = "") -> int:
    """Self-service turn-off after the caller checked a current code. Requires the current
    password, enforces policy, deletes the factor and returns the exact new session epoch."""
    require_current_password(user_id, current, ip=ip)
    return _disable(user_id)


def _disable(user_id: int) -> int:
    """Finish turn-off after the web door has checked the password and then the code."""
    with Session(_engine()) as s:
        user = s.get(User, user_id)
        if required_for(user):
            raise MfaError("Your organization requires two-step sign-in.")
        _delete_rows(s, int(user_id))
        epoch = bump_session_epoch(user_id, session=s)
        s.commit()
    _notify_change(user_id, "turned off")
    return epoch


def reset(user_id: int) -> dict:
    """Admin reset (web Users page, DELETE /api/users/{uid}/mfa, CLI `user mfa-reset`): delete
    the secret and every recovery code AND bump the session epoch in the same transaction,
    so every session the account holds ends at once (the usual reason for a reset is a lost
    or stolen phone). The person sets two-step sign-in up again at the next sign-in when
    the policy requires it. Returns {"ok", "email", "was_enrolled", "session_epoch"}."""
    uid = int(user_id or 0)
    with Session(_engine()) as s:
        user = s.get(User, uid) if uid else None
        if user is None:
            return {"ok": False, "email": None, "was_enrolled": False, "session_epoch": 0}
        email = user.email
        was_on = _delete_rows(s, uid)
        epoch = bump_session_epoch(uid, session=s)
        s.commit()
        return {"ok": True, "email": email, "was_enrolled": was_on, "session_epoch": epoch}


def delete_for_user(session: Session, user_id: int) -> None:
    """GDPR erase: remove the account's second-factor material inside the caller's
    transaction (the caller commits)."""
    _delete_rows(session, int(user_id))
