# 0067. Review holds for ambiguous dates, line breaks and glued words

Status: Accepted
Date: 2026-10-11

## Context

The soak showed engine output that is wrong but not broken: a numeric date whose day and month swap meaning
(10/11/2026 to German 10.11.2026), an added or lost line break in a one-line string, and two words glued across a
formatting change (de, nl, fi, ko, tr, ar, he). The maintainer approved these checks.

## Decision

- Three QA checks set `review_hold`: segment status NEEDS_REVIEW and not TM-eligible. The text is still exported
  as written (it does not break the file); connectors hold the unit back as for any NEEDS_REVIEW unit (#640).
- A human save or approve clears the hold.
- All checks are bounded (linear, capped windows); date patterns skip IDs and take years 1000 to 2999 only.
- A price written out in words stays held (decision 12, no change).

## Consequences

- More segments reach review (12 real holds in a 12,663-segment rescan, 0 false positives on 32 probes).
- Glue lists are short on purpose; German content-word glues without a case change stay silent.
