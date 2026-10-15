# Reporting and dashboard: Architecture (L1)

## 1. Role

This subsystem is the **read-only analytics surface**: the CSV report builder (cost, tokens,
volume, QA, post-edit) and the operator dashboard, help and favicon pages, plus the dashboard query
helpers that assemble those pages.

The report service (`app/services/reports.py`) is the one source of truth the web `/reports`
builder, the in-process `report run` CLI and the remote `/api/reports/run` endpoint all call, so a
report prints byte-identical numbers on every surface. It builds each report kind from existing data
(jobs, step runs, LQA results, post-edit rows), scopes every kind by the same confined project set,
and supports the standard axes and group-bys. Two quality/effort kinds (QA MQM findings and
post-edit effort) are dispatched by isolated paths because their columns differ from the job-loop
kinds.

The dashboard, reports and help routes (`app/web/home.py`) render the operator landing surface, run
a report from the web builder, count the live API endpoints for the help page, and serve the
favicon. The dashboard query helpers (`app/web/queries.py`) are the shared read helpers those
router modules use to assemble project stats and the report picker context.

It does not own the underlying run/LQA/post-edit rows it reads (translation-pipeline,
quality-lqa-evaluations, workflow-engine), the spend cap or budgets a cost report reflects
(cost-and-budgets), or the CLI dispatch machinery (surfaces-and-parity).

## 2. Invariants

- **A report is one service; every surface prints identical numbers.**
  `app/services/reports.py::run_report` is called by the web builder, the CLI and `/api` alike, so
  the three cannot diverge. Enforced by `verify_reports`.
- **Every report kind is scoped by the same confined project set.**
  `app/services/reports_the_query.py::allowed_project_ids` is the one ring every kind filters through, so a
  shared-host operator cannot sweep another org. Enforced by `verify_reports` and `verify_multiorg`.
- **An unknown report kind or unsupported axis is rejected.**
  `app/services/reports_the_query.py::validate_kind` and the supported-axis check reject a bad request rather
  than returning a wrong-shaped result. Enforced by `verify_reports`.
- **The dashboard, project list and `/reports` count in SQL, not in Python.**
  `app/web/queries.py::_dashboard_data` and `app/web/queries.py::_project_rows` use COUNT and GROUP BY
  with every IN list chunked at 150, and hydrate only the newest eight runs, instead of loading every
  segment, run, TM, step and finding row to bucket it (#514). A locale that is still being added
  (a half-cloned language, `app/services/locale_seed.py::seeding_pairs`) is subtracted from the
  counts so a dashboard never shows a language that is not finished (#583). Enforced by
  `verify_dashboard_sql_counts`.
- **The quality/effort kinds are dispatched by isolated paths.**
  The QA and post-edit kinds in `app/services/reports_the_query.py::KINDS` have their own column sets separate
  from the job-loop kinds. Enforced by `verify_reports`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Report service | `app/services/reports.py::run_report` · `app/services/reports_the_query.py::validate_kind` · `app/services/reports_the_query.py::KINDS` · `app/services/reports_the_query.py::allowed_project_ids` · `app/services/reports_csv.py::_METRIC_KEYS` · `app/services/reports_part3.py::_workflow_run_buckets` · `app/services/reports_the_query_leaf.py::_QA_QUERY_COLS` | The one CSV report engine, its kinds, validation and scoping ring. |
| Dashboard + help web | `app/web/home.py::dashboard` · `app/web/home.py::reports` · `app/web/home.py::help_page` · `app/web/home.py::api_endpoint_count` | The landing dashboard, the web report builder, and the help page. |
| Dashboard query helpers | `app/web/queries.py::_dashboard_data` · `app/web/queries.py::_project_rows` | The shared read helpers that assemble project stats and picker context. |
| CLI surface | `app/cli/cmd_report.py::register` | The `report` command group (run/kinds). |

## 4. Data & settings

- **Models:** the reports read existing rows (`app/models.py::StepRun`, `app/models.py::LQAResult`,
  `app/models.py::SegmentPostEdit` and the job rows) rather than owning a table of their own.
- **Settings:** reporting has no dedicated install setting; scoping is the multi-org gate and the
  confined project ring, and cost figures reflect the ledger documented in cost-and-budgets.
- **Help page:** `setting:accessibility_contact` is the "How to report a problem" contact in the
  help page's Accessibility section (a11y T10). `app/services/settings.py::accessibility_contact`
  returns it as a mailto link, an https link or blank ("Contact your administrator"), and treats a
  stored value that fails `app/services/settings.py::accessibility_contact_kind` as blank, so an
  unvalidated value never becomes a link. Every write door (the web Security page, `config set`,
  /api config) runs the same validator.

## 5. Enforced by

- `verify_reports`: the one-service parity, the confined scoping, the kind/axis validation, and the
  isolated quality/effort paths.
- `verify_multiorg`: the org confinement of every report kind.
- `verify_dashboard_sql_counts`: the SQL counts equal a load-everything oracle for unrestricted and
  restricted callers, and no row classes are hydrated.
- `verify_a11y_contact`: the accessibility contact validator on every write door, its rendering
  in the help page's Accessibility section, and a pin for every keyboard shortcut that section
  documents.

## 6. Battle scars

- **Reports had to be one service so the numbers never diverge.** The web builder, the CLI and
  `/api` all call one function, so a cost or volume figure is byte-identical across surfaces,
  recorded in the `app/services/reports.py`
  module docstring.
- **The quality and effort kinds needed isolated dispatch.** Their columns differ from the job-loop
  kinds, so they are dispatched separately while sharing the same confined project set, recorded in
  the `app/services/reports.py` module docstring.

## 7. Limits & accepted risks

- **Reports are read-only over existing data.** They never write and never fabricate a zero for
  untracked spend; a report reflects what the ledger and rows already hold, stated in the
  `app/services/reports.py` module docstring.
- **The dashboard query helpers hold no business logic of their own.** They are shared read helpers
  for the router modules, stated in the `app/web/queries.py` module docstring.
