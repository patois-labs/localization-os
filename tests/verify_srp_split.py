"""Runner-owned SRP executor fixtures; scratch only in the system temp root.

Git reads/checkout are modelled as a committed snapshot to obey the no-commit
builder contract. Ruff and the checker implementation are real, not stubs.
"""

import argparse
import ast
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import srp_split as split

FIXTURES = ROOT / "tests/fixtures/srp_split"
OLD = "app/x/old.py"
LEAF = "app/x/leaf.py"
OPS = "app/x/ops.py"
DOC = "docs/architecture/sample.md"
MANIFEST = {
    "old": OLD,
    "base": "base",
    "documented_in": DOC,
    "modules": [
        {
            "path": LEAF,
            "responsibility": "Cached numeric calculations",
            "defs": ["alpha", "Token"],
            "leaf": True,
        },
        {
            "path": OPS,
            "responsibility": "Serialize numeric values",
            "defs": ["beta"],
            "leaf": False,
        },
    ],
    "keep": ["keep_read", "other_read", "register", "long_helper"],
    "allow_added": [],
}
REAL_RUN = split.run


@contextlib.contextmanager
def fixture(source=None, owners=False, readonly_index=False):
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="srp-") as temp:
        os.chdir(temp)
        try:
            sources = {
                OLD: source or (FIXTURES / "sample.py").read_text(encoding="utf-8"),
                DOC: (FIXTURES / "sample.md").read_text(encoding="utf-8"),
                split.FLOOR: (FIXTURES / "floor.txt").read_text(encoding="utf-8"),
            }
            if owners:
                sources[split.OWNERS] = (
                    "from types import SimpleNamespace\n\n"
                    'FEATURE = SimpleNamespace(own_files=("app/x/old.py",))\n'
                )
            for path, text in sources.items():
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(text.encode())
            Path("pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())

            def execute(command, **kwargs):
                if command[0] == "git":
                    if command[1] == "checkout":
                        if readonly_index:
                            return subprocess.CompletedProcess(
                                command, 1, "", "index.lock: Permission denied"
                            )
                        Path(command[-1]).write_bytes(sources[command[-1]].encode())
                    elif command[1] == "show":
                        return subprocess.CompletedProcess(
                            command, 0, sources[command[-1][1:]].replace("\r\n", "\n"), ""
                        )
                    else:
                        assert command[1] in ("status", "ls-files", "diff"), command
                    return subprocess.CompletedProcess(command, 0, "", "")
                if len(command) > 1 and command[1].endswith("srp_move_check.py"):
                    output = io.StringIO()
                    with patch.object(sys, "argv", command[1:]), contextlib.redirect_stdout(output):
                        code = split.checker.main()
                    return subprocess.CompletedProcess(command, code, output.getvalue(), "")
                return REAL_RUN(command, **kwargs)

            with (
                patch.object(split, "run", side_effect=execute),
                patch.object(
                    split.checker, "show", side_effect=lambda rev, path: sources.get(path)
                ),
            ):
                yield sources
        finally:
            os.chdir(previous)


def refused(manifest, text, **options):
    options.setdefault("pull_deps", False)
    before = {p: p.read_bytes() for p in Path.cwd().rglob("*") if p.is_file()}
    try:
        split.apply(manifest, **options)
    except ValueError as exc:
        assert text in str(exc), str(exc)
    else:
        raise AssertionError(f"expected refusal: {text}")
    assert before == {p: p.read_bytes() for p in Path.cwd().rglob("*") if p.is_file()}


def check_inventory():
    with fixture():
        result = split.inventory(OLD)
        by_name = {d["name"]: d for d in result["definitions"]}
        assert len(by_name) == 7
        assert by_name["alpha"]["decorators"] == ["cache"]
        assert by_name["alpha"]["imports"] == ["cache", "sqrt"]
        assert by_name["beta"]["references"] == ["alpha"]
        assert by_name["Token"]["kind"] == "ClassDef"
        assert "Crème brûlée" in by_name["alpha"]["doc"]
        assert result["constants"][0]["readers"] == ["keep_read", "other_read", "register"]
        assert result["documented_in"] == "Documented in: " + DOC
        assert len(result["pins"]) == 2
        assert len(json.dumps(result)) < 4000
        assert by_name["long_helper"]["size"] > 200
    print("inventory_shape OK")


def check_apply_undo():
    with fixture() as sources:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert split.apply(MANIFEST, check=True) == 0, output.getvalue()
        assert "PURE MOVE OK" in output.getvalue(), output.getvalue()
        assert output.getvalue().rstrip().endswith("PASS")
        old, leaf, ops = (split.read(p) for p in (OLD, LEAF, OPS))
        assert "LIMIT = 7" in old and "LIMIT =" not in leaf + ops
        assert "# ================= Leaf calculations" in leaf
        assert "from app.x.leaf import alpha" in ops
        assert "import json" in ops
        assert (
            ast.get_docstring(ast.parse(old)).splitlines()[0]
            == (ast.get_docstring(ast.parse(sources[OLD])).splitlines()[0])
        )
        assert "Transitional compatibility shim for one release" in old
        assert f"{LEAF}::alpha" in split.read(DOC)
        assert f"{OPS}::beta" in split.read(DOC)
        assert OLD + "::alpha" not in split.read(DOC)
        assert "# sample (3)" in split.read(split.FLOOR)
        for path in (LEAF, OPS, OLD):
            assert f'"{path}"' in split.read(split.FLOOR)
        # Worktree reads equal the committed HEAD reader for the identical
        # after snapshot, without creating commits in the shared clone.
        after = {p: split.read(p) for p in (OLD, LEAF, OPS)}
        outputs = []
        for flags in ([], ["--worktree"]):
            buf = io.StringIO()
            with (
                patch.object(sys, "argv", ["checker", "base", "HEAD", OLD, LEAF, OPS, *flags]),
                patch.object(
                    split.checker,
                    "show",
                    side_effect=lambda rev, p: sources.get(p) if rev == "base" else after.get(p),
                ),
                contextlib.redirect_stdout(buf),
            ):
                assert split.checker.main() == 0
            outputs.append(buf.getvalue())
        assert outputs[0] == outputs[1]
        split.undo(MANIFEST)
        assert all(Path(p).read_bytes() == text.encode() for p, text in sources.items())
        assert not Path(LEAF).exists() and not Path(OPS).exists()
    print("apply_checker_comments_constants_cross_import_docs_floor_undo_head_parity OK")


def check_refusals():
    with fixture():
        manifest = copy.deepcopy(MANIFEST)
        manifest["keep"].remove("register")
        refused(manifest, "unassigned definitions")
        manifest = copy.deepcopy(MANIFEST)
        manifest["keep"].append("alpha")
        refused(manifest, "assigned more than once")
        manifest = copy.deepcopy(MANIFEST)
        manifest["modules"][1]["leaf"] = True
        refused(manifest, "leaf")
        manifest = copy.deepcopy(MANIFEST)
        manifest["modules"][0]["defs"].append("register")
        manifest["keep"].remove("register")
        refused(manifest, "keep register")
    source = (FIXTURES / "sample.py").read_text(encoding="utf-8")
    with fixture(source.replace("return sqrt(value)", "return beta(value)")):
        manifest = copy.deepcopy(MANIFEST)
        manifest["modules"][0]["leaf"] = False
        refused(manifest, "cycle")
    with fixture(source.replace("return sqrt(value)", "return sqrt( value )")):
        refused(MANIFEST, "ruff would change moved body")
    with fixture(source.replace("return sqrt(value)", "return sqrt(value) + LIMIT")):
        refused(MANIFEST, "leaf")
    with fixture(source.replace("return sqrt(value)", "global LIMIT\n    LIMIT = 2")):
        refused(MANIFEST, "leaf")
    with fixture(source.replace("from math import sqrt", "if True:\n    from math import sqrt")):
        refused(MANIFEST, "leaf")
    with fixture(source.replace("return sqrt(value)", "return globals()[value]")):
        refused(MANIFEST, "dynamic namespace")
    with fixture(
        source.replace(
            "return sqrt(value)", "from app.x.old import keep_read\n    return keep_read()"
        )
    ):
        refused(MANIFEST, "import cycle")
    # A local shadow must not induce a spurious cross-piece edge.
    assert "alpha" not in split.references("def f(alpha):\n    return alpha\n")
    with fixture():
        split.apply(MANIFEST)
        Path(LEAF).write_text(split.read(LEAF) + "# user's subsequent edit\n", encoding="utf-8")
        try:
            split.undo(MANIFEST)
        except ValueError as exc:
            assert "edited since apply" in str(exc)
        else:
            raise AssertionError("undo discarded a subsequent edit")
        # Remove only this fixture's external receipt.
        split.receipt_path(MANIFEST).unlink()
    print("cycle_unassigned_duplicate_leaf_register_formatter_shadow_dirty_undo_refusals OK")


def check_constants_owners_crlf():
    source = (FIXTURES / "sample.py").read_text(encoding="utf-8").replace("\n", "\r\n")
    with fixture(source, owners=True, readonly_index=True) as sources:
        manifest = copy.deepcopy(MANIFEST)
        manifest["modules"][0]["defs"].append("LIMIT")
        output = io.StringIO()
        # Git show normalizes its text output, like the real checker reader.
        with (
            contextlib.redirect_stdout(output),
            patch.object(
                split.checker,
                "show",
                side_effect=lambda rev, path: sources.get(path, "").replace("\r\n", "\n"),
            ),
        ):
            assert split.apply(manifest, check=True, summary="New concise summary.") == 0, (
                output.getvalue()
            )
        assert "_FEATURES_DIGEST recompute" in output.getvalue()
        assert LEAF in split.read(split.OWNERS) and OPS in split.read(split.OWNERS)
        assert "LIMIT = 7" in split.read(LEAF) and "LIMIT = 7" not in split.read(OLD)
        assert ast.get_docstring(ast.parse(split.read(OLD))).startswith("New concise summary.")
        assert (
            ast.get_docstring(ast.parse(split.read(OLD)))
            .split("\n\n")[1]
            .startswith("Transitional compatibility shim")
        )
        assert b"\r\n" in Path(LEAF).read_bytes()
        split.undo(manifest)
        assert all(Path(p).read_bytes() == text.encode() for p, text in sources.items())
    print("explicit_constant_move_own_files_digest_notice_crlf_summary OK")


def check_real_head_parity():
    """Compare real Git HEAD reads with disk using an already committed snapshot."""
    outputs = []
    old = "app/services/access.py"
    empty = "app/services/__init__.py"
    for flags in ([], ["--worktree"]):
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["checker", "HEAD", "HEAD", old, empty, *flags]),
            contextlib.redirect_stdout(output),
        ):
            assert split.checker.main() == 0, output.getvalue()
        outputs.append(output.getvalue())
    assert outputs[0] == outputs[1]
    print("real_committed_head_vs_worktree_reader_parity OK")


def small_manifest(source, moved=("alpha",), **options):
    return {
        "old": OLD,
        "base": "base",
        "documented_in": DOC,
        "modules": [
            {
                "path": LEAF,
                "responsibility": "Fixture calculations.",
                "defs": list(moved),
                "leaf": False,
            }
        ],
        "keep": [n for n in split.checker.defs(source) if n not in moved],
        **options,
    }


def execute_fixture(source, leaf=None):
    """Execute only synthetic fixture modules, never the application."""
    namespace = {"__name__": "fixture"}
    if leaf is None:
        exec(source, namespace)
    else:
        module = types.ModuleType("app.x.leaf")
        exec(leaf, module.__dict__)
        with patch.dict(sys.modules, {"app.x.leaf": module}):
            exec(source, namespace)
    return namespace


def help_tree(namespace):
    parser = argparse.ArgumentParser(prog="fixture")
    namespace["register"](parser.add_subparsers())

    def walk(parser):
        return (
            parser.format_help(),
            [
                (name, walk(child))
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
                for name, child in action.choices.items()
            ],
        )

    return walk(parser)


def check_v2_units():
    cases = {
        "for_tuple": 'for k, v in [("a", 2)]:\n    values = {k: v}\n',
        "with": "with open(__file__) as handle:\n    values = handle.name\n",
        "try_fallback": (
            "try:\n    from math import tau as values\nexcept ImportError:\n    values = 6\n"
        ),
        "if_binding": "if True:\n    values = 2\n",
    }
    for label, unit in cases.items():
        source = split.format_source(
            OLD, '"""Fixture."""\n' + unit + "\ndef alpha():\n    return values\n"
        )
        with fixture(source):
            first = next(iter(split.bound(ast.parse(unit).body[0])))
            manifest = small_manifest(source, ("alpha", first))
            assert split.apply(manifest, check=True) == 0
            assert ast.dump(ast.parse(unit).body[0]) in {
                ast.dump(n) for n in ast.parse(split.read(LEAF)).body
            }
            split.undo(manifest)
        print(label + " OK")
    source = (
        '"""Fixture."""\nif True:\n    values = 1\nvalues = 2\ndef alpha():\n    return values\n'
    )
    with fixture(source):
        refused(small_manifest(source), "duplicate top-level binding")
    source = split.format_source(
        OLD,
        '"""Fixture."""\nimport urllib.parse\nimport urllib.parse\nLIMIT = 1\nLIMIT = 2\n'
        "def alpha():\n    return urllib.parse.quote(str(LIMIT))\n",
    )
    # Preserve duplicate input imports (the formatter intentionally deduplicates).
    source = source.replace("import urllib.parse\n", "import urllib.parse\nimport urllib.parse\n")
    with fixture(source):
        manifest = small_manifest(source, ("alpha", "LIMIT"))
        assert split.apply(manifest, check=True) == 0
        assert split.read(LEAF).count("import urllib.parse") == 1
        assert split.read(LEAF).index("LIMIT = 1") < split.read(LEAF).index("LIMIT = 2")
        split.undo(manifest)
    print("compound_collision_identical_import_constant_reassignment OK")


def check_v2_pull_comments():
    source = split.format_source(
        OLD,
        '''"""Rules with `backticks`.
===
# A rule inside a docstring is not a comment.
"""
import logging
log = logging.getLogger(__name__)
LIMIT = 3
def helper():
    return LIMIT
# banner one

# banner two

def alpha():
    log.info("hello")
    return helper()
def beta():
    log.info("second piece")
    return 2
''',
    )
    with fixture(source):
        manifest = small_manifest(source)
        manifest["modules"].append(
            {"path": OPS, "responsibility": "Second logger reader.", "defs": ["beta"], "leaf": True}
        )
        manifest["keep"].remove("beta")
        manifest["modules"][0]["defs"].append("log")
        assert split.apply(manifest, check=True, pull_deps=True) == 0
        leaf = split.read(LEAF)
        assert "LIMIT = 3" in leaf and "def helper" in leaf
        assert "logging.getLogger(__name__)" in leaf
        assert "logging.getLogger(__name__)" in split.read(OLD)
        assert "logging.getLogger(__name__)" in split.read(OPS)
        assert "# banner one" in leaf and "# banner two" in leaf
        split.undo(manifest)
    source = (
        '"""Fixture."""\ndef helper():\n'
        + "    value = 1\n" * 40
        + "    return value\n\ndef alpha():\n    return helper()\n"
    )
    with fixture(source):
        try:
            split.apply(small_manifest(source), pull_deps=True)
        except ValueError as exc:
            assert "exceeds 40 lines" in str(exc)
        else:
            raise AssertionError("large pull accepted")
    for construct in ("globals()", "vars()", "getattr(sys.modules[__name__], 'x')"):
        source = f'"""Fixture."""\nimport sys\ndef alpha():\n    return {construct}\n'
        with fixture(source):
            refused(small_manifest(source), "at line")
    source = (
        '"""Fixture."""\ndef alpha():\n    return helper()\n'
        "def helper():\n    return beta()\ndef beta():\n    return alpha()\n"
    )
    with fixture(source):
        manifest = small_manifest(source)
        manifest["modules"].append(
            {"path": OPS, "responsibility": "Cycle reader.", "defs": ["beta"]}
        )
        manifest["keep"].remove("beta")
        refused(manifest, "cycle", pull_deps=True)
    print("pull_transitive_logger_limit_checker_banners_docstring_rules_dynamic_locations OK")


def check_v2_modes():
    source = split.format_source(
        OLD,
        '''"""Fixture."""
HELP = "one"
def register(sub):
    db = sub.add_parser("db")
    db_sub = db.add_subparsers(dest="command")
    p = db_sub.add_parser("one", help=HELP)
    p.add_argument("--value")
    p.set_defaults(func=alpha)
    p = db_sub.add_parser("two")
    p.set_defaults(func=beta)
    p = db_sub.add_parser("three")
    p.set_defaults(func=alpha)
def alpha(args):
    return 1
def beta(args):
    return 2
''',
    )
    with fixture(source):
        manifest = small_manifest(source, register_mode=True)
        assert split.apply(manifest, check=True, pull_deps=True) == 0
        assert 'HELP = "one"' in split.read(LEAF)
        old = split.read(OLD)
        assert old.index("register_leaf_db_sub(db_sub)") < old.index('"two"')
        assert old.index('"two"') < old.index("register_leaf_db_sub_2(db_sub)")
        assert help_tree(execute_fixture(source)) == help_tree(
            execute_fixture(old, split.read(LEAF))
        )
        split.undo(manifest)
    with fixture(source.replace("func=alpha", "func=lambda args: 1")):
        refused(small_manifest(source, register_mode=True), "simple name")
    changed = source.replace('p.add_argument("--value")', 'p.add_argument( "--value")')
    with fixture(changed):
        refused(
            small_manifest(changed, register_mode=True), "moved register statements", pull_deps=True
        )
    shared = source.replace(
        'db = sub.add_parser("db")', 'shared = "help"\n    db = sub.add_parser("db")'
    )
    shared = shared.replace(
        'db_sub.add_parser("one", help=HELP)', 'db_sub.add_parser("one", help=shared)'
    )
    with fixture(shared):
        refused(small_manifest(shared, register_mode=True), "share local state")
    source = split.format_source(
        OLD,
        '''"""Fixture."""
from fastapi import APIRouter, Depends
def gate():
    return True
router = APIRouter(prefix="/v1", tags=["test"], dependencies=[Depends(gate)])
@router.get("/same")
def alpha():
    return 1
@router.get("/same")
def beta():
    return 2
@router.get("/same")
def gamma():
    return 3
''',
    )
    with fixture(source):
        manifest = small_manifest(source, ("alpha", "gamma"), router_mode=True)
        assert split.apply(manifest, check=True, pull_deps=True) == 0
        old = split.read(OLD)
        assert old.index("router.include_router") < old.index("def beta")
        assert 'prefix="/v1"' in split.read(LEAF)

        def routes(namespace):
            flattened = []
            for route in namespace["router"].routes:
                if hasattr(route, "effective_route_contexts"):
                    flattened.extend(route.effective_route_contexts())
                else:
                    flattened.append(route)
            return [
                (
                    r.path,
                    sorted(r.methods),
                    r.name,
                    r.endpoint.__qualname__,
                    len(r.dependencies),
                    r.tags,
                )
                for r in flattened
            ]

        assert routes(execute_fixture(source)) == routes(execute_fixture(old, split.read(LEAF)))
        split.undo(manifest)
    collision = source + "\n_srp_router_options = 1\n"
    with fixture(collision):
        refused(
            small_manifest(collision, ("alpha", "gamma"), router_mode=True),
            "generated router binding collision",
            pull_deps=True,
        )
    print("register_nested_interleaved_simple_handler_router_options_overlap_collision OK")


def check_v2_paths():
    for old, leaf, doc in (
        ("app/x/__init__.py", LEAF, DOC),
        ("scripts/old.py", "scripts/leaf.py", None),
        ("integrations/mcp-server/old.py", "integrations/mcp-server/leaf.py", None),
    ):
        source = split.format_source(
            OLD,
            '''"""Registry with `backticks`.
===
"""
from .support import VALUE
def alpha():
    return VALUE
''',
        )
        with fixture(source) as sources:
            Path(old).parent.mkdir(parents=True, exist_ok=True)
            Path(OLD).rename(old)
            sources[old] = sources.pop(OLD)
            for path in (DOC, split.FLOOR):
                sources[path] = sources[path].replace(OLD, old)
                Path(path).write_bytes(sources[path].encode("utf-8"))
            manifest = small_manifest(source)
            manifest.update(old=old, documented_in=doc)
            manifest["modules"][0]["path"] = leaf
            Path("manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            manifest = split.load_manifest("manifest.json")
            # Hyphenated import paths cannot be rendered as Python imports.
            # The mcp-server case uses the module's actual standalone layout.
            if "mcp-server" in old:
                manifest["modules"][0]["path"] = "integrations/mcp-server/leaf.py"
            assert split.apply(manifest, check=True) == 0
            if doc is None:
                assert split.read(DOC) == sources[DOC]
                assert split.read(split.FLOOR) == sources[split.FLOOR]
            else:
                assert "from app.x.support import VALUE" in split.read(leaf)
            split.undo(manifest)
    print("package_init_relative_imports_null_docs_scripts_integrations_rules_backticks OK")


def check_v2_parity_probes():
    from srp_split.parity import snapshot

    with fixture():
        probe = Path("probe.py")
        source = (
            'import argparse\np = argparse.ArgumentParser(prog="probe")\n'
            'sub = p.add_subparsers()\nchild = sub.add_parser("child")\n'
            'child.add_argument("--flag")\np.parse_args()\n'
            'raise RuntimeError("a command handler ran")\n'
        )
        probe.write_text(source, encoding="utf-8")
        before = snapshot("helpdiff", "probe", Path.cwd(), Path.cwd() / "data")
        assert "child" in "".join(before) and "--flag" in "".join(before)
        probe.write_text(source.replace("--flag", "--changed"), encoding="utf-8")
        assert before != snapshot("helpdiff", "probe", Path.cwd(), Path.cwd() / "data")
        source = (
            "from types import SimpleNamespace as N\n"
            "def endpoint():\n    pass\n"
            'app = N(routes=[N(path="/x", methods={"GET"}, name="one", '
            "endpoint=endpoint, dependencies=[1])])\n"
        )
        probe.write_text(source, encoding="utf-8")
        before = snapshot("routediff", "probe", Path.cwd(), Path.cwd() / "data")
        probe.write_text(source.replace('name="one"', 'name="two"'), encoding="utf-8")
        assert before != snapshot("routediff", "probe", Path.cwd(), Path.cwd() / "data")
        probe.write_text('raise RuntimeError("broken probe")\n', encoding="utf-8")
        try:
            snapshot("routediff", "probe", Path.cwd(), Path.cwd() / "data")
        except ValueError as exc:
            assert "broken probe" in str(exc)
        else:
            raise AssertionError("failed parity subprocess passed")
    print("helpdiff_recursive_change_routediff_metadata_change_probe_failure OK")


def main():
    from srp_split_v3_cases import check_v3

    check_v3(sys.modules[__name__])
    check_inventory()
    check_apply_undo()
    check_refusals()
    check_constants_owners_crlf()
    check_real_head_parity()
    check_v2_units()
    check_v2_pull_comments()
    check_v2_modes()
    check_v2_paths()
    check_v2_parity_probes()
    print("SRP SPLIT OK")


if __name__ == "__main__":
    main()
