"""Mechanical argparse registration decomposition and router assembly."""

import ast
import collections
from pathlib import Path


def register(source, owners, old, require):
    tree = ast.parse(source)
    node = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "register"), None
    )
    require(node is not None, "register_mode needs register")
    lines = source.splitlines(keepends=True)
    parameter = node.args.args[0].arg
    groups = {parameter}
    blocks, pending = [], []
    for statement in node.body:
        calls = [n for n in ast.walk(statement) if isinstance(n, ast.Call)]
        starts = [
            n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == "add_parser"
        ]
        ends = [
            n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == "set_defaults"
        ]
        for call in calls:
            if isinstance(call.func, ast.Attribute) and call.func.attr == "add_subparsers":
                require(
                    isinstance(statement, ast.Assign) and len(statement.targets) == 1,
                    "unsupported nested group assignment",
                )
                groups.add(statement.targets[0].id)
        if starts:
            require(len(starts) == 1, "ambiguous parser block")
            if pending and any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"add_parser", "add_subparsers"}
                for statement in pending
                for n in ast.walk(statement)
            ):
                blocks.append((old, None, pending))
                pending = []
        pending.append(statement)
        if ends:
            handlers = [k.value for call in ends for k in call.keywords if k.arg == "func"]
            require(
                len(handlers) == 1 and isinstance(handlers[0], ast.Name),
                f"register handler must be a simple name at line {statement.lineno}",
            )
            target = owners.get(handlers[0].id, old)
            calls = [
                n for statement in pending for n in ast.walk(statement) if isinstance(n, ast.Call)
            ]
            parser = next(
                (
                    n
                    for n in calls
                    if isinstance(n.func, ast.Attribute) and n.func.attr == "add_parser"
                ),
                None,
            )
            require(
                parser is not None and isinstance(parser.func.value, ast.Name),
                "unsupported parser block",
            )
            group = parser.func.value.id
            require(group in groups, f"unknown parser group {group}")
            blocks.append((target, group, pending))
            pending = []
    if pending:
        blocks.append((old, None, pending))
    # Local variables must not leak between owners. The parser scratch variable
    # is reset by every add_parser; group objects are explicit parameters.
    local_owners = {}
    for target, _group, statements in blocks:
        for stmt in statements:
            for name in ast.walk(stmt):
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                    local_owners.setdefault(name.id, set()).add(target)
    for target, group, statements in blocks:
        assigned = {
            n.id
            for s in statements
            for n in ast.walk(s)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }
        loaded = {
            n.id
            for s in statements
            for n in ast.walk(s)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        for name in (loaded - assigned - {group}) & local_owners.keys():
            require(local_owners[name] == {target}, f"register blocks share local state: {name}")
    # Keep every contiguous run at its original position, including interleaving.
    additions, replacements, counts = {}, [], {}
    index = 0
    while index < len(blocks):
        target, group, statements = blocks[index]
        index += 1
        if target == old:
            continue
        statements = list(statements)
        while index < len(blocks) and blocks[index][:2] == (target, group):
            statements.extend(blocks[index][2])
            index += 1
        stem = "register_" + Path(target).stem + ("_" + group if group != parameter else "")
        counts[stem] = counts.get(stem, 0) + 1
        name = stem + (f"_{counts[stem]}" if counts[stem] > 1 else "")
        first, last = statements[0].lineno - 1, statements[-1].end_lineno
        body = "".join(lines[first:last])
        additions.setdefault(target, []).append((name, f"def {name}({group}) -> None:\n" + body))
        replacements.append((first, last, f"    {name}({group})\n"))
    for first, last, replacement in reversed(replacements):
        lines[first:last] = [replacement]
    return "".join(lines), additions


def routers(tree):
    return {
        n.targets[0].id: n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and isinstance(n.value, ast.Call)
        and ast.unparse(n.value.func) == "APIRouter"
    }


def register_matches(before, sources):
    original = next(n for n in ast.parse(before).body if getattr(n, "name", None) == "register")
    functions = {
        n.name: n
        for source in sources.values()
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef)
    }
    expanded = []
    for node in functions["register"].body:
        call = node.value if isinstance(node, ast.Expr) else None
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id.startswith("register_")
            and call.func.id in functions
        ):
            helper = functions[call.func.id]
            if (
                len(call.args) != 1
                or not isinstance(call.args[0], ast.Name)
                or len(helper.args.args) != 1
                or helper.args.args[0].arg != call.args[0].id
            ):
                return False
            expanded.extend(helper.body)
        else:
            expanded.append(node)
    return [ast.dump(n) for n in expanded] == [ast.dump(n) for n in original.body]


def register_bytes(sources):
    return {
        n.name: [ast.get_source_segment(source, statement) for statement in n.body]
        for source in sources.values()
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("register_")
    }


def route_names(node, names):
    return {
        d.func.value.id
        for d in getattr(node, "decorator_list", [])
        if isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and isinstance(d.func.value, ast.Name)
        and d.func.value.id in names
    }


def route_setup(tree, owners, old, require):
    definitions = routers(tree)
    reserved = {"_srp_router_options", "_srp_piece_router", "_srp_copy"} | {
        Path(path).stem + "_" + name
        for path in set(owners.values()) - {old}
        for name in definitions
    }
    require(
        not reserved & owners.keys(),
        f"generated router binding collision: {sorted(reserved & owners.keys())}",
    )
    used = {}
    for node in tree.body:
        target = owners.get(getattr(node, "name", None), old)
        for name in route_names(node, definitions):
            used.setdefault(target, set()).add(name)
    return definitions, used


def route_runs(tree, owners, old):
    result = {}
    for name in routers(tree):
        runs = []
        for node in tree.body:
            if name not in route_names(node, {name}):
                continue
            target = owners[node.name]
            count = sum(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name)
                and d.func.value.id == name
                for d in node.decorator_list
            )
            if runs and runs[-1][1] == target:
                runs[-1][2] += count
            else:
                runs.append([node, target, count])
        offsets = {}
        for node, target, count in runs:
            if target == old:
                continue
            start = offsets.get(target, 0)
            end = start + count
            offsets[target] = end
            sliced = sum(t == target for _, t, _ in runs) > 1
            result.setdefault(node.lineno, []).append(
                (name, target, (start, end) if sliced else None)
            )
    return result


def include(name, alias, subset=None):
    fields = ", ".join(f"{name}.{field}" for field in ("prefix", "tags", "dependencies"))
    body = (
        f"_srp_router_options = {fields}\n"
        f"{fields} = '', [], []\n"
        f"{name}.include_router({alias})\n"
        f"{fields} = _srp_router_options\n"
    )
    if subset is not None:
        first, last = subset
        body = (
            f"_srp_piece_router = {alias}\n"
            f"{alias} = _srp_copy({alias})\n"
            f"{alias}.routes = {alias}.routes[{first}:{last}]\n"
            + body
            + f"{alias} = _srp_piece_router\n"
        )
    return body


def statements_match(before, sources, module_statements, router_mode=False, replicate=False):
    """Permit only validated router assembly and exact logger replicas."""
    from .units import logger

    expected = module_statements(before)
    actual = collections.Counter()
    definitions = routers(ast.parse(before)) if router_mode else {}
    permitted = {ast.dump(n) for n in definitions.values()}
    if replicate:
        permitted.update(ast.dump(n) for n in ast.parse(before).body if logger(n))
    for source in sources.values():
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        aliases = {
            a.asname: a.name
            for n in tree.body
            if isinstance(n, ast.ImportFrom)
            for a in n.names
            if a.name in definitions and a.asname
        }
        spans = []
        for node in tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if not isinstance(call.func, ast.Attribute) or call.func.attr != "include_router":
                continue
            if not (
                isinstance(call.func.value, ast.Name)
                and call.func.value.id in definitions
                and len(call.args) == 1
                and isinstance(call.args[0], ast.Name)
            ):
                continue
            name, alias = call.func.value.id, call.args[0].id
            if aliases.get(alias) != name:
                continue
            position = tree.body.index(node)
            subset = None
            first, last = position - 2, position + 2
            if position >= 5:
                selection_node = tree.body[position - 3]
                value = getattr(selection_node, "value", None)
                if isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Slice):
                    selection = value.slice
                    if isinstance(selection.lower, ast.Constant) and isinstance(
                        selection.upper, ast.Constant
                    ):
                        subset = (selection.lower.value, selection.upper.value)
                        first, last = position - 5, position + 3
            candidates = tree.body[first:last]
            expected_nodes = ast.parse(include(name, alias, subset)).body
            if [ast.dump(n) for n in candidates] == [ast.dump(n) for n in expected_nodes]:
                spans.append((candidates[0].lineno - 1, candidates[-1].end_lineno))
        for first, last in reversed(spans):
            del lines[first:last]
        actual += module_statements("".join(lines))
    for statement in permitted:
        if actual[statement] >= expected[statement]:
            actual[statement] = expected[statement]
    return actual == expected
