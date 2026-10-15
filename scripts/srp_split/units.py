"""Module binding units and dependency closure (no application imports)."""

import ast
import symtable
from pathlib import Path

COMPOUNDS = (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try, ast.If, ast.While, ast.Match)


def compound_bound(node):
    table = symtable.symtable(ast.unparse(node), "<unit>", "exec")
    return {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported()}


def index(tree, bound, require):
    nodes, duplicates = {}, set()
    for node in tree.body:
        names = bound(node)
        for name in names:
            previous = nodes.get(name)
            if previous is not None:
                identical = isinstance(node, (ast.Import, ast.ImportFrom)) and (
                    ast.dump(previous) == ast.dump(node)
                )
                constant = (
                    name.isupper()
                    and isinstance(node, (ast.Assign, ast.AnnAssign))
                    and (
                        isinstance(previous, (ast.Assign, ast.AnnAssign))
                        and bound(previous) == names
                    )
                )
                # Unaliased dotted imports all bind the same root package, but
                # load different submodules; keep each distinct import.
                package = all(
                    isinstance(item, ast.Import)
                    and all(
                        a.asname is None
                        for a in item.names
                        if (a.asname or a.name.split(".")[0]) == name
                    )
                    for item in (previous, node)
                )
                require(identical or constant or package, f"duplicate top-level binding: {name}")
                if identical:
                    duplicates.add(node)
            else:
                nodes[name] = node
    return nodes, duplicates


def logger(node):
    value = getattr(node, "value", None)
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and ast.unparse(value.func) == "logging.getLogger"
    )


def pull(tree, nodes, owners, old, references, bound, require, excluded=(), extra=()):
    """Move each dependency once; other readers share that new owner."""
    replicas = {}
    for name, node in nodes.items():
        if logger(node) and owners[name] != old:
            replicas.setdefault(owners[name], set()).add(name)
            owners[name] = old
    changed = True
    while changed:
        changed = False
        requests = [
            (node, next((owners[n] for n in bound(node) if owners[n] != old), old))
            for node in tree.body
        ]
        for node, target in [*requests, *extra]:
            if target == old:
                continue
            wanted = references(ast.unparse(node)) & nodes.keys()
            for router in wanted & set(excluded):
                wanted |= references(ast.unparse(nodes[router])) & nodes.keys()
            for name in sorted(wanted):
                dependency = nodes[name]
                if (
                    name in excluded
                    or owners[name] != old
                    or isinstance(dependency, (ast.Import, ast.ImportFrom))
                ):
                    continue
                if logger(dependency):
                    replicas.setdefault(target, set()).add(name)
                    continue
                require(
                    not isinstance(
                        dependency, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    )
                    or dependency.end_lineno - dependency.lineno + 1 <= 40,
                    f"cannot pull dependency {name} into {target}: exceeds 40 lines "
                    f"({dependency.end_lineno - dependency.lineno + 1}); assign it explicitly",
                )
                for binding in bound(dependency):
                    owners[binding] = target
                print(f"PULL {name}: {old} -> {target}")
                changed = True
    return replicas


def piece_path(path, require):
    require(
        len(Path(path).stem) <= 40,
        f"manifest piece stem exceeds 40 characters: {path}; shorten the generator slug",
    )


def leaf_dependencies(path, cross, pull_deps):
    """Explain each unresolved generated import without silently reassigning it."""
    reason = "already assigned to another piece" if pull_deps else "dependency pulling disabled"
    return "; ".join(
        f"{alias.name} from {node.module}: {reason}"
        for node in ast.parse(cross[path]).body
        for alias in node.names
    )


def comment_segments(source, tree, owners, old, span, checker):
    """Detach checker-owned tokens before any containing assignment is copied."""
    lines = source.splitlines(keepends=True)
    spans = {node: span(node, lines) for node in tree.body}
    definitions = {getattr(node, "name", None): node for node in tree.body}
    detached = {}
    for name, token in checker.attached_comments(source):
        node = definitions[name]
        if owners[name] == old or token.start[0] - 1 >= spans[node][0]:
            continue
        detached[name] = detached.get(name, "") + token.string + "\n"
        index = token.start[0] - 1
        newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
        lines[index] = lines[index][: token.start[1]].rstrip() + newline
    detached[None] = lines
    return spans, detached


def dynamic(source, path, require, offset=0, context=None):
    modules, registries = set(), {"sys.modules"}
    for node in ast.walk(ast.parse(context or source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    modules.add(alias.asname)
                else:
                    parts = alias.name.split(".")
                    modules.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
                if alias.name == "sys":
                    registries.add((alias.asname or "sys") + ".modules")
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            registries.update(a.asname or a.name for a in node.names if a.name == "modules")

    def registry_item(node):
        return isinstance(node, ast.Subscript) and ast.unparse(node.value) in registries

    def module_arg(node):
        # A class/object attribute of a module is not itself a module.
        return ast.unparse(node) in modules or registry_item(node)

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Call, ast.Subscript)):
            continue
        suspect = registry_item(node)
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            suspect |= (
                name == "globals"
                or (name == "vars" and (not node.args or module_arg(node.args[0])))
                or (name == "getattr" and bool(node.args) and module_arg(node.args[0]))
            )
        require(
            not suspect,
            f"dynamic namespace access in {path}: {ast.unparse(node)} "
            f"at line {node.lineno + offset}; split manually",
        )
