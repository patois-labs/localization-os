"""Audit reviewed pinned inventories without installing or resolving packages.

Both CI and the local pregate use pip-audit with --no-deps --disable-pip.
Optional profiles require requirements-NAME.lock; missing locks block the gate.
Exit 3 means advisory transport failed, and is blocking in the pregate.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.lock_inventory import DEV_LOCK, aux_lock_paths
except ImportError:  # run as `python scripts/audit_runtime_profiles.py`
    from lock_inventory import DEV_LOCK, aux_lock_paths

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ".github/pip-audit-ignores.txt"
EXIT_WARN = 3
# The SAML-only lxml fallback (Dockerfile SAML_LXML=6.0.0, issues #253 and #407) replaces lxml,
# so it cannot join the combined inventory. It is audited as its own inventory. Its recorded
# advisory is a non-blocking WARNING until the exception below expires (UTC date, inclusive);
# any other advisory on it still fails. Extending the date is a deliberate, reviewed edit.
FALLBACK_LABEL = "saml-lxml-fallback"
FALLBACK_LOCK = "scripts/locks/requirements-saml-lxml.lock"
FALLBACK_ACCEPTED = {"PYSEC-2026-87": "2027-04-06"}
# Transport failures only. A resolver or advisory verdict is never classified as offline.
OFFLINE = re.compile(
    r"ConnectionError|NewConnectionError|ProxyError|ConnectTimeout|ReadTimeout|"
    r"Max retries exceeded|NameResolutionError|getaddrinfo failed|"
    r"Temporary failure in name resolution|connection broken by|"
    r"Failed to establish a new connection|timed out"
)


def runtime_profiles(root=ROOT):
    profiles = {
        "base": [],
        "vendor-studio": ["--no-deps", "-r", str(root / "tools/vendor_studio/requirements.txt")],
    }
    for path in sorted(root.glob("requirements-*.txt")):
        if path.name != "requirements-dev.txt":
            profiles[path.stem.removeprefix("requirements-")] = [
                "--no-deps",
                "-r",
                str(path.with_suffix(".lock")),
            ]
    optional = json.loads((root / ".github/runtime-profiles.json").read_text())
    if not isinstance(optional, dict):
        raise ValueError("Expected an optional profile mapping")
    # "locked" is the local audit's combined inventory key; a profile of that name would
    # replace it and shrink the scan (gate L1).
    collisions = (profiles.keys() | {"locked"}) & optional.keys()
    if collisions:
        raise ValueError(f"Optional profiles collide with locked profiles: {sorted(collisions)}")
    from packaging.requirements import Requirement

    for name, entries in optional.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise ValueError("Invalid optional profile name")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{name}: expected nonempty requirement list")
        for entry in entries:
            if not isinstance(entry, str) or entry.lstrip().startswith("-"):
                raise ValueError(f"{name}: pip options are forbidden")
            req = Requirement(entry)
            if req.url:
                raise ValueError(f"{name}: direct URLs are forbidden")
    profiles.update(optional)
    return profiles


def ledger_ids(content):
    """Python twin of the CI parser `awk '$1 !~ /^#/ && NF {print $1}'`."""
    ids = []
    for line in content.split("\n"):
        fields = line.split()
        if fields and not fields[0].startswith("#"):
            ids.append(fields[0])
    return ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", required=True, choices=sorted([*runtime_profiles(), *aux_labels()])
    )
    args = parser.parse_args()
    output = ROOT / "_audit-results"
    output.mkdir(exist_ok=True)
    status, lines = local_audit(ROOT, output, profile=args.profile)
    for line in lines:
        print(line)
    raise SystemExit(0 if status == "PASS" else 1)


# --------------------------------------------------------------------------- local


def _python(*module):
    """Interpreter argv that cannot import from the audited checkout.

    `-P` (safe path) keeps the working directory off sys.path, so a branch that ships its
    own top-level `pip_audit` or `pip` package cannot stand in for the real tool."""
    return [sys.executable, "-P", "-m", *module]


def _quiet(argv, timeout, cwd):
    # cwd is the evidence directory, never the checkout, as a second line of defence.
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        cwd=cwd,
        env={
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "SYSTEMROOT",
                "WINDIR",
                "PATH",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
                "LOCALAPPDATA",
                "APPDATA",
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "NO_PROXY",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE",
            }
        },
    )


def file_pins(paths):
    """Read exact pins only. Pip options, URLs and empty inventories fail closed."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pins = set()
    for path in paths:
        count = 0
        # Hash annotations are data, never pip arguments. Validate their syntax.
        content = path.read_text(encoding="utf-8").replace("\\\n", "")
        for raw in content.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            line = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line)
            req = Requirement(line)
            specs = list(req.specifier)
            if req.url or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
                raise ValueError(f"{path.name}: not a single == pin: {req}")
            pins.add(f"{canonicalize_name(req.name)}=={specs[0].version}")
            count += 1
        if not count:
            raise ValueError(f"{path.name}: empty inventory")
    return sorted(pins)


def locked_input_paths(root):
    """Files feeding the combined inventory: base/profile locks plus the independently
    deployed Vendor Studio pins."""
    return [
        root / "requirements.lock",
        # The dev lock is audited separately and advisory-only (dev_tool_pins, GitHub #412).
        *(p for p in sorted(root.glob("requirements-*.lock")) if p.name != DEV_LOCK),
        *(
            path.with_suffix(".lock")
            for path in sorted(root.glob("requirements-*.txt"))
            if path.name != "requirements-dev.txt"
        ),
        root / "tools/vendor_studio/requirements.txt",
        root / "tools/vendor_studio/requirements.lock",
    ]


def locked_pins(root):
    """Base/profile locks plus the independently deployed Vendor Studio pins."""
    return file_pins(locked_input_paths(root))


def aux_label(path):
    """Inventory name of a scripts/locks lock: the SAML lxml fallback keeps its own name."""
    if path.name == Path(FALLBACK_LOCK).name:
        return FALLBACK_LABEL
    return "aux-" + path.stem.removeprefix("requirements-")


def aux_inventories(root):
    """Every scripts/locks lock as its own inventory (GitHub #558). Each one replaces or
    sits beside the runtime closure (build tools, CI check tools, the lxml fallback), so none
    can join the combined inventory. A new lock there is audited automatically."""
    return {aux_label(path): file_pins([path]) for path in aux_lock_paths(root)}


def aux_labels(root=ROOT):
    return [aux_label(path) for path in aux_lock_paths(root)]


def fallback_inventories(root):
    """The SAML lxml fallback pins as their own inventory (empty when the lock is absent)."""
    return {k: v for k, v in aux_inventories(root).items() if k == FALLBACK_LABEL}


def audited_lock_paths(root):
    """Every install lock whose pins reach a blocking audit inventory: the combined one and
    the per-lock auxiliary ones. The dev lock has its own advisory pass (GitHub #412)."""
    root = Path(root)
    combined = {p for p in locked_input_paths(root) if p.suffix == ".lock"}
    return sorted(combined | set(aux_lock_paths(root)))


def audit_pins(label, pins, ignores, output, timeout, accepted=None):
    """Audit exact pins without resolving; returns (status, lines).

    `accepted` maps advisory id -> expiry date (YYYY-MM-DD). While unexpired, that advisory
    is reported as a WARNING line and does not fail; every other finding still fails."""
    requirements_file = output / f"{label}-pins.txt"
    requirements_file.write_bytes(("\n".join(pins) + "\n").encode("ascii"))
    report = output / f"{label}.json"
    report.unlink(missing_ok=True)  # a stale report must never stand in for this run
    result = _quiet(
        [
            *_python("pip_audit"),
            "--strict",
            "--no-deps",
            "--disable-pip",
            "--progress-spinner",
            "off",
            "-r",
            str(requirements_file),
            "--format=json",
            "--output",
            str(report),
            *ignores,
        ],
        timeout,
        output,
    )
    data = None
    if report.is_file() and report.stat().st_size:
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except ValueError:
            data = None
    if data is not None:
        today = datetime.now(timezone.utc).date().isoformat()
        live = {i for i, expiry in (accepted or {}).items() if expiry >= today}
        found, warned = [], []
        for dep in data.get("dependencies", []):
            for vuln in dep.get("vulns", []):
                line = f"{label}: {dep['name']} {dep['version']} {vuln['id']}" + (
                    f" (fixed in {', '.join(vuln['fix_versions'])})"
                    if vuln.get("fix_versions")
                    else ""
                )
                if vuln["id"] in live:
                    warned.append(
                        f"WARNING (non-blocking, accepted until {accepted[vuln['id']]}): {line}"
                    )
                else:
                    found.append(line)
        skipped = [
            f"{label}: {dep['name']} not auditable: {dep.get('skip_reason', 'skipped')}"
            for dep in data.get("dependencies", [])
            if "skip_reason" in dep
        ]
        # pip-audit exits 1 when it lists vulnerabilities; with only accepted ones that is the
        # expected code, any other non-zero code is a tool failure and still fails.
        if found or skipped or (result.returncode and (not warned or result.returncode != 1)):
            # pip-audit can list one advisory more than once; report each line once.
            lines = list(dict.fromkeys(found + skipped))
            return "FAIL", lines or [f"{label}: pip-audit exit {result.returncode}"]
        # A short, empty or foreign report is not evidence that THESE pins are clean.
        from packaging.utils import canonicalize_name

        reported = sorted(canonicalize_name(dep["name"]) for dep in data.get("dependencies", []))
        wanted = sorted(canonicalize_name(pin.split("==", 1)[0]) for pin in pins)
        if reported != wanted:
            return "FAIL", [
                f"{label}: report lists {len(reported)} packages, {len(wanted)} pins audited"
                + ("" if len(reported) != len(wanted) else " (names differ)")
            ]
        summary = f"{label}: {len(reported)} packages, " + (
            "only accepted advisories" if warned else "no known vulns"
        )
        return "PASS", [*dict.fromkeys(warned), summary]
    if OFFLINE.search(result.stderr):
        return "WARN", [f"{label}: vulnerability database unreachable (offline?)"]
    return "FAIL", [f"{label}: pip-audit exit {result.returncode}, no report"]


def profile_pins(root, name, entries):
    """An optional profile must have a reviewed closure; never ask pip to resolve it."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    path = root / f"requirements-{name}.lock"
    pins = file_pins([root / "requirements.lock", path])
    versions = {}
    for pin in pins:
        package, version = pin.split("==")
        versions.setdefault(package, set()).add(version)
    for entry in entries:
        req = Requirement(entry)
        found = versions.get(canonicalize_name(req.name), set())
        if not found or any(not req.specifier.contains(v, prereleases=True) for v in found):
            raise ValueError(f"{name}: requirement absent or inconsistent with reviewed lock")
    return pins


def local_audit(root, output, include_optional=True, timeout=600, profile=None):
    """Return (status, summary lines). Validate all inputs before contacting advisories."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from release_lock import ledger_errors

    try:
        content = (root / LEDGER).read_text(encoding="utf-8")
        errors = ledger_errors(content, datetime.now(timezone.utc).date())
        if errors:
            return "FAIL", [f"ledger: {error}" for error in errors]
        ids = ledger_ids(content)
        ignores = [arg for advisory in ids for arg in ("--ignore-vuln", advisory)]
        profiles = runtime_profiles(root)
        inventories = {}
        if profile is None:
            inventories["locked"] = locked_pins(root)
        for label, pins in aux_inventories(root).items():
            if profile is None or profile == label:
                inventories[label] = pins
        for name, entries in profiles.items():
            if profile is not None and profile != name:
                continue
            if not entries or entries[0] == "--no-deps":
                if profile is not None:
                    paths = [root / "requirements.lock"]
                    if entries:
                        paths.append(Path(entries[-1]))
                    if name == "vendor-studio":
                        paths.append(root / "tools/vendor_studio/requirements.lock")
                    inventories[name] = file_pins(paths)
            elif include_optional or profile is not None:
                inventories[name] = profile_pins(root, name, entries)
    except (OSError, ValueError) as exc:
        return "FAIL", [f"inventory: {exc}"]

    statuses, lines = [], []
    for label, pins in inventories.items():
        try:
            accepted = FALLBACK_ACCEPTED if label == FALLBACK_LABEL else None
            status, found = audit_pins(label, pins, ignores, output, timeout, accepted)
        except subprocess.TimeoutExpired:
            status, found = "WARN", [f"{label}: timed out; audit NOT performed"]
        statuses.append(status)
        lines.extend(found)
    lines.append(f"ledger ignores applied: {', '.join(ids) if ids else 'none'}")
    for verdict in ("FAIL", "WARN"):
        if verdict in statuses:
            return verdict, lines
    return "PASS", lines


def dev_tool_pins():
    """Audit the actual developer environment, including pip and bundled setuptools."""
    from importlib.metadata import distributions

    return sorted({f"{d.metadata['Name']}=={d.version}" for d in distributions()})


def local_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", required=True)
    parser.add_argument("--root", default=str(ROOT), help="checkout whose locks are audited")
    parser.add_argument("--output", help="evidence directory (default: a fresh temp dir)")
    parser.add_argument("--locked-only", action="store_true", help="skip optional profiles")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    output = Path(args.output or tempfile.mkdtemp(prefix="loc-dep-audit-"))
    output.mkdir(parents=True, exist_ok=True)
    try:
        status, lines = local_audit(root, output, not args.locked_only, args.timeout)
    except subprocess.TimeoutExpired:
        status, lines = "WARN", [f"audit timed out after {args.timeout}s; audit NOT performed"]
    try:
        dev_status, dev_lines = audit_pins("dev-tools", dev_tool_pins(), [], output, args.timeout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        dev_status, dev_lines = "WARN", ["dev-tools: audit unavailable"]
    lines.extend(f"dev-tools ({dev_status}, advisory only): {line}" for line in dev_lines)
    if status == "WARN":
        lines.insert(0, "WARNING: dependency audit incomplete, NOT a pass")
    for line in lines:
        print(line)
    print(f"evidence: {output}")
    print(f"DEP-AUDIT: {status}")
    return {"PASS": 0, "FAIL": 1, "WARN": EXIT_WARN}[status]


if __name__ == "__main__":
    if "--local" in sys.argv[1:]:
        raise SystemExit(local_main())
    main()
