# CAT research: memoQ + Trados Studio editor fundamentals (research notes, 2026-08-08)

> For the Vendor Platform (test-only CAT tool) build plan. Primary-source based.

## memoQ

### Editor grid
The Grid: source column, target column, Status column, row-per-segment.
Segment statuses (color-coded): **Not started** (gray) / **Edited** (brown) / **Pre-translated** (blue) / **Assembled from fragments** (pink) / **Translator Confirmed** (green) / **Reviewer 1 confirmed** / **Reviewer 2 confirmed**. Ladder: Not started → Edited/Pre-translated → Translator Confirmed → R1 → R2; backward moves only via explicit status change (limited set).
Confirm: `Ctrl+Enter` - writes source+target to primary TM and sets Confirmed. `Ctrl+Shift+Enter` = confirm WITHOUT TM update. Copy source→target: `Ctrl+Shift+S`. Split: `Ctrl+T`; join available in grid. Bulk "Copy source to target where empty" exists.

### Match provenance (Translation results pane)
Candidates color-coded by origin: TM/LiveDocs **red** (with match %: 101 context / 102 double-context / 100 exact / fuzzy bands 95-99 High, 85-94 Med-1, 75-84 Med-2, 50-74 Low); termbase **blue**; forbidden terms **black** (warn-only); fragment assembly **purple**; automated concordance light orange; **MT deep orange**; non-translatables gray; auto-translation rules green. For 95-101% matches, six mini-icons show exactly what differs (lit = fix manually, gray = auto-fixed). Grid rows: Pre-translated content shows blue vs manually-confirmed green - at-a-glance MT/TM-filled vs human-confirmed.

### Locked segments
PM-only lock/unlock (any role). Locked = uneditable by linguist. Typical: lock 100%/context matches. Wordcount: counting locked is configurable in Statistics. Exports still include locked segments (bilingual RTF includes locked + status + comments).

### Glossary/termbase in editor
Term matches auto-surface in the term pane (blue). Shortcuts: `Ctrl+E` full term-entry, `Ctrl+Q` quick create. Terminology QA flags missed/mistranslated terms.

### QA checks (Edit QA settings - 100+ checks in groups)
Segments+terminology (missing/mistranslated/forbidden terms) · Consistency (same source→different target and inverse) · Numbers · Punctuation · Spaces · Capitalization · Characters + inline tags · Spelling/grammar · Repeated words · Length · Empty/untranslated.
Severity: each check = Warning or Error (default Warning). **Errors block export; warnings don't.** Findings: row icons + resolution pane; per-finding Ignore and "Ignore all of this kind."

### Reviewer mode
Roles Translator/R1/R2 tied to the status ladder. Track Changes vs chosen base version (last received/delivered/custom); accept/reject on selection; Reject sets Rejected status; "Change tracked" filter.

### Comments
`Ctrl+M` / bubble icon → Notes window with severity + scope (which part of segment), doc-level and segment-level.

## Trados Studio

### Editor grid
Side-by-side source/target + **Segment Status column** (status + origin icon per row).
Statuses: **Not Translated → Draft → Translated → (Translation Rejected) → Translation Approved → (Sign-off Rejected) → Signed Off** - strict escalating pipeline translator → reviewer → sign-off, with reject-back states.
Confirm: `Ctrl+Enter` (writes TU to TM, jumps to next unconfirmed). Copy source: `Ctrl+Insert`. Merge/split via Home > Segments (same-paragraph only).

### Match provenance
Origin icon in status column: **CM** (context match) / **100%** / **fuzzy %** / **AT** (automated translation = MT) / manual (no icon). Translation Results window lists all TM/MT candidates with match types. Quirk: tracked changes collapse origin display to "TC" - true origin then only in the results window.

### Locked segments
Locked = uneditable, lock icon in status column. Per-segment right-click lock/unlock; PM/agency locks CM/100%/reps at pre-translation. Batch locking is clunky (filter + multiselect). Comments cannot attach to locked content.

### Termbase in editor
Auto-search of active MultiTerm termbase on segment entry; recognized terms get red bracketed underline in source + Term Recognition window. `Ctrl+Shift+L` lists recognized terms for insertion. Terminology Verifier (QA) checks target against termbase.

### QA Checker 3.0 categories
1. Segment Verification (empty translations, length discrepancies) 2. Punctuation (end punctuation, double/extra/missing spaces) 3. Numbers (dates/times/numbers/measurements/currency) 4. Length Verification (char limits) 5. Inconsistencies (same source→different target; repeated words; unedited fuzzy matches) 6. Tag Verification 7. Terminology Verification 8. Segments to Exclude (scoping).
Severity per check: **Error / Warning / Note**. Findings: row icon + Messages window (click-to-jump), severity filter, per-finding dismissal.

### Reviewer mode
Review/sign-off opens with Track Changes auto-on (`Ctrl+Alt+F9`); accept/reject individually or globally; reviewer confirm → Translation Approved; sign-off confirm → Signed Off.

### Comments
`Ctrl+Shift+N`; target segments only (not locked); file + segment level; threaded across roles.

## MINIMAL VIABLE CAT EDITOR (agent's synthesis)

### Irreducible core (~13 features)
1. Two/three-column grid: source (read-only), target (editable), status/origin per row.
2. Status lifecycle: `Not Translated / MT-Prefilled / Edited / Confirmed` (+ optional `Approved`), visually distinct colors.
3. Confirm action (`Ctrl+Enter` convention) - advances status, records the confirmed pair + who/when (the event matters even without a local TM).
4. Copy source→target shortcut.
5. Per-segment origin/provenance tag: MT vs TM-100% vs fuzzy-with-% vs human-typed. (The single most important "where did this come from" signal.)
6. Locked segments: read-only + lock icon + PM-only unlock.
7. Basic term highlighting from a flat glossary map + hover/pane with target term.
8. Minimal QA pass: empty target · target==source · tag/placeholder mismatch · number mismatch · repeated word · same-source-different-target inconsistency.
9. QA findings per-segment icon + clickable findings pane.
10. Per-finding ignore/dismiss.
11. One comment field per segment (translator ↔ reviewer handoff).
12. Reviewer mode: simple diff highlighting of edits + distinct Approved status (full track-changes machinery NOT required).
13. Delivery/export: mark job done, pull finished bilingual/target content, explicit locked-segment handling choice.

### Safe to skip
Real TM lookup/fuzzy matching engine (static per-segment metadata instead) · subsegment/fragment leverage · AutoSuggest · alignment · concordance · advanced filters · ICR portal · full track-changes formatting/history · live MT integration · configurable multi-tier severity models · LiveDocs/term extraction · 4-stage role ladders (one reviewer tier suffices) · bulk-ops UI.

## Sources
memoQ: docs.memoq.com (the-grid, change-segment-status, translation-results-list, keyboard-shortcuts, lock-segments, edit-qa-settings, concepts-quality-assurance-qa-warnings, ignore-all-warnings, ribbons-review, review-changes-and-conflicts, notes-and-discussions, copy-source-to-target-where-empty); support.memoq.com (status-column, lock-unlock rules, keyboard shortcuts).
Trados: docs.rws.com (translation-statuses-650851, segment-status-column-340490, locked-segments-340566, term-recognition-window-340719, verifying-translations-359741, qa-checker settings-359765, working-with-track-changes-353720, approving-translations-353714, working-with-comments-354423, merging-segments-505253); community.rws.com threads (origin-display quirk, batch locking).
