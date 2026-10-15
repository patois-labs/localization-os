---
module: glossary
title: Glossaries and terminology
summary: The terminology authority - the list of terms that must be translated a particular way, or must not be translated at all, enforced rather than merely suggested.
nouns:
  Glossary: "A customer-owned termbase with an approval lifecycle. A pinned glossary is used only for runs whose languages its pairs cover, and only once it is approved."
  Term: "One source term with an optional target term. The unit of enforcement."
  DNT (do not translate): "A term flagged to stay in the source language - product names, trademarks, code identifiers."
  Sense: "The same source term meaning two different things, each with its own target and a context cue that tells them apart."
  Language pairs: "Which source/target pairs a glossary applies to. Any approved glossary can be pinned to a step; one that does not cover a run's languages is skipped for that run."
  Status: "draft (editable, not usable in a step) or approved (usable). Changing content reverts an approved glossary to draft."
  Term candidate: "A proposed term extracted from a document, sitting in a review queue. Nothing is ever auto-added."
  Content type: "An optional extra scope, so a glossary can apply only to (say) marketing copy."
  Concept: "The unit of meaning beneath the termbase - it groups the terms that all mean one thing, across locales and synonyms. A homonym (one surface, two meanings) becomes two concepts sharing a source surface."
  Term (concept model): "One locale-and-surface under a concept, carrying a status, an audience, a term type, part of speech and grammatical detail. Richer than the flat source-to-target row the CSV grid shows; the two are kept in lock-step."
  Term status: "preferred, admitted, deprecated, or forbidden. Only preferred/admitted terms are offered as translations; a deprecated or forbidden term is recorded so it can point at what to use instead."
  Audience: "Which channels a term is offered to - any subset of human, mt, llm. Set per term, or in bulk with set-audience."
  Term type: "The kind of term (full form, acronym, abbreviation...), a TBX-Basic data category carried through the concept store, the `.glossary.json` round-trip, and TBX-Basic export."
  Project pin: "An approved glossary attached directly to a project, so every run on it matches those terms without any workflow step naming the glossary."
verbs:
  Browse entries: "CLI: `glossary list ID --limit N --cursor ID` (project default/cap 1000); `glossary module show ID --limit N --cursor ID` (default/cap 1000). Both accept --offset and --remote. API: GET /api/projects/{pid}/glossary and GET /api/glossaries/{gid}, optional limit/offset/cursor; paged responses carry truncated and next_cursor (null at end). Offset skips after cursor; reset offset to 0 for continuation. MCP: list_assets(view=glossary) forwards limit, offset, cursor. Web entry tables show 200 terms and a next link; exports include all terms."
  Create a glossary: "Web: Glossaries -> new, choosing the owning customer. CLI: `glossary module create <name> --customer ID`."
  Add terms by hand: "Web: the glossary detail page -> add term (with target, DNT flag and metadata). CLI: `glossary module add-term <glossary_id> <source_term> --target-term T`."
  Load an existing termbase: "Web: import on the glossary page - CSV, TBX, or a concept-aware .glossary.json. CLI: `glossary module import <glossary_id> <file.csv|file.tbx|file.glossary.json> --source en-US --target de-DE` (--source/--target are ignored for a .glossary.json, which carries every locale itself). A plain TBX imports as flat per-pair entries. `--concepts` (API/MCP: `concepts=true`, web: the import checkbox) is RESERVED: the concept-PRESERVING TBX import (every language, synonym, sense and data category) is deferred to the terminology authority train, so it is refused before any parse or write, pointing you at the standard import; round-trip a full termbase through `.glossary.json` meanwhile. A base-language termbase (`xml:lang=\"es\"`) projects onto a regional pair (`es-ES`) by base language, and the `.glossary.json` concept arm reports `projected_entries` (how many entries THIS import's terms enforce for the glossary's pair(s), so a re-import reports the subset its own terms cover)."
  Load a MultiTerm or Trados termbase: "Export it from MultiTerm as MultiTerm XML (or TBX) and import the .xml/.mtf/.tbx file like any termbase: web glossary page import, CLI `glossary module import <glossary_id> termbase.xml --source en-US --target de-DE`, API/MCP the same import with the file bytes. MultiTerm XML (UTF-16 is fine), the TBX 2002 default dialect, TBX-Basic and TBX v3 are read; definition, subject, part of speech, context and notes are mapped. Forbidden or deprecated terms are never used as the required translation; they are kept as do-not-use terms. The import reports each entry as imported, merged into an existing term, or skipped with the reason."
  Say which languages it covers: "Web: the language-pairs control on the glossary page. CLI: `glossary module set-pairs <glossary_id> --pair en-US:de-DE`. Widening the set (adding a pair) auto-creates the missing per-pair bindings for every concept that has a usable source and target for it, and reports `bindings_created` / `dnt_bindings_extended` (web `?msg=` banner, `--json`, `/api` and MCP response); narrowing leaves the existing bindings in place."
  Make it usable in workflows: "Web: the Approve button. CLI: `glossary module approve <glossary_id>`. Until then it will not appear in any step's dropdown."
  Mine a document for terms: "Web: 'Extract terms from a document' on the glossary page. CLI: `glossary module extract <glossary_id> <document_id>`. Candidates land pending for review - nothing is added automatically."
  Work the candidate queue: "Web: the review grid on the glossary page. CLI: `glossary module candidates list|accept|drop`. A dropped candidate is never re-proposed."
  Take it away: "Web: export from the glossary page. CLI: `glossary module export <glossary_id> <out.csv|out.tbx|out.glossary.json>`. The .glossary.json (glossary-concept/v1) is the concept-lossless format - every concept, term, status, audience and term type round-trips, and after T1b a per-concept `bindings` block carries each pair's enforced target, DNT, case-sensitivity and meta overrides, so the concept-lossless guarantee holds for documents with a bindings block (the product's own export always emits one; a pre-T1b document without bindings restores through the A.4-2 fallback). CSV is lossless only for what it carries, and TBX rides a private namespace for the house fields."
  Manage concepts: "The concept level beneath the termbase groups the terms that mean one thing across locales. Web: the concept editor on the glossary detail page. CLI: `glossary module concepts list <glossary_id>`, `glossary module concepts add <glossary_id> --term en-US:invoice`, `glossary module concepts show <glossary_id> <concept_id>`."
  Concept EDITING is flag-gated (default off this release): "The concept/term-store EDITOR (create/edit/delete concept, add/edit/delete term, set audience) ships behind the DEFAULT-OFF feature `terminology_concept_editor`; with it off those editor write seams refuse on all four surfaces (web/API/CLI/MCP) naming the switch. READING concepts (list/show, `.glossary.json` export, the read-only web view) is always available. After T1b (LAW-TERM-18 authority inversion) a `.glossary.json` / concept import is UNGATED on every install - a populated import is allowed regardless of the flag, upserting only the named concepts and reconciling id-less concepts by content identity. Turn the editor on with `config feature enable terminology_concept_editor` (or Settings > Features, or POST `/api/admin/features/{slug}`)."
  Add or edit concept terms: "Each term is one locale-and-surface carrying a status (preferred, admitted, deprecated, forbidden), an audience, a term type and grammatical detail. CLI: `glossary module terms add <glossary_id> <concept_id> --locale es-ES --term factura --status preferred --audience human,mt,llm`, `glossary module terms edit <glossary_id> <concept_id> <term_id> --status deprecated`. The per-pair CASE-SENSITIVITY fact is edited on the compat row editor (`glossary module edit-term <glossary_id> <entry_id> --case-sensitive` (or `--no-case-sensitive`), and the `/api` `POST /api/glossaries/{gid}/terms/{eid}/edit` `case_sensitive` field / MCP `edit_glossary`); the concept-term editor's DNT is a tri-state (turn it off from the web term-edit form, not only on)."
  Set who sees a term: "The audience (any subset of human, mt, llm) controls which channels a term is offered to. CLI: `glossary module terms set-audience <glossary_id> --term 5 --audience human,mt`."
  Pin a glossary to a project: "Attach an existing module glossary to a project so every run on it uses those terms, with no workflow step involved. Web: the Attached glossaries card on the project page. CLI: `project glossaries add 7 4`, `project glossaries list 7`, `project glossaries remove 7 4`. It works on projects with and without a content type. The glossary must belong to the project's customer, or to no customer at all (a shared house glossary); both must be in the same organization, and you must be able to see the glossary in the glossary module."
needs:
  - "A customer. Every module glossary belongs to one."
  - "At least one language pair set on the glossary for it to actually apply to a run. A step can pin any approved glossary regardless of pair; one whose pairs do not cover a run's languages is skipped for that run (the run log says so)."
  - "Approval. A draft glossary is invisible to workflow steps by design."
  - "For extraction: a document to mine, and a live engine - extraction is an LLM pass with a spend cap."
  - "Linguist or admin rights for content edits."
limits:
  - "A glossary must be APPROVED to be pinned to a step, and a pinned glossary is used only for runs whose languages its pairs cover. If it is missing from the dropdown, it is not approved."
  - "Editing content reverts an approved glossary to draft, so a step pinned to it stops getting the new terms until you re-approve. A re-import reverts it only when it CHANGED content; an idempotent re-import - the same file again, every row byte-identical to what is stored - deliberately does NOT revert it, in ANY format (CSV, the flat TBX entry path, or a `.glossary.json`). For the `.glossary.json` concept import the change is measured on the whole concept/term store the prompt renders (a rewritten definition, a term's status or audience), not just the enforced entries."
  - "Term metadata (part of speech, definition, subject field, notes, usage example, plus the concept-model status, audience, term type, grammatical gender/number and geographical usage) is organizational by default: with a step's concept-glossary renderer OFF (the default) it never reaches a prompt. Turn that step toggle ON and the metadata reaches the model ONLY through the audience-gated renderer - a term marked human-only is never sent, and the block is ordered (do-not-translate first) and capped."
  - "The concept-glossary renderer token cap has two levels, both using 0: a workflow step's own glossary_token_cap of 0 means inherit the install-wide glossary_prompt_token_cap (Settings), and the install cap's own value of 0 means no token limit (unlimited)."
  - "TBX export defaults to TBX-Basic v3 (DCA style). House fields with no standard data category - DNT, audience, forbidden/superseded status, UI/label term types, subdomain, out-of-picklist part-of-speech/gender/number, usage note, case-sensitivity, exact-match - ride a documented private namespace (los:), so a stock TBX-Basic validator sees them as foreign findings; a consumer that loads our extension reads them. `--strict-portable` drops the los: fields and emits a clean pure-TBX-Basic instance (lossy, but validates with zero findings). The .glossary.json (glossary-concept/v1) remains the no-caveat lossless format. `strict=true` on import validates against the vendored TBX core RELAX NG and an in-repo evaluator of the vendored TBX-Basic DCA Schematron, and refuses the first finding; without it, findings are warnings."
  - "The module CSV export is 16 columns: source_term, target_term, dnt, sense_label, context_cue, then the five meta columns (part_of_speech, definition, usage_example, subject_field, notes) and six concept-model term columns (status, term_type, audience, grammatical_gender, grammatical_number, geographical_usage). status and audience are always populated on a linked base row; the other four are blank when unset, and a sense row leaves the six blank."
  - "Terms match whole words in every script written with spaces (a term never matches inside a longer word, so an inflected form such as Russian, Polish or Finnish case endings is not a match; add those forms as their own terms). Chinese, Japanese, Thai, Lao, Khmer and Myanmar terms match anywhere in the text, since those scripts do not separate words."
  - "Longest match wins: when a shorter term occurs only inside a longer matched term (trail inside the DNT Colorado Trail), the shorter term is neither sent to the engine nor checked for that segment, across every glossary the step uses. An occurrence outside the longer term is still sent and checked. Only a longer term with a target or a DNT mark can hide a shorter one from the check."
  - "Extraction over-proposes on purpose and then prunes. Expect to reject a lot; every rejection is remembered so it is not re-proposed."
  - "There is no 'describe what you want' Draft box for glossaries yet - use extraction or import."
  - "A big CSV or TBX import is stored in short transactions (250 entries for a module glossary), so other writers are not held up; a file that fits one transaction is all-or-nothing, a bigger one that fails part way keeps what was stored (an approved glossary is back in draft) and importing the same file again finishes it with no duplicates. An entry someone deletes while the import runs is not restored; the summary says how many."
connects:
  - module: workflows
    how: "A Translate step can pin one approved glossary; it applies to runs whose languages its pairs cover. Its terms and DNT entries go into the prompt AND are checked afterwards - if a fix step introduces a new violation, the edit is discarded and the segment is flagged."
  - module: documents
    how: "Terms are extracted FROM documents, and glossary violations are flagged ON a document's segments during translation."
  - module: tm
    how: "TM remembers whole sentences; the glossary enforces single terms. When a remembered sentence contains a term that has since changed, the glossary wins and the segment is flagged for review."
  - module: engines
    how: "Term extraction is a real model call and is spend-capped. Offline, the deterministic mock still proposes candidates so the loop works without a key."
---

## What this is for

Some words are not negotiable: a product name, a legal term, a UI string that has to
match the software. TM remembers sentences and memory remembers habits - the
glossary is the one that is actually *enforced*.

## Getting started

1. Create a glossary for the customer.
2. Add its language pairs.
3. Put terms in - by hand, by importing a CSV/TBX, or by extracting candidates from
   a real document and accepting the good ones.
4. **Approve it.** Nothing sees a draft glossary.
5. On a Translate step in the workflow builder, pick it from the glossary dropdown.

To say "never translate this", add the term and tick **DNT**. You do not need a
target term for a DNT entry.

## Which glossary applies, in order

Four rungs, most specific first:

1. A **step pin** on a Translate step is an exclusive override - if it is usable. The
   steps are scanned in order and the first pin that is **approved**, **covers the
   run's language pair**, and **matches the project's content type** wins outright,
   with nothing merged into it. A pin failing any of those three is **skipped**, not
   an error: the scan moves on to later steps, and if none qualifies the run continues
   down this list as though no step pin existed. (That is deliberate - workflows are
   shared across projects, so a step pinned to the Marketing glossary must not inject
   marketing terms into a Product project reusing the same workflow. The skip shows up
   as a note in the run trace.)
2. Otherwise, **project pins** union in first, on typed and untyped projects alike, and
   they outrank auto-attach on a term conflict - including a pin whose content type
   differs from the project's, which is why the web picker labels those. They are
   filtered by approval and by the customer/org ring, but not by content type.
3. With no pin at all and a **typed** project, every approved glossary covering the pair
   is auto-attached: content-type-specific first, then General, then legacy per-project
   entries.
4. With no pin and an **untyped** project, nothing is auto-attached beyond legacy
   per-project entries.

A **draft** glossary contributes nothing at any rung. Pinning one is allowed and is
simply inert until you approve it. Watch what that means for a STEP pin, though: an
unapproved step pin does not make the run go without a glossary, it makes the run fall
through to the project pins and auto-attach below - so the step may quietly translate
against a glossary you did not choose.

## How enforcement actually works

Two things happen, and the second is the one that matters:

1. Matching terms are put into the translation prompt as approved terminology.
2. The output is checked against those terms afterwards. Prompts persuade; the check
   is what enforces. A correction step that introduces a NEW violation has its edit
   discarded and the segment flagged, rather than shipping the regression.

## Current warts worth knowing

- The commonest confusion is a glossary that does not appear in a step's dropdown.
  The dropdown lists every approved glossary you can see, so an absent one is simply
  not approved (or is outside your org/customer ring). Its language pairs no longer
  gate the dropdown; a pin whose pairs miss a run's languages is skipped for that run.
- Re-importing a file that rewrites every target reverts the glossary to draft - and
  should. Re-importing the identical file does not.

Glossary detail defaults to 1000 terms. Both glossary API routes reject limit above
1000 with a typed 400. Follow next_cursor or use an export to read every term.
Malformed or non-positive cursors return 400 (cursor must be a positive integer);
an empty cursor starts page one. CLI cursor validation uses exit 2.
