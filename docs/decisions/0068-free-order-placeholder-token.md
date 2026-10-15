# 0068. Argument-naming placeholders may reorder (token kind p)

Status: Accepted
Date: 2026-10-11

## Context

#693: MT legitimately reorders numbered or named placeholders (`%1$s`, `{name}`, `%(name)s`, XLIFF `<ph id>`), and
QA held every such reorder. Unnumbered ones (`%s`, `%@`, `{}`) cannot move without swapping values.

## Decision

- A placeholder that names its argument becomes a new point-token kind `p` (`⟦pN⟧`) that may move freely in the
  target. Drops, adds and duplicates still damage the segment. Every other point token stays `x` (order kept).
- Existing rows keep their `x` tokens; nothing is re-segmented. Only fresh ingests get `p`.
- `%(name)s` is protected as a placeholder. Latin-only product names and code lines no longer count as wrong script
  in a non-Latin target (only when the target has zero target-script letters).

## Consequences

- Any code that parses tokens must use the kind-agnostic grammar `⟦/?[a-z]?\d+⟧`.
- Other filters (STF `{0}`, TOML Go actions, server pages, subtitles) still emit `x` for named placeholders (safe:
  they hold on reorder). The MT prompt notation clause names r/g/x only.
