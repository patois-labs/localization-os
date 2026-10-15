# Localization OS Operator - Claude Skill

A [Claude Skill](https://docs.claude.com/en/docs/claude-code/skills) that turns any
Claude surface - Claude Code, Claude Desktop, and (with caveats, see below)
claude.ai - into a full **operator console for Localization OS**, the self-hosted
MT-first Translation Management System.

With this skill installed and the user's own API key configured, a plain request -
*"translate this file into Spanish and French for Acme"* - is enough: Claude
resolves the customer, the project pattern, the workflow, the engine, and the
language assets **from the install's own state**, states a plan, and drives the job
to delivered files. It carries the operating **judgment** (discovery, sequencing,
guardrail posture, error diagnosis) and pulls the live API truth from the connected
install rather than duplicating a route reference that would drift.

## What is in here

```
skill/
  SKILL.md                        the operating contract (transport, identity,
                                  discovery, execution, guardrails, reporting)
  references/
    context-inference.md          the discovery playbook
    api-recipes.md                literal MCP + raw-HTTP sequences for every flow
    troubleshooting.md            the full error vocabulary and diagnosis trees
  README.md                       this file
```

The skill format is a directory with a `SKILL.md` (YAML frontmatter + body) and
progressive-disclosure files under `references/`. There is **no `skill.json`** - all
metadata lives in the `SKILL.md` frontmatter.

## Install

**As a personal skill (Claude Code)** - copy this `skill/` directory into your
Claude Code skills directory, renamed to the skill name. Create the `skills` parent
first so the copy has somewhere to land:

```bash
# Claude Code, per user
mkdir -p ~/.claude/skills
cp -r integrations/mcp-server/skill ~/.claude/skills/loc-studio-operator
```

```powershell
# Windows (Claude Code)
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills" | Out-Null
Copy-Item -Recurse integrations\mcp-server\skill `
  "$env:USERPROFILE\.claude\skills\loc-studio-operator"
```

These paths are **Claude Code's** skills directory. **Claude Desktop** manages
skills through the app itself, not this `~/.claude/skills` path - add the skill
there through Claude Desktop's own skills/capabilities settings.

**As an org-distributed plugin** - bundle this directory as a skill inside a Claude
Code plugin and distribute it through your plugin marketplace, so every org member
gets the same versioned copy. (See the Claude Code plugin docs for the marketplace
layout; the skill directory drops in unchanged.)

The canonical copy is **versioned with the Localization OS repo** and is
copy-installable; update by re-copying after a `git pull`.

## Required secrets - the user's own key, never in the skill

**This skill contains zero secrets and requires zero hostnames.** It reads none and
embeds none. Each user supplies **their own** credentials through the environment or
their MCP config:

| Variable | Meaning |
|----------|---------|
| `LOC_STUDIO_TOKEN` | The user's own personal API key, prefix **`lsu_`**. Bound to that user (org, role, scopes, customer binding, audit attribution all ride on it server-side). |
| `LOC_STUDIO_BASE_URL` | Base URL of the running Localization OS install (e.g. a localhost dev server, an internal instance, or a hosted deployment). No hostname is hard-coded anywhere in the skill. |

Mint a key at **`/account`** in the Localization OS web UI (it is shown once). The shared
`lsk_` connector token does **not** work here - only a personal `lsu_` key. Never
paste a key into chat; the skill never asks you to, and never echoes one back.

When the **Localization OS MCP server** is connected, the same `LOC_STUDIO_TOKEN` /
`LOC_STUDIO_BASE_URL` are set in that server's MCP config (see
`integrations/mcp-server/README.md`), and the skill drives the MCP tools directly.
Without the MCP server, the skill falls back to raw `/api` over HTTPS (the token in
the `X-Loc-Token` header) or the repo CLI's `--remote` mode.

## Scope minimalism

Grant a key only the scopes its work needs. Three scopes exist (`api` is always
implied):

- **A documents-only key (`api docs`)** for the "translate this file / drive a
  workflow" flow - the common operator case (projects, documents, workflows,
  language assets, ops reads).
- **A CI/sync key (`api keys`)** for the continuous-localization Delta loop only
  (list/upsert/import/export keys + the change feed).
- Grant **both** only where one person genuinely drives both. A missing scope
  surfaces as a **403 that names the grant** - nothing degrades silently.

Minimal scoping is a real control: a CI sync token that reads the change feed has no
business uploading documents, and the split enforces exactly that.

## claude.ai status - stated honestly

The skill works today over **Claude Code and Claude Desktop** (via the Localization OS
MCP server over stdio, or raw `/api`, or the repo CLI).

Using it from **claude.ai** depends on the Localization OS MCP server being reachable as
a **custom connector**, and that path is **not yet proven end-to-end**:

- The **server half** of a static-bearer connector is built and verified.
- The **claude.ai half** is a **gated beta** ("contact Anthropic for early access")
  with a **known open bug** (`anthropics/claude-ai-mcp#644`) in which the
  configured request header is never sent - so **nobody has connected claude.ai to
  this server yet**. If the connector appears healthy (all tools listed) but the
  first tool call fails with *"No Localization OS token on this request,"* that is the
  header not reaching the server (the beta/bug), **not** a Localization OS fault.

Treat claude.ai support as **experimental** until that bug is resolved and the
OAuth-directory flow (Phase C2) ships. The full connector story lives in
`integrations/mcp-server/README.md` - that file is owned by the MCP-server build;
this README only summarizes it.

## Source of truth

This skill is written **against** `app/capabilities/llm.md` (the driver's manual),
`integrations/mcp-server/README.md` (the tool catalog), and `docs/CLI.md`. Where the skill and those docs
disagree, **those docs win and the skill has a bug.** The skill deliberately does
not restate the whole API - it teaches Claude to pull the live
`locstudio://guide/llm` resource (MCP) or `GET /api/capabilities/llm` +
`/openapi.json` from the connected install, which cannot drift.

The workflow-authoring surface referenced in `SKILL.md` §4 and
`references/api-recipes.md` (the `create_workflow` / `edit_workflow` MCP tools and
the `POST /api/workflows/...` routes) shipped with `SPEC-workflow-authoring-api` and
is **present on current builds**; because installs differ the skill still teaches
confirming it live before authoring over the API. One `lqa` step field the recipes
use - **`lqa_threshold`** (the configurable numeric LQA pass gate) - rides a
**later** feature (`feat/lqa-configurable-threshold`) and is pinned as such in
`references/api-recipes.md` Recipe D so it can be verified at merge; on a build
without that field the `lqa` gate is the fixed `score >= 90 & no major/critical`.
