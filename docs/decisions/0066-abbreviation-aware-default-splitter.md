# 0066. Abbreviation-aware default splitter, versioned by stamp

Status: Accepted
Date: 2026-10-11

## Context

The default sentence splitter broke after "e.g.", "incl.", "Max." and similar (soak round 10). Changing the split of
an existing document would move every segment id and break exports, TM alignment and edits. The maintainer approved this constraint.

## Decision

- New documents are split with version 2 of the default rules, which hold sentence breaks after known
  abbreviations (titles case-sensitive, measure and number abbreviations only before a digit, single-letter
  initials except before a sentence opener).
- The split version is stamped on the document (`legacy:2`; SRX `srx:<hash>`). Old documents keep version 1:
  a stamp-less document maps to `legacy` (v1), and `default_v1.srx` is a byte copy of the old rules so its hash
  still resolves. Custom SRX rule files still override.

## Consequences

- Any new splitter change must add a version and keep the old ones replayable.
- Residual: a pre-stamp document ingested with SRX on and no custom rules re-splits with v2 (SRX opt-in; no known
  installs). MT chunking of giant segments has its own unstamped cut.
