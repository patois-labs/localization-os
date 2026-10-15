#!/usr/bin/env bash
# install-app-backup.sh - run ON the university instance, as ec2-user (it uses sudo).
#
# Installs a once-a-night backup of the WHOLE app layer (database + uploads + keyfiles) that
# creates a verified full-install archive and expires successful archives after 35 days. It
# closes the gap where a hosted instance installed DLM EBS snapshots but only ever ran the
# application-layer backup by hand, so the nightly verified archive and its 35-day expiry that
# the backup/retention policies promise were never actually scheduled. No change to the app;
# it drives the same host-only commands the operator would run:
#   python -m app.cli db backup --full <archive>     create the whole-install archive
#   python -m app.cli db verify-backup <archive>     prove it is restorable (exit 0)
# A failed archive is removed (a bad archive is no backup), the failure is recorded, and - when
# a topic ARN is configured - emailed to the same locos-alerts topic install-audit-alert.sh uses.
# A successful run then expires successful archives older than RETENTION_DAYS (35) so local
# archives do not pile up forever on the encrypted disk.
#
# The archive lives inside the container at /data/backups, which is the host's /srv/loc-data on
# the encrypted volume, so each nightly archive is also carried inside the 07:00 UTC DLM
# snapshot and its us-west-2 cross-region copy. The timer runs BEFORE the snapshot (default
# 06:00 UTC) so the newest archive is already on disk when the snapshot is taken.
#
# Files it writes (all root-owned):
#   /usr/local/bin/locos-app-backup                  the nightly backup
#   /etc/locos-app-backup.conf                       container, dest dir, retention, topic ARN
#   /var/lib/locos-app-backup/last                   last result line (for --status and audits)
#   /etc/systemd/system/locos-app-backup.service     runs the backup once
#   /etc/systemd/system/locos-app-backup.timer       nightly at --time (UTC), and at the next
#                                                    boot if the box was off at that time
# Usage (on the box):
#   bash install-app-backup.sh [--container locosapp] [--dest /data/backups]
#        [--retention-days 35] [--topic-arn arn:aws:sns:us-east-2:123456789012:locos-alerts]
#        [--time 06:00] [--dry-run] [--yes]
#   --time is UTC (06:00 UTC = midnight or 1 am in Utah, before the 07:00 UTC snapshot).
#   --topic-arn is optional; without it a failure is recorded on the box but not emailed.
#   Re-running replaces the files in place.
# Safe tests afterwards (see LAUNCH.md section 6):
#   sudo /usr/local/bin/locos-app-backup --dry-run   run a full cycle, print it, keep everything
#   sudo /usr/local/bin/locos-app-backup --status     print the last recorded result
set -euo pipefail

ORIG_ARGS=("$@")
CONTAINER="locosapp"; DEST="/data/backups"; RETENTION_DAYS=35
TOPIC_ARN=""; AT="06:00"; DRY_RUN=0; YES=0
# Test hooks, used only by tests/verify_deploy_security.py (defaults are the real paths/commands).
ROOT="${LOCOS_ROOT:-}"
SUDO="${LOCOS_SUDO-sudo}"
SYSTEMCTL="${LOCOS_SYSTEMCTL:-systemctl}"

usage() { sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --container) CONTAINER="${2:?}"; shift 2 ;;
    --dest) DEST="${2:?}"; shift 2 ;;
    --retention-days) RETENTION_DAYS="${2:?}"; shift 2 ;;
    --topic-arn) TOPIC_ARN="${2:-}"; shift 2 ;;
    --time) AT="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
# no valid value contains a CR or LF. Reject them up front, then validate with
# whole-string Bash regex matches (grep is line-based and would accept a multiline value whose
# FIRST line looks valid; the extra lines would then reach the shell and the conf file).
for value in "$CONTAINER" "$DEST" "$RETENTION_DAYS" "$TOPIC_ARN" "$AT"; do
  case "$value" in *$'\n'*|*$'\r'*) echo "ERROR: option values must not contain newlines or carriage returns" >&2; exit 2 ;; esac
done
[[ "$CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "ERROR: bad --container name" >&2; exit 2; }
[[ "$DEST" =~ ^/[A-Za-z0-9._/-]*$ ]] || { echo "ERROR: --dest must be an absolute path (no shell metacharacters)" >&2; exit 2; }
[[ "$RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: --retention-days must be a positive integer" >&2; exit 2; }
[ "$RETENTION_DAYS" -ge 2 ] || { echo "ERROR: --retention-days must be 2 or more (keep more than the newest archive)" >&2; exit 2; }
[[ "$AT" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "ERROR: --time must be HH:MM (UTC)" >&2; exit 2; }
# The timer adds up to 5 minutes of random delay; keep the window clear of midnight.
case "$AT" in 23:5[5-9]) echo "ERROR: --time must be 23:54 UTC or earlier (the timer adds up to 5 minutes)" >&2; exit 2 ;; esac
AWS_REGION_NAME="us-east-2"
if [ -n "$TOPIC_ARN" ]; then
  if ! [[ "$TOPIC_ARN" =~ ^arn:aws:sns:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]+$ ]]; then
    echo "ERROR: --topic-arn must be arn:aws:sns:<region>:<account>:locos-alerts (or omit it)" >&2
    exit 2
  fi
  AWS_REGION_NAME="$(printf '%s' "$TOPIC_ARN" | cut -d: -f4)"
fi

if [ "$DRY_RUN" = 0 ] && [ "$YES" = 0 ]; then
  bash "$0" "${ORIG_ARGS[@]}" --dry-run
  echo
  read -r -p "Install the files listed above? Type yes to continue: " answer
  [ "$answer" = "yes" ] || { echo "Stopped. Nothing was changed."; exit 1; }
fi

echo "== Localization OS nightly app backup: install =="
echo "   container: $CONTAINER  dest: $DEST  retention: $RETENTION_DAYS days  nightly at: $AT UTC  topic: ${TOPIC_ARN:-<none, record only>}  dry-run: $DRY_RUN"

# Prerequisites. In a dry run a missing one is only reported.
problems=0
need() {  # need "description" command...
  local what="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "  ok: $what"
  else echo "  MISSING: $what"; problems=$((problems + 1)); fi
}
need "systemd (systemctl)" command -v "$SYSTEMCTL"
need "Docker (docker)" command -v docker
need "execution lock (flock)" command -v flock
# the name is a positional argument to docker inspect; the validated value never
# becomes shell text.
container_running() { $SUDO docker inspect -f '{{.State.Running}}' "$CONTAINER" | grep -qx true; }
need "container $CONTAINER is running" container_running
if [ -n "$TOPIC_ARN" ]; then need "AWS CLI (aws)" command -v aws; fi
if [ "$problems" -gt 0 ] && [ "$DRY_RUN" = 0 ]; then
  echo "ERROR: fix what is MISSING above, then run this again. Nothing was changed." >&2
  exit 1
fi

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/conf" <<EOF
# Written by install-app-backup.sh. Read (not executed) by /usr/local/bin/locos-app-backup.
CONTAINER=$CONTAINER
DEST_DIR=$DEST
RETENTION_DAYS=$RETENTION_DAYS
TOPIC_ARN=$TOPIC_ARN
AWS_REGION_NAME=$AWS_REGION_NAME
EOF

cat > "$WORK/check" <<'CHECK'
#!/usr/bin/env bash
# locos-app-backup - nightly whole-install app backup for Localization OS (installed by
# install-app-backup.sh; settings in /etc/locos-app-backup.conf).
#   (no option)  create a full-install archive, verify it, expire old successful archives;
#                on any failure record it and (if a topic is set) email the alert
#   --dry-run    run the cycle and print what would happen; keep every archive it makes
#   --status     print the last recorded result and exit 0 (1 if there is no record yet)
#   --test       create + verify one archive and email a TEST (proves the email path works)
# Exit: 0 archive created, verified and retention completed (or deliberately skipped),
#       1 backup / verify / cleanup failed or container not running,
#       2 bad setup, 3 email failed.
set -uo pipefail
CONF="${LOCOS_BACKUP_CONF:-/etc/locos-app-backup.conf}"
MODE=run
case "${1:-}" in
  --dry-run) MODE=dry ;;
  --status) MODE=status ;;
  --test) MODE=test ;;
  "") ;;
  *) echo "usage: locos-app-backup [--dry-run|--status|--test]" >&2; exit 2 ;;
esac
[ -r "$CONF" ] || { echo "ERROR: cannot read $CONF" >&2; exit 2; }
val() { sed -n "s/^$1=//p" "$CONF" | head -1 | tr -d '\r'; }
CONTAINER="$(val CONTAINER)"; DEST_DIR="$(val DEST_DIR)"
RETENTION_DAYS="$(val RETENTION_DAYS)"; TOPIC_ARN="$(val TOPIC_ARN)"
REGION="$(val AWS_REGION_NAME)"; REGION="${REGION:-us-east-2}"
[ -n "$CONTAINER" ] && [ -n "$DEST_DIR" ] || { echo "ERROR: $CONF is incomplete" >&2; exit 2; }
RETENTION_DAYS="${RETENTION_DAYS:-35}"
STATE_DIR="${LOCOS_STATE_DIR:-/var/lib/locos-app-backup}"
STATE_FILE="$STATE_DIR/last"
HOST="$(hostname 2>/dev/null || echo unknown-host)"
WHEN="$(date -u '+%Y-%m-%d %H:%M UTC')"
# Test hooks (tests/verify_deploy_security.py only): how long to wait for the container after a
# boot, and the poll interval. The real check waits up to 5 minutes for the app container.
WAIT_S="${LOCOS_WAIT_S:-300}"; POLL_S="${LOCOS_POLL_S:-10}"

running() { [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; }
cexec() { docker exec "$@"; }

# prune: delete successful archives older than RETENTION_DAYS inside DEST_DIR. Called ONLY
# after the newest archive verified, so an unverified (or absent) archive never prunes a
# good set. find matches whole 24-hour periods, so +N keeps files newer than (N+1) days:
# RETENTION_DAYS - 1 keeps the newest RETENTION_DAYS days and drops anything older.
prune() {
  # A dry run never expires anything: it only proves that a fresh archive can be created and
  # verified. run_cycle still CALLS prune on a success, so the "expire only after a verified
  # archive" ordering is exercised by the real check.
  if [ "${PRUNE:-1}" != 1 ]; then
    echo "dry run: would expire successful archives older than $RETENTION_DAYS days (kept them)"
    return 0
  fi
  local keep=$(( RETENTION_DAYS - 1 ))
  cexec "$CONTAINER" find "$DEST_DIR" -type f -name 'loc_tms-*.tar.gz' \
    -mtime "+$keep" -print -delete | sed 's/^/expired: /'
}

# BEGIN BACKUP-CORE (tests/verify_deploy_security.py extracts run_cycle and executes it with
# stubbed cexec/prune/ok_record/fail_record; the decision logic below is the real code path).
run_cycle() {
  # $ARCHIVE is the timestamped target inside the container. It never prunes unless the new
  # archive is created AND verifies, so a bad run can never delete a good backup.
  if ! cexec "$CONTAINER" python -m app.cli db backup --full "$ARCHIVE"; then
    # The CLI cleans its own .partial file. A refused destination may be an existing
    # verified archive, so this invocation has no right to remove it.
    fail_record "backup failed (db backup --full did not complete)"
    return 1
  fi
  if ! cexec "$CONTAINER" python -m app.cli db verify-backup "$ARCHIVE"; then
    if cexec "$CONTAINER" rm -f "$ARCHIVE" >/dev/null 2>&1; then
      fail_record "new archive failed verification; it was removed (a bad archive is no backup)"
    else
      fail_record "new archive failed verification and could NOT be removed; do not restore from it, delete it by hand: $ARCHIVE"
    fi
    return 1
  fi
  if ! prune; then
    fail_record "cleanup failed; new archive verified and retained: $ARCHIVE (see journal for diagnostics)"
    return 1
  fi
  ok_record "$ARCHIVE"
  return 0
}
# END BACKUP-CORE

record() {  # record RESULT detail   -> stdout, the journal, and the last-status file
  local line
  RESULT_DETAIL="$2"
  line="$(printf '%s %s %s' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "$2")"
  printf '%s\n' "$line"
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  printf '%s\n' "$line" > "$STATE_FILE" 2>/dev/null || true
}
# The mode tag tells a scheduled run (mode=run) from a --dry-run or --test in --status, so the
# go-live proof cannot be satisfied by a manual dry run alone.
ok_record() { record "OK" "$1 mode=$MODE"; }
fail_record() { record "FAIL" "$1"; }

send_mail() {  # send_mail SUBJECT BODY  -> nothing if no topic; exit 3 if sns publish fails
  [ -n "$TOPIC_ARN" ] || return 0
  aws sns publish --region "$REGION" --topic-arn "$TOPIC_ARN" --subject "$1" \
    --message "$2" --output text >/dev/null
}

if [ "$MODE" = status ]; then
  if [ -r "$STATE_FILE" ]; then cat "$STATE_FILE"; exit 0; fi
  echo "no app-backup run has been recorded yet ($STATE_FILE)"; exit 1
fi

# Serialize timer, manual, dry-run and test executions, including retention and alerts.
# Keep the lock inode in place; closing the descriptor releases it even after a crash.
mkdir -p "$STATE_DIR" && exec 9>"$STATE_DIR/lock" || exit 2
flock -x 9 || { echo "ERROR: could not lock app backup" >&2; exit 2; }

# After a boot (Persistent=true runs a missed backup at boot) the container may still be
# starting: wait for it up to WAIT_S seconds before doing anything.
waited=0
while ! running && [ "$waited" -lt "$WAIT_S" ]; do sleep "$POLL_S"; waited=$((waited + POLL_S)); done
if ! running; then
  msg="Localization OS nightly app backup could not run on $HOST
Time: $WHEN
Result: app container $CONTAINER is not running (waited ${waited} s).

The nightly verified archive did not run. Check: sudo docker ps -a; sudo docker logs --tail 50 $CONTAINER;
then start it with sudo docker start $CONTAINER and re-run: sudo /usr/local/bin/locos-app-backup --dry-run

Runbook: deploy/aws-hosted/LAUNCH.md, section 6."
  fail_record "container $CONTAINER not running after ${waited} s"
  if ! send_mail "LocOS WARNING: nightly app backup could not run on $HOST" "$msg"; then
    echo "ERROR: could not send the email (aws sns publish failed)." >&2; exit 3
  fi
  exit 1
fi

STAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
# Reserve a unique run name even when two serialized runs start in the same second.
RUN_DIR="$(mktemp -d "$STATE_DIR/run-XXXXXXXXXXXX")" || exit 2
trap 'rmdir "$RUN_DIR"' EXIT
ARCHIVE="$DEST_DIR/loc_tms-$STAMP-${RUN_DIR##*/}.tar.gz"
PRUNE=1
if [ "$MODE" = dry ] || [ "$MODE" = test ]; then
  # Neither a dry run nor a --test email is meant to delete anything: only a plain nightly run
  # expires old archives. A --test proves the create + verify + email path, nothing more.
  PRUNE=0
fi
if [ "$MODE" = dry ]; then
  echo "would create and verify: $ARCHIVE (a dry run keeps every existing archive)"
fi

run_cycle
rc=$?

if [ "$MODE" = dry ]; then
  if [ "$rc" = 0 ]; then
    echo "dry run: the archive verified and old archives were NOT expired (dry run keeps them)"
  else
    echo "dry run: the cycle reported a failure (a real run would record it and email the topic)"
  fi
  exit "$rc"
fi

if [ "$rc" = 0 ]; then
  if [ "$MODE" = test ]; then
    if send_mail "LocOS TEST (ignore): nightly app backup path works on $HOST" \
      "THIS IS A TEST sent with --test. No action needed. A fresh archive was created and verified on $HOST at $WHEN."; then
      echo "test email sent (or no topic configured)"
      exit 0
    else
      echo "ERROR: could not send the test email (aws sns publish failed)." >&2
      exit 3
    fi
  fi
  exit 0
fi

subject="LocOS CRITICAL: nightly app backup FAILED on $HOST"
first="The backup cycle did not complete. Check the recorded result and sudo journalctl -u locos-app-backup.service for diagnostics. If cleanup failed, the new verified archive was kept; repair retention and re-run the normal job (a dry run skips cleanup). If creation or verification failed, do NOT count on this run for a restore. The EBS snapshots (Phase 3) and their us-west-2 copy are an independent layer and are unaffected."
body="Localization OS nightly app backup
Host: $HOST
Time: $WHEN
Target archive: $ARCHIVE
Retention: keep $RETENTION_DAYS days
Result: $RESULT_DETAIL

What to do first: $first

Runbook: deploy/aws-hosted/LAUNCH.md, section 6 (Backups).
Re-run by hand on the box: sudo /usr/local/bin/locos-app-backup --dry-run"
if ! send_mail "$subject" "$body"; then
  echo "ERROR: could not send the email (aws sns publish failed; check the LocOS-Alerts-Publish policy)." >&2
  exit 3
fi
exit 1
CHECK

cat > "$WORK/service" <<EOF
[Unit]
Description=Localization OS nightly whole-install app backup (verified, expires old archives)
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
# ProtectSystem=strict makes /var/lib read-only; the backup records its last result in
# STATE_DIR (/var/lib/locos-app-backup), so declare it writable or --status never shows a run.
StateDirectory=locos-app-backup
ExecStart=/usr/local/bin/locos-app-backup
EOF

cat > "$WORK/timer" <<EOF
[Unit]
Description=Run the Localization OS nightly app backup daily at $AT UTC (before the 07:00 snapshot)

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
place "$WORK/check" /usr/local/bin/locos-app-backup 0755
place "$WORK/conf" /etc/locos-app-backup.conf 0644
place "$WORK/service" /etc/systemd/system/locos-app-backup.service 0644
place "$WORK/timer" /etc/systemd/system/locos-app-backup.timer 0644

if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] would run: $SUDO $SYSTEMCTL daemon-reload"
  echo "  [dry-run] would run: $SUDO $SYSTEMCTL enable --now locos-app-backup.timer"
  echo "Dry run only: nothing was changed."
  exit 0
fi
$SUDO "$SYSTEMCTL" daemon-reload
$SUDO "$SYSTEMCTL" enable --now locos-app-backup.timer
$SUDO "$SYSTEMCTL" list-timers locos-app-backup.timer --no-pager || true
echo
echo "Installed. Now test it (both are safe):"
echo "  sudo /usr/local/bin/locos-app-backup --dry-run    (creates and verifies one archive, keeps it)"
echo "  sudo /usr/local/bin/locos-app-backup --status     (prints the last recorded result)"
