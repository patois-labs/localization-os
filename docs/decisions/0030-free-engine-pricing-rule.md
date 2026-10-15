# 0030. One rule decides which engines are free

- Status: Accepted
- Date: 2026-10-01 (#360, #361)

## Context

Reports, project budgets, Advanced missions, the Evaluations Lab, OCR and the live ledger each decided
"free" differently (substring matches on model ids, `engine:N` local jobs priced at the default rate).
Meters and the ledger disagreed.

## Decision

- An engine is free iff `kind == "local"` and its base URL is a private endpoint
  (`local_endpoint_is_free`), or it is the `mock` kind. Everything else, including `openai_compatible` on a
  private IP and a public `local` host, is priced. The rule lives once in `spend` (`kind_is_free`,
  `ref_is_free`, `cost_for_ref`) and every consumer calls it.
- Model-id pricing in `pricing.py` uses an EXACT-ID free allowlist (`mock`, `local`, `local-model`),
  longest-match rows, size tiers from the call's prompt tokens, and dated rates by UTC job date.
- Default Gemini model is `gemini-3.6-flash`.
- When a job mixes providers, price at the most-used paid provider (over-count, safe direction).

## Consequences

- Mock runs can never trip a spend cap; tests that need a cap use a paid stub.
- Residual: a `local` engine pointed at a private proxy forwarding to a paid API is free.

## Links

- .claude/rules/spend-and-pricing.md; docs/architecture/cost-and-budgets.md
