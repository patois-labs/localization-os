# SPEC - MCP server (the product-ops fourth surface)

**Status: implemented. This document is the design record for the MCP server; the sections below describe
the design as it was decided and the waves in which it was built.**
Origin (2026-08-04): the maintainer asked for a server that covers everything but stays simple and, the
load-bearing half, whose upkeep is a small lift included in the development lifecycle so it always works.
Design pass: read-only trace of the 158 shipped `/api` routes at main `a05d825`.
Reconciles with the earlier Claude Desktop MCP plan (§13 below states exactly what is adopted,
superseded and deferred).

---

## 0. What already ships - the correction that reshapes this spec

This is **not** a greenfield build. The Phase A deliverable of the earlier Claude Desktop MCP plan
(§6b item 14, "14-lite") **shipped and is guarded**:

| Artifact | State |
|---|---|
| `integrations/mcp-server/server.py` | A real stdio MCP server, 352 lines, **5 tools**: `submit_translation`, `job_status`, `get_result`, `list_languages`, `list_capabilities` |
| `integrations/mcp-server/README.md` | Install + Claude Desktop config + docs-key minting, written |
| `integrations/mcp-server/skill/` | The real `loc-studio-operator` Claude Skill - versioned with the repo and installable per-user (see `skill/README.md`); no longer a scaffold |
| `tests/verify_mcp.py` | Level-1 tool tests against a real uvicorn'd app + a Level-2 stdio protocol round-trip |
| `mcp==1.28.1` | Pinned in `requirements.txt` **and** `requirements.lock` |

Three consequences run through everything below.

1. **The SDK and transport questions are settled by precedent, not by argument.** The official
   Python SDK's `mcp.server.fastmcp.FastMCP` is already the thing in the tree, already pinned,
   already exercised over real stdio in CI.
2. **The thin-client architecture is already law in this repo.** `server.py` imports nothing from
   `app/`; the app imports nothing from `mcp`. That separation is *stated in the module docstring
   and enforced by the fact that every other suite passes with the SDK uninstalled.* This spec
   extends that property; it must not spend it.
3. **v1 is an EXTENSION built on one registry, one guard, one resource.** The five shipped tools
   are not deprecated and not renamed - they become the "outcome" family (§13) and are the first
   five rows of the registry, so the registry describes reality from its first commit. (The
   registry has since grown to **72 tools across seven families**; the tier table in §4 and the
   totals in §14 are pinned to the live `registry.py` by `tests/verify_mcp_spec_tiers.py`, so these
   counts cannot silently re-drift.)

---

## 1. Why this, why now

Localization OS has three first-class surfaces (web, CLI, `/api`) and a fourth that exists as a
five-tool demo. Meanwhile `app/capabilities/llm.md` - the "Are you an LLM? Read here" card that
shipped this week - is a complete driver's manual for an agent, and the only way an agent can act
on it today is by hand-rolling HTTP or shelling out to `python -m app.cli`. The MCP server is the
missing packaging of a capability the product already has.

Two requirements, in priority order:

- **R1 - cover everything, but stay simple.** Not 158 tools. ~34 task-shaped tools, plus an
  accounting that proves nothing was silently dropped.
- **R2 - it must not rot.** *(The requirement.)* Every mechanism in §6–§9 exists to answer one
  question: when someone adds `/api/projects/{pid}/frobnicate` six months from now, what makes
  them add its MCP accounting in the same change? The answer is the same one the repo already
  uses three times over: **a suite that goes red.**

R2 is not speculative. This repo has watched an unguarded documentation mirror rot in
public - `/help` Part 2 documented 98 of 158 routes under a sentence claiming 93, with the
whole Delta API missing (`tests/verify_help.py`, the Part-2 comment block). The cards are pinned
to the argparse tree by `verify_ask_draft`; `docs/CLI.md` is pinned to the CLI leaves by
`verify_cli_parity`; the LLM card is pinned to the OpenAPI schema by `verify_help`. **An MCP tool
registry with no guard would be the fourth mirror and the only unpinned one.**

---

## 2. Architecture: a thin fourth surface

```
Claude Desktop / Claude Code / Codex / any MCP client
   │  stdio (local)  ·  streamable-HTTP (remote, §11)
   ▼
integrations/mcp-server/          ← standalone process. ZERO business logic.
   registry.py   the declarative tool table (the ONE file a feature dev edits)
   server.py     thin dispatch: build FastMCP tools from the registry, call HTTP
   │  X-Loc-Token: lsu_...   (never a query string)
   ▼
Localization OS /api  →  app/services/*  →  DB
```

**Locked calls (do not re-decide):**

- **v1 talks HTTP, not in-process.** One standalone process, one token, works against
  `127.0.0.1:8000` and against `https://your-host` with no code change and no new
  attack surface inside the app. An in-process `/mcp` mount is a later option if latency ever
  demands it; the registry is transport-agnostic so that port is a re-dispatch, not a rewrite.
- **Zero business logic in the server.** Every tool is: validate inputs → call one or more `/api`
  routes → reshape the JSON. No scoping filter, no RBAC decision, no retry-until-success loop, no
  translation. Where a tool composes calls (§3, `submit_translation` = upload + translate), the
  composition is a fixed two-step, not a workflow engine.
- **The server never interprets a 404.** A kill-switched feature answers 404 rather than 403 so it
  cannot be enumerated (`llm.md`, Guardrails). The MCP server relays that verbatim. The registry's
  `feature_gate` field is therefore **documentation and guard material, not runtime logic** - it
  makes the tool description honest ("requires key projects to be enabled") and lets the guard
  assert the named switch is real. Nothing in `server.py` reads settings.
- **The server never widens anything.** No tool takes an org id. No tool retries a 403. No tool
  probes ids to discover what exists (`llm.md`: *"Isolation is not yours to widen."*).

---

## 3. The tool surface: 72 tools, 7 families

(34 from waves 1+2; `create_workflow` + `edit_workflow` added by SPEC-workflow-authoring-api;
`attach_tm_source` + `pin_glossary` + `translate_keys` added by SPEC-full-remote-parity Wave R1a;
`manage_workflow_group` + `clone_workflow` + `translate_image` added by Wave R1b;
`connector_status` + `connector_pull` + `connector_translate` + `connector_push` + `connector_sync`
+ `connector_retry` added by Wave R4 - the NEVER_COVER amendment; and the Vendor Wave E tools,
`approve_segment` and the Wave 3 collaboration tools (6 query + 4 reviewer-LQA-finding + `my_work`)
bring the total to 72.)

Naming: unprefixed, as the five shipped tools already are (MCP clients namespace by server, and
the server id is `loc-studio`). Tier legend in §4. "Routes" is the exhaustive set a tool may call; the registry carries it and the guard checks it.

### Family A - Orientation (4)

| # | Tool | Tier | Routes |
|---|---|---|---|
| 81 | `release_manifest` | read | `GET /api/release` |
| 1 | `whoami` | read | `GET /api/account` |
| 2 | `list_languages` *(shipped)* | read | `GET /api/languages` |
| 3 | `list_capabilities` *(shipped, widened)* | read | `GET /api/formats`, `GET /api/langsets`, `GET /api/content-types` |

`whoami` is the first tool an agent should call: role, scopes, org/customer binding. It is also
the diagnosis the LLM card prescribes for a 403, which is otherwise undiagnosable ("the message
does not tell you which").

### Family B - Projects and customers (5)

| # | Tool | Tier | Routes |
|---|---|---|---|
| 4 | `list_projects` | read | `GET /api/projects`, `GET /api/customers`, `GET /api/projects/{pid}`, `GET /api/projects/{pid}/documents` |
| 5 | `create_project` | write | `POST /api/projects`, `POST /api/customers` |
| 6 | `configure_project` | write | `POST /api/projects/{pid}/` `rename` · `due` · `content-type` · `set-customer` · `add-locale` · `apply-langset` · `pair-providers` |
| 85 | `project_automation` | write | `GET`/`PUT /api/projects/{pid}/automation`, `POST /api/projects/{pid}/source`, `/source/unbind`, `/automation/pause`, `/automation/resume`, `/automation/handover`, `GET /api/projects/{pid}/runs` |
| 86 | `project_run` | spend | `GET /api/projects/{pid}/automation` (plan), `POST /api/projects/{pid}/runs` |

`create_project` is a **documented composite**: given a customer *name* that does not exist it
creates the customer first, because `POST /api/projects` 400s without `customer_id` and an agent
that cannot resolve that has no on-ramp at all. `configure_project` takes an `action` enum; it is
one tool because "adjust this project's settings" is one task.

### Family C - Documents (12 - the 6 below + the 6 Wave-3 Queries tools further down)

| # | Tool | Tier | Routes |
|---|---|---|---|
| 7 | `submit_translation` *(shipped)* | spend | `POST /api/documents`, `POST /api/documents/{doc_id}/translate` |
| 8 | `job_status` *(shipped, widened)* | read | `GET /api/jobs/{jid}`, `GET /api/jobs` |
| 9 | `get_result` *(shipped)* | read | `GET /api/documents/{doc_id}/download` |
| 10 | `apply_tm` | write | `POST /api/documents/{doc_id}/apply-tm` |
| 10a | `approve_segment` | write | `POST /api/segments/{sid}/approve` |
| 11 | `translate_text` | spend | `POST /api/translate/text` |

`submit_translation` with an empty `targets` list uploads only - that is how an agent stages a
document for a *workflow* run rather than plain MT, and it is why no separate `upload_document`
tool exists.

### Family D - Keys + the Delta loop (8) - **the first-class family**

| # | Tool | Tier | Gate | Routes |
|---|---|---|---|---|
| 12 | `list_keys` | read | `key_projects_enabled` | `GET /api/projects/{pid}/keys`, `GET /api/keys/{key_id}/values` |
| 13 | `upsert_keys` | write | `key_projects_enabled` | `POST /api/projects/{pid}/keys`, `PUT /api/keys/{key_id}/values/{locale}`, `PATCH /api/keys/{key_id}` |
| 14 | `import_keys` | write | `key_projects_enabled` | `POST /api/projects/{pid}/keys/import` |
| 15 | `export_keys` | read | `key_projects_enabled` | `GET /api/projects/{pid}/keys/export` |
| 16 | `key_changes` | read | `key_projects_enabled` | `GET /api/projects/{pid}/changes` |
| 37 | `translate_keys` | spend | `key_projects_enabled` | `POST /api/projects/{pid}/keys/translate` |
| 68 | `manage_key_image` | write | `key_image_context_enabled` | `POST /api/keys/{key_id}/images`, `DELETE /api/keys/{key_id}/images/{image_id}`, `POST /api/keys/{key_id}/images/{image_id}/primary` |
| 69 | `list_key_images` | read | `key_image_context_enabled` | `GET /api/keys/{key_id}/images` |

`manage_key_image` + `list_key_images` (Batch 14) manage a string key's per-string in-context
images: a vision-capable engine sees the primary image as context when translating that string.
Bytes ride the attach action as a **base64 param, never a URL** (no SSRF), and the JSON attach
body is read via the webhook stream-cap pattern (a raw ceiling aborts the stream before decode).
There is **no raw-bytes read tool** - `GET /api/keys/{key_id}/images/{image_id}/raw` stays EXEMPT
(authoring-surface); pulling customer pixels back is an exfiltration surface. The image DELETE is
a reversible soft-delete (write tier), so the destructive-tier-empty v1 law holds.

`translate_keys` (SPEC-full-remote-parity Wave R1) closes the "a key run is web-only" gap named
in `llm.md`: it launches a workflow over a key project's internal namespace documents, for one
target locale or `locale=all`. Spend, two-phase (unconfirmed plans and launches nothing), and it
**refuses the offline mock engine** exactly like document translate.
The response retains `skipped_incompatible` as a reserved empty list. FULL_TEXT
namespace documents now accept tm_match and use unverified TM offers (#155);
they are no longer skipped for this pairing.

This family is the anti-Lokalise wedge and gets the most careful descriptions in the whole
registry. Three contract facts are **pinned by the guard** (§9) into `key_changes`' description
because getting any of them wrong silently rots every downstream file:

- store `next_cursor` after every page and loop while `has_more`;
- `resync_required: true` means **re-bootstrap from `since=0`**, it does *not* mean "nothing
  changed";
- a deletion is an **entry** with `op: "deleted"`, not an absence.

`upsert_keys` returns `errors` verbatim: a 200 is not a promise that every key landed.

### Family E - Workflows and runs (5)

| # | Tool | Tier | Routes |
|---|---|---|---|
| 17 | `list_workflows` | read | `GET /api/workflows`, `GET /api/workflows/{wid}` |
| 18 | `run_workflow` | spend | `POST /api/workflows/{wid}/run`, `/run-batch`, `POST /api/batches/{batch_id}/rerun`, `/retry-failed` |
| 19 | `work_status` | read | `GET /api/runs/{run_id}`, `GET /api/batches/{batch_id}`, `GET /api/documents/{doc_id}/segments` |
| 78 | `ledger_summary` | read | `GET /api/prompt-tap/actions`, `GET /api/prompt-tap/actions/{action_id}/summary` |
| 80 | `ledger_runs` | read | `GET /api/prompt-tap/runs`, `GET /api/prompt-tap/runs/{run_id}` |
| 35 | `create_workflow` | write | `POST /api/workflows`, `POST /api/workflows/{wid}/steps` |
| 36 | `edit_workflow` | write | `POST /api/workflows/{wid}/steps`, `/{sid}/edit`, `/{sid}/delete`, `/{sid}/move` |
| 52 | `cancel_workflow_run` | write | `POST /api/runs/{run_id}/cancel` |
| 40 | `manage_workflow_group` | write | `GET /api/workflow-groups`, `GET /api/workflow-groups/{gid}`, `POST /api/workflow-groups`, `POST /api/workflow-groups/{gid}/add`, `POST /api/workflow-groups/{gid}/default` |
| 41 | `clone_workflow` | write | `POST /api/workflows/{wid}/clone` |

`work_status` is one tool because "where does my work stand?" is one question whether the answer
lives on a run, a batch or a document's segments. Its description carries two pins: **read
`qa_flags` alongside `status`**, and **`partial` is not success**.

**Workflow authoring (SPEC-workflow-authoring-api, added after waves 1+2).** `create_workflow`
builds a workflow and, optionally, its full ordered step list in ONE call (per-step accounting
in the response - a rejected step is reported and the rest still land). `edit_workflow` changes
a step, one action per call (`add_step` / `edit_step` (PARTIAL) / `delete_step` / `move_step`).
Both are **write tier, not spend** - authoring bills nothing. They are the /api arm of the
now-`--remote`-capable `workflow create/add-step/edit-step/delete-step/move-step` CLI verbs;
`edit_workflow`'s `delete_step` is a graph-authoring edit (can_tune_ai, fail-closed, reversible),
carved out of the NEVER_COVER delete-suffix rule by name - the whole-object workflow/group
deletes stay is_admin and uncovered.

### Family F - Language assets (9)

| # | Tool | Tier | Routes |
|---|---|---|---|
| 20 | `list_assets` | read | `GET /api/glossaries`, `/{gid}`, `/{gid}/candidates`, `/{gid}/export`, `GET /api/style-guides`, `/{gid}` |
| 21 | `edit_glossary` | write | `POST /api/glossaries`, `/{gid}/terms`, `/{gid}/terms/{eid}/edit`, `/{gid}/import`, `/{gid}/approve`, `/{gid}/unapprove`, `/{gid}/pairs`, `/{gid}/content-type`, `/{gid}/candidates/{cid}/accept`, `/{gid}/candidates/{cid}/drop` |
| 22 | `extract_terms` | spend | `POST /api/glossaries/{gid}/extract` |
| 23 | `manage_style_guide` | write | `POST /api/style-guides`, `/{gid}/update`, `/{gid}/set-status` |
| 24 | `search_tm` | read | `GET /api/tm`, `/api/tm/stats`, `/api/tm/{entry_id}` |
| 83 | `search_workspace` | read | `GET /api/search` |
| 84 | `manage_formality` | write | `GET /api/formality`, `GET /api/formality/registers`, `POST /api/formality` |
| 85 | `manage_custom_fields` | write | `GET /api/custom-fields`, `POST /api/custom-fields`, `PATCH /api/custom-fields/{fid}` |
| 86 | `set_project_fields` | write | `PATCH /api/projects/{pid}/fields` |
| 87 | `project_notifications` | write | `GET /api/notify/channels`, `GET /api/notify/routes`, `POST /api/notify/routes`, `GET /api/notify/projects/{pid}`, `POST /api/notify/projects/{pid}/mute` |
| 25 | `manage_tm` | write | `POST /api/tm/{entry_id}/edit`, `POST /api/projects/{pid}/tm/import`, `GET /api/projects/{pid}/tm/export` |
| 38 | `attach_tm_source` | write | `GET /api/projects/{pid}/tm-sources`, `POST /api/projects/{pid}/tm-sources`, `POST /api/projects/{pid}/tm-sources/remove` |
| 39 | `pin_glossary` | write | `GET /api/projects/{pid}/glossaries`, `POST /api/projects/{pid}/glossaries`, `POST /api/projects/{pid}/glossaries/remove` |

`attach_tm_source` and `pin_glossary` (SPEC-full-remote-parity Wave R1) each cover list/add/remove
over the project's TM-source references and glossary pins. Both are **write, not spend** (they
attach references, bill nothing) and **additive-only**: remove of a *pin or source link* is a
reversible re-attach, within the additive-only law, while content deletes stay excluded. A source
must be the same customer or a customer-less house project; a foreign-customer source or glossary
is refused, never attached.

`edit_glossary`'s description must state the rung rules it cannot enforce: **a draft glossary
contributes nothing**, and approving is `can_tune_ai` (admin or linguist only - not a rank
threshold), so a manager's key 403s here while passing `can_manage` elsewhere.

### Family G - Operational reads + connectors (10)

| # | Tool | Tier | Gate | Routes |
|---|---|---|---|---|
| 32 | `run_report` | read | - | `GET /api/reports/run` |
| 33 | `list_connectors` | read | - | `GET /api/connectors` |
| 34 | `list_images` | read | `image_localization_enabled` | `GET /api/images`, `/{doc_id}`, `/{doc_id}/export` |
| 42 | `translate_image` | spend | `image_localization_enabled` | `POST /api/images/{doc_id}/translate` |
| 43 | `connector_status` | read | - | `GET /api/connectors/health`, `GET /api/connectors` |
| 44 | `connector_pull` | write | - | `POST /api/connectors/{cid}/pull` |
| 45 | `connector_translate` | spend | - | `GET /api/connectors`, `POST /api/connectors/{cid}/{kind}/translate` (jira/confluence/document360/freshdesk/zoho/strapi/contentful/shopify/hubspot/intercom/salesforce/notion/helpscout/wordpress) |
| 46 | `connector_push` | spend | - | `GET /api/connectors`, `POST /api/connectors/{cid}/push` |
| 47 | `connector_sync` | spend | - | `GET /api/connectors`, `POST /api/connectors/{cid}/sync` |
| 48 | `connector_retry` | spend | - | `GET /api/connectors`, `POST /api/connectors/{cid}/retry` |
| 49 | `manage_users` | write | - | `POST /api/users`, `/{uid}/role`, `/{uid}/enable`, `/{uid}/disable` (Wave R2; NO erase/reset-password/api-key mint) |
| 50 | `manage_admin_ops` | write | - | `POST /api/routing/rules`, `/{rule_id}/toggle`, `POST /api/budgets`, `POST /api/lqa/rulebook` (Wave R2; deletes/config/credentials host-only) |
| 51 | `manage_install` | write | `remote_admin_enabled` | `GET/POST /api/admin/config`, `POST /api/admin/engines`, `/{eid}/update`, `/{eid}/test`, `POST /api/admin/providers/{name}/key/clear`, `GET /api/admin/providers/{name}/key` (Wave R3, `admin` scope; NON-secret only - every secret-SET path is /api-only, refused client-side) |
| 52 | `vendor_approve_quote` | spend | - | `GET /api/vendors/dispatches/{did}`, `POST /api/vendors/dispatches/{did}/approve-quote` (Wave E R3; two-phase confirm - the plan reads the leg's recorded quote + estimate, the confirm authorizes the quoted human spend and flips QUOTED→DISPATCHED) |
| 53 | `vendor_reject_rework` | spend | - | `GET /api/vendors/dispatches/{did}`, `POST /api/vendors/dispatches/{did}/reject-rework` (Wave E R4; two-phase confirm - the plan reads the leg's state + rework round, the confirm re-dispatches billable human rework or escalates to a human at the cap; injects NO content) |

`list_images` earns its place partly as the **worked example of the feature-gate field**: image
localization is Beta and kill-switchable, so this is the family that proves a switched-off feature
degrades to a relayed 404 rather than a confusing tool error. `translate_image` (Wave R1b) joins it
as the family's one **spend** tool: two-phase, `image_localization_enabled`-gated (404 when off),
and mock-refusing (409) before it re-renders anything.

The **connector tools (Wave R4)** are the NEVER_COVER amendment (see §5a): the connector `/api`
routes already existed and were all NEVER_COVER; R4 moves the write/spend subset onto tools while
keeping the destructive-into-a-tenant verbs locked. `connector_pull` is **write, not spend** - a
pull fetches external content INTO Localization OS and runs no MT, touching nothing external.
`connector_translate` dispatches on the connection's own `kind` (read from `GET /api/connectors`) to
the right per-platform `{kind}/translate` route, so the agent names a connection id and item ids, not
a platform verb. `connector_push`/`sync`/`retry` are two-phase spend. Every connector spend plan is
**GET-only**: it names the external target (the connection's kind/name/status/project) off the scoped
`GET /api/connectors` and surfaces the `allow_mock_egress` posture, but it does **not** preview the
record/issue/page count - the count/preview routes (e.g. `jira/count`) are POSTs a read-only plan
cannot call, so the count is resolved at run time and bounded server-side by `MAX_WRITES_PER_RUN`.
**Still NEVER_COVER** (each still a live-tenant write with no tool): Airtable record create/delete
(destructive into the tenant - the one verb pair the decision record keeps out), the whole
Airtable webhook family, `jira/count` + `jira/translate-pending`, `salesforce/rediscover-fields`, and
the three `resolve-link` id-map edits.

`manage_workflow_group` and `clone_workflow` (Wave R1b) complete the workflow-authoring surface for
the agent: a group holds one variant per language pair (create/list/show/add/set-default, bills
nothing), and a clone deep-copies a workflow's whole step graph into an editable copy. Both are
**write, not spend**, and a foreign-org/customer group or workflow is a 404, never a launch.

> **On the tool count.** The brief said ~20–30; the honest number after accounting is 72 (34 from waves 1+2, plus create_workflow + edit_workflow, the R1a content-parity trio, the R1b trio, the Wave R4 connector six, the Vendor Wave E tools, approve_segment and the Wave 3 collaboration tools). The
> pressure to hit 30 is pressure to stuff `action` enums: a tool whose enum has ten branches is a
> router, not a task, and models pick the wrong branch. Two tools that each do one thing cost the
> model ~60 tokens of schema and save it a category error. Where an enum *is* honest - one task at
> different lifecycle points, as in `configure_project` and `edit_glossary` - it is used.

### Queries (Wave 3, SPEC-wave3 §4.4) - part of the **documents** family (6)

These six join Family C in the registry (`family="documents"`); they are NOT an eighth family
(the registry's `FAMILIES` is a fixed seven and adding one is out of scope). Documents therefore
holds 12 tools total. Listed in their own block only for readability.

| # | Tool | Tier | Gate | Routes |
|---|---|---|---|---|
| 54 | `list_queries` | read | `queries_enabled` | `GET /api/queries` |
| 55 | `get_query` | read | `queries_enabled` | `GET /api/queries/{qid}` |
| 56 | `open_query` | write | `queries_enabled` | `POST /api/queries` |
| 57 | `reply_to_query` | write | `queries_enabled` | `POST /api/queries/{qid}/posts` |
| 58 | `set_query_status` | write | `queries_enabled` | `POST /api/queries/{qid}/status` |
| 59 | `assign_query` | write | `queries_enabled` | `POST /api/queries/{qid}/assign` |
| 60 | `my_work` | read | `worklist_enabled` | `GET /api/worklist` |
| 61 | `list_lqa_findings` | read | â | `GET /api/documents/{doc_id}/lqa/findings` |
| 62 | `log_lqa_finding` | write | â | `POST /api/documents/{doc_id}/lqa/findings` |
| 63 | `edit_lqa_finding` | write | â | `POST /api/lqa/findings/{fid}` |
| 64 | `withdraw_lqa_finding` | write | â | `POST /api/lqa/findings/{fid}/withdraw` |

The six query tools thinly relay `/api/queries*` (SPEC-wave3 collaboration layer): threaded
questions/comments scoped to a project, document, segment or key, with an `open → answered →
closed` lifecycle. Every one is gated on `queries_enabled` (a `"removed"` kill switch, so a
switched-off feature relays a 404 the same way `list_images` proves for image localization). RBAC
is the server's job on the key's user (a viewer may open/reply/answer; author/assignee/reviewer+
close/assign), and a thread outside the caller's scope is a 404, never a 403.

The four reviewer-LQA-finding tools thinly relay the core LQA finding routes (SPEC-wave3 Â§6.5): a human reviewer logs / lists / edits / withdraws MQM findings on a segment. They are CORE (no feature gate); the writes are reviewer+ (edit/withdraw own-row or manager+) server-side, and a finding never changes the machine LQA score or a workflow gate. Managing findings is NOT running a scoring â scoring stays web-only.

The `my_work` tool (SPEC-wave3 §8.4, LC-5) thinly relays `GET /api/worklist`: the key owner's
cross-project reviewer worklist (paused review steps that name them, queries assigned to /
answered for them, documents needing quality attention). It is gated on `worklist_enabled` (a
`"removed"` kill switch, so a switched-off feature relays a 404). It answers for the key's OWN
user; a shared connector token has no user and is refused 400 server-side, never an empty list.
---

## 4. Safety tiers

| Tier | Rule | Population (live `registry.py`, guard-pinned) |
|---|---|---|
| **read** | Unrestricted. No confirmation, no echo. | 26 tools |
| **write** | Normal call. Creates, imports, edits, attaches, pins - additive or reversible through the same surface. | 33 tools |
| **spend** | Bills the operator's account. Requires `confirm: true`; **without it the tool performs READ calls only and returns a plan** (resolved engine identity, locale count, segment count, live-vs-mock, budget state, and the API's own estimate where one exists). | 13 tools: `submit_translation`, `translate_text`, `translate_keys`, `translate_image`, `run_workflow`, `extract_terms`, `connector_push`, `connector_sync`, `connector_retry`, `connector_translate`, `project_run`, `vendor_approve_quote`, `vendor_reject_rework` |
| **destructive** | Deletes, purges, revocations, irreversible state changes. | **0 tools.** The tier exists in the registry schema so it can be expressed later; today it is empty by design and the guard asserts it is. |

**Say the honest thing about `confirm`.** An agent can set `confirm: true` by itself. The
parameter is a **disclosure mechanism and a speed bump, not a security control**. The real
controls are all server-side and already shipped: the mock-engine 409, department budgets (429),
`spend_cap_usd`, per-identity rate limits, the acknowledged-estimate 409, RBAC, and org pinning.
The tier system's job is to make an agent *tell the user what it is about to spend*, and to keep
the irreversible verbs out of the tool list entirely - which is the only guarantee that does not
depend on a model behaving.

**Kill switches:** a tool whose feature is off relays the API's 404 verbatim (§2). No tool is
hidden, no tool is special-cased, and the refusal shape is indistinguishable from "no such
object" - exactly as the product intends.

---

## 5. Coverage accounting - the three-way rule

> Every `/api` route is **(a)** named by a tool, **(b)** reachable through a documented composite
> tool, or **(c)** in the exemption table with a one-line justification. There is no fourth
> outcome, and a new route with no accounting fails the suite.

Every `/api` route is accounted for: `tests/verify_mcp_parity.py` pins the covered and exempt sets and fails on any unaccounted route.

### 5a. Exemption table (draft)

| Routes | n | Why not in v1 |
|---|---|---|
| `POST /api/account/skin`, `/onboarding-reset` | 2 | Human UI preferences. An agent setting somebody's theme is noise. |
| `GET /api/apikeys` | 1 | **Credential enumeration.** An MCP client must never list credentials, including its own siblings. |
| `GET /api/connectors/health` | 1 | Unauthenticated liveness probe. Nothing for an agent to do with it. |
| `GET /api/connectors/{cid}/airtable/webhook` + the Airtable record + webhook family, `jira/count`, `jira/translate-pending`, `salesforce/rediscover-fields`, the three `resolve-link`s | 14 | **Wave R4 amendment (2026-08-07):** the generic `pull`/`push`/`sync`/`retry` and every per-platform `{kind}/translate` route moved OUT of NEVER_COVER onto the connector tools (write/spend, two-phase). What stays: Airtable record create/delete (**destructive into the external tenant** - the one verb pair the decision record keeps out), the Airtable webhook family (standing config), `jira/count` (a POST a read-only plan cannot call) + `jira/translate-pending` (a human-curated release), `salesforce/rediscover-fields`, and the three `resolve-link` id-map edits. Each remains a live-tenant write with no tool. |
| `POST /api/content-types` · `/{cid}/rename` · `/{cid}/delete` | 3 | Taxonomy administration, set once by an operator. The read is covered. |
| `POST /api/customers/{cid}/` `archive` · `unarchive` · `delete` · `rename` · `pairs/import` | 5 | `delete`/`archive` are destructive tenancy acts; the rest are operator admin with no agent demand. |
| `GET /api/documents/{doc_id}/paragraphs`, `POST .../paragraphs/{index}/revert`, `/revert-all-damaged`, `/{index}/save` | 4 | The FULL_TEXT hand-repair surface for a human in the editor. `revert` overwrites a translation with SOURCE text - a recovery act, not an edit. |
| `POST /api/glossaries/{gid}/delete` · `/rename` · `/set-customer` | 3 | Destructive; a one-way tenancy move; and cosmetic admin. |
| `POST /api/jobs/run` | 1 | A generic "run any job kind" executor **bypasses every task-shaped tool and every tier in this design.** Excluding it is the point. |
| `DELETE /api/keys/{key_id}`, `POST /api/keys/{key_id}/restore` | 2 | A key delete emits a **tombstone every downstream CI consumer acts on** - irreversible in effect though soft in storage. `restore` exists only to undo a delete an agent cannot perform. |
| `POST /api/langsets` · `/{sid}` · `/{sid}/delete` | 3 | Operator presets. Agents read them (covered) and apply them to a project (covered by `configure_project`). |
| `POST /api/memories/{mid}/invalidate` · `/invalidate-since` · `/{mid}/purge` | 3 | Revocation and hard erase. `invalidate-since` is a rollback verb whose blast radius is a scope plus a timestamp; `purge` is `is_admin` and irreversible. |
| `POST /api/projects/{pid}/delete` · `/remove-locale` | 2 | Both demand a **typed echo** (the project name; the locale). `llm.md`: *"Never fabricate a confirmation."* A tool that takes the echo as a parameter hands the agent the only safety the design has. |
| `GET/POST /api/projects/{pid}/glossary`, `POST .../glossary/import` | 3 | The legacy per-project glossary, superseded by module glossaries (covered). Shipping both teaches an agent the wrong one. |
| `POST /api/prompt-templates/{tid}/rename` · `/delete` | 2 | Authoring surface. Step authoring has no `/api` route at all, so half a template CRUD is worse than none. |
| `GET /api/routing/rules/{rule_id}` | 1 | An orphan read: the rest of routing-rule CRUD is local-only, so this cannot do anything useful alone. |
| `POST /api/runs/{run_id}/resume` | 1 | **The single most important exclusion.** It is the only way past a `human_review` pause. `llm.md`: *"Do not invent an approval."* A tool here would let a model write the review text and sign off on it. |
| `POST /api/segprofiles/{pid}/rename` | 1 | Orphan admin route; segmentation-profile CRUD is otherwise local-only. |
| `POST /api/style-guides/{gid}/delete` | 1 | Destructive. |
| `POST /api/tm/bulk-delete` · `/scrub-hedges` · `/{entry_id}/delete` | 3 | Destructive memory edits. `bulk-delete` guards an unfiltered wipe with `confirm_all` - precisely the echo an agent must not fabricate. |
| `POST /api/workflows/{wid}/delete` · `/rename`, `/api/workflow-groups/{gid}/delete` · `/rename` | 4 | Destructive/authoring. The API cannot build a workflow, so it should not be able to delete one. |
| **Total** | **69** | |

Two properties of this table matter more than its contents. It is **per-route, in one reviewable
place, with a reason** - the shape `_API_DOC_EXEMPT` and `DOC_EXEMPT` already use. And an entry
naming a route that no longer exists is **itself a failure** (§7 check 4): a stale exemption
silently un-covers whatever route later lands on that path.

---

## 6. The registry - the one file a feature developer edits

`integrations/mcp-server/registry.py`

**Why a Python module and not JSON/YAML/TOML:** the input schema is a JSON-Schema dict and is
cheapest to write as one; the guard imports it in-process with zero parsing dependencies; and this
file is documentation as much as data, so it needs comments. The hard constraint that buys
everything else:

> **`registry.py` imports nothing.** Not `mcp`, not `app`, not `httpx`. It is data plus stdlib.

That is what lets `tests/verify_mcp_parity.py` run in every environment. `tests/verify_mcp.py`
today *skips cleanly* when the SDK is absent - correct for a protocol test, fatal for an
accounting guard. **The accounting guard must never skip.**

```python
# integrations/mcp-server/registry.py   (imports nothing but dataclasses/typing)

@dataclass(frozen=True)
class Tool:
    name: str                    # MCP tool name, unprefixed
    family: str                  # one of FAMILIES
    summary: str                 # <=240 chars, what the model sees
    tier: str                    # "read" | "write" | "spend" | "destructive"
    scope: str                   # API scope the routes need: "api" | "docs" | "keys"
    routes: tuple[tuple[str, str], ...]   # (METHOD, path) this tool may call
    input_schema: dict           # JSON Schema, type=object
    feature_gate: str | None = None       # a real settings key, or None
    pins: tuple[str, ...] = ()   # substrings the summary MUST contain (see §9)

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="key_changes",
        family="keys",
        summary=(
            "Read a key project's change feed since a cursor. Store next_cursor after "
            "every page and loop while has_more. resync_required means re-bootstrap "
            "from since=0 - it never means 'nothing changed'. A deletion arrives as an "
            "entry with op=deleted, not as an absence."),
        tier="read",
        scope="keys",
        routes=(("GET", "/api/projects/{pid}/changes"),),
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Key-based project id."},
                "since": {"type": "integer", "default": 0,
                          "description": "Your stored cursor; 0 bootstraps everything."},
                "limit": {"type": "integer", "default": 500, "maximum": 1000},
            },
            "required": ["project_id"],
        },
        feature_gate="key_projects_enabled",
        pins=("next_cursor", "resync_required", "op=deleted"),
    ),
    ...
)

#: Routes deliberately NOT reachable from any tool. Per route, with a reason.
#: A stale entry (naming a route that no longer exists) is an ERROR - see §5a.
EXEMPT: dict[tuple[str, str], str] = {
    ("POST", "/api/runs/{run_id}/resume"):
        "Human-review sign-off. llm.md: 'Do not invent an approval.'",
    ("POST", "/api/jobs/run"):
        "Generic job executor - bypasses every task-shaped tool and every tier.",
    ...
}
```

`server.py` becomes a dispatcher: for each `Tool` it registers a FastMCP tool from
`name`/`summary`/`input_schema` and binds it to a handler function looked up by name in a
`HANDLERS` dict. Adding a tool is: one `Tool(...)` entry + one handler. Adding a route to an
existing tool is: one tuple in `routes`. Deciding a route needs no tool is: one line in `EXEMPT`.

---

## 7. The guard - `tests/verify_mcp_parity.py`

Built on `verify_help.py`'s machinery (`_real_api_routes(app)` walks `app.routes`, not the OpenAPI
schema, so a route with `include_in_schema=False` is still held to being accounted for).

| # | Check | Fails when |
|---|---|---|
| 1 | **Route existence** - every `(METHOD, path)` in every tool's `routes` is a route this build serves | a tool points at a route that was renamed or deleted |
| 2 | **Method match** - the verb must match, not just the path | the likeliest defect: every token real, only the method wrong (`verify_help` proved this class is worth its own assertion) |
| 3 | **Coverage closes** - `covered ∪ exempt == real`, `covered ∩ exempt == ∅`, `len(covered) + len(exempt) == len(real)` | **a new `/api` route ships with no tool and no exemption.** This is the check R2 buys. |
| 4 | **No stale exemptions** - every `EXEMPT` key is a real route | an exemption outlives its route and silently un-covers the next route on that path |
| 5 | **Schemas are valid** - each `input_schema` is `type: "object"` with a `properties` dict, `required ⊆ properties`, every property typed, every property described | a tool ships with an untyped or undocumented parameter (no `jsonschema` dependency - hand-validated, ~25 lines) |
| 6 | **Fields are real** - `tier ∈` the four tiers, `scope ∈ {api, docs, keys}`, `family ∈ FAMILIES`, and `feature_gate` (when set) is a key in `app.services.settings.DEFAULTS` | a typo'd kill-switch name makes a tool description lie about what turns it off |
| 7 | **Registry ↔ server, both directions** - every `Tool.name` has a handler, and every tool the server registers with FastMCP is in the registry | a tool ships without a registry entry (the `verify_cli_parity` check-(b) shape, which is what actually forces the table to stay complete) |
| 8 | **Descriptions cannot lie** - every `/api/...` path mentioned in a `summary` is in that tool's `routes`; every string in `pins` appears in the `summary` | a description promises a route the tool cannot call, or a rewrite drops a load-bearing contract sentence (`resync_required`, "does not block", "refuses the offline mock") |
| 9 | **Tier invariant** - no tool declares `tier="destructive"` in v1 | a destructive tool sneaks in without a spec change |
| 10 | **Runs without the SDK** - the suite imports `registry`, `app.main` and `app.services.settings` only | someone makes the accounting guard skippable |

**Mutation-proven, both directions** (the house requirement - a guard that only catches invented
routes would have passed on the 98-of-158 page this pattern exists to have caught). The suite
forges, and asserts each goes red:

1. a tool route that does not exist → check 1;
2. a real path under the wrong verb → check 2;
3. a real route removed from both `covered` and `EXEMPT` → check 3 *(the load-bearing one)*;
4. an exemption for a nonexistent route → check 4;
5. a `pins` string deleted from a summary → check 8.

Check 7's server side needs the SDK, so it is **conditional**: skipped with a printed note when
`mcp` is absent, while checks 1–6 and 8–10 always run. The accounting can never be skipped; only
the protocol wiring check can.

---

## 8. The dev-lifecycle rule (requirement R2, in one paragraph)

**LANDED IN WAVE 0** (not wave 3, as originally scheduled). The rule guards the
lifecycle, so it ships with the guard that enforces it rather than with the packaging
of the thing it protects - a law that arrives after three waves of code is a law three
waves of code were written without. It is, verbatim,
in the header of `integrations/mcp-server/registry.py`, where the person editing the file
will actually read it. Wave 3 no longer carries it; the text, for the record:

> **MCP accounting travels with `/api`.** Every new or changed `/api` route ships, in the same
> change, with its MCP accounting: either a `routes` entry on a tool in
> `integrations/mcp-server/registry.py`, or a one-line justification in that file's `EXEMPT` map.
> `tests/verify_mcp_parity.py` fails otherwise, the same way `verify_cli_parity` fails a CLI leaf
> with no capability entry and `verify_help` fails a route missing from the API reference. Adding
> a tool is one `Tool(...)` entry plus one handler; exempting a route is one line with a reason.
> The MCP server is the fourth surface, and like the other three it is not optional.

**One correction to the brief:** `docs/GUIDE-NEW-FEATURES.md` is a *user-facing* guide ("How to
actually use the new stuff") - it is the wrong home for a developer rule. The developer rule goes
in the playbook (above) and in the header comment of `registry.py`, where the person editing the
file will actually read it. GUIDE-NEW-FEATURES gets a *user-facing* section when the server
ships: "Drive Localization OS from Claude Desktop", pointing at the README.

---

## 9. Keeping descriptions truthful

The docs campaign proved prose rots; this design assumes it will. Three layers, cheapest first:

1. **Minimal by default.** A tool summary says *what this tool does and what the caller must not
   assume*. It never re-explains the data model, the glossary rung rules, plural handling, or the
   error vocabulary. There is one place for that and it is the resource (§10).
2. **Pinned where a claim is load-bearing.** The `pins` field (check 8) is the cheap analogue of
   `verify_help`'s needle list. Roughly a dozen pins in total, e.g. `key_changes` →
   `resync_required` / `next_cursor`; `job_status` → "does not block"; `submit_translation` →
   "refuses" + "mock"; `work_status` → `qa_flags`.
3. **Semantics by reference, not by copy.** Every tool description ends with the same sentence
   pointing at the `locstudio://guide/llm` resource. That card is *already* pinned to the real
   router by `verify_help._check_llm_card_routes` (mutation-proven both ways). Pointing at it
   inherits a guarantee instead of forging a second one that can drift from the first.

---

## 10. Resources

| URI | Content | Source of truth |
|---|---|---|
| `locstudio://guide/llm` | The "Are you an LLM? Read here" card - the whole product at driving depth | `app/capabilities/llm.md`, already guard-pinned |
| `locstudio://openapi` | The live `GET /openapi.json` of the connected install | generated from the routes; cannot drift |

The card is the server's self-description: an agent reads it once and then drives. Serving the
*live install's* card matters - a remote MCP process must not describe a build it is not talking
to - which raises the one genuine gap: **there is no `/api` route that serves a capability card**.
`/help/{module}` renders HTML for a human.

**Stated trade (wave-1 review, Finding 9):** the route serves a card for a module whose
feature is switched off, and its 404 lists the real slugs. Both are deliberate. A card is
repo-tracked *documentation*, identical in every install of a given build, so it reveals
nothing about this tenant - and the slug list turns a typo into a fix instead of a guessing
game. Crucially it is **not a live oracle**: reading the keys card tells you the product has
key projects, not whether `key_projects_enabled` is on here, and the actual key routes still
answer an indistinguishable 404 either way. If a future card ever carries install-specific
content, that changes and this trade must be revisited.

**Recommendation:** add one read route, `GET /api/capabilities/{slug}` (base `api` scope,
returns the card's front-matter + body as JSON, 404 on an unknown slug), and account for it in the
registry as a `whoami`-family route. Cost: ~15 lines over the existing `assist.load_card` seam,
plus one row in `/help` Part 2 (`verify_help` will demand it - correctly). The alternative,
bundling the `.md` file next to `server.py`, is free but ships a card from the developer's checkout
while the tools call prod. See Q2.

---

## 11. SDK, transport, deployment

**SDK:** the official Python MCP SDK, `mcp==1.28.1`, `mcp.server.fastmcp.FastMCP` - already
pinned, already used, already tested over real stdio. No change.

**Transports:**

| Mode | Transport | Auth | Who it is for |
|---|---|---|---|
| Local / Claude Desktop | **stdio** (shipped) | `LOC_STUDIO_TOKEN` env (`lsu_`) | a developer's own machine; every self-hosted operator |
| Remote / own agent runtime | **streamable-HTTP** (`--transport streamable-http`, a flag on the same `server.py`) | **a per-request `lsu_` token in each request's `Authorization: Bearer` / `X-Loc-Token` header (REALIZED in wave 4)**; `LOC_STUDIO_TOKEN` is the fallback | a self-hosted customer wiring Localization OS into Claude Code, Codex, or their own tooling, behind their own TLS |
| claude.ai connector directory | streamable-HTTP + **OAuth 2.1** (DCR + PKCE + RFC 9728) | corporate IdP | **not v1** - this is `PLAN` Phase C2, unchanged |

> **Wave 3 → wave 4 (2026-08-05).** Wave 3 shipped the transport flag but the
> listener could only authenticate itself with the *process* env token; the
> per-request header this row always described was a documented divergence (README:
> "the HTTP listener has no auth of its own"). **Wave 4 realizes it:** each request's
> `Authorization: Bearer lsu_...` (or `X-Loc-Token`) header is forwarded as *that*
> request's `/api` credential - captured in a task-isolated contextvar, so one
> listener serves many users with no cross-request leakage - and the server still
> validates nothing itself, relaying the app's own `401`/`403`. It performs no
> listener-level OAuth `401` (that stays Phase C2, and a shared-secret `401` would
> block the token-free `initialize`/`tools/list` handshake), so a missing token is a
> clean per-tool error and TLS you control (your edge or proxy) is the boundary.
> This **supersedes the wave-3 "no listener auth" divergence note.**

Be precise about the limit, because it is the thing most likely to be misheard: the remote flag on
its own is not a *directory* claude.ai connector (that listing needs OAuth - Phase C2/D). It **is**,
however, the **server half** of a claude.ai *static-per-request-header* connector - see **decision
record 10 (2026-08-06, Path A)** for that reality (server half built + verified; the claude.ai half
is a gated beta with an open bug, `anthropics/claude-ai-mcp#644`). Shipping the flag in v1 costs
almost nothing and unlocks the self-hosted "point Claude Code at my TMS" story today; listing in the
Anthropic MCP Registry waits for Phase C2/D.

**Deployment:**

- *Self-hosted (the default):* nothing to deploy. `pip install -r requirements.txt` already brings
  `mcp`; the README's `claude_desktop_config.json` block is the whole setup. Point
  `LOC_STUDIO_BASE_URL` at localhost or at their own instance.
- *Hosted installs:* the MCP server is a **client**, so nothing needs to change on the server for v1: run
  `server.py` locally with `LOC_STUDIO_BASE_URL=https://your-host` and an `lsu_` key, and every tool works
  against that install immediately. Running it *as* a second process next to the app is only needed for the
  remote-HTTP mode and is an operator choice, not part of the v1 build.

---

## 12. Security

**What the token is.** A personal `lsu_` key, SHA-256-hashed at rest, carrying scopes and RBAC,
sent in `X-Loc-Token` - **header only, never a query string** (already correct in `server.py`;
query strings land in proxy and access logs). The shared `lsk_` connector token is rejected by
every document, key and remote-CLI route and must never be used here.

**Scope minimalism.** The registry declares each tool's required scope, so the server can print a
one-line startup report - *"token holds `api docs`; 5 keys-family tools will 403"* - instead of
letting the model discover it as an undiagnosable 403 mid-task. Recommended configs: a
documents-only key (`api docs`) for the "translate this" flow; a CI/sync key (`api keys`) for the
Delta loop; both only where an operator genuinely drives both.

**What an MCP client can never do**, structurally:

- **Change configuration.** There is no settings API. Kill switches, provider credentials, spend
  caps and every setting are local-only. An API key changes content, never behaviour.
- **Cross an org.** A key resolves to its owner's home org; foreign objects answer 404, not 403;
  there is no super-admin bypass on `/api`.
- **Delete anything.** Zero destructive tools in v1 (§4), guard-asserted (check 9).
- **Enumerate credentials.** `GET /api/apikeys` is exempt.
- **Approve a human review**, or write into a live
  external tenant. All exempt, each with its reason recorded.
- **Escape the output directory.** `get_result` writes files derived from the `Content-Disposition`
  header, which is attacker-influenceable - the audit
  `AUDIT-keys-live-qa-2026-08-03.md` found that `?namespace=../../evil` survives the server-side
  download-filename sanitizer (`/` and `..` are not stripped). The MCP server's
  `_filename_from_disposition` already ends in `Path(val).name`, which neutralises it on this path.
  **That line is a load-bearing security control, not tidiness** - it must be commented as such and
  get a regression test in the parity wave, and the server-side fix remains open on its own ticket.
- **Leak the token into a tool result.** Review lens for every wave: no error path may include
  request headers, and `_explain_status` must keep truncating server bodies.

**Residual risks, stated:** (1) an agent can set `confirm: true` itself - §4 says why that is
acceptable and what actually holds; (2) the tool surface is as trustworthy as the model driving it,
which is why the irreversible verbs are absent rather than gated; (3) remote-HTTP mode moves the
token into a client config file - a self-hosted operator's decision, documented, not defaulted.

---

## 13. Reconciliation with the earlier Claude Desktop MCP plan

| From the PLAN | This spec |
|---|---|
| §3 thin-facade architecture; MCP as a client of `/api`, no logic of its own | **ADOPTED verbatim** - it is the §2 architecture. |
| §3 platform constraints: no binaries in tool calls, ~1 MB result cap, ~4-min silent kill, no server push | **ADOPTED as design law.** Every read tool that could return bulk (`export_keys`, `get_result`) writes to `LOC_STUDIO_OUTPUT_DIR` and returns a path; no tool blocks; long work is job-polled. |
| §3 "MCP tool surface (outcome-shaped, deliberately small)" - the 6 demand-side tools | **ADOPTED and kept.** The five shipped tools are v1's outcome family, unrenamed. `create_upload` is **deferred** with the sandbox/upload-token loop it belongs to (Phase C). |
| §8.1 *"resist exposing TMS CRUD via MCP"* | **SUPERSEDED, deliberately and narrowly.** That rule was written for the *employee* front door, where CRUD is noise. This spec's driver is a **product-ops agent** working for the operator - the same audience `app/capabilities/llm.md` was written for. Two audiences, two tool families, one server. What survives of the rule: tools are task-shaped, not route-shaped (34 vs 158), and the CRUD that is *destructive* is still absent. |
| §7 Phase A (A-local stdio, docs-scoped token) | **SHIPPED.** This spec is what comes after it. |
| §7 Phase A-tunnel / C1 pilot / C2 OAuth+IdP / D reach | **DEFERRED, unchanged.** v1's remote-HTTP flag (§11) is not a substitute and is not claimed to be. |
| §6a S2 object-level authorization (creator-scoped jobs/documents, UUIDs) | **DEFERRED and re-flagged.** Not a v1 blocker (org + customer + membership scoping already applies to every route a tool calls), but it stays the gate for any multi-user front door. |
| §5 content routing (`RoutingRule`) | **NOT SURFACED.** `GET /api/routing/rules/{id}` is exempt (§5a); routing resolves *inside* the submit path where it belongs. |
| §10 open questions Q1–Q6 (pilot department, exposure model, IdP, TM-capture governance, naming, seat counts) | **STILL OPEN, untouched.** They gate Phase C, not this spec. |

**Net:** the PLAN owns the demand side (any employee, "translate this", chat as the front door)
and its Phases C–D remain the roadmap for it. This spec owns the supply side (an operator's agent
driving the product) and ships now on infrastructure the PLAN already built. They share one server,
one registry and one guard.

---

## 14. The v1 / v2 cut

**v1 (this spec):** 72 tools across 7 families · the registry · `verify_mcp_parity.py` · the two
resources · the streamable-HTTP flag · the playbook rule · README/`docs/CLI.md` cross-references.
Product-ops coverage with the continuous-loc loop first-class. **Touches `app/` in exactly one
place** (the optional `GET /api/capabilities/{slug}` of §10, pending Q2) - so the 213 existing
suites are unaffected by construction.

**v2 (explicitly deferred, in priority order):**

1. **The PLAN's Phases C–D** - sandbox upload/download loop, per-user OAuth against the corporate
   IdP, org-wide connector + skill distribution, MCP Registry listing.
2. **Connector writes** (22 routes) behind a per-connection allowlist.
3. **A read-only `review_queue` tool** - what is paused, what is `needs_review`, what a human must
   look at - the safe half of the resume exclusion (see Q4).
4. **Destructive tier population**, if ever, and only with the product's own typed echoes surfaced
   to the human rather than accepted as parameters.
5. **In-process `/mcp` mount**, if latency ever justifies it.
6. **MCP Apps in-chat review UI** (PLAN §8.6).
7. **Namespace hygiene on key writes** (wave-1 review, carried forward). `upsert_keys` and
   `import_keys` accept an arbitrary `namespace`, and a poisoned one (`"../../evil"`) is
   stored and then re-emitted in exports and in the change feed. The MCP server's own
   output-directory defence neutralises it *on its own download path*, but the CLI, the web
   export and every CI consumer read the same value without that defence, so this is an
   **app-side** validation gap that predates this build and is not the MCP server's to fix
   unilaterally. Wave 2 should either land the validation in `app/services/keys.py` (one
   seam, all four surfaces) or state why a namespace is free text.

---

## 15. Open questions (all answered; see the decision record)

**Q1 - Ship the remote (streamable-HTTP) flag in v1, or stdio-only until OAuth?**
*Recommendation: ship it,* documented as "for an agent runtime you control, behind your own
proxy," and explicitly **not** listed on claude.ai or in the MCP Registry until Phase C2. It is a
transport flag on a process that already exists; withholding it costs a real self-hosted use case
("point Claude Code at my TMS") and buys no safety the token model does not already provide.

**Q2 - Add `GET /api/capabilities/{slug}` so the LLM card is served by the install, or bundle the
`.md` next to the server?**
*Recommendation: add the route.* ~15 lines over the existing `assist.load_card` seam. Bundling
means a remote server describes the developer's checkout while its tools call prod - the exact
drift class this whole spec exists to prevent. It is also independently useful: any agent, not just
this server, can then fetch the driver's manual over HTTP.

**Q3 - Connector writes (22 routes): exempt in v1 as proposed?**
*Recommendation: yes, exempt.* They write into live Jira/Airtable/Zendesk tenants; repo law makes
those runs your explicit per-session call, and an MCP tool is the opposite of a per-session call.
v2 can add them behind a per-connection allowlist env var so a customer opts in one connection at a
time.

**Q4 - Should a `human_review` resume ever be an MCP tool?**
*Recommendation: never as a write.* It is the one place where a model could manufacture a human
approval, and the whole product's review perimeter rests on it. v2 gets a **read-only**
`review_queue` tool instead: show what is paused and where the human should click.

**Q5 - Spend policy: two-phase plan/confirm on every spend tool, or only on fan-out?**
*Recommendation: two-phase everywhere, but cheap.* An unconfirmed call is read-only and returns
the plan (engine identity, locale count, segment count, budget state). For a single-string
`translate_text` that is one extra round-trip for a call that costs cents - but it is also the only
mechanism that makes the agent *state the engine and the scope to the user* before spending, which
is your in-chat quality-transparency pitch (PLAN §8.2) arriving a year early. If the round-trip
annoys you in practice, the honest fallback is a mandatory `confirm: true` on the first call for
single-unit tools, keeping two-phase for `run_workflow` and `extract_terms`. **This is a spend decision, so it is yours.**

---

## 16. Build estimate - four waves

Each wave is one builder agent + the §5 adversarial-review gate, full suite green, merged before
the next starts.

| Wave | Content | Size | Why this order |
|---|---|---|---|
| **0 - the guard, first** | `registry.py` with **only the 5 shipped tools** + the complete 69-row `EXEMPT` table; `tests/verify_mcp_parity.py` with all ten checks and all five mutation proofs | **S** (~½ day) | This is the whole of R2 and it merges **before a single new tool exists**. From this commit on, forgetting the MCP is impossible - even if waves 1–3 slip indefinitely. |
| **1 - the loop** | Server dispatcher (registry-driven tool construction + `HANDLERS`); families A, B, C, D (16 tools); both resources; the startup scope report | **M** (~1 day) | Orientation → projects → documents → keys+Delta is the continuous-localization loop end to end. Ship a demoable agent before breadth. |
| **2 - breadth** | Families E, F, G, H, I (18 tools); the spend-tier two-phase plan shape applied uniformly | **M** (~1 day) | Mechanical once wave 1's dispatcher exists: each tool is a `Tool(...)` entry + a handler. |
| **3 - packaging** | `--transport streamable-http`; README rewrite; `docs/CLI.md` + `/help` cross-references; a user-facing GUIDE-NEW-FEATURES section | **S** (~½ day) | Documentation lands once the thing it describes is real. *(The lifecycle paragraph moved to wave 0 - see §8. Do not re-add it here.)* |

**~3 days of agent time, gated four times.** Risk is low and concentrated in wave 1 (the
dispatcher); waves 0, 2 and 3 are table-shaped work. The single largest risk is *scope creep into
the app* - the mitigation is the rule that v1 touches `app/` exactly once (Q2) and nothing else.

**Review lenses to hand the adversarial reviewer**, per wave: (a) *can the guard be made green
without doing the work?* - try commenting out an exemption, renaming a route, flipping a verb;
(b) *does any handler contain a decision?* - a filter, a retry, an inferred id, a fabricated
confirmation; (c) *does any error path echo the token or a request header?*; (d) *does a
kill-switched feature produce anything other than the API's own 404?*

---

## Changelog

- 2026-08-04: written as a proposal (read-only trace at main `a05d825`, 158 `/api` routes, 213
  suites). Not built at that point; Q1–Q5 were answered by the maintainer.
- 2026-08-04: **wave 0 BUILT** - `integrations/mcp-server/registry.py` (5 shipped tools ·
  6 covered routes · 69 permanent exemptions · 83 `planned:` markers = 158) and
  `tests/verify_mcp_parity.py`. The §8 lifecycle law landed here rather than in wave 3
  (see §8). Adversarial review ran 22 mutations and found the whole class the §7 checks
  had missed: **every field the registry declared about ITSELF was gameable, and the
  worst of them balanced perfectly** - moving `POST /api/runs/{run_id}/resume` out of
  `EXEMPT` onto a *read*-tier tool kept covered ∪ exempt == real while handing a model
  the human-review sign-off. Wave 0 therefore ships three tables §7 did not specify -
  `NEVER_COVER` (routes no tool may claim, ever), `SPEND_ROUTES` (only a spend tool may
  claim these), `MUTATING_TIERS` (a non-GET route forces a mutating tier) - plus a
  validated `CATEGORIES` vocabulary on every exemption (a justification of `""` or
  `"nah"` passed the first version), a one-way ratchet on the counts, and an AST read of
  `server.py`'s real signatures so the wave-1 debt list cannot be closed by deleting it.
  **Wave 1 inherits one hard constraint from the review:** `import_keys`' schema MUST NOT
  expose `cleanup` - `cleanup=true` retires every key absent from the uploaded file, a
  bulk delete that would contradict the `DELETE /api/keys/{key_id}` exemption. It is
  pinned on that route's `EXEMPT` entry and guard-enforced.

- 2026-08-04: **wave 1 BUILT** - the registry-driven dispatcher and families A–D.
  `server.py` is now ~950 lines of dispatcher + 16 handlers: FastMCP tools are
  constructed FROM `registry.py` (name, summary, input_schema, and a signature
  derived from the schema so the model is shown the reviewed text, not a pydantic
  rendering of it), bound to a handler of the same name in `HANDLERS`. **The
  two-phase spend contract lives in ONE function** (`_dispatch`): a `spend` tool
  called without `confirm=true` never reaches its handler, it gets `_plan`, and a
  contextvar makes any non-GET request raise while planning - so "nothing spends"
  is structural, not careful. Accounting moved **6 covered / 152 exempt (83
  planned) → 34 covered / 125 exempt (56 planned)** over **159** routes; the
  ratchet moved with it (`MIN_COVERED` 6→34, `MAX_PLANNED` 83→56, `MAX_DEBT` 2→0,
  `MIN_TOTAL_PINS` 8→22) and `SERVER_PY_WAVE1_DEBT` is empty *because the code
  agrees*, AST-verified. `GET /api/capabilities/{slug}` shipped (decision 7, the
  only `app/` touch) with its `/help` Part 2 row and its registry accounting in
  the same change - the lifecycle law applied to the wave that wrote its guard.
  Two guard changes were forced and are the reviewable act of this wave: the D9
  mutation now FORGES a signature drift (its real subject was paid off, and a
  check whose subject goes away must be re-proved against a synthetic one, not
  deleted), and the `import_keys` `cleanup` constraint MOVED from the EXEMPT
  entry - which no longer exists - onto the tool, as a new
  `_FORBIDDEN_SCHEMA_PROPS` check with both halves mutation-proven. 39 mutation
  proofs, up from 30. **Divergences from this spec, reality winning:** there is no
  `/api` route that translates a key project (the run is web-only - `llm.md`,
  Limits), so the keys family has no spend tool and wave 1 has exactly the 16
  tools of families A–D; `job_status` declares scope `docs` not `api` because its
  new listing leg is docs-gated; and the startup scope report cannot name the
  token's own grants (no route reports them and `GET /api/apikeys` is exempt), so
  it reports each tool's required grant and says so.

- 2026-08-04: **wave 1 fix round** after the adversarial review, which confirmed plan mode
  clean under a full-metadata census, `cleanup` unreachable, the contextvars sound and the
  ratchet exact - and found two blockers, both about a value rather than a name.
  **(1) The spend gate was a truthiness test.** `not kwargs.get("confirm")` meant the
  strings `"false"`, `"no"`, `"0"` and `"off"` all SPENT through any caller that skips the
  arg model - with spend-ledger rows to prove it - and the suite's own two-phase evidence
  came from exactly that seam. The gate is now `is not True`, the seam is renamed
  `call_tool_unvalidated` with a docstring saying what it is for, and the money contract is
  proved through FastMCP's arg model (the client path) with the raw seam kept as an
  additional hostile-argument probe. **(2) `upsert_keys` forwarded a free-form object.**
  `fields={"is_archived": true}` archived a key - removing it from every downstream build
  file while the change feed reported `op="updated"` - contradicting NEVER_COVER's delete
  entry, the restore exemption and `key_changes`' pinned deletion contract. Both write modes
  (the bulk `keys` array had the identical hole) now filter through an allowlist and refuse
  `is_archived`/`is_hidden`/anything unknown with a message naming why. The guard grew the
  check that generalises it: every free-form `{"type": "object"}` property must declare a
  policy - either the server-side allowlist constant it is filtered through (read out of
  `server.py` by AST and checked for silent-retirement field names) or a reason it is
  value-shaped - because a name-based table cannot see a flag hidden inside a value.
  Also: the plan stopped claiming an unrouted document "lands in your inbox project" (the
  install's routing rules decide the destination AND can override the engine, and `/api`
  cannot preview them); the schema derivation now FAILS CLOSED on any JSON-Schema keyword it
  does not enforce, guarded against the registry so the first wave-2 tool using `pattern`
  cannot advertise a constraint nothing checks; `minimum` bounds landed on the four cursor /
  limit properties; the connection error catches `TransportError` so a timeout is friendly;
  and the row-count assertion behind "nothing was spent" widened from 2 tables to 8,
  including `spendledger` and `auditlog`. 44 mutation proofs, up from 39.

- 2026-08-05: **wave 2 BUILT** - families E–I, the remaining 18 tools, one build
  wave. Accounting moved **34 covered / 125 exempt (56 planned) → 90 covered /
  69 exempt (0 planned)** over 159 routes; ratchet moved with it (`MIN_COVERED`
  34→90, `MAX_PLANNED` 56→**0**, `MIN_TOTAL_PINS` 22→51, `MAX_DEBT` stays 0,
  debt list still empty and AST-agreed). 47 mutation proofs, up from 45: the
  A3b/D6/A3c forges lost their subject (no live planned marker remains) and were
  re-proved against a synthetic one, D9-style; two new proofs (P1/P2) pin the
  new plan-mode POST whitelist. **Divergences from this spec, reality winning:**
  - **Kill-switch presentation is per-family, not uniform.** §2/§4 assume a
    switched-off feature answers 404. True for keys and images only. The registry keeps `feature_gate` as the master-switch pointer;
    server.py's 404 diagnosis note is restricted to the gates that really 404
    (`_GATE_404_STYLE`), and the suite asserts each family's REAL off-behavior.
  - **run_workflow declares three GET legs** (workflow, segments, batch) so its
    plan can name the step engines, size the spend and list a rerun's failed
    locales - the same pattern as submit_translation's project read.
  - **Synchronous-by-design routes get a 210s timeout** (`_SLOW_TIMEOUT`,
    under the ~4-min client kill): workflow run/batch/rerun returns finished state in one call, per repo law.
  - **app/ was touched a second time** (§14 said exactly once): a one-line
    pre-existing bug fix, not scope - POST /api/glossaries/{gid}/extract read
    `doc.id` after its session closed (DetachedInstanceError → 500 on every
    SUCCESSFUL extraction, candidates already committed), found by this wave's
    suite as the route's first end-to-end caller.
  - Also surfaced and pinned: /api/tm's customer-first `needs_customer` reply;
    `list_connectors` is base-`api` scoped (its route predates the docs gate);
    the wave-1 409 message ("configure an MT engine") stopped being appended to
    NON-engine 409s (for example an estimate mismatch) it never described.

---

# DECISION RECORD (maintainer, 2026-08-04/05 - all seven scoping calls LOCKED; spec is build-ready)

1. AUTH/DEPLOYMENT: standalone process + lsu_ token over HTTPS against /api. OAuth stays Phase C/D.
2. DESTRUCTIVE OPS: excluded entirely from v1 (guard asserts the destructive tier is EMPTY).
3. WRITE BREADTH: all safe writes in v1 (create/import/translate/key edits/TM attach/glossary pins/runs).
4. OUTCOME TOOL: yes - keep the five shipped demand-side tools; submit_translation remains the demo wedge.
5. SPEND CONFIRM: two-phase plan/confirm on EVERY spend-bearing tool (unconfirmed call = read-only preview with engine + scope + budget).
6. TRANSPORT: stdio default + --http streamable-HTTP flag in v1, documented as NOT a claude.ai connector.
7. LIVE RESOURCE: add GET /api/capabilities/{slug} (~15 lines over assist.load_card) so the server's self-description is served by the install it drives. The only v1 touch on app/.

Also settled by the destructive/safety policy: the 22 connector write routes stay EXEMPT in v1
(v2 per-connection allowlist); human_review resume is NEVER a write tool (v2 gets read-only
review_queue).

Build green-light is the maintainer's call; when given, execute the four waves (guard-first wave 0)
through the standard adversarial gate.

8. 2026-08-05, maintainer: `force` dropped from the MCP key-edit surface - plural demotion that destroys translations requires a human surface; web/CLI unchanged. (`force` removed from `_KEY_EDIT_FIELDS` + the upsert_keys schema/description, added to verify_mcp_parity's `_DESTRUCTIVE_FIELD_NAMES` so a re-add goes red, and verify_mcp_tools proves the seam refuses it and no request body carries it; the PluralDemotionRefused relay now names the web grid + `keys edit --single --force` as the human levers.)

9. 2026-08-05, maintainer: **wave 4 approved - remote multi-user via per-request `lsu_` bearer forwarding.** In streamable-http mode each request's `Authorization: Bearer lsu_...` (or `X-Loc-Token`) header is captured into a task-isolated contextvar (the plan-mode pattern) and used as that request's `/api` credential; `LOC_STUDIO_TOKEN` stays the stdio path and the per-request fallback. The server validates nothing itself (no hashing, no lookup) - it forwards the token and relays the app's own `401`/`403`; a request with no token in either place gets a clean per-tool error. No listener-level OAuth `401` (mcp==1.28.1 offers no clean shared-secret hook, and one would block the token-free `initialize`/`tools/list`), so TLS you control is the boundary. **OAuth 2.1 / the claude.ai connector directory remains Phase C2.** Running the listener as a stateless second process is a deployment choice and is off by default. Zero new `/api` routes; help.html gained an additive "Connect an AI agent" section and a `python -m app.cli mcp connect` verb prints the client snippet; tests extend `verify_mcp_transport.py` (per-request forwarding, header-beats-env, no cross-request leak under concurrency, contextvar reset - all mutation-proven).

10. 2026-08-06, maintainer: **Path A - the static-per-request-header claude.ai connector path.** claude.ai custom connectors also accept a fixed request header (`Authorization: Bearer <token>`), not only the OAuth-directory flow - so the wave-4 per-request `lsu_` bearer dispatch is already the **server half** of a claude.ai connector, now built + verified end-to-end (`tests/verify_mcp_connector.py` drives a real streamable-http listener: bearer forwards wire→`/api`, two concurrent sessions stay token-isolated, a missing token is a clean error, an over-cap POST is a clean `413`). `mcp` bumped 1.28.1→1.29.0 (1.x maintenance line; `func_metadata` byte-identical, so the M1 optional-string fix is unaffected - re-verified), with an explicit **8 MiB** streamable-http body cap (1.29's new `RequestBodyLimitMiddleware`; above the 4 MiB default for large inline bulk imports, capped at 8 to limit the pre-auth memory amplifier). **The claude.ai half is UNPROVEN:** request-header auth is a gated beta ("contact Anthropic for early access") with an open bug (`anthropics/claude-ai-mcp#644`, still open - the header is never sent; claude.ai attempts OAuth using the header *name* as `client_id`), so nobody has connected claude.ai to this server yet. Degraded signature if the header never arrives: anonymous `initialize`/`tools/list` answer `200` + all 34 tool defs, so the connector adds + displays its tools and the failure only surfaces on the first tool call as "No Localization OS token on this request". The OAuth-directory flow (DCR + PKCE + RFC 9728) stays Phase C2. Docs + SDK bump + tests only; zero new `/api` routes.
