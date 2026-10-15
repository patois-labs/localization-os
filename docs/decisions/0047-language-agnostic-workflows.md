# 0047. Workflows are language-agnostic; projects own languages; groups are Advanced

- Status: Accepted
- Date: 2026-10-02

## Context

The engine already takes a run's languages from the project (source) and the run (target) and never reads a
workflow's own pair, except to pick a variant inside a multilingual group. But Design a workflow stamped a pair on
every draft (en-US plus the first target by default) and fanned several targets out into a group, the builder
showed a pair and a "Copy to language pair(s)" panel, and the copy said a workflow "belongs to one source language
and one target language". Users read that as "a workflow needs a pair".

## Decision

- A workflow is written once for every language; prompts use `{source_locale}` / `{target_locale}`.
- Design a workflow asks for no languages, stamps no pair and creates no group.
- Multilingual groups (per-language variants) stay, as an Advanced feature; their CLI/API/MCP verbs stay and are
  documented as advanced.
- The glossary pin list shows every approved glossary with its languages; a pin that does not cover a run's
  languages is skipped at run time with a visible note.
- A one-time migration clears the pair on workflows that are not in a group; grouped variants keep theirs.
- Out of scope: Advanced missions' "en" source default; en-GB vs en-US group fallback.

## Consequences

Three trains (T1 designer + wording, T2 builder/groups/glossary, T3 migration) built in sequence.

## Links

- ADR 0011 (four surfaces); ADR 0025 (system prompt overrides)
