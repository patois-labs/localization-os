"""Engine policy for fix train A.

Bookkeeping never refuses for pool capacity: _finalize, _sync_wait_error,
_mark_queue_launch_failed, failure notes, audit tails and boot reconcile use pooled
sessions with headroom,
otherwise db.emergency_session(), the UNBOUNDED, BLOCKING emergency engine.
Bookkeeping never takes a relief permit. Wait reconcile follows this same policy,
with at most one checkout per two seconds.
Relief permits belong only to operator REQUEST paths admitted by the doorman:
_is_relief, _relief_slots and request reset. Only those paths begin relief scope.

Documented in: docs/architecture/data-and-migrations.md
"""

from contextlib import contextmanager

from sqlmodel import Session

from app import db


@contextmanager
def scope():
    token = db._bookkeeping_scope.set(True)
    try:
        yield
    finally:
        db._bookkeeping_scope.reset(token)


@contextmanager
def session():
    """A must-not-fail bookkeeping Session, open for the WHOLE `with` body.

    The bookkeeping scope stays set for the entire body (gate A round 9 LOW-3: callers no
    longer have to enter `scope()` themselves for helper Sessions to route correctly), and
    the engine is chosen ONCE, here: the pooled engine with headroom, otherwise the
    unbounded blocking emergency engine. The Session is PINNED to that choice
    (`relief_pin`), so its own reads never switch connections mid-transaction if the pool
    fills underneath it (LOW-1); bookkeeping never takes a relief permit."""
    with scope():
        s = db.emergency_session() if db.pool_has_no_capacity() else Session(db.engine)
        try:
            s.connection()
        except Exception as exc:
            s.close()
            if not db.is_pool_timeout_error(exc):
                raise
            s = db.emergency_session()
            s.connection()
        s.info["relief_pin"] = True
        try:
            yield s
        finally:
            s.close()
