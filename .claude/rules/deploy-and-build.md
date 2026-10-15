---
paths:
  - "Dockerfile"
  - "docker-entrypoint.sh"
  - "docker-compose.yml"
  - "deploy/**"
  - "requirements*.txt"
  - "requirements*.lock"
  - "scripts/hash_locks.py"
  - "scripts/release_lock.py"
  - "scripts/pin_base_images.py"
  - "docs/DEPLOY*.md"
---

# Build, image and deploy files

Deploy guides: `docs/DEPLOY.md` and `deploy/aws-hosted/LAUNCH.md`.

- `ARG RELEASE` / `ARG RELEASE_COMMIT` in the Dockerfile runtime stage are required build args; without them `/health` reports the stale `0.8.0`. Keep them when editing the Dockerfile.
- Build from a clean checkout: a Docker build ships the working directory, so local files in it end up in the image unless `.dockerignore` excludes them.
- No image serves unscanned, on any path: release, first launch, rollback, Caddy swap, restore. Scan the exact image ID with `scripts/scan_image.sh` and start or deploy that same ID or digest: AWS via `deploy/aws-hosted/aws-redeploy.sh` and `deploy/aws-hosted/LAUNCH.md`, Compose via `docs/DEPLOY.md` "Compose image scan gate". `tests/verify_image_scanning.py` guards these docs.
- The app is one process per database on purpose (in-memory job queue and scheduler thread); never run more than one replica against the same data.
- Locks are hash-pinned (`--require-hashes`); regenerate with network through `scripts/hash_locks.py` and review the diff. A Dependabot merge leaves the lock stale.
- Base images and caddy/Trivy images are digest-pinned (`scripts/pin_base_images.py`, Trivy `aquasec/trivy:0.74.0@sha256:...`). GitHub Actions are SHA-pinned.
- SAML stays out of the base image (`WITH_SAML=1` opt-in); `WITH_SQLCIPHER=1` is the encrypted-DB build. On a small host the SAML layer uses binary wheels `lxml==6.0.0` + `xmlsec==1.3.17` only; a source compile OOMs.
- `deploy/aws-hosted/` Caddyfile.cloudflare imports `/etc/caddy/origin-auth.caddy` with no fallback: Caddy will not start until the Cloudflare origin-header step is done. Validate before every caddy change. `aws-alerting-setup.sh` uses fixed names; make them per-host before a second host.
- Copying production data anywhere: leave out `/data/tailscale` (the Tailscale node identity) and set `sync_scheduler_enabled=0` before first boot.
- SQLite in WAL mode: a backup copies `.db`, `-wal`, `-shm`, `.enc_key`, `.session_secret` together with the app stopped (or checkpoint first). A lone `.db` copy is silently stale.
- Shell scripts that run on Linux hosts must be LF: write them with the Write tool and check for `\r` before upload.
