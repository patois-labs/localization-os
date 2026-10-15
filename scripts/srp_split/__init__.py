"""Deterministic, conservative SRP pure moves and compact planner inventories.

Run with the repository interpreter. No imports of application code, Git writes
except explicit undo, or network access. Unsupported/ambiguous layouts fail closed.
Undo receipts live in the system temporary directory, never in the repository.
"""

from __future__ import annotations

import argparse
import ast
import collections
import contextlib
import datetime
import hashlib
import json
import re
import subprocess
import symtable
import sys
import tempfile
import textwrap
from pathlib import Path

import srp_move_check as checker

from . import modes, units

DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
FLOOR = "tests/verify_docs_architecture.py"
OWNERS = "app/modules.py"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", **kwargs)


def read(path):
    return Path(path).read_bytes().decode("utf-8")


def module(path):
    parts = path.removesuffix(".py").split("/")
    # Standalone integration roots (e.g. mcp-server) are sys.path entries,
    # not importable Python package names.
    for index, part in enumerate(parts):
        if not part.isidentifier():
            parts = parts[index + 1 :]
            break
    return ".".join(parts).removesuffix(".__init__")


def safe_path(path):
    require(isinstance(path, str) and path, "path must be a nonempty string")
    p = Path(path)
    require(not p.is_absolute() and ".." not in p.parts, f"unsafe path: {path}")
    require("\\" not in path and ":" not in path, f"use repository-relative paths: {path}")
    require(p.resolve().is_relative_to(Path.cwd().resolve()), f"path escapes repository: {path}")
    require(not any(part.startswith(".git") for part in p.parts), f"Git path refused: {path}")
    return path


def bound(node):
    if isinstance(node, units.COMPOUNDS):
        return units.compound_bound(node)
    if isinstance(node, DEFINITIONS):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {a.asname or a.name.split(".")[0] for a in node.names}
    targets = node.targets if isinstance(node, ast.Assign) else []
    if isinstance(node, ast.AnnAssign):
        targets = [node.target]
    return {
        n.id
        for t in targets
        for n in ast.walk(t)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }


def references(source):
    """Symbol tables distinguish lexical locals from module/global references."""
    table = symtable.symtable(source, "<piece>", "exec")
    result = set()

    def visit(scope):
        for symbol in scope.get_symbols():
            if (
                symbol.is_referenced()
                or (scope != table and symbol.is_global() and symbol.is_assigned())
            ) and (scope == table or symbol.is_global()):
                result.add(symbol.get_name())
        for child in scope.get_children():
            visit(child)

    visit(table)
    # Annotations may be quoted or postponed; retain their import dependencies.
    for node in ast.walk(ast.parse(source)):
        annotations = []
        if isinstance(node, ast.arg):
            annotations = [node.annotation]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations = [node.returns]
        elif isinstance(node, ast.AnnAssign):
            annotations = [node.annotation]
        for annotation in annotations:
            if annotation:
                result.update(n.id for n in ast.walk(annotation) if isinstance(n, ast.Name))
                if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
                    with contextlib.suppress(SyntaxError):
                        result.update(
                            n.id
                            for n in ast.walk(ast.parse(annotation.value, mode="eval"))
                            if isinstance(n, ast.Name)
                        )
    return result


def start(node):
    return min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))]) - 1


def span(node, lines):
    """Use checker token ownership; never mistake docstring text for comments."""
    first = start(node)
    owned = {
        token.start[0] - 1
        for name, token in checker.attached_comments("".join(lines))
        if name == getattr(node, "name", None)
        and not lines[token.start[0] - 1][: token.start[1]].strip()
    }
    cursor = first - 1
    while cursor >= 0 and (not lines[cursor].strip() or cursor in owned):
        if cursor in owned:
            first = cursor
        cursor -= 1
    return first, node.end_lineno


def segment(node, lines, comments=True):
    first, last = span(node, lines) if comments else (start(node), node.end_lineno)
    return "".join(lines[first:last])


def inventory(path):
    safe_path(path)
    source = read(path)
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    names = {n.name for n in tree.body if isinstance(n, DEFINITIONS)}
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    imported = set().union(*(bound(n) for n in imports))
    uses = {n.name: references(segment(n, lines)) for n in tree.body if isinstance(n, DEFINITIONS)}
    definitions = []
    for node in tree.body:
        if isinstance(node, DEFINITIONS):
            doc = ast.get_docstring(node) or ""
            definitions.append(
                {
                    "name": node.name,
                    "kind": type(node).__name__,
                    "lines": [start(node) + 1, node.end_lineno],
                    "size": node.end_lineno - start(node),
                    "doc": doc.splitlines()[0] if doc else "",
                    "decorators": [ast.unparse(d) for d in node.decorator_list],
                    "references": sorted(uses[node.name] & names),
                    "imports": sorted(uses[node.name] & imported),
                }
            )
    constants = [
        {
            "names": sorted(bound(n)),
            "lines": [n.lineno, n.end_lineno],
            "readers": sorted(k for k, v in uses.items() if v & bound(n)),
        }
        for n in tree.body
        if isinstance(n, (ast.Assign, ast.AnnAssign))
    ]
    pins = []
    for doc in sorted(Path("docs/architecture").glob("*.md")):
        for number, line in enumerate(read(doc).splitlines(), 1):
            found = re.findall(re.escape(path) + r"::[\w.]+", line)
            if found:
                pins.append({"doc": doc.as_posix(), "line": number, "pins": found, "row": line})
    return {
        "path": path,
        "definitions": definitions,
        "constants": constants,
        "imports": [segment(n, lines, False).rstrip() for n in imports],
        "documented_in": next(
            (s.strip() for s in source.splitlines() if "Documented in:" in s), None
        ),
        "pins": pins,
    }


def load_manifest(path):
    manifest = json.loads(read(path))
    for key in ("old", "base", "modules", "keep", "documented_in"):
        require(key in manifest, f"manifest missing {key}")
    safe_path(manifest["old"])
    require(
        manifest["old"].endswith(".py"),
        "old must be a Python module",
    )
    doc = manifest["documented_in"]
    if doc is None:
        require(
            manifest["old"].startswith(("scripts/", "integrations/")),
            "null docs only outside architecture ratchet",
        )
    else:
        safe_path(doc)
        require(
            doc.startswith("docs/architecture/") and doc.endswith(".md"), "expected L1 doc path"
        )
    require(isinstance(manifest["base"], str) and manifest["base"], "base must be a revision")
    require(isinstance(manifest["keep"], list), "keep must be a list")
    require(
        isinstance(manifest.get("allow_added", []), list)
        and all(isinstance(n, str) for n in manifest.get("allow_added", [])),
        "allow_added must be a list of names",
    )
    require(
        isinstance(manifest["modules"], list) and manifest["modules"], "modules must be nonempty"
    )
    paths = [manifest["old"]]
    for piece in manifest["modules"]:
        paths.append(safe_path(piece["path"]))
        require(piece["path"].endswith(".py"), "module must be a .py path")
        units.piece_path(piece["path"], require)
        require(
            Path(piece["path"]).parent == Path(manifest["old"]).parent,
            "v1 requires sibling modules (relative import semantics)",
        )
        require(isinstance(piece.get("defs"), list) and piece["defs"], "defs must be nonempty")
        require(
            isinstance(piece.get("responsibility"), str) and piece["responsibility"].strip(),
            "responsibility must be nonempty",
        )
        require(isinstance(piece.get("leaf", False), bool), "leaf must be boolean")
    require(len(paths) == len(set(paths)), "duplicate module path")
    return manifest


def clean(paths):
    for path in paths:
        tracked = run(["git", "ls-files", "--error-unmatch", "--", path])
        require(tracked.returncode == 0, f"existing edit target is not tracked: {path}")
        status = run(["git", "status", "--porcelain", "--", path])
        require(status.returncode == 0 and not status.stdout.strip(), f"dirty edit target: {path}")


def doc_edits(manifest, owners):
    if manifest["documented_in"] is None:
        return {}
    old = manifest["old"]
    pieces = manifest["modules"]
    changes = {}
    claimed = False
    for path in sorted(Path("docs/architecture").glob("*.md")):
        before = read(path)
        lines = before.splitlines(keepends=True)
        for index, line in enumerate(lines):
            claims = line.lstrip().startswith("|") and re.search(
                r"`" + re.escape(old) + r"(?:::|`)", line
            )
            line = re.sub(
                re.escape(old) + r"::(\w+)", lambda m: owners.get(m[1], old) + "::" + m[1], line
            )
            if claims and not claimed:
                # Several L1 rows may claim one module; the first row (first page, first line)
                # receives the new pins, every row keeps its repointed symbol pins.
                claimed = True
                # Place the extra pins in the same cell as the incumbent module.
                cells = line.split("|")
                cell = next(
                    i
                    for i, c in enumerate(cells)
                    if old in c or any(p["path"] in c for p in pieces)
                )
                for piece in pieces:
                    if not re.search(re.escape(piece["path"]) + r"(?:::|`)", cells[cell]):
                        cells[cell] = cells[cell].rstrip() + (
                            f" · `{piece['path']}::{piece['defs'][0]}` "
                        )
                if not re.search(r"`" + re.escape(old) + r"(?:::|`)", cells[cell]):
                    cells[cell] = f" `{old}` ·" + cells[cell]
                line = "|".join(cells)
            lines[index] = line
        after = "".join(lines)
        if before != after:
            changes[path.as_posix()] = after
    source = read(FLOOR)
    promoted = False
    if not claimed:
        # Concrete connector plugins start on the authored allowlist. Promote
        # their documentation home, rather than silently growing that allowlist.
        allow = next((n for n in ast.parse(source).body if "_AUTHORED_ALLOWLIST" in bound(n)), None)
        entries = list(zip(allow.value.keys, allow.value.values, strict=True)) if allow else []
        entry = next(
            ((k, v) for k, v in entries if isinstance(k, ast.Constant) and k.value == old), None
        )
        require(entry is not None, f"no L1 table row or authored allowance claims {old}")
        lines = source.splitlines(keepends=True)
        end = entry[1].end_lineno
        while end < len(lines) and lines[end].strip() in (")", "),"):
            end += 1
        del lines[entry[0].lineno - 1 : end]
        source = "".join(lines)
        path = manifest["documented_in"]
        doc = changes.get(path, read(path))
        lines = doc.splitlines(keepends=True)
        heading = next(
            (
                i
                for i, line in enumerate(lines)
                if line.startswith("## ") and "Where the work happens" in line
            ),
            None,
        )
        require(heading is not None, "L1 doc missing Where the work happens table")
        end = heading + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        rows = [i for i in range(heading + 1, end) if lines[i].startswith("|")]
        require(len(rows) >= 2, "L1 doc missing table rows")
        pins = [f"`{old}`", *(f"`{p['path']}::{p['defs'][0]}`" for p in pieces)]
        lines.insert(
            rows[-1] + 1,
            f"| {Path(old).stem} connector | "
            + " · ".join(pins)
            + " | "
            + "; ".join(p["responsibility"] for p in pieces)
            + ". |\n",
        )
        changes[path] = "".join(lines)
        promoted = True
    tree = ast.parse(source)
    floor = next((n for n in tree.body if "_DOCUMENTED_FLOOR" in bound(n)), None)
    require(floor is not None, "missing _DOCUMENTED_FLOOR")
    lines = source.splitlines(keepends=True)
    candidates = [i for i in range(floor.lineno - 1, floor.end_lineno) if f'"{old}"' in lines[i]]
    if promoted:
        candidates = [
            i
            for i in range(floor.lineno - 1, floor.end_lineno)
            if re.search(
                r"# " + re.escape(Path(manifest["documented_in"]).stem) + r" \(\d+\)", lines[i]
            )
        ]
    require(len(candidates) == 1, f"{old} missing/ambiguous in _DOCUMENTED_FLOOR")
    cursor = candidates[0]
    while cursor > floor.lineno and not lines[cursor].lstrip().startswith("#"):
        cursor -= 1
    require(re.search(r"\(\d+\)", lines[cursor]), "floor block needs a (count) header")
    end = cursor + 1
    while end < floor.end_lineno and re.fullmatch(r'\s*"[^"\n]+",\s*', lines[end]):
        end += 1
    entries = re.findall(r'"([^"]+)"', "".join(lines[cursor + 1 : end]))
    require(promoted or old in entries, "unsupported floor block layout")
    entries = sorted(set(entries) | {old} | {p["path"] for p in pieces})
    indent = re.match(r"\s*", lines[cursor + 1])[0]
    newline = "\r\n" if "\r\n" in source else "\n"
    lines[cursor] = re.sub(r"\(\d+\)", f"({len(entries)})", lines[cursor])
    lines[cursor + 1 : end] = [f'{indent}"{p}",{newline}' for p in entries]
    changes[FLOOR] = "".join(lines)
    if Path(OWNERS).exists():
        source = read(OWNERS)
        lines = source.splitlines(keepends=True)
        edits = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.keyword) and node.arg == "own_files":
                for value in ast.walk(node.value):
                    if isinstance(value, ast.Constant) and value.value == old:
                        require(value.lineno == value.end_lineno, "unsupported own_files entry")
                        line = lines[value.lineno - 1]
                        # AST columns are UTF-8 offsets, not character indices.
                        extra = ", " + ", ".join(repr(p["path"]) for p in pieces)
                        edits.append((value.lineno - 1, value.end_col_offset, extra.encode()))
        for index, column, extra in sorted(edits, reverse=True):
            raw = lines[index].encode("utf-8")
            lines[index] = (raw[:column] + extra + raw[column:]).decode("utf-8")
        if edits:
            changes[OWNERS] = "".join(lines)
    return changes


def import_text(node, lines, old):
    text = segment(node, lines, False)
    if isinstance(node, ast.ImportFrom) and node.level:
        prefix = (module(old) + (".__init__" if old.endswith("/__init__.py") else "")).split(".")[
            : -node.level
        ]
        target = ".".join([*prefix, *([node.module] if node.module else [])])
        text = re.sub(r"from\s+\S+\s+import", f"from {target} import", text, count=1)
    return text


def shim_doc(source, manifest, summary):
    tree = ast.parse(source)
    require(
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str),
        "old needs a module docstring",
    )
    node = tree.body[0]
    lines = source.splitlines(keepends=True)
    raw = segment(node, lines, False)
    match = re.match(r'(?i:[ru]*)("""|\'\'\')', raw)
    require(match is not None, "old docstring must use triple quotes")
    quote = match[1]
    text = raw[match.end() : raw.rfind(quote)].replace("\r\n", "\n")
    if manifest["documented_in"] is not None and "Documented in:" not in text:
        text = text.rstrip("\r\n") + "\n\nDocumented in: " + manifest["documented_in"] + "\n"
    if summary is not None:
        text = summary + ("\n" + text.split("\n", 1)[1] if "\n" in text else "")
    paragraph = (
        "Transitional compatibility shim for one release "
        f"(added {datetime.date.today().isoformat()}, "
        "remove the re-export block after the next tagged "
        "release): `__all__` re-exports "
        + ", ".join(name for p in manifest["modules"] for name in p["defs"])
        + " from "
        + ", ".join(module(p["path"]) for p in manifest["modules"])
        + "."
    )
    first, separator, rest = text.partition("\n\n")
    paragraph = textwrap.fill(paragraph, width=100, break_long_words=False, break_on_hyphens=False)
    text = (
        first.rstrip("\r\n") + "\n\n" + paragraph + "\n\n" + rest
        if separator
        else (text.rstrip("\r\n") + "\n\n" + paragraph + "\n")
    )
    require(quote not in text, "docstring quote collision")
    lines[node.lineno - 1 : node.end_lineno] = [raw[: match.end()] + text + quote + "\n"]
    return "".join(lines)


def plan(manifest, summary=None, pull_deps=False):
    old = manifest["old"]
    for piece in manifest["modules"]:
        units.piece_path(piece["path"], require)
    source = read(old)
    require(
        checker.show(manifest["base"], old) == source.replace("\r\n", "\n"),
        "old differs from manifest base (or base is unreadable)",
    )
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    nodes, duplicates = units.index(tree, bound, require)
    owners = dict.fromkeys(nodes, old)
    assigned = list(manifest["keep"])
    for piece in manifest["modules"]:
        for name in piece["defs"]:
            require(name in nodes, f"unknown definition/assignment: {name}")
            require(
                isinstance(
                    nodes[name], (*DEFINITIONS, ast.Assign, ast.AnnAssign, *units.COMPOUNDS)
                ),
                f"cannot move import or unsupported statement: {name}",
            )
            require(name != "__all__", "__all__ is shim metadata, not a movable assignment")
            for binding in bound(nodes[name]):
                owners[binding] = piece["path"]
            assigned.append(name)
    require(all(isinstance(n, str) for n in assigned), "definition names must be strings")
    require(len(assigned) == len(set(assigned)), "definition assigned more than once")
    require(set(assigned) <= nodes.keys(), f"unknown keep names: {set(assigned) - nodes.keys()}")
    missing = {n.name for n in tree.body if isinstance(n, DEFINITIONS)} - set(assigned)
    require(not missing, f"unassigned definitions: {sorted(missing)}")
    require("register" not in nodes or "register" in manifest["keep"], "keep register in keep")
    for node in tree.body:
        require(
            len({owners[n] for n in bound(node)}) <= 1,
            f"all names of assignment at line {node.lineno} must move together",
        )
    router_defs, router_uses = (
        modes.route_setup(tree, owners, old, require) if manifest.get("router_mode") else ({}, {})
    )
    replicas = (
        units.pull(tree, nodes, owners, old, references, bound, require, router_defs)
        if pull_deps
        else {}
    )
    register_source, additions = (
        modes.register(source, owners, old, require)
        if manifest.get("register_mode")
        else (source, {})
    )
    if pull_deps and additions:
        while True:
            previous = owners.copy()
            extra = [
                (node, path)
                for path, functions in additions.items()
                for _, text in functions
                for node in ast.parse(text).body
            ]
            pulled = units.pull(
                tree, nodes, owners, old, references, bound, require, router_defs, extra
            )
            for path, names in pulled.items():
                replicas.setdefault(path, set()).update(names)
            if owners == previous:
                break
            register_source, additions = modes.register(source, owners, old, require)
    pieces = {old: [], **{p["path"]: [] for p in manifest["modules"]}}
    removed = set()
    originals = {}
    spans, detached = units.comment_segments(source, tree, owners, old, span, checker)
    lines = detached.pop(None)
    for node in duplicates:
        removed.update(range(start(node), node.end_lineno))
    for node in tree.body:
        if node in duplicates:
            continue
        for router_name, constructor in router_defs.items():
            if node is constructor:
                for path, names in router_uses.items():
                    if path != old and router_name in names:
                        pieces[path].append(segment(node, lines, False))
        name = next(iter(sorted(bound(node))), None)
        target = owners.get(name, old)
        if target != old:
            units.dynamic(segment(node, lines, False), old, require, start(node), source)
            first, last = spans[node]
            require(not removed.intersection(range(first, last)), "overlapping source segments")
            removed.update(range(first, last))
            text = detached.get(name, "") + "".join(lines[first:last])
            pieces[target].append(text)
            if isinstance(node, DEFINITIONS):
                originals[(target, name)] = text
    remaining = "".join(line for i, line in enumerate(lines) if i not in removed)
    if additions:
        original_register = next(
            n for n in ast.parse(remaining).body if getattr(n, "name", None) == "register"
        )
        new_register = next(
            n for n in ast.parse(register_source).body if getattr(n, "name", None) == "register"
        )
        retained = remaining.splitlines(keepends=True)
        retained[original_register.lineno - 1 : original_register.end_lineno] = [
            segment(new_register, register_source.splitlines(keepends=True), False)
        ]
        remaining = "".join(retained)
        for path, functions in additions.items():
            pieces[path].extend(text for name, text in functions)
    if router_defs:
        # Include immediately before the next retained statement after the first
        # moved route. This preserves precedence between retained and moved routes.
        runs = modes.route_runs(tree, owners, old)
        retained = []
        for index, line in enumerate(lines):
            for node in tree.body:
                if start(node) != index:
                    continue
                for name, target, subset in runs.get(node.lineno, []):
                    alias = Path(target).stem + "_" + name
                    retained.append(modes.include(name, alias, subset))
            if index not in removed:
                retained.append(line)
        remaining = "".join(retained)
    pieces[old] = [remaining]
    imports = [
        n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) and n not in duplicates
    ]
    for path, names in replicas.items():
        pieces[path][:0] = [segment(nodes[n], lines, False) for n in sorted(names)]
        print(f"REPLICATE logger {sorted(names)} in {path}; log names follow the piece module")
    require(not any("*" in bound(n) for n in imports), "star imports are unsupported")
    edges = {path: set() for path in pieces}
    cross = {}
    for path, blocks in pieces.items():
        used = references("\n".join(blocks))
        if path != old:
            units.dynamic("\n".join(blocks), path, require, context=source)
        wanted = {
            n
            for n in used
            if n in owners
            and owners[n] != path
            and n not in replicas.get(path, set())
            and n not in router_uses.get(path, set())
            and not isinstance(nodes[n], (ast.Import, ast.ImportFrom))
        }
        if path == old:
            wanted |= {n for n, owner in owners.items() if owner != old}
        cross[path] = "".join(
            f"from {module(owner)} import "
            + ", ".join(sorted(n for n in wanted if owners[n] == owner))
            + "\n"
            for owner in sorted({owners[n] for n in wanted})
        )
        edges[path].update(owners[n] for n in wanted)
    for path, functions in additions.items():
        cross[old] += f"from {module(path)} import " + ", ".join(n for n, _ in functions) + "\n"
    if router_defs and any(subset for calls in runs.values() for _, _, subset in calls):
        cross[old] += "from copy import copy as _srp_copy\n"
    for path, names in router_uses.items():
        if path != old:
            for name in sorted(names):
                cross[old] += f"from {module(path)} import {name} as {Path(path).stem}_{name}\n"
    for piece in manifest["modules"]:
        require(
            not piece.get("leaf") or not edges[piece["path"]],
            f"leaf {piece['path']} imports train pieces: {sorted(edges[piece['path']])}; "
            + units.leaf_dependencies(piece["path"], cross, pull_deps),
        )
    require(not checker.cycles(edges), f"cross-piece cycle: {checker.cycles(edges)}")
    result = {}
    for piece in manifest["modules"]:
        path = piece["path"]
        body = "\n\n".join(pieces[path])
        used = references(body)
        needed = [
            n
            for n in imports
            if bound(n) & used or (isinstance(n, ast.ImportFrom) and n.module == "__future__")
        ]
        doc = piece["responsibility"]
        if manifest["documented_in"] is not None:
            doc += "\n\nDocumented in: " + manifest["documented_in"]
        require('"""' not in doc and "\\" not in doc, "unsupported module docstring quoting")
        result[path] = (
            '"""'
            + doc
            + '\n"""\n\n'
            + "".join(import_text(n, lines, old) for n in needed)
            + cross[path]
            + "\n\n"
            + body
        )
        if re.search(r"(?m)^# noqa\b", body):
            # Detached checker-owned pragmas must survive verbatim even when
            # their former import/assignment no longer accompanies them.
            result[path] += "\n# Preserve relocated noqa comments.\n# ruff: noqa: RUF100\n"
    remaining = shim_doc(remaining, manifest, summary)
    # Keep import-time statements in their original order. Cross imports must be
    # available before any retained class, decorator, default or module statement.
    remaining_lines = remaining.splitlines(keepends=True)
    body = ast.parse(remaining).body
    anchor = body[0].end_lineno
    for node in body[1:]:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            anchor = node.end_lineno
        else:
            break
    all_names = sorted(n for n, owner in owners.items() if owner != old)
    # The old module's namespace must stay a superset of what it exported before the split: an
    # import that only moved code still uses stays importable from (and patchable on) the old
    # module, so list it in __all__ and the F401 fix below leaves it in place.
    import_names = {
        n
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
        for n in bound(node)
    }
    used_remaining = {x.id for x in ast.walk(ast.parse(remaining)) if isinstance(x, ast.Name)}
    all_names = sorted(set(all_names) | (import_names - used_remaining))
    if old.endswith("/__init__.py"):
        all_names = sorted(
            set(all_names)
            | {
                name
                for node in imports
                if getattr(node, "module", None) != "__future__"
                for name in bound(node)
            }
        )
    existing_all = next((n for n in body if "__all__" in bound(n)), None)
    if existing_all:
        value = ast.literal_eval(existing_all.value)
        require(
            isinstance(value, (list, tuple)) and all(isinstance(n, str) for n in value),
            "__all__ must be a literal string list/tuple",
        )
        all_names = sorted(set(all_names) | set(value))
        del remaining_lines[existing_all.lineno - 1 : existing_all.end_lineno]
    remaining_lines[anchor:anchor] = ["\n" + cross[old] + "\n__all__ = " + repr(all_names) + "\n"]
    result[old] = "".join(remaining_lines)
    # Include pre-existing and function-local imports when checking the complete DAG.
    names = {p: module(p) for p in result}
    full_edges = {
        names[p]: checker.imports_of(s, names[p]) & set(names.values()) for p, s in result.items()
    }
    require(not checker.cycles(full_edges), f"import cycle: {checker.cycles(full_edges)}")
    for piece in manifest["modules"]:
        require(
            not piece.get("leaf") or not full_edges[names[piece["path"]]],
            f"leaf imports train: {piece['path']}",
        )
    result.update(doc_edits(manifest, owners))
    return result, originals


def format_source(path, source):
    """Format only planned edit targets through stdin; never modify a moved body."""
    # Import-only fixes remove obsolete shim imports and sort generated imports.
    fixed = run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--fix",
            "--select",
            "I,F401,RUF022",
            "--config",
            "lint.isort.known-first-party=" + json.dumps([module(path).split(".")[0]]),
            "--stdin-filename",
            path,
            "-",
        ],
        input=source.replace("\r\n", "\n"),
    )
    require(fixed.returncode == 0, f"ruff imports failed for {path}: {fixed.stderr}")
    formatted = run(
        [sys.executable, "-m", "ruff", "format", "--no-cache", "--stdin-filename", path, "-"],
        input=fixed.stdout,
    )
    require(formatted.returncode == 0, f"ruff format failed for {path}: {formatted.stderr}")
    newline = "\r\n" if "\r\n" in source else "\n"
    return formatted.stdout.replace("\n", newline)


def receipt_path(manifest):
    identity = str(Path.cwd().resolve()) + json.dumps(
        {k: v for k, v in manifest.items() if k != "allow_added"}, sort_keys=True
    )
    key = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"srp-{key}.json"


def prove(before, sources, manifest=None):
    """Enforce checker invariants even when the optional subprocess gate is off."""
    expected = checker.defs(before)
    actual = {}
    comments = collections.Counter()
    statements = collections.Counter()
    attached = {}
    for path, source in sources.items():
        definitions = checker.defs(source)
        require(not actual.keys() & definitions.keys(), "duplicate output definition")
        actual.update(definitions)
        comments += checker.comments(source)
        statements += checker.module_statements(source)
        attached.update(checker.comments_by_def(source))
        require(not checker.HOOK_RE.search(source), f"forwarding hook in {path}")
    if manifest and manifest.get("register_mode"):
        require(modes.register_matches(before, sources), "register statement order/content changed")
        expected.pop("register", None)
        actual.pop("register", None)
        for name in manifest.get("allow_added", []):
            actual.pop(name, None)
    require(expected == actual, "definition AST changed during import cleanup/formatting")
    require(
        modes.statements_match(
            before,
            sources,
            checker.module_statements,
            bool(manifest and manifest.get("router_mode")),
            True,
        ),
        "module statements changed",
    )
    require(not checker.comments(before) - comments, "comments lost during import cleanup")
    for name, owned in checker.comments_by_def(before).items():
        if manifest and manifest.get("register_mode") and name == "register":
            continue
        require(not owned - attached[name], f"checker comment ownership would change: {name}")


def apply(manifest, check=False, summary=None, pull_deps=True):
    created = [p["path"] for p in manifest["modules"]]
    require(
        not receipt_path(manifest).exists(), "undo receipt exists; undo the previous apply first"
    )
    for path in created:
        require(not Path(path).exists(), f"new module already exists: {path}")
        require(Path(path).parent.is_dir(), f"module directory missing: {path}")
    planned, originals = plan(manifest, summary, pull_deps)
    if manifest.get("register_mode"):
        manifest["allow_added"] = sorted(
            {n for p in created for n in checker.defs(planned[p]) if n.startswith("register_")}
        )
        print(
            "CHECKER --allow register "
            + " ".join("--allow-added " + n for n in manifest["allow_added"])
        )
    clean([p for p in planned if p not in created])
    registration = modes.register_bytes({p: planned[p] for p in created})
    for path, source in planned.items():
        if path.endswith(".py"):
            planned[path] = format_source(path, source)
    require(
        registration == modes.register_bytes({p: planned[p] for p in created}),
        "ruff would change moved register statements",
    )
    for (path, name), original in originals.items():
        lines = planned[path].splitlines(keepends=True)
        node = next(n for n in ast.parse(planned[path]).body if name in bound(n))
        require(
            segment(node, lines).encode() == original.encode(),
            f"ruff would change moved body/comment block: {name} in {path}",
        )
    prove(read(manifest["old"]), {p: planned[p] for p in [manifest["old"], *created]}, manifest)
    receipt = {
        "created": created,
        "before": {p: read(p) for p in planned if p not in created},
        "after": planned,
    }
    receipt_path(manifest).write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    for path, source in planned.items():
        Path(path).write_bytes(source.encode("utf-8"))
        print(f"{'CREATE' if path in created else 'EDIT'} {path}")
    if OWNERS in planned:
        print(
            "_FEATURES_DIGEST recompute (run manually):\n"
            f'{sys.executable} -c "import dataclasses, hashlib; from app import modules; '
            "print(hashlib.sha256(repr(tuple(dataclasses.astuple(f) for f in "
            'modules.features_view())).encode()).hexdigest())"'
        )
    if check:
        command = [
            sys.executable,
            str(Path(checker.__file__).resolve()),
            manifest["base"],
            "WORKTREE",
            manifest["old"],
            *created,
            "--worktree",
        ]
        for name in manifest.get("allow_added", []):
            command += ["--allow-added", name]
        if manifest.get("register_mode"):
            command += ["--allow", "register"]
        if manifest.get("router_mode"):
            command += ["--router-mode"]
        if pull_deps:
            command += ["--replicate-loggers"]
        results = [
            run(command),
            run(
                [
                    sys.executable,
                    "-m",
                    "ruff",
                    "check",
                    "--no-cache",
                    *(p for p in planned if p.endswith(".py")),
                ]
            ),
        ]
        for result in results:
            print(result.stdout + result.stderr, end="")
        passed = all(r.returncode == 0 for r in results)
        print("PASS" if passed else "FAIL (files retained for inspection; use undo to restore)")
        return 0 if passed else 1
    return 0


def undo(manifest):
    receipt_file = receipt_path(manifest)
    require(receipt_file.exists(), "no undo receipt for this manifest and repository")
    receipt = json.loads(read(receipt_file))
    created = [p["path"] for p in manifest["modules"]]
    require(receipt["created"] == created, "receipt module paths disagree with manifest")
    allowed = {manifest["old"], FLOOR, OWNERS, *created}
    allowed.update(p.as_posix() for p in Path("docs/architecture").glob("*.md"))
    require(set(receipt["after"]) <= allowed, "unexpected receipt paths")
    require(
        set(receipt["before"]) | set(created) == set(receipt["after"]), "inconsistent undo receipt"
    )
    staged = run(["git", "diff", "--cached", "--quiet", "--", *receipt["after"]])
    require(staged.returncode == 0, "staged changes in undo targets; refusing checkout")
    for path, source in receipt["after"].items():
        safe_path(path)
        current = read(path) if Path(path).exists() else None
        require(
            current in (source, receipt["before"].get(path)),
            f"edited since apply; refusing to discard changes: {path}",
        )
    for path in receipt["before"]:
        result = run(["git", "checkout", "--", path])
        if result.returncode:
            readonly = "index.lock" in result.stderr and any(
                message in result.stderr
                for message in ("Permission denied", "Read-only file system")
            )
            require(readonly, f"git checkout failed: {result.stderr}")
            indexed = run(["git", "show", f":{path}"])
            require(
                indexed.returncode == 0
                and indexed.stdout == (receipt["before"][path].replace("\r\n", "\n")),
                f"read-only index content differs from original: {path}",
            )
            print(f"READ-ONLY INDEX: restore verified index bytes for {path}")
        # Git may change CRLF policy; restore the exact originally clean bytes.
        Path(path).write_bytes(receipt["before"][path].encode())
        print(f"RESTORE {path}")
    for path in created:
        Path(path).unlink(missing_ok=True)
        print(f"DELETE {path}")
    receipt_file.unlink()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inv = commands.add_parser("inventory")
    inv.add_argument("path")
    inv.add_argument("--json", metavar="OUT")
    app = commands.add_parser("apply")
    app.add_argument("manifest")
    app.add_argument("--check", action="store_true")
    app.add_argument("--pull-deps", action=argparse.BooleanOptionalAction, default=True)
    app.add_argument("--old-summary")
    rev = commands.add_parser("undo")
    rev.add_argument("manifest")
    for name in ("helpdiff", "routediff"):
        diff = commands.add_parser(name)
        diff.add_argument("module", nargs="?", default="app.main")
        diff.add_argument("--base", default="HEAD")
    args = parser.parse_args()
    try:
        if args.command in ("helpdiff", "routediff"):
            from .parity import compare

            return compare(args.command, args.module, args.base)
        if args.command == "inventory":
            result = json.dumps(inventory(args.path), ensure_ascii=False, separators=(",", ":"))
            if args.json:
                Path(args.json).write_text(result + "\n", encoding="utf-8")
            else:
                print(result)
            return 0
        manifest = load_manifest(args.manifest)
        if args.command == "undo":
            return undo(manifest)
        return apply(manifest, args.check, args.old_summary, args.pull_deps)
    except (ValueError, KeyError, TypeError, OSError, SyntaxError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
