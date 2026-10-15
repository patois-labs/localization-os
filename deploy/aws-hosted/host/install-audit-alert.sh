#!/usr/bin/env bash
# install-audit-alert.sh - run ON the university instance, as ec2-user (it uses sudo).
#
# Installs a once-a-day check of the app's tamper-evident audit log that EMAILS you when the
# chain is broken or the check could not run. No change to the app; no credential is stored
# on the box (it publishes with the instance role, which aws-alerting-setup.sh allows to
# publish to the one locos-alerts topic and nothing else).
#
# Files it writes (all root-owned):
#   /usr/local/bin/locos-audit-alert                 the check
#   /etc/locos-audit-alert.conf                      topic ARN, region, container name
#   /etc/systemd/system/locos-audit-alert.service    runs the check once
#   /etc/systemd/system/locos-audit-alert.timer      daily at --time (UTC), and at the next
#                                                    boot if the box was off at that time
# The check runs `python -m app.cli audit verify` inside the app container:
#   exit 0 and "OK: audit chain verified"  -> fine, nothing is sent
#   "BROKEN: audit chain ..."              -> CRITICAL email (Incident Response Plan, SEV-1)
#   "AUDIT OFF: ..." (audit logging off)   -> WARNING email "audit logging is switched OFF"
#   container not running (after waiting up to 5 minutes, e.g. right after a boot)
#                                          -> WARNING email "container not running"
#   anything else (an error)               -> one retry a minute later, then WARNING email
#                                             "could not run"
# Once a week (--heartbeat-day, default 1 = Monday) a clean run ALSO sends a short "still
# watching" email, so a lost email subscription (someone clicked Unsubscribe) is noticed.
#
# Usage (on the box):
#   bash install-audit-alert.sh --topic-arn arn:aws:sns:us-east-2:123456789012:locos-alerts
#        [--container locosapp] [--time 13:00] [--heartbeat-day 1] [--dry-run] [--yes]
#   --time is UTC (13:00 UTC = 7 or 6 am in Utah). --heartbeat-day is 1 (Monday) to 7
#   (Sunday), or off. Re-running replaces the files in place.
# Safe tests afterwards (see LAUNCH.md section 7):
#   sudo /usr/local/bin/locos-audit-alert --dry-run   runs the check, prints, sends nothing
#   sudo /usr/local/bin/locos-audit-alert --test      runs the check, sends ONE email marked TEST
set -euo pipefail

ORIG_ARGS=("$@")
TOPIC_ARN=""; CONTAINER="locosapp"; AT="13:00"; HEARTBEAT_DAY=1; DRY_RUN=0; YES=0
# Test hooks, used only by test/run-dry.sh (defaults are the real paths and commands).
ROOT="${LOCOS_ROOT:-}"
SUDO="${LOCOS_SUDO-sudo}"
SYSTEMCTL="${LOCOS_SYSTEMCTL:-systemctl}"

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --topic-arn) TOPIC_ARN="${2:-}"; shift 2 ;;
    --container) CONTAINER="${2:?}"; shift 2 ;;
    --time) AT="${2:?}"; shift 2 ;;
    --heartbeat-day) HEARTBEAT_DAY="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
# no valid ARN, time, container name or day contains a CR or LF. Reject them
# up front so a second line can never hide behind a whole-string validator that a line-based
# tool (grep) would have accepted on the first line alone.
for value in "$TOPIC_ARN" "$AT" "$CONTAINER" "$HEARTBEAT_DAY"; do
  case "$value" in *$'\n'*|*$'\r'*) echo "ERROR: option values must not contain newlines or carriage returns" >&2; exit 2 ;; esac
done
# Whole-string Bash regex matches (no pipes through grep: grep is line-based and would
# accept a multiline value whose FIRST line looks valid).
if ! [[ "$TOPIC_ARN" =~ ^arn:aws:sns:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]+$ ]]; then
  echo "ERROR: --topic-arn arn:aws:sns:<region>:<account>:locos-alerts is required" >&2
  echo "       (aws-alerting-setup.sh prints it at the end; SNS > Topics > locos-alerts > ARN)." >&2
  exit 2
fi
[[ "$AT" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "ERROR: --time must be HH:MM (UTC)" >&2; exit 2; }
# The timer adds up to 5 minutes of random delay; from 23:55 on that could move Monday's run
# (and its weekly email) into Tuesday, so those times are refused.
case "$AT" in 23:5[5-9]) echo "ERROR: --time must be 23:54 UTC or earlier (the timer adds up to 5 minutes)" >&2; exit 2 ;; esac
[[ "$CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "ERROR: bad --container name" >&2; exit 2; }
case "$HEARTBEAT_DAY" in [1-7]|off) ;; *) echo "ERROR: --heartbeat-day must be 1 (Monday) to 7, or off" >&2; exit 2 ;; esac
AWS_REGION_NAME="$(printf '%s' "$TOPIC_ARN" | cut -d: -f4)"

if [ "$DRY_RUN" = 0 ] && [ "$YES" = 0 ]; then
  bash "$0" "${ORIG_ARGS[@]}" --dry-run
  echo
  read -r -p "Install the files listed above? Type yes to continue: " answer
  [ "$answer" = "yes" ] || { echo "Stopped. Nothing was changed."; exit 1; }
fi

echo "== Localization OS audit-chain alert: install =="
echo "   topic: $TOPIC_ARN  region: $AWS_REGION_NAME  container: $CONTAINER  daily at: $AT UTC  weekly email day: $HEARTBEAT_DAY  dry-run: $DRY_RUN"

# Prerequisites. In a dry run a missing one is only reported.
problems=0
need() {  # need "description" command...
  local what="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "  ok: $what"
  else echo "  MISSING: $what"; problems=$((problems + 1)); fi
}
# this used to be sh -c with $CONTAINER interpolated inside single quotes, so a
# value with a quote and a newline broke out and ran shell code. The name is a positional
# argument to docker inspect now; the validated value never becomes shell text.
container_running() { $SUDO docker inspect -f '{{.State.Running}}' "$CONTAINER" | grep -qx true; }
need "systemd (systemctl)" command -v "$SYSTEMCTL"
need "AWS CLI (aws)" command -v aws
need "Docker (docker)" command -v docker
need "container $CONTAINER is running" container_running
if [ "$problems" -gt 0 ] && [ "$DRY_RUN" = 0 ]; then
  echo "ERROR: fix what is MISSING above, then run this again. Nothing was changed." >&2
  exit 1
fi

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/conf" <<EOF
# Written by install-audit-alert.sh. Read (not executed) by /usr/local/bin/locos-audit-alert.
TOPIC_ARN=$TOPIC_ARN
AWS_REGION_NAME=$AWS_REGION_NAME
CONTAINER=$CONTAINER
HEARTBEAT_DAY=$HEARTBEAT_DAY
EOF

cat > "$WORK/check" <<'CHECK'
#!/usr/bin/env bash
# locos-audit-alert - daily audit-chain check for Localization OS (installed by
# install-audit-alert.sh; settings in /etc/locos-audit-alert.conf).
#   (no option)  run the check; email if the chain is broken or the check could not run,
#                plus a short "still watching" email on HEARTBEAT_DAY (1 = Monday) when all is well
#   --dry-run    run the check and print what would be emailed; send nothing
#   --test       run the check and send ONE email marked TEST (proves the email path works)
# Exit: 0 chain OK (or test sent), 1 chain broken or check failed, 2 bad setup, 3 email failed.
set -uo pipefail
CONF="${LOCOS_AUDIT_CONF:-/etc/locos-audit-alert.conf}"
MODE=run
case "${1:-}" in
  --dry-run) MODE=dry ;;
  --test) MODE=test ;;
  "") ;;
  *) echo "usage: locos-audit-alert [--dry-run|--test]" >&2; exit 2 ;;
esac
[ -r "$CONF" ] || { echo "ERROR: cannot read $CONF" >&2; exit 2; }
val() { sed -n "s/^$1=//p" "$CONF" | head -1 | tr -d '\r'; }
TOPIC_ARN="$(val TOPIC_ARN)"; REGION="$(val AWS_REGION_NAME)"; CONTAINER="$(val CONTAINER)"
[ -n "$TOPIC_ARN" ] && [ -n "$REGION" ] && [ -n "$CONTAINER" ] || { echo "ERROR: $CONF is incomplete" >&2; exit 2; }
HEARTBEAT_DAY="$(val HEARTBEAT_DAY)"; HEARTBEAT_DAY="${HEARTBEAT_DAY:-off}"
HOST="$(hostname 2>/dev/null || echo unknown-host)"
WHEN="$(date -u '+%Y-%m-%d %H:%M UTC')"
# Test hooks (test/run-dry.sh only): how long to wait for the container, the retry pause, and
# the weekday (1 = Monday ... 7 = Sunday) used for the weekly "still watching" email.
WAIT_S="${LOCOS_WAIT_S:-300}"; POLL_S="${LOCOS_POLL_S:-10}"; RETRY_S="${LOCOS_RETRY_S:-60}"
DOW="${LOCOS_TODAY_DOW:-$(date -u +%u)}"

running() { [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; }
# classify: sets status/rc/full from one run of audit verify. The WHOLE output is searched
# (not only its last lines), so extra log lines after the result never hide it.
classify() {
  full="$(timeout 900 docker exec "$CONTAINER" python -m app.cli audit verify 2>&1)"; rc=$?
  full="$(printf '%s\n' "$full" | tr -d '\r' | LC_ALL=C tr -cd '\11\12\40-\176')"
  if printf '%s\n' "$full" | grep -q '^BROKEN: audit chain'; then status=BROKEN
  elif [ "$rc" = 0 ] && printf '%s\n' "$full" | grep -q '^AUDIT OFF:'; then status="AUDIT OFF"
  elif [ "$rc" = 0 ] && printf '%s\n' "$full" | grep -q '^OK: audit chain verified'; then status=OK
  else status="COULD NOT RUN (exit $rc)"; fi
}

# After a boot (Persistent=true runs a missed check at boot) the container may still be
# starting: in the daily run, wait for it up to WAIT_S seconds before deciding anything.
waited=0
if [ "$MODE" = run ]; then
  while ! running && [ "$waited" -lt "$WAIT_S" ]; do sleep "$POLL_S"; waited=$((waited + POLL_S)); done
fi
if ! running; then
  status="CONTAINER NOT RUNNING"; rc=""
  full="docker reports container $CONTAINER is not running (waited ${waited} s)."
else
  classify
  # One retry for a check that could not run (a slow start, a busy database), never for BROKEN.
  if [ "$MODE" = run ] && [ "$status" != OK ] && [ "$status" != BROKEN ] \
     && [ "$status" != "AUDIT OFF" ]; then
    echo "first attempt: $status; retrying in $RETRY_S s"
    sleep "$RETRY_S"; classify
  fi
fi
# For the email: the result lines first, then the last lines of the output for context.
out="$( { printf '%s\n' "$full" | grep -E '^(OK: audit|BROKEN: audit|AUDIT OFF:|error:)' | head -5; \
          echo '...'; printf '%s\n' "$full" | tail -8; } )"
case "$status" in
  OK)
    subject="LocOS audit log check OK on $HOST"
    first="Nothing to do." ;;
  BROKEN)
    subject="LocOS CRITICAL: audit log chain BROKEN on $HOST"
    first="Treat as SEV-1 by default (deploy/aws-hosted/LAUNCH.md, section 7; your incident response plan). Do NOT restart, redeploy or restore yet: that can destroy evidence. Sign in to /audit, note the first bad row id above, take an EBS snapshot of the volume now, then follow the Incident Response Plan (containment first)." ;;
  "AUDIT OFF")
    subject="LocOS WARNING: audit logging is switched OFF on $HOST"
    first="The chain is intact but nothing new is being recorded. Unless this was a planned change, treat it as a security incident: sign in to /audit and find the audit.logging_disabled row (who and when), then turn logging back on from the host: sudo docker exec $CONTAINER python -m app.cli config set audit_enabled 1 --yes" ;;
  "CONTAINER NOT RUNNING")
    subject="LocOS WARNING: app container $CONTAINER not running on $HOST"
    first="The site is probably down too (a site-down alarm may follow). Check: sudo docker ps -a; sudo docker logs --tail 50 $CONTAINER; start it with sudo docker start $CONTAINER. Then run: sudo /usr/local/bin/locos-audit-alert --dry-run" ;;
  *)
    subject="LocOS WARNING: audit log check could not run on $HOST"
    first="It failed twice, a minute apart. Check the site and: sudo docker ps; sudo docker logs --tail 50 $CONTAINER. Then run: sudo /usr/local/bin/locos-audit-alert --dry-run" ;;
esac
heartbeat=0
if [ "$MODE" = run ] && [ "$status" = OK ] && [ "$HEARTBEAT_DAY" = "$DOW" ]; then
  heartbeat=1
  subject="LocOS weekly: after-hours alerting is watching $HOST (audit log OK)"
  first="Nothing to do. This weekly email proves the email path still works. If a week passes without it, the subscription may be gone: run aws-verify.sh (LAUNCH.md section 7)."
fi
[ "$MODE" = test ] && subject="LocOS TEST (ignore): audit alert email works on $HOST, chain $status"
subject="$(printf '%s' "$subject" | cut -c1-99)"
message="Localization OS daily audit-chain check
Host: $HOST
Time: $WHEN
Result: $status
Container: $CONTAINER

Output of python -m app.cli audit verify:
$out

What to do first: $first

Runbook: deploy/aws-hosted/LAUNCH.md, section 7 (When an alert fires).
Re-run by hand on the box: sudo /usr/local/bin/locos-audit-alert --dry-run"
[ "$MODE" = test ] && message="THIS IS A TEST sent with --test. No action needed.

$message"

echo "audit chain check: $status"
if [ "$MODE" = dry ]; then
  echo "--- would email (dry run: nothing sent) ---"
  if [ "$status" = OK ]; then echo "(nothing: the chain is OK, so the daily run sends no email)"
  else printf 'Subject: %s\n\n%s\n' "$subject" "$message"; fi
  [ "$status" = OK ]; exit $?
fi
if [ "$status" = OK ] && [ "$MODE" = run ] && [ "$heartbeat" = 0 ]; then exit 0; fi
if ! aws sns publish --region "$REGION" --topic-arn "$TOPIC_ARN" --subject "$subject" \
     --message "$message" --output text >/dev/null; then
  echo "ERROR: could not send the email (aws sns publish failed; see above). Check the instance role has the LocOS-Alerts-Publish policy (aws-alerting-setup.sh)." >&2
  exit 3
fi
echo "email sent to the topic: $subject"
if [ "$MODE" = test ] || [ "$status" = OK ]; then exit 0; fi
exit 1
CHECK

cat > "$WORK/service" <<EOF
[Unit]
Description=Localization OS daily audit-chain check (emails on failure)
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=HOME=/root
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ExecStart=/usr/local/bin/locos-audit-alert
EOF

cat > "$WORK/timer" <<EOF
[Unit]
Description=Run the Localization OS audit-chain check daily at $AT UTC

[Timer]
OnCalendar=*-*-* $AT:00 UTC
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

# place SRC DEST MODE: install a file as root, reporting whether it changed.
place() {
  local src="$1" dest="$ROOT$2" mode="$3"
  if [ -f "$dest" ] && cmp -s "$src" "$dest"; then echo "  unchanged: $2"; return; fi
  if [ "$DRY_RUN" = 1 ]; then
    echo "  [dry-run] would write $2 (mode $mode):"; sed 's/^/    | /' "$src"; return
  fi
  $SUDO mkdir -p "$(dirname "$dest")"
  if [ -n "$SUDO" ]; then $SUDO install -m "$mode" -o root -g root "$src" "$dest"
  else install -m "$mode" "$src" "$dest"; fi
  echo "  wrote: $2"
}
place "$WORK/check" /usr/local/bin/locos-audit-alert 0755
place "$WORK/conf" /etc/locos-audit-alert.conf 0644
place "$WORK/service" /etc/systemd/system/locos-audit-alert.service 0644
place "$WORK/timer" /etc/systemd/system/locos-audit-alert.timer 0644

if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] would run: $SUDO $SYSTEMCTL daemon-reload"
  echo "  [dry-run] would run: $SUDO $SYSTEMCTL enable --now locos-audit-alert.timer"
  echo "Dry run only: nothing was changed."
  exit 0
fi
$SUDO "$SYSTEMCTL" daemon-reload
$SUDO "$SYSTEMCTL" enable --now locos-audit-alert.timer
$SUDO "$SYSTEMCTL" list-timers locos-audit-alert.timer --no-pager || true
echo
echo "Installed. Now test it (both are safe):"
echo "  sudo /usr/local/bin/locos-audit-alert --dry-run    (should print: audit chain check: OK)"
echo "  sudo /usr/local/bin/locos-audit-alert --test       (you should get one TEST email)"
