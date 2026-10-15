"""Check definition, comment and module-statement fidelity across an SRP split.

Definitions must remain AST-identical, including docstrings, except --allow NAME.
Added register decompositions require both --allow register and --allow-added NAME.
Comments must survive globally and with their owning definition. Module statements
must survive except imports, __all__, and module docstrings (documentation metadata).
Reject forwarding hooks and import cycles of any length within the train.

Usage: python srp_move_check.py BASE HEAD OLD_PATH NEW_PATH [NEW_PATH ...] [--allow NAME ...]
Exit 0 = pure move; exit 1 = findings printed.
"""

from __future__ import annotations

import argparse
import ast
import collections
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

HOOK_RE = re.compile(
    r"__class__\s*=|def __setattr__|def __getattr__|sys\.modules\[__name__\]|importlib\."
)


def show(rev: str, path: str) -> str | None:
    r = subprocess.run(
        ["git", "show", f"{rev}:{path}"], capture_output=True, text=True, encoding="utf-8"
    )
    return r.stdout if r.returncode == 0 else None


def worktree(path: str) -> str | None:
    """Read the after-side without requiring staging or a commit."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def defs(src: str) -> dict[str, str]:
    return {
        n.name: ast.dump(n)
        for n in ast.parse(src).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def comments(src: str) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type == tokenize.COMMENT and not t.string.strip().startswith("# noqa"):
            # lint pragmas are tooling metadata; ruff may drop an unused one after a move
            c[t.string.strip()] += 1
    return c


def attached_comments(src: str):
    """Yield the shared checker ownership decision and each comment token."""
    nodes = [
        n
        for n in ast.parse(src).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    for token in tokenize.generate_tokens(io.StringIO(src).readline):
        if token.type != tokenize.COMMENT or token.string.strip().startswith("# noqa"):
            continue
        for node in nodes:
            if token.start[0] <= node.end_lineno:
                yield node.name, token
                break


def comments_by_def(src: str) -> dict[str, collections.Counter]:
    """Attach interior comments to their def, and preceding comments to the next def."""
    out = {name: collections.Counter() for name in defs(src)}
    for name, token in attached_comments(src):
        out[name][token.string.strip()] += 1
    return out


def module_statements(src: str) -> collections.Counter:
    out: collections.Counter = collections.Counter()
    for index, node in enumerate(ast.parse(src).body):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
        ):
            continue
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else ([node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
        )
        if targets and all(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        out[ast.dump(node)] += 1
    return out


def imports_of(src: str, module: str) -> set[str]:
    out: set[str] = set()

    def walk(node, deferred=False):
        yield node, deferred
        for field, value in ast.iter_fields(node):
            delayed = deferred or (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and field == "body"
            )
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    yield from walk(child, delayed)

    for n, deferred in walk(ast.parse(src)):
        if isinstance(n, ast.ImportFrom):
            prefix = module.split(".")[: -n.level] if n.level else []
            target = ".".join([*prefix, *([n.module] if n.module else [])])
            # A deferred self import reads an already initialized module. Keep
            # imports of other train pieces, including moved functions' old home.
            if target == module and deferred:
                continue
            out.add(target)
            out.update(f"{target}.{alias.name}" for alias in n.names)
        elif isinstance(n, ast.Import):
            out.update(a.name for a in n.names if not (deferred and a.name == module))
    return out


def cycles(edges: dict[str, set[str]]) -> list[str]:
    visited: set[str] = set()
    active: list[str] = []
    found: list[str] = []

    def visit(module: str) -> None:
        if module in active:
            found.append(" -> ".join([*active[active.index(module) :], module]))
            return
        if module in visited:
            return
        visited.add(module)
        active.append(module)
        for dependency in sorted(edges[module]):
            visit(dependency)
        active.pop()

    for module in sorted(edges):
        visit(module)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("head")
    ap.add_argument("old")
    ap.add_argument("new", nargs="+")
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--allow-added", action="append", default=[])
    ap.add_argument("--worktree", action="store_true", help="read after-side from disk")
    ap.add_argument("--router-mode", action="store_true")
    ap.add_argument("--replicate-loggers", action="store_true")
    a = ap.parse_args()
    before_src = show(a.base, a.old)
    after_old_src = worktree(a.old) if a.worktree else show(a.head, a.old)
    if before_src is None or after_old_src is None:
        print(f"FAIL cannot read {a.old} at {a.base} / {a.head}")
        return 1
    before, after_old = defs(before_src), defs(after_old_src)
    mods = {a.old: after_old_src}
    after_new: dict[str, str] = {}
    for p in a.new:
        s = worktree(p) if a.worktree else show(a.head, p)
        if s is None:
            print(f"FAIL new module {p} missing at {a.head}")
            return 1
        mods[p] = s
        after_new.update(defs(s))
    findings: list[str] = []
    for name, dump in before.items():
        if name in a.allow:
            continue
        if name in after_old:
            if after_old[name] != dump:
                findings.append(f"CHANGED in place: {name}")
        elif name in after_new:
            if after_new[name] != dump:
                findings.append(f"CHANGED while moved: {name}")
        else:
            findings.append(f"LOST: {name}")
    for name in list(after_new) + list(after_old):
        allowed_addition = name in a.allow_added
        if name.lstrip("_").startswith("register"):
            allowed_addition = allowed_addition and "register" in a.allow and "register" in before
        if name not in before and not allowed_addition:
            findings.append(f"ADDED: {name}")
    before_c = comments(before_src)
    after_c: collections.Counter = collections.Counter()
    for s in mods.values():
        after_c += comments(s)
    lost = before_c - after_c
    if lost:
        findings.append(f"COMMENTS LOST: {sum(lost.values())} (first: {next(iter(lost))[:90]!r})")
    attached = {p: comments_by_def(s) for p, s in mods.items()}
    for name, original in comments_by_def(before_src).items():
        if name in a.allow:
            continue
        for path, owned in attached.items():
            if name in owned and original - owned[name]:
                findings.append(f"COMMENTS MISPLACED: {name} in {path}")
    before_statements = module_statements(before_src)
    after_statements: collections.Counter = collections.Counter()
    for source in mods.values():
        after_statements += module_statements(source)
    from srp_split.modes import statements_match

    if not statements_match(
        before_src, mods, module_statements, a.router_mode, a.replicate_loggers
    ):
        findings.append(
            f"MODULE STATEMENTS: lost {dict(before_statements - after_statements)}, "
            f"added {dict(after_statements - before_statements)}"
        )
    for p, s in mods.items():
        if HOOK_RE.search(s):
            findings.append(f"HOOK in {p}")
    names = {p: p[:-3].replace("/", ".").replace("\\", ".") for p in mods}
    edges = {
        names[p]: {m for m in imports_of(s, names[p]) if m in names.values()}
        for p, s in mods.items()
    }
    findings.extend(f"CYCLE: {cycle}" for cycle in cycles(edges))
    sizes = {p: len(s.splitlines()) for p, s in mods.items()}
    print("sizes:", sizes)
    if findings:
        print("\n".join("FAIL " + f for f in findings))
        return 1
    print(
        f"PURE MOVE OK: {len(before)} defs accounted for, comments preserved, no hooks, no cycles"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
