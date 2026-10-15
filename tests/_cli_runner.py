"""Shared in-process CLI runner for the verify_cli* suites.

``app.cli.__main__.main(argv)`` prints to stdout/stderr and raises SystemExit
on argparse usage errors instead of returning a code. run_cli() captures both
streams and normalizes SystemExit into the same (code, stdout, stderr) shape
main() returns on a clean exit, so callers never branch on how the CLI failed.

Previously pasted near-identically in verify_cli.py, verify_cli_polish.py and
verify_cli_visibility.py.
"""

import io
from contextlib import redirect_stderr, redirect_stdout

from app.cli.__main__ import main


def run_cli(argv):
    """Run the CLI in-process, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:  # argparse usage errors raise SystemExit
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()
