---
module: engines
title: Engines and providers
summary: Engines are the models that do the work - your local server, a cloud LLM, or a machine-translation service - and every step, judge and extraction resolves one.
nouns:
  Provider: "A built-in engine slot: local, claude, gemini, openai, or mock. A built-in goes live the moment its key (or, for local, its base URL) is configured."
  Engine profile: "A named, configured engine you create yourself, referenced as engine:<id>. It carries its own base URL, key, model and settings."
  Quality judge: "An engine profile of kind jev (hosted Jev by TypeSafe) or jev_local (a Jev-compatible server you run, such as SemIf or Kev). It scores translations for a workflow step's QE routing gate and never translates, so it appears only in the QE judge engine picker. Hosted Jev is priced and spend-capped; a local judge is free."
  Model: "Which model an engine uses. Blank means 'the engine's own default' - a step with provider local and a blank model uses your default local model."
  Default provider: "The engine everything falls back to when a project, pair or step does not name one."
  Fallback provider: "An optional backup engine used when the primary errors out mid-translation."
  Mock: "The offline stand-in. It produces deterministic placeholder text, costs nothing, and is what a keyless built-in resolves to."
  Live: "An engine is live when it can actually run - a cloud engine with a key, or a local engine with a base URL. A dead engine reference is refused, never silently swapped for the mock."
  Spend cap: "An optional monthly USD ceiling on real spend. Once reached, new real calls are refused until it is raised. Mock calls and local engines on loopback, private or tailnet hosts are exempt, and so is a Jev-compatible local server; hosted Jev judge calls count and stop at the cap."
  LLM Ledger: "An admin/host-only recorder of the exact request and reply for every model call (off by default). Its bodies-withheld SUMMARY (counts, statuses, error classes, finish reasons, spend, timings - never the prompt or reply text) is readable at the docs tier over /api, `--remote` and the `ledger_summary` MCP tool."
verbs:
  Remove an engine key: "Web: Setup AI -> engine Edit -> Remove key -> tick confirmation -> Save. CLI: engine update ID --clear-key; --clear-access-key-secret removes a Lara/Amazon paired secret. API update: clear_api_key: true or clear_access_key_secret: true. Blank inputs keep keys. Hosted Jev is disabled after removal; local Jev becomes keyless and warns off loopback. MCP never carries keys."
  Warm up local models: "Automatic. When a run starts, the app asks a llama.cpp server running in router mode to preload the local-engine models the workflow's steps use (translate, fallback, QE judge and auto-fix engines), so a later step does not wait 10-30 s for its model to load. It is best effort and in the background, covers at most 3 models, and never delays or changes the run; a server that is not in router mode is skipped without a word. Turn it off with `config set local_model_warmup 0` (on by default)."
  Point the app at your local model server: "Web: Setup AI -> Local / self-hosted engine - set the base URL, key and model, then use Test connection and Fetch models to confirm it answers. CLI: `config set local_base_url ...`. A new install ships with the local engine not configured (the URL is empty); an existing install keeps its saved URL."
  List the models on an existing engine: "Web: Setup AI -> engine Edit -> Fetch models (uses the engine's saved key server-side while the base URL stays on the same server; a refused key is reported as such). CLI: `engine models ID`. API: POST /api/admin/engines/{eid}/models. The key is never returned."
  Add a cloud key: "Web: Setup AI / Providers page. CLI: `providers key set <provider>`. A key stored in the app overrides the one in the environment."
  Create a named engine: "Web: Setup AI -> Engines section. CLI: `engine create`, then `engine test` to confirm it is reachable."
  Limit parallel translate jobs per engine: "Web: Setup AI -> a local engine's Edit -> Max parallel jobs (blank = no per-engine limit). CLI: `engine update ID --config max_parallel_jobs=4` (blank clears). API update / MCP update_engine: config.max_parallel_jobs (1 to 64, translation engines only). It caps how many translate jobs run on that engine at once, but only below the global job-worker cap LOC_TMS_JOB_WORKERS (default 2), so raise that setting first when an engine has more parallel slots than workers. A job over the cap goes back to the queue (no worker is held), so other engines and job kinds keep running. Raising LOC_TMS_JOB_WORKERS also lets paid engines run that many jobs at once unless you give them their own max_parallel_jobs. Each running job also adds load to this server (the 2026-10-07 production test peaked at 177% of 2 vCPU with two jobs). Unset engines, including paid ones, have no per-engine limit."
  Add a quality judge: "Web: Setup AI -> Engines -> + Add engine, kind 'Jev (TypeSafe, hosted)' (paste the TypeSafe key; the URL is fixed to https://api.typesafe.ai) or 'Jev-compatible local server (SemIf, Kev)' (its URL, e.g. http://127.0.0.1:8077, an optional key, model and floors). Both kinds are listed under 'Quality judges (QE gate only)' in the Kind menu, and the new engine lands in the Quality judges group. Test (under the card's Edit) shows the model list, whether the configured model is in it, and one tiny scored question about a fixed test sentence, never your text; on hosted Jev that costs a few tokens and is not sent once the spend cap is reached. Then pick it as a workflow step's QE judge engine. Both kinds take the optional confidence floor (0.5 to less than 1.0; other values refused), meaning-changed floor and timeout (blank = install defaults; timeout 10 s hosted, 30 s local) on the Add form and under Edit, where a blank field clears the value. Blank config values also clear these overrides on CLI update and API update; blank judge models reset to jev-latest. Inspect warns about legacy profiles with a confidence floor of 1.0. Try it requires source and target text and locale codes from the language catalog, not language names. CLI: `engine create --name Jev --kind jev --secret api_key=...` or `engine create --name SemIf --kind jev_local --base-url http://127.0.0.1:8077`, with optional `--config confidence_floor=0.6 --config meaning_floor=0.8 --config timeout_s=30`. API: POST /api/admin/engines (the key travels only there, never over MCP)."
  Inspect a quality judge: "Web: Setup AI -> Engines -> Quality judges -> Inspect on the judge's card (admins only). The page shows the questions Jev is asked with what every level means and the exact request body, the decision rule (confidence floor, meaning check, unsure handling, score formula, pass threshold) rendered from the code's own numbers and this engine's settings, a Try it form that scores one segment and shows the request, the raw answer, the verdict, the cost and the exact prompt the auto-fix post-editor would get, and a link to the LLM Ledger filtered to that engine. Hosted Jev bills a fraction of a cent per try and honours the spend cap; a local server is free. Tries are limited to 20 a minute per person, one at a time per engine (one hosted try at a time), and stop after the engine's timeout plus 2 seconds. CLI: `engine inspect 7` and `engine jev-try 7 --source 'Pay ten euros.' --target 'Zahlen Sie neun Euro.' --source-locale en --target-locale de`. API: GET /api/admin/engines/{eid}/inspect and POST /api/admin/engines/{eid}/jev-try. MCP: manage_install inspect_engine and jev_try (never a key; a try is two-phase, so without confirm listing jev_try it only returns a plan)."
  Turn the TYPESAFE_API_KEY into an engine: "Web: Setup AI -> Quality judges -> Create a Jev engine from TYPESAFE_API_KEY, shown only when that key is set on the install and no hosted Jev engine exists yet. CLI: `engine import-jev-env`. API: POST /api/admin/engines/import-jev-env. Running it twice creates nothing new; the key is stored encrypted and never shown."
  Run a Jev-compatible server yourself: "Follow the guide docs/guides/jev-compatible-server.md (also in Help -> Engines -> Run your own Jev-compatible server): start SemIf on a GPU box, add a 'Jev-compatible local server (SemIf, Kev)' engine at its URL (http://127.0.0.1:8077 on the same PC, or a Tailscale address), press Test, then pick it as a step's QE judge engine. On another machine, start SemIf on its Tailscale address with SEMIF_API_KEY set (required off loopback) and paste the same key into the engine; on a hosted install, the TS_AUTHKEY secret routes engine calls to tailnet addresses through a Tailscale sidecar. The CLI refine experiment can use it too: `lqa jev-refine --judge engine:7 --yes` (it prints a cost estimate first)."
  See what is configured: "Web: the coloured provider dots at the bottom of the sidebar show live vs no key at a glance (a local engine reads 'configured, not probed' because only its URL is checked). CLI: `engine list`, `providers models`."
  Choose which engine a step uses: "Web: the provider and model fields on each workflow step; blank inherits the run default. CLI: `workflow edit-step ...`."
  Cap what it can spend: "Web: Setup AI -> spend cap - ADMIN only (the figures and the control are hidden from a non-manager, and a direct POST from one is refused with a 403), the same rung as adding a cloud key. CLI: `config set spend_cap_usd 50` (the local operator is trusted). Under multi-org only the host operator can change this install-wide cap."
  See exactly what was sent: "Web: LLM Ledger (admin) - turn it on, run something, read the request and reply. To see one engine only, filter by provider (a quality judge's Inspect page links `/prompt-tap?provider=engine:<id>`; CLI `prompt-tap show --provider engine:<id>`). Judge calls carry their auto-fix round number, and Inspect tries are recorded as action `jev-try`; a key a judge echoes back is stored as [redacted]. CLI: `prompt-tap on`, then `prompt-tap show`; the grouped run view is `prompt-tap actions` / `prompt-tap show-action <id>`, and `prompt-tap export` writes captured records to a file for analysis. A flatter RUN-first view lists one row per translation run and drills into its calls grouped by step then unit: web `/prompt-tap/runs`, CLI `prompt-tap runs` / `prompt-tap run <id>` (`--bodies` host-only), and the bodies-withheld `GET /api/prompt-tap/runs[/{id}]` + the `ledger_runs` MCP tool. For diagnostics WITHOUT the bodies, `prompt-tap actions --summary` / `show-action --summary` (and `GET /api/prompt-tap/actions[/{id}/summary]`, the `ledger_summary` MCP tool) return a bodies-withheld failure summary at the docs tier, over `--remote` too."
needs:
  - "For a local engine: a running server (LM Studio or similar) reachable at the base URL you configure, with a model loaded."
  - "For a cloud engine: an API key, entered in the app or the environment."
  - "Nothing at all to try the app: pick Mock explicitly (Setup AI page, DEFAULT_PROVIDER=mock in .env, or `config set default_provider mock`) and everything resolves to the offline mock, producing placeholder text. That is a sanctioned mode, not a failure - but it is not a translation, and it is no longer automatic on a bare install: a keyless built-in on a configured install now refuses loudly instead of quietly handing back mock output."
  - "With sign-in enabled, administrator rights are required to register, edit, enable or delete engine profiles, configure the local/self-hosted server, probe unsaved destinations or credentials, and set cloud keys. Linguists can view Setup AI and use administrator-configured engines to tune workflows, models and prompts. The spend cap and mock-egress guard also remain administrator-only. With sign-in disabled, the trusted local operator controls configuration."
limits:
  - "A local engine that restarts mid-run does not fail the run in seconds: a streamed answer that ends without a finish_reason counts as a failed attempt (never a complete translation), and 502/503/504, connection errors and cut streams are retried on a longer ladder (about 78 seconds in total) when no failover engine is set. If the engine does not come back the segment fails with the engine error and the run ends failed; after one call spends the whole ladder the engine counts as down for 60 seconds and later calls fail fast, so a dead engine does not hold every segment for 78 seconds; while that holds the error text starts with 'engine marked down'. A read timeout (the engine is up but slow) keeps the short ladder. Four long prompts at once can still exceed the engine's shared context pool and fail with a context-size error; that is an engine-side limit."
  - "Engine base URLs refuse userinfo on every save surface; put credentials in the API key field instead. Existing stored userinfo stays usable but is hidden in displays, with Test and Inspect warnings."
  - "A saved key belongs to the server it was saved for. Moving an engine's base URL (or the local engine's URL) to another server - a different scheme, host or port - clears its saved key and secret unless you enter a new one in the same save, and the move is recorded in the audit log. A path change on the same server keeps the key. A local or OpenAI-compatible engine with no key of its own borrows the local engine key only when it points at the local engine's own server; anywhere else it sends the harmless lm-studio placeholder."
  - "A keyless BUILT-IN on a configured install is REFUSED loudly, not silently swapped for the mock - that closed the 2026-08-07 incident where a claude-pinned install with no key filled two days of runs with mock output under green checkmarks. To run offline, pick Mock explicitly: the Setup AI page, DEFAULT_PROVIDER=mock in .env, or `config set default_provider mock`. A named engine reference (engine:<id>) that is dead, disabled or keyless is likewise REFUSED loudly, because both are explicit configuration that must never silently degrade to placeholder output."
  - "If translations come back as placeholder text like [de-DE] ..., you are on the mock. A MOCK banner shows at the top of every page while the mock is the default; switch the default provider in Setup AI to leave it."
  - "A stored cloud key that no longer decrypts (a rotated or lost encryption key) is treated as a dead engine rather than used as an empty credential. Re-enter it."
  - "The spend cap is a brake, not a budget: it refuses new real calls once tripped. Mock and local engines on loopback, private or tailnet hosts never trip it; public or unresolved local endpoints are priced and capped."
  - "Model output limits are real. A very long output is automatically retried with a larger budget up to the model's ceiling, and then it fails honestly rather than truncating silently. A segment whose translation would not fit the engine's output limit in one answer is translated in pieces (cut at sentence ends, punctuation or spaces, never inside a tag or placeholder) and rejoined as one segment; one with nothing to cut at goes to review with the reason instead of failing the job."
  - "LLM Ledger writes nothing at all while it is off - it is a true no-op, not a filtered log."
  - "A quality judge (jev / jev_local) can only be a step's QE judge. Saving one as a project default, a pair provider, a step provider or fallback, or the install default is refused, and a hand-written reference to one in a translate slot fails loudly at run time. A hosted Jev engine's URL cannot be changed, so its key only ever reaches TypeSafe."
  - "A Jev-compatible local server reached over the network with no key works, but the app warns: anyone who can reach it can use it and see what is sent to it. Set a key on the server (for SemIf, SEMIF_API_KEY) and add it to the engine."
  - "The legacy qe_provider value 'jev' (the TYPESAFE_API_KEY environment setup) keeps working and is still switched by the qe_jev_enabled setting; a Jev engine profile is switched by its own Enabled box instead."
connects:
  - module: workflows
    how: "Every Translate and Check step resolves an engine: its own provider/model if set, else the run default. A drafted workflow whose steps resolve to nothing runnable has them bound to your selected engine, with a warning naming which."
  - module: glossary
    how: "Term extraction is a real engine call and is spend-capped. Offline it still works against the deterministic mock."
  - module: documents
    how: "Translating a document dispatches per-segment engine calls, bounded by the concurrency setting. TM hits skip the engine entirely."
---

## What this is for

Nothing in this app translates anything by itself - it orchestrates models. This
module is where you tell it which model, and how to reach it.

## The three states worth recognising

1. **Mock** - chosen on purpose (Setup AI, Settings or the new-project dialog offer
   "mock"; or `config set default_provider mock`). Everything runs, nothing is real.
   Output looks like `[de-DE] Hello world`, a MOCK banner shows at the top of every
   page, and the document page badges the engine MOCK. A provider with no key is NOT
   the mock: it refuses to translate, and a warning bar says translation is blocked.
2. **Local** - your own server (LM Studio and friends). Costs nothing, works
   offline, and is the default the app is set up for.
3. **Cloud** - a key for Claude, Gemini or OpenAI, or a named engine you configure.
   Real spend, tracked per call, optionally capped.

## Getting started with a local model

1. Start your local model server and load a model.
2. **Setup AI -> Local / self-hosted engine**: put in the base URL.
3. Press **Test connection**, then **Fetch models** and pick one as the default.
4. Leave workflow steps' provider as `local` with a blank model - they will all use
   that default, and changing it later changes them all at once.

## When something looks wrong

- Instant results with suspiciously perfect scores usually mean the mock. Check the
  dots.
- A step failing with "references an engine profile that is not live" is doing the
  right thing: it was pointed at a named engine that is deleted, disabled or has no
  key. Re-point it or fix the key - it deliberately will not fall back to the mock.
- Turn on **LLM Ledger** and run one segment. It shows the exact prompt, the model,
  the budget and the reply. Almost every "why did it do that" question is answered
  there in under a minute.

Read a judge's stored QE card and auto-fix history with `doc show ID --detail` (or
`--json`), `/api/documents/{id}/segments?detail=segments`, or MCP
`work_status(document_id=ID, detail=true)`. Records include capped round texts, scores,
categories, unsure dimensions, stop/skip reason, kept version, fixer engine/model and cost.
`workflow show-run ID`, `/api/runs/{id}` and MCP `work_status(run_id=ID)` expose per-visit
`qe_steps` counters, with undecided separate from skipped and skip reasons counted.
Ledger `/api/prompt-tap/runs/{id}` and MCP `ledger_runs` identify each call's round and
purpose (translate, qe judge, auto-fix post-editor or cross-check) and label the step.
