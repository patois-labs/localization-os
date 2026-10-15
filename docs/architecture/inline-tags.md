# Inline-tag integrity (mask, repair, quarantine): Architecture (L1)

## 1. Role

This subsystem is the **inline-tag protection ladder**: keeping the opaque formatting tokens that
carry a document's inline structure intact across a model call, and repairing or quarantining a
target when they are damaged.

The document filters replace mid-paragraph run formatting and inline elements with opaque tokens
before text reaches MT. A small model handed those tokens raw routinely renumbers, reorders,
translates or drops them, which collapses whole-paragraph formatting at export. The ladder is:
PREVENT, then DETECT, then REPAIR CHEAPLY, then REPAIR WITH AN LLM, then QUARANTINE. Prevention
(`app/services/ai_tagging.py`) is the mask/restore half: before a call every token is replaced by an
opaque sentinel the model has no reason to touch, and after the call it is restored. Detection lives
in the write choke point (`quality.tag_damage`, documented in quality-lqa-evaluations).

Cheap deterministic repair (`app/services/tag_repair.py`) handles the two mechanically-safe
corruption classes (surplus duplicates, point-token order) and returns a candidate only when its
tokens structurally re-verify against the source (same multiset, same order, well formed, no leaked
sentinel). A de-duplication is certified clean; a point-token reorder is a best guess, so its text
is committed but the segment keeps the `tag_integrity` flag and NEEDS_REVIEW. Repair therefore can
only turn a would-be quarantine into a commit, never lower the guarantee. The LLM repair rung
(`app/services/tag_llm_repair.py`) is the last step before quarantine for a genuinely dropped or
misplaced paired span: it hands the damage to a model with an explicit per-token description and
re-verifies with votes. The batch re-verification CLI (`app/cli/cmd_tagging.py`) sweeps already
stored targets with the same deterministic verifier and flags the damaged ones for review, a dry
run by default that never auto-rewrites text.

It does not own the write choke point or damage detector itself (quality-lqa-evaluations), the
segmentation that extracts the tokens (segmentation), or the provider seam the LLM rung calls
(engines-and-providers).

## 2. Invariants

- **Tokens are masked to opaque sentinels before a call and restored after.**
  `app/services/ai_tagging.py::mask` replaces each token with a fresh-nonce sentinel and
  `app/services/ai_tagging.py::restore` reverses it, with
  `app/services/ai_tagging.py::has_residual_sentinel` catching a leak. Enforced by
  `verify_ai_tagging`.
- **Deterministic repair returns only a candidate that structurally re-verifies.**
  `app/services/tag_repair.py::repair` re-checks its own output and returns None (the caller
  quarantines) rather than a candidate that does not certify. It returns `(text, clean)`: a
  de-dup is `clean=True`; a point-token reorder is `clean=False` (best guess), so the caller still
  flags it for review. Paired tokens are never reordered. Enforced by `verify_ai_tagging`.
- **A placeholder that names its argument may move (#693).** Filters write it as the free-order
  point kind `⟦pN⟧` (`app/services/segment.py::ph_free`): Apple and printf `%1$@`, Windows `%1`,
  `{name}` / `{0}`, Python `%(name)s`, Laravel `:name`, XLIFF `<x id>` / `<ph id>`. Build maps it
  back by number, so the target's order is safe. `app/services/segment.py::same_token_order`
  judges order only over the other tokens (drops, adds and duplicates still count); unnumbered
  `%@` / `%s`, tags and begin/end codes keep `⟦xN⟧` and their order. Older imports keep `⟦xN⟧`.
  Enforced by `verify_qa_reorder_names_693`.
- **The LLM repair rung is the last step before quarantine and re-verifies with votes.**
  `app/services/tag_llm_repair.py::repair` and `app/services/tag_llm_repair.py::verify_votes`
  re-verify a candidate before it is accepted: a structural lock, a body-preservation lock (no word
  may change), then independent placement verification where every vote must agree. A repair that
  skips or fails placement verification commits flagged, never clean. Enforced by
  `verify_tag_llm_repair`.
- **The batch sweep is a dry run by default and never auto-rewrites text.**
  `app/cli/cmd_tagging.py::register` runs the same deterministic verifier the write gate uses and
  only flags damaged segments unless `--apply` is passed. Enforced by `verify_ai_tagging`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Mask / restore | `app/services/ai_tagging.py::mask` · `app/services/ai_tagging.py::restore` · `app/services/ai_tagging.py::has_residual_sentinel` | Replace tokens with opaque sentinels before a call and restore them after. |
| Deterministic repair | `app/services/tag_repair.py::repair` | The cheap rung that fixes surplus duplicates and point-token order, re-verified. |
| LLM repair rung | `app/services/tag_llm_repair.py::repair` · `app/services/tag_llm_repair.py::verify_votes` · `app/services/tag_llm_repair.py::describe_damage` · `app/services/tag_llm_repair.py::repair_stored_segments` | The last-before-quarantine rung for dropped/misplaced paired spans; `repair_stored_segments` is the on-demand seam shared by the CLI verb and the editor's repair action. |
| Batch re-verification + repair | `app/cli/cmd_tagging.py::register` | The `tagging verify` sweep that flags stored damaged targets and the `tagging repair` verb that attempts an AI repair (dry-run unless `--apply`). |

## 4. Data & settings

- **Models:** damage is recorded as a `tag_integrity` qa_flag on `app/models.py::Segment` and routes
  the segment to needs-review; there is no separate table.
- **Settings:** `setting:ai_tagging_enabled` (default on) governs only the mask/restore layer;
  detection and deterministic repair are always on and never consult it.
  `setting:tag_llm_repair_enabled` (default off, shipped dark pending review findings) gates the LLM
  repair rung on `app/services/tag_llm_repair.py`, independent of the masking switch.
  `setting:tag_llm_repair_verify_enabled` (default on) turns the placement-verification call on (off
  means every LLM repair commits flagged), `setting:tag_llm_repair_verify_votes` (default 2, clamped
  1 to 5) is how many verdicts must agree, and `setting:tag_llm_repair_verify_provider` (blank means
  the repair engine) names a stronger judge for the on-demand surfaces.
- **Run cost (#596):** every billed repair and placement-verify call of a workflow run, accepted or
  rejected, is added to the run's last cost-bearing StepRun (cost and tokens) in the terminal
  transaction, or by the run wrapper's finalizer when the run fails, crashes or is cancelled after
  paying for repairs. Run cost, project budgets and reports therefore include accepted repairs too
  (they did not before). Spend gates read the ledger, which was already right.

## 5. Enforced by

- `verify_ai_tagging`: the mask/restore round trip, the deterministic repair contract, and the
  batch sweep.
- `verify_tag_llm_repair`: the LLM repair rung and its vote re-verification.

## 6. Battle scars

- **A dropped token collapses whole-paragraph formatting, so repair may never lower the guarantee.**
  Every repair candidate is re-verified by the same gate that requested it, so a repair either
  certifies clean or the segment quarantines, recorded in the `app/services/tag_repair.py` module
  docstring.
- **Already-polluted targets were not re-checked until re-translated.** The batch sweep runs the
  same verifier over stored targets and flags the damaged ones for review, a forward-and-backward
  pass recorded in the `app/cli/cmd_tagging.py` module docstring.

## 7. Limits & accepted risks

- **Deterministic repair covers only mechanically-safe classes.** A genuinely dropped or misplaced
  paired span has no deterministic fix and falls to the LLM rung or quarantine, stated in the
  `app/services/tag_repair.py` module docstring.
- **`tagging verify` flags but never rewrites; `tagging repair` is the opt-in AI repair.** The
  verify sweep with `--apply` writes a `tag_integrity` flag and NEEDS_REVIEW rather than rewriting
  text, while the separate `tagging repair` verb attempts an AI repair of broken inline-formatting
  tokens on damaged segments and persists it only with `--apply` (dry-run by default), per
  `app/cli/cmd_tagging.py`. Even a dry run calls the provider, so the verb only previews the damaged
  segments and a cost estimate until `--yes` confirms it, it refuses when
  `tag_llm_repair_enabled` is off, and it is in-process only (no `--remote`).
