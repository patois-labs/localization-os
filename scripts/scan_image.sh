#!/bin/sh
# Scan OS/language packages and embedded Go binaries before the image serves.
# Trivy pin resolved by the orchestrator from registry-1.docker.io, 2026-09-28.
# Usage: sh scripts/scan_image.sh <image:tag-or-id>
# SCAN_OUT_DIR defaults to scan-reports; SCAN_SEVERITIES to HIGH,CRITICAL.
# Exit 0: clean; 1: fixable findings; 2: usage or scanner error.
set -eu

if [ $# -ne 1 ] || [ -z "${1:-}" ]; then
    printf '%s\n' 'usage: sh scripts/scan_image.sh <image:tag-or-id>' >&2
    exit 2
fi
IMAGE="$1"
TRIVY_IMAGE="${TRIVY_IMAGE:-aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969}"
SEVERITIES="${SCAN_SEVERITIES:-HIGH,CRITICAL}"
OUT_DIR="${SCAN_OUT_DIR:-scan-reports}"
case "$TRIVY_IMAGE" in
    *@sha256:REPLACE_WITH_PINNED_TRIVY_DIGEST)
        printf '%s\n' 'ERROR: Trivy digest is still the placeholder.' >&2; exit 2 ;;
    *@sha256:*) ;;
    *) printf '%s\n' 'ERROR: TRIVY_IMAGE must be digest-pinned (tag@sha256:...).' >&2; exit 2 ;;
esac
digest="${TRIVY_IMAGE##*@sha256:}"
case "$digest" in
    *[!a-f0-9]*) printf '%s\n' 'ERROR: invalid Trivy digest.' >&2; exit 2 ;;
esac
[ "${#digest}" -eq 64 ] || { printf '%s\n' 'ERROR: invalid Trivy digest length.' >&2; exit 2; }

ID="$(docker image inspect --format '{{.Id}}' -- "$IMAGE")" || {
    printf '%s\n' "IMAGE-SCAN $IMAGE: ERROR cannot resolve local image" >&2; exit 2
}
case "$ID" in sha256:*) ;; *) printf '%s\n' "IMAGE-SCAN $IMAGE: ERROR empty/invalid image ID" >&2; exit 2 ;; esac
STAMP="$(date -u '+%Y-%m-%dT%H%M%SZ')"
SLUG="$(printf '%s' "$IMAGE" | tr -c 'a-zA-Z0-9._-' '_')"
[ -d "$OUT_DIR" ] || mkdir -p "$OUT_DIR"
OUT_ABS="$(cd "$OUT_DIR" && pwd)"
BASENAME="trivy-$SLUG-$STAMP-$$"

# Every report type scans the same immutable ID once. The persistent cache avoids
# downloading the database for every run. The first run refreshes it; the rest
# use that cached DB. Serialize scans on this host to keep the DB snapshot stable.
scan() {
    docker run --rm -u root -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$OUT_ABS:/reports" -v trivy-cache:/root/.cache/trivy \
        "$TRIVY_IMAGE" image --scanners vuln --no-progress "$@" "$ID"
}
error() {
    printf '%s\n' "IMAGE-SCAN $IMAGE: ERROR id=$ID scanner failed reports=$OUT_ABS/$BASENAME.*" >&2
    exit 2
}
# Reports retain unfixable findings; the CycloneDX inventory is unfiltered.
scan --severity "$SEVERITIES" --format table --output "/reports/$BASENAME.table.txt" || error
scan --skip-db-update --skip-java-db-update --severity "$SEVERITIES" \
    --format json --output "/reports/$BASENAME.json" || error
scan --skip-db-update --skip-java-db-update \
    --format cyclonedx --output "/reports/$BASENAME.cdx.json" || error
gate=0
scan --skip-db-update --skip-java-db-update --severity "$SEVERITIES" \
    --ignore-unfixed --exit-code 3 --quiet --format table --output /dev/null || gate=$?
case "$gate" in
    0) printf '%s\n' "IMAGE-SCAN $IMAGE: PASS id=$ID severities=$SEVERITIES reports=$OUT_ABS/$BASENAME.*" ;;
    3) printf '%s\n' "IMAGE-SCAN $IMAGE: FAIL id=$ID fixable findings severities=$SEVERITIES reports=$OUT_ABS/$BASENAME.*"; exit 1 ;;
    *) error ;;
esac
