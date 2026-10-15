"""Real-process audit append, publication and rotation races (#396)."""

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

if len(sys.argv) < 3:
    init_env("loctms_audit_process_")

from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import AuditLog
from app.services import audit, audit_state

_WAIT_TIMEOUT = 90
_RELEASE_TIMEOUT = 3 * _WAIT_TIMEOUT


def mark(name):
    (config.DATA_DIR / name).write_bytes(b"ready")


def wait(name, timeout=_WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while not (config.DATA_DIR / name).exists():
        assert time.monotonic() < deadline, name
        time.sleep(0.01)


def check_slow_startup():
    # Model an interpreter taking 20 seconds to signal readiness, without slowing
    # every run. The old 15-second deadline fails this same handshake.
    with (
        patch.object(Path, "exists", side_effect=[False] * 20 + [True]),
        patch.object(time, "monotonic", side_effect=range(30)),
        patch.object(time, "sleep"),
    ):
        wait("slow.ready")


def child(case, role):
    original_hash = audit._compute_hash
    original_lock = audit_state.process_lock

    @contextmanager
    def entering_lock():
        mark(f"{case}.{role}.locking")
        with original_lock():
            yield

    audit_state.process_lock = entering_lock

    def compute(*args, **kwargs):
        if args[3] == role or (role == "second" and case.startswith("ledger_")):
            mark(f"{case}.{role}.hash")
            if role == "first" and case in {"append", "killed", "timeout"}:
                wait(f"{case}.release", timeout=_RELEASE_TIMEOUT)
        return original_hash(*args, **kwargs)

    audit._compute_hash = compute
    if role == "first" and case in {"publish", "rotate", "ledger_set", "ledger_purge"}:
        name = "save_epoch" if case == "rotate" else "_prepare"
        original = getattr(audit_state, name)

        def paused(*args, **kwargs):
            mark(f"{case}.paused")
            wait(f"{case}.release", timeout=_RELEASE_TIMEOUT)
            return original(*args, **kwargs)

        setattr(audit_state, name, paused)
    if case.startswith("ledger_"):
        from app.services import ledger_controls

    mark(f"{case}.{role}.ready")
    if role == "second" and case == "timeout":
        from app.db import WriteContentionError

        audit_state._PROCESS_TIMEOUT = 0.1
        audit.fail_open = lambda: True
        audit.log(action=role, force=True)
        audit.fail_open = lambda: False
        try:
            audit.log(action=role, force=True)
        except WriteContentionError:
            pass
        else:
            raise AssertionError("lock timeout must respect fail-closed")
        audit.fail_open = lambda: True
        try:
            audit.run_chain_verification(record=True)
        except WriteContentionError:
            pass
        else:
            raise AssertionError("busy writers must not produce a broken-chain report")
        assert audit.dropped_writes() == 2
        assert not audit._WITNESS_ERROR, "contention must not latch corrupt trust state"
        return
    if role == "second" and case == "ledger_set":
        ledger_controls.set_envelopes(True)
    elif role == "second" and case == "ledger_purge":
        ledger_controls.purge(
            before="2000-01-01", confirm="purge envelopes before 2000-01-01", apply=True
        )
    elif role == "first" and case == "rotate":
        audit.rotate_key()
    else:
        audit.log(action=role, force=True)


def spawn(case, role):
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), case, role],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def main(cases):
    init_db()
    audit.log(action="seed", force=True)
    failures = []
    for case in cases:
        first = spawn(case, "first")
        second = None
        try:
            wait(
                f"{case}.first.hash"
                if case in {"append", "killed", "timeout"}
                else f"{case}.paused"
            )
            second = spawn(case, "second")
            wait(f"{case}.second.ready")
            wait(f"{case}.second.locking")
            # Both interpreters are initialized. The second must not read a predecessor
            # until the first has published its witness / key epoch.
            time.sleep(3)
            assert first.poll() is None, "first holder exited before release"
            blocked = not (config.DATA_DIR / f"{case}.second.hash").exists()
            if case == "timeout":
                out, err = second.communicate(timeout=_WAIT_TIMEOUT)
                assert second.returncode == 0, (out, err)
            if case == "killed":
                first.kill()
                first.communicate(timeout=_WAIT_TIMEOUT)
            else:
                mark(f"{case}.release")
                out, err = first.communicate(timeout=_WAIT_TIMEOUT)
                assert first.returncode == 0, (out, err)
            out, err = second.communicate(timeout=_WAIT_TIMEOUT)
            assert second.returncode == 0, (out, err)
            with Session(engine) as session:
                head = session.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
            state = audit_state.read()
            expected_rows = 2 if case in {"killed", "timeout"} else 3
            intact = audit.verify_chain() == (True, expected_rows, None)
            current = (state["id"], state["hash"]) == (head.id, head.row_hash)
            if case.startswith("ledger_"):
                # Ceremonies commit the DB anchor atomically with their mutation;
                # their filesystem witness is advanced by the next normal log.
                current = (state["id"], state["hash"]) == (head.id - 1, head.prev_hash)
            if case == "rotate":
                current = current and state["epochs"] == len(audit_state.epochs()) == 1
            print(f"{case}: blocked={blocked} chain={intact} witness={current}")
            if not (blocked and intact and current):
                failures.append(case)
                # Keep the cases independent on the pre-fix tree.
                break
        finally:
            for process in (first, second):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate(timeout=_WAIT_TIMEOUT)
    assert not failures, failures
    print("AUDIT PROCESS OK")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        child(*sys.argv[1:])
    elif len(sys.argv) == 2:
        main([sys.argv[1]])
    else:
        check_slow_startup()
        failures = []
        for case in (
            "ledger_set",
            "ledger_purge",
            "append",
            "publish",
            "rotate",
            "killed",
            "timeout",
        ):
            result = subprocess.run([sys.executable, __file__, case], check=False)
            if result.returncode:
                failures.append(case)
        assert not failures, failures
        print("AUDIT PROCESS OK")
