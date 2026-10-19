# Localization OS - Jira Forge app

An Atlassian Forge app that adds a **Localization OS** panel to the Jira issue view
with **Pull into Localization OS / Push translations back** buttons. The buttons call
a Forge backend resolver, which calls Localization OS's inbound API (so the token
stays server-side).

The vendor SDKs (`@forge/*`) install from npm under their vendors' own terms and are not covered by this project's Apache 2.0 license.

## Prerequisites
- Node.js 18+ and the Forge CLI: `npm install -g @forge/cli`
- An Atlassian account with permission to install apps on your Jira site
  (`forge login`).
- A Localization OS **Jira connection** already created (Connectors → Jira → add
  connection) - note its **connection id**.
- A per-user API key from Account for a dedicated, customer-confined linguist account.
  Legacy shared connector tokens are refused. All users of this Forge app exercise the
  dedicated account's authority; restrict app access accordingly.
- A publicly reachable Localization OS URL (see `../README.md` → tunneling).

## Required lock review before first deployment

`package-lock.json` was generated and reviewed on 2026-09-28 (registry.npmjs.org only,
sha512 integrity). KNOWN AND DEFERRED: `npm audit` reports 10 advisories (3 low, 7
moderate) in `@forge/react` 10, `@forge/api`, `@forge/resolver` and their transitive
dependencies (prismjs, refractor, uuid, webpack). Every fix needs the major upgrade to
`@forge/react` 12 and `@forge/resolver` 1.8, which needs a Forge deploy to test; the
owner deferred it to the planned Forge app update (moving it to per-user keys). Until
then, CI runs this app's npm audit with `--audit-level=high`: these known moderate and
low advisories do not fail the build, and any new high or critical advisory still does.
Every other integration fails on any advisory. To regenerate after a dependency
change, run these commands in this directory on an isolated, networked machine:

```bash
npm install --package-lock-only --ignore-scripts
npm audit --package-lock-only --ignore-scripts
```

Review the full resolved tree and integrity fields, fix reported advisories, and commit
the reviewed `package-lock.json`. Then use `npm ci --ignore-scripts` for installs.
Repeat the audit before every release, including when Actions minutes are unavailable.
An unreachable registry or failed audit blocks release; it is not a clean scan.

## Deploy
```bash
npm ci --ignore-scripts
forge register                 # assigns an app id - paste it into manifest.yml app.id
# Put your Localization OS host into manifest.yml permissions.external.fetch.backend

# Configure the backend (per environment, e.g. development):
forge variables set LOC_STUDIO_URL https://your-loc-studio-host.example.com
forge variables set --encrypt LOC_STUDIO_TOKEN <per-user-api-key>
forge variables set LOC_STUDIO_CONNECTION_ID <connection-id>

forge deploy
forge install            # choose Jira, pick your site
```

Open any issue → the **Localization OS** panel appears on the right. Click **Pull**,
translate in Localization OS, then **Push translations back** (written as a comment on
each issue, or to a custom field if the connection is configured that way).

## Tunnel during development
`forge tunnel` runs the backend locally; combine with a Localization OS tunnel so both
ends are reachable.

Optional extra variable - the languages the **Translate this issue** button uses
(if unset, Localization OS uses the connection's configured target locales):
```bash
forge variables set LOC_STUDIO_TARGET_LOCALES es-MX,de-DE,fr-FR
```

## How it talks to Localization OS
The primary **Translate this issue** button translates the open issue on demand -
each language is posted as a labelled comment:
```
POST {LOC_STUDIO_URL}/api/connectors/{ID}/jira/translate   header X-Loc-Token
body { "issue_key": "<current issue>", "target_locales": ["es-MX", ...] }
```
**Run saved JQL batch** translates every issue matching the connection's JQL. It is paid and
org-wide, so it only runs for a Jira site administrator (checked as the invoking user), and
Localization OS itself refuses a Jira batch unless the install key belongs to an admin account.
**Translate this issue** needs edit and comment rights on that issue. The app needs the
`read:jira-work` scope (already in `manifest.yml`), which makes `forge deploy` ask you to
re-install with `forge install --upgrade`:
```
POST {LOC_STUDIO_URL}/api/connectors/{ID}/sync   header X-Loc-Token
```
