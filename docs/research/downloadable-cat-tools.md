# Research: downloadable/self-hostable CAT tools to test the vendor roundtrip against (research notes, 2026-08-08)

> Purpose: a REAL third-party system on the receiving end of our dispatch (file
> exchange and/or API), independent of Vendor Studio. Windows 11 + Docker Desktop.

## Ranked recommendation
1. **translate5** (AGPL, Docker) - best conceptual match: an actual open-source LSP/vendor TMS with task-based projects, translator/reviewer roles, "REST API for all UI functions", XLIFF 1.2 + Okapi conversion. ~2-3h setup. Gotchas: docs expect a public hostname + SSL (use hosts-file + self-signed cert or a tunnel); must hand-edit docker-compose.yml to strip a license-disclaimer block or the stack fails cryptically. confluence.translate5.net "Docker based installation".
2. **Weblate** (GPL, Docker) - best pure API roundtrip partner: most mature/best-documented REST API of any self-hostable option, zero license friction, official docker-compose, 30-60 min setup. Flow: `POST /api/projects/` → `POST /api/projects/<slug>/components/` (XLIFF as zipfile) → edit in web UI → `GET /api/components/<p>/<c>/file/?format=xliff12`. Caveat: VCS-centric paradigm; pure upload/download works but is off its main path. docs.weblate.org/api.
3. **MateCat hosted** (matecat.com) - best effort-to-value: purpose-built project API (`POST /new` multipart → `GET /status` → `GET /url` incl. an xliff download), BUT credentials require emailing support@matecat.com (unknown lead time) - send that email early. **Do NOT self-host MateCat**: Ubuntu-only official path, Apache+MySQL+ActiveMQ/Java+Redis+PHP7.4+Node20, no supported Docker.
- **Honorable mention - OmegaT** (desktop, free): the manual file-exchange partner. Open our XLIFF 1.2 directly (add the Okapi filters plugin for full 1.2/2.x fidelity), edit in a real CAT editor, export target. <30 min, zero server ops.

## Other candidates assessed
- **Tolgee** - free self-host (Docker, 10 seats), real API + XLIFF 1.2, good editor; but it's i18n key/value management, one step removed from document CAT handoff.
- **Traduora** (ever-co) - maintained, AGPL, XLIFF 1.2 via API; demo compose has NO persistent storage; real deploy needs external MySQL + TLS proxy (effort ≈ translate5).
- **Pontoon** - DEAD END: Docker is dev-only per Mozilla, API is read-only GraphQL stats; cannot create projects or push/pull files.
- **Smartcat** - best free hosted SaaS secondary: genuinely free tier, no card, real API (project/create + multipart upload, 50+ formats incl. XLIFF).
- **memoQ** - WS-API is a separately licensed add-on; public 30-day trial is the desktop client, not the server. Days, not hours; needs sales contact.
- **Wordfast Anywhere** - free, opens XLIFF/TXLF/SDLXLIFF, but no confirmed public API; manual-exchange tier only.
- **Crowdin** - real API v2 + XLIFF, but free only for open-source projects; private test = 14-day trial window.
- **Phrase Strings** - 14-day trial; XLIFF 2.0 native.

## Gotchas (full list)
- MateCat self-host is a trap (see above).
- translate5: public-hostname/SSL expectation + the compose-file license-block edit.
- Weblate: custom bind-mount volumes expect UID 1000 (non-issue with the official compose as-is on WSL2).
- Traduora demo compose is ephemeral.
- AGPL (translate5/Traduora): share-alike on modifications if exposed to others - non-issue for internal testing.
- memoQ WS-API licensing; Crowdin free tier scope; Wordfast no API.

## Suggested test matrix (orchestrator note)
- API roundtrip vs a third party: **Weblate** (fast) then **translate5** (the LSP-shaped one).
- Manual XLIFF exchange: **OmegaT** (+ Okapi plugin) - the Wave D XLIFF 1.2 rendition acceptance test: our export opens clean, edits round-trip, locked (`translate="no"`) rows honored.
- Hosted API secondary: MateCat (email for key first), Smartcat free tier.
