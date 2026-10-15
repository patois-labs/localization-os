# docs/ map

Start with the repository `README.md`, then open anything here only when you need it. The documentation is
licensed under CC BY 4.0 (see the repository root).

| Folder | What lives there | How it changes |
|---|---|---|
| `decisions/` | ADRs (`NNNN-kebab-title.md`, Nygard format) plus `INDEX.md`, one line each. Numbers are never reused, so gaps are normal. | A changed decision gets a **new** ADR; the old one is marked `Superseded by NNNN`. Never edit a decision away. |
| `architecture/` | L1 architecture docs, one per subsystem, plus the authoring contract and template (start at `architecture/README.md`; the root map is `../ARCHITECTURE.md`). A doc-truth guard checks every reference in them. | Edit in place with the code. |
| `specs/` | `mcp-server.md`: the design record for the MCP server. | Edit in place. |
| `research/` | Background notes on CAT tools, vendor interoperability and quality-estimation research that informed the design. | Edit in place. |
| `guides/` | User-facing guides. | Edit in place. |
| `notebooks/` | The Databricks training walkthrough notebook. | Edit in place. |
| `releases/` | Release notes per version (`<version>.md`) and `README.md`. | One file per release; fix errors in place. |
| top-level `*.md` | Operator and user docs: `DEPLOY.md`, `BACKUP.md`, `SECRETS.md`, `CLI.md`, `LOCAL-INSTALL-SECURITY.md`, `governance.md` and others. | Edit in place. |

Related, outside `docs/`:

- `.claude/rules/*.md`: contributor notes about known traps that load automatically only when an AI coding
  agent opens matching files (each has a `paths:` frontmatter list). They are also plain reading for people.
- `deploy/aws-hosted/`: the reference kit for self-hosting on a single cloud VM.

Rules of the road:

1. Decisions are superseded, never edited away.
2. Every CLI leaf and flag appears in `CLI.md`; every module carries a `Documented in: docs/architecture/<name>.md`
   line.
3. No em dashes anywhere; ASCII preferred in anything that prints to a terminal.
