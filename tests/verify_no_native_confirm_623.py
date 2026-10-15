"""Issue #623 ratchet: no page or script under app/templates or app/static may call the native
confirm() dialog. It freezes the tab and automation and is not reliably exposed to screen
readers; every question is asked in the page with window.LocAsk (app/static/handlers.js),
which fails closed. The allowlist is empty on purpose.

What counts as a call: `window.confirm(`, `self.confirm(`, `globalThis.confirm(`,
`top.confirm(`, `parent.confirm(`, `window['confirm'](` and a bare `confirm(` that has an
argument (comments that talk about "confirm()" with nothing inside the parentheses are
prose, not calls). `data-action-confirm`, `LocAsk`, `askInPage`, `*Confirm(` and `.confirm(`
on other objects are not matches.

Pure file scan, no server. Run ONLY through tests/test_verify_suites.py. ASCII-only.
Final line: "NO NATIVE CONFIRM 623 OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Files allowed to contain a native confirm call. Empty: do not add to it, convert the call.
ALLOW: tuple = ()

_ROOTS = ("app/templates", "app/static")
_EXT = (".html", ".js", ".mjs", ".jinja", ".jinja2", ".htm", ".txt")

_BAD = re.compile(
    r"""(?x)
    (?:\b(?:window|self|globalThis|top|parent)\s*\.confirm\(      # window.confirm(
      |\b(?:window|self|globalThis|top|parent)\s*\[\s*['"]confirm['"]\s*\]\(
      |(?<![\w.$-])confirm\([^\s)]                                 # bare confirm(arg
    )"""
)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def scan(text: str) -> list:
    """Return the 1-based line numbers of native confirm calls in `text`."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if _BAD.search(line):
            hits.append(n)
    return hits


def _self_check():
    must_hit = (
        "if (!window.confirm('x')) {",
        "if (!confirm('Replace paragraph ' + i)) return;",
        "if (confirm(res.data.message)) {",
        "globalThis.confirm(msg)",
        "window['confirm'](msg)",
        "x = window.confirm(m)",
    )
    must_pass = (
        "// the delete confirm() reads the name",
        "// confirm() prompt is a genuine new send",
        'data-action-confirm="Delete?"',
        "window.LocAsk(msg).then(function (ok) {})",
        "obj.confirm(thing)",
        "function askConfirm(msg) {}",
        "var needsConfirm = true;",
        "window.confirm is never called",
        "window.confirm (which freezes the tab)",
        "confirm (fail-closed). #}",
    )
    for s in must_hit:
        if not scan(s):
            _fail(f"ratchet regex misses a native confirm call: {s!r}")
    for s in must_pass:
        if scan(s):
            _fail(f"ratchet regex flags harmless text: {s!r}")


def main():
    _self_check()
    seen = 0
    found = []
    for base in _ROOTS:
        for dirpath, _dirs, files in os.walk(os.path.join(_ROOT, base)):
            for name in files:
                if not name.endswith(_EXT):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
                if rel in ALLOW:
                    continue
                seen += 1
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n in scan(fh.read()):
                        found.append(f"{rel}:{n}")
    if seen < 50:
        _fail(f"scanned only {seen} files: the walk is broken")
    if found:
        _fail(
            "native confirm() is back (use window.LocAsk, which fails closed): " + ", ".join(found)
        )
    print(f"scanned {seen} template/static files; no native confirm calls")
    print("NO NATIVE CONFIRM 623 OK")


if __name__ == "__main__":
    main()
