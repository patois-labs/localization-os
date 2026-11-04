"""Small cross-process shared state for N web processes (S3c, ADR 0097).

Three stores that live in one process's memory are wrong once several web processes serve
one install: the API rate limiter, the SAML assertion replay guard and the one-time secret
stash. When `config.multi_web()` is true they use the `sharedstate` table through this
module; a single-process install never calls it and never writes the table.

Every function opens its own short Session/connection on `app.db.engine` and uses portable
SQL (SQLite 3.35+ and Postgres: INSERT ... ON CONFLICT DO UPDATE and DELETE ... RETURNING),
so each operation is one atomic statement. A busy SQLite writer lock or a Postgres
deadlock is retried through `db.retry_conflicts` instead of surfacing as a 500.

Keys are namespaced text (max 255): "rl:<identity>:<window>", "saml:<sha256>",
"stash:<nonce>". `expires_at` is unix epoch seconds; an expired row reads as absent and is
deleted by `sweep()`, which put/claim/incr run opportunistically at most once per 60 s per
process.

Documented in: docs/architecture/deployment.md
"""

import logging
import threading
import time

from sqlalchemy import text

log = logging.getLogger(__name__)

SWEEP_EVERY_S = 60.0

_sweep_lock = threading.Lock()
_last_sweep = {"at": 0.0}

_UPSERT_COUNTER = text(
    "INSERT INTO sharedstate (key, value, counter, expires_at) "
    "VALUES (:k, '', :by, :exp) "
    "ON CONFLICT (key) DO UPDATE SET "
    "counter = CASE WHEN sharedstate.expires_at <= :now "
    "THEN excluded.counter ELSE sharedstate.counter + excluded.counter END, "
    "expires_at = CASE WHEN sharedstate.expires_at <= :now "
    "THEN excluded.expires_at ELSE sharedstate.expires_at END "
    "RETURNING counter, expires_at"
)
_UPSERT_VALUE = text(
    "INSERT INTO sharedstate (key, value, counter, expires_at) "
    "VALUES (:k, :v, 0, :exp) "
    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, counter = 0, "
    "expires_at = excluded.expires_at"
)
_TAKE = text("DELETE FROM sharedstate WHERE key = :k RETURNING value, expires_at")
_SWEEP = text("DELETE FROM sharedstate WHERE expires_at <= :now")


def _db():
    from app import db  # local: app.db opens the engine at import; keep this module light

    return db


def _run(fn, action: str):
    """Run `fn(connection)` in its own transaction, retrying a lost SQLite lock or a
    Postgres deadlock."""
    db = _db()

    def tx():
        with db.engine.begin() as cx:
            return fn(cx)

    return db.retry_conflicts(tx, action)


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def incr_window(key: str, by: int = 1, ttl_s: float = 60.0, now: float | None = None):
    """Atomically add `by` to the counter of `key`; return (counter, expires_at).

    A missing or expired row starts a new window (counter = by, expires_at = now + ttl_s);
    a live row keeps its original expiry, so the window is fixed, not sliding."""
    t = _now(now)
    params = {"k": key, "by": int(by), "exp": t + float(ttl_s), "now": t}

    def go(cx):
        row = cx.execute(_UPSERT_COUNTER, params).one()
        return int(row[0]), float(row[1])

    out = _run(go, "shared_state.incr")
    _maybe_sweep(t)
    return out


def incr(key: str, by: int = 1, ttl_s: float = 60.0, now: float | None = None) -> int:
    """incr_window without the expiry: the new counter value."""
    return incr_window(key, by, ttl_s, now)[0]


def claim(key: str, ttl_s: float, now: float | None = None) -> bool:
    """True when this call created a live entry (first use of `key`), False when a live
    entry already existed (a replay)."""
    return incr_window(key, 1, ttl_s, now)[0] == 1


def put(key: str, value: str, ttl_s: float, now: float | None = None) -> None:
    """Store `value` under `key` for `ttl_s` seconds, replacing any earlier row."""
    t = _now(now)
    params = {"k": key, "v": value, "exp": t + float(ttl_s)}
    _run(lambda cx: cx.execute(_UPSERT_VALUE, params), "shared_state.put")
    _maybe_sweep(t)


def take(key: str, now: float | None = None) -> str | None:
    """Delete `key` and return its value once; None when absent or expired. The delete is
    one atomic statement, so two processes racing for the same key get it exactly once."""
    t = _now(now)

    def go(cx):
        row = cx.execute(_TAKE, {"k": key}).first()
        return None if row is None else (str(row[0]), float(row[1]))

    got = _run(go, "shared_state.take")
    if got is None or got[1] <= t:
        return None
    return got[0]


def sweep(now: float | None = None) -> int:
    """Delete expired rows; return how many went."""
    t = _now(now)
    return _run(lambda cx: cx.execute(_SWEEP, {"now": t}).rowcount or 0, "shared_state.sweep")


def _maybe_sweep(now: float) -> None:
    """Best-effort sweep at most once per SWEEP_EVERY_S per process (monotonic clock)."""
    mono = time.monotonic()
    if _last_sweep["at"] and mono - _last_sweep["at"] < SWEEP_EVERY_S:
        return
    if not _sweep_lock.acquire(blocking=False):
        return
    try:
        _last_sweep["at"] = mono
        try:
            sweep(now)
        except Exception as exc:  # a skipped sweep simply runs next time
            log.warning("shared_state sweep skipped (%s)", type(exc).__name__)
    finally:
        _sweep_lock.release()
