"""Batch-two regression cases, invoked only by the SRP verification runner."""

import collections
import json
import sys
from pathlib import Path
from unittest.mock import patch


def check_v3(harness):
    split = harness.split
    source = split.format_source(
        harness.OLD,
        '"""Fixture."""\nLIMIT = 1  # trailing owner\n'
        "# paragraph one\n# paragraph two\n"
        "def alpha():\n    return LIMIT\n",
    )
    with harness.fixture(source) as before:
        tree = split.ast.parse(source)
        lines = source.splitlines(keepends=True)
        assert split.span(tree.body[1], lines)[1] <= split.span(tree.body[2], lines)[0]
        manifest = harness.small_manifest(source, ("LIMIT", "alpha"))
        assert split.apply(manifest, check=True) == 0
        after = split.read(harness.LEAF)
        assert split.checker.comments(after) == split.checker.comments(source)
        assert split.checker.comments_by_def(after) == split.checker.comments_by_def(source)
        split.undo(manifest)
        assert all(Path(p).read_bytes() == s.encode() for p, s in before.items())
    # Different owners expose duplicate tokens hidden by a global subset check.
    with harness.fixture(source):
        manifest = harness.small_manifest(source)
        manifest["modules"].append(
            {"path": harness.OPS, "defs": ["LIMIT"], "responsibility": "Constants."}
        )
        assert split.apply(manifest, check=True) == 0
        counts = collections.Counter()
        for path in (harness.OLD, harness.LEAF, harness.OPS):
            counts += split.checker.comments(split.read(path))
        assert counts == split.checker.comments(source)
        split.undo(manifest)
        manifest["modules"][0]["leaf"] = True
        harness.refused(
            manifest,
            "LIMIT from app.x.ops: already assigned to another piece",
            pull_deps=True,
        )

    source = split.format_source(
        harness.OLD,
        '"""Fixture."""\ndef alpha():\n    return 1\n'
        "def retained():\n    from app.x.old import alpha\n\n    return alpha()\n",
    )
    with harness.fixture(source):
        manifest = harness.small_manifest(source)
        assert split.apply(manifest, check=True) == 0
        split.undo(manifest)
    assert harness.OLD.removesuffix(".py").replace("/", ".") in split.checker.imports_of(
        "from app.x.old import alpha", "app.x.old"
    )

    source = split.format_source(
        harness.OLD,
        '"""Fixture."""\nimport urllib\nimport urllib.error\nimport urllib.request\n'
        "def alpha():\n    return urllib.request, urllib.error\n",
    ).replace("import urllib\n", "import urllib\nimport urllib\n")
    with harness.fixture(source):
        manifest = harness.small_manifest(source)
        assert split.apply(manifest, check=True) == 0
        leaf = split.read(harness.LEAF)
        assert leaf.count("import urllib\n") == 1
        assert "import urllib.error" in leaf and "import urllib.request" in leaf
        split.undo(manifest)
    with harness.fixture('"""Fixture."""\nimport math as urllib\nimport urllib\n'):
        harness.refused(harness.small_manifest("", ("urllib",)), "duplicate top-level binding")

    source = split.format_source(
        harness.OLD,
        '"""Fixture."""\nfrom enum import Enum as RunStatus\n'
        "def alpha():\n    return vars(RunStatus)\n",
    )
    with harness.fixture(source):
        manifest = harness.small_manifest(source)
        assert split.apply(manifest, check=True) == 0
        split.undo(manifest)
    split.units.dynamic("import enum\nvars(enum.Enum)\n", harness.OLD, split.require)
    for expression in (
        "globals()",
        "vars()",
        "vars(sys)",
        "getattr(sys, 'path')",
        "sys.modules['x']",
    ):
        source = f'"""Fixture."""\nimport sys\ndef alpha():\n    return {expression}\n'
        with harness.fixture(source):
            harness.refused(harness.small_manifest(source), "dynamic namespace access")

    source = split.format_source(
        harness.OLD, '"""Fixture."""\nLIMIT = 2\ndef alpha():\n    return LIMIT\n'
    )
    for flags in ([], ["--pull-deps"], ["--no-pull-deps"]):
        with harness.fixture(source):
            manifest = harness.small_manifest(source)
            manifest["modules"][0]["leaf"] = True
            Path("manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(sys, "argv", ["split", "apply", "manifest.json", "--check", *flags]):
                assert split.main() == (1 if "--no-pull-deps" in flags else 0)
            if "--no-pull-deps" not in flags:
                assert "LIMIT = 2" in split.read(harness.LEAF)
                split.undo(manifest)
    with harness.fixture(source):
        manifest = harness.small_manifest(source)
        manifest["modules"][0]["path"] = "app/x/" + "x" * 41 + ".py"
        harness.refused(manifest, "stem exceeds 40")
        Path("manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        try:
            split.load_manifest("manifest.json")
        except ValueError as exc:
            assert "shorten the generator slug" in str(exc)
        else:
            raise AssertionError("oversized slug accepted")
        manifest["modules"][0]["path"] = "app/x/" + "x" * 40 + ".py"
        Path("manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert split.load_manifest("manifest.json") == manifest
    print("v3_comment_ownership_self_import_urllib_class_vars_default_pull_slug_guard OK")
