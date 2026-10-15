"""tests/_bootstrap.init_env cleanup — proves the per-suite temp DATA_DIR is
removed at process exit instead of accumulating in %TEMP%.

THE LEAK THIS PINS ( 2026-07-26): every verify_*.py subprocess called
tempfile.mkdtemp() and never cleaned up (~180 dirs/run); 45,445 stale loctms_*
dirs piled up in %TEMP% and slowed suites from ~5s to ~60s each. init_env now
registers an atexit cleanup; this asserts it actually fires.

The check runs a CHILD interpreter that calls init_env, imports app.db (the
Windows open-handle case that made naive rmtree fail), lets it exit normally,
then asserts the dir is GONE from the parent. A second child skips the fix by
un-registering atexit to show the probe would FAIL without it.

Hermetic: the child gets its own mkdtemp via init_env; ASCII-only prints.
"""

import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)


# A child that bootstraps, touches the DB (opens a SQLite handle — the case that
# broke naive rmtree on Windows), and exits normally. It prints its temp dir so
# the parent can check the dir is gone AFTER the child exits.
_CHILD = r"""
import os, sys
_ROOT = {root!r}
os.chdir(_ROOT); sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
tmp = init_env("loctms_cleanupprobe_")
print("TMPDIR=" + tmp)
# Import app.db + create the schema so a live SQLite handle exists at exit — the
# exact condition _cleanup disposes the engine to survive on Windows.
from app.db import init_db
init_db()
{sabotage}
sys.stdout.flush()
"""


def _run_child(*, sabotage: str) -> str:
    src = _CHILD.format(root=_ROOT, sabotage=sabotage)
    out = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=_ROOT)
    assert out.returncode == 0, ("child failed", out.returncode, out.stderr[-800:])
    tmp = None
    for line in out.stdout.splitlines():
        if line.startswith("TMPDIR="):
            tmp = line[len("TMPDIR=") :].strip()
    assert tmp, ("child did not report its tempdir", out.stdout[-400:])
    return tmp


def test_tempdir_removed_at_exit():
    tmp = _run_child(sabotage="")
    assert not os.path.exists(tmp), (
        f"the suite temp DATA_DIR must be removed at process exit, not leaked: {tmp}"
    )
    print(
        "  (A) init_env's temp DATA_DIR is gone after the child exits "
        "(atexit cleanup fired, even with a live SQLite handle) OK"
    )


def test_probe_would_fail_without_the_fix():
    # Un-register the atexit cleanup in the child: the dir must then SURVIVE,
    # proving the assertion above is real and not vacuous.
    tmp = _run_child(
        sabotage="import atexit\natexit.unregister(sys.modules['tests._bootstrap']._cleanup)\n"
    )
    survived = os.path.exists(tmp)
    if survived:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)  # don't leak from our own probe
    assert survived, (
        "with cleanup un-registered the dir must survive — else the fix isn't what removes it"
    )
    print(
        "  (B) with the atexit cleanup un-registered the dir SURVIVES, so (A) "
        "proves the fix, not luck OK"
    )


def test_no_stale_probe_dirs_left_behind():
    # Belt-and-braces: our own two children must not have left anything under our
    # unique prefix.
    leftovers = [
        d for d in os.listdir(tempfile.gettempdir()) if d.startswith("loctms_cleanupprobe_")
    ]
    assert not leftovers, ("this suite leaked its own probe dirs", leftovers[:5])
    print("  (C) no loctms_cleanupprobe_ dirs left behind by this suite OK")


def main():
    test_tempdir_removed_at_exit()
    test_probe_would_fail_without_the_fix()
    test_no_stale_probe_dirs_left_behind()
    print("BOOTSTRAP CLEANUP OK")


if __name__ == "__main__":
    main()
