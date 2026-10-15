#!/bin/sh
# Quiesced single-node nightly checkpoint. BACKUP_DIR must be encrypted off-host storage.
#
# Downtime: this job STOPS the app for the whole backup (docker compose stop app), so the
# install is offline until the archive is written -- a window that GROWS with the uploads
# tree, since a full-install archive copies every uploaded file. On SQLite the DB copy is a
# fast online snapshot; the wall-clock cost is dominated by the uploads tree.
#
# Retention: keep the newest KEEP verified archives (KEEP default 14 = ~2 weeks at one per
# night, minimum 1); older nightly-*.tar.gz are pruned ONLY after this run's verify PASSED,
# so a bad run never deletes a good older copy. An archive is written under a temporary
# name and renamed to nightly-*.tar.gz only AFTER its verify PASSED, so nightly-*.tar.gz in
# BACKUP_DIR means "verified" and a failed or partial archive never counts toward KEEP. A
# FAILED archive is deleted; an UNVERIFIED one is kept as unverified-*.tar.gz (newest KEEP
# are retained, and they never displace a verified nightly copy).
#
# Alerting: ALERT_CMD (optional) is invoked with a one-line message as its argument on ANY
# nonzero exit -- a setup error (APP_DIR/BACKUP_DIR unusable, KEEP invalid, the app could
# not be stopped), a backup error, a FAILED verify (corruption), an UNVERIFIED verify (could
# not validate, e.g. pg_restore absent), a retention error or a failed app restart.
# Exit codes: 1 setup or backup error, 2 FAILED verify, 3 UNVERIFIED verify, 4 retention
# error. Configure the scheduler to page on any of them.
#
# RPO/RTO: one verified archive per night puts the worst-case data loss (RPO) at ~24 h;
# restore time (RTO) is dominated by re-extracting the uploads tree.
set -eu

# --- alerting and exit handling FIRST: everything below may fail, and every failure must
# page. (Under `set -e` an early failure used to abort before any alert path existed.)
ALERT_CMD="${ALERT_CMD:-}"
alerted=0
stopped=0
stage="starting"
partial=""

alert() {
  # Best-effort operator alert hook: run ALERT_CMD with the message as $1.
  alerted=1
  [ -n "$ALERT_CMD" ] && sh -c "$ALERT_CMD \"\$1\"" _ "$1" || true
}

fail() {
  # fail <exit code> <message>: alert, then leave (the EXIT trap restarts the app).
  alert "$2"
  exit "$1"
}

on_exit() {
  rc=$?
  trap - EXIT
  # Restart the SAME stopped container (its scanned image, #385). `up` could recreate it from
  # whatever image the scheduler's environment resolves, which the scan gate never saw.
  if [ "$stopped" = 1 ]; then
    docker compose start app || {
      alert "nightly backup: the app could NOT be restarted (docker compose start app failed)"
      [ "$rc" -ne 0 ] || rc=1
    }
  fi
  # A partial archive is never left behind to look like a copy.
  [ -z "$partial" ] || rm -f -- "$partial"
  if [ "$rc" -ne 0 ] && [ "$alerted" = 0 ]; then
    alert "nightly backup exited $rc during: $stage"
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

stage="checking settings"
[ -n "${APP_DIR:-}" ] || fail 1 "nightly backup: APP_DIR is not set (the deployment checkout)"
[ -n "${BACKUP_DIR:-}" ] || fail 1 "nightly backup: BACKUP_DIR is not set (encrypted off-host dir)"
KEEP="${KEEP:-14}"
case "$KEEP" in
  '' | *[!0-9]*) fail 1 "nightly backup: KEEP must be a whole number >= 1 (got '$KEEP')" ;;
esac
[ "$KEEP" -ge 1 ] || fail 1 "nightly backup: KEEP must be >= 1 (got $KEEP): 0 would delete the archive just verified"

stage="entering APP_DIR $APP_DIR"
cd "$APP_DIR" || fail 1 "nightly backup: cannot cd to APP_DIR $APP_DIR"
stage="checking BACKUP_DIR $BACKUP_DIR"
[ -d "$BACKUP_DIR" ] || fail 1 "nightly backup: BACKUP_DIR $BACKUP_DIR is not a directory (is the off-host mount up?)"
[ -w "$BACKUP_DIR" ] || fail 1 "nightly backup: BACKUP_DIR $BACKUP_DIR is not writable"
umask 077

archive="nightly-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
building="$archive.partial"
partial="$BACKUP_DIR/$building"

# Operators must also stop external writers and block ingress before this job.
stage="stopping the app"
stopped=1 # try a restart even if the stop failed half way
docker compose stop app || fail 1 "nightly backup: docker compose stop app FAILED"

# Override the migration entrypoint: a backup must not change the schema.
stage="creating $archive"
if ! docker compose run --rm --no-deps --entrypoint python \
  -v "$BACKUP_DIR:/checkpoint" app -m app.cli db backup-full \
  "/checkpoint/$building"; then
  fail 1 "nightly backup FAILED to create $archive"
fi
[ -s "$partial" ] || fail 1 "nightly backup FAILED to create $archive (no archive was written)"

# Verify the stored archive. Distinguish UNVERIFIED (could not validate) from FAILED
# (corruption) so the alert says which.
stage="verifying $archive"
verify_rc=0
verify_json="$(docker compose run --rm --no-deps --entrypoint python \
  -v "$BACKUP_DIR:/checkpoint" app -m app.cli --json db verify-backup \
  "/checkpoint/$building")" || verify_rc=$?
if printf '%s' "$verify_json" | grep -q '"verification": "PASSED"'; then
  : # verified; promote it below
elif printf '%s' "$verify_json" | grep -q '"verification": "UNVERIFIED"'; then
  # Keep it, but outside the verified set: it must not count toward (or displace) KEEP.
  mv -- "$partial" "$BACKUP_DIR/unverified-$archive" && partial=""
  ls -1t "$BACKUP_DIR"/unverified-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" |
    while IFS= read -r old; do rm -f -- "$old"; done
  alert "nightly backup $archive is UNVERIFIED (could not validate; e.g. pg_restore absent)"
  exit 3
else
  # The partial is deleted by the EXIT trap, so a corrupt archive can never be counted.
  alert "nightly backup $archive FAILED verification (corruption, or verify exited $verify_rc with no verdict) -- do NOT rely on it"
  exit 2
fi

# Promote: only a verified archive ever carries the nightly-* name.
stage="promoting $archive"
mv -- "$partial" "$BACKUP_DIR/$archive" || fail 1 "nightly backup: could not rename the verified archive into place"
partial=""

# Retention: keep the newest KEEP verified archives, prune the rest. Runs ONLY after a PASSED
# verify above, so a bad run never prunes a good older copy, and nightly-*.tar.gz holds only
# verified archives, so a failed one never counts as a kept copy.
stage="pruning old archives"
if ! ls -1t "$BACKUP_DIR"/nightly-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" |
  {
    status=0
    while IFS= read -r old; do rm -f -- "$old" || status=1; done
    exit "$status"
  }; then
  alert "nightly backup $archive verified, but retention could not delete some old archives"
  exit 4
fi
