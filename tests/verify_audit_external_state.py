"""DB-only wipe/rollback and session rotation regressions (#278, #279)."""

import contextlib
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_audit_external_")

from sqlalchemy import delete
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import AuditLog, Setting
from app.services import audit


def main():
    init_db()
    # Empty verification deliberately pins zero. A later DB-only legacy restore
    # needs independently validated, host-side format recovery, not reset-anchor.
    assert audit.verify_chain() == (True, 0, None)
    format_path = config.DATA_DIR / ".audit_format"
    assert json.loads(format_path.read_bytes())["legacy_through"] == 0
    with Session(engine) as s:
        restored = AuditLog(action="restored.v1")
        restored.row_hash = audit._v1_hash(
            "", restored.ts.isoformat(), restored.actor, restored.action, "", "", "", row_id=1
        )
        s.add(restored)
        s.commit()
        restored_id = restored.id
    assert audit.verify_chain() == (False, 0, restored_id)
    detail = audit._format_chain_broken_detail(restored_id, 0, "tamper")
    # The one-line machine detail never carries remediation advice (#401 gate MEDIUM).
    assert ".audit_format" not in detail
    assert audit._parse_break_class(detail) == "tamper"
    hint = audit.legacy_restore_hint(restored_id)
    assert hint and ".audit_format" in hint and "independently validated" in hint
    assert "docs/BACKUP.md" in hint
    from app.cli._common import CliError
    from app.cli.cmd_db_db_init_schema import verify_audit_chain

    results = []
    messages = []

    def _result(text, data):
        messages.append(text)
        results.append(data)

    emit = SimpleNamespace(say=lambda text: None, result=_result)
    try:
        verify_audit_chain(emit)
    except CliError:
        pass
    else:
        raise AssertionError("CLI must reject the restored legacy row above the boundary")
    assert not results[-1]["ok"] and results[-1]["detail"] == detail
    assert results[-1]["hint"] == hint
    assert hint in messages[-1]
    # Simulate the documented recovery on this known-valid legacy-only fixture.
    format_path.unlink()
    assert audit.verify_chain() == (True, 1, None)
    with Session(engine) as s:
        s.exec(delete(AuditLog))
        s.commit()
    # Pre-keying installations must remain readable until their first anchored write.
    with Session(engine) as s:
        s.add(AuditLog(action="legacy"))
        s.commit()
    assert audit.verify_chain() == (True, 0, None)
    assert audit.verify_chain() == (True, 0, None)
    with Session(engine) as s:
        s.exec(delete(AuditLog))
        s.commit()
    for i in range(20):
        audit.log(action="user.role", target=str(i))
    assert audit.verify_chain() == (True, 20, None)
    # Real tamper / truncation must NOT get the delete-.audit_format advice.
    with Session(engine) as s:
        victim = s.exec(select(AuditLog).where(AuditLog.target == "10")).one()
        victim_id, orig_detail = victim.id, victim.detail
        victim.detail = "tampered"
        s.add(victim)
        s.commit()
    assert audit.verify_chain() == (False, victim_id - 1, victim_id)
    assert audit.legacy_restore_hint(victim_id) is None
    del messages[:]
    with contextlib.suppress(CliError):
        verify_audit_chain(emit)
    assert results[-1]["hint"] is None and ".audit_format" not in messages[-1]
    assert f"first_bad_id={victim_id}" in messages[-1]
    with Session(engine) as s:
        victim = s.get(AuditLog, victim_id)
        victim.detail = orig_detail
        s.add(victim)
        s.commit()
    assert audit.verify_chain() == (True, 20, None)
    assert audit.legacy_restore_hint(None) is None
    with Session(engine) as s:
        old_anchor = s.get(Setting, "audit_anchor").value
    original = config.SESSION_SECRET
    config.SESSION_SECRET = "rotated-" + original  # pregate: allow-secret rotation fixture
    rotation_ok = audit.verify_chain()[0]
    for i in range(5):
        audit.log(action="doc.delete", target=str(i))
    rotation_ok = rotation_ok and audit.verify_chain()[0]
    config.SESSION_SECRET = original  # pregate: allow-secret restore fixture
    assert rotation_ok, "session rotation must preserve old rows and new writes"
    audit.run_chain_verification(record=True)
    from app.cli._common import CliError
    from app.cli.cmd_db_db_init_schema import _audit_rotate_key

    captured = []
    emit = SimpleNamespace(result=lambda message, payload: captured.append(payload))
    try:
        _audit_rotate_key(SimpleNamespace(remote="https://invalid.test", yes=True), emit)
    except CliError:
        pass
    else:
        raise AssertionError("remote rotation must be refused before touching local state")
    assert _audit_rotate_key(SimpleNamespace(remote=None, yes=True), emit) == 0
    assert captured[0]["key_id"]
    audit.reset_anchor()
    for _ in range(2):
        audit.rotate_key()
        audit.log(action="after.rotation")
        assert audit.verify_chain()[0], "retired and active epochs must both verify"
        assert audit.chain_status_snapshot()["ok"], "rotation preserves status MAC validity"
    # Reload modules to prove there is no process-only key or high-water cache.
    import importlib

    from app.services import audit_state

    importlib.reload(audit_state)
    assert audit.verify_chain()[0]
    # Possession of a retired key cannot forge the new-key countersignature.
    with Session(engine) as s:
        boundary = s.exec(
            select(AuditLog).where(AuditLog.action == "audit.key_rotated").order_by(AuditLog.id)
        ).first()
        saved = (boundary.id, boundary.detail, boundary.row_hash)
        boundary.detail += " forged"
        boundary.row_hash = audit._compute_hash(
            boundary.prev_hash,
            boundary.ts.isoformat(),
            boundary.actor,
            boundary.action,
            boundary.target,
            boundary.detail,
            boundary.ip,
            row_id=boundary.id,
        )
        s.add(boundary)
        s.commit()
    assert audit.verify_chain()[2] == saved[0], "both signatures at the boundary are required"
    with Session(engine) as s:
        boundary = s.get(AuditLog, saved[0])
        boundary.detail, boundary.row_hash = saved[1:]
        s.add(boundary)
        s.commit()
    assert audit.verify_chain()[0]
    # Restore an older DB snapshot, including a genuinely MAC'd old anchor.
    with Session(engine) as s:
        tail = [r.model_dump() for r in s.exec(select(AuditLog).where(AuditLog.id > 20))]
        current_anchor = s.get(Setting, "audit_anchor").value
        s.exec(delete(AuditLog).where(AuditLog.id > 20))
        anchor = s.get(Setting, "audit_anchor")
        anchor.value = old_anchor
        s.add(anchor)
        s.commit()
    assert not audit.verify_chain()[0], "a valid older DB anchor must not defeat the witness"
    with Session(engine) as s:
        for values in tail:
            s.add(AuditLog(**values))
        anchor = s.get(Setting, "audit_anchor")
        anchor.value = current_anchor
        s.add(anchor)
        s.commit()
    assert audit.verify_chain()[0], "restoring all witnessed rows repairs a read-only rollback"
    with Session(engine) as s:
        first = s.exec(select(AuditLog).order_by(AuditLog.id)).first()
        s.exec(delete(AuditLog).where(AuditLog.id != first.id))
        s.exec(delete(Setting).where(Setting.key.in_(["audit_anchor", "audit_chain_status"])))
        s.commit()
    single_ok = not audit.verify_chain()[0]
    with Session(engine) as s:
        s.exec(delete(AuditLog))
        s.commit()
    wipe_ok = not audit.verify_chain()[0]
    audit.verify_chain_scheduled_sweep(now=datetime.utcnow() + timedelta(days=2))
    wipe_ok = wipe_ok and audit.chain_status_snapshot()["state"] == "broken"
    print(f"session rotation protected: {rotation_ok}")
    print(f"single survivor detected: {single_ok}; full wipe detected: {wipe_ok}")
    assert rotation_ok and single_ok and wipe_ok
    # New writes, even past the old highest id, cannot erase the filesystem latch.
    for _ in range(40):
        audit.log(action="refill")
    assert not audit.verify_chain()[0]
    print("AUDIT EXTERNAL STATE OK")


if __name__ == "__main__":
    main()
