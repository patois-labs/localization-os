"""Find tests coupled to classes, ids, and literal template/CSS strings.

Documented in: DESIGN.md. Reports candidates, not proof of assertion coverage:
constants used by helper assertions are included and labelled as references.
"""

import argparse
import ast
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TemplateStrings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.strings = set()
        self.hidden = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden = True
        for name, value in attrs:
            if value and name in ("class", "id"):
                self.strings.update(v for v in value.split() if re.fullmatch(r"[\w-]+", v))
            elif value and name in ("aria-label", "title", "placeholder") and "{" not in value:
                self.strings.add(value)

    def handle_data(self, data):
        if not self.hidden and "{" not in data and "}" not in data:
            value = " ".join(data.split())
            if len(value) >= 3:
                self.strings.add(value)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = False


def emitted_strings(path):
    source = path.read_text(encoding="utf-8")
    if path.suffix == ".css":
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        selectors = " ".join(re.findall(r"([^{}]+)\{", source))
        return set(re.findall(r"[.#]([A-Za-z_][\w-]*)", selectors)) | set(
            re.findall(r'content:\s*[\'"]([^\'"]+)[\'"]', source)
        )
    parser = TemplateStrings()
    source = re.sub(r"{#.*?#}", "", source, flags=re.S)
    parser.feed(re.sub(r"{{.*?}}|{%.*?%}", "", source, flags=re.S))
    # Translation keys locate localized text assertions without treating Jinja
    # syntax (if/and/or) or script storage values as emitted page copy.
    parser.strings.update(re.findall(r"\bt\(request,\s*['\"]([^'\"]+)['\"]", source))
    return parser.strings


def inventory(paths, tests_dir):
    sources = {}
    for test in sorted(tests_dir.rglob("*.py")):
        try:
            tree = ast.parse(test.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        assertions = {
            id(n) for a in ast.walk(tree) if isinstance(a, ast.Assert) for n in ast.walk(a)
        }
        docstrings = {
            id(n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        }
        sources[test] = [
            (n.value, n.lineno, "assert" if id(n) in assertions else "reference")
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
        ]
    lines = []
    for path in paths:
        needles = emitted_strings(path)
        matcher = (
            re.compile(
                r"(?<![\w-])(?:"
                + "|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True) if n)
                + r")(?![\w-])"
            )
            if needles
            else None
        )
        lines.append(f"{path.as_posix()}: {len(needles)} emitted strings/selectors")
        count = 0
        for test, constants in sources.items():
            matches = {}
            for value, line, kind in constants:
                hits = sorted({m[0] for m in matcher.finditer(value)}) if matcher else []
                if hits:
                    matches[(line, kind)] = hits
            if matches:
                count += 1
                lines.append(f"  {test.as_posix()}")
                for (line, kind), hits in sorted(matches.items()):
                    lines.append(f"    {line} {kind}: {', '.join(hits)}")
        lines.append(
            f"  Matched {count} test files. Review references for helper/data-driven assertions."
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--tests", type=Path, default=Path("tests"))
    args = parser.parse_args()
    print(inventory(args.files, args.tests).encode("ascii", "backslashreplace").decode("ascii"))


if __name__ == "__main__":
    main()
