#!/usr/bin/env python3
"""Inventory native libraries bundled inside Python wheels and match them to advisories.

pip-audit sees only Python distributions and Trivy sees OS packages, so a vulnerable
libxml2 vendored in a wheel's ``*.libs/`` folder is invisible to both (GitHub #559).
This scanner opens each wheel (zip) offline, finds bundled shared libraries
(``.so``, ``.dylib``, ``.dll``), derives name and version from the file name (auditwheel
style ``libxml2-<hash>.so.2.9.10``) or from an embedded ``<name> <version>`` string, and
matches them against ``scripts/native_advisories.json``. No network, no wheel execution.

Usage: python scripts/wheel_native_inventory.py [--advisories FILE] [--json] [--allow-empty]
       (WHEEL... | DIR... | --installed)
A DIR is walked for the same library files (an unpacked environment); --installed scans the
site-packages of the interpreter running the scanner (offline: this is what pregate runs).
Exit 0: no findings; 1: advisory findings; 2: usage error, unreadable wheel, invalid or empty
advisory file, or no native library found at all (unless --allow-empty). An advised component
whose version cannot be read is printed as UNVERIFIED and warned on stderr.
"""

import argparse
import json
import re
import sys
import sysconfig
import zipfile
from pathlib import Path

DEFAULT_ADVISORIES = Path(__file__).resolve().parent / "native_advisories.json"
_LIB_EXT = re.compile(r"\.(so(\.[0-9][0-9.]*)?|dylib|dll)$", re.IGNORECASE)
_NAME_VERSION = re.compile(r"^(?:lib)?([A-Za-z][A-Za-z0-9_+]*?)(?:-\d+)?(?:-[0-9a-f]{8,})?$")
_FILE_VERSION = re.compile(
    r"\.so\.(\d+(?:\.\d+)*)$|[-.](\d+(?:\.\d+)*)\.(?:dll|dylib)$", re.IGNORECASE
)
_EMBEDDED = re.compile(rb"\b(lib[a-z0-9_]+|openssl|OpenSSL)[ /-]v?(\d+\.\d+(?:\.\d+)*[a-z]?)\b")


def _version_key(text):
    return tuple(int(p) for p in re.findall(r"\d+", text))


def _component_name(filename):
    stem = _LIB_EXT.sub("", filename).lower()
    # macOS names carry the version before the extension: libxml2.2.9.10.dylib
    if filename.lower().endswith(".dylib"):
        stem = stem.split(".")[0]
    if not stem.startswith("lib"):
        stem = "lib" + stem
    stem = re.sub(r"-[0-9a-f]{8,}$", "", stem)
    stem = re.sub(r"-\d+(\.\d+)*$", "", stem)
    return stem


_REQUIRED_KEYS = ("id", "severity", "affected_below", "summary", "source")


def load_advisories(path=DEFAULT_ADVISORIES):
    """Load and validate the advisory file; any gap raises ValueError (fail closed)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: advisory file must be a JSON object")
    advisories = {k.lower(): v for k, v in data.items() if not k.startswith("_")}
    if not advisories:
        raise ValueError(
            f"{path}: advisory file has no components; refusing to report a clean scan"
        )
    for component, entries in advisories.items():
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{path}: {component} must be a non-empty list of advisories")
        for entry in entries:
            missing = [k for k in _REQUIRED_KEYS if not isinstance(entry, dict) or not entry.get(k)]
            if missing:
                raise ValueError(f"{path}: {component} advisory missing {', '.join(missing)}")
            if not _version_key(str(entry["affected_below"])):
                raise ValueError(f"{path}: {component} {entry['id']} has no numeric affected_below")
    return advisories


def _classify(base, read, source, path):
    """One library file -> {wheel, path, component, version}. `read` returns its bytes and is
    called only when the file name carries no release."""
    component = _component_name(base)
    version = None
    m = _FILE_VERSION.search(base)
    if m:
        version = m.group(1) or m.group(2)
        # A bare soname such as libxml2.so.2 or libxml2-2.dll names an ABI, not a
        # release: treat it as unknown so it neither matches nor falsely flags.
        if version and "." not in version:
            version = None
    if version is None:
        for m in _EMBEDDED.finditer(read()):
            if (
                _component_name(m.group(1).decode()) == component
                or component in m.group(1).decode().lower()
            ):
                version = m.group(2).decode()
                break
    return {"wheel": source, "path": path, "component": component, "version": version}


def inventory_wheel(wheel):
    """Return [{wheel, path, component, version}] for bundled native libraries."""
    found = []
    with zipfile.ZipFile(wheel) as zf:
        for info in zf.infolist():
            base = info.filename.rsplit("/", 1)[-1]
            if info.is_dir() or not _LIB_EXT.search(base):
                continue
            found.append(
                _classify(base, lambda info=info: zf.read(info), Path(wheel).name, info.filename)
            )
    return found


def inventory_tree(root):
    """Same inventory over an unpacked directory (an installed environment's site-packages).
    Raises OSError for a missing or unreadable root, so the caller fails closed."""
    root = Path(root)
    if not root.is_dir():
        raise OSError(f"not a directory: {root.name}")
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not _LIB_EXT.search(path.name):
            continue
        found.append(
            _classify(path.name, path.read_bytes, root.name, path.relative_to(root).as_posix())
        )
    return found


def installed_roots():
    """site-packages directories of the running interpreter (purelib and platlib)."""
    paths = sysconfig.get_paths()
    return sorted({paths[k] for k in ("purelib", "platlib") if paths.get(k)})


def scan(wheels, advisories):
    components, findings = [], []
    for wheel in wheels:
        items = inventory_tree(wheel) if Path(wheel).is_dir() else inventory_wheel(wheel)
        for item in items:
            components.append(item)
            if item["version"] is None:
                # An advised component whose release cannot be read is not silently clean:
                # it is reported as UNVERIFIED (see main) so a reviewer checks it by hand.
                item["unverified"] = item["component"] in advisories
                continue
            for adv in advisories.get(item["component"], []):
                if _version_key(item["version"]) < _version_key(adv["affected_below"]):
                    findings.append(
                        {
                            **item,
                            "advisory": adv["id"],
                            "severity": adv["severity"],
                            "fixed_in": adv["affected_below"],
                            "summary": adv["summary"],
                        }
                    )
    return components, findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wheels", nargs="*", help="wheel files or directories to scan")
    ap.add_argument(
        "--installed",
        action="store_true",
        help="scan the site-packages of the interpreter running this script",
    )
    ap.add_argument("--advisories", default=str(DEFAULT_ADVISORIES))
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--allow-empty",
        action="store_true",
        help="exit 0 when the wheels bundle no native libraries (default: exit 2)",
    )
    args = ap.parse_args(argv)
    if args.installed:
        args.wheels = [*args.wheels, *installed_roots()]
    if not args.wheels:
        ap.error("give at least one wheel or directory, or --installed")
    try:
        advisories = load_advisories(args.advisories)
        components, findings = scan(args.wheels, advisories)
    except Exception as exc:  # fail closed: any parse or read error is exit 2, never a clean scan
        print(f"NATIVE-INVENTORY ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not components and not args.allow_empty:
        print(
            f"NATIVE-INVENTORY ERROR: no native libraries found in {len(args.wheels)} wheel(s); "
            "pass --allow-empty if that is expected",
            file=sys.stderr,
        )
        return 2
    unverified = [c for c in components if c.get("unverified")]
    if args.json:
        print(
            json.dumps(
                {"components": components, "findings": findings, "unverified": unverified}, indent=2
            )
        )
    else:
        for c in components:
            print(
                f"component {c['component']} {c['version'] or 'unknown'} "
                f"in {c['wheel']}:{c['path']}"
            )
        for c in unverified:
            print(
                f"UNVERIFIED {c['component']} has advisories but no readable version "
                f"{c['wheel']}:{c['path']}"
            )
        for f in findings:
            print(
                f"FINDING {f['advisory']} {f['severity']} {f['component']} {f['version']} "
                f"(fixed in {f['fixed_in']}) {f['wheel']}:{f['path']}"
            )
        print(
            f"NATIVE-INVENTORY: {len(components)} components, {len(findings)} findings, "
            f"{len(unverified)} unverified"
        )
    if unverified:
        print(
            f"NATIVE-INVENTORY WARNING: {len(unverified)} advised component(s) with unknown "
            "version; check them by hand",
            file=sys.stderr,
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
