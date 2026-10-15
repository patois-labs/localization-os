"""Issue #544 item 2: plain `db backup` output is private (0600), offline.

A plain backup (SQLite copy or pg_dump file) holds password and key hashes, encrypted
connector tokens and all tenant content; the `--full` archive was fixed in #397 but the
plain one was written with default permissions. The backup now runs under a 0077 umask
and is chmod 0600 afterwards. Windows has no POSIX modes, so the checks observe the
umask in force while the file is written and the chmod call (and the real mode on POSIX).

Items 1 and 3 of #544 are done by another queue.
"""

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bk544_")

from app.db import init_db
from tests._cli_runner import run_cli

init_db()


_U = {"cur": 0o022}  # a simulated process umask: Windows keeps only the write bit


def _spy_umask(mask):
    prev = _U["cur"]
    _U["cur"] = mask
    return prev


os.umask = _spy_umask


def _tmp_dst():
    d = tempfile.mkdtemp(prefix="loctms_bk544_")
    return d, os.path.join(d, "plain.db")


def test_sqlite_backup_private():
    import sqlite3

    _d, dst = _tmp_dst()
    chmods = []
    real_chmod = os.chmod
    umasks = []
    real_connect = sqlite3.connect

    def spy_connect(path, *a, **kw):
        if str(path) == dst:
            umasks.append(_U["cur"])
        return real_connect(path, *a, **kw)

    def spy_chmod(path, mode, *a, **kw):
        chmods.append((str(path), mode))
        return real_chmod(path, mode, *a, **kw)

    sqlite3.connect = spy_connect
    os.chmod = spy_chmod
    try:
        code, out, err = run_cli(["db", "backup", dst])
    finally:
        sqlite3.connect = real_connect
        os.chmod = real_chmod
    assert code == 0, (code, out, err)
    assert umasks == [0o077], f"backup file created under umask {umasks}"
    assert (dst, 0o600) in chmods, chmods
    if os.name == "posix":
        assert os.stat(dst).st_mode & 0o077 == 0, oct(os.stat(dst).st_mode)
    print("  sqlite plain backup: private umask + 0600 OK")


def test_force_overwrite_narrows_existing_file_before_writing():
    import sqlite3

    _d, dst = _tmp_dst()
    open(dst, "wb").close()  # an empty file is a valid (empty) SQLite database
    events = []
    real_chmod = os.chmod
    real_connect = sqlite3.connect

    def spy_connect(path, *a, **kw):
        if str(path) == dst:
            events.append("write")
        return real_connect(path, *a, **kw)

    def spy_chmod(path, mode, *a, **kw):
        if str(path) == dst:
            events.append(("chmod", mode))
        return real_chmod(path, mode, *a, **kw)

    sqlite3.connect = spy_connect
    os.chmod = spy_chmod
    try:
        code, out, err = run_cli(["db", "backup", dst, "--force"])
    finally:
        sqlite3.connect = real_connect
        os.chmod = real_chmod
    assert code == 0, (code, out, err)
    # a kept file has its OLD (possibly 0644) mode: it is narrowed before the data lands
    assert events[0] == ("chmod", 0o600) and "write" in events, events
    print("  --force: existing file narrowed to 0600 before the new data is written OK")


def test_postgres_backup_private():
    from app.cli import backup_archive as bk
    from app.cli import cmd_db_db_init_schema as cmd

    _d, dst = _tmp_dst()
    seen = []

    def fake_dump(path):
        seen.append(_U["cur"])
        with open(path, "wb") as f:
            f.write(b"PGDMP")
        return None

    chmods = []
    real_chmod = os.chmod

    def spy_chmod(path, mode, *a, **kw):
        chmods.append((str(path), mode))
        return real_chmod(path, mode, *a, **kw)

    old = (cmd._is_sqlite, bk._dump_postgres)
    cmd._is_sqlite = lambda: False
    bk._dump_postgres = fake_dump
    os.chmod = spy_chmod
    try:
        code, out, err = run_cli(["db", "backup", dst])
    finally:
        cmd._is_sqlite, bk._dump_postgres = old
        os.chmod = real_chmod
    assert code == 0, (code, out, err)
    assert seen == [0o077], f"pg dump written under umask {seen}"
    assert (dst, 0o600) in chmods, chmods
    print("  postgres plain backup: private umask + 0600 OK")


def test_umask_restored_after_failure():
    from app.cli import backup_archive as bk
    from app.cli import cmd_db_db_init_schema as cmd

    _d, dst = _tmp_dst()
    before = _U["cur"]

    def boom(path):
        raise bk.BackupError("pg_dump failed")

    old = (cmd._is_sqlite, bk._dump_postgres)
    cmd._is_sqlite = lambda: False
    bk._dump_postgres = boom
    try:
        code, _, _ = run_cli(["db", "backup", dst])
    finally:
        cmd._is_sqlite, bk._dump_postgres = old
    after = _U["cur"]
    assert code == 1 and after == before, (code, oct(before), oct(after))
    print("  umask restored after a failed dump OK")


if __name__ == "__main__":
    test_sqlite_backup_private()
    test_force_overwrite_narrows_existing_file_before_writing()
    test_postgres_backup_private()
    test_umask_restored_after_failure()
    print("BK544 OK")
