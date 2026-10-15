<!-- claims -->
<!--
CLAIMS TRACE - every factual product claim on this page maps to shipped behavior.
Format: claim -> app anchor (file:symbol) [+ test]. Non-product claims (market
framing, attributed stats) are marked ATTRIBUTED and carry their source inline.

Lede / "settled definition" (allowlist + audit trail + risk-tiered review +
  named owner)                     -> ATTRIBUTED: Crowdin 2026 AI Translation Report;
                                      Bluente "AI Translation Governance Framework 2026"
Auditable AI
  keyed-HMAC hash chain            -> app/services/audit.py:_compute_hash (HMAC-SHA256,
                                      compact ASCII-escaped JSON array: ["audit-v2", prev_hash, ts, actor, action, target, detail, ip, customer_id])
  MAC key outside the DB           -> app/services/audit.py:_mac_key (derives from
                                      config.SESSION_SECRET keyfile)
  tail-truncation head anchor      -> app/services/audit.py:_write_anchor / verify_chain
  chain verification               -> app/services/audit.py:verify_chain
  verify: web / CLI / API          -> /audit route; CLI `admin audit verify`;
                                      /api/audit (app/features.py:CORE_API "/api/audit")
  recorded fields                  -> app/services/audit.py:log(actor, action, target,
                                      detail, ip, customer_id) + AuditLog.ts/prev_hash/row_hash;
                                      customer_id authenticated in v2, excluded in historical v1 rows
  NDJSON export                    -> app/services/audit.py:iter_export
  forced rows for operator verbs   -> app/services/audit.py:log(force=True)
  contention posture (fail-open/   -> app/services/audit.py:fail_open (audit_fail_open,
    fail-closed, default fail-open)    default "1"); log() sheds a row after bounded retry
Verification
  LQA gate + default threshold 90  -> app/services/lqa.py:LQA_PASS_THRESHOLD (=90)
  per-step configurable threshold  -> app/services/lqa.py:score_text(lqa_threshold=...),
                                      WorkflowStep.lqa_threshold, clamp_threshold
  needs_review routing             -> app/services/lqa.py:score_text (gate "needs_review")
  protection law (human work)      -> app/services/quality.py:commit_mt_output
                                      (status_protected on APPROVED/EDITED) +
                                      tests/verify_status_protection.py
  single MT write seam             -> app/services/quality.py:commit_mt_output
                                      (tests/verify_refusal_choke_point.py grep tripwire)
  hard TM lock refusal             -> app/services/quality.py:commit_mt_output
                                      (lock_refused; gated by TM_LOCKS_ENABLED)
Approved tools & kill-switch law
  engine/provider registry         -> app/services/providers.py; app/services/engines.py
  feature-flag admin, full removal -> app/features.py:is_enabled/path_is_off/card_is_off;
                                      20-module registry (MIN_FEATURES=13, a count floor,
                                      not the live total), semantics "removed" (10) 404s,
                                      "writes_off"/"opt_in" do not;
                                      Settings card at /settings#features (settings.html:166)
  MCP tool refuses (not de-listed) -> integrations/mcp-server/server.py register_tools()
                                      registers all tools; a disabled feature's tool 404s
                                      when CALLED (stays enumerable)
  per-feature kill switch          -> app/features.py:Feature.is_enabled (strict == "1")
Data sovereignty
  self-hosted or run anywhere      -> README.md (single-tenant on-prem; deployable container)
  BYO API keys                     -> app/services/cloud_keys.py; app/services/providers.py
                                      (ATTRIBUTED 88.8% BYO stat: Crowdin 2026)
  local / self-hosted models       -> app/services/providers.py:LocalProvider; air-gapped
  GDPR export + erase              -> app/services/gdpr.py:export_user / erase_user
  PII redaction (translation + LQA -> app/services/pii.py:enabled (pii_redaction toggle,
    source paths, default off)         default off); applied on the translation/LQA source
                                      paths, not to injected scaffolding
  encryption at rest (two opt-in   -> app/services/dbcrypt.py (opt-in SQLCipher, SQLite-only,
    layers)                            sqlcipher3 binding); Fernet uploads via
                                      app/services/storage.py:encode_at_rest (LSENC1: magic,
                                      encrypt_uploads gate)
  spend caps                       -> app/services/spend.py:meter_state/would_block
                                      (spend_cap_usd, default "0" = off)
  one-command export               -> CLI `admin audit export`, `admin gdpr export`
Context & loc-RAG
  TM/glossary/style/context inject -> app/services/prompts.py ({glossary}/{context}/
                                      {style_guide} placeholders; TM few-shot is an APPENDED
                                      fenced block, prompts.py:translation_prompt (the "TM
                                      few-shot" comment), not a token);
                                      context_pkg.render_block; app/services/tm.py; glossary.py
  segment status: machine vs human -> Segment status MT/NEEDS_REVIEW (machine) vs
                                      EDITED/APPROVED (person); mt_confidence, tm_match_pct,
                                      lock_band
  producing-path stamp on QUARANTINE-> quality.commit_mt_output records `source` on the
                                      refusal/scaffold qa_flag (no Segment origin column;
                                      not persisted on clean writes)
Predictive QE
  threshold routing before deliver -> app/services/lqa.py:score_text gate ->
                                      quality NEEDS_REVIEW (ATTRIBUTED framing: RBB, 2026)
Access control
  RBAC roles (SIX)                 -> app/models.py:Role.ALL = admin/manager/reviewer/
                                      linguist/translator/viewer; security.py predicates
  API-key scopes                   -> app/services/apikeys.py:ALL_SCOPES
                                      (api/docs/keys/admin/scim)
  OIDC / LDAP                      -> app/services/oidc.py; app/services/ldap_auth.py
  remote-admin master switch       -> remote_admin_enabled (REMOTE_IMMUTABLE, host-only)
  accountable owner (traceable,    -> no owner_id feature exists; RBAC role + audit
    NOT assigned by the product)     attribution make an owner's decisions traceable, the
                                      assignment itself is organizational

NOT CLAIMED (out of scope, unshipped, or overclaim): SOC 2 / ISO 27001 / HIPAA / PCI
certification; "EU AI Act compliant" or any compliance verdict; watermarking; AI-content
disclosure manifests; risk-tier profile objects; model-drift watch; verification-effort
reporting; any benchmark number about our own output. Provenance/audit features are
described as SUPPORTING disclosure/audit obligations (a capability), never as compliance.
-->

# Translation governance

By 2026 the industry has settled on what "translation governance" means, and it is
concrete: an **approved-tool allowlist**, an **audit trail**, **risk-tiered review**,
and a **named accountable owner** (Crowdin, *2026 AI Translation Report*; Bluente,
*How to Build an AI Translation Governance Framework in 2026*). Localization OS ships the
instruments for every one of these. It has since before the words were fashionable - the
product was built on the idea that the operator commands the machine translation, not the
reverse. (The one element no software can *assign* is the accountable owner - that is an
organizational choice; what the product supplies is the RBAC and audit attribution that
make an owner's decisions traceable.)

This page maps our feature names to the industry's vocabulary, so a buyer searching for
*trust layer*, *verification*, *auditable AI*, *data sovereignty*, *RAG*, or *predictive
QE* can see exactly where each lives in a running install. Nothing here is a roadmap
item; every claim points at code you can run offline today.

---

## Auditable AI

Every mutating action is written to an append-only, **tamper-evident ledger**. Each row
carries `row_hash = HMAC-SHA256(key, prev_hash | timestamp | actor | action | target |
detail | ip)`, chaining to the previous row. The MAC key is derived from a keyfile that
lives **outside the database**, so an attacker holding the database alone cannot rewrite
a row and recompute a chain that verifies. Deleting the tail of the log is caught too: a
MAC'd head anchor fails verification when the newest row id moves backwards.

The behavior under extreme write contention is itself an operator choice, not a silent
default. The `audit_fail_open` setting picks the posture: **fail-open** (the default on a
desktop install), where a row that cannot be written after bounded retries is shed with a
loud warning, counted and logged as an error, and the request proceeds, favoring
availability; or **fail-closed** (the default on a server install), where the request's
row is written before it runs, so a request whose row cannot be written is refused with a
retryable error and changes nothing. The trade is named and yours to set. Audit logging
itself is always on for an install that requires sign-in: only the host operator can
switch it off, and the switch is itself recorded.

That row structure is a field-for-field match to the settled audit-trail definition:

- **the tool/action used** - `action` (e.g. `glossary.approve`, an engine run, an erase)
- **the initiator** - `actor` (the signed-in user, an API token, or `system`)
- **the target** - `target` (the project, document, key, or user acted on)
- **the timestamp** - every row is time-stamped in UTC
- **whether human review occurred** - human sign-off is itself an audited action
  (approvals, glossary approve/unapprove, vendor-quote approval), and each segment's
  status separates machine work (`MT` / `NEEDS_REVIEW`) from a person's (`EDITED` /
  `APPROVED`).

The chain is **verifiable three ways** - a button on the `/audit` page, the
`admin audit verify` CLI command, and the `/api/audit` endpoint - and exports as
**NDJSON** with `prev_hash`/`row_hash` on every record, so a downstream SIEM can
re-verify the chain without touching this database. The cross-org operator verbs
(`org`, `user org`, `template delete --operator`) write their rows **even when the
global audit gate is off**, so an operator cannot switch auditing off and then perform
an unaudited privileged action. (The audited impersonation this once also covered was
removed on 2026-09-20 with the back-office console; operator power now requires shell
access on the host rather than an account.)

The result is what the regulated-industry conversation keeps asking for: not a log you
hope no one edited, but a verifiable, exportable chain - with the contention posture a
choice you make, not a silent default.

---

## Verification

The industry's own framing is that **in some market segments, 90–95% of human-involved
volume is now verification** of AI output rather than translation (Stefan Huyghe /
Slator, 2026). Localization OS is built as a verification machine.

- **LQA gates** score each translation against the MQM error typology and a per-language
  rulebook. A card **passes** only when its score clears the numeric bar **and** it
  carries no major/critical error. The default bar is **90**; an LQA workflow step can
  set its own per-step threshold, and an unset step inherits 90 unchanged.
- A failed gate routes the segment to **`needs_review`** - the exact seam a
  `run_condition`/branch step turns into a correction (Fix) loop.
- The **protection law** is the spine of it. Machine-produced and imported text can only
  reach a segment's target through **one sanctioned write seam**. At that seam, a segment
  a human has **approved or edited** is refused: the write is withheld, the row is left
  byte-untouched, and the outcome is marked `status_protected`. A model cannot silently
  overwrite a human decision - and because there is exactly one seam, this holds on every
  surface (web workflows, plain pretranslate, CLI, `/api`, MCP, key import, vendor
  return). It is **core, default-on** behavior with no kill switch. A pinned test
  (`tests/verify_status_protection.py`) defends it.

Hard TM-match locks ride the same seam: a locked segment refuses any machine overwrite,
and only a reviewer clearing the lock (an audited action) can change it.

---

## Approved tools & the kill-switch law

Governance's first component is an **approved-tools allowlist** - deciding which models
may run. Localization OS's **engine/provider registry** is exactly that: the operator
configures which providers and models are available, with their own keys.

The reach goes further than an allowlist. A central **Settings > Features** admin lets
the operator switch off any module in a fixed registry of non-core features (twenty
today; core sections such as Connectors are never in it). For the removal-semantics
modules - the majority (ten of the twenty) - a switch-off **removes** the feature rather
than hiding it: its routes answer `404` (indistinguishable from a route that was never
built, so a disabled feature can't be discovered by probing), its CLI verbs refuse, it
drops from the `/api` surface, and its **MCP tool refuses** when called. A few modules
switch off differently by design - the Evaluations Lab and LLMemory stop new writes but
keep their existing records readable, and a couple ship off by default as opt-ins - but
the on/off test is the same strict whitelist (`== "1"`), so a non-canonical value fails
toward **off**, the kill-switch-safe direction. The operator decides what the install
even is.

---

## Data sovereignty

BYO API keys are **the single most-cited enterprise requirement** - 88.8% of teams
require or prefer them (Crowdin, *2026 AI Translation Report*) - and a self-hosted TMS is
structurally strongest here. Localization OS is **self-hosted, or run it yourself anywhere a
container runs**: your box, your database, your keys.

- **Your data stays in your store** - SQLite or Postgres you control. There is no hosted
  service you depend on; the install *is* the tenant boundary.
- **Bring your own provider keys**, or run **local / self-hosted models** and go fully
  **air-gapped** - no content leaves the machine.
- **GDPR** subject-access **export** and **erasure** are built in (`export_user` /
  `erase_user`), and export is **one command** (`admin gdpr export`, `admin audit
  export`).
- **PII redaction** scrubs personal identifiers out of source content on the translation
  and LQA paths before it reaches an engine (toggleable, off by default - leave it off for
  local models, on when routing to the cloud).
- **Encryption at rest** comes as two opt-in layers: full-database SQLCipher (SQLite-only,
  requires the `sqlcipher3` binding) and Fernet-encrypted uploads.
- A **spend-cap kill switch** is a hard financial boundary: once the monthly ceiling is
  reached, no further paid provider calls are made (retries and failover can't hammer
  past it). It ships **off** (`spend_cap_usd = 0`); the local and mock engines are exempt.

This is the answer to the "platform capture" critique the industry has started naming
(Emily Diamandopoulou, AI Loc Think Tank, 2026): your data is portable and your exports
are one command, so the tool stays replaceable.

---

## Context & localization RAG

Retrieval-augmented generation for localization is not a missing feature to bolt on - it
is what the translation pipeline already does. Before each translation, Localization OS
retrieves and **injects the relevant glossary terms, style-guide sections, the resolved
context package, and TM matches** into the prompt - the `{glossary}`, `{style_guide}` and
`{context}` blocks, plus an appended, injection-fenced block of TM few-shot examples. That
is retrieval-injection - the same shape practitioner guides tell teams to hand-roll, built
in natively.

Retrieved context is treated as **data, not instructions**: external metadata is
allowlisted, sanitized at ingestion, and fenced at render, and the context budget is
hard-capped with ranked eviction so an over-matched termbase can't crowd out the text
being translated.

Each unit records enough to tell **whether a person has signed off**: its status separates
machine work (`MT` / `NEEDS_REVIEW`) from a person's (`EDITED` / `APPROVED`), and it
carries `mt_confidence`, TM-match percentage and lock band. When a guard quarantines a
suspected refusal or off-target write, the single write seam stamps the producing path
(`mt`, `workflow`, `key-import`, …) onto the quality flag, so a reviewer sees which path
produced the flagged output.

---

## Predictive QE

The market has moved past speed-alone: the value is identifying which segments need
human expertise **before** delivery, and treating random spot-checks as false security
(RBB Consultancy, 2026). Localization OS's **LQA-threshold routing** is precisely this - the
gate decides, per segment, what clears automatically and what is routed to a human, so
attention goes where the score says it is consequential rather than to a random sample.

---

## Access control

- **RBAC** with six roles (`admin`, `manager`, `reviewer`, `linguist`, `translator`,
  `viewer`) and per-project membership scoping; each write is gated by the matching
  predicate, enforced on both the web UI and `/api`.
- **Per-user API keys** with **scopes** (`api` / `docs` / `keys` / `admin` / `scim`); the top
  `admin` scope is **mintable only locally**, so a stolen key can never self-replicate a
  higher grant over the wire.
- Enterprise sign-in via **OIDC SSO** (PKCE + JWKS) and real **LDAP / Active Directory**
  (search-then-bind), layered on top of local passwords, never replacing them.
- A host-only **remote-admin master switch** decides whether the sharpest `/api` admin
  verbs are reachable over the wire at all.

---

## A note on compliance

Localization OS does not claim any certification (SOC 2, ISO 27001, HIPAA, PCI) and does not
assert compliance with the EU AI Act or any other framework. What it provides is
**capability**: the provenance, audit-chain, and export features here **support** the
disclosure and audit obligations that frameworks like the EU AI Act and GDPR place on
localized content. Whether a given deployment meets a given obligation is a decision for
you and your compliance function - this product gives you the instruments, not the
verdict.

---

## The map - industry term to Localization OS feature

| Industry term | Localization OS feature | Where to see it |
| --- | --- | --- |
| Auditable AI / audit trail | Keyed-HMAC tamper-evident audit chain, NDJSON export | `/audit`; `admin audit verify` / `export`; `/api/audit` |
| Verification | LQA gates (per-step threshold, default 90) + review routing | Workflow LQA step; `/help#workflows` |
| Trust layer / protection law | Single MT write seam; human-approved work is never overwritten | `app/services/quality.py:commit_mt_output` |
| Approved-tool allowlist | Engine/provider registry | Engines & providers; `/help#engines` |
| Kill-switch law | Feature-flag admin with full-removal semantics | `/settings#features` |
| Data sovereignty | Self-hosted, BYO keys, local models, air-gap | Providers; air-gapped deployment |
| Data portability / anti-capture | GDPR export/erase, one-command exports | `admin gdpr export`; `admin audit export` |
| RAG for localization | TM/glossary/style/context injected into every prompt | Translation; `/help#terminology`, `/help#tm` |
| Predictive QE | LQA-threshold routing before delivery | Workflow LQA step |
| Access control (traceable owner) | RBAC + audit attribution (the owner's *role*; assigning the owner is organizational), API-key scopes, OIDC/LDAP | Users & roles; `/help#users`, `/help#auth` |
| Spend governance | Spend-cap kill switch, department budgets | Setup AI (spend cap); department budgets (core) |

---

*Market
statistics are attributed to their published sources; every product claim traces to the
code anchors in the claims-trace comment at the top of this file.*
