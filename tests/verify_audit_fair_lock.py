"""S3c gate MED-4 (ADR 0097): the audit chain's cross-process lock is fair in the split roles.

Every mutating request appends to the tamper-evident audit chain under a cross-process OS lock
(`app/services/audit_state.py::process_lock`). Before S3c a process kept that lock for as long as
ANY of its threads was a user, so a steadily busy process starved its peers (measured on
Postgres: 1,453 rows against 8 and 8, a 15.6 s wait). Fair mode (roles web and worker): a
process that cannot get the lock marks itself waiting, and a holder past its tenure yields only
when it sees that mark.

Offline, a throwaway LOC_TMS_DATA_DIR (SQLite). Checks:

  [A] Three role-web processes with eight threads each append for a few seconds: every append
      succeeds (no ProcessLockError / timeout), the chain verifies afterwards, and no process
      is starved (each wrote at least a fifth of the busiest one's rows).
  [B] A nested entry made while the process is yielding does not wait (no self-deadlock), and
      an uncontended holder never yields (no waiter mark, no handoff).
  [C] A peer that holds the waiter mark is seen as waiting; an unopenable mark file is not.

NEW BEHAVIOUR vs the pre-S3c tree: [A] fails there (one process takes nearly every append),
[B] and [C] have nothing to test (no fair mode).
"""

import json
import os
import subprocess
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
os.environ["LOC_TMS_ROLE"] = "web"
from tests._bootstrap import init_env

DATA = init_env("loctms_auditfair_")

from app.db import init_db
from app.services import audit, audit_state

PY = sys.executable
SECS = 6.0
THREADS = 8
PROCS = 3

_CHILD = r"""
import json, sys, threading, time
sys.path.insert(0, sys.argv[1])
from app.services import audit
secs, threads = float(sys.argv[2]), int(sys.argv[3])
counts, errors = [0], []
lock = threading.Lock()
start = time.time() + 1.0
end = start + secs
def run():
    while time.time() < start:
        time.sleep(0.01)
    n = 0
    while time.time() < end:
        try:
            audit.log("fair@x", "fair.append", "t", "d")
            n += 1
        except Exception as exc:
            with lock:
                errors.append(repr(exc)[:200])
    with lock:
        counts[0] += n
ths = [threading.Thread(target=run) for _ in range(threads)]
for t in ths: t.start()
for t in ths: t.join()
print("FAIR " + json.dumps({"count": counts[0], "errors": errors[:5]}), flush=True)
"""


def _fail(msg):
    raise AssertionError(msg)


def check_three_processes():
    env = dict(os.environ, LOC_TMS_DATA_DIR=DATA, LOC_TMS_ROLE="web", PYTHONUTF8="1")
    kids = [
        subprocess.Popen(
            [PY, "-c", _CHILD, _ROOT, str(SECS), str(THREADS)],
            cwd=_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(PROCS)
    ]
    counts, errors = [], []
    for k in kids:
        out, err = k.communicate(timeout=SECS + 180)
        line = next((ln for ln in out.decode().splitlines() if ln.startswith("FAIR ")), None)
        if line is None:
            _fail(f"an appender process failed: {err.decode('utf-8', 'replace')[-1500:]!r}")
        got = json.loads(line[5:])
        counts.append(got["count"])
        errors += got["errors"]
    if errors:
        _fail(f"expected every append to succeed under contention; got {errors[:3]}")
    ok, checked, bad = audit.verify_chain()
    if not ok:
        _fail(f"expected the chain to verify after {sum(counts)} cross-process appends; bad {bad}")
    if min(counts) * 5 < max(counts):
        _fail(f"expected no starved process; appends per process {counts}")
    print(
        f"  [A] 3 processes x {THREADS} threads: appends {counts}, chain of {checked} verifies OK"
    )


def check_nested_and_uncontended():
    done = threading.Event()

    def nested():
        with audit_state.process_lock():
            audit_state._YIELDING = True
            try:
                with audit_state.process_lock():
                    done.set()
            finally:
                audit_state._YIELDING = False

    th = threading.Thread(target=nested, daemon=True)
    th.start()
    th.join(10)
    if not done.is_set():
        audit_state._YIELDING = False
        _fail("expected a nested entry to pass while its process is yielding (self-deadlock)")
    # Uncontended: hold past the tenure with entries in flight; with no waiter mark the holder
    # never yields (a timer-driven handoff cost 27 % of throughput in the S3c benchmark).
    yielded = []
    with audit_state.process_lock():
        time.sleep(audit_state._TENURE_MAX_S * 3)
        with audit_state.process_lock():
            pass

        def entrant():
            with audit_state.process_lock():
                yielded.append(1)

        th2 = threading.Thread(target=entrant)
        th2.start()
        th2.join(5)
        if not yielded or audit_state._YIELDING:
            _fail("expected an uncontended holder past its tenure to keep admitting entrants")
    print("  [B] nested entry while yielding passes; an uncontended holder never yields OK")


def check_waiter_mark():
    if audit_state._peer_waiting():
        _fail("expected no waiting peer before anyone marks")
    env = dict(os.environ, LOC_TMS_DATA_DIR=DATA, LOC_TMS_ROLE="web", PYTHONUTF8="1")
    code = (
        "import sys, time; sys.path.insert(0, sys.argv[1]);"
        "from app.services import audit_state;"
        "print('MARKED', audit_state._mark_waiting(), flush=True); time.sleep(30)"
    )
    peer = subprocess.Popen([PY, "-c", code, _ROOT], cwd=_ROOT, env=env, stdout=subprocess.PIPE)
    try:
        line = peer.stdout.readline().decode()
        if "MARKED True" not in line:
            _fail(f"expected the peer to take the waiter mark; got {line!r}")
        if not audit_state._peer_waiting():
            _fail("expected a peer holding the waiter mark to read as waiting")
    finally:
        peer.kill()
        peer.wait(10)
    real = audit_state._wait_fd
    try:
        audit_state._wait_fd = lambda: (_ for _ in ()).throw(PermissionError(13, "denied"))
        if audit_state._peer_waiting():
            _fail("expected an unopenable mark file to read as no waiting peer (gate LOW-2)")
    finally:
        audit_state._wait_fd = real
    print("  [C] a marked peer reads as waiting; an unopenable mark file does not OK")


def main():
    init_db()
    audit.log("fair@x", "fair.warm", "t", "d")
    check_three_processes()
    check_nested_and_uncontended()
    check_waiter_mark()
    print("verify_audit_fair_lock: all checks OK")


if __name__ == "__main__":
    main()
