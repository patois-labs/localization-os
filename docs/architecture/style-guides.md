# Style guides: Architecture (L1)

## 1. Role

This subsystem is the **style-guide language asset**: a first-class, approvable, customer-scoped
long-form style guide packaged into translation prompts, plus the A/B harness that measures its
lift.

The service (`app/services/style_guides.py`) is one seam the `/style-guides` web tab, the
`style-guide` CLI group and the `/api` endpoints all call. It owns StyleGuide CRUD with a
draft/approved approval lifecycle (a body or scope edit reverts an approved guide to draft; a rename
does not), scope resolution at the customer by content-type grain (optionally per-pair,
most-specific-wins), and the safe render of the one fenced, sanitized, budget-capped style-guide
prompt block. It is built on the same chassis the Context Package uses, because a style guide is a
scoped, layered, prompt-injected asset with the same layered-defense needs.

The A/B harness (`app/services/style_ab.py`) is the sibling of the context A/B harness: same
chassis, a different toggled asset. It runs the same segments through translation twice differing in
exactly one thing, whether the approved style-guide block reaches the model, so the guide's lift is
a measured number. The design note is explicit that a style guide dumped as prose is weak on its own
and its value is realized paired with verification; this harness is how that lift is proven.

It does not own the verification of style adherence at translation time (that is the LQA/QE style
gate in quality-lqa-evaluations), the prompt assembly it plugs into (engines-and-providers), or the
context package it shares a chassis with (context-and-adaptation).

## 2. Invariants

- **A body or scope edit reverts an approved guide to draft; a rename does not.**
  `app/services/style_guides.py::set_body` / `app/services/style_guides.py::set_scope` revert to
  draft and `app/services/style_guides.py::approve` is the one approval transition. Enforced by
  `verify_style_guides`.
- **Scope resolves most-specific-wins at the customer by content-type grain.**
  `app/services/style_guides.py::resolve_style_guide` picks the most specific applicable guide,
  optionally per-pair. Enforced by `verify_style_guides`.
- **The rendered block is fenced, sanitized and budget-capped.**
  `app/services/style_guides.py::render_style_guide` produces the one capped, injection-fenced
  `<style_guide>` block. Enforced by `verify_style_guides`.
- **A foreign org's or customer's guide 404s.**
  `app/services/style_guides.py::visible` is the one scope filter every surface fetches through.
  Enforced by `verify_style_guides` and `verify_multiorg`.
- **A customer-bound account cannot change a shared guide.** An org-level guide (no customer) is
  readable by every customer but writable only by unbound staff:
  `app/services/access.py::customer_write_blocks` refuses a bound caller's rename, body, scope,
  approve and delete with `app/services/access.py::SHARED_ASSET_READ_ONLY` (403) on web, `/api`,
  the remote CLI and MCP, and the style guide page hides the edit controls (#468; the same write ring
  covers global workflows, groups, prompt templates and language sets, see
  [identity and access](identity-and-access.md)). Enforced by `verify_shared_asset_write_ring`.
- **The A/B harness toggles exactly one variable.**
  `app/services/style_ab.py::run_ab` runs the same segments with and without the approved style-guide
  block and nothing else. Enforced by `verify_style_adherence`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Style-guide service | `app/services/style_guides.py::create` · `app/services/style_guides.py::approve` · `app/services/style_guides.py::resolve_style_guide` · `app/services/style_guides.py::render_style_guide` | CRUD, the approval lifecycle, scope resolution, and the fenced render. |
| Style A/B | `app/services/style_ab.py::run_ab` | The single-variable measurement of style-guide lift. |
| Web surface | `app/web/style_guides.py::style_guides_index` · `app/web/style_guides.py::style_guide_approve` | The `/style-guides` tab CRUD and approval. |
| CLI surface | `app/cli/cmd_style_guide.py::register` | The `style-guide` command group. |
| MT formality service | `app/services/formality.py::resolve_maps` · `app/services/formality.py::normalize_register` · `app/services/formality.py::write` | The register catalog, the document > project > customer resolver, and the one validated write path (section 5b). |
| MT formality surfaces | `app/web/formality_routes.py::api_formality_set` · `app/web/formality_routes.py::formality_set` · `app/cli/cmd_formality.py::register` | `/api/formality`, the page forms (`POST /formality`) and the `formality` command group. |

## 4. Data & settings

- **Models:** `app/models.py::StyleGuide` (a scoped, approvable long-form guide with its
  draft/approved status).
- **Settings:** style guides carry no dedicated install setting; scope and approval live on the
  guide row, and the rendered block reuses the prompt budgeting documented in engines-and-providers.

## 5. Enforced by

- `verify_style_guides`: CRUD, the approval lifecycle, scope resolution, the fenced render, and
  visibility scoping.
- `verify_style_adherence`: the single-variable style A/B harness.

## 5b. MT formality (#617 item 7, ADR 0062)

Formality (Sie vs du, vous vs tu, speech levels) is a setting per **target language** at three levels:
`document.formality`, `project.formality` and `customer.formality`, each a nullable JSON map
`{target_locale: register}`. `app/services/formality.py` owns the register catalog and the resolver:
document override, then project, then customer, then none; `auto` stops the chain; the exact locale
beats its language key. Values are the generic scale (`formal`, `informal`, `auto`) plus the language's
own registers; one validator (`normalize_register`) serves the web form (`POST /formality`), `/api/formality`,
the `formality` CLI group (local and `--remote`) and the `manage_formality` MCP tool, so an unknown
register for a language is a 400 everywhere. The resolved register is appended as ONE sentence to the
`<style_guide>` block (`render_style_guide(guide, formality)`), so every path that renders the guide
carries it: plain translate (summary event), workflows (full text and segmented; stamped on
`workflowrun.formality`), the LQA judge, vendor rework, Advanced (the `style.resolve` digest) and
`POST /api/translate/text` (request field or the key's customer default). Prompt-tap manifests carry a
`formality` key when one resolved. With nothing set, or `auto`, no prompt changes by a byte.
Who may write: `formality.can_set`: a customer default needs admin or manager, a project or
document entry admin, manager or linguist; the web form, the panel and `/api/formality` all ask it, after
the org / customer / project ring.

## 6. Battle scars

- **A style guide's value had to be proven, not asserted.** The A/B harness measures the lift of an
  approved guide precisely because prose dumped into a prompt is weak on its own, recorded in
  the `app/services/style_ab.py` module docstring.
- **An approved guide must not silently change under an edit.** A body or scope edit reverts to
  draft so an approved guide is always the reviewed text, recorded in the
  `app/services/style_guides.py` module docstring.

## 7. Limits & accepted risks

- **A style guide legitimately applies org-wide.** Unlike the customer-first glossary policy, a
  guide may be org-global (customer NULL), a documented scope choice stated in the
  `app/web/style_guides.py` module docstring.
- **Adherence verification is a separate build.** This subsystem packages the guide; verifying the
  translation honored it is the LQA/QE style gate in quality-lqa-evaluations.
