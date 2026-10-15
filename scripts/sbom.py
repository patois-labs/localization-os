"""Write a CycloneDX JSON SBOM from the exact release locks (offline, deterministic).

Python dependencies ONLY: this covers the locked pip packages, not the OS packages of
the container or the Go binaries it carries. For the whole-image SBOM (Debian packages,
tailscale/tailscaled included) use the image scan: scripts/scan_image.sh writes a
CycloneDX image SBOM next to its reports (#226).

    python scripts/sbom.py                      # the shipped image: base + postgres locks
    python scripts/sbom.py --profile ldap       # add an opt-in host profile lock
    python scripts/sbom.py --profile none       # the SQLite host install (base lock only)
    python scripts/sbom.py --release v0.9.6     # stamp the release identity explicitly

Output: dist/sbom/localization-os-<release>-<profiles>.cdx.json (dist/ is gitignored).
The same locks and the same release always produce byte-identical output: no
timestamp, no random serial number, sorted keys, LF. The generator is the pinned
cyclonedx-bom from requirements-dev.txt; nothing is fetched from the network.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE_PROFILES = ("postgres",)  # Dockerfile: requirements.lock + requirements-postgres.lock


def lock_paths(root, profiles):
    paths = [root / "requirements.lock"]
    for profile in profiles:
        path = root / f"requirements-{profile}.lock"
        if not path.is_file():
            raise SystemExit(f"No lock for profile {profile!r}: {path.name} is missing")
        paths.append(path)
    return paths


def pinned_lines(paths):
    """Comment-free pin lines, markers kept, in a stable order (sorted by name)."""
    lines = []
    for path in paths:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            line = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line)
            if line:
                lines.append(line)
    return sorted(
        set(lines), key=lambda line: (re.split(r"[=<>!~; \[]", line, maxsplit=1)[0].lower(), line)
    )


def release_identity(root):
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build(root, profiles, release, output):
    lines = pinned_lines(lock_paths(root, profiles))
    with tempfile.TemporaryDirectory(prefix="loc-sbom-") as tmp:
        requirements = Path(tmp) / "release-locks.txt"
        requirements.write_bytes(("\n".join(lines) + "\n").encode("ascii"))
        raw = Path(tmp) / "bom.json"
        env = {k: v for k, v in os.environ.items() if k != "SOURCE_DATE_EPOCH"}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cyclonedx_py",
                "requirements",
                "--pyproject",
                str(root / "pyproject.toml"),
                "--mc-type",
                "application",
                "--spec-version",
                "1.6",
                "--output-reproducible",
                "--output-format",
                "JSON",
                "--output-file",
                str(raw),
                str(requirements),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        if result.returncode or not raw.is_file():
            sys.stderr.write(result.stderr)
            raise SystemExit(f"cyclonedx-py failed (exit {result.returncode})")
        bom = json.loads(raw.read_text(encoding="utf-8"))
    component = bom["metadata"]["component"]
    if release:
        component["version"] = release
    component["properties"] = [
        {
            "name": "localization-os:locks",
            "value": ",".join(p.name for p in lock_paths(root, profiles)),
        }
    ]
    # Temp-file line numbers must not leak into refs: key every component by its purl.
    refs = {}
    for item in bom.get("components", []):
        refs[item["bom-ref"]] = item["bom-ref"] = item["purl"]
        item["description"] = "locked pin: " + item["description"].split(": ", 1)[-1]
    for dependency in bom.get("dependencies", []):
        dependency["ref"] = refs.get(dependency["ref"], dependency["ref"])
        if "dependsOn" in dependency:
            dependency["dependsOn"] = sorted(refs.get(r, r) for r in dependency["dependsOn"])
    bom["components"] = sorted(bom.get("components", []), key=lambda item: item["bom-ref"])
    bom["dependencies"] = sorted(bom.get("dependencies", []), key=lambda item: item["ref"])
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(bom, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    output.write_bytes(text.encode("ascii"))
    return bom


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--profile",
        action="append",
        help="extra lock profile (postgres, ldap, saml); repeatable; 'none' = base lock only. "
        "Default: the shipped container image (postgres).",
    )
    parser.add_argument("--release", help="release identity (default: git describe)")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--output", help="output file (default under dist/sbom/)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.profile is None:
        profiles = list(IMAGE_PROFILES)
    elif args.profile == ["none"]:
        profiles = []
    else:
        profiles = sorted(set(args.profile) - {"none"})
    release = args.release or release_identity(root)
    label = re.sub(r"[^A-Za-z0-9_.-]", "-", release or "unreleased")
    name = "-".join(["base", *profiles])
    output = (
        Path(args.output)
        if args.output
        else root / f"dist/sbom/localization-os-{label}-{name}.cdx.json"
    )
    bom = build(root, profiles, release, output)
    print(f"SBOM: {len(bom.get('components', []))} components -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
