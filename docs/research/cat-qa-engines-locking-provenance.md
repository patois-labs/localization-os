# CAT research: QA-check engines + locked-segment/provenance handoff semantics (research notes, 2026-08-08)

> For the Vendor Platform (test-only CAT tool) build plan. Primary-source based; note
> the flagged low-confidence tail on XLIFF 1.2 state-qualifier values.

## TOPIC A - QA engines

### Check catalogs by tool (condensed; full detail in agent transcript)
- **memoQ** (9 tabs): Segments+Terms (term consistency both ways, forbidden terms, length proportionality, target==source, empty target, formatting, NT consistency) · Consistency (repeated words, same-source→same-target enforcement, TM-match variance, unedited-best-match drift) · Numbers (locale-aware, alphanumeric codes/URLs, units) · Punctuation (language-specific, bracket/quote pairing, NBSP rules) · Spaces/Capitals/Characters (double/trailing space, initial caps, forbidden chars, spellcheck delegation) · Inline Tags (well-formedness, missing/extra/reordered) · Length (char + pixel) · Regex (7 custom types w/ capture-group autocorrect) · Severity (per-check warning-vs-error reclassification).
- **Trados QA Checker 3.0** (9 nodes): Segments Verification (untranslated, target==source) · Segments to Exclude · Inconsistencies (same-source-diff-target, repeated words) · Punctuation · Numbers (dates/times/currency/measurements) · Word List (blacklist pairs) · Regular Expressions · Trademark Check (™©®) · Length Verification; QA Checker Profiles (named shareable configs).
- **Xbench**: untranslated, target==source, tag mismatch, numeric mismatch, URL/email/alphanumeric mismatch, unpaired symbols/quotes, double blanks; inconsistency in source AND target; UPPERCASE/CamelCase mismatch; spellcheck; Key Terms (glossary deviation); custom checklists (.xbckl). Scoping: Only New / Only 100%+ / **Exclude Locked / Exclude ICE** toggles.
- **Verifika**: 55 error types, 70+ languages; in-place correction; **profiles that separate delivery-blocking from review-only issues** (first-class severity/gating).
- **Phrase TMS**: Linguistic (empty target, missing/extra numbers, inconsistent translations, leading/trailing/multiple spaces, max length, repeated words, spelling, target==source, trailing punctuation, **unedited TM fuzzy match, unedited NT/MT match**) · Terminology (forbidden, missing, disallowed chars) · Formatting+Tags (empty/inconsistent tag content, nested tags, XLIFF structure) · Workflow (newer version upstream, unconfirmed segments, unresolved comments) · Custom regex. All warnings unless PM marks mandatory; **mandatory warnings must be resolved or explicitly dismissed before Complete**; PM controls which warnings linguists may self-dismiss; separate LLM "AI checks" tab.
- **Weblate** (open-source rule engine - most reusable taxonomy): ~25 format-string checks (per templating syntax + generic Placeholders) · markup (XML/BBCode/Markdown/HTML/Fluent) · punctuation/whitespace symmetry (mismatched full stop/ellipsis/question/colon/etc., starting/trailing space/newline, double space, punctuation spacing) · consistency (Inconsistent, Reused Translation, plurals) · Does Not Follow Glossary · sanity (consecutive duplicate words, unchanged translation, accelerator keys, multiple capitals, zero-width space, max length/lines/size, URL, regex) · source-side checks · automatic fixups. **"Enforced" flag per check = unignorable, failing strings force-marked Needs editing.**

### THE CANONICAL QA TAXONOMY (agent's synthesis - ~16 recurring checks)
**Structural/deterministic (pure code, no NLP)**: 1 empty target · 2 target==source · 3 tag/placeholder mismatch · 4 numeric mismatch · 5 length overflow · 6 double/leading/trailing spaces · 7 repeated adjacent word · 8 unpaired brackets/quotes · 12 same-source→different-target · 13 different-source→same-target · regex/custom · 15 glossary non-adherence + 16 forbidden terms (list matching) · 14 unedited-fuzzy/MT-left-as-is.
**Language-intelligence-dependent (the LLM-advisor slot)**: 9 spelling/grammar · 10 capitalization · 11 language-specific punctuation rules · "does it read naturally." Every tool either skips these, bolts on a spellchecker, or (Phrase AI checks) hands them to an LLM. → Minimal tool: implement the deterministic ~12 in code; one advisory LLM step for linguistic judgment, clearly non-blocking.
**Workflow patterns**: warning/error binary everywhere; per-finding dismiss (with PM-controlled dismissability); the Phrase gate - QA clean-or-explicitly-dismissed before delivery - is the pattern to replicate; keep ≥1 unignorable check.

## TOPIC B - Locked segments + provenance on handoff

### Lock encoding by format (NOT interoperable out of the box)
| Format | Mechanism |
|---|---|
| XLIFF 1.2 | `translate="no"` on trans-unit (generic, default yes) |
| MQXLIFF | `translate="no"` + `mq:locked="locked"` extension |
| SDLXLIFF | `locked` on `<sdl:seg>` inside seg-defs (not on trans-unit); Ctrl+L only in native SDLXLIFF |
| TXLF | `gs4tr:locked="true"` - not recognized by other tools' filters (documented interop failure) |
Cross-tool lock round-tripping is a documented pain point (memoQ↔XTM, Xbench↔Studio issues).

### Receiving-editor behavior
Universal pattern: **visible-but-read-only** (grey background + lock icon), never hidden, never stripped from the file. Locked rows don't require confirmation for job completion; QA tools offer exclude-locked scoping toggles.

### Pricing
Match-grid convention: new = full rate; fuzzy = sliding discount; 100%/reps ≈ 30% (sometimes $0). **Locked ≠ 100%/ICE** - locked is a workflow restriction (often applied TO 100%/ICE but also to boilerplate/frozen content); tools provide a statistics toggle to exclude locked wordcount from pricing WITHOUT touching the file.

### Provenance encoding
- **XLIFF 1.2**: `<target state=...>` (new/needs-translation/.../translated/signed-off/final) + `state-qualifier` (**verified core: `exact-match`, `fuzzy-match`, `mt-suggestion`, `id-match`**; the `leveraged-*`/`rejected-*` tail was NOT re-verifiable against the truncating OASIS page this session - spot-check xliff-core.html#state-qualifier before hard-coding) + sibling `<alt-trans origin="..." match-quality="90%">` for candidates.
- **XLIFF 2.x**: 4-value `state` (initial/translated/reviewed/final) + `subState`; provenance moved to the Translation Candidates Module (`<mtc:match origin matchQuality type="mt">`).

### RECOMMENDATION (agent's analysis - adopted into the Vendor Platform plan)
Own a flat tool-native schema; treat XLIFF as a lossy boundary:
```
{ provenance: "tm_exact"|"tm_fuzzy"|"mt"|"new",
  match_score: number|null,   // tm_fuzzy only
  locked: bool,
  locked_reason: string|null }
```
Maps cleanly onto XLIFF 1.2 semantics for a later exporter without schema change.
Editor behavior: locked → visible/read-only/grey+icon; tm_exact → "100% TM" badge, editable-but-flag-if-edited; tm_fuzzy → badge with score, QA-flag if left unedited; mt → visually distinct "MT - post-edit required" badge; new → plain.
**Send locked segments, always, visible-but-locked**: context value dominates payload cost; stripping breaks segment continuity and consistency checks; pricing exclusion is an orthogonal statistics-level control (the pattern the whole industry converged on). Acceptable middle ground for payload-sensitive scenarios: keep source + locked marker, drop target - never omit the row.

## Sources
docs.memoq.com (edit-qa-settings, resconsole-qasettings, run-qa, lock-segments, bilingual-export-wizard, pre-translate-and-statistics) · docs.rws.com (qa-checker settings-359765, regex-345947, word-lists-571383) · producthelp.sdl.com QA_Checker · docs.xbench.net (work-qa-features, qa dialog) · e-verifika.com · support.phrase.com QA-TMS · docs.weblate.org checks · docs.oasis-open.org/xliff/v1.2/os/xliff-core.html · W3C ITS XLIFF_2 mapping · docs.oasis-open.org xliff-core-v2.0 · RWS community threads (translate=no, TXLF file type) · groups.google.com okapi-users · proz.com locked-wordcount thread · wordfast.com TXLF editor
