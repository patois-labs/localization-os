"""Audit write cost (#618): a row must not re-open the trust files it already holds.

Before: every audit.log() re-read .audit_key, .audit_format and .audit_head from disk, and read
the anchor Setting twice (about 18 ms per row on Windows, 7k rows = 128 s). The budgets here
are deterministic call counts, not wall time:

- steady state: zero trust-file reads per row and one anchor read per row;
- the cache never hides a change: deleted key, edited epochs file, tail deletion after cached
  writes, a swapped witness, a row edit, a deleted row, a reorder and concurrent writers all
  still end in a failed verification or a counted, refused write.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_audit_perf618_") if len(sys.argv) < 3 else None

from sqlalchemy import text
from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import AuditLog
from app.services import audit, audit_state, settings

TRUST = (".audit_key", ".audit_epochs", ".audit_format", ".audit_head")


audit_state._SETTLE_NS = 150_000_000  # production waits 3 s; the test shrinks the window


def _age_trust_files():
    """Let the rarely-changing files settle, as they do on a running install.

    The cache refuses files whose mtime OR ctime is inside the settle window, and utime()
    cannot age a ctime, so the test really waits (0.25 s against the shrunken window).
    """
    time.sleep(0.25)


def _count_trust_reads(rows):
    reads = []
    real_read = Path.read_bytes

    def counting(self):
        if self.name in TRUST:
            reads.append(self.name)
        return real_read(self)

    anchor_reads = []
    real_anchor = audit._read_anchor

    def counting_anchor():
        anchor_reads.append(1)
        return real_anchor()

    Path.read_bytes = counting
    audit._read_anchor = counting_anchor
    try:
        for i in range(rows):
            audit.log(actor="perf@example.com", action="perf.row", target=str(i))
    finally:
        Path.read_bytes = real_read
        audit._read_anchor = real_anchor
    return reads, anchor_reads


def test_steady_state_call_budget():
    for i in range(3):
        audit.log(actor="perf@example.com", action="perf.warm", target=str(i))
    _age_trust_files()
    audit.log(actor="perf@example.com", action="perf.fill")  # fills the caches from aged files
    rows = 25
    reads, anchor_reads = _count_trust_reads(rows)
    assert reads == [], f"{len(reads)} trust-file reads for {rows} rows: {sorted(set(reads))}"
    assert len(anchor_reads) <= rows, f"{len(anchor_reads)} anchor reads for {rows} rows"
    ok, checked, bad = audit.verify_chain()
    assert ok and bad is None, (ok, checked, bad)
    print(f"  [1] {rows} rows: 0 trust-file reads, {len(anchor_reads)} anchor reads, chain valid")


def test_tamper_cases_still_detected():
    # edit
    audit.log(actor="t@example.com", action="tamper.base", target="a")
    rows = sorted(audit.recent(limit=500), key=lambda r: r.id)
    victim = rows[-3]
    with Session(engine) as s:
        s.exec(text("UPDATE auditlog SET detail='x' WHERE id=:i").bindparams(i=victim.id))
        s.commit()
    ok, _, bad = audit.verify_chain()
    assert not ok and bad == victim.id, (ok, bad, victim.id)
    with Session(engine) as s:
        s.exec(
            text("UPDATE auditlog SET detail=:d WHERE id=:i").bindparams(
                d=victim.detail, i=victim.id
            )
        )
        s.commit()
    assert audit.verify_chain()[0], "restoring the edit must verify again"
    # reorder (swap two adjacent rows' ids)
    a, b = rows[-2], rows[-1]
    with Session(engine) as s:
        s.exec(text("UPDATE auditlog SET id=-1 WHERE id=:i").bindparams(i=a.id))
        s.exec(text("UPDATE auditlog SET id=:a WHERE id=:b").bindparams(a=a.id, b=b.id))
        s.exec(text("UPDATE auditlog SET id=:b WHERE id=-1").bindparams(b=b.id))
        s.commit()
    ok, _, bad = audit.verify_chain()
    assert not ok, "a reorder must fail verification"
    with Session(engine) as s:  # put them back
        s.exec(text("UPDATE auditlog SET id=-1 WHERE id=:i").bindparams(i=a.id))
        s.exec(text("UPDATE auditlog SET id=:a WHERE id=:b").bindparams(a=a.id, b=b.id))
        s.exec(text("UPDATE auditlog SET id=:b WHERE id=-1").bindparams(b=b.id))
        s.commit()
    assert audit.verify_chain()[0], "reorder undone must verify again"
    # delete a middle row
    mid = rows[-5]
    with Session(engine) as s:
        s.exec(text("DELETE FROM auditlog WHERE id=:i").bindparams(i=mid.id))
        s.commit()
        assert not audit.verify_chain()[0], "a deleted row must fail verification"
        s.add(
            AuditLog(
                id=mid.id,
                ts=mid.ts,
                actor=mid.actor,
                action=mid.action,
                target=mid.target,
                detail=mid.detail,
                ip=mid.ip,
                prev_hash=mid.prev_hash,
                row_hash=mid.row_hash,
                customer_id=mid.customer_id,
                org_id=mid.org_id,
            )
        )
        s.commit()
    assert audit.verify_chain()[0], "restored row must verify again"
    print("  [2] edit, reorder and middle deletion are each detected (and clear when undone)")


def test_truncated_tail_survives_cached_writes():
    """The head witness cache must not let a write after a truncation clear the evidence."""
    for i in range(6):
        audit.log(actor="trunc@example.com", action="trunc.row", target=str(i))
    _age_trust_files()
    audit.log(actor="trunc@example.com", action="trunc.fill")
    ok, _, _ = audit.verify_chain()
    assert ok
    with Session(engine) as s:
        top = s.exec(text("SELECT max(id) FROM auditlog")).first()[0]
        s.exec(text("DELETE FROM auditlog WHERE id > :t").bindparams(t=top - 3))
        s.commit()
    ok, _, _ = audit.verify_chain()
    assert not ok, "a truncated tail must fail verification"
    # new writes reuse the deleted ids; the witness must keep the break latched
    for i in range(5):
        audit.log(actor="trunc@example.com", action="trunc.after", target=str(i))
    assert not audit.verify_chain()[0], "writing after a truncation must not heal it"
    print("  [3] tail truncation stays detected, including after further cached writes")
    # re-baseline for the remaining tests (the host-only operator path)
    audit.reset_anchor(actor="perf-618-test")
    assert audit.verify_chain()[0]


def test_cache_never_masks_trust_file_changes():
    _age_trust_files()
    audit.log(actor="k@example.com", action="key.fill")
    key_path = config.DATA_DIR / ".audit_key"
    saved = key_path.read_bytes()
    dropped = audit.dropped_writes()
    key_path.unlink()
    audit.log(actor="k@example.com", action="key.gone")
    assert audit.dropped_writes() == dropped + 1, "a deleted audit key must refuse the write"
    key_path.write_bytes(saved)
    # unreadable trust latches BROKEN until a restart that verifies green; model the restart
    audit._WITNESS_ERROR, audit._WITNESS_NOTE = False, ""
    _age_trust_files()
    audit.log(actor="k@example.com", action="key.back")
    assert audit.verify_chain()[0]
    # a rewritten epochs file with a bad entry must not be hidden by a cached empty list
    epochs = config.DATA_DIR / ".audit_epochs"
    old = epochs.read_bytes() if epochs.exists() else None
    epochs.write_bytes(json.dumps([{"after": 0, "material": "00"}]).encode())
    dropped = audit.dropped_writes()
    audit.log(actor="k@example.com", action="epochs.bad")
    assert audit.dropped_writes() == dropped + 1, "an invalid epochs file must refuse the write"
    audit._WITNESS_ERROR, audit._WITNESS_NOTE = False, ""
    if old is None:
        epochs.unlink()
    else:
        epochs.write_bytes(old)
    audit.log(actor="k@example.com", action="epochs.ok")
    assert audit.verify_chain()[0]
    print("  [4] deleted key and rewritten epochs are seen on the next write (no stale cache)")


def test_swapped_witness_is_seen():
    audit.log(actor="w@example.com", action="witness.a")
    head = config.DATA_DIR / ".audit_head"
    old = head.read_bytes()
    for i in range(3):
        audit.log(actor="w@example.com", action="witness.b", target=str(i))
    newer = audit.verify_chain()
    assert newer[0]
    head.write_bytes(old)  # an older, validly MAC'd witness put back
    assert audit_state.read()["id"] < max(r.id for r in audit.recent(limit=1))
    audit.log(actor="w@example.com", action="witness.c")
    assert audit.verify_chain()[0], "an older witness is only stale, rows still chain"
    print("  [5] a swapped-back witness is re-read, not served from the cache")


def test_concurrent_threads_one_chain():
    base = audit.count()
    errors = []

    def worker(n):
        try:
            for i in range(20):
                audit.log(actor=f"th{n}@example.com", action="thread.row", target=str(i))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert audit.count() == base + 80, (audit.count(), base)
    ok, _, bad = audit.verify_chain()
    assert ok and bad is None, (ok, bad)
    print("  [6] 4 threads x 20 rows: all present, one valid chain")


def _edit_in_place_keep_times(path, new_bytes):
    """Overwrite a file in place (same inode, same size) and put its atime/mtime back."""
    st = os.stat(path)
    assert len(new_bytes) == st.st_size
    with open(path, "r+b") as stream:
        stream.write(new_bytes)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_verify_and_maintenance_never_use_the_caches():
    # Review: the caches belong to the audit WRITE path only. Outside cached_trust() every
    # read goes to the file, so `audit verify` cannot be served a stale key, epochs or format.
    audit.log(actor="v@example.com", action="verify.fill")
    _age_trust_files()
    audit.log(actor="v@example.com", action="verify.fill2")
    reads = []
    real_read = Path.read_bytes

    def counting(self):
        if self.name in TRUST:
            reads.append(self.name)
        return real_read(self)

    Path.read_bytes = counting
    try:
        audit_state.key()
        audit_state.key()
        audit_state.epochs()
        audit_state.epochs()
        assert audit_state.legacy_through(0) is not None
        assert audit_state.legacy_through(0) is not None
        assert audit.verify_chain()[0]
    finally:
        Path.read_bytes = real_read
    assert reads.count(".audit_key") >= 3 and reads.count(".audit_format") >= 3, reads
    # an in-place, same-size key swap with the old mtime restored is seen by verify at once,
    # even though a write-path cache filled from the old key a moment ago
    key_path = config.DATA_DIR / ".audit_key"
    old = key_path.read_bytes()
    swapped = (b"0" if old[:1] != b"0" else b"1") + old[1:]
    _age_trust_files()
    audit.log(actor="v@example.com", action="verify.fill3")
    _edit_in_place_keep_times(key_path, swapped)
    try:
        assert not audit.verify_chain()[0], "verify must read the swapped key, not a cached one"
    finally:
        _edit_in_place_keep_times(key_path, old)
        audit._WITNESS_ERROR, audit._WITNESS_NOTE = False, ""
    assert audit.verify_chain()[0]
    print("  [8] verify and maintenance read key/epochs/format fresh every time")


def test_in_place_witness_edit_with_old_mtime_is_seen():
    if os.name == "nt":  # NTFS st_ctime is the creation time; POSIX ctime catches the edit
        print("  [9] skipped on Windows (ctime is creation time there)")
        return
    audit.log(actor="h@example.com", action="head.a")
    audit.log(actor="h@example.com", action="head.b")  # now own-published and cached
    head = config.DATA_DIR / ".audit_head"
    good = head.read_bytes()
    marker = b'"mac": "'
    at = good.index(marker) + len(marker)
    bad = good[:at] + (b"0" if good[at : at + 1] != b"0" else b"1") + good[at + 1 :]
    _edit_in_place_keep_times(head, bad)
    audit.log(actor="h@example.com", action="head.c")
    assert audit._WITNESS_ERROR, "a forged witness with the old mtime must not be trusted"
    audit._WITNESS_ERROR, audit._WITNESS_NOTE = False, ""
    head.write_bytes(good)
    audit.log(actor="h@example.com", action="head.d")
    assert audit.verify_chain()[0]
    print("  [9] an in-place witness edit that restores mtime is still read and rejected")


def child():
    audit.log(actor="proc@example.com", action="proc.warm")
    for i in range(15):
        audit.log(actor="proc@example.com", action="proc.row", target=f"{os.getpid()}-{i}")


def test_concurrent_processes_one_chain():
    base = audit.count()
    env = dict(os.environ)
    procs = [
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "child", str(n)],
            env=env,
            cwd=_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        for n in range(3)
    ]
    for proc in procs:
        out, _ = proc.communicate(timeout=300)
        assert proc.returncode == 0, out.decode(errors="replace")[-800:]
    # the parent writes too, after the children, with its (now stale) cached witness
    for i in range(5):
        audit.log(actor="proc@example.com", action="proc.parent", target=str(i))
    assert audit.count() == base + 3 * 16 + 5, (audit.count(), base)
    ok, _, bad = audit.verify_chain()
    assert ok and bad is None, (ok, bad)
    print("  [7] 3 processes + parent: every row present, one valid chain")


def main():
    init_db()
    settings.set_many({"audit_enabled": "1"})
    test_steady_state_call_budget()
    test_tamper_cases_still_detected()
    test_truncated_tail_survives_cached_writes()
    test_cache_never_masks_trust_file_changes()
    test_swapped_witness_is_seen()
    test_verify_and_maintenance_never_use_the_caches()
    test_in_place_witness_edit_with_old_mtime_is_seen()
    test_concurrent_threads_one_chain()
    test_concurrent_processes_one_chain()
    print("verify_audit_perf_618: OK")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "child":
        init_db()
        child()
    else:
        main()
