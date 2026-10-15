"""Scheduled audit-chain verification + monitoring (gap G-10 / AU-05), offline.

Covers TRAIN 4 of the Batch-10 compliance remediation + the adversarial-gate fixes:
- the scheduled sweep runs verify_chain() when DUE and writes a non-forced
  audit.chain_verified heartbeat row + persists the monitoring status
- self-throttle: a second call before the cadence elapses is a no-op
- audit_chain_verify_hours=0 disables the sweep; audit_enabled=0 does NOT (item-3)
- a BROKEN chain writes a FORCED audit.chain_broken row + the re-alert LATCH
  suppresses a duplicate within the window, re-alerts on daily staleness and on a
  state change (broken->ok->broken), and records a recovery heartbeat
- the shared service function feeds the CLI `audit verify` result shape + exit code
- the Prometheus audit-chain gauges are present when metrics is armed, absent when off,
  and ALL-OR-NOTHING on a malformed status row (F6)
Gate fixes pinned (round 1 + round 2):
- F1: a forged (un-MAC'd) status row -> snapshot UNKNOWN, sweep detects the tamper,
  writes audit.chain_status_tampered, and re-verifies; a FUTURE last_checked -> due
- R1: a truncation stays RED (break_class=anchor) across MANY daily sweeps even as forced
  rows refill the ids up to the anchor (no naive day-N false recovered=1); reset-anchor is
  the single recovery path (re-baseline + clears the sticky status); a mid-chain tamper
  (break_class=tamper) still recovers to ok when healed
- R6: the sticky state is re-derived FROM THE LOG (chain_broken class=anchor vs anchor_reset),
  so DELETING the status Setting row cannot clear it — the next sweep still reports broken
- #278: one keyed row + no anchor is broken, as is a fully wiped initialized chain
- R3: a repeatedly re-forged status within the latch window -> one tampered row + one sweep
- R5: reset-anchor on an empty log claims nothing (no row written)
- F5: a garbage cadence value -> default 24 (NOT off) + a monitoring-disabled event on ->0
- F8: many keyed rows but the anchor row deleted -> verify_chain reports invalid
- item-3: the sweep runs with audit_enabled=0 and still writes a forced broken row on tamper
"""

import json
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_audit_chain_sched_")

from sqlalchemy import text
from sqlmodel import Session

from app.db import engine, init_db
from app.services import audit, settings


def _count(action: str) -> int:
    return audit.count(action=action)


def test_contention_surfaces():
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch

    from app.cli.__main__ import main as cli_main
    from app.db import WriteContentionError

    before = audit.chain_status_snapshot()
    broken = _count("audit.chain_broken")
    with patch.object(
        audit, "verify_chain", side_effect=WriteContentionError("audit.verify_chain")
    ):
        assert audit.verify_chain_scheduled_sweep() is False
        output = io.StringIO()
        with redirect_stdout(output):
            assert cli_main(["--json", "audit", "verify"]) == 1
        result = json.loads(output.getvalue())
        assert result["retry_after"] > 0 and result["ok"] is False, result
        assert "BROKEN" not in output.getvalue()
    assert audit.chain_status_snapshot() == before
    assert _count("audit.chain_broken") == broken
    print("  [#433] scheduler defers and CLI reports retryable contention, never BROKEN")


def test_http_contention():
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.db import WriteContentionError
    from app.main import app
    from app.models import Role
    from app.services import apikeys, security

    user = security.create_user("audit433@example.test", "audit433", Role.ADMIN, "audit-test-433")
    _, raw = apikeys.create_key(user.id, "audit433", scopes="api docs admin")
    settings.set_many({"security_auth_required": "1"})
    try:
        with (
            TestClient(app) as client,
            patch.object(
                audit, "verify_chain", side_effect=WriteContentionError("audit.verify_chain")
            ),
            patch.object(security, "session_user", return_value=user),
        ):
            for path in ("/api/audit/verify", "/audit?verify=1"):
                response = client.get(path, headers={"X-Loc-Token": raw})
                assert response.status_code == 503, (path, response.status_code, response.text)
                assert int(response.headers["Retry-After"]) > 0
                if path.startswith("/api"):
                    assert "valid" not in response.json(), response.json()
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  [#433] web and API return retryable 503 without a false chain result OK")


def main() -> None:
    init_db()
    settings.set_many({"audit_enabled": "1", "audit_chain_verify_hours": "24"})

    # seed a few normal rows so verify_chain has a real chain to walk
    audit.log(actor="alice@example.com", action="project.create", target="p1")
    audit.log(actor="bob@example.com", action="settings.update", target="s1")
    assert audit.chain_status_snapshot() is None, "no scheduled verify has run yet"
    test_contention_surfaces()

    t0 = datetime(2026, 8, 25, 3, 0, 0)

    # --- (1) DUE: first sweep runs, writes a chain_verified heartbeat + status --------
    ran = audit.verify_chain_scheduled_sweep(now=t0)
    assert ran is True, "first scheduled sweep must run"
    assert _count("audit.chain_verified") == 1, "one heartbeat row written"
    assert _count("audit.chain_broken") == 0, "no broken row on a clean chain"
    snap = audit.chain_status_snapshot()
    assert snap and snap["ok"] is True and snap["state"] == "ok", snap
    assert snap["rows_checked"] >= 2 and snap["first_bad_id"] is None, snap
    assert snap["last_checked"] == t0.isoformat(timespec="seconds"), snap
    assert snap["last_alert"] is None, snap
    # heartbeat is NON-forced (written through the normal gated path)
    print("  scheduled sweep ran: chain_verified heartbeat + ok status persisted")

    # --- (2) SELF-THROTTLE: not due yet -> no-op --------------------------------------
    ran = audit.verify_chain_scheduled_sweep(now=t0 + timedelta(minutes=5))
    assert ran is False, "sweep must not run again before the cadence elapses"
    assert _count("audit.chain_verified") == 1, "no extra heartbeat within the window"
    print("  self-throttle: second sweep 5m later is a no-op")

    # --- (3) DUE again after 24h ------------------------------------------------------
    ran = audit.verify_chain_scheduled_sweep(now=t0 + timedelta(hours=25))
    assert ran is True, "sweep due again after the cadence"
    assert _count("audit.chain_verified") == 2, "second heartbeat written"
    print("  cadence elapsed: sweep runs again")

    # --- (4) cadence 0 disables; audit_enabled=0 does NOT disable the sweep (item-3) ---
    settings.set_many({"audit_chain_verify_hours": "0"})
    assert audit.verify_chain_scheduled_sweep(now=t0 + timedelta(hours=60)) is False
    assert _count("audit.chain_verified") == 2, "cadence=0 sweep writes nothing"
    # item-3: with the cadence back on but the GLOBAL audit gate OFF, the sweep STILL RUNS
    # (verify is a pure read; chain_broken is force=True). On a clean chain the heartbeat is
    # gated/non-forced so no chain_verified row persists, but the status DOES update.
    settings.set_many({"audit_chain_verify_hours": "24", "audit_enabled": "0"})
    ran = audit.verify_chain_scheduled_sweep(now=t0 + timedelta(hours=90))
    assert ran is True, "sweep must run even when audit_enabled=0 (item-3)"
    assert _count("audit.chain_verified") == 2, "heartbeat stays gated when auditing is off"
    assert audit.chain_status_snapshot()["last_checked"] == (t0 + timedelta(hours=90)).isoformat(
        timespec="seconds"
    ), "status still updates with the gate off"
    settings.set_many({"audit_enabled": "1"})
    print("  cadence=0 disables; audit_enabled=0 does NOT (sweep still monitors)")

    # --- (5) BROKEN chain -> forced chain_broken row + latch --------------------------
    # tamper the FIRST seeded row's detail (mid-chain), breaking the hash of that row.
    victim = sorted(audit.recent(limit=500, action="project.create"), key=lambda r: r.id)[0]
    with Session(engine) as s:
        s.exec(
            text("UPDATE auditlog SET detail = 'tampered' WHERE id = :i").bindparams(i=victim.id)
        )
        s.commit()

    t1 = t0 + timedelta(hours=100)
    ok, checked, first_bad = audit.run_chain_verification(record=True, actor="scheduler", now=t1)
    assert ok is False and first_bad == victim.id, (ok, checked, first_bad)
    assert _count("audit.chain_broken") == 1, "one forced broken row on first detection"
    snap = audit.chain_status_snapshot()
    assert snap["state"] == "broken" and snap["ok"] is False, snap
    assert snap["first_bad_id"] == victim.id, snap
    assert snap["last_alert"] == t1.isoformat(timespec="seconds"), snap
    # the chain_broken row is written force=True: prove a forced row persists even with the
    # global gate OFF while a NON-forced row is dropped (a neutral action keeps the
    # chain_broken counter clean).
    settings.set_many({"audit_enabled": "0"})
    forced_before = _count("test.force_probe")
    gated_before = _count("test.gated_probe")
    audit.log(action="test.gated_probe", detail="dropped-when-off")  # non-forced -> dropped
    audit.log(action="test.force_probe", detail="written-when-off", force=True)
    assert _count("test.gated_probe") == gated_before, "non-forced row dropped with gate off"
    assert _count("test.force_probe") == forced_before + 1, "forced row writes with gate off"
    settings.set_many({"audit_enabled": "1"})
    print("  broken chain detected: forced chain_broken row + broken status")

    # latch: same break, inside the re-alert window -> suppressed (no new row)
    ok, _, _ = audit.run_chain_verification(record=True, now=t1 + timedelta(hours=1))
    assert ok is False
    assert _count("audit.chain_broken") == 1, "latch suppresses a duplicate broken row"
    print("  re-alert latch: duplicate broken row suppressed within the window")

    # daily staleness: >24h later -> re-alert
    ok, _, _ = audit.run_chain_verification(record=True, now=t1 + timedelta(hours=25))
    assert ok is False
    assert _count("audit.chain_broken") == 2, "re-alert after the daily staleness window"
    print("  re-alert latch: fires again after 24h while still broken")

    # --- (6) recovery + state-change re-alert -----------------------------------------
    with Session(engine) as s:
        s.exec(
            text("UPDATE auditlog SET detail = :d WHERE id = :i").bindparams(
                d=victim.detail, i=victim.id
            )
        )
        s.commit()
    hb_before = _count("audit.chain_verified")
    ok, _, _ = audit.run_chain_verification(record=True, now=t1 + timedelta(hours=30))
    assert ok is True, "chain verifies again after repair"
    assert _count("audit.chain_verified") == hb_before + 1, "recovery heartbeat written"
    assert audit.chain_status_snapshot()["state"] == "ok"
    # re-tamper -> state change ok->broken re-alerts immediately (no staleness wait)
    with Session(engine) as s:
        s.exec(text("UPDATE auditlog SET detail = 'again' WHERE id = :i").bindparams(i=victim.id))
        s.commit()
    ok, _, _ = audit.run_chain_verification(record=True, now=t1 + timedelta(hours=30, minutes=1))
    assert ok is False
    assert _count("audit.chain_broken") == 3, "state change ok->broken re-alerts at once"
    print("  recovery heartbeat + ok->broken state change re-alerts immediately")
    _contention_never_hides_real_tamper(t1 + timedelta(hours=30, minutes=1), victim.id)

    # repair for the remaining checks
    with Session(engine) as s:
        s.exec(
            text("UPDATE auditlog SET detail = :d WHERE id = :i").bindparams(
                d=victim.detail, i=victim.id
            )
        )
        s.commit()
    assert audit.verify_chain()[0] is True

    # --- (7) CLI shares the ONE service function: shape + exit code --------------------
    _cli_shape_and_exit()

    # --- (8) metrics gauges: armed/off + all-or-nothing (F6) --------------------------
    _metrics_gauges()

    # --- (9) cadence parsing (F5): garbage -> default; ->0 emits a disable event -------
    _cadence_cases()

    # --- (10) status-row integrity (F1) + audit_enabled=0 forced broken (item-3) ------
    _status_integrity_cases()

    # --- (11) anchor: truncation (F2) + missing anchor (F8) + reset-anchor verb -------
    _anchor_cases()

    print("AUDIT CHAIN SCHEDULE OK")


def _contention_never_hides_real_tamper(now, victim_id) -> None:
    """#433 gate: with a REAL mid-chain tamper on disk and the verify lock busy, the sweep
    defers without writing any status or row (never green, never a false break), and the
    first sweep after the lock frees reports the tamper with a forced chain_broken row."""
    from unittest.mock import patch

    from app.db import WriteContentionError
    from app.models import Setting

    with Session(engine) as s:  # no status row -> the sweep is due at any `now`
        row = s.get(Setting, "audit_chain_status")
        if row is not None:
            s.delete(row)
            s.commit()
    broken_before = _count("audit.chain_broken")
    verified_before = _count("audit.chain_verified")
    with patch.object(
        audit, "verify_chain", side_effect=WriteContentionError("audit.verify_chain")
    ):
        for _ in range(3):
            assert audit.verify_chain_scheduled_sweep(now=now) is False, "contention defers"
    assert audit.chain_status_snapshot() is None, "contention recorded no status"
    assert _count("audit.chain_broken") == broken_before, "contention wrote no break row"
    assert _count("audit.chain_verified") == verified_before, "contention wrote no heartbeat"
    assert audit.verify_chain_scheduled_sweep(now=now) is True, "retry runs once the lock frees"
    assert _count("audit.chain_broken") == broken_before + 1, "real tamper reported after retry"
    snap = audit.chain_status_snapshot()
    assert snap["state"] == "broken" and snap["first_bad_id"] == victim_id, snap
    print("  #433 gate: contention defers; the real tamper is reported once the lock frees")


def _cli_shape_and_exit() -> None:
    """`audit verify` (cmd_db.verify_audit_chain) routes through run_chain_verification
    (record=False): a pure read, same result shape, exit 0 clean / raise CliError(1) broken."""
    from app.cli import cmd_db
    from app.cli._common import CliError

    captured = {}

    class _Emit:
        as_json = False

        def result(self, text, obj):  # matches the emit.result(text, dict) contract
            captured["text"] = text
            captured["obj"] = obj

        def detail(self, *_a, **_k):
            pass

    emit = _Emit()
    # clean chain -> exit 0, no rows written by the manual/CLI path
    verified_before = audit.count(action="audit.chain_verified")
    rc = cmd_db.verify_audit_chain(emit)
    assert rc == 0, rc
    assert captured["obj"]["ok"] is True and captured["obj"]["first_bad_id"] is None
    assert audit.count(action="audit.chain_verified") == verified_before, (
        "the CLI/manual verify (record=False) must NOT write an audit row"
    )

    # broken chain -> CliError(exit 1), and still no audit row from the read-only path
    victim = sorted(audit.recent(limit=500, action="project.create"), key=lambda r: r.id)[0]
    with Session(engine) as s:
        s.exec(text("UPDATE auditlog SET detail = 'x' WHERE id = :i").bindparams(i=victim.id))
        s.commit()
    broken_before = audit.count(action="audit.chain_broken")
    try:
        cmd_db.verify_audit_chain(emit)
        raise AssertionError("verify_audit_chain must raise on a broken chain")
    except CliError as exc:
        assert exc.code == 1, exc.code
    assert captured["obj"]["ok"] is False and captured["obj"]["first_bad_id"] == victim.id
    assert audit.count(action="audit.chain_broken") == broken_before, (
        "the CLI/manual verify must NOT write a broken row either"
    )
    # repair
    with Session(engine) as s:
        s.exec(
            text("UPDATE auditlog SET detail = :d WHERE id = :i").bindparams(
                d=victim.detail, i=victim.id
            )
        )
        s.commit()
    assert audit.verify_chain()[0] is True
    print("  CLI audit verify: shared service fn, shape + exit 0/1, writes no rows")


def _metrics_gauges() -> None:
    # The Prometheus gauges belong to the observability add-on; the open core keeps the DATA
    # they read (the persisted chain status) and must show no registry.
    from app import editions

    assert not editions.addon_installed("observability"), "observability is a paid add-on"

    # refresh the persisted status to a known-ok result (chain was repaired above)
    ok, _, _ = audit.run_chain_verification(record=True, now=datetime(2026, 8, 26, 3, 0, 0))
    assert ok is True

    snap = audit.chain_status_snapshot()
    assert snap is not None and "last_checked" in snap and "rows_checked" in snap, snap

    # all-or-nothing source: a valid-MAC row MISSING last_checked still reads (the gauge side
    # decides to emit nothing), then re-heal so downstream sections start clean
    audit._write_chain_status({"state": "ok", "ok": True, "rows_checked": 5})
    assert audit.chain_status_snapshot() is not None, "valid-MAC partial row still reads"
    audit.run_chain_verification(record=True, now=datetime(2026, 8, 26, 6, 0, 0))
    print("  metrics: chain status data path intact; no gauges without the add-on")


def _cadence_cases() -> None:
    """F5: an unparseable cadence falls back to the DEFAULT (24), NOT off, with a
    once-per-process warning; only a literal non-positive integer disables; and a
    transition ON->0 emits a distinct audit.chain_monitoring_disabled event."""
    audit._VERIFY_HOURS_WARNED = False  # reset the once-per-process latch for the assertion
    settings.set_many({"audit_chain_verify_hours": "24h"})
    assert audit.verify_hours() == 24, "garbage '24h' must fall back to default 24"
    assert audit._VERIFY_HOURS_WARNED is True, "garbage cadence warns once"
    settings.set_many({"audit_chain_verify_hours": "1.5"})
    assert audit.verify_hours() == 24, "non-int '1.5' -> default 24 (not off)"
    settings.set_many({"audit_chain_verify_hours": "-3"})
    assert audit.verify_hours() == 0, "a literal non-positive int (-3) disables"

    settings.set_many({"audit_chain_verify_hours": "24"})  # back ON (no event: off->on)
    before = _count("audit.chain_monitoring_disabled")
    settings.set_many({"audit_chain_verify_hours": "0"})  # ON->0 : distinct disable event
    assert _count("audit.chain_monitoring_disabled") == before + 1, "->0 emits a disable event"
    settings.set_many({"audit_chain_verify_hours": "0"})  # 0->0 no transition, no re-emit
    assert _count("audit.chain_monitoring_disabled") == before + 1, "no re-emit without transition"
    settings.set_many({"audit_chain_verify_hours": "24"})  # restore ON for later sections
    print("  F5: garbage cadence -> 24 (+warn); literal <=0 disables; ->0 emits an event")


def _status_integrity_cases() -> None:
    """F1: a forged/un-MAC'd status row reads as UNKNOWN and the sweep detects + records the
    tamper then re-verifies; a FUTURE last_checked is treated as due. item-3: the sweep runs
    even when audit_enabled=0 and writes a FORCED chain_broken row on a real tamper."""
    from app.models import Setting

    settings.set_many({"audit_enabled": "1", "audit_chain_verify_hours": "24"})
    assert audit.verify_chain()[0] is True, "chain clean at the start of the integrity cases"
    tb = datetime(2026, 8, 27, 0, 0, 0)

    # F1: a bare, un-MAC'd forged status row must NOT be trusted.
    forged = json.dumps(
        {"ok": True, "state": "ok", "rows_checked": 999, "last_checked": "2099-01-01T00:00:00"}
    )
    with Session(engine) as s:
        row = s.get(Setting, "audit_chain_status")
        row.value = forged
        s.add(row)
        s.commit()
    assert audit.chain_status_snapshot() is None, "forged status reads as UNKNOWN, never ok"
    tamper_before = _count("audit.chain_status_tampered")
    assert audit.verify_chain_scheduled_sweep(now=tb) is True, "sweep runs on a tampered status"
    assert _count("audit.chain_status_tampered") == tamper_before + 1, "tampered event written"
    healed = audit.chain_status_snapshot()
    assert healed and healed["ok"] is True, "status re-healed with a valid MAC after re-verify"
    print("  F1: forged status -> UNKNOWN + chain_status_tampered + self-heal")

    # R3: a REPEATEDLY re-forged status row within the latch window must NOT re-run the O(n)
    # verify + write a tampered row every tick — process-local latch caps it to one.
    audit._CHAIN_TAMPER_HANDLED_AT = None  # reset the process latch for a clean measurement
    tr = datetime(2026, 8, 27, 6, 0, 0)
    _forge_status(forged)
    t_before = _count("audit.chain_status_tampered")
    v_before = _count("audit.chain_verified")
    assert audit.verify_chain_scheduled_sweep(now=tr) is True, "first forge handled"
    _forge_status(forged)  # attacker re-forges, destroying the healed row
    assert audit.verify_chain_scheduled_sweep(now=tr + timedelta(hours=1)) is False, (
        "a re-forge within the latch window is suppressed (no re-verify)"
    )
    _forge_status(forged)
    assert audit.verify_chain_scheduled_sweep(now=tr + timedelta(hours=5)) is False
    assert _count("audit.chain_status_tampered") == t_before + 1, "exactly ONE tampered row"
    assert _count("audit.chain_verified") == v_before + 1, "exactly ONE re-verify (one heartbeat)"
    # past the window, it handles again
    assert audit.verify_chain_scheduled_sweep(now=tr + timedelta(hours=30)) is True, (
        "past the 24h latch window, a still-forged status is handled again"
    )
    audit._CHAIN_TAMPER_HANDLED_AT = None  # leave the latch clean for later sections

    # #433: contention on the tamper re-verify defers WITHOUT a tampered row per tick, then the
    # retry (no new row) self-heals once the lock clears.
    from unittest.mock import patch

    from app.db import WriteContentionError

    audit._CHAIN_TAMPER_HANDLED_AT = None
    audit._CHAIN_TAMPER_REVERIFY_PENDING = False
    tc = datetime(2026, 8, 28, 6, 0, 0)
    _forge_status(forged)
    t_before = _count("audit.chain_status_tampered")
    with patch.object(
        audit, "verify_chain", side_effect=WriteContentionError("audit.verify_chain")
    ):
        for k in range(3):
            assert audit.verify_chain_scheduled_sweep(now=tc + timedelta(seconds=30 * k)) is False
    assert _count("audit.chain_status_tampered") == t_before + 1, "one tampered row, not per tick"
    assert audit.verify_chain_scheduled_sweep(now=tc + timedelta(minutes=2)) is True, "retry runs"
    assert audit.chain_status_snapshot() is not None, "status healed after the retry"
    assert _count("audit.chain_status_tampered") == t_before + 1
    assert audit.verify_chain_scheduled_sweep(now=tc + timedelta(minutes=3)) is False
    audit._CHAIN_TAMPER_HANDLED_AT = None
    print("  #433: contention on the tamper re-verify -> one tampered row, retry heals")
    print("  R3: repeated forged status within the latch window -> one tampered row + one sweep")

    # F1: a FUTURE last_checked (forged/clock-jump) is treated as due, never trusted to defer.
    audit._write_chain_status(
        {
            "state": "ok",
            "ok": True,
            "first_bad_id": None,
            "rows_checked": 3,
            "last_checked": "2099-06-01T00:00:00",
            "last_alert": None,
        }
    )
    assert audit.verify_chain_scheduled_sweep(now=tb + timedelta(hours=1)) is True, (
        "a future last_checked must be treated as due"
    )
    print("  F1: future last_checked -> sweep runs (clock clamp)")

    # item-3: audit_enabled=0, tamper -> sweep runs and writes a FORCED chain_broken row.
    victim = sorted(audit.recent(limit=500, action="project.create"), key=lambda r: r.id)[0]
    settings.set_many({"audit_enabled": "0"})
    with Session(engine) as s:
        s.exec(text("UPDATE auditlog SET detail='off-tamper' WHERE id=:i").bindparams(i=victim.id))
        s.commit()
    broken_before = _count("audit.chain_broken")
    assert audit.verify_chain_scheduled_sweep(now=tb + timedelta(hours=48)) is True
    assert _count("audit.chain_broken") == broken_before + 1, "forced broken row with audit off"
    settings.set_many({"audit_enabled": "1"})
    with Session(engine) as s:
        s.exec(
            text("UPDATE auditlog SET detail=:d WHERE id=:i").bindparams(
                d=victim.detail, i=victim.id
            )
        )
        s.commit()
    assert audit.verify_chain()[0] is True
    print("  item-3: sweep with audit_enabled=0 still writes a forced chain_broken on tamper")


def _anchor_cases() -> None:
    """R1: a tail truncation stays BROKEN across MANY daily sweeps even as forced chain_broken
    rows refill the truncated ids up to the anchor (the day-N false-recovery the monotonic
    anchor only delayed) — NO recovered=1, break_class=anchor, cleared ONLY by reset-anchor. A
    MID-CHAIN tamper still recovers to ok when healed. One keyed row with no anchor is
    broken (#278). F8: many keyed rows + no anchor -> invalid. R5: reset-anchor on an
    empty log claims nothing."""
    from app.models import Setting

    settings.set_many({"audit_enabled": "1", "audit_chain_verify_hours": "24"})
    audit.run_chain_verification(record=True, now=datetime(2026, 8, 28, 0, 0, 0))
    assert audit.verify_chain()[0] is True

    # R1: TRUNCATE 3 tail rows; the anchor still points at the higher head.
    with Session(engine) as s:
        top = s.exec(text("SELECT id FROM auditlog ORDER BY id DESC LIMIT 3")).all()
        for (rid,) in top:
            s.exec(text("DELETE FROM auditlog WHERE id=:i").bindparams(i=rid))
        s.commit()
    verified_before = _count("audit.chain_verified")
    # Run 6 DAILY sweeps. Each broken sweep appends a forced chain_broken row, refilling ids;
    # by ~day 4 the head reaches the anchor and a naive verify would PASS -> a false recovered=1.
    # The sticky anchor-class posture must keep it BROKEN every day.
    for day in range(1, 7):
        okd, _, _ = audit.run_chain_verification(
            record=True, now=datetime(2026, 8, 28, 0, 0, 0) + timedelta(days=day)
        )
        assert okd is False, f"truncation must stay broken on day {day} (sticky anchor-class)"
    snap = audit.chain_status_snapshot()
    assert snap["state"] == "broken" and snap["break_class"] == "anchor", snap
    assert _count("audit.chain_verified") == verified_before, "NO false recovered=1 across refill"
    # The filesystem witness retains the break even after forced rows refill the deleted ids.
    assert audit.verify_chain()[0] is False, "external witness preserves the rollback signal"
    print("  R1: truncation stays RED across 6 daily sweeps (refill); no false recovered=1")

    # R6: DELETE the status cache. The sticky state must be RE-DERIVED FROM THE LOG (the forced
    # chain_broken class=anchor rows), NOT lost when the deletable Setting row disappears.
    with Session(engine) as s:
        srow = s.get(Setting, "audit_chain_status")
        if srow is not None:
            s.delete(srow)
        s.commit()
    assert audit.chain_status_snapshot() is None, "status cache deleted"
    assert audit._anchor_break_open() is True, "the open anchor break is derivable from the log"
    okd, _, _ = audit.run_chain_verification(
        record=True, now=datetime(2026, 8, 28, 0, 0, 0) + timedelta(days=7)
    )
    assert okd is False, "R6: sticky survives status-row deletion (re-derived from the log)"
    snap6 = audit.chain_status_snapshot()
    assert snap6 and snap6["state"] == "broken" and snap6["break_class"] == "anchor", (
        "cache honestly re-written as broken"
    )
    print("  R6: deleting the status row cannot clear the sticky anchor break (log-derived)")

    # reset-anchor VERB — the SINGLE recovery path: re-baselines AND clears the sticky status.
    _reset_anchor_cli(yes=True, ack=True, expect="ok")
    assert audit.verify_chain()[0] is True, "reset-anchor recovered the chain"
    assert audit.chain_status_snapshot()["state"] == "ok", "reset-anchor cleared the sticky status"
    assert _count("audit.anchor_reset") >= 1, "reset writes a forced audit.anchor_reset row"
    # a normal sweep now flips/stays ok (no lingering sticky refusal)
    okr, _, _ = audit.run_chain_verification(
        record=True, now=datetime(2026, 8, 28, 0, 0, 0) + timedelta(days=8)
    )
    assert okr is True, "post-reset sweep verifies ok"
    print("  R1: reset-anchor is the single recovery path (re-baseline + clears sticky status)")

    # R1: a MID-CHAIN tamper (break_class=tamper) DOES recover when healed. Use a NON-genesis
    # row (settings.update) so the genesis stays pristine for the R2 single-row check below.
    target = sorted(audit.recent(limit=1000, action="settings.update"), key=lambda r: r.id)[0]
    orig_detail = target.detail
    with Session(engine) as s:
        s.exec(text("UPDATE auditlog SET detail='mid' WHERE id=:i").bindparams(i=target.id))
        s.commit()
    okt, _, fbt = audit.run_chain_verification(record=True, now=datetime(2026, 8, 29, 0, 0, 0))
    assert okt is False and isinstance(fbt, int), (okt, fbt)
    assert audit.chain_status_snapshot()["break_class"] == "tamper", "mid-chain break is 'tamper'"
    with Session(engine) as s:
        s.exec(
            text("UPDATE auditlog SET detail=:d WHERE id=:i").bindparams(d=orig_detail, i=target.id)
        )
        s.commit()
    hb = _count("audit.chain_verified")
    okh, _, _ = audit.run_chain_verification(record=True, now=datetime(2026, 8, 29, 1, 0, 0))
    assert okh is True, "a healed mid-chain tamper recovers to ok"
    assert _count("audit.chain_verified") == hb + 1, "tamper recovery writes a heartbeat"
    print("  R1: a mid-chain tamper (class=tamper) still recovers to ok when healed")

    # reset-anchor VERB — refused paths (no --yes; missing the explicit ack).
    _reset_anchor_cli(yes=False, ack=True, expect="refused")
    _reset_anchor_cli(yes=True, ack=False, expect="refused")
    print("  reset-anchor verb: refused without both confirmations")

    # F8: many keyed rows with the anchor deleted must fail verification.
    with Session(engine) as s:
        arow = s.get(Setting, "audit_anchor")
        if arow is not None:
            s.delete(arow)
        s.commit()
    assert audit._read_anchor() is None, "anchor row deleted"
    assert audit.verify_chain()[0] is False, "many keyed rows with NO anchor -> invalid (F8)"
    print("  F8: missing anchor with many keyed rows -> invalid")

    # #278: exactly one surviving keyed row with no anchor is a deletion, not a fresh install.
    with Session(engine) as s:
        s.exec(text("DELETE FROM auditlog WHERE prev_hash != ''"))
        s.commit()
        n = s.exec(text("SELECT COUNT(*) FROM auditlog")).one()[0]
    assert n == 1, f"expected exactly the genesis row, got {n}"
    okr2, checked2, _ = audit.verify_chain()
    assert okr2 is False, (okr2, checked2)
    print("  #278: one surviving keyed row with no anchor is broken")

    # R5: reset-anchor on an EMPTY log writes nothing and claims nothing.
    with Session(engine) as s:
        s.exec(text("DELETE FROM auditlog"))
        s.commit()
    reset_before = _count("audit.anchor_reset")
    captured = {}

    class _Args:
        remote = None
        yes = True
        ack_regress = True

    class _Emit:
        as_json = True
        quiet = False

        def warn(self, *_a, **_k):
            pass

        def result(self, _text, obj):
            captured["obj"] = obj

        def detail(self, *_a, **_k):
            pass

    from app.cli import cmd_db

    assert cmd_db._audit_reset_anchor(_Args(), _Emit()) == 0
    assert captured["obj"].get("no_op") is True and captured["obj"]["new_head"] is None, captured
    assert _count("audit.anchor_reset") == reset_before, "empty-log reset writes NO row"
    print("  R5: reset-anchor on an empty log -> 'nothing to re-baseline', no row written")


def _forge_status(raw: str) -> None:
    """Overwrite the audit_chain_status Setting row with a bare, un-MAC'd forged value."""
    from app.models import Setting

    with Session(engine) as s:
        row = s.get(Setting, "audit_chain_status")
        if row is None:
            row = Setting(key="audit_chain_status", value=raw)
        else:
            row.value = raw
        s.add(row)
        s.commit()


def _reset_anchor_cli(*, yes: bool, ack: bool, expect: str) -> None:
    """Drive cmd_db._audit_reset_anchor with a stub args/emit. expect='ok' -> returns 0;
    expect='refused' -> raises CliError (non-interactive, so confirm() refuses without --yes,
    and the explicit acknowledgement flag is separately required)."""
    from app.cli import cmd_db
    from app.cli._common import CliError

    class _Args:
        remote = None

    args = _Args()
    args.yes = yes
    args.ack_regress = ack

    class _Emit:
        # as_json=True makes confirm() refuse deterministically without --yes (a machine
        # caller must pass --yes) — no dependence on whether stdin is a tty, no prompt.
        as_json = True
        quiet = False

        def warn(self, *_a, **_k):
            pass

        def result(self, *_a, **_k):
            pass

        def detail(self, *_a, **_k):
            pass

    if expect == "ok":
        assert cmd_db._audit_reset_anchor(args, _Emit()) == 0
    else:
        try:
            cmd_db._audit_reset_anchor(args, _Emit())
            raise AssertionError("reset-anchor must refuse without both confirmations")
        except CliError:
            pass


if __name__ == "__main__":
    main()
    test_http_contention()
