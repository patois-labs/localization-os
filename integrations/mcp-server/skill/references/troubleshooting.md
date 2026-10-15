# Troubleshooting - the error vocabulary and diagnosis trees

Every status below is deliberate. The rule that governs the whole surface:
**refusals are the product working, not breaking.** Diagnose, adjust, and re-issue
the *right* call - do not retry the same one, and do not route around a guard.

## The HTTP status vocabulary

### 401 - bad credential
The token was absent or unrecognized, or the owning user is inactive / their org
suspended. **Fix the credential; do not retry.** Over MCP stdio this means
`LOC_STUDIO_TOKEN` is missing/invalid; over raw HTTP it means the `X-Loc-Token` /
`Authorization: Bearer lsu_...` header carried a token the server **rejected**. (The
separate case where the MCP server reports that **no** token reached it at all is a
tool-level error, not a 401 - see *"No Localization OS token on this request"* under
**Non-HTTP signals**.)

### 403 - real token, not allowed - **the message does not say which**
Three distinct causes share **one** response string:
- a **missing scope** (`docs` or `keys`) - a key holding only `api` gets 403 on
  anything needing them;
- a **role gate** failing (`can_write`, `can_manage`, `can_tune_ai`, `is_admin`) -
  all four return the same *"your role does not allow this action."*;
- a shared **`lsk_`** connector token on a personal-key-only route - the same
  string as the scope refusal.

**Diagnosis:** do not guess from the message. `GET /api/account` (`whoami`) for your
**role**; the key's **scopes** are visible only at `/account` in the web UI or via
`apikey list` (which over `--remote` is **admin-only** - a non-admin is refused, so
`/account` is the reliable place to read one's own scopes). Then map the action to
what it needs (below). **Retrying changes nothing.**

Common role requirements to name back to the user:
- `translate_text` / `POST /api/translate/text` → **`can_tune_ai`** (admin or
  linguist) on a personal key, even though the scope is only `api`.
- Language assets (glossaries, style guides, TM edits) → **`can_tune_ai`**.
- Approval-adjacent acts → **`can_manage`**.
- Deletes, key enumeration, workflow delete → **`is_admin`**.
- Nearly every other mutation → **`can_write`** (viewer is the only role that fails
  it).

### 404 - **four situations wearing one face** (by design)
A distinguishable 404 would let a caller enumerate another tenant, so these are
intentionally indistinguishable:
1. the object **does not exist**;
2. it exists in **another org or customer** (foreign objects answer 404, never
   403);
3. the whole **feature is switched off** (a kill switch - see below);
4. the project exists in **your own org and customer but you are not a member** of
   it (only with `project_scoping` on, and only for a **translator/viewer** who is
   neither creator nor in the `ProjectMember` list; admin/manager/linguist and
   customer-bound accounts never hit this).

**Diagnosis tree, in order:** (a) if it is a **key or image** route, suspect the
**kill switch** first - `key_projects_enabled` / `image_localization_enabled` off
makes the whole route 404. (b) Check **tenancy** - is this really the user's
customer/org? (c) Check **membership** - the one you can fix without changing
tenancy. Only after all three conclude it is a genuinely missing row. **Never probe
neighbouring ids** to test which case it is; that is the enumeration the design
prevents.

### 400 - validation failure, human message in `error`
A bad locale, a missing customer (`MissingCustomerError`), a mismatched
confirmation, a bad due date, an unknown project type (`keys` is not a valid
`project_type` - use `key_based`), a segment-count mismatch on resume. **The shape
of the call was right; read the message and correct the request.**

### 409 - a conflict the server refuses to resolve for you
The four that matter:
- **every translation target would resolve to the offline mock** - the engine is
  not live. Do not work around it; report it (see "engine down" below).
- an **export is quarantined** by tag damage - `allow_source_fallback=1` accepts
  source text for the damaged units as an explicit choice.
- a **delete refused** while other objects reference it - the response **names
  them**.

### 413 - over a size cap
`MAX_UPLOAD_BYTES` = 104857600 (100 MiB, document upload + key import);
`TRANSLATE_TEXT_MAX_CHARS` = 204800 (single-string translate; a character over 32 code
points of stacked marks or joiners is a 422 there, not a 413); `CONTEXT_JSON_MAX_CHARS`
= 8192 (upload context field). In **remote MCP HTTP** mode a POST body over **8 MiB**
is rejected pre-dispatch - if you hit it, use **stdio with a `file_path`** (the
server reads bytes off disk, no wire limit) or **chunk the import** (`import_keys` is
merge-only, so namespace- or locale-sliced imports compose). These constants are
env-overridable - treat them as defaults, not guarantees.

### 415 - unsupported file format on upload
`GET /api/formats` (or `list_capabilities`) for the supported list. Catch this
**before** upload by checking the format first.

### 422 - malformed parameter
A non-integer or negative `limit`, or context JSON that is oversized or
structurally invalid. Note the asymmetry: on the **key listing** an out-of-range
`limit` is a 422, but on the **change feed** an oversized `limit` is silently
**clamped** (only a negative one is a 422).

### 429 - rate limited or out of budget
Applies to document upload, document translate, and translate-text only (default
120/min per identity). Honor `retry_after` / the `Retry-After` header; **do not
tighten the loop.** Out-of-budget (a tripped `spend_cap_usd` or department budget)
can also surface here.

## Non-HTTP signals

### `No Localization OS token on this request` (MCP streamable-http)
Not an HTTP status - a **tool-level error** the MCP server raises when a request in
`streamable-http` mode reaches it carrying **no** `Authorization: Bearer lsu_...`
(or `X-Loc-Token`) header **and** no server-side `LOC_STUDIO_TOKEN` fallback is set.
It means the **header never reached the server**, not that the token is wrong. The
usual cause on **claude.ai** is the known **beta/bug** (`anthropics/claude-ai-mcp#644`)
in which the configured request header is never sent - the connector can look
healthy (all tools listed) yet the first tool call fails this way. It is **not** a
Localization OS fault; see the skill README's *"claude.ai status"* caveat. Fix the
header/token wiring; retrying unchanged will not help.

### `resync_required: true` (change feed)
Not an HTTP status. Your cursor predates retained history; `entries` is empty and a
`message` names the floor. **The only correct response: re-bootstrap from
`since=0` and re-export every file.** Treating it as "nothing changed" silently rots
every downstream file. `since=0` itself never reports it.

### `errors` non-empty in a 200 bulk response - **partial success**
`upsert_keys` / `POST /api/projects/{pid}/keys` report per-key failures this way. **A
200 is not a promise that every key landed** - inspect `errors` (and, over MCP, the
per-key report) every time.

### CLI exit 2 - a usage error, and *sometimes* the host-only refusal
Exit `2` is the CLI's **usage-error** code, and it is **overloaded**: it covers both
an ordinary usage mistake (an unknown flag, a bad argument, or **global flags placed
after the subcommand** - `--remote`/`--token`/`--json` must precede the command) and
the deliberate **host-only refusal**. So do **not** read every exit 2 as "this
capability is host-only" - **confirm the message says *"not available over
--remote"*** first. When it does, that **CLI verb** is host-only by design
(engine/provider key management, `db`, `init`, prompt-tap, `ask`/`draft` verbs, and
more): there is **no flag that unlocks it** - run it locally on the server, or use
the web UI. **But a host-only CLI verb does not always mean a host-only
capability:** `project tm-sources` and `project glossaries` refuse over `--remote`,
yet the same capability *is* reachable over MCP / raw `/api` (`attach_tm_source`,
`pin_glossary` - see "Local-only capabilities" below). So an exit-2 refusal on the
CLI is a signal to check for an `/api`+MCP path, not a verdict that the capability is
unreachable. When it does not, it is a plain usage error - fix the invocation (most
often the flag order) and re-issue. Exit codes: `0` ok, `1` operation failure, `2`
usage error, `3` remote unreachable.

## Graceful degradation - the honest failure modes

### Engine down - report it LOUDLY, never downgrade to mock
On some deployments the local engine rides a tailnet model server (the transport is
deployment-specific), and an outage is now a **loud error**, not a silent
fall-through. When a live engine fails or every target would land on the mock (the
409):
- **Report the failure honestly.** Do not silently produce MockProvider echoes and
  present them as translations.
- Suggest **retry-later**, or an **alternate engine the user's role can select**
  (a step/project provider override, if their role allows it).
- If the engine is down because **no provider is configured at all**, that is a
  **configuration** matter you cannot fix - name the switch/credential the operator
  must set (see below), do not look for a back door.

### Missing scope or role - name the grant, do not retry
On a 403, tell the user **exactly** what is missing: the specific scope (`docs` /
`keys`) to add to their key, or the role rung the action needs, and point them at
**`/account`** to mint a correctly-scoped key or to the admin who can adjust the
role. **Do not retry** - the grant will not appear by retrying.

### Local-only capabilities - the honest "who and how" answer
Some things **no API key of any kind reaches**, by design - *an API key changes
content, never configuration.* When asked for one, give the honest answer naming
the surface and the role, and **do not fake it**:
- **Settings / kill switches / provider credentials** → local CLI (`config set
  ...`) or the Settings UI, host-only. Name the exact switch (e.g.
  `key_projects_enabled`, `image_localization_enabled`, `spend_cap_usd`).
- **Workflow step editing on a pre-authoring install** → `workflow add-step` /
  `edit-step` (in-process CLI or web) until the authoring surface
  (`SPEC-workflow-authoring-api`, present on current builds) is confirmed in the
  connected install; verify via the live card / `/openapi.json` per SKILL §0.
- **Writing delivered translations back into TM** → approve the segment:
  `approve_segment` (MCP) / `POST /api/segments/{sid}/approve` / `doc approve-segment`
  (approval writes the pair to TM). The refusal/language guards run server-side, so a
  hedge/non-translation, or a target that drops or adds a placeholder like `{count}`, is
  withheld (`tm_written:false` means approved-but-not-seeded; pass
  `acknowledge_placeholder_loss` to save a placeholder loss anyway).
  Bulk-seeding from an external TMX is still `POST /api/projects/{pid}/tm/import`.
  *(Attaching a TM **source** and pinning a glossary are a different thing and also have
  an operator path - see the note just below.)*
- **Launching a run over a key project** → the **web key grid**; no CLI/API
  equivalent yet.
- **Anything destructive over MCP** - delete, human-review sign-off, connector write - is **absent from the tool list
  entirely**. Hand these to a human on the web UI or the local CLI with the right
  role.

In every one of these, the correct move is the **honest "host-only, here is who and
how"** answer. A confident fake is the single worst failure mode for a driver of
this system.

**Not on the local-only list any more - these DO have an operator path** (don't
refuse them as host-only, a rot the earlier skill carried): **attaching another
project's TM as a read-only source** → `attach_tm_source` / `POST
/api/projects/{pid}/tm-sources`; **pinning an approved module glossary to a
project** → `pin_glossary` / `POST /api/projects/{pid}/glossaries`. Both are write
tier, `docs` scope, `can_tune_ai`. Only the **repo CLI** verbs (`project tm-sources
add`, `project glossaries add`) are in-process and refuse over `--remote` - so reach
these over MCP or raw `/api`, not the remote CLI.
