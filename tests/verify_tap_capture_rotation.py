"""Prompt Tap CAPTURE-FILE RETENTION -- age-based TTL sweep of the run-capture files.

The per-action run-capture files (prompt_tap/actions/<action_id>.jsonl) are append-only
and NOT rotated, so without an age-out they grow linearly forever on a long-lived install
(soak 2026-08-14 finding S1: ~11 MB/h under load). prompt_tap.sweep_expired_captures
(piggybacked on the scheduler tick) deletes a capture file whose LAST WRITE is older than
prompt_tap_ttl_days. This suite proves:

  A  AGE-PRUNE + PRESERVE: aged files are deleted; a fresh file (still being appended) is
     kept. scanned/deleted arithmetic is honest.
  B  RETENTION SETTING respected: prompt_tap_ttl_days drives the cutoff; "0" DISABLES; the
     shipped default is "30" (ON by default -- 2026-08-14).
  C  LEGACY FILE age-out: a dormant global captures.jsonl is dropped whole; a fresh one is
     kept (no young record is ever dropped).
  D  DEGRADE-ON-FAILURE: an undeletable file is SKIPPED with a WARN, the sweep never raises,
     and capture() still works afterwards (the request path is unharmed).
  E  CONCURRENT-APPEND SAFE: appender threads hammer a shared action file while sweeps
     delete-and-recreate it; nothing raises, reads + a final capture still work, and on
     Windows NO unlink fails (a real _LOCK pin -- finding 8).
  F  ROTATION-AT-SIZE still works: the legacy rewrite-on-threshold rotation is untouched.
  G  SAFE ON EMPTY: a sweep over a missing store is a zero no-op and creates nothing.
  H  ENVELOPE PROTECTION (finding 4a): a NON-terminal (running/paused) envelope's file is
     never swept regardless of age; once the envelope is terminal the file ages out.
  I  ROLLUP HONESTY (finding 4b): a swept file can never masquerade as exact-ledger-zero --
     rollup_action_totals preserves the stored totals and marks capture_swept.
  J  TAP-DISABLED FREEZE (finding 7): with capturing off, an argless sweep is a no-op
     (evidence-preservation posture); re-enabling resumes retention.
  K  THROTTLE (finding 10): the sweep runs at most once per interval; `force` bypasses it.
  L  BAD/NEGATIVE/FUTURE inputs (findings 11 + robustness): garbage/negative TTL -> falls back
     to the default (30, ON), a negative also re-arming-WARNs; a future-mtime file is never swept.
  M  RE-ARMING WARN (finding 2): distinct failure signatures each warn; a repeat within the
     window is suppressed; a second DISTINCT signature is never masked by the first.
  N  BUDGET RECLAIM-THEN-RESUME: the disk-budget backstop withholds capture at the ceiling;
     the sweep reclaims aged files and capture RESUMES (cache invalidated).
  O  SCHEDULER WIRING (finding 8): scheduler.tick() actually invokes the sweep.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys; ASCII-only prints.
"""

import logging
import os
import shutil
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tapttl_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    TapAction,
    TapActionKind,
    TapRun,
    TapStatus,
)
from app.services import prompt_tap, scheduler, settings, tap_enrich, tap_envelope

_DAY = 86400


def _reset():
    """Clear both stores + DB envelope rows + reset the throttle/warn latches."""
    prompt_tap.clear()
    ad = prompt_tap._actions_dir()
    if ad.exists():
        shutil.rmtree(ad, ignore_errors=True)
    if prompt_tap._path().exists():
        os.remove(prompt_tap._path())
    with Session(engine) as s:
        for m in (TapRun, TapAction):
            for row in s.exec(select(m)).all():
                s.delete(row)
        s.commit()
    prompt_tap._SELFHEAL_WARNED = False
    prompt_tap._CAPTURE_WARNED = False
    prompt_tap._ttl_warn_at.clear()
    prompt_tap._last_sweep_at = 0.0
    with prompt_tap._LOCK:
        prompt_tap._budget_cache["bytes"] = None
        prompt_tap._budget_cache["at"] = 0.0
    settings.set_many(
        {
            "prompt_tap_enabled": "1",
            "prompt_tap_ttl_days": "7",
            "prompt_tap_actions_max_bytes": str(prompt_tap.ACTIONS_BUDGET_DEFAULT),
        }
    )


def _capture_action(aid, prompt="hello"):
    rec = prompt_tap.build_record(
        provider="mock",
        provider_kind="mock",
        model="m",
        task="translate",
        system="sys",
        prompt=prompt,
        max_tokens=64,
        payload={"text": prompt},
        outcome="ok",
        response_text="out",
        usage={"input_tokens": 1},
        tap_meta={"action_id": aid},
    )
    prompt_tap.capture(rec)


def _capture_legacy(prompt="hello"):
    rec = prompt_tap.build_record(
        provider="mock",
        provider_kind="mock",
        model="m",
        task="translate",
        system="sys",
        prompt=prompt,
        max_tokens=64,
        payload={"text": prompt},
        outcome="ok",
        response_text="out",
        usage={"input_tokens": 1},
    )
    prompt_tap.capture(rec)


def _age(path, days):
    old = time.time() - days * _DAY
    os.utime(str(path), (old, old))


def _mk_action(aid, status):
    with Session(engine) as s:
        s.add(TapAction(id=aid, kind=TapActionKind.WORKFLOW, status=status))
        s.commit()


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


# --- A: age-prune + preserve -------------------------------------------------
def test_A_age_prune_and_preserve():
    _reset()
    old1, old2, fresh = (tap_envelope.new_id() for _ in range(3))
    for a in (old1, old2, fresh):
        _capture_action(a)
    _age(prompt_tap._action_path(old1), 10)
    _age(prompt_tap._action_path(old2), 10)
    out = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
    assert out["scanned"] == 3, out
    assert out["deleted"] == 2, out
    assert out["freed_bytes"] > 0, out
    assert not prompt_tap._action_path(old1).exists()
    assert not prompt_tap._action_path(old2).exists()
    assert prompt_tap._action_path(fresh).exists(), "the fresh (live) file must be kept"
    assert {r["request"]["prompt"] for r in prompt_tap.read_records(limit=50)} == {"hello"}
    print("  (A) aged per-action files swept, the fresh/live one preserved OK")


# --- B: retention setting + opt-in default -----------------------------------
def test_B_retention_setting_and_optin_default():
    # The SHIPPED default is ON at 30 days (2026-08-14).
    assert settings.DEFAULTS["prompt_tap_ttl_days"] == "30", settings.DEFAULTS[
        "prompt_tap_ttl_days"
    ]
    assert prompt_tap.TTL_DEFAULT_DAYS == 30
    _reset()
    aid = tap_envelope.new_id()
    _capture_action(aid)
    _age(prompt_tap._action_path(aid), 10)

    settings.set_many({"prompt_tap_ttl_days": "0"})
    out = prompt_tap.sweep_expired_captures(force=True)
    assert out.get("skipped") == "retention_disabled" and out["deleted"] == 0, out
    assert prompt_tap._action_path(aid).exists(), "disabled sweep must not delete anything"

    settings.set_many({"prompt_tap_ttl_days": "30"})
    out = prompt_tap.sweep_expired_captures(force=True)
    assert out["ttl_days"] == 30 and out["deleted"] == 0, out
    assert prompt_tap._action_path(aid).exists(), "10d file must survive a 30d window"

    settings.set_many({"prompt_tap_ttl_days": "7"})
    out = prompt_tap.sweep_expired_captures(force=True)
    assert out["ttl_days"] == 7 and out["deleted"] == 1, out
    assert not prompt_tap._action_path(aid).exists(), "10d file must be swept at 7d"
    print("  (B) prompt_tap_ttl_days drives the cutoff; 0 disables; default is ON at 30 OK")


# --- C: legacy file age-out --------------------------------------------------
def test_C_legacy_age_out():
    _reset()
    _capture_legacy("legacy-old")
    _age(prompt_tap._path(), 10)
    out = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
    assert out["deleted"] == 1, out
    assert not prompt_tap._path().exists(), "a dormant legacy file should be dropped whole"

    _capture_legacy("legacy-fresh")
    out = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
    assert out["deleted"] == 0, out
    assert prompt_tap._path().exists(), "a fresh legacy file must be kept"
    assert {r["request"]["prompt"] for r in prompt_tap.read_records(limit=50)} == {"legacy-fresh"}
    print("  (C) dormant legacy file dropped whole; fresh legacy file kept OK")


# --- D: degrade-on-failure ---------------------------------------------------
def test_D_degrade_on_failure():
    _reset()
    aid = tap_envelope.new_id()
    _capture_action(aid)
    _age(prompt_tap._action_path(aid), 10)

    def _boom(path):
        raise OSError("simulated locked / wrong-owner file")

    real = prompt_tap._unlink_forcing
    prompt_tap._unlink_forcing = _boom
    try:
        out = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
    finally:
        prompt_tap._unlink_forcing = real
    assert out["deleted"] == 0 and out["scanned"] == 1, out
    assert "unlink_failed" in prompt_tap._ttl_warn_at, "an undeletable file must WARN"
    assert prompt_tap._action_path(aid).exists(), "the file is left in place to retry"

    # The REQUEST PATH is unharmed after the failed sweep.
    aid2 = tap_envelope.new_id()
    _capture_action(aid2, prompt="after-failure")
    assert prompt_tap._action_path(aid2).exists()
    assert "after-failure" in {r["request"]["prompt"] for r in prompt_tap.read_records(limit=50)}

    # Next cycle self-heals now that the file is deletable again.
    prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
    assert not prompt_tap._action_path(aid).exists(), "next cycle reclaims the old file"
    print("  (D) undeletable file -> WARN + skip, sweep never raises, request unharmed OK")


# --- E: concurrent-append safety (real _LOCK pin) ----------------------------
def test_E_concurrent_append_safe():
    _reset()
    aid = tap_envelope.new_id()
    errors = []
    stop = threading.Event()

    def _appender():
        try:
            i = 0
            while not stop.is_set():
                _capture_action(aid, prompt="c-%d" % i)  # noqa: UP031
                i += 1
        except Exception as exc:
            errors.append(("append", repr(exc)))

    def _sweeper():
        try:
            future = time.time() + 3650 * _DAY  # every file looks aged -> delete-and-recreate
            while not stop.is_set():
                prompt_tap.sweep_expired_captures(now_ts=future, ttl_days=1, force=True)
        except Exception as exc:
            errors.append(("sweep", repr(exc)))

    threads = [threading.Thread(target=_appender) for _ in range(3)]
    threads.append(threading.Thread(target=_sweeper))
    for t in threads:
        t.start()
    time.sleep(0.6)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not errors, errors
    # REAL LOCK PIN (finding 8): on Windows a delete racing an open writer handle FAILS; the
    # per-file _LOCK is what guarantees no writer handle is open at unlink time, so with the
    # lock there must be ZERO unlink failures. (Replace _LOCK with a no-op and this trips.)
    if os.name == "nt":
        assert "unlink_failed" not in prompt_tap._ttl_warn_at, (
            "a Windows unlink failure means the writer lock did not serialize the delete"
        )
    # Reads still parse; a final capture lands and is readable.
    for r in prompt_tap.read_records(limit=500):
        assert isinstance(r.get("request", {}).get("prompt"), str)
    _capture_action(aid, prompt="final")
    assert "final" in {r["request"]["prompt"] for r in prompt_tap.read_records(limit=500)}
    print("  (E) concurrent append + delete-and-recreate: no raise, reads+writes intact OK")


# --- F: rotation-at-size still works -----------------------------------------
def test_F_size_rotation_intact():
    _reset()
    n = prompt_tap.MAX_RECORDS + 50
    for i in range(n):
        _capture_legacy("seg-%d" % i)  # noqa: UP031
    with open(prompt_tap._path(), encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) <= prompt_tap.MAX_RECORDS, len(lines)
    newest = prompt_tap.read_records(limit=1)
    assert newest and newest[0]["request"]["prompt"] == "seg-%d" % (n - 1), newest  # noqa: UP031
    print("  (F) legacy size-rotation unaffected by the new age sweep OK")


# --- G: safe on empty / missing dir ------------------------------------------
def test_G_safe_on_empty():
    _reset()
    ad = prompt_tap._actions_dir()
    if ad.exists():
        shutil.rmtree(ad, ignore_errors=True)
    if prompt_tap._path().exists():
        os.remove(prompt_tap._path())
    out = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
    assert out["deleted"] == 0 and out["freed_bytes"] == 0 and out["scanned"] == 0, out
    assert not ad.exists(), "the sweep must not CREATE the actions dir"
    print("  (G) empty/missing store -> zero no-op, no dir creation OK")


# --- H: non-terminal envelope protection (finding 4a) ------------------------
def test_H_envelope_protection():
    for st in (TapStatus.RUNNING, TapStatus.PAUSED):
        _reset()
        aid = tap_envelope.new_id()
        _capture_action(aid)
        _age(prompt_tap._action_path(aid), 30)  # far past any window
        _mk_action(aid, st)
        out = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
        assert out["deleted"] == 0, (st, out)
        assert prompt_tap._action_path(aid).exists(), f"a {st} envelope's file must never be swept"
        # Once terminal, the same aged file ages out.
        with Session(engine) as s:
            a = s.get(TapAction, aid)
            a.status = TapStatus.COMPLETED
            s.add(a)
            s.commit()
        out = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
        assert out["deleted"] == 1, (st, out)
        assert not prompt_tap._action_path(aid).exists()
    print("  (H) running/paused envelope files protected; terminal ones age out OK")


# --- I: rollup honesty when the file was swept (finding 4b) ------------------
def test_I_rollup_capture_swept_marker():
    _reset()
    aid = tap_envelope.new_id()
    stored = {
        "schema_version": tap_enrich.ENRICH_SCHEMA_VERSION,
        "runs": 1,
        "by_run": [],
        "calls": 3,
        "spend_usd": 1.23,
        "spend_source": "ledger_per_call",
        "capture_withheld": False,
    }
    with Session(engine) as s:
        s.add(
            TapAction(
                id=aid, kind=TapActionKind.WORKFLOW, status=TapStatus.COMPLETED, totals=stored
            )
        )
        s.commit()
    # The per-action call file is GONE (swept). A re-roll must PRESERVE the honest totals.
    assert not prompt_tap._action_path(aid).exists()
    with Session(engine) as s:
        totals = tap_enrich.rollup_action_totals(s, aid)
    assert totals["capture_swept"] is True, totals
    assert totals["calls"] == 3 and totals["spend_usd"] == 1.23, totals
    assert totals["spend_source"] == "ledger_per_call", "must not masquerade as fresh zero"
    print("  (I) swept file -> totals preserved + capture_swept, never exact-ledger-zero OK")


# --- J: tap-disabled freezes retention (finding 7) ---------------------------
def test_J_disabled_tap_freezes_retention():
    _reset()
    aid = tap_envelope.new_id()
    _capture_action(aid)
    _age(prompt_tap._action_path(aid), 30)
    settings.set_many({"prompt_tap_enabled": "0"})
    out = prompt_tap.sweep_expired_captures(force=True)  # argless-equivalent: reads enabled()
    assert out.get("skipped") == "capture_disabled" and out["deleted"] == 0, out
    assert prompt_tap._action_path(aid).exists(), "disabled tap must freeze retention"
    settings.set_many({"prompt_tap_enabled": "1"})
    out = prompt_tap.sweep_expired_captures(force=True)
    assert out["deleted"] == 1, out
    print("  (J) capturing OFF freezes retention; re-enabling resumes it OK")


# --- K: throttle (finding 10) ------------------------------------------------
def test_K_throttle():
    _reset()
    a1, a2 = tap_envelope.new_id(), tap_envelope.new_id()
    _capture_action(a1)
    _capture_action(a2)
    _age(prompt_tap._action_path(a1), 30)
    _age(prompt_tap._action_path(a2), 30)
    # First unforced run sweeps a1; the immediate second unforced run is THROTTLED.
    out1 = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7)
    assert out1["deleted"] == 2 and "skipped" not in out1, out1
    _capture_action(a1)  # re-create an aged file to prove the next call did nothing
    _age(prompt_tap._action_path(a1), 30)
    out2 = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7)
    assert out2.get("skipped") == "throttled" and out2["deleted"] == 0, out2
    assert prompt_tap._action_path(a1).exists(), "throttled sweep must not run"
    # force bypasses the throttle.
    out3 = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
    assert out3["deleted"] == 1 and "skipped" not in out3, out3
    print("  (K) sweep self-throttles to at most one run per interval; force bypasses OK")


# --- L: bad/negative TTL + future mtime --------------------------------------
def test_L_bad_and_future_inputs():
    _reset()
    settings.set_many({"prompt_tap_ttl_days": "abc"})
    assert prompt_tap._ttl_days() == 30, "garbage TTL -> falls back to the default (30)"
    settings.set_many({"prompt_tap_ttl_days": "-5"})
    prompt_tap._ttl_warn_at.clear()
    assert prompt_tap._ttl_days() == 30, "negative TTL -> falls back to the default (30)"
    assert "bad_ttl" in prompt_tap._ttl_warn_at, "a negative TTL must WARN (finding 11)"

    # A FUTURE-mtime file is never swept (mtime >= cutoff).
    _reset()
    aid = tap_envelope.new_id()
    _capture_action(aid)
    _age(prompt_tap._action_path(aid), -5)  # mtime 5 days in the FUTURE
    out = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
    assert out["deleted"] == 0, out
    assert prompt_tap._action_path(aid).exists()
    print("  (L) garbage/negative TTL -> default(30)+WARN; future-mtime file never swept OK")


# --- M: re-arming per-signature WARN (finding 2) -----------------------------
def test_M_rearming_warn():
    _reset()
    log_ = logging.getLogger("loc_tms.prompt_tap")
    cap = _Capture()
    cap.setLevel(logging.WARNING)
    log_.addHandler(cap)
    try:
        prompt_tap._warn_ttl_sweep("sigA", "first A")
        prompt_tap._warn_ttl_sweep("sigA", "second A within window")  # suppressed
        prompt_tap._warn_ttl_sweep("sigB", "distinct B")  # NOT masked by A
        warns = [r for r in cap.records if r.levelno == logging.WARNING]
        assert len(warns) == 2, [r.getMessage() for r in warns]
        # Re-arm: pretend an hour passed for sigA -> it warns again.
        prompt_tap._ttl_warn_at["sigA"] = time.monotonic() - prompt_tap._WARN_REARM_S - 1
        prompt_tap._warn_ttl_sweep("sigA", "A after the window")
        warns = [r for r in cap.records if r.levelno == logging.WARNING]
        assert len(warns) == 3, [r.getMessage() for r in warns]
    finally:
        log_.removeHandler(cap)
    print("  (M) per-signature WARN: distinct signatures warn, repeats suppressed, re-arms OK")


# --- N: budget reclaim-then-resume -------------------------------------------
def test_N_budget_reclaim_then_resume():
    _reset()
    # Tiny budget: two small records already exceed it.
    settings.set_many({"prompt_tap_actions_max_bytes": "200"})
    a1 = tap_envelope.new_id()
    _capture_action(a1, prompt="x" * 300)  # first write lands, pushes over budget
    a2 = tap_envelope.new_id()
    _capture_action(a2, prompt="y" * 300)  # WITHHELD: dir already over budget
    assert not prompt_tap._action_path(a2).exists(), "capture must be withheld over budget"
    # Age the first file and sweep it -> space reclaimed + budget cache invalidated.
    _age(prompt_tap._action_path(a1), 30)
    out = prompt_tap.sweep_expired_captures(now_ts=time.time(), ttl_days=7, force=True)
    assert out["deleted"] == 1, out
    # Capture RESUMES now that we are back under budget.
    a3 = tap_envelope.new_id()
    _capture_action(a3, prompt="z")
    assert prompt_tap._action_path(a3).exists(), "capture must resume after reclaim"
    print("  (N) disk-budget withhold -> sweep reclaims -> capture resumes OK")


# --- O: scheduler wiring (finding 8) -----------------------------------------
def test_O_scheduler_tick_runs_sweep():
    _reset()
    aid = tap_envelope.new_id()
    _capture_action(aid)
    _age(prompt_tap._action_path(aid), 30)
    settings.set_many({"prompt_tap_ttl_days": "7"})
    prompt_tap._last_sweep_at = 0.0  # ensure the throttle allows this tick's sweep
    scheduler.tick()
    assert not prompt_tap._action_path(aid).exists(), (
        "scheduler.tick() must actually invoke the TTL sweep"
    )
    print("  (O) scheduler.tick() invokes the capture-file TTL sweep OK")


def main():
    init_db()
    test_A_age_prune_and_preserve()
    test_B_retention_setting_and_optin_default()
    test_C_legacy_age_out()
    test_D_degrade_on_failure()
    test_E_concurrent_append_safe()
    test_F_size_rotation_intact()
    test_G_safe_on_empty()
    test_H_envelope_protection()
    test_I_rollup_capture_swept_marker()
    test_J_disabled_tap_freezes_retention()
    test_K_throttle()
    test_L_bad_and_future_inputs()
    test_M_rearming_warn()
    test_N_budget_reclaim_then_resume()
    test_O_scheduler_tick_runs_sweep()
    print("verify_tap_capture_rotation OK")


if __name__ == "__main__":
    main()
