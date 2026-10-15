"""Audit hash-chain verification (offline).

- log() links rows via prev_hash/row_hash
- verify_chain() passes on an untampered chain
- tampering with a row's detail is detected (first bad id reported)
- recent()/count() actor and action filters
- concurrent writers do not fork the chain
"""

import json
import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_audit_")

from sqlalchemy import text
from sqlmodel import Session

from app.db import engine, init_db
from app.models import AuditLog
from app.services import audit, settings


def main() -> None:
    init_db()
    settings.set_many({"audit_enabled": "1"})

    # --- chain linkage ---
    audit.log(actor="alice@example.com", action="project.create", target="p1", ip="1.2.3.4")
    audit.log(actor="bob@example.com", action="user.role", target="alice", detail="admin")
    audit.log(actor="alice@example.com", action="settings.update")
    rows = sorted(audit.recent(limit=100), key=lambda r: r.id)
    assert len(rows) == 3, len(rows)
    assert rows[0].prev_hash == "" and rows[0].row_hash, "first row not hashed"
    assert rows[1].prev_hash == rows[0].row_hash, "row 2 not linked to row 1"
    assert rows[2].prev_hash == rows[1].row_hash, "row 3 not linked to row 2"
    assert len(rows[0].row_hash) == 64, "row_hash is not sha256 hex"
    print("  rows linked: prev_hash chains to previous row_hash")

    ok, checked, bad = audit.verify_chain()
    assert ok and checked == 3 and bad is None, (ok, checked, bad)
    print("  verify_chain OK on untampered log (3 checked)")

    # --- filters ---
    assert audit.count() == 3
    alice = audit.recent(actor="ALICE@example.com")  # case-insensitive exact
    assert len(alice) == 2 and all(r.actor == "alice@example.com" for r in alice)
    assert audit.recent(actor="alice") == [], "actor must be exact match"
    assert audit.count(action="user.") == 1, "action substring filter"
    assert audit.count(action="e") == 3
    assert audit.count(actor="bob@example.com", action="user.role") == 1
    print("  filters: actor exact-insensitive, action substring, count agrees")

    # --- tamper detection ---
    victim_id = rows[1].id
    with Session(engine) as s:
        s.exec(
            text("UPDATE auditlog SET detail = 'superadmin' WHERE id = :i").bindparams(i=victim_id)
        )
        s.commit()
    ok, checked, bad = audit.verify_chain()
    assert not ok and bad == victim_id, (ok, checked, bad)
    assert checked == 1, checked  # only the row before the tamper verified
    print("  tampered detail detected at id %d" % victim_id)  # noqa: UP031

    # repair the row so later checks run on a clean chain
    with Session(engine) as s:
        s.exec(text("UPDATE auditlog SET detail = 'admin' WHERE id = :i").bindparams(i=victim_id))
        s.commit()
    ok, _, _ = audit.verify_chain()
    assert ok, "chain should verify again after restoring the value"

    # --- legacy rows (empty row_hash) are skipped, chain still validated ---
    # Host-controlled fixture: explicitly mark only the manufactured historical row.
    from app import config

    (config.DATA_DIR / ".audit_format").write_bytes(
        json.dumps({"version": 2, "legacy_through": rows[0].id}).encode()
    )
    with Session(engine) as s:
        s.exec(
            text("UPDATE auditlog SET row_hash = '', prev_hash = '' WHERE id = :i").bindparams(
                i=rows[0].id
            )
        )
        s.commit()
    # rows[1] was written with prev_hash = rows[0].row_hash, so the chain
    # cannot start cleanly at rows[1]; emulate a true legacy prefix instead:
    # recompute rows[1] as the first hashed row of the chain.
    with Session(engine) as s:
        r1 = s.get(AuditLog, rows[1].id)
        r1.prev_hash = ""
        r1.row_hash = audit._compute_hash(
            "", r1.ts.isoformat(), r1.actor, r1.action, r1.target, r1.detail, r1.ip
        )
        prev = r1.row_hash
        r2 = s.get(AuditLog, rows[2].id)
        r2.prev_hash = prev
        r2.row_hash = audit._compute_hash(
            prev, r2.ts.isoformat(), r2.actor, r2.action, r2.target, r2.detail, r2.ip
        )
        s.add(r1)
        s.add(r2)
        s.commit()
        fixture_head = (r2.id, r2.row_hash)
    # This fixture deliberately rewrote history with the signing key. Re-baseline the
    # filesystem witness too, as a host-controlled migration would, before checking it.
    audit._write_anchor(*fixture_head, allow_regress=True)
    ok, checked, bad = audit.verify_chain()
    assert ok and checked == 2 and bad is None, (ok, checked, bad)
    print("  legacy unhashed leading row skipped; remaining chain verifies")

    # --- concurrency: 8 threads x 10 writes, chain must not fork ---
    before = audit.count()

    def writer(n):
        for i in range(10):
            audit.log(actor="t%d@example.com" % n, action="stress.write", detail="msg %d" % i)  # noqa: UP031

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert audit.count() == before + 80, audit.count()
    ok, checked, bad = audit.verify_chain()
    assert ok and bad is None, (ok, checked, bad)
    assert checked == before + 80 - 1, checked  # -1: legacy first row skipped
    print("  80 concurrent writes; chain intact (%d checked)" % checked)  # noqa: UP031

    # verify_chain(limit=...) walks only the first N rows
    ok, checked, bad = audit.verify_chain(limit=5)
    assert ok and checked == 4 and bad is None, (ok, checked, bad)  # 1 legacy skipped
    print("  verify_chain(limit=5) checks first rows only")

    # --- SIEM export: iter_export yields every row, oldest first ---
    total = audit.count()
    recs = list(audit.iter_export())
    assert len(recs) == total, (len(recs), total)
    ids = [r["id"] for r in recs]
    assert ids == sorted(ids), "export must be oldest-first (ascending id)"
    first = recs[0]
    for key in ("id", "ts", "actor", "action", "target", "detail", "ip", "prev_hash", "row_hash"):
        assert key in first, f"missing export field {key}"
    assert all(r["ts"].endswith("Z") for r in recs), "ts must be explicit UTC"
    print("  iter_export: %d rows, ascending, all fields, UTC timestamps" % total)  # noqa: UP031

    # batching must not skip or duplicate rows
    assert [r["id"] for r in audit.iter_export(batch=7)] == ids
    # filters mirror recent()/count()
    assert len(list(audit.iter_export(actor="t0@example.com"))) == 10
    assert len(list(audit.iter_export(action="stress."))) == 80
    # since: inclusive lower bound on ts
    cutoff = max(r.ts for r in audit.recent(limit=1000))
    expect = {r.id for r in audit.recent(limit=1000) if r.ts >= cutoff}
    got = {r["id"] for r in audit.iter_export(since=cutoff)}
    assert got == expect and got, (len(got), len(expect))
    print("  iter_export: batch=7 identical; actor/action/since filters work")

    # --- SIEM export endpoint: NDJSON stream, filters, bad since -> 400 ---
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get("/audit/export", params={"action": "stress."})
    assert resp.status_code == 200, resp.status_code
    assert resp.headers["content-disposition"].endswith('.ndjson"')
    assert "ndjson" in resp.headers["content-type"]
    lines = [json.loads(ln) for ln in resp.text.strip().splitlines()]
    assert len(lines) == 80, len(lines)
    assert all("stress." in ln["action"] for ln in lines)
    # the export itself was audited (didn't match the action filter above)
    assert audit.count(action="audit.export") == 1
    resp = client.get("/audit/export", params={"since": "not-a-date"})
    assert resp.status_code == 400, resp.status_code
    # a valid since bound returns only newer rows
    resp = client.get("/audit/export", params={"since": "2000-01-01T00:00:00Z"})
    assert resp.status_code == 200
    assert len(resp.text.strip().splitlines()) == audit.count()
    print("  GET /audit/export: NDJSON stream, filtered, audited, 400 on bad since")

    # --- AP-7: the /api twin GET /api/audit/export self-logs an audit.export row
    # too (previously only the web route did). Write BEFORE streaming.
    from app.services import apikeys, security

    before = audit.count(action="audit.export")
    admin = security.create_user(
        "apiaudit@example.com", "API Audit", "admin", "audit-api-pw-123456"
    )
    _row, tok = apikeys.create_key(admin.id, "audit-export", scopes="api docs")
    r = client.get("/api/audit/export", headers={"X-Loc-Token": tok})
    assert r.status_code == 200 and "ndjson" in r.headers["content-type"], r.text
    after = audit.count(action="audit.export")
    assert after == before + 1, (
        "the /api audit export must self-log exactly one audit.export row",
        before,
        after,
    )
    print("  GET /api/audit/export: NDJSON stream self-logs an audit.export row (AP-7)")

    # --- #546: GET /api/audit/export `since` must convert tz-aware input to naive
    # UTC. audit.iter_export compares `since` against the naive-UTC stored ts and
    # SQLite bind params silently drop a tz offset, so a +05:00 bound used to act
    # like the same wall-clock time in UTC and an incremental cursor skipped rows.
    from datetime import datetime

    fixtures = [
        ("2026-10-01T04:00:00", "since_offset.below"),  # strictly before the 05:00Z bound
        ("2026-10-01T05:00:00", "since_offset.at"),  # exactly at the bound (inclusive)
        ("2026-10-01T07:00:00", "since_offset.after"),
    ]
    fixture_ids = {}
    with Session(engine) as s:
        head = s.exec(text("SELECT row_hash FROM auditlog ORDER BY id DESC LIMIT 1")).first()
        prev_hash = head[0] if head else ""
        for ts_str, act in fixtures:
            ts = datetime.fromisoformat(ts_str)
            row = AuditLog(ts=ts, actor="since-offset@example.com", action=act, prev_hash=prev_hash)
            row.row_hash = audit._compute_hash(
                prev_hash, ts.isoformat(), row.actor, act, "", "", "", customer_id=None
            )
            s.add(row)
            s.commit()
            fixture_ids[act] = row.id
            prev_hash = row.row_hash
    assert fixture_ids["since_offset.below"] < fixture_ids["since_offset.at"]
    assert fixture_ids["since_offset.at"] < fixture_ids["since_offset.after"]
    print("  since-offset fixtures: 3 rows at known naive-UTC times (04:00, 05:00, 07:00)")

    def _since_ids(value):
        r = client.get("/api/audit/export", params={"since": value}, headers={"X-Loc-Token": tok})
        assert r.status_code == 200, (value, r.status_code, r.text)
        return {json.loads(ln)["id"] for ln in r.text.strip().splitlines()}

    def _check_since(label, value, cutoff):
        # The export must equal exactly the rows whose naive-UTC ts is >= cutoff.
        got = _since_ids(value)
        want = {r.id for r in audit.recent(limit=audit.count() + 10) if r.ts >= cutoff}
        assert got == want, (label, value, sorted(got ^ want))

    utc_cut = datetime(2026, 10, 1, 5, 0)
    # 10:00+05:00 == 05:00 UTC: `below` excluded, `at` and `after` included.
    _check_since("+05:00 offset", "2026-10-01T10:00:00+05:00", utc_cut)
    _check_since("Z suffix", "2026-10-01T05:00:00Z", utc_cut)
    # naive and date-only bounds behave exactly as before (naive == UTC).
    _check_since("naive", "2026-10-01T05:00:00", utc_cut)
    _check_since("date-only", "2026-10-01", datetime(2026, 10, 1, 0, 0))
    print("  GET /api/audit/export: since offsets normalized to UTC; naive/date-only unchanged")

    # The CLI twin (`audit export --since`) parses through _parse_since: same normalization.
    from app.cli.cmd_admin_helpers import _parse_since

    assert _parse_since("2026-10-01T10:00:00+05:00") == datetime(2026, 10, 1, 5, 0)
    assert _parse_since("2026-10-01T05:00:00Z") == datetime(2026, 10, 1, 5, 0)
    assert _parse_since("2026-10-01T05:00:00") == datetime(2026, 10, 1, 5, 0)
    assert _parse_since("2026-10-01") == datetime(2026, 10, 1, 0, 0)
    print("  CLI audit export --since: offsets normalized to naive UTC (parity with the API)")

    print("AUDIT OK")


if __name__ == "__main__":
    main()
