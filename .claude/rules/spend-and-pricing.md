---
paths:
  - "app/services/spend.py"
  - "app/services/pricing.py"
  - "app/services/reports*.py"
  - "app/services/project_budgets.py"
  - "app/services/budgets.py"
  - "app/services/jobs.py"
  - "app/services/ledger_controls.py"
  - "app/services/vendor_spend.py"
  - "app/services/advanced/**"
  - "app/services/eval_runner*.py"
  - "tests/verify_spend*"
  - "tests/verify_advanced_harness.py"
  - "tests/verify_live_translate_stream.py"
---

# Spend, pricing and budgets

Why: ADRs 0030 (free-engine rule), 0031 (Job.cost_usd). Architecture: `docs/architecture/cost-and-budgets.md` (CRLF in the working tree: edit as bytes).

- Free means: `kind == "local"` on a private endpoint (`local_endpoint_is_free`), or the `mock` kind. Everything else, including `openai_compatible` on a private IP and a public `local` host, is priced. One shared rule in `spend` (`kind_is_free`, `ref_is_free`, `cost_for_ref`); never re-derive it.
- `pricing.py` free model ids are an EXACT-ID allowlist (`mock`, `local`, `local-model`); no substring rules. Tiers pick from this call's prompt tokens; dated rates use the UTC job date.
- `Job.cost_usd` is the exact ledger cost booked at run time; readers prefer it via `_stored_job_cost` (finite and >= 0) and fall back to re-pricing. Only `jobs.py` writes it.
- Every `.submit(` / `run_in_executor(` in `app/` goes through `ai_optout.submit` (structural test): a raw pool submit loses the cost sink.
- Never put `cost_usd` on a yielded stream event or any translator-reachable payload; cost rides on `stats_sink` only (manager+ data).
- Advanced missions count toward the project budget and are refused at launch (`ProjectBudgetExhausted`, a `ValueError`: catch it explicitly). `AdvancedRun(` is created in exactly one place.
- Test fixtures: use the exact free id `mock`, or register a PAID stub as `claude` right before `try:` and pop `providers_hub._OVERRIDES["claude"]` in `finally`. A mock engine can never trip a cap.
- Spend cap blocks even localhost Jev seams; offline suites rely on MockProvider costing $0.
- Mock runs print `[<locale>] <text>`; that is expected offline output.
