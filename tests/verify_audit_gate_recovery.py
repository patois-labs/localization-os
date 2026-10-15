"""F21 gate regressions; each scenario gets an isolated install and process."""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def scenario(name):
    from tests._bootstrap import init_env

    init_env("loctms_audit_gate_")
    from app import config, db
    from app.services import audit, audit_state

    db.init_db()
    if name == "shed":
        # Reuses f21-gate/shed_probe.py's first-anchor-only commit fault.
        real = db.commit_or_shed
        calls = []

        def shed(s, stage, attempts=2, **kw):
            if attempts == 1 and not calls:
                calls.append(True)
                return False
            return real(s, stage=stage, attempts=attempts, **kw)

        with patch.object(db, "commit_or_shed", shed):
            audit.log(action="first")
        assert audit.verify_chain() == (True, 1, None), "shed genesis is not a truncation"
        for _ in range(3):
            audit.log(action="later")
        assert audit.verify_chain()[0], "first anchor shed must self-heal"
        return
    audit.log(action="seed")
    if name == "publish":
        real_replace = os.replace
        attempts = []

        def sharing(src, dst):
            attempts.append(dst)
            if len(attempts) < 3:
                raise PermissionError("reader holds witness")
            return real_replace(src, dst)

        with patch.object(os, "replace", sharing):
            audit.log(action="retry.publish")
        assert len(attempts) == 3, "publish must briefly retry sharing violations"
        assert not audit._WITNESS_ERROR
        with (
            patch.object(os, "replace", side_effect=PermissionError("still busy")),
            patch.object(audit_state, "_logger") as logger,
        ):
            audit.log(action="deferred.publish")
            assert logger.warning.called
        assert not audit._WITNESS_ERROR, "failed publish must not latch"
        audit.log(action="republish")
        assert audit.run_chain_verification(record=True)[0]
        assert audit.chain_status_snapshot()["state"] == "ok"
        (config.DATA_DIR / ".audit_head").unlink()
        with patch.object(os, "replace", side_effect=PermissionError("initialization busy")):
            assert audit.verify_chain()[0], "unbuffered publish failure must not latch"
        assert not audit._WITNESS_ERROR
    elif name == "trust_restart":
        # latch_reset.py: a separate reset cannot clear this process's latch.
        saved = (config.DATA_DIR / ".audit_head").read_bytes()
        (config.DATA_DIR / ".audit_head").write_bytes(b"garbage")
        assert not audit.verify_chain()[0]
        (config.DATA_DIR / ".audit_head").write_bytes(saved)
        audit.run_chain_verification(record=True)
        assert audit.chain_status_snapshot()["break_class"] == "trust"
        assert not audit._anchor_break_open(), "trust latch must not become sticky anchor"
        # A fresh process has no latch; retained rows/status must recover without reset.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from app.services import audit; "
                "assert audit.run_chain_verification(record=True)[0]; "
                "assert audit.chain_status_snapshot()['state'] == 'ok'",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    elif name == "missing_signing":
        from fastapi.testclient import TestClient

        from app.main import app

        audit.rotate_key()
        for filename in (".audit_key", ".audit_epochs"):
            path = config.DATA_DIR / filename
            saved = path.read_bytes()
            path.unlink()
            before = audit.dropped_writes()
            with patch.object(audit, "fail_open", return_value=True):
                audit.log(action="missing.open")
            assert audit.dropped_writes() == before + 1, "missing signing row counted"
            with (
                patch.object(audit, "fail_open", return_value=True),
                patch.object(audit, "_SIGNING_DROP_LOG_AT", None),
                patch.object(audit.time, "monotonic", return_value=100) as clock,
                patch.object(audit._logger, "error") as errors,
            ):
                for _ in range(3):
                    audit.log(action="missing.burst")
                assert errors.call_count == 1, "drop logs must be rate limited"
                clock.return_value = 102
                audit.log(action="missing.later")
                assert errors.call_count == 2, "drop logs must resume with the running total"
            assert audit.dropped_writes() == before + 5
            with patch.object(audit, "fail_open", return_value=False):
                try:
                    audit.log(action="missing.closed")
                except db.WriteContentionError:
                    pass
                else:
                    raise AssertionError("missing signing material must honor fail-closed")
                token = audit.REQUEST_RECORDED.set(True)
                try:
                    audit.log(action="missing.detail")
                finally:
                    audit.REQUEST_RECORDED.reset(token)
                client = TestClient(app, raise_server_exceptions=False)
                assert client.post("/login").status_code == 503
            assert audit.dropped_writes() >= before + 8
            path.write_bytes(saved)
            audit.reset_anchor(actor="gate")
    elif name == "reset_cli":
        from app.cli import backup_archive
        from app.cli._common import CliError
        from app.cli.cmd_db_db_init_schema import _audit_reset_anchor

        args = SimpleNamespace(remote=None, yes=True, ack_regress=True)
        emit = SimpleNamespace(result=lambda *a: None)
        with patch.object(backup_archive, "server_appears_running", return_value=(True, "host")):
            try:
                _audit_reset_anchor(args, emit)
            except CliError as exc:
                assert "Stop the server" in str(exc)
            else:
                raise AssertionError("reset must refuse a listening server")
    elif name == "reset_corrupt":
        from app.cli import backup_archive
        from app.cli._common import CliError
        from app.cli.cmd_db_db_init_schema import _audit_reset_anchor

        (config.DATA_DIR / ".audit_key").write_bytes(b"not hex")
        with patch.object(backup_archive, "server_appears_running", return_value=(False, "")):
            try:
                _audit_reset_anchor(
                    SimpleNamespace(remote=None, yes=True, ack_regress=True),
                    SimpleNamespace(result=lambda *a: None),
                )
            except CliError as exc:
                assert "restore matching trust files" in str(exc), str(exc)
            else:
                raise AssertionError("corrupt signing key must refuse reset")
    elif name == "posture":
        from sqlmodel import Session

        from app.models import AuditLog

        head = audit_state.read()
        with Session(db.engine) as session:
            session.delete(session.get(AuditLog, head["id"]))
            session.commit()
        with (
            patch.object(audit, "fail_open", return_value=False),
            patch.object(db, "commit_or_shed", return_value=False),
            patch.object(audit_state, "_prepare", side_effect=PermissionError("probe")),
        ):
            try:
                audit.log(action="must.refuse")
            except db.WriteContentionError:
                pass
            else:
                raise AssertionError("witness failure must not mask fail-closed DB refusal")
    elif name == "unreadable":
        foreign = json.loads((config.DATA_DIR / ".audit_head").read_bytes())
        foreign["mac"] = hmac.new(
            os.urandom(32), foreign["body"].encode(), hashlib.sha256
        ).hexdigest()
        for content in (b"", b"{", b'{"body":12}', json.dumps(foreign).encode()):
            (config.DATA_DIR / ".audit_head").write_bytes(content)
            with patch.object(audit._logger, "error") as warning:
                for posture in (True, False):
                    with patch.object(audit, "fail_open", return_value=posture):
                        audit.log(action="still.available")
                assert warning.call_count == 1, "one loud warning per incident/process"
            assert not audit.verify_chain()[0]
            audit.reset_anchor(actor="gate")
            assert audit.verify_chain()[0]
            assert "witness_replaced=1" in audit.recent(1)[0].detail
        audit.rotate_key()
        for filename in (".audit_key", ".audit_epochs"):
            path = config.DATA_DIR / filename
            saved = path.read_bytes()
            for missing in (True, False):
                if missing:
                    path.unlink()
                else:
                    path.write_bytes(b"{")
                audit.log(action="missing.material")
                assert not audit.verify_chain()[0]
                audit.run_chain_verification(record=True)
                assert audit.chain_monitor_view()["snapshot"]["state"] == "broken"
                path.write_bytes(saved)
                audit.reset_anchor(actor="gate")
                assert audit.verify_chain()[0]
        # Gate http_probe.py's requests, with the same corrupt-witness fault.
        from fastapi.testclient import TestClient

        from app.main import app

        (config.DATA_DIR / ".audit_head").write_bytes(b"{")
        client = TestClient(app, raise_server_exceptions=False)
        for url in ("/login", "/api/projects"):
            assert client.post(url, json={"name": "probe"}).status_code < 500
        audit.reset_anchor(actor="gate")
        for operation in ("read", "advance", "key", "epochs"):
            with patch.object(audit_state, operation, side_effect=PermissionError("probe")):
                audit.log(action="file.unreadable")
                assert not audit.verify_chain()[0]
                assert audit.chain_status_snapshot()["state"] == "broken"
            audit.reset_anchor(actor="gate")
            assert audit.verify_chain()[0]
    elif name == "initialize":
        (config.DATA_DIR / ".audit_head").unlink()
        with patch.object(audit._logger, "warning") as warning:
            assert audit.verify_chain()[0]
            assert warning.called, "initialization with an anchor must warn"
        assert "initialized" in audit.chain_status_snapshot()["note"]
    elif name == "rotation":
        from app.cli import backup_archive
        from app.cli._common import CliError
        from app.cli.cmd_db_db_init_schema import _audit_rotate_key

        emit = SimpleNamespace(result=lambda *a: None)
        with patch.object(backup_archive, "server_appears_running", return_value=(True, "host")):
            try:
                _audit_rotate_key(SimpleNamespace(remote=None, yes=True), emit)
            except CliError:
                pass
            else:
                raise AssertionError("rotation must refuse a listening server")
    elif name == "snapshot":
        for _ in range(1999):
            audit.log(action="timing")
        start = time.perf_counter()
        with (
            patch.object(audit_state, "key", wraps=audit_state.key) as keys,
            patch.object(audit_state, "epochs", wraps=audit_state.epochs) as epochs,
        ):
            assert audit.verify_chain()[0]
        elapsed = time.perf_counter() - start
        print(f"verify 2000 rows: {elapsed:.4f}s; reads {keys.call_count}/{epochs.call_count}")
        start = time.perf_counter()
        sync_time = []
        prepare_time = []
        replace_time = []
        real_sync, real_prepare = os.fsync, audit_state._prepare
        real_replace = os.replace

        def timed_sync(fd):
            before = time.perf_counter()
            real_sync(fd)
            sync_time.append(time.perf_counter() - before)

        def timed_prepare(content):
            before = time.perf_counter()
            result = real_prepare(content)
            prepare_time.append(time.perf_counter() - before)
            return result

        def timed_replace(src, dst):
            before = time.perf_counter()
            real_replace(src, dst)
            replace_time.append(time.perf_counter() - before)

        with (
            patch.object(os, "fsync", timed_sync),
            patch.object(audit_state, "_prepare", timed_prepare),
            patch.object(os, "replace", timed_replace),
        ):
            for _ in range(100):
                audit.log(action="timed.write")
        print(f"single write mean: {(time.perf_counter() - start) / 100:.6f}s")
        print(
            f"fsync mean: {sum(sync_time) / 100:.6f}s; prepare mean: {sum(prepare_time) / 100:.6f}s"
        )
        assert keys.call_count == epochs.call_count == 1, "one key/epoch snapshot per verify"
        print(f"replace mean: {sum(replace_time) / 100:.6f}s")
        original = audit._compute_hash

        def check_lock(*args, **kwargs):
            assert not audit._chain_lock._is_owned(), "walk must release chain lock"
            return original(*args, **kwargs)

        with patch.object(audit, "_compute_hash", check_lock):
            assert audit.verify_chain()[0]
        fsync = os.fsync

        def unlocked_sync(fd):
            assert not audit._chain_lock._is_owned(), "witness fsync must release chain lock"
            fsync(fd)

        with patch.object(os, "fsync", unlocked_sync):
            audit.log(action="unlocked.fsync")
        started, release = threading.Event(), threading.Event()
        errors = []

        def slow_sync(fd):
            if threading.current_thread().name == "old-witness":
                started.set()
                assert release.wait(10)
            fsync(fd)

        def old_write():
            try:
                audit.log(action="old.fsync")
            except Exception as exc:
                errors.append(exc)

        with patch.object(os, "fsync", slow_sync):
            writer = threading.Thread(target=old_write, name="old-witness")
            writer.start()
            assert started.wait(10)
            try:
                audit.log(action="new.fsync")
                newest = audit_state.read()["id"]
            finally:
                release.set()
                writer.join(10)
        assert not errors and not writer.is_alive()
        assert audit_state.read()["id"] == newest
        assert audit.verify_chain()[0]
    elif name == "backup":
        from app.cli import backup_archive as bk

        for rotated in (False, True):
            if rotated:
                audit.rotate_key()
                audit.log(action="after.rotation")
            archive = str(config.DATA_DIR / f"backup-{rotated}.tar.gz")
            manifest = bk.create_archive(archive, force=False)
            required = {".audit_key", ".audit_head", ".audit_format"}
            if rotated:
                required.add(".audit_epochs")
            assert required <= set(manifest["keyfiles_included"]), "backup must carry audit trust"
            assert bk.verify_archive(archive)[0]
            for target in ("same", "prerun", "clean"):
                audit.log(action="after.backup")
                db.engine.dispose()
                if target == "prerun":
                    (config.DATA_DIR / ".audit_key").write_bytes(os.urandom(32).hex().encode())
                    (config.DATA_DIR / ".audit_epochs").write_bytes(b"[]")
                elif target == "clean":
                    for filename in (".audit_key", ".audit_epochs", ".audit_head", ".audit_format"):
                        (config.DATA_DIR / filename).unlink(missing_ok=True)
                bk.restore_archive(archive)
                assert audit.verify_chain()[0], f"restore {target}, rotated {rotated}"
    print(name + " OK")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        scenario(sys.argv[1])
    else:
        failed = []
        evidence = []
        cases = ("unreadable", "backup", "shed", "snapshot", "initialize", "rotation", "posture")
        cases += ("publish", "trust_restart", "missing_signing", "reset_cli", "reset_corrupt")
        for case in os.environ.get("F21_CASES", " ".join(cases)).split():
            result = subprocess.run(
                [sys.executable, __file__, case], check=False, capture_output=True, text=True
            )
            print(case, result.returncode, result.stdout.strip(), result.stderr.splitlines()[-1:])
            evidence.append(f"{case}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
            if result.returncode:
                failed.append(case)
        if os.environ.get("F21_LOG_DIR"):
            Path(os.environ["F21_LOG_DIR"], "gate-scenarios.log").write_bytes(
                "\n".join(evidence).encode("utf-8")
            )
        probes = os.environ.get("F21_PROBES")
        if probes:
            # Reuse the gate's actual probes, always against new throwaway directories.
            for code in (str(ROOT), str(Path(probes) / "base")):
                with tempfile.TemporaryDirectory(prefix="loctms_gate_reuse_") as data:
                    for script, args in (
                        ("shed_probe.py", []),
                        ("branch_step.py", ["write", "1996"]),
                        ("branch_step.py", ["timeverify"]),
                        ("branch_step.py", ["write", "100"]),
                    ):
                        result = subprocess.run(
                            [sys.executable, str(Path(probes) / script), code, data, *args],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        evidence.append(code + " " + script + " " + result.stdout)
            Path(os.environ["F21_LOG_DIR"], "gate-probes.log").write_bytes(
                "\n".join(evidence).encode("utf-8")
            )
        regate = os.environ.get("F21_REGATE")
        if regate:
            outputs = []
            for script, expected in (
                ("reader_race.py", "status ok"),
                ("latch_reset.py", "'status ok'"),
            ):
                with tempfile.TemporaryDirectory(prefix="loctms_regate_reuse_") as data:
                    subprocess.run(
                        [
                            sys.executable,
                            str(Path(regate) / "step.py"),
                            str(ROOT),
                            data,
                            "seed",
                            "3",
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    result = subprocess.run(
                        [sys.executable, str(Path(regate) / script), str(ROOT), data],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    outputs.append(script + "\n" + result.stdout + result.stderr)
                    assert result.returncode == 0 and expected in result.stdout, outputs[-1]
            Path(os.environ["F21_LOG_DIR"], "round2-probes.log").write_bytes(
                "\n".join(outputs).encode("utf-8")
            )
        assert not failed, failed
        print("AUDIT GATE RECOVERY OK")
