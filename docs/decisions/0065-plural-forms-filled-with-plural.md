# 0065. Missing plural forms are filled with the plural translation, in every format

Status: Accepted
Date: 2026-10-11

## Context

#729: gettext catalogs into Polish, Russian, Arabic and others shipped the extra plural slots empty, so the program
showed English for most numbers. i18next JSON, Qt .ts, Rails YAML, Fluent and ARB had the same gap. Android, Apple
and Flutter already filled the forms. The maintainer approved the change.

## Decision

- One rule for all formats: every plural form the target language needs and the file lacks gets the plural
  translation (it keeps the count placeholder). The singular is used only where the target's rule puts n = 1, or
  when there is no plural translation.
- A form the file already has is never replaced; only empty or missing forms are filled. The PO slot map comes from
  the same Plural-Forms rule the header is written from, so header and slots cannot disagree.
- Ordinal selectors use the target's ordinal categories.

## Consequences

- Grammatically rough for some counts (few vs many share one text) but never English or blank. Translating each
  form separately is a possible later improvement.
- Not touched: i18next v3 suffixes, YAML flow maps, generic YAML, keys-hub PO export.
