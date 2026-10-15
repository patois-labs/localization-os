# Localization OS - Airtable Extension

A custom Airtable Extension that adds a **Pull into Localization OS / Push translations
back** panel inside any base, calling Localization OS's inbound API.

The vendor SDKs (`@airtable/blocks` and its CLI) install from npm under their vendors' own terms and are not covered by this project's Apache 2.0 license.

## Prerequisites
- Node.js 18+ and the Airtable Blocks CLI: `npm install -g @airtable/blocks-cli`
- A Localization OS **Airtable connection** already created (Connectors → Airtable →
  add connection) - note its **connection id** (from the URL `/connectors/connection/{id}`).
- Your **per-user API key**, created in Account, with an admin or linguist role.
  The extension holds it only in memory for this session. Each viewer supplies their own key.
  GlobalConfig is visible to every base collaborator: remove any old `apiToken` entry and
  rotate the legacy shared token. Shared tokens no longer authorize connector operations.
- A publicly reachable Localization OS URL (see `../README.md` → tunneling) if your
  Localization OS runs on localhost.

## Required lock review before first deployment

`package-lock.json` was generated and reviewed on 2026-09-28 (registry.npmjs.org only,
sha512 integrity; `npm audit` clean at that time). To regenerate after a dependency change,
run these commands in this directory on an isolated, networked machine:

```bash
npm install --package-lock-only --ignore-scripts
npm audit --package-lock-only --ignore-scripts
```

Review the full resolved tree and integrity fields, fix reported advisories, and commit
the reviewed `package-lock.json`. Then use `npm ci --ignore-scripts` for installs.
Repeat the audit before every release, including when Actions minutes are unavailable.
An unreachable registry or failed audit blocks release; it is not a clean scan.
React 16 remains pinned pending Blocks SDK compatibility testing; no unsupported
major upgrade has been guessed in this offline change.

## Create the extension in your base
1. In Airtable: **Extensions → Add an extension → Build a custom extension**.
   Give it a name; Airtable shows you a **block id** and **base id**.
2. In this folder, register the remote and start it:
   ```bash
   npm ci --ignore-scripts
   block add-remote <baseId>/<blockId> default
   block run
   ```
3. Back in Airtable, the extension loads from your local dev server. Open its
   **settings** and fill in:
   - **Localization OS URL** - e.g. `https://your-tunnel.trycloudflare.com`. It is saved for
     you only (not in the shared base settings), must be `https://` (or `http://localhost`),
     and you click **Trust this server** once before your key is ever sent. A URL a
     collaborator saved in the old shared setting is only offered as a suggestion.
   - **Your per-user API key** (entered again when the extension reloads)
   - **Connection id**
4. Click **Pull into Localization OS**, translate in Localization OS, then **Push
   translations back** - the target field in your base fills in.

## Release (so teammates can use it without `block run`)
```bash
block release
```

## How it talks to Localization OS
The primary **Translate with Localization OS** button runs the whole round-trip
server-side (pull → translate → push) so no one has to open Localization OS:
```
POST {locStudioUrl}/api/connectors/{connectionId}/sync   header X-Loc-Token: <api token>
```
The **Pull only** / **Push only** buttons run a single stage (e.g. pull, then
post-edit in Localization OS, then push):
```
POST {locStudioUrl}/api/connectors/{connectionId}/pull   header X-Loc-Token: <api token>
POST {locStudioUrl}/api/connectors/{connectionId}/push   header X-Loc-Token: <api token>
```
The connection's stored config (base id, table, source/target fields, locales)
determines what gets pulled and where translations are written.

## A server-side relay is required (browser CORS)

As written, the extension's `fetch` runs in the **browser** and sends the custom
`X-Loc-Token` header cross-origin. Any cross-origin request with a custom header
first sends a CORS **preflight** (`OPTIONS`), and Localization OS deliberately
ships **no CORS middleware** (an `OPTIONS` on `/api` answers `405`), so the
preflight fails and the real request never leaves the browser. That is by design:
opening CORS on the `/api` surface would let scripts on any web page a user visits
talk to your instance from their browser.

So the extension needs a small **server-side relay** before it can work: an
endpoint on a server you control that holds the API key, makes the same
`POST {locStudioUrl}/api/connectors/{connectionId}/...` call with the
`X-Loc-Token` header, and passes the JSON answer back. The extension then calls
your relay instead of `/api` (the relay answers the browser's preflight itself,
for your Airtable origin only), and the relay's own call to Localization OS is
server-to-server, so no CORS is needed on `/api` and the key stays off the client.
