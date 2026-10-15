"""cmd_workflow: part 3 (layered pure move)

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _wf_rename, _wf_archive, _wf_unarchive, _wf_delete,
_wf_author, _wf_author_setup, _wf_run_batch, batch_matrix_ascii, _wf_resume, _grp_create, _grp_add,
_grp_set_default, _grp_rename, _grp_delete, _pt_rename, _pt_delete from
app.cli.cmd_workflow_part3_leaf.

Documented in: docs/architecture/workflow-engine.md
"""

import argparse

from app import branding
from app import db as _db
from app.cli import _remote_impl
from app.cli._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _branch_label,
    _session,
    bounded_id,
    op_org_id,
    remote_mode,
    require_local,
    require_org_scope,
)
from app.cli.cmd_workflow_common import (
    _ascii_cell,
    _doc_locale,
    _get_grp,
    _get_pt,
    _get_wf,
    _load_edited_segments,
    _parse_pair,
    _reject_archived_customer,
    _run_doc,
    _summarize_repair_changes,
    batch_result_human,
)
from app.cli.cmd_workflow_part3_leaf import (
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
)

__all__ = [
    "EXIT_FAIL",
    "EXIT_USAGE",
    "CliError",
    "_ascii_cell",
    "_branch_label",
    "_db",
    "_doc_locale",
    "_get_grp",
    "_get_pt",
    "_get_wf",
    "_grp_add",
    "_grp_create",
    "_grp_delete",
    "_grp_rename",
    "_grp_set_default",
    "_load_edited_segments",
    "_parse_pair",
    "_pt_delete",
    "_pt_rename",
    "_reject_archived_customer",
    "_remote_impl",
    "_run_doc",
    "_session",
    "_summarize_repair_changes",
    "_wf_archive",
    "_wf_author",
    "_wf_author_setup",
    "_wf_delete",
    "_wf_rename",
    "_wf_resume",
    "_wf_run_batch",
    "_wf_unarchive",
    "batch_matrix_ascii",
    "batch_result_human",
    "op_org_id",
    "remote_mode",
    "require_local",
    "require_org_scope",
]


def register_cmd_workflow_part3_sub(sub) -> None:
    p = sub.add_parser("rename", help="Rename a workflow.")
    p.add_argument("workflow_id", type=bounded_id, help="workflow id.")
    p.add_argument("--name", required=True, help="new workflow name.")
    p.set_defaults(func=_wf_rename)

    p = sub.add_parser(
        "delete",
        help="Delete a workflow (cascades its steps + run "
        "history + fan-out batches; fails closed on external refs / in-flight runs). "
        f"A workflow any {branding.llmemory_name()} suggestion targets is permanently "
        "undeletable (those suggestions are audit tombstones that never clear); "
        "archive it instead.",
    )
    p.add_argument("workflow_id", type=bounded_id, help="workflow id.")
    p.add_argument(
        "--force",
        action="store_true",
        help="actually delete; without it, prints a preview of the cascade + any blockers.",
    )
    p.set_defaults(func=_wf_delete)

    p = sub.add_parser(
        "archive",
        help="Archive (retire) a workflow: it drops out of the "
        "default list + pickers + dispatch and is blocked from new runs; "
        "its steps + history are preserved and it is reversible.",
    )
    p.add_argument("workflow_id", type=bounded_id, help="workflow id.")
    p.set_defaults(func=_wf_archive)

    p = sub.add_parser(
        "unarchive",
        help="Unarchive (reactivate) a workflow: list it, "
        "offer it in pickers/dispatch, and make it runnable again.",
    )
    p.add_argument("workflow_id", type=bounded_id, help="workflow id.")
    p.set_defaults(func=_wf_unarchive)

    p = sub.add_parser(
        "author-prompt",
        help="Draft a step prompt from a description (AI-assisted); prints the draft.",
    )
    p.add_argument("workflow_id", type=bounded_id, help="workflow id (for context).")
    p.add_argument("description", help="plain-language description of the step.")
    p.add_argument("--provider", default="", help="provider for the authoring call.")
    p.add_argument(
        "--model",
        default="",
        help="authoring model id (blank = the provider's default; a keyed "
        "cloud provider resolves a default model automatically).",
    )
    p.set_defaults(func=_wf_author)

    p = sub.add_parser(
        "author",
        help="Draft a whole workflow (name + ordered steps) "
        "from a plain-language description (AI-assisted); "
        "prints the validated draft + warnings (SPEC-promptable-setup).",
    )
    p.add_argument("description", help="plain-language description of the workflow.")
    p.add_argument(
        "--provider",
        default="",
        help="provider for the authoring call (default: the configured "
        "default provider, same as the web dialog).",
    )
    p.add_argument(
        "--model",
        default="",
        help="authoring model id (blank = the provider's default; a keyed "
        "cloud provider resolves a default model automatically). List "
        "options with `providers models --provider <ref>`.",
    )
    p.add_argument(
        "--create",
        action="store_true",
        help="create the workflow from the draft (via the same "
        "apply_workflow_draft seam as the web confirm route; refuses "
        "if any step-level validation error remains).",
    )
    p.add_argument(
        "--adapt",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--auto-fix",
        nargs="?",
        type=int,
        const=2,
        default=None,
        metavar="N",
        help="(with --create) on a validation failure, attempt automatic "
        "repair up to N times (default N=2 when the flag is given) via the "
        "SAME repair seam as the web 'Fix this for me' button: prints each "
        "round's errors + what the repair changed, then still refuses "
        "cleanly (non-zero) if unresolved. Up to 2 provider calls per "
        "round (one JSON-parse retry).",
    )
    p.set_defaults(func=_wf_author_setup)


def register_cmd_workflow_part3_sub_2(sub) -> None:
    p = sub.add_parser("resume", help="Resume a paused run to completion (SYNCHRONOUS).")
    p.add_argument("run_id", type=bounded_id, help="workflow-run id (from `workflow run`).")
    p.add_argument(
        "--edited-text",
        default="",
        help="the human-reviewed text to "
        "continue with (blank = accept the produced MT as-is). A "
        "delimiter/segment-count mismatch is REFUSED cleanly (the run stays "
        "paused and re-resumable), never a terminal failure.",
    )
    p.add_argument(
        "--edited-segments",
        default="",
        metavar="JSON_FILE",
        help="path to a JSON file holding an array of strings (one per "
        "segment, in order) -- the safe structured alternative to "
        "--edited-text: the count is validated BEFORE any state change "
        "and the tool assembles the delimiters, so a script never "
        "hand-touches them. Read the current segments with `show-run`.",
    )
    p.add_argument("--locale", help="target locale (default: inferred from the doc).")
    p.set_defaults(func=_wf_resume)


def register_cmd_workflow_part3_gsub(gsub) -> None:
    g = gsub.add_parser("create", help="Create a multilingual workflow group.")
    g.add_argument("--name", required=True, help="group name.")
    g.add_argument("--customer-id", default="", help="customer id (blank = global group).")
    g.add_argument("--description", default="", help="optional description.")
    g.set_defaults(func=_grp_create)


def register_cmd_workflow_part3_gsub_2(gsub) -> None:
    g = gsub.add_parser(
        "add", help="Add an existing workflow to a group as a language-pair variant."
    )
    g.add_argument("workflow_id", type=bounded_id, help="workflow id to add.")
    g.add_argument("group_id", type=bounded_id, help="target group id.")
    g.add_argument(
        "--pair",
        default="",
        help="src:tgt language pair "
        "(blank = universal / group-default candidate). Refused when it "
        "would REPLACE an existing member's pair -- see --force.",
    )
    g.add_argument(
        "--default",
        dest="make_default",
        action="store_true",
        help="flag this variant as the group's fallback default.",
    )
    g.add_argument(
        "--force",
        action="store_true",
        help="allow --pair to REPLACE an already-grouped member's existing "
        "pair (reports the old pair -> new pair). Without it the "
        "replace is refused; a member with NO pair never needs it.",
    )
    g.set_defaults(func=_grp_add)

    # SPEC-workflows-redesign Phase 4a bonus: a first-class set-default command (the web
    # has POST /workflow-groups/{id}/default; the CLI had no non-`add` way to flip the
    # default without risking the pair). Takes the same <workflow> <group> positionals as
    # `add` for consistent ergonomics; validates membership before flipping.
    g = gsub.add_parser(
        "set-default", help="Make an existing group member the group's fallback default variant."
    )
    g.add_argument("workflow_id", type=bounded_id, help="workflow id (a group member).")
    g.add_argument("group_id", type=bounded_id, help="the group it belongs to.")
    g.set_defaults(func=_grp_set_default)

    g = gsub.add_parser("rename", help="Rename a workflow group.")
    g.add_argument("group_id", type=bounded_id, help="group id.")
    g.add_argument("--name", required=True, help="new group name.")
    g.set_defaults(func=_grp_rename)

    g = gsub.add_parser(
        "delete",
        help="Delete an EMPTY workflow group (fails closed while it still has member workflows).",
    )
    g.add_argument("group_id", type=bounded_id, help="group id.")
    g.add_argument(
        "--force", action="store_true", help="actually delete; without it, prints a preview."
    )
    g.set_defaults(func=_grp_delete)


def register_cmd_workflow_part3_psub(psub) -> None:
    pp = psub.add_parser("rename", help="Rename a prompt template.")
    pp.add_argument("template_id", type=bounded_id, help="prompt template id.")
    pp.add_argument("--name", required=True, help="new template name.")
    pp.set_defaults(func=_pt_rename)

    pp = psub.add_parser(
        "delete", help="Delete a prompt template (fails closed while any workflow step pins it)."
    )
    pp.add_argument("template_id", type=bounded_id, help="prompt template id.")
    pp.add_argument(
        "--force", action="store_true", help="actually delete; without it, prints a preview."
    )
    pp.set_defaults(func=_pt_delete)
