"""`engine create|update --secret api_key=-` reads the value from stdin (never stores "-").

docs/CLI.md already shows `--secret api_key=-` for connectors and vendors; the engine verbs
stored the literal "-" as the key. Now "-" reads one line from stdin on create AND update; a
normal value is unchanged; the key never appears in output.

Hermetic. ASCII-only. Final line "CLI ENGINE SECRET STDIN OK".
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_engsecstdin_")

from app.db import init_db
from app.services import engines

init_db()
K1 = "stdin-key-AAAA1111"
K2 = "stdin-key-BBBB2222"
K3 = "argv-key-CCCC3333"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _run(argv, stdin_text=""):
    from app.cli.__main__ import main

    real_in = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
    finally:
        sys.stdin = real_in
    return code, out.getvalue() + err.getvalue()


def main():
    code, text = _run(
        [
            "engine",
            "create",
            "--name",
            "stdin-eng",
            "--kind",
            "openai_compatible",
            "--base-url",
            "https://api.example.com/v1",
            "--model",
            "m1",
            "--secret",
            "api_key=-",
        ],
        K1 + "\n",
    )
    if code != 0:
        _fail(f"create exit {code}: {text[:300]}")
    prof = next(p for p in engines.list_profiles() if p.name == "stdin-eng")
    got = engines.dec_key(prof.api_key_enc)
    if got != K1:
        _fail(f"create must read the key from stdin, stored {got!r}")
    if K1 in text:
        _fail("the key must never be echoed")

    code, text = _run(["engine", "update", str(prof.id), "--secret", "api_key=-"], K2 + "\r\n")
    if code != 0:
        _fail(f"update exit {code}: {text[:300]}")
    got = engines.dec_key(engines.get_profile(prof.id).api_key_enc)
    if got != K2:
        _fail(f"update must read the key from stdin, stored {got!r}")
    if K2 in text:
        _fail("the key must never be echoed (update)")

    code, _t = _run(["engine", "update", str(prof.id), "--secret", f"api_key={K3}"])
    got = engines.dec_key(engines.get_profile(prof.id).api_key_enc)
    if code != 0 or got != K3:
        _fail(f"a plain value must still work: {code} {got!r}")
    # gate: an EMPTY stdin line (a pipe from an unset variable) is a usage error, never a
    # silent no-op that leaves the old key in place
    code, text = _run(["engine", "update", str(prof.id), "--secret", "api_key=-"], "\n")
    got = engines.dec_key(engines.get_profile(prof.id).api_key_enc)
    if code != 2 or "empty" not in text or got != K3:
        _fail(f"an empty stdin key must be refused (exit 2): {code} {text[:200]!r}")
    print("CLI ENGINE SECRET STDIN OK")


if __name__ == "__main__":
    main()
