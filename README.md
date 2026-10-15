# Localization OS

**Own your localization stack.** Localization OS is an open source, self-hosted, MT-first translation management
system (TMS). Upload a file or connect a CMS, let machine translation do the first pass with your translation memory,
glossary and style guide, check quality automatically, post-edit only what needs a person, and get the file back in
its original format.

It runs on your own machine or server, on cloud AI engines or fully local models, and nothing leaves your network
unless you tell it to.

- **License:** Apache 2.0 ([LICENSE](LICENSE)). Documentation is CC BY 4.0 ([docs/LICENSE.md](docs/LICENSE.md)).
- **Maintainer:** [Patois Labs, LLC](https://patoislabs.com). See [GOVERNANCE.md](GOVERNANCE.md) and our
  [stewardship promise](STEWARDSHIP.md).
- **Stack:** Python, FastAPI, Jinja2, vanilla JavaScript, SQLite (Postgres optional). No Node build step.

## Quick start (about 5 minutes)

You need Python 3.10 or newer (CI and the container use 3.13).

```bash
git clone https://github.com/patois-labs/localization-os.git
cd localization-os
python3 scripts/bootstrap.py     # macOS / Linux
python scripts/bootstrap.py      # Windows (or: py scripts\bootstrap.py)
```

`bootstrap.py` creates a `.venv`, installs the pinned runtime dependencies AND the developer tools (test runner, browser automation, audit tools), so the `.venv` is about 600 MB, and copies `.env.example` to `.env`. Then start it:

```bash
# macOS / Linux
.venv/bin/python run.py
# Windows
.venv\Scripts\python.exe run.py
```

Open <http://127.0.0.1:8000> (set `PORT` and `HOST` in `.env` to change it) and sign in with `admin` / `admin` (you are asked to choose a new password). The default
engine is the built-in **mock** engine, so everything works offline and free with obviously fake translations. Add a
key for Claude, Gemini or OpenAI in `.env`, or point it at a local model server (LM Studio, Ollama, vLLM), and
translations become real with no code changes.

Prefer containers? `docker compose up` runs it on SQLite; see [docs/DEPLOY.md](docs/DEPLOY.md) for Postgres, TLS and
the first-admin setup wizard.

## What it does

**Translation**
- Configurable workflows: translate, quality check, fix and re-check loops, human review steps, pause and resume.
- Describe a workflow in plain language and it drafts the steps for you to review.
- Translation memory (TMX, fuzzy matching, locks), glossaries with do-not-translate and required terms, style guides,
  content types and context packages, all sent to the model and verified on the way back.
- Automated quality checks: MQM-style scoring, tags, numbers, terms, placeholders, refusals.
- A post-editing editor for the segments that need a person.
- Engines behind one switch: Claude, Gemini, OpenAI, DeepL, Google, Microsoft, Amazon, local self-hosted models and any
  OpenAI-compatible endpoint, with failover and a spend cap.

**Content in and out**
- More than 80 file formats (Office, PDF, HTML, XML/DITA, Markdown, InDesign, subtitles, software strings, XLIFF and
  CAT packages, images) returned in the same format and layout. An untouched file comes back byte for byte.
- Three project types: documents, string keys (with a delta API for CI), and projects connected to a source system.
- 28 connectors, including Airtable, Jira, Strapi, Contentful, HubSpot, WordPress, Shopify, Zendesk, Confluence,
  GitHub, GitLab, SharePoint and Google Docs. Translations go back as each system's own language version; the source
  is never overwritten.
- External vendors: send work to a human vendor and receive a signed return.

**Security and administration**
- Role-based access, project membership, SSO (OIDC, SAML, LDAP / Active Directory), SCIM provisioning, MFA.
- A tamper-evident audit chain, an LLM Ledger of every model call, GDPR export and erasure, PII redaction, encryption
  at rest.
- Reports, a dashboard, a reviewer worklist, comments and queries, data export, retention rules.

**Built for agents**
- Every feature is an API first. A full REST API, a headless CLI with more than 480 commands, and an MCP server (72 task-shaped tools) so AI
  agents such as Claude can run projects, workflows, translation memory searches and connector syncs.

## Paid add-ons and hosting

The core in this repository is complete and free. Patois Labs also offers paid add-ons for specialist needs (for
example an agent-based advanced workflow tier, an evaluations lab, semantic translation memory and enterprise
infrastructure adapters), plus managed hosting, private AI on your hardware or ours, support, custom integrations
and procurement paperwork. What stays free forever is written down in [STEWARDSHIP.md](STEWARDSHIP.md). Details:
<https://patoislabs.com>.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): the whole system in one read. Per-module docs in
  [docs/architecture/](docs/architecture/).
- [docs/DEPLOY.md](docs/DEPLOY.md), [docs/SECRETS.md](docs/SECRETS.md), [docs/BACKUP.md](docs/BACKUP.md): running it
  for real.
- [docs/embedded.md](docs/embedded.md): API-only (headless) mode inside another product.
- [docs/CLI.md](docs/CLI.md): the command-line reference.
- [integrations/mcp-server/README.md](integrations/mcp-server/README.md): connecting Claude Desktop and other MCP
  clients.
- [docs/decisions/INDEX.md](docs/decisions/INDEX.md): why things are the way they are.
- [llms.txt](llms.txt): the entry point for AI readers.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) (setup, tests and the DCO sign-off), the
[code of conduct](CODE_OF_CONDUCT.md), and [AGENTS.md](AGENTS.md) if you work with an AI coding assistant. Report
security issues privately as described in [SECURITY.md](SECURITY.md), never in a public issue.

## License and trademarks

Copyright 2026 Patois Labs, LLC. Licensed under the Apache License, Version 2.0; see [LICENSE](LICENSE) and
[NOTICE](NOTICE). Third-party components keep their own licenses, listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The Apache license does not grant rights to the Patois Labs names
and logos; see [TRADEMARKS.md](TRADEMARKS.md).
