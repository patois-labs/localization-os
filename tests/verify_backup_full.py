"""`db backup --full` / `db verify-backup` / `db restore <archive>` verification
(Batch-10 Train 3, gap G-06: whole-install backup + DR covering the DB, the uploads
tree, AND the Fernet encryption keyfiles - the material a machine loss would make
unrecoverable).

Hermetic + offline: a throwaway LOC_TMS_DATA_DIR (tests._bootstrap) holding a real
init_db() SQLite DB, the two generated keyfiles (.enc_key/.session_secret), and a
synthetic uploads/ tree. Every case drives the actual CLI (run_cli) for web+CLI-parity
truth, not the helper module in isolation. Covers:

  1. CREATE: `db backup --full <archive>` writes a .tar.gz whose members are exactly
     {manifest.json, db/loc_tms.db, uploads/*, keyfiles/*}; the embedded manifest's
     per-member sha256s match a re-hash of the extracted bytes; --json carries the
     structured summary; the loud secrets-in-archive warning fires on stderr.
  2. VERIFY (pass): `db verify-backup <archive>` exits 0 with every check ok/skip;
     --json parity.
  3. VERIFY (fail): a single corrupted member (one upload's bytes flipped) -> exit 1,
     the member's sha256 check FAILs and names it; a truncated/garbage DB member ->
     the integrity_check FAILs.
  4. RESTORE: onto an EMPTIED target (DB gone, uploads wiped, keyfiles clobbered),
     `db restore <archive> --yes` reproduces the DB (integrity ok), the full uploads
     tree byte-for-byte, and the keyfiles byte-for-byte; the server-running guard is
     exercised (a patched "running" probe refuses the restore, exit 1).
  5. LEGACY UNBROKEN: plain `db backup <file>` (no --full) still writes a single valid
     SQLite file, and `db restore <file> --yes` still restores it (auto-detect never
     misroutes a non-archive to the full path).

Adversarial-gate regression pins (each was a probe-proven defect):
  6.  verify FAILS on an UNLISTED (injected) member.
  7.  restore VERIFIES the archive BEFORE any write and refuses a corrupt one, leaving
      the live DB + uploads untouched.
  8.  restore is ATOMIC: an induced late-swap failure rolls the DB + keyfiles back to
      their pre-restore state and leaves no .pre-restore / staging turds.
  9.  a symlink/link member is refused by verify (unlisted) AND by _safe_extractall.
  10. the extraction total-bytes cap trips (decompression-bomb guard).
  11. legacy single-file restore refuses a FOREIGN tar / non-DB file (SQLite-header
      check) rather than overwriting the live DB with garbage.
  12. the archive file is created 0600 on POSIX (it can hold live key material).
  13. SQLCipher snapshot uses the keyed online backup API - a competing WAL reader
      causes NO data loss, the snapshot stays encrypted, and verify runs a KEYED
      integrity_check (subprocess; skipped when sqlcipher3 is absent).
  14. rollback with ABSENT target keyfiles (a fresh machine) + an induced late failure
      leaves NOTHING installed - the archive's key material is not stranded on disk.
  15. a malformed manifest (bytes=null) on the trusted --skip-verify path never leaks a
      raw TypeError - the only-BackupError contract holds.
  16. the start-of-restore leftover sweep KEEPS a LIVE concurrent restore's staging dir
      (pid still alive) while removing a DEAD-pid one (RES-1).
  17. existing recovery copies block replacement and survive the leftover sweep,
      including on a KEYFILE-LESS target (#394).
  18. the extraction refusal names FREE DISK SPACE (not the bomb wording) when the
      free-space ceiling is the binding cap (RES-3).
  19. the archive is written to a private (0600, O_CREAT|O_EXCL) temp sibling and a
      newly created backup dir is mkdir'd 0700, so the secret-bearing bytes are
      private from the first byte (GitHub #397).

Run standalone: python tests/verify_backup_full.py  ->  prints "... OK".
"""

import io
import os
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_backupfull_")

import hashlib
import json

from app import config
from app.cli import backup_archive as bk
from app.cli import backup_restore
from app.db import init_db
from tests._cli_runner import run_cli


def run(argv):
    return run_cli(argv)


def run_json(argv):
    code, out, err = run(["--json"] + argv)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def _seed():
    """Build the synthetic DATA_DIR: a real DB + keyfiles (already generated at
    config import) + a small uploads tree with nested files."""
    init_db()
    from app.services import audit, audit_state

    audit_state.key()
    epochs = config.DATA_DIR / ".audit_epochs"
    if not epochs.exists():
        epochs.write_bytes(b"[]")
    audit.log(action="backup.fixture", force=True)
    up = config.UPLOADS_DIR
    (up / "a").mkdir(parents=True, exist_ok=True)
    (up / "b" / "c").mkdir(parents=True, exist_ok=True)
    (up / "root.txt").write_bytes(b"root upload\n")
    (up / "a" / "doc1.bin").write_bytes(bytes(range(256)) * 4)
    (up / "b" / "c" / "deep.txt").write_text("nested upload payload", encoding="utf-8")
    # a scratch file that MUST be excluded from the archive
    (up / "a" / "half.tmp").write_bytes(b"transient")
    # keyfiles were generated on config import; assert they exist + non-empty
    for name in bk.KEYFILE_NAMES:
        p = config.DATA_DIR / name
        assert p.is_file() and p.stat().st_size > 0, f"missing keyfile {name}"


def _legacy_audit_restore():
    """Pre-train archive format: original derivation and original keyfile allowlist."""
    import subprocess
    from unittest.mock import patch

    from app import db
    from app.services import audit

    # Reuse the initial unrotated fixture, whose hashes use the base-code derivation.
    archive = str(config.DATA_DIR / "legacy-audit.tar.gz")
    legacy_names = (".enc_key", ".session_secret")
    with patch.object(bk, "KEYFILE_NAMES", legacy_names):
        manifest = bk.create_archive(archive, force=False)
    assert not any(n.startswith(".audit_") for n in manifest["keyfiles_included"])
    original = {n: (config.DATA_DIR / n).read_bytes() for n in bk.KEYFILE_NAMES}
    audit.rotate_key()
    (config.DATA_DIR / ".session_secret").write_bytes(os.urandom(32).hex().encode())
    (config.DATA_DIR / ".audit_key").write_bytes(os.urandom(32).hex().encode())
    before = {n: (config.DATA_DIR / n).read_bytes() for n in bk.KEYFILE_NAMES}
    with patch.object(backup_restore, "_swap_dir", side_effect=OSError("late failure")):
        try:
            bk.restore_archive(archive)
        except bk.BackupError:
            pass
        else:
            raise AssertionError("injected restore failure must roll back")
    assert all((config.DATA_DIR / n).read_bytes() == data for n, data in before.items())
    with patch.object(bk, "server_appears_running", return_value=(False, "test")):
        code, out, err = run(["db", "restore", archive, "--yes"])
    assert code == 0, out + err
    assert "audit key will be re-derived from the restored session secret" in out
    assert all(
        not (config.DATA_DIR / n).exists()
        for n in (".audit_key", ".audit_epochs", ".audit_head", ".audit_format")
    ), "legacy clears target trust"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.services import audit; "
            "assert audit.verify_chain()[0]; "
            "assert audit.run_chain_verification(record=True)[0]; print('legacy green')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    db.engine.dispose()
    # Keep the surrounding backup suite's original complete keyfile fixture.
    for n, data in original.items():
        (config.DATA_DIR / n).write_bytes(data)
    print("  legacy full archive restores green on a previously rotated host OK")


def _snapshot_disk_state():
    """Capture sha256 of every non-scratch upload + each keyfile, for post-restore
    byte-equality checks."""
    state = {}
    for root, _dirs, files in os.walk(config.UPLOADS_DIR):
        for f in files:
            if f.endswith((".tmp", ".part")):
                continue
            ap = os.path.join(root, f)
            rel = os.path.relpath(ap, config.UPLOADS_DIR).replace(os.sep, "/")
            state["uploads/" + rel] = _sha(ap)
    for name in bk.KEYFILE_NAMES:
        p = config.DATA_DIR / name
        if p.is_file():
            state["keyfiles/" + name] = _sha(p)
    return state


def test_recovery_credentials():
    import shlex
    from unittest.mock import patch

    url = "postgresql+psycopg://audit_user:AUDIT_FAKE_PASSWORD@localhost:5432/audit_db"
    with patch.object(config, "DB_URL", url), patch.object(config, "IS_SQLITE", False):
        hint = bk._pg_restore_hint(config, "dump.file")
        assert "AUDIT_FAKE_PASSWORD" not in hint, hint
        assert "PGPASSWORD" in hint and "~/.pgpass" in hint
        argv = shlex.split(hint.splitlines()[-1])
        assert argv == [
            "pg_restore",
            "--clean",
            "--if-exists",
            "-h",
            "localhost",
            "-p",
            "5432",
            "-U",
            "audit_user",
            "-d",
            "audit_db",
            "dump.file",
        ], argv
        with patch.object(bk.shutil, "which", return_value=None):
            for flags in ([], ["--json"]):
                for command in (
                    ["db", "backup", str(Path(_TMP) / "missing.dump")],
                    ["db", "backup", "--full", str(Path(_TMP) / "missing.tar.gz")],
                ):
                    code, out, err = run(flags + command)
                    assert code != 0
                    assert "AUDIT_FAKE_PASSWORD" not in out + err, (out, err)
    from app.db_url import redact_db_url

    # MED-1 (round 2): redact_db_url stays greedy WITHIN the authority -- it excludes only
    # whitespace and '/' (the characters that end a URL authority), NOT '@'. A password with
    # a raw '@' (which SQLAlchemy splits on the LAST '@', a working config) is FULLY redacted,
    # not left in the clear after the first '@'. The twelve-case matrix the round-2 record
    # names, with LITERAL expected outputs:
    matrix = {
        # the round-2 leak: a raw '@' inside the password -> the whole authority is redacted
        "postgresql://u:pa@ss@h/d": "postgresql://***@h/d",
        url: "postgresql+psycopg://***@localhost:5432/audit_db",
        # percent-encoded credentials still redacted (%2F, %40)
        "postgresql+psycopg://audit_user:AUDIT_FAKE_PASSWORD%2Fw%20s@localhost:5432/audit_db": (
            "postgresql+psycopg://***@localhost:5432/audit_db"
        ),
        "postgresql://u:p%40w@h/d": "postgresql://***@h/d",
        # user-only, password-only, and a non-postgres scheme still redacted
        "postgresql://user@h/d": "postgresql://***@h/d",
        "redis://:secretpass@h:6379/0": "redis://***@h:6379/0",
        # composed text: only the URL token is touched; a later email '@' survives untouched
        "postgresql://u:p@h/d and also see admin@example.com": (
            "postgresql://***@h/d and also see admin@example.com"
        ),
        # no userinfo / no scheme:// -> returned verbatim
        "postgresql://h:5432/d": "postgresql://h:5432/d",
        "postgresql:///mydb": "postgresql:///mydb",
        "sqlite:///plain.db": "sqlite:///plain.db",
        "admin@example.com": "admin@example.com",
        r"C:\Users\admin@corp\db.sqlite": r"C:\Users\admin@corp\db.sqlite",
    }
    assert len(matrix) == 12, len(matrix)
    for raw, expected in matrix.items():
        assert redact_db_url(raw) == expected, (raw, redact_db_url(raw))
    assert "AUDIT_FAKE_PASSWORD" not in redact_db_url(url)
    # LOW-1: a URL-LEGAL (percent-encoded) password is redacted; the printed restore hint
    # NEVER carries the password regardless of its form (it goes to PGPASSWORD, never the
    # rendered argv), including a NON-URL-legal raw '/'+space password (the round-1 pin).
    legal = "postgresql+psycopg://audit_user:AUDIT_FAKE_PASSWORD%2Fw%20s@localhost:5432/audit_db"
    assert "AUDIT_FAKE_PASSWORD" not in redact_db_url(legal), redact_db_url(legal)
    for password in ("AUDIT_FAKE_PASSWORD/with space", "AUDIT_FAKE_PASSWORD%2Fwith%20space"):
        odd = "postgresql+psycopg://audit_user:" + password + "@localhost:5432/audit_db"
        with patch.object(config, "DB_URL", odd):
            assert "AUDIT_FAKE_PASSWORD" not in bk._pg_restore_hint(config, "dump.file")


def test_restore_output_and_dump_metadata():
    import logging
    from types import SimpleNamespace
    from unittest.mock import patch

    # pregate: allow-secret synthetic recovery credential for output redaction checks
    url = "postgresql+psycopg://audit_user:AUDIT_FAKE_PASSWORD@localhost:5432/audit_db"
    path = _postgres_fixture(b"PGDMP" + bytes(128))
    logs = io.StringIO()
    handler = logging.StreamHandler(logs)
    logging.getLogger().addHandler(handler)
    try:
        with patch.object(config, "DB_URL", url), patch.object(config, "IS_SQLITE", False):
            dry = bk.restore_archive(path, dry_run=True)
            assert "AUDIT_FAKE_PASSWORD" not in json.dumps(dry)
            with (
                patch.object(backup_restore, "verify_archive", return_value=(True, [])),
                patch.object(bk, "server_appears_running", return_value=(False, "test")),
            ):
                for flags in ([], ["--json"]):
                    code, out, err = run([*flags, "db", "restore", path, "--yes"])
                    assert code == 1, (out, err)
                    assert "pg_restore" in out and "AUDIT_FAKE_PASSWORD" not in out + err
                    if flags:
                        payload = json.loads(out)
                        assert payload["status"] == "partial" and payload["ok"] is False
                    else:
                        assert out.startswith("PARTIAL restore from "), out

            def dump_tool(argv, **kwargs):
                assert url not in argv and "AUDIT_FAKE_PASSWORD" not in str(argv)
                if argv == ["pg_dump", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="pg_dump (PostgreSQL) 16.4\n")
                assert kwargs["env"]["PGPASSWORD"] == "AUDIT_FAKE_PASSWORD"
                kwargs["stdout"].write(b"PGDMP" + bytes(128))
                return SimpleNamespace(returncode=0)

            with (
                patch.object(bk.shutil, "which", return_value="pg_dump"),
                patch("subprocess.run", side_effect=dump_tool),
            ):
                manifest = bk.create_archive(str(Path(_TMP) / "pg-created.tar.gz"), force=True)
                assert manifest["dump_size"] == 133
                assert manifest["pg_dump_version"] == "pg_dump (PostgreSQL) 16.4"
            with patch.object(bk.shutil, "which", return_value=None):
                try:
                    bk._dump_postgres(str(Path(_TMP) / "missing.dump"))
                except bk.BackupError as exc:
                    logging.getLogger("recovery-test").error("%s", exc)
    finally:
        logging.getLogger().removeHandler(handler)
    assert "AUDIT_FAKE_PASSWORD" not in logs.getvalue(), logs.getvalue()


def test_postgres_backup_preserves_last_good_dump():
    """GitHub #544 (1): `db backup PATH --force` on a Postgres backend must NOT truncate
    the previous dump when pg_dump fails. The dump is staged to a private temp sibling in
    the destination dir and renamed into place ONLY on success (os.replace); a failed dump
    unlinks the temp and leaves the existing file's bytes untouched. The external pg_dump
    is stubbed at the process boundary (the suite already fakes both backends this way);
    the atomic temp+rename guard under test runs for real, so this is not a weakened check.
    Before the fix, dst was opened "wb" before pg_dump ran -> truncated to 0 bytes."""
    from types import SimpleNamespace
    from unittest.mock import patch

    dst = Path(_TMP) / "pg-atomic.dump"
    dst.write_bytes(b"LAST-GOOD-DUMP-BYTES")
    url = "postgresql+psycopg://backup_user:FAKE_PW@localhost:5432/backup_db"

    def partials():
        return [p.name for p in Path(_TMP).glob("pg-atomic.dump.*.partial")]

    def failing_dump(argv, **kwargs):
        # a real failing pg_dump may emit partial bytes before it errors out
        if argv[-1] != "--version":
            stream = kwargs.get("stdout")
            if stream is not None:
                stream.write(b"partial-pg-dump-output")
            return SimpleNamespace(returncode=1, stderr=b"pg_dump: connection failed")
        return SimpleNamespace(returncode=0, stdout="pg_dump (PostgreSQL) 16.4\n")

    with (
        patch.object(config, "DB_URL", url),
        patch.object(config, "IS_SQLITE", False),
        patch.object(bk.shutil, "which", return_value="pg_dump"),
        patch("subprocess.run", side_effect=failing_dump),
    ):
        code, out, err = run(["db", "backup", str(dst), "--force"])
    assert code != 0, (code, out, err)
    assert dst.read_bytes() == b"LAST-GOOD-DUMP-BYTES", dst.read_bytes()
    assert partials() == [], f"temp sibling left behind: {partials()}"

    def succeeding_dump(argv, **kwargs):
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="pg_dump (PostgreSQL) 16.4\n")
        stream = kwargs.get("stdout")
        if stream is not None:
            stream.write(b"NEW-GOOD-DUMP")
        return SimpleNamespace(returncode=0, stderr=b"")

    with (
        patch.object(config, "DB_URL", url),
        patch.object(config, "IS_SQLITE", False),
        patch.object(bk.shutil, "which", return_value="pg_dump"),
        patch("subprocess.run", side_effect=succeeding_dump),
    ):
        code, out, err = run(["db", "backup", str(dst), "--force"])
    assert code == 0, (code, out, err)
    assert dst.read_bytes() == b"NEW-GOOD-DUMP", dst.read_bytes()
    assert partials() == [], f"temp sibling left behind: {partials()}"
    print("  postgres backup preserves the last good dump when pg_dump fails OK")


def test_restore_preview_bad_manifest_is_cli_error():
    """GitHub #544 (3): `db restore` on an archive whose manifest.json is invalid JSON must
    surface the normal CLI error (a CliError; a --json error object), not a raw traceback.
    The dry-run preview was previously unguarded, so _read_manifest's BackupError escaped as
    a traceback (before the fix)."""
    from unittest.mock import patch

    path = str(Path(_TMP) / "bad-manifest.tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        payload = b"{not valid json"
        ti = tarfile.TarInfo(bk.MANIFEST_NAME)
        ti.size = len(payload)
        tar.addfile(ti, io.BytesIO(payload))
    # manifest.json is present (so the restore routes to the full path) but unparsable
    assert bk.is_full_archive(path)

    with patch.object(bk, "server_appears_running", return_value=(False, "test")):
        code, out, err = run(["db", "restore", path, "--yes"])
        jcode, payload, jerr = run_json(["db", "restore", path, "--yes"])
    assert code != 0, (code, out, err)
    assert "Traceback" not in out and "Traceback" not in err, (out, err)
    assert jcode != 0, (jcode, payload, jerr)
    assert payload.get("ok") is False, payload
    assert "manifest.json" in payload.get("error", ""), payload
    print("  db restore preview on a bad manifest is a CLI error, not a traceback OK")


def _postgres_fixture(data, version="pg_dump (PostgreSQL) 16.4"):
    path = str(Path(_TMP) / "postgres-fixture.tar.gz")
    manifest = {
        "manifest_schema": 1,
        "backend": "postgres",
        "db_member": bk.DB_ARC_PG,
        "pg_dump_version": version,
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
    with tarfile.open(path, "w:gz") as tar:
        for name, payload in [
            (bk.MANIFEST_NAME, json.dumps(manifest).encode()),
            (bk.DB_ARC_PG, data),
        ]:
            ti = tarfile.TarInfo(name)
            ti.size = len(payload)
            tar.addfile(ti, io.BytesIO(payload))
    return path


def _postgres_restore_result(*, as_json=False):
    from unittest.mock import patch

    path = _postgres_fixture(b"PGDMP" + bytes(128))
    # pregate: allow-secret synthetic recovery credential; never a live provider key
    url = "postgresql+psycopg://audit_user:AUDIT_FAKE_PASSWORD@localhost:5432/audit_db"
    with (
        patch.object(config, "DB_URL", url),
        patch.object(config, "IS_SQLITE", False),
        patch.object(backup_restore, "verify_archive", return_value=(True, [])),
        patch.object(bk, "server_appears_running", return_value=(False, "test")),
    ):
        result = (run_json if as_json else run)(["db", "restore", path, "--yes"])
    assert "AUDIT_FAKE_PASSWORD" not in str(result)
    assert (config.DATA_DIR / "pg_dump-restore.dump").read_bytes() == b"PGDMP" + bytes(128)
    return path, result


def test_restore_missing_db_member():
    from unittest.mock import patch

    _seed()
    sqlite_path = str(Path(_TMP) / "missing-member-source.tar.gz")
    bk.create_archive(sqlite_path, force=True)
    postgres_path = _postgres_fixture(b"PGDMP" + bytes(128))
    for backend, source in (("sqlite", sqlite_path), ("postgres", postgres_path)):
        path = str(Path(_TMP) / f"{backend}-missing-member.tar.gz")
        _mangle_manifest(source, path, lambda m: m.pop("db_member"))
        with (
            patch.object(config, "IS_SQLITE", backend == "sqlite"),
            patch.object(config, "DB_URL", "postgresql+psycopg:///audit_db"),
            patch.object(bk, "server_appears_running", return_value=(False, "test")),
        ):
            dry = bk.restore_archive(path, dry_run=True)
            code, payload, err = run_json(["db", "restore", path, "--yes", "--skip-verify"])
            human_code, out, human_err = run(["db", "restore", path, "--yes", "--skip-verify"])
        partial = backend == "postgres"
        assert code == human_code == int(partial), (code, err, human_err)
        assert payload["ok"] is (not partial), payload
        for summary in (dry, payload):
            assert summary["status"] == ("partial" if partial else "complete"), summary
            assert summary["db_restore_required"] is partial, summary
            assert summary["db_restored"] is False and summary["verified"] is False
        if partial:
            assert out.startswith("PARTIAL restore from "), out
            assert "Restart the server." not in out
        else:
            assert out == (
                f"Full restore complete from {path} (backend=sqlite).\n"
                "  uploads restored: 3 file(s).\n"
                f"  keyfiles restored (0600): {', '.join(bk.KEYFILE_NAMES)}.\n"
                "  Audit trust state restored together with the database backup.\n"
                "Restart the server.\n"
            ), repr(out)


def test_restore_consent_and_help():
    from unittest.mock import patch

    _seed()
    sqlite_path = str(Path(_TMP) / "consent-sqlite.tar.gz")
    bk.create_archive(sqlite_path, force=True)
    postgres_path = _postgres_fixture(b"PGDMP" + bytes(128))
    for backend, path in (("sqlite", sqlite_path), ("postgres", postgres_path)):
        with (
            patch.object(config, "IS_SQLITE", backend == "sqlite"),
            patch.object(config, "DB_URL", "postgresql+psycopg:///audit_db"),
            patch.object(bk, "server_appears_running", return_value=(False, "test")),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="n") as consent,
        ):
            code, out, err = run(["db", "restore", path])
            help_code, help_out, help_err = run(["db", "restore", "--help"])
        assert code == 1 and "aborted" in err, (code, out, err)
        consent.assert_called_once()
        prompt = consent.call_args.args[0]
        if backend == "sqlite":
            assert prompt == (
                f"FULL RESTORE from {path} (backend=sqlite): this OVERWRITES the "
                "database, the uploads tree (3 file(s) from the archive), and the "
                f"encryption keyfiles ({', '.join(bk.KEYFILE_NAMES)}). "
                "1 existing upload file(s) NOT in this archive will be DELETED. Continue? [y/N] "
            ), prompt
        else:
            for text in (
                "restores the uploads tree",
                "keyfiles",
                "DATABASE is NOT restored",
                "pg_restore recipe follows",
                "exits 1 until it is restored manually",
            ):
                assert text in prompt, prompt
            assert "OVERWRITES the database" not in prompt
        assert help_code == 0, help_err
        rendered = " ".join(help_out.split())
        for text in (
            "DB + uploads + keyfiles on SQLite",
            "plain file restores just the SQLite DB",
            "On Postgres",
            "DATABASE is NOT restored",
            "PARTIAL and exits 1",
            "pg_restore recipe",
        ):
            assert text in rendered, rendered


def test_recovery_docs_contract():
    for name in ("docs/BACKUP.md",):
        text = " ".join((Path(_ROOT) / name).read_text(encoding="utf-8").split()).lower()
        for needle in (
            'status: "partial"',
            "ok: false",
            "db_restore_required: true",
            "exits **1**",
            "pg_restore",
            "do not start the server until the database is restored",
        ):
            assert needle in text, (name, needle)
    lines = (Path(_ROOT) / "docs/DEPLOY.md").read_text(encoding="utf-8").splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith("For PostgreSQL, this command reports")
    )
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("Image tag:"))
    assert all(len(line) <= 81 for line in lines[start:end]), lines[start:end]


def test_postgres_restore_exit():
    for as_json in (False, True):
        _, (code, _, _) = _postgres_restore_result(as_json=as_json)
        assert code == 1, f"Postgres restore exit {code}; expected 1"


def test_postgres_restore_json():
    _, (_, payload, _) = _postgres_restore_result(as_json=True)
    assert payload["ok"] is False, f"Postgres restore ok {payload['ok']}; expected False"
    assert payload["status"] == "partial", payload
    assert payload["db_restore_required"] is True, payload
    assert payload["db_restored"] is False, payload


def test_postgres_restore_human():
    path, (_, out, _) = _postgres_restore_result()
    lines = out.splitlines()
    expected = f"PARTIAL restore from {path} (backend=postgres): the DATABASE WAS NOT RESTORED."
    assert lines[0] == expected, lines[0]
    assert "Restart the server." not in out, out
    assert lines[-1] == "Do NOT start the server until the database is restored.", out
    assert out.index("pg_restore --clean --if-exists") < out.index(lines[-1]), out


def test_restore_dry_run_status():
    from unittest.mock import patch

    _seed()
    sqlite_path = str(Path(_TMP) / "sqlite-verdict.tar.gz")
    bk.create_archive(sqlite_path, force=True)
    postgres_path = _postgres_fixture(b"PGDMP" + bytes(128))
    for backend, path in (("sqlite", sqlite_path), ("postgres", postgres_path)):
        with (
            patch.object(config, "IS_SQLITE", backend == "sqlite"),
            patch.object(config, "DB_URL", "postgresql+psycopg:///audit_db"),
        ):
            summary = bk.restore_archive(path, dry_run=True)
        assert summary["status"] == ("complete" if backend == "sqlite" else "partial")
        assert summary["db_restore_required"] is (backend == "postgres")
        assert summary["dry_run"] is True and summary["verified"] is False


def test_sqlite_restore_verdict_and_output():
    from unittest.mock import patch

    _seed()
    path = str(Path(_TMP) / "sqlite-verdict.tar.gz")
    bk.create_archive(path, force=True)
    with patch.object(bk, "server_appears_running", return_value=(False, "test")):
        code, out, err = run(["db", "restore", path, "--yes"])
        json_code, payload, json_err = run_json(["db", "restore", path, "--yes"])
    # Captured from main@12201cb1's unchanged cmd_db.py before the #119 implementation.
    expected = (
        f"Full restore complete from {path} (backend=sqlite).\n"
        "  DB restored.\n"
        "  uploads restored: 3 file(s).\n"
        f"  keyfiles restored (0600): {', '.join(bk.KEYFILE_NAMES)}.\n"
        "  Audit trust state restored together with the database backup.\n"
        "Restart the server.\n"
    )
    assert out == expected, repr(out)
    assert code == json_code == 0, (code, err, json_code, json_err)
    assert payload["ok"] is True, payload
    assert payload["status"] == "complete", payload
    assert payload["db_restore_required"] is False, payload


def test_postgres_verification():
    from types import SimpleNamespace
    from unittest.mock import patch

    path = _postgres_fixture(b"PGDMP")
    assert not bk.verify_archive(path)[0], "five-byte PGDMP accepted"
    path = _postgres_fixture(b"PGDMP" + bytes(128))
    toc = (
        "; Archive created at test\n; Dumped by pg_dump version: 16.4\n"
        "1; 1259 42 TABLE public sample owner\n"
    )

    def stub(argv, **kwargs):
        assert argv[:2] == ["pg_restore", "--list"], argv
        assert Path(argv[2]).read_bytes().startswith(b"PGDMP")
        return SimpleNamespace(returncode=0, stdout=toc, stderr="")

    with (
        patch.object(bk.shutil, "which", return_value="pg_restore"),
        patch("subprocess.run", side_effect=stub),
    ):
        assert bk.verify_archive(path)[0]
    with patch.object(bk.shutil, "which", return_value=None):
        ok, checks = bk.verify_archive(path)
        assert not ok and any(
            c["status"] == "warn"
            and c["detail"] == "pg_restore not available: archive not validated"
            for c in checks
        ), checks
        for flags in ([], ["--json"]):
            code, out, err = run([*flags, "db", "verify-backup", path])
            assert code != 0 and "UNVERIFIED" in out, (code, out, err)
    with (
        patch.object(bk.shutil, "which", return_value="pg_restore"),
        patch(
            "subprocess.run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="truncated"),
        ),
    ):
        assert not bk.verify_archive(path)[0]
    # LOW-3 (round 2): an EMPTY recorded banner (size still matches) is the advisory metadata
    # `warn` lane, NOT a fail. Pin the LANE + status explicitly -- `not ok` alone was vacuous
    # (a warn and a fail both make ok False, so it proved nothing about which fired).
    path = _postgres_fixture(b"PGDMP" + bytes(128), version="")
    with (
        patch.object(bk.shutil, "which", return_value="pg_restore"),
        patch("subprocess.run", side_effect=stub),
    ):
        ok, checks = bk.verify_archive(path)
    by = {c["check"]: c for c in checks}
    assert ok is False, checks
    assert by["dump_metadata"]["status"] == "warn", checks
    assert "version not recorded" in by["dump_metadata"]["detail"], checks
    assert by["db_integrity"]["status"] == "ok", checks


def test_truncated_sqlite_with_matching_hash():
    src = Path(_TMP) / "truncate-source.db"
    with sqlite3.connect(src) as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('preserve me')")
    data = src.read_bytes()[:100]
    pg = _postgres_fixture(data)
    target = str(Path(_TMP) / "truncated-sqlite.tar.gz")
    _mangle_manifest(pg, target, lambda m: m.update(backend="sqlite"))
    ok, checks = bk.verify_archive(target)
    assert not ok, checks
    assert any(c["check"] == "db_integrity" and c["status"] == "fail" for c in checks), checks
    assert any(c["check"].startswith("member:") and c["status"] == "ok" for c in checks), checks


def test_schema1_archive_verifies_and_restores():
    """HIGH-3: a schema-1 Postgres checkpoint (an earlier release recorded no dump
    metadata) verifies as UNVERIFIED for that reason -- NOT `fail` -- and the documented
    failed-upgrade `db restore` of it SUCCEEDS. This is the exact archive DEPLOY.md tells
    the operator to restore in a failed upgrade."""
    from types import SimpleNamespace
    from unittest.mock import patch

    data = b"PGDMP" + bytes(128)
    base = _postgres_fixture(data)
    target = str(Path(_TMP) / "schema1-nometa.tar.gz")

    def strip_meta(m):
        m["manifest_schema"] = 1
        m.pop("dump_size", None)
        m.pop("pg_dump_version", None)

    _mangle_manifest(base, target, strip_meta)
    toc = "; Dumped by pg_dump version: 16.4\n1; 1259 42 TABLE public sample owner\n"

    def stub(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=toc, stderr="")

    with (
        patch.object(bk.shutil, "which", return_value="pg_restore"),
        patch("subprocess.run", side_effect=stub),
    ):
        ok, checks = bk.verify_archive(target)
    by = {c["check"]: c for c in checks}
    assert by["manifest"]["status"] == "ok", checks  # schema 1 is ACCEPTED, not a fail
    assert by["dump_metadata"]["status"] == "warn", checks
    assert "earlier release" in by["dump_metadata"]["detail"], checks
    assert by["db_integrity"]["status"] == "ok", checks  # TOC validated w/o a version check
    assert ok is False, "an advisory metadata warn keeps the overall verdict UNVERIFIED"

    url = "postgresql+psycopg://u:pw@localhost:5432/mydb"
    _orig = bk.server_appears_running
    bk.server_appears_running = lambda: (False, "test")
    try:
        with (
            patch.object(config, "DB_URL", url),
            patch.object(config, "IS_SQLITE", False),
            patch.object(bk.shutil, "which", return_value="pg_restore"),
            patch("subprocess.run", side_effect=stub),
        ):
            summary = bk.restore_archive(target)  # must NOT raise on the metadata warn
    finally:
        bk.server_appears_running = _orig
    assert summary["backend"] == "postgres" and summary["pg_restore_hint"], summary


def test_verify_fail_wins_over_warn():
    """MED-1: a genuinely corrupt archive that ALSO carries a warn (pg_restore absent)
    reads FAILED, not merely UNVERIFIED, in both the verdict and the restore refusal."""
    from unittest.mock import patch

    data = b"PGDMP" + bytes(128)  # 133 bytes
    base = _postgres_fixture(data)
    corrupt = str(Path(_TMP) / "corrupt-plus-warn.tar.gz")
    # Same length so the size check passes and the DB member fails on SHA, while the
    # metadata + PGDMP magic still hold so db_integrity reaches the pg_restore-absent warn.
    _rewrite_member(base, corrupt, bk.DB_ARC_PG, b"PGDMP" + bytes(127) + b"\x01")
    with patch.object(bk.shutil, "which", return_value=None):
        ok, checks = bk.verify_archive(corrupt)
    assert ok is False
    assert any(c["status"] == "fail" for c in checks), checks
    assert any(c["status"] == "warn" for c in checks), checks
    with patch.object(bk.shutil, "which", return_value=None):
        code, payload, err = run_json(["db", "verify-backup", corrupt])
    assert code == 1 and payload["verification"] == "FAILED", payload  # fail wins over warn

    _orig = bk.server_appears_running
    bk.server_appears_running = lambda: (False, "test")
    try:
        with (
            patch.object(config, "IS_SQLITE", False),  # a postgres archive on a postgres install
            patch.object(bk.shutil, "which", return_value=None),
        ):
            code, _out, err = run(["db", "restore", corrupt, "--yes"])
    finally:
        bk.server_appears_running = _orig
    assert code != 0 and "FAILED verification" in err, (code, err)
    assert "UNVERIFIED" not in err, err


def test_warn_branch_names_the_escape_hatch():
    """MED-1: the warn (pg_restore absent) refusal names `db verify-backup` and
    `--skip-verify`, the same exits the fail branch offers, so an operator on a bare
    machine is not left with a hard refusal and no documented way out."""
    from unittest.mock import patch

    path = _postgres_fixture(b"PGDMP" + bytes(128))
    _orig = bk.server_appears_running
    bk.server_appears_running = lambda: (False, "test")
    try:
        with patch.object(bk.shutil, "which", return_value=None):
            raised = None
            try:
                bk.restore_archive(path)
            except bk.BackupError as exc:
                raised = str(exc)
    finally:
        bk.server_appears_running = _orig
    assert raised and raised.startswith("UNVERIFIED"), raised
    assert "db verify-backup" in raised and "--skip-verify" in raised, raised


def test_connection_args_omit_absent():
    """MED-2: a socket/env-driven URL emits a flag only for the component it supplies,
    so libpq resolves the rest from PGHOST/PGPORT/PGUSER or the default socket."""
    flags, _ = bk._pg_connection_args("postgresql+psycopg:///mydb")
    assert flags == ["-d", "mydb"], flags
    flags, pw = bk._pg_connection_args("postgresql+psycopg://u:pw@/mydb")
    assert flags == ["-U", "u", "-d", "mydb"] and pw == "pw", (flags, pw)
    flags, _ = bk._pg_connection_args("postgresql+psycopg://u:pw@h:5432/db")
    assert flags == ["-h", "h", "-p", "5432", "-U", "u", "-d", "db"], flags
    # MED-2 (round 2): a socket directory / port supplied in the URL QUERY (libpq's own
    # ?host=/var/run/postgresql&port= form) is HONORED -- SQLAlchemy parses it into
    # parts.query, not parts.host/parts.port, so the code must read it there or it silently
    # drops the operator's configured socket target (the exact case the helper's comment
    # names as covered).
    flags, _ = bk._pg_connection_args("postgresql+psycopg:///mydb?host=/var/run/postgresql")
    assert flags == ["-h", "/var/run/postgresql", "-d", "mydb"], flags
    flags, _ = bk._pg_connection_args(
        "postgresql+psycopg:///mydb?host=/var/run/postgresql&port=5433"
    )
    assert flags == ["-h", "/var/run/postgresql", "-p", "5433", "-d", "mydb"], flags


def test_backup_survives_unreadable_pg_dump_banner():
    """LOW-3: once the dump is written, an unreadable/nonstandard pg_dump --version banner
    is recorded as absent and verify reports UNVERIFIED -- it never loses the backup."""
    from types import SimpleNamespace
    from unittest.mock import patch

    url = "postgresql+psycopg://u:pw@localhost:5432/mydb"

    def dump_tool(argv, **kwargs):
        if argv[:2] == ["pg_dump", "--version"]:
            # a localized/vendor banner that does not match the exact English shape
            return SimpleNamespace(returncode=0, stdout="pg_dump (PostgreSQL localise) 16.4\n")
        kwargs["stdout"].write(b"PGDMP" + bytes(128))
        return SimpleNamespace(returncode=0)

    out_path = str(Path(_TMP) / "pg-noversion.tar.gz")
    with (
        patch.object(config, "DB_URL", url),
        patch.object(config, "IS_SQLITE", False),
        patch.object(bk.shutil, "which", return_value="pg_dump"),
        patch("subprocess.run", side_effect=dump_tool),
    ):
        manifest = bk.create_archive(out_path, force=True)
    assert manifest["dump_size"] == 133, manifest
    assert "pg_dump_version" not in manifest, manifest  # absent banner is not recorded

    def stub(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="1; 1259 42 TABLE public s owner\n", stderr="")

    with (
        patch.object(bk.shutil, "which", return_value="pg_restore"),
        patch("subprocess.run", side_effect=stub),
    ):
        _ok, checks = bk.verify_archive(out_path)
    by = {c["check"]: c for c in checks}
    assert by["dump_metadata"]["status"] == "warn", checks
    assert "version not recorded" in by["dump_metadata"]["detail"], checks
    assert by["db_integrity"]["status"] == "ok", checks  # TOC validated without a version check


def test_malformed_banner_is_warn_not_fail():
    """LOW-4 (round 2): a recorded pg_dump banner that is PRESENT but malformed (the dump
    SIZE still matches the member) is an advisory `warn`, never a `fail`. A banner-text
    problem must not destroy an already-written, restorable backup; db_integrity still
    validates the dump via pg_restore --list."""
    from types import SimpleNamespace
    from unittest.mock import patch

    path = _postgres_fixture(b"PGDMP" + bytes(128), version="not a real banner")
    toc = "; Dumped by pg_dump version: 16.4\n1; 1259 42 TABLE public s owner\n"

    def stub(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=toc, stderr="")

    with (
        patch.object(bk.shutil, "which", return_value="pg_restore"),
        patch("subprocess.run", side_effect=stub),
    ):
        ok, checks = bk.verify_archive(path)
    by = {c["check"]: c for c in checks}
    assert ok is False, checks
    assert by["dump_metadata"]["status"] == "warn", checks
    assert "malformed" in by["dump_metadata"]["detail"], checks
    assert by["db_integrity"]["status"] == "ok", checks


def test_banner_toc_mismatch_is_warn_not_fail():
    """LOW-4 (round 2): a well-formed recorded banner that DISAGREES with the dump's own TOC
    version (size matched) is an advisory `warn`, NOT a db_integrity `fail`. pg_restore
    validated the TOC, so db_integrity stays `ok`; the version divergence is advisory only
    and never fails a restorable backup."""
    from types import SimpleNamespace
    from unittest.mock import patch

    path = _postgres_fixture(b"PGDMP" + bytes(128), version="pg_dump (PostgreSQL) 16.4")
    # the TOC reports a DIFFERENT version than the recorded banner
    toc = "; Dumped by pg_dump version: 15.1\n1; 1259 42 TABLE public s owner\n"

    def stub(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=toc, stderr="")

    with (
        patch.object(bk.shutil, "which", return_value="pg_restore"),
        patch("subprocess.run", side_effect=stub),
    ):
        ok, checks = bk.verify_archive(path)
    by = {c["check"]: c for c in checks}
    assert ok is False, checks
    assert by["db_integrity"]["status"] == "ok", checks
    assert by["dump_metadata"]["status"] == "warn", checks
    assert "TOC" in by["dump_metadata"]["detail"], checks


def test_dr_docs_state_size_fails_version_warns():
    """MED-1/LOW-2 (round 3 gate): round 3 RELAXED a dump-tool version disagreement from a
    fail to an advisory warn (test_banner_toc_mismatch_is_warn_not_fail pins that behavior),
    but the train's own recovery docs still asserted verification 'requires ... a matching
    dump-tool version'. For a recovery doc, overstating what verification enforces is the
    exact defect class this train exists to prevent. Pin both docs to the SHIPPED rule: a
    recorded dump SIZE that disagrees fails; a pg_dump VERSION that is absent, malformed or
    disagrees with the TOC is advisory and does not block a restore."""
    dr = (Path(_ROOT) / "docs/architecture/backups-and-dr.md").read_text(encoding="utf-8")
    # MED-1: the false "requires ... matching dump-tool version" enforcement claim is gone.
    assert "matching dump-tool version" not in dr, "backups-and-dr.md still overstates enforcement"
    assert "does not block a restore" in dr, (
        "backups-and-dr.md must state the version-warn relaxation"
    )
    assert "disagrees with the archived member" in dr, (
        "backups-and-dr.md must state the size-fails rule"
    )
    # LOW-2: docs/CLI.md draws the same size-fails / version-advisory distinction.
    cli = (Path(_ROOT) / "docs/CLI.md").read_text(encoding="utf-8")
    seg = cli[cli.find("Postgres verification uses pg_restore") :][:600]
    assert "does not block a restore" in seg, "docs/CLI.md must name the advisory version lane"
    assert "disagrees with the archived member" in seg, "docs/CLI.md must name the size-fails lane"


def main():
    _seed()
    _legacy_audit_restore()
    tests = [
        test_restore_missing_db_member,
        test_restore_consent_and_help,
        test_recovery_docs_contract,
        test_postgres_restore_exit,
        test_postgres_restore_json,
        test_postgres_restore_human,
        test_restore_dry_run_status,
        test_sqlite_restore_verdict_and_output,
        test_recovery_credentials,
        test_dr_docs_state_size_fails_version_warns,
        test_postgres_verification,
        test_restore_output_and_dump_metadata,
        test_postgres_backup_preserves_last_good_dump,
        test_restore_preview_bad_manifest_is_cli_error,
        test_truncated_sqlite_with_matching_hash,
        test_schema1_archive_verifies_and_restores,
        test_verify_fail_wins_over_warn,
        test_warn_branch_names_the_escape_hatch,
        test_connection_args_omit_absent,
        test_backup_survives_unreadable_pg_dump_banner,
        test_malformed_banner_is_warn_not_fail,
        test_banner_toc_mismatch_is_warn_not_fail,
    ]
    assert {f.__name__ for f in tests} == {n for n in globals() if n.startswith("test_")}
    for test in tests:
        test()
    _seed()
    pre = _snapshot_disk_state()
    archive = os.path.join(_TMP, "backup-full.tar.gz")

    # --- 1. CREATE -----------------------------------------------------------
    code, payload, err = run_json(["db", "backup", "--full", archive])
    assert code == 0, f"backup --full exit {code}: {err}"
    assert payload["ok"] and payload["secrets_included"] is True, payload
    assert set(payload["keyfiles_included"]) == set(bk.KEYFILE_NAMES), payload
    assert os.path.isfile(archive), "archive not written"
    # loud secrets warning on stderr
    assert "CONTAINS live secret key material" in err, err

    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
        manifest = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
    assert "manifest.json" in names, names
    assert "db/loc_tms.db" in names, names
    assert "keyfiles/.enc_key" in names and "keyfiles/.session_secret" in names, names
    assert any(n.startswith("uploads/") for n in names), names
    # scratch .tmp excluded
    assert not any(n.endswith(".tmp") for n in names), names
    # every manifest sha256 matches a re-hash of the extracted member
    with tarfile.open(archive, "r:gz") as tar:
        for m in manifest["members"]:
            data = tar.extractfile(m["path"]).read()
            assert hashlib.sha256(data).hexdigest() == m["sha256"], f"sha mismatch {m['path']}"
            assert len(data) == m["bytes"], f"size mismatch {m['path']}"
    # keyfile members stored 0600 (mode forced regardless of host FS)
    with tarfile.open(archive, "r:gz") as tar:
        for name in bk.KEYFILE_NAMES:
            assert tar.getmember("keyfiles/" + name).mode == 0o600, name
    print("  [1] create: archive members + manifest sha256s + 0600 keyfiles OK")

    # --- 2. VERIFY (pass) ----------------------------------------------------
    code, out, err = run(["db", "verify-backup", archive])
    assert code == 0, f"verify-backup exit {code}: {out}{err}"
    assert "PASSED" in out, out
    code, payload, err = run_json(["db", "verify-backup", archive])
    assert code == 0 and payload["ok"] is True, payload
    assert all(c["status"] in ("ok", "skip") for c in payload["checks"]), payload
    # the DB integrity_check actually ran (not skipped) on this plaintext SQLite DB
    integ = [c for c in payload["checks"] if c["check"] == "db_integrity"]
    assert integ and integ[0]["status"] == "ok", integ
    print("  [2] verify-backup PASSES on a good archive (DB integrity_check ran) OK")

    # --- 3a. VERIFY (fail: corrupted upload member) --------------------------
    corrupt = os.path.join(_TMP, "corrupt-upload.tar.gz")
    _rewrite_member(archive, corrupt, "uploads/a/doc1.bin", b"X" * 1024)
    code, payload, err = run_json(["db", "verify-backup", corrupt])
    assert code == 1 and payload["ok"] is False, payload
    bad = [c for c in payload["checks"] if c["check"] == "member:uploads/a/doc1.bin"]
    assert bad and bad[0]["status"] == "fail", bad
    print("  [3a] verify-backup FAILS (exit 1) on a corrupted upload member OK")

    # --- 3b. VERIFY (fail: garbage DB member -> integrity_check) -------------
    bad_db = os.path.join(_TMP, "corrupt-db.tar.gz")
    # same byte length as the real DB member so the size check passes and the failure
    # is forced through the integrity_check path, not the sha (sha still catches it too).
    real_len = next(m["bytes"] for m in manifest["members"] if m["path"] == "db/loc_tms.db")
    _rewrite_member(archive, bad_db, "db/loc_tms.db", b"\x00" * real_len)
    code, payload, err = run_json(["db", "verify-backup", bad_db])
    assert code == 1 and payload["ok"] is False, payload
    integ = [c for c in payload["checks"] if c["check"] == "db_integrity"]
    assert integ and integ[0]["status"] == "fail", integ
    print("  [3b] verify-backup FAILS on a garbage DB member (integrity_check) OK")

    # --- 4a. RESTORE guard: refuse while server appears running --------------
    _orig_probe = bk.server_appears_running
    bk.server_appears_running = lambda: (True, "127.0.0.1:8000")
    try:
        code, out, err = run(["db", "restore", archive, "--yes"])
        assert code == 1 and "appears to be RUNNING" in err, (code, err)
    finally:
        bk.server_appears_running = _orig_probe
    print("  [4a] restore refused while server appears running OK")

    # --- 4b. RESTORE onto an emptied target ----------------------------------
    # Empty the target: drop the DB, wipe uploads, clobber the keyfiles.
    import shutil

    from app.db import engine

    engine.dispose()  # release the SQLite handle so the file is replaceable on Windows
    for sc in ("", "-wal", "-shm"):
        p = str(config.DB_PATH) + sc
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(config.UPLOADS_DIR, ignore_errors=True)
    for name in bk.KEYFILE_NAMES:
        (config.DATA_DIR / name).write_text("CLOBBERED", encoding="utf-8")

    bk.server_appears_running = lambda: (False, "127.0.0.1:8000")
    try:
        code, payload, err = run_json(["db", "restore", archive, "--yes"])
    finally:
        bk.server_appears_running = _orig_probe
    assert code == 0 and payload["ok"], (code, payload, err)
    assert payload["db_restored"] is True, payload
    assert set(payload["keyfiles_restored"]) == set(bk.KEYFILE_NAMES), payload

    # DB is back + integrity ok
    assert os.path.isfile(config.DB_PATH), "DB not restored"
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    # uploads + keyfiles reproduced byte-for-byte vs the pre-backup snapshot
    post = _snapshot_disk_state()
    assert post == pre, f"restored tree differs:\n pre={pre}\n post={post}"
    # scratch .tmp was NOT resurrected (excluded from the archive)
    assert not (config.UPLOADS_DIR / "a" / "half.tmp").exists(), "scratch file resurrected"
    print("  [4b] restore reproduces DB + uploads tree + keyfiles byte-for-byte OK")

    # --- 5. LEGACY behaviors unbroken ----------------------------------------
    engine.dispose()
    plain = os.path.join(_TMP, "legacy.db")
    code, out, err = run(["db", "backup", plain])
    assert code == 0 and os.path.isfile(plain), (code, err)
    # it's a real SQLite file, NOT a full archive (auto-detect must not misroute it)
    assert not bk.is_full_archive(plain), "legacy backup wrongly detected as a full archive"
    conn = sqlite3.connect(plain)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    # legacy restore still works (server-guard patched off; a real DB passes the header check)
    engine.dispose()
    bk.server_appears_running = lambda: (False, "127.0.0.1:8000")
    try:
        code, out, err = run(["db", "restore", plain, "--yes"])
    finally:
        bk.server_appears_running = _orig_probe
    assert code == 0, f"legacy restore exit {code}: {err}"
    assert "Database restored" in out, out
    print("  [5] legacy db backup/restore (single-file) still works OK")

    # --- 6. VERIFY fails on an UNLISTED member (gate finding 4) ---------------
    inject = os.path.join(_TMP, "inject.tar.gz")
    _add_member(archive, inject, "uploads/injected_payload.exe", b"MALICIOUS-UNLISTED")
    code, payload, err = run_json(["db", "verify-backup", inject])
    assert code == 1 and payload["ok"] is False, payload
    bad = [c for c in payload["checks"] if c["check"] == "unlisted:uploads/injected_payload.exe"]
    assert bad and bad[0]["status"] == "fail", bad
    print("  [6] verify-backup FAILS on an unlisted (injected) member OK")

    # --- 7. RESTORE verifies BEFORE clobbering (gate finding 2) ---------------
    # Re-seed a known-good target, corrupt an archive of it, confirm restore refuses
    # AND leaves the live data untouched.
    engine.dispose()
    _seed_marker(b"PRE-RESTORE-DB-MARKER")
    (config.UPLOADS_DIR / "sentinel.txt").write_bytes(b"LIVE-SENTINEL")
    engine.dispose()
    good7 = os.path.join(_TMP, "good7.tar.gz")
    assert run(["db", "backup", "--full", good7, "--force"])[0] == 0
    corrupt7 = os.path.join(_TMP, "corrupt7.tar.gz")
    _rewrite_member(good7, corrupt7, "uploads/sentinel.txt", b"XXXXXXXXXXXX")
    engine.dispose()
    bk.server_appears_running = lambda: (False, "127.0.0.1:8000")
    try:
        code, out, err = run(["db", "restore", corrupt7, "--yes"])
    finally:
        bk.server_appears_running = _orig_probe
    assert code == 1 and "FAILED verification" in err, (code, err)
    assert (config.UPLOADS_DIR / "sentinel.txt").read_bytes() == b"LIVE-SENTINEL", "clobbered!"
    assert _has_marker(b"PRE-RESTORE-DB-MARKER"), "DB clobbered by a bad archive"
    print("  [7] restore VERIFIES first and refuses a corrupt archive (data intact) OK")

    # --- 8. ATOMIC rollback on induced late failure (gate finding 3) ---------
    engine.dispose()
    _seed_marker(b"MARKER-BEFORE-ROLLBACK")
    engine.dispose()
    good8 = os.path.join(_TMP, "good8.tar.gz")
    assert run(["db", "backup", "--full", good8, "--force"])[0] == 0
    # Make the archive's DB + keyfiles DIFFERENT so a successful swap would be visible.
    _seed_marker(b"MARKER-AFTER-BACKUP")
    for n in bk.KEYFILE_NAMES:
        (config.DATA_DIR / n).write_bytes(b"LIVE-KEY-" + n.encode())
    live_keys = {n: (config.DATA_DIR / n).read_bytes() for n in bk.KEYFILE_NAMES}
    engine.dispose()
    # Induce a failure at the LAST swap (uploads) AFTER keyfiles+DB were swapped.
    _orig_swap_dir = backup_restore._swap_dir

    def _boom(src, dst, swaps):
        raise RuntimeError("induced late failure at the uploads swap")

    backup_restore._swap_dir = _boom
    bk.server_appears_running = lambda: (False, "127.0.0.1:8000")
    try:
        raised = False
        try:
            bk.restore_archive(good8)
        except bk.BackupError:
            raised = True
        assert raised, "restore should have raised BackupError on the induced failure"
    finally:
        backup_restore._swap_dir = _orig_swap_dir
        bk.server_appears_running = _orig_probe
    # DB + keyfiles must be rolled back to the LIVE (post-backup) state, not the archive.
    assert _has_marker(b"MARKER-AFTER-BACKUP"), "DB not rolled back after induced failure"
    now_keys = {n: (config.DATA_DIR / n).read_bytes() for n in bk.KEYFILE_NAMES}
    assert now_keys == live_keys, "keyfiles not rolled back after induced failure"
    # no .pre-restore / staging turds left behind
    leftovers = [
        p.name
        for p in config.DATA_DIR.iterdir()
        if ".pre-restore" in p.name or p.name.startswith(".restore-staging")
    ]
    assert not leftovers, f"rollback left turds: {leftovers}"
    print("  [8] atomic restore rolls DB + keyfiles back on a late failure (no turds) OK")

    # --- 9. link-member refusal (gate finding 4: filter=data + guard) --------
    sym = os.path.join(_TMP, "sym.tar.gz")
    _add_symlink(archive, sym, "uploads/escape", "../../../../etc")
    # verify flags it as unlisted; _safe_extractall refuses the link outright
    ok9, _checks9 = bk.verify_archive(sym)
    assert ok9 is False, "verify accepted a symlink-bearing archive"
    xdir = os.path.join(_TMP, "x9")
    os.makedirs(xdir, exist_ok=True)
    raised9 = False
    try:
        with tarfile.open(sym, "r:gz") as t:
            bk._safe_extractall(t, xdir)
    except bk.BackupError:
        raised9 = True
    assert raised9, "_safe_extractall did not refuse a link member"
    print("  [9] symlink/link member refused by verify + _safe_extractall OK")

    # --- 10. decompression-bomb byte cap (gate finding 4) --------------------
    xdir2 = os.path.join(_TMP, "x10")
    os.makedirs(xdir2, exist_ok=True)
    capped = False
    try:
        with tarfile.open(archive, "r:gz") as t:
            bk._safe_extractall(t, xdir2, max_total_bytes=128)  # absurdly small cap
    except bk.BackupError as exc:
        capped = "bomb" in str(exc) or "allowed" in str(exc)
    assert capped, "extraction byte cap did not trip"
    print("  [10] extraction total-bytes cap trips (decompression-bomb guard) OK")

    # --- 11. legacy restore refuses a FOREIGN tar / non-DB file (finding 5) --
    foreign = os.path.join(_TMP, "foreign.tar.gz")
    with tarfile.open(foreign, "w:gz") as t:
        ti = tarfile.TarInfo("other_project/readme.txt")
        data = b"not a database at all"
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    assert not bk.is_full_archive(foreign), "foreign tar wrongly detected as a full archive"
    engine.dispose()
    _seed_marker(b"DB-MUST-SURVIVE-FOREIGN")
    engine.dispose()
    before16 = Path(config.DB_PATH).read_bytes()[:16]
    bk.server_appears_running = lambda: (False, "127.0.0.1:8000")
    try:
        code, out, err = run(["db", "restore", foreign, "--yes"])
    finally:
        bk.server_appears_running = _orig_probe
    assert code == 2 and "not a SQLite database" in err, (code, err)
    after16 = Path(config.DB_PATH).read_bytes()[:16]
    assert after16 == before16, "live DB overwritten by a non-DB file"
    assert _has_marker(b"DB-MUST-SURVIVE-FOREIGN"), "DB clobbered by a foreign tar"
    print("  [11] legacy restore refuses a foreign/non-DB file (DB intact) OK")

    # --- 12. archive created 0600 (POSIX; Windows synthesizes modes) ---------
    if os.name == "posix":
        import stat as _stat

        assert _stat.S_IMODE(os.stat(archive).st_mode) == 0o600, "archive not 0600 on POSIX"
        print("  [12] archive file is 0600 on POSIX (holds live secrets) OK")
    else:
        print("  [12] archive-0600 assert skipped on Windows (synthesized modes); chmod runs OK")

    # --- 14. R1: absent-target keyfiles + induced late failure -> nothing left --
    # A fresh machine has NO keyfiles yet (pre is None on the swap). A failed restore
    # must NOT leave the archive's live key material stranded on disk.
    engine.dispose()
    _seed_marker(b"R1-DB")
    (config.UPLOADS_DIR).mkdir(parents=True, exist_ok=True)
    good14 = os.path.join(_TMP, "good14.tar.gz")
    assert run(["db", "backup", "--full", good14, "--force"])[0] == 0
    for n in bk.KEYFILE_NAMES:  # remove the live keyfiles -> target has NONE
        p = config.DATA_DIR / n
        if p.exists():
            p.unlink()
    engine.dispose()
    _orig_swap_dir14 = backup_restore._swap_dir

    def _boom14(src, dst, swaps):
        raise RuntimeError("induced late failure (uploads swap)")

    backup_restore._swap_dir = _boom14
    bk.server_appears_running = lambda: (False, "127.0.0.1:8000")
    try:
        r1_raised = False
        try:
            bk.restore_archive(good14)
        except bk.BackupError:
            r1_raised = True
        assert r1_raised, "restore should have raised BackupError"
    finally:
        backup_restore._swap_dir = _orig_swap_dir14
        bk.server_appears_running = _orig_probe
    left = [n for n in bk.KEYFILE_NAMES if (config.DATA_DIR / n).exists()]
    assert left == [], f"archive keyfiles left installed after rollback: {left}"
    leftovers14 = [
        p.name
        for p in config.DATA_DIR.iterdir()
        if ".pre-restore" in p.name or p.name.startswith(".restore-staging")
    ]
    assert not leftovers14, f"rollback left turds: {leftovers14}"
    print("  [14] rollback with absent-target keyfiles leaves NOTHING installed OK")

    # --- 15. R2: malformed manifest (bytes=null) + --skip-verify -> no raw error -
    _seed_marker(b"R2-DB")
    (config.UPLOADS_DIR).mkdir(parents=True, exist_ok=True)
    engine.dispose()
    good15 = os.path.join(_TMP, "good15.tar.gz")
    assert run(["db", "backup", "--full", good15, "--force"])[0] == 0
    bad15 = os.path.join(_TMP, "bad15.tar.gz")
    _mangle_manifest(good15, bad15, lambda m: [mem.update(bytes=None) for mem in m["members"]])
    engine.dispose()
    bk.server_appears_running = lambda: (False, "127.0.0.1:8000")
    try:
        r2_type_error = False
        try:
            bk.restore_archive(bad15, skip_verify=True)  # completes OR clean BackupError
        except bk.BackupError:
            pass
        except TypeError:
            r2_type_error = True
        assert not r2_type_error, "raw TypeError leaked on the malformed-manifest path"
    finally:
        bk.server_appears_running = _orig_probe
    print("  [15] malformed manifest + --skip-verify: no raw TypeError (contract held) OK")

    # --- 16. RES-1: sweep KEEPS a live concurrent restore's staging, drops a dead one -
    import subprocess

    engine.dispose()
    _seed_marker(b"R16-DB")
    (config.UPLOADS_DIR).mkdir(parents=True, exist_ok=True)
    good16 = os.path.join(_TMP, "good16.tar.gz")
    assert run(["db", "backup", "--full", good16, "--force"])[0] == 0
    # a LIVE child whose pid names a staging dir the sweep will see
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        live_sib = config.DATA_DIR / f".restore-staging-{child.pid}"
        live_sib.mkdir(exist_ok=True)
        (live_sib / "in_flight_key").write_bytes(b"A SIBLING'S EXTRACTED KEY MATERIAL")
        # a DEAD-pid staging dir (child.pid+... very unlikely to be live) must be swept
        dead_sib = config.DATA_DIR / ".restore-staging-999999"
        dead_sib.mkdir(exist_ok=True)
        (dead_sib / "junk").write_bytes(b"junk")
        engine.dispose()
        bk.server_appears_running = lambda: (False, "127.0.0.1:8000")
        try:
            code, out, err = run(["db", "restore", good16, "--yes"])
        finally:
            bk.server_appears_running = _orig_probe
        assert code == 0, (code, err)
        assert live_sib.exists(), "sweep DELETED a live concurrent restore's staging dir"
        assert not dead_sib.exists(), "sweep did NOT remove a dead-pid staging dir"
    finally:
        child.terminate()
        child.wait(timeout=5)
        import shutil as _sh

        _sh.rmtree(config.DATA_DIR / f".restore-staging-{child.pid}", ignore_errors=True)
    print("  [16] sweep keeps a LIVE sibling's staging, removes a DEAD-pid one OK")

    # --- 17. Existing recovery copies require operator recovery (#394) ----------
    engine.dispose()
    _seed_marker(b"R17-DB")
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / ".enc_key").write_bytes(config.ENCRYPTION_KEY.encode())
    (config.DATA_DIR / ".session_secret").write_bytes(config.SESSION_SECRET.encode())
    good17 = os.path.join(_TMP, "good17.tar.gz")
    assert run(["db", "backup", "--full", good17, "--force"])[0] == 0
    recovery = config.DATA_DIR / ".enc_key.pre-restore"
    recovery.write_bytes(b"ONLY-SURVIVING-KEY")
    (config.DATA_DIR / ".enc_key").unlink()
    old_orphan = config.DATA_DIR / "orphan.pre-restore"
    old_orphan.write_bytes(b"OLD-ORPHAN")
    old_mtime = time.time() - 3600
    os.utime(old_orphan, (old_mtime, old_mtime))
    engine.dispose()
    bk.server_appears_running = lambda: (False, "127.0.0.1:8000")
    try:
        code, out, err = run(["db", "restore", good17, "--yes"])
    finally:
        bk.server_appears_running = _orig_probe
    assert code == 1, (code, out, err)
    assert "existing recovery copy" in out + err, (out, err)
    assert recovery.read_bytes() == b"ONLY-SURVIVING-KEY"
    assert not (config.DATA_DIR / ".enc_key").exists()
    assert old_orphan.read_bytes() == b"OLD-ORPHAN"
    recovery.unlink()
    old_orphan.unlink()
    print("  [17] restore refuses recovery copy; sweep preserves old original OK")

    # --- 18. RES-3: refusal names FREE SPACE when the free-space ceiling binds ------
    xdir18 = os.path.join(_TMP, "x18")
    os.makedirs(xdir18, exist_ok=True)
    msg18 = ""
    try:
        with tarfile.open(archive, "r:gz") as t:
            bk._safe_extractall(t, xdir18, max_total_bytes=64, cap_is_free_space=True)
    except bk.BackupError as exc:
        msg18 = str(exc)
    assert "FREE DISK SPACE" in msg18 and "bomb" not in msg18.lower(), msg18
    # and the plain (manifest) cap still uses the bomb wording
    msg18b = ""
    try:
        with tarfile.open(archive, "r:gz") as t:
            bk._safe_extractall(t, xdir18, max_total_bytes=64, cap_is_free_space=False)
    except bk.BackupError as exc:
        msg18b = str(exc)
    assert "bomb" in msg18b.lower(), msg18b
    print("  [18] refusal names free space when the free-space ceiling binds OK")

    # --- 19. #397: archive temp sibling + new backup dir private-at-creation ---------
    # The old write-then-chmod order left a predictable <dst>.partial group/other-readable
    # (mode 0666 & ~umask) for a normal 022 umask the whole time it was written, and a
    # sibling account in the same backup dir could open it and hold the fd past the
    # chmod. The archive must be written to an unpredictable 0600 sibling (O_CREAT|O_EXCL)
    # and a newly created backup dir must be mkdir'd 0700. Asserted cross-platform by
    # recording the mode/flags handed to os.open/os.mkdir (the create call is what
    # matters), plus a POSIX stat of the sibling the instant it is created.
    import stat as _stat19
    from unittest.mock import patch as _patch19

    init_db()
    (config.DATA_DIR / ".enc_key").write_text(config.ENCRYPTION_KEY, encoding="utf-8")
    (config.DATA_DIR / ".session_secret").write_text(config.SESSION_SECRET, encoding="utf-8")
    engine.dispose()

    dst19 = os.path.join(_TMP, "private19.tar.gz")
    newdir19 = os.path.join(_TMP, "backups19", "sub")  # parent does not exist yet
    dst19b = os.path.join(newdir19, "arch.tar.gz")

    real_open19 = os.open
    opened19 = []

    def _rec_open19(path, flags, mode=0o777, **kw):
        fd = real_open19(path, flags, mode, **kw)
        name = os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else None
        observed = os.fstat(fd).st_mode if name is not None else -1
        opened19.append((name, flags, mode, observed))
        return fd

    real_mkdir19 = os.mkdir
    made19 = []
    backup_dirs19 = {os.path.abspath(os.path.dirname(newdir19)), os.path.abspath(newdir19)}

    def _rec_mkdir19(path, mode=0o777, **kw):
        made19.append((os.path.abspath(os.fspath(path)), mode))
        result = real_mkdir19(path, mode, **kw)
        if os.name == "posix" and os.path.abspath(path) in backup_dirs19:
            assert _stat19.S_IMODE(os.stat(path).st_mode) & 0o077 == 0, (
                "backup directory world/group-accessible at creation"
            )
        return result

    real_chmod19 = os.chmod
    chmodded19 = []

    def _rec_chmod19(path, mode, **kw):
        chmodded19.append((os.path.abspath(path), mode))
        return real_chmod19(path, mode, **kw)

    with (
        _patch19("os.open", _rec_open19),
        _patch19("os.mkdir", _rec_mkdir19),
        _patch19("os.chmod", _rec_chmod19),
    ):
        manifest19 = bk.create_archive(dst19, force=True)
        assert os.path.isfile(dst19) and manifest19["secrets_included"], "archive/keyfiles missing"
        # the temp sibling that held the archive bytes: created in _TMP, O_CREAT, not dst
        sib19 = [
            (n, fl, md, obs)
            for (n, fl, md, obs) in opened19
            if n is not None
            and os.path.dirname(os.path.abspath(n)) == os.path.abspath(_TMP)
            and fl & os.O_CREAT
            and os.path.abspath(n) != os.path.abspath(dst19)
        ]
        assert sib19, "archive was not written via an O_CREAT temp sibling (write-then-chmod?)"
        for _n, fl, md, obs in sib19:
            assert md == 0o600, f"temp sibling opened with mode {oct(md)}, not 0600"
            assert fl & os.O_EXCL, "temp sibling not created with O_EXCL (predictable name?)"
            if os.name == "posix":
                assert _stat19.S_IMODE(obs) & 0o077 == 0, (
                    f"temp sibling world/group-readable at creation: {oct(_stat19.S_IMODE(obs))}"
                )
        assert not os.path.exists(dst19 + ".partial"), "predictable <dst>.partial left behind"
        # a newly created backup dir must be mkdir'd private-at-creation (0700, not 0777)
        bk.create_archive(dst19b, force=True)
        for directory in (os.path.dirname(newdir19), newdir19):
            made_parent = [m for (p, m) in made19 if p == os.path.abspath(directory)]
            assert made_parent, f"backup directory {directory} was never created via os.mkdir"
            assert all(m == 0o700 for m in made_parent), (
                f"backup directory {directory} mkdir'd with mode "
                f"{[oct(m) for m in made_parent]}, not 0700"
            )
            assert (os.path.abspath(directory), 0o700) in chmodded19
        assert all(p != os.path.abspath(_TMP) for p, _m in chmodded19), "existing ancestor chmod'd"
    if os.name == "posix":
        print("  [19] archive sibling 0600+O_EXCL + new backup dir 0700 private-at-creation OK")
    else:
        print("  [19] archive sibling 0600+O_EXCL + new backup dir 0700 (create args) OK")

    # --- 20. Missing roots and non-directory ancestors fail cleanly ------------
    # Isolate the patched root in a child: the old ancestor walk never terminates.
    import subprocess

    failures20 = []
    for operation in ("backup", "private_directory"):
        script20 = """
import sys
from pathlib import Path
from unittest.mock import patch
from app import config
from app.cli import backup_archive as bk
root = Path.cwd().anchor
with (
    patch.object(Path, "exists", return_value=False),
    patch.object(Path, "is_dir", return_value=False),
):
    try:
        if sys.argv[1] == "backup":
            bk.create_archive(str(Path(root) / "missing" / "backup.tar.gz"), force=True)
        else:
            config.private_directory(Path(root) / "missing")
    except (bk.BackupError, FileNotFoundError) as exc:
        assert "root does not exist" in str(exc), str(exc)
        if sys.argv[1] == "backup":
            assert isinstance(exc, bk.BackupError), type(exc)
    else:
        raise AssertionError("missing root accepted")
"""
        try:
            result20 = subprocess.run(
                [sys.executable, "-c", script20, operation],
                cwd=_ROOT,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result20.returncode:
                failures20.append(f"{operation}: {result20.stdout}{result20.stderr}")
        except subprocess.TimeoutExpired:
            failures20.append(f"{operation}: missing-root check timed out")
    blocked20 = config.DATA_DIR / "file-as-parent20"
    blocked20.write_bytes(b"not a directory")
    for destination20 in (blocked20 / "backup.tar.gz", blocked20 / "sub" / "backup.tar.gz"):
        try:
            bk.create_archive(str(destination20), force=True)
        except bk.BackupError as exc:
            assert "cannot create backup directory" in str(exc), str(exc)
        except OSError as exc:
            failures20.append(f"file ancestor leaked {type(exc).__name__}")
        else:
            failures20.append("file ancestor accepted")
    assert not failures20, "\n".join(failures20)
    print("  [20] missing roots and file ancestors fail cleanly within timeout OK")

    # --- 13. SQLCipher live-writer backup via the online backup API ----------
    _sqlcipher_subprocess_case()

    print("verify_backup_full ... OK")
    return 0


def _mangle_manifest(src_archive, dst_archive, mutate):
    """Copy src->dst, applying mutate(manifest_dict) to the parsed manifest in place."""
    with tarfile.open(src_archive, "r:gz") as s, tarfile.open(dst_archive, "w:gz") as d:
        for ti in s.getmembers():
            data = s.extractfile(ti).read() if ti.isfile() else b""
            if ti.name == "manifest.json":
                m = json.loads(data)
                mutate(m)
                data = json.dumps(m).encode()
                ti = tarfile.TarInfo("manifest.json")
                ti.size = len(data)
            d.addfile(ti, io.BytesIO(data))


def _seed_marker(marker: bytes) -> None:
    """(Re)build a clean DB carrying a probe_marker table with `marker`, so a later
    check can tell whether the DB was replaced/rolled back."""
    init_db()
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        conn.execute("DROP TABLE IF EXISTS probe_marker")
        conn.execute("CREATE TABLE probe_marker (v BLOB)")
        conn.execute("INSERT INTO probe_marker VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


def _has_marker(marker: bytes) -> bool:
    from app.db import engine as _e

    _e.dispose()
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        row = conn.execute("SELECT v FROM probe_marker").fetchone()
        return bool(row) and bytes(row[0]) == marker
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _add_member(src_archive, dst_archive, name, data):
    """Copy src_archive to dst_archive, ADDING an extra file member not in the manifest."""
    import io

    with tarfile.open(src_archive, "r:gz") as src, tarfile.open(dst_archive, "w:gz") as dst:
        for ti in src.getmembers():
            d = src.extractfile(ti).read() if ti.isfile() else b""
            dst.addfile(ti, io.BytesIO(d))
        ti = tarfile.TarInfo(name=name)
        ti.size = len(data)
        dst.addfile(ti, io.BytesIO(data))


def _add_symlink(src_archive, dst_archive, name, target):
    """Copy src_archive to dst_archive, adding a SYMLINK member pointing at target."""
    import io

    with tarfile.open(src_archive, "r:gz") as src, tarfile.open(dst_archive, "w:gz") as dst:
        for ti in src.getmembers():
            d = src.extractfile(ti).read() if ti.isfile() else b""
            dst.addfile(ti, io.BytesIO(d))
        ti = tarfile.TarInfo(name=name)
        ti.type = tarfile.SYMTYPE
        ti.linkname = target
        ti.size = 0
        dst.addfile(ti)


def _sqlcipher_subprocess_case():
    """SQLCipher snapshot must use the keyed online backup API (gate finding 1): a
    competing reader holding an open WAL snapshot must NOT cause silent row loss, and
    verify must run a KEYED integrity_check (not skip). SQLCipher mode is import-time,
    so this runs in a fresh subprocess. Skipped (announced) when sqlcipher3 is absent."""
    import importlib.util
    import subprocess

    if importlib.util.find_spec("sqlcipher3") is None:
        print("  [13] SQLCipher case SKIPPED (sqlcipher3 not importable) OK")
        return
    script = os.path.join(_TMP, "_cipher_case.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_CIPHER_SCRIPT)
    proc = subprocess.run([sys.executable, script], capture_output=True, text=True, cwd=_ROOT)
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0 and out.strip().splitlines()[-1].endswith("CIPHER-OK"), out
    print("  [13] SQLCipher live-writer backup: no data loss + keyed integrity_check OK")


_CIPHER_SCRIPT = r"""
import os, sys, tempfile, tarfile
_ROOT = r"{root}"
os.chdir(_ROOT); sys.path.insert(0, _ROOT)
_TMP = tempfile.mkdtemp(prefix="loctms_cipher_case_")
os.environ["LOC_TMS_DATA_DIR"] = _TMP
os.environ["LOC_TMS_DB_ENCRYPTION"] = "sqlcipher"
os.environ["ENCRYPTION_KEY"] = "0" * 43 + "="
for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LOCAL_BASE_URL"):
    os.environ[k] = ""
from app import config
from app.cli import backup_archive as bk
from app.db import engine, init_db
from app.services import dbcrypt
init_db()
assert config.DB_ENCRYPTION_ON
with engine.connect() as c:
    c.exec_driver_sql("CREATE TABLE IF NOT EXISTS probe (i INTEGER)")
    c.exec_driver_sql("INSERT INTO probe VALUES (1),(2),(3)")
    c.commit()
# competing reader holds an open WAL snapshot so a naive checkpoint would go BUSY
blocker = dbcrypt.connect(str(config.DB_PATH))
blocker.execute("BEGIN")
blocker.execute("SELECT count(*) FROM probe").fetchone()
with engine.connect() as c:
    c.exec_driver_sql("INSERT INTO probe VALUES (10),(11),(12),(13),(14)")
    c.commit()
    total = c.exec_driver_sql("SELECT count(*) FROM probe").fetchone()[0]
arc = os.path.join(_TMP, "cipher.tar.gz")
manifest = bk.create_archive(arc, force=True)
blocker.close()
assert manifest["db_encryption"] == "sqlcipher"
with tarfile.open(arc) as t:
    d = os.path.join(_TMP, "x"); os.makedirs(d, exist_ok=True)
    t.extract("db/loc_tms.db", d, filter="data")
snap = os.path.join(d, "db", "loc_tms.db")
# snapshot stays ENCRYPTED and holds ALL committed rows
assert open(snap, "rb").read(16) != b"SQLite format 3\x00", "snapshot is plaintext!"
c2 = dbcrypt.connect(snap)
got = c2.execute("SELECT count(*) FROM probe").fetchone()[0]
c2.close()
assert got == total, "SILENT DATA LOSS: %d of %d rows" % (got, total)
ok, checks = bk.verify_archive(arc)
integ = [c for c in checks if c["check"] == "db_integrity"]
assert ok and integ and integ[0]["status"] == "ok" and "keyed" in integ[0]["detail"].lower(), checks
print("CIPHER-OK")
""".replace("{root}", _ROOT)


def _rewrite_member(src_archive, dst_archive, target_name, new_bytes):
    """Copy src_archive to dst_archive, replacing target_name's content with
    new_bytes and leaving every other member (incl. the manifest) untouched - so the
    manifest's recorded sha256 for target_name no longer matches its bytes."""
    import io

    with tarfile.open(src_archive, "r:gz") as src, tarfile.open(dst_archive, "w:gz") as dst:
        for ti in src.getmembers():
            data = src.extractfile(ti).read() if ti.isfile() else b""
            if ti.name == target_name:
                data = new_bytes
                ti = tarfile.TarInfo(name=ti.name)
                ti.size = len(data)
            dst.addfile(ti, io.BytesIO(data))


if __name__ == "__main__":
    sys.exit(main())
