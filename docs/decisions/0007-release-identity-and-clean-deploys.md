# 0007. Every image carries its release identity and is built from a clean tree

- Status: Accepted
- Date: 2026-09-08 (build args); 2026-10-02 (clean detached worktree)

## Context

An image built without release information reports the stale `0.8.0` from `pyproject.toml`, which
already caused one bad release. A deploy that ships the working directory also ships whatever is
uncommitted or untracked there.

## Decision

- Every build passes `--build-arg RELEASE="$(git describe --tags --always --dirty)"` and
  `--build-arg RELEASE_COMMIT="$(git rev-parse HEAD)"`; the Dockerfile runtime stage writes `/app/RELEASE`.
- Deploys build from a clean detached worktree of the exact commit, or a `git archive` of it; never
  from a working checkout that may carry uncommitted files.
- Every host of one release runs the same commit.
- Verification includes reading the release back (`/health`, `docker exec ... cat /app/RELEASE`).

## Consequences

A `-dirty` suffix in a release string means the process was skipped.

## Links

- docs/DEPLOY.md
