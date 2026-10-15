# Translation context and adaptation: Architecture (L1)

## 1. Role

This subsystem is the **prompt-context enrichment layer**: the Context Package a translation
carries, the in-run adaptive feedback and document brief, and the A/B harness that measures whether
context actually helps.

The Context Package service (`app/services/context_pkg.py`) is one seam called by web, CLI and API:
it validates and sanitizes an inbound context at ingestion, resolves the effective package from a
layered scope chain with per-field provenance (request, document override, project card,
customer-by-content-type card, content-type card, customer card, org default; most specific wins
field by field), and renders the one capped, injection-fenced prompt block. Adaptive enrichment (`app/services/adaptive.py`) is two default-off engine-layer upgrades
shared by both translate paths: an in-run rolling window of confirmed source/target pairs injected
as few-shot examples, and a document brief; when both are off every helper is a no-op and the prompt
is byte-identical.

The A/B harness (`app/services/context_ab.py`)
runs the same segments twice differing in exactly one thing (whether the context block reaches the
model) and reports a measured number rather than a marketing claim. It is a measurement only: it
never writes a segment, a status or the TM, and a mock run is labelled a plumbing test and never
claims an improvement.

It does not own the TM/glossary the context references (tm-and-glossary), the provider or prompt
assembly it feeds (engines-and-providers), the LLMemory store behind the memory block (llmemory), or
the style-guide A/B sibling (style-guides).

## 2. Invariants

- **The Context Package is one validated, resolved, fenced seam.**
  `app/services/context_pkg.py::validate_and_sanitize` sanitizes at ingestion,
  `app/services/context_pkg.py::resolve` resolves the layered effective package, and
  `app/services/context_pkg.py::render_block` renders the one capped, injection-fenced block.
  Enforced by `verify_context_pkg`.
- **Adaptive enrichment is default-off and byte-identical when off.**
  `app/services/adaptive.py::FeedbackBuffer` and `app/services/adaptive.py::build_document_brief`
  return empty/unchanged output when their toggles are off, so the assembled prompt is unchanged.
  Enforced by `verify_f2_engine`.
- **The context A/B toggles exactly one variable.**
  `app/services/context_ab.py::run_ab` runs the same segments twice differing only in whether the
  context block reaches the model, and `app/services/context_ab.py::estimate_ab` prices the same
  sample before any provider call (four calls per segment, the shared #531 estimate gate). Below a
  minimum sample or inside the noise floor the verdict says so instead of claiming a win. Enforced
  by `verify_context_ab`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Context Package seam | `app/services/context_pkg.py::validate_and_sanitize` · `app/services/context_pkg.py::resolve` · `app/services/context_pkg.py::render_block` | Validate, resolve the layered effective package, and render the fenced prompt block. |
| Adaptive enrichment | `app/services/adaptive.py::FeedbackBuffer` · `app/services/adaptive.py::build_document_brief` · `app/services/adaptive.py::brief_block` | The default-off in-run feedback window and the document brief. |
| Context A/B | `app/services/context_ab.py::run_ab` · `app/services/context_ab.py::estimate_ab` · `app/services/context_ab.py::clamp_max_segments` | The single-variable measurement of context lift and its pre-flight estimate. |
| Standing cards | `app/services/context_pkg.py::upsert_profile` · `app/services/context_pkg.py::delete_profile` · `app/services/context_pkg.py::set_document_context` | Write the scoped context cards and a document's override through the one seam. |
| CLI surface | `app/cli/cmd_context.py::register` | The `context` command group: `show`, `set-profile`, `delete-profile`, `set-document`, `show-document` and `ab`. |

## 4. Data & settings

- **Models:** `app/models.py::ContextProfile` (a standing context card at a scope) and the
  per-document override stored on `app/models.py::Document`. Adaptive feeds off already-confirmed
  `app/models.py::Segment` pairs.
- **Settings:** adaptive feedback and the
  document brief are the `adaptive_feedback`, `adaptive_feedback_window` (default 5, clamped) and
  `doc_context_prepass` columns on `app/models.py::WorkflowStep` on the orchestrator path and
  off-by-default parameters on the simple translate path rather than install settings. The brief
  pre-pass goes through the normal completion seam (so it counts against spend) and fails soft:
  a failed pre-pass translates without a brief rather than failing the job.

## 5. Enforced by

- `verify_context_pkg`: the validate/resolve/render seam and its provenance.
- `verify_f2_engine`: the default-off adaptive feedback and document brief.
- `verify_context_ab`: the single-variable context A/B harness.

## 6. Battle scars

- **Context had to prove its lift with a number, not a claim.** The A/B harness runs identical
  segments with and without the context block precisely so the feature ships a measured result,
  recorded in the `app/services/context_ab.py` module
  docstring.
- **Adaptive enrichment must not change a prompt when off.** Both features are default-off and every
  helper is a no-op when off, so enabling them is the only thing that changes a prompt, recorded in
  the `app/services/adaptive.py` module docstring.

## 7. Limits & accepted risks
- No known limits beyond those stated in the sections above.
