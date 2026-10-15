"""The /help API reference must not drift from the handlers it describes (docs review, 10-08).

`verify_help` already holds the SET of rows to the SET of served routes (method and path, both
directions). It says so itself: a wrong gate and a wrong parameter are "checked by reading the
route, and nothing automated does it". A hand review on 10-08 found exactly those two kinds of
drift: every /api/vendors row said read/write/manage while the handlers are admin-only, the four
/api/prompt-tap rows said read while the handlers are admin-only, and eight list routes read query
parameters (status, archived, limit, offset, cursor, ...) that their rows never mentioned.

This suite closes both holes mechanically, from the handlers themselves:

  GATE   For every documented /api route, walk the handler's AST (and the app.web helpers it
         calls, three levels deep) for the role-block helpers (_admin_block, _manage_block,
         _ai_block, ...). The row's Gate cell must be at least as strong as the strongest block
         the handler can reach (read < write < tune-ai < manage < admin). A row that
         under-states its gate fails. Branch-only gates (a block that guards one optional
         parameter, not the route) are listed in GATE_ALLOWLIST with the reason.

  QUERY  Every query parameter the handler declares in its signature, or reads with
         request.query_params in its own body (.get / .getlist / ["x"] / "x" in, directly or
         through an alias such as qp = request.query_params), must appear as a word in the row's
         text. Parameters that are deliberately not documented go in QUERY_ALLOWLIST with the
         reason.

Both checks are mutation-proven: a forged row (gate weakened, parameter name removed) must make
the checker report a problem, so the guard cannot go soft. Allowlists shrink only; an entry that
no longer matches a real route, or that the check no longer needs, is itself an error.

ASCII-only prints.
"""

import ast
import html
import inspect
import linecache
import os
import re
import sys
import textwrap
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_help_api_drift_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.web._routes import walk_routes

# Role-block helper -> the lowest role that passes it. read < write < tune-ai < manage < admin.
RANKS = {"read": 0, "write": 1, "tune-ai": 2, "manage": 3, "admin": 4}
BLOCKS = {
    "_write_block": "write",
    "_write_role_block": "write",
    "_api_write_block": "write",
    "_ai_block": "tune-ai",
    "_ai_role_block": "tune-ai",
    "_manage_block": "manage",
    "_admin_block": "admin",
    "_gate_admin": "admin",
}

#: Gate-cell words and the rank they promise. A scope label (docs, keys, api) is a credential
#: grant, not a role, so it counts as the write rung: it satisfies a handler whose strongest
#: block is _write_block, and fails one that needs tune-ai, manage or admin unless the cell
#: names that role too.
CELL_WORDS = (
    ("admin", 4),
    ("manage", 3),
    ("tune-ai", 2),
    ("reviewer", 1),
    ("write", 1),
    ("linguist", 2),
    ("docs", 1),
    ("keys", 1),
    ("api", 1),
)

#: (METHOD, path) -> reason. The handler reaches a stronger block than the row states, on purpose.
GATE_ALLOWLIST = {
    ("GET", "/api/glossaries"): "the manage block guards only the optional unassigned=1 view",
    ("GET", "/api/connectors/{cid}/airtable/webhook"): (
        "shared _conn_gate(role=False): the role block is skipped for this read-only route"
    ),
    ("GET", "/api/workflows/{wid}/system-prompts"): (
        "shared _wf_gate: the manage block applies only to write=True (#630: admin or manager), "
        "reads use can_tune_ai or can_manage"
    ),
    ("GET", "/api/workflows/{wid}/system-prompts/{key}"): "same shared _wf_gate, read branch",
    (
        "GET",
        "/api/workflows/{wid}/system-prompts/{key}/history",
    ): "same shared _wf_gate, read branch",
}

#: (METHOD, path) -> {param: reason}. Query parameters the row deliberately does not name.
QUERY_ALLOWLIST: dict = {}
#: Signature names that are never query parameters.
NOT_QUERY = {"request", "response", "background_tasks", "session"}


def _helper_calls(fn, depth, seen, out):
    if fn in seen or depth < 0:
        return
    seen.add(fn)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name in BLOCKS:
            out.add(BLOCKS[name])
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id not in BLOCKS
        ):
            target = getattr(fn, "__globals__", {}).get(node.func.id)
            if isinstance(target, types.FunctionType) and target.__module__.startswith("app.web"):
                _helper_calls(target, depth - 1, seen, out)


def handler_gate(fn) -> str:
    """The strongest role block the handler (or a helper it calls) can reach; 'read' if none."""
    found: set = set()
    _helper_calls(fn, 3, set(), found)
    return max(found, key=RANKS.__getitem__) if found else "read"


def handler_query_params(fn, path: str) -> set:
    """Query parameters the handler declares in its signature or reads off request.query_params."""
    names = set()
    in_path = set(re.findall(r"{(\w+)}", path))
    for arg, prm in inspect.signature(fn).parameters.items():
        if arg in in_path or arg in NOT_QUERY:
            continue
        ann = str(prm.annotation)
        default = repr(prm.default)
        if any(x in ann for x in ("Request", "UploadFile", "dict", "list", "Session")):
            continue
        if any(x in default for x in ("Depends", "File(", "Form(", "Body(")):
            continue
        names.add(arg)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return names
    # Names bound to the query mapping (`qp = request.query_params`) read exactly like it.
    aliases = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and ast.unparse(node.value).endswith("query_params")
        for t in node.targets
        if isinstance(t, ast.Name)
    }

    def _is_qp(expr) -> bool:
        if isinstance(expr, ast.Name) and expr.id in aliases:
            return True
        return ast.unparse(expr).endswith("query_params")

    def _const(expr):
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value
        return None

    for node in ast.walk(tree):
        # qp.get("x") / qp.getlist("x")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "getlist")
            and node.args
            and _const(node.args[0])
            and _is_qp(node.func.value)
        ):
            names.add(_const(node.args[0]))
        # qp["x"]
        elif isinstance(node, ast.Subscript) and _is_qp(node.value) and _const(node.slice):
            names.add(_const(node.slice))
        # "x" in qp
        elif (
            isinstance(node, ast.Compare)
            and _const(node.left)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and _is_qp(node.comparators[0])
        ):
            names.add(_const(node.left))
        # a helper handed the mapping and a name: _tm_int(qp, "page")
        elif isinstance(node, ast.Call) and any(_is_qp(a) for a in node.args):
            names.update(_const(a) for a in node.args if _const(a))
    return {n for n in names if re.fullmatch(r"[a-z][a-z0-9_]*", n)}


_ROW = re.compile(
    r'<tr>\s*<td><span class="method \w+">(\w+)</span></td>\s*'
    r"<td><code>([^<]+)</code></td>(.*?)</tr>",
    re.S,
)


def documented_rows(page_html: str) -> dict:
    """(METHOD, path) -> (row text, gate cell text) for every live Part 2 row (comments dropped)."""
    page_html = re.sub(r"<!--.*?-->", "", page_html, flags=re.S)
    rows = {}
    for method, path, rest in _ROW.findall(page_html):
        cells = re.findall(r"<td>(.*?)</td>", rest, re.S)
        gate = re.sub(r"<[^>]+>", " ", cells[-1]) if cells else ""
        text = html.unescape(re.sub(r"<[^>]+>", " ", rest))
        rows[(method.upper(), html.unescape(path).strip())] = (text, html.unescape(gate).lower())
    return rows


def cell_rank(gate: str) -> int:
    return max((rank for word, rank in CELL_WORDS if word in gate), default=0)


def problems_for(page_html: str) -> list:
    rows = documented_rows(page_html)
    handlers = {}
    for route in walk_routes(app):
        path = getattr(route, "path", "") or ""
        if not path.startswith("/api"):
            continue
        for method in getattr(route, "methods", None) or ():
            if method not in ("HEAD", "OPTIONS"):
                handlers[(method, path)] = route.endpoint
    problems = []
    used_gate_allow, used_query_allow = set(), set()
    for key, (text, gate) in sorted(rows.items()):
        fn = handlers.get(key)
        if fn is None:
            continue  # verify_help owns "documented but not served"
        need = handler_gate(fn)
        if cell_rank(gate) < RANKS[need]:
            if key in GATE_ALLOWLIST:
                used_gate_allow.add(key)
            else:
                problems.append(
                    f"gate: {key[0]} {key[1]} documents '{gate.strip()}' but the handler can reach "
                    f"a {need} block"
                )
        allow = QUERY_ALLOWLIST.get(key, {})
        for name in sorted(handler_query_params(fn, key[1])):
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text):
                continue
            if name in allow:
                used_query_allow.add((key, name))
                continue
            problems.append(
                f"query: {key[0]} {key[1]} reads '{name}' but the row never mentions it"
            )
    for key in GATE_ALLOWLIST:
        if key not in handlers:
            problems.append(f"stale GATE_ALLOWLIST entry (no such route): {key}")
        elif key not in used_gate_allow:
            problems.append(f"GATE_ALLOWLIST entry no longer needed, drop it: {key}")
    for key, names in QUERY_ALLOWLIST.items():
        for name in names:
            if key not in handlers:
                problems.append(f"stale QUERY_ALLOWLIST entry (no such route): {key}")
            elif (key, name) not in used_query_allow:
                problems.append(f"QUERY_ALLOWLIST entry no longer needed, drop it: {key} {name}")
    return problems


def main() -> None:
    init_db()
    page = TestClient(app).get("/help")
    assert page.status_code == 200, page.status_code
    page_html = page.text
    rows = documented_rows(page_html)
    assert len(rows) >= 150, f"only {len(rows)} Part 2 rows parsed: the extractor stopped reading"

    problems = problems_for(page_html)
    assert not problems, "the /help API reference disagrees with its handlers:\n  " + "\n  ".join(
        problems
    )

    # MUTATION PROOF: the checker must notice a weakened gate and a dropped parameter.
    # 1. Vendors are admin-only; a row that says read must be reported.
    vendors_row = next(
        m
        for m in re.findall(r"<tr>.*?</tr>", page_html, re.S)
        if "<code>/api/vendors</code>" in m and "method get" in m
    )
    forged = page_html.replace(
        vendors_row, re.sub(r"<td>admin</td></tr>$", "<td>read</td></tr>", vendors_row)
    )
    assert forged != page_html, "mutation 1 did not change the page"
    assert any("gate: GET /api/vendors " in p for p in problems_for(forged)), (
        "the gate check does not notice a weakened gate, so it proves nothing"
    )
    # 2. GET /api/projects reads status/archived; a row that never names status must be reported.
    proj_row = next(
        m
        for m in re.findall(r"<tr>.*?</tr>", page_html, re.S)
        if "<code>/api/projects</code>" in m and "method get" in m
    )
    forged = page_html.replace(proj_row, proj_row.replace("status", "state"))
    assert forged != page_html, "mutation 2 did not change the page"
    assert any("query: GET /api/projects reads 'status'" in p for p in problems_for(forged)), (
        "the query check does not notice a dropped parameter, so it proves nothing"
    )
    # 3. Every way a handler reads the query string must be seen: direct .get, an alias
    #    (`qp = request.query_params`, missed before the 10-08 gate), a subscript, `in`,
    #    and a helper handed the mapping plus a name.
    ns: dict = {}
    src = textwrap.dedent(
        """
            def planted(request):
                qp = request.query_params
                a = request.query_params.get("p_direct")
                b = qp.get("p_alias")
                c = qp.getlist("p_list")
                d = request.query_params["p_sub"]
                e = qp["p_alias_sub"]
                f = "p_in" in request.query_params
                g = "p_alias_in" not in qp
                h = int_of(qp, "p_helper")
                return a, b, c, d, e, f, g, h
            """
    )
    fname = "<verify_help_api_drift planted>"
    linecache.cache[fname] = (len(src), None, src.splitlines(True), fname)  # getsource reads it
    exec(compile(src, fname, "exec"), ns)
    found = handler_query_params(ns["planted"], "/api/planted")
    want = {"p_direct", "p_alias", "p_list", "p_sub", "p_alias_sub", "p_in", "p_alias_in"}
    want.add("p_helper")
    assert found == want, f"query-read extractor missed {sorted(want - found)}"
    print(
        "  /help Part 2: %d rows, every gate at least as strong as its handler's role block, "  # noqa: UP031
        "every handler query parameter named (mutation-proven) OK" % len(rows)
    )


if __name__ == "__main__":
    main()
