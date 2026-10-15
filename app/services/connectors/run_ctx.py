"""Per-run switches a project automation sets around a connector run.

Two things the shared flow engine (flow.py) needs to know that are not part of a connector's
own configuration:

* FULL PULL: ignore the stored content hashes for this run, so every (record, locale) pair is
  translated (a 100% TM hit is free) and written again. A scheduled or manual "full" run of a
  project automation sets it. The review gate, the draft/live posture, parking of hand-edited
  copies and the write ceiling are NOT touched: a full pull only changes which pairs are
  considered "unchanged".
* CLAIM: when one connection feeds several projects, the run asks a claim object whether it
  may take an item (`claim.take(item_id)`); an item another project already owns is skipped and
  reported, never written over. The object is supplied by app/services/project_automation.py.

Both live in ContextVars so a run that sets nothing behaves byte-identically to before: the
defaults are "delta" and "no claim".

Documented in: docs/architecture/project-automation.md
"""

from __future__ import annotations

import contextlib
import contextvars

_FULL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "loc_connector_full_pull", default=False
)
_CLAIM: contextvars.ContextVar = contextvars.ContextVar("loc_connector_claim", default=None)


def full_pull() -> bool:
    """True while a full-pull run is active (stored content hashes do not suppress a pair)."""
    return bool(_FULL.get())


def claim():
    """The active claim object (has `take(item_id, locale) -> bool`), or None."""
    return _CLAIM.get()


_DONE: contextvars.ContextVar = contextvars.ContextVar("loc_connector_full_done", default=None)


def full_done_has(item_id) -> bool:
    """True when this FULL run already finished `item_id` earlier in the same full cycle (the
    cursor that lets a scope larger than the write ceiling be walked over several runs)."""
    done = _DONE.get()
    return done is not None and str(item_id) in done


def mark_done(item_id) -> None:
    """Record that every pair of `item_id` was written in this full run."""
    done = _DONE.get()
    if done is not None:
        done.add(str(item_id))


@contextlib.contextmanager
def scope(*, full: bool = False, claim_obj=None, full_done=None):
    """Set the per-run switches for the duration of one connector run. `full_done` is a mutable
    set of item ids finished in the current full cycle (read to skip them, added to as items
    complete)."""
    t_full = _FULL.set(bool(full))
    t_claim = _CLAIM.set(claim_obj)
    t_done = _DONE.set(full_done if full else None)
    try:
        yield
    finally:
        _DONE.reset(t_done)
        _CLAIM.reset(t_claim)
        _FULL.reset(t_full)
