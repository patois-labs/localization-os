"""audit: break-class helpers + log-derived anchor-break state (r6)

Documented in: docs/architecture/audit-chain.md
"""

import hashlib
import hmac as _hmac
import json
from datetime import datetime, timezone

from app.services import audit_state, settings

AUDIT_WRITE_ATTEMPTS = 2


# Contention posture (flood R3-F1). The audit row is written AFTER call_next, on EVERY
# mutating request, while the app-wide `_chain_lock` is held. Two decisions follow:
#
#   AUDIT_WRITE_ATTEMPTS — FEWER than db.WRITE_RETRY_ATTEMPTS (3). A long retry here would
#   serialize every other mutating request's audit write behind this lock (a self-inflicted
#   stall on the whole app), and the request the row describes has ALREADY succeeded. Two
#   attempts (1 initial + 1 JITTERED retry) keeps the desync benefit of jitter — the point
#   of the retry is to break a thundering herd, and that needs the second, jittered try —
#   while bounding the worst-case chain-lock hold. (The R3-F1 stale-snapshot lock class
#   fails IMMEDIATELY with SQLITE_BUSY rather than waiting out busy_timeout, so the realistic
#   cost of a shed write is ~one <1s backoff, not attempts*BUSY_TIMEOUT.) This mirrors the
#   apikeys usage-stamp attempts=1 reasoning (a best-effort write on the auth/mutation hot
#   path), pitched one higher because a dropped audit row matters more than a stale last_used.
#   Governs the ROW INSERT only; the _write_anchor update uses attempts=1 (it is stale-
#   tolerant + self-healing — see _write_anchor), so the worst-case lock hold is ~2×+1.
#
#   CONTENTION POSTURE — under sustained contention the audit row write can be SHED after
#   the bounded retry. FAIL-OPEN (the default) DROPS the row with one loud WARN and lets the
#   mutating request PROCEED unaudited: availability over audit completeness. FAIL-CLOSED is
#   the opposite posture (a shed audit write raises db.WriteContentionError, so the request
#   surfaces an honest retryable 503 rather than proceeding unaudited). The shed-handling
#   branch in log() routes on the snapshot fail_open() returns.
#
#   The maintainer RATIFIED this as a switchable posture (2026-08-13), so its home is now the
#   `audit_fail_open` Setting row (default "1" = fail-open), read via fail_open() below —
#   exactly like audit_enabled's `audit_enabled` row. It replaces the former `_AUDIT_FAIL_OPEN`
#   module placeholder. The posture is SNAPSHOT at the top of log() (before _chain_lock and
#   the write-flood the degrade reacts to), so the degrade decision reads a LOCAL bool and
#   never issues a settings read while the SQLite writer lock is contended — see log().


def enabled() -> bool:
    return settings.get("audit_enabled") == "1"


def fail_open() -> bool:
    """The contention posture (maintainer-ratified 2026-08-13): True (default) = FAIL-OPEN, a
    shed audit write DROPS the row + WARN and the request PROCEEDS unaudited; False =
    FAIL-CLOSED, a shed write raises db.WriteContentionError (an honest retryable 503).

    Backed by the `audit_fail_open` Setting row (default "1"), read through settings.get's
    read-through cache. MUST be SNAPSHOT at the top of log() (before the lock / the contention
    window): the cache is PER-KEY, and a MISS on this key opens a DB session (settings.get's
    miss path is unguarded), so reading the posture DURING a writer-lock-contention degrade
    would itself be lock-exposed. Snapshotting up front makes the degrade decision a local
    bool with no DB touch — that is the whole point of the switch. The snapshot call itself is
    wrapped in log() so a cold-key read against a locked DB defaults to fail-open rather than
    raising out of the audit tail (which would 500 an otherwise-successful mutating request —
    a failure mode the old module flag could not have)."""
    return settings.get("audit_fail_open") != "0"


def _mac_key() -> bytes:
    """Dedicated persistent audit key (never stored in the DB)."""
    return audit_state.row_key()


def _compute_hash(
    prev_hash: str,
    iso_ts: str,
    actor: str,
    action: str,
    target: str,
    detail: str,
    ip: str,
    *,
    row_id=None,
    customer_id=None,
) -> str:
    payload = json.dumps(
        ["audit-v2", prev_hash, iso_ts, actor, action, target, detail, ip, customer_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return _hmac.new(
        audit_state.row_key(row_id), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _v1_hash(prev_hash, iso_ts, actor, action, target, detail, ip, *, row_id):
    """Ambiguous historical encoding, accepted only inside the pinned legacy epoch."""
    payload = "|".join([prev_hash, iso_ts, actor, action, target, detail, ip])
    return _hmac.new(
        audit_state.row_key(row_id), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _legacy_hash(
    prev_hash: str, iso_ts: str, actor: str, action: str, target: str, detail: str, ip: str
) -> str:
    """Pre-upgrade unkeyed hash — accepted only for rows older than the first
    keyed row, so existing installs keep verifying."""
    payload = "|".join([prev_hash, iso_ts, actor, action, target, detail, ip])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _anchor_mac(head_id: int, head_hash: str, keyed_from: int) -> str:
    return _hmac.new(
        _mac_key(), f"{head_id}:{head_hash}:{keyed_from}".encode(), hashlib.sha256
    ).hexdigest()


def _as_id(v) -> int | None:
    """Coerce an id-ish value to a positive int within SQLite's signed-64-bit
    range, else None. Guards session.get from a raw OverflowError on a huge
    digit-string typo/forged value (mirrors the id coercion used elsewhere)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 0 < v <= (2**63 - 1) else None
    # Safe parse: str.isdigit() is True for Unicode digits int() rejects (e.g. the
    # superscript '2', U+00B2 -> ValueError), which slipped past the isdigit gate
    # straight into int() and raised. The overflow arm was already covered by the
    # bound below; this closes the ValueError arm the same way (-> None).
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n <= (2**63 - 1) else None


def _parse_iso(raw):
    """An ISO timestamp string -> naive-UTC datetime, or None on absence/garbage. A
    tz-aware value is normalized to naive UTC so it compares against a naive utcnow()."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _chain_status_mac(body: str) -> str:
    """MAC over the EXACT stored status body string (keyed off the current audit key,
    like the anchor). MACing the verbatim serialized string — not a re-derived canonical
    form — sidesteps any re-serialization ambiguity: write and read MAC the identical bytes."""
    return _hmac.new(
        _mac_key(), ("audit-chain-status:" + body).encode("utf-8"), hashlib.sha256
    ).hexdigest()


_CHAIN_BROKEN_ACTION = "audit.chain_broken"


_ANCHOR_RESET_ACTION = "audit.anchor_reset"


# --- break-class helpers + log-derived anchor-break state (R6) ---------------- #
def _format_chain_broken_detail(first_bad, checked, break_class) -> str:
    """Single-source the ``audit.chain_broken`` detail string. The ``class=`` token is
    LOAD-BEARING (R6: _anchor_break_open parses it back out of the row), so the format is
    written in exactly ONE place and read by exactly one parser below."""
    return f"first_bad_id={first_bad} checked={checked} class={break_class}"


LEGACY_RESTORE_HINT = (
    "A keyed pre-v2 row sits above the pinned .audit_format legacy boundary (a DB-only "
    "legacy restore after an empty-DB verification). Reset-anchor does not change the "
    "boundary. Only on a host whose restored chain you have independently validated, "
    "stop all writers, preserve the trust files, delete .audit_format, and run audit "
    "verify before restarting writers. Never use this to silence an unexplained failure. "
    "See docs/BACKUP.md for recovery and limits."
)


def _parse_break_class(detail) -> str | None:
    """Extract the ``class=<token>`` value from a chain_broken detail (R6); None if absent."""
    for tok in (detail or "").split():
        if tok.startswith("class="):
            return tok[len("class=") :] or None
    return None
