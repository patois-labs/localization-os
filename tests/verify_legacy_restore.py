"""Offline #398 regressions through the actual legacy restore CLI."""

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._bootstrap import init_env

init_env("loctms_legacy_restore_")

from sqlalchemy import create_engine

from app import config, db
from tests._cli_runner import run_cli


def interrupted_restore(interrupt, during_rollback=False):
    from app.cli._common import CliError
    from app.cli.legacy_restore import restore_database

    root = config.DATA_DIR / f"interrupt-{interrupt.__name__}-{during_rollback}"
    root.mkdir()
    live, backup = root / "live.db", root / "backup.db"
    conn = sqlite3.connect(backup)
    conn.execute("CREATE TABLE probe(value TEXT)")
    conn.close()
    originals = {suffix: ("original" + suffix).encode() for suffix in ("", "-wal", "-shm")}
    for suffix, data in originals.items():
        Path(str(live) + suffix).write_bytes(data)
    replace = os.replace

    def fail(src, dst):
        if str(src) == str(live) + "-shm":
            raise interrupt("interrupted move")
        if during_rollback and str(src).endswith("original.db-wal"):
            raise interrupt("interrupted rollback")
        return replace(src, dst)

    with patch("os.replace", side_effect=fail):
        try:
            restore_database(str(backup), str(live))
        except (KeyboardInterrupt, SystemExit, CliError):
            pass
        else:
            raise AssertionError("interruption was swallowed")
    recovery = list(root.glob(".legacy-restore-*"))
    if during_rollback:
        assert len(recovery) == 1, "interrupted rollback lost recovery directory"
        assert (recovery[0] / "original.db-wal").read_bytes() == originals["-wal"]
    else:
        for suffix, data in originals.items():
            assert Path(str(live) + suffix).read_bytes() == data, "interrupt lost original"
        assert not recovery


def value(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM recovery_probe").fetchone()[0]
    finally:
        conn.close()


def case(kind):
    root = config.DATA_DIR / kind
    root.mkdir()
    live, backup = root / "live.db", root / "backup.db"
    conn = sqlite3.connect(live)
    conn.execute("CREATE TABLE recovery_probe(value TEXT)")
    conn.execute("INSERT INTO recovery_probe VALUES ('backup-state')")
    conn.commit()
    out = sqlite3.connect(backup)
    conn.backup(out)
    out.close()
    conn.close()
    engine = create_engine(f"sqlite:///{live.as_posix()}")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA wal_autocheckpoint=0")
        conn.exec_driver_sql("UPDATE recovery_probe SET value='current-state'")
    original_replace = os.replace
    original_copy = __import__("shutil").copyfile
    original_remove = os.remove
    original_dispose = engine.dispose
    disposed = []
    wal_state = {suffix: Path(str(live) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}

    def server_probe():
        if kind != "pooled":
            original_dispose()
            for suffix, data in wal_state.items():
                Path(str(live) + suffix).write_bytes(data)
        return False, "offline"

    def dispose():
        disposed.append(True)
        original_dispose()

    def replace(src, dst):
        if kind == "sidecar" and str(src) == str(live) + "-shm":
            raise PermissionError("injected locked sidecar")
        if kind in ("install", "rollback") and str(dst) == str(live) and "staged.db" in str(src):
            raise OSError("injected install failure")
        if kind == "rollback" and str(src).endswith("original.db"):
            raise PermissionError("injected rollback failure")
        result = original_replace(src, dst)
        if kind == "verification" and str(dst) == str(live) and "staged.db" in str(src):
            c = sqlite3.connect(live)
            c.execute("UPDATE recovery_probe SET value='wrong-state'")
            c.commit()
            c.close()
        if kind == "final_corrupt" and str(dst) == str(live) and "staged.db" in str(src):
            Path(dst).write_bytes(b"SQLite format 3\0" + b"broken" * 1000)
        return result

    def remove(path, *args, **kwargs):
        if kind == "sidecar" and str(path) == str(live) + "-shm":
            raise PermissionError("injected locked sidecar")
        return original_remove(path, *args, **kwargs)

    def copy(src, dst, *args, **kwargs):
        if kind == "copy":
            Path(dst).write_bytes(b"partial copy")
            raise OSError("injected copy failure")
        return original_copy(src, dst, *args, **kwargs)

    if kind == "corrupt":
        backup.write_bytes(b"SQLite format 3\0" + b"broken" * 1000)
    before = backup.read_bytes()
    try:
        with (
            patch.object(config, "DB_PATH", live),
            patch.object(db, "engine", engine),
            patch.object(engine, "dispose", side_effect=dispose),
            patch("app.cli.backup_archive.server_appears_running", side_effect=server_probe),
            patch("os.replace", side_effect=replace),
            patch("os.remove", side_effect=remove),
            patch("shutil.copyfile", side_effect=copy),
        ):
            code, stdout, stderr = run_cli(
                ["--json", "db", "restore", str(backup), "--yes", "--skip-verify"]
            )
        payload = json.loads(stdout.strip().splitlines()[-1])
        if kind == "pooled":
            assert code == 0 and payload["ok"], (code, payload, stderr)
            assert disposed, "CLI pooled handles were not disposed"
            assert value(live) == "backup-state", "stale WAL won over backup"
        else:
            assert code != 0 and not payload["ok"], (kind, code, payload)
            if kind == "corrupt":
                assert str(backup) in payload["error"], payload
            if kind == "rollback":
                recovery = list(root.glob(".legacy-restore-*"))
                assert len(recovery) == 1, "rollback failure lost recovery files"
                assert str(recovery[0]) in payload["error"]
                assert "rollback incomplete" in payload["error"]
                assert value(recovery[0] / "original.db") == "current-state"
                return
            assert value(live) == "current-state", f"{kind}: rollback lost current data"
        assert backup.read_bytes() == before, "source backup changed"
        assert not list(root.glob(".legacy-restore-*")), "restore staging leaked"
    finally:
        original_dispose()


if __name__ == "__main__":
    failures = []
    for interrupt in (KeyboardInterrupt, SystemExit):
        for during_rollback in (False, True):
            try:
                interrupted_restore(interrupt, during_rollback)
            except Exception as exc:
                failures.append(f"{interrupt.__name__} rollback={during_rollback}: {exc}")
    for name in (
        "pooled",
        "corrupt",
        "sidecar",
        "copy",
        "install",
        "verification",
        "final_corrupt",
        "rollback",
    ):
        try:
            case(name)
            print(f"  {name} OK")
        except Exception as exc:
            failures.append(f"{name}: {exc}")
    assert not failures, "\n".join(failures)
    print("verify_legacy_restore OK")
