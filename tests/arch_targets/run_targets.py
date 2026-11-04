"""Run every backend architecture TARGET test, one subprocess each, and print a summary table.

    .venv\\Scripts\\python.exe tests\\arch_targets\\run_targets.py            # all targets
    .venv\\Scripts\\python.exe tests\\arch_targets\\run_targets.py t1 t5      # some

Expected today: every target FAILS (they describe work not done yet). A target that passes is done;
promote it to a tests/verify_*.py suite in the same change that makes it pass, so the release gate
keeps it green.
"""

import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def main(argv):
    files = sorted(glob.glob(os.path.join(HERE, "target_t*.py")))
    if argv:
        files = [
            f for f in files if any(os.path.basename(f).startswith(f"target_{a}") for a in argv)
        ]
    rows = []
    for f in files:
        t0 = time.time()
        p = subprocess.run(
            [sys.executable, f],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
        verdict = next(
            (ln for ln in reversed(lines) if ln.startswith("TARGET ")), "TARGET ? crashed"
        )
        rows.append((os.path.basename(f), verdict, round(time.time() - t0)))
        print(f"\n== {os.path.basename(f)} ({rows[-1][2]}s)")
        print("\n".join(ln for ln in lines if ln.lstrip().startswith("[")))
        if p.returncode not in (0, 1):
            print((p.stderr or "")[-1500:])
    print("\nSUMMARY")
    for _name, verdict, secs in rows:
        print(f"  {verdict}  ({secs}s)")
    return 0 if all(v.endswith("PASS") for _, v, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
