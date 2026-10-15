"""Fail-on-absence inventory of connector config writers, including their input handling."""

import ast
import hashlib


def inventory(root):
    result = {}
    for path in sorted((root / "app/services/connectors").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            writes = (
                isinstance(node, ast.Attribute)
                and node.attr == "config"
                and isinstance(node.ctx, ast.Store)
            )
            constructs = isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "Connection")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "Connection")
                or (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "setattr"
                    and len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "config"
                )
            )
            subscript = node
            while isinstance(subscript, ast.Subscript):
                subscript = subscript.value
            item_write = (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Store)
                and isinstance(subscript, ast.Attribute)
                and subscript.attr == "config"
            )
            mutates = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and (
                    (
                        isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "config"
                        and node.func.attr
                        in {"update", "pop", "setdefault", "clear", "__setitem__"}
                    )
                    or (
                        node.func.attr == "values" and any(k.arg == "config" for k in node.keywords)
                    )
                )
            )
            if not (writes or constructs or item_write or mutates):
                continue
            parent = node
            while parent in parents and not isinstance(
                parent, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                parent = parents[parent]
            label = f"{path.name}:{getattr(parent, 'name', '<module>')}"
            digest = hashlib.sha256(ast.dump(parent).encode()).hexdigest()
            result.setdefault(label, []).append(digest)
    return result
