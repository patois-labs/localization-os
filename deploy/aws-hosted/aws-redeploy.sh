#!/usr/bin/env bash
# aws-redeploy.sh - build (optional), SCAN, then switch the app container.
#
# The scan is the gate: scripts/scan_image.sh (Trivy, digest-pinned, run
# as a container) scans the exact image AFTER the build and BEFORE any
# container is stopped or renamed. A failing scan stops the redeploy. Only the
# operator may continue past it with --accept-scan-risk "<reason>": the reason
# is REQUIRED, is logged, and must be written into your change log.
# See deploy/aws-hosted/LAUNCH.md, sections 3 and 5.
#
# Usage:
#   bash aws-redeploy.sh --image localizationos:univ2 [options]
#     --image TAG                  the built image to serve (required)
#     --build-dir DIR              build the image first from this checkout;
#                                  requires --release and --release-commit
#     --release R                  release identity build arg (with --build-dir)
#     --release-commit C           commit build arg (with --build-dir)
#     --with-saml                  add --build-arg WITH_SAML=1 to the build
#     --container NAME             the app container (default locosapp)
#     --network NAME               docker network (default locos)
#     --publish SPEC               port publish (default 127.0.0.1:8000:8000)
#     --scan-script PATH           the scan script (default: scripts/scan_image.sh
#                                  next to --build-dir, else next to this kit)
#     --accept-scan-risk "reason"  continue past a failing scan; reason required
#
# The stopped data backup and <container>-old are retained for rollback.
# Runtime env overrides (including secrets) and bind mounts are carried over.
# Network/publish use the options above; restart and log caps use kit defaults.
# Startup failure restores data before restarting the old container. For a later
# manual rollback, stop the new container and restore the printed backup first.

set -euo pipefail
export MSYS_NO_PATHCONV=1

IMAGE=""; BUILD_DIR=""; RELEASE=""; RELEASE_COMMIT=""; WITH_SAML=0
CONTAINER="locosapp"; NETWORK="locos"; PUBLISH="127.0.0.1:8000:8000"
SCAN_SCRIPT=""; ACCEPT_RISK=""
KIT_DIR="$(cd "$(dirname "$0")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:?--image needs a value}"; shift 2 ;;
    --build-dir) BUILD_DIR="${2:?}"; shift 2 ;;
    --release) RELEASE="${2:?}"; shift 2 ;;
    --release-commit) RELEASE_COMMIT="${2:?}"; shift 2 ;;
    --with-saml) WITH_SAML=1; shift ;;
    --container) CONTAINER="${2:?}"; shift 2 ;;
    --network) NETWORK="${2:?}"; shift 2 ;;
    --publish) PUBLISH="${2:?}"; shift 2 ;;
    --scan-script) SCAN_SCRIPT="${2:?}"; shift 2 ;;
    --accept-scan-risk) ACCEPT_RISK="${2:?--accept-scan-risk needs a reason: what the scan found, why it is accepted, who approved}"
                        if [[ -z "${ACCEPT_RISK//[[:space:]]/}" || "$ACCEPT_RISK" == --* || "$ACCEPT_RISK" == *$'\n'* || "$ACCEPT_RISK" == *$'\r'* ]]; then
                          printf '%s\n' 'ERROR: --accept-scan-risk needs a nonblank reason, not an option or multiple lines.' >&2
                          exit 2
                        fi
                        shift 2 ;;
    -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$IMAGE" ] || { echo "ERROR: --image is required." >&2; exit 2; }
if [ -n "$BUILD_DIR" ]; then
  if [ -z "$RELEASE" ] || [ -z "$RELEASE_COMMIT" ]; then
    echo "ERROR: --build-dir needs --release and --release-commit (the image must carry its release identity, CLAUDE.md \"Deploying\")." >&2
    exit 2
  fi
fi
if [ -z "$SCAN_SCRIPT" ]; then
  if [ -n "$BUILD_DIR" ] && [ -f "$BUILD_DIR/scripts/scan_image.sh" ]; then
    SCAN_SCRIPT="$BUILD_DIR/scripts/scan_image.sh"
  else
    SCAN_SCRIPT="$KIT_DIR/../../scripts/scan_image.sh"
  fi
fi
[ -f "$SCAN_SCRIPT" ] || { echo "ERROR: scan script not found: $SCAN_SCRIPT" >&2; exit 2; }

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*"; }

# --- 1. Build (only with --build-dir; otherwise the image is expected built already)
if [ -n "$BUILD_DIR" ]; then
  log "building $IMAGE from $BUILD_DIR"
  build_args=(--build-arg "RELEASE=$RELEASE" --build-arg "RELEASE_COMMIT=$RELEASE_COMMIT")
  [ "$WITH_SAML" = 1 ] && build_args+=(--build-arg "WITH_SAML=1")
  docker build "${build_args[@]}" -t "$IMAGE" "$BUILD_DIR"
fi

# --- 2. Scan the exact image BEFORE anything is stopped or renamed
IMAGE_ID="$(docker image inspect --format '{{.Id}}' -- "$IMAGE")"
[[ "$IMAGE_ID" == sha256:* ]] || { log 'ERROR: cannot resolve image ID'; exit 1; }
log "scanning image $IMAGE id=$IMAGE_ID ($SCAN_SCRIPT)"
scan_status=0
sh "$SCAN_SCRIPT" "$IMAGE_ID" || scan_status=$?
if [ "$scan_status" -ne 0 ]; then
  if [ -z "$ACCEPT_RISK" ]; then
    log "SCAN FAILED (exit $scan_status): refusing to switch containers"
    log "fix the findings and redeploy, or pass --accept-scan-risk \"<reason>\" to continue (the reason is required and must go into your change log)"
    exit 1
  fi
  [ -d "${SCAN_OUT_DIR:-scan-reports}" ] || mkdir -p "${SCAN_OUT_DIR:-scan-reports}"
  log "ACCEPT-SCAN-RISK reason=\"$ACCEPT_RISK\" operator=\"$(id -un)\" image=$IMAGE id=$IMAGE_ID scan-exit=$scan_status" \
    | tee -a "${SCAN_OUT_DIR:-scan-reports}/accept-scan-risk.log"
fi

# --- 3. Switch: the old container is renamed for rollback, the new one inherits
# its settings (env incl. the two app secrets, bind mounts, network, log caps)
umask 077
env_dir="$(mktemp -d)"
trap 'rm -rf -- "$env_dir"' EXIT
old_env_file="$env_dir/overrides"
old_image="$(docker inspect --format '{{.Image}}' "$CONTAINER")"
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" > "$env_dir/container"
docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$old_image" > "$env_dir/image"
# Only runtime overrides survive. New image defaults must not be masked by the
# previous image's PATH, PYTHON_VERSION, etc. Never print these secret values.
awk 'FILENAME == ARGV[1] { defaults[$0]=1; next } !($0 in defaults) && NF' \
  "$env_dir/image" "$env_dir/container" > "$old_env_file"
chmod 600 "$old_env_file"
mounts="$(docker inspect --format '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}|{{.Destination}}|{{.RW}}{{println}}{{end}}{{end}}' "$CONTAINER")"
run_args=(-d --name "$CONTAINER" --restart unless-stopped
  --log-opt max-size=10m --log-opt max-file=5
  --env-file "$old_env_file" --network "$NETWORK" --publish "$PUBLISH")
sources=()
has_data=0
while IFS='|' read -r source destination writable; do
  [ -n "$source" ] || continue
  sources+=("$source")
  [ "$destination" = /data ] && has_data=1
  mode=rw
  [ "$writable" = true ] || mode=ro
  run_args+=(-v "$source:$destination:$mode")
done <<< "$mounts"
[ "$has_data" = 1 ] || { log 'ERROR: expected /data bind mount; refusing unsafe swap'; exit 1; }
stamp="$(date -u '+%Y%m%dT%H%M%SZ')-$$"
slug="$(printf '%s' "$IMAGE" | tr -c 'a-zA-Z0-9._-' '_')"
backup="$HOME/backup-pre-$slug-$stamp.tar.gz"
# Create privately before sudo opens it, retaining mode 0600 for keys and data.
(set -o noclobber; : > "$backup")
docker stop "$CONTAINER"
# The entire stopped data folder includes SQLite DB/WAL/SHM, uploads and keys.
# Back up every bind source, including read-only configuration mounts.
if ! sudo tar -czpf "$backup" -- "${sources[@]}"; then
  log "ERROR: backup failed; restarting existing container; backup=$backup"
  docker start "$CONTAINER"
  exit 1
fi
# First redeploy has no previous rollback container. A failed removal is safe:
# if an old container still occupies the name, rename fails and we restart below.
docker rm -f "${CONTAINER}-old" 2>/dev/null || true
if ! docker rename "$CONTAINER" "${CONTAINER}-old"; then
  docker start "$CONTAINER"
  exit 1
fi
log "old container kept as ${CONTAINER}-old; rollback backup=$backup"
rollback() {
  log "ERROR: new container failed; rolling back; backup=$backup"
  if ! docker rm -f "$CONTAINER"; then
    # docker run can fail before creating anything. Only proceed after Docker
    # confirms the failed container is absent, never while it could write data.
    remaining="$(docker container ls -aq --filter "name=^/${CONTAINER}$")" || return 1
    [ -z "$remaining" ] || { log 'ERROR: cannot remove failed container'; return 1; }
  fi
  # Startup can migrate data. Preserve failed data separately, then restore the
  # stopped backup, avoiding stale WAL files alongside the restored database.
  for source in "${sources[@]}"; do
    sudo mv -- "$source" "$source.failed-$stamp" || return 1
  done
  sudo tar -xzpf "$backup" -C / || return 1
  docker rename "${CONTAINER}-old" "$CONTAINER" || return 1
  docker start "$CONTAINER"
}
if ! docker run "${run_args[@]}" "$IMAGE_ID"; then
  rollback
  exit 1
fi
deadline=$((SECONDS + 60))
while [ "$SECONDS" -lt "$deadline" ]; do
  [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" = true ] || break
  if docker exec "$CONTAINER" curl -fsS --max-time 2 http://127.0.0.1:8000/ready >/dev/null; then
    log "SWITCHED to $IMAGE id=$IMAGE_ID (rollback container: ${CONTAINER}-old; backup=$backup)"
    exit 0
  fi
  sleep 2
done
rollback
exit 1
