"""Ratchet every real argparse leaf and its flags against docs/CLI.md.

This is a text check, not a Markdown parser: a command owns the paragraphs or
individual table rows containing its literal backticked path. Shared flag tables
are included only through an explicit link in that command's text. A mention in
another command's row cannot supply flags. No command groups are exempt.
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from tests._bootstrap import init_env

init_env("loctms_cli_documentation_")

from app.cli.__main__ import build_parser

# Enumerated exemptions: argparse injects these two equivalent help switches on
# every leaf. Their meaning is invariant and the CLI's opening help examples
# document it; repeating them in every command row would obscure useful flags.
FLAG_EXEMPTIONS = {
    "-h": "argparse's built-in help alias, identical on every command",
    "--help": "argparse's built-in help, illustrated in the opening CLI examples",
}
COMMAND_EXEMPTIONS: dict[str, str] = {}

# Exact, opt-in shared helpers. A command must link to the anchor to use it.
# The text between these headings is checked, not a hardcoded list of flags.
SHARED_HELPERS = {
    "workflow-step-configuration-flags": (
        "#### Workflow step configuration flags",
        "Example - build a workflow, run it, clear a human-review pause:",
    ),
    "translation-memory-filters": (
        "#### Translation memory filters",
        "**Glossary**",
    ),
}


def leaf_parsers(parser, path=""):
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        yield path, parser
    for action in subs:
        for name, child in action.choices.items():
            yield from leaf_parsers(child, f"{path} {name}".strip())


def documentation_failures(parser, text):
    # Blank-line paragraphs; table rows are independent command sections.
    blocks = []
    for paragraph in re.split(r"\n\s*\n", text):
        blocks.extend(paragraph.splitlines() if paragraph.startswith("|") else [paragraph])
    leaves = dict(leaf_parsers(parser))
    failures = []
    for command in COMMAND_EXEMPTIONS:
        assert command in leaves, f"stale command exemption: {command}"
    for command, leaf in sorted(leaves.items()):
        if command in COMMAND_EXEMPTIONS:
            continue
        pattern = re.compile(r"`" + re.escape(command) + r"(?=[ `])")
        owned = [block for block in blocks if pattern.search(block)]
        # A table row belongs to its first command, not commands cross-referenced
        # in the explanatory cell. Shared helpers require a deliberate link.
        owned = [
            block
            for block in owned
            if not block.startswith("|") or pattern.match(block.lstrip("| "))
        ]
        if not owned:
            failures.append(f"{command}: missing command")
            continue
        section = "\n".join(owned)
        for anchor, (start, end) in SHARED_HELPERS.items():
            if f"](#{anchor})" in section:
                assert start in text and end in text, f"missing shared helper: {anchor}"
                section += "\n" + text.split(start, 1)[1].split(end, 1)[0]
        flags = {flag for action in leaf._actions for flag in action.option_strings}
        missing = sorted(
            flag
            for flag in flags - FLAG_EXEMPTIONS.keys()
            if not re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", section)
        )
        if missing:
            failures.append(f"{command}: missing flags {' '.join(missing)}")
    return len(leaves), failures


def check_ratchet_contract():
    """Cheap adversarial checks: coverage must belong to the exact leaf/flag."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    first = sub.add_parser("first")
    first.add_argument("--mode")
    second = sub.add_parser("second")
    second.add_argument("--mode-extra")
    doc = "| `first` | Missing option. |\n| `second --mode-extra X` | Also mentions `first`. |"
    assert documentation_failures(parser, doc)[1] == ["first: missing flags --mode"]
    assert documentation_failures(parser, "| `firstly --mode X` | |\n" + doc)[1] == [
        "first: missing flags --mode"
    ]
    assert documentation_failures(parser, "| `second --mode-extra X` | |")[1] == [
        "first: missing command"
    ]
    anchor, (start, end) = next(iter(SHARED_HELPERS.items()))
    helper = f"\n\n{start}\n\n| `--mode X` | Shared option. |\n\n{end}\n"
    assert documentation_failures(parser, doc + helper)[1] == ["first: missing flags --mode"]
    linked = doc.replace("Missing option.", f"See [shared flags](#{anchor}).")
    assert not documentation_failures(parser, linked + helper)[1]
    assert documentation_failures(parser, linked + helper.replace("--mode X", "--gone X"))[1]
    first.add_argument("--new-option")
    assert documentation_failures(parser, linked + helper)[1] == [
        "first: missing flags --new-option"
    ]
    sub.add_parser("third")
    assert "third: missing command" in documentation_failures(parser, linked + helper)[1]


if __name__ == "__main__":
    check_ratchet_contract()
    count, failures = documentation_failures(
        build_parser(), (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    )
    if failures:
        # One line keeps the harness's 40-line failure tail complete even when
        # an audit finds many gaps; each entry names the exact command and flags.
        print("Documentation gaps: " + "; ".join(failures))
        print(f"verify_cli_documentation FAILED ({len(failures)} gaps, {count} leaves)")
        raise SystemExit(1)
    print(f"verify_cli_documentation: {count} leaves and all registered flags OK")
