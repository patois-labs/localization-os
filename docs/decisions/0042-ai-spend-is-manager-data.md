# 0042. AI spend is manager-and-above data; the install has a hard spend cap

- Status: Accepted
- Date: 2026-07-03 (spend cap); 2026-08-19 (cost visibility policy)

## Context

Translators see their own work; cost figures reveal pricing and customer volumes.

## Decision

- Cost and spend are visible only to managers and above (`reports.can_see_cost`). No cost field may ride
  a payload a translator can reach (SSE streams, image stats, CLI JSON for that role).
- A standalone install-wide spend cap (`spend_cap_usd`) is enforced in the single completion funnel;
  MockProvider and free local engines are exempt. Project budgets add per-project caps (Advanced missions
  included).

## Links

- docs/architecture/cost-and-budgets.md; ADRs 0030, 0031
