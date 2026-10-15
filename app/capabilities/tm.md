---
module: tm
title: Translation memory
summary: Every approved translation is remembered, so the same sentence never has to be translated - or paid for - twice.
nouns:
  TM entry: "One stored source/target pair for one language pair, inside one project. Written automatically when a segment is approved, or imported from a TMX file."
  Exact match: "The stored source is byte-identical to the segment being translated. These can be applied straight into a document without a model call."
  Fuzzy match: "The stored source is close but not identical, scored as a percentage. Shown to a translator as a suggestion; also usable as an example in a Translate step's prompt."
  TMX: "The industry interchange format for translation memory. This is how memory gets in and out of the tool."
  TM examples: "Similar past translations shown to the model inside a Translate step's prompt, so it matches your house style - three by default, up to five (per-step tm_examples_window, clamped 0-5), packed into a ~1200-character budgeted block. On by default for new steps."
  TM match step: "A tm_match workflow step: before any engine runs it pre-fills segments that have a qualifying memory hit and locks them - soft-locked by default (still enters LQA), or hard-locked when the '100s enter LQA' policy is off. Add it in the workflow builder or with `workflow add-step ... --type tm_match`."
  Configuring the match step: "Each tm_match step is tunable per step (Batch 15): a FILL THRESHOLD (fill matches at or above this score, 50-100; 100 = exact matches only), a FUZZY LOCK MODE for below-100 fills (none = a suggestion with no lock, soft = enters LQA, policy = the same lock the 100 band would give), tri-state overrides for lock_ice / hundreds_enter_lqa / context_floor, and a TM POOL SELECTION (which readable project TMs this step reads; blank = all readable). Each override left at INHERIT follows the matching global setting; a NEW step is seeded with a snapshot of the current globals. A fuzzy fill only lands when the segment source and the memory source carry IDENTICAL numbers/URLs/variables (a wrong number can never be filled), and it stamps its real match %. Set them in the builder's 'TM match policy' panel, the `workflow add-step` / `workflow edit-step` flags `--fill-threshold`, `--fuzzy-lock-mode`, `--lock-ice`, `--hundreds-enter-lqa`, `--context-floor`, `--tm-pools`, over /api, or through MCP create_workflow/edit_workflow. These fields are tm_match-only (refused on any other step type)."
  Concordance: "Free-text search across stored translations - 'how have we translated this phrase before?'"
  TM source: "Another project whose memory this project also MATCHES against. It is a reference, not a copy: no entry is duplicated, and the source's entries stay the source's."
verbs:
  Open from the navigation palette: "Web: Ctrl+K or Cmd+K outside text fields, type Translation Memory, then Enter. Escape closes and returns focus. The search row submits the existing top-bar search."
  Browse and search what is stored: "Web: TM in the nav - filter by project and language pair, or search. CLI: `tm search`, `tm list`, `tm stats`."
  Look at one entry: "Web: click the row. CLI: `tm show <id>`."
  Fix a bad entry: "Web: edit it on its detail page. CLI: `tm edit <id> --target \"the corrected translation\"`. An edit that drops or adds a placeholder is refused unless you tick the box (web) or pass `--acknowledge-placeholder-loss` (CLI); damaged inline tags are never saved."
  Delete entries: "Web: the delete button, or select rows and bulk-delete. CLI: `tm delete <id>`, or `tm delete --customer <id> <filters>` for bulk. A bulk delete only ever touches the customer whose TM page you are on. Deleting a document with Privacy erasure also removes the TM rows whose source matches that document's segments (a row another document shares by identical source goes too); an ordinary document delete keeps all TM."
  Load an existing memory: "Web: import TMX or a Trados Studio .sdltm on the project page. CLI: `tm import <project_id> <file.tmx or file.sdltm> --source en-US --target de-DE`. An .sdltm is read without changing it; units it cannot read are counted and listed with a reason; units with Trados tags are held back like tagged TMX units. Units that drop or add a placeholder, or carry inline tags, are held back and listed; tick the box (web) or pass `--acknowledge-placeholder-loss` (CLI) to import placeholder losses anyway."
  Take your memory with you: "CLI: `tm export <project_id> <file.tmx> --source en-US --target de-DE`."
  Fill a document from memory: "Web: the Apply 100% TM matches button on the document page - fills every segment that has a 100% match. CLI: `doc apply-tm <doc_id> <locale>`."
  Clean up hedged junk: "CLI: `tm scrub-hedges --customer <id>` (dry-run by default, `--apply` removes) finds stored entries whose target is model hedging rather than a translation; it only ever touches one customer's TM."
  Unlock a locked segment: "Explicitly clear a segment's TM lock. Web: the unlock action on the segment (POST /segments/{id}/unlock); CLI: `doc unlock-segment <segment_id>` (in-process, or over `--remote`); API: POST /api/segments/{sid}/unlock. Clearing a hard TM lock is a REVIEWER+ action: the web and API routes require project visibility AND the reviewer role or higher (reviewer/linguist/manager/admin - a translator or viewer is refused 403), so the `--remote` CLI arm (which drives the API route) needs a reviewer+ key; the in-process CLI runs as the trusted local operator and is always allowed. The allowed unlock writes a `tm.unlock` audit row and is idempotent (unlocked = false when it was not locked); a refused attempt writes no such row."
  Attach an established TM: "Point this project at another project's memory so your runs match against it too. Web: the Translation memory sources card on the project page. CLI: `project tm-sources add 7 3`, `project tm-sources list 7`, `project tm-sources remove 7 3`. The source must belong to the same customer as this project, or to no customer at all (a house project); both must be in the same organization. The web picker only offers projects that already hold entries; the CLI is deliberately not filtered that way."
needs:
  - "A project with a source and target language - TM is stored per project and per language pair, not globally."
  - "Approved translations, or a TMX file to import. An empty project has an empty memory and no matches to offer."
  - "For import: locales that actually correspond to the project's pair. A bare 'en'/'es' TMX is coerced to the project's real pair and the result is reported; an unrelated pair is refused before anything is written."
limits:
  - "TM is scoped to a project and a language pair, and never leaks across customers. It reaches another project's memory only where you have explicitly attached that project as a TM source."
  - "Attaching a TM source widens what this project MATCHES, and nothing else. Write-back, the project's TM count, TMX export, the TM browse and the editor's concordance all stay own-project only - you never read out or take ownership of borrowed entries, and your runs never touch the source's usage counters."
  - "At an equal match your own entry beats a borrowed one, so a reviewer's correction in this project takes effect immediately."
  - "Attachment is one level deep. The source's own attached sources do not chain in."
  - "A content-type mismatch between the two projects is not an error but is a no-op: the content-type scope still applies, and the command reports how many of the source's entries are actually reachable."
  - "A segment that failed the inline-tag or number-integrity checks is held out of TM. Approving it cannot launder damaged text into shared memory."
  - "A segment reverted to its source text is TM-ineligible for the same reason."
  - "Import is capped by the upload size limit, and a TMX declaring XML entities is refused outright (an amplification guard)."
  - "An .sdltm must be a Trados file-based memory whose language pair matches the requested one (either direction); another SQLite file or another pair is refused before anything is written."
  - "Fuzzy matching covers text up to 10,000 characters (about 1,500 words). A longer segment or memory entry still gets an exact 100% match but is never fuzzy-scored, so one huge segment cannot stall the app."
connects:
  - module: workflows
    how: "A Translate step with TM examples on puts three similar past translations into the prompt by default (per-step tm_examples_window, up to five). Segments filled by a 100% TM match are soft-locked by default - skipped by a normal translate step but still editable by a correction-loop fix step (which clears the lock); only a hard lock resists every step."
  - module: documents
    how: "Approving a segment in the editor is what writes to TM; Apply 100% TM matches on the document page is what reads back out of it in bulk."
  - module: glossary
    how: "Different jobs: TM remembers whole sentences you already translated, the glossary enforces individual terms. When they disagree about a term, the glossary wins."
  - module: engines
    how: "A TM hit costs nothing - no model call is made. The cheapest way to reduce spend is to have more memory."
---

## What this is for

Most real localization work is repetitive. TM is the part of the system that notices
you have translated this sentence before and either fills it in for free or shows it
to the model as an example so the new translation matches.

## Getting started

1. If you already have a TMX from a previous tool, import it into the project.
2. Otherwise it fills itself: every segment somebody approves in the editor is
   written back to memory automatically.
3. Before running a full translation, press **Apply 100% TM matches** on the document. Anything
   with a 100% match is filled in without spending a cent.

## Reading a match

- **100% / exact** - safe to apply in bulk.
- **Fuzzy (a percentage)** - a suggestion. The lower the number, the more of it you
  will end up rewriting.

## Self-healing drift

- A `tm_match` workflow step doesn't only fill empty segments. If a segment's source has a
  100% memory entry but the stored **machine** target (an `mt`/`new` value) has drifted away
  from it, the step **re-fills that segment from memory** and locks it - soft-locked by default
  (still enters LQA), or hard-locked when the '100s enter LQA' policy is off (Setup AI -> TM
  matching) - instead of leaving it for a later Translate step to re-scaffold every run. Your own **edited/approved** text is
  never touched by this - only machine values are healed - and a segment routed to review
  (`needs_review`) is left for the human. The run's `tm_match` summary counts these as
  `tm_healed`.

## The delivery empty gate

- A workflow can be **TM-only** (`tm_match -> deliver`, no LLM translator). When such a
  workflow leaves some segments unmatched, those targets stay **empty** - and a document (or
  key project) that still carries empty targets from a `tm_match` run is **held from
  delivery** until a person approves shipping the empties. The hold keys on the DURABLE
  per-run marker, not on the workflow's shape, so a `tm_match -> llm_agent` workflow whose MT
  step has not filled a segment yet is held in exactly the same way - which is why the gate is
  no longer described as "TM-only" on the refusal surfaces. This is on by default (Setup AI ->
  TM matching -> "Hold TM-only deliveries with empty targets"; the setting is
  `tm_empty_delivery_gate`).
- The hold is **live**: it is decided at delivery time from the current deliverable plus a
  durable per-run marker (`WorkflowRun.used_tm_match`), never armed at run end - so a run that
  failed, was cancelled, or was left paused with empties is held the same way. It covers every
  export surface: the web/`/api` document download, `doc export`, the project ZIP bundle, and
  the whole-project key export. It is **not** bypassable with `allow_held` (that flag only
  forgives the classic all-empty source-fallback hold; it cannot release a TM-only empty
  hold). Document download API: 401 without a token; 400 when locale is missing or blank.
- **Nothing is approved or released while a run is still in flight** over that document (or
  the project's key documents). Mid-run the targets are empty because the run has not reached
  them, not because they are final: a new approval is REFUSED (`400`, naming the run) and a
  STANDING approval is SUSPENDED - never revoked - so the export keeps refusing (`409`) until
  the run finishes or is cancelled, at which point the approval resumes by itself and covers
  whatever is still empty. A deliverable under back-to-back runs therefore stays held for as
  long as runs keep starting. A run is a run in either tier: a classic workflow run (a fan-out's member runs included, and the unfinished fan-out ITSELF only while that locale's current-attempt member is not yet created or is non-terminal; a re-run holds its selected locales from acceptance until their fresh members are terminal, and finished locales not selected still release) or an Advanced-Workflow run over the same project/document and locale. An
  Advanced mission is committed RUNNING before its harness loop starts and leaves every
  target empty until its terminal delivery write, so it holds exactly like a classic MT run;
  it matches on the mission's own bound segments, so a mission over one document does not
  hold its siblings.
- Re-run, retry-failed and run-now require a finished batch (finished_at is set).
- **A fan-out (batch) follows that same per-locale rule.** Members are created lazily,
  one per launch-loop turn. The refusal names it (`batch #3`). Wait for the batch to finish;
  `loc-tms db recover-jobs` finalizes batches with a confirmed-dead dispatcher on this host
  (install-wide). Manual recovery finalizes only batches with confirmed-dead local dispatcher processes.
  Live or unknown owners remain untouched. For unknown owners, only if the server died, after confirming no worker is live,
  restart the server to recover interrupted batches.
- **Approving** ships the unmatched targets as **empty strings** (not the source text). The
  approval is audited (`delivery.empty_approved`), records exactly the empty set the approver
  saw, and re-holds automatically if a later run adds NEW empties. Surfaces (web + CLI + `/api`
  parity, both document and key modes): the delivery-review panel shows the actual empty
  strings; `doc empties` / `doc deliver-approve` / `doc deliver-revoke` and `keys empties` /
  `keys deliver-approve` / `keys deliver-revoke` are their CLI twins. The approval is a human
  decision - an MCP agent can read the empties but never approve (the approve/revoke routes are
  MCP-exempt). A pure-LLM workflow, and any document a `tm_match` run never touched, is
  unaffected.

## Current warts worth knowing

- A TMX imported under the wrong locale pair produces a memory that never matches
  anything. The import now tells you which locales rows were actually stored under -
  read that line rather than assuming.
- TM does not deduplicate across projects. The same sentence in two projects is two
  entries, and improving one does not improve the other.
