# How to actually use the new stuff - a plain-language guide

For: an operator who is not an expert in this app. Everything here is doable in the web
UI unless marked CLI. Written 2026-08-02, last content update 2026-08-04.

---

## The 30-second mental model

- **A workflow** is a recipe: an ordered list of steps a document runs through.
  Steps are one of five kinds: **Translate** (an LLM writes the translation),
  **Check** (an LLM judge scores it - "LQA"), **Review** (a human looks at it),
  **TM match** (fill + lock exact translation-memory matches so later steps skip them),
  and **External** (dispatch the packaged text to a vendor and park the run until it returns).
- **Memory** is the system's notebook about a customer: terminology decisions and
  style rules it should keep applying. Some entries you write; some it learns from
  corrections people make.
- **The Evaluations Lab** is a sandbox for one question: *"which setup translates
  better?"* You give it content, two or more setups, and it scores them side by side
  and tells you if the difference is real.
- **Engines** are the models that do the work. Your default local engine (set on the
  **Setup AI** page) is your local model (for example in LM Studio); any step that says provider
  "local" with no model pinned uses it.

---

## Memory (LLMemory): teach it a rule

**What it does:** when a Translate step runs, the system quietly adds up to two
relevant terminology decisions and one style note into the prompt - scoped to that
customer. That's the whole trick. You can see exactly what it injected (below).

**Teach it something (1 minute):**
1. Go to **Memories** in the nav. Pick the customer.
2. Add a note. Two shapes matter:
   - **Term decision** - "when the source says X, the translation uses Y".
     Also how you say "never translate our product name": make X and Y the same
     (e.g. `Acme Cloud → Acme Cloud`).
   - **Tone/style rule** - free text, e.g. "Formal register: address the reader as
     usted." Scope it to a project if it only applies there.
3. That's it. Next run for that customer, if a segment's source contains the term,
   the decision rides along.

**See it working:** open **LLM Ledger** (admin) after a run - records with a
"memory" chip show exactly which memories were injected into which segment's prompt.
That chip is your proof it's on.

**It also learns by itself:** when someone corrects a translation in the editor and
saves, the before/after is captured as evidence. Run **distill** (Memories page
button, or CLI `memory distill`) and it turns repeated, consistent corrections into
new term decisions - which start life needing enough evidence, unlike your manual
notes, which are trusted immediately.

**Undo:** every memory has an **invalidate** button (kept but off) and a purge
(gone). There's also a "roll back everything it learned since <date>" verb (CLI
`memory invalidate-since`) if it picks up something bad.

---

## Parallel steps: run two checks at once

**What it does:** two Check (LQA) steps that sit next to each other can share a
**parallel group** - they then run at the same time against the same translation,
and the workflow continues when both finish. Use it for "quality gate + terminology
check simultaneously".

**Set it up (builder page):**
1. Open the workflow. Add (or edit) two **Check** steps so they're adjacent.
2. Give both the same **Parallel group** slug (e.g. `checks`). The ∥ chip appears.
3. Exactly **one** of them may carry the branch (e.g. `on_fail → fixer step`) -
   that one decides where the run goes after the group. The other lane's score is
   still recorded and still usable in conditions.
4. If a later step needs a *specific* lane's outcome, use a keyed condition:
   `step:<that step's key>:failed` instead of plain `prev_lqa` - the app will
   actually refuse the ambiguous form and tell you the exact syntax to use.

**On the graph:** group members sit inside a dashed outline. A lone member (nobody
next to it shares the slug) shows the chip greyed with "not active" - it just runs
normally, in order.

---

## The canvas: rearrange a workflow by dragging

On the workflow builder page, the graph is now editable:
- **Drag a step by its grip handle** (the dotted area on the node) up or down to
  reorder. Each drop saves immediately; an illegal drop bounces back with the exact
  reason.
- **Drag a Check step into a group's dashed band** to make it run in parallel;
  drag it out to a normal slot to make it sequential again. Both halves (position +
  group membership) happen together - no separate edit needed.
- Prefer buttons? Every step row has **move up / move down** buttons that do the
  same thing.
- Things that would break the workflow (splitting a group, moving a gate below the
  step that depends on it) are refused with the validator's message - the canvas
  can't save anything the forms would reject.

---

## Evaluations Lab: find out which setup is better

**The loop is always the same:** dataset → candidates → run → compare → promote.

1. **Dataset** = the content you test on.
   - **Upload**: one segment per line; add a TAB + the known-good translation if you
     have references (this unlocks the strong metrics like BLEU/chrF).
   - **Snapshot a document**: copies a real document's segments (and current
     translations as references) into the Lab. It's a frozen copy - later edits to
     the document never change the dataset.
   - Aim for **30+ segments** - below that the statistics engine will honestly
     refuse to call a winner (you'll see "indicative only").
2. **Candidates** = the setups you're comparing. Easiest: point each candidate at a
   workflow. To compare two *models*, make two one-step Translate workflows that
   differ only in the model, and one candidate for each.
3. **Run**: pick dataset + candidates + scorers. You'll get a **cost estimate
   first** and must confirm. Notes: reference metrics (BLEU/chrF/TER) need a
   dataset with references; `judge_mqm` uses an LLM judge - if the judge is the
   same model as a candidate, the app warns you it may favor itself (trust the
   deterministic metrics as the cross-check). A local-engine run costs $0.
   ⚠ Current wart: long runs can make the app slow/unresponsive until they finish -
   start them and get coffee. (A fix is chipped.)
4. **Compare**: pick the dataset, the baseline candidate, and the candidates -
   ⚠ current wart: include the baseline in the candidate list too, or it silently
   compares against the wrong thing (fix chipped). You get a verdict per candidate:
   **Improvement / Regression / Tradeoff / Tie**, with the judge's reasoning
   inline - never just a bare number.
5. **Promote**: one click turns the winning candidate into a real production
   workflow. "Supported" on the promote screen means the statistics actually back
   the win at your margin - it refuses to pretend otherwise.

**Example:** we uploaded 40 WMT sentences with references, made
"Local default" and "Llama 3 8B" candidates, ran both for $0, and the Lab called
the local default an Improvement (BLEU 43 vs 36) - then promoted it. That's the whole intended
loop, and it took four commands.

---

## Ask + Draft + module guides (`/help`)

**What it does:** every main page now has two things in the corner and one box.

- **A "?" next to the page title** opens that module's guide - a short, task-first
  page written for someone who is not an expert in this app. There is one per
  module (seventeen visible by default - documents, keys, workflows, TM, glossary,
  memories, evaluations, engines, connectors, images, routing, reports, vendors,
  queries, worklist, data export, plus the machine-facing LLM overview; Advanced
  Workflows adds an eighteenth card once enabled), and `/help` lists them all at
  the top.
- **"Ask about this page"** - a question box at the bottom of the page. Type a
  question, get an answer. The answer comes **only** from that module's guide (plus
  the guides it connects to), never from what the model happens to remember about
  translation software. If the guide doesn't cover your question it says so plainly
  and points at the nearest thing it does cover, rather than inventing a button.
  Every answer carries a footer naming which guide it came from.
- **Three starter questions** are offered as one-click chips: *What can this do?*,
  *How do I get started?*, *How does this connect to the rest of the app?*

**"Describe what you want" (Draft)** - on **Memories** and **Evaluations** only.

- On **Memories**: type the rule in plain words ("we never translate our product
  names", "always use usted"). It comes back as the ordinary add-note form(s),
  filled in, one per rule it found.
- On **Evaluations**: type what you want to find out ("is the new model better than
  the old one on my support articles?"). It comes back as the ordinary run-start
  form, filled in from **your actual datasets and candidates** - with one sentence
  per choice explaining why, and warnings where it had to drop something (a metric
  that needs references your dataset doesn't have, a sample too small to mean
  anything).

**Draft never writes and never launches.** What you get is a filled-in form. The
save/launch button on it is the same button as always, and the cost estimate,
judge-bias warning and confirm screen all still happen. If you close the page
without pressing anything, nothing happened.

**Two honest limits worth knowing:**
- With no engine configured, Ask says so and points you at the guide instead of
  making something up. Draft says it couldn't draft that. Neither pretends.
- A **viewer** doesn't get the Ask box (asking is a request the role can't make);
  the "?" guides are there for everyone.

**Turning it off:** `assist_enabled` is the kill switch - `config set
assist_enabled 0` hides every button and turns the routes off on both the web UI
and the CLI. The `/help` guides stay up either way; they're just documentation.

**Which engine answers:** `assist_provider` / `assist_model` (default: your local
engine, so it costs nothing). A cloud engine goes through the normal spend ledger.

**CLI:**
```bash
loc-tms ask memories "how do I stop it translating our product name?"
loc-tms ask "which module decides what a step's engine is?"     # no module = ask across all guides
loc-tms memory draft "we never translate Acme Cloud" --customer 1
loc-tms eval draft "is our local model better than Llama on the support articles?" --customer 1
```
Both `draft` verbs print the drafted values **plus the exact ordinary command** that
would apply them, so you can copy it and read it before running it. Adding
`--apply --yes` runs that same command's seam; `--apply` without `--yes` refuses in
words and writes nothing.

---

## Key-based projects: the second kind of project

Until now every project was a **document** project: a file goes in, a translated file
comes out. There is now a second kind - a **key** project - for the other shape of
localization work: a product whose strings change every week and needs the current set
in every language, forever.

**Making one.** New project -> **Project type** -> **Keys** (or, headlessly,
`project create "My app" --targets de-DE --customer-id 3 --type key_based`). Pick at least one target
language (a key's value is stored per language, so with none there is nowhere to put
it). The type is fixed once created - there is no conversion in either direction.

**What you see.** A key project has no document list. Its project page links to the
**key grid**, which is two columns: a sticky left rail with the controls (Filter,
Translate, Add a key, Export, Import, Key settings) and, on the right, the keys
themselves - **one card per key**, grouped under a heading per namespace. Inside a card
the source text comes first, marked with the project's source locale, and then one
labelled block per target language with its own box and Save button. A plural key gets
one box per CLDR form, each on its own line. (It used to be a matrix table with one
column per language; that packed every plural form into a single cell and wrapped
mid-form, so the cards replaced it.) The namespace is usually the file a key came from
and is part of the key's identity, so `common.json` and `admin.json` can both define
`title` without fighting.

**Getting strings in.** *Import* on the grid takes JSON (flat or nested) or gettext
`.po`. One bad key is reported and the rest of the file still lands - you get a count
plus a per-key error list, never an all-or-nothing crash.

**Getting strings out.** *Export* has two shapes, and it matters which you ask for:

- Pick a **namespace** -> the **build file**: bare key names, exactly what your app
  loads.
- Leave it on **whole project** -> the **transport file**: every name written
  `namespace::name` plus a marker saying so. That is the one you feed back into the
  tool (import it with the namespace `@from-key`).

The shape follows your choice and never the data. That is deliberate: a rule that
qualified names "when the export spans more than one namespace" would rewrite every
name in your next export the day somebody added a key in a second namespace.

**Translating.** The *Translate* control picks a workflow and a language - or **all**
languages at once - and runs it exactly like a document run - same run view, same TM, glossary, QA and spend safety.
Deleted keys are never included.

**Editing the source text.** Changing a key's base value flags every language that
already had a translation as **needs review**, so a source edit can never silently ship
a stale string. Per-project toggle at the bottom of the grid if you want it off.

**Character limits.** A key can carry a **char_limit** (Key settings, or `char_limit` on
the API/import). A value that exceeds it is still saved and stays linguistically valid, but
it is flagged and routed to **needs review** - the grid shows an over-limit badge and the
export report lists it. One thing to know before you build: on the **`.po` export path** a
needs-review value is written `#, fuzzy`, and `msgfmt` **drops fuzzy entries by default** (so
the app falls back to the English/base string for them). On a limit-heavy project, review and
clear the over-limit flags before you compile a build, or those strings will ship as English.

**Retiring a string.** *Archive* it to keep it out of new build files while keeping the
record. *Delete* it to tell every sync consumer it is gone - the translations are kept,
so it can be restored: tick **include deleted** in the grid filter and press *Restore*.

**Plurals.** Tick *plural* and each language gets exactly the CLDR forms it needs
(English two, Russian three, Arabic six), each edited and checked on its own.

### The change feed (CLI) - the part nobody else has

Ask *what changed since last time*, including deletions:

```bash
python -m app.cli keys changes --project 7 --cursor-file
```

It prints every creation, edit and deletion after your stored position and writes the
new position back to `.locstudio-sync.json`. Run it in CI and act only on what moved.
If your stored position is older than the retained history (90 days by default) it
tells you to re-bootstrap rather than claiming nothing changed.

Ten verbs are available headlessly - `keys add`, `keys list`, `keys set`,
`keys edit`, `keys delete`, `keys restore`, `keys import`, `keys export`,
`keys translate`, `keys changes` - and every one of them works over `--remote` with an
API key carrying the new **keys** scope (create one on your Account page). See `docs/CLI.md`.

Launching a run over the keys is **no longer web-only**: *Translate* now has a command,
`keys translate --project ID --workflow ID --locale LOC` (or `--locale all`), which also
mirrors `POST /api/projects/{id}/keys/translate` and the MCP `translate_keys` tool. One
grid control remains web-only for now - the per-project *auto-unverify* toggle.

---

## TM matching: reuse and lock exact matches (the "TM match" step)

Your **translation memory** already remembers every human-accepted translation. The new
**TM match** workflow step turns that memory into an explicit, up-front stage: put it
first in a workflow and it fills in every segment whose source is a **100% exact match**
of something the TM already knows, then **locks** those segments so the rest of the
workflow - the LLM translation, the LQA check, any fix rounds - **skips them entirely**.

Why bother:

- **Cost & speed.** A locked segment never reaches the LLM. You can see the split before
  anything runs.
- **Consistency.** An exact match is the product's own memory of an accepted translation.
  Re-translating it with a model is pure downside.
- **Safety.** The lock is the same rule this app draws everywhere: *a person may make the
  authoritative call; an LLM may not.* A locked segment cannot be overwritten by any
  automated step.

Two kinds of lock:

- **Soft lock** (the default for 100% matches): the segment is filled and skipped by
  normal translation, but it still enters LQA and can be edited in an LQA-flagged fix
  round.
- **Hard lock**: no automated step may ever change it. Only a **person** can - and simply
  **editing a locked segment in the CAT editor / key grid clears the lock** (your edit
  wins, and it's audited). To remove a lock without editing, use the **Unlock** action
  (CLI: `doc unlock-segment <id>`).

**The policy** (three settings on **Setup AI → "TM matching & locks"**, or the CLI
`config` group `tm_lock_ice` / `tm_hundreds_enter_lqa` / `tm_context_floor`):

- **100% matches enter LQA (soft lock)** - on by default. Turn it off to make 100%
  matches *hard* locks instead.
- **Hard-lock in-context (ICE / 101) matches** - for a future in-context-match wave; inert
  today.
- **Context-attach floor** - for the deferred context/fuzzy-context wave.

These settings are **global** for now (a labelled interim; a per-TM version is planned).
The `TM_LOCKS_ENABLED` master kill switch is an **environment variable** (needs a restart),
not a UI toggle.

**Where you see a lock:** the segment's headless detail (`doc show --detail` / the segments
API) shows a `lock` block; a **transport** export (whole-project JSON or PO) carries the
lock state so a round trip preserves it (a **build** deliverable never does); and a key
project's **delta feed** records the lock/unlock as an `updated` event.

*Not yet in this version:* in-context (ICE/101) matches, TM write-back from a fix round,
and native XLIFF export of locks - all planned follow-ups.

---

## Quality judges and auto-fix (Jev, SemIf) (added 2026-09-22)

**What it does:** a Translate step with its **QE routing gate** on scores every segment
and sends the low ones to review. The scorer can now be a **quality judge**: Jev by
TypeSafe (hosted, a fraction of a cent per segment) or a Jev-compatible server you run
yourself, such as SemIf on a GPU box (free). A judge only scores; it never translates.

**Set one up (5 minutes):**
1. **Setup AI → Engines → + Add engine.** Pick kind **Jev (TypeSafe, hosted)** and paste
   the key, or **Jev-compatible local server (SemIf, Kev)** and give its URL (for example
   `http://127.0.0.1:8077`, or the GPU box's Tailscale address). If the install already has
   `TYPESAFE_API_KEY`, the **Quality judges** group offers a one-click **Create a Jev engine
   from TYPESAFE_API_KEY** instead.
2. On the new card open **Edit → Test**: it lists the models and asks one tiny question.
3. Press **Inspect** on the card to see exactly what the judge is asked and how it
   decides, and use **Try it** on a sentence you know is wrong.
4. In the workflow builder, on the Translate step: tick **QE routing gate**, pick the judge
   as the **QE judge engine**, save.

**Auto-fix failures (optional):** with a Jev judge picked, the step shows **Auto-fix
failures**. Tick it and a failing segment is rewritten by an LLM post-editor (the step's own
engine by default) and re-scored, up to 3 rounds by default. A fixed segment passes; anything
still failing goes to review with every round on its badge. A confident major failure is fixed even when another dimension is unsure. The
unsure dimension is left unchanged and flagged for review; without a confident major,
the loop skips the unsure verdict. One that scored at or above the threshold but
with an unsure answer is *undecided*: it carries on unchanged and is not sent to review; in
the editor its **qe** badge reads "QE undecided" when you hover it.

**Full walkthrough** (SemIf install, Tailscale, troubleshooting):
`docs/guides/jev-compatible-server.md`. In the app: Help → *Engines & Setup AI → Quality
judges* and *Workflows → Auto-fix failures*.

---

## Which model is doing the work?

- **Setup AI → local** holds the default local model (whichever model you chose). Any
  step with provider `local` and a blank model uses it. Pin a model on a step only
  when you want that step to differ.
- **Test it's alive**: the Fetch models / Test connection buttons on the **Setup AI**
  page hit LM Studio directly.
- ⚠ One sharp edge found in live QA (fix chipped): the quality-judge side decides
  "is the local engine alive?" from the environment variable, not the settings
  screen. This is now set correctly on current installs; if local Check steps ever
  come back instantly with score 100 and no findings, that's the symptom.

## Where to look when something's weird

The LLM Ledger groups production calls into `workflow`, `direct`, and `advanced` actions.
An Advanced run's `action_id` is its ledger action id when envelope recording is enabled.
Find it on the web Ledger, `/api/prompt-tap/actions` and its action summary route,
`prompt-tap actions --summary` (local or `--remote`), or MCP `ledger_summary`.
All retain the existing org/customer boundaries. Envelopes record the run even when raw
capture is off; captured calls supply the derived call counts and spend totals. Disabling
envelopes or a failed envelope write leaves Advanced execution and its own spend unchanged.

- **Run view**: every step's score, gate, visits, cost; the graph shows the path
  the run actually took.
- **LLM Ledger** (admin): the exact prompt and reply for every model call - with
  the memory chip, so you can see precisely what the model was told.
- **Export warnings**: if segments come back in English with a "substituted"
  warning, the export guard caught damaged formatting and shipped the source
  instead of a broken file - that's it working, not failing.

---

## New since v37

### Borrow another project's translation memory

**What it does.** Point a project at another project's TM so your runs *match* against
it too. On the project page, the **Translation memory sources** card. On the command
line: `project tm-sources add 7 3`, `project tm-sources list 7`,
`project tm-sources remove 7 3` (local only - no `--remote`).

**The one thing to understand: it is a reference, not a copy.** Nothing is duplicated.
Attaching widens what you *match*; it does not widen what you can *read out* or *own*:

- Write-back, the project's TM count, TMX export, the TM browse and the editor's
  concordance all stay your own project's entries only.
- Your runs never touch the source project's usage counters.
- On an equal match, **your own entry wins**, so a reviewer's correction in this
  project takes effect immediately instead of losing to a borrowed row.
- It is **one level deep**. The source's own attached sources do not chain in.
- The source must belong to the **same customer** as this project, **or to no customer
  at all** (a house project you keep for install-wide material). A source under a
  *different* customer is refused, and both projects must be in the same organization.
  A self-link is refused.

The web dropdown only offers projects that already hold at least one entry - the card
is for attaching an *established* memory, and listing every project in the install would
bury the ones that carry any. The CLI is deliberately not filtered that way, so you can
wire up the link before importing into the source.

If the two projects have different content types, the attach is not an error but may be
a no-op: the content-type scope still applies, and the command reports how many of the
source's entries are actually reachable for you.

### Pin a glossary to a project

**What it does.** Attach an approved module glossary directly to a project, so every run
on it uses those terms with no workflow step naming the glossary. On the project page,
the **Attached glossaries** card; on the command line `project glossaries add 7 4`,
`project glossaries list 7`, `project glossaries remove 7 4` (local only).

**Which glossary wins**, most specific first:

1. A **step pin** on a Translate step overrides everything. Nothing is merged with it.
2. Otherwise your **project pins** union in first, on projects with and without a
   content type, and they **outrank** whatever the content type auto-attached - including
   a pin whose content type differs from the project's, which is why the picker labels
   those.
3. With no pin and a typed project, every approved glossary covering the pair is
   auto-attached: content-type-specific first, then General, then legacy per-project
   entries.
4. With no pin and an untyped project, nothing is auto-attached beyond legacy entries.

A **draft** glossary can be pinned but contributes nothing until you approve it. The
list marks it `no (needs approval)` so it does not look like it is working.

### Add a language to a workflow group from the group page

**What it does.** The **Add a language pair** control on a workflow-group page: tick the
target languages and submit. It copies the group's **default** variant - or, if the group
has no default, its lowest-sorting member (by pair, then id, so it is stable rather than
"whichever was added first") - onto each one, through the same machinery as *copy to
pairs* on a workflow, so naming, provenance and the duplicate guard behave identically.
The copy source is always chosen from the variants **you can see**. Previously adding a
third language meant navigating into one variant's builder first.

- **A pair the group already covers is shown ticked and greyed out**, labelled
  *already covered*, so you cannot re-submit it by accident - the picker takes the
  question off the table rather than letting you ask for something it would refuse. (The
  underlying seam still skips-and-reports a duplicate, which is what protects the CLI
  and API paths; from this page you simply never reach it.) A mixed submit creates the
  pairs that are genuinely new.
- Variants are named after the family's stem plus exactly **one** pair suffix -
  `ACME Workflow [en-US->de-DE]`. Copying from a workflow that is itself a variant strips
  the old suffix first, so names never compound into
  `ACME [en-US->fr-FR] [en-US->de-DE]`.
- Tick **adapt** and each copy's prompts are rewritten for its own language and flagged
  for review. An adaptation that fails keeps the original prompt and warns - it never
  loses the copy.

### The live progress strip on the key grid

**What it does.** Pressing **Translate** on a key grid used to hand you back a static
page. Now the runs it launched appear as a strip at the top of the grid, streaming their
status live.

**It reloads the grid only when every tracked run ended cleanly.** If any run FAILED or
is PAUSED for review, the strip stays put and offers you a reload link instead - because
a reload wipes the strip, and reloading past a failure would erase the one message this
widget exists to show.

It also stopped saying "Started" for things that did not start. Each language now gets
the sentence that is actually true:

- **started** - a fresh run is executing.
- **restarted** - a stale run was superseded and a fresh one took its place, naming both
  run numbers.
- **already running** - a live run is on it; nothing new was launched.
- **paused for review** - an existing run is waiting on a **person**. No amount of
  clicking Translate will move it; go and review it.
- **nothing to translate** - there was no content for that language yet.

The strip tracks at most six runs (each one holds a browser connection open). If you fan
out over more languages than that the extra runs are unaffected - they are simply not
being watched, and the notice says so rather than quietly showing six of nine.

### Run recovery notices, everywhere

The same honesty applies to document runs. Starting a run that is already under way does
not start a second one - the launcher re-attaches to the live run and tells you. A run
left RUNNING with no activity for long enough is treated as **stale**: it is failed with
a note naming the run that replaced it, and a fresh one starts. That matters on an
install that is never restarted, where a dead run used to block its document/language
forever while every Translate reported success and did nothing.

A run of a DIFFERENT workflow on the same document and language is a different story
(GitHub #213): there is nothing to re-attach to, and running both would interleave their
write-backs over one set of segments. That launch is **refused** with a message naming
the run that is executing and how to cancel it. The same sentence appears on the web, the
CLI (local and `--remote`) and the API (HTTP 409, `code: run_conflict`). Cancel the running
run (or wait for it) and launch again. A batch that includes such a locale keeps the
house behavior: it skips that locale, runs the others, and names the skip in its notes.

Three things are deliberately never swept: a run whose thread is still alive in this
process, a batch member (the batch dispatcher owns those), and anything that is not
RUNNING - a **paused** run is durable human state and a reviewer may come back to it days
later.

### Existing workflows lose an inert language pair

**What changed.** A workflow no longer needs a language pair to run, so the upgrade clears the
pair that older "Design a workflow" builds stamped on workflows that are NOT in a language
group. That pair never did anything while a workflow ran, but it filtered the glossary dropdown
and made the workflow look paired. After the upgrade those workflows simply show no pair.

- **Grouped variants keep their pair.** Inside a multilingual group the pair is how a run picks
  its variant, so nothing in a group changes.
- **Nothing about how runs behave changes.** Same steps, same prompts, same variant picked.
- It runs once, automatically, when the install upgrades (alembic revision `e3b9d5a1c7f4`).
  It cannot be undone by a downgrade, because the cleared values carried no behaviour.

---

## Turning features on and off (Settings → Features)

**What it does.** Localization OS has a lot of surface - images, key projects, evaluations,
memory, vendors, reports, routing, and more. Not every install wants all of it. The
**Settings → Features** page is one place to switch a **non-core** feature off, and
switching it off does not just hide it - it **removes** it. The nav item disappears,
the pages 404, the `/api` routes 404, the CLI group refuses, the MCP tools relay the
404, and the feature's help card 404s too. To anyone poking at the install, a disabled
feature looks exactly like a feature that was never built. Flip it back on and
everything returns, with no data touched either way.

**Core vs. toggleable.** Features that would leave the tool functionless if removed -
documents, projects, translation, TM, glossary, workflows, the editor, budgets/spend
caps, identity - are **core** and are not on this page; they stay always-on. Only the
peripheral/add-on features are toggleable.

**Three kinds of "off".** A toggleable feature behaves in one of three ways:

- **Removed** - the *whole* surface 404s (images, key projects, vendors, reports,
  routing authoring, assist, UTCP). These are default-ON kill switches; there is
  nothing left to see once off.
- **Writes off** - **memory (LLMemory)** and the **Evaluations Lab** keep *answering
  reads*, but every *write* refuses and names the switch. This is deliberate: the one
  place you go to diagnose "why won't this save?" should tell you the switch is off, not
  pretend the data vanished.
- **Opt-in (default-off)** - **LLM Ledger** and **Databricks export** start OFF and are
  turned on by an operator. The flag starts and stops the feature but never 404s a
  surface: their own control pages stay reachable (a prompt-tap page you cannot reach
  could not turn capture back on), so nothing is "removed" when they are off.

Two things that look like they should switch off but don't, on purpose:

- The **Connectors section** itself is core - you disable individual connector *kinds*
  instead (next section).
- **Content routing**: turning `routing_enabled` off removes the rule-*authoring*
  surface, but the runtime routing decision that existing rules already drive keeps
  running, because disabling authoring must never silently break inbound routing.

**The CLI twin.** Everything on the page has a headless equivalent:

```bash
python -m app.cli config features                 # list every toggleable feature + its on/off state
python -m app.cli config set image_localization_enabled 0   # remove Images (Beta) everywhere
python -m app.cli config set image_localization_enabled 1   # bring it back
python -m app.cli config set vendors_enabled 0    # remove the whole vendor surface
python -m app.cli config set reports_enabled 0    # remove Reports
python -m app.cli config set routing_enabled 0    # remove routing rule authoring (routing itself keeps running)
```

`config features` is read-only (it is the CLI mirror of the Features card); the actual
toggle is `config set <flag> 0|1`. The flag names are the switches themselves -
`image_localization_enabled`, `key_projects_enabled`, `evaluations_enabled`,
`llmemory_enabled`, `assist_enabled`, `prompt_tap_enabled`, `utcp_manual_enabled`,
`vendors_enabled`, `reports_enabled`, `routing_enabled`, `databricks_export_enabled`.

---

## Connector visibility: which kinds are available

**What it does.** The **Connectors** section is always on, but *which connector kinds*
appear on it is up to the admin. Two pages:

- **Manage connectors** - lists every kind on this install with an enable/disable
  toggle. Switch one off and it disappears **entirely**: no tile on the Connectors page,
  its per-kind routes 404, `connector create --kind <k>` refuses, and every verb
  touching an existing connection of that kind refuses through one shared guard (the
  scheduler skips those connections; an inbound webhook 404s before it even checks a
  signature). No connection data is lost - re-enable and it all comes back.
- **Catalog ("Get more connectors")** - shows every connector that has been developed -
  active, archived, and planned - with an **enable** button for the ones you have
  switched off.

**The setting behind it** is `connector_disabled_kinds`, a CSV of kind slugs. The pages
are just a friendly face on it; headlessly it is:

```bash
python -m app.cli config set connector_disabled_kinds "jira,shopify"   # hide these two kinds
python -m app.cli config set connector_disabled_kinds ""               # everything visible again
```

There is no dedicated CLI verb for the toggle - `config set connector_disabled_kinds …`
*is* the twin of the Manage/Catalog toggle. The CSV only ever **subtracts**: an
unknown slug in it is ignored, and a brand-new connector kind ships enabled (absent from
the CSV) automatically.

**Don't confuse this with "archived."** Archived is a property of the *code* (a kind
retired from new connections pending a rebuild - Confluence, Zendesk, and the rest);
disabled is a per-*install* choice you make here. They are independent: a kind can be
active-and-enabled, active-but-disabled, archived-but-enabled, or both.

---

## Drive Localization OS from Claude (the MCP server)

**What it does.** There is a fourth way to use Localization OS, alongside the web UI, the
CLI and the raw API: an **MCP server** that lets an AI agent - Claude Desktop, Claude
Code, or your own tooling - operate the product in plain English. *"Translate this
file into German and Japanese."* *"What changed in the string catalogue since last
sync?"* The agent calls tools; the tools call the same `/api` the web UI uses.

**What it can and can't do.** It packages the product as **89 task-shaped tools** across
9 families - orientation, projects, documents, keys + the change feed, workflows,
glossaries and TM, memory, the Evaluations Lab, and reports. Two safety rules are built in:

- **Nothing destructive.** No tool deletes a project, purges memory, signs off a human
  review, or writes into a connected Jira/Airtable/Zendesk. Those verbs simply aren't
  there.
- **Spending is two-phase.** Any tool that costs money (a translation, a workflow run,
  an evaluation) first returns a *plan* - the engine, the scope, the estimated cost -
  and spends nothing on that first call. The agent is expected to show you that plan
  and get your OK before it confirms. Be clear-eyed about what that buys you: the
  `confirm` flag is a **disclosure step, not a lock** - an agent can set it itself, so
  it makes a well-behaved agent state the cost, but the real spending limits are the
  server-side ones (spend caps, department budgets, the mock-engine refusal), and the
  genuinely irreversible actions aren't offered as tools at all.

**Set it up (5 minutes).** Mint a personal API key at **/account** (tick only the
scopes you need - "Documents" for the translate-a-file flow, "Keys" for a CI sync
loop), then add a small block to Claude Desktop's config pointing at your machine's
Python and the server file. The exact config, the Claude Code command, the
remote/HTTP option, and the full tool list are in
`integrations/mcp-server/README.md`.

**Using it from claude.ai.** The **server side** of a claude.ai static-bearer
connector is built and verified: run remote (HTTP) mode behind your own TLS, mint a
scoped `lsu_` key at **/account**, and it reads a fixed `Authorization: Bearer
lsu_...` header. **The claude.ai side is not proven, though** - claude.ai's
request-header auth is a **gated beta** ("contact Anthropic for early access") and
has an **open bug** (`anthropics/claude-ai-mcp#644`) where the header is never sent,
so nobody has connected claude.ai to this server yet. If the connector adds and
shows its tools but the first call fails with *"No Localization OS token on this
request"*, that is the header not reaching the server, not a Localization OS fault. Full
detail and the failure signature are in `integrations/mcp-server/README.md` under
**Connect from claude.ai** (the OAuth-directory connector flow remains a later
phase). For everyday use on your own machine, the default stdio mode is all you
need.

Jev runs deterministic structural checks BEFORE the judge: identical source/target text
(after collapsing whitespace and folding case; same base languages are exempt), inline tags/markup, placeholders (including double braces,
ICU arguments and printf), and number/time parity. Numbers-only text, URLs, codes and
applicable glossary DNT terms are exempt from the untranslated check. Identical single tokens (including acronyms and brands) are advisory MINOR findings,
with or without a glossary, and do not block a QE pass. Identical multi-word text
and other structural failures are MAJOR and block a pass regardless of score or threshold. The judge still runs;
its findings are retained. Cards expose `structural_findings` (dimension, severity, reason)
and `structural_blocked`; the shared rationale names the failure in web and CLI views.
The existing empty-target fast path still skips the judge and fails as empty.
The `qe_jev_enabled=0` kill switch disables judge calls, not structural checks.
A disabled, unavailable or unconfident judge has no score; a structural block
still routes to review and says 'judge did not score'. Lab records structural
blocks as 0 with the reason, preserving the judge score separately; an unscored
card without a block stays not applicable. Judge provenance is judge-wrappers/2.
Number and time parity keeps the number checker's own grade. A real mismatch (a changed
value such as 5 -> 50, a changed time such as 6:45 -> 7:45, an AM/PM swap) is MAJOR and
blocks. Two forms are advisory: a dot-format time that equals the source time (Finnish
klo 18.45, German 18.45 Uhr, Danish and Norwegian dot times) and a number from 0 to 10
written as a word (2 -> deux) in English, French, German, Spanish, Italian, Portuguese,
Dutch, Swedish, Danish, Norwegian, Finnish, Polish, Russian, Chinese or Japanese. They
are listed in `structural_findings` for review but do not block a QE pass. Every other
number finding is MAJOR and blocks, including a dropped or added number, a wrong dot
time, digits regrouped into different numbers (1-10 -> 110) and a rendering that is
ambiguous without a locale convention. Jev also reads what a number means, and each of
these is MAJOR. A clock time dropped from the target blocks (hour words such as cinq
heures et demie, an hour-only form such as 20 heures, and Chinese or Japanese clock
times are read as the time). A date whose day, month or year changed blocks: the order is
judged for the target locale, so 3/14/2026 -> 14.03.2026 is correct and
3/14/2026 -> 15.03.2026 is not;
a dot date is day first, ISO dates and Chinese, Japanese and Korean written dates are
read the same way, a date written out in words passes when its day and year match, and a
bare en source locale with both numbers 12 or below leaves both orders open. A lost minus sign blocks (a
hyphen-minus, U+2212, an en dash or a parenthesised currency amount is negative, and a
word such as minus or moins stands in for the sign). A swapped or dropped currency
blocks ($, USD, US$ and dollars are one currency; $ -> EUR is not; placement and spacing
per locale do not matter). A minus the target adds is not judged. An AM/PM marker
counts only on hours 1 to 12, and a bare lowercase am after a German or Dutch time is
the word for on the, not a meridiem. In scripts written without word spaces (Chinese,
Japanese, Thai, Lao, Khmer, Myanmar) a run is one token, so identical text is advisory,
only while it is at most 8 characters after trailing punctuation (ASCII or full-width) is
removed and holds no sentence punctuation inside; a longer or internally punctuated run is
multi-word text and a copy of it is MAJOR. So the short word with a closing mark and the
same word without it are treated alike. HTML void elements (br, hr, img, input and the
like) are one tag kind whether written <br>, <br/> or <br />.
Time parity accepts 17 h 30 and 5:30 p. m.; it abstains when either side has no
parseable time. ICU nested argument names are sets across branches. Markup parity
compares element names and opening/closing/self-closing kind, not attribute values.
Meaning promotion runs BEFORE the low-confidence skip: a meaning signal at its floor
lifts accuracy none, minor or unsure to major. Confident higher severity is retained.
Auto-fix targets confident major dimensions even alongside unsure dimensions, which are
left unchanged and flagged for review. A better rewrite can be kept for review with the
baseline unsure dimensions still flagged; it cannot pass. New unsure dimensions prevent
keeping an attempt. API and MCP return the same cards and flags as web and CLI.
