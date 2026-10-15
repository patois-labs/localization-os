"""`workflow` command group — Phase 6 (Jobs / Workflows).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _MAX_WAIT_HOURS, _wait_hours, _tm_match_config_args,
_tristate_bool, _reject_archived_customer, _get_wf, _run_doc, _doc_locale, _wf_create,
_last_run_utc_label, _wf_list, _parallel_brackets, _parse_branch_flags, _resolve_step_ref,
_summarize_repair_changes, batch_result_human, _CELL_CODE, _ascii_cell, _batch_or_usage,
_run_step_summary, _run_step_line, _wf_show_run, _load_edited_segments, _wf_cancel_run, _parse_pair,
_grp_list, _grp_show, _get_grp, _get_pt, _pt_create, _pt_list, _wf_show, _wf_add_step,
_wf_edit_step, _wf_delete_step, _wf_move_step, _wf_clone, _wf_rename, _wf_archive, _wf_unarchive,
_wf_delete, _wf_author, _wf_author_setup, _wf_run_batch, batch_matrix_ascii, _wf_resume,
_grp_create, _grp_add, _grp_set_default, _grp_rename, _grp_delete, _pt_rename, _pt_delete, _wf_run,
_batch_show, _batch_rerun, _batch_retry_failed, _wf_copy from app.cli.cmd_workflow_common,
app.cli.cmd_workflow_part2, app.cli.cmd_workflow_part3, app.cli.cmd_workflow_part4.

CRUD (create/list/show/add-step/delete-step/clone/author-prompt) is a thin shell
over app.services.workflows + orchestrator.author_prompt.

The RUN path is the phase's correctness crux. `workflow run` executes the
orchestrator SYNCHRONOUSLY in the calling thread via orchestrator.run_workflow()
— NOT orchestrator.start_run(), which spawns a daemon background thread that
would die with the one-shot CLI process before the run finished. run_workflow()
drains the run to completion (or to the first human_review step) inline and
returns a summary, so when the command returns the run's state is real.

Human-in-the-loop: a workflow with a human_review step PAUSES. `workflow run`
reports the run id, the paused step index, and the exact resume command. `workflow
resume <run-id> --edited-text ...` calls the synchronous resume_workflow(), which
advances past the human step and runs the remaining steps to completion inline.

AUTHORING OVER --remote (SPEC-workflow-authoring-api): `create`, `add-step`,
`edit-step`, `delete-step` and `move-step` are now --remote-capable — a thin HTTP
client of POST /api/workflows and POST /api/workflows/{id}/steps[/{sid}/edit|delete|
move] (see _remote_impl.workflow_create / _add_step / _edit_step / _delete_step /
_move_step). The in-process arm is byte-identical to before; the routes call the SAME
services this module does, so the two surfaces cannot drift. Every OTHER `workflow`
verb (clone, group management, prompt authoring, ...) stays require_local until it has
an /api route.

F3 note (glossary pinning parity): `add-step --glossary` pins an approved module
glossary for a step, re-validating via gm.options_for_step exactly as the web POST
/workflows/{id}/steps does — and, now, exactly as the /api route the --remote arm
posts to does (a hard refusal on an invalid pin, so remote and in-process surface the
same clear error).

Documented in: docs/architecture/workflow-engine.md
"""

import argparse

from app import branding
from app import db as _db
from app.cli.cmd_workflow_common import (
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
    batch_result_human,
    register_cmd_workflow_common_gsub,
    register_cmd_workflow_common_psub,
    register_cmd_workflow_common_sub,
    register_cmd_workflow_common_sub_2,
    register_cmd_workflow_common_sub_3,
)
from app.cli.cmd_workflow_part2 import (
    _wf_add_step,
    _wf_clone,
    _wf_delete_step,
    _wf_edit_step,
    _wf_move_step,
    _wf_show,
    register_cmd_workflow_part2_sub,
)
from app.cli.cmd_workflow_part3 import (
    _grp_add,
    _grp_create,
    _grp_delete,
    _grp_rename,
    _grp_set_default,
    _pt_delete,
    _pt_rename,
    _wf_archive,
    _wf_author,
    _wf_author_setup,
    _wf_delete,
    _wf_rename,
    _wf_resume,
    _wf_run_batch,
    _wf_unarchive,
    batch_matrix_ascii,
    register_cmd_workflow_part3_gsub,
    register_cmd_workflow_part3_gsub_2,
    register_cmd_workflow_part3_psub,
    register_cmd_workflow_part3_sub,
    register_cmd_workflow_part3_sub_2,
)
from app.cli.cmd_workflow_part4 import (
    _batch_rerun,
    _batch_retry_failed,
    _batch_show,
    _wf_copy,
    _wf_run,
    register_cmd_workflow_part4_bsub,
    register_cmd_workflow_part4_sub,
    register_cmd_workflow_part4_sub_2,
)

from . import _remote_impl
from ._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _branch_label,
    _opts_label,
    _prompt_snippet,
    _session,
    bounded_id,
    get_scoped_or_404,
    op_org_id,
    op_org_scope,
    remote_mode,
    render_package_view,
    require_local,
    require_org_scope,
)

__all__ = [
    "EXIT_FAIL",
    "EXIT_USAGE",
    "_CELL_CODE",
    "_MAX_WAIT_HOURS",
    "CliError",
    "_ascii_cell",
    "_batch_or_usage",
    "_batch_rerun",
    "_batch_retry_failed",
    "_batch_show",
    "_branch_label",
    "_db",
    "_doc_locale",
    "_get_grp",
    "_get_pt",
    "_get_wf",
    "_grp_add",
    "_grp_create",
    "_grp_delete",
    "_grp_list",
    "_grp_rename",
    "_grp_set_default",
    "_grp_show",
    "_last_run_utc_label",
    "_load_edited_segments",
    "_opts_label",
    "_parallel_brackets",
    "_parse_branch_flags",
    "_parse_pair",
    "_prompt_snippet",
    "_pt_create",
    "_pt_delete",
    "_pt_list",
    "_pt_rename",
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
    "_wf_add_step",
    "_wf_archive",
    "_wf_author",
    "_wf_author_setup",
    "_wf_cancel_run",
    "_wf_clone",
    "_wf_copy",
    "_wf_create",
    "_wf_delete",
    "_wf_delete_step",
    "_wf_edit_step",
    "_wf_list",
    "_wf_move_step",
    "_wf_rename",
    "_wf_resume",
    "_wf_run",
    "_wf_run_batch",
    "_wf_show",
    "_wf_show_run",
    "_wf_unarchive",
    "argparse",
    "batch_matrix_ascii",
    "batch_result_human",
    "bounded_id",
    "branding",
    "get_scoped_or_404",
    "op_org_id",
    "op_org_scope",
    "remote_mode",
    "render_package_view",
    "require_local",
    "require_org_scope",
]


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    grp = subparsers.add_parser(
        "workflow",
        help="Workflows (create, list, show, add-step, delete-step, "
        "move-step, clone, author-prompt, run, resume).",
    )
    sub = grp.add_subparsers(dest="workflow_cmd", metavar="<subcommand>")
    sub.required = True

    register_cmd_workflow_common_sub(sub)

    register_cmd_workflow_part2_sub(sub)

    register_cmd_workflow_part3_sub(sub)

    register_cmd_workflow_part4_sub(sub)

    register_cmd_workflow_common_sub_2(sub)

    register_cmd_workflow_part3_sub_2(sub)

    register_cmd_workflow_common_sub_3(sub)

    # --- fan-out batch monitor + actions (SPEC-workflows-redesign Phase 5b) ---------
    # NEW leaf command paths (a `batch` group) -> new verify_cli_parity entries + docs.
    grp_batch = sub.add_parser(
        "batch",
        help="Fan-out batch runs: show the locale x steps matrix, re-run a "
        "locale, or retry every failed locale (mirrors /batches/{id}).",
    )
    bsub = grp_batch.add_subparsers(dest="batch_cmd", metavar="<subcommand>")
    bsub.required = True

    register_cmd_workflow_part4_bsub(bsub)

    # --- multilingual workflow groups (P5, item 4) ---
    grp2 = sub.add_parser("group", help="Multilingual workflow groups (create, list, show, add).")
    gsub = grp2.add_subparsers(dest="group_cmd", metavar="<subcommand>")
    gsub.required = True

    register_cmd_workflow_part3_gsub(gsub)

    register_cmd_workflow_common_gsub(gsub)

    register_cmd_workflow_part3_gsub_2(gsub)

    # --- reusable prompt templates (create, list, rename, delete) ---
    grp3 = sub.add_parser(
        "prompt", help="Reusable prompt templates (create, list, rename, delete)."
    )
    psub = grp3.add_subparsers(dest="prompt_cmd", metavar="<subcommand>")
    psub.required = True

    register_cmd_workflow_common_psub(psub)

    register_cmd_workflow_part3_psub(psub)

    # --- copy a workflow to other language pairs (optionally LLM-adapted) ---
    register_cmd_workflow_part4_sub_2(sub)
