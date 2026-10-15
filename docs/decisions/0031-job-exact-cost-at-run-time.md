# 0031. Each Job stores the exact cost booked while it ran

- Status: Accepted
- Date: 2026-10-02 (#364)

## Context

Reports re-priced past jobs from the CURRENT engine profile, so moving an engine from public to private
(a linguist can) re-priced history to $0, and price-table changes rewrote the past.

## Decision

`Job.cost_usd` (migration `d1a8c5e3b7f2`) stores the exact USD the live ledger booked during the run,
collected through a contextvar credit sink. Only `jobs.py` writes it. Reports, the project meter and
`project_check` prefer it when finite and >= 0 and fall back to re-pricing otherwise. Cost never rides a
streamed event; it reaches callers only through `stats_sink` (AI spend is manager+ data,
`reports.can_see_cost`). `backfill_embeddings` counts toward the meter.

## Consequences

- Every pool submit goes through `ai_optout.submit` (structural test) so the sink follows the work.
- Cut-off live streams and boot-recovery re-runs still estimate (progressive sink writes are a follow-up).
- Open owner question: keep `backfill_embeddings` in the meter.
