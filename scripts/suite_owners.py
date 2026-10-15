"""Which verify suites own a change: suite_owners.py [RANGE] [--selector EXPR] [--json].

    python scripts/suite_owners.py                    # <merge-base of main and HEAD>..HEAD
    python scripts/suite_owners.py accc9661..HEAD
    python scripts/suite_owners.py --files app/services/scim.py app/web/admin.py
    python scripts/suite_owners.py main..HEAD --selector "verify_scim or verify_saml"

A suite OWNS a changed file when it
  (a) names the module (dotted name, a path to the file, or, for a script outside a package,
      its directory plus an import of it) AND the file was added or deleted, its module-level
      code outside any definition changed, or the suite reads the file as text and quotes
      text the edit removed (or, for a .py file, pins a count of a text whose count changed:
      ("app/x.py", "prof.base_url"): 12, #186). A plain import of an edited module is not
      ownership by itself: rules (b) and (c) say what the importer actually reaches;
  (b) uses a definition the diff added, removed or edited (functions, classes, class members,
      module constants; ast on both sides, so formatting and comments do not count, and a
      definition that moved unchanged to another file is not a change), or an unchanged
      function of the same file that calls an edited one (one hop). A literal table constant
      (dict, list, set) is judged by its changed ENTRIES, which the suite must quote;
  (c) drives a route whose handler or registration changed, with a matching HTTP method, or
      a CLI verb (argparse add_parser) whose handler or registration changed.
A changed tests/verify_*.py suite owns itself.
Paths outside the roots that suites still read or run (tests/ goldens and fixtures, the
wrapper, Dockerfile, docker-entrypoint.sh, alembic/, tools/, requirements*, pyproject.toml) are
covered too: a suite that names the path owns ANY edit to it. One that no suite names is listed
as unowned, and the exit status is 1 (pregate FAILs) rather than passing with nothing run (#524).

Scaffolding does not count: `from app.main import app`, signing in and out (SCAFFOLD_ROUTES),
a name shorter than MIN_NAME or in COMMON, a one-word CLI verb, a route with fewer than four
letters, a route string only compared in an assert (a link, a redirect target), a function
that only gained registration calls (a new line in build_parser), and a common-word name
(no underscore or camel case, defined more than once, a module stem, or defined by the suite
itself) unless the suite reaches it through its module or class. A module more than NARROW
suites name, and any single name, route or verb more than BROAD suites carry, is reported as
broad and never enforced: a name every suite mentions is scaffolding, not an owner.

--files names files instead of a diff: every named file counts as changed, and only rule (a)
and the changed-suite rule apply (no diff, so no changed symbols, routes or verbs).
Output: a per-file table and a ready-to-paste -k selector. With --selector, the owners that
selector would NOT collect are listed and the exit status is 1 when any is missing.
Standard library only (pytest's own -k parser is used when importable), deterministic,
offline; scripts/pregate.py runs the same derivation as its "Suite owners" check.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

ROOTS = ("app/", "scripts/", "deploy/", "integrations/", "tests/_")
# #524: inputs outside ROOTS that suites still read or run. Test goldens and fixtures, the
# wrapper and conftest, the image recipe, migrations and dependency locks used to be invisible,
# so a diff touching only them took pregate's early PASS and no owning suite ever ran. A suite
# owns one of these when it names the path (a suite that reads a golden or a fixture consumes
# the whole file, so ANY edit counts); a changed one that no suite names is reported in
# Report.unowned, and pregate FAILs on it instead of passing with zero suites.
EXTRA_PREFIXES = ("tests/", "alembic/", "tools/")
EXTRA_NAMES = {
    "Dockerfile",
    "docker-entrypoint.sh",
    "docker-compose.yml",
    "pyproject.toml",
    "alembic.ini",
}
MIGRATION_SUITES = {"verify_migrations", "verify_alembic_authority"}
EXTRA_REQUIREMENTS = re.compile(r"requirements[\w.-]*\.(?:lock|txt)")
SUITE = re.compile(r"tests/(verify_[\w]+)\.py")
BROAD = 40
MIN_NAME = 5
NARROW = 15
INDEX_REFS = 200
# Wall-clock sensitive: .claude/rules/tests-and-pregate.md says judge it alone on an idle machine.
IDLE_ONLY = {"verify_stress_liveness_pool"}
SCAFFOLD_MODULES = {"app", "app.main"}
# Signing in and out is how nearly every suite starts and ends, not what it tests.
SCAFFOLD_ROUTES = {"/", "/login", "/logout", "/auth/login", "/auth/logout", "/health", "/ready"}
# Names that read as ordinary words in any suite, whatever module defines them.
COMMON = {
    "build",
    "check",
    "close",
    "create",
    "delete",
    "handler",
    "index",
    "items",
    "logger",
    "lookup",
    "main",
    "model",
    "names",
    "parse",
    "query",
    "register",
    "render",
    "reset",
    "result",
    "router",
    "setup",
    "start",
    "status",
    "update",
    "value",
    "values",
}
ROUTE_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "api_route"}
ROUTE_METHODS |= {"websocket", "route"}
HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}
SEP = r"""(?:[/\\]{1,2}|["']\s*[/,]\s*[rbfRBF]?["'])"""
COMMENT = re.compile(r"(?m)^[ \t]*#.*$")
# String literals, so that a symbol is found only where code uses it, never in prose.
STRINGS = re.compile(
    r'''(?s)[rbfuRBFU]{0,2}(?:""".*?"""|\'\'\'.*?\'\'\'|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')'''
)
LITERAL = re.compile(r""""((?:\\.|[^"\\\n])*)"|'((?:\\.|[^'\\\n])*)'""")
IMPORT = re.compile(r"(?m)^[ \t]*import[ \t]+([\w. \t,]+)")
FROM = re.compile(r"(?m)^[ \t]*from[ \t]+([\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)")
DEFINES = re.compile(
    r"(?m)^[ \t]*(?:(?:async[ \t]+)?def|class)[ \t]+(\w+)|^[ \t]*(\w+)[ \t]*(?::[^=\n]*)?=(?!=)"
)


def git(root, *args, data=None):
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"git {args[0]} failed")
    return result.stdout


def text(data):
    # A UTF-8 BOM would make ast.parse fail and every definition read as removed.
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").removeprefix(chr(0xFEFF))


def read_many(root, rev, paths):
    """Read many blobs at one revision in a single git cat-file call; missing = None."""
    paths = list(paths)
    if not paths:
        return {}
    request = "".join(f"{rev}:{path}\n" for path in paths).encode("utf-8")
    output = git(root, "cat-file", "--batch", data=request)
    found = {}
    position = 0
    for path in paths:
        end = output.index(b"\n", position)
        header = output[position:end].split()
        position = end + 1
        if len(header) < 3 or header[1] != b"blob":
            found[path] = None
            continue
        size = int(header[2])
        found[path] = text(output[position : position + size])
        position += size + 1
    return found


def list_paths(root, rev):
    return [
        path
        for path in text(git(root, "ls-tree", "-r", "--name-only", "-z", rev)).split("\0")
        if path
    ]


def dotted(path):
    if not path.endswith(".py"):
        return None
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def imports(source):
    names = set()
    for match in IMPORT.finditer(source):
        for part in match[1].split(","):
            words = part.split()
            if words:
                names.add(words[0])
    for match in FROM.finditer(source):
        base = match[1]
        names.add(base)
        body = re.sub(r"#[^\n]*", "", match[2]).strip("()\\ \t\n")
        for part in body.split(","):
            words = part.split()
            if words and words[0].isidentifier():
                names.add(f"{base}.{words[0]}")
    return names


def unique_suffix(path, all_paths):
    """The shortest trailing run of path components that no other tracked path ends with."""
    parts = path.split("/")
    for size in range(1, len(parts) + 1):
        tail = "/" + "/".join(parts[-size:])
        if not any(other != path and ("/" + other).endswith(tail) for other in all_paths):
            return parts[-size:]
    return parts


def path_regex(parts):
    body = SEP.join(re.escape(part) for part in parts)
    return re.compile(r"(?<![\w.-])" + body + r"(?![\w-])")


def route_regex(route):
    body = "".join(
        r"""[^/"'\s?#]+""" if part.startswith("{") else re.escape(part)
        for part in re.split(r"(\{[^}]*\})", route)
        if part
    )
    # Opened by a quote (not an HTML attribute's) or by an f-string field: f"{base}/users".
    return re.compile(r"""(?:(?<!=)["']|\})""" + body + r"""(?=["'?#]|$)""", re.M)


# client.post( and a bare post( helper both say the method.
CALL = re.compile(r"(?:\.|(?<![\w.]))(get|post|put|delete|patch|head|options)\s*\(")


def drives(source, pattern, methods):
    """True when some use of the route drives it with one of these HTTP methods (None = any).

    An assert line with no request call only compares text (a link, a redirect target), so
    it is not a use. The method is read from the same line before the path (client.post(,
    a "POST" literal); a use with no method in sight counts, since the suite may build the
    request elsewhere (a list of pages, a helper)."""
    for match in pattern.finditer(source):
        start = source.rfind("\n", 0, match.start()) + 1
        end = source.find("\n", match.end())
        line = source[start : len(source) if end < 0 else end]
        if line.lstrip().startswith("assert") and not CALL.search(line):
            continue
        if methods is None:
            return True
        before = source[start : match.start()]
        said = CALL.findall(before)
        said += [m.lower() for m in re.findall(r"""["'](GET|POST|PUT|DELETE|PATCH)["']""", before)]
        if not said or said[-1] in methods:
            return True
    return False


def verb_regex(words):
    listed = r"""["']\s*,\s*["']""".join(re.escape(word) for word in words)
    spoken = r"[ \t]+".join(re.escape(word) for word in words)
    return re.compile(
        r"""["']""" + listed + r"""["']|["'][^"'\n]*(?<![\w-])""" + spoken + r"(?![\w-])"
    )


def bound_names(target):
    """Plain names an assignment target binds: `A`, and each of `A, (B, *C)` (#524)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in bound_names(item)]
    if isinstance(target, ast.Starred):
        return bound_names(target.value)
    return []


def definitions(tree):
    """qualname -> node for functions, classes (header only), class members and constants."""
    found = {}

    def visit(body, prefix=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[prefix + node.name] = node
            elif isinstance(node, ast.ClassDef):
                found[prefix + node.name] = ast.ClassDef(
                    name=node.name,
                    bases=node.bases,
                    keywords=node.keywords,
                    body=[],
                    decorator_list=node.decorator_list,
                    type_params=getattr(node, "type_params", []),
                )
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in bound_names(target):
                        found[prefix + name] = node
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                # #524: an `if`/`try`/`with` block holds definitions at module level AND in a
                # class body (a nested `class Config:` or a guarded method).
                visit(node.body, prefix)
                visit(getattr(node, "orelse", []), prefix)
                visit(getattr(node, "finalbody", []), prefix)
                for handler in getattr(node, "handlers", []):
                    visit(handler.body, prefix)

    visit(tree.body)
    return found


def callers(found, edited):
    """(caller, callee): unchanged functions of this file that call an EDITED function.

    One hop only. A suite that drives the caller reaches the edited code under another name,
    which is how a topic selector misses its owner.
    """
    targets = {name.rsplit(".", 1)[-1]: name for name in edited}
    pairs = []
    for qualname, node in sorted(found.items()):
        if qualname in edited or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in targets and targets[name] != qualname:
                    called.add(targets[name])
        pairs.extend((qualname, callee) for callee in sorted(called))
    return pairs


def dump(node):
    return ast.dump(node) if node is not None else None


TABLES = (ast.Dict, ast.List, ast.Set, ast.Tuple)


def entries(value):
    """Entry name -> entry node of a literal table (a dict by key, a sequence by first string)."""
    found = {}
    if isinstance(value, ast.Dict):
        for key, item in zip(value.keys, value.values, strict=True):
            found[key.value if isinstance(key, ast.Constant) else dump(key)] = item
    elif isinstance(value, TABLES):
        for item in value.elts:
            strings = [
                sub.value
                for sub in ast.walk(item)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            ]
            found[strings[0] if strings else dump(item)] = item
    return found


def unwrap(value):
    """frozenset({...}), tuple([...]), MappingProxyType({...}): the literal inside."""
    while isinstance(value, ast.Call) and len(value.args) == 1 and not value.keywords:
        value = value.args[0]
    return value


def registration_only(old, new):
    """True when an edited function only gained bare call statements (a new registration
    line in build_parser, say): what it did before, it still does, so its callers are not
    affected. A new guard, return or assignment is a real change."""
    if not all(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in (old, new)):
        return False
    head = [dump(node.args) + str([dump(d) for d in node.decorator_list]) for node in (old, new)]
    if head[0] != head[1] or dump(old.returns) != dump(new.returns):
        return False
    before = [dump(statement) for statement in old.body]
    position = 0
    for statement in new.body:
        if position < len(before) and dump(statement) == before[position]:
            position += 1
        elif not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
            return False
    return position == len(before)


def changed_terms(old, new):
    """Names of the changed entries when a literal table constant was edited, else [].

    Nested tables are followed down, so a changed list inside a dict names the list item
    that changed, not the dict key above it. A table added or removed whole is judged by its
    own name instead (returns [])."""
    tables = [unwrap(getattr(node, "value", None)) for node in (old, new)]
    if not all(isinstance(node, (ast.Assign, ast.AnnAssign)) for node in (old, new)) or not all(
        isinstance(value, TABLES) for value in tables
    ):
        return []

    def walk(before, after):
        # Every changed entry is named, and a nested table also names what changed inside it.
        # Each key is judged by its own sub-walk and keys are visited in sorted order, so the
        # result never depends on set iteration order (PYTHONHASHSEED).
        was, now = entries(before), entries(after)
        found = set()
        for name in sorted(was.keys() | now.keys(), key=repr):
            a, b = was.get(name), now.get(name)
            if dump(a) == dump(b):
                continue
            found.add(name)
            if isinstance(a, TABLES) and isinstance(b, TABLES):
                found |= walk(a, b)
        return found

    terms = walk(*tables)
    return sorted(
        term for term in terms if isinstance(term, str) and len(term.strip("_")) >= MIN_NAME
    )


def quoted(source):
    """The distinctive text a suite quotes: single-line literals of 8+ characters, split at
    f-string fields."""
    found = set()
    for match in LITERAL.finditer(source):
        for piece in re.split(r"\{[^{}]*\}", match[1] if match[1] is not None else match[2]):
            piece = piece.strip()
            if len(piece) >= 8:
                found.add(piece)
    return found


def ratchet_literals(source, here):
    """Texts a suite quotes on a line that names this file or calls .count(: the shape of a
    count ratchet, ("app/x.py", "prof.base_url"): 12."""
    found = set()
    for line in source.splitlines():
        # The pinned count is an integer in the code of the same line, not in a string.
        pinned = here.search(line) and re.search(r"\b\d+\b", STRINGS.sub('""', line))
        if pinned or ".count(" in line:
            found |= quoted(line)
    # The path's own pieces ("services", "engines.py") are not the counted text.
    return {item for item in found if not here.search(item) and "/" not in item}


def residue(tree):
    """Module-level code outside definitions, name assignments, imports and the docstring.

    An assignment to an attribute or a subscript (`settings.X = 1`, `TABLE["k"] = v`) binds no
    name, so `definitions` never sees it: it is residue (#524), as is any assignment whose
    targets are not all plain names."""
    skip = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Import,
        ast.ImportFrom,
    )
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]

    def binds_names(node):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return all(
            bound_names(target) and len(bound_names(target)) == count_leaves(target)
            for target in targets
        )

    return [
        dump(node)
        for node in body
        if not isinstance(node, skip)
        and not (isinstance(node, (ast.Assign, ast.AnnAssign)) and binds_names(node))
    ]


def count_leaves(target):
    """How many leaf targets an assignment target has (a name, attribute or subscript each one)."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return sum(count_leaves(item) for item in target.elts)
    if isinstance(target, ast.Starred):
        return count_leaves(target.value)
    return 1


def parse(source):
    if source is None:
        return None
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def http_methods(call, verb):
    """The HTTP methods a registration serves, lower case; None = unknown (any)."""
    for keyword in call.keywords:
        if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple, ast.Set)):
            return {
                item.value.lower()
                for item in keyword.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
    return {verb} if verb in HTTP_VERBS else None


def routes(tree):
    """(route, endpoint name, first line, last line, methods) per route registration."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in ROUTE_METHODS
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                    and decorator.args[0].value.startswith("/")
                ):
                    found.append(
                        (
                            decorator.args[0].value,
                            node.name,
                            decorator.lineno,
                            decorator.end_lineno,
                            http_methods(decorator, decorator.func.attr),
                        )
                    )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_api_route"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            endpoint = node.args[1] if len(node.args) > 1 else None
            name = endpoint.id if isinstance(endpoint, ast.Name) else None
            found.append(
                (node.args[0].value, name, node.lineno, node.end_lineno, http_methods(node, ""))
            )
    return found


def verbs(tree):
    """(verb words, handler name, first line, last line) for each argparse subcommand."""
    found = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        subs = {arg.arg: () for arg in function.args.args if "sub" in arg.arg}
        parsers = {}
        records = []
        calls = sorted(
            (node for node in ast.walk(function) if isinstance(node, ast.Call)),
            key=lambda node: (node.lineno, node.col_offset),
        )
        targets = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if names:
                    targets[id(node.value)] = names[0]
        for call in calls:
            if not isinstance(call.func, ast.Attribute) or not isinstance(
                call.func.value, ast.Name
            ):
                continue
            receiver = call.func.value.id
            target = targets.get(id(call))
            if (
                call.func.attr == "add_parser"
                and receiver in subs
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                words = (*subs[receiver], call.args[0].value)
                records.append([words, None, call.lineno])
                if target:
                    parsers[target] = len(records) - 1
            elif call.func.attr == "add_subparsers" and receiver in parsers and target:
                subs[target] = records[parsers[receiver]][0]
            elif call.func.attr == "set_defaults" and receiver in parsers:
                for keyword in call.keywords:
                    if keyword.arg == "func" and isinstance(keyword.value, ast.Name):
                        records[parsers[receiver]][1] = keyword.value.id
        for index, (words, handler, first) in enumerate(records):
            last = records[index + 1][2] - 1 if index + 1 < len(records) else function.end_lineno
            found.append((words, handler, first, last))
    return found


def unquote_path(name):
    """A path from a diff header: git C-quotes names that hold odd bytes (octal escapes)."""
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        body = name[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        return body.encode("latin-1", "replace").decode("utf-8", "replace")
    return name


def header_path(rest, prefix):
    """The repo path in a `--- a/x` or `+++ b/x` header body, or None."""
    name = unquote_path(rest.rstrip("\t"))
    return name[len(prefix) :] if name.startswith(prefix) else None


def hunk_lines(diff):
    """path -> (removed old-side line numbers, added new-side line numbers) from a -U0 diff.

    Lines inside a hunk are counted off, never read as headers: a removed line whose text
    starts with `-- a/` is the diff line `--- a/...` and must not reset the path (#524)."""
    lines = {}
    path = None
    old_left = new_left = 0
    for line in diff.splitlines():
        if old_left > 0 and line.startswith("-"):
            old_left -= 1
        elif new_left > 0 and line.startswith("+"):
            new_left -= 1
        elif line.startswith("\\"):
            continue
        elif line.startswith("diff --git "):
            path = None
            old_left = new_left = 0
        elif line.startswith("+++ "):
            found = header_path(line[4:], "b/")
            path = found if found is not None else path
        elif line.startswith("--- "):
            found = header_path(line[4:], "a/")
            path = found if found is not None else path
        elif path and (match := re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)):
            old, new = lines.setdefault(path, (set(), set()))
            old_start, old_count = int(match[1]), int(match[2] if match[2] is not None else 1)
            new_start, new_count = int(match[3]), int(match[4] if match[4] is not None else 1)
            old.update(range(old_start, old_start + old_count))
            new.update(range(new_start, new_start + new_count))
            old_left, new_left = old_count, new_count
    return lines


class Report:
    def __init__(self):
        self.owners = {}  # path -> suite -> [reasons]
        self.broad = []  # (path, reason, count)
        self.files = []
        self.idle = set()  # owners named but never enforced (IDLE_ONLY)
        self.unowned = []  # changed `extra` paths with no owning suite (#524)

    def add(self, path, suites, reason):
        for suite in suites:
            self.owners.setdefault(path, {}).setdefault(suite, [])
            if reason not in self.owners[path][suite]:
                self.owners[path][suite].append(reason)

    def suites(self):
        return sorted({suite for owned in self.owners.values() for suite in owned})

    def reasons(self, suite):
        return [
            (path, owned[suite]) for path, owned in sorted(self.owners.items()) if suite in owned
        ]

    def selector(self):
        return " or ".join(self.suites())

    def as_dict(self):
        return {
            "files": self.files,
            "owners": {
                path: dict(sorted(owned.items())) for path, owned in sorted(self.owners.items())
            },
            "broad": [{"path": p, "reason": r, "suites": n} for p, r, n in self.broad],
            "idle_only": sorted(self.idle),
            "unowned": list(self.unowned),
            "selector": self.selector(),
        }


def extra(path):
    """A path outside ROOTS that suites read or run (goldens, fixtures, Dockerfile, alembic...)."""
    if path.startswith(ROOTS) or SUITE.fullmatch(path):
        return False
    return (
        path.startswith(EXTRA_PREFIXES)
        or path in EXTRA_NAMES
        or bool(EXTRA_REQUIREMENTS.fullmatch(path))
    )


def candidate(kind, path):
    """Whether a changed path (git status letter, repo path) can have owning suites."""
    return path.startswith(ROOTS) or extra(path) or bool(SUITE.fullmatch(path) and kind != "D")


def derive(root, base=None, head="HEAD", files=None):
    """Owning suites for a diff base..head (or for named files at head). Returns a Report.

    `Report.unowned` lists the changed paths outside ROOTS (see `extra`) that no suite owns
    and that are not reported as broad: nothing would run for them (#524)."""
    report = _derive(root, base, head, files)
    # Review (#524): no suite names an individual migration file, so without this every new
    # alembic revision would FAIL pregate and need a waiver line. The migration suites run the
    # whole chain, so they own any edit under alembic/.
    present = {SUITE.fullmatch(p)[1] for p in list_paths(Path(root), head) if SUITE.fullmatch(p)}
    migration_owners = sorted(MIGRATION_SUITES & present)
    for path in report.files:
        if path.startswith("alembic/") and migration_owners and not report.owners.get(path):
            report.add(path, migration_owners, "alembic revision chain")
    report.unowned = [
        path
        for path in report.files
        if extra(path)
        and not report.owners.get(path)
        and not any(entry[0] == path for entry in report.broad)
    ]
    return report


def _derive(root, base=None, head="HEAD", files=None):
    root = Path(root)
    report = Report()
    all_paths = list_paths(root, head)
    if files is None:
        status = text(git(root, "diff", "--name-status", "--no-renames", "-z", base, head))
        fields = [field for field in status.split("\0") if field]
        changes = list(zip(fields[::2], fields[1::2], strict=True))
    else:
        changes = [("M", path.replace("\\", "/")) for path in files]
    changes = [(kind, path) for kind, path in changes if candidate(kind, path)]
    report.files = [path for _, path in changes]
    if not changes:
        return report
    suite_paths = [path for path in all_paths if SUITE.fullmatch(path)]
    suites = {
        SUITE.fullmatch(path)[1]: COMMENT.sub("", source)
        for path, source in read_many(root, head, suite_paths).items()
        if source is not None
    }
    codes = {name: STRINGS.sub('""', source) for name, source in suites.items()}
    code_words = {name: set(re.findall(r"\w+", source)) for name, source in codes.items()}
    imported = {name: imports(source) for name, source in suites.items()}
    # Names a suite defines itself: a local helper that shares a name is not a reference.
    suite_defs = {
        name: {match[1] or match[2] for match in DEFINES.finditer(source)}
        for name, source in codes.items()
    }
    index_suites = {
        name
        for name, source in suites.items()
        if len(set(re.findall(r"(?<![\w.])app[./](?:\w+[./])*\w+", source))) > INDEX_REFS
    }

    def carrying(pattern, hint=None):
        return {
            name
            for name, source in suites.items()
            if (hint is None or hint in source) and pattern.search(source)
        }

    def record(path, owners, reason):
        report.idle |= owners & IDLE_ONLY
        owners = owners - IDLE_ONLY
        if len(owners) > BROAD:
            report.broad.append((path, reason, len(owners)))
        else:
            report.add(path, sorted(owners), reason)

    python = [path for _, path in changes if path.endswith(".py") and not SUITE.fullmatch(path)]
    trees = {}
    lines = {}
    old_sources = new_sources = {}
    if files is None:
        others = [path for kind, path in changes if kind == "M" and not SUITE.fullmatch(path)]
        old_sources = read_many(root, base, others)
        new_sources = read_many(root, head, others)
    if files is None and python:
        old_sources.update(read_many(root, base, [p for p in python if p not in old_sources]))
        new_sources.update(read_many(root, head, [p for p in python if p not in new_sources]))
        trees = {path: (parse(old_sources[path]), parse(new_sources[path])) for path in python}
        diff = git(root, "diff", "-U0", "--no-renames", "--no-ext-diff", base, head, "--", *python)
        lines = hunk_lines(text(diff))

    # Rule (a) and the changed-suite rule.
    namers = {}
    for kind, path in changes:
        suite = SUITE.fullmatch(path)
        if suite:
            if suite[1] in suites:
                report.add(path, [suite[1]], "changed suite")
            continue
        importers = set()
        module = dotted(path)
        stem = Path(path).stem
        if module and module not in SCAFFOLD_MODULES:
            dotted_re = re.compile(r"(?<![\w.])" + re.escape(module) + r"(?!\w)")
            importers |= {name for name, names in imported.items() if module in names}
            importers |= carrying(dotted_re, module)
            if stem == "__main__":
                package = module.rsplit(".", 1)[0]
                importers |= carrying(
                    re.compile(r"""-m["',\s]+""" + re.escape(package) + r"(?![\w.])"), package
                )
        readers = set()
        if module != "app.main":
            parts = unique_suffix(path, all_paths)
            readers = carrying(path_regex(parts), parts[-1])
            if path.endswith(".py") and not path.startswith("app/") and stem.isidentifier():
                folder = path.split("/")[:-1]
                local = {name for name, names in imported.items() if stem in names}
                if path.startswith("tests/_"):
                    importers |= local
                elif folder and local:
                    importers |= {
                        name for name in local if path_regex(folder[-1:]).search(suites[name])
                    }
        namers[path] = importers | readers
        old_tree, new_tree = trees.get(path, (None, None))
        # An edited module: a suite that merely imports it owns it only when module-level code
        # outside any definition changed. Otherwise the changed definitions, routes and verbs
        # below decide, because they say what each importer actually reaches.
        if (
            kind == "M"
            and old_tree is not None
            and new_tree is not None
            and residue(old_tree) == residue(new_tree)
        ):
            importers = set()
        if (
            kind == "M"
            and not extra(path)
            and old_sources.get(path) is not None
            and new_sources.get(path) is not None
        ):
            # A suite that reads an edited file as text owns it when it quotes text the edit
            # removed; an edit elsewhere in a long document cannot break a substring check.
            # Python sources also carry count ratchets (#186): a suite pins how many times a
            # text appears in a named file, as ("app/x.py", "prof.base_url"): 12 or through
            # .count("..."). A text quoted in that shape whose count changed counts too; any
            # other quoted word ("password", "document") changing count is not ownership.
            before, after = old_sources[path], new_sources[path]
            here = path_regex(unique_suffix(path, all_paths))
            readers = {
                name
                for name in readers
                if any(
                    literal in before and literal not in after for literal in quoted(suites[name])
                )
                or (
                    path.endswith(".py")
                    and any(
                        before.count(literal) != after.count(literal)
                        for literal in sorted(ratchet_literals(suites[name], here))
                    )
                )
            }
        owners = importers | readers
        if kind not in {"A", "D"}:
            # An index suite (one that names most of the tree) only cares that a file exists.
            owners = owners - index_suites
        if len(owners - IDLE_ONLY) > NARROW:
            # Widely named: most namers use it as scaffolding. Its owners come from the
            # changed symbols, routes and verbs below, which say what each suite reaches.
            report.broad.append((path, f"module {module or path}", len(owners - IDLE_ONLY)))
        else:
            record(path, owners, f"module {module or path}")
    if not trees:
        return report

    # Rules (b) and (c).
    sides = {
        path: tuple(definitions(tree) if tree else {} for tree in pair)
        for path, pair in trees.items()
    }
    removed_dumps = {}
    added_dumps = {}
    changed = {}
    for path, (old, new) in sides.items():
        names = {
            name for name in old.keys() | new.keys() if dump(old.get(name)) != dump(new.get(name))
        }
        changed[path] = names
        for name in names:
            if name in old:
                removed_dumps.setdefault((name, dump(old[name])), set()).add(path)
            if name in new:
                added_dumps.setdefault((name, dump(new[name])), set()).add(path)
    # A definition that left one file and arrived unchanged in another is a move, not a change.
    moved = {
        key
        for key in removed_dumps.keys() & added_dumps.keys()
        if removed_dumps[key] != added_dumps[key]
    }
    for path, (old, new) in sides.items():
        changed[path] = {
            name
            for name in changed[path]
            if (name, dump(old.get(name))) not in moved and (name, dump(new.get(name))) not in moved
        }
    sources = [
        path
        for path in all_paths
        if path.endswith(".py") and path.startswith(("app/", "scripts/", "integrations/"))
    ]
    defined = {}
    for source in read_many(root, head, sources).values():
        for match in DEFINES.finditer(source or ""):
            name = match[1] or match[2]
            defined[name] = defined.get(name, 0) + 1
    stems = {Path(path).stem for path in all_paths if path.endswith(".py")}

    def anchored(suite, path, qualname):
        """The suite reaches this definition through its class or its module, not by a word."""
        leaf = qualname.rsplit(".", 1)[-1]
        if "." in qualname:
            return qualname.split(".", 1)[0] in code_words[suite]
        module = dotted(path)
        stem = Path(path).stem
        if module and (
            f"{module}.{leaf}" in imported[suite] or f"{module}.{leaf}" in suites[suite]
        ):
            return True
        aliases = {stem, *re.findall(r"\b" + re.escape(stem) + r"\s+as\s+(\w+)", suites[suite])}
        return any(f"{alias}.{leaf}" in codes[suite] for alias in aliases)

    def reaching(path, qualname, endpoints):
        """Suites that use a definition, or None when its name cannot be told from a word."""
        leaf = qualname.rsplit(".", 1)[-1]
        core = leaf.strip("_")
        if len(core) < MIN_NAME or core.lower() in COMMON or leaf.startswith("__"):
            return None
        if leaf in endpoints:
            return None  # a route handler is owned through its route (rule c)
        distinctive = (
            "." not in qualname
            and ("_" in core or re.search(r"[a-z][A-Z]", core) is not None)
            and defined.get(leaf, 0) <= 1
            and leaf not in stems
        )
        return {
            suite
            for suite, found in code_words.items()
            if leaf in found
            and ((distinctive and leaf not in suite_defs[suite]) or anchored(suite, path, qualname))
        }

    for path in trees:
        old_tree, new_tree = trees[path]
        endpoints = set()
        for tree in (old_tree, new_tree):
            if tree is not None:
                endpoints |= {endpoint for _, endpoint, _, _, _ in routes(tree)}
        old, new = sides[path]
        edited = {
            name
            for name in changed[path] & old.keys() & new.keys()
            if not registration_only(old[name], new[name])
        }
        reach = {}
        for qualname in sorted(changed[path]):
            terms = changed_terms(old.get(qualname), new.get(qualname))
            if terms:
                # A literal table changed: its changed ENTRIES are the API, not its name.
                table = qualname.rsplit(".", 1)[-1]
                for term in terms:
                    pattern = re.compile(r"""["']""" + re.escape(term) + r"""["']""")
                    owners = carrying(pattern, term)
                    if re.fullmatch(r"[A-Za-z]+", term):
                        # A plain word ("vendor") is everywhere; the suite must name the table.
                        owners = {suite for suite in owners if table in code_words[suite]}
                    record(path, owners, f"entry {qualname}[{term!r}]")
                continue
            if registration_only(old.get(qualname), new.get(qualname)):
                continue
            reach[qualname] = reaching(path, qualname, endpoints)
            if reach[qualname] is not None:
                record(path, reach[qualname], f"symbol {qualname}")
        for caller, callee in callers(new, edited):
            if reach.get(callee) is not None and len(reach[callee]) > BROAD:
                continue  # the callee is scaffolding; reaching it through a caller is too
            owners = reaching(path, caller, endpoints)
            if owners is not None:
                record(path, owners, f"symbol {caller} (calls {callee})")
        removed, added = lines.get(path, (set(), set()))
        names = {name.rsplit(".", 1)[-1] for name in changed[path]}
        touched = {}
        for tree, span in ((old_tree, removed), (new_tree, added)):
            if tree is None:
                continue
            for route, endpoint, first, last, methods in routes(tree):
                if endpoint in names or span.intersection(range(first, last + 1)):
                    known = touched.get(f"route {route}", (route, set()))[1]
                    merged = None if methods is None or known is None else known | methods
                    touched[f"route {route}"] = (route, merged)
            if path.startswith("app/cli/"):
                for words, handler, first, last in verbs(tree):
                    if len(words) >= 2 and (
                        handler in names or span.intersection(range(first, last + 1))
                    ):
                        touched["verb " + " ".join(words)] = words
        for reason, value in sorted(touched.items()):
            if reason.startswith("route "):
                value, methods = value
                literal = re.sub(r"\{[^}]*\}", "", value)
                if value in SCAFFOLD_ROUTES or len(re.findall(r"[A-Za-z]", literal)) < 4:
                    continue
                longest = max(re.split(r"\{[^}]*\}", value), key=len)
                pattern = route_regex(value)
                owners = {
                    suite
                    for suite in carrying(pattern, longest)
                    if drives(suites[suite], pattern, methods)
                }
            else:
                owners = carrying(verb_regex(value), value[-1])
            record(path, owners, reason)
    return report


def keyword_names(suite):
    """The names pytest -k matches for one wrapper item (substring, case-insensitive)."""
    return [f"test_verify_suite[{suite}]", "test_verify_suites.py", "tests", "parametrize"]


def collected(selection, suites):
    """The suites a -k expression selects, using pytest's own parser when it is importable."""
    try:
        from _pytest.mark.expression import Expression
    except ImportError:
        Expression = None
    if Expression is not None:
        expression = Expression.compile(selection)

        def evaluate(suite):
            names = [name.lower() for name in keyword_names(suite)]
            return expression.evaluate(
                lambda keyword, *_: any(keyword.lower() in name for name in names)
            )

        return {suite for suite in suites if evaluate(suite)}
    tokens = re.findall(r"\(|\)|[^\s()]+", selection)

    def matcher(suite):
        names = [name.lower() for name in keyword_names(suite)]
        position = 0

        def peek():
            return tokens[position] if position < len(tokens) else None

        def take():
            nonlocal position
            position += 1
            return tokens[position - 1]

        def either():
            value = both()
            while peek() == "or":
                take()
                value = both() or value
            return value

        def both():
            value = negation()
            while peek() == "and":
                take()
                value = negation() and value
            return value

        def negation():
            if peek() == "not":
                take()
                return not negation()
            if peek() == "(":
                take()
                value = either()
                take()
                return value
            word = take().lower()
            return any(word in name for name in names)

        return either()

    return {suite for suite in suites if matcher(suite)}


def default_base(root):
    for ref in ("main", "origin/main"):
        try:
            return text(git(root, "merge-base", ref, "HEAD")).strip()
        except ValueError:
            continue
    raise ValueError("no main branch to diff against; pass a RANGE")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("range", nargs="?", help="BASE..HEAD (default: merge-base with main..HEAD)")
    parser.add_argument("--files", nargs="+", help="name files instead of a diff")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--selector", help="report owners this -k expression would not collect")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.files:
        report = derive(root, files=args.files)
        label = "named files"
    else:
        base, _, head = (args.range or "").partition("..")
        base = base or default_base(root)
        head = head or "HEAD"
        report = derive(root, base, head)
        label = f"{base}..{head}"
    missing = []
    if args.selector is not None:
        missing = sorted(set(report.suites()) - collected(args.selector, report.suites()))
    if args.json:
        data = report.as_dict()
        if args.selector is not None:
            data["missing"] = missing
        print(json.dumps(data, indent=2))
        return 1 if missing or report.unowned else 0
    print(
        f"Suite owners for {label}: {len(report.files)} candidate files, "
        f"{len(report.owners)} owned, {len(report.suites())} owning suites"
    )
    for path, owned in sorted(report.owners.items()):
        print(f"\n{path}")
        width = max(len(suite) for suite in owned)
        for suite, reasons in sorted(owned.items()):
            print(f"  {suite:<{width}}  {'; '.join(reasons)}")
    for path, reason, count in report.broad:
        print(f"\nbroad, not enforced: {path}: {reason} is carried by {count} suites")
    for suite in sorted(report.idle):
        print(f"\nidle box only, not enforced: {suite} (run it alone on an idle machine)")
    for path in report.unowned:
        print(f"\nNO OWNING SUITE: {path} (no suite names it, so nothing would run for it)")
    if report.suites():
        print(f'\n-k "{report.selector()}"')
    else:
        print("\nNo owning suites.")
    if args.selector is not None:
        print("\nMissing from --selector: " + (", ".join(missing) if missing else "none"))
    return 1 if missing or report.unowned else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"suite_owners: {type(error).__name__}: {error}")
        raise SystemExit(2) from None
