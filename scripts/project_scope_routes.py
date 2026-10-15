"""Enumerate mounted API ID routes and trace their project visibility guards.

Imported by verify_project_scoping after its disposable database is initialized.
No application imports or database access at module import time.
"""

import ast
import importlib
import inspect
import re
import textwrap
from functools import cache


@cache
def calls(function):
    function = inspect.unwrap(function)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError, IndentationError):
        return ()
    namespace = dict(function.__globals__)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            module = importlib.import_module(node.module)
            for alias in node.names:
                if hasattr(module, alias.name):
                    namespace[alias.asname or alias.name] = getattr(module, alias.name)

    def resolve(node):
        if isinstance(node, ast.Name):
            return namespace.get(node.id)
        if isinstance(node, ast.Attribute):
            return getattr(resolve(node.value), node.attr, None)
        return None

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # #746: a function handed to a worker (`await deps.offload_blocking(fn, ...)`) runs
            # just as surely as a direct call, so a positional function argument counts too.
            for candidate in (node.func, *node.args):
                target = resolve(candidate)
                if inspect.isfunction(target) and target.__module__.startswith("app."):
                    found.append(target)
    return tuple(found)


def reaches_visibility(function):
    pending, seen = [function], set()
    while pending:
        target = pending.pop()
        if target in seen:
            continue
        seen.add(target)
        if target.__module__ == "app.services.access" and target.__name__ in (
            "can_view_project",
            "visible_project_ids",
        ):
            return True
        if target.__name__ not in {
            "_gate",
            "_api_gate",
            "_gate_admin",
            "_r3_gate",
            "_require_doc_scope",
            "_require_key_scope",
            "_require_admin_scope",
            "feature_api_block",
            "_write_block",
            "_manage_block",
            "_admin_block",
            "_ai_block",
            "_role_block",
        }:
            pending.extend(calls(target))
    return False


def inventory(app):
    """Mounted API routes with path IDs or project references in query/body code."""
    from app.web._routes import walk_routes

    rows = []
    for route in walk_routes(app):
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if not path.startswith("/api/") or not endpoint:
            continue
        ids = re.findall(r"\{([^}:]+)(?::[^}]+)?\}", path)
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(endpoint))))
        references = {
            "project",
            "project_id",
            "source_project_id",
            "project_ids",
            "document",
            "document_id",
            "document_ids",
            "doc_id",
            "segment_id",
            "segment_ids",
            "run_id",
            "mission_id",
            "key_id",
            "key_ids",
            "job_id",
            "tm_id",
            "glossary_id",
            "batch_id",
        }
        names = {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        if not ids and not names.intersection(references):
            continue
        rows.append((path, sorted(route.methods), ids, reaches_visibility(endpoint)))
    return sorted(rows)


def missing_callers(root):
    """Request adapters must explicitly forward users to trusted Advanced service APIs."""
    names = {
        "create_mission",
        "get_mission",
        "list_missions",
        "run_mission_now",
        "get_run",
        "list_runs",
        "transcript_view",
        "staged_target_map",
        "request_cancel",
        "delete_run",
        "active_target_locales",
        "estimate_rules_arm",
        "run_rules_arm",
    }
    problems = []
    for path in (root / "app" / "web").glob("*.py"):
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "advanced"
                and node.func.attr in names
                and not any(k.arg == "user" for k in node.keywords)
            ):
                problems.append(f"{path.name} line {node.lineno}: caller not forwarded")
    return problems


def report(app):
    from app.web._routes import walk_routes

    rows = inventory(app)
    print(f"Mounted routes: {len(list(walk_routes(app)))}; API ID routes: {len(rows)}")
    for path, methods, _ids, guarded in rows:
        print(f"{'guarded' if guarded else 'REVIEW'} {'/'.join(methods)} {path}")
    return rows


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests._bootstrap import init_env

    init_env("loctms_scope_inventory_")
    from app.main import app

    report(app)
