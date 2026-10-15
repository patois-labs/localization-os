# 0032. Jev is a product feature: judge-role engines with opt-in auto-fix

- Status: Accepted
- Date: 2026-09-22 to 2026-09-23

## Context

A quality-estimation proof of concept compared a dedicated hosted judge (Jev) and open clones with
general LLM judges on a 300-segment, 12-locale set. The dedicated judge was clearly better and far
cheaper per segment.

## Decision

- Two engine kinds, judge role only: `jev` (hosted, key only, URL pinned, priced, capped) and
  `jev_local` (SemIf/Kev-style, bring your own URL, optional key, free).
- "Unsure" never vetoes and never passes. Opt-in "Auto-fix failures" per step (1 to 5 rounds).
- A structural pre-check sits beside the judge (number checks, ADR 0033).

## Consequences

The remaining product choices from that round (#200 #201 #202 #213 part 2) were left to the maintainer.

## Links

- docs/architecture/quality-lqa-evaluations.md
