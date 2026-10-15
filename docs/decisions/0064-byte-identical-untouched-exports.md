# 0064. Untouched files export byte-identical

Status: Accepted
Date: 2026-10-11

## Context

The format soak (10-11) found exports of untouched PO, XLIFF, JSON, SRT, INI/CSV and InCopy files rewritten:
quote style, attribute order, CRLF to LF, BOM dropped, Windows-1252 to UTF-8 (21% of soak results were this
warning). The maintainer decided to promise byte-identical output.

## Decision

- An export with no translation for that target returns the stored upload verbatim, after the filter's own parse
  accepts it. No `lang`, `target-language` or PO header stamp is added to an untouched file.
- Anything translated goes through `build_localized` as before, but keeps line endings, BOM and the code page where
  the text fits. Every charset label in the file must name the codec actually written; when the translation does not
  fit the legacy code page the file is written UTF-8 under UTF-8 labels (never `?` replacement).
- A localized PO is always UTF-8 with a UTF-8 header (decision 9 wins for touched files).

## Consequences

- PO and SRT/HTML exports with changes cost about 2x the old build time (still linear).
- Not covered yet: translated YAML, ASS and INI-via-kv in cp1252/UTF-16 still export UTF-8.
