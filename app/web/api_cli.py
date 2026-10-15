"""Remote-CLI /api surface (multi-org P6).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _NOT_FOUND, _FORBIDDEN, _REVIEW_FORBIDDEN,
_LQA_FINDING_FORBIDDEN, _role_block, _write_block, _lqa_finding_review_block, _manage_block,
_ai_block, _admin_block, _remote_admin_block, _gate, _gate_admin, _int_id, LIST_HARD_CAP,
PROMPT_TAP_LIST_DEFAULT, PROMPT_TAP_LIST_CAP, _list_window, _glossary_window, _list_truncate,
_cust_of, _project_in_org, _visible_document, _customer_in_org, _langset_in_org, _wf_in_org,
_creator, _org_list, _scoped_customers, _content_type_in_scope, _glossary_in_scope,
_api_workflow_customer, _grp_in_org, _WAIT_POLL_INTERVAL_S, _WAIT_POLL_CEILING_S,
_stream_wait_route, _drain_run_to_terminal, _batch_wait_response, _json, _b64, _memory_customer,
_mock_spend_block_for_doc, _ab_chosen_locale, _user_in_org, _resolve_user_ref, _r3_gate,
_lqa_finding_dict, _lqa_finding_api_error, _admin_setting_write, api_srx_enable, api_srx_disable,
api_srx_validate, api_srx_upload, _decode_b64, api_retention_config, api_retention_status,
api_retention_purge, api_admin_tick, _norm_setting_value, _masked_setting,
_write_validated_settings, api_admin_config_get, api_admin_config_set, api_ledger_envelopes,
api_ledger_purge, api_admin_features_list, api_admin_feature_set, api_admin_engine_create,
api_admin_engine_update, api_admin_engine_delete, api_admin_engine_models, api_admin_engine_test,
api_admin_provider_key_set, api_admin_provider_key_clear, api_admin_provider_key_show, _LDAP_KEYS,
_OIDC_KEYS, _SMTP_KEYS, _identity_status, api_admin_ldap_set, api_admin_ldap_status,
api_admin_oidc_set, api_admin_oidc_status, api_admin_smtp_set, api_admin_smtp_status,
_StrictBoolError, _strict_bool_field, _int_id_list, _eval_ds_in_org, _eval_cand_in_org,
_eval_customer_id, api_eval_dataset_list, api_eval_dataset_create, api_eval_dataset_import_doc,
api_eval_dataset_show, api_eval_candidate_list, api_eval_candidate_create, api_eval_run_start,
api_eval_run_show, api_eval_compare, api_eval_export, api_eval_promote, api_eval_gold_list,
api_eval_gold_seed_demo, api_eval_gold_import, _api_eval_meta_rules, api_eval_meta,
_tm_source_visible, _scoped_projects, api_projects_list, api_projects_create, api_projects_show,
api_project_budget_get, api_project_budget_set, api_project_budget_delete, api_projects_leverage,
api_projects_add_locale, api_projects_remove_locale, api_projects_set_due,
api_projects_set_content_type, api_projects_set_customer, api_projects_set_pair_providers,
api_projects_apply_langset, api_projects_rename, api_projects_archive, api_projects_unarchive,
api_projects_delete, api_projects_status, api_projects_export, api_project_tm_sources_list,
api_project_tm_sources_add, api_project_tm_sources_remove, api_project_glossaries_list,
api_project_glossaries_add, api_project_glossaries_remove, api_documents_list, api_tm_import,
api_tm_export, api_glossary_import, api_glossary_add, api_glossary_list, api_glossary_export,
api_member_add, api_member_remove, api_member_list, api_customers_list, api_customers_create,
api_customers_rename, api_customers_delete, api_customer_pairs_import, api_customers_archive,
api_customers_unarchive, api_customer_detail, api_customer_settings, api_customer_defaults,
api_customer_pairs_add, api_customer_pairs_remove, api_vendors_list, api_vendors_create,
api_vendor_dispatches, api_vendor_dispatch_detail, api_vendor_dispatch_xliff, _read_body_capped,
api_vendor_return, api_vendor_poll, _leg_or_404, api_vendor_quote_record, api_vendor_quote_approve,
api_vendor_quote_decline, api_vendor_reject_rework, api_vendors_show, api_vendors_update,
api_vendors_rate_card, api_vendors_link_customer, api_vendors_unlink_customer, api_vendors_delete,
api_vendor_secret_set, api_glossaries_list, api_glossary_module_create,
api_glossary_module_set_content_type, api_glossary_module_rename, api_glossary_module_set_customer,
api_glossary_module_show, api_glossary_module_export, api_glossary_module_approve,
api_glossary_module_unapprove, api_glossary_module_set_pairs, api_glossary_module_add_term,
api_glossary_module_edit_term, _CONCEPT_FIELDS, _TERM_FIELDS, _concept_fields, _term_fields,
api_glossary_concepts_list, api_glossary_concepts_create, api_glossary_concepts_edit,
api_glossary_concepts_delete, api_glossary_concepts_add_term, api_glossary_concepts_edit_term,
api_glossary_concepts_delete_term, api_glossary_concepts_set_audience, api_glossary_module_import,
api_glossary_module_delete, api_glossary_module_extract, api_glossary_module_candidates,
api_glossary_module_candidate_accept, api_glossary_module_candidate_drop, _review_block,
api_segment_unlock, api_segment_approve, api_segment_propagation, _onboarding_state,
api_account_get, api_account_reset_onboarding, _SEGMENT_SURFACED, _segment_detail_row,
api_document_segments, api_document_apply_tm, api_document_rename, api_document_delete,
api_lqa_findings_list, api_lqa_finding_log, api_content_types_list, api_content_types_create,
api_content_types_rename, api_content_types_delete, api_segprofiles_rename, api_segprofiles_list,
api_segprofiles_show, api_segprofiles_create, api_segprofiles_clone, api_segprofiles_set_default,
api_segprofiles_delete, api_segprofiles_assign, api_workflows_list, api_workflows_show,
api_workflows_rename, api_workflows_delete, api_workflows_archive, api_workflows_unarchive,
_api_wf_step_or_none, _api_glossary_pin, _api_prompt_template_visible, _api_branch,
api_workflows_create, api_workflows_add_step, api_workflows_edit_step, api_workflows_delete_step,
api_workflows_move_step, _run_workflow_blocking, _run_batch_blocking, api_workflows_run,
api_workflows_run_batch, _mock_spend_block, api_workflows_clone, api_workflows_copy,
api_workflows_author, _style_guide_in_scope, api_style_guides_list, api_style_guides_create,
api_style_guides_show, api_style_guides_update, api_style_guides_set_status,
api_style_guides_delete, api_style_guides_ab, api_langsets_list, api_langsets_create,
api_langsets_update, api_langsets_delete, api_workflow_groups_rename, api_workflow_groups_delete,
api_workflow_groups_create, api_workflow_groups_list, api_workflow_groups_show,
api_workflow_groups_add, api_workflow_groups_set_default, _pt_in_org, api_prompt_templates_rename,
api_prompt_templates_delete, api_prompt_templates_create, api_prompt_templates_list,
_rerun_batch_blocking, api_batches_show, api_batches_rerun, api_batches_retry_failed, api_runs_show,
api_runs_resume, api_runs_cancel, _first_locale, api_prompt_tap_actions,
api_prompt_tap_action_summary, api_tap_runs, api_tap_run_detail, api_jobs_run, api_jobs_list,
_payload_key_document_block, _payload_in_org, _memory_in_scope, _llmemory_disabled,
_llmemory_off_block, _memory_list_filters, api_memories_list, api_memories_export,
api_memories_show, api_memories_events, api_memories_note, api_memories_invalidate,
api_memories_invalidate_since, api_memories_purge, api_memories_distill, api_memories_janitor,
api_memories_resolve_conflict, api_routing_rule_show, api_routing_rule_add, api_routing_rule_list,
api_routing_rule_toggle, api_routing_rule_delete, api_apikeys_list, api_apikey_issue,
api_apikey_revoke, api_apikey_revoke_all, api_reports_run, _tm_pids, _tm_browse,
_needs_customer_payload, _tm_int, _tm_date, _tm_content_type_filter, _tm_filters_from_qp,
api_tm_list, api_tm_stats, api_tm_show, api_tm_delete, api_tm_edit, api_tm_bulk_delete,
api_tm_scrub_hedges, _user_row, api_user_create, api_user_list, api_user_invites,
api_user_invite_revoke, api_user_show, api_user_role, api_user_department, api_user_customer,
api_user_enable, api_user_disable, _user_set_active, api_user_reset_password, api_user_invite,
api_user_export, api_user_erase, _suggestion_in_scope, _suggestion_target_ok, _suggestions_disabled,
_suggestions_off_block, api_suggestions_list, api_suggestions_export, api_suggestions_show,
api_suggestions_evidence, api_suggestions_generate, api_suggestions_approve, api_suggestions_reject,
api_suggestions_apply, api_suggestions_retry, api_suggestions_recover, _template_in_scope,
api_templates_list, api_templates_show, api_templates_create, api_templates_apply,
api_templates_rename, api_templates_delete, api_context_ab, api_context_set_profile,
api_context_set_document, api_context_resolve, api_budget_set, api_budget_list, api_budget_delete,
api_lqa_rulebook_get, api_lqa_rulebook_set, api_lqa_finding_edit, api_lqa_finding_withdraw,
api_audit_export, api_audit_verify, api_language_enable, api_language_disable, _language_toggle,
_connection_in_org, api_connector_secret_set, api_connector_oauth_secret_set,
api_connector_token_regen, _qthread_row_dict, _qthread_dict, _qpost_dict, _query_api_error,
_query_notify, api_queries_list, api_query_show, api_query_open, api_query_post, api_query_status,
api_query_assign, _worklist_item_dict, api_worklist, _export_scope_set, _export_run_view,
_export_owns, api_exports_start, api_exports_list, api_exports_status, _parse_range,
api_exports_artifact from app.web.api_cli_common, app.web.api_cli_admin_1, app.web.api_cli_admin_2,
app.web.api_cli_evaluations_1, app.web.api_cli_evaluations_2, app.web.api_cli_projects_1,
app.web.api_cli_projects_2, app.web.api_cli_customers, app.web.api_cli_vendors,
app.web.api_cli_glossaries_1, app.web.api_cli_glossaries_2, app.web.api_cli_misc_1,
app.web.api_cli_workflows_1, app.web.api_cli_workflows_2, app.web.api_cli_misc_2,
app.web.api_cli_misc_3, app.web.api_cli_memories, app.web.api_cli_misc_4, app.web.api_cli_users,
app.web.api_cli_misc_5, app.web.api_cli_misc_6, app.web.api_cli_misc_7.

These endpoints are the WEB SIDE of the remote-capable CLI commands (the
web+CLI parity rule): `loc-tms --remote URL --token lsu_...` calls them over
HTTP, and the in-process CLI arm calls the SAME service functions locally, so a
command behaves identically over either transport. They fill the /api gaps the
existing api.py (documents/connectors/jobs/languages/formats) did not cover:
project + customer CRUD, TM import/export, glossary import/add/list, Language
Set CRUD, workflow run/resume/list/show, and job run/list.

AUTHZ + ORG BINDING (P4 posture, reused verbatim):
  * every route requires a per-user API key (kind == "user") carrying the "docs"
    scope — deps._require_doc_scope. The shared connector token (lsk_) is rejected
    outright, and a plain "api" key is 403. There is no super-admin bypass: a
    super-admin's lsu_ key is bound to its HOME org exactly like anyone's (the
    linchpin), so remote mode is NEVER super-admin.
  * every row is org-bound via deps._ident_org_id(ident): lists filter to the
    caller's org; a fetch/mutation of a row in another org 404s (never 403 — an id
    in another org must be indistinguishable from a missing one). A strict no-op
    with the sacred gate OFF (single-tenant), so behavior is byte-identical to today.

No business logic lives here: each route is a thin shell over the SAME services the
web forms + in-process CLI use (content.py, tm.py, glossary.py, languages.py,
workflows.py, orchestrator.py, jobs.run_sync). Runs that the in-process CLI executes
SYNCHRONOUSLY (job run, workflow run/resume) run synchronously here too, so a remote
`job run` / `workflow run` returns finished state just like the local one.

Documented in: docs/architecture/surfaces-and-parity.md
"""

import base64
import json
import math
from copy import copy as _srp_copy
from functools import wraps

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app import branding
from app import db as _db
from app.db import engine
from app.extensions import addon_missing as _optional_addon_missing
from app.kernel import (
    access,
    audit,
    batch_runs,
    content,
    content_types,
    glossary,
    job_history,
    jobs,
    languages,
    lqa_safety,
    onboarding,
    orchestrator,
    output_guard,
    pair_providers,
    quality,
    refusal,
    run_overlay,
    security,
    segmentation_profiles,
    tm,
    vendors,
    workflows,
    worklist,
)
from app.kernel import ids as ids
from app.kernel import keys as keys_svc
from app.kernel import query_threads as qthreads
from app.kernel import workflow_shapes as wshapes
from app.models import (
    Customer,
    Document,
    Glossary,
    JobStatus,
    MemoryEntry,
    Project,
    RunStatus,
    Segment,
    StepRun,
    StepType,
    TargetLocale,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.web import deps
from app.web.api_cli_admin_1 import (
    _admin_setting_write,
    _decode_b64,
    _masked_setting,
    _norm_setting_value,
    _write_validated_settings,
    api_admin_config_get,
    api_admin_config_set,
    api_admin_tick,
    api_ledger_envelopes,
    api_ledger_purge,
    api_retention_config,
    api_retention_purge,
    api_retention_status,
    api_srx_disable,
    api_srx_enable,
    api_srx_upload,
    api_srx_validate,
)
from app.web.api_cli_admin_1 import router as api_cli_admin_1_router
from app.web.api_cli_admin_2 import (
    _LDAP_KEYS,
    _OIDC_KEYS,
    _SMTP_KEYS,
    _identity_status,
    api_admin_engine_create,
    api_admin_engine_delete,
    api_admin_engine_models,
    api_admin_engine_test,
    api_admin_engine_update,
    api_admin_feature_set,
    api_admin_features_list,
    api_admin_ldap_set,
    api_admin_ldap_status,
    api_admin_oidc_set,
    api_admin_oidc_status,
    api_admin_provider_key_clear,
    api_admin_provider_key_set,
    api_admin_provider_key_show,
    api_admin_smtp_set,
    api_admin_smtp_status,
)
from app.web.api_cli_admin_2 import router as api_cli_admin_2_router
from app.web.api_cli_common import (
    _FORBIDDEN,
    _LQA_FINDING_FORBIDDEN,
    _NOT_FOUND,
    _REVIEW_FORBIDDEN,
    _WAIT_POLL_CEILING_S,
    _WAIT_POLL_INTERVAL_S,
    LIST_HARD_CAP,
    PROMPT_TAP_LIST_CAP,
    PROMPT_TAP_LIST_DEFAULT,
    _ab_chosen_locale,
    _admin_block,
    _ai_block,
    _api_workflow_customer,
    _b64,
    _batch_wait_response,
    _content_type_in_scope,
    _creator,
    _cust_of,
    _customer_in_org,
    _drain_run_to_terminal,
    _gate,
    _gate_admin,
    _glossary_in_scope,
    _glossary_window,
    _grp_in_org,
    _int_id,
    _json,
    _langset_in_org,
    _list_truncate,
    _list_window,
    _lqa_finding_api_error,
    _lqa_finding_dict,
    _lqa_finding_review_block,
    _manage_block,
    _memory_customer,
    _mock_spend_block_for_doc,
    _org_list,
    _project_in_org,
    _r3_gate,
    _remote_admin_block,
    _resolve_user_ref,
    _role_block,
    _scoped_customers,
    _stream_wait_route,
    _user_in_org,
    _visible_document,
    _wf_in_org,
    _write_block,
)
from app.web.api_cli_customers import (
    api_customer_defaults,
    api_customer_detail,
    api_customer_pairs_add,
    api_customer_pairs_import,
    api_customer_pairs_remove,
    api_customer_settings,
    api_customers_archive,
    api_customers_create,
    api_customers_delete,
    api_customers_list,
    api_customers_rename,
    api_customers_unarchive,
)
from app.web.api_cli_customers import router as api_cli_customers_router

try:
    from app.web.api_cli_evaluations_1 import (
        _eval_cand_in_org,
        _eval_customer_id,
        _eval_ds_in_org,
        _int_id_list,
        _strict_bool_field,
        _StrictBoolError,
        api_eval_candidate_create,
        api_eval_candidate_list,
        api_eval_compare,
        api_eval_dataset_create,
        api_eval_dataset_import_doc,
        api_eval_dataset_list,
        api_eval_dataset_show,
        api_eval_export,
        api_eval_gold_import,
        api_eval_gold_list,
        api_eval_gold_seed_demo,
        api_eval_promote,
        api_eval_run_show,
        api_eval_run_start,
    )
    from app.web.api_cli_evaluations_1 import router as api_cli_evaluations_1_router
except ModuleNotFoundError as _opt_exc:  # optional add-on absent (app/editions.py)
    if not _optional_addon_missing(_opt_exc, "app.web.api_cli_evaluations_1"):
        raise
    api_cli_evaluations_1_router = None
try:
    from app.web.api_cli_evaluations_2 import _api_eval_meta_rules, api_eval_meta
    from app.web.api_cli_evaluations_2 import router as api_cli_evaluations_2_router
except ModuleNotFoundError as _opt_exc:  # optional add-on absent (app/editions.py)
    if not _optional_addon_missing(_opt_exc, "app.web.api_cli_evaluations_2"):
        raise
    api_cli_evaluations_2_router = None
from app.web.api_cli_glossaries_1 import (
    _CONCEPT_FIELDS,
    _TERM_FIELDS,
    _concept_fields,
    _term_fields,
    api_glossaries_list,
    api_glossary_concepts_add_term,
    api_glossary_concepts_create,
    api_glossary_concepts_delete,
    api_glossary_concepts_delete_term,
    api_glossary_concepts_edit,
    api_glossary_concepts_edit_term,
    api_glossary_concepts_list,
    api_glossary_concepts_set_audience,
    api_glossary_module_add_term,
    api_glossary_module_approve,
    api_glossary_module_create,
    api_glossary_module_delete,
    api_glossary_module_edit_term,
    api_glossary_module_export,
    api_glossary_module_import,
    api_glossary_module_rename,
    api_glossary_module_set_content_type,
    api_glossary_module_set_customer,
    api_glossary_module_set_pairs,
    api_glossary_module_show,
    api_glossary_module_unapprove,
)
from app.web.api_cli_glossaries_1 import router as api_cli_glossaries_1_router
from app.web.api_cli_glossaries_2 import (
    api_glossary_module_candidate_accept,
    api_glossary_module_candidate_drop,
    api_glossary_module_candidates,
    api_glossary_module_extract,
)
from app.web.api_cli_glossaries_2 import router as api_cli_glossaries_2_router

try:
    from app.web.api_cli_memories import (
        _llmemory_disabled,
        _llmemory_off_block,
        _memory_in_scope,
        _memory_list_filters,
        api_memories_distill,
        api_memories_events,
        api_memories_export,
        api_memories_invalidate,
        api_memories_invalidate_since,
        api_memories_janitor,
        api_memories_list,
        api_memories_note,
        api_memories_purge,
        api_memories_resolve_conflict,
        api_memories_show,
    )
    from app.web.api_cli_memories import router as api_cli_memories_router
except ModuleNotFoundError as _opt_exc:  # optional add-on absent (app/editions.py)
    if not _optional_addon_missing(_opt_exc, "app.web.api_cli_memories"):
        raise
    api_cli_memories_router = None
from app.web.api_cli_misc_1 import (
    _SEGMENT_SURFACED,
    _onboarding_state,
    _review_block,
    _segment_detail_row,
    api_account_get,
    api_account_reset_onboarding,
    api_content_types_create,
    api_content_types_delete,
    api_content_types_list,
    api_content_types_rename,
    api_document_apply_tm,
    api_document_delete,
    api_document_rename,
    api_document_segments,
    api_lqa_finding_log,
    api_lqa_findings_list,
    api_segment_approve,
    api_segment_propagation,
    api_segment_unlock,
    api_segprofiles_assign,
    api_segprofiles_clone,
    api_segprofiles_create,
    api_segprofiles_delete,
    api_segprofiles_list,
    api_segprofiles_rename,
    api_segprofiles_set_default,
    api_segprofiles_show,
)
from app.web.api_cli_misc_1 import router as api_cli_misc_1_router
from app.web.api_cli_misc_2 import (
    _pt_in_org,
    _style_guide_in_scope,
    api_langsets_create,
    api_langsets_delete,
    api_langsets_list,
    api_langsets_update,
    api_prompt_templates_create,
    api_prompt_templates_delete,
    api_prompt_templates_list,
    api_prompt_templates_rename,
    api_style_guides_ab,
    api_style_guides_create,
    api_style_guides_delete,
    api_style_guides_list,
    api_style_guides_set_status,
    api_style_guides_show,
    api_style_guides_update,
    api_workflow_groups_add,
    api_workflow_groups_create,
    api_workflow_groups_delete,
    api_workflow_groups_list,
    api_workflow_groups_rename,
    api_workflow_groups_set_default,
    api_workflow_groups_show,
)
from app.web.api_cli_misc_2 import router as api_cli_misc_2_router
from app.web.api_cli_misc_3 import (
    _first_locale,
    _payload_in_org,
    _payload_key_document_block,
    _rerun_batch_blocking,
    api_batches_rerun,
    api_batches_retry_failed,
    api_batches_show,
    api_jobs_list,
    api_jobs_run,
    api_prompt_tap_action_summary,
    api_prompt_tap_actions,
    api_runs_cancel,
    api_runs_resume,
    api_runs_show,
    api_tap_run_detail,
    api_tap_runs,
)
from app.web.api_cli_misc_3 import router as api_cli_misc_3_router
from app.web.api_cli_misc_4 import (
    _needs_customer_payload,
    _tm_browse,
    _tm_content_type_filter,
    _tm_date,
    _tm_filters_from_qp,
    _tm_int,
    _tm_pids,
    api_apikey_issue,
    api_apikey_revoke,
    api_apikey_revoke_all,
    api_apikeys_list,
    api_reports_run,
    api_routing_rule_add,
    api_routing_rule_delete,
    api_routing_rule_list,
    api_routing_rule_show,
    api_routing_rule_toggle,
    api_tm_bulk_delete,
    api_tm_delete,
    api_tm_edit,
    api_tm_list,
    api_tm_scrub_hedges,
    api_tm_show,
    api_tm_stats,
)
from app.web.api_cli_misc_4 import router as api_cli_misc_4_router
from app.web.api_cli_misc_5 import (
    _template_in_scope,
    api_budget_delete,
    api_budget_list,
    api_budget_set,
    api_context_ab,
    api_context_resolve,
    api_context_set_document,
    api_context_set_profile,
    api_templates_apply,
    api_templates_create,
    api_templates_delete,
    api_templates_list,
    api_templates_rename,
    api_templates_show,
)
from app.web.api_cli_misc_5 import router as api_cli_misc_5_router

try:
    from app.web.api_cli_memory_suggestions import (
        _suggestion_in_scope,
        _suggestion_target_ok,
        _suggestions_disabled,
        _suggestions_off_block,
        api_suggestions_apply,
        api_suggestions_approve,
        api_suggestions_evidence,
        api_suggestions_export,
        api_suggestions_generate,
        api_suggestions_list,
        api_suggestions_recover,
        api_suggestions_reject,
        api_suggestions_retry,
        api_suggestions_show,
    )
    from app.web.api_cli_memory_suggestions import router as api_cli_memory_suggestions_router
except ModuleNotFoundError as _opt_exc:  # optional add-on absent (app/editions.py)
    if not _optional_addon_missing(_opt_exc, "app.web.api_cli_memory_suggestions"):
        raise
    api_cli_memory_suggestions_router = None
from app.web.api_cli_misc_6 import (
    _connection_in_org,
    _language_toggle,
    _qpost_dict,
    _qthread_dict,
    _qthread_row_dict,
    _query_api_error,
    _query_notify,
    _worklist_item_dict,
    api_audit_export,
    api_audit_verify,
    api_connector_oauth_secret_set,
    api_connector_secret_set,
    api_connector_token_regen,
    api_language_disable,
    api_language_enable,
    api_lqa_finding_edit,
    api_lqa_finding_withdraw,
    api_lqa_rulebook_get,
    api_lqa_rulebook_set,
    api_queries_list,
    api_query_assign,
    api_query_open,
    api_query_post,
    api_query_show,
    api_query_status,
    api_worklist,
)
from app.web.api_cli_misc_6 import router as api_cli_misc_6_router
from app.web.api_cli_misc_7 import (
    _export_owns,
    _export_run_view,
    _export_scope_set,
    _parse_range,
    api_exports_artifact,
    api_exports_list,
    api_exports_start,
    api_exports_status,
)
from app.web.api_cli_misc_7 import router as api_cli_misc_7_router
from app.web.api_cli_projects_1 import (
    _scoped_projects,
    _tm_source_visible,
    api_project_budget_delete,
    api_project_budget_get,
    api_project_budget_set,
    api_projects_add_locale,
    api_projects_apply_langset,
    api_projects_archive,
    api_projects_create,
    api_projects_delete,
    api_projects_leverage,
    api_projects_list,
    api_projects_remove_locale,
    api_projects_rename,
    api_projects_set_content_type,
    api_projects_set_customer,
    api_projects_set_due,
    api_projects_set_pair_providers,
    api_projects_show,
    api_projects_status,
    api_projects_unarchive,
)
from app.web.api_cli_projects_1 import router as api_cli_projects_1_router
from app.web.api_cli_projects_2 import (
    api_documents_list,
    api_glossary_add,
    api_glossary_export,
    api_glossary_import,
    api_glossary_list,
    api_member_add,
    api_member_list,
    api_member_remove,
    api_project_glossaries_add,
    api_project_glossaries_list,
    api_project_glossaries_remove,
    api_project_tm_sources_add,
    api_project_tm_sources_list,
    api_project_tm_sources_remove,
    api_projects_export,
    api_tm_export,
    api_tm_import,
)
from app.web.api_cli_projects_2 import router as api_cli_projects_2_router
from app.web.api_cli_users import (
    _user_row,
    _user_set_active,
    api_user_create,
    api_user_customer,
    api_user_department,
    api_user_disable,
    api_user_enable,
    api_user_erase,
    api_user_export,
    api_user_invite,
    api_user_invite_revoke,
    api_user_invites,
    api_user_list,
    api_user_reset_password,
    api_user_role,
    api_user_show,
)
from app.web.api_cli_users import router as api_cli_users_router
from app.web.api_cli_vendors import (
    _leg_or_404,
    _read_body_capped,
    api_vendor_dispatch_detail,
    api_vendor_dispatch_xliff,
    api_vendor_dispatches,
    api_vendor_poll,
    api_vendor_quote_approve,
    api_vendor_quote_decline,
    api_vendor_quote_record,
    api_vendor_reject_rework,
    api_vendor_return,
    api_vendor_secret_set,
    api_vendors_create,
    api_vendors_delete,
    api_vendors_link_customer,
    api_vendors_list,
    api_vendors_rate_card,
    api_vendors_show,
    api_vendors_unlink_customer,
    api_vendors_update,
)
from app.web.api_cli_vendors import router as api_cli_vendors_router
from app.web.api_cli_workflows_1 import (
    _api_branch,
    _api_glossary_pin,
    _api_prompt_template_visible,
    _api_wf_step_or_none,
    api_workflows_add_step,
    api_workflows_archive,
    api_workflows_create,
    api_workflows_delete,
    api_workflows_edit_step,
    api_workflows_list,
    api_workflows_rename,
    api_workflows_show,
    api_workflows_unarchive,
)
from app.web.api_cli_workflows_1 import router as api_cli_workflows_1_router
from app.web.api_cli_workflows_2 import (
    _mock_spend_block,
    _run_batch_blocking,
    _run_workflow_blocking,
    api_workflows_author,
    api_workflows_clone,
    api_workflows_copy,
    api_workflows_delete_step,
    api_workflows_move_step,
    api_workflows_run,
    api_workflows_run_batch,
)
from app.web.api_cli_workflows_2 import router as api_cli_workflows_2_router
from app.web.streaming import StreamingResponse, retry_after, stream_slot
from app.web.streaming import run_sync as run_in_threadpool

__all__ = [
    "LIST_HARD_CAP",
    "PROMPT_TAP_LIST_CAP",
    "PROMPT_TAP_LIST_DEFAULT",
    "_CONCEPT_FIELDS",
    "_FORBIDDEN",
    "_LDAP_KEYS",
    "_LQA_FINDING_FORBIDDEN",
    "_NOT_FOUND",
    "_OIDC_KEYS",
    "_REVIEW_FORBIDDEN",
    "_SEGMENT_SURFACED",
    "_SMTP_KEYS",
    "_TERM_FIELDS",
    "_WAIT_POLL_CEILING_S",
    "_WAIT_POLL_INTERVAL_S",
    "Body",
    "Customer",
    "Document",
    "Glossary",
    "JSONResponse",
    "JobStatus",
    "MemoryEntry",
    "Project",
    "Query",
    "RedirectResponse",
    "Request",
    "RunStatus",
    "Segment",
    "Session",
    "StepRun",
    "StepType",
    "StreamingResponse",
    "TargetLocale",
    "Workflow",
    "WorkflowBatch",
    "WorkflowRun",
    "WorkflowStep",
    "_StrictBoolError",
    "_ab_chosen_locale",
    "_admin_block",
    "_admin_setting_write",
    "_ai_block",
    "_api_branch",
    "_api_eval_meta_rules",
    "_api_glossary_pin",
    "_api_prompt_template_visible",
    "_api_wf_step_or_none",
    "_api_workflow_customer",
    "_b64",
    "_batch_wait_response",
    "_concept_fields",
    "_connection_in_org",
    "_content_type_in_scope",
    "_creator",
    "_cust_of",
    "_customer_in_org",
    "_db",
    "_decode_b64",
    "_drain_run_to_terminal",
    "_eval_cand_in_org",
    "_eval_customer_id",
    "_eval_ds_in_org",
    "_export_owns",
    "_export_run_view",
    "_export_scope_set",
    "_first_locale",
    "_gate",
    "_gate_admin",
    "_glossary_in_scope",
    "_glossary_window",
    "_grp_in_org",
    "_identity_status",
    "_int_id",
    "_int_id_list",
    "_json",
    "_langset_in_org",
    "_language_toggle",
    "_leg_or_404",
    "_list_truncate",
    "_list_window",
    "_llmemory_disabled",
    "_llmemory_off_block",
    "_lqa_finding_api_error",
    "_lqa_finding_dict",
    "_lqa_finding_review_block",
    "_manage_block",
    "_masked_setting",
    "_memory_customer",
    "_memory_in_scope",
    "_memory_list_filters",
    "_mock_spend_block",
    "_mock_spend_block_for_doc",
    "_needs_customer_payload",
    "_norm_setting_value",
    "_onboarding_state",
    "_org_list",
    "_parse_range",
    "_payload_in_org",
    "_payload_key_document_block",
    "_project_in_org",
    "_pt_in_org",
    "_qpost_dict",
    "_qthread_dict",
    "_qthread_row_dict",
    "_query_api_error",
    "_query_notify",
    "_r3_gate",
    "_read_body_capped",
    "_remote_admin_block",
    "_rerun_batch_blocking",
    "_resolve_user_ref",
    "_review_block",
    "_role_block",
    "_run_batch_blocking",
    "_run_workflow_blocking",
    "_scoped_customers",
    "_scoped_projects",
    "_segment_detail_row",
    "_stream_wait_route",
    "_strict_bool_field",
    "_style_guide_in_scope",
    "_suggestion_in_scope",
    "_suggestion_target_ok",
    "_suggestions_disabled",
    "_suggestions_off_block",
    "_template_in_scope",
    "_term_fields",
    "_tm_browse",
    "_tm_content_type_filter",
    "_tm_date",
    "_tm_filters_from_qp",
    "_tm_int",
    "_tm_pids",
    "_tm_source_visible",
    "_user_in_org",
    "_user_row",
    "_user_set_active",
    "_visible_document",
    "_wf_in_org",
    "_worklist_item_dict",
    "_write_block",
    "_write_validated_settings",
    "access",
    "api_account_get",
    "api_account_reset_onboarding",
    "api_admin_config_get",
    "api_admin_config_set",
    "api_admin_engine_create",
    "api_admin_engine_delete",
    "api_admin_engine_models",
    "api_admin_engine_test",
    "api_admin_engine_update",
    "api_admin_feature_set",
    "api_admin_features_list",
    "api_admin_ldap_set",
    "api_admin_ldap_status",
    "api_admin_oidc_set",
    "api_admin_oidc_status",
    "api_admin_provider_key_clear",
    "api_admin_provider_key_set",
    "api_admin_provider_key_show",
    "api_admin_smtp_set",
    "api_admin_smtp_status",
    "api_admin_tick",
    "api_apikey_issue",
    "api_apikey_revoke",
    "api_apikey_revoke_all",
    "api_apikeys_list",
    "api_audit_export",
    "api_audit_verify",
    "api_batches_rerun",
    "api_batches_retry_failed",
    "api_batches_show",
    "api_budget_delete",
    "api_budget_list",
    "api_budget_set",
    "api_connector_oauth_secret_set",
    "api_connector_secret_set",
    "api_connector_token_regen",
    "api_content_types_create",
    "api_content_types_delete",
    "api_content_types_list",
    "api_content_types_rename",
    "api_context_ab",
    "api_context_resolve",
    "api_context_set_document",
    "api_context_set_profile",
    "api_customer_defaults",
    "api_customer_detail",
    "api_customer_pairs_add",
    "api_customer_pairs_import",
    "api_customer_pairs_remove",
    "api_customer_settings",
    "api_customers_archive",
    "api_customers_create",
    "api_customers_delete",
    "api_customers_list",
    "api_customers_rename",
    "api_customers_unarchive",
    "api_document_apply_tm",
    "api_document_delete",
    "api_document_rename",
    "api_document_segments",
    "api_documents_list",
    "api_eval_candidate_create",
    "api_eval_candidate_list",
    "api_eval_compare",
    "api_eval_dataset_create",
    "api_eval_dataset_import_doc",
    "api_eval_dataset_list",
    "api_eval_dataset_show",
    "api_eval_export",
    "api_eval_gold_import",
    "api_eval_gold_list",
    "api_eval_gold_seed_demo",
    "api_eval_meta",
    "api_eval_promote",
    "api_eval_run_show",
    "api_eval_run_start",
    "api_exports_artifact",
    "api_exports_list",
    "api_exports_start",
    "api_exports_status",
    "api_glossaries_list",
    "api_glossary_add",
    "api_glossary_concepts_add_term",
    "api_glossary_concepts_create",
    "api_glossary_concepts_delete",
    "api_glossary_concepts_delete_term",
    "api_glossary_concepts_edit",
    "api_glossary_concepts_edit_term",
    "api_glossary_concepts_list",
    "api_glossary_concepts_set_audience",
    "api_glossary_export",
    "api_glossary_import",
    "api_glossary_list",
    "api_glossary_module_add_term",
    "api_glossary_module_approve",
    "api_glossary_module_candidate_accept",
    "api_glossary_module_candidate_drop",
    "api_glossary_module_candidates",
    "api_glossary_module_create",
    "api_glossary_module_delete",
    "api_glossary_module_edit_term",
    "api_glossary_module_export",
    "api_glossary_module_extract",
    "api_glossary_module_import",
    "api_glossary_module_rename",
    "api_glossary_module_set_content_type",
    "api_glossary_module_set_customer",
    "api_glossary_module_set_pairs",
    "api_glossary_module_show",
    "api_glossary_module_unapprove",
    "api_jobs_list",
    "api_jobs_run",
    "api_langsets_create",
    "api_langsets_delete",
    "api_langsets_list",
    "api_langsets_update",
    "api_language_disable",
    "api_language_enable",
    "api_ledger_envelopes",
    "api_ledger_purge",
    "api_lqa_finding_edit",
    "api_lqa_finding_log",
    "api_lqa_finding_withdraw",
    "api_lqa_findings_list",
    "api_lqa_rulebook_get",
    "api_lqa_rulebook_set",
    "api_member_add",
    "api_member_list",
    "api_member_remove",
    "api_memories_distill",
    "api_memories_events",
    "api_memories_export",
    "api_memories_invalidate",
    "api_memories_invalidate_since",
    "api_memories_janitor",
    "api_memories_list",
    "api_memories_note",
    "api_memories_purge",
    "api_memories_resolve_conflict",
    "api_memories_show",
    "api_project_budget_delete",
    "api_project_budget_get",
    "api_project_budget_set",
    "api_project_glossaries_add",
    "api_project_glossaries_list",
    "api_project_glossaries_remove",
    "api_project_tm_sources_add",
    "api_project_tm_sources_list",
    "api_project_tm_sources_remove",
    "api_projects_add_locale",
    "api_projects_apply_langset",
    "api_projects_archive",
    "api_projects_create",
    "api_projects_delete",
    "api_projects_export",
    "api_projects_leverage",
    "api_projects_list",
    "api_projects_remove_locale",
    "api_projects_rename",
    "api_projects_set_content_type",
    "api_projects_set_customer",
    "api_projects_set_due",
    "api_projects_set_pair_providers",
    "api_projects_show",
    "api_projects_status",
    "api_projects_unarchive",
    "api_prompt_tap_action_summary",
    "api_prompt_tap_actions",
    "api_prompt_templates_create",
    "api_prompt_templates_delete",
    "api_prompt_templates_list",
    "api_prompt_templates_rename",
    "api_queries_list",
    "api_query_assign",
    "api_query_open",
    "api_query_post",
    "api_query_show",
    "api_query_status",
    "api_reports_run",
    "api_retention_config",
    "api_retention_purge",
    "api_retention_status",
    "api_routing_rule_add",
    "api_routing_rule_delete",
    "api_routing_rule_list",
    "api_routing_rule_show",
    "api_routing_rule_toggle",
    "api_runs_cancel",
    "api_runs_resume",
    "api_runs_show",
    "api_segment_approve",
    "api_segment_propagation",
    "api_segment_unlock",
    "api_segprofiles_assign",
    "api_segprofiles_clone",
    "api_segprofiles_create",
    "api_segprofiles_delete",
    "api_segprofiles_list",
    "api_segprofiles_rename",
    "api_segprofiles_set_default",
    "api_segprofiles_show",
    "api_srx_disable",
    "api_srx_enable",
    "api_srx_upload",
    "api_srx_validate",
    "api_style_guides_ab",
    "api_style_guides_create",
    "api_style_guides_delete",
    "api_style_guides_list",
    "api_style_guides_set_status",
    "api_style_guides_show",
    "api_style_guides_update",
    "api_suggestions_apply",
    "api_suggestions_approve",
    "api_suggestions_evidence",
    "api_suggestions_export",
    "api_suggestions_generate",
    "api_suggestions_list",
    "api_suggestions_recover",
    "api_suggestions_reject",
    "api_suggestions_retry",
    "api_suggestions_show",
    "api_tap_run_detail",
    "api_tap_runs",
    "api_templates_apply",
    "api_templates_create",
    "api_templates_delete",
    "api_templates_list",
    "api_templates_rename",
    "api_templates_show",
    "api_tm_bulk_delete",
    "api_tm_delete",
    "api_tm_edit",
    "api_tm_export",
    "api_tm_import",
    "api_tm_list",
    "api_tm_scrub_hedges",
    "api_tm_show",
    "api_tm_stats",
    "api_user_create",
    "api_user_customer",
    "api_user_department",
    "api_user_disable",
    "api_user_enable",
    "api_user_erase",
    "api_user_export",
    "api_user_invite",
    "api_user_invite_revoke",
    "api_user_invites",
    "api_user_list",
    "api_user_reset_password",
    "api_user_role",
    "api_user_show",
    "api_vendor_dispatch_detail",
    "api_vendor_dispatch_xliff",
    "api_vendor_dispatches",
    "api_vendor_poll",
    "api_vendor_quote_approve",
    "api_vendor_quote_decline",
    "api_vendor_quote_record",
    "api_vendor_reject_rework",
    "api_vendor_return",
    "api_vendor_secret_set",
    "api_vendors_create",
    "api_vendors_delete",
    "api_vendors_link_customer",
    "api_vendors_list",
    "api_vendors_rate_card",
    "api_vendors_show",
    "api_vendors_unlink_customer",
    "api_vendors_update",
    "api_workflow_groups_add",
    "api_workflow_groups_create",
    "api_workflow_groups_delete",
    "api_workflow_groups_list",
    "api_workflow_groups_rename",
    "api_workflow_groups_set_default",
    "api_workflow_groups_show",
    "api_workflows_add_step",
    "api_workflows_archive",
    "api_workflows_author",
    "api_workflows_clone",
    "api_workflows_copy",
    "api_workflows_create",
    "api_workflows_delete",
    "api_workflows_delete_step",
    "api_workflows_edit_step",
    "api_workflows_list",
    "api_workflows_move_step",
    "api_workflows_rename",
    "api_workflows_run",
    "api_workflows_run_batch",
    "api_workflows_show",
    "api_workflows_unarchive",
    "api_worklist",
    "audit",
    "base64",
    "batch_runs",
    "branding",
    "content",
    "content_types",
    "deps",
    "engine",
    "glossary",
    "ids",
    "job_history",
    "jobs",
    "json",
    "keys_svc",
    "languages",
    "math",
    "onboarding",
    "orchestrator",
    "pair_providers",
    "qthreads",
    "retry_after",
    "run_in_threadpool",
    "run_overlay",
    "security",
    "segmentation_profiles",
    "select",
    "stream_slot",
    "tm",
    "vendors",
    "workflows",
    "worklist",
    "wraps",
    "wshapes",
]

# Names of an absent optional add-on were never imported: drop them from the re-export list.
__all__ = [_n for _n in __all__ if _n in globals()]

router = APIRouter()


_srp_piece_router = api_cli_misc_1_router
api_cli_misc_1_router = _srp_copy(api_cli_misc_1_router)
api_cli_misc_1_router.routes = api_cli_misc_1_router.routes[0:2]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_1_router = _srp_piece_router


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_projects_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


_srp_piece_router = api_cli_projects_2_router
api_cli_projects_2_router = _srp_copy(api_cli_projects_2_router)
api_cli_projects_2_router.routes = api_cli_projects_2_router.routes[0:8]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_projects_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_projects_2_router = _srp_piece_router


# Human-facing (flag_type, label) pairs for review_summary_line, in report order.
# This MUST itemize EVERY family in _SEGMENT_SURFACED (asserted below). The header
# count is `len(flagged)` over segments carrying ANY surfaced flag, so a family the
# breakdown omits — a `qe`-gate quarantine (orchestrator) or a `nontranslation`
# language-mismatch (refusal) — would inflate the header while landing in no bucket,
# printing e.g. "1 flagged for review (dnt 0, glossary 0, refusal 0)": a header that
# names no reason, and (for the language mismatch) hides the very flag QA-5 added to
# make that quarantine legible. Keep aligned with _SEGMENT_SURFACED.
_SUMMARY_FAMILIES = (
    (quality.DNT_FLAG_TYPE, "dnt"),
    (quality.GLOSSARY_FLAG_TYPE, "glossary"),
    (quality.TAG_FLAG_TYPE, "tag"),
    (quality.NUMBER_FLAG_TYPE, "numbers"),
    (quality.PLACEHOLDER_FLAG_TYPE, "placeholder"),
    (quality.WHITESPACE_FLAG_TYPE, "whitespace"),
    (quality.REVIEW_HOLD_FLAG_TYPE, "review-hold"),
    (quality.LABEL_ECHO_FLAG_TYPE, "label-echo"),
    (quality.STEP_ERROR_FLAG_TYPE, "step-error"),
    (quality.SOURCE_FALLBACK_FLAG_TYPE, "source-fallback"),
    (lqa_safety.FIX_REVERTED_FLAG_TYPE, "fix-reverted"),
    (lqa_safety.FIX_REGRESSION_FLAG_TYPE, "fix-regression"),
    (refusal.FLAG_TYPE, "refusal"),
    (refusal.NONTRANSLATION_FLAG_TYPE, "nontranslation"),
    (output_guard.INSTRUCTION_ECHO_FLAG_TYPE, "instruction-echo"),
    (output_guard.SHORT_SOURCE_BLOWUP_FLAG_TYPE, "short-source-blowup"),
    (output_guard.NUMBER_MISMATCH_FLAG_TYPE, "number-mismatch"),
    ("qe", "qe"),
    ("qe_autofix", "auto-fix"),  # QE auto-fix record (Train B), beside its qe verdict
)
if {ft for ft, _ in _SUMMARY_FAMILIES} != set(_SEGMENT_SURFACED):  # survives python -O
    raise RuntimeError(
        "review_summary_line breakdown must itemize every _SEGMENT_SURFACED flag family"
    )


def review_summary_line(segment_details: list) -> str | None:
    """A calm one-line headless summary of the flagged segments in ONE locale group
    (the `doc show --detail` human affordance). Both CLI arms render it from the same
    `segment_details` rows, so the local and --remote outputs are identical. Returns
    None when nothing is flagged, so a clean locale prints no nagging line. The header
    counts SEGMENTS carrying review-worthy flags; passing QE, skipped QE (which does
    not route to review), and fixed/skipped auto-fix records are informational.
    The parenthetical itemizes each review-worthy family
    in _SUMMARY_FAMILIES (zero-count families dropped to stay calm), so the header is
    always fully accounted for — no `qe`-only or `nontranslation`-only quarantine can
    inflate the count without naming its reason."""

    def needs_review(flag):
        if flag.get("type") == "qe":
            return flag.get("passed") is not True and not flag.get("skipped")
        if flag.get("type") == "qe_autofix":
            return flag.get("outcome") not in ("fixed", "skipped")
        # #664: a stripped prompt label is already repaired; the note is informational.
        return flag.get("type") != quality.LABEL_ECHO_FLAG_TYPE

    flagged = [
        {"qa_flags": flags}
        for r in segment_details
        if (flags := [f for f in r.get("qa_flags", []) if needs_review(f)])
    ]
    if not flagged:
        return None

    def _n(flag_type: str) -> int:
        return sum(1 for r in flagged if any(f.get("type") == flag_type for f in r["qa_flags"]))

    parts = [f"{label} {n}" for ft, label in _SUMMARY_FAMILIES if (n := _n(ft))]
    # A flagged row always carries a surfaced flag (its qa_flags are pre-filtered to
    # _SEGMENT_SURFACED), so `parts` is never empty — but never print an empty "()" if
    # that invariant is ever broken upstream.
    if not parts:
        return f"{len(flagged)} flagged for review"
    return f"{len(flagged)} flagged for review ({', '.join(parts)})"


_srp_piece_router = api_cli_misc_1_router
api_cli_misc_1_router = _srp_copy(api_cli_misc_1_router)
api_cli_misc_1_router.routes = api_cli_misc_1_router.routes[2:8]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_1_router = _srp_piece_router


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_customers_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


_srp_piece_router = api_cli_vendors_router
api_cli_vendors_router = _srp_copy(api_cli_vendors_router)
api_cli_vendors_router.routes = api_cli_vendors_router.routes[0:17]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_vendors_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_vendors_router = _srp_piece_router


_srp_piece_router = api_cli_misc_1_router
api_cli_misc_1_router = _srp_copy(api_cli_misc_1_router)
api_cli_misc_1_router.routes = api_cli_misc_1_router.routes[8:13]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_1_router = _srp_piece_router


_srp_piece_router = api_cli_projects_2_router
api_cli_projects_2_router = _srp_copy(api_cli_projects_2_router)
api_cli_projects_2_router.routes = api_cli_projects_2_router.routes[8:14]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_projects_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_projects_2_router = _srp_piece_router


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_glossaries_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_glossaries_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


_srp_piece_router = api_cli_misc_2_router
api_cli_misc_2_router = _srp_copy(api_cli_misc_2_router)
api_cli_misc_2_router.routes = api_cli_misc_2_router.routes[0:10]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_2_router = _srp_piece_router


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_workflows_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


_srp_piece_router = api_cli_workflows_2_router
api_cli_workflows_2_router = _srp_copy(api_cli_workflows_2_router)
api_cli_workflows_2_router.routes = api_cli_workflows_2_router.routes[0:2]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_workflows_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_workflows_2_router = _srp_piece_router


_srp_piece_router = api_cli_misc_2_router
api_cli_misc_2_router = _srp_copy(api_cli_misc_2_router)
api_cli_misc_2_router.routes = api_cli_misc_2_router.routes[10:14]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_2_router = _srp_piece_router


_srp_piece_router = api_cli_workflows_2_router
api_cli_workflows_2_router = _srp_copy(api_cli_workflows_2_router)
api_cli_workflows_2_router.routes = api_cli_workflows_2_router.routes[2:4]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_workflows_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_workflows_2_router = _srp_piece_router


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_3_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


_srp_piece_router = api_cli_misc_4_router
api_cli_misc_4_router = _srp_copy(api_cli_misc_4_router)
api_cli_misc_4_router.routes = api_cli_misc_4_router.routes[0:10]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_4_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_4_router = _srp_piece_router


if api_cli_memories_router is not None:
    _srp_router_options = router.prefix, router.tags, router.dependencies
    router.prefix, router.tags, router.dependencies = "", [], []
    router.include_router(api_cli_memories_router)
    router.prefix, router.tags, router.dependencies = _srp_router_options


if api_cli_memory_suggestions_router is not None:
    _srp_router_options = router.prefix, router.tags, router.dependencies
    router.prefix, router.tags, router.dependencies = "", [], []
    router.include_router(api_cli_memory_suggestions_router)
    router.prefix, router.tags, router.dependencies = _srp_router_options


if api_cli_evaluations_1_router is not None:
    _srp_router_options = router.prefix, router.tags, router.dependencies
    router.prefix, router.tags, router.dependencies = "", [], []
    router.include_router(api_cli_evaluations_1_router)
    router.prefix, router.tags, router.dependencies = _srp_router_options


if api_cli_evaluations_2_router is not None:
    _srp_router_options = router.prefix, router.tags, router.dependencies
    router.prefix, router.tags, router.dependencies = "", [], []
    router.include_router(api_cli_evaluations_2_router)
    router.prefix, router.tags, router.dependencies = _srp_router_options


_srp_piece_router = api_cli_misc_2_router
api_cli_misc_2_router = _srp_copy(api_cli_misc_2_router)
api_cli_misc_2_router.routes = api_cli_misc_2_router.routes[14:21]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_2_router = _srp_piece_router


_srp_piece_router = api_cli_workflows_2_router
api_cli_workflows_2_router = _srp_copy(api_cli_workflows_2_router)
api_cli_workflows_2_router.routes = api_cli_workflows_2_router.routes[4:8]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_workflows_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_workflows_2_router = _srp_piece_router


_srp_piece_router = api_cli_misc_5_router
api_cli_misc_5_router = _srp_copy(api_cli_misc_5_router)
api_cli_misc_5_router.routes = api_cli_misc_5_router.routes[0:6]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_5_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_5_router = _srp_piece_router


_srp_piece_router = api_cli_misc_2_router
api_cli_misc_2_router = _srp_copy(api_cli_misc_2_router)
api_cli_misc_2_router.routes = api_cli_misc_2_router.routes[21:22]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_2_router = _srp_piece_router


_srp_piece_router = api_cli_misc_5_router
api_cli_misc_5_router = _srp_copy(api_cli_misc_5_router)
api_cli_misc_5_router.routes = api_cli_misc_5_router.routes[6:7]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_5_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_5_router = _srp_piece_router


_srp_piece_router = api_cli_misc_1_router
api_cli_misc_1_router = _srp_copy(api_cli_misc_1_router)
api_cli_misc_1_router.routes = api_cli_misc_1_router.routes[13:20]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_1_router = _srp_piece_router


_srp_piece_router = api_cli_misc_5_router
api_cli_misc_5_router = _srp_copy(api_cli_misc_5_router)
api_cli_misc_5_router.routes = api_cli_misc_5_router.routes[7:10]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_5_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_5_router = _srp_piece_router


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_users_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


_srp_piece_router = api_cli_misc_4_router
api_cli_misc_4_router = _srp_copy(api_cli_misc_4_router)
api_cli_misc_4_router.routes = api_cli_misc_4_router.routes[10:13]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_4_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_4_router = _srp_piece_router


_srp_piece_router = api_cli_projects_2_router
api_cli_projects_2_router = _srp_copy(api_cli_projects_2_router)
api_cli_projects_2_router.routes = api_cli_projects_2_router.routes[14:17]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_projects_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_projects_2_router = _srp_piece_router


_srp_piece_router = api_cli_misc_4_router
api_cli_misc_4_router = _srp_copy(api_cli_misc_4_router)
api_cli_misc_4_router.routes = api_cli_misc_4_router.routes[13:17]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_4_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_4_router = _srp_piece_router


_srp_piece_router = api_cli_misc_5_router
api_cli_misc_5_router = _srp_copy(api_cli_misc_5_router)
api_cli_misc_5_router.routes = api_cli_misc_5_router.routes[10:13]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_5_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_5_router = _srp_piece_router


_srp_piece_router = api_cli_misc_6_router
api_cli_misc_6_router = _srp_copy(api_cli_misc_6_router)
api_cli_misc_6_router.routes = api_cli_misc_6_router.routes[0:2]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_6_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_6_router = _srp_piece_router


_srp_piece_router = api_cli_admin_1_router
api_cli_admin_1_router = _srp_copy(api_cli_admin_1_router)
api_cli_admin_1_router.routes = api_cli_admin_1_router.routes[0:11]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_admin_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_admin_1_router = _srp_piece_router


_srp_piece_router = api_cli_misc_6_router
api_cli_misc_6_router = _srp_copy(api_cli_misc_6_router)
api_cli_misc_6_router.routes = api_cli_misc_6_router.routes[2:6]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_6_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_6_router = _srp_piece_router


_srp_piece_router = api_cli_admin_1_router
api_cli_admin_1_router = _srp_copy(api_cli_admin_1_router)
api_cli_admin_1_router.routes = api_cli_admin_1_router.routes[11:19]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_admin_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_admin_1_router = _srp_piece_router


_srp_piece_router = api_cli_admin_2_router
api_cli_admin_2_router = _srp_copy(api_cli_admin_2_router)
# [0:15] = features (2) + engines (4 + the 3 Jev Train C inspector routes) + provider keys
# (3) + search keys (3). A route added to api_cli_admin_2.py must move these bounds, or
# it is silently never served (the second slice below is open-ended so its tail cannot).
api_cli_admin_2_router.routes = api_cli_admin_2_router.routes[0:15]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_admin_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_admin_2_router = _srp_piece_router


_srp_piece_router = api_cli_misc_6_router
api_cli_misc_6_router = _srp_copy(api_cli_misc_6_router)
api_cli_misc_6_router.routes = api_cli_misc_6_router.routes[6:9]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_6_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_6_router = _srp_piece_router


_srp_piece_router = api_cli_vendors_router
api_cli_vendors_router = _srp_copy(api_cli_vendors_router)
api_cli_vendors_router.routes = api_cli_vendors_router.routes[17:18]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_vendors_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_vendors_router = _srp_piece_router


_srp_piece_router = api_cli_admin_2_router
api_cli_admin_2_router = _srp_copy(api_cli_admin_2_router)
api_cli_admin_2_router.routes = api_cli_admin_2_router.routes[15:]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_admin_2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_admin_2_router = _srp_piece_router


_srp_piece_router = api_cli_misc_6_router
api_cli_misc_6_router = _srp_copy(api_cli_misc_6_router)
api_cli_misc_6_router.routes = api_cli_misc_6_router.routes[9:16]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_6_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_6_router = _srp_piece_router


_srp_piece_router = api_cli_misc_1_router
api_cli_misc_1_router = _srp_copy(api_cli_misc_1_router)
api_cli_misc_1_router.routes = api_cli_misc_1_router.routes[20:23]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_1_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_1_router = _srp_piece_router


_srp_piece_router = api_cli_misc_6_router
api_cli_misc_6_router = _srp_copy(api_cli_misc_6_router)
api_cli_misc_6_router.routes = api_cli_misc_6_router.routes[16:18]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_6_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
api_cli_misc_6_router = _srp_piece_router


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(api_cli_misc_7_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
del _srp_piece_router, _srp_router_options  # splice temporaries (review gate M2)


# Import-time guard (#366): the fixed slice bounds above must cover every source
# route EXACTLY once. A route added mid-source shifts every later bound, so the
# stale slices then drop (or double-serve) a neighbour; /api/style-guides/{gid}/ab
# already shipped silently dead that way before this check. The splice pattern
# restores each source name to its full router, and include_router keeps each
# route's endpoint object across the copy, so the partition is checkable at
# import time by (endpoint identity, path, methods) tallies. The two whole-
# router includes at the bottom of this file join _SRP_SOURCES at the check
# below, so the check covers the assembled router and stays re-runnable.
_SRP_SOURCES = tuple(
    r
    for r in (  # an absent optional add-on's router is None (app/editions.py): nothing to splice
        api_cli_admin_1_router,
        api_cli_admin_2_router,
        api_cli_customers_router,
        api_cli_evaluations_1_router,
        api_cli_evaluations_2_router,
        api_cli_glossaries_1_router,
        api_cli_glossaries_2_router,
        api_cli_memories_router,
        api_cli_memory_suggestions_router,
        api_cli_misc_1_router,
        api_cli_misc_2_router,
        api_cli_misc_3_router,
        api_cli_misc_4_router,
        api_cli_misc_5_router,
        api_cli_misc_6_router,
        api_cli_misc_7_router,
        api_cli_projects_1_router,
        api_cli_projects_2_router,
        api_cli_users_router,
        api_cli_vendors_router,
        api_cli_workflows_1_router,
        api_cli_workflows_2_router,
    )
    if r is not None
)


def _srp_flattened(rtr):
    """Concrete routes of `rtr`. FastAPI >= 0.141 makes include_router lazy: the
    assembled router holds _IncludedRouter wrappers (which still reference the
    sliced copies) and effective_route_contexts() expands them to per-route
    contexts carrying path, methods and the original endpoint. Older FastAPI copies
    the routes eagerly, so plain routes pass through (that branch is dormant on the
    pinned version, kept so a downgrade or a rollback cannot break the import)."""
    flat = []
    for route in rtr.routes:
        if hasattr(route, "effective_route_contexts"):
            flat.extend(route.effective_route_contexts())
        else:
            flat.append(route)
    return flat


def _srp_slice_problems(served, sources):
    """Problems with splicing `sources` into `served`, empty when every source
    route was included exactly once. A count mismatch means the fixed slice
    bounds went stale (a route was added or removed mid-source); callers must
    fail loudly, because the silent outcome is an endpoint that 404s on the
    --remote transport while the in-process CLI arm still works."""

    def key(route):
        return (
            id(getattr(route, "endpoint", route)),
            getattr(route, "path", ""),
            tuple(sorted(getattr(route, "methods", None) or ())),
        )

    def label(route):
        # Mounts and websocket routes have no .methods or .endpoint; never let the
        # diagnosis itself raise AttributeError and hide the real problem.
        methods = ",".join(sorted(getattr(route, "methods", None) or ())) or "MOUNT"
        endpoint = getattr(route, "endpoint", None)
        qual = (
            getattr(endpoint, "__qualname__", None)
            or getattr(route, "name", None)
            or type(route).__name__
        )
        return f"{methods} {getattr(route, 'path', '?')} ({qual})"

    def tally(rtr):
        out = {}
        for route in _srp_flattened(rtr):
            k = key(route)
            if k not in out:
                out[k] = [0, label(route)]
            out[k][0] += 1
        return out

    served_counts = tally(served)
    expected_total = {}  # key -> [combined count, label, source-router count]
    problems = []
    for source in sources:
        for k, (expected, text) in tally(source).items():
            slot = expected_total.setdefault(k, [0, text, 0])
            slot[0] += expected
            slot[2] += 1
            got = served_counts.get(k, (0, None))[0]
            if got != expected:
                problems.append(f"{text}: spliced {got}x, source defines {expected}x")
    # A key defined by more than one source router must also reconcile in total:
    # the per-source check above passes 1==1 on each side even when the assembly
    # serves the combined two definitions only once.
    for k, (total, text, n_sources) in expected_total.items():
        if n_sources > 1:
            got = served_counts.get(k, (0, None))[0]
            if got != total:
                problems.append(f"{text}: served {got}x in total, sources define {total}x combined")
    for k, (got, text) in served_counts.items():
        if k not in expected_total:
            problems.append(f"{text}: spliced {got}x, no source router defines it")
    # Never pass vacuously: if a FastAPI change hides every route from
    # _srp_flattened on both sides, the tallies above agree at zero.
    if not expected_total:
        problems.append("no source routes visible (FastAPI router internals changed?)")
    return problems


# Editable page layouts (train L1): the self-service /api/account/layouts routes live in
# app/web/page_layouts.py and ride this router, so the pinned router assembly in
# app/modules.py ROUTER_GROUPS is unchanged.
from app.web.page_layouts import api_router as _page_layouts_api_router  # noqa: E402

router.include_router(_page_layouts_api_router)

# Read-only built-in system prompts (#356 phase 1); own router for the same reason.
from app.web.system_prompts_api import api_router as _system_prompts_api_router  # noqa: E402

router.include_router(_system_prompts_api_router)

# #595 global search: GET /api/search lives in app/web/search.py and rides this router.
from app.web.search import api_router as _search_api_router  # noqa: E402

router.include_router(_search_api_router)

# #617 item 7 MT formality: GET/POST /api/formality and /api/formality/registers live in
# app/web/formality_routes.py and ride this router.
from app.web.formality_routes import api_router as _formality_api_router  # noqa: E402

router.include_router(_formality_api_router)

# Project automation Part B: /api/notify/* (channels, routes, per-project view) live in
# app/web/notify_routes.py and ride this router.
from app.web.notify_routes import api_router as _notify_api_router  # noqa: E402

router.include_router(_notify_api_router)

# ADR 0063: POST /api/account/theme (per-user appearance) lives in app/web/account_theme_api.py
# and rides this router for the same reason (no fixed slice above moves).
from app.web.account_theme_api import api_router as _account_theme_api_router  # noqa: E402

router.include_router(_account_theme_api_router)

# Project automation: /api/projects/{id}/automation, /source and /runs live in
# app/web/project_automation.py and ride this router (no fixed slice above moves).
from app.web.project_automation import api_router as _project_automation_api_router  # noqa: E402

router.include_router(_project_automation_api_router)

# #366: fail the import unless the assembly above is an exact partition of its
# sources (every source route spliced exactly once; the two whole-router
# includes count as covered by construction).
_srp_guard_sources = (
    *_SRP_SOURCES,
    _page_layouts_api_router,
    _system_prompts_api_router,
    _search_api_router,
    _formality_api_router,
    _notify_api_router,
    _account_theme_api_router,
    _project_automation_api_router,
)
_srp_problems = _srp_slice_problems(router, _srp_guard_sources)
if _srp_problems:
    raise RuntimeError(
        "api_cli.py router assembly is not an exact partition of its source routers "
        "(stale fixed slice bounds after a mid-source route change?): " + "; ".join(_srp_problems)
    )
