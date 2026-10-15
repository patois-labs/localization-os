"""cmd_workflow: shared leaf helpers (layered pure move)

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _MAX_WAIT_HOURS, _wait_hours, _tm_match_config_args,
_tristate_bool, _reject_archived_customer, _get_wf, _run_doc, _doc_locale, _wf_create,
_last_run_utc_label, _wf_list, _parallel_brackets, _parse_branch_flags, _resolve_step_ref,
_summarize_repair_changes, batch_result_human, _CELL_CODE, _ascii_cell, _batch_or_usage,
_run_step_summary, _run_step_line, _wf_show_run, _wf_step_diff, _load_edited_segments,
_wf_cancel_run, _parse_pair, _grp_list, _grp_show, _get_grp, _get_pt, _pt_create, _pt_list
from app.cli.cmd_workflow_common_leaf.

Documented in: docs/architecture/workflow-engine.md
"""

import argparse

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    get_scoped_or_404,
    op_org_id,
    remote_mode,
    require_org_scope,
)
from app.cli.cmd_workflow_common_leaf import (
    _CELL_CODE,
    _MAX_WAIT_HOURS,
    _ascii_cell,
    _batch_or_usage,
    _doc_locale,
    _get_grp,
    _get_pt,
    _get_wf,
    _grp_list,
    _grp_show,
    _last_run_utc_label,
    _load_edited_segments,
    _parallel_brackets,
    _parse_branch_flags,
    _parse_pair,
    _pt_create,
    _pt_list,
    _pt_system,
    _pt_system_history,
    _pt_system_reset,
    _pt_system_restore,
    _pt_system_set,
    _reject_archived_customer,
    _resolve_step_ref,
    _run_doc,
    _run_step_line,
    _run_step_summary,
    _summarize_repair_changes,
    _tm_match_config_args,
    _tristate_bool,
    _wait_hours,
    _wf_cancel_run,
    _wf_create,
    _wf_list,
    _wf_show_run,
    _wf_step_diff,
    batch_result_human,
)

__all__ = [
    "EXIT_FAIL",
    "EXIT_USAGE",
    "_CELL_CODE",
    "_MAX_WAIT_HOURS",
    "CliError",
    "_ascii_cell",
    "_batch_or_usage",
    "_doc_locale",
    "_get_grp",
    "_get_pt",
    "_get_wf",
    "_grp_list",
    "_grp_show",
    "_last_run_utc_label",
    "_load_edited_segments",
    "_parallel_brackets",
    "_parse_branch_flags",
    "_parse_pair",
    "_pt_create",
    "_pt_list",
    "_pt_system",
    "_pt_system_history",
    "_pt_system_reset",
    "_pt_system_restore",
    "_pt_system_set",
    "_reject_archived_customer",
    "_remote_impl",
    "_resolve_step_ref",
    "_run_doc",
    "_run_step_line",
    "_run_step_summary",
    "_session",
    "_summarize_repair_changes",
    "_tm_match_config_args",
    "_tristate_bool",
    "_wait_hours",
    "_wf_cancel_run",
    "_wf_create",
    "_wf_list",
    "_wf_show_run",
    "_wf_step_diff",
    "argparse",
    "batch_result_human",
    "get_scoped_or_404",
    "op_org_id",
    "remote_mode",
    "require_org_scope",
]


def register_cmd_workflow_common_sub(sub) -> None:
    p = sub.add_parser("create", help="Create a workflow.")
    p.add_argument("--name", required=True, help="workflow name.")
    p.add_argument(
        "--scope",
        default="global",
        choices=["global", "customer", "project"],
        help="visibility scope (default global).",
    )
    p.add_argument("--customer-id", default="", help="customer id (for scope=customer).")
    p.set_defaults(func=_wf_create)

    p = sub.add_parser("list", help="List workflows.")
    p.add_argument(
        "--archived",
        default="active",
        choices=("active", "all", "only"),
        help="archive filter: 'active' (default, hides archived), 'all' (both), "
        "'only' (just archived).",
    )
    p.set_defaults(func=_wf_list)


def register_cmd_workflow_common_sub_2(sub) -> None:
    p = sub.add_parser(
        "show-run",
        help="Show a workflow run's status and, for a "
        "PAUSED human-review run, the review text with explicit segment "
        "boundaries (read the text safely before editing/resuming). "
        "FULL_TEXT tm_offers reports sentences_examined and matches_offered, unverified. "
        "Offers are not a guarantee; use SEGMENTED for exact wording.",
    )
    p.add_argument("run_id", type=bounded_id, help="workflow-run id (from `workflow run`).")
    p.add_argument(
        "--visits",
        action="store_true",
        help="attach the full per-VISIT row-level trace (one entry per "
        "StepRun row, not just the latest-per-step status the default "
        "output shows): status/score/gate/provider/model/attempts/"
        "error/latency per visit. Additive - omit for byte-identical "
        "default output (GET /api/runs/{id}?visits=1 parity).",
    )
    p.add_argument(
        "--findings",
        action="store_true",
        help="print the LQA MQM findings block for this run (dimension/"
        "severity/note per issue), read-only from the run's already-"
        'persisted scorecard -- no LQA re-run, no spend. "" / not shown '
        "when the run never reached an LQA step. BUG-07: previously "
        "score+gate were the only LQA signal visible headless. "
        "Additive (GET /api/runs/{id}?findings=1 parity).",
    )
    p.set_defaults(func=_wf_show_run)

    p = sub.add_parser(
        "step-diff",
        help="Show what a step CHANGED, segment by segment, word-level "
        "(GitHub #340 'Highlight differences'): compare two step runs of one run "
        "- by default the run's newest translating output vs its previous "
        "translating step, or name the pair with --from/--to (StepRun ids listed by "
        "this command's output). Read-only (splits the StepRun text the "
        "run already recorded; no provider call, no spend). Parity with GET "
        "/api/runs/{id}/step-diff; mirrors the /runs/{id}/step-diff page.",
    )
    p.add_argument("run_id", type=bounded_id, help="workflow-run id (from `workflow run`).")
    p.add_argument(
        "--from",
        dest="step_from",
        type=bounded_id,
        default=None,
        metavar="STEP_RUN_ID",
        help="StepRun id of the 'before' step (default: the previous translating step).",
    )
    p.add_argument(
        "--to",
        dest="step_to",
        type=bounded_id,
        default=None,
        metavar="STEP_RUN_ID",
        help="StepRun id of the 'after' step (default: the run's newest output).",
    )
    p.set_defaults(func=_wf_step_diff)


def register_cmd_workflow_common_sub_3(sub) -> None:
    p = sub.add_parser(
        "cancel-run",
        help="Cancel an in-flight run "
        "(pending/running/paused -> CANCELLED). Terminal + non-resumable; "
        "the escape hatch for a run stuck paused at human review / an "
        "external vendor step, and it unblocks mid-flight re-authoring.",
    )
    p.add_argument("run_id", type=bounded_id, help="workflow-run id (from `workflow run`).")
    p.set_defaults(func=_wf_cancel_run)


def register_cmd_workflow_common_gsub(gsub) -> None:
    g = gsub.add_parser("list", help="List workflow groups.")
    g.set_defaults(func=_grp_list)

    g = gsub.add_parser("show", help="Show a group and its per-pair variants.")
    g.add_argument("group_id", type=bounded_id, help="group id.")
    g.set_defaults(func=_grp_show)


def register_cmd_workflow_common_psub(psub) -> None:
    pp = psub.add_parser(
        "create",
        help="Create a reusable prompt template "
        "(global, or attached to a customer via --customer).",
    )
    pp.add_argument("--name", required=True, help="template name.")
    pp.add_argument("--body", default="", help="the prompt body text.")
    pp.add_argument(
        "--customer",
        dest="customer_id",
        type=bounded_id,
        default=None,
        help="attach to a customer id (active, same-org; omit for GLOBAL).",
    )
    pp.set_defaults(func=_pt_create)

    pp = psub.add_parser("list", help="List reusable prompt templates.")
    pp.set_defaults(func=_pt_list)

    pp = psub.add_parser(
        "system",
        help="Show the pipeline system prompts and the two editable heads. "
        "Use --key to print one in full.",
    )
    pp.add_argument("--key", default=None, help="print only the prompt with this key.")
    pp.add_argument(
        "--workflow",
        type=bounded_id,
        default=None,
        help="act on this workflow's own text instead of the org's (the workflow's text "
        "wins over the org's, which wins over the built-in text).",
    )
    pp.set_defaults(func=_pt_system)

    pp = psub.add_parser(
        "system-set",
        help="Save a new version of an editable system-prompt head (step_head or mt_head). "
        "Admin only (with --workflow: admin or manager); needs the system_prompt_overrides "
        "feature on.",
    )
    pp.add_argument("--key", required=True, help="step_head or mt_head.")
    pp.add_argument("--body", default=None, help="the new head text.")
    pp.add_argument("--file", default=None, help="read the new head text from this file.")
    pp.add_argument(
        "--workflow",
        type=bounded_id,
        default=None,
        help="act on this workflow's own text instead of the org's (the workflow's text "
        "wins over the org's, which wins over the built-in text).",
    )
    pp.set_defaults(func=_pt_system_set)

    pp = psub.add_parser(
        "system-reset",
        help="Go back to the built-in text for an editable head (history is kept).",
    )
    pp.add_argument("--key", required=True, help="step_head or mt_head.")
    pp.add_argument(
        "--workflow",
        type=bounded_id,
        default=None,
        help="act on this workflow's own text instead of the org's (the workflow's text "
        "wins over the org's, which wins over the built-in text).",
    )
    pp.set_defaults(func=_pt_system_reset)

    pp = psub.add_parser(
        "system-history",
        help="Show the version history of an editable head, newest first.",
    )
    pp.add_argument("--key", required=True, help="step_head or mt_head.")
    pp.add_argument(
        "--workflow",
        type=bounded_id,
        default=None,
        help="act on this workflow's own text instead of the org's (the workflow's text "
        "wins over the org's, which wins over the built-in text).",
    )
    pp.set_defaults(func=_pt_system_history)

    pp = psub.add_parser(
        "system-restore",
        help="Save a copy of an older version of an editable head as the newest.",
    )
    pp.add_argument("--key", required=True, help="step_head or mt_head.")
    pp.add_argument("--version", type=bounded_id, required=True, help="the version to copy.")
    pp.add_argument(
        "--workflow",
        type=bounded_id,
        default=None,
        help="act on this workflow's own text instead of the org's (the workflow's text "
        "wins over the org's, which wins over the built-in text).",
    )
    pp.set_defaults(func=_pt_system_restore)
