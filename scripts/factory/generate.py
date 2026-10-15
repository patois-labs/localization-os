"""Connector factory CLI — scaffold or verify a connector's parity touchpoints.

    python scripts/factory/generate.py new   <spec.yaml> [--root .] [--dry-run]
    python scripts/factory/generate.py check <spec.yaml> [--root .]

`new`   generates the per-connector FILES (module skeleton, verify test skeleton, runbook,
        BUILD-TODO, THIRD_PARTY_NOTICES entry) and inserts each shared-file entry at its
        `# factory:anchor <slot>` marker. Idempotent (skips entries/files already present),
        REFUSES to overwrite an existing connector's differing files, and never proceeds on
        a kind that collides with an existing connector (active or archived).
`check` verifies every shared-file touchpoint entry exists (used by tests + reviewers).

Runs standalone (no app import); the app resolves from --root only for reading files. ASCII
output only (Windows consoles).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow both `python scripts/factory/generate.py` and `python -m scripts.factory.generate`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.factory import spec as spec_mod
    from scripts.factory import templates as tpl
    from scripts.factory.touchpoints import TOUCHPOINTS
else:
    from . import spec as spec_mod
    from . import templates as tpl
    from .touchpoints import TOUCHPOINTS


ANCHOR = "factory:anchor"


class GenError(Exception):
    pass


# --- newline-verbatim I/O (MEDIUM-1 / MEDIUM-A) ------------------------------
# The factory must insert its one entry and leave EVERY OTHER BYTE untouched, including a
# file's existing line endings. The shared targets are CRLF in the working tree fly deploys
# (docs/CLI.md, help.html, connection.html are committed CRLF; .py files are CRLF-smudged by
# autocrlf), so we must NOT normalize. newline="" on BOTH read and write disables translation
# in both directions: read keeps \r\n as-is; write emits the string verbatim. (A previous fix
# used read_text + newline="\n", which normalized CRLF -> LF on read and then wrote LF-only,
# flattening every line of those three files on every run — the exact outage class. Fixed.)
# The one inserted entry carries \n line endings; existing CRLF lines are preserved unchanged.
def _read(p: Path) -> str:
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# --- new-file artifacts ------------------------------------------------------
def _new_files(sp) -> list[tuple[str, str]]:
    """(relative_path, content) for each generated per-connector FILE."""
    return [
        (f"app/services/connectors/{sp.module_name}.py", tpl.render_module(sp)),
        (f"tests/verify_{sp.kind}.py", tpl.render_test(sp)),
        (f"docs/connectors/RUNBOOK-{sp.kind}-live-qa.md", tpl.render_runbook(sp)),
        (f"docs/connectors/BUILD-TODO-{sp.kind}.md", tpl.render_build_todo(sp)),
    ]


def _compile_check(rel: str, content: str) -> None:
    """Belt-and-suspenders: a generated .py MUST parse (catches any templating/escaping bug
    before it lands on disk)."""
    if rel.endswith(".py"):
        try:
            compile(content, rel, "exec")
        except SyntaxError as e:
            raise GenError(f"generated {rel} does not parse: {e}") from e


def _anchor_line_index(lines: list[str], slot: str) -> int:
    needle = f"{ANCHOR} {slot}"
    for i, ln in enumerate(lines):
        if needle in ln:
            return i
    return -1


def _insert_before_anchor(text: str, slot: str, block: str) -> str:
    lines = text.splitlines(keepends=True)
    idx = _anchor_line_index(lines, slot)
    if idx < 0:
        raise GenError(f"anchor '{slot}' not found")
    chunk = block if block.endswith("\n") else block + "\n"
    lines.insert(idx, chunk)
    return "".join(lines)


def _insert_jinja_dict(text: str, slot: str, entry: str) -> str:
    """Insert a dict entry into a Jinja `{% set NAME = { ... } %}` whose comment anchor sits
    on the line BEFORE the `{% set %}` (Jinja forbids comments inside the statement). Finds
    the statement's closing `} %}` after the anchor and inserts the entry just above it."""
    lines = text.splitlines(keepends=True)
    idx = _anchor_line_index(lines, slot)
    if idx < 0:
        raise GenError(f"anchor '{slot}' not found")
    for j in range(idx + 1, len(lines)):
        if "} %}" in lines[j]:
            chunk = entry if entry.endswith("\n") else entry + "\n"
            lines.insert(j, chunk)
            return "".join(lines)
    raise GenError(f"no closing '}} %}}' found after anchor '{slot}'")


def _insert_tuple_line(text: str, slot: str, element: str) -> str:
    """Insert a bare element into the single-line tuple that follows the anchor (e.g. _KINDS
    = (...)). Adds `, <element>` before the tuple's closing paren."""
    lines = text.splitlines(keepends=True)
    idx = _anchor_line_index(lines, slot)
    if idx < 0:
        raise GenError(f"anchor '{slot}' not found")
    for j in range(idx + 1, min(idx + 4, len(lines))):
        if "= (" in lines[j] and ")" in lines[j]:
            close = lines[j].rfind(")")
            lines[j] = lines[j][:close] + f", {element}" + lines[j][close:]
            return "".join(lines)
    raise GenError(f"no single-line tuple found after anchor '{slot}'")


# --- planning ----------------------------------------------------------------
def _plan_new(sp, root: Path) -> list[dict]:
    plan: list[dict] = []

    # per-connector files
    for rel, content in _new_files(sp):
        _compile_check(rel, content)
        p = root / rel
        if p.exists():
            existing = _read(p)
            if existing == content:
                plan.append({"kind": "file", "rel": rel, "action": "skip (identical)"})
            else:
                raise GenError(
                    f"refusing to overwrite existing file {rel} (differs from the generated "
                    f"skeleton - an already-built connector, or a hand-edited one)."
                )
        else:
            plan.append({"kind": "file", "rel": rel, "action": "create", "content": content})

    # THIRD_PARTY_NOTICES append (only when a bb-io reference was studied)
    notices = tpl.render_notices(sp)
    if notices is not None:
        rel = "THIRD_PARTY_NOTICES.md"
        cur = _read(root / rel) if (root / rel).exists() else ""
        if f"### {sp.name} connector" in cur:
            plan.append({"kind": "notices", "rel": rel, "action": "skip (present)"})
        else:
            plan.append({"kind": "notices", "rel": rel, "action": "append", "content": notices})

    # shared-file touchpoint entries
    for tp in TOUCHPOINTS:
        rel = tp.rel_path
        p = root / rel
        if not p.exists():
            raise GenError(f"target file missing for touchpoint '{tp.slot}': {rel}")
        text = _read(p)
        if tp.present(sp, text):
            plan.append({"kind": "entry", "slot": tp.slot, "rel": rel, "action": "skip (present)"})
        elif f"{ANCHOR} {tp.slot}" not in text:
            raise GenError(
                f"anchor '{tp.slot}' not found in {rel} (run the anchor migration first)."
            )
        else:
            plan.append(
                {
                    "kind": "entry",
                    "slot": tp.slot,
                    "rel": rel,
                    "action": "insert",
                    "render": tp.render(sp),
                    "insert_mode": tp.insert_mode,
                }
            )
    return plan


def _materialize(plan: list[dict], root: Path) -> dict[str, str]:
    """Compute the FINAL text of every file this plan writes, IN MEMORY, without touching
    disk. Multiple entry-inserts to one shared file are composed in order. Returns
    {rel_path: final_text}. Nothing is written here — that is _apply's job, and only after
    every mutated .py has been compile-checked (atomic: no partial write, LOW-4)."""
    files: dict[str, str] = {}
    for step in plan:
        if step["kind"] == "file" and step["action"] == "create":
            files[step["rel"]] = step["content"]
    for step in plan:
        if step["kind"] == "notices" and step["action"] == "append":
            rel = step["rel"]
            cur = _read(root / rel) if (root / rel).exists() else ""
            sep = (
                "" if cur.endswith("\n\n") or not cur else ("\n" if cur.endswith("\n") else "\n\n")
            )
            files[rel] = cur + sep + step["content"]
    for step in plan:
        if step["kind"] == "entry" and step["action"] == "insert":
            rel = step["rel"]
            text = files.get(rel)
            if text is None:
                text = _read(root / rel)
            mode = step["insert_mode"]
            if mode == "tuple_line":
                text = _insert_tuple_line(text, step["slot"], step["render"])
            elif mode == "jinja_dict":
                text = _insert_jinja_dict(text, step["slot"], step["render"])
            else:
                text = _insert_before_anchor(text, step["slot"], step["render"])
            files[rel] = text
    return files


def _compile_all(files: dict[str, str]) -> None:
    """compile()-check EVERY mutated .py (not just the freshly generated ones) — a spec
    string injected into a shared file that broke its syntax fails the WHOLE op here,
    before any write (HIGH-1 / MEDIUM-4)."""
    for rel, text in files.items():
        if rel.endswith(".py"):
            try:
                compile(text, rel, "exec")
            except SyntaxError as e:
                raise GenError(
                    f"generated/edited {rel} does not parse after insertion "
                    f"(refusing to write anything): {e}"
                ) from e


def _apply(plan: list[dict], root: Path) -> None:
    files = _materialize(plan, root)
    _compile_all(files)  # validate ALL before writing ANY (atomic)
    for rel, text in files.items():
        _write(root / rel, text)


# --- commands ----------------------------------------------------------------
def cmd_new(sp, root: Path, dry_run: bool) -> int:
    spec_mod.validate_collision(sp, root)
    plan = _plan_new(sp, root)
    # Materialize + compile-check the full set NOW so a dry-run also proves the result parses
    # (and a real run never writes a half-broken tree). Cheap; no disk writes.
    _compile_all(_materialize(plan, root))
    verb = "PLAN (dry-run)" if dry_run else "GENERATE"
    print(f"[factory] {verb} connector '{sp.kind}' ({sp.class_name}, {sp.topology}) into {root}")
    for step in plan:
        label = step.get("slot") or step["rel"]
        print(f"  - {step['action']:<18} {step['kind']:<8} {label}")
    if dry_run:
        print("[factory] dry-run: nothing written.")
        return 0
    _apply(plan, root)
    print(
        f"[factory] done. Next: fill the SEMANTIC work in "
        f"app/services/connectors/{sp.module_name}.py and tests/verify_{sp.kind}.py "
        f"(see docs/connectors/BUILD-TODO-{sp.kind}.md)."
    )
    return 0


def cmd_check(sp, root: Path) -> int:
    missing: list[str] = []
    for tp in TOUCHPOINTS:
        p = root / tp.rel_path
        if not p.exists():
            missing.append(f"{tp.slot} (file missing: {tp.rel_path})")
            continue
        if not tp.present(sp, _read(p)):
            missing.append(f"{tp.slot} ({tp.rel_path})")
    total = len(TOUCHPOINTS)
    if missing:
        print(
            f"[factory] check '{sp.kind}': {total - len(missing)}/{total} touchpoints present. MISSING:"  # noqa: E501
        )
        for m in missing:
            print(f"  - {m}")
        return 1
    print(f"[factory] check '{sp.kind}': all {total}/{total} shared-file touchpoints present.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory", description="Connector factory scaffolder.")
    ap.add_argument("command", choices=("new", "check"))
    ap.add_argument("spec", help="path to a connector spec YAML")
    ap.add_argument("--root", default=".", help="target repo root (default: .)")
    ap.add_argument(
        "--dry-run", action="store_true", help="print the plan; write nothing (new only)"
    )
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        sp = spec_mod.load_spec(args.spec)
        if args.command == "new":
            return cmd_new(sp, root, args.dry_run)
        return cmd_check(sp, root)
    except (spec_mod.SpecError, GenError) as e:
        print(f"[factory] ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
