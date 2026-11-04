"""Output of the leverage report (`project leverage`, `job leverage`), local and remote.

One shape for both arms: the report dict app.services.leverage.analyze returns (or the same
JSON over /api). `--csv` prints the CSV for quoting (scope, language, band, segments, words);
`-o/--out FILE` writes it (refusing an existing file without `--force`, like `audit export`);
`--json` prints the report; otherwise an ASCII table.

Documented in: docs/architecture/tm-and-glossary.md
"""

import os
import sys

from app.cli._common import EXIT_USAGE, CliError


def add_flags(p) -> None:
    """The export flags shared by `project leverage` and `job leverage`."""
    p.add_argument(
        "--csv",
        action="store_true",
        help="print the report as CSV for quoting (scope, language, band, segments, words).",
    )
    p.add_argument("-o", "--out", default="", help="write the CSV to FILE instead of stdout.")
    p.add_argument("--force", action="store_true", help="overwrite -o FILE if it exists.")


def wants_csv(args) -> bool:
    return bool(getattr(args, "csv", False) or getattr(args, "out", ""))


def deliver_csv(args, emit, text: str) -> int:
    out = getattr(args, "out", "") or ""
    if not out:
        sys.stdout.write(text)
        return 0
    if os.path.exists(out) and not getattr(args, "force", False):
        raise CliError(f"destination exists: {out} (pass --force to overwrite).", EXIT_USAGE)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    emit.result(f"Wrote leverage CSV -> {out}", {"ok": True, "out": out})
    return 0


def deliver_report(emit, report: dict) -> int:
    """The report as JSON (--json) or an ASCII table: one row per language and band, a total
    row closing each language and an `all` block at the end."""
    from app.kernel import leverage

    if emit.as_json:
        emit.json({"ok": True, **report})
        return 0
    scope = (
        f"Job #{report['job_id']} (project #{report['project_id']})"
        if report.get("job_id") is not None
        else f"Project #{report['project_id']}"
    )
    emit.say(f"{scope} - word leverage (101 % in-context / repetitions / 100 % exact / new)")
    emit.table(leverage.table_text(report), headers=("language", "band", "segments", "words"))
    return 0
