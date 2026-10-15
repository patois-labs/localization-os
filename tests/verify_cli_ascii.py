"""ASCII-safe CLI chrome guard (issue #15) - suite.

Four standing checks that keep the mojibake class from regressing:

  1. HELP-TREE ASCII: walk the ENTIRE argparse tree (root + every subparser at every
     depth) and assert each parser's rendered --help is pure ASCII. argparse's own
     --help printer writes straight to sys.stdout, BYPASSING the _write_ascii_safe
     wrapper, so a stray em-dash/ellipsis/arrow in any help= or description= would
     mojibake on a legacy console. This walker is the enforcement for that whole
     surface (help text is program CHROME -> must be pure ASCII).

  2. CLI LITERAL SCAN: AST-scan every app/cli/*.py module for the forbidden
     typographic set in NON-docstring string literals (docstrings are house style and
     exempt; comments are not string literals). Catches chrome strings that never make
     it into a --help page (emit.say/result/CliError bodies, table footers).

  3. CLIERROR-BODY SCAN (gate C round 2, L1): follow ONE shape of CLI chrome back into the
     service module that builds it. Check 2 only walks app/cli/*.py, so a refusal string
     authored in a service and raised verbatim as `CliError(f"export refused: {reason}")`
     was invisible to the guard - which is exactly how a U+2014 em dash reached a `doc
     export` error body. This check resolves a `CliError(...)` argument that is a DIRECT
     SERVICE CALL - `reason = svc.f(...)` then `CliError(f"...{reason}")`, or the inline
     `CliError(f"...{svc.f(...)}")` - back to `<module>.<func>()` (following the
     `app.kernel` alias shims to the real module), walks that function and every
     same-module helper it calls, and scans their non-docstring literals. Mechanical, so
     it does not rot the way a curated list of "chrome-producing services" would.

     WHAT IT DOES NOT COVER (round 3, L4 - stated so the docstring does not overclaim):
     an EXCEPTION BINDING carrying service-authored prose (`except VendorError as exc:
     raise CliError(f"...{exc}")`) is the larger sibling shape and is NOT resolved -
     59 such sites exist in app/cli/*.py today (measured); nor are `.format(...)` / `+`
     argument shapes (neither is in the tree today) or `import x.y as z` module aliases
     (`_module_aliases` walks `ImportFrom` only). Those bodies are still covered wherever
     the authoring module is reached another way, and their literals are house-reviewed;
     widening the resolver to exception bindings is the natural next step for this check.

  4. UTF-8 DATA ROUNDTRIP: prove layer (a) of the fix - _force_utf8_streams() makes a
     Windows pipe (which reports cp1252) emit REAL UTF-8 bytes for user DATA, so an
     accented name renders correctly instead of as a lone cp1252 byte (mojibake). Run
     in a subprocess because only a real OS pipe exercises the encoding path (an
     in-process StringIO capture accepts arbitrary unicode and proves nothing).

The contract this encodes (issue #15):
  * user DATA (names, terms, footers) renders as real UTF-8, via the startup
    reconfigure - checked by (4);
  * program CHROME (help, labels, error messages) is pure ASCII regardless of the
    console code page - enforced by (1), (2) and (3) wherever the bytes were authored.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys at import.
"""

import ast
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

init_env("loctms_ascii_")

import argparse

from app.cli.__main__ import build_parser

# The typographic characters that break legacy Windows consoles. Kept in sync with
# the sweep in scratch tooling / the ASCII-safe rule.
FORBIDDEN = set("–—…→·‘’“”")  # - - ... -> * '' ""

# Documented allowlist: exact string literals permitted to contain a FORBIDDEN char
# despite the scan. EMPTY today (the whole CLI chrome surface is ASCII). To add one,
# put the literal's exact value here with a comment justifying why it must keep the
# non-ASCII char AND why it never reaches a real terminal. Prefer fixing the string.
LITERAL_ALLOWLIST: set[str] = set()


def _iter_parsers(parser, path="loc-tms"):
    """Yield (command_path, parser) for the root and every nested subparser."""
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _iter_parsers(sub, f"{path} {name}")


def check_help_tree_ascii():
    root = build_parser()
    parsers = list(_iter_parsers(root))
    assert len(parsers) > 100, f"expected the full command tree, got {len(parsers)} parsers"
    offenders = []
    for cmd_path, parser in parsers:
        help_text = parser.format_help()
        bad = {c for c in help_text if ord(c) > 127}
        if bad:
            offenders.append((cmd_path, sorted(hex(ord(c)) for c in bad)))
    assert not offenders, (
        "non-ASCII in --help output (program chrome must be pure ASCII):\n"
        + "\n".join(f"  `{p}` -> {b}" for p, b in offenders[:20])
    )
    print(f"  help-tree: {len(parsers)} parsers, all --help pure ASCII OK")


def _docstring_ids(tree):
    ds = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ds.add(id(body[0].value))
    return ds


def check_cli_literals_ascii():
    cli_dir = os.path.join(_ROOT, "app", "cli")
    files = sorted(f for f in os.listdir(cli_dir) if f.endswith(".py"))
    offenders = []
    for fname in files:
        path = os.path.join(cli_dir, fname)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        ds = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in ds
            ):
                # Chrome must be pure ASCII: flag ANY non-ASCII char (broader than the
                # curated FORBIDDEN set, so a stray char like U+2264 the sweep never
                # listed is still caught here, not only by the help-tree walker).
                bad = {c for c in node.value if ord(c) > 127}
                if bad and node.value not in LITERAL_ALLOWLIST:
                    offenders.append((fname, node.lineno, sorted(hex(ord(c)) for c in bad)))
    assert not offenders, (
        "non-ASCII chars in CLI (non-docstring) string literals -- program chrome must "
        "be pure ASCII; replace with ASCII (-- , ->, ...) or add to LITERAL_ALLOWLIST:\n"
        + "\n".join(f"  {f}:{ln} {b}" for f, ln, b in offenders[:30])
    )
    print(f"  literal-scan: {len(files)} app/cli/*.py modules, no forbidden chars OK")


# --- (3) CliError bodies authored in service modules -------------------------------
#
# The blind spot gate C round 2 (L1) named: `app/services/export.py` built a refusal
# string carrying a U+2014 em dash, `cmd_doc.py` raised it verbatim as
# `CliError(f"export refused: {_qgate}")`, and check 2 -- which only walks app/cli/*.py --
# saw nothing. The rule (#15) is about what reaches a terminal, not about which file the
# bytes were typed in.

#: `from app.kernel import export` IS `app.services.export` (the K1 alias facade). The
#: shims are lazy meta_path aliases with no file of their own, so the AST walk resolves
#: them the same way the finder does.
_KERNEL_ALIAS_ROOTS = ("app.services.", "app.")


def _module_file(dotted: str) -> str | None:
    """The .py file backing a dotted module name, following the app.kernel alias shims."""
    cands = [dotted]
    if dotted.startswith("app.kernel.") and dotted.count(".") == 2:
        leaf = dotted.rsplit(".", 1)[-1]
        cands += [root + leaf for root in _KERNEL_ALIAS_ROOTS]
    for d in cands:
        p = os.path.join(_ROOT, *d.split(".")) + ".py"
        if os.path.isfile(p):
            return p
    return None


def _module_aliases(tree) -> dict:
    """{local name -> (dotted, file)} for every `from <pkg> import <module>` in a file."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                dotted = f"{node.module}.{a.name}"
                path = _module_file(dotted)
                if path:
                    out[a.asname or a.name] = (dotted, path)
    return out


def _clierror_sources(tree, aliases) -> set:
    """{(dotted, file, funcname)} -- service functions whose return value is raised as a
    CliError body in this CLI module.

    Two shapes, both real in the tree today: `reason = svc.f(...)` then
    `CliError(f"...{reason}")`, and `CliError(f"...{svc.f(...)}")` inline. The name map is
    module-wide rather than per-function, which can only over-approximate (scan MORE
    functions) -- the safe direction for a guard.

    NOT an exception binding (`except E as exc` -> `CliError(f"...{exc}")`), not a
    `.format(...)` / concatenation argument, and not an `import x.y as z` alias: see the
    module docstring's check-3 note (round 3, L4) for why and what that leaves open."""
    produced = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                mod = aliases.get(fn.value.id)
                if mod:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            produced[t.id] = (mod[0], mod[1], fn.attr)
    hits = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name != "CliError":
            continue
        for arg in node.args:
            parts = [arg]
            if isinstance(arg, ast.JoinedStr):
                parts = [v.value for v in arg.values if isinstance(v, ast.FormattedValue)]
            for part in parts:
                if isinstance(part, ast.Name) and part.id in produced:
                    hits.add(produced[part.id])
                elif isinstance(part, ast.Call):
                    f2 = part.func
                    if isinstance(f2, ast.Attribute) and isinstance(f2.value, ast.Name):
                        mod = aliases.get(f2.value.id)
                        if mod:
                            hits.add((mod[0], mod[1], f2.attr))
    return hits


def _same_module_closure(tree, start: str) -> list:
    """`start` plus every function defined in the SAME module that it (transitively) calls
    -- so a public builder that delegates its wording to a private helper is covered
    (`export.gate_held_reason` -> `_gate_held_reason_for_docs`, where the em dash lived)."""
    defs = {
        n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    seen, stack = set(), [start]
    while stack:
        name = stack.pop()
        if name in seen or name not in defs:
            continue
        seen.add(name)
        for node in ast.walk(defs[name]):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in defs
            ):
                stack.append(node.func.id)
    return [defs[n] for n in seen]


def check_clierror_body_literals_ascii():
    cli_dir = os.path.join(_ROOT, "app", "cli")
    targets: set = set()
    for fname in sorted(f for f in os.listdir(cli_dir) if f.endswith(".py")):
        with open(os.path.join(cli_dir, fname), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        targets |= _clierror_sources(tree, _module_aliases(tree))
    assert targets, (
        "the CliError-body walker resolved NO service functions at all -- the CLI tree "
        "changed shape and this guard has gone inert"
    )
    offenders = []
    for _dotted, path, func in sorted(targets):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        ds = _docstring_ids(tree)
        for fd in _same_module_closure(tree, func):
            for node in ast.walk(fd):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in ds
                ):
                    bad = {c for c in node.value if ord(c) > 127}
                    if bad and node.value not in LITERAL_ALLOWLIST:
                        offenders.append(
                            (
                                os.path.relpath(path, _ROOT).replace("\\", "/"),
                                fd.name,
                                node.lineno,
                                sorted(hex(ord(c)) for c in bad),
                            )
                        )
    assert not offenders, (
        "non-ASCII in a string literal of a service function whose value is raised as a "
        "CliError body -- program chrome must be pure ASCII wherever it is authored:\n"
        + "\n".join(f"  {f}:{ln} in {fn}() {b}" for f, fn, ln, b in offenders[:30])
    )
    print(f"  clierror-bodies: {len(targets)} service functions followed, all ASCII OK")


def check_utf8_data_roundtrip():
    # Only a real OS pipe reports cp1252 on Windows; drive the reconfigure in a child
    # and read RAW bytes. Post-fix stdout must carry the UTF-8 encoding of 'e-acute'
    # (0xC3 0xA9), never the lone cp1252 byte 0xE9 (the mojibake tell).
    code = (
        "import sys;"
        "from app.cli.__main__ import _force_utf8_streams;"
        "_force_utf8_streams();"
        "sys.stdout.write('caf\\u00e9\\n')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, cwd=_ROOT, env=os.environ.copy()
    )
    assert proc.returncode == 0, f"child failed: {proc.stderr!r}"
    assert b"caf\xc3\xa9" in proc.stdout, (
        f"expected real UTF-8 bytes for the accented name, got {proc.stdout!r} "
        "(reconfigure did not take effect)"
    )
    assert b"caf\xe9\n" not in proc.stdout, (
        f"found a lone cp1252 byte 0xE9 -- mojibake regressed: {proc.stdout!r}"
    )
    print("  utf-8 roundtrip: child pipe emits real UTF-8 for DATA OK")


def check_stringio_guard():
    # The in-process verify suites redirect stdout to io.StringIO (no .reconfigure).
    # _force_utf8_streams must be a strict no-op there, never raising.
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.__main__ import _force_utf8_streams

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        _force_utf8_streams()  # must not raise on a StringIO
    print("  stringio guard: reconfigure is a no-op on capture streams OK")


if __name__ == "__main__":
    check_help_tree_ascii()
    check_cli_literals_ascii()
    check_clierror_body_literals_ascii()
    check_utf8_data_roundtrip()
    check_stringio_guard()
    print("verify_cli_ascii OK")
