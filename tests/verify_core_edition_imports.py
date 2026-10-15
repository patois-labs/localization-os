"""Open-core guard: no CORE module imports a PAID module at module level (offline, AST only).

The paid add-on code is listed in ONE place, `app/editions.py` (PAID_MODULES). The open core must
boot and run with that code absent, so a core module may reach a paid module only lazily (inside
a function body, behind the extension registry in `app/extensions.py`) or inside a module-level
try block that catches ImportError. A plain top-level `from app.services import llmemory` in a
core file would crash `import app.main` the moment the paid file is deleted; this suite finds it.

  [A] the scanner is not vacuous: planted violations (absolute, relative, kernel-alias and
      `from pkg import name` forms) are all reported, and the safe forms (lazy, try/except
      ImportError, a paid file importing a paid file) are not.
  [B] the PAID list is well-formed, every paid module is classified once, and NONE of them is in
      the public tree (they live in the private add-on repo).
  [C] no CORE module imports a PAID module at module level in the real tree.
  [D] the lazy reaches into paid code from core files are listed (informational) and may only
      shrink: LAZY_BASELINE pins the count so a NEW lazy reach has to be a deliberate edit.
  [E] `import app.main` + building the CLI parser load no paid module, in a fresh interpreter.

ASCII-only prints. Final line: `CORE EDITION IMPORTS OK`.
"""

import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

init_env("loctms_coreedition_")

from app import editions

#: Lazy (function-body) reaches from a CORE file into PAID code, counted per (file, module).
#: Ratchet: this number only goes DOWN. A new lazy reach must go through app/extensions.py or be
#: justified by raising this number in the same change.
LAZY_BASELINE = 3  # S2: 76 -> 3 once the paid code left (metrics route + 2 Enterprise preflights)


def _alias(dotted: str) -> str:
    """app.kernel.X is an alias shim for app.services.X (app/kernel/__init__.py)."""
    if dotted.startswith("app.kernel."):
        return "app.services." + dotted[len("app.kernel.") :]
    return dotted


def _module_name(root: str, path: str) -> str:
    rel = os.path.relpath(path, root)[: -len(".py")].replace(os.sep, ".")
    return rel[: -len(".__init__")] if rel.endswith(".__init__") else rel


def _resolve_from(node: ast.ImportFrom, modname: str, is_pkg: bool) -> list[str]:
    """Every dotted name an ImportFrom can bind: the module itself and module.name per alias."""
    if node.level:
        base_parts = modname.split(".")
        if not is_pkg:
            base_parts = base_parts[:-1]
        if node.level > 1:
            base_parts = base_parts[: len(base_parts) - (node.level - 1)]
        base = ".".join(base_parts + ([node.module] if node.module else []))
    else:
        base = node.module or ""
    out = [base] if base else []
    for a in node.names:
        if a.name != "*":
            out.append(f"{base}.{a.name}" if base else a.name)
    return out


def _guards_import_error(node: ast.Try) -> bool:
    for h in node.handlers:
        t = h.type
        names = []
        if t is None:
            return True
        for el in t.elts if isinstance(t, ast.Tuple) else [t]:
            names.append(getattr(el, "id", getattr(el, "attr", "")))
        if {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"} & set(names):
            return True
    return False


def scan_source(src: str, modname: str, is_pkg: bool = False) -> tuple[list, list]:
    """Return (top_level_hits, lazy_hits); each hit is (lineno, paid_dotted_name)."""
    tree = ast.parse(src)
    top: list = []
    lazy: list = []

    def visit(node, in_func: bool, guarded: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(child, True, guarded)
                continue
            g = guarded
            if isinstance(child, ast.Try) and _guards_import_error(child):
                # only the try BODY is protected by the handler
                for stmt in child.body:
                    visit_stmt(stmt, in_func, True)
                for part in (child.handlers, child.orelse, child.finalbody):
                    for stmt in part:
                        visit_stmt(stmt, in_func, guarded)
                continue
            visit_stmt(child, in_func, g)

    def visit_stmt(child, in_func: bool, guarded: bool):
        if isinstance(child, ast.Import):
            names = [a.name for a in child.names]
        elif isinstance(child, ast.ImportFrom):
            names = _resolve_from(child, modname, is_pkg)
        else:
            names = None
        if names is not None:
            hits = sorted({_alias(n) for n in names if editions.is_paid_module(_alias(n))})
            for h in hits:
                if in_func:
                    lazy.append((child.lineno, h))
                elif not guarded:
                    top.append((child.lineno, h))
            return
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            visit(child, True, guarded)
        else:
            visit(child, in_func, guarded)

    visit(tree, False, False)
    return top, lazy


def scan_tree(root: str):
    """Scan every CORE module under root/app. Returns (top_hits, lazy_hits) as lists of
    (module, lineno, paid_name)."""
    top_all: list = []
    lazy_all: list = []
    for dp, _dn, fn in os.walk(os.path.join(root, "app")):
        for f in sorted(fn):
            if not f.endswith(".py"):
                continue
            path = os.path.join(dp, f)
            mod = _module_name(root, path)
            if editions.is_paid_module(mod):
                continue
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            top, lazy = scan_source(src, mod, is_pkg=f == "__init__.py")
            top_all += [(mod, ln, name) for ln, name in top]
            lazy_all += [(mod, ln, name) for ln, name in lazy]
    return top_all, lazy_all


def test_a_scanner_is_not_vacuous():
    print("[A] planted violations are found, safe forms are not")
    bad = (
        "from app.services import llmemory\n"
        "import app.services.advanced\n"
        "from app.kernel import evaluations\n"
        "from . import cmd_eval\n"
        "from app.services.llmemory import select_doc_note\n"
    )
    top, lazy = scan_source(bad, "app.cli.cmd_probe")
    names = {n for _ln, n in top}
    want = {
        "app.services.llmemory",
        "app.services.advanced",
        "app.services.evaluations",
        "app.cli.cmd_eval",
    }
    assert want <= names, names
    assert {ln for ln, _n in top} == {1, 2, 3, 4, 5}, top
    assert lazy == [], lazy
    safe = (
        "from app import extensions\n"
        "try:\n"
        "    from app.services import llmemory\n"
        "except ImportError:\n"
        "    llmemory = None\n"
        "def f():\n"
        "    from app.services import advanced\n"
        "    return advanced\n"
    )
    top, lazy = scan_source(safe, "app.services.probe")
    assert top == [], top
    assert [n for _ln, n in lazy] == ["app.services.advanced"], lazy
    # a try that does NOT catch ImportError protects nothing
    unguarded = "try:\n    from app.services import llmemory\nexcept KeyError:\n    pass\n"
    top, _ = scan_source(unguarded, "app.services.probe")
    assert [n for _ln, n in top] == ["app.services.llmemory"], top
    print("  [A] OK")


def test_b_paid_list_is_well_formed_and_absent_from_the_tree():
    print("[B] every paid module is classified once and is NOT in the public tree")
    seen = {}
    for addon, mods in editions.PAID_MODULES.items():
        for m in mods:
            assert m not in seen, f"{m} listed under {seen[m]} and {addon}"
            seen[m] = addon
            assert all(part.isidentifier() for part in m.split(".")), f"malformed name {m!r}"
            rel = m.replace(".", os.sep)
            # Open core (S2): the add-on code lives in the private repo. A paid file that reappears
            # here is a leak of paid code into the public tree, and a hint someone restored it.
            assert not os.path.isfile(rel + ".py") and not os.path.isdir(rel), (
                f"paid module {m} is present in the public core tree"
            )
            assert not editions.module_installed(m), f"{m} is importable in the core tree"
    for slug, addon in editions.PAID_FEATURE_ADDON.items():
        assert addon in editions.PAID_MODULES, (slug, addon)
        assert not editions.addon_installed(addon), f"add-on {addon} reads installed"
    # The shared libraries the paid features merely use stay core and stay present.
    for core in (
        "app.services.eval_metrics",
        "app.services.quality_lens",
        "app.services.checks",
        "app.services.query_threads",
    ):
        assert not editions.is_paid_module(core), f"{core} must stay core"
        assert os.path.isfile(core.replace(".", os.sep) + ".py"), f"{core} missing"
    print(f"  [B] {len(seen)} paid modules classified, none present OK")


def test_e_core_boots_and_runs_alone():
    print("[E] import app.main and the CLI parser load no paid module")
    import subprocess

    code = (
        "import sys, os\n"
        "import app.main\n"
        "from app.cli.__main__ import build_parser\n"
        "build_parser()\n"
        "from app import editions\n"
        "bad = sorted(m for m in sys.modules if editions.is_paid_module(m))\n"
        "print('PAID_LOADED=' + ','.join(bad))\n"
    )
    env = dict(os.environ)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=_ROOT,
        timeout=300,
    )
    assert out.returncode == 0, out.stderr[-1500:]
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("PAID_LOADED=")][-1]
    assert line == "PAID_LOADED=", line
    print("  [E] OK")


def test_c_no_core_module_imports_paid_at_module_level():
    print("[C] real tree: no module-level import of paid code from core")
    top, lazy = scan_tree(_ROOT)
    for mod, ln, name in top:
        print(f"  VIOLATION {mod}:{ln} imports {name}")
    assert not top, f"{len(top)} core module(s) import paid code at module level"
    print(f"  [C] OK ({len(lazy)} lazy reaches, see [D])")
    return lazy


def test_d_lazy_reaches_only_shrink(lazy):
    print("[D] lazy reaches into paid code do not grow")
    assert len(lazy) <= LAZY_BASELINE, (
        f"lazy core->paid reaches grew to {len(lazy)} (baseline {LAZY_BASELINE}); route the new "
        "one through app/extensions.py or raise LAZY_BASELINE with a reason"
    )
    print(f"  [D] {len(lazy)} <= {LAZY_BASELINE} OK")


if __name__ == "__main__":
    test_a_scanner_is_not_vacuous()
    test_b_paid_list_is_well_formed_and_absent_from_the_tree()
    _lazy = test_c_no_core_module_imports_paid_at_module_level()
    test_d_lazy_reaches_only_shrink(_lazy)
    test_e_core_boots_and_runs_alone()
    print("CORE EDITION IMPORTS OK")
