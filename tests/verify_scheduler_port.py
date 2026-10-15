"""Scheduler / TimeDriver port -- suite for composable-core phase 1, Train 2.

Proves the load-bearing contracts of the port that names the passage-of-time seam over the
two in-process daemons (see app/ports/scheduler.py + the SPEC-ports-phase1 spec
Train 2):

  A  SELECTION (byte-identity of the startup split): the default-driver choice is a pure
     function of config.IS_EMBEDDED -- non-embedded -> InProcessTimeDriver, embedded ->
     ExternalTickDriver -- and in THIS (non-embedded) process the SELF-REGISTERED default
     resolved through the frozen registry is an InProcessTimeDriver. STRUCTURAL CHECK: both
     drivers satisfy the runtime_checkable TimeDriver Protocol, and a start+tick-only class
     does NOT -- so sweep()'s presence is an ENFORCED check (H6), not prose.
  B  DEFAULT start() starts BOTH daemons -- exactly the inline scheduler.start();
     retention.start() the startup block ran before this train (patched to record, no real
     threads), so naming the port did not perturb the default install.
  C  EXTERNAL start() starts NEITHER daemon (the embedded no-op start; complements the
     phase-0 verify_embedded_profile real-boot assertion that scheduler/retention._started
     stay False).
  D  tick() maps to scheduler.tick_now (NOT bare scheduler.tick) and is 409-SAFE: while a
     concurrent tick holds _tick_lock, tick() returns (False, None) without running -- the
     vendor double-fire guard is preserved. A free-lock tick() runs one real pass.
  E  sweep() maps to retention.purge and REACHES retention for real (returns the counts
     dict; all-zeros because retention is disabled by default).
  F  sweep() is NAMED SEPARATELY, not folded into tick(): a real scheduler.tick_now pass
     never calls retention.purge (spec H6 -- an alternate driver that dropped sweep would
     silently never reap GDPR-relevant data; keeping them distinct is what makes that
     impossible to do by accident).
  G  SEAM: a recording fake driver registered via ports.register('scheduler', ...) is what
     ports.scheduler.get() resolves, and start/tick/sweep route to it; after unregister,
     get() falls back to the self-registered InProcessTimeDriver default.
  H  EMBEDDED SELECTION (subprocess): a real LOC_TMS_EMBEDDED=1 boot self-registers an
     ExternalTickDriver as the default (config.IS_EMBEDDED flows into the import-time
     selection), so an embedded install starts no daemons through the port.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR; sync_scheduler_enabled + the vendors
feature switched OFF so the real tick() passes never egress -- L3). ASCII-only prints. Final
line: SCHEDULER PORT OK.
"""

import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

# The embedded-selection child (H): decide the mode from argv BEFORE any app import, since
# config freezes IS_EMBEDDED at import (the verify_embedded_profile pattern).
_EMB_CHILD = "--embedded-child" in sys.argv
if _EMB_CHILD:
    init_env("loctms_sched_emb_")
    os.environ.pop("LOC_TMS_SERVER", None)
    os.environ["LOC_TMS_EMBEDDED"] = "1"
else:
    init_env("loctms_scheduler_")
    os.environ.pop("LOC_TMS_EMBEDDED", None)

import subprocess

from app import config, features, ports
from app.ports import scheduler as sport
from app.services import retention, scheduler, settings

if not _EMB_CHILD:
    from app.db import init_db

    init_db()
    # L3: pin the real-tick arms HERMETIC. With sync_scheduler_enabled OFF, scheduler.tick()
    # short-circuits (returns []) BEFORE any vendor NETWORK sweep, so a future default flip
    # fails loudly HERE instead of a verify suite hitting the network. Belt-and-suspenders:
    # switch the vendors feature off too (its network sweeps sit below the sync kill switch).
    settings.set_many({"sync_scheduler_enabled": "0"})
    _vf = features.feature_by_slug("vendors")
    if _vf is not None:
        settings.set_many({_vf.setting_key: "0"})


# --- helpers -----------------------------------------------------------------
class _RecordingDriver:
    """A fake TimeDriver that just remembers how it was driven (through the port)."""

    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def tick(self, now=None):
        self.calls.append(("tick", now))
        return ("faketick", now)

    def sweep(self, now=None):
        self.calls.append(("sweep", now))
        return ("fakesweep", now)


class _Recorder:
    """Records calls to a patched module function and returns a fixed sentinel."""

    def __init__(self, ret):
        self.calls = []
        self._ret = ret

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return self._ret


# ============================================================================
# A. selection -- pure function + the self-registered default in this process
# ============================================================================
def test_default_driver_selection():
    assert isinstance(sport._select_default_driver(False), sport.InProcessTimeDriver), (
        "non-embedded must select the in-process daemons"
    )
    assert isinstance(sport._select_default_driver(True), sport.ExternalTickDriver), (
        "embedded must select the external-cron driver (no daemons)"
    )
    # STRUCTURAL CHECK (M1): both concrete drivers satisfy the runtime_checkable TimeDriver
    # Protocol, and -- the load-bearing NEGATIVE control -- a driver missing sweep() does
    # NOT. That is what makes sweep()'s presence an ENFORCED check (H6: an alternate driver
    # cannot silently drop retention), not merely prose in the interface.
    assert isinstance(sport.InProcessTimeDriver(), sport.TimeDriver)
    assert isinstance(sport.ExternalTickDriver(), sport.TimeDriver)

    class _NoSweep:
        def start(self): ...
        def tick(self, now=None): ...

    assert not isinstance(_NoSweep(), sport.TimeDriver), (
        "a driver implementing only start()+tick() must FAIL the structural TimeDriver "
        "check -- sweep() must be a real named operation (H6), not optional prose"
    )
    # This process is a normal (non-embedded) test boot, so the default self-registered at
    # import must be the in-process driver -- byte-identity of the old startup split.
    assert config.IS_EMBEDDED is False, "precondition: this process is not embedded"
    drv = sport.get()
    assert isinstance(drv, sport.InProcessTimeDriver), (
        "the self-registered default for a non-embedded boot must be InProcessTimeDriver "
        f"(got {drv!r})"
    )
    # The generic registry agrees (get() is a thin wrapper over ports.get('scheduler')).
    assert sport.get() is ports.get("scheduler")
    print(
        "  (A) selection: _select_default_driver(False|True) -> InProcess|External; the "
        "non-embedded self-registered default is InProcessTimeDriver; both drivers "
        "satisfy the TimeDriver Protocol, a start+tick-only class does NOT (sweep is a "
        "structural check) OK"
    )


# ============================================================================
# B. default start() starts BOTH daemons (byte-identical to the old inline block)
# ============================================================================
def test_inprocess_start_starts_both_daemons():
    started = {"scheduler": 0, "retention": 0}
    orig_s, orig_r = scheduler.start, retention.start
    scheduler.start = lambda: started.__setitem__("scheduler", started["scheduler"] + 1)
    retention.start = lambda: started.__setitem__("retention", started["retention"] + 1)
    try:
        sport.InProcessTimeDriver().start()
        assert started == {"scheduler": 1, "retention": 1}, (
            f"InProcessTimeDriver.start() must start BOTH daemons exactly once (got {started!r})"
        )
    finally:
        scheduler.start, retention.start = orig_s, orig_r
    print(
        "  (B) default start: InProcessTimeDriver.start() calls scheduler.start() + "
        "retention.start() once each (byte-identical to the old startup block) OK"
    )


# ============================================================================
# C. external start() starts NEITHER daemon (the embedded no-op)
# ============================================================================
def test_external_start_starts_no_daemon():
    started = {"scheduler": 0, "retention": 0}
    orig_s, orig_r = scheduler.start, retention.start
    scheduler.start = lambda: started.__setitem__("scheduler", started["scheduler"] + 1)
    retention.start = lambda: started.__setitem__("retention", started["retention"] + 1)
    try:
        assert sport.ExternalTickDriver().start() is None, "external start() is a no-op"
        assert started == {"scheduler": 0, "retention": 0}, (
            f"ExternalTickDriver.start() must start NEITHER daemon (got {started!r})"
        )
    finally:
        scheduler.start, retention.start = orig_s, orig_r
    print(
        "  (C) external start: ExternalTickDriver.start() starts NO daemon (embedded "
        "drives time via the external cron pair) OK"
    )


# ============================================================================
# D. tick() -> scheduler.tick_now, 409-safe, and one real pass
# ============================================================================
def test_tick_maps_to_tick_now_and_is_409_safe():
    now = datetime(2026, 8, 15, 12, 0, 0)
    # (1) Mapping: BOTH drivers route tick() to scheduler.tick_now(now), NOT bare tick.
    rec = _Recorder(ret=(True, [7]))
    orig = scheduler.tick_now
    scheduler.tick_now = rec
    try:
        for drv in (sport.InProcessTimeDriver(), sport.ExternalTickDriver()):
            out = drv.tick(now)
            assert out == (True, [7]), ("tick() must return scheduler.tick_now()'s value", out)
        assert rec.calls == [((now,), {}), ((now,), {})], (
            f"tick() must call scheduler.tick_now(now) for both drivers: {rec.calls!r}"
        )
    finally:
        scheduler.tick_now = orig

    # (2) 409-safety: hold the real _tick_lock; a tick() must report (False, None) WITHOUT
    # running (the non-blocking manual-entry guard, preserved through the port).
    got = scheduler._tick_lock.acquire(blocking=False)
    assert got, "expected the tick lock to be free"
    try:
        ran, fired = sport.InProcessTimeDriver().tick()
        assert ran is False and fired is None, (
            "tick() while the lock is held must be 409-safe: (False, None), got ({!r}, {!r})".format(  # noqa: E501, UP032
                ran, fired
            )
        )
    finally:
        scheduler._tick_lock.release()

    # (3) A real pass with the lock free: runs and returns (True, []) on a fresh DB.
    ran, fired = sport.InProcessTimeDriver().tick()
    assert ran is True and fired == [], (
        f"a free-lock tick() must run one real pass: (True, []), got ({ran!r}, {fired!r})"
    )
    print(
        "  (D) tick: both drivers -> scheduler.tick_now(now) (not bare tick); 409-safe "
        "(False, None) while locked; (True, []) on a real free-lock pass OK"
    )


# ============================================================================
# E. sweep() -> retention.purge, and reaches retention for real
# ============================================================================
def test_sweep_maps_to_retention_purge():
    now = datetime(2026, 8, 15, 12, 0, 0)
    rec = _Recorder(ret={"documents": 0, "sentinel": 1})
    orig = retention.purge
    retention.purge = rec
    try:
        for drv in (sport.InProcessTimeDriver(), sport.ExternalTickDriver()):
            out = drv.sweep(now)
            assert out == {"documents": 0, "sentinel": 1}, (
                "sweep() must return retention.purge()'s value",
                out,
            )
        assert rec.calls == [((now,), {}), ((now,), {})], (
            f"sweep() must call retention.purge(now) for both drivers: {rec.calls!r}"
        )
    finally:
        retention.purge = orig

    # Real reach: retention is disabled by default, so purge returns an all-zeros counts
    # dict -- proving sweep() genuinely reaches retention.purge (not a stub).
    result = sport.InProcessTimeDriver().sweep(now)
    assert isinstance(result, dict) and result.get("documents") == 0 and "jobs" in result, (
        f"sweep() must reach retention.purge and return its counts dict (got {result!r})"
    )
    print(
        "  (E) sweep: both drivers -> retention.purge(now); a real sweep reaches "
        "retention and returns its counts dict (all-zeros, retention off) OK"
    )


# ============================================================================
# F. sweep() is NAMED separately -- a real tick pass never reaps retention
# ============================================================================
def test_sweep_not_folded_into_tick():
    # H6: retention must NOT be folded into tick(). Patch retention.purge to a tripwire and
    # run a REAL tick pass (lock free) -- retention.purge must never be reached, so an
    # embedded external cron that schedules ONLY /api/admin/tick provably reaps nothing.
    tripped = {"purge": 0}
    orig = retention.purge
    retention.purge = lambda *a, **kw: tripped.__setitem__("purge", tripped["purge"] + 1)
    try:
        ran, _fired = sport.InProcessTimeDriver().tick()
        assert ran is True, "the real tick pass must have run"
        assert tripped["purge"] == 0, (
            "tick() must NEVER call retention.purge -- retention is the SEPARATE sweep() "  # noqa: UP031
            "operation (spec H6); got %d call(s)" % tripped["purge"]
        )
    finally:
        retention.purge = orig
    print(
        "  (F) named-separately: a real tick() pass never calls retention.purge -- "
        "sweep() is a distinct operation, not folded into tick (spec H6) OK"
    )


# ============================================================================
# G. the seam -- a recording fake driver resolves through the port
# ============================================================================
def test_recording_fake_driver_seam():
    fake = _RecordingDriver()
    assert isinstance(fake, sport.TimeDriver), (
        "the recording fake driver must itself satisfy the TimeDriver Protocol"
    )
    ports.register("scheduler", fake)
    try:
        assert sport.get() is fake, "get() must resolve a registered override driver"
        sport.get().start()
        sport.get().tick(None)
        sport.get().sweep(None)
        assert fake.calls == ["start", ("tick", None), ("sweep", None)], (
            f"start/tick/sweep must route to the registered fake driver: {fake.calls!r}"
        )
    finally:
        ports.unregister("scheduler")
    # After unregister, get() falls back to the untouched self-registered default.
    assert isinstance(sport.get(), sport.InProcessTimeDriver), (
        "after unregister, get() must fall back to the InProcessTimeDriver default"
    )
    print(
        "  (G) seam: a recording fake driver registered via ports.register('scheduler') "
        "is what get() resolves and drives; unregister restores the default OK"
    )


# ============================================================================
# H. embedded selection (subprocess) -- config.IS_EMBEDDED flows into the default
# ============================================================================
def embedded_child_main():
    assert config.IS_EMBEDDED is True, "LOC_TMS_EMBEDDED=1 must set IS_EMBEDDED"
    drv = ports.get("scheduler")
    assert isinstance(drv, sport.ExternalTickDriver), (
        f"an embedded boot must self-register an ExternalTickDriver default (got {drv!r})"
    )
    assert not isinstance(drv, sport.InProcessTimeDriver), "embedded must not be in-process"
    # And its start() is a no-op (no daemons), so scheduler/retention never start.
    assert drv.start() is None
    assert scheduler._started is False and retention._started is False, (
        "the embedded driver must never have started the daemons"
    )
    print("EMB CHILD OK")


def test_embedded_selection_via_child():
    env = dict(os.environ)
    env.pop("LOC_TMS_EMBEDDED", None)  # the child sets its own mode
    env.pop("LOC_TMS_DATA_DIR", None)  # the child's init_env mints its own
    child = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--embedded-child"],
        env=env,
        capture_output=True,
        text=True,
    )
    if child.returncode != 0:
        sys.stdout.write(child.stdout)
        sys.stderr.write(child.stderr)
        raise AssertionError("the embedded-selection child boot FAILED (see output above)")
    assert "EMB CHILD OK" in child.stdout, child.stdout
    for line in child.stdout.splitlines():
        if line.strip():
            print("  [emb-child] " + line.strip())
    print(
        "  (H) embedded selection: a real LOC_TMS_EMBEDDED=1 boot self-registers "
        "ExternalTickDriver (no daemons) -- config.IS_EMBEDDED flows into the default OK"
    )


def main():
    if _EMB_CHILD:
        embedded_child_main()
        return
    test_default_driver_selection()
    test_inprocess_start_starts_both_daemons()
    test_external_start_starts_no_daemon()
    test_tick_maps_to_tick_now_and_is_409_safe()
    test_sweep_maps_to_retention_purge()
    test_sweep_not_folded_into_tick()
    test_recording_fake_driver_seam()
    test_embedded_selection_via_child()
    print("SCHEDULER PORT OK")


if __name__ == "__main__":
    main()
