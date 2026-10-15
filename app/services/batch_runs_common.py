"""batch_runs: shared leaf helpers (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from contextlib import contextmanager

from app import db
from app.models import (
    RunStatus,
    WorkflowRun,
)

log = logging.getLogger("loc_tms.batch_runs")


_MAX_PARALLEL_CEIL = 8


_MAX_PARALLEL_DEFAULT = 3


_ACTIVE = (RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.PENDING, RunStatus.QUEUED)


# Run-level concurrency clamp (mirrors orchestrator._mt_workers' clamp idiom at the run
# level). max_parallel is bounded 1..8 so a per-batch override from the form/flag can never
# blow past the conservative ceiling (see the concurrency-budget note in the module docstring).
# The active (not-yet-terminal) run states — a run in one of these is still in flight.
# QUEUED is one of them (gate A round 3): a fan-out's queued row is work over that
# (workflow, document, locale) that has not been given a slot yet, so a batch member must
# SKIP it as already-in-flight rather than launch a rival execution over the same segment
# set, and the rollup must not read a queued locale as finished. Derived from the
# orchestrator's list, which `verify_batch_runs` pins as identical (batch_runs imports the
# orchestrator lazily by convention, so this cannot be a module-level reference to it).
class BatchError(ValueError):
    """A bad batch request (empty/invalid locales, missing workflow/document). The
    web/api routes map it to a 400 and the CLI to a usage error — the single-run
    surfaces' own validation posture, mirrored for the batch trigger."""


class BatchBudgetExhausted(BatchError):
    """CV-2: a new batch is refused because the project is at its monthly USD cap. A
    BatchError SUBCLASS so every existing `except BatchError` still catches it, but the
    HTTP batch surfaces catch it FIRST and map it to 429 (M2: unified with the single-
    run seam's 429, which the docs promise) rather than the generic 400."""


class BatchMissing(BatchError):
    """A batch disappeared while its caller was waiting or reading the result."""

    def __init__(self, batch_id):
        super().__init__(f"batch_missing: Batch {batch_id} no longer exists")


def default_max_parallel() -> int:
    """The configured run-level concurrency default (batch_max_parallel setting),
    clamped 1..8. Read like orchestrator._mt_workers reads mt_concurrency."""
    from app.services import settings

    try:
        n = int(settings.get("batch_max_parallel") or _MAX_PARALLEL_DEFAULT)
    except (TypeError, ValueError):
        n = _MAX_PARALLEL_DEFAULT
    return max(1, min(_MAX_PARALLEL_CEIL, n))


def clamp_max_parallel(value) -> int:
    """Clamp a per-batch override to 1..8; a blank/None/garbage value falls back to the
    configured default. The form/flag value is never trusted past the ceiling."""
    if value is None or value == "":
        return default_max_parallel()
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default_max_parallel()
    return max(1, min(_MAX_PARALLEL_CEIL, n))


@contextmanager
def _batch_result_session(session):
    """Use bookkeeping capacity for the result, then restore the caller bind."""
    original = session.bind
    relief = db.pool_has_no_capacity() and original is db.engine and not session.in_transaction()
    if relief:
        session.close()
        session.bind = db._ensure_emergency_engine()
    try:
        yield session
    finally:
        if relief:
            session.close()
            session.bind = original


# --- latest-per-locale helpers (5b: reruns give a locale >1 member) ---------------
def _members_by_locale_latest(members: list[WorkflowRun]) -> dict[str, WorkflowRun]:
    """The LATEST member run per locale (highest id wins — the most recent attempt).
    `members` MUST be ordered by id ascending (batch_members' order), so last-write-wins
    yields the highest id. Before 5b every locale had exactly one member (this collapses
    to the identity map); 5b's re-run gives a locale multiple members and the matrix +
    every rollup count read the LATEST attempt, never double-counting the superseded run."""
    latest: dict[str, WorkflowRun] = {}
    for m in members:  # ascending id -> the final assignment is the highest-id member
        latest[m.target_locale] = m
    return latest


def _ordinals(members: list[WorkflowRun]) -> dict[int, int]:
    """{run_id: attempt-ordinal} — a member's 1-based position among the SAME locale's
    members by id. attempt 1 is the original launch; attempt 2+ is a re-run. The LATEST
    member's ordinal equals the locale's total attempt count (the 'x2' badge source)."""
    seen: dict[str, int] = {}
    out: dict[int, int] = {}
    for m in members:  # ascending id
        seen[m.target_locale] = seen.get(m.target_locale, 0) + 1
        out[m.id] = seen[m.target_locale]
    return out


def _clear_coverage_notes(notes):
    import re

    return re.sub(r"Not launched: [^.]*\.\s*", "", notes or "").strip() or None


def _dispatcher_owner() -> dict:
    return {"host": socket.gethostname(), "pid": os.getpid()}


def _dispatcher_confirmed_dead(owner) -> bool:
    """Only a missing process on this host proves death. PID reuse holds conservatively.

    Windows must use OpenProcess, never os.kill(pid, 0), which can terminate the process.
    Unknown hosts, legacy rows, malformed owners and permission failures cannot prove death.
    """
    if not isinstance(owner, dict) or owner.get("host") != socket.gethostname():
        return False
    pid = owner.get("pid")
    if type(pid) is not int or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: no such PID
        try:
            return k32.WaitForSingleObject(handle, 0) == 0  # exited, not WAIT_TIMEOUT/FAILED
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _step_identities(steps) -> list[dict]:
    """The column-axis identity of a workflow's ordered steps: the fields the matrix keys a
    column on. NOT the run state — just the plan (order/name/type). Shared by the requested-
    workflow baseline and each member's own variant plan."""
    return [
        {
            "step_id": st.id,
            "order_index": st.order_index,
            "name": st.name or "",
            "step_type": st.step_type,
        }
        for st in steps
    ]


_TERMINAL = (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)


# --- the language x steps matrix (5b) — pure fold over batch_as_dict(detail=True) -----
def _col_key(s: dict) -> str:
    """A column's stable identity: (order_index, step_type, name). Two steps that share this
    triple ARE the same column; anything else is a distinct column. This is what makes the
    axis honest under structural divergence — a renamed / inserted variant step becomes its
    OWN column rather than sharing a header with a different step, so a cell can never sit
    under the wrong column (SPEC Phase 5 column-axis rule)."""
    return f"{s.get('order_index')}|{s.get('step_type')}|{s.get('name', '')}"


def _parse_skipped_already_running(notes: str) -> set:
    """The locales named in the 'Skipped (already running elsewhere): a, b.' notes segment."""
    import re

    m = re.search(r"Skipped \(already running elsewhere\): ([^.]+)\.", notes or "")
    if not m:
        return set()
    return {x.strip() for x in m.group(1).split(",") if x.strip()}


def _unlaunched_reason(loc: str, view: dict, skipped: set) -> str:
    """An honest per-row reason a requested locale has no member, from the batch notes."""
    if loc in skipped:
        return "already running elsewhere"
    notes = view.get("notes") or ""
    if view.get("spend_stopped"):
        return "not launched - spend cap"
    if "Fail-fast" in notes:
        return "not launched - fail-fast"
    return "not launched"


def build_matrix(view: dict) -> dict:
    """Fold a batch_as_dict(detail=True) view into the rows(locales) x columns(steps) matrix
    the monitor renders — PURE (no DB), so the web template, the web poll, and the CLI ASCII
    table all build the SAME grid from the SAME payload and can never drift.

    COLUMN AXIS (honest under divergence): the ordered UNION of column keys (see _col_key)
    from the requested workflow's steps (the baseline, so even an all-unlaunched batch shows
    a pipeline) plus every latest member's own variant steps, sorted by (order_index,
    step_type, name). A member's cells are placed STRICTLY by matching its own steps' keys, so
    a cell is only ever under the column it exactly equals (never misaligned). A member whose
    key set differs from the requested baseline is flagged `diverges`.

    ROWS: one per REQUESTED locale (including un-launched ones — an honest row carrying the
    reason). A launched row shows its LATEST member (with the attempt count / re-run marker),
    each cell {status, score, gate, visits, latency_ms} or None where the member lacks that
    column."""
    requested = view.get("requested_locales", []) or []
    req_steps = view.get("requested_steps", []) or []
    members = view.get("members", []) or []
    latest = {m["locale"]: m for m in members if m.get("is_latest")}

    columns: dict[str, dict] = {}

    def _add(s: dict) -> None:
        k = _col_key(s)
        if k not in columns:
            columns[k] = {
                "key": k,
                "order_index": s.get("order_index"),
                "name": s.get("name", ""),
                "step_type": s.get("step_type"),
            }

    for s in req_steps:
        _add(s)
    canonical_keys = set(columns.keys())
    for m in latest.values():
        for s in m.get("step_runs", []):
            _add(s)
    col_list = sorted(
        columns.values(), key=lambda c: (c["order_index"], c["step_type"] or "", c["name"] or "")
    )

    skipped = _parse_skipped_already_running(view.get("notes") or "")
    rows = []
    for loc in requested:
        m = latest.get(loc)
        if m is None:
            rows.append(
                {
                    "locale": loc,
                    "run_id": None,
                    "status": "unlaunched",
                    "attempt": 0,
                    "rerun": False,
                    "unlaunched": True,
                    "terminal": False,
                    "resume": False,
                    "diverges": False,
                    "reason": _unlaunched_reason(loc, view, skipped),
                    "cells": {c["key"]: None for c in col_list},
                }
            )
            continue
        cells = {c["key"]: None for c in col_list}
        mkeys = set()
        for s in m.get("step_runs", []):
            k = _col_key(s)
            mkeys.add(k)
            cells[k] = {
                "status": s.get("status"),
                "score": s.get("score"),
                "gate": s.get("gate"),
                "visits": s.get("visits", 0),
                "step_id": s.get("step_id"),
                "latency_ms": s.get("latency_ms"),
            }
        attempt = m.get("attempt", 1)
        rows.append(
            {
                "locale": loc,
                "run_id": m.get("run_id"),
                "status": m.get("status"),
                "attempt": attempt,
                "rerun": attempt > 1,
                "unlaunched": False,
                "terminal": m.get("status") in _TERMINAL,
                "resume": m.get("status") == RunStatus.PAUSED,
                "diverges": mkeys != canonical_keys,
                "reason": "",
                "cells": cells,
            }
        )
    diverged = any(r["diverges"] for r in rows) or set(columns.keys()) != canonical_keys
    return {"columns": col_list, "rows": rows, "diverged": diverged}
