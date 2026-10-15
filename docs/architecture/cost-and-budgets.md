# Cost, budgets, and content routing: Architecture (L1)

## 1. Role

This subsystem is the **money brake and the intake router**: the global spend-cap kill switch, the
static model price table it prices against, the per-project monthly USD budgets, and the
deterministic content-routing rules that pick a project and provider for an inbound document.

The spend cap (`app/services/spend.py`) is the brake: a durable per-period ledger of actual spend
accumulated from the token usage the provider seam captures, an enforcement check the provider seam
consults before a real call so retries and failover cannot hammer past the cap, and one audit event
on the first trip per period. Pricing (`app/services/pricing.py`) is a deliberately plain in-code
table of published list prices, not a setting: a brake whose calibration lived in the same DB an
operator is racing to edit under pressure is a footgun, so an unknown model falls back to a
conservative rate that trips the cap early, the safe direction.

Per-project budgets (`app/services/project_budgets.py`) are the money picture a token cap cannot see
and the first budget that counts both AI model spend and vendor human spend, attributed by document
to project in the current calendar month. Content routing (`app/services/routing.py`) decides, for
an inbound document, which project and provider it should be translated under based on file extension
and department, deterministically and audit-logged so a decision is always explainable; a
caller-explicit choice always wins and is not overridden.

It does not own the department token budget service (that is `app/services/budgets.py` in
connectors), the
provider seam that consults the cap (engines-and-providers), the reports that render spend
(reporting-and-dashboard), or the vendor human-spend ledger (vendor-outsourcing). The web routing
surface here also saves the department budgets, whose service lives in connectors.

## 2. Invariants

- Every completion attempt checks the cap. Billed failures, including truncation and
  interrupted streams, accrue to the ledger and the returned cumulative `cost_usd`, so
  workflow StepRun costs include retries and failover. Missing final stream usage is
  estimated from the prompt and received output; unseen vendor-side work is unknowable.
- Direct glossary sense selection, glossary building, term pruning and term proposals
  use the same accounting rules, estimating prompt/output tokens if usage is absent.
- OpenAI semantic-TM embeddings check the cap and book input tokens for lookup, write-back
  and backfill. A blocked embed returns no vectors and retains fuzzy-only behavior.
- Local completion engines on tailnet hosts (`*.ts.net` or CGNAT literals) are free
  without DNS, including a SOCKS-sidecar topology. Other hosts are free only when
  all addresses are loopback, RFC1918 or IPv6 ULA (including Tailscale IPv6).
  Per-host decisions are cached for 300 seconds. Public and unresolved non-tailnet hosts use
  conservative
  default pricing, including the legacy local slot. Setup AI explains this distinction.
- Under multi-org, spend cap and period changes are host-operator settings. Web, API and
  their remote CLI/MCP configuration doors reject tenant changes at `settings.set_many`.
  Host-local `config set` and `config apply` remain the operator doors.
- Ask and both Draft doors share per-identity call limits, configured by administrators
  with `assist_calls_per_minute` (default 6) and `assist_calls_per_day` (default 60, UTC).
  Zero disables that window. Under multi-org these too are host-operator-only settings.
  The host CLI system identity has its own bucket with the same limits, even with auth off.
- The cap stops subsequent calls; it does not reserve estimated in-flight costs. Concurrent
  calls and the final billed attempt may overshoot it. Ledger writes use the existing
  single-process lock; distributed worker accounting requires database-level serialization.

- **The provider seam consults the cap before a real call; a trip audits once.**
  `app/services/spend.py::would_block` gates a real call and `app/services/spend.py::note_trip`
  audits the first trip per period, so no further paid call is made once the cap is reached. Enforced
  by `verify_spend_cap`.
- **Pricing is a plain in-code table, and an unknown model over-counts.**
  `app/services/pricing.py::cost_usd` matches by longest model-id substring and an unknown model
  falls back to a conservative rate rather than zero, so the cap trips early. Zero is an exact-id
  allowlist (`mock`, `local`, `local-model`), never a substring match, so a paid model id that merely
  contains "local" or "mock" is priced normally; self-hosted engines are free through the engine-kind
  exemption in `app/services/spend.py::is_exempt`. A price that depends on the call is not flattened:
  a Gemini Pro call is priced at the tier for its own prompt size (over 200k tokens bills higher, and
  an unknown size takes the higher tier), and the Gemini 3.6 to 3.8 Flash rows use the list price in
  force on the day of the call (a lower rate through 2026-12-31). Rows cite the provider's published
  price page. Enforced by `verify_spend_cap`.
- **Cached prompt tokens are counted once, never below their real price (#506).** OpenAI
  `prompt_tokens` and Gemini `prompt_token_count` include the cached slice, so the adapters record
  the non-cached remainder as `input_tokens` and the cached slice as `cache_read_tokens`.
  `pricing.cache_read_mult` bills a cache read at 0.1x input only where the published cached rate was
  verified at or below that (Claude, OpenAI gpt-5 and gpt-6 non-pro, Gemini 2.5 and 3.x text
  models), 0.5x for the older OpenAI families (gpt-4*, gpt-3.5, o1, o3, o4), and the full input rate
  for everything else. Enforced by `verify_provider_rest506`.
- **A paid call is priced on the model the wire actually used (#373).**
  A step or authoring call may leave its model blank to INHERIT the engine profile's model,
  and `app/services/providers_common.py::_effective_model` is what resolves it. Adapters share
  this resolver, including built-in configuration defaults and blank-profile fallback literals.
  Blank-model estimates in `cost_for_ref` call that same resolver without a second provider
  allowlist (#434); unknown provider names pass through unchanged.
  Both completion funnels pass the resolved model into the request before the attempt. The ledger prices
  that resolved id through `app/services/spend.py::billable_model`, and
  `app/services/providers.py::run_completion` resolves it once per target so the request, the
  success and failed-attempt bookings, the `complete_direct` bypass and the returned `model_used`
  all name the same model: an inherited gpt-5.2-pro call books its own rate instead of the
  conservative blank-model fallback, so an expensive profile cannot out-run the cap while the
  ledger under-reports. The `local` carve-out above is untouched: a `LocalProvider` still prices
  on the blank model. Enforced by `verify_spend_cap`.
- **One "is this engine free" rule serves every spend figure (#361).**
  `app/services/spend.py::kind_is_free` (the core of `is_exempt`) decides from an engine kind and its
  base URL: `mock`, or the self-hosted `local` kind (a local LLM router, LM Studio, Ollama, llama.cpp) on a
  proven loopback, private or tailnet host. `spend.cost_for_ref` and `spend.record_ref_usage` apply it
  to a stored provider reference (`mock`, `local`, `engine:<id>`), and are what reports, project
  budgets (jobs), the Advanced pre-call estimates, the Lab cost estimate and the OCR and
  image-localization spend records call, so a free engine contributes $0 to the meter, the refusal
  check and the reports, not the 5/25 default. A `local` engine on a public or unresolvable host is
  not free and prices conservatively, ignoring the model id it reports, exactly as the ledger does.
  A job without a stored cost (below) stores only the engine name, so the rule reads the profile as
  it is now: a deleted engine prices at the conservative default, and an engine whose kind or URL
  changed after the run prices by its current kind. A job of that kind that used a free engine and
  also a paid one (a failover) prices its one token total at the most-used paid provider, never at $0. Workflow StepRun and Advanced run spend are the ledger's own recorded figures and
  were already $0 for a free engine. Enforced by `verify_spend_cap`.
- **Each Job stores the exact USD the ledger booked while it ran (#364).** `Job.cost_usd`
  (nullable, alembic `d1a8c5e3b7f2`) is the sum of every `spend.record_usage` and
  `record_external_usd` booking made on behalf of the job: `run_completion` and `complete_direct`
  calls, failover and failed attempts included, a free engine's calls 0.0. `jobs._execute` collects it
  with `spend.cost_scope()` (a context variable holding shared sinks, so `ai_optout.submit` pool
  threads credit the same job and concurrent jobs never mix) and writes it with the terminal status,
  so a failed or partly paid job keeps its partial spend. `translate.pretranslate_stream` puts the
  same figure on the caller's `stats_sink` only (never on a yielded event: the summary reaches
  translator-role surfaces, and AI spend is manager+ data), from which the live stream settle and the
  CLI and image sync rows (`record_sync_job`) store it; a cut-off live stream stays NULL so it keeps
  its conservative estimate.
  `reports._job_cost`, and so the cost report, the project meter and the `project_check` refusal,
  prefer the stored figure: deleting or re-pointing an engine no longer rewrites history. NULL
  (rows from before the column, cut-off streams) and a non-finite or negative value fall back to
  pricing the result tokens as above. The figure is written only by the job runner from the ledger's
  own total: a payload or handler `cost_usd` is ignored and no API, CLI or web door accepts it.
  Workflow StepRun and Advanced run spend are separate rows (their own ledger figures), so nothing
  is counted twice. The project meter counts `backfill_embeddings` jobs too (a project-scoped
  backfill's embedder spend), so it still reconciles with the cost report. Enforced by
  `verify_spend_cap`.
- **A project budget counts both AI and vendor human spend, per calendar month.**
  `app/services/project_budgets.py::project_check` and
  `app/services/project_budgets.py::project_spend_this_month` attribute spend by document to project
  in the current month, honouring the report cost seams verbatim. Advanced Workflows runs count too
  (`AdvancedRun.spent_usd`, attributed by the run's own project) and an Advanced launch is refused
  at 100% exactly like a classic one (`ProjectBudgetExhausted`; 429 on `/api`), including each
  LTB arm E mission, which is refused before its mission row is written. Enforced by
  `verify_project_budgets`.
- **Routing is deterministic, audit-logged, and never overrides a human.**
  `app/services/routing.py::resolve` applies the first active matching rule by priority; a
  caller-explicit override wins and is a no-op decision that is not audited. Enforced by
  `verify_routing`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Spend cap | `app/services/spend.py::would_block` · `app/services/spend.py::record_usage` · `app/services/spend.py::note_trip` · `app/services/spend.py::meter_state` | The per-period actual-spend ledger, the pre-call check, and the first-trip audit. |
| Model pricing | `app/services/pricing.py::cost_usd` · `app/services/pricing.py::rates_for` | The static per-model USD table with a conservative unknown fallback. |
| Project budgets | `app/services/project_budgets.py::project_check` · `app/services/project_budgets.py::upsert_project_budget` · `app/services/project_budgets.py::budget_state` | Per-project monthly USD caps over AI and vendor spend. |
| Content routing | `app/services/routing.py::resolve` · `app/services/routing.py::create_rule` · `app/services/routing.py::validate_rule` · `app/services/routing.py::department_for` | Deterministic ext/department routing to a project and provider. |
| Web surface | `app/web/routing.py::routing_index` · `app/web/routing.py::routing_create` · `app/web/routing.py::budget_save` · `app/web/routing.py::project_budget_save` | The `/routing` rule CRUD and the department + project budget save routes. |
| CLI surface | `app/cli/cmd_routing.py::register` | The `routing` command group (the feature-gated top-level twin). |

## 4. Data & settings

- **Models:** `app/models.py::ProjectBudget` (a per-project monthly USD cap) and
  `app/models.py::DepartmentBudget` (the department token cap, whose service is connectors but whose
  web save route lives here). Spend is a per-period ledger keyed off the provider usage.
- **Settings:** `setting:spend_cap_usd` (default 0, off) is the global cap the provider seam checks;
  `setting:routing_enabled` gates the routing feature and its CLI. Model prices are the in-code
  `app/services/pricing.py` table, never a setting.

## 5. Enforced by

- `verify_spend_cap`: the pre-call cap check, the first-trip audit, and the conservative pricing.
- `verify_project_budgets`: the per-project monthly USD cap over AI and vendor spend.
- `verify_routing`: the deterministic, audit-logged resolution and the human-override no-op.
- `verify_budget_parity_segprofile`: the budget save parity across surfaces.

## 6. Battle scars

- **The cap's calibration must not live where an operator edits under pressure.** Pricing is a plain
  in-code table shipped with the code that reads it, and an unknown model over-counts so the brake
  trips early, recorded in the `app/services/pricing.py` module docstring.
- **A mid-month spend must be attributed, never invented as zero.** Project budgets honour the
  report cost seams verbatim rather than faking a zero for untracked spend, recorded in the
  `app/services/project_budgets.py` module docstring.

## 7. Limits & accepted risks

- **Routing v2 fields are reserved and refused.** Subject, workflow and language-set routing are
  reserved and rejected at rule creation, stated in the `app/services/routing.py` module docstring.
- **The cap bounds spend across calls, not a single overshooting call.** The check is before a call
  against an estimate and records the actual after, stated in the `app/services/spend.py` module
  docstring.
