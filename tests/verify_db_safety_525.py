"""DB file safety, batch remote-db (issue #525).

Temp dirs only: nothing here touches data/loc_tms.db or any real backup (each suite and
each child process gets its own throwaway LOC_TMS_DATA_DIR).

  1. same_file(): a case variant (NTFS), a hard link and a symlink all count as the SAME file.
  2. scripts/convert_db_encryption.py and `db encrypt|decrypt` refuse src == dst through such an
     alias and leave the source intact, even with --force (it used to delete the source).
  3. `db backup --full` refuses a destination that aliases the live DB.
  4. restore_archive refuses an archive whose backend differs from the live backend, before
     touching anything (dry run included).
  5. DATABASE_URL=sqlite:///elsewhere.db moves config.DB_PATH to that file.
  6. A SQLCipher archive verifies on a host whose live key differs, using the archived key.

Final line: "DB SAFETY 525 OK".
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_dbsafe525_")

from app import config
from app.cli import backup_archive as bk
from app.cli.__main__ import main as cli_main
from app.cli._common import same_file


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _read(path):
    return Path(path).read_bytes()


def _aliases(path):
    """Alias paths of `path` that name the same file on this host."""
    out = []
    d, name = os.path.split(path)
    swapped = os.path.join(d, name.swapcase())
    if os.path.normcase(swapped) == os.path.normcase(path) and swapped != path:
        out.append(("case variant", swapped))
    link = os.path.join(d, "hardlink-" + name)
    try:
        os.link(path, link)
        out.append(("hard link", link))
    except OSError:
        pass
    sym = os.path.join(d, "symlink-" + name)
    try:
        os.symlink(path, sym)
        out.append(("symlink", sym))
    except (OSError, NotImplementedError):
        pass
    return out


def test_same_file():
    work = tempfile.mkdtemp(prefix="loctms_same525_")
    a = os.path.join(work, "live.db")
    with open(a, "wb") as fh:
        fh.write(b"x" * 200)
    other = os.path.join(work, "other.db")
    with open(other, "wb") as fh:
        fh.write(b"y")
    if same_file(a, other):
        _fail("two different files compared equal")
    if same_file(a, os.path.join(work, "missing.db")):
        _fail("a missing destination compared equal")
    if not same_file(a, a):
        _fail("a path is not the same file as itself")
    aliases = _aliases(a)
    if not aliases:
        _fail("no alias could be built to test with")
    for label, alias in aliases:
        if not same_file(a, alias):
            _fail(f"{label} was not recognised as the same file")
    shutil.rmtree(work, ignore_errors=True)


def test_convert_never_deletes_source():
    from scripts import convert_db_encryption as conv

    for direction in ("encrypt", "decrypt"):
        work = tempfile.mkdtemp(prefix="loctms_conv525_")
        src = os.path.join(work, "loc_tms.db")
        payload = b"SQLite format 3\x00" + b"z" * 300
        with open(src, "wb") as fh:
            fh.write(payload)
        for label, alias in _aliases(src):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = conv.main([direction, src, alias, "--force"])
            if rc != 2:
                _fail(f"convert {direction} via {label}: rc={rc} out={out.getvalue()!r}")
            if not os.path.isfile(src) or _read(src) != payload:
                _fail(f"convert {direction} via {label} destroyed or changed the source")
            # The CLI twin (`db encrypt|decrypt`) shares the guard.
            o, e = io.StringIO(), io.StringIO()
            with redirect_stdout(o), redirect_stderr(e):
                rc = cli_main(["db", direction, src, alias, "--force"])
            if rc == 0:
                _fail(f"db {direction} via {label} reported success")
            if not os.path.isfile(src) or _read(src) != payload:
                _fail(f"db {direction} via {label} destroyed or changed the source")
        shutil.rmtree(work, ignore_errors=True)


def test_backup_destination_aliasing_live_db():
    from app.db import init_db

    init_db()
    live = str(config.DB_PATH)
    for label, alias in _aliases(live):
        try:
            bk.create_archive(alias, force=True)
        except bk.BackupError:
            pass
        else:
            _fail(f"create_archive accepted {label} of the live DB as its destination")
        if not _read(live).startswith(b"SQLite format 3"):
            _fail(f"the live DB was overwritten through {label}")


def test_restore_refuses_other_backend():
    from app.cli import backup_restore as br

    arch = os.path.join(_TMP, "good.tar.gz")
    bk.create_archive(arch, force=True)
    before = _read(config.DB_PATH)
    real = config.IS_SQLITE
    config.IS_SQLITE = False  # the live install is now "Postgres"
    try:
        for dry in (True, False):
            try:
                br.restore_archive(arch, dry_run=dry)
            except bk.BackupError as exc:
                if "sqlite" not in str(exc) or "postgres" not in str(exc):
                    _fail(f"refusal does not name both backends: {exc}")
            else:
                _fail(f"a sqlite archive restored onto a postgres install (dry_run={dry})")
    finally:
        config.IS_SQLITE = real
    if _read(config.DB_PATH) != before:
        _fail("the refused restore changed the live DB file")


def test_restore_refusal_both_directions_and_cli():
    """The cross-backend refusal itself (#525): both directions, a clear message, a clean
    CLI exit 1, and nothing touched."""
    import hashlib
    import json
    import tarfile

    from app.cli import backup_restore as br

    pg = os.path.join(_TMP, "pg-fixture.tar.gz")
    data = b"PGDMP" + bytes(128)
    manifest = {
        "manifest_schema": 1,
        "backend": "postgres",
        "db_member": bk.DB_ARC_PG,
        "pg_dump_version": "pg_dump (PostgreSQL) 16.4",
        "dump_size": len(data),
        "members": [
            {
                "path": bk.DB_ARC_PG,
                "kind": "db",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    }
    with tarfile.open(pg, "w:gz") as tar:
        for name, payload in (
            (bk.MANIFEST_NAME, json.dumps(manifest).encode()),
            (bk.DB_ARC_PG, data),
        ):
            ti = tarfile.TarInfo(name)
            ti.size = len(payload)
            tar.addfile(ti, io.BytesIO(payload))
    before = _read(config.DB_PATH)
    # Postgres archive onto the live SQLite install.
    from unittest.mock import patch

    for dry in (True, False):
        try:
            with patch.object(br, "verify_archive", return_value=(True, [])):
                br.restore_archive(pg, dry_run=dry)
        except bk.BackupError as exc:
            msg = str(exc)
            if (
                "holds a postgres database" not in msg
                or "runs sqlite" not in msg
                or "nothing was changed" not in msg
            ):
                _fail(f"unclear refusal for a postgres archive on sqlite: {msg}")
        else:
            _fail(f"a postgres archive restored onto a sqlite install (dry_run={dry})")
    # CLI: sqlite archive onto a "postgres" install is a clean exit 1 with the message.
    arch = os.path.join(_TMP, "good-cli.tar.gz")
    bk.create_archive(arch, force=True)
    real = config.IS_SQLITE
    config.IS_SQLITE = False
    try:
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cli_main(["db", "restore", arch, "--yes"])
    finally:
        config.IS_SQLITE = real
    if rc != 1 or "refusing to restore" not in (o.getvalue() + e.getvalue()):
        _fail(
            f"db restore did not refuse cleanly: rc={rc} out={o.getvalue()!r} err={e.getvalue()!r}"
        )
    if _read(config.DB_PATH) != before:
        _fail("a refused restore changed the live DB file")


def test_same_file_junction_and_unc():
    work = tempfile.mkdtemp(prefix="loctms_junc525_")
    real_dir = os.path.join(work, "real")
    os.mkdir(real_dir)
    live = os.path.join(real_dir, "loc_tms.db")
    with open(live, "wb") as fh:
        fh.write(b"x" * 50)
    if os.name == "nt":
        junc = os.path.join(work, "junc")
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", junc, real_dir], capture_output=True, text=True
        )
        if r.returncode == 0:
            if not same_file(live, os.path.join(junc, "loc_tms.db")):
                _fail("a junction alias of an existing file was not recognised")
            if not same_file(live, os.path.join(junc, "LOC_TMS.DB")):
                _fail("a junction + case alias was not recognised")
            # destination that does not exist yet, reached through the junction, differs
            if same_file(live, os.path.join(junc, "other.db")):
                _fail("a different file through a junction compared equal")
        drive, rest = os.path.splitdrive(os.path.abspath(live))
        unc = "\\\\localhost\\" + drive[0] + "$" + rest
        if os.path.exists(unc) and not same_file(live, unc):
            _fail("a UNC admin-share alias of the same file was not recognised")
    shutil.rmtree(work, ignore_errors=True)


def _child(code, extra_env):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("LOC_TMS_ENCRYPTION_KEY", "ENCRYPTION_KEY", "LOC_TMS_DATA_DIR")
    }
    env.update(extra_env)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


_CHILD_PRE = (
    "import os, sys\n"
    f"sys.path.insert(0, {_ROOT!r})\n"
    "from tests._bootstrap import init_env\n"
    "init_env('loctms_dbsafe525c_')\n"
)


def test_database_url_moves_db_path():
    elsewhere = os.path.join(tempfile.mkdtemp(prefix="loctms_elsewhere525_"), "elsewhere.db")
    code = _CHILD_PRE + "from app import config\nprint(config.DB_PATH)\n"
    rc, out, err = _child(code, {"DATABASE_URL": "sqlite:///" + elsewhere})
    if rc != 0 or os.path.normcase(out.splitlines()[-1]) != os.path.normcase(elsewhere):
        _fail(f"DB_PATH did not follow DATABASE_URL: rc={rc} out={out!r} err={err[-300:]!r}")


def test_sqlcipher_archive_verifies_with_archived_key():
    try:
        import sqlcipher3  # noqa: F401
    except ImportError:
        print("  (skipped the SQLCipher check: no sqlcipher3 binding)")
        return
    arch = os.path.join(tempfile.mkdtemp(prefix="loctms_cipher525_"), "enc.tar.gz")
    make = (
        _CHILD_PRE
        + "os.environ['LOC_TMS_DB_ENCRYPTION'] = 'sqlcipher'\n"
        + "from app.db import init_db\n"
        + "init_db()\n"
        + "from app.cli import backup_archive as bk\n"
        + f"m = bk.create_archive({arch!r}, force=True)\n"
        + "print('MADE', m.get('db_encryption'), m.get('keyfiles_included'))\n"
    )
    rc, out, err = _child(make, {})
    if rc != 0 or "MADE sqlcipher" not in out:
        _fail(f"could not build the SQLCipher archive: rc={rc} out={out!r} err={err[-400:]!r}")
    if ".enc_key" not in out:
        _fail(f"the archive carries no .enc_key to verify with: {out!r}")
    # A DIFFERENT host: fresh data dir, so a fresh live ENCRYPTION_KEY.
    check = (
        _CHILD_PRE
        + "os.environ['LOC_TMS_DB_ENCRYPTION'] = 'sqlcipher'\n"
        + "from app.cli import backup_common as bc\n"
        + f"ok, checks = bc.verify_archive({arch!r})\n"
        + "bad = [c for c in checks if c['status'] == 'fail']\n"
        + "print('VERIFIED' if ok else 'NOT-VERIFIED', bad)\n"
    )
    rc, out, err = _child(check, {})
    if rc != 0 or "VERIFIED" not in out.splitlines()[-1] or "NOT-VERIFIED" in out:
        _fail(f"archive does not verify on a host with another key: {out!r} {err[-300:]!r}")


if __name__ == "__main__":
    test_same_file()
    test_convert_never_deletes_source()
    test_backup_destination_aliasing_live_db()
    test_restore_refuses_other_backend()
    test_restore_refusal_both_directions_and_cli()
    test_same_file_junction_and_unc()
    test_database_url_moves_db_path()
    test_sqlcipher_archive_verifies_with_archived_key()
    print("DB SAFETY 525 OK")
