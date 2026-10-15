# 0029. Untrusted text in prompts is fenced, and fencing errs toward fencing

- Status: Accepted
- Date: 2026-10-01 (#351), 2026-10-02 (#362)

## Context

Content placeholders are wrapped in a `<content>` fence unless the template author already fenced them.
The detector was switched off by prose mentions of a code fence and by inline tags (#351). Recalled
LLMemory text went into prompts unfenced (#362).

## Decision

- Author-fence detection only recognises a real opener (odd number of fence lines before the
  placeholder; block tags count, inline tags like `<b>`, `<span>`, XLIFF inline tags do not). Any change
  may only move toward fencing.
- Recalled LLMemory is wrapped in `<project_memory>` with every bracket escaped, plus one app-owned
  reference-data sentence appended after the locked floor; overrides cannot remove it, and the tag and
  sentence are refused inside override bodies.

## Consequences

Some safe-direction double-fencing is accepted. No memory block means byte-identical prompts.

## Links

- ADR 0025, ADR 0026
