# Context inference - the discovery playbook

Turning *"translate this for Acme into Spanish and French"* into a concrete,
correct job. The request names an intent and a customer; **everything else is
resolved from the install's own state**, never guessed. Each step below gives the
MCP tool and the raw-HTTP route; use whichever transport is live (SKILL.md §1).

The goal is a **resolved plan** you can state in one paragraph before acting:
customer, project, workflow, engine (live vs mock), locales, scope, cost posture.

## 1. Resolve the customer

Glossaries and translation memory are **partitioned by customer**, so a
customer id is needed far more often than it looks. Resolve the name to an id
first.

- **MCP:** `list_projects()` returns not only projects but a **top-level
  `customers` array** - the customer catalogue this key can see, including
  **customers that have zero projects**. Read that array directly; do not
  reconstruct the customer list from the projects (you would miss the empty ones).
  When you eventually create, `create_project` **reuses** an existing customer when
  its `customer_name` matches one **case-insensitively** (`strip().lower()`
  comparison), creating a new customer only when none matches.
- **HTTP:** `GET /api/customers` - match the user's name against `name`,
  fuzzily. "Acme", "Acme Corp", "acme" should all find the one Acme.
- **CLI:** `--remote ... --token ... customer list`.

If **several** customers plausibly match, or **none** does, **stop and ask** - do
not invent a customer or silently pick one. Creating a customer is a real write;
only do it when the user clearly intends a new client.

If the user's key is **customer-bound**, that binding already narrows everything to
their customer - you may not even need to resolve a name. The binding is **not**
reported by `whoami` / `GET /api/account` (those return role, not customer);
**infer it** from what `list_projects()` / `GET /api/customers` returns - a bound
key sees only its own customer, so a single-customer catalogue is the binding
showing through.

## 2. Learn the house pattern

The best template for a new job is the customer's **most recent similar project**.
Read their recent projects and copy what they already do rather than imposing
defaults.

- **MCP:** `list_projects()` for the list, then `list_projects(view="project",
  project_id=N)` for one project's full settings. **HTTP:** `GET /api/projects`
  filtered to the customer, then `GET /api/projects/{pid}` on the most recent one.
  Either transport reads the same full detail: its `source_locale`, target locales,
  `default_provider`, `default_workflow_id`, `provider_mode`, `content_type_id`.
- Prefer the most recent project **of the same content type / shape** (a marketing
  doc job is a poor template for a UI-strings job). If the customer has both
  document and key-based projects, match the **project_type** to the task: files →
  `document`, ongoing string catalogue → `key_based`. The type is **immutable at
  creation**, so getting it right matters.
- What you are harvesting: the **locales** they usually ship, the **provider** they
  use, the **workflow** they run, the **content type** that keys their glossary and
  segmentation. State any place your plan **departs** from their pattern (e.g. the
  user asked for a locale they have never shipped).

If the customer has **no** prior project, fall back to the install's defaults and
say so: default source `en-US`, the standard workflow (§3), the configured provider
(§4).

## 3. Find the preset workflow

- **MCP:** `list_workflows()` - every workflow this key can see, with usage and
  quality stats; `list_workflows(workflow_id=N)` for the full step graph (providers,
  prompts, QE gates, branching). **HTTP:** `GET /api/workflows`, then
  `GET /api/workflows/{wid}`.
- Prefer, in order: the customer's **named preset** (a workflow whose name or usage
  ties it to this customer/content type) → the project's `default_workflow_id` from
  step 2 → the install's **standard MT+LQA** workflow. Read the graph enough to know
  what it will **do** (does it have an LQA gate? a human_review step that will
  pause? which engine do its `llm_agent` steps name?).
- A workflow may belong to a **workflow group** that resolves a per-pair variant at
  launch - you do not pick the variant, the group does; just launch the workflow you
  chose and let resolution pick the right recipe for the pair.
- If **no** workflow fits and none is standard, plain MT (`submit_translation` /
  `doc translate`) is the honest fallback - but say the output will not pass through
  an LQA gate. Do **not** author a new workflow silently to fill the gap; authoring
  is a separate, explicit act (SKILL.md §4).

## 4. Judge the engine - live vs mock

This is the single most important thing to get right before spending.

- With **no provider key configured**, every engine resolves to the deterministic
  **MockProvider**, which **echoes** the source rather than translating. Its output
  is not a translation.
- The MCP spend tools do **not** decide this for you. An **unconfirmed**
  `submit_translation` / `run_workflow` plan names the **resolved provider and the
  locale/segment scope**, but it does **not** report live-vs-mock (and carries no cost estimate). The **authoritative live-vs-mock
  detector** is the **409** on `POST /api/documents/{doc_id}/translate`: it refuses
  before enqueueing if every target would land on the mock, so a 409 here is the
  engine telling you it is not live, not a transient error.
- A step that names provider `local` with no model pinned uses the install's
  configured **local engine** (which on some deployments is a tailnet model server,
  but the transport is deployment-specific) - that engine being **down is now a
  loud error**, not a silent fall-through. If it fails,
  report it and offer retry-later or an alternate engine **the user's role can
  select**; never downgrade to the mock (SKILL.md §5).
- Live engines **cost money** and may be stopped dead by a `spend_cap_usd`. Size the
  job before launching (§6).

## 5. Check asset state

- **MCP:** `list_assets()` - module glossaries (all, one, its candidates, or an
  export) and style guides. **HTTP:** `GET /api/glossaries`, `GET
  /api/glossaries/{gid}`, `GET /api/style-guides`.
- **Approval status is load-bearing.** Only an **approved** glossary or style guide
  contributes to a run; a **draft** contributes nothing. If a customer "has a
  glossary" but it is draft, the run will not use it - say so, and name who can
  approve it (`can_tune_ai`: admin or linguist).
- **Auto-attach**, for a **typed** project with no pin, brings in every approved
  glossary covering the pair (content-type-specific first, then General). This is
  the common, correct path - you usually do not need to touch glossaries at all.
- **TM presence:** `GET /api/projects/{pid}` reports TM counts. `apply_tm` (free)
  fills exact matches before any engine runs - the cheapest quality win; do it
  before `submit_translation`, not after.
- **Attaching a TM source / pinning a glossary IS drivable over the API.** Attach
  another project's TM as a **read-only source** with `attach_tm_source` (MCP) /
  `POST /api/projects/{pid}/tm-sources` - nothing is copied, an attach only widens
  what a lookup matches, and the source must share the customer or be a house
  project. Pin an **approved** module glossary with `pin_glossary` (MCP) / `POST
  /api/projects/{pid}/glossaries` - a draft stays inert until approved. Both are
  **write tier, `docs` scope, `can_tune_ai`**. *(The repo CLI verbs `project
  tm-sources add` / `project glossaries add` are **in-process only** and refuse over
  `--remote` - so use the MCP tool or the raw `/api` route as the remote path, not
  the CLI.)* **Writing delivered translations back into TM** now HAS an operator path:
  approve a segment (`approve_segment` / `POST /api/segments/{sid}/approve` /
  `doc approve-segment`) - approval writes the pair to TM, with a server-side hedge/
  non-translation withhold (`tm_written:false` means approved-but-not-seeded), and the same
  for a target that drops or adds a placeholder like `{count}` unless
  `acknowledge_placeholder_loss` is set. See
  `api-recipes.md` Recipe G.

## 6. Cost posture and scope

Before stating the plan, size the spend:

- **Segment/locale count** is the scale knob. Upload reports a segment count;
  `GET /api/documents/{doc_id}/segments` (or `work_status(document_id=...)`) gives
  per-locale counts. Multiply by locales.
- The **MCP plan** (unconfirmed spend call) carries the API's own cost estimate
  only where one exists - in practice **none today**. For
  `submit_translation` / `run_workflow` there is no estimate, so size the spend from
  the segment/locale counts yourself.
- **Every spend is confirmed the same way - no trivial-run exemption.** A
  single-locale MT pass on a small file follows the same state-plan → human-agrees →
  `confirm=true` + `plan_id` sequence as a **multi-locale batch**, a **large file**, or a
  **live-engine** job of real size (SKILL.md §3). Scale changes *how loudly* you
  warn about cost, never *whether* you wait for the go.
- Null `cost_usd` or token counts mean **unknown**, not free - report them as
  untracked, never as zero.

## 7. When to stop and confirm

Stop and wait for an explicit go when **any** of these holds:

- The user asked you to confirm before kickoff.
- The customer is **ambiguous or absent** (step 1).
- The plan involves **any spend** - every spend call is confirmed, with no
  trivial-run exemption (step 6).
- The plan **departs** from the customer's established pattern (a new locale, a
  different workflow, a different engine) in a way the user did not ask for.
- An **irreversible echoed confirmation** is required (delete, remove-locale,
  eval-launch estimate) - always ask; never echo the value yourself.

Otherwise, state the resolved plan and drive the job (SKILL.md §4, recipes in
`api-recipes.md`).
