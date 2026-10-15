# 0023. What the per-project AI opt-out promises

- Status: Accepted
- Date: 2026-09-27

## Context

#242: the per-project AI opt-out copy implied content could never reach any AI, but users can export
content or connect their own AI tools (the MCP agent connection) outside the switch.

## Decision

Fix the wording, not the feature. The canonical sentence: "This instance's AI engines never process this
project's content. Content you download, export, or connect to your own AI tools (for example the MCP
agent connection) is outside this switch." A per-user opt-out is deliberately not offered.

## Consequences

- Security questionnaires and the public docs use this sentence verbatim.
- Images sent to vision engines are not PII-redacted (#221 retest), so privacy claims say "text".

## Links

- .claude/rules/auth-and-security.md
