# Decisions (ADRs)

Short Nygard-format records of durable decisions. A changed decision gets a NEW ADR and the old one is
marked "Superseded by NNNN". Numbers are never reused, so gaps in this list are normal: they belong to
records that are not part of the public repository.

## Licence and governance
- [0089 Apache 2.0, DCO, CC BY 4.0 docs, narrow open core](0089-apache-licence-dco-docs-cc-by-narrow-open-core.md) - Apache 2.0 core, DCO sign-off and no CLA, CC BY 4.0 docs, a short list of paid add-ons, and a stewardship promise (Accepted)

## Product and architecture
- [0001 MT-first TMS, offline by default](0001-mt-first-tms.md) - LLMs translate nearly everything; CAT editor for the exception slice; MockProvider offline (Accepted)
- [0002 FastAPI + Jinja2 + vanilla JS monolith](0002-fastapi-jinja-vanilla-js-stack.md) - one process, named ports, kernel facade, SRP pure-move splits (Accepted)
- [0003 SQLite + SQLModel, Alembic authority](0003-sqlite-sqlmodel-alembic.md) - SQLite default, Postgres supported, one Alembic head, forward-only (Accepted)
- [0011 Four-surface parity](0011-four-surface-parity.md) - web + CLI + /api + MCP + help + llms.txt in the same change (Accepted)
- [0043 Keep the custom workflow engine](0043-keep-custom-workflow-engine.md) - n8n and Okapi ruled out; MIT/Apache/BSD library-shaped deps only (Accepted)
- [0044 Product name Localization OS](0044-product-name-localization-os.md) - brand in config; `loc-tms`, `LOC_TMS_*` and the MCP URI scheme unchanged (Accepted)
- [0047 Language-agnostic workflows](0047-language-agnostic-workflows.md) - projects own languages; designer stamps no pair; groups are Advanced; inert pairs cleared (Accepted)
- [0082 Project automation model](0082-project-automation-model.md) - one connection feeds many projects with per-item claims, scheduled delta and full pulls, UTC schedules, catch up once, no default run cap (Accepted)
- [0083 Notification channels](0083-notification-channels-permissions.md) - Slack, Teams, signed webhook, email; managers create, creator-or-admin edits, SSRF guard (Accepted)
- [0084 Custom project fields](0084-custom-project-fields-permissions.md) - managers define customer-scoped fields, org-wide stays admin, creator-or-admin edits (Accepted)
- [0088 Connected projects](0088-connected-projects.md) - Documents / Keys / Connected; create then connect; project languages drive runs (stop, never delete; add needs manager) (Accepted)

## Hosting and deploys
- [0007 Release identity and clean deploys](0007-release-identity-and-clean-deploys.md) - RELEASE build args always; build from a clean detached worktree (Accepted)
- [0037 Trusted proxy IPs measured](0037-trusted-proxy-ips-measured.md) - only measured /32 peers; loud when wrong (Accepted)
- [0086 Local Jev judge](0086-local-jev-judge-engine.md) - engine 15 ReMedy-9B behind a local adapter, cut 0.551; hosted Jev unused (Accepted)
- [0040 Dependency holds and deferrals](0040-dependency-holds-and-deferrals.md) - sqlmodel held 0.0.44; Forge pair and React 17 updates deferred (Accepted)

## Security, tenancy, privacy
- [0009 multi_org_enabled stays off](0009-multi-org-gate-stays-off.md) - not on any install until #177 #178 #181 land (Accepted)
- [0010 Isolation: 404 before 403](0010-isolation-404-before-403.md) - foreign id answers byte-identically to a missing id (Accepted)
- [0012 Back-office and super-admin removed](0012-back-office-and-super-admin-removed.md) - org lifecycle CLI-only behind the operator key (Accepted)
- [0022 Per-account login lockout](0022-account-lockout-per-account.md) - 20 fails/hour, 15 to 60 min, DB counters, admin unlock (Accepted)
- [0023 AI opt-out wording](0023-ai-optout-scope-wording.md) - instance engines never process the project; exports and MCP are outside (Accepted)
- [0036 Fail-closed audit](0036-fail-closed-audit.md) - no audit row, no request (503); actor never anonymous (Accepted)
- [0038 Per-user API keys](0038-per-user-api-keys.md) - shared lsk_ retired; expiry, rate limits, http only on loopback/tailnet (Accepted)
- [0041 Org-default context card admin-only](0041-org-default-context-card-admin-only.md) - write and delete on every surface (Accepted)
- [0042 AI spend is manager data](0042-ai-spend-is-manager-data.md) - cost hidden from translators; install spend cap (Accepted)
- [0024 WCAG 2.2 AA](0024-wcag-22-aa-accessibility.md) - shrink-only a11y ratchets and specific UI rulings (Accepted)

## Translation quality, prompts, memory, cost
- [0025 System-prompt overrides](0025-system-prompt-overrides.md) - editable heads, locked floor in code, admin-only writes, flag off = byte-identical (Accepted)
- [0029 Fence untrusted text in prompts](0029-fence-untrusted-text-in-prompts.md) - author-fence detection errs toward fencing; recalled memory fenced (Accepted)
- [0026 LLMemory two-actor / two-document bars](0026-llmemory-two-actor-two-document-bars.md) - learned terms need 2 humans, LQA rules 2 documents (Accepted)
- [0027 DOCX walk stamps](0027-docx-walk-stamps.md) - new walk = new stamp; older stamps byte-identical; 409 re-import on unknown (Accepted)
- [0032 Jev judge-role engines](0032-jev-judge-role-engines.md) - hosted `jev` and `jev_local`; unsure never vetoes; opt-in auto-fix (Accepted)
- [0033 Number-check advisory forms](0033-number-check-advisory-forms.md) - only H.MM times and 0-10 number words are advisory; the rest blocks (Accepted)
- [0030 Free-engine pricing rule](0030-free-engine-pricing-rule.md) - free iff local kind on a private endpoint or mock; exact-id allowlist (Accepted)
- [0031 Job exact cost at run time](0031-job-exact-cost-at-run-time.md) - `Job.cost_usd` from the live ledger; readers prefer it (Accepted)
- [0028 mcp 2.2.0 port](0028-mcp-2-port.md) - exact pin, loud on SDK drift, ToolError for expected refusals (Accepted)

## File formats and MCP
- [0064 Untouched files export byte-identical](0064-byte-identical-untouched-exports.md) - nothing translated = upload verbatim; touched files keep line endings, BOM, code page when it fits (Accepted)
- [0065 Plural forms filled with the plural translation](0065-plural-forms-filled-with-plural.md) - one rule for PO, i18next, Qt, Rails, Fluent, ARB; existing forms never replaced (Accepted, #729)
- [0066 Abbreviation-aware default splitter](0066-abbreviation-aware-default-splitter.md) - v2 rules for new documents; split version stamped, old documents replay v1 (Accepted)
- [0067 Review holds: dates, line breaks, glued words](0067-review-hold-checks.md) - `review_hold` = NEEDS_REVIEW, exported as written, cleared by save or approve (Accepted)
- [0068 Free-order placeholder token kind p](0068-free-order-placeholder-token.md) - argument-naming placeholders may reorder; `%(name)s` protected; old rows keep x (Accepted, #693)
- [0069 Bilingual target mismatch](0069-bilingual-target-mismatch.md) - translate only the declared language, refuse 422 if no project target matches; PO header, .hhp code page (Accepted, #687)
- [0070 Source-language code page for undeclared text](0070-source-language-code-page.md) - decode by the project's source language, stamped, warn on garbled letters (Accepted)
- [0071 Translate comments: per-project option, default off](0071-translate-comments-option.md) - every Office format; old projects keep Word on; SVG title/desc offered (Accepted, #690)
- [0072 RTL layout for sheets, IDML/ICML, SVG](0072-rtl-layout-sheets-idml-svg.md) - RTL targets only; World-Ready composer; LTR outputs byte-identical (Accepted)
- [0073 Excel export patches the original package](0073-excel-export-patches-package.md) - no openpyxl rebuild; walk stamp; charts and VML not walked yet (Accepted, #685)
- [0074 MCP annotations, titles, outputSchema](0074-mcp-annotations-and-output-schema.md) - destructive unless reviewed additive; structured output conforms or `{"value"}`; error code tail (Accepted)
- [0075 MCP external content marking](0075-mcp-external-content-marking.md) - outside text marked as data by default; vendor errors summarized and redacted (Accepted, #748)
- [0076 Background calls and idempotency keys](0076-background-calls-and-idempotency-keys.md) - Prefer: respond-async, Idempotency-Key, MCP launch then poll, job_status; no cancel yet (Accepted, #728)
- [0077 MCP tools filtered by key scope; toolsets](0077-mcp-scope-filtered-tools.md) - fails narrow on lookup errors (Accepted, #751)
- [0078 MCP OAuth AS + RS, off by default](0078-mcp-oauth-resource-server-off-by-default.md) - PKCE, CIMD, DCR off, audience-bound tokens; not yet tried with claude.ai/ChatGPT (Accepted, #753)
- [0079 MCP confirms bound to plan_id](0079-mcp-confirm-bound-to-plan-id.md) - BREAKING for direct confirm callers (Accepted, #757)
- [0080 MCP stdio file allowlist](0080-mcp-stdio-file-allowlist.md) - Documents, Downloads, OneDrive\Documents, output dir + `LOC_STUDIO_INPUT_DIRS` (Accepted, #755)
- [0081 Jira jql / Confluence space_key and cql admin-only](0081-jira-confluence-scope-admin-only.md) - same as ServiceNow and Notion (Accepted, #419)

## Process and interface
- [0004 Test wrapper is the gate](0004-test-wrapper-is-the-gate.md) - wrapper only, suite_owners selection (Accepted)
- [0021 No em dashes, ASCII](0021-no-em-dashes-ascii.md) - human-read prose and terminal output (Accepted)
- [0052 UX overhaul: visual only, Direction A](0052-ux-overhaul-visual-only-direction-a.md) - industry terms kept, shared macros (Accepted; skin parts superseded by 0063)
- [0054 Privacy erasure, encoding and identity decisions](0054-privacy-encoding-identity-decisions-2026-10-05-06.md) - erasure scope, Windows-1252, SSO/SCIM, multi-org, UX choices (Accepted)
- [0061 TMX imports commit in bounded chunks](0061-tmx-import-chunked-commits.md) - 2,500 rows per transaction, files over that are no longer all-or-nothing, retry is idempotent (Accepted, #615)
- [0062 MT formality is granular](0062-granular-mt-formality.md) - document > project > customer, per target language; generic formal/informal/auto plus a per-language register catalog; rides the style-guide block (Accepted, #617 item 7)
- [0063 Console is the only look; skins removed](0063-console-look.md) - neutral grays, one blue accent, Public Sans, hairline tables, row "..." menu; Light/Dark/Match my computer; Get started in the account menu (Accepted, supersedes the skin parts of 0052)
