"""The multi-agent workflow orchestrator — the heart of Localization OS.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, SEG_JOINER, EMPTY_DOCUMENT_MSG,
_mark_project_completed, _begin_run_envelope, _finalize_run_envelope, _mt_workers, ROLE_PRESETS,
DEFAULT_PROMPTS, _SYSTEM_HEAD, _SYSTEM_TAIL, _SYSTEM_NO_TOKENS_CLAUSE, _SYSTEM_RAW_CLAUSE,
_SYSTEM_MASKED_CLAUSE, _SYSTEM_RAW, _SYSTEM_MASKED, _SYSTEM_NO_TOKENS, _SYSTEM, _locale_phrase,
_SINGLE_UNIT_INSTRUCTION, _looks_like_unit_translation, _over_translation_guard_on,
_LQA_FINDINGS_PH, _LQA_FINDINGS_PREAMBLE, HYGIENE_NOTE, _references_prev_lqa, _is_lqa_onfail_target,
_memory_mode_off, _image_eligible_step, _adjudicated_count, _lqa_judge_unavailable, _end_run_cursor,
_LQA_PRIOR_VERDICT_LIMIT, _PriorVerdict, _lqa_no_progress, _lqa_no_progress_error,
GATE_HOLD_WARNING_CODE, GATE_HOLD_NO_PROGRESS, GATE_HOLD_LOOP_EXHAUSTED, GATE_HOLD_VISIT_CAP,
GATE_HOLD_UNREPAIRED, GATE_HOLD_JUDGE_UNAVAILABLE, _is_gate_step, VISIT_CAP_SKIP_WARNING_CODE,
_record_visit_cap_skip, _gate_hold_of, _GATE_FAMILY_LQA, _GATE_FAMILY_QE, _DEFAULT_VERDICT_SCOPE,
_verdict_family, _DeletedGateStep, _lqa_shape_kwargs, _union_seg_terms, _FIVE_KEYS, _five_key,
_any_render_toggle, _int_or, _ft_segment_id_for_stamp, LQA_FINDINGS_SEGMENT_CAP, _QUOTED_RE,
_FINDING_LINE_RE, _findings_relevant, _fragment_occurs, _STRUCTURAL_ERROR_RE,
_is_structural_error, _FINDINGS_TAIL,
_cap_keep_fence, _lang_guard_for, _inject_missing_prefixes, _notify_human_review_paused,
_lang_directive, get_steps, _latest_paused_steprun, ResumeMismatch, UnderTranslationError,
_resolved_glossary_id, _DRAFT_FENCE_RE, _DRAFT_PREAMBLE_RE, _clean_draft, _source_text,
_split_for_chunks, _render, _spec_for_step, _qe_spec_for_step, _new_qe_stats, DEFAULT_MAX_VISITS,
DEFAULT_TOTAL_STEP_CAP, _int_setting, _step_ref, _has_graph, _has_parallel, _group_at, _key_index,
_RC_REFS, _RC_OPS, _RC_STEP_PREFIX, _is_step_ref, KEY_TRASH_RUN_REFUSAL, _refuse_key_trash_document,
_refuse_archived, _UNSET, _StepDirective, _load_segments,
ProjectBudgetExhausted, _IN_FLIGHT_RUN_STATUSES, QUEUE_LAUNCH_FAILED_NOTE, new_queue_group,
queued_runs, _null_scope, _bookkeeping_session, RUN_THREAD_PREFIX, _run_thread_name,
_RUN_CREATE_LOCK, RESTART_NOTE, STALE_RUN_AFTER, STALE_NOTE, STALE_NOTE_NO_SUCCESSOR,
RECOVERY_ACTIVITY_GRACE, SUPERSEDED_ABORT_NOTE, WRITE_CONTENTION_NOTE, _crash_note,
_crash_terminal_note, _stamp_note,
_progress_pool_has_slot, _commit_run_terminal_direct, _mark_execution_start, _last_activity_at,
_TERMINAL, _step_event, _latch_seg_shape, _observer_session, _is_correction_loop_step,
_prior_lqa_verdicts, _gate_hold_message, _standing_gate_verdicts, _last_gate_judged_text,
_release_scope, _lqa_step_error, _lqa_event_payload, _render_cfg_for, _render_glossary_block,
_bound_findings_block, _resolve_step_template, document_has_no_content, _empty_document_run,
_paused_blob, repark_run_at_external_step, paused_review, _capture_resume_edits, author_prompt,
RunGuard, _spend_block_reason, _latest_outcome_steprun, _seg_chunks, _refuse_project_budget,
_queue_actor_scope, _mark_queue_launch_failed, _sweep_stale_at_capacity, _run_thread_alive,
_write_progress_row, _abort_if_superseded, _commit_run_terminal, _is_stale_run, CANCEL_NOTE,
_CANCELLABLE_RUN_STATUSES, RunCancelError, _replay_done_text, _system_for, _memory_eligible_step,
_record_gate_hold, _run_gate_outcome, _findings_suffix, prepare_resume_seed, _branch_cursor,
_visit_cap_decision, _eval_run_condition, _refuse_launch_guards, REASON_RESTART, REASON_STALE,
REASON_CANCELLED, REASON_WRITE_CONTENTION, REASON_SUPERSEDED_ABORT, REASON_CRASH,
REASON_POOL_EXHAUSTED, REASON_QUEUE_LAUNCH_FAILED, SPEND_CAP_STOP_NOTE, FAIL_FAST_STOP_NOTE,
REASON_SPEND_CAP_STOP, REASON_FAIL_FAST_STOP, _REASON_PATTERNS, _reason_patterns,
_failure_reason, _DELETED_GATE_STEP,
_terminal_gate_hold, _may_clear_gate_hold, _next_cursor, PROGRESS_MIN_INTERVAL_S, _progress_state,
_progress_lock, _progress_dirty, _progress_wake, _progress_idle, _progress_writer_lock,
_progress_writer_thread, PROGRESS_WRITER_THREAD_NAME, _progress_snapshot, _persist_progress,
_progress_writer_loop, _progress_writer_start, recover_interrupted_runs, cancel_run,
_run_parallel_group, _gate_release_mask_in_scope, _run_step_on_segment, run_reason, run_progress,
progress_drain, _flush_progress, _bump_run_progress, _progress_forget, _execute_step,
publish_run_progress, _gate_release_mask, _ProgressTicker, observe_run from
app.services.orchestrator_terminology_backen, app.services.orchestrator_part2,
app.services.orchestrator_part3, app.services.orchestrator_part4, app.services.orchestrator_part5,
app.services.orchestrator_part6, app.services.orchestrator_part7, app.services.orchestrator_part8,
app.services.orchestrator_part9, app.services.orchestrator_part10.

A Workflow is an ordered list of user-defined steps. Each step is one of:
  - llm_agent    : a freeform agent (translator, in-country reviewer, technical
                   reviewer, finalizer, …) — its prompt is whatever you write
  - lqa          : a structured QA reviewer (Chunk 7 swaps in MQM/DQF scoring;
                   here it behaves as a corrective review agent)
  - human_review : pauses the run and hands the document to the CAT editor;
                   resuming continues the remaining steps with the edited text

Execution passes each step's output to the next, injects TM/glossary/context into
prompts, auto-chunks oversized documents (and reassembles), runs with retry +
failover (via run_completion), and records a WorkflowRun + per-step StepRun trace.

Prompt placeholders available to every step:
  {text}  the content to process (source for step 1, prior output afterwards)
  {prior} alias of {text}
  {source} the original source text (full, unchunked)
  {source_locale} {target_locale} {glossary}

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import or_
from sqlmodel import Session, select

from app import config
from app import db as _db
from app.db import POOL_EXHAUSTED_NOTE
from app.models import (
    Customer,
    Document,
    LQAResult,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services.orchestrator_part2 import (
    _DRAFT_FENCE_RE,
    _DRAFT_PREAMBLE_RE,
    _IN_FLIGHT_RUN_STATUSES,
    _RC_OPS,
    _RC_REFS,
    _RC_STEP_PREFIX,
    _RUN_CREATE_LOCK,
    _TERMINAL,
    _UNSET,
    DEFAULT_MAX_VISITS,
    DEFAULT_TOTAL_STEP_CAP,
    KEY_TRASH_RUN_REFUSAL,
    QUEUE_LAUNCH_FAILED_NOTE,
    RECOVERY_ACTIVITY_GRACE,
    RESTART_NOTE,
    RUN_THREAD_PREFIX,
    STALE_NOTE,
    STALE_NOTE_NO_SUCCESSOR,
    STALE_RUN_AFTER,
    SUPERSEDED_ABORT_NOTE,
    WRITE_CONTENTION_NOTE,
    ProjectBudgetExhausted,
    ResumeMismatch,
    UnderTranslationError,
    _bookkeeping_session,
    _clean_draft,
    _commit_run_terminal_direct,
    _crash_note,
    _crash_terminal_note,
    _group_at,
    _has_graph,
    _has_parallel,
    _int_setting,
    _is_step_ref,
    _key_index,
    _lang_directive,
    _last_activity_at,
    _latch_seg_shape,
    _latest_paused_steprun,
    _load_segments,
    _mark_execution_start,
    _new_qe_stats,
    _notify_human_review_paused,
    _null_scope,
    _observer_session,
    _progress_pool_has_slot,
    _qe_spec_for_step,
    _refuse_archived,
    _refuse_key_trash_document,
    _render,
    _resolved_glossary_id,
    _run_thread_name,
    _source_text,
    _spec_for_step,
    _split_for_chunks,
    _stamp_note,
    _step_event,
    _step_ref,
    _StepDirective,
    get_steps,
    new_queue_group,
    queued_runs,
)
from app.services.orchestrator_part3 import (
    _bound_findings_block,
    _capture_resume_edits,
    _empty_document_run,
    _gate_hold_message,
    _is_correction_loop_step,
    _last_gate_judged_text,
    _lqa_event_payload,
    _lqa_step_error,
    _paused_blob,
    _prior_lqa_verdicts,
    _release_scope,
    _render_cfg_for,
    _render_glossary_block,
    _resolve_step_template,
    _standing_gate_verdicts,
    author_prompt,
    document_has_no_content,
    paused_review,
    repark_run_at_external_step,
    waiting_human_review,
)
from app.services.orchestrator_part4 import (
    _CANCELLABLE_RUN_STATUSES,
    CANCEL_NOTE,
    RunCancelError,
    RunGuard,
    _abort_if_superseded,
    _commit_run_terminal,
    _is_stale_run,
    _latest_outcome_steprun,
    _mark_queue_launch_failed,
    _queue_actor_scope,
    _refuse_project_budget,
    _register_tag_spend,
    _replay_done_text,
    _run_thread_alive,
    _seg_chunks,
    _settle_tag_repair_spend,
    _spend_block_reason,
    _stamp_tag_repair_spend,
    _sweep_stale_at_capacity,
    _write_progress_row,
)
from app.services.orchestrator_part5 import (
    _REASON_PATTERNS,
    FAIL_FAST_STOP_NOTE,
    REASON_CANCELLED,
    REASON_CRASH,
    REASON_FAIL_FAST_STOP,
    REASON_POOL_EXHAUSTED,
    REASON_QUEUE_LAUNCH_FAILED,
    REASON_RESTART,
    REASON_SPEND_CAP_STOP,
    REASON_STALE,
    REASON_SUPERSEDED_ABORT,
    REASON_WRITE_CONTENTION,
    SPEND_CAP_STOP_NOTE,
    _branch_cursor,
    _eval_run_condition,
    _failure_reason,
    _findings_suffix,
    _memory_eligible_step,
    _normalize_resume_text,
    _reason_patterns,
    _record_gate_hold,
    _refuse_launch_guards,
    _run_gate_outcome,
    _system_for,
    _system_prompt_stamp,
    _visit_cap_decision,
    prepare_resume_seed,
)
from app.services.orchestrator_part6 import (
    _DELETED_GATE_STEP,
    PROGRESS_MIN_INTERVAL_S,
    PROGRESS_WRITER_THREAD_NAME,
    _may_clear_gate_hold,
    _next_cursor,
    _persist_progress,
    _progress_dirty,
    _progress_idle,
    _progress_lock,
    _progress_snapshot,
    _progress_state,
    _progress_wake,
    _progress_writer_lock,
    _progress_writer_loop,
    _progress_writer_start,
    _progress_writer_thread,
    _terminal_gate_hold,
    cancel_run,
    recover_interrupted_runs,
)
from app.services.orchestrator_part7 import _run_parallel_group
from app.services.orchestrator_part8 import (
    _bump_run_progress,
    _flush_progress,
    _gate_release_mask_in_scope,
    _progress_forget,
    _run_step_on_segment,
    progress_drain,
    run_progress,
    run_reason,
)
from app.services.orchestrator_part9 import _execute_step, publish_run_progress
from app.services.orchestrator_part10 import _gate_release_mask, _ProgressTicker, observe_run
from app.services.orchestrator_terminology_backen import (
    _DEFAULT_VERDICT_SCOPE,
    _FINDING_LINE_RE,
    _FINDINGS_TAIL,
    _FIVE_KEYS,
    _GATE_FAMILY_LQA,
    _GATE_FAMILY_QE,
    _LQA_FINDINGS_PH,
    _LQA_FINDINGS_PREAMBLE,
    _LQA_PRIOR_VERDICT_LIMIT,
    _QUOTED_RE,
    _SINGLE_UNIT_INSTRUCTION,
    _STRUCTURAL_ERROR_RE,
    _SYSTEM,
    _SYSTEM_HEAD,
    _SYSTEM_MASKED,
    _SYSTEM_MASKED_CLAUSE,
    _SYSTEM_NO_TOKENS,
    _SYSTEM_NO_TOKENS_CLAUSE,
    _SYSTEM_RAW,
    _SYSTEM_RAW_CLAUSE,
    _SYSTEM_TAIL,
    DEFAULT_PROMPTS,
    EMPTY_DOCUMENT_MSG,
    GATE_HOLD_JUDGE_UNAVAILABLE,
    GATE_HOLD_LOOP_EXHAUSTED,
    GATE_HOLD_NO_PROGRESS,
    GATE_HOLD_UNREPAIRED,
    GATE_HOLD_VISIT_CAP,
    GATE_HOLD_WARNING_CODE,
    HYGIENE_NOTE,
    LQA_FINDINGS_SEGMENT_CAP,
    ROLE_PRESETS,
    SEG_JOINER,
    VISIT_CAP_SKIP_WARNING_CODE,
    _adjudicated_count,
    _any_render_toggle,
    _begin_run_envelope,
    _cap_keep_fence,
    _DeletedGateStep,
    _end_run_cursor,
    _finalize_run_envelope,
    _findings_relevant,
    _five_key,
    _fragment_occurs,
    _ft_segment_id_for_stamp,
    _gate_hold_of,
    _image_eligible_step,
    _inject_missing_prefixes,
    _int_or,
    _is_gate_step,
    _is_lqa_onfail_target,
    _is_structural_error,
    _lang_guard_for,
    _locale_phrase,
    _looks_like_unit_translation,
    _lqa_judge_unavailable,
    _lqa_no_progress,
    _lqa_no_progress_error,
    _lqa_shape_kwargs,
    _mark_project_completed,
    _memory_mode_off,
    _mt_workers,
    _over_translation_guard_on,
    _PriorVerdict,
    _record_visit_cap_skip,
    _references_prev_lqa,
    _union_seg_terms,
    _verdict_family,
)

__all__ = [
    "CANCEL_NOTE",
    "DEFAULT_MAX_VISITS",
    "DEFAULT_PROMPTS",
    "DEFAULT_TOTAL_STEP_CAP",
    "EMPTY_DOCUMENT_MSG",
    "FAIL_FAST_STOP_NOTE",
    "GATE_HOLD_JUDGE_UNAVAILABLE",
    "GATE_HOLD_LOOP_EXHAUSTED",
    "GATE_HOLD_NO_PROGRESS",
    "GATE_HOLD_UNREPAIRED",
    "GATE_HOLD_VISIT_CAP",
    "GATE_HOLD_WARNING_CODE",
    "HYGIENE_NOTE",
    "KEY_TRASH_RUN_REFUSAL",
    "LQA_FINDINGS_SEGMENT_CAP",
    "POOL_EXHAUSTED_NOTE",
    "PROGRESS_MIN_INTERVAL_S",
    "PROGRESS_WRITER_THREAD_NAME",
    "QUEUE_LAUNCH_FAILED_NOTE",
    "REASON_CANCELLED",
    "REASON_CRASH",
    "REASON_FAIL_FAST_STOP",
    "REASON_POOL_EXHAUSTED",
    "REASON_QUEUE_LAUNCH_FAILED",
    "REASON_RESTART",
    "REASON_SPEND_CAP_STOP",
    "REASON_STALE",
    "REASON_SUPERSEDED_ABORT",
    "REASON_WRITE_CONTENTION",
    "RECOVERY_ACTIVITY_GRACE",
    "RESTART_NOTE",
    "ROLE_PRESETS",
    "RUN_THREAD_PREFIX",
    "SEG_JOINER",
    "SPEND_CAP_STOP_NOTE",
    "STALE_NOTE",
    "STALE_NOTE_NO_SUCCESSOR",
    "STALE_RUN_AFTER",
    "SUPERSEDED_ABORT_NOTE",
    "VISIT_CAP_SKIP_WARNING_CODE",
    "WRITE_CONTENTION_NOTE",
    "_CANCELLABLE_RUN_STATUSES",
    "_DEFAULT_VERDICT_SCOPE",
    "_DELETED_GATE_STEP",
    "_DRAFT_FENCE_RE",
    "_DRAFT_PREAMBLE_RE",
    "_FINDINGS_TAIL",
    "_FINDING_LINE_RE",
    "_FIVE_KEYS",
    "_GATE_FAMILY_LQA",
    "_GATE_FAMILY_QE",
    "_IN_FLIGHT_RUN_STATUSES",
    "_LQA_FINDINGS_PH",
    "_LQA_FINDINGS_PREAMBLE",
    "_LQA_PRIOR_VERDICT_LIMIT",
    "_QUOTED_RE",
    "_RC_OPS",
    "_RC_REFS",
    "_RC_STEP_PREFIX",
    "_REASON_PATTERNS",
    "_RUN_CREATE_LOCK",
    "_SINGLE_UNIT_INSTRUCTION",
    "_STRUCTURAL_ERROR_RE",
    "_SYSTEM",
    "_SYSTEM_HEAD",
    "_SYSTEM_MASKED",
    "_SYSTEM_MASKED_CLAUSE",
    "_SYSTEM_NO_TOKENS",
    "_SYSTEM_NO_TOKENS_CLAUSE",
    "_SYSTEM_RAW",
    "_SYSTEM_RAW_CLAUSE",
    "_SYSTEM_TAIL",
    "_TERMINAL",
    "_UNSET",
    "AbstractContextManager",
    "Customer",
    "LQAResult",
    "NamedTuple",
    "ProjectBudgetExhausted",
    "ProviderCallError",
    "ProviderSpec",
    "ProviderTruncated",
    "ResumeMismatch",
    "RunCancelError",
    "RunGuard",
    "UnderTranslationError",
    "Workflow",
    "WorkflowStep",
    "_DeletedGateStep",
    "_PriorVerdict",
    "_ProgressTicker",
    "_StepDirective",
    "_abort_if_superseded",
    "_adjudicated_count",
    "_any_render_toggle",
    "_begin_run_envelope",
    "_bookkeeping_session",
    "_bound_findings_block",
    "_branch_cursor",
    "_bump_run_progress",
    "_cap_keep_fence",
    "_capture_resume_edits",
    "_clean_draft",
    "_commit_run_terminal",
    "_commit_run_terminal_direct",
    "_crash_note",
    "_crash_terminal_note",
    "_empty_document_run",
    "_end_run_cursor",
    "_eval_run_condition",
    "_execute_step",
    "_failure_reason",
    "_finalize_run_envelope",
    "_findings_relevant",
    "_findings_suffix",
    "_five_key",
    "_flush_progress",
    "_fragment_occurs",
    "_ft_segment_id_for_stamp",
    "_gate_hold_message",
    "_gate_hold_of",
    "_gate_release_mask",
    "_gate_release_mask_in_scope",
    "_group_at",
    "_has_graph",
    "_has_parallel",
    "_image_eligible_step",
    "_inject_missing_prefixes",
    "_int_or",
    "_int_setting",
    "_is_correction_loop_step",
    "_is_gate_step",
    "_is_lqa_onfail_target",
    "_is_stale_run",
    "_is_step_ref",
    "_is_structural_error",
    "_key_index",
    "_lang_directive",
    "_lang_guard_for",
    "_last_activity_at",
    "_last_gate_judged_text",
    "_latch_seg_shape",
    "_latest_outcome_steprun",
    "_latest_paused_steprun",
    "_load_segments",
    "_locale_phrase",
    "_looks_like_unit_translation",
    "_lqa_event_payload",
    "_lqa_judge_unavailable",
    "_lqa_no_progress",
    "_lqa_no_progress_error",
    "_lqa_shape_kwargs",
    "_lqa_step_error",
    "_mark_execution_start",
    "_mark_project_completed",
    "_mark_queue_launch_failed",
    "_may_clear_gate_hold",
    "_memory_eligible_step",
    "_memory_mode_off",
    "_mt_workers",
    "_new_qe_stats",
    "_next_cursor",
    "_notify_human_review_paused",
    "_null_scope",
    "_observer_session",
    "_over_translation_guard_on",
    "_paused_blob",
    "_persist_progress",
    "_prior_lqa_verdicts",
    "_progress_dirty",
    "_progress_forget",
    "_progress_idle",
    "_progress_lock",
    "_progress_pool_has_slot",
    "_progress_snapshot",
    "_progress_state",
    "_progress_wake",
    "_progress_writer_lock",
    "_progress_writer_loop",
    "_progress_writer_start",
    "_progress_writer_thread",
    "_qe_spec_for_step",
    "_queue_actor_scope",
    "_reason_patterns",
    "_record_gate_hold",
    "_record_visit_cap_skip",
    "_references_prev_lqa",
    "_refuse_archived",
    "_refuse_key_trash_document",
    "_refuse_launch_guards",
    "_refuse_project_budget",
    "_release_scope",
    "_render",
    "_render_cfg_for",
    "_render_glossary_block",
    "_replay_done_text",
    "_resolve_step_template",
    "_resolved_glossary_id",
    "_run_gate_outcome",
    "_run_parallel_group",
    "_run_step_on_segment",
    "_run_thread_alive",
    "_run_thread_name",
    "_seg_chunks",
    "_source_text",
    "_spec_for_step",
    "_spend_block_reason",
    "_split_for_chunks",
    "_stamp_note",
    "_standing_gate_verdicts",
    "_step_event",
    "_step_ref",
    "_sweep_stale_at_capacity",
    "_system_for",
    "_terminal_gate_hold",
    "_union_seg_terms",
    "_verdict_family",
    "_visit_cap_decision",
    "_write_progress_row",
    "assert_provider_visible",
    "author_prompt",
    "cancel_run",
    "document_has_no_content",
    "est_tokens",
    "get_provider",
    "get_steps",
    "lqa",
    "lqa_attribution",
    "new_queue_group",
    "observe_run",
    "or_",
    "paused_review",
    "prepare_resume_seed",
    "progress_drain",
    "publish_run_progress",
    "queued_runs",
    "re",
    "recover_interrupted_runs",
    "repark_run_at_external_step",
    "resolve_default_model",
    "run_progress",
    "run_reason",
    "scaffold",
    "segment",
    "tap_enrich",
    "timedelta",
    "uuid",
    "waiting_human_review",
]

log = logging.getLogger("loc_tms.orchestrator")
from app import extensions as _extensions
from app.services import (
    ai_tagging,
    context_pkg,
    formality,
    glossary_concepts,
    key_images,
    lqa,
    lqa_attribution,
    lqa_safety,
    output_guard,
    pii,
    pii_boundary,
    prompts,
    qe,
    qe_autofix,
    quality,
    refusal,
    scaffold,
    segment,
    style_guides,
    system_prompt_overrides,
    tag_llm_repair,
    tag_repair,
    tap_enrich,
    tap_envelope,
    tm,
)
from app.services.providers import (
    DeadEngineError,
    ProviderCallError,
    ProviderSpec,
    ProviderTruncated,
    assert_provider_visible,
    get_provider,
    resolve_default_model,
    run_completion,
    usage_token_counts,
)
from app.services.segment import est_tokens

# LLMemory is an optional add-on: recall/capture go through the extension facade (a no-op
# returning ("", []) when the add-on is absent). The name stays `llmemory` so every call site
# and every test patch target is unchanged.
llmemory = _extensions.use("memory")

# The QE gate's pure verdict on one card (skipped / undecided / passed / failed), shared
# by _score_seg and the auto-fix re-judge so a fixed version meets exactly the same bar.
_gate_decision = qe_autofix.gate_decision


#: The WorkflowRun columns a step stages in memory before its completion commit. `status`,
#: `finished_at` and the progress columns are deliberately NOT here: another session (a
#: cancel, a pause, the progress ticker) may write them while a lock-retry sleeps, and
#: re-applying a stale copy would clobber that write.
_STEP_STAGED_RUN_ATTRS = ("context", "used_tm_match", "notes")


def _pending_run_attrs(run) -> dict:
    """Snapshot the run state a step has staged but not yet committed.

    #504. `_db.commit_with_retry` rolls the session back between lock-contended attempts,
    and a rollback reverts the run's staged column writes (and expires its JSON `context`
    back to the last committed value; an autoflush does not help, the rolled-back
    transaction took the flushed UPDATE with it). A retry `stage` that re-applies only the
    StepRun and the cursor therefore silently drops everything else the step staged on the
    run: the QE `gate_hold` and its warning, the `qe_never_scored` warning, `used_tm_match`,
    the pool notes. Capture `_STEP_STAGED_RUN_ATTRS` ONCE, after the step has finished
    mutating the run and before the first commit attempt, and let every attempt
    `_reapply_run_attrs` them.

    `notes` is a shared, appending column that another session (a cancel) may extend while a
    lock-retry sleeps (#578), so the snapshot also records the committed value the step's
    additions were appended to (`_notes_base`); the re-apply merges instead of overwriting."""
    attrs = {key: getattr(run, key) for key in _STEP_STAGED_RUN_ATTRS}
    attrs["_notes_base"] = _committed_attr(run, "notes")
    return attrs


def _committed_attr(run, key):
    """The value of `run.<key>` as last loaded from the database, ignoring a staged change
    (the SQLAlchemy history's old value); None when it was never loaded."""
    from sqlalchemy import inspect as _sa_inspect

    hist = _sa_inspect(run).attrs[key].history
    if hist.deleted:
        return hist.deleted[0]
    if hist.unchanged:
        return hist.unchanged[0]
    return None


def _merge_staged_notes(base, staged, current):
    """The notes a retried step write should commit: what is in the database NOW plus what
    the step appended to `base`. Nothing foreign in the database -> exactly the staged text."""
    staged_raw = staged
    base, staged, current = base or "", staged or "", current or ""
    if not current or current == staged or current in staged:
        return staged_raw
    added = staged[len(base) :].strip() if staged.startswith(base) else staged
    return (current + " " + added).strip() if added else current


def _reapply_run_attrs(run, attrs: dict) -> None:
    attrs = dict(attrs)
    notes_base = attrs.pop("_notes_base", None)
    if "notes" in attrs:
        # After a lock-retry rollback `run.notes` reloads the committed value, which may
        # carry a note another session stamped (a cancel); keep it (#578).
        attrs["notes"] = _merge_staged_notes(notes_base, attrs["notes"], run.notes)
    for key, value in attrs.items():
        setattr(run, key, value)


@contextmanager
def _execution_slot(capacity_slot=None, *, refuse=True):
    """Reserve synchronous work; background and batch parents retain their own token."""
    owned = capacity_slot is None
    if owned and not refuse:
        capacity_slot = RUN_SLOTS.take()[0]
    elif owned:
        capacity_slot, observed = RUN_SLOTS.acquire()
        if capacity_slot is None:
            raise RunCapacityError(observed, config.MAX_CONCURRENT_RUNS)
    try:
        yield capacity_slot
    finally:
        if owned:
            capacity_slot.release(wake=True)


def run_workflow_stream(
    session: Session,
    workflow_id: int,
    doc_id: int,
    locale: str,
    run_id: int | None = None,
    seed_text: str | None = None,
    created_by: str = "",
    is_resume: bool | None = None,
    *,
    capacity_slot=None,
):
    """Count execution, not time suspended in a caller's abandoned iterator.

    First advancement admits fresh work. Later advancements continue an existing
    run unconditionally, like resume. Supplied background/batch owners keep their
    token for the entire execution and executor shutdown.
    """
    stream = None
    first = True
    try:
        while True:
            with _execution_slot(capacity_slot, refuse=first):
                if stream is None:
                    stream = _run_workflow_stream_finalized(
                        session,
                        workflow_id,
                        doc_id,
                        locale,
                        run_id=run_id,
                        seed_text=seed_text,
                        created_by=created_by,
                        is_resume=is_resume,
                    )
                try:
                    event = next(stream)
                except StopIteration:
                    return
                first = False
            yield event
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            with _execution_slot(capacity_slot, refuse=False):
                close()


def _run_workflow_stream_finalized(
    session: Session,
    workflow_id: int,
    doc_id: int,
    locale: str,
    run_id: int | None = None,
    seed_text: str | None = None,
    created_by: str = "",
    is_resume: bool | None = None,
):
    """Run-envelope FINALIZING WRAPPER over the workflow execution generator (proposal
    Wave A). It is a thin pass-through: it forwards every event unchanged, remembers the
    WorkflowRun id from the `run_started` event, and in a `finally` transitions this
    run's TapAction + TapRun to their terminal status by reading the run's final status.

    This is THE seam every launcher funnels through (start_run's background thread, the
    CLI `workflow run`, batch_runs._run_member, resume_run), so envelope finalization
    happens exactly once per run wherever it was launched — and it fires on normal
    completion, an early FAILED/PAUSED return, or generator abandonment (GeneratorExit),
    because a `finally` runs in all three. The envelope CREATION + call stamping live
    inside the impl (below), where the WorkflowRun id and step graph are in scope.

    The impl body is byte-identical to the pre-envelope function — the only reason for
    the split is to give finalization a single robust exit hook."""
    _wf_run_id = None
    _exc = False
    _abandoned = False
    from app.services import ai_optout as _ai_optout

    try:
        # AI opt-out (DPAI-08): every advance of the run executes inside the document's AI
        # scope, so any engine call a step makes passes ai_optout.guard with project context.
        for _ev in _ai_optout.scoped_stream(
            _run_workflow_stream_impl(
                session,
                workflow_id,
                doc_id,
                locale,
                run_id=run_id,
                seed_text=seed_text,
                created_by=created_by,
                is_resume=is_resume,
            ),
            document_id=doc_id,
        ):
            if isinstance(_ev, dict) and _ev.get("type") == "run_started":
                _wf_run_id = _ev.get("run_id")
            yield _ev
    except GeneratorExit:
        # The consumer closed the stream mid-run (SSE disconnect): dropped, not failed.
        _abandoned = True
        raise
    except BaseException:
        _exc = True
        raise
    finally:
        # #101 hygiene (gate F round 1, L1). These wrappers are the seams every launcher
        # funnels through (start_run's background thread, the synchronous CLI `workflow
        # run`, batch_runs._run_member, resume_run), so dropping the live progress entry
        # here covers the executors that have no crash handler of their own. A step that
        # died outside the ticker's own step-boundary path would otherwise leave one dict
        # per run in a long-lived server -- and because a leaked entry's `at` is newer than
        # any persisted row, `run_progress` would serve it forever for that id (and, after
        # SQLite rowid reuse, for whatever run inherited the id). The persisted columns keep
        # whatever the step reached, so forgetting here loses no signal.
        if _wf_run_id is not None:
            _progress_forget(_wf_run_id)
        _settle_tag_repair_spend(_wf_run_id, session, _exc or _abandoned)
        _finalize_run_envelope(_wf_run_id, doc_id, locale, exception=_exc, abandoned=_abandoned)
        # H4: close any still-open vendor dispatch leg of a run that FINISHED (completed/
        # failed) so a later expiry sweep can never drag a terminal run back to PAUSED.
        # Best-effort on an isolated session (never raises, never touches a paused run).
        if _wf_run_id is not None:
            try:
                from app.db import engine as _eng
                from app.services import vendor_dispatch as _vd

                with Session(_eng) as _s:
                    _vd.close_open_legs_for_run(_s, _wf_run_id)
            except Exception:
                # Open legs left behind let an expiry sweep drag this terminal run to PAUSED.
                log.warning(
                    "could not close open vendor legs for run %s", _wf_run_id, exc_info=True
                )


def _run_workflow_stream_impl(
    session: Session,
    workflow_id: int,
    doc_id: int,
    locale: str,
    run_id: int | None = None,
    seed_text: str | None = None,
    created_by: str = "",
    is_resume: bool | None = None,
):
    """Execute (or resume) a workflow over a document, yielding events per step.

    `created_by` (jobs-history attribution) is stamped onto a FRESH run (run_id is
    None — the CLI/synchronous path); it is ignored on resume (run_id set — the
    original launcher stays the owner).

    SEGMENTED / connector docs dispatch to run_workflow_stream_segmented (which
    writes each Segment.target_text). The FULL_TEXT body below was previously left
    BYTE-UNCHANGED (sacred gate) — everything that reaches it is a FULL_TEXT doc.
    Review finding (dead-engine hardening follow-up): the final run.status was set
    to COMPLETED unconditionally even when a step FAILED (empty output), reporting
    a false "completed" with a blank target — the gate now allows exactly one
    deviation, mirroring the segmented path's any_step_failed -> run FAILED logic;
    everything else in this function is unchanged.

    THE TRASH REFUSAL LIVES HERE (Wave B fix round 2, W1) because THIS is the seam
    every launcher funnels through — `start_run`'s background thread, `run_workflow`
    (the CLI's `workflow run`), `batch_runs._run_member` (`--all-locales`) and
    `resume_run`. Putting it on `start_run` alone left three launchers running a
    key project's TRASH document, and a run over parked rows spends provider budget
    on strings the customer just deleted while emitting ZERO delta entries."""
    doc = session.get(Document, doc_id)
    _refuse_key_trash_document(doc)
    # AI opt-out (DPAI-08): the seam every launcher funnels through, INCLUDING resume, so
    # an AI workflow can never run on an opted-out project's document. Human-only
    # workflows (human_review / tm_match / external) pass.
    from app.services import ai_optout as _ai_optout

    _ai_optout.refuse_workflow(session, workflow_id, doc)
    if doc.mode == TransMode.SEGMENTED:
        # Delegate to the segmented IMPL (not its finalizing wrapper): this outer
        # run_workflow_stream wrapper already owns finalization for the whole call, so
        # routing through the wrapper here would finalize the same envelope twice.
        yield from _run_workflow_stream_segmented_impl(
            session,
            workflow_id,
            doc_id,
            locale,
            run_id=run_id,
            seed_text=seed_text,
            created_by=created_by,
            is_resume=is_resume,
        )
        return
    project = session.get(Project, doc.project_id)
    steps = get_steps(session, workflow_id)
    src = project.source_locale
    _tag_spec = None  # AI Tagging 1.5c — last step's spec (see the loop below)
    _tag_redact = False  # #381: any step with Redact PII on masks the tag-repair calls
    # #596: every billed tag-repair call (accepted or rejected) lands here so the write-back
    # can fold it into the run's StepRun spend; see orchestrator_part4._stamp_tag_repair_spend.
    _tag_spend = [0.0, 0, 0, False]
    # GitHub #43 root cause C1: did a LIVE provider produce the text this run will
    # commit? Drives `lang_guard` on the write-back below (the mock's English echo must
    # never trip it). Updated per completion, so it describes the provider that actually
    # produced the FINAL text (post-failover), exactly like translate._lang_guard_for.
    _ft_lang_guard = False
    # LOCK-3 verifier engine: the LQA/judge step's spec when the workflow has one. Live
    # QA (2026-07-25) measured placement verification as ENGINE-BOUND — an 8B local model
    # approved deliberately misplaced tags 2 of 3 tries while claude-haiku was 4/4 — so
    # the verifier defaults to the workflow's QUALITY engine, not its translate engine.
    _tag_verify_spec = None
    source_text = _source_text(doc)

    # T1-05: a FULL_TEXT doc with blank/whitespace-only source has nothing to
    # translate — fail with a clear reason rather than running a step over "" and
    # ending as a bare 'failed'.
    # ENVELOPE NOTE (L6): this early return happens BEFORE _begin_run_envelope, so an
    # empty-document run creates NO envelope — deliberate: it made zero provider calls,
    # so there is no run capture to anchor. The wrapper's finally then finds no envelope
    # for this run id and is a clean no-op. (An empty run that made 0 calls is not the
    # "0 calls captured is an alarm" case, which is about a run that SHOULD have called.)
    if not source_text.strip():
        yield from _empty_document_run(
            session, workflow_id, doc_id, locale, run_id, created_by=created_by
        )
        return

    # Glossary resolution (SPEC-content-types §3): a step PIN wins (content-type-
    # skipped if it mismatches this project); otherwise auto-attach matching approved
    # glossaries for a TYPED project (untyped -> legacy only, byte-identical). One
    # shared plan + merge seam (glossary_module) so this path and the plain-MT path
    # (translate.py) never drift.
    # Context Package (SPEC-context-metadata phase 2A) resolved ONCE per run: the
    # effective TranslationContext for this doc's scope (request/doc/profile chain +
    # the ContentType fallback). `_ctx_sub` fills the `{context}` placeholder in the
    # step template (render_prefix -> block + "\n\n", or "" when nothing resolves ->
    # byte-identical context-less prompts). `_sense_hint` (domain/audience) reaches
    # glossary sense selection. Org-scoped so a foreign org's card is never applied.
    _eff_pkg, _ = context_pkg.resolve(
        session, document=doc, project=project, org_id=getattr(project, "org_id", None)
    )
    _ctx_sub = context_pkg.render_prefix(_eff_pkg)
    _sense_hint = context_pkg.sense_hint(_eff_pkg)
    # Style Guide (SPEC-workflows-redesign Track B, B2) resolved ONCE per run at the
    # customer x content_type (+ optional pair) grain — the standing brand asset. `_sg_sub`
    # fills the `{style_guide}` placeholder (style_guide_prefix -> fenced block + "\n\n",
    # or "" when no APPROVED guide resolves -> byte-identical prompts for installs with no
    # guides). Org-scoped so a foreign org's guide is never applied. NOT redacted below
    # (a standing asset, like {context}/{glossary}, is prompt scaffolding — PII redaction
    # only ever touches the SOURCE content being translated).
    _guide = style_guides.resolve_style_guide(
        session,
        org_id=getattr(project, "org_id", None),
        customer_id=getattr(project, "customer_id", None),
        content_type_id=getattr(project, "content_type_id", None),
        source_locale=src,
        target_locale=locale,
    )
    # MT formality (#617 item 7, ADR 0062): the register resolved for THIS document + target
    # language (document override > project > customer) joins the style-guide block, so every
    # step that renders {style_guide} (or gets it injected) and the LQA judge carry it. `_fm_record`
    # is stamped on the run below. None -> not a byte changes.
    _formality = formality.resolve_for(session, project=project, document=doc, target_locale=locale)
    _fm_record = _formality.record() if _formality is not None else None
    _sg_sub = style_guides.style_guide_prefix(_guide, _formality)
    # B3: the SAME resolved guide, rendered as the raw fenced block (no trailing
    # separator) for an LQA step to feed its judge — score_text checks the translation
    # ADHERES to it. "" when no APPROVED guide resolves -> byte-identical LQA judge
    # prompt (the sacred gate). Computed once (a stable doc-level fact), reused by every
    # LQA step this run visits.
    _sg_render = style_guides.render_style_guide(_guide, _formality)

    from app.services import glossary_module as _gm

    _gnotes: list = []
    gid = _resolved_glossary_id(
        session, steps, src, locale, getattr(project, "content_type_id", None), _gnotes
    )
    # Only the pin-resolution notes (a skipped pin) are user-facing; build_glossary_plan's
    # own notes (project-pinned / auto-attached trace) stay log-only as they always were.
    _pin_skip_notes = list(_gnotes)
    _plan = _gm.build_glossary_plan(
        session, project, src, locale, pinned_glossary_id=gid, notes=_gnotes
    )
    for _n in _gnotes:
        log.info("run glossary: %s", _n)
    # Terminology T3 (SPEC §9-T3 / LAW-TERM-15, residue [11]): swap the matcher for the
    # concept selector ONCE PER RUN, and ONLY when some step has the renderer toggle ON.
    # `_wd_concepts` (with detached `_concept` metadata) feeds each toggle-ON step's
    # per-step render at :3651; `terms` is its five-key projection (byte-identical to
    # match_terms_merged, LAW-TERM-1) and stays what the toggle-OFF glossary_line block +
    # the gates read. No toggle ON -> match_terms_merged exactly as today, `_wd_concepts`
    # is None, and nothing new is spent.
    _wd_render = _any_render_toggle(steps)
    if _wd_render:
        _wd_concepts = glossary_concepts.match_concepts_merged(
            session,
            doc.project_id,
            source_text,
            src,
            locale,
            _plan,
            conflict_state=set(),
            disambig=_sense_hint,
        )
        terms = _five_key(_wd_concepts)
    else:
        _wd_concepts = None
        terms = _gm.match_terms_merged(
            session,
            doc.project_id,
            source_text,
            src,
            locale,
            _plan,
            conflict_state=set(),
            disambig=_sense_hint,
        )
    gloss_text = prompts.glossary_line(terms)
    # Run-level shown-set home for the whole-document run (LAW-TERM-15 (i), residue [8]).
    # None until a toggle-ON producing step publishes its shown set (after the produce
    # decision, residue [13]); the FULL_TEXT regression arm reads it when set, else `terms`.
    # A toggle-OFF producing step RESETS it to None (LAW-TERM-15 "OFF producer -> full match",
    # M5): its enforcement seams then read the full run-level match exactly as today.
    _wd_shown_terms = None

    def _wd_lqa_terms_now():
        """The whole-document LQA judge / adjudicator glossary (LAW-TERM-15 consumer 4),
        resolved to the last toggle-ON producing step's shown set (`_wd_shown_terms`), else
        the full run-level match `terms` exactly as today. Mirrors the segmented
        `_lqa_terms_now()`; byte-identical when no step has the toggle ON (H1). The
        EXTERNAL/human-vendor dispatch does NOT read this value — it keeps the full match
        (`terms`), an `mt`/`human`-channel package, never the `llm`-gated/capped subset (M6)."""
        return _wd_shown_terms if _wd_shown_terms is not None else terms

    base_ctx = {
        "source": source_text,
        "source_locale": src,
        "target_locale": locale,
        "glossary": gloss_text,
        "context": _ctx_sub,
        "style_guide": _sg_sub,
        "lqa_findings": "",
    }

    # GitHub #356 phases 2a/2b: the AI-step system HEAD override in force for THIS run, resolved
    # ONCE per run: the workflow's own override, else its base workflow's (#365, a pair
    # variant's group default), else the run's project org's (never a caller-supplied org),
    # else the built-in text. None unless the
    # `system_prompt_overrides` flag is ON and an override is in force -> every composed system
    # message is then byte-identical to the built-in text. The locked floor is composed around
    # it by `_system_for`; `_sys_stamp` is what the StepRun rows record (empty while the flag
    # is off).
    _sys_ovr = system_prompt_overrides.resolve_step_head(
        session, getattr(project, "org_id", None), workflow_id
    )
    _sys_head_body = _sys_ovr.body if _sys_ovr is not None else None
    _sys_stamp = _system_prompt_stamp(_sys_ovr, src, locale)

    if run_id is not None:
        run = session.get(WorkflowRun, run_id)
        start = run.current_step_index
        # T1-02: an empty/whitespace-only edited_text on resume means "no edits" —
        # keep the text captured at the pause (never feed "" forward, which would
        # discard the produced MT). A real (non-blank) edit flows through unchanged.
        if seed_text is not None and not seed_text.strip():
            blob = _paused_blob(session, run_id)
            current_text = blob if blob is not None else source_text
        else:
            current_text = seed_text if seed_text is not None else source_text
    else:
        run = WorkflowRun(
            workflow_id=workflow_id,
            document_id=doc_id,
            target_locale=locale,
            created_by=created_by or "",
            status=RunStatus.RUNNING,
            current_step_index=0,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        start = 0
        current_text = source_text

    run.status = RunStatus.RUNNING
    run.formality = _fm_record  # #617 item 7: the register that applied (None = none resolved)
    # Review F2: execution begins (or RE-begins, after a human pause) NOW, so the
    # staleness clock starts here. The chokepoint every FULL_TEXT execution passes
    # through, fresh or resumed.
    _mark_execution_start(run)
    session.add(run)
    session.commit()

    # Make the glossary trace visible to the user (a non-covering pin skip / a
    # content-type skip): stamp each pin-skip note onto the run's notes, de-duplicated
    # and transaction-safe the way the tm_match notes above are. Plain log.info alone left
    # the skip invisible on the run page and API.
    for _gn in _pin_skip_notes:
        if _gn not in (run.notes or ""):
            _stamp_note(run, _gn)
    if _pin_skip_notes:
        session.add(run)
        session.commit()

    # Run envelope (proposal Wave A): created here, AFTER the WorkflowRun is committed
    # (session lock-free) and BEFORE the run_started yield, so even an immediate abandon
    # at that first yield still has an envelope to finalize (consistency with M1). The
    # isolated-session envelope write cannot contend with this run's own writes.
    # `_env_ids` (action_id, run_id) is stamped into every provider-call tap_meta below
    # via tap_envelope.stamp_meta so each captured call belongs to exactly one action.
    # Best-effort: (None, None) leaves stamp_meta a no-op and the run byte-identical.
    # Resume (run_id set) reuses the original envelope; a fresh run always mints one.
    _is_resume = is_resume if is_resume is not None else (run_id is not None)
    _env_ids = _begin_run_envelope(session, run, doc, project, steps, is_resume=_is_resume)
    _register_tag_spend(run.id, _tag_spend)
    yield {"type": "run_started", "run_id": run.id, "steps": len(steps), "start": start}

    # Track whether any step FAILED (e.g. a dead engine:<id> ref -> empty output),
    # mirroring the segmented path's any_step_failed. Without this, a failed step
    # (StepRun.status == FAILED, current_text == "") was still followed by an
    # unconditional run.status = COMPLETED below -> the API/CLI reported
    # {"ok": true, "status": "completed"} with a blank target, a false-completed run.
    any_step_failed = False
    fail_err: str | None = None
    # C1/C4: True once ANY step's output reads as a refusal. The per-step guard below
    # catches the hedge AT the step that produced it (not just the final step), so a
    # later "finalizer" step can't launder it into clean target-language text — on a
    # hit we stop feeding it forward and route the unit to NEEDS_REVIEW. Hoisted here
    # (was the post-loop final-check flag) so the loop and the write-back share it.
    refused = False
    # LQA safety floor (C), FULL_TEXT asymmetry (PROPOSAL §4.C). The whole-document path
    # holds one blob, so the same pre/post terminology delta is computable — but the
    # revert granularity would be THE ENTIRE DOCUMENT, which is far too blunt (one
    # introduced violation would discard an otherwise good pass). So FULL_TEXT FLAGS and
    # never reverts: each step that breaks a term contributes a `fix_term_regression`
    # qa_flag ATTRIBUTING the regression to it (the write-back's absolute glossary/DNT
    # gates already flag the violation itself and route to review; what they cannot say
    # is WHICH step caused it, which is the whole point of a delta). Empty on every run
    # with no glossary / no regression -> byte-identical.
    ft_regressions: list = []
    # Prompt-package engine (change 1): the rendered <lqa_findings> block of the MOST
    # RECENT LQA card this run has produced, threaded through run state so a downstream
    # llm_agent step can consume it via {lqa_findings} or the correction-loop auto-inject.
    # "" until an LQA step runs; UPDATED on every LQA visit so a loop's later fix sees the
    # FRESH card (visit 2 of a fix sees lqa2, not the stale lqa1). Never re-queries a
    # provider — it re-renders the score_text return value the LQA step already holds.
    # RESUME-SAFE (review fix 2): the block is PERSISTED into WorkflowRun.context (the same
    # resume-safe scratchpad RunGuard uses) whenever an LQA produces findings, and RESTORED
    # here on resume — so a fix step gated on prev_lqa still receives the findings after a
    # human_review pause, even though this in-memory local was reset to "".
    lqa_findings_block = (run.context or {}).get("lqa_findings_block", "")
    # RESUME-SAFE, the SAME defect one loop over (re-review N2): `_ft_lang_guard` is
    # run-LOCAL and the FULL_TEXT write-back (like the segmented one) happens after the
    # whole step loop, so a human_review pause reset it to False and the resumed run
    # committed its target with the language guard OFF. Measured: a live provider echoing
    # its English source lands NEEDS_REVIEW + `nontranslation` on a straight run, and
    # clean `mt` on the identical run with a review pause in it — the guard silently
    # disabled by the very act of putting a human in the loop. Restored here, persisted at
    # the pause site below on the same write-through terms as the segmented path.
    _ft_lang_guard = bool((run.context or {}).get("ft_lang_guard", _ft_lang_guard))

    # F-CRIT / F-LOW-1 (FULL_TEXT twin of the segmented read below): a vendor/external
    # RETURN's ONE-SHOT resume write-back policy. `skip` names the single whole-body slot
    # (position 0, "full:0") whose vendor verdict is authoritative and must NOT be
    # re-committed from the parked SOURCE; `tm_ineligible` keeps an admitted-vendor slot's
    # TM-ineligible intent. CONSUMED + CLEARED so it never leaks into a later resume.
    _vr_wb = (run.context or {}).get("_vendor_return_writeback") or {}
    _vr_skip = set(_vr_wb.get("skip") or [])
    _vr_tm_inelig = set(_vr_wb.get("tm_ineligible") or [])
    # PART 1 (INFO-2): this resume is a vendor/external RETURN completing-resume (not a human
    # pause-edit), captured BEFORE the pop below. The EXPLICIT `_resume_kind == "vendor"` marker is
    # the primary discriminator (set unconditionally by vendor_return); the `_vendor_return_writeback`  # noqa: E501
    # key is checked too for back-compat with a run parked under older code. An operator-edit
    # classification must not fire for it -- the vendor write-back path owns the body.
    _ctx0 = run.context or {}
    _ft_is_vendor_return = (
        _ctx0.get("_resume_kind") == "vendor" or "_vendor_return_writeback" in _ctx0
    )
    if "_vendor_return_writeback" in _ctx0 or "_resume_kind" in _ctx0:
        _c = dict(_ctx0)
        _c.pop("_vendor_return_writeback", None)
        _c.pop("_resume_kind", None)
        run.context = _c
        session.add(run)
        session.commit()

    # Cursor machine (SPEC §4.1): replaces `for i in range(start, len(steps))` with a
    # while-cursor loop so Phase 3 routes backward (bounded loops). On a plain / Phase-2
    # workflow (no branch / max_visits) `_has_graph` is False -> the RunGuard is inert
    # (never writes run.context, never trips a cap) and _next_cursor always returns
    # cursor+1: byte-identical to today, the sacred gate.
    key_index = _key_index(steps)
    guard = RunGuard(session, run, _has_graph(steps))
    # SPEC-parallel-routing: computed ONCE. False -> `_group_at` is never called and the
    # whole fork/join seam is unreachable (byte-identical to a pre-parallel run).
    par_on = _has_parallel(steps)

    def _ft_pause_ctx():
        # RESUME-SAFE (re-review N2): carry the language-guard decision across a
        # human_review pause via the shared _execute_step pause seam. WRITE-THROUGH: once
        # the key exists, every later pause republishes the current value (a second pause
        # after a NON-live step must be able to turn it back off), while a run that never
        # saw a live provider never writes run.context at all (the RunGuard NULL contract).
        _prev_ctx = run.context or {}
        if (
            _ft_lang_guard
            or _ft_operator_edited
            or "ft_lang_guard" in _prev_ctx
            or "ft_operator_edited" in _prev_ctx
        ):
            # NEW-ISSUE-4: `_ft_operator_edited` must be STICKY across parks (same reason as the
            # segmented operator_edited set) -- otherwise a second pause re-baselines the edit
            # against the newly-parked blob and the terminal commit reverts it to the stored
            # target. Persisted here, restored + OR'd with the fresh classification at entry.
            _ctx = {
                **_prev_ctx,
                "ft_lang_guard": bool(_ft_lang_guard),
                "ft_operator_edited": bool(_ft_operator_edited),
            }
            # PART 1: the operator's edited body text, sticky across parks (see _ft_edit_text).
            if _ft_edit_text is not None:
                _ctx["ft_operator_edited_text"] = _ft_edit_text
            return _ctx
        return None

    def _ft_dispatch_payload():
        # FULL_TEXT external dispatch (PROPOSAL-external-processing-step Wave A): the whole
        # document body is ONE contract unit. `current_text` is the current working target
        # (source_text before any step ran). No per-segment TM band exists in full-text
        # mode (the FT loop performs no per-segment TM lookup -- see the tm_match note),
        # so the estimate prices it as one `new` unit. L10: a STRING id ("full:0")
        # namespaces the whole-body unit so Wave B's id-accounting can never confuse it
        # with a SEGMENTED integer seg id (which are ints).
        return [
            {"id": "full:0", "source": source_text, "target": current_text, "tm_match_pct": None}
        ]

    # F7 + NEW-ISSUE 1/2/3 (adversarial re-review): STATUS PROTECTION for FULL_TEXT. If the
    # single body segment is human-APPROVED/EDITED, no PROVIDER step may re-translate it -- but
    # human_review / external / LQA steps MUST still run over it (NEW-ISSUE-3: skipping the whole
    # loop meant a reviewer never got to look, an EXTERNAL leg never dispatched, and LQA never
    # recorded). So we do NOT jump the cursor; instead each llm_agent provider fan-out is skipped
    # below when `_ft_protected and not _ft_operator_edited`, and the body's stored target rides
    # `current_text` so LQA / a human sees the real value. An OPERATOR edit made during the pause
    # is classified from the run's PARKED STATE (the paused blob), NOT from divergence against the
    # stored target (NEW-ISSUE-1): a concurrent human APPROVING their own wording mid-pause also
    # diverges from the stored target, and treating that as an operator edit would commit the
    # machine string over the fresh approval (with set_status=False leaving it FALSELY labelled
    # approved). Only a genuine pause has a parked blob, and resume_workflow / resume_run_async
    # now refuse a non-paused run (NEW-ISSUE-2), so a resume of a completed/never-paused run can
    # never reach the human_edit bypass.
    _ft_body_seg = session.exec(
        select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
    ).first()
    _ft_protected = _ft_body_seg is not None and getattr(_ft_body_seg, "status", None) in (
        SegStatus.APPROVED,
        SegStatus.EDITED,
    )
    # NEW-ISSUE-4: restore the STICKY operator-edit flag persisted at the last pause, then OR it
    # with this entry's fresh classification -- so an edit made at an EARLIER pause is not lost
    # when a LATER untouched pause re-baselines it against the newly-parked blob.
    _ft_operator_edited = bool((run.context or {}).get("ft_operator_edited", False))
    # PART 1: the operator's edited body TEXT (sticky across parks), so the terminal commit can
    # tell an edit that is STILL the final body (-> EDITED) from one a step AFTER the pause re-ran
    # over IN-RUN (-> MT, the in-run boundary), mirroring the segmented operator_edited_text.
    _ft_edit_text = (run.context or {}).get("ft_operator_edited_text")
    if not isinstance(_ft_edit_text, str):
        _ft_edit_text = None
    # PART 2 (vendor-return sticky-flag guard, 2026-08-13 security review): the RESTORED operator
    # flag must NOT survive into a vendor/external RETURN completing-resume. The FRESH
    # classification below is already vendor-suppressed (`not _ft_is_vendor_return`), but the
    # restored flag alone would leave `human_edit=_ft_operator_edited` TRUE at the terminal commit
    # (see ~2634 -> the commit_mt_output call below): the VENDOR's returned body would then ride the
    # operator human_edit channel -- bypassing the status gate the vendor write-back path relies on
    # -- and land via the operator-edit status block (EDITED/MT) instead of by vendor rules. FULL_TEXT  # noqa: E501
    # has ONE body slot; the operator DISPATCHED that body to the vendor AFTER editing it, so the
    # vendor's returned content SUPERSEDES the earlier edit. CLEAR the flag (not merely skip it): the  # noqa: E501
    # vendor body then commits exactly as a pure vendor return with no prior pause-edit -- the status  # noqa: E501
    # gate is honoured (a protected body is refused, not overwritten) and the vendor's verdict owns
    # the slot; a tag/dnt/glossary/char miss still routes to review through the ordinary seam gates
    # (those fire regardless of human_edit). The write-through at any LATER pause republishes
    # ft_operator_edited=False; the stale edit-text is inert once the flag is False.
    if _ft_is_vendor_return:
        _ft_operator_edited = False
        _ft_edit_text = None
    # PART 1: classify an operator pause-edit for ANY body status (not only a human-finalized
    # one). The flood's silent-loss class is an operator replacing a MACHINE (mt) value during
    # review; landing it EDITED protects it from the next run exactly as for a protected body.
    # Skipped on a vendor/external RETURN completing-resume (the vendor owns the body, not a human).
    if run_id is not None and not _ft_is_vendor_return:
        _ft_parked = _paused_blob(session, run_id)
        if _ft_parked is not None and current_text != _ft_parked:
            # resume + the operator changed the body during the pause -> land it (human_edit)
            # and remember the edited text for the in-run-boundary check at the terminal commit.
            _ft_operator_edited = True
            _ft_edit_text = current_text
    if _ft_protected and not _ft_operator_edited:
        # fresh, or a resume that did not touch the body (incl. a concurrent approval) -> keep
        # the human target as the working text; provider steps below are skipped and the
        # terminal commit's seam refusal surfaces status_protected.
        current_text = _ft_body_seg.target_text or current_text

    # LLMemory FULL_TEXT doc-note memo (Train D §7.2/E20, gate fix 1): the doc-level note
    # is a stable per-RUN fact, so it is resolved ONCE for the whole run — NOT once per
    # step-visit — exactly like the segmented site's `_memory_doc_note`/`_mem_note_state`.
    # Re-selecting it per step would re-charge the block budget AND bump its `use_count`
    # once per step, turning the "is this memory earning its place?" counter into a step
    # counter (the precise failure the segmented memo exists to avoid; janitor decay and
    # Train E's ablation both READ that counter). The whole-doc term pass stays per
    # step-visit (a term injected into N translate prompts was used N times). [0]=computed?,
    # [1]=block, [2]=ids, [3]=usage already stamped for the note?
    _ft_mem_note_state = [False, "", [], False]

    def _ft_memory_doc_note():
        """(block, ids) for this run's doc-level note. Computed at most once per run;
        ("", []) when recall is off, the project has no customer, or nothing qualifies.
        Selected with mark_used=False and stamped by the note latch in the step loop —
        at most once per run, on the first step-visit whose fold KEPT it (F1)."""
        if not _ft_mem_note_state[0]:
            _ft_mem_note_state[0] = True
            try:
                _blk, _ids = llmemory.select_doc_note(
                    session,
                    customer_id=getattr(project, "customer_id", None),
                    project_id=doc.project_id,
                    source_locale=src,
                    target_locale=locale,
                    org_id=getattr(project, "org_id", None),
                    mark_used=False,
                )
                _ft_mem_note_state[1], _ft_mem_note_state[2] = _blk, list(_ids)
            except Exception:
                log.warning("llmemory: FULL_TEXT doc-note recall failed", exc_info=True)
        return _ft_mem_note_state[1], _ft_mem_note_state[2]

    cursor = start
    while 0 <= cursor < len(steps):
        step = steps[cursor]
        # WHEN THIS STEP BEGAN EXECUTING (review NEW-1). Every StepRun the iteration
        # persists stamps it, and each stamps finished_at at its own commit -- so the
        # newest StepRun timestamp on a run is a real, production-written "last sign of
        # life". Nothing wrote these columns before, which made the activity-based
        # staleness floor and the boot healer's grace window inert on real data.
        step_started_at = datetime.utcnow()

        # SPEC-parallel-routing §5.2: FORK. `_group_at` returns bounds only at the FIRST
        # member of a group of 2+; everything else falls through to the ordinary
        # single-step path below, unchanged.
        _bounds = _group_at(steps, cursor) if par_on else None
        # ISSUE #94: the DOCUMENTED per-step execution ceiling, enforced BEFORE the step
        # (or the group it forks) runs -- not only when a branch jumps INTO it. See
        # `_visit_cap_decision` for the gate/producer split (H3) and for why NO StepRun row
        # is written (M1). Inert on a plain/linear workflow (guard inactive).
        #
        # AFTER the run_condition guard, semantically (H3(ii)): a step whose condition is
        # FALSE would have been SKIPPED and the run would have carried on, so sitting at
        # its ceiling must not upgrade that skip into a run end. The condition is only
        # evaluated here when the step IS capped -- the common path is untouched -- and the
        # guard below re-evaluates it for the skip itself, exactly as before.
        _cap_step, _cap_reason, _cap_is_gate = _visit_cap_decision(guard, steps, cursor, _bounds)
        if _cap_step is not None and not (
            not _bounds and step.run_condition and not _eval_run_condition(step, run, session)
        ):
            _cap_index = steps.index(_cap_step)
            if _cap_is_gate:
                _record_gate_hold(
                    session, run, step=_cap_step, step_index=_cap_index, cause=GATE_HOLD_VISIT_CAP
                )
                run.current_step_index = len(steps)
            else:
                # CONSTRAINT (gate C round 2, L2): the producer SKIP advances `cursor + 1`,
                # which is only correct because `_cap_index == cursor` whenever this branch
                # runs. A `bounds`-set decision can name a group MEMBER at any index, and a
                # cursor left mid-group would make `_group_at` return None, decomposing the
                # group into sequential single-step execution and skipping the frozen
                # snapshot fork and the join routing entirely. That is unreachable TODAY
                # because `parallel_group` is a dead field on every non-`lqa` step type
                # (workflows.py refuses it on llm_agent / human_review / tm_match /
                # external), so every group member is a GATE and takes the branch above.
                # If Phase 2 ever admits a producing lane, this must become
                # `max(_cap_index, cursor) + 1` (or refuse the producer branch when
                # `_bounds` is set); `verify_parallel_routing` pins the dead-field premise.
                _record_visit_cap_skip(
                    session, run, step=_cap_step, step_index=_cap_index, reason=_cap_reason
                )
                run.current_step_index = cursor + 1
            session.add(run)
            session.commit()
            yield {
                "type": "step_skipped",
                "run_id": run.id,
                "step_index": _cap_index,
                "step_name": _cap_step.name or _cap_step.step_type,
                "step_type": _cap_step.step_type,
                "condition": {},
                "visit_cap": True,
                "held": bool(_cap_is_gate),
                "error": _cap_reason,
                "output": current_text,
            }
            cursor = len(steps) if _cap_is_gate else cursor + 1
            continue
        if _bounds:
            _gr = _run_parallel_group(
                session,
                run,
                steps,
                _bounds[0],
                _bounds[1],
                guard=guard,
                key_index=key_index,
                src_blob=source_text,
                tgt_blob=current_text,
                src=src,
                locale=locale,
                sg_render=_sg_render,
                lqa_terms=_wd_lqa_terms_now(),
                workflow_id=workflow_id,
                doc_id=doc_id,
                env_ids=_env_ids,
            )
            if _gr["cap_reason"]:
                session.add(
                    StepRun(
                        workflow_run_id=run.id,
                        step_id=_gr["cap_step"].id,
                        status=RunStatus.FAILED,
                        input_text=current_text,
                        output_text="",
                        error=_gr["cap_reason"],
                        outcome_index=guard.visit_index(_gr["cap_step"]),
                        started_at=step_started_at,
                        finished_at=datetime.utcnow(),
                    )
                )
                _commit_run_terminal_direct(session, run, RunStatus.FAILED)
                yield {"type": "error", "run_id": run.id, "error": _gr["cap_reason"]}
                return
            if _gr["findings_block"] is not None:
                lqa_findings_block = _gr["findings_block"]
            for _ev in _gr["events"]:  # noqa: UP028
                yield _ev
            if _gr["failed"]:
                # §5.2: the run fails AFTER every member persisted, so the operator sees
                # every lane's outcome rather than only the first crash.
                _commit_run_terminal_direct(session, run, RunStatus.FAILED)
                yield {"type": "error", "run_id": run.id, "error": _gr["error"]}
                return
            cursor = _gr["next_cursor"]
            continue

        # SPEC §4.2 RunGuard: count this step execution (resume-safe scratchpad) BEFORE
        # anything runs, so a loop that diverges trips the run-level TOTAL-STEP cap and
        # halts CLEANLY (FAILED 'loop_cap_reached') instead of spinning. Inert on a
        # linear run (total <= len(steps)); no context write when _has_graph is False.
        cap_reason = guard.enter(step)
        if cap_reason:
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status=RunStatus.FAILED,
                    input_text=current_text,
                    output_text="",
                    error=cap_reason,
                    outcome_index=guard.visit_index(step),
                    started_at=step_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            _commit_run_terminal_direct(session, run, RunStatus.FAILED)
            yield {"type": "error", "run_id": run.id, "error": cap_reason}
            return

        # SPEC §5 PRE-STEP GUARD (Phase 2): skip this step when its run_condition
        # evaluates FALSE against the durable prior outcome. `step.run_condition` is
        # NULL on a plain step -> this short-circuits and NOTHING new runs (no evaluator
        # call, no SKIPPED row, no event) = byte-identical to today. On a skip we write
        # a SKIPPED StepRun, leave the rolling text (current_text) UNCHANGED so a later
        # step still sees the prior output, advance current_step_index + the cursor and
        # emit the additive step_skipped event, continue. A SKIP never routes a branch
        # (the step produced no gate) — it always falls through to cursor+1.
        if step.run_condition and not _eval_run_condition(step, run, session):
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status=RunStatus.SKIPPED,
                    input_text=current_text,
                    output_text=current_text,
                    outcome_index=guard.visit_index(step),
                    started_at=step_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            run.current_step_index = cursor + 1
            session.add(run)
            session.commit()
            yield {
                "type": "step_skipped",
                "run_id": run.id,
                "step_index": cursor,
                "step_name": step.name or step.step_type,
                "step_type": step.step_type,
                "condition": dict(step.run_condition),
                "output": current_text,
            }
            cursor = cursor + 1
            continue

        if step.step_type == StepType.TM_MATCH:
            # Read-only lookup keys; the source body and segments stay untouched.
            from app.services import srx

            sentences = srx.segment_text(source_text, src)
            # Same narrow-only, fail-closed selection as segmented TM matching.
            restrict_pids = None
            if step.tm_pool_selection is not None:
                from app.services import project_sources

                readable = {doc.project_id} | set(
                    project_sources.allowed_tm_source_ids(session, doc.project_id) or ()
                )
                selected = {int(p) for p in step.tm_pool_selection if p is not None}
                restrict_pids = selected & readable
                dropped = sorted(selected - readable)
                if dropped:
                    dropped_text = ", ".join(str(p) for p in dropped)
                    _stamp_note(run, f"tm_match TM pool {dropped_text} not readable -- skipped")
                if not restrict_pids:
                    _stamp_note(
                        run, "tm_match TM pool selection is empty -- no TM pool read this step"
                    )
            hits = tm.lookup_batch(
                session,
                doc.project_id,
                sentences,
                src,
                locale,
                min_score=100,
                restrict_project_ids=restrict_pids,
            )
            offers = [
                {"source": sentence, "target": hit["target_text"], "tm_id": hit["id"]}
                for sentence in dict.fromkeys(sentences)
                if (hit := hits.get(sentence)) and hit["match_pct"] == 100
            ]
            # `srx.segment_text` returns the body UNSPLIT past its 100 KB paragraph ceiling,
            # so a large document yields one "sentence" that matches nothing and reports a
            # clean zero. "This document has no TM coverage" and "the splitter gave up" are
            # very different facts and an operator must not have to guess which one they
            # got, so the second says so.
            _unsplit = len(sentences) == 1 and len(source_text) > srx._MAX_SRX_PARAGRAPH
            # Flag receiving-step redaction in the metadata every surface reads. This
            # does not suppress offers to other steps, so never claim nothing was sent
            # for a mixed workflow. Actual prompt withholding happens per step below.
            # Gate R4 M1: the message must say whether ANY later LLM step actually receives
            # the offers. Compute the later llm_agent steps and their redacting subset from
            # the same lookahead, so "other steps receive them" is only claimed when true.
            _later_llm = [st for st in steps[cursor + 1 :] if st.step_type == StepType.LLM_AGENT]
            _redacting = [st for st in _later_llm if st.redact_pii or pii.enabled()]
            _withheld = bool(offers) and bool(_redacting)
            _message = f"{len(offers)} approved renderings OFFERED (unverified)."
            if offers and not _later_llm:
                # L1 (#334): an offer no later step can receive is not "offered"; saying
                # so keeps the run from reading as if a step was handed the renderings.
                _message = (
                    f"{len(offers)} approved renderings found; no later LLM step exists "
                    "to receive them."
                )
            elif _withheld and len(_redacting) < len(_later_llm):
                _message = (
                    f"{len(offers)} approved renderings found; WITHHELD from steps that "
                    "redact PII because offers reproduce source sentences verbatim. "
                    "Other later LLM steps receive these unverified offers."
                )
            elif _withheld:
                _message = (
                    f"{len(offers)} approved renderings found; WITHHELD from every later LLM "
                    "step because they redact PII and offers reproduce source sentences "
                    "verbatim. No offer reached the engine."
                )
            if _unsplit:
                _message = (
                    "No renderings offered: this document is too large to split into "
                    "sentences, so no TM lookup was possible. Its size, not its TM "
                    "coverage, is the reason."
                )
            counts = {
                "sentences_examined": len(sentences),
                "matches_offered": len(offers),
                "message": _message,
            }
            sr = StepRun(
                workflow_run_id=run.id,
                step_id=step.id,
                status=RunStatus.COMPLETED,
                input_text=current_text,
                output_text=current_text,
                outcome_index=guard.visit_index(step),
                started_at=step_started_at,
                finished_at=datetime.utcnow(),
            )
            session.add(sr)
            session.flush()
            ctx = dict(run.context or {})
            visits = dict(ctx.get("tm_offer_steps") or {})
            visits[str(sr.id)] = counts
            run.context = {
                **ctx,
                "required_renderings": offers,
                "tm_offers": counts,
                "tm_offer_steps": visits,
            }
            run.used_tm_match = True
            run.current_step_index = cursor + 1
            session.add(run)
            session.commit()
            yield {
                "type": "step",
                "run_id": run.id,
                "step_index": cursor,
                "step_name": step.name or "TM match",
                "step_type": step.step_type,
                "output": current_text,
                "tm_match": counts,
            }
            cursor = cursor + 1
            continue

        # Shared per-step dispatch (the execute-one-step seam): human_review pause, the
        # LQA judge, and the llm_agent spend gate live in `_execute_step` — the ONE place
        # both run loops route these step types through. `input_blob` / `lqa_src` /
        # `terms` / `_ft_pause_ctx` are this loop's (FULL_TEXT) mode-specific values. On
        # `fallthrough` the llm_agent fan-out below runs; on a handled step (LQA) the
        # fresh findings block is threaded on; on a stop (pause / failure) the run is
        # already finalized and we return.
        _directive = yield from _execute_step(
            session,
            run,
            steps,
            step,
            cursor,
            guard=guard,
            key_index=key_index,
            input_blob=current_text,
            lqa_src=source_text,
            lqa_terms=_wd_lqa_terms_now(),
            external_glossary=terms,
            sg_render=_sg_render,
            workflow_id=workflow_id,
            doc_id=doc_id,
            env_ids=_env_ids,
            src=src,
            locale=locale,
            step_started_at=step_started_at,
            pause_context_fn=_ft_pause_ctx,
            dispatch_payload_fn=_ft_dispatch_payload,
        )
        if _directive.stop:
            return
        if not _directive.fallthrough:
            if _directive.lqa_findings_block is not _UNSET:
                lqa_findings_block = _directive.lqa_findings_block
            cursor = _directive.next_cursor
            continue

        # NEW-ISSUE-3 (adversarial re-review): PROVIDER-STEP skip for a protected body. This is
        # an llm_agent step (it fell through _execute_step, which already handled human_review /
        # LQA / external). If the body is a human sign-off we are protecting (not an operator
        # edit landing on THIS resume), do NOT call the provider -- keep `current_text` (the
        # human target seeded above) and record the step as a completed no-op. Restricts the
        # status protection to provider/translate steps ONLY, so a reviewer, an external leg, and
        # an LQA judge still see the protected body. The terminal commit's seam refusal surfaces
        # the protection; on an operator-edit resume `_ft_operator_edited` is True and this skip
        # is inert (the edit flows through the provider steps and lands at the terminal commit).
        if _ft_protected and not _ft_operator_edited:
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status=RunStatus.COMPLETED,
                    input_text=current_text,
                    output_text=current_text,
                    error="status-protected: a human-approved/edited body is not re-translated",
                    outcome_index=guard.visit_index(step),
                    started_at=step_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            run.current_step_index = cursor + 1
            session.add(run)
            session.commit()
            yield {
                "type": "step",
                "run_id": run.id,
                "step_index": cursor,
                "step_name": step.name or step.step_type,
                "step_type": step.step_type,
                "output": current_text,
            }
            cursor = cursor + 1
            continue

        spec = _spec_for_step(step)
        # AI Tagging 1.5c: remember the LAST step's resolved spec so the FULL_TEXT
        # commit below can bind the LLM tag-repair rung to the engine that actually
        # produced the text (pinned model + fallback chain), not a provider default.
        # Stays None when no step ever ran -> the rung is simply not injected.
        _tag_spec = spec
        _tag_redact = _tag_redact or bool(step.redact_pii)
        if step.step_type == StepType.LQA:
            _tag_verify_spec = spec
        # SPEC-prompt-template-runtime: resolve the effective prompt ONCE per step-visit
        # (inline prompt_text > bound PromptTemplate body > DEFAULT_PROMPTS). A dangling/
        # invalid binding falls back to the default with `tmpl_note` (surfaced on the
        # StepRun below) -- never a crashed run.
        resolved = _resolve_step_template(session, step)
        template = resolved.text
        tmpl_note = resolved.note
        # AI Tagging 1.1: mask the inline-fidelity ⟦⟧ tokens to opaque «TAG_N» sentinels
        # BEFORE chunking (SEGMENT-granular — the whole step input masked ONCE, so a
        # paired ⟦r1⟧…⟦/r1⟧ split across chunks keeps one consistent numbering, never the
        # per-chunk collision pii would produce). Restored (1.5 / below) BEFORE the output
        # feeds the next step / the write gate / TM — every verification sees real ⟦⟧.
        # No-op (byte-identical) when masking is off or the text carries no token.
        tag_text, tag_map = (
            ai_tagging.mask(current_text) if ai_tagging.enabled() else (current_text, {})
        )
        chunks = (
            [tag_text]
            if step.chunk_policy == "none"
            else _split_for_chunks(tag_text, config.MAX_CHUNK_TOKENS)
        )

        # LLMemory FULL_TEXT recall (Train D, SPEC-llmemory-phase2 §7.2). Gated on the
        # SAME _memory_eligible_step predicate the segmented path uses (a FULL_TEXT
        # translate step, never the LQA judge or the correction-loop fixer) PLUS the
        # default-OFF `llmemory_fulltext_recall_enabled` flag. OFF (the default) -> the
        # block is "" -> zero prompt bytes -> byte-identical to pre-Train-D (LAW 4).
        #
        # E20: the block is resolved per step-visit for the TERM pass (a whole-document
        # select_for_segment, its 2-entry cap acknowledged as low-value against a chunked
        # doc) FOLDED with the once-per-RUN doc-level note (`_ft_memory_doc_note`, the
        # PRIMARY arm — gate fix 1). The SAME resolved block is injected once per chunk
        # below (accepting the token cost of repeating a ~600-char block per chunk, small
        # next to a full chunk). Confinement is unchanged: both selectors funnel through
        # _active_entries with the run's customer_id/org_id (LAW 6). GDPR: memory text can
        # now reach a FULL_TEXT prompt's tap record — the prompt-tap capture files remain
        # the tap's own erasure path (purge docstring), no new obligation. Resolution here
        # is READ-ONLY (mark_used=False); the usage STAMP is deferred to AFTER the chunk
        # loop's provider calls (below) so the run session never holds a write lock while a
        # provider call writes the spend ledger on its own connection (SQLite BUSY).
        _ft_mem_block, _ft_mem_ids, _ft_note_ids = "", [], []
        if llmemory.fulltext_recall_enabled() and _memory_eligible_step(step, steps, template):
            try:
                _ft_cid = getattr(project, "customer_id", None)
                _ft_org = getattr(project, "org_id", None)
                # PER STEP-VISIT: the whole-document term pass (a term injected into N
                # translate prompts was genuinely used N times — the FULL_TEXT analog of
                # the segmented site's per-SEGMENT term selection).
                _s_blk, _s_ids = llmemory.select_for_segment(
                    session,
                    customer_id=_ft_cid,
                    project_id=doc.project_id,
                    source_locale=src,
                    target_locale=locale,
                    segment_source=source_text,
                    org_id=_ft_org,
                    # THE PRECEDENCE LAW (LAW 3): the whole-document's ALREADY-RESOLVED
                    # approved glossary matches (`terms`, matched once above through the
                    # org/customer-scoped plan), so a memory an approved term already
                    # decides is dropped rather than injected beside it — byte-for-byte the
                    # same veto the segmented path applies. No second, hand-rolled glossary.
                    glossary_terms=terms,
                    mark_used=False,
                )
                # PER RUN: the doc-level note (memoized — select_doc_note runs at most once
                # per run, no matter how many translate steps the run has, gate fix 1).
                _n_blk, _n_ids = _ft_memory_doc_note()
                # THE FOLD DECIDES PROVENANCE (F1): the budget is re-applied to the join,
                # so a line either renderer kept can still be dropped — `_kept` (not
                # `_s_ids + _n_ids`) is what rides into tap_meta AND what is usage-stamped.
                _blk, _kept = prompts.compose_memory_block_kept(_s_blk, _n_blk, _s_ids, _n_ids)
                _ft_mem_block = _blk
                _ft_mem_ids = list(_kept)
                _ft_note_ids = list(_n_ids or [])
            except Exception:
                log.warning("llmemory: FULL_TEXT recall failed", exc_info=True)
        # SAME MASK SEAM the segmented path uses (_run_step_on_segment): a memory body is
        # distilled from real segment text and can quote a raw inline-format token, so it
        # is masked into the SAME «TAG_N» notation as the masked {text}. No-op
        # (byte-identical) when masking is off or the block carries no token. tag_map is
        # per-step-visit here (the whole input masked once), so masking the resolved block
        # once is exactly right.
        if _ft_mem_block and ai_tagging.enabled():
            _ft_mem_block = ai_tagging.mask_like(_ft_mem_block, tag_map)

        # PII: redact the chunk before the engine sees it, restore after. Per-step
        # choice (step.redact_pii) OR the global pii_redaction setting (pii.enabled).
        # Raw PII must never reach the engine, StepRun.output_text, TM, or the target.
        redact = step.redact_pii or pii.enabled()
        # the {source} placeholder is reviewer context, never restored into the
        # output — scrub it too so raw PII cannot reach the engine through it.
        step_ctx = dict(base_ctx)
        # Terminology T3 (SPEC §9-T3 / LAW-TERM-14, residue [8]/[10]/[11]): resolve THIS
        # step's own render config and, when its toggle is ON, replace the {glossary} block
        # with the audience-gated/ordered/capped concept render + compute this step's shown
        # set (published to the run-level home AFTER the produce decision, residue [13]) and
        # the mt-audience-filtered payload glossary. When OFF, step_ctx["glossary"] stays the
        # run-level glossary_line block base_ctx built — byte-identical (LAW-TERM-1).
        _rc = _render_cfg_for(step)
        if _rc["on"] and _wd_concepts is not None:
            _wd_block, _wd_step_shown, _wd_mt_terms = _render_glossary_block(
                _wd_concepts, terms, _rc
            )
            step_ctx["glossary"] = _wd_block
        else:
            # OFF fast path (residue L6): do NOT call the pure-but-wasteful render on a
            # toggle-OFF step. step_ctx["glossary"] stays the run-level glossary_line block
            # base_ctx built, and the payload keeps the full-match `terms` — byte-identical.
            _wd_step_shown, _wd_mt_terms = None, terms
        from app.services.fulltext_tm_offers import REDACTED_STEP_NOTE, render_offers

        # A redacting step gets NO offers. An offer reproduces the document's own sentence
        # verbatim, so emitting it raw hands the engine the PII the line below scrubs out of
        # {text} -- and redacting the offer separately is worse, because `pii.redact` takes
        # no seed map and restarts its placeholder counter per call, so the offer's
        # «PII_EMAIL_1» names a different value than the body's while `pii.restore` resolves
        # against the body's map alone. On a two-PII document that delivers one subject's
        # data inside another subject's sentence. See fulltext_tm_offers.render_offers.
        _offers = (run.context or {}).get("required_renderings") or []
        _offers_withheld = bool(_offers) and redact
        offer_block = "" if _offers_withheld else render_offers(_offers)
        step_ctx["tm_offers"] = offer_block
        offers_suffix = ""
        if offer_block and "{tm_offers}" not in template:
            if "{glossary}" in template:
                step_ctx["glossary"] += "\n\n" + offer_block
            else:
                offers_suffix = "\n\n" + offer_block
        if redact:
            step_ctx["source"], _ = pii.redact(step_ctx.get("source", ""), lang=src)
        # Change 1: the {lqa_findings} substitution value for THIS step + the correction-
        # loop auto-append suffix (both "" until an LQA has run / unless this is a fix
        # step). Constant across chunks, so computed once here.
        step_ctx["lqa_findings"] = lqa_findings_block
        findings_suffix = _findings_suffix(step, steps, template, lqa_findings_block)
        # Mask-hole fix (see _run_step_on_segment): keep the injected findings in the
        # SAME notation as the masked {text} so the prompt never shows two vocabularies
        # for one concept. No-op when masking is off or the findings carry no tokens.
        if ai_tagging.enabled():
            step_ctx["lqa_findings"] = ai_tagging.mask_like(step_ctx["lqa_findings"], tag_map)
            findings_suffix = ai_tagging.mask_like(findings_suffix, tag_map)
        outputs, provider_used, model_used, attempts, err = [], None, None, 0, None
        # run-190 Item C: the last chunk's structured failure SHAPE (finish reason +
        # provider error class), stamped on this step's StepRun so the run view / API can
        # show WHY a step failed without an SSH into the host Ledger.
        ft_finish_reason, ft_error_class = None, None
        # Real per-step-visit spend (feat/workflow-spend): accumulate the EXACT ledger
        # cost + token usage across every chunk's completion call. Single-threaded here
        # (the chunk loop is serial), so plain += is race-free. Stamped on the StepRun
        # below; partial spend on a mid-loop failure is preserved (accumulation happens
        # before any break/except). `res["cost_usd"]` is the same figure record_usage
        # accrued (0.0 on the exempt mock — honest).
        cost_acc, tin_acc, tout_acc = 0.0, 0, 0
        t0 = time.time()
        # GitHub #101: IN-STEP PROGRESS on the FULL_TEXT path. A whole-document step makes a
        # handful of very LONG calls (~6 minutes each on a local engine measured on run 22),
        # so even the Ledger's per-call counter is too coarse to tell working from wedged --
        # the run must publish its own position. Unit is "chunks" here, not segments: that is
        # honestly what this path iterates.
        _prog = _ProgressTicker(run.id, len(chunks), "chunks", step=step)
        _prog.start()
        try:
            for _ci, ch in enumerate(chunks):
                eng_text, pii_map = pii.redact(ch, lang=src) if redact else (ch, {})
                ctx = {**step_ctx, "text": eng_text, "prior": eng_text}
                # GitHub #344: the single llm_agent render seam -- the chunk is fenced in
                # <content>...</content> so a segment like "1." is content, not
                # instruction (the segmented twin does the same in _run_step_on_segment).
                prompt, _ft_fenced = prompts.render_step_prompt(template, ctx)
                # Change 2: always-inject a resolved context / style-guide block the
                # template omitted (topmost); byte-identical when nothing resolved or the
                # placeholder was present. Change 1: append the correction-loop findings.
                prompt = (
                    _inject_missing_prefixes(
                        template, prompt, base_ctx["context"], base_ctx["style_guide"]
                    )
                    + findings_suffix
                    + offers_suffix
                )
                # LLMemory FULL_TEXT recall (Train D §7.2): prepend the once-per-step-visit
                # memory block, on TOP (above the injected context/style-guide prefix),
                # mirroring the segmented path's memory placement. "" -> byte-identical.
                if _ft_mem_block:
                    prompt = _ft_mem_block + "\n\n" + prompt
                # Provenance (§7.7): the injected memory ids ride tap_meta["memory_ids"] on
                # this new path too, attached ONLY when something was injected so every
                # capture an install without memories makes is byte-identical to today.
                _ft_tap_meta = tap_envelope.stamp_meta(
                    {
                        "workflow_id": workflow_id,
                        "workflow_run_id": run.id,
                        "step_key": step.key,
                        "step_name": step.name or step.step_type,
                        "document_id": doc_id,
                    },
                    *_env_ids,
                )
                if _ft_mem_ids:
                    _ft_tap_meta["memory_ids"] = list(_ft_mem_ids)
                # Terminology T3 (LAW-TERM-16, residue [9]): when the toggle is ON, stamp
                # this leg's actual shown set onto the per-call capture record for the
                # adherence audit (schema-flexible JSONL — no migration, no new store). The
                # whole-document leg is a single-segment action, so segment_index is the
                # document's own segment id when resolvable, else None. The `llm`-channel
                # stamp is the capped shown set (correct for this LLM-completion leg).
                if _rc["on"] and _wd_step_shown is not None:
                    _ft_tap_meta["shown_terms"] = list(_wd_step_shown)
                    _ft_tap_meta["target_locale"] = locale
                    _ft_tap_meta["segment_index"] = _ft_segment_id_for_stamp(
                        session, doc_id, locale
                    )
                # GitHub #43: the language pair rides the SYSTEM message on every
                # llm_agent call (no provider reads `payload`), and the notation clause
                # is keyed on THIS unit's actual tokens (`current_text` is the pre-mask
                # working text — the masked branch wins whenever tag_map is non-empty).
                _ft_system = _system_for(
                    tag_map,
                    current_text,
                    src_locale=src,
                    tgt_locale=locale,
                    head_body=_sys_head_body,
                )
                if _ft_fenced:
                    # GitHub #344: the fence sentence rides the system message only on
                    # calls whose prompt actually carries the <content> fence.
                    _ft_system += " " + prompts.CONTENT_FENCE_INSTRUCTION
                if _ft_mem_block:
                    # #362: the block is on this prompt, so the reference-data sentence rides
                    # the system message beside it.
                    _ft_system += " " + prompts.MEMORY_FENCE_INSTRUCTION
                res = pii_boundary.call_with_redaction(
                    redact,
                    run_completion,
                    spec,
                    prompt,
                    system=_ft_system,
                    task="translate",
                    payload={"text": eng_text, "target": locale, "glossary": _wd_mt_terms},
                    tap_meta=_ft_tap_meta,
                )
                cost_acc += res.get("cost_usd") or 0.0
                _prog.tick()  # #101: one whole-document chunk finished
                # QUEUES the persist on the background writer -- no Session, no pool
                # checkout and no wait on this (the translation) thread. See
                # `_flush_progress`.
                _prog.flush()
                _tin, _tout = usage_token_counts(res.get("usage"))
                tin_acc += _tin
                tout_acc += _tout
                # GitHub #344: strip a model-emitted <content> wrapper from the edges
                # before the chunk output is stored / feeds the next step.
                # Strip BEFORE the PII restore so a restored value can never lose an
                # edge marker; `source=eng_text` keeps a genuine edge marker of the source.
                _ft_stripped = prompts.strip_content_fence(res["text"], source=eng_text)
                out_text = pii.restore(_ft_stripped, pii_map) if pii_map else _ft_stripped
                # F9 (review): stamp provider/model/attempts/lang-guard from THIS chunk's
                # result BEFORE the under-translation guard, so a trip on chunk 1 records
                # them on the FAILED StepRun consistently with the spend stamps (and so the
                # refusal-break path below carries them too). Accumulating attempts per
                # chunk is unchanged — only moved ahead of the guard within the iteration.
                provider_used, model_used = res["provider_used"], res["model_used"]
                attempts += res["attempts"]
                # run-190 Item C: carry the last chunk's finish reason + error class.
                ft_finish_reason = res.get("finish_reason")
                ft_error_class = res.get("error_class")
                _ft_lang_guard = _lang_guard_for(res)
                if pii_map and "«PII_" in out_text:
                    # F6 safety net: a mangled/echoed-back placeholder means some PII
                    # could not be restored. Warn (do not fail the run).
                    log.warning("Residual PII placeholder after restore (FULL_TEXT step)")
                # HONEST-FAILURE under-translation guard (2026-08-13 big-doc flood): a
                # chunk whose output is a tiny fraction of its input is not a translation
                # — the sentence-tuned local model emitted ~74 chars for a 9,211-token
                # chunk (char ratio ~0.008-0.016), nonzero so it slipped past the
                # empty/blank guards, and the step committed COMPLETED over an
                # untranslated body. Char-based ratio (NOT usage tokens: prompt_tokens
                # carries template/system/glossary overhead that would depress it
                # dishonestly). `out_text` still carries «TAG_N» sentinels here when
                # AI-tagging is on (restore is post-loop) — immaterial to a char count.
                # Short chunks are EXEMPT (a one-sentence doc legitimately yields one
                # sentence); MIN_RATIO=0 is the kill switch.
                if (
                    config.UNDER_TRANSLATION_MIN_RATIO > 0.0
                    and len(eng_text) >= config.UNDER_TRANSLATION_MIN_INPUT_CHARS
                ):
                    _ut_out = len(out_text.strip())
                    _ut_ratio = _ut_out / max(1, len(eng_text))
                    if _ut_ratio < config.UNDER_TRANSLATION_MIN_RATIO:
                        # F2 (review): the REFUSAL guard owns the refusal class; the ratio
                        # guard owns non-refusal babble. A tiny output that IS a hedge/
                        # refusal must flow to the existing per-step refusal guard (withheld
                        # disposition + NEEDS_REVIEW routing + the T1-01 missing-{text} hint),
                        # NOT be reclassified as under-translation. So on a hedge, stop the
                        # chunk loop with the hedge AS the output and let `step_refused` below
                        # claim it exactly as on main; only a NON-refusal short output raises
                        # under-translation. (is_refusal is position-anchored, so it reads the
                        # hedge lead regardless of any «TAG_N» sentinels further in.)
                        if config.REFUSAL_GUARD_ENABLED and refusal.is_refusal(out_text):
                            outputs = [out_text]
                            break
                        # run-190 Item B (3): when the chunk produced ~nothing BECAUSE the
                        # provider FAILED (a ThinkingBurn/ProviderTruncated returning empty
                        # content), the honest message is the PROVIDER's reason, not "the
                        # engine emitted far too little output" — that phrasing described a
                        # model that babbled a too-short answer, and it actively misled the
                        # run-190 diagnosis (the model emitted 0 chars because its reasoning
                        # consumed the budget). Route res["error"]/finish_reason into the
                        # message whenever the last provider outcome for this chunk carried
                        # a failure; otherwise keep the classic too-little-output wording.
                        _prov_reason = res.get("error")
                        _prov_finish = res.get("finish_reason")
                        if _prov_reason:
                            raise UnderTranslationError(
                                "under-translation: step '%s' (%s->%s) chunk %d/%d - the "  # noqa: UP031
                                "provider returned no usable translation (%d input chars "
                                "produced %d output chars%s): %s"
                                % (
                                    step.name or step.step_type,
                                    src,
                                    locale,
                                    _ci + 1,
                                    len(chunks),
                                    len(eng_text),
                                    _ut_out,
                                    (f", finish_reason={_prov_finish}" if _prov_finish else ""),
                                    _prov_reason,
                                )
                            )
                        raise UnderTranslationError(
                            "under-translation: step '%s' (%s->%s) chunk %d/%d - the "  # noqa: UP031
                            "engine emitted far too little output for a translation: "
                            "%d input chars produced %d output chars (ratio %.4f, floor "
                            "%.2f). Retry the run or check the model; the ratio floor is "
                            "configurable via UNDER_TRANSLATION_MIN_RATIO (0 disables)."
                            % (
                                step.name or step.step_type,
                                src,
                                locale,
                                _ci + 1,
                                len(chunks),
                                len(eng_text),
                                _ut_out,
                                _ut_ratio,
                                config.UNDER_TRANSLATION_MIN_RATIO,
                            )
                        )
                outputs.append(out_text)
                err = res["error"] or err
        except DeadEngineError as exc:
            # A dead engine:<id> ref -> a step error (naming the ref), flowing through
            # the run's normal FAILED path rather than a raw propagation or silent mock.
            # Prefixed with step/pair context (finding 4) so a bare provider message
            # isn't ambiguous about which step/pair in a multi-step run failed.
            err = f"step '{step.name or step.step_type}' ({src}->{locale}): {exc}"
        except UnderTranslationError as exc:
            # Honest-failure trip: the message already carries step/pair/chunk/ratio
            # context. Discard any chunks stitched before the trip so `output == ""` ->
            # step_failed below -> the run FAILS honestly, never committing a partial
            # (mostly-untranslated) stitched body. cost_acc/tin_acc/tout_acc accrued so
            # far are preserved on the FAILED StepRun (stamped below).
            err = str(exc)
            outputs = []
        # #101: STEP BOUNDARY -- stamp where the chunk loop actually reached onto the run's
        # OWN ORM object (it rides this step's commit; no second connection) and drop the live
        # counter. Not advanced to total: a loop that tripped keeps its honest 2-of-5.
        _prog.finish(session, run)
        output = "\n\n".join(outputs)
        # LLMemory FULL_TEXT usage stamp (Train D §7.2/E20), DEFERRED to here — after the
        # chunk loop's provider calls — so the run session never holds a write lock while a
        # provider call writes the spend ledger on its own connection (SQLite "database is
        # locked"). Mirrors the segmented `_mem_stamp`: term memories are stamped per
        # step-visit; the doc note is stamped at most ONCE per run, on the first step-visit
        # whose fold KEPT it (the note latch), so a multi-step run does not turn `use_count`
        # into a step counter (gate fix 1). Staging-only (mark_used_ids) — it rides out on
        # the step's own commit and is lost if the step fails and rolls back (the accepted
        # trade: a rolled-back run did not use the memory).
        if _ft_mem_ids:
            _ft_note_set = set(_ft_note_ids)
            _ft_term_kept = [i for i in _ft_mem_ids if i not in _ft_note_set]
            if _ft_term_kept:
                llmemory.mark_used_ids(session, _ft_term_kept)
            _ft_note_kept = [i for i in _ft_mem_ids if i in _ft_note_set]
            if _ft_note_kept and not _ft_mem_note_state[3]:
                _ft_mem_note_state[3] = True
                llmemory.mark_used_ids(session, _ft_note_kept)
        # AI Tagging 1.1 restore + 1.5 inter-step verification: restore the ⟦⟧ tokens
        # from the sentinels BEFORE the output feeds the next step / write gate (so every
        # downstream check sees real tokens, never «TAG_N»). Then, if the step damaged the
        # tokens vs its INPUT (current_text), try the cheap deterministic repair rung
        # (1.5b) FIRST so recoverable damage is fixed at the step that caused it and never
        # flows downstream. Unrepairable damage falls through — the write gate
        # (commit_mt_output) is the backstop that quarantines it. No-op when no token.
        if tag_map:
            output, _tag_resid = ai_tagging.restore(output, tag_map)
            if quality.tag_damage(current_text, output):
                rep = tag_repair.repair(current_text, output)
                if rep is not None:
                    output = rep[0]  # use the repaired text; the commit gate re-flags
                    # a best-guess (non-clean) repair for review
        # Change 3: conservative output-hygiene strip of a leading meta line (e.g. a fix
        # step's "# Revised Target Text" heading) BEFORE the output feeds the next step /
        # the refusal check / the write-back. No-op on the common case (MockProvider echo,
        # a real translation) — see refusal.strip_meta_preamble.
        output, hygiene_line = refusal.strip_meta_preamble(output, source_text)
        latency = int((time.time() - t0) * 1000)

        # LQA safety floor (C), FULL_TEXT arm: did THIS step introduce a glossary/DNT
        # violation the text it was editing did not have? Computed against the run-level
        # matched `terms` (resolved once, org/customer-scoped) using the SAME matchers
        # the write-back gate runs. Skipped when the step produced nothing (a failure is
        # discarded anyway) and when `current_text` is still the untranslated SOURCE —
        # a DNT term is present in its own source by definition, so a delta against the
        # source would accuse the FIRST translating step of breaking every DNT term.
        # FLAG ONLY: `output` is fed forward unchanged (no whole-document revert).
        # Terminology T3 (LAW-TERM-15 (i), residue [8]/[13]/[14]): enforce against the
        # PRIOR producer's shown set when it had the toggle ON (`_wd_shown_terms`), else the
        # full run-level match `terms` exactly as today. `_wd_shown_terms` still holds the
        # prior producer's set here because this step publishes its own only AFTER the
        # produce decision below — so a toggle-ON Translate followed by a toggle-OFF
        # correction can no longer manufacture a regression flag for a capped-out/human-only
        # term the model was never shown. This arm is FLAG-ONLY (no whole-document revert).
        ft_step_regression = None
        _ft_enforce_terms = _wd_shown_terms if _wd_shown_terms is not None else terms
        if output and lqa_safety.fix_guard_enabled() and current_text != source_text:
            _ft_dnt, _ft_gloss = lqa_safety.introduced_violations(
                source_text, current_text, output, _ft_enforce_terms, target_locale=locale
            )
            if _ft_dnt or _ft_gloss:
                ft_step_regression = lqa_safety.regression_flag(
                    step.name or step.step_type, _ft_dnt, _ft_gloss
                )
                ft_regressions.append(ft_step_regression)

        # A step FAILED if it produced no usable output at all (a dead engine:<id>
        # ref -> err set + no outputs; a genuine provider outage -> run_completion's
        # error dict + text=""; an under-translation trip -> err set + outputs cleared).
        # Every "" here carries an err set at the same seam (the DeadEngine /
        # UnderTranslation excepts, or run_completion's error dict): there is no code
        # path where a step legitimately produces "" WITHOUT an error (chunks is always
        # non-empty, so at least one run_completion call always runs and either returns
        # real text, an error, or trips the under-translation guard), so this stays
        # exactly as strict as the pre-existing StepRun.status check — only now the
        # run-level status (below, after the loop) mirrors it too, instead of
        # unconditionally reporting COMPLETED with a blank target.
        step_failed = not output
        if step_failed:
            any_step_failed = True
            fail_err = err or fail_err
        # Terminology T3 (LAW-TERM-15 (i), residue [8]/[13] + M5): after the produce
        # decision, a toggle-ON producing step PUBLISHES its shown set to the run-level home
        # (last toggle-ON producer wins) and a toggle-OFF producing step RESETS the home to
        # None so its own terminal write-back and every later consumer enforce the FULL
        # run-level match — LAW-TERM-15 as written ("otherwise ... it uses the full
        # match_terms_merged result exactly as today"). A failed step never touches the home
        # (it produced nothing). The FULL_TEXT regression arm above already read the PRIOR
        # producer's set (residue [13] ordering), so this step's own arm judged against the
        # right set before this reset takes effect.
        if not step_failed:
            _wd_shown_terms = _wd_step_shown if (_rc["on"] and _wd_step_shown is not None) else None
        # C1/C4: per-step refusal guard. Run the check on THIS step's output BEFORE it
        # (a) becomes the next step's {text} input — so an early-step hedge cannot be
        # laundered into clean text by a later finalizer — and (b) is persisted to
        # StepRun.output_text / streamed on the `step` event — so the run trace shows
        # the withheld disposition, not the raw hedge (run_view.html renders it and it
        # persists in history). On a hit we route the unit to NEEDS_REVIEW at the post-
        # loop write-back (refused=True) and STOP the loop: no further step runs, so
        # the hedge is never fed forward. Same kill switch as every other call site.
        step_refused = (
            not step_failed and config.REFUSAL_GUARD_ENABLED and refusal.is_refusal(output)
        )
        persist_output = refusal.WITHHELD_OUTPUT if step_refused else output
        # T1-01 defense-in-depth: a step whose non-empty prompt lacks {text}/{prior}
        # sent the model NO source, so its output reads as a refusal and gets withheld —
        # a generic "model refusal" disposition that hides the real cause. When THAT is
        # why this step was withheld, put a specific, actionable hint on the run trace
        # (StepRun.error) instead. It does NOT fail the step (the run still completes and
        # routes to review, preserving the refusal-guard contract) — it only renames the
        # disposition so the operator sees the footgun.
        # SPEC-prompt-template-runtime: a dangling/invalid bound-template fallback note
        # takes the slot next after a provider error (an honest run-trace signal that the
        # step ran the DEFAULT instead of its saved template). It cannot co-occur with the
        # missing-source hint (that fallback used the default, which carries {text}).
        step_note = err or tmpl_note
        # #155: the withheld-offers note is the LOWEST-priority note there is. The fact it
        # carries now rides on the run-level `tm_offers` message, which every surface reads,
        # so this is a convenience for the run trace and must never displace telemetry that
        # exists nowhere else -- the hygiene strip, and above all the LQA safety floor's
        # terminology regression. The first version of this outranked both.
        if _offers_withheld and not err and not tmpl_note:
            step_note = REDACTED_STEP_NOTE
        # Change 3: record the hygiene strip honestly on the run trace (only when nothing
        # else already claims the note — a provider error / template note / missing-source
        # hint win).
        if hygiene_line and not err and not tmpl_note:
            step_note = f"{HYGIENE_NOTE}: {hygiene_line[:60]}"
        if (
            step_refused
            and step.step_type == StepType.LLM_AGENT
            and (step.prompt_text or "").strip()
            and not prompts.has_source_placeholder(step.prompt_text)
        ):
            step_note = prompts.MISSING_SOURCE_HINT
        # LQA safety floor telemetry (FULL_TEXT): the run trace names the step that
        # broke a term, even though the whole-document output is kept. Outranks only the
        # cosmetic hygiene note; a provider error / template note / missing-source hint
        # still win. Byte-identical when no regression was detected.
        if ft_step_regression is not None and (
            not step_note
            or step_note.startswith(HYGIENE_NOTE)
            or step_note == REDACTED_STEP_NOTE  # #155: the withheld note yields to this
        ):
            step_note = (
                "LQA safety floor: step introduced a terminology violation "
                "(whole-document output kept, review required)"
            )
        session.add(
            StepRun(
                workflow_run_id=run.id,
                step_id=step.id,
                status=RunStatus.COMPLETED if output else RunStatus.FAILED,
                **_sys_stamp,
                input_text=current_text,
                output_text=persist_output,
                provider_used=provider_used,
                model_used=model_used,
                attempts=attempts,
                error=step_note,
                latency_ms=latency,
                # Real per-step-visit spend (feat/workflow-spend): the EXACT ledger cost +
                # tokens this visit accrued across its chunk calls. A step that actually ran
                # a completion always stamps (0.0 on the exempt mock — an honest zero, never
                # NULL), so the reports workflow axis reads this run as tracked.
                cost_usd=cost_acc,
                tokens_in=tin_acc,
                tokens_out=tout_acc,
                # run-190 Item C: the per-step failure SHAPE (metadata only) — the last
                # chunk's finish reason + provider error class, and the chars this visit
                # actually produced (0 on a thinking burn / failed step).
                finish_reason=ft_finish_reason,
                error_class=ft_error_class,
                output_chars=len(output or ""),
                # Phase 1 durable signal (SPEC §3.2): a plain FULL_TEXT llm_agent step
                # produces no run-level outcome (QE is segmented-only) -> score/gate stay
                # NULL (model defaults). outcome_index is this step's 0-based visit number
                # (0 on a linear run; increments on a loop revisit).
                outcome_index=guard.visit_index(step),
                started_at=step_started_at,
                finished_at=datetime.utcnow(),
            )
        )
        run.current_step_index = cursor + 1
        session.add(run)
        session.commit()
        current_text = output
        yield {
            "type": "step",
            "run_id": run.id,
            "step_index": cursor,
            "step_name": step.name or step.step_type,
            "step_type": step.step_type,
            "provider": provider_used,
            "model": model_used,
            "chunks": len(chunks),
            "attempts": attempts,
            "error": step_note,
            "output": persist_output,
            **(
                {"lqa_safety": {"reverted": 0, "term_regression": 1}}
                if ft_step_regression is not None
                else {}
            ),
        }
        if step_refused:
            # Caught the hedge at the producing step — stop laundering it downstream.
            refused = True
            break
        if step_failed:
            # F3 (review): fail fast. A FAILED step (dead engine / provider outage /
            # under-translation trip -> output "") must NOT flow into later steps with
            # current_text="". Without this break a human_review step AFTER a failed step
            # would PAUSE, and a later resume would finalize the run COMPLETED with a
            # blank target and a FAILED StepRun buried in the trace. `any_step_failed` is
            # already set, so the terminal status below is FAILED; break so the failure is
            # terminal at once and never reaches a pause seam.
            break
        # FULL_TEXT llm_agent produces no gate (QE is segmented-only), so this always
        # falls through to cursor+1 (branch routing lives on the LQA block above).
        cursor = _next_cursor(step, cursor)

    # Review F1b, the FULL_TEXT half of the same guard: if a recovery sweep already
    # failed this row while this thread was executing, a replacement run owns the
    # document now. Write nothing and finish quietly rather than resurrecting the row
    # as COMPLETED over the replacement's target text.
    if _abort_if_superseded(session, run):
        session.rollback()
        yield {
            "type": "error",
            "run_id": run.id,
            "error": "This run was superseded by a recovery sweep while it was "
            "still executing; its results were discarded.",
        }
        return
    # F2 (mirrors the segmented path): the run is only genuinely COMPLETED if every
    # step actually produced output. Any FAILED StepRun above -> the run is FAILED
    # too, so the API/CLI report {"ok": true, "status": "failed"} instead of a
    # silent success-lie with a blank target.
    # LOW-1 (run-cancel train): the terminal status is flipped via the CAS at the commit
    # below (_commit_run_terminal), NOT dirtied on the ORM here -- an unconditional ORM
    # flush would clobber a concurrent CANCELLED before the guard could see it.
    _final_status = RunStatus.FAILED if any_step_failed else RunStatus.COMPLETED
    # STATUS PROTECTION (FULL_TEXT): set when the body segment is human-approved/edited and the
    # write-back seam refused the overwrite, so the terminal `done` event can surface it.
    ft_status_protected = False
    # ISSUE #107, round-1 LOW-3: did THIS run actually COMMIT a body? False on the vendor-RETURN
    # slot (the verdict owns the stored text), on a status-protected refusal (the human's body
    # stands) and on a quarantined/refused output (withheld). The paragraph-count warning below
    # is TRUE of the stored body either way, but only a run that wrote the body may attribute the
    # desync to "the engine".
    _ft_wrote_body = False
    # write the final text back so export / editor can use it (full-text docs) —
    # ONLY on a successful run; a FAILED run must not write a blank/partial target
    # (mirrors the segmented path's write-back-only-on-success guarantee).
    # F5: `refused` (hoisted above the loop) tracks whether the guard withheld
    # current_text from the DB, so the terminal `done` event (below) can mirror the
    # write-back instead of re-emitting the raw refusal hedge that was never committed.
    # An offer-only run has no translated body to commit. In particular, do not
    # turn its unchanged source into a target at finalization.
    offer_only = (
        "tm_offers" in (run.context or {})
        and not session.exec(
            select(StepRun.id)
            .join(WorkflowStep, WorkflowStep.id == StepRun.step_id)
            .where(
                StepRun.workflow_run_id == run.id,
                StepRun.status == RunStatus.COMPLETED,
                WorkflowStep.step_type.in_(
                    [StepType.LLM_AGENT, StepType.HUMAN_REVIEW, StepType.EXTERNAL]
                ),
            )
        ).first()
    )
    if doc.mode == TransMode.FULL_TEXT and not any_step_failed and not offer_only:
        seg = session.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).first()
        if seg and 0 in _vr_skip:
            # F-CRIT (FULL_TEXT): a vendor RETURN's whole-body slot whose verdict is
            # authoritative (no-op / refusal / hard-lock). Leave target_text + status
            # exactly as the admission left them; the terminal `done` event carries the
            # stored target (current_text was reseeded from it by the return).
            current_text = seg.target_text or current_text
        elif seg:
            # Refusal / meta-output GUARD (see the segmented write-back for the full
            # rationale). C1: `refused` is already True if the per-step guard caught a
            # hedge at an EARLIER step (and broke the loop) — a later finalizer never
            # ran to launder it. The final-step re-check stays as defense in depth (a
            # single-step run, or a hedge that first appears at the last step). Either
            # way a conversational refusal is routed to review, never committed.
            # Refusal / meta-output GUARD via the sanctioned CHOKE POINT (arch-review
            # Finding 2). `already_refused=refused` carries the per-step C1 signal so
            # the seam quarantines an earlier-caught hedge without re-running the
            # detector; otherwise it runs is_refusal(current_text). `set_status=False`
            # preserves this path's exact pre-seam behavior — the FULL_TEXT commit
            # branch wrote target_text WITHOUT changing seg.status.
            # GitHub #43 (C1): opt into the INDEPENDENT language-mismatch signal, gated
            # on a LIVE provider exactly as translate._lang_guard_for does — an English
            # echo from a real engine must route to review, and the offline mock's
            # deliberate English echo must NOT (several suites depend on mock output
            # committing clean).
            fx = quality.commit_mt_output(
                session,
                seg,
                current_text,
                source="workflow",
                already_refused=refused,
                set_status=False,
                # F7/F1 (FULL_TEXT): an operator edit made on an
                # approved/edited body during a human_review pause is
                # HUMAN content -- carry it through the human_edit
                # channel so the status gate does not refuse it (the
                # edit lands; set_status=False keeps the status). False
                # on every fresh / no-edit run -> the seam refuses and
                # ft_status_protected below surfaces it.
                human_edit=_ft_operator_edited,
                tm_eligible=(0 not in _vr_tm_inelig),
                # Terminology T3 (LAW-TERM-15 (i), consumer 1): the whole-document write-back
                # enforces against the last toggle-ON producing step's shown set, else the
                # full run-level match exactly as today (byte-identical when no step is ON).
                glossary_terms=(_wd_shown_terms if _wd_shown_terms is not None else terms),
                lang_guard=_ft_lang_guard,
                # reviewer H1: a full-text body is committed
                # verbatim (raw provider output keeps trailing
                # "\n"; paragraphs join "\n\n"), so exclude it
                # from the whitespace seam gate — else every real
                # full-text delivery routes to review on trailing
                # whitespace. Other gates still run.
                full_text=True,
                llm_tag_repair=(
                    tag_llm_repair.make_repairer(
                        src_locale=src,
                        tgt_locale=locale,
                        spec=_tag_spec,
                        redact=_tag_redact,
                        verify_spec=_tag_verify_spec,
                        spend_sink=_tag_spend,
                        tap_meta=tap_envelope.stamp_meta(
                            {
                                "workflow_id": workflow_id,
                                "workflow_run_id": run.id,
                                "document_id": doc_id,
                            },
                            *_env_ids,
                        ),
                    )
                    if _tag_spec is not None
                    else None
                ),
            )
            # #107 round-1 LOW-3: the seam WROTE the body unless it protected the human's text
            # or quarantined the output. Read straight off the seam's own verdict, so a new
            # withhold reason added there cannot silently re-open the false attribution.
            _ft_wrote_body = not (fx.status_protected or fx.flagged)
            if fx.status_protected:
                # STATUS PROTECTION (FULL_TEXT): the single body segment is human-approved/
                # edited, so the seam refused the overwrite and the stored target stands.
                # Reflect it in the terminal event (current_text may hold the withheld MT) and
                # mark it protected; do NOT set `refused` (this is not a hedge) nor route to
                # review -- the human's target is authoritative and byte-untouched.
                current_text = seg.target_text or current_text
                ft_status_protected = True
            elif fx.flagged:
                refused = True
            elif _ft_operator_edited:
                # PART 1 (FULL_TEXT pause-edit protection): the operator edited the body during a
                # human_review pause. The commit above used set_status=False + human_edit=True, so
                # it wrote the target but left the status at the PRE-run value -- which for an
                # approved body is the round-3 LOW-1 defect (the operator's edit shipped labelled
                # `approved`, a false sign-off). Land the operator's WORK coherently instead:
                #   * the edit is STILL the final body (current_text == the captured edit) -> the
                #     operator's edit WINS: SegStatus.EDITED, protected from the next run's machine
                #     passes (NOT approved, NOT mt);
                #   * a step AFTER the pause re-ran over it (current_text != the captured edit) ->
                #     it is machine output: SegStatus.MT (the in-run boundary, mirroring the
                #     segmented arm) -- never left falsely labelled approved.
                # F1/F2 carve-outs (segmented twin): a BLANKED body resets to NEW (the clear-to-
                # reset escape hatch -- never protect an empty body); a TAG-BREAKING body routes to
                # NEEDS_REVIEW with its badge (the seam wrote it; set_status=False left it there).
                if not (current_text or "").strip():
                    seg.status = SegStatus.NEW
                    # LOW: clear the spurious tag_integrity badge a blanked tagged body picks up
                    # from the seam (a flag on text that no longer exists), as save_paragraph does.
                    # reviewer H2b: the placeholder / whitespace families need the SAME clear —
                    # a blanked body must not keep a MAJOR "placeholder dropped" badge (though
                    # placeholder_violation now abstains on empty text, an earlier commit's badge
                    # can still be present) or a stale whitespace badge.
                    seg.qa_flags = quality.clear_tag_flags(seg.qa_flags)
                    seg.qa_flags = quality.clear_placeholder_flags(seg.qa_flags)
                    seg.qa_flags = quality.clear_whitespace_flags(seg.qa_flags)
                    seg.qa_flags = quality.clear_review_hold_flags(seg.qa_flags)
                # reviewer M2: honour the placeholder (and whitespace) verdict here too. The
                # seam set NEEDS_REVIEW, but this operator-edit branch RE-SETS status to
                # EDITED/MT and would otherwise ship an operator edit that dropped a
                # placeholder as clean (status lies). whitespace is consulted defensively:
                # full_text=True (above) makes it always "" for this body, so reading it here
                # removes the cross-line coupling on that flag rather than assuming it.
                elif (
                    fx.tag_violation
                    or fx.placeholder_violation
                    or fx.whitespace_violation
                    or fx.review_hold
                ):
                    seg.status = SegStatus.NEEDS_REVIEW
                elif _ft_edit_text is not None and current_text == _ft_edit_text:
                    seg.status = SegStatus.EDITED
                else:
                    seg.status = SegStatus.MT
                session.add(seg)
            # LQA safety floor (C), FULL_TEXT arm: attribute every terminology
            # regression a step introduced. The whole-document text was NOT reverted
            # (§4.C), so the flag is the deliverable: the write-back's absolute
            # glossary/DNT gates already flagged the violation and routed to review, but
            # only this says WHICH step broke it. Skipped on a quarantined (refused)
            # unit — its target was withheld, so attributing an edit to it would be
            # noise. merge_flags preserves every other family the commit seam wrote.
            if ft_regressions and not fx.flagged:
                seg.qa_flags = lqa_safety.merge_flags(seg.qa_flags, ft_regressions)
                seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
            # ISSUE #95 (gate C round 1, H1) -- THE GATE HOLD, FULL_TEXT arm. Identical
            # rule to the segmented write-back: a run whose correction loop ended with its
            # gate still FAILING does not deliver the body as finished work. The body
            # segment carries the CRITICAL gate-hold badge, lands NEEDS_REVIEW, and
            # `export.gate_held_reason` refuses the export until a reviewer approves it.
            # The `elif` is the stale-badge half -- and, as on the segmented twin (round 2,
            # H1), only a later run whose OWN gate PASSED may clear the badge. Skipped for
            # a status-protected body -- a human's approved text is authoritative and this
            # run never overwrote it.
            _ft_gate_hold = _terminal_gate_hold(session, run, steps)
            # ROUND 3, H1: the human-authored dispositions this write-back may NEVER
            # re-assert over. `ft_status_protected` is the seam's refusal (a body this run
            # never overwrote); `seg.status` catches the same disposition when the seam was
            # not the one that saw it. A reviewer's APPROVED body flipped back to
            # `needs_review` by an ordinary later run REVOKES a ratified release; an EDITED
            # body demoted that way loses its protection and the NEXT run's MT overwrites
            # the linguist's text outright (measured: two runs from finished to gone).
            _ft_human_authored = ft_status_protected or seg.status in (
                SegStatus.APPROVED,
                SegStatus.EDITED,
            )
            if _ft_gate_hold and not ft_status_protected:
                seg.qa_flags = quality.merge_gate_hold_flags(
                    seg.qa_flags,
                    [
                        quality.gate_hold_flag(
                            _ft_gate_hold.get("step_name") or "",
                            _ft_gate_hold.get("message") or "",
                            _ft_gate_hold.get("cause") or "",
                            _ft_gate_hold.get("family") or "",
                        )
                    ],
                )
                # ROUND 4, H1 -- THE HOLD-ASSERT ARM RESPECTS HUMAN WORK TOO. Round 3
                # guarded only the re-assertion arm below, so an operator's PAUSE-EDIT
                # (resumed under a check that then failed: status EDITED, written through
                # the seam with human_edit=True, so `ft_status_protected` is False) was
                # demoted here to NEEDS_REVIEW -- and NEEDS_REVIEW is the system's word for
                # "overwritable", so ONE ordinary later run replaced the linguist's body
                # with fresh MT. Measured, and a REGRESSION: main carries no hold at all,
                # so that edit used to survive. The badge STAYS on the unit (the hold is
                # true and the reviewer should see it) and the run-level record is
                # untouched; what does not happen is the demotion, so the body keeps its
                # status protection. `export._gate_held_reason_for_docs` skips APPROVED/
                # EDITED rows, so a human-authored unit does not block delivery either --
                # the human review this hold waits for already happened.
                if not _ft_human_authored:
                    seg.status = SegStatus.NEEDS_REVIEW
                session.add(seg)
            elif quality.has_gate_hold_flag(seg.qa_flags):
                # ROUND 3, M1: released only by a check that passed over THIS body, not by
                # a check that passed somewhere earlier in the run.
                # ROUND 4, M2: in the scope of the family that held this unit.
                if _gate_release_mask(
                    session,
                    run,
                    [current_text],
                    held_families=[quality.gate_hold_families(seg.qa_flags)],
                )[0]:
                    seg.qa_flags = quality.clear_gate_hold_flags(seg.qa_flags)
                    # ROUND 4, L4: land the RELEASE the way the segmented twin does. There
                    # the MT/TM status lines have already reset the slot to `mt` before the
                    # badge is cleared; here nothing did, so a body that had just PASSED its
                    # check and downloaded at 200 still read `needs_review` with nothing on
                    # it saying why -- the exact inverse of the drift the re-assertion arm
                    # exists to prevent. Narrow on purpose: only a unit whose sole remaining
                    # reason to be in review WAS the badge (no other flag survives the
                    # clear, no violation this run, not human-authored) is reset.
                    if (
                        seg.status == SegStatus.NEEDS_REVIEW
                        and not _ft_human_authored
                        and not fx.flagged
                        and not seg.qa_flags
                    ):
                        seg.status = SegStatus.MT
                    session.add(seg)
                elif not _ft_human_authored:
                    # The badge survived (see the segmented twin): keep the status honest.
                    # ROUND 3, H1: except on a human-authored body -- a reviewer's approval
                    # or a linguist's edit is not a status this run gets to overwrite. The
                    # badge stays (it is stale, not a lie), and `_gate_held_reason_for_docs`
                    # already skips APPROVED/EDITED rows, so the release the reviewer earned
                    # stands.
                    seg.status = SegStatus.NEEDS_REVIEW
                    session.add(seg)
        # ISSUE #107 -- THE PARAGRAPH-COUNT DESYNC, SURFACED AT WRITE-BACK. The exporter has
        # always compared the produced body against the source skeleton and quarantined a
        # count desync; nothing ran that comparison at the END OF THE RUN. So a FULL_TEXT run
        # over an 8.2k-word document whose model split 111 source paragraphs into 120 reported
        # `completed`, `warnings: []`, `finish_reason: stop`, the document read "translated
        # 1/1, 0 empties", and the ONLY surface that knew the body was unusable was the
        # download -- 30 minutes later, and its "Re-run the translation" advice sent the
        # operator into another 30-minute run with the same deterministic outcome.
        #
        # The check is a pure READ of the SHARED `align_parts` predicate (via
        # `fulltext_paragraphs.count_desync`), so it can never disagree with the exporter's
        # guard, and it is recorded as a run-level WARNING on run.context -- the seam the
        # FULL_TEXT tm_match no-op already uses, which every surface renders (web run view,
        # GET /api/runs/{id}, `workflow show-run`, MCP over the /api route). The run status is
        # deliberately NOT flipped: the body is written, an operator may still edit or revert
        # paragraphs, and the exporter remains the fail-closed door. Blank / skeleton-less
        # bodies return None (their own guards own them), so a clean run's context is
        # byte-identical to before.
        if seg is not None:
            from app.services import fulltext_paragraphs as _ftp

            _ft_desync = _ftp.count_desync(
                seg.target_text or "", (doc.skeleton or {}).get("units") or []
            )
            if _ft_desync is not None:
                _ft_more = _ft_desync["desync"] == "over"
                # ROUND-1 LOW-3 -- ATTRIBUTION, SCOPED. The COUNT is true of the stored body on
                # every branch (and the export will quarantine it either way, which is what the
                # operator needs to know), but "the engine split / merged or dropped paragraphs"
                # is only true where this run committed the text. On the vendor-RETURN,
                # status-protected and quarantined branches the body is someone else's, so the
                # sentence states the fact and names the owner instead of blaming this run's
                # engine for text it never wrote.
                if _ft_wrote_body:
                    _ft_cause = (
                        "the engine "
                        + ("split" if _ft_more else "merged or dropped")
                        + " paragraphs"
                    )
                    _ft_advice = (
                        "Re-running usually reproduces it with the same model; edit the "
                        "full-text body, or translate the document in segmented mode."
                    )
                else:
                    _ft_cause = (
                        "the STORED body has "
                        + ("more" if _ft_more else "fewer")
                        + " paragraphs than the source"
                    )
                    _ft_advice = (
                        "This run did not write the body (it is human-approved/edited, "
                        "quarantined, or owned by a vendor return), so the desync predates it: "
                        "edit the full-text body, or translate the document in segmented mode."
                    )
                _ft_warnings = list((run.context or {}).get("warnings") or [])
                # De-duped on the code (one body, one verdict) so a resume cannot stack it.
                if not any(w.get("code") == "fulltext_paragraph_desync" for w in _ft_warnings):
                    _ft_warnings.append(
                        {
                            "code": "fulltext_paragraph_desync",
                            "message": (
                                "FULL_TEXT paragraph count desync: the translated body has "
                                f"{_ft_desync['parts']} real paragraph(s), the source has "
                                f"{_ft_desync['units']} - {_ft_cause}, so the positional map "
                                "is untrustworthy and the export of this locale will be "
                                f"QUARANTINED until the body is fixed. {_ft_advice}"
                            ),
                            "parts": _ft_desync["parts"],
                            "units": _ft_desync["units"],
                            "desync": _ft_desync["desync"],
                            # Machine-readable half of the attribution scoping (LOW-3).
                            "written_by_this_run": bool(_ft_wrote_body),
                        }
                    )
                run.context = {**(run.context or {}), "warnings": _ft_warnings}
                session.add(run)
    # LOW-1: CAS the terminal flip + the staged FULL_TEXT write-back as one atomic unit.
    # If a cancel (or a recovery sweep) made the run terminal first, discard the write-back
    # rather than resurrect it to COMPLETED.
    _stamp_tag_repair_spend(session, run, _tag_spend)
    _ft_terminal_ok = _commit_run_terminal(session, run, _final_status)
    _tag_spend[3] = _ft_terminal_ok
    if not _ft_terminal_ok:
        yield {
            "type": "error",
            "run_id": run.id,
            "error": "This run was cancelled or superseded while it was finishing; its "
            "results were discarded.",
        }
        return
    _mark_project_completed(session, doc_id)
    if any_step_failed:
        yield {
            "type": "error",
            "run_id": run.id,
            "error": fail_err or "workflow step produced no output",
        }
    else:
        # F5: never let a refused unit's raw hedge leak into the emitted `done` event
        # — the DB write-back above already withheld it (seg.target_text left unset,
        # routed NEEDS_REVIEW instead), so the terminal event must mirror that rather
        # than re-emitting `current_text` (the refusal text itself) verbatim.
        done_text = "" if refused else current_text
        if offer_only:
            stored = session.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == locale
                )
            ).first()
            done_text = (stored.target_text or "") if stored else ""
        # LQA safety floor rollup, attached ONLY when a step broke a term OR the body was
        # status-protected (an untouched run's `done` shape is byte-identical). FULL_TEXT never
        # reverts, so `reverted` is structurally 0 here. F14 (adversarial review): whenever the
        # rollup IS attached, carry the standard FULL_TEXT keys (reverted + term_regression),
        # ZEROED as needed, so a status_protected-only run does not emit a shape MISSING those
        # keys -- avoiding drift a consumer reading `lqa_safety.reverted` would trip on.
        # `status_protected` (0/1 -- one body segment) surfaces the human-work protection the
        # same way the segmented path does.
        _ft_ls = {}
        if ft_regressions or ft_status_protected:
            _ft_ls = {"reverted": 0, "term_regression": len(ft_regressions)}
            if ft_status_protected:
                _ft_ls["status_protected"] = 1
        yield {
            "type": "done",
            "run_id": run.id,
            "text": done_text,
            **({"lqa_safety": _ft_ls} if _ft_ls else {}),
        }


def run_workflow_stream_segmented(
    session: Session,
    workflow_id: int,
    doc_id: int,
    locale: str,
    run_id: int | None = None,
    seed_text: str | None = None,
    created_by: str = "",
    is_resume: bool | None = None,
):
    """Run-envelope FINALIZING WRAPPER over the segmented execution generator (L3).

    ~12 suites (and any future caller) invoke run_workflow_stream_segmented DIRECTLY,
    bypassing run_workflow_stream, so it needs its own finalizer or those runs' always-on
    envelopes would be left stuck 'running'. Same shape as run_workflow_stream's wrapper
    (remember the run id, resolve the observed outcome, finalize in a finally). The
    DELEGATION path (run_workflow_stream -> its impl) calls the segmented IMPL below
    instead, so the envelope is finalized exactly once."""
    _wf_run_id = None
    _exc = False
    _abandoned = False
    from app.services import ai_optout as _ai_optout

    try:
        for _ev in _ai_optout.scoped_stream(
            _run_workflow_stream_segmented_impl(
                session,
                workflow_id,
                doc_id,
                locale,
                run_id=run_id,
                seed_text=seed_text,
                created_by=created_by,
                is_resume=is_resume,
            ),
            document_id=doc_id,
        ):
            if isinstance(_ev, dict) and _ev.get("type") == "run_started":
                _wf_run_id = _ev.get("run_id")
            yield _ev
    except GeneratorExit:
        _abandoned = True
        raise
    except BaseException:
        _exc = True
        raise
    finally:
        # #101 hygiene (gate F round 1, L1). These wrappers are the seams every launcher
        # funnels through (start_run's background thread, the synchronous CLI `workflow
        # run`, batch_runs._run_member, resume_run), so dropping the live progress entry
        # here covers the executors that have no crash handler of their own. A step that
        # died outside the ticker's own step-boundary path would otherwise leave one dict
        # per run in a long-lived server -- and because a leaked entry's `at` is newer than
        # any persisted row, `run_progress` would serve it forever for that id (and, after
        # SQLite rowid reuse, for whatever run inherited the id). The persisted columns keep
        # whatever the step reached, so forgetting here loses no signal.
        if _wf_run_id is not None:
            _progress_forget(_wf_run_id)
        _settle_tag_repair_spend(_wf_run_id, session, _exc or _abandoned)
        _finalize_run_envelope(_wf_run_id, doc_id, locale, exception=_exc, abandoned=_abandoned)
        # H4: close any still-open vendor dispatch leg of a run that FINISHED (completed/
        # failed) so a later expiry sweep can never drag a terminal run back to PAUSED.
        # Best-effort on an isolated session (never raises, never touches a paused run).
        if _wf_run_id is not None:
            try:
                from app.db import engine as _eng
                from app.services import vendor_dispatch as _vd

                with Session(_eng) as _s:
                    _vd.close_open_legs_for_run(_s, _wf_run_id)
            except Exception:
                # Open legs left behind let an expiry sweep drag this terminal run to PAUSED.
                log.warning(
                    "could not close open vendor legs for run %s", _wf_run_id, exc_info=True
                )


def _run_workflow_stream_segmented_impl(
    session: Session,
    workflow_id: int,
    doc_id: int,
    locale: str,
    run_id: int | None = None,
    seed_text: str | None = None,
    created_by: str = "",
    is_resume: bool | None = None,
):
    """Execute (or resume) a workflow over a SEGMENTED document, writing each
    Segment.target_text (unlike the FULL_TEXT path, which fills a single segment).

    Model (Option (c) hybrid): the orchestrator's step engine (llm_agent / lqa /
    human_review, retry/failover via _spec_for_step, StepRun trace) is driven
    PER SEGMENT, borrowing translate.py's per-segment iteration:
      - segments loaded sorted by order_index, locale-filtered;
      - {text} = the segment's CURRENT working text (source on step 0, prior
        step's output afterwards), {source} = the segment's original source,
        {glossary} matched PER SEGMENT;
      - TM-first short-circuit ONLY on step order_index == 0 (a 100% segment-TM
        hit fills the working text and skips the step-0 provider call for that
        segment; later steps still run on the TM-filled text);
      - provider calls for segments WITHIN one step run in a
        ThreadPoolExecutor(_mt_workers()); ORM writes/commits stay on this thread;
        steps are sequential.
    ONE StepRun is written per STEP (aggregate input/output = the segment texts
    joined by SEG_JOINER, aggregate attempts, provider/model from the last
    segment) so observe_run + run_view.html + the SSE event shapes are UNCHANGED.
    Write-back happens ONCE, AFTER the whole step loop, and ONLY on run success
    (no step FAILED). This means a mid-run failure leaves NO half-translated
    targets (targets stay untouched → the run ends FAILED), AND a human-review-as-
    LAST-step resume persists the operator's edits (working[] holds the re-split
    edited seed; the empty tail loop runs no steps but the post-loop write-back
    still commits the edits)."""
    doc = session.get(Document, doc_id)
    # AI opt-out (DPAI-08): also refused here for the direct segmented callers.
    from app.services import ai_optout as _ai_optout

    _ai_optout.refuse_workflow(session, workflow_id, doc)
    project = session.get(Project, doc.project_id)
    steps = get_steps(session, workflow_id)
    src = project.source_locale
    _tag_spec = None  # AI Tagging 1.5c - last step's spec (see below)
    _tag_redact = False  # #381: any step with Redact PII on masks the tag-repair calls
    # #596: every billed tag-repair call (accepted or rejected) lands here so the write-back
    # can fold it into the run's StepRun spend; see orchestrator_part4._stamp_tag_repair_spend.
    _tag_spend = [0.0, 0, 0, False]
    _tag_verify_spec = None  # LOCK-3 verifier = the LQA step's engine (see above)
    segments = _load_segments(session, doc_id, locale)
    # T1-05: no segments for this locale = nothing to translate — fail with a clear
    # reason rather than a bare 'failed' (the empty step loop below would otherwise
    # mark the run failed with only a generic message).
    # ENVELOPE NOTE (L6): like the FULL_TEXT twin, this early return precedes envelope
    # creation, so an empty run makes no envelope (zero provider calls) and the wrapper's
    # finally is a clean no-op.
    if not segments:
        yield from _empty_document_run(
            session, workflow_id, doc_id, locale, run_id, created_by=created_by
        )
        return
    seg_sources = [s.source_text for s in segments]
    # #705 (2026-10-11: "Workflow runs that come across tag only segments should copy
    # the tag to the target and deliver. This should be an across the board thing."): a
    # segment with no translatable text (only inline tags / placeholders and whitespace) is
    # never dispatched to ANY step -- no engine, no LLM, no spend, no QE score, no feedback
    # seed -- and the write-back lands the source as its own translation through the shared
    # copy action (status MT, never TM-eligible, never gate-held). Recomputed from the
    # sources on every (re)start, so a resumed run excludes exactly the same slots.
    from app.services import tags_only as _tags_only  # lazy (kernel facade)

    tags_only_idx: set[int] = _tags_only.indices(seg_sources)
    tags_only_ct = 0

    # per-segment glossary matched once up front (constant across steps). The plan
    # (pin/auto-attach + content-type scope, SPEC-content-types §3) is resolved ONCE
    # (project-level, constant across segments); the per-segment merge then matches +
    # merges terms. Shared plan/merge seam with the plain-MT path (translate.py).
    from app.services import glossary_module as _gm

    _gnotes: list = []
    gid = _resolved_glossary_id(
        session, steps, src, locale, getattr(project, "content_type_id", None), _gnotes
    )
    # Only the pin-resolution notes (a skipped pin) are user-facing; build_glossary_plan's
    # own notes (project-pinned / auto-attached trace) stay log-only as they always were.
    _pin_skip_notes = list(_gnotes)
    _plan = _gm.build_glossary_plan(
        session, project, src, locale, pinned_glossary_id=gid, notes=_gnotes
    )
    for _n in _gnotes:
        log.info("run glossary: %s", _n)
    _conflicts: set = set()
    # Context Package (SPEC-context-metadata phase 2A) resolved ONCE per run (NOT per
    # segment -- the effective card is a stable doc-level fact; a per-segment resolve
    # would be an N+1 DB hit AND would break the cache-friendly stable prefix). The
    # rendered block fills the step template's `{context}` placeholder via base_ctx;
    # `_sense_hint` (domain/audience) reaches glossary sense selection. render_prefix
    # is "" when nothing resolves -> byte-identical context-less prompts.
    _eff_pkg, _ = context_pkg.resolve(
        session, document=doc, project=project, org_id=getattr(project, "org_id", None)
    )
    _ctx_sub = context_pkg.render_prefix(_eff_pkg)
    _sense_hint = context_pkg.sense_hint(_eff_pkg)
    # Style Guide (SPEC-workflows-redesign Track B, B2) resolved ONCE per run (like the
    # context package -- a stable doc-level fact, not per-segment) at the customer x
    # content_type (+ optional pair) grain. Fills `{style_guide}` via base_ctx;
    # style_guide_prefix is "" when no APPROVED guide resolves -> byte-identical prompts.
    # NOT redacted (standing asset = prompt scaffolding, like {context}/{glossary}).
    _guide = style_guides.resolve_style_guide(
        session,
        org_id=getattr(project, "org_id", None),
        customer_id=getattr(project, "customer_id", None),
        content_type_id=getattr(project, "content_type_id", None),
        source_locale=src,
        target_locale=locale,
    )
    # MT formality (#617 item 7, ADR 0062): the register resolved for THIS document + target
    # language (document override > project > customer) joins the style-guide block, so every
    # step that renders {style_guide} (or gets it injected) and the LQA judge carry it. `_fm_record`
    # is stamped on the run below. None -> not a byte changes.
    _formality = formality.resolve_for(session, project=project, document=doc, target_locale=locale)
    _fm_record = _formality.record() if _formality is not None else None
    _sg_sub = style_guides.style_guide_prefix(_guide, _formality)
    # B3: the raw fenced block of the SAME resolved guide for an LQA step's adherence
    # judge (see the FULL_TEXT path). "" when nothing resolves -> byte-identical judge.
    _sg_render = style_guides.render_style_guide(_guide, _formality)
    # Terminology T3 (SPEC §9-T3 / LAW-TERM-15, residue [11]): swap the matcher for the
    # concept selector per segment ONCE PER RUN, and ONLY when some step has the renderer
    # toggle ON. `seg_concepts[j]` (with detached `_concept` metadata) is handed to each
    # toggle-ON step's per-segment render inside _run_step_on_segment; `seg_terms[j]` is its
    # five-key projection (byte-identical to match_terms_merged, LAW-TERM-1) and stays what
    # the toggle-OFF glossary_line block + the gates read. No toggle ON -> match_terms_merged
    # exactly as today, `seg_concepts` is None, nothing new spent.
    _seg_render = _any_render_toggle(steps)
    if _seg_render:
        seg_concepts = [
            glossary_concepts.match_concepts_merged(
                session,
                doc.project_id,
                s.source_text,
                src,
                locale,
                _plan,
                conflict_state=_conflicts,
                disambig=_sense_hint,
            )
            for s in segments
        ]
        seg_terms = [_five_key(c) for c in seg_concepts]
    else:
        seg_concepts = None
        seg_terms = [
            _gm.match_terms_merged(
                session,
                doc.project_id,
                s.source_text,
                src,
                locale,
                _plan,
                conflict_state=_conflicts,
                disambig=_sense_hint,
            )
            for s in segments
        ]
    # Phases 2+3: the doc-level union of those SAME matches, for the LQA step (which
    # scores the assembled blob). Computed once, reused by every LQA visit this run
    # makes — no extra matching, empty on an install with no glossary.
    _lqa_terms = _union_seg_terms(seg_terms)
    # Terminology T3 (LAW-TERM-15 (ii)/(iii), residue [12]/[13]): the run-scoped, in-memory
    # shown-set home for the SEGMENTED run. A toggle-ON producing step records segment j's
    # shown set here AFTER the produce decision; every cross-step consumer resolves its
    # enforcement set to `shown_terms_by_segment.get(j, seg_terms[j])` — the producing step's
    # shown set when it was ON, else the full match exactly as today. Empty (no toggle ON) ->
    # every consumer reads the full match -> byte-identical.
    shown_terms_by_segment: dict = {}

    def _lqa_terms_now():
        """The LQA/adjudicator document-level union, rebuilt LAZILY at each LQA visit from
        the shown-set map (residue [11]/[13]; NOT frozen at fork). Byte-identical to
        `_lqa_terms` when no step has the toggle ON."""
        if not _seg_render:
            return _lqa_terms
        return _union_seg_terms(
            [shown_terms_by_segment.get(j, seg_terms[j]) for j in range(len(segments))]
        )

    base_ctx = {
        "source_locale": src,
        "target_locale": locale,
        "context": _ctx_sub,
        "style_guide": _sg_sub,
        "lqa_findings": "",
    }

    # GitHub #356 phases 2a/2b: the AI-step system HEAD override in force for THIS run, resolved
    # ONCE per run: the workflow's own override, else its base workflow's (#365, a pair
    # variant's group default), else the run's project org's (never a caller-supplied org),
    # else the built-in text. None unless the
    # `system_prompt_overrides` flag is ON and an override is in force -> every composed system
    # message is then byte-identical to the built-in text. The locked floor is composed around
    # it by `_system_for`; `_sys_stamp` is what the StepRun rows record (empty while the flag
    # is off).
    _sys_ovr = system_prompt_overrides.resolve_step_head(
        session, getattr(project, "org_id", None), workflow_id
    )
    _sys_head_body = _sys_ovr.body if _sys_ovr is not None else None
    _sys_stamp = _system_prompt_stamp(_sys_ovr, src, locale)

    # TM few-shot (2B): the top-N fuzzy example pairs per segment, resolved ONCE per
    # run in ONE batched query (reused across every step — the source text + TM don't
    # change during a run). Retrieved at the MAX window; each step renders only the
    # first `tm_examples_window` pairs when its `tm_examples` toggle is ON. Skipped
    # entirely (no query) when NO llm_agent step wants examples. Empty per segment ->
    # no examples block -> byte-identical prompt.
    seg_examples: list[list] = [[] for _ in segments]
    _tm_ex_wanted = any(
        st.step_type == StepType.LLM_AGENT
        and getattr(st, "tm_examples", True)
        and context_pkg.clamp_examples_window(
            getattr(st, "tm_examples_window", context_pkg.TM_EXAMPLES_DEFAULT_WINDOW)
        )
        > 0
        for st in steps
    )
    if _tm_ex_wanted:
        _ex_map = tm.example_matches_batch(
            session,
            doc.project_id,
            seg_sources,
            src,
            locale,
            floor=context_pkg.TM_EXAMPLES_FLOOR,
            window=context_pkg.TM_EXAMPLES_MAX_WINDOW,
        )
        seg_examples = [_ex_map.get(s, []) for s in seg_sources]

    # F2.2 document-context pre-pass: a per-RUN memoized brief. Computed at most
    # ONCE (the first llm_agent step whose doc_context_prepass toggle is on), then
    # reused by every step that opts in — the brief is a stable doc-level fact, so
    # re-extracting it per step would waste calls. Soft-fails to "" (translate
    # normally). Uses the requesting step's provider spec so the call is billed on
    # the normal seam. _brief_state[0] = computed?, _brief_state[1] = block string.
    from app.services import adaptive

    _brief_state = [False, ""]

    def _doc_brief_block(step) -> str:
        if not getattr(step, "doc_context_prepass", False):
            return ""
        if not _brief_state[0]:
            _brief_state[0] = True
            sample = adaptive.sample_for_brief(seg_sources)
            brief, _made = pii_boundary.call_with_redaction(
                step.redact_pii,
                adaptive.build_document_brief,
                _spec_for_step(step),
                sample,
                src,
                locale,
            )
            _brief_state[1] = adaptive.brief_block(brief)
        return _brief_state[1]

    # LLMemory doc-level note: at most ONE tone/style/note memory per RUN (proposal §5
    # "chosen doc-level once per run"), memoized exactly like the brief above and for
    # the same two reasons — it is a stable doc-level fact, and re-selecting it per
    # segment would re-charge it against the block budget on every single call AND bump
    # its use_count once per segment, turning the "is this memory earning its place?"
    # counter into a segment counter. _mem_note_state[0] = computed?, [1] = block,
    # [2] = the memory ids (tap provenance), [3] = usage already stamped for the note?
    # The note is selected with `mark_used=False` and stamped by `_mem_stamp` below —
    # at most once per run, and only on a segment where it SURVIVED the fold (F1).
    _mem_note_state = [False, "", [], False]

    def _memory_doc_note():
        """(block, ids) for this run's doc-level note. Computed at most once; ("", [])
        when recall is off, the project has no customer, or nothing qualifies."""
        if not _mem_note_state[0]:
            _mem_note_state[0] = True
            try:
                blk, ids = llmemory.select_doc_note(
                    session,
                    customer_id=getattr(project, "customer_id", None),
                    project_id=doc.project_id,
                    source_locale=src,
                    target_locale=locale,
                    org_id=getattr(project, "org_id", None),
                    mark_used=False,
                )
                _mem_note_state[1], _mem_note_state[2] = blk, list(ids)
            except Exception:
                log.warning("llmemory: doc-note recall failed", exc_info=True)
        return _mem_note_state[1], _mem_note_state[2]

    if run_id is not None:
        run = session.get(WorkflowRun, run_id)
        start = run.current_step_index
        # resume: the operator edited the assembled blob; re-split by SEG_JOINER
        # back to per-segment working texts by index (segments were never merged).
        if seed_text is not None and seed_text.strip():
            # F4: a REAL edit (non-blank). If the operator altered the delimiter count
            # the parts no longer align to segments — do NOT silently pad/fold (that
            # would commit misaligned targets).
            parts = seed_text.split(SEG_JOINER)
            if len(parts) != len(segments):
                # Issue #23 (b) SAFE RESUME: a count mismatch must NOT terminally kill
                # the run. Restore the exact pre-resume PAUSED state — status PAUSED and
                # the cursor back to the human_review step (start-1, since the resume
                # entrypoints advance current_step_index past it before invoking this
                # generator) — so the run is fully re-resumable. No StepRun was written
                # on this attempt (this check precedes the run loop), so the ORIGINAL
                # PAUSED StepRun and its blob stay newest -> the run remains inspectable
                # and the operator can retry with a correct count. Yield a DISTINCT
                # 'resume_rejected' event (not 'error') so callers report a clean refusal
                # against a still-paused run, not a FAILED run. In-process callers pre-
                # validate via prepare_resume_seed, so this backstops the web/api paths
                # (whose current_step_index is likewise pre-advanced) too.
                run.current_step_index = max(0, start - 1)
                run.status = RunStatus.PAUSED
                run.finished_at = None
                session.add(run)
                session.commit()
                msg = (
                    f"resume text segment count {len(parts)} != expected "
                    f"{len(segments)} -- the review delimiters were altered; the run "
                    f"is unchanged and still paused"
                )
                yield {"type": "resume_rejected", "run_id": run.id, "error": msg}
                return
            working = list(parts)
        elif seed_text is not None:
            # T1-02: an empty/whitespace-only edited_text means "no edits" — recover
            # the working state captured at the pause (the paused human_review StepRun's
            # blob) and keep it, instead of treating "" as a delimiter mismatch that
            # FAILS the run and DISCARDS the already-produced MT. Only if the recovered
            # blob no longer aligns (should not happen — the joiner never occurs in
            # content) do we fall back to the sources.
            blob = _paused_blob(session, run_id)
            parts = blob.split(SEG_JOINER) if blob is not None else None
            working = (
                list(parts)
                if parts is not None and len(parts) == len(segments)
                else list(seg_sources)
            )
        else:
            working = list(seg_sources)
    else:
        run = WorkflowRun(
            workflow_id=workflow_id,
            document_id=doc_id,
            target_locale=locale,
            created_by=created_by or "",
            status=RunStatus.RUNNING,
            current_step_index=0,
        )

        # flood R2-F1: seam-shaped single-row INSERT (pending set {WorkflowRun}); ride out a
        # transient writer lock with the bounded jittered retry (re-adds after a rollback).
        # keep_loaded=True (fold-in) keeps run.id populated so no post-commit refresh() SELECT
        # (itself lock-prone) is needed.
        def _stage_fresh_run():
            session.add(run)

        _db.commit_with_retry(session, stage=_stage_fresh_run, keep_loaded=True)
        start = 0
        working = list(seg_sources)

    # Review F2: execution begins (or RE-begins, after a human pause) NOW, so the
    # staleness clock starts here. The chokepoint every segmented execution passes
    # through, fresh or resumed.
    #
    # flood R2-F1: this run-start flip (`UPDATE workflowrun SET started_at,status`) is the
    # seam-shaped single-row write run 296 died on under the flood — NOT a dense write-back /
    # terminal commit (those stay plain: the architectural single-writer ceiling, P-C5), so
    # give it the bounded jittered retry. On exhaustion it re-raises the OperationalError,
    # which _run_in_background's crash handler turns into the honest WRITE_CONTENTION_NOTE.
    def _stage_run_start():
        run.status = RunStatus.RUNNING
        run.formality = _fm_record  # #617 item 7: the register that applied (None = none)
        _mark_execution_start(run)
        session.add(run)

    _db.commit_with_retry(session, stage=_stage_run_start)

    # Make the glossary trace visible to the user (a non-covering pin skip / a
    # content-type skip): stamp each pin-skip note onto the run's notes, de-duplicated,
    # through the same flood-safe commit the run-start flip uses. Plain log.info alone left
    # the skip invisible on the run page and API.
    if any(_gn not in (run.notes or "") for _gn in _pin_skip_notes):

        def _stage_gloss_notes():
            for _gn in _pin_skip_notes:
                if _gn not in (run.notes or ""):
                    _stamp_note(run, _gn)
            session.add(run)

        _db.commit_with_retry(session, stage=_stage_gloss_notes)

    # Run envelope (proposal Wave A): created AFTER the WorkflowRun commit above (session
    # lock-free) and BEFORE the run_started yield, so even an immediate abandon at that
    # first yield still finalizes an existing envelope (M1 consistency). `_env_ids`
    # (action_id, run_id) is stamped into every provider-call tap_meta below via
    # tap_envelope.stamp_meta (the _seg_tap_meta closure, the LQA/QE judge calls, the
    # parallel group, the tag repairer) so every captured call belongs to exactly one
    # action. Best-effort + resume-reuse; (None, None) leaves stamping a no-op and the
    # run byte-identical. Finalized by run_workflow_stream's wrapper.
    _is_resume = is_resume if is_resume is not None else (run_id is not None)
    _env_ids = _begin_run_envelope(session, run, doc, project, steps, is_resume=_is_resume)
    _register_tag_spend(run.id, _tag_spend)
    yield {"type": "run_started", "run_id": run.id, "steps": len(steps), "start": start}

    # (write-back is done ONCE after the loop from working[], gated on run success —
    #  see the post-loop block — so there is no per-step "last output" write target.)
    # Per-segment origin: a segment filled by a 100% TM hit on step 0 keeps TM
    # provenance (status/confidence) at write-back; everything else is MT. Reset
    # to MT if any later step's provider overwrites the TM-filled working text.
    tm_origin = [False] * len(segments)
    # GitHub #43 (C1): per segment, did a LIVE provider produce the text this run will
    # commit? Drives `lang_guard` at the write-back below. Per-SEGMENT (not per-run)
    # because the accurate question is which provider produced THAT segment's final
    # text: a TM-filled segment never had a provider call at all (stays False — TM
    # content is not fresh MT), and a failover changes the answer mid-run.
    seg_lang_guard = [False] * len(segments)
    # GitHub #45: {j: (step_label, error)} for a segment whose step call FAILED while
    # its siblings succeeded. Its pre-step text is KEPT (working[j] untouched) and the
    # write-back routes it to review with a step_error qa_flag, instead of the whole run
    # failing and discarding every value for the locale. Populated per step visit; a
    # later step that SUCCEEDS on the segment clears its entry (the text it holds is no
    # longer the degraded one).
    seg_step_error: dict = {}
    # RESUME-SAFE (adversarial review P1) — restore BOTH from run.context, exactly as
    # `lqa_findings_block` below does, and for a sharper reason.
    #
    # Both are RUN-LOCAL and the segment write-back happens ONCE after the whole step
    # loop. A human_review step RETURNS from this generator mid-run; the resume re-enters
    # it from the top with fresh, EMPTY containers, and the write-back that eventually
    # runs reads those. So every degradation and every language-guard decision recorded
    # BEFORE the pause was silently discarded at the moment a human looked at the
    # document — the two worst possible directions: a segment whose fix step failed
    # shipped as clean `mt` with no step_error flag and no review routing, and a live
    # provider's English echo committed unguarded because seg_lang_guard[j] was False
    # again. A run WITHOUT a human_review step never noticed, which is why it survived
    # the first round. Persisted at the pause site below; a run that never pauses writes
    # nothing here and reads nothing back (byte-identical).
    _pause_ctx = run.context or {}
    _saved_guard = _pause_ctx.get("seg_lang_guard")
    if isinstance(_saved_guard, list) and len(_saved_guard) == len(segments):
        seg_lang_guard = [bool(v) for v in _saved_guard]
    for _sk, _sv in (_pause_ctx.get("seg_step_error") or {}).items():
        # JSON round-trips the int keys as strings and the tuple as a list; anything
        # that does not round-trip cleanly is dropped rather than trusted (a corrupt
        # context must not crash a resume, and losing a flag is the pre-fix behavior).
        try:
            _sj = int(_sk)
        except (TypeError, ValueError):
            continue
        if 0 <= _sj < len(segments) and isinstance(_sv, (list, tuple)) and len(_sv) == 2:
            seg_step_error[_sj] = (str(_sv[0] or ""), str(_sv[1] or ""))

    # LQA safety floor (C): the segments that already hold an ESTABLISHED TRANSLATION,
    # i.e. whose working[] slot is a target produced by a TM hit, an earlier step, or a
    # human_review resume edit — as opposed to the untranslated source `working` is
    # seeded with. The fixer post-check is a PRE/POST delta, and a delta against the
    # SOURCE text is not evidence of anything: a DNT term is present in its own source
    # by definition, so the FIRST step to translate a segment would look like it
    # "introduced" a DNT violation and its perfectly good translation would be reverted
    # back to English. So the guard only ever adjudicates an edit whose "before" is
    # itself a translation. On a resume, working[] holds the re-split edited/paused blob,
    # so any slot that differs from its source is already a translation (a translation
    # that happens to EQUAL its source is conservatively treated as not-yet-produced ->
    # no revert, which is the safe direction).
    produced: set[int] = {j for j in range(len(segments)) if working[j] != seg_sources[j]}
    # Per-segment fix-guard verdicts (C): {j: [qa_flag, ...]}, one entry per step whose
    # edit of that segment was rejected. Applied at write-back (flags + NEEDS_REVIEW)
    # exactly like the DNT/glossary re-assertions. A LIST, not a single dict, because
    # two different steps can each break a term on the same segment across a loop and
    # "nothing vanishes" is the proposal's invariant — the record of a discarded edit
    # is never overwritten by a later one. Empty unless a step actually broke a term.
    fix_guard_seg: dict[int, list] = {}
    # RESUME-SAFE (R4, this fix round): restore fix_guard_seg from run.context, exactly as
    # seg_step_error above and for the SAME reason. Both are run-local, the write-back runs
    # ONCE after the whole step loop, and a human_review pause RETURNS from this generator
    # mid-run -- so a fixer edit REVERTED before a pause would otherwise be discarded on
    # resume, and the reverted TM fill (tm_origin still True) would then be LOCKED at the
    # post-resume write-back (the F6 fix_guard leg of the lock-eligibility predicate keys on
    # this dict). Each entry is a JSON-round-tripped list of revert_flag dicts.
    for _fk, _fv in (_pause_ctx.get("fix_guard_seg") or {}).items():
        try:
            _fj = int(_fk)
        except (TypeError, ValueError):
            continue
        if 0 <= _fj < len(segments) and isinstance(_fv, list):
            fix_guard_seg[_fj] = [f for f in _fv if isinstance(f, dict)]
    # Run-level LQA-safety tally for the terminal event / run summary. Every counter
    # stays 0 on a workflow with no correction loop and no glossary, so the `done` event
    # shape is byte-identical there (the key is only attached when something happened).
    # `degraded` (GitHub #45, adversarial review P8) counts SEGMENT-VISITS that kept
    # their pre-step text after a step call failed for them specifically — the same class
    # the run trace's note names in prose, here as a machine-readable field so a headless
    # caller can act on "this run completed, but N segments never got the improvement it
    # promised" without parsing English.
    safety_run_stats = {"reverted": 0, "tm_protected": 0, "degraded": 0}
    # Batch 14 T2 (SPEC §6.3/§13/LAW-IMG-4): the per-run image-context skip counter. A
    # segment that HAD images resolved but sent them on NO target (images_present and not
    # images_sent — a non-vision engine, a ChatML adapter, an NMT, or a failover to a
    # text-only target) records the SINGLE skip note "image context skipped: engine not
    # vision-capable". Counted at most ONCE per run (the reason is the same regardless of
    # cause) so the note never spams; attached to the terminal `done` event only when it
    # fired. An OPTED-OUT / non-agent step resolves no images, so it is structurally
    # excluded (its results carry images_present False, §25.4) — no phantom skip.
    img_skip_run_ct = 0
    # PROPOSAL-tm-matching §5 (item 9): segments whose write-back was REFUSED by the
    # hard-lock seam -- their authoritative stored TM target is kept and emitted (not
    # blanked). Tracked SEPARATELY (not in safety_run_stats, whose exact shape existing
    # tests pin) and attached to the terminal `done` event only when it actually fired.
    tm_lock_refused_ct = 0

    # C1/C4: segments the per-step refusal guard has already caught as a hedge. Once a
    # segment lands here it is FROZEN — excluded from every later step's pending set
    # (so a finalizer step can't launder its hedge into clean text) and routed to
    # NEEDS_REVIEW at write-back WITHOUT committing target_text. Its slot in each
    # StepRun blob + `step` event is shown as the withheld disposition, not the raw
    # hedge (run_view.html renders those). Empty unless a step actually hedges.
    refused_seg: set[int] = set()
    # run-190 Item D / H2 (gate round 1): the segments the OVER-TRANSLATION (page-dump) guard
    # withheld. These are ALSO added to refused_seg (which drives the blob-withhold + later-
    # step exclusion), but Item D's withhold must NOT be governed by the REFUSAL_GUARD_ENABLED
    # kill switch — it has its own knob (OVER_TRANSLATION_MAX_RATIO). Tracking them separately
    # lets the write-back withhold target_text for a dump REGARDLESS of the refusal switch, so
    # the invariant "output_blob says WITHHELD iff target_text was not written" holds in BOTH
    # refusal-guard regimes (with the guard OFF, refused_seg holds only these dumps, so the two
    # signals stay in lockstep). Empty unless a step's candidate was a page dump.
    over_translation_seg: set[int] = set()
    # M-1: per-segment shape/char-count metadata for the withheld dumps, so the write-back
    # can build an HONESTLY-NAMED over-translation qa_flag (naming which shape rule fired and
    # the source/target lengths) instead of a "model refusal" flag. Keyed by segment index.
    over_translation_meta: dict[int, dict] = {}
    # #683: per-segment piece count of the producing step's translation (1 = one request),
    # stamped on Segment.meta["mt_chunks"] at write-back. Absent for an unproduced segment.
    seg_mt_chunks: dict[int, int] = {}
    # #343: segments whose llm_agent OUTPUT tripped an output guard (instruction echo /
    # short-source blow-up / number mismatch, app/services/output_guard.py). Maps segment index
    # -> the qa_flags entry. Each is ALSO added to refused_seg so it is quarantined exactly like a
    # refusal (target not written, NEEDS_REVIEW, TM-ineligible, later steps skip it); the run is
    # never blocked. Empty unless a step's output actually trips a guard.
    output_guard_seg: dict[int, dict] = {}
    # #343: the SOFT sibling (output_guard.NUMBER_MISMATCH_WITHHOLDS False): a dropped / added
    # number keeps the translation as the target but flags the segment for review. Maps segment
    # index -> the qa_flags entry; never added to refused_seg, and re-derived on every step.
    output_guard_soft_seg: dict[int, dict] = {}

    # PROPOSAL-tm-matching-workflow-step (§5/§7). TM-match lock bookkeeping:
    #   tm_lock_plan {j: (band, hard, tm_id)} -- what THIS run's tm_match step decided;
    #     applied to the segment rows AFTER the single write-back (so the seam never sees
    #     a locked segment mid-run and never refuses its own fill).
    #   tm_hard_idx / tm_soft_idx -- the persisted-lock-aware EXCLUSION sets. SEEDED from
    #     each segment's STORED lock (a hard/soft lock from a PRIOR run -- item 7b/R1-M8:
    #     skip the provider call this run too, not merely burn it and get refused at the
    #     seam) and extended by this run's tm_match step. HARD is excluded from EVERY
    #     llm_agent step; SOFT is excluded from a NORMAL translate step but DOES enter an
    #     LQA-flagged round-2 fix (§2 D3). Inert (both empty, no plan) on any workflow
    #     with no tm_match step AND no pre-existing locks -> byte-identical.
    tm_lock_plan: dict[int, tuple] = {}
    # F5 (Q9 fix round): the TM target text each plan entry intends to lock (the resolving
    # 100% hit's target). Run-local, keyed by segment index alongside tm_lock_plan; consumed
    # ONLY at write-back to detect an operator edit that landed on a planned slot during a
    # human_review pause (working[j] no longer byte-equals the intended target -> drop the
    # plan, no TM stamp). Persisted/restored across a pause exactly like tm_lock_plan.
    tm_plan_tgt: dict[int, str] = {}
    # Batch 15 T1 (SPEC-tm-match-step-config §5.3 / LAW-TM-19): the REAL match_pct each
    # tm_match fill stamps at write-back -- keyed by segment index for EVERY tm_origin
    # fill (exact 100, fuzzy, heal, relock), INCLUDING a fuzzy "none" fill that carries no
    # lock plan. The terminal write-back reads `tm_fill_pct.get(j, 100)` so an exact fill
    # stamps 100 (byte-identical, LAW-TM-3) and a fuzzy fill stamps its true score (e.g.
    # 85) -- which keeps it OUT of the `tm_match_pct == 100 AND status == MT` re-lock
    # whitelist and gives _seg_dispatch_payload the true band. Persisted/restored across a
    # pause exactly like tm_plan_tgt (a "none" fill has no lock plan, so this is its ONLY
    # resume-carried pct). A slot absent here defaults 100 (the step-0 exact TM
    # short-circuit + every non-fuzzy tm_origin path, byte-identical).
    tm_fill_pct: dict[int, int] = {}
    tm_hard_idx: set[int] = set()
    tm_soft_idx: set[int] = set()
    # CONTENT-PROTECTION (Q9 re-review, probe 5): segments whose stored target is a byte-exact
    # 100% TM fill UNDER A SOFT REVIEW GATE -- concretely tm_match_pct == 100 AND
    # status == NEEDS_REVIEW AND target byte-equals the resolving 100% hit (the probe-5 seed:
    # a soft-gated TM fill carrying a qa badge). The Q9 re-lock WHITELIST correctly refuses to
    # PROMOTE such a row to mt+locked -- doing so would erase the badge and lose review routing.
    # But leaving it wholly UNCLAIMED drops it into the next step's pending set, where an MT step
    # RE-TRANSLATES it: the byte-exact sentinel is destroyed, the badge is cleared by the
    # always-on stale-clear, and status returns to mt. The remedy is neither re-label nor lock:
    # mark the row content-PROTECTED so (a) every later step's pending set excludes it (no
    # re-translation) and (b) the write-back SKIPS its commit entirely (target_text / status /
    # qa_flags left EXACTLY as stored -- review routing intact). Populated by the tm_match step;
    # empty (and wholly inert -> byte-identical) on any run with no such seed. Persisted/restored
    # across a human_review pause exactly like tm_lock_plan / tm_origin.
    content_protected: set[int] = set()
    # #154: the parked blob, hoisted ABOVE the persisted-lock seed below because that seed
    # OVERWRITES working[] and the operator-edit classification further down compares
    # working[] against this blob. Computed here, reused verbatim there (one read, one
    # definition). Depends only on run_id/session/segments, none of which the seed touches.
    _parked_parts: list | None = None
    _parked_misaligned = False
    if run_id is not None:
        _pb = _paused_blob(session, run_id)
        if _pb is not None:
            _pp = _pb.split(SEG_JOINER)
            if len(_pp) == len(segments):
                _parked_parts = _pp
            else:
                _parked_misaligned = True
    # #154: a vendor/external RETURN re-seeds working[] from the vendor's text, which is not
    # an operator pause-edit -- the same discriminator the classification below uses.
    _is_vendor_return = (
        _pause_ctx.get("_resume_kind") == "vendor" or "_vendor_return_writeback" in _pause_ctx
    )

    for _lj, _lseg in enumerate(segments):
        if getattr(_lseg, "lock_band", None):
            # #154 (qa-0916w, sev:high): A HUMAN EDIT BEATS A STORED LOCK, exactly as it
            # already beats a lock PLANNED in this same run (the F5 tm_plan_tgt rule below)
            # and exactly as it does in the CAT editor, which clears the lock at its own save
            # site. The seam's hard-lock gate is documented on the assumption that "a HUMAN
            # edit does NOT reach this seam" -- true for the CAT editor and the key grid,
            # FALSE for a human_review resume, whose text is written back through this
            # machine path. So a reviewer was shown a locked segment, edited it, and had the
            # edit silently dropped on a run that reported `completed` with no warning
            # (measured on prod: runs #202/#203 dropped exactly the TM-locked slots while an
            # otherwise identical run on a never-TM-matched document kept all three edits).
            #
            # The test is the PARKED BLOB, never divergence from the stored target -- the
            # reasoning is spelled out at the status-protection block below and applies
            # identically here: divergence has two indistinguishable sources, the operator
            # editing the pause blob and a concurrent CAT approval landing mid-pause, and
            # only the parked comparison tells them apart. No parked blob (a fresh run, a
            # misaligned blob, a vendor return) -> nothing is treated as edited and this loop
            # is byte-identical to before.
            _edited_here = (
                not _is_vendor_return
                and _parked_parts is not None
                and (working[_lj] or "") != (_parked_parts[_lj] or "")
            )
            if _edited_here:
                # The reviewer's override, through the SAME audited seam the CAT editor uses
                # ("human edit wins, no stale lies") -- so the row cannot keep a band=100 /
                # hard=True claim pointing at text a human just replaced. NOT added to
                # tm_hard_idx/tm_soft_idx: the slot must reach the write-back like any other
                # edited slot, and the seam must not refuse it.
                try:
                    from app.services import tm_lock as _tm_lock

                    _tm_lock.clear_lock_on_human_edit(session, _lseg, actor="human_review")
                except Exception:
                    # An audit/lock-clear failure must not lose the edit or kill the run; the
                    # seam still sees a now-unlocked row in the common case, and the worst
                    # case is the pre-#154 behaviour for this one slot.
                    log.exception("#154: could not clear the TM lock on segment %s", _lseg.id)
                continue
            (tm_hard_idx if getattr(_lseg, "lock_hard", False) else tm_soft_idx).add(_lj)
            # A persisted-locked segment already holds authoritative TM content: seed
            # working[] from its STORED target (not the English source `working` was
            # seeded with) and mark it produced, so no step re-translates it AND the final
            # write-back does not clobber it with the source. At write-back the SOFT-lock
            # case re-commits identical text (the seam keeps it; 7a does not clear on a
            # no-op change) and the HARD-lock case is REFUSED by the seam (its stored text
            # is emitted, and the refusal is NOT counted -- see the write-back below).
            _lt = getattr(_lseg, "target_text", "") or ""
            if _lt:
                working[_lj] = _lt
                produced.add(_lj)
    # STATUS PROTECTION (Layer 2 -- the efficiency/UX half of the human-work protection whose
    # correctness half is quality.commit_mt_output's status gate). A segment a HUMAN has
    # APPROVED or EDITED is authoritative content the run must never re-translate: seed
    # working[] from its STORED target (so every downstream step -- LQA, QE, later MT -- sees
    # the real text, not the English source `working` was seeded with), mark it produced, and
    # record it in `status_protected` so the per-step pending build excludes it from EVERY
    # provider dispatch and the write-back SKIPS its commit. Mirrors content_protected (probe-5)
    # and the persisted-lock seed just above. Recomputed on every generator entry from the
    # freshly-loaded segments, so an approval that lands mid-pause (in another session) is
    # honoured on resume: the reloaded segment reads APPROVED here and is protected -- and the
    # parked-blob comparison below tells that concurrent approval apart from an operator's own
    # pause edit. The commit_mt_output seam refuses a protected slot too, as defense in depth.
    status_protected: set[int] = set()
    # F1 + NEW-ISSUE-1 (adversarial re-review): on a RESUME an operator can have EDITED an
    # approved/edited slot during the human_review pause; that edit is HUMAN content and must
    # LAND (via the seam's human_edit channel), not be discarded. But we must classify the edit
    # from the run's PARKED STATE, NOT from divergence against the stored target: text
    # divergence from the stored target has TWO indistinguishable sources -- (i) the operator
    # editing the pause blob, and (ii) a concurrent human APPROVING their own wording in the CAT
    # editor mid-pause, which changes the stored target UNDER the parked run. Case (ii) is
    # exactly what this protection exists to guard, so a divergence heuristic would DESTROY it
    # (commit the machine text over the fresh approval with human_edit=True, bypassing the
    # freshness read). Correct test: compare working[j] to the value the RUN ITSELF PARKED (the
    # paused blob), split per slot. working[j] != parked[j] => the operator edited THIS slot
    # (human_edit bypass); working[j] == parked[j] with a protected status => a human
    # approved/edited it (fresh or concurrent) and the machine must not touch it. Only a genuine
    # pause has a parked blob (a fresh run has none -> every approved slot is protected); this
    # also fixes RESIDUAL-1 (an operator deliberately typing the SOURCE into a pause slot now
    # reads as an edit -- working[j] != parked[j] -- instead of being silently discarded).
    # NEW-ISSUE-4 (adversarial re-review): `operator_edited` must be STICKY across MULTIPLE
    # parks. It is classified from the CURRENT park's blob, so on a SECOND pause the operator's
    # edited text becomes the newly-parked baseline (working == parked at pause B), the fresh
    # classification reads it as "unedited", re-protects it and RESETS it to the stored target --
    # the edit lost. So restore the set persisted at the LAST pause and UNION it with this
    # entry's fresh classification, exactly as tm_lock_plan / content_protected / tm_origin are
    # restored below (their "resume re-enters with fresh containers" comments describe this same
    # failure). A restored slot stays operator_edited on every subsequent entry (its status stays
    # APPROVED until the run finally completes and the write-back commits it).
    _restored_oe: set[int] = set()
    for _v in _pause_ctx.get("operator_edited") or []:
        try:
            _restored_oe.add(int(_v))
        except (TypeError, ValueError):
            continue
    # PART 1 (pause-edit protection, 2026-08-13): the operator's edited TEXT per slot. The
    # write-back uses it to tell an operator pause-edit that is the FINAL content (working[j]
    # still equals it -> land it SegStatus.EDITED, protected from the NEXT run's machine passes
    # by the status guard) from one a step AFTER the human_review pause re-ran over IN-RUN
    # (working[j] no longer equals it -> it is machine output, land MT -- the in-run boundary:
    # [review, MT] reruns MT on the edit, pre-existing behaviour kept). Restored across parks
    # alongside operator_edited; captured fresh at each pause below.
    operator_edited_text: dict[int, str] = {}
    _saved_oet = _pause_ctx.get("operator_edited_text")
    if isinstance(_saved_oet, dict):
        for _ok, _ov in _saved_oet.items():
            try:
                _oj = int(_ok)
            except (TypeError, ValueError):
                continue
            if 0 <= _oj < len(segments) and isinstance(_ov, str):
                operator_edited_text[_oj] = _ov
    # #154: _parked_parts / _parked_misaligned are computed ABOVE the persisted-lock
    # seed (they are needed there too); the original comment for the misaligned case lives
    # with that hoisted block.
    operator_edited: set[int] = set()
    # A vendor/external RETURN completing-resume re-seeds working[] from the vendor's returned
    # text (which differs from the parked pre-dispatch blob), so it must NOT be read as an
    # operator pause-edit: the vendor write-back path owns those slots (_vr_skip handles the
    # authoritative ones; a re-committed flagged slot must still re-trip its guards at the seam,
    # not short-circuit to EDITED here). Only a genuine human_review pause-edit is operator work.
    # `_pause_ctx` is the run.context snapshot taken BEFORE the vendor-resume keys are popped below.
    # INFO-2: the explicit `_resume_kind == "vendor"` marker is the primary discriminator; the
    # `_vendor_return_writeback` key is checked too for a run parked under older code.
    for _sj, _sseg in enumerate(segments):
        _fresh_edit = (
            not _is_vendor_return
            and _parked_parts is not None
            and working[_sj] != _parked_parts[_sj]
        )
        # PART 2 (vendor-return sticky-flag guard, 2026-08-13 security review): the RESTORED
        # operator set must be honoured PER SLOT on a vendor/external RETURN, not dropped wholesale
        # (the FT-arm twin CLEARS its single flag; the segmented arm has many slots, so it decides
        # each). `_fresh_edit` stays vendor-suppressed (a re-seed from the vendor's returned text is
        # never a fresh operator edit). For the RESTORED flag: a slot the vendor CHANGED (working, re-  # noqa: E501
        # seeded from the return, no longer byte-equals the pre-dispatch parked blob) is SUPERSEDED --  # noqa: E501
        # the operator dispatched it AFTER editing, so the vendor's content wins and the stale operator  # noqa: E501
        # flag must DROP (else the vendor body would commit via the human_edit bypass, riding the
        # operator channel instead of the vendor write-back's guards). A slot the vendor did NOT touch  # noqa: E501
        # (working byte-equals parked -- a no-op / source-echo return, OR an operator-edited slot the  # noqa: E501
        # F6-vs-status_protected exclusion still packaged but the vendor returned verbatim) KEEPS its  # noqa: E501
        # stickiness so the earlier edit still lands EDITED. We honour the restored flag ONLY when we  # noqa: E501
        # can PROVE the vendor left the slot untouched (parked blob aligned AND working == parked);
        # a misaligned/absent blob is treated conservatively as superseded (drop), so vendor content
        # can never ride the operator bypass through an unprovable slot.
        if _is_vendor_return:
            _keep_restored = _parked_parts is not None and working[_sj] == _parked_parts[_sj]
        else:
            _keep_restored = True
        _op_edit = _fresh_edit or (_sj in _restored_oe and _keep_restored)
        _st = getattr(_sseg, "status", None)
        if _st in (SegStatus.APPROVED, SegStatus.EDITED):
            if _op_edit:
                # the operator changed THIS protected slot at THIS pause (working differs from
                # what the run parked) OR at an EARLIER pause (restored, and the vendor did not
                # supersede it) -> honour the edit: leave working[_sj] as-is, do not protect it,
                # commit it via human_edit below.
                operator_edited.add(_sj)
                if _fresh_edit:
                    operator_edited_text[_sj] = working[_sj]
                continue
            working[_sj] = getattr(_sseg, "target_text", "") or ""
            produced.add(_sj)
            status_protected.add(_sj)
        elif _op_edit:
            # PART 1: an operator pause-edit of a MACHINE-status slot (new / mt / needs_review)
            # is human work too -- this is the flood's silent-loss class (a TM-filled `mt` slot
            # the operator replaced during review, which then landed `mt`, UNPROTECTED, and a
            # later machine run scaffolded over it). Mark it operator_edited so the write-back
            # lands it EDITED (protected). An UNTOUCHED machine slot (working == parked) is NOT
            # added and keeps its normal machine status. Deliberately NOT added to
            # status_protected / produced-as-protected: a machine slot must still flow to any
            # step AFTER the pause (the in-run boundary), so [review, MT] can re-MT the edit.
            operator_edited.add(_sj)
            if _fresh_edit:
                operator_edited_text[_sj] = working[_sj]
    # #343 RESUME-SAFE: restore the output-guard verdicts recorded BEFORE a human_review pause,
    # exactly as seg_step_error above (the write-back runs once, after the resume, and would
    # otherwise commit an echoed instruction as clean MT). A slot the operator EDITED during
    # the pause is human content: its record is dropped. A restored HARD entry goes back into
    # refused_seg (under the same kill switch the withhold itself honours).
    for _gname, _gdst in (
        ("output_guard_seg", output_guard_seg),
        ("output_guard_soft_seg", output_guard_soft_seg),
    ):
        for _gk, _gv in (_pause_ctx.get(_gname) or {}).items():
            try:
                _gj = int(_gk)
            except (TypeError, ValueError):
                continue
            if (
                0 <= _gj < len(segments)
                and isinstance(_gv, dict)
                and _gv.get("type") in output_guard.OUTPUT_GUARD_FLAG_TYPES
                and _gj not in operator_edited
            ):
                _gdst[_gj] = _gv
    # #505 item 3: restore the per-step refusal / over-translation withholds recorded BEFORE a
    # human_review pause (the write-back runs once, after the resume, and the withheld text
    # rides the paused blob). A slot the operator EDITED during the pause is human content and
    # drops its record, exactly like the output-guard records above.
    for _rk in _pause_ctx.get("refused_seg") or []:
        if isinstance(_rk, int) and 0 <= _rk < len(segments) and _rk not in operator_edited:
            refused_seg.add(_rk)
    for _rk in _pause_ctx.get("over_translation_seg") or []:
        if isinstance(_rk, int) and 0 <= _rk < len(segments) and _rk not in operator_edited:
            over_translation_seg.add(_rk)
            refused_seg.add(_rk)
    for _mk, _mv in (_pause_ctx.get("over_translation_meta") or {}).items():
        try:
            _mj = int(_mk)
        except (TypeError, ValueError):
            continue
        if _mj in over_translation_seg and isinstance(_mv, dict):
            over_translation_meta[_mj] = dict(_mv)
    if config.REFUSAL_GUARD_ENABLED:
        refused_seg.update(output_guard_seg)
    # Backstop tally for a segment a human approves MID-run (after this seed, caught only by
    # the seam's freshness check at write-back); disjoint from `status_protected`.
    status_protected_ct = 0
    # PART 1: operator pause-edits that landed as SegStatus.EDITED at the write-back (surfaced
    # in the run summary so the protection is visible, never silent).
    operator_edited_landed_ct = 0
    # RESUME-SAFE (B3): a human_review pause RETURNS from this generator; the resume
    # re-enters with fresh containers, so a tm_match that ran BEFORE the pause would lose
    # its plan + exclusion and a later step would re-translate (and never lock) the TM
    # fill. Restore both from run.context, the same write-through rule as seg_lang_guard.
    _saved_plan = _pause_ctx.get("tm_lock_plan")
    if isinstance(_saved_plan, dict):
        for _pk, _pv in _saved_plan.items():
            try:
                _pj = int(_pk)
            except (TypeError, ValueError):
                continue
            # Batch 15 T1 (LAW-TM-12): the lock-plan value is a 4-tuple
            # (band, hard, tm_id, match_pct). BACK-COMPAT: a run PAUSED pre-upgrade
            # serialized a 3-element list, so a short (>=3) entry defaults match_pct=100
            # (today's exact-only behaviour) -- a cross-deploy resume never IndexErrors or
            # mis-stamps.
            if 0 <= _pj < len(segments) and isinstance(_pv, (list, tuple)) and len(_pv) >= 3:
                _pband, _phard = str(_pv[0]), bool(_pv[1])
                _ppct = int(_pv[3]) if len(_pv) >= 4 else 100
                tm_lock_plan[_pj] = (_pband, _phard, _pv[2], _ppct)
                tm_fill_pct[_pj] = _ppct
                (tm_hard_idx if _phard else tm_soft_idx).add(_pj)
    # F5: restore the planned TM target text alongside the plan (see its declaration). A
    # legacy paused run that predates this key simply has no targets to compare -> the
    # write-back guard falls back to leaving the plan intact (the pre-fix behaviour).
    _saved_tgt = _pause_ctx.get("tm_plan_tgt")
    if isinstance(_saved_tgt, dict):
        for _tk, _tv in _saved_tgt.items():
            try:
                _tj = int(_tk)
            except (TypeError, ValueError):
                continue
            if 0 <= _tj < len(segments) and isinstance(_tv, str):
                tm_plan_tgt[_tj] = _tv
    # Batch 15 T1 (LAW-TM-19/12): restore the per-fill real match_pct, so a fuzzy fill
    # (INCLUDING a "none" fill that carries no lock plan) stamps its true score after a
    # pause/resume. A legacy paused run with no key leaves tm_fill_pct empty -> every slot
    # defaults 100 at write-back (byte-identical to the pre-Batch-15 exact-only stamp).
    _saved_fill_pct = _pause_ctx.get("tm_fill_pct")
    if isinstance(_saved_fill_pct, dict):
        for _fk, _fv in _saved_fill_pct.items():
            try:
                _fj = int(_fk)
                _fpct = int(_fv)
            except (TypeError, ValueError):
                continue
            if 0 <= _fj < len(segments):
                tm_fill_pct[_fj] = _fpct
    # F2 (Q9 final round, DEPLOY HAZARD): a run PAUSED under code that predates tm_plan_tgt
    # restores a tm_lock_plan with NO companion planned-target. The write-back guard now
    # keys on tm_plan_tgt, so such a legacy plan would sail past the guard and LOCK whatever
    # working text resumes -- including operator-edited text -- stamping false TM provenance
    # (band + tm_id + MT/pct=100) onto content a human changed. CONSERVATIVE RULE: lose a
    # lock, never stamp a lie. Drop every restored plan entry that has no tm_plan_tgt to
    # verify against; the segment simply does not lock on THIS resume (it keeps its restored
    # text, edited or not). A subsequent normal run (tm_match followed by translate) re-locks
    # the clean fills from scratch. Inert for any run paused under current code -- fills and
    # re-locks always write tm_lock_plan[j] and tm_plan_tgt[j] together, so nothing is
    # dropped. tm_hard_idx/tm_soft_idx membership is left as restored: the slot stays
    # excluded from re-translation this run, it just carries no lock.
    for _lpj in [k for k in tm_lock_plan if k not in tm_plan_tgt]:
        tm_lock_plan.pop(_lpj, None)
    # MED-5: tm_origin is ALSO run-local and reset to all-False on resume, so a TM-filled
    # segment that survived the pause would be written back as fresh MT (mt_confidence=0.9)
    # instead of TM (SegStatus.MT + tm_match_pct=100) -- claiming an MT provenance for a
    # locked TM value and corrupting the exact leverage numbers this feature produces.
    # Restore it from run.context alongside the lock plan.
    _saved_origin = _pause_ctx.get("tm_origin")
    if isinstance(_saved_origin, list) and len(_saved_origin) == len(segments):
        tm_origin = [bool(v) for v in _saved_origin]
    # CONTENT-PROTECTION resume-safety: the tm_match step that marks a row content-protected
    # runs BEFORE a human_review pause and does NOT re-run on resume, but the pending-set
    # exclusion + the write-back skip (which must both honour it) run AFTER it. Restore the set
    # from run.context so a resumed run still protects the same rows -- the SAME write-through
    # rule as tm_lock_plan / tm_origin. A run that never had the key keeps content_protected
    # empty (byte-identical). Bad/out-of-range entries are dropped defensively.
    _saved_cp = _pause_ctx.get("content_protected")
    if isinstance(_saved_cp, list):
        for _cpv in _saved_cp:
            try:
                _cpj = int(_cpv)
            except (TypeError, ValueError):
                continue
            if 0 <= _cpj < len(segments):
                content_protected.add(_cpj)

    # QE routing gate (roadmap gap #1/#6): per-segment QE result of the LAST step
    # that scored each segment (None = never scored / gate off). Applied at write-
    # back: a segment whose winning QE result FAILED the gate is routed to review
    # (SegStatus.NEEDS_REVIEW); the score+rationale is stored in Segment.qa_flags for
    # the CAT editor. qe_run_stats aggregates the run's scored/passed/failed/skipped
    # for the summary. Both inert (empty / all-zero) unless a step has qe_gate ON.
    qe_seg: dict[int, dict] = {}
    qe_run_stats = _new_qe_stats()
    # QE auto-fix (Train B): the `qe_autofix` qa_flags record per segment, written by the
    # step that last scored it (a later scoring step drops an earlier step's record).
    # Empty unless a step has qe_gate AND qe_autofix on, so a run without the opt-in
    # leaves no trace.
    qe_autofix_seg: dict[int, dict] = {}

    # Track whether any step FAILED (provider outage → empty output). On failure we
    # mark the run FAILED and SKIP write-back, so a broken run leaves NO blank/
    # partial targets (mirrors the FULL_TEXT path's failure behavior).
    any_step_failed = False
    # Prompt-package engine (change 1): the rendered <lqa_findings> block of the MOST
    # RECENT LQA card this run produced (segmented LQA scores the assembled blob, so this
    # is the doc-level card). "" until an LQA runs; updated per LQA visit for loop
    # freshness. Mirrors the FULL_TEXT path via the shared render/inject seams.
    # RESUME-SAFE (review fix 2): restored from run.context so a fix step still receives
    # the findings after a human_review pause/resume (persisted on each failing LQA below).
    lqa_findings_block = (run.context or {}).get("lqa_findings_block", "")

    # F-CRIT / F-LOW-1: a vendor/external RETURN hands its resume a ONE-SHOT write-back
    # policy through run.context -- `skip` names slots whose vendor verdict is authoritative
    # (a source-echo no-op, a withheld refusal, a hard-lock, or an EARLIER partial admit) so
    # the write-back must NOT re-commit them (re-committing the parked SOURCE would ship it
    # as clean MT and erase the guard verdict); `tm_ineligible` names admitted-vendor slots
    # whose re-commit keeps the vendor-content TM-ineligible intent. CONSUMED + CLEARED here
    # so it applies to exactly THIS resume's write-back, never a later human_review resume of
    # the same run (which would then silently discard that resume's own edits).
    _vr_wb = (run.context or {}).get("_vendor_return_writeback") or {}
    _vr_skip = set(_vr_wb.get("skip") or [])
    _vr_tm_inelig = set(_vr_wb.get("tm_ineligible") or [])
    # INFO-2: consume + clear BOTH one-shot vendor-resume keys (the explicit `_resume_kind`
    # marker and the `_vendor_return_writeback` policy) so neither leaks into a later
    # human_review resume of the same run. `_pause_ctx` (captured above, before this pop) still
    # carries them for the operator-edit classification's `_is_vendor_return` check.
    _ctx0 = run.context or {}
    if "_vendor_return_writeback" in _ctx0 or "_resume_kind" in _ctx0:
        _c = dict(_ctx0)
        _c.pop("_vendor_return_writeback", None)
        _c.pop("_resume_kind", None)
        run.context = _c
        session.add(run)
        session.commit()

    # Cursor machine (SPEC §4.1): replaces `for i in range(start, len(steps))` with a
    # while-cursor loop so Phase 3 routes backward (bounded loops). Kept in LOCKSTEP with
    # the FULL_TEXT loop above via the shared RunGuard / _next_cursor / _spend_block_reason
    # seams. On a plain / Phase-2 workflow `_has_graph` is False -> the guard is inert
    # (no run.context write, no cap trip) and _next_cursor always returns cursor+1:
    # byte-identical to today (incl. the human-review-as-LAST-step resume where
    # start == len(steps) and the empty loop still runs the post-loop write-back).
    key_index = _key_index(steps)
    guard = RunGuard(session, run, _has_graph(steps))
    # SPEC-parallel-routing: computed ONCE (see the FULL_TEXT loop for the rationale).
    par_on = _has_parallel(steps)
    # The assembled source blob for the LQA judge (a stable per-run fact — seg_sources is
    # fixed), hoisted out of the (now shared) LQA branch and handed to `_execute_step`.
    src_blob = SEG_JOINER.join(seg_sources)

    def _seg_pause_ctx():
        # RESUME-SAFE (adversarial review P1 / B3 / MED-5): the run-local per-segment
        # records that ride alongside `working` (survived in the StepRun blob) but whose
        # consuming write-back runs AFTER the resume. Persisted through the shared
        # _execute_step pause seam. WRITE-THROUGH once any key exists (re-review N1): a
        # segment repaired between two pauses must not be re-quarantined by a stale entry;
        # a run that never had either key keeps run.context NULL (the RunGuard contract,
        # asserted by verify_condwf_phase1 on a paused linear run).
        _prev_ctx = run.context or {}
        if (
            seg_step_error
            or any(seg_lang_guard)
            or tm_lock_plan
            or any(tm_origin)
            or fix_guard_seg
            or output_guard_seg
            or output_guard_soft_seg
            or content_protected
            or operator_edited
            or refused_seg
            or over_translation_seg
            or "refused_seg" in _prev_ctx
            or "over_translation_seg" in _prev_ctx
            or "seg_step_error" in _prev_ctx
            or "seg_lang_guard" in _prev_ctx
            or "tm_lock_plan" in _prev_ctx
            or "tm_origin" in _prev_ctx
            or "fix_guard_seg" in _prev_ctx
            or "output_guard_seg" in _prev_ctx
            or "output_guard_soft_seg" in _prev_ctx
            or "content_protected" in _prev_ctx
            or "operator_edited" in _prev_ctx
        ):
            return {
                **_prev_ctx,
                "seg_lang_guard": [bool(v) for v in seg_lang_guard],
                "seg_step_error": {str(k): [v[0], v[1]] for k, v in seg_step_error.items()},
                # R4: a reverted fixer edit survives the pause (see the restore above).
                "fix_guard_seg": {str(k): list(v) for k, v in fix_guard_seg.items()},
                # #343: output-guard verdicts survive the pause (restored above).
                "output_guard_seg": {str(k): dict(v) for k, v in output_guard_seg.items()},
                "output_guard_soft_seg": {
                    str(k): dict(v) for k, v in output_guard_soft_seg.items()
                },
                # Batch 15 T1 (LAW-TM-12): the lock-plan value is a 4-tuple
                # (band, hard, tm_id, match_pct) -- serialize all four so a fuzzy fill's
                # real pct survives the pause (the restore back-compats a short 3-element
                # entry to match_pct=100).
                "tm_lock_plan": {
                    str(k): [v[0], v[1], v[2], (v[3] if len(v) >= 4 else 100)]
                    for k, v in tm_lock_plan.items()
                },
                # F5: the planned TM target survives the pause so the write-back guard
                # can tell an operator's post-pause edit from the intended TM value.
                "tm_plan_tgt": {str(k): v for k, v in tm_plan_tgt.items()},
                # Batch 15 T1 (LAW-TM-19): the real per-fill match_pct survives the pause
                # for EVERY tm_origin fill, incl. a "none" fuzzy fill that has no lock plan.
                "tm_fill_pct": {str(k): int(v) for k, v in tm_fill_pct.items()},
                # MED-5: TM-vs-MT provenance survives the pause.
                "tm_origin": [bool(v) for v in tm_origin],
                # CONTENT-PROTECTION (probe 5): the pending-set exclusion + write-back
                # skip set survives the pause so a resumed run does not re-translate /
                # re-label a protected row.
                "content_protected": sorted(content_protected),
                # NEW-ISSUE-4: the operator-edited protected slots must be STICKY across
                # parks. Without this a run that parks AGAIN re-baselines the edit against
                # the newly-parked blob, re-protects it and RESETS it to the stored target
                # (edit lost). Restored + UNION'd with the fresh classification at entry.
                "operator_edited": sorted(operator_edited),
                # PART 1: the operator's edited TEXT per slot, sticky across parks, so the
                # write-back can tell an edit that is still the final content (-> EDITED)
                # from one a later in-run step re-ran over (-> MT, the in-run boundary).
                "operator_edited_text": {str(k): v for k, v in operator_edited_text.items()},
                # #505 item 3: the per-step refusal / over-translation withholds survive the
                # pause. The paused blob keeps the RAW withheld text (the review screen shows
                # what the model produced), and a refusal hedge is re-detected by
                # `is_refusal` at write-back, but a page DUMP is judged by per-step length and
                # shape rules the commit does not recompute: without these records an
                # "accept as is" resume committed the dump as clean MT.
                "refused_seg": sorted(refused_seg),
                "over_translation_seg": sorted(over_translation_seg),
                "over_translation_meta": {
                    str(k): dict(v) for k, v in over_translation_meta.items()
                },
            }
        return None

    def _seg_dispatch_payload():
        # SEGMENTED external dispatch (PROPOSAL-external-processing-step Wave A): one
        # contract unit per Segment, carrying the stable seg id, source, current working
        # target, and the persisted TM band (`tm_match_pct`/`lock_band`) so the scoping
        # estimate prices EXACTLY the packaging being sent (locked/100%/reps excluded).
        # F6 (adversarial review): a human-APPROVED/EDITED segment is NOT packaged into the
        # vendor contract at all -- otherwise it is quoted, PAID for, and then refused on
        # return (the seam declines a vendor overwrite of a sign-off), real money spent on a
        # string the vendor can never change. Excluding it here drops it from the contract,
        # the scoping estimate, AND the return-expectation together (all three read this one
        # payload), consistent with the run's own status_protected write-back skip that
        # preserves it. (FULL_TEXT is covered up-front by the F7 short-circuit -- an approved
        # body never reaches the EXTERNAL step.)
        return [
            {
                "id": segments[i].id,
                "source": seg_sources[i],
                "target": working[i],
                "tm_match_pct": getattr(segments[i], "tm_match_pct", None),
                "lock_band": getattr(segments[i], "lock_band", None),
            }
            for i in range(len(segments))
            if i not in status_protected
        ]

    def _seg_persist_inrun_hard_locks():
        """P-A1 (HIGH, data integrity): durably persist THIS run's in-run tm_match HARD
        locks (+ their TM fill) onto the Segment rows just before the run parks at an
        external dispatch, so vendor_return's existing hard-lock seam refuses a vendor
        overwrite. See the call site in `_execute_step`'s EXTERNAL branch for the why.

        Scope = HARD locks ONLY. A soft lock earns NO vendor-return protection anyway (the
        write seam refuses only hard locks), and the terminal write-back re-establishes soft
        locks on resume for any segment the vendor did not overwrite -- so persisting soft
        locks here would change nothing and only widen the mid-run write surface. Each fill
        is committed through the SAME write seam (`quality.commit_mt_output`) inside a
        per-segment SAVEPOINT and kept ONLY when it clears the EXACT lock-eligibility the
        terminal write-back uses AND would HARD-lock (R3: a fill carrying a soft dnt/glossary
        flag is a soft lock, not persisted here). A non-hard-lockable fill's speculative
        commit is rolled back to its savepoint so the row is left UNTOUCHED for the terminal
        write-back to settle exactly as before. Inert (no commit) on any run whose tm_match
        step planned no hard lock -> every workflow without a tm_match hard lock is
        byte-identical. Returns the number of segments persisted (for tests/telemetry)."""
        from app.services import tm_lock

        persisted = 0
        for j, seg in enumerate(segments):
            plan = tm_lock_plan.get(j)
            # HARD-planned only (plan[1] is `hard`); a soft plan is skipped (see docstring).
            if plan is None or not plan[1]:
                continue
            # An intervening step (or the plain-translate short-circuit) touched the planned
            # slot -> it is no longer this run's clean TM fill; leave it for the terminal
            # write-back (mirrors that write-back's F5/F1 tm_plan_tgt guard). Belt-and-
            # suspenders for a HARD plan (a hard lock excludes the slot from every llm_agent
            # step, so working[j] should still equal the planned target here).
            if j in tm_plan_tgt and tm_plan_tgt[j] != working[j]:
                continue
            sp = session.begin_nested()  # SAVEPOINT: keep-or-discard, per segment
            # Commit the fill through the ONE write seam (persists target + runs the same
            # deterministic gates the terminal write-back runs). No LLM tag repair here -- a
            # fill only HARD-locks when clean, so the deterministic verdict is sufficient.
            outcome = quality.commit_mt_output(
                session,
                seg,
                working[j],
                source="workflow",
                tm_eligible=True,
                glossary_terms=seg_terms[j],
                lang_guard=seg_lang_guard[j],
            )
            # The EXACT lock-eligibility predicate from the terminal write-back (Q6/F6): a
            # tm_origin fill the seam CLEANLY committed as authoritative content, MINUS
            # char_limit / whitespace / QE-fail / step-error / fix-reverted (tm_eligible
            # already excludes the poisoned nontranslation/tag/number/meta/placeholder
            # classes). whitespace is soft + tm-neutral so it is excluded EXPLICITLY here,
            # like char_limit (though a tm_origin fill never sets it: lang_guard is off for
            # a TM apply).
            card = qe_seg.get(j)
            eligible = (
                tm_origin[j]
                and outcome.committed
                and outcome.tm_eligible
                and not outcome.char_limit_violation
                and not outcome.whitespace_violation
                and not (card is not None and card.get("passed") is False)
                and j not in seg_step_error
                and j not in fix_guard_seg
            )
            # R3: a fill carrying a soft dnt/glossary adherence flag NEVER hard-locks -- it
            # is a SOFT lock the terminal write-back handles on resume, so it is not
            # persisted here (a soft lock earns no vendor-return protection anyway).
            flagged = bool(outcome.dnt_violations or outcome.glossary_violations)
            if not eligible or flagged:
                sp.rollback()  # discard the speculative commit; row left untouched
                continue
            # Clean hard fill: mirror the terminal write-back's TM branch (SegStatus.MT +
            # the fill's REAL tm_match_pct, mt_confidence left unset), then stamp the HARD
            # lock so the row is durably locked before the vendor can write it. Batch 15 T1
            # (LAW-TM-19): the plan is now a 4-tuple (band, hard, tm_id, match_pct); an
            # exact fill stamps 100 (byte-identical), a fuzzy hard `policy` fill stamps its
            # true score.
            seg.status = SegStatus.MT
            band, hard, tm_id, match_pct = plan
            seg.tm_match_pct = match_pct
            tm_lock._stamp_lock(seg, band=band, hard=hard, tm_id=tm_id, entry_id=tm_id)
            session.add(seg)
            sp.commit()  # RELEASE the savepoint: the lock joins the txn
            persisted += 1
        if persisted:
            session.commit()
        return persisted

    def _lqa_inputs(checks):
        from app.services.orchestrator_part3 import _lqa_repair_steps

        repairers = _lqa_repair_steps(checks, steps)
        excluded = set()
        if repairers and lqa_safety.tm_lqa_exclusion_enabled():
            excluded = set.intersection(
                *(
                    set(
                        lqa_safety.step_refused_indices(
                            range(len(segments)),
                            tm_origin,
                            fixer,
                            _is_correction_loop_step(fixer, steps),
                            tm_hard_idx=tm_hard_idx,
                            tm_soft_idx=tm_soft_idx,
                            content_protected=content_protected,
                            status_protected=status_protected,
                            refused_seg=refused_seg,
                        )
                    )
                    for fixer in repairers
                )
            )
        if not excluded:
            return src_blob, SEG_JOINER.join(working), _lqa_terms_now(), None
        judged = [j for j in range(len(segments)) if j not in excluded]
        return (
            SEG_JOINER.join(seg_sources[j] for j in judged),
            SEG_JOINER.join(working[j] for j in judged),
            _union_seg_terms(
                [
                    shown_terms_by_segment.get(j, seg_terms[j]) if _seg_render else seg_terms[j]
                    for j in judged
                ]
            ),
            {
                "excluded": len(excluded),
                "judged": len(judged),
                "excluded_indices": sorted(excluded),
            },
        )

    cursor = start
    while 0 <= cursor < len(steps):
        step = steps[cursor]
        # WHEN THIS STEP BEGAN EXECUTING (review NEW-1) -- see the FULL_TEXT loop's
        # note; the two stamp identically so the activity signal cannot differ by
        # document mode.
        step_started_at = datetime.utcnow()

        # SPEC-parallel-routing §5.2: FORK. Mirrors the FULL_TEXT loop exactly via the
        # shared `_run_parallel_group` seam — the ONLY difference is which blobs are
        # handed in, because a Phase-1 lane only READS the working text (it produces no
        # target, so `working[]`, `produced`, `refused_seg`, `tm_origin` and the
        # post-loop write-back are all untouched by a group).
        _bounds = _group_at(steps, cursor) if par_on else None
        # ISSUE #94: the DOCUMENTED per-step execution ceiling, enforced BEFORE the step
        # (or the group it forks) runs -- not only when a branch jumps INTO it. See
        # `_visit_cap_decision` for the gate/producer split (H3) and for why NO StepRun row
        # is written (M1). Inert on a plain/linear workflow (guard inactive).
        #
        # AFTER the run_condition guard, semantically (H3(ii)): a step whose condition is
        # FALSE would have been SKIPPED and the run would have carried on, so sitting at
        # its ceiling must not upgrade that skip into a run end. The condition is only
        # evaluated here when the step IS capped -- the common path is untouched -- and the
        # guard below re-evaluates it for the skip itself, exactly as before.
        _cap_step, _cap_reason, _cap_is_gate = _visit_cap_decision(guard, steps, cursor, _bounds)
        if _cap_step is not None and not (
            not _bounds and step.run_condition and not _eval_run_condition(step, run, session)
        ):
            _cap_index = steps.index(_cap_step)
            if _cap_is_gate:
                _record_gate_hold(
                    session, run, step=_cap_step, step_index=_cap_index, cause=GATE_HOLD_VISIT_CAP
                )
                run.current_step_index = len(steps)
            else:
                # CONSTRAINT (gate C round 2, L2): the producer SKIP advances `cursor + 1`,
                # which is only correct because `_cap_index == cursor` whenever this branch
                # runs. A `bounds`-set decision can name a group MEMBER at any index, and a
                # cursor left mid-group would make `_group_at` return None, decomposing the
                # group into sequential single-step execution and skipping the frozen
                # snapshot fork and the join routing entirely. That is unreachable TODAY
                # because `parallel_group` is a dead field on every non-`lqa` step type
                # (workflows.py refuses it on llm_agent / human_review / tm_match /
                # external), so every group member is a GATE and takes the branch above.
                # If Phase 2 ever admits a producing lane, this must become
                # `max(_cap_index, cursor) + 1` (or refuse the producer branch when
                # `_bounds` is set); `verify_parallel_routing` pins the dead-field premise.
                _record_visit_cap_skip(
                    session, run, step=_cap_step, step_index=_cap_index, reason=_cap_reason
                )
                run.current_step_index = cursor + 1
            session.add(run)
            session.commit()
            yield {
                "type": "step_skipped",
                "run_id": run.id,
                "step_index": _cap_index,
                "step_name": _cap_step.name or _cap_step.step_type,
                "step_type": _cap_step.step_type,
                "condition": {},
                "visit_cap": True,
                "held": bool(_cap_is_gate),
                "error": _cap_reason,
                "output": SEG_JOINER.join(working),
            }
            cursor = len(steps) if _cap_is_gate else cursor + 1
            continue
        if _bounds:
            _check_src, _snap, _check_terms, _check_scope = _lqa_inputs(
                list(steps[_bounds[0] : _bounds[1] + 1])
            )
            _gr = _run_parallel_group(
                session,
                run,
                steps,
                _bounds[0],
                _bounds[1],
                guard=guard,
                key_index=key_index,
                src_blob=_check_src,
                tgt_blob=_snap,
                src=src,
                locale=locale,
                sg_render=_sg_render,
                lqa_terms=_check_terms,
                lqa_scope=_check_scope,
                workflow_id=workflow_id,
                doc_id=doc_id,
                env_ids=_env_ids,
            )
            if _gr["cap_reason"]:
                session.add(
                    StepRun(
                        workflow_run_id=run.id,
                        step_id=_gr["cap_step"].id,
                        status=RunStatus.FAILED,
                        input_text=_snap,
                        output_text="",
                        error=_gr["cap_reason"],
                        outcome_index=guard.visit_index(_gr["cap_step"]),
                        started_at=step_started_at,
                        finished_at=datetime.utcnow(),
                    )
                )
                _commit_run_terminal_direct(session, run, RunStatus.FAILED)
                yield {"type": "error", "run_id": run.id, "error": _gr["cap_reason"]}
                return
            if _gr["findings_block"] is not None:
                lqa_findings_block = _gr["findings_block"]
            for _ev in _gr["events"]:  # noqa: UP028
                yield _ev
            if _gr["failed"]:
                _commit_run_terminal_direct(session, run, RunStatus.FAILED)
                yield {"type": "error", "run_id": run.id, "error": _gr["error"]}
                return
            cursor = _gr["next_cursor"]
            continue

        # SPEC §4.2 RunGuard: count this step execution (resume-safe scratchpad) BEFORE
        # anything runs, so a diverging loop trips the run-level TOTAL-STEP cap and halts
        # CLEANLY (FAILED 'loop_cap_reached'). Inert on a linear run; no partial targets
        # (the segment write-back happens only after the whole loop, on success).
        cap_reason = guard.enter(step)
        if cap_reason:
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status=RunStatus.FAILED,
                    input_text=SEG_JOINER.join(working),
                    output_text="",
                    error=cap_reason,
                    outcome_index=guard.visit_index(step),
                    started_at=step_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            _commit_run_terminal_direct(session, run, RunStatus.FAILED)
            yield {"type": "error", "run_id": run.id, "error": cap_reason}
            return

        # SPEC §5 PRE-STEP GUARD (Phase 2): identical logic to the FULL_TEXT loop via
        # the shared _eval_run_condition seam. NULL run_condition short-circuits (no
        # evaluator call, no SKIPPED row, no event) = byte-identical to today. On a skip
        # the rolling text (`working`) passes through UNCHANGED so a later step still
        # sees the prior output; we advance current_step_index + the cursor and emit the
        # additive step_skipped event. A SKIP never routes a branch (no gate) -> cursor+1.
        if step.run_condition and not _eval_run_condition(step, run, session):
            skip_blob = SEG_JOINER.join(working)
            session.add(
                StepRun(
                    workflow_run_id=run.id,
                    step_id=step.id,
                    status=RunStatus.SKIPPED,
                    input_text=skip_blob,
                    output_text=skip_blob,
                    outcome_index=guard.visit_index(step),
                    started_at=step_started_at,
                    finished_at=datetime.utcnow(),
                )
            )
            run.current_step_index = cursor + 1
            session.add(run)
            session.commit()
            yield {
                "type": "step_skipped",
                "run_id": run.id,
                "step_index": cursor,
                "step_name": step.name or step.step_type,
                "step_type": step.step_type,
                "condition": dict(step.run_condition),
                "output": skip_blob,
            }
            cursor = cursor + 1
            continue

        if step.step_type == StepType.TM_MATCH:
            # PROPOSAL-tm-matching-workflow-step (§7). ORCHESTRATION-ONLY (D8): no
            # provider call, no prompt. Fill every candidate segment that has a 100%-
            # byte-exact TM hit into working[] + mark it tm_origin (the single write-back
            # stamps MT + tm_match_pct=100), and RECORD the lock plan applied after that
            # write-back. Candidacy + policy live in tm_lock (the SINGLE engine's shared
            # helpers -- there is no second implementation). A poisoned hit is caught by
            # the write-back seam (routed to review, NEVER locked -- the post-loop pass
            # locks only a clean status==MT commit).
            from app.services import tm_lock

            # Batch 15 T1 (SPEC-tm-match-step-config §5.1b / N2): stamp the durable
            # provenance marker whenever the SEGMENTED tm_match branch EXECUTES (even if it
            # fills nothing, and even with TM_LOCKS_ENABLED off) -- the presence of the step
            # in the run is the fact the T2 delivery gate reads. Persisted with the StepRun
            # via _stage_tm_write below.
            run.used_tm_match = True
            _tm_locked_now = tm_hard_idx | tm_soft_idx
            # F4/F6 (Q9 fix round): FILLS and RE-LOCKS are ACCOUNTED SEPARATELY. A FILL is a
            # genuine USE of the TM (an empty slot got a target) -> it bumps use_count and is
            # reported as `filled`. A RE-LOCK is an accounting REPAIR (the target already
            # existed byte-exact; only its lost lock band is restored) -> it NEVER bumps usage
            # (a re-lock is not a USE of the TM) and is reported under a DISTINCT `relocked`.
            _filled_ct = 0  # genuine fills this step (reported as `filled`)
            _filled_ids: list[int] = []  # the SUBSET with a TM entry id -> usage bump
            _relocked_ct = 0
            _protected_ct = 0  # probe-5 content-protected rows (see content_protected)
            _healed_ct = 0  # PART 2 (option A): drifted MACHINE sentinels re-filled
            _noop_reason = ""
            if not config.TM_LOCKS_ENABLED:
                # L10: the kill switch must not CREATE new locks. With it off the step is
                # a no-op pass-through (no fill, no lock) -- fully inert.
                _noop_reason = "tm locks disabled (TM_LOCKS_ENABLED=0)"
            else:
                # Batch 15 T1 (SPEC-tm-match-step-config §6.1, LAW-TM-1): the EFFECTIVE
                # per-step policy (per-step override else the live global). A pre-Batch-15
                # step (all overrides NULL) resolves the exact global policy + threshold 100
                # it always did (LAW-TM-3).
                _pol = tm_lock.resolve_policy_for_step(session, project, step)
                # Batch 15 T1 (§7, LAW-TM-9): resolve the per-step TM pool selection to the
                # concrete readable id set, fail-closed + narrow-only. NULL = all readable
                # (restrict None, byte-identical). A non-NULL selection intersects with the
                # live readable pool set; ids outside it are DROPPED + surfaced as a run
                # note; a selection that resolves to the empty set matches nothing (an empty
                # set, distinct from None).
                _restrict_pids = None
                _raw_pool_sel = getattr(step, "tm_pool_selection", None)
                if _raw_pool_sel is not None:
                    from app.services import project_sources

                    _readable = {doc.project_id} | set(
                        project_sources.allowed_tm_source_ids(session, doc.project_id) or ()
                    )
                    _sel = {int(p) for p in _raw_pool_sel if p is not None}
                    _restrict_pids = _sel & _readable
                    _dropped = sorted(_sel - _readable)
                    if _dropped:
                        _dropped_txt = ", ".join(str(d) for d in _dropped)
                        _stamp_note(run, f"tm_match TM pool {_dropped_txt} not readable -- skipped")
                    # L2: a non-NULL selection that resolves to the EMPTY set matches nothing
                    # (fail-closed) -- surface it explicitly, whether the author wrote `[]`
                    # (no ids to drop, so the note above never fires) or every named id was
                    # unreadable. NULL still means all-readable and is never noted.
                    if not _restrict_pids:
                        _stamp_note(
                            run,
                            "tm_match TM pool selection is empty -- no TM pool read this step",
                        )
                # #617 item 5: re-running tm_match REFRESHES a MACHINE fill whose TM entry was
                # edited after the fill (the segment still holds the old target under a stored
                # lock, which the lock-seed above would otherwise skip). Only a persisted lock
                # on an MT/NEW row qualifies: an approved or edited row is human-final and keeps
                # its lock (it just shows the drift badge). The row is unlocked and handed to the
                # normal exact-100 heal below, which re-fills it from the entry and re-locks it
                # with a fresh `matched_at`. A row the heal does not take is restored after the
                # loop, so a refresh never leaves a segment unprotected.
                _refresh: list = []
                _drift = tm_lock.drifted(
                    session, [segments[j] for j in sorted(_tm_locked_now) if j not in tm_lock_plan]
                )
                for _rj in sorted(_tm_locked_now):
                    _rseg = segments[_rj]
                    if (
                        _rj not in tm_lock_plan
                        and getattr(_rseg, "id", None) in _drift
                        and getattr(_rseg, "status", None) in (SegStatus.MT, SegStatus.NEW)
                        and _rj not in operator_edited
                    ):
                        _refresh.append(
                            (_rj, _rseg.lock_band, bool(_rseg.lock_hard), dict(_rseg.meta or {}))
                        )
                        tm_hard_idx.discard(_rj)
                        tm_soft_idx.discard(_rj)
                        produced.discard(_rj)
                        tm_lock.clear_lock(_rseg)
                        session.add(_rseg)
                if _refresh:
                    _tm_locked_now = tm_hard_idx | tm_soft_idx
                # B4: FILL candidacy = STORED target empty (never clobber an existing
                # human/MT translation), not produced this run, not already locked.
                _cands = tm_lock.candidate_indices(segments, produced, _tm_locked_now)
                _cset = set(_cands)  # F8: O(1) membership for the re-lock winnow below
                # Q9 (core-QA fix train 2): ALSO RE-LOCK a segment whose stored target
                # ALREADY byte-equals its own 100% TM hit. A prior TM-exact fill -- the
                # plain-`translate` exact-TM short-circuit (translate.py), or an
                # abandoned/partial run -- stamps tm_match_pct=100 but NEVER a lock band;
                # tm_match then (correctly, B4) skips the non-empty target, so it sat
                # UN-protected while looking TM-trusted and a later lane rewrote it (the
                # live PO breach: pct=100 + lock_band=None + a rewritten sentinel). The
                # extra scope is every OTHER in-scope index (non-empty target); the
                # per-segment decision (fill / re-lock / skip-a-different-translation) is
                # `tm_lock.fill_or_relock`, whose byte-equality gate keeps re-lock B4-safe.
                _relock = [
                    j
                    for j in range(len(segments))
                    if j not in produced and j not in _tm_locked_now and j not in _cset
                ]
                _scope = _cands + _relock
                if not _scope:
                    # L3: a tm_match with nothing to fill OR re-lock (everything already
                    # produced/locked this run) -- report it, don't fail silently.
                    _noop_reason = (
                        "no un-translated segments to match "
                        "(is this tm_match after a translate step?)"
                    )
                else:
                    # Batch 15 T1 (§6.2, LAW-TM-20): TWO score floors. Widen the lookup to
                    # the effective FILL threshold (so an empty candidate can take a
                    # below-100 fuzzy hit), but keep the RE-LOCK / HEAL scope EXACT-100.
                    #
                    # M7 (cost): use TWO lookups, not one lowered floor over the whole scope.
                    # The low fill floor drops the fuzzy winnow bound to ~0.74*qlen (0.33 at
                    # 50), so a single lowered lookup would scan a huge fuzzy candidate pool
                    # for EVERY non-empty (_relock) segment and then discard every fuzzy hit
                    # at min_fill=100 -- a real cost regression on large docs/TMs. Instead:
                    #   * the EMPTY candidate scope (_cands) queried at min_score = threshold
                    #     (it is the only scope that may take a below-100 fill), and
                    #   * the NON-empty re-lock/heal scope (_relock) kept at min_score = 100
                    #     (a fuzzy hit can never byte-equal a stored target nor heal a drifted
                    #     MT one -- LAW-TM-20).
                    # A source shared between the two scopes: the _cands (low-floor) result
                    # wins the merge, and the PER-INDEX min_fill in the loop below still keeps
                    # a _relock-scope index from ever accepting a fuzzy hit (belt-and-braces,
                    # H2). An exact-100 hit is returned by the hash short-circuit at ANY floor,
                    # so the two lookups agree for every source with an exact entry. A
                    # threshold of 100 collapses both to today's single min_score=100
                    # (byte-identical, LAW-TM-3). The per-step pool selection narrows the
                    # readable set (restrict_pids).
                    _cand_hits = tm.lookup_batch(
                        session,
                        doc.project_id,
                        [seg_sources[j] for j in _cands],
                        src,
                        locale,
                        min_score=_pol.fill_threshold,
                        restrict_project_ids=_restrict_pids,
                    )
                    _relock_hits = tm.lookup_batch(
                        session,
                        doc.project_id,
                        [seg_sources[j] for j in _relock],
                        src,
                        locale,
                        min_score=100,
                        restrict_project_ids=_restrict_pids,
                    )
                    # cand wins on a shared source (a _relock-only source keeps its 100-floor
                    # result -- possibly None -- so a fuzzy-only match never reaches a
                    # re-lock/heal index).
                    _tm_hits = {**_relock_hits, **_cand_hits}
                    for j in _scope:
                        # EMPTY candidate -> the effective fill threshold; a non-empty
                        # re-lock-scope slot -> EXACT 100 only (LAW-TM-20).
                        _min_fill = _pol.fill_threshold if j in _cset else 100
                        _dec = tm_lock.fill_or_relock(
                            getattr(segments[j], "target_text", ""),
                            _tm_hits.get(seg_sources[j]),
                            _pol,
                            min_fill=_min_fill,
                            seg_source=seg_sources[j],
                        )
                        if _dec is None:
                            # B4: no 100% hit, or a DIFFERENT existing translation --
                            # never clobbered, never locked.
                            #
                            # PART 2 (option A, 2026-08-13) -- HEAL a DRIFTED MACHINE sentinel.
                            # The permanent-strand class (DECISION-concurrent-run-clobber.md): a
                            # segment whose source has a 100% TM entry but whose stored target is
                            # MACHINE (`mt`/`new`) and DIFFERS from that entry. B4 leaves it here
                            # ("a different existing translation, never clobber"), and the MT step
                            # then re-scaffolds it every run -- tm_match and MT hold contradictory
                            # contracts and MT wins, so the TM match is lost with no provenance and
                            # no flag, forever. Relax B4 for MACHINE-status rows ONLY: re-fill from
                            # TM through the SAME fill path below (working[] + tm_origin + lock plan
                            # + tm_plan_tgt), so a 100% TM match beats stale/drifted MT and the
                            # write-back applies every gate (char_limit, tag, refusal) exactly as a
                            # fresh fill. Bounded to the observed class; counted as `tm_healed`.
                            #
                            # HUMAN statuses keep ABSOLUTE B4 protection (edited/approved are never
                            # touched -- an operator's DIFFERENT translation is authoritative). And
                            # NEEDS_REVIEW is DELIBERATELY NOT healed: healing never touches a routed  # noqa: E501
                            # row (a QE fail, a refusal, an operator-flagged row) -- re-filling it
                            # from TM would erase the routing verdict + the reviewer's pending task.
                            # (A byte-EXACT needs_review TM sentinel reaches the probe-5 `relock`
                            # decision above, not this branch.) NOTE: this bounds the HEAL only; a
                            # later MT step in the SAME run may still overwrite a drifted needs_review  # noqa: E501
                            # row -- that is pre-existing, separate MT-step behaviour, not this heal.  # noqa: E501
                            #
                            # INFO-3: also EXCLUDE an operator-edited slot (j in operator_edited). A
                            # machine-status operator edit is usually in `produced` (working != source)  # noqa: E501
                            # and so already outside the fill/relock scope, but the RESIDUAL-1 "leave  # noqa: E501
                            # this in English" case -- an operator typing the SOURCE at a pause -- is  # noqa: E501
                            # NOT in produced (working == source), so an in-run [review, tm_match] would  # noqa: E501
                            # otherwise HEAL a 100% hit right over their deliberate edit. The explicit  # noqa: E501
                            # exclusion keeps a post-pause tm_match from clobbering operator work.
                            _hit_j = _tm_hits.get(seg_sources[j])
                            if (
                                _hit_j is not None
                                and j not in operator_edited
                                and getattr(segments[j], "status", None)
                                in (SegStatus.MT, SegStatus.NEW)
                            ):
                                # LAW-TM-20: the HEAL branch is EXACT-100 ONLY (min_fill=100)
                                # -- a fuzzy hit can NEVER overwrite an existing differing
                                # MT/NEW target through this direct-plan_hit path.
                                _hplan = tm_lock.plan_hit(
                                    _hit_j, _pol, min_fill=100, seg_source=seg_sources[j]
                                )
                                if _hplan is not None:
                                    _hband, _hhard = _hplan
                                    _htgt = _hit_j.get("target_text") or ""
                                    _hpct = _hit_j.get("match_pct") or 100
                                    working[j] = _htgt
                                    tm_origin[j] = True
                                    produced.add(j)
                                    tm_fill_pct[j] = _hpct
                                    (tm_hard_idx if _hhard else tm_soft_idx).add(j)
                                    tm_lock_plan[j] = (_hband, _hhard, _hit_j.get("id"), _hpct)
                                    tm_plan_tgt[j] = _htgt
                                    _healed_ct += 1
                                    # a heal is a genuine USE of the TM -> bump use_count like a
                                    # fill (unlike a re-lock, which only restores a lost band).
                                    _hid = _hit_j.get("id")
                                    if _hid is not None:
                                        _filled_ids.append(_hid)
                            continue
                        if _dec[0] == "relock":
                            # Re-lock is a WHITELIST (fix round F1/F2): ONLY a segment still
                            # carrying the exact machine-TM fingerprint -- tm_match_pct == 100
                            # AND status == MT (precisely what the plain-translate exact-TM
                            # short-circuit leaves on the Q9 seed) -- is repaired. EVERY other
                            # status is EXCLUDED on purpose, because a re-lock forces
                            # status->MT + a lock band + TM provenance and clears stale flags:
                            #   * NEEDS_REVIEW -- a QE-fail / step-error badge row would be
                            #     silently promoted to mt+locked (connector-pushable) and its
                            #     qa_flags ERASED by the stale-clear; review routing lost.
                            #   * EDITED -- a CAT-editor "confirm unchanged" leaves
                            #     EDITED + pct=100 + byte-equal target; re-locking would
                            #     relabel human work as machine.
                            #   * APPROVED / NEW -- never machine TM fills that lost a lock.
                            # The plain-translate shortcut leaves EXACTLY MT + pct=100 on the
                            # seed state, so the Q9 repair still fires.
                            _seg_j = segments[j]
                            # CONTENT-PROTECTION (Q9 re-review, probe 5 + F1 follow-up):
                            # reaching a "relock" decision here PROVES the stored target
                            # byte-equals the resolving 100% hit (that is fill_or_relock's
                            # byte-equality gate -- no parallel machinery). Three of the statuses
                            # the whitelist below EXCLUDES from re-lock are nonetheless
                            # byte-exact-DESTRUCTIBLE (pct == 100 + lock_band == None, the Q9
                            # seed shape) -- if left a bare `continue` they drop into the next
                            # step's pending set where an MT step RE-TRANSLATES them and destroys
                            # the sentinel:
                            #   * NEEDS_REVIEW -- the byte-exact soft-gated fill + qa badge
                            #     (probe 5): re-MT destroys the sentinel and the always-on
                            #     stale-clear erases the badge; review routing lost.
                            #   * EDITED / APPROVED (F1) -- a human "confirm unchanged" or a
                            #     manager sign-off on content that byte-equals the current TM:
                            #     re-MT SILENTLY OVERWRITES the human-finalized target with raw
                            #     MT and flips status->mt, destroying the sign-off (worse than
                            #     the NEEDS_REVIEW case). Since it byte-matches the CURRENT TM,
                            #     protecting it never freezes stale content -- approved content
                            #     that legitimately DIFFERS from TM makes fill_or_relock return
                            #     None and is out of scope (the broader non-byte-match APPROVED
                            #     clobber is a separate, chipped issue -- NOT fixed here).
                            # For all three we PROTECT rather than re-label: seed working[] from
                            # the STORED target (so the run blob / any LQA judge see the real
                            # content and `produced` holds), add it to `produced` (no later
                            # re-fill / re-translate) and to `content_protected` (pending-set
                            # exclusion + a write-back that leaves target_text/status/qa_flags
                            # EXACTLY as stored). NO tm_origin, NO lock, NO lock plan -- neither
                            # re-labelled nor locked. A row whose target DIFFERS from the hit
                            # never reaches here (fill_or_relock returned None) -- it is genuinely
                            # editable and stays translatable.
                            if getattr(_seg_j, "tm_match_pct", None) == 100 and getattr(
                                _seg_j, "status", None
                            ) in (SegStatus.NEEDS_REVIEW, SegStatus.EDITED, SegStatus.APPROVED):
                                working[j] = getattr(_seg_j, "target_text", "") or ""
                                produced.add(j)
                                content_protected.add(j)
                                _protected_ct += 1
                                continue
                            if (
                                getattr(_seg_j, "tm_match_pct", None) != 100
                                or getattr(_seg_j, "status", None) != SegStatus.MT
                            ):
                                continue
                        # Batch 15 T1: _dec is a 4-tuple (action, band, hard, lock_mode).
                        _band, _hard, _mode = _dec[1], _dec[2], _dec[3]
                        _hit = _tm_hits[seg_sources[j]]
                        _tgt = _hit["target_text"]
                        _pct = _hit.get("match_pct") or 100
                        # A re-lock re-writes IDENTICAL text (the stored target already IS
                        # the hit target); a fill writes it fresh. Both ride the single
                        # write seam below, so both clear the same gates before locking.
                        working[j] = _tgt
                        tm_origin[j] = True
                        produced.add(j)
                        # LAW-TM-19: record the REAL match_pct for the write-back stamp
                        # (exact = 100 byte-identical; a fuzzy fill = its true score).
                        tm_fill_pct[j] = _pct
                        if _mode == "none":
                            # §6.3 / LAW-TM-7: a fuzzy "none" fill is committed but NOT
                            # locked -- NO tm_hard_idx/tm_soft_idx, NO lock plan, NO
                            # tm_plan_tgt. It STAYS in pending_idx (subject only to the
                            # tm_protect guard), so a downstream UNPROTECTED translate step
                            # overwrites it (flipping tm_origin False), while a TM-only
                            # workflow keeps it to delivery. Never a lock, so the write seam
                            # never refuses a later edit.
                            pass
                        else:
                            (tm_hard_idx if _hard else tm_soft_idx).add(j)
                            tm_lock_plan[j] = (_band, _hard, _hit.get("id"), _pct)
                            # F5: remember the TM target this plan intends, so the write-back
                            # can DROP the plan (no stamp, no origin) if an operator edit
                            # during a human_review pause left working[j] no longer
                            # byte-equal to it.
                            tm_plan_tgt[j] = _tgt
                        if _dec[0] == "fill":
                            _filled_ct += 1
                            _hid = _hit.get("id")
                            if _hid is not None:
                                _filled_ids.append(_hid)
                        else:
                            _relocked_ct += 1
                    # #617 item 5: a refresh candidate the heal did not re-fill goes back exactly
                    # as it was (lock, provenance, protection from later steps).
                    for _rj, _rband, _rhard, _rmeta in _refresh:
                        if _rj in tm_lock_plan:
                            continue
                        _rseg = segments[_rj]
                        _rseg.lock_band = _rband
                        _rseg.lock_hard = _rhard
                        _rseg.meta = _rmeta
                        session.add(_rseg)
                        (tm_hard_idx if _rhard else tm_soft_idx).add(_rj)
                        produced.add(_rj)
                    # F7: scope was non-empty but produced NO genuine fill OR re-lock (every
                    # candidate was a different-translation skip, a sub-100 hit, or fell
                    # outside the machine-fingerprint whitelist) -- surface the no-op instead
                    # of reporting a silent success, exactly as an empty scope does. A
                    # content-PROTECTED row (probe 5) is genuine work the step did (it shielded
                    # a byte-exact sentinel from re-translation), so it too suppresses the no-op
                    # -- the step is neither a silent success nor a no-op.
                    if (
                        not _filled_ct
                        and not _relocked_ct
                        and not _protected_ct
                        and not _healed_ct
                        and not _noop_reason
                    ):
                        # Batch 15 T1 (finding 20): name the EFFECTIVE threshold -- the old
                        # "no 100% TM matches" copy is a lie under a below-100 fill threshold.
                        _noop_reason = (
                            f"no TM matches at or above {_pol.fill_threshold}% "
                            "to fill or re-lock in scope"
                        )
            if _filled_ids:
                # F4: bump usage for GENUINE FILLS ONLY -- a re-lock is an accounting repair,
                # not a USE of the TM, so it never touches use_count/last_used_at.
                from app.services import tm_module

                tm_module.bump_usage(session, _filled_ids, project_id=doc.project_id)
            _blob = SEG_JOINER.join(working)
            # P-C5 / flood R3-F1: this TM-match branch writes its OWN step completion
            # (StepRun insert + run step-index advance) and early-`continue`s, BYPASSING the
            # shared dispatch seam below -- so it must ride the SAME bounded, jittered
            # writer-lock retry that seam uses (see _stage_step_write ~5227), or a transient
            # herd collision on THIS commit fails the whole run. (The R3 flood's honest
            # mid-run lock failures, 2/8529 at 50 concurrent, were on exactly this line.)
            # Genuinely SEAM-SHAPED, not the in-transaction ceiling: only local TM work (DB
            # reads + staging) precedes it -- no provider/network call is held open across the
            # commit, so this is the same clean single-unit write the shared seam retries, not
            # the orchestrator-holds-a-write-across-a-provider-call circular wait. The StepRun
            # is built ONCE so a retry never drifts its timestamp; `_stage` re-applies both
            # mutations after the rollback a lost attempt performs. Byte-identical to a plain
            # commit on the uncontended path.
            _tm_sr = StepRun(
                workflow_run_id=run.id,
                step_id=step.id,
                status=RunStatus.COMPLETED,
                input_text=SEG_JOINER.join(seg_sources),
                output_text=_blob,
                outcome_index=guard.visit_index(step),
                started_at=step_started_at,
                finished_at=datetime.utcnow(),
            )

            # #504: a retry rollback also reverts used_tm_match, the stamped pool notes and
            # any other staged run state; capture it once and re-apply it per attempt.
            _tm_run_attrs = _pending_run_attrs(run)

            def _stage_tm_write(_row=_tm_sr, _cur=cursor, _attrs=_tm_run_attrs):
                session.add(_row)
                _reapply_run_attrs(run, _attrs)
                run.current_step_index = _cur + 1
                session.add(run)

            _db.commit_with_retry(session, stage=_stage_tm_write)
            # F6: `filled` counts GENUINE fills only (a re-lock is not a fill); `relocked`
            # is the DISTINCT re-lock tally. locked_hard/locked_soft keep their semantics --
            # the cumulative plan (fills AND re-locks) this run intends to lock.
            _tm_ev = {
                "filled": _filled_ct,
                "relocked": _relocked_ct,
                "locked_hard": sum(1 for p in tm_lock_plan.values() if p[1]),
                "locked_soft": sum(1 for p in tm_lock_plan.values() if not p[1]),
            }
            # probe-5: surface the content-protected tally ONLY when it fired, so a run with no
            # such seed keeps a byte-identical `tm_match` event shape (like `noop`).
            if _protected_ct:
                _tm_ev["content_protected"] = _protected_ct
            # PART 2: surface the healed-drift tally ONLY when it fired, so a run with no drifted
            # machine sentinel keeps a byte-identical `tm_match` event shape (like content_protected).  # noqa: E501
            if _healed_ct:
                _tm_ev["tm_healed"] = _healed_ct
            if _noop_reason:
                _tm_ev["noop"] = _noop_reason
            # #610: keep the step's TM tally on the run so the run page, GET /api/runs/{id}
            # and `workflow status` can show "N of M filled from TM" after the live stream is
            # gone. Merged into the existing context (the same pattern as `warnings`); the
            # last tm_match visit wins. `unmatched` is everything the TM did not take at the
            # fill threshold: those segments go on to the next step (usually machine
            # translation).
            # A HEAL (a drifted machine row re-filled from a 100% hit) is a genuine TM fill, and
            # a content-PROTECTED row already holds the exact TM text and is kept as is: neither
            # is "not matched, sent on" (gate finding on #610).
            _tm_total = len(segments)
            _tm_filled = _filled_ct + _healed_ct
            _tm_kept = _relocked_ct + _protected_ct
            _tm_summary = {
                "filled": _tm_filled,
                "relocked": _tm_kept,
                "total": _tm_total,
                "unmatched": max(0, _tm_total - _tm_filled - _tm_kept),
            }

            def _stage_tm_summary(_s=_tm_summary):
                run.context = {**(run.context or {}), "tm_match_summary": _s}
                session.add(run)

            _db.commit_with_retry(session, stage=_stage_tm_summary)
            yield {
                "type": "step",
                "run_id": run.id,
                "step_index": cursor,
                "step_name": step.name or "TM match",
                "step_type": step.step_type,
                "output": _blob,
                "tm_match": _tm_ev,
            }
            cursor = cursor + 1
            continue

        # The blob handed to the shared per-step dispatch: the assembled current target
        # of ALL segments (the human_review pause hands this to the operator; resume
        # re-splits it by index; the LQA judge scores it; the spend-gate StepRun records
        # it). Computed ONCE here where working[] is in its post-tm_match, pre-fan-out
        # state — byte-identical to the two separate joins it replaces.
        input_blob = SEG_JOINER.join(working)

        # Shared per-step dispatch (the execute-one-step seam): human_review pause, the
        # LQA judge, and the llm_agent spend gate live in `_execute_step` — the ONE place
        # both run loops route these step types through. `src_blob` / `_lqa_terms` /
        # `_seg_pause_ctx` are this loop's (SEGMENTED) mode-specific values. On
        # `fallthrough` the per-segment fan-out below runs; on a handled step (LQA) the
        # fresh findings block is threaded on; on a stop (pause / failure) the run is
        # already finalized and we return.
        _check_src, _check_terms, _check_scope = src_blob, None, None
        if step.step_type == StepType.LQA:
            _check_src, input_blob, _check_terms, _check_scope = _lqa_inputs([step])
        else:
            _check_terms = _lqa_terms_now()
        _directive = yield from _execute_step(
            session,
            run,
            steps,
            step,
            cursor,
            guard=guard,
            key_index=key_index,
            input_blob=input_blob,
            lqa_src=_check_src,
            lqa_terms=_check_terms,
            lqa_scope=_check_scope,
            external_glossary=_lqa_terms,
            sg_render=_sg_render,
            workflow_id=workflow_id,
            doc_id=doc_id,
            env_ids=_env_ids,
            src=src,
            locale=locale,
            step_started_at=step_started_at,
            pause_context_fn=_seg_pause_ctx,
            dispatch_payload_fn=_seg_dispatch_payload,
            post_dispatch_persist_fn=_seg_persist_inrun_hard_locks,
        )
        if _directive.stop:
            return
        if not _directive.fallthrough:
            if _directive.lqa_findings_block is not _UNSET:
                lqa_findings_block = _directive.lqa_findings_block
            cursor = _directive.next_cursor
            continue

        # llm_agent (or default) — run the step over every segment.
        spec = _spec_for_step(step)
        # AI Tagging 1.5c: remember the last step's resolved spec for the write-gate
        # commit loop below, so an LLM tag repair runs on the step's PINNED engine +
        # fallback chain rather than a rebuilt provider default.
        _tag_spec = spec
        _tag_redact = _tag_redact or bool(step.redact_pii)
        if step.step_type == StepType.LQA:
            _tag_verify_spec = spec
        # SPEC-prompt-template-runtime: resolve the effective prompt ONCE (inline > bound
        # template > default) via the SAME seam the FULL_TEXT path uses. A dangling/invalid
        # binding falls back to the default with `tmpl_note` (surfaced on the StepRun).
        resolved = _resolve_step_template(session, step)
        template = resolved.text
        tmpl_note = resolved.note
        # #343: the step's literal prompt 4-grams for the output-guard instruction-echo check
        # (computed once per step; empty when the refusal kill switch is off).
        _og_grams = (
            output_guard.literal_prompt_ngrams(
                template,
                _system_for({}, "", src_locale=src, tgt_locale=locale, head_body=_sys_head_body),
            )
            if config.REFUSAL_GUARD_ENABLED
            else set()
        )
        # GitHub #344/#353: the fence sentence rides the system message of fenced calls, so an
        # echo of it counts as an instruction echo too, but by run length (`fence_echo`), not
        # by lone 4-grams, which ordinary same-language rewrites contain.
        # #362: the memory sentence rides the same system messages, so its echo counts too.
        _og_fence = prompts.FENCE_ECHO_TEXT if config.REFUSAL_GUARD_ENABLED else None
        # Change 1: expose {lqa_findings} to this step's template (via base_ctx, read by
        # _run_step_on_segment) AND compute the correction-loop auto-append suffix. Both
        # "" until an LQA has run / unless this is a fix step -> byte-identical.
        base_ctx["lqa_findings"] = lqa_findings_block
        # Attribute once per step against ALL current texts, including resumed edits.
        # A finding matching nothing is unattributable and stays on every segment.
        #
        # #163: prefer the judge's STRUCTURED span over quotes scraped from its prose.
        # `_QUOTED_RE` treats an apostrophe as both an opener and a closer, so ordinary
        # English ("The target's register doesn't match") yields a junk fragment that can
        # never match any segment. Since #160 that predicate is not only a sort key but a
        # DELETE key, so a bad fragment costs a finding the segment it was really about.
        # The span is the judge's own answer to the same question and is already trusted
        # by `lqa_safety.adjudicate`. Both paths go through `_fragment_occurs`, so the two
        # sources cannot disagree about what "occurs in this segment" means.
        #
        # The fallback is NOT vestigial: rows judged before this shipped carry no span, a
        # run paused before it and resumed after has no map, and a judge is free to ignore
        # the instruction. In every one of those cases a finding with no usable span falls
        # back to the prose scrape and, failing that, is unattributable -- which under
        # #160's rule keeps it on EVERY segment. Brittleness costs SCOPING, never CONTENT.
        _finding_spans = (run.context or {}).get("lqa_findings_spans") or {}

        # Every per-step value this closure reads is bound as a DEFAULT, not captured: it is
        # defined inside the per-step loop, and a late-bound name would read a LATER step's
        # value if the function ever outlived its iteration. `segments` and `working` are
        # loop-invariant here and bound anyway, so the rule is "this closure captures
        # nothing" rather than a judgement call a later edit has to re-make correctly. Same
        # guard the tap-meta closure below uses for `step`.
        def _matching_segments(line, _spans=_finding_spans, _segs=segments, _work=working):
            _n = _FINDING_LINE_RE.match(line)
            _span = _spans.get(_n.group(1)) if _n else None
            if _span:
                hits = {
                    j
                    for j, segment in enumerate(_segs)
                    if _fragment_occurs(_span, segment.source_text, _work[j])
                }
                if hits:
                    return hits
                # A paraphrased or hallucinated span occurs nowhere. Fall through to the
                # prose scrape rather than declaring the finding unattributable outright,
                # so a judge that half-honours the instruction is no worse off than one
                # that ignores it entirely.
            return {
                j
                for j, segment in enumerate(_segs)
                if _findings_relevant(line, segment.source_text, _work[j])
            }

        finding_matches = {
            line: _matching_segments(line)
            for line in lqa_findings_block.splitlines()
            if len(segments) > 1 and _FINDING_LINE_RE.match(line)
        }
        findings_excluded = [
            frozenset(
                line for line, matches in finding_matches.items() if matches and j not in matches
            )
            if segment.source_text or working[j]
            else frozenset()
            for j, segment in enumerate(segments)
        ]
        # ...and the POSITIVE half of the same map, for the per-segment cap's ordering. The
        # cap used to re-sort on the prose predicate, which would have thrown away exactly
        # what the span just bought: a finding whose prose is unattributable but whose span
        # names THIS segment is admitted above, then sorted into the tail and truncated.
        # One map decides both, so admission and survival cannot disagree.
        #
        # None, NOT an empty frozenset, for a single-segment document. `finding_matches` is
        # itself gated on `len(segments) > 1`, so a one-segment run has an EMPTY map, and an
        # empty frozenset would say "nothing is relevant to this segment" rather than "I have
        # no opinion" -- disabling relevance ordering entirely and truncating away the very
        # finding that IS about the segment. A single-unit caller must keep the prose
        # ordering it had before this train, which is what `relevant_findings=None` selects.
        findings_relevant = [
            frozenset(line for line, matches in finding_matches.items() if j in matches)
            if len(segments) > 1
            else None
            for j in range(len(segments))
        ]
        findings_suffix = _findings_suffix(step, steps, template, lqa_findings_block)

        # Prompt Tap correlation for this step's per-segment calls (observer-only;
        # segment_index is added per-call below). Built here where the workflow/run/
        # doc/step identity is all cheaply in scope.
        def _seg_tap_meta(idx, _step=step):
            # Envelope ids (proposal Wave A) stamped HERE so every consumer of this
            # closure -- the translate fan-out, the QE judge, _seg_tap_meta_mem -- rides
            # the same action/run without a per-site edit.
            return tap_envelope.stamp_meta(
                {
                    "workflow_id": workflow_id,
                    "workflow_run_id": run.id,
                    "step_key": _step.key,
                    "step_name": _step.name or _step.step_type,
                    "document_id": doc_id,
                    "segment_index": segments[idx].order_index,
                },
                *_env_ids,
            )

        # PII: per-step choice OR the global setting. Each pending segment gets its
        # own pii_map inside _run_step_on_segment; the restored output is what lands
        # in the StepRun blob and Segment.target_text (raw PII never persisted).
        redact = step.redact_pii or pii.enabled()

        # TM-first short-circuit, ONLY on step order_index == 0: a 100% segment-TM
        # hit fills the working text and SKIPS the provider call for that segment.
        # Later steps always run on the (TM-filled) working text.
        pending_idx = []
        # LOW-1 (gate round 5): the indices this step-0 visit FILLED from a 100% TM match.
        # Those segments are delivered THIS visit (not carry-over) but are never appended to
        # `pending_idx` (no provider call), so the output_chars sum below must add their chars
        # back -- a TM fill is this visit's delivery, not a prior visit's kept text.
        tm_filled_idx: set[int] = set()
        # #705: the tags-only slots this step-0 visit delivered by copy (no provider call).
        tags_only_step0_idx: set[int] = set()
        if step.order_index == 0:
            needs = [seg_sources[j] for j in range(len(segments))]
            tm_matches = tm.lookup_batch(session, doc.project_id, needs, src, locale)
            applied_tm_ids: list[int] = []  # SegmentTM entries whose target we APPLIED
            for j in range(len(segments)):
                if j in status_protected:
                    # STATUS PROTECTION: a human-approved/edited segment is never TM-filled
                    # nor dispatched. working[j] already holds its stored target (seeded at
                    # run start); skip it here so no provider call and no usage bump touch it.
                    continue
                if j in tags_only_idx:
                    # #705: no translatable text -> never dispatched; the write-back copies
                    # the source. Delivered THIS visit like a step-0 TM fill (output_chars).
                    tags_only_step0_idx.add(j)
                    continue
                exact = tm_matches.get(seg_sources[j])
                if exact and exact["match_pct"] == 100:
                    working[j] = exact["target_text"]
                    tm_filled_idx.add(j)  # LOW-1 (round 5): delivered THIS visit via TM
                    tm_origin[j] = True  # F5: keep TM provenance at write-back
                    # F1 (Q9 final round): this step-0 short-circuit fills TM content with
                    # no tm_match step and hence no lock plan -- but the F5 pause-edit guard
                    # keyed only on tm_lock_plan, so an operator edit during a later
                    # human_review pause resumed with the ORIGINAL TM provenance stamped onto
                    # the edited text (MT + pct=100). Record the intended TM target here too
                    # (no tm_lock_plan entry -- this path never locks) so the write-back guard
                    # detects the edit and drops the false stamp. Keyed on tm_plan_tgt alone.
                    tm_plan_tgt[j] = exact["target_text"]
                    if exact.get("id") is not None:
                        applied_tm_ids.append(exact["id"])
                else:
                    pending_idx.append(j)
            # Usage tracking (TM module): bump every entry actually applied at step 0.
            # One bulk UPDATE, accumulated, never fails the run (bump_usage swallows).
            if applied_tm_ids:
                from app.services import tm_module

                # project-scoped so an attached source project's rows are never
                # written by this project's run (see tm_module.bump_usage).
                tm_module.bump_usage(session, applied_tm_ids, project_id=doc.project_id)
            # A TM-filled segment holds a real translation from here on, so the (C)
            # fixer post-check may adjudicate a later step's edit of it.
            produced.update(j for j in range(len(segments)) if tm_origin[j])
        else:
            # #705: a tags-only slot is never handed to a later step either.
            pending_idx = [j for j in range(len(segments)) if j not in tags_only_idx]

        # One refusal policy for dispatch and correction-loop judge scope.
        # Preserve the existing D1-only counter and safety-floor note.
        tm_protected_idx = []
        step_refused = lqa_safety.step_refused_indices(
            pending_idx,
            tm_origin,
            step,
            _is_correction_loop_step(step, steps),
            tm_hard_idx=tm_hard_idx,
            tm_soft_idx=tm_soft_idx,
            content_protected=content_protected,
            status_protected=status_protected,
            refused_seg=refused_seg,
            tm_protected_idx=tm_protected_idx,
        )
        pending_idx = [j for j in pending_idx if j not in step_refused]
        safety_run_stats["tm_protected"] += len(tm_protected_idx)

        # F2.2: the stable doc brief for THIS step (computed once per run, "" unless
        # the step opts in). F2.1: whether this step self-propagates feedback.
        brief_blk = _doc_brief_block(step)
        adaptive_fb = bool(getattr(step, "adaptive_feedback", False))
        fb_window = adaptive.clamp_window(
            getattr(step, "adaptive_feedback_window", adaptive.DEFAULT_WINDOW)
        )

        # TM few-shot (2B): this step's rendered examples block for segment j, gated by
        # the step's own toggle + window (clamped 0-5). "" when OFF / window 0 / no
        # match cleared the floor -> byte-identical. Pairs were retrieved once per run.
        _tm_ex_on = bool(getattr(step, "tm_examples", True))
        _tm_ex_window = context_pkg.clamp_examples_window(
            getattr(step, "tm_examples_window", context_pkg.TM_EXAMPLES_DEFAULT_WINDOW)
        )

        def _tm_ex_block(j: int, *, _tm_ex_on=_tm_ex_on, _tm_ex_window=_tm_ex_window) -> str:
            if not _tm_ex_on or _tm_ex_window <= 0:
                return ""
            return context_pkg.render_tm_examples(seg_examples[j][:_tm_ex_window])

        # LLMemory recall (PROPOSAL-llmemory §5). ONE gate decision per step
        # (`_memory_eligible_step`: an llm_agent translate step, never the LQA judge,
        # never the correction-loop fixer, honouring `memory_mode="off"`), then per
        # segment: the term memories matching THAT segment's source, folded with the
        # run-cached doc-level note. `_mem_cache` is per-STEP (rebuilt each loop
        # iteration) and per-segment, so a segment retried within a step re-uses the
        # same selection — the prompt and the tap record can never disagree about which
        # memories were injected, whichever of the two is assembled first.
        _mem_on = _memory_eligible_step(step, steps, template)
        _mem_cache: dict = {}

        def _memory_for(j: int, _on=_mem_on, *, _mem_cache=_mem_cache):
            """(block, memory_ids) for segment j on THIS step. ("", []) when the step
            is not eligible, when recall is off, when the project has no customer, or
            when nothing clears the selection gates — the empty branch that keeps the
            assembled prompt byte-identical to pre-LLMemory."""
            if j in _mem_cache:
                return _mem_cache[j]
            out = ("", [])
            if _on:
                try:
                    s_blk, s_ids = llmemory.select_for_segment(
                        session,
                        customer_id=getattr(project, "customer_id", None),
                        project_id=doc.project_id,
                        source_locale=src,
                        target_locale=locale,
                        segment_source=seg_sources[j],
                        org_id=getattr(project, "org_id", None),
                        # THE PRECEDENCE LAW (proposal §5): this segment's ALREADY-
                        # RESOLVED approved glossary matches — the exact list this
                        # step's prompt is about to carry — so a memory that
                        # contradicts an approved term is dropped rather than sent
                        # alongside it. Reuses `seg_terms[j]` (matched once per run
                        # through the org/customer-scoped plan): no second query, and
                        # therefore no second, hand-rolled idea of which glossary
                        # applies here.
                        glossary_terms=seg_terms[j],
                        # The stamp waits for the FOLD (F1): see _mem_stamp.
                        mark_used=False,
                    )
                    n_blk, n_ids = _memory_doc_note()
                    # THE FOLD DECIDES PROVENANCE. It re-applies the block budget to the
                    # join of the two parts, so a line either renderer kept can still be
                    # dropped here — and a dropped memory shaped nothing. `kept` (not
                    # `s_ids + n_ids`) is therefore what rides into tap_meta AND what is
                    # usage-stamped.
                    blk, kept = prompts.compose_memory_block_kept(s_blk, n_blk, s_ids, n_ids)
                    out = (blk, list(kept))
                    _mem_stamp(kept, n_ids)
                except Exception:
                    log.warning("llmemory: segment recall failed", exc_info=True)
            _mem_cache[j] = out
            return out

        def _mem_stamp(kept_ids, note_ids) -> None:
            """Usage-stamp the memories that SURVIVED the fold. Term memories are
            per-segment, so each segment stamps its own survivors (the per-segment cache
            above means a retried segment does not double-count); the doc-level note is
            stamped at most ONCE per run, on the first segment whose fold kept it —
            preserving the "one note, one use per run" property while no longer counting
            a use the prompt never received."""
            note_set = set(note_ids or [])
            llmemory.mark_used_ids(session, [i for i in kept_ids if i not in note_set])
            note_kept = [i for i in kept_ids if i in note_set]
            if note_kept and not _mem_note_state[3]:
                _mem_note_state[3] = True
                llmemory.mark_used_ids(session, note_kept)

        def _mem_block(j: int) -> str:
            return _memory_for(j)[0]

        def _seg_tap_meta_mem(j: int) -> dict:
            """The translate call's tap meta PLUS the injected memory ids (proposal §5
            "Provenance"). Attached ONLY when something was injected, so every capture
            an install without memories makes is byte-identical to today — and so the
            QE/LQA judge captures (which use the plain `_seg_tap_meta`, because the
            judge is handed no memory) can never claim provenance they do not have."""
            meta = _seg_tap_meta(j)
            ids = _memory_for(j)[1]
            if ids:
                meta["memory_ids"] = list(ids)
            return meta

        # Per-string image context (Batch 14 T2, SPEC §6.2/§25). PRESENCE-based, NOT
        # capability-gated (N-1): resolve a segment's images whenever this step is
        # image-eligible and the key has ready images, REGARDLESS of whether the engine
        # can see them — run_completion decides per target whether to thread the bytes,
        # and the caller records a skip when they were present but nobody sent them. The
        # step-gate (LLM_AGENT AND the per-step use_image_context toggle, §25) is hoisted
        # ONCE per step-loop iteration and bound as a closure default — the faithful
        # `_memory_for` shape — so within this step's fan-out every segment sees the same
        # verdict and a mid-run propagated toggle flip takes effect at the NEXT step
        # boundary (§25.9), never mid-step. An opted-out LLM_AGENT step (or any non-agent
        # type) resolves NO images here, exactly like a non-image step, so it contributes
        # nothing to the skip accounting (§25.4). Bytes are resolved on THIS (DB) thread
        # BEFORE fan-out (BlobStore reads are forbidden on pool workers, §2.3) and cached
        # per key_id for the batch (M-5: the stored blob is already send-ready PNG).
        _img_on = _image_eligible_step(step)
        _img_map: dict[int, list[bytes]] = {}  # key_id -> selected send-ready PNG bytes
        if _img_on and key_images.enabled():
            _pending_kids = []
            for _pj in pending_idx:
                _kid = getattr(segments[_pj], "key_id", None)
                if _kid:
                    _pending_kids.append(_kid)
            if _pending_kids:
                # ONE grouped query (ready rows, project-anchored — LOW-2 defense-in-depth
                # KeyImage.project_id == doc.project_id), then select per S3 (primary, or
                # all when send_all_images) and cap at MAX_IMAGES_PER_REQUEST.
                _rows_by_key = key_images.images_for_keys(
                    session, list(set(_pending_kids)), doc.project_id
                )
                for _kid, _rows in _rows_by_key.items():
                    _send_all = any(getattr(r, "send_all_images", False) for r in _rows)
                    if _send_all:
                        _selected = list(_rows)[: key_images.MAX_IMAGES_PER_KEY]
                    else:
                        _primary = [r for r in _rows if r.is_primary] or list(_rows[:1])
                        _selected = _primary[: key_images.MAX_IMAGES_PER_REQUEST]
                    _byts: list[bytes] = []
                    for _row in _selected:
                        _b = key_images.read_bytes(_row)  # swallow a missing blob (§12)
                        if _b is not None:
                            _byts.append(_b)
                    if _byts:
                        _img_map[_kid] = _byts

        def _images_for(j: int, *, _on=_img_on, _img_map=_img_map) -> list[bytes]:
            """The send-ready image bytes for segment j on THIS step, or [] when the step
            is image-ineligible (non-LLM_AGENT OR use_image_context off), the feature is
            off, or the segment's key has no ready image. Plain data, session-free by the
            time it is handed to _run_step_on_segment (resolved above on the DB thread)."""
            if not _on:
                return []
            _kid = getattr(segments[j], "key_id", None)
            return _img_map.get(_kid, []) if _kid else []

        # QE routing gate (roadmap gap #1/#6). When qe_gate is ON, each segment this
        # step produces is scored by an MQM-aligned LLM judge; below-threshold routes
        # to review. Default OFF -> the closure is a no-op and NOTHING calls the judge
        # (byte-identical). `qe_step_stats` tallies THIS step; qe_run_stats aggregates
        # the run. The judge sees the same REDACTED text the engine saw (r["fb_source"]
        # / r["fb_output"]) when the step redacts — never raw PII (mirrors the buffer).
        qe_on = bool(getattr(step, "qe_gate", False))
        qe_spec = _qe_spec_for_step(step) if qe_on else None
        qe_threshold = qe.clamp_threshold(getattr(step, "qe_threshold", qe.DEFAULT_THRESHOLD))
        qe_step_stats = _new_qe_stats()
        qe_visit_indices: set[int] = set()
        qe_skip_reasons = set()
        # Real per-step-visit spend (feat/workflow-spend): the QE judge call is part of
        # THIS step-visit, so fold its exact ledger cost + tokens into the stamped
        # StepRun cost. A mutable dict (not a nonlocal) so the nested _score_seg can
        # accumulate without a nonlocal decl. Judge calls are made INSIDE the segment
        # fan-out on the DB thread here (both branches call _score_seg on the DB thread,
        # after join), so plain accumulation is race-free.
        _qe_spend = {"cost": 0.0, "tin": 0, "tout": 0}
        # Jev-compatible judges (the legacy "jev" string, or an engine:<id> of kind jev /
        # jev_local) are scored by qe_jev, not qe.py. The target is resolved ONCE per step
        # visit, not per segment: one profile read + key decrypt for the whole step. A
        # dead/disabled judge engine does not fail the run; every segment gets the same
        # skipped provider_error card naming it (the judge soft-degrades, like qe.py).
        _jev_judge = {"target": None, "error": None}
        if qe_on:
            from app.services import qe_jev

            if qe_jev.is_judge_ref(qe_spec.provider):
                try:
                    _jev_judge["target"] = qe_jev.resolve_target(qe_spec.provider)
                except (DeadEngineError, ValueError) as _jev_exc:
                    _jev_judge["error"] = str(_jev_exc)

        def _qe_cancelled(run_id=run.id):
            # Same committed-state probe as _abort_if_superseded: never trust the
            # caller's stale run identity. These hooks run on the caller/DB thread,
            # after translation fan-out, never inside a provider worker.
            with Session(_db.engine) as probe:
                return (
                    probe.exec(select(WorkflowRun.status).where(WorkflowRun.id == run_id)).first()
                    == RunStatus.CANCELLED
                )

        def _score_seg(
            j: int,
            r: dict,
            *,
            qe_on=qe_on,
            qe_spec=qe_spec,
            _qe_spend=_qe_spend,
            qe_step_stats=qe_step_stats,
            qe_skip_reasons=qe_skip_reasons,
            qe_visit_indices=qe_visit_indices,
            qe_threshold=qe_threshold,
            _jev_judge=_jev_judge,
            redact=redact,
        ) -> bool | None:
            """Score segment j's produced output and record the result on qe_seg /
            the stats. Returns True (passed), False (routed to review), or None (gate
            off OR judge skipped/undecided -> segment proceeds, no gate decision).
            Adaptive seeding also checks undecided; legacy skipped behavior is unchanged."""
            if not qe_on or _qe_cancelled():
                return None
            # Batch 14 T2 (D2-QE-ACCOUNTING, §6.5): the QE judge INHERITS the generation
            # leg's per-segment send verdict — it sees the image ONLY when the generation
            # leg actually sent it (r["images_sent"]). Because _qe_spec_for_step can run a
            # DIFFERENT (vision) engine than generation, an ungated QE send would egress
            # pixels while the run recorded "skipped"; inheritance keeps the single skip
            # note truthful and means no image reaches an engine the generation step did
            # not already use. An opted-out step's generation sent nothing, so its judge
            # inherits None with zero toggle-specific logic (§25.3).
            _qe_images = _images_for(j) if r.get("images_sent") else None
            # Prompt Tap (change 4): correlate the QE judge call with this workflow/run/
            # step/doc/segment so the tap viewer no longer hides it (meta was None).
            if _jev_judge["error"] is not None:
                from app.services import qe_jev

                card = qe_jev.unavailable_card(_jev_judge["error"])
            elif _jev_judge["target"] is not None:
                from app.services import qe_jev

                card = pii_boundary.call_with_redaction(
                    redact,
                    qe_jev.score_segment,
                    r["fb_source"],
                    r["fb_output"],
                    src,
                    locale,
                    tap_meta=_seg_tap_meta(j),
                    images=_qe_images,
                    judge=_jev_judge["target"],
                    glossary_terms=seg_terms[j],
                )
            else:
                card = pii_boundary.call_with_redaction(
                    redact,
                    qe.score_segment,
                    qe_spec,
                    r["fb_source"],
                    r["fb_output"],
                    src,
                    locale,
                    tap_meta=_seg_tap_meta(j),
                    images=_qe_images,
                )
            _qe_spend["cost"] += card.get("cost_usd") or 0.0
            _qe_spend["tin"] += card.get("tokens_in") or 0
            _qe_spend["tout"] += card.get("tokens_out") or 0
            # This step now owns segment j's QE verdict, so an earlier step's auto-fix
            # record no longer describes it (a no-op on a dict that stays empty unless
            # some step opted into auto-fix).
            qe_autofix_seg.pop(j, None)
            qe_visit_indices.add(j)
            decision = _gate_decision(card, qe_threshold)
            if decision == "skipped":
                qe_skip_reasons.add(qe.skip_message(card))
                qe_step_stats["skipped"] += 1
                qe_run_stats["skipped"] += 1
                # A skipped judge still records a (skipped) flag so the editor shows
                # "QE skipped" rather than silently nothing; it never routes to review.
                qe_seg[j] = {**card, "passed": None, "threshold": qe_threshold}
                return None
            if decision == "undecided":
                qe_step_stats["skipped"] += 1
                qe_run_stats["skipped"] += 1
                qe_seg[j] = {**card, "passed": None, "threshold": qe_threshold, "undecided": True}
                return None
            passed = decision == "passed"
            qe_step_stats["scored"] += 1
            qe_run_stats["scored"] += 1
            key = "passed" if passed else "failed"
            qe_step_stats[key] += 1
            qe_run_stats[key] += 1
            qe_seg[j] = {**card, "passed": passed, "threshold": qe_threshold}
            return passed

        # LQA safety floor (C) — FIXER POST-CHECK WITH REVERT. The pre/post pair for a
        # segment exists exactly ONCE in this run: right here, at the moment r["output"]
        # arrives and working[j] still holds the pre-step text. `commit_mt_output` only
        # ever sees the FINAL text, so a revert is structurally impossible there — which
        # is why this guard cannot live at write-back. Contract copied from the tag-repair
        # seam ("this seam never trusts an injected repairer's word"), generalized from
        # inline tags to terminology: an edit that INTRODUCES a glossary/DNT violation
        # absent from the text it was editing is DISCARDED, not committed. A pre-existing
        # violation the edit leaves alone is not this guard's business (the write-back
        # gate flags it), and an edit that FIXES one is kept — the predicate is a DELTA.
        # Applies to every segmented llm_agent step (§4.C: a "Finalizer" or "In-country
        # reviewer" can break a term exactly as easily, and the check is free), but only
        # for a segment whose "before" is already a translation (see `produced`).
        fix_guard_on = lqa_safety.fix_guard_enabled()
        step_label = step.name or step.step_type
        # Terminology T3 (SPEC §9-T3, residue [8]/[12]): THIS step visit's render config,
        # resolved once (per-step, LAW-TERM-14). Handed to every _run_step_on_segment call
        # below; a producing step whose toggle is ON records its shown set into
        # shown_terms_by_segment[j] after the produce decision (residue [13]).
        _seg_rc = _render_cfg_for(step)

        def _seg_concepts_for(j):
            """This segment's detached concept matches for the renderer (None when the run
            has no toggle-ON step, so _run_step_on_segment falls back to glossary_line)."""
            return seg_concepts[j] if seg_concepts is not None else None

        def _fix_guard_rejects(
            j: int, new_text: str, *, fix_guard_on=fix_guard_on, step_label=step_label
        ) -> bool:
            """True when segment j's new text must be REJECTED (the pre-fix text kept).
            Records the qa_flag for the write-back as a side effect. Byte-identical
            no-op when the guard is off, the segment has no established translation
            yet, or the run has no glossary terms for it."""
            if not fix_guard_on or j not in produced:
                return False
            # Terminology T3 (LAW-TERM-15 (ii), residue [11]/[13]): enforce against the
            # PRODUCING step's shown set when it had the toggle ON (recorded AFTER the
            # produce decision, so this holds the prior producer's set), else the full match
            # `seg_terms[j]` exactly as today — so no fix step manufactures a revert for a
            # capped-out/human-only term the producing step never showed the model.
            _enforce_terms = shown_terms_by_segment.get(j, seg_terms[j])
            new_dnt, new_gloss = lqa_safety.introduced_violations(
                seg_sources[j], working[j], new_text, _enforce_terms, target_locale=locale
            )
            if not new_dnt and not new_gloss:
                return False
            fix_guard_seg.setdefault(j, []).append(
                lqa_safety.revert_flag(step_label, new_dnt, new_gloss)
            )
            safety_run_stats["reverted"] += 1
            return True

        # QE auto-fix (Train B, opt-in per step). A segment that FAILS a Jev QE gate is
        # handed to an LLM post-editor (qe_jev_loop.refine) and re-scored by the SAME
        # judge target, for at most qe_autofix_max_rounds rounds. The fixer spec is
        # resolved once per step visit (lazily, on the first failure); the spend-cap
        # latch is per step visit too: once a loop stops on the cap, no later segment of
        # this visit starts one. Runs serially here on the DB thread (v1), after the
        # gate's own call, in both branches. Off (the default) -> never entered, nothing
        # recorded, byte-identical.
        _af = {
            "on": qe_on and qe_autofix.enabled_for(step),
            "cap_tripped": False,
            "resolved": False,
            "spec": None,
            "warning": None,
            "priced": True,
        }

        def _autofix_seg(
            j: int,
            r: dict,
            *,
            step=step,
            _af=_af,
            qe_spec=qe_spec,
            qe_threshold=qe_threshold,
            _jev_judge=_jev_judge,
            _qe_spend=_qe_spend,
            qe_step_stats=qe_step_stats,
            redact=redact,
        ) -> bool:
            """Run the auto-fix loop for segment j right after _score_seg returned False
            (a gate FAIL), before its text is final. Returns True when a fixed version
            passed the gate (working[j] replaced, qe_seg[j] = that version's card, the
            step's failed tally moved to passed). Otherwise False, and the segment goes
            to review as it always did, keeping the best attempt only when its Jev score
            is STRICTLY higher than the original's. Either way the rounds are recorded in
            qe_autofix_seg[j] for the reviewer."""
            if not _af["on"]:
                return False
            card = qe_seg[j]
            judge = _jev_judge["target"]
            max_rounds = qe_autofix.clamp_rounds(getattr(step, "qe_autofix_max_rounds", 3))
            cross = bool(getattr(step, "qe_autofix_cross_check", False))
            judge_ref = judge.provider_ref if judge is not None else (qe_spec.provider or "")
            refused = j in refused_seg or (
                config.REFUSAL_GUARD_ENABLED and refusal.is_refusal(working[j])
            )
            reason = qe_autofix.skip_reason(
                card,
                judge_is_jev=judge is not None,
                redact=redact,
                cap_tripped=_af["cap_tripped"],
                refused=refused,
            )
            if reason is None and not _af["resolved"]:
                # No silent engine switch (owner decision): a non-LLM fixer is no fixer.
                _af["spec"], _af["warning"] = qe_autofix.fixer_spec(step)
                _af["resolved"] = True
                if _af["warning"]:
                    log.warning("QE auto-fix: %s", _af["warning"])
                if _af["spec"] is not None:
                    _af["priced"] = qe_autofix.loop_is_priced(_af["spec"], judge)
            if reason is None and _af["spec"] is None:
                reason = qe_autofix.SKIP_NO_FIXER
            # The REAL cap, not only this visit's latch (review B4): a loop that can spend
            # money never starts once the install cap is tripped. A fully local loop
            # ($0 jev_local judge + exempt local fixer) is not held by the cap, exactly
            # like every other $0 call; qe_autofix.loop_is_priced states the rule.
            if reason is None and _af["priced"] and qe_autofix.cap_blocked():
                _af["cap_tripped"] = True
                reason = qe_autofix.SKIP_SPEND_CAP
            if reason is not None:
                qe_autofix_seg[j] = qe_autofix.record(
                    outcome="skipped",
                    judge_ref=judge_ref,
                    original_score=card.get("score"),
                    final_score=card.get("score"),
                    max_rounds=max_rounds,
                    cross_check=cross,
                    warning=_af["warning"] if reason == qe_autofix.SKIP_NO_FIXER else None,
                    skipped_reason=reason,
                    unsure=card.get("unsure"),
                )
                return False
            spec = _af["spec"]
            before = working[j]
            _db.commit_if_staged(session)  # release the writer lock before provider calls
            result = qe_autofix.run_loop(
                seg_sources[j],
                before,
                src,
                locale,
                spec=spec,
                judge=judge,
                baseline_card=card,
                threshold=qe_threshold,
                max_rounds=max_rounds,
                cross_check=cross,
                accept=qe_autofix.make_accept(
                    seg_sources[j],
                    before,
                    shown_terms_by_segment.get(j, seg_terms[j]),
                    locale,
                    unit_guard=(
                        step.step_type == StepType.LLM_AGENT and _over_translation_guard_on(step)
                    ),
                ),
                tap_meta={**_seg_tap_meta(j), "purpose": "qe_autofix"},
                priced=_af["priced"],
                cancelled=_qe_cancelled,
                glossary_terms=seg_terms[j],
            )
            cost, tin, tout = qe_autofix.loop_spend(result, card)
            _qe_spend["cost"] += cost
            _qe_spend["tin"] += tin
            _qe_spend["tout"] += tout
            if result.get("stop_reason") == "spend_cap":
                _af["cap_tripped"] = True
            orig_score = card.get("score")
            # What is kept (a passing version; else the best CONFIDENT strictly-higher one;
            # else the original) is qe_autofix.pick_winner, the one rule the judge Inspect
            # page renders as qe_autofix.KEEP_RULE (Train C gate M1).
            winner, fixed = qe_autofix.pick_winner(result, orig_score, qe_threshold)
            if result.get("stop_reason") == "cancelled":
                winner, fixed = None, False
            if winner is not None:
                text = winner["translation"]
                working[j] = text
                r["output"] = text
                r["fb_output"] = text
                if fixed:
                    qe_seg[j] = {**winner["jev"], "passed": True, "threshold": qe_threshold}
                    for stats in (qe_step_stats, qe_run_stats):
                        stats["failed"] -= 1
                        stats["passed"] += 1
                else:
                    # Still below the bar (or at it but unsure): review, with the better
                    # version in place and its own card on the QE badge.
                    qe_seg[j] = {**winner["jev"], "passed": False, "threshold": qe_threshold}
                    if _gate_decision(winner["jev"], qe_threshold) == "undecided":
                        qe_seg[j]["undecided"] = True
            qe_autofix_seg[j] = qe_autofix.record(
                outcome="fixed" if fixed else "needs_review",
                judge_ref=judge_ref,
                fixer_ref=spec.provider,
                fixer_model=spec.model,
                result=result,
                cost_usd=cost,
                kept=("fixed" if fixed else "best") if winner is not None else "original",
                original_score=orig_score,
                final_score=winner["jev"]["score"] if winner is not None else orig_score,
                max_rounds=max_rounds,
                cross_check=cross,
                warning=_af["warning"],
            )
            return fixed

        # Segments THIS step-visit reverted (excluded from the QE judge and, in the
        # serial branch, from the feedback buffer — a rejected edit must not become
        # few-shot context, and scoring text we discarded would badge the segment with a
        # verdict about a string that was never committed).
        reverted_idx: set = set()
        # Segments THIS step-visit could not produce because the provider call failed
        # for them SPECIFICALLY (GitHub #45). Excluded from the QE judge and the feedback
        # buffer — there is no output to judge or to seed with. The post-loop refusal
        # scan still sees working[j], which is deliberate and inert: that slot holds the
        # PRE-STEP text an earlier step already cleared, so re-scanning it is idempotent.
        degraded_idx: set = set()

        def _seg_call_degraded(
            j: int, r: dict, *, step_label=step_label, degraded_idx=degraded_idx
        ) -> bool:
            """True when segment j's provider call FAILED and the run can carry on
            without it — its pre-step text is kept and it is routed to review.

            THE NARROWING (GitHub #45). `err = r["error"] or err` used to fold ANY
            per-segment error into the STEP's error, which made the step FAILED, the run
            FAILED, and (by the all-or-nothing write-back) every locale value for the
            document discarded. Prod run #74 lost a whole 100-key fr-FR namespace to ONE
            over-long fix prompt. This narrows what counts as a RUN failure; it does not
            change commit granularity — write-back is still all-or-nothing, still after
            the whole step loop, still success-only (F1+F2).

            TWO conditions, both required:
              * the call FAILED (`error` set). Whatever text came back with it is
                DISCARDED — see below;
              * the segment ALREADY HOLDS A TRANSLATION (`produced`), so there is
                something honest to keep. A segment that fails before anything ever
                translated it has only its SOURCE in `working[j]`, and committing an
                untranslated source as a delivered target is precisely the "no partial/
                blank targets on failure" guarantee this path exists to uphold — so that
                case is NOT degraded and still fails the run.

            THE PARTIAL-OUTPUT CASE (adversarial review P10). The first cut also required
            EMPTY output, on the theory that "text alongside a soft error" should commit
            as it always had. It never did commit: a result carrying both was folded into
            the step's `err` on the very next line, which failed the step and the run and
            discarded the whole locale — the exact prod-#74 outcome this seam exists to
            prevent, reachable through a CHUNKED segment whose first chunk succeeded and
            second chunk errored. And its `output` is the join of the chunks that DID
            return, i.e. a truncated translation that no caller can distinguish from a
            complete one. So there is nothing to commit and nothing to fail wholesale
            over: discard the partial text, keep the segment's pre-step translation, and
            route it to a human — which is what every other degradation here does.

            A step in which EVERY pending segment degrades is escalated back to a run
            failure by the caller, under the conditions documented there."""
            if not r.get("error"):
                return False
            if j not in produced:
                return False
            seg_step_error[j] = (step_label, r.get("error") or "")
            degraded_idx.add(j)
            return True

        provider_used, model_used, attempts_total, err = None, None, 0, None
        # run-190 Item C: the failure SHAPE (finish reason + provider error class) of the
        # last errored segment, stamped on this step's StepRun so the run view / API can
        # show WHY a segmented step failed (metadata only). None when no segment errored.
        seg_finish_reason, seg_error_class = None, None
        t0 = time.time()
        # provider calls for the pending segments run concurrently; results are
        # collected here on the DB thread (no ORM inside the pool).
        results: dict[int, dict] = {}
        # GitHub #101: IN-STEP PROGRESS. This step is the 10-25 minutes during which every
        # surface used to report nothing at all -- no StepRun row exists until it ENDS. The
        # ticker counts "N of M segments" as each unit lands (in MEMORY, from the worker
        # thread that finished it -- a pool thread must never open a Session, #100) and THIS
        # thread persists it on a coarse throttle, so /api/runs/{id}, `workflow show-run` and
        # the SSE stream can tell a working run from a wedged one without the default-OFF
        # Ledger. Observer only: best-effort everywhere, it cannot perturb this step.
        _prog = _ProgressTicker(run.id, len(pending_idx), "segments", step=step)
        _prog.start()
        if pending_idx and not adaptive_fb:
            # Cache warm-up (Feature 0): run the FIRST pending segment synchronously
            # so any shared cacheable prefix is written before the pool fans out and
            # the rest can cache-READ it (see translate.pretranslate_stream). A
            # single-segment step degenerates to one synchronous call.
            #
            # A dead engine:<id> ref (DeadEngineError from get_provider) is explicit
            # misconfiguration that would otherwise propagate out raw — convert it to
            # a step error string so it flows through the SAME step_failed / run
            # FAILED / {type:"error"} machinery every other failure uses, naming the
            # dead ref, rather than silently substituting mock output.
            try:
                first, rest = pending_idx[0], pending_idx[1:]
                # RESOLVE -> RELEASE -> DISPATCH (SPEC-parallel-routing Phase 0.1).
                # `_mem_block`/`_seg_tap_meta_mem` are the only ORM work left in this
                # block, and `_mem_stamp` STAGES the usage counters on THIS shared
                # session without committing (llmemory._mark_used, deliberately: a
                # second Session deadlocks). Resolve them into locals FIRST, then hand
                # the writer lock back, THEN make the call — otherwise the next ORM
                # read autoflushes those stamps, takes SQLite's single writer lock
                # UNCOMMITTED, and holds it across the provider call whose
                # `spend.record_usage` opens its own Session: QA-1's exact circular
                # wait (see tests/verify_orchestrator_spend_lock.py). Conditional by
                # construction — `db.commit_if_staged` is a no-op when recall is off
                # (nothing staged, nothing to release), so a memory-less run keeps its
                # existing transaction shape exactly.
                _first_mem = _mem_block(first)
                _first_tap = _seg_tap_meta_mem(first)
                # Batch 14 T2: resolve this segment's images (a pure lookup into the
                # DB-thread-built _img_map) here alongside the memory/tap resolution.
                _first_img = _images_for(first)
                _db.commit_if_staged(session)
                results[first] = _run_step_on_segment(
                    spec,
                    template,
                    base_ctx,
                    step,
                    seg_sources[first],
                    working[first],
                    locale,
                    seg_terms[first],
                    redact,
                    brief_block=brief_blk,
                    tm_examples_block=_tm_ex_block(first),
                    memory_block=_first_mem,
                    tap_meta=_first_tap,
                    findings_suffix=findings_suffix,
                    excluded_findings=findings_excluded[first],
                    relevant_findings=findings_relevant[first],
                    images=_first_img,
                    seg_concepts=_seg_concepts_for(first),  # Terminology T3 (SPEC §9-T3)
                    render_cfg=_seg_rc,
                    segment_id=segments[first].id,
                    system_head=_sys_head_body,
                )
                _prog.tick()  # #101: the cache warm-up segment is a finished unit too
                _prog.flush()  # queues a background persist; never blocks this thread
                if rest:
                    # Same discipline, and here it is load-bearing for the WHOLE
                    # fan-out: building the futures dict inline would interleave
                    # `pool.submit` with per-segment recall, so the lock taken by
                    # segment k's autoflush would be held while workers 0..k-1 are
                    # already calling providers. Materialize every segment's
                    # session-touching argument on this (DB) thread, release, dispatch.
                    _fan_args = [
                        (j, _mem_block(j), _seg_tap_meta_mem(j), _images_for(j)) for j in rest
                    ]
                    _db.commit_if_staged(session)
                    # AI opt-out (DPAI-08): submit through ai_optout so each worker
                    # thread carries the run's AI scope (a pool does not copy context).
                    from app.services import ai_optout as _ai_optout

                    with ThreadPoolExecutor(max_workers=_mt_workers()) as pool:
                        futs = {
                            _ai_optout.submit(
                                pool,
                                _run_step_on_segment,
                                spec,
                                template,
                                base_ctx,
                                step,
                                seg_sources[j],
                                working[j],
                                locale,
                                seg_terms[j],
                                redact,
                                brief_block=brief_blk,
                                tm_examples_block=_tm_ex_block(j),
                                # Selected on THIS (DB) thread BEFORE dispatch
                                # began — recall touches the ORM session, and
                                # _run_step_on_segment is session-free by
                                # contract.
                                memory_block=_mem_j,
                                tap_meta=_tap_j,
                                findings_suffix=findings_suffix,
                                excluded_findings=findings_excluded[j],
                                relevant_findings=findings_relevant[j],
                                images=_img_j,
                                seg_concepts=_seg_concepts_for(j),  # Terminology T3
                                render_cfg=_seg_rc,
                                segment_id=segments[j].id,
                                system_head=_sys_head_body,
                            ): j
                            for (j, _mem_j, _tap_j, _img_j) in _fan_args
                        }
                        # #101: count each call as it LANDS, not as the DB thread collects
                        # results below -- collection is in submission order, so a slow
                        # first segment would otherwise hide every later completion. The
                        # callback runs ON THE WORKER THREAD and therefore only touches
                        # memory; the persist happens below, on this thread.
                        for _fut in futs:
                            _fut.add_done_callback(_prog.on_done)
                        for fut in futs:
                            j = futs[fut]
                            results[j] = fut.result()
                            # The counter has been moving in memory throughout the
                            # fan-out; queue it onto the row on the coarse throttle so a
                            # DIFFERENT process (the server answering /api for a headless
                            # CLI run) sees it move too. Queue, not write: the persist runs
                            # on the background writer, so an exhausted pool costs a skipped
                            # counter refresh and never a stalled translation (H1).
                            _prog.flush()
            except DeadEngineError as exc:
                # Prefixed with step/pair context (finding 4) — same rationale as the
                # FULL_TEXT site above.
                err = f"step '{step.name or step.step_type}' ({src}->{locale}): {exc}"
            else:
                for j in pending_idx:
                    r = results[j]
                    # GitHub #45: a call that failed for THIS segment alone degrades
                    # gracefully (keep its pre-step text, flag it) instead of failing
                    # the run. Checked FIRST — a failed call has no output for any of
                    # the guards below to adjudicate.
                    if _seg_call_degraded(j, r):
                        attempts_total += r["attempts"]
                        # M-2 (gate round 5): a degraded segment IS a failed provider call
                        # (a step-2+ burn on an already-translated segment `continue`s here,
                        # before the latch below), so its failure shape must feed the
                        # worst-case-wins latch too -- otherwise a mixed step could stamp a
                        # clean "stop" beside a real burn, contradicting the `docs/CLI.md`
                        # promise. A clean segment can never reach this branch, so the latch
                        # only ever WORSENS the row (a failure winning), never erases one.
                        seg_finish_reason, seg_error_class = _latch_seg_shape(
                            r, seg_finish_reason, seg_error_class
                        )
                        continue
                    # (C) post-check BEFORE the assignment: on a reject, working[j] and
                    # tm_origin[j] are both left UNTOUCHED — the pre-fix text stands, and
                    # because no text changed the segment keeps whatever provenance it
                    # had (a reverted edit of a TM-filled segment stays TM).
                    if _fix_guard_rejects(j, r["output"]):
                        reverted_idx.add(j)
                    else:
                        working[j] = r["output"]
                        tm_origin[j] = False  # a provider call overrode any TM seed
                        produced.add(j)
                        seg_mt_chunks[j] = int(r.get("mt_chunks") or 1)
                        # Terminology T3 (LAW-TERM-15 (ii), residue [12]/[13] + M5): after the
                        # produce decision, a toggle-ON producer records segment j's shown set
                        # (any producing step type, not just Translate) and a toggle-OFF
                        # producer CLEARS j's entry so `.get(j, seg_terms[j])` reverts to the
                        # FULL match — LAW-TERM-15 as written ("OFF producer -> full match").
                        # A rejected edit never reaches here (the fix guard ran first over the
                        # PRIOR producer's set), so a reject never resets the map.
                        if _seg_rc["on"] and r.get("shown_terms") is not None:
                            shown_terms_by_segment[j] = r["shown_terms"]
                        else:
                            shown_terms_by_segment.pop(j, None)
                        # #43 (C1) / #45: this step produced THIS segment's current
                        # text, so its provider decides the language guard, and any
                        # earlier step-failure flag now describes text that is gone.
                        seg_lang_guard[j] = _lang_guard_for(r)
                        seg_step_error.pop(j, None)
                    attempts_total += r["attempts"]
                    provider_used, model_used = r["provider_used"], r["model_used"]
                    err = r["error"] or err
                    # run-190 Item C / M-3 (gate round 4): accumulate the step's finish shape
                    # with a DETERMINISTIC, order-independent, self-consistent rule (worst-case
                    # wins), NOT the round-3 "last truthy wins" `or` where a LATER clean "stop"
                    # clobbered an EARLIER segment's "length" (an order-dependent row reading
                    # `stop` beside `ThinkingBurn`). `_latch_seg_shape` latches the FIRST failing
                    # segment's (finish_reason, error_class) pair TOGETHER and never overwrites
                    # it; a clean "stop" is recorded only while no failure has latched.
                    seg_finish_reason, seg_error_class = _latch_seg_shape(
                        r, seg_finish_reason, seg_error_class
                    )
                    # run-190 Item D: a page-dump the OVER-TRANSLATION guard rejected (after
                    # its one strengthened-instruction retry) is WITHHELD — route it to the
                    # same NEEDS_REVIEW/withheld disposition as a refusal so the dump is
                    # never committed (`refused_seg` drives the write-back + later-step
                    # exclusion + blob withhold; `over_translation_seg` drives the target
                    # withhold independently of REFUSAL_GUARD_ENABLED — H2). Quarantines the
                    # segment, run carries on.
                    if r.get("over_translation"):
                        refused_seg.add(j)
                        over_translation_seg.add(j)
                        over_translation_meta[j] = {
                            "shape": r.get("over_translation_reason") or "",
                            "src_len": r.get("over_translation_src_len"),
                            "tgt_len": r.get("over_translation_tgt_len"),
                            # #683: the engine-output-limit withhold's own sentence.
                            "reason": r.get("too_long_reason") or "",
                        }
                # QE gate: score AFTER the fan-out (no adaptive buffer here, so order
                # doesn't matter). Scored in order_index order for deterministic
                # stats/spend. A judge failure soft-skips (segment still proceeds).
                if qe_on and not err:
                    for j in pending_idx:
                        if _qe_cancelled():
                            break
                        if j in reverted_idx or j in degraded_idx:
                            continue
                        if _score_seg(j, results[j]) is False:
                            # QE auto-fix (opt-in): a no-op returning False when off.
                            _autofix_seg(j, results[j])
        elif pending_idx:
            # F2.1 ON: ORDERED, serial pass so each segment's feedback block is
            # DETERMINISTIC (segment order, never thread-completion order). Seed the
            # buffer with every NON-pending segment (TM-filled at step 0, or the
            # prior step's output on step>0) IN order_index order — those are the
            # confirmed, highest-quality signals — then translate pending segments
            # strictly in order, appending each confirmed output before building the
            # next prompt. Serial is the correct cost of order-dependent context.
            fb = adaptive.FeedbackBuffer()
            pend_set = set(pending_idx)
            try:
                for j in range(len(segments)):
                    if _qe_cancelled():
                        break
                    if j in refused_seg:
                        # C1: a segment already frozen as a hedge must NOT seed the
                        # feedback buffer — injecting its hedge as a few-shot example
                        # would poison later segments' prompts. It is neither pending
                        # (excluded above) nor a confirmed signal; skip it entirely.
                        continue
                    if j not in pend_set:
                        # TM/prior-step confirmed: never goes through this step's own
                        # redaction, but the buffer's render() DOES get injected into
                        # a LATER segment's prompt (which the engine sees) — so when
                        # this step redacts, seed with the REDACTED source AND the
                        # REDACTED target, same as the engine-bound path below (an
                        # approved/TM target can itself carry PII — the target of a
                        # PII-bearing source usually repeats it — so BOTH halves
                        # must be scrubbed: never raw PII in the buffer).
                        if redact:
                            fb.add(
                                pii.redact(seg_sources[j], lang=src)[0],
                                pii.redact(working[j], lang=locale)[0],
                            )  # TM/prior-step confirmed
                        else:
                            fb.add(seg_sources[j], working[j])  # TM/prior-step confirmed
                        continue
                    feedback_blk = fb.render(window=fb_window)
                    # RESOLVE -> RELEASE -> CALL (Phase 0.1). Serial, but the hazard is
                    # identical: from the second segment on, this segment's recall
                    # autoflushes the PREVIOUS segment's staged usage stamps, taking the
                    # writer lock uncommitted — and `spend.record_usage` inside the call
                    # below opens its own Session on this very thread. No-op when recall
                    # is off (nothing staged).
                    _mem_j = _mem_block(j)
                    _tap_j = _seg_tap_meta_mem(j)
                    _img_j = _images_for(j)  # Batch 14 T2 (pure _img_map lookup)
                    _db.commit_if_staged(session)
                    r = _run_step_on_segment(
                        spec,
                        template,
                        base_ctx,
                        step,
                        seg_sources[j],
                        working[j],
                        locale,
                        seg_terms[j],
                        redact,
                        brief_block=brief_blk,
                        feedback_block=feedback_blk,
                        tm_examples_block=_tm_ex_block(j),
                        memory_block=_mem_j,
                        tap_meta=_tap_j,
                        findings_suffix=findings_suffix,
                        excluded_findings=findings_excluded[j],
                        relevant_findings=findings_relevant[j],
                        images=_img_j,
                        seg_concepts=_seg_concepts_for(j),  # Terminology T3 (SPEC §9-T3)
                        render_cfg=_seg_rc,
                        segment_id=segments[j].id,
                        system_head=_sys_head_body,
                    )
                    results[j] = r
                    _prog.tick()  # #101: serial adaptive-feedback branch, same signal
                    _prog.flush()
                    # GitHub #45 (see the concurrent branch): a call that failed for
                    # THIS segment alone keeps its pre-step text and continues. It also
                    # never seeds the feedback buffer — there is no output to seed with.
                    if _seg_call_degraded(j, r):
                        attempts_total += r["attempts"]
                        # M-2 (gate round 5): latch the degraded segment's failure shape before
                        # the `continue` (serial branch; mirrors the concurrent branch) so a
                        # step-2+ burn on an already-translated segment cannot leave a clean
                        # "stop" on the StepRun beside a real burn.
                        seg_finish_reason, seg_error_class = _latch_seg_shape(
                            r, seg_finish_reason, seg_error_class
                        )
                        continue
                    # (C) post-check BEFORE the assignment (see the concurrent branch).
                    seg_reverted = _fix_guard_rejects(j, r["output"])
                    if seg_reverted:
                        reverted_idx.add(j)
                    else:
                        working[j] = r["output"]
                        tm_origin[j] = False
                        produced.add(j)
                        seg_mt_chunks[j] = int(r.get("mt_chunks") or 1)
                        # Terminology T3 (LAW-TERM-15 (ii), residue [12]/[13] + M5): record the
                        # shown set AFTER the produce decision for a toggle-ON producer, and
                        # CLEAR j's entry for a toggle-OFF producer so enforcement reverts to
                        # the full match (serial branch — mirrors the concurrent branch above).
                        if _seg_rc["on"] and r.get("shown_terms") is not None:
                            shown_terms_by_segment[j] = r["shown_terms"]
                        else:
                            shown_terms_by_segment.pop(j, None)
                        seg_lang_guard[j] = _lang_guard_for(r)
                        seg_step_error.pop(j, None)
                    attempts_total += r["attempts"]
                    provider_used, model_used = r["provider_used"], r["model_used"]
                    err = r["error"] or err
                    # run-190 Item C / M-3 (gate round 4): accumulate the finish shape with the
                    # deterministic worst-case-wins latch (serial branch; mirrors the concurrent
                    # branch above) so a later clean "stop" cannot clobber an earlier segment's
                    # failure shape and the row is never self-contradictory or order-dependent.
                    seg_finish_reason, seg_error_class = _latch_seg_shape(
                        r, seg_finish_reason, seg_error_class
                    )
                    # run-190 Item D: a page-dump the OVER-TRANSLATION guard rejected (after
                    # its one strengthened-instruction retry) is WITHHELD — route it to the
                    # same NEEDS_REVIEW/withheld disposition as a refusal so the dump is
                    # never committed (`refused_seg` drives the write-back + later-step
                    # exclusion + blob withhold; `over_translation_seg` drives the target
                    # withhold independently of REFUSAL_GUARD_ENABLED — H2). Quarantines the
                    # segment, run carries on.
                    if r.get("over_translation"):
                        refused_seg.add(j)
                        over_translation_seg.add(j)
                        over_translation_meta[j] = {
                            "shape": r.get("over_translation_reason") or "",
                            "src_len": r.get("over_translation_src_len"),
                            "tgt_len": r.get("over_translation_tgt_len"),
                            # #683: the engine-output-limit withhold's own sentence.
                            "reason": r.get("too_long_reason") or "",
                        }
                    # QE gate: score THIS segment before deciding whether it seeds the
                    # feedback buffer. Composition rule (documented): a QE-FAILED
                    # segment must NOT poison the buffer — only QE-passing (or gate-off
                    # / judge-skipped) segments become few-shot context for later
                    # segments. `passed` is True/False/None (None = off or skipped).
                    # Skip the judge call entirely once the step has errored (mirrors
                    # the concurrent path's `if qe_on and not err:` guard) — a run that
                    # already failed gets discarded, so scoring its later segments only
                    # burns judge spend for nothing.
                    # C1 (F1 fix): a hedged segment must be caught HERE, inside the
                    # serial pass, BEFORE it can seed the FeedbackBuffer — the post-
                    # loop scan below runs too late (this segment's hedge would already
                    # have been rendered as a "confirmed earlier translation" few-shot
                    # example into every LATER pending segment's prompt). Freeze it the
                    # instant is_refusal fires: add to refused_seg (so the post-loop
                    # write-back routes it to NEEDS_REVIEW and later steps exclude it)
                    # and DO NOT seed the buffer with it. Gated by the kill switch.
                    # (C): a REVERTED edit was never accepted, so neither the refusal
                    # guard nor the QE judge may act on it — freezing the segment on a
                    # discarded hedge would withhold target_text and throw away the GOOD
                    # pre-fix text we just chose to keep, and a QE badge would describe a
                    # string that was never committed. It also never seeds the feedback
                    # buffer (a rejected edit must not become few-shot context).
                    # run-190 Item D: a withheld over-translation (page dump) is treated
                    # like a hedge for buffer purposes too — it must NEVER seed the feedback
                    # buffer as a "confirmed" few-shot example for later segments.
                    seg_refused = (
                        j not in reverted_idx
                        and config.REFUSAL_GUARD_ENABLED
                        and refusal.is_refusal(r["output"])
                    ) or bool(r.get("over_translation"))
                    # #343: an output-guard hit is frozen INLINE too, so an echoed instruction
                    # or a number-mismatched output never seeds the feedback buffer.
                    _og_soft_hit = False
                    if (
                        not seg_refused
                        and config.REFUSAL_GUARD_ENABLED
                        and not err
                        and j not in reverted_idx
                        and j not in degraded_idx
                    ):
                        output_guard_soft_seg.pop(j, None)
                        _og_inline = output_guard.check_output(
                            segments[j].source_text,
                            r["output"],
                            _og_grams,
                            fence_text=_og_fence,
                            number_already_gated=bool(
                                quality.number_violation(
                                    segments[j].source_text, r["output"], locale
                                )
                            ),
                            locale=locale,
                        )
                        if _og_inline is not None:
                            _og_inline["source"] = "workflow"
                            if output_guard.withholds(_og_inline):
                                output_guard_seg[j] = _og_inline
                                seg_refused = True
                            else:
                                # Soft: keep the text, flag for review, but never seed the
                                # feedback buffer with a number-mismatched output.
                                output_guard_soft_seg[j] = _og_inline
                                _og_soft_hit = True
                    if seg_refused:
                        refused_seg.add(j)
                    passed = _score_seg(j, r) if not err and j not in reverted_idx else None
                    if passed is False:
                        # QE auto-fix (opt-in) BEFORE the buffer decision below, so a
                        # fixed segment seeds the buffer with its fixed text (r is
                        # updated in place) and a still-failing one never does.
                        passed = _autofix_seg(j, r)
                    if (
                        passed is not False
                        and not qe_seg.get(j, {}).get("undecided")
                        and not seg_refused
                        and not _og_soft_hit
                        and j not in reverted_idx
                    ):
                        # Redaction-safe seed for the NEXT segment (see
                        # _run_step_on_segment's docstring): redacted source + pre-
                        # restore engine output when this step redacts; else unchanged.
                        # A hedged segment (seg_refused) is NEVER seeded — a hedge must
                        # not become few-shot context for a sibling segment's prompt.
                        fb.add(r["fb_source"], r["fb_output"])  # confirmed -> next segment sees it
            except DeadEngineError as exc:
                err = f"step '{step.name or step.step_type}' ({src}->{locale}): {exc}"

        # #101: STEP BOUNDARY -- both branches are done, so stamp where the step actually
        # reached (on the run's OWN ORM object, riding this step's commit) and drop the live
        # counter. NOT advanced to total, and futures that RAISED were never counted, so an
        # aborted step keeps its honest 5-of-6 instead of a cosmetic 6/6 beside `failed`.
        _prog.finish(session, run)

        # GitHub #45 — THE ESCALATION. Per-segment degradation is for the case where one
        # request was bad (too long a prompt, one pathological string). When every
        # pending segment in a step errors, that CAN mean the engine is down for this
        # step — a structural failure, where failing the run wholesale (writing nothing)
        # is the right answer. Judged on `pending_idx` because a TM-filled / frozen
        # segment made no call and cannot vote.
        #
        # WHY "ALL PENDING FAILED" IS NOT ENOUGH BY ITSELF (adversarial review P3).
        # `pending_idx` is a POST-FILTER set: TM-protected segments, refused segments and
        # run-condition skips have already been removed from it. In the very workflow
        # shape #45 was written for — a correction loop whose fix step only runs on the
        # segments the LQA failed — that set is routinely ONE segment long. So "every
        # pending segment failed" degenerated into "the single pending segment failed",
        # and one over-long fix prompt again failed the whole run and discarded every
        # value for the locale, TM-protected 100% matches included. That IS prod #74,
        # re-created inside its own fix.
        #
        # THE SOLE TEST IS THE ERROR CLASS (re-review N3): is this a STRUCTURAL failure —
        # a dead or unreachable engine, a refused/reset connection, a DNS/TLS failure —
        # rather than a per-request one? A structural error is not a property of the
        # request at all, so it says "the engine is down" even on a sample of one, and
        # there is no completed work to keep. Everything else degrades, no matter HOW
        # many segments it hit: see `_STRUCTURAL_ERROR_RE` for why a count of failures is
        # not evidence of the difference, and for the measurement that removed the old
        # size arm.
        if pending_idx and degraded_idx and len(degraded_idx) == len(pending_idx):
            _errs = [seg_step_error.get(j, ("", ""))[1] for j in pending_idx]
            if any(_is_structural_error(e) for e in _errs):
                err = "step '{}' ({}->{}): every segment failed - {}".format(
                    step_label,
                    src,
                    locale,
                    seg_step_error.get(pending_idx[0], ("", ""))[1],
                )
                for j in pending_idx:
                    seg_step_error.pop(j, None)
                degraded_idx.clear()
        latency = int((time.time() - t0) * 1000)

        # Real per-step-visit spend (feat/workflow-spend): fold every segment's
        # thread-local per-segment spend (returned from _run_step_on_segment, already
        # joined into `results`) PLUS the QE-judge spend into this step-visit's totals,
        # here on the DB thread AFTER the fan-out has joined — no lost updates. Covers
        # both branches (concurrent + serial) and partial fan-outs (a DeadEngineError
        # mid-loop still leaves the completed segments in `results`). A step that ran at
        # least one completion stamps a real (0.0-on-mock) cost, so the reports workflow
        # axis reads the run as tracked.
        step_cost = sum((r.get("cost_usd") or 0.0) for r in results.values()) + _qe_spend["cost"]
        step_tin = sum((r.get("tokens_in") or 0) for r in results.values()) + _qe_spend["tin"]
        step_tout = sum((r.get("tokens_out") or 0) for r in results.values()) + _qe_spend["tout"]

        # C1/C4: per-step refusal guard. Scan the segments THIS step just produced
        # (pending_idx) BEFORE their working[] text feeds the next step or is persisted
        # to the StepRun blob / streamed. A hedge caught here is added to refused_seg,
        # which (a) excludes it from every later step's pending set (no laundering by a
        # finalizer) and (b) routes it to NEEDS_REVIEW at write-back without committing
        # target_text. Skipped when the step errored (mirrors the QE gate's `not err`
        # guard — a failed step is discarded, so checking its output is moot). Same
        # kill switch as the fresh-MT sites. Scans `pending_idx` only: a TM-filled /
        # already-frozen segment is not re-checked here. This is the primary catch for
        # the CONCURRENT branch; the adaptive-feedback (serial) branch already froze
        # any hedge INLINE (before it could seed the buffer — F1 fix), and re-adding to
        # the set here is idempotent, so this doubles as a harmless backstop for it.
        if config.REFUSAL_GUARD_ENABLED and not err:
            for j in pending_idx:
                if refusal.is_refusal(working[j]):
                    refused_seg.add(j)
            # #343: the OUTPUT guards (instruction echo, short-source blow-up, number
            # mismatch) over the text THIS step just produced. Same scope and kill switch as
            # the refusal scan above; a segment a refusal/dump already froze, a reverted edit
            # and a degraded (step-failed) slot are skipped (their working[] text was not
            # produced by this step). The prompt n-grams (`_og_grams`) are computed once per
            # step from the literal (non-placeholder) template text and the system message.
            for j in pending_idx:
                if j in refused_seg or j in reverted_idx or j in degraded_idx or j not in results:
                    continue
                # A soft verdict from an earlier step must not outlive the text that earned it:
                # it is cleared and re-derived from THIS step's output below.
                _og_flag = output_guard.check_output(
                    segments[j].source_text,
                    working[j],
                    _og_grams,
                    fence_text=_og_fence,
                    number_already_gated=bool(
                        quality.number_violation(segments[j].source_text, working[j], locale)
                    ),
                    locale=locale,
                )
                output_guard_soft_seg.pop(j, None)
                if _og_flag is not None:
                    _og_flag["source"] = "workflow"
                    if output_guard.withholds(_og_flag):
                        output_guard_seg[j] = _og_flag
                        refused_seg.add(j)
                    else:
                        output_guard_soft_seg[j] = _og_flag

        # C4: a refused segment's slot in the persisted StepRun blob + the `step` event
        # shows the withheld disposition, NOT the raw hedge (run_view.html renders the
        # blob and it persists in history) — mirrors how target_text is withheld.
        # M-1: an over-translation (page-dump) withhold uses its OWN marker (never the
        # refusal one) so the run trace honestly names which guard fired.
        output_blob = SEG_JOINER.join(
            (
                (
                    # #683: the engine-output-limit withhold names itself in the trace.
                    refusal.WITHHELD_OUTPUT_ENGINE_LIMIT
                    if j in over_translation_seg
                    and over_translation_meta.get(j, {}).get("shape") == refusal.ENGINE_LIMIT_SHAPE
                    else refusal.WITHHELD_OUTPUT_OVER_TRANSLATION
                    if j in over_translation_seg
                    else refusal.WITHHELD_OUTPUT_GUARD
                    if j in output_guard_seg
                    else refusal.WITHHELD_OUTPUT
                )
                if j in refused_seg
                else working[j]
            )
            for j in range(len(segments))
        )
        # F2: derive the StepRun status from REAL output, NOT the joined blob (the
        # SEG_JOINER is non-empty, so a blob is truthy even when every segment
        # output is ""). A step FAILED if the provider surfaced an error OR it
        # produced no usable text at all. This is STRICTER than FULL_TEXT's
        # empty-output-only check on purpose: a per-segment error means at least one
        # segment is a blank "", so treating the whole step as failed (and skipping
        # write-back) upholds the "no partial/blank targets on failure" guarantee
        # rather than committing a mix of good + blank targets. TM-filled working
        # text alone (a TM-only step 0, no provider error) is NOT a failure.
        step_failed = bool(err) or not any(w.strip() for w in working)
        if step_failed:
            any_step_failed = True
        # T1-01 defense-in-depth: a step whose non-empty prompt lacks {text}/{prior}
        # sent the model NO source, so EVERY segment it produced reads as a refusal and
        # gets withheld — a generic "model refusal" disposition that hides the real
        # cause. When this step withheld ALL the segments it just produced AND its
        # template could not have inserted the source, put a specific, actionable hint
        # on the run trace (StepRun.error) instead. It does NOT fail the step (the run
        # still completes and routes to review, preserving the refusal-guard contract)
        # — it only renames the disposition so the operator sees the footgun.
        # SPEC-prompt-template-runtime: a dangling/invalid bound-template fallback note
        # (used the DEFAULT instead of the saved template) rides just after a provider
        # error, ahead of the hygiene note.
        step_note = err or tmpl_note
        # Change 3: record the output-hygiene strip honestly (any segment this step
        # produced had a leading meta line removed) when nothing else claims the note.
        if (
            not err
            and not tmpl_note
            and any(results.get(j, {}).get("hygiene_stripped") for j in pending_idx)
        ):
            step_note = HYGIENE_NOTE
        if (
            not step_failed
            and pending_idx
            and step.step_type == StepType.LLM_AGENT
            and (step.prompt_text or "").strip()
            and not prompts.has_source_placeholder(step.prompt_text)
            and all(j in refused_seg for j in pending_idx)
        ):
            step_note = prompts.MISSING_SOURCE_HINT
        # LQA safety floor telemetry: what THIS step-visit's guards did, on the run
        # trace (StepRun.error is the timeline's note column, which run_view.html and
        # `doc show-run` both render). A discarded edit / a skipped TM segment is a real
        # operator-visible event and outranks the cosmetic hygiene note, but never
        # displaces a provider error or a template-fallback note. Byte-identical (note
        # untouched) when neither guard did anything — the common case.
        safety_bits = []
        if reverted_idx:
            safety_bits.append("%d segment edit(s) reverted (terminology)" % len(reverted_idx))  # noqa: UP031
        if tm_protected_idx:
            safety_bits.append("%d TM segment(s) protected" % len(tm_protected_idx))  # noqa: UP031
        # GitHub #45: per-segment degradation is a real operator-visible event and must
        # appear on the run trace — a step that quietly "completed" while N segments
        # kept their pre-step text would be the silent-success lie F2 exists to prevent.
        # Rides the SAME note slot as the other safety telemetry (never displacing a
        # provider error or a template-fallback note), and is absent when nothing
        # degraded -> byte-identical for every existing run.
        if degraded_idx:
            safety_bits.append(
                "%d segment(s) kept pre-step text after a step failure "  # noqa: UP031
                "(routed to review)" % len(degraded_idx)
            )
            # P8: the same fact, structured, for the run-level rollup on `done`. Counted
            # HERE (after the escalation above, which clears `degraded_idx` when it fires)
            # so an escalated step contributes nothing — that run FAILED and wrote no
            # targets, so it has no degraded segments to report.
            safety_run_stats["degraded"] += len(degraded_idx)
        if safety_bits and (not step_note or step_note == HYGIENE_NOTE):
            step_note = "LQA safety floor: " + "; ".join(safety_bits)
        # Batch 14 T2 (SPEC §6.3/§13/LAW-IMG-4): the SINGLE image-context skip note, folded
        # over results.values() AFTER the fan-out joins (V-2 — NOT inside the concurrent
        # branch's per-segment loop, which early-continues a degraded segment before this
        # logic). A leg that WAS handed images (images_present, minted at the leg, M-2) but
        # sent them on no target (images_sent False — a non-vision engine, a ChatML
        # adapter, an NMT, or a failover to text-only) means the visual context was lost to
        # an engine limitation. An opted-out / non-agent step got [] so its images_present
        # is False and it is structurally excluded (§25.4) — no phantom skip. Counted at
        # most ONCE per run (the reason is identical regardless of cause, so no spam) and
        # surfaced on the run trace only when a more important note has not already claimed
        # the slot. LOGGED, not audited (§13: image bytes are never audited/logged).
        if any(r.get("images_present") and not r.get("images_sent") for r in results.values()):
            if img_skip_run_ct == 0:
                img_skip_run_ct = 1
                log.info(
                    "image context skipped: engine not vision-capable (run %s, step '%s')",
                    getattr(run, "id", None),
                    step_label,
                )
            if not step_note or step_note == HYGIENE_NOTE:
                step_note = "image context skipped: engine not vision-capable"
        # Phase 1 durable signal (SPEC §3.2): a QE-gated step that scored >=1 segment
        # produces a run-level QE outcome — pass-RATE as the aggregate score (D2's
        # confirmed aggregation), gate = "passed" iff no segment failed the gate. A
        # Phase-3 branch on this step routes by that gate (below). A non-QE step (or one
        # that scored nothing) has NO outcome -> score/gate stay NULL. outcome_index is
        # this step's 0-based visit number (0 on a linear run; increments per loop
        # revisit). Additive, so an OFF step's persisted row is byte-identical.
        sr_score = sr_gate = None
        if qe_on and qe_step_stats["skipped"] and not qe_step_stats["scored"]:
            reasons = sorted(qe_skip_reasons)
            if reasons:
                warnings = list((run.context or {}).get("warnings") or [])
                warnings.append(
                    {
                        "code": "qe_never_scored",
                        "step_id": step.id,
                        "message": f"QE gate on step {step.name or step.id} scored no segments: "
                        + "; ".join(reasons),
                    }
                )
                run.context = {**(run.context or {}), "warnings": warnings}
        if qe_on and qe_step_stats["scored"] > 0:
            sr_score = round(100.0 * qe_step_stats["passed"] / qe_step_stats["scored"], 2)
            sr_gate = "passed" if qe_step_stats["failed"] == 0 else "failed"
        if qe_on:
            history = list((run.context or {}).get("qe_steps") or [])
            history.append(
                {
                    "step_id": step.id,
                    "step_name": step.name or step.step_type,
                    "step_key": step.key,
                    "visit": guard.visit_index(step),
                    **qe_autofix.visit_counts(
                        (qe_seg[j] for j in qe_visit_indices),
                        (qe_autofix_seg[j] for j in qe_visit_indices if j in qe_autofix_seg),
                    ),
                }
            )
            run.context = {**(run.context or {}), "qe_steps": history}
        _sr = StepRun(
            workflow_run_id=run.id,
            step_id=step.id,
            status=RunStatus.FAILED if step_failed else RunStatus.COMPLETED,
            **_sys_stamp,
            input_text=input_blob,
            output_text=output_blob,
            provider_used=provider_used,
            model_used=model_used,
            attempts=attempts_total,
            error=step_note,
            latency_ms=latency,
            # Real per-step-visit spend (feat/workflow-spend): the segment fan-out's
            # exact ledger cost + tokens + the QE-judge spend. Stamped unconditionally
            # for this llm_agent step-visit (0.0 when every segment was TM-filled and no
            # completion ran — an honest zero, so the run still reads as tracked).
            cost_usd=step_cost,
            tokens_in=step_tin,
            tokens_out=step_tout,
            score=sr_score,
            gate=sr_gate,
            # run-190 Item C: the per-step failure SHAPE (metadata only) — the last errored
            # segment's finish reason + provider error class, and the chars of translation
            # text this step visit actually PRODUCED.
            finish_reason=seg_finish_reason,
            error_class=seg_error_class,
            # M-2 (gate round 2): output_chars is the PRODUCED translation length, honouring
            # the column contract ("characters of translation text this step visit actually
            # produced") and matching the FULL_TEXT seam's len(output). It is the sum of the
            # COMMITTED per-segment text — a withheld unit (refusal hedge or over-translation
            # dump; both live in refused_seg, which equals the write-back's withhold set in
            # BOTH refusal-guard regimes) counts 0, NOT the length of the withheld-aware blob
            # (which folds in the SEG_JOINER separators and the "[withheld — ...]" markers, so
            # a fully-withheld step would otherwise report a non-zero "produced" figure).
            # LOW-1 (gate round 3): also exclude the DEGRADED segments — a step whose pending
            # segments errored (e.g. a thinking burn) KEEPS each one's prior working[] text
            # rather than committing new text this visit, so counting it stamped the CARRIED-
            # OVER length (a step-2 burn reporting step-1's chars) against the "this step visit
            # produced" contract. A degraded segment produced nothing this visit -> counts 0,
            # matching the FULL_TEXT seam (len(output)==0 on a burn).
            # LOW-2 (gate round 4): sum over `pending_idx` — the segments THIS step visit
            # actually processed — not `range(len(segments))`. A status-protected
            # (human-approved / edited), TM hard/soft-locked or content-protected segment was
            # dropped from `pending_idx` and KEEPS its prior working[] text (carry-over), so
            # counting it stamped a previous visit's chars against the "this step visit produced"
            # contract exactly as the degraded case did. `pending_idx` already excludes all of
            # those; refused/degraded segments in it produced nothing this visit -> still 0.
            # LOW-2 (gate round 5): also exclude `reverted_idx` -- a fix-guard REVERTED segment
            # is in `pending_idx`, is neither refused nor degraded, but kept its PRIOR working[]
            # text (the rejected edit was discarded), so counting it is the same carry-over
            # breach. LOW-1 (gate round 5): ADD the step-0 TM fills (`tm_filled_idx`) -- they are
            # this visit's delivery but never entered `pending_idx`. The two sets are disjoint;
            # union + the exclusions yields exactly the indices this visit produced text for.
            output_chars=sum(
                len(working[j])
                # #705: a step-0 tags-only copy is this visit's delivery too (no call).
                for j in (set(pending_idx) | tm_filled_idx | tags_only_step0_idx)
                if j not in refused_seg and j not in degraded_idx and j not in reverted_idx
            ),
            outcome_index=guard.visit_index(step),
            started_at=step_started_at,
            finished_at=datetime.utcnow(),
        )
        # SPEC §5.4: a QE-gated step with a branch routes by its run-level QE gate
        # (Phase 3). sr_gate is None on a non-QE step -> cursor+1 (byte-identical). A
        # failed step returns below (run FAILED), so next_cur is only consumed on success.
        # H2: a QE gate is a gate too -- a refused `on_fail` loop-back from a FAILED QE
        # verdict ends the run HELD rather than running the downstream steps on text the
        # gate did not pass.
        next_cur, _qe_hold_cause = _branch_cursor(
            step, cursor, gate=sr_gate, key_index=key_index, steps=steps, guard=guard
        )
        if _qe_hold_cause:
            _record_gate_hold(session, run, step=step, step_index=cursor, cause=_qe_hold_cause)

        # P-C5: this `INSERT INTO steprun` (+ the run's step-index UPDATE) is the exact
        # write the QA flood reproduced `database is locked` on at ~15+ concurrent runs.
        # Commit it through the bounded jittered writer-lock retry so a transient herd
        # collision is ridden out instead of failing the whole run. `_stage` re-applies
        # both mutations on each attempt because a rollback expunges the pending StepRun
        # and reverts the run UPDATE; the StepRun row is built ONCE above so a retry
        # never drifts its timestamps/spend. Byte-identical to a plain commit when the
        # first attempt succeeds (the common, uncontended case).
        # #504: a lock-retry rollback reverts EVERYTHING pending on the run, not just the
        # StepRun and cursor. Capture the run's full staged state now (qe_steps, the QE
        # gate_hold + its warning, qe_never_scored, ...) so each attempt re-applies it all.
        _run_attrs = _pending_run_attrs(run)

        def _stage_step_write(_row=_sr, _next=next_cur, _attrs=_run_attrs):
            session.add(_row)
            _reapply_run_attrs(run, _attrs)
            run.current_step_index = _next
            session.add(run)

        _db.commit_with_retry(session, stage=_stage_step_write)
        step_ev = {
            "type": "step",
            "run_id": run.id,
            "step_index": cursor,
            "step_name": step.name or step.step_type,
            "step_type": step.step_type,
            "provider": provider_used,
            "model": model_used,
            "chunks": 1,
            "attempts": attempts_total,
            "error": step_note,
            "output": output_blob,
        }
        # QE gate: surface this step's tally ONLY when the gate was on, so an OFF step's
        # event shape is byte-identical to pre-QE (the F2 event-shape assertions hold).
        if qe_on:
            step_ev["qe"] = dict(qe_step_stats)
        # LQA safety floor: attach this step-visit's guard tally ONLY when a guard
        # actually acted, so an ordinary step's event shape stays byte-identical (the
        # F2 event-shape assertions hold) while a reverted fix / protected TM segment is
        # never invisible on the live SSE stream.
        # GitHub #45 (P8): a degrading step joins the same slot on the same terms — a
        # step-visit that quietly "completed" while N segments kept their pre-step text
        # must not be invisible on the live SSE stream either.
        if reverted_idx or tm_protected_idx or degraded_idx:
            step_ev["lqa_safety"] = {
                "reverted": len(reverted_idx),
                "tm_protected": len(tm_protected_idx),
                "degraded": len(degraded_idx),
            }
        yield step_ev

        # F1+F2: if this step failed, stop now and mark the run FAILED WITHOUT any
        # write-back — targets are left untouched (no blank/partial MT).
        if step_failed:
            _commit_run_terminal_direct(session, run, RunStatus.FAILED)
            yield {
                "type": "error",
                "run_id": run.id,
                "error": err or "workflow step produced no output",
            }
            return
        cursor = next_cur

    # F1+F2: segment write-back happens ONCE here, AFTER the whole step loop, and
    # ONLY on run success (no step failed, run not paused — a pause returned early).
    # Writing from working[] (rather than at the last output step inside the loop)
    # means a human-review-as-LAST-step resume persists the operator's edits: on
    # resume `working` holds the re-split edited seed_text, the empty range(N,N)
    # loop runs no steps, and this block still writes the edits to every target.
    # Review F1b -- THE LAST THING CHECKED BEFORE ANYTHING IS WRITTEN. A recovery
    # sweep is a DB write and cannot stop this thread; if one already failed our row,
    # a REPLACEMENT run now owns this segment set, and flushing here would resurrect a
    # dead run AND race the replacement's writes. Discard instead: no targets, no
    # changelog, no TM, no COMPLETED.
    if _abort_if_superseded(session, run):
        session.rollback()
        yield {
            "type": "error",
            "run_id": run.id,
            "error": "This run was superseded by a recovery sweep while it was "
            "still executing; its results were discarded.",
        }
        return
    # F5: the text actually committed per segment (refused units left as "" so the
    # terminal `done` event, built from this below, never leaks a raw hedge that was
    # withheld from the DB — see the write-back loop).
    emitted = list(working)
    # Q6/F6 (core-QA fix train 1): the tm_match FILLs eligible to KEEP their lock band.
    # Populated at the END of each write-back iteration (see the decision block there),
    # NOT gated on `status == MT`. A fill lands here only when the seam committed it as
    # authoritative content (committed AND tm_eligible) and its SOLE divergence from clean-
    # MT status is a soft glossary/dnt flag -- so such a flag no longer strips the band,
    # while a poisoned hit (tm_eligible False), a QE fail, an over-limit fill, a step error
    # and a reverted fixer edit all stay UNLOCKED. This is the lock-apply seam's gate below.
    tm_lock_content_ok: set[int] = set()
    # R3 (fix round): the fills whose CLEAN commit nonetheless caught a SOFT dnt/glossary
    # adherence flag. Populated in the write-back loop below (the dnt/glossary re-assertion
    # blocks). A flagged fill still LOCKS (Q6 -- the flag rides alongside the band), but it
    # must NEVER hard-lock: consulted at the lock-apply seam to downgrade a hard plan to
    # SOFT (see the policy comment there).
    tm_lock_flagged: set[int] = set()
    # ISSUE #95 (gate C round 1, H1): the run-level GATE HOLD, read ONCE here and applied
    # per segment in the write-back below. None on every run whose gates ended cleanly ->
    # the write-back is byte-identical to before.
    # ROUND 2, M2: and the ending that has no correction loop -- decided here, off the
    # STANDING VERDICT, because routing is not the posture (see `_terminal_gate_hold`).
    _gate_hold = _terminal_gate_hold(session, run, steps) if not any_step_failed else None
    # ROUND 2, H1: and whether this run may CLEAR a standing badge off a unit it produced
    # -- only a run whose own gate PASSED may (see `_run_gate_outcome`). Read once.
    # ROUND 3, M1: per UNIT, and only over the text this write-back is about to commit --
    # a producer that ran AFTER the passing check leaves the badge exactly where it is.
    # ROUND 4, M2: each unit is released in the scope of the family that HELD it, so a hold
    # a QE step's own correction loop recorded (ERRATUM E1) is releasable by a clean run of
    # that same workflow. An all-`lqa` document is one group and reads exactly as in round 3.
    _gate_release = (
        _gate_release_mask(
            session,
            run,
            working,
            held_families=[quality.gate_hold_families(s.qa_flags) for s in segments],
        )
        if not any_step_failed
        else [False] * len(working)
    )
    if not any_step_failed:
        for j, seg in enumerate(segments):
            if j in _vr_skip:
                # F-CRIT: a vendor/external RETURN already made the authoritative content
                # decision for this slot through the guarded write seam (a source-echo
                # no-op that kept the prior target, a quarantined refusal, a hard-lock, or a
                # slot an EARLIER partial return admitted). Re-committing the resume seed
                # here would ship the parked SOURCE as clean MT and erase that verdict -- so
                # leave target_text / status / qa_flags exactly as the admission left them,
                # and let the terminal `done` event carry the actually-stored target.
                emitted[j] = seg.target_text or ""
                continue
            if j in content_protected and working[j] == (seg.target_text or ""):
                # CONTENT-PROTECTION (Q9 re-review, probe 5): a byte-exact 100% TM fill under a
                # soft review gate (pct == 100 + NEEDS_REVIEW + qa badge) the tm_match re-lock
                # whitelist DELIBERATELY did not promote. Running commit_mt_output here -- even
                # on the byte-identical stored target -- would rewrite the sentinel, flip status
                # to MT, and let the always-on stale-clear ERASE the badge (the exact content-
                # destruction this class exists to prevent). So skip the commit entirely,
                # EXACTLY like a _vr_skip slot: leave target_text / status / qa_flags as stored
                # (review routing intact) and emit the stored target. Never re-labelled (no
                # tm_origin) and never locked (not in tm_lock_plan, so the lock-apply seam below
                # cannot reach it).
                #
                # THE `working[j] == seg.target_text` GUARD (mirrors the F5 pause-edit guard
                # below): the protected slot's working text is the stored target the tm_match
                # step seeded. If an operator EDITED it during a human_review pause, working[j]
                # (the re-split resume seed) no longer byte-equals the stored target -- HUMAN
                # EDIT WINS: fall through to the ordinary MT commit so the edit is written (as
                # any resumed operator edit is), rather than silently discarded. Only an
                # UNTOUCHED protected slot (the common no-pause path, and an unchanged resume)
                # takes the skip.
                emitted[j] = seg.target_text or ""
                continue
            if (
                j not in operator_edited
                and (j in status_protected or seg.status in (SegStatus.APPROVED, SegStatus.EDITED))
                and working[j] == (seg.target_text or "")
            ):
                # STATUS PROTECTION (Layer 2 write-back): a human-approved/edited segment is
                # authoritative -- never re-commit it. Leave target_text / status / qa_flags
                # EXACTLY as the human left them and emit the stored target, EXACTLY like a
                # content-protected slot. Reads seg.status directly too, so it holds even when
                # `status_protected` was not repopulated (a human-review-as-last-step resume)
                # and honours a mid-pause approval on a reloaded segment. THE
                # `working[j] == seg.target_text` GUARD MIRRORS the content_protected pause-edit
                # guard above: an operator who EDITED this slot during a human_review pause
                # (`j in operator_edited`, working[j] no longer byte-equals the stored target)
                # FALLS THROUGH to the ordinary commit so the edit LANDS via the seam's
                # human_edit channel -- HUMAN EDIT WINS. The commit_mt_output seam refuses an
                # untouched approved slot as well (defense in depth; the freshness backstop
                # below catches a mid-RUN approval whose stale seg.status still reads unprotected).
                emitted[j] = seg.target_text or ""
                continue
            if j in tags_only_idx and j not in operator_edited:
                # #705: a segment with no translatable text was never dispatched; land the
                # source as its own translation through the shared copy action (status MT,
                # never TM-eligible, no QE / fixer / gate-hold re-assertion -- nothing judged
                # or produced it). A hard lock or a human-approved row is the seam's own
                # disposition: emit whatever the row holds. An operator pause-edit of the
                # slot falls through to the ordinary commit below (HUMAN EDIT WINS).
                _tc = _tags_only.commit_copy(session, seg)
                if getattr(_tc, "committed", False):
                    tags_only_ct += 1
                elif getattr(_tc, "lock_refused", False):
                    # gate: the same run counters the ordinary commit below keeps.
                    if j not in tm_hard_idx:
                        tm_lock_refused_ct += 1
                elif getattr(_tc, "status_protected", False):
                    try:
                        session.refresh(seg, attribute_names=["target_text"])
                    except Exception:
                        log.warning(
                            "status-protected segment %s: target_text refresh failed",
                            seg.id,
                            exc_info=True,
                        )
                    status_protected_ct += 1
                emitted[j] = seg.target_text or ""
                continue
            # Batch 15 fix-train (D15-6 / LAW-TM-13, LAW-TM-16) -- THE PRODUCED-MASK GATE.
            # A slot NO step produced this run still holds the untouched source-seeded
            # `working` buffer (`working = list(seg_sources)` at run start). EVERY genuine
            # production -- a TM fill, a provider translation, a persisted-lock re-seed, an
            # operator pause-edit of a protected slot -- reassigns working[j] AND pairs it
            # with `produced.add(j)`, so at this terminal write-back `j not in produced`
            # means working[j] is byte-identical to seg_sources[j] (the `working[j] ==
            # seg_sources[j]` term below asserts that pairing; if some future edit ever
            # mutated working without adding to `produced`, this term keeps the slot on the
            # COMMIT path -- the safe direction, never a silent content drop). Committing
            # such a slot writes the SOURCE text through the seam as clean MT (the `else`
            # MT-origin branch below: status mt, mt_confidence 0.9) under a target-locale
            # filename -- the exact SILENT SOURCE-FILL that makes a COMPLETED tm_match-only
            # run leave ZERO empties, so `tm_delivery.empty_gate_decision` never fires
            # (D15-6 unreachable on its primary path) and the deliverable ships English.
            # So SKIP the commit entirely, EXACTLY like the _vr_skip / content_protected
            # skips above: leave target_text / status / qa_flags as they stand (empty + NEW
            # on a fresh doc -> the gate HOLDS and the human sees the empties; a PRIOR run's
            # translation on a re-run -> PRESERVED, never blanked with source), and emit the
            # stored target so the terminal `done` event stays honest (F5). A slot a step
            # GENUINELY produced -- INCLUDING a translator that legitimately output
            # source-identical text (names/numbers/short strings): it went through the
            # produce path, so `produced.add(j)` ran REGARDLESS of the text value -- is NOT
            # in this branch and commits exactly as today (LAW-TM-3 byte-inertness for every
            # pipeline with a real translator). `operator_edited` is excluded so an operator
            # who typed the source back during a human_review pause still LANDS the edit (a
            # machine-status pause-edit is deliberately NOT added to `produced`, PART 1).
            # `seg_step_error` is excluded so a degraded/step-errored slot keeps its
            # NEEDS_REVIEW routing + step_error badge (a human-VISIBLE disposition, never a
            # silent source-fill; note `_seg_call_degraded` only records one for an already-
            # produced slot in-run, so this guards the resume-restored case, item 4806).
            # RESUME-SAFETY (the load-bearing pair): `produced` is RUN-LOCAL and re-derived
            # by TEXT COMPARISON at the top of a resumed generator (item 4820), so a genuine
            # production whose text HAPPENS to equal its source -- a live provider's English
            # ECHO (the lang_guard class) or a TM fill whose target byte-equals its source --
            # is NOT reconstructed into `produced` after a human_review pause. Both are
            # recovered from the DURABLE, pause-PERSISTED per-segment markers instead:
            # `seg_lang_guard[j]` (True for a live-provider output, restored at item 4794 --
            # its whole point is that the echo must still be lang-guarded at THIS write-back)
            # and `tm_origin[j]` (True for a TM fill, restored with tm_lock_plan). Excluding
            # both keeps a source-identical production on the COMMIT path across a resume, so
            # the lang_guard / TM-provenance stamping still runs -- only a slot NOTHING ever
            # produced is skipped.
            if (
                j not in produced
                and not tm_origin[j]
                and not seg_lang_guard[j]
                and j not in operator_edited
                and j not in seg_step_error
                and working[j] == seg_sources[j]
            ):
                emitted[j] = seg.target_text or ""
                continue
            # F5 (Q9 fix round): an operator edit during a human_review pause can land on a
            # slot the tm_match step planned to FILL or RE-LOCK. On resume working[j] carries
            # the EDITED text, but the restored tm_lock_plan + tm_origin would otherwise stamp
            # TM provenance (band + tm_id) and SegStatus.MT/pct=100 onto content a human just
            # changed -- a stale lie (pre-existing for fills; re-lock widened the exposure).
            # Cheapest sound guard: if the working text no longer byte-equals the TM target
            # the plan intended, DROP the plan entry + origin for this slot -- no stamp, no
            # origin -- so it commits down the ordinary MT path like any other operator edit.
            # (An unplanned slot has no tm_plan_tgt entry and is untouched. A legacy paused
            # run predating tm_plan_tgt has no target to compare -> plan left intact.)
            # F1 (Q9 final round): keyed on `tm_plan_tgt` ALONE, not tm_lock_plan -- the
            # step-0 TM short-circuit records a planned target with NO lock plan, and that
            # path also stamps TM provenance an operator edit must be able to clear.
            # tm_lock_plan.pop(j, None) is None-safe, so a plan-less slot drops cleanly.
            if j in tm_plan_tgt and tm_plan_tgt[j] != working[j]:
                tm_lock_plan.pop(j, None)
                tm_origin[j] = False
                # F4 (Q9 final round): also CLEAR the stale 100% match_pct. A prior TM-exact
                # fill (or this run's fill/re-lock) left tm_match_pct=100 on the row; once a
                # human has edited the text it is no longer a 100% match, and leaving the 100
                # would misprice the human-edited segment as a full TM leverage hit in
                # vendor_cost.classify_band / tm.leverage_bands. tm_origin is now False, so the
                # write-back's TM branch will not re-stamp it -> it stays cleared.
                seg.tm_match_pct = None
            # Refusal / meta-output GUARD (across-the-board net; DEFAULT ON — see
            # app/services/refusal.py). The orchestrator llm_agent path builds its
            # prompt from a USER-AUTHORED step template (ROLE_PRESETS), NOT the
            # hardened translation_prompt, so LAYER 1 does not cover it — this guard
            # is the net. A produced segment that reads as a conversational refusal is
            # routed to review WITHOUT writing target_text, so the hedge is never
            # committed (and the connector write-back, which excludes NEEDS_REVIEW,
            # never posts it). Only ever downgrades suspicious output to review.
            # C1: `refused_seg` already holds every segment the PER-STEP guard caught
            # at the step that produced the hedge (before a finalizer could launder
            # it) — honoring it here is the authoritative signal; the is_refusal()
            # re-check stays as defense in depth (working[j] still holds the frozen
            # hedge for a refused segment, so both agree).
            # Route the write through the sanctioned CHOKE POINT (arch-review Finding
            # 2). `already_refused` carries the C1 authority: refused_seg holds every
            # segment the PER-STEP guard already froze as a hedge, so the seam
            # quarantines it WITHOUT re-running the detector; the is_refusal(working[j])
            # re-check inside the seam stays as defense in depth (working[j] still holds
            # the frozen hedge, so both agree). Gated by the kill switch exactly as
            # before: (guard and refused_seg) OR (guard and is_refusal) == the old
            # `guard and (refused_seg or is_refusal)`.
            # QA campaign BUG-06 / adversarial review: snapshot the `nontranslation`
            # flag family BEFORE the commit. That verdict has no CommitOutcome field, so
            # the re-assertion below has to read it off the flags the seam writes — and
            # NOTHING ever CLEARS this family, so a flag left by an earlier run (or by
            # the translate path, which is the one that opts into `lang_guard`) would
            # otherwise pin the segment at NEEDS_REVIEW forever, even after a clean
            # re-run produced a perfectly good target. Reproduced. Comparing by IDENTITY
            # is exact here: every `merge_*` helper rebuilds the LIST but reuses the
            # surviving flag OBJECTS, so a flag object that is new after the call is one
            # THIS commit wrote.
            _nt_before = [
                id(f)
                for f in (seg.qa_flags or [])
                if isinstance(f, dict) and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            ]
            outcome = quality.commit_mt_output(
                session,
                seg,
                working[j],
                source="workflow",
                # F1 (adversarial review): an operator edit made on an approved/edited slot
                # during a human_review pause is HUMAN content -- carry it through the seam's
                # human_edit channel so the status gate does not refuse it (the edit MUST land,
                # exactly as it does on main, landing as SegStatus.MT). Empty (False) on every
                # non-resume / non-edited slot -> byte-identical.
                human_edit=(j in operator_edited),
                # F-LOW-1: an admitted VENDOR/EXTERNAL slot carries its "TM-ineligible"
                # intent through the resume re-commit (the vendor return committed it with
                # tm_eligible=False; the default True here would silently drop that intent).
                # Empty on every non-vendor resume -> byte-identical.
                tm_eligible=(j not in _vr_tm_inelig),
                # H2 (gate round 1): a refusal hedge is withheld only when the refusal guard
                # is ON (its kill switch), but an over-translation page DUMP is withheld
                # unconditionally — governed by OVER_TRANSLATION_MAX_RATIO alone, never by
                # REFUSAL_GUARD_ENABLED. Without the second term, REFUSAL_GUARD_ENABLED=0 would
                # commit the dump to target_text while output_blob still rendered WITHHELD (the
                # run trace contradicting the delivered target). The invariant this restores:
                # output_blob is WITHHELD iff target_text was not written.
                already_refused=(
                    (config.REFUSAL_GUARD_ENABLED and j in refused_seg) or j in over_translation_seg
                ),
                # #343: an output-guard withhold carries its own qa_flags entry (own type).
                # (a SOFT number-mismatch entry rides the same channel: the seam keeps the text
                # and only flags + routes it, unless the segment is withheld for another reason)
                output_guard_flag=output_guard_seg.get(j)
                or (output_guard_soft_seg.get(j) if j not in refused_seg else None),
                # M-1 (gate round 2): an over-translation withhold is NOT a model refusal.
                # Hand the seam an honestly-named `nontranslation`-family flag (the shape rule
                # that fired + the char counts) so a genuine refusal keeps the refusal
                # disposition while a page dump reads as over-translation. None for every
                # non-dump segment -> the refusal disposition exactly as before.
                over_translation_flag=(
                    refusal.over_translation_flag(
                        "workflow",
                        shape=over_translation_meta.get(j, {}).get("shape"),
                        src_len=over_translation_meta.get(j, {}).get("src_len"),
                        tgt_len=over_translation_meta.get(j, {}).get("tgt_len"),
                        reason=over_translation_meta.get(j, {}).get("reason") or None,
                    )
                    if j in over_translation_seg
                    else None
                ),
                # Terminology T3 (LAW-TERM-15 consumer 1): the producing step's OWN
                # write-back enforces against what the model was SHOWN — the shown set of
                # the toggle-ON step that produced this segment's final text, else the full
                # match exactly as today. So a capped-out / human-only term the producer was
                # never shown cannot become a manufactured glossary/DNT violation here.
                glossary_terms=shown_terms_by_segment.get(j, seg_terms[j]),
                # GitHub #43 (C1): opt into the language-mismatch signal for a segment
                # whose committed text came from a LIVE provider. Per-segment, resolved
                # by `translate._lang_guard_for` (the SAME rule the plain-MT path uses),
                # so a TM-filled segment and every offline MockProvider echo stay off
                # and commit exactly as before.
                lang_guard=seg_lang_guard[j],
                llm_tag_repair=(
                    tag_llm_repair.make_repairer(
                        src_locale=src,
                        tgt_locale=locale,
                        spec=_tag_spec,
                        redact=_tag_redact,
                        verify_spec=_tag_verify_spec,
                        spend_sink=_tag_spend,
                        tap_meta=tap_envelope.stamp_meta(
                            {
                                "workflow_id": workflow_id,
                                "workflow_run_id": run.id,
                                "document_id": doc_id,
                                "segment_index": seg.order_index,
                            },
                            *_env_ids,
                        ),
                    )
                    if _tag_spec is not None
                    else None
                ),
            )
            if outcome.label_echo_stripped:
                # #664: the seam removed an echoed prompt label from the committed target;
                # the run's emitted text must be that same cleaned string, not the raw output.
                emitted[j] = seg.target_text or ""
            if outcome.flagged:
                if outcome.lock_refused:
                    # item 9: a HARD-lock refusal. Its authoritative stored TM target is
                    # intact -- emit THAT, not "", so the run's final text is honest.
                    emitted[j] = seg.target_text or ""
                    # LOW-11: do NOT count the EXPECTED refusal of a segment the run
                    # deliberately EXCLUDED and seeded with its own stored text (that is a
                    # no-op re-commit the seam declines every run -- counting it inflates
                    # the number on every re-run). Count ONLY an UNEXPECTED refusal: a
                    # hard-locked segment a step actually tried to overwrite despite the
                    # exclusion (defense-in-depth), which is the real disposition worth
                    # surfacing.
                    if j not in tm_hard_idx:
                        tm_lock_refused_ct += 1
                elif outcome.status_protected:
                    # STATUS PROTECTION backstop: a human approved/edited this segment MID-run
                    # (the write-back skip above missed it because seg was loaded stale, but the
                    # seam's freshness re-read caught it). Emit the stored target (refresh
                    # target_text -- only status was re-read in the seam) and count it; never
                    # route to review.
                    try:
                        session.refresh(seg, attribute_names=["target_text"])
                    except Exception:
                        # A stale target_text here would emit pre-edit text over a human edit.
                        log.warning(
                            "status-protected segment %s: target_text refresh failed",
                            seg.id,
                            exc_info=True,
                        )
                    emitted[j] = seg.target_text or ""
                    status_protected_ct += 1
                else:
                    emitted[j] = ""
                # F6: skip the QE routing gate below for a refused unit — it is already
                # routed to NEEDS_REVIEW with a refusal qa_flag; scoring a hedge for
                # quality is meaningless. Refusal supersedes QE.
                continue
            if j in operator_edited and working[j] == operator_edited_text.get(j):
                # PART 1 (pause-edit protection): an operator pause-edit that is STILL the final
                # content lands as SegStatus.EDITED -- human work, protected from every LATER
                # RUN's machine passes by the status guard (the seam refuses APPROVED/EDITED).
                # This ends the flood's silent-loss strand: the operator's edit was landing `mt`,
                # unprotected, and the next run's tm_match-skip + MT-scaffold destroyed it.
                #
                # IN-RUN BOUNDARY (protection applies from the NEXT run on, NOT this one): a step
                # AFTER the human_review pause that re-ran over this slot changed working[j] away
                # from the operator's captured text, so `working[j] != operator_edited_text[j]`
                # and it falls through to the ordinary MT path below -- the workflow explicitly
                # asked for machine processing of the edit (e.g. [review, MT] re-MTs it). The
                # commit above already used human_edit=True, so the seam did not refuse the write.
                #
                # Skip the tm_origin/MT stamping, the QE gate, and every soft-flag NEEDS_REVIEW
                # re-assertion below: routing human work to review would UN-protect it (the seam
                # does not protect NEEDS_REVIEW) and re-open exactly this strand. The seam's
                # qa_flag badges (dnt/glossary/etc.), if any, are still recorded for the editor.
                # emitted[j] already holds working[j] (the edit) from the `emitted = list(working)`
                # seed. tm_origin[j] is False here (the F5 guard dropped any TM plan for an edit),
                # so no TM band/provenance is stamped and the lock-apply pass below cannot reach it.
                #
                # TWO carve-outs so the protection never STRANDS a slot (review F1/F2):
                #   F1 -- a BLANKED pause-edit resets to NEW (empty), the documented clear-to-reset
                #   escape hatch: landing an empty slot EDITED would protect it FOREVER, so no
                #   machine pass could ever refill it (the exact strand class ft-clear F1 closed,
                #   and what this change's own workflows.md sentence promises). Both direct editors
                #   already reset an emptied unit to NEW.
                #   F2 -- a TAG-BREAKING pause-edit routes to NEEDS_REVIEW WITH its badge (the seam
                #   already wrote the tag_integrity flag): both direct editors route a tag-damaging
                #   human save to review, and because EDITED is protected, keeping it EDITED would
                #   freeze a structurally broken segment that even tag_llm_repair could no longer
                #   touch (only the export guard would catch it, at delivery). Losing protection on
                #   a broken segment is the right trade and restores editor parity.
                if not (working[j] or "").strip():
                    seg.status = SegStatus.NEW
                    # LOW: a blanked TAGGED slot picks up a spurious tag_integrity badge from the
                    # seam (tag_damage vs the empty target reads as "dropped ⟦x⟧") -- a flag on text
                    # that no longer exists. Clear it, exactly as save_paragraph's empty branch does.  # noqa: E501
                    seg.qa_flags = quality.clear_tag_flags(seg.qa_flags)
                elif outcome.tag_violation:
                    seg.status = SegStatus.NEEDS_REVIEW
                else:
                    seg.status = SegStatus.EDITED
                    operator_edited_landed_ct += 1
                session.add(seg)
                continue
            if tm_origin[j]:
                # F5: keep TM provenance — mirror translate.py's segment-TM branch
                # (SegStatus.MT + tm_match_pct, mt_confidence left unset). Batch 15 T1
                # (LAW-TM-19): stamp the fill's REAL pct -- an exact/step-0 fill stamps 100
                # (byte-identical), a below-100 fuzzy fill (incl. a "none" fill with no lock
                # plan) stamps its true score, which keeps it OUT of the `pct==100 AND
                # status==MT` re-lock whitelist and gives vendor scoping the true band.
                seg.status = SegStatus.MT
                seg.tm_match_pct = tm_fill_pct.get(j, 100)
                # Q6/F6: lock-eligibility is decided ONCE at the END of this iteration
                # (below, just before session.add) — NOT here — because the narrowing
                # conditions (QE fail, char_limit, step_error, fix_reverted) are asserted
                # after this point and must all be known before the fill can be locked.
            else:
                # MT origin — mirror translate._apply_result (MT, confidence 0.9).
                seg.status = SegStatus.MT
                seg.mt_confidence = 0.9
            # #683: note a translation produced in pieces (cleared for a one-piece or TM one).
            from app.services.mt_chunking import stamp as _mt_stamp  # lazy (kernel facade)

            _mt_stamp(seg, None if tm_origin[j] else seg_mt_chunks.get(j))
            # QE routing gate: apply the winning QE result for this segment (the last
            # step that scored it). A gate FAIL routes the segment to the review queue
            # (SegStatus.NEEDS_REVIEW overrides the MT status set above — the reviewer
            # surface is the CAT editor status chip); pass/skip leave the status as-is.
            # The score+rationale is stored in Segment.qa_flags EITHER way (pass, fail,
            # or skip) so the editor's QE badge always reflects the judge's verdict.
            card = qe_seg.get(j)
            if card is not None:
                passed = card.get("passed")
                if passed is False:
                    seg.status = SegStatus.NEEDS_REVIEW
                seg.qa_flags = qe.merge_qe_flag(
                    seg.qa_flags,
                    {
                        **qe_autofix.public_flag(card, session=session),
                        **qe.qe_flag(
                            card.get("score"),
                            card.get("rationale", ""),
                            card.get("threshold", qe.DEFAULT_THRESHOLD),
                            passed is True,
                            bool(card.get("skipped")),
                            reason=card.get("reason"),
                            error=card.get("error"),
                        ),
                    }
                    if not card.get("undecided")
                    else {
                        **card,
                        "type": "qe",
                        "severity": "info",
                        "message": f"QE undecided: {card.get('rationale', '')}",
                    },
                )
                # QE auto-fix record (Train B) beside the qe flag: the rounds, fixer and
                # stop reason the reviewer reads. A stale record from an earlier run is
                # dropped when this run's verdict came without one; a segment that never
                # carried one is untouched (byte-identical with the opt-in off).
                af_rec = qe_autofix_seg.get(j)
                if af_rec is not None:
                    seg.qa_flags = qe_autofix.merge_flag(seg.qa_flags, af_rec)
                elif qe_autofix.has_flag(seg.qa_flags):
                    seg.qa_flags = qe_autofix.clear_flag(seg.qa_flags)
            # DNT verification (2B): the choke point already appended the dnt_violation
            # qa_flag (and merge_qe_flag above preserved it), but the MT/TM status lines
            # here RESET status to MT — so re-apply NEEDS_REVIEW for a DNT miss, exactly
            # as a QE fail does. A DNT miss still committed the text (not blocked), it
            # just routes to a human. No violation -> no-op.
            if outcome.dnt_violations:
                seg.status = SegStatus.NEEDS_REVIEW
                tm_lock_flagged.add(j)  # R3: a flagged fill never hard-locks (seam below)
            # Glossary adherence (Track B / B1): mirror the DNT re-application above.
            # The choke point already appended the glossary_violation qa_flag (and
            # merge_qe_flag preserved it), but the MT/TM status lines RESET status to
            # MT — so re-apply NEEDS_REVIEW for a required-term miss, exactly as a DNT
            # miss / QE fail does. The text was still committed (soft, not blocked).
            if outcome.glossary_violations:
                seg.status = SegStatus.NEEDS_REVIEW
                tm_lock_flagged.add(j)  # R3: a flagged fill never hard-locks (seam below)
            # AI Tagging 1.2: re-apply NEEDS_REVIEW for unrepaired inline-tag damage.
            # The choke point already appended the tag_integrity qa_flag and forced
            # tm_eligible=False, but the MT/TM status lines above RESET status to MT —
            # so re-assert review here, like DNT/glossary. HARD gate: a tag-broken
            # target is TM-ineligible AND withheld from connector push (NEEDS_REVIEW is
            # what the write-back excludes), so a dropped run token never seeds TM nor
            # pushes to a live connector. A deterministically-repaired segment carries
            # NO violation (outcome.tag_violation == ""), so it lands clean.
            if outcome.tag_violation:
                seg.status = SegStatus.NEEDS_REVIEW
            # QA campaign BUG-06: the two new choke-point gates need the SAME
            # re-assertion. `quality.commit_mt_output` sets NEEDS_REVIEW for each, but
            # the MT/TM status lines above then RESET status to MT — so on the workflow
            # path alone the routing was silently wiped and a segment carrying a
            # CORRUPTED FIGURE or a surviving translator note shipped as clean MT.
            #   * number_violation — a source number's value is not preserved by any
            #     target number, read with the target locale's decimal convention.
            #   * meta_commentary — a model self-commentary parenthetical survived into
            #     the delivered target.
            # Both are SOFT (the text is committed) and both already forced
            # tm_eligible=False at the seam, so this only restores the human routing.
            if outcome.number_violation:
                seg.status = SegStatus.NEEDS_REVIEW
            if outcome.meta_commentary:
                seg.status = SegStatus.NEEDS_REVIEW
            # #343: a SOFT output-guard hit (dropped / added number) keeps the text but must
            # stay in review; the status lines above reset it to MT.
            if j in output_guard_soft_seg and j not in refused_seg:
                seg.status = SegStatus.NEEDS_REVIEW
            # K8: the char_limit gate rides its OWN family (not nontranslation), so unlike
            # K4's length_runaway it gets NO coverage from the flag-family re-assertion
            # below — the MT/TM status lines above would wipe its NEEDS_REVIEW and a key
            # value over its UI-slot limit would ship as clean MT (dropped by `--status
            # needs_review`, shown as MT beside a char_limit badge, and pushed by a
            # connector that gates on status). Same one-line re-assertion as number/meta.
            if outcome.char_limit_violation:
                seg.status = SegStatus.NEEDS_REVIEW
            # LC-7 / QA-1: the placeholder and whitespace gates need the SAME re-assertion
            # (the MT/TM status lines above wipe the seam's NEEDS_REVIEW on the workflow
            # path alone). placeholder is HARD (already tm_eligible=False at the seam);
            # whitespace is SOFT (tm_eligible untouched) — both only restore human routing.
            if outcome.placeholder_violation:
                seg.status = SegStatus.NEEDS_REVIEW
            if outcome.whitespace_violation:
                seg.status = SegStatus.NEEDS_REVIEW
            # Decisions 11 / 14 / 16: the review-hold family (date / line breaks / glued words)
            # needs the same re-assertion after the MT/TM status reset above.
            if outcome.review_hold:
                seg.status = SegStatus.NEEDS_REVIEW
            # ...and the SAME pre-existing wipe applies to the language-mismatch
            # (`nontranslation`) verdict, which predates BUG-06 and has no CommitOutcome
            # field — so it is read off the flag family the seam just wrote. A suspected
            # non-translation is exactly the class that must never ship as clean MT.
            # (meta_commentary rides this same family with its own reason, so the two
            # checks overlap harmlessly — both only ever set the same status.)
            # ONLY a flag THIS commit wrote routes: see the `_nt_before` snapshot above.
            # A stale flag from an earlier run describes text this run just replaced, and
            # honouring it would make NEEDS_REVIEW permanent (and the segment permanently
            # undeliverable) no matter how good the new translation is.
            if any(
                isinstance(f, dict)
                and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
                and id(f) not in _nt_before
                for f in (seg.qa_flags or [])
            ):
                seg.status = SegStatus.NEEDS_REVIEW
            # LQA safety floor (C): a step edit this run REVERTED. Mirror the DNT /
            # glossary / tag re-assertions above — the committed text is the PRE-fix
            # target the LQA had already judged (the guard kept it deliberately), but a
            # human must see that an automated improvement was discarded and why, so the
            # segment carries a `fix_reverted` qa_flag naming the step and the broken
            # term(s) and is routed to review. merge_flags composes with every other
            # family (the commit seam's dnt/glossary/tag flags survive).
            if j in fix_guard_seg:
                seg.qa_flags = lqa_safety.merge_flags(seg.qa_flags, fix_guard_seg[j])
                seg.status = SegStatus.NEEDS_REVIEW
            # GitHub #45: a segment whose step call FAILED while its siblings succeeded.
            # The text committed above is its PRE-STEP translation (the run deliberately
            # kept it rather than discarding the whole locale), so a human must see that
            # the improvement this step promised never happened, and why. Mirrors the
            # DNT / glossary / tag / fix-guard re-assertions: the MT/TM status lines
            # above RESET status to MT, so review is re-applied here.
            if j in seg_step_error:
                _st, _msg = seg_step_error[j]
                seg.qa_flags = quality.merge_step_error_flags(
                    seg.qa_flags, [quality.step_error_flag(_st, _msg)]
                )
                seg.status = SegStatus.NEEDS_REVIEW
            elif any(
                isinstance(f, dict) and f.get("type") == quality.STEP_ERROR_FLAG_TYPE
                for f in (seg.qa_flags or [])
            ):
                # ...and the STALE-BADGE half of the same rule (adversarial review P4),
                # the defect `_nt_before` above documents for `nontranslation` and
                # `clear_tag_flags` / `clear_number_flags` close for their own families:
                # nothing ever CLEARED this one. A step_error flag left by an EARLIER run
                # describes text THIS run just replaced, and the re-assertion above would
                # keep re-routing the segment to NEEDS_REVIEW — permanently undeliverable
                # — no matter how clean the new translation is. This run reached the
                # write-back and committed a target for this segment WITHOUT a step
                # failure, so the old verdict is about a string that no longer exists.
                # Guarded by the `any(...)` so a segment that never carried the flag has
                # its qa_flags column left untouched (byte-identical for every run that
                # has never degraded).
                seg.qa_flags = quality.clear_step_error_flags(seg.qa_flags)
            # ISSUE #95 (gate C round 1, H1) -- THE GATE HOLD, on the segment.
            # This run's correction loop ENDED with its gate's standing verdict still
            # FAILED. In a classic workflow the write-back IS the delivery -- there is no
            # downstream deliver step to withhold -- so "the run holds" has to mean the
            # TEXT does not ship as finished work: every unit this run produced carries a
            # CRITICAL gate-hold badge, lands NEEDS_REVIEW, and `export.gate_held_reason`
            # refuses the document export (409, not bypassable with allow_held) until a
            # reviewer approves or edits it. Placed last among the status re-assertions,
            # after the MT/TM status lines have reset status to MT, exactly like the
            # step_error rule above. The `elif` is that rule's stale-badge half -- but a
            # gate hold's stale rule is STRICTER than a step error's (round 2, H1): only a
            # later run whose OWN gate PASSED over this unit's new text may clear it, so a
            # re-run with a failing (or absent) check can no longer release gate-failed
            # text in one click. A held document is still never permanently undeliverable:
            # a reviewer approval releases it, and so does a genuinely clean run.
            if _gate_hold:
                seg.qa_flags = quality.merge_gate_hold_flags(
                    seg.qa_flags,
                    [
                        quality.gate_hold_flag(
                            _gate_hold.get("step_name") or "",
                            _gate_hold.get("message") or "",
                            _gate_hold.get("cause") or "",
                            _gate_hold.get("family") or "",
                        )
                    ],
                )
                seg.status = SegStatus.NEEDS_REVIEW
            elif _gate_release[j] and quality.has_gate_hold_flag(seg.qa_flags):
                seg.qa_flags = quality.clear_gate_hold_flags(seg.qa_flags)
            elif quality.has_gate_hold_flag(seg.qa_flags) and seg.status not in (
                SegStatus.APPROVED,
                SegStatus.EDITED,
            ):
                # The badge SURVIVED this run (no gate of ours passed this text). Keep the
                # unit's status consistent with the badge it still wears -- otherwise a
                # gate-less re-run would leave a CRITICAL badge on a row reading clean
                # `mt`, which is the same lie in a smaller font.
                # ROUND 3, H1: never over a HUMAN-AUTHORED disposition. Today a human's
                # APPROVED/EDITED slot has already `continue`d out above (the status-
                # protection skip, and the seam's freshness backstop), so this is
                # defense in depth on the twin of the FULL_TEXT arm where the same
                # re-assertion DID reach a protected body and destroyed it.
                seg.status = SegStatus.NEEDS_REVIEW
            # Q6/F6 — DECIDE TM-LOCK ELIGIBILITY, once, now that every status re-assertion
            # above has run. POLICY (fix round): lock ONLY a fill whose SOLE divergence
            # from clean-MT status is a SOFT glossary/dnt adherence flag. Concretely a
            # lockable fill is a tm_origin segment the seam CLEANLY committed as
            # authoritative content (committed AND tm_eligible — tm_eligible already
            # excludes every poisoned hit: nontranslation/tag/number/meta/length/placeholder
            # force it False, and a refused/hard-lock-refused fill has committed False),
            # MINUS the classes the old `status == MT` gate excluded but a bare
            # `committed and tm_eligible` would silently re-admit:
            #   * char_limit_violation — an over-limit TM fill must stay UNLOCKED so a
            #     reviewer can SHORTEN it (char_limit is soft and leaves tm_eligible True);
            #   * whitespace_violation — soft + tm-neutral like char_limit, excluded for the
            #     same reason (a tm_origin fill never sets it: lang_guard is off for TM);
            #   * a per-segment step error (seg_step_error);
            #   * a reverted fixer edit (fix_guard_seg).
            #   * a QE-gate FAIL — belt-and-suspenders: a QE-scored segment was re-run by a
            #     provider call, which already flipped tm_origin False, so the tm_origin
            #     term below excludes it anyway; the `card` check keeps the predicate honest
            #     to the policy and robust to a future refactor that scored a TM fill.
            # These keep their old unlocked behaviour. dnt/glossary flags are the ONLY
            # divergences that still lock, which is exactly the Q6 policy ("the flag RIDES
            # ALONGSIDE the lock, it does not gate it"). `card` is this segment's QE result
            # (set unconditionally above).
            if (
                tm_origin[j]
                and outcome.committed
                and outcome.tm_eligible
                and not outcome.char_limit_violation
                and not outcome.whitespace_violation
                and not (card is not None and card.get("passed") is False)
                and j not in seg_step_error
                and j not in fix_guard_seg
                # #95: a gate-held unit is never authoritative content -- it is text a
                # judge FAILED and no loop repaired, so it must not become a TM lock.
                and not _gate_hold
            ):
                tm_lock_content_ok.add(j)
            session.add(seg)

    # PROPOSAL-tm-matching-workflow-step (§5/§8): apply the `tm_match` step's LOCKS,
    # AFTER the single write-back committed each target from working[] (so the write
    # seam never saw a locked segment mid-run and never refused its own fill). Lock ONLY
    # a CLEANLY committed TM fill: the write-back routes a poisoned/withheld 100% hit to
    # NEEDS_REVIEW (or it is in refused_seg), and such a segment must NEVER become a lock
    # (a hedge/length-runaway is not authoritative content). Inert on any run with no
    # tm_match step.
    #
    # Q6/F6 (core-QA fix train 1): gate on `tm_lock_content_ok` (computed above), NOT on
    # `status == MT`. This is NOT a strict superset of the old status gate -- it corrects
    # it in BOTH directions:
    #   * it ADDS the fills the old gate wrongly demoted: a soft glossary/dnt flag re-routes
    #     a byte-exact TM fill to NEEDS_REVIEW while leaving it tm_eligible, and the live
    #     campaign proved the old `status != MT` skip stripped the band off otherwise-
    #     trusted content (deterministically, at strings-file scale);
    #   * it REMOVES fills the old gate wrongly ADMITTED: (a) a TM fill a correction-loop
    #     step REWROTE flips tm_origin False, yet its rewritten text could land back at
    #     status==MT -- the old gate then stamped band + tm_id PROVENANCE onto LLM-authored
    #     text; the tm_origin term of the eligibility predicate now excludes it (F10); and
    #     (b) an over-limit / QE-failed / step-errored / fix-reverted fill, which
    #     `committed and tm_eligible` alone would re-admit -- the eligibility predicate
    #     subtracts all four (F6). The "never lock a hedge" protection is fully preserved.
    # A `_vr_skip` (vendor/external RETURN) slot is ALSO never in `tm_lock_content_ok`: the
    # write-back loop `continue`s before the eligibility decision for it, so a slot whose
    # authoritative content was already decided by a guarded vendor write seam can never be
    # freshly stamped with a TM band here -- it keeps whatever lock that admission left.
    if tm_lock_plan and not any_step_failed:
        from app.services import tm_lock

        for j, seg in enumerate(segments):
            plan = tm_lock_plan.get(j)
            if plan is None or j not in tm_lock_content_ok:
                continue
            # Batch 15 T1 (LAW-TM-12): the plan is a 4-tuple (band, hard, tm_id, match_pct);
            # the pct rode the terminal seg.tm_match_pct stamp already (tm_fill_pct), so only
            # the lock provenance is read here.
            band, hard, tm_id = plan[0], plan[1], plan[2]
            # R3 POLICY (fix round): a FLAGGED fill NEVER hard-locks. A hard lock is for
            # CLEAN, trusted fills; when the plan says hard=True (tm_hundreds_enter_lqa off)
            # but the commit outcome carried a dnt/glossary violation, stamp the SOFT band
            # instead. The protection is retained (the segment is already NEEDS_REVIEW and,
            # as a soft lock, is still excluded from a normal translate step next run and
            # routed to review), but a SOFT lock keeps MACHINE REPAIR possible after the
            # review -- a correction-loop fix step may edit a soft lock (D3), whereas a HARD
            # lock would freeze the flagged fill as unfixable by any machine path forever.
            if hard and j in tm_lock_flagged:
                hard = False
            tm_lock._stamp_lock(seg, band=band, hard=hard, tm_id=tm_id, entry_id=tm_id)
            session.add(seg)
            # LOW-12: the tm_match FILL already emitted this key value's `updated` Delta
            # row (the key change-log hook fires on the write-back commit), so the lock
            # apply rides that row -- emitting a SECOND one here was a duplicate. A Delta
            # consumer re-fetches value state (which carries the lock) on the fill's row.
        session.commit()

    # LOW-1 (run-cancel train): CAS the terminal flip -- only flip a run still
    # running/pending, so a cancel (or recovery sweep) that landed while this thread was
    # finishing is never overwritten with COMPLETED. The per-segment content committed
    # incrementally above stands (as `_abort_if_superseded` documents); only the terminal
    # status is guarded here.
    if tags_only_ct:
        # #705: how many no-text segments this run delivered by copy (no step touched them).
        run.context = {**(run.context or {}), "tags_only": tags_only_ct}
    _stamp_tag_repair_spend(session, run, _tag_spend)
    session.commit()
    _tag_spend[3] = True
    if not _commit_run_terminal(session, run, RunStatus.COMPLETED):
        yield {
            "type": "error",
            "run_id": run.id,
            "error": "This run was cancelled or superseded while it was finishing; its "
            "terminal write-back was discarded.",
        }
        return
    _mark_project_completed(session, doc_id)
    # F5: joined from `emitted`, not `working` — a refused segment's slot was blanked
    # above so its raw hedge never appears in the terminal event even though the run
    # otherwise succeeded (the DB write-back withheld it the same way).
    done_ev = {"type": "done", "run_id": run.id, "text": SEG_JOINER.join(emitted)}
    # QE gate: attach the run-level tally to the terminal event ONLY when the gate ran
    # for at least one segment, so an off run's 'done' shape is byte-identical.
    if any(qe_run_stats.values()):
        done_ev["qe"] = dict(qe_run_stats)
    # Batch 14 T2 (SPEC §6.3/§13): the run-level image-context rollup, attached ONLY when
    # a skip actually fired (a run with no image skip keeps a byte-identical `done` shape).
    # `skipped` is 1 once-per-run when some eligible segment's images were resolved but no
    # target sent them (LAW-IMG-4); an opted-out step contributes nothing (§25.4).
    if img_skip_run_ct:
        done_ev["image_context"] = {"skipped": img_skip_run_ct}
    # #705: attached only when a tags-only copy landed (byte-identical `done` otherwise).
    if tags_only_ct:
        done_ev["tags_only"] = tags_only_ct
    # LQA safety floor: the run-level rollup, attached ONLY when a guard acted (an
    # untouched run's `done` shape is byte-identical), mirroring how qe_run_stats rides.
    # STATUS PROTECTION: total segments shielded from a machine overwrite because a human
    # APPROVED or EDITED them -- the ones excluded from dispatch up front PLUS any caught mid-
    # run by the seam's freshness backstop (disjoint sets). Surfaced so the protection is
    # visible in the run summary, never silent (silent skipping is the bug this fix removes).
    _sp_total = len(status_protected) + status_protected_ct
    # LOW-2: surface a parked-blob misalignment ONLY when it could actually have swallowed a
    # pause-edit -- i.e. there were protected slots to shield. A run with no protected slots is
    # byte-identical.
    _misaligned_note = _parked_misaligned and bool(status_protected)
    _lqa_excluded = (run.context or {}).get("lqa_tm_excluded", 0)
    if (
        any(safety_run_stats.values())
        or _lqa_excluded
        or tm_lock_refused_ct
        or _sp_total
        or _misaligned_note
        or operator_edited_landed_ct
    ):
        _ls = dict(safety_run_stats)
        if _lqa_excluded:
            _ls["lqa_tm_excluded"] = _lqa_excluded
        # item 9: attach the hard-lock-refusal disposition to the rollup ONLY when it
        # fired, so a run with no hard locks keeps a byte-identical `lqa_safety` shape.
        if tm_lock_refused_ct:
            _ls["tm_lock_refused"] = tm_lock_refused_ct
        if _sp_total:
            _ls["status_protected"] = _sp_total
        # PART 1: operator pause-edits that landed EDITED (protected) this run. Attached ONLY
        # when it fired, so a run with no pause-edit keeps a byte-identical `lqa_safety` shape.
        if operator_edited_landed_ct:
            _ls["operator_edited"] = operator_edited_landed_ct
        if _misaligned_note:
            # the parked review blob no longer aligned to the segment set, so a pause-edit to a
            # protected slot could not be classified and was NOT applied -- surfaced, not silent.
            _ls["parked_blob_misaligned"] = 1
        done_ev["lqa_safety"] = _ls
    yield done_ev


def run_workflow(
    session: Session, workflow_id: int, doc_id: int, locale: str, created_by: str = ""
) -> dict:
    _refuse_launch_guards(session, workflow_id, session.get(Document, doc_id))
    # GitHub #213 part 2, the SYNCHRONOUS (local CLI / vendor) arm. This launcher creates
    # its run row without the same-workflow re-attach guard (pre-existing design: a
    # one-shot process cannot re-attach to anything), but it must not be the one launch
    # path through which a second workflow walks onto a segment set another workflow is
    # writing back to. THE CHECK AND THE CREATE ARE ONE CRITICAL SECTION (fix round 1):
    # the refusal AND the RUNNING-row insert (the batch-member pattern: the row is
    # pre-created here and handed to `run_workflow_stream` with `is_resume=False`) run
    # together under _RUN_CREATE_LOCK -- the lock every other launcher holds for its own
    # check-and-create -- so no launcher on any arm can pass its own check inside this
    # arm's old check-then-create window. Like every guard here it is PROCESS-LOCAL: a
    # separate local-CLI process beside the server is not serialized by this in-process
    # lock (the pre-existing limit, same as the same-workflow guard).
    # This caller drains the iterator without yielding to an external consumer.
    # Own one token through summary construction, including between inner events.
    # The capacity slot is taken BEFORE the row exists, exactly like `start_run` takes
    # its admission token first: a refusal -- capacity or conflict -- leaves no row.
    with _execution_slot() as slot:
        with _RUN_CREATE_LOCK:
            doc = session.get(Document, doc_id)
            # The refusals `run_workflow_stream` raises on its first advance (trash
            # document, AI opt-out), run EAGERLY here so they refuse before the row,
            # mirroring `_start_run_admitted`; the stream re-checks them as the
            # load-bearing seam. Read-only, so double-running them changes nothing.
            _refuse_key_trash_document(doc)
            from app.services import ai_optout as _ai_optout

            _ai_optout.refuse_workflow(session, workflow_id, doc)
            _refuse_overlapping_run(session, workflow_id, doc_id, locale)
            run = WorkflowRun(
                workflow_id=workflow_id,
                document_id=doc_id,
                target_locale=locale,
                created_by=created_by or "",
                status=RunStatus.RUNNING,
                current_step_index=0,
            )

            def _stage_run_insert():
                session.add(run)

            # The same launch-write seam `_start_run_admitted` uses: the INSERT rides the
            # bounded writer-lock retry, and keep_loaded=True keeps the PK populated
            # after the commit without a refresh() SELECT on the launch hot path.
            _db.commit_launch(
                session,
                stage=_stage_run_insert,
                keep_loaded=True,
                what=f"launch run (sync arm) for workflow {workflow_id} / document {doc_id} / {locale}",  # noqa: E501
            )
            run_id = run.id
        try:
            return _run_workflow_admitted(
                session, workflow_id, doc_id, locale, created_by, slot, run_id=run_id
            )
        except Exception as exc:
            # An exception escaping the stream after the pre-created row would otherwise
            # leave it RUNNING -- holding its (document, locale) against every other
            # launcher until the staleness floor sweeps it. The sibling executors' crash
            # handlers (`_run_in_background`, `_run_member`) mark the row FAILED for
            # exactly this reason; do the same, then let the caller see the error.
            # Best-effort: the rollback can itself lose the writer lock (a lock crash is
            # the very case this handler serves); it only needs to release the caller
            # session's lock before the bookkeeping write below, so a failure here costs
            # nothing -- the FAILED mark owns its own session.
            try:  # noqa: SIM105
                session.rollback()
            except Exception:  # noqa: S110 -- deliberate: a rollback that loses the lock only delays the bookkeeping write below, which retries
                pass
            # #337 N-1 (stamped why, mirroring the siblings): without a note the
            # row ends `failed` with reason null -- the reasonless shape #106 was
            # filed about. Same note selection as `_run_in_background`: retryable
            # write contention, pool exhaustion, or the bounded crash text. The
            # mark-FAILED write itself rides `_bookkeeping_session` + the bounded
            # writer-lock retry exactly like the sibling: under the very contention
            # that crashed this run, a plain commit on the caller's session would be
            # the next lock victim and leave the row RUNNING with no reason.
            try:
                with _bookkeeping_session() as s:
                    row = s.get(WorkflowRun, run_id)
                    if row is not None and row.status == RunStatus.RUNNING:
                        note = _crash_terminal_note(exc)

                        # Re-check the terminal status INSIDE the stage, not just once
                        # above: the retry's rollback + jittered backoff widens the
                        # check-then-act window, and an operator cancel can commit
                        # inside it -- never resurrect a cancelled run to FAILED.
                        def _stage(_run=row, _note=note):
                            if _run.status in (
                                RunStatus.COMPLETED,
                                RunStatus.PAUSED,
                                RunStatus.CANCELLED,
                            ):
                                return  # someone finished it first -- commit nothing
                            if _note not in (_run.notes or ""):
                                _stamp_note(_run, _note)
                            _run.status = RunStatus.FAILED
                            _run.finished_at = datetime.utcnow()
                            s.add(_run)

                        _db.commit_with_retry(s, stage=_stage)
            except Exception:
                log.exception("could not mark sync-arm run %s failed after a crash", run_id)
            raise


def _run_workflow_admitted(session, workflow_id, doc_id, locale, created_by, slot, run_id=None):
    # Archive gate (residual sweep): refuse LAUNCHING a run on an archived workflow /
    # project up front (before the stream / any WorkflowRun row), mirroring start_run's
    # eager guard. In-flight/resume paths are untouched (this is a fresh launch only).
    # `run_id`: the row `run_workflow` pre-created under `_RUN_CREATE_LOCK` (fix round 1
    # of #213 part 2); it is a FRESH leg, so `is_resume=False` -- same reasoning as
    # `_run_member`'s (rowid reuse must never adopt a deleted run's envelope).
    summary = {"run_id": None, "status": None, "steps": [], "text": ""}
    for ev in run_workflow_stream(
        session,
        workflow_id,
        doc_id,
        locale,
        run_id=run_id,
        is_resume=False,
        created_by=created_by,
        capacity_slot=slot,
    ):
        if ev["type"] == "run_started":
            summary["run_id"] = ev["run_id"]
        elif ev["type"] == "step":
            summary["steps"].append(ev)
            if "matches_offered" in ev.get("tm_match", {}):
                summary["tm_offers"] = ev["tm_match"]
        elif ev["type"] == "paused":
            summary["status"] = "paused"
            summary["run_id"] = ev["run_id"]
            summary["paused_text"] = ev["text"]
            summary["paused_step"] = ev["step_index"]
        elif ev["type"] == "done":
            summary["status"] = "completed"
            summary["text"] = ev["text"]
            if "qe" in ev:
                summary["qe"] = ev["qe"]  # run-level QE tally (gate on only)
            if "lqa_safety" in ev:
                # LQA safety floor rollup (reverted fixes / protected TM segments);
                # absent unless a guard acted, so an untouched run's summary is
                # byte-identical.
                summary["lqa_safety"] = ev["lqa_safety"]
        elif ev["type"] == "error":
            # A terminal failure (a failed step). DON'T drop it — the run is really
            # FAILED; surfacing it lets callers report ok:false / exit 1 instead of a
            # silent success-lie.
            summary["status"] = "failed"
            summary["error"] = ev.get("error")
        elif ev["type"] == "resume_rejected":
            # Issue #23 (b): a count-mismatch resume that left the run PAUSED (NOT
            # failed). Distinct status so callers report a clean, re-resumable refusal.
            summary["status"] = "rejected"
            summary["error"] = ev.get("error")
    return summary


def _claim_paused_run(session: Session, run_id: int) -> bool:
    """Flip a run PAUSED -> RUNNING, advance its cursor past the human step and restart its
    staleness clock, as ONE conditional UPDATE ... WHERE status = 'paused' (#578).

    `_RUN_CREATE_LOCK` serializes resumes inside one process only; a CLI resume in its own
    process racing the web server read the same PAUSED row and both advanced it. The database
    arbitrates here instead: exactly one UPDATE matches the row, and the loser sees rowcount 0
    and must refuse. Stages the UPDATE on `session`; the caller commits (through the launch
    retry) and treats False as 'a rival resumed it first'."""
    from sqlalchemy import update

    result = session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.status == RunStatus.PAUSED)
        .values(
            current_step_index=WorkflowRun.current_step_index + 1,
            status=RunStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def resume_workflow(session: Session, run_id: int, locale: str, edited_text: str) -> dict:
    with _execution_slot(refuse=False) as slot:
        return _resume_workflow_admitted(session, run_id, locale, edited_text, slot)


def _resume_workflow_admitted(session, run_id, locale, edited_text, slot) -> dict:
    """Resume a paused run: the edited text becomes the paused step's output, then
    the remaining steps run to completion."""
    # #505 item 2: the check-PAUSED-then-advance is taken under `_RUN_CREATE_LOCK` and flips
    # PAUSED -> RUNNING in the SAME commit, exactly like `resume_run_async`. It used to check
    # PAUSED outside the lock and only advance `current_step_index`, so an overlapping CLI and
    # web resume both read PAUSED: the segment set ran twice (double spend) and the second
    # advance skipped a step. The first resume now flips the row; the second reads a
    # non-PAUSED run under the lock and is refused. The blocking execution below runs AFTER
    # the lock releases, so real concurrency is unaffected.
    with _RUN_CREATE_LOCK:
        # populate_existing: another session may have flipped the row since this one loaded it.
        run = session.get(WorkflowRun, run_id, populate_existing=True)
        # NEW-ISSUE-2 (adversarial re-review): only a PAUSED run may be resumed. Without this
        # the synchronous CLI path (cmd_workflow `run resume`) advanced current_step_index and
        # executed a COMPLETED / RUNNING run with no guard at all -- and on this branch that
        # reaches the FT/segmented human_edit channel, letting a resume with arbitrary text
        # OVERWRITE an already-finished protected body. The async path (resume_run_async)
        # already has this guard; this brings the sync twin to parity. The vendor-rework flow
        # legitimately resumes a reopened run, but it reparks to PAUSED first
        # (repark_run_at_external_step), so it passes.
        if run is not None and run.target_locale:
            # Issue #493: resume with the RUN's own locale, never the caller's (an inferred
            # first-locale or a form field can name another target locale of the same
            # document, and the stream impls load and write segments by that locale).
            locale = run.target_locale
        if run is None or run.status != RunStatus.PAUSED:
            # LOW-3: carry the run's ACTUAL status so the CLI does not misreport a non-paused
            # refusal as "paused" (this branch fires precisely because it is NOT paused).
            return {
                "run_id": run_id,
                "status": "rejected",
                "run_status": (run.status if run is not None else None),
                "error": (
                    f"run is not paused (status {run.status}); only a paused run can be resumed"
                    if run is not None
                    else "run not found"
                ),
            }

        # flood R2-F1: the resume advance is a LAUNCH write; ride out a transient writer lock
        # with the bounded jittered retry. The SYNCHRONOUS (CLI/vendor) twin returns a dict
        # rather than raising, so on exhaustion surface the honest retryable contention as a
        # 'failed' summary (mirroring the not-paused / count-mismatch refusals). The run stays
        # PAUSED and re-resumable — commit_launch rolled back, nothing was advanced.
        # #578: the flip is a conditional UPDATE ... WHERE status = 'paused', so a resume in
        # ANOTHER process (the CLI beside the web server) cannot also win it.
        claimed = {"ok": False}

        def _stage_resume_advance():
            claimed["ok"] = _claim_paused_run(session, run_id)

        try:
            _db.commit_launch(
                session,
                stage=_stage_resume_advance,
                what=f"resume run {run_id} ({locale})",
            )
        except _db.WriteContentionError as exc:
            return {"run_id": run_id, "status": "failed", "error": str(exc)}
        if not claimed["ok"]:
            session.expire_all()
            now = session.get(WorkflowRun, run_id)
            current = now.status if now is not None else None
            return {
                "run_id": run_id,
                "status": "rejected",
                "run_status": current,
                "error": f"run is not paused (status {current}); only a paused run can be resumed",
            }
    summary = {"run_id": run_id, "status": None, "steps": [], "text": ""}
    for ev in run_workflow_stream(
        session,
        run.workflow_id,
        run.document_id,
        locale,
        run_id=run_id,
        seed_text=edited_text,
        capacity_slot=slot,
    ):
        if ev["type"] == "step":
            summary["steps"].append(ev)
            if "matches_offered" in ev.get("tm_match", {}):
                summary["tm_offers"] = ev["tm_match"]
        elif ev["type"] == "paused":
            summary["status"] = "paused"
            summary["paused_text"] = ev["text"]
            summary["paused_step"] = ev["step_index"]
        elif ev["type"] == "done":
            summary["status"] = "completed"
            summary["text"] = ev["text"]
            if "qe" in ev:
                summary["qe"] = ev["qe"]  # run-level QE tally (gate on only)
            if "lqa_safety" in ev:
                # LQA safety floor rollup (reverted fixes / protected TM segments);
                # absent unless a guard acted, so an untouched run's summary is
                # byte-identical.
                summary["lqa_safety"] = ev["lqa_safety"]
        elif ev["type"] == "error":
            # Same fix as run_workflow: a dropped "error" event left status=None,
            # which every consumer read as "not paused" -> "completed" (a silent
            # success-lie). Propagate it so resume reports the real failure.
            summary["status"] = "failed"
            summary["error"] = ev.get("error")
        elif ev["type"] == "resume_rejected":
            # Issue #23 (b): the edited text's segment count did not match; the run was
            # left PAUSED (re-resumable), not failed. Report a distinct 'rejected' status
            # so the caller surfaces a clean refusal against a still-paused run.
            summary["status"] = "rejected"
            summary["error"] = ev.get("error")
    persisted_run = session.get(WorkflowRun, run_id)
    if persisted_run and "tm_offers" in (persisted_run.context or {}):
        summary["tm_offers"] = persisted_run.context["tm_offers"]
    return summary


# --- background execution (POST starts; GET only observes) ---
#
# The workflow run is executed OFF the request thread on a dedicated daemon
# thread, writing WorkflowRun/StepRun state to the DB exactly as it did inline.
# A POST route calls start_run()/resume_run_async(); the SSE GET is now a
# read-only observer (observe_run) that polls those rows. This removes the
# CSRF-adjacent "GET executes a workflow" flaw and makes stream reconnects (or a
# second tab) safe — they attach to the SAME run and never spawn execution.


class RunCapacityError(Exception):
    """A workflow launch was REFUSED at the door because this process is already
    executing `config.MAX_CONCURRENT_RUNS` runs (GitHub #99/#100).

    HONEST + RETRYABLE, and the sibling of `db.WriteContentionError`: NO NEW WORK was
    written (the gate runs BEFORE the run row exists), so the caller should surface a
    503 with a `retry_after` hint and try again once the running work drains. Every
    surface renders it that way — /api, the web launch route, the CLI, MCP.

    "No new work" is the precise claim, not "no write at all" (gate A round 2, LOW-7):
    the refusal path may still FAIL a stale corpse it found while deciding
    (`_sweep_stale_at_capacity`, round 1's LOW-5). That write is bookkeeping about work
    that already ended, never a launch, and it is best-effort — it can never turn the
    clean 503 into a 500.

    THE POINT of refusing: a run pins one pooled DB connection for its ENTIRE duration
    (see `_work` below), so admitted-but-unservable runs do not queue politely — they
    exhaust the pool, crash on a connection checkout, and (before this train) could not
    even record WHY, leaving a RUNNING row frozen at step 0 forever. A door that says
    "not now" is strictly better than a door that lets everyone in and then jams."""

    def __init__(self, in_flight: int, cap: int, needed: int = 1, *, operation: str = "launch"):
        self.in_flight, self.cap, self.needed = in_flight, cap, max(1, int(needed))
        self.operation = operation
        self.retry_after = RUN_CAPACITY_RETRY_AFTER_S
        if operation != "launch":
            super().__init__(
                f"At capacity: the {operation} was refused because {in_flight} work item(s) "
                f"are already executing and this install admits {cap} at a time. "
                f"Wait about {self.retry_after}s and retry once the running work finishes."
            )
            return
        # `in_flight` is the count the RESERVATION observed, not a fresh read (gate A
        # round 1, LOW-6): re-reading it after the refusal produced messages like "6 runs
        # are already executing and this install admits 8", because a run ended in the
        # microseconds between. One snapshot, one sentence, no contradiction.
        #
        # ONE SLOT IS ALL ANYTHING NEEDS (gate A round 3, MED-4). Round 2's message asked
        # for `needed` FREE SLOTS because a fan-out reserved every slot up front; a fan-out
        # now QUEUES instead and is never refused at all, so this exception is only ever
        # raised for a SINGLE launch and "needs 72 free slots" was both unreachable and
        # false. The advice to raise LOC_TMS_MAX_CONCURRENT_RUNS is gone with it: the cap is
        # not why this launch was refused (one run executing is), and raising it is the
        # configuration that OOM-killed prod on 2026-09-06 (GitHub #109).
        super().__init__(
            f"At capacity: {in_flight} workflow run(s) are already executing and this "
            f"install admits {cap} at a time. Nothing was launched; retry once the running "
            f"work drains. A project-wide translate is never refused for capacity — it "
            f"queues and starts on its own as slots free. Nothing was spent. "
            f"Wait about {self.retry_after}s and try again."
        )


#: Seconds a refused launch is told to wait. A run is a minutes-long unit of work, so the
#: hint is deliberately coarser than the writer-lock one (db.LAUNCH_CONTENTION_RETRY_AFTER_S).
RUN_CAPACITY_RETRY_AFTER_S = 15


class RunConflictError(Exception):
    """GitHub #213 part 2: a launch was REFUSED because a run of a DIFFERENT workflow is
    already in flight on the same (document, locale) (prod: runs 237/wf 65 and 239/wf 72
    launched on doc 159 ja-JP within the same second; both wrote back to one segment set
    and the final text interleaved the two workflows' outputs).

    The sibling of `RunCapacityError` in spirit, the OPPOSITE in kind: capacity says
    "retry when the running work drains"; this says "retrying changes nothing -- the run
    you would join is not yours, cancel it first". Nothing was written (the check is
    read-only and runs before the first write), so every surface renders the SAME refusal
    sentence: /api answers 409 with `{"error": ..., "code": "run_conflict", "run_id": N}`,
    the web shows the themed error page, the CLI exits non-zero with the message (local
    and --remote, same text), MCP gets the API's 409 body through its thin client.

    The SAME-workflow double-submit is NOT this: that is the genuine double-POST and it
    still RE-ATTACHES (unchanged). Only a different workflow on the segment set refuses."""

    #: Stable machine code on every surface (the 409 body's `code`).
    code = "run_conflict"

    def __init__(self, run_id: int, workflow_name: str, document_id: int, locale: str):
        self.run_id, self.workflow_name = run_id, workflow_name
        self.document_id, self.locale = document_id, locale
        super().__init__(
            f'Refused: run #{run_id} (workflow "{workflow_name}") is already running on '
            f"this document for {locale}. Only one run at a time may write to a document + "
            f"locale, whatever the workflow, and nothing was launched. Cancel run #{run_id} "
            f"first (CLI: workflow cancel-run {run_id}; web: the Cancel button on its run "
            f"page) and launch again."
        )


def _overlapping_run(session, workflow_id: int, doc_id: int, locale: str):
    """The in-flight run of a DIFFERENT workflow on this (document, locale), as
    `(run, workflow_name)`, or None. GitHub #213 part 2.

    Same read the same-workflow double-submit guard does, with the workflow equality
    flipped to `!=`: a same-workflow row has something to re-attach to, a different-
    workflow row would spawn a second execution over ONE segment set, so it is refused
    (owner decision 2026-09-28: refuse, not queue, not serialise).

    The staleness floor's three refusals apply here too: a rival row is skipped only when
    it is a non-batch RUNNING row with no live thread in this process and no sign of life
    past STALE_RUN_AFTER (`_is_stale_run` is itself only ever True for RUNNING, so PAUSED
    and PENDING/QUEUED rivals always hold the door). A live-thread rival IS a conflict --
    its thread really writes -- so it is never skipped.

    Callers hold `_RUN_CREATE_LOCK` (the same lock that makes the check-and-create atomic
    against every other launcher: `start_run`, `queue_runs`, the batch dispatcher)."""
    rows = session.exec(
        select(WorkflowRun)
        .where(
            WorkflowRun.document_id == doc_id,
            WorkflowRun.target_locale == locale,
            WorkflowRun.workflow_id != workflow_id,
            WorkflowRun.status.in_(_IN_FLIGHT_RUN_STATUSES),
        )
        .order_by(WorkflowRun.id.desc())
    ).all()
    for run in rows:
        if run.batch_id is None and not _run_thread_alive(run.id) and _is_stale_run(session, run):
            continue
        wf = session.get(Workflow, run.workflow_id)
        return run, (wf.name if wf is not None else f"workflow {run.workflow_id}")
    return None


def _refuse_overlapping_run(session, workflow_id: int, doc_id: int, locale: str) -> None:
    """Raise `RunConflictError` when a DIFFERENT workflow has a run in flight on this
    (document, locale). THE ONE GUARD, reached from every launch decision:
    `_start_run_admitted` (single runs on web//api/CLI --remote/MCP), `queue_runs` (the
    keys fan-out) and `run_workflow` (the synchronous CLI arm). Callers hold
    `_RUN_CREATE_LOCK` around this and their own create."""
    found = _overlapping_run(session, workflow_id, doc_id, locale)
    if found is not None:
        rival, wf_name = found
        raise RunConflictError(rival.id, wf_name, doc_id, locale)


class _SlotToken:
    """OWNERSHIP of exactly one `RUN_SLOTS` reservation (gate A round 1, HIGH-2).

    The first round counted with a bare integer and released with `max(0, n-1)`, and the
    gate found the hole that leaves: when `Thread.start()` raised, the callee released the
    slot AND the caller's `finally` released it again, so a second, LIVE run's slot was
    silently stolen — `in_flight` measured 1 -> 0 with a real run executing. The clamp
    hides it (the count never goes negative, it just stops being true), and the trigger is
    `RuntimeError: can't start new thread`, i.e. exactly the thread/memory exhaustion the
    incident ended in. Sustained, that pins `in_flight` near 0 and defeats the gate
    completely, at the moment it is the only thing holding the install together.

    So a reservation is an OBJECT with two properties instead of an arithmetic operation:

      * `release()` is IDEMPOTENT — a second call on the same token is a no-op, not
        another decrement. Nothing can double-release.
      * `handed_off` is the transfer flag, set by `_run_in_background` at ENTRY, before
        anything in it can fail. A caller releases only a token it still owns, so nothing
        can release a slot out from under a running execution either.

    Both directions matter: an under-count disables the gate, an over-count permanently
    shrinks the install's capacity."""

    __slots__ = ("_lock", "_released", "_slots", "handed_off")

    def __init__(self, slots: "_RunSlots") -> None:
        self._slots = slots
        self._lock = threading.Lock()
        self._released = False
        #: True once the execution thread's owner has taken responsibility for releasing.
        self.handed_off = False

    def release(self, *, wake=False) -> bool:
        """Give the slot back. Returns True if THIS call did it (False = already gone)."""
        with self._lock:
            if self._released:
                return False
            self._released = True
        self._slots._give_back(wake=wake or self.handed_off)
        return True

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released


#: How long the admission scheduler parks on the slot Condition before re-reading the
#: queue. It is woken the instant any run ends (`_give_back` notifies) and the instant new
#: work is queued, so this is only the "nothing happened at all" backstop; a background
#: daemon waiting here blocks nobody.
QUEUE_SLOT_WAIT_S = 30.0

#: How often the admission scheduler re-reads the queue with nothing else waking it. The
#: DB is the queue, so a row inserted by ANOTHER process (a CLI fan-out against the same
#: install) is picked up within this interval even though nothing notified this one.
QUEUE_POLL_S = 5.0

#: Bound the startup adoption count used for logging. Admission itself selects groups
#: in SQL, so this limit never excludes later fan-outs from the rotation.
QUEUE_SCAN_LIMIT = 500

#: How long a `wait=True` CLI fan-out waits for its QUEUED rows to be admitted before it
#: stops waiting and REPORTS them as still queued (gate A round 3, LOW-2). Round 2 joined an
#: in-memory drainer with no timeout at all while each pending launch could wait an hour, so
#: `loc keys translate --wait` on a busy install could block for (pairs x 1h). Bounded for
#: the WHOLE queue, not per item — and giving up here loses nothing now: the rows are durable,
#: so the work continues on the server and the operator is told which pairs are still waiting.
QUEUE_DRAIN_WAIT_S = config.QUEUE_DRAIN_WAIT_S

#: THE FAIRNESS WINDOW (gate A round 3, HIGH-3). The scheduler is parked ON the slot
#: Condition, so without this it wins every slot the instant a run ends — measured: one
#: 6-pair fan-out on a cap-1 install refused 25 of 25 interactive launches, i.e. one
#: "translate all" made every other launch on the install 503 for hours. So the scheduler
#: YIELDS: after an interactive launch has been refused, the next free slot is left free for
#: this long before the queue may take it. An arriving single launch (a non-blocking
#: take-if-free) gets it; if nobody comes, the queue takes it and nothing is lost but this
#: interval. Short enough to be invisible against a run, long enough that a human's click or
#: a client's retry lands inside it.
QUEUE_YIELD_S = 0.25


class _RunSlots:
    """The in-flight run counter behind `RunCapacityError`.

    Counts EXECUTIONS, not launch requests: a slot is taken before a run's execution
    begins and released in its `finally`, so the number tracks what is actually holding
    pool connections right now.

    A FAN-OUT IS NEVER UNSATISFIABLE (gate A round 2, HIGH-1). Round 1 exempted the key
    fan-out and batches from the gate; round 2 made them all-or-nothing, which turned "more
    (locale x document) pairs than the run ceiling" into a PERMANENT refusal — a 72-run
    "translate all" on a cap-8 install failed at 3am on a completely idle machine, and the
    remedy the refusal named (raise the cap to 72) is the configuration that OOM-killed
    prod. Capacity is about WHEN work runs, never about whether it may run at all. So a
    fan-out is a DURABLE queue (round 3, HIGH-1/2/3 — round 2's in-memory one was invisible,
    undroppable-but-droppable and lost on restart):

      * a single launch takes ONE slot and is refused past the cap;
      * keys and batch fan-outs persist ALL their rows as QUEUED before execution;
        the shared scheduler takes one slot per admission, rotates across groups, and
        reserves interactive capacity. Batch width and spend/fail-fast gates still apply;
      * a fan-out is NEVER refused for capacity — capacity decides when work runs, never
        whether it may. Only a SINGLE launch can be refused, and only when nothing is free;
      * an Advanced mission takes one slot for its synchronous run and is refused with the
        same 503;
      * a re-attach and a resume of an already-counted run take nothing.

    Taking the slot before any write is what makes the gate honest under a launch burst:
    without it, N simultaneous launches would all read "under the cap" and all be admitted
    (the same check-then-act `_RUN_CREATE_LOCK` closes for run creation).

    Children do not take additional run slots: MT and Advanced delegates are bounded by
    config.RUN_WORKERS under their parent's lifetime and included in memory_estimate_mb.
    Their pooled sessions still need DB_REQUEST_HEADROOM. The historical two-connections
    planning factor is not a hard quota on each run. Provider
    stream pumps have their own STREAM_WORKERS ceiling in the memory estimate;
    evaluations, direct LLMemory and direct job/report/export work share this
    counter through maintenance.admitted_work. Scheduled children retain their
    modeled maintenance/JOB_WORKERS allowance."""

    def __init__(self) -> None:
        #: A Condition, not a Lock: `_give_back` must WAKE the admission scheduler the
        #: instant a run ends, or the queue would be a poll loop.
        self._lock = threading.Condition()
        self._n = 0
        #: Interactive launches refused since the queue last yielded a slot (HIGH-3).
        self._refused_since_yield = 0
        #: Monotonic deadline of a fairness window the queue must not take a slot in.
        self._yield_until = 0.0

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._n

    def acquire(
        self, timeout: float = 0.0, *, count_refusal: bool = True
    ) -> tuple[_SlotToken | None, int]:
        """Take ONE slot for an INTERACTIVE launch, waiting up to `timeout` for one to free.

        Returns `(token, in_flight_observed)`; `token is None` means the wait expired with
        the install still at its ceiling. The observed count is returned rather than
        re-read later so a refusal message and the decision describe the same instant
        (round 1, LOW-6). `timeout=0` is the non-blocking take-if-free.

        A refusal here is REMEMBERED (`_refused_since_yield`), and that is the whole of the
        fan-out fairness rule: it tells the admission scheduler that a person or a client is
        being turned away, so the next slot to free is left free for one `QUEUE_YIELD_S`
        window before the queue may take it. See `acquire_for_queue`.
        Direct non-launch work passes `count_refusal=False` to preserve that signal
        exclusively for interactive launches while enforcing the same ceiling."""
        cap = int(getattr(config, "MAX_CONCURRENT_RUNS", 0) or 0)
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            while True:
                observed = self._n
                if not cap or observed < cap:
                    self._n += 1
                    return _SlotToken(self), observed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if count_refusal:
                        self._refused_since_yield += 1
                    return None, observed
                self._lock.wait(remaining)

    def acquire_for_queue(self, timeout: float = 0.0) -> "_SlotToken | None":
        """Take ONE slot FOR THE ADMISSION SCHEDULER, yielding to interactive launches.

        Identical to `acquire` except for the fairness window. The scheduler is a daemon
        parked on this Condition, so it is always the first thing `notify_all` wakes when a
        run ends, while an arriving single launch has to take the lock cold — measured
        before this rule: 0 of 25 interactive launches admitted while a 6-pair fan-out
        drained a cap-1 install, i.e. one "translate all" 503-ing the whole install for the
        length of the fan-out.

        So when an interactive launch has been refused since the last yield, the queue does
        NOT take the slot that just freed: it opens a `QUEUE_YIELD_S` window, waits it out
        with the slot free, and re-reads. A launch arriving in the window takes it (that is
        the point); if none comes, the queue takes it on the next pass and the only cost is
        the window. NEVER refuses interactive launches back: the queue's own work is bounded
        by the same cap, so a yield can at worst delay it, never strand it — and it cannot
        deadlock on a cap-1 install, because a yield with nobody waiting simply expires.

        Returns the token, or None when the timeout expired at capacity (the caller re-reads
        its queue and asks again)."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            while True:
                cap = int(getattr(config, "MAX_CONCURRENT_RUNS", 0) or 0)
                now = time.monotonic()
                remaining = deadline - now
                # Keep one slot for interactive work on a multi-slot install.
                # A cap of one uses the timed yield below so queued work can progress.
                queue_cap = max(1, cap - 1)
                if not cap or self._n < queue_cap:
                    if now < self._yield_until:
                        # A fairness window is open: leave the slot free for it.
                        self._lock.wait(min(self._yield_until - now, max(0.0, remaining)))
                        if time.monotonic() >= deadline:
                            return None
                        continue
                    if self._refused_since_yield:
                        self._refused_since_yield = 0
                        self._yield_until = now + QUEUE_YIELD_S
                        continue
                    self._n += 1
                    return _SlotToken(self)
                if remaining <= 0:
                    return None
                self._lock.wait(remaining)

    def acquire_many(self, n: int) -> tuple[list[_SlotToken], int]:
        """Take UP TO `n` slots for a fan-out — whatever is free RIGHT NOW, never waiting.

        It grabs at most what the plan will actually run (a 1-locale batch must not hold
        `max_parallel` slots with the rest idle, round 2 MED-5), and returns immediately.
        An EMPTY list is no longer a refusal (gate A round 3, HIGH-1/2/3): it means "this
        install has nothing free this instant", and every caller answers it by QUEUING the
        work as durable rows instead. A short list means "launch these now, queue the rest".

        NO WAIT, on purpose (round 3, MED-3). The 2.0s `wait_first` this used to take was a
        blocking wait on a REQUEST thread that was already holding a checked-out pooled
        connection — measured at 2.01s with 1 connection held — i.e. the exact accumulator
        this train exists to remove, on the launch path, 20 of them at once under the
        train's own burst arm. It existed only so a launch arriving microseconds after a run
        ended was not refused for nothing, and nothing is refused for capacity any more.

        A refusal here is NOT counted against the queue's fairness window: a fan-out that
        finds the install full queues its work rather than being turned away, so it is not
        the population `acquire`'s counter exists to protect.

        Returns `(tokens, in_flight_observed)`."""
        cap = int(getattr(config, "MAX_CONCURRENT_RUNS", 0) or 0)
        n = max(1, int(n))
        with self._lock:
            observed = self._n
            free = n if not cap else max(0, min(n, cap - observed))
            if free:
                self._n += free
                return [_SlotToken(self) for _ in range(free)], observed
            return [], observed

    def take(self, n: int = 1) -> list[_SlotToken]:
        """Take `n` slots UNCONDITIONALLY — for an execution that is already counted
        elsewhere or must not be refused at this seam (a resume of a paused run). It still
        counts, so a launch that CAN be refused sees the true load."""
        n = max(1, int(n))
        with self._lock:
            self._n += n
        return [_SlotToken(self) for _ in range(n)]

    def _give_back(self, *, wake=True) -> None:
        """Only `_SlotToken.release` calls this, and only once per token."""
        with self._lock:
            self._n = max(0, self._n - 1)
            # Wake every waiter: a fan-out drainer queued behind this run may now proceed.
            self._lock.notify_all()
        if wake:
            ADMISSION.notify()


RUN_SLOTS = _RunSlots()


def _release_all(tokens) -> None:
    """Release every token still held (idempotent per token)."""
    for tok in tokens or ():
        tok.release()


@contextmanager
def fanout_slot(token: "_SlotToken"):
    """OWN a fan-out's pre-paid slot for exactly ONE launch iteration (gate A round 2,
    HIGH-2). Every fan-out call site wraps its `start_run` in this.

    THE BUG THIS EXISTS TO MAKE IMPOSSIBLE: the loops used to `slots.pop()` the token and
    then call `start_run(..., slot=tok)`. Between those two statements the token was owned
    by NOBODY — it was out of the pending list the loop's `finally` walks, `start_run`
    deliberately does not release a caller-supplied slot, and `_run_in_background` had not
    run so `handed_off` was still False. So any exception from `start_run` leaked the slot
    for the life of the process. The trigger is not exotic: `db.commit_launch` raises
    `WriteContentionError` by DESIGN when the SQLite writer lock never clears, which is a
    documented-normal outcome under exactly the concurrent-launch pressure this train is
    about. Measured before this fix: `in_flight` 3 -> 1 after one aborted fan-out, and
    cumulatively eight such events pin a default install at `in_flight == 8` with nothing
    executing and every launch 503-ing forever.

    THE RULE: a token is owned by exactly one party at every instant. The fan-out loop owns
    it from `pop()` until `_run_in_background` sets `handed_off` (which it does on its very
    first line, before anything in it can raise), and this `finally` covers EVERY exit path
    — a re-attach (which starts no execution), a `RunCapacityError`, a
    `WriteContentionError`, a `RuntimeError: can't start new thread`, a KeyboardInterrupt.
    Release is idempotent, so covering a path twice is free; leaving one uncovered is not."""
    try:
        yield token
    finally:
        if not token.handed_off:
            token.release()


def _rollback_quietly(session) -> None:
    """Best-effort `session.rollback()` that never raises (a failed rollback leaves nothing
    further to salvage; the caller is already on an error path)."""
    try:
        session.rollback()
    except Exception:
        log.debug("rollback after a failed commit also failed", exc_info=True)


def queue_runs(
    session: Session,
    plan,
    *,
    created_by: str = "",
    group: str | None = None,
    start_scheduler: bool = True,
) -> dict:
    """Create a DURABLE, VISIBLE QUEUED run row for every planned fan-out member
    before execution (gate A round 3, HIGH-1/2/3). Returns
    `{"group", "queued": [...], "attached": [...], "failed": [...], "conflicts": [...]}`
    (`conflicts`: GitHub #213 part 2, a different workflow already runs on that
    document + locale; the pair is reported, never queued, never raised fan-out-wide).

    `plan` is `[(workflow_id, document_id, locale, label)]` — `label` is the document name
    the surfaces show beside the row.

    WHY A ROW AND NOT A LIST. Round 2 kept the overflow of a fan-out in an in-memory
    `FanoutQueue` object, and the gate found what that costs on every axis at once: the
    queued pairs were invisible on /api, MCP, `--remote` and the audit trail (a 5-pair
    "translate all" answered `{"ok": true, "launched": [1,2]}` with an audit row reading
    `launched=2 skipped=0`); a queued launch that RAISED — on `WriteContentionError`, which
    this train's own documentation calls a normal outcome under launch pressure — was
    dropped in silence, leaving 1 run row of 3 and two locales that existed only in a log
    line; the object was unreachable from any surface, so the work could be neither seen nor
    cancelled; and a restart lost it with no record, on a deployment whose own docs call
    redeploys and machine auto-stop normal.

    A row fixes all four AT ONCE, because every one of those surfaces already reads run
    rows: `/api/runs/{id}` and the run lists, the web pages, `workflow show-run`, MCP
    `work_status`, the batch matrix, `cancel_run`, and the boot reconcile. The queue needs no
    surface of its own — it needs to stop being invisible to the ones that exist.

    THE ROW HOLDS NOTHING. No capacity slot (`RUN_SLOTS` counts EXECUTIONS) and no pooled
    connection: it is a row, waiting. `ADMISSION` — one daemon per process — flips it to
    RUNNING and starts it as slots free.

    THE DOUBLE-SUBMIT GUARD STILL RULES. Under the same `_RUN_CREATE_LOCK` `start_run` uses,
    an existing PENDING/QUEUED/RUNNING/PAUSED run for this exact (workflow, document,
    locale) is re-attached to instead of queued behind — reported in `attached` with its
    status, so a second "translate all" over a fan-out already in flight adds nothing and
    says so, rather than queueing a duplicate execution over one segment set. A run of a
    DIFFERENT workflow on the same (document, locale) (GitHub #213 part 2) can be neither
    attached to nor queued behind — it lands in `conflicts` with the running run's id and
    the standard refusal sentence, and the rest of the fan-out proceeds.

    A ROW THAT CANNOT BE WRITTEN IS REPORTED, NEVER SWALLOWED. `commit_launch` can raise
    `WriteContentionError` under exactly the launch pressure a wide fan-out creates. The
    inline half of the same loop surfaces that to the caller, so this half does too: the pair
    lands in `failed` with its reason and every surface carries it as `queue_incomplete`.
    Nothing is silently absent, which was HIGH-2's whole complaint.

    The eager launch guards (trash document, archived project/workflow, FULL_TEXT x tm_match,
    project budget) are NOT re-run here: the caller resolved this plan through them a moment
    ago, and the LOAD-BEARING copies live in `run_workflow_stream`, which every admitted run
    passes through — so a document deleted while its row waited is refused there and the run
    ends honestly failed, exactly as it would for a run that was already executing."""
    group = group or new_queue_group()
    queued: list[dict] = []
    attached: list[dict] = []
    failed: list[dict] = []
    conflicts: list[dict] = []
    for wf_id, doc_id, locale, label in plan:
        row = {"locale": locale, "document_id": doc_id, "document_name": label}
        try:
            with _RUN_CREATE_LOCK:
                existing = session.exec(
                    select(WorkflowRun)
                    .where(
                        WorkflowRun.workflow_id == wf_id,
                        WorkflowRun.document_id == doc_id,
                        WorkflowRun.target_locale == locale,
                        WorkflowRun.status.in_(_IN_FLIGHT_RUN_STATUSES),
                    )
                    .order_by(WorkflowRun.id.desc())
                ).first()
                superseded = None
                if (
                    existing is not None
                    and existing.batch_id is None
                    and not _run_thread_alive(existing.id)
                    and _is_stale_run(session, existing)
                ):
                    superseded, existing = existing, None
                if existing is not None:
                    attached.append({**row, "run_id": existing.id, "status": existing.status})
                    continue
                # GitHub #213 part 2: a run of a DIFFERENT workflow on this (document,
                # locale) is not attachable and must not queue behind either -- the row
                # would wake at admission and write back over the rival's segment set.
                # Reported per pair (the fan-out runs the others), with the SAME refusal
                # sentence `RunConflictError` carries, so no surface paraphrases it.
                rival = _overlapping_run(session, wf_id, doc_id, locale)
                if rival is not None:
                    rival_run, rival_wf = rival
                    conflicts.append(
                        {
                            **row,
                            "code": RunConflictError.code,
                            "run_id": rival_run.id,
                            "workflow_name": rival_wf,
                            "status": rival_run.status,
                            "reason": str(RunConflictError(rival_run.id, rival_wf, doc_id, locale)),
                        }
                    )
                    continue
                run = WorkflowRun(
                    workflow_id=wf_id,
                    document_id=doc_id,
                    target_locale=locale,
                    created_by=created_by or "",
                    status=RunStatus.QUEUED,
                    current_step_index=0,
                    queue_group=group,
                )

                def _stage_queue_insert(_run=run):
                    session.add(_run)

                # keep_loaded=True for the same reason start_run uses it: the PK stays
                # populated after the commit, so the id is read without a refresh() SELECT
                # on the launch hot path.
                _db.commit_launch(
                    session,
                    stage=_stage_queue_insert,
                    keep_loaded=True,
                    what=f"queue run for workflow {wf_id} / document {doc_id} / {locale}",
                )
                if superseded is not None:
                    # #505 item 1: the QUEUED row is ALREADY committed above, so this stale
                    # supersede is best-effort, exactly as in `start_run`: it rides the same
                    # bounded writer-lock retry (the staged fields are re-applied after each
                    # rollback), and one that still loses the lock leaves the stale row
                    # RUNNING for the next sweep and NEVER fails (or half-reports) the pair
                    # whose run row is in fact queued. A bare commit here used to leave the
                    # session needing a rollback, so pair 1 was reported failed although its
                    # QUEUED row existed and pairs 2..N raised PendingRollbackError untried.
                    _queued_id = run.id

                    def _stage_supersede(_old=superseded, _new_id=_queued_id):
                        _old.status = RunStatus.FAILED
                        _old.finished_at = datetime.utcnow()
                        _stamp_note(_old, STALE_NOTE % _new_id)
                        session.add(_old)

                    try:
                        _db.commit_with_retry(session, stage=_stage_supersede)
                        row["superseded"] = superseded.id
                    except Exception:
                        _rollback_quietly(session)
                        log.warning(
                            "Could not supersede stale run %s while queueing run %s; "
                            "leaving it for the next staleness sweep",
                            superseded.id,
                            _queued_id,
                        )
            queued.append({**row, "run_id": run.id, "status": RunStatus.QUEUED})
        except _db.WriteContentionError as exc:
            _rollback_quietly(session)
            log.warning(
                "Queueing a run (wf=%s doc=%s %s) lost the writer lock", wf_id, doc_id, locale
            )
            failed.append({**row, "reason": str(exc)})
        except Exception as exc:
            # A failed commit leaves the session in a state that raises PendingRollbackError
            # on its next use: roll back so one bad pair cannot poison every pair after it.
            _rollback_quietly(session)
            log.exception("Queueing a run (wf=%s doc=%s %s) failed", wf_id, doc_id, locale)
            failed.append({**row, "reason": f"{type(exc).__name__}: {exc}"})
    if queued and start_scheduler:
        ADMISSION.ensure_started()
        ADMISSION.notify()
    return {
        "group": group,
        "queued": queued,
        "attached": attached,
        "failed": failed,
        "conflicts": conflicts,
    }


class _AdmissionScheduler:
    """THE ONE daemon that turns QUEUED run rows into running ones as slots free.

    One per process, started on first use and deduped exactly like the #101 progress writer
    (`_progress_writer_start`) — which is the fix for round 3's MED-5: round 2 spawned a
    drainer thread PER fan-out with no registry, no cap and no dedupe, so an impatient
    operator clicking "Translate all" five times got five drainers, each parked on the same
    Condition. `RuntimeError: can't start new thread` is the failure class this whole train
    exists for; a fixed thread count cannot cause it.

    IT IS FAIR, on two axes (HIGH-3):

      * ROUND-ROBIN BETWEEN FAN-OUTS. It admits the oldest row of the fan-out AFTER the one
        it served last, so a 72-run "translate all" cannot starve a second operator's
        6-run one. Within a fan-out it is FIFO by row id.
      * IT YIELDS TO INTERACTIVE LAUNCHES. It takes slots through
        `RUN_SLOTS.acquire_for_queue`, which leaves the first slot free for a
        `QUEUE_YIELD_S` window after any single launch has been refused. Measured before
        this: 0 of 25 interactive launches admitted during one fan-out; the queue simply
        won every slot the instant it freed, because it was already parked on the Condition.

    IT NEVER BLOCKS A REQUEST. Every caller returns the moment its rows are written; this
    thread does the waiting. And it SKIPS a pass entirely when the connection pool has
    nothing free (the progress writer's rule, gate F): admitting a run into an exhausted
    pool is how the run would freeze at step 0, which is GitHub #100 itself.

    A CRASH IS RECORDED ON THE ROW. If admission raises — write contention, a pool timeout,
    a thread that will not start — the run is marked FAILED with a named reason through the
    pool-less bookkeeping session, so the operator sees a failed run with a sentence instead
    of a row that waits forever."""

    THREAD_NAME = "loc-run-admission"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        #: The fan-out served last, for round-robin (in-memory: a restart simply starts the
        #: rotation at the oldest group again, which is not unfair, only different).
        self._last_group: str | None = None
        #: Set by the tests' inline drain so a pass can be driven deterministically.
        self.admitted_total = 0
        self._batches_dirty = threading.Event()
        self._supervise_lock = threading.Lock()
        self._next_restart = 0.0
        self._restarted = False

    # -- lifecycle -----------------------------------------------------------------
    def ensure_started(self) -> bool:
        """Start the daemon if it is not already running. Returns True if this call started
        it. GUARDS `Thread.start()` (round 3, LOW-1): `RuntimeError: can't start new thread`
        is the #109 trigger, and letting it propagate out of a fan-out would 500 a launch
        whose inline half had already succeeded. A queue nobody drains is still visible,
        still cancellable and still adopted at the next boot, so a failure here degrades to
        'later' rather than to 'lost' — logged loudly."""
        t = self._thread
        if t is not None and t.is_alive():
            return False
        with self._lock:
            t = self._thread
            if t is not None and t.is_alive():
                return False
            t = threading.Thread(target=self._loop, name=self.THREAD_NAME, daemon=True)
            try:
                t.start()
            except BaseException:
                log.exception(
                    "Could not start the run-admission scheduler; queued runs stay QUEUED "
                    "(visible and cancellable) until one starts"
                )
                return False
            self._thread = t
            return True

    def notify(self) -> None:
        """Wake the scheduler now (new rows were queued, or a slot freed)."""
        self._wake.set()
        self._batches_dirty.set()

    def alive(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def health_detail(self) -> str:
        if not self.alive():
            return "dead"
        return "restarted" if self._restarted else "alive"

    def supervise(self, now: float | None = None) -> str:
        """The lifecycle scheduler checks us every interval, without touching the DB.

        Restart attempts are rate-limited to once per 30 seconds, including failure
        to create a thread. A healthy daemon requires no lock or pooled Session.
        """
        if self.alive():
            return self.health_detail()
        now = time.monotonic() if now is None else now
        with self._supervise_lock:
            if self.alive() or now < self._next_restart:
                return self.health_detail()
            self._next_restart = now + 30.0
            log.error("Run-admission daemon is dead; attempting restart")
            if self.ensure_started():
                self._restarted = True
                self.notify()
        return self.health_detail()

    def _loop(self) -> None:
        while True:
            try:
                self._wake.wait(QUEUE_POLL_S)
                self._wake.clear()
                self.drain_once()
            except Exception:  # pragma: no cover - the daemon must outlive any one pass
                log.exception("run-admission scheduler pass failed")
            except BaseException:
                log.exception("Run-admission daemon died; supervisor will restart it")
                return

    # -- one pass ------------------------------------------------------------------
    def drain_once(
        self,
        *,
        slot_wait: float | None = None,
        limit: int | None = None,
        batch_id: int | None = None,
        queue_group: str | None = None,
    ) -> int:
        """Admit as many queued rows as the install has capacity for right now. Returns how
        many were admitted. Public so a test (and `progress_drain`'s sibling shape) can drive
        one pass without the daemon."""
        wait = QUEUE_SLOT_WAIT_S if slot_wait is None else slot_wait
        admitted = 0
        skipped = set()
        from app.services import batch_runs

        if _db.pool_has_no_capacity():
            return admitted
        if self._batches_dirty.is_set():
            self._batches_dirty.clear()
            batch_runs.finalize_ready_batches()
        while True:
            if limit is not None and admitted >= limit:
                return admitted
            if _db.pool_has_no_capacity():
                # Nothing to admit INTO: a run needs a pooled connection for its whole life,
                # so starting one now is how it freezes at step 0 (GitHub #100). The rows
                # keep waiting and the next tick re-reads.
                return admitted
            if not self._has_queued(batch_id=batch_id, queue_group=queue_group):
                return admitted
            token = RUN_SLOTS.acquire_for_queue(timeout=wait)
            if token is None:
                return admitted  # at capacity, or yielding to an interactive launch
            with fanout_slot(token):
                picked = self._next_queued(skipped, batch_id=batch_id, queue_group=queue_group)
                if picked is None:
                    token.release()
                    return admitted
                run_id, group = picked
                self._last_group = group
                if self._admit(run_id, token):
                    admitted += 1
                    self.admitted_total += 1
                else:
                    skipped.add(group)

    def _has_queued(self, *, batch_id: int | None = None, queue_group: str | None = None) -> bool:
        from app.db import engine

        try:
            with Session(engine) as s:
                statement = select(WorkflowRun.id).where(WorkflowRun.status == RunStatus.QUEUED)
                if batch_id is not None:
                    statement = statement.where(WorkflowRun.batch_id == batch_id)
                if queue_group is not None:
                    statement = statement.where(WorkflowRun.queue_group == queue_group)
                return s.exec(statement.limit(1)).first() is not None
        except Exception:
            log.debug("admission scheduler could not read the queue", exc_info=True)
            return False

    def _next_queued(
        self, skipped=(), *, batch_id: int | None = None, queue_group: str | None = None
    ) -> tuple[int, str | None] | None:
        """Rotate across groups with a keyset query; choose their oldest waiting row.

        Limiting raw rows hid later fan-outs behind the first 500 members of a wide
        group. Grouping in SQL and wrapping after the previous key bounds the returned
        data without excluding any group from the rotation.
        """
        from sqlalchemy import String, cast, func

        from app.db import engine

        group = func.coalesce(WorkflowRun.queue_group, cast(WorkflowRun.id, String))
        statement = (
            select(func.min(WorkflowRun.id), group)
            .where(WorkflowRun.status == RunStatus.QUEUED)
            .where(group.notin_(skipped))
            .group_by(group)
            .order_by(group)
            .limit(1)
        )
        if batch_id is not None:
            statement = statement.where(WorkflowRun.batch_id == batch_id)
        if queue_group is not None:
            statement = statement.where(WorkflowRun.queue_group == queue_group)
        with Session(engine) as s:
            row = None
            if self._last_group is not None:
                row = s.exec(statement.where(group > self._last_group)).first()
            if row is None:
                row = s.exec(statement).first()
            return tuple(row) if row else None

    def _admit(self, run_id: int, token: "_SlotToken") -> bool:
        """QUEUED -> RUNNING for one row, under the slot `token`, then start its execution.

        Re-reads the row under `_RUN_CREATE_LOCK` and admits ONLY a still-QUEUED one, which
        is what makes cancel work through the ordinary path: `cancel_run` flips the row to
        CANCELLED, and this sees it and walks away without ever executing it."""
        from app.db import engine

        try:
            with fanout_slot(token):
                with Session(engine) as s, _RUN_CREATE_LOCK:
                    run = s.get(WorkflowRun, run_id)
                    if run is None or run.status != RunStatus.QUEUED:
                        return False  # cancelled, deleted, or admitted by another pass
                    wf_id = run.workflow_id
                    doc_id = run.document_id
                    locale = run.target_locale
                    actor = run.created_by or ""
                    batch_id = run.batch_id
                    if batch_id is not None:
                        from app.services import batch_runs

                        if not batch_runs.queue_member_allowed(s, run):
                            return False

                    from sqlalchemy import update

                    claimed = False

                    def _stage_admit():
                        nonlocal claimed
                        # Cancel (or another process) may have changed this row after
                        # our read. Claim only a row that is STILL queued in the DB.
                        result = s.exec(
                            update(WorkflowRun)
                            .where(WorkflowRun.id == run_id, WorkflowRun.status == RunStatus.QUEUED)
                            .values(status=RunStatus.RUNNING, started_at=datetime.utcnow())
                            .execution_options(synchronize_session=False)
                        )
                        claimed = result.rowcount == 1

                    _db.commit_launch(s, stage=_stage_admit, what=f"admit queued run {run_id}")
                    if not claimed:
                        return False
                # The actor scope is entered on THIS thread so the raw run thread inherits
                # it, exactly as an in-loop launch does — rebuilt from the row's own
                # `created_by`, which is why the queue needed no in-memory closure to carry it.
                with _queue_actor_scope(wf_id, locale, actor):
                    if batch_id is not None:
                        batch_runs.run_queued_member(run_id, wf_id, doc_id, locale, token)
                    else:
                        _run_in_background(
                            wf_id, doc_id, locale, run_id=run_id, is_resume=False, slot=token
                        )
            return True
        except BaseException as exc:
            log.exception("Admitting queued run %s failed", run_id)
            _mark_queue_launch_failed(run_id, exc)
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            return False


ADMISSION = _AdmissionScheduler()


def adopt_queued_runs() -> int:
    """Boot reconcile for the queue (gate A round 3, HIGH-3): count the QUEUED rows this
    install came up with and start the scheduler for them. Returns the count.

    A restart is exactly what round 2's in-memory queue could not survive, and it is a
    documented-normal event here (redeploys and host auto-stop). The rows are already durable,
    so 'recovery' is not a repair: nothing is
    failed, nothing is re-created, the same rows are simply picked up again. Called from the
    startup reconcile beside `recover_interrupted_runs` (which deliberately does NOT touch
    QUEUED rows — a queued row has no thread to have lost)."""
    try:
        with _bookkeeping_session() as s:
            n = len(
                s.exec(
                    select(WorkflowRun.id)
                    .where(WorkflowRun.status == RunStatus.QUEUED)
                    .limit(QUEUE_SCAN_LIMIT)
                ).all()
            )
    except Exception:
        log.warning("Could not read the run queue at startup", exc_info=True)
        return 0
    # Poll even an initially empty queue: a separate CLI process may enqueue later.
    ADMISSION.ensure_started()
    ADMISSION.notify()
    return n


def _run_in_background(
    workflow_id: int,
    doc_id: int,
    locale: str,
    run_id: int | None = None,
    seed_text: str | None = None,
    is_resume: bool | None = None,
    slot: "_SlotToken | None" = None,
) -> None:
    """Drain run_workflow_stream on a background thread with its own Session.
    The DB rows it writes are the source of truth the observer replays; the
    yielded events are discarded here.

    `is_resume` (R1) is the launcher's fresh/resume assertion: start_run passes False
    (a fresh run with a pre-created run_id), resume_run_async passes True.

    `slot` is the caller's already-reserved capacity token (start_run reserves BEFORE it
    writes the run row, so a refusal never leaves an orphaned RUNNING row behind).
    Anything else takes one here, unconditionally — it counts toward capacity but is never
    refused at this seam.

    SINGLE OWNERSHIP (gate A round 1, HIGH-2). `handed_off` is set on the very first line,
    before anything here can raise, so from this call onward exactly one place releases the
    token: `_work`'s `finally` if the thread started, this function's `except` if
    `Thread.start()` raised. The caller's own `finally` then sees `handed_off` and keeps
    its hands off. The first round released in BOTH places on the thread-start failure and
    stole a live run's slot."""
    from app.db import engine

    token = slot if slot is not None else RUN_SLOTS.take()[0]
    token.handed_off = True

    def _work():
        try:
            with Session(engine) as s:
                for _ev in run_workflow_stream(
                    s,
                    workflow_id,
                    doc_id,
                    locale,
                    run_id=run_id,
                    seed_text=seed_text,
                    is_resume=is_resume,
                    capacity_slot=token,
                ):
                    pass
        except Exception as exc:
            log.exception("Workflow run (wf=%s doc=%s) crashed", workflow_id, doc_id)
            try:
                with _bookkeeping_session() as s:
                    rid = run_id
                    if rid is None:
                        # find the run this thread just created for this doc/wf. #505
                        # item 7: the LOCALE is part of the identity: a sibling locale's run
                        # of the same (workflow, document) is a different run, and the
                        # newest row by id is not necessarily this thread's.
                        _where = [
                            WorkflowRun.workflow_id == workflow_id,
                            WorkflowRun.document_id == doc_id,
                        ]
                        if locale:
                            _where.append(WorkflowRun.target_locale == locale)
                        r = s.exec(
                            select(WorkflowRun).where(*_where).order_by(WorkflowRun.id.desc())
                        ).first()
                        rid = r.id if r else None
                    if rid is not None:
                        run = s.get(WorkflowRun, rid)
                        # CANCELLED joins COMPLETED/PAUSED as a status the crash handler
                        # must NOT overwrite: an operator cancel that raced a crashing
                        # thread stays cancelled, never flipped to FAILED (run-cancel train).
                        if run and run.status not in (
                            RunStatus.COMPLETED,
                            RunStatus.PAUSED,
                            RunStatus.CANCELLED,
                        ):
                            # P-C5 honest error: record WHY this run failed so the
                            # observer's `_failure_reason` notes-fallback surfaces it
                            # instead of the pre-fix `error: null`. A transient writer-
                            # lock (the many-concurrent-runs ceiling) gets the distinct,
                            # retryable WRITE_CONTENTION_NOTE; any other uncaught crash
                            # records its bounded exception text.
                            # GitHub #100: a CONNECTION-POOL timeout is its own class and
                            # gets its own sentence. It is not a writer-lock (nothing was
                            # contended on disk) and it is not a bug in the workflow — it
                            # is this install running more work than its pool can carry,
                            # which is the one failure an operator can fix by CONFIGURING
                            # something. `error: null` (the pre-fix shape) told them none
                            # of that; twelve of these were left RUNNING at step 0.
                            note = _crash_terminal_note(exc)

                            # Retry the FAILED-marking write itself: under the very
                            # contention that caused the crash, this honest-error commit
                            # would otherwise be the next lock victim. Re-stage on each
                            # attempt (a rollback reverts the status/notes writes); the
                            # note guard keeps a re-stage from appending the sentence twice.
                            #
                            # RE-CHECK the terminal status INSIDE the stage, not just once
                            # above: the retry's rollback + jittered backoff widens the
                            # check-then-act window to seconds under exactly this
                            # contention, and an operator cancel can commit inside it. A
                            # rollback expires `_run`, so re-reading its status sees that
                            # committed CANCELLED/COMPLETED/PAUSED and this handler no-ops
                            # rather than resurrecting it to FAILED (the run-cancel-train
                            # invariant `_commit_run_terminal` guards for the happy path).
                            def _stage(_run=run, _note=note):
                                if _run.status in (
                                    RunStatus.COMPLETED,
                                    RunStatus.PAUSED,
                                    RunStatus.CANCELLED,
                                ):
                                    return  # someone finished it first — commit nothing
                                if _note not in (_run.notes or ""):
                                    _stamp_note(_run, _note)
                                _run.status = RunStatus.FAILED
                                _run.finished_at = datetime.utcnow()
                                s.add(_run)

                            _db.commit_with_retry(s, stage=_stage)
            except Exception:
                log.exception("Failed to mark run failed after crash")
        finally:
            # ALWAYS give the slot back — including on the crash path, and including when
            # the crash handler itself failed. A leaked slot is a permanent, invisible
            # reduction of this install's launch capacity: every later launch is refused
            # with a 503 for a run that finished hours ago. FIRST, so that a raise out of
            # the #101 hygiene below cannot cost the install a unit of capacity.
            token.release()
            # #101 hygiene: the ticker drops its own state at every step boundary, but a
            # step that died OUTSIDE that path (the crash above) would leave one entry per
            # run in a long-lived server. The persisted columns keep whatever the step
            # reached, so forgetting here loses no signal.
            if run_id is not None:
                _progress_forget(run_id)

    # THE RUN ID IS IN THE THREAD NAME ON PURPOSE (review F1). It is the only
    # in-process record of "run N is being executed right now" — the DB row cannot
    # say it (a RUNNING row means exactly the same thing whether its thread is alive
    # or was killed by a restart, which is the whole reason recovery exists). Both
    # sweepers consult `_run_thread_alive` before failing anything, so neither can
    # terminate a run this process is still executing. Name-encoded rather than a
    # registry dict because a thread that dies takes its name with it — there is no
    # cleanup path to forget.
    try:
        threading.Thread(
            target=_work, name=_run_thread_name(run_id, workflow_id, doc_id), daemon=True
        ).start()
    except BaseException:
        # The thread never started, so `_work`'s finally will never run: hand the slot
        # back here or this launch permanently costs the install one unit of capacity.
        # Exactly ONE release happens (the token is idempotent) and the caller will not
        # add a second one, because `handed_off` is already True — HIGH-2.
        token.release()
        raise


_progress_idle.set()


def start_run(
    session: Session,
    workflow_id: int,
    doc_id: int,
    locale: str,
    created_by: str = "",
    detail: dict | None = None,
    slot: "_SlotToken | None" = None,
) -> int:
    """Admit, then create-and-execute. See `_start_run_admitted` for the launch itself.

    THE ADMISSION GATE (GitHub #99/#100). Beyond `config.MAX_CONCURRENT_RUNS` executions
    in flight, a launch that would start NEW work raises `RunCapacityError`, which every
    surface renders as an honest, retryable 503 (the shape `db.WriteContentionError`
    already gets). The refusal happens before any WRITE, so nothing is left behind — no
    run row, nothing for an operator to find frozen at step 0, which is precisely the
    damage the flood did.

    A RE-ATTACH IS NEVER REFUSED. Being at capacity is a statement about starting more
    work, not about answering for work already running: the double-submit guard's whole
    job is to hand a caller back the run it already has, and 503-ing that would make a
    polling client believe its own live run had been rejected. So when no slot is free we
    still run the launch path in re-attach-only mode (`admit_new=False`), which returns an
    existing run or raises rather than creating one. A re-attach also consumes NO slot —
    the run it points at is already counted.

    The slot, once handed to the execution thread, is released in its `finally`. Set
    MAX_CONCURRENT_RUNS=0 to disable the gate entirely and get the pre-fix unbounded
    behaviour.

    A supplied `slot` pays for this single execution. Durable fan-outs instead enqueue
    all their rows and let the shared scheduler acquire one slot per admission.

    OWNERSHIP: the token is released here only while `handed_off` is False, i.e. while
    nothing else has taken responsibility for it. Once `_run_in_background` has it, the
    execution owns it (HIGH-2)."""
    if slot is not None:
        token, observed = slot, RUN_SLOTS.in_flight
    else:
        token, observed = RUN_SLOTS.acquire(timeout=0)
    refusal = (
        None
        if token is not None
        else RunCapacityError(observed, int(getattr(config, "MAX_CONCURRENT_RUNS", 0) or 0))
    )
    try:
        run_id, _spawned = _start_run_admitted(
            session,
            workflow_id,
            doc_id,
            locale,
            created_by=created_by,
            detail=detail,
            slot=token,
            refusal=refusal,
        )
        return run_id
    finally:
        # Re-attached, refused, or raised before the execution thread took ownership.
        # A CALLER-SUPPLIED slot is left alone here BY DESIGN and released by the caller's
        # own per-iteration owner (gate A round 2, HIGH-2) — see `_fanout_slot` below,
        # which every fan-out call site uses so the token has exactly one owner at every
        # instant, including the `WriteContentionError` path out of `db.commit_launch`.
        if token is not None and not token.handed_off and slot is None:
            token.release()


def _start_run_admitted(
    session: Session,
    workflow_id: int,
    doc_id: int,
    locale: str,
    created_by: str = "",
    detail: dict | None = None,
    slot: "_SlotToken | None" = None,
    refusal: "RunCapacityError | None" = None,
) -> tuple[int, bool]:
    """Create the WorkflowRun row and start executing it on a background thread.
    Returns (run_id, spawned) — `spawned` is False for a re-attach, which starts no
    execution and therefore owns no capacity slot. Callers use `start_run`.
    Returns the run id immediately so the caller can redirect to the observer.

    `detail` (GitHub #44 -- THE HONEST RE-ATTACH) is an optional out-parameter: when
    a dict is supplied it is filled with {"run_id": int, "reattached": bool} so the
    caller can tell "I started this" from "this was already in flight and I attached
    to it". The return value is UNCHANGED (the run id, re-attached or fresh), so
    every existing caller is byte-identical and only a caller that asks pays any
    attention. It exists because `keys_translate` counted a re-attach as a launch and
    told the operator "Started 3 run(s)" while one of the three locales was in fact
    bound to a corpse and would never translate -- the exact prod #74 story.

    `created_by` (jobs-history attribution) stamps the launcher onto the run so the
    unified history log can show who ran it; "" (the default) preserves the old
    behavior (the history renders "-"). Re-attaching to an existing in-flight run
    does NOT overwrite its original launcher.

    Double-submit guard: if a RUNNING/PAUSED/PENDING WorkflowRun already exists for
    this exact (workflow, document, LOCALE), re-attach to it instead of spawning a
    second concurrent execution over the same segment set (a fast double-POST / double
    tab). The guard is LOCALE-AWARE: a run for a DIFFERENT locale of the same document
    is a different segment set and must NOT be re-attached to — before this was locale-
    blind, so while a Phase-5a batch kept N members RUNNING for one (workflow, document)
    a manual single run for ANY locale silently re-attached to a batch member of some
    OTHER locale and the requested locale never ran. Matching a same-locale run
    (including a batch member) IS still re-attached — that is the genuine double-submit,
    and re-attaching prevents double-running that one segment set. The check-and-create
    is serialized by _RUN_CREATE_LOCK so two concurrent launchers cannot both pass the
    check and both create. Only genuinely idle (terminal/absent) state starts a fresh
    run.

    CROSS-WORKFLOW OVERLAP (GitHub #213 part 2): a run of a DIFFERENT workflow on the
    same (document, LOCALE) writes back to the same segment set and there is nothing to
    re-attach to — it is REFUSED outright with `RunConflictError` (stable code
    `run_conflict`, names the running run and how to cancel it; every surface renders
    the same sentence). Same workflow = re-attach, unchanged; a different LOCALE still
    starts.

    REFUSES a key project's TRASH document (SPEC-key-based-projects §5, Wave B fix
    round A-F1). A soft-deleted key's value rows are parked there so no run can reach
    them; `keys.translatable_documents` never lists it — but a caller that resolves a
    document id ITSELF (POST /documents/{id}/run did, with a bare `session.get`) never
    consulted that list, so a run could be pointed straight at the trash document and
    spend provider budget on strings the customer had just deleted, emitting ZERO delta
    entries (a tombstoned key is silent by design, review A9).

    This copy is the EAGER one and it is deliberately kept (fix round 2, W1): it fires
    before a WorkflowRun row is created, so an HTTP caller gets a clean refusal rather
    than a run row that dies on a background thread. The LOAD-BEARING refusal is
    `_refuse_key_trash_document`'s other call site, in `run_workflow_stream` — the seam
    `start_run`'s own thread, `run_workflow`, `batch_runs._run_member` and `resume_run`
    ALL pass through. `start_run` is one launcher of four; the seam is all of them.

    STALENESS FLOOR (GitHub #44). A RUNNING run with no activity for
    STALE_RUN_AFTER is NOT a valid re-attach target: on an install that is never
    restarted, startup recovery never runs, so a corpse would block its (workflow,
    document, locale) forever and every Translate would report success while doing
    nothing. Such a row is FAILED here (stamping STALE_NOTE, which NAMES the run that
    superseded it) and a fresh run starts; `detail["superseded"]` reports it so the
    caller can say so out loud.

    THREE THINGS THE FLOOR REFUSES TO SWEEP, each a review finding:
      * a run whose thread is ALIVE in this process (F1) -- the sweep is only a DB
        write and cannot stop a thread, so sweeping a live run produced two runs
        racing writes over one segment set;
      * a BATCH MEMBER (F3) -- `batch_id IS NOT NULL` rows belong to the batch
        dispatcher and its own healer; failing one behind the dispatcher's back
        leaves the batch un-finalized and its rollup wrong. Same exclusion the
        startup healer has always had; the floor was missing it;
      * anything that is not RUNNING -- PAUSED is durable human state (a reviewer may
        come back to it days later, F7) and PENDING is a momentary pre-execution
        state. Neither ages out, ever."""
    doc = session.get(Document, doc_id)
    _refuse_key_trash_document(doc)
    _refuse_launch_guards(session, workflow_id, doc)
    superseded = None
    with _RUN_CREATE_LOCK:
        existing = session.exec(
            select(WorkflowRun)
            .where(
                WorkflowRun.workflow_id == workflow_id,
                WorkflowRun.document_id == doc_id,
                WorkflowRun.target_locale == locale,
                WorkflowRun.status.in_(_IN_FLIGHT_RUN_STATUSES),
            )
            .order_by(WorkflowRun.id.desc())
        ).first()
        if (
            existing is not None
            and existing.batch_id is None
            and not _run_thread_alive(existing.id)
            and _is_stale_run(session, existing)
        ):
            superseded = existing
            existing = None
        if existing is not None:
            if detail is not None:
                detail["run_id"] = existing.id
                detail["reattached"] = True
                detail["paused"] = existing.status == RunStatus.PAUSED
                # ...and whether the thing we re-attached to is WAITING FOR A SLOT rather
                # than executing (gate A round 3, HIGH-1). "Already running (run #12)" about
                # a queued row is the same class of small lie as reporting a queued fan-out
                # as launched, and it sends the operator to a run page that will not move
                # yet. Every fan-out surface reads this and says "already queued".
                detail["queued"] = existing.status == RunStatus.QUEUED
            return existing.id, False  # re-attach: no execution spawned, no slot owned

        # GitHub #213 part 2: NO run of a DIFFERENT workflow either. A same-triple rival
        # has a run to re-attach to; a different-workflow one would spawn a SECOND
        # execution writing back over the SAME segment set (the prod story: runs 237 and
        # 239, one document, one locale, one second). Read-only, same lock, before the
        # first write -- the check-and-create stays atomic and a refusal leaves the DB
        # untouched, exactly like the capacity refusal below it.
        _refuse_overlapping_run(session, workflow_id, doc_id, locale)

        if refusal is not None:
            # At capacity and there is nothing to re-attach to, so this would be NEW
            # work. Refuse HERE — after the read-only guard, before the first write —
            # so the caller gets a clean 503 and the database is untouched.
            #
            # BUT SWEEP THE CORPSE FIRST (gate A round 1, LOW-5). The staleness floor
            # (GitHub #44) exists for installs that are never restarted, where a dead
            # RUNNING row blocks its (workflow, document, locale) forever. Refusing
            # before the sweep meant that at capacity — which on a wedged install is
            # exactly when corpses accumulate — the floor never ran, so the pressure
            # protected the very rows the floor is for. The sweep is a bounded write on
            # a row nothing owns, and it is followed by the same refusal either way.
            if superseded is not None:
                _sweep_stale_at_capacity(session, superseded, workflow_id, doc_id, locale)
            raise refusal

        run = WorkflowRun(
            workflow_id=workflow_id,
            document_id=doc_id,
            target_locale=locale,
            created_by=created_by or "",
            status=RunStatus.RUNNING,
            current_step_index=0,
        )

        # flood R2-F1: this run-row INSERT is the exact launch write the 25-70-way flood
        # 500'd on (`database is locked` at orchestrator.start_run's session.commit()).
        # Commit it through the bounded, jittered writer-lock retry; on exhaustion raise
        # WriteContentionError so the LAUNCH request surfaces an HONEST retryable 503 rather
        # than a 500 traceback. `_stage` re-adds the pending row after the retry's rollback —
        # the pending set here is exactly {WorkflowRun} (the guard above only READ), so a
        # retry loses nothing and is byte-identical to a plain commit when the first wins.
        def _stage_run_insert():
            session.add(run)

        # keep_loaded=True (fold-in): the run's PK stays populated after the commit, so we
        # read run.id WITHOUT a `session.refresh(run)` SELECT — that refresh is on the launch
        # hot path and could ITSELF lose the writer lock and 500 (the R2-F1(a) shape).
        _db.commit_launch(
            session,
            stage=_stage_run_insert,
            keep_loaded=True,
            what=f"launch run for workflow {workflow_id} / document {doc_id} / {locale}",
        )
        run_id = run.id
        if superseded is not None:
            # Stamped only now, because the note NAMES its successor -- "restarted as
            # run #12" is the sentence that turns a bare FAILED into something an
            # operator can follow. Same lock, so no third launcher can interleave.
            def _stage_supersede():
                superseded.status = RunStatus.FAILED
                superseded.finished_at = datetime.utcnow()
                _stamp_note(superseded, STALE_NOTE % run_id)
                session.add(superseded)

            # Best-effort under the same writer-lock retry: the FRESH run is ALREADY committed
            # above, so a stale-run supersede that still loses the lock after the retry must
            # NOT sink the launch (that would strand a live run and 503 a caller whose run is
            # in fact running). On exhaustion, log and proceed — the un-superseded stale row
            # stays RUNNING and is swept by the next staleness pass / launch.
            try:
                _db.commit_launch(
                    session,
                    stage=_stage_supersede,
                    what=f"supersede stale run {superseded.id}",
                )
            except _db.WriteContentionError:
                log.warning(
                    "Could not supersede stale run %s under write contention; "
                    "leaving it RUNNING for the next sweep (fresh run %s is live)",
                    superseded.id,
                    run_id,
                )
            log.warning(
                "Superseded stale run %s (wf=%s doc=%s %s) with fresh run %s",
                superseded.id,
                workflow_id,
                doc_id,
                locale,
                run_id,
            )
    # is_resume=False (R1): start_run created a FRESH run row above, then drives it with
    # its pre-created run_id -- NOT a resume, so it must never look up an existing
    # envelope by that (possibly rowid-reused) id.
    # `slot`: the capacity token `start_run` (or a fan-out) took before any row existed;
    # the execution thread owns it from here and releases it when it ends.
    _run_in_background(workflow_id, doc_id, locale, run_id=run_id, is_resume=False, slot=slot)
    if detail is not None:
        detail["run_id"], detail["reattached"] = run_id, False
        detail["paused"] = False
        if superseded is not None:
            detail["superseded"] = superseded.id
    return run_id, True


def resume_run_async(
    session: Session,
    run_id: int,
    locale: str,
    edited_text: str,
    actor: str = "",
    capture: bool = True,
) -> bool:
    """Resume a paused run off the request thread (mirrors resume_workflow's
    advance-past-the-human-step, then continues on a background thread).

    Returns True if it LAUNCHED (the run was PAUSED and is now RUNNING on a background
    thread), False if it was a no-op (the run was missing or not paused — the double-
    submit guard fired). Every current caller (web + /api resume routes) ignores the
    result; it exists so a caller CAN tell a fresh launch from a re-attach no-op.

    Double-submit guard: only a PAUSED run may be resumed. A second resume (or a resume of
    an already-running/terminal run) is a no-op — otherwise it would advance
    current_step_index again and spawn a duplicate background execution that skips a step.
    The check-PAUSED-then-flip-to-RUNNING is taken under `_RUN_CREATE_LOCK` — the SAME lock
    `start_run` uses — so it is ATOMIC against a rival resume even when two resumes run on
    genuinely parallel threads (the web route is a sync `def` Starlette runs in a threadpool;
    the /api wait=true path can drive a resume from a threadpool worker). Without the lock
    those two could both read PAUSED and both spawn an execution (double provider spend, two
    threads racing one Segment set). With it, the first flips PAUSED→RUNNING+commits inside
    the lock and the second reads a non-PAUSED row and no-ops. The blocking EXECUTION runs on
    a separate thread AFTER the lock releases, so real concurrency is unaffected. `resume_workflow`
    is the SYNCHRONOUS twin (the CLI path); as of NEW-ISSUE-2 it carries the SAME PAUSED
    precondition (it refuses a non-paused run rather than advancing the index unguarded), so both
    resume surfaces reject a completed/running run.

    `actor` (LLMemory, adversarial review S20) is attribution only — it labels the
    capture events written for the segments the operator CHANGED, exactly as on the
    `prepare_resume_seed` path. THIS is the resume surface a human reviewer actually
    uses, so an anonymous event here is the most costly of the three. It is never
    used for any authorization decision; the caller (the web resume route) supplies
    the session identity.

    `capture=False` SKIPS the internal capture below. The /api resume route already
    captured through `prepare_resume_seed` (its structured-segments + clean-400 pre-
    validation path) before calling this, so letting this capture again would DOUBLE-write
    the resume MemoryEvents. The web route (the only capture=True caller) does no
    pre-validation and relies on the capture here."""
    with _RUN_CREATE_LOCK:
        run = session.get(WorkflowRun, run_id)
        if run is None or run.status != RunStatus.PAUSED:
            return False
        if run.target_locale:
            # Issue #493: resume with the RUN's own locale, never the caller's.
            locale = run.target_locale
        # LLMemory capture seam 2, THE WEB HALF. The CLI and /api resume paths pre-validate
        # through `prepare_resume_seed` (which captures there, and passes capture=False here);
        # THIS route hands `edited_text` straight to the background runner and would otherwise
        # be the one resume surface that learns nothing — the surface a human reviewer actually
        # uses. Capture BEFORE the state change, while the run is still PAUSED and
        # `paused_review` can still read the produced blob to diff against. A blank
        # edited_text is accept-as-is (no edits, nothing to capture); the count-mismatch case
        # is handled downstream and `zip` simply pairs nothing extra here. Under the lock so a
        # rival resume cannot slip a second capture in between this and the RUNNING flip.
        if capture and (edited_text or "").strip():
            # #136: normalize the free-text seed before capture AND execution split it.
            # capture=False callers already prepared their seed, which may contain
            # structured segment text whose internal line endings must stay untouched.
            edited_text = _normalize_resume_text(edited_text)
            try:
                view = paused_review(session, run_id)
                if view is not None:
                    parts = edited_text.split(SEG_JOINER)
                    if len(parts) == view["segment_count"]:
                        _capture_resume_edits(
                            session, run_id, view["segments"], parts, actor=actor or ""
                        )
            except Exception:
                log.warning("llmemory: web resume capture failed", exc_info=True)

        # flood R2-F1: the PAUSED->RUNNING resume flip is a LAUNCH write on the request
        # thread — give it the same bounded, jittered writer-lock retry as start_run and,
        # on exhaustion, raise WriteContentionError for an honest retryable 503 (the run
        # stays PAUSED and re-resumable — nothing was advanced). `_stage` re-applies all
        # three mutations after a retry's rollback; the pending set here is {WorkflowRun}.
        # #578: the flip is ONE conditional UPDATE ... WHERE status = 'paused' (it also
        # restarts the staleness clock, review F2), so a resume in another process (the CLI
        # beside the web server) cannot also win it; the loser no-ops like a double submit.
        claimed = {"ok": False}

        def _stage_resume_flip():
            claimed["ok"] = _claim_paused_run(session, run_id)

        _db.commit_launch(
            session,
            stage=_stage_resume_flip,
            what=f"resume run {run_id} ({locale})",
        )
        if not claimed["ok"]:
            return False
    # EXECUTION spawns AFTER the lock releases (mirrors start_run): the lock only serializes
    # the launch DECISION, never the minutes-long run.
    _run_in_background(
        run.workflow_id,
        run.document_id,
        locale,
        run_id=run_id,
        seed_text=edited_text,
        is_resume=True,
    )
    return True
