---
module: keys
title: Key-based projects and the change feed
summary: A second kind of project for continuous localization - the unit of work is a string KEY rather than an uploaded file, and the deliverable is one key/value file per language, on demand, forever.
nouns:
  Key-based project: "A project whose content is a set of string keys instead of documents. You choose the type when you create the project, and it cannot be changed afterwards."
  Key: "One translatable string identity. Two keys are the same key when their project, namespace, name and context all match - that tuple is the identity everything else hangs off."
  Namespace: "A first-class grouping, usually the file a key came from. Two files can define the same name without colliding, because the namespace is part of the identity rather than a prefix on the name."
  Context: "A short disambiguator for two keys that genuinely share a name. Unlike some tools, it is editable after the fact."
  Base value: "The source-language text of a key. It is what gets translated, and changing it is what triggers auto-unverify."
  Plural key: "A key whose value is a set of CLDR forms rather than one string. Each language gets exactly the forms it needs - English two, Russian three, Arabic six - and each form is edited and checked on its own."
  Auto-unverify: "The default rule that a changed base value flags every language that already had a translation for review, so a source edit can never silently ship a stale string."
  Archived key: "A key retired from the product. It stays for the record but is left out of exports by default, and a cleanup import never deletes it."
  Tombstone: "The change-feed entry that says a key was deleted. Deletion is an event you receive, not an absence you have to notice."
  Change feed: "The per-project log of everything that happened to keys and values, ordered by a cursor. This is what the Delta API serves."
  Cursor: "Your position in the change feed. You keep the last one you saw and ask for everything after it."
  Build file: "A per-namespace export with bare key names - the file your application actually loads."
  Transport file: "A whole-project export with every name qualified as namespace::name plus a marker saying so. Meant for round-tripping back into the tool, not for shipping."
verbs:
  Create a key project: "Web: New project -> Project type -> Keys. CLI: `project create \"My app\" --targets de-DE --customer-id 3 --type key_based`. Either way it needs at least one target language, because values are stored per language."
  See the keys: "Web: the key grid at Keys on the project page. CLI: `keys list --project 7`."
  Add a key: "Web: Add a key on the grid. CLI: `keys add --project 7 --namespace common --name error::generic --base \"Something went wrong.\"`. Leave --namespace off and the key lands in the default (empty) namespace, which is a different key."
  Edit a translation: "Web: on the key grid each key is a card, and each language is its own labelled block inside it - type in that language's box and press Save. A plural key gets one box per CLDR form. CLI: `keys set --project 7 --namespace common --name error::generic --locale de-DE --text \"...\"`."
  Change the source text: "Web: the base block is the first one on the card, marked with the project's source locale. CLI: the same `keys set --project 7 --namespace common --name error::generic --locale en-US --text \"...\"` with the project's SOURCE locale instead of a target one. Either way, every language that already had a translation is flagged for review."
  Load a file of keys: "Web: Import on the grid. CLI: `keys import --project 7 --file ./en/common.json`. JSON (flat or nested), gettext .po, and every resource format with a key view: Java .properties, Apple .strings, INI, RESJSON and YAML today, plus each resource filter that adds one (Android, RESX, stringsdict, ARB, PHP arrays, TOML ...) automatically. A resource file's keys keep their comments as descriptions and their plural forms as plural keys; two entries with the same key, or a key another format already defined in the namespace, are refused, never merged. The namespace defaults to the FILE STEM, so that file fills namespace `common` - which is why every example above names it."
  Get the files back: "Web: Export on the grid. CLI: `keys export --project 7 --locale de-DE --namespace common -o de/common.json`."
  Translate everything: "Web: the Translate control on the grid picks a workflow and a language - or all languages at once - and runs it exactly as it would over a document. Launching a run is available on all four surfaces: web, the CLI (`keys translate --project 7 --workflow 9 --locale de-DE`, or `--locale all`), the API (`POST /api/projects/{pid}/keys/translate`), and MCP (`translate_keys`). Use `keys list --project 7 --untranslated-in de-DE` to see what is left. A project that holds a key hidden from translators can be launched by a manager or admin only."
  Ask what changed: "CLI: `keys changes --project 7 --cursor-file`. It prints everything after your stored cursor - creations, edits and deletions - and writes the new cursor back."
  Retire a string: "Archive it to keep it out of new builds - `keys edit --project 7 --namespace common --name error::generic --archive` - or delete it to send consumers a tombstone. Deleting keeps the translations: `keys list --project 7 --include-deleted` finds it and `keys restore --project 7 --namespace common --name error::generic` brings it back (the grid has the same two controls)."
needs:
  - "The key_projects_enabled switch on. With it off the project type is not offered, the key pages return not-found, and the CLI refuses."
  - "A project created as key-based. The type is fixed at creation - an existing document project cannot be converted, and vice versa."
  - "At least one target language before keys can hold values at all - a base value is stored on the per-language rows."
  - "For the API and for `--remote`: a personal API key carrying the keys scope. It is separate from the documents scope on purpose."
  - "For translation: a workflow that can produce the run's target, exactly as for a document (a grouped workflow picks its per-language variant by the run's languages)."
limits:
  - "A key project never shows a document list, and its internal storage documents are hidden from every document surface. The project delivery bundle is a document feature; a key project's deliverable is the per-language key file."
  - "Export shape follows the request, never the data: naming a namespace always gives bare names, omitting it always gives qualified names plus a marker. This is deliberate - a shape that flipped when someone added a key in a second namespace would silently rewrite every lookup key in your product."
  - "Re-importing a whole-project export needs the marker, so pointing that mode at an ordinary file is refused rather than quietly filing every key under an invented namespace."
  - "Cleanup mode only runs on a source-language import. A translation file is a partial view of the key set by nature, so letting one drive deletions would remove keys, not values."
  - "A namespace may not contain :: and may not be called __default__ - both would make a qualified name ambiguous."
  - "Some gettext languages have fewer plural categories than the .po format has slots. Where a translation provably cannot survive the round trip, the import report says so rather than dropping it quietly."
  - "The change feed keeps 90 days by default. A cursor older than that is answered with a resync signal instead of a confident, wrong 'nothing changed'."
  - "The feed assumes one writer per project at a time. Concurrent writers on Postgres should re-read a small overlap window; entries are safe to replay."
  - "Deleting the whole project is 'gone, re-bootstrap' for a consumer, not a stream of tombstones."
connects:
  - module: documents
    how: "Key values are ordinary segments under the hood, so everything the document pipeline does - segment status, QA flags, the editor, the export tag guard - applies to a key value for free."
  - module: workflows
    how: "Translating a key project runs a normal workflow over its keys, with the same run view, the same steps and the same spend safety as a document run."
  - module: tm
    how: "Key values read from and write to translation memory exactly as document segments do, so a repeated string is leveraged across both project types."
  - module: glossary
    how: "Glossary and do-not-translate checks fire on key values and surface as QA flags on the value."
---

## What this is for

Documents are a job: a file arrives, gets translated, gets delivered. Keys are a
**relationship**: your product has strings, they change every week, and every release
needs the current set in every language. This project type is built for the second
shape.

The important consequence is the **change feed**. Instead of asking "what does the
whole catalogue look like now?" and diffing it yourself, you keep a cursor and ask
"what happened since?". Creations, edits and **deletions** all come back as entries.
Most tools in this space cannot tell you a key was deleted at all - you find out
because it stopped appearing, which means you cannot tell deletion from a filter
mistake.

## The normal path

1. **Create** the project as key-based, with your target languages.
2. **Import** your source file. The namespace defaults to the file name, so
   `common.json` and `admin.json` can both define `title` without fighting.
3. **Translate** with a workflow, or fill values by hand in the grid.
4. **Export** one file per language per namespace - those are your build files.
5. On the next release, **import the new source file** and repeat. Add `--cleanup`
   when you want strings you removed from the source to be retired too.
6. In CI, poll `keys changes --project 7 --cursor-file` and act only on what moved.

When listing through the keys API, omit `namespace` for all namespaces; explicit
`?namespace=` selects only the root namespace. In the CLI, `keys list --project 7 --namespace ""`
selects the root namespace; omitting `--namespace` lists all namespaces.

## Bundling the in-context images with an export

An export can carry each key's **in-context images** alongside the key file. Turn it on
with the `include string images` option (the grid checkbox, `?include_string_images=1`
on the download route, or `--include-string-images` on the CLI) and the download becomes a
**ZIP bundle** instead of a bare key file: the key file itself, an `images/` subtree holding
each exported key's ready images, and a `manifest.json` that links every image back to its
key. Only the keys that actually ship in this export contribute images, and only images that
belong to this project: an image attached to another customer's key can never ride out in
your bundle. It is off by default, and a plain file is byte-for-byte what it was before. The
CLI needs an output path (`-o`) because a ZIP is binary. A bundle whose images run past the
per-export caps (`key_image_export_max_files` / `key_image_export_max_bytes`) is refused
whole rather than truncated, so you narrow the export (a namespace, a status filter) or
raise the caps. That `/api` refusal carries a stable `code: "key_image_export_max"` beside
its human message (and so does the in-process CLI's `--json` error), so the `--remote` CLI
re-classes it to the same exit code the local arm uses off the code, not the message text.
There is no MCP surface for this: the whole `include_string_images` export is deliberately
absent from the MCP tool set (an operator download, never an agent capability), so the
error `code` has no MCP form either. The bundled image bytes are the plaintext screenshots, so treat the ZIP
as you would any operator download of customer content; an export that carries images is
audit-logged with the image ids that left.

This is an **operator download only**: it is available on the web, the `/api` download route
and the CLI, but **not through the `export_keys` MCP tool**. An agent has no in-context-image
export use case, and letting one pull a whole project's screenshots to disk in a single call
would be an exfiltration surface, so the option is simply absent from the tool.

Note this is the **keys** export. The separate install-wide compliance data export (the full
dump) does **not** include in-context images: `KeyImage` rows and their pixels are excluded
there, and turning this keys-export option on does not change that.

## The two export shapes, and why it matters

A **build file** is scoped to a namespace and has bare key names - it is what your
application loads. A **transport file** covers the whole project, qualifies every
name as `namespace::name`, and carries a marker saying so - it is what you feed back
into the tool.

Which one you get is decided by whether you named a namespace, and by nothing else.
An earlier design decided it from the data - qualify if the export happened to span
more than one namespace - and that meant adding a single key in a second namespace
rewrote every name in the next export. For a `.po` file that is every `msgid`, which
is every runtime lookup in the product, changed silently.

The same split decides what happens to a value whose inline placeholder tags are
damaged. A build file **never ships one**: its source text goes out instead, and you
are told which keys - a warning on the command line, `substituted_keys` under `--json`,
and a notice on the grid after a browser download. A transport file carries **what is
actually stored**, damaged tags and all, because its whole job is to be imported back:
substituting there wrote English over the real translation on the way in and cleared
its needs-review flag. Exporting the whole project and importing it straight back now
changes nothing at all - no value, no status - in JSON and in `.po` alike, for every
key that has no **context**. Keys that DO carry a context round-trip cleanly through
`.po` only: JSON has nowhere to put a context, so such a key comes back as a new,
context-less key of the same name. That is a known bug, not a design choice - avoid
context on a project you round-trip as JSON until it is fixed. (`.po` was the exception
until recently for a different reason: gettext can only mark a whole plural entry as
"unreviewed", never one form of it, and re-importing that used to knock every other
form of the key back to needs-review.)

That gettext limitation has one consequence worth knowing if you review in a `.po`
editor. Adding `#, fuzzy` by hand to an entry you did **not** otherwise change asks for
a re-review, and on an ordinary key that is exactly what happens - it comes back
needs-review. On a **plural** key the flag names no particular form, so it is dropped
rather than applied to all four: flagging `one`, `few`, `many` and `other` together
would undo real sign-offs on forms nobody questioned. To send one plural form back for
review, change that form's text, or do it in the product.

An import is a machine write, so it **never overwrites a value a human has approved or
edited** - the same human-work protection that stops a re-translation from clobbering a
sign-off. A `.po` round trip that changes an approved form's text, or a hand-added
`#, fuzzy` on an approved entry, is **refused, not applied**: the sign-off stands
byte-for-byte and the import report names the key it kept. This holds per plural form (an
edit to the `mt` form lands as usual while the approved form beside it is kept). A value
write through the API (`PUT /api/keys/{id}/values/{locale}`) over an approved/edited value is
answered **409** with a named `status_protected` reason rather than a silent success. If you
genuinely want the machine value back, clear or edit the value in the grid or CAT editor
first - clearing it resets the status so the next import can fill it.

Importing source text into a target language is treated as what it is: not a
translation, and never a write. It does not overwrite a translation that is already
there, and it does not fill an empty slot either - an untranslated string stays
untranslated and stays in the "untranslated in" list, which is the thing that gets it
translated at all. A stray trailing newline, a stray space, or the same word spelled
with combining accents instead of composed ones does not sneak past this. **Different
capitalisation does not count as the source text** - `Email` for `email` is a real
translation and is written; treating case as noise made a de-DE to de-CH project
unable to import its own target locale at all. Either way the import names the keys
it skipped: on the command line and in the `--json` report as a sentence per key, and
on the grid after a browser upload as a list of key names - with an explicit `+N more`
when there are too many names to fit.

Exporting with **empty-as base** fills untranslated slots with the source text, which
is what it is for. If a value was deliberately WITHHELD - flagged for review with
nothing in it - the export says so and marks it as a suggestion rather than shipping
English as a finished translation.

## In-context images (for vision engines)

A string key can carry up to five in-context images: attach a screenshot of the UI the
string appears in, and a vision-capable engine sees it as context when it translates that
string. This is a translation aid, not OCR: the picture rides the model call beside the
source text, it is never transcribed, and it never enters the text prompt or any log.

Image context only works when the project's translation engine can see images (a
vision-capable model). Vision-capable engines include a local vision model via LM Studio
(configured with a vision model on the chat path) and the cloud models Claude, OpenAI and
Gemini. With a text-only engine (for example DeepL or an NMT engine, or a local
ChatML-adapter model like qwen3.5) the image is stored for reviewers and skipped at
translation time, and the run records that it was skipped.

The first image on a key is its primary, the one sent by default. Manage images from the
Strings grid, the CLI (`keys image add`, `keys image list`, `keys image rm`,
`keys image set-primary`), the `/api/keys/{key_id}/images` routes, or the MCP tools
`manage_key_image` (attach via a base64 param, delete, set-primary) and `list_key_images`
(metadata only). Raster PNG/JPEG only; each image is downscaled to a send-ready PNG at
upload. Switch the whole feature off with `key_image_context_enabled=0`.

## Current warts worth knowing

- Changing a key's base value flags all its translations for review. That is the
  point, but on a large import of edited source text it can flag a lot at once. The
  per-project auto-unverify toggle is on the grid if you want it off.
- Plural forms materialize per language from CLDR, not from the file you imported.
  A Spanish key imported with `one` and `other` will export `one`, `many` and
  `other`, because those are the forms Spanish has.
- Demoting a plural key back to a single value refuses while another form still
  holds a translation. Tick the discard box if you really mean it.
- A base value given when you ADD a plural key fills the `other` form only, and so does
  editing one form's base value later. The other CLDR forms need their own source text
  - a singular sentence is the wrong source for a "few" or "many" form, and machine
  translation would happily translate it into every slot. Both the grid and the CLI say
  which forms are still empty.
- A `.po` export marks a plural key as unreviewed if ANY of its forms is, because that
  is the only thing the format can say. Nothing is lost - importing it back changes no
  status - but a human reading the file sees the whole entry flagged.
- "Quarantine" as the tag-damage policy applies to build files. A transport file still
  carries the damaged value, because dropping it there would delete the translation on
  the way back in. It is counted and named either way.
