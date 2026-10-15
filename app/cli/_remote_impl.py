"""Remote (--remote) implementations of the remote-capable CLI commands (P6).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _client, _read_bytes, _write_bytes, _cd_filename,
account_show, account_set_theme, account_reset_onboarding, customer_create, customer_pairs_import,
customer_list, project_create, project_list, project_show, project_leverage, project_set_providers,
project_add_locale, _sg_body_arg, _tm_query_params, _tm_row, _TERM_META_FLAGS, _term_meta_from_args,
_gmod_row, _parse_substitution_detail, _ascii_error, _routing_rule_line, report_kinds,
_POLL_INTERVAL_S, _POLL_CEILING_S, _RUN_TERMINAL, _poll_run, _add_step_body, _edit_step_body,
_body_arg, _keys_lookup, _q_row_line, _lqa_row_line, _export_run_line, _remote_run_or_missing,
project_remove_locale, project_set_due, project_apply_langset, project_set_customer,
project_set_content_type, project_rename, project_delete, content_type_create, content_type_list,
content_type_rename, content_type_delete, style_guide_create, style_guide_list, style_guide_show,
style_guide_update, style_guide_set_status, style_guide_delete, tm_import, tm_export, tm_list,
tm_search, tm_show, tm_edit, tm_stats, tm_scrub_hedges, tm_delete, glossary_import, glossary_add,
glossary_list, glossary_export, gmod_create, gmod_list, gmod_set_customer, gmod_set_content_type,
gmod_show, gmod_approve, gmod_unapprove, gmod_set_pairs, gmod_add_term, gmod_edit_term, gmod_import,
gmod_export, gmod_delete, gmod_rename, gmod_candidates_list, gmod_extract, gmod_candidate_accept,
gmod_candidate_drop, langset_create, langset_list, langset_update, langset_delete, doc_upload,
doc_list, doc_show, doc_apply_tm, doc_translate, doc_unlock_segment, doc_approve_segment,
doc_segment_propagation, doc_export, doc_paragraphs, doc_revert_paragraph, doc_set_paragraph,
job_run, job_status, job_list, apikey_list, routing_rule_add, routing_rule_list,
routing_rule_toggle, routing_rule_delete, routing_rule_show, admin_tick, connector_pull,
connector_push, connector_sync, connector_list, report_run, workflow_list, workflow_show,
_poll_batch, workflow_create, workflow_add_step, workflow_edit_step, workflow_delete_step,
workflow_move_step, workflow_batch_show, workflow_show_run, workflow_step_diff, workflow_resume,
workflow_cancel_run, _keys_find, keys_add, keys_list, keys_import, keys_export, keys_translate,
keys_changes, keys_image_add, keys_image_list, keys_image_rm, keys_image_set_primary, audit_export,
audit_verify, workflow_clone, workflow_copy, workflow_author, workflow_group_create,
workflow_group_list, workflow_group_show, workflow_group_add, workflow_group_set_default,
prompt_template_create, prompt_template_list, system_prompt_list, system_prompt_detail,
system_prompt_set, system_prompt_reset, system_prompt_restore, template_create, template_list,
template_show, template_apply, template_rename, template_delete, image_upload, image_translate,
context_show, context_set_profile, context_set_document, context_ab, segprofile_list,
segprofile_show, segprofile_create, segprofile_clone, segprofile_rename, segprofile_delete,
segprofile_set_default, segprofile_assign, query_open, query_list, workflow_run,
workflow_batch_rerun, query_show, query_reply, query_status, query_assign, work_list, lqa_log,
lqa_findings, lqa_edit, lqa_withdraw, export_run, export_list, export_status, export_download,
config_features, config_feature_set from app.cli._remote_impl_common, app.cli._remote_impl_part2,
app.cli._remote_impl_part3, app.cli._remote_impl_part4, app.cli._remote_impl_part5,
app.cli._remote_impl_part6, app.cli._remote_impl_part7.

Each function here is the HTTP-backed twin of an in-process command body: it calls
the server's /api over the RemoteClient and emits the SAME human table / --json
shape the local arm does, so a command is transport-independent. Org scoping is done
ENTIRELY server-side by the token (never super-admin); these functions send no
--org/--operator.

Files are moved as base64 in the JSON body (upload/import) or base64 out (export),
so a single JSON transport carries binary payloads without multipart plumbing on
every route.

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64
import os
import time

from app.cli._remote_impl_common import (
    _POLL_CEILING_S,
    _POLL_INTERVAL_S,
    _RUN_TERMINAL,
    _TERM_META_FLAGS,
    _add_step_body,
    _ascii_error,
    _body_arg,
    _cd_filename,
    _client,
    _edit_step_body,
    _export_run_line,
    _gmod_row,
    _keys_lookup,
    _lqa_row_line,
    _parse_substitution_detail,
    _poll_run,
    _q_row_line,
    _read_bytes,
    _remote_run_or_missing,
    _routing_rule_line,
    _sg_body_arg,
    _term_meta_from_args,
    _tm_query_params,
    _tm_row,
    _write_bytes,
    account_reset_onboarding,
    account_set_theme,
    account_show,
    customer_create,
    customer_list,
    customer_pairs_import,
    project_add_locale,
    project_create,
    project_leverage,
    project_list,
    project_set_providers,
    project_show,
    report_kinds,
)
from app.cli._remote_impl_part2 import (
    content_type_create,
    content_type_delete,
    content_type_list,
    content_type_rename,
    glossary_add,
    glossary_export,
    glossary_import,
    glossary_list,
    gmod_add_term,
    gmod_approve,
    gmod_create,
    gmod_edit_term,
    gmod_list,
    gmod_set_content_type,
    gmod_set_customer,
    gmod_set_pairs,
    gmod_show,
    gmod_unapprove,
    project_apply_langset,
    project_delete,
    project_make_connected,
    project_remove_locale,
    project_rename,
    project_set_content_type,
    project_set_customer,
    project_set_due,
    project_set_intake,
    style_guide_create,
    style_guide_delete,
    style_guide_list,
    style_guide_set_status,
    style_guide_show,
    style_guide_update,
    tm_delete,
    tm_edit,
    tm_export,
    tm_import,
    tm_list,
    tm_scrub_hedges,
    tm_search,
    tm_show,
    tm_stats,
)
from app.cli._remote_impl_part3 import (
    admin_tick,
    apikey_list,
    connector_pull,
    connector_push,
    doc_apply_tm,
    doc_approve_segment,
    doc_export,
    doc_list,
    doc_paragraphs,
    doc_revert_paragraph,
    doc_segment_propagation,
    doc_set_paragraph,
    doc_show,
    doc_translate,
    doc_unlock_segment,
    doc_upload,
    gmod_candidate_accept,
    gmod_candidate_drop,
    gmod_candidates_list,
    gmod_delete,
    gmod_export,
    gmod_extract,
    gmod_import,
    gmod_rename,
    job_list,
    job_run,
    job_status,
    langset_create,
    langset_delete,
    langset_list,
    langset_update,
    routing_rule_add,
    routing_rule_delete,
    routing_rule_list,
    routing_rule_show,
    routing_rule_toggle,
)
from app.cli._remote_impl_part4 import (
    _poll_batch,
    connector_list,
    connector_sync,
    report_run,
    workflow_add_step,
    workflow_batch_show,
    workflow_cancel_run,
    workflow_create,
    workflow_delete_step,
    workflow_edit_step,
    workflow_list,
    workflow_move_step,
    workflow_resume,
    workflow_show,
    workflow_show_run,
    workflow_step_diff,
)
from app.cli._remote_impl_part5 import (
    _keys_find,
    keys_add,
    keys_export,
    keys_import,
    keys_list,
    keys_translate,
)
from app.cli._remote_impl_part6 import (
    audit_export,
    audit_verify,
    context_ab,
    context_set_document,
    context_set_profile,
    context_show,
    image_translate,
    image_upload,
    keys_changes,
    keys_image_add,
    keys_image_list,
    keys_image_rm,
    keys_image_set_primary,
    prompt_template_create,
    prompt_template_list,
    query_list,
    query_open,
    segprofile_assign,
    segprofile_clone,
    segprofile_create,
    segprofile_delete,
    segprofile_list,
    segprofile_rename,
    segprofile_set_default,
    segprofile_show,
    system_prompt_detail,
    system_prompt_list,
    system_prompt_reset,
    system_prompt_restore,
    system_prompt_set,
    template_apply,
    template_create,
    template_delete,
    template_list,
    template_rename,
    template_show,
    workflow_author,
    workflow_clone,
    workflow_copy,
    workflow_group_add,
    workflow_group_create,
    workflow_group_list,
    workflow_group_set_default,
    workflow_group_show,
)
from app.cli._remote_impl_part7 import (
    config_feature_set,
    config_features,
    export_download,
    export_list,
    export_run,
    export_status,
    lqa_edit,
    lqa_findings,
    lqa_log,
    lqa_withdraw,
    query_assign,
    query_reply,
    query_show,
    query_status,
    work_list,
    workflow_batch_rerun,
    workflow_run,
)

from ._common import (
    EXIT_USAGE,
    CliError,
    _branch_label,
    _opts_label,
    _prompt_snippet,
    projected_entries_sentence,
    remote_client,
    render_package_view,
)

__all__ = [
    "_POLL_CEILING_S",
    "_POLL_INTERVAL_S",
    "_RUN_TERMINAL",
    "_TERM_META_FLAGS",
    "_add_step_body",
    "_ascii_error",
    "_body_arg",
    "_branch_label",
    "_cd_filename",
    "_client",
    "_edit_step_body",
    "_export_run_line",
    "_gmod_row",
    "_keys_find",
    "_keys_lookup",
    "_lqa_row_line",
    "_opts_label",
    "_parse_substitution_detail",
    "_poll_batch",
    "_poll_run",
    "_prompt_snippet",
    "_q_row_line",
    "_read_bytes",
    "_remote_run_or_missing",
    "_routing_rule_line",
    "_sg_body_arg",
    "_term_meta_from_args",
    "_tm_query_params",
    "_tm_row",
    "_write_bytes",
    "account_reset_onboarding",
    "account_set_theme",
    "account_show",
    "admin_tick",
    "apikey_list",
    "audit_export",
    "audit_verify",
    "base64",
    "config_feature_set",
    "config_features",
    "connector_list",
    "connector_pull",
    "connector_push",
    "connector_sync",
    "content_type_create",
    "content_type_delete",
    "content_type_list",
    "content_type_rename",
    "context_ab",
    "context_set_document",
    "context_set_profile",
    "context_show",
    "customer_create",
    "customer_list",
    "customer_pairs_import",
    "doc_apply_tm",
    "doc_approve_segment",
    "doc_export",
    "doc_list",
    "doc_paragraphs",
    "doc_revert_paragraph",
    "doc_segment_propagation",
    "doc_set_paragraph",
    "doc_show",
    "doc_translate",
    "doc_unlock_segment",
    "doc_upload",
    "export_download",
    "export_list",
    "export_run",
    "export_status",
    "glossary_add",
    "glossary_export",
    "glossary_import",
    "glossary_list",
    "gmod_add_term",
    "gmod_approve",
    "gmod_candidate_accept",
    "gmod_candidate_drop",
    "gmod_candidates_list",
    "gmod_create",
    "gmod_delete",
    "gmod_edit_term",
    "gmod_export",
    "gmod_extract",
    "gmod_import",
    "gmod_list",
    "gmod_rename",
    "gmod_set_content_type",
    "gmod_set_customer",
    "gmod_set_pairs",
    "gmod_show",
    "gmod_unapprove",
    "image_translate",
    "image_upload",
    "job_list",
    "job_run",
    "job_status",
    "keys_add",
    "keys_changes",
    "keys_export",
    "keys_image_add",
    "keys_image_list",
    "keys_image_rm",
    "keys_image_set_primary",
    "keys_import",
    "keys_list",
    "keys_translate",
    "langset_create",
    "langset_delete",
    "langset_list",
    "langset_update",
    "lqa_edit",
    "lqa_findings",
    "lqa_log",
    "lqa_withdraw",
    "os",
    "project_add_locale",
    "project_apply_langset",
    "project_create",
    "project_delete",
    "project_leverage",
    "project_list",
    "project_make_connected",
    "project_remove_locale",
    "project_rename",
    "project_set_content_type",
    "project_set_customer",
    "project_set_due",
    "project_set_intake",
    "project_set_providers",
    "project_show",
    "projected_entries_sentence",
    "prompt_template_create",
    "prompt_template_list",
    "query_assign",
    "query_list",
    "query_open",
    "query_reply",
    "query_show",
    "query_status",
    "remote_client",
    "render_package_view",
    "report_kinds",
    "report_run",
    "routing_rule_add",
    "routing_rule_delete",
    "routing_rule_list",
    "routing_rule_show",
    "routing_rule_toggle",
    "segprofile_assign",
    "segprofile_clone",
    "segprofile_create",
    "segprofile_delete",
    "segprofile_list",
    "segprofile_rename",
    "segprofile_set_default",
    "segprofile_show",
    "style_guide_create",
    "style_guide_delete",
    "style_guide_list",
    "style_guide_set_status",
    "style_guide_show",
    "style_guide_update",
    "system_prompt_detail",
    "system_prompt_list",
    "system_prompt_reset",
    "system_prompt_restore",
    "system_prompt_set",
    "template_apply",
    "template_create",
    "template_delete",
    "template_list",
    "template_rename",
    "template_show",
    "time",
    "tm_delete",
    "tm_edit",
    "tm_export",
    "tm_import",
    "tm_list",
    "tm_scrub_hedges",
    "tm_search",
    "tm_show",
    "tm_stats",
    "work_list",
    "workflow_add_step",
    "workflow_author",
    "workflow_batch_rerun",
    "workflow_batch_show",
    "workflow_cancel_run",
    "workflow_clone",
    "workflow_copy",
    "workflow_create",
    "workflow_delete_step",
    "workflow_edit_step",
    "workflow_group_add",
    "workflow_group_create",
    "workflow_group_list",
    "workflow_group_set_default",
    "workflow_group_show",
    "workflow_list",
    "workflow_move_step",
    "workflow_resume",
    "workflow_run",
    "workflow_show",
    "workflow_show_run",
    "workflow_step_diff",
]


def workflow_batch_retry_failed(args, emit) -> int:
    """SPEC Phase 5b: re-run every failed locale of a batch over /api. LAUNCH-AND-POLL
    (AUDIT follow-up to Q2): the /retry-failed route now re-dispatches ASYNC. A batch with
    nothing failed is a server-side no-op (retried=[]) and returns at once."""
    c = _client(args)
    launched = c.post(f"/api/batches/{args.batch_id}/retry-failed", json={})
    from app.cli.cmd_workflow import batch_matrix_ascii

    retried = launched.get("retried")
    if not retried:
        emit.result("No failed locales to retry.", {"ok": True, **launched})
        return 0
    bid = launched.get("id")
    if bid is None:  # defensive: an unexpected body -> surface, don't poll a null id
        emit.result(str(launched), {"ok": bool(launched.get("ok")), **launched})
        return 0 if launched.get("ok") else 1
    emit.say(
        f"Batch #{bid} retry-failed launched ({len(retried)} locale(s)); polling to completion..."
    )
    body, timed_out = _poll_batch(c, bid)
    if timed_out:
        emit.result(
            f"Batch #{bid} still running (ceiling reached). Poll with:\n"
            f"  python -m app.cli --remote {args.remote} --token ... "
            f"workflow batch show {bid}",
            {"ok": True, "id": bid, "status": "running", "retried": retried, "timed_out": True},
        )
        return 0
    body = c.get(f"/api/batches/{bid}?detail=steps")  # the matrix shape the poll GET omits
    emit.result(
        batch_matrix_ascii(body), {"ok": body.get("status") != "failed", "retried": retried, **body}
    )
    return 1 if body.get("status") == "failed" else 0


def keys_set(args, emit) -> int:
    from .cmd_keys import _label

    c = _client(args)
    key, source_locale = _keys_find(c, args)
    if args.locale == source_locale:
        # The source locale means the BASE value on every seam (import, export, the
        # web grid), so it means it here too -- PATCH, not PUT.
        base = dict(key.get("base") or {})
        value = {**base, (args.form or "other"): args.text} if key.get("is_plural") else args.text
        body = c.patch("/api/keys/{}".format(key["id"]), json={"base": value})
    elif args.form:
        body = c.put(
            "/api/keys/{}/values/{}".format(key["id"], args.locale),
            json={"forms": {args.form: args.text}},
        )
    else:
        body = c.put(
            "/api/keys/{}/values/{}".format(key["id"], args.locale), json={"text": args.text}
        )
    emit.result(
        "Set {} [{}{}].".format(
            _label(args.namespace, args.name), args.locale, "/" + args.form if args.form else ""
        ),
        {"ok": True, "key": body.get("key", {})},
    )
    return 0


def keys_delete(args, emit) -> int:
    from ._common import confirm
    from .cmd_keys import _label

    c = _client(args)
    key, _source_locale = _keys_find(c, args)
    label = _label(args.namespace, args.name, args.context)
    if not confirm(emit, f"Soft-delete key {label}?", args.yes):
        raise CliError("aborted.", EXIT_USAGE)
    c.delete("/api/keys/{}".format(key["id"]))
    emit.result(
        f"Deleted {label} (tombstoned; restorable).",
        {"ok": True, "id": key["id"], "deleted": True},
    )
    return 0


def keys_restore(args, emit) -> int:
    from .cmd_keys import _label

    c = _client(args)
    key, _source_locale = _keys_find(c, args, include_deleted=True)
    label = _label(args.namespace, args.name, args.context)
    if not key.get("deleted"):
        raise CliError(f"{label} is not deleted.", EXIT_USAGE)
    body = c.post("/api/keys/{}/restore".format(key["id"]))
    emit.result(
        f"Restored {label}.",
        {"ok": True, "id": key["id"], "deleted": False, "key": body.get("key", {})},
    )
    return 0


def keys_edit(args, emit) -> int:
    from .cmd_keys import _label, edit_fields

    c = _client(args)
    fields = edit_fields(args)
    if not fields or set(fields) == {"force"}:
        raise CliError(
            "nothing to change: pass at least one of --rename, "
            "--move-namespace, --set-context, --description, --tags, "
            "--char-limit, --archive/--unarchive, --hide/--unhide, "
            "--plural/--single, --plural-arg.",
            EXIT_USAGE,
        )
    key, _source_locale = _keys_find(c, args)
    # PATCH takes exactly `keys.EDIT_FIELDS`, and `apply_key_edit` behind it validates
    # the WHOLE edit before the first write -- so the remote arm inherits the local
    # arm's all-or-nothing refusal for free rather than re-deriving it here.
    body = c.patch("/api/keys/{}".format(key["id"]), json=fields)
    emit.result(
        f"Updated {_label(args.namespace, args.name, args.context)}.",
        {"ok": True, "key": body.get("key", {})},
    )
    return 0
