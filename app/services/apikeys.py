"""Per-user API keys for the inbound token API.

A key is shown ONCE at creation ("lsu_..."); only its sha256 hash is stored.
Verification hashes the presented key and looks the digest up in the DB (the
attacker-controlled input never meets a stored secret byte-by-byte, so there
is no string comparison to time-attack — though the index lookup itself is
not constant-time). The key and its owner must both be active. The legacy
shared connector token (settings `connector_api_token`, "lsk_...") remains a
separate, connector-only credential.

Documented in: docs/architecture/identity-and-access.md
"""

import hashlib
import logging
import secrets
from contextlib import nullcontext
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import db as _db
from app.db import engine
from app.models import ApiKey, User

log = logging.getLogger(__name__)


def _hash(full_key: str) -> str:
    return hashlib.sha256((full_key or "").encode("utf-8")).hexdigest()


class InvalidScopeError(ValueError):
    """A key mint requested a grant that is not in ALL_SCOPES. Raised at EVERY mint
    surface (via create_key) so a key that would authorize nothing is never issued —
    the `apikey issue --scopes all` trap, where "all" minted a working-nowhere key
    (QA Q1). A ValueError subclass so the `init --issue-key` path (security.seed_admin,
    caught as ValueError) and the CLI both degrade to a clean refusal, not a traceback."""


def max_key_age_days() -> int | None:
    """The install's maximum key age in days, or None when unset (the default).
    `apikey_max_age_days` blank/unset/zero = no maximum, so behaviour is
    unchanged until an admin sets it (#270 r2-apiauth-10)."""
    from app.services import settings

    raw = (settings.get("apikey_max_age_days") or "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else None


def resolve_expiry(expires_at: datetime | None) -> datetime | None:
    """Normalize an optional key expiry at the ONE mint seam every surface
    funnels through (#270 r2-apiauth-10). Returns the stored UTC timestamp or
    None (no expiry). Raises ValueError with a clear message when the expiry
    is already in the past, or when the `apikey_max_age_days` setting is set
    and the requested lifetime is missing or longer than the maximum."""
    now = datetime.utcnow()
    if expires_at is None:
        max_days = max_key_age_days()
        if max_days is not None:
            raise ValueError(
                f"this install caps API keys at {max_days} day(s) (setting apikey_max_age_days); "
                "create the key with an expiry within that lifetime."
            )
        return None
    if expires_at <= now:
        raise ValueError("the key expiry is already in the past; choose a future date.")
    max_days = max_key_age_days()
    if max_days is not None:
        ceiling = now + timedelta(days=max_days)
        if expires_at > ceiling:
            raise ValueError(
                "key lifetime exceeds this install's maximum of {} day(s) "
                "(setting apikey_max_age_days); pick an expiry on or before {}.".format(
                    max_days, ceiling.strftime("%Y-%m-%d %H:%M UTC")
                )
            )
    return expires_at


#: The longest lifetime any mint surface accepts, in days (10 years, the same ceiling the
#: apikey_max_age_days setting allows). A larger number is refused cleanly: an unbounded
#: day count overflows datetime arithmetic and used to surface as a 500 / traceback.
MAX_LIFETIME_DAYS = 3650


def expiry_from_days(raw) -> datetime | None:
    """The expiry for a requested lifetime in whole days from now, shared by the web form,
    POST /api/apikeys/issue and `apikey issue` (#270 gate fix). Blank/None = no expiry
    (None). Anything that is not a whole number from 1 to MAX_LIFETIME_DAYS raises
    ValueError with one clear message (bools and fractional numbers included)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        days = None
    elif isinstance(raw, int):
        days = raw
    elif isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
        days = int(raw.strip())
    else:
        days = None
    if days is None or not 1 <= days <= MAX_LIFETIME_DAYS:
        raise ValueError(
            f"expires_in_days must be a whole number of days from 1 to {MAX_LIFETIME_DAYS} "
            "(leave it out for no expiry)."
        )
    return datetime.utcnow() + timedelta(days=days)


def key_is_expired(key: ApiKey) -> bool:
    """True when the key's optional expiry has passed (or is now). A key with
    no expiry (expires_at NULL — every pre-existing key) never expires."""
    return key.expires_at is not None and key.expires_at <= datetime.utcnow()


def create_key(
    user_id: int, name: str = "", scopes: str = "api", expires_at: datetime | None = None
) -> tuple[ApiKey, str]:
    """Mint a key for a user. Returns (row, full_key) — the full key is never
    stored and cannot be recovered later.

    `scopes` is a space/comma-separated list of grants. "api" (the default) is
    the base inbound-API grant; "docs" additionally unlocks the document
    endpoints (upload/translate/download) used by the MCP front door. A plain
    "api" key must NOT reach the document routes.

    `expires_at` is the optional expiry (#270 r2-apiauth-10); None = no expiry.
    The org-level `apikey_max_age_days` setting, when set, caps the lifetime
    (see resolve_expiry) — a too-long or missing expiry is refused here so no
    mint surface can bypass the cap.

    Unknown grants are REFUSED here (validate_scopes) so the trap can't reach the DB
    from ANY surface — the CLI `apikey issue`, the /api mint route, `init --issue-key`
    and the /account form all funnel through this seam."""
    validate_scopes(scopes)
    expires_at = resolve_expiry(expires_at)
    full_key = "lsu_" + secrets.token_urlsafe(32)
    key = ApiKey(
        user_id=user_id,
        name=(name or "").strip() or "API key",
        prefix=full_key[:12],
        key_hash=_hash(full_key),
        scopes=normalize_scopes(scopes),
        expires_at=expires_at,
    )
    with Session(engine) as s:
        s.add(key)
        s.commit()
        s.refresh(key)
    return key, full_key


def create_remote_key(
    caller,
    owner_id: int,
    name: str = "",
    scopes: str = "docs",
    expires_at: datetime | None = None,
):
    """Remote issuance is self-service only; privileged grants stay local.
    `expires_at` obeys the same org-level maximum as local mints (resolve_expiry
    in create_key)."""
    if caller is None or caller.id != owner_id:
        raise PermissionError("You may issue keys only for your own account.")
    if scope_set(scopes) & {"admin", "scim"}:
        raise ValueError("Admin and SCIM scoped keys must be issued locally.")
    return create_key(owner_id, name=name, scopes=scopes, expires_at=expires_at)


# The full set of grant strings a key may carry. "api" is the base grant every key
# holds (always implied). "docs" unlocks the document endpoints + the whole
# remote-CLI /api surface; "keys" unlocks the key-project + Delta endpoints;
# "admin" (SPEC-full-remote-parity Wave R2) is the TOP grant — it gates the sharpest
# identity/admin verbs (GDPR export/erase, retention purge-now, apikey revoke-all)
# and, in R3, config + credentials. `admin` is mintable ONLY locally: apikey issue
# over /api refuses it, so a stolen admin-scoped key can never self-replicate.
# "scim" (Train I) is the grant an IdP's SCIM key carries: it is what unlocks the /scim/v2
# provisioning mount (Bearer + X-Loc-Token), and only when the key's owner is an active
# admin. It adds NO reach on the ordinary /api surface beyond the base "api" grant every
# key implicitly holds — the document/key/admin routes stay gated by "docs"/"keys"/"admin".
ALL_SCOPES: frozenset = frozenset({"api", "docs", "keys", "admin", "scim"})


def normalize_scopes(scopes: str) -> str:
    """Canonicalize a space/comma-separated scope list to a de-duped, sorted,
    space-joined string. "api" is always implied so every key can hit the base
    inbound API."""
    parts = {p for p in (scopes or "").replace(",", " ").split() if p}
    parts.add("api")
    return " ".join(sorted(parts))


def scope_set(scopes: str) -> set[str]:
    """The set of grants in a scope string (comma/space separated)."""
    return {p for p in (scopes or "").replace(",", " ").split() if p}


def validate_scopes(scopes: str) -> None:
    """Raise InvalidScopeError, naming the offending token(s) AND the valid vocabulary,
    if `scopes` requests any grant outside ALL_SCOPES. "api" is always implied and
    always valid; a blank/None list is fine (it becomes "api"). Enforced ONLY at mint
    (create_key) — never in normalize_scopes, so reading a key minted before this guard
    (e.g. a stored "all") is unaffected; only NEW mints are gated."""
    unknown = sorted(scope_set(scopes) - ALL_SCOPES)
    if unknown:
        raise InvalidScopeError(
            "unknown scope(s): {} (valid scopes: {}; 'api' is always implied).".format(
                ", ".join(unknown), ", ".join(sorted(ALL_SCOPES))
            )
        )


def display_prefix_for(full_key: str) -> str:
    """The stored display prefix of the ACTIVE key `full_key` belongs to, or "". A pure read
    (no last-used stamp), for the audit row of a genuine key that was refused anyway, e.g.
    because its org is suspended: the row can name the key by the prefix the product already
    shows, and never by more characters of the secret (gate audit-actor round 1, LOW-2)."""
    if not full_key:
        return ""
    with Session(_db.relief_engine_or(engine)) as s:
        row = s.exec(
            select(ApiKey).where(ApiKey.key_hash == _hash(full_key), ApiKey.active == True)  # noqa: E712
        ).first()
        return (row.prefix or "") if row else ""


def verify_key(full_key: str) -> ApiKey | None:
    """The active ApiKey a full key belongs to (with its .scopes), or None.
    The row's owning User must also be active. Best-effort last_used stamp.

    Historically returned a User; callers that want the user read
    `key.user_id` / go through deps.api_identity (which loads the User)."""
    if not full_key:
        return None
    digest = _hash(full_key)
    matched = None
    with Session(engine) as s:
        rows = s.exec(select(ApiKey).where(ApiKey.key_hash == digest)).all()
        for key in rows:
            if not key.active:
                continue
            # Expired = revoked, exactly (#270 r2-apiauth-10): an expired key is
            # skipped HERE so verify_key returns None -> the same 401 shape an
            # unknown or revoked key gets; nothing ever reveals the key existed.
            if key_is_expired(key):
                continue
            user = s.get(User, key.user_id)
            if not (user and user.active):
                continue
            # EXPUNGE the matched row with its loaded attributes INTACT, then return it.
            # The SELECT above loaded every column, and expunge (unlike commit/refresh)
            # never EXPIRES anything, so the caller can read key.user_id / key.scopes after
            # this session closes without any lazy reload.
            #
            # Flood R2-F1: the old code stamped last_used with `s.commit()` (which expires
            # the row) then `s.refresh(key)` to reload before detaching — but under sustained
            # write contention the refresh SELECT lost the writer lock, the bare `except`
            # swallowed it, and the key was returned EXPIRED + detached. api_identity's
            # `s.get(User, key.user_id)` then triggered an expired-attribute load on a closed
            # session -> DetachedInstanceError -> a 500 on the LAUNCH request (the traceback
            # through orm/state.py `_load_expired`). Detaching BEFORE any write eliminates the
            # expired-reload class entirely; the usage stamp moves to its own session below so
            # it can never touch this returned object.
            s.expunge(key)
            matched = key
            break
    if matched is None:
        return None
    # Best-effort usage stamp in its OWN session, keyed by id and OFF the returned object:
    # a stamp write that loses the lock is simply dropped (a stale last_used must never break
    # authentication) — and it CANNOT expire or detach `matched`, which is already a
    # standalone detached row. attempts=1 (fold-in): this is the AUTH HOT PATH of the very
    # flood this fixes — every authenticated request runs it — so a lost stamp must NOT block
    # for the bounded retry's worst-case (~15s at 3 attempts). One try, then drop.
    try:
        with Session(engine) as s2:

            def _stamp():
                row = s2.get(ApiKey, matched.id)
                if row is not None:
                    row.last_used = datetime.utcnow()
                    s2.add(row)

            _db.commit_with_retry(s2, stage=_stamp, attempts=1)
    except Exception:
        # debug, not warning: this is the per-request auth hot path under lock contention.
        log.debug("api key last_used stamp dropped for key id %s", matched.id, exc_info=True)
    return matched


def list_keys(user_id: int) -> list[ApiKey]:
    with Session(engine) as s:
        return list(
            s.exec(
                select(ApiKey)
                .where(ApiKey.user_id == user_id)
                .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
            ).all()
        )


def key_metadata_dict(key: ApiKey, email: str) -> dict:
    """The ONE stable, SAFE JSON row shape for enumerating a key — id, owner, label,
    scopes, timestamps and revoked state ONLY. It NEVER carries the key material or
    the sha256 hash (key_hash is intentionally excluded), so this is safe to surface
    to any admin. Emitted identically by `apikey list` (in-process + --remote) and
    GET /api/apikeys so the surfaces cannot drift (the #17 row-shape lesson). `active`
    True = live, False = revoked. `expires_at` is the optional expiry
    (#270 r2-apiauth-10; None = no expiry) and `expired` its derived state."""
    return {
        "id": key.id,
        "user": email,
        "user_id": key.user_id,
        "name": key.name,
        "prefix": key.prefix,
        "scopes": key.scopes,
        "active": key.active,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "last_used": key.last_used.isoformat() if key.last_used else None,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "expired": key_is_expired(key),
    }


def list_keys_scoped(
    session: Session, *, org_id: int | None = None, user_id: int | None = None
) -> list[dict]:
    """Enumerate issued API keys as SAFE metadata dicts (key_metadata_dict), joined to
    each owner's email — the shared seam for the system-wide `apikey list` and GET
    /api/apikeys.

    Scoping is by the OWNER's org (ApiKey has no org_id — a key belongs to the org of
    its user): `org_id` given confines to keys owned by users in THAT org, so an org
    admin can never see another org's keys (the project's most reliably-found defect
    class); org_id=None (the in-process trusted operator / gate off) enumerates every
    key. `user_id` optionally narrows to one owner. Newest-first, stable order."""
    stmt = select(ApiKey, User.email).where(ApiKey.user_id == User.id)
    if org_id is not None:
        stmt = stmt.where(User.org_id == org_id)
    if user_id is not None:
        stmt = stmt.where(ApiKey.user_id == user_id)
    stmt = stmt.order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
    return [key_metadata_dict(k, email) for k, email in session.exec(stmt).all()]


def revoke(key_id: int, user_id: int | None = None) -> bool:
    """Deactivate one key. When user_id is given (self-service path), the key
    must belong to that user — users cannot revoke other people's keys.
    Admin paths pass user_id=None."""
    with Session(engine) as s:
        key = s.get(ApiKey, key_id)
        if not key or (user_id is not None and key.user_id != user_id):
            return False
        if key.active:
            key.active = False
            s.add(key)
            s.commit()
        return True


def revoke_all_for_user(user_id: int, *, org_id, session: Session | None = None) -> int:
    """Deactivate all keys; an optional caller Session owns the atomic commit."""
    from app.services.security_client_ip_one_authority_ import _scoped_user

    with Session(engine) if session is None else nullcontext(session) as s:
        if _scoped_user(s, user_id, org_id) is None:
            return 0
        keys = s.exec(select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.active == True)).all()  # noqa: E712
        for key in keys:
            key.active = False
            s.add(key)
        # #753: consented MCP OAuth grants are API credentials too. Every caller (account
        # disable #422, erasure, admin/CLI/API revoke-all) funnels through here, so revoking
        # them in the same transaction means re-enabling an account revives nothing.
        from app.services import mcp_as

        mcp_as.revoke_all_for_user(user_id, session=s)
        if session is None:
            s.commit()
        return len(keys)
