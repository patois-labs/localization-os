"""Compare house-schema reviews offline, preserving every matching pair.

Text similarity is the overlap coefficient of distinct word trigrams:
intersection / smaller set size. Empty trigram sets never match by text.
Matching is many-to-many; an unmatched finding has no matching counterpart.
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def read_review(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != 1 or not isinstance(data.get("findings"), list):
        raise ValueError(f"{path}: expected schema_version 1 and findings list")
    required = {
        "id",
        "severity",
        "confidence",
        "file",
        "line",
        "scenario",
        "expected",
        "observed",
        "evidence",
        "minimal_fix",
    }
    for finding in data["findings"]:
        if not isinstance(finding, dict) or not required <= finding.keys():
            raise ValueError(f"{path}: finding missing required fields")
        if type(finding["line"]) is not int or finding["line"] < 1:
            raise ValueError(f"{path}: finding line must be a positive integer")
        if any(not isinstance(finding[key], str) for key in required - {"line"}):
            raise ValueError(f"{path}: finding fields other than line must be strings")
    return data["findings"]


def trigrams(finding: dict) -> set[tuple[str, ...]]:
    words = re.findall(r"[^\W_]+", (finding["scenario"] + " " + finding["observed"]).lower())
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def matches(a: dict, b: dict) -> bool:
    if a["file"] == b["file"] and abs(a["line"] - b["line"]) <= 3:
        return True
    left, right = trigrams(a), trigrams(b)
    return bool(left and right and len(left & right) * 5 >= min(len(left), len(right)) * 3)


def compare(a: list[dict], b: list[dict]) -> dict:
    shared, matched_a, matched_b = [], set(), set()
    for i, left in enumerate(a):
        for j, right in enumerate(b):
            if matches(left, right):
                shared.append({"a_id": left["id"], "b_id": right["id"], "a": left, "b": right})
                matched_a.add(i)
                matched_b.add(j)
    a_only = [finding for i, finding in enumerate(a) if i not in matched_a]
    b_only = [finding for j, finding in enumerate(b) if j not in matched_b]
    groups = {
        "a": a,
        "b": b,
        "shared_a": [finding for i, finding in enumerate(a) if i in matched_a],
        "shared_b": [finding for j, finding in enumerate(b) if j in matched_b],
        "a_only": a_only,
        "b_only": b_only,
    }
    counts = {
        name: dict(sorted(Counter(f["severity"] for f in rows).items()))
        for name, rows in groups.items()
    }
    return {"shared": shared, "a_only": a_only, "b_only": b_only, "severity_counts": counts}


def markdown(result: dict) -> str:
    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = [
        "| Group | A id | B id | Severity (A / B) | Location (A / B) |",
        "|---|---|---|---|---|",
    ]
    rows = [("shared", pair["a"], pair["b"]) for pair in result["shared"]]
    rows += [("a_only", finding, None) for finding in result["a_only"]]
    rows += [("b_only", None, finding) for finding in result["b_only"]]
    for group, a, b in rows:
        severity = " / ".join(f["severity"] if f else "" for f in (a, b))
        location = " / ".join(f"{f['file']}:{f['line']}" if f else "" for f in (a, b))
        values = [group, a["id"] if a else "", b["id"] if b else "", severity, location]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines += ["", "| Group | Severity | Count |", "|---|---|---|"]
    for group, counts in result["severity_counts"].items():
        for severity, count in counts.items():
            lines.append(f"| {group} | {cell(severity)} | {count} |")
        if not counts:
            lines.append(f"| {group} | (none) | 0 |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("a", type=Path, help="Reviewer A JSON")
    parser.add_argument("b", type=Path, help="Reviewer B JSON")
    parser.add_argument("--markdown", action="store_true", help="Print comparison tables")
    args = parser.parse_args()
    try:
        result = compare(read_review(args.a), read_review(args.b))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(markdown(result) if args.markdown else json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
