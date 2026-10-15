"""Canonical audit fields and bounded legacy verification (#401), offline."""

import hashlib
import hmac
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_audit_encoding_")

from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import AuditLog, Customer
from app.services import audit


def old_hash(row):
    payload = "|".join(
        [row.prev_hash, row.ts.isoformat(), row.actor, row.action, row.target, row.detail, row.ip]
    )
    return hmac.new(audit._mac_key(), payload.encode(), hashlib.sha256).hexdigest()


def main():
    init_db()
    with Session(engine) as s:
        customers = [Customer(name="A"), Customer(name="B")]
        s.add_all(customers)
        s.commit()
        a, b = [c.id for c in customers]
        s.add(AuditLog(action="unhashed"))
        unkeyed = AuditLog(action="unkeyed")
        unkeyed.row_hash = audit._legacy_hash(
            "", unkeyed.ts.isoformat(), unkeyed.actor, unkeyed.action, "", "", ""
        )
        s.add(unkeyed)
        s.commit()
        legacy = AuditLog(
            action="old", target="doc:1", detail="approved|context", prev_hash=unkeyed.row_hash
        )
        legacy.row_hash = old_hash(legacy)
        s.add(legacy)
        s.commit()
        legacy_id, legacy_hash = legacy.id, legacy.row_hash
    audit._write_anchor(legacy_id, legacy_hash)
    assert audit.verify_chain() == (True, 2, None)
    audit.log(action="new", target="doc:1", detail="approved|context", customer_id=a)
    row = audit.recent(limit=1)[0]
    assert audit.verify_chain() == (True, 3, None)
    failures = []
    for changes in (
        {"target": "doc:1|approved", "detail": "context"},
        {"customer_id": b},
        {"customer_id": None},
    ):
        with Session(engine) as s:
            victim = s.get(AuditLog, row.id)
            for field, value in changes.items():
                setattr(victim, field, value)
            s.add(victim)
            s.commit()
        if audit.verify_chain()[0] or audit.verify_chain(limit=row.id)[0]:
            failures.append(changes)
        with Session(engine) as s:
            victim = s.get(AuditLog, row.id)
            for field in changes:
                setattr(victim, field, getattr(row, field))
            s.add(victim)
            s.commit()
    assert not failures, f"DB-only edits verified: {failures}"
    # Even possession of the test key cannot make a v1 row valid in the new epoch.
    with Session(engine) as s:
        victim = s.get(AuditLog, row.id)
        downgraded_hash = victim.row_hash = old_hash(victim)
        s.add(victim)
        s.commit()
    audit._write_anchor(row.id, downgraded_hash, allow_regress=True)
    assert not audit.verify_chain()[0], "reset must not reopen the legacy epoch"
    with Session(engine) as s:
        victim = s.get(AuditLog, row.id)
        victim.row_hash = row.row_hash
        s.add(victim)
        s.commit()
    audit._write_anchor(row.id, row.row_hash, allow_regress=True)
    assert audit.verify_chain()[0]
    # Boundary survives process reload and cannot be extended via database edits.
    import importlib

    from app.services import audit_state

    importlib.reload(audit_state)
    assert audit.verify_chain()[0]
    boundary_path = config.DATA_DIR / ".audit_format"
    boundary = boundary_path.read_bytes()
    audit.log(action="unicode", target='pipe|quote"', detail="line\n\\\u2603", force=True)
    system_row = audit.recent(limit=1)[0]
    assert audit.verify_chain()[0], "JSON escaping must round-trip"
    with Session(engine) as s:
        victim = s.get(AuditLog, system_row.id)
        victim.customer_id = a
        s.add(victim)
        s.commit()
    assert audit.verify_chain()[2] == system_row.id, "NULL-to-customer must be detected"
    with Session(engine) as s:
        victim = s.get(AuditLog, system_row.id)
        victim.customer_id = None
        s.add(victim)
        s.commit()
    assert audit.verify_chain()[0]
    assert boundary_path.read_bytes() == boundary
    # No fallback to an unsigned new row, even when it is the first row scanned.
    with Session(engine) as s:
        victim = s.get(AuditLog, row.id)
        victim.row_hash = ""
        s.add(victim)
        s.commit()
    assert audit.verify_chain()[2] == row.id
    boundary_path.write_bytes(b'{"version": 99, "legacy_through": 999999}')
    assert not audit.verify_chain()[0], "unknown format state fails closed"
    print("AUDIT_ENCODING OK")


if __name__ == "__main__":
    main()
