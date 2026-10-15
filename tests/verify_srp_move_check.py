"""Owner fixtures for the move checker; no Git writes or commits required."""

import contextlib
import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("srp_move_check", ROOT / "scripts/srp_move_check.py")
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)

OLD = "app/cli/old.py"
NEW = "app/cli/new.py"
BODY = "def value():\n    # Keep this explanation.\n    return 1\n"
FAILURES = []


def check_case(after_old, after_new, expected, finding, *, before=BODY, extra=None, flags=()):
    sources = {f"base:{OLD}": before, f"head:{OLD}": after_old, f"head:{NEW}": after_new}
    extra = extra or {}
    sources.update({f"head:{path}": source for path, source in extra.items()})

    def git_show(command, **kwargs):
        assert command[:2] == ["git", "show"], command
        assert kwargs["encoding"] == "utf-8"
        source = sources.get(command[2])
        return subprocess.CompletedProcess(command, 0 if source is not None else 1, source, "")

    output = io.StringIO()
    with (
        patch.object(sys, "argv", ["srp_move_check.py", "base", "head", OLD, NEW, *extra, *flags]),
        patch.object(CHECKER.subprocess, "run", side_effect=git_show),
        contextlib.redirect_stdout(output),
    ):
        try:
            result = CHECKER.main()
        except SystemExit as exc:
            result = exc.code
    if result != expected or finding not in output.getvalue():
        FAILURES.append(f"Expected {expected}: {finding}\n{output.getvalue()}")


def main():
    check_case("from app.cli.new import value\n", BODY, 0, "PURE MOVE OK")
    check_case("", BODY.replace("return 1", "return 2"), 1, "CHANGED while moved: value")
    check_case("", BODY.replace("    # Keep this explanation.\n", ""), 1, "COMMENTS LOST")
    check_case("__class__ = object\n", BODY, 1, "HOOK")
    check_case(
        "from app.cli.new import value\n",
        "from app.cli.old import other\n" + BODY,
        1,
        "CYCLE",
    )
    added = BODY + "\ndef register_sneaky():\n    return 2\n"
    check_case("", added, 1, "ADDED: register_sneaky")
    check_case("", added, 1, "ADDED: register_sneaky", flags=("--allow", "register"))
    check_case("", added, 1, "ADDED: register_sneaky", flags=("--allow", "register_sneaky"))
    register = "def register():\n    pass\n"
    split = "def register_part():\n    pass\n"
    check_case(
        register,
        split,
        1,
        "ADDED: register_part",
        before=register,
        flags=("--allow-added", "register_part"),
    )
    check_case(
        register,
        split,
        0,
        "PURE MOVE OK",
        before=register,
        flags=("--allow", "register", "--allow-added", "register_part"),
    )
    other = "def other():\n    return 2\n"
    explanation = "# This explains value.\n"
    check_case(
        explanation + other,
        BODY,
        1,
        "COMMENTS MISPLACED: value",
        before=explanation + BODY + other,
    )
    check_case(other, explanation + BODY, 0, "PURE MOVE OK", before=explanation + BODY + other)
    check_case(
        "from app.cli.new import value\n",
        "from app.cli.third import other\n" + BODY,
        1,
        "CYCLE",
        extra={"app/cli/third.py": "from app.cli.old import value\n"},
    )
    for changed in ("", "LIMIT = 2\n", "LIMIT = 1\nEXTRA = 2\n"):
        check_case("", changed + BODY, 1, "MODULE STATEMENTS", before="LIMIT = 1\n" + BODY)
    check_case(
        "__all__ = ['value']\n",
        "LIMIT = 1\n" + BODY,
        0,
        "PURE MOVE OK",
        before="LIMIT = 1\n" + BODY,
    )
    assert not FAILURES, "\n".join(FAILURES)
    print("SRP MOVE CHECK OK")


if __name__ == "__main__":
    main()
