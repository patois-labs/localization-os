# 0033. Number checks block by default; only two forms are advisory

- Status: Accepted
- Date: 2026-10-01 (#335), extended 2026-10-02 (#367)

## Context

The Jev structural check flagged legitimate locale renderings (6:45 PM as 18.45, "five" for 5) as MAJOR,
while missing real damage (dropped time, changed date, lost minus, swapped currency).

## Decision

- A number finding is advisory (MINOR) ONLY when rewriting one of two owner-approved forms back to digits
  clears it: a dot-format clock time H.MM equal to the source time, or a number word 0 to 10 for its
  digit in the listed languages. Everything else is MAJOR and blocks.
- Dropped times, changed dates (locale-correct reorders pass), lost minus signs and currency swaps or
  drops block (#367). Written-out times and dates are accepted silently.
- These rules live in `qe_jev_structural.py` / `qe_jev_numbers.py`; `qa.py` (shared by the editor and
  output guard) is unchanged.
- The rule text must read the same on all seven surfaces (help, llm.md, CLI.md, guides, architecture,
  the Jev server doc, `describe_rule`).

## Consequences

Corpus false-MAJOR rate 0.10% on 40,000 en to fr/es pairs. Known false negatives are listed in the gate.

## Links

- .claude/rules/jev-and-number-checks.md
