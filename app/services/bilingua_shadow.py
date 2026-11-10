"""Bilingua shadow runner: run the Bilingua engine beside the Python filter on a new upload.

Documented in: docs/architecture/translation-pipeline.md

WHY. Before any format is switched to Bilingua
(`bilingua_formats`), an install can list it in `bilingua_shadow_formats`. Every NEW upload of
such a format is parsed by the Python filter exactly as always; this module then parses the same
bytes with the Bilingua adapter in a background thread and compares the two results. The only
output is ONE log line per upload (logger `app.services.bilingua_shadow`) holding counts, flags
and hashes: how many units each engine found, whether the unit texts and the inline tokens are
equal, whether an untouched Bilingua rebuild returns the upload byte for byte, the refusal CLASS
if Bilingua refused the file. That is how a prod install can show "it works" on real customer
files before an operator switches a format.

THE LAWS (each one has a test in tests/verify_bilingua_routing.py)
  * It never changes what is stored: it reads the upload bytes and a copy of the Python unit
    list, and writes nothing (no skeleton key, no blob, no row, no setting).
  * It never logs content: no file name, no text, no exception message (an engine message can
    quote the file). Only integers, booleans, short hashes of the unit lists, the format
    name and an exception CLASS or error code reduced to `[A-Za-z0-9_.-]`.
  * It never fails or slows an upload: ingest calls `prepare` after the Python parse (one
    settings read when the format is not shadowed), then hands the job to POOL through
    ai_optout.submit, so the upload's AI scope and cost sink travel to the daemon thread in a
    copied context like every other pool fan-out (#364); every error is swallowed (and counted).
  * It is bounded: one parse at a time, at most MAX_PENDING queued, files over MAX_BYTES are
    skipped (counted), so a bulk upload cannot pile up threads or memory.
  * Off by default: `bilingua_shadow_formats` is empty, and then `submit` returns at once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time

log = logging.getLogger(__name__)

#: Files larger than this are not shadowed (counted as `skipped_large`).
MAX_BYTES = 16 * 1024 * 1024
#: Uploads waiting for the single shadow worker; more are dropped (counted as `skipped_busy`).
MAX_PENDING = 4

_TOKEN = re.compile("⟦/?[a-z]?\\d+⟧")  # the LocOS inline token grammar
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")

_state = threading.Lock()  # guards _pending and _stats
_run = threading.Lock()  # one shadow parse at a time
_idle = threading.Event()
_idle.set()
_pending = 0
_stats: dict = {}
_recent: list = []  # the last records, newest last (a test and diagnostic aid, bounded)
_RECENT_MAX = 50


def _bump(name: str, n: int = 1) -> None:
    with _state:
        _stats[name] = _stats.get(name, 0) + n


def stats() -> dict:
    """Counters since the process started (submitted, done, refused, errors, skipped_*)."""
    with _state:
        return dict(_stats)


def recent() -> list:
    """The last shadow records (what was logged), oldest first."""
    with _state:
        return [dict(r) for r in _recent]


def reset() -> None:
    """Forget counters and records (tests)."""
    with _state:
        _stats.clear()
        _recent.clear()


def wait_idle(timeout: float = 30.0) -> bool:
    """Block until no shadow work is queued or running (tests). True when idle."""
    return _idle.wait(timeout)


def _safe_name(value) -> str:
    return _SAFE.sub("", str(value))[:40] or "unknown"


def _sha(rows) -> str:
    blob = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()[:16]


def _adapter_for(fmt: str):
    """The Bilingua filter for `fmt` (a seam: tests replace it with a fake), or None when this
    install cannot run it (no module, or Bilingua does not parse the format)."""
    from app.filters import bilingua_adapter

    if fmt not in (getattr(bilingua_adapter, "_FORMATS", None) or ()):
        return None
    if not bilingua_adapter.available():
        return None
    return bilingua_adapter.BilinguaFilter(fmt)


def _units(parsed) -> list:
    """[(p_index, text)] from a parse result or a skeleton's unit dicts."""
    out = []
    for u in parsed:
        if isinstance(u, dict):
            out.append((u.get("p_index"), u.get("text") or ""))
        else:
            out.append((getattr(u, "p_index", None), getattr(u, "text", "") or ""))
    return out


def compare(fmt: str, data: bytes, py_units: list, source_locale: str = "") -> dict:
    """Run Bilingua on `data` and compare with `py_units` ([(p_index, text)] the Python filter
    produced). Returns the record that is logged. Pure of side effects; never raises."""
    rec: dict = {"fmt": _safe_name(fmt), "bytes": len(data), "py_units": len(py_units)}
    started = time.monotonic()
    try:
        filt = _adapter_for(fmt)
    except BaseException as exc:
        rec.update(outcome="error", refusal=_safe_name(type(exc).__name__))
        filt = None
    else:
        if filt is None:
            rec.update(outcome="unavailable")
    if filt is None:
        rec["ms"] = int((time.monotonic() - started) * 1000)
        return rec
    try:
        from app.filters.base import source_code_page

        # the same project-language context the ingest parse runs in (decision 22)
        with source_code_page(source_locale or ""):
            parsed = filt.parse(data)
        bi = _units(parsed.units)
    except BaseException as exc:  # a refusal, a bug or a native panic: class or code only
        code = getattr(exc, "code", None) or type(exc).__name__
        rec.update(outcome="refused", refusal=_safe_name(code), bi_units=0)
        rec["ms"] = int((time.monotonic() - started) * 1000)
        return rec
    py_tokens = [t for _p, text in py_units for t in _TOKEN.findall(text)]
    bi_tokens = [t for _p, text in bi for t in _TOKEN.findall(text)]
    first_diff = -1
    for i, (a, b) in enumerate(zip(py_units, bi, strict=False)):
        if a != b:
            first_diff = i
            break
    if first_diff < 0 and len(py_units) != len(bi):
        first_diff = min(len(py_units), len(bi))
    rec.update(
        outcome="ok",
        bi_units=len(bi),
        units_equal=py_units == bi,
        first_diff=first_diff,
        py_tokens=len(py_tokens),
        bi_tokens=len(bi_tokens),
        tokens_equal=py_tokens == bi_tokens,
        py_sha=_sha(py_units),
        bi_sha=_sha(bi),
    )
    try:
        out = filt.build(data, {})
        rec["untouched_equal"] = bytes(out) == bytes(data)
    except BaseException as exc:  # a native panic (pyo3 PanicException) included
        rec["untouched_equal"] = None
        rec["build_refusal"] = _safe_name(getattr(exc, "code", None) or type(exc).__name__)
    rec["ms"] = int((time.monotonic() - started) * 1000)
    return rec


def _publish(rec: dict) -> None:
    with _state:
        _recent.append(dict(rec))
        del _recent[:-_RECENT_MAX]
    try:
        log.info("bilingua.shadow %s", json.dumps(rec, sort_keys=True))
    except Exception:  # logging must not break anything
        log.debug("bilingua shadow log failed", exc_info=True)


def run_shadow(fmt: str, data: bytes, py_units: list, source_locale: str = "") -> dict:
    """Synchronous shadow run: compare, count, log. Never raises."""
    try:
        rec = compare(fmt, data, py_units, source_locale)
        _bump("done")
        outcome = rec.get("outcome")
        if outcome == "refused":
            _bump("refused")
        elif outcome == "error":
            _bump("errors")
        _publish(rec)
        return rec
    except BaseException:  # never let a panic reach threading.excepthook (it prints text)
        _bump("errors")
        return {"outcome": "error"}


class _ShadowPool:
    """The shadow runner's executor: a bounded `submit(fn, *args)` that ai_optout.submit can
    drive (it passes `copy_context().run` as fn, so the submitter's AI scope and cost sink
    travel with the work, as with every other pool fan-out in app/; see #364). One daemon
    thread per queued task, at most MAX_PENDING queued, one parse at a time (`_run`). Returns
    True when the work was queued; never raises, never blocks."""

    def submit(self, fn, *args, **kwargs) -> bool:
        global _pending
        with _state:
            if _pending >= MAX_PENDING:
                _stats["skipped_busy"] = _stats.get("skipped_busy", 0) + 1
                return False
            _pending += 1
            _idle.clear()
            _stats["submitted"] = _stats.get("submitted", 0) + 1
        try:
            threading.Thread(
                target=self._run_one,
                args=(fn, args, kwargs),
                name="bilingua-shadow",
                daemon=True,
            ).start()
        except Exception:
            self._done()
            return False
        return True

    @staticmethod
    def _run_one(fn, args, kwargs) -> None:
        try:
            with _run:
                fn(*args, **kwargs)
        except BaseException:  # never let a panic reach threading.excepthook (it prints text)
            _bump("errors")
        finally:
            _ShadowPool._done()

    @staticmethod
    def _done() -> None:
        global _pending
        with _state:
            _pending = max(0, _pending - 1)
            if _pending == 0:
                _idle.set()


#: The executor ingest hands shadow work to, through ai_optout.submit.
POOL = _ShadowPool()


def prepare(fmt: str, data: bytes, skeleton: dict | None, source_locale: str = ""):
    """The shadow job for a new upload, or None when `fmt` is not in `bilingua_shadow_formats`
    (one settings read), the document is already a Bilingua document, or the file is over
    MAX_BYTES (counted). Copies the upload bytes and the Python units NOW, before ingest edits
    the skeleton. Never raises, never touches `skeleton` or `data`."""
    try:
        from app.filters.base import is_bilingua_skeleton
        from app.services import settings

        canon = (fmt or "").lower()
        if canon not in settings.bilingua_shadow_formats():
            return None
        if is_bilingua_skeleton(skeleton):
            return None  # the document is already a Bilingua document: nothing to compare
        if len(data) > MAX_BYTES:
            _bump("skipped_large")
            return None
        py_units = _units((skeleton or {}).get("units") or [])  # a copy, before ingest edits it
        return (canon, bytes(data), py_units, source_locale or "")
    except Exception:
        log.debug("bilingua shadow prepare failed", exc_info=True)
        return None


def run_job(job) -> dict:
    """Run one prepared job (on the POOL thread, in the submitter's copied context)."""
    return run_shadow(*job)


def submit(
    fmt: str, data: bytes, skeleton: dict | None, source_locale: str = "", project_id=None
) -> bool:
    """prepare() + ai_optout.submit(POOL, run_job, job) in the project's AI scope: the same
    two steps ingest takes. Returns True when work was queued. Never raises, never blocks."""
    job = prepare(fmt, data, skeleton, source_locale)
    if job is None:
        return False
    try:
        from app.services import ai_optout

        with ai_optout.scope(project_id=project_id):
            return bool(ai_optout.submit(POOL, run_job, job))
    except Exception:
        log.debug("bilingua shadow submit failed", exc_info=True)
        return False
