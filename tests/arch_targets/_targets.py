"""Shared helpers for the backend architecture TARGET tests (2026-10-22).

These tests are written BEFORE the work they describe. Each target_*.py states one proposed backend
change as checkable behavior and is expected to FAIL on today's main; it turns green when the change
lands. They live outside the verify_*.py glob on purpose, so the release gate
(tests/test_verify_suites.py) is unaffected until a target is promoted into a real verify suite.

A target script runs standalone (local-tms venv python), prints one line per check and ends with
"TARGET <id> <name>: PASS" or "TARGET <id> <name>: FAIL (<passed>/<total>)"; exit code 0 only on
PASS. Run them all with tests/arch_targets/run_targets.py.

"""

from __future__ import annotations

import sys
import time
import traceback

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> bool:
    """Run one check. fn returns True/False or (bool, detail), or raises (a FAIL, with why)."""
    t0 = time.time()
    try:
        out = fn()
        ok, detail = out if isinstance(out, tuple) else (bool(out), "")
    except NotImplementedError as e:
        ok, detail = False, f"not implemented: {e}"
    except AssertionError as e:
        ok, detail = False, str(e) or "assertion failed"
    except Exception as e:
        ok, detail = (
            False,
            f"{type(e).__name__}: {e} | {traceback.format_exc(limit=2).splitlines()[-1]}",
        )
    _RESULTS.append((name, ok, detail))
    tail = " - " + detail if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} ({time.time() - t0:.1f}s){tail}", flush=True)
    return ok


def need(module, attr: str):
    """The seam a target needs, or NotImplementedError naming it."""
    if not hasattr(module, attr):
        raise NotImplementedError(f"{module.__name__}.{attr}")
    return getattr(module, attr)


def finish(tid: str, name: str) -> None:
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    verdict = "PASS" if total and passed == total else f"FAIL ({passed}/{total})"
    print(f"TARGET {tid} {name}: {verdict}", flush=True)
    sys.exit(0 if verdict == "PASS" else 1)


def wait_until(pred, timeout: float, every: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def pct(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))] if vals else None
