# Terminology backend (concepts, TBX, extraction): Architecture (L1)

## 1. Role

This subsystem is the **terminology depth layer beneath the compatibility termbase**: the
concept/term rich store, the TBX interchange format and its strict conformance evaluator, and the
document term-extraction pipeline. The everyday glossary CRUD, the `/glossaries` surface, and the
compat `GlossaryEntry`/`GlossarySense` rows a translation prompt reads are documented in
tm-and-glossary; this doc owns the model UNDER those rows and the interchange around them.

The concept store (`app/services/glossary_concepts.py`) holds `GlossaryConcept` and
`GlossaryTerm` rows and is, after the T1b authority inversion, the **single write authority**:
every write on every surface is a concept-store operation, and the compat entry/sense rows are a
pure projection of the per-pair bindings. The projector is the only writer of the derived compat
columns and never deletes; a backfill writes only NULLs on upgrade.

TBX (TermBase eXchange) is how the industry hands terminology around. The format layer
(`app/services/glossary_tbx.py`) turns TBX bytes into the same normalized row dicts a glossary CSV
yields and turns stored entries back into TBX, tolerating both the martif/2.0 and the 3.0/DCA
dialects; the single importer stays `glossary._apply_rows`, not a forked path. Strict TBX
validation runs the TBX-Basic (DCA) schemas, fetched once with `scripts/fetch_tbx_schemas.py` (sha256-pinned), through the in-repo evaluator
(`app/services/tbx_conformance.py`): the TBX core RELAX NG plus the DCA Schematron level
constraints and picklists, never fetched at validation time; when the schema files are absent strict validation refuses and names the download URLs. A hostile TBX (entity declaration or oversize) is refused with a `TbxError`
before lxml sees it, on both the strict and tolerant arms. The only concept-preserving round trip
today is the `glossary-concept/v1` JSON (`.glossary.json`) import through
`app/services/glossary_concepts_part5.py::from_v1`; TBX import takes the standard entry path.
The shared streaming TBX parser imports house `los:dnt` (true/1/yes, case-insensitive)
and legacy `doNotTranslate` notes; any recognized truthy marker at any source metadata
level sets DNT, and no marker ever clears it. Other fields retain narrower-source precedence.

Term extraction (`app/services/term_extraction.py`) is the upstream half: extract candidate
terminology from a document, propose translations, and land candidates PENDING in a per-glossary
review grid where a reviewer accepts each into a real entry or drops it (singly or in a batch), with
no silent auto-add. Re-extraction never re-proposes a term already in the glossary or already
decided, a spend-blocked run writes no candidates, a no-provider-call estimate is available for the
launch form, and a candidate's context snippet is redacted for a viewer who cannot see its source
document.

It does not own the compat termbase CRUD or the glossary matching at translation time
(tm-and-glossary), nor the LLM extractor call itself (it reuses `glossary.build_from_text`).

## 2. Invariants

- **The concept store is the single authority; compat rows are a projection (LAW-TERM-18).**
  `app/services/glossary_concepts_part4.py::project` is the only writer of the binding-owned compat
  columns and `app/services/glossary_concepts_part4.py::reconcile_glossary_from_entries` never deletes on
  the projection arm. Enforced by `verify_glossary_authority` and `verify_glossary_write_parity`.
- **Backfill writes only NULLs, on upgrade and boot.**
  `app/services/glossary_concepts_part4.py::backfill_glossary` derives concepts for entries that lack
  them without overwriting existing data. Enforced by `verify_glossary_backfill`.
- **TBX import reuses the one glossary importer, and both dialects normalize to one shape.**
  `app/services/glossary_tbx.py::parse_rows` yields the same row dicts the CSV path yields, so
  `glossary.import_bytes` stays the single write path. Enforced by `verify_glossary_tbx`.
- **strict=true TBX validation runs the pinned schemas, never a network fetch at validation time.**
  `app/services/tbx_conformance.py::validate` runs the core RELAX NG
  (`app/services/tbx_conformance.py::relaxng_findings`) and the DCA Schematron
  (`app/services/tbx_conformance.py::evaluate`) over files in `app/resources/tbx/` (not vendored: `scripts/fetch_tbx_schemas.py` downloads them, sha256-pinned; without them strict validation refuses with the download URLs). Enforced by
  `verify_tbx_basic_conformance`.
- **Extraction never auto-adds a term; a reviewer decides.**
  `app/services/term_extraction.py::extract` lands candidates PENDING and
  `app/services/term_extraction.py::accept` / `app/services/term_extraction.py::drop` are the only
  ways a candidate becomes or leaves a real entry. Enforced by `verify_term_extraction`.
- **The concept editor is off by default.**
  `app/services/glossary_concepts_common.py::concept_editor_enabled` reads
  `setting:terminology_concept_editor_enabled` (default off). Enforced by
  `verify_glossary_concept_flag`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Concept/term store | `app/services/glossary_concepts.py` · `app/services/glossary_concepts_part4.py::project` · `app/services/glossary_concepts_part2.py::sync_entry_concept` · `app/services/glossary_concepts_common.py::SCHEMA_V1` · `app/services/glossary_concepts_part3.py::_sync_glossary_from_entries` · `app/services/glossary_concepts_part5.py::from_v1` | The authoritative store, the projector to compat rows, and the `glossary-concept/v1` codec. |
| Concept backfill / reconcile | `app/services/glossary_concepts_part4.py::backfill_glossary` · `app/services/glossary_concepts_part4.py::reconcile_glossary_from_entries` | The upgrade/boot backfill (NULLs only) and the content-identity reconcile. |
| TBX interchange | `app/services/glossary_tbx.py::parse_rows` · `app/services/glossary_tbx.py::TBX_MEDIA_TYPE` | TBX bytes to normalized rows and stored entries to TBX, both dialects. |
| Termbase dialects (MultiTerm XML, TBX 2002) | `app/services/glossary_tbx.py::mtf_to_entry` · `app/services/glossary_tbx.py::status_of` · `app/services/glossary_tbx.py::record_status_terms` · `app/services/glossary_tbx.py::EntryReportTracker` | MultiTerm conceptGrp converted to a TBX v2 entry and read by the same row builder; usage status keeps forbidden/deprecated terms out of targets; per-entry import report. |
| TBX conformance | `app/services/tbx_conformance.py::validate` · `app/services/tbx_conformance.py::permitted_termnote_types` | The strict core RNG + DCA Schematron evaluator and the picklists. |
| Term extraction | `app/services/term_extraction.py::extract` · `app/services/term_extraction.py::estimate` · `app/services/term_extraction.py::accept` · `app/services/term_extraction.py::drop` · `app/services/term_extraction.py::batch` | Extract candidates, then a reviewer accepts or drops each into the glossary. |

## 4. Data & settings

- **Models:** `app/models.py::GlossaryConcept` and `app/models.py::GlossaryTerm` (the authoritative
  store), projected onto `app/models.py::GlossaryEntry` and `app/models.py::GlossarySense` (the
  compat termbase rows, whose CRUD is tm-and-glossary).
- **Settings:** `setting:terminology_concept_editor_enabled` (default off) gates the concept
  editor. TBX validation reads no setting: the schemas live under `app/resources/tbx/` once fetched with `scripts/fetch_tbx_schemas.py`.

## 5. Enforced by

- `verify_glossary_concepts`, `verify_glossary_authority`, `verify_glossary_write_parity`: the
  concept-store authority and the write/projection parity.
- `verify_glossary_backfill`: the NULL-only upgrade/boot backfill.
- `verify_glossary_tbx`: the TBX round-trip through the one importer, both dialects.
- `verify_tbx_basic_conformance`: the core RNG + DCA Schematron strict validation.
- `verify_term_extraction`: extract, the PENDING review grid, accept and drop.

## 6. Battle scars

- **The authority was inverted so compat rows can never diverge from concepts.** T1b made the
  concept store authoritative and the compat columns a projection, proven by a write-parity golden
  and a cross-tree fuzz.
- **Concept-preserving TBX import was cut and stays reserved.** T2 shipped DCA export, conformance
  and the entry-path importer only; the TBX concept parser stays visible but refuses before parse
  rather than regress a green tree.

## 7. Limits & accepted risks

- **Do-not-use terms are kept, not enforced as targets.** A forbidden/deprecated term from a MultiTerm or TBX import is, for a module glossary, stored as a forbidden/deprecated GlossaryTerm on the concept found by its first usable source term (skipped when a binding still enforces that surface, so a live binding never points at a forbidden term; this path does not re-project). A per-project glossary has no concept store, so the generated do-not-use line in the entry notes is all it keeps, and it never replaces a stored note the file does not replace (`app/services/glossary_tbx.py::merge_dont_use`). Status words are a heuristic over free-text MultiTerm picklists (`app/services/glossary_tbx.py::status_of`), negation-aware. MultiTerm transacGrp (author/date) and cross-references have no glossary column and are dropped, stated in the `app/services/glossary_tbx.py::mtf_to_entry` comment block.

- **TBX concept-preserving import is reserved, not shipped.** `--concepts` / `concepts=true`
  refuses before any parse or write on every surface and points at the standard import, stated in
  the `app/services/glossary_hub.py::import_bytes` refusal (the `concepts=True` RESERVED block).
- **Extraction recall depends on the configured engine.** The recall stage reuses the spend-capped
  `glossary.build_from_text` extractor, so a weaker engine proposes fewer candidates, stated in the
  `app/services/term_extraction.py` module docstring.
