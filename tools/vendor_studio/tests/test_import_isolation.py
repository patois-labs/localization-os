"""WIRE-ONLY ISOLATION GUARD (both directions), enforced by scanning imports:

  * nothing under tools/vendor_studio/ may import from `app` or `integrations`;
  * nothing OUTSIDE tools/vendor_studio/ may import `vstudio`.

If either fails, the contract is broken — the whole point of the exercise is that the two
sides speak only over the HTTP wire.

The scan covers TRACKED files only (`git ls-files`): the main checkout carries dozens of
full repo clones under .claude/worktrees/, and a plain directory walk into them made the
reverse guard take 10+ minutes while flagging phantom "outside" vstudio imports inside
the nested checkouts (issue #338). Without a usable git (e.g. a source archive) it
falls back to a directory walk that prunes the same local-only trees.
"""

from __future__ import annotations

import ast
import os
import subprocess

_TESTS = os.path.dirname(os.path.abspath(__file__))
_TOOL_ROOT = os.path.dirname(_TESTS)  # tools/vendor_studio
_REPO = os.path.dirname(os.path.dirname(_TOOL_ROOT))  # repo root

# Local-only / git-ignored trees a plain directory walk must never enter. .claude
# holds Claude Code scratch + git worktrees (each a full checkout); VIDEO_DEMOS is
# untracked on purpose (CLAUDE.md).
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "data",
    "data_qa",
    ".claude",
    ".qwen",
    "VIDEO_DEMOS",
    "sample-docs",
    "scratchpad",
    "scratchpad_review",
    ".test-instances",
    "scan-reports",
    "_audit-results",
    "dist",
    "build",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
}


def _py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _tracked_py_files(root: str) -> list:
    """The .py files the guard scans under `root`: tracked plus untracked-not-ignored
    files when git is usable (fast, and structurally skips every ignored local-only
    tree such as .claude/worktrees), else a pruned walk. Files deleted from the
    working tree but still in the index are dropped."""
    root_abs = os.path.abspath(root)
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed git argv, no shell
            [  # noqa: S607 -- git from PATH by design
                "git",
                "-C",
                _REPO,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "*.py",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return list(_py_files(root_abs))
    if proc.returncode == 0:
        prefix = root_abs + os.sep
        files = [
            os.path.join(_REPO, name.decode("utf-8", "replace"))
            for name in proc.stdout.split(b"\0")
            if name
        ]
        return [f for f in files if os.path.abspath(f).startswith(prefix) and os.path.isfile(f)]
    return list(_py_files(root_abs))


def _imported_roots(path: str) -> set:
    """The set of top-level module roots imported by a file (e.g. 'app', 'vstudio')."""
    with open(path, encoding="utf-8") as fh:
        try:
            tree = ast.parse(fh.read(), filename=path)
        except SyntaxError:
            return set()
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _forward_offenders(files: list) -> list:
    """vendor_studio files that import Localization OS code (must stay empty)."""
    offenders = []
    for path in files:
        roots = _imported_roots(path)
        for forbidden in ("app", "integrations"):
            if forbidden in roots:
                offenders.append((os.path.relpath(path, _REPO), forbidden))
    return offenders


def _reverse_offenders(files: list) -> list:
    """Files outside vendor_studio that import `vstudio` (must stay empty)."""
    prefix = os.path.abspath(_TOOL_ROOT) + os.sep
    offenders = []
    for path in files:
        if os.path.abspath(path).startswith(prefix):
            continue  # vendor_studio's own imports of `vstudio` are fine
        if "vstudio" in _imported_roots(path):
            offenders.append(os.path.relpath(path, _REPO))
    return offenders


def test_vendor_studio_never_imports_app_or_integrations():
    offenders = _forward_offenders(_tracked_py_files(_TOOL_ROOT))
    assert not offenders, (
        f"Vendor Studio must not import Localization OS code (wire-only isolation): {offenders}"
    )


def test_nothing_outside_vendor_studio_imports_vstudio():
    offenders = _reverse_offenders(_tracked_py_files(_REPO))
    assert not offenders, (
        f"No code outside tools/vendor_studio may import vstudio (wire-only isolation): {offenders}"
    )


def test_scan_is_not_vacuous():
    # A git failure or an over-eager prune would leave the guards above with an empty
    # file list and a green result for the wrong reason.
    tool_files = _tracked_py_files(_TOOL_ROOT)
    assert tool_files, "vendor_studio scan came up empty: the guards would pass vacuously"
    assert any(os.path.basename(p) == "test_import_isolation.py" for p in tool_files)
    assert len(_tracked_py_files(_REPO)) > len(tool_files), (
        "repo scan must cover more files than the vendor tree"
    )


def test_reverse_guard_flags_a_real_outside_import(tmp_path):
    bad = tmp_path / "outside_vstudio_import.py"
    bad.write_text("import vstudio\n", encoding="utf-8")
    offenders = _reverse_offenders([str(bad)])
    assert offenders, "a real `import vstudio` outside vendor_studio must be flagged"


def test_forward_guard_flags_a_real_inside_import(tmp_path):
    bad = tmp_path / "inside_imports_app.py"
    bad.write_text("from app.services import something\n", encoding="utf-8")
    offenders = _forward_offenders([str(bad)])
    assert offenders, "an `app` import inside vendor_studio must be flagged"


def test_fallback_walk_skips_local_only_trees(tmp_path):
    clone = tmp_path / ".claude" / "worktrees" / "a-clone"
    clone.mkdir(parents=True)
    (clone / "decoy.py").write_text("import vstudio\n", encoding="utf-8")
    demos = tmp_path / "VIDEO_DEMOS"  # untracked on purpose (CLAUDE.md)
    demos.mkdir()
    (demos / "clip.py").write_text("import vstudio\n", encoding="utf-8")
    (tmp_path / "real.py").write_text("import os\n", encoding="utf-8")
    found = {os.path.basename(p) for p in _py_files(str(tmp_path))}
    assert found == {"real.py"}, f".claude/worktrees + VIDEO_DEMOS must be pruned, got {found}"
