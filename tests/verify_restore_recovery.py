"""Offline regression checks for #394, incomplete restore swaps and recovery copies."""

import os
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._bootstrap import init_env

ROOT = Path(init_env("loctms_restore_recovery_"))

from app import config
from app.cli import backup_restore as restore


def swap_failure(kind, original):
    folder = ROOT / f"{kind}-{original}"
    folder.mkdir()
    src, dst = folder / "staged", folder / "final"
    if kind == "dir":
        src.mkdir()
        (src / "payload").write_bytes(b"new")
        if original:
            dst.mkdir()
            (dst / "payload").write_bytes(b"old")
    else:
        src.write_bytes(b"new")
        if original:
            dst.write_bytes(b"old")
    journal = []
    replace = os.replace

    def fail_install(source, target):
        if str(source) == str(src):
            raise OSError("injected installation failure")
        replace(source, target)

    with patch.object(restore.os, "replace", side_effect=fail_install):
        try:
            getattr(restore, f"_swap_{kind}")(str(src), str(dst), journal)
        except OSError:
            pass
        else:
            raise AssertionError("expected installation failure")
        restore._rollback(journal)
    if original:
        payload = dst / "payload" if kind == "dir" else dst
        assert payload.read_bytes() == b"old", "original stranded outside journal"
    else:
        assert not dst.exists()
    assert not Path(str(dst) + ".pre-restore").exists()


def recovery_copy(kind, original):
    folder = ROOT / f"leftover-{kind}-{original}"
    folder.mkdir()
    src, dst = folder / "staged", folder / "final"
    pre = Path(str(dst) + ".pre-restore")
    if kind == "dir":
        src.mkdir()
        pre.mkdir()
        (pre / "payload").write_bytes(b"only original")
        if original:
            dst.mkdir()
    else:
        src.write_bytes(b"new")
        pre.write_bytes(b"only original")
        if original:
            dst.write_bytes(b"current")
    os.utime(pre, (1, 1))
    restore._sweep_restore_leftovers(folder)
    payload = pre / "payload" if kind == "dir" else pre
    assert payload.read_bytes() == b"only original", "sweep destroyed recovery copy"
    journal = []
    try:
        getattr(restore, f"_swap_{kind}")(str(src), str(dst), journal)
    except restore.BackupError as exc:
        assert str(pre) in str(exc)
    else:
        raise AssertionError("retry must refuse existing recovery copy")
    assert not journal
    assert payload.read_bytes() == b"only original"
    assert src.exists()
    assert dst.exists() == original


def rollback_error_reporting(error_type):
    folder = ROOT / f"reporting-{error_type.__name__}"
    folder.mkdir()
    uploads = folder / "uploads"
    uploads.mkdir()
    (uploads / "payload").write_bytes(b"original uploads")
    archive = folder / "input.tar"
    manifest = folder / "manifest.json"
    manifest.write_bytes(b'{"backend":"sqlite","members":[]}')
    with tarfile.open(archive, "w") as tar:
        tar.add(manifest, arcname="manifest.json")
    replace = os.replace

    def fail_install_and_rollback(source, target):
        if str(target) == str(uploads):
            if str(source).endswith(".pre-restore"):
                raise OSError("injected rollback failure")
            raise error_type("injected rename failure")
        replace(source, target)

    with (
        patch.object(config, "DATA_DIR", folder),
        patch.object(config, "UPLOADS_DIR", uploads),
        patch.object(restore.os, "replace", side_effect=fail_install_and_rollback),
    ):
        try:
            restore.restore_archive(str(archive), skip_verify=True)
        except restore.BackupError as exc:
            message = str(exc)
            assert "rollback incomplete" in message, message
            assert "was rolled back" not in message, message
            assert str(uploads) in message, message
        else:
            raise AssertionError("expected restore failure")
    pre = folder / "uploads.pre-restore"
    assert (pre / "payload").read_bytes() == b"original uploads"
    # Recovery must remain possible even after the copy becomes old and a retry runs.
    os.utime(pre, (1, 1))
    restore._sweep_restore_leftovers(folder)
    assert (pre / "payload").read_bytes() == b"original uploads"


def rollback_failures():
    """Failed removals/renames are visible, and other entries still recover."""
    for kind in ("file", "dir"):
        for original in (True, False):
            folder = ROOT / f"rollback-{kind}-{original}"
            folder.mkdir()
            final = folder / "final"
            pre = folder / "final.pre-restore" if original else None
            if kind == "dir":
                final.mkdir()
                if pre:
                    pre.mkdir()
                    (pre / "payload").write_bytes(b"old")
            else:
                final.write_bytes(b"new")
                if pre:
                    pre.write_bytes(b"old")
            other, other_pre = folder / "other", folder / "other.pre-restore"
            other_pre.write_bytes(b"recover me")
            journal = [
                ("file", str(other), str(other_pre)),
                (kind, str(final), str(pre) if pre else None),
            ]
            target = restore.shutil if kind == "dir" else restore.os
            method = "rmtree" if kind == "dir" else "replace" if original else "remove"
            real = getattr(target, method)

            def fail_one(*args, final=final, pre=pre, real=real, **kwargs):
                if str(args[0]) in (str(final), str(pre)):
                    raise OSError("injected rollback failure")
                return real(*args, **kwargs)

            with patch.object(target, method, side_effect=fail_one):
                failures = restore._rollback(journal)
            assert failures and str(final) in failures[0], failures
            assert other.read_bytes() == b"recover me", "rollback stopped at first failure"
            if pre:
                payload = pre / "payload" if kind == "dir" else pre
                assert payload.read_bytes() == b"old"


def cleanup_warning():
    from app.cli import backup_archive, cmd_db_db_init_schema

    for kind in ("file", "dir", "clean"):
        folder = ROOT / f"cleanup-{kind}"
        folder.mkdir()
        uploads = folder / "uploads"
        uploads.mkdir()
        (uploads / "payload").write_bytes(b"old uploads")
        key = folder / ".enc_key"
        key.write_bytes(b"old key")
        manifest = folder / "manifest.json"
        manifest.write_bytes(b'{"backend":"sqlite","members":[]}')
        staged_key = folder / "new-key"
        staged_key.write_bytes(b"new key")
        archive = folder / "input.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(manifest, arcname="manifest.json")
            tar.add(staged_key, arcname="keyfiles/.enc_key")
        blocked = str(key if kind == "file" else uploads) + ".pre-restore"
        remove, rmtree = os.remove, restore.shutil.rmtree

        def locked_remove(path, *args, kind=kind, blocked=blocked, remove=remove, **kwargs):
            if kind == "file" and str(path) == blocked:
                raise PermissionError("locked recovery file")
            return remove(path, *args, **kwargs)

        def locked_rmtree(path, *args, kind=kind, blocked=blocked, rmtree=rmtree, **kwargs):
            if kind == "dir" and str(path) == blocked:
                assert kwargs.get("ignore_errors")
                return
            return rmtree(path, *args, **kwargs)

        emit = Mock()
        with (
            patch.object(config, "DATA_DIR", folder),
            patch.object(config, "UPLOADS_DIR", uploads),
            patch.object(restore.os, "remove", side_effect=locked_remove),
            patch.object(restore.shutil, "rmtree", side_effect=locked_rmtree),
            patch.object(backup_archive, "server_appears_running", return_value=(False, "test")),
        ):
            code = cmd_db_db_init_schema._restore_full(
                SimpleNamespace(yes=True, skip_verify=True), emit, str(archive)
            )
        message, summary = emit.result.call_args.args
        assert code == 0 and summary["status"] == "complete"
        assert key.read_bytes() == b"new key"
        expected = [] if kind == "clean" else [blocked]
        assert summary.get("leftover_recovery_copies") == expected, summary
        if expected:
            assert "WARNING" in message and blocked in message, message
            assert "before" in message and "restore" in message, message
        else:
            assert "WARNING" not in message, message


def preflight_conflict():
    folder = ROOT / "preflight"
    folder.mkdir()
    key = folder / ".enc_key"
    key.write_bytes(b"old key")
    manifest = folder / "manifest.json"
    manifest.write_bytes(b'{"backend":"sqlite","members":[]}')
    archive = folder / "input.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(manifest, arcname="manifest.json")
        tar.add(key, arcname="keyfiles/.enc_key")
    uploads = folder / "uploads"
    pre = folder / "uploads.pre-restore"
    pre.mkdir()
    with (
        patch.object(config, "DATA_DIR", folder),
        patch.object(config, "UPLOADS_DIR", uploads),
        patch.object(restore.os, "replace", wraps=os.replace) as replace,
    ):
        try:
            restore.restore_archive(str(archive), skip_verify=True)
        except restore.BackupError as exc:
            assert str(pre) in str(exc)
        else:
            raise AssertionError("expected preflight refusal")
    assert replace.call_count == 0, "conflict discovered after swapping earlier members"
    assert key.read_bytes() == b"old key" and pre.is_dir()


def main():
    failures = []
    for kind in ("file", "dir"):
        for original in (True, False):
            for check in (swap_failure, recovery_copy):
                try:
                    check(kind, original)
                except Exception as exc:
                    failures.append(f"{check.__name__}({kind}, {original}): {exc}")
    for error_type in (OSError, restore.BackupError):
        try:
            rollback_error_reporting(error_type)
        except Exception as exc:
            failures.append(f"rollback_error_reporting({error_type.__name__}): {exc}")
    for check in (rollback_failures, cleanup_warning, preflight_conflict):
        try:
            check()
        except Exception as exc:
            failures.append(f"{check.__name__}: {exc}")
    assert not failures, "\n".join(failures)
    print("verify_restore_recovery OK")


if __name__ == "__main__":
    main()
