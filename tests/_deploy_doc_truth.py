"""Parse CLI invocations in fenced operator examples without executing commands."""

import contextlib
import io
import re
import shlex
from pathlib import Path


def _parse_doc_command(parser, argv, rel, line):
    """Parse argv with `parser`, but turn argparse's SystemExit (an unknown subcommand or
    flag in a documented example) into an AssertionError that NAMES the offending doc and
    line (LOW-4). Without this the failure surfaces as bare argparse usage text with no
    pointer to the fenced command that is wrong."""
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            return parser.parse_args(argv)
        except SystemExit:
            raise AssertionError((rel, line)) from None


def check_deploy_commands():
    from app.cli.__main__ import build_parser

    root = Path(__file__).resolve().parents[1]
    parser = build_parser()
    seen = []
    from alembic.config import CommandLine

    alembic_parser = CommandLine().parser
    alembic_seen = []
    for rel in ("docs/DEPLOY.md", "docs/architecture/deployment.md"):
        text = (root / rel).read_text(encoding="utf-8")
        for fence in re.findall(r"```[^\n]*\n(.*?)```", text, re.S):
            for line in fence.replace("\\\n", " ").splitlines():
                match = re.search(
                    r"(?:\bloc-tms|\bpython(?:3)? -m app\.cli|"
                    r"--entrypoint python app -m app\.cli)\s+(.+)",
                    line,
                )
                if not match:
                    migration = re.search(r"(?:--entrypoint alembic app|\balembic)\s+(.+)", line)
                    if migration:
                        argv = shlex.split(migration[1], comments=True)
                        parsed = _parse_doc_command(alembic_parser, argv, rel, line)
                        assert getattr(parsed, "cmd", None), line
                        alembic_seen.append(argv[0])
                    continue
                argv = shlex.split(match[1], comments=True)
                assert argv, (rel, line)
                args = _parse_doc_command(parser, argv, rel, line)
                assert callable(getattr(args, "func", None)), (rel, line)
                seen.append(argv)
    assert {"heads", "current", "upgrade"} <= set(alembic_seen), alembic_seen
    assert any(a[:2] == ["db", "backup-full"] for a in seen), seen
    assert any(a[:2] == ["release", "manifest"] for a in seen), seen
    # LOW-4: prove the ACTUAL doc pipeline (via _parse_doc_command) rejects an invented
    # subcommand or flag AND surfaces the offending (rel, line), not bare argparse usage.
    for bad in (["db", "backup-full", "x", "--imaginary"], ["db", "imaginary"]):
        try:
            _parse_doc_command(parser, bad, "docs/DEPLOY.md", "loc-tms " + " ".join(bad))
        except AssertionError as exc:
            assert exc.args[0] == ("docs/DEPLOY.md", "loc-tms " + " ".join(bad)), exc.args
        else:
            raise AssertionError(bad)
    return len(seen)
