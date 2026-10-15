"""Offline pre-gate: pregate.py <clone name or path> <base ref> [brief path].

Clone names resolve under this repository's .claude/worktrees directory.
All subprocess output stays private except safe summaries. The one networked step is
the dependency audit (pip-audit); unreachable advisories block the gate.

"Suite owners" (after "Brief suites", issue #182) derives the verify suites that own the
changed files (scripts/suite_owners.py: a suite names the module, uses a changed symbol, or
drives a changed route or CLI verb) and FAILS when one was not collected by the brief's -k
or by a ratchet the pre-gate runs anyway. It fails on absence only and never adds a suite
to the run; the record lists each missing owner, its reason, and a -k line to paste. For a
false positive, put a line in the brief:

    owners-waive: verify_x reason text

The reason is required (a waiver without one FAILS), and every waiver is printed in the
record.

The brief is optional: it is a Markdown file whose first `-k "..."` line after any
heading selects the suites to run (see docs/ and CONTRIBUTING for the shape). Without a
brief the pre-gate runs a small default selection. Records are written to a gitignored
`.pregate/` folder at the clone root. Every command runs with the interpreter that runs this
script, so run it from the virtualenv you use for the project.
"""

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PYTHON = sys.executable
RECORD_DIR = ".pregate"
DEFAULT_SELECTION = "verify_ruff or verify_import_contracts or verify_branding"
RATCHETS = "verify_mcp_parity or verify_modules or verify_features or verify_badinput_folded"
BOUNDARY_RATCHETS = (
    "verify_branding or verify_import_contracts or verify_kernel_facade "
    "or verify_connector_config_writers"
)
SECRET = re.compile(
    "|".join(
        [
            r"lsu_[A-Za-z0-9_-]{8,}",
            "sk-" + "ant-",
            r"sk-[A-Za-z0-9]{20,}",
            "AI" + r"za[A-Za-z0-9_-]{35,}",
            r"AKIA[0-9A-Z]{16}",
            "-----" + "BEGIN .*PRIVATE KEY",
        ]
    )
)
ASSIGNMENT = re.compile(
    r"""(?ix)["']?\b(?P<key>\w*(?:api_?key|secret|token|password|passphrase)[\w-]*)\b["']?"""
    r"""\s*\]?\s*(?::\s*(?:str|Optional\[str\])\s*)?(?:=(?!=)|:)[ \t]*"""
)
ALLOW_SECRET = re.compile(r"(?:#|\bREM|<!--)\s*pregate: allow-secret\b(.*?)(?:\s*-->)?\s*$")
# A brief line excusing one owning suite from the Suite owners check; the reason is required.
WAIVER = re.compile(r"^[ \t>*+-]*`?owners-waive:[ \t]*([^\s`]*)[ \t]*([^`\r\n]*)`?[ \t]*$")
STEP = "parse arguments"
TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".json",
    ".txt",
    ".cmd",
    ".ps1",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
    ".html",
    ".css",
    ".js",
}


def resolve_clone(value):
    path = Path(value)
    if path.exists():
        return path.resolve(strict=True)
    root = Path(__file__).resolve().parents[1]
    worktrees = root / ".claude/worktrees"
    if worktrees.exists():
        return (worktrees / value).resolve(strict=True)
    alternates = root / ".git/objects/info/alternates"
    if alternates.is_file():
        for entry in alternates.read_text(encoding="utf-8").splitlines():
            objects = Path(entry)
            if not objects.is_absolute():
                objects = root / ".git/objects" / objects
            worktrees = objects.resolve().parents[1] / ".claude/worktrees"
            if worktrees.exists():
                return (worktrees / value).resolve(strict=True)
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    worktrees = common.resolve().parent / ".claude/worktrees"
    return (worktrees / value).resolve(strict=True)


def encoding_offset(content):
    if content.startswith((b"\xff\xfe", b"\xfe\xff", b"\xc3\xbf\xc3\xbe")):
        return 0
    offset = content.find(b"\0")
    return offset if offset >= 0 else None


def allow_pragma(source, prose=False):
    """A quoted example is not an end-of-line comment."""
    quote = None
    position = 0
    while position < len(source):
        if quote and source[position] == "\\":
            position += 2
            continue
        if quote:
            if source.startswith(quote, position):
                position += len(quote)
                quote = None
                continue
        elif source[position] in "\"'":
            if (
                prose
                and source[position] == "'"
                and position > 0
                and re.match(r"\w'\w", source[position - 1 : position + 2])
            ):
                position += 1
                continue
            quote = source[position] * (
                3 if source.startswith(source[position] * 3, position) else 1
            )
            position += len(quote)
            continue
        elif match := ALLOW_SECRET.match(source, position):
            if source.startswith("<!--", position) and not source.rstrip().endswith("-->"):
                return None
            return match
        position += 1
    return None


def brief_selection(source):
    for match in re.finditer(r'-k\s+"([^"\r\n]+)"', source):
        selection = match[1].strip()
        if selection and selection != "..." and not re.search(r"<[^<>]*>", selection):
            return selection
    return None


def assignment_spans(source, path=""):
    """Yield literal value spans, including conservatively incomplete quoted values."""
    filename = Path(path).name.lower()
    code = Path(path).suffix.lower() in {
        ".py",
        ".js",
        ".ts",
        ".cmd",
        ".ps1",
        ".sh",
    } and not filename.startswith((".env", "dockerfile", "docker-compose"))
    for match in ASSIGNMENT.finditer(source):
        start = match.end()
        tail = source[start:]
        quoted = re.match(r"(?i)([rubf]*)(\"\"\"|'''|\"|')", tail)
        if quoted:
            prefix, quote = quoted.groups()
            start += quoted.end()
            closing = re.search(r"(?<!\\)" + re.escape(quote), source[start:])
            end = start + closing.start() if closing else len(source)
            value = source[start:end]
            if code and "f" in prefix.lower() and "{" in value:
                continue
            if closing:
                rest = source[end + len(quote) :].lstrip()
                if code and rest.startswith(("+", "-", "*", "/", "%", "&", "|", "^", ".", "[")):
                    continue
        else:
            # Bound container/scalar scanning to this pair's value.
            scalar = re.match(
                r"(?:true|false|null|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
                r"(?=$|[\s,;#}\]\)])",
                tail,
                re.IGNORECASE,
            )
            if scalar:
                value = scalar[0]
                if re.fullmatch(
                    r"tokens_in|tokens_out|max_tokens|\w+_tokens|token_count",
                    match["key"],
                    re.IGNORECASE,
                ) and re.fullmatch(r"[-+]?\d+", value):
                    continue
                if not re.fullmatch(r"true|false|null", value, re.IGNORECASE) and not (
                    Path(path).suffix.lower() in {".json", ".yaml", ".yml"}
                    and sum(char.isdigit() for char in value) <= 12
                ):
                    yield start, start + len(value)
                continue
            if tail.startswith(("[", "{")):
                depth = 0
                quote = None
                escaped = False
                for index, char in enumerate(tail):
                    if quote:
                        if escaped:
                            escaped = False
                        elif char == "\\":
                            escaped = True
                        elif char == quote:
                            quote = None
                    elif char in "\"'":
                        quote = char
                    elif char in "[{":
                        depth += 1
                    elif char in "]}":
                        depth -= 1
                        if depth == 0:
                            tail = tail[: index + 1]
                            break
                for item in re.finditer(
                    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#[^\r\n]*|[^\s\[\]{},;:#"']+""", tail
                ):
                    token = item[0]
                    if tail[item.end() :].lstrip().startswith(":"):
                        continue
                    if token.startswith("#"):
                        break
                    if re.fullmatch(r"true|false|null", token, re.IGNORECASE):
                        continue
                    if (
                        Path(path).suffix.lower() in {".json", ".yaml", ".yml"}
                        and re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", token)
                        and sum(char.isdigit() for char in token) <= 12
                    ):
                        continue
                    # Synthetic assignment reuses value/placeholder tests and offsets.
                    # An empty path selects config rules without numeric exclusions.
                    if token[:1] not in "\"'" and re.fullmatch(
                        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", token
                    ):
                        yield start + item.start(), start + item.end()
                    else:
                        for left, right in assignment_spans("token" + "=" + token):
                            yield start + item.start() + left - 6, start + item.start() + right - 6
                continue
            token = re.match(r"[^\s#,;}\])]+" if code else r"[^\s#,;]+", tail)
            if not token:
                continue
            value = token[0]
            end = start + len(value)
            if code and any(char in value for char in "(.["):
                continue
            if (
                code
                and Path(path).suffix.lower() not in {".cmd", ".ps1", ".sh"}
                and re.fullmatch(r"[a-z_]+|[A-Z_]+", value)
            ):
                continue
        value = value.strip()
        if (
            value
            and value.lower() not in {"none", "null", "lsu_..."}
            and not re.fullmatch(r"<[^<>]*>|\*+|\$\{\w+\}|%\w+%", value)
        ):
            yield start, end


def secret_assignment(source, path=""):
    return any(assignment_spans(source, path))


def redact_secrets(source):
    spans = list(assignment_spans(source))
    for match in SECRET.finditer(source):
        # Some credential detectors match only a provider prefix; redact its whole token.
        suffix = re.match(r"[A-Za-z0-9_-]*", source[match.end() :])
        spans.append((match.start(), match.end() + suffix.end()))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        source = source[:start] + "***" + source[end:]
    return source


def run(clone, argv, extra_env=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    env.update(extra_env or {})
    return subprocess.run(
        argv,
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def git(clone, *args):
    result = run(clone, ["git", *args])
    if result.returncode:
        raise ValueError("git command failed (diagnostics withheld to protect secrets)")
    return result.stdout.rstrip("\r\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scanned_tree_hash(clone, revision="HEAD"):
    """Hash tracked modes, object IDs and paths, excluding tool/reviewer records."""
    entries = git(clone, "ls-tree", "-r", "-z", "--full-tree", revision).split("\0")
    scanned = [
        entry
        for entry in entries
        if entry
        and not entry.split("\t", 1)[1].startswith(
            (f"{RECORD_DIR}/_pregate-", f"{RECORD_DIR}/_review-")
        )
        and not (
            entry.split("\t", 1)[1].startswith(f"{RECORD_DIR}/")
            and tool_output(entry.split("\t", 1)[1])
        )
    ]
    return hashlib.sha256("\0".join(sorted(scanned)).encode("utf-8")).hexdigest()


def branch_name(clone):
    branch = git(clone, "rev-parse", "--abbrev-ref", "HEAD")
    return re.sub(r"[^A-Za-z0-9_.-]", "-", branch)


def resolve_brief(clone, value):
    if not value:
        return None
    path = Path(value)
    return (path if path.is_absolute() else clone / path).resolve(strict=True)


def pytest_command(selection):
    return [
        PYTHON,
        "-m",
        "pytest",
        "tests/test_verify_suites.py",
        "-k",
        selection,
        "-p",
        "no:cacheprovider",
        "-q",
    ]


def tool_output(path):
    path = str(path).replace("\\", "/")
    return bool(
        re.fullmatch(rf"{re.escape(RECORD_DIR)}/_(?:pregate|review|gate)-[^/]+\.(?:md|json)", path)
        or re.fullmatch(rf"{re.escape(RECORD_DIR)}/_brief-[^/]+-reply\.md", path)
        # Generated npm lockfiles: package names like "@atlaskit/tokens": trip the
        # assignment heuristic. Named secret classes still apply to them.
        or path.split("/")[-1] == "package-lock.json"
    )


def prose_file(path):
    return Path(path).suffix.lower() == ".md"


def prose_code(source, fence):
    """Mask prose while preserving code offsets and the current fence delimiter."""
    marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", source)
    if fence:
        if (
            marker
            and marker[1][0] == fence[0]
            and len(marker[1]) >= len(fence)
            and not marker[2].strip()
        ):
            return "", None
        return source, fence
    if marker:
        return "", marker[1]
    masked = [" "] * len(source)
    for span in re.finditer(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", source):
        masked[span.start(2) : span.end(2)] = span[2]
    return "".join(masked), None


def removed_lines(diff, path=""):
    """Collect scanned removed text; Markdown needs full old-side fence context."""
    removed = set()
    if tool_output(path):
        return removed
    in_hunk = False
    fence = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            fence = None
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith(("-", " ")):
            source = line[1:]
            if prose_file(path):
                content, fence = prose_code(source, fence)
                # Mixed prose/code must not launder an unscanned assignment.
                if not content.strip() or any(
                    list(assignment_spans(source, scan_path))
                    != list(assignment_spans(content, scan_path))
                    for scan_path in ("sample.py", "")
                ):
                    continue
            if line.startswith("-"):
                removed.add(source)
    return removed


def secret_hits(diff, path, accepted=None, moved=None, exemptions=None):
    """Scan additions only; never return source lines or captured values."""
    hits = []

    def append_hit(source, line_no):
        location = f"{path}:{line_no}"
        if exemptions is not None and moved is not None and source in moved:
            # One removal may exempt several additions for section moves.
            exemptions.append(location)
        else:
            hits.append(location)

    line_no = 0
    fence = None
    for line in diff.splitlines():
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
        if hunk:
            line_no = int(hunk[1])
        elif not line_no:
            continue
        elif line.startswith("+"):
            source = line[1:]
            content = source
            scan_path = path
            if prose_file(path):
                content, fence = prose_code(source, fence)
                scan_path = "sample.py"
            if tool_output(path):
                content = ""
            pragma = allow_pragma(
                source if prose_file(path) and not tool_output(path) else content,
                prose=prose_file(path) and not fence,
            )
            reason = pragma[1].strip() if pragma else ""
            content = content[: pragma.start()] if pragma else content
            if SECRET.search(source) or (pragma and not reason):
                hits.append(f"{path}:{line_no}")
            elif pragma:
                if accepted is not None:
                    # A reason must never echo the value it is explaining.
                    for start, end in assignment_spans(content, scan_path):
                        reason = reason.replace(content[start:end], "***")
                    accepted.append(
                        {"location": f"{path}:{line_no}", "reason": redact_secrets(reason)}
                    )
            elif secret_assignment(content, scan_path):
                append_hit(source, line_no)
            line_no += 1
        elif line.startswith(" "):
            if prose_file(path):
                _, fence = prose_code(line[1:], fence)
            line_no += 1
    return hits


def annotated_revision(source):
    tree = ast.parse(source)
    declarations = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
    revisions = []
    for node in declarations:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == "revision" for target in targets):
            revisions.append(node)
    return (
        len(revisions) == 1
        and isinstance(revisions[0], ast.AnnAssign)
        and isinstance(revisions[0].annotation, ast.Name)
        and revisions[0].annotation.id == "str"
        and isinstance(revisions[0].value, ast.Constant)
        and isinstance(revisions[0].value.value, str)
        and bool(revisions[0].value.value)
    )


def whitespace_summary(result):
    locations = [
        f"{match['path']}:{match['line']}"
        for match in re.finditer(
            r"^(?P<path>[^:\n]+):(?P<line>\d+): (?P<msg>[^\n]+)\.$",
            result.stdout.replace("\r\n", "\n"),
            re.MULTILINE,
        )
        if not match["path"].startswith("+")
    ]
    return result.returncode == 0, (
        "exit 0"
        if result.returncode == 0
        else "\n".join(locations) or f"exit {result.returncode}; diagnostics withheld"
    )


AUDIT_SCRIPT = "audit_runtime_profiles.py"
AUDIT_VERDICT = re.compile(r"^DEP-AUDIT: (PASS|FAIL|WARN)$")
AUDIT_LINE = re.compile(r"^[ -~]{1,300}$")


def dependency_audit(clone, add):
    """pip-audit the clone's locks, profiles and ledger on EVERY run.

    Advisories are published daily, independent of the diff, so a cache keyed on the
    lock files would miss exactly the new advisories this step exists to catch; the
    audit reads only pinned inventories, without installing or resolving packages.
    The gate's own audit script runs against the clone's inputs, so an older branch
    is audited the same way. An incomplete audit is a blocking FAIL in the gate.
    """
    global STEP
    STEP = "dependency audit"
    script = Path(__file__).resolve().parent / AUDIT_SCRIPT
    argv = [PYTHON, str(script), "--local", "--root", str(clone)]
    start = time.monotonic()
    try:
        result = run(clone, argv)
        lines = result.stdout.replace("\r\n", "\n").strip().splitlines()
        verdict = AUDIT_VERDICT.match(lines[-1]) if lines else None
        expected = {0: "PASS", 1: "FAIL", 3: "WARN"}.get(result.returncode)
        if verdict and verdict[1] == expected:
            status = expected
            # The script composes these lines itself; withhold anything else.
            detail = [
                line
                for line in lines[:-1]
                if AUDIT_LINE.match(line) and not line.startswith("evidence: ")
            ]
            summary = "\n".join(detail) or f"exit {result.returncode}"
            if status == "WARN":
                status = "FAIL"
                summary = "Dependency audit incomplete; merge blocked.\n" + summary
        else:
            status, summary = "FAIL", f"exit {result.returncode}; no audit verdict (withheld)"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        status, summary = "FAIL", f'{type(error).__name__} during "{STEP}" (details withheld)'
    add(
        "Dependency audit",
        subprocess.list2cmdline(argv),
        status,
        summary,
        time.monotonic() - start,
    )


NATIVE_SCRIPT = "wheel_native_inventory.py"
NATIVE_ADVISORIES = "scripts/native_advisories.json"
NATIVE_VERDICT = re.compile(
    r"^NATIVE-INVENTORY: (\d+) components, (\d+) findings, (\d+) unverified$"
)
NATIVE_LINE = re.compile(r"^(FINDING|UNVERIFIED) [ -~]{1,300}$")


def native_library_scan(clone, add):
    """Scan the native libraries bundled in the installed environment (#559, #620).

    pip-audit sees Python distributions only, so a vulnerable libxml2 or libpq vendored in a
    wheel's `*.libs/` folder is invisible to the dependency audit above. This step runs the
    gate's own scanner over the interpreter's site-packages (offline, no download, no install)
    against the clone's maintained `scripts/native_advisories.json`, and BLOCKS like the
    dependency audit does: a finding is a FAIL, and so is any scanner error or an empty
    inventory (exit 2). UNVERIFIED lines (an advised library whose version cannot be read)
    are listed for a manual look and do not block. The CI half downloads the hash-locked
    wheels per profile (scripts/native_wheel_audit.py, dep-audit workflow).
    """
    global STEP
    STEP = "native library scan"
    script = Path(__file__).resolve().parent / NATIVE_SCRIPT
    advisories = Path(clone) / NATIVE_ADVISORIES
    if not advisories.is_file():  # an older clone predates the file: use the gate's own
        advisories = Path(__file__).resolve().parent / "native_advisories.json"
    argv = [PYTHON, str(script), "--installed", "--advisories", str(advisories)]
    start = time.monotonic()
    try:
        result = run(clone, argv)
        lines = result.stdout.replace("\r\n", "\n").strip().splitlines()
        verdict = NATIVE_VERDICT.match(lines[-1]) if lines else None
        # The exit code and the verdict line must agree: no verdict, or a clean exit that
        # reports findings (or the reverse), is a FAIL, never a pass.
        agrees = verdict and result.returncode == (1 if int(verdict[2]) else 0)
        if agrees:
            status = "FAIL" if result.returncode else "PASS"
            detail = [line for line in lines[:-1] if NATIVE_LINE.match(line)]
            summary = "\n".join([*detail, lines[-1]])
        else:
            status = "FAIL"
            summary = f"exit {result.returncode}; no native scan verdict (details withheld)"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        status, summary = "FAIL", f'{type(error).__name__} during "{STEP}" (details withheld)'
    add(
        "Native library scan",
        subprocess.list2cmdline(argv),
        status,
        summary,
        time.monotonic() - start,
    )


def descriptor_touch(paths, head_paths):
    """Whether the diff reaches a surface the descriptor ratchets (RATCHETS) guard."""
    return any(
        path.startswith(("app/web/", "app/cli/", "integrations/mcp-server/"))
        or path == "app/modules.py"
        for path in paths
        if path in head_paths
    )


def owner_waivers(source):
    """{suite: reason} from the brief's `owners-waive: verify_x reason` lines, plus the
    malformed lines (no suite name, or no reason), which fail the check."""
    waivers, bad = {}, []
    fence = None
    for number, line in enumerate(source.splitlines(), 1):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            fence = None if fence and marker[1][0] == fence[0] else (fence or marker[1])
            continue
        # An example inside a fenced or indented code block is not a waiver.
        if fence or re.match(r"^(?: {4}|\t)", line) or "owners-waive" not in line.lower():
            continue
        match = WAIVER.match(line)
        if not match:
            # Prose that quotes the syntax in an inline code span is documentation, not a
            # waiver; anything else that says owners-waive and does not parse is reported.
            if "owners-waive" not in re.sub(r"`[^`]*`", "", line).lower():
                continue
            bad.append(f"owners-waive line {number} does not parse (owners-waive: verify_x reason)")
            continue
        suite, reason = match[1], match[2].strip()
        if not re.fullmatch(r"verify_\w+", suite):
            bad.append(f"owners-waive line names no verify_ suite: {suite or '(blank)'}")
        elif len(reason.split()) < 2:
            bad.append(f"owners-waive {suite} has no reason (a reason of a few words is required)")
        else:
            waivers[suite] = redact_secrets(reason)
    return waivers, bad


UNMAPPED = re.compile(r"^owners-unmapped:\s+(\S+)\s+(\S.*)$")


def owner_unmapped_waivers(source):
    """{path: reason} from the brief's `owners-unmapped: <path> reason` lines (#524).

    A changed non-app path (golden, fixture, Dockerfile, alembic, lock) that no suite names
    fails the owner check; this line, with a reason of a few words, excuses one such path."""
    waived = {}
    fence = None
    for line in source.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            fence = None if fence and marker[1][0] == fence[0] else (fence or marker[1])
            continue
        match = None if fence else UNMAPPED.match(line.strip())
        if match and len(match[2].split()) >= 2:
            waived[match[1]] = redact_secrets(match[2].strip())
    return waived


def suite_owner_check(clone, merge_base, head, revision_range, brief_source, selection, add):
    """FAIL when a suite that owns a changed file was not collected (issue #182).

    Owners come from scripts/suite_owners.py (module, symbol, route or verb rules). A suite
    counts as collected when the brief's -k selects it or the pre-gate runs it anyway (the
    boundary ratchets always, the descriptor ratchets when the diff reaches their surface).
    Fails on ABSENCE only, never adds a suite to the run; a brief line
    `owners-waive: verify_x reason` excuses one owner and is printed in the record. Any
    internal error is a FAIL with a message, never a traceback.
    """
    global STEP
    STEP = "suite owners"
    start = time.monotonic()
    command_text = f"scripts/suite_owners.py {revision_range} (owners vs the brief selector)"
    try:
        import suite_owners
        from _pytest.mark.expression import Expression

        diff = git(clone, "diff", "--name-only", "--no-renames", "-z", revision_range)
        paths = [path for path in diff.split("\0") if path]
        head_paths = set(git(clone, "ls-tree", "-r", "--name-only", "-z", head).split("\0"))
        waivers, bad = owner_waivers(brief_source)
        if not any(
            suite_owners.candidate("M" if path in head_paths else "D", path) for path in paths
        ):
            nothing = "owners not derived for these paths (only app, scripts, deploy, "
            nothing += "integrations, tests, alembic, tools, Dockerfile, locks and pyproject "
            nothing += "are covered)"
            summary = "\n".join([nothing, *bad])
            add("Suite owners", command_text, "FAIL" if bad else "PASS", summary, 0)
            return
        report = suite_owners.derive(clone, merge_base, head)
        owners = report.suites()
        expression = Expression.compile(selection)
        ran = set(BOUNDARY_RATCHETS.split(" or "))
        if descriptor_touch(paths, head_paths):
            ran |= set(RATCHETS.split(" or "))
        collected = {
            suite
            for suite in owners
            if suite in ran
            or expression.evaluate(
                lambda keyword, suite=suite: any(
                    keyword.lower() in name.lower() for name in suite_owners.keyword_names(suite)
                )
            )
        }
        missing = [suite for suite in owners if suite not in collected and suite not in waivers]
        lines = [
            f"{len(owners)} owning suites for {len(report.owners)} changed files; "
            f"{len(collected)} collected, {len(missing)} missing, "
            f"{len([s for s in owners if s in waivers and s not in collected])} waived"
        ]
        for suite in missing:
            why = "; ".join(
                f"{path}: {', '.join(reasons)}" for path, reasons in report.reasons(suite)
            )
            lines.append(f"MISSING {suite} ({why})")
        if missing:
            lines.append('add to the brief -k: "' + " or ".join(missing) + '"')
        lines.extend(bad)
        for suite, reason in sorted(waivers.items()):
            note = "" if suite in owners else " (not an owner of this diff)"
            lines.append(f"waived {suite}: {reason}{note}")
        for path, reason, count in report.broad:
            lines.append(f"broad, not enforced: {path}: {reason} ({count} suites)")
        for suite in sorted(report.idle):
            lines.append(f"idle box only, not enforced: {suite}")
        # #524: a changed golden, fixture, Dockerfile, alembic or lock path that no suite names
        # used to pass with nothing run. It now fails unless the brief says why.
        unmapped = owner_unmapped_waivers(brief_source)
        unowned = [path for path in report.unowned if path not in unmapped]
        for path in unowned:
            lines.append(
                f"UNOWNED {path}: no verify suite names this path, so no suite would run for "
                "it; run the suites that read it and record `owners-unmapped: <path> reason` "
                "in the brief"
            )
        for path in report.unowned:
            if path in unmapped:
                lines.append(f"unmapped {path}: {unmapped[path]}")
        status = "FAIL" if missing or bad or unowned else "PASS"
        summary = "\n".join(lines)
    except Exception as error:  # this check must never crash the pre-gate
        status = "FAIL"
        summary = (
            f"internal error in the owner check: {type(error).__name__} (details withheld); "
            "run scripts/suite_owners.py on this range to see it"
        )
    add("Suite owners", command_text, status, summary, time.monotonic() - start)


def a11y_browser_check(clone, paths, source, add):
    """Every interface edit needs Chromium, unless the brief records a reason."""
    if not any(p.startswith(("app/templates/", "app/static/")) for p in paths):
        return
    waiver = re.search(r"(?m)^a11y-browser-waive: ([^\r\n]+)$", source)
    if waiver and len(waiver[1].strip().split()) >= 3:
        add("Keyboard browser", "a11y-browser-waive", "WARN", waiver[1].strip())
        return
    argv = pytest_command("verify_a11y_browser")
    result = run(clone, argv, {"LOC_TMS_A11Y_BROWSER_REQUIRED": "1"})
    add(
        "Keyboard browser",
        subprocess.list2cmdline(argv),
        "PASS" if result.returncode == 0 else "FAIL",
        "Required Chromium keyboard tests passed."
        if result.returncode == 0
        else "Required Chromium keyboard tests failed. Install Playwright and Chromium, "
        "or record a11y-browser-waive: <reason> in the brief. See the suite output.",
    )


def main():
    global STEP
    STEP = "parse arguments"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clone_dir")
    parser.add_argument("base_ref")
    parser.add_argument("brief", nargs="?", default=None)
    args = parser.parse_args()
    STEP = "resolve clone_dir"
    clone = resolve_clone(args.clone_dir)
    STEP = "resolve brief"
    brief = resolve_brief(clone, args.brief)
    STEP = "resolve revisions"
    head = git(clone, "rev-parse", "--verify", "HEAD^{commit}")
    tree_hash = scanned_tree_hash(clone, head)
    base = git(clone, "rev-parse", "--verify", "--end-of-options", args.base_ref + "^{commit}")
    merge_base = git(clone, "merge-base", base, head)
    revision_range = f"{merge_base}..{head}"
    branch = branch_name(clone)
    STEP = "read brief"
    initial_brief_hash = digest(brief) if brief else None
    brief_source = brief.read_text(encoding="utf-8-sig") if brief else ""
    selection_match = brief_selection(brief_source)
    selection = selection_match or DEFAULT_SELECTION
    items = []

    def add(name, command, status, summary, elapsed=0):
        items.append(
            {
                "name": name,
                "command": command,
                "status": status,
                "summary": summary,
                "wall_seconds": round(elapsed, 3),
            }
        )

    def command(name, argv, summarize=None):
        global STEP
        STEP = name
        start = time.monotonic()
        try:
            result = run(clone, argv)
            ok = result.returncode == 0
            summary = "exit 0" if ok else f"exit {result.returncode}; diagnostics withheld"
            if ok and argv[1:4] == ["-m", "ruff", "check"]:
                summary = "All checks passed!"
            elif ok and argv[1:4] == ["-m", "ruff", "format"]:
                match = re.search(r"\d+ files? already formatted", result.stdout)
                summary = match[0] if match else "format check passed"
            if summarize:
                ok, summary = summarize(result)
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
            ok, summary = False, f'{type(error).__name__} during "{STEP}" (details withheld)'
        add(
            name,
            subprocess.list2cmdline(argv),
            "PASS" if ok else "FAIL",
            summary,
            time.monotonic() - start,
        )

    def suite_summary(result):
        # Extract only pytest's numeric summary, never failure/source/parameter text.
        matches = re.findall(
            r"(?:\d+ (?:passed|failed|deselected|skipped|errors?|warnings?|xfailed|xpassed)"
            r"(?:, )?)+ in \d+(?:\.\d+)?s(?: \([\d:]+\))?",
            result.stdout,
        )
        return result.returncode == 0, matches[-1] if matches else f"exit {result.returncode}"

    command("Diff whitespace", ["git", "diff", "--check", revision_range], whitespace_summary)
    start = time.monotonic()
    STEP = "check clean tree"
    dirty = git(clone, "status", "--porcelain=v1", "--untracked-files=all")
    add(
        "Clean tree",
        "git status --porcelain=v1 --untracked-files=all",
        "FAIL" if dirty else "PASS",
        dirty or "clean",
        time.monotonic() - start,
    )
    command("Ruff lint", [PYTHON, "-m", "ruff", "check", "."])
    command("Ruff format", [PYTHON, "-m", "ruff", "format", "--check", "."])
    dependency_audit(clone, add)
    native_library_scan(clone, add)
    a11y_browser_check(
        clone,
        git(clone, "diff", "--name-only", revision_range).splitlines(),
        brief_source,
        add,
    )
    command(
        "Brief suites" + (" (FALLBACK: brief has no -k line)" if not selection_match else ""),
        pytest_command(selection),
        suite_summary,
    )
    brief_green = items[-1]["status"] == "PASS"
    if not selection_match and brief_green:
        items[-1]["status"] = "WARN"
    suite_owner_check(clone, merge_base, head, revision_range, brief_source, selection, add)

    def collection_summary(result):
        collected = set(
            re.findall(
                r"^tests/test_verify_suites\.py::test_verify_suite\[([^\]\r\n]+)\]$",
                result.stdout,
                re.MULTILINE,
            )
        )
        missing = set(BOUNDARY_RATCHETS.split(" or ")) - collected
        if result.returncode != 0:
            return False, f"collection exit {result.returncode}; diagnostics withheld"
        if missing:
            return False, "missing boundary suites: " + ", ".join(sorted(missing))
        return True, "all boundary suites collected"

    command(
        "Boundary collection",
        [*pytest_command(BOUNDARY_RATCHETS), "--collect-only"],
        collection_summary,
    )
    boundaries_exist = items[-1]["status"] == "PASS"

    from _pytest.mark.expression import Expression

    brief_expression = Expression.compile(selection)
    remaining = [
        name
        for name in BOUNDARY_RATCHETS.split(" or ")
        if not brief_expression.evaluate(lambda keyword, name=name: keyword.lower() in name.lower())
    ]

    def boundary_summary(result):
        if not boundaries_exist:
            return False, "boundary collection did not prove every required suite exists"
        return suite_summary(result)

    if remaining:
        command("Boundary ratchets", pytest_command(" or ".join(remaining)), boundary_summary)
    else:
        summary = (
            "covered by Brief suites (all required boundary ratchets)"
            if brief_green
            else "covered by Brief suites (FAILED there)"
        )
        if not boundaries_exist:
            summary += "; boundary collection did not prove every required suite exists"
        add(
            "Boundary ratchets", "", "PASS" if brief_green and boundaries_exist else "FAIL", summary
        )
    STEP = "inventory changed paths"
    paths = git(clone, "diff", "--name-only", "--no-renames", "-z", revision_range).split("\0")
    head_paths = set(git(clone, "ls-tree", "-r", "--name-only", "-z", head).split("\0"))
    paths = [path for path in paths if path]
    STEP = "check encoding"
    bad_encoding = []
    for path in paths:
        if path in head_paths and Path(path).suffix.lower() in TEXT_EXTENSIONS:
            offset = encoding_offset((clone / path).read_bytes())
            if offset is not None:
                bad_encoding.append(path)
                add(
                    "Encoding",
                    "check changed text bytes",
                    "FAIL",
                    f"{path}: NUL bytes / UTF-16 BOM at offset {offset}",
                )
    if descriptor_touch(paths, head_paths):
        command("Descriptor ratchets", pytest_command(RATCHETS), suite_summary)
    else:
        add("Descriptor ratchets", "diff path inventory", "SKIP", "no descriptor-trigger paths")
    start = time.monotonic()
    hits = []
    accepted = []
    skipped = {}
    STEP = "secret scan"
    moved = set()
    diffs = {}
    exemptions = []
    for path in paths:
        if path not in head_paths and tool_output(path):
            continue
        if path in bad_encoding:
            skipped[path] = f"{path}: skipped: invalid encoding"
            continue
        if tool_output(path):
            skipped[path] = f"{path}: skipped: tool output"
        diff = git(
            clone,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--unified=2147483647" if prose_file(path) else "--unified=0",
            revision_range,
            "--",
            path,
        )
        diffs[path] = diff
        moved.update(removed_lines(diff, path))
    for path, diff in diffs.items():
        hits.extend(secret_hits(diff, path, accepted, moved=moved, exemptions=exemptions))
        if not tool_output(path) and not re.search(r"^@@ ", diff, re.MULTILINE):
            skipped[path] = path
    add(
        "Secret scan",
        f"git diff --no-ext-diff --no-textconv {revision_range} (full context for prose)",
        "FAIL" if hits else "PASS",
        "\n".join(
            [
                *(hits or ["no secret hits"]),
                *([f"moved-line exemptions: {len(exemptions)}"] if exemptions else []),
                *exemptions,
            ]
        ),
        time.monotonic() - start,
    )
    if skipped:
        add(
            "Secret scan skipped paths",
            "tool outputs (assignment heuristic skipped); paths without textual hunks",
            "SKIP",
            "\n".join(skipped[path] for path in paths if path in skipped),
        )
    paths = [path for path in paths if path in head_paths]
    migrations = [path for path in paths if path.startswith("alembic/versions/")]
    STEP = "migration sanity"
    if migrations:

        def heads_summary(result):
            count = sum(
                bool(re.search(r"\(head\)\s*$", line)) for line in result.stdout.splitlines()
            )
            return result.returncode == 0 and count == 1, f"exit {result.returncode}; {count} heads"

        command("Migration heads", [PYTHON, "-m", "alembic", "heads"], heads_summary)
        start = time.monotonic()
        bad = []
        for path in migrations:
            if path.endswith(".py") and (clone / path).exists():
                try:
                    valid = annotated_revision((clone / path).read_text(encoding="utf-8-sig"))
                except (SyntaxError, UnicodeError):
                    valid = False
                if not valid:
                    bad.append(path)
        add(
            "Annotated revisions",
            "AST: revision: str = literal (changed surviving migrations)",
            "FAIL" if bad else "PASS",
            "\n".join(bad) or "house revision form",
            time.monotonic() - start,
        )
        command("Migration mirror", pytest_command("verify_migrations"), suite_summary)
    else:
        add("Migration sanity", "diff path inventory", "SKIP", "no migration changes")
    start = time.monotonic()
    STEP = "inventory gate records"
    gates = [
        (int(match[1]), path)
        for path in paths
        if (match := re.fullmatch(rf"{re.escape(RECORD_DIR)}/_gate-.*-round-(\d+)\.md", path))
    ]
    # Commit order defines newest across trains; round number breaks same-commit ties.
    if gates:
        order = git(clone, "rev-list", head).splitlines()
        ranks = {commit: rank for rank, commit in enumerate(order)}
        number, newest = min(
            gates,
            key=lambda pair: (
                ranks[git(clone, "log", "-1", "--format=%H", head, "--", pair[1])],
                -pair[0],
                pair[1],
            ),
        )
        missing = number > 0 and not (clone / (newest[:-3] + "-reply.md")).is_file()
        summary = newest + (": reply missing" if missing else ": reply present or round 0")
        add(
            "Reply record",
            "git log -1 per changed gate; matching reply path",
            "WARN" if missing else "PASS",
            summary,
            time.monotonic() - start,
        )
    else:
        add("Reply record", "diff path inventory", "SKIP", "no gate record in diff")
    STEP = "check input stability"
    stable = (
        git(clone, "rev-parse", "HEAD") == head
        and (digest(brief) if brief else None) == initial_brief_hash
        and git(clone, "status", "--porcelain=v1", "--untracked-files=all") == dirty
        and git(clone, "rev-parse", "--verify", "--end-of-options", args.base_ref + "^{commit}")
        == base
    )
    add(
        "Input stability",
        "recheck HEAD, base, brief SHA256 and git status",
        "PASS" if stable else "FAIL",
        "unchanged" if stable else "inputs changed during checks",
    )
    passed = all(item["status"] != "FAIL" for item in items)
    STEP = "write records"
    folder = clone / RECORD_DIR
    folder.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        record = folder / f"_pregate-{branch}-{number}.md"
        sidecar = record.with_suffix(".json")
        if record.exists():
            number += 1
            continue
        try:
            handle = sidecar.open("x", encoding="utf-8")
            break
        except FileExistsError:
            number += 1
    data = {
        "schema_version": 1,
        "clone": str(clone),
        "base_ref": args.base_ref,
        "base_commit": base,
        "merge_base": merge_base,
        "head": head,
        "scanned_tree_sha256": tree_hash,
        "branch": branch,
        "brief": str(brief) if brief else None,
        "brief_sha256": initial_brief_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "items": items,
        "record": str(record),
        "selection": selection,
        "accepted_secret_lines": accepted,
    }
    body = [
        f"# Mechanical pre-gate: {branch} / {number}",
        "",
        f"Verdict: {'PASS' if passed else 'FAIL'}",
        f"Clone: {clone}",
        f"Base: {args.base_ref} ({base})",
        f"Merge-base: {merge_base}",
        f"HEAD: {head}",
        f"Scanned tree SHA256: {tree_hash}",
        f"Brief: {brief or 'none (default selection)'}",
        "",
    ]
    for item in items:
        body.extend(
            [
                f"## {item['name']}: {item['status']}",
                "",
                "```text",
                item["command"],
                item["summary"],
                f"Wall time: {item['wall_seconds']:.3f}s",
                "```",
                "",
            ]
        )
    body.extend(
        [
            "## Accepted secret-shaped lines",
            "",
            "Named credential-class patterns (house keys, cloud key prefixes, PEM private keys) "
            "are NEVER allowable by pragma. A missing reason is a FAIL.",
            "",
            *[f"- {entry['location']}: {entry['reason']}" for entry in accepted],
            "" if accepted else "None.",
        ]
    )
    record.write_text("\n".join(body).replace("\u2014", "--").rstrip() + "\n", encoding="utf-8")
    data["record_sha256"] = digest(record)
    with handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print(f"PRE-GATE: {'PASS' if passed else 'FAIL'}")
    for item in items:
        if len(item["summary"].splitlines()) > 1:
            print(f"{item['status']:4} {item['name']} ({item['wall_seconds']:.2f}s):")
            for line in item["summary"].splitlines():
                print(f"  {line}")
            continue
        if item["name"] == "Encoding":
            print(f"FAIL Encoding: {item['summary']}")
            continue
        if item["name"] in {"Secret scan", "Diff whitespace"} and item["status"] == "FAIL":
            print(f"FAIL {item['name']} ({item['wall_seconds']:.2f}s):")
            for location in item["summary"].splitlines():
                print(location)
            continue
        summary = item["summary"].splitlines()[0]
        print(f"{item['status']:4} {item['name']}: {summary[:100]} ({item['wall_seconds']:.2f}s)")
    print(f"Record: {record}")
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f'PRE-GATE: FAIL - {type(error).__name__} during "{STEP}" (details withheld)')
        raise SystemExit(2) from None
