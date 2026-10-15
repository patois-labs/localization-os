"""SBOM generator: offline, deterministic CycloneDX JSON from the exact release locks."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_lock import canonicalize_name, requirements

# Any network attempt fails fast instead of silently succeeding.
OFFLINE = {
    **os.environ,
    "HTTP_PROXY": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "",
    "PIP_NO_INDEX": "1",
    "SOURCE_DATE_EPOCH": "1700000000",  # must be ignored: the output carries no timestamp
}


def generate(output, *args):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/sbom.py"), "--output", str(output), *args],
        capture_output=True,
        text=True,
        env=OFFLINE,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SBOM: " in result.stdout
    return output.read_bytes()


def pins(*locks):
    found = set()
    for lock in locks:
        for req in requirements(ROOT / lock):
            found.add((canonicalize_name(req.name), next(iter(req.specifier)).version))
    return found


def test_generator_is_pinned():
    import importlib.util

    assert importlib.util.find_spec("cyclonedx_py"), (
        "cyclonedx-bom missing: pip install -r requirements-dev.txt"
    )
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "\ncyclonedx-bom==" in dev and "\npip-audit==" in dev
    assert "dist/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_image_sbom_deterministic_and_complete():
    with tempfile.TemporaryDirectory(prefix="verify_sbom_") as tmp:
        first = generate(Path(tmp) / "a.json", "--release", "v9.9.9-test")
        second = generate(Path(tmp) / "b.json", "--release", "v9.9.9-test")
        assert first == second, "SBOM output is not byte-identical across runs"
        assert b"\r" not in first and first.endswith(b"\n")
        first.decode("ascii")
        assert tmp.encode() not in first and b"release-locks.txt" not in first
        bom = json.loads(first)
        assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.6"
        assert "serialNumber" not in bom and "timestamp" not in bom["metadata"]
        root = bom["metadata"]["component"]
        assert root["name"] == "localization-os" and root["version"] == "v9.9.9-test"
        assert root["properties"] == [
            {
                "name": "localization-os:locks",
                "value": "requirements.lock,requirements-postgres.lock",
            }
        ]
        components = {
            (canonicalize_name(item["name"]), item["version"]) for item in bom["components"]
        }
        # Every pin of the image locks, including platform-marked ones, and nothing else.
        assert components == pins("requirements.lock", "requirements-postgres.lock")
        for item in bom["components"]:
            assert item["purl"].startswith("pkg:pypi/") and item["bom-ref"] == item["purl"]
            assert item["type"] == "library"
        refs = [item["bom-ref"] for item in bom["components"]]
        assert refs == sorted(refs)
        assert {dep["ref"] for dep in bom["dependencies"]} <= {*refs, root["bom-ref"]}


def test_profiles():
    with tempfile.TemporaryDirectory(prefix="verify_sbom_") as tmp:
        base = json.loads(generate(Path(tmp) / "base.json", "--profile", "none", "--release", "x"))
        names = {canonicalize_name(item["name"]) for item in base["components"]}
        assert names == {name for name, _ in pins("requirements.lock")}
        assert "psycopg" not in names
        ldap = json.loads(generate(Path(tmp) / "ldap.json", "--profile", "ldap", "--release", "x"))
        names = {canonicalize_name(item["name"]) for item in ldap["components"]}
        assert "ldap3" in names and "psycopg" not in names
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/sbom.py"), "--profile", "nonexistent"],
            capture_output=True,
            text=True,
            env=OFFLINE,
            check=False,
        )
        assert result.returncode != 0 and "nonexistent" in result.stderr


def test_default_output_path():
    source = (ROOT / "scripts/sbom.py").read_text(encoding="utf-8")
    assert 'root / f"dist/sbom/localization-os-{label}-{name}.cdx.json"' in source
    wrapper = (ROOT / "scripts/sbom.cmd").read_text(encoding="utf-8")
    assert "sbom.py" in wrapper and ".venv" in wrapper
    docs = (ROOT / "docs/DEPLOY.md").read_text(encoding="utf-8")
    assert "scripts\\sbom.cmd" in docs and "dist/sbom/" in docs


def main():
    tests = (
        test_generator_is_pinned,
        test_image_sbom_deterministic_and_complete,
        test_profiles,
        test_default_output_path,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("SBOM OK")


if __name__ == "__main__":
    main()
