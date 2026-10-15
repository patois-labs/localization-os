# Segmentation: Architecture (L1)

## 1. Role

This subsystem is **how a paragraph becomes translatable units**: sentence segmentation, inline-tag
extraction, size-aware chunk planning, the standards-based SRX rule engine, and the named,
scoped segmentation profiles that select a ruleset per customer or project.

The core (`app/services/segment.py`) splits a paragraph into sentence-level segments (the atomic TM
unit), extracts the inline tags MT must preserve verbatim (placeholders, printf specifiers, HTML
tags, URLs) into opaque tokens, and groups consecutive paragraphs into request-sized chunks so a
huge document translates without overloading a provider. The default splitter is a pragmatic regex
sentence splitter that also handles CJK enders. Since decision 15 (2026-10-11) it is versioned:
version 2 (stamp `legacy:2`, every new document) does not break after common abbreviations
(`app/services/segment.py::_ABBREV_RULES`: e.g., incl., ext., Mr., Dr., initials, "Max. 5", plus
per-source-language sets for de, fr, es, pt, it, nl); version 1 (stamp `legacy`, or no stamp)
is what documents imported earlier were split with, and export keeps re-splitting them that way.

The SRX engine (`app/services/srx.py`) is the real, standards-based path. It is opt-in: the legacy
splitter stays the byte-unchanged default (gated by the `srx_enabled` setting), and when enabled the
core splitter delegates to it with the source locale. It parses and validates an SRX 2.0 document,
caches compiled rules, and exposes a stable engine identity so a ruleset change is detectable.

Segmentation profiles (`app/services/segmentation_profiles.py`) are named SRX rulesets scoped per
customer or project, shared by the settings section, the per-project picker and the `segprofile`
CLI. A NULL-blob profile is the built-in default that segments byte-identically to the legacy
splitter; a non-NULL blob is a custom, bounded, parse-validated ruleset. Rulesets are immutable
(clone-before-edit), and at most one profile is the system default (per install, or per org while
multi-org is on). A project resolves its profile as: its own pick, then its customer's default, then
the system default, then (no profile at all) the install's active ruleset, so an install with a legacy
single custom upload and no profiles keeps segmenting with it.

It does not own the ingest that calls segmentation (translation-pipeline), the inline-tag
mask/restore ladder around the extracted tokens (inline-tags), or the TM the segments become units
of (tm-and-glossary).

## 2. Invariants

- **The default splitter is byte-unchanged; SRX is opt-in.**
  `app/services/segment.py::split_sentences` delegates to `app/services/srx.py::segment_text` only
  when `setting:srx_enabled` is on, so a stock install segments exactly as before. Enforced by
  `verify_srx`.
- **A stored document re-splits with the splitter version it was imported with.**
  Ingest stamps `seg_engine` `legacy:2` (abbreviation rules) with SRX off;
  `app/services/segment.py::split_sentences` runs version 1 for a `legacy` stamp and
  `app/services/segment.py::_stamped_engine` maps a missing stamp to `legacy`, so the abbreviation
  rules never move an older document's segment boundaries on export. The shipped
  `app/resources/default.srx` carries the same no-break rules (version 2); the version 1 ruleset
  ships as `app/resources/default_v1.srx` so an `srx:<v1 hash>` stamp still resolves, and it stays
  the timeout fallback (`app/services/srx.py::fallback_document`). Enforced by `verify_segment_abbrev`.
- **Inline tags are extracted to opaque tokens MT must not touch.**
  `app/services/segment.py::extract_inline_tags` finds placeholders, printf specifiers, HTML tags
  and URLs and replaces them with opaque tokens. Enforced by `verify_srx` and the inline-tags
  ladder.
- **A NULL-blob profile segments identically to the legacy default.**
  `app/services/segmentation_profiles.py::resolve_ruleset` returns the shipped default ruleset for a
  NULL-blob profile, reproducing the legacy splitter; with no resolved profile it returns the
  install's active ruleset (`app/services/srx.py::active_ruleset_bytes`), and the same bytes drive
  both the split and the `seg_engine` stamp at ingest. Enforced by `verify_seg_profiles`.
- **Rulesets are immutable: clone before edit, one system default.**
  `app/services/segmentation_profiles.py::clone_profile` is the edit path and
  `app/services/segmentation_profiles.py::system_default` finds the single default rung (one per
  org under multi-org, never clearing another org's). `update_profile` changes metadata only and never
  the blob. Enforced by `verify_seg_profiles`.
- **A custom ruleset is bounded and parse-validated.**
  `app/services/segmentation_profiles.py::validate_ruleset` bounds size and parses an uploaded SRX
  ruleset before it is stored. Enforced by `verify_seg_profiles`.
- **The install-wide SRX ruleset is host-only under multi-org.** `setting:srx_enabled` and
  `setting:srx_file` (and the one `DATA_DIR/segmentation.srx` they point at) carry no org
  dimension, so with `setting:multi_org_enabled` on an org admin may not replace the ruleset or
  toggle SRX for every org. The web form
  (`app/web/admin.py::settings_segmentation_save`) and the `/api` doors
  (`app/web/api_cli_admin_1.py::api_srx_upload`) refuse before any file is written, the write
  itself is atomic (`app/services/srx.py::write_custom_ruleset`), and the host CLI keeps the door.
  Enforced by `verify_multiorg_install_wide_files`.

- **A slow custom ruleset falls back to the default rules, and export replays that choice.**
  `app/services/srx.py::spans_with_default_fallback` re-segments a paragraph with the built-in
  default rules when a custom rule passes the per-match timeout or the 10-second paragraph budget
  (#511). The budget is wall-clock, so ingest records each fallback
  (`app/services/srx.py::record_fallbacks`, `skeleton.srx_fallback`) and export replays it
  (`app/services/segment.py::segments_per_unit`) instead of re-timing on another box. Enforced by
  `verify_srx_511`.

- **Export rejoins a unit with its own whitespace, not a blanket space.**
  Segments are stored trimmed, so `app/services/segment.py::unit_layout` recovers each unit's edge
  whitespace and the separator between its sentences with the same stamped split, and
  `app/services/segment.py::join_unit` (called from `app/services/export.py::paragraph_texts_for`)
  rebuilds the unit: edge whitespace kept, a seam whose characters are the source's own keeps the
  source separator (a held export is byte-identical), a line-break separator is kept, no space
  after a full-width stop or between two CJK characters, and one space where a no-space source
  seam now joins two spaced-script sentences (#668, #671). Enforced by `verify_export_seams_668`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Core segmentation | `app/services/segment.py::split_sentences` · `app/services/segment.py::extract_inline_tags` · `app/services/segment.py::plan_chunks` | Sentence splitting, inline-tag extraction, and size-aware chunk planning. |
| SRX engine | `app/services/srx.py::segment_text` · `app/services/srx.py::parse` · `app/services/srx.py::active_document` · `app/services/srx.py::engine_identity` | The opt-in SRX 2.0 rule engine with a stable engine identity. |
| Segmentation profiles | `app/services/segmentation_profiles.py::create_profile` · `app/services/segmentation_profiles.py::resolve_profile` · `app/services/segmentation_profiles.py::assign_to_project` · `app/services/segmentation_profiles.py::validate_ruleset` | Named SRX rulesets scoped per customer/project, immutable and validated. |
| CLI surface | `app/cli/cmd_segprofile.py::register` | The `segprofile` command group. |

## 4. Data & settings

- **Models:** `app/models.py::SegmentationProfile` (a named SRX ruleset with its scope and the
  system-default flag). Segments themselves are `app/models.py::Segment` rows.
- **Settings:** `setting:srx_enabled` (default off) switches the SRX path on; `setting:srx_file`
  points at the active ruleset resource. The built-in default is the shipped `default.srx`. Both
  keys and the custom `segmentation.srx` are install-wide: host-only under multi-org (#424).

## 5. Enforced by

- `verify_srx`: the opt-in SRX path, the byte-unchanged default, and inline-tag extraction.
- `verify_seg_profiles`: the NULL-default equivalence, immutability, the single system default, and
  ruleset validation.
- `verify_export_seams_668`: the export rejoin keeps unit whitespace and CJK seams (txt, csv,
  html, srt, docx through the real upload and export).
- `verify_multiorg_install_wide_files`: the install-wide SRX ruleset and its settings are host-only
  under multi-org (web and `/api` refuse before any write; the host CLI keeps the door).

## 6. Battle scars

- **SRX had to be opt-in and byte-identical off.** The standards-based engine ships behind a setting
  so a stock install segments exactly as before and enabling SRX is the only change, recorded in the
  `app/services/srx.py` module docstring.
- **A custom ruleset must be immutable once in use.** Rulesets are clone-before-edit (the memoQ
  convention) so a change cannot silently re-segment stored work, recorded in the
  `app/services/segmentation_profiles.py` module docstring.

## 7. Limits & accepted risks

- **The default splitter is a pragmatic regex, not a full SRX engine.** It handles common sentence
  and CJK enders; the standards path is SRX, opt-in, stated in the `app/services/segment.py` module
  docstring.
- **A custom SRX blob is size-bounded.** An uploaded ruleset is capped and parse-validated before
  storage, stated in the `app/services/segmentation_profiles.py` module docstring.
