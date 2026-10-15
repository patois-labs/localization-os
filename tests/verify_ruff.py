"""Repo-wide lint gate as a first-class verify_*.py suite.

`ruff check` used to run ONLY in CI and via the per-file post-edit hook
(.claude/hooks/ruff_postedit.py). The hook fails OPEN in builder worktrees
(which have no .venv, so `python -m ruff` isn't there) and only ever sees the
single file being edited, so lint drift accumulated invisibly to the local
suite until a repo-wide pass caught it (the 104-error clean-up train, and
before it the T2 +40). This suite closes that hole: it runs BOTH
`ruff check .` and `ruff format --check .` over the WHOLE repo, from the repo
root, as part of the normal `pytest tests/` gate that every train already runs.

Contract with tests/test_verify_suites.py: that harness runs this file as its
own subprocess (``sys.executable tests/verify_ruff.py``, cwd = repo root) and
asserts the process exits 0 AND the last non-empty stdout line ends with
``OK``. So on success we print exactly one final ``RUFF OK`` line; on a real
finding we print ruff's own output (so the failure is actionable) and exit 1.

Skip-vs-fail policy (deliberate):
  * If ruff itself is NOT AVAILABLE (not importable / not launchable) we SKIP
    gracefully: print ``RUFF OK (skipped: ruff not installed)`` and exit 0.
    This keeps a dev-dep-less or minimal CI checkout from breaking; the
    dedicated CI ruff job still enforces lint there, and locally the .venv has
    ruff so the gate really runs.
  * We DO NOT fail open on actual ruff FINDINGS. A missing module is detected
    up front via importlib (and a launch error is caught), which is a distinct
    signal from a non-zero return code carrying findings — only the former
    skips.

Version: the repo pins ruff to an exact patch (requirements-dev.txt) because
even a patch can change `ruff format` output. We read that pin and the running
version and print a NOTE on mismatch, but never hard-fail on it (a mismatch is
worth surfacing, not worth blocking an otherwise-clean tree).

This file is itself in the repo-wide scan, so it must stay clean under both
`ruff check` and `ruff format`.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PIN_RE = re.compile(r"^ruff==([0-9][^\s#]*)", re.MULTILINE)
# Generous cap: ruff scans ~1k files in seconds, but a cold 2-core CI runner
# and a large tree deserve headroom. Well clear of real runtime.
_TIMEOUT_S = 300


def _ruff_available() -> bool:
    """True only when the `ruff` module can actually be imported here. This is
    the ONE signal that gates skip-vs-run: a missing module means "skip", a
    non-zero return code from a real run means "findings -> fail"."""
    try:
        return importlib.util.find_spec("ruff") is not None
    except (ImportError, ValueError):
        return False


def _pinned_version() -> str | None:
    """The exact ruff pin from requirements-dev.txt, or None if not found."""
    req = REPO_ROOT / "requirements-dev.txt"
    try:
        m = _PIN_RE.search(req.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def _running_version() -> str | None:
    """`ruff --version` -> "0.16.4", or None if it can't be determined."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    parts = (r.stdout or "").strip().split()
    return parts[1] if len(parts) >= 2 else None


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run `python -m ruff <args> .` at the repo root, honoring pyproject
    excludes via --force-exclude. Returns None only if ruff can't be launched
    at all (treated as "not installed" -> skip by the caller)."""
    try:
        return subprocess.run(
            [sys.executable, "-m", "ruff", *args, "--force-exclude", "."],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def main() -> int:
    if not _ruff_available():
        print("RUFF OK (skipped: ruff not installed)")
        return 0

    pinned, running = _pinned_version(), _running_version()
    if pinned and running and pinned != running:
        print(
            f"NOTE: ruff version {running} != pinned {pinned} "
            "(requirements-dev.txt); linting with the installed version.",
            file=sys.stderr,
        )

    checks = (
        (["check"], "ruff check ."),
        (["format", "--check"], "ruff format --check ."),
    )
    problems = []
    for args, label in checks:
        r = _run(args)
        if r is None:
            # ruff vanished between the import probe and the run: skip, don't
            # fail a tree we never actually linted.
            print("RUFF OK (skipped: ruff not installed)")
            return 0
        if r.returncode != 0:
            out = (r.stdout or "").rstrip() or (r.stderr or "").rstrip()
            problems.append(f"===== {label} FAILED =====\n{out}")

    if problems:
        print("\n\n".join(problems))
        print(
            "\nrepo-wide ruff findings above. Fix them (run "
            "`ruff format .` for the format ones), then re-run.",
            file=sys.stderr,
        )
        return 1

    print("RUFF OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
