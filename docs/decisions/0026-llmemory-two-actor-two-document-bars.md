# 0026. LLMemory learns only from two people or two documents

- Status: Accepted
- Date: 2026-10-01 (#358), 2026-10-02 (#363)

## Context

LLMemory distils reviewer edits into terms and LQA adjudications into style rules that are then recalled
into prompts. One person (or one document with many spans) could activate a rule on their own.

## Decision

- A learned (reviewer-edit) term needs TWO distinct HUMAN actors (email-shaped, casefolded) as well as
  its segment count. Automated actors (cli, system, scheduler, vendor-callback, unauthenticated, any
  actor without `@`, and local parts like `mt@`/`api@`) never count.
- An LQA-derived style rule needs findings from TWO distinct documents (distinct document id) in the same
  customer, project, locale pair and dimension.
- Legacy rows that do not meet the bar are demoted to CANDIDATE in place on the next distillation,
  audited; MANUAL and human-adjudicated rows are untouched. Suggestions never auto-apply.

## Consequences

- With auth off every actor is `system`, so learned terms never activate; only manual notes reach prompts.
- After deploying #358/#363, run one distillation per customer (Memories page) so demotions happen.
- Open owner questions: distinct documents by id (current) or by content (needs a Document fingerprint);
  guidance to keep `project_scoping` on where OIDC JIT is open to a broad IdP (one person, two identities).

## Links

- docs/architecture/llmemory.md
