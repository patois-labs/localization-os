# 0025. Editable system prompts: locked floor, admin-only writes, behind a flag

- Status: Accepted
- Date: 2026-10-01 (#356 phases 1, 2a, 2b); extended 2026-10-02 (#365)

## Context

#356 asked for user-editable system prompts. The pipeline's safety depends on app-owned sentences: the
identity head (refusal detection keys on it via `refusal._META_STEMS` and output-guard n-grams), the
`<content>` fence sentence, the single-unit retry sentence, the MT locked prefix and suffix.

## Decision

- Phase 1: a read-only catalogue (`GET /api/system-prompts`, page, CLI), single-sourced from the real
  constants and builders, audience `can_tune_ai`. Assistant prompts are not exposed. Showing exact
  guard text is fine: no defence depends on secrecy.
- Phase 2: editable HEADS only. The floor (identity sentence, language pair, notation clause, fence,
  retry, MT locked prefix/suffix, memory fence) is composed in code around the editable body.
  `validate_body` refuses fence tags, markers, identity forgery and floor sentences in any spelling;
  it warns (never blocks) on "output only" style text. Body cap 2,000 characters.
- Writes are ADMIN ONLY (a maintainer decision): org heads, per-workflow heads, and group-default changes
  that move a live head. Linguists read; translators get 403.
- Behind a feature flag, default OFF. Flag off = byte-identical prompts (proven over 35 engine calls).
- Resolution: pair variant > base workflow (group default) > org > built-in, through one resolver.
  Each StepRun stamps key, version, scope and the sha256 of the composed text.
- Semantic sabotage by an admin cannot be refused; the control is admin-only plus audit (accepted).

## Consequences

- Any new app-added sentence joins `_floor_texts`, the forge checks and the catalogue.
- Known built-in-only paths: live budget estimate, style/context A-B, Evaluations Lab, Advanced workflows.
- Open owner items: whether customer-bound linguists may still edit GLOBAL workflows and groups; paused
  runs re-resolve on resume (no snapshot).

## Links

- .claude/rules/prompts-and-llm.md
