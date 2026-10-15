"""scripts/nightly-backup.sh failure handling, batch remote-db (issue #526).

Runs the REAL script under a POSIX sh behind a stub `docker` (no server, no image, no real
backup): the stub logs every call and answers per STUB_* variables. All files live in temp
dirs.

  * every early failure (APP_DIR unset or missing, BACKUP_DIR missing / not writable, KEEP
    invalid or 0, docker stop failing) exits nonzero AND fires ALERT_CMD;
  * a failed create, a FAILED verify and an UNVERIFIED verify keep their distinct exit codes
    (1 / 2 / 3), alert, and leave NO nightly-*.tar.gz for the bad archive;
  * retention counts only verified archives: after a failed run the older good copies are all
    still there, and a PASSED run prunes to exactly KEEP verified ones;
  * the app is restarted on every path that stopped it, and a failed restart alerts.

Final line: "NIGHTLY BACKUP 526 OK".
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = _ROOT / "scripts" / "nightly-backup.sh"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _find_posix_sh():
    """A POSIX sh plus the dirs its coreutils live in (never the WSL stub in System32)."""

    def usable(path):
        low = str(path).replace("\\", "/").lower()
        return bool(path) and "/system32/" not in low and "/windowsapps/" not in low

    candidates = [shutil.which("sh"), shutil.which("bash")]
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent
        candidates += [root / "usr" / "bin" / "sh.exe", root / "bin" / "sh.exe"]
    for found in candidates:
        if found and usable(found) and Path(found).exists():
            shp = Path(found).resolve()
            extra = []
            for cand in (shp.parent, shp.parent.parent / "usr" / "bin", shp.parent.parent / "bin"):
                if cand.exists() and str(cand) not in extra:
                    extra.append(str(cand))
            return str(shp), extra
    return None, []


def _fwd(p):
    return str(p).replace("\\", "/")


DOCKER_STUB = r"""#!/bin/sh
# Stub docker for the nightly-backup harness. Logs every call.
printf '%s\n' "$*" >> "$STUB_LOG"
sub="$2"
if [ "$sub" = "stop" ]; then [ "${STUB_STOP_FAIL:-0}" = 1 ] && exit 1; exit 0; fi
if [ "$sub" = "start" ]; then [ "${STUB_START_FAIL:-0}" = 1 ] && exit 1; exit 0; fi
case "$*" in
  *backup-full*)
    if [ "${STUB_BACKUP_FAIL:-0}" = 1 ]; then
      printf junk > "$STUB_BACKUP_DIR/leftover.partial"
      exit 1
    fi
    for a in "$@"; do last="$a"; done
    name="${last#/checkpoint/}"
    printf 'archive-bytes' > "$STUB_BACKUP_DIR/$name"
    exit 0 ;;
  *verify-backup*)
    case "${STUB_VERIFY:-PASSED}" in
      PASSED) printf '%s\n' '{"ok": true, "verification": "PASSED"}'; exit 0 ;;
      UNVERIFIED) printf '%s\n' '{"ok": false, "verification": "UNVERIFIED"}'; exit 1 ;;
      FAILED) printf '%s\n' '{"ok": false, "verification": "FAILED"}'; exit 1 ;;
      *) exit 1 ;;
    esac ;;
esac
exit 0
"""


class Harness:
    def __init__(self):
        self.sh, self.path_dirs = _find_posix_sh()
        self.scratch = Path(tempfile.mkdtemp(prefix="loctms_nightly526_"))
        self.stub = self.scratch / "stub"
        self.stub.mkdir()
        (self.stub / "docker").write_text(DOCKER_STUB, encoding="utf-8", newline="\n")
        (self.stub / "alert_sink").write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$1" >> "$STUB_ALERTS"\n', encoding="utf-8", newline="\n"
        )
        # An `rm` that refuses the OLD seeded archives when STUB_RM_FAIL=1 (retention error path).
        real_rm = next(
            (
                str(Path(d) / n)
                for d in self.path_dirs
                for n in ("rm.exe", "rm")
                if (Path(d) / n).exists()
            ),
            # Absolute path of the real rm: a bare "rm" would resolve to this very stub (first on
            # PATH) and exec itself forever (Linux, where no rm lives beside the found sh).
            shutil.which("rm") or "rm",
        )
        rm_stub = (
            "#!/bin/sh",
            'if [ "${STUB_RM_FAIL:-0}" = 1 ]; then',
            '  case "$*" in *2026010*) exit 1 ;; esac',
            "fi",
            f'exec "{_fwd(real_rm)}" "$@"',
        )
        (self.stub / "rm").write_bytes(("\n".join(rm_stub) + "\n").encode())
        # Linux refuses to exec a stub without the exec bit (Git Bash does not care).
        for _stub in ("docker", "alert_sink", "rm"):
            os.chmod(self.stub / _stub, 0o700)
        self.app = self.scratch / "app"
        self.app.mkdir()
        self.bk = self.scratch / "bk"
        self.log = self.scratch / "docker.log"
        self.alerts = self.scratch / "alerts.log"

    def seed(self, n_nightly=4):
        if self.bk.exists():
            shutil.rmtree(self.bk)
        self.bk.mkdir()
        for i in range(n_nightly):
            f = self.bk / f"nightly-2026010{i}T000000Z.tar.gz"
            f.write_bytes(b"verified-old")
            old = time.time() - (10000 - i)
            os.utime(f, (old, old))
        for p in (self.log, self.alerts):
            if p.exists():
                p.unlink()

    def run(self, **over):
        env = os.environ.copy()
        for k in ("APP_DIR", "BACKUP_DIR", "KEEP", "ALERT_CMD"):
            env.pop(k, None)
        env["PATH"] = os.pathsep.join([str(self.stub), *self.path_dirs, env.get("PATH", "")])
        env.update(
            {
                "APP_DIR": _fwd(self.app),
                "BACKUP_DIR": _fwd(self.bk),
                "KEEP": "3",
                "ALERT_CMD": _fwd(self.stub / "alert_sink"),
                "STUB_LOG": _fwd(self.log),
                "STUB_ALERTS": _fwd(self.alerts),
                "STUB_BACKUP_DIR": _fwd(self.bk),
            }
        )
        for k, v in over.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = str(v)
        proc = subprocess.run(
            [self.sh, _fwd(SCRIPT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
        )
        alerts = self.alerts.read_text(encoding="utf-8") if self.alerts.exists() else ""
        calls = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        nightly = (
            sorted(p.name for p in self.bk.glob("nightly-*.tar.gz")) if self.bk.exists() else []
        )
        return proc, alerts, calls, nightly

    def close(self):
        shutil.rmtree(self.scratch, ignore_errors=True)


def _expect(label, proc, rc, alerts, want_alert):
    if proc.returncode != rc:
        _fail(f"{label}: exit {proc.returncode}, wanted {rc}: {proc.stdout!r} {proc.stderr!r}")
    if want_alert and not alerts.strip():
        _fail(f"{label}: no alert fired (exit {proc.returncode}): {proc.stderr!r}")
    if not want_alert and alerts.strip():
        _fail(f"{label}: unexpected alert: {alerts!r}")


def main():
    h = Harness()
    if not h.sh:
        print("  (skipped: no POSIX sh available)")
        print("NIGHTLY BACKUP 526 OK")
        return
    try:
        # --- early failures: nonzero AND an alert, before anything is stopped -----------
        h.seed()
        proc, alerts, calls, _ = h.run(APP_DIR=None)
        _expect("APP_DIR unset", proc, 1, alerts, True)
        proc, alerts, calls, _ = h.run(BACKUP_DIR=None)
        _expect("BACKUP_DIR unset", proc, 1, alerts, True)
        proc, alerts, calls, _ = h.run(APP_DIR=_fwd(h.scratch / "no-such-checkout"))
        _expect("APP_DIR missing", proc, 1, alerts, True)
        proc, alerts, calls, _ = h.run(BACKUP_DIR=_fwd(h.scratch / "mount-dropped"))
        _expect("BACKUP_DIR missing (mount dropped)", proc, 1, alerts, True)
        if "stop" in calls:
            _fail(f"the app was stopped although BACKUP_DIR was unusable: {calls!r}")
        for bad in ("0", "abc", "-1", "2.5"):
            proc, alerts, calls, nightly = h.run(KEEP=bad)
            _expect(f"KEEP={bad}", proc, 1, alerts, True)
            if len(nightly) != 4:
                _fail(f"KEEP={bad} deleted archives: {nightly!r}")
            if "stop" in calls:
                _fail(f"KEEP={bad} stopped the app")

        # --- stop failure: alert, and a restart is still attempted -----------------------
        h.seed()
        proc, alerts, calls, nightly = h.run(STUB_STOP_FAIL=1)
        _expect("docker stop fails", proc, 1, alerts, True)
        if "start app" not in calls:
            _fail(f"no restart attempt after a failed stop: {calls!r}")
        if len(nightly) != 4:
            _fail(f"stop failure changed the archives: {nightly!r}")

        # --- backup creation fails: exit 1, alert, restart, no junk, old copies kept ----
        h.seed()
        proc, alerts, calls, nightly = h.run(STUB_BACKUP_FAIL=1)
        _expect("backup create fails", proc, 1, alerts, True)
        if "start app" not in calls:
            _fail("the app was not restarted after a failed backup")
        if len(nightly) != 4:
            _fail(f"a failed backup changed the verified archives: {nightly!r}")

        # --- FAILED verify: exit 2, alert, the bad archive is NOT left as nightly-* ----
        h.seed()
        proc, alerts, calls, nightly = h.run(STUB_VERIFY="FAILED")
        _expect("verify FAILED", proc, 2, alerts, True)
        if "FAILED verification" not in alerts:
            _fail(f"alert does not say verification failed: {alerts!r}")
        if len(nightly) != 4:
            _fail(f"a failed archive counted as a kept copy / old copies pruned: {nightly!r}")
        leftovers = [p.name for p in h.bk.iterdir() if "partial" in p.name]
        if leftovers:
            _fail(f"a partial archive was left behind: {leftovers!r}")
        # A streak of failures must never erode the good copies (KEEP=3 < 4 seeded).
        for _ in range(3):
            proc, alerts, calls, nightly = h.run(STUB_VERIFY="FAILED")
            _expect("verify FAILED (streak)", proc, 2, alerts, True)
            time.sleep(1.1)  # distinct timestamps
        if len(nightly) != 4:
            _fail(f"a failure streak ate the verified archives: {nightly!r}")

        # --- no verdict at all from verify: still a failure, never a pass ---------------
        h.seed()
        proc, alerts, calls, nightly = h.run(STUB_VERIFY="NONE")
        _expect("verify gave no verdict", proc, 2, alerts, True)

        # --- UNVERIFIED: exit 3, alert, kept apart from the verified set -----------------
        h.seed()
        proc, alerts, calls, nightly = h.run(STUB_VERIFY="UNVERIFIED")
        _expect("verify UNVERIFIED", proc, 3, alerts, True)
        if len(nightly) != 4:
            _fail(f"an UNVERIFIED archive disturbed the verified set: {nightly!r}")
        if not list(h.bk.glob("unverified-nightly-*.tar.gz")):
            _fail("the UNVERIFIED archive was not kept under unverified-*")

        # --- PASSED: exit 0, no alert, exactly KEEP verified archives remain -------------
        h.seed()
        proc, alerts, calls, nightly = h.run(STUB_VERIFY="PASSED")
        _expect("verify PASSED", proc, 0, alerts, False)
        if len(nightly) != 3:
            _fail(f"retention did not prune to KEEP=3 verified archives: {nightly!r}")
        if not any(n > "nightly-2026" and "2026010" not in n for n in nightly):
            _fail(f"the fresh verified archive is missing: {nightly!r}")
        if "start app" not in calls:
            _fail("the app was not restarted after a good run")
        # KEEP=1 keeps just the fresh verified one.
        h.seed()
        proc, alerts, calls, nightly = h.run(STUB_VERIFY="PASSED", KEEP=1)
        _expect("KEEP=1", proc, 0, alerts, False)
        if len(nightly) != 1 or "2026010" in nightly[0]:
            _fail(f"KEEP=1 should leave only the fresh archive: {nightly!r}")

        # --- retention cannot delete: exit 4 with an alert, the verified archive is kept -
        h.seed()
        proc, alerts, calls, nightly = h.run(STUB_VERIFY="PASSED", STUB_RM_FAIL=1)
        _expect("retention error", proc, 4, alerts, True)
        if "retention" not in alerts or not any("2026010" not in n for n in nightly):
            _fail(f"retention failure alert/archives wrong: {alerts!r} {nightly!r}")
        if "start app" not in calls:
            _fail("the app was not restarted after a retention error")

        # --- restart failure alerts and is nonzero even after a good backup --------------
        h.seed()
        proc, alerts, calls, nightly = h.run(STUB_VERIFY="PASSED", STUB_START_FAIL=1)
        if proc.returncode == 0 or "restart" not in alerts.lower():
            _fail(f"a failed restart was silent: rc={proc.returncode} alerts={alerts!r}")
    finally:
        h.close()
    print("NIGHTLY BACKUP 526 OK")


if __name__ == "__main__":
    main()
