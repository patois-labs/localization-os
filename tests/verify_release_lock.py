"""Release locks, installed METADATA constraints, and advisory expiry guards."""

import ast
import importlib.metadata as metadata
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_lock import (
    Requirement,
    active,
    canonicalize_name,
    closure_errors,
    compare,
    dev_install_errors,
    docker_install_errors,
    installed_errors,
    ledger_errors,
    locked,
    marker_errors,
    prerelease_errors,
    requirements,
    workflow_install_errors,
)


def _profiles():
    yield ROOT / "requirements.txt", [ROOT / "requirements.lock"]
    for path in sorted(ROOT.glob("requirements-*.txt")):
        if path.name != "requirements-dev.txt":
            yield path, [ROOT / "requirements.lock", path.with_suffix(".lock")]


def _runtime_locks():
    """Runtime/profile locks: the runtime closure only, the dev lock is verified on its own."""
    return [
        path
        for path in sorted(ROOT.glob("requirements*.lock"))
        if path.name != "requirements-dev.lock"
    ]


def test_docker_installs_lock():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert not docker_install_errors(docker)
    for suffix in (" && pip install requests", " && pip install --upgrade requests", " --upgrade"):
        assert docker_install_errors(
            docker.replace(
                "-r requirements-postgres.lock", "-r requirements-postgres.lock" + suffix
            )
        )


def test_direct_requirements():
    errors = []
    for path, locks in _profiles():
        assert all(p.exists() for p in locks), f"Missing profile lock: {path.name}"
        pins = locked(locks)
        for req in requirements(path):
            if active(req):
                version = pins.get(canonicalize_name(req.name))
                if version is None or not req.specifier.contains(version, prereleases=True):
                    errors.append(f"{path.name}: {req}, locked {version}")
    assert not errors, "\n".join(errors)


def test_installed_metadata():
    errors = []
    snapshots = json.loads((ROOT / ".github/locked-profile-metadata.json").read_text())
    for path, locks in _profiles():
        directs = list(dict.fromkeys([ROOT / "requirements.txt", path]))
        # Verify both supported platform closures even on the Windows host.
        for platform, system, os_name in (("win32", "Windows", "nt"), ("linux", "Linux", "posix")):
            environment = {"sys_platform": platform, "platform_system": system, "os_name": os_name}
            errors.extend(
                f"{path.name} ({platform}): {e}"
                for e in closure_errors(directs, locks, snapshots, environment)
            )
    # Only the named optional consumer can constrain shared pins from outside the lock.
    base = locked([ROOT / "requirements.lock"])
    try:
        consumer = metadata.distribution("presidio-analyzer")
    except metadata.PackageNotFoundError:
        print("Package not installed: presidio-analyzer; optional consumer check skipped")
    else:
        for raw in consumer.requires or []:
            req = Requirement(raw)
            version = base.get(canonicalize_name(req.name))
            if active(req) and version and not req.specifier.contains(version, prereleases=True):
                errors.append(f"presidio-analyzer requires {req}, locked {version}")
    assert not errors, "\n".join(sorted(set(errors)))


def test_installed_versions():
    exclude = [p for p in os.environ.get("RELEASE_LOCK_EXCLUDE_PATHS", "").split(os.pathsep) if p]
    paths = _runtime_locks()
    errors = installed_errors(paths, exclude_paths=exclude)
    profile = os.environ.get("RELEASE_LOCK_PROFILE")
    if profile:
        image_paths = [ROOT / "requirements.lock", ROOT / "requirements-postgres.lock"]
        if profile != "postgres":
            image_paths.append(ROOT / f"requirements-{profile}.lock")
        errors += installed_errors(image_paths, require_installed=True, exclude_paths=exclude)
    assert not errors, "\n".join(errors)


def test_installed_versions_tool_shadow():
    """#404 (deps-02): a validation-tool mount first on sys.path must not shadow the
    measured runtime inventory. Exercises the real installed_errors against a
    conflicting on-disk tool distribution, with no metadata mock."""
    locked_version = metadata.version("packaging")
    with tempfile.TemporaryDirectory() as tmp:
        tool = Path(tmp) / "checks"
        dist = tool / "packaging-99.99.99.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: packaging\nVersion: 99.99.99\n"
        )
        lock = Path(tmp) / "requirements.lock"
        lock.write_text(f"packaging=={locked_version}\n")
        # PYTHONPATH=/checks puts the tool dir ahead of site-packages, as CI does.
        # POSIX keeps such entries verbatim in sys.path, trailing separator included; a
        # clean-path exclusion must still match the mount it names.
        shadow_entry = str(tool) + os.sep
        sys.path.insert(0, shadow_entry)
        try:
            # Without exclusion the tool dist poisons the read: 99.99.99 != the lock.
            shadowed = installed_errors([lock])
            assert shadowed == [f"packaging: installed 99.99.99, locked {locked_version}"], (
                f"expected the tool shadow to trip the guard, got {shadowed}"
            )
            # Excluding the tool mount measures the image runtime, not the tools.
            clean = installed_errors([lock], exclude_paths=[str(tool)])
            assert not clean, f"exclude_paths must remove the shadow, got {clean}"
        finally:
            sys.path.remove(shadow_entry)


def test_no_prerelease_pins():
    assert not (errors := prerelease_errors(sorted(ROOT.glob("requirements*.lock")))), "\n".join(
        errors
    )


def test_static_profile_locks():
    paths = _runtime_locks()
    assert not (errors := marker_errors(paths)), "\n".join(errors)
    snapshots = json.loads((ROOT / ".github/locked-profile-metadata.json").read_text())
    real_requires = metadata.requires

    def without_optional(name):
        if name in snapshots:
            raise metadata.PackageNotFoundError(name)
        return real_requires(name)

    errors = []
    with patch("scripts.release_lock.metadata.requires", side_effect=without_optional):
        for path, locks in _profiles():
            if path.name != "requirements.txt":
                errors.extend(closure_errors([ROOT / "requirements.txt", path], locks, snapshots))
    assert not errors, "\n".join(errors)


def test_profile_lock_byte_identity():
    for path in sorted(ROOT.glob("requirements-*.lock")):
        content = path.read_bytes()
        lines = content.decode("ascii").splitlines()
        header = []
        for line in lines:
            if not line.startswith("#"):
                break
            header.append(line)
        assert header, f"{path.name}: missing leading provenance header"
        pins = lines[len(header) :]
        assert pins and all(line and not line.startswith("#") for line in pins), (
            f"{path.name}: expected only pins after the leading header"
        )
        # A pin may carry an inline "# prerelease-accepted: ..." note (release_lock parses it).
        names = [req.name for req in requirements(path)]
        assert names == sorted(names, key=str.lower), f"{path.name}: pins not in name.lower() order"
        regenerated = ("\n".join(header + pins) + "\n").encode("ascii")
        assert content == regenerated, f"{path.name}: expected byte-identical LF output (0 CR)"


def test_profile_collision_guard():
    import yaml

    from scripts.audit_runtime_profiles import runtime_profiles

    profiles = runtime_profiles()
    workflow = yaml.safe_load((ROOT / ".github/workflows/dep-audit.yml").read_text())
    strategy = workflow["jobs"]["pip-audit"]["strategy"]
    assert strategy["fail-fast"] is True
    from scripts.audit_runtime_profiles import aux_labels

    assert set(strategy["matrix"]["profile"]) == profiles.keys() | set(aux_labels(ROOT))
    # A scripts/locks inventory must never share a name with a runtime profile (one would
    # replace the other in local_audit's inventory map).
    assert not set(aux_labels(ROOT)) & profiles.keys()
    with patch("scripts.audit_runtime_profiles.json.loads", return_value={"base": ["unlocked"]}):
        try:
            runtime_profiles()
        except ValueError as exc:
            assert "base" in str(exc)
        else:
            raise AssertionError("Accepted optional/locked profile collision")
    # Gate L1: "locked" is the combined inventory key; that profile name must be refused.
    with patch("scripts.audit_runtime_profiles.json.loads", return_value={"locked": ["boto3"]}):
        try:
            runtime_profiles()
        except ValueError as exc:
            assert "locked" in str(exc)
        else:
            raise AssertionError("Accepted an optional profile named 'locked'")


def test_vendor_studio_lock_matches_root():
    """Gate L4: every Vendor Studio closure pin equals the root lock's pin."""
    from scripts.audit_runtime_profiles import file_pins

    root = dict(p.split("==") for p in file_pins([ROOT / "requirements.lock"]))
    vendor = file_pins([ROOT / "tools/vendor_studio/requirements.lock"])
    drift = []
    for pin in vendor:
        name, version = pin.split("==")
        if name in root and root[name] != version:
            drift.append(pin)
    assert not drift, f"Vendor Studio lock drifts from requirements.lock: {drift}"


def test_lock_regressions():
    with tempfile.TemporaryDirectory() as tmp:
        direct, lock = Path(tmp) / "requirements.txt", Path(tmp) / "requirements.lock"
        direct.write_text("server[standard]==1\n")
        lock.write_text('server==1\nparser==2\nloop==3; sys_platform != "win32"\n')
        requires = {
            "server": [
                'parser>=2; extra == "standard"',
                'loop>=3; extra == "standard" and sys_platform != "win32"',
            ],
            "parser": [],
            "loop": [],
        }
        with patch(
            "scripts.release_lock.metadata.requires", side_effect=lambda name: requires[name]
        ):
            assert not closure_errors([direct], [lock])
            direct.write_text("server==1\n")
            assert "ORPHAN-PIN: parser" in closure_errors([direct], [lock])
            direct.write_text("server[standard]==1\n")
            lock.write_text("server==1\n")
            assert any("parser" in e for e in closure_errors([direct], [lock]))
        lock.write_text("server==1\n")
        for version in ("0.9", "1.1"):
            with patch("scripts.release_lock.metadata.version", return_value=version):
                assert installed_errors([lock]) == [f"server: installed {version}, locked 1"]
        with patch(
            "scripts.release_lock.metadata.version", side_effect=metadata.PackageNotFoundError
        ):
            assert not installed_errors([lock])
            assert installed_errors([lock], require_installed=True)
        for version in ("1.0b1", "1.0rc1", "1.0.dev1"):
            lock.write_text(f"server=={version}\n")
            assert prerelease_errors([lock])
            lock.write_text(f"server=={version} # prerelease-accepted: owner review\n")
            assert not prerelease_errors([lock])


def test_cli_optimization():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger"
        ledger.write_text("ID # expired\n")
        result = subprocess.run(
            [sys.executable, "-O", str(ROOT / "scripts/release_lock.py"), "--ledger", str(ledger)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONOPTIMIZE": "1"},
        )
        assert result.returncode != 0 and "missing owner" in result.stderr


def test_marker_and_snapshot_regressions():
    with tempfile.TemporaryDirectory() as tmp:
        direct, lock = Path(tmp) / "direct", Path(tmp) / "lock"
        lock.write_text("pywin32==312\n")
        assert marker_errors([lock])
        lock.write_text('unrelated==1; sys_platform != "win32"\n')
        assert marker_errors([lock])
        lock.write_text('unrelated==1; sys_platform != "win32" # platform: documented exception\n')
        assert not marker_errors([lock])
        direct.write_text("optional==1\n")
        lock.write_text("optional==1\n")
        with patch(
            "scripts.release_lock.metadata.requires", side_effect=metadata.PackageNotFoundError
        ):
            assert not closure_errors(
                [direct], [lock], {"optional": {"version": "1", "requires": []}}
            )
            assert any(
                "Stale profile metadata: optional" in e
                for e in closure_errors(
                    [direct], [lock], {"optional": {"version": "2", "requires": []}}
                )
            )


def test_advisory_ledger():
    content = (ROOT / ".github/pip-audit-ignores.txt").read_bytes().decode("utf-8")
    assert not (errors := ledger_errors(content, date.today())), "\n".join(errors)


def test_advisory_notes():
    content = (ROOT / ".github/pip-audit-ignores.txt").read_text(encoding="utf-8")
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        advisory, _, note = line.partition("#")
        advisory = advisory.strip()
        assert re.search(r"\breachability=(yes|no|unknown)\b", note), (
            f"{advisory}: missing reachability= verdict in note"
        )
        assert re.search(r"\bfixed=\d+(?:\.\d+)+(?![\w.])", note), (
            f"{advisory}: missing fixed= version in note"
        )


def test_ledger_has_no_stale_entries():
    pins = locked([ROOT / "requirements.lock"])
    content = (ROOT / ".github/pip-audit-ignores.txt").read_text(encoding="utf-8")
    package = None
    errors = []
    for raw in content.splitlines():
        line = raw.strip()
        header = re.match(r"^# ([A-Za-z0-9][A-Za-z0-9._-]*) ([0-9][^\s]*)", line)
        if header:
            package = canonicalize_name(header[1])
        if not line.startswith(("PYSEC-", "GHSA-", "CVE-")):
            continue
        advisory = line.split()[0]
        assert package in pins, f"{advisory}: missing locked package cluster header"
        fixed = re.search(r"\bfixed=([^\s;]+)", line)
        assert fixed, f"{advisory}: missing fixed= version"
        if Version(pins[package]) >= Version(fixed[1]):
            errors.append(
                f"{advisory}: stale entry for {package}, locked {pins[package]} >= fixed {fixed[1]}"
            )
    assert not errors, "\n".join(errors)


def test_ledger_boundaries():
    today = date(2026, 9, 7)
    assert not ledger_errors("ID # owner=maintainer expires=2026-09-07 reason=review", today)
    for note in (
        "owner=maintainer expires=2026-09-06 reason=review",
        "expires=2026-10-01 reason=review",
        "owner=maintainer reason=review",
        "owner=maintainer expires=2026-02-30 reason=review",
    ):
        assert ledger_errors(f"ID # {note}", today), note


def test_image_comparison():
    with tempfile.TemporaryDirectory() as tmp:
        lock, freeze = Path(tmp) / "lock", Path(tmp) / "freeze"
        lock.write_text('Some_Package==1.2\nAbsent==1 ; sys_platform == "never"\n')
        freeze.write_text("some-package==1.2\n")
        compare(freeze, [lock])
        for content in ("", "some-package==1.3\n", "some-package==1.2\nextra==1\n"):
            freeze.write_text(content)
            try:
                compare(freeze, [lock])
            except ValueError:
                pass
            else:
                raise AssertionError(f"Accepted image drift: {content!r}")


def test_ci_release_guards():
    import yaml

    # BaseLoader preserves the GitHub Actions "on" key (YAML 1.1 treats it as True).
    ci = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    assert ci["on"] == {
        "push": {"branches": ["main"], "tags": ["*"]},
        "workflow_dispatch": {},
        "pull_request": {"branches": ["main"]},
        "schedule": [{"cron": "17 3 * * *"}],
    }, "CI workflow triggers changed"
    image = ci["jobs"]["release-image"]

    # Parse the complete condition, preserving operator precedence and string literals.
    # Compare syntax trees without evaluating workflow-controlled code.
    def condition_tree(expression):
        expression = expression.strip()
        if expression.startswith("${{") and expression.endswith("}}"):
            expression = expression[3:-2].strip()
        expression = expression.replace("&&", " and ").replace("||", " or ")
        return ast.dump(ast.parse(expression, mode="eval"))

    expected = (
        "(github.event_name == 'push' && (github.ref == 'refs/heads/main' || "
        "startsWith(github.ref, 'refs/tags/'))) || github.event_name == 'workflow_dispatch'"
    )
    assert condition_tree(image["if"]) == condition_tree(expected), (
        "release-image condition must allow only main/tag pushes or manual dispatch"
    )
    commands = "\n".join(step.get("run", "") for step in image["steps"])
    assert "-m pip check" in commands and "import app.main" in commands
    assert "-m pip freeze > image-freeze.txt" in commands
    assert "scripts/release_lock.py --verify --profile postgres --require-installed" in commands
    # #404: the validation tool dir (PYTHONPATH=/checks) must not shadow the image's
    # locked packaging. Pin the tool to the locked version and exclude /checks from the
    # installed-version read.
    # #412: the tools come from a hash-pinned lock, so the pin lives in that lock.
    tools_lock = "scripts/locks/requirements-ci-check-tools.lock"
    assert re.search(
        r"pip install --require-hashes --no-deps --target \.ci-check-tools -r "
        + re.escape(tools_lock),
        commands,
    ), "isolated validation tools must install from the hashed tools lock"
    tool_pin = locked([ROOT / tools_lock]).get("packaging")
    assert tool_pin, "isolated validation tools must pin packaging"
    assert tool_pin == locked([ROOT / "requirements.lock"])["packaging"], (
        f"validation packaging {tool_pin} must equal requirements.lock (else it shadows it)"
    )
    assert "--require-installed --exclude-path /checks" in commands, (
        "the verify command must exclude the tool mount from the installed-version lookup"
    )
    assert "RELEASE_LOCK_EXCLUDE_PATHS=/checks" in commands, (
        "the in-image verify suite must exclude the tool mount too"
    )
    assert not any(
        "docker build" in step.get("run", "") for step in ci["jobs"]["default-install"]["steps"]
    )
    audit = (ROOT / ".github/workflows/dep-audit.yml").read_text(encoding="utf-8")
    audit_on = yaml.load(audit, Loader=yaml.BaseLoader)["on"]
    assert set(audit_on) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    assert audit_on["schedule"] == [{"cron": "17 6 * * 1"}]
    for event in ("push", "pull_request"):
        assert audit_on[event]["branches"] == ["main"]
        assert {"requirements*.lock", "requirements*.txt"} <= set(audit_on[event]["paths"])
    assert "scripts/release_lock.py --ledger .github/pip-audit-ignores.txt" in audit
    assert "scripts/audit_runtime_profiles.py" in audit
    profiles = json.loads((ROOT / ".github/runtime-profiles.json").read_text())
    assert {"pii", "ocr", "sqlcipher"} <= profiles.keys()
    assert "-r requirements-saml.txt" in (ROOT / "requirements-dev.txt").read_text()


def test_local_audit_modes():
    """The pregate's offline-safe audit: same ledger, same profiles, WARN when offline."""
    from scripts import audit_runtime_profiles as audit

    # The Python ledger parser is the CI awk parser's twin (field 1, # lines skipped).
    ledger = "# header\n\nPYSEC-1  # owner=a expires=2999-01-01 reason=r\n  GHSA-2 x\n#GHSA-3\n"
    assert audit.ledger_ids(ledger) == ["PYSEC-1", "GHSA-2"]
    real = (ROOT / ".github/pip-audit-ignores.txt").read_bytes().decode("utf-8")
    ids = [line.split()[0] for line in real.splitlines() if line.split()]
    assert audit.ledger_ids(real) == [i for i in ids if not i.startswith("#")]
    pins = audit.locked_pins(ROOT)
    assert any(pin.startswith("uvloop==") for pin in pins), "Linux-only pins must be audited"
    assert any(pin.startswith("pywin32==") for pin in pins), "Windows-only pins must be audited"
    assert any(pin.startswith("python3-saml==") for pin in pins), "profile locks must be audited"

    def fake(verdicts, resolution="ok"):
        calls = []

        def quiet(argv, timeout, cwd):
            calls.append(argv)
            # Never run a tool from the audited checkout: safe path, evidence dir as cwd.
            assert argv[:3] == [sys.executable, "-P", "-m"], argv
            assert Path(cwd) == output, cwd
            assert "pip" not in argv and "--disable-pip" in argv, argv
            report = Path(argv[argv.index("--output") + 1])
            label = report.stem
            verdict = verdicts.get(label, "clean")
            if verdict == "offline":
                return subprocess.CompletedProcess(argv, 1, "", "requests ConnectionError")
            # pip-audit can repeat an advisory; the summary must list it once.
            vulns = [{"id": "GHSA-test", "fix_versions": ["9.9"]}] * 2 if verdict == "vuln" else []
            pins = Path(argv[argv.index("-r") + 1]).read_text().split()
            deps = [
                {"name": pin.split("==")[0], "version": pin.split("==")[1], "vulns": []}
                for pin in pins
            ]
            if verdict == "short":  # the gate's shadow pip_audit: one foreign, clean entry
                deps = [{"name": "x", "version": "1", "vulns": []}]
            if verdict == "renamed":  # right count, wrong packages
                deps = [{**dep, "name": dep["name"] + "-other"} for dep in deps]
            deps[0]["vulns"] = vulns
            report.write_text(json.dumps({"dependencies": deps}))
            return subprocess.CompletedProcess(argv, 1 if vulns else 0, "", "")

        return quiet, calls

    with tempfile.TemporaryDirectory() as tmp:
        root, output = Path(tmp) / "root", Path(tmp) / "out"
        (root / ".github").mkdir(parents=True)
        output.mkdir()
        (root / "requirements.lock").write_text('pkg==1.0\nuvloop==1 ; sys_platform != "win32"\n')
        (root / "requirements-ldap.txt").write_text("ldap3\n")
        (root / "requirements-ldap.lock").write_text("ldap3==2\n")
        (root / "requirements-s3.lock").write_bytes(b"boto3==1.0\n")
        vendor = root / "tools/vendor_studio"
        vendor.mkdir(parents=True)
        (vendor / "requirements.txt").write_bytes(b"vendor==1\n")
        (vendor / "requirements.lock").write_bytes(b"vendor==1\n")
        (root / ".github/runtime-profiles.json").write_text('{"s3": ["boto3"]}')
        ledger_path = root / ".github/pip-audit-ignores.txt"
        ledger_path.write_bytes(b"GHSA-keep  # owner=maintainer expires=2999-01-01 reason=r\n")
        for verdicts, resolution, expected in [
            ({}, "ok", "PASS"),
            ({"locked": "vuln"}, "ok", "FAIL"),
            ({"s3": "vuln"}, "ok", "FAIL"),
            ({"locked": "offline"}, "ok", "WARN"),
            ({"s3": "offline"}, "ok", "WARN"),
            ({"locked": "vuln", "s3": "offline"}, "ok", "FAIL"),
            ({"locked": "short"}, "ok", "FAIL"),
            ({"s3": "short"}, "ok", "FAIL"),
            ({"locked": "renamed"}, "ok", "FAIL"),
        ]:
            quiet, calls = fake(verdicts, resolution)
            with patch.object(audit, "_quiet", quiet):
                status, lines = audit.local_audit(root, output)
            assert status == expected, (verdicts, resolution, status, lines)
            assert len(lines) == len(set(lines)), f"advisory listed twice: {lines}"
            if "short" in verdicts.values() or "renamed" in verdicts.values():
                assert any("pins audited" in line for line in lines), lines
            audits = [argv for argv in calls if "pip_audit" in argv]
            assert all(argv[argv.index("--ignore-vuln") + 1] == "GHSA-keep" for argv in audits)
            assert all("--strict" in argv and "--no-deps" in argv for argv in audits)
            if expected == "FAIL" and "vuln" in verdicts.values():
                assert any("GHSA-test" in line and "9.9" in line for line in lines), lines
        locked_file = (output / "locked-pins.txt").read_text()
        assert "uvloop==1" in locked_file and "ldap3==2" in locked_file
        assert "vendor==1" in locked_file
        assert all("pip" not in argv for argv in calls)
        # An expired or ownerless ignore fails before any network call.
        for entry in (
            b"GHSA-old  # owner=maintainer expires=2000-01-01 reason=r\n",
            b"GHSA-anon  # expires=2999-01-01 reason=r\n",
        ):
            ledger_path.write_bytes(entry)
            quiet, calls = fake({})
            with patch.object(audit, "_quiet", quiet):
                status, lines = audit.local_audit(root, output)
            assert status == "FAIL" and not calls and lines[0].startswith("ledger: "), lines
        # A lock line that is not one exact == pin fails before any audit runs.
        ledger_path.write_bytes(b"")
        for bad in ("pkg>=1.0\n", "pkg==1.0,!=1.1\n", "pkg==1.*\n"):
            (root / "requirements.lock").write_text(bad)
            quiet, calls = fake({})
            with patch.object(audit, "_quiet", quiet):
                status, lines = audit.local_audit(root, output)
            assert status == "FAIL" and not calls and "not a single == pin" in lines[0], lines


def test_saml_lxml_fallback_is_audited_and_non_blocking():
    """Issue #407: the SAML lxml fallback lock is audited, its recorded advisory is a
    non-blocking WARNING until the exception expires, and anything else still fails."""
    from scripts import audit_runtime_profiles as audit

    assert audit.FALLBACK_LABEL in audit.fallback_inventories(ROOT)
    assert audit.fallback_inventories(ROOT)[audit.FALLBACK_LABEL] == ["lxml==6.0.0"]
    # Owner decision (ADR 0054): exactly this advisory, exactly this expiry. Adding an id or
    # moving the date must be a reviewed edit here too.
    assert audit.FALLBACK_ACCEPTED == {"PYSEC-2026-87": "2027-04-06"}

    def quiet_with(vulns):
        def quiet(argv, timeout, cwd):
            report = Path(argv[argv.index("--output") + 1])
            pins = Path(argv[argv.index("-r") + 1]).read_text().split()
            deps = [
                {"name": p.split("==")[0], "version": p.split("==")[1], "vulns": []} for p in pins
            ]
            deps[0]["vulns"] = vulns
            report.write_text(json.dumps({"dependencies": deps}))
            return subprocess.CompletedProcess(argv, 1 if vulns else 0, "", "")

        return quiet

    known = [{"id": "PYSEC-2026-87", "fix_versions": ["6.1.0"]}]
    other = [{"id": "GHSA-new", "fix_versions": ["9"]}]
    pins = ["lxml==6.0.0"]
    accepted = {"PYSEC-2026-87": "2999-01-01"}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        with patch.object(audit, "_quiet", quiet_with(known)):
            status, lines = audit.audit_pins("fb", pins, [], out, 5, accepted=accepted)
            assert status == "PASS", lines
            assert any("WARNING" in ln and "PYSEC-2026-87" in ln and "6.1.0" in ln for ln in lines)
            # No exception on file: the same finding blocks.
            assert audit.audit_pins("fb", pins, [], out, 5)[0] == "FAIL"
            # An expired exception blocks again (fail closed).
            expired = {"PYSEC-2026-87": "2000-01-01"}
            assert audit.audit_pins("fb", pins, [], out, 5, accepted=expired)[0] == "FAIL"
        with patch.object(audit, "_quiet", quiet_with(known + other)):
            status, lines = audit.audit_pins("fb", pins, [], out, 5, accepted=accepted)
            assert status == "FAIL" and any("GHSA-new" in ln for ln in lines), lines
        with patch.object(audit, "_quiet", quiet_with(other)):
            assert audit.audit_pins("fb", pins, [], out, 5, accepted=accepted)[0] == "FAIL"

        # An accepted advisory plus a tool failure (exit 2, not 1) is not a pass.
        def crashed(argv, timeout, cwd):
            quiet_with(known)(argv, timeout, cwd)
            return subprocess.CompletedProcess(argv, 2, "", "")

        with patch.object(audit, "_quiet", crashed):
            assert audit.audit_pins("fb", pins, [], out, 5, accepted=accepted)[0] == "FAIL"
    # The default local audit includes the fallback inventory, and it does not fail the gate.
    with tempfile.TemporaryDirectory() as tmp:
        seen = []

        def spy(argv, timeout, cwd):
            seen.append(Path(argv[argv.index("-r") + 1]).name)
            return quiet_with(known if "saml-lxml" in seen[-1] else [])(argv, timeout, cwd)

        with patch.object(audit, "_quiet", spy):
            status, lines = audit.local_audit(ROOT, Path(tmp), include_optional=False)
        assert any("saml-lxml" in n for n in seen), seen
        assert status == "PASS", lines
        assert any("WARNING" in ln and "lxml" in ln for ln in lines), lines


def _repo_lock_files():
    """Every *.lock in the checkout (repo-relative, posix). Git lists the tracked ones; CI runs
    this suite inside the shipped image (python:3.13-slim, no git, read-only /repo), so walk
    the tree there, skipping dot directories (.git, .venv, .ci-check-tools) and node_modules."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "*.lock"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        return result.stdout.split()
    except (OSError, subprocess.CalledProcessError):
        found = []
        for current, dirs, files in os.walk(ROOT):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".") and d not in {"node_modules", "__pycache__"}
            ]
            for name in files:
                if name.endswith(".lock"):
                    found.append((Path(current) / name).relative_to(ROOT).as_posix())
        return sorted(found)


def test_every_install_lock_is_audited_and_triggers_ci():
    """GitHub #558: no tracked lock may sit outside the audit inventory, the audit and
    hash_locks share one list, and a change confined to scripts/locks triggers dep-audit."""
    import yaml

    from scripts import audit_runtime_profiles as audit
    from scripts import hash_locks
    from scripts.lock_inventory import install_lock_paths

    tracked = _repo_lock_files()
    assert "scripts/locks/requirements-saml-lxml.lock" in tracked, tracked
    audited = {p.relative_to(ROOT).as_posix() for p in audit.audited_lock_paths(ROOT)}
    missing = sorted(t for t in tracked if t != "requirements-dev.lock" and t not in audited)
    assert not missing, f"locks absent from the audit inventory: {missing}"
    pins = {pin for inv in audit.aux_inventories(ROOT).values() for pin in inv}
    assert {"lxml==6.0.0", "setuptools==84.0.0"} <= pins, pins
    # One shared list: hash_locks and the audit derive from the same function.
    assert set(hash_locks.lock_paths(ROOT)) == set(install_lock_paths(ROOT))
    assert set(hash_locks.lock_paths(ROOT)) - {ROOT / "requirements-dev.lock"} == {
        ROOT / p for p in audited
    }
    # CI: both trigger lists name the auxiliary locks and the inventory tooling.
    workflow = yaml.safe_load((ROOT / ".github/workflows/dep-audit.yml").read_text())
    triggers = workflow.get("on") or workflow[True]
    for event in ("push", "pull_request"):
        paths = triggers[event]["paths"]
        for needed in ("scripts/locks/**", "scripts/hash_locks.py", "scripts/lock_inventory.py"):
            assert needed in paths, (event, needed)
    # A new lock dropped into scripts/locks is audited with no code change.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts/locks").mkdir(parents=True)
        (root / "scripts/locks/requirements-newtool.lock").write_text("newtool==1.0" + chr(10))
        assert audit.aux_inventories(root) == {"aux-newtool": ["newtool==1.0"]}


def test_shadow_pip_audit_refused():
    """Gate LOW 1: a branch-shipped `pip_audit` package must never produce the verdict.

    Reproduces the gate's shadow probe without requesting advisories. An import-origin
    subprocess checks safe-path isolation; fake reports check the verdict logic."""
    from scripts import audit_runtime_profiles as audit

    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp)
        marker = shadow / "shadow-ran.txt"
        (shadow / "pip_audit").mkdir()
        (shadow / "pip_audit/__init__.py").write_bytes(b"")
        (shadow / "pip_audit/__main__.py").write_bytes(
            (
                "import json, sys\n"
                f"open({str(marker)!r}, 'w').write('ran')\n"
                "out = sys.argv[sys.argv.index('--output') + 1]\n"
                "deps = [{'name': 'x', 'version': '1', 'vulns': []}]\n"
                "open(out, 'w').write(json.dumps({'dependencies': deps}))\n"
            ).encode("ascii")
        )
        # Import origin only: prove isolation without contacting any advisory service.
        result = audit._quiet(
            [sys.executable, "-P", "-c", "import pip_audit; print(pip_audit.__file__)"],
            30,
            shadow,
        )
        # The real tool imports from its install, or (in the shipped image, which carries no
        # pip_audit) the import fails: either way the shadow copy in cwd is never imported.
        absent = result.returncode != 0 and "No module named 'pip_audit'" in result.stderr
        assert (result.returncode == 0 or absent) and str(shadow) not in result.stdout, result
        assert not marker.exists(), "the shadow pip_audit package ran"
        # And the verdict logic alone refuses the shadow's report (count and names).
        report = json.dumps({"dependencies": [{"name": "x", "version": "1", "vulns": []}]})

        def shadow_quiet(argv, timeout, cwd):
            Path(argv[argv.index("--output") + 1]).write_text(report)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(audit, "_quiet", shadow_quiet):
            for pins in (["jinja2==3.1.4"], ["jinja2==3.1.4", "pkg==1.0"]):
                status, lines = audit.audit_pins("locked", pins, [], shadow, 5)
                assert status == "FAIL" and "pins audited" in lines[0], (pins, status, lines)


def test_workflow_permissions():
    """#298: every workflow explicitly limits its job tokens."""
    import yaml

    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        assert data.get("permissions") == {"contents": "read"}, workflow
        assert all(not job.get("permissions") for job in data["jobs"].values())


def test_npm_supply_chain():
    """#299: exact direct pins, audit jobs and update coverage for every integration."""
    import yaml

    updates = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]
    watched = {(u["package-ecosystem"], u["directory"]) for u in updates}
    assert ("pip", "/tools/vendor_studio") in watched
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"]["npm-audit"]
    commands = "\n".join(s.get("run", "") for s in job["steps"])
    assert "npm audit --package-lock-only --ignore-scripts" in commands
    assert "test -f package-lock.json" in commands
    # Only the Jira Forge app may relax the audit level, and never below high.
    relaxed = re.findall(r"matrix\.directory == '([^']+)' && '--audit-level=(\w+)'", commands)
    assert relaxed == [("integrations/jira-forge-app", "high")], relaxed
    assert commands.count("--audit-level") == 1, commands
    assert "continue-on-error" not in str(job)
    for directory in (ROOT / "integrations").glob("*/package.json"):
        relative = directory.parent.relative_to(ROOT).as_posix()
        assert ("npm", "/" + relative) in watched
        assert relative in job["strategy"]["matrix"]["directory"]
        package = json.loads(directory.read_text())
        assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in package["dependencies"].values())
        doc = directory.with_name("README.md").read_text(encoding="utf-8")
        assert "npm ci --ignore-scripts" in doc
        assert "npm install --package-lock-only --ignore-scripts" in doc


def test_vendor_supply_chain():
    """#295: Vendor Studio shares reviewed pins and has an audited image closure."""
    from scripts import audit_runtime_profiles as audit

    vendor = ROOT / "tools/vendor_studio"
    base = locked([ROOT / "requirements.lock"])
    for req in requirements(vendor / "requirements.txt"):
        assert req.specifier.contains(base[canonicalize_name(req.name)])
    for platform, system, os_name in (("win32", "Windows", "nt"), ("linux", "Linux", "posix")):
        errors = closure_errors(
            [vendor / "requirements.txt"],
            [vendor / "requirements.lock"],
            environment={"sys_platform": platform, "platform_system": system, "os_name": os_name},
        )
        assert not errors, errors
    docker = (vendor / "Dockerfile").read_text()
    assert "--no-deps --only-binary=:all: -r requirements.lock" in docker
    assert "vendor-studio" in audit.runtime_profiles()
    assert "starlette==1.7.0" in audit.locked_pins(ROOT)


def test_vendor_audit_automation():
    """#295: both dependency inputs and the image recipe trigger the vendor scan."""
    import fnmatch

    import yaml

    workflow = yaml.load(
        (ROOT / ".github/workflows/dep-audit.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert "vendor-studio" in workflow["jobs"]["pip-audit"]["strategy"]["matrix"]["profile"]
    for event in ("push", "pull_request"):
        for path in (
            "tools/vendor_studio/requirements.txt",
            "tools/vendor_studio/requirements.lock",
            "tools/vendor_studio/Dockerfile",
        ):
            assert any(fnmatch.fnmatchcase(path, p) for p in workflow["on"][event]["paths"]), (
                f"{event}: Vendor Studio audit does not cover {path}"
            )
    dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    assert any(
        entry["package-ecosystem"] == "pip"
        and entry["directory"] == "/tools/vendor_studio"
        and entry["schedule"]["interval"] == "weekly"
        for entry in dependabot["updates"]
    )


def test_vendor_transitive_audit():
    """Unique vendor pins must reach both local and CI scanners, without resolution."""
    from scripts import audit_runtime_profiles as audit

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".github").mkdir()
        (root / ".github/runtime-profiles.json").write_bytes(b"{}")
        (root / ".github/pip-audit-ignores.txt").write_bytes(b"")
        (root / "requirements.lock").write_bytes(b"base==1\n")
        vendor = root / "tools/vendor_studio"
        vendor.mkdir(parents=True)
        direct, lock = vendor / "requirements.txt", vendor / "requirements.lock"
        direct.write_bytes(b"vendor==1\n")
        closure = b"vendor==1\nvendor-transitive==2\n"
        lock.write_bytes(closure)

        def scanner(argv, timeout, cwd):
            assert "--no-deps" in argv and "--disable-pip" in argv
            pins = Path(argv[argv.index("-r") + 1]).read_text().splitlines()
            assert "vendor==1" in pins and "vendor-transitive==2" in pins
            deps = [
                {
                    "name": pin.split("==")[0],
                    "version": pin.split("==")[1],
                    "vulns": [{"id": "GHSA-vendor-test"}]
                    if pin.startswith("vendor-transitive==")
                    else [],
                }
                for pin in pins
            ]
            Path(argv[argv.index("--output") + 1]).write_bytes(
                json.dumps({"dependencies": deps}).encode()
            )
            return subprocess.CompletedProcess(argv, 1, "", "")

        for profile in (None, "vendor-studio"):
            with patch.object(audit, "_quiet", side_effect=scanner) as quiet:
                status, lines = audit.local_audit(root, root, profile=profile)
            assert quiet.call_count == 1 and status == "FAIL", lines
            assert any("vendor-transitive 2 GHSA-vendor-test" in line for line in lines), lines
            for path in (direct, lock):
                content = path.read_bytes()
                path.unlink()
                with patch.object(audit, "_quiet") as quiet:
                    status, lines = audit.local_audit(root, root, profile=profile)
                assert status == "FAIL" and not quiet.called, lines
                path.write_bytes(content)


def test_profile_input_boundaries():
    """#297: unsafe or missing inventories never launch a resolver or scanner."""
    from scripts import audit_runtime_profiles as audit

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".github").mkdir()
        (root / ".github/pip-audit-ignores.txt").write_bytes(b"")
        manifest = root / ".github/runtime-profiles.json"
        for entries in (
            ["--extra-index-url", "https://example.invalid", "pkg"],
            ["pkg @ https://example.invalid/pkg.whl"],
            ["--no-deps"],
        ):
            manifest.write_bytes(json.dumps({"evil": entries}).encode())
            with patch.object(audit, "_quiet") as quiet:
                status, _ = audit.local_audit(root, root)
            assert status == "FAIL" and not quiet.called
        manifest.write_bytes(b'{"s3": ["boto3"]}')
        (root / "requirements.lock").write_bytes(b"base==1\n")
        for value in (
            None,
            b"boto3\n",
            b"boto3>=1\n",
            b"boto3==1.*\n",
            b"--index-url https://example.invalid\n",
            b"",
        ):
            path = root / "requirements-s3.lock"
            if value is not None:
                path.write_bytes(value)
            with patch.object(audit, "_quiet") as quiet:
                status, _ = audit.local_audit(root, root, profile="s3")
            assert status == "FAIL" and not quiet.called, value
        path.write_bytes(b"boto3==1 --hash=sha256:" + b"a" * 64 + b"\n")
        assert audit.profile_pins(root, "s3", ["boto3"]) == ["base==1", "boto3==1"]
        with patch.object(audit, "_quiet", side_effect=subprocess.TimeoutExpired("audit", 1)):
            assert audit.local_audit(root, root, profile="s3")[0] == "WARN"
        with (
            patch.dict(os.environ, {"PROVIDER_PRIVATE_VALUE": "fixture", "PYTHONPATH": tmp}),
            patch.object(audit.subprocess, "run") as run,
        ):
            audit._quiet(["unused"], 1, root)
        env = run.call_args.kwargs["env"]
        assert "PROVIDER_PRIVATE_VALUE" not in env and "PYTHONPATH" not in env


def test_hash_locks():
    from scripts import hash_locks as hashes
    from scripts.audit_runtime_profiles import file_pins
    from scripts.sbom import pinned_lines

    first, second = "a" * 64, "b" * 64
    calls = []

    def fetch(url):
        calls.append(url)
        return {"urls": [{"digests": {"sha256": d}} for d in (second, first, first)]}

    source = '# provenance\npkg==1.0rc1 ; sys_platform != "win32" # prerelease-accepted: review\n'
    result = hashes.hashed(source, fetch)
    assert result == hashes.hashed(result, fetch)
    assert result.count("--hash=") == 2 and result.index(first) < result.index(second)
    assert result.endswith(" # prerelease-accepted: review\n")
    assert calls == ["https://pypi.org/pypi/pkg/1.0rc1/json"] * 2
    assert hashes.missing_hashes(source) and not hashes.missing_hashes(result)
    for bad in ("http://pypi.org/x", "https://evil.invalid/x", "https://pypi.org@evil.invalid/x"):
        try:
            hashes.fetch_json(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe URL accepted")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "requirements.lock"
        path.write_bytes(result.encode("ascii"))
        assert next(iter(requirements(path))).name == "pkg"
        assert not prerelease_errors([path])
        assert file_pins([path]) == ["pkg==1.0rc1"]
        assert pinned_lines([path]) == [source.splitlines()[1].partition("#")[0].strip()]
        assert locked([path], {"sys_platform": "linux"}) == {"pkg": "1.0rc1"}
        for lock in hashes.lock_paths(root):
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_bytes(b"pkg==1\n")
        assert hashes.main(["--check", "--root", tmp], fetch) == 1
        assert hashes.main(["--root", tmp], fetch) == 0
        assert hashes.main(["--check", "--root", tmp], fetch) == 0

    # The manifest feeds web/API/CLI/MCP and the embedded-host upgrade checker.
    from app.release import release_manifest

    read = Path.read_text

    def with_hashes(path, *args, **kwargs):
        text = read(path, *args, **kwargs)
        return hashes.hashed(text, fetch) if path.suffix == ".lock" else text

    baseline = release_manifest()
    with patch.object(Path, "read_text", with_hashes):
        assert release_manifest() == baseline


def test_committed_hash_ratchet():
    from scripts.hash_locks import lock_paths, missing_hashes

    missing = [p.name for p in lock_paths() if missing_hashes(p.read_text(encoding="ascii"))]
    assert not missing, f"Run scripts/hash_locks.py: unhashed locks {missing}"


def _direct_dev_pins(path):
    """Pinned requirements in a dev file, skipping -r/-c option and comment lines."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        body = raw.split("#", 1)[0].strip()
        if not body or body.startswith("-"):
            continue
        yield Requirement(body)


def test_dev_lock_fully_hashed():
    """GitHub #412: the dev closure is a fully hashed lock, not an unpinned requirements file."""
    from scripts.hash_locks import missing_hashes

    dev_lock = ROOT / "requirements-dev.lock"
    assert dev_lock.exists(), "requirements-dev.lock is missing (GitHub #412)"
    assert not missing_hashes(dev_lock.read_text(encoding="utf-8")), (
        "requirements-dev.lock has unhashed pins; run scripts/hash_locks.py"
    )
    # Every direct dev requirement resolves to the reviewed version in the runtime+dev union.
    union = locked([ROOT / "requirements.lock", dev_lock])
    for path in (ROOT / "requirements-dev.txt", ROOT / "requirements-saml.txt"):
        for req in _direct_dev_pins(path):
            if not active(req):
                continue
            version = union.get(canonicalize_name(req.name))
            assert version and req.specifier.contains(version, prereleases=True), (
                f"{path.name}: {req} not satisfied by the dev lock (locked {version})"
            )


def test_ci_dev_install_hashed():
    """GitHub #412: CI installs the dev toolchain hash-pinned, never the unpinned file."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    audit = (ROOT / ".github/workflows/dep-audit.yml").read_text(encoding="utf-8")
    assert not dev_install_errors(ci, audit), "\n".join(dev_install_errors(ci, audit))
    # Gate: every workflow, not only these two, and every pip install in it is hashed.
    for workflow in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        errors = dev_install_errors(workflow.read_text(encoding="utf-8"))
        assert not errors, (workflow.name, errors)
    assert dev_install_errors("pip install -r requirements.txt")
    assert dev_install_errors("pip install --no-deps -r requirements-postgres.txt")
    assert dev_install_errors("pip -q install --require-hashes --no-deps ruff==0.16.9")
    assert dev_install_errors("run: python -m pip install -e .")
    assert not dev_install_errors("  # prose: pip install -r requirements-dev.txt")
    assert "-r requirements-dev.txt" not in ci, "CI must install the hashed dev lock, not dev.txt"
    assert "-r requirements-dev.lock" in ci, "CI must install requirements-dev.lock"
    # Negative control: the guard refuses the pre-#412 mixed install and an unhashed lock install.
    assert dev_install_errors("pip install -r requirements.lock -r requirements-dev.txt")
    assert dev_install_errors("pip install -r requirements-dev.lock")
    assert dev_install_errors("pip install --require-hashes -r requirements-dev.lock")
    # Bare specs (the pre-fix dep-audit and release-check installs) can never be hashed.
    assert dev_install_errors("run: pip install pip-audit==2.10.1 packaging==26.2")
    assert dev_install_errors("python -m pip install --target .t pytest==8.3.4")
    assert dev_install_errors("pip install \\\n  --require-hashes --no-deps pytest==9.1.1")
    assert dev_install_errors("pip install --require-hashes -r a.lock && pip install ruff")
    # Gate fix: the unhashed pip self-upgrade bootstrap is refused too.
    assert dev_install_errors("python -m pip install --upgrade pip")
    assert dev_install_errors("pip install -U pip")
    assert not dev_install_errors(
        "pip install \\\n  --require-hashes --no-deps -r requirements-dev.lock"
    )
    assert not dev_install_errors("pip install --require-hashes --no-deps -t x -r tools.lock")
    # The release-check tools mounted into the image are hash-pinned from a reviewed subset.
    tools = ROOT / "scripts/locks/requirements-ci-check-tools.lock"
    from scripts.hash_locks import lock_paths, missing_hashes

    assert tools in lock_paths(), "hash_locks must cover the CI release-check tools lock"
    assert not missing_hashes(tools.read_text(encoding="ascii"))
    assert set(locked([tools]).items()) <= set(locked([ROOT / "requirements-dev.lock"]).items())
    assert "-r scripts/locks/requirements-ci-check-tools.lock" in ci
    assert "pytest==8.3.4" not in ci and "pip-audit==2.10.1 packaging" not in audit
    assert "-r requirements-dev.lock" in audit, "dep-audit must install the hashed dev lock"


def test_dev_lock_is_not_a_runtime_profile():
    """GitHub #412: the dev lock must not leak into the runtime manifest or blocking audit."""
    from app.release import release_manifest
    from scripts import audit_runtime_profiles as audit

    manifest = release_manifest()
    assert "requirements-dev.lock" not in manifest["dependencies"]
    assert "dev" not in manifest["enabled_extras"]
    # A dev-only pin (a version no runtime lock carries) must not reach the blocking audit.
    # Platform-independent on both sides, like the audit itself: locked() honours environment
    # markers, so on Linux the win32-only tzdata pin in requirements-postgres.lock counted as
    # dev-only here while the audit (which inventories every platform's pins) already carried it.
    runtime = set()
    for path in _runtime_locks():
        runtime |= {pin.lower() for pin in audit.file_pins([path])}
    dev_only = {pin.lower() for pin in audit.file_pins([ROOT / "requirements-dev.lock"])} - runtime
    assert dev_only, "expected dev-only pins (pytest, ruff, pip-audit)"
    blocking = {pin.lower() for pin in audit.locked_pins(ROOT)}
    assert not (dev_only & blocking), sorted(dev_only & blocking)


def test_every_docker_install_requires_hashes():
    for path in (
        ROOT / "Dockerfile",
        ROOT / "tools/vendor_studio/Dockerfile",
        ROOT / ".github/Dockerfile.profile-check",
    ):
        source = path.read_text(encoding="utf-8").replace("\\\n", " ")
        from scripts.release_lock import docker_run_lines

        installs = [line for line in docker_run_lines(source) if "pip install" in line]
        assert installs
        for line in installs:
            for command in re.findall(r"\bpip install\s+([^;&\n]+)", line):
                assert "--require-hashes" in command and "--no-deps" in command, command


def test_ci_installs_hash_consistent():
    """#403: one pip call must not arm hash mode while an unhashed -r pin remains.

    A hash on any requirement OR constraint arms pip's require-hashes mode for
    the whole invocation, so the hashed locks and the unhashed dev tools must
    never share a pip call; the checker follows nested -r/-c lines (the
    python3-saml range hides in requirements-dev.txt's -r requirements-saml.txt).
    """
    for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
        content = workflow.read_text(encoding="utf-8")
        assert not (errors := workflow_install_errors(content, ROOT)), (workflow.name, errors)
    # The shipped bug: hashed locks + unhashed dev in one call must be refused.
    broken = "pip install -r requirements.lock -r requirements-dev.txt\n"
    assert any("requirements-dev.txt" in e for e in workflow_install_errors(broken, ROOT))
    # A hashed -c constraint arms the mode identically, and the nested saml
    # range counts as an unhashed requirement of the same invocation.
    assert workflow_install_errors(
        "pip install -r requirements-dev.txt -c requirements.lock\n", ROOT
    )
    # The long-form options name the same files and cannot slip past the guard.
    assert workflow_install_errors(
        "pip install --requirement=requirements.lock --requirement=requirements-dev.txt\n", ROOT
    )
    for option in ("--requirement", "--constraint"):
        assert workflow_install_errors(
            f"pip install {option} requirements.lock --requirement requirements-dev.txt", ROOT
        )
    # Opus gate: every other spelling optparse resolves to the same -r/-c, and
    # every other way to launch pip install (each was accepted before the gate fix).
    for spelling in (
        "pip install -rrequirements.lock -rrequirements-dev.txt",
        "pip install -crequirements.lock -r requirements-dev.txt",
        "pip install -Ur requirements.lock -r requirements-dev.txt",
        "pip install -qrrequirements.lock -r requirements-dev.txt",
        "pip install --cons requirements.lock -r requirements-dev.txt",
        "pip install --constr=requirements.lock --requirement requirements-dev.txt",
        "pip3 install -r requirements.lock -r requirements-dev.txt",
        "pip3.13 install -r requirements.lock -r requirements-dev.txt",
        "pip -q install -r requirements.lock -r requirements-dev.txt",
        "python -m pip --quiet install -r requirements.lock -r requirements-dev.txt",
        "sudo .venv/bin/pip install -r requirements.lock -r requirements-dev.txt",
        "run: pip install -r requirements.lock -r requirements-dev.txt",
    ):
        assert workflow_install_errors(spelling + "\n", ROOT), spelling
    # Unrelated tools and value-taking short clusters do not over-match.
    for clean in (
        "pip-audit -r requirements.lock --requirement requirements-dev.txt",
        "pip install -t .tools -r requirements-dev.txt",
        "pip install -e . -r requirements-dev.txt",
    ):
        assert not workflow_install_errors(clean + "\n", ROOT), clean
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "compact.txt").write_bytes(b"-rplain.txt\n")
        (root / "plain.txt").write_bytes(b"dev==1\n")
        (root / "hashed.txt").write_bytes(b"runtime==1 --hash=sha256:" + b"a" * 64 + b"\n")
        assert workflow_install_errors("pip install -r compact.txt -c hashed.txt", root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "nested.txt").write_bytes(b"--requirement plain.txt\n")
        (root / "plain.txt").write_bytes(b"dev==1\n")
        (root / "hashed.txt").write_bytes(b"runtime==1 --hash=sha256:" + b"a" * 64 + b"\n")
        assert workflow_install_errors(
            "pip install --requirement nested.txt --constraint hashed.txt", root
        )
    # The fixed recipe's two calls, and each existing clean call, stay clean.
    assert not workflow_install_errors("pip install -r requirements-dev.txt\n", ROOT)
    assert not workflow_install_errors(
        "pip install --require-hashes --no-deps -r requirements.lock -r requirements-saml.lock\n",
        ROOT,
    )


def test_pip_hash_mode_armed_by_any_hash():
    """The #403 mechanism proven by the real pip resolver, fully offline.

    Two inert local wheels stand in for the lock and dev fixtures: pip must
    refuse hashed+unhashed in one call (as -r AND as -c). The real ci.yml recipe
    (#412: one fully hashed call, dev lock included) is then executed and must
    refuse an unhashed or mismatched dev pin. --no-index keeps this hermetic.
    """
    import base64
    import hashlib
    import zipfile

    import yaml

    def build_wheel(index, name, version="1.0", requires=()):
        stem = name.replace("-", "_")  # PEP 427: names are underscore-normalized
        dist_info = f"{stem}-{version}.dist-info"
        metadata = (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {req}\n" for req in requires)
        ).encode("ascii")
        wheel = b"Wheel-Version: 1.0\nGenerator: probe\nRoot-Is-Purelib: true\nTag: py3-none-any\n"

        def record(data):
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            return f"sha256={digest.decode()},{len(data)}"

        record_txt = (
            f"{stem}/__init__.py,{record(b'')}\n"
            f"{dist_info}/METADATA,{record(metadata)}\n"
            f"{dist_info}/WHEEL,{record(wheel)}\n"
            f"{dist_info}/RECORD,,\n"
        ).encode("ascii")
        path = index / f"{stem}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{stem}/__init__.py", b"")
            archive.writestr(f"{dist_info}/METADATA", metadata)
            archive.writestr(f"{dist_info}/WHEEL", wheel)
            archive.writestr(f"{dist_info}/RECORD", record_txt)
        return path

    with tempfile.TemporaryDirectory() as tmp:
        root, index = Path(tmp), Path(tmp) / "index"
        index.mkdir()
        lock_pin = build_wheel(index, "probe-a")
        build_wheel(index, "probe-b")
        digest = hashlib.sha256(lock_pin.read_bytes()).hexdigest()
        (root / "hashed.txt").write_text(f"probe-a==1.0 --hash=sha256:{digest}\n")
        (root / "plain.txt").write_text("probe-b==1.0\n")

        def pip(*args):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--dry-run",
                    "--no-index",
                    "--find-links",
                    str(index),
                    *args,
                ],
                capture_output=True,
                text=True,
                cwd=root,
            )
            return result.returncode, result.stdout + result.stderr

        for label, args in (
            ("mixed -r", ("-r", "hashed.txt", "-r", "plain.txt")),
            ("hashed -c", ("-r", "plain.txt", "-c", "hashed.txt")),
        ):
            code, output = pip(*args)
            assert code != 0 and "Hashes are required" in output, label
        for label, args in (
            ("lock call", ("--require-hashes", "--no-deps", "-r", "hashed.txt")),
            (
                "dev call",
                (
                    "-r",
                    "plain.txt",
                ),
            ),
        ):
            code, output = pip(*args)
            assert code == 0, (label, output)

        # Execute the actual YAML recipe with inert wheels in an empty venv.
        # Only the pip bootstrap upgrade is skipped; no network or shared-env writes.
        import venv

        venv.EnvBuilder(with_pip=True).create(root / "venv")
        python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = {
            **os.environ,
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": str(index),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": os.devnull,
        }
        env.pop("PYTHONPATH", None)
        ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        recipe = next(
            step["run"]
            for step in ci["jobs"]["test-full"]["steps"]
            if step.get("name") == "Install dependencies"
        )
        commands = [shlex.split(line) for line in recipe.splitlines() if line.strip()]
        install = next(cmd for cmd in commands if "--require-hashes" in cmd)
        lock_names = [install[i + 1] for i, arg in enumerate(install) if arg == "-r"]
        constraint_names = [install[i + 1] for i, arg in enumerate(install) if arg == "-c"]
        # #412: the dev toolchain is one of the hashed -r files of the SAME call.
        assert "requirements-dev.lock" in lock_names, install
        assert "--no-deps" in install, install
        assert not any("requirements-dev.txt" in cmd for cmd in commands), recipe
        hashed_lines = {}
        for i, filename in enumerate(lock_names):
            name = f"runtime-{i}"
            wheel = build_wheel(index, name)
            build_wheel(index, name, "2.0")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            hashed_lines[filename] = f"{name}==1.0 --hash=sha256:{digest}\n"
            (root / filename).write_bytes(hashed_lines[filename].encode())
        # A constraint lock (the SAML lock) repeats a pin the -r locks carry, hash and all.
        for filename in constraint_names:
            (root / filename).write_bytes(hashed_lines[lock_names[0]].encode())

        def run_command(command):
            command = list(command)
            destination = None
            if ">" in command:
                pos = command.index(">")
                destination = root / command[pos + 1]
                command = command[:pos]
            if command[0] == "pip":
                command = [str(python), "-m", *command]
            elif command[0] == "python":
                command[0] = str(python)
                if command[1].startswith("scripts/"):
                    command[1] = str(ROOT / command[1])
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            if destination and result.returncode == 0:
                destination.write_bytes(result.stdout.encode())
            return result

        # release_lock uses packaging, which CI has just installed from its runtime lock.
        # Supply that existing library to the fixture venv without downloading anything.
        import shutil

        import packaging

        site = subprocess.check_output(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
        ).strip()
        shutil.copytree(Path(packaging.__file__).parent, Path(site) / "packaging")
        for command in commands:
            if "--upgrade" in command:
                continue
            result = run_command(command)
            assert result.returncode == 0, result.stdout + result.stderr
        result = run_command(["pip", "freeze"])
        assert result.returncode == 0
        for i in range(len(lock_names)):
            assert f"runtime-{i}==1.0" in result.stdout
        # #412: an unhashed dev pin can never ride along in the one hashed call.
        dev_lock = root / "requirements-dev.lock"
        build_wheel(index, "dev-tool")
        dev_lock.write_bytes((hashed_lines["requirements-dev.lock"] + "dev-tool==1.0\n").encode())
        result = run_command(install)
        assert result.returncode != 0 and "Hashes are required" in result.stderr, (
            f"unhashed dev pin installed: {result.stdout}{result.stderr}"
        )
        # A hash that does not match the wheel is refused, too.
        dev_lock.write_bytes(f"dev-tool==1.0 --hash=sha256:{'0' * 64}\n".encode())
        result = run_command(install)
        assert result.returncode != 0 and "DO NOT MATCH" in result.stderr.upper(), (
            f"mismatched dev hash installed: {result.stdout}{result.stderr}"
        )
        # The constraint lock still binds: a constraint that disagrees with a lock pin fails.
        if constraint_names:
            dev_lock.write_bytes(hashed_lines["requirements-dev.lock"].encode())
            other = build_wheel(index, "runtime-0", "2.0")
            digest = hashlib.sha256(other.read_bytes()).hexdigest()
            (root / constraint_names[0]).write_bytes(
                f"runtime-0==2.0 --hash=sha256:{digest}\n".encode()
            )
            result = run_command(install)
            assert result.returncode != 0, f"constraint ignored: {result.stdout}{result.stderr}"


def main():
    tests = (
        test_hash_locks,
        test_committed_hash_ratchet,
        test_dev_lock_fully_hashed,
        test_ci_dev_install_hashed,
        test_dev_lock_is_not_a_runtime_profile,
        test_every_docker_install_requires_hashes,
        test_docker_installs_lock,
        test_direct_requirements,
        test_installed_metadata,
        test_installed_versions,
        test_installed_versions_tool_shadow,
        test_no_prerelease_pins,
        test_static_profile_locks,
        test_profile_lock_byte_identity,
        test_profile_collision_guard,
        test_vendor_studio_lock_matches_root,
        test_lock_regressions,
        test_cli_optimization,
        test_marker_and_snapshot_regressions,
        test_advisory_ledger,
        test_advisory_notes,
        test_ledger_has_no_stale_entries,
        test_ledger_boundaries,
        test_image_comparison,
        test_ci_release_guards,
        test_ci_installs_hash_consistent,
        test_pip_hash_mode_armed_by_any_hash,
        test_local_audit_modes,
        test_saml_lxml_fallback_is_audited_and_non_blocking,
        test_every_install_lock_is_audited_and_triggers_ci,
        test_shadow_pip_audit_refused,
        test_workflow_permissions,
        test_npm_supply_chain,
        test_vendor_supply_chain,
        test_vendor_audit_automation,
        test_vendor_transitive_audit,
        test_profile_input_boundaries,
    )
    ran, errors = set(), []
    for test in tests:
        ran.add(test.__name__)
        try:
            test()
        except Exception as exc:
            errors.append(f"{test.__name__}: {exc}")
    assert ran == {
        name for name, value in globals().items() if name.startswith("test_") and callable(value)
    }
    assert not errors, "\n".join(errors)
    print("RELEASE LOCK OK")


if __name__ == "__main__":
    main()
