"""Password policy for LOCAL accounts (HECVAT AAAI-03: an institution not using SSO can
enforce its own password complexity rules).

ONE policy, read from five settings (defaults follow NIST SP 800-63B; an institution can
tighten them on the web Security page's Passwords card, `config set`, POST
/api/admin/config or MCP `manage_install set_config`):

  password_min_length       8..128, default 12
  password_require_classes  0..4 of lower, upper, digit, symbol, default 0
  password_block_common     1/0, default 1: refuse a password whose normalized core is on
                            the bundled common list (app/resources/common_passwords.txt,
                            SecLists, MIT; see THIRD_PARTY_NOTICES.md), a repetitive or
                            sequential string, one containing the account's email, the
                            username with digits/symbols added, or the FULL product or
                            company name
  password_history          0..24, default 5: refuse reuse of the last N passwords
  password_max_age_days     0 = never (default; NIST advises against forced rotation), else
                            a change is required at the next sign-in after N days

`issue()` is the rule engine; `security.password_issue(pw, user=...)` is the ONE authority
every door calls (web setup, invite accept, account change, forced change, admin create,
admin reset, CLI, /api). It returns ONE plain sentence naming exactly what is wrong, or
None. Spaces and any Unicode are allowed; the maximum is MAX_LENGTH characters.

`record_password()` is the one writer of a local password: it stores the Argon2id hash,
stamps password_changed_at, appends the hash to PasswordHistory and prunes the history to
the configured depth. `change_required()` answers "must this person pick a new password
before anything else?" (an admin reset, or max age) for the sign-in seam.

SSO / LDAP / SCIM / SAML accounts carry no local password and are never checked. The dev
seeders keep bypassing the rules (create_user(enforce_policy=False)).

Documented in: docs/architecture/identity-and-access.md
"""

import functools
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.models import PasswordHistory, User

#: Generous upper bound (NIST: at least 64). Keeps an Argon2 hash of a pasted novel off
#: the request path. The minimum setting tops out at 128, so the two can never cross.
MAX_LENGTH = 1024

#: The setting keys, in display order. Also settings.NOT_SECRET_KEYS (a policy number,
#: not a secret, despite "password" in the name).
KEYS = (
    "password_min_length",
    "password_require_classes",
    "password_block_common",
    "password_history",
    "password_max_age_days",
)

#: Defaults (NIST SP 800-63B). settings.DEFAULTS carries the same values; a garbage stored
#: value falls back to these on read (the write doors already refuse garbage).
DEFAULT_MIN_LENGTH = 12
DEFAULT_REQUIRE_CLASSES = 0
DEFAULT_BLOCK_COMMON = True
DEFAULT_HISTORY = 5
DEFAULT_MAX_AGE_DAYS = 0

#: Hard caps, identical to the settings validators.
HISTORY_CAP = 24

_COMMON_FILE = Path(__file__).resolve().parent.parent / "resources" / "common_passwords.txt"

#: A product or company name shorter than this (spaces removed) is not refused (a very
#: short name would otherwise ban a large share of ordinary passphrases).
_MIN_TERM = 4
_SPLIT = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True)
class Policy:
    min_length: int = DEFAULT_MIN_LENGTH
    require_classes: int = DEFAULT_REQUIRE_CLASSES
    block_common: bool = DEFAULT_BLOCK_COMMON
    history: int = DEFAULT_HISTORY
    max_age_days: int = DEFAULT_MAX_AGE_DAYS


def _int_setting(key: str, default: int, lo: int, hi: int) -> int:
    from app.services import settings

    try:
        value = int(str(settings.get(key)).strip())
    except (TypeError, ValueError):
        return default
    return value if lo <= value <= hi else default


def current() -> Policy:
    """The policy in force now (a garbage stored value reads as its default)."""
    from app.services import settings

    block = str(settings.get("password_block_common") or "").strip()
    return Policy(
        min_length=_int_setting("password_min_length", DEFAULT_MIN_LENGTH, 8, 128),
        require_classes=_int_setting("password_require_classes", DEFAULT_REQUIRE_CLASSES, 0, 4),
        block_common=DEFAULT_BLOCK_COMMON if block not in ("0", "1") else block == "1",
        history=_int_setting("password_history", DEFAULT_HISTORY, 0, HISTORY_CAP),
        max_age_days=_int_setting("password_max_age_days", DEFAULT_MAX_AGE_DAYS, 0, 3650),
    )


def summary(policy: Policy | None = None) -> str:
    """The rules in force as plain sentences, for form help text and the CLI."""
    p = policy or current()
    parts = [f"At least {p.min_length} characters. Spaces and any language's letters are fine."]
    if p.require_classes:
        parts.append(
            f"Use at least {p.require_classes} of these: a lowercase letter, an uppercase "
            "letter, a digit, a symbol."
        )
    if p.block_common:
        parts.append(
            "It cannot be a commonly used password, contain your email, or be your user name "
            "with digits or symbols added, and it cannot contain the full product or company "
            "name."
        )
    parts.append("It must be different from your current password.")
    if p.history > 1:
        parts.append(f"It cannot be one of your last {p.history} passwords.")
    return " ".join(parts)


_EDGE = re.compile(r"^[\W\d_]+|[\W\d_]+$")
_RUN = re.compile(r"(.)\1{2,}")
#: Keyboard and alphabet runs a password may not simply be a stretch of (NIST SP 800-63B
#: "repetitive or sequential characters"), checked forwards and backwards.
_SEQUENCES = (
    "01234567890123456789",
    "abcdefghijklmnopqrstuvwxyz",
    "qwertyuiopasdfghjklzxcvbnm",
    "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p",
)
#: A password with fewer distinct characters than this is "repetitive".
_MIN_DISTINCT = 4


def _unrepeat(s: str) -> str:
    """ "abcabcabc" -> "abc": the unit a string is a whole repeat of, else the string."""
    for n in range(1, len(s) // 2 + 1):
        if len(s) % n == 0 and s[:n] * (len(s) // n) == s:
            return s[:n]
    return s


def common_core(pw: str) -> str:
    """The normalized form the common-password rule compares: lowercased, a whole repeat
    collapsed to its unit, leading and trailing digits and symbols stripped, runs of 3+
    identical characters collapsed. "Password1234!" -> "password", "iloveyou2024!" ->
    "iloveyou", "trustno1trustno1" -> "trustno". The bundled list stores these cores."""
    s = _unrepeat(_fold(pw).strip())
    s = _EDGE.sub("", s)
    s = _RUN.sub(r"\1", s)
    return _unrepeat(s)


@functools.lru_cache(maxsize=1)
def _common() -> frozenset:
    try:
        text = _COMMON_FILE.read_bytes().decode("utf-8")
    except OSError:
        return frozenset()
    return frozenset(w.strip() for w in text.splitlines() if w.strip())


def _sequential(s: str) -> bool:
    return len(s) >= 4 and any(s in seq or s in seq[::-1] for seq in _SEQUENCES)


def is_common(pw: str) -> bool:
    """True when `pw` is a common password: its normalized core (common_core) is on the
    bundled list, or it is repetitive (fewer than 4 distinct characters) or a plain
    keyboard / alphabet / digit sequence. Offline; case-insensitive."""
    folded = _fold(pw).strip()
    if len(set(folded)) < _MIN_DISTINCT or _sequential(folded.replace(" ", "")):
        return True
    core = common_core(pw)
    return len(core) >= 4 and core in _common()


def classes_in(pw: str) -> set:
    """Which of lower / upper / digit / symbol `pw` uses. A letter with no case (most
    scripts outside Latin, Greek, Cyrillic and Armenian) counts as lowercase, so a
    password in such a script is never refused for missing a "lowercase letter"."""
    found = set()
    for ch in pw or "":
        if ch.isdigit():
            found.add("digit")
        elif ch.isupper():
            found.add("upper")
        elif ch.isalpha():
            found.add("lower")
        else:
            found.add("symbol")  # punctuation, spaces, emoji and every other non-letter
    return found


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").casefold()


def _letters(s: str) -> str:
    return "".join(c for c in s if c.isalpha())


def _product_terms() -> list[str]:
    """The FULL product name and the FULL company / organization name, folded with spaces
    and punctuation removed. Individual words are never terms on their own ("State
    University" refuses "stateuniversity2026", not every password containing "state")."""
    from app import branding

    terms = []
    for name in (branding.brand_name(), branding.org_name()):
        squashed = _SPLIT.sub("", _fold(name))
        if len(squashed) >= _MIN_TERM:
            terms.append(squashed)
    return list(dict.fromkeys(terms))


def _personal_hit(pw: str, email: str) -> str | None:
    """What personal or product term `pw` is built on, or None.

    The username here IS the email address, so a password is refused when it contains
    the whole email address, or when it IS the username (the part before the @) with
    only digits, symbols or spaces added ("jane.doe2026!"). It is also refused when it
    contains the full product name or the full company name (spaces and case ignored)."""
    folded = _fold(pw)
    squashed = _SPLIT.sub("", folded)
    mail = _fold(email).strip()
    if "@" in mail and mail in folded:
        return "your email address"
    local = _letters(mail.split("@", 1)[0])
    if len(local) >= 3 and _letters(folded) == local:
        return "your username"
    for term in _product_terms():
        if term in squashed:
            return "the product or company name"
    return None


def _user_email(user) -> str:
    if user is None:
        return ""
    if isinstance(user, str):
        return user
    return str(getattr(user, "email", "") or "")


def _stored_hashes(user, depth: int) -> tuple[str, list[str]]:
    """(the CURRENT hash, the newest `depth` history hashes) for a User row; ("", [])
    for anything without an id (an email string, a not-yet-created account)."""
    uid = getattr(user, "id", None)
    if not uid or isinstance(user, str):
        return "", []
    from app.services import security_client_ip_one_authority_ as _sec

    with Session(_sec._engine()) as s:
        row = s.get(User, uid)
        current_hash = (row.password_hash if row else "") or ""
        rows = []
        if depth > 0:
            rows = s.exec(
                select(PasswordHistory.password_hash)
                .where(PasswordHistory.user_id == uid)
                .order_by(PasswordHistory.id.desc())
                .limit(depth)
            ).all()
    return current_hash, [h for h in rows if h and h != current_hash]


def issue(
    pw: str, *, user=None, policy: Policy | None = None, admin_reset: bool = False
) -> str | None:
    """Why `pw` is unacceptable, as ONE plain sentence, or None. `user` is the account
    the password is for when the door knows it: a User row (enables the current-password
    and reuse rules), or any object with an `.email`, or a bare email string (the
    personal-term rule only).

    Rules against the account's STORED passwords (the current one, always; the reuse
    history, per `password_history`) apply to a person choosing their own password. An
    admin reset passes `admin_reset=True` and skips them: the answer would tell the admin
    whether a guess is the person's current or an old password (a guessing oracle), and
    the reset forces the person to choose a fresh password at the next sign-in anyway."""
    p = policy or current()
    pw = pw if isinstance(pw, str) else ""
    if len(pw) < p.min_length:
        return f"Password must be at least {p.min_length} characters."
    if len(pw) > MAX_LENGTH:
        return f"Password must be at most {MAX_LENGTH} characters."
    if not pw.strip():
        return "Password cannot be only spaces."
    if p.require_classes:
        have = len(classes_in(pw))
        if have < p.require_classes:
            return (
                f"Password must use at least {p.require_classes} of these: a lowercase "
                f"letter, an uppercase letter, a digit, a symbol. It uses {have}."
            )
    if p.block_common:
        if is_common(pw):
            return "That password is too common. Choose one that is harder to guess."
        what = _personal_hit(pw, _user_email(user))
        if what:
            return f"Password cannot contain {what}."
    if admin_reset:
        return None
    current_hash, previous = _stored_hashes(user, p.history)
    if current_hash or previous:
        from app.services import security_client_ip_one_authority_ as _sec

        # ALWAYS, whatever password_history says: a forced change (admin reset, max age)
        # must never be satisfiable by typing the same password again.
        if current_hash and _sec.verify_password(current_hash, pw):
            return "Choose a password different from your current one."
        # The current password is one of the last N, so N-1 previous ones are checked.
        for h in previous[: max(0, p.history - 1)]:
            if _sec.verify_password(h, pw):
                return (
                    f"Password cannot be one of your last {p.history} passwords. Choose a new one."
                )
    return None


def record_password(session: Session, row: User, pw: str, *, must_change: bool = False) -> None:
    """THE writer of a local password: hash, clock, flag and history, in the caller's
    transaction (the caller commits). Prunes history to the configured depth."""
    from app.services import security_client_ip_one_authority_ as _sec

    depth = current().history  # read before this transaction writes anything
    new_hash = _sec.hash_password(pw)
    row.password_hash = new_hash
    row.password_changed_at = datetime.utcnow()
    row.password_must_change = bool(must_change)
    session.add(row)
    session.flush()
    if depth:
        session.add(PasswordHistory(user_id=row.id, password_hash=new_hash))
        session.flush()
    _prune(session, row.id, depth)


def _prune(session: Session, uid: int, keep: int) -> None:
    ids = session.exec(
        select(PasswordHistory.id)
        .where(PasswordHistory.user_id == uid)
        .order_by(PasswordHistory.id.desc())
    ).all()
    for stale in ids[max(0, keep) :]:
        obj = session.get(PasswordHistory, stale)
        if obj is not None:
            session.delete(obj)


def delete_history(session: Session, uid: int) -> int:
    """Remove every stored previous-password hash for one account (GDPR erase)."""
    rows = session.exec(select(PasswordHistory).where(PasswordHistory.user_id == uid)).all()
    for r in rows:
        session.delete(r)
    return len(rows)


def change_required(user) -> str | None:
    """Returns "reset" when an admin reset the password, "expired" when it is older than
    password_max_age_days, else None. Only LOCAL accounts with a password can be asked."""
    if user is None or not getattr(user, "password_hash", ""):
        return None
    from app.services.security_client_ip_one_authority_ import auth_source_of

    if auth_source_of(user) != "local":
        return None
    if getattr(user, "password_must_change", False):
        return "reset"
    days = current().max_age_days
    changed = getattr(user, "password_changed_at", None)
    if days and changed is not None and datetime.utcnow() - changed > timedelta(days=days):
        return "expired"
    return None
