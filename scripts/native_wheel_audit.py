#!/usr/bin/env python3
"""Scan the native libraries inside the hash-locked wheels of one audit profile (#620).

The CI half of the #559 native-library coverage: for the profile's reviewed hash lock(s) it
downloads each pinned wheel (``--no-deps --only-binary=:all: --require-hashes``: nothing is
resolved, built or executed) and runs ``scripts/wheel_native_inventory.py`` over them. Profile
names are the dep-audit matrix names (base, optional profiles, vendor-studio, aux-*).

Exit 0: clean. Exit 1: a bundled library is below its fixed version. Exit 2: fail closed (lock
missing, a download or hash failure, no wheel scanned, scanner error). A pinned package with no
binary wheel for this platform (an sdist-only package) cannot vendor a library, so it is listed
as SKIPPED and does not fail; every other pip failure does.

The local pregate does not download anything: it scans the installed environment instead
(``wheel_native_inventory.py --installed``, see scripts/DEPENDENCY_AUDIT.md).
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from scripts.lock_inventory import VENDOR_LOCK, aux_lock_paths
except ImportError:  # run as `python scripts/native_wheel_audit.py`
    from lock_inventory import VENDOR_LOCK, aux_lock_paths

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = Path(__file__).resolve().parent / "wheel_native_inventory.py"
NO_WHEEL = re.compile(r"No matching distribution found|Could not find a version that satisfies")
# pip prints the same "No matching distribution" line when the index was merely unreachable
# (offline, DNS, proxy, TLS, a flaky mirror), so any sign of a network problem in its output
# makes the package an ERROR, never a SKIPPED one: a flaky network must not read as clean.
NETWORK_TROUBLE = re.compile(
    r"Retrying|Connection|Max retries|Temporary failure|Name or service|timed out|"
    r"ProxyError|SSLError|Could not fetch URL|Network is unreachable|HTTP error",
    re.IGNORECASE,
)


def aux_name(path):
    """Matrix name of a scripts/locks lock (mirrors audit_runtime_profiles.aux_label)."""
    if path.name == "requirements-saml-lxml.lock":
        return "saml-lxml-fallback"
    return "aux-" + path.stem.removeprefix("requirements-")


def profile_locks(root, profile):
    """The hash lock files whose wheels make up `profile`. Raises ValueError (fail closed)
    for an unknown profile or a missing lock."""
    root = Path(root)
    for path in aux_lock_paths(root):
        if aux_name(path) == profile:
            locks = [path]
            break
    else:
        base = root / "requirements.lock"
        if profile == "base":
            locks = [base]
        elif profile == "vendor-studio":
            locks = [base, root / VENDOR_LOCK]
        elif re.fullmatch(r"[a-z][a-z0-9-]*", profile):
            locks = [base, root / f"requirements-{profile}.lock"]
        else:
            raise ValueError(f"unknown profile name: {profile!r}")
    for lock in locks:
        if not lock.is_file():
            raise ValueError(f"missing lock for profile {profile}: {lock.name}")
    return locks


def lock_requirements(paths):
    """Hashed requirement lines (one per package) from the lock files, de-duplicated by line.
    Lines without a hash are refused: --require-hashes would refuse them anyway."""
    seen, out = set(), []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\\\n", "")
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "--hash=sha256:" not in line:
                raise ValueError(f"{Path(path).name}: requirement without a hash: {line[:60]}")
            if line not in seen:
                seen.add(line)
                out.append(line)
    if not out:
        raise ValueError("no requirements in the profile locks")
    return out


def download(line, dest, timeout=600):
    """Download one pinned wheel. Returns 'ok', 'no-wheel' or 'error'."""
    with tempfile.TemporaryDirectory(prefix="native-req-") as tmp:
        req = Path(tmp) / "one.txt"
        req.write_bytes((line + "\n").encode("utf-8"))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--no-deps",
                "--only-binary=:all:",
                "--require-hashes",
                "--disable-pip-version-check",
                "-q",
                "-d",
                str(dest),
                "-r",
                str(req),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    if result.returncode == 0:
        return "ok"
    if NO_WHEEL.search(result.stderr) and not NETWORK_TROUBLE.search(result.stderr):
        return "no-wheel"
    return "error"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", required=True)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--advisories", help="advisory file (default: scripts/native_advisories.json)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        locks = profile_locks(root, args.profile)
        lines = lock_requirements(locks)
    except (OSError, ValueError) as exc:
        print(f"NATIVE-WHEEL-AUDIT ERROR: {exc}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="native-wheels-") as tmp:
        skipped, failed = [], []
        for line in lines:
            name = line.split("--hash", 1)[0].strip()
            outcome = download(line, tmp)
            if outcome == "no-wheel":
                skipped.append(name)
            elif outcome == "error":
                failed.append(name)
        for name in skipped:
            print(f"SKIPPED {name}: no binary wheel for this platform (nothing to scan)")
        if failed:
            for name in failed:
                print(f"NATIVE-WHEEL-AUDIT ERROR: could not download {name}", file=sys.stderr)
            return 2
        wheels = sorted(str(p) for p in Path(tmp).glob("*.whl"))
        if not wheels:
            print("NATIVE-WHEEL-AUDIT ERROR: no wheels downloaded", file=sys.stderr)
            return 2
        argv2 = [sys.executable, str(INVENTORY)]
        if args.advisories:
            argv2 += ["--advisories", args.advisories]
        if args.profile in {aux_name(p) for p in aux_lock_paths(root)}:
            argv2.append("--allow-empty")  # build tools may bundle no native library at all
        result = subprocess.run(argv2 + wheels, check=False)
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
