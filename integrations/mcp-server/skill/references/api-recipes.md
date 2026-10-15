# API recipes - literal sequences

Follow these literally. Ids are placeholders; substitute real ones. Each flow is
given in the **MCP-tool** form and the **raw-HTTP** form; the repo CLI in `--remote`
mode maps one-to-one onto the HTTP routes. Every HTTP call carries the token
through its **environment variable** - `X-Loc-Token: $LOC_STUDIO_TOKEN` (PowerShell
`$env:LOC_STUDIO_TOKEN`) - and a JSON body unless noted. **Never write the literal
`lsu_` value onto a command line**; it lands in shell history and the transcript.
Base path is `/api`.

Two conventions that recur:
- **Spend tools are two-phase.** Call `submit_translation`, `translate_text`,
  `run_workflow`, `extract_terms` **without**
  `confirm=true` first - you get a **plan** and nothing is spent. State it; call
  again with `confirm=true` plus the plan's `plan_id` (same arguments) only after
  the human agrees. No `plan_id`, changed arguments or a reused plan = `spend_refused`.
- **File-returning MCP tools write to disk.** `get_result`, `export_keys`, and the
  glossary/TM/image exports (`list_assets` glossary export, `manage_tm`, `list_images`) land in
  `LOC_STUDIO_OUTPUT_DIR` and return a **path** - the bytes never ride the tool
  result.

---

## Recipe A - Document project, end to end

### MCP

1. Orient. `whoami()` for role; `list_capabilities()` for formats,
   Language Sets and content types; `list_languages()` to resolve "German" →
   `de-DE`.
2. Resolve context (see `context-inference.md`): `list_projects()` for the customer
   and project list (it returns a top-level `customers` array too), then
   `list_projects(view="project", project_id=N)` for one project's full settings
   (`default_provider`, `default_workflow_id`, `provider_mode`, `content_type_id`);
   `list_workflows()`; `list_assets()`.
3. Create the project (only if none fits). `create_project(name="Acme Site",
   customer_name="Acme Corp", target_locales=["de-DE","fr-FR"])`. Pass
   `customer_id` instead when you know it; `customer_name` reuses an existing
   customer whose name matches case-insensitively (trimmed, lowercased), creating
   one only if none matches. `project_type`
   defaults to `document` and is **permanent**.
4. Upload. `submit_translation(file_path="C:\\...\\handbook.docx", targets=[],
   project_id=12)` - an **empty** `targets` uploads only, returning `document_id`
   and a segment count. (Or upload and translate in one call - step 6 - by passing
   the locales now.) **Always pass `project_id`** so it lands in the project from
   step 3. With **no** `project_id` the server's **routing rules** decide the
   destination (and may **override the engine**); only if no rule matches does it
   fall back to a **shared per-department inbox** (not a per-identity one), and
   `/api` exposes no route to preview that decision - so the plan cannot tell you
   the project or engine. Passing `project_id` is what makes both resolve in the
   plan.
5. Free quality win. `apply_tm(document_id=42, locale="de-DE")` per locale - fills
   exact TM matches before any engine runs. Costs nothing; do it **before**
   translating.
6. Translate. **Plain MT:** `submit_translation(file_path=..., targets=["de-DE",
   "fr-FR"], project_id=12)` - **plan first** (no `confirm`), then `confirm=true`
   once the human agrees. Returns one `job_id` per locale; it does **not** wait.
   **A real workflow:** `run_workflow(mode="run", workflow_id=9, document_id=42,
   locale="de-DE")` (plan, then `confirm=true` once the human agrees) -
   **synchronous**, the reply is the finished or paused state.
7. Poll (plain MT only - workflow runs return finished). `job_status(job_id=99)`
   long-polls ~45s and returns whatever the status is; call again while not
   finished. `finished` means only **COMPLETED or FAILED** - read `status` and
   `error`, a finished job is not automatically success.
8. Verify. `work_status(document_id=42, detail=true)` - **read `qa_flags`
   alongside status**, per locale. Counts alone hide quarantined segments. A run's
   graph and LQA findings: `work_status(run_id=NN, detail=true)`.
9. Export + save. `get_result(document_id=42, locale="de-DE")` writes the
   format-preserving file to `LOC_STUDIO_OUTPUT_DIR` and returns the path.

### Raw HTTP

1. `GET /api/account`; `GET /api/formats`; `GET /api/languages`.
2. `GET /api/customers`; `GET /api/projects`; `GET /api/workflows`;
   `GET /api/glossaries`.
3. `POST /api/projects` with
   `{"name":"Acme Site","source_locale":"en-US","targets":"de-DE,fr-FR","customer_id":7}`.
   **The new id comes back as `id`, not `project_id`** - every other route uses
   `project_id`. `project_type` is `document` (default) or `key_based`; `keys` is
   not a value (400).
4. `POST /api/documents` - multipart: `file`, `project_id`, `mode` (default
   `segmented`). Returns `{"document_id":42,"segments":118,...}`. Rate limited.
5. `POST /api/documents/42/apply-tm` with `{"locale":"de-DE"}`.
6. **Plain MT:** `POST /api/documents/42/translate` with
   `{"targets":["de-DE","fr-FR"]}` (+ optional `provider`). Returns **202** with a
   `jobs` array (one per locale) and a `status_url` each. **409 if any target would
   resolve to the offline mock** - it fails before enqueueing rather than emit fake
   output. **A workflow:** `POST /api/workflows/9/run` with
   `{"doc_id":42,"locale":"de-DE"}` (synchronous; reply carries `text` or a
   `paused_step`), or `POST /api/workflows/9/run-batch` with
   `{"doc_id":42,"locales":["de-DE","fr-FR"],"max_parallel":2,"fail_fast":false}`.
7. Poll `GET /api/jobs/{jid}` until it leaves `running`.
8. `GET /api/documents/42/segments?detail=segments` - per-segment `status` and
   `qa_flags`. For a run: `GET /api/runs/{run_id}?findings=1`.
9. `GET /api/documents/42/download?locale=de-DE` - raw bytes. Response headers
   `X-Export-Substituted` / `-Detail` flag tag-damaged units replaced by source.
   **409 when export is quarantined** by tag damage; pass `allow_source_fallback=1`
   to accept source text for the damaged units instead (an explicit choice).

### CLI (`--remote`)

**The global flags (`--remote`, `--token`) precede the subcommand** - they are
parsed at the root, so every call is `python -m app.cli --remote URL --token
$LOC_STUDIO_TOKEN <subcommand>` (never `<subcommand> ... --remote`, which is a
usage error):

`--remote URL --token $LOC_STUDIO_TOKEN project create "Acme Site" --customer-id 7
--targets de-DE,fr-FR` → `--remote ... --token ... doc upload 12 ./handbook.docx` →
`--remote ... --token ... doc apply-tm 42 de-DE` → `--remote ... --token ... doc
translate 42 --locale de-DE` (enqueues; poll `--remote ... --token ... job status
99`) **or** `--remote ... --token ... workflow run 42 9 de-DE` → `--remote ...
--token ... doc show 42 --detail` → `--remote ... --token ... doc export 42 --locale
de-DE --out handbook.de-DE.docx`.

---

## Recipe B - Plain MT, minimal

The fast path when there is no workflow and no LQA gate is wanted. Say plainly that
the output does **not** pass an LQA gate.

- **MCP:** `submit_translation(file_path=..., targets=["de-DE"])` - plan, then
  `confirm=true` once the human agrees → `job_status(job_id=...)` until finished →
  `get_result(document_id=..., locale="de-DE")`.
- **HTTP:** `POST /api/documents` → `POST /api/documents/{id}/translate` (202, or
  409 on mock, or 429 on a used-up budget; a 202 may list `budget_refused` locales) → poll `GET /api/jobs/{jid}` → `GET
  /api/documents/{id}/download?locale=de-DE`.

**One short string, not a file:** `translate_text(text="...",
target_locale="de-DE")` (plan, then `confirm=true` once the human agrees) /
`POST /api/translate/text`
with `{"text":"...","target_locale":"de-DE"}`. This route needs **`can_tune_ai`**
(admin or linguist) on a personal key even though it needs only the `api` scope, and
it does **not** refuse the mock - check `provider_used` before trusting the output.
Over `TRANSLATE_TEXT_MAX_CHARS` (204800) is a 413; a character with more than 32 code
points (a flood of stacked combining marks or joiners) is a 422 naming its position.

---

## Recipe C - Key-based continuous localization + the Delta cursor loop

Needs the **`keys`** scope, and 404s when `key_projects_enabled` is off.

### Set up and seed

1. Create a key project. **MCP:** `create_project(name="Acme Strings",
   customer_name="Acme Corp", project_type="key_based",
   target_locales=["de-DE"])`. **CLI:** `project create "Acme Strings" --type
   key_based --customer-id 7 --targets de-DE`. At least one target is required; the
   type cannot change later.
2. Import the source file. **MCP:** `import_keys(project_id=8,
   file_path="C:\\...\\en\\common.json")` - or `data="..."` inline when you have
   the text, not a path. **HTTP:** `POST /api/projects/8/keys/import` with
   `{"filename":"common.json","data_b64":"..."}`. The namespace defaults to the
   **file stem** (`common.json` → namespace `common`). Import is **merge-only** over
   MCP - it never retires absent keys.
3. Translate. **MCP:** `translate_keys(project_id=8, workflow_id=3, locale="de-DE")`
   (or `locale="all"`); it bills, so plan first and confirm with `confirm=true` plus the
   plan's `plan_id`. **HTTP:** `POST /api/projects/8/keys/translate`. **CLI:** `keys
   translate --project 8 --workflow 3 --locale de-DE`. The web key grid's Translate
   control does the same. (You can also set individual values with `upsert_keys` /
   `PUT /api/keys/{key_id}/values/{locale}`, but that is hand-entry, not MT.)
4. Export build files. **MCP:** `export_keys(project_id=8, locale="de-DE",
   namespace="common")` → path. **HTTP:** `GET
   /api/projects/8/keys/export?locale=de-DE&fmt=json&namespace=common`. **Naming the
   namespace** gives a **build file** (bare key names); **omitting** it gives a
   **transport file** (namespace-qualified, with a marker). JSON qualification nests
   under a top-level `$meta.loc_studio` envelope and one object per namespace - it is
   **not** a literal `namespace::name` key (that form is PO only).

### The Delta loop (every CI tick)

Store one integer cursor. On each tick:

1. **MCP:** `key_changes(project_id=8, since=<stored>)`. **HTTP:**
   `GET /api/projects/8/changes?since=1840&limit=500`.
2. Apply the entries. Each is `{cursor, entity: key|value, op:
   created|updated|deleted, key_id, namespace, name, target_locale,
   plural_category, ...}`. **A deletion is an entry (`op=deleted`), not an
   absence** - `key_id` survives it, so you can resolve what died.
3. Store `next_cursor` after **every** page. While `has_more` is true, request
   again immediately with the new cursor. On an empty page `next_cursor` equals the
   `since` you sent - that is not an error.
4. **`resync_required: true`** means your cursor predates retained history
   (default 90 days). `entries` is empty and a `message` names the floor. The
   **only** correct response: re-bootstrap from **`since=0`** and re-export every
   file. **Never** read it as "nothing changed."

   *Alternate mode - you already hold a full copy and just want what changed since a
   wall-clock time:* `key_changes(project_id=8, modified_after="<ISO>")` / **HTTP**
   `GET /api/projects/8/changes?modified_after=<ISO>&limit=500`. It returns the
   current-state value rows changed after that UTC instant (each with `text` +
   `content_hash`), keyset-paged by `next_modified_after` + `next_after_id` (feed both
   back), optional validated `locale=`. It **never reports deletions** and is not a
   bootstrap - keep the `since` cursor feed for the first sync and for tombstones.
5. Re-export the affected build files (step 4 above) and commit them downstream.
6. On the next release, `import_keys` the new source file again (add `cleanup` on a
   **source-language** import to retire strings removed from source; cleanup never
   runs on a translation import, which is a partial view by nature).

Only ever send a cursor **this feed gave you**: a negative one is refused (422),
and a cursor **ahead** of the feed is a silent blind spot (answers no entries and
`resync_required` false forever).

---

## Recipe D - Workflow authoring

*(Authoring surface - `SPEC-workflow-authoring-api`, **present on current builds**.
Installs differ, so still confirm the routes / tools below exist in the connected
install per §0; on a pre-authoring build authoring is local CLI or web only and the
remote CLI refuses at exit 2. One field below - `lqa_threshold` - rides a **later**
feature; it carries its own pin.)*

### The authoring surface (present on current builds; confirm live)

- **MCP:** `create_workflow(...)` - write tier, `docs` scope. Creates the workflow
  and optionally its full ordered step list in one call (partial-success reporting
  like `upsert_keys`; the reply accounts for every step). `edit_workflow(...)` -
  write tier; one **action** per call: `add_step`, `edit_step` (partial update,
  three-state booleans - `true`/`false`/absent = set/clear/leave), `delete_step`,
  `move_step`.
- **HTTP:** `POST /api/workflows` (create); `POST /api/workflows/{wid}/steps`
  (add); `POST /api/workflows/{wid}/steps/{sid}/edit` (partial); `POST
  .../{sid}/delete`; `POST .../{sid}/move`. `docs` scope; RBAC **`can_tune_ai`**
  (workflow *delete* stays `is_admin`).
- **CLI (in-process and, on current builds, over `--remote` - thin HTTP twins of
  the routes):** `workflow create --name "..."`, `workflow add-step 9 --type
  llm_agent --prompt "..."`, `edit-step`, `delete-step`, `move-step`; verify with
  `workflow show 9`, then `workflow run`.

### The step graph, conceptually

The three step kinds you author most: **Translate** (`llm_agent` - an LLM writes
the translation), **Check** (`lqa` - an LLM judge scores the translation and
produces a **pass/fail gate**), **Review** (`human_review` - execution **pauses**
for a person). Two more exist: **`tm_match`** (fills exact TM before any engine
runs) and **`external`** (dispatch-and-wait to a vendor). A step carries
`provider`/`model`, a prompt (inline `prompt_text` or a `prompt_template_id`), an
optional `glossary_id` **step pin**, conditional
`run_condition`/`branch`/`key`/`max_visits`, and an optional
`parallel_group` slug for adjacent steps that run at once.

**Two different gates - do not conflate them (an easy, costly mistake).** "Score"
appears in two unrelated mechanisms:

- **The `lqa` step gate.** An `lqa` step runs an LLM judge over the whole
  translation and emits a durable **pass/fail** gate. Its threshold is **not**
  `qe_threshold` - `qe_threshold` (and the whole `qe_*` family, plus
  `prompt_text`/`glossary_id`) is a **dead field on an `lqa` step and is refused at
  save**. The lqa pass rule is **`score >= 90 AND no major/critical finding`** by
  default. To gate on a **different** number - *"an LQA step that must pass with a
  94"* - set the lqa step's **`lqa_threshold`** field to `94`; that raises the
  numeric pass floor (the `no major/critical` half is unchanged, so a major finding
  still fails the gate regardless of score). `lqa_threshold` is **nullable**: unset
  / NULL = the built-in default 90. *(Requires the `lqa_threshold` field, landing
  with **feat/lqa-configurable-threshold** - this skill-doc train merges after it.
  Confirm the field exists in the connected install before you author it; an older
  build ignores or rejects it and you silently get the fixed ≥90 gate. Verify via
  the live card / `/openapi.json` per §0.)*
- **The `llm_agent` QE gate (`qe_gate=True`).** A *different* mechanism on a
  **translate** step: it scores **each segment** this step produced and routes those
  **below `qe_threshold` (default 70)** to `needs_review`. That is per-segment QE
  routing, not a whole-translation gate, and it does **not** produce the
  `{lqa_findings}` a reviewer/fix step consumes. When you want a gate a correction
  loop can key on, use an **`lqa` step** (+ `lqa_threshold`), not `qe_gate`.

### Prompt rules for an `llm_agent` step - verbatim, non-negotiable

- The prompt **must contain `{text}`** (or its alias `{prior}`, which reuses the
  previous step's output). Saving one without either is **rejected** with: *"Your
  prompt must include {text} where the source content is inserted (or {prior} to
  reuse the previous step's output). Without it the model receives no source to
  translate."*
- The prompt **should name the target language**, ideally with `{target_locale}`.
  Naming none is a **warning**: *"prompt names no target language - add
  {target_locale} (or name the language in the prompt)."* This matters more than it
  looks - the observed failure mode is the model echoing English into a whole
  namespace.
- Full placeholder list: `{text}`, `{prior}`, `{source}`, `{source_locale}`,
  `{target_locale}`, `{glossary}`, `{context}`, `{style_guide}`. **`{context}` and
  `{style_guide}` are substitution keys only - they never carry the content to
  translate, so they do NOT satisfy the `{text}` rule.** Unknown tokens are left
  verbatim. Substitution is a single simultaneous pass (a substituted value that
  looks placeholder-shaped is never re-scanned).
- An **empty** prompt is fine on any step - it falls back to a built-in preset that
  already carries `{text}` and the language. `lqa` and `human_review` steps ignore
  `prompt_text` entirely.

Warnings come back in the response as `"warnings": [...]` - surface them, never
drop them.

### The correction loop - translate → fix → LQA gate, deliver-or-hold

This is the shape behind *"translate it, review it, and if the LQA doesn't pass with
a 94 fix it and re-check - up to 3 tries; deliver on a pass, but if it still fails
don't deliver, hold it for me."* Two engine facts make it work and **both are
non-obvious - the natural reading gets them wrong:**

- **Loop-back is BACKWARD-ONLY.** A `branch` target names a step by its `key`, and a
  jump to an **earlier** key is the loop-back. So the **fix step must be authored
  BEFORE the lqa step** - the opposite of the natural "translate → LQA → fix" order.
  Author fix *after* lqa and `on_fail` has no earlier fix to jump back to → **no
  loop**.
- **The fix step runs a PHANTOM first pass, so `max_visits` = N+1 for N real
  attempts.** Because fix sits before the first lqa, it executes once *before* any
  LQA verdict exists (with empty `{lqa_findings}`); a `run_condition: prev_lqa:failed`
  on it **cannot suppress that pass** (no prior gate → the guard fails open). So for
  *"up to **3** real correction attempts"* set the fix step's **`max_visits = 4`**
  (1 phantom + 3 real). `max_visits = 3` yields only **2** real attempts.

**Deliver-or-hold is NOT the default - you must author the hold.** When the loop
exhausts `max_visits`, the branch seam refuses the jump and the run **falls through
and COMPLETES** - by default a persistently-failing locale is **delivered**, its
segments merely flagged `needs_review`. That is the **opposite** of "hold it." To
get *"deliver on pass, HOLD on persistent fail"* author an explicit trailing
**`human_review` step gated to fire only on the failed path**
(`run_condition {"ref":"prev_lqa","op":"failed"}`): on a persistent fail the run
reaches it and **pauses** (not delivered, waits for a person); on a pass the guard
is false, the step is skipped, and the run completes → delivered.

**The graph (the example above), as authored:**

```
#0 tm_match       key=tm
#1 llm_agent      key=translate   provider=<engine>   prompt has {text} + {target_locale}
#2 llm_agent      key=fix         provider=<engine>   max_visits=4
                  prompt consumes {lqa_findings} + {text} + {target_locale}
#3 lqa            key=lqa         lqa_threshold=94     branch={on_pass:"end", on_fail:{step:"fix"}}
#4 human_review   key=review      run_condition={ref:"prev_lqa", op:"failed"}
```

`fix` (#2) is **before** `lqa` (#3) so `on_fail:{step:"fix"}` is a backward jump.
`lqa_threshold=94` gates on 94 *(feature-pin: see "The `lqa` step gate" above)*. On
**pass** → `on_pass:"end"` (deliver) and #4's guard is false (skipped). On **fail**
→ back to `fix`, up to the cap; when the cap is hit the run falls through to #4,
whose `prev_lqa:failed` guard is now true → **pause** → held for human review.

The **fix step's prompt** obeys the same `llm_agent` rules above (must carry `{text}`
or `{prior}` and name the target language) and should add **`{lqa_findings}`** so the
reviewer sees what the judge flagged, e.g. `"You are revising a {target_locale}
translation. The LQA reviewer found: {lqa_findings}. Correct only those issues and
return the translation: {text}"`.

**Authoring the loop - the three surfaces.**

- **MCP** (`create_workflow`, one call - its `branch` step-field takes the **list**
  form `["on_pass=end","on_fail=fix"]`, bare keys, no `step:` prefix):

  ```
  create_workflow(name="Acme MT + LQA correction loop", scope="customer",
    customer_id=1, steps=[
      {"step_type":"tm_match", "key":"tm"},
      {"step_type":"llm_agent", "key":"translate", "provider":"local",
       "prompt_text":"Translate into {target_locale}: {text}"},
      {"step_type":"llm_agent", "key":"fix", "provider":"local", "max_visits":4,
       "prompt_text":"Revise the {target_locale} translation. LQA found: {lqa_findings}. Fix only those and return it: {text}"},
      {"step_type":"lqa", "key":"lqa", "provider":"local", "lqa_threshold":94,
       "branch":["on_pass=end","on_fail=fix"]},
      {"step_type":"human_review", "key":"review",
       "run_condition":{"ref":"prev_lqa","op":"failed"}}
    ])
  ```
  Then `edit_workflow(action="add_step"|"edit_step"|...)` to adjust. *(`lqa_threshold`
  is feature-pinned above - omit it on a build that lacks the field, or the step is
  rejected.)*

- **Raw HTTP** - `POST /api/workflows` (create) then one `POST
  /api/workflows/{wid}/steps` per step. The steps route's `branch` body accepts
  **either** the list form `["on_pass=end","on_fail=fix"]` **or** the object form
  `{"on_pass":"end","on_fail":{"step":"fix"}}`; `run_condition` is the object
  `{"ref":"prev_lqa","op":"failed"}`; `max_visits` and (feature-pinned)
  `lqa_threshold` are plain integer fields. Order the POSTs tm → translate → **fix**
  → lqa → review so `fix` precedes `lqa`.

- **CLI** (in-process today; `--remote` once the authoring surface is confirmed on
  the install - global flags first): `workflow create --name "..." --scope customer
  --customer-id 1` → `workflow add-step <wid> --type tm_match --key tm` → `... --type
  llm_agent --key translate --prompt "...{text}...{target_locale}..."` → `... --type
  llm_agent --key fix --max-visits 4 --prompt "...{lqa_findings}...{text}..."` → `...
  --type lqa --key lqa --lqa-threshold 94 --branch on_pass=end --branch on_fail=fix`
  → `... --type human_review --key review --run-condition prev_lqa:failed`. The
  `--branch` flag repeats (one per slot), targets are **bare keys** (`on_fail=fix`,
  never `on_fail=step:fix`); `--run-condition` takes the compact `ref:op` form
  (`prev_lqa:failed`). *(`--lqa-threshold` is feature-pinned above.)*

---

## Recipe E - Interpreting a run, and resuming a pause

### Reading a result

- Read **`status` AND `qa_flags` on every segment** - either alone will mislead.
  A segment can sit at status `mt` and still carry a `tag_integrity` flag that makes
  it unshippable. Statuses: `new`, `mt`, `edited`, `approved`, `needs_review`.
- `needs_review` is set by a QE gate below threshold, a refusal/non-translation
  guard, tag damage, a DNT or glossary violation, an LQA fix reverted, or a key's
  source text changing. **It is never a finished translation and must never ship.**
- Tag damage is a **hard** downgrade (also TM-ineligible and withheld from
  connector write-back); DNT/glossary violations are **soft** (flagged but
  eligible).
- Run `status`: `pending`, `running`, `paused`, `completed`, `failed`, `skipped`.
  **`paused` = a `human_review` step is waiting** - not terminal, not a failure.
  Batch `BatchStatus`: `running`, `completed`, **`partial`** (some locales did not
  finish - **not success**), `failed`.
- Read the run's **`notes`** before trusting a terminal status. It is empty on an
  ordinary run and carries a fixed sentence when something abnormal happened
  (*"Interrupted by a server restart."*; *"Superseded: no activity for over 6
  hours; restarted as run #N."*; a discarded-results sentence).
- Null `cost_usd`/token counts mean **unknown**, not free. A QE-judge failure does
  **not** block production (segment proceeds, counted in `qe_skipped`).

### Resuming a paused run

The **only** way past a `human_review` pause. **Never invent an approval** - hand
it to the person unless the user has explicitly given you the edited text.

- **HTTP:** `POST /api/runs/{run_id}/resume` with `{"edited_text":"..."}` or
  `{"edited_segments":[...]}`. **CLI (global flags first):** `--remote ... --token
  ... workflow resume 42 --edited-text "..."`.
- A **segment-count mismatch is a 400 and the run stays paused** - it does not
  half-apply. Fix the count and resend.

---

## Recipe F - Polling patterns

- **Async MT jobs** (`submit_translation` / `POST
  /api/documents/{id}/translate`): each locale is a job. `job_status(job_id=...)`
  long-polls ~45s then returns - **call again** while not finished; it does not
  block longer. `finished` = COMPLETED or FAILED only. Over HTTP, poll `GET
  /api/jobs/{jid}` until it leaves `running`. Report done/total between polls.
- **Workflow runs** (`run_workflow` / `POST /api/workflows/{wid}/run`): the API
  defaults to an async launch. MCP sends `wait=false` in all four modes, then polls
  `work_status` every 2.5 seconds within a 45-second budget across launch and status reads.
  Slow runs return their id and continuation payload instead of a wait-expiry error.
  If `done:false`, follow `next` and call `work_status(run_id=...)` / `GET
  /api/runs/{run_id}` again. `done:true` means terminal or paused; check status for
  success. The same bounded wait applies to batch, rerun and retry-failed modes.
- **Batches:** `work_status(batch_id=..., detail=true)` / `GET
  /api/batches/{batch_id}?detail=steps` for the per-locale matrix; `rerun` chosen
  locales or `retry_failed` via `run_workflow(mode="rerun"|"retry_failed",
  batch_id=...)` / `POST /api/batches/{id}/rerun` | `/retry-failed`.
- **Rate limits:** document upload, document translate, and translate-text are
  capped (default 120/min per identity). A 429 carries `retry_after` - honor it, do
  not tighten the loop. Everything else is unthrottled; do not hammer it.

---

## Recipe G - Export + local save

- **Document:** `get_result(document_id=..., locale=...)` writes to
  `LOC_STUDIO_OUTPUT_DIR` and returns the path / `GET
  /api/documents/{id}/download?locale=...` returns bytes. A **409** means the export
  is quarantined by tag damage - report which units; `allow_source_fallback=1`
  (HTTP) or `doc export --allow-source-fallback` (CLI, full-text only) delivers
  source text for the damaged units as an explicit, reported choice. Both overrides
  (`allow_source_fallback`, `allow_held`) need a write role (translator or above); a
  viewer key gets a 403.
- **A fully-untranslated locale now 409s ("held") - but partial exports still
  succeed, so you must gate on run status.** As of chip task_9c665111, exporting a
  locale where **NOT ONE segment carries a target** (a never-run locale, or a
  correction-loop hold whose segments are all still `new`) is refused with a **409
  `held`** whose body names `allow_held=1` (HTTP) / `doc export --allow-held` (CLI) as
  the explicit source-anyway override - you no longer silently get a 100%-source file.
  **KNOWN RESIDUAL (does NOT 409):** a run **paused at `human_review` with committed
  targets already written**, or a locale that is only **partially** translated
  (≥1 committed target, the rest `new`), exports **200** with source-fallback for the
  uncommitted segments. Full run-status gating of export is a deliberate design call
  left to the product owner. So: **gate on the run status, never on the export
  succeeding** - do not download a locale whose run is `paused` or whose segments are
  still `needs_review`, and never present a source-echo export as a delivered
  translation. (needs_review targets ARE intentionally shippable - that is a reviewed
  slice, not an uncommitted one.)
- **Keys:** `export_keys(...)` / `GET /api/projects/{id}/keys/export` (Recipe C).
- **Glossary / TM / image** exports also write to
  `LOC_STUDIO_OUTPUT_DIR` and return a path - via the MCP `list_assets`
  (`glossary_export` view), `manage_tm` (TMX export), and `list_images` (localized images) tools. (`search_tm` reads
  matches into the reply; it does **not** write a file.)
- **Always tell the user where the file landed** and surface any substituted /
  withheld unit counts alongside it.

### Saving delivered translations back to TM - the segment-approve path

*"Save the delivered pairs to translation memory"* **is** drivable over `/api`+MCP now:
**approving a segment writes its source→target pair to TM** (the same thing a reviewer
does in the CAT editor). Use it per segment you want to promote:

- **MCP:** the `approve_segment` tool (`{segment_id}`, optional `acknowledge_placeholder_loss`, optional `propagate`, optional `propagation` = `undo` | `unlink` | `link` to manage the segment's repeats instead of approving) - write tier, `docs` scope, or
- **`/api`:** `POST /api/segments/{sid}/approve` (optional body `{"acknowledge_placeholder_loss": true}`,
  `docs` scope, **can_write** - a viewer key is 403'd), or
- **CLI:** `doc approve-segment <segment_id> [--acknowledge-placeholder-loss] [--no-propagate]` (in-process or `--remote`).

Approving also copies the target onto the document's other open repeats of the same source (Issue #617,
same document and language only; never onto approved, human-edited, locked or unlinked segments). The reply
lists `propagated` ids and `propagation_refused`; send `propagate: false` to approve this segment only.

The **refusal/language guards run server-side** on every surface: a suspected hedge or a
non-translation (language mismatch) is **WITHHELD from shared TM** - the reply carries
`tm_written:false` with a `tm_skipped` reason, and the segment still keeps its approved
target. The same withhold applies when the target drops or adds a real placeholder such as
`{count}` (`tm_skipped_code:"placeholder_integrity"`); only pass `acknowledge_placeholder_loss`
when the person has said to save that broken pair to TM. Inline tags are stricter: a tagged
source or a target whose inline tags differ from the source is never saved to TM
(`tm_skipped_code` `tagged_source` or `tag_integrity`) and no acknowledgement overrides it. Every withhold also carries a `tm_skipped_code` (`placeholder_integrity`, `tag_integrity`, `tagged_source`, `source_fallback`, `empty_target`, `suspected_refusal`, `language_mismatch`, `tm_write_refused`). So read `tm_written` on the reply and report honestly: `true` means the pair was
saved; `false` means it was approved but deliberately not seeded into TM. A soft-deleted
key's value is refused (404). This approves ONE already-produced segment; it never
invents a human approval past a run's `human_review` **pause** (that stays
`resume_workflow` / a person - "do not invent an approval").

Alternatives that remain valid:
- **TMX import of the delivered pairs** - `POST /api/projects/{pid}/tm/import`
  (idempotent; a re-import skips pairs already present). Seeds TM in bulk from an
  external TMX rather than segment-by-segment; export with `tm/export` reaches the same
  bucket (a region/case locale variant is coerced to the project's spelling). The import applies
  the approve-time integrity gate (Issue #567): units whose target drops or adds a placeholder,
  or that carry inline tags, are withheld and listed in `skipped_rows` (`code`
  `placeholder_integrity`, `tagged_source`, `tag_integrity`); the rest import. Pass
  `acknowledge_placeholder_loss: true` (MCP `manage_tm`, `/api` body, CLI flag) to import
  placeholder losses; inline-tag breakage is never imported. `manage_tm` `edit` is gated the same way.

(Distinct from **attaching another project's existing TM as a read-only source**,
which has its own operator path - `attach_tm_source` / `POST
/api/projects/{pid}/tm-sources`; see `context-inference.md` §5.)
