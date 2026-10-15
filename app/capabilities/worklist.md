---
module: worklist
title: My work
summary: The cross-project reviewer worklist - one "My work" page (plus /api and CLI) listing, for you specifically, the paused review steps that name you, the queries assigned to you (and your own answered ones), and the documents in your scope needing quality attention. It is a page you visit, not a notification centre; it never assigns, gates, or moves anything. Web, /api, CLI and MCP in the same change.
nouns:
  Work item: "One row on your worklist, tagged with its section (review / query / quality), a short reason, the project and (where it applies) the document + locale, and a link to where you act. Items are merged newest-first across every project you can see."
  Review section: "Paused workflow steps whose STEP DEFINITION names your email. Assignment lives on the workflow definition (WorkflowStep.assignee_email), NOT on the individual run - so if a workflow's review step names you, EVERY paused run of that workflow appears here. Per-run assignment does not exist yet."
  Query section: "Query threads assigned to you that are open or answered, plus your OWN threads that were answered (so you can act on the reply). It disappears entirely when the queries feature is turned off; the review and quality sections are unaffected."
  Quality section: "Every document you can see with at least one LIVE (non-withdrawn) major or critical MQM LQA finding in a declared recency window (90 days by default). It is a DERIVED list, not an assignment - nothing in Localization OS assigns an LQA task to a person. Reviewer-or-higher only. Grouped by document + locale, so one noisy document is one row."
  Recency window: "The quality section's lookback, stated on the page ('findings from the last N days') and overridable per request (?days= / --days). Declared, never silent."
verbs:
  See my work: "Web: the My work page in the nav. /api: GET /api/worklist (the identity is the API key's OWN user). CLI: `work list` in-process (or `--actor <email> work list` to view another user's, actor is a global flag before the subcommand), or `--remote`. MCP: my_work (answers for the key's owner). It reads only - opening the worklist never changes anything."
  Narrow the quality window: "Web: the 'Quality window (days)' field. /api: ?days=N. CLI: --days N. MCP: days. Bounded 1..3650 days."
  See NEEDS_REVIEW segments instead: "Web: the 'show NEEDS_REVIEW segments' toggle. /api: ?quality=needs_review. CLI: --quality needs_review. MCP: quality. An OFF-BY-DEFAULT filter that swaps the quality section for the list of segments a write-seam QA gate routed to a human - a machine flag, not an assignment, and on an established install there can be a great many (which is why it is off by default)."
needs:
  - "The worklist_enabled switch on (default ON). With it off the feature is REMOVED, not hidden: /my-work and GET /api/worklist 404 (themed, never 403 - non-enumerable), the nav item disappears, this card 404s on /help and /api/capabilities, the `work` CLI group refuses (`the reviewer worklist is disabled (setting worklist_enabled=0).`), and the my_work MCP tool relays the 404. It is a pure read leaf, so there is no data to preserve."
  - "A resolvable identity. /api/worklist needs a per-user API key - a shared connector token has no user and is refused with a named 400 (`the worklist needs a per-user API key ...`), never an empty list that would read as 'you have no work'. In-process `work list` needs `--actor <email>` (or `--remote`) and refuses without it. The quality section additionally needs the reviewer role (or higher); a translator does not see it."
  - "Nothing else. The worklist assigns nothing, gates no workflow, re-translates nothing and moves no money - it is a read over data other features already produced (paused runs, query threads, LQA findings)."
limits:
  - "The review section's honest limitation: assignment is a property of the workflow DEFINITION, not of an individual run. So 'paused steps that name you' means every paused run whose current step's definition names your address - if two runs of the same workflow are paused at a review step that names you, both appear. Per-run assignment is a recorded deferral, not a bug."
  - "The quality section is NOT keyed on the NEEDS_REVIEW segment status. That status is set by every soft QA gate and cleared only by a human edit, so on a mature install it FLOODS - a monotonically growing list of everything the machine ever doubted, which is not one reviewer's work. The default view is live major/critical LQA findings within the window; NEEDS_REVIEW is an explicit, captioned, off-by-default filter."
  - "The quality section is a DERIVED list, not an assignment. It is every document you can SEE with open major/critical findings - it does not mean anyone was told to fix them. Withdrawn findings never appear."
  - "It is a page you visit, not a notification centre. There is no bell, no unread count, no digest and no push - the only push channel in the product is the query-assignment email. The worklist is the in-app 'there is something for you' signal, and you go to it."
  - "Everything is scoped to what you can already see. A customer-bound user sees only its customer's work; a non-member under project scoping sees only its member/created projects. The count you are shown (`total`) only ever counts in-scope rows."
connects:
  - module: queries
    how: "The query section lists the threads assigned to you and your own answered ones; the dashboard's 'N queries assigned to you' line links here. Turning queries off removes the query section (only)."
  - module: workflows
    how: "The review section lists paused human-review steps whose definition names you; each row links to the run where you post-edit and resume."
  - module: reports
    how: "The quality section reads the same major/critical MQM LQA findings the qa report kind measures - the worklist is the per-person 'what needs my attention' view of that data, not a measurement."
---

## What this is for

"My work" answers one question across every project you can see: **what is waiting on
me?** It gathers three kinds of thing that used to be scattered - paused review steps that
name you, queries on you, and documents with open quality problems - into one list, newest
first. It is a page you **visit**, not a notification: there is no bell and no digest, and
the worklist never assigns, gates, or changes anything. Opening it is always safe.

## The three sections

**Review** shows paused workflow steps whose *definition* names your email. Because
assignment lives on the workflow definition and not on the individual run, a review step
that names you surfaces **every paused run of that workflow** - per-run assignment does not
exist yet, and that limitation is stated on the page rather than hidden.

**Query** shows the threads assigned to you (open or answered) and your own threads that
were answered, so you can act on the reply. It disappears entirely when the queries feature
is turned off; the other two sections are unaffected.

**Quality** shows every document you can see with a live major or critical MQM finding in a
declared window (90 days by default, `?days=` to change it). It is a **derived list**, not
an assignment - nothing in Localization OS assigns an LQA task to a person. It is reviewer-only,
grouped by document and locale.

## Why not NEEDS_REVIEW

The obvious key for "documents needing attention" would be the `NEEDS_REVIEW` segment
status - and it is the wrong one. That status is set by every soft QA gate (glossary,
placeholders, tags, numbers, whitespace, and more) and is cleared only by a human edit, so
on an established install it **floods**: a never-shrinking list of everything the machine
ever doubted. The default quality view is therefore live major/critical **findings** within
the window; the `NEEDS_REVIEW` segment list is an explicit, captioned, **off-by-default**
filter (`?quality=needs_review`) for when you specifically want it.

## Identity, honestly

The worklist is inherently per-person, so every surface needs to know **whose** work to
show. The web page uses your session. `/api/worklist` uses the API key's own user - a shared
connector token has no user and is **refused** with a named message, never an empty list
that would read as "you have no work". In-process the CLI is the trusted operator with no
session, so `work list` **requires** `--actor <email>` (or `--remote`).
