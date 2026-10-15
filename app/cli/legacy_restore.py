"""Rollback-protected single-file SQLite recovery.

Documented in: docs/architecture/backups-and-dr.md
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

from app.cli._common import CliError
from app.cli.backup_common import sha256_file


def restore_database(src: str, dst: str) -> None:
    """Validate a private copy, retire old sidecars, then install and verify it.

    The operator must stop all other database users. Keep every displaced file
    until verification passes, including when a move or rollback itself fails.
    """
    from app.cli.cmd_db_db_init_schema import _assert_looks_like_db
    from app.db import engine

    staging = None
    moved = []
    installed = False
    keep_recovery = False
    try:
        if Path(dst).exists() and os.path.samefile(src, dst):
            raise CliError("restore source must differ from the live DB path.")
        # A legacy backup is a standalone snapshot, not an active WAL database.
        # Do not silently omit committed transactions residing beside the source.
        if any(Path(src + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise CliError("backup has SQLite sidecars; create a standalone SQLite backup first.")
        staging = Path(tempfile.mkdtemp(prefix=".legacy-restore-", dir=Path(dst).parent))
        staged = staging / "staged.db"
        shutil.copyfile(src, staged)
        os.chmod(staged, 0o600)
        try:
            _assert_looks_like_db(str(staged))
        except CliError as exc:
            # The private staged copy is deleted before the operator reads this.
            raise CliError(
                str(exc).replace(str(staged), src), exc.code, payload=exc.payload
            ) from exc
        expected = sha256_file(str(staged))
        # CLI boot reads settings through this pool, even for recovery commands.
        engine.dispose()
        # Rename, never suppress removal errors. Record each move immediately so
        # failure midway through the trio still restores every displaced file.
        for suffix in ("-wal", "-shm", "-journal", ""):
            current = Path(dst + suffix)
            if current.exists():
                saved = staging / ("original.db" + suffix)
                os.replace(current, saved)
                moved.append((current, saved))
        os.replace(staged, dst)
        installed = True
        _assert_looks_like_db(dst)
        if any(Path(dst + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise CliError("SQLite sidecars reappeared during restore; stop all database users.")
        if sha256_file(dst) != expected:
            raise CliError("restored database does not match the validated backup.")
    except BaseException as exc:
        try:
            if installed:
                for suffix in ("-wal", "-shm", "-journal", ""):
                    Path(dst + suffix).unlink(missing_ok=True)
            while moved:
                current, saved = moved[-1]
                os.replace(saved, current)
                moved.pop()
        except BaseException as rollback_exc:
            keep_recovery = True
            message = (
                f"restore failed ({exc!r}); rollback incomplete ({rollback_exc!r}). "
                f"Recovery files retained at {staging}. Stop all database users before recovery."
            )
            if isinstance(exc, Exception) and isinstance(rollback_exc, Exception):
                raise CliError(message) from exc
            # Ctrl-C / SystemExit: keep the interrupt semantics but say where the copies are.
            print(message, file=sys.stderr)
            if isinstance(rollback_exc, Exception):
                raise exc from rollback_exc
            raise rollback_exc from exc
        if staging is not None and any(staging.glob("original.db*")):
            # An interrupt landed between a move and its bookkeeping: keep what is there.
            keep_recovery = True
            print(
                f"restore interrupted; displaced originals retained at {staging}.",
                file=sys.stderr,
            )
        if not isinstance(exc, Exception) or isinstance(exc, CliError):
            raise
        raise CliError(f"restore failed; previous database preserved: {exc}") from exc
    finally:
        if staging is not None and not keep_recovery:
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                raise CliError(
                    f"restore cleanup failed; inspect recovery files at {staging}: {exc}"
                ) from exc
