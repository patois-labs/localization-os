# 0021. No em dashes in human-read prose; ASCII in terminal output

- Status: Accepted
- Date: 2026-08-27 (outward copy); extended to all human-read prose; 2026-09-30 (12-hour times in reports)

## Context

Em dashes read as machine-written copy, and non-ASCII output breaks the Windows cp1252 console.

## Decision

- No em dashes in any prose a person reads: UI copy, docs, help, commit messages, reports, resource
  names and descriptions. Use a comma, colon, parentheses or a hyphen.
- ASCII only in anything that prints to a terminal (`verify_cli_ascii`; test `print()` output).
- Times in reports and UI copy use the 12-hour clock with AM/PM.

## Consequences

Pre-existing em dashes in code constants are left alone unless touched; never add new ones.

## Links

- .claude/rules/docs-and-copy.md
