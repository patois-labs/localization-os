# 0004. Suites run only through the wrapper, selected by ownership

- Status: Accepted
- Date: 2026-09-05 (wrapper rule); suite_owners from 2026-09-16

## Context

There are about 470 `tests/verify_*.py` scripts (about 13 seconds each; a full sweep is about 90
minutes). Running one directly under pytest gives fake failures. Topic-based `-k` filters let three red
suites ship (e.g. `-k prompt_authoring` misses `verify_qa_7_8_10_authoring`).

## Decision

- Run suites only as `python -m pytest tests/test_verify_suites.py -k <expr>`; the wrapper runs each
  suite in its own subprocess with utf-8 decoding and temp-dir cleanup.
- Build `-k` from `scripts/suite_owners.py` over the diff, plus ratchet suites that read touched files;
  pregate fails on a missing owner. A `-k` filter is not a gate until you check what it collected.
- Never run the wrapper with an empty `-k`: it runs every suite.
- After a batch of merges, run one suite_owners sweep over the whole batch's diff.

## Consequences

Timing-sensitive suites are judged alone on an idle machine. Suites are free and offline (keys cleared,
throwaway `LOC_TMS_DATA_DIR`).

## Links

- .claude/rules/tests-and-pregate.md; CONTRIBUTING.md
