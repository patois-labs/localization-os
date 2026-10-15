"""Claude Code PostToolUse hook: ruff-check every edited/written Python file.

Findings surface at edit time (exit 2 feeds them back to the agent) instead of
accumulating for the next repo-wide pass. CI runs the repo-wide check; this hook
keeps individual edits clean between CI runs.

Reads the hook payload from stdin, ignores non-Python paths and ruff's own
excludes (--force-exclude honours pyproject's extend-exclude, e.g. alembic/).
Fails OPEN (exit 0) when ruff isn't installed or the payload is malformed —
the hook must never block editing in an environment without dev deps.
"""

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not file_path.endswith(".py"):
        return 0
    p = Path(file_path)
    if not p.is_file():
        return 0

    problems = []
    for args, label in (
        (["check", "--force-exclude", "--quiet"], "ruff check"),
        (["format", "--check", "--force-exclude", "--quiet"], "ruff format"),
    ):
        try:
            r = subprocess.run(  # noqa: S603 -- fixed argv: this venv's ruff on one path
                [sys.executable, "-m", "ruff", *args, str(p)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:
            return 0  # ruff missing/broken here: fail open, CI still enforces
        if r.returncode != 0:
            out = (r.stdout or "").strip() or (r.stderr or "").strip()
            problems.append(f"[{label}] {out}" if out else f"[{label}] failed")

    if problems:
        print(
            "\n".join(problems) + "\nFix the ruff findings above in the file you just edited "
            "(run `ruff format <file>` for the format ones).",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
