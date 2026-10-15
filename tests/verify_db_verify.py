"""`db verify` verification — CLI-parity backlog item 1 (read-only DB integrity/
health check, distinct from `audit verify`'s tamper-evident hash-chain check).

Hermetic + offline: throwaway LOC_TMS_DATA_DIR (tests._bootstrap), a plain SQLite
DB built via the create_all+shim path (app.db.init_db under config.IS_TEST — see
its docstring). Covers:

  1. a CLEAN throwaway DB: exit 0, every check "ok" or "skip" (never "fail"); the
     alembic_revision check is honestly "skip" (this DB has no alembic_version
     table — the hermetic-test path never runs `alembic upgrade head`).
  2. a CORRUPTED DB (a Segment row's document_id pointed at a document that does
     not exist, written via a raw sqlite3 connection with PRAGMA foreign_keys=OFF
     so the write bypasses app.db's own FK-enforcing connection listener): exit 1,
     both the SQLite PRAGMA foreign_key_check AND the portable orphan-count check
     for that FK report "fail" and name the row/table.
  3. --json carries the SAME structured checks (machine-readable parity with the
     human table).
  4. --remote is refused cleanly (exit 2) — `db verify` is local-only by design
     (mirrors `health` / `db migrate` / `audit verify`), never silently no-ops.
  5. ALL-SKIP HONESTY (review finding P2): when EVERY check skips (forced here by
     patching engine.dialect.name off "sqlite" so the SQLite PRAGMA checks skip,
     and cmd_db._ORPHAN_FKS to reference tables that don't exist so the portable
     orphan checks skip too — the hermetic DB already has no alembic_version, so
     that check skips naturally), the summary says SKIPPED, not PASSED — a clean
     exit 0 (no failure found is still true) without claiming a pass nothing
     earned. Both monkeypatches are restored in a finally block.

Run standalone: python tests/verify_db_verify.py  ->  prints "... OK".
"""

import os
import sqlite3
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_dbverify_")

import json

from sqlmodel import Session

from app import config
from app.cli import cmd_db
from app.db import engine, init_db
from app.models import Document, Project, Segment, TransMode
from tests._cli_runner import run_cli


def run(argv):
    return run_cli(argv)


def run_json(argv):
    code, out, err = run(["--json"] + argv)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


def _checks_by_name(payload):
    return {c["check"]: c for c in payload["checks"]}


# --- 1. clean DB: exit 0, nothing FAILs, alembic check honestly skips --------
def test_clean_db_passes():
    init_db()
    code, payload, err = run_json(["db", "verify"])
    assert code == 0, (code, payload, err)
    assert payload["ok"] is True, payload
    by = _checks_by_name(payload)
    statuses = {c["status"] for c in payload["checks"]}
    assert "fail" not in statuses, payload["checks"]
    # the hermetic create_all+shim DB never gets an alembic_version table -> skip,
    # not a failure (see app/db.py init_db's docstring: TEST path never runs
    # `alembic upgrade head`).
    assert by["alembic_revision"]["status"] == "skip", by["alembic_revision"]
    assert "no alembic_version table" in by["alembic_revision"]["detail"]
    # sanity: the human table + summary line render too.
    code2, out2, _ = run(["db", "verify"])
    assert code2 == 0, out2
    assert "PASSED" in out2, out2
    assert "sqlite_integrity_check" in out2 and "OK" in out2, out2
    print("  clean DB: db verify exits 0, no FAIL checks, alembic check honestly SKIPs OK")


# --- 2. a corrupted DB: dangling FK caught by BOTH the PRAGMA and the portable
#        orphan-count check -----------------------------------------------------
def test_orphan_fk_detected():
    init_db()
    with Session(engine) as s:
        p = Project(name="DBVerify Proj", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(
            project_id=p.id,
            filename="dbverify.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": []},
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        seg = Segment(
            document_id=d.id,
            target_locale="es-ES",
            order_index=0,
            source_text="hello",
            target_text="",
        )
        s.add(seg)
        s.commit()
        s.refresh(seg)
        seg_id = seg.id

    # Orphan it: point document_id at a document that does not exist. Done via a
    # RAW sqlite3 connection (not the app engine) with foreign_keys=OFF, so the
    # write bypasses app.db's connect-time FK-enforcing PRAGMA listener — exactly
    # the kind of pre-existing corruption `db verify` exists to catch (a real DB
    # can end up this way from a bug, a hand-edit, or a restore from a backup
    # taken mid-write). This never touches data/ or .env — config.DB_PATH here is
    # the suite's own throwaway LOC_TMS_DATA_DIR (see init_env above).
    raw = sqlite3.connect(str(config.DB_PATH))
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("UPDATE segment SET document_id = 999999 WHERE id = ?", (seg_id,))
        raw.commit()
    finally:
        raw.close()

    code, payload, err = run_json(["db", "verify"])
    assert code == 1, (code, payload, err)
    assert payload["ok"] is False, payload
    by = _checks_by_name(payload)

    fk_check = by["sqlite_foreign_key_check"]
    assert fk_check["status"] == "fail", fk_check
    assert "segment" in fk_check["detail"], fk_check

    orphan_check = by["orphan:segment.document_id -> document.id"]
    assert orphan_check["status"] == "fail", orphan_check
    assert "1 orphaned row" in orphan_check["detail"], orphan_check

    # integrity_check itself is unaffected (a dangling FK is not file corruption).
    assert by["sqlite_integrity_check"]["status"] == "ok", by["sqlite_integrity_check"]

    # human mode reports FAILED + shows the two FAIL rows.
    code2, out2, _ = run(["db", "verify"])
    assert code2 == 1, out2
    assert "FAILED" in out2, out2
    assert "FAIL" in out2 and "segment" in out2, out2
    print(
        "  corrupted DB: dangling segment.document_id caught by BOTH the SQLite "
        "PRAGMA and the portable orphan-count check; exit 1; FAILED in human output OK"
    )


# --- 3. --remote is refused cleanly (local-only by design) -------------------
def test_remote_refused():
    code, out, err = run(["--remote", "http://example.invalid", "--token", "lsu_x", "db", "verify"])
    assert code == 2, (code, out, err)
    assert "not available over --remote" in err, err
    print("  db verify --remote is refused cleanly (exit 2, local-only by design) OK")


# --- 5. all-skip honesty (review finding P2) ----------------------------------
def test_all_skip_is_not_reported_as_passed():
    init_db()
    prev_dialect_name = engine.dialect.name
    prev_orphan_fks = cmd_db._ORPHAN_FKS
    # Force EVERY check to skip: dialect.name off "sqlite" skips the two SQLite
    # PRAGMA checks; a bogus orphan-FK table skips the portable checks (the
    # hermetic DB already has no alembic_version, so that check skips naturally).
    engine.dialect.name = "postgresql"
    cmd_db._ORPHAN_FKS = [
        ("bogus.a -> bogus.b", "no_such_child_table", "a", "no_such_parent_table", "b", False),
    ]
    try:
        code, payload, err = run_json(["db", "verify"])
        assert code == 0, (code, payload, err)  # no failure found -> exit 0 stays true
        assert payload["ok"] is True, payload
        statuses = {c["status"] for c in payload["checks"]}
        assert statuses == {"skip"}, ("expected every check to skip", payload["checks"])
        assert payload.get("all_skipped") is True, payload

        code2, out2, _err2 = run(["db", "verify"])
        assert code2 == 0, out2
        assert "SKIPPED" in out2, out2
        assert "nothing was actually verified" in out2, out2
        assert "PASSED" not in out2, ("must not claim a pass when everything skipped", out2)
    finally:
        engine.dialect.name = prev_dialect_name
        cmd_db._ORPHAN_FKS = prev_orphan_fks
    print(
        "  P2 fix: when EVERY check skips, db verify reports SKIPPED (not PASSED) "
        "in --json (all_skipped=True) and human output; exit code stays 0 OK"
    )


def test_deployment_truth():
    from tests._deploy_doc_truth import check_deploy_commands

    assert check_deploy_commands()
    from pathlib import Path

    text = (Path(_ROOT) / "docs/DEPLOY.md").read_text(encoding="utf-8")
    for required in (
        "## Upgrade",
        "RPO 24 h",
        "RTO 1 h",
        "non-additive",
        "alembic heads",
        "alembic current",
        "backup-full",
        "never run an old image against a new schema",
        # HIGH-1: /health is coarse liveness; `version` is the authoritative release check.
        "coarse major.minor",
        # HIGH-3: a pre-P2a checkpoint verifies UNVERIFIED for the metadata reason but is
        # restorable; only a missing-pg_restore UNVERIFIED blocks.
        "dump metadata not recorded",
        # MED-4: retention (KEEP), the alert hook, and the nightly downtime window.
        "KEEP",
        "ALERT_CMD",
        "Retention:",
        "Downtime:",
        # LOW-2: the compose image tag rename is documented for existing installs.
        "localization-os:latest",
        "${IMAGE_TAG:-local}",
    ):
        assert required in text, required

    # MED-4: the shipped nightly script actually implements retention, an alert hook, and a
    # distinct nonzero exit for an UNVERIFIED verify (so the scheduler can page on it).
    script = (Path(_ROOT) / "scripts/nightly-backup.sh").read_text(encoding="utf-8")
    for required in ('KEEP="${KEEP:-14}"', "ALERT_CMD", "UNVERIFIED", "exit 3", "nightly-*.tar.gz"):
        assert required in script, ("nightly-backup.sh missing", required)
    # #385: the restart must reuse the stopped (scanned) container. `compose up` could recreate
    # it from whatever image the scheduler's environment resolves, never scanned.
    assert "docker compose start app" in script and "trap on_exit EXIT" in script, (
        "restart the same container from the EXIT trap"
    )
    assert "compose up" not in script, "nightly backup must not recreate the app container"


def _find_posix_sh():
    """Locate a POSIX sh plus the dirs its coreutils live in. On Linux `shutil.which('sh')`
    is enough; on Windows, Git-for-Windows ships sh under .../Git/usr/bin (coreutils beside
    it). Returns (sh_path, [dirs to PREPEND to the child PATH]) or (None, [])."""
    import shutil
    from pathlib import Path

    sh = shutil.which("sh")
    if sh is None:
        git = shutil.which("git")
        if git:
            root = Path(git).resolve().parents[1]
            for cand in (root / "usr" / "bin" / "sh.exe", root / "bin" / "sh.exe"):
                if cand.exists():
                    sh = str(cand)
                    break
    if not sh:
        return None, []
    shp = Path(sh).resolve()
    extra: list[str] = []
    for cand in (shp.parent, shp.parent.parent / "usr" / "bin", shp.parent.parent / "bin"):
        s = str(cand)
        if cand.exists() and s not in extra:
            extra.append(s)
    return str(shp), extra


def test_nightly_script_runs_end_to_end():
    """HIGH-1 (round 2): EXECUTE scripts/nightly-backup.sh behind a stub `docker` that
    forwards the post-`app` container argv to the REAL interpreter (so the verify step's
    `-m app.cli` module flag is genuinely exercised, not substring-pinned). A substring pin
    proved the tokens existed while the verify invocation could not run at all -- python
    rejected `--json` and the job paged FAILED + exited 2 on every healthy backup, never
    pruning. Assertions: a healthy backup -> exit 0, NO alert, retention prunes to KEEP; a
    FAILED verify -> exit 2 and ALERT_CMD fires (and retention does NOT run)."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    sh, path_dirs = _find_posix_sh()
    if not sh:
        print("  [HIGH-1] nightly-backup.sh execution SKIPPED (no POSIX sh available) OK")
        return

    init_db()  # seed the DB the backup-full subprocess snapshots

    def _fwd(p):  # forward-slash path: Git sh + Windows python both accept it
        return str(p).replace("\\", "/")

    scratch = Path(tempfile.mkdtemp(prefix="loctms_nightly_"))
    stub_dir = scratch / "stub"
    stub_dir.mkdir()
    bk_dir = scratch / "bk"
    alert_log = scratch / "alert.log"

    # Stub `docker`: `stop`/`start` are no-ops; `compose run ... app <argv>` drops everything up
    # to and including the `app` service token, rewrites the mounted /checkpoint/ prefix to
    # the real BACKUP_DIR, and exec's REAL_PYTHON with the remaining argv (so the entrypoint
    # override `--entrypoint python` is honored faithfully). With STUB_FAIL=1 the verify-backup
    # invocation instead prints a canned FAILED verdict, to drive the failure lane.
    (stub_dir / "docker").write_text(
        "#!/bin/sh\n"
        'sub="$2"\n'
        'if [ "$sub" = "stop" ] || [ "$sub" = "start" ]; then exit 0; fi\n'
        "shift 2\n"
        'while [ "$#" -gt 0 ] && [ "$1" != "app" ]; do shift; done\n'
        "shift\n"
        "is_verify=0\n"
        'for a in "$@"; do [ "$a" = "verify-backup" ] && is_verify=1; done\n'
        'if [ "$is_verify" = "1" ] && [ "${STUB_FAIL:-0}" = "1" ]; then\n'
        '  printf \'%s\\n\' \'{"ok": false, "verification": "FAILED"}\'\n'
        "  exit 1\n"
        "fi\n"
        'newargs=""\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        "    /checkpoint/*) a=\"$BACKUP_DIR/$(printf '%s' \"$a\" | sed 's#^/checkpoint/##')\" ;;\n"
        "  esac\n"
        '  newargs="$newargs \\"$a\\""\n'
        "done\n"
        'eval "exec \\"$REAL_PYTHON\\" $newargs"\n',
        encoding="utf-8",
    )
    (stub_dir / "alert_sink").write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$1" >> "' + _fwd(alert_log) + '"\n',
        encoding="utf-8",
    )
    # Linux refuses to exec a stub without the exec bit (Git Bash does not care).
    for _stub in ("docker", "alert_sink"):
        os.chmod(stub_dir / _stub, 0o700)

    def _run(keep, fail):
        if bk_dir.exists():
            shutil.rmtree(bk_dir)
        bk_dir.mkdir(parents=True)
        # seed 8 OLD nightly archives (all older than the fresh one) so retention has debris
        for i in range(8):
            f = bk_dir / f"nightly-2026010{i}T000000Z.tar.gz"
            f.write_bytes(b"old-seed")
            old = time.time() - (10000 - i)
            os.utime(f, (old, old))
        if alert_log.exists():
            alert_log.unlink()
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join([*path_dirs, str(stub_dir), env.get("PATH", "")])
        env["APP_DIR"] = _fwd(_ROOT)
        env["BACKUP_DIR"] = _fwd(bk_dir)
        env["KEEP"] = str(keep)
        env["ALERT_CMD"] = _fwd(stub_dir / "alert_sink")
        env["REAL_PYTHON"] = _fwd(sys.executable)
        env["LOC_TMS_DATA_DIR"] = str(config.DATA_DIR)
        env["STUB_FAIL"] = "1" if fail else "0"
        proc = subprocess.run(
            [sh, _fwd(Path(_ROOT) / "scripts/nightly-backup.sh")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=240,
        )
        alerts = alert_log.read_text(encoding="utf-8") if alert_log.exists() else ""
        archives = sorted(p.name for p in bk_dir.glob("nightly-*.tar.gz"))
        return proc, alerts, archives

    try:
        # good path: a healthy backup verifies PASSED -> exit 0, NO alert, prune to KEEP=5.
        # This is the discriminator: with `-m app.cli` dropped, the stub exec's
        # `python --json ...`, python rejects --json, the job alerts FAILED and exits 2.
        proc, alerts, archives = _run(keep=5, fail=False)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert alerts == "", ("a healthy backup must not alert", alerts, proc.stdout, proc.stderr)
        assert len(archives) == 5, ("retention did not prune to KEEP", archives)

        # failure path: a FAILED verify -> exit 2, ALERT_CMD fires, retention does NOT run.
        proc, alerts, archives = _run(keep=5, fail=True)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
        assert "FAILED verification" in alerts, (alerts, proc.stdout, proc.stderr)
        # #526: the failed archive is deleted (never counted as a kept copy), and the 8 older
        # verified archives are all still there.
        assert len(archives) == 8, (
            "a failed verify must not prune or leave the bad archive",
            archives,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    # Belt-and-suspenders (LOW-1): EVERY compose-run python invocation carries `-m app.cli`,
    # the exact token whose loss made the verify step dead.
    import re as _re

    script = (Path(_ROOT) / "scripts/nightly-backup.sh").read_text(encoding="utf-8")
    runs = _re.findall(r"--entrypoint python\b.*?\bapp\b([^\n]*)", script, _re.DOTALL)
    assert runs, "no compose-run python invocations found in nightly-backup.sh"
    for tail in runs:
        assert tail.lstrip().startswith("-m app.cli"), ("compose-run missing -m app.cli", tail)
    print("  [HIGH-1] nightly-backup.sh executes: good->exit0+prune, fail->exit2+alert OK")


def main():
    tests = [
        test_clean_db_passes,
        test_orphan_fk_detected,
        test_remote_refused,
        test_all_skip_is_not_reported_as_passed,
        test_deployment_truth,
        test_nightly_script_runs_end_to_end,
    ]
    assert {f.__name__ for f in tests} == {n for n in globals() if n.startswith("test_")}
    for test in tests:
        test()
    print("verify_db_verify OK")


if __name__ == "__main__":
    main()
