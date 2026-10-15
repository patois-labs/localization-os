"""Prompt Tap retention is bounded by SIZE as well as age, and its state is visible (#616).

The pressure test (T7) saw prompt_tap/ grow about 7 KB per translated segment with no visible
retention. The age sweep existed (30 days) but a busy install could still fill the disk budget
inside the window (the budget only WITHHOLDS new capture), and nothing showed the operator the
size, the caps or the last prune. This suite proves:

  A  SIZE CAP: with nothing aged out, a sweep over a dir above the byte budget deletes the
     OLDEST per-action files until it is under 80 percent of the budget; the newest survive.
  B  PROTECTION: a RUNNING envelope's file is never size-pruned, even when it is the oldest.
  C  SCOPE: only prompt_tap/actions/*.jsonl are deleted; a sibling file in prompt_tap/ and a
     file outside it are untouched.
  D  STAYS BOUNDED: capture + hourly-style forced sweeps over many writes never leave the dir
     above the budget after a sweep.
  E  STATUS: ledger_controls.status() reports raw_bytes, retention_ttl_days,
     retention_max_bytes and last_prune (written by the sweep, survives a latch reset).
  G  FAIL CLOSED: symlinks, just-written files, non-ULID names and wrong-case extensions are
     never size-pruned (Review).
  H  A budget below the 1 MB floor (a misconfiguration) deletes nothing (Review).
  I  org/customer-scoped status withholds the install-wide raw_bytes and last_prune (Review).
  F  WEB + CLI: the Ledger page and `prompt-tap status` show the size, the caps and the prune.

Hermetic: throwaway LOC_TMS_DATA_DIR; ASCII-only prints.
"""

import os
import shutil
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tapretstatus_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import TapAction, TapActionKind, TapRun, TapStatus
from app.services import ledger_controls, prompt_tap, settings, tap_envelope

BUDGET = 20000
# Tests use a tiny budget; the production floor (1 MB) is exercised in test_H below.
PROD_FLOOR = prompt_tap.PRUNE_MIN_BUDGET_BYTES
prompt_tap.PRUNE_MIN_BUDGET_BYTES = 1


def _reset():
    ad = prompt_tap._actions_dir()
    if ad.exists():
        shutil.rmtree(ad, ignore_errors=True)
    state = prompt_tap._prune_state_path()
    if state.exists():
        os.remove(state)
    with Session(engine) as s:
        for m in (TapRun, TapAction):
            for row in s.exec(select(m)).all():
                s.delete(row)
        s.commit()
    prompt_tap._ttl_warn_at.clear()
    prompt_tap._last_sweep_at = 0.0
    with prompt_tap._LOCK:
        prompt_tap._budget_cache["bytes"] = None
        prompt_tap._budget_cache["at"] = 0.0
    ledger_controls.invalidate_scan_cache()
    settings.set_many(
        {
            "prompt_tap_enabled": "1",
            "prompt_tap_ttl_days": "30",
            "prompt_tap_actions_max_bytes": str(BUDGET),
        }
    )


def _capture(aid, prompt="x" * 1000):
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


def _dir_bytes():
    ad = prompt_tap._actions_dir()
    return sum(p.stat().st_size for p in ad.iterdir()) if ad.exists() else 0


def _fill(n, spacing_s=60):
    """n files, file 0 oldest, each `spacing_s` newer; none older than a day."""
    ids = []
    base = time.time() - 3600 - n * spacing_s  # all older than the 10-minute recency guard
    for i in range(n):
        aid = tap_envelope.new_id()
        _capture(aid)
        t = base + i * spacing_s
        os.utime(str(prompt_tap._action_path(aid)), (t, t))
        ids.append(aid)
    return ids


def test_A_size_cap_prunes_oldest():
    _reset()
    # Raise the budget while filling so capture is not withheld, then lower it.
    settings.set_many({"prompt_tap_actions_max_bytes": str(10 * 1024 * 1024)})
    ids = _fill(40)
    total = _dir_bytes()
    assert total > BUDGET, total
    settings.set_many({"prompt_tap_actions_max_bytes": str(BUDGET)})
    with prompt_tap._LOCK:
        prompt_tap._budget_cache["bytes"] = None
    out = prompt_tap.sweep_expired_captures(force=True)
    assert out["deleted"] == 0, out  # nothing is old enough for the age pass
    assert out["size_pruned"] > 0 and out["size_freed_bytes"] > 0, out
    after = _dir_bytes()
    assert after <= BUDGET * prompt_tap.PRUNE_TARGET_FRACTION, (after, out)
    assert not prompt_tap._action_path(ids[0]).exists(), "the oldest file must go first"
    assert prompt_tap._action_path(ids[-1]).exists(), "the newest file must survive"
    print("  (A) over-budget dir pruned oldest-first to the target, newest kept OK")


def test_B_running_envelope_protected():
    _reset()
    settings.set_many({"prompt_tap_actions_max_bytes": str(10 * 1024 * 1024)})
    ids = _fill(40)
    with Session(engine) as s:
        s.add(TapAction(id=ids[0], kind=TapActionKind.WORKFLOW, status=TapStatus.RUNNING))
        s.commit()
    settings.set_many({"prompt_tap_actions_max_bytes": str(BUDGET)})
    prompt_tap.sweep_expired_captures(force=True)
    assert prompt_tap._action_path(ids[0]).exists(), "a RUNNING envelope's file is protected"
    assert not prompt_tap._action_path(ids[1]).exists()
    print("  (B) the oldest file of a RUNNING envelope survives the size prune OK")


def test_C_only_actions_files_touched():
    _reset()
    settings.set_many({"prompt_tap_actions_max_bytes": str(10 * 1024 * 1024)})
    _fill(40)
    sibling = prompt_tap._dir() / "keep-me.txt"
    sibling.write_text("x" * 5000)
    outside = prompt_tap._dir().parent / "outside-prompt-tap.txt"
    outside.write_text("y" * 5000)
    old = time.time() - 400 * 86400
    os.utime(str(sibling), (old, old))
    os.utime(str(outside), (old, old))
    settings.set_many({"prompt_tap_actions_max_bytes": str(BUDGET)})
    prompt_tap.sweep_expired_captures(force=True)
    assert sibling.exists() and outside.exists(), "files outside actions/ must never be deleted"
    os.remove(outside)
    print("  (C) only prompt_tap/actions/*.jsonl files are ever deleted OK")


def test_G_symlink_recent_and_foreign_names_kept():
    """The prune deletes production files, so it fails closed: a symlink named like a capture,
    a file written inside the recency guard, non-ULID names, wrong-case extensions and the
    legacy file are never deleted, and a symlink's TARGET is never touched."""
    _reset()
    settings.set_many({"prompt_tap_actions_max_bytes": str(10 * 1024 * 1024)})
    _fill(40)
    ad = prompt_tap._actions_dir()
    target = prompt_tap._dir().parent / "symlink-target.txt"
    target.write_text("z" * 5000)
    old = time.time() - 400 * 86400
    settled = time.time() - 3600  # settled for the recency guard, far inside the age window
    os.utime(str(target), (settled, settled))
    link = ad / (tap_envelope.new_id() + ".jsonl")
    made_link = True
    try:
        os.symlink(str(target), str(link))
        # The LINK is the oldest entry in the dir, so oldest-first would delete it first.
        os.utime(str(link), (old + 86400, old + 86400), follow_symlinks=False)
    except (OSError, NotImplementedError):
        made_link = False  # Windows without the privilege: the other guards still run
    recent = tap_envelope.new_id()
    _capture(recent)  # mtime == now: inside the recency guard
    odd = {
        ad / "not-a-ulid.jsonl": "a" * 3000,
        ad / (tap_envelope.new_id() + ".JSONL"): "b" * 3000,
        ad / (tap_envelope.new_id() + ".jsonl.tmp"): "c" * 3000,
    }
    for pth, body in odd.items():
        pth.write_text(body)
        settled = time.time() - 3600
        os.utime(str(pth), (settled, settled))
    # A budget so small that EVERY eligible file must go: only the guards can save the rest.
    settings.set_many({"prompt_tap_actions_max_bytes": "1000"})
    out = prompt_tap.sweep_expired_captures(force=True)
    assert out["size_pruned"] > 0, out
    assert target.exists() and target.read_text() == "z" * 5000, "a symlink target was touched"
    if made_link:
        assert os.path.islink(str(link)), "a symlink named like a capture must not be pruned"
    assert prompt_tap._action_path(recent).exists(), "a just-written file is never size-pruned"
    for pth in odd:
        assert pth.exists(), pth
    os.remove(target)
    print("  (G) symlinks, recent files and non-ULID names survive the size prune OK")


def test_H_misconfigured_budget_prunes_nothing():
    _reset()
    settings.set_many({"prompt_tap_actions_max_bytes": str(10 * 1024 * 1024)})
    ids = _fill(40)
    prompt_tap.PRUNE_MIN_BUDGET_BYTES = PROD_FLOOR
    try:
        settings.set_many({"prompt_tap_actions_max_bytes": "512"})  # "512" meant 512 MB
        out = prompt_tap.sweep_expired_captures(force=True)
        assert out["size_pruned"] == 0, out
        assert all(prompt_tap._action_path(i).exists() for i in ids)
    finally:
        prompt_tap.PRUNE_MIN_BUDGET_BYTES = 1
    print("  (H) a budget below the floor deletes nothing OK")


def test_I_scoped_status_withholds_install_wide_figures():
    _reset()
    settings.set_many({"prompt_tap_actions_max_bytes": str(10 * 1024 * 1024)})
    _fill(5)
    prompt_tap.sweep_expired_captures(force=True)
    full = ledger_controls.status()
    assert full["raw_bytes"] and full["last_prune"]
    for kw in ({"scoped": True, "org_id": 1}, {"customer_id": 1}):
        st = ledger_controls.status(**kw)
        assert st["raw_bytes"] is None and st["last_prune"] is None, (kw, st)
        assert st["retention_max_bytes"] == full["retention_max_bytes"]
    assert "freed_bytes" in full["last_prune"] and "/" not in str(full["last_prune"])
    print("  (I) org/customer-scoped status hides raw bytes and last prune; no paths OK")


def test_D_stays_bounded_under_writes():
    _reset()
    pruned = 0
    for i in range(120):
        aid = tap_envelope.new_id()
        _capture(aid)
        old = time.time() - 3600 + i  # settled files; fresh ones are protected by design
        if prompt_tap._action_path(aid).exists():  # absent == withheld at the budget
            os.utime(str(prompt_tap._action_path(aid)), (old, old))
        if i % 10 == 9:
            pruned += prompt_tap.sweep_expired_captures(force=True)["size_pruned"]
            assert _dir_bytes() <= BUDGET, (i, _dir_bytes())
    pruned += prompt_tap.sweep_expired_captures(force=True)["size_pruned"]
    assert _dir_bytes() <= BUDGET
    assert pruned > 0, "the size prune (not only the withhold backstop) must have run"
    print("  (D) 120 captures with periodic sweeps never leave the dir over the cap OK")


def test_E_status_fields():
    _reset()
    settings.set_many({"prompt_tap_actions_max_bytes": str(10 * 1024 * 1024)})
    _fill(40)
    settings.set_many({"prompt_tap_actions_max_bytes": str(BUDGET)})
    before = ledger_controls.status()
    assert before["last_prune"] is None
    assert before["raw_bytes"] == _dir_bytes() and before["raw_bytes"] > BUDGET
    assert before["retention_ttl_days"] == 30 and before["retention_max_bytes"] == BUDGET
    out = prompt_tap.sweep_expired_captures(force=True)
    prompt_tap._last_sweep_at = 0.0  # a "restart": the on-disk state is what status reads
    st = ledger_controls.status()
    lp = st["last_prune"]
    assert lp and lp["size_pruned"] == out["size_pruned"] and lp["at"].endswith("Z"), lp
    assert st["raw_bytes"] == _dir_bytes() <= BUDGET
    print("  (E) status reports size, ttl, cap and the persisted last prune OK")


def test_F_web_and_cli():
    # State from E is still on disk.
    ledger_controls.invalidate_scan_cache()
    with TestClient(app) as client:
        page = client.get("/prompt-tap").text
    assert "data-ledger-retention" in page, "the Ledger page must always show retention"
    assert "Last prune:" in page and "size cap" in page
    from app.cli import cmd_prompt_tap

    summary = cmd_prompt_tap._retention_summary()
    assert summary["max_bytes"] == BUDGET and summary["last_prune"], summary
    print("  (F) the Ledger page and `prompt-tap status` show retention OK")


def test_J_malformed_prune_state_never_crashes():
    """Gate: a truncated or hand-edited prune_state.json must not crash the Ledger page or the
    CLI status line (both add the counters). Bad counters read as 0; a bad record reads as None."""
    import json

    from app.cli import cmd_prompt_tap

    state = prompt_tap._prune_state_path()
    state.write_bytes(json.dumps({"at": "2026-10-08T10:00:00Z", "deleted": "7"}).encode())
    lp = prompt_tap.last_prune_state()
    assert lp and lp["deleted"] == 0 and lp["size_pruned"] == 0 and lp["freed_bytes"] == 0, lp
    with TestClient(app) as client:
        resp = client.get("/prompt-tap")
    assert resp.status_code == 200 and "Last prune:" in resp.text, resp.status_code
    lines = []

    class _Emit:
        def result(self, msg, data):
            lines.append(msg)

    class _Args:
        remote = None

    cmd_prompt_tap._status(_Args(), _Emit())
    assert lines and "last prune: 2026-10-08T10:00:00Z, 0 files removed" in lines[0], lines
    for bad in (b"[1, 2]", b"{not json", json.dumps({"deleted": 3}).encode()):
        state.write_bytes(bad)
        assert prompt_tap.last_prune_state() is None, bad
    print("  (J) a malformed prune state never crashes the page or the CLI status OK")


def main():
    init_db()
    test_A_size_cap_prunes_oldest()
    test_B_running_envelope_protected()
    test_C_only_actions_files_touched()
    test_D_stays_bounded_under_writes()
    test_G_symlink_recent_and_foreign_names_kept()
    test_H_misconfigured_budget_prunes_nothing()
    test_I_scoped_status_withholds_install_wide_figures()
    test_E_status_fields()
    test_F_web_and_cli()
    test_J_malformed_prune_state_never_crashes()
    print("verify_tap_retention_status OK")


if __name__ == "__main__":
    main()
