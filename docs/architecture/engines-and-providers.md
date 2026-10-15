# Engines and providers (the MT/LLM engine layer): Architecture (L1)

## 1. Role

With sign-in enabled, engine registration and configuration require an administrator
on web and API, including credentials, enablement, endpoint changes, the legacy local
slot and probes with unsaved destinations or keys (#375). Remote CLI and MCP use the
administrator API gates; local CLI access is trusted host-operator access. Linguists
can use configured engines and tune workflow models and prompts, but cannot register
an outside processor. Credential-origin clearing remains defense in depth for
administrator changes. Regression: `verify_engine_admin_boundary`.

Local model discovery (`app/services/providers_part2.py::local_list_models_result`) answers with the
model list plus separate signals for a policy refusal (`blocked`), a rejected key (`auth_failed`)
and a server that did not answer (`unreachable`, #564), so the Setup AI page, the engine rows and
`engine models` say "the server did not answer" rather than "no models found". The page's automatic
on-load probe is the quick form (2 seconds to connect) so an unreachable server does not hold the
card at "checking".

When instance PII redaction is on, AI text requests across web, API, CLI/MCP and jobs
mask recognized identifiers in all prompts, system messages and context payloads at
the engine boundary, then restore response placeholders before returning to callers.

Workflow steps with `redact_pii` also force this boundary protection while the
global setting is off. The synchronous call scope covers segment and full-text
generation, retries, document briefs, QE and serial/parallel LQA calls, including
their textual context. Each worker enters its own scope and resets it on return
or exception; no scope remains active across a workflow event yield. Images are
not PII-redacted. See `app/services/pii_boundary.py::call_with_redaction` and its
call sites in `app/services/orchestrator.py`, `app/services/orchestrator_part7.py`,
`app/services/orchestrator_part8.py`, and `app/services/orchestrator_part9.py`.

DeepL, Google Translate, Microsoft Translator, Cohere, Lara and Amazon Translate
recheck their effective endpoint at use time through the same local-engine client
guard as OpenAI-compatible engines. Stored profile validation alone is insufficient
when DNS changes later. Metadata, link-local and other fixed-deny targets are
refused before sending credentials; redirects are disabled. This shared provider
path covers web, API, CLI/MCP and background translations. Intentional private
local-engine targets remain supported under the existing engine policy.
DNS-to-socket address pinning remains a separate follow-up (#233).

This subsystem is the **model-facing layer every LLM task in the product goes through**: the
Provider seam that abstracts a call, the registry of credentialed engine profiles behind it, the
per-language-pair provider selection, the shared prompt builder, and the tolerant JSON extractor
for judge output.

Every task passes a natural-language `prompt` and a structured `task`+`payload` to a Provider
(`app/services/providers.py`). Real models answer the prompt; the deterministic MockProvider
answers from the payload, so the whole pipeline is testable offline with no keys. On top of the
seam sit the real Claude/Gemini/OpenAI/NMT/local clients, hot-swap, failover, backoff, streaming,
an output-token budget, and the truncation and thinking-burn guards. The engine registry
(`app/services/engines.py`) turns the single global engine slot into named `EngineProfile` rows,
each addressable as `engine:<id>`, with Fernet-encrypted keys that never echo and a per-kind
test-connection probe. Per-pair selection (`app/services/pair_providers.py`) lets a project name a
provider per (source, target) pair, falling back to the project then the global default.

The prompt builder (`app/services/prompts.py`) is the one place the MT path and the orchestrator
assemble a translation prompt from the canonical placeholder set, the glossary line, the context
block and the memory block. The JSON extractor (`app/services/llm_json.py`) is the one tolerant
parser both LLM-judge seams import, because the reference local model reliably wraps its JSON in
prose or a code fence and a bare parse would silently degrade a judge to a no-op.

It does not own the run engine that drives these calls (translation-pipeline, workflow-engine), the
spend ledger and cap the provider seam consults (cost-and-budgets), the embedder seam for semantic
TM (context-and-adaptation), or the quality lenses that judge a result (advanced-workflows,
quality-lqa-evaluations).

## 2. Invariants

- **Every model call goes through the Provider seam, offline-testable by the mock.**
  `app/services/providers_common.py::Provider` is the interface and `app/services/providers_hub.py::MockProvider`
  is the deterministic keyless implementation `app/services/providers_hub.py::get_provider` returns
  for the explicit `mock` provider, so nothing above this file changes between mock and real.
  Enforced by `verify_provider_resilience` and `verify_provider_robustness`.
- **A configured provider that cannot be built fails loud; it never fakes output with the mock.**
  A keyless built-in (claude, gemini, openai, local) raises `ConfiguredProviderUnavailable`, an
  unknown name raises `UnknownProviderError`, and a dead, disabled or keyless `engine:<id>` raises
  `DeadEngineError`, on a configured install (the 2026-08-07 production incident). The only way a
  keyless built-in becomes the mock is `app/config.py::MOCK_FALLBACK_OK`, which the hermetic suites
  and an offline-by-design box set. A zero-config install defaults to `mock`, the app banner says
  plainly when the default engine has no key, and choosing the offline mock for a project, a pair or
  customer defaults is an administrator-only act when sign-in is on
  (`app/services/pair_providers.py::refuse_mock_unless_admin`, #577; the trusted local CLI, an
  admin and an install whose default is already mock are not refused). Enforced by
  `verify_keyless_provider_state` and `verify_api_rbac`.
- **An engine key is Fernet ciphertext that never leaves the service.**
  `app/services/engines.py::create_profile` stores `api_key_enc` and
  `app/services/engines.py::engine_row` emits only a masked presence flag. Enforced by
  `verify_engines`.
- **An engine profile's `base_url` passes the egress door at the WRITE door, not only at call time.**
  `app/services/engines.py::create_profile` and `app/services/engines.py::update_profile` run
  `app/services/egress.py::validate_local_engine_target` for every kind and raise
  `app/services/engines.py::EngineTargetBlocked` (a `ValueError`, so every surface's existing
  error mapping carries the door's own text). CREATE/UPDATE bound the DNS wait with the
  door's opt-in deadline, because they run from an async route;
  `app/services/engines.py::test_profile` does NOT, because it is the last check before a
  real request to that target. An update whose submitted `base_url` is UNCHANGED from the stored one
  never revalidates -- the Setup AI form posts every field, so testing "was one passed" would
  freeze a grandfathered row -- so such a row stays editable and fixable, and
  `app/services/engines.py::test_profile` re-checks the stored target so such a row still answers
  with the door's words. Enforced by `verify_engines` and `verify_api_r3_config_credentials`.
- **`engine:<id>` resolves through the registry to a built Provider.**
  `app/services/engines.py::canonicalize_ref` normalizes the ref and `get_provider` builds the
  right Provider subclass from the row. Enforced by `verify_engines`.
- **Per-pair selection falls back deterministically, byte-identical in UNIFORM mode.**
  `app/services/pair_providers.py::resolve_provider` short-circuits with no rows in UNIFORM mode and
  otherwise resolves pair, then project, then global. Enforced by `verify_pair_providers`.
- **A step prompt without a source placeholder fails validation.**
  `app/services/prompts.py::validate_step_prompt` requires a `{text}`/`{prior}` source placeholder;
  `{context}` is a substitution key only, never the content to translate. Enforced by
  `verify_prompt_template_runtime`.
- **Judge JSON is extracted tolerantly, so a prose-wrapped verdict is not a silent no-op.**
  `app/services/llm_json.py::loads` strips code fences and preamble before parsing. Enforced by
  `verify_judge_json`.
- **A cut stream is a failure, and a restarting local engine gets a longer retry ladder.**
  A streamed completion that ends without a finish reason raises
  `app/services/providers_common.py::StreamIncomplete` (retryable, with the partial text and usage
  attached) instead of passing as a finished translation, for the OpenAI-compatible, Claude and
  Gemini clients alike (#606). A `local` engine with no failover target gets extra attempts on a
  capped, doubling ladder of about 78 seconds in total, but only for outage-shaped errors:
  502/503/504, a refused or reset connection, a connect timeout, or a cut stream
  (`app/services/providers.py::_outage_extra_attempts`, `app/services/providers.py::_is_outage_error`).
  A read timeout keeps the short ladder, because an engine that is up but slow is not restarting.
  A ladder spent in full marks that engine dead for 60 seconds, so a document with hundreds of
  segments against a dead or mistyped engine fails in seconds instead of holding every worker for
  the full ladder per segment; the mark clears on the next success and the terminal error names it.
  `app/config.py::LOCAL_OUTAGE_RETRIES` (environment `LOC_TMS_LOCAL_OUTAGE_RETRIES`, default 5; 0
  restores the old ladder) sets the extra attempts. Enforced by `verify_local_outage`.
- **A quality-judge engine never becomes a Provider, and never fills a translate slot.**
  `app/services/engines.py::JUDGE_KINDS` (`jev` hosted TypeSafe, `jev_local` for a Jev-compatible
  server such as SemIf or Kev) are scored only by `app/services/qe_jev.py::score_segment`.
  `app/services/providers_hub.py::_provider_for_profile` raises
  `app/services/providers_common.py::JudgeEngineNotAProvider` (a `DeadEngineError`) for them, and
  an unknown kind now raises `DeadEngineError` instead of silently becoming the mock.
  `app/services/engines.py::ROLE_KINDS` says which kinds fill which role (translate: LLM or NMT;
  judge: LLM or JUDGE; fixer: LLM); `app/services/engines.py::assert_ref_role` refuses a mismatch
  at SAVE time from the step add/edit seams, project create, customer defaults, routing rules, the
  per-pair map, the template draft and the `default_provider` setting, and `app/web/deps.py::engine_options` (default
  `role="translate"`) hides judges from every translate picker. Enforced by `verify_jev_engines`.
- **A hosted Jev key only ever reaches TypeSafe.** A `jev` profile's `base_url` is pinned to
  `app/services/engines.py::JEV_HOSTED_BASE_URL`; create and update refuse any other value.
  `jev_local` requires a `base_url` through the same egress door as every kind, allows a blank key
  (live without one, like `local`, in `app/services/providers_common.py::_profile_is_live`), and
  `app/services/engines.py::profile_warnings` warns on every write surface when the host is off
  loopback with no key. A `jev_local` URL naming TypeSafe (`typesafe.ai` or any subdomain, in any
  spelling) is refused at save, at resolve, at the probe and in the transport
  (`app/services/engines.py::assert_not_typesafe`), because it would bill uncapped and unbooked.
  A pasted key is stripped and refused if it holds a control character,
  judge probe errors never quote the exception text, neither the probe nor the scorer follows
  a redirect, and both use `app/services/engines.py::judge_timeout_s`. The hosted probe's one
  billable question honours the spend cap and books its reported usage. Under the multi-org
  gate every `/api/admin/engines/{eid}` route answers a foreign engine with the same 404 as a
  missing one (`app/web/api_cli_admin_2.py::_owned_engine`), so one org can never repoint and
  test another org's keyed engine. Enforced by `verify_jev_engines`.
- **A judge engine can be inspected on every surface, and the inspection cannot drift from the
  code.** `app/services/jev_inspector.py::inspect_payload` builds the criteria from the live
  question set and the request template from the same builder the scorer sends
  (`app/services/qe_jev.py::build_request`), and renders the decision rule with
  `app/services/qe_jev.py::describe_rule`, which reads the real constants and this profile's
  resolved floors at call time. `app/services/jev_inspector.py::try_it` scores one segment
  through the ordinary scorer (so a hosted try checks the spend cap first, sends nothing once it
  is reached, and books its cost), taps it to the Ledger as action `jev-try`, and returns the
  gate verdict and the exact round-1 post-editor prompt; no answer carries a key or an auth
  header, and because a Jev-compatible server is operator-run code, anything it answers is
  passed through `app/services/qe_jev.py::redact` before it reaches a trace, a Ledger record, a
  probe message or a try result (the key echoed raw, escaped, URL-encoded, in any letter case or
  as a field name is removed; a base64 or split echo is out of scope, and a key under
  `app/services/qe_jev.py::MIN_SUBSTRING_KEY_LEN` characters is matched only as a whole Bearer
  value so ordinary text is never garbled). A try's wall clock is bounded by the engine's timeout
  plus `app/services/jev_inspector.py::TRY_DEADLINE_MARGIN_S`, and its admission state is
  process-local (a single-process install; the hosted spend cap is the cross-process backstop). A
  try
  is admitted by `app/services/jev_inspector.py::_try_slot`: a per identity + org rate on the
  shared `app/services/ratelimit.py::check` window, one try in flight per engine and one priced
  try at all (so the cap check and the booking cannot interleave), else a 429 with Retry-After;
  and it is recorded under its own org-owned 'direct' Ledger action, so an org sees its tries. The web page (`app/web/admin_jev.py::setup_ai_engine_inspect`) is admin-only, and every
  surface answers a foreign, missing or non-judge engine with the same not-found
  (`app/services/jev_inspector.py::owned_judge`). `app/services/jev_inspector.py::import_env`
  turns `TYPESAFE_API_KEY` into a hosted `jev` engine, idempotently. Setting up a local
  Jev-compatible server (SemIf, Kev, Tailscale) is the operator guide
  [jev-compatible-server](../guides/jev-compatible-server.md). Enforced by
  `verify_jev_inspector`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| PII boundary | `app/services/pii_boundary.py` | Request masking and response restoration. |
| Provider seam | `app/services/providers.py` · `app/services/providers_common.py::Provider` · `app/services/providers_hub.py::MockProvider` · `app/services/providers_hub.py::get_provider` · `app/services/providers_part2.py::_require_model` · `app/services/providers_part3.py::_finalize_local_adapter_output` · `app/services/providers_part4.py::resolve_vision_capability` | The abstract call, the deterministic mock, and the resolver that builds a real or mock provider. |
| Real calls + budget | `app/services/providers.py::run_completion` · `app/services/providers_common.py::est_output_tokens` · `app/services/providers_common.py::ThinkingBurn` | The completion path with the output-token budget and the truncation/thinking-burn guards. |
| Runaway guard | `app/services/providers_part5.py::RunawayWatch` · `app/services/providers_part5.py::raise_if_loop` · `app/services/providers_common.py::GenerationRunaway` | #720: a translation-shaped engine call is watched while it streams; a repetition loop the request does not contain, or an answer far longer than the request, is cut off with `GenerationRunaway` (terminal for that engine, never escalated). A truncated answer that ends in a loop fails the same way instead of climbing the output-budget ladder. |
| Engine registry | `app/services/engines.py::create_profile` · `app/services/engines.py::canonicalize_ref` · `app/services/engines.py::test_profile` | Named `EngineProfile` CRUD, `engine:<id>` normalization, and the per-kind probe. |
| Quality judges | `app/services/engines.py::assert_ref_role` · `app/services/engines.py::_probe_jev` · `app/services/engines.py::profile_warnings` | The `jev` / `jev_local` kinds: role checks, the two-call Test probe (model list + one question), and the networked-keyless warning. The per-profile judge target the QE gate calls (`JevTarget`, `resolve_target`) belongs to quality-lqa-evaluations. |
| Judge inspector | `app/services/jev_inspector.py::inspect_payload` · `app/services/jev_inspector.py::try_it` · `app/services/jev_inspector.py::import_env` · `app/web/admin_jev.py::setup_ai_engine_inspect` · `app/web/admin_jev.py::setup_ai_engine_inspect_try` · `app/web/admin_jev.py::setup_ai_jev_import_env` | The Inspect page for a `jev` / `jev_local` engine (criteria, request template, decision rule, try-it with the post-editor prompt, Ledger link) and the one-click `TYPESAFE_API_KEY` import, shared by web, /api, CLI and MCP. |
| Profile resolution | `app/services/engines.py::get_profile` · `app/services/engines.py::invalidate_profile_cache` · `app/services/providers_hub.py::provider_is_live` · `app/services/providers_common.py::_profile_is_live` | The memoized profile read on the per-segment hot path, its write-invalidation, and the one liveness predicate both resolvers apply. |
| Local model warm-up | `app/services/model_warmup.py::start_for_run` · `app/services/model_warmup.py::collect_targets` | At run start (called from the run-envelope begin every launcher and both run loops pass through) a daemon thread asks a router-mode local server (`POST /models/load`) to preload the `local`-kind engine models the steps will use, capped at 3, best effort, never touching the run. Switch: `setting:local_model_warmup`. |
| Per-pair selection | `app/services/pair_providers.py::resolve_provider` · `app/services/pair_providers.py::mapping_for_project` | The UNIFORM/PER_PAIR provider chooser with deterministic fallback. |
| Prompt builder | `app/services/prompts.py::translation_prompt` · `app/services/prompts.py::resolve_effective_prompt` · `app/services/prompts.py::glossary_line` | The one placeholder-substituting prompt assembler for the MT and orchestrator paths. |
| Judge JSON extractor | `app/services/llm_json.py::loads` | The one tolerant JSON parser both judge seams import. |
| Engine CLI | `app/cli/cmd_engine.py::register` | The `engine` command group (CRUD + test, and for judges inspect, jev-try and import-jev-env). |
| Providers CLI | `app/cli/cmd_providers.py::register` | The `providers` command group (models + cloud keys). |

## 4. Data & settings

- **Models:** `app/models.py::EngineProfile` (a named, credentialed engine) and
  `app/models.py::ProjectPairProvider` (a per-pair provider override).
- **Settings:** engine keys are Fernet-encrypted with the install encryption key resolved in
  `app/config.py` (the same key connector credentials use), never a setting. The provider seam
  consults
  `setting:spend_cap_usd` before a real call (the cap itself is documented in cost-and-budgets), and
  cloud keys are managed through the `providers` CLI / Setup AI rather than as install settings.
- **A machine-translation engine is data, not code.** It is an `EngineProfile` row, for example of
  kind `local` pointing at a self-hosted model router. Changing the model is `engine update`, not a
  release, and the per-engine `max_parallel_jobs` cap (jobs-and-scheduler) bounds how many translate
  jobs hit it at once.
- **Model warm-up switch:** `setting:local_model_warmup` (default on, `config set local_model_warmup 0` to
  turn it off) gates the run-start warm-up of router-mode local models. Off sends nothing.
- **The profile memo is an environment dial, not a setting:**
  `app/config.py::ENGINE_PROFILE_CACHE_TTL_S` (`LOC_TMS_ENGINE_PROFILE_CACHE_TTL_S`, default
  5.0 seconds; 0 reads through every time) bounds how stale a memoized `EngineProfile` may be
  when ANOTHER process edited the row. It is not the invalidation mechanism: an
  `EngineProfile` written anywhere in THIS process drops the memo on the spot, through the
  flush and commit listeners in `app/services/engines.py`, so disabling or deleting an engine
  takes effect on the very next resolution. The memo exists because `get_profile` sits on the
  per-segment provider-resolution path, where a document's worth of segments once meant a
  connection checkout per segment (documented in configuration).

## 5. Enforced by

- `verify_provider_resilience` and `verify_provider_robustness`: the seam, failover, backoff and the
  truncation/thinking-burn guards.
- `verify_engines`: the registry CRUD, the `engine:<id>` resolution, the masked secret and the
  egress door at create/update/test (#144).
- `verify_stress_liveness_pool`: that a burst of resolutions over one profile issues a single
  row read, and that a write to that profile is visible on the very next resolution.
- `verify_pair_providers`: the UNIFORM/PER_PAIR resolution and fallback.
- `verify_prompt_template_runtime`: the placeholder contract and step-prompt validation.
- `verify_judge_json`: the tolerant judge-JSON extraction.
- `verify_jev_engines`: the judge kinds (dispatch to the right target, `jev_local` unpriced, hosted
  `jev` priced and capped, the pinned URL, role filtering and the save-time refusals, the probe, and
  the key never appearing in rows, MCP output, traces or errors).
- `verify_jev_inspector`: the Inspect page and its org scoping, the rule text tracking the real
  constants, try-it (request with no key or auth header, verdict equal to the gate decision, the
  round-1 prompt equal to what the loop sends, the hosted spend cap and booking, the Ledger tap),
  the /api, CLI and MCP twins agreeing, and the idempotent env import that never echoes the key.
- `verify_output_token_budget`: the output-token budgeting and escalation.
- `verify_local_outage`: the cut-stream failure, the extended local ladder, the dead-engine mark and
  the read-versus-connect timeout split.

## 6. Battle scars

- **A bare JSON parse turned the whole LLM-judge dimension into a silent no-op.** The reference
  local model prefixes or fences its JSON, so both judge seams raised and soft-degraded to an empty
  result while the run still reported approved; the one tolerant extractor fixed it, recorded in the
  `app/services/llm_json.py` module docstring.
- **Unbounded reasoning burned the output budget on the chat path.** The thinking-burn guard
  detects an empty completion with a length finish reason and applies the cure rather than escalating
  tokens forever, recorded in the `app/services/providers.py` module docstring.

## 7. Limits & accepted risks

- **A provider call already in flight cannot be interrupted.** Cancel and cap stop the next call,
  not one mid-flight, stated in the `app/services/providers.py` module docstring.
- **Static pricing lives with the code, not in the DB.** The model price table is a plain in-code
  table (cost-and-budgets) so the cap's calibration cannot be edited under pressure in the same DB
  an operator is racing, stated in the `app/services/pricing.py` module docstring.
