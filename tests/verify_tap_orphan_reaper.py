"""Prompt Tap ORPHAN REAPER -- residual sweep ("no reaper for orphaned actions").

An envelope (TapAction / TapRun) left RUNNING by a crash / kill / thread-death lingered
forever. tap_envelope.reap_orphaned_envelopes closes such a row with the terminal
ORPHANED marker (never a delete) once it is PROVABLY dead: aged past the threshold AND
with no live backing production run. This suite proves:

  A  An aged RUNNING action + its RUNNING leg are reaped -> ORPHANED, finished_at stamped
     (the hierarchy is preserved -- rows transitioned, not deleted).
  B  A FRESH RUNNING action (young) is UNTOUCHED (no behavior change for live envelopes).
  C  A LIVE backing run protects an aged action: while its WorkflowRun is non-terminal the
     whole action is skipped; once that run is terminal the action reaps.
  D  PAUSED is never reaped (durable human-review state) -- neither an aged PAUSED action
     nor a PAUSED leg under an otherwise-reaped action.
  E  IDEMPOTENT: a second pass reaps nothing.
  F  DISABLED: max_age_hours=0 (the "0 disables" setting) reaps nothing.
  G  STRAY LEG: an aged RUNNING TapRun under an already-TERMINAL action is reaped alone.
  H  ROLLUP HONESTY: an all-terminal action containing an ORPHANED leg rolls up to
     ORPHANED, never COMPLETED.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys; ASCII-only prints.
"""

import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_taprreap_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    RunStatus,
    TapAction,
    TapActionKind,
    TapRun,
    TapStatus,
    TransMode,
    Workflow,
    WorkflowRun,
)
from app.services import settings, tap_envelope

_SEQ = [0]
_NOW = datetime(2026, 8, 9, 12, 0, 0)


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _reset():
    with Session(engine) as s:
        for m in (TapRun, TapAction, WorkflowRun, Document, Project, Workflow):
            for row in s.exec(select(m)).all():
                s.delete(row)
        s.commit()
    settings.set_many({"prompt_tap_envelopes_enabled": "1"})
    tap_envelope._WARNED = False


def _mk_action(s, *, status=TapStatus.RUNNING, age_hours=48, finished=None):
    a = TapAction(
        id=tap_envelope.new_id(),
        kind=TapActionKind.DIRECT,
        status=status,
        started_at=_NOW - timedelta(hours=age_hours),
        finished_at=finished,
        created_at=_NOW - timedelta(hours=age_hours),
    )
    s.add(a)
    s.commit()
    return a.id


def _mk_run(s, action_id, *, status=TapStatus.RUNNING, age_hours=48, wf_run_id=None):
    r = TapRun(
        id=tap_envelope.new_id(),
        action_id=action_id,
        status=status,
        workflow_run_id=wf_run_id,
        started_at=_NOW - timedelta(hours=age_hours),
        created_at=_NOW - timedelta(hours=age_hours),
    )
    s.add(r)
    s.commit()
    return r.id


def _mk_wf_run(s, status):
    wf = Workflow(name="reap-wf-%d" % _seq(), scope="global")  # noqa: UP031
    s.add(wf)
    proj = Project(name="reap-proj-%d" % _seq(), source_locale="en-US", default_provider="mock")  # noqa: UP031
    s.add(proj)
    s.commit()
    s.refresh(wf)
    s.refresh(proj)
    doc = Document(project_id=proj.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED)
    s.add(doc)
    s.commit()
    s.refresh(doc)
    run = WorkflowRun(workflow_id=wf.id, document_id=doc.id, target_locale="es-ES", status=status)
    s.add(run)
    s.commit()
    s.refresh(run)
    return run.id


def _status(model, row_id):
    with Session(engine) as s:
        row = s.get(model, row_id)
        return None if row is None else (row.status, row.finished_at)


def test_A_aged_running_reaped():
    _reset()
    with Session(engine) as s:
        aid = _mk_action(s)
        rid = _mk_run(s, aid)  # direct leg, no backing run
    out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert out == {"actions": 1, "runs": 1}, out
    a_status, a_fin = _status(TapAction, aid)
    r_status, r_fin = _status(TapRun, rid)
    assert a_status == TapStatus.ORPHANED, a_status
    assert r_status == TapStatus.ORPHANED, r_status
    assert a_fin == _NOW and r_fin == _NOW, (a_fin, r_fin)
    # Preserved, not deleted: the rows still exist.
    with Session(engine) as s:
        assert s.get(TapAction, aid) is not None
        assert s.get(TapRun, rid) is not None
    print("A ok: aged RUNNING action+leg reaped -> ORPHANED, finished stamped, preserved")


def test_B_fresh_untouched():
    _reset()
    with Session(engine) as s:
        aid = _mk_action(s, age_hours=1)  # young
        rid = _mk_run(s, aid, age_hours=1)
    out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert out == {"actions": 0, "runs": 0}, out
    assert _status(TapAction, aid)[0] == TapStatus.RUNNING
    assert _status(TapRun, rid)[0] == TapStatus.RUNNING
    print("B ok: fresh RUNNING envelope untouched")


def test_C_live_backing_run_protects():
    _reset()
    with Session(engine) as s:
        live_run = _mk_wf_run(s, RunStatus.RUNNING)
        aid = _mk_action(s)
        _mk_run(s, aid, wf_run_id=live_run)
    out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert out == {"actions": 0, "runs": 0}, out
    assert _status(TapAction, aid)[0] == TapStatus.RUNNING, "a live run must protect its action"
    # Now flip the backing run terminal -> the action is reapable.
    with Session(engine) as s:
        run = s.get(WorkflowRun, live_run)
        run.status = RunStatus.COMPLETED
        s.add(run)
        s.commit()
    out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert out == {"actions": 1, "runs": 1}, out
    assert _status(TapAction, aid)[0] == TapStatus.ORPHANED
    print("C ok: live backing run protects; terminal backing run reaps")


def test_D_paused_handling_M2():
    # D1: an aged PAUSED action is never even a reap candidate (not RUNNING).
    _reset()
    with Session(engine) as s:
        paused_aid = _mk_action(s, status=TapStatus.PAUSED)
    tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert _status(TapAction, paused_aid)[0] == TapStatus.PAUSED, "paused action must survive"

    # D2: a RUNNING action with a PAUSED leg whose backing run is LIVE -> SKIPPED entirely
    # (a human is genuinely mid-review; the action is never terminalized).
    _reset()
    with Session(engine) as s:
        live = _mk_wf_run(s, RunStatus.PAUSED)
        aid = _mk_action(s)
        prid = _mk_run(s, aid, status=TapStatus.PAUSED, wf_run_id=live)
    out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert out == {"actions": 0, "runs": 0}, out
    assert _status(TapAction, aid)[0] == TapStatus.RUNNING, "live paused leg protects action"
    assert _status(TapRun, prid)[0] == TapStatus.PAUSED

    # D3: a PAUSED leg whose backing run is TERMINAL (completed) -> the leg is CLOSED to that
    # terminal, and the action rolls up HONESTLY (COMPLETED, not ORPHANED).
    _reset()
    with Session(engine) as s:
        done = _mk_wf_run(s, RunStatus.COMPLETED)
        aid = _mk_action(s)
        prid = _mk_run(s, aid, status=TapStatus.PAUSED, wf_run_id=done)
    out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert _status(TapRun, prid)[0] == TapStatus.COMPLETED, "paused leg closed to backing terminal"
    assert _status(TapAction, aid)[0] == TapStatus.COMPLETED, "honest rollup, not orphaned"

    # D3b: a PAUSED leg whose backing run was CANCELLED -> leg ABANDONED (a drop, not failed).
    _reset()
    with Session(engine) as s:
        cancelled = _mk_wf_run(s, RunStatus.CANCELLED)
        aid = _mk_action(s)
        prid = _mk_run(s, aid, status=TapStatus.PAUSED, wf_run_id=cancelled)
    tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert _status(TapRun, prid)[0] == TapStatus.ABANDONED

    # D4: a PAUSED leg whose backing run is GONE (no terminal evidence) -> action SKIPPED.
    _reset()
    with Session(engine) as s:
        aid = _mk_action(s)
        prid = _mk_run(s, aid, status=TapStatus.PAUSED)  # no wf_run_id -> no evidence
    out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert out == {"actions": 0, "runs": 0}, out
    assert _status(TapAction, aid)[0] == TapStatus.RUNNING, "no terminal evidence -> skip"
    assert _status(TapRun, prid)[0] == TapStatus.PAUSED
    print(
        "D ok (M2): paused leg -- live/no-evidence skip; terminal backing closes leg + honest rollup"  # noqa: E501
    )


def test_I_honest_rollup_M1():
    # An aged RUNNING action whose legs are ALL already terminal (stuck running) rolls up to
    # the HONEST status, NOT a blanket ORPHANED (M1).
    _reset()
    with Session(engine) as s:
        aid = _mk_action(s)
        _mk_run(s, aid, status=TapStatus.COMPLETED)
        _mk_run(s, aid, status=TapStatus.COMPLETED)
    tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert _status(TapAction, aid)[0] == TapStatus.COMPLETED, (
        "all-completed must NOT become orphaned"
    )

    _reset()
    with Session(engine) as s:
        aid = _mk_action(s)
        _mk_run(s, aid, status=TapStatus.FAILED)
    tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert _status(TapAction, aid)[0] == TapStatus.FAILED, "failed leg must NOT be erased"
    print("I ok (M1): all-terminal legs roll up honestly (completed/failed), not blanket ORPHANED")


def test_J_zero_leg_abandoned_L8():
    _reset()
    with Session(engine) as s:
        aid = _mk_action(s)  # no legs at all
    tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert _status(TapAction, aid)[0] == TapStatus.ABANDONED, "zero-leg aged action -> ABANDONED"
    print("J ok (L8): zero-leg aged RUNNING action -> ABANDONED")


def test_K_runs_when_envelopes_disabled_L5():
    _reset()
    settings.set_many({"prompt_tap_envelopes_enabled": "0"})  # recording OFF
    try:
        with Session(engine) as s:
            aid = _mk_action(s)
            _mk_run(s, aid)  # frozen RUNNING row from before the toggle
        out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
        assert out == {"actions": 1, "runs": 1}, out
        assert _status(TapAction, aid)[0] == TapStatus.ORPHANED
    finally:
        settings.set_many({"prompt_tap_envelopes_enabled": "1"})
    print("K ok (L5): reaper closes frozen RUNNING rows even when recording is disabled")


def test_E_idempotent():
    _reset()
    with Session(engine) as s:
        aid = _mk_action(s)
        _mk_run(s, aid)
    tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    out2 = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert out2 == {"actions": 0, "runs": 0}, out2
    print("E ok: idempotent (second pass reaps nothing)")


def test_F_disabled():
    _reset()
    with Session(engine) as s:
        aid = _mk_action(s)
        _mk_run(s, aid)
    out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=0)
    assert out == {"actions": 0, "runs": 0}, out
    assert _status(TapAction, aid)[0] == TapStatus.RUNNING
    # And via the setting seam ("0" disables).
    settings.set_many({"prompt_tap_orphan_reap_hours": "0"})
    assert tap_envelope.orphan_reap_hours() == 0
    out2 = tap_envelope.reap_orphaned_envelopes(now=_NOW)
    assert out2 == {"actions": 0, "runs": 0}, out2
    settings.set_many({"prompt_tap_orphan_reap_hours": "24"})
    assert tap_envelope.orphan_reap_hours() == 24
    print("F ok: reaper disabled by max_age_hours=0 / setting '0'")


def test_G_stray_leg_under_terminal_action():
    _reset()
    with Session(engine) as s:
        aid = _mk_action(s, status=TapStatus.COMPLETED, finished=_NOW - timedelta(hours=40))
        rid = _mk_run(s, aid, status=TapStatus.RUNNING)  # crash between leg + action finalize
    out = tap_envelope.reap_orphaned_envelopes(now=_NOW, max_age_hours=24)
    assert out == {"actions": 0, "runs": 1}, out
    assert _status(TapRun, rid)[0] == TapStatus.ORPHANED
    assert _status(TapAction, aid)[0] == TapStatus.COMPLETED, "settled action left as-is"
    print("G ok: stray RUNNING leg under terminal action reaped alone")


def test_H_rollup_orphaned_not_completed():
    from app.services.tap_envelope import _action_rollup_status

    assert _action_rollup_status([TapStatus.COMPLETED, TapStatus.ORPHANED]) == TapStatus.ORPHANED
    assert _action_rollup_status([TapStatus.FAILED, TapStatus.ORPHANED]) == TapStatus.FAILED
    assert _action_rollup_status([TapStatus.COMPLETED, TapStatus.COMPLETED]) == TapStatus.COMPLETED
    print("H ok: rollup surfaces ORPHANED over COMPLETED, FAILED over ORPHANED")


def main():
    init_db()
    test_A_aged_running_reaped()
    test_B_fresh_untouched()
    test_C_live_backing_run_protects()
    test_D_paused_handling_M2()
    test_E_idempotent()
    test_F_disabled()
    test_G_stray_leg_under_terminal_action()
    test_H_rollup_orphaned_not_completed()
    test_I_honest_rollup_M1()
    test_J_zero_leg_abandoned_L8()
    test_K_runs_when_envelopes_disabled_L5()
    print("verify_tap_orphan_reaper OK")


if __name__ == "__main__":
    main()
