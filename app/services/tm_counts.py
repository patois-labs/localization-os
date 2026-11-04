"""Cached TM row counts for the per-request project surfaces (S1 item 6, gate G5).

`tm_common.count` / `tm_common.ft_count` run a live `SELECT count(*)` over the segment /
full-text TM per project. On a 1.7M-unit TM that is seconds on SQLite, and the project GET,
the project page and the customer rollup ran it on EVERY hit. This module keeps the exact
COUNT but reuses it:

  * Process-local cache keyed by (kind, project_id); kind is "seg" (SegmentTM) or "ft"
    (FullTextTM). That is the full scope the underlying counts compute (project_id only).
  * Stale-while-revalidate: a cached value is returned at once; once it is older than
    TTL_SECONDS ONE background thread refreshes it (single-flight per key, never N COUNTs).
  * The first-ever miss computes synchronously under a per-key lock, so concurrent first
    callers wait on one query instead of running N.
  * `invalidate(project_id)` drops the entries, so the next read is a fresh COUNT. Called
    from the TM import and TM delete paths; every other TM write (approve write-back,
    full-text store) is bounded by the TTL.
  * LRU capped at MAX_KEYS; all state is guarded by one lock held only for dict work.

The cached value is always a real COUNT; the API shape of callers is unchanged. Each worker
process has its own cache (counts may differ by up to TTL_SECONDS between workers).

Documented in: docs/architecture/tm-and-glossary.md
"""

import logging
import threading
import time
from collections import OrderedDict

from sqlmodel import Session

log = logging.getLogger(__name__)

TTL_SECONDS = 30.0
MAX_KEYS = 4096

_SEG = "seg"
_FT = "ft"

_lock = threading.Lock()
# key -> (value, computed_at monotonic); most recently used last.
_cache: "OrderedDict[tuple, tuple[int, float]]" = OrderedDict()
# key -> [lock, users]: single-flight lock for a synchronous first miss.
_first: dict = {}
# keys with a background refresh in flight.
_refreshing: set = set()
# bumped by every invalidate; a compute that started before one never stores its result.
_epoch = 0


def _compute(kind: str, session: Session, project_id) -> int:
    # Looked up at call time so a patched tm.count / tm.ft_count is honoured.
    from app.services import tm

    return tm.count(session, project_id) if kind == _SEG else tm.ft_count(session, project_id)


def _store(key: tuple, value: int, started_epoch: int) -> None:
    with _lock:
        if started_epoch != _epoch:
            return  # an invalidate raced the COUNT: the next read recomputes
        _cache[key] = (value, time.monotonic())
        _cache.move_to_end(key)
        while len(_cache) > MAX_KEYS:
            _cache.popitem(last=False)


def _refresh_worker(kind: str, project_id) -> None:
    key = (kind, project_id)
    try:
        from app import db

        with _lock:
            started = _epoch
        with Session(db.engine) as s:
            value = _compute(kind, s, project_id)
        _store(key, value, started)
    except Exception:
        log.warning("tm_counts: background refresh failed for %s", key, exc_info=True)
    finally:
        with _lock:
            _refreshing.discard(key)


def _spawn_refresh(kind: str, project_id) -> None:
    key = (kind, project_id)
    with _lock:
        if key in _refreshing:
            return
        _refreshing.add(key)
    try:
        threading.Thread(
            target=_refresh_worker, args=(kind, project_id), daemon=True, name="tm-count-refresh"
        ).start()
    except Exception:  # thread exhaustion: allow a later retry
        with _lock:
            _refreshing.discard(key)


def _get(kind: str, session: Session, project_id) -> int:
    key = (kind, project_id)
    with _lock:
        ent = _cache.get(key)
        if ent is not None:
            _cache.move_to_end(key)
    if ent is not None:
        value, ts = ent
        if time.monotonic() - ts >= TTL_SECONDS:
            _spawn_refresh(kind, project_id)
        return value

    # First miss: one query per key, other callers wait on it.
    with _lock:
        slot = _first.setdefault(key, [threading.Lock(), 0])
        slot[1] += 1
    try:
        with slot[0]:
            with _lock:
                ent = _cache.get(key)
                started = _epoch
            if ent is not None:
                return ent[0]
            value = _compute(kind, session, project_id)
            _store(key, value, started)
            return value
    finally:
        with _lock:
            slot[1] -= 1
            if slot[1] == 0:
                _first.pop(key, None)


def count(session: Session, project_id) -> int:
    """Cached `tm.count(session, project_id)`."""
    return _get(_SEG, session, project_id)


def ft_count(session: Session, project_id) -> int:
    """Cached `tm.ft_count(session, project_id)`."""
    return _get(_FT, session, project_id)


def invalidate(project_id=None) -> None:
    """Forget the cached counts of one project (both TM kinds), or of every project when
    `project_id` is None. Cheap and safe to call from any thread, even on a failed write."""
    global _epoch
    with _lock:
        _epoch += 1
        if project_id is None:
            _cache.clear()
        else:
            _cache.pop((_SEG, project_id), None)
            _cache.pop((_FT, project_id), None)


def _reset_for_tests() -> None:
    global _epoch
    with _lock:
        _epoch += 1
        _cache.clear()
        _refreshing.clear()
