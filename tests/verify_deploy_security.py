"""Offline regressions for AWS review #254, #258, #238, #405, #406 and #415; no AWS or Docker calls.

Execute the actual metadata and subscription shell blocks with in-process stubs.
Owned production files: deploy/aws-hosted/aws-instance-setup.sh,
deploy/aws-hosted/aws-alerting-setup.sh, deploy/aws-hosted/aws-verify.sh,
deploy/aws-hosted/host/install-audit-alert.sh,
deploy/aws-hosted/caddy/Caddyfile.cloudflare.
Check the Caddy routing contract and operator instructions statically. Real Caddy
validation and Cloudflare requests remain mandatory launch checks in the runbook.
LOCOS_DEPLOY_TEST_BASE=HEAD reads base production files for the red regression run.
"""

import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import jmespath
except ImportError:
    jmespath = None  # Optional S3 extra; snapshot behavior still runs on a default install.

ROOT = Path(__file__).resolve().parents[1]
KIT = "deploy/aws-hosted/"
BASE = os.environ.get("LOCOS_DEPLOY_TEST_BASE")


def _bash():
    """A real bash. On Windows the WSL `bash.exe` stub (System32, WindowsApps) is first on
    PATH outside Git Bash (the detached pregate), so it is skipped, as in verify_docker_saml."""

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


BASH = _bash()


def source(path):
    if BASE:
        return subprocess.check_output(
            ["git", "show", f"{BASE}:{path}"], cwd=ROOT, encoding="utf-8"
        )
    return (ROOT / path).read_text(encoding="utf-8")


def shell(body):
    result = subprocess.run(
        [BASH, "--noprofile", "--norc"],
        input="set -eu\n" + body,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def metadata():
    for name in ("aws-instance-setup.sh", "aws-alerting-setup.sh", "aws-verify.sh"):
        result = subprocess.run(
            [BASH, "-n"],
            input=source(KIT + name),
            capture_output=True,
            encoding="utf-8",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
    setup = source(KIT + "aws-instance-setup.sh")
    block = setup.split("# --- 1. IMDSv2", 1)[1].split("# --- 2.", 1)[0]
    block = block[block.index("\n") :]
    verify = source(KIT + "aws-verify.sh")
    check = verify.split("# 2. Instance metadata", 1)[1].split('ping="', 1)[0]
    check = check[check.index("\n") :]
    for hop in ("1", "2", "64", "None", "ERROR"):
        stubs = f"""
INSTANCE=i-example
iq() {{
  case "$1" in
    *.HttpTokens) echo required ;;
    *.HttpPutResponseHopLimit) echo {hop} ;;
    *.HttpEndpoint) echo enabled ;;
    *.State) echo applied ;;
  esac
}}
change() {{ echo "CHANGE $*"; }}
note() {{ :; }}
pass() {{ echo "PASS $*"; }}
fail() {{ echo "FAIL $*"; }}
"""
        out = shell(stubs + block)
        if hop == "1":
            assert "CHANGE" not in out, out
        else:
            assert "--http-put-response-hop-limit 1" in out, out
        out = shell(stubs + check)
        assert ("FAIL IMDS response hop limit" in out) == (hop != "1"), out
    assert "MetadataOptions.State" in check
    for endpoint in ("enabled", "disabled", "None", "ERROR"):
        endpoint_stubs = stubs.replace(f"echo {hop}", "echo 1")
        endpoint_stubs = endpoint_stubs.replace("echo enabled", f"echo {endpoint}")
        out = shell(endpoint_stubs + check)
        assert ("FAIL IMDS endpoint" in out) == (endpoint != "enabled"), out
        assert ("PASS IMDS endpoint" in out) == (endpoint == "enabled"), out
        assert ("FAIL" in out) == (endpoint != "enabled"), out
    assert "--http-tokens required" in block
    book = " ".join(source(KIT + "LAUNCH.md").split())  # prose wraps; compare flattened
    assert "**Metadata response hop limit: 1**" in book
    assert "--http-put-response-hop-limit 1" in book
    assert "Pre-check before applying this to an existing instance" in book
    assert "S3, Bedrock or any other AWS SDK" in book
    assert "explicit scoped credentials" in book
    assert "accepted hop-limit-2 exception" in book
    assert "Only proceed with hop limit 1 after confirming" in book
    dry = source(KIT + "test/run-dry.sh")
    assert 'SHIM_HOP_LIMIT=2 bash "$KIT/aws-instance-setup.sh"' in dry
    assert (
        dry.count(
            'has "--http-tokens required --http-endpoint enabled --http-put-response-hop-limit 1"'
        )
        == 2
    )
    assert '"repairs hop limit alone"' in dry


def subscriptions():
    verify = source(KIT + "aws-verify.sh")
    block = verify[verify.index("  topic_check() {") :]
    block = block.split('  pol="', 1)[0]
    for auth in ("true", "false", "None", "ERROR"):
        for region in ("us-east-2", "us-east-1"):
            stubs = f"""
REGION=us-east-2; HC_REGION=us-east-1; TOPIC=locos-alerts
TOPIC_ARN=arn:aws:sns:us-east-2:123456789012:locos-alerts
TOPIC1_ARN=arn:aws:sns:us-east-1:123456789012:locos-alerts
tab=$(printf '\\t')
pass() {{ echo "PASS $*"; }}
fail() {{ echo "FAIL $*"; }}
r() {{
  local action="$2" rg="" arn=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --region) rg="$2"; shift ;;
      --topic-arn) arn="$2"; shift ;;
    esac
    shift
  done
  case "$action" in
    get-topic-attributes) echo "$arn" ;;
    list-subscriptions-by-topic)
      printf 'operator@example.invalid\\t%s:sub\\n' "$arn" ;;
    get-subscription-attributes)
      if [ "$rg" = "{region}" ]; then echo {auth}; else echo true; fi ;;
    *) exit 90 ;;
  esac
}}
"""
            out = shell(stubs + block)
            assert ("FAIL" in out) == (auth != "true"), out
            if auth != "true":
                assert f"missing or unreadable ({region})" in out, out
    setup = source(KIT + "aws-alerting-setup.sh")
    assert "Attributes.ConfirmationWasAuthenticated" in setup
    assert "--authenticate-on-unsubscribe true" in setup
    assert "click Confirm subscription" not in setup
    ensure = setup[setup.index("ensure_topic() {") :].split('echo "[1/6]', 1)[0]
    for auth in ("true", "false", "None"):
        out = shell(
            f"""
EMAIL=operator@example.invalid; TOPIC=locos-alerts; DRY_RUN=1; PENDING=0
probe_in() {{ return 0; }}
note() {{ echo "$*"; }}
change_in() {{ echo UNEXPECTED_WRITE; }}
q_in() {{
  case "$3" in
    list-subscriptions-by-topic) echo arn:aws:sns:us-east-1:123456789012:locos-alerts:sub ;;
    get-subscription-attributes) echo {auth} ;;
    *) exit 90 ;;
  esac
}}
"""
            + ensure
            + '\nensure_topic us-east-1 example\necho "PENDING $PENDING"\n'
        )
        assert "UNEXPECTED_WRITE" not in out, out
        assert ("PENDING 1" in out) == (auth != "true"), out
    book = source(KIT + "LAUNCH.md")
    assert book.count("--authenticate-on-unsubscribe true") >= 2
    assert "aws sns unsubscribe" in book and "aws sns get-subscription-attributes" in book


def origin():
    config = source(KIT + "caddy/Caddyfile.cloudflare")
    assert "import /etc/caddy/origin-auth.caddy" in config
    route = config.split("handle @origin {", 1)[1].split("\n\thandle {", 1)
    assert len(route) == 2 and config.count("reverse_proxy ") == 1
    assert "reverse_proxy locosapp:8000" in route[0]
    assert "header_up -X-Locos-Origin" in route[0]
    assert 'respond "Forbidden" 403' in route[1]
    book = source(KIT + "LAUNCH.md")
    for required in (
        "@origin header X-Locos-Origin %s",
        "openssl rand -hex 32",
        "never Add",
        "-v /srv/caddy:/etc/caddy:ro",
        "caddy validate",
        "--resolve",
        "X-Locos-Origin: wrong",
        "**403**",
        "**200**",
        "Existing host",
        "Launch-day checklist",
    ):
        assert required in book, required


_CLIENT_IP_CHILD = r"""
import json, sys
from tests._bootstrap import init_env
init_env("loctms_deploy_cfip_")
from starlette.requests import Request
from app.services import security, settings
value = sys.argv[1]
settings.get = lambda key, *a, **kw: value if key == "trusted_proxy_ips" else ""
out = []
for peer, xff in json.loads(sys.argv[2]):
    scope = {"type": "http", "client": (peer, 5000),
             "headers": [(b"x-forwarded-for", xff.encode())] if xff else []}
    out.append(security.client_ip(Request(scope)))
print(json.dumps(out))
"""


def cloudflare_ip():
    # #415: Caddy takes the visitor from CF-Connecting-IP only and REPLACES X-Forwarded-For
    # with that one address; the app trusts only Caddy. Trusting the Cloudflare ranges made
    # client_ip() skip a Cloudflare-range hop in a visitor-extendable chain, so the caller chose
    # the address to its left. Red on base (CF ranges trusted, chain kept).
    config = source(KIT + "caddy/Caddyfile.cloudflare")
    lines = [
        " ".join(raw.split()) for raw in config.splitlines() if not raw.strip().startswith("#")
    ]
    assert "client_ip_headers CF-Connecting-IP" in lines, (
        "client IP must come from CF-Connecting-IP only"
    )
    assert not any(ln.startswith("client_ip_headers") and "X-Forwarded-For" in ln for ln in lines)
    route = config.split("handle @origin {", 1)[1].split("\n\thandle {", 1)[0]
    assert "header_up X-Forwarded-For {client_ip}" in route, "Caddy must replace X-Forwarded-For"
    # The doc-to-config link: the value the app is told to trust is the one the Caddyfile
    # comment states, and the operator guide's step shows the same single address.
    stated = re.search(r"^#\s+trusted_proxy_ips = (\S+)\s*$", config, re.M)
    assert stated, "Caddyfile.cloudflare must state the trusted_proxy_ips value"
    value = stated.group(1)
    assert value == "172.30.0.10/32", f"app must trust only Caddy, got {value[:60]!r}"
    book = source(KIT + "LAUNCH.md")
    step = book.split("**Tell the app to trust Caddy, and only Caddy**", 1)[1]
    assert step.split("```", 2)[1].strip() == value, "operator guide and Caddyfile disagree"
    cases = [
        ("172.30.0.10", "198.51.100.7"),  # what Caddy now forwards
        ("172.30.0.10", "203.0.113.99, 198.51.100.7, 172.64.1.1"),
        ("172.30.0.10", "203.0.113.99, 2a06:98c0:3600::103, 172.64.1.1"),
        ("172.30.0.1", "203.0.113.99"),  # the host via the Docker gateway
    ]
    result = subprocess.run(
        [sys.executable, "-c", _CLIENT_IP_CHILD, value, json.dumps(cases)],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got[0] == "198.51.100.7", got
    assert "203.0.113.99" not in got, f"a caller-written prefix chose the client IP: {got}"
    assert got[3] == "172.30.0.1", got


def operator_safety():
    book = source(KIT + "LAUNCH.md")
    existing = book.split("### Existing host: Caddy backup, swap and rollback", 1)[1].split(
        "### Close the web ports", 1
    )[0]
    for required in (
        "Caddy will not start without",
        "`/etc/caddy/origin-auth.caddy`",
        "Precondition check",
        "grep -i '^cf-ray:'",
        "do not install origin-auth on that host",
        "{{json .Mounts}} {{json .NetworkSettings.Networks}}",
        'sudo cp -a /srv/caddy "$CADDY_BACKUP"',
        'sudo docker inspect caddy > "$HOME/caddy-inspect-$CADDY_STAMP.json"',
        "sudo docker rename caddy caddy-old && sudo docker stop caddy-old",
        '--network "${HOST_NETWORK:?}" --ip "${HOST_IP:?}"',
        'sudo cp -a "$CADDY_BACKUP" /srv/caddy',
        "sudo docker rm -f caddy && sudo docker start caddy-old",
        "trusted_proxy_ips",
    ):
        assert required in existing, required
    assert existing.index("grep -i '^cf-ray:'") < existing.index("sudo cp -a /srv/caddy")
    assert existing.index("caddy validate") < existing.index("sudo docker rename caddy caddy-old")
    assert existing.index('sudo cp -a "$CADDY_BACKUP"') < existing.index("sudo docker rm -f caddy")
    assert "--network locos" not in existing
    assert "before EVERY change under `/srv/caddy`" in book
    assert "if reload reports an error, fix or restore the file" in book
    rotation = book.split("**Rotation without a 403 window.**", 1)[1].split("### Existing host", 1)[
        0
    ]
    assert "@origin header X-Locos-Origin <old> <new>" in rotation
    assert "Values in one header matcher are OR'd" in rotation
    assert rotation.index("caddy validate") < rotation.index("sudo cp -a /srv/caddy-next/.")
    assert rotation.index("dual-value reload") < rotation.index("switch the Cloudflare rule")
    assert rotation.index("confirm public **200**") < rotation.index("remove the old value")
    assert "Never disable authentication to rotate" in rotation
    readme = source(KIT + "README.md")
    assert "demo host: only if it is Cloudflare-proxied" in readme


def snapshot_stub():
    """Apply EC2 filters and real CLI queries to offline snapshot fixtures."""
    rows = json.loads(sys.argv[2])
    args = sys.argv[3:]
    region = args[args.index("--region") + 1]
    if rows == "ERROR" or rows.get(region) == "ERROR":
        print("ERROR")
        return
    rows = rows[region]
    assert args[:2] == ["ec2", "describe-snapshots"], args
    assert args[args.index("--owner-ids") + 1] == "self", args
    for arg in args:
        if not arg.startswith("Name="):
            continue
        name, values = arg[5:].split(",Values=", 1)
        key = {"volume-id": "VolumeId", "status": "State", "description": "Description"}.get(
            name, name
        )
        rows = [
            row
            for row in rows
            if (row["Tags"].get(name[4:]) if name.startswith("tag:") else row[key])
            in values.split(",")
        ]
    query = args[args.index("--query") + 1]
    if jmespath is not None:
        result = jmespath.search(query, {"Snapshots": rows})
    else:
        # Only the two CLI query shapes used here. A changed query must be covered
        # explicitly; installed jmespath and the dry-run pins validate real syntax.
        rows = sorted(rows, key=lambda row: row["StartTime"])
        if query == "sort_by(Snapshots,&StartTime)[].[SnapshotId,StartTime]":
            result = [[row["SnapshotId"], row["StartTime"]] for row in rows]
        else:
            assert query == "sort_by(Snapshots,&StartTime)[-1].StartTime", query
            result = rows[-1]["StartTime"] if rows else None
    if isinstance(result, list):
        for row in result:
            print("\t".join(row) if isinstance(row, list) else row)
    else:
        print("None" if result is None else result)


def snapshots():
    verify = source(KIT + "aws-verify.sh")
    block = verify.split("# 6. Snapshots", 1)[1].split("# 7.", 1)[0]
    block = block[block.index("\n") :]
    errors = []

    def snap(sid, vid="vol-a", age=2, state="completed", policy="policy-a", origin=None):
        tags = {"CreatedBy": "DLM-LocOS", "aws:dlm:lifecycle-policy-id": policy}
        if origin:
            tags["aws:dlm:source-snapshot-id"] = origin
            tags["aws:dlm:source-region"] = "us-east-2"
        return {
            "SnapshotId": sid,
            "VolumeId": vid,
            "State": state,
            "Description": "",
            "StartTime": (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat(),
            "Tags": tags,
        }

    def check(label, sources, copies, fails, vols="vol-a True", pid="policy-a"):
        fixtures = {"us-east-2": sources, "us-west-2": copies}
        command = " ".join(
            shlex.quote(value)
            for value in (sys.executable, __file__, "--snapshot-stub", json.dumps(fixtures))
        )
        out = shell(
            f"""
REGION=us-east-2; COPY_REGION=us-west-2; pid={shlex.quote(pid)}
vols={shlex.quote(vols)}
pass() {{ echo "PASS $*"; }}
fail() {{ echo "FAIL $*"; }}
r() {{ {command} "$@"; }}
"""
            + block
        )
        actual = sum(line.startswith("FAIL ") for line in out.splitlines())
        if actual != fails:
            errors.append(f"{label}: expected {fails} failures, got {actual}\n{out}")

    good = snap("snap-a")
    copy = snap("snap-copy", vid="vol-destination", origin="snap-a")
    check("completed pair", [good], [copy], 0)
    # CopySnapshot default description shape, with no assumed destination DLM tags.
    description_copy = dict(
        copy, Tags={}, Description="Copied snapshot snap-a from region us-east-2"
    )
    check("description lineage without tags", [good], [description_copy], 0)
    check("either lineage shape", [good], [copy, description_copy], 0)
    # Believed EC2 default shape (bracketed); unconfirmed offline, verify on a real install.
    bracketed = dict(description_copy, Description="[Copied snap-a from us-east-2]")
    check("bracketed description lineage", [good], [bracketed], 0)
    for description in (
        "Copied snapshot snap-other from region us-east-2",
        "Copied snapshot snap-a from region us-east-1",
        "Copied snapshot snap-ab from region us-east-2",
        "Copied snapshot snap-a from region us-east-20",
    ):
        check("unrelated description", [good], [dict(description_copy, Description=description)], 1)
    check(
        "description still requires source policy",
        [snap("snap-a", policy="policy-other")],
        [description_copy],
        2,
    )
    check(
        "description per volume",
        [good, snap("snap-b", vid="vol-b")],
        [description_copy],
        1,
        "vol-a True\nvol-b True",
    )
    check(
        "fresh description beats stale tagged copy",
        [good],
        [dict(copy, StartTime=snap("old", age=51)["StartTime"]), description_copy],
        0,
    )
    for state in ("pending", "error"):
        check(f"description copy {state}", [good], [dict(description_copy, State=state)], 1)
        check(f"source {state}", [snap("snap-a", state=state)], [copy], 2)
        check(f"copy {state}", [good], [dict(copy, State=state)], 1)
        check(
            "new unusable source does not hide old backup",
            [snap("snap-old", age=30), snap("snap-a", state=state)],
            [copy],
            2,
        )
        check(
            "completed backup still usable", [good, snap("snap-new", age=1, state=state)], [copy], 0
        )
    check("unrelated volume", [good], [snap("snap-copy", origin="snap-other")], 1)
    check("wrong source policy", [snap("snap-a", policy="policy-other")], [copy], 2)
    check(
        "wrong copy policy", [good], [snap("snap-copy", origin="snap-a", policy="policy-other")], 1
    )
    check("no lineage", [good], [snap("snap-copy")], 1)
    wrong_region = snap("snap-copy", origin="snap-a")
    wrong_region["Tags"]["aws:dlm:source-region"] = "us-east-1"
    check("wrong source region", [good], [wrong_region], 1)
    check(
        "each volume needs a copy",
        [good, snap("snap-b", vid="vol-b")],
        [copy],
        1,
        "vol-a True\nvol-b True",
    )
    check(
        "both volumes protected",
        [good, snap("snap-b", vid="vol-b")],
        [copy, snap("snap-copy-b", origin="snap-b")],
        0,
        "vol-a True\nvol-b True",
    )
    check("stale local", [snap("snap-a", age=27)], [copy], 1)
    check("stale copy", [good], [dict(copy, StartTime=snap("old", age=51)["StartTime"])], 1)
    check(
        "recent copy of stale data",
        [good, snap("snap-old", age=51)],
        [snap("snap-copy", origin="snap-old")],
        1,
    )
    check(
        "previous daily copy allowed",
        [good, snap("snap-old", age=30)],
        [snap("snap-copy", origin="snap-old")],
        0,
    )
    check("no source", [], [copy], 2)
    check("no copy", [good], [], 1)
    check("source API failure", "ERROR", [copy], 2)
    check("copy API failure", [good], "ERROR", 1)
    check("missing policy", [good], [copy], 1, pid="None")
    check("volume discovery failed", [good], [copy], 1, vols="ERROR")
    check("no volumes", [good], [copy], 1, vols="")
    assert not errors, "\n".join(errors)

    # Keep the existing dry-run shim and its operator-output assertions aligned.
    shim = shlex.quote((ROOT / KIT / "test/bin/aws").as_posix())
    for age in (20, 80):
        out = shell(
            f"""
REGION=us-east-2; COPY_REGION=us-west-2; pid=policy-a
vols='vol-0aaa1111bbbb2222c True'
pass() {{ echo "PASS  $*"; }}
fail() {{ echo "FAIL  $*"; }}
r() {{ SHIM_SCENARIO=done SHIM_COPY_AGE_H={age} bash {shim} "$@"; }}
"""
            + block
        )
        expected = (
            f"{'PASS' if age == 20 else 'FAIL'}  newest completed snapshot copy of "
            f"vol-0aaa1111bbbb2222c in us-west-2 is {age} h old"
        )
        assert expected in out, out
        assert expected in source(KIT + "test/run-dry.sh")
    if jmespath is not None:
        checker = runpy.run_path(str(ROOT / KIT / "test/jmespath_check.py"))
        for (operation, query), expected in checker["PINS"].items():
            if operation == "ec2 describe-snapshots":
                assert query in block
                assert [
                    jmespath.search(query, sample) for sample in checker["SAMPLES"][operation]
                ] == expected


def app_backup():
    """GitHub #393: the hosted launch kit must install the promised nightly VERIFIED application
    archive with 35-day expiry, not only give one-off commands. Checks the installer
    (deploy/aws-hosted/host/install-app-backup.sh) statically, then EXECUTES its core
    backup -> verify -> expire decision (run_cycle) against stubs: only I/O (docker exec, prune,
    record) is stubbed; the guard that prunes only after a verified archive runs for real."""
    inst = KIT + "host/install-app-backup.sh"
    try:
        unit = source(inst)
    except (subprocess.CalledProcessError, FileNotFoundError):
        unit = ""
    assert "# BEGIN BACKUP-CORE" in unit, f"installer missing or incomplete: {inst}"

    syntax = subprocess.run(
        [BASH, "-n"], input=unit, capture_output=True, encoding="utf-8", timeout=20
    )
    assert syntax.returncode == 0, syntax.stderr

    for marker in (
        "/usr/local/bin/locos-app-backup",
        "/etc/locos-app-backup.conf",
        "/etc/systemd/system/locos-app-backup.service",
        "/etc/systemd/system/locos-app-backup.timer",
    ):
        assert marker in unit, marker
    assert "OnCalendar=*-*-*" in unit and "Persistent=true" in unit
    assert "NoNewPrivileges=true" in unit and "ProtectSystem=strict" in unit
    # ProtectSystem=strict mounts /var/lib read-only; the backup records its last result in
    # STATE_DIR (/var/lib/locos-app-backup), so the unit must grant that path via StateDirectory
    # or --status (the go-live proof the runbook promises) can never report an OK run.
    assert "StateDirectory=locos-app-backup" in unit, "state dir must be writable under strict"
    assert "/var/lib/locos-app-backup" in unit
    # a --test email must not expire archives (same non-destructive posture as --dry-run)
    assert '[ "$MODE" = dry ] || [ "$MODE" = test ]' in unit, "--test must skip prune"
    # it creates AND verifies the archive inside the container, then expires old ones
    assert "db backup --full" in unit and "db verify-backup" in unit
    assert "RETENTION_DAYS" in unit and "35" in unit and "-mtime" in unit

    # ---- real check: run the extracted decision block with stubbed I/O only ----
    core = unit.split("# BEGIN BACKUP-CORE", 1)[1]
    core = core[core.index("\n") + 1 :]
    core = core.split("# END BACKUP-CORE", 1)[0]
    assert "run_cycle()" in core

    def cycle(backup_rc, verify_rc, prune_rc=0, rm_rc=0):
        stubs = (
            "CONTAINER=locosapp\n"
            "ARCHIVE=/data/backups/loc_tms-test.tar.gz\n"
            f"BACKUP_RC={backup_rc}\n"
            f"VERIFY_RC={verify_rc}\n"
            f"PRUNE_RC={prune_rc}\n"
            f"RM_RC={rm_rc}\n"
            'LOG=""\n'
            "cexec() {\n"
            '  LOG="$LOG\ncexec $*"\n'
            '  case "$*" in\n'
            '    *"db backup --full"*) return $BACKUP_RC ;;\n'
            '    *"db verify-backup"*) return $VERIFY_RC ;;\n'
            '    *"rm -f"*) return $RM_RC ;;\n'
            "    *) return 0 ;;\n"
            "  esac\n"
            "}\n"
            'prune() { LOG="$LOG\n'
            'PRUNE"; return "$PRUNE_RC"; }\n'
            'ok_record() { LOG="$LOG\n'
            'OKRECORD $1"; }\n'
            'fail_record() { LOG="$LOG\n'
            'FAILRECORD $1"; }\n'
        )
        out = shell(
            stubs + core + '\nrun_cycle && rc=0 || rc=$?\necho "rc=$rc"\n'
            'printf "LOGSTART%sLOGEND" "$LOG"\n'
        )
        body = out.split("LOGSTART", 1)[1].split("LOGEND", 1)[0]
        return int(re.search(r"rc=(\d+)", out).group(1)), body

    # A refused destination belongs to someone else: never remove it on backup failure.
    rc, log = cycle(1, 0)
    assert rc == 1 and "FAILRECORD" in log and "rm -f" not in log, log
    assert "PRUNE" not in log and "OKRECORD" not in log, log
    # verify fails on a created archive: remove the bad archive, record, still no prune
    rc, log = cycle(0, 1)
    assert rc == 1 and "FAILRECORD" in log and "rm -f" in log, log
    assert "PRUNE" not in log and "OKRECORD" not in log, log
    assert log.index("db verify-backup") < log.index("rm -f"), log
    assert "it was removed" in log, log
    # verify fails AND the removal fails: the record must not claim the archive is gone
    rc, log = cycle(0, 1, rm_rc=1)
    assert rc == 1 and "could NOT be removed" in log and "it was removed" not in log, log
    assert "PRUNE" not in log and "OKRECORD" not in log, log
    # success: create, verify, THEN expire (prune), record OK
    rc, log = cycle(0, 0)
    assert rc == 0 and "OKRECORD" in log and "PRUNE" in log and "FAILRECORD" not in log, log
    assert log.index("db backup --full") < log.index("db verify-backup"), log
    assert log.index("db verify-backup") < log.index("PRUNE"), log

    # Retention is part of success, even when the new archive verified. Preserve it.
    rc, log = cycle(0, 0, 1)
    assert rc == 1 and "FAILRECORD" in log and "cleanup failed" in log, log
    assert "OKRECORD" not in log and "rm -f" not in log, log

    # the launch kit points operators at the timer and its go-live proof
    book = source(KIT + "LAUNCH.md")
    assert "install-app-backup.sh" in book and "locos-app-backup --status" in book
    assert "Nightly verified application archive" in book
    assert "35 days" in book


def app_backup_runtime():
    """Run the installed script with Docker/SNS I/O faked, including the real prune pipeline."""
    unit = source(KIT + "host/install-app-backup.sh")
    check = unit.split("<<'CHECK'\n", 1)[1].split("\nCHECK\n", 1)[0]
    assert "flock -x 9 ||" in check
    assert check.index("flock -x 9 ||") < check.index("while ! running")
    for scenario in ("cleanup", "collision", "dry", "lock"):
        out = shell(
            """
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export LOCOS_BACKUP_CONF="$work/conf" LOCOS_STATE_DIR="$work/state" LOCOS_WAIT_S=0
printf 'CONTAINER=locosapp\\nDEST_DIR=%s\\n' "$work" > "$work/conf"
printf 'RETENTION_DAYS=35\\nTOPIC_ARN=test-topic\\n' >> "$work/conf"
docker() {
  if [ "$1" = inspect ]; then echo true; return; fi
  shift 2
  case "$*" in
    *"db backup --full"*)
      target="${@: -1}"
      echo "$target" >> "$work/targets"
      if [ "$scenario" = collision ]; then
        echo existing-verified-archive > "$target"
        return 1
      fi
      echo new-archive > "$target" ;;
    *"db verify-backup"*) return 0 ;;
    find*) echo 'find: expired/archive: Permission denied' >&2; return 1 ;;
    rm*) "$@" ;;
    *) return 91 ;;
  esac
}
aws() { echo "MAIL $*" >> "$work/mail"; }
# Git Bash has no flock. Check that lock failure prevents all Docker writes;
# flock's actual OS exclusion is provided by util-linux on the AWS host.
flock() { [ "$scenario" != lock ]; }
date() {
  if [ "${2:-}" = '+%Y%m%dT%H%M%SZ' ]; then echo 20261004T060000Z
  else command date "$@"; fi
}
"""
            + f"scenario={scenario}\n"
            + "for attempt in 1 2; do\nset +e\n(\n"
            + ("set -- --dry-run\n" if scenario == "dry" else "set --\n")
            + check
            + """
) 2>&1
rc=$?
set -e
echo "RC=$rc"
done
if [ -f "$work/state/last" ]; then cat "$work/state/last"; fi
if [ -f "$work/mail" ]; then cat "$work/mail"; fi
if [ -f "$work/targets" ]; then
  echo "UNIQUE=$(sort -u "$work/targets" | wc -l)"
  while IFS= read -r target; do cat "$target"; done < "$work/targets"
fi
"""
        )
        if scenario == "lock":
            assert out.count("RC=2") == 2 and "UNIQUE=" not in out, out
        elif scenario == "cleanup":
            assert out.count("RC=1") == 2 and " OK " not in out, out
            assert "find: expired/archive: Permission denied" in out, out
            assert "MAIL sns publish" in out and "Result: cleanup failed" in out, out
            assert "new-archive" in out and "UNIQUE=2" in out, out
        elif scenario == "collision":
            assert out.count("RC=1") == 2 and "existing-verified-archive" in out, out
            assert "MAIL sns publish" in out and " OK " not in out, out
        else:
            assert out.count("RC=0") == 2 and "UNIQUE=2" in out, out
            assert " OK " in out and "mode=dry" in out and "mode=run" not in out, out
            assert "Permission denied" not in out and "MAIL" not in out, out


# GitHub #405: the installer read --container (an operator-copied CLI argument), validated it
# with a LINE-anchored grep -Eq (a multiline value passed on its first line) and interpolated
# it into a single-quoted sh -c, so a second line broke out of the quotes and EXECUTED, even
# under --dry-run. This runs the REAL installer under real bash: the real validators and the
# real prerequisite check run unmodified, the same offline transport as test/run-dry.sh
# (aws/docker shims in test/bin; the LOCOS_* hooks point writes and systemctl at throwaways).
# LOCOS_DEPLOY_TEST_BASE=HEAD feeds the base installer and MUST be red on it.
RUNNER = r"""
set -u
here="$(mktemp -d)"
# cd + pwd turns a Windows "C:/..." path into "/c/..." so the colon does not split PATH.
shim="$(cd "$LOC405_SHIM" && pwd)" || exit 9
export PATH="$shim:$PATH"
export LOCOS_ROOT="$here/root" LOCOS_SUDO= LOCOS_SYSTEMCTL=true
export SHIM_LOG="$here/calls.log" SHIM_STATE="$here/sg-state"
ARN="arn:aws:sns:us-east-2:123456789012:locos-alerts"
mark="$here/injection-marker"
bad="$(printf "locosapp\n'; : > '%s'; echo '" "$mark")"
runi() { bash "$LOC405_INST" "$@" --dry-run >"$here/out" 2>&1; echo "$?"; }
echo "docker-shim:$([ "$(command -v docker)" = "$shim/docker" ] && echo yes || echo no)"
echo "valid:$(runi --topic-arn "$ARN")"
grep -q "ok: container locosapp is running" "$here/out" \
  && echo "running:ok" || echo "running:missing"
grep -qx "docker inspect -f {{.State.Running}} locosapp" "$SHIM_LOG" \
  && echo "inspect:ok" || echo "inspect:missing"
: > "$SHIM_LOG"
echo "multiline-container:$(runi --topic-arn "$ARN" --container "$(printf 'locosapp\nx')")"
echo "cr-container:$(runi --topic-arn "$ARN" --container "$(printf 'locosapp\rx')")"
echo "multiline-arn:$(runi --topic-arn "$(printf '%s\nx' "$ARN")")"
echo "multiline-time:$(runi --topic-arn "$ARN" --time "$(printf '13:00\nx')")"
echo "inject:$(runi --topic-arn "$ARN" --container "$bad")"
[ -e "$mark" ] && echo "marker:EXECUTED" || echo "marker:none"
[ -e "$LOCOS_ROOT" ] && echo "files:wrote" || echo "files:none"
"""


def container_names():
    src = source(KIT + "host/install-audit-alert.sh")
    if BASE:
        inst = Path(tempfile.mkdtemp(prefix="loc405_")) / "install-audit-alert.sh"
        inst.write_text(src, encoding="utf-8", newline="\n")
    else:
        inst = ROOT / KIT / "host" / "install-audit-alert.sh"
    shim = ROOT / KIT / "test" / "bin"
    for name in ("aws", "docker"):
        (shim / name).chmod(0o755)
    env = os.environ.copy()
    env["LOC405_INST"] = inst.as_posix()
    env["LOC405_SHIM"] = shim.as_posix()
    result = subprocess.run(
        [BASH, "--noprofile", "--norc"],
        input=RUNNER,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=env,
    )
    report = result.stdout + result.stderr
    lines = dict(line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)
    assert lines.get("docker-shim") == "yes", f"the offline docker shim is not on PATH: {report}"
    assert lines.get("valid") == "0", f"a valid install plan must still work: {report}"
    assert lines.get("running") == "ok", f"the container prerequisite did not pass: {report}"
    assert lines.get("inspect") == "ok", f"docker inspect did not get the bare name: {report}"
    for case in (
        "multiline-container",
        "cr-container",
        "multiline-arn",
        "multiline-time",
        "inject",
    ):
        assert lines.get(case) == "2", f"{case} must be refused with exit 2: {report}"
    assert lines.get("marker") == "none", f"the injected second line EXECUTED: {report}"
    assert lines.get("files") == "none", f"the dry run wrote files: {report}"


# GitHub #484: install-app-backup.sh had the same #405 flaw (line-based grep validators on
# --container/--dest/--topic-arn, the container name single-quote interpolated into sh -c, and the
# values written into /etc/locos-app-backup.conf). Real installer, real bash, offline shims.
RUNNER_BACKUP = r"""
set -u
here="$(mktemp -d)"
shim="$(cd "$LOC405_SHIM" && pwd)" || exit 9
export PATH="$shim:$PATH"
export LOCOS_ROOT="$here/root" LOCOS_SUDO= LOCOS_SYSTEMCTL=true
export SHIM_LOG="$here/calls.log" SHIM_STATE="$here/sg-state"
ARN="arn:aws:sns:us-east-2:123456789012:locos-alerts"
mark="$here/injection-marker"
bad="$(printf "locosapp\n'; : > '%s'; echo '" "$mark")"
runi() { bash "$LOC405_INST" "$@" --dry-run >"$here/out" 2>&1; echo "$?"; }
echo "valid:$(runi)"
grep -q "ok: container locosapp is running" "$here/out" \
  && echo "running:ok" || echo "running:missing"
grep -qx "docker inspect -f {{.State.Running}} locosapp" "$SHIM_LOG" \
  && echo "inspect:ok" || echo "inspect:missing"
: > "$SHIM_LOG"
echo "multiline-container:$(runi --container "$(printf 'locosapp\nx')")"
echo "cr-container:$(runi --container "$(printf 'locosapp\rx')")"
echo "multiline-dest:$(runi --dest "$(printf '/data/backups\nDEST_DIR=/')")"
echo "multiline-arn:$(runi --topic-arn "$(printf '%s\nx' "$ARN")")"
echo "multiline-time:$(runi --time "$(printf '06:00\nx')")"
echo "multiline-retention:$(runi --retention-days "$(printf '35\nx')")"
echo "inject:$(runi --container "$bad")"
[ -e "$mark" ] && echo "marker:EXECUTED" || echo "marker:none"
[ -e "$LOCOS_ROOT" ] && echo "files:wrote" || echo "files:none"
"""


def app_backup_inputs():
    src = source(KIT + "host/install-app-backup.sh")
    if BASE:
        inst = Path(tempfile.mkdtemp(prefix="loc484_")) / "install-app-backup.sh"
        inst.write_text(src, encoding="utf-8", newline="\n")
    else:
        inst = ROOT / KIT / "host" / "install-app-backup.sh"
    shim = ROOT / KIT / "test" / "bin"
    for name in ("aws", "docker"):
        (shim / name).chmod(0o755)
    env = os.environ.copy()
    env["LOC405_INST"] = inst.as_posix()
    env["LOC405_SHIM"] = shim.as_posix()
    result = subprocess.run(
        [BASH, "--noprofile", "--norc"],
        input=RUNNER_BACKUP,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=env,
    )
    report = result.stdout + result.stderr
    lines = dict(line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)
    assert lines.get("valid") == "0", f"a valid install plan must still work: {report}"
    assert lines.get("running") == "ok", f"the container prerequisite did not pass: {report}"
    assert lines.get("inspect") == "ok", f"docker inspect did not get the bare name: {report}"
    for case in (
        "multiline-container",
        "cr-container",
        "multiline-dest",
        "multiline-arn",
        "multiline-time",
        "multiline-retention",
        "inject",
    ):
        assert lines.get(case) == "2", f"{case} must be refused with exit 2: {report}"
    assert lines.get("marker") == "none", f"the injected second line EXECUTED: {report}"
    assert lines.get("files") == "none", f"the dry run wrote files: {report}"
    text = source(KIT + "host/install-app-backup.sh")
    assert "'$CONTAINER'" not in text, "container name is single-quote interpolated into sh -c"
    assert "| grep -Eq" not in text.split("<<'CHECK'")[0], "a line-based grep validator is back"


# GitHub #405 also asked to audit the OTHER grep-based CLI validators for the same multiline
# flaw: a value passed to `printf '%s' "$VAR" | grep -Eq '^...$'` is accepted on its first
# line when a second line hides behind it. The instance-id / hostname / email validators were
# converted to whole-string `[[ "$VAR" =~ ... ]]`. This reads each script's source and fails if
# any reverted to the line-based grep form. Red on base (base still had the grep form).
def validators_whole_string():
    def whole(script, var, prefix):
        text = source(KIT + script)
        assert f'"${var}" | grep -Eq' not in text, f"{script}: {prefix} validator uses line grep"
        assert f'[[ "${var}" =~ {prefix}' in text, f"{script}: {prefix} validator not whole-string"

    for script in ("aws-alerting-setup.sh", "aws-instance-setup.sh", "aws-verify.sh"):
        whole(script, "INSTANCE", "^i-")
    whole("aws-alerting-setup.sh", "HOST", "^(")
    whole("aws-alerting-setup.sh", "EMAIL", "^[a-z0-9")
    # The installer must not rebuild the sh -c that single-quote interpolated the container name.
    alert = source(KIT + "host/install-audit-alert.sh")
    assert "'$CONTAINER'" not in alert, "container name is single-quote interpolated into sh -c"
    assert '[[ "$TOPIC_ARN" =~ ^arn:aws:sns' in alert, "the topic ARN validator is not whole-string"


if __name__ == "__main__" and "--snapshot-stub" in sys.argv:
    snapshot_stub()
elif __name__ == "__main__":
    failures = 0
    tests = (
        metadata,
        subscriptions,
        origin,
        cloudflare_ip,
        operator_safety,
        app_backup,
        app_backup_runtime,
        container_names,
        app_backup_inputs,
        validators_whole_string,
        snapshots,
    )
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except (AssertionError, IndexError, ValueError) as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"AWS security regressions: {len(tests) - failures} passed, {failures} failed")
    if not failures:
        print("DEPLOY SECURITY OK")
    raise SystemExit(bool(failures))
