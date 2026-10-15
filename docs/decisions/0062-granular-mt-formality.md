# 0062. MT formality is granular: customer, project and document, per target language

Status: Accepted
Date: 2026-10-08

## Context

Pressure test T8 (#610, #617 item 7): German output used "Sie" where the customer wanted "du", and there was no way to say so
except by hand-editing every style guide. The first recommendation was one formality field per project or customer
(formal, informal, auto). A refinement on 10-08: one customer has some projects and documents that are formal (Sie)
and others informal (du), and some languages have more than two levels (Japanese, Korean), so the setting must be
granular and language-aware.

## Decision

1. Resolution order, most specific first, for one target language:
   document override, then the project setting for that target language, then the customer default for that target
   language, then none. "None" means no instruction at all: today's behavior, byte for byte.
2. Storage: a nullable JSON map `{target_locale: register}` on `customer.formality`, `project.formality` and
   `document.formality` (migration `b8e2f4a6c1d3`, nullable, no backfill). A lookup tries the exact locale (`de-AT`),
   then its language (`de`). A customer default is normally keyed by language; a project or document by the project's
   target locale.
3. Values: the generic scale `formal`, `informal`, `auto`, valid for every language, plus language-specific registers
   from ONE catalog (`app/services/formality.py`): de `sie`/`du`, fr `vous`/`tu`, es `usted`/`tu`, it `lei`/`tu`,
   nl `u`/`je`, pt `senhor`/`voce` (pt-PT adds `tu`), ru `vy`/`ty`, pl `pan`/`ty`, tr `siz`/`sen`, sv `ni`/`du`,
   hi `aap`/`tum`, ja `casual`/`polite`/`honorific`, ko `deferential`/`polite`/`casual`. An unknown register for that
   language is refused with a 400 on every surface (the same function validates web, API, CLI, MCP).
4. `auto` set at a level is an explicit "no instruction here": it stops the chain, so a project can opt out of a
   customer default. Clearing a level (`register` empty or null) removes the entry and lets the next level apply.
   A generic `formal`/`informal` on a language with specific registers resolves to that language's formal/informal
   register for the prompt (de formal = Sie; ja formal = polite, ja informal = casual; ko formal = deferential,
   ko informal = casual); the stored value stays what the user chose.
5. The resolved register reaches the model through the style-guide block: `style_guides.render_style_guide(guide,
   formality)` appends one "Formality" sentence inside the `<style_guide>` fence (or emits a block with
   `source="formality"` when no guide resolves). Every path that renders the style guide therefore carries it: plain
   translate, workflows (full-text and segmented, including the LQA judge's adherence check), Advanced (style.resolve
   digest), translate/text (customer default or an explicit `formality` field), style A/B, vendor rework. The register
   is authoritative: its sentence says it overrides any other formality hint (the context card's `formality` field
   stays a free hint). With nothing resolved, or `auto`, no byte is added.
6. Explainability: the resolved register is recorded on the run (`workflowrun.formality`: register, source level, target
   locale), in the plain-translate summary event, in the prompt-tap manifest, and on the run/API payloads.
7. LQA and Jev see the same sentence in the style-guide block they already receive, and it says the register was chosen
   on purpose, so the chosen register is not reported as an error.
8. Who may change it (10-08): a CUSTOMER default needs an admin or manager (`can_manage`); a project setting
   or document override needs the customer-configuration role (admin, manager or linguist; a linguist owns language
   tuning per project). One predicate, `formality.can_set`, serves the web form, the panel (a linguist sees no
   customer-level edit controls) and `/api/formality` (so the remote CLI and MCP). The org / customer / project ring is
   checked first (a foreign id answers like a missing one). Reads are open to anyone who can open the object. Every
   write is audited (`formality.set`, `formality.clear`).

## Consequences

- Existing installs change nothing until someone sets a value.
- Prompts for a formality-bearing run grow by one sentence (about 200 characters); a style guide's 2,000-character cap
  is applied to the guide body only, so the sentence is never truncated away.
- Adding a language is one catalog entry. The wording for each register is code, not data, so it is reviewed like any
  other prompt text and listed in the floor texts.
- Tags and per-segment overrides are out of scope; a document override is per target language, not per segment.
