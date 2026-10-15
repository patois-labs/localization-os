"""Offline base/worktree parity probes in short, disposable temp checkouts."""

import difflib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROBE = r"""
import argparse, importlib, json, os, runpy, sys
sys.path.insert(0, os.getcwd())
kind, command = sys.argv[1:]
if kind == "helpdiff":
    def dump(parser, path=()):
        result = [(list(path), parser.format_help())]
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    result.extend(dump(child, (*path, name)))
        return result
    def capture(parser, *args, **kwargs):
        print("SRP_PARITY=" + json.dumps(dump(parser), sort_keys=True))
        raise SystemExit(0)
    argparse.ArgumentParser.parse_args = capture
    sys.argv = [command]
    runpy.run_module(command, run_name="__main__")
else:
    app = importlib.import_module(command).app
    rows = []
    def flattened(routes):
        for route in routes:
            if hasattr(route, "effective_route_contexts"):
                yield from route.effective_route_contexts()
            else:
                yield route
    for route in flattened(app.routes):
        endpoint = getattr(route, "endpoint", None)
        dependencies = getattr(getattr(route, "dependant", None), "dependencies",
                               getattr(route, "dependencies", []))
        rows.append((route.path, sorted(getattr(route, "methods", None) or []),
                     route.name, getattr(endpoint, "__qualname__", None),
                     len(dependencies or [])))
    print("SRP_PARITY=" + json.dumps(rows, sort_keys=True))
"""


def snapshot(kind, command, cwd, temp):
    env = os.environ.copy()
    env.update(
        dict.fromkeys(
            (
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
                "OPENAI_API_KEY",
                "LOCAL_BASE_URL",
                "DATABASE_URL",
            ),
            "",
        )
    )
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        LOC_TMS_DATA_DIR=str(temp / "data"),
        LOC_TMS_MOCK_FALLBACK="1",
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", PROBE, kind, command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise ValueError(f"{kind} probe failed in {cwd}: {result.stdout}\n{result.stderr}")
    payload = next(
        (
            s.removeprefix("SRP_PARITY=")
            for s in result.stdout.splitlines()
            if s.startswith("SRP_PARITY=")
        ),
        None,
    )
    if payload is None:
        raise ValueError("command did not build an argparse parser / route table")
    return json.dumps(json.loads(payload), indent=2, ensure_ascii=False).splitlines(keepends=True)


def compare(kind, command, base="HEAD"):
    command = command.removeprefix("python -m ").removeprefix("-m ")
    with tempfile.TemporaryDirectory(prefix="sd-") as directory:
        temp = Path(directory)
        archive = subprocess.run(["git", "archive", "--format=zip", base], capture_output=True)
        if archive.returncode:
            raise ValueError(archive.stderr.decode("utf-8", errors="replace"))
        checkout = temp / "b"
        checkout.mkdir()
        with zipfile.ZipFile(io.BytesIO(archive.stdout)) as bundle:
            for name in bundle.namelist():
                if not (checkout / name).resolve().is_relative_to(checkout.resolve()):
                    raise ValueError("unsafe archive path")
            bundle.extractall(checkout)
        before = snapshot(kind, command, checkout, temp / "base")
        after = snapshot(kind, command, Path.cwd(), temp / "head")
        diff = "".join(difflib.unified_diff(before, after, fromfile=base, tofile="WORKTREE"))
        print(diff or f"{kind} PASS: base/worktree snapshots identical", end="" if diff else "\n")
        return int(bool(diff))
