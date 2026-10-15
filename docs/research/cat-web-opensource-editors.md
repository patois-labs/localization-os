# CAT research: browser-based + open-source editors (research notes, 2026-08-08)

> For the Vendor Platform (test-only CAT tool) build plan. Primary-source based.

## MateCat (open source, LGPL-3.0, github.com/matecat/MateCat)
PHP backend + JS/Vite frontend; web UI → PHP CAT server; XLIFF converter normalizes sources, segmentation manager splits, DB persists.
- **Editor**: grid of source/target rows, per-segment status bar, TM/MT match panel by the active segment.
- **Statuses (4-state, color-coded)**: NEW (white) → DRAFT (grey, edited-not-saved; leaving without saving keeps text but does NOT update TM - a known trap) → TRANSLATED (blue, explicit save, updates TM) → APPROVED (green, reviewer). Striped bar = saved-without-TM-update. Manual override via dropdown.
- **QA**: tag mismatch (HARD error, blocks correct output), tag-order mismatch (soft), whitespace mismatch, translation conflicts (same source → different targets, warning); opt-in spellcheck/ICU; paid lexiQA add-on; aggregate Quality Report.
- **Revision**: Revise link → reviewer approves (Ctrl+Shift+Enter) or edits + tags an ISSUE TYPE before approving; bulk mark-approved; Revise 2 = identical second pass showing R1's issue tags.
- **PM flow**: split job into chunks; push-assign via email + auto-generated per-job password link + deadline (no claim model); progress % (raw word count); 100% flips "Draft" download → "Download translation"; split jobs need all chunks at 100%.

## Phrase TMS web editor (Memsource; desktop editor in maintenance since Oct 2025)
- **Layout**: segment grid + dockable panes: CAT pane (TM/MT/NT results with scores), Search, QA, Changes, Comments, LQA, Resources, in-context preview.
- **Provenance**: TM/MT/NT suggestions with scores; configurable coloring - default purple for MT/NT, or score-based (green 101/100, orange fuzzy).
- **QA**: termbase compliance, spelling, tag content/mismatch + configured checks. QA can be MANDATORY per job - warnings must be fixed or explicitly ignored (some checks "Unignorable") before Complete is allowed. Yellow-triangle warnings in QA pane; resolved go gray.
- **Flow**: linguist Accepts assigned job → works → Complete (editor menu or portal) → job goes READ-ONLY → next workflow step's linguists auto-notified instantly and were blocked from editing until then. Strict staged pipeline with notification-on-handoff.

## Smartling (brief)
- Language Resources panel: TM entries w/ status + last-used metadata + source TM; MT labeled by producing engine/LLM - explicit provenance labels. Glossary tab: term, translation, definition, notes, POS.
- **QA checks**: Spellcheck, Leading/trailing spaces, Blocklist, Whitespaces, Placeholder, Tag consistency, Insertable consistency, Emoji consistency, Number consistency, Repeated words, Target-in-wrong-language, Source-in-wrong-language, Capitalization consistency; string-level: Target length limit, Custom length limit, Plurals; Glossary Compliance (DNT usage).

## Crowdin (brief)
- Editor: AI/TM/MT suggestions + glossary terms highlighted IN the source string (mandatory terms specially marked).
- **QA checks**: Empty translation, Length, Tags mismatch, Spaces, Variables, Punctuation, Character case, Special characters, Flagged issues, Spelling, ICU syntax, Consistent terminology, Duplicate translation, FTL/Android syntax, Numbers mismatch, AI-powered check, Outdated translation.
- **Severity model**: each check type configured Warning ("Save anyway" allowed) or Error (cannot save) - per-check toggle, not a global gate.

## OmegaT (brief)
No grid - continuous running-text view with inline segment markers + dockable Fuzzy Matches pane (top 5 ≥30% from project TMX, insertable), Glossary/Dictionary panes, Hunspell. Proves a minimal keyboard-driven editor + matches pane + glossary pane is sufficient for real professional work - no server, no workflow, no QA gate.

## Weblate / Pontoon (brief)
- **Weblate checks framework** (the standout reusable taxonomy): translation checks on every save - format-string checks (~15 language-specific + Placeholders + Regex), markup (XML/BBCode/Markdown/Unsafe HTML/Fluent), punctuation/whitespace (mismatched end punctuation/ellipsis/etc., punctuation spacing, leading/trailing/double space), structural (newlines, plurals), consistency (unchanged translation, inconsistent-across-project, reused, glossary non-adherence), character checks (consecutive duplicate words, multiple capitals, zero-width space, accelerator keys, max length/lines), interpolation (ICU MessageFormat, i18next, Vue, AngularJS); source checks; automatic fixups (silent auto-corrections). PMs mark checks **"Enforced"** = un-ignorable, failing strings force-marked "Needs editing."
- Suggestions/voting: N-vote auto-accept - lightweight peer review. Locking: per-string timed lock + auto-lock while editing (prevents concurrent edits).
- **Pontoon**: suggestion panel with author+timestamp from history/TM/terminology/MT/other locales. Statuses Translated/Warnings/Errors/Missing. Two check tiers: strict = block save; loose = bypassable warnings. Pretranslation (100% TM or MT) flagged unreviewed; QA-failing pretranslations do NOT propagate until fixed in review - QA as a hard gate specifically for prefilled content.

## PM/workflow table
| Tool | Arrival/assignment | Progress | Complete/deliver |
|---|---|---|---|
| MateCat | push-assign email+link+deadline; splittable | word-count % | 100% unlocks download; all chunks needed |
| Phrase | workflow-template assignment; linguist Accepts | segment counts, per-language % | Complete → read-only → next step notified; QA gate optional-mandatory |
| Smartling | strings/jobs via workflow steps | dashboards | step completion advances workflow |
| Crowdin | open/claim or PM-assigned | per-language % | per-check severity gates saving; approval flips status |
| OmegaT | local files | manual stats | local export |
| Weblate/Pontoon | VCS sync; open contribution + locking | per-component % | per-string approval; VCS commit = delivery; Pontoon withholds QA-failing pretranslation |

## COMMON DENOMINATOR (agent's synthesis)
1. Discrete unit (segment/string) source/target pairing - everything hangs off the unit.
2. Status per unit, minimum 3-state arc: untranslated → translated/saved → approved, + a rejected/needs-fixing state.
3. Provenance badge on suggestions: TM-with-% vs MT vs other, color-coded by confidence.
4. Inline glossary highlighting + a "glossary term not used" QA check.
5. Enumerated QA list, always ≥: tag/placeholder integrity, whitespace, punctuation, numbers, terminology consistency.
6. Severity split: hard-block vs overridable-with-conscious-click (Crowdin per-check toggle, Pontoon strict/loose tiers).
7. Progress %, unit/word based, always gating something (download/complete/dashboard).
8. Explicit terminal action that changes who can edit - completion is a HANDOFF (locks prior editor, unblocks/notifies next role).

## ARCHITECTURE NOTES FOR A MINIMAL BUILD (agent's synthesis)
- **Data model**: Job → File → ordered Segments. Segment: source, target, status enum (new/draft/translated/approved/rejected - MateCat's 4 + reject), provenance field (tm/mt/human/none + numeric score), issue/comment field (MateCat's revision issue-tagging is cheap and worth copying). TM as a simple source→target lookup; no fuzzy engine.
- **Editing**: save-per-segment (`PATCH /segments/{id}` on blur or per-row save), never form-wide batch; autosave-on-blur avoids MateCat's draft-lost-on-navigate trap.
- **QA**: batch-gate at "Mark Complete" (less code than live checks): run fixed check set (tag/placeholder presence, empty target, whitespace, doubled spaces, glossary-missing, number mismatch ≈ 80% of everyone's catalogs), QA pane lists failures, fix-or-explicit-ignore per warning, keep ≥1 check truly unignorable (missing-tag), Phrase-style.
- **Stealable designs**: MateCat status color bar (white/grey/blue/green); MateCat Revise-1/2 with issue tags; Phrase's notify-next-step-on-complete; Crowdin per-check severity toggle; Weblate Enforced-checks flag; OmegaT's permission to not over-build (segment list + sidebar + status column = defensible v1).

## Sources
github.com/matecat/MateCat · guides.matecat.com (translationeditor, segment-status, quality-check-tools, qa-messages, revising, revise-2, splittingjobs, how-to-assign, step-5-deliver, finalising) · site.matecat.com architecture deliverable · support.phrase.com (CAT-Editor, CAT-Web-Editor, QA pane, QA overview, Job-Status, Workflow, Linguist) · help.smartling.com (CAT-Tool-Overview, Quality-Check-Options, MT-in-CAT-Tool) · support.crowdin.com (online-editor, qa-checks) · omegat.sourceforge.io manual · docs.weblate.org (checks, workflows) · pontoon.mozilla.org docs
