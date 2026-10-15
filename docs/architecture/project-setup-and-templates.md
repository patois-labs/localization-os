# Project setup and templates: Architecture (L1)

## 1. Role

This subsystem is **getting a project set up faster**: promptable authoring that turns a
plain-language description into a validated draft, reusable project templates, and the first-run
onboarding checklist.

Promptable setup (`app/services/setup_author.py`) is the propose-then-validate-untrusted pattern:
it resolves and spend-guards a provider, runs a completion for a structured draft, and VALIDATES
every field of the returned draft before anything is applied. It authors a project-creation draft
and a workflow draft; the human confirms the pre-filled form, and applying a workflow draft binds
each drafted step's provider through the real seams. It never writes on its own: the only writes are
the human's confirmation through the existing service seams. A hard JSON parse failure gets exactly
one repair retry, and a confirm-time validation failure can be handed back for one stateless
repair pass (`app/services/setup_author.py::repair_workflow_draft`) that merges onto the human's
current edits so nothing they typed is blanked.

Templates (`app/services/templates.py`) capture a project's setup (source and target locales,
default provider, default workflow, segmentation profile) as a one-time snapshot, optionally
customer-scoped, and instantiate a new project from it, re-validating every captured reference at
apply time and degrading a dangling reference to a printed warning rather than failing the
instantiation. Onboarding (`app/services/onboarding.py`) is the single source of truth for the
first-run checklist: eligibility (admin and linguist only, never with auth off), per-user dismissal
state, and the setup steps, exposed as a service seam so a surface reads the predicates rather than
re-deriving them. The step set is role-aware: an admin sees customer, project, workflow and provider;
a linguist sees only workflow and provider. Each step reads done from a real record in the caller's
visible scope, and the provider step is done only when the install's default provider is live (the
same test the app-shell keyless banner uses, #561); the offline mock never counts.

It does not own project/customer CRUD (content-and-projects), the workflow graph a drafted workflow
becomes (workflow-engine), the provider seam a draft binds (engines-and-providers), or the account
settings and UI shell (surfaces-and-parity).

## 2. Invariants

- **A drafted setup is validated before anything is applied, and the drafter never writes.**
  `app/services/setup_author.py::author_project_setup` and
  `app/services/setup_author.py::author_workflow_setup` return a validated draft; the only writes are
  the human's confirmation, and `app/services/setup_author.py::apply_workflow_draft` binds providers
  through the real seams. The apply re-validates the submitted form as untrusted input and creates
  nothing when any check fails; a field that is dead for its step type is stripped with a warning
  instead of rejected, and the one workflow it creates is ungrouped with no language pair.
  Enforced by `verify_promptable_setup`.
- **A template is a one-time snapshot; apply re-validates every reference.**
  `app/services/templates.py::apply_template` skips a dangling or foreign-customer reference with a
  warning rather than failing, and `app/services/templates.py::save_as_template` captures a snapshot
  with no versioning/propagation. A customer-bound caller can only save a customer-scoped template
  from its own customer's project, and every template read is org-confined through the shared
  visibility guard. Enforced by `verify_templates`.
- **A customer-scoped template is offered only for that customer.**
  `app/services/templates.py::list_templates` returns global plus own, never a foreign customer's.
  Enforced by `verify_templates`.
- **Onboarding eligibility and state have one authority.**
  `app/services/onboarding.py::is_eligible` (admin/linguist only) and
  `app/services/onboarding.py::steps_state` are the predicates any surface reads. Enforced by
  `verify_onboarding`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Promptable setup | `app/services/setup_author.py::author_project_setup` · `app/services/setup_author.py::author_workflow_setup` · `app/services/setup_author.py::apply_workflow_draft` · `app/services/setup_author_helpers.py::_today_anchor` · `app/services/setup_author_part2.py::_author_json` | Turn a description into a validated project/workflow draft the human confirms. |
| Templates | `app/services/templates.py::save_as_template` · `app/services/templates.py::apply_template` · `app/services/templates.py::prefill_draft` | Snapshot a project's setup and instantiate a new project from it. |
| Onboarding | `app/services/onboarding.py::is_eligible` · `app/services/onboarding.py::steps_state` · `app/services/onboarding.py::dashboard_panel` | The first-run checklist eligibility, state, and dashboard card. |
| CLI surface | `app/cli/cmd_template.py::register` | The `template` command group (create, list, show, apply, rename, delete). The `account reset-onboarding` re-arm verb lives in the `account` command group (`loc-tms account reset-onboarding`). |
| Onboarding reset | `app/services/onboarding.py::reset` · `app/services/onboarding.py::dismiss` | Stamp or clear the per-user dismissal flag (web dismiss, auto-dismiss when all done, CLI and API reset). |

## 4. Data & settings

- **Models:** templates capture a project snapshot (source/target locales, default provider,
  default workflow, segmentation profile); onboarding state is the per-user `onboarding_dismissed_at`
  flag. The applied artifacts are ordinary `app/models.py::Project` and workflow rows.
- **Settings:** promptable setup resolves a provider through the normal engine settings and is
  spend-guarded like any completion; no dedicated install setting gates templates or onboarding.

## 5. Enforced by

- `verify_promptable_setup`: the validate-before-apply drafter and its no-write guarantee.
- `verify_templates`: the one-time snapshot, apply-time re-validation, and customer isolation.
- `verify_onboarding`: the eligibility, per-user state, and the role-aware setup steps.

## 6. Battle scars

- **The drafter must never be a hidden write path.** Promptable setup returns a draft and writes
  only on the human's confirmation through existing seams, so a model cannot invent and persist a
  configuration, recorded in the `app/services/setup_author.py` module docstring.
- **A template apply must degrade, not fail, on a dangling reference.** A captured workflow or
  segmentation id that no longer resolves is skipped with a warning so the instantiation still
  succeeds, recorded in the `app/services/templates.py` module docstring.

## 7. Limits & accepted risks

- **The onboarding checklist UX is web-only.** Parity is owed over the dismissal flag, not the
  visual overlay, a recorded parity exception stated in the `app/services/onboarding.py` module
  docstring.
- **A template is a snapshot with no propagation.** Editing a template does not update projects
  already instantiated from it, stated in the `app/services/templates.py` module docstring.
