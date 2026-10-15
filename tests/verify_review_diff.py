"""Offline review comparison fixtures, executed by test_verify_suites."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.review_diff import compare, matches


def finding(identifier, file, line, scenario="", observed=""):
    return {
        "id": identifier,
        "severity": "LOW",
        "confidence": "CONFIRMED",
        "file": file,
        "line": line,
        "scenario": scenario,
        "observed": observed,
        "expected": "fixture",
        "evidence": "fixture",
        "minimal_fix": "fixture",
    }


def main():
    fixtures = [
        (finding("A1", "same.py", 10), finding("B1", "same.py", 10), True),
        (finding("A2", "near.py", 10), finding("B2", "near.py", 13), True),
        (
            finding("A3", "left.py", 1, "ONE, two; THREE", "four five six seven"),
            finding("B3", "right.py", 100, "one two three", "four five other words"),
            True,
        ),
        (
            finding("A4", "far.py", 10, "unrelated phrase here"),
            finding("B4", "far.py", 14, "different words entirely"),
            False,
        ),
    ]
    for a, b, expected in fixtures:
        assert matches(a, b) is expected
        result = compare([a], [b])
        assert len(result["shared"]) == int(expected)
        assert len(result["a_only"]) == len(result["b_only"]) == int(not expected)
        assert result["severity_counts"]["a"] == {"LOW": 1}
    assert not matches(finding("x", "x", 1, "one two"), finding("y", "y", 1, "one two"))
    assert not matches(
        finding("x", "x", 1, "one two three four five six seven"),
        finding("y", "y", 1, "one two three four other words here"),
    )
    a, b, _ = fixtures[0]
    assert len(compare([a], [b, {**b, "id": "B5"}])["shared"]) == 2
    assert compare([], [b])["b_only"] == [b]
    with tempfile.TemporaryDirectory() as tmp:
        paths = [Path(tmp) / name for name in ("a.json", "b.json")]
        for path, data in zip(paths, ([a], [b]), strict=True):
            path.write_text(json.dumps({"schema_version": 1, "findings": data}), encoding="utf-8")
        command = [sys.executable, str(ROOT / "scripts/review_diff.py"), *map(str, paths)]
        run = subprocess.run(command, capture_output=True, text=True, check=True)
        assert json.loads(run.stdout)["shared"][0]["b_id"] == "B1"
        run = subprocess.run([*command, "--markdown"], capture_output=True, text=True, check=True)
        assert "| shared | A1 | B1 |" in run.stdout
        assert "| shared_a | LOW | 1 |" in run.stdout
        paths[0].write_text('{"schema_version": 2, "findings": []}', encoding="utf-8")
        run = subprocess.run(command, capture_output=True, text=True)
        assert run.returncode == 2 and "schema_version 1" in run.stderr
    print("REVIEW DIFF OK")


if __name__ == "__main__":
    main()
