"""Offline regressions for GitHub #226: the image OS layer and the Go binaries were never scanned.

Covers:
  * scripts/scan_image.sh exists, is POSIX sh + LF + set -eu, runs Trivy as a
    digest-pinned container image (a placeholder digest only in the MARKED form,
    never an invented one), fails on HIGH/CRITICAL by default (gate run:
    --ignore-unfixed --exit-code 3), and writes the dated table + JSON report +
    CycloneDX image SBOM next to each other under a configurable directory.
  * the scan script EXECUTED under a POSIX shell with a stubbed docker (fed to
    the shell as BYTES: text-mode stdin on Windows writes CRLF, which dash
    rejects): the placeholder and a tag without digest are refused, the gate's
    PASS/FAIL/ERROR exit codes hold, and the report + SBOM are written even when
    the gate fails.
  * deploy/aws-hosted/aws-redeploy.sh calls the scan AFTER the build and BEFORE
    anything is stopped or renamed, refuses to switch when the scan fails, and
    honors --accept-scan-risk only WITH a reason (required, logged). Executed
    end-to-end under bash with a stubbed docker on PATH; no Docker, no network.
  * the Dockerfile carries the APT_UPGRADE rebuild knob, and the Compose launch gate in
    docs/DEPLOY.md and docs/BACKUP.md scans the exact image ID before it serves (GitHub #385).

ASCII-only prints. Final line "IMAGE SCANNING OK".
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

init_env("loctms_image_scanning_")

SCAN = _ROOT / "scripts" / "scan_image.sh"
REDEPLOY = _ROOT / "deploy" / "aws-hosted" / "aws-redeploy.sh"
LAUNCH = _ROOT / "deploy" / "aws-hosted" / "LAUNCH.md"
CADDY_PIN = "caddy:2@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52"
PLACEHOLDER = "REPLACE_WITH_PINNED_TRIVY_DIGEST"
PIN_RE = re.compile(r"TRIVY_IMAGE:-aquasec/trivy:([0-9][\w.-]*)@sha256:([\w]+)")


def _posix_shell():
    """dash first (the script is #!/bin/sh); busybox sh; bash; Git bash last resort.

    Returns the argv prefix that reads the SCRIPT FROM STDIN while keeping the
    following arguments as positional parameters: all shells need "-s"."""
    for name in ("dash", "busybox", "bash"):
        executable = shutil.which(name)
        if executable and not any(p in executable.lower() for p in ("system32", "windowsapps")):
            if name == "busybox":
                return [executable, "sh", "-s"]
            return [executable, "-s"]
    if os.name == "nt":
        dash = Path("C:/Program Files/Git/usr/bin/dash.exe")
        if dash.exists():
            return [str(dash), "-s"]
        return ["C:/Program Files/Git/bin/bash.exe", "--noprofile", "--norc", "-s"]
    raise AssertionError("No dash, busybox sh or bash available")


def _bash():
    """A real bash, skipping the WSL stubs on PATH (as in verify_deploy_security)."""

    def usable(path):
        low = str(path).replace("\\", "/").lower()
        return "/system32/" not in low and "/windowsapps/" not in low

    candidates = [shutil.which("bash")]
    git = shutil.which("git")
    if git:
        candidates.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    candidates.append("C:/Program Files/Git/bin/bash.exe")
    for found in candidates:
        if found and usable(found) and Path(found).exists():
            return str(found)
    return "bash"


def _no_em_dashes(path):
    assert "\u2014" not in path.read_text(encoding="utf-8"), f"em dash in {path}"


def static_scan():
    data = SCAN.read_bytes()
    assert b"\r" not in data, "scan script must be LF"
    lines = data.split(b"\n")
    assert lines[0] == b"#!/bin/sh", "POSIX sh shebang"
    assert b"set -eu" in lines, "set -eu on its own line"
    text = data.decode("utf-8")
    _no_em_dashes(SCAN)

    m = PIN_RE.search(text)
    assert m, "Trivy must be pinned tag@sha256:<digest> in the TRIVY_IMAGE default"
    tag, digest = m.groups()
    assert re.fullmatch(r"[0-9][\w.-]*", tag)
    assert re.fullmatch(r"[a-f0-9]{64}", digest), (
        f"default digest must be real 64-hex, got {digest!r}"
    )
    assert PLACEHOLDER in text, "the marked placeholder form must be present"
    assert "trivy:latest" not in text, ":latest is forbidden"

    # Fails on HIGH/CRITICAL by default; the gate counts only findings WITH a fix.
    assert "SCAN_SEVERITIES:-HIGH,CRITICAL" in text, "default severity list HIGH,CRITICAL"
    assert "--ignore-unfixed --exit-code 3" in text, "distinct findings status"
    assert "docker run" in text and '"$TRIVY_IMAGE"' in text, "Trivy runs as a container"
    # Dated table + JSON report + CycloneDX image SBOM, side by side, configurable dir.
    for token in (
        "--format table",
        "--format json",
        "--format cyclonedx",
        "--output",
        "date -u",
        "SCAN_OUT_DIR",
        ".cdx.json",
    ):
        assert token in text, f"scan script must use {token!r}"
    print("  static: POSIX sh + LF + set -eu; digest pin (marked form); HIGH/CRITICAL gate")
    print("  static: dated table + JSON report + CycloneDX image SBOM under a configurable dir")


def static_redeploy():
    data = REDEPLOY.read_bytes()
    assert b"\r" not in data, "redeploy script must be LF"
    assert data.startswith(b"#!/usr/bin/env bash"), "kit scripts are bash"
    assert b"set -euo pipefail" in data.split(b"\n")
    text = data.decode("utf-8")
    _no_em_dashes(REDEPLOY)

    scan_call = text.index('sh "$SCAN_SCRIPT"')
    # The quoted forms are the real commands (the header's rollback recipe is prose).
    stop_call = text.index('docker stop "$CONTAINER"')
    rename_call = text.index('docker rename "$CONTAINER"')
    assert scan_call < stop_call, "scan runs BEFORE the container is stopped"
    assert scan_call < rename_call, "scan runs BEFORE the old container is renamed"
    assert "--image" in text and "${2:?--image" in text, "--image is required"
    assert "--build-dir" in text and "RELEASE_COMMIT" in text, "build keeps the release args"
    # --accept-scan-risk needs a reason (required by the flag itself) and is logged.
    risk_line = next(line for line in text.splitlines() if "--accept-scan-risk)" in line)
    assert "${2:?--accept-scan-risk" in risk_line, "the reason is required"
    assert "ACCEPT-SCAN-RISK" in text and '"$ACCEPT_RISK"' in text, "the reason is logged"
    assert "refusing to switch" in text, "a failing scan refuses the switch"
    print("  static: redeploy scans before the switch; accept-risk requires and logs a reason")


def dockerfile_apt_upgrade_knob():
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG APT_UPGRADE=0" in dockerfile
    assert "apt-get upgrade -y" in dockerfile
    print("  the Dockerfile carries the APT_UPGRADE rebuild knob")


def _code_lines(text, inline=False):
    """Commands inside fenced blocks (and, with inline=True, inline `code` spans, which the
    launch runbook uses for its commands), backslash continuations joined."""
    text = text.replace("\r\n", "\n")
    chunks = re.findall(r"```[a-z]*\n(.*?)```", text, re.S)
    if inline:
        chunks += re.findall(r"(?<!`)`([^`\n]+)`(?!`)", text)
    lines = []
    for chunk in chunks:
        joined = re.sub(r"\\\r?\n\s*", " ", chunk.replace("\r\n", "\n"))
        lines += [line.strip() for line in joined.split("\n") if line.strip()]
    return lines


# Every active document an operator follows to build, deploy, promote, restore or roll back.
SERVING_DOCS = (
    "docs/DEPLOY.md",
    "docs/BACKUP.md",
    "deploy/aws-hosted/LAUNCH.md",
)


def no_bypass_commands():
    """Gate round: sweep every serving doc for the commands that put an image into service.

    A container start must name a scanned ID variable or a digest-pinned reference (never a
    mutable tag); Compose starts never build or pull.
    Each pattern here is a path an earlier review round found open."""
    for rel in SERVING_DOCS:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        # Container starts are checked in inline code too: prose never names `docker run -d`.
        for line in _code_lines(text, inline=True):
            if re.search(r"\bdocker run\b[^|;&]*\s-d\b", line):
                try:
                    image = shlex.split(line.rstrip(" &\\"))[-1]
                except ValueError:
                    image = line.rstrip(" &\\").split()[-1]
                ok = "@sha256:" in image or re.match(r"\$\{?[A-Z_]*(ID|IMAGE)\b", image)
                assert ok, f"{rel}: container start of an unscanned/mutable image: {line}"
        # Compose commands: fenced blocks (prose quotes them inline as warnings).
        for line in _code_lines(text):
            if re.search(r"\bdocker compose\b.*\bup\b", line):
                assert "--build" not in line.replace("--no-build", ""), f"{rel}: {line}"
                assert "--no-build --pull never" in line, f"{rel}: start may rebuild: {line}"
    print("  #385 gate: no serving doc starts a mutable tag, rebuilds on start or skips the gate")


def launch_integrates_scan():
    """The scanner is wired into the launch procedure, not just present beside it.

    Fails when a launch step can serve an unscanned image."""
    _no_em_dashes(LAUNCH)
    launch = LAUNCH.read_text(encoding="utf-8")

    # App image: scanned before it serves; capture the immutable ID, scan it, start that ID.
    assert "Scan the image BEFORE it serves" in launch, "the image step must scan before serving"
    capture = launch.index(
        "APP_ID=\"$(docker image inspect --format '{{.Id}}' localizationos:launch1)\" && "
        'sh scripts/scan_image.sh "$APP_ID"'
    )
    start_app = launch.index("4. Start the app")
    assert capture < start_app, "the app image is scanned before the start step"
    start_cmd = launch[start_app:].split("docker run -d --name locosapp", 1)[1].split("```", 1)[0]
    assert '"${APP_ID:?scan the image in section 3 first}"' in start_cmd, start_cmd
    assert "localizationos:launch1" not in start_cmd, "the start step must not use the mutable tag"
    assert "A failing scan" in launch and "named approver" in launch

    # Caddy image: the exact pinned reference is pulled, scanned and validated before it starts.
    pull = launch.index('docker pull "$CADDY_IMAGE" && sh scripts/scan_image.sh "$CADDY_IMAGE"')
    validate = launch.index('docker run --rm -v /srv/caddy:/etc/caddy:ro "$CADDY_IMAGE"')
    assert launch.index(f"CADDY_IMAGE={CADDY_PIN}") < pull < validate
    assert validate < launch.index("docker run -d --name caddy"), "Caddy starts after the scan"
    assert "&&" in launch[pull : pull + 80], "a failed pull must not proceed to the scan"

    # Existing host: the running Caddy image ID is scanned before validation and the swap.
    resolve = launch.index("HOST_IMAGE=\"$(sudo docker inspect --format '{{.Image}}' caddy)\"")
    scan = launch.index('sh scripts/scan_image.sh "${HOST_IMAGE:?}"')
    check = launch.index('sudo docker run --rm -v /srv/caddy-next:/etc/caddy:ro "${HOST_IMAGE:?}"')
    assert resolve < scan < check < launch.index("sudo docker stop caddy-old")

    # Discoverability and pinning.
    assert "MONTHLY" in launch.upper() and "aws-redeploy.sh" in launch
    refs = re.findall(r"(?<![\w/.-])(?:caddy|postgres):[\w.\-]+(?:@sha256:[0-9a-f]{64})?", launch)
    for ref in refs:
        assert "@sha256:" in ref, f"unpinned base reference in the launch guide: {ref!r}"
    print("  launch guide: app + Caddy scanned before serving; failing scan needs an approver")


def compose_launch_gate():
    doc = (_ROOT / "docs/DEPLOY.md").read_text(encoding="utf-8")
    for heading in ("## 0. TL;DR", "## 1. Quick start", "## Upgrade"):
        section = doc.split(heading, 1)[1].split("\n## ", 1)[0]
        assert "#compose-image-scan-gate" in section, heading
    assert not re.search(r"docker compose[^\n]*up[^\n]*--build", doc)
    gate = doc.split("## Compose image scan gate", 1)[1].split("\n## ", 1)[0]
    commands = (
        "docker compose build app",
        "docker compose config --services",
        'docker compose config --images "$SERVICE"',
        "docker image inspect --format",
        'sh scripts/scan_image.sh "$IMAGE_ID"',
        'docker tag "$IMAGE_ID" "$PINNED"',
        'image: "%s"',
        'export COMPOSE_FILE="$COMPOSE_FILE:$SCAN_OVERRIDE"',
        "for PINNED in $(docker compose config --images)",
        '= "sha256:${PINNED##*:}" ] || exit 1',
        "docker compose up -d --no-build --pull never",
    )
    positions = [gate.index(command) for command in commands]
    assert positions == sorted(positions)
    assert 'docker compose pull "$SERVICE"' in gate
    assert "|| exit 1" in gate and '*" $IMAGE_ID "*)' in gate and "*) exit 1 ;;" in gate
    assert "written risk acceptance" in gate and "exact image ID" in gate
    assert "Vulnerability Scan Record" in gate and "MONTHLY in-season rebuild" in gate
    for command in re.findall(r"^\s*docker compose[^\n]*\bup\b[^\n]*", doc, re.M):
        assert "--no-build --pull never" in command, command
    # Gate round: the failed-upgrade rollback and the restore runbook route through the gate.
    failed = doc.split("For a failed upgrade", 1)[1].split("```", 1)[0]
    assert "#compose-image-scan-gate" in failed, "rollback to the old image is scanned"
    backup = (_ROOT / "docs/BACKUP.md").read_text(encoding="utf-8")
    assert "Compose image scan gate" in backup and "--build" not in backup.replace("--no-build", "")
    assert "docker compose start app" in gate, "nightly restart reuses the scanned container"
    print("  #385 round 2: Compose entrypoints scan and pin all enabled service images")


# The fake container filesystem only exposes /reports through a bind mount.
# Every scan fails if it omits the host mount, cache or immutable image ID.
DOCKER_STUB = r"""#!/bin/sh
printf 'docker %s\n' "$*" >> "$SHIM_LOG"
case "$1 $2" in
  'image inspect')
    case "$*" in
      *Config.Env*) printf 'PATH=/old/bin\nPYTHON_VERSION=old\nPORT=8000\n' ;;
      *) printf 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' ;;
    esac
    exit 0 ;;
  'inspect --format')
    case "$3" in
      *Config.Env*) printf 'PATH=/old/bin\nPYTHON_VERSION=old\nPORT=9000\nEXTRA=keep\n' ;;
      *Mounts*) printf '%s|/data|true\n%s|/extra|false\n' "$DATA_SOURCE" "$EXTRA_SOURCE" ;;
      *Image*) printf 'sha256:old\n' ;;
    esac
    exit 0 ;;
  'inspect -f') printf '%s\n' "${RUNNING:-true}"; exit 0 ;;
  'rm -f')
    if [ "$3" = locosapp-old ]; then exit "${REMOVE_OLD_EXIT:-0}"; fi
    if [ "$3" = locosapp ]; then exit "${REMOVE_NEW_EXIT:-0}"; fi
    exit 0 ;;
  'exec locosapp') exit "${READY_EXIT:-0}" ;;
  'run -d')
    last=''
    while [ $# -gt 0 ]; do
      if [ "$1" = --env-file ]; then
        cp "$2" "$ENV_CAPTURE"
        stat -c %a "$2" > "$MODE_CAPTURE"
      fi
      last="$1"; shift
    done
    [ "$last" = sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ] || exit 95
    exit "${RUN_EXIT:-0}" ;;
  'run --rm') ;;
  *) exit 0 ;;
esac
out=''; reports=''; cache=0; gate=0; vuln=0; last=''
while [ $# -gt 0 ]; do
  case "$1" in
    -v)
      case "$2" in
        *:/reports) reports="${2%:/reports}" ;;
        trivy-cache:/root/.cache/trivy) cache=1 ;;
      esac ;;
    --output) out="$2" ;;
    --exit-code) [ "$2" = 3 ] || exit 94; gate=1 ;;
    --scanners) [ "$2" = vuln ] || exit 93; vuln=1 ;;
  esac
  last="$1"; shift
done
[ "$last" = sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ] || exit 92
[ -n "$reports" ] && [ "$cache" = 1 ] && [ "$vuln" = 1 ] || exit 91
[ "${REPORT_EXIT:-0}" = 0 ] || exit "$REPORT_EXIT"
case "$out" in
  /reports/*) printf 'stub report\n' > "$reports/${out#/reports/}" ;;
  /dev/null) ;;
  *) exit 90 ;;
esac
if [ "$gate" = 1 ]; then exit "${GATE_EXIT:-0}"; fi
"""


def _fixture(tmp):
    root = Path(tmp)
    bindir = root / "bin"
    bindir.mkdir()
    (root / "reports with spaces").mkdir()
    (bindir / "docker").write_bytes(DOCKER_STUB.encode())
    (bindir / "sudo").write_bytes(b"""#!/bin/sh
printf 'sudo %s\\n' "$*" >> "$SHIM_LOG"
exit "${BACKUP_EXIT:-0}"
""")
    (bindir / "sleep").write_bytes(b"#!/bin/sh\nexit 0\n")
    for file in bindir.iterdir():
        file.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=str(bindir) + os.pathsep + env["PATH"],
        SHIM_LOG=(root / "docker.log").as_posix(),
        SCAN_OUT_DIR=(root / "reports with spaces").as_posix(),
        DATA_SOURCE=(root / "data with spaces").as_posix(),
        EXTRA_SOURCE=(root / "extra").as_posix(),
        ENV_CAPTURE=(root / "env").as_posix(),
        MODE_CAPTURE=(root / "mode").as_posix(),
        HOME=root.as_posix(),
        MSYS_NO_PATHCONV="1",
    )
    if os.name == "nt":
        env["PATH"] = (
            str(bindir) + os.pathsep + "C:/Program Files/Git/usr/bin" + os.pathsep + env["PATH"]
        )
    for key in (
        "TRIVY_IMAGE",
        "GATE_EXIT",
        "REPORT_EXIT",
        "RUN_EXIT",
        "READY_EXIT",
        "RUNNING",
        "BACKUP_EXIT",
    ):
        env.pop(key, None)
    if os.name == "nt":
        for key in (
            "SHIM_LOG",
            "SCAN_OUT_DIR",
            "DATA_SOURCE",
            "EXTRA_SOURCE",
            "ENV_CAPTURE",
            "MODE_CAPTURE",
            "HOME",
        ):
            env[key] = "/" + env[key][0].lower() + env[key][2:]
    return root, env


def _output(proc):
    return (proc.stdout + proc.stderr).decode("utf-8", "replace")


def scan_behavior():
    with tempfile.TemporaryDirectory(prefix="loctms_scanrun_") as tmp:
        root, env = _fixture(tmp)
        cases = (
            (
                "placeholder",
                {"TRIVY_IMAGE": "aquasec/trivy:0.74.0@sha256:" + PLACEHOLDER},
                2,
                "placeholder",
            ),
            ("tag only", {"TRIVY_IMAGE": "aquasec/trivy:0.74.0"}, 2, "digest-pinned"),
            ("default pin", {}, 0, "PASS"),
            ("findings", {"GATE_EXIT": "3"}, 1, "FAIL"),
            ("scanner error", {"GATE_EXIT": "1"}, 2, "ERROR"),
            ("report error", {"REPORT_EXIT": "1"}, 2, "ERROR"),
        )
        for name, overrides, rc, marker in cases:
            log = root / "docker.log"
            log.unlink(missing_ok=True)
            reports = root / "reports with spaces"
            if reports.exists():
                for file in reports.iterdir():
                    file.unlink()
            proc = subprocess.run(
                [*_posix_shell(), "testimg:v1"],
                input=SCAN.read_bytes(),
                capture_output=True,
                env=env | overrides,
                timeout=60,
            )
            out = _output(proc)
            assert proc.returncode == rc and marker in out, (name, proc.returncode, out)
            lines = log.read_text().splitlines() if log.exists() else []
            if name in ("placeholder", "tag only"):
                assert not lines
                continue
            assert sum(line.startswith("docker image inspect ") for line in lines) == 1
            if name == "report error":
                continue
            runs = [line for line in lines if line.startswith("docker run ")]
            assert len(runs) == 4, lines
            assert sum("--format cyclonedx" in line for line in runs) == 1
            assert "--output /dev/null" in runs[-1]
            assert "id=sha256:" in out and "IMAGE-SCAN testimg:v1:" in out
            assert len(list(reports.glob("*"))) == 3
        print("  scan: mounted host reports, immutable ID, cache, vuln-only, distinct errors OK")


def redeploy_behavior():
    with tempfile.TemporaryDirectory(prefix="loctms_redeploy_") as tmp:
        root, env = _fixture(tmp)

        def run(extra=(), **overrides):
            (root / "docker.log").unlink(missing_ok=True)
            proc = subprocess.run(
                [
                    _bash(),
                    "--noprofile",
                    "--norc",
                    "-s",
                    "--",
                    "--image",
                    "testimg:v1",
                    "--scan-script",
                    SCAN.as_posix(),
                    *extra,
                ],
                input=REDEPLOY.read_bytes(),
                capture_output=True,
                env=env | overrides,
                timeout=120,
            )
            log = root / "docker.log"
            lines = log.read_text().splitlines() if log.exists() else []
            return proc, lines

        proc, lines = run()
        assert proc.returncode == 0 and "SWITCHED" in _output(proc), _output(proc)
        scan = next(i for i, line in enumerate(lines) if line.startswith("docker run --rm"))
        stop = lines.index("docker stop locosapp")
        backup = next(i for i, line in enumerate(lines) if line.startswith("sudo tar "))
        rename = lines.index("docker rename locosapp locosapp-old")
        assert scan < stop < backup < rename
        assert env["DATA_SOURCE"] in lines[backup] and env["EXTRA_SOURCE"] in lines[backup]
        assert "backup-pre-" in lines[backup] and "backup=" in _output(proc)
        assert (root / "env").read_text().splitlines() == ["PORT=9000", "EXTRA=keep"]
        assert (root / "mode").read_text().strip() == "600"
        assert any("/extra:ro" in line for line in lines)

        proc, lines = run(REMOVE_OLD_EXIT="1")
        assert proc.returncode == 0 and "SWITCHED" in _output(proc), _output(proc)

        for overrides in ({"GATE_EXIT": "3"}, {"GATE_EXIT": "1"}):
            proc, lines = run(**overrides)
            assert proc.returncode == 1 and "refusing to switch" in _output(proc)
            assert "docker stop locosapp" not in lines

        reason = "suite proof: approved by operator"
        proc, lines = run(["--accept-scan-risk", reason], GATE_EXIT="3")
        assert proc.returncode == 0, _output(proc)
        assert reason in _output(proc)
        log = root / "reports with spaces" / "accept-scan-risk.log"
        assert reason in log.read_text() and "operator=" in log.read_text()

        for args in (
            ["--accept-scan-risk"],
            ["--accept-scan-risk", "  \t"],
            ["--accept-scan-risk", "--image", "x"],
        ):
            proc, lines = run(args)
            assert proc.returncode != 0 and not lines, (_output(proc), lines)

        proc, lines = run(BACKUP_EXIT="1")
        assert proc.returncode == 1 and "docker start locosapp" in lines
        assert "docker rename locosapp locosapp-old" not in lines
        assert not any(line.startswith("docker run -d") for line in lines)

        for overrides in (
            {"RUN_EXIT": "1", "REMOVE_NEW_EXIT": "1"},
            {"RUN_EXIT": "1"},
            {"RUNNING": "false"},
            {"READY_EXIT": "1"},
        ):
            proc, lines = run(**overrides)
            assert proc.returncode == 1, _output(proc)
            assert "docker rename locosapp-old locosapp" in lines
            assert lines[-1] == "docker start locosapp"
            assert "SWITCHED" not in _output(proc) and "backup=" in _output(proc)
            remove = lines.index("docker rm -f locosapp")
            restore = next(i for i, line in enumerate(lines) if line.startswith("sudo tar -xzpf"))
            rename = lines.index("docker rename locosapp-old locosapp")
            assert remove < restore < rename
            assert sum(line.startswith("sudo mv --") for line in lines) == 2
        print("  redeploy: backup before swap, env difference, persistent risk log, rollback OK")


def main():
    # Run every block even on failure so the red run records independent defects.
    failures = []
    for check in (
        static_scan,
        static_redeploy,
        dockerfile_apt_upgrade_knob,
        launch_integrates_scan,
        compose_launch_gate,
        no_bypass_commands,
        scan_behavior,
        redeploy_behavior,
    ):
        try:
            check()
        except (AssertionError, ValueError) as exc:
            failures.append(f"{check.__name__}: {exc}")
    assert not failures, "\n".join(failures)
    print("IMAGE SCANNING OK")


if __name__ == "__main__":
    main()
