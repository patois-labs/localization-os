---
module: reports
title: Reports
summary: Operational cost, token, volume, qa and postedit reporting over the data you can already see - a pipeline-health dashboard plus a report builder that groups the numbers the way you ask (by customer, project, provider, engine, date, workflow, locale, vendor, dimension or actor) and exports the table as CSV. It reports on existing runs; it never triggers work and never moves money.
nouns:
  Report kind: "What the report measures. Five kinds: cost (money - AI model spend and vendor Human spend, side by side), tokens (input / output / cache-read / cache-write counts), volume (jobs, segments, words, characters), qa (MQM LQA findings - a dimension-by-severity cross-tab with per-card scores), and postedit (edit distance and post-editing effort - segments edited, episodes, char/word distance, percent, and edit time). You pick exactly one kind per report."
  Group-by: "How the rows are bucketed. For cost/tokens/volume: customer, project, provider, engine, date, workflow, locale, vendor, or none (a single total row). qa adds dimension (the MQM cross-tab) and actor (the machine source/judge); postedit adds actor (the human editor or vendor). The group-by column is prepended to the metric columns. An axis a kind does not support degrades to none with a caveat that names it."
  Metric column: "One measured number in a row. cost adds cost_usd (AI / model spend) and, last, human_cost_usd (vendor post-edit spend) as two separate columns - the AI-versus-Human split is never conflated into one figure. human_cost_usd appears only on the workflow axis, and only for an admin/operator; on other axes or for non-admin callers the column is dropped entirely. An AVERAGE column (avg_card_score, avg_distance_pct, avg_tte_ms, avg_lqa_score) renders BLANK, not 0, when nothing was measured - a 0 would read as a real measurement - and is never summed into the TOTALS row."
  Scope: "The slice of data a report covers, always intersected with what the caller may see. Optional filters narrow it further: a customer, a project, a provider, and a from / to date window."
  CSV export: "The same grouped table streamed as a text/csv attachment instead of rendered as HTML - the format a spreadsheet or a billing pipeline consumes."
  Pipeline-health dashboard: "The at-a-glance reports landing view, over the same in-scope data the builder queries."
verbs:
  See the reports: "Web: the Reports page (its nav link and dashboard quick-action appear only while the feature is on). CLI: `report kinds` lists the five kinds and the columns each one renders."
  Run a report: "Web: pick a kind, a group-by and any filters on the Reports form. CLI: `report run cost --group-by customer` (kind is cost | tokens | volume | qa | postedit; add --project, --customer, --provider, --from, --to to narrow the scope). The cost kind needs the manager role or higher (it exposes AI spend); the local CLI runs as the trusted operator, so it is allowed regardless of role. An unknown kind is rejected at the CLI door rather than silently coerced."
  Group the numbers: "Add --group-by (customer | project | provider | engine | date | workflow | locale | vendor | dimension | actor | none) to bucket the rows; none is a single total. qa/postedit are quality kinds - dimension is qa only, actor is qa and postedit. The actor axis needs the manager role or higher (it ranks named people - a postedit bucket key is an editor's email): on a remote /api call an under-ranked key is refused with a 403 EVEN WHEN the login wall is off (it exposes editor emails, so the gate is always-on there), while the local operator is trusted; the web form hides the option. An axis a kind does not support degrades to none with a caveat. Web: the Group by control on the form."
  Export as CSV: "Web: the Download CSV link on a run. CLI: `report run cost --group-by project -o costs.csv` writes the same grouped table as CSV to the path."
needs:
  - "The reports_enabled switch on. With it off the feature is REMOVED, not hidden: the /reports page and its GET /reports/run builder both return not-found (404), /api/reports 404s, the nav link and the dashboard quick-action disappear, the report CLI group refuses (`reports are disabled (setting reports_enabled=0).`), and the MCP report tool relays the same 404. Turn it back on and every surface returns - no data is affected either way, because a report only reads."
  - "The manager role (or higher) to run a cost report or the actor axis on a remote/web surface - the cost kind exposes AI model spend and the actor axis ranks named people by output. The actor axis exposes editor emails (PII), so its /api gate is ALWAYS-ON: an under-ranked docs-scoped key is refused even when the login wall is off (unlike the cost gate, which no-ops under auth-off single-tenant). tokens/volume/qa/postedit and every other axis stay viewer-readable within scope. The local CLI runs as the trusted operator and is allowed regardless of role."
  - "Existing runs to report on. A report is a read over the jobs already translated in scope; a fresh install with no runs reports zeros, not an error."
  - "Nothing else to set up. Cost figures reconcile with what actually accrued (the same pricing the spend cap uses), so a cost report needs no separate configuration."
limits:
  - "Read-only. A report never launches a run, never re-translates, and never moves money - it aggregates what already happened. Running one is always safe and always free."
  - "Every report is scoped to what the caller may see first, then narrowed by any filters. A report can never widen visibility past the caller's own access - a scoping miss would leak cost and volume across customers, so scope is applied before anything is grouped."
  - "cost_usd is AI (model) spend; human_cost_usd is vendor (Human post-edit) spend, appended as its own last column on the workflow axis for admins/operators (dropped elsewhere). The two are reported separately on purpose and never summed into a single opaque number."
  - "The actor axis (qa/postedit) needs the manager role or higher on a remote or web surface (it ranks named people - a postedit bucket key is an editor's email); the local CLI operator is trusted. Because it exposes editor emails, the /api gate is ALWAYS-ON - an under-ranked key is refused even with the login wall off. The web form hides the option for an under-ranked ident, and a crafted request is refused with a 403 rather than silently rebucketed."
  - "The qa `cards` count counts LQA cards that HAVE findings - a clean, zero-finding card writes no rows - so it is not a count of cards scored, and avg_card_score is biased low. The counts also include accumulated workflow-step findings that survive a re-score, so a qa count can rise over time with no new scoring; and an ad-hoc re-score of a document+locale DESTROYS that pair's prior findings, so a count can also legitimately drop between two runs with no deletion visible. These are caveated on every surface."
  - "Reviewer-authored (human) findings are counted in `findings`, the severity columns and the `human` column, but are EXCLUDED from `cards` and `avg_card_score` (a hand-logged bug is not a scored card) and never change the machine LQA score or a workflow gate; a report fires a caveat when it contains any. A reviewer can WITHDRAW a finding: a withdrawn finding is excluded from every qa/vendor report bucket and from the LQA page, but the row itself is RETAINED (a soft delete, not a row delete)."
  - "postedit capture coverage is not uniform across surfaces. A full-text paragraph save is captured on web, /api and CLI; a vendor return is captured wherever it is admitted; but the segmented CAT editor save (POST /segments/{id}/save) is web-only and has no headless twin, so a segmented document edited only through a headless install produces no postedit data. A paragraph REVERT is deliberately not captured (it is a discard, not post-editing)."
  - "Reports read other modules' data (for example vendor spend by run) strictly as the consumer. Disabling Reports never touches those data sources, and disabling one of them does not break Reports beyond that source's own rows."
connects:
  - module: engines
    how: "Model (AI) spend on every run is what the cost and tokens reports aggregate; a report's cost column reconciles with the same per-run pricing the engine ledger and the spend cap use."
  - module: vendors
    how: "Vendor (Human) post-edit spend is reported alongside model spend as human_cost_usd - Reports reads vendor spend-by-run to render the AI-versus-Human split, as the consumer of that data."
  - module: documents
    how: "Volume and token counts roll up from the jobs that translated documents and keys, so a report is a grouped view over the same runs the document pipeline produced."
---

## What this is for

You need to answer operational questions about work that already ran: *what did
this month cost*, *how many words did we translate for this customer*, *which
provider is burning the most tokens*. Reports aggregates the numbers already
recorded on your runs and hands them back grouped the way you ask - on screen or as
a CSV your finance pipeline can load.

The important property is that **a report only reads**. It never launches a run,
never re-translates, and never moves money. Running one is safe, free, and
repeatable.

## The five kinds

- **cost** - money. Two separate columns: `cost_usd` (AI / model spend) and, last,
  `human_cost_usd` (vendor post-edit spend; only on `--group-by workflow`, and only
  for an admin/operator - dropped entirely elsewhere). The AI-versus-Human split is
  deliberate and never collapsed into one figure. Needs the manager role or higher on a
  remote/web surface.
- **tokens** - input, output, and cache read / write token counts.
- **volume** - jobs, segments, words, and characters.
- **qa** - MQM LQA findings, machine-scored AND reviewer-authored. Columns: `findings`, the
  five severity counts (`critical`, `major`, `minor`, `neutral`, `other`), `human` (the
  count of hand-logged reviewer findings in the bucket), `cards` (distinct scored cards with
  findings) and `avg_card_score`. A hand-logged human finding counts in `findings` and its
  severity column and in `human`, but NOT in `cards`/`avg_card_score` (it is not a scored
  card) and it never changes a workflow gate. Group by `dimension` for the cross-tab or by
  `actor`: a machine finding buckets on its source/judge, a human finding on the REVIEWER'S
  EMAIL - which is why the qa actor axis is PII and manager+ gated, exactly like postedit.
- **postedit** - post-editing effort. Columns: `segments_edited`, `episodes`, `edits`,
  `words_derived`, `mt_chars`, `final_chars`, `distance_char`, `distance_word`,
  `avg_distance_pct`, `tte_ms`, `avg_tte_ms`. On the `actor`/`vendor` axes the distance is
  each actor's own episode-local edit (not the cumulative machine-to-final figure), and the
  per-segment columns (`words_derived`, `tte_ms`, `avg_tte_ms`) are omitted so a segment
  touched by two actors is not double-counted.

## Grouping and scope

Pick one **group-by** - for cost/tokens/volume: `customer`, `project`, `provider`,
`engine`, `date`, `workflow`, `locale`, `vendor`, or `none` for a single total; qa adds
`dimension` and `actor`, postedit adds `actor` - and the rows bucket accordingly. `vendor`
is a dispatch-leg scorecard with its own columns (legs, word_count, returned-on-time, LQA
quality columns, and the admin-only human-spend money group: estimate-vs-actual over
returned legs, `open_estimate_usd` over in-flight legs, and `quoted_cost_usd` over approved
quotes). The `actor` axis needs the
manager role or higher on a remote/web surface - and because it exposes editor emails its
`/api` gate is always-on (refused even with the login wall off). An axis a kind does not
support degrades to `none` with a caveat that names it. Optional filters (a customer, a project, a provider, a
`from` / `to` date window) narrow the data further. Every report is intersected with **what
the caller may see** before anything is grouped, so a report can never widen visibility past
your own access.

## The normal path

1. **List the kinds** to see the columns each renders (`report kinds`).
2. **Run** one, grouped (`report run cost --group-by customer`), adding filters to
   narrow the scope.
3. **Export** the same table as CSV when a spreadsheet or billing pipeline needs it
   (`report run cost --group-by project -o costs.csv`, or the Download CSV link on
   the web run).

## When the switch is off

`reports_enabled = 0` **removes** the feature: the page, the builder, the `/api`
routes and the MCP tool all 404; the nav link and dashboard quick-action vanish; the
CLI refuses. No data is touched - flip it back on and every surface returns exactly
as it was.
