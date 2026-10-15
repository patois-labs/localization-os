"""Offline regressions for GitHub #559: native-library coverage inside wheels was never
demonstrated.

pip-audit sees Python distributions and Trivy sees OS packages; a vulnerable libxml2 vendored
inside a wheel's ``*.libs/`` folder is invisible to both. scripts/wheel_native_inventory.py
inventories bundled native libraries and matches them against scripts/native_advisories.json.

Covers:
  * a deterministic in-test vulnerable wheel (libxml2 2.9.10 in lxml-style ``*.libs/``, plus an
    embedded-version-string library and a patched libxml2) is built with zipfile, no network;
  * the REAL scanner CLI run over it exits 1 and names the bundled component and advisory;
  * a patched wheel and an unknown library produce no finding (no over-reporting);
  * an unreadable wheel fails closed (exit 2);
  * with the libxml2 advisories removed the same wheel's libxml2 is NOT flagged, proving the
    assertion depends on the coverage; an empty or malformed advisory file fails closed (exit 2);
  * no native library at all exits 2 (unless --allow-empty), and an advised library with no
    readable version is reported UNVERIFIED;
  * if Trivy is on PATH with a local DB, an offline run is attempted and its result is reported;
    otherwise an explicit SKIP marker is printed (Trivy cannot see vendored libs anyway: see
    scripts/DEPENDENCY_AUDIT.md);
  * the docs state what is and is not covered, and verify_image_scanning keeps its stub tests.

ASCII-only prints. Final line "NATIVE WHEEL SCAN OK".
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

init_env("loctms_native_wheel_")

SCANNER = _ROOT / "scripts" / "wheel_native_inventory.py"
ADVISORIES = _ROOT / "scripts" / "native_advisories.json"
AUDIT_DOC = _ROOT / "scripts" / "DEPENDENCY_AUDIT.md"
FIXED_TIME = (2026, 10, 6, 0, 0, 0)
VULN_WHEEL = "fakelxml-1.0-cp313-manylinux_2_28_x86_64.whl"


def _write(zf, name, data):
    zf.writestr(zipfile.ZipInfo(name, date_time=FIXED_TIME), data)


def _build_wheel(path, libs):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _write(zf, "fakelxml-1.0.dist-info/METADATA", "Name: fakelxml\nVersion: 1.0\n")
        _write(zf, "fakelxml/__init__.py", "")
        for name, payload in libs.items():
            _write(zf, name, payload)
    return path


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCANNER), *map(str, args)],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
        timeout=60,
    )


def vulnerable_wheel_flagged(tmp):
    wheel = _build_wheel(
        tmp / VULN_WHEEL,
        {
            "fakelxml.libs/libxml2-a1b2c3d4.so.2.9.10": b"\x7fELF" + b"\0" * 16,
            "fakelxml.libs/libxslt-9f8e7d6c.so.1.1.34": b"\x7fELF" + b"\0" * 16,
        },
    )
    proc = _run(wheel)
    out = proc.stdout
    assert proc.returncode == 1, (
        f"vulnerable wheel must exit 1, got {proc.returncode}: {out}{proc.stderr}"
    )
    assert "component libxml2 2.9.10 in fakelxml-1.0" in out, out
    assert "FINDING CVE-2021-3518 HIGH libxml2 2.9.10 (fixed in 2.9.11)" in out, out
    assert "FINDING CVE-2022-23308 HIGH libxml2 2.9.10" in out, out
    assert "FINDING CVE-2021-30560 HIGH libxslt 1.1.34" in out, out
    report = json.loads(_run("--json", wheel).stdout)
    assert {f["advisory"] for f in report["findings"]} >= {"CVE-2021-3518", "CVE-2021-30560"}
    print("  vulnerable wheel: real scanner exits 1, names libxml2/libxslt and the advisories")


def embedded_version_found(tmp):
    wheel = _build_wheel(
        tmp / "embedded-1.0-py3-none-any.whl",
        {"embedded.libs/libxml2-deadbeef.so": b"\x7fELF\0\0 libxml2 2.9.4 build\0\0"},
    )
    proc = _run(wheel)
    assert proc.returncode == 1 and "CVE-2021-3518" in proc.stdout, proc.stdout
    print("  embedded version string: version read from the binary when the file name has none")


def _findings(tmp, name, libs):
    wheel = _build_wheel(tmp / name, libs)
    proc = _run("--json", wheel)
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    return proc.returncode, report["components"], {f["advisory"] for f in report["findings"]}


def name_and_version_variants(tmp):
    """Detection must hold across platform naming, case and version edge cases."""
    elf = b"ELF"
    cases = {
        # numeric (not string) comparison: 2.9.9 < 2.9.11, and 2.10.0 is NOT below 2.9.13
        "v299-1.0-py3-none-any.whl": (
            {"a.libs/libxml2-a1b2c3d4.so.2.9.9": elf},
            {"CVE-2021-3518", "CVE-2022-23308", "CVE-2022-40303", "CVE-2022-40304"},
        ),
        "v2100-1.0-py3-none-any.whl": (
            {"a.libs/libxml2-a1b2c3d4.so.2.10.0": elf},
            {"CVE-2022-40303", "CVE-2022-40304"},
        ),
        # the fixed version itself is clean, one below is not
        "v2911-1.0-py3-none-any.whl": (
            {"a.libs/libxml2-a1b2c3d4.so.2.9.11": elf},
            {"CVE-2022-23308", "CVE-2022-40303", "CVE-2022-40304"},
        ),
        "v2103-1.0-py3-none-any.whl": ({"a.libs/libxml2-a1b2c3d4.so.2.10.3": elf}, set()),
        # macOS dylib, Windows dll, upper case, no hash, deep directories
        "mac-1.0-py3-none-any.whl": (
            {"a/.dylibs/libxml2.2.9.10.dylib": elf},
            {"CVE-2021-3518", "CVE-2022-23308", "CVE-2022-40303", "CVE-2022-40304"},
        ),
        "win-1.0-py3-none-any.whl": (
            {"a/libxml2-2.9.10.dll": elf},
            {"CVE-2021-3518", "CVE-2022-23308", "CVE-2022-40303", "CVE-2022-40304"},
        ),
        "upper-1.0-py3-none-any.whl": (
            {"a/LIBXML2-2.9.10.DLL": elf},
            {"CVE-2021-3518", "CVE-2022-23308", "CVE-2022-40303", "CVE-2022-40304"},
        ),
        "plain-1.0-py3-none-any.whl": ({"a/libxslt.so.1.1.30": elf}, {"CVE-2021-30560"}),
        # a bare soname names an ABI, not a release: unknown version, no false finding
        "soname-1.0-py3-none-any.whl": ({"a/libxml2.so.2": elf, "a/libxml2-2.dll": elf}, set()),
        # an embedded string belonging to a different library must not be attributed to this one
        "cross-1.0-py3-none-any.whl": ({"a/libother.so": b"libxml2 2.9.4"}, set()),
    }
    for name, (libs, expected) in cases.items():
        rc, components, found = _findings(tmp, name, libs)
        assert found == expected, (
            f"{name}: expected {sorted(expected)}, got {sorted(found)} ({components})"
        )
        assert rc == (1 if expected else 0), f"{name}: exit {rc}"
    _, comps, _ = _findings(tmp, "soname2-1.0-py3-none-any.whl", {"a/libxml2.so.2": elf})
    assert comps[0]["component"] == "libxml2" and comps[0]["version"] is None, comps
    print(
        "  naming variants: .so/.dylib/.dll, case, soname-only, numeric version compare, "
        "fixed-version boundary"
    )


def patched_and_unknown_clean(tmp):
    wheel = _build_wheel(
        tmp / "patched-1.0-py3-none-any.whl",
        {
            "patched.libs/libxml2-a1b2c3d4.so.2.13.4": b"\x7fELF",
            "patched.libs/libxslt-9f8e7d6c.so.1.1.42": b"\x7fELF",
            "patched.libs/libmystery-11223344.so.0.1": b"\x7fELF",
        },
    )
    proc = _run(wheel)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "3 components, 0 findings" in proc.stdout, proc.stdout
    print("  patched and unknown libraries: inventoried, no findings")


def unreadable_fails_closed(tmp):
    bad = tmp / "broken-1.0-py3-none-any.whl"
    bad.write_bytes(b"not a zip")
    assert _run(bad).returncode == 2, "an unreadable wheel must fail closed"
    assert _run(tmp / "missing.whl").returncode == 2
    print("  unreadable or missing wheel: exit 2 (fail closed)")


def coverage_removal_is_detected(tmp):
    """Removing libxml2 advisories un-flags libxml2, so the vulnerable-wheel assertion
    depends on the coverage; an empty or malformed advisory file fails closed instead of reporting
    a clean scan."""
    wheel = tmp / VULN_WHEEL
    data = json.loads(ADVISORIES.read_text(encoding="utf-8"))
    for lib in ("libxml2", "libxslt"):
        assert data.get(lib), f"advisory inventory lost its {lib} coverage"
        for entry in data[lib]:
            assert entry.get("source", "").startswith(
                "https://nvd.nist.gov/vuln/detail/" + entry["id"]
            ), entry
    xslt_only = tmp / "xslt-only-advisories.json"
    xslt_only.write_bytes(json.dumps({"libxslt": data["libxslt"]}).encode("utf-8"))
    proc = _run("--advisories", xslt_only, wheel)
    assert proc.returncode == 1 and "CVE-2021-30560" in proc.stdout, proc.stdout + proc.stderr
    assert "CVE-2021-3518" not in proc.stdout, proc.stdout
    bad_files = {
        "empty": "{}",
        "comment-only": json.dumps({"_comment": "x"}),
        "not-object": "[]",
        "malformed": "{not json",
        "no-source": json.dumps(
            {
                "libxml2": [
                    {
                        "id": "CVE-2021-3518",
                        "severity": "HIGH",
                        "affected_below": "2.9.11",
                        "summary": "s",
                    }
                ]
            }
        ),
        "no-version": json.dumps(
            {
                "libxml2": [
                    {
                        "id": "CVE-2021-3518",
                        "severity": "HIGH",
                        "affected_below": "none",
                        "summary": "s",
                        "source": "u",
                    }
                ]
            }
        ),
        "empty-list": json.dumps({"libxml2": []}),
    }
    for label, body in bad_files.items():
        path = tmp / f"adv-{label}.json"
        path.write_bytes(body.encode("utf-8"))
        proc = _run("--advisories", path, wheel)
        assert proc.returncode == 2, (
            f"{label} advisory file must fail closed, got {proc.returncode}: {proc.stdout}"
        )
        assert "NATIVE-INVENTORY ERROR" in proc.stderr, f"{label}: {proc.stderr}"
    print(
        "  advisory inventory covers libxml2 and libxslt with sources; "
        "empty/malformed data fails closed"
    )


def empty_inventory_is_loud(tmp):
    """A wheel set with no native library at all is an error unless explicitly allowed, and an
    advised library whose version cannot be read is reported as UNVERIFIED, not silently clean."""
    pure = _build_wheel(tmp / "pure-1.0-py3-none-any.whl", {})
    proc = _run(pure)
    assert proc.returncode == 2 and "no native libraries found" in proc.stderr, (
        proc.stdout + proc.stderr
    )
    proc = _run("--allow-empty", pure)
    assert proc.returncode == 0 and "0 components, 0 findings" in proc.stdout, (
        proc.stdout + proc.stderr
    )
    soname = _build_wheel(
        tmp / "unver-1.0-py3-none-any.whl", {"a/libxml2.so.2": b"ELF", "a/libzz.so.1": b"ELF"}
    )
    proc = _run(soname)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UNVERIFIED libxml2 has advisories but no readable version" in proc.stdout, proc.stdout
    assert "UNVERIFIED libzz" not in proc.stdout and "1 unverified" in proc.stdout, proc.stdout
    assert "NATIVE-INVENTORY WARNING" in proc.stderr, proc.stderr
    report = json.loads(_run("--json", soname).stdout)
    assert [c["component"] for c in report["unverified"]] == ["libxml2"], report
    print(
        "  empty inventory exits 2 unless --allow-empty; "
        "unknown-version advised library is UNVERIFIED"
    )


def trivy_optional(tmp):
    trivy = shutil.which("trivy")
    cache = os.environ.get("TRIVY_CACHE_DIR")
    if not trivy or not cache or not (Path(cache) / "db" / "trivy.db").exists():
        print(
            "  SKIP real-trivy: no trivy binary or local DB (set TRIVY_CACHE_DIR to run it); "
            "Trivy does not see vendored wheel libs, so the inventory scanner is the gate"
        )
        return
    proc = subprocess.run(
        [
            trivy,
            "fs",
            "--skip-db-update",
            "--offline-scan",
            "--quiet",
            "--format",
            "json",
            str(tmp / VULN_WHEEL),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(
        f"  real-trivy: ran offline, rc={proc.returncode} "
        "(informational: vendored libs are not Trivy scope)"
    )


def directory_scan_flags_installed_libs(tmp):
    """#620: the same inventory over an unpacked directory (what pregate scans via
    --installed); a missing directory fails closed."""
    tree = tmp / "site-packages"
    lib = tree / "fakelxml.libs"
    lib.mkdir(parents=True)
    (lib / "libxml2-a1b2c3d4.so.2.9.10").write_bytes(b"\x7fELF")
    (lib / "libxslt-9f8e7d6c.so.1.1.36").write_bytes(b"\x7fELF")  # patched: clean
    proc = _run(tree)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FINDING CVE-2021-3518 HIGH libxml2 2.9.10" in proc.stdout, proc.stdout
    assert "libxslt 1.1.36" in proc.stdout and "FINDING CVE-2021-30560" not in proc.stdout
    assert "NATIVE-INVENTORY: 2 components, " in proc.stdout.splitlines()[-1]
    missing = _run(tmp / "no-such-dir")
    assert missing.returncode == 2, (missing.returncode, missing.stdout, missing.stderr)
    # --installed scans the running interpreter's site-packages and prints the verdict line
    inst = _run("--installed", "--allow-empty")
    assert inst.returncode in (0, 1), inst.stdout[-300:] + inst.stderr[-300:]
    assert inst.stdout.strip().splitlines()[-1].startswith("NATIVE-INVENTORY: "), inst.stdout[-200:]
    nothing = _run()
    assert nothing.returncode == 2, "no wheels, no directories and no --installed is a usage error"
    print("  directory scan: installed-style tree flagged, missing dir exit 2, --installed works")


def ci_audit_script_and_workflow(tmp):
    """#620: scripts/native_wheel_audit.py resolves every dep-audit matrix profile to existing
    hash locks, classifies downloads, fails closed, and the workflow runs it per profile."""
    sys.path.insert(0, str(_ROOT / "scripts"))
    import native_wheel_audit as nwa
    import yaml
    from audit_runtime_profiles import aux_labels, runtime_profiles

    workflow_text = (_ROOT / ".github/workflows/dep-audit.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    job = workflow["jobs"]["pip-audit"]
    assert any(
        "native_wheel_audit.py --profile ${{ matrix.profile }}" in step.get("run", "")
        for step in job["steps"]
    ), "dep-audit must run the native wheel scan for every matrix profile"
    # every matrix profile maps to existing locks and hashed requirement lines
    for profile in job["strategy"]["matrix"]["profile"]:
        locks = nwa.profile_locks(_ROOT, profile)
        assert locks and all(p.is_file() for p in locks), profile
        assert nwa.lock_requirements(locks), profile
    assert set(job["strategy"]["matrix"]["profile"]) == runtime_profiles().keys() | set(
        aux_labels(_ROOT)
    )
    for bad in ("nope-profile", "BAD NAME", "../x"):
        try:
            nwa.profile_locks(_ROOT, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unknown profile {bad!r} must fail closed")
    unhashed = tmp / "unhashed.lock"
    unhashed.write_text("lxml==5.0.0\n", encoding="utf-8")
    try:
        nwa.lock_requirements([unhashed])
    except ValueError:
        pass
    else:
        raise AssertionError("a requirement without a hash must be refused")
    # main(): a vulnerable downloaded wheel exits 1, an sdist-only package is SKIPPED, a
    # download error exits 2, nothing downloaded exits 2
    (tmp / "audit-src").mkdir()
    wheel_src = _build_wheel(
        tmp / "audit-src" / VULN_WHEEL, {"fakelxml.libs/libxml2-a1b2c3d4.so.2.9.10": b"ELF"}
    )
    lines = nwa.lock_requirements(nwa.profile_locks(_ROOT, "base"))
    calls = {"n": 0}

    def fake_download(line, dest, timeout=600):
        calls["n"] += 1
        if calls["n"] == 1:
            shutil.copy(wheel_src, Path(dest) / wheel_src.name)
            return "ok"
        return "no-wheel" if calls["n"] == 2 else "ok"

    real = nwa.download
    nwa.download = fake_download
    try:
        assert nwa.main(["--profile", "base"]) == 1
        nwa.download = lambda line, dest, timeout=600: "error"
        assert nwa.main(["--profile", "base"]) == 2
        nwa.download = lambda line, dest, timeout=600: "no-wheel"
        assert nwa.main(["--profile", "base"]) == 2, "nothing scanned must not read as clean"
    finally:
        nwa.download = real
    assert nwa.main(["--profile", "not-a-profile"]) == 2
    assert len(lines) > 10
    # Review: pip prints "No matching distribution" for an unreachable index too, so only a
    # clean "no wheel" message may SKIP; any network trouble in the output is an error.
    real_run = nwa.subprocess.run

    def pip_says(stderr, code=1):
        return lambda *a, **k: subprocess.CompletedProcess(a[0], code, "", stderr)

    try:
        nwa.subprocess.run = pip_says("ERROR: No matching distribution found for sdistonly==1\n")
        assert nwa.download("sdistonly==1 --hash=sha256:" + "0" * 64, tmp) == "no-wheel"
        nwa.subprocess.run = pip_says(
            "WARNING: Retrying (Retry(total=4)) after connection broken by "
            "'NewConnectionError'\nERROR: No matching distribution found for lxml==5\n"
        )
        assert nwa.download("lxml==5 --hash=sha256:" + "0" * 64, tmp) == "error"
        nwa.subprocess.run = pip_says("ERROR: THESE PACKAGES DO NOT MATCH THE HASHES\n")
        assert nwa.download("lxml==5 --hash=sha256:" + "0" * 64, tmp) == "error"
        nwa.subprocess.run = pip_says("", 0)
        assert nwa.download("lxml==5 --hash=sha256:" + "0" * 64, tmp) == "ok"
    finally:
        nwa.subprocess.run = real_run
    crlf = tmp / "crlf.lock"
    crlf.write_bytes(
        b"a==1 ; sys_platform == 'win32' --hash=sha256:aa\r\nb==2 --hash=sha256:bb\r\n"
    )
    assert [x.split("==")[0] for x in nwa.lock_requirements([crlf])] == ["a", "b"]
    print(
        "  CI script: every matrix profile resolves to hash locks; vuln wheel exit 1, errors exit 2"
    )


def docs_state_coverage():
    text = AUDIT_DOC.read_text(encoding="utf-8")
    for token in (
        "wheel_native_inventory.py",
        "native_advisories.json",
        "Not covered",
        "verify_native_wheel_scan",
        "native_wheel_audit.py",
        "--installed",
        "pregate",
        "dep-audit",
    ):
        assert token in text, f"DEPENDENCY_AUDIT.md must mention {token!r}"
    assert "—" not in text
    stub = (_ROOT / "tests" / "verify_image_scanning.py").read_text(encoding="utf-8")
    assert "scan_behavior" in stub and "stubbed docker" in stub, "stub tests must stay"
    print("  docs: DEPENDENCY_AUDIT.md states covered and not covered scope; stub suite kept")


def main():
    failures = []
    tmp = Path(tempfile.mkdtemp(prefix="loctms_nws_"))
    try:
        for check in (
            vulnerable_wheel_flagged,
            embedded_version_found,
            name_and_version_variants,
            patched_and_unknown_clean,
            unreadable_fails_closed,
            coverage_removal_is_detected,
            empty_inventory_is_loud,
            directory_scan_flags_installed_libs,
            ci_audit_script_and_workflow,
            trivy_optional,
        ):
            try:
                check(tmp)
            except (AssertionError, ValueError, OSError, subprocess.SubprocessError) as exc:
                failures.append(f"{check.__name__}: {exc}")
        try:
            docs_state_coverage()
        except (AssertionError, OSError) as exc:
            failures.append(f"docs_state_coverage: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert not failures, "\n".join(failures)
    print("NATIVE WHEEL SCAN OK")


if __name__ == "__main__":
    main()
