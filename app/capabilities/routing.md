---
module: routing
title: Content routing
summary: Inbound content-routing rules - match an incoming submission by file extension and submitter department and pick where it lands (project) and which engine translates it (provider). Subject matching and workflow / language-set targeting are reserved for v2 and not consulted at run time yet. Rules take effect on two API document paths only - PLACEMENT on POST /api/documents (when the caller names no project), and PROVIDER OVERRIDE on POST /api/documents/{id}/translate (reachable from /api, MCP and --remote CLI; the only place a rule's provider target has effect). Web-UI uploads and in-process CLI uploads consult no rules. Rule authoring is the switchable feature; the runtime routing decision that existing rules drive is part of the core inbound pipeline and stays on.
nouns:
  Routing rule: "One match-then-place instruction. Its match side is file extension and/or submitter department; its target side sets the project and/or provider a matching submission should use. (Subject, workflow and language-set fields exist on the model but are reserved for v2 - not consulted at run time yet, and refused at authoring time.) A rule is active or inactive."
  Priority: "The order rules are tried, ascending - a lower number wins. The first ACTIVE rule that matches decides; nothing after it is consulted."
  Match fields: "What a rule keys on: ext (one or more file extensions) and department. An empty match field is a wildcard for that dimension, so a rule with no match fields matches everything at its priority. (Subject matching is reserved for v2 and not consulted yet.)"
  Target: "What a matched rule sets - a target project and/or a provider override. Any subset; an unset target leaves that choice to the default. (Workflow and language-set targeting are reserved for v2 and not consulted yet.)"
  Department inbox: "The fallback landing project for a submitter's department, found-or-created when no rule places an upload. This is the safety net so a submission with no matching rule is never dropped."
  Routing decision: "The runtime resolution - given an upload's extension and department, pick the project and provider by walking active rules in priority order, else the department inbox. It runs whether or not the authoring feature is on, but takes effect on two API paths only: it PLACES an upload on POST /api/documents (only when the caller names no project - an explicit project short-circuits the rules), and a rule's PROVIDER override wins on POST /api/documents/{id}/translate (the only site where a rule's provider target has any effect; intake reads only the decision's project). Web-UI uploads and in-process CLI uploads consult no rules."
verbs:
  See your rules: "Web: the Routing page (its nav link appears only while the feature is on). CLI: `routing rule list` shows rules in priority order; `routing rule show 5` reads one rule's full config back."
  Add a rule: "Web: the Routing page form. CLI: `routing rule add --ext md,html --department Support --project 3 --provider claude --priority 50` - match fields (--ext, --department) and targets (--project, --provider) are optional; --priority defaults to 100. The --subject, --workflow and --language-set flags still parse but are REFUSED (reserved for v2, not consulted at run time yet)."
  Turn a rule on or off: "Web: the rule's toggle. CLI: `routing rule toggle 5` flips it active/inactive - an inactive rule is skipped by the routing decision but kept for the record."
  Remove a rule: "Web: the rule's delete control. CLI: `routing rule delete 5 --yes` (destructive; --yes skips the confirmation prompt)."
needs:
  - "The routing_enabled switch on. With it off the RULE-AUTHORING feature is REMOVED, not hidden: the /routing page returns not-found (404), /api/routing 404s, the nav link disappears, the routing CLI group refuses (`content routing is disabled (setting routing_enabled=0).`), and the MCP routing-admin tool relays the same 404. Existing rules are preserved and the runtime routing decision keeps honoring them - only authoring is removed."
  - "A target to point a rule at - a project and/or a provider override. A rule with no target still matches, but places nothing, so a match with no project falls through to the department inbox. (Workflow and language-set targets are reserved for v2 and not consulted yet.)"
  - "Nothing for the runtime decision itself: with no rules at all, resolve returns the default (the department inbox for an upload), so routing is safe to leave un-configured."
limits:
  - "Only rule AUTHORING is switchable. The runtime routing decision (resolve / department inbox) is deliberately NOT gated - disabling the authoring surface must never break inbound routing that existing rules already drive. Turning the feature off freezes the rule set; it does not stop routing."
  - "First active match by priority wins, ascending. A lower-priority-number rule earlier in the order short-circuits the rest, so ordering is how you resolve overlap - not every matching rule is applied, only the first."
  - "An empty match field is a wildcard. A rule with no ext and no department matches every submission at its priority - useful as a catch-all, easy to make too broad by accident."
  - "Subject / workflow / language-set are reserved for v2. They exist on the rule model but NO reader consults them at run time yet (matching is ext + department; targets are project + provider), so they are refused at authoring time on every surface. Pre-existing rules carrying them keep the data but are flagged inert in list/show output."
  - "Rules take effect on two API document paths only. PLACEMENT: on POST /api/documents, a matching rule's target project receives an upload - but ONLY when the caller names no project (an explicit project short-circuits the rules); with no matching rule the department inbox catches it. PROVIDER OVERRIDE: on POST /api/documents/{id}/translate (reachable from /api, MCP and --remote CLI doc translate), a matching rule's provider wins over the project/config default - this is the ONLY site where a rule's provider target has any effect, since intake reads only the decision's project. Web-UI uploads and in-process CLI uploads consult no rules - a known v1 asymmetry."
  - "Department budgets live under /routing/budgets but are NOT part of this feature - spend control is core and independent of content routing, so a budget stays reachable when routing is switched off."
  - "Per-project MONEY budgets (CV-2) live under /routing/project-budgets, also core and NOT part of this feature. A project's monthly USD cap counts AI model spend (machine translation + workflow runs) plus SETTLED vendor human spend; at 100% new machine-translate submissions and new vendor dispatches for that project are refused (human review is never blocked), and admins are alerted at 80% and 100%. Committed-but-unpaid vendor work is shown on the meter but not enforced against. Manage them (admin-only) on the routing admin page, via `admin budget project set|list|delete`, or the /api/projects/{id}/budget routes; the project page shows a read-only meter to managers and up."
  - "Language sets (the reusable source + target-locale bundles) are their own core surface at /languages, not part of the routing switch. Binding a language set to a rule is reserved for v2 and not consulted yet."
connects:
  - module: workflows
    how: "A rule's provider target picks which engine translates a matched submission. (Steering a submission onto a specific multi-agent workflow is reserved for v2 - the workflow field is not consulted at run time yet.)"
  - module: documents
    how: "On POST /api/documents the routing decision places an upload in a matching rule's target project (or the department inbox) when the caller names no project, before the document pipeline takes over; on POST /api/documents/{id}/translate a matching rule's provider override wins over the project default. Web-UI and in-process CLI uploads do not consult routing yet - they place documents by their explicit project or the department inbox."
  - module: connectors
    how: "Connectors place content by their connection's configured project and consult no routing rules; only content submitted through POST /api/documents is routed. (The reserved-for-v2 subject match would have keyed on a connector email subject, but no reader consults it.)"
---

## What this is for

Content arrives from many places - uploads, connector intakes, department mailboxes -
and it should land in the right project, with the right provider, without someone
sorting it by hand each time. Content routing is the set of **rules** that make that
automatic: match a submission by its file extension or submitter department, and
place it.

## How a rule decides

Rules are tried in **priority order, ascending** - a lower number is consulted first,
and the **first active rule that matches wins**. Nothing after it is consulted, so
ordering is how you resolve overlap. Each rule's match side (`ext`, `department`) is
ANDed, and an empty match field is a wildcard for that dimension - a rule with no
match fields is a catch-all at its priority.

A match sets any subset of a **target**: the project the submission lands in and a
provider override. When no rule places an upload, it falls to the submitter's
**department inbox**, found or created so nothing is ever dropped.

## Reserved for v2 (authored-but-inert fields)

A rule also carries `subject`, `workflow` and `language_set` fields, but **no reader
consults them at run time yet** - a rule authored with them would silently do nothing,
so they are **refused at authoring time** on every surface (web form, CLI, `/api`,
MCP). (`customer` is likewise a reserved, unused column with no authoring control; a
rule's project already determines the customer.) Any pre-existing rule still carrying
these fields keeps them for the record and is flagged as **inert** in the web rule
list and the CLI `rule list` / `rule show` output.

## Where rules fire

A routing rule takes effect on **two API document paths**, and nowhere else:

- **Placement** - on `POST /api/documents`, a matching rule's target project (else the
  department inbox) receives the upload. This fires **only when the caller passes no
  explicit project**; naming a project short-circuits the rules outright. The upload
  response then carries a `routing` field saying how the project was chosen (a matched
  rule or the department inbox), so the placement is never silent.
- **Provider override** - on `POST /api/documents/{id}/translate` (reachable from the
  API, MCP, and `--remote` CLI document translate), a matching rule's provider wins over
  the project/config default engine. **This is the only site where a rule's provider
  target has any effect** - the intake path reads only the decision's project, never its
  provider.

**Web-UI uploads and in-process CLI document uploads consult no routing rules** - they
place documents by their explicit project or the department inbox. This asymmetry is a
known v1 limitation.

## The normal path

1. **List** the current rules in priority order (`routing rule list`).
2. **Add** a rule matching the content you want to steer
   (`routing rule add --ext md,html --department Support --project 3 --priority 50`).
3. **Toggle** a rule off when you want to keep it but stop it firing
   (`routing rule toggle 5`), or **delete** it (`routing rule delete 5 --yes`).

## Authoring switches; routing does not

`routing_enabled = 0` **removes the authoring surface** - the page, the `/api`
routes, the nav link, the CLI group and the MCP tool all 404 or refuse. What it does
NOT do is stop routing: existing rules are preserved and the runtime decision keeps
honoring them, because disabling rule authoring must never break inbound routing that
is already in place. Department budgets (`/routing/budgets`), per-project money
budgets (`/routing/project-budgets`) and language sets (`/languages`) are core and
stay reachable regardless.
