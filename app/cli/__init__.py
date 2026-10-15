"""Headless command-line interface for Localization OS.

Runs IN-PROCESS by default: it imports the same services and opens the same
database the web app does (like Alembic), so it works with NO server running -
essential for offline migrate/seed/config/backup/encrypt on an install.

Entry point: `python -m app.cli` (see __main__.py). Repo-root `loc-tms.cmd` /
`loc-tms.ps1` wrappers forward to it.

SECURITY MODEL (read this): in-process mode is a LOCAL-ROOT operation at the same
trust level as editing the SQLite file with the `sqlite3` shell or running
`alembic` - the operator already holds the DB file and the ENCRYPTION_KEY, so there
is NO RBAC and NO login. Audit rows are attributed to the actor "cli" (override
with --actor). Destructive commands confirm before acting (--yes skips). A future
`--remote URL --token` mode WILL go through the server's full token/RBAC/scope
enforcement; the Phase 1-2 commands are all in-process only and the remote flags
are accepted-but-unsupported so far.
"""
