# Collaboration and review (queries, worklist, approval): Architecture (L1)

## 1. Role

This subsystem is the **human collaboration layer over translation work**: threaded queries and
comments anchored to a unit, the cross-project reviewer worklist ("My work"), and the one seam that
approves a reviewed segment and writes its pair to TM.

Threaded queries (`app/services/query_threads.py`) are the one truth for the query entity: every
surface (web, `/api`, CLI, MCP) and the vendor round trip are thin callers, and nothing else writes
the thread and post tables outside the cascade-delete and GDPR paths. A thread's tenant anchor is
its project id, the only column the access ring speaks, so every read and write funnels through that
ring and an out-of-scope thread is a 404, never a 403.

The worklist (`app/services/worklist.py`) is one service with three sections and one scoping ring:
review (paused steps whose definition names my email), query (threads assigned to me), and quality
(recent needs-review items). Each section is defined on data that actually exists, and the query
section disappears when its kill switch is off. Segment approval (`app/services/segment_review.py`)
is the one seam behind the CAT "approve" action on every surface: it applies the refusal and
language guards and writes the reviewed pair to TM, so approving cannot seed shared TM from retired
content.

It does not own the CAT editor itself, the TM the approval writes into (tm-and-glossary), the
external vendor dispatch a query can attach to (vendor-outsourcing), or the run engine that pauses a
step for review (workflow-engine).

## 2. Invariants

- **The query entity has one service; nothing else writes its tables.**
  `app/services/query_threads.py::open_thread` / `app/services/query_threads.py::add_post` are the
  writers, and a thread is anchored by its project id through the access ring. Enforced by
  `verify_queries`.
- **An out-of-scope thread is a 404, never a 403.**
  `app/services/query_threads.py::get_thread` and `app/services/query_threads.py::list_threads`
  filter on the tenant-anchor project id so a foreign thread is non-enumerable. Enforced by
  `verify_queries` and `verify_multiorg`.
- **The worklist is one service, three sections, one ring.**
  `app/services/worklist.py::my_work` resolves the allowed project ring once and builds the review,
  query and quality sections on data that exists. Enforced by `verify_worklist`.
- **The query section disappears when queries are off.**
  `app/services/worklist.py::my_work` drops the query section when `setting:queries_enabled` is off,
  while review and quality survive. Enforced by `verify_worklist`.
- **Approve-and-write-TM is one seam applying the same guards on every surface.**
  `app/services/segment_review.py::approve_and_write_tm` refuses a soft-deleted key's value and
  applies the refusal/language perimeter, so the guards cannot diverge across web, `/api`, CLI and
  MCP. Enforced by `verify_segment_approve_parity`. A target that drops or adds a real placeholder
  (`{count}`, `%s`) is approved but withheld from TM unless the caller acknowledges the loss
  (Issue #562, `verify_approve_placeholder_562`).
  Inline tags are stricter: a tagged source or a target whose inline tags differ from the source is
  never written to TM and no acknowledgement overrides it; every withhold names its reason
  (`tm_skipped`, `tm_skipped_code`) (#581).
- **A run parked on a human review step holds its document and locale.**
  `app/services/segment_review.py::refuse_if_awaiting_review` raises `AwaitingReviewError` (409,
  code `awaiting_review`) for every segment writer outside the run itself: editor save and approve,
  apply-TM, plain MT, TM unlock, inline-tag repair, image regions, FULL_TEXT paragraphs and key
  values, on web, `/api` (and so MCP) and the CLI, always after the visibility gate (#617 item 4).
  Flag-only safety sweeps (TM remediation quarantine, the tag scan) and key BASE text edits are not
  held. Enforced by `verify_decisions_617` section 6.
- **Approving a segment propagates to its repeats, in the same document only.**
  After the approval commits, `segment_review.propagate_repeats` copies the target onto every other
  segment with the same source (`qa._consistency_norm`, the key the INCONSISTENT check uses) and target
  locale in the same document that is not APPROVED, not human-EDITED, not locked (`lock_band`) and not
  unlinked (`meta.no_propagate`). A copy becomes EDITED (so MT re-runs cannot overwrite it) with
  `meta.propagated_from`, which the editor shows as a "Propagated" badge; it is never auto-approved
  (no new `SegStatus`: 35 write sites and every grid/export filter key on the existing five). A copy
  whose own source the text would break (inline tag, placeholder; an acknowledged placeholder loss
  carries over) is refused and listed in `propagation_refused`. One `segment.propagate` audit row lists
  the ids; `undo_propagation` restores the copies still exactly as propagated (previous text, status
  and flags are kept on the source's `meta.last_propagation`). Scope is the document because the
  INCONSISTENT check and the repetition band are document-level and TM already carries text across
  documents. Issue #617 item 6, enforced by `verify_repeat_propagation_617`.
  Race rule (review): the copies are read after the SQLite writer reservation is taken (FOR UPDATE on
  Postgres) and refreshed from the database, so a copy another linguist approved, edited, locked or
  unlinked a moment earlier is judged on its current state; the whole propagation is one commit.
  Undo uses the same rule and also redraws the editor (Undo repeats button; MCP
  `approve_segment propagation=undo|unlink|link`).
  Gate rules: the source row is locked and refreshed in the same id-ordered pass (a concurrent unlink
  of the source wins; Postgres locks in id order, so overlapping propagations queue instead of
  deadlocking). A copy counts as propagated only while `meta.propagated_digest` matches its text, so a
  writer that changes the text without clearing the marker turns it into ordinary text that is never
  overwritten. The two text writes live in the choke-point module (`quality.write_propagated_copy`,
  which runs the per-copy tag and placeholder gate, and `quality.restore_propagated_copy`, a
  compare-and-set), so `verify_refusal_choke_point` keeps its allowlist unchanged.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Query threads service | `app/services/query_threads.py::open_thread` · `app/services/query_threads.py::add_post` · `app/services/query_threads.py::list_threads` · `app/services/query_threads.py::record_vendor_post` | The one truth for threaded queries, tenant-anchored and RBAC-gated. |
| Worklist service | `app/services/worklist.py::my_work` · `app/services/worklist.py::my_open_query_count` | The three-section cross-project reviewer worklist over one ring. |
| Approve + write TM | `app/services/segment_review.py::approve_and_write_tm` | The one guarded seam behind the CAT approve action on every surface. |
| Human review hold | `app/services/segment_review.py::refuse_if_awaiting_review` | The 409 every other segment writer raises while a run waits on human review. |
| Query web surface | `app/web/query_threads.py::queries_list` · `app/web/query_threads.py::query_open` · `app/web/query_threads.py::query_post` | The `/queries` pages, thin callers of the service. |
| Worklist web surface | `app/web/worklist.py::my_work_page` | The `/my-work` page. |
| CLI surfaces | `app/cli/cmd_query.py::register` · `app/cli/cmd_work.py::register` | The `query` and `work` command groups. |

## 4. Data & settings

- **Models:** `app/models.py::QueryThread` (a thread anchored to a project) and
  `app/models.py::QueryPost` (a post in a thread). The worklist and approval read existing run,
  segment and LQA rows rather than owning tables.
- **Settings:** `setting:queries_enabled` gates the threaded-query feature (and the worklist's query
  section); `setting:worklist_enabled` gates the worklist surface.

## 5. Enforced by

- `verify_queries` and `verify_queries_surfaces`: the one-truth service, the tenant anchor, and the
  four surfaces.
- `verify_worklist`: the three sections, the one ring, and the queries-off section drop.
- `verify_segment_approve_parity`: the one approve-and-write-TM seam across surfaces.

## 6. Battle scars

- **Approve-and-write-TM once lived inline in the web route, so no other surface could save to TM.**
  It was extracted into one seam so `/api`, the CLI and MCP run the identical guards and the perimeter
  cannot diverge, recorded in the `app/services/segment_review.py` module docstring.
- **The worklist review section keys on the step definition, not a per-run assignment.** "Assigned
  to me" means every paused run whose current step definition names my email, pinned so nobody
  quietly changes it, recorded in the `app/services/worklist.py` module docstring.

## 7. Limits & accepted risks

- **A thread's tenant anchor is fixed at creation.** It is resolved once from the thread's scope and
  is the only column the access ring speaks, stated in the `app/services/query_threads.py` module
  docstring.
- **The cascade-delete and GDPR paths reach the tables directly.** They go through `app/models.py`,
  not this service, a documented fourth seam stated in the `app/services/query_threads.py` module
  docstring.
